"""Varredura periódica: agendador e reconciliação.



O que se confere (`cq_server.scheduler`, a varredura):
- **cada regra antes e no horário** (`dispatch.validate`, `dispatch.expire`, `dispatch.open`,
  `dispatch.close`, `delivery.prepare`, `submission.grade`, `delivery.reveal`, `telegram.update`): só os
  assuntos vencidos recebem tarefa, com a chave `<tipo>:<id>:r<k>`, o payload do assunto e
  `available_at = now` (nenhuma tarefa com `available_at` futuro); o que não venceu, não;
- **sem duplicar**: duas varreduras simultâneas, cada uma na sua conexão, criam uma só tarefa por
  assunto;
- **posse esgotada** (tarefa montada por SQL com `attempts = max_attempts`, `state = 'leased'` e
  `lease_until` no passado; a fila de tarefas a leva a `failed`): a rodada `r2` só sai depois do
  `retry_gap` (`backoff_max_seconds`);
- **relógio que volta**: o "vencido" e o intervalo usam o `now` passado, nunca o relógio do banco;
- **tipo sem tratador** não é enfileirado (`Sweeper(handled_types=…)`; sem bots, nenhum
  `telegram.update`);
- **expiração** do agendado (`confirm_deadline = opens_at`) e do imediato (24 h depois da validação) →
  `cancelled/expired` com `dispatch.cancelled` exato; **corrida confirmação × expiração** com duas
  conexões → um só desfecho;
- **update venenoso** → 3 rodadas `failed` e depois `processed/abandoned`, consultável por
  (`bot_id`, `update_id`), sem evento;
- a **correção reanimada**: a tarefa `submission.grade` `failed` pela posse → a varredura cria
  a rodada `r2` depois do `retry_gap` e a correção sai (worker real);
- **continuação da validação** com a posse esgotada → rodada `r<k>` que retoma pelo estado e chega a
  `scheduled`;
- **`limit = 2`** com 5 assuntos vencidos e os 2 primeiros no intervalo → o 3º e o 4º recebem a rodada
  (os filtros ficam antes do `LIMIT`);
- **leitor atrasado além de 34 s** → as rodadas do fechamento terminam `done` sem efeito, a seguinte sai
  depois de `CLOSE_RECHECK_SECONDS`, e o fechamento acontece sem nenhum `task.failed`;
- **rodada vazia** sem escrita nem log; `scheduler.swept` só com contagens;
- **entrada depois de `closes_at`** com o fechamento atrasado além de 3 rodadas (barreira presa por um
  update fantasma) → o update continua `pending`, as rodadas `done` não contam, nada é abandonado, e
  depois da transição ele vira `answered_late` (worker real);
- a regra `delivery.reveal` não enfileira entregas `not_delivered` nem `question_failed`;
- **teto da validação**: a continuação que falha de vez em toda rodada (`validate_task_handler`
  com `channel=None` → `PermanentTaskError` "canal sem rota") → exatamente `VALIDATE_MAX_ROUNDS = 3`
  rodadas `failed`, depois `rejected/internal_failure` com `dispatch.rejected` exato e sem 4ª rodada; no
  agendado com o `FakeClock` além de `opens_at` → `cancelled/validation_overran`;
- as constantes da varredura (reais no esqueleto) ficam na mesma função de um caso de `sweep_once`
  : `test_empty_round_writes_nothing_and_swept_logs_only_counts`.

**Montagem.** Duas formas:
1. **Banco montado por SQL** (a maior parte): o oferecimento, o bot, a referência, os disparos, as
   entregas, as submissões, os updates e as tarefas são inseridos nas colunas das tabelas do disparo e
   dos oferecimentos e do vínculo do aluno, coerentes com os CHECKs (os mesmos estados de `test_migration_0004.py`), com os horários
   pelo `FakeClock`. A varredura roda por `scheduler.sweep_once(conn, now=…, retry_gap=…,
   rules=DISPATCH_RULES)` numa conexão própria; os tratadores do disparo (validação, expiração, fechamento) são os
   reais, montados como o `run_worker` os monta (o `HttpQuestionEngine` sobre o `engine_fake`, a
   `repo_fetcher` e o `TelegramChannel` com rota sobre a fixture `bots`), e as tarefas são tomadas por
   SQL uma a uma, só do tipo pedido, com a mesma forma da posse do `TaskQueue.claim` (como a semente de
   `tests/helpers/calls.py`), e concluídas ou falhadas pela fila de tarefas (`complete`/`fail`). A
   posse esgotada passa pelo `TaskQueue.claim` real (o banco montado não tem outras tarefas).
2. **Worker real** (a correção reanimada e a entrada depois do horário): oferecimento e bot pela API do oferecimento, o aluno pela importação da
   lista e o vínculo por SQL (matrícula `active` com o termo e endereço `active`), o disparo pela API e o
   resto pelo `run_worker` com bots, o motor falso, a obtenção falsa e a Bot API falsa estendida
   (`rich_bot_api`); a varredura é a do laço do worker (`Sweeper`).

**Leituras [I] deste arquivo:**
- `k` = 1 + tarefas **terminadas** (`done` ou `failed`) do tipo com o payload do assunto, contando as
  continuações `c<task_id>` da validação e a tarefa original (sem sufixo) do update e da correção; os
  updates e os disparos montados por SQL não têm tarefa original, e a primeira rodada é `r1`;
- o teto das regras conta só as terminadas `failed`; no `telegram.update`, esgotado o teto, a
  rodada seguinte da varredura (depois do `retry_gap`) grava o abandono com `processed_at = now` e conta
  `abandoned = 1` no `SweepResult`; na validação, o esgotamento não enfileira nada (`enqueued` sem o
  tipo) e o teste não afirma o `abandoned` (o abandono só está definido para o update);
- a regra `submission.grade` vale para toda submissão `grading`, inclusive a de uma entrega `no_answer` de
  disparo fechado (a correção pendente no fechamento), e a `delivery.reveal` a exclui;
- o registro `scheduler.swept` tem só `enqueued` (por tipo) e `abandoned`, além das chaves do
  `JsonFormatter` (`level`, `logger`, `event`, `time`);
- a expiração sai na tarefa `dispatch.expire` com o `now` do tratador (`cancelled_at` e `occurred_at`).

Ids internos, `seq`, `update_id`, pseudônimos e seeds nunca são literais esperados: vêm do banco. No
esqueleto, os casos com banco montado falham na primeira tabela do disparo (`question_refs`, ausente: a
migração `0004` não existe) e os do worker, no preparo do disparo (rota do disparo ausente, 404); as
chamadas diretas a `sweep_once`, `Sweeper.maybe_run` e aos tratadores lançariam `NotImplementedError`
logo depois.
"""

from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import io
import itertools
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cq_server._leasing import LEASE_EXHAUSTED_ERROR
from cq_server.scheduler import (
    CLOSE_RECHECK_SECONDS,
    IMMEDIATE_CONFIRM_HOURS,
    DISPATCH_RULES,
    SWEEP_BATCH,
    SWEEP_MAX_UPDATE_ROUNDS,
    SWEEP_SECONDS,
    VALIDATE_MAX_ROUNDS,
    Sweeper,
    SweepResult,
    sweep_once,
)
from cq_server.tasks import ClaimedTask, PermanentTaskError, RetryTaskLater, TaskQueue
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    confirm_dispatch,
    create_offering,
    import_students,
    prepare_dispatch,
    register_bot,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import FakeClock, iso
from tests.helpers.contract import assert_error
from tests.helpers.logs import capture_logs

# --- constantes --------------------------------------------------------------------------------------

REPO = "https://repos.cq.test/turma/questoes-varredura.git"
COMMIT = "5ca1ab1e" * 5
QID = "number"  # o preset do motor falso: um campo `number`, resposta certa 6,5
ANSWER = "6.5"
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
WAIT = 20.0  # segundos reais de espera por uma condição (threads do worker)
OWNER = "cq-teste-varredura"  # dono das posses tomadas por SQL
LOST_OWNER = "cq-worker-perdido"  # dono fictício das posses esgotadas
ZONE = "America/Sao_Paulo"
RECHECK = CLOSE_RECHECK_SECONDS + 1  # avanço que vence o `done_gap` das rodadas `done`

# Os tipos de tarefa do disparo e a chave do assunto de cada regra, escritos
# aqui, e não importados, para o teste não depender da implementação.
VALIDATE, EXPIRE, OPEN, CLOSE = "dispatch.validate", "dispatch.expire", "dispatch.open", "dispatch.close"
PREPARE, GRADE, REVEAL, UPDATE = "delivery.prepare", "submission.grade", "delivery.reveal", "telegram.update"
DISPATCH_TASK_TYPES = (VALIDATE, EXPIRE, OPEN, CLOSE, PREPARE, REVEAL, GRADE, "question.refetch", UPDATE)
SUBJECT_KEY = {VALIDATE: "dispatch_row_id", EXPIRE: "dispatch_row_id", OPEN: "dispatch_row_id",
               CLOSE: "dispatch_row_id", PREPARE: "delivery_row_id", REVEAL: "delivery_row_id",
               GRADE: "submission_row_id", UPDATE: "update_row_id"}

# A prévia gravada nos disparos validados montados por SQL (um objeto).
_PREVIEW = {"title": "Questão neutra", "statement": "Enunciado neutro.",
            "fields": [{"type": "number", "label": None, "suffix": None, "hint": "um número"}],
            "target_count": 0, "warnings": []}
_REASONS = [{"code": "question_code_error", "message": "falha neutra", "phase": "generate"}]

