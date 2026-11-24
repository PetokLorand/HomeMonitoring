"""AC controller: sends presets, tracks the assumed state, runs schedules and timers.

With a live driver (HomeWhiz) the AC's real state is read back, and changes made
with the physical remote are recorded as "device" events. With an IR driver the
"state" is simply the last preset that was successfully sent.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, tzinfo

from .config import Config
from .drivers import Driver, DriverError
from .store import Preset, Schedule, Store

log = logging.getLogger(__name__)

READINGS_RETENTION_S = 90 * 86400
LIVE_STATE_MAX_AGE_S = 600  # older live state is not trusted
OWN_COMMAND_WINDOW_S = 120  # a power change this soon after our command is ours


# ---------------------------------------------------------------------------
# Schedule window maths (pure functions, easy to test)
# ---------------------------------------------------------------------------

def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _at(d: date, t: dtime, tz: tzinfo | None) -> datetime:
    dt = datetime.combine(d, t)
    return dt.replace(tzinfo=tz) if tz else dt.astimezone()


def window_for(s: Schedule, now: datetime) -> tuple[datetime, datetime] | None:
    """Return (start, end) of the window of `s` containing `now`, if any."""
    tz = now.tzinfo
    start_t, end_t = _hm(s.start), _hm(s.end)
    crosses = end_t <= start_t
    for back in (0, 1) if crosses else (0,):
        d = now.date() - timedelta(days=back)
        if str(d.weekday()) not in s.days:
            continue
        start = _at(d, start_t, tz)
        end = _at(d + timedelta(days=1) if crosses else d, end_t, tz)
        if start <= now < end:
            return start, end
    return None


def active_windows(schedules: list[Schedule], now: datetime) -> list[tuple[Schedule, datetime]]:
    """Enabled schedules whose window contains `now`, with each window's start."""
    out = []
    for s in schedules:
        if s.enabled and (w := window_for(s, now)):
            out.append((s, w[0]))
    return out


