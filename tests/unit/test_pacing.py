"""Ritmo dos envios por bot: no máximo um envio por segundo por chat, até 30 por segundo por bot,
e a espera pedida pelo Telegram num 429.

`SendPacer(*, chat_interval_seconds=1.0, bot_per_second=30, monotonic=time.monotonic)`, com o relógio
monotônico **injetado** (nos testes
unitários):
- `allows(bot_id, chat_id)`: a consulta, sem efeito; `record(bot_id, chat_id)`: o envio, registrado na
  posse; no máximo 1 envio por `chat_interval_seconds` por chat (de um bot) e no máximo
  `bot_per_second` envios numa janela **deslizante** de 1 s por bot;
- `penalize(bot_id, chat_id, seconds)`: o `retry_after` de um 429 (esperar pelo menos o
  `retry_after`); nunca encurta uma espera maior já em curso;
- `next_ready_in()`: quantos segundos faltam para a primeira espera em curso acabar (de um chat
  recusado ou de um bot esgotado); `None` quando nada espera [I: a leitura de `float | None` da
  assinatura];
- o `chat_id` fica só em memória: nenhum registro de log, nada em fd 1 e 2.

Fronteiras [I]: um chat que enviou em `t` volta a ser permitido em `t + intervalo` (e não antes); um
envio feito em `t` sai da janela do bot em `t + 1`; `next_ready_in()` é coerente com isso (> 0 enquanto
algo espera; `None` no instante em que a última espera acaba). Os instantes são exatos em binário
(`t0 = 1024`, passos múltiplos de 2⁻¹⁰), para as fronteiras não dependerem de arredondamento.

No esqueleto, o construtor só guarda os argumentos e os 4 métodos lançam `NotImplementedError`: todo
teste deste arquivo falha por esse motivo.
"""

from __future__ import annotations

from typing import Any

import pytest

from cq_server.pacing import SendPacer
from tests.helpers.logs import capture_logs

BOT = 7
OTHER_BOT = 8
CHAT = 761_000_000_001
OTHER_CHAT = 761_000_000_002

# Um passo abaixo de cada fronteira, exato em binário (2⁻¹⁰).
EPS = 2.0 ** -10


class FakeMonotonic:
    """Relógio monotônico injetado: só anda quando o teste manda (`at(x)` = `t0 + x`)."""

    def __init__(self, start: float = 1024.0) -> None:
        self.start = start
        self.now = start

    def __call__(self) -> float:
        return self.now

    def at(self, offset: float) -> None:
        assert self.start + offset >= self.now, "o relógio monotônico não volta"
        self.now = self.start + offset


def _pacer(clock: FakeMonotonic, **kwargs: Any) -> SendPacer:
    return SendPacer(monotonic=clock, **kwargs)


def _fresh_chats(base: int) -> Any:
    """Chats nunca usados (para medir só a janela do bot)."""
    return iter(range(base, base + 10_000))


# --- por chat ---------------------------------------------------------------------------------------

def test_one_send_per_second_per_chat_with_defaults() -> None:
    """No máximo 1 mensagem por segundo por chat (padrão): com os
    padrões (`chat_interval_seconds = 1.0`), um chat novo é permitido; depois do `record`, o mesmo chat
    fica recusado até 1 s depois (em 0, 0,25, 0,5 e 1 − 2⁻¹⁰ s) e volta a ser permitido em 1 s; o
    relógio usado é o injetado (o tempo real não conta)."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    assert pacer.allows(BOT, CHAT) is True
    pacer.record(BOT, CHAT)
    assert pacer.allows(BOT, CHAT) is False
    for offset in (0.25, 0.5, 1.0 - EPS):
        clock.at(offset)
        assert pacer.allows(BOT, CHAT) is False, offset
    clock.at(1.0)
    assert pacer.allows(BOT, CHAT) is True
    pacer.record(BOT, CHAT)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(2.0 - EPS)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(2.0)
    assert pacer.allows(BOT, CHAT) is True


def test_allows_has_no_side_effect() -> None:
    """O `record` acontece na posse: `allows` é só consulta; chamado 100
    vezes para o mesmo chat e para 100 chats, não consome o ritmo do chat nem a janela do bot (com 2
    por segundo, um `record` ainda deixa outro chat passar)."""
    clock = FakeMonotonic()
    pacer = _pacer(clock, bot_per_second=2)
    for _ in range(100):
        assert pacer.allows(BOT, CHAT) is True
    for chat in range(100):
        assert pacer.allows(BOT, 761_000_100_000 + chat) is True
    pacer.record(BOT, CHAT)
    assert pacer.allows(BOT, OTHER_CHAT) is True
    assert pacer.allows(BOT, OTHER_CHAT) is True


def test_chat_interval_is_per_chat_and_per_bot() -> None:
    """Por chat de cada bot (o limite é por chat de cada bot; os 30/s são por bot): depois do envio a um chat,
    outro chat do mesmo bot e o mesmo número de chat em outro bot continuam permitidos."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    pacer.record(BOT, CHAT)
    assert pacer.allows(BOT, CHAT) is False
    assert pacer.allows(BOT, OTHER_CHAT) is True
    assert pacer.allows(OTHER_BOT, CHAT) is True
    pacer.record(OTHER_BOT, CHAT)
    assert pacer.allows(OTHER_BOT, CHAT) is False
    assert pacer.allows(BOT, OTHER_CHAT) is True


