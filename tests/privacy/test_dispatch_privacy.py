"""Privacidade do disparo e da resposta.


Contratos: em `question_refs`, `repo` e `qid` guardados **literais**, o `title` redigido na
gravação; `dispatches.preview` e `rejection_reasons` redigidos na gravação;
`free_messages.text` redigido na gravação; as mensagens livres; os lugares da identidade, do texto
do aluno e do texto de terceiros (a lista fechada mais `submissions.partial_scores`);
na API, `x-untrusted` com `x-free-text` na prévia, nos motivos e em `messages.list`;
`question.repo` e `question.qid` só `x-free-text`, redigidos só na saída; a referência com
`?private_token=…` é `repo invalid_format` e não aparece em saída, evento nem log; e no CLI.

Casos (cada um com controle positivo):
1. a prévia de uma questão canário (título, enunciado, rótulo, dica e aviso com a identidade de
   `DISPATCH_B`) e os motivos de rejeição do motor com a mesma identidade saem com `[pessoa <apelido>]` na
   API (`dispatches.get`, `dispatches.list`) e no CLI (`cq dispatches show`/`list`, texto e `--json`), e
   ficam redigidos no banco; o `technical_detail` do motor (`TECHNICAL_DETAIL_SENTINEL`) não aparece em
   lugar nenhum; na mesma função, a detecção cruzada dos alvos canário (cada alvo `disparo-*` não é
   achado com `ALL_PREFIXES` nem com `STUDENT_PREFIXES`, e cada alvo das fases anteriores não é achado com
   `DISPATCH_PREFIXES`);
2. a mensagem livre de `DISPATCH_A` com o nome, o RA e o e-mail de `DISPATCH_B` é gravada redigida e sai
   redigida em `messages.list` e em `cq messages list`; o `@username`, o nome e o id do Telegram de
   `DISPATCH_B` saem como vieram (exceção declarada, com controle positivo); o texto literal fica em
   `telegram_updates.text` (limitação conhecida);
3. a referência com `?private_token=<segredo registrado>` (e com o segredo como `userinfo`) é recusada com
   `repo invalid_format` pela API e pelo CLI, sem disparo, e o segredo não aparece em resposta, CLI, evento,
   log, tarefa nem coluna nenhuma;
4. a sentinela do gabarito (`ANSWER_SENTINEL` do motor falso) nunca aparece na API, no CLI, nos eventos,
   nos logs nem em chamada nenhuma à Bot API falsa antes do `dispatch.closed`, e aparece (controle
   positivo) na edição do fechamento; o texto digitado pelo aluno só aparece nas colunas da lista fechada
   (`STUDENT_TEXT_COLUMNS`; varredura de todas as colunas de todas as tabelas);
5. um disparo preparado (fica `validating`, sem worker) com o RA e a parte local do e-mail de `DISPATCH_B`
   no caminho do `repo` e no `qid` sai com `[pessoa <apelido>]` em `dispatches.get`, `dispatches.list` e
   `cq dispatches show`/`list`; a gravação guarda o valor literal (é a chave da obtenção; controle
   positivo), e nenhum evento, log nem tarefa o traz;
6. (mensagens livres opcionais) no modo desligado (o padrão),
   a mensagem livre de `DISPATCH_A` com o nome, o RA, o e-mail, o `@username` e o id do Telegram de
   `DISPATCH_B` não deixa rastro: `telegram_updates.text` mascarado, nenhuma linha em `free_messages`, nada
   em `outbox_messages`, eventos, logs, `messages.list` e `cq messages list`; controle positivo: o mesmo
   texto no modo ligado deixa o `@username` no update e em `messages.list`.

O caso 2 liga as mensagens livres no cadastro
(as mensagens livres são opcionais, e o padrão é desligado); a prévia do caso 1, de um oferecimento no padrão, traz o aviso
`no_teacher_contact`; no caso 4, a edição de fechamento de B (`no_answer`) não traz o
gabarito.

Arnês: oferecimento ativo com bot pela API de oferecimentos e bots; os alunos canário do disparo importados pela API do vínculo do aluno
(`students.import`, que grava a identidade usada pela redação) e vinculados por SQL (matrícula `active`
com o termo aceito e o endereço `active` no bot, como `test_free_messages.py`; o fluxo do `/start` e do
`/aceito` é do vínculo do aluno). A validação do caso 1 roda pelo tratador real (`validate_task_handler`, montado como
o `run_worker` monta, sobre o motor falso estrito e a obtenção falsa) e pela fila da API base; os casos 2 e 4
sobem o worker com bots (`bot_worker_thread`) sobre a Bot API falsa estendida (`rich_bot_api`). Horários
pelo `FakeClock`; ids internos, pseudônimos e apelidos nunca como literal (vêm do banco pelo RA).

Escolhas deste arquivo [I] (inferências deste arquivo sobre o que a documentação não fixa):
- a redação do `repo` e do `qid` na saída cobre, além do RA, a **parte local** do e-mail da pessoa
  (com o RA e a parte local do e-mail trocados por
  `[pessoa <apelido>]`); o `redaction.redact` da API base só troca nome, RA e e-mail inteiros, então a saída
  desses dois campos precisa da regra a mais;
- a saída em texto de `cq dispatches show` não tem formato fixado (traz estado, questão,
  horários, origem, motivo, contagens e a prévia recuada): o teste confere a ausência dos alvos e a
  presença da marca da redação, e compara o `--json` com a API por igualdade.

No esqueleto, toda função falha: a rota
`dispatches.prepare` (ou `dispatches.get`) ausente (404), o tratador `validate_task_handler` que lança
`NotImplementedError`, ou o desfecho `free_message` ainda não gravado (o vínculo do aluno grava `not_handled`). A
detecção cruzada dos alvos (comportamento do auxiliar, já real) fica na mesma função do caso 1.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
import re
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.scheduler import CLOSE_RECHECK_SECONDS
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_dispatches,
    list_free_messages,
    marker,
    prepare_dispatch,
    register_bot,
    update_offering_settings,
)
from tests.helpers.canary import (
    ALL_PREFIXES,
    CANARY_DISPATCH_STUDENTS,
    CANARY_PEOPLE,
    CANARY_STUDENTS,
    CANARY_TELEGRAM_USERS,
    DISPATCH_A,
    DISPATCH_B,
    DISPATCH_PREFIXES,
    DISPATCH_TARGETS,
    STUDENT_PREFIXES,
    STUDENT_TARGETS,
    TARGETS,
    TG_DISPATCH_A1,
    TG_DISPATCH_B,
    TG_TARGETS,
    CanaryStudent,
    find_canary,
    find_secrets,
)
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error
from tests.helpers.db import scan_all_tables
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    TECHNICAL_DETAIL_SENTINEL,
    VARIANT_BROKEN,
    FakeReason,
    canonical,
    preset,
)
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import remember_secret

ADMIN = auth(ADMIN_TOKEN)
ZONE = "America/Sao_Paulo"
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ORIGIN = "api"
WORKER_ID = "worker-privacidade-disparo"
VALIDATE = "dispatch.validate"
PROFILE = "bot.sync_profile"
PROCESS_TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, perfil) e as etapas da validação
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
CLOSE_TIMEOUT = 60.0  # prazo real do laço do fechamento
# O ritmo curto: a configuração inteira chega ao worker.
FAST = DeliverySettings(chat_interval_seconds=0.05)

# A questão dos disparos: um repositório no host de `TEST_REPO_SETTINGS` (a fixture `app`), um commit
# SHA-1 e os QIDs dos presets do motor falso, que existem na árvore `course` da obtenção falsa.
REPO = "https://repos.cq.test/disciplina/questoes-privacidade.git"
COMMIT = "b0a7" * 10

# a linha da edição de fechamento (a `no_answer` a recebe sem o gabarito, que só vai a quem respondeu).
CLOSED_NOTICE = "Janela fechada."
# O aviso da prévia com as mensagens livres desligadas e sem contato.
NO_TEACHER_CONTACT = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: o "
                      "aluno que escrever fora das questões não saberá a quem se dirigir.")
# Mensagens livres opcionais: a resposta do modo desligado sem contato e a marca do texto mascarado.
ANSWERS_ONLY_REPLY = "Este canal recebe só respostas às questões."
FREE_MESSAGE_MASK = "[mensagem livre]"

# A identidade canário de `DISPATCH_B` no texto de terceiros (título, enunciado, rótulo e aviso da questão
# canário; motivo do motor) e a forma redigida esperada: nome, RA e e-mail viram cada um a marca, e o que
# está entre eles fica (as três ocorrências não se encostam).
IDENTITY = f"{DISPATCH_B.name} (RA {DISPATCH_B.ra}, {DISPATCH_B.email})"
UNSUPPORTED_MESSAGE = f"A questão de {IDENTITY} usa o elemento pl-matrix-input, que o canal não suporta."

# mensagens pareadas da rejeição usadas aqui.
REJECTION_MESSAGES = {
    "question_unsupported": "A questão usa recursos que o sistema não suporta.",
    "question_broken": "A questão falhou ao gerar uma variante.",
}
# As colunas internas de identidade dos bots (o id do chat e do remetente), fora das varreduras de texto.
INTERNAL_COLUMNS = frozenset({("telegram_updates", "chat_id"), ("telegram_updates", "sender_id"),
                              ("delivery_endpoints", "chat_id")})
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}

# um token de acesso privado de formato plausível (registrado como segredo no uso).
PRIVATE_TOKEN = "glpat-" + "Vw3_Hn8-" * 3

# O texto digitado pelo aluno no caso 4: um marcador inconfundível que o teste procura em todas as
# colunas de todas as tabelas (não é identidade; a identidade de terceiros fica nos casos 1, 2 e 5).
STUDENT_TOKEN = "azul-celeste-digitado-QX42"
STUDENT_TEXT = f"{STUDENT_TOKEN} com a minha explicação"

# A lista fechada (com `submissions.partial_scores`): as únicas colunas
# em que o texto digitado pelo aluno pode ficar.
STUDENT_TEXT_COLUMNS = frozenset({
    ("telegram_updates", "text"),
    ("submissions", "raw_input"), ("submissions", "submitted_answers"), ("submissions", "format_errors"),
    ("submissions", "feedback"), ("submissions", "params"), ("submissions", "partial_scores"),
    ("delivery_contents", "parts"), ("delivery_contents", "current_render"),
})

# Os tipos de evento (da API base ao disparo), para ler o log inteiro.
EVENT_TYPES = (
    "person.registered", "token.created", "token.revoked", "identity.revealed", "identity.reveal_denied",
    "task.failed", "outbox.failed",
    "offering.created", "offering.state_changed", "offering.archive_prepared", "bot.registered",
    "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed",
    "student.list_imported", "student.invites_prepared", "student.invites_queued", "student.invite_sent",
    "student.invite_failed", "student.invite_cancelled", "student.invite_revoked", "student.joined",
    "dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
    "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
    "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know", "submission.graded",
    "submission.internal_failure",
)

_codes = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Student:
    """Um aluno canário do disparo importado: a linha, a conta do Telegram, o pseudônimo, o apelido e
    `people.id`."""

    canary: CanaryStudent
    user: Any
    pseudo_id: str
    alias: str
    person_id: int

    @property
    def chat_id(self) -> int:
        return self.user.user_id

    @property
    def mark(self) -> str:
        return marker(self.alias)


@dataclass
class _Ctx:
    client: Any
    sql: psycopg.Connection
    clock: Any
    offering: dict
    offering_row: int
    bot_row: int
    tg: int
    a: _Student
    b: _Student

    @property
    def code(self) -> str:
        return self.offering["code"]


def _setup(client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, *, linked: bool = True,
           free_messages: bool | None = None) -> _Ctx:
    """Oferecimento ativo com bot; `DISPATCH_A` e `DISPATCH_B` importados pela API (a identidade da
    redação) e, com `linked`, vinculados por SQL: A pela conta A1, B pela sua conta.

    Mensagens livres opcionais: `free_messages` vai ao cadastro só quando não é `None` (`None` omite o campo: o
    padrão, desligado; os casos que não mexem nas mensagens livres mandam o corpo sem o campo)."""
    extra = {} if free_messages is None else {"free_messages": free_messages}
    offering = create_offering(client, ADMIN_TOKEN, code=f"privacidade-disparo-{next(_codes)}", course=COURSE,
                               term=TERM, timezone=ZONE, **extra)
    tg = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(tg))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    imported = import_students(client, ADMIN_TOKEN, offering["code"], [s.row() for s in CANARY_DISPATCH_STUDENTS])
    assert (imported["people_created"], imported["enrollments_created"]) == (2, 2), imported
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                               (uuid.UUID(offering["offering_id"]),)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    students = []
    for canary, user in ((DISPATCH_A, TG_DISPATCH_A1), (DISPATCH_B, TG_DISPATCH_B)):
        person_id, pseudo_id, alias = sql.execute(
            "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
            "WHERE i.ra = %s", (canary.ra,)).fetchone()
        if linked:
            _link(sql, clock, offering_row=offering_row, bot_row=bot_row, person_id=person_id,
                  chat_id=user.user_id)
        students.append(_Student(canary, user, pseudo_id, alias, person_id))
    a, b = students
    return _Ctx(client, sql, clock, offering, offering_row, bot_row, tg, a, b)


def _link(sql: psycopg.Connection, clock: Any, *, offering_row: int, bot_row: int, person_id: int,
          chat_id: int) -> None:
    """O vínculo do aluno: matrícula `active` com o termo aceito e o endereço
    vigente `active` no bot do oferecimento."""
    now = clock.now()
    updated = sql.execute(
        "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
        "terms_accepted_at = %s WHERE person_id = %s AND offering_id = %s AND role = 'student'",
        (now, now, person_id, offering_row)).rowcount
    assert updated == 1
    sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_id, bot_row, chat_id, now, now))


def _redacted(ctx: _Ctx) -> str:
    """`IDENTITY` redigida: nome, RA e e-mail de B trocados cada um pela marca."""
    return f"{ctx.b.mark} (RA {ctx.b.mark}, {ctx.b.mark})"


def _engine(engine_fake: Any) -> HttpQuestionEngine:
    """O adaptador real do motor sobre o motor falso (uma conexão por chamada)."""
    return HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))


def _record(client: Any, sink: list[tuple[str, str, int, str, str]]) -> None:
    """Grava (método, caminho, status, cabeçalhos, corpo) de toda resposta que passa pelo cliente HTTP
    do teste (API e CLI em processo)."""

    def hook(response: Any) -> None:
        response.read()
        request = response.request
        headers = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items())
        sink.append((request.method, request.url.path, response.status_code, headers, response.text))

    hooks = client.event_hooks
    client.event_hooks = {"request": list(hooks["request"]), "response": [*hooks["response"], hook]}


def _dispatch_hits(*texts: str) -> list[str]:
    return sorted({hit for text in texts for hit in find_canary(text, prefixes=DISPATCH_PREFIXES)})


def _events_text(client: Any) -> str:
    return json.dumps(all_events(client, ADMIN_TOKEN, *EVENT_TYPES), ensure_ascii=False)


def _wait(predicate: Callable[[], Any], what: str, timeout: float = PROCESS_TIMEOUT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"prazo de {timeout} s esgotado: {what}"
        time.sleep(0.05)


class _Validation:
    """O tratador `dispatch.validate` montado como o `run_worker` monta (o `HttpQuestionEngine` sobre o
    motor falso, a obtenção falsa e o canal com rota `TelegramChannel(…, delivery=…)` sobre a fixture
    `bots`, com um `ConsoleChannel` em memória como `default`) e a fila real da API base."""

    def __init__(self, ctx: _Ctx, settings: Any, engine_fake: Any, repo_fetcher: Any, bots: Any) -> None:
        from cq_server.bots import TelegramChannel
        from cq_server.dispatches import validate_task_handler
        from cq_server.tasks import TaskQueue

        conninfo = settings.database_url
        self.ctx = ctx
        self._closers: list[Callable[[], None]] = []
        self.engine = _engine(engine_fake)
        self.channel = TelegramChannel(conninfo, clock=ctx.clock, runtime=bots,
                                       default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
        self.handler = validate_task_handler(conninfo, clock=ctx.clock, settings=settings, engine=self.engine,
                                             fetcher=repo_fetcher, channel=self.channel)
        self.queue = TaskQueue(conninfo, clock=ctx.clock, settings=settings)
        self._closers = [self.queue.close, self.handler.close, self.channel.close, self.engine.close]

    def drain(self, *, limit: int = 30) -> int:
        """Processa as `dispatch.validate` disponíveis (a `bot.sync_profile` do registro do bot é
        concluída sem efeito) até não restar nenhuma; devolve quantas validações rodaram."""
        handlers = {VALIDATE: self.handler, PROFILE: lambda task: None}
        ran = 0
        for _ in range(limit):
            task_id = self.queue.work_once(WORKER_ID, handlers)
            if task_id is None:
                return ran
            if self.queue.get(task_id).task_type == VALIDATE:
                ran += 1
        raise AssertionError(f"a validação não terminou em {limit} execuções")

    def close(self) -> None:
        for close in reversed(self._closers):
            close()
        self._closers = []


@pytest.fixture
def validation(settings: Any, engine_fake: Any, repo_fetcher: Any, bots: Any) -> Iterator[Callable[[_Ctx], _Validation]]:
    runners: list[_Validation] = []

    def make(ctx: _Ctx) -> _Validation:
        runner = _Validation(ctx, settings, engine_fake, repo_fetcher, bots)
        runners.append(runner)
        return runner

    yield make
    for runner in reversed(runners):
        runner.close()


def _start_worker(bot_worker_thread: Any, settings: Any, engine_fake: Any, repo_fetcher: Any, rich: Any,
                  ctx: _Ctx) -> Any:
    """O worker com bots, motor e obtenção (`run_worker(…, engine=, fetcher=, delivery=)`); espera a
    primeira consulta do bot."""
    thread = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID),
                               channel=ConsoleChannel(io.StringIO()), engine=_engine(engine_fake),
                               fetcher=repo_fetcher, delivery=FAST)
    rich.wait_for(lambda: rich.pending_polls(ctx.tg), timeout=PROCESS_TIMEOUT)
    return thread


def _update_row(sql: psycopg.Connection, tg: int, update_id: int) -> tuple | None:
    return sql.execute(
        "SELECT u.text, u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (tg, update_id)).fetchone()


def _processed(sql: psycopg.Connection, tg: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve (texto, estado, desfecho)."""
    assert update_id is not None, "o dublê descartou o update"
    return _wait(lambda: (row := _update_row(sql, tg, update_id)) is not None and row[1] == "processed" and row,
                 f"update {update_id} não processado")


