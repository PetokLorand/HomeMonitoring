import datetime as dt
import json

import pytest

from acmonitor.drivers import DriverError
from acmonitor.drivers.homewhiz import ACMap, Credentials, buffer_from_shadow, friendly, sign_get

# Structure as published by Arçelik for HomeWhiz air conditioners
# (field names per the home-assistant-HomeWhiz configuration model).
BASIC = {
    "program": {
        "strKey": "AIR_CONDITIONER_PROGRAM",
        "wifiArrayIndex": 34,
        "wfaWriteIndex": None,
        "isSwitch": None,
        "values": [
            {"strKey": "AIR_CONDITIONER_PROGRAM_COOLING", "wifiArrayValue": 0},
            {"strKey": "AIR_CONDITIONER_PROGRAM_AUTO", "wifiArrayValue": 6},
            {"strKey": "AIR_CONDITIONER_PROGRAM_DRY", "wifiArrayValue": 1},
            {"strKey": "AIR_CONDITIONER_PROGRAM_HEATING", "wifiArrayValue": 4},
            {"strKey": "AIR_CONDITIONER_PROGRAM_FAN", "wifiArrayValue": 2},
        ],
    },
    "subPrograms": [
        {
            "strKey": "AIR_CONDITIONER_TARGET_TEMPERATURE",
            "wifiArrayIndex": 35,
            "boundedValues": [{"factor": 1, "lowerLimit": 17, "upperLimit": 30, "step": 1,
                               "strKey": "TEMP", "unit": "°C"}],
            "enumValues": None,
        },
        {
            "strKey": "AIR_CONDITIONER_WIND_STRENGTH",
            "wifiArrayIndex": 36,
            "boundedValues": None,
            "enumValues": [
                {"strKey": "WIND_STRENGTH_LOW", "wifiArrayValue": 0},
                {"strKey": "WIND_STRENGTH_MID", "wifiArrayValue": 1},
                {"strKey": "WIND_STRENGTH_HIGH", "wifiArrayValue": 2},
                {"strKey": "WIND_STRENGTH_AUTO", "wifiArrayValue": 3},
            ],
        },
    ],
    "deviceStates": {
        "wifiArrayReadIndex": 43,
        "wifiArrayWriteIndex": 43,
        "wfaIndex": None,
        "states": [
            {"strKey": "DEVICE_STATE_ON", "wifiArrayValue": 10},
            {"strKey": "DEVICE_STATE_OFF", "wifiArrayValue": 20},
        ],
    },
    "monitorings": [
        {
            "strKey": "AIR_CONDITIONER_ROOM_TEMPERATURE",
            "wifiArrayIndex": 40,
            "boundedValues": [{"factor": 1, "lowerLimit": 0, "upperLimit": 90, "step": 1,
                               "strKey": "T", "unit": "°C"}],
        }
    ],
}

ADVANCED = json.loads(json.dumps(BASIC))
ADVANCED["deviceStates"]["wifiArrayWriteIndex"] = 44
ADVANCED["subPrograms"][0]["boundedValues"][0].update(factor=0.5, lowerLimit=16)
ADVANCED["subPrograms"][1] = {
    "strKey": "AIR_CONDITIONER_WIND_STRENGTH",
    "wifiArrayIndex": 36,
    "enumValues": [{"strKey": "WIND_STRENGTH_CHAOS_SWING", "wifiArrayValue": 8}],
    "boundedValues": [{"factor": 1, "lowerLimit": 2, "upperLimit": 6, "step": 1, "strKey": "W", "unit": None}],
}
ADVANCED["monitorings"][0]["boundedValues"][0]["factor"] = 0.5
ADVANCED["monitorings"].append(
    {
        "strKey": "AIR_CONDITIONER_INSTANT_CONSUMPTION",
        "wifiArrayIndex": 45,
        "boundedValues": [{"factor": 1, "lowerLimit": 0, "upperLimit": 100, "step": 1, "strKey": "C", "unit": "hw"}],
    }
)


def buf(values: dict[int, int], size: int = 80) -> list[int]:
    b = [0] * size
    for k, v in values.items():
        b[k] = v
    return b


def test_friendly():
    assert friendly("AIR_CONDITIONER_PROGRAM") == "air_conditioner_program"
    assert friendly("A+B_") == "aplusb"


def test_decode_basic():
    m = ACMap.from_config(BASIC)
    off = m.decode(buf({34: 4, 35: 26, 36: 0, 40: 28, 43: 20}))
    assert off == {"power": False, "mode": "heat", "target_temp": 26, "room_temp": 28,
                   "fan": "low", "consumption": None}
    auto = m.decode(buf({34: 6, 35: 23, 36: 3, 40: 26, 43: 10}))
    assert auto["power"] is True and auto["mode"] == "auto" and auto["fan"] == "auto"


def test_decode_clamps_high_bit():
    m = ACMap.from_config(BASIC)
    assert m.decode(buf({43: 10 + 128}))["power"] is True


def test_commands_basic():
    m = ACMap.from_config(BASIC)
    off = m.decode(buf({34: 4, 35: 26, 36: 0, 40: 28, 43: 20}))
    # Turning on in the mode it already has: only the power write.
    assert m.commands({"power": True, "mode": "heat"}, off) == [(43, 10)]
    assert m.commands({"power": True, "mode": "auto"}, off) == [(43, 10), (34, 6)]
    on = m.decode(buf({34: 6, 35: 23, 36: 3, 43: 10}))
    assert m.commands({"power": False}, on) == [(43, 20)]
    # Unknown current state: send everything that was asked for.
    assert m.commands({"power": True, "mode": "cool", "target_temp": 24, "fan": "high"}) == [
        (43, 10), (34, 0), (35, 24), (36, 2)]


def test_commands_validate():
    m = ACMap.from_config(BASIC)
    with pytest.raises(DriverError):
        m.commands({"power": True, "mode": "turbo"})
    with pytest.raises(DriverError):
        m.commands({"power": True, "target_temp": 40})


def test_advanced_factors_and_write_index():
    m = ACMap.from_config(ADVANCED)
    st = m.decode(buf({34: 4, 35: 60, 36: 6, 40: 30, 43: 20, 45: 4}))
    assert st["target_temp"] == 30.0
    assert st["room_temp"] == 15.0
    assert st["fan"] == "6"
    assert st["consumption"] == 0.4  # firmware factor bug corrected
    assert m.commands({"power": True}, st) == [(44, 10)]
    assert m.commands({"power": True, "target_temp": 23.5}, st)[-1] == (35, 47)
    assert m.options()["fan_speeds"] == ["chaos_swing", "2", "3", "4", "5", "6"]
    assert m.options()["modes"] == ["cool", "auto", "dry", "heat", "fan"]


def test_not_an_ac():
    with pytest.raises(DriverError):
        ACMap.from_config({"deviceStates": BASIC["deviceStates"], "program": {"strKey": "WASHER_PROGRAM"}})


def test_buffer_from_shadow():
    payload = json.dumps({"state": {"reported": {"wfa": [1, 2, 3], "wfaStartOffset": 30, "connected": True}}})
    b, rep = buffer_from_shadow(payload)
    assert len(b) == 33 and b[30:] == [1, 2, 3] and rep["connected"]
    b, _ = buffer_from_shadow(json.dumps({"state": {"reported": {"wfa": [7]}}}))
    assert b[26] == 7  # default offset
    assert buffer_from_shadow(b"{}") is None
    assert buffer_from_shadow(b"not json") is None


def test_sign_get_matches_aws_reference():
    # Expected value cross-checked against botocore's SigV4Auth for the same inputs.
    creds = Credentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "TOKEN", 0)
    now = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    h = sign_get("https://api.arcelikiot.com/procam/contents?lang=en-GB&applianceId=A1&ctype=X%2CY", creds, now)
    assert h["x-amz-date"] == "20260102T030405Z"
    assert h["Authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260102/eu-west-1/execute-api/aws4_request, "
        "SignedHeaders=host;x-amz-date;x-amz-security-token, Signature="
    )
    assert h["Authorization"].endswith(SIGNATURE)


SIGNATURE = "0e5c6ae558f12876f3f093e22fdd2b7ee6c15ca04d73188390c5fa03aea161b7"


# ---------------------------------------------------------------------------
# Driver wiring with the cloud and MQTT faked out
# ---------------------------------------------------------------------------

class _Done:
    def result(self, timeout=None):
        return {}


class FakeConnection:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.subs = {}
        self.published = []

    def connect(self):
        return _Done()

    def disconnect(self):
        return _Done()

    def subscribe(self, topic, qos, callback=None):
        self.subs[topic] = callback
        return _Done(), 1

    def publish(self, topic, payload, qos, retain=False):
        self.published.append((topic, json.loads(payload)))
        return _Done(), 1

    def push(self, values, offset=26):
        wfa = buf(values)[offset:]
        doc = json.dumps({"state": {"reported": {"wfa": wfa, "wfaStartOffset": offset, "connected": True}}})
        for cb in self.subs.values():
            cb(topic="t", payload=doc.encode(), dup=False, qos=1, retain=False)
            break


@pytest.fixture
def driver(monkeypatch, tmp_path):
    from awsiot import mqtt_connection_builder

    from acmonitor.config import HomeWhizConfig
    from acmonitor.drivers import homewhiz as hw

    conns = []

    def build(**kw):
        conns.append(FakeConnection(**kw))
        return conns[-1]

    monkeypatch.setattr(mqtt_connection_builder, "websockets_with_default_aws_signing", build)
    monkeypatch.setattr(hw, "login", lambda u, p: hw.Credentials("A", "S", "T", 9e12))
    monkeypatch.setattr(hw, "list_appliances", lambda c: [
        {"applianceId": "W1", "applianceType": 1, "connectivity": "WIFI", "name": "Washer"},
        {"applianceId": "AC1", "applianceType": 9, "connectivity": "WIFI", "name": "Bedroom AC", "model": "X"},
    ])
    monkeypatch.setattr(hw, "fetch_configuration", lambda c, a, lang: ADVANCED)
    monkeypatch.setattr(hw, "COMMAND_GAP_S", 0)
    d = hw.HomeWhizDriver(HomeWhizConfig(username="u", password="p"), cache_dir=tmp_path)
    d._conns = conns
    return d


def test_driver_connects_and_reads(driver, tmp_path):
    driver.poll()
    assert driver.error is None, driver.error
    conn = driver._conns[0]
    assert conn.kwargs["endpoint"].endswith("iot.eu-west-1.amazonaws.com")
    assert set(conn.subs) == {"$aws/things/AC1/shadow/update/accepted", "$aws/things/AC1/shadow/get/accepted"}
    topics = [p[0] for p in conn.published]
    assert topics == ["$aws/things/AC1/shadow/get", "$aws/things/AC1/shadow/get"]
    assert conn.published[1][1] == {"type": "fread/command"}
    assert (tmp_path / "homewhiz-AC1.json").exists()  # config cached for offline restarts

    assert driver.read_state() is None
    conn.push({34: 0, 35: 48, 40: 50, 43: 10})
    st = driver.read_state()
    assert st["power"] is True and st["mode"] == "cool" and st["target_temp"] == 24.0 and st["room_temp"] == 25.0
    assert driver.read_sensors() == {"temperature": 25.0, "humidity": None}
    assert "Bedroom AC" in driver.describe()


def test_driver_apply_publishes_writes(driver):
    driver.poll()
    conn = driver._conns[0]
    conn.push({34: 0, 35: 48, 43: 20})  # off, cool, 24
    conn.published.clear()
    driver.apply({"power": True, "mode": "heat", "target_temp": 22})
    writes = [p[1] for p in conn.published if p[0] == "AC1/command"]
    assert writes == [
        {"type": "write", "prm": "[44,10]"},
        {"type": "write", "prm": "[34,4]"},
        {"type": "write", "prm": "[35,44]"},
    ]
    assert conn.published[-1][0] == "$aws/things/AC1/shadow/get"  # refresh afterwards


def test_driver_reports_missing_ac(driver, monkeypatch):
    from acmonitor.drivers import homewhiz as hw

    monkeypatch.setattr(hw, "list_appliances", lambda c: [{"applianceId": "W1", "applianceType": 1, "name": "Washer"}])
    driver.poll()
    assert "No matching Wi-Fi air conditioner" in driver.error and "Washer" in driver.error
    with pytest.raises(DriverError):
        driver.apply({"power": False})
