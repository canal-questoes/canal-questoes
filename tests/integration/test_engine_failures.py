"""Falhas do motor e da obtenção na abertura e na correção: motor fora do ar ou incompatível,
variante ou correção `broken`, conteúdo do repositório apagado e pedido de correção recusado pelo
motor.

Contratos: os estados e motivos da entrega, a submissão (`internal_failure`,
`broken`, a marca de correção pendente), a chave `question.refetch:<ref>:r<k>` com uma só
tarefa viva por referência, a `delivery.prepare` (motor fora do ar repete; `not_found`
pede a nova obtenção e repete), os desfechos da correção (a nova obtenção que falha
deixa a submissão `grading`), o fechamento (as entregas `pending` viram
`not_delivered/engine_unavailable`), o arquivamento recusado com `grading_pending`, os
eventos e os textos; a porta do motor, a injeção de falhas nos testes e a
regra em que a falha interna reabre a entrega.

Tudo passa pelo caminho real, só com dublês: o oferecimento e o bot pela API, as pessoas pelo
cadastro de pessoas, e a matrícula de aluno ativa com o endereço vigente no bot gravadas por SQL (o vínculo
do aluno é outra história; o molde é o `_endpoint` de `test_outbox_routes.py`). O worker real
(`run_worker(…, bots=…, engine=…, fetcher=…, delivery=…)`) roda numa thread com o adaptador HTTP real
(`HttpQuestionEngine`) sobre o motor falso estrito (`engine_fake`), a obtenção falsa (`repo_fetcher`)
e a Bot API falsa com a extensão do disparo (`rich_bot_api`); o disparo é preparado e confirmado pela API.
O aluno responde pela Bot API falsa (texto em resposta à mensagem da questão). Estados, submissões,
tarefas e mensagens da fila são lidos por SQL; os eventos, pela API (`events.list`).

Horários pelo `FakeClock`: com o worker no ar, o relógio anda em passos de até 10 s, esperando a
renovação da posse do bot (o `_advance`); para chegar ao fechamento, o worker para, o relógio salta e
um worker novo sobe (o `_jump_past`). A espera crescente é medida no `available_at` das tarefas (o
relógio parado entre as tentativas). Ids internos, seeds, `button_token` e pseudônimos nunca como
literal: só relações e formas.

Sentinela do detalhe técnico: o motor falso manda `technical_detail` com
`TECHNICAL_DETAIL_SENTINEL` na variante e na correção `broken` (controle positivo em
`engine_fake.calls`); ele não pode aparecer em log, evento, tabela, resposta da API nem parâmetro
enviado ao Telegram.

No esqueleto, toda função falha no primeiro
`dispatches.prepare` (rota ausente, 404). Ficam verdes quando entram as partes implementadas
depois.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import deliverytexts
from cq_server.adapters.console import ConsoleChannel
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.bots import TelegramChannel
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.grading import grade_task_handler
from cq_server.ports.repos import FetchNotFound, FetchTemporaryFailure
from cq_server.tasks import ClaimedTask, RetryTaskLater
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    confirm_dispatch,
    create_offering,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import TelegramUser, make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error
from tests.helpers.db import scan_all_tables
from tests.helpers.engine import TECHNICAL_DETAIL, TECHNICAL_DETAIL_SENTINEL, VARIANT_BROKEN, seed_value
from tests.helpers.logs import capture_logs

COURSE = "Física Experimental"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/turma/questoes-falhas-motor.git"
COMMIT = "fa11" * 10
WORKER_ID = "worker-falhas-motor"
DEAD_WORKER = "worker-que-caiu"
TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
ENGINE_TIMEOUT = 10.0  # + 15 <= a posse de 30 s (check_engine_timeout)
DAY = timedelta(days=1)
SHORT = timedelta(minutes=10)
STEP = 10.0  # salto do relógio com o worker no ar (menor que a margem de renovação da posse)
RENEW_MARGIN = timedelta(seconds=13)  # poll 1 s + Telegram 2 s + renovação 10 s (bot_settings)
RETRY_GAP = 60.0  # settings.backoff_max_seconds: a varredura espera isto depois de uma rodada `failed`
CLOSE_RECHECK = 6.0  # um pouco mais que scheduler.CLOSE_RECHECK_SECONDS (5 s)
NUMBER_ANSWER = "6.5"  # a resposta certa do preset `number`

DELIVERY_EVENTS = ("delivery.delivered", "delivery.not_delivered", "delivery.question_failed",
                      "delivery.dont_know")
SUBMISSION_EVENTS = ("submission.graded", "submission.internal_failure")

_codes = itertools.count(1)
_people = itertools.count(1)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any, bot_settings: Any) -> None:
    """As esperas deste arquivo dependem do `settings` (posse 30 s, 3 tentativas, espera 2 s, teto
    60 s) e do `bot_settings` (consulta 1 s, Telegram 2 s, renovação 10 s) do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (30, 3, 2, 60)
    assert (bot_settings.poll_timeout_seconds, bot_settings.telegram_timeout_seconds,
            bot_settings.renew_seconds) == (1, 2, 10)


# ---------------------------------------------------------------------------------------------
# Apoio


