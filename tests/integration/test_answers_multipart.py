"""Questões com várias partes: a escolha, o número, o pedido de cada campo, a correção única e o
feedback por parte, com a escolha única que marca e envia, a entrega sem
resposta sem gabarito e o feedback com a nota.

Cobre:
- escolha + número: a escolha única só **marca** (✅) e a parte sai no "Enviar";
  a edição com o progresso (`PROGRESS`) a cada parte, o pedido do campo digitável
  depois da escolha (`FIELD_REQUEST`, com a dica no campo de digitação), o motor chamado uma única vez
  com os dois campos, o feedback por parte e o geral; o aluno não volta a um campo preenchido;
- nota geral parcial: a marca de cada parte nas três faixas (✅, ◐ ou ❌) e, no resultado,
  "◐ parcialmente correta" com a nota ("nota 50%", que sai em toda correção válida), também no
  feedback curto (o feedback dos textos fixos ao aluno);
- parte com nota fracionária (múltipla com crédito parcial, nota 0,5) pela composição da entrega: "◐" na
  linha da parte e o resultado com a nota geral exata ("nota 75%"), nunca as partes arredondadas para
  0/1 (uma parte com nota parcial);
- erro de formato só na parte 2: só a parte 2 é pedida de novo, a parte 1 fica, e a nova correção leva
  as duas; o erro chega traduzido, nunca em inglês;
- o erro do `server.py` fora de `fields[]` numa questão de várias partes: todas as partes limpas e a
  volta ao primeiro campo;
- o fechamento com partes preenchidas: `no_answer` sem o gabarito, as partes gravadas e a
  atrasada completando a questão, com o gabarito revelado na correção;
- o toque atrasado num campo já preenchido (a marcação e o "Enviar" antigos da única) e o texto
  por "responder" ao pedido de um campo já preenchido → "esta parte já foi respondida"
  (`answer_stale`), sem efeito;
- o pedido do campo 1 ainda `pending` quando o aluno o preenche respondendo à questão → `cancelled`, e
  só o pedido do campo 2 sai;
- duas múltiplas seguidas com as mesmas chaves: o segundo campo começa sem marcas, e o "Enviar" sem
  alternância é a seleção vazia (campo ausente na resposta crua);
- duas únicas seguidas com as mesmas chaves: marcar não preenche a parte, e o segundo campo começa sem
  marca;
- ecos longos que passam do limite do canal: a edição sai sem o bloco de feedback e o feedback curto
  sai no papel `notice`, em resposta à original.

**Montagem** como em `test_dispatch_closing.py`: oferecimento e bot pela API, alunos pela
importação da lista e o vínculo por SQL; o disparo pelo caminho real (API e worker com o motor falso, a
obtenção falsa e a Bot API falsa estendida). No esqueleto, todo caso falha no preparo do disparo (rota
do disparo ausente, 404).
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
from cq_server.adapters.console import ConsoleChannel
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
from tests.helpers.bot_api_dispatch import RICH_TEXT_MAX
from tests.helpers.clock import iso
from tests.helpers.engine import (
    SERVER_FORMAT_ERROR,
    FakeQuestion,
    integer_field,
    multiple_choice_field,
    number_field,
    single_choice_field,
    text_field,
)

# --- constantes ------------------------------------------------------------------------------------

ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/disciplina/questoes-varias-partes.git"
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
# a faixa parcial do resultado (`VERDICT_PARTIAL` dos textos fixos ao aluno), literal aqui porque a
# constante ainda não existe em `deliverytexts` no esqueleto (assim o módulo continua
# coletando).
VERDICT_PARTIAL = "◐ parcialmente correta"

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
    offering = create_offering(client, ADMIN_TOKEN, code=f"varias-partes-{n}", timezone=ZONE)
    tg_bot = api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    bot_row, offering_row = sql.execute(
        "SELECT id, offering_id FROM bots WHERE telegram_bot_id = %s", (tg_bot,)).fetchone()
    repo_fetcher.add(REPO, COMMIT)
    created: list[Student] = []
    for i in range(1, students + 1):
        import_students(client, ADMIN_TOKEN, offering["code"], [{
            "name": f"Pessoa Varias Partes {n} {chr(64 + i)}", "ra": f"57{n:04d}{i:04d}",
            "email": f"pessoa.variaspartes.{n}.{i}@turma.example.org"}])
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


# --- questões e auxiliares das várias partes --------------------------------------------------------------

NUMBER_HINT = "Número; ex.: 4.25, -17 ou 3/7"
SERVER_CHECK_TEXT = "A questão recusou a resposta. Confira o formato e envie de novo."


def _choice_and_number(**overrides: Any) -> FakeQuestion:
    return FakeQuestion("Escolha e número", "Escolha a cor e calcule a velocidade.",
                        (single_choice_field("cor"), number_field("vel")), **overrides)


def _prompts(world: World, student: Student) -> list[Any]:
    """Os pedidos de campo aceitos (mensagens com `ForceReply`), em ordem."""
    return [m for m in world.api.messages(world.tg_bot, student.chat_id) if m.force_reply]


def _student_texts(world: World, student: Student) -> list[str]:
    """Tudo o que o aluno viu: o texto de cada mensagem aceita e o de cada edição."""
    sent = [m.plain_text or m.caption or "" for m in world.api.messages(world.tg_bot, student.chat_id)]
    edited = [e.after.plain_text or "" for e in world.api.edits(world.tg_bot, student.chat_id)]
    return sent + edited


def _grade_raws(world: World) -> list[Any]:
    return [c.request["raw_submitted_answers"] for c in world.engine.calls("grade")]


def _field_actions(msg: Any) -> set[tuple[str, int]]:
    return {(a, f) for a, f, _k, _d in _buttons(msg) if a != "n"}


def _marked_labels(msg: Any) -> list[str]:
    """Os rótulos com a marca de seleção "✅ (k)"."""
    return [b.label for b in msg.buttons() if b.label.startswith("✅")]


def _mark_and_submit(world: World, student: Student, opened: Opened, key: str) -> tuple[Any, Any]:
    """Na escolha única corrente, toca na alternativa `key`
    (ação `m`, só marca) e, com o sistema parado, no "Enviar" (ação `s`) do teclado vigente; devolve os
    dois toques (marcação, "Enviar")."""
    mark = _tap(world, student, _question(world, opened, student), "m", key)
    _settle(world)
    submit = _tap(world, student, _question(world, opened, student), "s")
    _settle(world)
    return mark, submit


# --- escolha + número ----------------------------------------------------------------------------------------

def test_choice_then_number_grades_once_with_both_fields(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """A escolha grava a parte 1
    (`answer_part_recorded`), a mensagem da questão é editada com "Parte 2 de 2" e sem os botões do campo
    preenchido (o aluno não volta a ele), e sai o pedido do campo digitável, em resposta à questão, com
    a dica no campo de digitação; o número completa a questão: o motor é chamado uma única vez com os
    dois campos, e o feedback traz cada parte e o geral.

    Com a escolha única por marcar e enviar: o campo da escolha única tem as
    alternativas com a ação `m` e o "Enviar" (`s`); o toque em (b) só marca (`answer_toggled`, nenhuma
    parte, o campo corrente fica, sem correção nem pedido), e a parte 1 sai no "Enviar"."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multipart", _choice_and_number())
    question = _question(world, opened, s1)
    assert _field_actions(question) == {("m", 0), ("s", 0)}
    assert _prompts(world, s1) == []
    before = _delivery(world, opened, s1)
    assert (before["current_field"], _contents(world, before)["parts"]) == (0, {})

    mark = _tap(world, s1, question, "m", "b")
    _settle(world)
    assert _update(world, mark.update_id)["outcome"] == "answer_toggled"
    marked = _delivery(world, opened, s1)
    assert (marked["state"], marked["current_field"], marked["controls_version"]) == (
        "delivered", 0, before["controls_version"])
    assert _contents(world, marked) == {"parts": {}, "selection": ["b"]}
    assert _submissions(world, marked) == [] and engine_fake.calls("grade") == []
    assert _prompts(world, s1) == []
    assert _marked_labels(_question(world, opened, s1)) == ["✅ (b)"]

    part = _tap(world, s1, _question(world, opened, s1), "s")
    _settle(world)
    assert _update(world, part.update_id)["outcome"] == "answer_part_recorded"
    after_part = _delivery(world, opened, s1)
    assert (after_part["state"], after_part["current_field"], after_part["controls_version"]) == (
        "delivered", 1, before["controls_version"] + 1)
    assert _contents(world, after_part)["parts"] == {"cor": "b"}
    assert _submissions(world, after_part) == []
    assert engine_fake.calls("grade") == []
    progressed = _question(world, opened, s1)
    assert "Parte 2 de 2" in progressed.plain_text
    assert _field_actions(progressed) == set(), "o campo preenchido não pode ser tocado de novo"
    (prompt,) = _prompts(world, s1)
    assert prompt.plain_text.startswith("Campo 2 de 2: ")
    assert "Velocidade:" in prompt.plain_text and "m/s" in prompt.plain_text
    assert prompt.plain_text.endswith("— digite a resposta.")
    assert prompt.placeholder == NUMBER_HINT
    assert prompt.reply_parameters["message_id"] == question.message_id

    _reply(world, s1, prompt.message_id, "6.5")
    _settle(world)
    assert _grade_raws(world) == [{"cor": "b", "vel": "6.5"}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], d1["current_field"]) == ("answered", None)
    assert (sub["outcome"], sub["valid"], sub["score"], sub["raw_input"], sub["late"]) == (
        "graded", True, 1.0, {"cor": "b", "vel": "6.5"}, False)
    final = _question(world, opened, s1).plain_text
    assert re.search(r"Parte 1: .+ · ✅", final)
    assert "Parte 2: 6.5 · ✅" in final
    assert f"Resultado: {texts.VERDICT_CORRECT}" in final
    (event,) = _events(world, "submission.graded", dispatch=opened)
    parts = event["data"]["parts"]
    assert parts[0] == _choice_part(0, 1.0, "Azul")
    assert (len(parts), parts[1]["index"], parts[1]["score"], parts[1]["choices"]) == (2, 1, 1.0, [])
    assert all(isinstance(parts[1][k], (int, float)) or parts[1][k] is None for k in ("ratio", "residual"))
    assert {k: v for k, v in event["data"].items() if k != "parts"} == {
        "offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
        "delivery_id": str(d1["delivery_uuid"]), "submission_id": str(sub["submission_uuid"]),
        "question_ref_id": opened.question_ref_id, "person": s1.pseudo_id, "valid": True, "late": False,
        "normalized": False, "score": 1.0}


