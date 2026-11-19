"""Fila de saída das entregas (disparo e resposta).

Cobre a fila das entregas e as mensagens fora das entregas, que saem sem atraso, com as escolhas
de teste para a fusão das edições, o bot sem acesso, o cancelamento, a edição tardia e o dialeto (na
recepção, na fila e no worker, e no modo de testar deste
arquivo):
- a fusão das edições pendentes de uma mensagem (`enqueue_delivery_edit`: a última vence,
  `render_version = greatest(...)`, `edit_text` se alguma é de texto) e a edição em voo, que nunca é
  alterada; as mensagens fora das entregas sem atraso com o `FakeClock` parado (na mesma função da fusão);
- a ordem por entrega (cada mensagem só depois do desfecho da anterior), a prioridade 1 das respostas
  fixas atrás das mensagens de entrega e a posse pelo menor `id` com prioridade igual;
- o desfecho da `question` gravado na mesma transação do envio (`DeliveryOutcome`), conferido por um
  gatilho de restrição adiado criado pelo teste no banco do teste;
- o cancelamento das mensagens `pending` da entrega no fechamento e no 400 definitivo da `question`
  (na mesma transação, também por gatilho adiado);
- a posse esgotada montada por SQL (numa `question` → `not_delivered/channel_unavailable`; numa edição →
  `fallback`), o `Sent` tardio numa entrega que já virou `not_delivered`, o bot sem acesso,
  o dialeto gravado e a edição em voo que volta a `pending` absorvendo a pendente.


**Montagem.** Como em `test_dispatch_opening.py`: oferecimento, bot e ativação pela API de oferecimentos,
pessoas pela API base, matrícula ativa e endereço por SQL, e o disparo preparado e confirmado pela API e
processado pelo worker real (`run_worker(…, bots=…, engine=…, fetcher=…, delivery=…)`) com o adaptador
HTTP real do motor sobre o motor falso estrito, a obtenção falsa e a `rich_bot_api`. Os estados da fila
que dependem de uma queda (linha `leased` de outro worker, posse esgotada) são montados por SQL, como no
vínculo do aluno; as edições diretas usam a função da interface da fila (`outbox.enqueue_delivery_edit`, na
fila do worker). No esqueleto, cada teste falha no primeiro passo do disparo (a rota `dispatches.prepare`
ausente, 404, ou uma tabela nova ausente).
"""

from __future__ import annotations

import dataclasses
import itertools
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import deliverytexts
from cq_server._leasing import LEASE_EXHAUSTED_ERROR
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.adapters.telegram import http_factory
from cq_server.bots import BotRuntime, TelegramChannel
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.outbox import Outbox, enqueue_delivery_edit
from cq_server.pacing import SendPacer
from cq_server.ports.delivery import OutgoingMessage, Sent
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, confirm_dispatch, create_offering,
                               prepare_dispatch, register_bot, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.engine import FakeQuestion, preset
from tests.helpers.logs import capture_logs

REPO = "https://repos.cq.test/disparos/fila.git"
COMMIT = "f1a5" * 10
TZ = "America/Sao_Paulo"
WAIT = 20.0
FAST = DeliverySettings(chat_interval_seconds=0.05)
SEND_METHODS = ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument")
EDIT_METHODS = ("editMessageText", "editMessageReplyMarkup")

_codes = itertools.count(1)


def _chat_of(call: Any) -> int | None:
    """O `chat_id` de um pedido (no multipart das mídias, ele chega como texto)."""
    value = call.params.get("chat_id")
    return int(value) if isinstance(value, (int, str)) and str(value).lstrip("-").isdigit() else None


@dataclasses.dataclass(frozen=True)
class Student:
    pseudo_id: str
    person_row: int
    enrollment_row: int | None
    endpoint_row: int | None
    user: Any

    @property
    def chat(self) -> int:
        return self.user.user_id

    @property
    def uuid(self) -> uuid.UUID:
        return uuid.UUID(self.pseudo_id)


@dataclasses.dataclass(frozen=True)
class Disp:
    id: str
    row: int