def upcoming_transitions(schedules: list[Schedule], now: datetime, days: int = 8) -> list[dict]:
    """Window starts/ends in the next `days` days, soonest first."""
    tz = now.tzinfo
    out = []
    for s in schedules:
        if not s.enabled:
            continue
        start_t, end_t = _hm(s.start), _hm(s.end)
        crosses = end_t <= start_t
        for i in range(-1, days):
            d = now.date() + timedelta(days=i)
            if str(d.weekday()) not in s.days:
                continue
            start = _at(d, start_t, tz)
            end = _at(d + timedelta(days=1) if crosses else d, end_t, tz)
            if start > now:
                out.append({"at": start.timestamp(), "kind": "start", "schedule": s.name, "preset": s.preset})
            if end > now:
                out.append({"at": end.timestamp(), "kind": "end", "schedule": s.name, "preset": None})
    out.sort(key=lambda x: x["at"])
    return out


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class Controller:
    def __init__(self, cfg: Config, store: Store, driver: Driver) -> None:
        self.cfg = cfg
        self.store = store
        self.driver = driver
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sensor = 0.0
        self._last_prune = 0.0
        self.sensor_error: str | None = None

    @property
    def tz(self) -> tzinfo | None:
        return self.cfg.tz

    def now(self) -> datetime:
        return datetime.now(self.tz) if self.tz else datetime.now().astimezone()

    # -- commands ----------------------------------------------------------
    def apply(self, preset_name: str, source: str = "manual", off_after_min: float | None = None) -> dict:
        """Apply a preset. Raises KeyError for unknown presets, DriverError on failure."""
        preset = self.store.get_preset(preset_name)
        if preset is None:
            raise KeyError(preset_name)
        return self._send(
            preset.settings(), preset.name, preset.label, source, off_after_min, self.store.get_code(preset.name)
        )

    def control(self, settings: dict, source: str = "manual", off_after_min: float | None = None) -> dict:
        """Apply arbitrary settings (live drivers only; IR needs a learned preset)."""
        if self.driver.needs_code:
            raise DriverError("IR drivers can only send learned presets")
        settings = {"power": bool(settings.get("power"))} | {
            k: settings.get(k) for k in ("mode", "target_temp", "fan") if settings.get("power")
        }
        label = _describe(settings)
        return self._send(settings, "custom", label, source, off_after_min, None)

    def _send(self, settings, name, label, source, off_after_min, code) -> dict:
        is_off = not settings.get("power")
        with self._lock:
            try:
                self.driver.apply(settings, code)
            except DriverError as exc:
                self.store.add_event(source, label, is_off, False, str(exc))
                log.error("Applying %s failed: %s", label, exc)
                raise
            detail = f"auto-off in {off_after_min:g} min" if off_after_min and not is_off else ""
            self.store.add_event(source, label, is_off, True, detail)
            self._set_state(name, label, settings, source)
            self.store.kv_set("observed_power", not is_off)
            # Any new command replaces a pending auto-off, unless it sets one.
            if off_after_min and not is_off:
                self.store.kv_set("off_timer_at", time.time() + off_after_min * 60)
            else:
                self.store.kv_set("off_timer_at", None)
            log.info("Applied %s (%s)", label, source)
            return self.state()

    def _set_state(self, name: str, label: str, settings: dict, source: str) -> None:
        self.store.kv_set(
            "state",
            {
                "preset": name,
                "label": label,
                "is_off": not settings.get("power"),
                "mode": settings.get("mode"),
                "target_temp": settings.get("target_temp"),
                "fan": settings.get("fan"),
                "since": time.time(),
                "source": source,
            },
        )

    def turn_off(self, source: str = "manual") -> dict:
        off = self.store.off_preset()
        if off is not None:
            return self.apply(off.name, source)
        if self.driver.needs_code:
            raise KeyError("no 'off' preset learned yet")
        return self.control({"power": False}, source)

    def set_off_timer(self, minutes: float | None) -> None:
        self.store.kv_set("off_timer_at", time.time() + minutes * 60 if minutes else None)

    def learn(self, name: str) -> Preset:
        """Learn an IR code for an existing preset (IR drivers only)."""
        preset = self.store.get_preset(name)
        if preset is None:
            raise KeyError(name)
        with self._lock:
            code = self.driver.learn(self.cfg.device.learn_timeout_s)
        return self.store.save_preset(
            preset.name, preset.label, code, preset.is_off, preset.mode, preset.target_temp, preset.fan
        )

    # -- status -------------------------------------------------------------
    def live(self) -> dict | None:
        live = self.driver.read_state()
        if live and time.time() - live.get("updated_at", 0) <= LIVE_STATE_MAX_AGE_S:
            return live
        return None

    def is_on(self) -> bool | None:
        live = self.live()
        if live and live.get("power") is not None:
            return live["power"]
        state = self.store.kv_get("state")
        return None if state is None else not state["is_off"]

    def state(self) -> dict:
        live = self.live()
        return {
            "driver": {
                "kind": self.driver.kind,
                "live_state": self.driver.live_state,
                "needs_code": self.driver.needs_code,
                "description": self.driver.describe(),
                "options": self.driver.options(),
            },
            "on": self.is_on(),
            "live": live,
            "last_command": self.store.kv_get("state"),
            "off_timer_at": self.store.kv_get("off_timer_at"),
            "room": self.store.latest_reading(),
            "sensor_error": self.sensor_error,
            "active_schedules": self.store.kv_get("active_schedules", []),
            "upcoming": upcoming_transitions(self.store.list_schedules(), self.now())[:5],
            "rated_power_kw": self.cfg.monitor.rated_power_kw,
        }

    def runtime(self, start: float, end: float) -> float:
        """Seconds the AC was running between start and end, from the event log."""
        prev = self.store.last_ok_event_before(start)
        on = bool(prev) and not prev["is_off"]
        t, total = start, 0.0
        for ev in self.store.ok_events_between(start, end):
            if on:
                total += ev["ts"] - t
            t, on = ev["ts"], not ev["is_off"]
        if on:
            total += min(end, time.time()) - t
        return max(total, 0.0)

    def daily_stats(self, days: int = 7) -> list[dict]:
        today = self.now().date()
        out = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            start = _at(d, dtime(0), self.tz).timestamp()
            end = _at(d + timedelta(days=1), dtime(0), self.tz).timestamp()
            secs = self.runtime(start, end)
            out.append(
                {
                    "date": d.isoformat(),
                    "runtime_h": round(secs / 3600, 2),
                    "energy_kwh": round(secs / 3600 * self.cfg.monitor.rated_power_kw, 2),
                }
            )
        return out

    # -- background loop ------------------------------------------------------
    def tick(self, now: datetime | None = None) -> None:
        now = now or self.now()
        try:
            self.driver.poll()
        except Exception:
            log.exception("Driver poll failed")
        self._observe(now)
        self._run_schedules(now)
        self._run_off_timer(now)
        self._poll_sensors(now)

    def _observe(self, now: datetime) -> None:
        """Record power changes the AC reports that we did not cause (remote, app)."""
        live = self.live()
        if not live or live.get("power") is None:
            return
        power = live["power"]
        last = self.store.kv_get("observed_power")
        if last == power:
            return
        self.store.kv_set("observed_power", power)
        state = self.store.kv_get("state") or {}
        ours = (
            state.get("is_off") == (not power)
            and now.timestamp() - state.get("since", 0) < OWN_COMMAND_WINDOW_S
        )
        if ours:
            return
        settings = {k: live.get(k) for k in ("mode", "target_temp", "fan")} | {"power": power}
        label = _describe(settings)
        detail = "first reading" if last is None else "changed outside acmonitor (remote or app)"
        self.store.add_event("device", label, not power, True, detail, ts=now.timestamp())
        self._set_state("device", label, settings, "device")
        if not power:
            self.store.kv_set("off_timer_at", None)

    def _run_schedules(self, now: datetime) -> None:
        active = active_windows(self.store.list_schedules(), now)
        now_ids = {s.id for s, _ in active}
        prev_ids = set(self.store.kv_get("active_schedules", []))
        started, ended = now_ids - prev_ids, prev_ids - now_ids
        # Persist first so a failed send is not retried every tick.
        self.store.kv_set("active_schedules", sorted(now_ids))
        if not started and not ended:
            return

        state = self.store.kv_get("state") or {}
        if active:
            # Overlapping windows: the one that started most recently wins.
            winner = max(active, key=lambda a: (a[1], a[0].id))[0]
            if winner.id in started or state.get("preset") != winner.preset:
                self._safe(lambda: self.apply(winner.preset, "schedule"), f"window '{winner.name}' started")
        elif ended and self.is_on() is not False:
            self._safe(lambda: self.turn_off("schedule"), "window ended")

    def _run_off_timer(self, now: datetime) -> None:
        at = self.store.kv_get("off_timer_at")
        if at and now.timestamp() >= at:
            self.store.kv_set("off_timer_at", None)
            self._safe(lambda: self.turn_off("timer"), "auto-off timer")

    def _safe(self, fn, why: str) -> None:
        log.info("%s", why)
        try:
            fn()
        except (DriverError, KeyError) as exc:
            log.error("%s: failed: %s", why, exc)
            if isinstance(exc, KeyError):  # nothing reached the event log yet
                self.store.add_event("schedule", "?", True, False, f"{why}: {exc}")

    def _poll_sensors(self, now: datetime) -> None:
        ts = now.timestamp()
        if ts - self._last_sensor < self.cfg.monitor.sensor_interval_s:
            return
        self._last_sensor = ts
        try:
            data = self.driver.read_sensors()
            self.sensor_error = None
        except DriverError as exc:
            self.sensor_error = str(exc)
            log.warning("Sensor read failed: %s", exc)
            return
        if data:
            self.store.add_reading(data.get("temperature"), data.get("humidity"), ts)
        if ts - self._last_prune > 86400:
            self._last_prune = ts
            self.store.prune(ts - READINGS_RETENTION_S)

    def start(self) -> None:
        if self._thread:
            return
        self.driver.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="acmonitor-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self.driver.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the scheduler die
                log.exception("Scheduler tick failed")
            self._stop.wait(self.cfg.monitor.tick_s)


def _describe(settings: dict) -> str:
    if not settings.get("power"):
        return "Off"
    t = settings.get("target_temp")
    parts = [settings.get("mode"), f"{t:g}°C" if t is not None else None,
             f"fan {settings['fan']}" if settings.get("fan") else None]
    return " · ".join(p for p in parts if p) or "On"