def _settled(sql: psycopg.Connection) -> None:
    """Espera a fila sem linha `pending` nem `leased` e nenhuma tarefa viva."""
    _wait(lambda: sql.execute(
        "SELECT (SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')) + "
        "(SELECT count(*) FROM tasks WHERE state IN ('pending', 'leased'))").fetchone()[0] == 0,
          "a fila e as tarefas não assentaram")


def _prepare_validated(ctx: _Ctx, qid: str, *, closes_at: datetime) -> str:
    """Prepara um disparo imediato e espera o worker levá-lo a `scheduled`; devolve o `dispatch_id`."""
    prepared = prepare_dispatch(ctx.client, ADMIN_TOKEN, ctx.code, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(closes_at), origin=ORIGIN)
    assert prepared["state"] == "validating"
    dispatch_id = prepared["dispatch_id"]
    body = _wait(lambda: (b := get_dispatch(ctx.client, ADMIN_TOKEN, ctx.code, dispatch_id))["state"] != "validating"
                 and b, "a validação não terminou")
    assert (body["state"], body["confirmed"], body["rejection"]) == ("scheduled", False, None), body
    return dispatch_id


def _close(ctx: _Ctx, dispatch_id: str, closes_at: datetime) -> None:
    """O fechamento determinístico: com tudo assentado, o relógio passa de `closes_at` e, em
    laço com prazo real, espera o `dispatch.closed`, avançando `CLOSE_RECHECK_SECONDS` a cada espera
    sem fechamento; depois espera as edições da revelação saírem."""
    _settled(ctx.sql)
    ctx.clock.set(closes_at + timedelta(seconds=1))
    deadline = time.monotonic() + CLOSE_TIMEOUT
    while get_dispatch(ctx.client, ADMIN_TOKEN, ctx.code, dispatch_id)["state"] != "closed":
        assert time.monotonic() < deadline, "o disparo não fechou no prazo"
        wait_until = time.monotonic() + 2.0
        while time.monotonic() < wait_until:
            if get_dispatch(ctx.client, ADMIN_TOKEN, ctx.code, dispatch_id)["state"] == "closed":
                break
            time.sleep(0.1)
        else:
            ctx.clock.advance(CLOSE_RECHECK_SECONDS)
    _settled(ctx.sql)


