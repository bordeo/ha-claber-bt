"""Sensor platform for Claber BT (passive).

0.1.13: monotonic irrigation_ends_at + clear cache after Stop ACK.

0.1.10: Rain last_seen timestamp like Hydro.
0.1.9: Hydro remaining watering time is a timestamp (irrigation end), not a
duration in minutes. Program enum uses none/A/B/manual/semi_auto_A/semi_auto_B.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClaberBTConfigEntry
from .const import (
    DOMAIN,
    DEVICE_KIND_HYDRO4,
    DEVICE_KIND_RAIN,
    ENERGY_LEVEL_OPTIONS,
    MANUFACTURER,
    MODEL_NAMES,
    MODEL_NUMBERS,
    OPENING_PROGRAM_OPTIONS,
)
from .parser import ClaberUpdate

_RAIN_SENSORS: dict[str, SensorEntityDescription] = {
    "temperature": SensorEntityDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        name="Temperature",
    ),
    "temperature_max_24h": SensorEntityDescription(
        key="temperature_max_24h",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        name="Max temperature 24h",
    ),
    "temperature_min_24h": SensorEntityDescription(
        key="temperature_min_24h",  # gitleaks:allow  # HA entity key, not a secret
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        name="Min temperature 24h",
    ),
    "battery": SensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        name="Battery",
    ),
    "energy_level": SensorEntityDescription(
        key="energy_level",
        device_class=SensorDeviceClass.ENUM,
        options=ENERGY_LEVEL_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        name="Energy level",
    ),
}

_HYDRO_SENSORS: dict[str, SensorEntityDescription] = {
    "irrigation_ends_at": SensorEntityDescription(
        key="irrigation_ends_at",
        device_class=SensorDeviceClass.TIMESTAMP,
        name="Irrigation ends at",
    ),
    "opening_program": SensorEntityDescription(
        key="opening_program",
        device_class=SensorDeviceClass.ENUM,
        options=OPENING_PROGRAM_OPTIONS,
        name="Active program",
    ),
    "battery": SensorEntityDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        name="Battery",
    ),
    "energy_level": SensorEntityDescription(
        key="energy_level",
        device_class=SensorDeviceClass.ENUM,
        options=ENERGY_LEVEL_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        name="Energy level",
    ),
}

_SIGNAL = SensorEntityDescription(
    key="signal_strength",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=True,
    name="Signal strength",
)

_LAST_SEEN = SensorEntityDescription(
    key="last_seen",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    name="Last seen",
)


def _device_info(update: ClaberUpdate) -> DeviceInfo:
    kind = update.device_kind
    return DeviceInfo(
        name=f"{MODEL_NAMES.get(kind, 'Claber')} {update.address}",
        manufacturer=MANUFACTURER,
        model=MODEL_NAMES.get(kind, kind),
        model_id=MODEL_NUMBERS.get(kind),
        connections={(CONNECTION_BLUETOOTH, update.address)},
        identifiers={("claber_bt", update.address)},
    )


# address -> (remaining_minutes, ends_at UTC). Prevents each advert from
# pushing irrigation_ends_at forward when remaining is unchanged (minute
# bucket), which made the Lovelace countdown look like it "restarted"
# after Stop while the device still advertised remaining > 0.
_irrigation_ends_cache: dict[str, tuple[int, datetime]] = {}


def clear_irrigation_ends_cache(address: str | None) -> None:
    """Drop cached ends_at (after confirmed Stop)."""
    if not address:
        return
    _irrigation_ends_cache.pop((address or "").upper(), None)



def _irrigation_end(address: str, remaining_minutes: Any) -> datetime | None:
    """Map remaining minutes to an absolute UTC end timestamp (monotonic).

    PassiveBLE adverts report remaining as whole minutes. Naively doing
    ``now + remaining`` on every packet pushes irrigation_ends_at forward
    while the minute bucket is unchanged, so a Lovelace countdown looks
    like it "restarts" (including after Stop, until remaining finally
    reaches 0). Keep a per-device cache:

    - remaining <= 0 → clear
    - remaining increases → new/extended run
    - remaining unchanged → keep prior ends_at if still future
    - remaining decreases → ``now + remaining``, capped by prior ends_at
    """
    try:
        mins = int(remaining_minutes)
    except (TypeError, ValueError):
        return None
    key = (address or "").upper()
    if mins <= 0:
        _irrigation_ends_cache.pop(key, None)
        return None
    now = datetime.now(timezone.utc)
    proposed = now + timedelta(minutes=mins)
    prev = _irrigation_ends_cache.get(key)
    if prev is not None:
        prev_mins, prev_end = prev
        if mins == prev_mins and prev_end > now:
            return prev_end
        if mins < prev_mins:
            end = min(proposed, prev_end) if prev_end > now else proposed
            _irrigation_ends_cache[key] = (mins, end)
            return end
    _irrigation_ends_cache[key] = (mins, proposed)
    return proposed


def sensor_update_to_bluetooth_data_update(
    update: ClaberUpdate,
) -> PassiveBluetoothDataUpdate[Any]:
    """Convert a ClaberUpdate into sensor PassiveBluetoothDataUpdate."""
    if not update.values_updated:
        return PassiveBluetoothDataUpdate()

    descriptions: dict[str, SensorEntityDescription]
    if update.device_kind == DEVICE_KIND_RAIN:
        descriptions = _RAIN_SENSORS
    elif update.device_kind == DEVICE_KIND_HYDRO4:
        descriptions = _HYDRO_SENSORS
    else:
        descriptions = {}

    values: dict[str, Any] = {}
    presence_last_seen = False
    if update.trustworthy and update.fields:
        if update.device_kind == DEVICE_KIND_RAIN:
            values = {
                "temperature": update.fields.get("temperature_c"),
                "temperature_max_24h": update.fields.get("temperature_max_24h_c"),
                "temperature_min_24h": update.fields.get("temperature_min_24h_c"),
                "battery": update.fields.get("battery_percent"),
                "energy_level": update.fields.get("energy_level"),
            }
            presence_last_seen = True
        elif update.device_kind == DEVICE_KIND_HYDRO4:
            values = {
                "irrigation_ends_at": _irrigation_end(
                    update.address, update.fields.get("remaining_minutes")
                ),
                "opening_program": update.fields.get("opening_program"),
                "battery": update.fields.get("battery_percent"),
                "energy_level": update.fields.get("energy_level"),
            }
            presence_last_seen = True
    elif not update.trustworthy and not update.fields and descriptions:
        values = {key: None for key in descriptions}
        if update.device_kind == DEVICE_KIND_HYDRO4:
            presence_last_seen = bool(update.live_confirmed)
    elif not update.trustworthy:
        if update.rssi is None and not getattr(update, "presence_only", False):
            return PassiveBluetoothDataUpdate()
        if update.device_kind == DEVICE_KIND_HYDRO4:
            values = {key: None for key in _HYDRO_SENSORS}
            descriptions = dict(_HYDRO_SENSORS)
            presence_last_seen = bool(update.live_confirmed)
        else:
            descriptions = {}

    entity_descriptions: dict[PassiveBluetoothEntityKey, SensorEntityDescription] = {}
    entity_data: dict[PassiveBluetoothEntityKey, Any] = {}
    entity_names: dict[PassiveBluetoothEntityKey, str | None] = {}

    allow_none = (not update.trustworthy and update.device_kind == DEVICE_KIND_HYDRO4) or (
        not update.fields and update.device_kind == DEVICE_KIND_HYDRO4
    )
    for key, description in descriptions.items():
        if key not in values:
            continue
        if values[key] is None and not allow_none:
            continue
        entity_key = PassiveBluetoothEntityKey(key, None)
        entity_descriptions[entity_key] = description
        entity_data[entity_key] = values[key]
        entity_names[entity_key] = description.name

    if update.rssi is not None:
        signal_key = PassiveBluetoothEntityKey("signal_strength", None)
        entity_descriptions[signal_key] = _SIGNAL
        entity_data[signal_key] = update.rssi
        entity_names[signal_key] = _SIGNAL.name

    if presence_last_seen and (update.live_confirmed or update.trustworthy):
        seen_key = PassiveBluetoothEntityKey("last_seen", None)
        entity_descriptions[seen_key] = _LAST_SEEN
        entity_data[seen_key] = datetime.now(timezone.utc)
        entity_names[seen_key] = _LAST_SEEN.name

    return PassiveBluetoothDataUpdate(
        devices={None: _device_info(update)},
        entity_descriptions=entity_descriptions,
        entity_data=entity_data,
        entity_names=entity_names,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaberBTConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Claber BT sensors from a config entry."""
    coordinator = entry.runtime_data
    processor = PassiveBluetoothDataProcessor(sensor_update_to_bluetooth_data_update)
    processor.restore_key = None
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    store.setdefault("processors", []).append(processor)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            ClaberBluetoothSensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, SensorEntityDescription)
    )


