"""Hydro manual commands (start line N for M minutes, or global stop).

Always available for Hydro-4 entries (no separate read-only / opt-in mode).
GATT connection happens only when a command entity or service is invoked.

Hard limits:
- lines 1..4, minutes 1..120, or explicit global stop_only
- One command at a time (domain lock); no retries; disconnect always attempted
- Auth is MAC-derived (app ValidationCommand); no PIN stage
- Address = config-entry unique_id; preferred Bluetooth source = optional option

Ambiguous ACK is NOT success. Disconnect is NOT a physical-flow guarantee.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv

try:
    from .const import (
        CONF_BLUETOOTH_SOURCE,
        CONF_DEVICE_KIND,
        DEFAULT_MANUAL_MINUTES,
        DOMAIN,
        MANUAL_LINES,
        MANUAL_MAX_MINUTES,
        MANUAL_MIN_MINUTES,
        is_hydro_family,
        manual_lines_for_kind,
    )
    from .hydro_commands import (
        build_auth_packet,
        build_manual_start,
        build_manual_stop,
        build_program_start,
        verification_tag as _verification_tag,
    )
except ImportError:  # unittest without package context
    from const import (  # type: ignore[no-redef]
        CONF_BLUETOOTH_SOURCE,
        CONF_DEVICE_KIND,
        DEFAULT_MANUAL_MINUTES,
        DOMAIN,
        MANUAL_LINES,
        MANUAL_MAX_MINUTES,
        MANUAL_MIN_MINUTES,
        is_hydro_family,
        manual_lines_for_kind,
    )
    from hydro_commands import (  # type: ignore[no-redef]
        build_auth_packet,
        build_manual_start,
        build_manual_stop,
        build_program_start,
        verification_tag as _verification_tag,
    )

_LOGGER = logging.getLogger(__name__)

SERVICE_MANUAL_ONCE = "manual_once"

NOTIFY_UUID = "01ff2c23-2f17-198c-e343-3ca6fe3ceebb"
WRITE_UUID = "02ff2c23-2f17-198c-e343-3ca6fe3ceebb"


TOTAL_DEADLINE = 20.0
DISCONNECT_TIMEOUT = 2.5
CLEANUP_RESERVE = DISCONNECT_TIMEOUT
CONNECT_TIMEOUT = 15.0
NOTIFY_SETUP_TIMEOUT = 3.0
WRITE_TIMEOUT = 2.0
NOTIFY_WAIT = 5.0

ERRORS = frozenset(
    {
        "busy",
        "address_not_allowed",
        "not_hydro",
        "proxy_not_seen",
        "proxy_not_last_source",
        "proxy_source_unsupported",
        "proxy_no_slot",
        "proxy_source_unverified",
        "proxy_source_mismatch",
        "auth_input_format",
        "connect_timeout",
        "connect_failed",
        "notify_setup_timeout",
        "write_timeout",
        "write_not_allowlisted",
        "gatt_error",
        "ack_missing",
        "ack_ambiguous",
        "deadline_exceeded",
        "internal_error",
        "fixed_parameters_required",
        "refused_budget",
        "auth_missing",
        "auth_rejected",
        "disconnect_failed",
        "no_config_entry",
        "invalid_line",
        "invalid_minutes",
    }
)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

STORE_DURATION_KEY = "manual_duration_minutes"
LOCK_KEY = f"{DOMAIN}_gatt_lock"  # shared with program sessions (0.1.13)


def _norm_mac(value: str) -> str:
    return value.strip().upper().replace("-", ":")






class ProbeAbort(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in ERRORS else "internal_error"


class Deadline:
    def __init__(self, total: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.start = clock()
        self.end = self.start + total

    def remaining(self, reserve: float = 0.0) -> float:
        return max(0.0, self.end - reserve - self._clock())

    def step(self, cap: float, reserve: float = 0.0) -> float:
        return min(cap, self.remaining(reserve))

    def elapsed(self) -> float:
        return self._clock() - self.start


def _classify_frame(data: Any) -> tuple[int, int, int, bool] | None:
    try:
        b = bytes(data)
    except Exception:  # noqa: BLE001
        return None
    if len(b) < 4 or b[0] != 0x23:
        return None
    n = b[3]
    well_formed = (
        len(b) == 4 + n + 2
        and b[4 + n] == 0xFF
        and (sum(b[: 4 + n]) + b[-1]) & 0xFF == 0
    )
    return b[1], b[2], n, well_formed


class FrameTally:
    def __init__(self) -> None:
        self.active: str | None = None
        self.expected = 0
        self.event = asyncio.Event()
        self.invalid = False
        self.windows: dict[str, list] = {}

    def arm(self, role: str, identifier: int) -> None:
        self.active, self.expected = role, identifier
        self.windows[role] = [False, None, False]
        self.event.clear()

    def feed(self, data: Any) -> None:
        try:
            if self.active is None:
                self.invalid = True
                return
            window = self.windows[self.active]
            if window[0]:
                window[2] = True
            window[0] = True
            parsed = _classify_frame(data)
            if parsed is None or not parsed[3] or parsed[0] != self.expected:
                window[2] = True
            elif window[1] is None:
                window[1] = parsed[1]
        except Exception:  # noqa: BLE001
            self.invalid = True
        finally:
            self.event.set()

    def outcome(self, role: str) -> str:
        window = self.windows.get(role)
        if window is None:
            return "not_attempted"
        seen, code, ambiguous = window
        if self.invalid or not seen or ambiguous or code is None:
            return "ambiguous"
        return "accepted" if code == 0x00 else "rejected"

    def export(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for role in ("auth", "action"):
            out[f"{role}_outcome"] = self.outcome(role)
            out[f"{role}_status"] = self.windows.get(role, [False, None, False])[1]
        return out


def _src(value: Any) -> str:
    return value.upper() if isinstance(value, str) else ""


def _safe_ident(value: Any) -> str | None:
    if value is None:
        return None
    return (
        value
        if (isinstance(value, str) and _IDENT.fullmatch(value) and not re.search(r"[0-9A-Fa-f]{8,}", value))
        else "Exception"
    )


def _sanitize_result(result: dict[str, Any], *, address: str | None) -> dict[str, Any]:
    """Allowlisted codes only — never auth packets, notify bytes, or free-form MACs in logs."""
    out: dict[str, Any] = {
        "connected": result.get("connected") is True,
        "success": result.get("success") is True,
        "outcome": result.get("outcome")
        if result.get("outcome") in ("accepted", "rejected", "ambiguous")
        else "ambiguous",
        "auth_outcome": result.get("auth_outcome")
        if result.get("auth_outcome")
        in ("accepted", "rejected", "ambiguous", "not_attempted")
        else "not_attempted",
        "action_outcome": result.get("action_outcome")
        if result.get("action_outcome")
        in ("accepted", "rejected", "ambiguous", "not_attempted")
        else "not_attempted",
        "stop_only": result.get("stop_only") is True,
        "line": result.get("line")
        if isinstance(result.get("line"), int) and result["line"] in MANUAL_LINES
        else None,
        "minutes": result.get("minutes")
        if isinstance(result.get("minutes"), int)
        and MANUAL_MIN_MINUTES <= result["minutes"] <= MANUAL_MAX_MINUTES
        else None,
        "writes": result.get("writes")
        if isinstance(result.get("writes"), int) and 0 <= result["writes"] <= 2
        else 0,
        "error": None
        if result.get("error") is None
        else (result["error"] if result["error"] in ERRORS else "internal_error"),
        "error_type": _safe_ident(result.get("error_type")),
        "disconnected": result.get("disconnected")
        if isinstance(result.get("disconnected"), bool)
        else None,
        "disconnect_error": _safe_ident(result.get("disconnect_error")),
        "address_configured": bool(address),
        "source_matched": result.get("source_matched"),
        "verification": result.get("verification")
        if result.get("verification") in ("live", "re_only", "stop_re_only")
        else None,
    }
    for key in ("connect_s", "elapsed_s", "auth_status", "action_status", "rssi"):
        val = result.get(key)
        if isinstance(val, bool):
            continue
        if isinstance(val, (int, float)):
            out[key] = val
    return out



def _set_semantic_outcome(result: dict[str, Any]) -> None:
    result["success"] = False
    observed = (
        result.get("action_outcome")
        if result.get("auth_outcome") == "accepted"
        else result.get("auth_outcome")
    )
    result["outcome"] = observed if observed in ("accepted", "rejected") else "ambiguous"
    if result.get("error") is None and (
        result.get("disconnected") is not True or result.get("disconnect_error") is not None
    ):
        result["error"] = "disconnect_failed"
    if result.get("error") is not None:
        result["outcome"] = "ambiguous" if observed != "rejected" else "rejected"
    result["success"] = result["outcome"] == "accepted" and result.get("error") is None


def _connected_source(client: Any) -> str | None:
    try:
        src = getattr(getattr(client, "_connected_scanner", None), "source", None)
    except Exception:  # noqa: BLE001
        return None
    return src.upper() if isinstance(src, str) and src else None


def _make_pinned_client_class(base: Any, *, address: str, proxy_source: str) -> type | None:
    hooks = (
        "_async_get_best_available_backend_and_device",
        "_async_get_backend_for_ble_device",
    )
    if not isinstance(base, type) or not all(callable(getattr(base, h, None)) for h in hooks):
        return None
    wanted = proxy_source.upper()
    target = _norm_mac(address)

    class ProxyPinnedClient(base):  # type: ignore[misc, valid-type]
        def _async_get_best_available_backend_and_device(self, manager: Any) -> Any:
            for dev in manager.async_scanner_devices_by_address(target, True):
                if _src(getattr(dev.scanner, "source", None)) != wanted:
                    continue
                backend = self._async_get_backend_for_ble_device(
                    manager, dev.scanner, dev.ble_device
                )
                if backend is None:
                    raise ProbeAbort("proxy_no_slot")
                if _src(getattr(backend, "source", None)) != wanted:
                    raise ProbeAbort("proxy_source_mismatch")
                return backend
            raise ProbeAbort("proxy_not_seen")

    return ProxyPinnedClient


def _load_pinned_client_class(address: str, proxy_source: str) -> type | None:
    try:
        from habluetooth import HaBleakClientWrapper
    except Exception:  # noqa: BLE001
        return None
    return _make_pinned_client_class(
        HaBleakClientWrapper, address=address, proxy_source=proxy_source
    )


def _consume_task_result(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded_disconnect(client: Any, timeout: float) -> tuple[bool, str | None]:
    error: str | None = None
    try:
        task = asyncio.ensure_future(client.disconnect())
    except Exception as err:  # noqa: BLE001
        task, error = None, type(err).__name__
    if task is not None:
        task.add_done_callback(_consume_task_result)
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if not done:
            task.cancel()
            error = "TimeoutError"
        elif task.cancelled():
            error = "CancelledError"
        elif task.exception() is not None:
            error = type(task.exception()).__name__
    try:
        still_connected = client.is_connected
    except Exception:  # noqa: BLE001
        still_connected = None
    return still_connected is False, error


def _validate_params(line: int, minutes: int, stop_only: bool) -> str | None:
    if type(stop_only) is not bool:
        return "fixed_parameters_required"
    if stop_only:
        return None
    if type(line) is not int or line not in MANUAL_LINES:
        return "invalid_line"
    if type(minutes) is not int or not MANUAL_MIN_MINUTES <= minutes <= MANUAL_MAX_MINUTES:
        return "invalid_minutes"
    return None


async def run_manual_once(
    hass: HomeAssistant,
    *,
    address: str,
    preferred_source: str | None,
    line: int = 1,
    minutes: int = 1,
    stop_only: bool = False,
    program: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Execute one bounded auth+action session for the configured Hydro address.

    ``client_factory`` is for offline tests only (fake BLE). Production path
    always uses the pinned HaBleakClientWrapper.
    """
    if program is not None:
        if program not in ("A", "B"):
            return _sanitize_result(
                {"error": "invalid_program", "writes": 0, "stop_only": False, "program": program},
                address=address,
            )
        if stop_only:
            return _sanitize_result(
                {"error": "invalid_params", "writes": 0, "stop_only": True, "program": program},
                address=address,
            )
        bad = None
    else:
        bad = _validate_params(line, minutes, stop_only)
    if bad:
        return _sanitize_result({"error": bad, "writes": 0, "stop_only": stop_only}, address=address)

    address = _norm_mac(address)
    if program is not None:
        action_pkt = build_program_start(program)
        verification = f"program_{program}_re_only"
    else:
        action_pkt = build_manual_stop() if stop_only else build_manual_start(line, minutes)
        verification = _verification_tag(stop_only=stop_only, line=line, minutes=minutes)

    deadline = Deadline(TOTAL_DEADLINE, clock)
    result: dict[str, Any] = {
        "connected": False,
        "error": None,
        "writes": 0,
        "disconnected": None,
        "disconnect_error": None,
        "stop_only": stop_only,
        "program": program,
        "line": None if (stop_only or program) else line,
        "minutes": None if (stop_only or program) else minutes,
        "source_matched": None,
        "verification": verification,
    }
    tally = FrameTally()
    client: Any = None

    async def _bounded(awaitable: Any, cap: float, code: str) -> Any:
        budget = deadline.step(cap, CLEANUP_RESERVE)
        if budget <= 0:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise ProbeAbort("deadline_exceeded")
        try:
            async with asyncio.timeout(budget):
                return await awaitable
        except TimeoutError:
            raise ProbeAbort(code if budget >= cap else "deadline_exceeded") from None

    try:
        async with asyncio.timeout(deadline.remaining(CLEANUP_RESERVE)):
            si = bluetooth.async_last_service_info(hass, address, connectable=True)
            if si is None:
                raise ProbeAbort("proxy_not_seen")
            last_source = _src(getattr(si, "source", None))
            result["rssi"] = getattr(si, "rssi", None)
            if preferred_source:
                wanted = preferred_source.upper()
                result["source_matched"] = last_source == wanted
                if last_source != wanted:
                    raise ProbeAbort("proxy_not_last_source")
            else:
                result["source_matched"] = True
                wanted = last_source
                if not wanted:
                    raise ProbeAbort("proxy_not_seen")

            devices = bluetooth.async_scanner_devices_by_address(
                hass, address, connectable=True
            )
            proxy_dev = None
            for dev in devices or ():
                if _src(getattr(getattr(dev, "scanner", None), "source", None)) == wanted:
                    proxy_dev = dev
                    break
            if proxy_dev is None and client_factory is None:
                raise ProbeAbort("proxy_not_seen")

            if client_factory is None:
                client_class = _load_pinned_client_class(address, wanted)
                if client_class is None:
                    raise ProbeAbort("proxy_source_unsupported")
            else:
                client_class = None

            try:
                auth = build_auth_packet(address)
            except ValueError:
                raise ProbeAbort("auth_input_format") from None
            pending = [auth, action_pkt]

            async def _write(pkt: bytes) -> None:
                if not pending or pkt != pending[0]:
                    raise ProbeAbort("write_not_allowlisted")
                pending.pop(0)
                result["writes"] += 1
                await _bounded(
                    client.write_gatt_char(WRITE_UUID, pkt, response=False),
                    WRITE_TIMEOUT,
                    "write_timeout",
                )

            connect_cap = deadline.step(CONNECT_TIMEOUT, CLEANUP_RESERVE)
            if client_factory is not None:
                client = client_factory(
                    address=address,
                    source=wanted,
                    ble_device=getattr(proxy_dev, "ble_device", None),
                    timeout=connect_cap,
                )
            else:
                assert proxy_dev is not None and client_class is not None
                client = client_class(proxy_dev.ble_device, timeout=connect_cap)
            t0 = clock()
            try:
                await _bounded(client.connect(), CONNECT_TIMEOUT, "connect_timeout")
            except ProbeAbort:
                raise
            except Exception as err:  # noqa: BLE001
                result["error_type"] = type(err).__name__
                raise ProbeAbort("connect_failed") from None
            result["connected"] = True
            result["connect_s"] = clock() - t0

            via = _connected_source(client)
            if via is None:
                # Fake clients may expose source via attribute.
                via = _src(getattr(client, "source", None)) or None
            if via is None:
                raise ProbeAbort("proxy_source_unverified")
            if via != wanted:
                raise ProbeAbort("proxy_source_mismatch")

            try:
                await _bounded(
                    client.start_notify(NOTIFY_UUID, lambda _h, d: tally.feed(d)),
                    NOTIFY_SETUP_TIMEOUT,
                    "notify_setup_timeout",
                )
                if tally.invalid:
                    raise ProbeAbort("ack_ambiguous")
                tally.arm("auth", 0x00)
                await _write(auth)
                await _bounded(tally.event.wait(), NOTIFY_WAIT, "ack_missing")
                if tally.outcome("auth") != "accepted":
                    raise ProbeAbort(
                        "auth_rejected"
                        if tally.outcome("auth") == "rejected"
                        else "ack_ambiguous"
                    )
                if deadline.remaining(CLEANUP_RESERVE) < WRITE_TIMEOUT + NOTIFY_WAIT:
                    raise ProbeAbort("refused_budget")
                tally.arm("action", 0x0C)
                await _write(action_pkt)
                await _bounded(tally.event.wait(), NOTIFY_WAIT, "ack_missing")
            except ProbeAbort:
                raise
            except Exception as err:  # noqa: BLE001
                result["error_type"] = type(err).__name__
                raise ProbeAbort("gatt_error") from None
    except ProbeAbort as abort:
        result["error"] = abort.code
    except TimeoutError:
        result["error"] = "deadline_exceeded"
    except Exception as err:  # noqa: BLE001
        result["error"] = "internal_error"
        result["error_type"] = type(err).__name__
    finally:
        if client is not None:
            result["disconnected"], result["disconnect_error"] = await _bounded_disconnect(
                client, DISCONNECT_TIMEOUT
            )
        result.update(tally.export())
        result["elapsed_s"] = deadline.elapsed()
        _set_semantic_outcome(result)
    return _sanitize_result(result, address=address)


