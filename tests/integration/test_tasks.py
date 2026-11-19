"""Fila de tarefas (tarefa sem tratador
e queda recorrente).

Cada transição confere **todos** os campos do `TaskRecord` pelo estado esperado em cada transição da
fila de tarefas (os campos não citados ficam como estavam). Horários pelo `FakeClock`, conferidos
por igualdade de instante; ids nunca por valor literal (só que crescem). Os eventos `task.failed`
são lidos pela API (`events.list`) e conferidos por inteiro, sem o `seq`.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from cq_server.events import EventType
from cq_server.tasks import ClaimedTask, PermanentTaskError, TaskQueue, TaskRecord
from tests.helpers.api import ADMIN_TOKEN, all_events
from tests.helpers.channels import Crash
from tests.helpers.clock import START, iso
from tests.helpers.logs import capture_logs

T0 = START
LEASE = timedelta(seconds=30)
EXPIRE = 31  # avanço que vence a posse (a fronteira é estrita: lease_until < agora)
NO_HANDLER = "sem tratador para o tipo de tarefa"
LEASE_EXHAUSTED = "posse vencida sem conclusão"
# Mensagem de exceção que não pode ir ao banco, aos eventos nem aos logs (só o nome
# do tipo). Não tem forma de segredo nem de identidade, para que a redação não a esconda.
SENTINEL = "valor-sentinela-do-erro-7Q2w"


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (30, 3, 2, 60)


# --- estados esperados (o estado da fila de tarefas em cada transição) ------------------------

def _enqueued(task_id: int, task_type: str, at: datetime) -> TaskRecord:
    return TaskRecord(id=task_id, task_type=task_type, state="pending", attempts=0, available_at=at,
                      lease_owner=None, lease_until=None, last_error=None, finished_at=None)


def _leased(prev: TaskRecord, owner: str, now: datetime) -> TaskRecord:
    return dataclasses.replace(prev, state="leased", attempts=prev.attempts + 1, lease_owner=owner,
                               lease_until=now + LEASE)


def _done(prev: TaskRecord, now: datetime) -> TaskRecord:
    return dataclasses.replace(prev, state="done", lease_owner=None, lease_until=None, finished_at=now)


def _retry(prev: TaskRecord, now: datetime, wait_seconds: float, error: str) -> TaskRecord:
    return dataclasses.replace(prev, state="pending", available_at=now + timedelta(seconds=wait_seconds),
                               lease_owner=None, lease_until=None, last_error=error)


def _failed(prev: TaskRecord, now: datetime, error: str) -> TaskRecord:
    return dataclasses.replace(prev, state="failed", lease_owner=None, lease_until=None, finished_at=now,
                               last_error=error)


def _assert_record(queue: Any, expected: TaskRecord) -> None:
    actual = queue.get(expected.id)
    assert actual == expected, f"TaskRecord {actual!r} != {expected!r}"
    for name in ("available_at", "lease_until", "finished_at"):
        value = getattr(actual, name)
        if value is not None:
            assert value.utcoffset() == timedelta(0), f"{name} não está em UTC: {value!r}"


def _failed_events(client: Any) -> list[dict]:
    """Os eventos `task.failed` pela API, sem o `seq` (que só pode crescer)."""
    events = all_events(client, ADMIN_TOKEN, "task.failed")
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(set(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _failed_event(task_id: int, task_type: str, attempts: int, error: str, at: datetime) -> dict:
    return {"type": "task.failed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"task_id": task_id, "task_type": task_type, "attempts": attempts, "error": error}}


def _assert_processed(captured: Any, expected: list[tuple[int, str]]) -> None:
    """Um registro `task.processed` por tarefa processada, na ordem, ⊇ {logger, event, id, state}."""
    records = captured.with_event("task.processed")
    assert len(records) == len(expected), f"registros task.processed: {records}"
    for record, (task_id, state) in zip(records, expected):
        subset = {"logger": "cq_server.worker", "event": "task.processed", "id": task_id, "state": state}
        assert subset.items() <= record.items(), f"{record} não contém {subset}"


class _Recorder:
    """Tratador de teste que registra as tarefas recebidas e, opcionalmente, levanta ou avança o relógio."""

    def __init__(self, raises: BaseException | None = None,
                 before: Callable[[ClaimedTask], None] | None = None) -> None:
        self.calls: list[ClaimedTask] = []
        self.raises = raises
        self.before = before

    def __call__(self, task: ClaimedTask) -> None:
        self.calls.append(task)
        if self.before is not None:
            self.before(task)
        if self.raises is not None:
            raise self.raises


def _count_tasks(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT count(*) FROM tasks").fetchone()[0]


# --- idempotência --------------------------------------------------------------------

def test_same_key_same_content_returns_existing_task(queue: Any, clock: Any, sql: psycopg.Connection) -> None:
    """mesma chave e mesmo conteúdo → mesmo id, uma só tarefa, estado intacto (também
    depois de concluída, sem rodar de novo); o payload é comparado como objeto JSON."""
    payload = {"oferecimento": "mc102", "itens": [1, 2], "texto": "ação"}
    tid = queue.enqueue("demo.idempotente", payload, idempotency_key="chave-idem-1")
    record = _enqueued(tid, "demo.idempotente", T0)
    _assert_record(queue, record)

    clock.advance(5)
    same_payload_other_order = {"texto": "ação", "itens": [1, 2], "oferecimento": "mc102"}
    assert queue.enqueue("demo.idempotente", same_payload_other_order, idempotency_key="chave-idem-1") == tid
    _assert_record(queue, record)  # available_at continua o da primeira vez

    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.idempotente", payload=payload, attempts=1)
    record = _leased(record, "w1", clock.now())
    assert queue.complete(tid, "w1") is True
    record = _done(record, clock.now())
    _assert_record(queue, record)

    clock.advance(5)
    assert queue.enqueue("demo.idempotente", payload, idempotency_key="chave-idem-1") == tid
    _assert_record(queue, record)
    assert queue.claim("w1") is None
    rows = sql.execute("SELECT id, task_type, payload, idempotency_key FROM tasks").fetchall()
    assert rows == [(tid, "demo.idempotente", payload, "chave-idem-1")]


@pytest.mark.parametrize(("task_type", "payload"), [
    ("demo.idempotente", {"n": 2}),
    ("demo.outro_tipo", {"n": 1}),
    ("demo.outro_tipo", {"n": 2}),
], ids=["outro-payload", "outro-tipo", "os-dois"])
def test_same_key_different_content_raises_value_error(queue: Any, sql: psycopg.Connection,
                                                       task_type: str, payload: dict) -> None:
    """mesma chave com conteúdo diferente → `ValueError`, sem criar nem
    alterar tarefa; a fila segue usável."""
    tid = queue.enqueue("demo.idempotente", {"n": 1}, idempotency_key="chave-idem-2")
    with pytest.raises(ValueError):
        queue.enqueue(task_type, payload, idempotency_key="chave-idem-2")
    _assert_record(queue, _enqueued(tid, "demo.idempotente", T0))
    other = queue.enqueue("demo.depois", {"n": 3})
    assert other > tid
    rows = sql.execute("SELECT id, task_type, payload, idempotency_key FROM tasks ORDER BY id").fetchall()
    assert rows == [(tid, "demo.idempotente", {"n": 1}, "chave-idem-2"), (other, "demo.depois", {"n": 3}, None)]


def test_conflicting_key_keeps_caller_transaction_usable(queue: Any, db_url: str, sql: psycopg.Connection) -> None:
    """`enqueue` com `conn`: o `ValueError` da chave repetida não provoca erro no banco; a
    transação do chamador segue usável (o que veio antes e depois é confirmado no `commit`)."""
    original = queue.enqueue("demo.idempotente", {"n": 1}, idempotency_key="chave-idem-3")
    with psycopg.connect(db_url) as conn:
        before = queue.enqueue("demo.antes", {"n": 2}, conn=conn)
        with pytest.raises(ValueError):
            queue.enqueue("demo.idempotente", {"n": 999}, idempotency_key="chave-idem-3", conn=conn)
        with pytest.raises(ValueError):
            queue.enqueue("demo.outro_tipo", {"n": 1}, idempotency_key="chave-idem-3", conn=conn)
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        after = queue.enqueue("demo.depois", {"n": 3}, conn=conn)
        conn.execute("SELECT 1").fetchone()
        conn.commit()
    rows = sql.execute("SELECT id, task_type, payload, idempotency_key, state FROM tasks ORDER BY id").fetchall()
    assert rows == [
        (original, "demo.idempotente", {"n": 1}, "chave-idem-3", "pending"),
        (before, "demo.antes", {"n": 2}, None, "pending"),
        (after, "demo.depois", {"n": 3}, None, "pending"),
    ]
    assert original < before < after


def test_enqueue_without_key_twice_creates_two_tasks(queue: Any, sql: psycopg.Connection) -> None:
    """sem chave, o mesmo conteúdo duas vezes → duas tarefas, com ids crescentes."""
    first = queue.enqueue("demo.sem_chave", {"n": 1})
    second = queue.enqueue("demo.sem_chave", {"n": 1})
    assert second > first
    _assert_record(queue, _enqueued(first, "demo.sem_chave", T0))
    _assert_record(queue, _enqueued(second, "demo.sem_chave", T0))
    assert _count_tasks(sql) == 2


def test_enqueue_in_caller_transaction_is_visible_only_after_commit(queue: Any, db_url: str,
                                                                    clock: Any) -> None:
    """com `conn`, a tarefa entra na transação do chamador e só existe para a fila
    depois do `commit`."""
    clock.advance(12)
    with psycopg.connect(db_url) as conn:
        tid = queue.enqueue("demo.transacional", {"n": 1}, conn=conn)
        assert queue.claim("w1") is None
        with pytest.raises(LookupError):
            queue.get(tid)
        conn.commit()
    record = _enqueued(tid, "demo.transacional", T0 + timedelta(seconds=12))
    _assert_record(queue, record)
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.transacional", payload={"n": 1}, attempts=1)


def test_enqueue_in_rolled_back_transaction_leaves_nothing(queue: Any, db_url: str, sql: psycopg.Connection) -> None:
    """`enqueue` com `conn`: `enqueue` numa transação desfeita não deixa tarefa nem
    reserva a chave."""
    with psycopg.connect(db_url) as conn:
        tid = queue.enqueue("demo.desfeita", {"n": 1}, idempotency_key="chave-desfeita", conn=conn)
        conn.rollback()
    with pytest.raises(LookupError):
        queue.get(tid)
    assert queue.claim("w1") is None
    assert _count_tasks(sql) == 0
    # a chave da transação desfeita não ficou reservada
    again = queue.enqueue("demo.outra", {"n": 2}, idempotency_key="chave-desfeita")
    _assert_record(queue, _enqueued(again, "demo.outra", T0))


# --- task_type ---------------------------------------------------------------------------------

@pytest.mark.parametrize("task_type", [
    "", "Maiúscula", "a" * 101, "Demo.tarefa", "9tarefa", "tarefa-com-hifen", "tarefa com espaco", ".tarefa",
], ids=["vazio", "maiuscula-acento", "101-caracteres", "inicial-maiuscula", "inicial-digito", "hifen",
        "espaco", "inicial-ponto"])
def test_enqueue_rejects_invalid_task_type(queue: Any, db_url: str, sql: psycopg.Connection, task_type: str) -> None:
    """`task_type` fora de `^[a-z][a-z0-9_.]{0,99}$` → `ValueError`, sem tarefa e
    sem erro no banco (a transação do chamador segue usável)."""
    with pytest.raises(ValueError):
        queue.enqueue(task_type, {"n": 1})
    with psycopg.connect(db_url) as conn:
        with pytest.raises(ValueError):
            queue.enqueue(task_type, {"n": 1}, conn=conn)
        assert conn.info.transaction_status in (TransactionStatus.IDLE, TransactionStatus.INTRANS)
        kept = queue.enqueue("demo.valida", {"n": 2}, conn=conn)
        conn.commit()
    rows = sql.execute("SELECT id, task_type FROM tasks").fetchall()
    assert rows == [(kept, "demo.valida")]


@pytest.mark.parametrize("task_type", ["a", "a" * 100, "demo.tipo_2.sub"], ids=["1", "100", "pontos-e-digitos"])
def test_enqueue_accepts_task_type_limits(queue: Any, task_type: str) -> None:
    """os limites do padrão de `task_type` (1 e 100 caracteres) são aceitos."""
    tid = queue.enqueue(task_type, {"n": 1})
    _assert_record(queue, _enqueued(tid, task_type, T0))
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type=task_type, payload={"n": 1}, attempts=1)


# --- posse, disponibilidade e ordem -------------------------------------

def test_claim_takes_exclusive_lease(queue: Any, clock: Any) -> None:
    """`claim` → `leased`, `attempts = 1`, dono e `lease_until = agora + 30 s`; outro
    worker → `None`; na fronteira (`lease_until == agora`) a posse ainda vale; depois dela, outro
    worker retoma com `attempts = 2`."""
    tid = queue.enqueue("demo.posse", {"n": 1})
    record = _enqueued(tid, "demo.posse", T0)
    clock.advance(3)
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.posse", payload={"n": 1}, attempts=1)
    record = _leased(record, "w1", clock.now())
    _assert_record(queue, record)
    assert record.lease_until == T0 + timedelta(seconds=33)

    assert queue.claim("w2") is None
    assert queue.claim("w1") is None
    clock.advance(30)  # agora == lease_until: ainda não venceu
    assert queue.claim("w2") is None
    _assert_record(queue, record)

    clock.advance(1)
    assert queue.claim("w2") == ClaimedTask(id=tid, task_type="demo.posse", payload={"n": 1}, attempts=2)
    record = _leased(record, "w2", clock.now())
    _assert_record(queue, record)


def test_claim_respects_available_at(queue: Any, clock: Any) -> None:
    """só é pega quando `available_at <= agora` (relógio que volta)."""
    tid = queue.enqueue("demo.disponivel", {"n": 1})
    record = _enqueued(tid, "demo.disponivel", T0)
    clock.set(T0 - timedelta(seconds=10))
    assert queue.claim("w1") is None
    _assert_record(queue, record)
    clock.set(T0)
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.disponivel", payload={"n": 1}, attempts=1)
    _assert_record(queue, _leased(record, "w1", T0))


def test_claim_order_follows_id_not_clock(queue: Any, clock: Any) -> None:
    """Ordem: t1 em T e t2 em T−1 h, com o relógio em T+1 s → pega t1 e depois t2."""
    t1 = queue.enqueue("demo.ordem", {"n": 1})
    clock.set(T0 - timedelta(hours=1))
    t2 = queue.enqueue("demo.ordem", {"n": 2})
    assert t2 > t1
    _assert_record(queue, _enqueued(t2, "demo.ordem", T0 - timedelta(hours=1)))
    clock.set(T0 + timedelta(seconds=1))
    assert queue.claim("w1") == ClaimedTask(id=t1, task_type="demo.ordem", payload={"n": 1}, attempts=1)
    assert queue.claim("w1") == ClaimedTask(id=t2, task_type="demo.ordem", payload={"n": 2}, attempts=1)
    _assert_record(queue, _leased(_enqueued(t1, "demo.ordem", T0), "w1", clock.now()))
    _assert_record(queue, _leased(_enqueued(t2, "demo.ordem", T0 - timedelta(hours=1)), "w1", clock.now()))


# --- conclusão ---------------------------------------------------------------------------

def test_complete_only_by_owner_with_valid_lease(queue: Any, clock: Any, client: Any) -> None:
    """`complete`/`fail` de outro worker → `False` e nada muda; o dono conclui
    na fronteira (`lease_until >= agora`) → `done`; chamadas depois de concluída → `False`."""
    tid = queue.enqueue("demo.conclusao", {"n": 1})
    record = _leased(_enqueued(tid, "demo.conclusao", T0), "w1", T0)
    assert queue.claim("w1") is not None
    assert queue.complete(tid, "w2") is False
    assert queue.fail(tid, "w2", "erro de intruso") is False
    assert queue.fail(tid, "w2", "erro de intruso", permanent=True) is False
    _assert_record(queue, record)

    clock.advance(30)  # agora == lease_until: a posse ainda vale
    assert queue.complete(tid, "w1") is True
    record = _done(record, clock.now())
    _assert_record(queue, record)

    clock.advance(1)
    assert queue.complete(tid, "w1") is False
    assert queue.fail(tid, "w1", "erro tardio") is False
    assert queue.fail(tid, "w1", "erro tardio", permanent=True) is False
    _assert_record(queue, record)
    assert queue.claim("w2") is None
    assert _failed_events(client) == []


def test_expired_lease_is_retaken_and_late_calls_change_nothing(queue: Any, clock: Any, client: Any) -> None:
    """depois da posse vencida, o `complete` do dono → `False`; outro
    worker retoma (`attempts = 2`) e conclui; o `complete`/`fail` atrasado do primeiro → `False` e nada
    muda (antes e depois da conclusão)."""
    tid = queue.enqueue("demo.retomada", {"n": 1})
    record = _enqueued(tid, "demo.retomada", T0)
    assert queue.claim("w1") is not None
    record = _leased(record, "w1", T0)

    clock.advance(EXPIRE)
    assert queue.complete(tid, "w1") is False
    assert queue.fail(tid, "w1", "erro atrasado") is False
    _assert_record(queue, record)

    assert queue.claim("w2") == ClaimedTask(id=tid, task_type="demo.retomada", payload={"n": 1}, attempts=2)
    record = _leased(record, "w2", clock.now())
    _assert_record(queue, record)

    assert queue.complete(tid, "w1") is False
    assert queue.fail(tid, "w1", "erro atrasado") is False
    assert queue.fail(tid, "w1", "erro atrasado", permanent=True) is False
    _assert_record(queue, record)

    clock.advance(5)
    assert queue.complete(tid, "w2") is True
    record = _done(record, clock.now())
    _assert_record(queue, record)

    assert queue.complete(tid, "w1") is False
    assert queue.fail(tid, "w1", "erro atrasado", permanent=True) is False
    _assert_record(queue, record)
    assert _failed_events(client) == []


# --- falhas ------------------------------------------------------------------------------

def test_temporary_failure_backs_off_then_fails_with_event(queue: Any, clock: Any, client: Any) -> None:
    """espera de 2 s e depois 4 s (disponível só a partir dela); na 3ª
    tentativa → `failed` com o texto da última falha e um único `task.failed` exato."""
    tid = queue.enqueue("demo.temporaria", {"n": 1})
    record = _enqueued(tid, "demo.temporaria", T0)

    assert queue.claim("w1") is not None
    record = _leased(record, "w1", clock.now())
    assert queue.fail(tid, "w1", "falha temporaria um") is True
    record = _retry(record, clock.now(), 2, "falha temporaria um")
    _assert_record(queue, record)
    assert record.available_at == T0 + timedelta(seconds=2)

    clock.advance(1)
    assert queue.claim("w1") is None
    clock.advance(1)  # agora == available_at
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.temporaria", payload={"n": 1}, attempts=2)
    record = _leased(record, "w1", clock.now())
    _assert_record(queue, record)  # last_error e available_at ficam como estavam
    assert queue.fail(tid, "w1", "falha temporaria dois") is True
    record = _retry(record, clock.now(), 4, "falha temporaria dois")
    _assert_record(queue, record)
    assert _failed_events(client) == []

    clock.advance(3)
    assert queue.claim("w1") is None
    clock.advance(1)
    assert queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.temporaria", payload={"n": 1}, attempts=3)
    record = _leased(record, "w1", clock.now())
    _assert_record(queue, record)
    clock.advance(2)
    failed_at = clock.now()
    assert queue.fail(tid, "w1", "falha temporaria tres") is True
    record = _failed(record, failed_at, "falha temporaria tres")
    _assert_record(queue, record)
    assert _failed_events(client) == [_failed_event(tid, "demo.temporaria", 3, "falha temporaria tres", failed_at)]

    clock.advance(3600)
    assert queue.claim("w1") is None
    assert queue.fail(tid, "w1", "mais uma") is False
    _assert_record(queue, record)
    assert len(_failed_events(client)) == 1


def test_backoff_is_capped_by_backoff_max(db_url: str, clock: Any, settings: Any) -> None:
    """`espera = min(base · 2^(attempts−1), máximo)`: com máximo 5 s e 10
    tentativas, as esperas são 2, 4, 5, 5, 5."""
    capped = dataclasses.replace(settings, max_attempts=10, backoff_max_seconds=5)
    capped_queue = TaskQueue(db_url, clock=clock, settings=capped)
    try:
        tid = capped_queue.enqueue("demo.teto", {})
        record = _enqueued(tid, "demo.teto", T0)
        for attempt, wait_seconds in enumerate([2, 4, 5, 5, 5], start=1):
            assert capped_queue.claim("w1") == ClaimedTask(id=tid, task_type="demo.teto", payload={},
                                                           attempts=attempt)
            record = _leased(record, "w1", clock.now())
            assert capped_queue.fail(tid, "w1", f"falha {attempt}") is True
            record = _retry(record, clock.now(), wait_seconds, f"falha {attempt}")
            _assert_record(capped_queue, record)
            clock.advance(wait_seconds)
    finally:
        capped_queue.close()


def test_permanent_failure_fails_at_once_with_event(queue: Any, clock: Any, client: Any) -> None:
    """`fail(permanent=True)` na 1ª tentativa → `failed` na hora, com `task.failed` exato e sem
    nova tentativa."""
    tid = queue.enqueue("demo.definitiva", {"n": 1})
    assert queue.claim("w1") is not None
    record = _leased(_enqueued(tid, "demo.definitiva", T0), "w1", T0)
    clock.advance(4)
    assert queue.fail(tid, "w1", "falha definitiva pedida", permanent=True) is True
    record = _failed(record, clock.now(), "falha definitiva pedida")
    _assert_record(queue, record)
    assert _failed_events(client) == [
        _failed_event(tid, "demo.definitiva", 1, "falha definitiva pedida", T0 + timedelta(seconds=4))]
    clock.advance(86400)
    assert queue.claim("w1") is None
    _assert_record(queue, record)


# --- work_once ---------------------------------------------------------------------------------

def test_work_once_runs_handler_and_completes(queue: Any, clock: Any, client: Any) -> None:
    """`work_once`: chama o tratador do tipo com a `ClaimedTask`,
    devolve o id, conclui (`finished_at` = agora na conclusão) e registra `task.processed`."""
    payload = {"oferecimento": "mc102", "lista": [1, "dois"], "aninhado": {"ok": True}}
    tid = queue.enqueue("demo.sucesso", payload)
    handler = _Recorder(before=lambda task: clock.advance(7))
    with capture_logs() as captured:
        assert queue.work_once("w1", {"demo.sucesso": handler, "demo.outro": _Recorder()}) == tid
    assert handler.calls == [ClaimedTask(id=tid, task_type="demo.sucesso", payload=payload, attempts=1)]
    record = _done(_leased(_enqueued(tid, "demo.sucesso", T0), "w1", T0), T0 + timedelta(seconds=7))
    _assert_record(queue, record)
    _assert_processed(captured, [(tid, "done")])
    assert _failed_events(client) == []


def test_work_once_permanent_task_error_fails_with_type_name(queue: Any, clock: Any, client: Any) -> None:
    """`PermanentTaskError("x")` → `failed` na hora com
    `"PermanentTaskError"` (só o nome do tipo), evento e `task.processed` com `state: failed`."""
    tid = queue.enqueue("demo.permanente", {"n": 1})
    handler = _Recorder(raises=PermanentTaskError("x"))
    with capture_logs() as captured:
        assert queue.work_once("w1", {"demo.permanente": handler}) == tid
    assert handler.calls == [ClaimedTask(id=tid, task_type="demo.permanente", payload={"n": 1}, attempts=1)]
    record = _failed(_leased(_enqueued(tid, "demo.permanente", T0), "w1", T0), T0, "PermanentTaskError")
    _assert_record(queue, record)
    _assert_processed(captured, [(tid, "failed")])
    assert _failed_events(client) == [_failed_event(tid, "demo.permanente", 1, "PermanentTaskError", T0)]
    clock.advance(3600)
    assert queue.work_once("w1", {"demo.permanente": handler}) is None
    assert len(handler.calls) == 1


def test_work_once_other_exception_is_temporary_and_keeps_only_type_name(
        queue: Any, clock: Any, client: Any, sql: psycopg.Connection) -> None:
    """`ValueError(<sentinela>)` → `pending` com `"ValueError"`
    e espera; esgotadas as tentativas, `failed` com `"ValueError"` no evento; o sentinela não aparece no
    banco, nos eventos nem nos logs."""
    tid = queue.enqueue("demo.sentinela", {"n": 1})
    handler = _Recorder(raises=ValueError(SENTINEL))
    handlers = {"demo.sentinela": handler}
    record = _enqueued(tid, "demo.sentinela", T0)
    with capture_logs() as captured:
        assert queue.work_once("w1", handlers) == tid
        record = _retry(_leased(record, "w1", clock.now()), clock.now(), 2, "ValueError")
        _assert_record(queue, record)

        clock.advance(2)
        assert queue.work_once("w1", handlers) == tid
        record = _retry(_leased(record, "w1", clock.now()), clock.now(), 4, "ValueError")
        _assert_record(queue, record)

        clock.advance(4)
        assert queue.work_once("w1", handlers) == tid
        record = _failed(_leased(record, "w1", clock.now()), clock.now(), "ValueError")
        _assert_record(queue, record)
    assert [c.attempts for c in handler.calls] == [1, 2, 3]
    _assert_processed(captured, [(tid, "pending"), (tid, "pending"), (tid, "failed")])
    assert _failed_events(client) == [
        _failed_event(tid, "demo.sentinela", 3, "ValueError", T0 + timedelta(seconds=6))]

    dumps = [row[0] for row in sql.execute("SELECT t::text FROM tasks t").fetchall()]
    dumps += [row[0] for row in sql.execute("SELECT e::text FROM events e").fetchall()]
    assert dumps, "a varredura do banco não pode ser vazia"
    assert all(SENTINEL not in dump for dump in dumps), "a mensagem da exceção foi gravada no banco"
    every_event = all_events(client, ADMIN_TOKEN, *[t.value for t in EventType])
    assert SENTINEL not in json.dumps(every_event, ensure_ascii=False)
    assert captured.lines, "a captura de logs não pode ser vazia"
    assert SENTINEL not in captured.text and SENTINEL.casefold() not in captured.text.casefold()


def test_work_once_without_handler_fails_task_and_continues(queue: Any, clock: Any, client: Any) -> None:
    """Tarefa sem tratador: tipo sem tratador → `failed` com
    `"sem tratador para o tipo de tarefa"` e evento, sem levantar; a próxima tarefa é processada."""
    orphan = queue.enqueue("demo.sem_tratador", {"n": 1})
    clock.advance(1)
    other = queue.enqueue("demo.com_tratador", {"n": 2})
    handler = _Recorder()
    handlers = {"demo.com_tratador": handler}
    with capture_logs() as captured:
        assert queue.work_once("w1", handlers) == orphan
        orphan_record = _failed(_leased(_enqueued(orphan, "demo.sem_tratador", T0), "w1", clock.now()),
                                clock.now(), NO_HANDLER)
        _assert_record(queue, orphan_record)
        assert handler.calls == []

        assert queue.work_once("w1", handlers) == other
        assert handler.calls == [ClaimedTask(id=other, task_type="demo.com_tratador", payload={"n": 2}, attempts=1)]
        other_record = _done(_leased(_enqueued(other, "demo.com_tratador", clock.now()), "w1", clock.now()),
                             clock.now())
        _assert_record(queue, other_record)
        assert queue.work_once("w1", handlers) is None
    _assert_processed(captured, [(orphan, "failed"), (other, "done")])
    assert _failed_events(client) == [
        _failed_event(orphan, "demo.sem_tratador", 1, NO_HANDLER, T0 + timedelta(seconds=1))]


def test_work_once_on_empty_queue_returns_none(queue: Any) -> None:
    """fila vazia → `None`, sem chamar tratador nem registrar `task.processed`."""
    handler = _Recorder()
    with capture_logs() as captured:
        assert queue.work_once("w1", {"demo.qualquer": handler}) is None
    assert handler.calls == []
    assert captured.with_event("task.processed") == []


def test_crash_in_handler_leaves_lease_and_another_worker_finishes(queue: Any, db_url: str, clock: Any,
                                                                   settings: Any, client: Any) -> None:
    """uma queda (`BaseException` que não é `Exception`, como o `Crash` dos testes de
    falha) no tratador escapa de `work_once` e deixa a tarefa em posse (a posse é confirmada
    antes do processamento); depois que a posse vence, outro worker a conclui com
    `attempts = 2`."""
    tid = queue.enqueue("demo.queda", {"n": 1})
    crashing = _Recorder(raises=Crash())
    with pytest.raises(Crash):
        queue.work_once("w1", {"demo.queda": crashing})
    record = _leased(_enqueued(tid, "demo.queda", T0), "w1", T0)
    _assert_record(queue, record)

    clock.advance(EXPIRE)
    second = TaskQueue(db_url, clock=clock, settings=settings)
    try:
        handler = _Recorder()
        assert second.work_once("w2", {"demo.queda": handler}) == tid
        assert handler.calls == [ClaimedTask(id=tid, task_type="demo.queda", payload={"n": 1}, attempts=2)]
        record = _done(_leased(record, "w2", clock.now()), clock.now())
        _assert_record(second, record)
    finally:
        second.close()
    assert _failed_events(client) == []


# --- queda recorrente --------------------------------------------------------------------------

def _expire_three_leases(queue: Any, clock: Any, task_id: int, task_type: str) -> TaskRecord:
    record = _enqueued(task_id, task_type, T0)
    for n in (1, 2, 3):
        assert queue.claim(f"w{n}") == ClaimedTask(id=task_id, task_type=task_type, payload={"n": 1}, attempts=n)
        record = _leased(record, f"w{n}", clock.now())
        _assert_record(queue, record)
        clock.advance(EXPIRE)
    return record


@pytest.mark.parametrize("via", ["claim", "work_once"])
def test_recurring_expired_lease_fails_task(queue: Any, clock: Any, client: Any, via: str) -> None:
    """Queda recorrente (posse esgotada): 3 posses vencidas → no 4º
    `claim` a tarefa vira `failed` com `attempts = 3` e `"posse vencida sem conclusão"`, com evento,
    sem chamar o tratador e sem `task.processed`; depois, `claim` → `None`."""
    tid = queue.enqueue("demo.recorrente", {"n": 1})
    record = _expire_three_leases(queue, clock, tid, "demo.recorrente")
    exhausted_at = clock.now()
    handler = _Recorder()
    with capture_logs() as captured:
        if via == "claim":
            assert queue.claim("w4") is None
        else:
            assert queue.work_once("w4", {"demo.recorrente": handler}) is None
    assert handler.calls == []
    assert captured.with_event("task.processed") == []
    record = _failed(record, exhausted_at, LEASE_EXHAUSTED)
    assert record.attempts == 3
    _assert_record(queue, record)
    assert _failed_events(client) == [_failed_event(tid, "demo.recorrente", 3, LEASE_EXHAUSTED, exhausted_at)]

    clock.advance(EXPIRE)
    assert queue.claim("w5") is None
    _assert_record(queue, record)
    assert len(_failed_events(client)) == 1


def test_recurring_expired_lease_search_continues_to_next_task(queue: Any, clock: Any, client: Any) -> None:
    """Queda recorrente: ao esgotar a posse de uma tarefa, a busca segue
    para a próxima, que é pega normalmente."""
    tid = queue.enqueue("demo.recorrente", {"n": 1})
    record = _expire_three_leases(queue, clock, tid, "demo.recorrente")
    exhausted_at = clock.now()
    nxt = queue.enqueue("demo.proxima", {"n": 2})
    assert queue.claim("w4") == ClaimedTask(id=nxt, task_type="demo.proxima", payload={"n": 2}, attempts=1)
    _assert_record(queue, _failed(record, exhausted_at, LEASE_EXHAUSTED))
    _assert_record(queue, _leased(_enqueued(nxt, "demo.proxima", exhausted_at), "w4", exhausted_at))
    assert _failed_events(client) == [_failed_event(tid, "demo.recorrente", 3, LEASE_EXHAUSTED, exhausted_at)]


# --- consulta ------------------------------------------------------------------------------------

def test_get_unknown_task_raises_lookup_error(queue: Any) -> None:
    """`get`: id inexistente → `LookupError`."""
    tid = queue.enqueue("demo.existente", {"n": 1})
    _assert_record(queue, _enqueued(tid, "demo.existente", T0))
    for missing in (tid + 1_000_000, 0, -1):
        with pytest.raises(LookupError):
            queue.get(missing)


# --- concorrência ---------------------------------------------------------------------

def _other_client_backends(sql: psycopg.Connection) -> int:
    return sql.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND backend_type = 'client backend' AND pid <> pg_backend_pid()").fetchone()[0]


def test_concurrent_workers_complete_each_task_exactly_once(db_url: str, clock: Any, settings: Any,
                                                            sql: psycopg.Connection) -> None:
    """1.000 tarefas e 4 threads, cada uma com a sua `TaskQueue` (mesmo
    `conninfo` e relógio, `worker_id` distintos), em `work_once` até `None` → cada id tratado
    exatamente uma vez, todas `done` com `attempts = 1`; os 4 workers seguram tarefas ao mesmo tempo
    (barreira no 1º tratamento de cada um: a posse é curta e o processamento fica fora dela);
    todas as filas fechadas no fim (`close` idempotente), sem conexão aberta."""
    total, workers = 1000, 4
    queues: list[TaskQueue] = []
    try:
        for _ in range(workers):
            queues.append(TaskQueue(db_url, clock=clock, settings=settings))
        with psycopg.connect(db_url) as conn:
            ids = [queues[0].enqueue("demo.carga", {"i": i}, conn=conn) for i in range(total)]
            conn.commit()
        assert len(set(ids)) == total

        counts: Counter[int] = Counter()
        per_worker: Counter[str] = Counter()
        lock = threading.Lock()
        errors: list[str] = []
        barrier = threading.Barrier(workers, timeout=30)

        def make_handler(worker_id: str) -> Callable[[ClaimedTask], None]:
            first = [True]

            def handler(task: ClaimedTask) -> None:
                if first[0]:
                    first[0] = False
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        with lock:
                            errors.append(f"{worker_id}: os workers não seguraram tarefas ao mesmo tempo")
                        raise
                with lock:
                    counts[task.id] += 1
                    per_worker[worker_id] += 1

            return handler

        def run(q: TaskQueue, worker_id: str) -> None:
            handlers = {"demo.carga": make_handler(worker_id)}
            try:
                while q.work_once(worker_id, handlers) is not None:
                    pass
            except BaseException as exc:  # noqa: BLE001 - registrado e afirmado abaixo
                with lock:
                    errors.append(f"{worker_id}: {type(exc).__name__}")

        threads = [threading.Thread(target=run, args=(q, f"worker-{n}"), daemon=True)
                   for n, q in enumerate(queues)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(180)
        assert [t.is_alive() for t in threads] == [False] * workers, "workers não terminaram em 180 s"
        assert errors == []
        assert counts == Counter({tid: 1 for tid in ids})
        assert sorted(per_worker) == [f"worker-{n}" for n in range(workers)]
        rows = sql.execute("SELECT id, state, attempts, lease_owner, lease_until FROM tasks ORDER BY id").fetchall()
        assert rows == [(tid, "done", 1, None, None) for tid in sorted(ids)]
        assert queues[0].work_once("worker-0", {"demo.carga": _Recorder()}) is None
    finally:
        for q in queues:
            q.close()
        for q in queues:
            q.close()  # idempotente

    # A saída do backend do servidor é assíncrona ao fechamento no cliente: espera real, com prazo.
    deadline = time.monotonic() + 5
    remaining = _other_client_backends(sql)
    while remaining and time.monotonic() < deadline:
        threading.Event().wait(0.05)
        remaining = _other_client_backends(sql)
    assert remaining == 0, f"{remaining} conexões ao banco ainda abertas depois de close()"
