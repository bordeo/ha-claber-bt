"""Constants for the Claber BT integration."""

from __future__ import annotations

DOMAIN = "claber_bt"
VERSION = "0.1.14"

CONF_DEVICE_KIND = "device_kind"
CONF_BLUETOOTH_SOURCE = "preferred_bluetooth_source"
CONF_ENABLE_MANUAL_ONCE = "enable_experimental_manual_once"  # legacy ignored

DEVICE_KIND_HYDRO4 = "hydro4"
DEVICE_KIND_MODULO4 = "modulo4"
DEVICE_KIND_ELETTROVALVOLA = "elettrovalvola"
DEVICE_KIND_RAIN = "rain_sensor"

# App model nibble 1/2/3 share decode_hydro; rain is 4.
HYDRO_FAMILY_KINDS = frozenset(
    {DEVICE_KIND_HYDRO4, DEVICE_KIND_MODULO4, DEVICE_KIND_ELETTROVALVOLA}
)
SUPPORTED_DEVICE_KINDS = frozenset({*HYDRO_FAMILY_KINDS, DEVICE_KIND_RAIN})

MANUFACTURER = "Claber"

# Official myAquaSystem SKUs. Model-code → product mapping for 2/3 follows
# Claber's underground range naming (Controller-4 / Elettra), not a live capture.
MODEL_NUMBERS = {
    DEVICE_KIND_HYDRO4: "90840",
    DEVICE_KIND_MODULO4: "90842",
    DEVICE_KIND_ELETTROVALVOLA: "90843",
    DEVICE_KIND_RAIN: "90844",
}

MODEL_NAMES = {
    DEVICE_KIND_HYDRO4: "Hydro-4 BT",
    DEVICE_KIND_MODULO4: "Controller-4 BT",
    DEVICE_KIND_ELETTROVALVOLA: "Elettra-BT",
    DEVICE_KIND_RAIN: "Rain Sensor BT",
}

OPENING_PROGRAM_OPTIONS = [
    "none",
    "A",
    "B",
    "manual",
    "semi_auto_A",
    "semi_auto_B",
]

ENERGY_LEVEL_OPTIONS = list("FEDCBA")

MANUAL_MIN_MINUTES = 1
MANUAL_MAX_MINUTES = 120
MANUAL_LINES = (1, 2, 3, 4)
DEFAULT_MANUAL_MINUTES = 1

PROGRAM_IDS = ("A", "B")
WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_LABELS_IT = {
    "mon": "Lun",
    "tue": "Mar",
    "wed": "Mer",
    "thu": "Gio",
    "fri": "Ven",
    "sat": "Sab",
    "sun": "Dom",
}
START_SLOTS = (1, 2, 3, 4)


def is_hydro_family(kind: str | None) -> bool:
    """True for Hydro-4 / Controller-4 / Elettra (shared hydro advert parser)."""
    return kind in HYDRO_FAMILY_KINDS


def manual_lines_for_kind(kind: str | None) -> tuple[int, ...]:
    """Irrigation lines exposed for this device kind."""
    if kind == DEVICE_KIND_ELETTROVALVOLA:
        return (1,)
    if is_hydro_family(kind):
        return MANUAL_LINES
    return ()


def normalize_mac(address: str) -> str:
    """Normalize a Bluetooth MAC to AA:BB:CC:DD:EE:FF (uppercase)."""
    raw = (address or "").strip().replace("-", ":").replace(".", ":").upper()
    parts = [p for p in raw.split(":") if p]
    if len(parts) == 1 and len(parts[0]) == 12:
        s = parts[0]
        parts = [s[i : i + 2] for i in range(0, 12, 2)]
    if len(parts) != 6 or any(len(p) > 2 for p in parts):
        raise ValueError(f"invalid Bluetooth address: {address!r}")
    return ":".join(p.zfill(2) for p in parts)
