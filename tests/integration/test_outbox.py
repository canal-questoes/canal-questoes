"""Fila de saída transacional.

Cada transição confere **todos** os campos do `OutboxRecord`, estado a estado
(os campos não citados ficam como estavam). Horários pelo `FakeClock`, conferidos por
igualdade de instante e em UTC; ids nunca por valor literal. Os eventos `outbox.failed` são lidos
pela API (`events.list`, validados contra o esquema congelado) e conferidos por inteiro, sem o `seq`.

Quem "é outro worker" usa outra instância de `Outbox` (uma instância por thread).
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.events import EventType
from cq_server.outbox import Outbox, OutboxRecord
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, RetryLater, Sent, Undeliverable
from tests.helpers.api import ADMIN_TOKEN, all_events, marker, register_person
from tests.helpers.calls import SEEDED_FREE_TEXT
from tests.helpers.canary import URSULA, find_canary
from tests.helpers.channels import Crash, ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.logs import capture_logs

LEASE_SECONDS = 30
LEASE = timedelta(seconds=LEASE_SECONDS)
EXPIRE = LEASE_SECONDS + 1  # avanço que vence a posse (a fronteira é estrita: lease_until < agora)
LEASE_EXHAUSTED = "posse vencida sem conclusão"
CONSOLE_REF = re.compile(r"^console-[0-9a-f]{32}$")
# Mensagem de exceção do canal que não pode ir ao banco, aos eventos nem aos logs (do erro, fica
# só o nome do tipo). Não tem forma de segredo nem de identidade, para a redação não a esconder.
SENTINEL = "sentinela-do-canal-4Kp8"
ALL_TABLES = ("schema_migrations", "people", "identities", "api_tokens", "events", "tasks",
              "outbox_messages")


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (LEASE_SECONDS, 3, 2, 60)


def _s(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


# --- estados esperados (o estado da fila em cada transição) ---------------------

def _enqueued(message_id: int, recipient: uuid.UUID, text: str, at: datetime) -> OutboxRecord:
    return OutboxRecord(id=message_id, recipient=recipient, text=text, state="pending", attempts=0,
                        available_at=at, lease_owner=None, lease_until=None, channel_ref=None,
                        last_error=None, sent_at=None)


def _leased(prev: OutboxRecord, owner: str, now: datetime) -> OutboxRecord:
    return dataclasses.replace(prev, state="leased", attempts=prev.attempts + 1, lease_owner=owner,
                               lease_until=now + LEASE)


def _sent(prev: OutboxRecord, channel_ref: str, now: datetime) -> OutboxRecord:
    return dataclasses.replace(prev, state="sent", lease_owner=None, lease_until=None,
                               channel_ref=channel_ref, sent_at=now)


def _retry(prev: OutboxRecord, now: datetime, wait_seconds: float, error: str) -> OutboxRecord:
    return dataclasses.replace(prev, state="pending", available_at=now + _s(wait_seconds),
                               lease_owner=None, lease_until=None, last_error=error)


def _failed(prev: OutboxRecord, error: str) -> OutboxRecord:
    return dataclasses.replace(prev, state="failed", lease_owner=None, lease_until=None, last_error=error)


def _assert_record(outbox: Any, expected: OutboxRecord) -> None:
    actual = outbox.get(expected.id)
    assert actual == expected, f"OutboxRecord {actual!r} != {expected!r}"
    for name in ("available_at", "lease_until", "sent_at"):
        value = getattr(actual, name)
        if value is not None:
            assert value.utcoffset() == timedelta(0), f"{name} fora de UTC: {value!r}"


# --- apoio ---------------------------------------------------------------------------------

def _register(client: Any, name: str, **identity: Any) -> tuple[uuid.UUID, str]:
    body = register_person(client, ADMIN_TOKEN, name=name, **identity)
    return uuid.UUID(body["pseudo_id"]), body["alias"]


def _enqueue(outbox: Any, db_url: str, recipient: uuid.UUID, text: str, key: str | None = None) -> int:
    """Enfileira na transação de uma conexão do chamador, confirmada no fim do bloco."""
    with psycopg.connect(db_url) as conn:
        return outbox.enqueue(conn, recipient=recipient, text=text, idempotency_key=key)


@contextlib.contextmanager
def _other_worker(db_url: str, clock: Any, settings: Any) -> Iterator[Outbox]:
    other = Outbox(db_url, clock=clock, settings=settings)
    try:
        yield other
    finally:
        other.close()


def _failed_events(client: Any) -> list[dict]:
    """Os eventos `outbox.failed` pela API, sem o `seq` (que só é conferido como inteiro)."""
    events = all_events(client, ADMIN_TOKEN, "outbox.failed")
    for event in events:
        assert isinstance(event["seq"], int)
    return [{k: v for k, v in event.items() if k != "seq"} for event in events]


def _failed_event(message_id: int, recipient: uuid.UUID, attempts: int, error: str,
                  at: datetime) -> dict:
    return {"type": "outbox.failed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"message_id": message_id, "recipient": str(recipient), "attempts": attempts,
                     "error": error}}


def _database_text(sql: psycopg.Connection) -> str:
    parts: list[str] = []
    for table in ALL_TABLES:
        parts.extend(row[0] for row in sql.execute(f"SELECT t::text FROM {table} t"))
    return "\n".join(parts)


def _dispatched(cap: Any) -> list[tuple[Any, Any]]:
    return [(r.get("id"), r.get("state")) for r in cap.with_event("outbox.dispatched")]


def _crash_dispatch(db_url: str, clock: Any, settings: Any, worker_id: str) -> list[OutgoingMessage]:
    """Um worker que cai dentro do `send` (queda do processo); devolve o que o canal recebeu."""
    channel = ScriptedChannel([Crash()])
    with _other_worker(db_url, clock, settings) as worker:
        with pytest.raises(Crash):
            worker.dispatch_once(channel, worker_id)
    return channel.calls


class _NotebookChannel:
    """Canal falso de outra classe, sem herdar nada do projeto: a fila só depende da porta."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=2, supports_math=True,
                                                supports_rich_text=False)
        self.pages: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.pages.append(message)
        return Sent(f"caderno-{len(self.pages)}")