# ---------------------------------------------------------------------------------------------
# 1. Prévia e motivos redigidos; `technical_detail` ausente; detecção cruzada dos alvos
# ---------------------------------------------------------------------------------------------


def test_canary_preview_and_engine_reasons_are_redacted_in_api_cli_and_database(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, engine_fake: Any, cli_invoke: Any,
        db_url: str, validation: Callable[[_Ctx], _Validation]) -> None:
    """A prévia e os motivos de rejeição com a identidade de um aluno
    canário, com controle positivo da redação; o texto de terceiros na prévia e nos motivos.


    Três disparos imediatos no mesmo oferecimento, validados pelo tratador real:
    - a questão canário (`preset("canary")` com `IDENTITY` = nome, RA e e-mail de `DISPATCH_B` no título,
      no enunciado, no rótulo e no aviso `console_output`, e o nome na dica) → `scheduled` com a prévia
      exata, cada ocorrência trocada por `[pessoa <apelido de B>]`;
    - a questão não suportada com a mesma identidade no motivo do motor → `rejected/question_unsupported`
      com `details` redigido;
    - a questão quebrada na variante (o motor manda `technical_detail`) → `rejected/question_broken` com
      só a mensagem do motor.
    Controle positivo: as respostas cruas do motor falso trazem a identidade e o detalhe técnico. A API
    (`dispatches.get`, `dispatches.list`), o CLI (`cq dispatches show`/`list`, texto e `--json`), os
    eventos, os logs e o banco (`question_refs.title`, `dispatches.preview`, `rejection_reasons`) não
    trazem alvo `disparo-*` nem o detalhe técnico; a marca da redação aparece onde a identidade estava.

    Na mesma função: a detecção cruzada dos alvos canário.

    Relatórios (exceção nomeada): a questão canário chega a `scheduled`, e a validação grava a amostra da
    prévia (`dispatch_preview_contents.ir`, a IR literal da variante, sem redação, origem da prévia renderizada
    ao professor); na varredura do banco, essa coluna é a única exceção nomeada, com controle positivo e contagem
    exata: exatamente os rótulos da identidade da questão canário (extensão da exceção do conteúdo da questão na prévia)."""
    ctx = _setup(client, sql, clock, bot_api)
    mark, redacted = ctx.b.mark, _redacted(ctx)
    engine_fake.program((REPO, COMMIT, "canary"), preset("canary", identity=IDENTITY, hint=DISPATCH_B.name))
    reason = FakeReason("unsupported_elements", UNSUPPORTED_MESSAGE, ("pl-matrix-input",))
    engine_fake.program((REPO, COMMIT, "unsupported"), preset("unsupported", inspect_reasons=(reason,)))
    engine_fake.program((REPO, COMMIT, "broken_on_variant"), "broken_on_variant")
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=30)
    exchanges: list[tuple[str, str, int, str, str]] = []
    _record(client, exchanges)

    with capture_logs() as logs:
        prepared = {qid: prepare_dispatch(client, ADMIN_TOKEN, ctx.code, repo=REPO, commit=COMMIT, qid=qid,
                                          closes_at=iso(closes_at), origin=ORIGIN)
                    for qid in ("canary", "unsupported", "broken_on_variant")}
        run = validation(ctx)
        assert run.drain() >= 3
        canary_body = get_dispatch(client, ADMIN_TOKEN, ctx.code, prepared["canary"]["dispatch_id"])
        unsupported_body = get_dispatch(client, ADMIN_TOKEN, ctx.code, prepared["unsupported"]["dispatch_id"])
        broken_body = get_dispatch(client, ADMIN_TOKEN, ctx.code, prepared["broken_on_variant"]["dispatch_id"])
        listed = list_dispatches(client, ADMIN_TOKEN, ctx.code)
        cli = {
            "show": cli_invoke(["dispatches", "show", ctx.code, prepared["canary"]["dispatch_id"]]),
            "show_json": cli_invoke(["dispatches", "show", ctx.code, prepared["canary"]["dispatch_id"]], json=True),
            "show_rejected": cli_invoke(["dispatches", "show", ctx.code, prepared["unsupported"]["dispatch_id"]]),
            "list": cli_invoke(["dispatches", "list", ctx.code]),
            "list_json": cli_invoke(["dispatches", "list", ctx.code], json=True),
        }

    # Controle positivo: o motor falso mandou a identidade (prévia e motivo) e o detalhe técnico.
    identity_labels = {f"disparo-nome:{DISPATCH_B.name}", f"disparo-ra:{DISPATCH_B.ra}",
                       f"disparo-email:{DISPATCH_B.email}"}
    engine_out = json.dumps([c.response for c in engine_fake.calls() if c.op in ("inspect", "create_variant")],
                            ensure_ascii=False)
    assert identity_labels <= set(_dispatch_hits(engine_out)), "controle positivo: a identidade saiu do motor"
    raw_broken = [c.response for c in engine_fake.calls("create_variant")
                  if c.response and "reason" in c.response]
    assert raw_broken and TECHNICAL_DETAIL_SENTINEL in raw_broken[0]["reason"]["technical_detail"], (
        "controle positivo: o motor mandou o technical_detail")

    # A prévia exata (redigida) da questão canário.
    issued = [v for v in engine_fake.issued_variants() if v.qid == "canary"]
    assert len(issued) == 1
    statement = run.channel.formatter.plain(issued[0].ir["statement"]["html"])
    assert IDENTITY in statement, "controle positivo: o enunciado renderizado traz a identidade"
    assert canary_body["state"] == "scheduled"
    preview = canary_body["preview"]
    assert (preview["title"], preview["statement"], preview["fields"], preview["target_count"]) == (
        f"Questão de {redacted}", statement.replace(IDENTITY, redacted),
        [{"type": "number", "label": f"{redacted}:", "suffix": "m/s", "hint": mark}], 2)
    # O oferecimento fica no padrão (mensagens livres
    # desligadas e sem contato), e `no_teacher_contact` entra por último nos avisos.
    assert preview["warnings"] == [{"kind": "console_output", "message": redacted},
                                   {"kind": "no_teacher_contact", "message": NO_TEACHER_CONTACT}]
    assert ANSWER_SENTINEL not in canonical(canary_body)

    # Os motivos de rejeição: o do motor redigido; o da questão quebrada só com a mensagem do motor.
    assert (unsupported_body["state"], unsupported_body["rejection"]) == ("rejected", {
        "reason": "question_unsupported", "message": REJECTION_MESSAGES["question_unsupported"],
        "details": [UNSUPPORTED_MESSAGE.replace(IDENTITY, redacted)]})
    assert (broken_body["state"], broken_body["rejection"]) == ("rejected", {
        "reason": "question_broken", "message": REJECTION_MESSAGES["question_broken"],
        "details": [VARIANT_BROKEN.message]})
    summary = {item["dispatch_id"]: item for item in listed["items"]}
    assert summary[prepared["unsupported"]["dispatch_id"]]["rejection"] == unsupported_body["rejection"]
    assert "preview" not in summary[prepared["canary"]["dispatch_id"]]

    # Nenhuma resposta da API (corpo e cabeçalhos), saída do CLI, evento ou log com alvo, gabarito ou
    # detalhe técnico.
    for method, path, status, headers, body in exchanges:
        assert _dispatch_hits(headers, body) == [], (method, path, status, _dispatch_hits(headers, body))
        assert TECHNICAL_DETAIL_SENTINEL not in body and ANSWER_SENTINEL not in body, (method, path)
    assert mark in json.dumps(canary_body, ensure_ascii=False) and mark in json.dumps(unsupported_body)
    for name, result in cli.items():
        assert result.exit_code == 0, (name, result.stderr)
        assert _dispatch_hits(result.stdout, result.stderr) == [], (name, _dispatch_hits(result.stdout, result.stderr))
        assert TECHNICAL_DETAIL_SENTINEL not in result.output and ANSWER_SENTINEL not in result.output, name
    assert json.loads(cli["show_json"].stdout) == canary_body
    assert json.loads(cli["list_json"].stdout) == listed
    assert mark in cli["show"].stdout, "controle positivo: a prévia redigida aparece no CLI"
    assert mark in cli["show_rejected"].stdout, "controle positivo: o motivo redigido aparece no CLI"
    events = _events_text(client)
    assert _dispatch_hits(events) == [] and TECHNICAL_DETAIL_SENTINEL not in events and ANSWER_SENTINEL not in events
    assert _dispatch_hits(logs.text) == [] and TECHNICAL_DETAIL_SENTINEL not in logs.text
    assert ANSWER_SENTINEL not in logs.text

    # O banco: título, prévia e motivos gravados redigidos; o detalhe técnico em lugar nenhum.
    title = sql.execute("SELECT title FROM question_refs WHERE qid = 'canary'").fetchone()[0]
    assert title == f"Questão de {redacted}"
    stored = sql.execute("SELECT preview::text, rejection_reasons::text FROM dispatches d JOIN offerings o "
                         "ON o.id = d.offering_id WHERE o.code = %s ORDER BY d.id", (ctx.code,)).fetchall()
    assert len(stored) == 3
    assert _dispatch_hits(*(text for row in stored for text in row if text)) == []
    assert mark in (stored[0][0] or "") and mark in (stored[1][1] or "")
    leaks = [(t, c) for t, c, text in scan_all_tables(db_url) if TECHNICAL_DETAIL_SENTINEL in text]
    assert leaks == [], f"technical_detail gravado: {leaks}"
    outside = {(t, c): hits for t, c, text in scan_all_tables(db_url)
               if t != "identities" and (t, c) not in INTERNAL_COLUMNS and (hits := _dispatch_hits(text))}
    # Relatórios: só a exceção nomeada `("dispatch_preview_contents", "ir")` (a amostra da
    # única questão que chega a `scheduled`, a canário), com exatamente os rótulos da identidade dela (controle
    # positivo e contagem exata); nenhum outro alvo `disparo-*` fora de identities e das colunas do chat.
    canary_labels = _dispatch_hits(IDENTITY, DISPATCH_B.name)
    assert identity_labels <= set(canary_labels), "controle positivo: a identidade da questão canário"
    assert outside == {("dispatch_preview_contents", "ir"): canary_labels}, (
        f"alvo disparo-* no banco fora de identities, das colunas do chat e da amostra da prévia: {outside}")

    # Detecção cruzada: os alvos do disparo e os das fases anteriores não se acham uns aos outros.
    for student in CANARY_DISPATCH_STUDENTS:
        texts = [student.name, student.ra, student.email,
                 *(f"{acc.full_name} @{acc.username} {acc.user_id}" for acc in student.accounts)]
        for text in texts:
            assert find_canary(text, prefixes=ALL_PREFIXES) == [], text
            assert find_canary(text, prefixes=STUDENT_PREFIXES) == [], text
            assert find_canary(text) == [], text
            assert find_canary(text, prefixes=DISPATCH_PREFIXES), f"controle positivo: {text}"
    older = [p.name for p in CANARY_PEOPLE] + [p.ra for p in CANARY_PEOPLE if p.ra] + [
        p.email for p in CANARY_PEOPLE if p.email]
    older += [f"{u.full_name} @{u.username} {u.user_id}" for u in CANARY_TELEGRAM_USERS]
    older += [text for s in CANARY_STUDENTS for text in (s.name, s.ra, s.email)]
    older += [f"{u.full_name} @{u.username} {u.user_id}" for s in CANARY_STUDENTS for u in s.accounts]
    for text in older:
        assert find_canary(text, prefixes=DISPATCH_PREFIXES) == [], text
    assert {label for label, _ in DISPATCH_TARGETS}.isdisjoint(
        {label for label, _ in TARGETS + TG_TARGETS + STUDENT_TARGETS})


