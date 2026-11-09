"""Diagnostics for Claber BT (no secrets; no personal identifiers).

Live config-entry unique_id / BLE address identity is unchanged. This module
only redacts identifiers from the *diagnostics dump* so support exports cannot
leak MAC addresses embedded in titles, unique_id, connections, or strings.
"""

from __future__ import annotations

import re
import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import ClaberBTConfigEntry
from .const import DOMAIN

# Keys whose values are identifiers even when the string form varies.
TO_REDACT_KEYS = {
    "address",
    "unique_id",
    "serial_number",
    "mac",
    "mac_address",
    "connections",
    "identifiers",
    "suggested_area",  # unused; keep conservative
}

# Colon/hyphen MACs and compact 12-hex forms (normalized unique_id style).
_MAC_SEP = re.compile(
    r"(?i)(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}"
)
_MAC_COMPACT = re.compile(
    r"(?i)(?<![0-9A-Fa-f])(?:[0-9A-F]{2}){6}(?![0-9A-Fa-f])"
)
REDACTED_MAC = "**REDACTED_MAC**"
_REDACTED = REDACTED_MAC  # alias for internal use


def redact_mac_strings(value: Any) -> Any:
    """Recursively redact MAC-like tokens from strings; drop identifier keys."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in TO_REDACT_KEYS:
                out[key] = _REDACTED
            else:
                out[key] = redact_mac_strings(item)
        return out
    if isinstance(value, list):
        return [redact_mac_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_mac_strings(item) for item in value)
    if isinstance(value, set | frozenset):
        return [redact_mac_strings(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        # Never emit raw advertisement / MSD bytes (may embed address suffix).
        return _REDACTED
    if isinstance(value, str):
        text = _MAC_SEP.sub(_REDACTED, value)
        text = _MAC_COMPACT.sub(_REDACTED, text)
        return text
    # Unknown objects (e.g. discovery key dataclasses): scrub their text form.
    text = repr(value)
    if _MAC_SEP.search(text) or _MAC_COMPACT.search(text):
        return _REDACTED
    return value


def _safe_entry_snapshot(entry: ClaberBTConfigEntry) -> dict[str, Any]:
    """Build a curated entry snapshot (no discovery_keys / raw identity objects).

    ``as_dict()`` can embed Bluetooth discovery key objects whose ``repr``
    contains the device MAC even after key-based redaction. Only export a
    whitelist of non-identifying fields, then MAC-scrub strings.
    """
    raw: dict[str, Any] = {
        "domain": entry.domain,
        "entry_id": entry.entry_id,
        "version": entry.version,
        "minor_version": getattr(entry, "minor_version", None),
        "source": entry.source,
        "data": dict(entry.data),
        # title/unique_id are included only so redaction can prove they scrub;
        # values are always MAC-scrubbed / replaced below.
        "title": entry.title,
        "unique_id": entry.unique_id,
        "pref_disable_new_entities": getattr(
            entry, "pref_disable_new_entities", False
        ),
        "pref_disable_polling": getattr(entry, "pref_disable_polling", False),
        # Explicitly omitted: discovery_keys, runtime_data, connections,
        # identifiers, options that may nest BLE addresses.
    }
    return redact_mac_strings(async_redact_data(raw, TO_REDACT_KEYS))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClaberBTConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (MAC-safe)."""
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    device_data = store.get("device_data")
    last = device_data.last_update if device_data else None
    last_values_age: float | None = None
    if device_data is not None and device_data.last_values_monotonic is not None:
        last_values_age = max(0.0, time.monotonic() - device_data.last_values_monotonic)

    last_update_payload: dict[str, Any] | None
    if last is None:
        last_update_payload = None
    else:
        # Safe field subset for Hydro stale-line diagnosis (no MSD/MAC bytes).
        field_snapshot: dict[str, Any] = {}
        for key in (
            "lines_watering",
            "remaining_minutes",
            "opening_program",
            "automatic_watering_enabled",
            "is_raining",
            "temperature_c",
            "energy_level",
            "battery_percent",
        ):
            if key in last.fields:
                field_snapshot[key] = last.fields[key]
        last_update_payload = redact_mac_strings(
            {
                "address": last.address,
                "device_kind": last.device_kind,
                "name": last.name,
                "rssi": last.rssi,
                "values_updated": last.values_updated,
                "field_keys": sorted(last.fields.keys()),
                "fields": field_snapshot,
                "had_raw": last.had_raw,
                "manufacturer_key_count": last.manufacturer_key_count,
                "company_id": last.company_id,
                "selection": last.selection,
                "trustworthy": last.trustworthy,
                "live_confirmed": last.live_confirmed,
                "advert_age_seconds": last.advert_age_seconds,
                "presence_only": getattr(last, "presence_only", False),
            }
        )

    reception = None
    last_trustworthy_payload = None
    if device_data is not None:
        rx = device_data.last_reception
        reception = {
            "had_raw": rx.had_raw,
            "manufacturer_key_count": rx.manufacturer_key_count,
            "company_id": rx.company_id,
            "selection": rx.selection,
            "values_updated": rx.values_updated,
            "trustworthy": rx.trustworthy,
            "live_confirmed": rx.live_confirmed,
            "advert_age_seconds": rx.advert_age_seconds,
            # Age of last reception attempt (presence), distinct from entity
            # last_updated (which can bump on restore/re-add with no RF).
            "reception_age_seconds": (
                None
                if rx.monotonic is None
                else max(0.0, time.monotonic() - rx.monotonic)
            ),
        }
        trusted = device_data.last_trustworthy_update
        if trusted is not None:
            last_trustworthy_payload = {
                "values_updated": trusted.values_updated,
                "trustworthy": True,
                "selection": trusted.selection,
                "had_raw": trusted.had_raw,
                "manufacturer_key_count": trusted.manufacturer_key_count,
                "company_id": trusted.company_id,
                "lines_watering": trusted.fields.get("lines_watering"),
                "opening_program": trusted.fields.get("opening_program"),
                "remaining_minutes": trusted.fields.get("remaining_minutes"),
                "is_raining": trusted.fields.get("is_raining"),
                "trustworthy_values_age_seconds": (
                    None
                    if device_data.last_trustworthy_values_monotonic is None
                    else max(
                        0.0,
                        time.monotonic()
                        - device_data.last_trustworthy_values_monotonic,
                    )
                ),
            }

    payload = {
        "entry": _safe_entry_snapshot(entry),
        "last_update": last_update_payload,
        "last_trustworthy_update": last_trustworthy_payload,
        "last_values_age_seconds": last_values_age,
        "last_reception": reception,
        "presence_vs_values": (
            "HA/coordinator availability means a callback was heard (including "
            "history replay / remapped times after restart). "
            "entity last_updated can bump on restore/re-add without RF. "
            "Trustworthy line/rain binary state requires live_confirmed "
            "(service_info.time advanced past the first session callback). "
            "last_trustworthy_update is the only binary ON/OFF provenance."
        ),
        "line_running_note": (
            "Binary watering entities do not restore ON from HA Store. "
            "First session callback is treated as untrusted (history/cache). "
            "remaining_minutes==0 must NOT force lines OFF. "
            "If Line is unknown after restart: wait for a newer advert "
            "(time advanced) or check last_reception.live_confirmed."
        ),
        "passive_only": True,
        "connectable_matcher": False,
        "hydro_advert_semantics": (
            "Hydro-4 live state is in the on-air manufacturer AD including the "
            "two company-ID bytes (Rea / _parseHydroData). Trustworthy decode "
            "requires service_info.raw; HA manufacturer_data keys accumulate "
            "old company IDs and must not be used. No GATT status command."
        ),
        "note": (
            "connectable=false opts into non-connectable scanners; "
            "it does not exclude devices that advertise as connectable."
        ),
    }
    # Final sweep for any nested title/unique_id/MAC strings we missed.
    return redact_mac_strings(payload)