# --- testes --------------------------------------------------------------------------------

def test_committed_message_is_sent_once_to_console(client: Any, outbox: Any, db_url: str,
                                                   clock: Any) -> None:
    """Transação confirmada: antes do commit nada sai; depois, uma linha exata
    no console, `sent` com `channel_ref` e `sent_at`, `recipient == pseudo_id`, e nada é enviado de
    novo."""
    pid, _ = _register(client, "Pessoa Destinataria Confirmada")
    stream = io.StringIO()
    console = ConsoleChannel(stream)
    text = "Olá! Sua questão chegou.\nResponda quando puder."
    t0 = clock.now()

    conn = psycopg.connect(db_url)
    try:
        mid = outbox.enqueue(conn, recipient=pid, text=text)
        assert outbox.dispatch_once(console, "worker-1") is None, "mensagem saiu antes do commit"
        assert stream.getvalue() == ""
        conn.commit()
    finally:
        conn.close()
    enqueued = _enqueued(mid, pid, text, t0)
    _assert_record(outbox, enqueued)

    clock.advance(5)
    assert outbox.dispatch_once(console, "worker-1") == mid
    out = stream.getvalue()
    assert out.endswith("\n") and out.count("\n") == 1, f"esperada uma linha só: {out!r}"
    line = json.loads(out)
    ref = line["channel_ref"]
    assert CONSOLE_REF.fullmatch(ref), ref
    assert line == {"channel_ref": ref, "message_id": mid, "recipient": str(pid), "text": text}
    sent = _sent(_leased(enqueued, "worker-1", clock.now()), ref, clock.now())
    assert sent.attempts == 1 and sent.sent_at == t0 + _s(5) and sent.available_at == t0
    _assert_record(outbox, sent)

    clock.advance(EXPIRE)
    assert outbox.dispatch_once(console, "worker-2") is None
    assert stream.getvalue() == out
    _assert_record(outbox, sent)
    assert _failed_events(client) == []


def test_rolled_back_message_is_never_sent(client: Any, outbox: Any, db_url: str,
                                           sql: psycopg.Connection) -> None:
    """Transação desfeita: `dispatch_once` devolve `None`, o console fica vazio e a
    mensagem não existe."""
    pid, _ = _register(client, "Pessoa Destinataria Desfeita")
    stream = io.StringIO()
    console = ConsoleChannel(stream)
    with psycopg.connect(db_url) as conn:
        mid = outbox.enqueue(conn, recipient=pid, text="Mensagem que nunca pode sair")
        conn.rollback()

    assert outbox.dispatch_once(console, "worker-1") is None
    assert stream.getvalue() == ""
    assert sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 0
    with pytest.raises(LookupError):
        outbox.get(mid)


