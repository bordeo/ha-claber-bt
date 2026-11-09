"""Offline Hydro-4 BT program codec (Claber app 2.2.4 static RE).

Builds/decodes frames as bytes. No I/O, no BLE, no Home Assistant.
Every example frame is NEVER SENT. Source: status-read-re-20261109.
Budget=byte14, weekdays=byte1 (FEATURE_MAP E3 had these swapped — corrected).
"""

from __future__ import annotations

from typing import Any

# LIVE_ENABLED: user-initiated GATT writes (programs / auto) are allowed.
# No separate read-only mode. No automatic / periodic / boot writes.
# Rea recommended gating behind an extra OK; Alex wants manual entities that
# write on user action — this flag documents that policy.
LIVE_ENABLED = True

PROGRAM_LINE_MIN = 0
PROGRAM_LINE_MAX = 120  # PROVED app upper bound
BUDGET_MIN = 50  # PROVED
BUDGET_MAX = 150  # PROVED
BUDGET_STEP = 10  # INFERRED from SliderBottomSheet divisions

WEEKDAY_BITS = {
    "mon": 0x01,
    "tue": 0x02,
    "wed": 0x04,
    "thu": 0x08,
    "fri": 0x10,
    "sat": 0x20,
    "sun": 0x40,
}
WEEKDAY_ORDER = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

READ_PROGRAMS_REQUEST = bytes.fromhex("260B0000FFCF")  # PROVED
AUTO_ON = bytes.fromhex("2605010101FFD2")  # PROVED
AUTO_OFF = bytes.fromhex("2605010100FFD3")  # PROVED

# Example NEVER SENT (from Rea / codec):
EXAMPLE_WRITE_0B = bytes.fromhex(
    "260B011E01150A0A0500061E1400FFFFFFFF64000000000000FFFFFFFFFFFFFFFF64F28D"
)
EXAMPLE_DATE_TIME_2026_11_09_153000 = bytes.fromhex(
    "2604010B2611090115300000000000FF44"
)


def checksum(body: bytes) -> bytes:
    """cksum16 = (−Σ body) & 0xFFFF, big-endian. PROVED."""
    return ((-sum(body)) & 0xFFFF).to_bytes(2, "big")


def frame(cmd: int, rw: int, payload: bytes) -> bytes:
    body = bytes([0x26, cmd, rw, len(payload)]) + payload
    return body + checksum(body)


def encode_program(
    enabled: bool,
    weekdays: int,
    lines_min: list[int],
    starts: list[tuple[int, int] | None],
    budget: int,
) -> bytes:
    """Mirrors HydroProgram.toHexString. PROVED layout."""
    if len(lines_min) > 4 or len(starts) != 4:
        raise ValueError("invalid_program_shape")
    for m in lines_min:
        if type(m) is not int or not PROGRAM_LINE_MIN <= m <= PROGRAM_LINE_MAX:
            raise ValueError("invalid_line_minutes")
    if type(budget) is not int or not BUDGET_MIN <= budget <= BUDGET_MAX or budget % BUDGET_STEP:
        raise ValueError("invalid_budget")
    if type(weekdays) is not int or not 0 <= weekdays <= 0x7F:
        raise ValueError("invalid_weekdays")
    out = bytes([1 if enabled else 0, weekdays & 0xFF])
    ln = bytes(int(m) & 0xFF for m in lines_min)
    out += ln + bytes(4 - len(ln))
    for s in starts:
        if s is None:
            out += b"\xFF\xFF"
        else:
            h, m = s
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("invalid_start_time")
            out += bytes([h, m])
    out += bytes([budget & 0xFF])
    if len(out) != 15:
        raise RuntimeError("encode_length")
    return out


