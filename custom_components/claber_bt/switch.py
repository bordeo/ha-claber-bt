"""Switch platform — Auto mode + program enable + weekdays + start-slot enable.

Days use 7 switches (not a select): HA has no multi-select entity; one switch
per weekday maps 1:1 to the PROVED bitmask and stays clear in the UI.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    WEEKDAY_KEYS,
    WEEKDAY_LABELS_IT,
    START_SLOTS,
    is_hydro_family,
)
from .hydro_program_session import async_apply_program_field, async_set_auto_mode
from .parser import ClaberUpdate
from .program_store import ProgramStore

_LOGGER = logging.getLogger(__name__)


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
    entities: list[SwitchEntity] = [ClaberAutoModeSwitch(hass, entry)]
    for prog in PROGRAM_IDS:
        entities.append(ClaberProgramEnabledSwitch(hass, entry, ps, prog))
        for day in WEEKDAY_KEYS:
            entities.append(ClaberProgramDaySwitch(hass, entry, ps, prog, day))
        for slot in START_SLOTS:
            entities.append(ClaberProgramStartEnableSwitch(hass, entry, ps, prog, slot))
    async_add_entities(entities)


class _ProgramEntityBase(SwitchEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClaberBTConfigEntry,
        ps: ProgramStore,
        program: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._ps = ps
        self._program = program
        self._attr_device_info = _device_info(entry)
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
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._ps.loaded

    def _prog(self) -> dict[str, Any] | None:
        return self._ps.get(self._program)

    async def _apply(self, field: str, value: Any) -> None:
        result = await async_apply_program_field(
            self.hass,
            self._entry.entry_id,
            program=self._program,
            field=field,
            value=value,
        )
        if not result.get("success"):
            detail = result.get("validation_error") or result.get("error") or "write_failed"
            raise HomeAssistantError(
                f"Claber programma {self._program}: operazione rifiutata ({detail})"
            )


class ClaberProgramEnabledSwitch(_ProgramEntityBase):
    def __init__(self, hass, entry, ps, program: str) -> None:
        super().__init__(hass, entry, ps, program)
        address = entry.unique_id
        self._attr_unique_id = f"{address}_program_{program}_enabled"
        self._attr_name = f"Programma {program} abilitato"

    @property
    def is_on(self) -> bool | None:
        p = self._prog()
        return None if p is None else bool(p.get("enabled"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply("enabled", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply("enabled", False)


class ClaberProgramDaySwitch(_ProgramEntityBase):
    def __init__(self, hass, entry, ps, program: str, day: str) -> None:
        super().__init__(hass, entry, ps, program)
        self._day = day
        address = entry.unique_id
        self._attr_unique_id = f"{address}_program_{program}_day_{day}"
        self._attr_name = f"Programma {program} {WEEKDAY_LABELS_IT[day]}"

    @property
    def is_on(self) -> bool | None:
        p = self._prog()
        if p is None:
            return None
        from .hydro_program_codec import WEEKDAY_BITS

        return bool(p.get("weekdays", 0) & WEEKDAY_BITS[self._day])

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._apply("weekday_bit", (self._day, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply("weekday_bit", (self._day, False))


class ClaberProgramStartEnableSwitch(_ProgramEntityBase):
    """On = slot has a time; Off = FFFF empty slot."""

    def __init__(self, hass, entry, ps, program: str, slot: int) -> None:
        super().__init__(hass, entry, ps, program)
        self._slot = slot
        address = entry.unique_id
        self._attr_unique_id = f"{address}_program_{program}_start_{slot}_enabled"
        self._attr_name = f"Programma {program} orario {slot} attivo"

    @property
    def is_on(self) -> bool | None:
        p = self._prog()
        if p is None:
            return None
        starts = p.get("starts") or []
        if self._slot - 1 >= len(starts):
            return False
        return starts[self._slot - 1] is not None

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Enable with existing time entity value or default 06:00
        p = self._prog() or {}
        starts = list(p.get("starts") or [None, None, None, None])
        current = starts[self._slot - 1]
        hm = current if current is not None else (6, 0)
        await self._apply("start", (self._slot, hm))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._apply("start", (self._slot, None))


class ClaberAutoModeSwitch(SwitchEntity):
    """Global automatic watering (cmd 05). State from advert b[5] bit0."""

    _attr_has_entity_name = True
    _attr_name = "Auto mode"

    def __init__(self, hass: HomeAssistant, entry: ClaberBTConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_auto_mode"
        self._attr_device_info = _device_info(entry)
        self._is_on: bool | None = None
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        store = self.hass.data[DOMAIN][self._entry.entry_id]
        listeners = store.setdefault("state_listeners", [])

        @callback
        def _on_update(update: ClaberUpdate) -> None:
            if not is_hydro_family(update.device_kind):
                return
            if update.trustworthy and update.fields:
                self._is_on = bool(update.fields.get("automatic_watering_enabled"))
                self.async_write_ha_state()

        listeners.append(_on_update)

        def _remove() -> None:
            try:
                listeners.remove(_on_update)
            except ValueError:
                pass

        self._unsub = _remove
        dd = store.get("device_data")
        last = getattr(dd, "last_trustworthy_update", None) if dd else None
        if last is not None:
            _on_update(last)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool | None:
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        result = await async_set_auto_mode(self.hass, self._entry.entry_id, on=True)
        if not result.get("success"):
            raise HomeAssistantError(
                f"Claber Auto mode ON fallito ({result.get('error')})"
            )
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        result = await async_set_auto_mode(self.hass, self._entry.entry_id, on=False)
        if not result.get("success"):
            raise HomeAssistantError(
                f"Claber Auto mode OFF fallito ({result.get('error')})"
            )
        self._is_on = False
        self.async_write_ha_state()
