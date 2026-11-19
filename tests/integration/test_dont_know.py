""""Não sei" na resposta do aluno (uma resposta válida por disparo). Na escolha
única, a opção é marcada com `m` e enviada com "Enviar", `s`; e vale o "Não sei" atrasado, depois do
fechamento, já que o gabarito só vai a quem já respondeu.

Cobre:
- o toque "Não sei" antes de responder: `dont_know`, submissão `dont_know` sem chamar o motor
  (`raw_input` = as partes já preenchidas), `delivery.dont_know` exato, a edição com
  `DONT_KNOW_RECORDED` e sem controles; toques e textos seguintes → "você já respondeu"
  (`refused/already_answered`, `answer_refused`);
- no meio de uma questão de várias partes: as partes preenchidas ficam gravadas, sem correção;
- depois de responder: "você já respondeu";
- depois do fechamento (o gabarito só vai a quem já respondeu): a edição de fechamento da `no_answer`
  oferece o "Não sei" (botão; no console, o `DONT_KNOW_HOWTO`), com `CLOSED_NOTICE` e `LATE_HOWTO`, sem
  o gabarito; o "Não sei" numa `no_answer` (o botão da edição de fechamento, o botão de antes do
  fechamento, de qualquer versão, e o `/naosei` por "responder") é o "Não sei" atrasado: entrega
  `dont_know`, submissão `dont_know` com `late = true` e sem motor, `revealed_at` e o gabarito permitido
  na mesma edição (na variante única, nada), e o `delivery.dont_know` depois do `dispatch.closed`;
  o "Não sei" gravado depois do horário com o disparo ainda aberto espera a transição e então vale como
  atrasado; o toque gravado antes do horário e processado depois vale (processado antes da transição);
  sem a recusa `refused/window_closed`, já que o gabarito só vai a quem já respondeu;
- o "Não sei" depois do
  fechamento numa `no_answer` com a correção atrasada pendente é recusado com `GRADING_IN_PROGRESS`
  (o "Não sei" atrasado com a correção pendente), e a correção, quando sai, dá `answered_late` e revela; antes do
  fechamento, o "Não sei" de controles superados numa `delivered` fica `answer_stale` (como os outros
  controles superados); no console, o `/naosei` sem "responder" depois do fechamento, no padrão desligado, é
  mascarado e não vira mensagem livre;
- o `/naosei` num canal sem botões (console), pela resposta à questão e pela regra do campo único
  ;
- o pedido do primeiro campo ainda `pending` (segurado por um 429) é cancelado na mesma transação do
  "Não sei" (e do preenchimento do último campo pela resposta à questão), e só a edição sai (o aluno
  nunca recebe o pedido).

**Montagem** como em `test_dispatch_closing.py`: oferecimento e bot pela API de oferecimentos, alunos pela
importação da lista e o vínculo por SQL; o disparo pelo caminho real (API e worker com o motor falso, a
obtenção falsa e o canal do Telegram ou o console com rota). No esqueleto, todo caso falha no preparo
do disparo (rota do disparo ausente, 404).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import itertools
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cq_server import deliverytexts as texts
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.buttons import CALLBACK_DATA
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.scheduler import CLOSE_RECHECK_SECONDS
from cq_server.tasks import enqueue_in_transaction
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    prepare_dispatch,
    register_bot,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleStudent
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    FakeQuestion,
    number_field,
    single_choice_field,
)

# --- constantes ------------------------------------------------------------------------------------

ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/disciplina/questoes-nao-sei.git"
COMMIT = "0c1d" * 10
WINDOW = timedelta(minutes=10)
WAIT = 20.0  # segundos reais de espera por uma condição
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
RECHECK = CLOSE_RECHECK_SECONDS + 1  # avanço que vence o intervalo das rodadas `done`
# Ritmo por chat curto nos testes (o `SendPacer` real, com o relógio monotônico).
DELIVERY = DeliverySettings(chat_interval_seconds=0.05)

# Textos das questões (tests/helpers/engine.py), para as conferências de valor.
COLOR_STATEMENT = "Qual é a cor do céu num dia limpo?"
COLOR_EXPLANATION = "O céu limpo é azul pelo espalhamento de Rayleigh."
HIDDEN_EXPLANATION = "Explicação que não pode ser mostrada."
SINGLE_VARIANT_EXPLANATION = "A resposta é a cor do céu."
VISIBLE_KEY = "GABARITO-VISIVEL-A1"
HIDDEN_KEY = "GABARITO-OCULTO-B2"
# A marca fixa do texto do update classificado como livre no modo desligado (mensagens livres).
FREE_MESSAGE_MASK = "[mensagem livre]"

_codes = itertools.count(1)
_phantom_update_ids = itertools.count(2_100_000_001)


# --- montagem ----------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Student:
    index: int
    chat_id: int
    user: Any
    person_row: int
    pseudo_id: str
    enrollment_row: int
    endpoint_row: int


@dataclasses.dataclass
class World:
    client: Any
    sql: psycopg.Connection
    clock: Any
    settings: Any
    engine: Any
    fetcher: Any
    api: Any  # a `rich_bot_api` (ou a `bot_api` pura, no console)
    code: str
    offering_id: str
    tg_bot: int
    bot_row: int
    students: list[Student]


@dataclasses.dataclass(frozen=True)
class Opened:
    dispatch_id: str
    row: int
    closes_at: datetime
    question_ref_id: str


def _mark_known_chat(api: Any, tg_bot: int, chat_id: int) -> None:
    """O chat privado do aluno já falou com o bot (senão o envio dá 400 `chat not found`)."""
    fake = getattr(api, "fake", api)
    with fake._cond:
        fake._bot(tg_bot).known_chats[chat_id] = "private"


def _world(client: Any, sql: psycopg.Connection, clock: Any, settings: Any, engine_fake: Any,
           repo_fetcher: Any, api: Any, *, students: int) -> World:
    """Oferecimento `active` com bot `registered` e `students` alunos vinculados (ver o docstring)."""
    n = next(_codes)
    offering = create_offering(client, ADMIN_TOKEN, code=f"nao-sei-{n}", timezone=ZONE)
    tg_bot = api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    bot_row, offering_row = sql.execute(
        "SELECT id, offering_id FROM bots WHERE telegram_bot_id = %s", (tg_bot,)).fetchone()
    repo_fetcher.add(REPO, COMMIT)
    created: list[Student] = []
    for i in range(1, students + 1):
        import_students(client, ADMIN_TOKEN, offering["code"], [{
            "name": f"Pessoa Nao Sei {n} {chr(64 + i)}", "ra": f"58{n:04d}{i:04d}",
            "email": f"pessoa.naosei.{n}.{i}@turma.example.org"}])
        enrollment_row, person_row, pseudo_id = sql.execute(
            "SELECT e.id, e.person_id, p.pseudo_id FROM enrollments e JOIN people p ON p.id = e.person_id "
            "WHERE e.offering_id = %s AND e.role = 'student' ORDER BY e.id DESC LIMIT 1",
            (offering_row,)).fetchone()
        now = clock.now()
        sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                    "terms_accepted_at = %s WHERE id = %s", (now, now, enrollment_row))
        user = make_user(i)
        endpoint_row = sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
            "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person_row, bot_row, user.user_id, now, now)).fetchone()[0]
        _mark_known_chat(api, tg_bot, user.user_id)
        created.append(Student(index=i, chat_id=user.user_id, user=user, person_row=person_row,
                               pseudo_id=str(pseudo_id), enrollment_row=enrollment_row,
                               endpoint_row=endpoint_row))
    return World(client=client, sql=sql, clock=clock, settings=settings, engine=engine_fake,
                 fetcher=repo_fetcher, api=api, code=offering["code"],
                 offering_id=offering["offering_id"], tg_bot=tg_bot, bot_row=bot_row, students=created)


def _engine(world: World) -> HttpQuestionEngine:
    return HttpQuestionEngine(EngineSettings(engine_url=world.engine.base_url,
                                             timeout_seconds=ENGINE_TIMEOUT))


def _last_poll(world: World) -> datetime | None:
    return world.sql.execute("SELECT last_poll_ok_at FROM bots WHERE id = %s",
                             (world.bot_row,)).fetchone()[0]


def _start_bots(world: World, bot_worker_thread: Any, **kwargs: Any) -> Any:
    """O worker com bots (o caminho de produção), com o motor, a obtenção e a entrega do disparo."""
    kwargs.setdefault("delivery", DELIVERY)
    thread = bot_worker_thread(engine=_engine(world), fetcher=world.fetcher, **kwargs)
    _wait(lambda: _last_poll(world) is not None, "o leitor do bot consultou o Telegram")
    return thread


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.05)


def _advance_until(world: World, predicate: Callable[[], Any], what: str, *, step: float = 15.0,
                   timeout: float = 3 * WAIT) -> None:
    """Espera `predicate()`; sem ele em 1,5 s reais, avança o relógio falso `step` segundos (menos que a
    posse de 30 s) e espera de novo: as repetições e as rodadas da varredura correm no relógio falso."""
    deadline = time.monotonic() + timeout
    while True:
        window = time.monotonic() + 1.5
        while time.monotonic() < window:
            if predicate():
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        world.clock.advance(step)


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dispatch_state(world: World, opened: Opened) -> str:
    return world.sql.execute("SELECT state FROM dispatches WHERE id = %s", (opened.row,)).fetchone()[0]


def _prepare(world: World, qid: str, question: FakeQuestion | str) -> tuple[str, datetime]:
    """Prepara um disparo imediato com `closes_at` informado e espera a validação (`scheduled`)."""
    world.engine.program((REPO, COMMIT, qid), question)
    closes_at = world.clock.now() + WINDOW
    prepared = prepare_dispatch(world.client, ADMIN_TOKEN, world.code, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(closes_at))
    dispatch_id = prepared["dispatch_id"]
    _wait(lambda: get_dispatch(world.client, ADMIN_TOKEN, world.code, dispatch_id)["state"] != "validating",
          "a validação do disparo")
    body = get_dispatch(world.client, ADMIN_TOKEN, world.code, dispatch_id)
    assert (body["state"], body["confirmed"]) == ("scheduled", False), body.get("rejection")
    return dispatch_id, closes_at


def _open(world: World, qid: str, question: FakeQuestion | str, *,
          until: Callable[[Opened], bool] | None = None,
          before_confirm: Callable[[], None] | None = None) -> Opened:
    """Prepara, confirma e espera a abertura; sem `until`, espera toda entrega `delivered`."""
    dispatch_id, closes_at = _prepare(world, qid, question)
    if before_confirm is not None:
        before_confirm()
    confirm_dispatch(world.client, ADMIN_TOKEN, world.code, dispatch_id)
    row, ref_uuid = world.sql.execute(
        "SELECT s.id, r.ref_uuid FROM dispatches s JOIN question_refs r ON r.id = s.question_ref_id "
        "WHERE s.dispatch_uuid = %s", (dispatch_id,)).fetchone()
    opened = Opened(dispatch_id=dispatch_id, row=row, closes_at=closes_at, question_ref_id=str(ref_uuid))
    _wait(lambda: _dispatch_state(world, opened) == "open", "a abertura do disparo")

    def delivered() -> bool:
        states = [r[0] for r in world.sql.execute(
            "SELECT state FROM deliveries WHERE dispatch_id = %s", (row,)).fetchall()]
        return len(states) == len(world.students) and all(s == "delivered" for s in states)

    _wait((lambda: until(opened)) if until is not None else delivered, "as entregas da abertura")
    return opened


def _delivery(world: World, opened: Opened, student: Student) -> dict:
    with world.sql.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT d.* FROM deliveries d JOIN enrollments e ON e.id = d.enrollment_id "
            "WHERE d.dispatch_id = %s AND e.person_id = %s", (opened.row, student.person_row)).fetchone()
    assert row is not None, f"sem entrega do aluno {student.index}"
    return row


def _contents(world: World, delivery: dict) -> dict:
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT parts, selection FROM delivery_contents WHERE delivery_id = %s",
                           (delivery["id"],)).fetchone()


def _submissions(world: World, delivery: dict) -> list[dict]:
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM submissions WHERE delivery_id = %s ORDER BY id",
                           (delivery["id"],)).fetchall()


def _messages(world: World, delivery: dict) -> list[dict]:
    """As mensagens da fila da entrega, em ordem de `id`."""
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT id, delivery_role, operation, state, attempts, channel_ref, field_index, text "
            "FROM outbox_messages WHERE delivery_id = %s ORDER BY id", (delivery["id"],)).fetchall()


def _principal_id(world: World, delivery: dict) -> int:
    ref = world.sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s",
                            (delivery["principal_message_id"],)).fetchone()[0]
    return int(ref)


def _question(world: World, opened: Opened, student: Student) -> Any:
    """A mensagem principal (a com os controles) da entrega, no estado atual da Bot API falsa."""
    delivery = _delivery(world, opened, student)
    return world.api.current(world.tg_bot, student.chat_id, _principal_id(world, delivery))


def _buttons(msg: Any) -> list[tuple[str, int, str | None, str]]:
    """(ação, campo, chave, dado) de cada botão, pela gramática `cq1:`."""
    out = []
    for button in msg.buttons():
        match = CALLBACK_DATA.fullmatch(button.data or "")
        assert match is not None, f"botão fora da gramática cq1: {button.label!r}"
        out.append((match.group(2), int(match.group(3), 36), match.group(5), button.data))
    return out


def _data(msg: Any, action: str, key: str | None = None) -> str:
    found = [data for a, _f, k, data in _buttons(msg) if a == action and k == key]
    assert len(found) == 1, f"botão {action}/{key} não está no teclado: {[b[:3] for b in _buttons(msg)]}"
    return found[0]


def _tap(world: World, student: Student, msg: Any, action: str, key: str | None = None) -> Any:
    return world.api.tap(world.tg_bot, student.user, msg.message_id, _data(msg, action, key))


def _mark_and_submit(world: World, student: Student, opened: Opened, key: str) -> tuple[Any, Any]:
    """Escolha única com confirmação: toca a alternativa `key` (`m`, `answer_toggled`, só os botões)
    e depois o "Enviar" (`s`) do teclado corrente; devolve os dois toques."""
    mark = _tap(world, student, _question(world, opened, student), "m", key)
    _settle(world)
    assert _update(world, mark.update_id)["outcome"] == "answer_toggled"
    submit = _tap(world, student, _question(world, opened, student), "s")
    _settle(world)
    return mark, submit


def _actions(msg: Any) -> set[str]:
    return {a for a, _f, _k, _d in _buttons(msg)}


def _sentinel_calls(world: World, student: Student) -> list[int]:
    """Os `seq` das chamadas à Bot API falsa dirigidas ao chat do aluno com a sentinela do gabarito."""
    return [c.seq for c in world.api.calls() if str(c.params.get("chat_id")) == str(student.chat_id)
            and ANSWER_SENTINEL in _params_text(c)]


def _reply(world: World, student: Student, message_id: int, text: str) -> int:
    return world.api.push_text(world.tg_bot, student.user, text, reply_to_message_id=message_id)


def _update(world: World, update_id: int) -> dict:
    with world.sql.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM telegram_updates WHERE bot_id = %s AND update_id = %s",
                           (world.bot_row, update_id)).fetchone()


def _busy(world: World, *, updates: bool = True, outbox: bool = True) -> list[str]:
    now = world.clock.now()
    busy = [f"tarefa {t}" for (t,) in world.sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    if outbox:
        busy += [f"mensagem {m}" for (m,) in world.sql.execute(
            "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
            (now,)).fetchall()]
    if updates:
        busy += [f"update {u}" for (u,) in world.sql.execute(
            "SELECT id FROM telegram_updates WHERE state = 'pending'").fetchall()]
    return busy


def _settle(world: World, *, updates: bool = True, outbox: bool = True) -> None:
    """Espera o sistema parar: nenhuma tarefa, mensagem ou update disponível, duas vezes, com mais de
    uma rodada da varredura (1 s real) entre elas."""
    deadline = time.monotonic() + 2 * WAIT
    while True:
        _wait(lambda: not _busy(world, updates=updates, outbox=outbox), "o sistema parar", timeout=2 * WAIT)
        time.sleep(1.2)
        if not _busy(world, updates=updates, outbox=outbox):
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"o sistema não parou: {_busy(world, updates=updates, outbox=outbox)}")


def _close_wait(world: World, opened: Opened, *, timeout: float = 3 * WAIT) -> None:
    """Espera o `closed`; sem ele, avança o relógio além de `CLOSE_RECHECK_SECONDS` (nova rodada)."""
    deadline = time.monotonic() + timeout
    while _dispatch_state(world, opened) != "closed":
        window = time.monotonic() + 2.5
        while time.monotonic() < window and _dispatch_state(world, opened) != "closed":
            time.sleep(0.05)
        if _dispatch_state(world, opened) == "closed":
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"o disparo não fechou em {timeout} s: {_dispatch_state(world, opened)}")
        world.clock.advance(RECHECK)


def _close(world: World, opened: Opened) -> datetime:
    """Leva o relógio além de `closes_at` e espera o fechamento; devolve o `closed_at` gravado."""
    world.clock.set(opened.closes_at + timedelta(seconds=1))
    _close_wait(world, opened)
    return _closed_at(world, opened)


def _closed_at(world: World, opened: Opened) -> datetime:
    return world.sql.execute("SELECT closed_at FROM dispatches WHERE id = %s", (opened.row,)).fetchone()[0]


def _events(world: World, *types: str, dispatch: Opened | None = None) -> list[dict]:
    items = all_events(world.client, ADMIN_TOKEN, *types)
    if dispatch is not None:
        items = [e for e in items if e["data"].get("dispatch_id") == dispatch.dispatch_id]
    return items


def _strip(events: list[dict]) -> list[dict]:
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _hash(html: str) -> str:
    """O `content_hash` de uma opção (o do motor, `engine/src/ir`: espaços colapsados)."""
    return "sha256:" + hashlib.sha256(re.sub(r"\s+", " ", html).strip().encode("utf-8")).hexdigest()


def _counts(**values: int) -> dict[str, int]:
    base = {k: 0 for k in ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                           "answered_late", "question_failed")}
    base.update(values)
    return base


def _closed_event(world: World, opened: Opened, closed_at: datetime, counts: dict[str, int]) -> dict:
    return {"type": "dispatch.closed", "occurred_at": iso(closed_at), "actor_token_id": None,
            "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                     "trigger": "window", "counts": counts}}


def _graded_event(world: World, opened: Opened, student: Student, delivery: dict, submission: dict, *,
                  late: bool, score: float, parts: list[dict]) -> dict:
    return {"type": "submission.graded", "occurred_at": iso(submission["graded_at"]), "actor_token_id": None,
            "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                     "delivery_id": str(delivery["delivery_uuid"]),
                     "submission_id": str(submission["submission_uuid"]),
                     "question_ref_id": opened.question_ref_id, "person": student.pseudo_id,
                     "valid": True, "late": late, "normalized": False, "score": score, "parts": parts}}


def _choice_part(index: int, score: float, html: str) -> dict:
    return {"index": index, "score": score, "choices": [_hash(html)], "ratio": None, "residual": None}


def _params_text(call: Any) -> str:
    return json.dumps(call.params, ensure_ascii=False, default=str)


def _sent_to(world: World, student: Student, *, since: datetime | None = None) -> list[Any]:
    """As mensagens novas do bot ao aluno (envios aceitos), desde `since` (horário do relógio falso)."""
    return [m for m in world.api.messages(world.tg_bot, student.chat_id)
            if since is None or m.clock_at >= since]


class _Gate:
    """Embrulha o tratador `telegram.update` do worker: fechada, segura a tarefa em curso (o laço do
    worker para nela) até `release`; aberta, só repassa."""

    def __init__(self, handler: Callable[[Any], None]) -> None:
        self._handler = handler
        self._open = threading.Event()
        self._open.set()
        self.entered = threading.Event()

    def close(self) -> None:
        self.entered.clear()
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def __call__(self, task: Any) -> None:
        if not self._open.is_set():
            self.entered.set()
            self._open.wait(3 * WAIT)
        self._handler(task)


@pytest.fixture
def gated_updates(settings: Any, clock: Any, bots: Any) -> Any:
    """O tratador `telegram.update` montado como o `run_worker` o monta, atrás de uma
    `_Gate`; passado ao worker por `handlers=`."""
    from cq_server import reception
    from cq_server.bots import TelegramChannel

    channel = TelegramChannel(settings.database_url, clock=clock, runtime=bots,
                              default=ConsoleChannel(io.StringIO()), delivery=DELIVERY)
    handler = reception.update_task_handler(settings.database_url, clock=clock, runtime=bots,
                                            settings=settings, channel=channel, delivery=DELIVERY)
    gate = _Gate(handler)
    yield gate
    gate.release()
    handler.close()
    channel.close()


def _phantom_update(world: World, chat_id: int) -> tuple[int, int]:
    """Um update `pending` gravado agora (texto de quem não é aluno), com a tarefa `telegram.update`
    tomada por um dono fictício com posse longa (a barreira (a) fica de pé enquanto ela durar);
    devolve (`telegram_updates.id`, `tasks.id`). Tudo numa transação: o worker nunca a vê livre."""
    now = world.clock.now()
    with psycopg.connect(world.settings.database_url) as conn:
        with conn.transaction():
            epoch = conn.execute("SELECT update_epoch FROM bots WHERE id = %s", (world.bot_row,)).fetchone()[0]
            row = conn.execute(
                "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, "
                "sender_id, text, command, details, received_at, state) "
                "VALUES (%s, %s, %s, 'message', %s, 'private', %s, 'olá', NULL, %s, %s, 'pending') RETURNING id",
                (world.bot_row, epoch, next(_phantom_update_ids), chat_id, chat_id,
                 Jsonb({"message_id": 1, "date": int(now.timestamp())}), now)).fetchone()[0]
            task = enqueue_in_transaction(conn, "telegram.update", {"update_row_id": row}, now=now,
                                          idempotency_key=f"telegram.update:{row}")
            conn.execute("UPDATE tasks SET state = 'leased', lease_owner = 'cq-teste-fantasma', "
                         "lease_until = %s, attempts = 1 WHERE id = %s", (now + timedelta(days=1), task))
    return row, task


def _free_phantom(world: World, task: int) -> None:
    world.sql.execute("UPDATE tasks SET state = 'pending', lease_owner = NULL, lease_until = NULL, "
                      "available_at = %s WHERE id = %s", (world.clock.now(), task))


def _rounds(world: World, task_type: str, subject: dict) -> list[tuple[str, str | None]]:
    """(estado, último erro) das tarefas de `task_type` do assunto, em ordem de `id`."""
    return [(r[0], r[1]) for r in world.sql.execute(
        "SELECT state, last_error FROM tasks WHERE task_type = %s AND payload @> %s ORDER BY id",
        (task_type, Jsonb(subject))).fetchall()]


def _world_rich(client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
                rich_bot_api: Any, students: int) -> World:
    return _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=students)


# --- "Não sei" pelo botão --------------------------------------------------------------------------------

def _dont_know_event(world: World, opened: Opened, student: Student, delivery: dict, submission: dict) -> dict:
    return {"type": "delivery.dont_know", "occurred_at": iso(submission["created_at"]), "actor_token_id": None,
            "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                     "delivery_id": str(delivery["delivery_uuid"]),
                     "submission_id": str(submission["submission_uuid"]), "person": student.pseudo_id}}


def _answer_texts(world: World, tap: Any) -> list[str | None]:
    return [a.text for a in world.api.answers(world.tg_bot) if a.callback_id == tap.callback_id]


def test_dont_know_before_answering_closes_the_delivery(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Antes de responder (uma resposta válida por disparo), "Não sei" → `dont_know`,
    submissão `dont_know` sem chamar o motor, `delivery.dont_know` exato, a edição com
    `DONT_KNOW_RECORDED` e sem controles (`controls_version`/`render_version` + 1); depois, um toque
    antigo e um texto por "responder" recebem "você já respondeu" e ficam `refused/already_answered`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    before = _delivery(world, opened, s1)
    question = _question(world, opened, s1)
    old_choice = _data(question, "m", "b")  # a alternativa da única marca com `m`
    clock.advance(4)
    tap = _tap(world, s1, question, "n")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    row = _update(world, tap.update_id)
    assert (row["state"], row["outcome"]) == ("processed", "answer_dont_know")
    assert (d1["state"], d1["answered_at"], d1["current_field"]) == ("dont_know", row["received_at"], None)
    assert (d1["controls_version"], d1["render_version"]) == (
        before["controls_version"] + 1, before["render_version"] + 1)
    assert (sub["outcome"], sub["raw_input"], sub["valid"], sub["refusal"], sub["received_at"]) == (
        "dont_know", {}, None, None, row["received_at"])
    assert engine_fake.calls("grade") == []
    assert _strip(_events(world, "delivery.dont_know", dispatch=opened)) == [
        _dont_know_event(world, opened, s1, d1, sub)]
    edited = _question(world, opened, s1)
    assert texts.DONT_KNOW_RECORDED in edited.plain_text
    assert edited.buttons() == []
    assert len(_answer_texts(world, tap)) == 1

    stale_tap = rich_bot_api.tap_raw(world.tg_bot, s1.user, question.message_id, old_choice)
    _settle(world)
    typed = _reply(world, s1, question.message_id, "b")
    _settle(world)

    assert _answer_texts(world, stale_tap) == [texts.ALREADY_ANSWERED]
    replies = [m for m in _sent_to(world, s1) if texts.ALREADY_ANSWERED in (m.plain_text or "")]
    assert len(replies) == 1
    for update_id in (stale_tap.update_id, typed):
        assert _update(world, update_id)["outcome"] == "answer_refused"
    subs = _submissions(world, _delivery(world, opened, s1))
    assert [(s["outcome"], s["refusal"]) for s in subs] == [
        ("dont_know", None), ("refused", "already_answered"), ("refused", "already_answered")]
    assert _delivery(world, opened, s1)["state"] == "dont_know"
    assert engine_fake.calls("grade") == []
    assert len(_events(world, "delivery.dont_know", dispatch=opened)) == 1


def test_dont_know_in_the_middle_of_multipart_keeps_filled_parts(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Numa questão de várias partes com a primeira
    preenchida, "Não sei" encerra a entrega; as partes preenchidas ficam gravadas na submissão
    `dont_know` e no conteúdo da entrega, sem correção."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Escolha e número", "Escolha a cor e calcule a velocidade.",
                            (single_choice_field("cor"), number_field("vel")))
    opened = _open(world, "multipart", question)
    _mark, part = _mark_and_submit(world, s1, opened, "b")  # marcar e enviar
    assert _update(world, part.update_id)["outcome"] == "answer_part_recorded"
    assert _delivery(world, opened, s1)["current_field"] == 1

    tap = _tap(world, s1, _question(world, opened, s1), "n")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], d1["current_field"]) == ("dont_know", None)
    assert (sub["outcome"], sub["raw_input"], sub["telegram_update_id"]) == (
        "dont_know", {"cor": "b"}, _update(world, tap.update_id)["id"])
    assert _contents(world, d1)["parts"] == {"cor": "b"}
    assert engine_fake.calls("grade") == []
    assert _strip(_events(world, "delivery.dont_know", dispatch=opened)) == [
        _dont_know_event(world, opened, s1, d1, sub)]


def test_dont_know_after_answering_is_refused(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Depois de uma resposta válida, um toque no "Não sei" antigo recebe
    "você já respondeu", fica `refused/already_answered` e não muda a entrega."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    question = _question(world, opened, s1)
    old_dont_know = _data(question, "n")
    _mark_and_submit(world, s1, opened, "b")  # marcar e enviar
    answered = _delivery(world, opened, s1)
    assert answered["state"] == "answered"

    tap = rich_bot_api.tap_raw(world.tg_bot, s1.user, question.message_id, old_dont_know)
    _settle(world)
    assert _answer_texts(world, tap) == [texts.ALREADY_ANSWERED]
    assert _update(world, tap.update_id)["outcome"] == "answer_refused"
    after = _delivery(world, opened, s1)
    assert (after["state"], after["answered_at"], after["controls_version"], after["render_version"]) == (
        "answered", answered["answered_at"], answered["controls_version"], answered["render_version"])
    assert [(s["outcome"], s["refusal"]) for s in _submissions(world, after)] == [
        ("graded", None), ("refused", "already_answered")]
    assert _events(world, "delivery.dont_know", dispatch=opened) == []


# --- depois do horário ------------------------------------------------------------------------------------

def test_dont_know_after_close_on_no_answer_ends_late_and_reveals(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O "Não sei" depois do fechamento numa `no_answer` (o gabarito só vai a quem já respondeu).
    No fechamento sem respostas, as edições de fechamento das
    `no_answer` oferecem o "Não
    sei" (botão), com `CLOSED_NOTICE` e `LATE_HOWTO`, sem o gabarito e sem `revealed_at`. O "Não sei"
    depois do fechamento — o botão da edição de fechamento (S1) e o `/naosei` em resposta à questão (S2)
    — encerra a entrega: `answer_dont_know`, entrega `dont_know` (`answered_at` = horário gravado do
    update) com `revealed_at`, uma submissão `dont_know` com `late = true` e sem motor, a edição com
    `DONT_KNOW_RECORDED`, `CLOSED_NOTICE` e o gabarito, sem botões, e o `delivery.dont_know` (sem
    `late`) depois do `dispatch.closed`; um toque seguinte recebe "você já respondeu"."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    closed_at = _close(world, opened)
    _settle(world)
    for student in world.students:
        msg = _question(world, opened, student)
        assert "n" in _actions(msg), (student.index, [b.label for b in msg.buttons()])
        assert texts.CLOSED_NOTICE in msg.plain_text and texts.LATE_HOWTO in msg.plain_text
        assert ANSWER_SENTINEL not in msg.plain_text, student.index
        assert _delivery(world, opened, student)["revealed_at"] is None, student.index

    clock.advance(10)
    closing_n = _data(_question(world, opened, s1), "n")
    tap = _tap(world, s1, _question(world, opened, s1), "n")
    _settle(world)
    command = rich_bot_api.push_text(world.tg_bot, s2.user, texts.DONT_KNOW_COMMAND, bot_command=True,
                                     reply_to_message_id=_question(world, opened, s2).message_id)
    _settle(world)

    recorded = {}
    for student, update_id in ((s1, tap.update_id), (s2, command)):
        row = _update(world, update_id)
        assert (row["state"], row["outcome"]) == ("processed", "answer_dont_know"), student.index
        delivery = _delivery(world, opened, student)
        assert (delivery["state"], delivery["answered_at"], delivery["current_field"]) == (
            "dont_know", row["received_at"], None)
        assert delivery["revealed_at"] is not None and delivery["revealed_at"] >= closed_at
        (sub,) = _submissions(world, delivery)
        assert (sub["outcome"], sub["late"], sub["raw_input"], sub["received_at"]) == (
            "dont_know", True, {}, row["received_at"])
        msg = _question(world, opened, student)
        for fragment in (texts.DONT_KNOW_RECORDED, texts.CLOSED_NOTICE, ANSWER_SENTINEL):
            assert fragment in msg.plain_text, (student.index, fragment)
        assert msg.buttons() == []
        recorded[student.index] = (delivery, sub)
    assert _answer_texts(world, tap) == [None]
    assert engine_fake.calls("grade") == []
    assert _strip(_events(world, "dispatch.closed", "delivery.dont_know", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(no_answer=2)),
        _dont_know_event(world, opened, s1, *recorded[s1.index]),
        _dont_know_event(world, opened, s2, *recorded[s2.index]),
    ]

    again = rich_bot_api.tap_raw(world.tg_bot, s1.user, _question(world, opened, s1).message_id, closing_n)
    _settle(world)
    assert _answer_texts(world, again) == [texts.ALREADY_ANSWERED]
    assert _update(world, again.update_id)["outcome"] == "answer_refused"
    assert [(s["outcome"], s["refusal"]) for s in _submissions(world, _delivery(world, opened, s1))] == [
        ("dont_know", None), ("refused", "already_answered")]


def test_old_dont_know_button_after_close_ends_late_and_reveals(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O botão "Não sei" de antes do fechamento: numa
    `no_answer`, o "Não sei" vale de qualquer versão. Depois do fechamento sem resposta, o toque no "Não
    sei" capturado **antes** do fechamento (versão superada pela edição de fechamento) é o "Não sei"
    atrasado: `answer_dont_know`, entrega `dont_know` com `revealed_at`, a submissão `dont_know` com
    `late = true`, o gabarito na mesma edição (`DONT_KNOW_RECORDED`, `CLOSED_NOTICE` e a sentinela, sem
    botões), a confirmação sem texto e o `delivery.dont_know` depois do `dispatch.closed`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    question = _question(world, opened, s1)
    old = _data(question, "n")
    closed_at = _close(world, opened)
    _settle(world)
    frozen = _delivery(world, opened, s1)
    assert (frozen["state"], frozen["revealed_at"]) == ("no_answer", None)

    clock.advance(10)
    tap = rich_bot_api.tap_raw(world.tg_bot, s1.user, question.message_id, old)
    _settle(world)

    row = _update(world, tap.update_id)
    assert (row["state"], row["outcome"]) == ("processed", "answer_dont_know")
    assert _answer_texts(world, tap) == [None]
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["answered_at"], d1["current_field"]) == ("dont_know", row["received_at"], None)
    assert d1["revealed_at"] is not None and d1["revealed_at"] >= closed_at
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["late"], sub["raw_input"], sub["refusal"]) == ("dont_know", True, {}, None)
    msg = _question(world, opened, s1)
    for fragment in (texts.DONT_KNOW_RECORDED, texts.CLOSED_NOTICE, ANSWER_SENTINEL):
        assert fragment in msg.plain_text, fragment
    assert msg.buttons() == []
    assert engine_fake.calls("grade") == []
    assert _strip(_events(world, "dispatch.closed", "delivery.dont_know", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(no_answer=1)),
        _dont_know_event(world, opened, s1, d1, sub),
    ]


def _late_dont_know_waits_for_transition(world: World, rich_bot_api: Any, *, via: str) -> None:
    """Montagem e conferências comuns dos dois casos do "Não sei" gravado depois do horário com o
    disparo ainda aberto: um update `pending` gravado antes do horário e tomado por um dono fictício segura a
    barreira (a) do fechamento; o "Não sei" (`via` = `"command"`: o `/naosei` em resposta à questão;
    `"button"`: o botão da mensagem da questão, da versão de antes do fechamento) é gravado depois de
    `closes_at` com o disparo ainda `open` e espera a transição; depois dela, vale como atrasado."""
    (s1,) = world.students
    opened = _open(world, "single_choice", "single_choice")
    _settle(world)
    stranger = make_user(91)
    _mark_known_chat(rich_bot_api, world.tg_bot, stranger.user_id)
    world.clock.set(opened.closes_at - timedelta(seconds=3))
    _settle(world, updates=False)
    _phantom_row, phantom_task = _phantom_update(world, stranger.user_id)

    world.clock.set(opened.closes_at + timedelta(seconds=1))
    question = _question(world, opened, s1)
    tap = None
    if via == "command":
        update_id = rich_bot_api.push_text(world.tg_bot, s1.user, texts.DONT_KNOW_COMMAND, bot_command=True,
                                           reply_to_message_id=question.message_id)
    else:
        tap = _tap(world, s1, question, "n")
        update_id = tap.update_id
    _wait(lambda: _update(world, update_id) is not None, "o leitor gravou o \"Não sei\" depois do horário")
    late = _update(world, update_id)
    assert late["received_at"] > opened.closes_at
    subject = {"update_row_id": late["id"]}
    _wait(lambda: any(state == "done" for state, _e in _rounds(world, "telegram.update", subject)),
          "a primeira rodada do update")

    # A primeira rodada termina `done` sem efeito: nada gravado, o update continua `pending`.
    assert _dispatch_state(world, opened) == "open"
    assert _update(world, update_id)["state"] == "pending", _update(world, update_id)["outcome"]
    first = _delivery(world, opened, s1)
    assert first["state"] == "delivered"
    assert _submissions(world, first) == []

    _free_phantom(world, phantom_task)
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    _advance_until(world, lambda: _delivery(world, opened, s1)["state"] != "no_answer",
                   "o \"Não sei\" atrasado depois do fechamento", step=RECHECK)
    _settle(world)

    row = _update(world, update_id)
    assert (row["state"], row["outcome"]) == ("processed", "answer_dont_know")
    assert all(state == "done" for state, _e in _rounds(world, "telegram.update", subject))
    assert [e for e in _events(world, "task.failed")] == []
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["answered_at"]) == ("dont_know", late["received_at"])
    assert d1["revealed_at"] is not None and d1["revealed_at"] >= closed_at
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["late"], sub["received_at"]) == ("dont_know", True, late["received_at"])
    assert ANSWER_SENTINEL in _question(world, opened, s1).plain_text
    if tap is not None:
        assert texts.PART_ALREADY_ANSWERED not in _answer_texts(world, tap)
        assert all(text is None for text in _answer_texts(world, tap))
    assert _strip(_events(world, "dispatch.closed", "delivery.dont_know", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(no_answer=1)),
        _dont_know_event(world, opened, s1, d1, sub),
    ]


def test_dont_know_recorded_after_time_with_dispatch_open_waits_for_transition(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O caso do comando, com o disparo ainda aberto depois do horário: o
    `/naosei` em resposta à questão, gravado depois de `closes_at` com o disparo ainda `open` (a
    barreira presa por um update anterior ao horário), espera a transição: a primeira rodada do
    `telegram.update` termina `done` sem efeito (update `pending`, nenhuma submissão, entrega
    `delivered`); depois do fechamento, `answer_dont_know`, a entrega `dont_know` com `revealed_at`, a
    submissão com `late = true` e o `delivery.dont_know` depois do `dispatch.closed`; nenhuma rodada
    `failed`, e nenhuma recusa com `window_closed`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    _start_bots(world, bot_worker_thread)
    _late_dont_know_waits_for_transition(world, rich_bot_api, via="command")


def test_dont_know_button_recorded_after_time_waits_then_counts_as_late(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O caso do botão, com o disparo ainda aberto depois do horário (o botão de antes do fechamento
    vale de qualquer versão): o botão "Não sei" da mensagem da questão (versão de antes do
    fechamento), tocado depois de `closes_at` com o disparo ainda `open`, espera a transição como o
    `/naosei`; depois dela, numa `no_answer`, vale de qualquer versão: `answer_dont_know` (não
    `answer_stale`), a entrega `dont_know` com `revealed_at`, `late = true`, a confirmação sem "esta
    parte já foi respondida" e o `delivery.dont_know` depois do `dispatch.closed`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    _start_bots(world, bot_worker_thread)
    _late_dont_know_waits_for_transition(world, rich_bot_api, via="button")


def test_late_dont_know_on_single_variant_reveals_nothing(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Variante única: numa questão de variante única
    sem resposta, o "Não sei" depois do fechamento (o botão da edição de fechamento) encerra a entrega
    (`dont_know`, `revealed_at` gravado, `DONT_KNOW_RECORDED` e `CLOSED_NOTICE` na edição), mas nenhum
    gabarito sai: a sentinela e a explicação da variante única nunca chegam ao chat.

    A regra (gabarito da variante única só com nota ≥ 1, e o "Não sei" não tem nota) é uma
    escolha do projeto, e não fato do PL; se a escolha mudar para a alternativa
    (revelar no "Não sei"),
    este caso muda."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_variant", "single_variant")
    closed_at = _close(world, opened)
    _settle(world)
    closing = _question(world, opened, s1)
    assert "n" in _actions(closing), [b.label for b in closing.buttons()]

    clock.advance(10)
    tap = _tap(world, s1, closing, "n")
    _settle(world)

    assert _update(world, tap.update_id)["outcome"] == "answer_dont_know"
    d1 = _delivery(world, opened, s1)
    assert d1["state"] == "dont_know"
    assert d1["revealed_at"] is not None and d1["revealed_at"] >= closed_at
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["late"]) == ("dont_know", True)
    msg = _question(world, opened, s1)
    assert texts.DONT_KNOW_RECORDED in msg.plain_text and texts.CLOSED_NOTICE in msg.plain_text
    assert _sentinel_calls(world, s1) == []
    leaked = [c.seq for c in world.api.calls() if str(c.params.get("chat_id")) == str(s1.chat_id)
              and SINGLE_VARIANT_EXPLANATION in _params_text(c)]
    assert leaked == []


def test_late_naosei_on_console_needs_reply_and_reveals(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        bot_api: Any, worker_thread: Any) -> None:
    """O "Não sei" atrasado no console, que precisa de "responder" e revela:
    no console (sem botões), a linha de fechamento da `no_answer` traz `CLOSED_NOTICE`, `LATE_HOWTO` e
    o `DONT_KNOW_HOWTO`, sem o gabarito. O `/naosei` **sem** "responder" depois do fechamento não chega
    à entrega (a regra do campo único só conta disparos abertos): nenhuma submissão, a entrega continua
    `no_answer` sem `revealed_at`; no padrão desligado (oferecimento sem `free_messages` nem contato), o
    update segue a regra das mensagens livres desligadas: `processed` com
    `free_message_masked_replied`, `text` mascarado (`[mensagem livre]`), `command` nulo e nenhuma linha
    em `free_messages` (o texto da resposta do bot é dos testes das mensagens livres e não é afirmado aqui). O
    `/naosei` em resposta à linha de fechamento é o "Não sei" atrasado:
    `dont_know` com `late = true` e uma linha nova (com `reply_to`) com `DONT_KNOW_RECORDED` e o
    gabarito."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, bot_api, students=1)
    (s1,) = world.students
    stream = io.StringIO()
    worker_thread(channel=ConsoleChannel(stream), console_routes=True, engine=_engine(world),
                  fetcher=repo_fetcher, delivery=DELIVERY)
    student = ConsoleStudent(ConsoleInbox(settings.database_url, clock=clock), bot_id=world.bot_row,
                             chat_id=s1.chat_id, recipient=s1.pseudo_id, source=stream)
    opened = _open(world, "number", FakeQuestion("Velocidade média", "Um corpo percorre 13 m em 2 s.",
                                                 (number_field(),)))
    student.wait_for_text("Um corpo percorre 13 m em 2 s.", new=False)
    student.mark()
    closed_at = _close(world, opened)
    _settle(world)
    closing = [line for line in student.new_lines() if texts.CLOSED_NOTICE in line.text]
    assert len(closing) == 1, [line.text[:80] for line in student.lines()]
    for fragment in (texts.LATE_HOWTO, texts.DONT_KNOW_HOWTO):
        assert fragment in closing[0].text, fragment
    assert ANSWER_SENTINEL not in closing[0].text
    assert closing[0].answerable
    assert _delivery(world, opened, s1)["revealed_at"] is None

    clock.advance(10)
    last_update = sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates WHERE bot_id = %s",
                              (world.bot_row,)).fetchone()[0]
    student.dont_know(None)
    _settle(world)
    loose = _delivery(world, opened, s1)
    assert (loose["state"], loose["revealed_at"]) == ("no_answer", None)
    assert _submissions(world, loose) == []
    # No padrão desligado: o `/naosei` sem "responder" é texto livre, mascarado no mesmo
    # `UPDATE` que o marca `processed` (`command` também), e não vira registro de mensagem livre.
    loose_updates = sql.execute(
        "SELECT id, text, command, state, outcome FROM telegram_updates WHERE bot_id = %s AND id > %s "
        "ORDER BY id", (world.bot_row, last_update)).fetchall()
    assert [row[1:] for row in loose_updates] == [
        (FREE_MESSAGE_MASK, None, "processed", "free_message_masked_replied")]
    assert sql.execute("SELECT count(*) FROM free_messages WHERE telegram_update_id = %s",
                       (loose_updates[0][0],)).fetchone()[0] == 0

    student.mark()
    student.dont_know(closing[0])
    _settle(world)
    d1 = _delivery(world, opened, s1)
    assert d1["state"] == "dont_know"
    assert d1["revealed_at"] is not None and d1["revealed_at"] >= closed_at
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["late"], sub["raw_input"]) == ("dont_know", True, {})
    recorded = [line for line in student.new_lines() if texts.DONT_KNOW_RECORDED in line.text]
    assert len(recorded) == 1 and recorded[0].reply_to is not None
    assert ANSWER_SENTINEL in recorded[0].text


