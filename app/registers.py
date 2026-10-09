"""Modbus register map for the Anker SOLIX Solarbank 4 E5000 (Pro).

Addresses, types and scaling come from Anker's official Home Assistant
integration (MIT licensed):
https://github.com/anker-charging/ha-anker-solix-official
(custom_components/anker_solix_official/config/58f0132b...yaml)

Reading is the default. The only writes are the WRITABLE registers below,
made by app/control.py when battery control is switched on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INPUT = "input"
HOLDING = "holding"


@dataclass(frozen=True)
class Register:
    key: str
    address: int
    data_type: str  # UINT16, INT16, UINT32, INT32, STRING
    count: int = 1
    gain: int = 1  # raw value is divided by this
    kind: str = INPUT


REGISTERS: list[Register] = [
    # Live power (W). Sign conventions follow the official integration.
    Register("battery_status", 10001, "UINT16"),
    Register("pv_power_pcs", 10002, "INT32", 2),
    Register("pv_power_third_party", 10004, "INT32", 2),
    Register("battery_power", 10008, "INT32", 2),  # + discharging, - charging
    Register("load_power", 10010, "INT32", 2),
    Register("grid_power", 10012, "INT32", 2),  # + import, - export
    Register("battery_soc", 10014, "UINT16"),
    Register("pv_total_generation", 10018, "UINT32", 2, gain=10),  # kWh
    Register("max_charge_power", 10036, "INT32", 2),
    Register("max_discharge_power", 10038, "INT32", 2),
    # Identity
    Register("device_sn", 10100, "STRING", 12),
    Register("device_sw_version", 10112, "STRING", 6),
    # AC side and battery totals
    Register("ac_output_power", 10208, "INT32", 2),
    Register("rated_energy", 10250, "UINT32", 2, gain=10),  # kWh
    Register("cumulative_charge_energy", 10262, "UINT32", 2, gain=10),  # kWh
    Register("cumulative_discharge_energy", 10264, "UINT32", 2, gain=10),  # kWh
    Register("device_model", 32768, "STRING", 5),
    # Settings (holding registers, read only here)
    Register("operating_mode", 10064, "UINT16", kind=HOLDING),
    Register("charging_limit_soc", 60000, "UINT16", kind=HOLDING),
    Register("discharge_limit_soc", 60001, "UINT16", kind=HOLDING),
    Register("backup_reserve_soc", 60002, "UINT16", kind=HOLDING),
]

# Contiguous blocks to read in one request each, mirroring the official
# batch ranges. 32774 (ems mode mask) is left out on purpose; some firmware
# pads a batch that spans unimplemented addresses with zeros.
READ_BLOCKS: list[tuple[str, int, int]] = [
    (INPUT, 10000, 10050),
    (INPUT, 10090, 10156),
    (INPUT, 10208, 10265),
    (INPUT, 32768, 32772),
    (HOLDING, 10064, 10064),
    (HOLDING, 60000, 60002),
]

# Registers battery control may write. The setpoint is only obeyed in
# third-party control mode (operating_mode 3): negative charges, positive
# discharges, 0 holds. The official integration never reads it back.
WRITABLE = {
    "operating_mode": (10064, "UINT16"),
    "battery_power_setpoint": (10071, "INT32"),
}
THIRD_PARTY_MODE = 3

BATTERY_STATUS = {0: "standby", 1: "charging", 2: "discharging", 3: "sleep"}

OPERATING_MODES = {
    0: "Self-consumption",
    1: "Time of use",
    3: "Third-party control",
    4: "Custom",
    5: "Socket overlay",
    6: "Smart",
    7: "Dynamic pricing",
}


def decode(data_type: str, words: list[int]) -> Any:
    """Decode big-endian Modbus words into a Python value."""
    if data_type == "UINT16":
        return words[0] & 0xFFFF
    if data_type == "INT16":
        raw = words[0] & 0xFFFF
        return raw - 0x10000 if raw & 0x8000 else raw
    if data_type in ("UINT32", "INT32"):
        raw = ((words[0] & 0xFFFF) << 16) | (words[1] & 0xFFFF)
        if data_type == "INT32" and raw & 0x80000000:
            raw -= 0x100000000
        return raw
    if data_type == "VERSION":
        raw = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in words[:2])
        return ".".join(str(b) for b in raw)
    if data_type == "STRING":
        raw = b"".join((w & 0xFFFF).to_bytes(2, "big") for w in words)
        return raw.decode("utf-8", errors="ignore").replace("\x00", "").strip()
    raise ValueError(f"unknown data type {data_type}")


def encode(data_type: str, value: Any, count: int = 1) -> list[int]:
    """Inverse of decode; used by the simulator and tests."""
    if data_type in ("UINT16", "INT16"):
        return [int(value) & 0xFFFF]
    if data_type in ("UINT32", "INT32"):
        raw = int(value) & 0xFFFFFFFF
        return [raw >> 16, raw & 0xFFFF]
    if data_type == "VERSION":
        parts = [int(x) for x in str(value).split(".")][:4]
        parts += [0] * (4 - len(parts))
        return [(parts[0] << 8) | parts[1], (parts[2] << 8) | parts[3]]
    if data_type == "STRING":
        raw = str(value).encode().ljust(count * 2, b"\x00")[: count * 2]
        return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]
    raise ValueError(f"unknown data type {data_type}")


def extract(blocks: dict[tuple[str, int], list[int]], registers: list[Register] | None = None) -> dict[str, Any]:
    """Pull every register out of the blocks that were read successfully.

    `blocks` maps (kind, start_address) to the words read from that block.
    Registers whose block failed are simply absent from the result.
    """
    values: dict[str, Any] = {}
    for reg in REGISTERS if registers is None else registers:
        for (kind, start), words in blocks.items():
            offset = reg.address - start
            if kind == reg.kind and 0 <= offset and offset + reg.count <= len(words):
                value = decode(reg.data_type, words[offset : offset + reg.count])
                if reg.gain != 1:
                    value = value / reg.gain
                values[reg.key] = value
                break
    return values


def derive(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn raw register values into the snapshot the dashboard shows."""

    def num(key: str) -> float | None:
        v = raw.get(key)
        return v if isinstance(v, (int, float)) else None

    pv_parts = [v for v in (num("pv_power_pcs"), num("pv_power_third_party")) if v is not None]
    battery = num("battery_power")
    grid = num("grid_power")
    status = raw.get("battery_status")
    mode = raw.get("operating_mode")

    return {
        "solar_w": sum(pv_parts) if pv_parts else None,
        "home_w": num("load_power"),
        "battery_w": battery,  # + discharging, - charging
        "grid_w": grid,  # + import, - export
        "ac_output_w": num("ac_output_power"),
        "soc": num("battery_soc"),
        "battery_status": BATTERY_STATUS.get(status, None if status is None else f"unknown ({status})"),
        "operating_mode": OPERATING_MODES.get(mode, None if mode is None else f"unknown ({mode})"),
        "charging_limit_soc": num("charging_limit_soc"),
        "discharge_limit_soc": num("discharge_limit_soc"),
        "backup_reserve_soc": num("backup_reserve_soc"),
        "max_charge_w": num("max_charge_power"),
        "max_discharge_w": num("max_discharge_power"),
        "rated_kwh": num("rated_energy"),
        "solar_total_kwh": num("pv_total_generation"),
        "charged_total_kwh": num("cumulative_charge_energy"),
        "discharged_total_kwh": num("cumulative_discharge_energy"),
        "model": raw.get("device_model") or None,
        "serial": raw.get("device_sn") or None,
        "firmware": raw.get("device_sw_version") or None,
    }


