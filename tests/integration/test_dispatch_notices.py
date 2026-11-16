"""Avisos imediatos dos disparos à equipe.

Cobre os avisos de rejeição, de abertura e de cancelamento, a reexecução de cada detecção e o piso por fato, com
os destinatários dos avisos, o aviso de rejeição sem os motivos do motor, o disparo que fecha antes do aviso
"aberto" e a detecção pela varredura e pelo gancho do fechamento (os modelos de `noticetexts`):
- rejeição de cada classe da validação do disparo → um aviso a cada membro com endereço ativo, com a referência curta
  (`qid@commit_short`), o título redigido (ou `UNTITLED` com o título nulo, antes da inspeção),
  `REJECTION_MESSAGES[classe]` e `cq dispatches show <código> <dispatch_id>`, sem os motivos do motor,
  traceback, caminho nem URL do repositório;
- abertura de 30 com 28 entregues, 1 não entregue e 1 em falha de questão → um único "aberto" com as
  contagens e o fechamento no fuso, e nenhuma mensagem de aviso a endereço de aluno;
- o motor fora do ar na abertura, com pendentes 10 min depois → o "aberto" com `OPENED_PENDING` e nenhum
  segundo "aberto"; o alvo vazio → um "aberto" com tudo 0 já na primeira rodada, sem esperar os
  10 min, e a reexecução da regra e da tarefa sem duplicar;
- os três cancelamentos automáticos (`expired`, `window_missed`, `validation_overran`) com o motivo, e os
  dois sem aviso (pelo operador e pelo encerramento do oferecimento);
- a reexecução da tarefa depois de uma queda e a posse esgotada sem duplicar; o membro que bloqueou
  o bot → `failed` para ele e os demais enviados;
- a janela de 5 min com pendentes → o "aberto" antecipado no fechamento, com as contagens do instante
  anterior à passagem das pendentes a não entregues, antes do resumo, uma vez cada, nessa ordem a cada
  destinatário;
- concorrência com duas conexões: o gancho do fechamento × a regra `notice.dispatch_opened` → um aviso só;
- o piso por fato: o disparo criado antes do deploy e rejeitado, cancelado ou aberto depois avisa,
  e o fato anterior ao deploy não; a varredura reanima cada detecção sem tarefa viva;
- o truncamento: o título longo, com `<`, `&` e caracteres multibyte, no rejeitado, no aberto e
  no cancelado → o texto cabe no `fits` do formatador e em 4096 caracteres de markup, só o título é cortado,
  os números e o comando ficam inteiros [os três modelos não levam a disciplina; a
  disciplina longa fica nos avisos de oferecimento];
- os avisos pelo console e pela Bot API falsa (o alvo vazio pelos dois canais e a rejeição pelo worker
  completo, `report_worker`, nos dois canais).

**Montagem.** A equipe vem da fixture `team_members` (o vínculo real dos bots; `rich_bot_api` pedida antes
dela); os alunos entram por SQL nas colunas do esquema do banco, como em `test_dispatch_opening.py`, e ficam
conhecidos do dublê por um `my_chat_member`. O disparo é preparado, confirmado e cancelado pela API. O
resto corre num **arnês passo a passo** (`Driver`), sem thread, para que os prazos (10 min do "aberto",
abertura atrasada, expiração) sejam exatos no `FakeClock`: os tratadores do disparo (validação, expiração,
abertura, fechamento e preparo das entregas) e os do acompanhamento (`teamnotices.task_handlers` e
`reports.freeze_task_handler`) montados como o `run_worker` os monta, a fila de tarefas (`TaskQueue.work_once`
só com esses tipos), a varredura (`scheduler.sweep_once` com as regras de `DISPATCH_RULES + REPORT_RULES` desses
tipos, como o `Sweeper`) e a fila de saída montada como a do worker com bots (`content`, `outcome`,
`notice_content=NoticeContentLoader()`), despachada pelo `TelegramChannel` real sobre a Bot API falsa ou
pelo `ConsoleChannel`. A prévia (`preview.send`) fica fora do arnês (tem os seus testes). Um teste usa o worker
completo (`report_worker`), para a regra rodar no laço real.

Textos: os modelos e as linhas opcionais de `noticetexts` (as opcionais entram antes da
linha do comando) preenchidos pelo teste, conferidos linha a linha, em ordem, no texto recebido; o horário
pelo formato da casa, `timezones.format_local` [I]. Ids internos, `seq`, chaves e
`channel_ref` só por relação.

**Vermelho no esqueleto.** O `Driver` monta os tratadores dos avisos antes dos do disparo: `teamnotices.task_handlers`
lança `NotImplementedError`; os demais casos falham em `record_opened_on_close` (`NotImplementedError`),
na tabela `team_notices` ausente ou na regra ainda ausente de `REPORT_RULES`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cq_server import deliveries, dispatches, noticetexts, previews, reports, scheduler, teamnotices
from cq_server.adapters.console import ConsoleChannel
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.bots import TelegramChannel
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.dispatches import REJECTION_MESSAGES, DeliveryCounts
from cq_server.outbox import Outbox
from cq_server.ports.repos import FetchNotFound, FetchRefused
from cq_server.tasks import ClaimedTask, TaskQueue, enqueue_in_transaction
from cq_server.telegramrender import TelegramHtmlFormatter, TelegramRichFormatter
from cq_server.timezones import format_local
from tests.helpers.api import (ADMIN_TOKEN, all_events, auth, cancel_dispatch, confirm_dispatch, prepare_dispatch,
                               register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import read_lines
from tests.helpers.engine import (TECHNICAL_DETAIL_SENTINEL, UNSUPPORTED_REASON, DYNAMIC_UNSUPPORTED_REASON,
                                  VARIANT_BROKEN, preset)
from tests.helpers.team import ACTIVE, CONSOLE, Team, team_messages

REPO = "https://repos.cq.test/avisos/disparos.git"
REPO_HOST = "repos.cq.test"
COMMIT = "d15a" * 10
COMMIT_SHORT = COMMIT[:12]
MISSING_QID = "questao_que_nao_existe"
TITLE = "Plano inclinado com atrito"
WORKER = "worker-avisos-disparo"
CRASHED_WORKER = "worker-caido"
EXPIRE = 31  # a posse dos testes (30 s) mais 1 s
WAIT = 20.0  # tempo real máximo de espera pelo worker completo (report_worker)
OPEN_WAIT = timedelta(seconds=600)  # teamnotices.OPEN_NOTICE_WAIT_SECONDS
ZERO_COUNTS = DeliveryCounts(pending=0, delivered=0, not_delivered=0, answered=0, dont_know=0, no_answer=0,
                             answered_late=0, question_failed=0, grading_pending=0)
REJECTED_TASK = "notice.dispatch_rejected"
CANCELLED_TASK = "notice.dispatch_cancelled"
OPENED_TASK = "notice.dispatch_opened"
SEND_TASK = "notice.send"
# O que nunca sai num aviso: os motivos do motor, o detalhe técnico, traceback, a URL
# e o host do repositório e os caminhos da área de repositórios.
FORBIDDEN = ("Traceback", "technical_detail", TECHNICAL_DETAIL_SENTINEL, "https://", "http://", REPO_HOST,
             "cq-repos", ".staging", UNSUPPORTED_REASON.message, DYNAMIC_UNSUPPORTED_REASON.message,
             VARIANT_BROKEN.message)
# Título longo: `<`, `&` e multibyte (inclusive fora do BMP, 2 unidades UTF-16).
LONG_TITLE = "Ação & reação <forças> em equilíbrio 🙂 — " * 150

_codes = itertools.count(1)
_DEFAULT: Any = object()

# A posse, por SQL, da tarefa disponível de menor `id` de um tipo e de um assunto.
_CLAIM_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s AND payload @> %(subject)s "
    "            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")


# ---------------------------------------------------------------------------------------------
# Arnês
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int


@dataclasses.dataclass(frozen=True)
class Student:
    pseudo_id: str
    person_row: int
    endpoint_row: int
    user: Any

    @property
    def chat(self) -> int:
        return self.user.user_id


def _rule(task_type: str) -> Any:
    found = [rule for rule in scheduler.REPORT_RULES if rule.task_type == task_type]
    assert len(found) == 1, f"regra {task_type} ausente de scheduler.REPORT_RULES"
    return found[0]


def _subject(task_type: str, row_id: int) -> dict:
    return {_rule(task_type).subject_key: row_id}


def _assert_lines(text: str, lines: Sequence[str]) -> None:
    """Cada linha esperada aparece no texto, nessa ordem."""
    position = 0
    for line in lines:
        found = text.find(line, position)
        assert found >= 0, f"linha ausente ou fora de ordem no aviso: {line!r}\n---\n{text}"
        position = found + len(line)


def _assert_clean(text: str) -> None:
    for forbidden in FORBIDDEN:
        assert forbidden not in text, f"{forbidden!r} num aviso à equipe"


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class World:
    """O oferecimento da equipe simulada, os alunos e os disparos do teste."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any,
                 bots: Any, rich: Any, engine_fake: Any, repo_fetcher: Any, team: Team) -> None:
        self.client, self.sql, self.db_url, self.clock, self.settings = client, sql, db_url, clock, settings
        self.bots, self.rich, self.engine_fake, self.fetcher, self.team = bots, rich, engine_fake, repo_fetcher, team
        self.offering = team.offering
        self.code = team.code
        self.bot = team.bot_id
        self.bot_row = team.bot_row
        self.tz = team.offering["timezone"]
        self.offering_row = int(sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                            (team.offering["offering_id"],)).fetchone()[0])
        self._users = itertools.count(1)
        self.drivers: list[Driver] = []

    def close(self) -> None:
        for driver in self.drivers:
            driver.close()

    # -- montagem ------------------------------------------------------------------------------

    def program(self, qid: str, question: Any) -> None:
        self.engine_fake.program((REPO, COMMIT, qid), question)

    def student(self, *, endpoint: str = "active") -> Student:
        """Um aluno vinculado (matrícula `student` `active`, termo aceito) com endereço no bot."""
        n = next(self._users)
        user = make_user(73_000 + n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Avisos Disparo {n}")
        person_row = int(self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                          (body["pseudo_id"],)).fetchone()[0])
        now = self.clock.now()
        self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
            (person_row, self.offering_row, now, now, now))
        endpoint_row = int(self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, %s, %s, %s) RETURNING id",
            (person_row, self.bot_row, user.user_id, endpoint, now, now)).fetchone()[0])
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        return Student(body["pseudo_id"], person_row, endpoint_row, user)

    def prepare(self, qid: str, *, opens_at: datetime | None = None, closes_at: datetime | None = None) -> Disp:
        closes_at = closes_at if closes_at is not None else (opens_at or self.clock.now()) + timedelta(hours=2)
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at), closes_at=iso(closes_at))
        row = int(self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                                   (body["dispatch_id"],)).fetchone()[0])
        return Disp(body["dispatch_id"], row)

    def confirm(self, d: Disp) -> dict:
        return confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)

    def driver(self, **kwargs: Any) -> Driver:
        driver = Driver(self, **kwargs)
        self.drivers.append(driver)
        return driver

    def set_floor_now(self) -> int:
        """O "deploy" agora: o piso dos eventos passa a ser o maior `seq`."""
        return int(self.sql.execute("UPDATE team_notice_floors SET event_floor = (SELECT coalesce(max(seq), 0) "
                                    "FROM events) WHERE id = 1 RETURNING event_floor").fetchone()[0])

    # -- consultas -----------------------------------------------------------------------------

    def dispatch(self, d: Disp) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM dispatches WHERE id = %s", (d.row,)).fetchone()

    def deliveries(self, d: Disp) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM deliveries WHERE dispatch_id = %s ORDER BY id", (d.row,)).fetchall()

    def notices(self, kind: str | None = None, d: Disp | None = None) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT * FROM team_notices WHERE offering_id = %s AND (%s::text IS NULL OR kind = %s::text) "
                "AND (%s::bigint IS NULL OR dispatch_id = %s::bigint) ORDER BY id",
                (self.offering_row, kind, kind, None if d is None else d.row, None if d is None else d.row)).fetchall()

    def notice_messages(self, kind: str, d: Disp) -> list[Any]:
        ids = {n["id"] for n in self.notices(kind, d)}
        return [m for m in team_messages(self.sql, kind) if m.notice_id in ids]

    def texts(self, kind: str, d: Disp, *, stream: io.StringIO | None = None) -> dict[str, list[str]]:
        """O texto recebido por cada membro com endereço ativo (pelo rótulo), dos avisos `kind` do disparo:
        pela Bot API falsa (`plain_text` da mensagem corrente) ou, com `stream`, pela linha do console."""
        lines = {line.message_id: line for line in read_lines(stream.getvalue())} if stream is not None else {}
        out: dict[str, list[str]] = {m.label: [] for m in self.team.active}
        for message in self.notice_messages(kind, d):
            member = next(m for m in self.team.members if m.pseudo_id == message.recipient)
            assert message.state == "sent", f"aviso {kind} não enviado a {member.label}: {message.state}"
            if stream is not None:
                text = lines[message.message_id].text
            else:
                sent = self.rich.current(self.bot, member.chat_id, int(message.channel_ref))
                text = sent.plain_text or ""
            out[member.label].append(text)
        return out

    def events(self, *types: str) -> list[tuple]:
        return [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"])
                for e in all_events(self.client, ADMIN_TOKEN, *types)]

    def recorded(self, kind: str, d: Disp, *, at: datetime, state: str = "queued", count: int | None = None) -> tuple:
        """O `notice.recorded` de um aviso de disparo."""
        return ("notice.recorded", iso(at), None,
                {"offering_id": self.offering["offering_id"], "notice_type": kind, "dispatch_id": d.id,
                 "report_version": None, "episode_seq": None, "batch_id": None, "window_started_at": None,
                 "local_date": None, "state": state,
                 "recipient_count": len(self.team.active) if count is None else count})

    def recorded_for(self, d: Disp) -> list[tuple]:
        return [e for e in self.events("notice.recorded") if e[3]["dispatch_id"] == d.id]

    # -- textos esperados ----------------------------------------------------------------------

    def rejected_lines(self, d: Disp, qid: str, title: str | None, rejection: str) -> list[str]:
        return noticetexts.REJECTED.format(reference=f"{qid}@{COMMIT_SHORT}", title=title or noticetexts.UNTITLED,
                                           reason=REJECTION_MESSAGES[rejection], code=self.code,
                                           dispatch_id=d.id).split("\n")

    def opened_lines(self, d: Disp, *, target: int, delivered: int, not_delivered: int, question_failed: int,
                     pending: int, title: str = TITLE) -> list[str]:
        closes_at = self.dispatch(d)["closes_at"]
        lines = noticetexts.OPENED.format(title=title, target=target, delivered=delivered,
                                          not_delivered=not_delivered, question_failed=question_failed,
                                          pending=pending, closes_at=format_local(closes_at, self.tz),
                                          code=self.code, dispatch_id=d.id).split("\n")
        if pending:
            lines.insert(len(lines) - 1, noticetexts.OPENED_PENDING)
        return lines

    def cancelled_lines(self, d: Disp, reason: str, title: str = TITLE) -> list[str]:
        return noticetexts.CANCELLED.format(title=title, reason=noticetexts.CANCEL_REASONS[reason], code=self.code,
                                            dispatch_id=d.id).split("\n")