def test_late_dont_know_in_multipart_keeps_filled_parts(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Várias partes, depois do fechamento: numa
    questão de várias partes com a 1ª preenchida (o número, por "responder" à questão) e a 2ª (a escolha
    única) não, o fechamento leva a entrega a `no_answer`; o "Não sei" pela edição de fechamento grava a
    submissão `dont_know` com `raw_input` igual às partes já enviadas e `late = true`, sem chamar o
    motor, e a entrega fica `dont_know` com `current_field` nulo.

    Também: antes do
    fechamento, com o campo 2 corrente, o toque no "Não sei" da versão 1 (superada) fica `answer_stale`
    ("esta parte já foi respondida"), sem submissão e sem mudar a entrega (controle que já passa no esqueleto,
    dentro de uma função que falha nele)."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Número e escolha", "Calcule a velocidade e escolha a cor.",
                            (number_field("vel"), single_choice_field("cor")))
    opened = _open(world, "multipart", question)
    old_n = _data(_question(world, opened, s1), "n")  # o "Não sei" da versão 1 (campo 1 corrente)
    part = _reply(world, s1, _question(world, opened, s1).message_id, "6.5")
    _settle(world)
    assert _update(world, part)["outcome"] == "answer_part_recorded"
    assert _delivery(world, opened, s1)["current_field"] == 1

    # Antes do fechamento, o "Não sei" de controles
    # superados segue a regra dos controles superados: o `n` da versão 1, tocado com o campo 2 corrente numa entrega `delivered`,
    # recebe "esta parte já foi respondida" (`answer_stale`), sem submissão e sem mudar a entrega (antes do
    # fechamento, esse toque não é aceito).
    filled = _delivery(world, opened, s1)
    stale = rich_bot_api.tap_raw(world.tg_bot, s1.user, _question(world, opened, s1).message_id, old_n)
    _settle(world)
    stale_row = _update(world, stale.update_id)
    assert (stale_row["state"], stale_row["outcome"]) == ("processed", "answer_stale")
    assert _answer_texts(world, stale) == [texts.PART_ALREADY_ANSWERED]
    held = _delivery(world, opened, s1)
    columns = ("state", "current_field", "controls_version", "render_version", "revealed_at", "answered_at")
    assert {k: held[k] for k in columns} == {k: filled[k] for k in columns}
    assert (held["state"], held["current_field"]) == ("delivered", 1)
    assert _submissions(world, held) == []
    assert _contents(world, held)["parts"] == {"vel": "6.5"}
    assert _events(world, "delivery.dont_know", dispatch=opened) == []
    _close(world, opened)
    _settle(world)
    assert (_delivery(world, opened, s1)["state"], _delivery(world, opened, s1)["current_field"]) == (
        "no_answer", 1)

    closing = _question(world, opened, s1)
    assert "n" in _actions(closing), [b.label for b in closing.buttons()]
    clock.advance(10)
    tap = _tap(world, s1, closing, "n")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], d1["current_field"]) == ("dont_know", None)
    assert (sub["outcome"], sub["raw_input"], sub["late"], sub["telegram_update_id"]) == (
        "dont_know", {"vel": "6.5"}, True, _update(world, tap.update_id)["id"])
    assert engine_fake.calls("grade") == []


