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


def test_collector_reads_simulated_meter():
    from app.registers import METER, METER_REGISTERS
    from simulator.sim import METER_IMPLEMENTED, METER_STATIC, meter_values

    async def run():
        regs = Registers(METER_REGISTERS, METER_IMPLEMENTED)
        regs.write(METER_STATIC)
        regs.write(meter_values({"grid_power": -750}, 1000.0, 2000.0))
        server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = Settings(5, 365, ":memory:", "unused.json", "UTC")
        collector = Collector(settings, None, Connection("127.0.0.1", port, 1), METER)
        try:
            await collector.poll_once()
        finally:
            collector.close()
            server.close()
        return collector

    c = asyncio.run(run())
    assert c.snapshot["grid_w"] == -750
    assert c.snapshot["serial"] == METER_STATIC["meter_sn"]
    assert c.snapshot["firmware"] == "1.0.4.2"
    assert c.snapshot["meter_type"] == "Single phase"
    assert c.snapshot["import_total_kwh"] == 1.0
    assert len(c.snapshot["phases"]) == 1


def test_meter_that_refuses_block_reads():
    # Some firmware rejects a range spanning unimplemented addresses but
    # answers each register on its own; Anker's integration falls back the
    # same way. Implement only the mapped registers, so every block fails.
    from app.collector import probe
    from app.registers import METER, METER_REGISTERS
    from simulator.sim import METER_STATIC, meter_values

    only_mapped = {4: {r.address + i for r in METER_REGISTERS for i in range(r.count)}}

    async def run():
        regs = Registers(METER_REGISTERS, only_mapped)
        regs.write(METER_STATIC)
        regs.write(meter_values({"grid_power": 420}, 1000.0, 2000.0))
        server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = Settings(5, 365, ":memory:", "unused.json", "UTC")
        collector = Collector(settings, None, Connection("127.0.0.1", port, 1), METER)
        try:
            found = await probe(Connection("127.0.0.1", port, 1), METER)
            await collector.poll_once()
        finally:
            collector.close()
            server.close()
        return found, collector

    found, c = asyncio.run(run())
    assert found["serial"] == METER_STATIC["meter_sn"]
    assert found["grid_w"] == 420
    assert c.snapshot["grid_w"] == 420
    assert c.snapshot["firmware"] == "1.0.4.2"


def test_setup_test_never_opens_a_second_connection():
    # The Smart Meter accepts one Modbus client at a time and drops any other.
    from app.registers import METER, METER_REGISTERS
    from simulator.sim import METER_IMPLEMENTED, METER_STATIC, meter_values

    async def run():
        regs = Registers(METER_REGISTERS, METER_IMPLEMENTED)
        regs.write(METER_STATIC)
        regs.write(meter_values({"grid_power": 300}, 1000.0, 2000.0))
        active = []

        async def one_slot(reader, writer):
            if active:
                writer.close()
                return
            active.append(writer)
            try:
                await handle(regs, reader, writer)
            finally:
                active.remove(writer)

        server = await asyncio.start_server(one_slot, "127.0.0.1", 0)
        conn = Connection("127.0.0.1", server.sockets[0].getsockname()[1], 1)
        settings = Settings(5, 365, ":memory:", "unused.json", "UTC")
        collector = Collector(settings, None, conn, METER)
        try:
            await collector.poll_once()
            same = await collector.test(conn)  # reuses the poller's connection
            collector.connected = False  # e.g. mid-retry: the test must free the slot itself
            again = await collector.test(conn)
            await collector.poll_once()  # and polling reconnects afterwards
        finally:
            collector.close()
            server.close()
        return same, again, collector

    same, again, c = asyncio.run(run())
    assert same["serial"] == again["serial"] == METER_STATIC["meter_sn"]
    assert c.snapshot["grid_w"] == 300
