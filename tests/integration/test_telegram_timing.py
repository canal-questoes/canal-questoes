"""Os tempos de resposta do disparo em tempo real.


Os tempos de resposta, com os dublês e **30 alunos**, medidos pelo `monotonic()` que a Bot API falsa estendida
(`rich_bot_api`) registra em cada pedido (`started`; `tests.helpers.bot_api.monotonic`, o
`perf_counter()`), a partir do gatilho, com o **worker já no ar** e os limites exatos, sem margem e sem
repetição automática:

- confirmação de um imediato → 30ª questão enviada, **15 s**: da resposta do `dispatches.confirm`
  recebida pelo teste ao maior `started` das 30 mensagens de questão;
- toque → confirmação do toque, **2 s**: do toque gravado pela Bot API falsa (`Tap.emitted`) ao
  `answerCallbackQuery` aceito (`CallbackAnswer.started`), na marcação da alternativa e no "Enviar" da
  escolha única;
- resposta → edição com o feedback, **5 s**: do "Enviar" gravado (`Tap.emitted`) ou do texto gravado (o
  `monotonic()` logo depois do `push_text`) à primeira edição da questão com o feedback (`Edit.started`);
- horário de fechamento → última edição do gabarito de 30 entregas, **60 s**: do avanço do `FakeClock`
  além de `closes_at` ao maior `started` das 30 edições da revelação.

Política: uma falha é rodada de novo uma vez, isolada, e registrada;
duas seguidas são falha real, e mudar os limites exige rever os testes congelados. Nada aqui
repete sozinho.

**Montagem** (como `test_dispatch_opening.py`): o oferecimento, o bot (na `rich_bot_api`) e a ativação
pela API de oferecimentos e bots; as pessoas pela API base; a matrícula ativa com o termo aceito e o endereço de entrega
por SQL, e o chat de cada aluno conhecido do dublê por um `my_chat_member`
(`kicked` → `member`, `not_handled` na recepção). O worker com bots (`run_worker(…, bots=…)`, o caminho de
produção) sobe **antes** de qualquer medida, com o motor falso estrito pelo adaptador HTTP real
(`HttpQuestionEngine`), a obtenção falsa e o `DeliverySettings()` padrão (Rich, 12 botões, 1 mensagem por
segundo por chat e 30 por segundo por bot: o ritmo real). Dois disparos imediatos com os
presets do motor falso: a escolha única (toques: marcar e "Enviar") e o número (texto em resposta ao
pedido do campo), com fechamentos distintos, para que cada fechamento revele exatamente 30 entregas.

O `FakeClock` só anda com o sistema parado e em saltos de até 10 s (a posse do bot conferida depois de
cada salto; a margem de renovação do worker é de 13 s); o fechamento segue um protocolo determinístico:
sem fechamento em 2,5 s reais, o relógio avança `CLOSE_RECHECK_SECONDS` (5 s) + 1 para a rodada
seguinte, e o tempo medido inclui essas esperas.

Leituras [I] deste arquivo (escolhas deste teste):
- a 30ª questão enviada é o pedido de envio da mensagem principal de cada entrega (a de papel
  `question`, lida por `deliveries.principal_message_id` → `outbox_messages.channel_ref`); o pedido do
  campo não conta;
- o texto gravado pela Bot API falsa é o `push_text` que voltou (o update já está na fila do dublê); o
  dublê não registra instante para ele;
- a edição com o feedback é a primeira edição da mensagem da questão cujo texto traz o cabeçalho do
  feedback ("Sua resposta:"); a do gabarito, a que traz "Janela fechada.";
- o gatilho do fechamento é o salto que leva o relógio além de `closes_at` (o salto anterior para 1 s
  antes, com o sistema parado);
- na escolha única, os 30 alunos marcam `(b)` "ao mesmo tempo", o teste espera as 30
  confirmações e as 30 edições da marcação (a 1ª linha do teclado com "✅ (b)") e o sistema parado, e só
  então os 30 tocam "Enviar" "ao mesmo tempo": as edições da marcação ficam fora da medida do feedback (o
  ritmo de 30 por segundo por bot); a edição da marcação não tem limite próprio nos tempos medidos.

**A confirmação do toque fora do limitador por chat** (ela não pode passar pelo limitador): com um aluno e
o `DeliverySettings(chat_interval_seconds=5)`, a questão de escolha única recém-enviada satura o limitador
do chat; o toque logo em seguida (a marcação de `(b)`) tem o `answerCallbackQuery` aceito bem antes do
fim do intervalo por chat (medido pelo `started` da Bot API falsa, a partir do `started` do envio da
questão), enquanto as chamadas de envio ou edição seguintes ao mesmo chat (a edição da marcação, se não for
coalescida com a do feedback, e a edição com o feedback; controle positivo) respeitam o intervalo.
O "Enviar" sai logo depois da confirmação da marcação e também é confirmado dentro do limite.
Com o ritmo padrão (1 s), uma confirmação que passasse pelo limitador ainda caberia nos 2 s do limite; com
5 s, ela chegaria perto de 5 s depois da questão. Leituras [I] (escolhas deste teste): "bem antes" é ao menos
`CONFIRM_EARLY` (1,5 s) antes do fim do intervalo; o toque sai até `TAP_AFTER_QUESTION` (1,5 s) depois do
envio da questão (pré-condição da saturação); o controle aceita `PACE_TOLERANCE` (0,2 s) de encolhimento,
como a `test_telegram_e2e.py`.

No esqueleto, a montagem passa, e as duas funções
falham no primeiro `dispatches.prepare` (rota ausente, 404). Alguns casos dependem
de partes implementadas depois e só passam quando elas existirem.

Escolha única com confirmação: o teclado da única tem o
"Enviar", a alternativa só marca (ação `m`) e a resposta sai no "Enviar" (ação `s`).
Nos tempos medidos, o teclado é conferido contra `CHOICE_KEYBOARD` (com a linha
"Enviar"); no teste do limitador, o `_choice_data` procura o botão da ação `m`
(marcar).
"""

