"""Avisos de falhas do oferecimento: questão, bot sem acesso e convites.

Cobre os avisos de falha do oferecimento, com os cenários de questão que quebra, bot sem acesso e
convites que falham, e as escolhas de esquema e de textos dos avisos (os modelos de
`noticetexts`):
- a correção que quebra (`grading_broken`) para 3 alunos → um único `QUESTION_FAILED` por disparo; a falha
  de preparação depois do "aberto" fica só no relatório;
- o episódio de bot sem acesso com o retorno depois do fechamento (início e fim no fuso, não entregues
  `bot_no_access` dos disparos fechados entre `lost` e `restored`, pendentes no retorno) e antes do
  fechamento (zero não entregues e as pendentes); um resumo de fechamento emitido no
  episódio, que falha de vez, é contado no aviso do retorno, com a orientação `cq reports resend` e `cq
  events list --type notice.recorded`; o arquivamento sem retorno → `not_sent` com evento e nada
  enviado;
- `students.confirm_invites` grava uma `invite_batches` por confirmação e o `batch_id` só nos envios
  `batch`; o lote de 40 com 3 falhas de vez → um aviso `37 enviados e 3 com falha` com `cq events
  list --type student.invite_failed` e `cq invites resend <código> <apelidos>`, sem pseudônimos, com os
  cancelados (já vinculado, substituído) fora das contagens; o lote sem falha →
  nenhum aviso; as falhas da coordenação e do autosserviço em 15 min → um aviso por janela ancorada na
  primeira falha não coberta, enviado no fim da janela;
- os pisos: nenhum aviso de fato anterior à migração (episódio, lote e janela), inclusive a limitação conhecida
  do envio criado antes do deploy que falha depois, com o controle positivo de um episódio novo;
- a reexecução de cada detecção sem duplicar e o `notice.recorded` com `episode_seq`, `batch_id`
  (o `batch_uuid`) e `window_started_at`.

**Montagem.** Como em `test_dispatch_notices.py`: a equipe da fixture `team_members` (pela Bot API
falsa com a extensão do disparo, ou pela entrada simulada do console), os alunos por SQL ou pela lista de alunos
(`students.import`, `invites.prepare_send`, `invites.send`, `invites.resend`), e um arnês passo a passo
(`Driver`) com os tratadores do vínculo do aluno aos relatórios montados como o `run_worker` os monta (validação, abertura,
fechamento, preparo, correção e revelação das entregas; `invite.email` com um canal de verificação
roteirizado, que recusa de vez os endereços escolhidos; no console, o `telegram.update` com
`runtime=None`, para as respostas dos alunos e o `/convite` pela `ConsoleInbox`; os 8 tratadores de
`teamnotices.task_handlers` e o `report.freeze`), a fila de tarefas, a varredura com as regras desses tipos e
a fila de saída montada como a do worker com bots. O bot perde o acesso por `bots.mark_no_access` e volta
pela troca do token (`bots.register` com o token girado na Bot API falsa); o "deploy" (os pisos) é a linha
de `team_notice_floors` regravada pelo teste com o maior `seq` e o maior `invite_sends.id` do momento.

Textos: os modelos de `noticetexts` preenchidos pelo teste e conferidos linha a linha, em ordem; os
horários pelo formato da casa, `timezones.format_local` (início e fim no fuso) [I].
Ids internos, `seq`, chaves, apelidos e pseudônimos só por relação.

**Vermelho no esqueleto.** O `Driver` monta os tratadores dos avisos primeiro (`teamnotices.task_handlers` →
`NotImplementedError`); o caso dos lotes sem arnês falha na tabela `invite_batches` ausente.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cq_server import (deliveries, dispatches, grading, noticetexts, previews, reception, reports, scheduler,
                       students, teamnotices)
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.bots import TelegramChannel, mark_no_access
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.outbox import Outbox
from cq_server.ports.verification import (Delivered, PermanentFailure, VerificationCapabilities,
                                          VerificationMessage)
from cq_server.tasks import ClaimedTask, TaskQueue, enqueue_in_transaction
from cq_server.timezones import format_local
from tests.helpers.api import (ADMIN_TOKEN, all_events, auth, confirm_dispatch, get_report, import_students,
                               prepare_dispatch, prepare_invites, register_bot, register_person, resend_invites,
                               send_invites)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import read_lines
from tests.helpers.engine import preset
from tests.helpers.team import ACTIVE, CONSOLE, Team, team_messages

REPO = "https://repos.cq.test/avisos/oferecimento.git"
COMMIT = "0ffe" * 10
TITLE = "Corrente elétrica no circuito"
WORKER = "worker-avisos-oferecimento"
CRASHED_WORKER = "worker-caido"
NUMBER_ANSWER = "6.5"  # a resposta certa do preset `number` (e do `broken_on_grade`)
WINDOW = timedelta(seconds=900)  # teamnotices.INVITE_FAILURE_WINDOW_SECONDS
OPEN_WAIT = timedelta(seconds=600)  # teamnotices.OPEN_NOTICE_WAIT_SECONDS
QUESTION_FAILED_TASK = "notice.question_failed"
ACCESS_TASK = "notice.access_episode"
BATCH_TASK = "notice.invite_batch"
WINDOW_TASK = "notice.invite_window"
SEND_TASK = "notice.send"
# A variante quebrada na preparação (o motor responde `broken` à `create_variant`; como no disparo).
BROKEN_VARIANT = {"outcome": "broken", "warnings": [],
                  "reason": {"code": "question_code_error", "phase": "generate",
                             "message": "O código da questão falhou ao gerar a variante."}}

_codes = itertools.count(1)

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
    chat: int


@dataclasses.dataclass(frozen=True)
class Listed:
    """Um aluno da lista importada: o e-mail, o apelido, o pseudônimo e a matrícula."""

    email: str
    alias: str
    pseudo_id: str
    enrollment_row: int


class MailChannel:
    """Canal de verificação roteirizado (porta `VerificationChannel`): recusa de vez (`PermanentFailure`,
    categoria `recipient`) os endereços de `refused` e aceita os demais; registra os endereços enviados."""

    def __init__(self, refused: Sequence[str] = ()) -> None:
        self.capabilities = VerificationCapabilities("email")
        self.refused = {address.lower() for address in refused}
        self.sent: list[str] = []
        self._refs = itertools.count(1)

    def send(self, message: VerificationMessage) -> Any:
        address = message.address.lower()
        self.sent.append(address)
        if address in self.refused:
            return PermanentFailure("recipient", "SMTPRecipientsRefused")
        return Delivered(f"msg-{next(self._refs)}@cq.example.org")


def _rule(task_type: str) -> Any:
    found = [rule for rule in scheduler.REPORT_RULES if rule.task_type == task_type]
    assert len(found) == 1, f"regra {task_type} ausente de scheduler.REPORT_RULES"
    return found[0]


def _subject(task_type: str, row_id: int) -> dict:
    return {_rule(task_type).subject_key: row_id}


def _assert_lines(text: str, lines: Sequence[str]) -> None:
    position = 0
    for line in lines:
        found = text.find(line, position)
        assert found >= 0, f"linha ausente ou fora de ordem no aviso: {line!r}\n---\n{text}"
        position = found + len(line)


class World:
    """O oferecimento da equipe simulada, os alunos, os disparos e os convites do teste."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any,
                 bots: Any, rich: Any, engine_fake: Any, repo_fetcher: Any, team: Team) -> None:
        self.client, self.sql, self.db_url, self.clock, self.settings = client, sql, db_url, clock, settings
        self.bots, self.rich, self.engine_fake, self.fetcher, self.team = bots, rich, engine_fake, repo_fetcher, team
        self.offering = team.offering
        self.code = team.code
        self.bot = team.bot_id
        self.bot_row = team.bot_row
        self.tz = team.offering["timezone"]
        self.course = team.offering["course"]
        self.offering_row = int(sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                            (team.offering["offering_id"],)).fetchone()[0])
        self._users = itertools.count(1)
        self._listed = itertools.count(1)
        self.drivers: list[Driver] = []

    def close(self) -> None:
        for driver in self.drivers:
            driver.close()

    def driver(self, **kwargs: Any) -> Driver:
        driver = Driver(self, **kwargs)
        self.drivers.append(driver)
        return driver

    # -- alunos e disparos ---------------------------------------------------------------------

    def program(self, qid: str, question: Any) -> None:
        self.engine_fake.program((REPO, COMMIT, qid), question)

    def student(self) -> Student:
        """Um aluno vinculado (matrícula `student` `active`, termo aceito) com endereço ativo no bot."""
        n = next(self._users)
        user = make_user(74_000 + n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Avisos Oferta {n}")
        person_row = int(self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                          (body["pseudo_id"],)).fetchone()[0])
        now = self.clock.now()
        self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
            (person_row, self.offering_row, now, now, now))
        endpoint_row = int(self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, self.bot_row, user.user_id, now, now)).fetchone()[0])
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        return Student(body["pseudo_id"], person_row, endpoint_row, user.user_id)

    def prepare(self, qid: str, *, closes_at: datetime | None = None) -> Disp:
        closes_at = closes_at if closes_at is not None else self.clock.now() + timedelta(hours=2)
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(closes_at))
        row = int(self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                                   (body["dispatch_id"],)).fetchone()[0])
        return Disp(body["dispatch_id"], row)

    def confirm(self, d: Disp) -> dict:
        return confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)

    def dispatch(self, d: Disp) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM dispatches WHERE id = %s", (d.row,)).fetchone()

    def deliveries(self, d: Disp) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM deliveries WHERE dispatch_id = %s ORDER BY id", (d.row,)).fetchall()

    # -- bot sem acesso ------------------------------------------------------------------------

    def lose_access(self) -> int:
        """O bot perde o acesso (`bots.mark_no_access`, `token_rejected`); devolve o `seq` do `lost`."""
        version = int(self.sql.execute("SELECT token_version FROM bots WHERE id = %s", (self.bot_row,)).fetchone()[0])
        with psycopg.connect(self.db_url, autocommit=True) as conn:
            assert mark_no_access(conn, bot_id=self.bot_row, token_version=version, problem="token_rejected",
                                  now=self.clock.now()) is True
        return self.access_events()[-1]["seq"]

    def restore_access(self) -> int:
        """O acesso volta pela troca do token (`bots.register` com o token girado); devolve o `seq` do
        `restored`."""
        register_bot(self.client, ADMIN_TOKEN, self.code, self.rich.rotate_token(self.bot))
        last = self.access_events()[-1]
        assert last["data"]["access"] == "restored"
        return last["seq"]

    def access_events(self) -> list[dict]:
        return [e for e in all_events(self.client, ADMIN_TOKEN, "bot.access_changed")
                if e["data"]["offering_id"] == self.offering["offering_id"]]

    def archive(self) -> None:
        for path, body, op in ((f"/v1/offerings/{self.code}/close", None, "close"),
                               (f"/v1/offerings/{self.code}/archive/prepare", None, "prepare")):
            resp = self.client.post(path, headers=auth(ADMIN_TOKEN), json=body)
            assert resp.status_code == 200, f"{op}: {resp.status_code} {resp.text[:300]}"
            if op == "prepare":
                code = resp.json()["confirmation_code"]
        resp = self.client.post(f"/v1/offerings/{self.code}/archive", headers=auth(ADMIN_TOKEN),
                                json={"confirmation_code": code})
        assert resp.status_code == 200, f"archive: {resp.status_code} {resp.text[:300]}"

    # -- convites ------------------------------------------------------------------------------

    def import_list(self, count: int) -> list[Listed]:
        """`count` alunos na lista (nomes, RAs e e-mails neutros); devolve-os na ordem."""
        rows = []
        for _ in range(count):
            n = next(self._listed)
            rows.append({"name": f"Aluno Lista Avisos {n}", "ra": f"66{n:08d}",
                         "email": f"aluno.lista.avisos.{n}@example.edu"})
        import_students(self.client, ADMIN_TOKEN, self.code, rows)
        out = []
        for row in rows:
            found = self.sql.execute(
                "SELECT p.alias, p.pseudo_id::text, e.id FROM identities i JOIN people p ON p.id = i.person_id "
                "JOIN enrollments e ON e.person_id = p.id AND e.offering_id = %s WHERE i.email_normalized = %s",
                (self.offering_row, row["email"])).fetchone()
            assert found is not None, "aluno da lista sem matrícula"
            out.append(Listed(row["email"], found[0], found[1], int(found[2])))
        return out

    def confirm_batch(self) -> None:
        prepared = prepare_invites(self.client, ADMIN_TOKEN, self.code)
        send_invites(self.client, ADMIN_TOKEN, self.code, prepared["confirmation_code"])

    def resend(self, listed: Sequence[Listed]) -> None:
        resend_invites(self.client, ADMIN_TOKEN, self.code, [x.alias for x in listed])

    def sends(self) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT s.* FROM invite_sends s JOIN enrollments e ON e.id = s.enrollment_id "
                               "WHERE e.offering_id = %s ORDER BY s.id", (self.offering_row,)).fetchall()

    def set_floors_now(self) -> None:
        """O "deploy" agora."""
        self.sql.execute("UPDATE team_notice_floors SET event_floor = (SELECT coalesce(max(seq), 0) FROM events), "
                         "invite_send_floor = (SELECT coalesce(max(id), 0) FROM invite_sends) WHERE id = 1")

    # -- avisos --------------------------------------------------------------------------------

    def notices(self, kind: str | None = None) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM team_notices WHERE offering_id = %s "
                               "AND (%s::text IS NULL OR kind = %s::text) ORDER BY id",
                               (self.offering_row, kind, kind)).fetchall()

    def texts(self, notice_id: int, *, stream: io.StringIO | None = None) -> list[str]:
        """O texto recebido por cada membro com endereço ativo do aviso, na ordem dos membros; cada um
        recebeu exatamente uma mensagem (`sent`)."""
        lines = {line.message_id: line for line in read_lines(stream.getvalue())} if stream is not None else {}
        by_member: dict[str, list[str]] = {m.pseudo_id: [] for m in self.team.active}
        for message in team_messages(self.sql, notice_id=notice_id):
            member = next(m for m in self.team.members if m.pseudo_id == message.recipient)
            assert message.state == "sent", f"aviso não enviado a {member.label}: {message.state}"
            if stream is not None:
                text = lines[message.message_id].text
            else:
                text = self.rich.current(self.bot, member.chat_id, int(message.channel_ref)).plain_text or ""
            by_member[member.pseudo_id].append(text)
        assert all(len(items) == 1 for items in by_member.values()), by_member
        return [by_member[m.pseudo_id][0] for m in self.team.active]

    def recorded(self) -> list[dict]:
        return [e["data"] for e in all_events(self.client, ADMIN_TOKEN, "notice.recorded")
                if e["data"]["offering_id"] == self.offering["offering_id"]]

    def recorded_event(self, kind: str, *, state: str = "queued", count: int | None = None, **subject: Any) -> dict:
        data = {"offering_id": self.offering["offering_id"], "notice_type": kind, "dispatch_id": None,
                "report_version": None, "episode_seq": None, "batch_id": None, "window_started_at": None,
                "local_date": None, "state": state,
                "recipient_count": len(self.team.active) if count is None else count}
        data.update(subject)
        return data


