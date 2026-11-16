"""Fechamento, revelação do gabarito e atrasadas: o gabarito ao fechar a janela, as respostas atrasadas
com métrica dedicada, a resposta gravada antes do fechamento e processada depois, a entrada depois do
horário que espera a transição, os eventos depois do fechamento e a revelação devida.
Os casos seguem as regras de resposta e de feedback abaixo.

**As regras de resposta e de feedback:**
- a escolha única tem confirmação: o toque numa alternativa só a marca (ação `m`) e a resposta
  sai no "Enviar" (`s`); toda resposta por botão deste arquivo é "marcar e enviar"; o texto digitado
  numa escolha continua submetendo direto;
- só quem já respondeu recebe o gabarito: no fechamento, o gabarito é revelado só às entregas com resposta ou
  "Não sei"; a `no_answer` fica sem `revealed_at` e sem o gabarito, com os controles de resposta **com**
  o "Não sei" e o `LATE_HOWTO`; a atrasada revela o gabarito na mesma edição do feedback
  (`revealed_at = graded_at`); a tarefa `delivery.reveal` nunca revela uma `no_answer`;
- o feedback de nota parcial: o feedback com ✅, ◐ ou ❌ e a nota ("nota N%") em toda correção
  válida, com as marcas por alternativa da múltipla, também na atrasada e na variante única;
- as mensagens livres são opcionais: o oferecimento é cadastrado com elas ligadas (`free_messages=True`): o
  caso do texto sem "responder" depois do horário afirma o comportamento do modo ligado;
- os pedidos de campo são lidos depois de `_settle`, porque saem depois da `question`
  (ritmo por chat).

Cobre a barreira, a transição, a revelação, a edição que falha e as atrasadas, os desfechos
de uma correção pendente no fechamento e a entrada depois do horário com o disparo aberto:
- a barreira: o fechamento só acontece sem update `pending` com `received_at <= closes_at` e com
  `last_poll_ok_at >= closes_at` (o bot sendo consultado); enquanto isso, as rodadas `dispatch.close`
  terminam `done` sem efeito;
- a transição (`delivered` → `no_answer`; `pending` → `not_delivered` com o motivo, mensagens da fila
  canceladas), o `dispatch.closed {trigger: window, counts}` exato e a revelação a partir do que a
  entrega guardou, com o motor parado;
- o gabarito permitido (`can_show`, `showCorrectAnswer`, variante única com nota ≥ 1), a sentinela
  do gabarito (controle positivo na edição; ausente antes do fechamento e das entregas sem gabarito);
- a edição que falha no fechamento, a `question` em voo no fechamento, o bot sem
  acesso, a correção pendente no fechamento e os seus desfechos;
- entradas gravadas antes do horário e processadas depois (worker segurado) valem no prazo;
  as gravadas depois do horário com o disparo ainda aberto esperam a transição;
- o console: a revelação sai como linha nova;
- o eco da alternativa escolhida (a chave e o texto da opção, sem as não escolhidas) em
  toda edição com o feedback de uma escolha, também depois da revelação e na atrasada.

**Montagem.** O oferecimento e o bot entram pela API (a Bot API falsa estendida, `rich_bot_api`,
ou a `bot_api` pura no console); os alunos, pela importação da lista (`students.import`), e o vínculo
(matrícula `active` com o termo aceito e o endereço `active` no bot), por SQL direto, nas colunas do
esquema do banco, como o `test_outbox_routes.py` monta endereços (o vínculo pelo Telegram é
tratado em outros testes e não é o assunto aqui). O chat de cada aluno é marcado como conhecido na Bot API falsa (o
`sendMessage` a um chat que nunca falou com o bot dá 400). O disparo passa pelo caminho real: preparo
e confirmação pela API, validação, abertura e entrega pelo worker (`run_worker` com o motor falso,
a obtenção falsa e o canal do Telegram ou o console com rota).

**Tempo.** O `FakeClock` decide janelas, prazos, varredura e atraso; os saltos grandes acontecem com o
sistema parado (`_settle`), e o fechamento avança o relógio além de `CLOSE_RECHECK_SECONDS` até a
rodada que fecha (a barreira pode ver o leitor antes da primeira consulta depois do salto). Para
segurar um update gravado antes do horário e processado depois (worker parado), o tratador
`telegram.update` do worker é o mesmo que o `run_worker` monta, embrulhado numa trava que o teste solta
(`_Gate`), como a semente monta o tratador da validação. Um update que precisa ficar
`pending` por várias rodadas (barreira (a)) tem a tarefa tomada por um dono fictício, montada por SQL,
como nos testes da posse esgotada.

Ids internos, `seq`, seeds, `button_token`, `update_id` e pseudônimos nunca são literais esperados:
vêm do banco, do dublê ou da API. No esqueleto, todo caso falha no preparo do disparo (rota do disparo
ausente, 404) ou numa tabela do disparo ausente.
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
from collections.abc import Callable, Iterable, Mapping
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
from cq_server.scheduler import CLOSE_RECHECK_SECONDS, SWEEP_MAX_UPDATE_ROUNDS
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
from tests.helpers.bot_api_dispatch import DISPATCH_ERRORS
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleStudent
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    COLOR_OPTIONS,
    PRIME_OPTIONS,
    FakeQuestion,
    multiple_choice_field,
    number_field,
    single_choice_field,
)

# --- constantes ------------------------------------------------------------------------------------

ZONE = "America/Sao_Paulo"
REPO = "https://repos.cq.test/disciplina/questoes-fechamento.git"
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
# As alternativas da escolha única dos presets: chave → texto da opção (a Vermelho, b Azul, c Verde, d Amarelo).
COLORS = {o.key: o.html for o in COLOR_OPTIONS}
# As alternativas da múltipla dos presets (a Dois, b Nove, c Sete, d Dez; gabarito a, c; `net-correct`).
PRIMES = {o.key: o.html for o in PRIME_OPTIONS}
NUMBER_EXPLANATION = "A velocidade média é a distância dividida pelo tempo."
PRIME_EXPLANATION = "Dois e Sete são os primos da lista."
# Feedback de nota parcial (`VERDICT_PARTIAL` e `SCORE`): literais aqui porque as constantes
# ainda não existem em `deliverytexts` no esqueleto (onde o módulo continua coletando e o teste falha).
VERDICT_PARTIAL = "◐ parcialmente correta"
SCORE = "nota {n}%"
# O formato do `FEEDBACK`: "Sua resposta: {echo} · {verdict} · {score}"; o ◐ é a faixa parcial.
_ECHO = re.compile(r"Sua resposta:\s*(?P<echo>.*?)\s*·\s*(?:✅|◐|❌)", re.S)

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
    # As mensagens livres ligadas (opcionais por oferecimento); o caso do texto sem "responder"
    # depois do horário afirma o modo ligado (o desligado fica em test_free_messages.py).
    offering = create_offering(client, ADMIN_TOKEN, code=f"fechamento-{n}", timezone=ZONE, free_messages=True)
    tg_bot = api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], api.token_of(tg_bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    bot_row, offering_row = sql.execute(
        "SELECT id, offering_id FROM bots WHERE telegram_bot_id = %s", (tg_bot,)).fetchone()
    repo_fetcher.add(REPO, COMMIT)
    created: list[Student] = []
    for i in range(1, students + 1):
        import_students(client, ADMIN_TOKEN, offering["code"], [{
            "name": f"Pessoa Fechamento {n} {chr(64 + i)}", "ra": f"59{n:04d}{i:04d}",
            "email": f"pessoa.fechamento.{n}.{i}@turma.example.org"}])
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


def _mark_submit(world: World, student: Student, msg: Any, key: str, *, wait_mark: bool = True) -> tuple[Any, Any]:
    """Marcar e enviar: toca a alternativa `key` (ação `m`) e, no mesmo teclado
    (a marcação não muda a versão dos controles), o "Enviar" (`s`). Com `wait_mark`, espera a marcação
    ser processada antes do "Enviar" (sem isso, só a ordem dos updates as separa). Devolve os dois
    toques."""
    mark = _tap(world, student, msg, "m", key)
    if wait_mark:
        _wait(lambda: (_update(world, mark.update_id) or {}).get("state") == "processed",
              f"a marcação de ({key}) processada")
    submit = _tap(world, student, msg, "s")
    return mark, submit


def _feedback(verdict: str, n: int) -> str:
    """O feedback de nota parcial: o veredito seguido da nota, "{verdict} · nota N%"."""
    return f"{verdict} · {SCORE.format(n=n)}"


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


def _assert_choice_echo(text: str | None, options: Mapping[str, str], chosen: Iterable[str]) -> None:
    """o eco do feedback (entre "Sua resposta:" e o veredito) traz a chave (símbolo
    isolado) e o texto de cada alternativa escolhida, e nem a chave nem o texto de uma não escolhida; sem
    fixar ordem, separador nem pontuação ([I]: a forma do eco de uma escolha não está fixada no
    produto). O enunciado (com todas as alternativas) e o gabarito revelado (que traz "Azul")
    ficam fora da conferência."""
    match = _ECHO.search(text or "")
    assert match, f"feedback sem 'Sua resposta: {{eco}} · {{veredito}}': {text!r}"
    echo = match.group("echo")
    picked = set(chosen)
    for key, label in options.items():
        has_key = re.search(rf"(?<![0-9A-Za-zÀ-ÿ]){re.escape(key)}(?![0-9A-Za-zÀ-ÿ])", echo) is not None
        if key in picked:
            assert has_key and label in echo, f"o eco {echo!r} não traz a alternativa escolhida ({key}) {label}"
        else:
            assert not has_key and label not in echo, (
                f"o eco {echo!r} traz a alternativa não escolhida ({key}) {label}")


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


# --- transição, contagens, revelação e atrasada ---------------------------------------------------------

def test_close_transitions_reveals_without_engine_and_accepts_late_answer(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O gabarito ao fechar a janela e as atrasadas com métrica dedicada: no horário,
    `open` → `closed` com `dispatch.closed {trigger: window, counts}` exato; `delivered` sem resposta →
    `no_answer`; toda entrega entregue é editada a partir do que guardou, **com o motor parado**; as
    `answered`/`dont_know` são reveladas (`revealed_at = closed_at`): sem "Não sei", com `CLOSED_NOTICE`,
    o gabarito e a explicação permitidos (a sentinela aparece: controle positivo); a `no_answer` fica
    sem `revealed_at` e recebe `CLOSED_NOTICE`, os controles de resposta (marcar, "Enviar") **com** o
    "Não sei" e o `LATE_HOWTO`, sem a sentinela nem a explicação; a sentinela não aparece em
    nenhuma chamada à Bot API anterior ao fechamento, nem à `no_answer` antes da atrasada; `response_ms`
    do envio aceito ao `received_at` do "Enviar"; a atrasada por botão (marcar e enviar) é
    corrigida com `late = true`, `answered_late` e o feedback que compõe com o gabarito, revelado
    nessa edição (`revealed_at = graded_at`)."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 3)
    s1, s2, s3 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    sent_at = _delivery(world, opened, s1)["sent_at"]
    assert sent_at == clock.now()

    clock.advance(7)
    _mark_submit(world, s1, _question(world, opened, s1), "b")  # marcar e enviar
    _settle(world)
    _tap(world, s2, _question(world, opened, s2), "n")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    (sub1,) = _submissions(world, d1)
    assert (d1["state"], sub1["outcome"], sub1["valid"], sub1["late"], sub1["score"]) == (
        "answered", "graded", True, False, 1.0)
    assert sub1["response_ms"] == 7000  # envio aceito → `received_at`
    assert sub1["raw_input"] == {"resposta": "b"}

    engine_fake.stop()  # a transição e a revelação não chamam o motor
    closed_at = _close(world, opened)
    _settle(world)

    rows = {s.index: _delivery(world, opened, s) for s in world.students}
    assert [(rows[i]["state"], rows[i]["revealed_at"]) for i in (1, 2, 3)] == [
        ("answered", closed_at), ("dont_know", closed_at), ("no_answer", None)]  # a no_answer sem revelação
    assert (rows[3]["current_field"], rows[3]["answered_at"]) == (0, None)
    assert [_strip(_events(world, "dispatch.closed", dispatch=opened))] == [[
        _closed_event(world, opened, closed_at, _counts(answered=1, dont_know=1, no_answer=1))]]

    for student in world.students:
        msg = _question(world, opened, student)
        actions = {a for a, _f, _k, _d in _buttons(msg)}
        assert texts.CLOSED_NOTICE in msg.plain_text, student.index
        if student is s3:  # a no_answer continua com o "Não sei" e sem o gabarito
            assert "n" in actions, "o 'Não sei' não continua oferecido à no_answer"
            assert ANSWER_SENTINEL not in msg.plain_text, "o gabarito chegou à no_answer no fechamento"
            assert COLOR_EXPLANATION not in msg.plain_text, "a explicação chegou à no_answer no fechamento"
            continue
        assert "n" not in actions, f"'Não sei' continua oferecido ao aluno {student.index}"
        assert ANSWER_SENTINEL in msg.plain_text  # controle positivo: o gabarito permitido saiu
        assert COLOR_EXPLANATION in msg.plain_text
    late_msg = _question(world, opened, s3)
    assert texts.LATE_HOWTO in late_msg.plain_text
    late_actions = {a for a, _f, _k, _d in _buttons(late_msg)}
    assert sorted(k for a, _f, k, _d in _buttons(late_msg) if a == "m") == ["a", "b", "c", "d"]  # marcar e enviar
    assert {"s", "n"} <= late_actions, late_actions  # o "Enviar" e o "Não sei"
    assert texts.VERDICT_CORRECT in _question(world, opened, s1).plain_text
    _assert_choice_echo(_question(world, opened, s1).plain_text, COLORS, ["b"])  # a revelação mantém o eco
    assert texts.DONT_KNOW_RECORDED in _question(world, opened, s2).plain_text
    for student in (s1, s2):
        assert _buttons(_question(world, opened, student)) == [], "entrega encerrada sem controles"

    before = [c.seq for c in rich_bot_api.calls() if c.clock_at < closed_at and ANSWER_SENTINEL in _params_text(c)]
    after = [c.seq for c in rich_bot_api.calls() if c.clock_at >= closed_at and ANSWER_SENTINEL in _params_text(c)]
    assert before == [], "a sentinela do gabarito chegou ao aluno antes do dispatch.closed"
    assert after, "controle positivo: a revelação leva a sentinela"
    to_silent = [c.seq for c in rich_bot_api.calls()
                 if c.params.get("chat_id") == s3.chat_id and ANSWER_SENTINEL in _params_text(c)]
    assert to_silent == [], "a sentinela chegou à no_answer antes da atrasada"

    engine_fake.start()
    clock.advance(3)
    _mark_submit(world, s3, _question(world, opened, s3), "b")  # marcar e enviar, atrasada
    _settle(world)
    d3 = _delivery(world, opened, s3)
    (sub3,) = _submissions(world, d3)
    assert (d3["state"], d3["answered_at"], d3["current_field"], d3["revealed_at"]) == (
        "answered_late", sub3["received_at"], None, sub3["graded_at"])  # revelada na correção
    assert (sub3["outcome"], sub3["valid"], sub3["late"], sub3["score"]) == ("graded", True, True, 1.0)
    late_msg = _question(world, opened, s3)
    for fragment in (texts.VERDICT_CORRECT, ANSWER_SENTINEL, texts.CLOSED_NOTICE):
        assert fragment in late_msg.plain_text, fragment
    _assert_choice_echo(late_msg.plain_text, COLORS, ["b"])  # o feedback da atrasada com a alternativa
    assert "n" not in {a for a, _f, _k, _d in _buttons(late_msg)}

    graded = _events(world, "submission.graded", "delivery.dont_know", "dispatch.closed", dispatch=opened)
    d2 = rows[2]
    (sub2,) = _submissions(world, d2)
    assert sub2["outcome"] == "dont_know"
    assert _strip(graded) == [
        _graded_event(world, opened, s1, d1, sub1, late=False, score=1.0,
                      parts=[_choice_part(0, 1.0, "Azul")]),
        {"type": "delivery.dont_know", "occurred_at": iso(sub2["created_at"]), "actor_token_id": None,
         "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                  "delivery_id": str(d2["delivery_uuid"]), "submission_id": str(sub2["submission_uuid"]),
                  "person": s2.pseudo_id}},
        _closed_event(world, opened, closed_at, _counts(answered=1, dont_know=1, no_answer=1)),
        _graded_event(world, opened, s3, d3, sub3, late=True, score=1.0,
                      parts=[_choice_part(0, 1.0, "Azul")]),
    ]


def test_response_time_goes_to_last_part_and_keeps_negative_values(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """`response_ms` = `received_at` do update que completou a submissão
    válida (a última parte) − `sent_at`; com o relógio voltando, o valor negativo é gravado (a marca),
    e a submissão continua válida e corrigida. Cada parte da escolha única é marcada e enviada;
    o relógio avança entre a marcação e o "Enviar" da última parte, e a medida é a do "Enviar"."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Duas escolhas", "Responda às duas partes.",
                            (single_choice_field("cor"), single_choice_field("tom", correct="c")))
    opened = _open(world, "multipart", question)
    sent_at = _delivery(world, opened, s1)["sent_at"]

    clock.advance(3)
    _mark_submit(world, s1, _question(world, opened, s1), "b")
    _settle(world)
    _tap(world, s1, _question(world, opened, s1), "m", "c")  # a marcação da última parte
    _settle(world)
    clock.advance(5)
    _tap(world, s1, _question(world, opened, s1), "s")  # o "Enviar" completa a submissão: 3 + 5 s
    _settle(world)

    clock.set(sent_at - timedelta(seconds=2))
    _mark_submit(world, s2, _question(world, opened, s2), "b")
    _settle(world)
    _mark_submit(world, s2, _question(world, opened, s2), "c")
    _settle(world)

    results = []
    for student in (s1, s2):
        delivery = _delivery(world, opened, student)
        (sub,) = _submissions(world, delivery)
        results.append((delivery["state"], sub["outcome"], sub["valid"], sub["late"], sub["response_ms"],
                        sub["raw_input"]))
    assert results == [
        ("answered", "graded", True, False, 8000, {"cor": "b", "tom": "c"}),
        ("answered", "graded", True, False, -2000, {"cor": "b", "tom": "c"}),
    ]


