"""Fila estendida dos avisos à equipe (relatórios e acompanhamento).

Cobre os avisos à equipe do lado da fila (posse esgotada, envio que falha, reexecução da fase 2, bloqueio
e bot sem acesso), com as escolhas de esquema e as interfaces do servidor que os testes usam
(os avisos à equipe
e a fila de saída):
- `teamnotices.record` idempotente pelo tipo e assunto (`None` no segundo registro) e `teamnotices.deliver`
  com os destinatários exatos (matrícula `team` `active` + endereço `active` no bot do oferecimento, em
  ordem de `enrollments.id`), com os controles negativos: o membro bloqueado, o sem endereço, o
  aluno vinculado com endereço ativo no mesmo bot e a matrícula de equipe `pending` com endereço ativo;
- sem destinatário → `no_recipient` com `notice.recorded`, nada na fila, reexecução sem efeito; sem
  retroativo: o membro vinculado depois de um aviso `queued` ou `no_recipient` não o recebe;
- a linha de cada destinatário: `text_format = formatter.text_format`, `notice_id`, `priority = 2`, a
  chave `notice:<id>:p<person_row_id>` e `created_at`/`available_at` = o `now` passado ao `deliver`,
  gravada por `outbox.enqueue_notice_text`; `enqueue_notice_text` e `enqueue_notice_content` com a
  chave repetida devolvem a existente e, com outro conteúdo, `ValueError` sem escrita;
- o despacho pelo caminho do disparo (Rich com `skip_entity_detection: true`; HTML com a prévia de link
  desligada; console em texto simples, com as 4 chaves da API base) e a linha de texto sem `text_format`
  igual à de antes;
- prioridade 2 atrás das mensagens aos alunos (0) e das respostas fixas (1) do mesmo bot; a ordem por
  endereço entre mensagens de aviso (a segunda espera a primeira, com falha temporária no meio);
- 429 com `retry_after`, 5xx e rede com espera crescente; 403 de bloqueio → `failed` + endereço `blocked`,
  os demais enviados; bot sem acesso → `RetryLater` até esgotar e `outbox.failed`;
- a queda do worker (`Crash`) na fase 2 e a posse esgotada (da tarefa e da mensagem) sem duplicar nem
  perder; duas tarefas `notice.send` do mesmo aviso em duas conexões (uma enfileira, a outra não tem
  efeito);
- a mensagem da forma 3 (prévia) só é candidata com o `NoticeContentLoader`, na posse de duas transações
  montada como a do worker; nem na posse do disparo sem o carregador nem na legada, com ou sem ele;
- a regra `notice.send` da varredura reanima um aviso `pending` sem tarefa viva.

**Montagem.** A equipe vem da fixture `team_members` (o vínculo real de oferecimentos e bots, pela Bot API
falsa com a extensão do disparo, `rich_bot_api`, pedida antes dela); os alunos e a matrícula de equipe
`pending` com endereço entram por SQL nas colunas já existentes, como em `test_dispatch_opening.py`. O aviso de texto usado
pela fila é o "aberto" registrado pelo gancho do fechamento (`teamnotices.record_opened_on_close`, com as
contagens tipadas `DeliveryCounts`), sobre um disparo de alvo vazio que o próprio teste leva a `open`
pelos tratadores do disparo montados como o `run_worker` os monta (posse por SQL da tarefa do disparo, no
mesmo molde da semente dos relatórios), sem worker em thread: a fila de saída é dirigida pelo teste
(`Outbox.dispatch_once`), com a posse de duas transações montada como a do worker com bots (`content`,
`outcome` e, quando o caso pede, `notice_content=NoticeContentLoader()` e o `SendPacer` com o relógio
monotônico injetado). As tarefas dos relatórios vêm de `teamnotices.task_handlers` e as regras de
`scheduler.REPORT_RULES` (a chave do assunto lida da própria regra, `SweepRule.subject_key`).

Horários pelo `FakeClock` (`iso`); o horário do fechamento no texto pelo formato da casa,
`timezones.format_local` (o do CLI de oferecimentos; aviso do bot sem acesso) [I]. Ids internos,
`seq`, `channel_ref` e chaves de mensagem só por relação.

**Vermelho no esqueleto.** Cada teste chega aos avisos depois de passos anteriores que já funcionam e falha
ali: `NotImplementedError` (`record`, `record_opened_on_close`, `deliver`, `team_recipients`,
`enqueue_notice_text`, `enqueue_notice_content`, `task_handlers`, `preview_task_handler`), tabela nova
ausente (`team_notices`) ou a regra nova ainda ausente de `REPORT_RULES`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cq_server import outbox as cq_outbox
from cq_server import previews, reception, scheduler, teamnotices
from cq_server._leasing import LEASE_EXHAUSTED_ERROR
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.bots import TelegramChannel, mark_no_access
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.deliveries import DeliveryContentLoader, DeliveryOutcomeRecorder
from cq_server.dispatches import (OPEN_TASK_TYPE, VALIDATE_TASK_TYPE, DeliveryCounts, open_task_handler,
                                  validate_task_handler)
from cq_server.outbox import Outbox
from cq_server.pacing import SendPacer
from cq_server.ports.delivery import OutgoingMessage, RetryLater, Sent
from cq_server.tasks import ClaimedTask, TaskQueue, enqueue_in_transaction
from cq_server.timezones import format_local
from tests.helpers.api import (ADMIN_TOKEN, all_events, confirm_dispatch, create_team_link, link_code,
                               prepare_dispatch, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import Crash, ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.console_student import PLAIN_KEYS, read_lines
from tests.helpers.engine import preset
from tests.helpers.team import ACTIVE, BLOCKED, NO_ENDPOINT, Team, TeamMember, team_messages

REPO = "https://repos.cq.test/avisos/fila-de-avisos.git"
COMMIT = "f17a" * 10
QID = "number"
SCHEDULED_QID = "single_choice"
TITLE = "Velocidade média na rampa"
WORKER = "worker-fila-avisos"
CRASHED_WORKER = "worker-caido"
# A posse dos testes (30 s, fixture `settings`) mais 1 s: a posse de quem caiu fica vencida.
EXPIRE = 31
NOTICE_PRIORITY = 2
SEND_TASK = "notice.send"
PREVIEW_TASK = "preview.send"
# O "aberto" registrado pelo gancho num disparo de alvo vazio: tudo zero.
ZERO_COUNTS = DeliveryCounts(pending=0, delivered=0, not_delivered=0, answered=0, dont_know=0, no_answer=0,
                             answered_late=0, question_failed=0, grading_pending=0)
SEND_METHODS = ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument")

# A posse, por SQL, da tarefa disponível de menor `id` de um tipo e de um assunto (`payload @> subject`),
# com a mesma forma do `TaskQueue.claim` (posse do `settings`, tentativa contada).
_CLAIM_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s AND payload @> %(subject)s "
    "            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")

_people = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Arnês
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int
    closes_at: datetime


@dataclasses.dataclass(frozen=True)
class Person:
    """Uma pessoa com o seu endereço no bot do oferecimento (`endpoint_row` `None` sem endereço)."""

    pseudo_id: str
    person_row: int
    endpoint_row: int | None
    chat: int | None

    @property
    def uuid(self) -> uuid.UUID:
        return uuid.UUID(self.pseudo_id)


def _rule(task_type: str) -> Any:
    """A regra dos relatórios de `task_type`."""
    found = [rule for rule in scheduler.REPORT_RULES if rule.task_type == task_type]
    assert len(found) == 1, f"regra {task_type} ausente de scheduler.REPORT_RULES"
    return found[0]


def _subject(task_type: str, row_id: int) -> dict:
    """O payload da tarefa dos relatórios para o assunto `row_id`, com a chave da própria regra."""
    return {_rule(task_type).subject_key: row_id}


def _assert_lines(text: str, lines: Sequence[str]) -> None:
    """Cada linha esperada aparece no texto, nessa ordem."""
    position = 0
    for line in lines:
        found = text.find(line, position)
        assert found >= 0, f"linha ausente ou fora de ordem no aviso: {line!r}\n---\n{text}"
        position = found + len(line)


class Ctx:
    """O oferecimento da equipe simulada, as pessoas, os disparos e os tratadores do teste."""

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
        self.stack = contextlib.ExitStack()
        self.queue = TaskQueue(db_url, clock=clock, settings=settings)
        self.stack.callback(self.queue.close)
        self._r5: tuple[Any, Any] | None = None
        engine_fake.program((REPO, COMMIT, QID), preset("number", title=TITLE))
        engine_fake.program((REPO, COMMIT, SCHEDULED_QID), preset("single_choice", title=TITLE))

    def close(self) -> None:
        self.stack.close()

    # -- pessoas -------------------------------------------------------------------------------

    def member(self, member: TeamMember) -> Person:
        row = self.sql.execute(
            "SELECT p.id, d.id FROM people p LEFT JOIN delivery_endpoints d ON d.person_id = p.id "
            "AND d.bot_id = %s AND d.state IN ('active', 'blocked') WHERE p.pseudo_id = %s",
            (self.bot_row, uuid.UUID(member.pseudo_id))).fetchone()
        assert row is not None, f"membro {member.label} sem linha em people"
        return Person(member.pseudo_id, int(row[0]), None if row[1] is None else int(row[1]), member.chat_id)

    def recipients(self) -> list[Any]:
        """Os destinatários esperados (os membros com endereço ativo, na ordem do vínculo)."""
        out = []
        for member in self.team.active:
            person = self.member(member)
            out.append(teamnotices.Recipient(person_row_id=person.person_row, pseudo_id=person.uuid,
                                             endpoint_row_id=person.endpoint_row))
        return out

    def _endpoint(self, person_row: int, chat: int, state: str = "active") -> int:
        now = self.clock.now()
        endpoint = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, %s, %s, %s) RETURNING id",
            (person_row, self.bot_row, chat, state, now, now)).fetchone()[0]
        return int(endpoint)

    def student(self) -> Person:
        """Um aluno vinculado (matrícula `student` `active`, termo aceito) com endereço ativo no mesmo bot."""
        n = next(_people)
        user = make_user(76_000 + n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Fila Avisos {n}")
        person_row = int(self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                          (body["pseudo_id"],)).fetchone()[0])
        now = self.clock.now()
        self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
            (person_row, self.offering_row, now, now, now))
        endpoint = self._endpoint(person_row, user.user_id)
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        return Person(body["pseudo_id"], person_row, endpoint, user.user_id)

    def pending_team_member_with_endpoint(self) -> Person:
        """Uma matrícula de equipe `pending` (o link de equipe gerado, sem `/start`) com endereço
        `active` no bot do oferecimento, gravado por SQL."""
        n = next(_people)
        user = make_user(77_000 + n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Membro Pendente Fila {n}", ra=f"89{n:08d}")
        create_team_link(self.client, ADMIN_TOKEN, self.code, body["alias"])
        person_row = int(self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                          (body["pseudo_id"],)).fetchone()[0])
        [(state, role)] = self.sql.execute(
            "SELECT state, role FROM enrollments WHERE person_id = %s AND offering_id = %s",
            (person_row, self.offering_row)).fetchall()
        assert (state, role) == ("pending", "team"), "o link de equipe cria a matrícula pendente"
        endpoint = self._endpoint(person_row, user.user_id)
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        return Person(body["pseudo_id"], person_row, endpoint, user.user_id)

    def link_directly(self) -> Person:
        """Um membro da equipe vinculado pelo link de equipe (`/start <código>` pela entrada simulada do
        console e o tratador `telegram.update` montado como o `run_worker` do console o monta,
        `runtime=None`), sem worker em thread: nenhuma varredura dos relatórios roda no meio do teste."""
        n = next(_people)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Membro Tardio Fila {n}", ra=f"87{n:08d}")
        code = link_code(create_team_link(self.client, ADMIN_TOKEN, self.code, body["alias"])["link"])
        chat = make_user(78_000 + n).user_id
        inbox = ConsoleInbox(self.db_url, clock=self.clock)
        try:
            inbox.send_text(self.bot_row, chat, f"/start {code}")
        finally:
            inbox.close()
        handler = reception.update_task_handler(self.db_url, clock=self.clock, runtime=None, settings=self.settings,
                                                channel=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
        try:
            handlers = {reception.UPDATE_TASK_TYPE: handler}
            for _ in range(5):
                if self.queue.work_once(WORKER, handlers, task_types=[reception.UPDATE_TASK_TYPE]) is None:
                    break
        finally:
            handler.close()
        person_row = int(self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                          (body["pseudo_id"],)).fetchone()[0])
        row = self.sql.execute(
            "SELECT e.role, e.state, d.id, d.state FROM enrollments e JOIN delivery_endpoints d "
            "ON d.person_id = e.person_id AND d.bot_id = %s WHERE e.person_id = %s AND e.offering_id = %s",
            (self.bot_row, person_row, self.offering_row)).fetchone()
        assert row is not None and (row[0], row[1], row[3]) == ("team", "active", "active"), (
            "o vínculo de equipe não ativou a matrícula e o endereço")
        return Person(body["pseudo_id"], person_row, int(row[2]), chat)

    # -- canais --------------------------------------------------------------------------------

    def telegram(self, delivery: DeliverySettings | None = None) -> TelegramChannel:
        channel = TelegramChannel(self.db_url, clock=self.clock, runtime=self.bots,
                                  default=ConsoleChannel(io.StringIO()), delivery=delivery or DeliverySettings())
        self.stack.callback(channel.close)
        return channel

    def outbox(self, channel: Any, *, notice_loader: bool = True, pacer: SendPacer | None = None,
               legacy: bool = False) -> Outbox:
        """A fila montada como a do worker com bots (`content`, `outcome`, `notice_content`); com
        `legacy=True`, a posse legada (sem `content`, `outcome` nem `pacer`)."""
        loader = previews.NoticeContentLoader() if notice_loader else None
        if legacy:
            box = Outbox(self.db_url, clock=self.clock, settings=self.settings, notice_content=loader)
        else:
            box = Outbox(self.db_url, clock=self.clock, settings=self.settings,
                         content=DeliveryContentLoader(channel, delivery=DeliverySettings()),
                         outcome=DeliveryOutcomeRecorder(channel, clock=self.clock), pacer=pacer,
                         notice_content=loader)
        self.stack.callback(box.close)
        return box

    # -- tarefas -------------------------------------------------------------------------------

    def claim(self, task_type: str, subject: dict, *, owner: str = WORKER) -> ClaimedTask | None:
        now = self.clock.now()
        row = self.sql.execute(_CLAIM_SQL, {"owner": owner, "until": now + timedelta(seconds=self.settings.lease_seconds),
                                            "type": task_type, "now": now, "subject": Jsonb(subject)}).fetchone()
        if row is None:
            return None
        return ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3]))

    def run(self, task_type: str, handler: Any, subject: dict, *, limit: int = 8) -> int:
        """Processa as tarefas de `task_type` do assunto, uma a uma, com o tratador dado."""
        processed = 0
        while True:
            task = self.claim(task_type, subject)
            if task is None:
                return processed
            handler(task)
            assert self.queue.complete(task.id, WORKER), f"{task_type} não concluiu (posse perdida)"
            processed += 1
            assert processed <= limit, f"mais de {limit} tarefas {task_type} para {subject}"

    def rerun(self, handlers: dict[str, Any], task_type: str, subject: dict, key: str) -> None:
        """A reexecução de uma tarefa dos relatórios já concluída (outra tarefa do mesmo assunto, com outra chave),
        pelo tratador dado, depois do commit da primeira."""
        with psycopg.connect(self.db_url) as conn:
            enqueue_in_transaction(conn, task_type, subject, now=self.clock.now(), idempotency_key=key)
        task = self.claim(task_type, subject)
        assert task is not None, f"a reexecução de {task_type} não foi enfileirada"
        handlers[task_type](task)
        assert self.queue.complete(task.id, WORKER), f"a reexecução de {task_type} não concluiu"

    def _question_tasks(self) -> tuple[Any, Any]:
        if self._r5 is None:
            engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url, timeout_seconds=10.0))
            self.stack.callback(engine.close)
            validate = validate_task_handler(self.db_url, clock=self.clock, settings=self.settings, engine=engine,
                                             fetcher=self.fetcher, channel=self.telegram())
            self.stack.callback(validate.close)
            opener = open_task_handler(self.db_url, clock=self.clock, settings=self.settings)
            self.stack.callback(opener.close)
            self._r5 = (validate, opener)
        return self._r5

    def report_handlers(self, channel: Any) -> dict[str, Any]:
        handlers = teamnotices.task_handlers(self.db_url, clock=self.clock, settings=self.settings,
                                             channel=channel)
        for handler in handlers.values():
            self.stack.callback(handler.close)
        return handlers

    def sweep(self, *task_types: str) -> Any:
        """Uma rodada da varredura só com as regras dos relatórios dos tipos dados."""
        rules = [_rule(task_type) for task_type in task_types]
        return scheduler.sweep_once(self.sql, now=self.clock.now(), retry_gap=float(self.settings.backoff_max_seconds),
                                    rules=rules)

    # -- disparos ------------------------------------------------------------------------------

    def dispatch_state(self, row: int) -> str:
        return str(self.sql.execute("SELECT state FROM dispatches WHERE id = %s", (row,)).fetchone()[0])

    def validated(self, *, qid: str = QID, opens_at: datetime | None = None,
                  closes_at: datetime | None = None) -> Disp:
        """Um disparo preparado pela API e validado pelo tratador do disparo (`scheduled`, sem confirmação)."""
        closes_at = closes_at if closes_at is not None else self.clock.now() + timedelta(hours=2)
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at), closes_at=iso(closes_at))
        row = int(self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                                   (body["dispatch_id"],)).fetchone()[0])
        validate, _ = self._question_tasks()
        self.run(VALIDATE_TASK_TYPE, validate, {"dispatch_row_id": row})
        assert self.dispatch_state(row) == "scheduled", "a validação não levou o disparo a scheduled"
        return Disp(body["dispatch_id"], row, closes_at)

    def opened(self) -> Disp:
        """Um disparo imediato de alvo vazio levado a `open` pelos tratadores do disparo."""
        d = self.validated()
        confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)
        _, opener = self._question_tasks()
        self.run(OPEN_TASK_TYPE, opener, {"dispatch_row_id": d.row})
        assert self.dispatch_state(d.row) == "open", "a abertura não levou o disparo a open"
        return d

    # -- avisos --------------------------------------------------------------------------------

    def record_opened(self, d: Disp) -> int:
        """O "aberto" registrado pelo gancho do fechamento (fase 1, `pending`); devolve o id do aviso."""
        with psycopg.connect(self.db_url) as conn:
            assert teamnotices.record_opened_on_close(conn, dispatch_row_id=d.row, offering_row_id=self.offering_row,
                                                      counts=ZERO_COUNTS, now=self.clock.now()) is True
        [notice_id] = [n["id"] for n in self.notices("dispatch_opened") if n["dispatch_id"] == d.row]
        return int(notice_id)

    def deliver(self, notice_id: int, channel: Any, now: datetime | None = None) -> Any:
        with psycopg.connect(self.db_url) as conn:
            return teamnotices.deliver(conn, notice_id, channel=channel, now=now or self.clock.now())

    def notices(self, kind: str | None = None) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM team_notices WHERE offering_id = %s "
                               "AND (%s::text IS NULL OR kind = %s::text) ORDER BY id",
                               (self.offering_row, kind, kind)).fetchall()

    def notice(self, notice_id: int) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT * FROM team_notices WHERE id = %s", (notice_id,)).fetchone()
        assert row is not None
        return row

    def rows(self, notice_id: int) -> list[dict]:
        """As linhas da fila do aviso, na ordem de `id`."""
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM outbox_messages WHERE notice_id = %s ORDER BY id",
                               (notice_id,)).fetchall()

    def row(self, message_id: int) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            found = cur.execute("SELECT * FROM outbox_messages WHERE id = %s", (message_id,)).fetchone()
        assert found is not None
        return found

    def events(self, *types: str) -> list[tuple]:
        return [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"])
                for e in all_events(self.client, ADMIN_TOKEN, *types)]

    def recorded(self, d: Disp, *, state: str, count: int, at: datetime) -> tuple:
        """O `notice.recorded` do "aberto" do disparo `d`."""
        return ("notice.recorded", iso(at), None,
                {"offering_id": self.offering["offering_id"], "notice_type": "dispatch_opened", "dispatch_id": d.id,
                 "report_version": None, "episode_seq": None, "batch_id": None, "window_started_at": None,
                 "local_date": None, "state": state, "recipient_count": count})

    def opened_lines(self, d: Disp) -> list[str]:
        """As linhas do "aberto" de alvo vazio (`OPENED`), sem a das pendentes."""
        return [f"Disparo aberto: {TITLE}", "Alunos-alvo: 0", "Entregues: 0", "Não entregues: 0",
                "Em falha de questão: 0", "Pendentes: 0", f"Fechamento: {format_local(d.closes_at, self.tz)}",
                f"Para acompanhar: cq dispatches show {self.code} {d.id}"]

    def endpoint_state(self, endpoint_row: int) -> str:
        return str(self.sql.execute("SELECT state FROM delivery_endpoints WHERE id = %s",
                                    (endpoint_row,)).fetchone()[0])


@pytest.fixture
def ctx(rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, db_url: str, clock: Any,
        settings: Any, bots: Any, engine_fake: Any, repo_fetcher: Any) -> Iterator[Ctx]:
    context = Ctx(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, bots=bots, rich=rich_bot_api,
                  engine_fake=engine_fake, repo_fetcher=repo_fetcher, team=team_members)
    try:
        yield context
    finally:
        context.close()


def _drain(box: Outbox, channel: Any, *, limit: int = 20) -> list[int]:
    """Despacha até a fila não ter candidata (`dispatch_once` devolve `None`)."""
    taken: list[int] = []
    for _ in range(limit):
        message_id = box.dispatch_once(channel, WORKER, include_routed=True)
        if message_id is None:
            return taken
        taken.append(message_id)
    raise AssertionError(f"a fila ainda tinha candidatas depois de {limit} despachos")


def _chat_of(call: Any) -> int | None:
    value = call.params.get("chat_id")
    return int(value) if isinstance(value, (int, str)) and str(value).lstrip("-").isdigit() else None


# ---------------------------------------------------------------------------------------------
# Registro e destinatários
# ---------------------------------------------------------------------------------------------


def test_record_is_idempotent_by_kind_and_subject_and_has_no_event(ctx: Ctx) -> None:
    """`record` grava o aviso `pending` (sem `finished_at`, com
    `recipient_count` 0, a fotografia e os horários dados) e devolve o id; o segundo registro do mesmo tipo e
    assunto devolve `None` e não muda a linha; outro tipo com o mesmo assunto é outro aviso. A fase 1 não
    grava evento."""
    d = ctx.validated()
    t0 = ctx.clock.now()
    data = {"rejection": "question_broken"}

    with psycopg.connect(ctx.db_url) as conn:
        first = teamnotices.record(conn, offering_row_id=ctx.offering_row, kind=teamnotices.NoticeKind.DISPATCH_REJECTED,
                                   subject_key=f"dispatch:{d.row}", data=data, now=t0, dispatch_row_id=d.row)
    ctx.clock.advance(5)
    with psycopg.connect(ctx.db_url) as conn:
        again = teamnotices.record(conn, offering_row_id=ctx.offering_row, kind=teamnotices.NoticeKind.DISPATCH_REJECTED,
                                   subject_key=f"dispatch:{d.row}", data={"rejection": "internal_failure"},
                                   now=ctx.clock.now(), dispatch_row_id=d.row)
        other = teamnotices.record(conn, offering_row_id=ctx.offering_row, kind=teamnotices.NoticeKind.DISPATCH_CANCELLED,
                                   subject_key=f"dispatch:{d.row}", data={}, now=ctx.clock.now(), dispatch_row_id=d.row)

    assert isinstance(first, int) and again is None and isinstance(other, int) and other != first
    row = ctx.notice(first)
    assert (row["offering_id"], row["kind"], row["subject_key"], row["state"], row["recipient_count"],
            row["dispatch_id"], row["report_id"], row["local_date"], row["data"], row["created_at"],
            row["finished_at"]) == (ctx.offering_row, "dispatch_rejected", f"dispatch:{d.row}", "pending", 0,
                                    d.row, None, None, data, t0, None)
    assert [(n["kind"], n["state"]) for n in ctx.notices()] == [("dispatch_rejected", "pending"),
                                                                ("dispatch_cancelled", "pending")]
    assert ctx.events("notice.recorded") == []
    assert ctx.rows(first) == [] and ctx.rows(other) == []


@pytest.mark.parametrize("team_members", [(ACTIVE, BLOCKED, NO_ENDPOINT, ACTIVE)], indirect=True)
def test_deliver_reaches_only_active_team_members_with_active_endpoint(ctx: Ctx) -> None:
    """Os destinatários são exatamente as matrículas `team` `active`
    com endereço `active` no bot do oferecimento, em ordem de `enrollments.id`: o membro bloqueado e o sem
    endereço ficam fora, e também (controles negativos, **[F]** o esquema os admite: `0002_offerings_bots.sql`
    l. 129-133 e 166-179) um aluno vinculado com endereço ativo no mesmo bot e uma matrícula de equipe
    `pending` com endereço ativo no bot. Uma mensagem por destinatário, nenhuma com `notice_id` aos
    endereços dos outros; o aviso fica `queued` com `recipient_count` 2, `finished_at` e um
    `notice.recorded`."""
    student = ctx.student()
    pending = ctx.pending_team_member_with_endpoint()
    blocked = ctx.member(ctx.team.member("membro-2"))
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    expected = ctx.recipients()
    t0 = ctx.clock.now()

    with psycopg.connect(ctx.db_url) as conn:
        assert teamnotices.team_recipients(conn, ctx.offering_row) == expected
    assert ctx.deliver(notice_id, ctx.telegram()) == teamnotices.NoticeState.QUEUED

    assert len(expected) == 2
    rows = ctx.rows(notice_id)
    assert [(r["recipient_person_id"], r["delivery_endpoint_id"]) for r in rows] == [
        (r.person_row_id, r.endpoint_row_id) for r in expected]
    others = {student.endpoint_row, pending.endpoint_row, blocked.endpoint_row}
    assert ctx.sql.execute("SELECT count(*) FROM outbox_messages WHERE notice_id IS NOT NULL "
                           "AND delivery_endpoint_id = ANY(%s)", (sorted(others),)).fetchone()[0] == 0
    notice = ctx.notice(notice_id)
    assert (notice["state"], notice["recipient_count"], notice["finished_at"]) == ("queued", 2, t0)
    assert ctx.events("notice.recorded") == [ctx.recorded(d, state="queued", count=2, at=t0)]
    assert repr(expected[0]) == f"Recipient(person_row_id={expected[0].person_row_id})"


@pytest.mark.parametrize("team_members", [()], indirect=True)
def test_no_recipient_records_event_enqueues_nothing_and_rerun_has_no_effect(ctx: Ctx) -> None:
    """Sem destinatário, `deliver` → `no_recipient`, com
    `finished_at`, `recipient_count` 0 e um `notice.recorded`, sem nada na fila; a reexecução fora de
    `pending` devolve o estado, sem efeito (nem evento novo, nem `finished_at` novo)."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    t0 = ctx.clock.now()

    assert ctx.deliver(notice_id, ctx.telegram()) == teamnotices.NoticeState.NO_RECIPIENT
    ctx.clock.advance(30)
    assert ctx.deliver(notice_id, ctx.telegram()) == teamnotices.NoticeState.NO_RECIPIENT

    notice = ctx.notice(notice_id)
    assert (notice["state"], notice["recipient_count"], notice["finished_at"]) == ("no_recipient", 0, t0)
    assert ctx.rows(notice_id) == []
    assert ctx.sql.execute("SELECT count(*) FROM outbox_messages WHERE notice_id IS NOT NULL").fetchone()[0] == 0
    assert ctx.events("notice.recorded") == [ctx.recorded(d, state="no_recipient", count=0, at=t0)]