_counter = itertools.count(1)
_update_ids = itertools.count(840_000_001)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any) -> None:
    """Os valores deste arquivo dependem do `settings` do conftest (posse de 30 s, 3 tentativas,
    espera base de 2 s e `retry_gap` = `backoff_max_seconds` = 60 s)."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (30, 3, 2, 60)


# --- banco montado por SQL ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Row:
    """Uma linha montada: o `id` interno e o `uuid` público (quando a tabela tem um)."""

    id: int
    uuid: str | None = None


@dataclasses.dataclass
class H:
    """O banco montado: um oferecimento `active` com bot `registered`, uma pessoa com endereço
    `active` no bot e uma referência de questão (a do motor falso), com o relógio do teste."""

    db_url: str
    sql: psycopg.Connection
    clock: FakeClock
    settings: Any
    t0: datetime
    offering: Row
    bot: int
    person: int
    endpoint: int
    ref: Row


def _insert(conn: psycopg.Connection, table: str, key: str = "id", **values: Any) -> Any:
    """`INSERT` com as colunas dadas; devolve a chave gerada."""
    columns = list(values)
    query = pgsql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        pgsql.Identifier(table),
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
        pgsql.SQL(", ").join(pgsql.Placeholder() for _ in columns),
        pgsql.Identifier(key))
    return conn.execute(query, [values[c] for c in columns]).fetchone()[0]


def _offering_bot(sql: psycopg.Connection, t0: datetime) -> tuple[Row, int]:
    """Oferecimento `active` e o bot `registered` dele (colunas dos bots; sem posse do leitor)."""
    n = next(_counter)
    offering_uuid = uuid.uuid4()
    offering = _insert(sql, "offerings", offering_id=offering_uuid, code=f"varredura-{n:04d}",
                       course="Disciplina Neutra", term="2031s1", timezone=ZONE, response_window_minutes=60,
                       state="active", created_at=t0, activated_at=t0)
    bot = _insert(sql, "bots", bot_uuid=uuid.uuid4(), offering_id=offering,
                  telegram_bot_id=6_500_000_000 + n, username=f"cq_varredura_{n:04d}_bot",
                  token_ciphertext=bytes([1]) + secrets.token_bytes(79), token_version=1, state="registered",
                  access_problem=None, profile_state="configured", registered_at=t0)
    return Row(offering, str(offering_uuid)), bot


def _person(sql: psycopg.Connection, t0: datetime) -> int:
    n = next(_counter)
    return _insert(sql, "people", pseudo_id=uuid.uuid4(), alias=f"var{n:09x}", created_at=t0)


def _harness(db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> H:
    t0 = clock.now()
    offering, bot = _offering_bot(sql, t0)
    person = _person(sql, t0)
    endpoint = _insert(sql, "delivery_endpoints", person_id=person, bot_id=bot, channel="telegram",
                       chat_id=5_300_000_000 + next(_counter), state="active", created_at=t0, changed_at=t0)
    ref_uuid = uuid.uuid4()
    ref = _insert(sql, "question_refs", ref_uuid=ref_uuid, repo=REPO, repo_sha256=_repo_key(REPO),
                  commit_sha=COMMIT, qid=QID, created_at=t0)
    return H(db_url=db_url, sql=sql, clock=clock, settings=settings, t0=t0, offering=offering, bot=bot,
             person=person, endpoint=endpoint, ref=Row(ref, str(ref_uuid)))


def _repo_key(repo: str) -> str:
    from cq_server.questionrefs import repo_key

    return repo_key(repo)


def _dispatch(h: H, state: str, *, immediate: bool = False, confirmed: bool | None = None,
              opens_at: datetime | None = None, closes_at: datetime | None = None,
              validated_at: datetime | None = None, confirmed_at: datetime | None = None,
              offering: Row | None = None) -> Row:
    """Um disparo coerente no estado `state` (os estados de
    `test_migration_0004.py`): `validating`, `rejected`, `cancelled` (pelo operador, durante a
    validação), `scheduled` (confirmado ou não), `open` e `closed`.

    O agendado tem os dois horários desde o preparo (padrão: abre em 1 h e fecha 1 h depois); o
    imediato não confirmado tem só a janela capturada (60 min); o validado tem `validated_at` (padrão:
    o início do teste, ou 1 min antes da abertura do agendado que abre antes dele), a prévia e o `confirm_deadline` (`opens_at` no agendado, `validated_at + 24 h`
    no imediato); o confirmado imediato abre na confirmação (`opens_at = confirmed_at`) e fecha 1 h
    depois, salvo `closes_at`. O preparo (`created_at`) fica 1 dia antes do início do teste."""
    t0 = h.t0
    if confirmed is None:
        confirmed = state in ("open", "closed")
    dispatch_uuid = uuid.uuid4()
    values: dict[str, Any] = {
        "dispatch_uuid": dispatch_uuid, "offering_id": (offering or h.offering).id, "question_ref_id": h.ref.id,
        "origin": "integration", "immediate": immediate, "state": state, "opens_at": None, "closes_at": None,
        "window_minutes": None, "preview": None, "rejection": None, "rejection_reasons": None,
        "cancel_reason": None, "created_at": t0 - timedelta(days=1), "validated_at": None,
        "confirm_deadline": None,
        "confirmed_at": None, "opened_at": None, "closed_at": None, "rejected_at": None, "cancelled_at": None,
    }
    if immediate:
        values["window_minutes"] = 60
    else:
        values["opens_at"] = opens_at or t0 + timedelta(hours=1)
        values["closes_at"] = closes_at or values["opens_at"] + timedelta(hours=1)
    if state in ("scheduled", "open", "closed"):
        validated = validated_at or (t0 if immediate else min(t0, values["opens_at"] - timedelta(minutes=1)))
        values |= {"validated_at": validated, "preview": Jsonb(_PREVIEW),
                   "confirm_deadline": validated + timedelta(hours=24) if immediate else values["opens_at"]}
        if confirmed:
            values["confirmed_at"] = confirmed_at or validated
            if immediate:
                values["opens_at"] = opens_at or values["confirmed_at"]
                values["closes_at"] = closes_at or values["opens_at"] + timedelta(hours=1)
        if state in ("open", "closed"):
            values["opened_at"] = values["opens_at"]
        if state == "closed":
            values["closed_at"] = values["closes_at"]
    elif state == "rejected":
        values |= {"rejected_at": t0, "rejection": "question_broken", "rejection_reasons": Jsonb(_REASONS)}
    elif state == "cancelled":
        values |= {"cancelled_at": t0, "cancel_reason": "operator"}
    else:
        assert state == "validating", state
    return Row(_insert(h.sql, "dispatches", **values), str(dispatch_uuid))


def _principal(h: H) -> int:
    """Uma mensagem da fila para o `principal_message_id` (FK; forma (a) da fila de saída, como em
    `test_migration_0004.py`): nenhum teste deste arquivo roda a fila de saída sobre o banco montado."""
    return _insert(h.sql, "outbox_messages", recipient_person_id=h.person, text="mensagem neutra da fila",
                   state="pending", attempts=0, available_at=h.t0, created_at=h.t0)


def _delivery(h: H, dispatch: Row, state: str, *, variant: bool | None = None,
              revealed_at: datetime | None = None) -> Row:
    """Uma entrega coerente no estado `state`, com uma matrícula nova de aluno
    `active` no oferecimento (a unicidade é por disparo e matrícula) e o endereço do banco montado.
    `variant` só muda a `pending` (com a variante gravada: `variant_at` e `current_field`)."""
    t0 = h.t0
    sent = state in ("delivered", "answered", "dont_know", "no_answer", "answered_late")
    with_variant = sent or (state == "pending" and bool(variant))
    enrollment = _insert(h.sql, "enrollments", person_id=_person(h.sql, t0), offering_id=h.offering.id,
                         role="student", state="active", created_at=t0, activated_at=t0, terms_version=1,
                         terms_accepted_at=t0)
    delivery_uuid = uuid.uuid4()
    values: dict[str, Any] = {
        "delivery_uuid": delivery_uuid, "dispatch_id": dispatch.id, "enrollment_id": enrollment,
        "delivery_endpoint_id": h.endpoint, "seed": secrets.randbelow(2**32),
        "button_token": base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii"),
        "state": state, "undelivered_reason": "endpoint_blocked" if state == "not_delivered" else None,
        "failure": "variant_broken" if state == "question_failed" else None,
        "current_field": 0 if with_variant and state in ("pending", "delivered", "no_answer") else None,
        "principal_message_id": _principal(h) if sent else None, "created_at": t0,
        "variant_at": t0 if with_variant else None, "sent_at": t0 if sent else None,
        "answered_at": t0 if state in ("answered", "dont_know", "answered_late") else None,
        "revealed_at": revealed_at,
    }
    return Row(_insert(h.sql, "deliveries", **values), str(delivery_uuid))


def _update(h: H, *, bot: int | None = None, state: str = "pending",
            received_at: datetime | None = None) -> Row:
    """Um update de texto de um remetente neutro (colunas dos bots); `processed` com o desfecho
    `not_linked`. Devolve o `id` e, em `uuid`, o `update_id` do Telegram (texto)."""
    at = received_at or h.clock.now()
    update_id = next(_update_ids)
    chat = 5_300_900_000 + update_id % 100_000
    values: dict[str, Any] = {
        "bot_id": bot or h.bot, "update_epoch": 0, "update_id": update_id, "kind": "message",
        "chat_id": chat, "chat_type": "private", "sender_id": chat, "text": "texto neutro", "command": None,
        "details": Jsonb({"message_id": update_id % 1000 + 1, "date": int(at.timestamp())}),
        "received_at": at, "state": state,
    }
    if state == "processed":
        values |= {"outcome": "not_linked", "processed_at": at}
    return Row(_insert(h.sql, "telegram_updates", **values), str(update_id))


def _submission(h: H, delivery: Row, outcome: str) -> Row:
    """Uma submissão com um update próprio já processado."""
    update = _update(h, state="processed")
    submission_uuid = uuid.uuid4()
    values: dict[str, Any] = {
        "submission_uuid": submission_uuid, "delivery_id": delivery.id, "telegram_update_id": update.id,
        "outcome": outcome, "valid": True if outcome == "graded" else None,
        "refusal": "already_answered" if outcome == "refused" else None, "late": False,
        "raw_input": Jsonb({"resposta": ANSWER}), "received_at": h.t0, "created_at": h.t0,
        "graded_at": h.t0 if outcome == "graded" else None,
    }
    return Row(_insert(h.sql, "submissions", **values), str(submission_uuid))


def _task(h: H, task_type: str, subject: dict[str, int], key: str, state: str, *,
          attempts: int = 1, finished_at: datetime | None = None, lease_owner: str | None = None,
          lease_until: datetime | None = None, last_error: str | None = None) -> int:
    """Uma tarefa montada por SQL (colunas da fila de tarefas)."""
    now = h.clock.now()
    return _insert(h.sql, "tasks", task_type=task_type, payload=Jsonb(subject), idempotency_key=key,
                   state=state, attempts=attempts, available_at=now, created_at=now, lease_owner=lease_owner,
                   lease_until=lease_until, last_error=last_error, finished_at=finished_at)


# --- varredura, tarefas e tratadores sobre o banco montado --------------------------------------------


def _sweep(h: H, *, now: datetime | None = None, limit: int | None = None,
           rules: Sequence[Any] | None = None) -> SweepResult:
    """Uma rodada de `sweep_once` numa conexão própria, com o `retry_gap` do worker
    (`backoff_max_seconds`) e as regras do disparo."""
    kwargs: dict[str, Any] = {} if limit is None else {"limit": limit}
    with psycopg.connect(h.db_url, autocommit=True) as conn:
        return sweep_once(conn, now=now or h.clock.now(), retry_gap=float(h.settings.backoff_max_seconds),
                          rules=DISPATCH_RULES if rules is None else rules, **kwargs)


def _result(result: SweepResult) -> tuple[dict[str, int], int]:
    return dict(result.enqueued), result.abandoned


def _dispatch_tasks(h: H) -> list[tuple[str, str, str, dict, int, datetime]]:
    """(tipo, chave, estado, payload, tentativas, `available_at`) das tarefas do disparo, em ordem de `id`."""
    return [tuple(row) for row in h.sql.execute(
        "SELECT task_type, idempotency_key, state, payload, attempts, available_at FROM tasks "
        "WHERE task_type = ANY(%s) ORDER BY id", (list(DISPATCH_TASK_TYPES),)).fetchall()]


def _rounds(h: H, task_type: str, subject: dict[str, int]) -> list[tuple[str, str]]:
    """(chave, estado) das tarefas do tipo para o assunto, em ordem de `id`."""
    return [(row[0], row[1]) for row in h.sql.execute(
        "SELECT idempotency_key, state FROM tasks WHERE task_type = %s AND payload @> %s ORDER BY id",
        (task_type, Jsonb(subject))).fetchall()]


def _task_id(h: H, key: str) -> int:
    return h.sql.execute("SELECT id FROM tasks WHERE idempotency_key = %s", (key,)).fetchone()[0]


def _pending(task_type: str, row: int, k: int, now: datetime) -> tuple:
    """A linha esperada de uma rodada recém-enfileirada pela varredura."""
    return (task_type, f"{task_type}:{row}:r{k}", "pending", {SUBJECT_KEY[task_type]: row}, 0, now)


_CLAIM_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s AND (%(id)s::bigint IS NULL OR id = %(id)s::bigint) "
    "            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")


def _claim(h: H, task_type: str, *, clock: FakeClock | None = None, task_id: int | None = None
           ) -> ClaimedTask | None:
    """Toma por SQL a tarefa disponível de menor `id` do tipo (ou a de `task_id`), com a forma da posse
    do `TaskQueue.claim` (posse do `settings`, tentativa contada)."""
    now = (clock or h.clock).now()
    row = h.sql.execute(_CLAIM_SQL, {"owner": OWNER, "until": now + timedelta(seconds=h.settings.lease_seconds),
                                     "type": task_type, "now": now, "id": task_id}).fetchone()
    if row is None:
        return None
    return ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3]))


def _run(h: H, task_type: str, handler: Callable[[ClaimedTask], None], *, clock: FakeClock | None = None,
         limit: int = 12) -> list[tuple[str, str]]:
    """Processa as tarefas disponíveis do tipo, uma a uma, como o `work_once` da fila (a mesma posse; o
    `PermanentTaskError` falha de vez e o `RetryTaskLater` falha temporariamente, pela fila de tarefas); as
    outras exceções sobem. Devolve (chave, estado final) de cada tarefa processada."""
    queue = TaskQueue(h.db_url, clock=clock or h.clock, settings=h.settings)
    done: list[tuple[str, str]] = []
    try:
        while (task := _claim(h, task_type, clock=clock)) is not None:
            try:
                handler(task)
            except PermanentTaskError as exc:
                assert queue.fail(task.id, OWNER, type(exc).__name__, permanent=True)
            except RetryTaskLater as exc:
                assert queue.fail(task.id, OWNER, type(exc).__name__)
            else:
                assert queue.complete(task.id, OWNER), f"a posse da tarefa {task.task_type} venceu"
            record = queue.get(task.id)
            key = h.sql.execute("SELECT idempotency_key FROM tasks WHERE id = %s", (task.id,)).fetchone()[0]
            done.append((key, record.state))
            assert len(done) <= limit, f"mais de {limit} tarefas {task_type} processadas"
    finally:
        queue.close()
    return done


def _engine(engine_fake: Any) -> Any:
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.config import EngineSettings

    return HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))


def _channel(h: H, bots: Any) -> Any:
    """O canal com rota que o `run_worker` usa com bots (o `TelegramChannel` no modo padrão)."""
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.bots import TelegramChannel
    from cq_server.config import DeliverySettings

    return TelegramChannel(h.db_url, clock=h.clock, runtime=bots, default=ConsoleChannel(io.StringIO()),
                           delivery=DeliverySettings())


def _events(h: H, *types: str) -> list[tuple[str, Any, dict, datetime]]:
    """(tipo, `actor_token_id`, `data`, `occurred_at`) dos eventos dos tipos dados, em ordem de `seq`."""
    return [tuple(row) for row in h.sql.execute(
        "SELECT type, actor_token_id, data, occurred_at FROM events WHERE type = ANY(%s) ORDER BY seq",
        (list(types),)).fetchall()]


def _all_event_types(h: H) -> list[str]:
    return [row[0] for row in h.sql.execute("SELECT type FROM events ORDER BY seq").fetchall()]


def _dispatch_row(h: H, dispatch: Row) -> dict[str, Any]:
    with h.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM dispatches WHERE id = %s", (dispatch.id,)).fetchone()


def _snapshot(h: H) -> dict[str, list[tuple]]:
    """Tudo o que uma rodada da varredura poderia escrever, com o `xmin` das linhas dos assuntos (um
    `UPDATE` sem mudança de valor também muda o `xmin`)."""
    return {
        "tasks": [tuple(r) for r in h.sql.execute(
            "SELECT id, xmin::text, task_type, idempotency_key, state FROM tasks ORDER BY id").fetchall()],
        "events": [tuple(r) for r in h.sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall()],
        "dispatches": [tuple(r) for r in h.sql.execute(
            "SELECT id, xmin::text, state FROM dispatches ORDER BY id").fetchall()],
        "deliveries": [tuple(r) for r in h.sql.execute(
            "SELECT id, xmin::text, state FROM deliveries ORDER BY id").fetchall()],
        "submissions": [tuple(r) for r in h.sql.execute(
            "SELECT id, xmin::text, outcome FROM submissions ORDER BY id").fetchall()],
        "telegram_updates": [tuple(r) for r in h.sql.execute(
            "SELECT id, xmin::text, state, outcome FROM telegram_updates ORDER BY id").fetchall()],
    }


# --- constantes e rodada vazia --------------------------------------------------------------------------


def test_empty_round_writes_nothing_and_swept_logs_only_counts(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """As constantes da varredura (reais no esqueleto)
    e as 8 regras do disparo (`DISPATCH_RULES`: tipo, chave do assunto, teto de rodadas `failed` — 3 na validação
    e no update, nenhum nas demais —, o `done_gap` de `CLOSE_RECHECK_SECONDS` no fechamento e no update e
    o `on_exhausted` só nesses dois tetos); uma rodada sem nada vencido, ou com o vencido já com tarefa
    viva, devolve `SweepResult` vazio, não escreve (nem `xmin` muda) e não registra nada; a rodada que
    enfileira registra um único INFO `scheduler.swept` só com as contagens por tipo e o `abandoned`, sem
    id, payload nem chave."""
    assert (SWEEP_SECONDS, SWEEP_BATCH, IMMEDIATE_CONFIRM_HOURS, SWEEP_MAX_UPDATE_ROUNDS, VALIDATE_MAX_ROUNDS,
            CLOSE_RECHECK_SECONDS) == (1.0, 100, 24, 3, 3, 5.0)
    assert (dict(SweepResult().enqueued), SweepResult().abandoned) == ({}, 0)
    rules = {rule.task_type: (rule.subject_key, rule.max_rounds, rule.done_gap, rule.on_exhausted is not None)
             for rule in DISPATCH_RULES}
    assert len(DISPATCH_RULES) == len(rules) == 8
    assert rules == {
        VALIDATE: ("dispatch_row_id", VALIDATE_MAX_ROUNDS, None, True),
        EXPIRE: ("dispatch_row_id", None, None, False),
        OPEN: ("dispatch_row_id", None, None, False),
        CLOSE: ("dispatch_row_id", None, CLOSE_RECHECK_SECONDS, False),
        PREPARE: ("delivery_row_id", None, None, False),
        GRADE: ("submission_row_id", None, None, False),
        REVEAL: ("delivery_row_id", None, None, False),
        UPDATE: ("update_row_id", SWEEP_MAX_UPDATE_ROUNDS, CLOSE_RECHECK_SECONDS, True),
    }

    h = _harness(db_url, sql, clock, settings)
    t0 = h.t0
    # Nada vencido: agendado sem confirmação com prazo futuro, aberto e fechado sem entregas, rejeitado,
    # cancelado, update já processado e update `pending` de um oferecimento sem disparo aberto.
    _dispatch(h, "scheduled", opens_at=t0 + timedelta(hours=2))
    _dispatch(h, "open", opens_at=t0 - timedelta(minutes=30), closes_at=t0 + timedelta(minutes=30))
    _dispatch(h, "closed", opens_at=t0 - timedelta(hours=2), closes_at=t0 - timedelta(hours=1))
    _dispatch(h, "rejected")
    _dispatch(h, "cancelled")
    _update(h, state="processed")
    _, other_bot = _offering_bot(sql, t0)
    _update(h, bot=other_bot)
    # Vencido, mas com tarefa viva: a expiração do agendado com o prazo agora.
    live = _dispatch(h, "scheduled", opens_at=t0)
    _task(h, EXPIRE, {"dispatch_row_id": live.id}, f"{EXPIRE}:{live.id}:r1", "pending", attempts=0)
    # A que vai vencer depois: prazo em 10 s.
    later = _dispatch(h, "scheduled", opens_at=t0 + timedelta(seconds=10))

    before = _snapshot(h)
    with capture_logs() as captured:
        result = _sweep(h)
    assert _result(result) == ({}, 0)
    assert _snapshot(h) == before, "a rodada vazia escreveu no banco"
    assert [r for r in captured.records if str(r.get("logger", "")).startswith("cq_server")] == [], (
        "a rodada vazia registrou algo")

    clock.advance(10)
    with capture_logs() as captured:
        result = _sweep(h)
    assert _result(result) == ({EXPIRE: 1}, 0)
    assert _dispatch_tasks(h)[-1] == _pending(EXPIRE, later.id, 1, clock.now())
    swept = [r for r in captured.records if r.get("event") == "scheduler.swept"]
    assert len(swept) == 1, captured.records
    assert set(swept[0]) == {"level", "logger", "event", "time", "enqueued", "abandoned"}
    assert (swept[0]["level"], swept[0]["enqueued"], swept[0]["abandoned"]) == ("INFO", {EXPIRE: 1}, 0)
    assert later.uuid not in captured.text and f":{later.id}:" not in captured.text
    assert [r for r in captured.records if str(r.get("logger", "")).startswith("cq_server")
            and r.get("event") != "scheduler.swept"] == []


# --- cada regra antes e no horário ------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RuleCase:
    """Os assuntos vencidos e os não vencidos de uma regra; `boundary` é o horário em que os vencidos
    vencem (`None` nas regras de estado, que já valem no início)."""

    due: list[int]
    not_due: list[int]
    boundary: datetime | None


def _case_validate(h: H) -> RuleCase:
    due = [_dispatch(h, "validating", immediate=True).id, _dispatch(h, "validating").id]
    not_due = [_dispatch(h, "rejected").id, _dispatch(h, "cancelled").id, _dispatch(h, "scheduled").id]
    return RuleCase(due, not_due, None)


def _case_expire(h: H) -> RuleCase:
    deadline = h.t0 + timedelta(hours=2)
    due = [_dispatch(h, "scheduled", opens_at=deadline).id,
           _dispatch(h, "scheduled", immediate=True,
                     validated_at=deadline - timedelta(hours=IMMEDIATE_CONFIRM_HOURS)).id]
    not_due = [_dispatch(h, "scheduled", confirmed=True, opens_at=deadline + timedelta(hours=1)).id,
               _dispatch(h, "scheduled", opens_at=deadline + timedelta(seconds=1)).id,
               _dispatch(h, "validating").id]
    return RuleCase(due, not_due, deadline)


def _case_open(h: H) -> RuleCase:
    opens = h.t0 + timedelta(hours=2)
    due = [_dispatch(h, "scheduled", confirmed=True, opens_at=opens).id]
    not_due = [_dispatch(h, "scheduled", confirmed=True, opens_at=opens + timedelta(seconds=1)).id,
               _dispatch(h, "scheduled", opens_at=opens + timedelta(hours=1)).id,
               _dispatch(h, "open", opens_at=h.t0, closes_at=opens + timedelta(hours=1)).id]
    return RuleCase(due, not_due, opens)


def _case_close(h: H) -> RuleCase:
    closes = h.t0 + timedelta(hours=2)
    due = [_dispatch(h, "open", opens_at=h.t0, closes_at=closes).id]
    not_due = [_dispatch(h, "open", opens_at=h.t0, closes_at=closes + timedelta(seconds=1)).id,
               _dispatch(h, "closed", opens_at=h.t0 - timedelta(hours=2), closes_at=h.t0 - timedelta(hours=1)).id]
    return RuleCase(due, not_due, closes)


def _open_dispatch(h: H) -> Row:
    return _dispatch(h, "open", opens_at=h.t0 - timedelta(minutes=5), closes_at=h.t0 + timedelta(hours=1))


def _closed_dispatch(h: H) -> Row:
    return _dispatch(h, "closed", opens_at=h.t0 - timedelta(hours=2), closes_at=h.t0 - timedelta(hours=1))


def _case_prepare(h: H) -> RuleCase:
    dispatch = _open_dispatch(h)
    due = [_delivery(h, dispatch, "pending").id]
    not_due = [_delivery(h, dispatch, "pending", variant=True).id, _delivery(h, dispatch, "delivered").id,
               _delivery(h, dispatch, "not_delivered").id, _delivery(h, dispatch, "question_failed").id]
    return RuleCase(due, not_due, None)


def _case_grade(h: H) -> RuleCase:
    dispatch = _open_dispatch(h)
    due = [_submission(h, _delivery(h, dispatch, "delivered"), "grading").id]
    not_due = [_submission(h, _delivery(h, dispatch, "answered"), "graded").id,
               _submission(h, _delivery(h, dispatch, "delivered"), "refused").id]
    return RuleCase(due, not_due, None)


def _case_reveal(h: H) -> RuleCase:
    closed = _closed_dispatch(h)
    due = [_delivery(h, closed, "answered").id]
    not_due = [_delivery(h, closed, "answered", revealed_at=h.t0).id,
               _delivery(h, _open_dispatch(h), "answered").id]
    return RuleCase(due, not_due, None)


def _case_update(h: H) -> RuleCase:
    _open_dispatch(h)
    due = [_update(h).id]
    not_due = [_update(h, state="processed").id]
    # Outro oferecimento, só com um agendado confirmado (ainda não aberto), e um terceiro sem disparo.
    other, other_bot = _offering_bot(h.sql, h.t0)
    _dispatch(h, "scheduled", confirmed=True, opens_at=h.t0 + timedelta(hours=3), offering=other)
    _, third_bot = _offering_bot(h.sql, h.t0)
    not_due += [_update(h, bot=other_bot).id, _update(h, bot=third_bot).id]
    return RuleCase(due, not_due, None)


RULE_CASES: dict[str, Callable[[H], RuleCase]] = {
    VALIDATE: _case_validate, EXPIRE: _case_expire, OPEN: _case_open, CLOSE: _case_close,
    PREPARE: _case_prepare, GRADE: _case_grade, REVEAL: _case_reveal, UPDATE: _case_update,
}


@pytest.mark.parametrize("task_type", list(RULE_CASES))
def test_rule_enqueues_only_due_subjects_at_their_time(
        task_type: str, db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """Antes do horário (1 s antes), nada; no
    horário exato (`<=`), cada assunto vencido recebe uma tarefa `pending` com a chave
    `<tipo>:<id>:r1`, o payload `{<chave do assunto>: id}`, nenhuma tentativa e `available_at = now`
    (nunca futuro); os assuntos não vencidos (estado errado, horário 1 s depois, oferecimento sem disparo
    aberto ou fechado) não recebem; a rodada seguinte, com as tarefas vivas, não enfileira de novo. As
    regras de estado (validação, preparo da entrega, correção, revelação, update) já valem no início.
    Regras: `dispatch.validate` (`validating`), `dispatch.expire` (agendado sem confirmação com
    `confirm_deadline = opens_at`, imediato com `validated_at + 24 h`), `dispatch.open` (confirmado com
    `opens_at <= now`), `dispatch.close` (`open` com `closes_at <= now`), `delivery.prepare` (`pending`
    sem variante, disparo `open`), `submission.grade` (`grading`), `delivery.reveal` (revelação devida)
    e `telegram.update` (`pending` de bot com disparo `open` ou `closed`)."""
    h = _harness(db_url, sql, clock, settings)
    case = RULE_CASES[task_type](h)
    assert case.due and case.not_due

    if case.boundary is not None:
        clock.set(case.boundary - timedelta(seconds=1))
        assert _result(_sweep(h)) == ({}, 0)
        assert _dispatch_tasks(h) == []
        clock.set(case.boundary)

    now = clock.now()
    assert _result(_sweep(h)) == ({task_type: len(case.due)}, 0)
    assert sorted(_dispatch_tasks(h), key=lambda t: t[1]) == sorted(
        (_pending(task_type, row, 1, now) for row in case.due), key=lambda t: t[1])
    subjects = {row[3][SUBJECT_KEY[task_type]] for row in _dispatch_tasks(h)}
    assert subjects.isdisjoint(case.not_due)

    assert _result(_sweep(h)) == ({}, 0), "a rodada seguinte enfileirou de novo com as tarefas vivas"
    assert len(_dispatch_tasks(h)) == len(case.due)


def test_reveal_rule_skips_not_delivered_question_failed_and_pending_grading(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """A regra da revelação, com o gabarito indo só a quem já respondeu:
    num disparo fechado, a regra `delivery.reveal` enfileira só a revelação devida —
    `answered`, `dont_know` e `answered_late` sem `revealed_at` e sem correção pendente —, nunca
    `not_delivered`, `question_failed` nem `no_answer` (a `no_answer` recebe só a edição de
    fechamento, sem gabarito, e é revelada pela atrasada válida ou pelo "Não sei" atrasado), nem a já
    revelada, nem a `no_answer` com a correção pendente (esta recebe a `submission.grade`, que a
    revelará no desfecho válido)."""
    h = _harness(db_url, sql, clock, settings)
    closed = _closed_dispatch(h)
    due = [_delivery(h, closed, state).id for state in ("answered", "dont_know", "answered_late")]
    unrevealed_no_answer = _delivery(h, closed, "no_answer").id  # sem `revealed_at` e sem correção
    _delivery(h, closed, "not_delivered")
    _delivery(h, closed, "question_failed")
    _delivery(h, closed, "question_failed", revealed_at=h.t0)
    _delivery(h, closed, "no_answer", revealed_at=h.t0)
    grading = _submission(h, _delivery(h, closed, "no_answer"), "grading")

    now = clock.now()
    assert _result(_sweep(h)) == ({REVEAL: 3, GRADE: 1}, 0)
    tasks = _dispatch_tasks(h)
    assert sorted(t for t in tasks if t[0] == REVEAL) == sorted(_pending(REVEAL, row, 1, now) for row in due)
    assert unrevealed_no_answer not in {t[3][SUBJECT_KEY[REVEAL]] for t in tasks if t[0] == REVEAL}
    assert [t for t in tasks if t[0] == GRADE] == [_pending(GRADE, grading.id, 1, now)]


# --- concorrência, posse esgotada, relógio, limite -----------------------------------------------------


def test_two_connections_never_duplicate_a_round(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """A trava é a primeira defesa, e a chave é a segunda: duas
    varreduras simultâneas, cada uma na sua conexão e soltas juntas por uma barreira, criam exatamente
    uma tarefa por assunto vencido (a soma das contagens dos dois `SweepResult` é o número de assuntos),
    com a chave `r1`; repetido em 3 levas de assuntos novos."""
    h = _harness(db_url, sql, clock, settings)
    for wave in range(3):
        deadline = clock.now() + timedelta(minutes=10)
        expiring = [_dispatch(h, "scheduled", opens_at=deadline).id for _ in range(3)]
        closing = [_dispatch(h, "open", opens_at=h.t0, closes_at=deadline).id for _ in range(3)]
        clock.set(deadline)
        before = len(_dispatch_tasks(h))
        barrier = threading.Barrier(2)

        def sweep() -> SweepResult:
            with psycopg.connect(db_url, autocommit=True) as conn:
                barrier.wait(timeout=WAIT)
                return sweep_once(conn, now=deadline, retry_gap=float(settings.backoff_max_seconds),
                                  rules=DISPATCH_RULES)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result(timeout=2 * WAIT) for f in [pool.submit(sweep), pool.submit(sweep)]]
        total: dict[str, int] = {}
        for result in results:
            for task_type, count in dict(result.enqueued).items():
                total[task_type] = total.get(task_type, 0) + count
        assert total == {EXPIRE: 3, CLOSE: 3}, f"leva {wave}: {total}"
        new = _dispatch_tasks(h)[before:]
        assert sorted(new) == sorted([_pending(EXPIRE, row, 1, deadline) for row in expiring]
                                     + [_pending(CLOSE, row, 1, deadline) for row in closing]), f"leva {wave}"


def test_lease_exhausted_task_gets_the_second_round_only_after_retry_gap(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """A tarefa `dispatch.open` montada por SQL
    em posse esgotada (`attempts = max_attempts`, `state = 'leased'`, `lease_until` no passado) é viva:
    a varredura não enfileira; a fila de tarefas a leva a `failed` (`LEASE_EXHAUSTED_ERROR`, com um
    `task.failed`); a varredura ainda não enfileira até `finished_at + retry_gap` (1 s antes, nada) e,
    no horário, cria a rodada `dispatch.open:<id>:r2` (k = 1 + 1 terminada)."""
    h = _harness(db_url, sql, clock, settings)
    dispatch = _dispatch(h, "scheduled", confirmed=True, opens_at=h.t0 - timedelta(seconds=1))
    subject = {"dispatch_row_id": dispatch.id}
    exhausted = _task(h, OPEN, subject, f"{OPEN}:{dispatch.id}", "leased", attempts=settings.max_attempts,
                      lease_owner=LOST_OWNER, lease_until=clock.now() - timedelta(seconds=1))

    assert _result(_sweep(h)) == ({}, 0), "a posse esgotada ainda é tarefa viva"
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    try:
        assert queue.claim("cq-teste-varredura-fila") is None
        record = queue.get(exhausted)
    finally:
        queue.close()
    failed_at = clock.now()
    assert (record.state, record.attempts, record.last_error, record.finished_at) == (
        "failed", settings.max_attempts, LEASE_EXHAUSTED_ERROR, failed_at)
    assert _events(h, "task.failed") == [
        ("task.failed", None, {"task_id": exhausted, "task_type": OPEN, "attempts": settings.max_attempts,
                               "error": LEASE_EXHAUSTED_ERROR}, failed_at)]

    assert _result(_sweep(h)) == ({}, 0)
    gap = settings.backoff_max_seconds
    assert _result(_sweep(h, now=failed_at + timedelta(seconds=gap - 1))) == ({}, 0)
    assert _rounds(h, OPEN, subject) == [(f"{OPEN}:{dispatch.id}", "failed")]

    clock.set(failed_at + timedelta(seconds=gap))
    assert _result(_sweep(h)) == ({OPEN: 1}, 0)
    assert _dispatch_tasks(h)[-1] == _pending(OPEN, dispatch.id, 2, clock.now())
    assert _rounds(h, OPEN, subject) == [(f"{OPEN}:{dispatch.id}", "failed"), (f"{OPEN}:{dispatch.id}:r2", "pending")]


def test_clock_going_back_uses_the_injected_now(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """Relógio que voltou: o vencido e o intervalo usam o `now`
    passado à varredura, nunca o relógio do banco (o `FakeClock` está em 2031). A rodada `r1` de um
    `dispatch.open` termina `done` sem efeito (montada por SQL: o tratador viu o relógio voltar) e o
    relógio volta para antes de `opens_at`: nada vence; já depois de `opens_at` de novo, mas dentro do
    `done_gap` da regra (o `backoff_max_seconds`, contado do `finished_at`), nada; só em
    `finished_at + backoff_max_seconds` sai a `r2`."""
    h = _harness(db_url, sql, clock, settings)
    opens = h.t0 + timedelta(minutes=30)
    dispatch = _dispatch(h, "scheduled", confirmed=True, opens_at=opens)
    subject = {"dispatch_row_id": dispatch.id}

    clock.set(opens + timedelta(seconds=30))
    assert _result(_sweep(h)) == ({OPEN: 1}, 0)
    finished = clock.now()
    sql.execute("UPDATE tasks SET state = 'done', attempts = 1, finished_at = %s WHERE idempotency_key = %s",
                (finished, f"{OPEN}:{dispatch.id}:r1"))

    for back in (opens - timedelta(seconds=10), opens, finished, finished + timedelta(seconds=59)):
        clock.set(back)
        assert _result(_sweep(h)) == ({}, 0), f"enfileirou em {back.isoformat()}"
    assert _rounds(h, OPEN, subject) == [(f"{OPEN}:{dispatch.id}:r1", "done")]

    clock.set(finished + timedelta(seconds=settings.backoff_max_seconds))
    assert _result(_sweep(h)) == ({OPEN: 1}, 0)
    assert _rounds(h, OPEN, subject) == [(f"{OPEN}:{dispatch.id}:r1", "done"), (f"{OPEN}:{dispatch.id}:r2", "pending")]


def test_limit_applies_after_live_and_gap_filters(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """Com `limit = 2` e 5 assuntos vencidos em
    ordem de `id`, os 2 primeiros no intervalo depois de uma rodada `failed`, os filtros "sem tarefa
    viva" e "fora do intervalo" ficam antes do `LIMIT`: a rodada enfileira o 3º e o 4º; a seguinte, o 5º;
    a outra, nada; e, vencido o intervalo, os 2 primeiros recebem a `r2`."""
    h = _harness(db_url, sql, clock, settings)
    deadline = clock.now()
    rows = [_dispatch(h, "scheduled", opens_at=deadline).id for _ in range(5)]
    failed_at = clock.now() - timedelta(seconds=10)
    for row in rows[:2]:
        _task(h, EXPIRE, {"dispatch_row_id": row}, f"{EXPIRE}:{row}:r1", "failed",
              attempts=settings.max_attempts, finished_at=failed_at, last_error="RuntimeError")

    now = clock.now()
    assert _result(_sweep(h, limit=2)) == ({EXPIRE: 2}, 0)
    assert _dispatch_tasks(h)[2:] == [_pending(EXPIRE, rows[2], 1, now), _pending(EXPIRE, rows[3], 1, now)]
    assert _result(_sweep(h, limit=2)) == ({EXPIRE: 1}, 0)
    assert _dispatch_tasks(h)[4:] == [_pending(EXPIRE, rows[4], 1, now)]
    assert _result(_sweep(h, limit=2)) == ({}, 0)

    clock.set(failed_at + timedelta(seconds=settings.backoff_max_seconds))
    later = clock.now()
    assert _result(_sweep(h, limit=2)) == ({EXPIRE: 2}, 0)
    assert _dispatch_tasks(h)[5:] == [_pending(EXPIRE, rows[0], 2, later), _pending(EXPIRE, rows[1], 2, later)]


def test_sweeper_only_enqueues_types_with_a_handler(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """Só enfileira tipos com tratador no laço: o `Sweeper`
    do laço do worker roda só as regras dos tipos em `handled_types`: com só `dispatch.expire`, a
    abertura e o update vencidos não recebem tarefa; sem bots (todos os tipos menos `telegram.update`),
    a abertura recebe e o update não. O `Sweeper` usa o `now` do `Clock` injetado."""
    h = _harness(db_url, sql, clock, settings)
    expiring = _dispatch(h, "scheduled", opens_at=clock.now())
    opening = _dispatch(h, "scheduled", confirmed=True, opens_at=clock.now())
    _open_dispatch(h)
    update = _update(h)

    def run(handled: set[str]) -> SweepResult:
        sweeper = Sweeper(db_url, clock=clock, settings=settings, handled_types=handled, interval=0.01)
        try:
            deadline = time.monotonic() + WAIT
            while (result := sweeper.maybe_run()) is None:
                assert time.monotonic() < deadline, "o Sweeper não rodou"
                time.sleep(0.02)
            return result
        finally:
            sweeper.close()

    now = clock.now()
    assert _result(run({EXPIRE})) == ({EXPIRE: 1}, 0)
    assert _dispatch_tasks(h) == [_pending(EXPIRE, expiring.id, 1, now)]

    without_bots = {VALIDATE, EXPIRE, OPEN, CLOSE, PREPARE, REVEAL, GRADE, "question.refetch"}
    assert _result(run(without_bots)) == ({OPEN: 1}, 0)
    assert _dispatch_tasks(h) == [_pending(EXPIRE, expiring.id, 1, now), _pending(OPEN, opening.id, 1, now)]
    assert _rounds(h, UPDATE, {"update_row_id": update.id}) == []


# --- expiração e a corrida com a confirmação --------------------------------------------------------------


def test_expiry_of_scheduled_and_immediate_cancels_with_event(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """O agendado sem confirmação até a abertura
    (`confirm_deadline = opens_at`) e o imediato validado há 24 h (`validated_at + 24 h`) vencem juntos;
    1 s antes, nada; no horário, a varredura enfileira as duas `dispatch.expire` e o tratador real leva
    os dois a `cancelled/expired` (`cancelled_at` = o horário, sem confirmação), com um
    `dispatch.cancelled {offering_id, dispatch_id, cancel_reason: expired}` cada, sem ator; o agendado
    confirmado com a mesma abertura não é tocado; depois, nenhuma rodada nova."""
    from cq_server.dispatches import expire_task_handler

    h = _harness(db_url, sql, clock, settings)
    deadline = h.t0 + timedelta(hours=3)
    scheduled = _dispatch(h, "scheduled", opens_at=deadline)
    immediate = _dispatch(h, "scheduled", immediate=True,
                          validated_at=deadline - timedelta(hours=IMMEDIATE_CONFIRM_HOURS))
    confirmed = _dispatch(h, "scheduled", confirmed=True, opens_at=deadline + timedelta(hours=1))

    clock.set(deadline - timedelta(seconds=1))
    assert _result(_sweep(h)) == ({}, 0)
    clock.set(deadline)
    assert _result(_sweep(h)) == ({EXPIRE: 2}, 0)

    handler = expire_task_handler(db_url, clock=clock, settings=settings)
    try:
        assert sorted(_run(h, EXPIRE, handler)) == sorted(
            [(f"{EXPIRE}:{scheduled.id}:r1", "done"), (f"{EXPIRE}:{immediate.id}:r1", "done")])
    finally:
        handler.close()

    for dispatch in (scheduled, immediate):
        row = _dispatch_row(h, dispatch)
        assert (row["state"], row["cancel_reason"], row["cancelled_at"], row["confirmed_at"]) == (
            "cancelled", "expired", deadline, None)
    assert (_dispatch_row(h, confirmed)["state"], _dispatch_row(h, confirmed)["cancelled_at"]) == ("scheduled", None)
    assert sorted(_events(h, "dispatch.cancelled"), key=lambda e: e[2]["dispatch_id"]) == sorted(
        [("dispatch.cancelled", None, {"offering_id": h.offering.uuid, "dispatch_id": d.uuid,
                                       "cancel_reason": "expired"}, deadline) for d in (scheduled, immediate)],
        key=lambda e: e[2]["dispatch_id"])

    clock.advance(settings.backoff_max_seconds + 1)
    assert _result(_sweep(h)) == ({}, 0)
    assert sorted(t[1] for t in _dispatch_tasks(h)) == sorted([f"{EXPIRE}:{scheduled.id}:r1", f"{EXPIRE}:{immediate.id}:r1"])


def test_confirmation_racing_expiry_has_a_single_outcome(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any, client: Any) -> None:
    """Quem trava primeiro decide: para
    4 agendados sem confirmação com o prazo em `D`, a confirmação pela API (relógio do app em `D − 1 s`)
    e o tratador real da `dispatch.expire` (relógio próprio em `D`, a tarefa da varredura real) correm
    ao mesmo tempo, cada um na sua conexão: ou a confirmação vence (200, `confirmed_at = D − 1 s`, o
    tratador termina sem efeito) ou a expiração vence (`cancelled/expired` em `D` e 409
    `invalid_dispatch_state` na confirmação); nunca os dois, e exatamente um evento
    (`dispatch.confirmed` ou `dispatch.cancelled`) por disparo."""
    from cq_server.dispatches import expire_task_handler

    h = _harness(db_url, sql, clock, settings)
    deadline = h.t0 + timedelta(hours=2)
    code = sql.execute("SELECT code FROM offerings WHERE id = %s", (h.offering.id,)).fetchone()[0]
    dispatches = [_dispatch(h, "scheduled", opens_at=deadline) for _ in range(4)]
    late_clock = FakeClock(deadline)
    assert _result(_sweep(h, now=deadline)) == ({EXPIRE: 4}, 0)
    clock.set(deadline - timedelta(seconds=1))

    handler = expire_task_handler(db_url, clock=late_clock, settings=settings)
    queue = TaskQueue(db_url, clock=late_clock, settings=settings)
    try:
        for dispatch in dispatches:
            task = _claim(h, EXPIRE, clock=late_clock, task_id=_task_id(h, f"{EXPIRE}:{dispatch.id}:r1"))
            assert task is not None
            barrier = threading.Barrier(2)

            def confirm(dispatch: Row = dispatch) -> Any:
                barrier.wait(timeout=WAIT)
                return client.post(f"/v1/offerings/{code}/dispatches/{dispatch.uuid}/confirm",
                                   headers=auth(ADMIN_TOKEN))

            def expire(task: ClaimedTask = task) -> None:
                barrier.wait(timeout=WAIT)
                handler(task)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                confirmed, expired = pool.submit(confirm), pool.submit(expire)
                resp = confirmed.result(timeout=2 * WAIT)
                expired.result(timeout=2 * WAIT)
            assert queue.complete(task.id, OWNER)

            row = _dispatch_row(h, dispatch)
            events = [(e[0], e[1] is None) for e in _events(h, "dispatch.confirmed", "dispatch.cancelled")
                      if e[2]["dispatch_id"] == dispatch.uuid]
            if resp.status_code == 200:
                assert (resp.json()["state"], resp.json()["confirmed"]) == ("scheduled", True)
                assert (row["state"], row["confirmed_at"], row["cancel_reason"], row["cancelled_at"]) == (
                    "scheduled", deadline - timedelta(seconds=1), None, None)
                assert events == [("dispatch.confirmed", False)]
            else:
                assert_error(resp, 409, "invalid_dispatch_state")
                assert (row["state"], row["confirmed_at"], row["cancel_reason"], row["cancelled_at"]) == (
                    "cancelled", None, "expired", deadline)
                assert events == [("dispatch.cancelled", True)]
    finally:
        queue.close()
        handler.close()


# --- update venenoso --------------------------------------------------------------------------------------


def test_poison_update_is_abandoned_after_three_failed_rounds_without_event(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any) -> None:
    """O update `pending` de um bot com
    disparo aberto, cuja tarefa falha de vez em toda rodada (a fila de tarefas com `PermanentTaskError`),
    recebe as rodadas `r1`, `r2` e `r3`, cada uma só depois do `retry_gap` da anterior (1 s antes,
    nada); esgotado o teto de `SWEEP_MAX_UPDATE_ROUNDS = 3` rodadas `failed`, a rodada seguinte não
    enfileira e grava `processed` com `outcome = 'abandoned'` e `processed_at = now`
    (`SweepResult.abandoned == 1`), consultável por (`bot_id`, `update_id`), sem evento além dos 3
    `task.failed`; depois, nada mais."""
    h = _harness(db_url, sql, clock, settings)
    _open_dispatch(h)
    update = _update(h)
    subject = {"update_row_id": update.id}
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    gap = settings.backoff_max_seconds
    try:
        for k in range(1, SWEEP_MAX_UPDATE_ROUNDS + 1):
            assert _result(_sweep(h)) == ({UPDATE: 1}, 0), f"rodada {k}"
            task = _claim(h, UPDATE)
            assert task is not None and task.payload == subject
            assert queue.fail(task.id, OWNER, "PermanentTaskError", permanent=True)
            assert _result(_sweep(h, now=clock.now() + timedelta(seconds=gap - 1))) == ({}, 0)
            clock.advance(gap)
    finally:
        queue.close()
    assert _rounds(h, UPDATE, subject) == [(f"{UPDATE}:{update.id}:r{k}", "failed") for k in (1, 2, 3)]

    abandoned_at = clock.now()
    assert _result(_sweep(h)) == ({}, 1)
    assert sql.execute("SELECT state, outcome, processed_at FROM telegram_updates "
                       "WHERE bot_id = %s AND update_id = %s", (h.bot, int(update.uuid))).fetchone() == (
        "processed", "abandoned", abandoned_at)
    assert len(_rounds(h, UPDATE, subject)) == 3
    assert _all_event_types(h) == ["task.failed"] * 3

    clock.advance(gap)
    assert _result(_sweep(h)) == ({}, 0)
    assert len(_rounds(h, UPDATE, subject)) == 3


# --- fechamento com o leitor atrasado ------------------------------------------------------------------------


def test_close_rounds_wait_for_late_reader_without_task_failed(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any, bots: Any) -> None:
    """Barreira do fechamento: com o bot sendo
    consultado (posse viva do leitor) e `last_poll_ok_at` anterior a `closes_at`, cada rodada
    `dispatch.close` (o tratador real) termina `done` sem efeito, sem tentativa extra; a seguinte só sai
    `CLOSE_RECHECK_SECONDS` depois do `finished_at` da anterior (1 s antes, nada), com a chave `r<k>`; o
    leitor fica atrasado por mais de 34 s (a soma das esperas que esgotaria um `RetryTaskLater`), sem
    nenhum `task.failed`; quando o leitor alcança `closes_at`, a rodada seguinte fecha, com um único
    `dispatch.closed {trigger: window, counts}` (8 contagens em 0) sem ator, e nenhuma rodada nova."""
    from cq_server.dispatches import close_task_handler

    h = _harness(db_url, sql, clock, settings)
    closes = h.t0 + timedelta(minutes=30)
    dispatch = _dispatch(h, "open", opens_at=h.t0, closes_at=closes)
    subject = {"dispatch_row_id": dispatch.id}
    sql.execute("UPDATE bots SET lease_owner = 'cq-leitor-atrasado', lease_until = %s, last_poll_ok_at = %s "
                "WHERE id = %s", (closes + timedelta(days=1), closes - timedelta(seconds=30), h.bot))

    channel = _channel(h, bots)
    handler = close_task_handler(db_url, clock=clock, settings=settings, channel=channel)
    try:
        clock.set(closes)
        rounds = 0
        while clock.now() - closes <= timedelta(seconds=40):
            rounds += 1
            assert _result(_sweep(h)) == ({CLOSE: 1}, 0), f"rodada {rounds}"
            assert _run(h, CLOSE, handler) == [(f"{CLOSE}:{dispatch.id}:r{rounds}", "done")]
            assert _dispatch_row(h, dispatch)["state"] == "open"
            clock.advance(CLOSE_RECHECK_SECONDS - 1)
            assert _result(_sweep(h)) == ({}, 0), "a rodada seguinte saiu antes de CLOSE_RECHECK_SECONDS"
            clock.advance(1)
        assert rounds >= 8
        attempts = [r[0] for r in sql.execute("SELECT attempts FROM tasks WHERE task_type = %s ORDER BY id",
                                              (CLOSE,)).fetchall()]
        assert attempts == [1] * rounds
        assert _events(h, "task.failed", "dispatch.closed") == []

        sql.execute("UPDATE bots SET last_poll_ok_at = %s WHERE id = %s", (closes, h.bot))
        closed_at = clock.now()
        assert _result(_sweep(h)) == ({CLOSE: 1}, 0)
        assert _run(h, CLOSE, handler) == [(f"{CLOSE}:{dispatch.id}:r{rounds + 1}", "done")]
    finally:
        handler.close()
        channel.close()

    row = _dispatch_row(h, dispatch)
    assert (row["state"], row["closed_at"]) == ("closed", closed_at)
    counts = {k: 0 for k in ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                             "answered_late", "question_failed")}
    assert _events(h, "task.failed", "dispatch.closed") == [
        ("dispatch.closed", None, {"offering_id": h.offering.uuid, "dispatch_id": dispatch.uuid,
                                   "trigger": "window", "counts": counts}, closed_at)]
    assert all(state == "done" for _key, state in _rounds(h, CLOSE, subject))
    clock.advance(RECHECK)
    assert _result(_sweep(h)) == ({}, 0)


# --- validação: continuação com a posse esgotada e o teto de rodadas ---------------------------------------


def test_validate_continuation_lost_by_lease_resumes_by_state(
        db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any, bots: Any, engine_fake: Any,
        repo_fetcher: Any) -> None:
    """A rodada `r1`
    da validação conclui a obtenção e enfileira a continuação `dispatch.validate:<id>:c<id da r1>`; a
    continuação montada em posse esgotada vira `failed` pela fila de tarefas; a varredura só a reanima
    depois do `retry_gap`, com a rodada `r3` (k = 1 + 2 terminadas), que retoma **pelo estado**: a
    árvore reaproveitada (sem rede), a inspeção (ainda não feita), a continuação `c<id da r3>` e a
    variante de amostra, até `scheduled` com a prévia e `confirm_deadline = validated_at + 24 h`, e um
    único `dispatch.validated {target_count: 0, warnings: [empty_target, no_teacher_contact]}` sem
    ator (o oferecimento montado por SQL fica no padrão: mensagens livres desligadas e sem contato; o
    aviso `no_teacher_contact` vem por último). O motor recebe uma inspeção e uma
    variante; a obtenção, uma cópia e dois reaproveitamentos."""
    from cq_server.dispatches import validate_task_handler

    h = _harness(db_url, sql, clock, settings)
    engine_fake.program((REPO, COMMIT, QID), QID)
    dispatch = _dispatch(h, "validating", immediate=True)
    subject = {"dispatch_row_id": dispatch.id}

    engine = _engine(engine_fake)
    channel = _channel(h, bots)
    handler = validate_task_handler(db_url, clock=clock, settings=settings, engine=engine,
                                    fetcher=repo_fetcher, channel=channel)
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    try:
        assert _result(_sweep(h)) == ({VALIDATE: 1}, 0)
        first = _claim(h, VALIDATE)
        assert first is not None
        handler(first)
        assert queue.complete(first.id, OWNER)
        continuation = f"{VALIDATE}:{dispatch.id}:c{first.id}"
        assert _rounds(h, VALIDATE, subject) == [(f"{VALIDATE}:{dispatch.id}:r1", "done"), (continuation, "pending")]
        assert [c.reused for c in repo_fetcher.calls()] == [False]
        assert [c.op for c in engine_fake.calls() if c.op != "health"] == []

        lost = _task_id(h, continuation)
        assert sql.execute(
            "UPDATE tasks SET state = 'leased', attempts = %s, lease_owner = %s, lease_until = %s "
            "WHERE id = %s AND state = 'pending' RETURNING id",
            (settings.max_attempts, LOST_OWNER, clock.now() - timedelta(seconds=1), lost)).fetchone() == (lost,)
        assert _result(_sweep(h)) == ({}, 0), "a continuação em posse ainda é tarefa viva"
        assert queue.claim("cq-teste-varredura-fila") is None
        failed_at = clock.now()
        assert (queue.get(lost).state, queue.get(lost).last_error) == ("failed", LEASE_EXHAUSTED_ERROR)
        assert _result(_sweep(h)) == ({}, 0)
        assert _dispatch_row(h, dispatch)["state"] == "validating"

        clock.set(failed_at + timedelta(seconds=settings.backoff_max_seconds))
        validated_at = clock.now()
        assert _result(_sweep(h)) == ({VALIDATE: 1}, 0)
        processed = _run(h, VALIDATE, handler)
    finally:
        queue.close()
        handler.close()
        channel.close()
        engine.close()

    third = _task_id(h, f"{VALIDATE}:{dispatch.id}:r3")
    assert processed == [(f"{VALIDATE}:{dispatch.id}:r3", "done"), (f"{VALIDATE}:{dispatch.id}:c{third}", "done")]
    assert _rounds(h, VALIDATE, subject) == [
        (f"{VALIDATE}:{dispatch.id}:r1", "done"), (continuation, "failed"),
        (f"{VALIDATE}:{dispatch.id}:r3", "done"), (f"{VALIDATE}:{dispatch.id}:c{third}", "done")]
    row = _dispatch_row(h, dispatch)
    assert (row["state"], row["validated_at"], row["confirm_deadline"], row["rejection"], row["confirmed_at"]) == (
        "scheduled", validated_at, validated_at + timedelta(hours=IMMEDIATE_CONFIRM_HOURS), None, None)
    assert isinstance(row["preview"], dict) and row["preview"]
    assert [c.reused for c in repo_fetcher.calls()] == [False, True, True]
    assert [c.op for c in engine_fake.calls() if c.op != "health"] == ["inspect", "create_variant"]
    assert _events(h, "dispatch.validated", "dispatch.rejected", "dispatch.cancelled") == [
        ("dispatch.validated", None, {"offering_id": h.offering.uuid, "dispatch_id": dispatch.uuid,
                                      "target_count": 0, "warnings": ["empty_target", "no_teacher_contact"]},
         validated_at)]
    assert _events(h, "task.failed") == [
        ("task.failed", None, {"task_id": lost, "task_type": VALIDATE, "attempts": settings.max_attempts,
                               "error": LEASE_EXHAUSTED_ERROR}, failed_at)]


@pytest.mark.parametrize("scheduled", [False, True], ids=["imediato", "agendado-depois-da-abertura"])
def test_validate_rounds_stop_at_ceiling(
        scheduled: bool, db_url: str, sql: psycopg.Connection, clock: FakeClock, settings: Any,
        engine_fake: Any, repo_fetcher: Any) -> None:
    """Com o `validate_task_handler` sem
    canal (`channel=None`), a obtenção e a inspeção passam e a variante falha de vez
    (`PermanentTaskError` "canal sem rota") em toda rodada: `r1` (obtenção) e `c<r1>` (inspeção)
    terminam `done`, `c<c1>`, `r4` e `r5` terminam `failed` — exatamente `VALIDATE_MAX_ROUNDS = 3`
    rodadas `failed`, cada uma só depois do `retry_gap` —; a rodada seguinte da varredura não enfileira
    e, na transação do assunto, leva o disparo a `rejected/internal_failure` com
    `dispatch.rejected {rejection_reason: internal_failure}` sem ator (imediato), ou, no agendado com o
    relógio além de `opens_at`, a `cancelled/validation_overran` com
    `dispatch.cancelled {cancel_reason: validation_overran}`; nenhuma 4ª rodada `failed` depois."""
    from cq_server.dispatches import validate_task_handler

    h = _harness(db_url, sql, clock, settings)
    engine_fake.program((REPO, COMMIT, QID), QID)
    opens = h.t0 + timedelta(hours=1)
    dispatch = (_dispatch(h, "validating", opens_at=opens) if scheduled
                else _dispatch(h, "validating", immediate=True))
    subject = {"dispatch_row_id": dispatch.id}
    gap = settings.backoff_max_seconds

    engine = _engine(engine_fake)
    handler = validate_task_handler(db_url, clock=clock, settings=settings, engine=engine,
                                    fetcher=repo_fetcher, channel=None)
    try:
        assert _result(_sweep(h)) == ({VALIDATE: 1}, 0)
        first_round = _run(h, VALIDATE, handler)
        r1 = _task_id(h, f"{VALIDATE}:{dispatch.id}:r1")
        c1 = _task_id(h, f"{VALIDATE}:{dispatch.id}:c{r1}")
        assert first_round == [(f"{VALIDATE}:{dispatch.id}:r1", "done"), (f"{VALIDATE}:{dispatch.id}:c{r1}", "done"),
                               (f"{VALIDATE}:{dispatch.id}:c{c1}", "failed")]
        for k in (4, 5):
            assert _result(_sweep(h, now=clock.now() + timedelta(seconds=gap - 1))) == ({}, 0)
            clock.advance(gap)
            assert _result(_sweep(h)) == ({VALIDATE: 1}, 0), f"rodada r{k}"
            assert _run(h, VALIDATE, handler) == [(f"{VALIDATE}:{dispatch.id}:r{k}", "failed")]
            assert _dispatch_row(h, dispatch)["state"] == "validating"
    finally:
        handler.close()
        engine.close()

    failed = [key for key, state in _rounds(h, VALIDATE, subject) if state == "failed"]
    assert len(failed) == VALIDATE_MAX_ROUNDS == 3
    if scheduled:
        clock.set(opens + timedelta(seconds=1))
    else:
        clock.advance(gap)
    ended_at = clock.now()
    enqueued, _abandoned = _result(_sweep(h))
    assert VALIDATE not in enqueued, "uma 4ª rodada foi enfileirada depois do teto"

    row = _dispatch_row(h, dispatch)
    ref = {"offering_id": h.offering.uuid, "dispatch_id": dispatch.uuid}
    if scheduled:
        assert (row["state"], row["cancel_reason"], row["cancelled_at"], row["rejection"], row["validated_at"]) == (
            "cancelled", "validation_overran", ended_at, None, None)
        assert _events(h, "dispatch.rejected", "dispatch.cancelled", "dispatch.validated") == [
            ("dispatch.cancelled", None, {**ref, "cancel_reason": "validation_overran"}, ended_at)]
    else:
        assert (row["state"], row["rejection"], row["rejected_at"], row["validated_at"], row["preview"]) == (
            "rejected", "internal_failure", ended_at, None, None)
        assert _events(h, "dispatch.rejected", "dispatch.cancelled", "dispatch.validated") == [
            ("dispatch.rejected", None, {**ref, "rejection_reason": "internal_failure"}, ended_at)]
    assert len(_rounds(h, VALIDATE, subject)) == 5

    clock.advance(gap)
    enqueued, _abandoned = _result(_sweep(h))
    assert VALIDATE not in enqueued
    assert len(_rounds(h, VALIDATE, subject)) == 5
    assert [e[2]["task_type"] for e in _events(h, "task.failed")] == [VALIDATE] * 3


# --- worker real: a correção reanimada e a entrada depois do horário ----------------------


DELIVERY_PACE = 0.05  # ritmo por chat curto nos testes (o `SendPacer` real)


@dataclasses.dataclass
class World:
    client: Any
    sql: psycopg.Connection
    clock: FakeClock
    settings: Any
    api: Any
    code: str
    offering_id: str
    tg_bot: int
    bot_row: int
    user: Any
    person_row: int
    pseudo_id: str


@dataclasses.dataclass(frozen=True)
class Opened:
    dispatch_id: str
    row: int
    closes_at: datetime
    question_ref_id: str


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.05)


def _mark_known_chat(api: Any, tg_bot: int, chat_id: int) -> None:
    """O chat privado já falou com o bot (senão o envio dá 400 `chat not found`)."""
    fake = getattr(api, "fake", api)
    with fake._cond:
        fake._bot(tg_bot).known_chats[chat_id] = "private"


def _world(client: Any, sql: psycopg.Connection, clock: FakeClock, settings: Any, api: Any,
           engine_fake: Any, repo_fetcher: Any) -> World:
    """Oferecimento `active` com bot `registered` (API do oferecimento), um aluno importado e vinculado por SQL
    (matrícula `active` com o termo aceito e endereço `active` no bot)."""
    n = next(_counter)
    offering = create_offering(client, ADMIN_TOKEN, code=f"varredura-w-{n:04d}", timezone=ZONE)
    tg_bot = api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    bot_row, offering_row = sql.execute("SELECT id, offering_id FROM bots WHERE telegram_bot_id = %s",
                                        (tg_bot,)).fetchone()
    import_students(client, ADMIN_TOKEN, offering["code"], [{
        "name": f"Pessoa Varredura {n}", "ra": f"58{n:04d}0001",
        "email": f"pessoa.varredura.{n}@turma.example.org"}])
    enrollment_row, person_row, pseudo_id = sql.execute(
        "SELECT e.id, e.person_id, p.pseudo_id FROM enrollments e JOIN people p ON p.id = e.person_id "
        "WHERE e.offering_id = %s AND e.role = 'student'", (offering_row,)).fetchone()
    now = clock.now()
    sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                "terms_accepted_at = %s WHERE id = %s", (now, now, enrollment_row))
    user = make_user(700 + n)
    sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                (person_row, bot_row, user.user_id, now, now))
    _mark_known_chat(api, tg_bot, user.user_id)
    engine_fake.program((REPO, COMMIT, QID), QID)
    repo_fetcher.add(REPO, COMMIT)
    return World(client=client, sql=sql, clock=clock, settings=settings, api=api, code=offering["code"],
                 offering_id=offering["offering_id"], tg_bot=tg_bot, bot_row=bot_row, user=user,
                 person_row=person_row, pseudo_id=str(pseudo_id))


def _start(world: World, bot_worker_thread: Any, engine_fake: Any, repo_fetcher: Any) -> Any:
    """O worker com bots (o caminho de produção), com o motor, a obtenção e a entrega do disparo; devolve o
    adaptador do motor (o teste o fecha depois de parar o worker)."""
    from cq_server.config import DeliverySettings

    engine = _engine(engine_fake)
    bot_worker_thread(engine=engine, fetcher=repo_fetcher,
                      delivery=DeliverySettings(chat_interval_seconds=DELIVERY_PACE))
    _wait(lambda: world.sql.execute("SELECT last_poll_ok_at FROM bots WHERE id = %s",
                                    (world.bot_row,)).fetchone()[0] is not None,
          "o leitor do bot consultou o Telegram")
    return engine


def _dispatch_state(world: World, opened: Opened) -> str:
    return world.sql.execute("SELECT state FROM dispatches WHERE id = %s", (opened.row,)).fetchone()[0]


def _delivery_row(world: World, opened: Opened) -> dict:
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM deliveries WHERE dispatch_id = %s", (opened.row,)).fetchone()


def _open(world: World) -> Opened:
    """Prepara um imediato com o fechamento em 10 min, espera a validação, confirma e espera a entrega
    `delivered` com a questão e o pedido do campo enviados."""
    closes_at = world.clock.now() + timedelta(minutes=10)
    prepared = prepare_dispatch(world.client, ADMIN_TOKEN, world.code, repo=REPO, commit=COMMIT, qid=QID,
                                closes_at=iso(closes_at))
    dispatch_id = prepared["dispatch_id"]
    row, ref_uuid = world.sql.execute(
        "SELECT s.id, r.ref_uuid FROM dispatches s JOIN question_refs r ON r.id = s.question_ref_id "
        "WHERE s.dispatch_uuid = %s", (dispatch_id,)).fetchone()
    opened = Opened(dispatch_id=dispatch_id, row=row, closes_at=closes_at, question_ref_id=str(ref_uuid))
    _wait(lambda: _dispatch_state(world, opened) == "scheduled", "a validação do disparo")
    confirm_dispatch(world.client, ADMIN_TOKEN, world.code, dispatch_id)
    _wait(lambda: (d := _delivery_row(world, opened)) is not None and d["state"] == "delivered",
          "a entrega da questão")
    _wait(lambda: [tuple(r) for r in world.sql.execute(
        "SELECT delivery_role, state FROM outbox_messages WHERE delivery_id = %s ORDER BY id",
        (_delivery_row(world, opened)["id"],)).fetchall()] == [("question", "sent"), ("field_prompt", "sent")],
        "a questão e o pedido do campo")
    return opened


def _busy(world: World) -> list[str]:
    now = world.clock.now()
    busy = [f"tarefa {t}" for (t,) in world.sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"mensagem {m}" for (m,) in world.sql.execute(
        "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    return busy


def _settle(world: World) -> None:
    """Espera o sistema parar (nenhuma tarefa nem mensagem disponível), duas vezes, com mais de uma
    rodada da varredura (1 s real) entre elas."""
    deadline = time.monotonic() + 2 * WAIT
    while True:
        _wait(lambda: not _busy(world), "o sistema parar", timeout=2 * WAIT)
        time.sleep(1.2)
        if not _busy(world):
            return
        assert time.monotonic() < deadline, f"o sistema não parou: {_busy(world)}"


def _world_rounds(world: World, task_type: str, subject: dict[str, int]) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in world.sql.execute(
        "SELECT idempotency_key, state FROM tasks WHERE task_type = %s AND payload @> %s ORDER BY id",
        (task_type, Jsonb(subject))).fetchall()]


def _world_events(world: World, *types: str) -> list[tuple[str, Any, dict]]:
    return [tuple(r) for r in world.sql.execute(
        "SELECT type, actor_token_id, data FROM events WHERE type = ANY(%s) ORDER BY seq",
        (list(types),)).fetchall()]


def _graded_event(world: World, opened: Opened, delivery: dict, submission: dict, *, late: bool) -> tuple:
    """O `submission.graded` da resposta exata do preset `number` (nota 1, razão 1, resíduo 0)."""
    return ("submission.graded", None, {
        "offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
        "delivery_id": str(delivery["delivery_uuid"]), "submission_id": str(submission["submission_uuid"]),
        "question_ref_id": opened.question_ref_id, "person": world.pseudo_id, "valid": True, "late": late,
        "normalized": False, "score": 1.0,
        "parts": [{"index": 0, "score": 1.0, "choices": [], "ratio": 1.0, "residual": 0.0}]})


def _submissions(world: World, delivery: dict) -> list[dict]:
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM submissions WHERE delivery_id = %s ORDER BY id",
                           (delivery["id"],)).fetchall()


def test_lease_exhausted_grading_is_revived_and_grading_completes(
        client: Any, sql: psycopg.Connection, clock: FakeClock, settings: Any, rich_bot_api: Any,
        engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """A tarefa de correção é marcada como `failed` pela posse, e a correção sai
    assim mesmo: com o motor fora do ar na
    correção, a tarefa `submission.grade` da resposta digitada fica `pending` depois da 1ª falha; montada
    por SQL em posse esgotada, o worker a leva a `failed` (`LEASE_EXHAUSTED_ERROR`, um `task.failed`
    exato) e a submissão continua `grading`; dentro do `retry_gap`, nenhuma rodada nova; com o motor de
    volta e o relógio em `failed_at + retry_gap`, a varredura do worker cria
    `submission.grade:<id>:r2`, a correção sai (`graded`, válida, nota 1), a entrega vira `answered` e
    sai um único `submission.graded` exato; o motor recebeu a mesma resposta crua nas duas chamadas."""
    world = _world(client, sql, clock, settings, rich_bot_api, engine_fake, repo_fetcher)
    engine = _start(world, bot_worker_thread, engine_fake, repo_fetcher)
    try:
        opened = _open(world)
        _settle(world)
        engine_fake.fail("grade", error="internal_error", times=None)
        rich_bot_api.push_text(world.tg_bot, world.user, ANSWER)
        delivery = _delivery_row(world, opened)
        (submission,) = _wait(lambda: _submissions(world, delivery), "a submissão da resposta")
        subject = {"submission_row_id": submission["id"]}
        grade_task = _wait(lambda: world.sql.execute(
            "SELECT id FROM tasks WHERE task_type = %s AND payload @> %s AND state = 'pending' "
            "AND last_error IS NOT NULL", (GRADE, Jsonb(subject))).fetchone(), "a 1ª falha da correção")[0]

        assert sql.execute(
            "UPDATE tasks SET state = 'leased', attempts = %s, lease_owner = %s, lease_until = %s "
            "WHERE id = %s AND state = 'pending' RETURNING id",
            (settings.max_attempts, LOST_OWNER, clock.now() - timedelta(seconds=1), grade_task)).fetchone() == (
            grade_task,)
        _wait(lambda: sql.execute("SELECT state FROM tasks WHERE id = %s", (grade_task,)).fetchone()[0] == "failed",
              "a posse esgotada virou failed")
        failed_at = clock.now()
        assert sql.execute("SELECT last_error, finished_at FROM tasks WHERE id = %s", (grade_task,)).fetchone() == (
            LEASE_EXHAUSTED_ERROR, failed_at)
        time.sleep(2.5)  # mais de duas rodadas da varredura (1 s real) dentro do `retry_gap`
        assert [state for _key, state in _world_rounds(world, GRADE, subject)] == ["failed"]
        assert _submissions(world, delivery)[0]["outcome"] == "grading"
        assert _delivery_row(world, opened)["state"] == "delivered"

        engine_fake.clear_failures()
        clock.set(failed_at + timedelta(seconds=settings.backoff_max_seconds))
        _wait(lambda: _delivery_row(world, opened)["state"] == "answered", "a correção reanimada")
        _settle(world)
    finally:
        bot_worker_thread.stop()
        engine.close()

    assert _world_rounds(world, GRADE, subject)[1:] == [(f"{GRADE}:{submission['id']}:r2", "done")]
    assert [state for _key, state in _world_rounds(world, GRADE, subject)] == ["failed", "done"]
    delivery = _delivery_row(world, opened)
    (graded,) = _submissions(world, delivery)
    assert (graded["outcome"], graded["valid"], graded["late"], graded["score"]) == ("graded", True, False, 1.0)
    calls = engine_fake.calls("grade")
    assert len(calls) == 2 and calls[-1].status == 200
    assert [c.request["raw_submitted_answers"] for c in calls] == [{"resposta": ANSWER}] * 2
    assert _world_events(world, "submission.graded") == [_graded_event(world, opened, delivery, graded, late=False)]
    assert _world_events(world, "task.failed") == [
        ("task.failed", None, {"task_id": grade_task, "task_type": GRADE, "attempts": settings.max_attempts,
                               "error": LEASE_EXHAUSTED_ERROR})]


_phantom_ids = itertools.count(2_200_000_001)


def test_late_entry_waits_for_transition_and_done_rounds_do_not_count(
        client: Any, sql: psycopg.Connection, clock: FakeClock, settings: Any, rich_bot_api: Any,
        engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """Com a
    barreira do fechamento presa por um update fantasma (`pending`, gravado antes de `closes_at`, com a
    tarefa tomada por um dono fictício de posse longa), a resposta por "responder" gravada depois de
    `closes_at` termina `done` sem efeito em toda rodada: a tarefa original e as rodadas
    `telegram.update:<id>:r2` … `r5` (uma a cada `CLOSE_RECHECK_SECONDS`), todas `done`, mais que
    `SWEEP_MAX_UPDATE_ROUNDS`; o update continua `pending` (nada é abandonado, nenhuma submissão, nenhum
    `task.failed`) e o disparo continua `open`; solto o fantasma, o fechamento acontece e a mesma
    entrada é processada depois dele: `answer_recorded`, submissão válida com `late = true`,
    `answered_late`, e os eventos na ordem `dispatch.closed` → `submission.graded`."""
    world = _world(client, sql, clock, settings, rich_bot_api, engine_fake, repo_fetcher)
    engine = _start(world, bot_worker_thread, engine_fake, repo_fetcher)
    try:
        opened = _open(world)
        _settle(world)
        principal = _delivery_row(world, opened)["principal_message_id"]
        question_id = int(sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s",
                                      (principal,)).fetchone()[0])

        # O fantasma: gravado 3 s antes do horário, com a tarefa tomada numa só transação.
        clock.set(opened.closes_at - timedelta(seconds=3))
        stranger = make_user(990)
        _mark_known_chat(rich_bot_api, world.tg_bot, stranger.user_id)
        now = clock.now()
        with psycopg.connect(settings.database_url) as conn, conn.transaction():
            epoch = conn.execute("SELECT update_epoch FROM bots WHERE id = %s", (world.bot_row,)).fetchone()[0]
            phantom = conn.execute(
                "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, "
                "sender_id, text, command, details, received_at, state) VALUES "
                "(%s, %s, %s, 'message', %s, 'private', %s, 'olá', NULL, %s, %s, 'pending') RETURNING id",
                (world.bot_row, epoch, next(_phantom_ids), stranger.user_id, stranger.user_id,
                 Jsonb({"message_id": 1, "date": int(now.timestamp())}), now)).fetchone()[0]
            phantom_task = conn.execute(
                "INSERT INTO tasks (task_type, payload, idempotency_key, state, attempts, available_at, "
                "created_at, lease_owner, lease_until) VALUES (%s, %s, %s, 'leased', 1, %s, %s, %s, %s) "
                "RETURNING id", (UPDATE, Jsonb({"update_row_id": phantom}), f"{UPDATE}:{phantom}", now, now,
                                 "cq-teste-fantasma", now + timedelta(days=1))).fetchone()[0]

        clock.set(opened.closes_at + timedelta(seconds=1))
        late_update_id = rich_bot_api.push_text(world.tg_bot, world.user, ANSWER, reply_to_message_id=question_id)
        late = _wait(lambda: sql.execute(
            "SELECT id, received_at FROM telegram_updates WHERE bot_id = %s AND update_id = %s",
            (world.bot_row, late_update_id)).fetchone(), "o leitor gravou a resposta atrasada")
        late_row, late_received = late
        assert late_received > opened.closes_at
        subject = {"update_row_id": late_row}

        rounds = SWEEP_MAX_UPDATE_ROUNDS + 2
        for k in range(1, rounds + 1):
            _wait(lambda k=k: sum(1 for _key, s in _world_rounds(world, UPDATE, subject) if s == "done") >= k,
                  f"a rodada {k} da entrada atrasada")
            assert _dispatch_state(world, opened) == "open"
            clock.advance(RECHECK)
        keys = _world_rounds(world, UPDATE, subject)[:rounds]
        assert keys == [(f"{UPDATE}:{late_row}", "done")] + [
            (f"{UPDATE}:{late_row}:r{k}", "done") for k in range(2, rounds + 1)]
        assert sql.execute("SELECT state, outcome FROM telegram_updates WHERE id = %s", (late_row,)).fetchone() == (
            "pending", None), "a entrada atrasada foi abandonada"
        assert _submissions(world, _delivery_row(world, opened)) == []
        assert _world_events(world, "task.failed") == []
        close_rounds = _world_rounds(world, CLOSE, {"dispatch_row_id": opened.row})
        assert len(close_rounds) >= 2 and all(state == "done" for _key, state in close_rounds), close_rounds

        sql.execute("UPDATE tasks SET state = 'pending', lease_owner = NULL, lease_until = NULL, available_at = %s "
                    "WHERE id = %s", (clock.now(), phantom_task))
        deadline = time.monotonic() + 3 * WAIT
        while _delivery_row(world, opened)["state"] != "answered_late":
            assert time.monotonic() < deadline, "a entrada atrasada não virou answered_late"
            window = time.monotonic() + 1.5
            while time.monotonic() < window and _delivery_row(world, opened)["state"] != "answered_late":
                time.sleep(0.05)
            if _delivery_row(world, opened)["state"] != "answered_late":
                clock.advance(RECHECK)
        _settle(world)
    finally:
        bot_worker_thread.stop()
        engine.close()

    assert _dispatch_state(world, opened) == "closed"
    assert sql.execute("SELECT state, outcome FROM telegram_updates WHERE id = %s", (late_row,)).fetchone() == (
        "processed", "answer_recorded")
    assert sql.execute("SELECT state FROM telegram_updates WHERE id = %s", (phantom,)).fetchone()[0] == "processed"
    assert all(state == "done" for _key, state in _world_rounds(world, UPDATE, subject))
    assert sql.execute("SELECT count(*) FROM telegram_updates WHERE outcome = 'abandoned'").fetchone()[0] == 0
    delivery = _delivery_row(world, opened)
    (sub,) = _submissions(world, delivery)
    assert (sub["outcome"], sub["valid"], sub["late"], sub["received_at"]) == ("graded", True, True, late_received)
    assert (delivery["state"], delivery["answered_at"]) == ("answered_late", late_received)
    order = [e[0] for e in _world_events(world, "dispatch.closed", "submission.graded")
             if e[2]["dispatch_id"] == opened.dispatch_id]
    assert order == ["dispatch.closed", "submission.graded"]
    assert _world_events(world, "submission.graded") == [_graded_event(world, opened, delivery, sub, late=True)]
    assert _world_events(world, "task.failed") == []