def test_partial_overall_score_shows_each_part_and_the_score(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O feedback com a nota parcial (o feedback, o `VERDICT_PARTIAL` e o
    `SCORE` dos textos fixos ao aluno): escolha + número, a escolha certa e o
    número errado ("7"); com crédito parcial, a nota geral é 0,5. O feedback traz "Parte 1: (b) Azul · ✅",
    "Parte 2: 7 · ❌" e "Resultado: ◐ parcialmente correta · nota 50%"; o evento leva a nota como veio
    (0,5) e as partes 1 e 0.

    A escolha vai digitada por "responder" à questão (vale como escolha digitada e submete direto,
    sem marcar), um caminho que marcar e enviar não muda: assim o caso depende só do resultado,
    não do toque. A saída errada que o caso acusa: "Resultado: ❌ incorreta", sem a nota."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multipart", _choice_and_number())
    question = _question(world, opened, s1)
    part = _reply(world, s1, question.message_id, "b")
    _settle(world)
    assert _update(world, part)["outcome"] == "answer_part_recorded"
    (prompt,) = _prompts(world, s1)
    _reply(world, s1, prompt.message_id, "7")
    _settle(world)

    assert _grade_raws(world) == [{"cor": "b", "vel": "7"}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["score"], sub["raw_input"]) == (
        "answered", "graded", True, 0.5, {"cor": "b", "vel": "7"})
    final = _question(world, opened, s1).plain_text
    assert "Parte 1: (b) Azul · ✅" in final
    assert "Parte 2: 7 · ❌" in final
    assert f"Resultado: {VERDICT_PARTIAL} · nota 50%" in final
    assert f"Resultado: {texts.VERDICT_INCORRECT}" not in final
    (event,) = _events(world, "submission.graded", dispatch=opened)
    parts = event["data"]["parts"]
    assert parts[0] == _choice_part(0, 1.0, "Azul")
    assert (len(parts), parts[1]["index"], parts[1]["score"], parts[1]["choices"]) == (2, 1, 0.0, [])
    assert (event["data"]["score"], event["data"]["valid"], event["data"]["late"]) == (0.5, True, False)