@pytest.mark.parametrize("team_members", [()], indirect=True)
def test_member_linked_later_receives_nothing_retroactively(ctx: Ctx) -> None:
    """Um aviso `no_recipient` (sem membro) e outro `queued` (com 1 membro, vinculado
    entre os dois pelo link de equipe); um membro novo vinculado depois, também pelo link de equipe; a
    reexecução do `notice.send` dos dois avisos e uma rodada da varredura dos relatórios (com as tarefas que ela
    criar processadas) → nenhuma mensagem de aviso para o membro novo, e o estado, o `recipient_count` e o
    `finished_at` dos dois avisos inalterados."""
    channel = ctx.telegram()
    first, second = ctx.opened(), ctx.opened()
    lonely = ctx.record_opened(first)
    queued = ctx.record_opened(second)
    assert ctx.deliver(lonely, channel) == teamnotices.NoticeState.NO_RECIPIENT
    early = ctx.link_directly()
    assert ctx.deliver(queued, channel) == teamnotices.NoticeState.QUEUED
    snapshot = [(n["id"], n["state"], n["recipient_count"], n["finished_at"]) for n in ctx.notices()]
    late = ctx.link_directly()

    handlers = ctx.report_handlers(channel)
    for notice_id in (lonely, queued):
        ctx.rerun(handlers, SEND_TASK, _subject(SEND_TASK, notice_id), f"{SEND_TASK}:{notice_id}:retroativo")
    ctx.sweep(*(rule.task_type for rule in scheduler.REPORT_RULES if rule.task_type in handlers))
    for _ in range(20):
        if ctx.queue.work_once(WORKER, handlers, task_types=list(handlers)) is None:
            break

    assert [(n["id"], n["state"], n["recipient_count"], n["finished_at"]) for n in ctx.notices()] == snapshot
    assert [(s[0], s[1], s[2]) for s in snapshot] == [(lonely, "no_recipient", 0), (queued, "queued", 1)]
    assert [(m.notice_id, m.recipient) for m in team_messages(ctx.sql)] == [(queued, early.pseudo_id)]
    assert team_messages(ctx.sql, recipient=late.pseudo_id) == []