def _hydro_entry(hass: HomeAssistant, prefer_entry_id: str | None = None):
    store = hass.data.get(DOMAIN, {})
    preferred = None
    for ent_id, payload in store.items():
        if not isinstance(payload, dict):
            continue
        entry = payload.get("entry")
        if entry is None:
            continue
        if not is_hydro_family(entry.data.get(CONF_DEVICE_KIND)):
            continue
        if prefer_entry_id and ent_id == prefer_entry_id:
            return entry
        if preferred is None:
            preferred = entry
    return preferred


def get_stored_duration(hass: HomeAssistant, entry_id: str) -> int:
    store = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    value = store.get(STORE_DURATION_KEY, DEFAULT_MANUAL_MINUTES)
    if type(value) is not int or not MANUAL_MIN_MINUTES <= value <= MANUAL_MAX_MINUTES:
        return DEFAULT_MANUAL_MINUTES
    return value


def set_stored_duration(hass: HomeAssistant, entry_id: str, minutes: int) -> None:
    if type(minutes) is not int or not MANUAL_MIN_MINUTES <= minutes <= MANUAL_MAX_MINUTES:
        raise ValueError("invalid_minutes")
    store = hass.data.setdefault(DOMAIN, {}).setdefault(entry_id, {})
    store[STORE_DURATION_KEY] = minutes