class ClaberBluetoothSensorEntity(
    PassiveBluetoothProcessorEntity[PassiveBluetoothDataProcessor[Any, ClaberUpdate]],
    SensorEntity,
):
    """Representation of a Claber BT sensor."""

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Trust/freshness attributes distinct from entity last_updated."""
        attrs: dict[str, Any] = {}
        store: dict[str, Any] | None = None
        for payload in self.hass.data.get(DOMAIN, {}).values():
            if (
                isinstance(payload, dict)
                and payload.get("coordinator") is self.processor.coordinator
            ):
                store = payload
                break
        if not store:
            return attrs
        device_data = store.get("device_data")
        if device_data is None:
            return attrs
        rx = device_data.last_reception
        attrs["claber_live_confirmed"] = rx.live_confirmed
        attrs["claber_trustworthy"] = rx.trustworthy
        attrs["claber_selection"] = rx.selection
        attrs["claber_had_raw"] = rx.had_raw
        attrs["claber_manufacturer_key_count"] = rx.manufacturer_key_count
        if rx.advert_age_seconds is not None:
            attrs["claber_advert_age_seconds"] = round(rx.advert_age_seconds, 3)
        if device_data.last_trustworthy_values_monotonic is not None:
            import time as _time

            attrs["claber_trustworthy_values_age_seconds"] = round(
                max(
                    0.0,
                    _time.monotonic()
                    - device_data.last_trustworthy_values_monotonic,
                ),
                3,
            )
        return attrs

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.processor.entity_data.get(self.entity_key)