class Driver:
    """O worker passo a passo, sem thread (ver o docstring do módulo): os tratadores do disparo e dos avisos montados
    como o `run_worker` os monta, a fila de tarefas, a varredura e a fila de saída como a do worker com bots."""

    def __init__(self, w: World, *, console: bool = False, delivery: DeliverySettings | None = None,
                 engine: Any = _DEFAULT) -> None:
        self.w = w
        self.stack = contextlib.ExitStack()
        self.stream = io.StringIO()
        self.delivery = delivery or DeliverySettings()
        try:
            self._build(console, engine)
        except BaseException:
            self.stack.close()
            raise

    def _build(self, console: bool, engine: Any) -> None:
        w = self.w
        if console:
            self.channel: Any = ConsoleChannel(self.stream)
        else:
            self.channel = TelegramChannel(w.db_url, clock=w.clock, runtime=w.bots, default=ConsoleChannel(self.stream),
                                           delivery=self.delivery)
            self.stack.callback(self.channel.close)
        r6 = teamnotices.task_handlers(w.db_url, clock=w.clock, settings=w.settings, channel=self.channel)
        for handler in r6.values():
            self.stack.callback(handler.close)
        freeze = reports.freeze_task_handler(w.db_url, clock=w.clock, settings=w.settings, channel=self.channel)
        self.stack.callback(freeze.close)
        if engine is _DEFAULT:
            engine = HttpQuestionEngine(EngineSettings(engine_url=w.engine_fake.base_url, timeout_seconds=10.0))
            self.stack.callback(engine.close)
        r5 = {
            dispatches.VALIDATE_TASK_TYPE: dispatches.validate_task_handler(
                w.db_url, clock=w.clock, settings=w.settings, engine=engine, fetcher=w.fetcher, channel=self.channel),
            dispatches.EXPIRE_TASK_TYPE: dispatches.expire_task_handler(w.db_url, clock=w.clock, settings=w.settings),
            dispatches.OPEN_TASK_TYPE: dispatches.open_task_handler(w.db_url, clock=w.clock, settings=w.settings),
            dispatches.CLOSE_TASK_TYPE: dispatches.close_task_handler(w.db_url, clock=w.clock, settings=w.settings,
                                                                      channel=self.channel),
            deliveries.PREPARE_TASK_TYPE: deliveries.prepare_task_handler(
                w.db_url, clock=w.clock, settings=w.settings, engine=engine, channel=self.channel),
        }
        for handler in r5.values():
            self.stack.callback(handler.close)
        self.handlers: dict[str, Any] = {**r5, **r6, reports.FREEZE_TASK_TYPE: freeze}
        self.rules = [rule for rule in scheduler.DISPATCH_RULES + scheduler.REPORT_RULES if rule.task_type in self.handlers]
        self.queue = TaskQueue(w.db_url, clock=w.clock, settings=w.settings)
        self.stack.callback(self.queue.close)
        self.outbox = Outbox(w.db_url, clock=w.clock, settings=w.settings,
                             content=deliveries.DeliveryContentLoader(self.channel, delivery=self.delivery),
                             outcome=deliveries.DeliveryOutcomeRecorder(self.channel, clock=w.clock),
                             notice_content=previews.NoticeContentLoader())
        self.stack.callback(self.outbox.close)

    def close(self) -> None:
        self.stack.close()

    # -- passos --------------------------------------------------------------------------------

    def sweep(self, *task_types: str) -> int:
        rules = self.rules if not task_types else [r for r in self.rules if r.task_type in task_types]
        result = scheduler.sweep_once(self.w.sql, now=self.w.clock.now(),
                                      retry_gap=float(self.w.settings.backoff_max_seconds), rules=rules)
        return sum(result.enqueued.values()) + result.abandoned

    def work(self, *, limit: int = 600) -> int:
        done = 0
        while self.queue.work_once(WORKER, self.handlers, task_types=list(self.handlers)) is not None:
            done += 1
            assert done <= limit, "o arnês processou tarefas demais"
        return done

    def send(self, *, limit: int = 600) -> int:
        sent = 0
        while self.outbox.dispatch_once(self.channel, WORKER, include_routed=True) is not None:
            sent += 1
            assert sent <= limit, "o arnês despachou mensagens demais"
        return sent

    def _next_due(self) -> datetime | None:
        """O próximo horário (estritamente futuro) de uma tarefa dos tipos do arnês ou de uma mensagem
        `pending` (uma retida, já disponível, não faz o relógio andar)."""
        now = self.w.clock.now()
        row = self.w.sql.execute(
            "SELECT min(at) FROM (SELECT min(available_at) AS at FROM tasks WHERE state = 'pending' "
            "AND task_type = ANY(%s) AND available_at > %s UNION ALL SELECT min(available_at) FROM outbox_messages "
            "WHERE state = 'pending' AND available_at > %s) AS due", (list(self.handlers), now, now)).fetchone()
        return row[0]

    def settle(self, *, advance: bool = False, rounds: int = 80) -> None:
        """Processa tarefas, mensagens e rodadas da varredura até nada mais andar; com `advance`, o relógio
        salta para a próxima tarefa ou mensagem com espera (as repetições da fila de tarefas)."""
        for _ in range(rounds):
            if self.work() + self.send() + self.sweep():
                continue
            if not advance:
                return
            due = self._next_due()
            if due is None:
                return
            self.w.clock.set(max(due, self.w.clock.now()))
        raise AssertionError(f"o arnês não sossegou em {rounds} rodadas")

    def claim(self, task_type: str, subject: dict, *, owner: str = WORKER) -> ClaimedTask | None:
        now = self.w.clock.now()
        row = self.w.sql.execute(_CLAIM_SQL, {"owner": owner, "until": now + timedelta(seconds=self.w.settings.lease_seconds),
                                              "type": task_type, "now": now, "subject": Jsonb(subject)}).fetchone()
        return None if row is None else ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2],
                                                    attempts=int(row[3]))

    def run(self, task_type: str, subject: dict, *, limit: int = 8) -> int:
        """Só as tarefas de `task_type` do assunto, uma a uma."""
        done = 0
        while (task := self.claim(task_type, subject)) is not None:
            self.handlers[task_type](task)
            assert self.queue.complete(task.id, WORKER), f"{task_type} não concluiu"
            done += 1
            assert done <= limit
        return done

    def enqueue(self, task_type: str, subject: dict, key: str) -> None:
        with psycopg.connect(self.w.db_url) as conn:
            enqueue_in_transaction(conn, task_type, subject, now=self.w.clock.now(), idempotency_key=key)

    def validate(self, d: Disp) -> str:
        self.run(dispatches.VALIDATE_TASK_TYPE, {"dispatch_row_id": d.row})
        return str(self.w.dispatch(d)["state"])

    def step(self, task_type: str, d: Disp) -> str:
        """A tarefa do disparo que a varredura criaria (mesmo payload e chave da 1ª rodada), já processada."""
        self.enqueue(task_type, {"dispatch_row_id": d.row}, f"{task_type}:{d.row}:r1")
        assert self.run(task_type, {"dispatch_row_id": d.row}) == 1
        return str(self.w.dispatch(d)["state"])


