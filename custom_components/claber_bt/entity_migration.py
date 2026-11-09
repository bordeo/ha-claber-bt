"""Supported entity-registry migration (no .storage edits).

0.1.9 bug: PassiveBluetooth unique_ids use ``MAC-key`` (hyphen), while migration
only matched ``_key`` suffixes. 0.1.10 matches both forms.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

try:
    from .const import CONF_DEVICE_KIND, DEVICE_KIND_HYDRO4, DEVICE_KIND_RAIN
except ImportError:  # unittest without package context
    from const import CONF_DEVICE_KIND, DEVICE_KIND_HYDRO4, DEVICE_KIND_RAIN  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Tokens that appear at the end of unique_id (after _ or -)
_OBSOLETE_UID_TOKENS = (
    "open_line_1",
    "open_line_2",
    "open_line_3",
    "open_line_4",
    "line_1_watering",
    "line_2_watering",
    "line_3_watering",
    "line_4_watering",
    "aggiorna_ora",
    "remaining_minutes",
    # 0.1.10: auto mode moved from binary_sensor → switch (new unique_id)
    "automatic_watering",
)


def _uid_is_obsolete(uid: str) -> bool:
    if not uid:
        return False
    for tok in _OBSOLETE_UID_TOKENS:
        if uid == tok or uid.endswith(f"_{tok}") or uid.endswith(f"-{tok}"):
            return True
    return False


async def async_migrate_entity_registry(hass: "HomeAssistant", entry: "ConfigEntry") -> None:
    """Drop obsolete registry rows for this config entry."""
    kind = entry.data.get(CONF_DEVICE_KIND)
    if kind not in (DEVICE_KIND_HYDRO4, DEVICE_KIND_RAIN):
        return
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    removed: list[str] = []
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        uid = ent.unique_id or ""
        # Only strip Hydro obsolete tokens; Rain keeps its sensors
        if kind == DEVICE_KIND_HYDRO4 and _uid_is_obsolete(uid):
            registry.async_remove(ent.entity_id)
            removed.append(ent.entity_id)
    if removed:
        _LOGGER.info(
            "claber_bt 0.1.10 registry migration removed %s obsolete entities: %s",
            len(removed),
            ", ".join(removed),
        )