class Flow:
    """Um oferecimento ativo com bot, os alunos e o disparo do teste (ver o docstring do módulo)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any,
                 rich: Any, engine_fake: Any, repo_fetcher: Any, start: Any, stop_one: Any) -> None:
        self.client, self.sql, self.db_url, self.clock, self.settings = client, sql, db_url, clock, settings
        self.rich, self.engine_fake, self.fetcher = rich, engine_fake, repo_fetcher
        self._start, self._stop_one = start, stop_one
        self._engines: list[Any] = []
        self._threads: list[Any] = []
        self._users = itertools.count(1)
        self._workers = itertools.count(1)
        self.offering = create_offering(client, ADMIN_TOKEN, code=f"disparo-fila-{next(_codes)}", timezone=TZ)
        self.code = self.offering["code"]
        self.bot = rich.add_bot()
        register_bot(client, ADMIN_TOKEN, self.code, rich.token_of(self.bot))
        activate_bot(client, ADMIN_TOKEN, self.code)
        self.offering_row, self.bot_row = sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id "
            "WHERE b.telegram_bot_id = %s", (self.bot,)).fetchone()

    # -- montagem ------------------------------------------------------------------------------

    def program(self, qid: str, question: FakeQuestion) -> None:
        self.engine_fake.program((REPO, COMMIT, qid), question)

    def student(self, *, enrollment: str | None = "active") -> Student:
        """Uma pessoa com endereço `active` no bot e, salvo `enrollment=None`, matrícula de aluno."""
        n = next(self._users)
        user = make_user(800 + n)
        person = register_person(self.client, ADMIN_TOKEN, name=f"Aluno Fila {n}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (person["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment_row = None
        if enrollment is not None:
            enrollment_row = self.sql.execute(
                "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
                "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) "
                "RETURNING id", (person_row, self.offering_row, now, now, now)).fetchone()[0]
        endpoint_row = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, self.bot_row, user.user_id, now, now)).fetchone()[0]
        # O chat passa a ser conhecido do dublê (senão, 400 `chat not found`).
        self.rich.push_my_chat_member(self.bot, user, old_status="kicked", new_status="member")
        return Student(person["pseudo_id"], person_row, enrollment_row, endpoint_row, user)

    def prepare(self, qid: str, *, closes_at: datetime | None = None) -> Disp:
        closes_at = closes_at if closes_at is not None else self.clock.now() + timedelta(hours=2)
        body = prepare_dispatch(self.client, ADMIN_TOKEN, self.code, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(closes_at))
        row = self.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                               (body["dispatch_id"],)).fetchone()[0]
        return Disp(body["dispatch_id"], row)

    def confirm(self, d: Disp) -> dict:
        return confirm_dispatch(self.client, ADMIN_TOKEN, self.code, d.id)

    def edit(self, delivery: dict, student: Student, *, markup_only: bool, version: int,
             role: str = "question") -> int:
        """Uma edição enfileirada pela função do contrato, numa transação confirmada no fim."""
        with psycopg.connect(self.db_url) as conn:
            return enqueue_delivery_edit(conn, delivery_row_id=delivery["id"], recipient=student.uuid,
                                         bot_id=self.bot_row, endpoint_row_id=student.endpoint_row, role=role,
                                         markup_only=markup_only, version=version)

    def lease_by_other(self, message_row: int, *, until: datetime, attempts: int | None = None,
                       owner: str = "worker-caido") -> None:
        """A linha em posse de outro worker (a queda montada por SQL)."""
        self.sql.execute(
            "UPDATE outbox_messages SET state = 'leased', lease_owner = %s, lease_until = %s, "
            "attempts = coalesce(%s, attempts) WHERE id = %s", (owner, until, attempts, message_row))

    # -- worker --------------------------------------------------------------------------------

    def engine(self) -> Any:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url, timeout_seconds=10))
        self._engines.append(engine)
        return engine

    def start(self, *, delivery: DeliverySettings = FAST, **kwargs: Any) -> Any:
        settings = dataclasses.replace(self.settings, worker_id=f"worker-fila-{next(self._workers)}")
        thread = self._start(settings=settings, engine=self.engine(), fetcher=self.fetcher, delivery=delivery,
                             **kwargs)
        self._threads.append(thread)
        return thread

    def stop_all(self) -> None:
        for thread in self._threads:
            if thread.is_alive():
                assert self._stop_one(thread) != float("inf"), "o worker não parou em 10 s"

    def close(self) -> None:
        for engine in self._engines:
            engine.close()

    # -- espera --------------------------------------------------------------------------------

    def wait(self, predicate: Callable[[], Any], timeout: float = WAIT) -> Any:
        return self.rich.wait_for(predicate, timeout=timeout)

    def wait_state(self, d: Disp, state: str) -> dict:
        def ready() -> dict | None:
            row = self.dispatch(d)
            if row["state"] == state:
                return row
            assert row["state"] in ("validating", "scheduled", "open"), (
                f"disparo em {row['state']} (rejeição {row['rejection']}, cancelamento {row['cancel_reason']})")
            return None
        return self.wait(ready)

    def validated(self, d: Disp, **kwargs: Any) -> Any:
        worker = self.start(**kwargs)
        self.wait_state(d, "scheduled")
        return worker

    def settled(self, d: Disp, count: int) -> list[dict] | None:
        rows = self.deliveries(d)
        if len(rows) != count or any(r["state"] == "pending" for r in rows):
            return None
        busy = self.sql.execute(
            "SELECT count(*) FROM outbox_messages m JOIN deliveries d ON d.id = m.delivery_id "
            "WHERE d.dispatch_id = %s AND m.state IN ('pending', 'leased')", (d.row,)).fetchone()[0]
        return rows if busy == 0 else None

    def wait_settled(self, d: Disp, count: int) -> list[dict]:
        return self.wait(lambda: self.settled(d, count))

    def advance_until(self, predicate: Callable[[], Any], *, step: float, tries: int = 10) -> Any:
        for _ in range(tries):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                time.sleep(0.05)
            self.clock.advance(step)
        value = predicate()
        assert value, f"condição não satisfeita depois de {tries} avanços de {step} s"
        return value

    # -- consultas -----------------------------------------------------------------------------

    def _rows(self, query: str, params: tuple) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(query, params).fetchall()

    def dispatch(self, d: Disp) -> dict:
        return self._rows("SELECT * FROM dispatches WHERE id = %s", (d.row,))[0]

    def deliveries(self, d: Disp) -> list[dict]:
        return self._rows("SELECT * FROM deliveries WHERE dispatch_id = %s ORDER BY id", (d.row,))

    def delivery_of(self, d: Disp, student: Student) -> dict:
        [row] = [r for r in self.deliveries(d) if r["enrollment_id"] == student.enrollment_row]
        return row

    def contents(self, delivery_row: int) -> dict | None:
        rows = self._rows("SELECT * FROM delivery_contents WHERE delivery_id = %s", (delivery_row,))
        return rows[0] if rows else None

    def messages(self, delivery_row: int) -> list[dict]:
        return self._rows("SELECT * FROM outbox_messages WHERE delivery_id = %s ORDER BY id", (delivery_row,))

    def message(self, message_row: int) -> dict:
        return self._rows("SELECT * FROM outbox_messages WHERE id = %s", (message_row,))[0]

    def events(self, *types: str, match: Callable[[dict], bool]) -> list[tuple]:
        return [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"])
                for e in all_events(self.client, ADMIN_TOKEN, *types) if match(e["data"])]

    def sent_to(self, chat: int) -> list[Any]:
        return self.rich.messages(self.bot, chat)

    def calls_to(self, chat: int, methods: tuple[str, ...] = SEND_METHODS + EDIT_METHODS) -> list[Any]:
        return [c for c in self.rich.calls() if c.method in methods and _chat_of(c) == chat]


@pytest.fixture
def flow(client: Any, sql: psycopg.Connection, db_url: str, clock: Any, settings: Any, rich_bot_api: Any,
         engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any, stop_one: Any) -> Iterator[Flow]:
    made = Flow(client=client, sql=sql, db_url=db_url, clock=clock, settings=settings, rich=rich_bot_api,
                engine_fake=engine_fake, repo_fetcher=repo_fetcher, start=bot_worker_thread, stop_one=stop_one)
    try:
        yield made
    finally:
        bot_worker_thread.stop()
        made.close()


def _delivered(flow: Flow, qid: str, question: FakeQuestion, *, count: int = 1,
               delivery: DeliverySettings = FAST) -> tuple[Disp, list[Student]]:
    """Um disparo imediato já entregue a `count` alunos; o worker fica parado no fim."""
    flow.program(qid, question)
    students = [flow.student() for _ in range(count)]
    d = flow.prepare(qid)
    flow.validated(d, delivery=delivery)
    flow.confirm(d)
    flow.wait_settled(d, count)
    flow.stop_all()
    return d, students


def _question_retried(flow: Flow, d: Disp, student: Student) -> dict | None:
    """A `question` do aluno de volta a `pending` depois da 1ª tentativa sem desfecho (500)."""
    rows = [r for r in flow.deliveries(d) if r["enrollment_id"] == student.enrollment_row]
    if not rows:
        return None
    msgs = flow.messages(rows[0]["id"])
    question = next((m for m in msgs if m["delivery_role"] == "question"), None)
    ok = question is not None and question["attempts"] == 1 and question["state"] == "pending"
    return question if ok else None


def _edit_rows(flow: Flow, delivery_row: int) -> list[tuple]:
    return [(m["id"], m["delivery_role"], m["operation"], m["render_version"], m["state"], m["attempts"])
            for m in flow.messages(delivery_row) if m["operation"] in ("edit_text", "edit_markup")]


# ---------------------------------------------------------------------------------------------
# Fusão, edição em voo e as mensagens fora das entregas sem atraso
# ---------------------------------------------------------------------------------------------


def test_pending_edits_merge_last_wins_and_earlier_messages_have_no_delay_with_stopped_clock(
        flow: Flow, outbox: Any, bots: Any) -> None:
    """As edições `pending` de uma mesma (entrega, papel)
    se fundem numa linha só, sem texto: a última vence, `render_version = greatest(...)` e a operação
    vira `edit_text` se alguma era de texto (e não volta a `edit_markup`). Na mesma função: com
    o `FakeClock` parado, as mensagens da API base saem uma atrás da outra, só com `message_id`, `recipient` e `text`,
    e as de oferecimentos e bots por bot (`sendMessage {chat_id, text}`) saem com o `SendPacer` sem consumir tentativa nem
    mudar `available_at`, no máximo com a espera real do ritmo."""
    d, [student] = _delivered(flow, "single_choice", preset("single_choice"))
    other = flow.student(enrollment=None)
    [delivery] = flow.deliveries(d)

    e1 = flow.edit(delivery, student, markup_only=True, version=2)
    assert _edit_rows(flow, delivery["id"]) == [(e1, "question", "edit_markup", 2, "pending", 0)]
    assert flow.edit(delivery, student, markup_only=False, version=1) == e1
    assert _edit_rows(flow, delivery["id"]) == [(e1, "question", "edit_text", 2, "pending", 0)]
    assert flow.edit(delivery, student, markup_only=True, version=4) == e1
    assert _edit_rows(flow, delivery["id"]) == [(e1, "question", "edit_text", 4, "pending", 0)]
    row = flow.message(e1)
    assert (row["text"], row["recipient_person_id"], row["bot_id"], row["delivery_endpoint_id"], row["priority"],
            row["media_id"], row["field_index"]) == (None, student.person_row, flow.bot_row, student.endpoint_row,
                                                     0, None, None)
    flow.sql.execute("UPDATE outbox_messages SET state = 'cancelled' WHERE id = %s", (e1,))

    t = flow.clock.now()
    with psycopg.connect(flow.db_url) as conn:
        r2 = [outbox.enqueue(conn, recipient=student.uuid, text=text) for text in ("Aviso um", "Aviso dois")]
    scripted = ScriptedChannel([Sent("r2-um"), Sent("r2-dois")])
    paced = Outbox(flow.db_url, clock=flow.clock, settings=flow.settings, pacer=SendPacer())
    channel = TelegramChannel(flow.db_url, clock=flow.clock, runtime=bots, default=ScriptedChannel([]),
                              delivery=DeliverySettings())
    try:
        assert [paced.dispatch_once(scripted, "worker-r2") for _ in r2] == r2
        assert scripted.calls == [OutgoingMessage(message_id=r2[0], recipient=student.uuid, text="Aviso um"),
                                  OutgoingMessage(message_id=r2[1], recipient=student.uuid, text="Aviso dois")]

        with psycopg.connect(flow.db_url) as conn:
            r3 = [outbox.enqueue(conn, recipient=who.uuid, bot_id=flow.bot_row, text=text)
                  for who, text in ((student, "Aviso por bot um"), (student, "Aviso por bot dois"),
                                    (other, "Aviso por bot três"))]
        sent: list[int] = []
        deadline = time.monotonic() + 3.0
        while len(sent) < 3 and time.monotonic() < deadline:
            taken = paced.dispatch_once(channel, "worker-r3", include_routed=True)
            if taken is None:
                time.sleep(0.02)
            else:
                sent.append(taken)
        assert sorted(sent) == r3
        assert flow.clock.now() == t
        for message_id in r2 + r3:
            record = outbox.get(message_id)
            assert (record.state, record.attempts, record.available_at, record.sent_at) == ("sent", 1, t, t)
        calls = [c for c in flow.rich.calls("sendMessage")]
        assert sorted((c.params["chat_id"], c.params["text"]) for c in calls) == sorted(
            [(student.chat, "Aviso por bot um"), (student.chat, "Aviso por bot dois"),
             (other.chat, "Aviso por bot três")])
        assert all(set(c.params) == {"chat_id", "text"} for c in calls)
    finally:
        paced.close()
        channel.close()


def test_edit_in_flight_is_never_altered_and_blocks_the_next_one(flow: Flow) -> None:
    """Uma edição em voo (`leased` por outro worker, montada por SQL) não é
    alterada por uma edição nova da mesma mensagem: a nova vira outra linha `pending`, que só é tomada
    depois do desfecho da em voo (ordem por entrega); vencida a posse, as duas saem, em ordem."""
    d, [student] = _delivered(flow, "single_choice", preset("single_choice"))
    [delivery] = flow.deliveries(d)
    [question] = flow.sent_to(student.chat)
    t = flow.clock.now()
    e1 = flow.edit(delivery, student, markup_only=True, version=2)
    flow.lease_by_other(e1, until=t + timedelta(seconds=60), attempts=1)
    e2 = flow.edit(delivery, student, markup_only=False, version=3)
    assert e2 != e1
    in_flight = flow.message(e1)
    assert (in_flight["state"], in_flight["operation"], in_flight["render_version"], in_flight["lease_owner"],
            in_flight["attempts"]) == ("leased", "edit_markup", 2, "worker-caido", 1)
    assert _edit_rows(flow, delivery["id"]) == [(e1, "question", "edit_markup", 2, "leased", 1),
                                                (e2, "question", "edit_text", 3, "pending", 0)]

    flow.start()
    time.sleep(1.5)
    assert _edit_rows(flow, delivery["id"]) == [(e1, "question", "edit_markup", 2, "leased", 1),
                                                (e2, "question", "edit_text", 3, "pending", 0)]
    assert flow.calls_to(student.chat, EDIT_METHODS) == []

    flow.clock.advance(61)
    flow.wait(lambda: [r[4] for r in _edit_rows(flow, delivery["id"])] == ["sent", "sent"])
    assert [(r[0], r[5]) for r in _edit_rows(flow, delivery["id"])] == [(e1, 2), (e2, 1)]
    edits = flow.calls_to(student.chat, EDIT_METHODS)
    assert [(c.method, c.params["message_id"]) for c in edits] == [
        ("editMessageReplyMarkup", question.message_id), ("editMessageText", question.message_id)]


# ---------------------------------------------------------------------------------------------
# Ordem por entrega, prioridade e posse
# ---------------------------------------------------------------------------------------------


def test_messages_of_a_delivery_leave_in_order_each_after_the_previous_outcome(flow: Flow) -> None:
    """Enquanto a mídia de uma entrega não tem desfecho (500 na 1ª tentativa,
    de volta a `pending` com o `FakeClock` parado), a questão e o pedido de campo dela não são tomados
    (nenhuma tentativa), mesmo disponíveis; a outra entrega segue sem esperar; depois, as três saem na
    ordem da fila."""
    flow.program("with_media", preset("with_media"))
    waiting, free = flow.student(), flow.student()
    d = flow.prepare("with_media")
    flow.validated(d)
    flow.rich.fail("sendPhoto", status=500, times=1, when=lambda call: _chat_of(call) == waiting.chat)
    flow.confirm(d)

    def blocked() -> list[dict] | None:
        rows = {r["enrollment_id"]: r for r in flow.deliveries(d)}
        if len(rows) != 2 or rows[free.enrollment_row]["state"] != "delivered":
            return None
        if any(m["state"] != "sent" for m in flow.messages(rows[free.enrollment_row]["id"])):
            return None
        msgs = flow.messages(rows[waiting.enrollment_row]["id"])
        return msgs if len(msgs) == 3 and msgs[0]["attempts"] == 1 and msgs[0]["state"] == "pending" else None

    flow.wait(blocked)
    time.sleep(1.0)
    msgs = flow.messages(flow.delivery_of(d, waiting)["id"])
    assert [(m["delivery_role"], m["state"], m["attempts"]) for m in msgs] == [
        ("media", "pending", 1), ("question", "pending", 0), ("field_prompt", "pending", 0)]
    assert [c.method for c in flow.calls_to(waiting.chat)] == ["sendPhoto"]

    flow.clock.advance(3)
    flow.wait_settled(d, 2)
    msgs = flow.messages(flow.delivery_of(d, waiting)["id"])
    assert [(m["delivery_role"], m["state"], m["attempts"]) for m in msgs] == [
        ("media", "sent", 2), ("question", "sent", 1), ("field_prompt", "sent", 1)]
    accepted = [c for c in flow.calls_to(waiting.chat) if c.status == 200]
    assert [c.method for c in accepted][:1] == ["sendPhoto"] and len(accepted) == 3
    assert [str(m.message_id) for m in flow.sent_to(waiting.chat)] == [m["channel_ref"] for m in msgs]
    free_question = flow.sent_to(free.chat)[1]
    assert free_question.call_seq < accepted[1].seq


def test_fixed_replies_wait_behind_delivery_messages_and_equal_priority_follows_id(flow: Flow, outbox: Any) -> None:
    """As respostas fixas com `priority = 1`, enfileiradas **antes** (id
    menor), só saem depois das mensagens de entrega (`priority = 0`) disponíveis ao mesmo tempo; com
    prioridade igual, a posse segue o menor `id` (as questões das duas entregas, e as duas respostas
    fixas entre si)."""
    flow.program("number", preset("number"))
    first, second = flow.student(), flow.student()
    d = flow.prepare("number")
    flow.validated(d)
    flow.stop_all()
    t = flow.clock.now()
    with psycopg.connect(flow.db_url) as conn:
        replies = [outbox.enqueue(conn, recipient=first.uuid, bot_id=flow.bot_row, text=text, priority=1)
                   for text in (deliverytexts.FREE_MESSAGE_REPLY, deliverytexts.USE_REPLY)]
    flow.sql.execute("UPDATE outbox_messages SET available_at = %s WHERE id = ANY(%s)",
                     (t + timedelta(seconds=10), replies))
    flow.rich.fail("sendRichMessage", status=500, times=2)
    flow.confirm(d)
    flow.start()
    flow.wait(lambda: _question_retried(flow, d, first) and _question_retried(flow, d, second))

    flow.clock.advance(10)
    flow.wait(lambda: flow.settled(d, 2) and all(flow.message(r)["state"] == "sent" for r in replies))
    assert [flow.message(r)["priority"] for r in replies] == [1, 1]
    rows = {r["enrollment_id"]: r for r in flow.deliveries(d)}
    first_msgs, second_msgs = (flow.messages(rows[s.enrollment_row]["id"]) for s in (first, second))
    assert {m["priority"] for m in first_msgs + second_msgs} == {0}
    assert max(replies) < min(m["id"] for m in first_msgs + second_msgs)

    # O `message_id` da Bot API falsa é por chat: a chave leva o chat.
    by_ref = {(s.chat, str(m.message_id)): m for s in (first, second) for m in flow.sent_to(s.chat)}
    seq_of = {m["id"]: by_ref[(s.chat, m["channel_ref"])].call_seq
              for s, msgs in ((first, first_msgs), (second, second_msgs)) for m in msgs}
    reply_calls = [c for c in flow.rich.calls("sendMessage") if c.status == 200 and _chat_of(c) == first.chat]
    assert [c.params["text"] for c in reply_calls] == [deliverytexts.FREE_MESSAGE_REPLY, deliverytexts.USE_REPLY]
    # Prioridade igual: as duas questões pelo menor id, e as duas respostas fixas também. Prioridade 0
    # antes da 1: as mensagens de entrega do mesmo chat e a questão da outra entrega, disponíveis ao
    # mesmo tempo, saem antes da primeira resposta fixa, embora ela tenha o menor id.
    lower, higher = sorted((first_msgs[0]["id"], second_msgs[0]["id"]))
    assert seq_of[lower] < seq_of[higher] < reply_calls[0].seq
    first_question, first_prompt = (seq_of[m["id"]] for m in first_msgs)
    assert first_question < first_prompt < reply_calls[0].seq < reply_calls[1].seq


def test_question_outcome_is_recorded_in_the_same_transaction_as_the_send(flow: Flow) -> None:
    """O `Sent` da `question` volta ao domínio (`DeliveryOutcome`) na mesma
    transação que registra o envio: no commit em que a linha vira `sent`, a entrega já está
    `delivered`, com a principal e o `sent_at` (gatilho de restrição adiado no banco do teste), e o
    `delivery.delivered` sai no mesmo horário."""
    flow.program("single_choice", preset("single_choice"))
    students = [flow.student(), flow.student()]
    flow.sql.execute("CREATE TABLE cq_test_sent_audit (message_id bigint, delivery_state text, principal bigint, "
                     "delivery_sent_at timestamptz, message_sent_at timestamptz)")
    flow.sql.execute(
        "CREATE FUNCTION cq_test_sent_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO cq_test_sent_audit SELECT NEW.id, d.state, d.principal_message_id, d.sent_at, NEW.sent_at "
        "FROM deliveries d WHERE d.id = NEW.delivery_id; RETURN NULL; END $$")
    flow.sql.execute(
        "CREATE CONSTRAINT TRIGGER cq_test_sent_audit AFTER UPDATE ON outbox_messages "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.delivery_role = 'question' AND NEW.state = 'sent' "
        "AND OLD.state IS DISTINCT FROM 'sent') EXECUTE FUNCTION cq_test_sent_audit()")
    d = flow.prepare("single_choice")
    flow.validated(d)
    t = flow.clock.now()
    flow.confirm(d)
    rows = flow.wait_settled(d, 2)

    audit = sorted(flow.sql.execute("SELECT * FROM cq_test_sent_audit").fetchall())
    questions = sorted(m["id"] for r in rows for m in flow.messages(r["id"]))
    assert audit == [(q, "delivered", q, t, t) for q in questions]
    events = flow.events("delivery.delivered", match=lambda data: data["dispatch_id"] == d.id)
    assert sorted(e[3]["person"] for e in events) == sorted(s.pseudo_id for s in students)
    assert {e[1] for e in events} == {iso(t)}


# ---------------------------------------------------------------------------------------------
# Cancelamento, posse esgotada e envio tardio
# ---------------------------------------------------------------------------------------------


def test_close_cancels_the_pending_messages_of_a_pending_delivery(flow: Flow) -> None:
    """No fechamento, a entrega ainda `pending` com a
    `question` na fila (em voo noutro worker, montada por SQL) vira `not_delivered/channel_unavailable`,
    e as mensagens `pending` dela (o pedido de campo) vão a `cancelled`, sem tentativa; a linha em voo
    não é tocada; a outra entrega, entregue, vira `no_answer`."""
    flow.program("number", preset("number"))
    stuck, fine = flow.student(), flow.student()
    closes_at = flow.clock.now() + timedelta(minutes=30)
    d = flow.prepare("number", closes_at=closes_at)
    flow.validated(d)
    flow.rich.fail("sendRichMessage", status=500, times=1, when=lambda call: _chat_of(call) == stuck.chat)
    flow.confirm(d)
    question = flow.wait(lambda: _question_retried(flow, d, stuck))
    flow.wait(lambda: flow.delivery_of(d, fine)["state"] == "delivered"
              and all(m["state"] == "sent" for m in flow.messages(flow.delivery_of(d, fine)["id"])))
    flow.stop_all()
    flow.lease_by_other(question["id"], until=closes_at + timedelta(hours=1))

    flow.clock.set(closes_at + timedelta(seconds=1))
    flow.start()
    closed = flow.advance_until(lambda: flow.dispatch(d)["state"] == "closed" and flow.dispatch(d), step=6)
    pending = flow.delivery_of(d, stuck)
    assert (pending["state"], pending["undelivered_reason"], pending["current_field"], pending["sent_at"]) == (
        "not_delivered", "channel_unavailable", None, None)
    rows = [(m["delivery_role"], m["state"], m["attempts"], m["lease_owner"]) for m in flow.messages(pending["id"])]
    assert rows == [("question", "leased", 1, "worker-caido"), ("field_prompt", "cancelled", 0, None)]
    assert flow.delivery_of(d, fine)["state"] == "no_answer"
    assert flow.events("delivery.not_delivered", match=lambda data: data["dispatch_id"] == d.id) == [
        ("delivery.not_delivered", iso(closed["closed_at"]), None,
         {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
          "delivery_id": str(pending["delivery_uuid"]), "person": stuck.pseudo_id,
          "undelivered_reason": "channel_unavailable"})]


def test_exhausted_lease_on_a_question_makes_the_delivery_channel_unavailable(flow: Flow) -> None:
    """A `question` com a posse esgotada (montada por SQL:
    `leased`, `attempts = max_attempts`, posse vencida) vai a `failed` sem chamar o canal, e o
    `DeliveryOutcome` (`Exhausted`) leva a entrega a `not_delivered/channel_unavailable` na mesma
    transação, com `delivery.not_delivered` e o `outbox.failed` da fila."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    d = flow.prepare("single_choice")
    flow.validated(d)
    flow.rich.fail("sendRichMessage", status=500, times=1)
    flow.confirm(d)
    question = flow.wait(lambda: _question_retried(flow, d, student))
    flow.stop_all()
    t = flow.clock.now()
    flow.lease_by_other(question["id"], until=t - timedelta(seconds=1), attempts=flow.settings.max_attempts)

    flow.start()
    [delivery] = flow.wait_settled(d, 1)
    assert (delivery["state"], delivery["undelivered_reason"], delivery["sent_at"]) == (
        "not_delivered", "channel_unavailable", None)
    row = flow.message(question["id"])
    assert (row["state"], row["attempts"], row["last_error"]) == ("failed", flow.settings.max_attempts,
                                                                  LEASE_EXHAUSTED_ERROR)
    assert len(flow.calls_to(student.chat)) == 1
    assert flow.events("delivery.not_delivered", match=lambda data: data["dispatch_id"] == d.id) == [
        ("delivery.not_delivered", iso(t), None,
         {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
          "delivery_id": str(delivery["delivery_uuid"]), "person": student.pseudo_id,
          "undelivered_reason": "channel_unavailable"})]
    assert flow.events("outbox.failed", match=lambda data: data["message_id"] == question["id"]) == [
        ("outbox.failed", iso(t), None, {"message_id": question["id"], "recipient": student.pseudo_id,
                                         "attempts": flow.settings.max_attempts, "error": LEASE_EXHAUSTED_ERROR})]