@pytest.fixture
def world(rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, db_url: str, clock: Any,
          settings: Any, bots: Any, engine_fake: Any, repo_fetcher: Any) -> Iterator[World]:
    w = World(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, bots=bots, rich=rich_bot_api,
              engine_fake=engine_fake, repo_fetcher=repo_fetcher, team=team_members)
    try:
        yield w
    finally:
        w.close()


def _assert_one_each(w: World, texts: dict[str, list[str]]) -> list[str]:
    """Cada membro com endereço ativo recebeu exatamente um aviso; devolve os textos na ordem dos membros."""
    assert sorted(texts) == sorted(m.label for m in w.team.active)
    assert all(len(items) == 1 for items in texts.values()), texts
    return [texts[m.label][0] for m in w.team.active]


# ---------------------------------------------------------------------------------------------
# Rejeitado
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Rejection:
    rejection: str
    qid: str
    title: str | None
    setup: Callable[[World], Any]
    delivery: DeliverySettings | None = None
    advance: bool = False


def _fetch(result: Any) -> Callable[[World], Any]:
    def setup(w: World) -> None:
        w.fetcher.program(REPO, COMMIT, result)
    return setup


def _nothing(w: World) -> None:
    return None


def _stop_engine(w: World) -> None:
    w.engine_fake.stop()


