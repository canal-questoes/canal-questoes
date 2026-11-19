"""Nenhuma resposta perdida.


Contratos: uma submissão por update e no máximo uma submissão válida por entrega, pelo
índice `submissions_one_final_idx`; as chaves das rodadas da varredura, `<tipo>:<id>:r<k>`;
cada tratador confere o estado antes de agir (uma reexecução depois do commit
termina sem efeito); o toque vindo de um `message_id` desconhecido no endereço vale como
cópia não registrada da mensagem com os controles, com "você já
respondeu" nas respostas seguintes; a varredura reanima a tarefa `failed` pela posse
esgotada; o update é gravado uma vez por `(bot, época, update_id)`.

O arnês é o de `test_engine_failures.py`: oferecimento e bot pela API de oferecimentos, pessoas pelo
cadastro da API base, matrícula de aluno e endereço vigente por SQL; o worker real numa thread, com o
adaptador HTTP real sobre o motor falso, a obtenção falsa e a Bot API falsa com a extensão do disparo.
A queda do worker é um `Crash(BaseException)` levantado pelo motor, embrulhado, na primeira correção
(a tarefa fica em posse, como numa queda do processo); a posse esgotada é montada por SQL
(`attempts = max_attempts`, `state = 'leased'`, `lease_until` no passado), como no vínculo do aluno: o `claim` da API base
a marca `failed` (com `task.failed`), e a varredura cria a rodada seguinte depois do intervalo
(`backoff_max_seconds`). A rodada `dispatch.close:<id>:r1` que um worker que caiu deixou em posse é
gravada por SQL com a chave e o payload que o produto usa.

**Reexecução depois do commit**: um cenário completo passa por todos os tipos
de tarefa do disparo e pelos `telegram.update` de dois toques (marcar e "Enviar"); com o worker
parado, o teste monta o tratador do tipo como o `run_worker` monta (o canal com rota é o
`TelegramChannel` sobre a fixture `bots`, com um `ConsoleChannel` em memória como padrão) e o
chama de novo com o mesmo `ClaimedTask` da tarefa concluída (id, tipo, payload e tentativas lidos da
linha `done`; no `telegram.update`, um por toque). Antes e depois são iguais: eventos, disparos,
entregas, submissões, `outbox_messages`, a seleção e as partes em andamento (`delivery_contents`),
e as mensagens, edições e confirmações da Bot API falsa; e, na `delivery.prepare` e na
`submission.grade`, também as chamadas ao motor. Na `dispatch.validate` e na `question.refetch` é aceita, e declarada, uma leitura idempotente a mais no
motor (`health`/`inspect`) ou no `repo_fetcher` (`Fetched(reused=True)`).

Em todos os casos, `_assert_no_answer_lost`: nenhuma submissão `grading` nem update `pending` no fim,
toda resposta gravada com a sua submissão, nenhuma entrega com mais de uma submissão válida e nenhuma
resposta no prazo marcada como atrasada.

No esqueleto, toda função falha no primeiro
`dispatches.prepare` (rota ausente, 404). Ficam verdes com a implementação, inclusive a reexecução
depois do commit.

"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import threading
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
from cq_server.tasks import ClaimedTask
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    confirm_dispatch,
    create_offering,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import TelegramUser, make_user
from tests.helpers.channels import Crash
from tests.helpers.clock import iso

COURSE = "Estruturas de Dados"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/turma/questoes-sem-perda.git"
COMMIT = "5a1f" * 10
WORKER_ID = "worker-sem-perda"
SECOND_WORKER = "worker-sem-perda-2"
DEAD_WORKER = "worker-que-caiu"
TIMEOUT = 20.0
ENGINE_TIMEOUT = 10.0
DAY = timedelta(days=1)
SHORT = timedelta(minutes=10)
STEP = 10.0
RENEW_MARGIN = timedelta(seconds=13)
RETRY_GAP = 60.0  # settings.backoff_max_seconds
CLOSE_RECHECK = 6.0
NUMBER_ANSWER = "6.5"
# A 1ª linha do teclado da escolha única com `(b)` marcada (rótulos de referência).
MARKED_FIRST_ROW = ["(a)", "✅ (b)", "(c)", "(d)"]

REEXECUTED_TYPES = ("dispatch.validate", "dispatch.open", "dispatch.expire", "delivery.prepare",
                    "submission.grade", "dispatch.close", "delivery.reveal", "question.refetch",
                    "telegram.update")
# Tipos em que uma leitura idempotente a mais é aceita na reexecução (declarado no docstring).
EXTRA_READ_TYPES = ("dispatch.validate", "question.refetch")

_codes = itertools.count(1)
_people = itertools.count(1)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any, bot_settings: Any) -> None:
    """As esperas deste arquivo dependem do `settings` e do `bot_settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (30, 3, 2, 60)
    assert (bot_settings.poll_timeout_seconds, bot_settings.telegram_timeout_seconds,
            bot_settings.renew_seconds) == (1, 2, 10)


# ---------------------------------------------------------------------------------------------
# Apoio (o arnês de test_engine_failures.py)


def _until(fn: Callable[[], Any], *, what: str, timeout: float = TIMEOUT) -> Any:
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


@dataclasses.dataclass(frozen=True)
class _Offering:
    code: str
    offering_id: str
    row_id: int
    bot: int
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


class _CrashOnGrade:
    """O motor real embrulhado: a primeira correção derruba o worker (`Crash`, que não é `Exception`)
    antes de chamar o motor; o resto delega ao adaptador HTTP real."""

    def __init__(self, inner: HttpQuestionEngine) -> None:
        self._inner = inner
        self.crashed = threading.Event()

    def inspect(self, ref: Any) -> Any:
        return self._inner.inspect(ref)

    def create_variant(self, ref: Any, seed: str) -> Any:
        return self._inner.create_variant(ref, seed)

    def grade(self, ref: Any, snapshot: Any, raw_answers: Any) -> Any:
        if not self.crashed.is_set():
            self.crashed.set()
            raise Crash()
        return self._inner.grade(ref, snapshot, raw_answers)

    def close(self) -> None:
        self._inner.close()


class _World:
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
        self.threads: list[tuple[Any, str]] = []
        self._last: dict[str, Any] = {}

    def new_engine(self) -> HttpQuestionEngine:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url,
                                                   timeout_seconds=ENGINE_TIMEOUT))
        self._engines.append(engine)
        return engine

    def offering(self) -> _Offering:
        code = f"sem-perda-{next(_codes)}"
        body = create_offering(self.client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
        bot = self.rich.add_bot()
        register_bot(self.client, ADMIN_TOKEN, code, self.rich.token_of(bot))
        activate_bot(self.client, ADMIN_TOKEN, code)
        row_id, bot_row = self.sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.code = %s",
            (code,)).fetchone()
        return _Offering(code=code, offering_id=body["offering_id"], row_id=row_id, bot=bot, bot_row=bot_row)

    def student(self, off: _Offering) -> _Student:
        """Aluno vinculado (ver `test_engine_failures.py`): pessoa pela API base, matrícula e endereço por SQL; o
        chat privado passa a existir na Bot API falsa por um update fora do `allowed_updates`."""
        n = next(_people)
        user = make_user(n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Pessoa Aluna Sem Perda {n:02d}")
        person_row = self.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                      (body["pseudo_id"],)).fetchone()[0]
        now = self.clock.now()
        enrollment_row = self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
            "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) "
            "RETURNING id", (person_row, off.row_id, now, now, now)).fetchone()[0]
        endpoint_row = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
            "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, off.bot_row, user.user_id, now, now)).fetchone()[0]
        dropped = self.rich.push_update(off.bot, {"message_reaction": {
            "chat": user.as_private_chat(), "message_id": 1, "user": user.as_user(), "date": 0,
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}})
        assert dropped is None
        return _Student(user=user, pseudo_id=body["pseudo_id"], person_row=person_row,
                        enrollment_row=enrollment_row, endpoint_row=endpoint_row)

    # -- worker ----------------------------------------------------------------------------

    def start_worker(self, off: _Offering, *, engine: Any = None, worker_id: str = WORKER_ID,
                     wait_polls: bool = True) -> Any:
        chosen = engine if engine is not None else self.engine
        thread = self._start(settings=dataclasses.replace(self.settings, worker_id=worker_id), engine=chosen,
                             fetcher=self.fetcher, delivery=DeliverySettings())
        self.threads.append((thread, worker_id))
        self._last = {"off": off, "engine": chosen, "worker_id": worker_id}
        if wait_polls:
            self.rich.wait_for(lambda: self.rich.pending_polls(off.bot), timeout=TIMEOUT)
        return thread

    def stop_workers(self) -> None:
        for thread, _ in self.threads:
            if thread.is_alive():
                assert self._start.stop_one(thread) < 10.0, "o worker não parou em 10 s"
        self.threads = [(t, w) for t, w in self.threads if t.is_alive()]

    def restart(self) -> Any:
        return self.start_worker(self._last["off"], engine=self._last["engine"], worker_id=self._last["worker_id"])

    def close(self) -> None:
        self._start.stop()
        for engine in self._engines:
            with contextlib.suppress(Exception):
                engine.close()

    # -- relógio ---------------------------------------------------------------------------

    def _wait_leases(self) -> None:
        """Espera a renovação das posses dos bots mantidas por workers vivos (até 3 s reais)."""
        live = [w for t, w in self.threads if t.is_alive()]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            now = self.clock.now()
            rows = self.sql.execute("SELECT lease_until FROM bots WHERE lease_owner = ANY(%s)", (live,)).fetchall()
            if all(until - now >= RENEW_MARGIN for (until,) in rows):
                return
            time.sleep(0.02)

    def advance(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0:
            step = min(STEP, remaining)
            self.clock.advance(step)
            remaining -= step
            self._wait_leases()

    def jump_past(self, moment: datetime) -> None:
        self.stop_workers()
        self.clock.set(max(self.clock.now(), moment + timedelta(seconds=1)))
        self.restart()

    # -- disparo ---------------------------------------------------------------------------

    def validated(self, off: _Offering, qid: str, *, closes_in: timedelta = DAY) -> dict:
        self.engine_fake.program((REPO, COMMIT, qid), qid)
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
        deadline = time.monotonic() + 3 * TIMEOUT
        while True:
            try:
                return _until(lambda: (row := self.dispatch_row(dispatch)) and row["state"] == "closed" and row,
                              what="o fechamento", timeout=2.0)
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
                self.advance(CLOSE_RECHECK)

    # -- leitura ---------------------------------------------------------------------------

    def delivery_of(self, dispatch: dict, student: _Student) -> dict:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT d.*, d.delivery_uuid::text AS delivery_id FROM deliveries d "
                "JOIN dispatches p ON p.id = d.dispatch_id WHERE p.dispatch_uuid = %s AND d.enrollment_id = %s",
                (dispatch["dispatch_id"], student.enrollment_row)).fetchone()

    def wait_delivery(self, dispatch: dict, student: _Student, state: str) -> dict:
        def ready() -> dict | None:
            row = self.delivery_of(dispatch, student)
            if row and row["state"] == state and (state != "delivered" or row["sent_at"]):
                return row
            return None
        return _until(ready, what=f"a entrega em {state}")

    def submissions(self, delivery: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM submissions WHERE delivery_id = %s ORDER BY id",
                               (delivery["id"],)).fetchall()

    def wait_submissions(self, delivery: dict, count: int, *, settled: bool = True) -> list[dict]:
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

    def exhaust_lease(self, task_id: int) -> None:
        """A posse esgotada montada por SQL (como no vínculo do aluno): em posse de um worker que caiu, com as
        tentativas esgotadas e a posse vencida."""
        self.sql.execute(
            "UPDATE tasks SET state = 'leased', lease_owner = %s, lease_until = %s, attempts = %s WHERE id = %s",
            (DEAD_WORKER, self.clock.now() - timedelta(seconds=1), self.settings.max_attempts, task_id))

    def principal(self, off: _Offering, student: _Student, delivery: dict) -> Any:
        row = self.sql.execute(
            "SELECT o.channel_ref FROM deliveries d JOIN outbox_messages o ON o.id = d.principal_message_id "
            "WHERE d.id = %s", (delivery["id"],)).fetchone()
        assert row is not None and row[0] is not None, "a entrega não tem mensagem principal"
        return self.rich.current(off.bot, student.chat, int(row[0]))

    def answer(self, off: _Offering, student: _Student, delivery: dict, text: str = NUMBER_ANSWER) -> int:
        question = self.principal(off, student, delivery)
        update_id = self.rich.push_text(off.bot, student.user, text, reply_to_message_id=question.message_id)
        assert update_id is not None
        return update_id

    def update_row(self, off: _Offering, update_id: int) -> dict:
        def ready() -> dict | None:
            with self.sql.cursor(row_factory=dict_row) as cur:
                row = cur.execute(
                    "SELECT u.* FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                    "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (off.bot, update_id)).fetchone()
            return row if row and row["state"] == "processed" else None
        return _until(ready, what="o update processado")

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


def _data(message: Any, action: str, key: str | None = None) -> str:
    """O `callback_data` do botão da ação (e da chave) no teclado corrente."""
    for button in message.buttons():
        parts = (button.data or "").split(":")
        if len(parts) >= 5 and parts[0] == "cq1" and parts[2] == action and (key is None or parts[5:] == [key]):
            return button.data
    raise AssertionError(f"sem botão {action}/{key} no teclado: {[b.label for b in message.buttons()]}")


def _assert_no_answer_lost(world: _World) -> None:
    """toda resposta gravada terminou corrigida ou com desfecho registrado; nenhuma entrega com
    mais de uma submissão válida; nenhuma resposta no prazo marcada como atrasada."""
    sql = world.sql
    assert sql.execute("SELECT count(*) FROM submissions WHERE outcome = 'grading'").fetchone()[0] == 0
    assert sql.execute("SELECT count(*) FROM telegram_updates WHERE state = 'pending'").fetchone()[0] == 0
    orphans = sql.execute(
        "SELECT u.id FROM telegram_updates u WHERE u.outcome IN ('answer_recorded', 'answer_refused', "
        "'answer_dont_know') AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.telegram_update_id = u.id)"
    ).fetchall()
    assert orphans == []
    doubles = sql.execute(
        "SELECT delivery_id FROM submissions WHERE (outcome = 'graded' AND valid) OR outcome = 'dont_know' "
        "GROUP BY delivery_id HAVING count(*) > 1").fetchall()
    assert doubles == []
    late_in_time = sql.execute(
        "SELECT s.id FROM submissions s JOIN deliveries d ON d.id = s.delivery_id "
        "JOIN dispatches p ON p.id = d.dispatch_id WHERE s.late AND s.received_at <= p.closes_at").fetchall()
    assert late_in_time == []


# ---------------------------------------------------------------------------------------------
# Queda do worker, update duplicado e mensagem duplicada


def test_worker_crash_between_recording_and_grading_is_revived_by_the_sweep(world: _World) -> None:
    """A resposta é gravada (`answer_recorded`, submissão
    `grading`) e o worker cai na correção (a tarefa fica em posse); com a posse esgotada, o `claim` a
    marca `failed` (`task.failed`), a varredura cria `submission.grade:<id>:r2` e a correção sai."""
    off = world.offering()
    alice = world.student(off)
    crashing = _CrashOnGrade(world.new_engine())
    first_worker = world.start_worker(off, engine=crashing)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    update_id = world.answer(off, alice, delivery)
    assert world.update_row(off, update_id)["outcome"] == "answer_recorded"
    _until(lambda: not first_worker.is_alive(), what="a queda do worker")
    assert isinstance(first_worker.error, Crash)
    assert crashing.crashed.is_set()

    [pending] = world.submissions(delivery)
    assert pending["outcome"] == "grading"
    [grade] = world.tasks("submission.grade")
    assert (grade["state"], grade["attempts"], grade["lease_owner"]) == ("leased", 1, WORKER_ID)
    assert world.engine_fake.calls("grade") == []
    world.exhaust_lease(grade["id"])

    world.start_worker(off, engine=crashing, worker_id=SECOND_WORKER, wait_polls=False)
    world.wait_task("submission.grade", lambda t: t["state"] == "failed", what="a correção esgotada pela posse")
    world.advance(RETRY_GAP)
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["id"], graded["outcome"], graded["valid"], graded["score"], graded["late"]) == (
        pending["id"], "graded", True, 1.0, False)
    tasks = world.tasks("submission.grade")
    assert [(t["id"], t["state"]) for t in tasks] == [(grade["id"], "failed"), (tasks[1]["id"], "done")]
    assert tasks[1]["idempotency_key"] == f"submission.grade:{pending['id']}:r2"
    failed = [d for _, d in world.events("task.failed") if d["task_type"] == "submission.grade"]
    assert [(d["task_id"], d["attempts"]) for d in failed] == [(grade["id"], world.settings.max_attempts)]
    world.wait_delivery(dispatch, alice, "answered")
    assert len(world.engine_fake.calls("grade")) == 1
    _assert_no_answer_lost(world)


def test_the_same_update_delivered_twice_is_stored_once_with_a_single_submission(world: _World) -> None:
    """O Telegram entrega de novo o mesmo update (mesmo `update_id`); ele
    é gravado uma só vez, e existe uma só submissão (e uma só correção)."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    update_id = world.answer(off, alice, delivery)
    [graded] = world.wait_submissions(delivery, 1)
    world.rich.redeliver(off.bot, update_id)

    def delivered_twice() -> bool:
        batches = [c.result or [] for c in world.rich.calls("getUpdates") if c.outcome == "ok"]
        return sum(1 for batch in batches for u in batch if u.get("update_id") == update_id) >= 2

    _until(delivered_twice, what="o update entregue de novo")
    time.sleep(1.0)
    rows = world.sql.execute(
        "SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (off.bot, update_id)).fetchone()[0]
    assert rows == 1
    assert [s["id"] for s in world.submissions(delivery)] == [graded["id"]]
    assert (graded["outcome"], graded["valid"]) == ("graded", True)
    assert len(world.engine_fake.calls("grade")) == 1
    assert len(world.events("submission.graded")) == 1
    _assert_no_answer_lost(world)


def test_tap_on_the_unregistered_copy_counts_and_the_next_on_the_principal_is_already_answered(
        world: _World) -> None:
    """A mensagem da questão fica
    duplicada (`fail(…, after_effect=True)`); a primeira resposta, na cópia não registrada (marcar e
    "Enviar"), vale, e a edição da marcação sai na principal; o toque seguinte, na mensagem
    principal (com o teclado de antes da edição), recebe "você já respondeu" e é gravado como
    `refused/already_answered`; uma só submissão válida, e a cópia nunca é editada."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "single_choice")
    world.rich.fail("sendRichMessage", status=502, times=1, after_effect=True)
    world.confirm(off, dispatch)
    _until(lambda: [c for c in world.rich.calls("sendRichMessage") if c.outcome == "error"], what="o 5xx")
    world.advance(STEP)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    principal = world.principal(off, alice, delivery)
    copies = [m for m in world.rich.messages(off.bot, alice.chat) if m.buttons()]
    assert len(copies) == 2, "a mensagem da questão deveria estar duplicada"
    [copy] = [m for m in copies if m.message_id != principal.message_id]
    stale = _data(principal, "m", "a")

    # Marcar `(b)` na cópia: sem submissão; a edição da marcação sai na principal.
    mark = world.rich.tap(off.bot, alice.user, copy.message_id, _data(copy, "m", "b"))
    assert world.update_row(off, mark.update_id)["outcome"] == "answer_toggled"
    assert world.submissions(delivery) == []
    marked = _until(lambda: [e for e in world.rich.edits(off.bot, alice.chat, principal.message_id)
                             if [b.label for b in (e.after.keyboard or ((),))[0]] == MARKED_FIRST_ROW],
                    what="a edição da marcação na principal")
    # "Enviar" na cópia: a resposta vale.
    first = world.rich.tap(off.bot, alice.user, copy.message_id, _data(copy, "s"))
    assert world.update_row(off, first.update_id)["outcome"] == "answer_recorded"
    [valid] = world.wait_submissions(delivery, 1)
    assert (valid["outcome"], valid["valid"], valid["score"]) == ("graded", True, 1.0)
    world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: [e for e in world.rich.edits(off.bot, alice.chat, principal.message_id)
                    if e.call_seq > marked[0].call_seq], what="a edição da principal com a resposta")

    second = world.rich.tap_raw(off.bot, alice.user, principal.message_id, stale)
    assert world.update_row(off, second.update_id)["outcome"] == "answer_refused"
    submissions = world.wait_submissions(delivery, 2)
    assert (submissions[1]["outcome"], submissions[1]["refusal"]) == ("refused", "already_answered")
    answers = _until(lambda: [a for a in world.rich.answers(off.bot) if a.callback_id == second.callback_id],
                     what="a confirmação do segundo toque")
    assert [a.text for a in answers] == [deliverytexts.ALREADY_ANSWERED]
    assert len([s for s in submissions if s["outcome"] == "graded" and s["valid"]]) == 1
    assert world.rich.edits(off.bot, alice.chat, copy.message_id) == []
    _assert_no_answer_lost(world)


# ---------------------------------------------------------------------------------------------
# Tarefas de entrega e de fechamento `failed` pela posse


def test_delivery_prepare_failed_by_an_exhausted_lease_is_requeued_and_finishes(world: _World) -> None:
    """A `delivery.prepare` fica com a posse esgotada (worker que caiu); o
    `claim` a marca `failed`, a varredura cria `delivery.prepare:<id>:r2` depois do intervalo, e a
    entrega sai."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.engine_fake.stop()
    world.confirm(off, dispatch)
    prepare = world.wait_task("delivery.prepare", lambda t: t["state"] == "pending" and t["attempts"] == 1,
                              what="a delivery.prepare tentada")
    world.stop_workers()
    world.exhaust_lease(prepare["id"])
    world.engine_fake.start()
    world.restart()

    world.wait_task("delivery.prepare", lambda t: t["state"] == "failed", what="a delivery.prepare esgotada")
    world.advance(RETRY_GAP)
    world.wait_delivery(dispatch, alice, "delivered")
    delivery = world.delivery_of(dispatch, alice)
    tasks = world.tasks("delivery.prepare")
    assert [(t["id"], t["state"]) for t in tasks] == [(prepare["id"], "failed"), (tasks[1]["id"], "done")]
    assert tasks[1]["idempotency_key"] == f"delivery.prepare:{delivery['id']}:r2"
    failed = [d for _, d in world.events("task.failed") if d["task_type"] == "delivery.prepare"]
    assert [(d["task_id"], d["attempts"]) for d in failed] == [(prepare["id"], world.settings.max_attempts)]
    _assert_no_answer_lost(world)


def test_close_task_failed_by_an_exhausted_lease_is_requeued_and_the_dispatch_closes(world: _World) -> None:
    """A rodada `dispatch.close:<id>:r1` ficou em posse de um worker
    que caiu, com as tentativas esgotadas; o `claim` a marca `failed`, a varredura cria a rodada `r2`
    depois do intervalo, e o disparo fecha, com a resposta dada antes corrigida e revelada."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    world.answer(off, alice, delivery)
    world.wait_submissions(delivery, 1)
    world.wait_delivery(dispatch, alice, "answered")

    world.stop_workers()
    world.clock.set(_parse(dispatch["closes_at"]) + timedelta(seconds=1))
    now = world.clock.now()
    row = world.dispatch_row(dispatch)
    close_task = world.sql.execute(
        "INSERT INTO tasks (task_type, payload, idempotency_key, state, attempts, available_at, lease_owner, "
        "lease_until, created_at) VALUES ('dispatch.close', %s::jsonb, %s, 'leased', %s, %s, %s, %s, %s) "
        "RETURNING id",
        (json.dumps({"dispatch_row_id": row["id"]}), f"dispatch.close:{row['id']}:r1",
         world.settings.max_attempts, now, DEAD_WORKER, now - timedelta(seconds=1), now)).fetchone()[0]
    world.restart()

    _until(lambda: world.tasks("dispatch.close")[0]["state"] == "failed", what="a rodada r1 esgotada")
    world.advance(RETRY_GAP)
    world.wait_closed(dispatch)
    tasks = world.tasks("dispatch.close")
    assert (tasks[0]["id"], tasks[0]["state"]) == (close_task, "failed")
    assert tasks[1]["idempotency_key"] == f"dispatch.close:{row['id']}:r2"
    assert all(t["state"] == "done" for t in tasks[1:])
    failed = [d for _, d in world.events("task.failed") if d["task_type"] == "dispatch.close"]
    assert [(d["task_id"], d["attempts"]) for d in failed] == [(close_task, world.settings.max_attempts)]
    closed = world.delivery_of(dispatch, alice)
    assert closed["state"] == "answered" and closed["revealed_at"] is not None
    [(_, data)] = world.events("dispatch.closed")
    assert (data["trigger"], data["counts"]["answered"]) == ("window", 1)
    _assert_no_answer_lost(world)


# ---------------------------------------------------------------------------------------------
# Reexecução depois do commit


@dataclasses.dataclass
class _Scenario:
    off: _Offering
    alice: _Student
    opened: dict  # o disparo que abre, recebe a resposta, fecha e revela
    expired: dict  # o disparo validado e nunca confirmado, que expira
    tap_update_ids: tuple[int, ...]  # os updates dos toques: a marcação e o "Enviar"


def _full_scenario(world: _World) -> _Scenario:
    """Um cenário que conclui uma tarefa de cada tipo do disparo e os `telegram.update` de dois toques (marcar
    `(b)` e "Enviar"): validação de dois disparos; abertura e entrega de um; a árvore apagada antes
    do "Enviar" (`not_found`, nova obtenção e correção); fechamento de um e expiração do outro; e uma
    revelação devida sem tarefa (`revealed_at` anulado por SQL depois do fechamento) para a
    `delivery.reveal` da varredura."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    opened = world.validated(off, "single_choice", closes_in=SHORT)
    expired = world.validated(off, "number")
    world.confirm(off, opened)
    delivery = world.wait_delivery(opened, alice, "delivered")
    question = world.principal(off, alice, delivery)
    mark = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "b"))
    assert world.update_row(off, mark.update_id)["outcome"] == "answer_toggled"
    assert world.fetcher.delete_tree(REPO, COMMIT)
    tap = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "s"))
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_recorded"
    world.wait_task("question.refetch", lambda t: t["state"] == "done", what="a nova obtenção")
    grade = world.wait_task("submission.grade", lambda t: t["state"] in ("pending", "done"), what="a correção")
    if grade["state"] == "pending":
        world.advance(max(0.0, (grade["available_at"] - world.clock.now()).total_seconds()))
    world.wait_submissions(delivery, 1)
    world.wait_delivery(opened, alice, "answered")

    deadline = world.dispatch_row(expired)["confirm_deadline"]
    world.jump_past(max(_parse(opened["closes_at"]), deadline))
    world.wait_closed(opened)
    world.wait_dispatch(expired, "cancelled")
    assert world.dispatch_row(expired)["cancel_reason"] == "expired"
    revealed = world.delivery_of(opened, alice)
    assert revealed["revealed_at"] is not None
    world.sql.execute("UPDATE deliveries SET revealed_at = NULL WHERE id = %s", (revealed["id"],))
    world.wait_task("delivery.reveal", lambda t: t["state"] == "done", what="a revelação da varredura")
    assert world.delivery_of(opened, alice)["revealed_at"] is not None
    _until(lambda: world.sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0] == 0,
        what="a fila de saída vazia")
    world.stop_workers()
    return _Scenario(off=off, alice=alice, opened=opened, expired=expired,
                     tap_update_ids=(mark.update_id, tap.update_id))


def _done_tasks(world: _World, scenario: _Scenario, task_type: str) -> list[ClaimedTask]:
    """Os `ClaimedTask` das tarefas `done` do tipo (id, tipo, payload e tentativas da linha): uma por
    tipo; no `telegram.update`, uma por toque (a marcação e o "Enviar")."""
    sql = world.sql
    if task_type == "telegram.update":
        filters = [("idempotency_key = %s", (f"telegram.update:{world.update_row(scenario.off, u)['id']}",))
                   for u in scenario.tap_update_ids]
    elif task_type in ("dispatch.validate", "dispatch.open", "dispatch.close", "dispatch.expire"):
        target = scenario.expired if task_type == "dispatch.expire" else scenario.opened
        filters = [("payload @> %s::jsonb", (json.dumps({"dispatch_row_id": world.dispatch_row(target)["id"]}),))]
    else:
        filters = [("TRUE", ())]
    tasks = []
    for where, args in filters:
        with sql.cursor(row_factory=dict_row) as cur:
            row = cur.execute(f"SELECT * FROM tasks WHERE task_type = %s AND state = 'done' AND {where} "
                              "ORDER BY id DESC LIMIT 1", (task_type, *args)).fetchone()
        assert row is not None, f"o cenário não concluiu uma tarefa {task_type}"
        tasks.append(ClaimedTask(id=row["id"], task_type=row["task_type"], payload=row["payload"],
                                 attempts=row["attempts"]))
    return tasks


def _handler(world: _World, task_type: str, engine: HttpQuestionEngine, channel: TelegramChannel) -> Any:
    """O tratador do tipo, montado como o `run_worker` monta."""
    from cq_server import deliveries, dispatches, grading, reception

    conninfo = world.settings.database_url
    common: dict[str, Any] = {"clock": world.clock, "settings": world.settings}
    factories: dict[str, Callable[[], Any]] = {
        "dispatch.validate": lambda: dispatches.validate_task_handler(conninfo, **common, engine=engine,
                                                                      fetcher=world.fetcher, channel=channel),
        "dispatch.open": lambda: dispatches.open_task_handler(conninfo, **common),
        "dispatch.expire": lambda: dispatches.expire_task_handler(conninfo, **common),
        "dispatch.close": lambda: dispatches.close_task_handler(conninfo, **common, channel=channel),
        "delivery.prepare": lambda: deliveries.prepare_task_handler(conninfo, **common, engine=engine,
                                                                    channel=channel),
        "delivery.reveal": lambda: deliveries.reveal_task_handler(conninfo, **common, channel=channel),
        "submission.grade": lambda: grading.grade_task_handler(conninfo, **common, engine=engine, channel=channel),
        "question.refetch": lambda: grading.refetch_task_handler(conninfo, **common, fetcher=world.fetcher),
        "telegram.update": lambda: reception.update_task_handler(conninfo, clock=world.clock, runtime=world.bots,
                                                                 settings=world.settings, channel=channel,
                                                                 delivery=DeliverySettings()),
    }
    return factories[task_type]()


def _state(world: _World) -> dict[str, Any]:
    """O estado comparado antes e depois da reexecução (ver o docstring)."""
    def rows(query: str) -> list[str]:
        return [r[0] for r in world.sql.execute(query).fetchall()]

    return {
        "events": rows("SELECT row_to_json(e)::text FROM events e ORDER BY seq"),
        "dispatches": rows("SELECT row_to_json(d)::text FROM dispatches d ORDER BY id"),
        "deliveries": rows("SELECT row_to_json(d)::text FROM deliveries d ORDER BY id"),
        "submissions": rows("SELECT row_to_json(s)::text FROM submissions s ORDER BY id"),
        "outbox_messages": rows("SELECT row_to_json(o)::text FROM outbox_messages o ORDER BY id"),
        # A marcação reexecutada não regrava a seleção.
        "delivery_contents": rows("SELECT json_build_object('delivery_id', delivery_id, 'selection', selection, "
                                  "'parts', parts)::text FROM delivery_contents ORDER BY delivery_id"),
        "messages": world.rich.messages(),
        "edits": world.rich.edits(),
        "answers": world.rich.answers(),
    }


@pytest.mark.parametrize("task_type", REEXECUTED_TYPES)
def test_reexecution_after_commit_has_no_new_effect(world: _World, task_type: str) -> None:
    """Depois de a tarefa concluir (`done`), o tratador do tipo, chamado
    de novo com o mesmo `ClaimedTask`, termina sem efeito: eventos, disparos, entregas, submissões,
    `outbox_messages` e o que a Bot API falsa recebeu ficam iguais; na `delivery.prepare` e na
    `submission.grade`, o motor não é chamado; na `dispatch.validate` e na `question.refetch`, só uma
    leitura idempotente a mais é aceita. No `telegram.update`, os dois toques (a marcação, que não regrava
    a seleção nem edita de novo, e o "Enviar", que não cria outra submissão)."""
    scenario = _full_scenario(world)
    tasks = _done_tasks(world, scenario, task_type)
    engine = world.new_engine()
    channel = TelegramChannel(world.settings.database_url, clock=world.clock, runtime=world.bots,
                              default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
    handler = _handler(world, task_type, engine, channel)
    before = _state(world)
    engine_before = len(world.engine_fake.calls())
    fetches_before = len(world.fetcher.calls())
    try:
        for task in tasks:
            handler(task)
    finally:
        handler.close()
        channel.close()
    time.sleep(0.5)  # uma confirmação ou mensagem que escapasse do tratador chegaria à Bot API falsa
    after = _state(world)
    assert {k: v for k, v in after.items() if v != before[k]} == {}

    new_engine_calls = world.engine_fake.calls()[engine_before:]
    new_fetches = world.fetcher.calls()[fetches_before:]
    if task_type in EXTRA_READ_TYPES:
        assert {c.op for c in new_engine_calls} <= {"health", "inspect"}
        assert len([c for c in new_engine_calls if c.op == "inspect"]) <= 1
        assert len(new_fetches) <= 1 and all(c.reused for c in new_fetches)
        if task_type == "question.refetch":
            assert new_engine_calls == []
    else:
        assert new_engine_calls == []
        assert new_fetches == []
    _assert_no_answer_lost(world)