# ---------------------------------------------------------------------------------------------
# A linha da fila e as funções de enfileiramento
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["rich", "html", "plain"])
def test_notice_rows_carry_dialect_notice_priority_key_and_the_passed_now(ctx: Ctx, dialect: str) -> None:
    """Uma linha por destinatário, gravada por
    `outbox.enqueue_notice_text`, com `text` (o aviso no dialeto), `text_format = formatter.text_format` do
    canal (Rich e HTML do Telegram; `plain` do console), `notice_id`, `priority = 2`, a chave
    `notice:<id>:p<person_row_id>`, a pessoa, o bot e o endereço, sem nenhuma coluna de entrega, e
    `created_at`/`available_at` = o `now` passado ao `deliver` (não o relógio da fila)."""
    channel: Any = ConsoleChannel(io.StringIO()) if dialect == "plain" else ctx.telegram(
        DeliverySettings(telegram_format=dialect))
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    passed = ctx.clock.now() + timedelta(minutes=3)

    assert ctx.deliver(notice_id, channel, now=passed) == teamnotices.NoticeState.QUEUED

    rows = ctx.rows(notice_id)
    expected = ctx.recipients()
    assert len(rows) == len(expected) == 2
    for row, recipient in zip(rows, expected):
        assert (row["recipient_person_id"], row["bot_id"], row["delivery_endpoint_id"], row["reply_to_update_id"],
                row["notice_id"], row["text_format"], row["priority"], row["idempotency_key"], row["state"],
                row["attempts"], row["created_at"], row["available_at"], row["delivery_id"], row["delivery_role"],
                row["operation"], row["render_version"], row["media_id"], row["field_index"], row["sent_at"]) == (
            recipient.person_row_id, ctx.bot_row, recipient.endpoint_row_id, None, notice_id, dialect,
            NOTICE_PRIORITY, f"notice:{notice_id}:p{recipient.person_row_id}", "pending", 0, passed, passed, None,
            None, None, None, None, None, None)
        assert isinstance(row["text"], str) and row["text"]
    assert rows[0]["text"] == rows[1]["text"]
    if dialect == "plain":
        _assert_lines(rows[0]["text"], ctx.opened_lines(d))
    assert ctx.notice(notice_id)["finished_at"] == passed
    assert ctx.events("notice.recorded") == [ctx.recorded(d, state="queued", count=2, at=passed)]


