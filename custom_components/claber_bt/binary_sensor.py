"""Binary sensor platform for Claber BT (passive).

Volatile watering/rain bits are never restored from HA processor storage as
trusted current state. Until a live-confirmed advert decode arrives, moisture /
connectivity entities expose ``unknown`` (is_on=None) instead of a cached ON.

0.1.9: Hydro line_N watering binary sensors removed (replaced by valves).
0.1.10: automatic_watering binary removed (replaced by Auto mode switch).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothDataProcessor,
    PassiveBluetoothDataUpdate,
    PassiveBluetoothEntityKey,
    PassiveBluetoothProcessorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClaberBTConfigEntry
from .const import (
    DOMAIN,
    DEVICE_KIND_RAIN,
    is_hydro_family,
    MANUFACTURER,
    MODEL_NAMES,
    MODEL_NUMBERS,
)
from .parser import ClaberUpdate

_RAIN_BINARY: dict[str, BinarySensorEntityDescription] = {
    "raining": BinarySensorEntityDescription(
        key="raining",
        device_class=BinarySensorDeviceClass.MOISTURE,
        name="Rain",
    ),
    "has_connected": BinarySensorEntityDescription(
        key="has_connected",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        name="Has connected",
    ),
    "has_scanned": BinarySensorEntityDescription(
        key="has_scanned",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        name="Has scanned",
    ),
}

_HYDRO_BINARY: dict[str, BinarySensorEntityDescription] = {
    "rain_sensor_connected": BinarySensorEntityDescription(
        key="rain_sensor_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        name="Rain sensor connected",
    ),
    "raining": BinarySensorEntityDescription(
        key="raining",
        device_class=BinarySensorDeviceClass.MOISTURE,
        name="Raining",
    ),
    "reachable": BinarySensorEntityDescription(
        key="reachable",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        name="Reachable",
    ),
    "battery_warning": BinarySensorEntityDescription(
        key="battery_warning",
        device_class=BinarySensorDeviceClass.BATTERY,
        name="Battery warning",
    ),
}


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


def binary_sensor_update_to_bluetooth_data_update(
    update: ClaberUpdate,
) -> PassiveBluetoothDataUpdate[bool | None]:
    """Convert a ClaberUpdate into binary-sensor PassiveBluetoothDataUpdate."""
    if not update.values_updated:
        return PassiveBluetoothDataUpdate()
    if not update.fields and update.trustworthy:
        return PassiveBluetoothDataUpdate()

    values: dict[str, bool | None] = {}
    descriptions: dict[str, BinarySensorEntityDescription]

    if update.device_kind == DEVICE_KIND_RAIN:
        descriptions = _RAIN_BINARY
        if update.trustworthy:
            values = {
                "raining": bool(update.fields.get("is_raining")),
                "has_connected": bool(update.fields.get("has_connected")),
                "has_scanned": bool(update.fields.get("has_scanned")),
            }
        else:
            values = {
                "raining": None,
                "has_connected": None,
                "has_scanned": None,
            }
    elif is_hydro_family(update.device_kind):
        descriptions = _HYDRO_BINARY
        reachable = True if update.live_confirmed else None
        if update.trustworthy and update.fields:
            values = {
                "rain_sensor_connected": bool(
                    update.fields.get("rain_sensor_connected")
                ),
                "raining": bool(update.fields.get("is_raining")),
                "reachable": reachable if reachable is not None else True,
                "battery_warning": bool(update.fields.get("battery_warning")),
            }
        else:
            values = {
                "rain_sensor_connected": None,
                "raining": None,
                "reachable": reachable,
                "battery_warning": None,
            }
    else:
        return PassiveBluetoothDataUpdate()

    entity_descriptions: dict[
        PassiveBluetoothEntityKey, BinarySensorEntityDescription
    ] = {}
    entity_data: dict[PassiveBluetoothEntityKey, bool | None] = {}
    entity_names: dict[PassiveBluetoothEntityKey, str | None] = {}

    for key, description in descriptions.items():
        if key not in values:
            continue
        entity_key = PassiveBluetoothEntityKey(key, None)
        entity_descriptions[entity_key] = description
        entity_data[entity_key] = values[key]
        entity_names[entity_key] = description.name

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
    """Set up Claber BT binary sensors from a config entry."""
    coordinator = entry.runtime_data
    processor = PassiveBluetoothDataProcessor(
        binary_sensor_update_to_bluetooth_data_update
    )
    processor.restore_key = None
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    store.setdefault("processors", []).append(processor)
    entry.async_on_unload(
        processor.async_add_entities_listener(
            ClaberBluetoothBinarySensorEntity, async_add_entities
        )
    )
    entry.async_on_unload(
        coordinator.async_register_processor(processor, BinarySensorEntityDescription)
    )


class ClaberBluetoothBinarySensorEntity(
    PassiveBluetoothProcessorEntity[
        PassiveBluetoothDataProcessor[bool | None, ClaberUpdate]
    ],
    BinarySensorEntity,
):
    """Representation of a Claber BT binary sensor."""

    @property
    def is_on(self) -> bool | None:
        """Return true/false when live-confirmed; None when provenance unknown."""
        return self.processor.entity_data.get(self.entity_key)

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