def test_reveal_task_never_reveals_no_answer(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Só quem já respondeu recebe o gabarito: a tarefa `delivery.reveal` (a rede de segurança
    da revelação) nunca revela
    uma `no_answer`. Disparo fechado pelo roteiro, com uma `answered` e uma `no_answer`; depois do
    fechamento assentado, o `revealed_at` das **duas** é anulado por SQL (o que tira a dependência do
    que o fechamento gravou) e a tarefa é enfileirada para as duas pelo `enqueue` direto (o tratador, não
    a regra; a varredura pode enfileirar outra, e nenhuma pode revelar a `no_answer`). Só o que vem
    depois da marca conta: a `no_answer` continua sem `revealed_at`, sem edição nova e sem a sentinela; a
    `answered` recebe `revealed_at` e a edição com a sentinela (controle positivo)."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    # Resposta por texto ("responder"): o caso só depende do gabarito, não do teclado de confirmação.
    _reply(world, s1, _question(world, opened, s1).message_id, "b")
    _settle(world)
    _close(world, opened)
    _settle(world)
    assert [_delivery(world, opened, s)["state"] for s in (s1, s2)] == ["answered", "no_answer"]

    rows = {s.index: _delivery(world, opened, s)["id"] for s in world.students}
    mark = max((c.seq for c in rich_bot_api.calls()), default=0)
    now = clock.now()
    with psycopg.connect(settings.database_url) as conn:
        with conn.transaction():
            conn.execute("UPDATE deliveries SET revealed_at = NULL WHERE dispatch_id = %s", (opened.row,))
            for row in rows.values():
                enqueue_in_transaction(conn, "delivery.reveal", {"delivery_row_id": row}, now=now)
    _settle(world)

    after = [c for c in rich_bot_api.calls() if c.seq > mark]
    silent = _delivery(world, opened, s2)
    assert (silent["state"], silent["revealed_at"]) == ("no_answer", None), "a tarefa revelou a no_answer"
    assert [c.seq for c in after if c.params.get("chat_id") == s2.chat_id] == [], (
        "a tarefa delivery.reveal editou a no_answer")
    assert ANSWER_SENTINEL not in _question(world, opened, s2).plain_text
    answered = _delivery(world, opened, s1)
    assert answered["revealed_at"] is not None  # controle positivo: a answered é revelada pela tarefa
    assert [c.seq for c in after if c.params.get("chat_id") == s1.chat_id and ANSWER_SENTINEL in _params_text(c)], (
        "controle positivo: a revelação da answered leva a sentinela")


def test_late_answer_reveals_key_once_and_close_edit_had_none(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Só quem já respondeu recebe o gabarito, com
    a nota em toda correção válida: questão de número com explicação e gabarito mostrável, não de
    variante única; o aluno não responde, o disparo fecha, e a resposta atrasada (por "responder") está
    errada (nota 0). A edição de fechamento vem sem a sentinela e sem a explicação; a edição do feedback
    da atrasada traz "❌ incorreta · nota 0%", `CLOSED_NOTICE`, a sentinela e a explicação, com
    `revealed_at = graded_at`; nenhuma chamada leva a sentinela entre o `closed_at` e o `received_at` da
    atrasada, e o gabarito chega ao aluno uma só vez."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Velocidade média", "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?",
                            (number_field(),), explanation=NUMBER_EXPLANATION)
    opened = _open(world, "number", question)  # o qid da árvore de teste, com a questão programada aqui
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)

    closing = _question(world, opened, s1)
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["revealed_at"]) == ("no_answer", None)
    assert texts.CLOSED_NOTICE in closing.plain_text
    assert ANSWER_SENTINEL not in closing.plain_text, "o gabarito chegou à no_answer no fechamento"
    assert NUMBER_EXPLANATION not in closing.plain_text, "a explicação chegou à no_answer no fechamento"

    clock.advance(3)
    late_update = _reply(world, s1, closing.message_id, "7")
    _settle(world)
    received_at = _update(world, late_update)["received_at"]
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["late"], sub["score"]) == (
        "answered_late", "graded", True, True, 0.0)
    assert d1["revealed_at"] == sub["graded_at"]
    feedback = _question(world, opened, s1)
    for fragment in (_feedback(texts.VERDICT_INCORRECT, 0), texts.CLOSED_NOTICE, ANSWER_SENTINEL,
                     NUMBER_EXPLANATION):
        assert fragment in feedback.plain_text, fragment
    between = [c.seq for c in rich_bot_api.calls()
               if closed_at <= c.clock_at < received_at and ANSWER_SENTINEL in _params_text(c)]
    assert between == [], "a sentinela saiu entre o fechamento e a resposta atrasada"
    with_key = [c.seq for c in rich_bot_api.calls()
                if c.params.get("chat_id") == s1.chat_id and ANSWER_SENTINEL in _params_text(c)]
    assert len(with_key) == 1, f"o gabarito chegou ao aluno {len(with_key)} vezes (esperada uma, a do feedback)"