def test_enqueue_notice_functions_are_idempotent_by_key_and_refuse_other_content(ctx: Ctx) -> None:
    """No molde de `outbox.enqueue_delivery`: `enqueue_notice_text` e
    `enqueue_notice_content` rodam na transação do chamador, com o `now` dele e `priority = 2`; a chave
    repetida com o mesmo conteúdo devolve a mensagem existente (uma linha só); com outro conteúdo,
    `ValueError` sem escrita e sem deixar a transação em erro."""
    d = ctx.validated(qid=SCHEDULED_QID, opens_at=ctx.clock.now() + timedelta(hours=1))
    person = ctx.member(ctx.team.member("membro-1"))
    t0 = ctx.clock.now()
    with psycopg.connect(ctx.db_url) as conn:
        text_notice = teamnotices.record(conn, offering_row_id=ctx.offering_row,
                                         kind=teamnotices.NoticeKind.DISPATCH_REJECTED, subject_key=f"dispatch:{d.row}",
                                         data={}, now=t0, dispatch_row_id=d.row)
        preview_notice = teamnotices.record(conn, offering_row_id=ctx.offering_row,
                                            kind=teamnotices.NoticeKind.DISPATCH_PREVIEW,
                                            subject_key=f"dispatch:{d.row}", data={}, now=t0, dispatch_row_id=d.row)
    assert isinstance(text_notice, int) and isinstance(preview_notice, int)
    text_key = f"notice:{text_notice}:p{person.person_row}"
    content_key = f"notice:{preview_notice}:p{person.person_row}:question"
    common = {"recipient": person.uuid, "bot_id": ctx.bot_row, "endpoint_row_id": person.endpoint_row}

    with psycopg.connect(ctx.db_url) as conn:
        m1 = cq_outbox.enqueue_notice_text(conn, notice_row_id=text_notice, text="Aviso de teste", text_format="rich",
                                           idempotency_key=text_key, now=t0, **common)
        assert cq_outbox.enqueue_notice_text(conn, notice_row_id=text_notice, text="Aviso de teste",
                                             text_format="rich", idempotency_key=text_key, now=t0, **common) == m1
        with pytest.raises(ValueError):
            cq_outbox.enqueue_notice_text(conn, notice_row_id=text_notice, text="Outro texto", text_format="rich",
                                          idempotency_key=text_key, now=t0, **common)
        with pytest.raises(ValueError):
            cq_outbox.enqueue_notice_text(conn, notice_row_id=text_notice, text="Aviso de teste", text_format="html",
                                          idempotency_key=text_key, now=t0, **common)
        m2 = cq_outbox.enqueue_notice_content(conn, notice_row_id=preview_notice, role="question",
                                              idempotency_key=content_key, now=t0, **common)
        assert cq_outbox.enqueue_notice_content(conn, notice_row_id=preview_notice, role="question",
                                                idempotency_key=content_key, now=t0, **common) == m2
        with pytest.raises(ValueError):
            cq_outbox.enqueue_notice_content(conn, notice_row_id=preview_notice, role="field_prompt", field_index=0,
                                             idempotency_key=content_key, now=t0, **common)
        assert conn.execute("SELECT 1").fetchone() == (1,)  # a transação não ficou em erro

    rows = ctx.sql.execute(
        "SELECT id, notice_id, text, text_format, priority, delivery_role, operation, media_id, field_index, "
        "created_at, available_at, idempotency_key FROM outbox_messages WHERE notice_id IS NOT NULL ORDER BY id"
    ).fetchall()
    assert rows == [(m1, text_notice, "Aviso de teste", "rich", NOTICE_PRIORITY, None, None, None, None, t0, t0,
                     text_key),
                    (m2, preview_notice, None, None, NOTICE_PRIORITY, "question", "send", None, None, t0, t0,
                     content_key)]