def _until(fn: Callable[[], Any], *, what: str, timeout: float = TIMEOUT) -> Any:
    """Espera `fn()` ficar verdadeiro (tempo real) e devolve o valor."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"tempo esgotado esperando {what}")
        time.sleep(0.02)


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def _seconds(delta: timedelta) -> float:
    return delta.total_seconds()


@dataclasses.dataclass(frozen=True)
class _Offering:
    code: str
    offering_id: str
    row_id: int
    bot: int  # id do bot na Bot API falsa
    bot_row: int


@dataclasses.dataclass(frozen=True)
class _Student:
    user: TelegramUser
    pseudo_id: str
    person_row: int
    enrollment_row: int
    endpoint_row: int

    @property
    def chat(self) -> int:
        return self.user.user_id


class _World:
    """O arnês do arquivo: oferecimento, alunos vinculados, worker e disparos (ver o docstring)."""

    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, settings: Any, bots: Any,
                 rich: Any, engine_fake: Any, repo_fetcher: Any, start: Any) -> None:
        self.client = client
        self.sql = sql
        self.clock = clock
        self.settings = settings
        self.bots = bots
        self.rich = rich
        self.engine_fake = engine_fake
        self.fetcher = repo_fetcher
        self._start = start
        self._engines: list[HttpQuestionEngine] = []
        self.engine = self.new_engine()
        self.threads: list[Any] = []
        self._last: dict[str, Any] = {}

    # -- montagem --------------------------------------------------------------------------

    def new_engine(self) -> HttpQuestionEngine:
        """Um adaptador HTTP real novo (sem o `/v1/health` em cache) sobre o motor falso."""
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url,
                                                   timeout_seconds=ENGINE_TIMEOUT))
        self._engines.append(engine)
        return engine

    def offering(self) -> _Offering:
        """Oferecimento `active` com bot `registered` na Bot API falsa (API de oferecimentos)."""
        code = f"falhas-motor-{next(_codes)}"
        body = create_offering(self.client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
        bot = self.rich.add_bot()
        register_bot(self.client, ADMIN_TOKEN, code, self.rich.token_of(bot))
        activate_bot(self.client, ADMIN_TOKEN, code)
        row_id, bot_row = self.sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.code = %s",
            (code,)).fetchone()
        return _Offering(code=code, offering_id=body["offering_id"], row_id=row_id, bot=bot, bot_row=bot_row)

    def student(self, off: _Offering, index: int | None = None) -> _Student:
        """Aluno vinculado: pessoa pelo cadastro de pessoas; matrícula `student` `active` com o termo aceito e
        endereço `active` no bot, por SQL. O chat privado passa a existir na Bot API falsa (o aluno falou
        com o bot no vínculo) por um update de tipo fora do `allowed_updates`, que o dublê descarta na
        geração: nenhum update chega ao worker."""
        n = next(_people) if index is None else index
        user = make_user(n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Pessoa Aluna Falhas Motor {n:02d}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (body["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment_row = self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) "
            "RETURNING id", (person_row, off.row_id, now, now, now)).fetchone()[0]
        endpoint_row = self.endpoint(off, person_row, user)
        return _Student(user=user, pseudo_id=body["pseudo_id"], person_row=person_row,
                        enrollment_row=enrollment_row, endpoint_row=endpoint_row)

    def endpoint(self, off: _Offering, person_row: int, user: TelegramUser) -> int:
        now = self.clock.now()
        row = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
            "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, off.bot_row, user.user_id, now, now)).fetchone()[0]
        dropped = self.rich.push_update(off.bot, {"message_reaction": {
            "chat": user.as_private_chat(), "message_id": 1, "user": user.as_user(), "date": 0,
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}})
        assert dropped is None, "o update de apoio não pode chegar ao worker"
        return row

    # -- worker ----------------------------------------------------------------------------

    def start_worker(self, off: _Offering, *, engine: Any = None, worker_id: str = WORKER_ID) -> Any:
        kwargs = {"settings": dataclasses.replace(self.settings, worker_id=worker_id),
                  "engine": engine if engine is not None else self.engine, "fetcher": self.fetcher,
                  "delivery": DeliverySettings()}
        thread = self._start(**kwargs)
        self.threads.append(thread)
        self._last = {"off": off, "engine": kwargs["engine"], "worker_id": worker_id}
        self.rich.wait_for(lambda: self.rich.pending_polls(off.bot), timeout=TIMEOUT)
        return thread

    def stop_workers(self) -> None:
        for thread in self.threads:
            if thread.is_alive():
                assert self._start.stop_one(thread) < 10.0, "o worker não parou em 10 s"
        self.threads = [t for t in self.threads if t.is_alive()]

    def restart(self) -> Any:
        return self.start_worker(self._last["off"], engine=self._last["engine"],
                                 worker_id=self._last["worker_id"])

    def close(self) -> None:
        self._start.stop()
        for engine in self._engines:
            with contextlib.suppress(Exception):
                engine.close()

    # -- relógio ---------------------------------------------------------------------------

    def _wait_leases(self) -> None:
        """Depois de um salto, espera a renovação das posses vivas dos bots (até 3 s reais)."""
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            now = self.clock.now()
            rows = self.sql.execute("SELECT lease_until FROM bots WHERE lease_owner IS NOT NULL").fetchall()
            if all(until - now >= RENEW_MARGIN for (until,) in rows):
                return
            time.sleep(0.02)

    def advance(self, seconds: float) -> None:
        """Anda o `FakeClock` com o worker no ar, em passos de até `STEP`."""
        remaining = float(seconds)
        while remaining > 0:
            step = min(STEP, remaining)
            self.clock.advance(step)
            remaining -= step
            self._wait_leases()

    def jump_past(self, moment: datetime) -> None:
        """Para o worker, leva o relógio 1 s além de `moment` e sobe um worker novo."""
        self.stop_workers()
        self.clock.set(max(self.clock.now(), moment + timedelta(seconds=1)))
        self.restart()

    # -- disparo ---------------------------------------------------------------------------

    def validated(self, off: _Offering, qid: str, question: Any = None, *,
                  closes_in: timedelta = DAY) -> dict:
        """Disparo imediato preparado pela API e validado pelo worker (espera `scheduled`)."""
        self.engine_fake.program((REPO, COMMIT, qid), question if question is not None else qid)
        dispatch = prepare_dispatch(self.client, ADMIN_TOKEN, off.code, repo=REPO, commit=COMMIT, qid=qid,
                                    closes_at=iso(self.clock.now() + closes_in))
        self.wait_dispatch(dispatch, "scheduled")
        return dispatch

    def confirm(self, off: _Offering, dispatch: dict) -> dict:
        confirmed = confirm_dispatch(self.client, ADMIN_TOKEN, off.code, dispatch["dispatch_id"])
        self.wait_dispatch(dispatch, "open")
        return confirmed

    def dispatch_row(self, dispatch: dict) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM dispatches WHERE dispatch_uuid = %s",
                               (dispatch["dispatch_id"],)).fetchone()

    def wait_dispatch(self, dispatch: dict, state: str) -> dict:
        return _until(lambda: (row := self.dispatch_row(dispatch)) and row["state"] == state and row,
                      what=f"o disparo em {state}")

    def wait_closed(self, dispatch: dict) -> dict:
        """Espera o `dispatch.closed`, andando o relógio em `CLOSE_RECHECK` a cada espera sem
        fechamento (a barreira do fechamento termina a rodada sem efeito)."""
        deadline = time.monotonic() + 3 * TIMEOUT
        while True:
            try:
                return _until(lambda: (row := self.dispatch_row(dispatch)) and row["state"] == "closed" and row,
                              what="o fechamento", timeout=2.0)
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
                self.advance(CLOSE_RECHECK)

    def ref_row(self, qid: str) -> int:
        return self.sql.execute("SELECT id FROM question_refs WHERE commit_sha = %s AND qid = %s",
                                (COMMIT, qid)).fetchone()[0]

    def ref_uuid(self, qid: str) -> str:
        return self.sql.execute("SELECT ref_uuid::text FROM question_refs WHERE commit_sha = %s AND qid = %s",
                                (COMMIT, qid)).fetchone()[0]

    # -- entregas, submissões, tarefas e mensagens -----------------------------------------

    def deliveries(self, dispatch: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT d.*, d.delivery_uuid::text AS delivery_id FROM deliveries d "
                "JOIN dispatches p ON p.id = d.dispatch_id WHERE p.dispatch_uuid = %s ORDER BY d.id",
                (dispatch["dispatch_id"],)).fetchall()

    def delivery_of(self, dispatch: dict, student: _Student) -> dict:
        [row] = [d for d in self.deliveries(dispatch) if d["enrollment_id"] == student.enrollment_row]
        return row

    def wait_delivery(self, dispatch: dict, student: _Student, state: str) -> dict:
        def ready() -> dict | None:
            rows = [d for d in self.deliveries(dispatch) if d["enrollment_id"] == student.enrollment_row]
            if rows and rows[0]["state"] == state and (state != "delivered" or rows[0]["sent_at"]):
                return rows[0]
            return None
        return _until(ready, what=f"a entrega em {state}")

    def contents(self, delivery: dict) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT parts, selection FROM delivery_contents WHERE delivery_id = %s",
                               (delivery["id"],)).fetchone()

    def submissions(self, delivery: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM submissions WHERE delivery_id = %s ORDER BY id",
                               (delivery["id"],)).fetchall()

    def wait_submissions(self, delivery: dict, count: int, *, settled: bool = True) -> list[dict]:
        """Espera `count` submissões da entrega (a última fora de `grading`, com `settled`)."""
        def ready() -> list[dict] | None:
            rows = self.submissions(delivery)
            if len(rows) == count and (not settled or rows[-1]["outcome"] != "grading"):
                return rows
            return None
        return _until(ready, what=f"{count} submissões")

    def tasks(self, task_type: str) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM tasks WHERE task_type = %s ORDER BY id", (task_type,)).fetchall()

    def wait_task(self, task_type: str, check: Callable[[dict], bool], *, what: str) -> dict:
        def ready() -> dict | None:
            rows = self.tasks(task_type)
            return rows[-1] if rows and check(rows[-1]) else None
        return _until(ready, what=what)

    def live_tasks(self, task_type: str) -> list[dict]:
        return [t for t in self.tasks(task_type) if t["state"] in ("pending", "leased")]

    def delivery_messages(self, delivery: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, delivery_role, operation, state, attempts, channel_ref, field_index, text "
                "FROM outbox_messages WHERE delivery_id = %s ORDER BY id", (delivery["id"],)).fetchall()

    def principal(self, off: _Offering, student: _Student, delivery: dict) -> Any:
        """A mensagem com os controles da entrega, no estado corrente da Bot API falsa."""
        row = self.sql.execute(
            "SELECT o.channel_ref FROM deliveries d JOIN outbox_messages o ON o.id = d.principal_message_id "
            "WHERE d.id = %s", (delivery["id"],)).fetchone()
        assert row is not None and row[0] is not None, "a entrega não tem mensagem principal"
        return self.rich.current(off.bot, student.chat, int(row[0]))

    def answer(self, off: _Offering, student: _Student, delivery: dict, text: str = NUMBER_ANSWER) -> int:
        """O aluno responde, pelo "responder", à mensagem da questão."""
        question = self.principal(off, student, delivery)
        update_id = self.rich.push_text(off.bot, student.user, text, reply_to_message_id=question.message_id)
        assert update_id is not None
        return update_id

    def prompts(self, off: _Offering, student: _Student) -> list[Any]:
        """Os pedidos de campo (`ForceReply`) recebidos pelo aluno, na ordem."""
        return [m for m in self.rich.messages(off.bot, student.chat) if m.force_reply]

    def events(self, *types: str) -> list[tuple[str, dict]]:
        return [(e["type"], e["data"]) for e in all_events(self.client, ADMIN_TOKEN, *types)]


@pytest.fixture
def world(client: Any, sql: psycopg.Connection, clock: Any, settings: Any, bots: Any, rich_bot_api: Any,
          engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any) -> Iterator[_World]:
    w = _World(client=client, sql=sql, clock=clock, settings=settings, bots=bots, rich=rich_bot_api,
               engine_fake=engine_fake, repo_fetcher=repo_fetcher, start=bot_worker_thread)
    try:
        yield w
    finally:
        w.close()


def _appearances(rich: Any, bot: int, chat: int, text: str) -> int:
    """Quantas vezes `text` apareceu ao aluno: mensagens que nasceram com ele e edições que o
    acrescentaram (ausente antes, presente depois)."""
    edits = rich.edits(bot, chat)
    first_before: dict[int, Any] = {}
    for edit in edits:
        first_before.setdefault(edit.message_id, edit.before)
    count = 0
    for message in rich.messages(bot, chat):
        initial = first_before.get(message.message_id, message)
        if text in (initial.plain_text or "") or text in (initial.caption or ""):
            count += 1
    for edit in edits:
        if text in (edit.after.plain_text or "") and text not in (edit.before.plain_text or ""):
            count += 1
    return count


def _labels(message: Any) -> list[str]:
    return [b.label for b in message.buttons()]


def _delivery_event(off: _Offering, dispatch: dict, delivery: dict, student: _Student, **extra: Any) -> dict:
    return {"offering_id": off.offering_id, "dispatch_id": dispatch["dispatch_id"],
            "delivery_id": delivery["delivery_id"], "person": student.pseudo_id, **extra}


def _counts(**states: int) -> dict[str, int]:
    base = dict.fromkeys(("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                          "answered_late", "question_failed"), 0)
    base.update(states)
    return base


def _assert_no_technical_detail(world: _World, logs: Any, off: _Offering, dispatch: dict) -> None:
    """o `technical_detail` não sai em log, evento, tabela, API nem parâmetro ao Telegram."""
    assert TECHNICAL_DETAIL_SENTINEL not in logs.text
    events = world.sql.execute("SELECT type, data::text FROM events ORDER BY seq").fetchall()
    assert [t for t, data in events if TECHNICAL_DETAIL_SENTINEL in data] == []
    found = [(table, column) for table, column, text in scan_all_tables(world.settings.database_url)
             if TECHNICAL_DETAIL_SENTINEL in text]
    assert found == []
    assert [c.method for c in world.rich.calls() if TECHNICAL_DETAIL_SENTINEL in repr(c.params)] == []
    resp = world.client.get(f"/v1/offerings/{off.code}/dispatches/{dispatch['dispatch_id']}",
                            headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200
    assert TECHNICAL_DETAIL_SENTINEL not in resp.text


# ---------------------------------------------------------------------------------------------
# Motor fora do ar e incompatível na abertura


def test_engine_down_at_opening_keeps_delivery_pending_with_growing_wait_until_it_returns(world: _World) -> None:
    """Com o motor parado (conexão recusada: o motor
    fora do ar), a `delivery.prepare` falha de forma repetível e volta com espera crescente; a entrega
    continua `pending`, sem variante, mensagem nem evento; o motor volta e a variante sai, sem mudar o
    fechamento."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    variants_before = len(world.engine_fake.calls("create_variant"))
    world.engine_fake.stop()
    world.confirm(off, dispatch)

    first = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                            what="a 1ª falha da delivery.prepare")
    wait1 = first["available_at"] - world.clock.now()
    assert wait1 > timedelta(0)
    world.advance(_seconds(wait1))
    second = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 2,
                             what="a 2ª falha da delivery.prepare")
    assert second["id"] == first["id"]
    wait2 = second["available_at"] - world.clock.now()
    assert wait2 > wait1, "a espera entre as tentativas tem de crescer"

    delivery = world.delivery_of(dispatch, alice)
    assert (delivery["state"], delivery["variant_at"], delivery["current_field"], delivery["sent_at"],
            delivery["principal_message_id"]) == ("pending", None, None, None, None)
    assert world.delivery_messages(delivery) == []
    assert world.rich.messages(off.bot, alice.chat) == []
    assert world.events(*DELIVERY_EVENTS) == []
    assert len(world.engine_fake.calls("create_variant")) == variants_before

    world.engine_fake.start()
    world.advance(_seconds(wait2))
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    assert [c.outcome for c in world.engine_fake.calls("create_variant")][variants_before:] == ["ok"]
    assert world.events(*DELIVERY_EVENTS) == [
        ("delivery.delivered", _delivery_event(off, dispatch, delivery, alice))]
    assert world.dispatch_row(dispatch)["closes_at"] == _parse(dispatch["closes_at"])
    question = world.principal(off, alice, delivery)
    assert "Não sei" in _labels(question)