from __future__ import annotations

import contextlib
import dataclasses
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.buttons import CALLBACK_DATA
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso
from tests.helpers.engine import ANSWER_SENTINEL

# --- limites dos tempos (exatos, sem margem) ---------------------------------------------------

CONFIRM_TO_LAST_QUESTION = 15.0
TAP_TO_CONFIRMATION = 2.0
ANSWER_TO_FEEDBACK = 5.0
CLOSE_TO_LAST_REVEAL = 60.0

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

CODE = "telegram-tempos"
ZONE = "America/Sao_Paulo"
BOT_USERNAME = "cq_tempos_bot"
WORKER_ID = "worker-telegram-tempos"
REPO = "https://repos.cq.test/disciplina/questoes-tempos.git"
COMMIT = "9c3d" * 10
N = 30
USER_BASE = 900
ENGINE_TIMEOUT = 10.0
WAIT = 60.0  # segundos reais de espera por uma condição fora das medidas
MEASURE_WAIT = 120.0  # espera real pelo fim de uma medida (o limite é conferido depois, pelo valor medido)
CLOCK_STEP = 10  # segundos por salto do relógio com o worker no ar
LEASE_MARGIN = 13  # consulta 1 s + Telegram 2 s + renovação 10 s (bot_settings dos testes)
RECHECK = 6  # avanço que vence `CLOSE_RECHECK_SECONDS` (5 s) entre as rodadas do fechamento
# Os fechamentos (a janela mínima é de 5 min); o do número bem depois do da única, para
# que as rodadas do primeiro fechamento não alcancem o segundo.
CHOICE_WINDOW = timedelta(minutes=6)
NUMBER_WINDOW = timedelta(minutes=10)
QIDS = ("single_choice", "number")

# Textos por extenso (os do disparo e da resposta).
FEEDBACK_HEAD = "Sua resposta:"
CLOSED_NOTICE = "Janela fechada."
CORRECT = "✅ correta"
NUMBER_HINT = "Número; ex.: 4.25, -17 ou 3/7"
CHOICE_KEYBOARD = [["(a)", "(b)", "(c)", "(d)"], ["Enviar"], ["Não sei"]]
# A 1ª linha do teclado da única com `(b)` marcada (rótulos de referência).
MARKED_CHOICE_ROW = ["(a)", "✅ (b)", "(c)", "(d)"]
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}

# A confirmação do toque fora do limitador por chat.
PACED_CHAT_INTERVAL = 5.0  # o intervalo por chat do teste (`DeliverySettings(chat_interval_seconds=5)`)
TAP_AFTER_QUESTION = 1.5  # pré-condição: o toque sai até 1,5 s depois do envio da questão (limitador cheio)
CONFIRM_EARLY = 1.5  # "bem antes": a confirmação chega ao menos 1,5 s antes do fim do intervalo por chat
PACE_TOLERANCE = 0.2  # encolhimento aceito no controle (como `test_telegram_e2e.py`)
SEND_METHODS = frozenset({"sendMessage", "sendRichMessage", "editMessageText", "editMessageReplyMarkup",
                          "sendPhoto", "sendDocument"})


# --- montagem ----------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _S:
    i: int
    user: Any
    pseudo: str
    person_row: int

    @property
    def chat(self) -> int:
        return self.user.user_id


class _NoRouteChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota, então nada pode chegar aqui."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


@dataclasses.dataclass
class _T:
    client: Any
    sql: psycopg.Connection
    clock: Any
    rich: Any
    offering: dict
    bot: int
    bot_row: int
    offering_row: int
    students: list[_S]
    dispatches: dict[str, dict] = dataclasses.field(default_factory=dict)
    closes: dict[str, datetime] = dataclasses.field(default_factory=dict)


