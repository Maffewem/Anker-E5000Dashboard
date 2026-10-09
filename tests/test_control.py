import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import control
from app.battery_care import care
from app.collector import Collector
from app.config import Connection, Settings
from app.control import APP, CHARGE, HOLD, ControlSettings, Controller, Window, cheap_windows, decide
from app.storage import Storage
from simulator.sim import STATIC, Battery, Registers, handle

UTC = timezone.utc
NIGHT = Window(datetime(2026, 10, 10, 0, 30, tzinfo=UTC), datetime(2026, 10, 10, 5, 30, tzinfo=UTC), "Cheapest rate")
IN = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)
OUT = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


def test_decide():
    on = ControlSettings(enabled=True)
    assert decide(ControlSettings(), [NIGHT], {"soc": 50}, IN)[0] == APP  # off
    assert decide(on, [NIGHT], {"soc": 50}, OUT)[0] == APP
    assert decide(on, [NIGHT], {"soc": 50}, IN)[:2] == (HOLD, 0)
    charge = ControlSettings(enabled=True, grid_charge=True, charge_power_w=3000, charge_target_soc=90)
    assert decide(charge, [NIGHT], {"soc": 50, "max_charge_w": 2500}, IN)[:2] == (CHARGE, -2500)
    assert decide(charge, [NIGHT], {"soc": 90}, IN)[:2] == (HOLD, 0)  # full enough: just hold


def test_cheap_windows_from_offpeak_hours_cross_midnight():
    tz = ZoneInfo("Europe/London")
    ws = cheap_windows(None, ("23:30", "05:30"), tz, datetime(2026, 1, 10, 1, 0, tzinfo=UTC))
    assert any(w.start <= datetime(2026, 1, 10, 1, 0, tzinfo=UTC) < w.end for w in ws)
    assert cheap_windows(None, None, tz, IN) == []


def test_cheap_windows_from_octopus():
    status = {"prices": [{}], "recommendations": [
        {"kind": "charge", "start": "2026-10-10T00:30:00Z", "end": "2026-10-10T05:30:00Z", "note": "Cheapest rate", "avg_p": 8.5},
        {"kind": "avoid", "start": "2026-10-10T16:00:00Z", "end": "2026-10-10T19:00:00Z", "note": "Peak", "avg_p": 40}],
        "dispatches": [{"start": "2026-10-10T20:00:00Z", "end": "2026-10-10T21:00:00Z"}]}
    ws = cheap_windows(status, None, UTC, IN)
    assert [(w.start.hour, w.end.hour) for w in ws] == [(0, 5), (20, 21)]


class FakeCollector:
    def __init__(self, mode=6, soc=50):
        self.connected = True
        self.raw = {"operating_mode": mode}
        self.snapshot = {"soc": soc}
        self.writes = []

    async def write(self, key, value):
        self.writes.append((key, value))
        if key == "operating_mode":
            self.raw["operating_mode"] = value


def make(collector, settings, store):
    store.setdefault("settings", settings)
    return Controller(collector, load=lambda: dict(store), save_state=lambda s: store.__setitem__("state", s),
                      windows=lambda now: [NIGHT])


def test_live_hold_then_hand_back(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(mode=6), {}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl.tick(IN))
    assert c.writes == [("operating_mode", 3), ("battery_power_setpoint", 0)]
    assert store["state"] == {"saved_mode": 6, "in_control": True}
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert len(c.writes) == 2  # nothing new to say
    asyncio.run(ctl.tick(OUT))
    assert c.writes[-1] == ("operating_mode", 6)  # back to Smart mode
    assert store["state"]["in_control"] is False


def test_dry_run_writes_nothing(monkeypatch):
    monkeypatch.delenv("CONTROL_LIVE", raising=False)
    c = FakeCollector()
    ctl = make(c, {"enabled": True}, {})
    asyncio.run(ctl.tick(IN))
    assert c.writes == []
    assert ctl.action == HOLD and not ctl.status()["live"]
    assert any("Dry run" in e["text"] for e in ctl.log)


def test_restart_mid_window_still_restores_the_old_mode(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector(mode=3)  # left in third-party mode by a crash
    store = {"settings": {"enabled": False}, "state": {"saved_mode": 1, "in_control": True}}
    asyncio.run(make(c, {}, store).tick(IN))
    assert c.writes == [("operating_mode", 1)]


def test_turning_off_hands_back(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(mode=0), {}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl.tick(IN))
    ctl.update_settings(ControlSettings(enabled=False))
    asyncio.run(ctl.tick(IN))
    assert c.writes[-1] == ("operating_mode", 0)


def test_mode_changed_in_app_is_respected(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector(mode=6)
    ctl = make(c, {"enabled": True}, {})
    asyncio.run(ctl.tick(IN))
    ctl.took_control_at -= 1000
    c.raw["operating_mode"] = 0  # user picked self-consumption in the app
    asyncio.run(ctl.tick(IN + timedelta(minutes=5)))
    assert ctl.action == APP and not ctl.in_control
    assert c.writes[-1] == ("battery_power_setpoint", 0)  # nothing written over the user's choice


def test_validate():
    with pytest.raises(ValueError):
        ControlSettings(charge_power_w=50).validate()
    with pytest.raises(ValueError):
        ControlSettings(charge_target_soc=10).validate()


def test_controls_the_simulator_over_modbus(monkeypatch):
    """Real Modbus writes against the bundled simulator."""
    monkeypatch.setenv("CONTROL_LIVE", "1")

    async def run():
        regs = Registers()
        regs.write(STATIC)
        battery = Battery(regs)
        regs.write(battery.step())
        server = await asyncio.start_server(lambda r, w: handle(regs, r, w), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        settings = Settings(5, 365, ":memory:", "unused.json", "UTC")
        collector = Collector(settings, Storage(":memory:", 365), Connection("127.0.0.1", port, 1))
        store = {"settings": {"enabled": True, "grid_charge": True, "charge_power_w": 1200}}
        ctl = Controller(collector, load=lambda: dict(store), save_state=lambda s: None, windows=lambda now: [NIGHT])
        try:
            await collector.poll_once()
            await ctl.tick(IN)
            regs.write(battery.step())
            await collector.poll_once()
            during = (collector.snapshot["operating_mode"], collector.snapshot["battery_w"])
            await ctl.tick(OUT)
            await collector.poll_once()
            after = collector.snapshot["operating_mode"]
        finally:
            collector.close()
            server.close()
        return during, after

    during, after = asyncio.run(run())
    assert during == ("Third-party control", -1200)
    assert after == "Smart"


def test_battery_care_tips():
    snap = {"rated_kwh": 5.0, "discharged_total_kwh": 400.0, "charging_limit_soc": 100, "discharge_limit_soc": 0}
    out = care(snap, {"days": 20, "full_share": 0.4, "empty_share": 0.0}, {"grid_charge": True, "charge_target_soc": 100})
    titles = [t["title"] for t in out["tips"]]
    assert out["cycles"] == 80.0
    assert "Often sitting full" in titles
    assert "Discharge limit 0%" in titles
    assert "Grid charging to 100%" in titles
