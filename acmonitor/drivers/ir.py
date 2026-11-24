"""IR blaster drivers (blind: they cannot read the AC's state back).

The Broadlink driver talks to an RM4 mini / RM4 Pro / RM mini 3 on the LAN.
The mock driver lets you run the whole service without hardware.

An AC remote sends the complete state (power, mode, temperature, fan) with
every press, so one learned code == one complete preset.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time

from . import DriverError

log = logging.getLogger(__name__)

IR_OPTIONS = {
    "modes": ["cool", "heat", "dry", "fan", "auto"],
    "fan_speeds": ["auto", "low", "mid", "high"],
    "temp_min": 16,
    "temp_max": 30,
    "temp_step": 1,
}


class _IRBase:
    live_state = False
    needs_code = True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def poll(self) -> None:
        pass

    def read_state(self) -> dict | None:
        return None

    def options(self) -> dict:
        return IR_OPTIONS

    def apply(self, settings: dict, code: bytes | None) -> None:
        if not code:
            raise DriverError("This preset has no learned IR code yet. Learn it first.")
        self.send(code)

    def send(self, code: bytes) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class MockDriver(_IRBase):
    """Pretends to be an IR blaster. Useful for trying the dashboard."""

    kind = "mock"

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, code: bytes) -> None:
        self.sent.append(code)
        log.info("[mock] sent %d-byte IR code", len(code))

    def learn(self, timeout_s: int) -> bytes:
        log.info("[mock] learned a fake IR code")
        return b"\x26\x00" + os.urandom(64)

    def read_sensors(self) -> dict | None:
        hour = time.localtime().tm_hour
        return {
            "temperature": round(23 + 3 * ((hour - 6) % 24) / 24 + random.uniform(-0.3, 0.3), 1),
            "humidity": round(50 + random.uniform(-3, 3), 1),
        }

    def describe(self) -> str:
        return "Mock IR blaster (no hardware)"


class BroadlinkDriver(_IRBase):
    """Broadlink RM-series IR blaster, reconnecting on failure."""

    kind = "broadlink"

    def __init__(self, host: str = "") -> None:
        self.host = host
        self._dev = None
        self._lock = threading.Lock()

    def _connect(self):
        import broadlink

        if self.host:
            dev = broadlink.hello(self.host, timeout=5)
        else:
            found = [d for d in broadlink.discover(timeout=5) if _is_remote(d)]
            if not found:
                raise DriverError(
                    "No Broadlink IR blaster found on the network. "
                    "Set device.host in the config to its IP address."
                )
            dev = found[0]
            self.host = dev.host[0]
        dev.auth()
        log.info("Connected to %s (%s) at %s", dev.model, dev.type, dev.host[0])
        return dev

    def _call(self, fn):
        """Run fn(device); reconnect and retry once on any error."""
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._dev is None:
                        self._dev = self._connect()
                    return fn(self._dev)
                except DriverError:
                    raise
                except Exception as exc:  # broadlink raises many types
                    self._dev = None
                    if attempt == 2:
                        raise DriverError(f"Broadlink error: {exc}") from exc
                    log.warning("Broadlink call failed (%s); reconnecting", exc)

    def send(self, code: bytes) -> None:
        self._call(lambda d: d.send_data(code))

    def learn(self, timeout_s: int) -> bytes:
        from broadlink.exceptions import ReadError, StorageError

        def _learn(dev):
            dev.enter_learning()
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                time.sleep(1)
                try:
                    return dev.check_data()
                except (ReadError, StorageError):
                    continue  # nothing received yet
            raise DriverError(
                f"No IR signal received within {timeout_s}s. "
                "Point the remote at the blaster from ~10 cm and press the button."
            )

        return self._call(_learn)

    def read_sensors(self) -> dict | None:
        def _read(dev):
            if not hasattr(dev, "check_sensors"):
                return None  # RM mini 3 has no sensors
            data = dev.check_sensors()
            # RM4 mini without the HTS2 cable reports zeros.
            if not data or (data.get("temperature") == 0 and data.get("humidity") == 0):
                return None
            return {"temperature": data["temperature"], "humidity": data.get("humidity")}

        return self._call(_read)

    def describe(self) -> str:
        if self._dev is None:
            return f"Broadlink ({self.host or 'auto-discover'}, not connected yet)"
        return f"Broadlink {self._dev.model} at {self._dev.host[0]}"


def _is_remote(dev) -> bool:
    return hasattr(dev, "send_data") and hasattr(dev, "enter_learning")