def _one(sql: psycopg.Connection, query: str, params: tuple = ()) -> dict | None:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _lease_left(t: _T) -> float:
    until = t.sql.execute("SELECT lease_until FROM bots WHERE id = %s", (t.bot_row,)).fetchone()[0]
    return -1.0 if until is None else (until - t.clock.now()).total_seconds()


def _lease_ok(t: _T) -> None:
    _wait(lambda: _lease_left(t) >= LEASE_MARGIN, "a renovação da posse do bot")


def _advance(t: _T, seconds: float) -> datetime:
    """Avança o relógio em saltos de até 10 s, esperando a posse do bot ter folga depois de cada salto."""
    left = seconds
    while left > 0:
        step = min(CLOCK_STEP, left)
        left -= step
        t.clock.advance(step)
        _lease_ok(t)
    return t.clock.now()


def _busy(t: _T) -> list[str]:
    now = t.clock.now()
    busy = [f"tarefa {k}" for (k,) in t.sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"mensagem {m}" for (m,) in t.sql.execute(
        "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"update {u}" for (u,) in t.sql.execute(
        "SELECT id FROM telegram_updates WHERE state = 'pending'").fetchall()]
    return busy


def _settle(t: _T) -> None:
    """Espera o sistema parar (nenhuma tarefa, mensagem ou update disponível), duas vezes, com mais de uma
    rodada da varredura (1 s real) entre elas; só então o relógio pode saltar."""
    for _ in range(10):
        _wait(lambda: not _busy(t), "o sistema parar")
        time.sleep(1.2)
        if not _busy(t):
            return
    raise AssertionError(f"o sistema não parou: {_busy(t)}")


def _student(t_client: Any, sql: psycopg.Connection, rich: Any, clock: Any, *, i: int, bot: int, bot_row: int,
             offering_row: int) -> _S:
    """Uma pessoa com matrícula `student` ativa (termo aceito) e endereço de entrega ativo no bot."""
    user = make_user(USER_BASE + i)
    person = register_person(t_client, ADMIN_TOKEN, name=f"Pessoa Tempos {i:02d}")
    person_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (person["pseudo_id"],)).fetchone()[0]
    now = clock.now()
    sql.execute(
        "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, terms_version, "
        "terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
        (person_row, offering_row, now, now, now))
    sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_row, bot_row, user.user_id, now, now))
    # O chat passa a ser conhecido do dublê (senão, 400 `chat not found`).
    rich.push_my_chat_member(bot, user, old_status="kicked", new_status="member")
    return _S(i=i, user=user, pseudo=person["pseudo_id"], person_row=person_row)


# --- consultas ----------------------------------------------------------------------------------------


def _delivery(t: _T, qid: str, s: _S) -> dict:
    row = _one(t.sql, """
        SELECT d.id, d.state, d.current_field, d.button_token, d.answered_at, d.revealed_at,
               d.principal_message_id
          FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id
          JOIN enrollments e ON e.id = d.enrollment_id
         WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s""", (t.dispatches[qid]["dispatch_id"], s.person_row))
    assert row is not None, f"sem entrega do aluno {s.i} em {qid}"
    return row


def _question_id(t: _T, qid: str, s: _S) -> int:
    delivery = _delivery(t, qid, s)
    assert delivery["principal_message_id"] is not None, f"entrega de {s.i} em {qid} sem principal"
    ref = t.sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s",
                        (delivery["principal_message_id"],)).fetchone()[0]
    return int(ref)


def _delivered(t: _T, qid: str) -> bool:
    """As 30 entregas `delivered`, com a mensagem principal enviada e nada da entrega na fila."""
    rows = t.sql.execute(
        "SELECT d.state, m.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "LEFT JOIN outbox_messages m ON m.id = d.principal_message_id WHERE x.dispatch_uuid = %s::uuid",
        (t.dispatches[qid]["dispatch_id"],)).fetchall()
    return len(rows) == N and set(rows) == {("delivered", "sent")}


def _prompt_id(t: _T, s: _S) -> int:
    """O pedido do campo do número enviado: `ForceReply` com a dica, em resposta à questão."""
    delivery = _delivery(t, "number", s)
    rows = _wait(lambda: t.sql.execute(
        "SELECT channel_ref FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' "
        "AND field_index = 0 AND state = 'sent'", (delivery["id"],)).fetchall(),
        f"o pedido do campo do aluno {s.i}")
    assert len(rows) == 1
    message = t.rich.current(t.bot, s.chat, int(rows[0][0]))
    assert (message.force_reply, message.placeholder, message.reply_to_message_id) == (
        True, NUMBER_HINT, _question_id(t, "number", s))
    return message.message_id


def _feedback_edit(t: _T, qid: str, s: _S, since: float) -> Any:
    """A primeira edição da questão com o feedback depois de `since` (`None` se ainda não houve)."""
    edits = [e for e in t.rich.edits(t.bot, s.chat, _question_id(t, qid, s))
             if e.started >= since and FEEDBACK_HEAD in (e.after.plain_text or "")]
    return edits[0] if edits else None


