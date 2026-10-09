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
import logging
import struct
import time

from .collector import DEVICE_NAMES, Collector

log = logging.getLogger("solarbank.relay")

ILLEGAL_FUNCTION = 0x01
ILLEGAL_ADDRESS = 0x02
TARGET_FAILED = 0x0B  # "gateway target device failed to respond"


class Relay:
    def __init__(self, collector: Collector, port: int, host: str = "0.0.0.0") -> None:
        self.collector = collector
        self.port = port
        self.host = host
        self.server: asyncio.base_events.Server | None = None

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
        if not self.fresh():
            return bytes([fc | 0x80, TARGET_FAILED])
        words = self.collector.words
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
