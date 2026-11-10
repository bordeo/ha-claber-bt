"""Number platform — manual duration + program line runtimes + budget."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import ClaberBTConfigEntry
from .const import (
    CONF_DEVICE_KIND,
    DEFAULT_MANUAL_MINUTES,
    DOMAIN,
    MANUAL_MAX_MINUTES,
    MANUAL_MIN_MINUTES,
    MANUFACTURER,
    MODEL_NAMES,
    MODEL_NUMBERS,
    PROGRAM_IDS,
    is_hydro_family,
    manual_lines_for_kind,
)
from .hydro_program_codec import (
    BUDGET_MAX,
    BUDGET_MIN,
    BUDGET_STEP,
    PROGRAM_LINE_MAX,
    PROGRAM_LINE_MIN,
)
from .hydro_program_session import async_apply_program_field
from .manual_once import get_stored_duration, set_stored_duration
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
    kind = entry.data.get(CONF_DEVICE_KIND)
    if not is_hydro_family(kind):
        return
    store = hass.data[DOMAIN][entry.entry_id]
    ps: ProgramStore = store.setdefault("program_store", ProgramStore())
    entities: list[NumberEntity] = [ClaberManualDurationNumber(hass, entry)]
    for prog in PROGRAM_IDS:
        entities.append(ClaberProgramBudgetNumber(hass, entry, ps, prog))
        for line in manual_lines_for_kind(kind):
            entities.append(ClaberProgramLineNumber(hass, entry, ps, prog, line))
    async_add_entities(entities)


class ClaberManualDurationNumber(NumberEntity, RestoreEntity):
    """Duration used by valve OPEN (app UI range 1..120)."""

    _attr_has_entity_name = True
    _attr_name = "Durata manuale"
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = float(MANUAL_MIN_MINUTES)
    _attr_native_max_value = float(MANUAL_MAX_MINUTES)
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX

    def __init__(self, hass: HomeAssistant, entry: ClaberBTConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_manual_duration"
        self._attr_device_info = _device_info(entry)
        self._attr_native_value = float(get_stored_duration(hass, entry.entry_id))

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state not in (None, "unknown", "unavailable"):
            try:
                value = int(float(last.state))
            except (TypeError, ValueError):
                value = DEFAULT_MANUAL_MINUTES
            if MANUAL_MIN_MINUTES <= value <= MANUAL_MAX_MINUTES:
                set_stored_duration(self.hass, self._entry.entry_id, value)
                self._attr_native_value = float(value)

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(value)
        set_stored_duration(self.hass, self._entry.entry_id, minutes)
        self._attr_native_value = float(minutes)
        self.async_write_ha_state()


class _ProgramNumberBase(NumberEntity):
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

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


class ClaberProgramLineNumber(_ProgramNumberBase):
    _attr_native_min_value = float(PROGRAM_LINE_MIN)
    _attr_native_max_value = float(PROGRAM_LINE_MAX)
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = "min"

    def __init__(self, hass, entry, ps, program: str, line: int) -> None:
        super().__init__(hass, entry, ps, program)
        self._line = line
        address = entry.unique_id
        self._attr_unique_id = f"{address}_program_{program}_line_{line}_minutes"
        self._attr_name = f"Programma {program} linea {line}"

    @property
    def native_value(self) -> float | None:
        p = self._ps.get(self._program)
        if p is None:
            return None
        lines = p.get("lines_min") or []
        if self._line - 1 >= len(lines):
            return 0.0
        return float(lines[self._line - 1])

    async def async_set_native_value(self, value: float) -> None:
        await self._apply("line_minutes", (self._line, int(value)))


class ClaberProgramBudgetNumber(_ProgramNumberBase):
    _attr_native_min_value = float(BUDGET_MIN)
    _attr_native_max_value = float(BUDGET_MAX)
    _attr_native_step = float(BUDGET_STEP)
    _attr_native_unit_of_measurement = "%"

    def __init__(self, hass, entry, ps, program: str) -> None:
        super().__init__(hass, entry, ps, program)
        address = entry.unique_id
        self._attr_unique_id = f"{address}_program_{program}_budget"
        self._attr_name = f"Programma {program} budget"

    @property
    def native_value(self) -> float | None:
        p = self._ps.get(self._program)
        if p is None:
            return None
        return float(p.get("budget_pct") or 100)

    async def async_set_native_value(self, value: float) -> None:
        await self._apply("budget", int(value))
