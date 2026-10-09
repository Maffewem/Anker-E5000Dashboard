"""A fake Solarbank 4 E5000 Pro that answers Modbus TCP reads.

Lets you try the dashboard without the real battery:

    python -m simulator.sim --port 5020

Values follow a rough sunny-day curve and change every couple of seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import struct
import time
from datetime import datetime

from app.registers import HOLDING, REGISTERS, encode

STATIC = {
    "battery_status": 1,
    "max_charge_power": 2500,
    "max_discharge_power": 2500,
    "device_sn": "APCDN7M0SIMULATOR1",
    "device_sw_version": "v1.0.0",
    "rated_energy": 50,  # 5.0 kWh
    "device_model": "A17C5",
    "operating_mode": 6,
    "charging_limit_soc": 100,
    "discharge_limit_soc": 5,
    "backup_reserve_soc": 10,
}


class Battery:
    def __init__(self) -> None:
        self.soc = 55.0
        self.pv_total = 12345.0  # Wh
        self.charged = 4321.0
        self.discharged = 4000.0
        self.last = time.time()

    def step(self) -> dict:
        now = time.time()
        dt_h = (now - self.last) / 3600
        self.last = now
        hour = datetime.now().hour + datetime.now().minute / 60
        sun = max(0.0, math.sin(math.pi * (hour - 6) / 14))
        solar = round(3200 * sun * random.uniform(0.85, 1.0))
        home = round(350 + 250 * random.random() + (900 if 17 <= hour <= 20 else 0))
        surplus = solar - home
        if surplus > 0 and self.soc < 100:
            battery = -min(surplus, 2500)  # charging
        elif surplus < 0 and self.soc > 5:
            battery = min(-surplus, 2500)  # discharging
        else:
            battery = 0
        grid = home - solar - battery
        self.soc = min(100.0, max(0.0, self.soc - battery * dt_h / 5000 * 100))
        self.pv_total += solar * dt_h
        if battery < 0:
            self.charged += -battery * dt_h
        else:
            self.discharged += battery * dt_h
        status = 1 if battery < 0 else 2 if battery > 0 else 0
        return {
            "battery_status": status,
            "pv_power_pcs": solar,
            "pv_power_third_party": 0,
            "battery_power": battery,
            "load_power": home,
            "grid_power": grid,
            "battery_soc": round(self.soc),
            "pv_total_generation": round(self.pv_total / 100),  # kWh * 10
            "ac_output_power": home - grid,
            "cumulative_charge_energy": round(self.charged / 100),
            "cumulative_discharge_energy": round(self.discharged / 100),
        }


class Registers:
    """Holds the simulated register values, keyed by (function code, address)."""

    def __init__(self) -> None:
        self.words: dict[tuple[int, int], int] = {}

    def write(self, values: dict) -> None:
        for reg in REGISTERS:
            if reg.key in values:
                fc = 3 if reg.kind == HOLDING else 4
                for i, word in enumerate(encode(reg.data_type, values[reg.key], reg.count)):
                    self.words[(fc, reg.address + i)] = word

    def read(self, fc: int, address: int, count: int) -> list[int] | None:
        """Mimic the device: implemented ranges read as zero-filled, others fail."""
        implemented = IMPLEMENTED[fc]
        if not all(a in implemented for a in range(address, address + count)):
            return None
        return [self.words.get((fc, a), 0) for a in range(address, address + count)]


IMPLEMENTED = {
    4: {*range(10000, 10266), *range(32768, 32775)},
    3: {*range(10060, 10082), *range(60000, 60004)},
}


async def handle(regs: Registers, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal Modbus TCP: function codes 3 and 4 only."""
    try:
        while True:
            header = await reader.readexactly(7)
            tid, pid, length, unit = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            fc = pdu[0]
            if fc in (3, 4) and len(pdu) >= 5:
                address, count = struct.unpack(">HH", pdu[1:5])
                words = regs.read(fc, address, count) if 1 <= count <= 125 else None
                if words is None:
                    body = bytes([fc | 0x80, 2])  # illegal data address
                else:
                    body = bytes([fc, count * 2]) + struct.pack(f">{count}H", *words)
            else:
                body = bytes([(fc | 0x80) & 0xFF, 1])  # illegal function
            writer.write(struct.pack(">HHHB", tid, pid, len(body) + 1, unit) + body)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def update_loop(regs: Registers, battery: Battery) -> None:
    while True:
        regs.write(battery.step())
        await asyncio.sleep(2)


async def main(port: int) -> None:
    regs = Registers()
    battery = Battery()
    regs.write(STATIC)
    regs.write(battery.step())
    asyncio.create_task(update_loop(regs, battery))
    server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "0.0.0.0", port)
    print(f"Simulated Solarbank listening on 0.0.0.0:{port}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5020)
    asyncio.run(main(parser.parse_args().port))