# A linha da parte termina no `\n` (a linha seguinte é outra parte ou o resultado); o começo não é
# ancorado porque o `plain_text` junta os blocos da mensagem sem quebra antes do feedback.
_PART_LINE = re.compile(r"Parte (?P<k>\d+): (?P<echo>[^\n]*) · (?P<mark>✅|◐|❌)\n")


def _part_lines(text: str) -> dict[int, tuple[str, str]]:
    """As linhas "Parte {k}: {eco} · {✅|◐|❌}" do feedback (o modelo `FEEDBACK`), por `k`:
    (eco, marca). Cada parte aparece uma única vez."""
    found = [(int(m.group("k")), m.group("echo"), m.group("mark")) for m in _PART_LINE.finditer(text)]
    assert len({k for k, _e, _m in found}) == len(found), f"parte repetida no feedback: {text!r}"
    return {k: (echo, mark) for k, echo, mark in found}


def test_partial_part_shows_half_mark_and_the_final_score(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """A nota parcial numa parte
    (uma parte com nota parcial, por exemplo uma múltipla com crédito
    parcial e nota 0,5, traz ◐; o feedback, o `VERDICT_PARTIAL` e o `SCORE` dos textos fixos ao
    aluno). O caso de integração que passa pela
    composição do feedback da entrega (`deliveries._feedback`) com uma parte de nota fracionária: o
    unitário de `deliverytexts.feedback` (`test_delivery_texts.py`, `partial-parts`) recebe as notas das
    partes prontas e não pega uma composição que as arredonde para 0/1 antes de chamá-lo.

    Múltipla (gabarito `a` e `c`, `net-correct`) + número, de mesmo peso: o aluno marca só (a) e toca em
    "Enviar" (a parte 1; uma marcação vai ao motor como texto) e digita o número
    certo ("6.5"). Notas: parte 1 = 0,5 (1 acerto, 0 erro, de 2), parte 2 = 1, geral = 0,75. O feedback
    traz "Parte 1: {eco} · ◐", com o eco "(a) Dois ✅" (a marca da alternativa logo depois do texto,
    em cada alternativa marcada), "Parte 2: 6.5 · ✅" e "Resultado: ◐ parcialmente correta · nota 75%"; a submissão e o evento
    guardam as notas como vieram do motor (0,5, 1 e 0,75).

    Pega as composições erradas: parte 0,5 arredondada para 0 (marca ❌) ou para 1 (marca ✅); o geral
    recomposto das partes arredondadas ("nota 50%" ou "nota 100%"); a nota numa linha de parte.

    A saída errada que o caso acusa: a parte 1 com ❌ (a marca por parte só em ✅/❌) e o resultado com "❌
    incorreta", sem a nota."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Primos e velocidade", "Marque os primos e calcule a velocidade.",
                            (multiple_choice_field("primos"), number_field("vel")))
    opened = _open(world, "multipart", question)
    first = _question(world, opened, s1)
    assert sorted(k for a, f, k, _d in _buttons(first) if (a, f) == ("m", 0)) == ["a", "b", "c", "d"]

    toggle = _tap(world, s1, first, "m", "a")
    _settle(world)
    assert _update(world, toggle.update_id)["outcome"] == "answer_toggled"
    marked = _question(world, opened, s1)
    assert sorted(k for a, f, k, _d in _buttons(marked) if (a, f) == ("u", 0)) == ["a"]
    part = _tap(world, s1, marked, "s")
    _settle(world)
    assert _update(world, part.update_id)["outcome"] == "answer_part_recorded"
    assert _delivery(world, opened, s1)["current_field"] == 1
    assert engine_fake.calls("grade") == []
    (prompt,) = _prompts(world, s1)
    assert prompt.plain_text.startswith("Campo 2 de 2: ")
    _reply(world, s1, prompt.message_id, "6.5")
    _settle(world)

    assert _grade_raws(world) == [{"primos": "a", "vel": "6.5"}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["score"], sub["late"]) == (
        "answered", "graded", True, 0.75, False)
    assert sub["partial_scores"] == {"primos": {"score": 0.5, "weight": 1}, "vel": {"score": 1.0, "weight": 1}}

    final = _question(world, opened, s1).plain_text
    lines = _part_lines(final)
    assert sorted(lines) == [1, 2], f"o feedback não traz uma linha por parte: {final!r}"
    echo1, mark1 = lines[1]
    assert mark1 == "◐", f"a parte com nota 0,5 tem de trazer ◐, veio {mark1!r}: {final!r}"
    assert "(a)" in echo1 and "Dois ✅" in echo1, f"o eco da parte 1 sem '(a) Dois ✅': {echo1!r}"
    for key, label in (("b", "Nove"), ("c", "Sete"), ("d", "Dez")):
        assert f"({key})" not in echo1 and label not in echo1, f"o eco traz a não escolhida ({key}): {echo1!r}"
    assert lines[2] == ("6.5", "✅")
    assert f"\nResultado: {VERDICT_PARTIAL} · nota 75%" in final, (
        f"o resultado tem de ser '{VERDICT_PARTIAL} · nota 75%' (nota geral 0,75): {final!r}")
    for wrong in ("nota 50%", "nota 100%", "nota 0%", f"Resultado: {texts.VERDICT_INCORRECT}",
                  f"Resultado: {texts.VERDICT_CORRECT}", "0,75", "0.75", "0,5"):
        assert wrong not in final, f"feedback com {wrong!r}: {final!r}"
    for echo, _mark in lines.values():
        assert "%" not in echo and "nota" not in echo, "a nota só na linha do resultado"

    (event,) = _events(world, "submission.graded", dispatch=opened)
    parts = event["data"]["parts"]
    assert parts[0] == _choice_part(0, 0.5, "Dois")
    assert (len(parts), parts[1]["index"], parts[1]["score"], parts[1]["choices"]) == (2, 1, 1.0, [])
    assert (event["data"]["score"], event["data"]["valid"], event["data"]["late"]) == (0.75, True, False)


def test_format_error_in_part_two_asks_only_part_two_again(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O erro de formato só na parte 2 limpa só a
    parte 2 (a 1 fica), explica o erro em português (nada do texto do PL chega ao aluno) e pede de novo
    só o campo 2; a nova correção leva os dois campos. A parte 1 sai por marcar e enviar
    na escolha única."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multipart", _choice_and_number())
    _mark_and_submit(world, s1, opened, "b")
    (first_prompt,) = _prompts(world, s1)
    controls = _delivery(world, opened, s1)["controls_version"]

    _reply(world, s1, first_prompt.message_id, "abc")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    (invalid,) = _submissions(world, d1)
    assert (invalid["outcome"], invalid["valid"], sorted(invalid["format_errors"])) == ("graded", False, ["vel"])
    assert (d1["state"], d1["current_field"], d1["controls_version"]) == ("delivered", 1, controls + 1)
    assert _contents(world, d1)["parts"] == {"cor": "b"}
    prompts = _prompts(world, s1)
    assert len(prompts) == 2 and prompts[1].plain_text.startswith("Campo 2 de 2: ")
    assert _field_actions(_question(world, opened, s1)) == set()
    seen = _student_texts(world, s1)
    assert any(text.startswith("Resposta não aceita (não conta como tentativa): ") or
               "Resposta não aceita (não conta como tentativa): " in text for text in seen)
    assert not any("Invalid format" in text or "decimal number" in text for text in seen)

    _reply(world, s1, prompts[1].message_id, "6.5")
    _settle(world)
    assert _grade_raws(world) == [{"cor": "b", "vel": "abc"}, {"cor": "b", "vel": "6.5"}]
    d1 = _delivery(world, opened, s1)
    assert [(s["outcome"], s["valid"]) for s in _submissions(world, d1)] == [("graded", False), ("graded", True)]
    assert (d1["state"], _submissions(world, d1)[1]["raw_input"]) == ("answered", {"cor": "b", "vel": "6.5"})


def test_server_format_error_clears_every_part_and_returns_to_the_first(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O erro de formato do `server.py` fora de `fields[]`
    numa questão de várias partes limpa todas as partes e a seleção, volta ao primeiro campo (os botões
    da escolha voltam, com versão nova) e explica com a frase da chave fora dos campos; o texto do autor
    em inglês nunca chega ao aluno.

    Com a escolha única por marcar e enviar: a parte 1 sai por marcar e enviar, e o teclado reaberto tem
    as alternativas com `m` e o "Enviar" (`s`), todas desmarcadas (a seleção zerada)."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multipart", _choice_and_number(server_format_error=SERVER_FORMAT_ERROR))
    _mark_and_submit(world, s1, opened, "b")
    (prompt,) = _prompts(world, s1)
    controls = _delivery(world, opened, s1)["controls_version"]
    _reply(world, s1, prompt.message_id, "6.5")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["valid"], sorted(sub["format_errors"])) == ("graded", False, ["verificacao"])
    assert (d1["state"], d1["current_field"], d1["controls_version"]) == ("delivered", 0, controls + 1)
    assert _contents(world, d1) == {"parts": {}, "selection": []}
    reopened = _question(world, opened, s1)
    assert sorted(k for a, f, k, _d in _buttons(reopened) if (a, f) == ("m", 0)) == ["a", "b", "c", "d"]
    assert [(a, f) for a, f, _k, _d in _buttons(reopened) if a == "s"] == [("s", 0)]
    assert _marked_labels(reopened) == [], "a seleção foi zerada: nenhuma alternativa marcada"
    seen = _student_texts(world, s1)
    assert any(SERVER_CHECK_TEXT in text for text in seen)
    assert not any("server check" in text for text in seen)
    assert len(_prompts(world, s1)) == 1, "o primeiro campo é uma escolha: nenhum pedido novo"


# --- fechamento com partes preenchidas --------------------------------------------------------------------

def test_close_with_filled_parts_then_late_completion(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com a parte 1 preenchida e sem correção, o fechamento
    leva a entrega a `no_answer` com as partes gravadas e o campo corrente mantido; a resposta atrasada
    à mensagem da questão completa a questão: uma correção com os dois campos, `late = true`,
    `answered_late`.

    A parte 1 sai por marcar e enviar; a
    entrega fica `no_answer`, sem o gabarito (`revealed_at` nulo no fechamento), e a atrasada corrigida o
    revela (o gabarito só vai a quem já respondeu)."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multipart", _choice_and_number())
    _mark_and_submit(world, s1, opened, "b")
    _close(world, opened)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["current_field"], d1["revealed_at"]) == ("no_answer", 1, None)
    assert _contents(world, d1)["parts"] == {"cor": "b"}
    assert _submissions(world, d1) == [] and engine_fake.calls("grade") == []

    clock.advance(20)
    _reply(world, s1, _question(world, opened, s1).message_id, "6.5")
    _settle(world)
    assert _grade_raws(world) == [{"cor": "b", "vel": "6.5"}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["late"], sub["raw_input"]) == (
        "answered_late", "graded", True, True, {"cor": "b", "vel": "6.5"})
    assert d1["revealed_at"] is not None, "a atrasada corrigida revela o gabarito"


# --- parte já respondida -----------------------------------------------------------------------------------

def test_late_tap_on_a_filled_field_is_stale(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Numa questão com dois campos de escolha, um toque
    atrasado num botão do campo já preenchido (edição ainda não aplicada no cliente) é confirmado com
    "esta parte já foi respondida" (`answer_stale`), sem submissão e sem mudar o campo corrente.

    Com a escolha única que marca e envia: o campo 1 (`cor`) é marcado (b) e enviado; os
    dois dados antigos do campo 1, a marcação `m a` e o "Enviar" `s`, tocados depois, recebem os dois
    "esta parte já foi respondida" (`answer_stale`), e o campo 2 continua sem marca."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Duas escolhas", "Escolha a cor e depois o tom.",
                            (single_choice_field("cor"), single_choice_field("tom", correct="c")))
    opened = _open(world, "multipart", question)
    first = _question(world, opened, s1)
    old_mark = _data(first, "m", "a")
    old_submit = _data(first, "s")
    _mark_and_submit(world, s1, opened, "b")
    filled = _delivery(world, opened, s1)
    assert (filled["current_field"], _contents(world, filled)) == (1, {"parts": {"cor": "b"}, "selection": []})

    stale = [rich_bot_api.tap_raw(world.tg_bot, s1.user, first.message_id, data)
             for data in (old_mark, old_submit)]
    _settle(world)
    for tap in stale:
        assert [a.text for a in rich_bot_api.answers(world.tg_bot) if a.callback_id == tap.callback_id] == [
            texts.PART_ALREADY_ANSWERED]
        assert _update(world, tap.update_id)["outcome"] == "answer_stale"
    d1 = _delivery(world, opened, s1)
    assert {k: d1[k] for k in ("state", "current_field", "controls_version", "render_version")} == {
        k: filled[k] for k in ("state", "current_field", "controls_version", "render_version")}
    assert _contents(world, d1) == {"parts": {"cor": "b"}, "selection": []}
    assert _submissions(world, d1) == [] and engine_fake.calls("grade") == []
    assert _marked_labels(_question(world, opened, s1)) == [], "o campo 2 continua sem marca"


def test_reply_to_the_prompt_of_a_filled_field_is_stale(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O texto por "responder" ao pedido do campo 1, já
    preenchido, recebe `PART_ALREADY_ANSWERED` em resposta (`answer_stale`), sem submissão e sem efeito
    no campo 2; a resposta ao pedido do campo 2 completa a questão."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Dois números", "Calcule a velocidade e conte as unidades.",
                            (number_field("vel"), integer_field("qtd")))
    opened = _open(world, "multipart", question)
    _settle(world)  # o pedido do campo sai depois do `delivered`, no ritmo por chat
    (prompt1,) = _prompts(world, s1)
    assert prompt1.plain_text.startswith("Campo 1 de 2: ")
    _reply(world, s1, prompt1.message_id, "6.5")
    _settle(world)
    prompt2 = _prompts(world, s1)[1]
    assert prompt2.plain_text.startswith("Campo 2 de 2: ")
    filled = _delivery(world, opened, s1)
    assert filled["current_field"] == 1

    stale = _reply(world, s1, prompt1.message_id, "7")
    _settle(world)
    assert _update(world, stale)["outcome"] == "answer_stale"
    replies = [m for m in _sent_to(world, s1) if texts.PART_ALREADY_ANSWERED in (m.plain_text or "")]
    assert len(replies) == 1
    d1 = _delivery(world, opened, s1)
    assert (d1["current_field"], d1["controls_version"]) == (1, filled["controls_version"])
    assert _contents(world, d1)["parts"] == {"vel": "6.5"}
    assert _submissions(world, d1) == []

    _reply(world, s1, prompt2.message_id, "42")
    _settle(world)
    assert _grade_raws(world) == [{"vel": "6.5", "qtd": "42"}]
    assert _delivery(world, opened, s1)["state"] == "answered"


def test_pending_prompt_of_a_field_filled_through_the_question_is_cancelled(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com o pedido do campo 1 ainda `pending` (segurado por um
    429), o aluno preenche o campo 1 respondendo à mensagem da questão; o pedido do campo 1 vai a
    `cancelled` na mesma transação, e só o pedido do campo 2 sai."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    rich_bot_api.fail("sendRichMessage", retry_after=1, times=None,
                      when=lambda call: call.params.get("chat_id") == s1.chat_id
                      and "Campo 1 de 2" in str(call.params.get("rich_message")))
    question = FakeQuestion("Dois números", "Calcule a velocidade e conte as unidades.",
                            (number_field("vel"), integer_field("qtd")))

    def held(opened: Opened) -> bool:
        rows = [m for m in _messages(world, _delivery(world, opened, s1)) if m["delivery_role"] == "field_prompt"]
        return _delivery(world, opened, s1)["state"] == "delivered" and [
            (m["state"], m["attempts"], m["field_index"]) for m in rows] == [("pending", 1, 0)]

    opened = _open(world, "multipart", question, until=held)
    _reply(world, s1, _question(world, opened, s1).message_id, "6.5")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["current_field"], _contents(world, d1)["parts"]) == (1, {"vel": "6.5"})
    rows = [(m["field_index"], m["state"]) for m in _messages(world, d1) if m["delivery_role"] == "field_prompt"]
    assert rows == [(0, "cancelled"), (1, "sent")]
    prompts = _prompts(world, s1)
    assert len(prompts) == 1 and prompts[0].plain_text.startswith("Campo 2 de 2: ")


# --- duas múltiplas com as mesmas chaves ----------------------------------------------------------------------

def test_second_multiple_choice_starts_unmarked_and_empty_submit_is_absent(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Em duas múltiplas seguidas com as mesmas chaves (a, b, c, d),
    as marcas do primeiro campo não passam ao segundo: ele começa sem marcas (seleção `[]`), e um
    "Enviar" sem alternância manda o segundo campo **ausente** na resposta crua."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Duas múltiplas", "Marque os primos nas duas listas.",
                            (multiple_choice_field("p1"), multiple_choice_field("p2")))
    opened = _open(world, "multipart", question)
    for key in ("a", "c"):
        _tap(world, s1, _question(world, opened, s1), "m", key)
        _settle(world)
    marked = _question(world, opened, s1)
    assert sorted(k for a, f, k, _d in _buttons(marked) if (a, f) == ("u", 0)) == ["a", "c"]
    assert _contents(world, _delivery(world, opened, s1))["selection"] == ["a", "c"]
    _tap(world, s1, marked, "s")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["current_field"], _contents(world, d1)) == (1, {"parts": {"p1": ["a", "c"]}, "selection": []})
    second = _question(world, opened, s1)
    assert sorted(k for a, f, k, _d in _buttons(second) if (a, f) == ("m", 1)) == ["a", "b", "c", "d"]
    assert [b for b in _buttons(second) if b[0] == "u"] == []
    assert engine_fake.calls("grade") == []

    _tap(world, s1, second, "s")
    _settle(world)
    assert _grade_raws(world) == [{"p1": ["a", "c"]}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["valid"], sorted(sub["format_errors"])) == ("graded", False, ["p2"])
    assert (d1["state"], d1["current_field"], _contents(world, d1)) == (
        "delivered", 1, {"parts": {"p1": ["a", "c"]}, "selection": []})


def test_second_single_choice_starts_unmarked_and_marking_does_not_fill(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com a escolha única que marca e envia (o "Enviar" sem marcação segue o
    PL): em duas únicas seguidas com as mesmas chaves (a, b, c, d), marcar (b) no campo 0 não
    preenche a parte (`answer_toggled`, nenhuma parte, o campo corrente fica); o "Enviar" grava
    `{"cor": "b"}` (`str`), passa ao campo 1 e zera a seleção, e o teclado do campo 1 começa sem nenhum
    "✅"; marcar e enviar no campo 1 → uma só correção com os dois campos.

    A saída errada que o caso acusa: o teclado da única sem a ação `m` nem o "Enviar" (só `c`)."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Duas únicas", "Escolha a cor e depois o tom.",
                            (single_choice_field("cor"), single_choice_field("tom", correct="c")))
    opened = _open(world, "multipart", question)
    first = _question(world, opened, s1)
    assert _field_actions(first) == {("m", 0), ("s", 0)}
    before = _delivery(world, opened, s1)

    mark = _tap(world, s1, first, "m", "b")
    _settle(world)
    assert _update(world, mark.update_id)["outcome"] == "answer_toggled"
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["current_field"], d1["controls_version"]) == (
        "delivered", 0, before["controls_version"])
    assert _contents(world, d1) == {"parts": {}, "selection": ["b"]}
    assert _submissions(world, d1) == [] and engine_fake.calls("grade") == []
    assert _marked_labels(_question(world, opened, s1)) == ["✅ (b)"]

    submit = _tap(world, s1, _question(world, opened, s1), "s")
    _settle(world)
    assert _update(world, submit.update_id)["outcome"] == "answer_part_recorded"
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["current_field"], d1["controls_version"]) == (
        "delivered", 1, before["controls_version"] + 1)
    assert _contents(world, d1) == {"parts": {"cor": "b"}, "selection": []}
    second = _question(world, opened, s1)
    assert _field_actions(second) == {("m", 1), ("s", 1)}, "o campo preenchido não pode ser tocado de novo"
    assert sorted(k for a, f, k, _d in _buttons(second) if (a, f) == ("m", 1)) == ["a", "b", "c", "d"]
    assert _marked_labels(second) == [], "a marca do campo 0 não passa ao campo 1"
    assert engine_fake.calls("grade") == []

    _mark_and_submit(world, s1, opened, "c")
    assert _grade_raws(world) == [{"cor": "b", "tom": "c"}]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["score"], sub["raw_input"]) == (
        "answered", "graded", True, 1.0, {"cor": "b", "tom": "c"})


# --- feedback que não cabe ----------------------------------------------------------------------------------

def test_feedback_that_does_not_fit_goes_to_a_short_notice(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Quando a renderização com o
    feedback passa do limite do Rich mesmo com os ecos truncados, a edição da original sai sem o bloco
    de feedback e o feedback curto (no formato do feedback de nota parcial, sem eco) sai no papel `notice`,
    em resposta à original.

    O enunciado é calibrado para a mensagem da questão caber com folga de 100 bytes: uma questão de
    sonda, de mesma forma, mede o que a renderização acrescenta ao enunciado (o texto sem as tags, em
    bytes UTF-8, a medida do limite)."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)

    def two_texts(statement: str) -> FakeQuestion:
        return FakeQuestion("Dois textos", statement,
                            (text_field("t1", label="Primeiro:"), text_field("t2", label="Segundo:")))

    probe_statement = "a" * 1000
    probe = _open(world, "text", two_texts(probe_statement))
    overhead = len(_question(world, probe, s1).plain_text.encode("utf-8")) - len(probe_statement)
    statement = "b" * (RICH_TEXT_MAX - 100 - overhead)
    opened = _open(world, "multipart", two_texts(statement))
    _settle(world)  # o pedido do campo sai depois do `delivered`, no ritmo por chat
    question = _question(world, opened, s1)
    assert RICH_TEXT_MAX - 150 <= len(question.plain_text.encode("utf-8")) <= RICH_TEXT_MAX

    prompt1 = [p for p in _prompts(world, s1) if p.reply_parameters["message_id"] == question.message_id][0]
    _reply(world, s1, prompt1.message_id, "a")
    _settle(world)
    prompt2 = [p for p in _prompts(world, s1) if p.reply_parameters["message_id"] == question.message_id][-1]
    assert prompt2.plain_text.startswith("Campo 2 de 2: ")
    echo = "y" * 300
    _reply(world, s1, prompt2.message_id, echo)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["valid"], sub["score"], sub["raw_input"]) == (
        "answered", True, 0.0, {"t1": "a", "t2": echo})
    final = _question(world, opened, s1)
    assert "Resultado:" not in final.plain_text and "y" * 50 not in final.plain_text
    notices = [m for m in rich_bot_api.messages(world.tg_bot, s1.chat_id)
               if (m.reply_parameters or {}).get("message_id") == question.message_id
               and texts.VERDICT_INCORRECT in (m.plain_text or "")]
    assert len(notices) == 1
    assert "y" * 20 not in notices[0].plain_text
    roles = [(m["delivery_role"], m["operation"], m["state"]) for m in _messages(world, d1)]
    assert roles.count(("notice", "send", "sent")) == 1


def test_short_notice_follows_the_partial_format(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O feedback curto no formato da nota parcial (dos textos fixos ao
    aluno): a montagem de `test_feedback_that_does_not_fit_goes_to_a_short_notice`, com t1
    certo ("azul") e t2 longo e errado; com crédito parcial, a nota geral é 0,5. O feedback curto, em
    resposta à original, é exatamente "Parte 1: ✅\nParte 2: ❌\nResultado: ◐ parcialmente correta · nota
    50%", sem eco.

    A saída errada que o caso acusa: o aviso com "Resultado: ❌ incorreta", sem a nota."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, students=1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)

    def two_texts(statement: str) -> FakeQuestion:
        return FakeQuestion("Dois textos", statement,
                            (text_field("t1", label="Primeiro:"), text_field("t2", label="Segundo:")))

    probe_statement = "a" * 1000
    probe = _open(world, "text", two_texts(probe_statement))
    overhead = len(_question(world, probe, s1).plain_text.encode("utf-8")) - len(probe_statement)
    statement = "b" * (RICH_TEXT_MAX - 100 - overhead)
    opened = _open(world, "multipart", two_texts(statement))
    _settle(world)
    question = _question(world, opened, s1)
    assert RICH_TEXT_MAX - 150 <= len(question.plain_text.encode("utf-8")) <= RICH_TEXT_MAX

    prompt1 = [p for p in _prompts(world, s1) if p.reply_parameters["message_id"] == question.message_id][0]
    _reply(world, s1, prompt1.message_id, "azul")
    _settle(world)
    prompt2 = [p for p in _prompts(world, s1) if p.reply_parameters["message_id"] == question.message_id][-1]
    assert prompt2.plain_text.startswith("Campo 2 de 2: ")
    echo = "y" * 300
    _reply(world, s1, prompt2.message_id, echo)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["valid"], sub["score"], sub["raw_input"]) == (
        "answered", True, 0.5, {"t1": "azul", "t2": echo})
    final = _question(world, opened, s1)
    assert "Resultado:" not in final.plain_text and "y" * 50 not in final.plain_text
    notices = [m for m in rich_bot_api.messages(world.tg_bot, s1.chat_id)
               if (m.reply_parameters or {}).get("message_id") == question.message_id
               and "Resultado:" in (m.plain_text or "")]
    assert [m.plain_text for m in notices] == [
        f"Parte 1: ✅\nParte 2: ❌\nResultado: {VERDICT_PARTIAL} · nota 50%"]
    roles = [(m["delivery_role"], m["operation"], m["state"]) for m in _messages(world, d1)]
    assert roles.count(("notice", "send", "sent")) == 1
