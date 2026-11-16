"""Falhas do Telegram nas mensagens de entrega.


Contratos: a fila de saída das entregas (papéis, fusão das edições, a edição em voo que
volta a `pending` e absorve a pendente), o envio (o resultado do envio decide a entrega;
`Undeliverable` classificado pelo `blocked` ou pelo `reason`; `endpoint_inactive` → `channel_refused`,
sem o 403), o ritmo (1 mensagem por segundo por chat, 30 por bot, `penalize` no `retry_after`), a recepção
(toque e texto: o chat que não é o endereço vigente é "sem efeito"; a regra do campo digitável único),
as mensagens assíncronas (ao endereço vigente) e a edição que falha (429 e 5xx
repetem; bloqueio só registra; os demais definitivos → `fallback` em resposta à original, com a
renderização completa, que vira a principal se leva os controles; um só `fallback`; `fallback`
e `notice` que falham de vez só registram); a troca de conta com o
disparo aberto e o bot sem acesso.

O arnês é o de `test_engine_failures.py`: oferecimento e bot pela API de oferecimentos e bots, pessoas pelo
cadastro da API base, matrícula de aluno e endereço vigente por SQL; o worker real numa thread, com o
adaptador HTTP real sobre o motor falso, a obtenção falsa e a Bot API falsa com a extensão do disparo e da resposta
(`rich_bot_api`), no modo `rich` (padrão). As falhas do Telegram são programadas na Bot API falsa
(`fail`, `fail(…, after_effect=True)`, `block_chat`, `delete_message`, `on_call`); as trocas de conta,
por SQL (o endereço antigo `inactive` e um endereço novo `active`, como o vínculo do aluno deixa depois do novo
vínculo). O caso da edição em voo sobe dois workers: enquanto um segura a edição (o
gancho `on_call` da Bot API falsa espera o teste), o outro processa o toque seguinte; o prazo do
Telegram desses workers é maior (8 s, dentro do invariante da posse) para o gancho caber.

Tempo: as esperas da fila andam no `FakeClock`; o ritmo (`SendPacer`) usa o relógio monotônico
real e é medido nos `started`/`ended` da Bot API falsa (`perf_counter`). A folga `PACE_TOLERANCE`
cobre só a medição: o limitador lê o `time.monotonic()` (grão de 15,6 ms no Windows) na posse, e o
dublê marca a chegada do pedido HTTP.

No esqueleto, toda função falha no primeiro
`dispatches.prepare` (rota ausente, 404). Cada caso passa quando existir a parte de que depende, entre as
partes implementadas depois.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import deliverytexts
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.outbox import Outbox
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
from tests.helpers.bot_api_dispatch import DISPATCH_ERRORS
from tests.helpers.clock import iso
from tests.helpers.engine import ANSWER_SENTINEL, preset

COURSE = "Laboratório de Redes"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/turma/questoes-falhas-telegram.git"
COMMIT = "7e1e" * 10
WORKER_ID = "worker-falhas-telegram"
TIMEOUT = 20.0
ENGINE_TIMEOUT = 10.0
DAY = timedelta(days=1)
SHORT = timedelta(minutes=10)
STEP = 10.0
RENEW_MARGIN = timedelta(seconds=13)
CLOSE_RECHECK = 6.0
NUMBER_ANSWER = "6.5"
RETRY_AFTER = 2  # segundos do `retry_after` programado
CHAT_INTERVAL = 1.0  # DeliverySettings().chat_interval_seconds
BOT_PER_SECOND = 30  # DeliverySettings().bot_per_second
PACE_TOLERANCE = 0.05  # só a medição (ver o docstring)
HOOK_WAIT = 6.0  # quanto o gancho segura a edição em voo (menor que o prazo do Telegram de 8 s)
SEND_METHODS = ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument")
EDIT_METHODS = ("editMessageText", "editMessageReplyMarkup")
PACED_METHODS = SEND_METHODS + EDIT_METHODS
EDIT_OPERATIONS = ("edit_text", "edit_markup")  # outbox_messages.operation das edições
MC_LABELS = ["⬜ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)", "Enviar", "Não sei"]
LONG_STATEMENT = ("Leia o texto e responda com uma palavra. " * 800)[:32_200]  # ASCII: 1 byte cada
EURO_ANSWER = "€" * 250  # 1 unidade UTF-16 e 3 bytes UTF-8 por caractere

DELIVERY_EVENTS = ("delivery.delivered", "delivery.not_delivered", "delivery.question_failed",
                      "delivery.dont_know")

_codes = itertools.count(1)
_people = itertools.count(1)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any, bot_settings: Any) -> None:
    """As esperas deste arquivo dependem do `settings` e do `bot_settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (30, 3, 2, 60)
    assert (bot_settings.poll_timeout_seconds, bot_settings.telegram_timeout_seconds,
            bot_settings.renew_seconds, bot_settings.lease_seconds) == (1, 2, 10, 30)
    assert (DeliverySettings().chat_interval_seconds, DeliverySettings().bot_per_second) == (
        CHAT_INTERVAL, BOT_PER_SECOND)


# ---------------------------------------------------------------------------------------------
# Apoio (o arnês de test_engine_failures.py, com dois workers e o prazo do Telegram configurável)


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


class _World:
    def __init__(self, *, client: Any, sql: psycopg.Connection, clock: Any, settings: Any, bot_settings: Any,
                 rich: Any, engine_fake: Any, repo_fetcher: Any, start: Any) -> None:
        self.client = client
        self.sql = sql
        self.clock = clock
        self.settings = settings
        self.bot_settings = bot_settings
        self.rich = rich
        self.engine_fake = engine_fake
        self.fetcher = repo_fetcher
        self._start = start
        self._engines: list[HttpQuestionEngine] = []
        self.engine = self.new_engine()
        self.threads: list[Any] = []
        self._last: list[dict[str, Any]] = []

    def new_engine(self) -> HttpQuestionEngine:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url,
                                                   timeout_seconds=ENGINE_TIMEOUT))
        self._engines.append(engine)
        return engine

    def offering(self) -> _Offering:
        code = f"falhas-telegram-{next(_codes)}"
        body = create_offering(self.client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
        bot = self.rich.add_bot()
        register_bot(self.client, ADMIN_TOKEN, code, self.rich.token_of(bot))
        activate_bot(self.client, ADMIN_TOKEN, code)
        row_id, bot_row = self.sql.execute(
            "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.code = %s",
            (code,)).fetchone()
        return _Offering(code=code, offering_id=body["offering_id"], row_id=row_id, bot=bot, bot_row=bot_row)

    def student(self, off: _Offering) -> _Student:
        """Aluno vinculado (ver `test_engine_failures.py`): pessoa pela API base, matrícula e endereço por SQL."""
        n = next(_people)
        user = make_user(n)
        body = register_person(self.client, ADMIN_TOKEN, name=f"Pessoa Aluna Falhas Telegram {n:02d}")
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
        """Endereço `active` no bot; o chat privado passa a existir na Bot API falsa por um update fora
        do `allowed_updates` (descartado na geração; nada chega ao worker)."""
        now = self.clock.now()
        row = self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
            "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, off.bot_row, user.user_id, now, now)).fetchone()[0]
        dropped = self.rich.push_update(off.bot, {"message_reaction": {
            "chat": user.as_private_chat(), "message_id": 1, "user": user.as_user(), "date": 0,
            "old_reaction": [], "new_reaction": [{"type": "emoji", "emoji": "👍"}]}})
        assert dropped is None
        return row

    def switch_account(self, off: _Offering, student: _Student, new_user: TelegramUser | None) -> int | None:
        """Troca de conta (o que o novo vínculo do aluno deixa): o endereço antigo fica `inactive` e, com
        `new_user`, um endereço novo `active` no chat dele."""
        now = self.clock.now()
        self.sql.execute("UPDATE delivery_endpoints SET state = 'inactive', changed_at = %s WHERE id = %s",
                         (now, student.endpoint_row))
        return None if new_user is None else self.endpoint(off, student.person_row, new_user)

    # -- worker ----------------------------------------------------------------------------

    def start_worker(self, off: _Offering, *, worker_id: str = WORKER_ID, delivery: DeliverySettings | None = None,
                     bot_settings: Any = None, wait_polls: bool = True) -> Any:
        kwargs: dict[str, Any] = {"settings": dataclasses.replace(self.settings, worker_id=worker_id),
                                  "engine": self.engine, "fetcher": self.fetcher,
                                  "delivery": delivery if delivery is not None else DeliverySettings()}
        if bot_settings is not None:
            kwargs["bot_settings"] = bot_settings
        thread = self._start(**kwargs)
        self.threads.append(thread)
        self._last.append({"off": off, "worker_id": worker_id, "delivery": delivery, "bot_settings": bot_settings})
        if wait_polls:
            self.rich.wait_for(lambda: self.rich.pending_polls(off.bot), timeout=TIMEOUT)
        return thread

    def stop_workers(self) -> None:
        for thread in self.threads:
            if thread.is_alive():
                assert self._start.stop_one(thread) < 10.0, "o worker não parou em 10 s"
        self.threads = [t for t in self.threads if t.is_alive()]

    def restart(self) -> None:
        configs, self._last = self._last, []
        for n, config in enumerate(configs):
            self.start_worker(config["off"], worker_id=config["worker_id"], delivery=config["delivery"],
                              bot_settings=config["bot_settings"], wait_polls=n == len(configs) - 1)

    def close(self) -> None:
        self._start.stop()
        for engine in self._engines:
            with contextlib.suppress(Exception):
                engine.close()

    # -- relógio ---------------------------------------------------------------------------

    def _wait_leases(self) -> None:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            now = self.clock.now()
            rows = self.sql.execute("SELECT lease_until FROM bots WHERE lease_owner IS NOT NULL").fetchall()
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

    def validated(self, off: _Offering, qid: str, question: Any = None, *, closes_in: timedelta = DAY) -> dict:
        self.engine_fake.program((REPO, COMMIT, qid), question if question is not None else qid)
        dispatch = prepare_dispatch(self.client, ADMIN_TOKEN, off.code, repo=REPO, commit=COMMIT, qid=qid,
                                    closes_at=iso(self.clock.now() + closes_in))
        self.wait_dispatch(dispatch, "scheduled")
        return dispatch

    def confirm(self, off: _Offering, dispatch: dict, *, wait_open: bool = True) -> dict:
        confirmed = confirm_dispatch(self.client, ADMIN_TOKEN, off.code, dispatch["dispatch_id"])
        if wait_open:
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

    def close_dispatch(self, dispatch: dict) -> dict:
        self.jump_past(_parse(dispatch["closes_at"]))
        return self.wait_closed(dispatch)

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

    def delivery_messages(self, delivery: dict) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                "SELECT id, delivery_role, operation, state, attempts, channel_ref, render_version, text "
                "FROM outbox_messages WHERE delivery_id = %s ORDER BY id", (delivery["id"],)).fetchall()

    def tasks(self, task_type: str) -> list[dict]:
        with self.sql.cursor(row_factory=dict_row) as cur:
            return cur.execute("SELECT * FROM tasks WHERE task_type = %s ORDER BY id", (task_type,)).fetchall()

    def edit_rows(self, delivery: dict) -> list[dict]:
        return [m for m in self.delivery_messages(delivery) if m["operation"] in ("edit_text", "edit_markup")]

    def endpoint_state(self, row: int) -> str:
        return self.sql.execute("SELECT state FROM delivery_endpoints WHERE id = %s", (row,)).fetchone()[0]

    def principal(self, off: _Offering, student: _Student, delivery: dict, *, chat: int | None = None) -> Any:
        row = self.sql.execute(
            "SELECT o.channel_ref FROM deliveries d JOIN outbox_messages o ON o.id = d.principal_message_id "
            "WHERE d.id = %s", (delivery["id"],)).fetchone()
        assert row is not None and row[0] is not None, "a entrega não tem mensagem principal"
        return self.rich.current(off.bot, student.chat if chat is None else chat, int(row[0]))

    def prompts(self, off: _Offering, student: _Student) -> list[Any]:
        return [m for m in self.rich.messages(off.bot, student.chat) if m.force_reply]

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

    def calls_to(self, chat: int, *, since: int = 0, methods: tuple[str, ...] = PACED_METHODS) -> list[Any]:
        """As chamadas de envio e de edição ao `chat` com `seq > since`."""
        return [c for c in self.rich.calls() if c.seq > since and c.method in methods
                and str(c.params.get("chat_id")) == str(chat)]

    def last_seq(self) -> int:
        calls = self.rich.calls()
        return calls[-1].seq if calls else 0

    def events(self, *types: str) -> list[tuple[str, dict]]:
        return [(e["type"], e["data"]) for e in all_events(self.client, ADMIN_TOKEN, *types)]


@pytest.fixture
def world(client: Any, sql: psycopg.Connection, clock: Any, settings: Any, bot_settings: Any,
          rich_bot_api: Any, engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any) -> Iterator[_World]:
    w = _World(client=client, sql=sql, clock=clock, settings=settings, bot_settings=bot_settings,
               rich=rich_bot_api, engine_fake=engine_fake, repo_fetcher=repo_fetcher, start=bot_worker_thread)
    try:
        yield w
    finally:
        w.close()


def _data(message: Any, action: str, key: str | None = None) -> str:
    """O `callback_data` do botão da ação (e da chave) no teclado corrente da mensagem
    (`cq1:<token>:<ação>:<campo>:<versão>[:<chave>]`)."""
    for button in message.buttons():
        parts = (button.data or "").split(":")
        if len(parts) >= 5 and parts[0] == "cq1" and parts[2] == action and (key is None or parts[5:] == [key]):
            return button.data
    raise AssertionError(f"sem botão {action}/{key} no teclado: {[b.label for b in message.buttons()]}")


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


def _assert_pace(rich: Any, bot: int) -> None:
    """por chat, chamadas de envio e de edição separadas por pelo menos 1 s; por bot, no máximo
    30 em qualquer janela de 1 s (medidas no `started` da Bot API falsa; folga só de medição)."""
    calls = [c for c in rich.calls(bot_id=bot) if c.method in PACED_METHODS]
    by_chat: dict[str, list[float]] = {}
    for call in calls:
        by_chat.setdefault(str(call.params.get("chat_id")), []).append(call.started)
    for starts in by_chat.values():
        gaps = [b - a for a, b in itertools.pairwise(starts)]
        assert all(gap >= CHAT_INTERVAL - PACE_TOLERANCE for gap in gaps), (
            f"ritmo por chat acima do limite: {[round(g, 3) for g in gaps]}")
    starts = sorted(c.started for c in calls)
    for i, start in enumerate(starts):
        window = [s for s in starts[i:] if s < start + 1.0 - PACE_TOLERANCE]
        assert len(window) <= BOT_PER_SECOND


def _wait_edit_failure(world: _World, since: int, *, what: str) -> Any:
    return _until(lambda: [c for c in world.rich.calls() if c.seq > since and c.method in EDIT_METHODS
                           and c.outcome == "error"], what=what)[0]


def _wait_requeued(world: _World, dispatch: dict, student: _Student, *, operations: tuple[str, ...],
                   what: str, role: str | None = None) -> None:
    """Espera a mensagem da entrega (`operations`, e o papel `role` se dado) de volta a `pending` depois
    da 1ª tentativa (o worker já gravou o `RetryLater`, com o `available_at` lido do `FakeClock`). Só
    depois o teste avança o relógio:
    o 429/5xx aparece na Bot API falsa antes de o worker gravar o resultado, e um avanço nesse intervalo
    faria o `available_at` sair do relógio já avançado, sem outro avanço que o vencesse (corrida com o
    tempo real)."""
    def ready() -> list[dict] | None:
        delivery = world.delivery_of(dispatch, student)
        if delivery is None:
            return None
        return [m for m in world.delivery_messages(delivery) if m["operation"] in operations
                and (role is None or m["delivery_role"] == role)
                and (m["state"], m["attempts"]) == ("pending", 1)] or None
    _until(ready, what=what)


def _two_worker_settings(world: _World) -> Any:
    """Prazo do Telegram de 8 s (30 − 10 ≥ 1 + 8) para o gancho segurar a edição em voo."""
    return dataclasses.replace(world.bot_settings, telegram_timeout_seconds=8)


def _hold_first_edit(world: _World) -> tuple[threading.Event, threading.Event]:
    """Gancho na Bot API falsa: a primeira edição que chega fica presa (em voo, `leased`) até o teste
    soltar `release` (no máximo `HOOK_WAIT`); `entered` avisa que ela chegou."""
    entered, release = threading.Event(), threading.Event()

    def hook(call: Any) -> None:
        if not entered.is_set():
            entered.set()
            release.wait(HOOK_WAIT)

    for method in EDIT_METHODS:
        world.rich.on_call(method, hook)
    return entered, release


# ---------------------------------------------------------------------------------------------
# 429 com `retry_after` e 5xx


def test_429_on_the_question_waits_at_least_retry_after_and_keeps_the_pace(world: _World) -> None:
    """A mensagem da questão recebe 429 com `retry_after`; a nova tentativa só
    chega depois de pelo menos o indicado (o `penalize` do ritmo), a entrega sai, e o ritmo nunca passa
    de 1 mensagem por segundo por chat nem de 30 por segundo por bot."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.rich.fail("sendRichMessage", retry_after=RETRY_AFTER, times=1)
    world.confirm(off, dispatch)
    _until(lambda: [c for c in world.rich.calls("sendRichMessage") if c.outcome == "error"], what="o 429")
    _wait_requeued(world, dispatch, alice, operations=("send",), role="question",
                   what="a questão de volta à fila")
    world.advance(STEP)  # vence a espera da fila (FakeClock); o ritmo segura pelo `retry_after`
    world.wait_delivery(dispatch, alice, "delivered")

    sends = world.rich.calls("sendRichMessage")
    assert [c.outcome for c in sends[:2]] == ["error", "ok"]
    assert sends[1].started - sends[0].ended >= RETRY_AFTER - PACE_TOLERANCE
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    _assert_pace(world.rich, off.bot)


@pytest.mark.parametrize("failure", ["429", "5xx"])
def test_429_and_5xx_on_an_edit_are_retried_and_the_edit_is_applied(world: _World, failure: str) -> None:
    """429 (com `retry_after`) e 5xx numa edição da mensagem da questão são
    repetidos com espera; a edição sai, com o feedback, e o ritmo é mantido."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    since = world.last_seq()
    if failure == "429":
        world.rich.fail("*", retry_after=RETRY_AFTER, times=1, when=lambda c: c.method in EDIT_METHODS)
    else:
        world.rich.fail("*", status=502, times=1, when=lambda c: c.method in EDIT_METHODS)
    world.answer(off, alice, delivery)
    failed = _wait_edit_failure(world, since, what="a edição que falha")
    _wait_requeued(world, dispatch, alice, operations=EDIT_OPERATIONS, what="a edição de volta à fila")
    world.advance(STEP)

    delivery = world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.principal(off, alice, delivery).plain_text or ""),
           what="o feedback aplicado")
    later = [c for c in world.rich.calls() if c.seq > failed.seq and c.method in EDIT_METHODS]
    assert later and later[0].outcome == "ok"
    if failure == "429":
        assert later[0].started - failed.ended >= RETRY_AFTER - PACE_TOLERANCE
    assert [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"] == []
    _assert_pace(world.rich, off.bot)


def test_5xx_on_the_question_is_retried_and_delivered_once(world: _World) -> None:
    """Um 5xx (sem efeito) na mensagem da questão é repetido; a entrega sai com uma só
    mensagem da questão."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.rich.fail("sendRichMessage", status=502, times=1)
    world.confirm(off, dispatch)
    _until(lambda: [c for c in world.rich.calls("sendRichMessage") if c.outcome == "error"], what="o 5xx")
    _wait_requeued(world, dispatch, alice, operations=("send",), role="question",
                   what="a questão de volta à fila")
    world.advance(STEP)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    questions = [m for m in world.rich.messages(off.bot, alice.chat) if m.buttons()]
    assert [m.message_id for m in questions] == [world.principal(off, alice, delivery).message_id]
    assert [c.outcome for c in world.rich.calls("sendRichMessage")][:2] == ["error", "ok"]


def test_duplicated_question_after_a_5xx_with_effect_and_the_second_answer_gets_already_answered(
        world: _World) -> None:
    """Com `fail("sendRichMessage", status=502, after_effect=True)`, a
    mensagem da questão fica duplicada (aceitável); a resposta pela mensagem principal (marcar e
    "Enviar") vale, e a segunda, pela cópia (a marcação de outra alternativa), recebe "você já
    respondeu" e é gravada como recusa `already_answered`; uma só submissão válida."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "single_choice")
    world.rich.fail("sendRichMessage", status=502, times=1, after_effect=True)
    world.confirm(off, dispatch)
    _until(lambda: [c for c in world.rich.calls("sendRichMessage") if c.outcome == "error"], what="o 5xx")
    _wait_requeued(world, dispatch, alice, operations=("send",), role="question",
                   what="a questão de volta à fila")
    world.advance(STEP)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    principal = world.principal(off, alice, delivery)
    copies = [m for m in world.rich.messages(off.bot, alice.chat) if m.buttons()]
    assert len(copies) == 2, "a mensagem da questão deveria estar duplicada"
    [copy] = [m for m in copies if m.message_id != principal.message_id]
    assert _labels(copy) == _labels(principal)

    # A resposta pela principal: marcar `(b)` e "Enviar".
    mark = world.rich.tap(off.bot, alice.user, principal.message_id, _data(principal, "m", "b"))
    assert world.update_row(off, mark.update_id)["outcome"] == "answer_toggled"
    assert world.submissions(delivery) == []
    world.rich.tap(off.bot, alice.user, principal.message_id, _data(principal, "s"))
    [valid] = world.wait_submissions(delivery, 1)
    assert (valid["outcome"], valid["valid"], valid["score"]) == ("graded", True, 1.0)
    world.wait_delivery(dispatch, alice, "answered")

    # A segunda, pela cópia: a marcação `(a)` recebe "você já respondeu" (a recusa vem antes da ação).
    tap = world.rich.tap(off.bot, alice.user, copy.message_id, _data(copy, "m", "a"))
    submissions = world.wait_submissions(delivery, 2)
    assert (submissions[1]["outcome"], submissions[1]["refusal"], submissions[1]["valid"]) == (
        "refused", "already_answered", None)
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_refused"
    answer = _until(lambda: [a for a in world.rich.answers(off.bot) if a.callback_id == tap.callback_id],
                    what="a confirmação do segundo toque")
    assert [a.text for a in answer] == [deliverytexts.ALREADY_ANSWERED]
    valid_rows = [s for s in submissions if s["outcome"] == "graded" and s["valid"]]
    assert len(valid_rows) == 1
    assert world.delivery_of(dispatch, alice)["state"] == "answered"


# ---------------------------------------------------------------------------------------------
# 403 de bloqueio


def test_403_at_opening_blocks_the_endpoint_and_leaves_the_delivery_not_delivered(world: _World) -> None:
    """O aluno bloqueou o bot; o 403 no envio da questão bloqueia o endereço
    e deixa a entrega `not_delivered/endpoint_blocked`, com o evento; as mensagens pendentes da
    entrega são canceladas, e nada mais é chamado para o chat."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.rich.block_chat(off.bot, alice.user)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "not_delivered")

    assert (delivery["undelivered_reason"], delivery["current_field"], delivery["sent_at"],
            delivery["principal_message_id"]) == ("endpoint_blocked", None, None, None)
    assert _until(lambda: world.endpoint_state(alice.endpoint_row) == "blocked", what="o endereço bloqueado")
    rows = world.delivery_messages(delivery)
    assert rows[0]["delivery_role"] == "question" and rows[0]["state"] == "failed"
    assert all(r["state"] == "cancelled" for r in rows[1:])
    assert [c.method for c in world.calls_to(alice.chat)] == ["sendRichMessage"]
    assert world.rich.messages(off.bot, alice.chat) == []
    assert sorted(world.events("endpoint.blocked", *DELIVERY_EVENTS), key=lambda e: e[0]) == [
        ("delivery.not_delivered", _delivery_event(off, dispatch, delivery, alice,
                                                   undelivered_reason="endpoint_blocked")),
        ("endpoint.blocked", {"offering_id": off.offering_id, "person": alice.pseudo_id, "channel": "telegram"}),
    ]


def test_403_on_an_edit_and_edits_to_an_already_blocked_endpoint_are_only_recorded(world: _World) -> None:
    """O aluno bloqueia o bot depois de receber a questão; a resposta
    dele é gravada e corrigida (nada se perde), a edição do feedback recebe 403 e só é registrada (o
    endereço fica bloqueado, sem `fallback`); a revelação no fechamento, para o endereço já `blocked`
    (`reason = "endpoint_blocked"`, sem chamar o Telegram), também só é registrada."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    since = world.last_seq()
    world.rich.block_chat(off.bot, alice.user)
    world.answer(off, alice, delivery)

    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"]) == ("graded", True)
    world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: world.endpoint_state(alice.endpoint_row) == "blocked", what="o endereço bloqueado")
    after_block = world.calls_to(alice.chat, since=since)
    assert [c.method for c in after_block if c.method in SEND_METHODS] == []
    assert [(c.method in EDIT_METHODS, c.outcome) for c in after_block] == [(True, "error")]

    world.close_dispatch(dispatch)
    delivery = _until(lambda: (d := world.delivery_of(dispatch, alice))["revealed_at"] and d,
                      what="a revelação registrada")
    assert delivery["state"] == "answered"
    time.sleep(1.0)  # a edição da revelação, se fosse mandada, já teria saído
    assert [(c.method in EDIT_METHODS, c.outcome) for c in world.calls_to(alice.chat, since=since)] == [
        (True, "error")]
    assert [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"] == []
    assert world.rich.edits(off.bot, alice.chat) == []


# ---------------------------------------------------------------------------------------------
# Edição que falha: `fallback` em resposta à original


def test_edit_of_a_deleted_message_falls_back_to_a_reply_that_becomes_the_principal(world: _World) -> None:
    """O aluno toca uma alternativa e apaga a mensagem da questão; a
    edição recebe 400 ("message to edit not found") e vira uma mensagem nova (`fallback`) em resposta à
    original (`allow_sending_without_reply`), com a renderização completa do estado; como leva os
    controles, ela passa a ser a principal, e as edições seguintes se aplicam a ela."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "multiple_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    original = world.principal(off, alice, delivery)
    assert sorted(_labels(original)) == sorted(MC_LABELS)

    world.rich.tap(off.bot, alice.user, original.message_id, _data(original, "m", "a"))
    world.rich.delete_message(off.bot, alice.user, original.message_id)

    def moved() -> Any:
        row = world.delivery_of(dispatch, alice)
        current = world.principal(off, alice, row)
        return current if current.message_id != original.message_id else None

    fallback = _until(moved, what="o fallback como nova principal")
    assert fallback.reply_parameters is not None
    assert (fallback.reply_parameters["message_id"], fallback.reply_parameters["allow_sending_without_reply"]) == (
        original.message_id, True)
    assert "Marque os números primos." in (fallback.plain_text or "")
    assert sorted(_labels(fallback)) == sorted(["✅ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)", "Enviar", "Não sei"])
    fallback_rows = [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]
    assert [(m["operation"], m["state"], m["channel_ref"]) for m in fallback_rows] == [
        ("send", "sent", str(fallback.message_id))]

    world.rich.tap(off.bot, alice.user, fallback.message_id, _data(fallback, "m", "c"))
    toggled = _until(lambda: (m := world.rich.current(off.bot, alice.chat, fallback.message_id))
                     and "✅ (c)" in _labels(m) and m, what="a edição aplicada ao fallback")
    assert sorted(_labels(toggled)) == sorted(["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)", "Enviar", "Não sei"])
    world.rich.tap(off.bot, alice.user, fallback.message_id, _data(toggled, "s"))
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    delivery = world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.rich.current(off.bot, alice.chat,
                                                                          fallback.message_id).plain_text or ""),
           what="o feedback no fallback")
    assert world.rich.edits(off.bot, alice.chat, original.message_id) == []
    assert len([m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]) == 1


def _selection(world: _World, delivery: dict) -> Any:
    """A seleção em andamento da entrega (`delivery_contents.selection`)."""
    row = world.sql.execute("SELECT selection FROM delivery_contents WHERE delivery_id = %s",
                            (delivery["id"],)).fetchone()
    return None if row is None else row[0]


def _delete_on_first_edit(world: _World, off: _Offering, student: _Student, message_id: int) -> None:
    """Gancho na Bot API falsa: a primeira edição da mensagem `message_id` que chega apaga a mensagem
    antes de ser processada (o aluno a apagou entre o toque e a edição), e a edição recebe o 400
    "message to edit not found" do próprio dublê, sem corrida com o tempo real."""
    deleted = threading.Event()

    def hook(call: Any) -> None:
        if deleted.is_set() or str(call.params.get("message_id")) != str(message_id):
            return
        deleted.set()
        world.rich.delete_message(off.bot, student.user, message_id)

    for method in EDIT_METHODS:
        world.rich.on_call(method, hook)


def test_single_choice_mark_edit_that_fails_for_good_falls_back_to_a_reply_that_becomes_the_principal(
        world: _World) -> None:
    """Edição que falha (na alternância ou na marcação, com a escolha única confirmada):
    na escolha única, o aluno marca
    `(b)` e a mensagem da questão é apagada antes da edição de marcação (gancho determinístico na Bot
    API falsa); a edição recebe 400 "message to edit not found" e vira uma mensagem nova (`fallback`) em
    resposta à original (`allow_sending_without_reply`), com a renderização completa do estado (o
    enunciado e o teclado `(a)`, `✅ (b)`, `(c)`, `(d)`, "Enviar", "Não sei"); como leva os controles, ela
    passa a ser a principal, a marcação não gera submissão, e as edições seguintes (a troca para `(c)`,
    a volta para `(b)` e o feedback depois do "Enviar") se aplicam a ela; um único `fallback`.

    Na escolha única, o toque só marca, e a resposta sai no "Enviar"."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "single_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    original = world.principal(off, alice, delivery)
    assert _labels(original) == ["(a)", "(b)", "(c)", "(d)", "Enviar", "Não sei"], (
        f"o teclado da única deveria marcar e ter o 'Enviar': {_labels(original)}")
    since = world.last_seq()
    _delete_on_first_edit(world, off, alice, original.message_id)

    mark = world.rich.tap(off.bot, alice.user, original.message_id, _data(original, "m", "b"))
    assert world.update_row(off, mark.update_id)["outcome"] == "answer_toggled"

    def moved() -> Any:
        current = world.principal(off, alice, world.delivery_of(dispatch, alice))
        return current if current.message_id != original.message_id else None

    fallback = _until(moved, what="o fallback como nova principal")
    for method in EDIT_METHODS:
        world.rich.on_call(method, None)
    failed = [c for c in world.calls_to(alice.chat, since=since, methods=EDIT_METHODS)
              if str(c.params.get("message_id")) == str(original.message_id)]
    assert [(c.outcome, c.status, c.description) for c in failed] == [
        ("error", *DISPATCH_ERRORS["edit_not_found"])]
    assert fallback.reply_parameters is not None
    assert (fallback.reply_parameters["message_id"], fallback.reply_parameters["allow_sending_without_reply"]) == (
        original.message_id, True)
    assert "Qual é a cor do céu num dia limpo?" in (fallback.plain_text or "")
    assert _labels(fallback) == ["(a)", "✅ (b)", "(c)", "(d)", "Enviar", "Não sei"]
    fallback_rows = [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]
    assert [(m["operation"], m["state"], m["channel_ref"]) for m in fallback_rows] == [
        ("send", "sent", str(fallback.message_id))]
    assert _until(lambda: [m["state"] for m in world.edit_rows(delivery)] == ["failed"],
                  what="a edição de marcação registrada como falha definitiva")
    marked = world.delivery_of(dispatch, alice)
    assert (marked["state"], marked["current_field"], _selection(world, delivery)) == ("delivered", 0, ["b"])
    assert world.submissions(delivery) == []

    # As edições seguintes se aplicam ao `fallback`, a nova principal.
    world.rich.tap(off.bot, alice.user, fallback.message_id, _data(fallback, "m", "c"))
    swapped = _until(lambda: (m := world.rich.current(off.bot, alice.chat, fallback.message_id))
                     and "✅ (c)" in _labels(m) and m, what="a troca aplicada ao fallback")
    assert _labels(swapped) == ["(a)", "(b)", "✅ (c)", "(d)", "Enviar", "Não sei"]
    world.rich.tap(off.bot, alice.user, fallback.message_id, _data(swapped, "m", "b"))
    back = _until(lambda: (m := world.rich.current(off.bot, alice.chat, fallback.message_id))
                  and "✅ (b)" in _labels(m) and m, what="a volta para (b) aplicada ao fallback")
    assert _labels(back) == ["(a)", "✅ (b)", "(c)", "(d)", "Enviar", "Não sei"]
    assert world.submissions(delivery) == []
    world.rich.tap(off.bot, alice.user, fallback.message_id, _data(back, "s"))
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    delivery = world.wait_delivery(dispatch, alice, "answered")
    _until(lambda: deliverytexts.VERDICT_CORRECT in (world.rich.current(off.bot, alice.chat,
                                                                          fallback.message_id).plain_text or ""),
           what="o feedback no fallback")
    assert world.rich.edits(off.bot, alice.chat, original.message_id) == []
    assert len([m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]) == 1
    assert world.principal(off, alice, world.delivery_of(dispatch, alice)).message_id == fallback.message_id


def test_a_failed_edit_superseded_by_a_newer_one_applies_only_the_newest(world: _World) -> None:
    """A edição da primeira alternância falha (5xx) e volta à fila; a segunda
    alternância se funde a ela; só a mais nova é aplicada, e o estado mostrado nunca regride."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "multiple_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    question = world.principal(off, alice, delivery)
    since = world.last_seq()
    world.rich.fail("*", status=502, times=1, when=lambda c: c.method in EDIT_METHODS)

    world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "a"))
    _wait_edit_failure(world, since, what="a primeira edição com 5xx")
    _until(lambda: [m for m in world.edit_rows(delivery) if m["state"] == "pending" and m["attempts"] == 1],
           what="a edição de volta à fila")
    tap = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "c"))
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_toggled"
    world.advance(STEP)

    applied = _until(lambda: world.rich.edits(off.bot, alice.chat, question.message_id),
                     what="a edição aplicada")
    time.sleep(1.5)  # nenhuma outra edição da mesma mensagem pode chegar depois
    applied = world.rich.edits(off.bot, alice.chat, question.message_id)
    assert len(applied) == 1
    assert sorted(_labels(applied[0].after)) == sorted(["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)", "Enviar", "Não sei"])
    rows = world.edit_rows(delivery)
    assert [m["state"] for m in rows] == ["sent"]
    assert [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"] == []


def test_in_flight_edit_absorbs_the_pending_one_on_5xx_without_fallback(world: _World) -> None:
    """A primeira edição está em voo (`leased`) quando a segunda alternância cria
    outra edição `pending` da mesma mensagem; a em voo recebe 5xx e absorve a pendente (que vai a
    `cancelled`), volta à fila com a versão mais nova e sai uma única vez, sem `fallback`."""
    off = world.offering()
    alice = world.student(off)
    slow_telegram = _two_worker_settings(world)
    world.start_worker(off, worker_id="worker-falhas-telegram-1", bot_settings=slow_telegram)
    world.start_worker(off, worker_id="worker-falhas-telegram-2", bot_settings=slow_telegram)
    dispatch = world.validated(off, "multiple_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    question = world.principal(off, alice, delivery)
    entered, release = _hold_first_edit(world)
    world.rich.fail("*", status=502, times=1, when=lambda c: c.method in EDIT_METHODS)

    world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "a"))
    assert entered.wait(TIMEOUT), "a primeira edição não chegou à Bot API falsa"
    tap = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "c"))
    _until(lambda: [m["state"] for m in world.edit_rows(delivery)] == ["leased", "pending"],
           what="uma edição em voo e outra pendente", timeout=HOOK_WAIT - 1)
    release.set()
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_toggled"
    _until(lambda: [m["state"] for m in world.edit_rows(delivery)] == ["pending", "cancelled"],
           what="a pendente absorvida pela em voo")
    world.advance(STEP)

    _until(lambda: [m["state"] for m in world.edit_rows(delivery)] == ["sent", "cancelled"],
           what="a edição absorvente enviada")
    applied = world.rich.edits(off.bot, alice.chat, question.message_id)
    assert len(applied) == 1
    assert sorted(_labels(applied[0].after)) == sorted(["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)", "Enviar", "Não sei"])
    assert [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"] == []
    rows = world.edit_rows(delivery)
    assert rows[0]["render_version"] >= rows[1]["render_version"]


def test_in_flight_edit_that_fails_for_good_with_a_pending_edit_makes_a_single_fallback(world: _World) -> None:
    """A edição E1 está em voo quando a alternância seguinte cria a E2 `pending`
    da mesma mensagem; o aluno apaga a mensagem e E1 falha de vez (400); sai um único `fallback`, a E2
    vai a `cancelled`, e o `fallback` mostra a renderização mais nova (as duas marcas)."""
    off = world.offering()
    alice = world.student(off)
    slow_telegram = _two_worker_settings(world)
    world.start_worker(off, worker_id="worker-falhas-telegram-1", bot_settings=slow_telegram)
    world.start_worker(off, worker_id="worker-falhas-telegram-2", bot_settings=slow_telegram)
    dispatch = world.validated(off, "multiple_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    question = world.principal(off, alice, delivery)
    entered, release = _hold_first_edit(world)

    world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "a"))
    assert entered.wait(TIMEOUT), "a primeira edição não chegou à Bot API falsa"
    tap = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "c"))
    _until(lambda: [m["state"] for m in world.edit_rows(delivery)] == ["leased", "pending"],
           what="uma edição em voo e outra pendente", timeout=HOOK_WAIT - 1)
    world.rich.delete_message(off.bot, alice.user, question.message_id)
    release.set()
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_toggled"

    fallback = _until(lambda: (m := world.principal(off, alice, world.delivery_of(dispatch, alice)))
                      and m.message_id != question.message_id and m, what="o fallback")
    time.sleep(1.5)  # nenhum segundo fallback pode chegar
    fallbacks = [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]
    assert [(m["state"], m["channel_ref"]) for m in fallbacks] == [("sent", str(fallback.message_id))]
    assert [m["state"] for m in world.edit_rows(delivery)] == ["failed", "cancelled"]
    assert sorted(_labels(fallback)) == sorted(["✅ (a)", "⬜ (b)", "✅ (c)", "⬜ (d)", "Enviar", "Não sei"])
    assert fallback.reply_parameters is not None and fallback.reply_parameters["message_id"] == question.message_id
    assert [m.message_id for m in world.rich.messages(off.bot, alice.chat) if m.buttons()] == [
        question.message_id, fallback.message_id]


def test_a_fallback_that_fails_for_good_is_only_recorded(world: _World) -> None:
    """A edição da mensagem apagada vira `fallback`, e o `fallback` recebe 400
    (falha definitiva): só registro, nenhuma mensagem nova nem outro `fallback`; a entrega continua
    `delivered`."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "multiple_choice")
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    question = world.principal(off, alice, delivery)
    since = world.last_seq()
    world.rich.fail("sendRichMessage", status=400, times=1)

    world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "m", "a"))
    world.rich.delete_message(off.bot, alice.user, question.message_id)
    _until(lambda: [m for m in world.delivery_messages(delivery)
                    if m["delivery_role"] == "fallback" and m["state"] == "failed"],
           what="o fallback que falha de vez")
    time.sleep(1.5)
    sends = world.calls_to(alice.chat, since=since, methods=SEND_METHODS)
    assert [(c.method, c.outcome) for c in sends] == [("sendRichMessage", "error")]
    assert len([m for m in world.delivery_messages(delivery) if m["delivery_role"] == "fallback"]) == 1
    assert [m.message_id for m in world.rich.messages(off.bot, alice.chat)] == [question.message_id]
    row = world.delivery_of(dispatch, alice)
    assert row["state"] == "delivered"
    assert world.principal(off, alice, row).message_id == question.message_id


def test_a_notice_that_fails_for_good_is_only_recorded(world: _World) -> None:
    """O feedback não cabe na mensagem da questão (enunciado longo e eco de
    200 caracteres de 3 bytes, no Rich medido em bytes UTF-8); a edição sai sem o bloco de feedback, e
    o `notice` curto, em resposta à original, recebe 400 (falha definitiva): só registro, sem nova
    mensagem; a entrega fica `answered`."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    assert LONG_STATEMENT.isascii(), "o enunciado longo é calibrado em 1 byte UTF-8 por caractere"
    question_def = preset("text", title="Texto longo", statement=LONG_STATEMENT)
    dispatch = world.validated(off, "text", question_def)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    since = world.last_seq()

    def is_notice(call: Any) -> bool:
        markup = str(call.params.get("reply_markup") or "")
        return call.params.get("reply_parameters") is not None and "force_reply" not in markup

    for method in ("sendRichMessage", "sendMessage"):
        world.rich.fail(method, status=400, times=1, when=is_notice)
    world.answer(off, alice, delivery, EURO_ANSWER)
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["raw_input"]) == ("graded", True, {"resposta": EURO_ANSWER})
    delivery = world.wait_delivery(dispatch, alice, "answered")
    notices = _until(lambda: [m for m in world.delivery_messages(delivery)
                              if m["delivery_role"] == "notice" and m["state"] in ("failed", "sent")],
                     what="o notice decidido")
    world.rich.clear_failures()
    assert [(m["operation"], m["state"]) for m in notices] == [("send", "failed")]
    time.sleep(1.5)
    sends = world.calls_to(alice.chat, since=since, methods=SEND_METHODS)
    assert [c.outcome for c in sends] == ["error"]
    edited = world.principal(off, alice, delivery)
    assert EURO_ANSWER[:200] not in (edited.plain_text or "")
    assert edited.buttons() == []
    assert len([m for m in world.delivery_messages(delivery) if m["delivery_role"] == "notice"]) == 1


# ---------------------------------------------------------------------------------------------
# Troca de conta e endereço vigente


def test_account_switch_while_open_uses_the_single_typed_field_rule_and_the_current_endpoint(
        world: _World) -> None:
    """O aluno troca de conta com o disparo
    aberto; um toque no chat antigo é "sem efeito" (`answer_ignored`, confirmado, sem submissão); um
    texto sem "responder" no chat novo, com um único campo digitável aberto, vale; o feedback e, no
    fechamento, o gabarito saem como mensagem nova no endereço vigente (sem `reply_parameters`, porque
    o chat mudou), e nada mais vai ao chat antigo."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    question = world.principal(off, alice, delivery)
    new_user = make_user(9000 + next(_people))
    world.switch_account(off, alice, new_user)
    since = world.last_seq()

    tap = world.rich.tap(off.bot, alice.user, question.message_id, _data(question, "n"))
    assert world.update_row(off, tap.update_id)["outcome"] == "answer_ignored"
    _until(lambda: [a for a in world.rich.answers(off.bot) if a.callback_id == tap.callback_id],
           what="a confirmação do toque no chat antigo")
    assert world.submissions(delivery) == []

    update_id = world.rich.push_text(off.bot, new_user, NUMBER_ANSWER)
    assert world.update_row(off, update_id)["outcome"] == "answer_recorded"
    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    world.wait_delivery(dispatch, alice, "answered")
    feedback = _until(lambda: [m for m in world.rich.messages(off.bot, new_user.user_id)
                               if deliverytexts.VERDICT_CORRECT in (m.plain_text or "")],
                      what="o feedback no endereço vigente")
    assert feedback[0].reply_parameters is None

    world.close_dispatch(dispatch)
    _until(lambda: [m for m in world.rich.messages(off.bot, new_user.user_id)
                    if ANSWER_SENTINEL in (m.plain_text or "")], what="o gabarito no endereço vigente")
    revealed = [m for m in world.rich.messages(off.bot, new_user.user_id) if ANSWER_SENTINEL in (m.plain_text or "")]
    assert deliverytexts.CLOSED_NOTICE in (revealed[-1].plain_text or "")
    # O gabarito chega à Bot API falsa antes de o worker gravar o resultado do envio (a linha segue
    # `leased` até a resposta HTTP voltar): espera-se o registro, não um estado em voo.
    fallbacks = _until(lambda: (rows := [m for m in world.delivery_messages(delivery)
                                         if m["delivery_role"] == "fallback"])
                       and all(m["state"] not in ("pending", "leased") for m in rows) and rows,
                       what="o registro do envio dos fallbacks")
    assert [c.method for c in world.calls_to(alice.chat, since=since)] == []
    assert len(fallbacks) >= 2 and all(m["state"] == "sent" for m in fallbacks)


def test_async_messages_without_a_current_endpoint_are_only_recorded(world: _World) -> None:
    """A correção sai depois de o endereço do aluno deixar de ser vigente, sem
    endereço novo; o feedback e, no fechamento, o gabarito ficam só registrados (nenhuma chamada ao
    Telegram para o chat), e a resposta não se perde."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number", closes_in=SHORT)
    world.confirm(off, dispatch)
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    _until(lambda: world.prompts(off, alice), what="o pedido do campo")
    world.engine_fake.stop()
    world.answer(off, alice, delivery)
    grade = _until(lambda: [t for t in world.tasks("submission.grade")
                            if t["state"] == "pending" and t["attempts"] >= 1], what="a correção adiada")[0]
    _until(lambda: _appearances(world.rich, off.bot, alice.chat, deliverytexts.GRADING_DELAYED) == 1,
           what="o aviso GRADING_DELAYED")
    world.switch_account(off, alice, None)
    since = world.last_seq()
    world.engine_fake.start()
    world.advance(max(0.0, (grade["available_at"] - world.clock.now()).total_seconds()))

    [graded] = world.wait_submissions(delivery, 1)
    assert (graded["outcome"], graded["valid"], graded["score"]) == ("graded", True, 1.0)
    world.wait_delivery(dispatch, alice, "answered")
    world.close_dispatch(dispatch)
    _until(lambda: world.delivery_of(dispatch, alice)["revealed_at"], what="a revelação registrada")
    time.sleep(1.0)
    assert world.calls_to(alice.chat, since=since) == []
    assert _appearances(world.rich, off.bot, alice.chat, deliverytexts.VERDICT_CORRECT) == 0
    assert _appearances(world.rich, off.bot, alice.chat, ANSWER_SENTINEL) == 0


def test_account_switch_between_opening_and_send_is_channel_refused(world: _World) -> None:
    """A troca de conta acontece entre a abertura (o endereço da entrega
    é o vigente na abertura) e o envio; o canal recusa o endereço que deixou de ser vigente
    (`endpoint_inactive`) e a entrega vira `not_delivered/channel_refused`, sem chamada ao Telegram."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.engine_fake.slow("create_variant", 1.5)
    variants_before = len(world.engine_fake.calls("create_variant"))
    world.confirm(off, dispatch)
    _until(lambda: len(world.engine_fake.calls("create_variant")) > variants_before, what="a variante em curso")
    opened = world.delivery_of(dispatch, alice)
    assert opened["delivery_endpoint_id"] == alice.endpoint_row
    new_user = make_user(9000 + next(_people))
    world.switch_account(off, alice, new_user)

    delivery = world.wait_delivery(dispatch, alice, "not_delivered")
    assert (delivery["undelivered_reason"], delivery["delivery_endpoint_id"]) == ("channel_refused",
                                                                                 alice.endpoint_row)
    assert world.calls_to(alice.chat) == [] and world.calls_to(new_user.user_id) == []
    assert all(m["state"] in ("failed", "cancelled") for m in world.delivery_messages(delivery))
    assert world.events(*DELIVERY_EVENTS) == [
        ("delivery.not_delivered", _delivery_event(off, dispatch, delivery, alice,
                                                   undelivered_reason="channel_refused"))]


# ---------------------------------------------------------------------------------------------
# Bot sem acesso


def test_bot_without_access_keeps_delivery_messages_pending_without_consuming_attempts(world: _World) -> None:
    """Com o bot sem acesso, as mensagens da entrega
    ficam `pending` sem serem tomadas (nenhuma tentativa consumida) e a entrega fica `pending`; uma
    mensagem comum da fila de saída do mesmo bot segue a regra geral (tomada, `RetryLater("bot_no_access")`, tentativa
    consumida); com o acesso de volta, a questão sai na primeira tentativa."""
    off = world.offering()
    alice = world.student(off)
    world.start_worker(off)
    dispatch = world.validated(off, "number")
    world.stop_workers()
    world.confirm(off, dispatch, wait_open=False)
    world.sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected', lease_owner = NULL, "
                      "lease_until = NULL WHERE id = %s", (off.bot_row,))
    outbox = Outbox(world.settings.database_url, clock=world.clock, settings=world.settings)
    try:
        with psycopg.connect(world.settings.database_url) as conn:
            r3_message = outbox.enqueue(conn, recipient=uuid.UUID(alice.pseudo_id), bot_id=off.bot_row,
                                        text="Aviso da equipe do oferecimento de teste.")
    finally:
        outbox.close()
    world.start_worker(off, wait_polls=False)
    world.wait_dispatch(dispatch, "open")
    delivery = _until(lambda: (d := world.delivery_of(dispatch, alice)) and d["variant_at"] and d,
                      what="a variante gravada")
    _until(lambda: world.sql.execute("SELECT attempts FROM outbox_messages WHERE id = %s",
                                     (r3_message,)).fetchone()[0] >= 1, what="a mensagem comum tentada")
    world.advance(STEP)
    time.sleep(1.5)
    rows = world.delivery_messages(delivery)
    assert rows and all((m["state"], m["attempts"]) == ("pending", 0) for m in rows)
    assert world.delivery_of(dispatch, alice)["state"] == "pending"
    assert world.rich.messages(off.bot, alice.chat) == []

    register_bot(world.client, ADMIN_TOKEN, off.code, world.rich.token_of(off.bot))  # o acesso volta
    delivery = world.wait_delivery(dispatch, alice, "delivered")
    [question] = [m for m in world.delivery_messages(delivery) if m["delivery_role"] == "question"]
    assert (question["state"], question["attempts"]) == ("sent", 1)