# --- o gabarito permitido --------------------------------------------------------------------------------

def test_close_without_show_correct_answer_still_edits_without_key(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Mensagem no fechamento sem gabarito: com `showCorrectAnswer` falso, a mensagem é editada
    assim mesmo (com `CLOSED_NOTICE`; sem "Não sei" nas encerradas; com ele, os controles e o
    `LATE_HOWTO` na `no_answer`, que fica sem `revealed_at`), sem gabarito nem explicação: a
    sentinela nunca chega ao aluno. A resposta no prazo é marcar e enviar."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "no_show_correct_answer", "no_show_correct_answer")
    _mark_submit(world, s1, _question(world, opened, s1), "a")
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)

    assert [(_delivery(world, opened, s)["state"], _delivery(world, opened, s)["revealed_at"])
            for s in (s1, s2)] == [("answered", closed_at), ("no_answer", None)]
    for student in (s1, s2):
        msg = _question(world, opened, student)
        assert msg.edit_count >= 1
        assert texts.CLOSED_NOTICE in msg.plain_text
        # o "Não sei" fica só na no_answer (s2).
        assert ("n" in {a for a, _f, _k, _d in _buttons(msg)}) is (student is s2), student.index
        assert HIDDEN_EXPLANATION not in msg.plain_text
    assert texts.VERDICT_INCORRECT in _question(world, opened, s1).plain_text
    _assert_choice_echo(_question(world, opened, s1).plain_text, COLORS, ["a"])  # a errada, e não a certa
    assert texts.LATE_HOWTO in _question(world, opened, s2).plain_text
    leaked = [c.seq for c in rich_bot_api.calls() if ANSWER_SENTINEL in _params_text(c)]
    assert leaked == [], "a sentinela saiu numa questão sem gabarito mostrável"


def test_close_reveals_only_fields_with_can_show(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O gabarito de cada campo só aparece com `can_show`; o campo com a
    exibição desligada não é revelado a ninguém (a questão não tem explicação). O único aluno fica
    `no_answer`, e o fechamento não lhe revela nada (`revealed_at` nulo, nenhum dos dois gabaritos); a
    atrasada (as duas partes marcadas e enviadas) revela, na edição do feedback, só o gabarito do
    campo com `can_show` (controle positivo)."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Duas partes", "Escolha a cor e depois o tom.",
                            (single_choice_field("cor", display=VISIBLE_KEY),
                             single_choice_field("tom", correct="c", display=HIDDEN_KEY, can_show=False)))
    opened = _open(world, "multipart", question)
    before = _question(world, opened, s1)
    assert VISIBLE_KEY not in before.plain_text and HIDDEN_KEY not in before.plain_text
    _close(world, opened)
    _settle(world)

    msg = _question(world, opened, s1)
    closed = _delivery(world, opened, s1)
    assert (closed["state"], closed["revealed_at"]) == ("no_answer", None)  # só quem respondeu recebe o gabarito
    assert VISIBLE_KEY not in msg.plain_text, "o gabarito chegou à no_answer no fechamento"
    assert HIDDEN_KEY not in msg.plain_text

    _mark_submit(world, s1, msg, "b")  # a atrasada, parte 1
    _settle(world)
    _mark_submit(world, s1, _question(world, opened, s1), "c")  # parte 2
    _settle(world)
    late = _delivery(world, opened, s1)
    assert late["state"] == "answered_late"
    assert late["revealed_at"] is not None
    msg = _question(world, opened, s1)
    assert VISIBLE_KEY in msg.plain_text  # controle positivo: revelado na edição do feedback
    assert HIDDEN_KEY not in msg.plain_text
    hidden = [c.seq for c in rich_bot_api.calls() if HIDDEN_KEY in _params_text(c)]
    assert hidden == [], "o gabarito de um campo sem can_show chegou ao aluno"


def test_single_variant_reveals_key_only_with_full_score(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Na variante única, o gabarito (e a
    explicação) só aparece na entrega com nota ≥ 1; sem submissão válida, não aparece; a atrasada com
    nota ≥ 1 revela na mesma edição do feedback (`revealed_at = graded_at`; a `no_answer` não é
    revelada no fechamento); a sentinela nunca chega à entrega com nota < 1. As respostas são
    marcar e enviar: a edição da marcação vem antes da do feedback."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 3)
    s1, s2, s3 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_variant", "single_variant")
    _mark_submit(world, s1, _question(world, opened, s1), "b")
    _mark_submit(world, s2, _question(world, opened, s2), "a")
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)

    states = [(_delivery(world, opened, s)["state"], _delivery(world, opened, s)["revealed_at"])
              for s in world.students]
    assert states == [("answered", closed_at), ("answered", closed_at), ("no_answer", None)]  # só quem respondeu
    right, wrong, silent = (_question(world, opened, s) for s in world.students)
    assert ANSWER_SENTINEL in right.plain_text and SINGLE_VARIANT_EXPLANATION in right.plain_text
    _assert_choice_echo(right.plain_text, COLORS, ["b"])
    _assert_choice_echo(wrong.plain_text, COLORS, ["a"])
    for msg in (wrong, silent):
        assert texts.CLOSED_NOTICE in msg.plain_text
        assert ANSWER_SENTINEL not in msg.plain_text
        assert SINGLE_VARIANT_EXPLANATION not in msg.plain_text

    clock.advance(5)
    edits_before = len(rich_bot_api.edits(world.tg_bot, s3.chat_id))
    _mark_submit(world, s3, silent, "b")
    _settle(world)
    late = _delivery(world, opened, s3)
    assert late["state"] == "answered_late"
    (late_sub,) = _submissions(world, late)
    assert late["revealed_at"] == late_sub["graded_at"]  # revelada na correção da atrasada
    new_edits = rich_bot_api.edits(world.tg_bot, s3.chat_id)[edits_before:]
    with_feedback = [e for e in new_edits if texts.VERDICT_CORRECT in (e.after.plain_text or "")]
    assert len(with_feedback) >= 1
    assert ANSWER_SENTINEL in with_feedback[0].after.plain_text  # mesma edição do feedback
    _assert_choice_echo(with_feedback[0].after.plain_text, COLORS, ["b"])

    to_wrong = [c.seq for c in rich_bot_api.calls()
                if c.params.get("chat_id") == s2.chat_id and ANSWER_SENTINEL in _params_text(c)]
    assert to_wrong == [], "a sentinela chegou à entrega de variante única com nota < 1"