def test_enqueue_idempotency_and_validation(client: Any, outbox: Any, db_url: str, clock: Any,
                                            sql: psycopg.Connection) -> None:
    """`Outbox.enqueue`: mesma chave e mesmo conteúdo → mesmo id; chave com outro
    texto ou outro destinatário → `ValueError`; texto com 0 ou 4097 caracteres → `ValueError` (1 e 4096
    aceitos, contando caracteres e não bytes); destinatário inexistente → `ValueError`."""
    pid, _ = _register(client, "Pessoa Destinataria Validacao")
    other, _ = _register(client, "Pessoa Outra Validacao")
    t0 = clock.now()

    first = _enqueue(outbox, db_url, pid, "Aviso com chave", key="aviso-chave-1")
    assert _enqueue(outbox, db_url, pid, "Aviso com chave", key="aviso-chave-1") == first
    for recipient, text in ((pid, "Aviso com outro texto"), (other, "Aviso com chave")):
        with pytest.raises(ValueError):
            _enqueue(outbox, db_url, recipient, text, key="aviso-chave-1")
    for bad in ("", "a" * 4097, "é" * 4097):
        with pytest.raises(ValueError):
            _enqueue(outbox, db_url, pid, bad)
    with pytest.raises(ValueError):
        _enqueue(outbox, db_url, uuid.uuid4(), "Destinatario que nao existe")

    one = _enqueue(outbox, db_url, pid, "a")
    full = _enqueue(outbox, db_url, pid, "é" * 4096)  # 4096 caracteres, 8192 bytes em UTF-8
    assert first < one < full
    _assert_record(outbox, _enqueued(first, pid, "Aviso com chave", t0))
    _assert_record(outbox, _enqueued(one, pid, "a", t0))
    _assert_record(outbox, _enqueued(full, pid, "é" * 4096, t0))
    assert sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == 3


def test_dispatch_order_follows_id_not_clock(client: Any, outbox: Any, db_url: str, clock: Any) -> None:
    """m1 enfileirada em T e m2 em T−1 h (relógio voltando); com o relógio em T+1 s, sai m1
    primeiro."""
    pid, _ = _register(client, "Pessoa Destinataria Ordem")
    t = clock.now()
    m1 = _enqueue(outbox, db_url, pid, "primeira da fila")
    clock.set(t - timedelta(hours=1))
    m2 = _enqueue(outbox, db_url, pid, "segunda da fila")
    clock.set(t + _s(1))

    channel = ScriptedChannel([Sent("ref-ordem-1"), Sent("ref-ordem-2")])
    assert outbox.dispatch_once(channel, "worker-1") == m1
    assert outbox.dispatch_once(channel, "worker-1") == m2
    assert outbox.dispatch_once(channel, "worker-1") is None
    assert channel.calls == [OutgoingMessage(message_id=m1, recipient=pid, text="primeira da fila"),
                             OutgoingMessage(message_id=m2, recipient=pid, text="segunda da fila")]
    now = clock.now()
    first = _leased(_enqueued(m1, pid, "primeira da fila", t), "worker-1", now)
    second = _leased(_enqueued(m2, pid, "segunda da fila", t - timedelta(hours=1)), "worker-1", now)
    _assert_record(outbox, _sent(first, "ref-ordem-1", now))
    _assert_record(outbox, _sent(second, "ref-ordem-2", now))


@pytest.mark.parametrize(("reason", "retry_after", "wait"), [
    ("ocupado", 30.0, 30),
    ("x", None, 2),
    ("curto", 1.0, 2),
    ("limite", 120.0, 120),
], ids=["retry_after-30s", "sem-retry_after", "retry_after-menor-que-a-espera",
        "retry_after-maior-que-o-teto"])