def test_configured_chat_interval_is_honored() -> None:
    """Padrões configuráveis (`CQ_SEND_CHAT_INTERVAL_SECONDS`): com intervalo de 2,5 s,
    o chat fica recusado em 1 s e em 2,5 − 2⁻¹⁰ s e volta em 2,5 s; com 0,25 s, volta em 0,25 s."""
    clock = FakeMonotonic()
    pacer = _pacer(clock, chat_interval_seconds=2.5)
    pacer.record(BOT, CHAT)
    clock.at(1.0)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(2.5 - EPS)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(2.5)
    assert pacer.allows(BOT, CHAT) is True

    fast = _pacer(clock, chat_interval_seconds=0.25)
    fast.record(BOT, CHAT)
    assert fast.allows(BOT, CHAT) is False
    clock.at(2.75 - EPS)
    assert fast.allows(BOT, CHAT) is False
    clock.at(2.75)
    assert fast.allows(BOT, CHAT) is True


# --- por bot ----------------------------------------------------------------------------------------

def test_thirty_sends_per_second_per_bot_with_defaults() -> None:
    """30 por segundo por bot (padrão): 30 envios a 30 chats diferentes no mesmo
    instante esgotam o bot; um 31º chat (nunca usado) é recusado nesse bot e permitido em outro bot; em
    1 − 2⁻¹⁰ s continua recusado; em 1 s volta."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    chats = [761_000_200_000 + i for i in range(31)]
    for chat in chats[:30]:
        assert pacer.allows(BOT, chat) is True
        pacer.record(BOT, chat)
    assert pacer.allows(BOT, chats[30]) is False
    assert pacer.allows(OTHER_BOT, chats[30]) is True
    clock.at(1.0 - EPS)
    assert pacer.allows(BOT, chats[30]) is False
    clock.at(1.0)
    assert pacer.allows(BOT, chats[30]) is True


def test_bot_limit_is_a_sliding_window() -> None:
    """A janela deslizante de 30/s por bot: 15 envios em 0 s e 15 em
    0,5 s esgotam o bot; em 1 s os 15 primeiros saem da janela e o bot volta; mais 15 em 1 s esgotam de
    novo, e em 1,25 s continua recusado (15 de 0,5 s + 15 de 1 s; um balde fixo por segundo inteiro
    permitiria), até 1,5 s."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    fresh = _fresh_chats(761_000_300_000)

    def send(n: int) -> None:
        for _ in range(n):
            chat = next(fresh)
            assert pacer.allows(BOT, chat) is True, clock.now
            pacer.record(BOT, chat)

    send(15)
    clock.at(0.5)
    send(15)
    assert pacer.allows(BOT, next(fresh)) is False
    clock.at(1.0)
    send(15)
    assert pacer.allows(BOT, next(fresh)) is False
    clock.at(1.25)
    assert pacer.allows(BOT, next(fresh)) is False
    clock.at(1.5 - EPS)
    assert pacer.allows(BOT, next(fresh)) is False
    clock.at(1.5)
    assert pacer.allows(BOT, next(fresh)) is True


def test_configured_bot_rate_is_honored() -> None:
    """Ritmo do bot configurável (`CQ_SEND_BOT_PER_SECOND`): com 2 por segundo, o 3º chat é recusado no
    mesmo segundo e permitido 1 s depois; o limite por chat vale junto (com intervalo de 3 s, o chat
    que enviou continua recusado em 1 s e volta em 3 s)."""
    clock = FakeMonotonic()
    pacer = _pacer(clock, bot_per_second=2, chat_interval_seconds=3.0)
    pacer.record(BOT, CHAT)
    pacer.record(BOT, OTHER_CHAT)
    third = 761_000_400_000
    assert pacer.allows(BOT, third) is False
    clock.at(1.0)
    assert pacer.allows(BOT, third) is True
    assert pacer.allows(BOT, CHAT) is False
    clock.at(3.0)
    assert pacer.allows(BOT, CHAT) is True


# --- penalidade (`retry_after`) ----------------------------------------------------------------------