def test_late_partial_answer_composes_with_the_revealed_key(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Feedback de nota parcial na atrasada, com as marcas por alternativa escolhida:
    múltipla (`net-correct`, gabarito a e c, mostrável); o aluno não
    responde no prazo, o disparo fecha, e ele marca só "a" e toca em "Enviar" (nota 0,5). Uma só edição
    traz o eco com a marca "(a) Dois ✅", "◐ parcialmente correta · nota 50%", `CLOSED_NOTICE` e o
    gabarito (a sentinela), sem controles; a submissão é `late = true`, e o gabarito é revelado nessa
    edição (`revealed_at = graded_at`)."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "multiple_choice", "multiple_choice")
    _settle(world)
    _close(world, opened)
    _settle(world)
    assert _delivery(world, opened, s1)["state"] == "no_answer"

    edits_before = len(rich_bot_api.edits(world.tg_bot, s1.chat_id))
    clock.advance(3)
    _mark_submit(world, s1, _question(world, opened, s1), "a")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["late"], sub["score"]) == (
        "answered_late", "graded", True, True, 0.5)
    new_edits = rich_bot_api.edits(world.tg_bot, s1.chat_id)[edits_before:]
    with_feedback = [e for e in new_edits if VERDICT_PARTIAL in (e.after.plain_text or "")]
    assert len(with_feedback) == 1, [(e.after.plain_text or "")[-120:] for e in new_edits]
    text = with_feedback[0].after.plain_text
    for fragment in (_feedback(VERDICT_PARTIAL, 50), texts.CLOSED_NOTICE, ANSWER_SENTINEL):
        assert fragment in text, fragment
    assert re.search(r"\(a\) Dois ✅", text), f"sem a marca da alternativa escolhida: {text!r}"
    _assert_choice_echo(text, PRIMES, ["a"])
    assert with_feedback[0].after.buttons() == []
    assert d1["revealed_at"] == sub["graded_at"]  # o gabarito aparece na edição do feedback


def test_single_variant_partial_score_shows_partial_and_no_key(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Feedback de nota parcial na variante única, com as marcas por alternativa escolhida:
    múltipla de variante única; o aluno marca só "a" e envia no
    prazo (nota 0,5): o feedback traz "(a) Dois ✅" e "◐ parcialmente correta · nota 50%"; o disparo
    fecha, e a edição de fechamento mantém o resultado parcial, com `CLOSED_NOTICE`, sem o gabarito (a
    sentinela) e sem a explicação (regra nota ≥ 1 da variante única); a sentinela nunca
    chega ao aluno."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    question = FakeQuestion("Primos de variante única", "Marque os números primos.", (multiple_choice_field(),),
                            single_variant=True, explanation=PRIME_EXPLANATION)
    opened = _open(world, "single_variant", question)  # o qid da árvore de teste, com a múltipla programada aqui
    _settle(world)
    _mark_submit(world, s1, _question(world, opened, s1), "a")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (d1["state"], sub["outcome"], sub["valid"], sub["late"], sub["score"]) == (
        "answered", "graded", True, False, 0.5)
    graded = _question(world, opened, s1).plain_text
    assert _feedback(VERDICT_PARTIAL, 50) in graded, graded
    assert re.search(r"\(a\) Dois ✅", graded), f"sem a marca da alternativa escolhida: {graded!r}"

    closed_at = _close(world, opened)
    _settle(world)
    msg = _question(world, opened, s1)
    assert texts.CLOSED_NOTICE in msg.plain_text
    assert _feedback(VERDICT_PARTIAL, 50) in msg.plain_text
    _assert_choice_echo(msg.plain_text, PRIMES, ["a"])
    assert ANSWER_SENTINEL not in msg.plain_text and PRIME_EXPLANATION not in msg.plain_text
    leaked = [c.seq for c in rich_bot_api.calls()
              if c.params.get("chat_id") == s1.chat_id and ANSWER_SENTINEL in _params_text(c)]
    assert leaked == [], "a sentinela chegou à variante única com nota < 1"
    assert (_delivery(world, opened, s1)["state"], _delivery(world, opened, s1)["revealed_at"]) == (
        "answered", closed_at)


# --- a barreira ------------------------------------------------------------------------------------------

def test_barrier_waits_for_pending_update_and_late_entry_survives_rounds(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com um update `pending` gravado até
    `closes_at`, cada rodada `dispatch.close` termina `done` sem efeito (nenhum `task.failed`) e o
    disparo continua `open`; uma entrada gravada depois do horário espera a transição por mais de
    `SWEEP_MAX_UPDATE_ROUNDS` rodadas sem ser abandonada (as rodadas `done` não contam) e, depois do
    fechamento, vira `answered_late`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    _settle(world)

    stranger = make_user(90)
    _mark_known_chat(rich_bot_api, world.tg_bot, stranger.user_id)
    clock.set(opened.closes_at - timedelta(seconds=3))
    _settle(world, updates=False)
    phantom_row, phantom_task = _phantom_update(world, stranger.user_id)

    clock.set(opened.closes_at + timedelta(seconds=1))
    question = _question(world, opened, s2)
    late_update = _reply(world, s2, question.message_id, "b")
    _wait(lambda: _update(world, late_update) is not None, "o leitor gravou a resposta atrasada")
    late = _update(world, late_update)
    assert late["received_at"] > opened.closes_at

    subject = {"update_row_id": late["id"]}
    rounds = SWEEP_MAX_UPDATE_ROUNDS + 2
    for k in range(1, rounds + 1):
        _wait(lambda k=k: sum(1 for s, _e in _rounds(world, "telegram.update", subject) if s == "done") >= k,
              f"a rodada {k} da entrada atrasada")
        assert _dispatch_state(world, opened) == "open"
        clock.advance(RECHECK)
    assert _update(world, late_update)["state"] == "pending", "a entrada atrasada foi abandonada"
    assert _submissions(world, _delivery(world, opened, s2)) == []
    close_rounds = _rounds(world, "dispatch.close", {"dispatch_row_id": opened.row})
    assert len(close_rounds) >= 2 and all(state == "done" for state, _e in close_rounds), close_rounds
    assert [e for e in _events(world, "task.failed")] == []

    _free_phantom(world, phantom_task)
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    _advance_until(world, lambda: _delivery(world, opened, s2)["state"] == "answered_late",
                   "a atrasada depois do fechamento", step=RECHECK)
    _settle(world)

    assert _update(world, late_update)["outcome"] == "answer_recorded"
    assert all(state == "done" for state, _e in _rounds(world, "telegram.update", subject))
    d2 = _delivery(world, opened, s2)
    (sub,) = _submissions(world, d2)
    assert (sub["late"], sub["valid"], sub["received_at"]) == (True, True, late["received_at"])
    # a atrasada é revelada na correção; a no_answer, nunca no fechamento.
    assert (d2["state"], d2["revealed_at"]) == ("answered_late", sub["graded_at"])
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["revealed_at"]) == ("no_answer", None)
    phantom = sql.execute("SELECT state FROM telegram_updates WHERE id = %s", (phantom_row,)).fetchone()[0]
    assert phantom == "processed"
    order = [e["type"] for e in _events(world, "dispatch.closed", "submission.graded", dispatch=opened)]
    assert order == ["dispatch.closed", "submission.graded"]


def test_barrier_waits_for_a_poll_after_closes_at(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Espera da barreira sem consumir tentativas: com o bot sendo consultado, o fechamento espera
    `last_poll_ok_at >= closes_at`; com o `getUpdates` falhando depois do horário, as rodadas terminam
    `done` sem efeito e o disparo continua `open`; a primeira consulta `ok` libera o fechamento."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    _settle(world)
    clock.set(opened.closes_at - timedelta(seconds=2))
    _wait(lambda: _last_poll(world) == clock.now(), "uma consulta ok antes do horário")

    polls = len(rich_bot_api.calls("getUpdates"))
    rich_bot_api.fail("getUpdates", status=502, times=None)
    _wait(lambda: any(c.status == 502 for c in rich_bot_api.calls("getUpdates")[polls:]),
          "uma consulta falha (a que estava em curso terminou)")
    last_ok = _last_poll(world)
    assert last_ok < opened.closes_at

    clock.set(opened.closes_at + timedelta(seconds=1))
    subject = {"dispatch_row_id": opened.row}
    for k in (1, 2):
        _wait(lambda k=k: sum(1 for s, _e in _rounds(world, "dispatch.close", subject) if s == "done") >= k,
              f"a rodada {k} do fechamento")
        assert _dispatch_state(world, opened) == "open"
        assert _last_poll(world) == last_ok
        clock.advance(RECHECK)
    assert all(state == "done" for state, _e in _rounds(world, "dispatch.close", subject))
    assert _events(world, "dispatch.closed", dispatch=opened) == []

    rich_bot_api.clear_failures()
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    assert _last_poll(world) >= opened.closes_at
    assert _delivery(world, opened, world.students[0])["state"] == "no_answer"
    assert _strip(_events(world, "dispatch.closed", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(no_answer=1))]
    assert [e for e in _events(world, "task.failed")] == []


# --- entregas pendentes no fechamento -------------------------------------------------------------------

def test_pending_without_variant_becomes_not_delivered_engine_unavailable(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Com o motor fora do ar desde a abertura, a entrega fica `pending`
    sem variante e, no fechamento, vira `not_delivered/engine_unavailable`, com o
    `delivery.not_delivered` antes do `dispatch.closed`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)

    def no_variant(opened: Opened) -> bool:
        tries = sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'delivery.prepare' AND "
                            "attempts >= 1 AND state IN ('pending', 'failed')").fetchone()[0]
        return tries >= 1 and _delivery(world, opened, s1)["variant_at"] is None

    opened = _open(world, "single_choice", "single_choice", until=no_variant,
                   before_confirm=engine_fake.stop)
    closed_at = _close(world, opened)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["undelivered_reason"], d1["variant_at"], d1["current_field"]) == (
        "not_delivered", "engine_unavailable", None, None)
    assert _messages(world, d1) == []
    assert _strip(_events(world, "delivery.not_delivered", "dispatch.closed", dispatch=opened)) == [
        {"type": "delivery.not_delivered", "occurred_at": iso(closed_at), "actor_token_id": None,
         "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                  "delivery_id": str(d1["delivery_uuid"]), "person": s1.pseudo_id,
                  "undelivered_reason": "engine_unavailable"}},
        _closed_event(world, opened, closed_at, _counts(not_delivered=1)),
    ]


