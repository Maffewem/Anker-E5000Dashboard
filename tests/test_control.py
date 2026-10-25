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
    assert store["state"] == {"saved_mode": 6, "in_control": True, "saved_limits": {}, "override": None, "full_at": None,
                              "room_ok": None, "room_declined": None}
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
        assert client.post("/api/control/override", json={"action": "hold"}).status_code == 409  # no battery connected
        assert client.post("/api/control/override", json={"action": "off"}).json()["override"] is None


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


def _charging(collector=None):
    c = collector or FakeCollector(mode=6, soc=50)
    c.raw.update({"charging_limit_soc": 100, "discharge_limit_soc": 10})
    return c, {}


def test_charge_narrows_the_batterys_own_limit_then_puts_it_back(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging()
    ctl = make(c, {"enabled": True, "grid_charge": True, "charge_target_soc": 80}, store)
    asyncio.run(ctl.tick(IN))
    assert ("charging_limit_soc", 80) in c.writes
    assert store["state"]["saved_limits"] == {"charging_limit_soc": 100}
    n = len(c.writes)
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert [w for w in c.writes[n:] if w[0] == "charging_limit_soc"] == []  # said once, not every tick
    asyncio.run(ctl.tick(OUT))
    assert c.writes[-1] == ("charging_limit_soc", 100)
    assert store["state"]["saved_limits"] == {}


def test_a_stricter_limit_of_your_own_is_never_widened(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging()
    c.raw["charging_limit_soc"] = 70
    ctl = make(c, {"enabled": True, "grid_charge": True, "charge_target_soc": 90}, store)
    asyncio.run(ctl.tick(IN))
    assert ("charging_limit_soc", 70) in c.writes and ("charging_limit_soc", 90) not in c.writes


def test_limits_are_restored_after_a_restart(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, _ = _charging(FakeCollector(mode=3))  # left in third-party mode by a crash
    store = {"settings": {"enabled": False},
             "state": {"saved_mode": 6, "in_control": True, "saved_limits": {"charging_limit_soc": 95}}}
    asyncio.run(make(c, {}, store).tick(IN))
    assert ("operating_mode", 6) in c.writes and c.writes[-1] == ("charging_limit_soc", 95)


def test_limits_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    monkeypatch.setenv("CONTROL_LIMITS", "0")
    c, store = _charging()
    asyncio.run(make(c, {"enabled": True, "grid_charge": True}, store).tick(IN))
    assert not [w for w in c.writes if w[0].endswith("_limit_soc")]


def test_quick_override_works_with_control_off_and_ends_by_itself(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging(FakeCollector(mode=6, soc=50))
    ctl = make(c, {"enabled": False}, store)
    asyncio.run(ctl.set_override("charge", 30, 2000, 70, now=OUT))
    # The battery won't take a charge limit under 80%, so the cap stops there.
    assert ("battery_power_setpoint", -2000) in c.writes and ("charging_limit_soc", 80) in c.writes
    assert ctl.status()["override"]["action"] == "charge" and store["state"]["override"]
    asyncio.run(ctl.tick(OUT + timedelta(minutes=31)))
    assert c.writes[-1] == ("charging_limit_soc", 100) and ("operating_mode", 6) in c.writes
    assert ctl.override is None and not ctl.in_control


def test_quick_override_hold_and_cancel(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector(mode=0)
    ctl = make(c, {"enabled": False}, {})
    asyncio.run(ctl.set_override("hold", 60, now=OUT))
    assert ctl.action == HOLD and ctl.in_control
    asyncio.run(ctl.clear_override())
    assert ctl.override is None and c.writes[-1] == ("operating_mode", 0)
    with pytest.raises(ValueError):
        asyncio.run(ctl.set_override("charge", 2))
    with pytest.raises(ValueError):
        asyncio.run(ctl.set_override("explode", 30))


def test_quick_override_beats_a_schedule_window(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector(mode=0, soc=50)
    ctl = make(c, {"enabled": True, "grid_charge": True}, {})
    asyncio.run(ctl.set_override("hold", 60, now=IN))  # inside the cheap window that would charge
    assert ctl.action == HOLD


def test_default_charge_target_is_80():
    assert ControlSettings().charge_target_soc == 80


def test_discharge_cap_stays_inside_what_the_battery_accepts(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging(FakeCollector(mode=6, soc=80))
    ctl = make(c, {"enabled": False}, store)
    asyncio.run(ctl.set_override("discharge", 30, 2000, 40, now=OUT))
    assert ("discharge_limit_soc", 20) in c.writes  # not 40: the battery's discharge limit tops out at 20%


def test_set_battery_limits(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging()
    ctl = make(c, {"enabled": False}, store)
    asyncio.run(ctl.set_limits({"charging_limit_soc": 85, "discharge_limit_soc": 10}))
    assert c.writes == [("charging_limit_soc", 85)]  # discharge was already 10
    assert ctl.status()["limits"] == {"charging_limit_soc": 85, "discharge_limit_soc": 10}
    for bad in ({"charging_limit_soc": 70}, {"discharge_limit_soc": 30}, {"backup_reserve_soc": 20}):
        with pytest.raises(ValueError):
            asyncio.run(ctl.set_limits(bad))


def test_set_battery_limits_dry_run_writes_nothing(monkeypatch):
    monkeypatch.delenv("CONTROL_LIVE", raising=False)
    c, store = _charging()
    asyncio.run(make(c, {"enabled": False}, store).set_limits({"charging_limit_soc": 80}))
    assert c.writes == [] and c.raw["charging_limit_soc"] == 100


def test_new_limit_while_control_is_capping_is_put_back_afterwards(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = _charging()
    ctl = make(c, {"enabled": True, "grid_charge": True, "charge_target_soc": 90}, store)
    asyncio.run(ctl.tick(IN))
    assert ("charging_limit_soc", 90) in c.writes
    asyncio.run(ctl.set_limits({"charging_limit_soc": 85}))
    assert store["state"]["saved_limits"] == {"charging_limit_soc": 85}
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert c.writes[-1] == ("charging_limit_soc", 85)  # stricter than the cap, so it applies now
    asyncio.run(ctl.tick(OUT))
    assert c.writes[-1] == ("charging_limit_soc", 85) and store["state"]["saved_limits"] == {}


def test_happy_hour_charges_at_full_power_and_beats_schedules():
    from app.control import Schedule, happy_windows
    tz = ZoneInfo("Europe/London")
    hh = ({"date": "2026-10-10", "start": "11:00", "end": "12:00"},)
    sch = Schedule("discharge", "10:00", "13:00", (0, 1, 2, 3, 4, 5, 6), 1500, 20, True)
    on = ControlSettings(enabled=True, charge_target_soc=90, happy_hours=hh, schedules=(sch,)).validate()
    from app.control import schedule_windows
    inside = datetime(2026, 10, 10, 10, 30, tzinfo=UTC)  # 11:30 BST
    ws = schedule_windows(on.schedules, tz, inside) + happy_windows(on.happy_hours, tz, inside)
    action, setpoint, reason, w = decide(on, ws, {"soc": 50, "max_charge_w": 2500}, inside)
    assert (action, setpoint) == (CHARGE, -2500) and w.happy and "Happy Hour" in reason
    assert decide(on, ws, {"soc": 95}, inside)[:2] == (HOLD, 0)  # full: still no discharge
    after = datetime(2026, 10, 10, 11, 30, tzinfo=UTC)
    assert decide(on, ws, {"soc": 50}, after)[0] == "discharge"  # schedule resumes
    off = ControlSettings(enabled=True, charge_happy=False, happy_hours=hh, schedules=(sch,))
    assert decide(off, ws, {"soc": 50}, inside)[0] == "discharge"


def test_happy_hour_validation():
    with pytest.raises(ValueError):
        ControlSettings(happy_hours=({"date": "nope", "start": "11:00", "end": "12:00"},)).validate()


def test_discharge_schedule_stops_at_the_battery_reserve():
    from datetime import datetime, timezone
    sch = Schedule(DISCHARGE, "00:00", "23:59", tuple(range(7)), 1000, 5)
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    win = Window(now - timedelta(hours=1), now + timedelta(hours=1), "test", "schedule", sch)
    st = ControlSettings(enabled=True)
    snap = {"soc": 12, "max_discharge_w": 2400, "reserve_soc": 15}
    assert decide(st, [win], snap, now)[0] == HOLD
    assert decide(st, [win], {**snap, "soc": 30}, now)[0] == DISCHARGE


def test_auto_balance_charges_to_100_in_a_cheap_window():
    plain = ControlSettings(enabled=True, hold_cheap=False, grid_charge=False, charge_target_soc=80)
    assert decide(plain, [NIGHT], {"soc": 85}, IN)[0] == APP
    action, setpoint, reason, _ = decide(plain, [NIGHT], {"soc": 85, "max_charge_w": 2000}, IN, balance=True)
    assert (action, setpoint) == (CHARGE, -1500) and "100%" in reason
    assert decide(plain, [NIGHT], {"soc": 100}, IN, balance=True)[:2] == (HOLD, 0)
    assert decide(plain, [NIGHT], {"soc": 85}, OUT, balance=True)[0] == APP  # only inside a window


def test_balance_due_follows_last_full(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    monkeypatch.setenv("CONTROL_LIMITS", "1")
    now = datetime(2026, 10, 20, tzinfo=UTC)
    store = {"settings": {"enabled": True, "auto_balance": True, "balance_days": 28}, "state": {}}
    last = (now - timedelta(days=10)).timestamp()
    c = Controller(None, lambda: store, lambda st: None, lambda n: [], last_full=lambda: last)
    assert not c.balance_due(now)
    c.full_at = (now - timedelta(days=40)).timestamp()
    c._history_full = lambda: None
    assert c.balance_due(now)
    c.settings = ControlSettings(enabled=True)
    assert not c.balance_due(now)  # off


def test_active_charge_is_the_charging_window_in_progress():
    from datetime import timedelta
    from app.control import ControlSettings, Schedule, Window, active_charge, planned_charges
    now = datetime(2026, 10, 21, 0, 39, tzinfo=timezone.utc)
    sch = Schedule(CHARGE, "00:30", "05:30")
    on = ControlSettings(enabled=True, schedules=(sch,))
    wins = [Window(now - timedelta(minutes=9), now + timedelta(hours=4, minutes=51), "mine", "schedule", sch),
            Window(now + timedelta(hours=23, minutes=51), now + timedelta(hours=28, minutes=51), "mine", "schedule", sch)]
    assert active_charge(on, wins, now) == (wins[0].end, "mine")
    assert planned_charges(on, wins, now) == [(wins[1].start, "mine")]
    assert active_charge(ControlSettings(enabled=False, schedules=(sch,)), wins, now) is None


def test_active_target_is_where_the_charge_in_progress_stops():
    from app.control import ControlSettings, Schedule, Window, active_target
    now = datetime(2026, 10, 21, 0, 39, tzinfo=timezone.utc)
    sch = Schedule(CHARGE, "00:30", "05:30", target_soc=80)
    on = ControlSettings(enabled=True, schedules=(sch,))
    win = Window(now - timedelta(minutes=9), now + timedelta(hours=4), "mine", "schedule", sch)
    assert active_target(on, [win], now) == 80
    assert active_target(on, [win], now, balance=True) == 100
    assert active_target(on, [], now) is None


def _hh(start, end, day="2026-10-11"):
    from app.control import happy_windows
    return happy_windows(({"date": day, "start": start, "end": end},), UTC, IN)


def test_happy_headroom_lowers_the_overnight_target():
    from app.control import happy_headroom, lowered
    ws = [NIGHT, *_hh("11:00", "13:00")]  # two hours tomorrow
    snap = {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 2500, "reserve_soc": 10}
    on = ControlSettings(enabled=True, grid_charge=True, charge_target_soc=90)
    ok = ws[1].start.isoformat()
    room = happy_headroom(on, ws, snap, IN, accepted=ok)
    assert not happy_headroom(on, ws, snap, IN)["ok"] and room["ok"]
    assert room["kwh"] == 4.8 and round(room["pct"]) == 95  # 2 h x 2.5 kW x 0.95 of 5 kWh
    assert lowered(90, room) == 30  # floored, not negative
    big = {**snap, "rated_kwh": 20.0}
    pace = {"room_ok": ok}
    action, setpoint, reason, _ = decide(on, ws, {**big, "soc": 50}, IN, pace=pace)
    assert (action, setpoint) == (CHARGE, -1500) and "to 66%" in reason and "leaving room" in reason
    assert decide(on, ws, {**big, "soc": 70}, IN, pace=pace)[:2] == (HOLD, 0)  # already at the lowered target
    assert "to 90%" in decide(on, ws, {**big, "soc": 50}, IN)[2]  # not lowered until you say yes
    assert happy_headroom(ControlSettings(charge_happy=False), ws, snap, IN) is None
    assert happy_headroom(on, [NIGHT, *_hh("11:00", "13:00", "2026-10-20")], snap, IN) is None  # too far off


def test_headroom_skips_quick_override_and_active_happy_hour():
    ws = [NIGHT, *_hh("11:00", "12:00")]
    snap = {"soc": 50, "rated_kwh": 5.0, "max_charge_w": 2500}
    on = ControlSettings(enabled=True, grid_charge=True, charge_target_soc=90)
    inside = datetime(2026, 10, 11, 11, 30, tzinfo=UTC)
    action, setpoint, reason, w = decide(on, ws, snap, inside)
    assert (action, setpoint) == (CHARGE, -2500) and w.happy and "leaving room" not in reason


def test_balance_waits_for_the_happy_hour():
    ws = [NIGHT, *_hh("11:00", "12:00")]
    snap = {"soc": 50, "rated_kwh": 20.0, "max_charge_w": 2500}
    on = ControlSettings(enabled=True, grid_charge=True, charge_target_soc=90)
    assert "to 100%" not in decide(on, ws, snap, IN, balance=True)[2]
    inside = datetime(2026, 10, 11, 11, 30, tzinfo=UTC)
    assert "to 100%" in decide(on, ws, snap, inside, balance=True)[2]


def test_happy_hour_energy_is_priced_free(tmp_path):
    from datetime import date
    from app.control import happy_slots
    from app.tariff import FreeSlots, Tariff, daily_costs
    tz = ZoneInfo("Europe/London")
    free = happy_slots(({"date": "2026-10-11", "start": "11:00", "end": "12:00"},), tz)
    assert free == {("2026-10-11", 22), ("2026-10-11", 23)}
    slot = {"day": "2026-10-11", "slot": 22, "grid_charge_wh": 1000.0, "solar_charge_wh": 0.0, "discharge_wh": 0.0,
            "import_wh": 1500.0}
    paid = daily_costs(Tariff(peak_rate=30, offpeak_rate=30), [slot], date(2026, 10, 11), 1)["days"][0]
    assert paid["grid_charge_cost"] == 0.3
    got = daily_costs(Tariff(peak_rate=30, offpeak_rate=30), [slot], date(2026, 10, 11), 1, FreeSlots(None, free))["days"][0]
    assert got["grid_charge_cost"] == 0 and got["grid_charge_kwh"] == 1.0
    storage = Storage(tmp_path / "s.db", tz)
    storage.set_free_slots(free, "2026-10-01")
    storage.set_free_slots(set(), "2026-10-12")  # later days cleared, this one kept as history
    assert storage.free_slots() == free
    storage.set_free_slots(set(), "2026-10-11")
    assert storage.free_slots() == set()


def test_controller_reports_and_logs_the_happy_hour_room(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c = FakeCollector(mode=6, soc=40)
    c.snapshot = {"soc": 40, "rated_kwh": 20.0, "max_charge_w": 2500}
    store = {"settings": {"enabled": True, "grid_charge": True, "charge_target_soc": 90}}
    ctl = Controller(c, load=lambda: dict(store), save_state=lambda s: None,
                     windows=lambda now: [NIGHT, *_hh("11:00", "13:00")])
    events = []
    ctl._event = lambda *a, **k: events.append(a[0])
    asyncio.run(ctl.tick(IN))
    assert ctl.room is None and ctl.suggestion["to"] == 66 and ctl.status()["happy_suggestion"]["from"] == 90
    ctl.answer_room(ctl.suggestion["start"], True)
    assert ctl.suggestion is None and ctl.room_ok
    asyncio.run(ctl.tick(IN))
    assert ctl.room["target"] == 66 and ctl.room["kwh"] == 4.8
    assert ctl.status()["happy_room"]["target"] == 66
    assert any("Leaving room" in e and "66%" in e for e in events)
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert sum("Leaving room" in e for e in events) == 1  # logged once


# ---------- paced charging ----------

from app.control import pace_charge  # noqa: E402

PACE_WINDOW = Window(datetime(2026, 10, 10, 0, 30, tzinfo=UTC), datetime(2026, 10, 10, 5, 30, tzinfo=UTC), "Cheapest rate")


def _paced(**kw):
    return ControlSettings(**{"enabled": True, "grid_charge": True, "charge_power_w": 2500, "charge_target_soc": 80,
                              "pace_charge": True, **kw})


def test_pace_off_by_default_charges_flat_out():
    assert ControlSettings().pace_charge is False
    snap = {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 2500}
    off = _paced(pace_charge=False)
    assert decide(off, [PACE_WINDOW], snap, IN)[:2] == (CHARGE, -2500)


def test_pace_spreads_the_charge_and_leaves_the_margin():
    snap = {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 2500}
    action, setpoint, reason, _ = decide(_paced(), [PACE_WINDOW], snap, IN, pace={})
    assert action == CHARGE
    # 60% of 5 kWh (3.16 kWh from the grid) over 3.75 h (01:00 to 04:45 with the default 45 min spare): ~850 W, so 900
    assert -setpoint == 900 and "paced" in reason
    plan = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90)  # an explicit 90 min margin
    assert plan["deadline"] == datetime(2026, 10, 10, 4, 0, tzinfo=UTC) and plan["finish"] <= plan["deadline"]


def test_pace_never_exceeds_the_cap_and_falls_back_to_full_power_when_behind():
    late = datetime(2026, 10, 10, 3, 30, tzinfo=UTC)  # 30 min to the 04:00 deadline, a long way to go
    assert pace_charge(late, PACE_WINDOW, 20, 80, 5.0, 2500, 90)["kind"] == "full"
    assert decide(_paced(), [PACE_WINDOW], {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 1000}, late, pace={})[:2] == (CHARGE, -1000)
    assert pace_charge(IN, PACE_WINDOW, 70, 80, 5.0, 2500, 90)["power"] >= 300  # floor


def test_pace_speeds_up_when_the_battery_charges_slower_than_commanded():
    normal = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90)["power"]
    slow = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90, delivery=0.5)["power"]
    assert slow > normal


def test_pace_keeps_the_current_setpoint_when_close():
    assert pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90, current_w=1200)["power"] == 1200


def test_pace_cheapest_half_hours_first():
    prices = tuple((PACE_WINDOW.start + timedelta(minutes=30 * i), 5.0 if i in (3, 4) else 10.0) for i in range(10))
    w = Window(PACE_WINDOW.start, PACE_WINDOW.end, "Agile", prices=prices)
    snap = {"soc": 60, "rated_kwh": 5.0, "max_charge_w": 2500}
    # needs ~1 kWh: one cheap half hour at 2.5 kW (02:00); 01:00 is a dear slot, so wait
    action, setpoint, reason, _ = decide(_paced(), [w], snap, IN, pace={})
    assert (action, setpoint) == (HOLD, 0) and "cheaper" in reason
    cheap_now = datetime(2026, 10, 10, 2, 10, tzinfo=UTC)
    assert decide(_paced(), [w], snap, cheap_now, pace={})[:2] == (CHARGE, -2500)


def test_pace_leaves_happy_hours_and_balance_alone():
    happy = Window(IN - timedelta(minutes=10), IN + timedelta(hours=2), "Happy Hour", "octopus", happy=True)
    snap = {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 2400}
    assert decide(_paced(charge_happy=True), [happy], snap, IN, pace={})[:2] == (CHARGE, -2400)
    # balance aims for 100%, so the same window needs more power than for 80%
    p80 = -decide(_paced(), [PACE_WINDOW], {**snap, "soc": 50}, IN, pace={})[1]
    p100 = -decide(_paced(), [PACE_WINDOW], {**snap, "soc": 50}, IN, balance=True, pace={})[1]
    assert p100 > p80


def test_pace_settings_validate():
    with pytest.raises(ValueError):
        ControlSettings(pace_margin_min=5).validate()
    assert ControlSettings(pace_charge=True, pace_margin_min=60).validate().pace_margin_min == 60


def test_controller_paces_logs_once_and_respects_live_guard(monkeypatch):
    c = FakeCollector(mode=6, soc=20)
    c.snapshot = {"soc": 20, "rated_kwh": 5.0, "max_charge_w": 2500}
    ctl = Controller(c, load=lambda: {"settings": {"enabled": True, "grid_charge": True, "pace_charge": True}, "state": {}},
                     save_state=lambda s: None, windows=lambda now: [PACE_WINDOW])
    monkeypatch.delenv("CONTROL_LIVE", raising=False)
    asyncio.run(ctl.tick(IN))
    assert c.writes == [] and ctl.pace["kind"] == "even" and ctl.pace["power_w"] == 900  # dry run: nothing written
    notes = [e["text"] for e in ctl.log if e["text"].startswith("Pacing the charge")]
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert [e["text"] for e in ctl.log if e["text"].startswith("Pacing the charge")] == notes and len(notes) == 1
    # behind schedule later: back to full power, and it says so
    asyncio.run(ctl.tick(datetime(2026, 10, 10, 4, 0, tzinfo=UTC)))
    assert ctl.pace["kind"] == "full" and any("behind schedule" in e["text"] for e in ctl.log)
    assert ctl.status()["pace"]["kind"] == "full"


def test_pace_holds_a_recent_setpoint_unless_well_off_pace():
    need = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90)["power"]  # 1100
    kept = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90, current_w=need - 200, settled=True)
    assert kept["power"] == need - 200  # within 20%: leave it
    moved = pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90, current_w=need - 400, settled=True)
    assert moved["power"] == need  # too far short: correct it
    assert pace_charge(IN, PACE_WINDOW, 20, 80, 5.0, 2500, 90, current_w=need - 200)["power"] == need  # not settled


def _profile(kwh_per_slot, days=5):
    from app.usage_forecast import slot_profile
    rows = [{"day": f"2026-10-{d:02d}", "slot": s, "home_wh": kwh_per_slot * 1000, "solar_wh": 0, "minutes": 30}
            for d in range(1, days + 1) for s in range(48)]
    return slot_profile(rows, datetime(2026, 10, 10).date())


def test_usage_forecast_sums_the_half_hours_to_the_slot():
    from app.usage_forecast import predict_kwh
    prof = _profile(0.2)
    start = datetime(2026, 10, 10, 8, 30, tzinfo=UTC)
    assert round(predict_kwh(prof, start, start + timedelta(hours=2, minutes=30))[0], 2) == 1.0  # 5 half hours
    assert round(predict_kwh(prof, start, start + timedelta(minutes=15))[0], 2) == 0.1  # half a slot
    assert predict_kwh(_profile(0.2, days=2), start, start + timedelta(hours=1)) is None  # too little history


def test_expected_home_use_shrinks_the_happy_hour_room():
    from app.control import happy_headroom, lowered
    ws = [NIGHT, *_hh("11:00", "13:00")]
    snap = {"soc": 20, "rated_kwh": 10.0, "max_charge_w": 2500, "reserve_soc": 10}
    on = ControlSettings(enabled=True, grid_charge=True, charge_target_soc=90)
    plain = happy_headroom(on, ws, snap, IN)
    used = happy_headroom(on, ws, snap, IN, lambda a, b: 2.0 if (a, b) == (NIGHT.end, ws[1].start) else None)
    assert used["slot_kwh"] == 4.8 and used["use_kwh"] == 2.1  # 2 kWh drawn from 05:30 to 11:00, as stored energy
    assert lowered(90, used) > lowered(90, plain)  # charges higher overnight
    heavy = happy_headroom(on, ws, snap, IN, lambda a, b: 9.0)
    assert heavy["kwh"] == 0 and lowered(90, heavy) == 90  # house would use more than the slot can take: no room kept


def test_declining_the_suggestion_keeps_the_target_and_stops_asking():
    c = FakeCollector(mode=6, soc=40)
    c.snapshot = {"soc": 40, "rated_kwh": 20.0, "max_charge_w": 2500}
    store = {"settings": {"enabled": True, "grid_charge": True, "charge_target_soc": 90}}
    ctl = Controller(c, load=lambda: dict(store), save_state=lambda s: None,
                     windows=lambda now: [NIGHT, *_hh("11:00", "13:00")])
    asyncio.run(ctl.tick(IN))
    ctl.answer_room(ctl.suggestion["start"], False)
    asyncio.run(ctl.tick(IN + timedelta(seconds=15)))
    assert ctl.suggestion is None and ctl.room is None
    with pytest.raises(ValueError):
        ctl.answer_room("2026-01-01T00:00:00+00:00", True)

def test_failsafe_hands_back_after_lost_contact(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(mode=6), {}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl.tick(IN))
    assert ctl.in_control
    c.connected = False
    asyncio.run(ctl.tick(IN + timedelta(seconds=60)))
    assert ctl.in_control  # a short blip is ignored
    asyncio.run(ctl.tick(IN + timedelta(seconds=300)))
    assert c.writes[-1] == ("operating_mode", 6)
    assert not ctl.in_control and store["state"]["in_control"] is False


def test_failsafe_retries_until_the_write_lands(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(mode=6), {}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl.tick(IN))
    c.connected = False
    real = c.write

    async def broken(key, value):
        raise ConnectionError("down")
    c.write = broken
    asyncio.run(ctl.tick(IN + timedelta(seconds=60)))
    asyncio.run(ctl.tick(IN + timedelta(seconds=300)))
    assert ctl.in_control and "Failsafe" in ctl.last_error
    c.write = real
    asyncio.run(ctl.tick(IN + timedelta(seconds=330)))
    assert not ctl.in_control


def test_setpoint_is_clamped_to_the_battery_limits(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(), {}
    c.snapshot = {"soc": 50, "max_charge_w": 2000, "max_discharge_w": 1000}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl._write("battery_power_setpoint", -4000))
    asyncio.run(ctl._write("battery_power_setpoint", 4000))
    assert [w[1] for w in c.writes] == [-2000, 1000]


def test_emergency_stop(monkeypatch):
    monkeypatch.setenv("CONTROL_LIVE", "1")
    c, store = FakeCollector(mode=6), {}
    ctl = make(c, {"enabled": True}, store)
    asyncio.run(ctl.tick(IN))
    asyncio.run(ctl.stop())
    assert c.writes[-1] == ("operating_mode", 6)
    assert not ctl.in_control and not ctl.settings.enabled



def test_usage_forecast_leans_on_the_same_weekday_then_weekend():
    from app.usage_forecast import predict_kwh, slot_profile
    # 2026-10-25 is a Sunday: past Sundays use 1 kWh a slot, other weekend days 0.5, weekdays 0.1.
    kwh = {5: 0.5, 6: 1.0}
    rows = [{"day": f"2026-10-{d:02d}", "slot": 20, "home_wh": kwh.get(datetime(2026, 10, d).weekday(), 0.1) * 1000,
             "solar_wh": 0, "minutes": 30} for d in range(1, 25)]
    prof = slot_profile(rows, datetime(2026, 10, 25).date())
    sunday = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
    got = predict_kwh(prof, sunday, sunday + timedelta(minutes=30))[0]
    assert 0.7 < got < 1.0  # near the Sunday figure
    tuesday = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    assert predict_kwh(prof, tuesday, tuesday + timedelta(minutes=30))[0] < 0.3  # weekday leans on weekdays


def test_forecast_basis_counts_matching_days():
    from app.usage_forecast import basis
    rows = [{"day": f"2026-10-{d:02d}", "slot": 20, "home_wh": 100, "solar_wh": 0, "minutes": 30} for d in range(12, 25)]
    from app.usage_forecast import slot_profile
    prof = slot_profile(rows, datetime(2026, 10, 25).date())  # 12th is a Monday; 17th/18th and 24th are weekend days
    b = basis(prof, datetime(2026, 10, 25, 11, 0))  # a Sunday
    assert b == {"days": 13, "same_weekday": 1, "same_kind": 3}