class Driver:
    """O worker passo a passo, sem thread (ver o docstring do módulo)."""

    def __init__(self, w: World, *, console: bool = False, mail: Any = None) -> None:
        self.w = w
        self.stack = contextlib.ExitStack()
        self.stream = io.StringIO()
        self.delivery = DeliverySettings()
        try:
            self._build(console, mail)
        except BaseException:
            self.stack.close()
            raise

    def _build(self, console: bool, mail: Any) -> None:
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
        engine = HttpQuestionEngine(EngineSettings(engine_url=w.engine_fake.base_url, timeout_seconds=10.0))
        self.stack.callback(engine.close)
        base = {"clock": w.clock, "settings": w.settings}
        built: dict[str, Any] = {
            dispatches.VALIDATE_TASK_TYPE: dispatches.validate_task_handler(w.db_url, engine=engine, fetcher=w.fetcher,
                                                                            channel=self.channel, **base),
            dispatches.EXPIRE_TASK_TYPE: dispatches.expire_task_handler(w.db_url, **base),
            dispatches.OPEN_TASK_TYPE: dispatches.open_task_handler(w.db_url, **base),
            dispatches.CLOSE_TASK_TYPE: dispatches.close_task_handler(w.db_url, channel=self.channel, **base),
            deliveries.PREPARE_TASK_TYPE: deliveries.prepare_task_handler(w.db_url, engine=engine,
                                                                          channel=self.channel, **base),
            deliveries.REVEAL_TASK_TYPE: deliveries.reveal_task_handler(w.db_url, channel=self.channel, **base),
            grading.GRADE_TASK_TYPE: grading.grade_task_handler(w.db_url, engine=engine, channel=self.channel, **base),
            students.INVITE_EMAIL_TASK_TYPE: students.invite_email_task_handler(w.db_url, channel=mail, **base),
        }
        if console:
            built[reception.UPDATE_TASK_TYPE] = reception.update_task_handler(
                w.db_url, runtime=None, channel=self.channel, delivery=self.delivery, **base)
        for handler in built.values():
            self.stack.callback(handler.close)
        self.handlers: dict[str, Any] = {**built, **r6, reports.FREEZE_TASK_TYPE: freeze}
        self.rules = [rule for rule in scheduler.DISPATCH_RULES + scheduler.REPORT_RULES if rule.task_type in self.handlers]
        self.queue = TaskQueue(w.db_url, **base)
        self.stack.callback(self.queue.close)
        self.outbox = Outbox(w.db_url, content=deliveries.DeliveryContentLoader(self.channel, delivery=self.delivery),
                             outcome=deliveries.DeliveryOutcomeRecorder(self.channel, clock=w.clock),
                             notice_content=previews.NoticeContentLoader(), **base)
        self.stack.callback(self.outbox.close)

    def close(self) -> None:
        self.stack.close()

    def sweep(self, *task_types: str) -> int:
        rules = self.rules if not task_types else [r for r in self.rules if r.task_type in task_types]
        result = scheduler.sweep_once(self.w.sql, now=self.w.clock.now(),
                                      retry_gap=float(self.w.settings.backoff_max_seconds), rules=rules)
        return sum(result.enqueued.values()) + result.abandoned

    def work(self, *, limit: int = 800) -> int:
        done = 0
        while self.queue.work_once(WORKER, self.handlers, task_types=list(self.handlers)) is not None:
            done += 1
            assert done <= limit, "o arnês processou tarefas demais"
        return done

    def send(self, *, limit: int = 800) -> int:
        sent = 0
        while self.outbox.dispatch_once(self.channel, WORKER, include_routed=True) is not None:
            sent += 1
            assert sent <= limit, "o arnês despachou mensagens demais"
        return sent

    def _next_due(self) -> datetime | None:
        now = self.w.clock.now()
        row = self.w.sql.execute(
            "SELECT min(at) FROM (SELECT min(available_at) AS at FROM tasks WHERE state = 'pending' "
            "AND task_type = ANY(%s) AND available_at > %s UNION ALL SELECT min(available_at) FROM outbox_messages "
            "WHERE state = 'pending' AND available_at > %s) AS due", (list(self.handlers), now, now)).fetchone()
        return row[0]

    def settle(self, *, advance: bool = False, rounds: int = 80) -> None:
        for _ in range(rounds):
            if self.work() + self.send() + self.sweep():
                continue
            if not advance:
                return
            due = self._next_due()
            if due is None:
                return
            self.w.clock.set(due)
        raise AssertionError(f"o arnês não sossegou em {rounds} rodadas")

    def claim(self, task_type: str, subject: dict, *, owner: str = WORKER) -> ClaimedTask | None:
        now = self.w.clock.now()
        row = self.w.sql.execute(_CLAIM_SQL, {"owner": owner, "until": now + timedelta(seconds=self.w.settings.lease_seconds),
                                              "type": task_type, "now": now, "subject": Jsonb(subject)}).fetchone()
        return None if row is None else ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2],
                                                    attempts=int(row[3]))

    def rerun(self, task_type: str, subject: dict, key: str) -> None:
        """A reexecução da detecção depois do commit (outra tarefa do mesmo assunto, com outra chave)."""
        with psycopg.connect(self.w.db_url) as conn:
            enqueue_in_transaction(conn, task_type, subject, now=self.w.clock.now(), idempotency_key=key)
        task = self.claim(task_type, subject)
        assert task is not None
        self.handlers[task_type](task)
        assert self.queue.complete(task.id, WORKER)


