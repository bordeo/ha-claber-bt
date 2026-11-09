"""GATT session for Hydro program read/write and auto toggle.

User-initiated only. No retries. No boot/periodic reads.
Cmd 0C (start program now / manual valve open) is NEVER used here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

try:
    from .const import CONF_BLUETOOTH_SOURCE, CONF_DEVICE_KIND, DEVICE_KIND_HYDRO4, DOMAIN
    from .hydro_commands import build_auth_packet
    from .hydro_program_codec import (
        LIVE_ENABLED,
        auto_frame,
        date_time_frame,
        merge_field,
        program_to_record,
        read_programs_request,
        try_parse_0b_reply,
        try_parse_status_ack,
        validate_program_pair,
        write_programs_frame,
    )
    from .manual_once import (
        CLEANUP_RESERVE,
        CONNECT_TIMEOUT,
        DISCONNECT_TIMEOUT,
        Deadline,
        NOTIFY_SETUP_TIMEOUT,
        NOTIFY_UUID,
        TOTAL_DEADLINE,
        WRITE_TIMEOUT,
        WRITE_UUID,
        ProbeAbort,
        _norm_mac,
        _sanitize_result,
    )
    from .program_store import ProgramStore
except ImportError:  # pragma: no cover
    raise

_LOGGER = logging.getLogger(__name__)

LOCK_KEY = f"{DOMAIN}_gatt_lock"  # shared with manual_once (0.1.13)
USER_TZ = ZoneInfo("Europe/Rome")


def _get_store(hass: HomeAssistant, entry_id: str) -> ProgramStore:
    store = hass.data[DOMAIN][entry_id]
    ps = store.get("program_store")
    if ps is None:
        ps = ProgramStore()
        store["program_store"] = ps
    return ps


async def _disconnect(client: Any, result: dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT)
        result["disconnected"] = True
    except Exception as err:  # noqa: BLE001
        result["disconnected"] = False
        result["disconnect_error"] = type(err).__name__


async def _open_session(
    hass: HomeAssistant,
    *,
    address: str,
    preferred_source: str | None,
    deadline: Deadline,
) -> tuple[Any, Any, bytearray, dict[str, Any]]:
    """Connect + notify + auth. Returns (client, ble_device, buf, result_stub)."""
    result: dict[str, Any] = {
        "connected": False,
        "success": False,
        "writes": 0,
        "error": None,
    }
    ble = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble is None:
        ble = bluetooth.async_ble_device_from_address(hass, address, connectable=False)
    if ble is None:
        raise ProbeAbort("proxy_not_seen")

    from bleak import BleakClient

    client = BleakClient(ble, timeout=deadline.step(CONNECT_TIMEOUT, CLEANUP_RESERVE))
    buf = bytearray()

    def _on_notify(_handle: int, data: bytearray) -> None:
        buf.extend(bytes(data))

    t0 = time.monotonic()
    await asyncio.wait_for(client.connect(), timeout=deadline.step(CONNECT_TIMEOUT, CLEANUP_RESERVE))
    result["connected"] = True
    result["connect_s"] = round(time.monotonic() - t0, 3)

    await asyncio.wait_for(
        client.start_notify(NOTIFY_UUID, _on_notify),
        timeout=deadline.step(NOTIFY_SETUP_TIMEOUT, CLEANUP_RESERVE),
    )

    auth = build_auth_packet(address)
    buf.clear()
    await asyncio.wait_for(
        client.write_gatt_char(WRITE_UUID, auth, response=False),
        timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
    )
    result["writes"] = 1
    # wait for auth ack
    end = time.monotonic() + min(5.0, deadline.remaining(CLEANUP_RESERVE))
    status = None
    while time.monotonic() < end:
        status = try_parse_status_ack(bytes(buf), 0x00)
        if status is not None:
            break
        await asyncio.sleep(0.05)
    if status != 0x00:
        raise ProbeAbort("auth_rejected" if status not in (None,) else "auth_missing")
    return client, ble, buf, result


async def async_read_programs(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """User-pressed 'Leggi programmi': one read, no retry."""
    entry = hass.data[DOMAIN][entry_id]["entry"]
    if entry.data.get(CONF_DEVICE_KIND) != DEVICE_KIND_HYDRO4:
        return {"success": False, "error": "not_hydro"}
    address = _norm_mac(entry.unique_id or "")
    preferred = entry.options.get(CONF_BLUETOOTH_SOURCE)
    store = _get_store(hass, entry_id)

    lock = hass.data.setdefault(DOMAIN, {}).setdefault(LOCK_KEY, asyncio.Lock())
    # 0.1.13: wait for the shared GATT lock instead of failing busy so
    # sequential card field writes (and batch) are not dropped.
    async with lock:
        deadline = Deadline(TOTAL_DEADLINE)
        client = None
        result: dict[str, Any] = {"success": False, "error": None, "writes": 0}
        try:
            client, _ble, buf, result = await _open_session(
                hass, address=address, preferred_source=preferred, deadline=deadline
            )
            buf.clear()
            req = read_programs_request()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, req, response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            parsed = None
            end = time.monotonic() + min(8.0, deadline.remaining(CLEANUP_RESERVE))
            while time.monotonic() < end:
                parsed = try_parse_0b_reply(bytes(buf))
                if parsed is not None:
                    break
                await asyncio.sleep(0.05)
            if parsed is None:
                store.set_error("reply_undecodable")
                result["error"] = "ack_ambiguous"
                return _sanitize_result(result, address=address)
            prog_a, prog_b = parsed
            store.set_programs(prog_a, prog_b)
            result["success"] = True
            return _sanitize_result(result, address=address)
        except ProbeAbort as err:
            store.set_error(err.code)
            result["error"] = err.code
            return _sanitize_result(result, address=address)
        except Exception as err:  # noqa: BLE001
            store.set_error("gatt_error")
            result["error"] = "gatt_error"
            result["error_type"] = type(err).__name__
            _LOGGER.warning("claber_bt program read failed: %s", type(err).__name__)
            return _sanitize_result(result, address=address)
        finally:
            if client is not None:
                await _disconnect(client, result)


async def async_apply_program_field(
    hass: HomeAssistant,
    entry_id: str,
    *,
    program: str,
    field: str,
    value: Any,
) -> dict[str, Any]:
    """Fresh read → merge one field → validate → 04 → 0B → verify read. No retry."""
    if not LIVE_ENABLED:
        return {"success": False, "error": "writes_disabled"}

    entry = hass.data[DOMAIN][entry_id]["entry"]
    if entry.data.get(CONF_DEVICE_KIND) != DEVICE_KIND_HYDRO4:
        return {"success": False, "error": "not_hydro"}
    address = _norm_mac(entry.unique_id or "")
    preferred = entry.options.get(CONF_BLUETOOTH_SOURCE)
    store = _get_store(hass, entry_id)

    lock = hass.data.setdefault(DOMAIN, {}).setdefault(LOCK_KEY, asyncio.Lock())
    # 0.1.13: wait for the shared GATT lock instead of failing busy so
    # sequential card field writes (and batch) are not dropped.
    async with lock:
        deadline = Deadline(TOTAL_DEADLINE + 15.0)  # read+write+verify
        client = None
        result: dict[str, Any] = {"success": False, "error": None, "writes": 0}
        try:
            client, _ble, buf, result = await _open_session(
                hass, address=address, preferred_source=preferred, deadline=deadline
            )

            async def _read_once() -> tuple[dict, dict]:
                buf.clear()
                await asyncio.wait_for(
                    client.write_gatt_char(WRITE_UUID, read_programs_request(), response=False),
                    timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
                )
                result["writes"] = result.get("writes", 0) + 1
                end = time.monotonic() + min(8.0, deadline.remaining(CLEANUP_RESERVE))
                while time.monotonic() < end:
                    parsed = try_parse_0b_reply(bytes(buf))
                    if parsed is not None:
                        return parsed
                    await asyncio.sleep(0.05)
                raise ProbeAbort("ack_ambiguous")

            # 1) Fresh read (mandatory)
            prog_a, prog_b = await _read_once()

            # 2) Merge single field
            new_a, new_b = merge_field(
                prog_a, prog_b, program=program, field=field, value=value
            )

            # 3) Validate
            err = validate_program_pair(new_a, new_b)
            if err is not None:
                store.set_error(err)
                result["error"] = "fixed_parameters_required"
                result["validation_error"] = err
                # expose clear HA error via last_error; do not write
                return {
                    **_sanitize_result(result, address=address),
                    "validation_error": err,
                }

            # 4) Set date/time (local Europe/Rome) — INFERRED risk if wrong TZ
            now = datetime.now(USER_TZ)
            wd = now.isoweekday()  # 1=Mon..7=Sun matches Dart
            dt_frame = date_time_frame(
                now.year, now.month, now.day, wd, now.hour, now.minute, now.second
            )
            buf.clear()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, dt_frame, response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            # App ignores 04 ack — brief pause
            await asyncio.sleep(0.25)

            # 5) Write both programs
            frame_0b = write_programs_frame(
                program_to_record(new_a), program_to_record(new_b)
            )
            buf.clear()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, frame_0b, response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            end = time.monotonic() + min(5.0, deadline.remaining(CLEANUP_RESERVE))
            status = None
            while time.monotonic() < end:
                status = try_parse_status_ack(bytes(buf), 0x0B)
                if status is not None:
                    break
                await asyncio.sleep(0.05)
            if status != 0x00:
                store.set_error("write_rejected" if status is not None else "ack_ambiguous")
                result["error"] = "ack_ambiguous" if status is None else "gatt_error"
                result["write_ack"] = status
                return _sanitize_result(result, address=address)

            # 6) Verify read
            ver_a, ver_b = await _read_once()
            store.set_programs(ver_a, ver_b)
            result["success"] = True
            result["write_ack"] = 0x00
            return _sanitize_result(result, address=address)
        except ProbeAbort as err:
            store.set_error(err.code)
            result["error"] = err.code
            return _sanitize_result(result, address=address)
        except Exception as err:  # noqa: BLE001
            store.set_error("gatt_error")
            result["error"] = "gatt_error"
            result["error_type"] = type(err).__name__
            _LOGGER.warning("claber_bt program write failed: %s", type(err).__name__)
            return _sanitize_result(result, address=address)
        finally:
            if client is not None:
                await _disconnect(client, result)



async def async_apply_program_fields(
    hass: HomeAssistant,
    entry_id: str,
    *,
    program: str,
    fields: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Fresh read → merge many fields → one 0B write → verify. No retry.

    Fixes card Salva firing N sequential GATT sessions (busy / overwrite / lost
    Programma A enable during 21:47 session).
    """
    if not LIVE_ENABLED:
        return {"success": False, "error": "writes_disabled"}
    if not fields:
        return {"success": True, "error": None, "writes": 0}

    entry = hass.data[DOMAIN][entry_id]["entry"]
    if entry.data.get(CONF_DEVICE_KIND) != DEVICE_KIND_HYDRO4:
        return {"success": False, "error": "not_hydro"}
    address = _norm_mac(entry.unique_id or "")
    preferred = entry.options.get(CONF_BLUETOOTH_SOURCE)
    store = _get_store(hass, entry_id)

    lock = hass.data.setdefault(DOMAIN, {}).setdefault(LOCK_KEY, asyncio.Lock())
    # 0.1.13: wait for the shared GATT lock instead of failing busy so
    # sequential card field writes (and batch) are not dropped.
    async with lock:
        deadline = Deadline(TOTAL_DEADLINE + 20.0)
        client = None
        result: dict[str, Any] = {"success": False, "error": None, "writes": 0}
        try:
            client, _ble, buf, result = await _open_session(
                hass, address=address, preferred_source=preferred, deadline=deadline
            )

            async def _read_once() -> tuple[dict, dict]:
                buf.clear()
                await asyncio.wait_for(
                    client.write_gatt_char(WRITE_UUID, read_programs_request(), response=False),
                    timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
                )
                result["writes"] = result.get("writes", 0) + 1
                end = time.monotonic() + min(8.0, deadline.remaining(CLEANUP_RESERVE))
                while time.monotonic() < end:
                    parsed = try_parse_0b_reply(bytes(buf))
                    if parsed is not None:
                        return parsed
                    await asyncio.sleep(0.05)
                raise ProbeAbort("ack_ambiguous")

            prog_a, prog_b = await _read_once()
            new_a, new_b = prog_a, prog_b
            for field, value in fields:
                new_a, new_b = merge_field(
                    new_a, new_b, program=program, field=field, value=value
                )

            err = validate_program_pair(new_a, new_b)
            if err is not None:
                store.set_error(err)
                result["error"] = "fixed_parameters_required"
                result["validation_error"] = err
                return {
                    **_sanitize_result(result, address=address),
                    "validation_error": err,
                }

            now = datetime.now(USER_TZ)
            wd = now.isoweekday()
            dt_frame = date_time_frame(
                now.year, now.month, now.day, wd, now.hour, now.minute, now.second
            )
            buf.clear()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, dt_frame, response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            await asyncio.sleep(0.25)

            frame_0b = write_programs_frame(
                program_to_record(new_a), program_to_record(new_b)
            )
            buf.clear()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, frame_0b, response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            end = time.monotonic() + min(5.0, deadline.remaining(CLEANUP_RESERVE))
            status = None
            while time.monotonic() < end:
                status = try_parse_status_ack(bytes(buf), 0x0B)
                if status is not None:
                    break
                await asyncio.sleep(0.05)
            if status != 0x00:
                store.set_error("write_rejected" if status is not None else "ack_ambiguous")
                result["error"] = "ack_ambiguous" if status is None else "gatt_error"
                result["write_ack"] = status
                return _sanitize_result(result, address=address)

            ver_a, ver_b = await _read_once()
            store.set_programs(ver_a, ver_b)
            result["success"] = True
            result["write_ack"] = 0x00
            return _sanitize_result(result, address=address)
        except ProbeAbort as err:
            store.set_error(err.code)
            result["error"] = err.code
            return _sanitize_result(result, address=address)
        except Exception as err:  # noqa: BLE001
            store.set_error("gatt_error")
            result["error"] = "gatt_error"
            result["error_type"] = type(err).__name__
            _LOGGER.warning("claber_bt program batch write failed: %s", type(err).__name__)
            return _sanitize_result(result, address=address)
        finally:
            if client is not None:
                await _disconnect(client, result)