# ---------------------------------------------------------------------------------------------
# 2. Mensagens livres redigidas
# ---------------------------------------------------------------------------------------------


def test_free_message_with_another_students_identity_is_redacted_on_write_and_on_output(
        client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, cli_invoke: Any, db_url: str) -> None:
    """Controle positivo da redação das mensagens livres (texto livre com redação automática, gravada redigida;
    as identidades do Telegram nas mensagens livres).

    `DISPATCH_A`, vinculado e sem disparo, manda ao bot um texto com o nome, o RA e o e-mail de
    `DISPATCH_B` (os dois importados na lista) e o `@username`, o nome e o id do Telegram de B:
    - o update fica `processed` com o desfecho `free_message`, e `telegram_updates.text` guarda o texto
      literal (limitação conhecida: controle positivo da identidade gravada ali);
    - `free_messages.text` guarda o texto com o nome, o RA e o e-mail trocados por `[pessoa <apelido>]`;
      o `@username`, o nome e o id do Telegram ficam (o sistema não os conhece);
    - `messages.list` e `cq messages list` (texto e `--json`) devolvem esse texto: nenhum alvo de lista
      de B (nome, palavras, RA, e-mail), e os do Telegram de B presentes (exceção declarada, com
      controle positivo);
    - nenhum evento, log nem outra coluna traz alvo `disparo-*`.

    Mensagens livres opcionais: o caso é do modo **ligado** (a limitação conhecida só vale
    nele), com as mensagens livres ligadas no cadastro; o modo desligado está em
    `test_off_mode_free_message_with_identity_leaves_no_trace`."""
    ctx = _setup(client, sql, clock, rich_bot_api, free_messages=True)
    a, b = ctx.a, ctx.b
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    clock.advance(5)
    t1 = clock.now()
    text = (f"A {DISPATCH_B.name} (RA {DISPATCH_B.ra}, e-mail {DISPATCH_B.email}) pediu ajuda; "
            f"no Telegram é @{TG_DISPATCH_B.username}, {TG_DISPATCH_B.full_name}, id {TG_DISPATCH_B.user_id}.")
    stored = (f"A {b.mark} (RA {b.mark}, e-mail {b.mark}) pediu ajuda; "
              f"no Telegram é @{TG_DISPATCH_B.username}, {TG_DISPATCH_B.full_name}, id {TG_DISPATCH_B.user_id}.")
    tg_labels = set(_dispatch_hits(f"@{TG_DISPATCH_B.username} {TG_DISPATCH_B.full_name} {TG_DISPATCH_B.user_id}"))
    list_labels = set(_dispatch_hits(f"{DISPATCH_B.name} {DISPATCH_B.ra} {DISPATCH_B.email}"))
    assert tg_labels and list_labels and tg_labels.isdisjoint(list_labels)
    assert set(_dispatch_hits(stored)) == tg_labels, "a forma redigida só guarda os alvos do Telegram de B"

    with capture_logs() as logs:
        update_id = rich_bot_api.push_text(ctx.tg, a.user, text)
        literal, _state, outcome = _processed(sql, ctx.tg, update_id)
        assert (literal, outcome) == (text, "free_message")
        _settled(sql)
        listed = list_free_messages(client, ADMIN_TOKEN, ctx.code)
        cli_text = cli_invoke(["messages", "list", ctx.code])
        cli_json = cli_invoke(["messages", "list", ctx.code], json=True)

    rows = sql.execute("SELECT f.id, f.text, f.replied, f.ambiguous, f.received_at FROM free_messages f "
                       "WHERE f.offering_id = %s ORDER BY f.id", (ctx.offering_row,)).fetchall()
    assert [r[1:] for r in rows] == [(stored, True, False, t1)]
    assert listed == {"offering": {"offering_id": ctx.offering["offering_id"], "code": ctx.code},
                      "items": [{"pseudo_id": a.pseudo_id, "alias": a.alias, "text": stored, "created_at": iso(t1)}],
                      "next_cursor": rows[0][0]}
    assert set(_dispatch_hits(json.dumps(listed, ensure_ascii=False))) == tg_labels, "só os alvos do Telegram"
    assert (cli_text.exit_code, cli_json.exit_code) == (0, 0), (cli_text.stderr, cli_json.stderr)
    assert json.loads(cli_json.stdout) == listed
    assert cli_text.stdout.splitlines() == [f"{iso(t1)}  {a.alias}  {stored}", f"Próximo cursor: {rows[0][0]}"]
    assert set(_dispatch_hits(cli_text.stdout)) == tg_labels and _dispatch_hits(cli_text.stderr) == []
    # Controle positivo da limitação conhecida: o texto literal, com a identidade, no update.
    assert list_labels | tg_labels <= set(_dispatch_hits(literal))

    assert _dispatch_hits(_events_text(client)) == []
    assert _dispatch_hits(logs.text) == []
    # O banco: `identities` (a lista), as colunas internas do chat (o id da conta), o texto literal do
    # update e o texto redigido da mensagem livre (só os alvos do Telegram de B); nada mais.
    found: dict[tuple[str, str], set[str]] = {}
    for table, column, value in scan_all_tables(db_url):
        if hits := _dispatch_hits(value):
            found.setdefault((table, column), set()).update(hits)
    outside = {key: hits for key, hits in found.items()
               if key[0] != "identities" and key not in INTERNAL_COLUMNS
               and key not in {("telegram_updates", "text"), ("free_messages", "text")}}
    assert outside == {}, f"alvo disparo-* no banco fora das exceções: {outside}"
    assert found.get(("free_messages", "text")) == tg_labels
    assert list_labels <= found.get(("telegram_updates", "text"), set())


