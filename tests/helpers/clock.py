"""Relógio falso dos testes.

Começa longe da data real para que uma implementação que use o relógio do banco, e não o `Clock`
injetado, falhe de forma visível.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

START = datetime(2031, 1, 1, tzinfo=timezone.utc)


class FakeClock:
    """Relógio controlado pelo teste; aceita voltar no tempo."""

    def __init__(self, start: datetime = START) -> None:
        if start.tzinfo is None:
            raise ValueError("o relógio falso só aceita horários com fuso")
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> datetime:
        with self._lock:
            self._now = self._now + timedelta(seconds=seconds)
            return self._now

    def set(self, dt: datetime) -> datetime:
        if dt.tzinfo is None:
            raise ValueError("o relógio falso só aceita horários com fuso")
        with self._lock:
            self._now = dt
            return self._now


def iso(dt: datetime) -> str:
    """Formato de referência dos horários da API: RFC 3339 em UTC com sufixo `Z`."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