@pytest.fixture
def world(rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, db_url: str, clock: Any,
          settings: Any, bots: Any, engine_fake: Any, repo_fetcher: Any) -> Iterator[World]:
    w = World(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, bots=bots, rich=rich_bot_api,
              engine_fake=engine_fake, repo_fetcher=repo_fetcher, team=team_members)
    try:
        yield w
    finally:
        w.close()


def _assert_rerun_is_inert(w: World, driver: Driver, task_type: str, row_id: int, notice_id: int) -> None:
    """com o aviso registrado, a regra não vence mais e a reexecução da tarefa não muda nada."""
    before = ([(m.message_id, m.state) for m in team_messages(w.sql, notice_id=notice_id)], w.recorded())
    w.clock.advance(5)
    assert driver.sweep(task_type) == 0
    driver.rerun(task_type, _subject(task_type, row_id), f"{task_type}:{row_id}:reexecucao")
    driver.settle()
    assert ([(m.message_id, m.state) for m in team_messages(w.sql, notice_id=notice_id)], w.recorded()) == before


# ---------------------------------------------------------------------------------------------
# Falha de questão
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}], indirect=True)
def test_grading_broken_for_three_students_sends_a_single_question_failed_notice(world: World) -> None:
    """A correção que quebra (`grading_broken`) para os 3 alunos que
    responderam (pelo console, em resposta à mensagem da questão) → um único aviso `question_failed` por
    disparo, a cada membro, com o título e `cq reports show <código> <dispatch_id>`, sem detalhe técnico, e um
    `notice.recorded`; a reexecução da detecção não duplica."""
    world.program("broken_on_grade", preset("broken_on_grade", title=TITLE))
    students_ = [world.student() for _ in range(3)]
    driver = world.driver(console=True)
    d = world.prepare("broken_on_grade")
    driver.settle()
    world.confirm(d)
    driver.settle()
    assert [r["state"] for r in world.deliveries(d)] == ["delivered"] * 3
    inbox = ConsoleInbox(world.db_url, clock=world.clock)
    try:
        for student, delivery in zip(students_, world.deliveries(d)):
            inbox.send_text(world.bot_row, student.chat, NUMBER_ANSWER, reply_to=str(delivery["principal_message_id"]))
    finally:
        inbox.close()

    driver.settle()

    assert [(r["state"], r["failure"]) for r in world.deliveries(d)] == [("question_failed", "grading_broken")] * 3
    [notice] = world.notices("question_failed")
    assert (notice["subject_key"], notice["dispatch_id"], notice["state"]) == (f"dispatch:{d.row}", d.row, "queued")
    expected = noticetexts.QUESTION_FAILED.format(title=TITLE, code=world.code, dispatch_id=d.id).split("\n")
    for text in world.texts(notice["id"], stream=driver.stream):
        _assert_lines(text, expected)
        assert "technical_detail" not in text and "Traceback" not in text
    assert [r for r in world.recorded() if r["notice_type"] == "question_failed"] == [
        world.recorded_event("question_failed", dispatch_id=d.id)]
    _assert_rerun_is_inert(world, driver, QUESTION_FAILED_TASK, d.row, notice["id"])