# ---------------------------------------------------------------------------------------------
# 3. `?private_token=` recusado e ausente de toda saída
# ---------------------------------------------------------------------------------------------


def test_repo_with_private_token_is_refused_without_echo_anywhere(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, cli_invoke: Any, db_url: str) -> None:
    """A regra da API (uma referência com `?private_token=…` é `repo invalid_format` e não
    aparece em nenhuma saída, evento ou log) e a do CLI.

    O segredo (registrado por `remember_secret`) vai na consulta do `repo` e, noutro pedido, como
    `userinfo`: a API responde 400 `invalid_request` com exatamente `repo invalid_format` (sem eco), o CLI
    sai com código 1 e a mesma mensagem, nenhum disparo, referência, tarefa nem evento de disparo é
    criado, e o segredo não aparece em resposta (corpo e cabeçalhos), saída do CLI, evento, log nem coluna
    nenhuma do banco. Controle positivo: o detector acha o segredo no próprio pedido."""
    ctx = _setup(client, sql, clock, bot_api, linked=False)
    remember_secret(PRIVATE_TOKEN)
    repos = (f"{REPO}?private_token={PRIVATE_TOKEN}", f"https://oauth2:{PRIVATE_TOKEN}@repos.cq.test/turma/q.git")
    for repo in repos:
        assert find_secrets(json.dumps({"repo": repo}), [PRIVATE_TOKEN]), "controle positivo do detector"
    exchanges: list[tuple[str, str, int, str, str]] = []
    _record(client, exchanges)
    tasks_before = sql.execute("SELECT count(*) FROM tasks").fetchone()[0]

    with capture_logs() as logs:
        responses = [client.post(f"/v1/offerings/{ctx.code}/dispatches", headers=ADMIN,
                                 json={"repo": repo, "commit": COMMIT, "qid": "number"}) for repo in repos]
        runs = [cli_invoke(["dispatches", "prepare", ctx.code, "--repo", repo, "--commit", COMMIT, "--qid", "number"],
                           json=json_mode) for repo in repos for json_mode in (False, True)]

    for resp in responses:
        assert_error(resp, 400, "invalid_request", fields=[("repo", "invalid_format")])
    for index, result in enumerate(runs):
        assert (result.exit_code, result.stdout) == (1, ""), (index, result.stderr)
        if index % 2 == 0:
            assert result.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - repo: invalid_format"]
        else:
            assert json.loads(result.stderr) == {"error": {"code": "invalid_request",
                                                           "message": MESSAGES["invalid_request"],
                                                           "fields": [{"field": "repo", "reason": "invalid_format"}]}}
    assert sql.execute("SELECT count(*) FROM dispatches").fetchone()[0] == 0
    assert sql.execute("SELECT count(*) FROM question_refs").fetchone()[0] == 0
    assert sql.execute("SELECT count(*) FROM tasks").fetchone()[0] == tasks_before

    secrets = [PRIVATE_TOKEN]
    for method, path, status, headers, body in exchanges:
        assert find_secrets(headers, secrets) == [] and find_secrets(body, secrets) == [], (method, path, status)
    for result in runs:
        assert find_secrets(result.output, secrets) == [] and find_secrets(result.stderr, secrets) == []
    assert find_secrets(_events_text(client), secrets) == []
    assert find_secrets(logs.text, secrets) == []
    found = [(t, c) for t, c, text in scan_all_tables(db_url) if find_secrets(text, secrets)]
    assert found == [], f"segredo gravado: {found}"


