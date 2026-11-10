"""0.1.14: Controller-4 / Elettra model codes unlock via proved app table."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components"


def _load(modname: str):
    if str(PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(PKG_ROOT))
    to_drop = [
        name
        for name in sys.modules
        if name == "claber_bt" or name.startswith("claber_bt.")
    ]
    for name in to_drop:
        del sys.modules[name]
    return importlib.import_module(f"claber_bt.{modname}")


def _hydro_full(*, model_nibble: int, mac_suffix=b"\xaa\xbb\xcc") -> bytes:
    """Build a minimal 9-byte full MSD that classify() accepts as hydro-family.

    Layout uses the proved hydro-family marker (full[2] high nibble 0xF) and
    model nibble in full[4]. Address suffix is full[6:9].
    """
    full = bytearray(9)
    full[0] = 0x00  # no lines watering, program none
    full[1] = 0x00  # remaining minutes
    full[2] = 0xF0  # hydro-family marker
    full[3] = 0x00
    full[4] = model_nibble & 0x0F
    full[5] = 0x50  # energy code 5 -> A / 100%
    full[6:9] = mac_suffix
    return bytes(full)


ADDRESS = "11:22:33:AA:BB:CC"


def test_model_codes_classify_modulo4_and_elettrovalvola():
    passive = _load("claber_passive")
    assert passive.classify(_hydro_full(model_nibble=1), ADDRESS) == "hydro4"
    assert passive.classify(_hydro_full(model_nibble=2), ADDRESS) == "modulo4"
    assert passive.classify(_hydro_full(model_nibble=3), ADDRESS) == "elettrovalvola"
    assert passive.classify(_hydro_full(model_nibble=4), ADDRESS) == "rain_sensor"


def test_decode_uses_hydro_parser_for_codes_2_and_3():
    passive = _load("claber_passive")
    for nibble, kind in ((2, "modulo4"), (3, "elettrovalvola")):
        decoded = passive.decode(_hydro_full(model_nibble=nibble), ADDRESS)
        assert decoded is not None
        assert decoded["device_kind"] == kind
        assert decoded["model"] == kind
        assert decoded["model_code"] == nibble
        assert "lines_watering" in decoded
        assert "battery_percent" in decoded


def test_supported_kinds_and_line_counts():
    const = _load("const")
    assert const.DEVICE_KIND_MODULO4 in const.SUPPORTED_DEVICE_KINDS
    assert const.DEVICE_KIND_ELETTROVALVOLA in const.SUPPORTED_DEVICE_KINDS
    assert const.is_hydro_family("modulo4")
    assert const.is_hydro_family("elettrovalvola")
    assert const.is_hydro_family("hydro4")
    assert not const.is_hydro_family("rain_sensor")
    assert const.manual_lines_for_kind("hydro4") == (1, 2, 3, 4)
    assert const.manual_lines_for_kind("modulo4") == (1, 2, 3, 4)
    assert const.manual_lines_for_kind("elettrovalvola") == (1,)
    assert const.MODEL_NUMBERS["modulo4"] == "90842"
    assert const.MODEL_NUMBERS["elettrovalvola"] == "90843"
    assert const.MODEL_NAMES["modulo4"] == "Controller-4 BT"
    assert const.MODEL_NAMES["elettrovalvola"] == "Elettra-BT"
    assert const.VERSION == "0.1.14"


def test_parser_accepts_hydro_family_kinds():
    parser = _load("parser")
    const = _load("const")
    for kind in (
        const.DEVICE_KIND_HYDRO4,
        const.DEVICE_KIND_MODULO4,
        const.DEVICE_KIND_ELETTROVALVOLA,
        const.DEVICE_KIND_RAIN,
    ):
        data = parser.ClaberBluetoothDeviceData(kind)
        assert data.expected_kind == kind


def test_config_flow_no_longer_lists_unsupported_kinds():
    const = _load("const")
    assert not hasattr(const, "UNSUPPORTED_DEVICE_KINDS") or not getattr(
        const, "UNSUPPORTED_DEVICE_KINDS", frozenset()
    )
    # Discovery should treat codes 2/3 as supported (config_flow uses this set).
    assert {"modulo4", "elettrovalvola"} <= set(const.SUPPORTED_DEVICE_KINDS)