def test_retry_later_waits_before_next_attempt(client: Any, outbox: Any, db_url: str, clock: Any,
                                               reason: str, retry_after: float | None, wait: int) -> None:
    """Falha temporária: `RetryLater("ocupado", 30)` → espera de 30 s
    e `last_error == "ocupado"`; `RetryLater("x")` → espera de 2 s; um `retry_after` menor que a espera
    crescente não a encurta; um `retry_after` acima do teto da espera crescente (`backoff_max` = 60 s)
    vale inteiro (120 s), sem ser cortado pelo teto. Antes do tempo → `None`; no tempo →
    `attempts = 2`."""
    pid, _ = _register(client, "Pessoa Destinataria Espera")
    text = "Mensagem adiada"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    result = RetryLater(reason) if retry_after is None else RetryLater(reason, retry_after)
    channel = ScriptedChannel([result, Sent("ref-depois-da-espera")])

    assert outbox.dispatch_once(channel, "worker-1") == mid
    first = _leased(_enqueued(mid, pid, text, t0), "worker-1", t0)
    retried = _retry(first, t0, wait, reason)
    _assert_record(outbox, retried)

    clock.advance(wait - 1)
    assert outbox.dispatch_once(channel, "worker-1") is None
    assert len(channel.calls) == 1
    _assert_record(outbox, retried)

    clock.advance(1)
    assert outbox.dispatch_once(channel, "worker-2") == mid
    second = _leased(retried, "worker-2", clock.now())
    _assert_record(outbox, _sent(second, "ref-depois-da-espera", clock.now()))
    assert outbox.get(mid).attempts == 2
    assert channel.calls == [OutgoingMessage(message_id=mid, recipient=pid, text=text)] * 2
    assert _failed_events(client) == []


def test_undeliverable_fails_with_redacted_event(client: Any, outbox: Any, db_url: str, clock: Any,
                                                 sql: psycopg.Connection) -> None:
    """Falha definitiva: `Undeliverable(SEEDED)` → `failed` e
    `outbox.failed` exato, com o erro redigido e `actor_token_id: null`, sem nova tentativa."""
    body = register_person(client, ADMIN_TOKEN, **URSULA.payload())
    pid, alias = uuid.UUID(body["pseudo_id"]), body["alias"]
    reason = SEEDED_FREE_TEXT["outbox_reason"]
    redacted = reason.replace(URSULA.name, marker(alias)).replace(URSULA.ra, marker(alias))
    assert find_canary(redacted) == []
    text = "Mensagem sem destino possível"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)

    clock.advance(5)
    channel = ScriptedChannel([Undeliverable(reason)])
    assert outbox.dispatch_once(channel, "worker-1") == mid
    failed = _failed(_leased(_enqueued(mid, pid, text, t0), "worker-1", clock.now()), redacted)
    _assert_record(outbox, failed)
    stored = sql.execute("SELECT last_error FROM outbox_messages WHERE id = %s", (mid,)).fetchone()[0]
    assert stored == redacted
    assert _failed_events(client) == [_failed_event(mid, pid, 1, redacted, clock.now())]

    clock.advance(86400)
    assert outbox.dispatch_once(channel, "worker-1") is None
    assert len(channel.calls) == 1
    _assert_record(outbox, failed)


def test_retry_later_until_attempts_are_exhausted(client: Any, outbox: Any, db_url: str,
                                                  clock: Any) -> None:
    """`RetryLater` repetido espera 2 s e depois 4 s; na 3ª tentativa
    (`max_attempts`) vira `failed`, com `available_at` intacto, o texto da última falha e o evento."""
    pid, _ = _register(client, "Pessoa Destinataria Limite")
    text = "Mensagem que sempre adia"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    channel = ScriptedChannel([RetryLater("x"), RetryLater("x"), RetryLater("x")])

    assert outbox.dispatch_once(channel, "worker-1") == mid
    state = _retry(_leased(_enqueued(mid, pid, text, t0), "worker-1", t0), t0, 2, "x")
    _assert_record(outbox, state)

    clock.advance(2)
    assert outbox.dispatch_once(channel, "worker-1") == mid
    state = _retry(_leased(state, "worker-1", clock.now()), clock.now(), 4, "x")
    _assert_record(outbox, state)
    assert state.available_at == t0 + _s(6)

    clock.advance(10)
    assert outbox.dispatch_once(channel, "worker-1") == mid
    failed = _failed(_leased(state, "worker-1", clock.now()), "x")
    assert failed.attempts == 3 and failed.available_at == t0 + _s(6)
    _assert_record(outbox, failed)
    assert _failed_events(client) == [_failed_event(mid, pid, 3, "x", clock.now())]

    clock.advance(3600)
    assert outbox.dispatch_once(channel, "worker-1") is None
    assert len(channel.calls) == 3


