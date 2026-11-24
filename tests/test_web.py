import json
import urllib.error
import urllib.request

import pytest

from acmonitor.web import serve_in_thread


@pytest.fixture
def api(ir_ctl):
    server = serve_in_thread(ir_ctl)
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if r.headers["Content-Type"] == "application/json" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, (json.loads(raw) if e.headers.get("Content-Type") == "application/json" else raw)

    yield call
    server.shutdown()


def test_api_roundtrip(api):
    code, html = api("GET", "/")
    assert code == 200 and "AC Monitor" in html
    code, st = api("GET", "/api/state")
    assert st["driver"]["kind"] == "mock" and st["on"] is None

    code, st = api("POST", "/api/presets/cool24/apply", {"off_after_min": 45})
    assert code == 200 and st["on"] is True and st["off_timer_at"]
    assert api("POST", "/api/presets/nope/apply")[0] == 404
    assert api("POST", "/api/presets/cool24/apply", {"off_after_min": -1})[0] == 400

    code, s = api("POST", "/api/schedules", {"days": "01234", "start": "18:00", "end": "22:00", "preset": "cool24"})
    assert code == 200
    sid = s["id"]
    assert api("POST", "/api/schedules", {"start": "18:00", "end": "18:00", "preset": "cool24"})[0] == 400
    assert api("POST", "/api/schedules", {"start": "25:00", "end": "18:00", "preset": "cool24"})[0] == 400
    assert api("POST", "/api/schedules", {"start": "08:00"})[0] == 400
    assert api("PATCH", f"/api/schedules/{sid}", {"enabled": False})[1]["enabled"] is False
    assert api("DELETE", "/api/presets/cool24")[0] == 400  # in use by a schedule
    assert api("DELETE", f"/api/schedules/{sid}")[0] == 200

    assert api("POST", "/api/presets", {"name": "Bad Name"})[0] == 400
    assert api("POST", "/api/presets", {"name": "dry", "mode": "dry"})[0] == 200
    assert api("POST", "/api/presets/dry/learn")[1]["has_code"] is True
    assert api("POST", "/api/control", {"power": True})[0] == 502  # IR can't do free-form settings

    assert api("POST", "/api/off")[1]["on"] is False
    code, h = api("GET", "/api/history?hours=24")
    assert len(h["events"]) == 2  # apply, off (the rejected control request is not an event)
    assert len(api("GET", "/api/stats")[1]["days"]) == 7
    assert {p["name"] for p in api("GET", "/api/presets/export")[1]} == {"off", "cool24", "heat22", "dry"}
    assert api("GET", "/api/nothing")[0] == 404
    assert api("PUT", "/api/state")[0] in (404, 405, 501)