def test_exhausted_lease_on_an_edit_sends_a_fallback_in_reply_to_the_original(flow: Flow) -> None:
    """A edição com a posse esgotada (montada por SQL) vai a `failed`, e o
    `DeliveryOutcome` (`Exhausted`) enfileira a mensagem nova (`fallback`, `send`) em resposta à
    original, com a renderização completa do estado; como ela leva os controles, vira a principal."""
    d, [student] = _delivered(flow, "single_choice", preset("single_choice"))
    [delivery] = flow.deliveries(d)
    [question] = flow.sent_to(student.chat)
    t = flow.clock.now()
    edit = flow.edit(delivery, student, markup_only=False, version=1)
    flow.lease_by_other(edit, until=t - timedelta(seconds=1), attempts=flow.settings.max_attempts)

    flow.start()

    def fallback_sent() -> dict | None:
        rows = [m for m in flow.messages(delivery["id"]) if m["delivery_role"] == "fallback"]
        return rows[0] if len(rows) == 1 and rows[0]["state"] == "sent" else None

    fallback = flow.wait(fallback_sent)
    assert (fallback["operation"], fallback["text"], fallback["id"] > edit) == ("send", None, True)
    row = flow.message(edit)
    assert (row["state"], row["last_error"]) == ("failed", LEASE_EXHAUSTED_ERROR)
    after = flow.deliveries(d)[0]
    assert (after["state"], after["principal_message_id"]) == ("delivered", fallback["id"])
    original, new = flow.sent_to(student.chat)
    assert original.message_id == question.message_id
    assert (new.method, new.reply_parameters, str(new.message_id)) == (
        "sendRichMessage", {"message_id": question.message_id, "allow_sending_without_reply": True},
        fallback["channel_ref"])
    assert new.keyboard == question.keyboard
    assert "Qual é a cor do céu num dia limpo?" in new.plain_text
    assert flow.calls_to(student.chat, EDIT_METHODS) == []