def test_channel_exception_is_temporary_failure_without_message(client: Any, outbox: Any, db_url: str,
                                                                clock: Any,
                                                                sql: psycopg.Connection) -> None:
    """`RuntimeError(<sentinela>)` no `send` → `pending`
    com `"RuntimeError"` e a espera crescente; o sentinela fica fora do banco, dos eventos e dos
    logs."""
    pid, _ = _register(client, "Pessoa Destinataria Excecao")
    text = "Mensagem com canal quebrado"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    channel = ScriptedChannel([RuntimeError(SENTINEL)])

    with capture_logs() as cap:
        assert outbox.dispatch_once(channel, "worker-1") == mid
    _assert_record(outbox, _retry(_leased(_enqueued(mid, pid, text, t0), "worker-1", t0), t0, 2,
                                  "RuntimeError"))
    assert _dispatched(cap) == [(mid, "pending")]
    assert SENTINEL not in cap.text
    assert SENTINEL not in _database_text(sql)
    events = all_events(client, ADMIN_TOKEN, *[t.value for t in EventType])
    assert SENTINEL not in json.dumps(events, ensure_ascii=False)
    assert [e for e in events if e["type"] == "outbox.failed"] == []


def test_slow_send_result_is_recorded(client: Any, outbox: Any, db_url: str, clock: Any) -> None:
    """Mesmo que a posse tenha vencido durante o `send`: um canal que avança o
    relógio além da posse dentro do `send` → o resultado é registrado (`sent`)."""
    pid, _ = _register(client, "Pessoa Destinataria Lenta")
    text = "Mensagem de envio lento"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    channel = ScriptedChannel([Sent("ref-envio-lento")], on_send=lambda message: clock.advance(EXPIRE))

    assert outbox.dispatch_once(channel, "worker-1") == mid
    assert clock.now() == t0 + _s(EXPIRE)
    leased = _leased(_enqueued(mid, pid, text, t0), "worker-1", t0)
    _assert_record(outbox, _sent(leased, "ref-envio-lento", clock.now()))
    assert outbox.dispatch_once(ScriptedChannel([]), "worker-2") is None
    assert len(channel.calls) == 1


def test_crash_leaves_message_leased_until_lease_expires(client: Any, outbox: Any, db_url: str,
                                                         clock: Any, settings: Any) -> None:
    """uma `BaseException` que não é `Exception` escapa do
    `dispatch_once` e deixa a mensagem em posse (confirmada antes do envio); até a posse vencer
    (fronteira estrita), ninguém a pega; depois, outro worker a envia com `attempts = 2`."""
    pid, _ = _register(client, "Pessoa Destinataria Queda")
    text = "Mensagem interrompida"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)

    assert _crash_dispatch(db_url, clock, settings, "worker-a") == [
        OutgoingMessage(message_id=mid, recipient=pid, text=text)]
    leased = _leased(_enqueued(mid, pid, text, t0), "worker-a", t0)
    _assert_record(outbox, leased)

    channel_b = ScriptedChannel([Sent("ref-retomada")])
    with _other_worker(db_url, clock, settings) as worker_b:
        assert worker_b.dispatch_once(channel_b, "worker-b") is None
        clock.set(t0 + LEASE)
        assert worker_b.dispatch_once(channel_b, "worker-b") is None
        _assert_record(outbox, leased)
        clock.set(t0 + _s(EXPIRE))
        assert worker_b.dispatch_once(channel_b, "worker-b") == mid
    assert channel_b.calls == [OutgoingMessage(message_id=mid, recipient=pid, text=text)]
    retaken = _leased(leased, "worker-b", clock.now())
    assert retaken.attempts == 2
    _assert_record(outbox, _sent(retaken, "ref-retomada", clock.now()))