def test_preparation_failure_after_the_opened_notice_is_only_in_the_report(world: World) -> None:
    """Com o motor fora do ar na abertura, o "aberto" sai aos
    10 min com a entrega pendente; com o motor de volta, a variante sai quebrada na preparação
    (`variant_broken`): nenhum aviso `question_failed` (só `grading_broken` avisa), e a falha aparece no
    relatório (`reach.question_failed_by_stage.preparation` 1)."""
    world.program("number", preset("number", title=TITLE))
    world.student()
    driver = world.driver()
    d = world.prepare("number")
    driver.settle()
    world.engine_fake.stop()
    world.confirm(d)
    driver.settle()
    world.clock.set(world.dispatch(d)["opened_at"] + OPEN_WAIT)
    driver.settle()
    assert [n["kind"] for n in world.notices()] == ["dispatch_opened"]

    world.engine_fake.start()
    world.engine_fake.inject("create_variant", body=BROKEN_VARIANT, times=1)
    world.clock.advance(world.settings.backoff_max_seconds + 1)
    driver.settle(advance=True)
    world.clock.advance(5)
    driver.settle()

    assert [(r["state"], r["failure"]) for r in world.deliveries(d)] == [("question_failed", "variant_broken")]
    assert [n["kind"] for n in world.notices()] == ["dispatch_opened"]
    assert driver.sweep(QUESTION_FAILED_TASK) == 0
    report = get_report(world.client, world.code, d.id)
    assert report["reach"]["question_failed_by_stage"] == {"preparation": 1, "grading": 0}


