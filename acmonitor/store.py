"""SQLite persistence: presets (learned IR codes), schedules, events, sensor readings."""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS presets (
    name        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    code        BLOB,                 -- learned IR code (IR drivers only)
    is_off      INTEGER NOT NULL DEFAULT 0,
    mode        TEXT,
    target_temp REAL,
    fan         TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    days     TEXT NOT NULL,           -- weekday digits, Monday=0, e.g. "01234"
    start    TEXT NOT NULL,           -- "HH:MM"
    end      TEXT NOT NULL,           -- "HH:MM"; earlier than start = crosses midnight
    preset   TEXT NOT NULL REFERENCES presets(name) ON UPDATE CASCADE,
    enabled  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    source  TEXT NOT NULL,            -- manual | schedule | timer | device
    preset  TEXT NOT NULL,
    is_off  INTEGER NOT NULL,
    ok      INTEGER NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS readings (
    ts          REAL NOT NULL,
    temperature REAL,
    humidity    REAL
);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass
class Preset:
    name: str
    label: str
    is_off: bool
    mode: str | None
    target_temp: float | None
    fan: str | None
    has_code: bool
    created_at: float

    def public(self) -> dict:
        return asdict(self)

    def settings(self) -> dict:
        return {
            "power": not self.is_off,
            "mode": self.mode,
            "target_temp": self.target_temp,
            "fan": self.fan,
        }

    def describe(self) -> str:
        if self.is_off:
            return "Off"
        parts = [p for p in (self.mode, f"{self.target_temp:g}°C" if self.target_temp is not None else None,
                             f"fan {self.fan}" if self.fan else None) if p]
        return " · ".join(parts) or "On"


@dataclass
class Schedule:
    id: int
    name: str
    days: str
    start: str
    end: str
    preset: str
    enabled: bool

    def public(self) -> dict:
        return asdict(self)


def validate_schedule(days: str, start: str, end: str) -> None:
    if not days or any(c not in "0123456" for c in days) or len(set(days)) != len(days):
        raise ValueError("days must be distinct weekday digits 0-6 (Monday=0)")
    for t in (start, end):
        if not TIME_RE.match(t):
            raise ValueError(f"invalid time {t!r}, expected HH:MM")
    if start == end:
        raise ValueError("start and end must differ")


class Store:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock, self._conn:
            return self._conn.execute(sql, args).fetchall()

    # -- presets ----------------------------------------------------------
    def save_preset(
        self,
        name: str,
        label: str,
        code: bytes | None = None,
        is_off: bool = False,
        mode: str | None = None,
        target_temp: float | None = None,
        fan: str | None = None,
    ) -> Preset:
        """Create or update a preset. code=None keeps an existing learned code."""
        if not NAME_RE.match(name):
            raise ValueError("preset name must be 1-40 chars of a-z, 0-9, _ or -")
        if is_off:
            mode = target_temp = fan = None
        self._q(
            """INSERT INTO presets (name, label, code, is_off, mode, target_temp, fan, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET label=excluded.label,
                   code=COALESCE(excluded.code, presets.code), is_off=excluded.is_off,
                   mode=excluded.mode, target_temp=excluded.target_temp, fan=excluded.fan""",
            (name, label or name, code or None, int(is_off), mode or None, target_temp, fan or None, time.time()),
        )
        return self.get_preset(name)  # type: ignore[return-value]

    def get_preset(self, name: str) -> Preset | None:
        rows = self._q("SELECT * FROM presets WHERE name = ?", (name,))
        return _preset(rows[0]) if rows else None

    def get_code(self, name: str) -> bytes | None:
        rows = self._q("SELECT code FROM presets WHERE name = ?", (name,))
        return bytes(rows[0]["code"]) if rows and rows[0]["code"] else None

    def list_presets(self) -> list[Preset]:
        return [_preset(r) for r in self._q("SELECT * FROM presets ORDER BY is_off DESC, name")]

    def off_preset(self) -> Preset | None:
        rows = self._q("SELECT * FROM presets WHERE is_off = 1 ORDER BY name LIMIT 1")
        return _preset(rows[0]) if rows else None

    def delete_preset(self, name: str) -> None:
        if self._q("SELECT 1 FROM schedules WHERE preset = ?", (name,)):
            raise ValueError(f"preset {name!r} is used by a schedule; delete that first")
        self._q("DELETE FROM presets WHERE name = ?", (name,))

    def export_presets(self) -> list[dict]:
        rows = self._q("SELECT * FROM presets ORDER BY name")
        return [
            {
                "name": r["name"],
                "label": r["label"],
                "code": base64.b64encode(bytes(r["code"])).decode() if r["code"] else None,
                "is_off": bool(r["is_off"]),
                "mode": r["mode"],
                "target_temp": r["target_temp"],
                "fan": r["fan"],
            }
            for r in rows
        ]

    def import_presets(self, items: list[dict]) -> int:
        for it in items:
            self.save_preset(
                it["name"],
                it.get("label", it["name"]),
                base64.b64decode(it["code"]) if it.get("code") else None,
                bool(it.get("is_off")),
                it.get("mode"),
                it.get("target_temp"),
                it.get("fan"),
            )
        return len(items)

    # -- schedules --------------------------------------------------------
    def add_schedule(
        self, name: str, days: str, start: str, end: str, preset: str, enabled: bool = True
    ) -> Schedule:
        validate_schedule(days, start, end)
        if self.get_preset(preset) is None:
            raise ValueError(f"unknown preset {preset!r}")
        days = "".join(sorted(days))
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO schedules (name, days, start, end, preset, enabled) VALUES (?,?,?,?,?,?)",
                (name or f"{start}-{end}", days, start, end, preset, int(enabled)),
            )
            sid = cur.lastrowid
        return self.get_schedule(sid)  # type: ignore[arg-type,return-value]

    def update_schedule(self, sid: int, **fields) -> Schedule:
        cur = self.get_schedule(sid)
        if cur is None:
            raise KeyError(sid)
        merged = {**cur.public(), **{k: v for k, v in fields.items() if v is not None}}
        validate_schedule(merged["days"], merged["start"], merged["end"])
        if self.get_preset(merged["preset"]) is None:
            raise ValueError(f"unknown preset {merged['preset']!r}")
        self._q(
            "UPDATE schedules SET name=?, days=?, start=?, end=?, preset=?, enabled=? WHERE id=?",
            (
                merged["name"],
                "".join(sorted(merged["days"])),
                merged["start"],
                merged["end"],
                merged["preset"],
                int(merged["enabled"]),
                sid,
            ),
        )
        return self.get_schedule(sid)  # type: ignore[return-value]

    def get_schedule(self, sid: int) -> Schedule | None:
        rows = self._q("SELECT * FROM schedules WHERE id = ?", (sid,))
        return _schedule(rows[0]) if rows else None

    def list_schedules(self) -> list[Schedule]:
        return [_schedule(r) for r in self._q("SELECT * FROM schedules ORDER BY start, id")]

    def delete_schedule(self, sid: int) -> None:
        self._q("DELETE FROM schedules WHERE id = ?", (sid,))

    # -- events & readings ------------------------------------------------
    def add_event(
        self, source: str, preset: str, is_off: bool, ok: bool, detail: str = "", ts: float | None = None
    ) -> None:
        self._q(
            "INSERT INTO events (ts, source, preset, is_off, ok, detail) VALUES (?,?,?,?,?,?)",
            (ts or time.time(), source, preset, int(is_off), int(ok), detail),
        )

    def events(self, since: float = 0, limit: int = 200) -> list[dict]:
        rows = self._q(
            "SELECT * FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?", (since, limit)
        )
        return [dict(r) | {"is_off": bool(r["is_off"]), "ok": bool(r["ok"])} for r in rows]

    def last_ok_event_before(self, ts: float) -> dict | None:
        rows = self._q(
            "SELECT * FROM events WHERE ok = 1 AND ts < ? ORDER BY ts DESC LIMIT 1", (ts,)
        )
        return dict(rows[0]) if rows else None

    def ok_events_between(self, start: float, end: float) -> list[dict]:
        rows = self._q(
            "SELECT * FROM events WHERE ok = 1 AND ts >= ? AND ts < ? ORDER BY ts", (start, end)
        )
        return [dict(r) for r in rows]

    def add_reading(self, temperature: float | None, humidity: float | None, ts: float | None = None) -> None:
        self._q(
            "INSERT INTO readings (ts, temperature, humidity) VALUES (?,?,?)",
            (ts or time.time(), temperature, humidity),
        )

    def readings(self, since: float) -> list[dict]:
        rows = self._q("SELECT * FROM readings WHERE ts >= ? ORDER BY ts", (since,))
        return [dict(r) for r in rows]

    def latest_reading(self) -> dict | None:
        rows = self._q("SELECT * FROM readings ORDER BY ts DESC LIMIT 1")
        return dict(rows[0]) if rows else None

    def prune(self, older_than: float) -> None:
        self._q("DELETE FROM readings WHERE ts < ?", (older_than,))

    # -- key/value state ---------------------------------------------------
    def kv_get(self, key: str, default=None):
        rows = self._q("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def kv_set(self, key: str, value) -> None:
        self._q(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def _preset(r: sqlite3.Row) -> Preset:
    return Preset(
        name=r["name"],
        label=r["label"],
        is_off=bool(r["is_off"]),
        mode=r["mode"],
        target_temp=r["target_temp"],
        fan=r["fan"],
        has_code=r["code"] is not None,
        created_at=r["created_at"],
    )


def _schedule(r: sqlite3.Row) -> Schedule:
    return Schedule(
        id=r["id"],
        name=r["name"],
        days=r["days"],
        start=r["start"],
        end=r["end"],
        preset=r["preset"],
        enabled=bool(r["enabled"]),
    )