def test_recurring_crash_exhausts_attempts(client: Any, outbox: Any, db_url: str, clock: Any,
                                           settings: Any) -> None:
    """Queda recorrente (posse esgotada): 3 posses vencidas
    → no 4º `dispatch_once` a mensagem vira `failed` com `"posse vencida sem conclusão"` e
    `attempts = 3`, com `outbox.failed` exato, sem chamar o canal, sem `outbox.dispatched` e devolvendo
    `None`."""
    pid, _ = _register(client, "Pessoa Destinataria Recorrente")
    text = "Mensagem que derruba o worker"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    state = _enqueued(mid, pid, text, t0)
    for n in range(3):
        if n:
            clock.advance(EXPIRE)
        owner = f"worker-queda-{n}"
        assert _crash_dispatch(db_url, clock, settings, owner) == [
            OutgoingMessage(message_id=mid, recipient=pid, text=text)]
        state = _leased(state, owner, clock.now())
        _assert_record(outbox, state)
    assert state.attempts == 3

    clock.advance(EXPIRE)
    channel = ScriptedChannel([])
    with capture_logs() as cap:
        with _other_worker(db_url, clock, settings) as worker:
            assert worker.dispatch_once(channel, "worker-final") is None
    assert channel.calls == []
    assert cap.with_event("outbox.dispatched") == []
    failed = _failed(state, LEASE_EXHAUSTED)
    assert failed.attempts == 3 and failed.available_at == t0
    _assert_record(outbox, failed)
    assert _failed_events(client) == [_failed_event(mid, pid, 3, LEASE_EXHAUSTED, clock.now())]

    clock.advance(3600)
    assert outbox.dispatch_once(channel, "worker-final") is None
    assert channel.calls == []
    _assert_record(outbox, failed)


def test_lease_exhausted_then_next_message_is_dispatched(client: Any, outbox: Any, db_url: str,
                                                         clock: Any, settings: Any) -> None:
    """Posse esgotada (a busca segue para o próximo item): no mesmo
    `dispatch_once`, a mensagem de posse esgotada vira `failed` e a seguinte é enviada."""
    pid, _ = _register(client, "Pessoa Destinataria Seguinte")
    t0 = clock.now()
    m1 = _enqueue(outbox, db_url, pid, "Mensagem esgotada")
    state = _enqueued(m1, pid, "Mensagem esgotada", t0)
    for n in range(3):
        if n:
            clock.advance(EXPIRE)
        _crash_dispatch(db_url, clock, settings, f"worker-queda-{n}")
        state = _leased(state, f"worker-queda-{n}", clock.now())
    clock.advance(EXPIRE)
    now = clock.now()
    m2 = _enqueue(outbox, db_url, pid, "Mensagem seguinte")

    channel = ScriptedChannel([Sent("ref-seguinte")])
    assert outbox.dispatch_once(channel, "worker-final") == m2
    assert channel.calls == [OutgoingMessage(message_id=m2, recipient=pid, text="Mensagem seguinte")]
    _assert_record(outbox, _failed(state, LEASE_EXHAUSTED))
    second = _leased(_enqueued(m2, pid, "Mensagem seguinte", now), "worker-final", now)
    _assert_record(outbox, _sent(second, "ref-seguinte", now))
    assert _failed_events(client) == [_failed_event(m1, pid, 3, LEASE_EXHAUSTED, now)]


@pytest.mark.parametrize(("late_result", "outcome"), [
    (Sent("ref-atrasada-de-a"), "sent"),
    (RetryLater(SEEDED_FREE_TEXT["outbox_reason"], 5.0), "retry"),
    (Undeliverable(SEEDED_FREE_TEXT["outbox_reason"]), "undeliverable"),
], ids=["sent", "retry", "undeliverable"])
def test_late_result_is_discarded_after_takeover(client: Any, outbox: Any, db_url: str, clock: Any,
                                                 settings: Any, late_result: Any, outcome: str) -> None:
    """Resultado do envio: A pega a mensagem; dentro do `send` de A,
    o relógio passa da posse e B a retoma e envia; o resultado de A é descartado (estado e `attempts`
    de B intactos), com `outbox.result_discarded` ⊇ `{id, outcome}`, sem o `reason` no log.

    A pessoa do `reason` não é cadastrada, para que um `reason` registrado no log apareça cru."""
    pid, _ = _register(client, "Pessoa Destinataria Disputada")
    text = "Mensagem disputada"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, pid, text)
    takeover: dict[str, Any] = {}

    def run_b() -> None:
        try:
            channel_b = ScriptedChannel([Sent("ref-de-b")])
            with _other_worker(db_url, clock, settings) as worker_b:
                takeover["id"] = worker_b.dispatch_once(channel_b, "worker-b")
            takeover["calls"] = list(channel_b.calls)
        except BaseException as exc:  # a thread nunca deixa exceção escapar; o teste confere abaixo
            takeover["error"] = exc

    def during_a_send(message: OutgoingMessage) -> None:
        # Nada de `assert` aqui: uma exceção dentro do `send` seria tratada como falha do canal.
        clock.advance(EXPIRE)
        thread = threading.Thread(target=run_b, daemon=True)
        thread.start()
        thread.join(10)
        takeover["b_finished"] = not thread.is_alive()

    channel_a = ScriptedChannel([late_result], on_send=during_a_send)
    with capture_logs() as cap:
        with _other_worker(db_url, clock, settings) as worker_a:
            assert worker_a.dispatch_once(channel_a, "worker-a") == mid

    assert takeover.get("b_finished") is True, "B não terminou em 10 s: a posse de A ficou presa"
    assert "error" not in takeover, f"B falhou: {takeover.get('error')!r}"
    assert takeover["id"] == mid
    expected_message = OutgoingMessage(message_id=mid, recipient=pid, text=text)
    assert takeover["calls"] == [expected_message]
    assert channel_a.calls == [expected_message]
    by_a = _leased(_enqueued(mid, pid, text, t0), "worker-a", t0)
    by_b = _leased(by_a, "worker-b", t0 + _s(EXPIRE))
    assert by_b.attempts == 2
    _assert_record(outbox, _sent(by_b, "ref-de-b", t0 + _s(EXPIRE)))

    discarded = cap.with_event("outbox.result_discarded")
    assert len(discarded) == 1, cap.lines
    assert {"event": "outbox.result_discarded", "id": mid, "outcome": outcome}.items() <= discarded[0].items()
    assert "reason" not in discarded[0]
    assert find_canary(cap.text) == []
    assert _failed_events(client) == []


