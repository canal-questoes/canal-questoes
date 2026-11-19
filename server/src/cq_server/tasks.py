"""Fila de tarefas.

- **Enfileirar** (`enqueue`): na transação do chamador (`conn=`) ou numa própria. As validações vêm
  antes de qualquer SQL e a chave de idempotência é conferida por consulta, com
  `ON CONFLICT DO NOTHING` só para a corrida: um `ValueError` nunca deixa erro no banco, e a
  transação do chamador segue usável.
- **Pegar** (`claim`): numa transação curta, a de menor `id` disponível
  (`pending` com `available_at <= agora`, ou `leased` com a posse vencida), com
  `FOR UPDATE SKIP LOCKED`; ela passa a `leased`, com `attempts + 1`. O processamento fica fora
  dessa transação, então uma queda no meio deixa a tarefa em posse até a posse vencer.
- **Posse esgotada:** a tarefa em posse vencida com `attempts >= max_attempts` vira `failed` sem
  incrementar `attempts`, com evento, e a busca segue para a próxima.
- **Concluir/falhar** (`complete`/`fail`): só o dono com posse vigente (`lease_until >= agora`).
- Ao virar `failed`, o `last_error` (redigido) e o evento `task.failed` (redigido) entram na mesma
  transação (nada se perde). Toda falha registrada lê as identidades da redação depois do lock
  consultivo dos eventos (`events.load_write_identities`), sempre depois da trava da linha.
- Um texto com U+0000 ou substituto UTF-16 isolado é recusado no `enqueue` (`ValueError`) e trocado
  por U+FFFD no erro de `fail` (`_leasing.storable_text`), para que registrar a falha nunca falhe.
- **Espera mínima**: um tratador que lança `RetryTaskLater(n)`
  falha de forma temporária com `available_at = agora + min(max(espera padrão, n), MAX_WAIT_SECONDS)`,
  como a fila de saída faz com o `retry_after`; conta como tentativa (na última, é falha definitiva).

Todo horário vem do `Clock` injetado; a ordem é sempre a de `id`. Cada instância é usada por
uma thread e tem a sua conexão (`_leasing.LazyConnection`).

Varredura: `has_live_task` e
`finished_rounds`, auxiliares da varredura (`scheduler`), da continuação da validação e da
`question.refetch`, por consulta sobre o `payload` (sem coluna própria). O assunto é
o payload inteiro ou parte dele (`payload @> subject`), sempre com o tipo; consultam na transação do
chamador, sem travar nada.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server._leasing import (
    LEASE_EXHAUSTED_ERROR,
    MAX_WAIT_SECONDS,
    LazyConnection,
    backoff_seconds,
    has_unstorable,
    storable_text,
    utc,
)
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.events import EventType, append_event, load_write_identities, redact_free_text

logger = logging.getLogger("cq_server.worker")

TASK_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_.]{0,99}")

# O texto de `last_error` quando falta o tratador.
NO_HANDLER_ERROR = "sem tratador para o tipo de tarefa"


class PermanentTaskError(Exception):
    """Falha definitiva: a tarefa não deve ser tentada de novo."""


class RetryTaskLater(Exception):
    """Falha temporária com espera mínima: a próxima tentativa não
    vem antes de `min(max(backoff, seconds), MAX_WAIT_SECONDS)`; conta como tentativa.

    Um `seconds` que não é número finito (ou é booleano) vale como ausente: fica a espera padrão."""

    def __init__(self, seconds: float) -> None:
        super().__init__(seconds)
        self.seconds = seconds


def _minimum_wait(exc: RetryTaskLater) -> float | None:
    """A espera mínima pedida por `RetryTaskLater`, ou `None` se o valor não serve."""
    seconds = exc.seconds
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)):
        return None
    return float(seconds)


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    task_type: str
    payload: dict
    attempts: int


@dataclass(frozen=True)
class TaskRecord:
    id: int
    task_type: str
    state: str
    attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    last_error: str | None
    finished_at: datetime | None


def _contains_unstorable(value: Any) -> bool:
    """Algum texto do valor (chaves inclusive) tem NUL ou substituto UTF-16 isolado."""
    if isinstance(value, str):
        return has_unstorable(value)
    if isinstance(value, Mapping):
        return any(_contains_unstorable(k) or _contains_unstorable(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_unstorable(v) for v in value)
    return False


def _validate_enqueue(task_type: str, payload: Any, idempotency_key: str | None) -> None:
    """Confere a entrada de `enqueue` sem tocar no banco (`ValueError`)."""
    if not isinstance(task_type, str) or TASK_TYPE_PATTERN.fullmatch(task_type) is None:
        raise ValueError("task_type inválido: use ^[a-z][a-z0-9_.]{0,99}$")
    if not isinstance(payload, Mapping):
        raise ValueError("payload deve ser um objeto JSON")
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("payload deve ser serializável como JSON") from None
    # O `json.dumps` acima aceita o substituto isolado (sai como o escape `\ud800`), mas o `jsonb`
    # o recusa no banco, e o psycopg não codifica o da chave de idempotência: os dois viram
    # `ValueError` aqui, antes de qualquer SQL, como o NUL.
    if _contains_unstorable(payload):
        raise ValueError("payload não pode conter o caractere NUL nem substituto UTF-16 isolado")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str):
            raise ValueError("idempotency_key deve ser texto")
        if has_unstorable(idempotency_key):
            raise ValueError("idempotency_key não pode conter o caractere NUL nem substituto UTF-16 "
                             "isolado")


def _existing_task(conn: psycopg.Connection, task_type: str, payload: Mapping[str, Any],
                   idempotency_key: str) -> int | None:
    row = conn.execute(
        "SELECT id, task_type = %s AND payload = %s FROM tasks WHERE idempotency_key = %s",
        (task_type, Jsonb(dict(payload)), idempotency_key),
    ).fetchone()
    if row is None:
        return None
    task_id, same = row
    if not same:
        raise ValueError("idempotency_key já usada com outro conteúdo")
    return int(task_id)


def _insert_task(conn: psycopg.Connection, task_type: str, payload: Mapping[str, Any],
                 idempotency_key: str | None, now: datetime) -> int:
    """Grava a tarefa na transação de `conn` (a entrada já foi conferida por `_validate_enqueue`)."""
    if idempotency_key is not None:
        existing = _existing_task(conn, task_type, payload, idempotency_key)
        if existing is not None:
            return existing
    row = conn.execute(
        "INSERT INTO tasks (task_type, payload, idempotency_key, state, attempts, available_at, "
        "created_at) VALUES (%s, %s, %s, 'pending', 0, %s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        (task_type, Jsonb(dict(payload)), idempotency_key, now, now),
    ).fetchone()
    if row is not None:
        return int(row[0])
    # Corrida: outra transação gravou a mesma chave entre a consulta e o INSERT.
    assert idempotency_key is not None
    existing = _existing_task(conn, task_type, payload, idempotency_key)
    if existing is None:  # pragma: no cover - só com isolamento acima de READ COMMITTED
        raise ValueError("idempotency_key em uso por uma transação concorrente")
    return existing


def enqueue_in_transaction(conn: psycopg.Connection, task_type: str, payload: Mapping[str, Any], *,
                           now: datetime, idempotency_key: str | None = None) -> int:
    """Enfileira uma tarefa na transação de `conn`, com o horário `now` do chamador (para as
    operações do núcleo que agendam tarefas junto com a mudança, como `bot.sync_profile`).

    As mesmas regras de `TaskQueue.enqueue` (validação antes de qualquer SQL; chave repetida com o
    mesmo conteúdo devolve o id existente; com outro conteúdo, `ValueError`)."""
    _validate_enqueue(task_type, payload, idempotency_key)
    return _insert_task(conn, task_type, payload, idempotency_key, now)


def has_live_task(conn: psycopg.Connection, task_type: str, subject: Mapping[str, Any]) -> bool:
    """Há tarefa viva (`pending` ou `leased`) do tipo para o assunto (`payload @> subject`).

    Uma tarefa em posse vencida (mesmo esgotada) ainda é viva: só a fila a leva a `failed`."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM tasks WHERE task_type = %s AND state IN ('pending', 'leased') "
        "AND payload @> %s)",
        (task_type, Jsonb(dict(subject))),
    ).fetchone()
    return bool(row[0])