def test_late_sent_question_on_a_not_delivered_delivery_edits_it_without_keyboard(flow: Flow) -> None:
    """A `question` em voo (montada por SQL) cuja entrega já virou
    `not_delivered` é aceita pelo Telegram: nenhuma transição (sem `delivered`, sem principal, sem
    `sent_at`, sem `delivery.delivered`); a linha fica `sent` com o `channel_ref`; o `current_render`
    passa a "questão + Janela fechada" sem teclado (`render_version += 1`), e sai uma `editMessageText`
    cujo alvo é o `message_id` desta `question`, sem `reply_markup`."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    d = flow.prepare("single_choice")
    flow.validated(d)
    flow.rich.fail("sendRichMessage", status=500, times=1)
    flow.confirm(d)
    question = flow.wait(lambda: _question_retried(flow, d, student))
    flow.stop_all()
    [delivery] = flow.deliveries(d)
    t = flow.clock.now()
    flow.lease_by_other(question["id"], until=t + timedelta(seconds=60))
    flow.sql.execute("UPDATE deliveries SET state = 'not_delivered', undelivered_reason = 'channel_unavailable', "
                     "current_field = NULL WHERE id = %s", (delivery["id"],))

    flow.start()
    flow.clock.advance(61)
    flow.wait(lambda: flow.message(question["id"])["state"] == "sent"
              and [r[4] for r in _edit_rows(flow, delivery["id"])] == ["sent"])
    after = flow.deliveries(d)[0]
    assert (after["state"], after["undelivered_reason"], after["principal_message_id"], after["sent_at"],
            after["render_version"]) == ("not_delivered", "channel_unavailable", None, None, 2)
    [msg] = flow.sent_to(student.chat)
    assert flow.message(question["id"])["channel_ref"] == str(msg.message_id)
    assert [r[1:4] for r in _edit_rows(flow, delivery["id"])] == [("question", "edit_text", 2)]
    [edit_call] = flow.calls_to(student.chat, EDIT_METHODS)
    assert (edit_call.method, edit_call.params["message_id"], "reply_markup" in edit_call.params) == (
        "editMessageText", msg.message_id, False)
    assert msg.keyboard is None
    assert deliverytexts.CLOSED_NOTICE in msg.plain_text and "Qual é a cor do céu num dia limpo?" in msg.plain_text
    assert flow.events("delivery.delivered", match=lambda data: data["dispatch_id"] == d.id) == []


def test_definitive_400_on_the_question_cancels_the_queued_field_prompt_in_the_same_transaction(flow: Flow) -> None:
    """Um 400 definitivo na `question` com o `field_prompt` na fila leva a entrega
    a `not_delivered/channel_refused` e cancela o pedido na mesma transação (gatilho adiado: no commit
    da transição, nenhuma mensagem `pending` da entrega); o pedido nunca chega ao canal."""
    flow.program("number", preset("number"))
    student = flow.student()
    flow.sql.execute("CREATE TABLE cq_test_undelivered_audit (delivery_id bigint, pending bigint, "
                     "cancelled bigint)")
    flow.sql.execute(
        "CREATE FUNCTION cq_test_undelivered_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
        "INSERT INTO cq_test_undelivered_audit SELECT NEW.id, count(*) FILTER (WHERE state = 'pending'), "
        "count(*) FILTER (WHERE state = 'cancelled') FROM outbox_messages WHERE delivery_id = NEW.id; "
        "RETURN NULL; END $$")
    flow.sql.execute(
        "CREATE CONSTRAINT TRIGGER cq_test_undelivered_audit AFTER UPDATE ON deliveries DEFERRABLE INITIALLY "
        "DEFERRED FOR EACH ROW WHEN (NEW.state = 'not_delivered' AND OLD.state IS DISTINCT FROM 'not_delivered') "
        "EXECUTE FUNCTION cq_test_undelivered_audit()")
    d = flow.prepare("number")
    flow.validated(d)
    flow.rich.fail("sendRichMessage", status=400, times=1)
    t = flow.clock.now()
    flow.confirm(d)
    [delivery] = flow.wait_settled(d, 1)

    assert (delivery["state"], delivery["undelivered_reason"]) == ("not_delivered", "channel_refused")
    assert [(m["delivery_role"], m["state"], m["attempts"]) for m in flow.messages(delivery["id"])] == [
        ("question", "failed", 1), ("field_prompt", "cancelled", 0)]
    assert flow.sql.execute("SELECT * FROM cq_test_undelivered_audit").fetchall() == [(delivery["id"], 0, 1)]
    assert len(flow.calls_to(student.chat)) == 1
    assert flow.events("delivery.not_delivered", match=lambda data: data["dispatch_id"] == d.id) == [
        ("delivery.not_delivered", iso(t), None,
         {"offering_id": flow.offering["offering_id"], "dispatch_id": d.id,
          "delivery_id": str(delivery["delivery_uuid"]), "person": student.pseudo_id,
          "undelivered_reason": "channel_refused"})]


# ---------------------------------------------------------------------------------------------
# Bot sem acesso, dialeto gravado e edição em voo que volta a `pending`
# ---------------------------------------------------------------------------------------------


def test_bot_without_access_keeps_delivery_messages_untaken_while_bot_messages_follow_the_bot_rules(flow: Flow,
                                                                                    outbox: Any) -> None:
    """Com o bot `no_access`, as mensagens com `delivery_id` do
    bot não são tomadas pela posse nem consomem tentativa (a entrega fica `pending`), enquanto uma
    mensagem sem `delivery_id` do mesmo bot segue a regra comum (`bot_no_access` até esgotar as tentativas)."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    d = flow.prepare("single_choice")
    flow.validated(d)
    flow.stop_all()
    flow.confirm(d)
    flow.sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected', lease_owner = NULL, "
                     "lease_until = NULL WHERE id = %s", (flow.bot_row,))
    with psycopg.connect(flow.db_url) as conn:
        r3 = outbox.enqueue(conn, recipient=student.uuid, bot_id=flow.bot_row, text="Aviso antigo ao aluno")

    flow.start()
    flow.wait(lambda: flow.deliveries(d) and flow.messages(flow.deliveries(d)[0]["id"]))
    record = flow.advance_until(lambda: outbox.get(r3).state == "failed" and outbox.get(r3), step=5)
    assert (record.attempts, record.last_error) == (flow.settings.max_attempts, "bot_no_access")
    [delivery] = flow.deliveries(d)
    assert (delivery["state"], delivery["variant_at"] is not None) == ("pending", True)
    assert [(m["delivery_role"], m["state"], m["attempts"]) for m in flow.messages(delivery["id"])] == [
        ("question", "pending", 0)]
    assert flow.calls_to(student.chat) == []