def test_fake_channel_of_another_class_works(client: Any, outbox: Any, db_url: str, clock: Any) -> None:
    """A fila só depende da porta `DeliveryChannel`: um canal falso de outra classe recebe
    as mensagens em ordem e as referências dele são registradas."""
    pid, _ = _register(client, "Pessoa Destinataria Caderno")
    t0 = clock.now()
    m1 = _enqueue(outbox, db_url, pid, "Primeira página")
    m2 = _enqueue(outbox, db_url, pid, "Segunda página")
    notebook = _NotebookChannel()

    assert outbox.dispatch_once(notebook, "worker-caderno") == m1
    assert outbox.dispatch_once(notebook, "worker-caderno") == m2
    assert outbox.dispatch_once(notebook, "worker-caderno") is None
    assert notebook.pages == [OutgoingMessage(message_id=m1, recipient=pid, text="Primeira página"),
                              OutgoingMessage(message_id=m2, recipient=pid, text="Segunda página")]
    for mid, text, ref in ((m1, "Primeira página", "caderno-1"), (m2, "Segunda página", "caderno-2")):
        leased = _leased(_enqueued(mid, pid, text, t0), "worker-caderno", t0)
        _assert_record(outbox, _sent(leased, ref, t0))


def test_dispatch_logs_final_state(client: Any, outbox: Any, db_url: str) -> None:
    """cada mensagem processada gera um registro `outbox.dispatched` ⊇ `{id, state}` no
    logger `cq_server.worker`, com o estado final (`sent`, `pending`, `failed`); sem mensagem, nenhum."""
    pid, _ = _register(client, "Pessoa Destinataria Log")
    m1 = _enqueue(outbox, db_url, pid, "Mensagem entregue")
    m2 = _enqueue(outbox, db_url, pid, "Mensagem adiada")
    m3 = _enqueue(outbox, db_url, pid, "Mensagem sem rota")
    channel = ScriptedChannel([Sent("ref-log"), RetryLater("tente depois"), Undeliverable("sem rota")])

    with capture_logs() as cap:
        assert [outbox.dispatch_once(channel, "worker-log") for _ in range(3)] == [m1, m2, m3]
        assert outbox.dispatch_once(channel, "worker-log") is None
    assert _dispatched(cap) == [(m1, "sent"), (m2, "pending"), (m3, "failed")]
    for record in cap.with_event("outbox.dispatched"):
        assert {"logger": "cq_server.worker", "event": "outbox.dispatched"}.items() <= record.items()
    assert [outbox.get(m).state for m in (m1, m2, m3)] == ["sent", "pending", "failed"]


def test_get_unknown_message_raises_lookup_error(client: Any, outbox: Any, db_url: str) -> None:
    """`Outbox.get`: um id inexistente → `LookupError`."""
    pid, _ = _register(client, "Pessoa Destinataria Consulta")
    mid = _enqueue(outbox, db_url, pid, "Mensagem existente")
    assert outbox.get(mid).id == mid
    with pytest.raises(LookupError):
        outbox.get(mid + 1000)


# --- concorrência ("pelo menos uma vez") ----------------------------------------------

