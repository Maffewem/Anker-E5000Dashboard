import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

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


def test_live_actions_go_to_the_event_log(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    events = []

    class Store:
        def record_event(self, kind, message, **kw):
            events.append((kind, kw["field"], kw["old"], kw["new"], kw["source"]))

    c = FakeCollector(mode=6)
    c.storage = Store()
    ctl = make(c, {"enabled": True}, {})
    asyncio.run(ctl.tick(IN))
    asyncio.run(ctl.tick(OUT))
    assert events == [
        ("control", "operating_mode", "Smart", "Third-party control", "octopus"),
        ("control", "battery_power_setpoint", None, 0, "octopus"),
        ("control", "operating_mode", "Third-party control", "Smart", "dashboard"),
    ]


def test_control_settings_and_state_are_saved(tmp_path):
    from app.config import ConnectionStore

    store = ConnectionStore(str(tmp_path / "settings.json"))
    store.save_section("control", {"enabled": True})
    store.save_section("control_state", {"saved_mode": 6, "in_control": True})
    again = ConnectionStore(str(tmp_path / "settings.json"))
    assert again.load_section("control") == {"enabled": True}
    assert again.load_section("control_state")["saved_mode"] == 6


# ---------- your own schedules and the mode picker ----------

from app.control import DISCHARGE, Schedule, schedule_windows  # noqa: E402

LONDON = ZoneInfo("Europe/London")
SAT = datetime(2026, 10, 10, 1, 0, tzinfo=UTC)  # Saturday 02:00 in London


def sched(**kw):
    return Schedule(**kw).validate()


def test_schedule_windows_follow_days_and_cross_midnight():
    every = sched(action="charge", start="23:30", end="05:30")
    ws = schedule_windows([every], LONDON, SAT)
    assert any(w.start <= SAT < w.end for w in ws)  # started Friday 23:30
    weekdays = sched(action="charge", start="23:30", end="05:30", days=[0, 1, 2, 3])  # Fri start not included
    assert not any(w.start <= SAT < w.end for w in schedule_windows([weekdays], LONDON, SAT))
    off = Schedule(action="hold", start="00:00", end="06:00", enabled=False)
    assert schedule_windows([off], LONDON, SAT) == []


def test_schedules_charge_discharge_hold_and_beat_cheap_hours():
    on = ControlSettings(enabled=True, hold_cheap=True)
    dis = schedule_windows([sched(action="discharge", start="01:00", end="03:00", power_w=3000, target_soc=20)], LONDON, SAT)
    action, sp, reason, w = decide(on, dis + [NIGHT], {"soc": 60, "max_discharge_w": 2500}, SAT)
    assert (action, sp) == (DISCHARGE, 2500) and w.schedule  # schedule wins over the cheap window
    assert decide(on, dis, {"soc": 20}, SAT)[:2] == (HOLD, 0)  # floor reached
    chg = schedule_windows([sched(action="charge", start="01:00", end="03:00", power_w=800, target_soc=80)], LONDON, SAT)
    assert decide(on, chg, {"soc": 50}, SAT)[:2] == (CHARGE, -800)
    assert decide(on, chg, {"soc": 80}, SAT)[:2] == (HOLD, 0)
    # Cheap-hour options off: only schedules act.
    plain = ControlSettings(enabled=True, hold_cheap=False, grid_charge=False)
    assert decide(plain, [NIGHT], {"soc": 50}, IN)[0] == APP
    assert decide(plain, chg + [NIGHT], {"soc": 50}, SAT)[0] == CHARGE


def test_schedule_validation():
    for bad in ({"start": "25:00"}, {"start": "01:00", "end": "01:00"}, {"days": []}, {"action": "boost"},
                {"power_w": 50}, {"target_soc": 2}):
        with pytest.raises(ValueError):
            Schedule(**bad).validate()
    assert sched(action="hold", power_w=0).power_w == 0  # power doesn't matter for hold
    with pytest.raises(ValueError):
        ControlSettings(schedules=tuple(Schedule() for _ in range(21))).validate()


def test_settings_round_trip_with_schedules():
    s = ControlSettings(enabled=True, schedules=(sched(action="discharge", start="16:00", end="19:00", days=[0, 4]),))
    from dataclasses import asdict
    again = ControlSettings.from_dict(asdict(s))
    assert again == s


def test_set_mode_writes_logs_and_stands_back(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    events = []

    class Store:
        def record_event(self, kind, message, **kw):
            events.append((kw["field"], kw["old"], kw["new"], kw["source"]))

    c = FakeCollector(mode=6)
    c.storage = Store()
    ctl = make(c, {"enabled": True}, {})
    asyncio.run(ctl.tick(IN))
    assert ctl.in_control
    asyncio.run(ctl.set_mode(0))
    assert c.writes[-1] == ("operating_mode", 0) and not ctl.in_control
    assert events[-1] == ("operating_mode", "Third-party control", "Self-consumption", "dashboard")
    asyncio.run(ctl.tick(IN + timedelta(minutes=1)))
    assert c.writes[-1] == ("operating_mode", 0)  # stays out until the window ends
    with pytest.raises(ValueError):
        asyncio.run(ctl.set_mode(3))


def test_saving_settings_is_logged_even_in_dry_run(monkeypatch):
    monkeypatch.delenv("CONTROL_LIVE", raising=False)
    events = []

    class Store:
        def record_event(self, kind, message, **kw):
            events.append(message)

    c = FakeCollector()
    c.storage = Store()
    ctl = make(c, {}, {})
    ctl.update_settings(ControlSettings(enabled=True, schedules=(sched(action="hold", start="16:00", end="19:00"),)))
    assert events and "control on (dry run)" in events[0] and "Hold" in events[0]


def test_api_schedules_and_mode(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    for name in ("SOLARBANK_HOST", "METER_HOST", "OCTOPUS_API_KEY", "OCTOPUS_ACCOUNT", "CONTROL_LIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "solarbank.db"))
    from app.main import app

    with TestClient(app) as client:
        body = {"enabled": True, "hold_cheap": False, "schedules": [
            {"action": "discharge", "start": "16:00", "end": "19:00", "days": [0, 1, 2, 3, 4], "power_w": 2000, "target_soc": 20}]}
        r = client.post("/api/control", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["settings"]["schedules"][0]["action"] == "discharge"
        assert {m["value"] for m in r.json()["modes"]} >= {0, 1, 6} and 3 not in {m["value"] for m in r.json()["modes"]}
        bad = client.post("/api/control", json={"schedules": [{"start": "16:00", "end": "16:00"}]})
        assert bad.status_code == 422
        assert client.post("/api/control/mode", json={"mode": 0}).status_code == 409  # no battery connected
        saved = app.state.connection_store.load_section("control")
        assert saved["schedules"][0]["days"] == [0, 1, 2, 3, 4]


# An Intelligent Go top-up Octopus adds in the middle of the day.
SLOT = Window(datetime(2026, 10, 10, 12, 20, tzinfo=UTC), datetime(2026, 10, 10, 12, 40, tzinfo=UTC),
              "Intelligent Go smart-charge slot 13:20-13:40", dispatch=True)
IN_SLOT = datetime(2026, 10, 10, 12, 25, tzinfo=UTC)


def test_smart_charge_slot_charges_then_holds_without_any_cheap_hour_options():
    plain = ControlSettings(enabled=True, hold_cheap=False, grid_charge=False, charge_power_w=2000, charge_target_soc=90)
    action, setpoint, reason, w = decide(plain, [NIGHT, SLOT], {"soc": 40, "max_charge_w": 1200}, IN_SLOT)
    assert (action, setpoint, w) == (CHARGE, -1200, SLOT) and "13:20-13:40" in reason
    assert decide(plain, [NIGHT, SLOT], {"soc": 95}, IN_SLOT)[:2] == (HOLD, 0)  # full: still don't discharge
    assert decide(plain, [NIGHT, SLOT], {"soc": 40}, SLOT.end)[0] == APP  # back to normal straight after
    off = ControlSettings(enabled=True, hold_cheap=False, grid_charge=False, charge_dispatch=False)
    assert decide(off, [SLOT], {"soc": 40}, IN_SLOT)[0] == APP


def test_smart_charge_slot_beats_a_discharge_schedule():
    from app.control import Schedule, schedule_windows
    sch = Schedule("discharge", "13:00", "14:00", (0, 1, 2, 3, 4, 5, 6), 1500, 20, True)
    tz = ZoneInfo("Europe/London")
    on = ControlSettings(enabled=True, schedules=(sch,))
    ws = schedule_windows(on.schedules, tz, IN_SLOT) + [SLOT]
    assert decide(on, ws, {"soc": 60}, IN_SLOT)[0] == CHARGE
    assert decide(on, ws, {"soc": 60}, SLOT.end + timedelta(minutes=5))[0] == "discharge"


def test_dispatch_windows_keep_odd_times():
    tz = ZoneInfo("Europe/London")
    status = {"prices": [{}], "recommendations": [], "dispatches": [
        {"start": "2026-10-10T12:20:00Z", "end": "2026-10-10T12:40:00Z"}]}
    (w,) = cheap_windows(status, None, tz, IN_SLOT)
    assert w.dispatch and w.start.minute == 20 and w.end.minute == 40
    assert w.why == "Intelligent Go smart-charge slot 13:20-13:40"


def test_smart_charge_slot_is_logged_and_handed_back(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    events = []

    class Store:
        def record_event(self, kind, message, **kw):
            events.append((kind, message, kw["new"], kw["source"]))

    c = FakeCollector(mode=6, soc=40)
    c.storage = Store()
    ctl = Controller(c, load=lambda: {"settings": {"enabled": True, "hold_cheap": False}}, save_state=lambda s: None,
                     windows=lambda now: [SLOT])
    asyncio.run(ctl.tick(IN_SLOT))
    assert c.writes == [("operating_mode", 3), ("battery_power_setpoint", -1500)]
    asyncio.run(ctl.tick(SLOT.end))
    assert c.writes[-1] == ("operating_mode", 6)
    assert [e[3] for e in events] == ["octopus", "octopus", "dashboard"]  # took control, charged, handed back
    assert "13:20-13:40" in events[0][1]


def test_overlapping_schedules_are_rejected():
    from app.control import Schedule
    every = (0, 1, 2, 3, 4, 5, 6)
    night = Schedule("charge", "00:30", "05:30", every, 1500, 90, True)
    day = Schedule("charge", "13:00", "15:00", (0, 1, 2, 3, 4), 1500, 90, True)
    peak = Schedule("discharge", "16:00", "19:00", every, 1500, 20, True)
    ControlSettings(schedules=(night, day, peak)).validate()  # several windows a day is fine
    clash = Schedule("hold", "05:00", "06:00", every, 1500, 90, True)
    with pytest.raises(ValueError, match="overlap"):
        ControlSettings(schedules=(night, clash)).validate()
    ControlSettings(schedules=(night, Schedule("hold", "05:00", "06:00", every, 1500, 90, False))).validate()  # off: fine
    # Sunday 23:00 to 01:00 runs into Monday's 00:30 start.
    late = Schedule("hold", "23:00", "01:00", (6,), 1500, 90, True)
    with pytest.raises(ValueError, match="overlap"):
        ControlSettings(schedules=(night, late)).validate()
    ControlSettings(schedules=(Schedule("hold", "23:00", "00:30", (6,), 1500, 90, True), night)).validate()  # touching is fine