def test_late_dont_know_while_late_grading_is_pending_is_refused(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O "Não sei" depois do fechamento, com correção pendente da atrasada, ele
    recebe "sua resposta está sendo corrigida"; a
    correção pendente (`grading_pending`) é conferida antes do `n` numa `no_answer`.


    Questão de um campo numérico, fechada sem resposta: a edição de fechamento da `no_answer` oferece o
    "Não sei". Com o motor parado, a resposta atrasada por "responder" à questão deixa a correção pendente
    (`answer_recorded`, submissão `grading` com `late = true`, entrega `no_answer`). Enquanto ela durar, o
    botão "Não sei" da edição de fechamento e o `/naosei` em resposta à questão são recusados:
    `answer_refused`, submissões `refused/grading_pending` ligadas aos updates, a confirmação do toque com
    `GRADING_IN_PROGRESS` e uma mensagem com `GRADING_IN_PROGRESS` ao `/naosei`; a entrega continua
    `no_answer`, sem `revealed_at` nem `answered_at`, sem `delivery.dont_know` e sem a sentinela do
    gabarito em nenhuma chamada ao chat. Com o motor de volta, a correção sai: `answered_late`
    (`answered_at` = horário gravado da resposta, `revealed_at = graded_at`), uma só edição leva o
    feedback (`VERDICT_CORRECT`) junto com o gabarito, a mensagem fica com `CLOSED_NOTICE` e sem o "Não
    sei", e os eventos são o `dispatch.closed` e o `submission.graded` (`late = true`), sem
    `delivery.dont_know`.

    *(Este caso passa pelo botão "Não sei" da edição de fechamento, que o esqueleto ainda não tem,
    e por isso falha nele.)*
    """
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "number", "number")
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)
    closing = _question(world, opened, s1)
    closing_n = _data(closing, "n")  # o "Não sei" da edição de fechamento da `no_answer`
    assert texts.CLOSED_NOTICE in closing.plain_text and texts.LATE_HOWTO in closing.plain_text
    frozen = _delivery(world, opened, s1)
    assert (frozen["state"], frozen["revealed_at"]) == ("no_answer", None)

    # A atrasada por "responder", com o motor parado: a correção fica pendente.
    clock.advance(10)
    engine_fake.stop()
    late = _reply(world, s1, closing.message_id, "6.5")
    _wait(lambda: [s["outcome"] for s in _submissions(world, _delivery(world, opened, s1))] == ["grading"],
          "a correção atrasada pendente")
    _settle(world)
    late_row = _update(world, late)
    assert (late_row["state"], late_row["outcome"]) == ("processed", "answer_recorded")
    pending = _delivery(world, opened, s1)
    (grading,) = _submissions(world, pending)
    assert (grading["outcome"], grading["late"], grading["raw_input"], grading["received_at"]) == (
        "grading", True, {"resposta": "6.5"}, late_row["received_at"])
    assert (pending["state"], pending["revealed_at"]) == ("no_answer", None)

    # O "Não sei" durante a correção pendente: o botão da edição de fechamento e o `/naosei` por "responder".
    tap = rich_bot_api.tap_raw(world.tg_bot, s1.user, closing.message_id, closing_n)
    _settle(world)
    in_progress = sum(texts.GRADING_IN_PROGRESS in (m.plain_text or "") for m in _sent_to(world, s1))
    command = rich_bot_api.push_text(world.tg_bot, s1.user, texts.DONT_KNOW_COMMAND, bot_command=True,
                                     reply_to_message_id=closing.message_id)
    _settle(world)

    assert _answer_texts(world, tap) == [texts.GRADING_IN_PROGRESS]
    assert sum(texts.GRADING_IN_PROGRESS in (m.plain_text or "") for m in _sent_to(world, s1)) == in_progress + 1
    refused_rows = [_update(world, update_id) for update_id in (tap.update_id, command)]
    for row in refused_rows:
        assert (row["state"], row["outcome"]) == ("processed", "answer_refused")
    held = _delivery(world, opened, s1)
    subs = _submissions(world, held)
    assert [(s["outcome"], s["refusal"], s["valid"]) for s in subs] == [
        ("grading", None, None), ("refused", "grading_pending", None), ("refused", "grading_pending", None)]
    assert [s["telegram_update_id"] for s in subs[1:]] == [row["id"] for row in refused_rows]
    assert subs[1]["raw_input"] == {}  # a recusa de um toque guarda `{}`
    assert (held["state"], held["revealed_at"], held["answered_at"]) == ("no_answer", None, None)
    assert _events(world, "delivery.dont_know", dispatch=opened) == []
    assert _sentinel_calls(world, s1) == []
    assert texts.DONT_KNOW_RECORDED not in _question(world, opened, s1).plain_text
    edits_before = len(rich_bot_api.edits(world.tg_bot, s1.chat_id))

    # O motor volta: a correção atrasada sai válida e revela o gabarito na edição do feedback.
    engine_fake.start()
    _advance_until(world, lambda: _delivery(world, opened, s1)["state"] != "no_answer",
                   "o desfecho da correção atrasada pendente")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    subs = _submissions(world, d1)
    assert [(s["outcome"], s["refusal"]) for s in subs] == [
        ("graded", None), ("refused", "grading_pending"), ("refused", "grading_pending")]
    graded = subs[0]
    assert (graded["valid"], graded["late"], graded["score"]) == (True, True, 1.0)
    assert (d1["state"], d1["answered_at"], d1["current_field"], d1["revealed_at"]) == (
        "answered_late", late_row["received_at"], None, graded["graded_at"])
    msg = _question(world, opened, s1)
    for fragment in (texts.VERDICT_CORRECT, texts.CLOSED_NOTICE, ANSWER_SENTINEL):
        assert fragment in msg.plain_text, fragment
    assert texts.DONT_KNOW_RECORDED not in msg.plain_text
    assert "n" not in _actions(msg)
    revealing = [e for e in rich_bot_api.edits(world.tg_bot, s1.chat_id)[edits_before:]
                 if ANSWER_SENTINEL in e.after.plain_text]
    assert len(revealing) == 1 and texts.VERDICT_CORRECT in revealing[0].after.plain_text
    events = _strip(_events(world, "dispatch.closed", "delivery.dont_know", "submission.graded", dispatch=opened))
    assert [e["type"] for e in events] == ["dispatch.closed", "submission.graded"]
    assert events[0] == _closed_event(world, opened, closed_at, _counts(no_answer=1))
    assert (events[1]["data"]["submission_id"], events[1]["data"]["late"], events[1]["data"]["valid"]) == (
        str(graded["submission_uuid"]), True, True)


def test_dont_know_recorded_before_close_and_processed_after_is_valid(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any, gated_updates: Any) -> None:
    """O toque em "Não sei" gravado
    antes do horário e processado depois (worker segurado) vale: é processado antes da transição, a
    entrega vira `dont_know` (nunca `no_answer`) e o `delivery.dont_know` sai antes do
    `dispatch.closed`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread, handlers={"telegram.update": gated_updates})
    opened = _open(world, "single_choice", "single_choice")
    clock.set(opened.closes_at - timedelta(seconds=5))
    _settle(world)
    gated_updates.close()
    tap = _tap(world, s1, _question(world, opened, s1), "n")
    _wait(gated_updates.entered.is_set, "o worker segurado no toque")
    received = _update(world, tap.update_id)["received_at"]
    assert received < opened.closes_at
    clock.set(opened.closes_at + timedelta(seconds=2))
    gated_updates.release()
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], d1["answered_at"], d1["revealed_at"]) == ("dont_know", received, closed_at)
    assert (sub["outcome"], sub["received_at"]) == ("dont_know", received)
    assert _update(world, tap.update_id)["outcome"] == "answer_dont_know"
    assert _strip(_events(world, "delivery.dont_know", "dispatch.closed", dispatch=opened)) == [
        _dont_know_event(world, opened, s1, d1, sub),
        _closed_event(world, opened, closed_at, _counts(dont_know=1)),
    ]