class _ConcurrentChannel:
    """Canal de um worker do teste de concorrência: uma referência única por envio, registrada num
    diário comum sob lock; o 1º `send` espera na barreira (sem `assert`: uma exceção dentro do `send`
    seria tratada como falha do canal, então o erro vai para a lista de erros)."""

    def __init__(self, worker_id: str, barrier: threading.Barrier, lock: threading.Lock,
                 journal: list[tuple[str, OutgoingMessage, str]], errors: list[str]) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self._worker_id = worker_id
        self._barrier = barrier
        self._lock = lock
        self._journal = journal
        self._errors = errors
        self._first = True
        self._count = 0

    def send(self, message: OutgoingMessage) -> Sent:
        if self._first:
            self._first = False
            try:
                self._barrier.wait()
            except threading.BrokenBarrierError:
                with self._lock:
                    self._errors.append(
                        f"{self._worker_id}: os workers não seguraram mensagens ao mesmo tempo")
                raise
        self._count += 1
        ref = f"ref-{self._worker_id}-{self._count}"
        with self._lock:
            self._journal.append((self._worker_id, message, ref))
        return Sent(ref)


def test_concurrent_dispatchers_send_each_message_once(client: Any, outbox: Any, db_url: str,
                                                       clock: Any, settings: Any) -> None:
    """Pelo menos uma vez: sem queda, nenhuma duplicata, com vários workers
    concorrentes (cada mensagem é recebida uma vez): 200 mensagens confirmadas e 4
    threads, cada uma com a sua `Outbox` (mesmo `conninfo` e relógio, `worker_id` distintos) e o seu
    canal, em `dispatch_once` até `None` → cada mensagem enviada exatamente uma vez, com o conteúdo
    certo; todas `sent` com `attempts = 1`, o dono do envio e o `channel_ref` devolvido nele; os 4
    workers seguram mensagens ao mesmo tempo (barreira no 1º `send` de cada um: a posse é confirmada
    antes do envio); todas as filas fechadas no fim (`close` idempotente)."""
    total, workers = 200, 4
    pid, _ = _register(client, "Pessoa Destinataria Concorrente")
    t0 = clock.now()
    texts = [f"Mensagem concorrente {i:03d}" for i in range(total)]
    with psycopg.connect(db_url) as conn:
        ids = [outbox.enqueue(conn, recipient=pid, text=text) for text in texts]
    assert len(set(ids)) == total

    worker_ids = [f"worker-concorrente-{n}" for n in range(workers)]
    lock = threading.Lock()
    barrier = threading.Barrier(workers, timeout=30)
    journal: list[tuple[str, OutgoingMessage, str]] = []
    errors: list[str] = []
    dispatchers: list[Outbox] = []
    try:
        for _ in worker_ids:
            dispatchers.append(Outbox(db_url, clock=clock, settings=settings))

        def run(dispatcher: Outbox, worker_id: str) -> None:
            channel = _ConcurrentChannel(worker_id, barrier, lock, journal, errors)
            try:
                while dispatcher.dispatch_once(channel, worker_id) is not None:
                    pass
            except BaseException as exc:  # noqa: BLE001 - registrado e afirmado abaixo
                with lock:
                    errors.append(f"{worker_id}: {type(exc).__name__}")

        threads = [threading.Thread(target=run, args=(d, w), daemon=True)
                   for d, w in zip(dispatchers, worker_ids)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        assert [t.is_alive() for t in threads] == [False] * workers, "workers não terminaram em 120 s"
        assert errors == []
    finally:
        for d in dispatchers:
            d.close()
        for d in dispatchers:
            d.close()  # idempotente

    sends = Counter(message.message_id for _, message, _ in journal)
    assert sends == Counter({mid: 1 for mid in ids}), "mensagem perdida ou enviada mais de uma vez"
    assert sorted({worker_id for worker_id, _, _ in journal}) == worker_ids
    by_id = {message.message_id: (worker_id, message, ref) for worker_id, message, ref in journal}
    for mid, text in zip(ids, texts):
        worker_id, message, ref = by_id[mid]
        assert message == OutgoingMessage(message_id=mid, recipient=pid, text=text)
        leased = _leased(_enqueued(mid, pid, text, t0), worker_id, t0)
        _assert_record(outbox, _sent(leased, ref, t0))
    assert outbox.dispatch_once(ScriptedChannel([]), "worker-depois") is None
    assert _failed_events(client) == []