def _inspect_rejected(w: World) -> None:
    w.engine_fake.fail("inspect", error="invalid_request", times=None)


REJECTIONS = {
    "repository_unreachable": _Rejection("repository_unreachable", "number", None, _fetch(FetchNotFound("repository"))),
    "repository_refused": _Rejection("repository_refused", "number", None, _fetch(FetchRefused("submodule"))),
    "commit_not_found": _Rejection("commit_not_found", "number", None, _fetch(FetchNotFound("commit"))),
    "question_not_found": _Rejection("question_not_found", MISSING_QID, None, _nothing),
    "question_unsupported": _Rejection("question_unsupported", "dynamic_unsupported", TITLE, _nothing),
    "question_broken": _Rejection("question_broken", "broken_on_variant", TITLE, _nothing),
    "engine_unavailable": _Rejection("engine_unavailable", "number", None, _stop_engine, advance=True),
    "channel_limits_exceeded": _Rejection("channel_limits_exceeded", "single_choice", TITLE, _nothing,
                                          delivery=DeliverySettings(telegram_format="html")),
    "internal_failure": _Rejection("internal_failure", "number", None, _inspect_rejected),
}


def _program_all(w: World) -> None:
    w.program("number", preset("number", title=TITLE))
    w.program("dynamic_unsupported", preset("dynamic_unsupported", title=TITLE))
    w.program("broken_on_variant", preset("broken_on_variant", title=TITLE))
    w.program("single_choice", preset("single_choice", title=TITLE,
                                      statement="Texto longo do enunciado da questão. " * 160))


@pytest.mark.parametrize("case", list(REJECTIONS), ids=list(REJECTIONS))
def test_each_rejection_class_notifies_each_active_member_once_without_engine_details(world: World, case: str) -> None:
    """Cada classe de rejeição da validação do disparo (obtenção, commit,
    questão inexistente, não suportada, quebrada, motor fora do ar até a última tentativa, acima dos limites
    do canal e recusa do motor) → um único aviso `dispatch_rejected` a cada membro com endereço ativo, com a
    referência curta, o título redigido (ou `UNTITLED` quando a rejeição veio antes da inspeção), a mensagem
    pareada da classe e o comando; sem os motivos do motor, traceback, caminho, URL nem host do repositório.
    Nenhuma mensagem de aviso ao aluno vinculado com endereço ativo no mesmo bot; um
    `notice.recorded` (`queued`, 2 destinatários)."""
    spec = REJECTIONS[case]
    _program_all(world)
    student = world.student()
    driver = world.driver(delivery=spec.delivery)
    spec.setup(world)
    d = world.prepare(spec.qid)

    driver.settle(advance=spec.advance)

    row = world.dispatch(d)
    assert (row["state"], row["rejection"]) == ("rejected", spec.rejection)
    at = world.clock.now()
    texts = _assert_one_each(world, world.texts("dispatch_rejected", d))
    for text in texts:
        _assert_lines(text, world.rejected_lines(d, spec.qid, spec.title, spec.rejection))
        _assert_clean(text)
    for message in world.notice_messages("dispatch_rejected", d):
        assert (message.text_format, message.priority) == ((spec.delivery or DeliverySettings()).telegram_format, 2)
        _assert_clean(message.text or "")
    assert team_messages(world.sql, recipient=student.pseudo_id) == []
    assert world.recorded_for(d) == [world.recorded("dispatch_rejected", d, at=at)]
    [notice] = world.notices("dispatch_rejected", d)
    assert (notice["subject_key"], notice["state"], notice["recipient_count"]) == (f"dispatch:{d.row}", "queued", 2)


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}, (ACTIVE, ACTIVE)],
                         ids=["console", "telegram"], indirect=True)