# ---------------------------------------------------------------------------------------------
# 4. Sentinela do gabarito e texto do aluno
# ---------------------------------------------------------------------------------------------


def test_answer_sentinel_only_in_closing_edit_and_student_text_only_in_declared_columns(
        client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, cli_invoke: Any, db_url: str) -> None:
    """A sentinela do gabarito e o texto do aluno só nas
    colunas da lista fechada, mais `submissions.partial_scores`.


    Um disparo imediato da questão de texto (com explicação), para A e B vinculados; A responde por
    "responder" ao pedido do campo com um texto inconfundível; B não responde; o disparo fecha pelo
    roteiro determinístico:
    - antes do `dispatch.closed`, nenhuma chamada à Bot API falsa traz a sentinela (mensagem da questão,
      pedido de campo, edição do feedback); depois dele, a edição da revelação de A a traz (controle
      positivo: `can_show` e variante não única); a edição de fechamento de B, sem resposta, não:
      ela traz `CLOSED_NOTICE` ("Janela fechada.") e nenhuma sentinela;
    - a API (`dispatches.get`, `dispatches.list`), o CLI (`cq dispatches show`/`list`), os eventos e os
      logs não trazem a sentinela nem o texto do aluno;
    - na varredura de todas as colunas de todas as tabelas, o texto do aluno só aparece nas colunas de
      `STUDENT_TEXT_COLUMNS`, com controle positivo em `telegram_updates.text` e
      `submissions.raw_input`; nunca em `outbox_messages` (as mensagens de entrega não guardam texto)."""
    engine_fake.program((REPO, COMMIT, "text"), preset("text", explanation="O céu limpo é azul."))
    ctx = _setup(client, sql, clock, rich_bot_api)
    a, b = ctx.a, ctx.b
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    for student in (a, b):
        _processed(sql, ctx.tg, rich_bot_api.push_text(ctx.tg, student.user, "/start", bot_command=True))
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=10)
    exchanges: list[tuple[str, str, int, str, str]] = []
    _record(client, exchanges)

    with capture_logs() as logs:
        dispatch_id = _prepare_validated(ctx, "text", closes_at=closes_at)
        confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
        _wait(lambda: get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)["deliveries"]["delivered"] == 2,
              "as duas entregas não saíram")
        _settled(sql)
        prompt = _wait(lambda: [m for m in rich_bot_api.messages(ctx.tg, a.chat_id) if m.force_reply],
                       "o pedido do campo não saiu")[0]
        _, _, outcome = _processed(sql, ctx.tg, rich_bot_api.push_text(ctx.tg, a.user, STUDENT_TEXT,
                                                                        reply_to_message_id=prompt.message_id))
        assert outcome == "answer_recorded"
        _wait(lambda: get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)["deliveries"]["answered"] == 1,
              "a resposta não foi corrigida")
        _settled(sql)
        before_close = [c.seq for c in rich_bot_api.calls()]
        leaked = [(c.method, c.seq) for c in rich_bot_api.calls()
                  if ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False)]
        assert leaked == [], f"a sentinela do gabarito chegou ao aluno antes do fechamento: {leaked}"
        _close(ctx, dispatch_id, closes_at)
        body = get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
        listed = list_dispatches(client, ADMIN_TOKEN, ctx.code)
        runs = [cli_invoke(["dispatches", "show", ctx.code, dispatch_id]),
                cli_invoke(["dispatches", "show", ctx.code, dispatch_id], json=True),
                cli_invoke(["dispatches", "list", ctx.code]),
                cli_invoke(["dispatches", "list", ctx.code], json=True)]

    assert (body["state"], body["deliveries"]) == ("closed", {**ZERO_COUNTS, "answered": 1, "no_answer": 1})
    assert [(item["state"], item["deliveries"]) for item in listed["items"] if item["dispatch_id"] == dispatch_id] == [
        (body["state"], body["deliveries"])]
    assert json.loads(runs[1].stdout) == body and json.loads(runs[3].stdout) == listed
    # Controle positivo: a edição do fechamento de A traz o gabarito com a sentinela; a de B
    # (`no_answer`), não (o gabarito só vai a quem respondeu).
    last_seq = max(before_close)
    closing = {(e.chat_id, e.message_id) for e in rich_bot_api.edits(ctx.tg) if e.call_seq > last_seq
               and ANSWER_SENTINEL in (e.after.text or "") + json.dumps(e.after.rich_message or {}, ensure_ascii=False)}
    assert {chat for chat, _ in closing} == {a.chat_id}, closing
    # B recebe, depois do `dispatch.closed`, a edição de fechamento com `CLOSED_NOTICE` e sem a sentinela.
    b_closing = [(e.after.text or "") + json.dumps(e.after.rich_message or {}, ensure_ascii=False)
                 for e in rich_bot_api.edits(ctx.tg, b.chat_id) if e.call_seq > last_seq]
    assert any(CLOSED_NOTICE in text for text in b_closing), b_closing
    assert all(ANSWER_SENTINEL not in text for text in b_closing), b_closing

    for method, path, status, headers, text in exchanges:
        assert ANSWER_SENTINEL not in text and STUDENT_TOKEN not in text, (method, path, status)
    for result in runs:
        assert result.exit_code == 0, result.stderr
        assert ANSWER_SENTINEL not in result.output and STUDENT_TOKEN not in result.output
    events = _events_text(client)
    assert ANSWER_SENTINEL not in events and STUDENT_TOKEN not in events
    assert ANSWER_SENTINEL not in logs.text and STUDENT_TOKEN not in logs.text

    # O texto do aluno só nas colunas da lista fechada (varredura de todas as colunas de todas as tabelas).
    where = {(t, c) for t, c, text in scan_all_tables(db_url) if STUDENT_TOKEN in text}
    assert where - STUDENT_TEXT_COLUMNS == set(), f"texto do aluno fora da lista fechada de colunas: {where}"
    assert {("telegram_updates", "text"), ("submissions", "raw_input")} <= where, (
        f"controle positivo: o texto do aluno gravado onde a regra manda: {where}")
    assert not any(t == "outbox_messages" for t, _ in where)