def test_deliveries_still_pending_at_close_become_not_delivered_engine_unavailable(world: _World) -> None:
    """O motor não volta; o fechamento acontece
    no horário gravado, e a entrega ainda `pending` (sem variante) vira
    `not_delivered/engine_unavailable`, com o evento e as contagens do `dispatch.closed`."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    world.engine_fake.stop()
    world.confirm(off, dispatch)
    world.wait_task("delivery.prepare", lambda t: t["attempts"] >= 1 and t["state"] != "leased",
                    what="a delivery.prepare tentada")
    assert world.delivery_of(dispatch, alice)["state"] == "pending"

    closes_at = _parse(dispatch["closes_at"])
    world.jump_past(closes_at)
    row = world.wait_closed(dispatch)
    assert row["closes_at"] == closes_at, "o fechamento não muda com o motor fora do ar"

    delivery = world.delivery_of(dispatch, alice)
    assert (delivery["state"], delivery["undelivered_reason"], delivery["failure"], delivery["current_field"],
            delivery["variant_at"], delivery["sent_at"]) == (
        "not_delivered", "engine_unavailable", None, None, None, None)
    assert world.rich.messages(off.bot, alice.chat) == []
    assert world.events(*DELIVERY_EVENTS, "dispatch.closed") == [
        ("delivery.not_delivered", _delivery_event(off, dispatch, delivery, alice,
                                                   undelivered_reason="engine_unavailable")),
        ("dispatch.closed", {"offering_id": off.offering_id, "dispatch_id": dispatch["dispatch_id"],
                             "trigger": "window", "counts": _counts(not_delivered=1)}),
    ]


def test_incompatible_engine_is_unavailable_and_retried_without_being_called(world: _World) -> None:
    """Um motor que declara outra versão do contrato no `/v1/health` é tratado como
    indisponível (repetível), sem chamar a operação; a entrega continua `pending` até ele voltar a
    ser compatível. Um adaptador novo (sem o `health` em cache) sobe com o segundo worker."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.stop_workers()
    variants_before = len(world.engine_fake.calls("create_variant"))
    health_before = len(world.engine_fake.calls("health"))
    world.engine_fake.set_health(contract_version="9.9")

    world.start_worker(off, engine=world.new_engine())
    world.confirm(off, dispatch)
    first = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                            what="a falha da delivery.prepare com o motor incompatível")
    assert len(world.engine_fake.calls("create_variant")) == variants_before, "o motor incompatível foi chamado"
    health = world.engine_fake.calls("health")[health_before:]
    assert health and health[-1].response["contract_version"] == "9.9"
    assert world.delivery_of(dispatch, alice)["state"] == "pending"

    world.engine_fake.set_health()
    world.advance(_seconds(first["available_at"] - world.clock.now()))
    world.wait_delivery(dispatch, alice, "delivered")
    assert [c.outcome for c in world.engine_fake.calls("create_variant")][variants_before:] == ["ok"]


