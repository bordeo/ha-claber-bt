"""Claber Hydro-4 BT / Rain Sensor BT passive advertisement decoder.

Pure Python, standard library only, no Home Assistant imports, no I/O.
Passive scope: it decodes bytes that were already received. It never scans,
connects, reads GATT or writes anything.

Index convention used everywhere in this module ("full MSD", see SPEC.md):
    full = company_id_le (2 bytes) + manufacturer_value (HA dict value)
so full[0] and full[1] are the two bytes HA turns into the dict KEY.
"""
from __future__ import annotations

FULL_MSD_LEN = 9          # OBSERVED for both devices (2 company-ID bytes + 7)
MIN_DECODE_LEN = 6        # the app parsers index up to full[5]
TEMP_OFFSET = 10          # PROVED: degrees C = unsigned byte - 10
ENERGY_LEVELS = "FEDCBA"  # PROVED: code 0..5 -> F..A
RAIN_BATTERY_PCT = (16, 20, 25, 33, 50, 100)  # PROVED app display, code 0..5
OPENING_PROGRAMS = (      # PROVED app order 0..5; 0.1.9 HA-facing names
    "none", "A", "B", "manual",
    "semi_auto_A", "semi_auto_B",
)
MODEL_CODES = {           # PROVED app table for the low nibble of full[4]
    1: "hydro4",
    2: "modulo4",
    3: "elettrovalvola",
    4: "rain_sensor",
}
LOCAL_NAME = "Claber"     # OBSERVED advertised name; app compares to it


class DecodeError(ValueError):
    """Raised for payloads the app itself would reject or cannot index."""


def raw_from_ha(company_id: int, data: bytes) -> bytes:
    """Rebuild the full MSD from one HA ``manufacturer_data`` item.

    HA/bleak strip the two little-endian company-ID bytes into the dict key.
    Claber puts live data in those bytes, so they must be put back.
    """
    if not isinstance(company_id, int) or not 0 <= company_id <= 0xFFFF:
        raise DecodeError("company_id must be an int in 0..65535")
    return company_id.to_bytes(2, "little") + bytes(data)


def ha_from_raw(full: bytes) -> tuple[int, bytes]:
    """Inverse of raw_from_ha (for tests and logging)."""
    if len(full) < 2:
        raise DecodeError("full MSD shorter than the company-ID field")
    return int.from_bytes(full[:2], "little"), bytes(full[2:])


def msd_from_adv(adv: bytes) -> bytes | None:
    """Return the first manufacturer-specific AD structure (type 0xFF) of a raw
    advertisement, INCLUDING its two company-ID bytes. None if absent.
    Raises DecodeError on a truncated AD structure."""
    i = 0
    adv = bytes(adv)
    while i < len(adv):
        size = adv[i]
        if size == 0:
            break
        if i + 1 + size > len(adv):
            raise DecodeError("truncated AD structure")
        if adv[i + 1] == 0xFF:
            return adv[i + 2:i + 1 + size]
        i += size + 1
    return None


def msd_from_service_info(
    raw_adv: bytes | None,
    manufacturer_data: dict[int, bytes],
    seen_company_ids: set[int] | frozenset[int] | None = None,
    *,
    address: str | None = None,
    name: str | None = None,
    expected_kind: str | None = None,
) -> bytes | None:
    """Choose the CURRENT full MSD for one callback.

    1. Prefer the raw advertisement (habluetooth ``BluetoothServiceInfoBleak.raw``).
    2. Without raw, accept the dict only if it has exactly one key: habluetooth
       merges old and new keys, and key order does not tell which is current.
    3. Without raw and with multiple keys: if ``seen_company_ids`` is provided and
       exactly **one** company-id key is novel (not previously observed for this
       device), use that key. INFERRED mitigation for habluetooth merge/rx-cache
       when ``raw`` is absent — not a substitute for raw, and not a guess among
       several novel keys.
    4. Without raw and still ambiguous: if ``address`` (and optional
       ``expected_kind`` / ``name``) are provided, accept a key only when rebuilt
       MSD ``classify()`` matches exactly one candidate. OBSERVED need: ESPHome
       proxy callbacks often omit ``raw`` while habluetooth keeps multiple merged
       company-id keys (Claber encodes live data in the company-id bytes).
    4b. Without raw, multiple classify matches, but all share the **identical**
       7-byte value: accept that payload (prefer company_id 0, else lowest cid).
       OBSERVED Hydro-4 static beacon with identical payload under keys 0/1/305/….
    5. Otherwise return None (ambiguous): keep the previous state, do not guess.
    """
    if raw_adv:
        found = msd_from_adv(raw_adv)
        if found is not None:
            return found
    if len(manufacturer_data) == 1:
        (cid, value), = manufacturer_data.items()
        return raw_from_ha(cid, value)
    if seen_company_ids is not None and len(manufacturer_data) > 1:
        novel = set(manufacturer_data) - set(seen_company_ids)
        if len(novel) == 1:
            cid = next(iter(novel))
            return raw_from_ha(cid, manufacturer_data[cid])
    if address is not None and len(manufacturer_data) > 1:
        matched: list[bytes] = []
        # History/proxy callbacks often set name to the MAC or another label.
        # Address-suffix + model nibble are sufficient; only honor an explicit
        # Claber local name when present.
        name_for_match = name if name == LOCAL_NAME else None
        for cid, value in manufacturer_data.items():
            if not isinstance(value, (bytes, bytearray)):
                continue
            if len(bytes(value)) != FULL_MSD_LEN - 2:
                continue
            try:
                full = raw_from_ha(cid, bytes(value))
            except DecodeError:
                continue
            kind = classify(full, address, name_for_match)
            if kind is None:
                continue
            if expected_kind is not None and kind != expected_kind:
                continue
            matched.append(full)
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            # OBSERVED Hydro-4: habluetooth merges several company-id keys that
            # share the identical 7-byte value (static identity beacon; company
            # id on-air is 0). Prefer cid 0 when present; else lowest cid.
            # Do NOT guess when rebuilt value payloads differ.
            values = {bytes(full[2:]) for full in matched}
            if len(values) == 1:
                for full in matched:
                    if int.from_bytes(full[:2], "little") == 0:
                        return full
                return min(matched, key=lambda f: int.from_bytes(f[:2], "little"))
    return None