# ---------------------------------------------------------------------------------------------
# 5. `repo` e `qid` com identidade: literais na gravação, redigidos na saída
# ---------------------------------------------------------------------------------------------


def test_repo_and_qid_with_identity_are_redacted_on_output_and_never_in_events_or_logs(
        client: Any, sql: psycopg.Connection, clock: Any, bot_api: Any, cli_invoke: Any, db_url: str) -> None:
    """A regra (`repo` e `qid` guardados literais: são a chave da
    obtenção e do motor; redigidos na saída; nunca em eventos, logs ou
    `last_error`); o contrato da API (`question.repo` e `question.qid` com `x-free-text`, redigidos só na
    saída) e o do CLI.

    Um disparo preparado (sem worker: fica `validating`) com o RA e a parte local do e-mail de
    `DISPATCH_B` no caminho do `repo` (`https://repos.cq.test/<RA>/<local>.git`, host permitido) e no
    `qid` (`<RA>/<local>`): a resposta do preparo, `dispatches.get`, `dispatches.list` e `cq dispatches
    show`/`list` (texto e `--json`) mostram `[pessoa <apelido de B>]` no lugar de cada um; a linha de
    `question_refs` guarda os valores literais (controle positivo); nenhum evento, log, tarefa nem outra
    coluna traz o RA ou a parte local."""
    ctx = _setup(client, sql, clock, bot_api, linked=False)
    mark = ctx.b.mark
    local = DISPATCH_B.email.split("@", 1)[0]
    repo = f"https://repos.cq.test/{DISPATCH_B.ra}/{local}.git"
    qid = f"{DISPATCH_B.ra}/{local}"
    labels = {f"disparo-ra:{DISPATCH_B.ra}", f"disparo-email-local:{DISPATCH_B.email}"}
    assert labels <= set(_dispatch_hits(repo)) and labels <= set(_dispatch_hits(qid)), "controle positivo do detector"
    exchanges: list[tuple[str, str, int, str, str]] = []
    _record(client, exchanges)
    t0 = clock.now()

    with capture_logs() as logs:
        prepared = prepare_dispatch(client, ADMIN_TOKEN, ctx.code, repo=repo, commit=COMMIT, qid=qid, origin=ORIGIN)
        body = get_dispatch(client, ADMIN_TOKEN, ctx.code, prepared["dispatch_id"])
        listed = list_dispatches(client, ADMIN_TOKEN, ctx.code)
        runs = {"show": cli_invoke(["dispatches", "show", ctx.code, prepared["dispatch_id"]]),
                "show_json": cli_invoke(["dispatches", "show", ctx.code, prepared["dispatch_id"]], json=True),
                "list": cli_invoke(["dispatches", "list", ctx.code]),
                "list_json": cli_invoke(["dispatches", "list", ctx.code], json=True)}

    question = {"question_ref_id": prepared["question"]["question_ref_id"],
                "repo": f"https://repos.cq.test/{mark}/{mark}.git", "commit": COMMIT, "qid": f"{mark}/{mark}"}
    assert prepared["question"] == question and body["question"] == question
    assert body == {
        "offering": {"offering_id": ctx.offering["offering_id"], "code": ctx.code}, "timezone": ZONE,
        "dispatch_id": prepared["dispatch_id"], "question": question, "state": "validating", "confirmed": False,
        "origin": ORIGIN, "opens_at": None, "closes_at": None, "created_at": iso(t0), "validated_at": None,
        "confirmed_at": None, "rejected_at": None, "opened_at": None, "closed_at": None, "cancelled_at": None,
        "rejection": None, "cancel_reason": None, "target_count": body["target_count"],
        "deliveries": dict(ZERO_COUNTS), "preview": None}
    assert [item["question"] for item in listed["items"]] == [question]
    for method, path, status, headers, text in exchanges:
        assert _dispatch_hits(headers, text) == [], (method, path, status, _dispatch_hits(headers, text))
    for name, result in runs.items():
        assert result.exit_code == 0, (name, result.stderr)
        assert _dispatch_hits(result.stdout, result.stderr) == [], (name, _dispatch_hits(result.stdout, result.stderr))
    assert json.loads(runs["show_json"].stdout) == body and json.loads(runs["list_json"].stdout) == listed
    assert mark in runs["show"].stdout and mark in runs["list"].stdout, "controle positivo: a marca no CLI"

    # Controle positivo: a gravação guarda os valores literais (a chave da obtenção).
    assert sql.execute("SELECT repo, qid FROM question_refs").fetchall() == [(repo, qid)]
    events = _events_text(client)
    assert _dispatch_hits(events) == [] and repo not in events and qid not in events
    assert _dispatch_hits(logs.text) == [] and repo not in logs.text
    tasks = json.dumps(sql.execute("SELECT task_type, payload, idempotency_key, last_error FROM tasks").fetchall(),
                       ensure_ascii=False, default=str)
    assert _dispatch_hits(tasks) == []
    outside = {(t, c): hits for t, c, text in scan_all_tables(db_url)
               if (hits := _dispatch_hits(text)) and t not in ("identities", "question_refs")}
    assert outside == {}, f"RA ou parte local fora de identities e question_refs: {outside}"
    assert {c for t, c, text in scan_all_tables(db_url) if t == "question_refs" and _dispatch_hits(text)} == {"repo", "qid"}
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", prepared["dispatch_id"])