def test_pending_with_message_in_queue_becomes_channel_unavailable(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """A `question` recusada com 5xx e ainda na fila (espera
    maior que a janela) no horário → `not_delivered/channel_unavailable`, e as mensagens `pending` da
    entrega vão a `cancelled` na transição; a outra entrega segue para `no_answer`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    slow_retry = dataclasses.replace(settings, backoff_base_seconds=3600, backoff_max_seconds=3600)
    _start_bots(world, bot_worker_thread, settings=slow_retry)
    rich_bot_api.fail("sendRichMessage", status=502, times=None,
                      when=lambda call: call.params.get("chat_id") == s1.chat_id)

    def one_pending(opened: Opened) -> bool:
        rows = sql.execute("SELECT d.state FROM deliveries d WHERE d.dispatch_id = %s ORDER BY d.id",
                           (opened.row,)).fetchall()
        if sorted(r[0] for r in rows) != ["delivered", "pending"]:
            return False
        question = [m for m in _messages(world, _delivery(world, opened, s1)) if m["delivery_role"] == "question"]
        return bool(question) and (question[0]["state"], question[0]["attempts"]) == ("pending", 1)

    opened = _open(world, "single_choice", "single_choice", until=one_pending)
    closed_at = _close(world, opened)
    _settle(world)

    d1, d2 = _delivery(world, opened, s1), _delivery(world, opened, s2)
    assert (d1["state"], d1["undelivered_reason"], d1["current_field"], d1["sent_at"]) == (
        "not_delivered", "channel_unavailable", None, None)
    assert d2["state"] == "no_answer"
    assert {m["state"] for m in _messages(world, d1)} == {"cancelled"}
    assert all(m.chat_id != s1.chat_id for m in rich_bot_api.messages(world.tg_bot))
    assert _strip(_events(world, "delivery.not_delivered", "dispatch.closed", dispatch=opened)) == [
        {"type": "delivery.not_delivered", "occurred_at": iso(closed_at), "actor_token_id": None,
         "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                  "delivery_id": str(d1["delivery_uuid"]), "person": s1.pseudo_id,
                  "undelivered_reason": "channel_unavailable"}},
        _closed_event(world, opened, closed_at, _counts(not_delivered=1, no_answer=1)),
    ]


def test_bot_without_access_until_close_leaves_messages_untouched(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Bot sem acesso: com o bot `no_access` da
    abertura ao fechamento, as mensagens da entrega ficam `pending` com `attempts = 0` (a posse não as
    toma) até o fechamento, que as cancela, e a entrega vira `not_delivered/bot_no_access`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)

    def queued(opened: Opened) -> bool:
        delivery = _delivery(world, opened, s1)
        return delivery["variant_at"] is not None and bool(_messages(world, delivery))

    # A confirmação acontece com o bot ainda `registered`; a perda de acesso vem logo depois.
    dispatch_id, closes_at = _prepare(world, "single_choice", "single_choice")
    world.engine.slow("create_variant", 5.0, times=None)
    confirm_dispatch(client, ADMIN_TOKEN, world.code, dispatch_id)
    rich_bot_api.revoke(world.tg_bot)
    _wait(lambda: sql.execute("SELECT state FROM bots WHERE id = %s", (world.bot_row,)).fetchone()[0]
          == "no_access", "o bot perdeu o acesso")
    row, ref_uuid = sql.execute(
        "SELECT s.id, r.ref_uuid FROM dispatches s JOIN question_refs r ON r.id = s.question_ref_id "
        "WHERE s.dispatch_uuid = %s", (dispatch_id,)).fetchone()
    opened = Opened(dispatch_id=dispatch_id, row=row, closes_at=closes_at, question_ref_id=str(ref_uuid))
    _wait(lambda: queued(opened), "as mensagens da entrega na fila")
    time.sleep(2.0)  # mais de uma volta do laço: a posse não pode tomar as mensagens
    d1 = _delivery(world, opened, s1)
    assert d1["state"] == "pending"
    assert [(m["state"], m["attempts"]) for m in _messages(world, d1)] == [("pending", 0)] * len(_messages(world, d1))

    closed_at = _close(world, opened)
    _settle(world, outbox=False)
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["undelivered_reason"], d1["sent_at"]) == ("not_delivered", "bot_no_access", None)
    assert [(m["state"], m["attempts"]) for m in _messages(world, d1)] == [("cancelled", 0)] * len(_messages(world, d1))
    assert [m for m in rich_bot_api.messages(world.tg_bot) if m.chat_id == s1.chat_id] == []
    assert _strip(_events(world, "delivery.not_delivered", "dispatch.closed", dispatch=opened)) == [
        {"type": "delivery.not_delivered", "occurred_at": iso(closed_at), "actor_token_id": None,
         "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                  "delivery_id": str(d1["delivery_uuid"]), "person": s1.pseudo_id,
                  "undelivered_reason": "bot_no_access"}},
        _closed_event(world, opened, closed_at, _counts(not_delivered=1)),
    ]


def test_bot_access_back_before_close_delivers_the_question(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """O acesso que volta antes do fechamento (token novo registrado)
    libera as mensagens da entrega, que saem pela fila; a entrega vira `delivered` e, no fechamento,
    `no_answer`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    dispatch_id, closes_at = _prepare(world, "single_choice", "single_choice")
    world.engine.slow("create_variant", 5.0, times=None)
    confirm_dispatch(client, ADMIN_TOKEN, world.code, dispatch_id)
    rich_bot_api.revoke(world.tg_bot)
    _wait(lambda: sql.execute("SELECT state FROM bots WHERE id = %s", (world.bot_row,)).fetchone()[0]
          == "no_access", "o bot perdeu o acesso")
    row, ref_uuid = sql.execute(
        "SELECT s.id, r.ref_uuid FROM dispatches s JOIN question_refs r ON r.id = s.question_ref_id "
        "WHERE s.dispatch_uuid = %s", (dispatch_id,)).fetchone()
    opened = Opened(dispatch_id=dispatch_id, row=row, closes_at=closes_at, question_ref_id=str(ref_uuid))
    _wait(lambda: _delivery(world, opened, s1)["variant_at"] is not None, "a variante gravada")
    assert _delivery(world, opened, s1)["state"] == "pending"

    register_bot(client, ADMIN_TOKEN, world.code, rich_bot_api.rotate_token(world.tg_bot))
    _wait(lambda: _delivery(world, opened, s1)["state"] == "delivered", "a entrega depois do acesso")
    d1 = _delivery(world, opened, s1)
    assert d1["sent_at"] is not None and d1["principal_message_id"] is not None
    assert _question(world, opened, s1).method == "sendRichMessage"
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)
    assert _delivery(world, opened, s1)["state"] == "no_answer"
    assert _strip(_events(world, "dispatch.closed", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(no_answer=1))]