async def execute_command(
    hass: HomeAssistant,
    *,
    entry_id: str,
    line: int | None = None,
    minutes: int | None = None,
    stop_only: bool = False,
    program: str | None = None,
) -> dict[str, Any]:
    """Run one command under the domain lock (entity and service entrypoint)."""
    hass.data.setdefault(DOMAIN, {})
    if LOCK_KEY not in hass.data[DOMAIN]:
        hass.data[DOMAIN][LOCK_KEY] = asyncio.Lock()
    lock: asyncio.Lock = hass.data[DOMAIN][LOCK_KEY]
    if lock.locked():
        entry = _hydro_entry(hass, entry_id)
        return _sanitize_result(
            {"error": "busy", "writes": 0, "stop_only": stop_only, "program": program},
            address=getattr(entry, "unique_id", None),
        )

    ack_address = ""
    ack_line = line
    ack_minutes = minutes
    ack_stop = stop_only
    async with lock:
        entry = _hydro_entry(hass, entry_id)
        if entry is None:
            return _sanitize_result({"error": "no_config_entry", "writes": 0}, address=None)
        address = entry.unique_id
        if not address:
            return _sanitize_result({"error": "no_config_entry", "writes": 0}, address=None)
        if program is not None:
            use_line, use_minutes, use_stop = 1, 1, False
        elif stop_only:
            use_line, use_minutes, use_stop = 1, 1, True
        else:
            use_line = 1 if line is None else line
            allowed = manual_lines_for_kind(entry.data.get(CONF_DEVICE_KIND))
            if use_line not in allowed:
                return _sanitize_result(
                    {
                        "error": "invalid_line",
                        "writes": 0,
                        "stop_only": False,
                        "line": use_line,
                    },
                    address=address,
                )
            use_minutes = (
                get_stored_duration(hass, entry.entry_id) if minutes is None else minutes
            )
            use_stop = False
        result = await run_manual_once(
            hass,
            address=address,
            preferred_source=entry.options.get(CONF_BLUETOOTH_SOURCE) or None,
            line=use_line,
            minutes=use_minutes,
            stop_only=use_stop,
            program=program,
        )
        ack_address = address
        ack_line = use_line
        ack_minutes = use_minutes
        ack_stop = use_stop
    _LOGGER.info("claber_bt manual command: %s", result)
    try:
        from .optimistic_state import apply_after_manual_ack

        apply_after_manual_ack(
            hass,
            entry_id,
            address=ack_address,
            stop_only=ack_stop,
            line=ack_line,
            minutes=ack_minutes,
            program=program,
            success=bool(result.get("success")),
        )
    except Exception:  # noqa: BLE001
        _LOGGER.exception("claber_bt optimistic state after ACK failed")
    return result


SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("line", default=1): vol.All(
            vol.Coerce(int), vol.In(list(MANUAL_LINES))
        ),
        vol.Optional("minutes", default=DEFAULT_MANUAL_MINUTES): vol.All(
            vol.Coerce(int),
            vol.Range(min=MANUAL_MIN_MINUTES, max=MANUAL_MAX_MINUTES),
        ),
        vol.Optional("stop_only", default=False): cv.boolean,
    }
)


async def async_register_manual_once(
    hass: HomeAssistant, entry_id: str
) -> Callable[[], None]:
    """Register the domain service if not already present; returns unregister."""
    hass.data.setdefault(DOMAIN, {})
    if LOCK_KEY not in hass.data[DOMAIN]:
        hass.data[DOMAIN][LOCK_KEY] = asyncio.Lock()

    async def _handler(call: ServiceCall) -> ServiceResponse:
        return await execute_command(
            hass,
            entry_id=entry_id,
            line=call.data.get("line", 1),
            minutes=call.data.get("minutes", DEFAULT_MANUAL_MINUTES),
            stop_only=call.data.get("stop_only", False),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_MANUAL_ONCE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_MANUAL_ONCE,
            _handler,
            schema=SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )

    def _unregister() -> None:
        still = False
        for ent_id, payload in hass.data.get(DOMAIN, {}).items():
            if ent_id == LOCK_KEY:
                continue
            if not isinstance(payload, dict):
                continue
            entry = payload.get("entry")
            if entry is not None and is_hydro_family(
                entry.data.get(CONF_DEVICE_KIND)
            ):
                still = True
                break
        if not still and hass.services.has_service(DOMAIN, SERVICE_MANUAL_ONCE):
            hass.services.async_remove(DOMAIN, SERVICE_MANUAL_ONCE)

    return _unregister