async def async_set_auto_mode(hass: HomeAssistant, entry_id: str, *, on: bool) -> dict[str, Any]:
    """Toggle automatic irrigation (cmd 05). User action only. No retry."""
    if not LIVE_ENABLED:
        return {"success": False, "error": "writes_disabled"}

    entry = hass.data[DOMAIN][entry_id]["entry"]
    if entry.data.get(CONF_DEVICE_KIND) != DEVICE_KIND_HYDRO4:
        return {"success": False, "error": "not_hydro"}
    address = _norm_mac(entry.unique_id or "")
    preferred = entry.options.get(CONF_BLUETOOTH_SOURCE)

    lock = hass.data.setdefault(DOMAIN, {}).setdefault(LOCK_KEY, asyncio.Lock())
    async with lock:
        deadline = Deadline(TOTAL_DEADLINE)
        client = None
        result: dict[str, Any] = {"success": False, "error": None, "writes": 0}
        try:
            client, _ble, buf, result = await _open_session(
                hass, address=address, preferred_source=preferred, deadline=deadline
            )
            buf.clear()
            await asyncio.wait_for(
                client.write_gatt_char(WRITE_UUID, auto_frame(on=on), response=False),
                timeout=deadline.step(WRITE_TIMEOUT, CLEANUP_RESERVE),
            )
            result["writes"] = result.get("writes", 0) + 1
            end = time.monotonic() + min(5.0, deadline.remaining(CLEANUP_RESERVE))
            status = None
            while time.monotonic() < end:
                status = try_parse_status_ack(bytes(buf), 0x05)
                if status is not None:
                    break
                await asyncio.sleep(0.05)
            if status != 0x00:
                result["error"] = "ack_ambiguous" if status is None else "gatt_error"
                return _sanitize_result(result, address=address)
            result["success"] = True
            return _sanitize_result(result, address=address)
        except ProbeAbort as err:
            result["error"] = err.code
            return _sanitize_result(result, address=address)
        except Exception as err:  # noqa: BLE001
            result["error"] = "gatt_error"
            result["error_type"] = type(err).__name__
            return _sanitize_result(result, address=address)
        finally:
            if client is not None:
                await _disconnect(client, result)