def decode_program(rec: bytes, n_lines: int = 4) -> dict[str, Any]:
    """Mirrors HydroProgram.fromHexString for one 15-byte record. PROVED."""
    if len(rec) < 15:
        raise ValueError("short_record")
    lines = [0 if b == 0xFF else b for b in rec[2 : 2 + n_lines]]
    starts: list[tuple[int, int] | None] = []
    for i in range(4):
        h, m = rec[6 + 2 * i], rec[7 + 2 * i]
        starts.append(None if (h == 0xFF or m == 0xFF) else (h, m))
    return {
        "enabled": rec[0] == 1,
        "weekdays": rec[1],
        "days": [d for d, bit in WEEKDAY_BITS.items() if rec[1] & bit],
        "lines_min": lines,
        "starts": starts,
        "budget_pct": rec[14],
    }


def empty_program() -> dict[str, Any]:
    return {
        "enabled": False,
        "weekdays": 0,
        "days": [],
        "lines_min": [0, 0, 0, 0],
        "starts": [None, None, None, None],
        "budget_pct": 100,
    }


def program_to_record(prog: dict[str, Any]) -> bytes:
    return encode_program(
        bool(prog.get("enabled")),
        int(prog.get("weekdays") or 0),
        list(prog.get("lines_min") or [0, 0, 0, 0]),
        list(prog.get("starts") or [None, None, None, None]),
        int(prog.get("budget_pct") if prog.get("budget_pct") is not None else 100),
    )


def write_programs_frame(rec_a: bytes, rec_b: bytes) -> bytes:
    if len(rec_a) != 15 or len(rec_b) != 15:
        raise ValueError("record_length")
    return frame(0x0B, 0x01, rec_a + rec_b)


def date_time_frame(y: int, mo: int, d: int, wd: int, h: int, mi: int, s: int) -> bytes:
    """WriteDateAndTime 04: decimal digits as hex nibbles (BCD-like).
    wd = 1 Mon … 7 Sun (Dart). PROVED."""
    if not 1 <= wd <= 7:
        raise ValueError("invalid_weekday")
    dec = f"{y % 100:02d}{mo:02d}{d:02d}{wd:02d}{h:02d}{mi:02d}{s:02d}00000000"
    return frame(0x04, 0x01, bytes.fromhex(dec))


def read_programs_request() -> bytes:
    return bytes(READ_PROGRAMS_REQUEST)


def auto_frame(*, on: bool) -> bytes:
    return bytes(AUTO_ON if on else AUTO_OFF)


