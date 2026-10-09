"""Polls the Solarbank over Modbus TCP and records what it sees."""

from __future__ import annotations

import asyncio
import errno
import logging
import socket
import time
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .config import Connection, Settings
from .registers import HOLDING, INPUT, READ_BLOCKS, derive, extract
from .storage import MinuteBucket, Storage

log = logging.getLogger("solarbank.collector")


class Collector:
    def __init__(self, settings: Settings, storage: Storage, connection: Connection) -> None:
        self.settings = settings
        self.connection = connection
        self._changed = asyncio.Event()
        self.storage = storage
        self.client: AsyncModbusTcpClient | None = None
        self.snapshot: dict[str, Any] = {}
        self.raw: dict[str, Any] = {}
        self.connected = False
        self.last_update: float | None = None
        self.last_error: str | None = None
        self._bucket: MinuteBucket | None = None
        self._last_sample: float | None = None
        self._last_prune = 0.0
        self._unavailable_blocks: set[tuple[str, int]] = set()

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.connection.host),
            "host": self.connection.host,
            "connected": self.connected,
            "last_update": self.last_update,
            "last_error": self.last_error,
            "poll_seconds": self.settings.poll_seconds,
        }

    def reconfigure(self, connection: Connection) -> None:
        """Switch to a different battery address; takes effect immediately."""
        self.connection = connection
        self._mark_offline(None)
        self.snapshot, self.raw = {}, {}
        self.last_update = None
        self._unavailable_blocks.clear()
        self._changed.set()

    async def _wait(self, seconds: float | None) -> None:
        """Sleep, but wake early if the connection settings change."""
        try:
            await asyncio.wait_for(self._changed.wait(), seconds)
        except asyncio.TimeoutError:
            pass
        self._changed.clear()

    async def run(self) -> None:
        backoff = self.settings.poll_seconds
        while True:
            if not self.connection.host:
                log.info("No battery address yet; waiting for setup")
                await self._wait(None)
                continue
            started = time.monotonic()
            conn = self.connection
            try:
                await self.poll_once()
                backoff = self.settings.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as err:  # keep polling whatever happens
                if self.connection is not conn:
                    continue  # address changed mid-poll; start over with the new one
                self._mark_offline(str(err) or err.__class__.__name__)
                backoff = min(backoff * 2, 60)
                log.warning("Poll failed (%s); retrying in %ss", self.last_error, backoff)
                await self._wait(backoff)
                continue
            elapsed = time.monotonic() - started
            await self._wait(max(0.0, self.settings.poll_seconds - elapsed))

    async def poll_once(self) -> None:
        conn = self.connection
        client = await self._ensure_client()
        blocks: dict[tuple[str, int], list[int]] = {}
        for kind, start, end in READ_BLOCKS:
            count = end - start + 1
            reader = client.read_holding_registers if kind == HOLDING else client.read_input_registers
            try:
                result = await reader(start, count=count, device_id=self.connection.unit_id)
            except ModbusException as err:
                if not client.connected:
                    raise ConnectionError(f"lost connection: {err}") from err
                self._note_unavailable(kind, start, str(err))
                continue
            if result.isError():
                # An exception response (e.g. illegal address on older firmware)
                # only loses this block, not the whole poll.
                self._note_unavailable(kind, start, str(result))
                continue
            blocks[(kind, start)] = list(result.registers)

        if self.connection is not conn:
            return  # reconfigured while reading; drop the old battery's data
        if not blocks:
            raise ConnectionError("device answered but returned no data")

        self.raw = extract(blocks)
        self.snapshot = derive(self.raw)
        now = time.time()
        self.connected = True
        self.last_update = now
        self.last_error = None
        self._record(now)

    def _note_unavailable(self, kind: str, start: int, reason: str) -> None:
        if (kind, start) not in self._unavailable_blocks:
            self._unavailable_blocks.add((kind, start))
            log.info("%s registers from %s unavailable: %s", kind, start, reason)

    def _record(self, now: float) -> None:
        # Credit each sample with the time since the previous one, capped so a
        # long outage doesn't get counted as energy at the last known power.
        cap = self.settings.poll_seconds * 3
        seconds = min(now - self._last_sample, cap) if self._last_sample else self.settings.poll_seconds
        self._last_sample = now

        minute = int(now // 60 * 60)
        if self._bucket and self._bucket.minute != minute:
            self.storage.write_minute(self._bucket.row())
            self._bucket = None
        if self._bucket is None:
            self._bucket = MinuteBucket(minute)
        self._bucket.add(self.snapshot, seconds)

        if now - self._last_prune > 3600:
            self.storage.prune()
            self._last_prune = now

    def flush(self) -> None:
        if self._bucket:
            self.storage.write_minute(self._bucket.row())
            self._bucket = None

    async def _ensure_client(self) -> AsyncModbusTcpClient:
        conn = self.connection
        if self.client is None:
            self.client = AsyncModbusTcpClient(conn.host, port=conn.port, timeout=5, retries=1)
        if not self.client.connected:
            log.info("Connecting to %s:%s", conn.host, conn.port)
            await tcp_check(conn.host, conn.port)
            if not await self.client.connect():
                raise ConnectionError(f"cannot connect to {conn.host}:{conn.port}")
        return self.client

    def _mark_offline(self, reason: str | None) -> None:
        self.connected = False
        self.last_error = reason
        self._last_sample = None
        if self.client is not None:
            self.client.close()
            self.client = None

    def close(self) -> None:
        self.flush()
        if self.client is not None:
            self.client.close()
            self.client = None


async def tcp_check(host: str, port: int, timeout: float = 4) -> None:
    """Open a plain TCP connection first so failures can be explained.

    pymodbus only reports "failed to connect"; the underlying OS error says
    whether the address is unroutable, the port is closed, or nothing answered.
    """
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        raise ConnectionError(
            f"No answer from {host}. Check the IP address in the Anker app "
            "(Three-Party Control Settings) and that the battery is online."
        ) from None
    except OSError as err:
        if err.errno == errno.ENETUNREACH or err.errno == errno.EHOSTUNREACH:
            raise ConnectionError(
                f"No network route from this container to {host}. Check the address is on your "
                "home network; if it is, run the container with host networking "
                "(network_mode: host)."
            ) from None
        if err.errno == errno.ECONNREFUSED:
            raise ConnectionError(
                f"{host} is reachable but port {port} is closed. Turn on Modbus TCP in the Anker app "
                "(Device > Settings > Three-Party Control Settings)."
            ) from None
        if isinstance(err, socket.gaierror):
            raise ConnectionError(f"Can't resolve the name {host}. Use the battery's IP address instead.") from None
        raise ConnectionError(f"Can't connect to {host}:{port} ({err.strerror or err})") from None
    writer.close()


async def probe(conn: Connection) -> dict[str, Any]:
    """Check that a Solarbank answers at `conn`; returns its identity.

    Used by the setup screen before saving, so a typo is caught straight away.
    """
    await tcp_check(conn.host, conn.port)
    client = AsyncModbusTcpClient(conn.host, port=conn.port, timeout=4, retries=0)
    try:
        if not await client.connect():
            raise ConnectionError(
                f"Nothing answered at {conn.host}:{conn.port}. Check the IP address and that "
                "Modbus TCP is turned on in the Anker app."
            )
        blocks: dict[tuple[str, int], list[int]] = {}
        for kind, start, end in READ_BLOCKS:
            if kind != INPUT:
                continue
            result = await client.read_input_registers(start, count=end - start + 1, device_id=conn.unit_id)
            if not result.isError():
                blocks[(kind, start)] = list(result.registers)
        if not blocks:
            raise ConnectionError(
                f"{conn.host}:{conn.port} answered but returned no Solarbank data. "
                "Is this the right device?"
            )
        snap = derive(extract(blocks))
        return {k: snap.get(k) for k in ("model", "serial", "firmware", "soc")}
    except ModbusException as err:
        raise ConnectionError(f"{conn.host}:{conn.port} did not respond to Modbus requests ({err})") from err
    finally:
        client.close()