def test_question_accepted_after_close_edits_window_closed_without_transition(
        client: Any, sql: Any, clock: Any, settings: Any, bot_settings: Any, engine_fake: Any,
        repo_fetcher: Any, rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """A `question` em voo (`leased`) no fechamento conta como
    mensagem na fila (`not_delivered/channel_unavailable`); o envio aceito depois não é transição (sem
    `delivery.delivered`), a linha fica `sent` com o `channel_ref`, e sai uma edição dessa mensagem com
    a "questão + `CLOSED_NOTICE`" sem teclado nem gabarito. Dois workers: um fica preso no envio (a
    Bot API falsa segura o pedido), o outro fecha."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    long_timeout = dataclasses.replace(bot_settings, telegram_timeout_seconds=17)
    for name in ("cq-teste-w1", "cq-teste-w2"):
        _start_bots(world, bot_worker_thread, bot_settings=long_timeout,
                    settings=dataclasses.replace(settings, worker_id=name))
    rich_bot_api.fail("sendRichMessage", status=502, times=1,
                      when=lambda call: call.params.get("chat_id") == s1.chat_id)

    def retry_waiting(opened: Opened) -> bool:
        question = [m for m in _messages(world, _delivery(world, opened, s1)) if m["delivery_role"] == "question"]
        return bool(question) and (question[0]["state"], question[0]["attempts"]) == ("pending", 1)

    opened = _open(world, "single_choice", "single_choice", until=retry_waiting)
    hold, entered = threading.Event(), threading.Event()

    def hook(call: Any) -> None:
        if call.params.get("chat_id") == s1.chat_id:
            entered.set()
            hold.wait(14)

    rich_bot_api.on_call("sendRichMessage", hook)
    try:
        clock.set(opened.closes_at - timedelta(seconds=1))
        _wait(entered.is_set, "a question em voo")
        clock.set(opened.closes_at + timedelta(seconds=1))
        _close_wait(world, opened, timeout=10.0)
        d1 = _delivery(world, opened, s1)
        assert (d1["state"], d1["undelivered_reason"]) == ("not_delivered", "channel_unavailable")
    finally:
        hold.set()
        rich_bot_api.on_call("sendRichMessage", None)
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["undelivered_reason"], d1["sent_at"], d1["principal_message_id"]) == (
        "not_delivered", "channel_unavailable", None, None)
    question = [m for m in _messages(world, d1) if (m["delivery_role"], m["operation"]) == ("question", "send")]
    assert [m["state"] for m in question] == ["sent"]
    message_id = int(question[0]["channel_ref"])
    edits = rich_bot_api.edits(world.tg_bot, s1.chat_id, message_id)
    assert len(edits) == 1
    after = edits[0].after
    assert texts.CLOSED_NOTICE in after.plain_text and COLOR_STATEMENT in after.plain_text
    assert after.buttons() == []
    assert ANSWER_SENTINEL not in after.plain_text
    assert _events(world, "delivery.delivered", dispatch=opened) == []


# --- edição que falha no fechamento ----------------------------------------------------------------------

def test_close_edit_failures_follow_fr_046b(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Edição que falha no fechamento: mensagem apagada → mensagem nova em
    resposta à original com a renderização completa (`CLOSED_NOTICE`, controles com o "Não sei",
    `LATE_HOWTO`; sem gabarito: os cinco ficam `no_answer`), que passa a principal; "sem mudança"
    → sucesso, sem mensagem nova; 5xx e 429 → repetição, e a edição sai; 403 → só registro, sem
    mensagem nova."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 5)
    deleted, same, five_xx, limited, blocked = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    _settle(world)
    originals = {s.index: _question(world, opened, s).message_id for s in world.students}

    rich_bot_api.delete_message(world.tg_bot, deleted.user, originals[deleted.index])

    def chat(student: Student) -> Callable[[Any], bool]:
        return lambda call: call.params.get("chat_id") == student.chat_id

    status, description = DISPATCH_ERRORS["not_modified"]
    rich_bot_api.fail("editMessageText", status=status, description=description, times=1, when=chat(same))
    rich_bot_api.fail("editMessageText", status=502, times=1, when=chat(five_xx))
    rich_bot_api.fail("editMessageText", retry_after=1, times=1, when=chat(limited))
    rich_bot_api.fail("editMessageText", error="blocked", times=1, when=chat(blocked))

    closed_at = _close(world, opened)
    for _ in range(3):  # as esperas das repetições correm no relógio falso
        _settle(world)
        clock.advance(5)
    _settle(world)

    fallback = [m for m in rich_bot_api.messages(world.tg_bot, deleted.chat_id) if m.clock_at >= closed_at]
    assert len(fallback) == 1
    new = fallback[0]
    assert new.reply_parameters == {"message_id": originals[deleted.index], "allow_sending_without_reply": True}
    for fragment in (texts.CLOSED_NOTICE, texts.LATE_HOWTO, COLOR_STATEMENT):
        assert fragment in new.plain_text, fragment
    assert ANSWER_SENTINEL not in new.plain_text, "o gabarito chegou à no_answer no fechamento"
    assert sorted(k for a, _f, k, _d in _buttons(new) if a == "m") == ["a", "b", "c", "d"]  # marcar e enviar
    assert {"s", "n"} <= {a for a, _f, _k, _d in _buttons(new)}  # o "Enviar" e o "Não sei"
    d_deleted = _delivery(world, opened, deleted)
    assert _principal_id(world, d_deleted) == new.message_id
    roles = [(m["delivery_role"], m["operation"], m["state"]) for m in _messages(world, d_deleted)]
    assert ("fallback", "send", "sent") in roles

    assert _sent_to(world, same, since=closed_at) == []
    assert [m["state"] for m in _messages(world, _delivery(world, opened, same))
            if m["operation"] == "edit_text"] == ["sent"]

    for student in (five_xx, limited):
        msg = _question(world, opened, student)
        assert msg.message_id == originals[student.index]
        assert texts.CLOSED_NOTICE in msg.plain_text and ANSWER_SENTINEL not in msg.plain_text  # sem gabarito
        assert _sent_to(world, student, since=closed_at) == []

    assert _sent_to(world, blocked, since=closed_at) == []
    blocked_msg = rich_bot_api.current(world.tg_bot, blocked.chat_id, originals[blocked.index])
    assert blocked_msg.edit_count == 0
    d_blocked = _delivery(world, opened, blocked)
    assert [m["delivery_role"] for m in _messages(world, d_blocked)].count("fallback") == 0
    assert [m["state"] for m in _messages(world, d_blocked) if m["operation"] == "edit_text"] == ["failed"]
    assert d_blocked["principal_message_id"] is not None
    assert _principal_id(world, d_blocked) == originals[blocked.index]


# --- correção pendente no fechamento ----------------------------------------------------------------------

def test_grading_pending_at_close_outcome_comes_after_closed_with_reveal(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Eventos depois do fechamento: a entrega com correção pendente no
    fechamento não vira `no_answer` (conta como `delivered` no `counts`) nem é revelada; o desfecho sai
    depois do `dispatch.closed`, com o `submission.graded` (`late = false`), `revealed_at` e uma só
    edição com o feedback, o `CLOSED_NOTICE` e o gabarito permitido."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 1)
    (s1,) = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "single_choice", "single_choice")
    engine_fake.stop()
    _mark_submit(world, s1, _question(world, opened, s1), "b")  # marcar e enviar
    _wait(lambda: [s["outcome"] for s in _submissions(world, _delivery(world, opened, s1))] == ["grading"],
          "a submissão aguardando correção")
    _settle(world)

    closed_at = _close(world, opened)
    _settle(world)
    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["revealed_at"]) == ("delivered", None)
    assert _strip(_events(world, "dispatch.closed", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(delivered=1))]
    assert ANSWER_SENTINEL not in _question(world, opened, s1).plain_text
    edits_before = len(rich_bot_api.edits(world.tg_bot, s1.chat_id))

    engine_fake.start()
    _advance_until(world, lambda: _delivery(world, opened, s1)["state"] == "answered",
                   "o desfecho da correção pendente")
    _settle(world)
    d1 = _delivery(world, opened, s1)
    (sub,) = _submissions(world, d1)
    assert (sub["outcome"], sub["valid"], sub["late"]) == ("graded", True, False)
    assert d1["revealed_at"] == sub["graded_at"]
    new_edits = rich_bot_api.edits(world.tg_bot, s1.chat_id)[edits_before:]
    assert len(new_edits) == 1
    for fragment in (texts.VERDICT_CORRECT, texts.CLOSED_NOTICE, ANSWER_SENTINEL):
        assert fragment in new_edits[0].after.plain_text, fragment
    _assert_choice_echo(new_edits[0].after.plain_text, COLORS, ["b"])
    assert new_edits[0].after.buttons() == []
    assert _strip(_events(world, "dispatch.closed", "submission.graded", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(delivered=1)),
        _graded_event(world, opened, s1, d1, sub, late=False, score=1.0, parts=[_choice_part(0, 1.0, "Azul")]),
    ]


def test_broken_grading_around_close_leads_to_question_failed(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Revelação devida e a falha de questão: o desfecho `broken` de uma correção pendente no
    fechamento → `question_failed/grading_broken`, sem `revealed_at`, sem gabarito e sem controles; a
    correção atrasada `broken` de uma `no_answer` (sem `revealed_at`) →
    `question_failed/grading_broken`, também sem `revealed_at`; cada uma com o `delivery.question_failed`
    e o `QUESTION_FAILED`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "broken_on_grade", "broken_on_grade")
    _settle(world)  # o pedido do campo sai depois da `question` (ritmo por chat)
    prompt = {s.index: [m for m in rich_bot_api.messages(world.tg_bot, s.chat_id) if m.force_reply]
              for s in world.students}
    assert all(len(p) == 1 for p in prompt.values())
    engine_fake.stop()
    _reply(world, s1, prompt[s1.index][0].message_id, "6.5")
    _wait(lambda: [s["outcome"] for s in _submissions(world, _delivery(world, opened, s1))] == ["grading"],
          "a submissão aguardando correção")
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)
    engine_fake.start()
    _advance_until(world, lambda: _delivery(world, opened, s1)["state"] == "question_failed",
                   "o desfecho broken")
    _settle(world)

    d1 = _delivery(world, opened, s1)
    assert (d1["state"], d1["failure"], d1["revealed_at"], d1["current_field"]) == (
        "question_failed", "grading_broken", None, None)
    (sub1,) = _submissions(world, d1)
    assert (sub1["outcome"], sub1["failure_code"], sub1["failure_phase"]) == ("broken", "question_code_error", "grade")
    msg1 = _question(world, opened, s1)
    assert texts.QUESTION_FAILED in msg1.plain_text and texts.CLOSED_NOTICE in msg1.plain_text
    assert msg1.buttons() == [] and ANSWER_SENTINEL not in msg1.plain_text

    d2 = _delivery(world, opened, s2)
    assert (d2["state"], d2["revealed_at"]) == ("no_answer", None)  # só quem respondeu recebe o gabarito
    _reply(world, s2, _question(world, opened, s2).message_id, "6.5")
    _settle(world)
    d2 = _delivery(world, opened, s2)
    assert (d2["state"], d2["failure"], d2["revealed_at"], d2["current_field"]) == (
        "question_failed", "grading_broken", None, None)  # nunca revelada
    (sub2,) = _submissions(world, d2)
    assert (sub2["outcome"], sub2["late"]) == ("broken", True)
    assert texts.QUESTION_FAILED in _question(world, opened, s2).plain_text
    assert _question(world, opened, s2).buttons() == []

    events = _events(world, "dispatch.closed", "delivery.question_failed", dispatch=opened)
    assert [(e["type"], e["actor_token_id"], e["data"]) for e in events] == [
        ("dispatch.closed", None, _closed_event(world, opened, closed_at, _counts(delivered=1, no_answer=1))["data"]),
        ("delivery.question_failed", None,
         {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
          "delivery_id": str(d1["delivery_uuid"]), "person": s1.pseudo_id, "failure": "grading_broken"}),
        ("delivery.question_failed", None,
         {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
          "delivery_id": str(d2["delivery_uuid"]), "person": s2.pseudo_id, "failure": "grading_broken"}),
    ]
    assert all(_at(e["occurred_at"]) >= closed_at for e in events)


def test_internal_failure_and_invalid_outcome_after_close_reopen_as_no_answer(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any) -> None:
    """Falha interna depois do fechamento: correção pendente no fechamento que sai com
    400 → `submission.internal_failure` depois do `dispatch.closed`, a entrega `no_answer` sem
    `revealed_at` e sem gabarito, uma só edição com os controles de atrasada (com o "Não sei") e o
    `LATE_HOWTO`, `ANSWER_KEPT` uma vez; e um desfecho inválido (erro de formato) depois do fechamento →
    `no_answer`, também sem `revealed_at` nem gabarito; nas duas, uma atrasada depois → `answered_late`,
    com o gabarito revelado na correção."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread)
    opened = _open(world, "number", "number")
    _settle(world)  # o pedido do campo sai depois da `question` (ritmo por chat)
    prompts = {s.index: [m for m in rich_bot_api.messages(world.tg_bot, s.chat_id) if m.force_reply][0]
               for s in world.students}
    engine_fake.stop()
    _reply(world, s1, prompts[s1.index].message_id, "6.5")
    _reply(world, s2, prompts[s2.index].message_id, "abc")
    _wait(lambda: all([s["outcome"] for s in _submissions(world, _delivery(world, opened, st))] == ["grading"]
                      for st in world.students), "as duas submissões aguardando correção")
    _settle(world)
    closed_at = _close(world, opened)
    _settle(world)
    assert _strip(_events(world, "dispatch.closed", dispatch=opened)) == [
        _closed_event(world, opened, closed_at, _counts(delivered=2))]
    edits_before = {s.index: len(rich_bot_api.edits(world.tg_bot, s.chat_id)) for s in world.students}

    engine_fake.fail("grade", error=400, times=1,
                     when=lambda call: (call.request or {}).get("raw_submitted_answers") == {"resposta": "6.5"})
    engine_fake.start()
    _advance_until(world, lambda: all(_delivery(world, opened, st)["state"] == "no_answer"
                                      for st in world.students),
                   "os dois desfechos depois do fechamento")
    _settle(world)

    for student in world.students:
        delivery = _delivery(world, opened, student)
        assert (delivery["state"], delivery["current_field"]) == ("no_answer", 0)
        assert delivery["revealed_at"] is None, student.index  # a no_answer não é revelada
        after = _question(world, opened, student)
        for fragment in (texts.CLOSED_NOTICE, texts.LATE_HOWTO):
            assert fragment in after.plain_text, (student.index, fragment)
        assert ANSWER_SENTINEL not in after.plain_text, (student.index, "o gabarito chegou à no_answer")
        assert "n" in {a for a, _f, _k, _d in _buttons(after)}, student.index  # o "Não sei" fica
    s1_edits = rich_bot_api.edits(world.tg_bot, s1.chat_id)[edits_before[s1.index]:]
    assert len(s1_edits) == 1, "a falha interna depois do fechamento sai numa só edição"
    (sub1,) = _submissions(world, _delivery(world, opened, s1))
    assert (sub1["outcome"], sub1["failure_code"], sub1["late"]) == ("internal_failure", "invalid_request", False)
    (sub2,) = _submissions(world, _delivery(world, opened, s2))
    assert (sub2["outcome"], sub2["valid"], sub2["late"]) == ("graded", False, False)

    def kept_count() -> int:
        sends = [m for m in _sent_to(world, s1) if texts.ANSWER_KEPT in (m.plain_text or "")]
        edits = [e for e in rich_bot_api.edits(world.tg_bot, s1.chat_id)
                 if texts.ANSWER_KEPT in (e.after.plain_text or "")
                 and texts.ANSWER_KEPT not in (e.before.plain_text or "")]
        return len(sends) + len(edits)

    assert kept_count() == 1
    d1 = _delivery(world, opened, s1)
    events = _events(world, "dispatch.closed", "submission.internal_failure", dispatch=opened)
    assert [(e["type"], e["actor_token_id"], e["data"]) for e in events] == [
        ("dispatch.closed", None, _closed_event(world, opened, closed_at, _counts(delivered=2))["data"]),
        ("submission.internal_failure", None,
         {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
          "delivery_id": str(d1["delivery_uuid"]), "submission_id": str(sub1["submission_uuid"]),
          "person": s1.pseudo_id, "engine_error": "invalid_request"}),
    ]
    assert _at(events[1]["occurred_at"]) >= closed_at

    for student in world.students:
        _reply(world, student, _question(world, opened, student).message_id, "6.5")
        _settle(world)
    for student in world.students:
        delivery = _delivery(world, opened, student)
        last = _submissions(world, delivery)[-1]
        assert (delivery["state"], last["outcome"], last["valid"], last["late"]) == (
            "answered_late", "graded", True, True)
        assert delivery["revealed_at"] is not None, student.index  # revelada na correção atrasada
        assert ANSWER_SENTINEL in _question(world, opened, student).plain_text, student.index
    assert kept_count() == 1


# --- Entradas gravadas antes do horário e processadas depois -----------------------------------------------

def test_entries_recorded_before_close_are_processed_before_transition(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any, gated_updates: Any) -> None:
    """Resposta gravada antes do fechamento e processada depois: um texto por
    "responder" e um toque em "Não sei" gravados antes de `closes_at`, com o worker segurado até depois
    do horário, são processados antes da transição: `answered`/`dont_know`, nunca `no_answer` nem
    `answered_late`, com `late = false`, e nenhum evento dessas entradas sai depois do
    `dispatch.closed`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 3)
    s1, s2, s3 = world.students
    _start_bots(world, bot_worker_thread, handlers={"telegram.update": gated_updates})
    opened = _open(world, "single_choice", "single_choice")
    clock.set(opened.closes_at - timedelta(seconds=5))
    _settle(world)

    gated_updates.close()
    typed = _reply(world, s1, _question(world, opened, s1).message_id, "b")
    tap = _tap(world, s2, _question(world, opened, s2), "n")
    _wait(lambda: _update(world, typed) is not None and _update(world, tap.update_id) is not None,
          "o leitor gravou as duas entradas")
    _wait(gated_updates.entered.is_set, "o worker segurado na primeira entrada")
    received = [_update(world, u)["received_at"] for u in (typed, tap.update_id)]
    assert all(r < opened.closes_at for r in received)
    clock.set(opened.closes_at + timedelta(seconds=2))
    gated_updates.release()
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    _settle(world)

    d1, d2, d3 = (_delivery(world, opened, s) for s in world.students)
    assert [(d["state"], d["revealed_at"]) for d in (d1, d2, d3)] == [
        ("answered", closed_at), ("dont_know", closed_at), ("no_answer", None)]  # gabarito a quem respondeu
    (sub1,) = _submissions(world, d1)
    (sub2,) = _submissions(world, d2)
    assert (sub1["outcome"], sub1["valid"], sub1["late"], sub1["received_at"]) == ("graded", True, False, received[0])
    assert (sub2["outcome"], sub2["received_at"], d2["answered_at"]) == ("dont_know", received[1], received[1])
    for update_id, outcome in ((typed, "answer_recorded"), (tap.update_id, "answer_dont_know")):
        row = _update(world, update_id)
        assert (row["state"], row["outcome"]) == ("processed", outcome)
        assert row["processed_at"] > opened.closes_at  # processado depois do horário
    # A ordem das tarefas é a dos ids: os dois updates (gravados antes), depois a correção criada pelo
    # primeiro e, por último, o fechamento que a varredura cria depois de soltar o worker.
    assert _strip(_events(world, "submission.graded", "delivery.dont_know", "dispatch.closed", dispatch=opened)) == [
        {"type": "delivery.dont_know", "occurred_at": iso(sub2["created_at"]), "actor_token_id": None,
         "data": {"offering_id": world.offering_id, "dispatch_id": opened.dispatch_id,
                  "delivery_id": str(d2["delivery_uuid"]), "submission_id": str(sub2["submission_uuid"]),
                  "person": s2.pseudo_id}},
        _graded_event(world, opened, s1, d1, sub1, late=False, score=1.0, parts=[_choice_part(0, 1.0, "Azul")]),
        _closed_event(world, opened, closed_at, _counts(answered=1, dont_know=1, no_answer=1)),
    ]
    assert [a.callback_id for a in rich_bot_api.answers(world.tg_bot)] == [tap.callback_id]


def test_entries_after_time_with_dispatch_open_wait_for_transition(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any, gated_updates: Any) -> None:
    """Com o disparo ainda aberto depois do horário, um texto por
    "responder" e um "Enviar" (com a alternativa marcada antes do horário) gravados depois de
    `closes_at` esperam a transição: o primeiro processamento termina `done` sem efeito, sem gravar
    nada, e o update continua `pending`; depois do `dispatch.closed`, o texto vira `answered_late`; o
    "Enviar" que esperou traz a versão dos controles de antes da edição de fechamento e recebe "esta
    parte já foi respondida" (limitação conhecida), e marcar e enviar no
    teclado da edição de fechamento vira `answered_late`. Um update `pending` fantasma, gravado antes do
    horário, segura a barreira (a) até a correção do "Enviar" no prazo terminar: sem ele, o fechamento
    pode correr antes da correção, e uma correção pendente no fechamento conta a entrega como
    `delivered`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 3)
    s1, s2, s3 = world.students
    _start_bots(world, bot_worker_thread, handlers={"telegram.update": gated_updates})
    opened = _open(world, "single_choice", "single_choice")
    clock.set(opened.closes_at - timedelta(seconds=5))
    _settle(world)
    # s3 marca (b) no prazo, processada antes de segurar o worker; o "Enviar" vem depois do horário.
    _tap(world, s3, _question(world, opened, s3), "m", "b")
    _settle(world)
    s3_before_close = _question(world, opened, s3)
    stranger = make_user(90)
    _mark_known_chat(rich_bot_api, world.tg_bot, stranger.user_id)
    _, phantom_task = _phantom_update(world, stranger.user_id)

    gated_updates.close()
    mark1, submit1 = _mark_submit(world, s1, _question(world, opened, s1), "b", wait_mark=False)
    _wait(lambda: all(_update(world, t.update_id) is not None for t in (mark1, submit1)),
          "o leitor gravou a marcação e o 'Enviar' no prazo")
    _wait(gated_updates.entered.is_set, "o worker segurado na entrada no prazo")
    clock.set(opened.closes_at + timedelta(seconds=2))
    typed = _reply(world, s2, _question(world, opened, s2).message_id, "b")
    tapped = _tap(world, s3, s3_before_close, "s")  # o "Enviar" da versão de antes do fechamento
    _wait(lambda: all(_update(world, u) is not None for u in (typed, tapped.update_id)),
          "o leitor gravou as entradas depois do horário")
    late_rows = {u: _update(world, u) for u in (typed, tapped.update_id)}
    assert all(r["received_at"] > opened.closes_at for r in late_rows.values())
    gated_updates.release()

    _wait(lambda: [s["outcome"] for s in _submissions(world, _delivery(world, opened, s1))] == ["graded"],
          "a correção do 'Enviar' no prazo antes do fechamento")
    assert _dispatch_state(world, opened) == "open"
    _free_phantom(world, phantom_task)
    _close_wait(world, opened)
    closed_at = _closed_at(world, opened)
    closed = _events(world, "dispatch.closed", dispatch=opened)
    assert [e["data"]["counts"] for e in closed] == [_counts(answered=1, no_answer=2)]
    assert closed[0]["occurred_at"] == iso(closed_at)
    for update_id in (typed, tapped.update_id):
        rounds = _rounds(world, "telegram.update", {"update_row_id": late_rows[update_id]["id"]})
        assert rounds[:1] == [("done", None)], rounds  # a espera da transição não consome tentativa
    for _ in range(3):
        clock.advance(RECHECK)
        _settle(world)

    d1, d2, d3 = (_delivery(world, opened, s) for s in world.students)
    assert [d["state"] for d in (d1, d2, d3)] == ["answered", "answered_late", "no_answer"]
    (sub1,) = _submissions(world, d1)
    (sub2,) = _submissions(world, d2)
    assert sub1["late"] is False
    assert (sub2["outcome"], sub2["valid"], sub2["late"], sub2["received_at"]) == (
        "graded", True, True, late_rows[typed]["received_at"])
    rounds = _rounds(world, "telegram.update", {"update_row_id": late_rows[typed]["id"]})
    assert len(rounds) >= 2 and all(state == "done" for state, _e in rounds), rounds
    stale = _update(world, tapped.update_id)
    assert (stale["state"], stale["outcome"]) == ("processed", "answer_stale")
    assert _submissions(world, d3) == []
    assert [a.text for a in rich_bot_api.answers(world.tg_bot) if a.callback_id == tapped.callback_id] == [
        texts.PART_ALREADY_ANSWERED]

    _mark_submit(world, s3, _question(world, opened, s3), "b")  # no teclado da edição de fechamento
    _settle(world)
    d3 = _delivery(world, opened, s3)
    (sub3,) = _submissions(world, d3)
    assert (d3["state"], sub3["late"], sub3["valid"]) == ("answered_late", True, True)
    graded = _events(world, "submission.graded", "dispatch.closed", dispatch=opened)
    assert [(e["type"], e["data"].get("late"), e["data"].get("person")) for e in graded] == [
        ("submission.graded", False, s1.pseudo_id), ("dispatch.closed", None, None),
        ("submission.graded", True, s2.pseudo_id), ("submission.graded", True, s3.pseudo_id)]


