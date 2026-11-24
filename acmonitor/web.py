"""HTTP API and dashboard, on the standard library only (runs on a phone too)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from .controller import Controller
from .drivers import DriverError

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


class HTTPError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status, self.detail = status, detail


# -- request-body helpers -------------------------------------------------------

def _opt(body: dict, key: str, kind, default=None):
    v = body.get(key, default)
    if v is None or v == "":
        return default
    try:
        if kind is bool:
            if not isinstance(v, bool):
                raise ValueError
            return v
        return kind(v)
    except (TypeError, ValueError):
        raise HTTPError(400, f"{key} must be {kind.__name__}") from None


def _minutes(body: dict, key: str) -> float | None:
    v = _opt(body, key, float)
    if v is not None and not (0 < v <= 24 * 60):
        raise HTTPError(400, f"{key} must be between 0 and 1440 minutes")
    return v


# -- application -------------------------------------------------------------

class App:
    def __init__(self, ctl: Controller) -> None:
        self.ctl = ctl
        self.store = ctl.store
        self.routes: list[tuple[str, re.Pattern, callable]] = []
        r = self.route
        r("GET", r"/", self.index)
        r("GET", r"/api/state", lambda q, b: self.ctl.state())
        r("GET", r"/api/history", self.history)
        r("GET", r"/api/stats", self.stats)
        r("POST", r"/api/control", self.control)
        r("POST", r"/api/off", lambda q, b: self.ctl.turn_off("manual"))
        r("POST", r"/api/timer", self.timer)
        r("GET", r"/api/presets", self.presets)
        r("POST", r"/api/presets", self.save_preset)
        r("GET", r"/api/presets/export", lambda q, b: self.store.export_presets())
        r("DELETE", r"/api/presets/(?P<name>[^/]+)", self.delete_preset)
        r("POST", r"/api/presets/(?P<name>[^/]+)/apply", self.apply_preset)
        r("POST", r"/api/presets/(?P<name>[^/]+)/learn", lambda q, b, name: self.ctl.learn(name).public())
        r("GET", r"/api/schedules", lambda q, b: [s.public() for s in self.store.list_schedules()])
        r("POST", r"/api/schedules", self.add_schedule)
        r("PATCH", r"/api/schedules/(?P<sid>\d+)", self.patch_schedule)
        r("DELETE", r"/api/schedules/(?P<sid>\d+)", self.delete_schedule)

    def route(self, method: str, pattern: str, fn) -> None:
        self.routes.append((method, re.compile(pattern + "$"), fn))

    def dispatch(self, method: str, path: str, query: dict, body: dict):
        allowed = False
        for m, pat, fn in self.routes:
            match = pat.match(path)
            if not match:
                continue
            allowed = True
            if m == method:
                try:
                    return fn(query, body, **match.groupdict())
                except HTTPError:
                    raise
                except KeyError as exc:
                    raise HTTPError(404, f"not found: {exc.args[0] if exc.args else ''}") from exc
                except ValueError as exc:
                    raise HTTPError(400, str(exc)) from exc
                except DriverError as exc:
                    raise HTTPError(502, str(exc)) from exc
        raise HTTPError(405 if allowed else 404, "method not allowed" if allowed else "not found")

    # -- handlers ----------------------------------------------------------------
    def index(self, q, b):
        return resources.files("acmonitor").joinpath("static/index.html").read_text(encoding="utf-8")

    def history(self, q, b):
        hours = float(q.get("hours", 24))
        since = time.time() - min(max(hours, 0.1), 24 * 90) * 3600
        prev = self.store.last_ok_event_before(since)
        return {
            "since": since,
            "initial_on": bool(prev) and not prev["is_off"],
            "readings": self.store.readings(since),
            "events": self.store.events(since, limit=500),
        }

    def stats(self, q, b):
        days = max(1, min(int(q.get("days", 7)), 90))
        return {"days": self.ctl.daily_stats(days), "rated_power_kw": self.ctl.cfg.monitor.rated_power_kw}

    def control(self, q, b):
        settings = {
            "power": _opt(b, "power", bool, False),
            "mode": _opt(b, "mode", str),
            "target_temp": _opt(b, "target_temp", float),
            "fan": _opt(b, "fan", str),
        }
        return self.ctl.control(settings, "manual", _minutes(b, "off_after_min"))

    def timer(self, q, b):
        self.ctl.set_off_timer(_minutes(b, "minutes"))
        return self.ctl.state()

    def presets(self, q, b):
        return [p.public() | {"summary": p.describe()} for p in self.store.list_presets()]

    def save_preset(self, q, b):
        name = _opt(b, "name", str)
        if not name:
            raise HTTPError(400, "name is required")
        return self.store.save_preset(
            name, _opt(b, "label", str, ""), None, _opt(b, "is_off", bool, False),
            _opt(b, "mode", str), _opt(b, "target_temp", float), _opt(b, "fan", str),
        ).public()

    def delete_preset(self, q, b, name):
        self.store.delete_preset(name)
        return {"ok": True}

    def apply_preset(self, q, b, name):
        return self.ctl.apply(name, "manual", _minutes(b, "off_after_min"))

    def add_schedule(self, q, b):
        for k in ("start", "end", "preset"):
            if not b.get(k):
                raise HTTPError(400, f"{k} is required")
        return self.store.add_schedule(
            _opt(b, "name", str, ""), _opt(b, "days", str, "0123456"), b["start"], b["end"], b["preset"],
            _opt(b, "enabled", bool, True),
        ).public()

    def patch_schedule(self, q, b, sid):
        fields = {k: _opt(b, k, str) for k in ("name", "days", "start", "end", "preset")}
        fields["enabled"] = _opt(b, "enabled", bool)
        return self.store.update_schedule(int(sid), **fields).public()

    def delete_schedule(self, q, b, sid):
        self.store.delete_schedule(int(sid))
        return {"ok": True}


def _handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "acmonitor"

        def _handle(self, method: str) -> None:
            u = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise HTTPError(413, "request too large")
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    raise HTTPError(400, "invalid JSON") from None
                if not isinstance(body, dict):
                    raise HTTPError(400, "JSON object expected")
                result = app.dispatch(method, u.path, query, body)
                status = 200
            except HTTPError as exc:
                status, result = exc.status, {"detail": exc.detail}
            except Exception:
                log.exception("Unhandled error for %s %s", method, self.path)
                status, result = 500, {"detail": "internal error"}
            if isinstance(result, str):
                data, ctype = result.encode(), "text/html; charset=utf-8"
            else:
                data, ctype = json.dumps(result, default=str).encode(), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PATCH(self):
            self._handle("PATCH")

        def do_DELETE(self):
            self._handle("DELETE")

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def make_server(ctl: Controller, host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _handler(App(ctl)))
    server.daemon_threads = True
    return server


def serve(ctl: Controller, host: str, port: int) -> None:
    """Run the scheduler and the web server until interrupted."""
    server = make_server(ctl, host, port)
    ctl.start()
    log.info("Dashboard on http://%s:%d", host if host != "0.0.0.0" else "<this-device-ip>", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ctl.stop()


def serve_in_thread(ctl: Controller, host: str = "127.0.0.1", port: int = 0):
    """For tests: start a server on a free port without the scheduler."""
    server = make_server(ctl, host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
