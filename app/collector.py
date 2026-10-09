"""Polls the Solarbank over Modbus TCP and records what it sees."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .config import Settings
from .registers import HOLDING, READ_BLOCKS, derive, extract
from .storage import MinuteBucket, Storage

log = logging.getLogger("solarbank.collector")


class Collector:
    def __init__(self, settings: Settings, storage: Storage) -> None:
        self.settings = settings
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
            "configured": bool(self.settings.host),
            "host": self.settings.host,
            "connected": self.connected,
            "last_update": self.last_update,
            "last_error": self.last_error,
            "poll_seconds": self.settings.poll_seconds,
        }

    async def run(self) -> None:
        if not self.settings.host:
            self.last_error = "SOLARBANK_HOST is not set"
            log.error(self.last_error)
            return
        backoff = self.settings.poll_seconds
        while True:
            started = time.monotonic()
            try:
                await self.poll_once()
                backoff = self.settings.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as err:  # keep polling whatever happens
                self._mark_offline(str(err) or err.__class__.__name__)
                backoff = min(backoff * 2, 60)
                log.warning("Poll failed (%s); retrying in %ss", self.last_error, backoff)
                await asyncio.sleep(backoff)
                continue
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self.settings.poll_seconds - elapsed))

    async def poll_once(self) -> None:
        client = await self._ensure_client()
        blocks: dict[tuple[str, int], list[int]] = {}
        for kind, start, end in READ_BLOCKS:
            count = end - start + 1
            reader = client.read_holding_registers if kind == HOLDING else client.read_input_registers
            try:
                result = await reader(start, count=count, device_id=self.settings.unit_id)
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
        if self.client is None:
            self.client = AsyncModbusTcpClient(
                self.settings.host,
                port=self.settings.port,
                timeout=5,
                retries=1,
            )
        if not self.client.connected:
            log.info("Connecting to %s:%s", self.settings.host, self.settings.port)
            if not await self.client.connect():
                raise ConnectionError(f"cannot connect to {self.settings.host}:{self.settings.port}")
        return self.client

    def _mark_offline(self, reason: str) -> None:
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