def test_text_without_reply_after_time_is_a_free_message(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        rich_bot_api: Any, bot_worker_thread: Any, gated_updates: Any) -> None:
    """Um texto sem "responder" gravado **antes** do
    horário vai ao único campo digitável aberto (controle positivo); gravado **depois** do horário, com
    o disparo ainda aberto, vira mensagem livre: a entrega de um disparo com o horário passado não
    conta, porque a atrasada exige "responder"; no fechamento, essa entrega fica `no_answer`."""
    world = _world_rich(client, sql, clock, settings, engine_fake, repo_fetcher, rich_bot_api, 2)
    s1, s2 = world.students
    _start_bots(world, bot_worker_thread, handlers={"telegram.update": gated_updates})
    opened = _open(world, "number", "number")
    clock.set(opened.closes_at - timedelta(seconds=5))
    _settle(world)

    gated_updates.close()
    before = rich_bot_api.push_text(world.tg_bot, s1.user, "6.5")
    _wait(gated_updates.entered.is_set, "o worker segurado na entrada no prazo")
    clock.set(opened.closes_at + timedelta(seconds=2))
    after = rich_bot_api.push_text(world.tg_bot, s2.user, "6.5")
    _wait(lambda: _update(world, after) is not None, "o leitor gravou o texto depois do horário")
    gated_updates.release()
    _close_wait(world, opened)
    _settle(world)

    rows = {u: _update(world, u) for u in (before, after)}
    assert rows[before]["received_at"] < opened.closes_at < rows[after]["received_at"]
    assert [(rows[u]["state"], rows[u]["outcome"]) for u in (before, after)] == [
        ("processed", "answer_recorded"), ("processed", "free_message")]
    d1, d2 = _delivery(world, opened, s1), _delivery(world, opened, s2)
    assert (d1["state"], d2["state"]) == ("answered", "no_answer")
    assert _submissions(world, d2) == []
    free = sql.execute("SELECT count(*), bool_and(NOT ambiguous) FROM free_messages "
                       "WHERE telegram_update_id = %s", (rows[after]["id"],)).fetchone()
    assert free == (1, True)
    replies = [m for m in _sent_to(world, s2) if texts.FREE_MESSAGE_REPLY in (m.plain_text or "")]
    assert len(replies) == 1


