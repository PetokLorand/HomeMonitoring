import pytest

from acmonitor.config import Config
from acmonitor.controller import Controller
from acmonitor.drivers.ir import MockDriver
from acmonitor.store import Store


class FakeLiveDriver:
    """A live driver whose state the test controls; records applied settings."""

    kind = "fake"
    live_state = True
    needs_code = False

    def __init__(self):
        self.applied = []
        self.state = None

    def start(self): ...
    def stop(self): ...
    def poll(self): ...

    def apply(self, settings, code=None):
        self.applied.append(settings)

    def read_state(self):
        return self.state

    def read_sensors(self):
        return None

    def learn(self, timeout_s):
        raise NotImplementedError

    def options(self):
        return {"modes": ["cool", "heat"], "fan_speeds": [], "temp_min": 16, "temp_max": 30, "temp_step": 1}

    def describe(self):
        return "fake"


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.monitor.db_path = str(tmp_path / "t.db")
    c.monitor.timezone = "UTC"
    c.monitor.sensor_interval_s = 10**9  # don't poll sensors in tests
    return c


@pytest.fixture
def store(cfg):
    return Store(cfg.monitor.db_path)


@pytest.fixture
def ir_ctl(cfg, store):
    store.save_preset("off", "Off", b"OFF", is_off=True)
    store.save_preset("cool24", "Cool 24", b"C24", mode="cool", target_temp=24)
    store.save_preset("heat22", "Heat 22", b"H22", mode="heat", target_temp=22)
    return Controller(cfg, store, MockDriver())


@pytest.fixture
def live_ctl(cfg, store):
    store.save_preset("cool24", "Cool 24", mode="cool", target_temp=24)
    return Controller(cfg, store, FakeLiveDriver())
