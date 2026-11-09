"""Parse BluetoothServiceInfoBleak into Claber updates (passive only).

No client connections, no GATT, no writes. Manufacturer IDs are rebuilt from
the current advertisement (prefer ``raw``) and never guessed from a multi-key
merged manufacturer_data dict unless exactly one novel company-id appears
(INFERRED rx-cache mitigation; see ``msd_from_service_info``).

Trust / provenance
------------------
HA PassiveBluetooth processors restore entity_data across restarts, and the
Bluetooth manager replays advertisement history when a coordinator starts.
Local-adapter history restore even remaps ``service_info.time`` to
``now_monotonic``, so age alone cannot prove RF freshness after reboot.

This parser therefore treats the **first** service_info seen in a process
session as untrusted provenance (history/cache possible). A later callback
whose ``service_info.time`` is **strictly greater** than the first confirms
live reception. Volatile entity publishers must not present untrusted ON as
current. This does **not** invent advertising intervals or force OFF from
remaining_minutes.

Hydro-4 BT live state is in the on-air manufacturer AD **including** the two
company-ID bytes (Rea / app ``_parseHydroData``, 2026-11-09). HA's
``manufacturer_data`` dict accumulates old company IDs and must not be used for
Hydro. Trustworthy Hydro **and Rain** state requires ``service_info.raw``; without
raw the values stay unknown (Rain temperature is also encoded in company-ID
bytes; merged manufacturer_data keys are ambiguous). GATT status is not used
(``build_hydro_status_read`` stays unimplemented).
"""

from __future__ import annotations

STALE_TRUSTWORTHY_SECONDS = 900.0  # 15 minutes

import time
from dataclasses import dataclass, field
from typing import Any

try:
    from .claber_passive import (
        LOCAL_NAME,
        DecodeError,
        classify,
        decode,
        ha_from_raw,
        msd_from_adv,
        msd_from_service_info,
    )
    from .const import SUPPORTED_DEVICE_KINDS
except ImportError:  # unittest without package context
    from claber_passive import (  # type: ignore[no-redef]
        LOCAL_NAME,
        DecodeError,
        classify,
        decode,
        ha_from_raw,
        msd_from_adv,
        msd_from_service_info,
    )
    from const import SUPPORTED_DEVICE_KINDS  # type: ignore[no-redef]


@dataclass(slots=True)
class ClaberUpdate:
    """One decoded advertisement (or a keep-alive with previous fields)."""

    address: str
    device_kind: str
    name: str | None
    rssi: int | None
    fields: dict[str, Any] = field(default_factory=dict)
    # True when this callback only refreshed presence (ambiguous / ignored frame)
    values_updated: bool = True
    # Reception / decode observability (no MAC / no address-suffix bytes)
    had_raw: bool = False
    manufacturer_key_count: int = 0
    company_id: int | None = None
    selection: str = "none"  # raw | single_key | novel_key | ambiguous | error
    # Provenance: True only after a post-start live advert (time advanced)
    trustworthy: bool = False
    advert_time: float | None = None
    advert_age_seconds: float | None = None
    live_confirmed: bool = False
    trustworthy_values_age_seconds: float | None = None
    # True when Hydro was seen but raw was missing (presence only).
    presence_only: bool = False


@dataclass(slots=True)
class ReceptionMeta:
    """Last reception attempt metadata (distinct from entity last_updated)."""

    had_raw: bool = False
    manufacturer_key_count: int = 0
    company_id: int | None = None
    selection: str = "none"
    values_updated: bool = False
    monotonic: float | None = None
    trustworthy: bool = False
    advert_time: float | None = None
    advert_age_seconds: float | None = None
    live_confirmed: bool = False