# ---------------------------------------------------------------------------------------------
# Questão quebrada


def test_broken_variant_fails_only_that_delivery(world: _World) -> None:
    """A variante de um aluno sai `broken` (com `technical_detail`); só
    a entrega dele vira `question_failed/variant_broken`, com o evento e sem mensagem; a do outro
    aluno segue. O detalhe técnico não sai em lugar nenhum (controle positivo no motor falso)."""
    off = world.offering()
    alice = world.student(off)
    bruno = world.student(off)
    with capture_logs() as logs:
        world.start_worker(off)
        dispatch = world.validated(off, "number")
        broken = {"outcome": "broken", "warnings": [],
                  "reason": {"code": VARIANT_BROKEN.code, "message": VARIANT_BROKEN.message,
                             "phase": VARIANT_BROKEN.phase, "technical_detail": TECHNICAL_DETAIL}}
        world.engine_fake.inject("create_variant", body=broken, times=1)
        world.confirm(off, dispatch)

        def settled() -> list[dict] | None:
            rows = world.deliveries(dispatch)
            done = [r for r in rows if r["state"] == "question_failed" or (r["state"] == "delivered" and r["sent_at"])]
            return rows if len(done) == 2 else None

        _until(settled, what="as duas entregas decididas")
        world.stop_workers()

    [programmed] = [c for c in world.engine_fake.calls("create_variant") if c.programmed]
    assert TECHNICAL_DETAIL_SENTINEL in programmed.response["reason"]["technical_detail"]  # controle positivo
    by_student = {s.pseudo_id: world.delivery_of(dispatch, s) for s in (alice, bruno)}
    failed = [s for s in (alice, bruno) if seed_value(programmed.request["seed"]) == by_student[s.pseudo_id]["seed"]]
    assert len(failed) == 1
    victim = failed[0]
    other = bruno if victim is alice else alice
    lost, kept = by_student[victim.pseudo_id], by_student[other.pseudo_id]
    assert (lost["state"], lost["failure"], lost["undelivered_reason"], lost["current_field"], lost["sent_at"],
            lost["variant_at"]) == ("question_failed", "variant_broken", None, None, None, None)
    assert kept["state"] == "delivered"
    assert world.rich.messages(off.bot, victim.chat) == []
    assert world.delivery_messages(lost) == []
    assert "Não sei" in _labels(world.principal(off, other, kept))
    assert sorted(world.events(*DELIVERY_EVENTS), key=lambda e: e[0]) == [
        ("delivery.delivered", _delivery_event(off, dispatch, kept, other)),
        ("delivery.question_failed", _delivery_event(off, dispatch, lost, victim, failure="variant_broken")),
    ]
    _assert_no_technical_detail(world, logs, off, dispatch)