# --- /naosei no console -------------------------------------------------------------------------------------

@pytest.mark.parametrize("via", ["reply", "single_field"])
def test_naosei_on_a_channel_without_buttons(
        via: str, client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        bot_api: Any, worker_thread: Any) -> None:
    """Canal sem botões: no console, a mensagem da
    questão anuncia o `/naosei` (`DONT_KNOW_HOWTO`); o `/naosei` em resposta à questão ou, sem
    "responder", pela regra do campo único, tem o mesmo desfecho do botão: `dont_know`, submissão
    `dont_know` sem o motor, `delivery.dont_know` e a linha nova com `DONT_KNOW_RECORDED`."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, bot_api, students=1)
    (s1,) = world.students
    stream = io.StringIO()
    worker_thread(channel=ConsoleChannel(stream), console_routes=True, engine=_engine(world),
                  fetcher=repo_fetcher, delivery=DELIVERY)
    student = ConsoleStudent(ConsoleInbox(settings.database_url, clock=clock), bot_id=world.bot_row,
                             chat_id=s1.chat_id, recipient=s1.pseudo_id, source=stream)
    opened = _open(world, "number", FakeQuestion("Velocidade média", "Um corpo percorre 13 m em 2 s.",
                                                 (number_field(),)))
    question = student.wait_for_text("Um corpo percorre 13 m em 2 s.", new=False)
    assert texts.DONT_KNOW_HOWTO in question.text
    student.mark()
    student.dont_know(question if via == "reply" else None)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], d1["current_field"]) == ("dont_know", None)
    assert (sub["outcome"], sub["raw_input"]) == ("dont_know", {})
    assert engine_fake.calls("grade") == []
    outcome = sql.execute("SELECT outcome, text FROM telegram_updates WHERE id = %s",
                          (sub["telegram_update_id"],)).fetchone()
    assert outcome == ("answer_dont_know", texts.DONT_KNOW_COMMAND)
    new = student.new_lines()
    recorded = [line for line in new if texts.DONT_KNOW_RECORDED in line.text]
    assert len(recorded) == 1 and recorded[0].reply_to is not None
    assert _strip(_events(world, "delivery.dont_know", dispatch=opened)) == [
        _dont_know_event(world, opened, s1, d1, sub)]


# --- o pedido do campo ainda na fila -------------------------------------------------------------------------

def _is_prompt_to(student: Student) -> Callable[[Any], bool]:
    """O pedido de campo ao chat do aluno: o `ForceReply` no `reply_markup` (no Rich, o pedido sai por
    `sendRichMessage`, no dialeto da entrega)."""
    return lambda call: (call.params.get("chat_id") == student.chat_id
                         and "force_reply" in str(call.params.get("reply_markup")))


def _hold_prompts(world: World, student: Student) -> None:
    """Todo `sendRichMessage` do pedido de campo ao chat do aluno recebe 429 (o pedido do campo fica
    `pending`, esperando o relógio falso, que o teste não avança)."""
    world.api.fail("sendRichMessage", retry_after=1, times=None, when=_is_prompt_to(student))


def _prompt_rows(world: World, opened: Opened, student: Student) -> list[dict]:
    return [m for m in _messages(world, _delivery(world, opened, student)) if m["delivery_role"] == "field_prompt"]


def _prompt_held(world: World, student: Student) -> Callable[[Opened], bool]:
    def held(opened: Opened) -> bool:
        rows = _prompt_rows(world, opened, student)
        return _delivery(world, opened, student)["state"] == "delivered" and [
            (m["state"], m["attempts"]) for m in rows] == [("pending", 1)]
    return held


def test_dont_know_cancels_the_pending_first_field_prompt(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com o pedido do primeiro campo ainda
    `pending` (segurado por `fail("sendRichMessage", status=429)`), o "Não sei" o leva a `cancelled` na
    mesma transação, e só a edição do "Não sei" sai: o aluno nunca recebe o pedido."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    _hold_prompts(world, s1)
    opened = _open(world, "number", "number", until=_prompt_held(world, s1))
    _tap(world, s1, _question(world, opened, s1), "n")
    _settle(world)

    assert _delivery(world, opened, s1)["state"] == "dont_know"
    assert [(m["state"], m["field_index"]) for m in _prompt_rows(world, opened, s1)] == [("cancelled", 0)]
    assert [m for m in rich_bot_api.messages(world.tg_bot, s1.chat_id) if m.force_reply] == []
    attempts = [c for c in rich_bot_api.calls("sendRichMessage") if _is_prompt_to(s1)(c)]
    assert [c.status for c in attempts] == [429]
    edited = _question(world, opened, s1)
    assert texts.DONT_KNOW_RECORDED in edited.plain_text and edited.buttons() == []


def test_answer_by_replying_to_the_question_cancels_the_pending_prompt(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com o pedido do único campo ainda
    `pending`, a resposta digitada em resposta à mensagem da questão preenche o último campo, e o
    pedido vai a `cancelled` na mesma transação da submissão; o aluno recebe só o feedback."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    _hold_prompts(world, s1)
    opened = _open(world, "number", "number", until=_prompt_held(world, s1))
    _reply(world, s1, _question(world, opened, s1).message_id, "6.5")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["raw_input"]) == (
        "answered", "graded", True, {"resposta": "6.5"})
    assert [(m["state"], m["field_index"]) for m in _prompt_rows(world, opened, s1)] == [("cancelled", 0)]
    assert [m for m in rich_bot_api.messages(world.tg_bot, s1.chat_id) if m.force_reply] == []
    assert texts.VERDICT_CORRECT in _question(world, opened, s1).plain_text