def _reveal_edit(t: _T, qid: str, s: _S, since: float) -> Any:
    edits = [e for e in t.rich.edits(t.bot, s.chat, _question_id(t, qid, s))
             if e.started >= since and CLOSED_NOTICE in (e.after.plain_text or "")]
    assert len(edits) <= 1, f"mais de uma revelação do aluno {s.i} em {qid}"
    return edits[0] if edits else None


def _dispatch_state(t: _T, qid: str) -> str:
    return t.sql.execute("SELECT state FROM dispatches WHERE dispatch_uuid = %s::uuid",
                         (t.dispatches[qid]["dispatch_id"],)).fetchone()[0]


def _counts(**values: int) -> dict[str, int]:
    return {**ZERO_COUNTS, **values}


# --- as medidas ----------------------------------------------------------------------------------------


def _measure_confirmation(t: _T, qid: str) -> float:
    """Confirmação do imediato → 30ª questão enviada (o maior `started` das 30 mensagens de questão)."""
    body = confirm_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches[qid]["dispatch_id"])
    trigger = monotonic()
    assert (body["state"], body["confirmed"], body["target_count"]) == ("scheduled", True, N)
    _wait(lambda: _delivered(t, qid), f"as trinta questões de {qid}", timeout=MEASURE_WAIT)
    questions = [t.rich.current(t.bot, s.chat, _question_id(t, qid, s)) for s in t.students]
    assert sorted(m.chat_id for m in questions) == sorted(s.chat for s in t.students)
    assert {(m.method, m.text_format) for m in questions} == {("sendRichMessage", "rich_html")}
    assert all(ANSWER_SENTINEL not in (m.plain_text or "") for m in questions)
    if qid == "single_choice":
        assert all([[b.label for b in row] for row in m.keyboard or ()] == CHOICE_KEYBOARD for m in questions)
    return max(m.started for m in questions) - trigger


def _button_data(t: _T, s: _S, action: str, key: str | None = None) -> tuple[int, str]:
    """O dado do botão da ação (e da chave) na questão de escolha única do aluno (gramática `cq1:`)."""
    message = t.rich.current(t.bot, s.chat, _question_id(t, "single_choice", s))
    found = []
    for button in message.buttons():
        match = CALLBACK_DATA.fullmatch(button.data or "")
        assert match is not None, f"botão fora da gramática cq1: {button.label!r}"
        if (match.group(2), match.group(5)) == (action, key):
            found.append(button.data)
    assert len(found) == 1, f"botões da ação {action}/{key} do aluno {s.i}: {len(found)}"
    return message.message_id, found[0]


def _choice_data(t: _T, s: _S, key: str) -> tuple[int, str]:
    """O dado do botão que marca a alternativa `key` (ação `m`)."""
    return _button_data(t, s, "m", key)


def _submit_data(t: _T, s: _S) -> tuple[int, str]:
    """O dado do botão "Enviar" da escolha única (ação `s`, sem chave)."""
    return _button_data(t, s, "s")


def _mark_edit(t: _T, s: _S, since: float) -> Any:
    """A primeira edição da questão de escolha única depois de `since` com `(b)` marcada (`None` se ainda
    não houve)."""
    edits = [e for e in t.rich.edits(t.bot, s.chat, _question_id(t, "single_choice", s))
             if e.started >= since and [b.label for b in (e.after.keyboard or ((),))[0]] == MARKED_CHOICE_ROW]
    return edits[0] if edits else None


def _confirmations(t: _T, taps: dict[int, Any], what: str) -> dict[int, Any]:
    """Os `answerCallbackQuery` aceitos dos toques (um por aluno), esperados até `MEASURE_WAIT`."""
    def answered() -> dict[int, Any] | None:
        by_callback = {a.callback_id: a for a in t.rich.answers(t.bot)}
        found = {i: by_callback.get(tap.callback_id) for i, tap in taps.items()}
        return found if all(found.values()) else None

    answers = _wait(answered, what, timeout=MEASURE_WAIT)
    assert sorted(a.callback_id for a in answers.values()) == sorted(tap.callback_id for tap in taps.values())
    return answers


def _update_outcomes(t: _T, taps: dict[int, Any]) -> set[str]:
    """Os desfechos gravados (`telegram_updates.outcome`) dos updates dos toques, depois de processados."""
    ids = [tap.update_id for tap in taps.values()]

    def processed() -> list[tuple] | None:
        rows = t.sql.execute(
            "SELECT outcome FROM telegram_updates WHERE bot_id = %s AND update_id = ANY(%s) "
            "AND state = 'processed'", (t.bot_row, ids)).fetchall()
        return rows if len(rows) == len(ids) else None

    return {outcome for (outcome,) in _wait(processed, "os updates dos toques processados")}