def _norm_addr(address: str) -> bytes | None:
    hexdigits = address.replace(":", "").replace("-", "")
    if len(hexdigits) != 12:
        return None
    try:
        return bytes.fromhex(hexdigits)
    except ValueError:
        return None


def address_suffix_matches(full: bytes, address: str) -> bool | None:
    """OBSERVED: full[6:9] equals the last three bytes of the device address.
    Returns None when the address is not a 48-bit MAC (e.g. a platform UUID)."""
    mac = _norm_addr(address)
    if mac is None:
        return None
    return len(full) == FULL_MSD_LEN and full[6:9] == mac[3:6]


def classify(full: bytes, address: str | None = None,
             name: str | None = None) -> str | None:
    """Return 'hydro4', 'rain_sensor', 'modulo4', 'elettrovalvola' or None.

    Checks, in order: optional local name == 'Claber'; length 9; family marker
    (full[2] high nibble == 0xF, the app's Hydro-family test); model nibble
    (full[4] & 0x0F); optional address-suffix check. Any failure -> None.
    """
    if name is not None and name != LOCAL_NAME:
        return None
    full = bytes(full)
    if len(full) != FULL_MSD_LEN:
        return None
    if (full[2] >> 4) != 0xF:
        return None
    model = MODEL_CODES.get(full[4] & 0x0F)
    if model is None:
        return None
    if address is not None and address_suffix_matches(full, address) is False:
        return None
    return model


def _energy(full: bytes) -> int:
    code = (full[5] >> 4) & 0x07
    if code > 5:
        raise DecodeError(f"energy code {code} is undefined (app rejects 6/7)")
    return code


def _check_len(full: bytes) -> bytes:
    full = bytes(full)
    if len(full) < MIN_DECODE_LEN:
        raise DecodeError(f"full MSD shorter than {MIN_DECODE_LEN} bytes")
    return full


def decode_rain(full: bytes) -> dict:
    """Rain Sensor BT 90844. ``full`` includes the 2 company-ID bytes."""
    full = _check_len(full)
    code = _energy(full)
    return {
        "temperature_c": full[0] - TEMP_OFFSET,        # PROVED
        "temperature_max_24h_c": full[1] - TEMP_OFFSET,  # PROVED
        "temperature_min_24h_c": full[3] - TEMP_OFFSET,  # PROVED
        "is_raining": bool(full[5] & 0x01),            # PROVED, active high
        "has_connected": bool(full[5] & 0x02),         # PROVED name, meaning UNKNOWN
        "has_scanned": bool(full[5] & 0x04),           # PROVED name, meaning UNKNOWN
        "energy_code": code,                           # PROVED
        "energy_level": ENERGY_LEVELS[code],           # PROVED
        "battery_percent": RAIN_BATTERY_PCT[code],     # PROVED app display only
    }


def decode_hydro(full: bytes) -> dict:
    """Hydro-4 BT 90840 (app also uses this parser for model codes 2/3)."""
    full = _check_len(full)
    code = _energy(full)
    program = (full[0] >> 4) & 0x07
    if program > 5:
        raise DecodeError(f"opening-program code {program} undefined (app rejects)")
    model_code = full[4] & 0x0F
    if model_code not in MODEL_CODES:
        raise DecodeError(f"model code {model_code} undefined (app rejects)")
    return {
        "lines_watering": [bool(full[0] & (1 << i)) for i in range(4)],  # PROVED
        "remaining_minutes": full[1],                    # PROVED unit: minutes
        "opening_program_code": program,                 # PROVED
        "opening_program": OPENING_PROGRAMS[program],    # PROVED enum names
        "model_code": model_code,                        # PROVED
        "model": MODEL_CODES[model_code],                # PROVED
        "automatic_watering_enabled": bool(full[5] & 0x01),  # PROVED
        "rain_sensor_connected": bool(full[5] & 0x02),   # PROVED
        "is_raining": bool(full[5] & 0x04),              # PROVED
        "energy_code": code,                             # PROVED
        "energy_level": ENERGY_LEVELS[code],             # PROVED
        "battery_percent": 20 * code,                    # PROVED app display only
        # Rea / app: warn ≤2 (D/40%), critical ≤1; OTA only A/B/C
        "battery_warning": code <= 2,                    # PROVED threshold from app
    }


def decode(full: bytes, address: str | None = None,
           name: str | None = None) -> dict | None:
    """classify() then decode; returns None for non-matching adverts."""
    kind = classify(full, address, name)
    if kind is None:
        return None
    data = decode_rain(full) if kind == "rain_sensor" else decode_hydro(full)
    data["device_kind"] = kind
    return data