# ---------------------------------------------------------------------------------------------
# O despacho pelo caminho do disparo
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", ["rich", "html", "plain"])
def test_notice_goes_out_by_the_dispatch_path_and_a_line_without_dialect_stays_plain(ctx: Ctx, dialect: str) -> None:
    """A linha de aviso sai pelo caminho do disparo no
    dialeto gravado: no Telegram, Rich por `sendRichMessage` com `skip_entity_detection: true`, HTML por
    `sendMessage` com `parse_mode = "HTML"` e a prévia de link desligada; no console, a linha em texto
    simples com só as 4 chaves da API base (sem campo novo). Na mesma função, a regressão: uma
    mensagem antiga pelo mesmo bot, sem `text_format`, sai igual a antes (`sendMessage` `{chat_id, text}`)."""
    stream = io.StringIO()
    channel: Any = ConsoleChannel(stream) if dialect == "plain" else ctx.telegram(
        DeliverySettings(telegram_format=dialect))
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    assert ctx.deliver(notice_id, channel) == teamnotices.NoticeState.QUEUED
    members = [ctx.member(m) for m in ctx.team.active]
    r3 = ctx.student()
    with psycopg.connect(ctx.db_url) as conn:
        legacy = Outbox(ctx.db_url, clock=ctx.clock, settings=ctx.settings)
        try:
            r3_id = legacy.enqueue(conn, recipient=r3.uuid, bot_id=ctx.bot_row, text="Mensagem antiga ao aluno")
        finally:
            legacy.close()
    box = ctx.outbox(channel)

    taken = _drain(box, channel)

    rows = ctx.rows(notice_id)
    assert sorted(taken) == sorted([r["id"] for r in rows] + [r3_id])
    assert all(ctx.row(m)["state"] == "sent" for m in taken)
    if dialect == "plain":
        lines = read_lines(stream.getvalue())
        mine = {line.message_id: line for line in lines}
        for row, member in zip(rows, members):
            line = mine[row["id"]]
            assert set(line.raw) == PLAIN_KEYS and line.recipient == member.pseudo_id
            assert line.text == row["text"]
            _assert_lines(line.text, ctx.opened_lines(d))
        assert set(mine[r3_id].raw) == PLAIN_KEYS and mine[r3_id].text == "Mensagem antiga ao aluno"
        return
    for row, member in zip(rows, members):
        [msg] = ctx.rich.messages(ctx.bot, member.chat)[-1:]
        assert str(msg.message_id) == row["channel_ref"]
        if dialect == "rich":
            assert (msg.method, msg.rich_message["skip_entity_detection"], msg.rich_message["html"]) == (
                "sendRichMessage", True, row["text"])
        else:
            assert (msg.method, msg.parse_mode, msg.link_preview_options, msg.text) == (
                "sendMessage", "HTML", {"is_disabled": True}, row["text"])
        assert msg.keyboard is None and msg.force_reply is False
        _assert_lines(msg.plain_text or "", ctx.opened_lines(d))
    [r3_call] = [c for c in ctx.rich.calls("sendMessage") if _chat_of(c) == r3.chat]
    assert r3_call.params == {"chat_id": r3.chat, "text": "Mensagem antiga ao aluno"}


