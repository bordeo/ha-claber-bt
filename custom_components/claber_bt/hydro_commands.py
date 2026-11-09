"""Pure Hydro-4 manual command byte builders (no I/O, no Home Assistant).

Cmd 0C program A/B start (eight zero bytes + 01/02) is exposed as explicit
user buttons only. Line manual start/stop unchanged. Program config still
uses 0B/04/05.
"""

from __future__ import annotations

MANUAL_MIN_MINUTES = 1
MANUAL_MAX_MINUTES = 120
MANUAL_LINES = (1, 2, 3, 4)

LIVE_PROVED_START_L1_M1 = bytes.fromhex("260C0109010100000000000000FFC2")
RE_PROVED_STOP = bytes.fromhex("260C0109000000000000000000FFC4")


def checksum(body: bytes) -> bytes:
    """E1: big-endian negative byte sum."""
    if type(body) is not bytes:
        raise TypeError("bytes required")
    return ((-sum(body)) & 0xFFFF).to_bytes(2, "big")


def build_manual_start(line: int, minutes: int) -> bytes:
    """HydroManualTurnOnCommand: 0C/01/09, four [flag,minutes] + 00."""
    if type(line) is not int or line not in MANUAL_LINES:
        raise ValueError("invalid_line")
    if type(minutes) is not int or not MANUAL_MIN_MINUTES <= minutes <= MANUAL_MAX_MINUTES:
        raise ValueError("invalid_minutes")
    payload = bytearray()
    for slot in MANUAL_LINES:
        if slot == line:
            payload.extend((0x01, minutes & 0xFF))
        else:
            payload.extend((0x00, 0x00))
    payload.append(0x00)
    body = bytes((0x26, 0x0C, 0x01, 0x09)) + bytes(payload)
    return body + checksum(body)


def build_manual_stop() -> bytes:
    """HydroManualTurnOffCommand — nine zero payload bytes (global stop)."""
    body = bytes.fromhex("260C0109000000000000000000")
    return body + checksum(body)


def build_program_start(program: str) -> bytes:
    """HydroManualTurnOnCommand for 'start program A/B now'.

    Payload: eight zero bytes + 01 (A) or 02 (B). Opens valves immediately.
    """
    if program not in ("A", "B"):
        raise ValueError("invalid_program")
    code = 0x01 if program == "A" else 0x02
    payload = bytes(8) + bytes((code,))
    body = bytes((0x26, 0x0C, 0x01, 0x09)) + payload
    return body + checksum(body)


RE_PROVED_PROGRAM_A_START = bytes.fromhex("260C0109000000000000000001FFC3")
RE_PROVED_PROGRAM_B_START = bytes.fromhex("260C0109000000000000000002FFC2")


def build_auth_packet(
    address: str, *, random_hex: str | None = None, rotation: int | None = None
) -> bytes:
    """Reproduce myClaber ValidationCommand; MAC-based, not PIN-based."""
    import os

    mac = address.replace(":", "").replace("-", "").upper()
    if len(mac) != 12 or any(c not in "0123456789ABCDEF" for c in mac):
        raise ValueError("Invalid MAC format")
    base = mac[:3] + mac[-5:]
    permuted = "".join(base[i] for i in (2, 7, 4, 6, 1, 3, 0, 5))
    encoded = f"{int(permuted, 16) ^ 0xA3E5276C:08X}"
    if random_hex is None:
        random_hex = os.urandom(4).hex().upper()
    if len(random_hex) != 8 or any(c not in "0123456789ABCDEF" for c in random_hex):
        raise ValueError("Invalid random hex input")
    if rotation is None:
        rotation = os.urandom(1)[0] & 0x0F
    if type(rotation) is not int or not 0 <= rotation < 16:
        raise ValueError("Invalid rotation")
    interleaved = "".join(a + b for a, b in zip(encoded, random_hex, strict=True))
    rotated = interleaved[rotation:] + interleaved[:rotation]
    code = rotated[:8] + f"{rotation:X}" + rotated[8:] + f"{15 - rotation:X}"
    body = bytes.fromhex("26000109" + code)
    return body + checksum(body)


def verification_tag(*, stop_only: bool, line: int, minutes: int) -> str:
    if stop_only:
        return "stop_re_only"
    if line == 1 and minutes == 1:
        return "live"
    return "re_only"