# =============================================================================
# Anker SOLIX Smart Meter Gen 2
# From the official integration's config/42bcf12f...yaml. All input registers.
# "Primary" is the main CT clamp on the grid connection; "secondary" is the
# optional second CT. Power is positive when importing from the grid
# (assumed from the meter's conventions; not yet checked on real hardware).
# =============================================================================

METER_REGISTERS: list[Register] = [
    Register("meter_model", 10620, "STRING", 10),
    Register("meter_type", 10630, "UINT16"),
    Register("phase_1_voltage", 10632, "UINT16", gain=10),
    Register("phase_2_voltage", 10633, "UINT16", gain=10),
    Register("phase_3_voltage", 10634, "UINT16", gain=10),
    Register("phase_1_current", 10635, "INT16", gain=100),
    Register("phase_2_current", 10636, "INT16", gain=100),
    Register("phase_3_current", 10637, "INT16", gain=100),
    Register("phase_1_power", 10638, "INT32", 2),
    Register("phase_2_power", 10640, "INT32", 2),
    Register("phase_3_power", 10642, "INT32", 2),
    Register("total_power", 10644, "INT32", 2),
    Register("total_power_factor", 10648, "INT16", gain=1000),
    Register("total_import_energy", 10656, "UINT32", 2, gain=10),  # kWh
    Register("total_export_energy", 10664, "UINT32", 2, gain=10),  # kWh
    Register("secondary_total_power", 10675, "INT32", 2),
    Register("secondary_import_energy", 10686, "UINT32", 2, gain=10),
    Register("secondary_export_energy", 10694, "UINT32", 2, gain=10),
    Register("meter_sw_version", 10696, "VERSION", 2),
    Register("meter_sn", 10702, "STRING", 10),
    # Product number. Not shown, but Home Assistant reads it to recognise the
    # device, so the relay (app/relay.py) needs it.
    Register("meter_pn", 32768, "STRING", 5),
]

