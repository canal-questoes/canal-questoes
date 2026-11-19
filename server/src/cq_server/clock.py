"""Relógio injetável (nenhuma ordenação depende dele)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Relógio de parede, sempre em UTC (`datetime` com fuso)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)