def test_broken_grading_records_failure_and_tells_the_student_without_technical_detail(world: _World) -> None:
    """A correção sai `broken`; a submissão fica `broken` com o código e
    a fase (sem o `technical_detail`), a entrega vira `question_failed/grading_broken`, o aluno vê
    `QUESTION_FAILED` numa edição sem controles, e sai `delivery.question_failed` (nenhum
    `submission.graded`)."""
    off = world.offering()
    alice = world.student(off)
    with capture_logs() as logs:
        world.start_worker(off)
        dispatch = world.validated(off, "broken_on_grade")
        world.confirm(off, dispatch)
        delivery = world.wait_delivery(dispatch, alice, "delivered")
        world.answer(off, alice, delivery)
        [submission] = world.wait_submissions(delivery, 1)
        delivery = world.wait_delivery(dispatch, alice, "question_failed")
        question = _until(lambda: (m := world.principal(off, alice, delivery))
                          and deliverytexts.QUESTION_FAILED in (m.plain_text or "") and m,
                          what="a edição com QUESTION_FAILED")
        world.stop_workers()

    grade = world.engine_fake.calls("grade")[-1]
    assert TECHNICAL_DETAIL_SENTINEL in grade.response["reason"]["technical_detail"]  # controle positivo
    assert (submission["outcome"], submission["valid"], submission["refusal"], submission["failure_code"],
            submission["failure_phase"], submission["score"], submission["raw_input"]) == (
        "broken", None, None, "question_code_error", "grade", None, {"resposta": NUMBER_ANSWER})
    assert (delivery["failure"], delivery["undelivered_reason"], delivery["current_field"]) == (
        "grading_broken", None, None)
    assert question.buttons() == []
    assert world.events(*DELIVERY_EVENTS, *SUBMISSION_EVENTS) == [
        ("delivery.delivered", _delivery_event(off, dispatch, delivery, alice)),
        ("delivery.question_failed", _delivery_event(off, dispatch, delivery, alice, failure="grading_broken")),
    ]
    _assert_no_technical_detail(world, logs, off, dispatch)


# ---------------------------------------------------------------------------------------------
# Motor fora do ar na correção


