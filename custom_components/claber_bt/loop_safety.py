"""Event-loop hopping for PassiveBluetooth processor pushes.

HA 2026.11 custom integrations raise RuntimeError if async_write_ha_state
runs off the event loop. Sync callbacks from async_track_time_interval may
execute in an executor worker — never call processor.async_handle_update there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any


def run_on_hass_loop(hass: Any, fn: Callable[[], Any]) -> None:
    """Run ``fn`` on the Home Assistant event-loop thread.

    If already on the running hass loop, call inline; otherwise schedule with
    ``loop.call_soon_threadsafe``.
    """
    loop = hass.loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        fn()
        return
    loop.call_soon_threadsafe(fn)


def push_processors(store: dict[str, Any], update: Any) -> None:
    """Deliver an update to registered PassiveBluetooth processors (loop only!)."""
    for processor in list(store.get("processors") or []):
        handle = getattr(processor, "async_handle_update", None)
        if handle is not None:
            handle(update, True)
