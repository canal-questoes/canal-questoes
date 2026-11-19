"""Limitador de ritmo dos envios por bot, nas mensagens com rota da fila de saída: respeita
os limites de envio do Telegram por chat e por bot.

`SendPacer`: 1 mensagem por intervalo por chat e `bot_per_second` por segundo por bot, em memória,
pelo relógio monotônico **injetado**; consultado na posse da fila de saída (`allows`), registrado na
posse (`record`), penalizado pelo `retry_after` de um 429 (`penalize`). O `chat_id` fica só em memória:
nunca é gravado nem registrado. Só as mensagens com rota passam por ele; a confirmação do toque não.

Núcleo puro. Por chat, guarda o instante a partir do qual ele volta a ser permitido (o maior entre o
fim do intervalo do último envio e o fim da penalidade); por bot, os instantes dos envios da última
janela de 1 s. As entradas vencidas são descartadas a cada consulta.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

# A janela do limite por bot (segundos).
_BOT_WINDOW_SECONDS = 1.0


class SendPacer:
    """Ritmo dos envios por chat e por bot (ver o docstring do módulo)."""

    def __init__(self, *, chat_interval_seconds: float = 1.0, bot_per_second: int = 30,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._chat_interval_seconds = chat_interval_seconds
        self._bot_per_second = bot_per_second
        self._monotonic = monotonic
        # (bot_id, chat_id) → instante em que o chat volta a ser permitido. Só em memória.
        self._chat_ready_at: dict[tuple[int, int], float] = {}
        # bot_id → instantes dos envios ainda dentro da janela, em ordem.
        self._bot_sends: dict[int, deque[float]] = {}

    def __repr__(self) -> str:
        return (f"SendPacer(chat_interval_seconds={self._chat_interval_seconds!r}, "
                f"bot_per_second={self._bot_per_second!r})")

    __str__ = __repr__

    def _prune(self, now: float) -> None:
        """Descarta as esperas de chat vencidas e os envios que saíram da janela do bot."""
        for chat, ready_at in list(self._chat_ready_at.items()):
            if ready_at <= now:
                del self._chat_ready_at[chat]
        for bot, sends in list(self._bot_sends.items()):
            # Um envio feito em `t` sai da janela em `t + 1`.
            while sends and sends[0] + _BOT_WINDOW_SECONDS <= now:
                sends.popleft()
            if not sends:
                del self._bot_sends[bot]

    def _bot_ready_at(self, bot_id: int) -> float | None:
        """Quando o bot esgotado volta a ter vaga na janela (`None` se não está esgotado)."""
        sends = self._bot_sends.get(bot_id)
        if not sends or len(sends) < self._bot_per_second:
            return None
        return sends[len(sends) - self._bot_per_second] + _BOT_WINDOW_SECONDS

    def allows(self, bot_id: int, chat_id: int) -> bool:
        """Se um envio a `chat_id` por `bot_id` cabe agora (só consulta, sem efeito)."""
        now = self._monotonic()
        self._prune(now)
        if self._chat_ready_at.get((bot_id, chat_id), now) > now:
            return False
        return self._bot_ready_at(bot_id) is None

    def record(self, bot_id: int, chat_id: int) -> None:
        """Registra um envio (na posse da fila de saída)."""
        now = self._monotonic()
        self._prune(now)
        key = (bot_id, chat_id)
        self._chat_ready_at[key] = max(self._chat_ready_at.get(key, now), now + self._chat_interval_seconds)
        self._bot_sends.setdefault(bot_id, deque()).append(now)

    def penalize(self, bot_id: int, chat_id: int, seconds: float) -> None:
        """O `retry_after` de um 429: o chat espera pelo menos `seconds`; nunca encurta uma espera maior."""
        now = self._monotonic()
        self._prune(now)
        key = (bot_id, chat_id)
        self._chat_ready_at[key] = max(self._chat_ready_at.get(key, now), now + max(float(seconds), 0.0))

    def next_ready_in(self) -> float | None:
        """Segundos até a primeira espera em curso acabar (de um chat ou de um bot esgotado), ou `None`."""
        now = self._monotonic()
        self._prune(now)
        waits = [ready_at for ready_at in self._chat_ready_at.values() if ready_at > now]
        for bot_id in self._bot_sends:
            ready_at = self._bot_ready_at(bot_id)
            if ready_at is not None and ready_at > now:
                waits.append(ready_at)
        if not waits:
            return None
        return min(waits) - now
