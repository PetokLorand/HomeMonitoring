from fastapi.testclient import TestClient

from acmonitor.web import create_app


def test_api_roundtrip(ir_ctl):
    c = TestClient(create_app(ir_ctl, run_background=False))
    assert "AC Monitor" in c.get("/").text
    st = c.get("/api/state").json()
    assert st["driver"]["kind"] == "mock" and st["on"] is None

    r = c.post("/api/presets/cool24/apply", json={"off_after_min": 45})
    assert r.status_code == 200 and r.json()["on"] is True and r.json()["off_timer_at"]
    assert c.post("/api/presets/nope/apply").status_code == 404

    r = c.post("/api/schedules", json={"days": "01234", "start": "18:00", "end": "22:00", "preset": "cool24"})
    assert r.status_code == 200
    sid = r.json()["id"]
    assert c.post("/api/schedules", json={"start": "18:00", "end": "18:00", "preset": "cool24"}).status_code == 400
    assert c.post("/api/schedules", json={"start": "25:00", "end": "18:00", "preset": "cool24"}).status_code == 400
    assert c.patch(f"/api/schedules/{sid}", json={"enabled": False}).json()["enabled"] is False
    assert c.delete("/api/presets/cool24").status_code == 400  # in use by a schedule
    assert c.delete(f"/api/schedules/{sid}").status_code == 200

    assert c.post("/api/presets", json={"name": "Bad Name"}).status_code == 400
    assert c.post("/api/presets", json={"name": "dry", "mode": "dry"}).status_code == 200
    learned = c.post("/api/presets/dry/learn").json()
    assert learned["has_code"] is True

    assert c.post("/api/off").json()["on"] is False
    h = c.get("/api/history?hours=24").json()
    assert len(h["events"]) == 2
    assert len(c.get("/api/stats").json()["days"]) == 7
    exported = c.get("/api/presets/export").json()
    assert {p["name"] for p in exported} == {"off", "cool24", "heat22", "dry"}