def test_engine_down_at_grading_keeps_submission_grading_notifies_once_and_retries_without_limit(
        world: _World) -> None:
    """Com o motor parado, a submissão fica `grading` (a entrega
    `delivered`, com a marca), o aluno recebe `GRADING_DELAYED` uma única vez, a correção repete com
    espera crescente até esgotar a tarefa (`task.failed`) e a varredura cria a rodada
    `submission.grade:<id>:r2` depois do intervalo; com o motor de volta, a correção sai."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    world.engine_fake.stop()
    world.answer(off, alice, delivery)
    [pending] = world.wait_submissions(delivery, 1, settled=False)

    first = world.wait_task("submission.grade", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                            what="a 1ª falha da correção")
    wait1 = first["available_at"] - world.clock.now()
    world.advance(_seconds(wait1))
    second = world.wait_task("submission.grade", lambda t: t["state"] == "pending" and t["attempts"] == 2,
                             what="a 2ª falha da correção")
    wait2 = second["available_at"] - world.clock.now()
    assert timedelta(0) < wait1 < wait2
    world.advance(_seconds(wait2))
    exhausted = world.wait_task("submission.grade", lambda t: t["state"] == "failed",
                                what="a correção esgotada")
    assert exhausted["id"] == first["id"]
    [submission] = world.submissions(delivery)
    assert (submission["outcome"], submission["delay_notice_sent"]) == ("grading", True)
    assert world.delivery_of(dispatch, alice)["state"] == "delivered"
    _until(lambda: _appearances(world.rich, off.bot, alice.chat, deliverytexts.GRADING_DELAYED) == 1,
           what="o aviso GRADING_DELAYED")
    failed = [data for _, data in world.events("task.failed") if data["task_type"] == "submission.grade"]
    assert [(d["task_id"], d["attempts"]) for d in failed] == [(first["id"], 3)]

    world.engine_fake.start()
    world.advance(RETRY_GAP)
    [graded] = world.wait_submissions(delivery, 1)
    tasks = world.tasks("submission.grade")
    assert [t["state"] for t in tasks] == ["failed", "done"]
    assert tasks[1]["idempotency_key"] == f"submission.grade:{pending['id']}:r2"
    assert (graded["outcome"], graded["valid"], graded["score"], graded["late"]) == ("graded", True, 1.0, False)
    delivery = world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.principal(off, alice, delivery).plain_text or ""),
           what="o feedback da correção")
    assert _appearances(world.rich, off.bot, alice.chat, deliverytexts.GRADING_DELAYED) == 1
    [(kind, data)] = world.events("submission.graded")
    assert {k: v for k, v in data.items() if k != "parts"} == {
        "offering_id": off.offering_id, "dispatch_id": dispatch["dispatch_id"],
        "delivery_id": delivery["delivery_id"], "submission_id": str(graded["submission_uuid"]),
        "question_ref_id": world.ref_uuid("number"), "person": alice.pseudo_id, "valid": True, "late": False,
        "normalized": False, "score": 1.0}
    assert [(p["index"], p["score"]) for p in data["parts"]] == [(0, 1.0)]


# ---------------------------------------------------------------------------------------------
# Conteúdo do repositório apagado: `not_found` e `question.refetch` (rodadas da nova obtenção)


def test_not_found_at_grading_refetches_once_and_the_grading_goes_out(world: _World) -> None:
    """A árvore some depois da validação; a correção recebe
    `not_found`, a submissão continua `grading`, sai uma `question.refetch:<ref>:r1`, a nova obtenção
    recria a árvore (contada no `repo_fetcher`) e a correção seguinte sai."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    fetches_before = world.fetcher.count(REPO, COMMIT)
    assert world.fetcher.delete_tree(REPO, COMMIT)
    world.answer(off, alice, delivery)

    grade_task = world.wait_task("submission.grade", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                                 what="a correção com not_found")
    assert [c.status for c in world.engine_fake.calls("grade")] == [404]
    [submission] = world.submissions(delivery)
    assert submission["outcome"] == "grading"
    refetch = world.wait_task("question.refetch", lambda t: t["state"] == "done", what="a nova obtenção")
    assert refetch["idempotency_key"] == f"question.refetch:{world.ref_row('number')}:r1"
    assert refetch["payload"] == {"question_ref_row_id": world.ref_row("number")}
    new_calls = world.fetcher.calls(REPO, COMMIT)[fetches_before:]
    assert [(c.outcome, c.reused) for c in new_calls] == [("Fetched", False)]

    world.advance(_seconds(grade_task["available_at"] - world.clock.now()))
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    assert [c.status for c in world.engine_fake.calls("grade")] == [404, 200]
    world.wait_delivery(dispatch, alice, "answered")
    assert len(world.tasks("question.refetch")) == 1
    assert world.fetcher.count(REPO, COMMIT) == fetches_before + 1
    _until(lambda: _appearances(world.rich, off.bot, alice.chat, deliverytexts.GRADING_DELAYED) == 1,
           what="o aviso GRADING_DELAYED")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.principal(off, alice, delivery).plain_text or ""),
           what="o feedback da correção")
    assert _appearances(world.rich, off.bot, alice.chat, deliverytexts.GRADING_DELAYED) == 1


@pytest.mark.parametrize("failure", [FetchTemporaryFailure("connect"), FetchNotFound("repository")],
                         ids=["temporaria", "definitiva"])
