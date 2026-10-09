"""Imports the integration with Home Assistant replaced by minimal stand-ins, so its logic can be
tested without installing Home Assistant. Only `aiohttp`, `cryptography` and `pytz` are needed:

    pip install aiohttp cryptography pytz
    for t in tests/test_*.py; do python3 "$t" || exit 1; done
"""
import importlib
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components"))


class ConfigEntryAuthFailed(Exception):
    pass


class UpdateFailed(Exception):
    pass


class DataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass, self.update_interval, self.data = hass, update_interval, None

    def __class_getitem__(cls, item):
        return cls


def _stub(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


def load():
    """Returns the `smartthings_find` package (its `utils` module is `.utils`)."""
    _stub("homeassistant")
    for name in ("core", "helpers", "helpers.typing", "const", "helpers.aiohttp_client", "helpers.entity",
                 "helpers.device_registry", "helpers.entity_registry", "config_entries", "components",
                 "components.zone", "helpers.event", "helpers.restore_state"):
        _stub("homeassistant." + name, **{k: MagicMock() for k in (
            "HomeAssistant", "ConfigType", "Platform", "async_get_clientsession", "ConfigEntry", "DeviceInfo",
            "callback", "device_registry", "entity_registry", "async_active_zone")})
    _stub("homeassistant.helpers.update_coordinator",
          DataUpdateCoordinator=DataUpdateCoordinator, UpdateFailed=UpdateFailed)
    _stub("homeassistant.exceptions", ConfigEntryAuthFailed=ConfigEntryAuthFailed)
    sys.modules["homeassistant.helpers"].device_registry = MagicMock()
    sys.modules["homeassistant.helpers"].entity_registry = MagicMock()
    package = importlib.import_module("smartthings_find")
    logging.disable(logging.CRITICAL)
    return package


class Checker:
    """Tiny PASS/FAIL reporter shared by the test scripts."""

    def __init__(self):
        self.failed = 0

    def __call__(self, label, got, want):
        ok = got == want
        self.failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got}" + ("" if ok else f"   (wanted {want})"))

    def finish(self):
        print("\nFAILED CHECKS:", self.failed)
        sys.exit(1 if self.failed else 0)