def test_open_delivery_keeps_its_recorded_dialect_when_the_channel_changes(flow: Flow) -> None:
    """A entrega aberta em `rich` guarda o dialeto; com o worker trocado
    para `html`, uma edição dela sai no Rich (`rich_message` com `skip_entity_detection`, sem
    `parse_mode`), enquanto a entrega aberta depois da troca sai no HTML simples."""
    flow.program("single_choice", preset("single_choice"))
    student = flow.student()
    old = flow.prepare("single_choice")
    new = flow.prepare("single_choice")
    flow.validated(old)
    flow.wait_state(new, "scheduled")
    flow.confirm(old)
    [old_delivery] = flow.wait_settled(old, 1)
    flow.stop_all()

    flow.start(delivery=dataclasses.replace(FAST, telegram_format="html"))
    flow.confirm(new)
    [new_delivery] = flow.wait_settled(new, 1)
    edit = flow.edit(old_delivery, student, markup_only=False, version=old_delivery["render_version"])
    flow.wait(lambda: flow.message(edit)["state"] == "sent")

    assert flow.contents(old_delivery["id"])["rendered"]["format"] == "rich"
    assert flow.contents(new_delivery["id"])["rendered"]["format"] == "html"
    old_msg, new_msg = flow.sent_to(student.chat)
    assert (new_msg.method, new_msg.parse_mode) == ("sendMessage", "HTML")
    [edit_call] = flow.calls_to(student.chat, EDIT_METHODS)
    assert (edit_call.method, edit_call.params["message_id"]) == ("editMessageText", old_msg.message_id)
    assert edit_call.params["rich_message"]["skip_entity_detection"] is True
    assert "parse_mode" not in edit_call.params and "text" not in edit_call.params


