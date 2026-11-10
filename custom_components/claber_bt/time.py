"""Time platform — program A/B start slots 1..4 (hour:minute).

Disabled slots are FFFF on device; enable/disable is a companion switch.
While the slot switch is off, the time entity stays available for editing the
cached clock value used when re-enabling.
"""

from __future__ import annotations

from datetime import time as dt_time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import ClaberBTConfigEntry
from .const import (
    CONF_DEVICE_KIND,
    DOMAIN,
    MANUFACTURER,
    MODEL_NAMES,
    MODEL_NUMBERS,
    PROGRAM_IDS,
    START_SLOTS,
    is_hydro_family,
)
from .hydro_program_session import async_apply_program_field
from .program_store import ProgramStore


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaberBTConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not is_hydro_family(entry.data.get(CONF_DEVICE_KIND)):
        return
    store = hass.data[DOMAIN][entry.entry_id]
    ps: ProgramStore = store.setdefault("program_store", ProgramStore())
    entities = [
        ClaberProgramStartTime(hass, entry, ps, prog, slot)
        for prog in PROGRAM_IDS
        for slot in START_SLOTS
    ]
    async_add_entities(entities)


class ClaberProgramStartTime(TimeEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaberBTConfigEntry,
        ps: ProgramStore,
        program: str,
        slot: int,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._ps = ps
        self._program = program
        self._slot = slot
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_program_{program}_start_{slot}"
        self._attr_name = f"Programma {program} orario {slot}"
        self._attr_device_info = _device_info(entry)
        self._draft: dt_time | None = None
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub = self._ps.add_listener(self._on_store)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
        await super().async_will_remove_from_hass()

    @callback
    def _on_store(self) -> None:
        self._draft = None
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._ps.loaded

    @property
    def native_value(self) -> dt_time | None:
        if self._draft is not None:
            return self._draft
        p = self._ps.get(self._program)
        if p is None:
            return None
        starts = p.get("starts") or []
        hm = starts[self._slot - 1] if self._slot - 1 < len(starts) else None
        if hm is None:
            return None
        return dt_time(hour=hm[0], minute=hm[1])

    async def async_set_value(self, value: dt_time) -> None:
        self._draft = value
        # Writing a time implies enabling the slot with that clock
        result = await async_apply_program_field(
            self.hass,
            self._entry.entry_id,
            program=self._program,
            field="start",
            value=(self._slot, (value.hour, value.minute)),
        )
        if not result.get("success"):
            detail = result.get("validation_error") or result.get("error") or "write_failed"
            raise HomeAssistantError(
                f"Claber programma {self._program} orario {self._slot}: "
                f"operazione rifiutata ({detail})"
            )
