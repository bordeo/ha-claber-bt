"""Claber Hydro-4 BT / Rain Sensor BT — Home Assistant custom integration.

Passive BLE advertisement sensors always.
Hydro-4 also exposes valve/number/button command entities.
GATT only on explicit entity/service press.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from datetime import timedelta

from homeassistant.components.bluetooth import BluetoothScanningMode
from homeassistant.components.bluetooth.passive_update_processor import (
    PassiveBluetoothProcessorCoordinator,
)
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_KIND,
    DEFAULT_MANUAL_MINUTES,
    DEVICE_KIND_HYDRO4,
    DOMAIN,
    VERSION,
    normalize_mac,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
from .entity_migration import async_migrate_entity_registry
from .loop_safety import push_processors, run_on_hass_loop
from .parser import ClaberBluetoothDeviceData, ClaberUpdate

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.VALVE,
    Platform.SWITCH,
    Platform.TIME,
]

type ClaberBTConfigEntry = ConfigEntry[PassiveBluetoothProcessorCoordinator[ClaberUpdate]]

_FRONTEND_REGISTERED = False


def _mac_from_entry(entry: ConfigEntry) -> str | None:
    """Best-effort MAC for entries that predate normalized unique_id."""
    for candidate in (entry.unique_id, entry.data.get(CONF_ADDRESS)):
        if candidate:
            try:
                return normalize_mac(str(candidate))
            except ValueError:
                pass
    m = re.search(
        r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})",
        entry.title or "",
    )
    if m:
        try:
            return normalize_mac(m.group(1))
        except ValueError:
            return None
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Claber BT integration (registers Lovelace card once)."""
    await _async_register_frontend(hass)
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the Lovelace card and register it as an extra JS module."""
    global _FRONTEND_REGISTERED
    if _FRONTEND_REGISTERED:
        return

    www = Path(__file__).parent / "www"
    url_path = f"/{DOMAIN}"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                url_path,
                str(www),
                cache_headers=False,
            )
        ]
    )
    # Cache-bust by integration version so HACS updates pick up the new card.
    add_extra_js_url(hass, f"{url_path}/claber-card.js?v={VERSION}")
    _FRONTEND_REGISTERED = True
    _LOGGER.debug("Registered Claber card at %s/claber-card.js?v=%s", url_path, VERSION)


async def async_setup_entry(hass: HomeAssistant, entry: ClaberBTConfigEntry) -> bool:
    """Set up one Claber device from a config entry."""
    await _async_register_frontend(hass)

    address = _mac_from_entry(entry)
    if address is None:
        _LOGGER.error(
            "claber_bt entry %s has no MAC unique_id/address (title=%r)",
            entry.entry_id,
            entry.title,
        )
        return False

    updates: dict = {}
    if entry.unique_id != address:
        updates["unique_id"] = address
    new_data = dict(entry.data)
    if new_data.get(CONF_ADDRESS) != address:
        new_data[CONF_ADDRESS] = address
        updates["data"] = new_data
    if updates:
        hass.config_entries.async_update_entry(entry, **updates)
        _LOGGER.info(
            "claber_bt assigned unique_id=%s on entry %s",
            address,
            entry.entry_id,
        )

    device_kind = entry.data[CONF_DEVICE_KIND]
    device_data = ClaberBluetoothDeviceData(device_kind)

    def _update_method(service_info) -> ClaberUpdate:
        return device_data.update(service_info)

    # connectable=False: receive ads from non-connectable scanner paths
    # (ESPHome proxy PASSIVE often reports connectable=False even for ADV_IND).
    coordinator = PassiveBluetoothProcessorCoordinator(
        hass,
        _LOGGER,
        address=entry.unique_id,
        mode=BluetoothScanningMode.PASSIVE,
        update_method=_update_method,
        connectable=False,
    )
    entry.runtime_data = coordinator
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    from .program_store import ProgramStore

    store.update({
        "device_data": device_data,
        "coordinator": coordinator,
        "entry": entry,
        "processors": store.get("processors") or [],
        "state_listeners": store.get("state_listeners") or [],
        "manual_duration_minutes": store.get(
            "manual_duration_minutes", DEFAULT_MANUAL_MINUTES
        ),
        "program_store": store.get("program_store") or ProgramStore(),
    })

    await async_migrate_entity_registry(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(coordinator.async_start())

    # Cache reconcile for BOTH Hydro and Rain. ESPHome proxy callbacks often
    # omit raw while async_last_service_info still has it; without reconcile
    # Rain stays selection=ambiguous among merged company-id keys (temp lives
    # in the company-id bytes, same class of bug as Hydro).
    from homeassistant.components import bluetooth as ha_bt

    @callback
    def _push(update: ClaberUpdate) -> None:
        """Must run on the HA event loop only."""
        from .optimistic_state import patch_update_fields

        patch_update_fields(hass, entry.entry_id, update)
        push_processors(store, update)
        for listener in list(store.get("state_listeners") or []):
            try:
                listener(update)
            except Exception:  # noqa: BLE001 — never break the push path
                _LOGGER.exception("claber_bt state listener failed")

    def _reconcile_body() -> None:
        address = entry.unique_id
        if not address:
            return
        si = ha_bt.async_last_service_info(hass, address, connectable=True)
        if si is None:
            si = ha_bt.async_last_service_info(hass, address, connectable=False)
        if si is None or not getattr(si, "raw", None):
            # Still try manufacturer_data-only (Rain may unlock via novel key).
            if si is None:
                return
        last = device_data.last_trustworthy_update
        adv_time = getattr(si, "time", None)
        if (
            last is not None
            and last.trustworthy
            and last.advert_time is not None
            and adv_time is not None
            and float(adv_time) <= float(last.advert_time)
        ):
            return
        _push(device_data.update(si))

    @callback
    def _reconcile(_now=None) -> None:
        run_on_hass_loop(hass, _reconcile_body)

    async def _async_reconcile(_now=None) -> None:
        _reconcile_body()

    store["reconcile"] = _reconcile
    entry.async_on_unload(
        async_track_time_interval(hass, _async_reconcile, timedelta(seconds=20))
    )

    async def _kick() -> None:
        await asyncio.sleep(3)
        await _async_reconcile()
        await asyncio.sleep(8)
        await _async_reconcile()

    task = hass.async_create_task(_kick())
    entry.async_on_unload(task.cancel)

    if device_kind == DEVICE_KIND_HYDRO4:
        from .manual_once import async_register_manual_once
        from .hydro_program_session import async_register_program_services

        unregister = await async_register_manual_once(hass, entry.entry_id)
        entry.async_on_unload(unregister)
        unregister_prog = await async_register_program_services(hass, entry.entry_id)
        entry.async_on_unload(unregister_prog)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClaberBTConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