def test_running_worker_delivers_the_rejection_to_each_member(world: World, report_worker: Any) -> None:
    """Com o worker completo no ar (`run_worker` com bots ou com o console com
    rota; os tratadores dos avisos entram pelo próprio `run_worker`), a validação rejeita a questão quebrada e a
    regra `notice.dispatch_rejected` da varredura do laço registra e entrega o aviso, uma vez a cada membro,
    pelo canal do worker; nenhum outro aviso chega à equipe para esse disparo."""
    _program_all(world)
    console = world.team.channel == CONSOLE
    stream = world.team.stream if console else io.StringIO()
    settings = dataclasses.replace(world.settings, worker_id="worker-avisos-laco")
    report_worker(console=console, stream=stream, settings=settings, delivery=DeliverySettings())
    d = world.prepare("broken_on_variant")

    def delivered() -> bool:
        messages = world.notice_messages("dispatch_rejected", d)
        return len(messages) == len(world.team.active) and all(m.state == "sent" for m in messages)

    world.rich.wait_for(delivered, timeout=WAIT)
    report_worker.stop()

    texts = _assert_one_each(world, world.texts("dispatch_rejected", d, stream=stream if console else None))
    for text in texts:
        _assert_lines(text, world.rejected_lines(d, "broken_on_variant", TITLE, "question_broken"))
        _assert_clean(text)
    assert [n["kind"] for n in world.notices(d=d)] == ["dispatch_rejected"]
    assert [e[3]["state"] for e in world.recorded_for(d)] == ["queued"]


# ---------------------------------------------------------------------------------------------
# Aberto
# ---------------------------------------------------------------------------------------------


def test_opening_of_thirty_with_28_delivered_1_not_delivered_1_failed_sends_one_opened(world: World) -> None:
    """Um disparo que abre para 30 alunos (29 com endereço ativo e
    1 bloqueado) e cujas entregas terminam com 28 entregues, 1 não entregue (`endpoint_blocked`) e 1 em falha
    de questão (o motor recusa uma variante) → quando a última entrega sai de pendente, um único "aberto" a
    cada membro, com essas contagens e o fechamento no fuso do oferecimento, sem a linha das pendentes;
    nenhuma mensagem com `notice_id` a endereço de aluno do disparo (o aluno não vê nada novo)."""
    world.program("single_choice", preset("single_choice", title=TITLE))
    students = [world.student() for _ in range(29)] + [world.student(endpoint="blocked")]
    driver = world.driver()
    d = world.prepare("single_choice")
    driver.settle()
    assert world.dispatch(d)["state"] == "scheduled"
    world.engine_fake.fail("create_variant", error="invalid_request", times=1)
    world.confirm(d)

    driver.settle()

    states = sorted((r["state"], r["undelivered_reason"], r["failure"]) for r in world.deliveries(d))
    assert states == sorted([("delivered", None, None)] * 28 + [("not_delivered", "endpoint_blocked", None),
                                                                ("question_failed", None, "engine_rejected")])
    at = world.clock.now()
    texts = _assert_one_each(world, world.texts("dispatch_opened", d))
    for text in texts:
        _assert_lines(text, world.opened_lines(d, target=30, delivered=28, not_delivered=1, question_failed=1,
                                               pending=0))
        assert noticetexts.OPENED_PENDING not in text
    student_endpoints = sorted(s.endpoint_row for s in students)
    assert world.sql.execute("SELECT count(*) FROM outbox_messages WHERE notice_id IS NOT NULL "
                             "AND delivery_endpoint_id = ANY(%s)", (student_endpoints,)).fetchone()[0] == 0
    assert world.recorded_for(d) == [world.recorded("dispatch_opened", d, at=at)]


def test_engine_down_at_opening_sends_opened_with_pending_after_ten_minutes_only_once(world: World) -> None:
    """Com o motor fora do ar na abertura, as entregas ficam pendentes; até
    599 s depois da abertura, nenhum "aberto"; aos 600 s, um "aberto" com as pendentes e a linha
    `OPENED_PENDING`; com o motor de volta e as entregas feitas, nenhum segundo "aberto"."""
    world.program("number", preset("number", title=TITLE))
    [world.student() for _ in range(2)]
    driver = world.driver()
    d = world.prepare("number")
    driver.settle()
    world.engine_fake.stop()
    world.confirm(d)
    driver.settle()
    opened_at = world.dispatch(d)["opened_at"]
    assert [r["state"] for r in world.deliveries(d)] == ["pending", "pending"]

    world.clock.set(opened_at + OPEN_WAIT - timedelta(seconds=1))
    driver.settle()
    assert world.notices("dispatch_opened", d) == []
    world.clock.set(opened_at + OPEN_WAIT)
    driver.settle()
    at = world.clock.now()
    texts = _assert_one_each(world, world.texts("dispatch_opened", d))
    for text in texts:
        _assert_lines(text, world.opened_lines(d, target=2, delivered=0, not_delivered=0, question_failed=0,
                                               pending=2))

    world.engine_fake.start()
    driver.settle(advance=True)
    assert [r["state"] for r in world.deliveries(d)] == ["delivered", "delivered"]
    world.clock.advance(OPEN_WAIT.total_seconds())
    driver.settle()
    assert len(world.notices("dispatch_opened", d)) == 1
    assert [len(items) for items in world.texts("dispatch_opened", d).values()] == [1, 1]
    assert world.recorded_for(d) == [world.recorded("dispatch_opened", d, at=at)]


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}, (ACTIVE, ACTIVE)],
                         ids=["console", "telegram"], indirect=True)
def test_empty_target_sends_opened_with_zeros_on_the_first_round_and_reruns_do_not_duplicate(world: World) -> None:
    """Alvo vazio: o disparo de alvo vazio abre sem entrega e o
    "aberto" sai já na primeira rodada da regra `notice.dispatch_opened` (o `FakeClock` parado, sem esperar
    os 10 min), com alvo, entregues, não entregues, falha de questão e pendentes 0 e sem a linha das
    pendentes, a cada membro, pelo console e pela Bot API falsa; a reexecução da regra (nenhuma tarefa nova)
    e da tarefa (nenhuma mensagem nem evento novo) não duplica."""
    console = world.team.channel == CONSOLE
    world.program("number", preset("number", title=TITLE))
    driver = world.driver(console=console)
    t0 = world.clock.now()
    d = world.prepare("number")
    driver.settle()
    world.confirm(d)

    driver.settle()

    assert world.clock.now() == t0 and world.dispatch(d)["state"] == "open"
    texts = _assert_one_each(world, world.texts("dispatch_opened", d, stream=driver.stream if console else None))
    for text in texts:
        _assert_lines(text, world.opened_lines(d, target=0, delivered=0, not_delivered=0, question_failed=0,
                                               pending=0))
        assert noticetexts.OPENED_PENDING not in text
    expected_format = "plain" if console else "rich"
    assert all(m.text_format == expected_format for m in world.notice_messages("dispatch_opened", d))
    before = world.sql.execute("SELECT count(*) FROM tasks WHERE task_type = %s", (OPENED_TASK,)).fetchone()[0]
    world.clock.advance(5)
    assert driver.sweep(OPENED_TASK) == 0
    driver.enqueue(OPENED_TASK, _subject(OPENED_TASK, d.row), f"{OPENED_TASK}:{d.row}:reexecucao")
    assert driver.run(OPENED_TASK, _subject(OPENED_TASK, d.row)) == 1
    driver.settle()

    assert world.sql.execute("SELECT count(*) FROM tasks WHERE task_type = %s", (OPENED_TASK,)).fetchone()[0] == before + 1
    assert [len(items) for items in world.texts("dispatch_opened", d, stream=driver.stream if console else None).values()] == [1, 1]
    assert world.recorded_for(d) == [world.recorded("dispatch_opened", d, at=t0)]


