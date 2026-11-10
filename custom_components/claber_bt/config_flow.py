"""Config flow for Claber BT (passive discovery; optional Bluetooth source).

Default: pick a discovered BLE device (no manual MAC field). Model/title from
advert classification when verifiable. Passive sensors always. Hydro-family
command entities (duration + open lines + stop) are always available.

0.1.14: Controller-4 BT (modulo4) and Elettra-BT (elettrovalvola) unlocked via
the proved myClaber model nibble table; same hydro confirm/options path.
0.1.11: unique_id is always a normalized MAC (AA:BB:...); discovery confirm
copy is kind-specific (Rain vs Hydro-family).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .claber_passive import DecodeError, classify, msd_from_service_info
from .const import (
    CONF_BLUETOOTH_SOURCE,
    CONF_DEVICE_KIND,
    DEVICE_KIND_RAIN,
    DOMAIN,
    MODEL_NAMES,
    SUPPORTED_DEVICE_KINDS,
    is_hydro_family,
    manual_lines_for_kind,
    normalize_mac,
)
from .parser import ClaberBluetoothDeviceData


def _classify_service_info(discovery_info: BluetoothServiceInfoBleak) -> str | None:
    """Classify a discovery advertisement; None if not a Claber family advert."""
    try:
        full = msd_from_service_info(
            getattr(discovery_info, "raw", None),
            discovery_info.manufacturer_data,
        )
    except DecodeError:
        return None
    if full is None:
        return None
    try:
        return classify(full, discovery_info.address, discovery_info.name)
    except DecodeError:
        return None


class ClaberBTConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claber BT."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_kind: str | None = None
        self._discovered_devices: dict[str, tuple[str, BluetoothServiceInfoBleak]] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        address = normalize_mac(discovery_info.address)
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()

        kind = _classify_service_info(discovery_info)
        if kind is None or kind not in SUPPORTED_DEVICE_KINDS:
            return self.async_abort(reason="not_supported")

        self._discovery_info = discovery_info
        self._discovered_kind = kind
        if kind == DEVICE_KIND_RAIN:
            return await self.async_step_bluetooth_confirm_rain()
        return await self.async_step_bluetooth_confirm_hydro()

    async def _async_confirm(
        self, user_input: dict[str, Any] | None, *, step_id: str
    ) -> ConfigFlowResult:
        assert self._discovery_info is not None
        assert self._discovered_kind is not None
        address = normalize_mac(self._discovery_info.address)
        title = self._title(self._discovered_kind, address)

        if user_input is not None:
            return self.async_create_entry(
                title=title,
                data={
                    CONF_DEVICE_KIND: self._discovered_kind,
                    CONF_ADDRESS: address,
                },
            )

        self.context["title_placeholders"] = {"name": title}
        return self.async_show_form(
            step_id=step_id,
            description_placeholders={"name": title},
        )

    async def async_step_bluetooth_confirm_hydro(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_confirm(
            user_input, step_id="bluetooth_confirm_hydro"
        )

    async def async_step_bluetooth_confirm_rain(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_confirm(
            user_input, step_id="bluetooth_confirm_rain"
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = normalize_mac(user_input[CONF_ADDRESS])
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            kind, _info = self._discovered_devices[address]
            return self.async_create_entry(
                title=self._title(kind, address),
                data={CONF_DEVICE_KIND: kind, CONF_ADDRESS: address},
            )

        current_addresses = {
            normalize_mac(x)
            for x in self._async_current_ids(include_ignore=False)
            if x
        }
        for discovery_info in async_discovered_service_info(self.hass, False):
            try:
                address = normalize_mac(discovery_info.address)
            except ValueError:
                continue
            if address in current_addresses or address in self._discovered_devices:
                continue
            kind = _classify_service_info(discovery_info)
            if kind in SUPPORTED_DEVICE_KINDS:
                if ClaberBluetoothDeviceData(kind).supported(discovery_info):
                    self._discovered_devices[address] = (kind, discovery_info)

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{self._title(kind, address)}"
                            for address, (kind, _info) in self._discovered_devices.items()
                        }
                    )
                }
            ),
        )

    @staticmethod
    def _title(kind: str, address: str) -> str:
        return f"{MODEL_NAMES.get(kind, kind)} ({address})"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ClaberBTOptionsFlow:
        return ClaberBTOptionsFlow()


class ClaberBTOptionsFlow(OptionsFlowWithReload):
    """Options: optional preferred Bluetooth source for hydro-family commands."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        kind = self.config_entry.data.get(CONF_DEVICE_KIND)
        if not is_hydro_family(kind):
            return self.async_abort(reason="options_only_hydro")

        if user_input is not None:
            source = str(user_input.get(CONF_BLUETOOTH_SOURCE) or "").strip()
            return self.async_create_entry(
                title="",
                data={**({CONF_BLUETOOTH_SOURCE: source} if source else {})},
            )

        lines = manual_lines_for_kind(kind)
        if lines == (1,):
            scope = "linea 1, durata 1-120 min, stop globale; auth MAC; nessun PIN"
        else:
            scope = "linee 1-4, durata 1-120 min, stop globale; auth MAC; nessun PIN"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_BLUETOOTH_SOURCE,
                        default=self.config_entry.options.get(CONF_BLUETOOTH_SOURCE)
                        or "",
                    ): str,
                }
            ),
            description_placeholders={"scope": scope},
        )
