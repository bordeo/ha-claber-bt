"""0.1.13: irrigation_ends_at must not jump forward on same remaining."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = ROOT / "custom_components"


def _load():
    if str(PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(PKG_ROOT))
    # Ensure package import (relative imports inside sensor.py)
    if "claber_bt" in sys.modules:
        # Drop cached modules so each test gets a fresh irrigation cache
        to_drop = [name for name in sys.modules if name == "claber_bt" or name.startswith("claber_bt.")]
        for name in to_drop:
            del sys.modules[name]
    mod = importlib.import_module("claber_bt.sensor")
    mod._irrigation_ends_cache.clear()
    return mod


def test_clear_when_remaining_zero():
    m = _load()
    assert m._irrigation_end("AA", 5) is not None
    assert m._irrigation_end("AA", 0) is None
    assert m._irrigation_end("AA", None) is None


def test_same_remaining_does_not_extend(monkeypatch):
    m = _load()
    t0 = datetime(2026, 11, 9, 20, 0, 0, tzinfo=timezone.utc)
    times = {"now": t0}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return times["now"]

    monkeypatch.setattr(m, "datetime", FakeDateTime)

    end1 = m._irrigation_end("AA", 5)
    assert end1 == t0 + timedelta(minutes=5)
    times["now"] = t0 + timedelta(seconds=20)
    end2 = m._irrigation_end("AA", 5)
    assert end2 == end1


def test_decrease_capped(monkeypatch):
    m = _load()
    t0 = datetime(2026, 11, 9, 20, 0, 0, tzinfo=timezone.utc)
    times = {"now": t0}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return times["now"]

    monkeypatch.setattr(m, "datetime", FakeDateTime)
    end1 = m._irrigation_end("AA", 5)
    # After 70s, now+4min still overshoots the prior ends_at → cap to end1.
    times["now"] = t0 + timedelta(seconds=70)
    end2 = m._irrigation_end("AA", 4)
    assert end2 <= end1
    assert end2 == end1
    # Later, now+3min is earlier than the cap → follow the proposed end.
    times["now"] = t0 + timedelta(minutes=1, seconds=30)
    end3 = m._irrigation_end("AA", 3)
    assert end3 == times["now"] + timedelta(minutes=3)
    assert end3 < end1


def test_increase_new_run(monkeypatch):
    m = _load()
    t0 = datetime(2026, 11, 9, 20, 0, 0, tzinfo=timezone.utc)
    times = {"now": t0}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return times["now"]

    monkeypatch.setattr(m, "datetime", FakeDateTime)
    end1 = m._irrigation_end("AA", 2)
    times["now"] = t0 + timedelta(seconds=10)
    end2 = m._irrigation_end("AA", 10)
    assert end2 == times["now"] + timedelta(minutes=10)
    assert end2 > end1


def test_stop_then_stale_same_remaining_keeps_old_until_clear(monkeypatch):
    """After remaining hits 0, cache clears; a later 0 stays clear."""
    m = _load()
    t0 = datetime(2026, 11, 9, 20, 0, 0, tzinfo=timezone.utc)
    times = {"now": t0}

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return times["now"]

    monkeypatch.setattr(m, "datetime", FakeDateTime)
    assert m._irrigation_end("AA", 3) is not None
    times["now"] = t0 + timedelta(seconds=5)
    assert m._irrigation_end("AA", 0) is None
    times["now"] = t0 + timedelta(seconds=6)
    assert m._irrigation_end("AA", 0) is None