# ---------------------------------------------------------------------------------------------
# Cancelado
# ---------------------------------------------------------------------------------------------


def test_automatic_cancellations_notify_with_the_reason_and_operator_and_offering_close_do_not(world: World) -> None:
    """O agendado sem confirmação que expira, o imediato cuja abertura atrasa e
    perde a janela de 5 min e o agendado cuja validação começa com a abertura já passada → um aviso
    `dispatch_cancelled` a cada membro, com o motivo funcional de `CANCEL_REASONS`; o cancelamento pelo
    operador e o do encerramento do oferecimento → nenhum aviso (nem registro), com as rodadas da varredura
    passando por eles."""
    world.program("number", preset("number", title=TITLE))
    driver = world.driver()
    t0 = world.clock.now()
    expiring = world.prepare("number", opens_at=t0 + timedelta(hours=1))
    missing = world.prepare("number", closes_at=t0 + timedelta(minutes=5))
    by_operator = world.prepare("number", opens_at=t0 + timedelta(hours=2))
    by_closing = world.prepare("number", opens_at=t0 + timedelta(hours=2))
    driver.settle()
    assert [world.dispatch(x)["state"] for x in (expiring, missing, by_operator, by_closing)] == ["scheduled"] * 4
    world.confirm(missing)
    cancel_dispatch(world.client, ADMIN_TOKEN, world.code, by_operator.id)

    world.clock.set(t0 + timedelta(seconds=61))
    driver.settle()
    overran = world.prepare("number", opens_at=world.clock.now() + timedelta(minutes=10))
    world.clock.set(world.clock.now() + timedelta(minutes=10))
    driver.settle()
    world.clock.set(t0 + timedelta(hours=1))
    driver.settle()
    resp = world.client.post(f"/v1/offerings/{world.code}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    driver.settle()
    world.clock.advance(5)
    driver.settle()

    reasons = {x.row: world.dispatch(x)["cancel_reason"] for x in (expiring, missing, overran, by_operator, by_closing)}
    assert reasons == {expiring.row: "expired", missing.row: "window_missed", overran.row: "validation_overran",
                       by_operator.row: "operator", by_closing.row: "offering_closed"}
    for d, reason in ((expiring, "expired"), (missing, "window_missed"), (overran, "validation_overran")):
        texts = _assert_one_each(world, world.texts("dispatch_cancelled", d))
        for text in texts:
            _assert_lines(text, world.cancelled_lines(d, reason))
            _assert_clean(text)
        assert [e[3]["state"] for e in world.recorded_for(d)] == ["queued"]
    for d in (by_operator, by_closing):
        assert world.notices(d=d) == [] and world.recorded_for(d) == []


# ---------------------------------------------------------------------------------------------
# Reexecução, posse esgotada e bloqueio
# ---------------------------------------------------------------------------------------------


def test_rerun_after_a_crash_and_exhausted_lease_of_the_detection_do_not_duplicate(world: World) -> None:
    """(a) a tarefa `notice.dispatch_rejected` que registrou o aviso mas caiu
    antes de concluir (posse de um worker que caiu) é retomada depois da posse e termina `done` sem efeito
    novo; (b) com a posse esgotada, a fila de tarefas a leva a `failed` e a varredura não cria outra rodada (o
    aviso já existe). Cada membro recebe cada aviso uma única vez; um aviso e um `notice.recorded` por
    disparo."""
    world.program("number", preset("number", title=TITLE))
    driver = world.driver()
    a, b = world.prepare(MISSING_QID), world.prepare(MISSING_QID)
    assert driver.validate(a) == driver.validate(b) == "rejected"
    assert driver.sweep(REJECTED_TASK) == 2

    for d in (a, b):
        task = driver.claim(REJECTED_TASK, _subject(REJECTED_TASK, d.row), owner=CRASHED_WORKER)
        assert task is not None
        driver.handlers[REJECTED_TASK](task)  # a queda logo depois do commit: a tarefa fica em posse
    notices = {d.row: [n["id"] for n in world.notices("dispatch_rejected", d)] for d in (a, b)}
    assert [len(ids) for ids in notices.values()] == [1, 1]
    world.sql.execute("UPDATE tasks SET attempts = %s WHERE task_type = %s AND payload @> %s",
                      (world.settings.max_attempts, REJECTED_TASK, Jsonb(_subject(REJECTED_TASK, b.row))))
    world.clock.advance(EXPIRE)

    driver.settle()
    key = _rule(REJECTED_TASK).subject_key
    rows = world.sql.execute("SELECT payload, state, attempts FROM tasks WHERE task_type = %s ORDER BY id",
                             (REJECTED_TASK,)).fetchall()
    assert {r[0][key]: (r[1], r[2]) for r in rows} == {a.row: ("done", 2), b.row: ("failed", world.settings.max_attempts)}
    world.clock.advance(world.settings.backoff_max_seconds + 1)
    assert driver.sweep(REJECTED_TASK) == 0
    driver.settle()

    for d in (a, b):
        assert [n["id"] for n in world.notices("dispatch_rejected", d)] == notices[d.row]
        _assert_one_each(world, world.texts("dispatch_rejected", d))
        assert len(world.recorded_for(d)) == 1
    assert world.sql.execute("SELECT count(*) FROM tasks WHERE task_type = %s", (REJECTED_TASK,)).fetchone()[0] == 2


@pytest.mark.parametrize("team_members", [(ACTIVE, ACTIVE, ACTIVE)], indirect=True)
def test_member_who_blocked_the_bot_fails_and_the_others_receive(world: World) -> None:
    """O membro que bloqueou o bot depois do vínculo → o envio do aviso falha de
    vez para ele (403), o endereço fica `blocked`, com `outbox.failed` e `endpoint.blocked`; os demais recebem
    normalmente. O aviso fica `queued` com os 3 destinatários do momento do enfileiramento."""
    world.program("number", preset("number", title=TITLE))
    blocker = world.team.member("membro-2")
    world.rich.block_chat(world.bot, blocker.user)
    driver = world.driver()
    d = world.prepare(MISSING_QID)
    cursor = len(world.events("outbox.failed", "endpoint.blocked"))

    driver.settle()

    messages = {m.recipient: m for m in world.notice_messages("dispatch_rejected", d)}
    assert sorted(messages) == sorted(m.pseudo_id for m in world.team.members)
    assert messages[blocker.pseudo_id].state == "failed"
    assert all(messages[m.pseudo_id].state == "sent" for m in world.team.members if m is not blocker)
    endpoint = world.sql.execute(
        "SELECT d.state FROM delivery_endpoints d JOIN people p ON p.id = d.person_id "
        "WHERE p.pseudo_id = %s AND d.bot_id = %s", (uuid.UUID(blocker.pseudo_id), world.bot_row)).fetchone()[0]
    assert endpoint == "blocked"
    events = world.events("outbox.failed", "endpoint.blocked")[cursor:]
    assert [(e[0], e[3].get("message_id"), e[3].get("person"), e[3].get("recipient")) for e in events] == [
        ("outbox.failed", messages[blocker.pseudo_id].message_id, None, blocker.pseudo_id),
        ("endpoint.blocked", None, blocker.pseudo_id, None)]
    [notice] = world.notices("dispatch_rejected", d)
    assert (notice["state"], notice["recipient_count"]) == ("queued", 3)


# ---------------------------------------------------------------------------------------------
# O "aberto" antecipado no fechamento e a concorrência com o gancho
# ---------------------------------------------------------------------------------------------


def test_closing_before_the_ten_minutes_anticipates_the_opened_notice_before_the_summary(world: World) -> None:
    """O disparo que fecha antes do aviso "aberto": janela de 5 min, motor fora
    do ar na abertura e 2 entregas pendentes; o disparo fecha antes de vencer o prazo de 10 min do "aberto":
    o gancho do fechamento registra o "aberto" com as contagens do instante anterior à passagem das pendentes
    a não entregues (pendentes 2, não entregues 0), e o congelamento o entrega antes do resumo de
    fechamento. Cada membro recebe os dois, uma vez cada, nessa ordem (na fila e na Bot API falsa)."""
    world.program("number", preset("number", title=TITLE))
    [world.student() for _ in range(2)]
    driver = world.driver()
    t0 = world.clock.now()
    d = world.prepare("number", closes_at=t0 + timedelta(minutes=5))
    driver.settle()
    world.engine_fake.stop()
    world.confirm(d)
    driver.settle()
    assert [r["state"] for r in world.deliveries(d)] == ["pending", "pending"]
    assert world.notices(d=d) == []

    world.clock.set(world.dispatch(d)["closes_at"])
    driver.settle()

    assert world.dispatch(d)["state"] == "closed"
    assert sorted((r["state"], r["undelivered_reason"]) for r in world.deliveries(d)) == [
        ("not_delivered", "engine_unavailable")] * 2
    opened = world.texts("dispatch_opened", d)
    for text in _assert_one_each(world, opened):
        _assert_lines(text, world.opened_lines(d, target=2, delivered=0, not_delivered=0, question_failed=0,
                                               pending=2))
    for text in _assert_one_each(world, world.texts("dispatch_closed", d)):
        _assert_lines(text, [noticetexts.CLOSED_SUMMARY.split("\n")[0].format(title=TITLE)])
    for member in world.team.active:
        mine = [m for m in team_messages(world.sql, recipient=member) if m.kind in ("dispatch_opened", "dispatch_closed")]
        assert [m.kind for m in mine] == ["dispatch_opened", "dispatch_closed"]
        received = [msg.message_id for msg in world.rich.messages(world.bot, member.chat_id)]
        refs = [int(m.channel_ref) for m in mine]
        assert refs == sorted(refs) and all(ref in received for ref in refs)
    assert [e[3]["notice_type"] for e in world.recorded_for(d)] == ["dispatch_opened", "dispatch_closed"]


def _lock_waiters(sql: psycopg.Connection) -> int:
    return int(sql.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND wait_event_type = 'Lock'").fetchone()[0])


def test_close_hook_and_opened_rule_on_two_connections_record_a_single_notice(world: World) -> None:
    """Concorrência: o gancho do fechamento
    (numa conexão, com o disparo travado como no `_close_locked`, registrando o "aberto" antecipado) e a
    tarefa da regra `notice.dispatch_opened` (noutra conexão, no tratador dos avisos) correm ao mesmo tempo:
    a tarefa espera e termina sem erro e sem segundo registro; depois da entrega, um único "aberto" por
    membro e um único `notice.recorded` do "aberto"."""
    world.program("number", preset("number", title=TITLE))
    driver = world.driver()
    d = world.prepare("number")
    assert driver.validate(d) == "scheduled"
    world.confirm(d)
    assert driver.run(dispatches.OPEN_TASK_TYPE, {"dispatch_row_id": d.row}) == 1
    assert world.dispatch(d)["state"] == "open"
    driver.enqueue(OPENED_TASK, _subject(OPENED_TASK, d.row), f"{OPENED_TASK}:{d.row}:r1")
    task = driver.claim(OPENED_TASK, _subject(OPENED_TASK, d.row))
    assert task is not None
    errors: list[BaseException] = []

    def rule_task() -> None:
        try:
            driver.handlers[OPENED_TASK](task)
        except BaseException as exc:  # noqa: BLE001 - conferida pelo teste
            errors.append(exc)

    thread = threading.Thread(target=rule_task, daemon=True)
    with psycopg.connect(world.db_url) as hook:
        with hook.transaction():
            hook.execute("SELECT id FROM dispatches WHERE id = %s FOR UPDATE", (d.row,))
            assert teamnotices.record_opened_on_close(hook, dispatch_row_id=d.row, offering_row_id=world.offering_row,
                                                      counts=ZERO_COUNTS, now=world.clock.now()) is True
            thread.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and thread.is_alive() and _lock_waiters(world.sql) < 1:
                time.sleep(0.02)
    thread.join(10)
    assert not thread.is_alive(), "a tarefa da regra ficou presa"
    assert errors == []
    assert driver.queue.complete(task.id, WORKER)
    driver.settle()

    assert len(world.notices("dispatch_opened", d)) == 1
    _assert_one_each(world, world.texts("dispatch_opened", d))
    assert [e[3]["notice_type"] for e in world.recorded_for(d)] == ["dispatch_opened"]


# ---------------------------------------------------------------------------------------------
# Piso por fato e a varredura que reanima
# ---------------------------------------------------------------------------------------------


def test_fact_after_the_deploy_notifies_even_for_older_dispatches_and_fact_before_does_not(world: World) -> None:
    """O piso dos avisos de disparo é pelo `seq` do evento da transição.
    Com os disparos criados antes do "deploy" (o piso gravado agora), o rejeitado, o cancelado (expirado) e o
    aberto **depois** dele avisam; os mesmos fatos **antes** dele não avisam (nenhum registro), mesmo com as
    rodadas da varredura passando por eles."""
    world.program("number", preset("number", title=TITLE))
    driver = world.driver()
    t0 = world.clock.now()
    old_rejected, new_rejected = world.prepare(MISSING_QID), world.prepare(MISSING_QID)
    old_expired = world.prepare("number", opens_at=t0 + timedelta(minutes=30))
    new_expired = world.prepare("number", opens_at=t0 + timedelta(hours=1))
    old_opened = world.prepare("number", closes_at=t0 + timedelta(hours=3))
    new_opened = world.prepare("number", opens_at=t0 + timedelta(hours=2), closes_at=t0 + timedelta(hours=3))
    assert driver.validate(old_rejected) == "rejected"
    assert [driver.validate(x) for x in (old_expired, new_expired, old_opened, new_opened)] == ["scheduled"] * 4
    world.confirm(old_opened)
    world.confirm(new_opened)
    assert driver.run(dispatches.OPEN_TASK_TYPE, {"dispatch_row_id": old_opened.row}) == 1
    world.clock.set(t0 + timedelta(minutes=30))
    assert driver.step(dispatches.EXPIRE_TASK_TYPE, old_expired) == "cancelled"
    assert world.dispatch(old_opened)["state"] == "open"

    floor = world.set_floor_now()
    assert floor > 0
    assert driver.validate(new_rejected) == "rejected"
    world.clock.set(t0 + timedelta(hours=1))
    assert driver.step(dispatches.EXPIRE_TASK_TYPE, new_expired) == "cancelled"
    world.clock.set(t0 + timedelta(hours=2))
    assert driver.step(dispatches.OPEN_TASK_TYPE, new_opened) == "open"
    driver.settle()
    world.clock.advance(5)
    driver.settle()

    for d, kind in ((new_rejected, "dispatch_rejected"), (new_expired, "dispatch_cancelled"),
                    (new_opened, "dispatch_opened")):
        _assert_one_each(world, world.texts(kind, d))
        assert [e[3]["notice_type"] for e in world.recorded_for(d)] == [kind]
    for d in (old_rejected, old_expired, old_opened):
        assert world.notices(d=d) == [] and world.recorded_for(d) == []


@pytest.mark.parametrize("kind", ["dispatch_rejected", "dispatch_cancelled", "dispatch_opened"])
def test_sweep_revives_each_detection_without_live_task(world: World, kind: str) -> None:
    """Para cada detecção de disparo, a regra enfileira a tarefa
    `<tipo>:<id>:r1` quando o fato vence; com a tarefa viva, nenhuma outra; com a tarefa terminada em falha
    (sem tarefa viva), a rodada depois do intervalo (`retry_gap`) cria `<tipo>:<id>:r2`, que registra e
    entrega o aviso; depois disso a regra não vence mais."""
    task_type = {"dispatch_rejected": REJECTED_TASK, "dispatch_cancelled": CANCELLED_TASK,
                 "dispatch_opened": OPENED_TASK}[kind]
    world.program("number", preset("number", title=TITLE))
    driver = world.driver()
    t0 = world.clock.now()
    if kind == "dispatch_rejected":
        d = world.prepare(MISSING_QID)
        assert driver.validate(d) == "rejected"
    elif kind == "dispatch_cancelled":
        d = world.prepare("number", opens_at=t0 + timedelta(hours=1))
        assert driver.validate(d) == "scheduled"
        world.clock.set(t0 + timedelta(hours=1))
        assert driver.step(dispatches.EXPIRE_TASK_TYPE, d) == "cancelled"
    else:
        d = world.prepare("number")
        assert driver.validate(d) == "scheduled"
        world.confirm(d)
        assert driver.run(dispatches.OPEN_TASK_TYPE, {"dispatch_row_id": d.row}) == 1

    assert driver.sweep(task_type) == 1
    assert driver.sweep(task_type) == 0
    task = driver.claim(task_type, _subject(task_type, d.row))
    assert task is not None
    assert driver.queue.fail(task.id, WORKER, "FalhaSimulada", permanent=True)
    assert driver.sweep(task_type) == 0
    world.clock.advance(world.settings.backoff_max_seconds + 1)
    assert driver.sweep(task_type) == 1
    driver.settle()
    world.clock.advance(5)

    assert driver.sweep(task_type) == 0
    keys = [r[0] for r in world.sql.execute("SELECT idempotency_key FROM tasks WHERE task_type = %s ORDER BY id",
                                            (task_type,)).fetchall()]
    assert keys == [f"{task_type}:{d.row}:r1", f"{task_type}:{d.row}:r2"]
    _assert_one_each(world, world.texts(kind, d))


# ---------------------------------------------------------------------------------------------
# Truncamento
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["rich", "html"])
@pytest.mark.parametrize("kind", ["dispatch_rejected", "dispatch_opened", "dispatch_cancelled"])
def test_long_title_is_cut_while_numbers_and_command_stay_whole(world: World, kind: str, dialect: str) -> None:
    """Com o título da questão longo (≈ 6 000 caracteres, com `<`, `&`,
    acentos e um caractere fora do BMP), o rejeitado, o aberto e o cancelado cabem no limite do formatador
    (`fits`) e em 4 096 caracteres de markup; só o título é cortado (um prefixo dele, sem o título inteiro),
    e as demais linhas (motivo, contagens, horário, comando) saem inteiras; a Bot API falsa aceita sem erro
    de protocolo nem de política (fixture `rich_bot_api`)."""
    qid = "broken_on_variant" if kind == "dispatch_rejected" else "number"
    world.program(qid, preset(qid, title=LONG_TITLE))
    formatter = TelegramRichFormatter() if dialect == "rich" else TelegramHtmlFormatter()
    driver = world.driver(delivery=DeliverySettings(telegram_format=dialect))
    t0 = world.clock.now()
    if kind == "dispatch_cancelled":
        d = world.prepare(qid, opens_at=t0 + timedelta(hours=1))
        driver.settle()
        world.clock.set(t0 + timedelta(hours=1))
    else:
        d = world.prepare(qid)
        driver.settle()
        if kind == "dispatch_opened":
            world.confirm(d)
    driver.settle()

    if kind == "dispatch_rejected":
        whole = world.rejected_lines(d, qid, None, "question_broken")[1:]
    elif kind == "dispatch_opened":
        whole = world.opened_lines(d, target=0, delivered=0, not_delivered=0, question_failed=0, pending=0)[1:]
    else:
        whole = world.cancelled_lines(d, "expired")[1:]
    for text in _assert_one_each(world, world.texts(kind, d)):
        _assert_lines(text, whole)
        assert LONG_TITLE[:120] in text and LONG_TITLE not in text
        assert _utf16(text) <= 4096 and formatter.escape(text).fits
    for message in world.notice_messages(kind, d):
        assert message.text_format == dialect and message.text is not None
        assert len(message.text) <= 4096