# ---------------------------------------------------------------------------------------------
# Bot sem acesso
# ---------------------------------------------------------------------------------------------


def _access_lines(w: World, *, started: datetime, ended: datetime, not_delivered: int, pending: int,
                  lost_notices: int, lost_summaries: int) -> list[str]:
    lines = noticetexts.ACCESS_RESTORED.format(
        course=w.course, started_at=format_local(started, w.tz), ended_at=format_local(ended, w.tz),
        not_delivered=not_delivered, pending=pending, lost_notices=lost_notices, lost_summaries=lost_summaries,
        code=w.code).split("\n")
    return lines


def test_return_after_closing_reports_the_episode_and_the_summary_lost_in_it(world: World) -> None:
    """O bot perde o acesso com um disparo aberto e 2
    entregas retidas; o disparo fecha no episódio (as 2 viram `not_delivered/bot_no_access`), e o "aberto"
    antecipado e o resumo de fechamento à equipe falham de vez (o bot sem acesso, até esgotar as tentativas).
    Com o acesso de volta (troca do token), um único aviso `bot_access_restored` pelo bot que voltou, a cada
    membro: o início e o fim do período no fuso, 2 entregas não feitas, 0 pendentes, 2 avisos perdidos, 1
    resumo perdido e a orientação de reenvio (`cq reports resend`, `cq events list --type notice.recorded`);
    o `notice.recorded` com o `episode_seq` (o `seq` do `lost`); a reexecução não duplica."""
    world.program("number", preset("number", title=TITLE))
    [world.student() for _ in range(2)]
    driver = world.driver()
    t0 = world.clock.now()
    d = world.prepare("number", closes_at=t0 + timedelta(minutes=5))
    driver.settle()
    world.confirm(d)
    lost_seq = world.lose_access()
    started = world.clock.now()
    driver.settle()
    assert [r["state"] for r in world.deliveries(d)] == ["pending", "pending"]

    world.clock.set(world.dispatch(d)["closes_at"])
    driver.settle()
    driver.settle(advance=True)
    assert world.dispatch(d)["state"] == "closed"
    assert [(r["state"], r["undelivered_reason"]) for r in world.deliveries(d)] == [("not_delivered", "bot_no_access")] * 2
    lost = [m for m in team_messages(world.sql) if m.kind in ("dispatch_opened", "dispatch_closed")]
    assert sorted({m.kind for m in lost}) == ["dispatch_closed", "dispatch_opened"]
    assert all(m.state == "failed" for m in lost)

    world.clock.advance(60)
    ended = world.clock.now()
    world.restore_access()
    driver.settle()

    [notice] = world.notices("bot_access_restored")
    assert (notice["subject_key"], notice["state"], notice["dispatch_id"]) == (f"episode:{lost_seq}", "queued", None)
    expected = _access_lines(world, started=started, ended=ended, not_delivered=2, pending=0, lost_notices=2,
                             lost_summaries=1)
    for text in world.texts(notice["id"]):
        _assert_lines(text, expected)
    assert [r for r in world.recorded() if r["notice_type"] == "bot_access_restored"] == [
        world.recorded_event("bot_access_restored", episode_seq=lost_seq)]
    _assert_rerun_is_inert(world, driver, ACCESS_TASK, world.offering_row, notice["id"])


def test_return_before_closing_reports_zero_not_delivered_and_the_pending(world: World) -> None:
    """Com o acesso de volta antes do fechamento, o aviso do retorno traz 0 entregas não
    feitas e as 2 pendentes no retorno, sem avisos nem resumos perdidos e, portanto, sem a linha da
    orientação de reenvio; depois, as entregas retidas saem pelo bot que voltou."""
    world.program("number", preset("number", title=TITLE))
    [world.student() for _ in range(2)]
    driver = world.driver()
    d = world.prepare("number")
    driver.settle()
    world.confirm(d)
    world.lose_access()
    started = world.clock.now()
    driver.settle()
    assert [r["state"] for r in world.deliveries(d)] == ["pending", "pending"]
    world.clock.advance(120)
    ended = world.clock.now()
    world.restore_access()

    assert driver.sweep(ACCESS_TASK) == 1
    driver.work()
    driver.settle()

    [notice] = world.notices("bot_access_restored")
    lines = _access_lines(world, started=started, ended=ended, not_delivered=0, pending=2, lost_notices=0,
                          lost_summaries=0)
    for text in world.texts(notice["id"]):
        _assert_lines(text, lines[:-1])
        assert lines[-1] not in text
    assert [r["state"] for r in world.deliveries(d)] == ["delivered", "delivered"]


def test_archived_without_return_records_not_sent_and_sends_nothing(world: World) -> None:
    """O oferecimento arquivado com o bot ainda sem acesso → o episódio é
    registrado `not_sent`, com `notice.recorded` (`episode_seq`, 0 destinatários) e nenhuma mensagem."""
    lost_seq = world.lose_access()
    driver = world.driver()
    world.archive()

    driver.settle()
    world.clock.advance(5)
    driver.settle()

    [notice] = world.notices("bot_access_restored")
    assert (notice["subject_key"], notice["state"], notice["recipient_count"]) == (f"episode:{lost_seq}", "not_sent", 0)
    assert team_messages(world.sql, notice_id=notice["id"]) == []
    assert world.recorded() == [world.recorded_event("bot_access_restored", state="not_sent", count=0,
                                                     episode_seq=lost_seq)]


