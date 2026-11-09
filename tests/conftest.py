"""Minimal Home Assistant stubs so claber_bt.sensor can be imported offline."""

from __future__ import annotations

import sys
import types
from enum import Enum
from typing import Any


def _ensure(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    parent_name, _, child = name.rpartition(".")
    if parent_name:
        parent = _ensure(parent_name)
        setattr(parent, child, mod)
    return mod


def _install_stubs() -> None:
    if getattr(sys.modules.get("homeassistant"), "__claber_bt_stub__", False):
        return

    ha = _ensure("homeassistant")
    ha.__claber_bt_stub__ = True

    # --- const / core / helpers ---
    const = _ensure("homeassistant.const")

    class EntityCategory(str, Enum):
        DIAGNOSTIC = "diagnostic"

    class UnitOfTemperature(str, Enum):
        CELSIUS = "°C"

    class Platform(str, Enum):
        BINARY_SENSOR = "binary_sensor"
        SENSOR = "sensor"
        NUMBER = "number"
        BUTTON = "button"
        VALVE = "valve"
        SWITCH = "switch"
        TIME = "time"

    const.PERCENTAGE = "%"
    const.SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
    const.EntityCategory = EntityCategory
    const.UnitOfTemperature = UnitOfTemperature
    const.Platform = Platform
    const.CONF_ADDRESS = "address"

    core = _ensure("homeassistant.core")

    class HomeAssistant:
        pass

    def callback(func):
        return func

    core.HomeAssistant = HomeAssistant
    core.callback = callback
    core.ServiceCall = object

    helpers = _ensure("homeassistant.helpers")
    device_registry = _ensure("homeassistant.helpers.device_registry")
    device_registry.CONNECTION_BLUETOOTH = "bluetooth"

    class DeviceInfo(dict):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(kwargs)
            for key, value in kwargs.items():
                setattr(self, key, value)

    device_registry.DeviceInfo = DeviceInfo

    entity_platform = _ensure("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = object

    cv = _ensure("homeassistant.helpers.config_validation")
    cv.config_entry_only_config_schema = lambda domain: {}
    cv.empty_config_schema = lambda domain: {}

    typing_mod = _ensure("homeassistant.helpers.typing")
    typing_mod.ConfigType = dict

    event = _ensure("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *a, **k: lambda: None

    # --- config entries ---
    config_entries = _ensure("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            return super().__init_subclass__()

    class OptionsFlowWithReload:
        pass

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlowWithReload = OptionsFlowWithReload

    # --- exceptions ---
    exceptions = _ensure("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception

    # --- bluetooth ---
    bt = _ensure("homeassistant.components.bluetooth")

    class BluetoothScanningMode(str, Enum):
        PASSIVE = "passive"
        ACTIVE = "active"

    bt.BluetoothScanningMode = BluetoothScanningMode
    bt.async_last_service_info = lambda *a, **k: None

    pup = _ensure("homeassistant.components.bluetooth.passive_update_processor")

    class _Subscriptable:
        def __class_getitem__(cls, item):
            return cls

    class PassiveBluetoothDataProcessor(_Subscriptable):
        pass

    class PassiveBluetoothDataUpdate(dict):
        def __init__(self, *args, **kwargs):
            super().__init__()
            for key, value in kwargs.items():
                self[key] = value
                setattr(self, key, value)

    class PassiveBluetoothEntityKey:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class PassiveBluetoothProcessorEntity(_Subscriptable):
        pass

    class PassiveBluetoothProcessorCoordinator(_Subscriptable):
        def __init__(self, *args, **kwargs) -> None:
            pass

    pup.PassiveBluetoothDataProcessor = PassiveBluetoothDataProcessor
    pup.PassiveBluetoothDataUpdate = PassiveBluetoothDataUpdate
    pup.PassiveBluetoothEntityKey = PassiveBluetoothEntityKey
    pup.PassiveBluetoothProcessorEntity = PassiveBluetoothProcessorEntity
    pup.PassiveBluetoothProcessorCoordinator = PassiveBluetoothProcessorCoordinator

    # --- sensor ---
    sensor = _ensure("homeassistant.components.sensor")

    class SensorDeviceClass(str, Enum):
        TEMPERATURE = "temperature"
        BATTERY = "battery"
        ENUM = "enum"
        TIMESTAMP = "timestamp"
        SIGNAL_STRENGTH = "signal_strength"

    class SensorStateClass(str, Enum):
        MEASUREMENT = "measurement"

    class SensorEntity:
        pass

    class SensorEntityDescription:
        def __init__(self, **kwargs) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    sensor.SensorEntity = SensorEntity
    sensor.SensorEntityDescription = SensorEntityDescription

    # --- frontend / http (imported by __init__) ---
    frontend = _ensure("homeassistant.components.frontend")
    frontend.add_extra_js_url = lambda *a, **k: None

    http = _ensure("homeassistant.components.http")

    class StaticPathConfig:
        def __init__(self, *args, **kwargs) -> None:
            pass

    http.StaticPathConfig = StaticPathConfig


_install_stubs()