def finished_rounds(conn: psycopg.Connection, task_type: str, subject: Mapping[str, Any],
                    ) -> tuple[int, datetime | None]:
    """Quantas tarefas do tipo para o assunto já terminaram (`done` ou `failed`) e o `finished_at`
    da última (a chave da rodada seguinte é `<tipo>:<id>:r<k>`, com `k` = 1 + terminadas).

    Contam todas as terminadas com o payload do assunto: a original (sem sufixo), as rodadas `r<k>` e
    as continuações `c<task_id>`. O horário é o maior `finished_at`; `None` se
    nenhuma terminou."""
    row = conn.execute(
        "SELECT count(*), max(finished_at) FROM tasks WHERE task_type = %s AND state IN ('done', 'failed') "
        "AND payload @> %s",
        (task_type, Jsonb(dict(subject))),
    ).fetchone()
    return int(row[0]), utc(row[1])


class TaskQueue:
    """Fila de tarefas sobre a tabela `tasks` (ver o docstring do módulo)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings) -> None:
        self._conninfo = conninfo
        self._clock = clock
        self._settings = settings
        self._db = LazyConnection(conninfo)
        self._secrets = (settings.admin_token,)

    # --- enfileirar -----------------------------------------------------------------------------

    def enqueue(self, task_type: str, payload: dict, *, idempotency_key: str | None = None,
                conn: psycopg.Connection | None = None) -> int:
        """Enfileira uma tarefa e devolve o id (o da existente, se a chave repetir com o mesmo
        conteúdo). `ValueError` para `task_type` inválido, NUL ou substituto UTF-16 isolado no
        `payload` (chaves inclusive) ou na chave, ou chave repetida com outro conteúdo."""
        _validate_enqueue(task_type, payload, idempotency_key)
        if conn is not None:
            return self._enqueue(conn, task_type, payload, idempotency_key)
        own = self._db.get()
        with own.transaction():
            return self._enqueue(own, task_type, payload, idempotency_key)

    def _enqueue(self, conn: psycopg.Connection, task_type: str, payload: Mapping[str, Any],
                 idempotency_key: str | None) -> int:
        return _insert_task(conn, task_type, payload, idempotency_key, self._clock.now())

    # --- pegar ----------------------------------------------------------------------------------

    def claim(self, worker_id: str, *, task_types: Collection[str] | None = None) -> ClaimedTask | None:
        """Pega a próxima tarefa disponível para `worker_id`, ou `None`.

        Com `task_types`, só tarefas desses tipos (a recepção durante a correção, `worker.py`)."""
        conn = self._db.get()
        lease = timedelta(seconds=self._settings.lease_seconds)
        only = "" if task_types is None else "AND task_type = ANY(%(types)s) "
        while True:
            now = self._clock.now()
            with conn.transaction():
                row = conn.execute(
                    "SELECT id, task_type, state, attempts FROM tasks "
                    "WHERE ((state = 'pending' AND available_at <= %(now)s) "
                    "   OR (state = 'leased' AND lease_until < %(now)s)) " + only +
                    "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    {"now": now, "types": list(task_types or ())},
                ).fetchone()
                if row is None:
                    return None
                task_id, task_type, state, attempts = row
                if state == "leased" and attempts >= self._settings.max_attempts:
                    # Posse esgotada: falha sem incrementar attempts; a busca segue.
                    self._mark_failed(conn, task_id, task_type, attempts, LEASE_EXHAUSTED_ERROR, now)
                    continue
                claimed = conn.execute(
                    "UPDATE tasks SET state = 'leased', lease_owner = %s, lease_until = %s, "
                    "attempts = attempts + 1 WHERE id = %s RETURNING id, task_type, payload, attempts",
                    (worker_id, now + lease, task_id),
                ).fetchone()
                assert claimed is not None
                return ClaimedTask(id=int(claimed[0]), task_type=claimed[1], payload=claimed[2],
                                   attempts=int(claimed[3]))

    # --- concluir e falhar ----------------------------------------------------------------------

    def complete(self, task_id: int, worker_id: str) -> bool:
        """Conclui a tarefa, se `worker_id` for o dono com posse vigente; senão `False`."""
        now = self._clock.now()
        row = self._db.get().execute(
            "UPDATE tasks SET state = 'done', lease_owner = NULL, lease_until = NULL, finished_at = %s "
            "WHERE id = %s AND state = 'leased' AND lease_owner = %s AND lease_until >= %s RETURNING id",
            (now, task_id, worker_id, now),
        ).fetchone()
        return row is not None

    def fail(self, task_id: int, worker_id: str, error: str, *, permanent: bool = False) -> bool:
        """Registra a falha da tarefa, se `worker_id` for o dono com posse vigente; senão `False`.

        Temporária com `attempts < max_attempts`: volta a `pending` depois da espera. Definitiva, ou
        com as tentativas esgotadas: `failed`, com o evento `task.failed`.
        """
        return self._fail(task_id, worker_id, error, permanent=permanent) is not None

    def _fail(self, task_id: int, worker_id: str, error: str, *, permanent: bool,
              min_wait: float | None = None) -> str | None:
        """Como `fail`, mas devolve o novo estado (`pending`/`failed`) ou `None`.

        `min_wait` (de `RetryTaskLater`): a espera da falha temporária passa a ser
        `min(max(espera padrão, min_wait), MAX_WAIT_SECONDS)`.
        """
        text = storable_text(str(error))
        conn = self._db.get()
        now = self._clock.now()
        with conn.transaction():
            row = conn.execute(
                "SELECT task_type, attempts FROM tasks WHERE id = %s AND state = 'leased' "
                "AND lease_owner = %s AND lease_until >= %s FOR UPDATE",
                (task_id, worker_id, now),
            ).fetchone()
            if row is None:
                return None
            task_type, attempts = row
            if permanent or attempts >= self._settings.max_attempts:
                self._mark_failed(conn, task_id, task_type, attempts, text, now)
                return "failed"
            seconds = backoff_seconds(self._settings, attempts)
            if min_wait is not None and min_wait > seconds:
                seconds = min(min_wait, MAX_WAIT_SECONDS)
            wait = timedelta(seconds=seconds)
            # Mesmo sem evento, o `last_error` é redigido na gravação com as identidades lidas
            # depois do lock dos eventos (ver `events.load_write_identities`).
            identities = load_write_identities(conn)
            conn.execute(
                "UPDATE tasks SET state = 'pending', available_at = %s, lease_owner = NULL, "
                "lease_until = NULL, last_error = %s WHERE id = %s",
                (now + wait, redact_free_text(conn, text, secrets=self._secrets, identities=identities),
                 task_id),
            )
            return "pending"

    def _mark_failed(self, conn: psycopg.Connection, task_id: int, task_type: str, attempts: int,
                     error: str, now: datetime) -> None:
        """`failed` + `task.failed`, na transação de `conn`, com o erro redigido nos dois.

        As identidades são lidas depois do lock dos eventos, e não só no INSERT de `task.failed`:
        um cadastro concorrente confirmado antes do evento entra na redação (ver
        `events.load_write_identities`). A posse da linha já foi tomada; o lock vem depois dela.
        """
        identities = load_write_identities(conn)
        redacted = redact_free_text(conn, error, secrets=self._secrets, identities=identities)
        conn.execute(
            "UPDATE tasks SET state = 'failed', lease_owner = NULL, lease_until = NULL, "
            "finished_at = %s, last_error = %s WHERE id = %s",
            (now, redacted, task_id),
        )
        append_event(
            conn, EventType.TASK_FAILED,
            {"task_id": int(task_id), "task_type": task_type, "attempts": int(attempts), "error": redacted},
            occurred_at=now, actor_token_id=None, secrets=self._secrets, identities=identities,
        )

    # --- processar ------------------------------------------------------------------------------

    def work_once(self, worker_id: str, handlers: Mapping[str, Callable[[ClaimedTask], None]], *,
                  task_types: Collection[str] | None = None) -> int | None:
        """Pega uma tarefa e chama o tratador do tipo; devolve o id, ou `None` se a fila estiver vazia.

        `PermanentTaskError` é falha definitiva; `RetryTaskLater(n)`, temporária com espera mínima de
        `n` s; outra `Exception`, temporária; tipo sem tratador, definitiva. Do erro, só o nome do
        tipo é guardado. Uma `BaseException` que não é `Exception` (queda) escapa e deixa a tarefa
        em posse. Registra `task.processed` com o estado final.
        """
        task = self.claim(worker_id) if task_types is None else self.claim(worker_id, task_types=task_types)
        if task is None:
            return None
        handler = handlers.get(task.task_type)
        state: str | None
        if handler is None:
            state = self._fail(task.id, worker_id, NO_HANDLER_ERROR, permanent=True)
        else:
            try:
                handler(task)
            except PermanentTaskError as exc:
                state = self._fail(task.id, worker_id, type(exc).__name__, permanent=True)
            except RetryTaskLater as exc:
                state = self._fail(task.id, worker_id, type(exc).__name__, permanent=False,
                                   min_wait=_minimum_wait(exc))
            except Exception as exc:  # noqa: BLE001 - falha temporária; só o nome do tipo é guardado
                state = self._fail(task.id, worker_id, type(exc).__name__, permanent=False)
            else:
                state = "done" if self.complete(task.id, worker_id) else None
        if state is None:
            # A posse venceu durante o tratamento: o resultado não vale e a tarefa será retomada.
            state = self.get(task.id).state
        logger.info("task.processed", extra={"event": "task.processed", "id": task.id, "state": state})
        return task.id

    # --- consultar e fechar ---------------------------------------------------------------------

    def get(self, task_id: int) -> TaskRecord:
        """O registro da tarefa; `LookupError` se não existir."""
        row = self._db.get().execute(
            "SELECT id, task_type, state, attempts, available_at, lease_owner, lease_until, "
            "last_error, finished_at FROM tasks WHERE id = %s",
            (task_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"tarefa inexistente: {task_id}")
        return TaskRecord(id=int(row[0]), task_type=row[1], state=row[2], attempts=int(row[3]),
                          available_at=utc(row[4]), lease_owner=row[5], lease_until=utc(row[6]),
                          last_error=row[7], finished_at=utc(row[8]))

    def close(self) -> None:
        """Fecha a conexão da fila (idempotente)."""
        self._db.close()