# ---------------------------------------------------------------------------------------------
# Convites
# ---------------------------------------------------------------------------------------------


def test_each_confirmation_creates_one_batch_and_only_batch_sends_carry_it(world: World) -> None:
    """Cada `invites.send` confirmado cria uma `invite_batches` (com
    `batch_uuid` próprio, no oferecimento, no horário da confirmação) e grava o `batch_id` dela em cada envio
    `batch` do lote; o reenvio da coordenação não tem lote (`batch_id` nulo)."""
    first = world.import_list(2)
    world.confirm_batch()
    world.clock.advance(60)
    world.import_list(1)
    world.confirm_batch()
    world.resend(first[:1])

    batches = world.sql.execute("SELECT id, batch_uuid, offering_id, created_at FROM invite_batches ORDER BY id").fetchall()
    assert [(b[2], b[3]) for b in batches] == [(world.offering_row, world.clock.now() - timedelta(seconds=60)),
                                               (world.offering_row, world.clock.now())]
    assert len({b[1] for b in batches}) == 2
    by_origin = [(s["origin"], s["batch_id"]) for s in world.sends()]
    assert by_origin == [("batch", batches[0][0]), ("batch", batches[0][0]), ("batch", batches[1][0]),
                         ("coordination", None)]


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}, (ACTIVE, ACTIVE)],
                         ids=["console", "telegram"], indirect=True)
def test_batch_of_forty_with_three_failures_sends_one_aggregated_notice(world: World) -> None:
    """Um lote de 42 convites, em que 1 é substituído por um reenvio da
    coordenação e 1 aluno se vincula antes do envio (os 2 cancelados, fora das contagens), e 3 endereços
    recusados de vez → um único aviso `invite_failures` do lote (`batch:<id>`), quando nenhum envio dele está
    pendente: `37 enviados e 3 com falha`, com `cq events list --type student.invite_failed` e `cq invites
    resend <código> <apelidos>`, sem apelido nem pseudônimo de aluno; `notice.recorded` com o `batch_uuid`;
    pelo console e pela Bot API falsa; a reexecução não duplica."""
    console = world.team.channel == CONSOLE
    listed = world.import_list(42)
    failing = [listed[3], listed[17], listed[29]]
    mail = MailChannel([x.email for x in failing])
    driver = world.driver(console=console, mail=mail)
    world.confirm_batch()
    world.resend([listed[40]])
    now = world.clock.now()
    world.sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                      "terms_accepted_at = %s WHERE id = %s", (now, now, listed[41].enrollment_row))

    driver.settle()

    [(batch_row, batch_uuid)] = world.sql.execute("SELECT id, batch_uuid::text FROM invite_batches").fetchall()
    batch = [s for s in world.sends() if s["batch_id"] == batch_row]
    assert sorted((s["state"], s["cancel_reason"]) for s in batch if s["state"] != "sent") == sorted(
        [("failed", None)] * 3 + [("cancelled", "superseded"), ("cancelled", "already_linked")])
    assert sum(1 for s in batch if s["state"] == "sent") == 37
    [notice] = world.notices("invite_failures")
    assert (notice["subject_key"], notice["state"], notice["dispatch_id"]) == (f"batch:{batch_row}", "queued", None)
    expected = noticetexts.INVITE_FAILURES_BATCH.format(course=world.course, sent=37, failed=3,
                                                        code=world.code).split("\n")
    for text in world.texts(notice["id"], stream=driver.stream if console else None):
        _assert_lines(text, expected)
        for student in listed:
            assert student.alias not in text and student.pseudo_id not in text and student.email not in text
    assert world.recorded() == [world.recorded_event("invite_failures", batch_id=batch_uuid)]
    _assert_rerun_is_inert(world, driver, BATCH_TASK, batch_row, notice["id"])


def test_batch_without_failure_sends_no_notice(world: World) -> None:
    """O lote em que todos os envios saem não gera aviso nem registro."""
    world.import_list(3)
    driver = world.driver(mail=MailChannel())
    world.confirm_batch()

    driver.settle()
    world.clock.advance(WINDOW.total_seconds() + 5)
    driver.settle()

    assert [s["state"] for s in world.sends()] == ["sent"] * 3
    assert world.notices() == [] and world.recorded() == []


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}], indirect=True)
def test_coordination_and_self_service_failures_are_grouped_by_anchored_fifteen_minute_windows(world: World) -> None:
    """Uma falha do reenvio da coordenação em `t0` e uma do autosserviço
    (`/convite <e-mail>` pela entrada simulada do console) em `t0 + 5 min` → nada antes do fim da janela;
    em `t0 + 15 min`, um aviso `invite_failures` (`window:<id da primeira falha>`) com 2 falhas entre o início
    e o fim da janela no fuso; a falha seguinte, em `t0 + 20 min`, não coberta pela janela anterior, abre
    outra janela, avisada em `t0 + 35 min` com 1 falha. Cada `notice.recorded` com o `window_started_at` (o
    `finished_at` da primeira falha da janela); a reexecução não duplica."""
    a, b, c = world.import_list(3)
    mail = MailChannel([a.email, b.email, c.email])
    driver = world.driver(console=True, mail=mail)
    t0 = world.clock.now()
    world.resend([a])
    driver.settle()
    world.clock.set(t0 + timedelta(minutes=5))
    inbox = ConsoleInbox(world.db_url, clock=world.clock)
    try:
        inbox.send_text(world.bot_row, make_user(79_001).user_id, f"/convite {b.email}")
    finally:
        inbox.close()
    driver.settle()
    sends = world.sends()
    assert [(s["origin"], s["state"], s["finished_at"]) for s in sends] == [
        ("coordination", "failed", t0), ("self_service", "failed", t0 + timedelta(minutes=5))]

    world.clock.set(t0 + WINDOW - timedelta(seconds=1))
    driver.settle()
    assert world.notices("invite_failures") == []
    world.clock.set(t0 + WINDOW)
    driver.settle()
    world.clock.set(t0 + timedelta(minutes=20))
    world.resend([c])
    driver.settle()
    world.clock.set(t0 + timedelta(minutes=20) + WINDOW)
    driver.settle()

    first, second = world.notices("invite_failures")
    assert (first["subject_key"], second["subject_key"]) == (f"window:{sends[0]['id']}", f"window:{world.sends()[2]['id']}")
    windows = ((first, 2, t0), (second, 1, t0 + timedelta(minutes=20)))
    for notice, failed, start in windows:
        expected = noticetexts.INVITE_FAILURES_WINDOW.format(
            course=world.course, failed=failed, window_start=format_local(start, world.tz),
            window_end=format_local(start + WINDOW, world.tz), code=world.code).split("\n")
        for text in world.texts(notice["id"], stream=driver.stream):
            _assert_lines(text, expected)
    assert world.recorded() == [world.recorded_event("invite_failures", window_started_at=iso(t0)),
                                world.recorded_event("invite_failures",
                                                     window_started_at=iso(t0 + timedelta(minutes=20)))]
    _assert_rerun_is_inert(world, driver, WINDOW_TASK, world.offering_row, second["id"])