class ClaberBluetoothDeviceData:
    """Stateful passive parser for one configured Claber device."""

    def __init__(self, expected_kind: str) -> None:
        if expected_kind not in SUPPORTED_DEVICE_KINDS:
            raise ValueError(f"unsupported device kind: {expected_kind}")
        self.expected_kind = expected_kind
        self._last: ClaberUpdate | None = None
        self._last_trustworthy: ClaberUpdate | None = None
        self.last_service_info: Any | None = None
        # Monotonic time of the last successfully decoded value payload.
        self.last_values_monotonic: float | None = None
        self.last_trustworthy_values_monotonic: float | None = None
        # Company-id keys observed across callbacks (habluetooth merge memory).
        self._seen_company_ids: set[int] = set()
        self.last_reception: ReceptionMeta = ReceptionMeta()
        # Session provenance (resets every setup_entry / process start).
        self._first_advert_time: float | None = None
        self._live_confirmed: bool = False
        self._callback_count: int = 0

    @property
    def last_update(self) -> ClaberUpdate | None:
        """Return the last update object (may be untrusted keepalive/decode)."""
        return self._last

    @property
    def last_trustworthy_update(self) -> ClaberUpdate | None:
        """Return the last decode with live-confirmed provenance, if any."""
        return self._last_trustworthy

    def force_republish(self) -> ClaberUpdate | None:
        """Republish the last trustworthy update (no radio). For the refresh button."""
        src = self._last_trustworthy or (
            self._last if self._last and self._last.trustworthy else None
        )
        if src is None or not src.fields:
            return None
        age = None
        if self.last_trustworthy_values_monotonic is not None:
            age = max(0.0, time.monotonic() - self.last_trustworthy_values_monotonic)
        update = ClaberUpdate(
            address=src.address,
            device_kind=src.device_kind,
            name=src.name,
            rssi=src.rssi,
            fields=dict(src.fields),
            values_updated=True,
            had_raw=src.had_raw,
            manufacturer_key_count=src.manufacturer_key_count,
            company_id=src.company_id,
            selection=src.selection,
            trustworthy=src.trustworthy,
            advert_time=src.advert_time,
            advert_age_seconds=src.advert_age_seconds,
            live_confirmed=src.live_confirmed,
            trustworthy_values_age_seconds=age,
            presence_only=False,
        )
        self._last = update
        self.last_reception.values_updated = True
        self.last_reception.trustworthy = bool(src.trustworthy)
        return update


    def supported(self, service_info: Any) -> str | None:
        """Return expected kind if this advertisement matches this device."""
        full, _meta = self._current_full_msd(service_info, remember=False)
        if full is None:
            return None
        try:
            kind = classify(full, service_info.address, service_info.name)
        except DecodeError:
            return None
        if kind == self.expected_kind:
            return kind
        return None

    def _evaluate_live(self, service_info: Any) -> tuple[float | None, float | None, bool]:
        """Return (advert_time, age_seconds, live_confirmed for this callback)."""
        now = time.monotonic()
        advert_time = getattr(service_info, "time", None)
        if not isinstance(advert_time, (int, float)):
            advert_time = None
        age = None if advert_time is None else max(0.0, now - float(advert_time))

        if advert_time is None:
            # No provenance timestamp: never upgrade to live on this path alone.
            return None, age, self._live_confirmed

        if self._first_advert_time is None:
            # First callback in this session: may be history replay / remapped time.
            self._first_advert_time = float(advert_time)
            return float(advert_time), age, False

        if float(advert_time) > self._first_advert_time:
            self._live_confirmed = True
            return float(advert_time), age, True

        # Identical Hydro raw frames may keep the same service_info.time; a
        # second-or-later callback in-session is still live RF (Rea/passive).
        if self._callback_count >= 2:
            self._live_confirmed = True
            return float(advert_time), age, True

        return float(advert_time), age, self._live_confirmed

    def update(self, service_info: Any) -> ClaberUpdate:
        """Parse one advertisement callback into a ClaberUpdate."""
        address = service_info.address
        name = getattr(service_info, "name", None)
        rssi = getattr(service_info, "rssi", None)
        has_raw = bool(getattr(service_info, "raw", None))
        # Hydro status is raw-only (Rea). Count / first_advert_time only for
        # raw callbacks so history or manufacturer_data-only frames cannot
        # poison live detection or unlock trustworthy publish.
        if has_raw:
            self._callback_count += 1
        if self.expected_kind == "hydro4" and not has_raw:
            advert_time = getattr(service_info, "time", None)
            if not isinstance(advert_time, (int, float)):
                advert_time = None
            now = time.monotonic()
            advert_age = None if advert_time is None else max(0.0, now - float(advert_time))
            live = self._live_confirmed
        else:
            advert_time, advert_age, live = self._evaluate_live(service_info)

        full, meta = self._current_full_msd(service_info, remember=True)
        self.last_reception = ReceptionMeta(
            had_raw=meta["had_raw"],
            manufacturer_key_count=meta["manufacturer_key_count"],
            company_id=meta["company_id"],
            selection=meta["selection"],
            values_updated=False,
            monotonic=time.monotonic(),
            trustworthy=False,
            advert_time=advert_time,
            advert_age_seconds=advert_age,
            live_confirmed=live,
        )
        if full is None:
            return self._keepalive(address, name, rssi, meta, advert_time, advert_age, live)

        try:
            kind = classify(full, address, name)
        except DecodeError:
            return self._keepalive(address, name, rssi, meta, advert_time, advert_age, live)

        if kind != self.expected_kind:
            return self._keepalive(address, name, rssi, meta, advert_time, advert_age, live)

        try:
            decoded = decode(full, address, name)
        except DecodeError:
            return self._keepalive(address, name, rssi, meta, advert_time, advert_age, live)

        if decoded is None:
            return self._keepalive(address, name, rssi, meta, advert_time, advert_age, live)

        company_id, _ = ha_from_raw(full)
        self._seen_company_ids.add(company_id)

        fields = {k: v for k, v in decoded.items() if k != "device_kind"}
        # Hydro + Rain: live state lives in company-ID bytes of the on-air AD.
        # HA manufacturer_data merges historical company IDs (Rain temp changes
        # the key). Without raw, never publish values as trustworthy.
        if kind in ("hydro4", "rain_sensor"):
            trustworthy = (
                bool(live)
                and bool(meta.get("had_raw"))
                and meta.get("selection") == "raw"
            )
            if not trustworthy:
                fields = {}
        else:
            trustworthy = bool(live)
        update = ClaberUpdate(
            address=address,
            device_kind=kind,
            name=name or LOCAL_NAME,
            rssi=rssi,
            fields=fields,
            values_updated=True,
            had_raw=meta["had_raw"],
            manufacturer_key_count=meta["manufacturer_key_count"],
            company_id=company_id,
            selection=meta["selection"],
            trustworthy=trustworthy,
            advert_time=advert_time,
            advert_age_seconds=advert_age,
            live_confirmed=live,
            presence_only=(kind == "hydro4" and not trustworthy),
        )
        if self.last_trustworthy_values_monotonic is not None:
            update.trustworthy_values_age_seconds = max(
                0.0, time.monotonic() - self.last_trustworthy_values_monotonic
            )
        self._last = update
        self.last_service_info = service_info
        self.last_values_monotonic = time.monotonic()
        self.last_reception.values_updated = True
        self.last_reception.trustworthy = trustworthy
        self.last_reception.company_id = company_id
        if trustworthy:
            self._last_trustworthy = update
            self.last_trustworthy_values_monotonic = self.last_values_monotonic
            update.trustworthy_values_age_seconds = 0.0
        return update

    def _keepalive(
        self,
        address: str,
        name: str | None,
        rssi: int | None,
        meta: dict[str, Any],
        advert_time: float | None,
        advert_age: float | None,
        live: bool,
    ) -> ClaberUpdate:
        age = None
        if self.last_trustworthy_values_monotonic is not None:
            age = max(0.0, time.monotonic() - self.last_trustworthy_values_monotonic)
        stale = age is not None and age > STALE_TRUSTWORTHY_SECONDS
        if self._last is not None:
            return ClaberUpdate(
                address=address,
                device_kind=self._last.device_kind,
                name=name or self._last.name,
                rssi=rssi if rssi is not None else self._last.rssi,
                fields={} if stale else dict(self._last.fields),
                values_updated=bool(stale),
                had_raw=meta["had_raw"],
                manufacturer_key_count=meta["manufacturer_key_count"],
                company_id=meta["company_id"],
                selection=meta["selection"],
                trustworthy=False,
                advert_time=advert_time,
                advert_age_seconds=advert_age,
                live_confirmed=live,
                trustworthy_values_age_seconds=age,
            )
        return ClaberUpdate(
            address=address,
            device_kind=self.expected_kind,
            name=name,
            rssi=rssi,
            fields={},
            values_updated=False,
            had_raw=meta["had_raw"],
            manufacturer_key_count=meta["manufacturer_key_count"],
            company_id=meta["company_id"],
            selection=meta["selection"],
            trustworthy=False,
            advert_time=advert_time,
            advert_age_seconds=advert_age,
            live_confirmed=live,
            trustworthy_values_age_seconds=age,
        )

    def _current_full_msd(
        self, service_info: Any, *, remember: bool
    ) -> tuple[bytes | None, dict[str, Any]]:
        raw = getattr(service_info, "raw", None)
        manufacturer_data = getattr(service_info, "manufacturer_data", {}) or {}
        meta: dict[str, Any] = {
            "had_raw": bool(raw),
            "manufacturer_key_count": len(manufacturer_data),
            "company_id": None,
            "selection": "ambiguous",
        }

        # Hydro-4: ONLY the on-air raw AD (Rea). Accumulated manufacturer_data
        # keys (0/1/2/4/8/305) are historical company IDs, not current state.
        if self.expected_kind == "hydro4":
            if not raw:
                meta["selection"] = "no_raw"
                if remember:
                    self._seen_company_ids.update(manufacturer_data.keys())
                return None, meta
            try:
                full = msd_from_adv(bytes(raw))
            except DecodeError:
                meta["selection"] = "error"
                return None, meta
            if full is None:
                meta["selection"] = "no_msd"
                return None, meta
            try:
                cid, _ = ha_from_raw(full)
            except DecodeError:
                meta["selection"] = "error"
                return None, meta
            meta["company_id"] = cid
            meta["selection"] = "raw"
            if remember:
                self._seen_company_ids.update(manufacturer_data.keys())
                self._seen_company_ids.add(cid)
            return full, meta

        try:
            full = msd_from_service_info(
                raw,
                manufacturer_data,
                self._seen_company_ids,
                address=getattr(service_info, "address", None),
                name=getattr(service_info, "name", None),
                expected_kind=self.expected_kind,
            )
        except DecodeError:
            meta["selection"] = "error"
            if remember:
                self._seen_company_ids.update(manufacturer_data.keys())
            return None, meta

        if full is None:
            meta["selection"] = "ambiguous"
            if remember:
                self._seen_company_ids.update(manufacturer_data.keys())
            return None, meta

        cid, _ = ha_from_raw(full)
        meta["company_id"] = cid
        if raw:
            meta["selection"] = "raw"
        elif len(manufacturer_data) == 1:
            meta["selection"] = "single_key"
        else:
            novel = set(manufacturer_data) - set(self._seen_company_ids)
            meta["selection"] = "novel_key" if len(novel) == 1 and cid in novel else "matched_key"
        if remember:
            self._seen_company_ids.update(manufacturer_data.keys())
            self._seen_company_ids.add(cid)
        return full, meta