class _EditFailsOnce:
    """Porta do Telegram que levanta uma exceção fora da porta na 1ª `edit_message_text` (a exceção do
    canal); o resto delega ao adaptador real."""

    armed = True

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def edit_message_text(self, *args: Any, **kwargs: Any) -> Any:
        if _EditFailsOnce.armed:
            _EditFailsOnce.armed = False
            raise RuntimeError("falha simulada do canal na edição")
        return self._inner.edit_message_text(*args, **kwargs)


@pytest.mark.parametrize("failure", ["retry_after", "exception"])
def test_retried_edit_in_flight_absorbs_the_pending_edit(flow: Flow, bot_settings: Any, failure: str) -> None:
    """A edição E1 em voo (`leased` noutro worker,
    montada por SQL) e a edição E2 `pending` da mesma (entrega, papel), criada por uma alternância do
    aluno; tomada de novo, E1 recebe um 429 com `retry_after` (ou o canal levanta uma exceção): nenhum
    `IntegrityError` nem `worker.iteration_failed`; E2 vai a `cancelled`, E1 volta a `pending` com a
    maior `render_version` e `edit_text`; depois, a renderização mais nova (✅ na alternativa) é
    aplicada uma só vez, sem `fallback`."""
    d, [student] = _delivered(flow, "multiple_choice", preset("multiple_choice"))
    [delivery] = flow.deliveries(d)
    [question] = flow.sent_to(student.chat)
    t = flow.clock.now()
    e1 = flow.edit(delivery, student, markup_only=False, version=1)
    flow.lease_by_other(e1, until=t + timedelta(seconds=60), attempts=1)
    kwargs: dict[str, Any] = {}
    if failure == "retry_after":
        flow.rich.fail("editMessageText", status=429, retry_after=1, times=1)
    else:
        _EditFailsOnce.armed = True
        base = http_factory(bot_settings)
        kwargs["bots"] = BotRuntime(bot_settings, api_factory=lambda token: _EditFailsOnce(base(token)))

    with capture_logs() as logs:
        flow.start(**kwargs)
        flow.rich.tap(flow.bot, student.user, question.message_id, f"cq1:{delivery['button_token']}:m:0:1:a")

        def pending_e2() -> list[tuple] | None:
            rows = _edit_rows(flow, delivery["id"])
            ok = len(rows) == 2 and rows[1][4] == "pending" and flow.deliveries(d)[0]["render_version"] == 2
            return rows if ok else None

        (_, _, _, v1, _, _), (e2, _, _, v2, _, _) = flow.wait(pending_e2)
        assert (v1, v2) == (1, 2)
        flow.clock.advance(61)
        flow.wait(lambda: [(r[0], r[4]) for r in _edit_rows(flow, delivery["id"])] == [(e1, "pending"),
                                                                                      (e2, "cancelled")])
        absorbed, cancelled = _edit_rows(flow, delivery["id"])
        assert absorbed == (e1, "question", "edit_text", 2, "pending", 2)
        assert (cancelled[0], cancelled[1], cancelled[3:]) == (e2, "question", (2, "cancelled", 0))
        flow.clock.advance(10)
        flow.wait(lambda: flow.message(e1)["state"] == "sent")
        time.sleep(0.5)

    assert logs.with_event("worker.iteration_failed") == []
    assert "IntegrityError" not in logs.text and "UniqueViolation" not in logs.text
    accepted = flow.rich.edits(flow.bot, student.chat, question.message_id)
    assert len(accepted) == 1
    assert "✅ (a)" in [b.label for b in accepted[0].after.buttons()]
    edit_calls = [c for c in flow.calls_to(student.chat, EDIT_METHODS) if c.method == "editMessageText"]
    assert [c.status for c in edit_calls] == ([429, 200] if failure == "retry_after" else [200])
    assert {m["delivery_role"] for m in flow.messages(delivery["id"])} == {"question"}
    assert len(flow.sent_to(student.chat)) == 1
