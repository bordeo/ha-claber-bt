"""Valve platform — Hydro lines 1..4 as water valves (OPEN/CLOSE).

OPEN uses the shared duration number (1..120 min) via the proven manual_once
GATT path. CLOSE maps to global stop (only RE-proved stop command). State comes
from advertisement line-watering bits (passive).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClaberBTConfigEntry
from .const import (
    CONF_DEVICE_KIND,
    DEVICE_KIND_HYDRO4,
    DOMAIN,
    MANUAL_LINES,
    MANUFACTURER,
    MODEL_NAMES,
    MODEL_NUMBERS,
)
from .manual_once import execute_command
from .parser import ClaberUpdate

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaberBTConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_KIND) != DEVICE_KIND_HYDRO4:
        return
    async_add_entities(
        [ClaberLineValve(hass, entry, line) for line in MANUAL_LINES]
    )


def _device_info(entry: ClaberBTConfigEntry) -> DeviceInfo:
    address = entry.unique_id
    assert address is not None
    kind = entry.data[CONF_DEVICE_KIND]
    return DeviceInfo(
        name=f"{MODEL_NAMES.get(kind, 'Claber')} {address}",
        manufacturer=MANUFACTURER,
        model=MODEL_NAMES.get(kind, kind),
        model_id=MODEL_NUMBERS.get(kind),
        connections={(CONNECTION_BLUETOOTH, address)},
        identifiers={(DOMAIN, address)},
    )


class ClaberLineValve(ValveEntity):
    """One Hydro irrigation line as a water valve."""

    _attr_has_entity_name = True
    _attr_device_class = ValveDeviceClass.WATER
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_reports_position = False

    def __init__(
        self, hass: HomeAssistant, entry: ClaberBTConfigEntry, line: int
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._line = line
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_line_{line}_valve"
        self._attr_name = f"Linea {line}"
        self._attr_device_info = _device_info(entry)
        self._attr_is_closed: bool | None = None
        self._unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        store = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        listeners: list = store.setdefault("state_listeners", [])

        @callback
        def _on_update(update: ClaberUpdate) -> None:
            if update.device_kind != DEVICE_KIND_HYDRO4:
                return
            if not update.trustworthy or not update.fields:
                # Keep last known closed/open; unknown only before first live.
                return
            lines = update.fields.get("lines_watering") or [False, False, False, False]
            idx = self._line - 1
            watering = bool(lines[idx]) if idx < len(lines) else False
            self._attr_is_closed = not watering
            self.async_write_ha_state()

        listeners.append(_on_update)

        def _remove() -> None:
            try:
                listeners.remove(_on_update)
            except ValueError:
                pass

        self._unsub = _remove
        # Seed from last trustworthy update if any.
        device_data = store.get("device_data")
        last = getattr(device_data, "last_trustworthy_update", None) if device_data else None
        if last is not None:
            _on_update(last)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        await super().async_will_remove_from_hass()

    async def async_open_valve(self) -> None:
        result = await execute_command(
            self.hass,
            entry_id=self._entry.entry_id,
            line=self._line,
            minutes=None,
            stop_only=False,
        )
        if not result.get("success"):
            _LOGGER.warning(
                "claber_bt open valve line %s failed: %s", self._line, result
            )

    async def async_close_valve(self) -> None:
        # Only RE-proved stop is global stop (same as Stop button).
        result = await execute_command(
            self.hass,
            entry_id=self._entry.entry_id,
            stop_only=True,
        )
        if not result.get("success"):
            _LOGGER.warning(
                "claber_bt close valve line %s (global stop) failed: %s",
                self._line,
                result,
            )