def test_priority_two_waits_behind_student_messages_and_fixed_replies(ctx: Ctx) -> None:
    """No mesmo bot, a linha de aviso (prioridade 2, enfileirada
    primeiro) só sai depois da mensagem ao aluno (0) e da resposta fixa (1) enfileiradas depois dela; com a
    prioridade igual, vale o menor `id`."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    student = ctx.student()
    assert ctx.deliver(notice_id, ctx.telegram()) == teamnotices.NoticeState.QUEUED
    notice_rows = [r["id"] for r in ctx.rows(notice_id)]
    sender = Outbox(ctx.db_url, clock=ctx.clock, settings=ctx.settings)
    try:
        with psycopg.connect(ctx.db_url) as conn:
            to_student = sender.enqueue(conn, recipient=student.uuid, bot_id=ctx.bot_row, text="Mensagem ao aluno")
            fixed = sender.enqueue(conn, recipient=student.uuid, bot_id=ctx.bot_row, text="Resposta fixa",
                                   priority=1)
    finally:
        sender.close()
    assert min(notice_rows) < to_student < fixed
    scripted = ScriptedChannel([Sent(f"ref-{i}") for i in range(4)])
    box = ctx.outbox(scripted)

    taken = _drain(box, scripted)

    assert taken == [to_student, fixed, *notice_rows]
    assert [c.text_format for c in scripted.calls] == ["plain", "plain", "rich", "rich"]
    assert [ctx.row(m)["priority"] for m in taken] == [0, 1, NOTICE_PRIORITY, NOTICE_PRIORITY]


def test_order_per_endpoint_holds_the_second_notice_behind_a_temporary_failure(ctx: Ctx) -> None:
    """Duas mensagens de aviso ao mesmo endereço saem em
    ordem; com falha temporária na primeira (de volta a `pending`, com espera), a segunda não é candidata
    enquanto a primeira está `pending`/`leased`, embora disponível; o endereço do outro membro não espera.
    Vencida a espera, a primeira sai e só então a segunda."""
    first, second = ctx.opened(), ctx.opened()
    a = ctx.record_opened(first)
    b = ctx.record_opened(second)
    channel = ctx.telegram()
    assert ctx.deliver(a, channel) == teamnotices.NoticeState.QUEUED
    assert ctx.deliver(b, channel) == teamnotices.NoticeState.QUEUED
    a1, a2 = [r["id"] for r in ctx.rows(a)]
    b1, b2 = [r["id"] for r in ctx.rows(b)]
    scripted = ScriptedChannel([RetryLater("rede"), Sent("a2"), Sent("b2"), Sent("a1"), Sent("b1")])
    box = ctx.outbox(scripted)

    assert _drain(box, scripted) == [a1, a2, b2]
    held = ctx.row(a1)
    assert (held["state"], held["attempts"]) == ("pending", 1)
    assert held["available_at"] > ctx.clock.now()
    assert ctx.row(b1)["state"] == "pending" and ctx.row(b1)["attempts"] == 0
    ctx.clock.set(held["available_at"])
    assert _drain(box, scripted) == [a1, b1]
    assert [c.message_id for c in scripted.calls] == [a1, a2, b2, a1, b1]
    assert all(ctx.row(m)["state"] == "sent" for m in (a1, a2, b1, b2))


# ---------------------------------------------------------------------------------------------
# Falhas do envio
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("team_members", [(ACTIVE, ACTIVE, ACTIVE)], indirect=True)
def test_429_5xx_and_network_retry_with_growing_wait_and_403_blocks_only_that_member(ctx: Ctx) -> None:
    """Pela Bot API falsa, o 429 com `retry_after` espera o maior
    entre o `retry_after` e a espera crescente (e segura o chat no `SendPacer`); o 5xx e a queda da rede
    voltam a `pending` com espera crescente (2 s → 4 s, `backoff_base` 2); o 403 de bloqueio leva só aquela
    mensagem a `failed` e o endereço a `blocked`, com `outbox.failed` e `endpoint.blocked`; as mensagens dos
    demais terminam `sent`. Nenhum evento por destinatário além dos da fila."""
    m1, m2, m3 = (ctx.member(m) for m in ctx.team.members)
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    channel = ctx.telegram()
    assert ctx.deliver(notice_id, channel) == teamnotices.NoticeState.QUEUED
    r1, r2, r3 = [r["id"] for r in ctx.rows(notice_id)]
    ctx.rich.fail("sendRichMessage", retry_after=7, times=1, when=lambda c: _chat_of(c) == m1.chat)
    ctx.rich.fail("sendRichMessage", status=502, times=1, when=lambda c: _chat_of(c) == m2.chat)
    ctx.rich.fail_network("sendRichMessage", kind="reset", times=1, when=lambda c: _chat_of(c) == m2.chat)
    ctx.rich.fail("sendRichMessage", error="blocked", times=1, when=lambda c: _chat_of(c) == m3.chat)
    mono = [1000.0]
    box = ctx.outbox(channel, pacer=SendPacer(chat_interval_seconds=0.0, monotonic=lambda: mono[0]))
    t0 = ctx.clock.now()
    cursor = ctx.events("outbox.failed", "endpoint.blocked")

    assert sorted(_drain(box, channel)) == [r1, r2, r3]
    assert (ctx.row(r1)["state"], ctx.row(r1)["attempts"], ctx.row(r1)["available_at"]) == (
        "pending", 1, t0 + timedelta(seconds=7))
    assert (ctx.row(r2)["state"], ctx.row(r2)["attempts"], ctx.row(r2)["available_at"]) == (
        "pending", 1, t0 + timedelta(seconds=2))
    failed = ctx.row(r3)
    assert (failed["state"], failed["attempts"]) == ("failed", 1)
    assert ctx.endpoint_state(m3.endpoint_row) == "blocked"

    ctx.clock.set(t0 + timedelta(seconds=2))
    mono[0] += 2
    assert _drain(box, channel) == [r2]
    assert (ctx.row(r2)["state"], ctx.row(r2)["attempts"], ctx.row(r2)["available_at"]) == (
        "pending", 2, t0 + timedelta(seconds=6))
    ctx.clock.set(t0 + timedelta(seconds=6))
    mono[0] += 4
    assert _drain(box, channel) == [r2]
    ctx.clock.set(t0 + timedelta(seconds=7))
    mono[0] += 1
    assert _drain(box, channel) == [r1]

    assert [(ctx.row(r)["state"], ctx.row(r)["attempts"]) for r in (r1, r2, r3)] == [
        ("sent", 2), ("sent", 3), ("failed", 1)]
    assert ctx.events("outbox.failed", "endpoint.blocked")[len(cursor):] == [
        ("outbox.failed", iso(t0), None, {"message_id": r3, "recipient": m3.pseudo_id, "attempts": 1,
                                          "error": failed["last_error"]}),
        ("endpoint.blocked", iso(t0), None, {"offering_id": ctx.offering["offering_id"], "person": m3.pseudo_id,
                                             "channel": "telegram"})]
    assert [m.state for m in team_messages(ctx.sql, notice_id=notice_id)] == ["sent", "sent", "failed"]
    assert len(ctx.events("notice.recorded")) == 1


def test_bot_without_access_retries_until_exhausted_and_fails_with_outbox_failed(ctx: Ctx) -> None:
    """Com o bot sem acesso, as mensagens de aviso
    **são** candidatas (só as de entrega são retidas); o canal devolve `RetryLater("bot_no_access")` sem
    chamar o Telegram, e cada mensagem volta a `pending` com espera crescente até esgotar as tentativas
    (3, `settings`), quando vira `failed` com `outbox.failed`."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    channel = ctx.telegram()
    assert ctx.deliver(notice_id, channel) == teamnotices.NoticeState.QUEUED
    rows = [r["id"] for r in ctx.rows(notice_id)]
    version = int(ctx.sql.execute("SELECT token_version FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone()[0])
    with psycopg.connect(ctx.db_url, autocommit=True) as conn:
        assert mark_no_access(conn, bot_id=ctx.bot_row, token_version=version, problem="token_rejected",
                              now=ctx.clock.now()) is True
    box = ctx.outbox(channel)
    sends_before = [c.seq for c in ctx.rich.calls() if c.method in SEND_METHODS]
    cursor = len(ctx.events("outbox.failed"))
    waits: list[float] = []

    for attempt in range(1, ctx.settings.max_attempts + 1):
        assert sorted(_drain(box, channel)) == rows
        states = [(ctx.row(r)["state"], ctx.row(r)["attempts"]) for r in rows]
        if attempt < ctx.settings.max_attempts:
            assert states == [("pending", attempt)] * len(rows)
            available = ctx.row(rows[0])["available_at"]
            waits.append((available - ctx.clock.now()).total_seconds())
            ctx.clock.set(available)
        else:
            assert states == [("failed", attempt)] * len(rows)
    t_end = ctx.clock.now()

    assert waits == [2.0, 4.0]
    assert [c.seq for c in ctx.rich.calls() if c.method in SEND_METHODS] == sends_before
    members = [ctx.member(m) for m in ctx.team.active]
    assert ctx.events("outbox.failed")[cursor:] == [
        ("outbox.failed", iso(t_end), None, {"message_id": r, "recipient": m.pseudo_id,
                                             "attempts": ctx.settings.max_attempts, "error": "bot_no_access"})
        for r, m in zip(rows, members)]


def test_access_back_before_the_attempts_run_out_sends_the_notice_once(ctx: Ctx) -> None:
    """Bot sem acesso no momento de um aviso:
    o aviso é enfileirado com o bot **já** sem acesso; a primeira tentativa de cada mensagem
    é recusada (`RetryLater("bot_no_access")`, sem chamar o Telegram: o controle positivo da recusa), e a
    mensagem volta a `pending` com a espera de 2 s. O acesso volta pela troca do token (`bots.register` com
    o token girado na Bot API falsa, como em `test_offering_notices.py`) antes de esgotar as tentativas (3):
    vencida a espera (e não antes), cada mensagem sai **uma** vez, pelo token novo, na 2ª tentativa; depois
    disso a fila não tem candidata. Uma chamada de envio e uma mensagem por destinatário, nenhuma
    `outbox.failed`, um único `notice.recorded` e o episódio (`lost` e `restored`) registrado uma vez."""
    from tests.helpers.api import register_bot

    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    channel = ctx.telegram()
    members = [ctx.member(m) for m in ctx.team.active]
    access_cursor = len(ctx.events("bot.access_changed"))
    failed_cursor = len(ctx.events("outbox.failed"))
    version = int(ctx.sql.execute("SELECT token_version FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone()[0])
    t_lost = ctx.clock.now()
    with psycopg.connect(ctx.db_url, autocommit=True) as conn:
        assert mark_no_access(conn, bot_id=ctx.bot_row, token_version=version, problem="token_rejected",
                              now=t_lost) is True
    assert ctx.deliver(notice_id, channel) == teamnotices.NoticeState.QUEUED
    rows = [r["id"] for r in ctx.rows(notice_id)]
    assert len(rows) == len(members) == 2
    box = ctx.outbox(channel)
    sends_before = {c.seq for c in ctx.rich.calls() if c.method in SEND_METHODS}

    # 1ª tentativa, com o bot sem acesso: recusada sem chamar o Telegram.
    t_first = ctx.clock.now()
    assert sorted(_drain(box, channel)) == rows
    for message_id in rows:
        row = ctx.row(message_id)
        assert (row["state"], row["attempts"], row["last_error"], row["available_at"], row["channel_ref"]) == (
            "pending", 1, "bot_no_access", t_first + timedelta(seconds=2), None)
    assert {c.seq for c in ctx.rich.calls() if c.method in SEND_METHODS} == sends_before

    # O acesso volta antes de esgotar as tentativas; a espera de cada mensagem continua valendo.
    ctx.clock.advance(1)
    t_restored = ctx.clock.now()
    fake_version = ctx.rich.state(ctx.bot).token_version
    register_bot(ctx.client, ADMIN_TOKEN, ctx.code, ctx.rich.rotate_token(ctx.bot))
    assert ctx.sql.execute("SELECT state, token_version FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone() == (
        "registered", version + 1)
    assert _drain(box, channel) == []
    assert [(ctx.row(m)["state"], ctx.row(m)["attempts"]) for m in rows] == [("pending", 1)] * 2

    # 2ª tentativa, vencida a espera: cada mensagem sai uma vez; depois, nada mais.
    ctx.clock.set(t_first + timedelta(seconds=2))
    assert sorted(_drain(box, channel)) == rows
    ctx.clock.advance(ctx.settings.backoff_max_seconds + 1)
    assert _drain(box, channel) == []

    assert [(ctx.row(m)["state"], ctx.row(m)["attempts"]) for m in rows] == [("sent", 2)] * 2
    assert [m.state for m in team_messages(ctx.sql, notice_id=notice_id)] == ["sent", "sent"]
    assert ctx.sql.execute("SELECT count(*) FROM outbox_messages WHERE notice_id = %s",
                           (notice_id,)).fetchone()[0] == 2
    sends = [c for c in ctx.rich.calls() if c.method in SEND_METHODS and c.seq not in sends_before]
    assert sorted(_chat_of(c) for c in sends) == sorted(m.chat for m in members)
    assert [(c.method, c.outcome, c.token_state, c.token_version) for c in sends] == [
        ("sendRichMessage", "ok", "valid", fake_version + 1)] * 2
    for message_id, member in zip(rows, members):
        row = ctx.row(message_id)
        opened = [msg for msg in ctx.rich.messages(ctx.bot, member.chat)
                  if f"Disparo aberto: {TITLE}" in (msg.plain_text or "")]
        assert len(opened) == 1 and str(opened[0].message_id) == row["channel_ref"]
        assert opened[0].rich_message["html"] == row["text"]
    notice = ctx.notice(notice_id)
    assert (notice["state"], notice["recipient_count"], notice["finished_at"]) == ("queued", 2, t_lost)
    assert ctx.events("outbox.failed")[failed_cursor:] == []
    assert ctx.events("notice.recorded") == [ctx.recorded(d, state="queued", count=2, at=t_lost)]
    assert [(e[0], e[1], e[3]) for e in ctx.events("bot.access_changed")[access_cursor:]] == [
        ("bot.access_changed", iso(t_lost),
         {"offering_id": ctx.offering["offering_id"], "access": "lost", "problem": "token_rejected"}),
        ("bot.access_changed", iso(t_restored),
         {"offering_id": ctx.offering["offering_id"], "access": "restored", "problem": None})]


# ---------------------------------------------------------------------------------------------
# Queda, posse esgotada e concorrência
# ---------------------------------------------------------------------------------------------


class _CrashOnFormatter:
    """O canal real com uma queda (`Crash`) na primeira leitura do formatador: a queda do worker no meio
    da fase 2 (o `deliver` compõe o texto com o `channel.formatter`), antes de qualquer escrita."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.capabilities = inner.capabilities
        self.crashes = 1

    @property
    def formatter(self) -> Any:
        if self.crashes:
            self.crashes -= 1
            raise Crash()
        return self._inner.formatter

    def formatter_for(self, text_format: str) -> Any:
        return self._inner.formatter_for(text_format)

    def send(self, message: OutgoingMessage) -> Any:
        return self._inner.send(message)


def test_crash_in_phase_two_and_exhausted_leases_neither_duplicate_nor_lose(ctx: Ctx) -> None:
    """Sem duplicar nem perder: (1) a queda do
    worker (`Crash`) na fase 2 do `notice.send` não deixa nada (o aviso continua `pending`, nenhuma linha na
    fila, nenhum evento) e a tarefa fica em posse; (2) com a posse esgotada, a fila de tarefas leva a tarefa a
    `failed` e a regra `notice.send` cria a rodada seguinte, que enfileira uma mensagem por destinatário;
    (3) a reexecução depois do commit não tem efeito; (4) na fila de saída, a queda no envio deixa a
    mensagem em posse, retomada e enviada uma vez depois da posse, e a mensagem com a posse esgotada (de um
    worker que caiu) vira `failed` com `outbox.failed`. No fim, no máximo uma mensagem por par aviso ×
    destinatário, e toda mensagem termina `sent` ou `failed`."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    channel = ctx.telegram()
    crashing = ctx.report_handlers(_CrashOnFormatter(channel))
    assert ctx.sweep(SEND_TASK).enqueued == {SEND_TASK: 1}
    [task] = ctx.sql.execute("SELECT id, idempotency_key, payload FROM tasks WHERE task_type = %s",
                             (SEND_TASK,)).fetchall()
    assert (task[1], task[2]) == (f"{SEND_TASK}:{notice_id}:r1", _subject(SEND_TASK, notice_id))

    with pytest.raises(Crash):
        ctx.queue.work_once(CRASHED_WORKER, crashing, task_types=[SEND_TASK])
    assert (ctx.notice(notice_id)["state"], ctx.rows(notice_id), ctx.events("notice.recorded")) == ("pending", [], [])
    assert ctx.sql.execute("SELECT state, lease_owner FROM tasks WHERE id = %s", (task[0],)).fetchone() == (
        "leased", CRASHED_WORKER)

    ctx.sql.execute("UPDATE tasks SET attempts = %s WHERE id = %s", (ctx.settings.max_attempts, task[0]))
    ctx.clock.advance(EXPIRE)
    good = ctx.report_handlers(channel)
    assert ctx.queue.work_once(WORKER, good, task_types=[SEND_TASK]) is None
    assert ctx.sql.execute("SELECT state, last_error FROM tasks WHERE id = %s", (task[0],)).fetchone() == (
        "failed", LEASE_EXHAUSTED_ERROR)
    ctx.clock.advance(ctx.settings.backoff_max_seconds + 1)
    assert ctx.sweep(SEND_TASK).enqueued == {SEND_TASK: 1}
    t_queued = ctx.clock.now()
    assert ctx.queue.work_once(WORKER, good, task_types=[SEND_TASK]) is not None
    assert ctx.notice(notice_id)["state"] == "queued"
    r1, r2 = [r["id"] for r in ctx.rows(notice_id)]

    ctx.rerun(good, SEND_TASK, _subject(SEND_TASK, notice_id), f"{SEND_TASK}:{notice_id}:reexecucao")
    assert ctx.sweep(SEND_TASK).enqueued == {}
    assert [r["id"] for r in ctx.rows(notice_id)] == [r1, r2]

    scripted = ScriptedChannel([Crash(), Sent("depois-da-queda")])
    box = ctx.outbox(scripted)
    with pytest.raises(Crash):
        box.dispatch_once(scripted, CRASHED_WORKER, include_routed=True)
    assert (ctx.row(r1)["state"], ctx.row(r1)["lease_owner"]) == ("leased", CRASHED_WORKER)
    ctx.sql.execute("UPDATE outbox_messages SET state = 'leased', lease_owner = %s, lease_until = %s, attempts = %s "
                    "WHERE id = %s", (CRASHED_WORKER, ctx.clock.now() + timedelta(seconds=1),
                                      ctx.settings.max_attempts, r2))
    ctx.clock.advance(EXPIRE)
    t_end = ctx.clock.now()
    cursor = len(ctx.events("outbox.failed"))
    assert _drain(box, scripted) == [r1]

    assert [(ctx.row(r)["state"], ctx.row(r)["attempts"]) for r in (r1, r2)] == [
        ("sent", 2), ("failed", ctx.settings.max_attempts)]
    second = ctx.member(ctx.team.active[1])
    assert ctx.events("outbox.failed")[cursor:] == [
        ("outbox.failed", iso(t_end), None, {"message_id": r2, "recipient": second.pseudo_id,
                                             "attempts": ctx.settings.max_attempts, "error": LEASE_EXHAUSTED_ERROR})]
    pairs = ctx.sql.execute("SELECT notice_id, recipient_person_id, count(*) FROM outbox_messages "
                            "WHERE notice_id IS NOT NULL GROUP BY 1, 2").fetchall()
    assert sorted(count for _, _, count in pairs) == [1, 1]
    assert ctx.events("notice.recorded") == [ctx.recorded(d, state="queued", count=2, at=t_queued)]


def _lock_waiters(sql: psycopg.Connection) -> int:
    return int(sql.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND wait_event_type = 'Lock'").fetchone()[0])


def test_two_notice_send_tasks_of_the_same_notice_on_two_connections_enqueue_once(ctx: Ctx) -> None:
    """Concorrência: duas tarefas
    `notice.send` do mesmo aviso, cada uma num tratador com a sua conexão, rodam ao mesmo tempo enquanto o
    teste segura a linha do aviso; soltas, uma enfileira e a outra não tem efeito (o aviso já fora de
    `pending`): uma mensagem por destinatário, `queued` e um único `notice.recorded`, sem erro em nenhuma."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    channel = ctx.telegram()
    first, second = ctx.report_handlers(channel), ctx.report_handlers(channel)
    subject = _subject(SEND_TASK, notice_id)
    with psycopg.connect(ctx.db_url) as conn:
        for key in ("r1", "dup"):
            enqueue_in_transaction(conn, SEND_TASK, subject, now=ctx.clock.now(),
                                   idempotency_key=f"{SEND_TASK}:{notice_id}:{key}")
    tasks = [ctx.claim(SEND_TASK, subject), ctx.claim(SEND_TASK, subject)]
    assert all(t is not None for t in tasks)
    errors: list[BaseException] = []

    def run(handlers: dict, task: ClaimedTask) -> None:
        try:
            handlers[SEND_TASK](task)
        except BaseException as exc:  # noqa: BLE001 - conferida pelo teste
            errors.append(exc)

    with psycopg.connect(ctx.db_url) as holder:
        with holder.transaction():
            holder.execute("SELECT 1 FROM team_notices WHERE id = %s FOR UPDATE", (notice_id,))
            threads = [threading.Thread(target=run, args=(h, t), daemon=True)
                       for h, t in zip((first, second), tasks)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _lock_waiters(ctx.sql) < 2 and any(t.is_alive() for t in threads):
                time.sleep(0.02)
        for thread in threads:
            thread.join(10)
    assert not any(t.is_alive() for t in threads), "um tratador ficou preso"

    assert errors == []
    assert ctx.notice(notice_id)["state"] == "queued"
    assert [(r["recipient_person_id"], r["delivery_endpoint_id"]) for r in ctx.rows(notice_id)] == [
        (r.person_row_id, r.endpoint_row_id) for r in ctx.recipients()]
    assert len(ctx.events("notice.recorded")) == 1


# ---------------------------------------------------------------------------------------------
# A mensagem da prévia na posse e a regra `notice.send`
# ---------------------------------------------------------------------------------------------


def test_preview_message_is_a_candidate_only_with_the_notice_loader_in_the_two_step_claim(ctx: Ctx) -> None:
    """Com a prévia de um disparo agendado gravada pela
    tarefa `preview.send` (o cabeçalho na forma de texto e a questão na forma 3, sem texto), (a) a posse de
    duas transações **sem** `notice_content` leva só os cabeçalhos (com `answerable = True`) e não
    toca as mensagens da forma 3; (b) a posse legada não as candidata (`m.text IS NOT NULL`), (c) nem com o
    `notice_content` (o `_legacy` não muda); (d) com a fila montada como a do worker (`content`, `outcome` e
    `notice_content=NoticeContentLoader()`), cada questão sai pela carga, com o teclado `cq1:` da prévia."""
    d = ctx.validated(qid=SCHEDULED_QID, opens_at=ctx.clock.now() + timedelta(hours=1))
    channel = ctx.telegram()
    engine = HttpQuestionEngine(EngineSettings(engine_url=ctx.engine_fake.base_url, timeout_seconds=10.0))
    ctx.stack.callback(engine.close)
    handler = previews.preview_task_handler(ctx.db_url, clock=ctx.clock, settings=ctx.settings, engine=engine,
                                            channel=channel)
    ctx.stack.callback(handler.close)
    [(preview_row, token)] = ctx.sql.execute("SELECT id, button_token FROM dispatch_previews WHERE dispatch_id = %s",
                                             (d.row,)).fetchall()
    assert ctx.sweep(PREVIEW_TASK).enqueued == {PREVIEW_TASK: 1}
    assert ctx.run(PREVIEW_TASK, handler, _subject(PREVIEW_TASK, preview_row)) == 1
    messages = team_messages(ctx.sql, "dispatch_preview")
    headers = [m for m in messages if m.text is not None]
    questions = [m for m in messages if m.role == "question"]
    assert len(headers) == len(questions) == len(ctx.team.active)
    assert all(m.text is None and m.text_format is None and m.priority == NOTICE_PRIORITY for m in questions)

    scripted = ScriptedChannel([Sent(f"cabecalho-{i}") for i in range(len(headers))])
    without_loader = ctx.outbox(scripted, notice_loader=False)
    assert _drain(without_loader, scripted) == [m.message_id for m in headers]
    assert all(call.answerable is True and call.text_format == "rich" for call in scripted.calls)
    untouched = [(ctx.row(m.message_id)["state"], ctx.row(m.message_id)["attempts"]) for m in questions]
    assert untouched == [("pending", 0)] * len(questions)

    for legacy in (ctx.outbox(scripted, notice_loader=False, legacy=True),
                   ctx.outbox(scripted, notice_loader=True, legacy=True)):
        assert legacy.dispatch_once(scripted, WORKER, include_routed=True) is None
    assert [(ctx.row(m.message_id)["state"], ctx.row(m.message_id)["attempts"]) for m in questions] == untouched

    like_worker = ctx.outbox(channel, notice_loader=True)
    assert _drain(like_worker, channel) == [m.message_id for m in questions]
    for message in questions:
        row = ctx.row(message.message_id)
        assert (row["state"], row["attempts"]) == ("sent", 1)
        member = next(m for m in ctx.team.active if m.pseudo_id == message.recipient)
        sent = ctx.rich.current(ctx.bot, member.chat_id, int(row["channel_ref"]))
        data = [b.data for b in sent.buttons()]
        assert data and all(isinstance(x, str) and x.startswith("cq1:") and token in x for x in data)


def test_sweep_rule_notice_send_revives_a_pending_notice_without_live_task(ctx: Ctx) -> None:
    """O aviso `pending` sem tarefa viva vence a regra `notice.send`
    (`s.state = 'pending'`): a rodada enfileira uma tarefa com o payload do assunto e a chave
    `notice.send:<id>:r1`; com a tarefa viva, a rodada seguinte não enfileira outra; processada, o aviso
    fica `queued` e a regra deixa de vencer, mesmo depois do intervalo."""
    d = ctx.opened()
    notice_id = ctx.record_opened(d)
    handlers = ctx.report_handlers(ctx.telegram())

    assert ctx.sweep(SEND_TASK).enqueued == {SEND_TASK: 1}
    assert ctx.sweep(SEND_TASK).enqueued == {}
    rows = ctx.sql.execute("SELECT idempotency_key, payload, state FROM tasks WHERE task_type = %s ORDER BY id",
                           (SEND_TASK,)).fetchall()
    assert rows == [(f"{SEND_TASK}:{notice_id}:r1", _subject(SEND_TASK, notice_id), "pending")]
    assert ctx.queue.work_once(WORKER, handlers, task_types=[SEND_TASK]) is not None
    ctx.clock.advance(ctx.settings.backoff_max_seconds + 5)

    assert ctx.sweep(SEND_TASK).enqueued == {}
    assert ctx.notice(notice_id)["state"] == "queued"
    assert len(ctx.rows(notice_id)) == len(ctx.team.active)
    assert ctx.sql.execute("SELECT count(*) FROM tasks WHERE task_type = %s", (SEND_TASK,)).fetchone()[0] == 1