def _measure_taps(t: _T) -> tuple[list[float], list[float], list[float]]:
    """A escolha única pelos 30 alunos: os 30 marcam `(b)` "ao mesmo tempo" (marcação →
    confirmação); o teste espera as 30 edições da marcação, sem resposta nem feedback, e o sistema parado;
    então os 30 tocam "Enviar" "ao mesmo tempo" ("Enviar" → confirmação e "Enviar" → edição com o
    feedback)."""
    targets = {s.i: _choice_data(t, s, "b") for s in t.students}
    marks = {s.i: t.rich.tap(t.bot, s.user, *targets[s.i]) for s in t.students}
    mark_answers = _confirmations(t, marks, "as trinta confirmações da marcação")

    def mark_edited() -> dict[int, Any] | None:
        found = {s.i: _mark_edit(t, s, marks[s.i].emitted) for s in t.students}
        return found if all(found.values()) else None

    mark_edits = _wait(mark_edited, "as trinta edições da marcação", timeout=MEASURE_WAIT)
    for s in t.students:
        shown = mark_edits[s.i].after
        assert [[b.label for b in row] for row in shown.keyboard or ()][1:] == CHOICE_KEYBOARD[1:], (s.i, shown)
        assert FEEDBACK_HEAD not in (shown.plain_text or ""), "a marcação não traz feedback"
    assert _update_outcomes(t, marks) == {"answer_toggled"}
    _settle(t)
    assert all(_delivery(t, "single_choice", s)["state"] == "delivered" for s in t.students), (
        "a marcação não responde")
    assert not [e for s in t.students for e in t.rich.edits(t.bot, s.chat, _question_id(t, "single_choice", s))
                if FEEDBACK_HEAD in (e.after.plain_text or "")], 'nenhum feedback antes do "Enviar"'

    submit_targets = {s.i: _submit_data(t, s) for s in t.students}
    submits = {s.i: t.rich.tap(t.bot, s.user, *submit_targets[s.i]) for s in t.students}
    submit_answers = _confirmations(t, submits, 'as trinta confirmações do "Enviar"')

    def edited() -> dict[int, Any] | None:
        found = {s.i: _feedback_edit(t, "single_choice", s, submits[s.i].emitted) for s in t.students}
        return found if all(found.values()) else None

    edits = _wait(edited, "as trinta edições com o feedback da escolha", timeout=MEASURE_WAIT)
    for s in t.students:
        shown = edits[s.i].after.plain_text or ""
        assert CORRECT in shown and ANSWER_SENTINEL not in shown, (s.i, shown)
        assert edits[s.i].after.buttons() == [], "a entrega respondida fica sem botões"
    assert _update_outcomes(t, submits) == {"answer_recorded"}
    return ([mark_answers[i].started - marks[i].emitted for i in marks],
            [submit_answers[i].started - submits[i].emitted for i in submits],
            [edits[i].started - submits[i].emitted for i in submits])


def _measure_replies(t: _T) -> list[float]:
    """As 30 respostas "6,5" em resposta ao pedido do campo do número: texto gravado → feedback."""
    prompts = {s.i: _prompt_id(t, s) for s in t.students}
    pushed: dict[int, float] = {}
    for s in t.students:
        uid = t.rich.push_text(t.bot, s.user, "6,5", reply_to_message_id=prompts[s.i])
        pushed[s.i] = monotonic()
        assert uid is not None

    def edited() -> dict[int, Any] | None:
        found = {s.i: _feedback_edit(t, "number", s, pushed[s.i]) for s in t.students}
        return found if all(found.values()) else None

    edits = _wait(edited, "as trinta edições com o feedback do número", timeout=MEASURE_WAIT)
    for s in t.students:
        shown = edits[s.i].after.plain_text or ""
        assert CORRECT in shown and "6,5" in shown and ANSWER_SENTINEL not in shown, (s.i, shown)
    return [edits[i].started - pushed[i] for i in pushed]


def _measure_close(t: _T, qid: str) -> float:
    """Horário de fechamento → última edição do gabarito das 30 entregas do disparo `qid`."""
    closes = t.closes[qid]
    _advance(t, (closes - t.clock.now()).total_seconds() - 1)
    _settle(t)
    since = monotonic()
    t.clock.advance(2)  # o salto que leva o relógio além de `closes_at`
    trigger = monotonic()
    _lease_ok(t)
    deadline = time.monotonic() + MEASURE_WAIT
    while _dispatch_state(t, qid) != "closed":
        window = time.monotonic() + 2.5
        while time.monotonic() < window and _dispatch_state(t, qid) != "closed":
            time.sleep(0.05)
        if _dispatch_state(t, qid) == "closed":
            break
        assert time.monotonic() < deadline, f"{qid} não fechou em {MEASURE_WAIT} s"
        _advance(t, RECHECK)

    def revealed() -> dict[int, Any] | None:
        found = {s.i: _reveal_edit(t, qid, s, since) for s in t.students}
        return found if all(found.values()) else None

    edits = _wait(revealed, f"as trinta revelações de {qid}", timeout=MEASURE_WAIT)
    closed_at = t.sql.execute("SELECT closed_at FROM dispatches WHERE dispatch_uuid = %s::uuid",
                              (t.dispatches[qid]["dispatch_id"],)).fetchone()[0]
    for s in t.students:
        delivery = _delivery(t, qid, s)
        assert (delivery["state"], delivery["revealed_at"]) == ("answered", closed_at), (qid, s.i)
        assert ANSWER_SENTINEL in (edits[s.i].after.plain_text or ""), "o gabarito sai na revelação"
    return max(e.started for e in edits.values()) - trigger