# ---------------------------------------------------------------------------------------------
# Pisos
# ---------------------------------------------------------------------------------------------


def test_facts_before_the_deploy_never_notify_and_a_new_episode_does(world: World) -> None:
    """Com o "deploy" gravado depois de (a) um episódio de bot sem
    acesso com perda e retorno, (b) um lote com uma falha de vez e (c) um reenvio da coordenação criado mas
    ainda não enviado, nenhum aviso sai: nem do episódio, nem do lote, nem da janela da falha do reenvio, que
    só acontece depois do deploy (a limitação conhecida do envio criado antes do deploy). O controle
    positivo: um episódio novo, depois do deploy, avisa."""
    listed = world.import_list(2)
    mail = MailChannel([listed[0].email, listed[1].email])
    driver = world.driver(mail=mail)
    world.lose_access()
    world.clock.advance(30)
    world.restore_access()
    world.confirm_batch()
    driver.work()  # só as tarefas do lote: nenhuma rodada da varredura antes do "deploy"
    world.resend(listed[1:])
    world.set_floors_now()

    driver.settle()
    world.clock.advance(WINDOW.total_seconds() + 5)
    driver.settle()
    assert [s["state"] for s in world.sends()] == ["failed", "failed", "failed"]
    assert world.notices() == [] and world.recorded() == []

    new_lost = world.lose_access()
    world.clock.advance(30)
    world.restore_access()
    driver.settle()
    [notice] = world.notices()
    assert (notice["kind"], notice["subject_key"]) == ("bot_access_restored", f"episode:{new_lost}")
    assert [r["notice_type"] for r in world.recorded()] == ["bot_access_restored"]


# ---------------------------------------------------------------------------------------------
# A varredura que reanima
# ---------------------------------------------------------------------------------------------


@contextlib.contextmanager
def _rule_held(driver: Driver, task_type: str) -> Iterator[None]:
    """A montagem do fato sem a regra `task_type` nas rodadas da varredura do arnês (as demais rodam): o
    fato só vence a regra quando o teste a roda, e a 1ª tarefa dela é a do teste."""
    rules = driver.rules
    driver.rules = [rule for rule in rules if rule.task_type != task_type]
    try:
        yield
    finally:
        driver.rules = rules


def _assert_revived_once(w: World, driver: Driver, task_type: str, row_id: int, kind: str, *,
                         stream: io.StringIO | None = None) -> dict:
    """No molde de `test_dispatch_notices.py::
    test_sweep_revives_each_detection_without_live_task`: com o fato montado e nenhuma tarefa de
    `task_type`, a regra enfileira `<tipo>:<id>:r1`; com ela viva, nenhuma outra; a tarefa termina em
    falha (sem concluir e sem efeito) e, sem tarefa viva, a rodada antes do intervalo (`retry_gap`) não
    cria nada e a seguinte cria `<tipo>:<id>:r2` (uma só), que registra e entrega o aviso. Depois disso a
    regra não vence mais: um aviso `kind`, uma mensagem `sent` por membro com endereço ativo e um
    `notice.recorded` desse tipo. Devolve o aviso."""
    assert w.notices(kind) == []
    assert w.sql.execute("SELECT count(*) FROM tasks WHERE task_type = %s", (task_type,)).fetchone()[0] == 0
    assert driver.sweep(task_type) == 1
    assert driver.sweep(task_type) == 0
    task = driver.claim(task_type, _subject(task_type, row_id))
    assert task is not None
    assert driver.queue.fail(task.id, WORKER, "FalhaSimulada", permanent=True)
    assert w.notices(kind) == []
    assert driver.sweep(task_type) == 0
    w.clock.advance(w.settings.backoff_max_seconds + 1)
    assert driver.sweep(task_type) == 1
    driver.settle()
    w.clock.advance(5)
    driver.settle()

    assert driver.sweep(task_type) == 0
    rows = w.sql.execute("SELECT idempotency_key, payload, state FROM tasks WHERE task_type = %s ORDER BY id",
                         (task_type,)).fetchall()
    assert rows == [(f"{task_type}:{row_id}:r1", _subject(task_type, row_id), "failed"),
                    (f"{task_type}:{row_id}:r2", _subject(task_type, row_id), "done")]
    [notice] = w.notices(kind)
    assert (notice["state"], notice["recipient_count"]) == ("queued", len(w.team.active))
    assert len(w.texts(notice["id"], stream=stream)) == len(w.team.active)
    assert len(team_messages(w.sql, notice_id=notice["id"])) == len(w.team.active)
    assert [r["notice_type"] for r in w.recorded()].count(kind) == 1
    return notice


