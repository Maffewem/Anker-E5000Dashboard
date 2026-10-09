"""Poll the bundled simulator over a real TCP socket."""

import asyncio

from app.collector import Collector
from app.config import Connection, Settings
from app.storage import Storage
from simulator.sim import STATIC, Battery, Registers, handle


def test_collector_reads_simulator():
    async def run():
        regs = Registers()
        regs.write(STATIC)
        regs.write(Battery().step())
        server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = Settings(5, 365, ":memory:", "unused.json", "UTC")
        collector = Collector(settings, Storage(":memory:", 365), Connection("127.0.0.1", port, 1))
        try:
            await collector.poll_once()
        finally:
            collector.close()
            server.close()
        return collector

    c = asyncio.run(run())
    assert c.connected
    assert c.snapshot["serial"] == STATIC["device_sn"]
    assert c.snapshot["rated_kwh"] == 5.0
    assert c.snapshot["operating_mode"] == "Smart"
    assert c.snapshot["soc"] is not None