def test_penalize_blocks_the_chat_for_retry_after() -> None:
    """Esperar pelo menos o `retry_after` indicado pelo Telegram antes de repetir
    (o 429 chama `penalize`): sem envio anterior, `penalize(bot, chat, 5)` recusa o chat
    até 5 s depois (em 1 s e em 5 − 2⁻¹⁰ s ainda não) e o libera em 5 s; o mesmo número de chat em
    outro bot não é afetado."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    pacer.penalize(BOT, CHAT, 5)
    assert pacer.allows(BOT, CHAT) is False
    assert pacer.allows(OTHER_BOT, CHAT) is True
    clock.at(1.0)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(5.0 - EPS)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(5.0)
    assert pacer.allows(BOT, CHAT) is True


def test_penalize_extends_but_never_shortens_the_wait() -> None:
    """Pelo menos o `retry_after`: depois de um envio (espera até 1 s), um `penalize` de
    0,25 s não libera o chat antes de 1 s (recusado em 0,5 s); um `penalize` de 4 s em 0,5 s leva a
    espera a 4,5 s; um `penalize` menor depois (1 s, em 1 s) não a encurta (recusado em 3 s e em
    4,5 − 2⁻¹⁰ s); liberado em 4,5 s; `seconds` fracionário aceito."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    pacer.record(BOT, CHAT)
    pacer.penalize(BOT, CHAT, 0.25)
    clock.at(0.5)
    assert pacer.allows(BOT, CHAT) is False
    pacer.penalize(BOT, CHAT, 4)
    clock.at(1.0)
    assert pacer.allows(BOT, CHAT) is False
    pacer.penalize(BOT, CHAT, 1.0)
    clock.at(3.0)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(4.5 - EPS)
    assert pacer.allows(BOT, CHAT) is False
    clock.at(4.5)
    assert pacer.allows(BOT, CHAT) is True


# --- `next_ready_in` ----------------------------------------------------------------------------------

def test_next_ready_in_reports_the_earliest_wait() -> None:
    """A primeira espera (`next_ready_in() -> float | None`): sem nada esperando, `None`; depois de
    um envio a um chat, o que falta para o intervalo acabar (1 s, depois 0,75 s); com dois chats, o
    menor dos restos; com uma penalidade maior em outro bot, continua o menor; acabadas as esperas dos
    chats, o resto da penalidade; acabadas todas, `None` de novo; o tempo vem do relógio injetado."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    assert pacer.next_ready_in() is None
    pacer.record(BOT, CHAT)  # espera até 1
    assert pacer.next_ready_in() == pytest.approx(1.0)
    clock.at(0.25)
    assert pacer.next_ready_in() == pytest.approx(0.75)
    pacer.record(BOT, OTHER_CHAT)  # espera até 1,25
    assert pacer.next_ready_in() == pytest.approx(0.75)
    pacer.penalize(OTHER_BOT, CHAT, 10)  # espera até 10,25
    assert pacer.next_ready_in() == pytest.approx(0.75)
    clock.at(1.0)
    assert pacer.next_ready_in() == pytest.approx(0.25)
    clock.at(1.25)
    assert pacer.next_ready_in() == pytest.approx(9.0)
    clock.at(10.25)
    assert pacer.next_ready_in() is None


def test_next_ready_in_covers_the_bot_window_and_penalties() -> None:
    """um bot esgotado (30 envios em 0 s) espera o fim da janela: em 0,375 s,
    `next_ready_in()` dá 0,625 s; em 1 s, `None`; uma penalidade sozinha dá o seu resto (5 s, depois
    3 s)."""
    clock = FakeMonotonic()
    pacer = _pacer(clock)
    for i in range(30):
        pacer.record(BOT, 761_000_500_000 + i)
    clock.at(0.375)
    assert pacer.next_ready_in() == pytest.approx(0.625)
    clock.at(1.0)
    assert pacer.next_ready_in() is None

    alone = _pacer(clock)
    alone.penalize(BOT, CHAT, 5)
    assert alone.next_ready_in() == pytest.approx(5.0)
    clock.at(3.0)
    assert alone.next_ready_in() == pytest.approx(3.0)


# --- o `chat_id` só em memória --------------------------------------------------------------------------

SENTINEL_CHAT = 7_654_321_098_765


def test_chat_id_stays_in_memory_and_nothing_is_logged(capfd: pytest.CaptureFixture[str]) -> None:
    """O `chat_id` fica só em memória: nunca é gravado nem registrado em log;
    princípio da identidade só pela revelação: `allows`, `record`, `penalize` e `next_ready_in`, com um chat sentinela, inclusive
    recusas (do chat e do bot esgotado), não geram nenhum registro de log (em DEBUG) nem saída em fd 1
    e 2; o `repr` e o `str` do limitador não mostram o chat."""
    clock = FakeMonotonic()
    with capture_logs() as logs:
        pacer = _pacer(clock, bot_per_second=1)
        assert pacer.allows(BOT, SENTINEL_CHAT) is True
        pacer.record(BOT, SENTINEL_CHAT)
        assert pacer.allows(BOT, SENTINEL_CHAT) is False
        assert pacer.allows(BOT, SENTINEL_CHAT + 1) is False
        pacer.penalize(BOT, SENTINEL_CHAT, 3)
        assert pacer.next_ready_in() == pytest.approx(1.0)
        clock.at(3.0)
        assert pacer.allows(BOT, SENTINEL_CHAT) is True
        text = repr(pacer) + str(pacer)
    assert logs.records == []
    assert str(SENTINEL_CHAT) not in logs.text
    assert str(SENTINEL_CHAT) not in text
    out, err = capfd.readouterr()
    assert str(SENTINEL_CHAT) not in out + err
    assert out == "" and err == ""
