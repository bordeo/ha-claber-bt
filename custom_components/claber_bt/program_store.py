"""In-memory Hydro program A/B state. Unknown until a successful GATT read."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

Listener = Callable[[], None]


class ProgramStore:
    """Per-entry store for decoded programs. Not persisted across restarts."""

    def __init__(self) -> None:
        self.loaded = False
        self.program_a: dict[str, Any] | None = None
        self.program_b: dict[str, Any] | None = None
        self.last_error: str | None = None
        self._listeners: list[Listener] = []

    def add_listener(self, cb: Listener) -> Callable[[], None]:
        self._listeners.append(cb)

        def _remove() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return _remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("program_store listener failed")

    def set_programs(self, prog_a: dict[str, Any], prog_b: dict[str, Any]) -> None:
        self.program_a = copy.deepcopy(prog_a)
        self.program_b = copy.deepcopy(prog_b)
        self.loaded = True
        self.last_error = None
        self._notify()

    def set_error(self, code: str) -> None:
        self.last_error = code
        self._notify()

    def get(self, program: str) -> dict[str, Any] | None:
        if program == "A":
            return copy.deepcopy(self.program_a) if self.program_a else None
        if program == "B":
            return copy.deepcopy(self.program_b) if self.program_b else None
        raise ValueError("program")
