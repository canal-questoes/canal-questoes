"""Validação do disparo no worker: a tarefa `dispatch.validate`.
Cobre a questão suportada, os avisos da prévia, as rejeições, as repetições e as esperas, a recusa da
obtenção, o fim das rodadas e o cancelamento que vence.

Contratos do esquema do banco: `question_refs` (a inspeção gravada uma vez, título redigido), os disparos
(`dispatches`: transições, `validated_at`, `confirm_deadline`, `preview`, `rejection`,
`rejection_reasons`, `cancel_reason`), a chave de continuação (`dispatch.validate:<id>:c<task_id>`),
a validação (os três passos, um passo externo por execução, o passo sai do estado gravado; as classes de
rejeição; as tentativas da fila de tarefas e a classe na última; `validation_overran`; o cancelamento que vence;
`EngineUnavailable("unconfigured")` e `FetchTemporaryFailure("repos_unavailable")`), a prévia e
os eventos (`dispatch.validated`, `dispatch.rejected`, `dispatch.cancelled`); o contrato da API (o `Dispatch`
e as mensagens pareadas da rejeição); as interfaces do servidor que os testes usam (tratador
`validate_task_handler(conninfo, *, clock, settings, engine, fetcher, channel)`,
`dispatches.reject_validation_exhausted`, `TelegramChannel(…, delivery=…)`, `formatter.plain`).

Arnês: o disparo é preparado pela API (`dispatches.prepare`, `tests/helpers/api.py`) num oferecimento
ativo com bot; a validação roda pelo tratador real, montado como o `run_worker` monta (o
`HttpQuestionEngine` sobre o motor falso estrito `engine_fake`, a obtenção falsa `repo_fetcher` e o canal
com rota `TelegramChannel(…, delivery=…)` sobre a fixture `bots`, com um `ConsoleChannel` em memória
como `default`), e é processada pela fila real de tarefas (`TaskQueue.work_once`, uma tarefa por
vez), com o `settings` da fila para as tentativas (`max_attempts = 5`, espera de 60 s dobrando até 300 s):
assim as repetições, as esperas e a posse esgotada são as da fila real, sem worker em thread. A única outra
tarefa da fila é a `bot.sync_profile` do registro do bot, concluída sem efeito. Horários pelo
`FakeClock`; ids internos, `seq`, seeds e pseudônimos nunca como literal. O alvo é montado por SQL
(matrícula `student` `active` e endereço `delivery_endpoints`, como `test_outbox_routes.py`), porque o
vínculo do aluno pelo Telegram exige o worker de recepção.

Sentinelas: o gabarito do motor falso (`ANSWER_SENTINEL`) nunca aparece na API, nos eventos, nos logs nem
em tabela nenhuma (a validação não guarda a variante), com controle positivo na variante emitida; o
`technical_detail` do motor (`TECHNICAL_DETAIL_SENTINEL`) nunca aparece, com controle positivo na
resposta crua do motor falso; a URL do repositório nunca aparece em evento, log nem `last_error`.

Escolhas deste arquivo [I] (inferências deste arquivo sobre o que a documentação não fixa):
- `rejection.details` é a lista das `message` dos motivos do motor (`reasons[].message` do "não
  suportada"; `reason.message` do "quebrada"), na ordem do motor; as classes sem motivo do motor saem com
  `details == []` (a regra: `details` são os motivos do motor);
- as mensagens dos avisos do núcleo (`empty_target`, `too_many_options`, `repeated_question`) não têm
  texto fixado: o teste confere o `kind`, que a mensagem é texto não vazio e que não
  traz detalhe técnico; as do motor são conferidas exatas. A ordem dos avisos não é fixada: comparada
  ordenada pelo `kind`; onde o teste compara a lista inteira sem ordenar, `no_teacher_contact` vem por
  último (ele se acrescenta aos outros);
- o aviso `no_teacher_contact` (mensagens livres opcionais) tem texto fixo e é
  conferido exato onde o teste compara a mensagem: todo oferecimento deste arquivo fica no padrão (mensagens
  livres desligadas e sem contato do professor), e por isso toda prévia validada traz o aviso;
- `target_count` fora do `scheduled` não é fixado (o contrato da API fala do agendado e do aberto): nos
  estados `rejected` e `cancelled`, o corpo é comparado inteiro menos esse campo;
- `repeated_question`: o disparo anterior da mesma referência no mesmo oferecimento está `scheduled`
  (validado); outro oferecimento com a mesma referência não gera o aviso;
- a questão acima dos limites: o enunciado de 40 000 caracteres passa do limite do Rich (32 768 bytes
  UTF-8) e o de 5 000, do HTML simples (4 096 unidades UTF-16), mas cabe no Rich (controle positivo).

O cancelamento durante a
validação usa a rota `dispatches.cancel` e só fica verde depois que essa rota existir.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.events import EventType
from cq_server.ports.repos import (
    REFUSED_RULES,
    FetchNotFound,
    FetchRefused,
    FetchTemporaryFailure,
)
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    cancel_dispatch,
    create_offering,
    get_dispatch,
    marker,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.clock import iso
from tests.helpers.db import scan_all_tables
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    DYNAMIC_UNSUPPORTED_REASON,
    MEDIA_ERROR_WARNING,
    MISSING_MEDIA_WARNING,
    TECHNICAL_DETAIL_SENTINEL,
    UNSUPPORTED_REASON,
    VARIANT_BROKEN,
    FakeQuestion,
    FakeWarning,
    canonical,
    number_field,
    preset,
    question_content_hash,
)
from tests.helpers.engine import repo_key as engine_repo_key
from tests.helpers.logs import capture_logs
from tests.helpers.repos import FIXTURES

# --------------------------------------------------------------------------------------------------
# Constantes (entradas do teste, não resultados)
# --------------------------------------------------------------------------------------------------

REPO = "https://repos.cq.test/turma/questoes-validacao.git"
REPO_HOST = "repos.cq.test"
COMMIT = "a1b2c3d4" * 5
QID = "number"
ZONE = "America/Sao_Paulo"
ORIGIN = "integration"
WORKER_ID = "worker-validacao"
VALIDATE = "dispatch.validate"
PROFILE = "bot.sync_profile"
# O nome do campo do PL, que nunca sai na prévia.
FIELD_NAME = "campo_pl_interno"
STATEMENT_SENTENCE = "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?"
NUMBER_HINT = "Número; ex.: 4.25, -17 ou 3/7"

# As tentativas da fila de tarefas (até 5, espera crescente até 5 min): 60 · 2^(k−1), no máximo 300.
MAX_ATTEMPTS = 5
BACKOFF = [60.0, 120.0, 240.0, 300.0]
# o prazo de confirmação do imediato.
IMMEDIATE_CONFIRM = timedelta(hours=24)

# mensagens pareadas da rejeição.
REJECTION_MESSAGES = {
    "repository_unreachable": "Não foi possível obter o repositório da questão.",
    "repository_refused": "O repositório foi recusado pela política de obtenção.",
    "commit_not_found": "O commit informado não existe no repositório.",
    "question_not_found": "A questão não existe nesse commit.",
    "question_unsupported": "A questão usa recursos que o sistema não suporta.",
    "question_broken": "A questão falhou ao gerar uma variante.",
    "engine_unavailable": "O motor de questões ficou indisponível durante a validação.",
    "channel_limits_exceeded": "A questão não cabe nos limites do canal.",
    "internal_failure": "Falha interna na validação.",
}
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}
DISPATCH_EVENT_TYPES = ("dispatch.validated", "dispatch.rejected", "dispatch.cancelled")
# O aviso da prévia de um oferecimento com as mensagens livres
# desligadas e sem contato do professor, o padrão de todo oferecimento deste arquivo.
NO_TEACHER_CONTACT = "no_teacher_contact"
NO_TEACHER_CONTACT_MESSAGE = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                              "o aluno que escrever fora das questões não saberá a quem se dirigir.")
NO_TEACHER_CONTACT_WARNING = {"kind": NO_TEACHER_CONTACT, "message": NO_TEACHER_CONTACT_MESSAGE}
# O que nunca aparece num motivo, num evento, num log ou num `last_error`.
FORBIDDEN_IN_REASONS = ("Traceback", "server.py", "technical_detail", TECHNICAL_DETAIL_SENTINEL, "https://",
                        "http://", REPO_HOST, "/questions/", "cq-repos", ".staging")

_chats = itertools.count(880_000_001)
_codes = itertools.count(1)


def _question(**overrides: Any) -> FakeQuestion:
    """A questão numérica do preset `number`, com o campo `FIELD_NAME` (rótulo "Velocidade:", sufixo
    "m/s", a dica do motor para número)."""
    return preset("number", fields=(number_field(FIELD_NAME),), **overrides)


# --------------------------------------------------------------------------------------------------
# Contexto e arnês
# --------------------------------------------------------------------------------------------------


@dataclass
class _Ctx:
    client: Any
    sql: psycopg.Connection
    clock: Any
    settings: Any
    engine_fake: Any
    repo_fetcher: Any
    bots: Any
    bot_api: Any
    offering: dict
    offering_row: int
    bot_row: int
    runners: list[Any] = field(default_factory=list)

    @property
    def code(self) -> str:
        return self.offering["code"]

    @property
    def db_url(self) -> str:
        return self.settings.database_url


def _offering_with_bot(client: Any, sql: psycopg.Connection, bot_api: Any) -> tuple[dict, int, int]:
    """Um oferecimento `active` com bot registrado (pela API); devolve (corpo, `offerings.id`, `bots.id`)."""
    offering = create_offering(client, ADMIN_TOKEN, code=f"validacao-{next(_codes)}", timezone=ZONE)
    telegram_bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(telegram_bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                               (offering["offering_id"],)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    return offering, offering_row, bot_row


@pytest.fixture
def ctx(client: Any, sql: psycopg.Connection, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        bots: Any, bot_api: Any) -> Iterator[_Ctx]:
    """O oferecimento ativo com bot e o `settings` com as tentativas da fila (5, espera até 300 s)."""
    retry = dataclasses.replace(settings, max_attempts=MAX_ATTEMPTS, backoff_base_seconds=60,
                                backoff_max_seconds=300)
    offering, offering_row, bot_row = _offering_with_bot(client, sql, bot_api)
    context = _Ctx(client=client, sql=sql, clock=clock, settings=retry, engine_fake=engine_fake,
                   repo_fetcher=repo_fetcher, bots=bots, bot_api=bot_api, offering=offering,
                   offering_row=offering_row, bot_row=bot_row)
    try:
        yield context
    finally:
        for runner in reversed(context.runners):
            runner.close()


_DEFAULT: Any = object()


class _Runner:
    """O tratador `dispatch.validate` montado como o `run_worker` monta e a fila real de tarefas.

    `step()` processa a próxima `dispatch.validate` disponível (pulando e concluindo sem efeito a
    `bot.sync_profile` do registro do bot) e devolve o registro dela depois do processamento; `drain()`
    processa até não restar nenhuma, seguindo as esperas das repetições pelo `FakeClock`.
    """

    def __init__(self, ctx: _Ctx, *, delivery: Any = None, engine: Any = _DEFAULT, fetcher: Any = _DEFAULT,
                 channel: Any = _DEFAULT) -> None:
        from cq_server.adapters.console import ConsoleChannel
        from cq_server.adapters.engine import HttpQuestionEngine
        from cq_server.bots import TelegramChannel
        from cq_server.config import DeliverySettings, EngineSettings
        from cq_server.dispatches import validate_task_handler
        from cq_server.tasks import TaskQueue

        self.ctx = ctx
        self.others: list[str] = []
        self._closers: list[Callable[[], None]] = []
        own_engine = engine is _DEFAULT
        own_channel = channel is _DEFAULT
        self.engine = (HttpQuestionEngine(EngineSettings(engine_url=ctx.engine_fake.base_url, timeout_seconds=10.0))
                       if own_engine else engine)
        self.channel = (TelegramChannel(ctx.db_url, clock=ctx.clock, runtime=ctx.bots,
                                        default=ConsoleChannel(io.StringIO()),
                                        delivery=delivery or DeliverySettings())
                        if own_channel else channel)
        self.handler = validate_task_handler(ctx.db_url, clock=ctx.clock, settings=ctx.settings,
                                             engine=self.engine,
                                             fetcher=ctx.repo_fetcher if fetcher is _DEFAULT else fetcher,
                                             channel=self.channel)
        self.queue = TaskQueue(ctx.db_url, clock=ctx.clock, settings=ctx.settings)
        # Só depois de tudo montado (no esqueleto, o tratador lança antes e nada fica para fechar).
        self._closers = [self.queue.close, self.handler.close]
        if own_channel:
            self._closers.append(self.channel.close)
        if own_engine:
            self._closers.append(self.engine.close)
        self._handlers = {VALIDATE: self.handler, PROFILE: self._skip}
        ctx.runners.append(self)

    def _skip(self, task: Any) -> None:
        self.others.append(task.task_type)

    def step(self) -> Any:
        while True:
            task_id = self.queue.work_once(WORKER_ID, self._handlers)
            if task_id is None:
                return None
            record = self.queue.get(task_id)
            if record.task_type == VALIDATE:
                return record
            assert record.task_type == PROFILE, f"tarefa inesperada na fila: {record.task_type}"

    def drain(self, *, limit: int = 30) -> list[Any]:
        done: list[Any] = []
        for _ in range(limit):
            record = self.step()
            if record is not None:
                done.append(record)
                continue
            waiting = self.ctx.sql.execute(
                "SELECT min(available_at) FROM tasks WHERE task_type = %s AND state = 'pending'",
                (VALIDATE,)).fetchone()[0]
            if waiting is None:
                return done
            self.ctx.clock.set(max(waiting, self.ctx.clock.now()))
        raise AssertionError(f"a validação não terminou em {limit} execuções")

    def close(self) -> None:
        for close in reversed(self._closers):
            close()
        self._closers = []


@pytest.fixture
def validation(ctx: _Ctx) -> Callable[..., _Runner]:
    """Fábrica do `_Runner` (montado no corpo do teste; fechado no fim pela fixture `ctx`)."""
    return lambda **kwargs: _Runner(ctx, **kwargs)


# --------------------------------------------------------------------------------------------------
# Ajudantes
# --------------------------------------------------------------------------------------------------


def _program(ctx: _Ctx, question: FakeQuestion | str | None = None, *, qid: str = QID) -> None:
    ctx.engine_fake.program((REPO, COMMIT, qid), _question() if question is None else question)


def _prepare(ctx: _Ctx, *, qid: str = QID, opens_at: datetime | None = None, closes_at: datetime | None = None,
             code: str | None = None) -> dict:
    return prepare_dispatch(ctx.client, ADMIN_TOKEN, code or ctx.code, repo=REPO, commit=COMMIT, qid=qid,
                            opens_at=None if opens_at is None else iso(opens_at),
                            closes_at=None if closes_at is None else iso(closes_at), origin=ORIGIN)


def _get(ctx: _Ctx, prepared: dict, *, code: str | None = None) -> dict:
    return get_dispatch(ctx.client, ADMIN_TOKEN, code or ctx.code, prepared["dispatch_id"])


def _row(ctx: _Ctx, prepared: dict) -> dict:
    """A linha de `dispatches` (sem os `uuid`)."""
    cur = ctx.sql.execute(
        "SELECT id, state, validated_at, confirm_deadline, rejected_at, cancelled_at, rejection, "
        "rejection_reasons, cancel_reason, preview, question_ref_id FROM dispatches WHERE dispatch_uuid = %s",
        (prepared["dispatch_id"],))
    names = [c.name for c in cur.description]
    row = cur.fetchone()
    assert row is not None, "disparo preparado ausente de dispatches"
    return dict(zip(names, row))


def _ref_row(ctx: _Ctx) -> dict:
    cur = ctx.sql.execute(
        "SELECT id, repo, repo_sha256, commit_sha, qid, supported, show_correct_answer, single_variant, "
        "partial_credit, title, answers_names, content_hash, inspected_at FROM question_refs "
        "WHERE commit_sha = %s AND qid = %s", (COMMIT, QID))
    names = [c.name for c in cur.description]
    rows = cur.fetchall()
    assert len(rows) == 1, f"a referência devia ter uma linha, tem {len(rows)}"
    return dict(zip(names, rows[0]))


@dataclass(frozen=True)
class _Task:
    id: int
    key: str | None
    state: str
    attempts: int
    available_at: datetime
    last_error: str | None


def _tasks(ctx: _Ctx, dispatch_row: int) -> list[_Task]:
    rows = ctx.sql.execute(
        "SELECT id, idempotency_key, state, attempts, available_at, last_error FROM tasks "
        "WHERE task_type = %s AND payload @> %s::jsonb ORDER BY id",
        (VALIDATE, json.dumps({"dispatch_row_id": dispatch_row}))).fetchall()
    return [_Task(*row) for row in rows]


def _ops(ctx: _Ctx) -> list[str]:
    """As chamadas externas ao motor (inspeção e variante), na ordem; a saúde fica fora."""
    return [c.op for c in ctx.engine_fake.calls() if c.op in ("inspect", "create_variant")]


def _fetches(ctx: _Ctx) -> list[tuple[str, bool | None]]:
    return [(c.outcome, c.reused) for c in ctx.repo_fetcher.calls(REPO, COMMIT)]


def _events(ctx: _Ctx, prepared: dict) -> list[tuple[str, str | None, str, dict]]:
    """Os eventos de validação, rejeição e cancelamento do disparo: (tipo, ator, horário, dados)."""
    items = all_events(ctx.client, ADMIN_TOKEN, *DISPATCH_EVENT_TYPES)
    return [(e["type"], e["actor_token_id"], e["occurred_at"], e["data"]) for e in items
            if e["data"]["dispatch_id"] == prepared["dispatch_id"]]


def _link_student(ctx: _Ctx, *, enrollment: str = "active", endpoint: str | None = "active",
                  offering_row: int | None = None, bot_row: int | None = None) -> None:
    """Uma pessoa com matrícula `student` e, se `endpoint`, um endereço do Telegram no bot (SQL, como
    `test_outbox_routes.py`; o vínculo do aluno pelo Telegram exige o worker de recepção)."""
    body = register_person(ctx.client, ADMIN_TOKEN, name=f"Aluno Alvo Validacao {next(_codes)}")
    person_row = ctx.sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (body["pseudo_id"],)).fetchone()[0]
    now = ctx.clock.now()
    active = enrollment == "active"
    ctx.sql.execute(
        "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, terms_version, "
        "terms_accepted_at) VALUES (%s, %s, 'student', %s, %s, %s, %s, %s)",
        (person_row, offering_row or ctx.offering_row, enrollment, now, now if active else None,
         1 if active else None, now if active else None))
    if endpoint is not None:
        ctx.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, %s, %s, %s)",
            (person_row, bot_row or ctx.bot_row, next(_chats), endpoint, now, now))


def _expected(ctx: _Ctx, prepared: dict, *, created: datetime, qid: str = QID, opens_at: datetime | None = None,
              closes_at: datetime | None = None, **changes: Any) -> dict:
    """O `Dispatch` completo esperado, a partir do estado `validating`."""
    body = {
        "offering": {"offering_id": ctx.offering["offering_id"], "code": ctx.code},
        "timezone": ZONE,
        "dispatch_id": prepared["dispatch_id"],
        "question": {"question_ref_id": prepared["question"]["question_ref_id"], "repo": REPO, "commit": COMMIT,
                     "qid": qid},
        "state": "validating", "confirmed": False, "origin": ORIGIN,
        "opens_at": None if opens_at is None else iso(opens_at),
        "closes_at": None if closes_at is None else iso(closes_at),
        "created_at": iso(created), "validated_at": None, "confirmed_at": None, "rejected_at": None,
        "opened_at": None, "closed_at": None, "cancelled_at": None, "rejection": None, "cancel_reason": None,
        "target_count": None, "deliveries": dict(ZERO_COUNTS), "preview": None,
    }
    body.update(changes)
    return body


def _without_target(body: dict) -> dict:
    """O corpo sem `target_count` (não fixado fora do `scheduled`; ver o docstring do módulo)."""
    return {k: v for k, v in body.items() if k != "target_count"}


def _assert_core_warning(warning: dict) -> None:
    message = warning["message"]
    assert isinstance(message, str) and message.strip(), f"aviso {warning['kind']} sem mensagem"
    for forbidden in FORBIDDEN_IN_REASONS + (ANSWER_SENTINEL, FIELD_NAME):
        assert forbidden not in message, f"aviso {warning['kind']} com {forbidden!r}"


def _assert_nothing_leaks(ctx: _Ctx, *texts: str) -> None:
    """Nenhum evento, `last_error` nem texto dado traz a URL, o detalhe técnico ou o gabarito."""
    events = json.dumps(all_events(ctx.client, ADMIN_TOKEN, *(t.value for t in EventType)), ensure_ascii=False)
    errors = json.dumps([r[0] for r in ctx.sql.execute("SELECT last_error FROM tasks").fetchall()],
                        ensure_ascii=False)
    for where, text in (("eventos", events), ("tasks.last_error", errors),
                        *((f"texto {i}", t) for i, t in enumerate(texts))):
        for forbidden in (REPO, REPO_HOST, TECHNICAL_DETAIL_SENTINEL, "Traceback", ANSWER_SENTINEL):
            assert forbidden not in text, f"{forbidden!r} em {where}"


def _hook(action: Callable[[], None], errors: list[BaseException]) -> Callable[[Any], bool]:
    """Predicado `when` do motor falso que executa `action` durante a chamada (e guarda a exceção)."""

    def when(_call: Any) -> bool:
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - o teste afirma a lista vazia
            errors.append(exc)
        return True

    return when


def _exhaust_lease(ctx: _Ctx, task_id: int) -> None:
    """Posse esgotada montada por SQL: a próxima posse da fila marca a tarefa `failed`."""
    ctx.sql.execute(
        "UPDATE tasks SET state = 'leased', attempts = %s, lease_owner = 'worker-perdido', lease_until = %s "
        "WHERE id = %s", (MAX_ATTEMPTS, ctx.clock.now() - timedelta(seconds=1), task_id))


# --------------------------------------------------------------------------------------------------
# Questão suportada: agendado, prévia, inspeção, eventos
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["scheduled", "immediate"])
def test_supported_question_becomes_scheduled_with_exact_preview_and_no_answer_key(
        ctx: _Ctx, validation: Callable[..., _Runner], mode: str) -> None:
    """A questão suportada leva o
    disparo a `scheduled` sem confirmação, com `validated_at`, `confirm_deadline` (agendado: `opens_at`;
    imediato: `validated_at + 24 h`), a prévia exata (título, campo com tipo, rótulo, sufixo e dica em
    texto, enunciado pelo `formatter.plain` do canal com rota, alvo no momento da validação, só com o aviso
    `no_teacher_contact` do oferecimento no padrão), sem o gabarito nem o nome do campo do PL;
    a inspeção gravada na referência e o `dispatch.validated` exato. O alvo conta só matrícula `student` `active` com endereço vigente
    (`active` ou `blocked`); o `target_count` da consulta é o alvo **agora**, e o da prévia não muda.

    Avisos à equipe: a amostra da prévia gravada na validação (`dispatch_preview_contents.ir`,
    a IR literal da variante, com o gabarito) é a única exceção nomeada
    da varredura do banco pela sentinela do gabarito, com contagem exata: uma linha, a do disparo validado, e
    a sentinela em nenhum outro lugar (controle positivo)."""
    _program(ctx)
    _link_student(ctx, endpoint="active")
    _link_student(ctx, endpoint="blocked")
    _link_student(ctx, endpoint="inactive")
    _link_student(ctx, enrollment="pending", endpoint="active")
    _link_student(ctx, endpoint=None)
    t0 = ctx.clock.now()
    opens = t0 + timedelta(hours=2) if mode == "scheduled" else None
    closes = t0 + timedelta(hours=3) if mode == "scheduled" else None
    prepared = _prepare(ctx, opens_at=opens, closes_at=closes)
    ctx.clock.advance(7)
    t_valid = ctx.clock.now()

    run = validation()
    with capture_logs() as logs:
        done = run.drain()

    assert [(t.state, t.attempts) for t in done] == [("done", 1)] * 3, "três execuções: obtenção, inspeção, variante"
    assert _ops(ctx) == ["inspect", "create_variant"]
    issued = ctx.engine_fake.issued_variants()
    assert len(issued) == 1 and (issued[0].repo, issued[0].commit, issued[0].qid) == (REPO, COMMIT, QID)
    assert ANSWER_SENTINEL in canonical(issued[0].snapshot), "controle positivo: a variante leva o gabarito"
    statement = run.channel.formatter.plain(issued[0].ir["statement"]["html"])
    assert STATEMENT_SENTENCE in statement and "<" not in statement

    preview = {"title": "Velocidade média", "statement": statement,
               "fields": [{"type": "number", "label": "Velocidade:", "suffix": "m/s", "hint": NUMBER_HINT}],
               "target_count": 2, "warnings": [NO_TEACHER_CONTACT_WARNING]}
    body = _get(ctx, prepared)
    assert body == _expected(ctx, prepared, created=t0, opens_at=opens, closes_at=closes, state="scheduled",
                             validated_at=iso(t_valid), target_count=2, preview=preview)
    dumped = json.dumps(body, ensure_ascii=False)
    assert ANSWER_SENTINEL not in dumped and FIELD_NAME not in dumped

    row = _row(ctx, prepared)
    deadline = opens if mode == "scheduled" else t_valid + IMMEDIATE_CONFIRM
    assert (row["state"], row["validated_at"], row["confirm_deadline"], row["rejection"], row["cancel_reason"]) == (
        "scheduled", t_valid, deadline, None, None)
    ref = _ref_row(ctx)
    assert {k: v for k, v in ref.items() if k != "id"} == {
        "repo": REPO, "repo_sha256": engine_repo_key(REPO), "commit_sha": COMMIT,
        "qid": QID, "supported": True, "show_correct_answer": True, "single_variant": False,
        "partial_credit": True, "title": "Velocidade média", "answers_names": [FIELD_NAME],
        "content_hash": question_content_hash(FIXTURES / "course" / "questions" / QID), "inspected_at": t_valid}
    assert row["question_ref_id"] == ref["id"]

    assert _events(ctx, prepared) == [
        ("dispatch.validated", None, iso(t_valid),
         {"offering_id": ctx.offering["offering_id"], "dispatch_id": prepared["dispatch_id"], "target_count": 2,
          "warnings": [NO_TEACHER_CONTACT]})]
    found = [(t, c) for t, c, text in scan_all_tables(ctx.db_url) if ANSWER_SENTINEL in text]
    # Avisos à equipe: só a exceção nomeada `("dispatch_preview_contents", "ir")`, exatamente uma
    # vez (a amostra deste disparo), com controle positivo; sem a gravação da amostra, o teste fica vermelho.
    assert found == [("dispatch_preview_contents", "ir")], f"gabarito gravado no banco fora da amostra: {found}"
    _assert_nothing_leaks(ctx, logs.text)

    # O alvo da consulta é o de agora; o da prévia fica o do momento da validação.
    _link_student(ctx, endpoint="active")
    later = _get(ctx, prepared)
    assert (later["target_count"], later["preview"]["target_count"]) == (3, 2)


def test_validation_runs_one_external_step_per_execution_and_resumes_from_state(
        ctx: _Ctx, validation: Callable[..., _Runner]) -> None:
    """Uma chamada externa por
    execução, com a continuação `dispatch.validate:<id>:c<task_id>` enfileirada para agora na mesma
    transação; a execução seguinte começa pela obtenção, que acha a árvore (`reused`) e segue; a inspeção
    é pulada quando a referência já foi inspecionada. Uma rodada nova com outra chave (a da varredura,
    `:r1`), depois da posse esgotada da continuação, retoma pelo **estado** e não pela chave."""
    _program(ctx)
    t0 = ctx.clock.now()
    prepared = _prepare(ctx)
    run = validation()
    dispatch_row = _row(ctx, prepared)["id"]
    first = _tasks(ctx, dispatch_row)
    assert [(t.state, t.attempts) for t in first] == [("pending", 0)], "o preparo enfileira uma validação"

    # Execução 1: só a obtenção (sem rede no motor).
    ctx.clock.advance(3)
    t1 = run.step()
    assert (t1.id, t1.state, t1.attempts) == (first[0].id, "done", 1)
    assert _fetches(ctx) == [("Fetched", False)] and _ops(ctx) == []
    tasks = _tasks(ctx, dispatch_row)
    assert [(t.key, t.state, t.available_at) for t in tasks[1:]] == [
        (f"{VALIDATE}:{dispatch_row}:c{t1.id}", "pending", ctx.clock.now())]
    assert (_row(ctx, prepared)["state"], _ref_row(ctx)["inspected_at"]) == ("validating", None)

    # Execução 2: obtenção reaproveitada e a inspeção.
    ctx.clock.advance(3)
    t_inspect = ctx.clock.now()
    t2 = run.step()
    assert (t2.id, t2.state) == (tasks[1].id, "done")
    assert _fetches(ctx) == [("Fetched", False), ("Fetched", True)] and _ops(ctx) == ["inspect"]
    tasks = _tasks(ctx, dispatch_row)
    assert [(t.key, t.state) for t in tasks[2:]] == [(f"{VALIDATE}:{dispatch_row}:c{t2.id}", "pending")]
    assert (_row(ctx, prepared)["state"], _ref_row(ctx)["inspected_at"]) == ("validating", t_inspect)

    # A continuação se perde (posse esgotada, por SQL); a rodada `r1` retoma pelo estado: variante.
    _exhaust_lease(ctx, tasks[2].id)
    round_id = run.queue.enqueue(VALIDATE, {"dispatch_row_id": dispatch_row},
                                 idempotency_key=f"{VALIDATE}:{dispatch_row}:r1")
    ctx.clock.advance(3)
    t_valid = ctx.clock.now()
    t3 = run.step()
    assert (t3.id, t3.state) == (round_id, "done")
    assert run.queue.get(tasks[2].id).state == "failed"
    assert _fetches(ctx) == [("Fetched", False), ("Fetched", True), ("Fetched", True)]
    assert _ops(ctx) == ["inspect", "create_variant"], "a inspeção não se repete"
    assert [t.id for t in _tasks(ctx, dispatch_row)] == [first[0].id, tasks[1].id, tasks[2].id, round_id], (
        "o passo da variante é o último: nenhuma continuação")
    row = _row(ctx, prepared)
    assert (row["state"], row["validated_at"], row["confirm_deadline"]) == (
        "scheduled", t_valid, t_valid + IMMEDIATE_CONFIRM)
    assert _ref_row(ctx)["inspected_at"] == t_inspect
    assert run.step() is None
    assert _get(ctx, prepared)["created_at"] == iso(t0)


def test_inspection_is_recorded_once_per_reference_and_repeated_question_is_warned(
        ctx: _Ctx, validation: Callable[..., _Runner]) -> None:
    """a inspeção é gravada uma vez por `(repo, commit, qid)`,
    compartilhada entre disparos e oferecimentos (nenhuma segunda chamada `inspect`); o segundo disparo da
    mesma referência no mesmo oferecimento (permitido) tem o aviso `repeated_question`, o primeiro não; o
    disparo de outro oferecimento com a mesma referência não o tem. Sem aluno, `empty_target`. Os três
    oferecimentos estão no padrão (mensagens livres desligadas, sem contato): todos com `no_teacher_contact`
    ."""
    _program(ctx)
    run = validation()
    first = _prepare(ctx)
    run.drain()
    t_first = ctx.clock.now()
    ctx.clock.advance(60)
    second = _prepare(ctx)
    run.drain()
    other, other_row, other_bot = _offering_with_bot(ctx.client, ctx.sql, ctx.bot_api)
    _link_student(ctx, offering_row=other_row, bot_row=other_bot)
    third = _prepare(ctx, code=other["code"])
    run.drain()

    assert _ops(ctx) == ["inspect", "create_variant", "create_variant", "create_variant"]
    assert _ref_row(ctx)["inspected_at"] == t_first
    kinds = {name: sorted(w["kind"] for w in _get(ctx, p, code=c)["preview"]["warnings"])
             for name, p, c in (("first", first, ctx.code), ("second", second, ctx.code),
                                ("third", third, other["code"]))}
    assert kinds == {"first": ["empty_target", NO_TEACHER_CONTACT],
                     "second": ["empty_target", NO_TEACHER_CONTACT, "repeated_question"],
                     "third": [NO_TEACHER_CONTACT]}
    for warning in _get(ctx, second)["preview"]["warnings"]:
        _assert_core_warning(warning)
    assert [(e[0], sorted(e[3]["warnings"])) for e in _events(ctx, second)] == [
        ("dispatch.validated", ["empty_target", NO_TEACHER_CONTACT, "repeated_question"])]
    assert [(e[0], e[3]["warnings"], e[3]["target_count"]) for e in _events(ctx, third)] == [
        ("dispatch.validated", [NO_TEACHER_CONTACT], 1)]
    assert ctx.sql.execute("SELECT count(*) FROM question_refs").fetchone()[0] == 1


@pytest.mark.parametrize("name,expected", [
    ("missing_media", [{"kind": "missing_media", "message": MISSING_MEDIA_WARNING.message}]),
    ("media_error", [{"kind": "media_error", "message": MEDIA_ERROR_WARNING.message}]),
    ("console_output", [{"kind": "console_output", "message": "Saída de print no console da questão."}]),
])
def test_engine_warnings_reach_the_preview_and_the_event(ctx: _Ctx, validation: Callable[..., _Runner],
                                                         name: str, expected: list[dict]) -> None:
    """os avisos do motor (`missing_media`, `media_error`,
    `console_output`) saem na prévia com a mensagem do motor e no `dispatch.validated` pelo `kind`; o
    disparo continua agendado (mídia ausente não rejeita). O oferecimento no padrão acrescenta, por último,
    `no_teacher_contact`."""
    if name == "console_output":
        qid = QID
        _program(ctx, _question(variant_warnings=(FakeWarning("console_output", expected[0]["message"],
                                                              "generate"),)))
    else:
        qid = name
        _program(ctx, name, qid=name)
    _link_student(ctx)
    prepared = _prepare(ctx, qid=qid)
    validation().drain()

    body = _get(ctx, prepared)
    assert (body["state"], body["preview"]["warnings"], body["preview"]["target_count"]) == (
        "scheduled", [*expected, NO_TEACHER_CONTACT_WARNING], 1)
    assert [(e[0], e[3]["warnings"]) for e in _events(ctx, prepared)] == [
        ("dispatch.validated", [expected[0]["kind"], NO_TEACHER_CONTACT])]


@pytest.mark.parametrize("max_buttons,expected", [
    (2, ["empty_target", NO_TEACHER_CONTACT, "too_many_options"]),
    (5, ["empty_target", NO_TEACHER_CONTACT, "too_many_options"]),
    (6, ["empty_target", NO_TEACHER_CONTACT]),
    (12, ["empty_target", NO_TEACHER_CONTACT])])
def test_too_many_options_for_the_channel_buttons_is_a_preview_warning(
        ctx: _Ctx, validation: Callable[..., _Runner], max_buttons: int, expected: list[str]) -> None:
    """a escolha única com 4
    alternativas (mais o "Enviar" e o "Não sei", alternativas + 2) acima de `max_buttons` do canal com
    rota degrada e a prévia avisa `too_many_options` (com 5 botões, os 6 controles não cabem); dentro do
    limite (6 e 12), sem esse aviso (controle positivo). A prévia traz o campo de escolha sem rótulo nem
    sufixo e com a dica do motor, sem a alternativa correta marcada. O oferecimento no padrão tem também
    `no_teacher_contact`."""
    from cq_server.config import DeliverySettings

    ctx.engine_fake.program((REPO, COMMIT, "single_choice"), "single_choice")
    prepared = _prepare(ctx, qid="single_choice")
    validation(delivery=DeliverySettings(max_buttons=max_buttons)).drain()

    body = _get(ctx, prepared)
    assert body["state"] == "scheduled"
    assert body["preview"]["fields"] == [
        {"type": "single_choice", "label": None, "suffix": None, "hint": "Escolha uma alternativa"}]
    assert sorted(w["kind"] for w in body["preview"]["warnings"]) == expected
    for warning in body["preview"]["warnings"]:
        _assert_core_warning(warning)
    assert [sorted(e[3]["warnings"]) for e in _events(ctx, prepared)] == [expected]
    assert ANSWER_SENTINEL not in json.dumps(body, ensure_ascii=False)


def test_preview_and_inspection_title_are_redacted_on_write_and_on_output(
        ctx: _Ctx, validation: Callable[..., _Runner]) -> None:
    """o texto de terceiros com a identidade de
    uma pessoa cadastrada (título, enunciado, rótulo, dica e aviso do motor) sai redigido na prévia
    (`[pessoa <apelido>]`) e é gravado redigido em `question_refs.title` e em `dispatches.preview`."""
    name = "Doralice Previa Redigida"
    person = register_person(ctx.client, ADMIN_TOKEN, name=name)
    mark = marker(person["alias"])
    _program(ctx, preset("canary", identity=name))
    prepared = _prepare(ctx)
    run = validation()
    run.drain()

    statement = run.channel.formatter.plain(ctx.engine_fake.issued_variants()[0].ir["statement"]["html"])
    assert name in statement, "controle positivo: o enunciado renderizado traz o nome"
    preview = _get(ctx, prepared)["preview"]
    warnings = sorted(preview["warnings"], key=lambda w: w["kind"])
    assert (preview["title"], preview["statement"], preview["fields"], preview["target_count"]) == (
        f"Questão de {mark}", statement.replace(name, mark),
        [{"type": "number", "label": f"{mark}:", "suffix": "m/s", "hint": mark}], 0)
    assert [w["kind"] for w in warnings] == ["console_output", "empty_target", NO_TEACHER_CONTACT]
    assert warnings[0]["message"] == mark
    stored = ctx.sql.execute("SELECT r.title, d.preview::text FROM question_refs r JOIN dispatches d "
                             "ON d.question_ref_id = r.id").fetchall()
    assert len(stored) == 1 and stored[0][0] == f"Questão de {mark}"
    assert name not in stored[0][1] and mark in stored[0][1]


# --------------------------------------------------------------------------------------------------
# Rejeições
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rejection:
    reason: str
    details: tuple[str, ...]
    fetches: int
    ops: tuple[str, ...]
    setup: Callable[[_Ctx], str]  # programa o caso e devolve o QID


def _fetch_result(result: Any) -> Callable[[_Ctx], str]:
    def setup(ctx: _Ctx) -> str:
        _program(ctx)
        ctx.repo_fetcher.program(REPO, COMMIT, result)
        return QID
    return setup


def _engine_preset(name: str) -> Callable[[_Ctx], str]:
    def setup(ctx: _Ctx) -> str:
        _program(ctx, name, qid=name)
        return name
    return setup


def _engine_error(op: str, error: str) -> Callable[[_Ctx], str]:
    def setup(ctx: _Ctx) -> str:
        _program(ctx)
        ctx.engine_fake.fail(op, error=error, times=None)
        return QID
    return setup


def _missing_question(ctx: _Ctx) -> str:
    return "questao_que_nao_existe"


REJECTIONS = {
    "repositorio-inexistente": _Rejection("repository_unreachable", (), 1, (),
                                          _fetch_result(FetchNotFound("repository"))),
    "commit-inexistente": _Rejection("commit_not_found", (), 1, (), _fetch_result(FetchNotFound("commit"))),
    "questao-inexistente": _Rejection("question_not_found", (), 2, ("inspect",), _missing_question),
    "nao-suportada": _Rejection("question_unsupported", (UNSUPPORTED_REASON.message,), 2, ("inspect",),
                                _engine_preset("unsupported")),
    "nao-suportada-na-variante": _Rejection("question_unsupported", (DYNAMIC_UNSUPPORTED_REASON.message,), 3,
                                            ("inspect", "create_variant"), _engine_preset("dynamic_unsupported")),
    "quebrada": _Rejection("question_broken", (VARIANT_BROKEN.message,), 3, ("inspect", "create_variant"),
                           _engine_preset("broken_on_variant")),
    "motor-recusa-inspecao-400": _Rejection("internal_failure", (), 2, ("inspect",),
                                            _engine_error("inspect", "invalid_request")),
    "motor-recusa-variante-413": _Rejection("internal_failure", (), 3, ("inspect", "create_variant"),
                                            _engine_error("create_variant", "payload_too_large")),
}


@pytest.mark.parametrize("case", list(REJECTIONS), ids=list(REJECTIONS))
def test_each_rejection_class_has_exact_reason_message_details_and_event(
        ctx: _Ctx, validation: Callable[..., _Runner], case: str) -> None:
    """Repositório e commit inexistentes, questão
    inexistente no commit, não suportada (estática e na criação da variante, com os motivos do motor em
    `details`), variante quebrada (só a mensagem do motor; o `technical_detail` descartado) e o motor que
    recusa o pedido (400/413 → falha interna) levam o disparo a `rejected` com a classe e a mensagem
    pareada exatas, sem repetição (cada chamada externa uma vez) e com um `dispatch.rejected`. A tarefa
    termina `done`. Nenhum motivo, evento, log nem `last_error` traz traceback, caminho, URL ou o detalhe
    técnico; nenhuma prévia."""
    spec = REJECTIONS[case]
    qid = spec.setup(ctx)
    t0 = ctx.clock.now()
    prepared = _prepare(ctx, qid=qid)
    ctx.clock.advance(11)
    t_reject = ctx.clock.now()
    with capture_logs() as logs:
        done = validation().drain()

    assert [t.state for t in done] == ["done"] * spec.fetches
    assert (len(_fetches(ctx)), tuple(_ops(ctx))) == (spec.fetches, spec.ops)
    rejection = {"reason": spec.reason, "message": REJECTION_MESSAGES[spec.reason], "details": list(spec.details)}
    body = _get(ctx, prepared)
    assert _without_target(body) == _without_target(_expected(
        ctx, prepared, created=t0, qid=qid, state="rejected", rejected_at=iso(t_reject), rejection=rejection))
    row = _row(ctx, prepared)
    assert (row["state"], row["rejection"], row["validated_at"], row["confirm_deadline"], row["preview"]) == (
        "rejected", spec.reason, None, None, None)
    assert _events(ctx, prepared) == [
        ("dispatch.rejected", None, iso(t_reject),
         {"offering_id": ctx.offering["offering_id"], "dispatch_id": prepared["dispatch_id"],
          "rejection_reason": spec.reason})]
    reasons = json.dumps([body["rejection"], row["rejection_reasons"]], ensure_ascii=False)
    for forbidden in FORBIDDEN_IN_REASONS:
        assert forbidden not in reasons, f"{forbidden!r} no motivo"
    _assert_nothing_leaks(ctx, logs.text)
    if case == "quebrada":
        raw = ctx.engine_fake.calls("create_variant")[-1].response
        assert TECHNICAL_DETAIL_SENTINEL in raw["reason"]["technical_detail"], "controle positivo do descarte"


@pytest.mark.parametrize("rule", sorted(REFUSED_RULES))
def test_every_fetch_policy_refusal_rejects_as_repository_refused(
        ctx: _Ctx, validation: Callable[..., _Runner], rule: str) -> None:
    """Cada regra do `FetchRefused` (inclusive
    `submodule`, `redirect`, `address_not_global`, os tetos e os links) rejeita na primeira tentativa com
    `repository_refused`, sem repetir e sem chamar o motor; o `dispatch.rejected` não leva a regra nem a
    URL."""
    _program(ctx)
    t0 = ctx.clock.now()
    ctx.repo_fetcher.program(REPO, COMMIT, FetchRefused(rule))
    prepared = _prepare(ctx)
    done = validation().drain()

    assert [(t.state, t.attempts) for t in done] == [("done", 1)]
    assert (_fetches(ctx), _ops(ctx), ctx.engine_fake.calls()) == ([("FetchRefused", None)], [], [])
    body = _get(ctx, prepared)
    rejection = {"reason": "repository_refused", "message": REJECTION_MESSAGES["repository_refused"], "details": []}
    assert _without_target(body) == _without_target(_expected(
        ctx, prepared, created=t0, state="rejected", rejected_at=iso(t0), rejection=rejection))
    assert [(e[0], e[3]["rejection_reason"]) for e in _events(ctx, prepared)] == [
        ("dispatch.rejected", "repository_refused")]
    assert rule not in json.dumps(_events(ctx, prepared))
    _assert_nothing_leaks(ctx)


TEMPORARY = {
    "timeout": "repository_refused",
    "dns": "repository_unreachable",
    "connect": "repository_unreachable",
    "http_5xx": "repository_unreachable",
    "git_error": "repository_unreachable",
    "repos_unavailable": "repository_unreachable",
    "OSError": "repository_unreachable",
    "obtencao-desligada": "repository_unreachable",
}


@pytest.mark.parametrize("error_type", list(TEMPORARY))
def test_temporary_fetch_failure_repeats_with_task_backoff_and_rejects_on_the_last_attempt(
        ctx: _Ctx, validation: Callable[..., _Runner], error_type: str) -> None:
    """A obtenção com falha
    temporária repete como as tarefas da fila (5 tentativas, espera 60 → 120 → 240 → 300 s, até 5 min), com
    o disparo em `validating`; na última tentativa, o `timeout` esgotado rejeita com
    `repository_refused` (teto de tempo) e os demais temporários (inclusive a obtenção desligada,
    `fetcher=None` → `FetchTemporaryFailure("repos_unavailable")`) com `repository_unreachable`. A tarefa
    termina `done`; o motor não é chamado."""
    _program(ctx)
    t0 = ctx.clock.now()
    prepared = _prepare(ctx)
    if error_type == "obtencao-desligada":
        run = validation(fetcher=None)
    else:
        ctx.repo_fetcher.program(REPO, COMMIT, FetchTemporaryFailure(error_type))
        run = validation()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        record = run.step()
        assert record.attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert (record.state, _row(ctx, prepared)["state"]) == ("pending", "validating"), (
                f"tentativa {attempt} devia voltar à fila com o disparo em validação")
            assert record.available_at == ctx.clock.now() + timedelta(seconds=BACKOFF[attempt - 1])
            assert REPO not in (record.last_error or "") and REPO_HOST not in (record.last_error or "")
            ctx.clock.set(record.available_at)
    t_reject = ctx.clock.now()
    assert record.state == "done", "na última tentativa, o tratador rejeita em vez de falhar"
    assert run.step() is None
    assert len(_fetches(ctx)) == (0 if error_type == "obtencao-desligada" else MAX_ATTEMPTS)
    assert ctx.engine_fake.calls() == []
    reason = TEMPORARY[error_type]
    body = _get(ctx, prepared)
    assert _without_target(body) == _without_target(_expected(
        ctx, prepared, created=t0, state="rejected", rejected_at=iso(t_reject),
        rejection={"reason": reason, "message": REJECTION_MESSAGES[reason], "details": []}))
    assert _events(ctx, prepared) == [
        ("dispatch.rejected", None, iso(t_reject),
         {"offering_id": ctx.offering["offering_id"], "dispatch_id": prepared["dispatch_id"],
          "rejection_reason": reason})]
    _assert_nothing_leaks(ctx)


@pytest.mark.parametrize("op,how", [("inspect", "internal_error"), ("create_variant", "internal_error"),
                                    ("inspect", "stopped"), ("inspect", "unconfigured")])
def test_engine_unavailable_repeats_with_task_backoff_and_rejects_on_the_last_attempt(
        ctx: _Ctx, validation: Callable[..., _Runner], op: str, how: str) -> None:
    """O motor indisponível (500
    `internal_error`, conexão recusada ou motor não configurado → `EngineUnavailable("unconfigured")`)
    repete com as esperas da fila e, na 5ª tentativa, rejeita com `engine_unavailable`; a tarefa termina
    `done` e sai um `dispatch.rejected`."""
    _program(ctx)
    t0 = ctx.clock.now()
    prepared = _prepare(ctx)
    run = validation(engine=None) if how == "unconfigured" else validation()
    assert run.step().state == "done"  # a obtenção
    if op == "create_variant":
        assert run.step().state == "done"  # a inspeção
    if how == "internal_error":
        ctx.engine_fake.fail(op, error="internal_error", times=None)
    elif how == "stopped":
        ctx.engine_fake.stop()
    before = len(_ops(ctx))

    for attempt in range(1, MAX_ATTEMPTS + 1):
        record = run.step()
        assert record.attempts == attempt
        if attempt < MAX_ATTEMPTS:
            assert (record.state, _row(ctx, prepared)["state"]) == ("pending", "validating")
            assert record.available_at == ctx.clock.now() + timedelta(seconds=BACKOFF[attempt - 1])
            ctx.clock.set(record.available_at)
    t_reject = ctx.clock.now()
    assert record.state == "done"
    expected_calls = MAX_ATTEMPTS if how == "internal_error" else 0
    assert _ops(ctx)[before:] == [op] * expected_calls
    body = _get(ctx, prepared)
    assert _without_target(body) == _without_target(_expected(
        ctx, prepared, created=t0, state="rejected", rejected_at=iso(t_reject),
        rejection={"reason": "engine_unavailable", "message": REJECTION_MESSAGES["engine_unavailable"],
                   "details": []}))
    assert [(e[0], e[3]["rejection_reason"]) for e in _events(ctx, prepared)] == [
        ("dispatch.rejected", "engine_unavailable")]


@pytest.mark.parametrize("where", ["fetch", "inspect", "create_variant"])
def test_temporary_failure_followed_by_success_goes_on_to_scheduled(
        ctx: _Ctx, validation: Callable[..., _Runner], where: str) -> None:
    """Controle positivo das repetições: duas falhas temporárias seguidas de sucesso, na
    obtenção ou no motor, não rejeitam: a validação segue e o disparo fica `scheduled`, sem
    `dispatch.rejected`."""
    _program(ctx)
    prepared = _prepare(ctx)
    if where == "fetch":
        ctx.repo_fetcher.program(REPO, COMMIT, FetchTemporaryFailure("connect"), times=2)
    else:
        ctx.engine_fake.fail(where, error="internal_error", times=2)
    done = validation().drain()

    assert [t.state for t in done].count("pending") == 2
    assert _row(ctx, prepared)["state"] == "scheduled"
    assert [e[0] for e in _events(ctx, prepared)] == ["dispatch.validated"]
    expected_ops = {"fetch": ["inspect", "create_variant"], "inspect": ["inspect"] * 3 + ["create_variant"],
                    "create_variant": ["inspect"] + ["create_variant"] * 3}[where]
    assert _ops(ctx) == expected_ops


@pytest.mark.parametrize("telegram_format,length,expected", [
    ("rich", 40_000, "rejected"), ("html", 5_000, "rejected"), ("rich", 5_000, "scheduled")])
def test_statement_above_the_channel_limits_rejects_with_channel_limits_exceeded(
        ctx: _Ctx, validation: Callable[..., _Runner], telegram_format: str, length: int, expected: str) -> None:
    """O enunciado que não cabe no dialeto do canal
    com rota (Rich: 32 768 bytes UTF-8; HTML simples: 4 096 unidades UTF-16) rejeita com
    `channel_limits_exceeded`, sem dividir; o mesmo enunciado de 5 000 caracteres cabe no Rich (controle
    positivo)."""
    from cq_server.config import DeliverySettings

    _program(ctx, _question(statement="Texto longo do enunciado. " * (length // 26 + 1)))
    t0 = ctx.clock.now()
    prepared = _prepare(ctx)
    validation(delivery=DeliverySettings(telegram_format=telegram_format)).drain()

    assert _ops(ctx) == ["inspect", "create_variant"]
    body = _get(ctx, prepared)
    assert body["state"] == expected
    if expected == "rejected":
        assert _without_target(body) == _without_target(_expected(
            ctx, prepared, created=t0, state="rejected", rejected_at=iso(t0),
            rejection={"reason": "channel_limits_exceeded",
                       "message": REJECTION_MESSAGES["channel_limits_exceeded"], "details": []}))
        assert [(e[0], e[3]["rejection_reason"]) for e in _events(ctx, prepared)] == [
            ("dispatch.rejected", "channel_limits_exceeded")]
    else:
        assert [e[0] for e in _events(ctx, prepared)] == ["dispatch.validated"]


def test_exhausted_validation_rounds_reject_as_internal_failure_or_cancel_when_overran(
        ctx: _Ctx, validation: Callable[..., _Runner]) -> None:
    """Falha interna na validação: o canal sem rota
    (`channel=None`) na variante é `PermanentTaskError` e deixa a tarefa `failed` com o disparo ainda em
    `validating`; `reject_validation_exhausted` (o fim das rodadas pela varredura) leva o `validating` a
    `rejected/internal_failure` com `dispatch.rejected`, ou, no agendado com `now >= opens_at`, a
    `cancelled/validation_overran`; num disparo que já saiu de `validating`, devolve `False` sem efeito."""
    from cq_server.dispatches import reject_validation_exhausted

    _program(ctx)
    t0 = ctx.clock.now()
    immediate = _prepare(ctx)
    run = validation(channel=None)
    records = run.drain()
    assert [(t.state, t.last_error) for t in records][-1] == ("failed", "PermanentTaskError")
    assert _row(ctx, immediate)["state"] == "validating"
    assert _events(ctx, immediate) == []

    scheduled = _prepare(ctx, opens_at=t0 + timedelta(minutes=30), closes_at=t0 + timedelta(hours=2))
    ctx.clock.set(t0 + timedelta(minutes=30))
    t_end = ctx.clock.now()
    with psycopg.connect(ctx.db_url) as conn:
        assert reject_validation_exhausted(conn, _row(ctx, immediate)["id"], now=t_end) is True
    with psycopg.connect(ctx.db_url) as conn:
        assert reject_validation_exhausted(conn, _row(ctx, scheduled)["id"], now=t_end) is True
    with psycopg.connect(ctx.db_url) as conn:
        assert reject_validation_exhausted(conn, _row(ctx, immediate)["id"], now=t_end) is False

    body = _get(ctx, immediate)
    assert _without_target(body) == _without_target(_expected(
        ctx, immediate, created=t0, state="rejected", rejected_at=iso(t_end),
        rejection={"reason": "internal_failure", "message": REJECTION_MESSAGES["internal_failure"],
                   "details": []}))
    assert _events(ctx, immediate) == [
        ("dispatch.rejected", None, iso(t_end), {"offering_id": ctx.offering["offering_id"],
                                                  "dispatch_id": immediate["dispatch_id"],
                                                  "rejection_reason": "internal_failure"})]
    other = _get(ctx, scheduled)
    assert (other["state"], other["cancel_reason"], other["cancelled_at"], other["rejection"]) == (
        "cancelled", "validation_overran", iso(t_end), None)
    assert [(e[0], e[1], e[3]["cancel_reason"]) for e in _events(ctx, scheduled)] == [
        ("dispatch.cancelled", None, "validation_overran")]


# --------------------------------------------------------------------------------------------------
# A abertura que passa durante a validação e o cancelamento que vence
# --------------------------------------------------------------------------------------------------


def test_validation_that_finishes_at_or_after_the_scheduled_opening_cancels_as_validation_overran(
        ctx: _Ctx, validation: Callable[..., _Runner]) -> None:
    """O passo da variante começa antes de `opens_at`,
    mas termina com `now == opens_at` (o relógio anda durante a chamada ao motor): o disparo vai a
    `cancelled/validation_overran`, e não a `scheduled`, com `dispatch.cancelled` do worker e sem prévia
    nem `dispatch.validated`."""
    _program(ctx)
    t0 = ctx.clock.now()
    opens = t0 + timedelta(minutes=10)
    closes = t0 + timedelta(hours=1)
    prepared = _prepare(ctx, opens_at=opens, closes_at=closes)
    run = validation()
    assert run.step().state == "done" and run.step().state == "done"  # obtenção e inspeção
    ctx.clock.set(opens - timedelta(seconds=5))
    errors: list[BaseException] = []
    ctx.engine_fake.slow("create_variant", 0.0, when=_hook(lambda: ctx.clock.set(opens), errors))
    last = run.step()

    assert errors == [] and (last.state, _ops(ctx)) == ("done", ["inspect", "create_variant"])
    assert run.step() is None
    body = _get(ctx, prepared)
    assert _without_target(body) == _without_target(_expected(
        ctx, prepared, created=t0, opens_at=opens, closes_at=closes, state="cancelled",
        cancelled_at=iso(opens), cancel_reason="validation_overran"))
    row = _row(ctx, prepared)
    assert (row["validated_at"], row["confirm_deadline"], row["preview"]) == (None, None, None)
    assert _events(ctx, prepared) == [
        ("dispatch.cancelled", None, iso(opens), {"offering_id": ctx.offering["offering_id"],
                                                   "dispatch_id": prepared["dispatch_id"],
                                                   "cancel_reason": "validation_overran"})]


@pytest.mark.parametrize("before_step", [1, 2, 3])
def test_step_that_starts_at_or_after_the_opening_cancels_without_external_calls(
        ctx: _Ctx, validation: Callable[..., _Runner], before_step: int) -> None:
    """todo passo começa conferindo, no agendado, `now < opens_at`;
    uma execução que começa com o relógio já em `opens_at` (antes da obtenção, da inspeção ou da variante)
    leva o disparo a `cancelled/validation_overran` com o evento, **sem** chamar a obtenção nem o motor
    naquela execução."""
    _program(ctx)
    t0 = ctx.clock.now()
    opens = t0 + timedelta(minutes=10)
    closes = t0 + timedelta(hours=1)
    prepared = _prepare(ctx, opens_at=opens, closes_at=closes)
    run = validation()
    for _ in range(before_step - 1):
        assert run.step().state == "done"
    fetches, ops = len(_fetches(ctx)), list(_ops(ctx))
    ctx.clock.set(opens)
    last = run.step()

    assert last.state == "done"
    assert (len(_fetches(ctx)), _ops(ctx)) == (fetches, ops), "nenhuma chamada externa na execução"
    assert run.step() is None
    body = _get(ctx, prepared)
    assert (body["state"], body["cancel_reason"], body["cancelled_at"], body["validated_at"], body["preview"]) == (
        "cancelled", "validation_overran", iso(opens), None, None)
    assert _events(ctx, prepared) == [
        ("dispatch.cancelled", None, iso(opens), {"offering_id": ctx.offering["offering_id"],
                                                   "dispatch_id": prepared["dispatch_id"],
                                                   "cancel_reason": "validation_overran"})]


@pytest.mark.parametrize("moment", ["between-steps", "during-variant"])
def test_cancel_during_validation_wins_and_validation_does_not_change_the_state(
        ctx: _Ctx, validation: Callable[..., _Runner], moment: str) -> None:
    """O cancelamento durante a validação vence: o operador
    cancela o disparo `validating` entre dois passos, ou enquanto o motor cria a variante; a validação que
    termina depois não muda o estado (`cancelled/operator`), não grava prévia nem `validated_at`, não emite
    `dispatch.validated` e não enfileira continuação; entre passos, o passo seguinte nem chama o motor.
    Depende da rota `dispatches.cancel`."""
    _program(ctx)
    t0 = ctx.clock.now()
    prepared = _prepare(ctx)
    run = validation()
    assert run.step().state == "done"  # a obtenção
    ctx.clock.advance(5)
    t_cancel = ctx.clock.now()
    cancelled: list[dict] = []
    errors: list[BaseException] = []

    def cancel() -> None:
        cancelled.append(cancel_dispatch(ctx.client, ADMIN_TOKEN, ctx.code, prepared["dispatch_id"]))

    if moment == "between-steps":
        cancel()
    else:
        assert run.step().state == "done"  # a inspeção
        ctx.engine_fake.slow("create_variant", 0.0, when=_hook(cancel, errors))
    ops_before = list(_ops(ctx))
    last = run.step()

    assert errors == [] and len(cancelled) == 1
    assert last.state == "done"
    assert run.step() is None, "nenhuma continuação depois do cancelamento"
    if moment == "between-steps":
        assert _ops(ctx) == ops_before == [], "o passo seguinte não chama o motor"
    else:
        assert _ops(ctx) == ["inspect", "create_variant"]
    expected = _expected(ctx, prepared, created=t0, state="cancelled", cancelled_at=iso(t_cancel),
                         cancel_reason="operator")
    assert _without_target(cancelled[0]) == _without_target(expected)
    assert _without_target(_get(ctx, prepared)) == _without_target(expected)
    row = _row(ctx, prepared)
    assert (row["validated_at"], row["confirm_deadline"], row["preview"]) == (None, None, None)
    assert _events(ctx, prepared) == [
        ("dispatch.cancelled", "bootstrap", iso(t_cancel), {"offering_id": ctx.offering["offering_id"],
                                                            "dispatch_id": prepared["dispatch_id"],
                                                            "cancel_reason": "operator"})]