# --- o teste -------------------------------------------------------------------------------------------


def test_times_with_thirty_students_measured_on_the_fake_bot_api(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, rich_bot_api: Any, engine_fake: Any,
        repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """Com o worker já no ar e 30 alunos, confirmação do imediato → 30ª questão
    ≤ 15 s (nos dois disparos); toque → confirmação ≤ 2 s (as 30 marcações e os 30 "Enviar" da escolha
    única) e "Enviar" → edição com o feedback ≤ 5 s (as marcações antes, fora da medida);
    texto gravado → edição com o feedback ≤ 5 s (as 30 respostas ao pedido do
    campo do número); avanço do `FakeClock` além de `closes_at` → última edição do gabarito das 30
    entregas ≤ 60 s (nos dois fechamentos). Cada tempo pelo `monotonic()` da `rich_bot_api`, com os limites
    exatos, sem margem e sem repetição automática."""
    rich = rich_bot_api
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, timezone=ZONE)
    bot = rich.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, CODE, rich.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, CODE)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    students = [_student(client, sql, rich, clock, i=i, bot=bot, bot_row=bot_row, offering_row=offering_row)
                for i in range(N)]
    t = _T(client=client, sql=sql, clock=clock, rich=rich, offering=offering, bot=bot, bot_row=bot_row,
           offering_row=offering_row, students=students)
    engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    default_channel = _NoRouteChannel()
    try:
        # O worker no ar antes de qualquer medida; os `my_chat_member` da montagem processados.
        bot_worker_thread(channel=default_channel, engine=engine, fetcher=repo_fetcher, delivery=DeliverySettings(),
                          settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(lambda: rich.pending_polls(bot), "o leitor do bot consultando")
        _wait(lambda: sql.execute("SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND state = 'processed'",
                                  (bot_row,)).fetchone()[0] == N, "os trinta my_chat_member")
        _lease_ok(t)
        _settle(t)
        _run(t)
        assert default_channel.calls == []
    finally:
        bot_worker_thread.stop()
        with contextlib.suppress(NotImplementedError):
            engine.close()


def _run(t: _T) -> None:
    # Preparo e validação dos dois imediatos (fora das medidas).
    t_prep = _advance(t, 10)
    t.closes = {"single_choice": t_prep + CHOICE_WINDOW, "number": t_prep + NUMBER_WINDOW}
    for qid in QIDS:
        body = prepare_dispatch(t.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(t.closes[qid]))
        assert (body["state"], body["opens_at"], body["closes_at"]) == ("validating", None, iso(t.closes[qid]))
        t.dispatches[qid] = body
    _wait(lambda: all(_dispatch_state(t, qid) == "scheduled" for qid in QIDS), "as duas validações")
    _settle(t)
    t_conf = _advance(t, 10)

    # Tempo (1): confirmação de um imediato para 30 alunos → 30ª questão enviada, ≤ 15 s (cada disparo).
    confirm_times = {}
    for qid in QIDS:
        confirm_times[qid] = _measure_confirmation(t, qid)
        _settle(t)
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches[qid]["dispatch_id"])
        assert (body["state"], body["opened_at"], body["deliveries"]) == ("open", iso(t_conf), _counts(delivered=N))
    for qid, elapsed in confirm_times.items():
        assert elapsed <= CONFIRM_TO_LAST_QUESTION, (
            f"tempo de resposta: confirmação de {qid} → 30ª questão em {elapsed:.3f} s (limite {CONFIRM_TO_LAST_QUESTION} s)")

    # Tempos (2) e (3): toque (marcação ou "Enviar") → confirmação ≤ 2 s; resposta ("Enviar" ou texto) →
    # edição com o feedback ≤ 5 s.
    _advance(t, 10)
    mark_answers, submit_answers, tap_feedbacks = _measure_taps(t)
    _settle(t)
    reply_feedbacks = _measure_replies(t)
    _settle(t)
    for qid in QIDS:
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches[qid]["dispatch_id"])
        assert body["deliveries"] == _counts(answered=N), (qid, body["deliveries"])
    assert len(mark_answers) == len(submit_answers) == len(tap_feedbacks) == len(reply_feedbacks) == N
    assert max(mark_answers) <= TAP_TO_CONFIRMATION, (
        f"tempo de resposta: marcação → confirmação em até {max(mark_answers):.3f} s (limite {TAP_TO_CONFIRMATION} s)")
    assert max(submit_answers) <= TAP_TO_CONFIRMATION, (
        f'tempo de resposta: "Enviar" → confirmação em até {max(submit_answers):.3f} s (limite {TAP_TO_CONFIRMATION} s)')
    assert max(tap_feedbacks) <= ANSWER_TO_FEEDBACK, (
        f'tempo de resposta: "Enviar" → feedback em até {max(tap_feedbacks):.3f} s (limite {ANSWER_TO_FEEDBACK} s)')
    assert max(reply_feedbacks) <= ANSWER_TO_FEEDBACK, (
        f"tempo de resposta: texto → feedback em até {max(reply_feedbacks):.3f} s (limite {ANSWER_TO_FEEDBACK} s)")

    # Tempo (4): horário de fechamento → última edição do gabarito das 30 entregas, ≤ 60 s (cada disparo).
    close_times = {}
    for qid in QIDS:
        close_times[qid] = _measure_close(t, qid)
        _settle(t)
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches[qid]["dispatch_id"])
        assert (body["state"], body["deliveries"]) == ("closed", _counts(answered=N)), qid
    for qid, elapsed in close_times.items():
        assert elapsed <= CLOSE_TO_LAST_REVEAL, (
            f"tempo de resposta: fechamento de {qid} → última revelação em {elapsed:.3f} s (limite {CLOSE_TO_LAST_REVEAL} s)")
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []


# --- a confirmação do toque fora do limitador por chat ----------------------------------


def _chat_sends(t: _T, s: _S) -> list[Any]:
    """As chamadas de envio e de edição ao chat do aluno, pela ordem de chegada à Bot API falsa."""
    return sorted((c for c in t.rich.calls(bot_id=t.bot)
                   if c.method in SEND_METHODS and int(c.params.get("chat_id", 0)) == s.chat),
                  key=lambda c: (c.started, c.seq))


def test_tap_confirmation_bypasses_the_saturated_chat_pacer(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, rich_bot_api: Any, engine_fake: Any,
        repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """Confirmação fora do limitador (ela não pode passar pelo limitador de ritmo por chat): com o intervalo por chat de 5 s e a
    questão recém-enviada ao chat do aluno (o limitador cheio), o `answerCallbackQuery` do toque seguinte
    (a marcação de `(b)`) é aceito bem antes do fim do intervalo (≥ 1,5 s antes, pelo `started` da Bot
    API falsa) e dentro do limite de tempo; o "Enviar", logo depois, também é confirmado dentro do limite;
    controle positivo: as chamadas seguintes ao mesmo chat (a edição da marcação, se não for coalescida com
    a do feedback, e a edição com o feedback) respeitam os 5 s."""
    rich = rich_bot_api
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, timezone=ZONE)
    bot = rich.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, CODE, rich.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, CODE)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    s = _student(client, sql, rich, clock, i=0, bot=bot, bot_row=bot_row, offering_row=offering_row)
    t = _T(client=client, sql=sql, clock=clock, rich=rich, offering=offering, bot=bot, bot_row=bot_row,
           offering_row=offering_row, students=[s])
    engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    default_channel = _NoRouteChannel()
    try:
        bot_worker_thread(channel=default_channel, engine=engine, fetcher=repo_fetcher,
                          delivery=DeliverySettings(chat_interval_seconds=PACED_CHAT_INTERVAL),
                          settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(lambda: rich.pending_polls(bot), "o leitor do bot consultando")
        _wait(lambda: sql.execute("SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND state = 'processed'",
                                  (bot_row,)).fetchone()[0] == 1, "o my_chat_member")
        _lease_ok(t)
        _settle(t)
        _run_paced(t, s)
        assert default_channel.calls == []
    finally:
        bot_worker_thread.stop()
        with contextlib.suppress(NotImplementedError):
            engine.close()


def _run_paced(t: _T, s: _S) -> None:
    # Preparo, validação e confirmação do imediato de escolha única (fora da medida).
    t_prep = _advance(t, 10)
    closes = t_prep + CHOICE_WINDOW
    body = prepare_dispatch(t.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid="single_choice",
                            closes_at=iso(closes))
    assert (body["state"], body["opens_at"], body["closes_at"]) == ("validating", None, iso(closes))
    t.dispatches["single_choice"] = body
    _wait(lambda: _dispatch_state(t, "single_choice") == "scheduled", "a validação")
    _settle(t)
    _advance(t, 10)
    assert _chat_sends(t, s) == [], "nada foi enviado ao chat antes da questão"
    body = confirm_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches["single_choice"]["dispatch_id"])
    assert (body["state"], body["confirmed"], body["target_count"]) == ("scheduled", True, 1)

    # A questão recém-enviada satura o limitador do chat; o toque sai logo em seguida.
    _wait(lambda: t.sql.execute(
        "SELECT d.state, m.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "LEFT JOIN outbox_messages m ON m.id = d.principal_message_id WHERE x.dispatch_uuid = %s::uuid",
        (t.dispatches["single_choice"]["dispatch_id"],)).fetchall() == [("delivered", "sent")],
        "a questão enviada", timeout=MEASURE_WAIT)
    message_id, data = _choice_data(t, s, "b")
    tap = t.rich.tap(t.bot, s.user, message_id, data)
    question = t.rich.current(t.bot, s.chat, message_id)
    assert (question.method, question.text_format) == ("sendRichMessage", "rich_html")
    assert [c.seq for c in _chat_sends(t, s)][:1] == [question.call_seq], "a questão é o 1º envio ao chat"
    assert tap.emitted - question.started <= TAP_AFTER_QUESTION, (
        f"pré-condição: o toque saiu {tap.emitted - question.started:.3f} s depois da questão "
        f"(limite {TAP_AFTER_QUESTION} s), com o limitador do chat ainda cheio")

    answer = _wait(lambda: next((a for a in t.rich.answers(t.bot) if a.callback_id == tap.callback_id), None),
                   "a confirmação do toque", timeout=MEASURE_WAIT)
    # O "Enviar" logo depois da confirmação da marcação: a resposta sai nele.
    submit_message_id, submit_data = _submit_data(t, s)
    assert submit_message_id == message_id
    submit = t.rich.tap(t.bot, s.user, message_id, submit_data)
    submit_answer = _wait(
        lambda: next((a for a in t.rich.answers(t.bot) if a.callback_id == submit.callback_id), None),
        'a confirmação do "Enviar"', timeout=MEASURE_WAIT)
    feedback = _wait(lambda: _feedback_edit(t, "single_choice", s, submit.emitted), "a edição com o feedback",
                     timeout=MEASURE_WAIT)
    shown = feedback.after.plain_text or ""
    assert CORRECT in shown and ANSWER_SENTINEL not in shown, shown
    _settle(t)

    # a confirmação escapa do limitador por chat (bem antes do fim do intervalo) e cabe no limite de tempo.
    free_at = question.started + PACED_CHAT_INTERVAL
    assert answer.started <= free_at - CONFIRM_EARLY, (
        f"confirmação {answer.started - question.started:.3f} s depois da questão, com o intervalo por "
        f"chat de {PACED_CHAT_INTERVAL} s (deveria sair ao menos {CONFIRM_EARLY} s antes do fim): passou pelo "
        "limitador por chat")
    assert answer.delay <= TAP_TO_CONFIRMATION, (
        f"tempo de resposta: toque → confirmação em {answer.delay:.3f} s (limite {TAP_TO_CONFIRMATION} s)")
    assert submit_answer.delay <= TAP_TO_CONFIRMATION, (
        f'tempo de resposta: "Enviar" → confirmação em {submit_answer.delay:.3f} s (limite {TAP_TO_CONFIRMATION} s)')
    assert [a.callback_id for a in t.rich.answers(t.bot)] == [tap.callback_id, submit.callback_id], (
        'uma confirmação da marcação e uma do "Enviar"')
    with t.sql.cursor() as cur:
        outcomes = dict(cur.execute(
            "SELECT update_id, outcome FROM telegram_updates WHERE bot_id = %s AND update_id = ANY(%s)",
            (t.bot_row, [tap.update_id, submit.update_id])).fetchall())
    assert outcomes == {tap.update_id: "answer_toggled", submit.update_id: "answer_recorded"}

    # Controle positivo: a chamada seguinte ao mesmo chat (envio ou edição) respeita o intervalo por chat,
    # e a edição com o feedback é uma das que vieram depois da questão.
    sends = _chat_sends(t, s)
    assert sends[0].seq == question.call_seq and len(sends) >= 2, [c.method for c in sends]
    assert feedback.call_seq in [c.seq for c in sends[1:]]
    # Ao chat vão a questão, a edição da marcação ou nenhuma (coalescida com a do feedback) e a
    # edição com o feedback, nesta ordem.
    assert len(sends) in (2, 3) and sends[-1].seq == feedback.call_seq, [c.method for c in sends]
    middle = {c.seq for c in sends[1:-1]}
    marking = [e for e in t.rich.edits(t.bot, s.chat, message_id) if e.call_seq in middle]
    assert len(marking) == len(middle), "entre a questão e o feedback, só a edição da marcação"
    assert all([b.label for b in (e.after.keyboard or ((),))[0]] == MARKED_CHOICE_ROW for e in marking)
    gaps = [b.started - a.started for a, b in pairwise(sends)]
    assert min(gaps) >= PACED_CHAT_INTERVAL - PACE_TOLERANCE, (
        f"controle: intervalo por chat de {min(gaps):.3f} s entre envios (limite {PACED_CHAT_INTERVAL} s)")
    assert answer.started < sends[1].started - PACE_TOLERANCE, "a confirmação sai antes da edição seguinte"
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []
