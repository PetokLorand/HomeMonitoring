"""HomeWhiz cloud driver for Arçelik / Beko / Grundig air conditioners.

Talks to the same cloud the HomeWhiz app uses, so the AC must already be
linked to your HomeWhiz account (it stays linked even if the app misbehaves).

Protocol (as implemented by the community home-assistant-HomeWhiz project):
* POST https://api.arcelikiot.com/auth/login -> temporary AWS credentials
* SigV4-signed GETs on smarthome.arcelikiot.com / api.arcelikiot.com list the
  appliances and point at a per-model JSON "configuration" on S3, which maps
  each setting to an index in the appliance's byte array ("wfa").
* AWS IoT over MQTT/websockets: the device shadow carries the byte array in
  state.reported.wfa (shifted by wfaStartOffset); commands are published to
  "<applianceId>/command" as {"type": "write", "prm": "[index,value]"}.

Everything here is pure Python (stdlib + paho-mqtt), so it also runs on a
phone (Termux) or a 32-bit Raspberry Pi.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import DriverError

log = logging.getLogger(__name__)

LOGIN_URL = "https://api.arcelikiot.com/auth/login"
HOMES_URL = "https://smarthome.arcelikiot.com/my-homes"
CONTENTS_URL = "https://api.arcelikiot.com/procam/contents"
S3_URL = "https://s3-eu-west-1.amazonaws.com/procam-contents/{ctype}S/{cid}/v{ver}/{cid}.{lang}.json"
IOT_ENDPOINT = "ajf7v9dcoe69w-ats.iot.eu-west-1.amazonaws.com"
REGION = "eu-west-1"
USER_AGENT = "HomeWhiz/1.0"
AC_APPLIANCE_TYPE = 9
DEFAULT_WFA_OFFSET = 26
FORCE_READ_INTERVAL_S = 60
COMMAND_GAP_S = 0.5


# ---------------------------------------------------------------------------
# Byte-array map, parsed from the per-model configuration JSON (pure, testable)
# ---------------------------------------------------------------------------

def friendly(name: str) -> str:
    name = name.replace("+", "plus").lower()
    return re.sub("[^a-z0-9-_]", "", name).removesuffix("_")


def clamp(v: int) -> int:
    """The appliance sets bit 7 on some values; the HomeWhiz app ignores it."""
    return v if v < 128 else v - 128


MODE_BY_SUFFIX = {
    "cooling": "cool",
    "cool": "cool",
    "heating": "heat",
    "heat": "heat",
    "auto": "auto",
    "dry": "dry",
    "dehumidification": "dry",
    "fan": "fan",
}


@dataclass
class Field:
    read: int
    write: int
    factor: float = 1.0
    options: dict[str, int] = field(default_factory=dict)  # name -> raw value
    lower: float | None = None
    upper: float | None = None
    step: float | None = None
    unit: str | None = None

    def decode_enum(self, buf: list[int]) -> str | None:
        raw = _get(buf, self.read)
        for name, v in self.options.items():
            if v == raw:
                return name
        return None

    def decode_num(self, buf: list[int]) -> float:
        return round(_get(buf, self.read) * self.factor, 2)


def _get(buf: list[int], i: int) -> int:
    return clamp(buf[i]) if 0 <= i < len(buf) else 0


@dataclass
class ACMap:
    power: Field
    mode: Field
    target_temp: Field
    room_temp: Field | None = None
    fan: Field | None = None
    consumption: Field | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "ACMap":
        states = cfg.get("deviceStates") or {}
        read = _first(states.get("wifiArrayReadIndex"), states.get("wfaIndex"))
        write = _first(states.get("wifiArrayWriteIndex"), states.get("wfaIndex"), read)
        opts = {}
        for st in states.get("states", []):
            key = friendly(st["strKey"])
            if key.endswith("_on"):
                opts["on"] = st["wifiArrayValue"]
            elif key.endswith("_off"):
                opts["off"] = st["wifiArrayValue"]
        if read is None or set(opts) != {"on", "off"}:
            raise DriverError("Appliance configuration has no on/off state; is this an AC?")
        power = Field(read=read, write=write, options=opts)

        prog = cfg.get("program") or {}
        if friendly(prog.get("strKey", "")) != "air_conditioner_program":
            raise DriverError("Appliance configuration has no AIR_CONDITIONER_PROGRAM; is this an AC?")
        modes = {}
        for v in prog.get("values", []):
            key = friendly(v["strKey"])
            suffix = key.split("_")[-1]
            modes.setdefault(MODE_BY_SUFFIX.get(suffix, key), v["wifiArrayValue"])
        mode = Field(
            read=prog["wifiArrayIndex"],
            write=_first(prog.get("wfaWriteIndex"), prog["wifiArrayIndex"]),
            options=modes,
        )

        feats = {}
        for section in ("subPrograms", "customSubPrograms", "settings", "commands", "monitorings"):
            for f in cfg.get(section) or []:
                if f.get("strKey"):
                    feats.setdefault(friendly(f["strKey"]), f)

        target = feats.get("air_conditioner_target_temperature")
        if not target:
            raise DriverError("Appliance configuration has no target temperature")
        return cls(
            power=power,
            mode=mode,
            target_temp=_numeric(target),
            room_temp=_numeric(feats["air_conditioner_room_temperature"])
            if "air_conditioner_room_temperature" in feats
            else None,
            fan=_fan(feats["air_conditioner_wind_strength"])
            if "air_conditioner_wind_strength" in feats
            else None,
            consumption=_consumption(feats.get("air_conditioner_instant_consumption")),
        )

    # -- decode ---------------------------------------------------------------
    def decode(self, buf: list[int]) -> dict:
        power = self.power.decode_enum(buf)
        return {
            "power": None if power is None else power == "on",
            "mode": self.mode.decode_enum(buf),
            "target_temp": self.target_temp.decode_num(buf),
            "room_temp": self.room_temp.decode_num(buf) if self.room_temp else None,
            "fan": self.fan.decode_enum(buf) if self.fan else None,
            "consumption": self.consumption.decode_num(buf) if self.consumption else None,
        }

    # -- encode ---------------------------------------------------------------
    def commands(self, settings: dict, current: dict | None = None) -> list[tuple[int, int]]:
        """(index, value) writes that bring the AC to `settings`.

        Power goes first: many units ignore mode/temperature writes while off.
        When `current` is known, unchanged settings are skipped.
        """
        cur = current or {}
        out: list[tuple[int, int]] = []
        if not settings.get("power"):
            return [(self.power.write, self.power.options["off"])]
        if cur.get("power") is not True:
            out.append((self.power.write, self.power.options["on"]))
        mode = settings.get("mode")
        if mode is not None and mode != cur.get("mode"):
            if mode not in self.mode.options:
                raise DriverError(f"Unsupported mode {mode!r}; this AC has {sorted(self.mode.options)}")
            out.append((self.mode.write, self.mode.options[mode]))
        temp = settings.get("target_temp")
        if temp is not None and float(temp) != cur.get("target_temp"):
            t = self.target_temp
            if t.lower is not None and not (t.lower <= float(temp) <= t.upper):
                raise DriverError(f"Target temperature must be between {t.lower:g} and {t.upper:g}")
            out.append((t.write, int(round(float(temp) / t.factor))))
        fan = settings.get("fan")
        if fan is not None and self.fan and fan != cur.get("fan"):
            if fan not in self.fan.options:
                raise DriverError(f"Unsupported fan speed {fan!r}; this AC has {list(self.fan.options)}")
            out.append((self.fan.write, self.fan.options[fan]))
        return out

    def options(self) -> dict:
        t = self.target_temp
        return {
            "modes": list(self.mode.options),
            "fan_speeds": list(self.fan.options) if self.fan else [],
            "temp_min": t.lower,
            "temp_max": t.upper,
            "temp_step": t.step,
        }


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def _write_index(f: dict) -> int:
    return _first(f.get("wfaWriteIndex"), f["wifiArrayIndex"])


def _numeric(f: dict) -> Field:
    b = (f.get("boundedValues") or [{}])[0]
    return Field(
        read=f["wifiArrayIndex"],
        write=_write_index(f),
        factor=b.get("factor") or 1.0,
        lower=b.get("lowerLimit"),
        upper=b.get("upperLimit"),
        step=b.get("step"),
        unit=b.get("unit"),
    )


def _fan(f: dict) -> Field:
    opts: dict[str, int] = {}
    for e in f.get("enumValues") or []:
        if e.get("wifiArrayValue") is None:
            continue
        name = friendly(e["strKey"]).removeprefix("air_conditioner_").removeprefix("wind_strength_")
        opts[name] = e["wifiArrayValue"]
    for b in f.get("boundedValues") or []:
        factor, step = b.get("factor") or 0, b.get("step") or 0
        if factor == 0 or step <= 0:
            continue
        v = b["lowerLimit"]
        while v <= b["upperLimit"] + 1e-9:
            opts.setdefault(f"{v:g}", int(round(v / factor)))
            v += step
    return Field(read=f["wifiArrayIndex"], write=_write_index(f), options=opts)


def _consumption(f: dict | None) -> Field | None:
    if not f:
        return None
    fld = _numeric(f)
    # Known firmware bug (see home-assistant-HomeWhiz): reports factor 1 for 0.1 steps.
    if fld.unit == "hw" and fld.factor == 1:
        fld.factor = 0.1
    return fld


def buffer_from_shadow(payload: bytes | str) -> tuple[list[int], dict] | None:
    """Extract the byte array from an AWS IoT shadow document."""
    try:
        doc = json.loads(payload)
    except (ValueError, TypeError):
        return None
    reported = (doc.get("state") or {}).get("reported") or {}
    wfa = reported.get("wfa")
    if not isinstance(wfa, list):
        return None
    offset = int(reported.get("wfaStartOffset") or DEFAULT_WFA_OFFSET)
    return [0] * offset + [int(x) for x in wfa], reported


# ---------------------------------------------------------------------------
# Cloud REST client
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    access_key: str
    secret_key: str
    session_token: str
    expires_at: float  # epoch seconds


def _http(req: urllib.request.Request, timeout: float = 20) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        raise DriverError(f"HomeWhiz API {exc.code} for {req.full_url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise DriverError(f"Cannot reach {req.full_url}: {exc.reason}") from exc


def login(username: str, password: str) -> Credentials:
    req = urllib.request.Request(
        LOGIN_URL,
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    resp = _http(req)
    if not resp.get("success", True) or "data" not in resp:
        raise DriverError("HomeWhiz login failed; check username and password")
    c = resp["data"]["credentials"]
    exp = int(c["expiration"])
    return Credentials(c["accessKey"], c["secretKey"], c["sessionToken"], exp / 1000 if exp > 1e11 else exp)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign_get(url: str, creds: Credentials, now: dt.datetime | None = None) -> dict:
    """AWS SigV4 headers for a GET to an execute-api endpoint."""
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    u = urllib.parse.urlsplit(url)
    pairs = sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    query = "&".join(
        f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in pairs
    )
    path = urllib.parse.quote(u.path or "/", safe="/-_.~")
    canonical_headers = f"host:{u.netloc}\nx-amz-date:{amz_date}\nx-amz-security-token:{creds.session_token}\n"
    signed_headers = "host;x-amz-date;x-amz-security-token"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical = f"GET\n{path}\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    scope = f"{day}/{REGION}/execute-api/aws4_request"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_key}".encode(), day), REGION), "execute-api"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amz_date,
        "x-amz-security-token": creds.session_token,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={creds.access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def signed_get(url: str, creds: Credentials) -> dict:
    return _http(urllib.request.Request(url, headers=sign_get(url, creds)))


def _find_appliances(node, found: dict) -> None:
    """Collect every object that looks like an appliance, wherever it is nested."""
    if isinstance(node, dict):
        if node.get("applianceId"):
            found.setdefault(node["applianceId"], node)
        for v in node.values():
            _find_appliances(v, found)
    elif isinstance(node, list):
        for v in node:
            _find_appliances(v, found)


def _homes(resp: dict) -> list[dict]:
    data = resp.get("data")
    if isinstance(data, dict):  # tolerate {"data": {"homes": [...]}}
        data = data.get("homes") or data.get("items") or [data]
    return [h for h in data or [] if isinstance(h, dict)]


def list_appliances(creds: Credentials) -> list[dict]:
    out: dict[str, dict] = {}
    homes_resp = signed_get(HOMES_URL, creds)
    _find_appliances(homes_resp, out)  # some accounts list them right here
    for home in _homes(homes_resp):
        hid = home.get("id") or home.get("homeId")
        if hid is None:
            continue
        found: dict[str, dict] = {}
        _find_appliances(signed_get(f"{HOMES_URL}/{hid}", creds), found)
        for aid, a in found.items():
            out[aid] = a | {"homeName": home.get("name")}
    return list(out.values())


def outline(node, depth: int = 0, max_depth: int = 6):
    """Shape of an API response with values elided, for safe troubleshooting."""
    keep = {"name", "applianceType", "connectivity", "brand", "model", "platformType", "success"}
    if depth >= max_depth:
        return "…"
    if isinstance(node, dict):
        return {k: (v if k in keep and not isinstance(v, (dict, list)) else outline(v, depth + 1))
                for k, v in node.items()}
    if isinstance(node, list):
        return [outline(v, depth + 1) for v in node[:5]] + ([f"… {len(node) - 5} more"] if len(node) > 5 else [])
    return type(node).__name__


def debug_account(creds: Credentials) -> dict:
    homes_resp = signed_get(HOMES_URL, creds)
    report = {"my-homes": outline(homes_resp), "homes": []}
    for home in _homes(homes_resp):
        hid = home.get("id") or home.get("homeId")
        if hid is not None:
            report["homes"].append({"name": home.get("name"),
                                    "detail": outline(signed_get(f"{HOMES_URL}/{hid}", creds))})
    return report


def fetch_configuration(creds: Credentials, appliance_id: str, lang: str = "en-GB") -> dict:
    q = urllib.parse.urlencode(
        {"applianceId": appliance_id, "ctype": "CONFIGURATION,LOCALIZATION", "lang": lang, "testMode": "true"}
    )
    results = (signed_get(f"{CONTENTS_URL}?{q}", creds).get("data") or {}).get("results") or []
    conf = next((r for r in results if r.get("ctype") == "CONFIGURATION"), None)
    if conf is None:
        raise DriverError(f"No configuration published for appliance {appliance_id}")
    url = S3_URL.format(ctype=conf["ctype"], cid=conf["cid"], ver=conf["ver"], lang=conf["lang"])
    return _http(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}))


def is_cloud_ac(a: dict) -> bool:
    return a.get("applianceType") == AC_APPLIANCE_TYPE and a.get("connectivity", "BT") not in ("BT", "BASICBT")


# ---------------------------------------------------------------------------
# AWS IoT MQTT-over-websocket transport (paho-mqtt + our own SigV4 presign)
# ---------------------------------------------------------------------------

def presign_iot_path(creds: Credentials, now: dt.datetime | None = None,
                     host: str = IOT_ENDPOINT, region: str = REGION) -> str:
    """Websocket path with a SigV4 query signature, as AWS IoT expects.

    Same as the AWS IoT SDKs: service "iotdevicegateway", only the host header
    signed, and the session token appended *after* signing (IoT does not sign it).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    service = "iotdevicegateway"
    scope = f"{day}/{region}/{service}/aws4_request"
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{creds.access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-SignedHeaders": "host",
    }
    enc = lambda v: urllib.parse.quote(v, safe="-_.~")  # noqa: E731
    query = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    canonical = f"GET\n/mqtt\n{query}\nhost:{host}\n\nhost\n{hashlib.sha256(b'').hexdigest()}"
    to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    path = f"/mqtt?{query}&X-Amz-Signature={signature}"
    if creds.session_token:
        path += f"&X-Amz-Security-Token={enc(creds.session_token)}"
    return path


class IoTConnection:
    """Minimal blocking wrapper around paho-mqtt for AWS IoT over websockets."""

    def __init__(self, creds: Credentials, on_message, client_id: str | None = None,
                 host: str = IOT_ENDPOINT) -> None:
        import ssl

        import paho.mqtt.client as mqtt

        self.creds = creds
        self.host = host
        self._on_message_cb = on_message
        self._connack = threading.Event()
        self._connect_rc = None
        self._acks: dict[int, threading.Event] = {}
        self.connected = False
        c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or uuid.uuid1().hex,
            clean_session=True,
            protocol=mqtt.MQTTv311,
            transport="websockets",
        )
        c.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        c.ws_set_options(path=presign_iot_path(creds, host=host))
        c.reconnect_delay_set(min_delay=2, max_delay=120)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = lambda client, userdata, msg: self._on_message_cb(msg.topic, msg.payload)
        c.on_subscribe = self._on_ack
        self._client = c

    # paho callbacks run on paho's network thread
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._connect_rc = reason_code
        self.connected = not reason_code.is_failure
        if self.connected and getattr(self, "_topics", None):
            for t in self._topics:  # re-subscribe after an automatic reconnect
                client.subscribe(t, qos=1)
        self._connack.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        self.connected = False
        # Signatures are time-bound: give the automatic reconnect a fresh one.
        client.ws_set_options(path=presign_iot_path(self.creds, host=self.host))
        log.warning("HomeWhiz MQTT disconnected (%s)", reason_code)

    def _on_ack(self, client, userdata, mid, *args):
        ev = self._acks.pop(mid, None)
        if ev:
            ev.set()

    def connect(self, timeout: float = 30) -> None:
        self._client.connect(self.host, 443, keepalive=300)
        self._client.loop_start()
        if not self._connack.wait(timeout):
            self.disconnect()
            raise DriverError("Timed out connecting to the HomeWhiz MQTT broker")
        if not self.connected:
            self.disconnect()
            raise DriverError(f"HomeWhiz MQTT broker refused the connection: {self._connect_rc}")

    def subscribe(self, topics: list[str], timeout: float = 15) -> None:
        self._topics = list(topics)
        for t in topics:
            ev = threading.Event()
            rc, mid = self._client.subscribe(t, qos=1)
            self._acks[mid] = ev
            if rc != 0 or not ev.wait(timeout):
                raise DriverError(f"Subscribing to {t} failed")

    def publish(self, topic: str, payload: str, qos: int = 0, timeout: float = 10) -> None:
        info = self._client.publish(topic, payload, qos=qos)
        if qos:
            info.wait_for_publish(timeout)
            if not info.is_published():
                raise DriverError(f"Publish to {topic} was not acknowledged")

    def disconnect(self) -> None:
        try:
            self._client.disconnect()
            self._client.loop_stop()
        except Exception:
            pass
        self.connected = False


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

class HomeWhizDriver:
    kind = "homewhiz"
    live_state = True
    needs_code = False

    def __init__(self, cfg, cache_dir: str | Path = ".", connection_factory=IoTConnection) -> None:
        self.cfg = cfg
        self._make_connection = connection_factory
        self.cache_dir = Path(cache_dir)
        self._lock = threading.RLock()  # connection & commands
        self._state_lock = threading.Lock()  # latest shadow; used by the MQTT thread
        self._creds: Credentials | None = None
        self._conn: IoTConnection | None = None
        self.appliance: dict | None = None
        self.acmap: ACMap | None = None
        self._buf: list[int] | None = None
        self._reported: dict = {}
        self._updated_at = 0.0
        self._last_force_read = 0.0
        self._last_attempt = 0.0
        self.error: str | None = None

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> None:
        pass  # connection happens lazily in poll() so startup never blocks

    def stop(self) -> None:
        with self._lock:
            self._disconnect()

    def poll(self) -> None:
        """Called by the scheduler every tick: (re)connect and refresh state."""
        with self._lock:
            needs_login = self._creds is None or self._creds.expires_at - time.time() < 120
            if needs_login or self._conn is None:
                if time.time() - self._last_attempt < 60:
                    return  # back off between failed attempts
                self._last_attempt = time.time()
                try:
                    self._connect()
                    self.error = None
                except Exception as exc:
                    self.error = str(exc)
                    log.error("HomeWhiz connect failed: %s", exc)
                    self._disconnect()
                    return
            if time.time() - self._last_force_read >= FORCE_READ_INTERVAL_S:
                self._force_read()

    def _connect(self) -> None:
        self._disconnect()
        if not self.cfg.username or not self.cfg.password:
            raise DriverError("Set homewhiz.username and homewhiz.password (or HOMEWHIZ_PASSWORD)")
        self._creds = login(self.cfg.username, self.cfg.password)
        if self.appliance is None or self.acmap is None:
            self._select_appliance()
        conn = self._make_connection(self._creds, self._on_message)
        conn.connect()
        aid = self.appliance["applianceId"]
        conn.subscribe([f"$aws/things/{aid}/shadow/update/accepted", f"$aws/things/{aid}/shadow/get/accepted"])
        self._conn = conn
        log.info("Connected to HomeWhiz cloud for %s", self.describe())
        conn.publish(f"$aws/things/{aid}/shadow/get", "{}")
        self._force_read()

    def _disconnect(self) -> None:
        if self._conn is not None:
            self._conn.disconnect()
        self._conn = None

    def _select_appliance(self) -> None:
        apps = list_appliances(self._creds)
        wanted = self.cfg.appliance_id
        if wanted:
            match = [a for a in apps if a.get("applianceId") == wanted]
        else:
            match = [a for a in apps if is_cloud_ac(a)]
        if not match:
            names = ", ".join(f"{a.get('name')} ({a.get('applianceId')}, type {a.get('applianceType')})" for a in apps)
            raise DriverError(
                "No matching Wi-Fi air conditioner in this HomeWhiz account. "
                f"Appliances found: {names or 'none'}"
            )
        self.appliance = match[0]
        self.acmap = ACMap.from_config(self._load_configuration(self.appliance["applianceId"]))

    def _load_configuration(self, appliance_id: str) -> dict:
        cache = self.cache_dir / f"homewhiz-{appliance_id}.json"
        try:
            conf = fetch_configuration(self._creds, appliance_id, self.cfg.lang)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(conf))
            return conf
        except DriverError:
            if cache.exists():
                log.warning("Using cached appliance configuration %s", cache)
                return json.loads(cache.read_text())
            raise

    # -- MQTT callback (runs on the MQTT network thread) -------------------------
    def _on_message(self, topic, payload) -> None:
        parsed = buffer_from_shadow(payload)
        if parsed is None:
            return
        buf, reported = parsed
        with self._state_lock:
            self._buf, self._reported, self._updated_at = buf, reported, time.time()

    # -- operations -------------------------------------------------------------
    def _force_read(self) -> None:
        if self._conn is None or self.appliance is None:
            return
        aid = self.appliance["applianceId"]
        if aid.startswith("T"):  # Tuya-based modules
            topic, msg = f"$aws/things/{aid}/shadow/get", {"type": "fread/tuyacommand", "applianceId": aid}
        else:
            topic, msg = f"$aws/things/{aid}/shadow/get", {"type": "fread/command"}
        self._conn.publish(topic, json.dumps(msg))
        self._last_force_read = time.time()

    def apply(self, settings: dict, code: bytes | None = None) -> None:
        with self._lock:
            if self._conn is None or self.acmap is None:
                self._last_attempt = 0  # allow an immediate reconnect attempt
                self.poll()
            if self._conn is None or self.acmap is None:
                raise DriverError(f"Not connected to HomeWhiz cloud: {self.error or 'connecting'}")
            current = self.read_state()
            if current and time.time() - current["updated_at"] > 300:
                current = None  # stale: send every setting rather than trusting it
            cmds = self.acmap.commands(settings, current)
            aid = self.appliance["applianceId"]
            tuya = aid.startswith("T")
            topic = f"{aid}/tuyacommand" if tuya else f"{aid}/command"
            for i, (index, value) in enumerate(cmds):
                if i:
                    time.sleep(COMMAND_GAP_S)
                msg = {"type": "write", "prm": f"[{index},{value}]"}
                if tuya:
                    msg["applianceId"] = aid
                try:
                    self._conn.publish(topic, json.dumps(msg), qos=1)
                except DriverError:
                    raise
                except Exception as exc:
                    raise DriverError(f"Publishing command failed: {exc}") from exc
                log.info("HomeWhiz write [%d,%d]", index, value)
            time.sleep(COMMAND_GAP_S)
            self._force_read()

    def read_state(self) -> dict | None:
        with self._state_lock:
            buf, reported, updated = self._buf, self._reported, self._updated_at
        if buf is None or self.acmap is None:
            return None
        state = self.acmap.decode(buf)
        state["online"] = bool(reported.get("connected", True))
        state["updated_at"] = updated
        return state

    def read_sensors(self) -> dict | None:
        s = self.read_state()
        if not s or s.get("room_temp") is None or time.time() - s["updated_at"] > 600:
            return None
        return {"temperature": s["room_temp"], "humidity": None}

    def learn(self, timeout_s: int) -> bytes:
        raise DriverError("The HomeWhiz driver does not learn IR codes; create presets from settings instead")

    def options(self) -> dict:
        if self.acmap is None:
            return {"modes": [], "fan_speeds": [], "temp_min": 16, "temp_max": 30, "temp_step": 1}
        return self.acmap.options()

    def describe(self) -> str:
        if self.appliance is None:
            return "HomeWhiz cloud (" + ("error: " + self.error if self.error else "connecting…") + ")"
        a = self.appliance
        status = "connected" if self._conn is not None and self._conn.connected else f"disconnected{': ' + self.error if self.error else ''}"
        return f"HomeWhiz: {a.get('name') or 'AC'} {a.get('model', '')} ({a.get('applianceId')}) – {status}"
