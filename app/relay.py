"""Read-only Modbus TCP relay: re-serves the registers a collector last read.

The Smart Meter accepts a single Modbus TCP client, so the dashboard and Home
Assistant can't both talk to it. With the relay on, the dashboard keeps that
one connection and answers Home Assistant (or anything else) from the words
it read on its last poll, at the same addresses and unit id.

It never forwards anything to the device. Reads (function codes 3 and 4) are
answered from the cache; every other function, writes included, gets an
"illegal function" exception.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from pathlib import Path

from .collector import DEVICE_NAMES, Collector
from .registers import Register

log = logging.getLogger("solarbank.relay")

ILLEGAL_FUNCTION = 0x01
ILLEGAL_ADDRESS = 0x02
TARGET_FAILED = 0x0B  # "gateway target device failed to respond"

# Registers Home Assistant reads to recognise the device (model, firmware,
# serial, product number). They never change, so they are kept and served even
# while the meter is unreachable. Otherwise a reload of the integration during
# an outage fails identification and shows "Device not supported".
IDENTITY_KEYS = ("meter_model", "meter_sw_version", "meter_sn", "meter_pn")


class Relay:
    def __init__(self, collector: Collector, port: int, host: str = "0.0.0.0") -> None:
        self.collector = collector
        self.port = port
        self.host = host
        self.server: asyncio.base_events.Server | None = None
        self.identity_path = Path(collector.settings.settings_path).with_name("meter-identity.json")
        self.identity: dict[tuple[int, int], int] = self._load_identity()

    def _identity_regs(self) -> list[Register]:
        return [r for r in self.collector.profile.registers if r.key in IDENTITY_KEYS]

    def _load_identity(self) -> dict[tuple[int, int], int]:
        try:
            data = json.loads(self.identity_path.read_text())
            return {(int(k.split(":")[0]), int(k.split(":")[1])): int(v) for k, v in data.items()}
        except (OSError, ValueError, IndexError, AttributeError):
            return {}

    def _remember_identity(self) -> None:
        """Keep the identification words from the latest poll, on disk too."""
        words = self.collector.words
        found = {}
        for reg in self._identity_regs():
            keys = [(4, reg.address + i) for i in range(reg.count)]
            if all(k in words for k in keys):
                found.update({k: words[k] for k in keys})
        if found and found != {k: self.identity.get(k) for k in found}:
            self.identity.update(found)
            try:
                self.identity_path.write_text(json.dumps({f"{f}:{a}": v for (f, a), v in self.identity.items()}))
            except OSError as err:
                log.warning("Couldn't save the meter identity to %s: %s", self.identity_path, err)

    @property
    def name(self) -> str:
        return DEVICE_NAMES[self.collector.profile.name]

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("Relaying the %s read-only on Modbus TCP port %s", self.name, self.port)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def fresh(self) -> bool:
        """Whether the cached words are recent enough to hand out."""
        c = self.collector
        max_age = max(30, c.settings.poll_seconds * 6)
        return c.connected and c.last_update is not None and time.time() - c.last_update <= max_age

    def answer(self, pdu: bytes) -> bytes:
        """The response PDU for one request PDU."""
        fc = pdu[0]
        if fc not in (3, 4):
            return bytes([(fc | 0x80) & 0xFF, ILLEGAL_FUNCTION])
        if len(pdu) < 5:
            return bytes([fc | 0x80, ILLEGAL_ADDRESS])
        address, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= 125:
            return bytes([fc | 0x80, ILLEGAL_ADDRESS])
        self._remember_identity()
        if self.fresh():
            words = self.collector.words
        else:
            # Stale or not yet polled: only the fixed identification words can be answered.
            words = self.identity
            if not all((fc, a) in words for a in range(address, address + count)):
                return bytes([fc | 0x80, TARGET_FAILED])
        try:
            values = [words[(fc, a)] for a in range(address, address + count)]
        except KeyError:
            # Not read from the device (or the device refused it): answer
            # like the device would for an unimplemented range.
            return bytes([fc | 0x80, ILLEGAL_ADDRESS])
        return bytes([fc, count * 2]) + struct.pack(f">{count}H", *values)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        who = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        log.info("Relay client connected from %s", who)
        writes = 0
        try:
            while True:
                header = await reader.readexactly(7)
                tid, protocol, length, unit = struct.unpack(">HHHB", header)
                if protocol != 0 or not 2 <= length <= 254:
                    break  # not Modbus TCP
                pdu = await reader.readexactly(length - 1)
                body = self.answer(pdu)
                if pdu[0] not in (3, 4):
                    writes += 1
                    if writes == 1:
                        log.warning("Relay refused function code %s from %s: the relay is read-only", pdu[0], who)
                writer.write(struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            log.info("Relay client %s disconnected", who)
            writer.close()