SERVICE_WRITE_PROGRAM = "write_program"


async def async_register_program_services(hass: HomeAssistant, entry_id: str):
    """Register claber_bt.write_program (batch) once per domain."""
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv
    from homeassistant.core import ServiceCall

    async def _handler(call: ServiceCall) -> None:
        prog = call.data["program"]
        fields: list[tuple[str, Any]] = []
        if "enabled" in call.data:
            fields.append(("enabled", bool(call.data["enabled"])))
        if "budget" in call.data:
            fields.append(("budget", int(call.data["budget"])))
        days = call.data.get("days") or {}
        for day, on in days.items():
            fields.append(("weekday_bit", (str(day), bool(on))))
        lines = call.data.get("lines") or {}
        for lk, mins in lines.items():
            fields.append(("line_minutes", (int(lk), int(mins))))
        starts = call.data.get("starts") or {}
        for sk, spec in starts.items():
            if not isinstance(spec, dict):
                continue
            if not spec.get("on"):
                fields.append(("start", (int(sk), None)))
            else:
                hhmm = str(spec.get("time") or "06:00")
                parts = hhmm.split(":")
                hm = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
                fields.append(("start", (int(sk), hm)))
        # Prefer the entry_id from the registering Hydro entry
        result = await async_apply_program_fields(
            hass, entry_id, program=prog, fields=fields
        )
        if not result.get("success"):
            from homeassistant.exceptions import HomeAssistantError
            detail = result.get("validation_error") or result.get("error") or "write_failed"
            raise HomeAssistantError(f"Claber write_program {prog}: {detail}")

    schema = vol.Schema(
        {
            vol.Required("program"): vol.In(["A", "B"]),
            vol.Optional("enabled"): cv.boolean,
            vol.Optional("budget"): vol.All(vol.Coerce(int), vol.Range(min=50, max=150)),
            vol.Optional("days"): dict,
            vol.Optional("lines"): dict,
            vol.Optional("starts"): dict,
        }
    )
    if not hass.services.has_service(DOMAIN, SERVICE_WRITE_PROGRAM):
        hass.services.async_register(
            DOMAIN, SERVICE_WRITE_PROGRAM, _handler, schema=schema
        )

    def _unregister() -> None:
        # Keep service while any Hydro entry remains
        still = False
        for ent_id, payload in hass.data.get(DOMAIN, {}).items():
            if not isinstance(payload, dict):
                continue
            entry = payload.get("entry")
            if entry is not None and entry.data.get(CONF_DEVICE_KIND) == DEVICE_KIND_HYDRO4:
                still = True
                break
        if not still and hass.services.has_service(DOMAIN, SERVICE_WRITE_PROGRAM):
            hass.services.async_remove(DOMAIN, SERVICE_WRITE_PROGRAM)

    return _unregister
