"""Button platform — Stop, Leggi programmi, Avvia ora Programma A/B."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
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
    is_hydro_family,
)
from .hydro_program_session import async_read_programs
from .manual_once import execute_command

_STOP = ButtonEntityDescription(
    key="stop",
    name="Stop",
    icon="mdi:water-pump-off",
)
_READ_PROGRAMS = ButtonEntityDescription(
    key="read_programs",
    name="Leggi programmi",
    icon="mdi:book-open-page-variant",
)
_START_PROG_A = ButtonEntityDescription(
    key="start_program_a_now",
    name="Avvia ora Programma A",
    icon="mdi:play",
)
_START_PROG_B = ButtonEntityDescription(
    key="start_program_b_now",
    name="Avvia ora Programma B",
    icon="mdi:play",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClaberBTConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not is_hydro_family(entry.data.get(CONF_DEVICE_KIND)):
        return
    async_add_entities(
        [
            ClaberStopButton(hass, entry, _STOP),
            ClaberReadProgramsButton(hass, entry, _READ_PROGRAMS),
            ClaberStartProgramButton(hass, entry, _START_PROG_A, "A"),
            ClaberStartProgramButton(hass, entry, _START_PROG_B, "B"),
        ]
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


class ClaberStopButton(ButtonEntity):
    """Global manual stop."""

    _attr_has_entity_name = True

    def __init__(self, hass, entry, description: ButtonEntityDescription) -> None:
        self.hass = hass
        self._entry = entry
        self.entity_description = description
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        result = await execute_command(
            self.hass, entry_id=self._entry.entry_id, stop_only=True
        )
        if not result.get("success"):
            from logging import getLogger

            getLogger(__name__).warning("claber_bt stop failed: %s", result)


class ClaberReadProgramsButton(ButtonEntity):
    """Single GATT program read (0B). No periodic/boot reads."""

    _attr_has_entity_name = True

    def __init__(self, hass, entry, description: ButtonEntityDescription) -> None:
        self.hass = hass
        self._entry = entry
        self.entity_description = description
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        result = await async_read_programs(self.hass, self._entry.entry_id)
        if not result.get("success"):
            raise HomeAssistantError(
                f"Claber lettura programmi fallita ({result.get('error')})"
            )


class ClaberStartProgramButton(ButtonEntity):
    """Start program A/B now (Cmd 0C). Opens valves immediately."""

    _attr_has_entity_name = True

    def __init__(
        self, hass, entry, description: ButtonEntityDescription, program: str
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._program = program
        self.entity_description = description
        address = entry.unique_id
        assert address is not None
        self._attr_unique_id = f"{address}_{description.key}"
        self._attr_device_info = _device_info(entry)

    async def async_press(self) -> None:
        result = await execute_command(
            self.hass, entry_id=self._entry.entry_id, program=self._program
        )
        if not result.get("success"):
            raise HomeAssistantError(
                f"Claber avvio programma {self._program} fallito ({result.get('error')})"
            )