METER_BLOCKS: list[tuple[str, int, int]] = [
    (INPUT, 10620, 10647),
    (INPUT, 10648, 10695),
    (INPUT, 10696, 10712),
    (INPUT, 32768, 32772),
]

METER_TYPES = {1: "Single phase", 2: "Three phase"}


def derive_meter(raw: dict[str, Any]) -> dict[str, Any]:
    """Turn raw Smart Meter registers into the snapshot the dashboard shows."""

    def num(key: str) -> float | None:
        v = raw.get(key)
        return v if isinstance(v, (int, float)) else None

    meter_type = raw.get("meter_type")
    three_phase = meter_type == 2
    phases = []
    for n in (1, 2, 3) if three_phase else (1,):
        phases.append(
            {
                "phase": n,
                "voltage": num(f"phase_{n}_voltage"),
                "current": num(f"phase_{n}_current"),
                "power_w": num(f"phase_{n}_power"),
            }
        )
    return {
        "grid_w": num("total_power"),  # + import, - export
        "power_factor": num("total_power_factor"),
        "phases": phases,
        "import_total_kwh": num("total_import_energy"),
        "export_total_kwh": num("total_export_energy"),
        "secondary_w": num("secondary_total_power"),
        "secondary_import_total_kwh": num("secondary_import_energy"),
        "secondary_export_total_kwh": num("secondary_export_energy"),
        "meter_type": METER_TYPES.get(meter_type, None if meter_type is None else f"unknown ({meter_type})"),
        "model": raw.get("meter_model") or None,
        "serial": raw.get("meter_sn") or None,
        "firmware": raw.get("meter_sw_version") or None,
    }


@dataclass(frozen=True)
class Profile:
    """What to read from one kind of device and how to present it."""

    name: str
    registers: list[Register]
    blocks: list[tuple[str, int, int]]
    derive: Any  # Callable[[dict], dict]


BATTERY = Profile("battery", REGISTERS, READ_BLOCKS, derive)
METER = Profile("meter", METER_REGISTERS, METER_BLOCKS, derive_meter)
