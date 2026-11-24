"""AC drivers.

A driver turns "settings" into something the AC understands:

    settings = {"power": bool, "mode": str|None, "target_temp": float|None, "fan": str|None}

Drivers come in two flavours:
* live drivers (HomeWhiz cloud) can read the AC's real state back;
* blind drivers (IR blasters) can only replay learned codes, so the
  controller has to remember what it last sent.
"""

from __future__ import annotations

from typing import Protocol


class DriverError(RuntimeError):
    pass


class Driver(Protocol):
    kind: str
    live_state: bool  # True if read_state() returns the AC's real state
    needs_code: bool  # True if presets must carry a learned IR code

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def poll(self) -> None: ...
    def apply(self, settings: dict, code: bytes | None) -> None: ...
    def read_state(self) -> dict | None: ...
    def read_sensors(self) -> dict | None: ...
    def learn(self, timeout_s: int) -> bytes: ...
    def options(self) -> dict: ...
    def describe(self) -> str: ...


def make_driver(cfg) -> Driver:
    kind = cfg.device.backend
    if kind == "homewhiz":
        from .homewhiz import HomeWhizDriver

        return HomeWhizDriver(cfg.homewhiz, cache_dir=cfg.cache_dir)
    from .ir import BroadlinkDriver, MockDriver

    if kind == "mock":
        return MockDriver()
    return BroadlinkDriver(cfg.device.host)
