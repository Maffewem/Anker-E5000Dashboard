"""The event log: what gets recorded when readings change."""

from app.events import STATUS_HOLD, EventWatcher
from app.storage import Storage


def battery(status="standby", mode="Smart", soc=50, reserve=10):
    return {"battery_status": status, "operating_mode": mode, "soc": soc, "backup_reserve_soc": reserve,
            "charging_limit_soc": 100, "discharge_limit_soc": 5, "firmware": "1.2.3"}


def messages(s):
    return [e["message"] for e in reversed(s.events())]


def test_first_readings_are_a_baseline_then_changes_are_logged():
    s = Storage(":memory:", 0)
    w = EventWatcher(s, "battery")
    w.observe(battery(), 0)
    assert s.events() == []
    w.observe(battery(mode="Time of use", reserve=20.0), 5)
    assert messages(s) == ["Mode changed from Smart to Time of use", "Backup reserve changed from 10% to 20%"]
    e = s.events(kinds=["setting"])[0]
    assert (e["field"], e["old"], e["new"], e["source"], e["ts"]) == ("backup_reserve_soc", "10", "20", "device", 5)


def test_charging_is_logged_once_it_holds_with_its_start_time():
    s = Storage(":memory:", 0)
    w = EventWatcher(s, "battery")
    w.observe(battery(), 0)
    w.observe(battery("charging", soc=40), 10)  # a blip...
    w.observe(battery("standby"), 15)  # ...that didn't last
    w.observe(battery("charging", soc=41), 20)
    w.observe(battery("charging", soc=42), 20 + STATUS_HOLD)
    w.observe(battery("standby", soc=90), 1000)
    w.observe(battery("standby", soc=90), 1000 + STATUS_HOLD)
    assert messages(s) == ["Started charging at 41%", "Stopped charging at 90%"]
    assert [e["ts"] for e in reversed(s.events())] == [20, 1000]


def test_changes_while_stopped_are_logged_on_restart():
    s = Storage(":memory:", 0)
    EventWatcher(s, "battery").observe(battery(), 0)
    EventWatcher(s, "battery").observe(battery(mode="Custom"), 100)
    assert messages(s) == ["Mode changed from Smart to Custom"]


def test_missing_readings_are_not_changes_and_paging_works():
    s = Storage(":memory:", 0)
    w = EventWatcher(s, "battery")
    w.observe(battery(), 0)
    w.observe({"battery_status": None, "operating_mode": None}, 5)
    assert s.events() == []
    for n in range(5):
        s.record_event("control", f"action {n}", source="dashboard")
    newest = s.events(limit=2)
    assert [e["message"] for e in newest] == ["action 4", "action 3"]
    assert [e["message"] for e in s.events(limit=2, before=newest[-1]["id"])] == ["action 2", "action 1"]


def test_events_page_by_offset_and_count_by_kind(tmp_path):
    s = Storage(str(tmp_path / "t.db"), 30, "Europe/London")
    for i in range(1, 6):
        s.record_event("control", f"action {i}", ts=i)
    s.record_event("setting", "a setting", ts=6)
    assert [e["message"] for e in s.events(limit=2, offset=1, kinds=["control"])] == ["action 4", "action 3"]
    assert s.count_events() == 6 and s.count_events(["control"]) == 5
