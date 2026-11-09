"""Constants for the Claber BT integration."""

from __future__ import annotations

DOMAIN = "claber_bt"
VERSION = "0.1.13"

CONF_DEVICE_KIND = "device_kind"
CONF_BLUETOOTH_SOURCE = "preferred_bluetooth_source"
CONF_ENABLE_MANUAL_ONCE = "enable_experimental_manual_once"  # legacy ignored

DEVICE_KIND_HYDRO4 = "hydro4"
DEVICE_KIND_RAIN = "rain_sensor"
SUPPORTED_DEVICE_KINDS = frozenset({DEVICE_KIND_HYDRO4, DEVICE_KIND_RAIN})
UNSUPPORTED_DEVICE_KINDS = frozenset({"modulo4", "elettrovalvola"})

MANUFACTURER = "Claber"

MODEL_NUMBERS = {
    DEVICE_KIND_HYDRO4: "90840",
    DEVICE_KIND_RAIN: "90844",
}

MODEL_NAMES = {
    DEVICE_KIND_HYDRO4: "Hydro-4 BT",
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