# --- console: a revelação como linha nova -----------------------------------------------------------------

def test_console_reveal_is_a_new_line_and_late_answer_composes(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        bot_api: Any, worker_thread: Any) -> None:
    """No console (`can_edit = False`), a
    revelação sai como linha nova em resposta à da questão (`reply_to`), com `CLOSED_NOTICE` e o
    gabarito; a linha de fechamento da `no_answer` traz o `LATE_HOWTO` e o `/naosei`
    (`DONT_KNOW_HOWTO`), sem o gabarito e sem `revealed_at`; nenhuma linha escrita antes do
    fechamento traz a sentinela; a atrasada pela chave compõe o feedback com o gabarito, revelado na
    linha do feedback."""
    world = _world(client, sql, clock, settings, engine_fake, repo_fetcher, bot_api, students=2)
    s1, s2 = world.students
    stream = io.StringIO()
    worker_thread(channel=ConsoleChannel(stream), console_routes=True, engine=_engine(world),
                  fetcher=repo_fetcher, delivery=DELIVERY)
    inbox = ConsoleInbox(settings.database_url, clock=clock)
    console = {s.index: ConsoleStudent(inbox, bot_id=world.bot_row, chat_id=s.chat_id, recipient=s.pseudo_id,
                                       source=stream) for s in world.students}
    opened = _open(world, "single_choice", "single_choice")
    questions = {i: c.wait_for_text(COLOR_STATEMENT, new=False) for i, c in console.items()}
    assert all(q.answerable for q in questions.values())
    console[s1.index].choose(questions[s1.index], "b")
    _settle(world)
    assert _delivery(world, opened, s1)["state"] == "answered"

    written = len(console[s1.index].all_lines())
    closed_at = _close(world, opened)
    _settle(world)
    lines = console[s1.index].all_lines()
    assert [line.index for line in lines[:written] if ANSWER_SENTINEL in line.text] == []

    reveals = {}
    for student in world.students:
        mine = [line for line in lines[written:] if line.recipient == student.pseudo_id]
        assert len(mine) == 1, student.index
        reveals[student.index] = mine[0]
        assert texts.CLOSED_NOTICE in mine[0].text
        if student is s2:  # a linha de fechamento da no_answer, sem o gabarito
            assert ANSWER_SENTINEL not in mine[0].text, "o gabarito chegou à no_answer no fechamento"
            assert texts.DONT_KNOW_HOWTO in mine[0].text  # o /naosei na linha de fechamento
            assert _delivery(world, opened, student)["revealed_at"] is None
            continue
        assert ANSWER_SENTINEL in mine[0].text
        assert _delivery(world, opened, student)["revealed_at"] == closed_at
    # Em resposta à linha da questão (a de S1 pode citar a linha do feedback, se ela virou a principal).
    assert str(reveals[s2.index].reply_to) == questions[s2.index].channel_ref
    s1_refs = {line.channel_ref for line in lines[:written] if line.recipient == s1.pseudo_id and line.answerable}
    assert str(reveals[s1.index].reply_to) in s1_refs
    assert texts.LATE_HOWTO in reveals[s2.index].text
    assert texts.VERDICT_CORRECT in reveals[s1.index].text
    _assert_choice_echo(reveals[s1.index].text, COLORS, ["b"])  # a chave digitada, com o texto da opção

    console[s2.index].mark()
    console[s2.index].choose(questions[s2.index], "b")
    _settle(world)
    assert _delivery(world, opened, s2)["state"] == "answered_late"
    assert _delivery(world, opened, s2)["revealed_at"] is not None  # revelada na correção atrasada
    late = console[s2.index].new_lines()
    fragments = (texts.VERDICT_CORRECT, ANSWER_SENTINEL, texts.CLOSED_NOTICE)
    assert any(all(f in line.text for f in fragments) for line in late), [line.text[:80] for line in late]
    composed = [line for line in late if all(f in line.text for f in fragments)]
    _assert_choice_echo(composed[-1].text, COLORS, ["b"])
    assert all(line.reply_to is not None for line in late)


# --- a questão com número: a sentinela ausente antes do fechamento também no console ---------------------

def test_console_sentinel_absent_until_close_for_typed_question(
        client: Any, sql: Any, clock: Any, settings: Any, engine_fake: Any, repo_fetcher: Any,
        bot_api: Any, worker_thread: Any) -> None:
    """Numa questão digitável pelo console, o pedido do campo, o
    feedback e o erro de formato saem antes do fechamento sem a sentinela; o teste lê o fluxo antes de
    levar o relógio além de `closes_at`; depois, a revelação a traz (controle positivo)."""
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
    student.reply(question, "abc")
    _settle(world)
    student.reply(question, "6,5")
    _settle(world)
    assert _delivery(world, opened, s1)["state"] == "answered"
    before = stream.getvalue()
    assert ANSWER_SENTINEL not in before
    assert texts.VERDICT_CORRECT in before
    _close(world, opened)
    _settle(world)
    assert ANSWER_SENTINEL in stream.getvalue()[len(before):]
