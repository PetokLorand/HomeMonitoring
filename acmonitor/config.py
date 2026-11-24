"""Configuration loaded from a TOML file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo


BACKENDS = ("homewhiz", "broadlink", "mock")


@dataclass
class DeviceConfig:
    backend: str = "mock"  # "homewhiz", "broadlink" or "mock"
    host: str = ""  # Broadlink only: IP of the blaster; empty = auto-discover
    learn_timeout_s: int = 30


@dataclass
class HomeWhizConfig:
    username: str = ""  # your HomeWhiz account e-mail
    password: str = ""  # or set the HOMEWHIZ_PASSWORD environment variable
    appliance_id: str = ""  # empty = first Wi-Fi air conditioner in the account
    lang: str = "en-GB"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class MonitorConfig:
    db_path: str = "acmonitor.db"
    timezone: str = ""  # IANA name, e.g. "Europe/Istanbul"; empty = system local
    sensor_interval_s: int = 300
    rated_power_kw: float = 1.0  # average draw while running, for energy estimates
    tick_s: int = 20  # how often the scheduler checks windows and timers


@dataclass
class Config:
    device: DeviceConfig = field(default_factory=DeviceConfig)
    homewhiz: HomeWhizConfig = field(default_factory=HomeWhizConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)

    @property
    def tz(self) -> tzinfo | None:
        return ZoneInfo(self.monitor.timezone) if self.monitor.timezone else None

    @property
    def cache_dir(self) -> str:
        return str(Path(self.monitor.db_path).resolve().parent)


def _build(cls, data: dict):
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path | None) -> Config:
    if path is None:
        return _apply_env(Config())
    if not Path(path).exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    cfg = Config(
        device=_build(DeviceConfig, raw.get("device", {})),
        homewhiz=_build(HomeWhizConfig, raw.get("homewhiz", {})),
        server=_build(ServerConfig, raw.get("server", {})),
        monitor=_build(MonitorConfig, raw.get("monitor", {})),
    )
    # Resolve a relative db path against the config file's directory.
    db = Path(cfg.monitor.db_path)
    if not db.is_absolute():
        cfg.monitor.db_path = str(Path(path).resolve().parent / db)
    if cfg.device.backend not in BACKENDS:
        raise ValueError(f"device.backend must be one of {', '.join(BACKENDS)}")
    cfg.tz  # validate timezone early
    return _apply_env(cfg)


def _apply_env(cfg: Config) -> Config:
    """Secrets may come from the environment instead of the config file."""
    cfg.homewhiz.username = os.environ.get("HOMEWHIZ_USERNAME", cfg.homewhiz.username)
    cfg.homewhiz.password = os.environ.get("HOMEWHIZ_PASSWORD", cfg.homewhiz.password)
    return cfg