# ---------------------------------------------------------------------------------------------
# 6. Modo desligado: a mensagem livre com identidade não deixa rastro
# ---------------------------------------------------------------------------------------------


def test_off_mode_free_message_with_identity_leaves_no_trace(
        client: Any, sql: psycopg.Connection, clock: Any, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, cli_invoke: Any, db_url: str) -> None:
    """Mensagens livres opcionais: desligadas, a mensagem não
    vira registro e o texto do update é mascarado na classificação; no modo desligado, a
    máscara é a asserção negativa, e o controle positivo é a mesma identidade presente onde ela deve
    estar; o texto mascarado deixa a coluna `telegram_updates.text`.


    Oferecimento no padrão (mensagens livres desligadas, sem contato). `DISPATCH_A`, vinculado e sem
    disparo, manda o texto com o nome, o RA e o e-mail de `DISPATCH_B` e o `@username`, o nome e o id do
    Telegram de B:
    - o update fica `processed` com `free_message_masked_replied`, `text = '[mensagem livre]'` e
      `command` nulo; a resposta "Este canal recebe só respostas às questões." ao chat de A;
    - nenhuma linha em `free_messages`; `messages.list` e `cq messages list` (texto e `--json`) vazios;
    - nenhum alvo `disparo-*` em `outbox_messages`, eventos, logs nem em coluna nenhuma fora de
      `identities` e das colunas internas do chat (varredura de todas as tabelas).
    Controle positivo: o texto enviado traz todos os alvos; depois, com as mensagens livres ligadas
    (`offerings.update_settings`), o mesmo texto deixa o literal no update e o `@username` em
    `messages.list`."""
    ctx = _setup(client, sql, clock, rich_bot_api)
    a, b = ctx.a, ctx.b
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    clock.advance(5)
    text = (f"A {DISPATCH_B.name} (RA {DISPATCH_B.ra}, e-mail {DISPATCH_B.email}) pediu ajuda; "
            f"no Telegram é @{TG_DISPATCH_B.username}, {TG_DISPATCH_B.full_name}, id {TG_DISPATCH_B.user_id}.")
    tg_labels = set(_dispatch_hits(f"@{TG_DISPATCH_B.username} {TG_DISPATCH_B.full_name} {TG_DISPATCH_B.user_id}"))
    list_labels = set(_dispatch_hits(f"{DISPATCH_B.name} {DISPATCH_B.ra} {DISPATCH_B.email}"))
    assert tg_labels and list_labels and (tg_labels | list_labels) <= set(_dispatch_hits(text)), (
        "controle positivo: o texto enviado traz os alvos de lista e do Telegram de B")

    with capture_logs() as logs:
        update_id = rich_bot_api.push_text(ctx.tg, a.user, text)
        masked, _state, outcome = _processed(sql, ctx.tg, update_id)
        assert (masked, outcome) == (FREE_MESSAGE_MASK, "free_message_masked_replied")
        _settled(sql)
        listed = list_free_messages(client, ADMIN_TOKEN, ctx.code)
        cli_text = cli_invoke(["messages", "list", ctx.code])
        cli_json = cli_invoke(["messages", "list", ctx.code], json=True)

    command = sql.execute("SELECT u.command FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                          "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (ctx.tg, update_id)).fetchone()[0]
    assert command is None
    assert sql.execute("SELECT count(*) FROM free_messages WHERE offering_id = %s",
                       (ctx.offering_row,)).fetchone()[0] == 0
    assert listed == {"offering": {"offering_id": ctx.offering["offering_id"], "code": ctx.code},
                      "items": [], "next_cursor": 0}
    assert (cli_text.exit_code, cli_json.exit_code) == (0, 0), (cli_text.stderr, cli_json.stderr)
    assert json.loads(cli_json.stdout) == listed
    assert cli_text.stdout.splitlines() == ["Próximo cursor: 0"]
    assert _dispatch_hits(cli_text.stdout, cli_text.stderr, cli_json.stdout, cli_json.stderr) == []
    outbox = sql.execute("SELECT text, priority FROM outbox_messages WHERE bot_id = %s ORDER BY id",
                         (ctx.bot_row,)).fetchall()
    assert outbox == [(ANSWERS_ONLY_REPLY, 1)]
    assert [(m.chat_id, m.text) for m in rich_bot_api.messages(ctx.tg)] == [(a.chat_id, ANSWERS_ONLY_REPLY)]
    assert _dispatch_hits(_events_text(client)) == []
    assert _dispatch_hits(logs.text) == []
    found: dict[tuple[str, str], set[str]] = {}
    for table, column, value in scan_all_tables(db_url):
        if hits := _dispatch_hits(value):
            found.setdefault((table, column), set()).update(hits)
    outside = {key: hits for key, hits in found.items() if key[0] != "identities" and key not in INTERNAL_COLUMNS}
    assert outside == {}, f"alvo disparo-* no banco fora de identities e das colunas do chat: {outside}"

    # Controle positivo: com as mensagens livres ligadas, o mesmo texto deixa o literal no update e
    # o `@username` de B em `messages.list`; o update do modo desligado continua mascarado.
    switched = update_offering_settings(client, ADMIN_TOKEN, ctx.code, free_messages=True)
    assert switched["free_messages"] is True
    clock.advance(5)
    on_id = rich_bot_api.push_text(ctx.tg, a.user, text)
    literal, _state, on_outcome = _processed(sql, ctx.tg, on_id)
    _settled(sql)
    assert (literal, on_outcome) == (text, "free_message")
    assert list_labels | tg_labels <= set(_dispatch_hits(literal))
    listed_on = list_free_messages(client, ADMIN_TOKEN, ctx.code)
    assert set(_dispatch_hits(json.dumps(listed_on, ensure_ascii=False))) == tg_labels
    assert f"@{TG_DISPATCH_B.username}" in listed_on["items"][0]["text"]
    assert b.mark in listed_on["items"][0]["text"]
    assert _update_row(sql, ctx.tg, update_id)[0] == FREE_MESSAGE_MASK