def try_parse_0b_reply(buf: bytes) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Parse a complete 0B reply from a buffer (may be exact frame).

    Envelope 23 0B <status> 1E + 30 payload + cksum16 is INFERRED (no capture).
    Returns None if incomplete or undecodable — caller must NOT write.
    """
    if len(buf) < 6:
        return None
    # Find 23 0B header (allow leading noise)
    start = buf.find(b"\x23\x0b")
    if start < 0:
        return None
    data = buf[start:]
    if len(data) < 4:
        return None
    status, length = data[2], data[3]
    need = 4 + length + 2
    if len(data) < need:
        return None
    frame_bytes = data[:need]
    body, ck = frame_bytes[:-2], frame_bytes[-2:]
    if checksum(body) != ck:
        return None
    if status != 0x00:
        return None
    if length != 30:
        return None
    payload = frame_bytes[4:-2]
    return decode_program(payload[0:15]), decode_program(payload[15:30])


def try_parse_status_ack(buf: bytes, expected_cmd: int) -> int | None:
    """Return status byte if a well-formed 23 <cmd> <status> … cksum frame is found."""
    start = buf.find(bytes([0x23, expected_cmd & 0xFF]))
    if start < 0:
        return None
    data = buf[start:]
    if len(data) < 6:
        return None
    length = data[3]
    need = 4 + length + 2
    if len(data) < need:
        return None
    frame_bytes = data[:need]
    if checksum(frame_bytes[:-2]) != frame_bytes[-2:]:
        return None
    return frame_bytes[2]


def effective_run_minutes(prog: dict[str, Any]) -> int:
    """Σ line minutes × budget/100, whole minutes. PROVED app formula."""
    total = sum(int(x) for x in (prog.get("lines_min") or []))
    budget = int(prog.get("budget_pct") or 100)
    return (total * budget) // 100


def _intervals_for_day(prog: dict[str, Any], day_bit: int) -> list[tuple[int, int]]:
    """Return [start_min, end_min) intervals for one weekday bit; spill to +1440."""
    if not (prog.get("weekdays", 0) & day_bit):
        return []
    if not prog.get("enabled"):
        return []
    run = effective_run_minutes(prog)
    if run <= 0:
        return []
    out: list[tuple[int, int]] = []
    for start in prog.get("starts") or []:
        if start is None:
            continue
        h, m = start
        begin = h * 60 + m
        end = begin + run
        out.append((begin, end))
    return out


def _overlap_pair(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def validate_program_pair(prog_a: dict[str, Any], prog_b: dict[str, Any]) -> str | None:
    """Return an error code string, or None if OK.

    Checks ranges and overlaps within/across A/B on shared days, including
    runs that cross midnight (spill > 1440). PROVED app blocks overlap before save.
    """
    for label, prog in (("A", prog_a), ("B", prog_b)):
        try:
            program_to_record(prog)
        except ValueError as err:
            return f"invalid_{label.lower()}_{err}"
        # Within-program overlap
        for bit in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40):
            intervals = _intervals_for_day(prog, bit)
            for i, left in enumerate(intervals):
                for right in intervals[i + 1 :]:
                    if _overlap_pair(left, right):
                        return f"overlap_within_{label.lower()}"
                    # also compare spill into next day against next day's same program
                    if left[1] > 1440:
                        spilled = (0, left[1] - 1440)
                        # next weekday bit
                        next_bit = bit << 1 if bit != 0x40 else 0x01
                        for other in _intervals_for_day(prog, next_bit):
                            if _overlap_pair(spilled, other):
                                return f"overlap_within_{label.lower()}_midnight"

    # Across A/B on shared days
    for bit in (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40):
        a_iv = _intervals_for_day(prog_a, bit)
        b_iv = _intervals_for_day(prog_b, bit)
        for left in a_iv:
            for right in b_iv:
                if _overlap_pair(left, right):
                    return "overlap_between_a_b"
            if left[1] > 1440:
                spilled = (0, left[1] - 1440)
                next_bit = bit << 1 if bit != 0x40 else 0x01
                for right in _intervals_for_day(prog_b, next_bit):
                    if _overlap_pair(spilled, right):
                        return "overlap_between_a_b_midnight"
        for right in b_iv:
            if right[1] > 1440:
                spilled = (0, right[1] - 1440)
                next_bit = bit << 1 if bit != 0x40 else 0x01
                for left in _intervals_for_day(prog_a, next_bit):
                    if _overlap_pair(spilled, left):
                        return "overlap_between_a_b_midnight"
    return None


def merge_field(
    prog_a: dict[str, Any],
    prog_b: dict[str, Any],
    *,
    program: str,
    field: str,
    value: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return deep-copied A/B with one field changed on program 'A' or 'B'."""
    import copy

    a = copy.deepcopy(prog_a)
    b = copy.deepcopy(prog_b)
    target = a if program == "A" else b
    if field == "enabled":
        target["enabled"] = bool(value)
    elif field == "weekdays":
        target["weekdays"] = int(value) & 0x7F
        target["days"] = [d for d, bit in WEEKDAY_BITS.items() if target["weekdays"] & bit]
    elif field == "weekday_bit":
        day, on = value
        bit = WEEKDAY_BITS[day]
        if on:
            target["weekdays"] |= bit
        else:
            target["weekdays"] &= ~bit & 0x7F
        target["days"] = [d for d, bt in WEEKDAY_BITS.items() if target["weekdays"] & bt]
    elif field == "line_minutes":
        line, minutes = value
        lines = list(target["lines_min"])
        lines[line - 1] = int(minutes)
        target["lines_min"] = lines
    elif field == "start":
        slot, hm = value  # hm is (h,m) or None
        starts = list(target["starts"])
        starts[slot - 1] = hm
        target["starts"] = starts
    elif field == "budget":
        target["budget_pct"] = int(value)
    else:
        raise ValueError("unknown_field")
    return a, b