def test_refetch_that_fails_keeps_submission_grading_with_one_live_refetch_and_blocks_archive(
        world: _World, failure: Any) -> None:
    """Com a árvore apagada e a nova obtenção falhando
    (temporária, ou definitiva: o repositório sumiu na origem), a submissão continua `grading`, a
    correção repete com no máximo uma `question.refetch` viva por referência (rodadas `r1`, `r2`… só
    depois de a anterior terminar), cada `question.refetch` `failed` gera um único `task.failed`, e o
    arquivamento do oferecimento é recusado com `grading_pending`."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    assert world.fetcher.delete_tree(REPO, COMMIT)
    world.fetcher.program(REPO, COMMIT, failure)
    world.answer(off, alice, delivery)
    _until(lambda: any(c.outcome == type(failure).__name__ for c in world.fetcher.calls(REPO, COMMIT)),
           what="a nova obtenção que falha")

    ref = world.ref_row("number")
    for _ in range(6):
        assert len(world.live_tasks("question.refetch")) <= 1, "mais de uma question.refetch viva"
        world.advance(20)
        time.sleep(0.3)
        assert len(world.live_tasks("question.refetch")) <= 1, "mais de uma question.refetch viva"

    refetches = world.tasks("question.refetch")
    assert [t["idempotency_key"] for t in refetches] == [
        f"question.refetch:{ref}:r{k}" for k in range(1, len(refetches) + 1)]
    assert all(t["payload"] == {"question_ref_row_id": ref} for t in refetches)
    failed_ids = [t["id"] for t in refetches if t["state"] == "failed"]
    refetch_failures = [d["task_id"] for _, d in world.events("task.failed") if d["task_type"] == "question.refetch"]
    assert refetch_failures == failed_ids
    [submission] = world.submissions(delivery)
    assert submission["outcome"] == "grading"
    assert world.delivery_of(dispatch, alice)["state"] == "delivered"
    assert all(c.status == 404 for c in world.engine_fake.calls("grade"))
    assert world.events(*SUBMISSION_EVENTS) == []

    resp = world.client.post(f"/v1/offerings/{off.code}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    resp = world.client.post(f"/v1/offerings/{off.code}/archive/prepare", headers=auth(ADMIN_TOKEN))
    assert_error(resp, 409, "grading_pending")


def test_second_loss_of_the_tree_creates_a_round_two_refetch(world: _World) -> None:
    """Duas perdas seguidas da árvore; a segunda
    `question.refetch` da mesma referência é a rodada `:r2` (a chave fixa não é reusada depois de uma
    tarefa terminada) e as duas correções saem."""
    off = world.offering()
    alice = world.student(off)
    bruno = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    ref = world.ref_row("number")
    # As duas entregas saem antes da primeira perda: a preparação da segunda não pode encontrar a árvore apagada.
    for student in (alice, bruno):
        world.wait_delivery(dispatch, student, "delivered")
    for n, student in enumerate((alice, bruno), start=1):
        delivery = world.wait_delivery(dispatch, student, "delivered")
        assert world.fetcher.delete_tree(REPO, COMMIT)
        world.answer(off, student, delivery)
        world.wait_task("question.refetch", lambda t, n=n: len(world.tasks("question.refetch")) == n
                        and t["state"] == "done", what=f"a {n}ª nova obtenção")
        grade = world.wait_task("submission.grade", lambda t: t["state"] in ("pending", "done"),
                                what="a correção seguinte")
        if grade["state"] == "pending":
            world.advance(max(0.0, _seconds(grade["available_at"] - world.clock.now())))
        [graded] = world.wait_submissions(delivery, 1)
        assert (graded["outcome"], graded["valid"]) == ("graded", True)
    assert [(t["idempotency_key"], t["state"]) for t in world.tasks("question.refetch")] == [
        (f"question.refetch:{ref}:r1", "done"), (f"question.refetch:{ref}:r2", "done")]


def test_three_hundred_gradings_on_the_same_not_found_keep_a_single_live_refetch(world: _World) -> None:
    """Trezentas correções batem no mesmo `not_found` (três
    alunos; o worker e depois o tratador `submission.grade` chamado de novo com as tarefas deles) com a
    nova obtenção falhando de forma temporária: existe uma só `question.refetch` para a referência, e
    ela continua viva; as submissões continuam `grading`."""
    off = world.offering()
    students = [world.student(off) for _ in range(3)]
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    deliveries = [world.wait_delivery(dispatch, s, "delivered") for s in students]
    assert world.fetcher.delete_tree(REPO, COMMIT)
    world.fetcher.program(REPO, COMMIT, FetchTemporaryFailure("connect"))
    for student, delivery in zip(students, deliveries, strict=True):
        world.answer(off, student, delivery)

    def attempted() -> list[dict] | None:
        grades = world.tasks("submission.grade")
        refetch = world.tasks("question.refetch")
        if (len(grades) == 3 and all(t["attempts"] >= 1 and t["state"] == "pending" for t in grades)
                and refetch and refetch[0]["attempts"] >= 1 and refetch[0]["state"] == "pending"):
            return grades
        return None

    grades = _until(attempted, what="as três correções e a nova obtenção tentadas")
    world.stop_workers()

    channel = TelegramChannel(world.settings.database_url, clock=world.clock, runtime=world.bots,
                              default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
    handler = grade_task_handler(world.settings.database_url, clock=world.clock, settings=world.settings,
                                 engine=world.new_engine(), channel=channel)
    try:
        claimed = [ClaimedTask(id=t["id"], task_type=t["task_type"], payload=t["payload"],
                               attempts=t["attempts"]) for t in grades]
        n = 0
        while len([c for c in world.engine_fake.calls("grade") if c.status == 404]) < 300:
            with pytest.raises(RetryTaskLater):
                handler(claimed[n % 3])
            n += 1
    finally:
        handler.close()
        channel.close()

    assert len([c for c in world.engine_fake.calls("grade") if c.status == 404]) == 300
    ref = world.ref_row("number")
    assert [(t["idempotency_key"], t["state"]) for t in world.tasks("question.refetch")] == [
        (f"question.refetch:{ref}:r1", "pending")]
    assert [s["outcome"] for d in deliveries for s in world.submissions(d)] == ["grading"] * 3


def test_tree_deleted_before_delivery_prepare_refetches_and_the_variant_goes_out(world: _World) -> None:
    """A árvore some antes da `delivery.prepare`; a criação da variante recebe
    `not_found`, a entrega continua `pending`, sai uma só `question.refetch` para a referência, a nova
    obtenção é contada no `repo_fetcher`, e a variante sai na tentativa seguinte."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    fetches_before = world.fetcher.count(REPO, COMMIT)
    variants_before = len(world.engine_fake.calls("create_variant"))
    assert world.fetcher.delete_tree(REPO, COMMIT)
    world.confirm(off, dispatch)

    prepare = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                              what="a delivery.prepare com not_found")
    delivery = world.delivery_of(dispatch, alice)
    assert (delivery["state"], delivery["variant_at"]) == ("pending", None)
    refetch = world.wait_task("question.refetch", lambda t: t["state"] == "done", what="a nova obtenção")
    assert refetch["idempotency_key"] == f"question.refetch:{world.ref_row('number')}:r1"
    assert [(c.outcome, c.reused) for c in world.fetcher.calls(REPO, COMMIT)[fetches_before:]] == [
        ("Fetched", False)]

    world.advance(_seconds(prepare["available_at"] - world.clock.now()))
    world.wait_delivery(dispatch, alice, "delivered")
    assert [c.status for c in world.engine_fake.calls("create_variant")][variants_before:] == [404, 200]
    assert len(world.tasks("question.refetch")) == 1
    assert world.fetcher.count(REPO, COMMIT) == fetches_before + 1