@pytest.mark.parametrize("team_members", [{"states": (ACTIVE, ACTIVE), "channel": CONSOLE}], indirect=True)
def test_sweep_revives_the_question_failed_detection_without_live_task(world: World) -> None:
    """A correção que quebra (`grading_broken`) para 1 aluno
    (resposta pelo console), montada sem a regra `notice.question_failed`; a tarefa r1 da regra (assunto: o
    disparo) termina em falha, a varredura a reanima uma vez (r2), e o aviso `question_failed` do disparo
    sai uma vez a cada membro, com o `notice.recorded` do disparo."""
    world.program("broken_on_grade", preset("broken_on_grade", title=TITLE))
    student = world.student()
    driver = world.driver(console=True)
    with _rule_held(driver, QUESTION_FAILED_TASK):
        d = world.prepare("broken_on_grade")
        driver.settle()
        world.confirm(d)
        driver.settle()
        [delivery] = world.deliveries(d)
        assert delivery["state"] == "delivered"
        inbox = ConsoleInbox(world.db_url, clock=world.clock)
        try:
            inbox.send_text(world.bot_row, student.chat, NUMBER_ANSWER, reply_to=str(delivery["principal_message_id"]))
        finally:
            inbox.close()
        driver.settle()
    assert [(r["state"], r["failure"]) for r in world.deliveries(d)] == [("question_failed", "grading_broken")]

    notice = _assert_revived_once(world, driver, QUESTION_FAILED_TASK, d.row, "question_failed", stream=driver.stream)

    assert (notice["subject_key"], notice["dispatch_id"]) == (f"dispatch:{d.row}", d.row)
    expected = noticetexts.QUESTION_FAILED.format(title=TITLE, code=world.code, dispatch_id=d.id).split("\n")
    for text in world.texts(notice["id"], stream=driver.stream):
        _assert_lines(text, expected)
    assert [r for r in world.recorded() if r["notice_type"] == "question_failed"] == [
        world.recorded_event("question_failed", dispatch_id=d.id)]


def test_sweep_revives_the_access_episode_detection_without_live_task(world: World) -> None:
    """Um episódio de bot sem acesso com o retorno (troca do
    token) e nenhum disparo; a tarefa r1 da regra `notice.access_episode` (assunto: o oferecimento) termina
    em falha, a varredura a reanima uma vez (r2), e o aviso `bot_access_restored` sai uma vez a cada
    membro, pelo bot que voltou: o período no fuso e zeros, sem a linha da orientação de reenvio, com o
    `notice.recorded` do `episode_seq`."""
    driver = world.driver()
    lost_seq = world.lose_access()
    started = world.clock.now()
    world.clock.advance(90)
    ended = world.clock.now()
    world.restore_access()

    notice = _assert_revived_once(world, driver, ACCESS_TASK, world.offering_row, "bot_access_restored")

    assert (notice["subject_key"], notice["dispatch_id"]) == (f"episode:{lost_seq}", None)
    lines = _access_lines(world, started=started, ended=ended, not_delivered=0, pending=0, lost_notices=0,
                          lost_summaries=0)
    for text in world.texts(notice["id"]):
        _assert_lines(text, lines[:-1])
        assert lines[-1] not in text
    assert world.recorded() == [world.recorded_event("bot_access_restored", episode_seq=lost_seq)]


def test_sweep_revives_the_invite_batch_detection_without_live_task(world: World) -> None:
    """Um lote de 3 convites com 1 endereço recusado de vez,
    montado sem a regra `notice.invite_batch`; a tarefa r1 da regra (assunto: o lote) termina em falha, a
    varredura a reanima uma vez (r2), e o aviso `invite_failures` do lote (`2 enviados e 1 com falha`) sai
    uma vez a cada membro, com o `notice.recorded` do `batch_uuid`."""
    listed = world.import_list(3)
    driver = world.driver(mail=MailChannel([listed[1].email]))
    with _rule_held(driver, BATCH_TASK):
        world.confirm_batch()
        driver.settle()
    [(batch_row, batch_uuid)] = world.sql.execute("SELECT id, batch_uuid::text FROM invite_batches").fetchall()
    assert sorted((s["enrollment_id"], s["batch_id"], s["state"]) for s in world.sends()) == sorted(
        (x.enrollment_row, batch_row, "failed" if x is listed[1] else "sent") for x in listed)

    notice = _assert_revived_once(world, driver, BATCH_TASK, batch_row, "invite_failures")

    assert (notice["subject_key"], notice["dispatch_id"]) == (f"batch:{batch_row}", None)
    expected = noticetexts.INVITE_FAILURES_BATCH.format(course=world.course, sent=2, failed=1,
                                                        code=world.code).split("\n")
    for text in world.texts(notice["id"]):
        _assert_lines(text, expected)
    assert world.recorded() == [world.recorded_event("invite_failures", batch_id=batch_uuid)]


def test_sweep_revives_the_invite_window_detection_without_live_task(world: World) -> None:
    """Uma falha do reenvio da coordenação em `t0`; no
    fim da janela (`t0 + 15 min`), a tarefa r1 da regra `notice.invite_window` (assunto: o oferecimento)
    termina em falha, a varredura a reanima uma vez (r2), e o aviso `invite_failures` da janela
    (`window:<id da falha>`, 1 falha entre `t0` e `t0 + 15 min` no fuso) sai uma vez a cada membro, com o
    `notice.recorded` do `window_started_at`."""
    [listed] = world.import_list(1)
    driver = world.driver(mail=MailChannel([listed.email]))
    t0 = world.clock.now()
    with _rule_held(driver, WINDOW_TASK):
        world.resend([listed])
        driver.settle()
    [send] = world.sends()
    assert (send["origin"], send["state"], send["finished_at"]) == ("coordination", "failed", t0)
    world.clock.set(t0 + WINDOW - timedelta(seconds=1))
    assert driver.sweep(WINDOW_TASK) == 0  # a janela ainda aberta: a regra não vence
    world.clock.set(t0 + WINDOW)

    notice = _assert_revived_once(world, driver, WINDOW_TASK, world.offering_row, "invite_failures")

    assert (notice["subject_key"], notice["dispatch_id"]) == (f"window:{send['id']}", None)
    expected = noticetexts.INVITE_FAILURES_WINDOW.format(
        course=world.course, failed=1, window_start=format_local(t0, world.tz),
        window_end=format_local(t0 + WINDOW, world.tz), code=world.code).split("\n")
    for text in world.texts(notice["id"]):
        _assert_lines(text, expected)
    assert world.recorded() == [world.recorded_event("invite_failures", window_started_at=iso(t0))]
