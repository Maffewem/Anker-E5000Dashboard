"""The read-only Modbus relay, read through with a real Modbus client."""

import asyncio
import time

from pymodbus.client import AsyncModbusTcpClient

from app.collector import Collector
from app.config import Connection, Settings
from app.registers import METER, METER_REGISTERS, decode
from app.relay import Relay
from simulator.sim import METER_IMPLEMENTED, METER_STATIC, Registers, handle, meter_values


def test_relay_serves_meter_registers_and_refuses_writes():
    async def run():
        regs = Registers(METER_REGISTERS, METER_IMPLEMENTED)
        regs.write(METER_STATIC)
        regs.write(meter_values({"grid_power": -750}, 1000.0, 2000.0))
        server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "127.0.0.1", 0)
        conn = Connection("127.0.0.1", server.sockets[0].getsockname()[1], 1)
        collector = Collector(Settings(5, 365, ":memory:", "unused.json", "UTC"), None, conn, METER)
        relay = Relay(collector, 0, "127.0.0.1")
        await relay.start()
        client = AsyncModbusTcpClient("127.0.0.1", port=relay.port, timeout=2, retries=0)
        try:
            await client.connect()
            # Before the first poll there is nothing to relay.
            before = await client.read_input_registers(10620, count=28, device_id=1)
            await collector.poll_once()
            # Home Assistant's identification read and its batch ranges.
            pn = await client.read_input_registers(32768, count=5, device_id=1)
            batches = [await client.read_input_registers(a, count=b - a + 1, device_id=1)
                       for a, b in ((10620, 10647), (10648, 10695), (10696, 10712))]
            gap = await client.read_input_registers(20000, count=2, device_id=1)
            holding = await client.read_holding_registers(10620, count=1, device_id=1)
            write = await client.write_register(10630, 2, device_id=1)
            writes = await client.write_registers(10630, [2, 2], device_id=1)
            after_writes = await client.read_input_registers(10630, count=1, device_id=1)
        finally:
            client.close()
            await relay.close()
            collector.close()
            server.close()
        return before, pn, batches, gap, holding, write, writes, after_writes, regs

    before, pn, batches, gap, holding, write, writes, after_writes, regs = asyncio.run(run())
    assert before.isError() and before.exception_code == 0x0B
    assert decode("STRING", pn.registers) == METER_STATIC["meter_pn"]
    for b in batches:
        assert not b.isError()
    start = 10620
    assert batches[0].registers == regs.read(4, start, 28)
    assert batches[1].registers == regs.read(4, 10648, 48)
    assert gap.isError() and gap.exception_code == 2
    assert holding.isError()  # the meter has no holding registers
    assert write.isError() and write.exception_code == 1
    assert writes.isError() and writes.exception_code == 1
    assert after_writes.registers == [METER_STATIC["meter_type"]]  # nothing changed


def test_relay_goes_quiet_when_the_meter_drops():
    collector = Collector(Settings(5, 365, ":memory:", "unused.json", "UTC"), None, Connection(), METER)
    collector.words = {(4, 10630): 1}
    collector.connected = True
    collector.last_update = time.time()
    relay = Relay(collector, 0)
    request = bytes([4]) + (10630).to_bytes(2, "big") + (1).to_bytes(2, "big")
    assert relay.answer(request) == bytes([4, 2, 0, 1])
    collector.last_update -= 120  # no fresh poll for two minutes
    assert relay.answer(request) == bytes([0x84, 0x0B])
