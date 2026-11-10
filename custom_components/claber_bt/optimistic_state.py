"""Post-ACK optimistic Hydro state + stop hold-off (0.1.13).

After a confirmed GATT command, push expected valve/program bits to
state_listeners immediately, and suppress stale watering ads for a short
hold-off so Stop cannot flash closed→open from a buffered advert.
No BLE I/O here.
"""
from __future__ import annotations

import time
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .const import CONF_DEVICE_KIND, DEVICE_KIND_HYDRO4, DOMAIN, is_hydro_family
from .parser import ClaberUpdate
from .sensor import clear_irrigation_ends_cache

STOP_HOLDOFF_S = 25.0
START_HOLDOFF_S = 30.0


def _store(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})


def _opt(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    return _store(hass, entry_id).setdefault(
        "cmd_optimistic",
        {
            "stop_holdoff_until": 0.0,
            "force_lines": None,  # list[bool] | None
            "force_until": 0.0,
            "force_program": None,
        },
    )


def filter_watering_lines(
    hass: HomeAssistant, entry_id: str, lines: list[bool]
) -> list[bool]:
    """Apply stop hold-off / forced lines over a trustworthy advert."""
    o = _opt(hass, entry_id)
    now = time.monotonic()
    out = list(lines) if lines else [False, False, False, False]
    while len(out) < 4:
        out.append(False)
    out = out[:4]
    if now < float(o.get("stop_holdoff_until") or 0):
        return [False, False, False, False]
    force_until = float(o.get("force_until") or 0)
    force = o.get("force_lines")
    if force is not None and now < force_until:
        return list(force)[:4]
    return out


def clear_force_if_advert_matches(
    hass: HomeAssistant, entry_id: str, lines: list[bool]
) -> None:
    o = _opt(hass, entry_id)
    force = o.get("force_lines")
    if force is None:
        return
    now = time.monotonic()
    if now >= float(o.get("force_until") or 0):
        o["force_lines"] = None
        return
    cur = list(lines) if lines else [False, False, False, False]
    while len(cur) < 4:
        cur.append(False)
    if cur[:4] == list(force)[:4]:
        o["force_lines"] = None


def _notify_listeners(hass: HomeAssistant, entry_id: str, update: ClaberUpdate) -> None:
    store = _store(hass, entry_id)
    for listener in list(store.get("state_listeners") or []):
        try:
            listener(update)
        except Exception:  # noqa: BLE001
            pass


def apply_after_manual_ack(
    hass: HomeAssistant,
    entry_id: str,
    *,
    address: str,
    stop_only: bool,
    line: int | None,
    minutes: int | None,
    program: str | None,
    success: bool,
) -> None:
    """Optimistic HA-facing state after device accepted the command."""
    if not success:
        return
    o = _opt(hass, entry_id)
    now = time.monotonic()
    if stop_only:
        o["stop_holdoff_until"] = now + STOP_HOLDOFF_S
        o["force_lines"] = [False, False, False, False]
        o["force_until"] = now + STOP_HOLDOFF_S
        o["force_program"] = "none"
        clear_irrigation_ends_cache(address)
        fields = {
            "lines_watering": [False, False, False, False],
            "remaining_minutes": 0,
            "opening_program": "none",
        }
    elif program is not None:
        # Program start now — do not invent line bits; just clear stop hold-off.
        o["stop_holdoff_until"] = 0.0
        o["force_lines"] = None
        o["force_program"] = f"semi_auto_{program}" if program in ("A", "B") else program
        o["force_until"] = now + START_HOLDOFF_S
        fields = {
            "opening_program": o["force_program"],
        }
    else:
        ln = int(line or 1)
        mins = int(minutes or 1)
        forced = [False, False, False, False]
        if 1 <= ln <= 4:
            forced[ln - 1] = True
        o["stop_holdoff_until"] = 0.0
        o["force_lines"] = forced
        o["force_until"] = now + START_HOLDOFF_S
        o["force_program"] = "manual"
        fields = {
            "lines_watering": forced,
            "remaining_minutes": mins,
            "opening_program": "manual",
        }
    entry = _store(hass, entry_id).get("entry")
    kind = (
        entry.data.get(CONF_DEVICE_KIND)
        if entry is not None
        else DEVICE_KIND_HYDRO4
    )
    update = ClaberUpdate(
        address=address,
        device_kind=kind if is_hydro_family(kind) else DEVICE_KIND_HYDRO4,
        name=None,
        rssi=None,
        fields=fields,
        values_updated=True,
        trustworthy=True,
        live_confirmed=True,
    )
    _notify_listeners(hass, entry_id, update)


@callback
def patch_update_fields(hass: HomeAssistant, entry_id: str, update: ClaberUpdate) -> None:
    """Mutate a live advert update in-place for hold-off / force."""
    if (
        not is_hydro_family(update.device_kind)
        or not update.trustworthy
        or not update.fields
    ):
        return
    raw = list(update.fields.get("lines_watering") or [False, False, False, False])
    clear_force_if_advert_matches(hass, entry_id, raw)
    filtered = filter_watering_lines(hass, entry_id, raw)
    update.fields["lines_watering"] = filtered
    o = _opt(hass, entry_id)
    now = time.monotonic()
    if now < float(o.get("stop_holdoff_until") or 0):
        update.fields["remaining_minutes"] = 0
        update.fields["opening_program"] = "none"
        clear_irrigation_ends_cache(update.address)
