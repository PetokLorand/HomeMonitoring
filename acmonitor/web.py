"""HTTP API and dashboard."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .controller import Controller
from .drivers import DriverError


class PresetIn(BaseModel):
    name: str
    label: str = ""
    is_off: bool = False
    mode: str | None = None
    target_temp: float | None = None
    fan: str | None = None


class ApplyIn(BaseModel):
    off_after_min: float | None = Field(default=None, gt=0, le=24 * 60)


class ControlIn(ApplyIn):
    power: bool
    mode: str | None = None
    target_temp: float | None = None
    fan: str | None = None


class TimerIn(BaseModel):
    minutes: float | None = Field(default=None, gt=0, le=24 * 60)


class ScheduleIn(BaseModel):
    name: str = ""
    days: str = "0123456"
    start: str
    end: str
    preset: str
    enabled: bool = True


class SchedulePatch(BaseModel):
    name: str | None = None
    days: str | None = None
    start: str | None = None
    end: str | None = None
    preset: str | None = None
    enabled: bool | None = None


def _guard(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, f"not found: {exc.args[0] if exc.args else ''}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except DriverError as exc:
        raise HTTPException(502, str(exc)) from exc


def create_app(ctl: Controller, run_background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_background:
            ctl.start()
        yield
        if run_background:
            ctl.stop()

    app = FastAPI(title="AC Monitor", lifespan=lifespan)
    store = ctl.store

    @app.get("/", response_class=HTMLResponse)
    def index():
        return resources.files("acmonitor").joinpath("static/index.html").read_text(encoding="utf-8")

    # -- status ---------------------------------------------------------------
    @app.get("/api/state")
    def state():
        return ctl.state()

    @app.get("/api/history")
    def history(hours: float = 24):
        since = time.time() - min(hours, 24 * 90) * 3600
        prev = store.last_ok_event_before(since)
        return {
            "since": since,
            "initial_on": bool(prev) and not prev["is_off"],
            "readings": store.readings(since),
            "events": store.events(since, limit=500),
        }

    @app.get("/api/stats")
    def stats(days: int = 7):
        return {"days": ctl.daily_stats(max(1, min(days, 90))), "rated_power_kw": ctl.cfg.monitor.rated_power_kw}

    # -- control ----------------------------------------------------------------
    @app.post("/api/control")
    def control(body: ControlIn):
        settings = body.model_dump(exclude={"off_after_min"})
        return _guard(lambda: ctl.control(settings, "manual", body.off_after_min))

    @app.post("/api/off")
    def off():
        return _guard(lambda: ctl.turn_off("manual"))

    @app.post("/api/timer")
    def timer(body: TimerIn):
        ctl.set_off_timer(body.minutes)
        return ctl.state()

    # -- presets ----------------------------------------------------------------
    @app.get("/api/presets")
    def presets():
        return [p.public() | {"summary": p.describe()} for p in store.list_presets()]

    @app.post("/api/presets")
    def save_preset(body: PresetIn):
        return _guard(
            lambda: store.save_preset(
                body.name, body.label, None, body.is_off, body.mode, body.target_temp, body.fan
            ).public()
        )

    @app.delete("/api/presets/{name}")
    def delete_preset(name: str):
        _guard(lambda: store.delete_preset(name))
        return {"ok": True}

    @app.post("/api/presets/{name}/apply")
    def apply_preset(name: str, body: ApplyIn | None = None):
        return _guard(lambda: ctl.apply(name, "manual", body.off_after_min if body else None))

    @app.post("/api/presets/{name}/learn")
    def learn(name: str):
        return _guard(lambda: ctl.learn(name).public())

    @app.get("/api/presets/export")
    def export_presets():
        return store.export_presets()

    # -- schedules --------------------------------------------------------------
    @app.get("/api/schedules")
    def schedules():
        return [s.public() for s in store.list_schedules()]

    @app.post("/api/schedules")
    def add_schedule(body: ScheduleIn):
        return _guard(lambda: store.add_schedule(**body.model_dump()).public())

    @app.patch("/api/schedules/{sid}")
    def patch_schedule(sid: int, body: SchedulePatch):
        return _guard(lambda: store.update_schedule(sid, **body.model_dump()).public())

    @app.delete("/api/schedules/{sid}")
    def delete_schedule(sid: int):
        store.delete_schedule(sid)
        return {"ok": True}

    return app