def test_tree_lost_before_prepare_with_fetch_not_found_stays_pending_until_close(world: _World) -> None:
    """A árvore some antes da `delivery.prepare` e a nova obtenção recebe
    `FetchNotFound` (o repositório sumiu na origem); a entrega continua `pending` e vira
    `not_delivered/engine_unavailable` no fechamento."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    assert world.fetcher.delete_tree(REPO, COMMIT)
    world.fetcher.program(REPO, COMMIT, FetchNotFound("repository"))
    world.confirm(off, dispatch)
    refetch = world.wait_task("question.refetch", lambda t: t["state"] == "failed",
                              what="a nova obtenção recusada")
    assert refetch["idempotency_key"] == f"question.refetch:{world.ref_row('number')}:r1"
    delivery = world.delivery_of(dispatch, alice)
    assert (delivery["state"], delivery["variant_at"]) == ("pending", None)
    assert len(world.live_tasks("question.refetch")) <= 1

    world.jump_past(_parse(dispatch["closes_at"]))
    world.wait_closed(dispatch)
    delivery = world.delivery_of(dispatch, alice)
    assert (delivery["state"], delivery["undelivered_reason"]) == ("not_delivered", "engine_unavailable")
    assert world.rich.messages(off.bot, alice.chat) == []
    assert world.events("delivery.not_delivered") == [
        ("delivery.not_delivered", _delivery_event(off, dispatch, delivery, alice,
                                                   undelivered_reason="engine_unavailable"))]


# ---------------------------------------------------------------------------------------------
# 400/413 na correção: falha interna, que reabre a entrega


@pytest.mark.parametrize(("status", "code"), [(400, "invalid_request"), (413, "payload_too_large")],
                         ids=["400", "413"])
def test_engine_rejecting_the_grading_request_is_an_internal_failure_that_reopens_the_delivery(
        world: _World, status: int, code: str) -> None:
    """Um 400/413 do motor na correção vira
    submissão `internal_failure` com o `failure_code`, `submission.internal_failure`, `ANSWER_KEPT` uma
    única vez ao aluno; a entrega continua `delivered` e é reaberta (partes e seleção limpas,
    `current_field` = o primeiro campo, edição com os controles e o pedido do primeiro campo); uma
    segunda falha interna na mesma entrega não repete o `ANSWER_KEPT`; a resposta seguinte é corrigida
    normalmente; a entrega nunca vira `question_failed`."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    [first_prompt] = _until(lambda: world.prompts(off, alice), what="o pedido do primeiro campo")
    before = world.delivery_of(dispatch, alice)

    world.engine_fake.fail("grade", error=code, times=1)
    world.answer(off, alice, delivery)
    [first] = world.wait_submissions(delivery, 1)
    assert (first["outcome"], first["valid"], first["failure_code"], first["score"], first["raw_input"]) == (
        "internal_failure", None, code, None, {"resposta": NUMBER_ANSWER})
    assert world.engine_fake.calls("grade")[-1].status == status
    reopened = world.delivery_of(dispatch, alice)
    assert (reopened["state"], reopened["current_field"], reopened["failure"]) == ("delivered", 0, None)
    assert reopened["controls_version"] > before["controls_version"]
    assert reopened["render_version"] > before["render_version"]
    assert world.contents(reopened) == {"parts": {}, "selection": []}

    def reopened_view() -> bool:
        question = world.principal(off, alice, reopened)
        return ("Não sei" in _labels(question) and len(world.prompts(off, alice)) == 2
                and _appearances(world.rich, off.bot, alice.chat, deliverytexts.ANSWER_KEPT) == 1)

    _until(reopened_view, what="a entrega reaberta ao aluno (controles, pedido do campo e ANSWER_KEPT)")
    question = world.principal(off, alice, reopened)
    new_prompt = world.prompts(off, alice)[1]
    assert new_prompt.message_id != first_prompt.message_id
    assert new_prompt.reply_parameters is not None
    assert (new_prompt.reply_parameters["message_id"],
            new_prompt.reply_parameters["allow_sending_without_reply"]) == (question.message_id, True)

    world.engine_fake.fail("grade", error=code, times=1)
    world.answer(off, alice, delivery, "7")
    second = world.wait_submissions(delivery, 2)[1]
    assert (second["outcome"], second["failure_code"]) == ("internal_failure", code)
    assert world.delivery_of(dispatch, alice)["state"] == "delivered"

    world.answer(off, alice, delivery)
    third = world.wait_submissions(delivery, 3)[2]
    assert (third["outcome"], third["valid"], third["score"]) == ("graded", True, 1.0)
    delivery = world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.principal(off, alice, delivery).plain_text or ""),
           what="o feedback da resposta corrigida")
    assert _appearances(world.rich, off.bot, alice.chat, deliverytexts.ANSWER_KEPT) == 1

    internal = [(t, d) for t, d in world.events(*SUBMISSION_EVENTS, "delivery.question_failed")
                if t != "submission.graded"]
    assert internal == [
        ("submission.internal_failure", _delivery_event(off, dispatch, delivery, alice,
                                                        submission_id=str(first["submission_uuid"]),
                                                        engine_error=code)),
        ("submission.internal_failure", _delivery_event(off, dispatch, delivery, alice,
                                                        submission_id=str(second["submission_uuid"]),
                                                        engine_error=code)),
    ]
    assert [d["submission_id"] for _, d in world.events("submission.graded")] == [str(third["submission_uuid"])]
