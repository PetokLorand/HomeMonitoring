import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from acmonitor.controller import active_windows, upcoming_transitions, window_for
from acmonitor.drivers import DriverError
from acmonitor.store import Schedule

UTC = ZoneInfo("UTC")


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# 2026-11-23 is a Monday (weekday 0).
def sched(id=1, days="0123456", start="08:00", end="10:00", preset="cool24", enabled=True):
    return Schedule(id=id, name=f"s{id}", days=days, start=start, end=end, preset=preset, enabled=enabled)


def test_window_simple():
    s = sched(days="0")
    assert window_for(s, at(2026, 11, 23, 9)) is not None
    assert window_for(s, at(2026, 11, 23, 10)) is None  # end is exclusive
    assert window_for(s, at(2026, 11, 24, 9)) is None  # Tuesday


def test_window_crosses_midnight():
    s = sched(days="0", start="22:00", end="06:00")  # Monday night
    assert window_for(s, at(2026, 11, 23, 23))
    assert window_for(s, at(2026, 11, 24, 5, 59))  # Tuesday morning belongs to Monday's window
    assert not window_for(s, at(2026, 11, 24, 23))  # Tuesday night not scheduled
    assert not window_for(s, at(2026, 11, 23, 3))  # Monday early morning = Sunday's window


def test_disabled_and_upcoming():
    s1, s2 = sched(1), sched(2, enabled=False)
    assert [s.id for s, _ in active_windows([s1, s2], at(2026, 11, 23, 9))] == [1]
    up = upcoming_transitions([s1], at(2026, 11, 23, 9))
    assert up[0]["kind"] == "end" and up[1]["kind"] == "start"


def tick(ctl, *args):
    ctl.tick(at(*args))


def test_schedule_edges(ir_ctl, store):
    store.add_schedule("morning", "0123456", "08:00", "10:00", "cool24")
    sent = ir_ctl.driver.sent
    tick(ir_ctl, 2026, 11, 23, 7, 59)
    assert sent == []
    tick(ir_ctl, 2026, 11, 23, 8, 0)
    assert sent == [b"C24"]
    tick(ir_ctl, 2026, 11, 23, 9, 0)  # still inside: no resend
    assert sent == [b"C24"]
    tick(ir_ctl, 2026, 11, 23, 10, 0)
    assert sent == [b"C24", b"OFF"]
    tick(ir_ctl, 2026, 11, 23, 11, 0)
    assert len(sent) == 2


def test_manual_off_inside_window_is_respected(ir_ctl, store):
    store.add_schedule("m", "0123456", "08:00", "10:00", "cool24")
    tick(ir_ctl, 2026, 11, 23, 8, 0)
    ir_ctl.turn_off()
    tick(ir_ctl, 2026, 11, 23, 9, 0)  # no re-apply mid-window
    tick(ir_ctl, 2026, 11, 23, 10, 0)  # already off: no extra off
    assert ir_ctl.driver.sent == [b"C24", b"OFF"]


def test_overlap_latest_start_wins(ir_ctl, store):
    store.add_schedule("long", "0123456", "08:00", "12:00", "cool24")
    store.add_schedule("short", "0123456", "09:00", "10:00", "heat22")
    sent = ir_ctl.driver.sent
    for h in (8, 9, 10, 12):
        tick(ir_ctl, 2026, 11, 23, h, 0)
    # 08 cool, 09 heat (newer window), 10 back to cool, 12 off
    assert sent == [b"C24", b"H22", b"C24", b"OFF"]


def test_off_timer(ir_ctl):
    ir_ctl.apply("cool24", off_after_min=30)
    t = ir_ctl.store.kv_get("off_timer_at")
    assert t and abs(t - (time.time() + 1800)) < 5
    ir_ctl.tick(datetime.fromtimestamp(t - 10, UTC))
    assert ir_ctl.driver.sent == [b"C24"]
    ir_ctl.tick(datetime.fromtimestamp(t + 1, UTC))
    assert ir_ctl.driver.sent == [b"C24", b"OFF"]
    assert ir_ctl.store.kv_get("off_timer_at") is None


def test_ir_preset_without_code_fails_and_is_logged(ir_ctl, store):
    store.save_preset("dry", "Dry", None, mode="dry")
    with pytest.raises(DriverError):
        ir_ctl.apply("dry")
    assert store.events()[0]["ok"] is False


def test_ir_cannot_send_arbitrary_settings(ir_ctl):
    with pytest.raises(DriverError):
        ir_ctl.control({"power": True, "mode": "cool"})


def test_runtime(ir_ctl, store):
    t0 = time.time() - 7200
    store.add_event("manual", "cool24", False, True, ts=t0)
    store.add_event("manual", "off", True, True, ts=t0 + 1800)
    store.add_event("manual", "cool24", False, False, ts=t0 + 2000)  # failed: ignored
    store.add_event("manual", "cool24", False, True, ts=t0 + 3600)
    secs = ir_ctl.runtime(t0 - 100, t0 + 5400)
    assert secs == pytest.approx(1800 + 1800)


def test_live_control_and_remote_detection(live_ctl, store):
    drv = live_ctl.driver
    live_ctl.control({"power": True, "mode": "cool", "target_temp": 22})
    assert drv.applied == [{"power": True, "mode": "cool", "target_temp": 22, "fan": None}]
    now = time.time()
    drv.state = {"power": True, "mode": "cool", "target_temp": 22, "updated_at": now}
    live_ctl.tick()
    assert [e["source"] for e in store.events()] == ["manual"]  # our own change
    # Someone switches it off with the remote.
    drv.state = {"power": False, "mode": "cool", "target_temp": 22, "updated_at": now}
    live_ctl.tick()
    ev = store.events()[0]
    assert ev["source"] == "device" and ev["is_off"] and "remote" in ev["detail"]
    assert live_ctl.is_on() is False


def test_live_schedule_end_uses_real_state(live_ctl, store):
    drv = live_ctl.driver
    store.add_schedule("m", "0123456", "08:00", "10:00", "cool24")
    tick(live_ctl, 2026, 11, 23, 8, 0)
    assert drv.applied[-1]["power"] is True
    tick(live_ctl, 2026, 11, 23, 10, 0)
    # No off preset exists: live drivers send power off directly.
    assert drv.applied[-1] == {"power": False}
