"""Ponta a ponta do disparo e da resposta pela Bot API falsa.


O mesmo roteiro do `test_console_e2e.py` (oferecimento → lista → vínculo → disparo → resposta → fechamento →
gabarito → atrasada), agora pelo canal do Telegram, com **30 alunos**: botões, edições, confirmações de
toque e a interface de resposta (o pedido de campo com `ForceReply` e a dica), no modo padrão
(`DeliverySettings()`: formatação rica, 12 botões, 1 mensagem por segundo por chat e 30 por segundo por bot).

- **Oferecimento, bot e lista** pela API de oferecimentos e bots e do vínculo do aluno; **convites** pelo envio em duas fases, com os
  e-mails mandados pelo worker (`verification=` o adaptador SMTP real sobre o SMTP falso); **vínculo** pelo
  Telegram: cada aluno manda `/start <código>` (o código do link do e-mail) e `/aceito` pela Bot API falsa.
- **Worker com bots** (`run_worker(…, bots=…)`, o caminho de produção) sobre a Bot API falsa com a extensão
  do disparo e da resposta (`rich_bot_api`), o motor falso estrito pelo adaptador HTTP real (`HttpQuestionEngine`) e a obtenção
  falsa (`repo_fetcher`).
- **Disparos**: seis imediatos, um por questão do curso de teste (escolha única, múltipla, número, inteiro,
  texto, várias partes); a prévia exata (com o aviso `no_teacher_contact` do oferecimento no padrão,
  mensagens livres desligadas); a confirmação; cada aluno recebe a sua variante com o teclado do tipo (`(a)` por
  alternativa e "Enviar" na única, com confirmação; `⬜ (a)` e "Enviar" na múltipla; "Não sei" em toda questão;
  até 4 chaves por linha) e, no campo digitável, o pedido do campo em resposta à questão,
  com `ForceReply` e a dica; o dado de cada botão na gramática `cq1:` com o `button_token` da
  entrega e no máximo 64 bytes.
- **Respostas**, por grupos de alunos: 10 acertam tudo (marcações e "Enviar", número com vírgula,
  inteiro, texto e as cinco partes; a escolha única em duas rodadas, marcar e "Enviar"); 10 erram, e
  no texto respondem com `<`, `&`, tags, uma URL e um comando
  (o eco sai escapado, a Bot API falsa aceita o markup e o aluno vê o texto literal); 5 tocam "Não
  sei" em tudo; 5 não respondem, e três deles respondem atrasado depois do fechamento (dois pelos botões,
  um pelo pedido do campo). Cada resposta: a linha literal, o desfecho, a resposta crua exata no motor,
  a submissão como o motor devolveu, a entrega, a edição com o feedback (sem botões) e o
  evento exato. O feedback em três faixas (✅, ◐, ❌) com a nota, e as marcas por alternativa da múltipla.

- **Fechamento** pelo relógio: a edição de fechamento, com "Janela fechada."; o gabarito (a
  sentinela do motor falso, controle positivo) e o `revealed_at` só às respondidas e às "Não sei";
  nas `no_answer`, os controles de resposta com o "Não sei" (o mesmo teclado da abertura) e o aviso da
  atrasada, sem gabarito; o `dispatch.closed` exato. Na atrasada, o gabarito sai com o feedback.
- **Protocolo e política**: zero `protocol_errors()` e `policy_errors()` na `rich_bot_api` e zero
  `protocol_errors()` no `engine_fake` (também conferidos pelas fixtures); todo toque confirmado
  exatamente uma vez; o ritmo por chat (1 por segundo) e por bot (30 por segundo) nas chamadas de envio e
  de edição; a sentinela do gabarito ausente de toda chamada à `rich_bot_api` anterior ao fechamento.
  Os tempos medidos ficam em `test_telegram_timing.py`.

Os textos esperados são os do disparo e da resposta e os do aluno, por extenso. Horários pelo `FakeClock`,
que só anda com o sistema parado e em saltos de até 10 s (a posse do bot conferida a cada salto; a margem
de renovação do worker é de 13 s): o preparo e a validação em `t_prep`, a confirmação, a abertura e a
entrega em `t_conf`, as respostas em `t_ans`. Ids internos, `seq`, seeds, tokens, `update_id` e pseudônimos
nunca como literal.

Leituras [I] deste arquivo (escolhas deste teste):
- o ritmo é medido pelo `started` (o `monotonic()` da chegada do pedido à Bot API falsa), e o limitador
  registra o envio na posse, antes do HTTP: os intervalos medidos no dublê podem encolher pelo tempo de
  montagem e de transporte de cada pedido. A conferência aceita `PACE_TOLERANCE` (0,2 s) de encolhimento,
  bem abaixo do intervalo de 1 s (sem o limitador, as mensagens ao mesmo chat sairiam a milissegundos);
- a confirmação de um toque válido não tem texto fixado (a conferência é "uma e só uma" por toque);
- o eco de uma escolha no feedback (`FEEDBACK`: "Sua resposta: {echo} · {verdict} ·
  {score}") traz a chave e o texto de cada alternativa escolhida, e nenhuma não escolhida; a
  forma não está fixada: conferem-se só a presença e a ausência entre "Sua resposta:" e o veredito;
- o resultado tem três faixas (✅ com nota ≥ 1, ◐ com 0 < nota < 1, ❌ com nota ≤ 0) e a nota "nota N%" sai em
  toda correção válida, com N = 100 (nota ≥ 1), ⌊nota × 100⌋ (0 < nota < 1) ou 0 (nota ≤ 0): "{verdict} ·
  {score}" (`SCORE`); na múltipla, cada alternativa
  escolhida do eco termina em " ✅" (entre as corretas) ou " ❌", logo depois do texto dela;
- a edição do feedback e a do "Não sei" levam a entrega sem botões (estado terminal); a edição de
  fechamento de uma `no_answer` leva os controles de resposta com o "Não sei", o mesmo teclado da abertura
  (o gabarito só vai a quem já respondeu), e nas questões digitáveis só o
  "Não sei";
- nas partes do `submission.graded`, `ratio` e `residual` das escolhas são nulos, e os das demais são os
  gravados em `submissions.part_derived`; `choices` são os `content_hash` das marcadas, na ordem das opções;
- o pedido do campo `k` (base 0) de `n` começa por "Campo {k+1} de {n}:".

No esqueleto, o vínculo passa, e a função falha no primeiro
`dispatches.prepare` (rota ausente, 404). Alguns casos dependem de partes
implementadas depois e só passam quando elas existirem.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.buttons import CALLBACK_DATA
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.tasks import NO_HANDLER_ERROR
from cq_server.telegramrender import TelegramRichFormatter
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_events,
    prepare_dispatch,
    prepare_invites,
    register_bot,
    send_invites,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso
from tests.helpers.engine import ANSWER_SENTINEL

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

COURSE = "Programação de Computadores"
TERM = "2031s2"
ZONE = "America/Sao_Paulo"
CODE = "telegram-ponta-a-ponta"
BOT_USERNAME = "cq_telegram_ponta_bot"
WORKER_ID = "worker-telegram-ponta"
REPO = "https://repos.cq.test/disciplina/questoes-ponta-telegram.git"
COMMIT = "7a2c" * 10
ORIGIN = "integration"
WINDOW = timedelta(minutes=10)
ENGINE_TIMEOUT = 10.0
WAIT = 60.0  # segundos reais de espera por uma condição (30 chats a 1 mensagem por segundo)
CLOCK_STEP = 10  # segundos por salto do relógio com o worker no ar
LEASE_MARGIN = 13  # consulta 1 s + Telegram 2 s + renovação 10 s (bot_settings dos testes)
RECHECK = 6  # avanço que vence `CLOSE_RECHECK_SECONDS` (5 s) entre as rodadas do fechamento
RESPONSE_SECONDS = 30
CHAT_INTERVAL = 1.0  # 1 mensagem por segundo por chat (padrão de `DeliverySettings`)
BOT_PER_SECOND = 30  # 30 por segundo por bot
PACE_TOLERANCE = 0.2  # ver a docstring do módulo
USER_BASE = 700
QIDS = ("single_choice", "multiple_choice", "number", "integer", "text", "multipart")
N = 30
OK = tuple(range(0, 10))  # acertam tudo
BAD = tuple(range(10, 20))  # erram; no texto, a resposta com `<`, `&`, tags, URL e comando
DK = tuple(range(20, 25))  # "Não sei" em tudo
LATE = tuple(range(25, 30))  # não respondem; 25 e 26 respondem a única atrasados, 27 o número
SPECIAL = "<b>azul</b> & 3 < 4 <script>x</script> https://exemplo.test/?a=1&b=2 /start"

DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                  "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
                  "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                  "submission.graded", "submission.internal_failure")
SEND_METHODS = frozenset({"sendMessage", "sendRichMessage", "editMessageText", "editMessageReplyMarkup",
                          "sendPhoto", "sendDocument"})

# Textos por extenso: os do aluno e os do disparo e da resposta.
WELCOME = (f"Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {COURSE} ({TERM}). As "
           "questões da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação.")
CLOSED_NOTICE = "Janela fechada."
LATE_HOWTO = 'Você ainda pode responder, fora do prazo: use os botões ou toque em "Responder" nesta mensagem.'
FEEDBACK_HEAD = "Sua resposta:"
RESULT_HEAD = "Resultado:"
DEADLINE_HEAD = "Prazo:"
CORRECT = "✅ correta"
PARTIAL = "◐ parcialmente correta"  # `VERDICT_PARTIAL`
INCORRECT = "❌ incorreta"
DONT_KNOW = "Não sei"
SUBMIT = "Enviar"

NUMBER = {"type": "number", "label": "Velocidade:", "suffix": "m/s", "hint": "Número; ex.: 4.25, -17 ou 3/7"}
INTEGER = {"type": "integer", "label": "Quantidade:", "suffix": None, "hint": "Número inteiro; ex.: 42 ou -17"}
TEXT = {"type": "text", "label": "Cor:", "suffix": None, "hint": "Texto"}
SINGLE = {"type": "single_choice", "label": None, "suffix": None, "hint": "Escolha uma alternativa"}
MULTIPLE = {"type": "multiple_choice", "label": None, "suffix": None, "hint": "Marque de 1 a 4 alternativas"}
COLORS = ("Vermelho", "Azul", "Verde", "Amarelo")
PRIMES = ("Dois", "Nove", "Sete", "Dez")
CHOICE_TYPES = frozenset({"single_choice", "multiple_choice"})
# `FEEDBACK`: "Sua resposta: {echo} · {verdict} · {score}" (três faixas: correta, parcial e errada).
_ECHO = re.compile(r"Sua resposta:\s*(?P<echo>.*?)\s*·\s*(?:✅|◐|❌)", re.S)
# O aviso da prévia de um oferecimento no padrão (mensagens livres
# desligadas e sem contato do professor), como este.
NO_TEACHER_CONTACT = "no_teacher_contact"
NO_TEACHER_CONTACT_MESSAGE = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                              "o aluno que escrever fora das questões não saberá a quem se dirigir.")
NO_TEACHER_CONTACT_WARNING = {"kind": NO_TEACHER_CONTACT, "message": NO_TEACHER_CONTACT_MESSAGE}


@dataclasses.dataclass(frozen=True)
class _Spec:
    title: str
    statement: str
    fields: tuple[dict, ...]
    options: tuple[str, ...]


SPECS: dict[str, _Spec] = {
    "single_choice": _Spec("Cor do céu", "Qual é a cor do céu num dia limpo?", (SINGLE,), COLORS),
    "multiple_choice": _Spec("Números primos", "Marque os números primos.", (MULTIPLE,), PRIMES),
    "number": _Spec("Velocidade média", "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?", (NUMBER,),
                    ()),
    "integer": _Spec("Contagem", "Quantas unidades há em quatro dezenas e duas unidades?", (INTEGER,), ()),
    "text": _Spec("Cor em uma palavra", "Escreva, em uma palavra minúscula, a cor do céu num dia limpo.",
                  (TEXT,), ()),
    "multipart": _Spec("Várias partes", "Responda às cinco partes.", (NUMBER, INTEGER, TEXT, SINGLE, MULTIPLE),
                       ()),
}
# os teclados na abertura (até 4 chaves por linha; "Enviar" e "Não sei" em linhas
# próprias; o campo corrente digitável só com o "Não sei"); a única com o "Enviar".
OPEN_KEYBOARDS: dict[str, list[list[str]]] = {
    "single_choice": [["(a)", "(b)", "(c)", "(d)"], [SUBMIT], [DONT_KNOW]],
    "multiple_choice": [["⬜ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)"], [SUBMIT], [DONT_KNOW]],
    "number": [[DONT_KNOW]], "integer": [[DONT_KNOW]], "text": [[DONT_KNOW]], "multipart": [[DONT_KNOW]],
}
# A edição de fechamento de uma `no_answer` leva os controles de resposta com o "Não sei" (o gabarito só
# vai a quem já respondeu): o mesmo teclado da abertura.
LATE_KEYBOARDS: dict[str, list[list[str]]] = dict(OPEN_KEYBOARDS)
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}


# --- montagem ----------------------------------------------------------------------------------------


@dataclasses.dataclass
class _S:
    i: int
    user: Any
    name: str
    ra: str
    email: str
    pseudo: str = ""
    person_row: int = 0

    @property
    def chat(self) -> int:
        return self.user.user_id

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


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
    engine_fake: Any
    offering: dict
    bot: int
    bot_row: int
    offering_row: int
    students: list[_S]
    closes: datetime | None = None
    dispatches: dict[str, dict] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _Final:
    """O desfecho esperado de uma entrega depois das respostas: `graded` (com a resposta crua, a literal,
    os campos normalizados, o eco, as alternativas escolhidas, a marca de cada escolhida da múltipla e o
    update que a completou) ou `dont_know` (com as partes)."""

    kind: str
    uid: int
    raw: dict | None = None
    literal: dict | None = None
    normalized: tuple[str, ...] = ()
    echo: str | None = None
    parts: dict | None = None
    chosen: tuple[str, ...] | None = None
    marks: dict[str, bool] | None = None


def _verdict(score: float) -> str:
    """As três faixas do resultado: ✅ com nota ≥ 1, ◐ com 0 < nota < 1, ❌ com
    nota ≤ 0 (as do `determine_score_params` do PL fixado)."""
    return CORRECT if score >= 1 else PARTIAL if score > 0 else INCORRECT


def _score_text(score: float) -> str:
    """A nota mostrada ao aluno (`SCORE`): "nota N%", com N = 100 (nota
    ≥ 1), ⌊nota × 100⌋ (0 < nota < 1) ou 0 (nota ≤ 0), o número do selo do PL."""
    return f"nota {100 if score >= 1 else math.floor(score * 100) if score > 0 else 0}%"


def _assert_marks(text: str, options: Mapping[str, str], marks: Mapping[str, bool]) -> None:
    """Feedback de nota parcial (`FEEDBACK`): na múltipla, cada alternativa escolhida do eco
    termina em " ✅" (chave entre as corretas) ou " ❌", logo depois do texto dela; procurada só no eco (entre
    "Sua resposta:" e o veredito), sem atravessar a vírgula nem a linha (o enunciado lista as alternativas)."""
    match = _ECHO.search(text or "")
    assert match, f"feedback sem 'Sua resposta: {{eco}} · {{veredito}}': {text!r}"
    echo = match.group("echo")
    for key, right in marks.items():
        mark = "✅" if right else "❌"
        pattern = rf"\({re.escape(key)}\) {re.escape(options[key])}[^,·\n]*? {mark}"
        assert re.search(pattern, echo), f"sem a marca {mark} da alternativa ({key}) no eco: {echo!r}"


def _assert_choice_echo(text: str | None, options: Mapping[str, str], chosen: Iterable[str]) -> None:
    """o eco do feedback (entre "Sua resposta:" e o veredito) traz a chave (símbolo
    isolado) e o texto de cada alternativa escolhida, e nem a chave nem o texto de uma não escolhida; sem
    fixar ordem, separador nem pontuação. O enunciado (com todas as alternativas) e o gabarito revelado
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


def _one(sql: psycopg.Connection, query: str, params: tuple = ()) -> dict | None:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchone()


def _all(sql: psycopg.Connection, query: str, params: tuple = ()) -> list[dict]:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


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


def _advance(t: _T, seconds: float) -> datetime:
    """Avança o relógio em saltos de até 10 s, esperando a posse do bot ter folga depois de cada salto."""
    left = seconds
    while left > 0:
        step = min(CLOCK_STEP, left)
        left -= step
        t.clock.advance(step)
        _wait(lambda: _lease_left(t) >= LEASE_MARGIN, "a renovação da posse do bot")
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
    for _ in range(10):
        _wait(lambda: not _busy(t), "o sistema parar")
        time.sleep(1.2)
        if not _busy(t):
            return
    raise AssertionError(f"o sistema não parou: {_busy(t)}")


def _ev(type_: str, at: datetime, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _last_seq(t: _T) -> int:
    return t.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events(t: _T, cursor: int, *types: str) -> list[dict]:
    out: list[dict] = []
    after = cursor
    while True:
        page = list_events(t.client, ADMIN_TOKEN, *types, after=after, limit=500)
        items = page["items"]
        out += [{k: v for k, v in e.items() if k != "seq"} for e in items]
        if not items or page.get("next_cursor") is None:
            return out
        after = items[-1]["seq"]


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _same(got: list[dict], expected: list[dict]) -> None:
    assert sorted(map(_canon, got)) == sorted(map(_canon, expected))


def _counts(**values: int) -> dict[str, int]:
    return {**ZERO_COUNTS, **values}


# --- Bot API falsa: updates, mensagens, botões ---------------------------------------------------------


def _update(t: _T, uid: int) -> dict | None:
    return _one(t.sql, "SELECT * FROM telegram_updates WHERE bot_id = %s AND update_id = %s", (t.bot_row, uid))


def _processed(t: _T, uids: dict[int, int], outcomes: dict[int, str]) -> dict[int, dict]:
    """Espera os updates (aluno → `update_id`) processados e confere o desfecho de cada um."""

    def done() -> dict[int, dict] | None:
        rows = {i: _update(t, uid) for i, uid in uids.items()}
        return rows if all(r is not None and r["state"] == "processed" for r in rows.values()) else None

    rows = _wait(done, f"{len(uids)} updates processados")
    assert {i: r["outcome"] for i, r in rows.items()} == outcomes
    return rows


def _delivery(t: _T, qid: str, s: _S) -> dict:
    row = _one(t.sql, """
        SELECT d.id, d.delivery_uuid::text AS delivery_uuid, d.state, d.undelivered_reason, d.failure,
               d.current_field, d.seed, d.button_token, d.variant_at, d.sent_at, d.answered_at, d.revealed_at,
               d.principal_message_id, c.snapshot, c.ir, c.parts
          FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id
          JOIN enrollments e ON e.id = d.enrollment_id
          LEFT JOIN delivery_contents c ON c.delivery_id = d.id
         WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s""", (t.dispatches[qid]["dispatch_id"], s.person_row))
    assert row is not None, f"sem entrega do aluno {s.i} em {qid}"
    return row


def _question_id(t: _T, qid: str, s: _S) -> int:
    delivery = _delivery(t, qid, s)
    assert delivery["principal_message_id"] is not None, f"entrega de {s.i} em {qid} sem principal"
    ref = t.sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s",
                        (delivery["principal_message_id"],)).fetchone()[0]
    return int(ref)


def _question(t: _T, qid: str, s: _S) -> Any:
    """A mensagem com os controles (a principal) da entrega, no estado atual da Bot API falsa."""
    return t.rich.current(t.bot, s.chat, _question_id(t, qid, s))


def _prompt(t: _T, qid: str, s: _S, index: int) -> Any:
    """O pedido do campo `index` enviado: `ForceReply` com a dica, em resposta à questão."""
    delivery = _delivery(t, qid, s)

    def sent() -> list[tuple]:
        return t.sql.execute(
            "SELECT channel_ref FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' "
            "AND field_index = %s AND state = 'sent'", (delivery["id"], index)).fetchall()

    rows = _wait(sent, f"o pedido do campo {index} do aluno {s.i} em {qid}")
    assert len(rows) == 1
    message = t.rich.current(t.bot, s.chat, int(rows[0][0]))
    fields = SPECS[qid].fields
    assert (message.force_reply, message.placeholder, message.reply_to_message_id) == (
        True, fields[index]["hint"], _question_id(t, qid, s)), (qid, s.i, index)
    assert message.reply_parameters is not None and message.reply_parameters["allow_sending_without_reply"] is True
    assert (message.plain_text or "").startswith(f"Campo {index + 1} de {len(fields)}:"), message.plain_text
    return message


def _buttons(message: Any) -> list[tuple[str, int, str | None, str]]:
    """(ação, campo, chave, dado) de cada botão, pela gramática `cq1:`."""
    out = []
    for button in message.buttons():
        match = CALLBACK_DATA.fullmatch(button.data or "")
        assert match is not None, f"botão fora da gramática cq1: {button.label!r}"
        out.append((match.group(2), int(match.group(3), 36), match.group(5), button.data))
    return out


def _labels(message: Any) -> list[list[str]] | None:
    return None if message.keyboard is None else [[b.label for b in row] for row in message.keyboard]


def _tap(t: _T, qid: str, s: _S, action: str, key: str | None = None, *, field: int = 0) -> int:
    """Toque no botão (`action`, `key`) do campo `field` do teclado corrente (esperado até o teclado
    mostrar esse campo); devolve o `update_id` do toque."""

    def ready() -> Any:
        message = _question(t, qid, s)
        found = [data for a, f, k, data in _buttons(message) if (a, f, k) == (action, field, key)]
        return (message, found[0]) if len(found) == 1 else None

    message, data = _wait(ready, f"o botão {action}/{key} do campo {field} do aluno {s.i} em {qid}")
    return t.rich.tap(t.bot, s.user, message.message_id, data).update_id


def _reply(t: _T, s: _S, message_id: int, text: str) -> int:
    uid = t.rich.push_text(t.bot, s.user, text, reply_to_message_id=message_id)
    assert uid is not None
    return uid


def _step(t: _T, actions: dict[int, Callable[[], int]], outcomes: dict[int, str]) -> dict[int, dict]:
    """Uma rodada de respostas (uma entrada por aluno, "ao mesmo tempo"): dispara todas e espera cada
    update processado com o desfecho."""
    uids = {i: act() for i, act in actions.items()}
    return _processed(t, uids, outcomes)


# --- conferências --------------------------------------------------------------------------------------


def _submissions(t: _T, delivery: dict) -> list[dict]:
    return _all(t.sql, "SELECT *, submission_uuid::text AS submission_id FROM submissions WHERE delivery_id = %s "
                "ORDER BY id", (delivery["id"],))


def _grade_calls(t: _T, qid: str, delivery: dict) -> list[Any]:
    return [c for c in t.engine_fake.calls("grade") if c.request is not None
            and c.request["snapshot"] == delivery["snapshot"] and c.request["question_ref"]["qid"] == qid]


def _option_hash(delivery: dict, index: int, key: str) -> str:
    found = [o["content_hash"] for o in delivery["ir"]["fields"][index]["options"] if o["key"] == key]
    assert len(found) == 1
    return found[0]


def _derived(stored: Any, index: int) -> dict:
    item = stored[index] if isinstance(stored, list) else stored[str(index)]
    assert isinstance(item, dict)
    return item


def _parts(delivery: dict, submission: dict, raw: dict, response: dict) -> list[dict]:
    parts = []
    for index, field in enumerate(delivery["ir"]["fields"]):
        name = field["name"]
        score = response["partial_scores"][name]["score"]
        if field["type"] in CHOICE_TYPES:
            value = raw.get(name)
            chosen = [] if value is None else ([value] if isinstance(value, str) else list(value))
            keys = [o["key"] for o in field["options"]]
            choices = [_option_hash(delivery, index, k) for k in keys if k in chosen]
            ratio = residual = None
        else:
            derived = _derived(submission["part_derived"], index)
            choices, ratio, residual = [], derived.get("ratio"), derived.get("residual")
        parts.append({"index": index, "score": score, "choices": choices, "ratio": ratio, "residual": residual})
    return parts


def _last_edit(t: _T, qid: str, s: _S, since: float) -> Any:
    edits = [e for e in t.rich.edits(t.bot, s.chat, _question_id(t, qid, s)) if e.started >= since]
    assert edits, f"sem edição da questão do aluno {s.i} em {qid}"
    return edits[-1]


def _check_graded(t: _T, qid: str, s: _S, final: _Final, *, late: bool, at: datetime, response_ms: int,
                  since: float) -> dict:
    """A correção válida, a entrega, a edição do feedback e o evento exato."""
    delivery = _delivery(t, qid, s)
    update = _update(t, final.uid)
    assert update is not None
    graded = [x for x in _submissions(t, delivery) if x["outcome"] == "graded"]
    assert len(graded) == 1, f"submissões válidas do aluno {s.i} em {qid}: {len(graded)}"
    submission = graded[0]
    calls = _grade_calls(t, qid, delivery)
    assert len(calls) == 1, f"correções no motor do aluno {s.i} em {qid}: {len(calls)}"
    assert calls[0].request["raw_submitted_answers"] == final.raw
    response = calls[0].response
    assert (submission["valid"], submission["score"], submission["partial_scores"], submission["submitted_answers"],
            submission["format_errors"], submission["feedback"], submission["params"],
            submission["correct_answers"]) == (
        True, response["score"], response["partial_scores"], response["submitted_answers"],
        response["format_errors"], response["feedback"], response["params"], response["correct_answers"])
    assert (submission["refusal"], submission["late"], list(submission["normalized_fields"]),
            submission["raw_input"], submission["received_at"], submission["response_ms"],
            submission["telegram_update_id"]) == (
        None, late, list(final.normalized), final.literal, at, response_ms, update["id"])
    assert (delivery["state"], delivery["answered_at"], delivery["current_field"]) == (
        "answered_late" if late else "answered", at, None)
    edit = _last_edit(t, qid, s, since)
    # O resultado em três faixas e a nota em toda correção válida, "{verdict} ·
    # {score}" (`FEEDBACK`).
    verdict = f"{_verdict(response['score'])} · {_score_text(response['score'])}"
    shown = edit.after.plain_text or ""
    head = RESULT_HEAD if len(SPECS[qid].fields) > 1 else FEEDBACK_HEAD
    assert head in shown and verdict in shown, (qid, s.i, verdict, shown)
    assert edit.after.buttons() == [], "a entrega encerrada fica sem botões"
    if final.echo is not None:
        assert final.echo in shown, (qid, s.i, shown)
    if final.chosen is not None:
        _assert_choice_echo(shown, dict(zip("abcd", SPECS[qid].options)), final.chosen)
    if final.marks is not None:
        _assert_marks(shown, dict(zip("abcd", SPECS[qid].options)), final.marks)
    assert (ANSWER_SENTINEL in shown) is late, "o gabarito só aparece na atrasada (revelado com o feedback)"
    return _ev("submission.graded", at, offering_id=t.offering["offering_id"],
               dispatch_id=t.dispatches[qid]["dispatch_id"], delivery_id=delivery["delivery_uuid"],
               submission_id=submission["submission_id"],
               question_ref_id=t.dispatches[qid]["question"]["question_ref_id"], person=s.pseudo, valid=True,
               late=late, normalized=bool(final.normalized), score=response["score"],
               parts=_parts(delivery, submission, final.raw or {}, response))


def _check_dont_know(t: _T, qid: str, s: _S, final: _Final, *, at: datetime, since: float) -> dict:
    delivery = _delivery(t, qid, s)
    update = _update(t, final.uid)
    assert update is not None
    subs = _submissions(t, delivery)
    assert [x["outcome"] for x in subs] == ["dont_know"], (qid, s.i)
    assert (subs[0]["valid"], subs[0]["score"], subs[0]["raw_input"], subs[0]["received_at"],
            subs[0]["telegram_update_id"]) == (None, None, final.parts, at, update["id"])
    assert _grade_calls(t, qid, delivery) == []
    assert (delivery["state"], delivery["answered_at"], delivery["current_field"]) == ("dont_know", at, None)
    edit = _last_edit(t, qid, s, since)
    assert edit.after.buttons() == [] and ANSWER_SENTINEL not in (edit.after.plain_text or "")
    return _ev("delivery.dont_know", at, offering_id=t.offering["offering_id"],
               dispatch_id=t.dispatches[qid]["dispatch_id"], delivery_id=delivery["delivery_uuid"],
               submission_id=subs[0]["submission_id"], person=s.pseudo)


def _check_question(t: _T, qid: str, s: _S) -> None:
    """A mensagem da questão: Rich, enunciado, alternativas, prazo, o teclado do tipo com os
    dados `cq1:` da entrega (≤ 64 bytes), sem gabarito; o pedido do primeiro campo digitável."""
    spec = SPECS[qid]
    delivery = _delivery(t, qid, s)
    message = _question(t, qid, s)
    assert (message.method, message.text_format) == ("sendRichMessage", "rich_html")
    shown = message.plain_text or ""
    for piece in (spec.statement, DEADLINE_HEAD, *spec.options):
        assert piece in shown, (qid, s.i, piece)
    assert ANSWER_SENTINEL not in json.dumps(message.rich_message, ensure_ascii=False)
    assert _labels(message) == OPEN_KEYBOARDS[qid], (qid, s.i, _labels(message))
    for action, field, _key, data in _buttons(message):
        assert data.split(":")[1] == delivery["button_token"] and field == 0 and action in "cmsn"
        assert len(data.encode("utf-8")) <= 64
    if spec.fields[0]["type"] not in CHOICE_TYPES:
        _prompt(t, qid, s, 0)


def _close_all(t: _T, timeout: float = 3 * WAIT) -> None:
    """Espera os seis disparos `closed`; sem isso, avança o relógio além de `CLOSE_RECHECK_SECONDS`."""
    deadline = time.monotonic() + timeout

    def states() -> dict[str, str]:
        return {qid: t.sql.execute("SELECT state FROM dispatches WHERE dispatch_uuid = %s::uuid",
                                   (t.dispatches[qid]["dispatch_id"],)).fetchone()[0] for qid in QIDS}

    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if set(states().values()) == {"closed"}:
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            raise AssertionError(f"os disparos não fecharam em {timeout} s: {states()}")
        _advance(t, RECHECK)


def _check_pacing(t: _T, after_seq: int) -> None:
    """nas chamadas de envio e de edição (a confirmação do toque fica fora), no máximo uma
    por segundo por chat e 30 por segundo por bot, medidas na Bot API falsa (ver a docstring)."""
    calls = sorted((c for c in t.rich.calls(bot_id=t.bot) if c.seq > after_seq and c.method in SEND_METHODS),
                   key=lambda c: c.started)
    assert len(calls) > N, "controle contra o vazio: a fase do disparo e da resposta enviou mensagens"
    by_chat: dict[int, list[float]] = defaultdict(list)
    for call in calls:
        by_chat[int(call.params["chat_id"])].append(call.started)
    for starts in by_chat.values():
        gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        assert not gaps or min(gaps) >= CHAT_INTERVAL - PACE_TOLERANCE, (
            f"ritmo por chat violado: intervalo de {min(gaps):.3f} s")
    starts = [c.started for c in calls]
    windows = [starts[k + BOT_PER_SECOND] - starts[k] for k in range(len(starts) - BOT_PER_SECOND)]
    assert not windows or min(windows) >= 1.0 - PACE_TOLERANCE, (
        f"ritmo por bot violado: {BOT_PER_SECOND + 1} envios em {min(windows):.3f} s")


# --- o roteiro -------------------------------------------------------------------------------------------


def test_telegram_end_to_end_with_thirty_students(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, rich_bot_api: Any, smtp: Any,
        mail_channel: Any, engine_fake: Any, repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """O roteiro pela Bot API falsa estendida com 30 alunos
    (vínculo do aluno, seis disparos, botões, edições, confirmações de toque, a interface de resposta,
    fechamento, gabarito e atrasadas); respostas com `<`, `&`, tags, URLs e comandos; zero erro de protocolo
    e de política na Bot API falsa e de protocolo no motor falso; todo toque confirmado uma vez; o ritmo por
    chat e por bot; a sentinela do gabarito ausente de toda chamada anterior ao fechamento."""
    rich = rich_bot_api
    students = [_S(i=i, user=make_user(USER_BASE + i), name=f"Pessoa Telegram Ponta {i:02d}", ra=f"59200005{i:02d}",
                   email=f"telegram.ponta{i:02d}@turma.example.org") for i in range(N)]
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, course=COURSE, term=TERM, timezone=ZONE)
    bot = rich.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, CODE, rich.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, CODE)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    body = import_students(client, ADMIN_TOKEN, CODE, [s.row() for s in students])
    assert (body["received"], body["enrollments_created"]) == (N, N)
    for s in students:
        s.person_row, s.pseudo = sql.execute(
            "SELECT p.id, p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
            (s.ra,)).fetchone()
    t = _T(client=client, sql=sql, clock=clock, rich=rich, engine_fake=engine_fake, offering=offering, bot=bot,
           bot_row=bot_row, offering_row=offering_row, students=students)
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    default_channel = _NoRouteChannel()
    try:
        bot_worker_thread(channel=default_channel, verification=mail_channel, engine=engine, fetcher=repo_fetcher,
                          delivery=DeliverySettings(), settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(lambda: rich.pending_polls(bot), "o leitor do bot consultando")
        _link(t, smtp)
        _flow(t, repo_fetcher)
        assert default_channel.calls == []
    finally:
        bot_worker_thread.stop()
        with contextlib.suppress(NotImplementedError):
            engine.close()


def _link(t: _T, smtp: Any) -> None:
    """Convites pelo vínculo do aluno (e-mails pelo worker) e o vínculo pelo Telegram: `/start <código>` e `/aceito`."""
    prepared = prepare_invites(t.client, ADMIN_TOKEN, CODE)
    assert prepared["eligible"] == N
    assert send_invites(t.client, ADMIN_TOKEN, CODE, prepared["confirmation_code"])["queued"] == N
    _wait(lambda: len(smtp.messages()) == N, "os trinta e-mails de convite")
    codes = {}
    for s in t.students:
        links = [link for link in smtp.invite_links() if link.rcpt_to == (s.email,)]
        assert len(links) == 1 and links[0].bot_username == BOT_USERNAME
        codes[s.i] = links[0].code
    t_link = _advance(t, 10)
    cursor = _last_seq(t)
    uids = {s.i: t.rich.push_start(t.bot, s.user, codes[s.i]) for s in t.students}
    _processed(t, uids, {s.i: "student_terms_presented" for s in t.students})
    uids = {s.i: t.rich.push_text(t.bot, s.user, "/aceito", bot_command=True) for s in t.students}
    _processed(t, uids, {s.i: "student_joined" for s in t.students})
    for s in t.students:
        _wait(lambda s=s: [m for m in t.rich.messages(t.bot, s.chat) if m.text == WELCOME],
              f"as boas-vindas do aluno {s.i}")
    endpoints = _all(t.sql, "SELECT d.person_id, d.chat_id, d.state FROM delivery_endpoints d WHERE d.bot_id = %s "
                     "ORDER BY d.person_id", (t.bot_row,))
    assert endpoints == [{"person_id": s.person_row, "chat_id": s.chat, "state": "active"}
                         for s in sorted(t.students, key=lambda s: s.person_row)]
    _same(_events(t, cursor, "student.joined"),
          [_ev("student.joined", t_link, offering_id=t.offering["offering_id"], person=s.pseudo, terms_version=1,
               endpoint_replaced=False) for s in t.students])
    _settle(t)


def _flow(t: _T, repo_fetcher: Any) -> None:
    offering_id = t.offering["offering_id"]
    r5_mark = max((c.seq for c in t.rich.calls()), default=0)
    by_i = {s.i: s for s in t.students}

    # 1. Preparo e validação dos seis disparos; a prévia exata no modo padrão (só o aviso `no_teacher_contact`
    # do oferecimento no padrão: mensagens livres desligadas e sem contato).
    t.engine_fake.program_presets()
    repo_fetcher.add(REPO, COMMIT)
    t_prep = _advance(t, 10)
    t.closes = t_prep + WINDOW
    cursor = _last_seq(t)
    for qid in QIDS:
        prepared = prepare_dispatch(t.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid=qid,
                                    closes_at=iso(t.closes), origin=ORIGIN)
        assert (prepared["state"], prepared["created_at"], prepared["closes_at"], prepared["preview"]) == (
            "validating", iso(t_prep), iso(t.closes), None)
        t.dispatches[qid] = prepared

    def validated() -> bool:
        return all(get_dispatch(t.client, ADMIN_TOKEN, CODE, t.dispatches[qid]["dispatch_id"])["state"]
                   != "validating" for qid in QIDS)

    _wait(validated, "as seis validações")
    _settle(t)
    shown: dict[str, dict] = {}
    expected: list[dict] = []
    for qid in QIDS:
        prepared = t.dispatches[qid]
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, prepared["dispatch_id"])
        [sample] = t.engine_fake.issued_variants(qid)
        statement = TelegramRichFormatter().plain(sample.ir["statement"]["html"])
        assert SPECS[qid].statement in statement and "<" not in statement
        assert body == {**prepared, "state": "scheduled", "validated_at": iso(t_prep), "target_count": N,
                        "preview": {"title": SPECS[qid].title, "statement": statement,
                                    "fields": [dict(f) for f in SPECS[qid].fields], "target_count": N,
                                    "warnings": [NO_TEACHER_CONTACT_WARNING]}}
        assert ANSWER_SENTINEL not in json.dumps(body, ensure_ascii=False)
        shown[qid] = body
        expected += [
            _ev("dispatch.prepared", t_prep, "bootstrap", offering_id=offering_id, dispatch_id=body["dispatch_id"],
                question_ref_id=body["question"]["question_ref_id"], origin=ORIGIN),
            _ev("dispatch.validated", t_prep, offering_id=offering_id, dispatch_id=body["dispatch_id"],
                target_count=N, warnings=[NO_TEACHER_CONTACT])]
    _same(_events(t, cursor, *DISPATCH_EVENT_TYPES), expected)

    # 2. Confirmação e abertura: cada aluno recebe a sua variante, com o teclado do tipo.
    t_conf = _advance(t, 10)
    cursor = _last_seq(t)
    for qid in QIDS:
        body = confirm_dispatch(t.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "confirmed": True, "confirmed_at": iso(t_conf), "opens_at": iso(t_conf)}

    def opened() -> bool:
        states = [st for (st,) in t.sql.execute(
            "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id WHERE x.offering_id = %s",
            (t.offering_row,)).fetchall()]
        queued = t.sql.execute("SELECT count(*) FROM outbox_messages WHERE delivery_id IS NOT NULL AND state IN "
                               "('pending', 'leased')").fetchone()[0]
        return len(states) == 6 * N and set(states) == {"delivered"} and queued == 0

    _wait(opened, "as cento e oitenta entregas")
    _settle(t)
    expected = []
    for qid in QIDS:
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "state": "open", "confirmed": True, "confirmed_at": iso(t_conf),
                        "opens_at": iso(t_conf), "opened_at": iso(t_conf), "deliveries": _counts(delivered=N)}
        shown[qid] = body
        expected += [
            _ev("dispatch.confirmed", t_conf, "bootstrap", offering_id=offering_id, dispatch_id=body["dispatch_id"],
                target_count=N, opens_at=iso(t_conf), closes_at=iso(t.closes)),
            _ev("dispatch.opened", t_conf, offering_id=offering_id, dispatch_id=body["dispatch_id"], target_count=N)]
        seeds = set()
        for s in t.students:
            delivery = _delivery(t, qid, s)
            assert (delivery["state"], delivery["current_field"], delivery["variant_at"], delivery["sent_at"]) == (
                "delivered", 0, t_conf, t_conf)
            [issued] = [v for v in t.engine_fake.issued_variants(qid) if int(v.seed, 36) == delivery["seed"]]
            assert delivery["snapshot"] == issued.snapshot
            seeds.add(delivery["seed"])
            expected.append(_ev("delivery.delivered", t_conf, offering_id=offering_id, dispatch_id=body["dispatch_id"],
                                delivery_id=delivery["delivery_uuid"], person=s.pseudo))
            _check_question(t, qid, s)
        assert len(seeds) == N, f"seeds repetidas em {qid}"
    _same(_events(t, cursor, *DISPATCH_EVENT_TYPES), expected)

    # 3. Respostas, por grupos, todas em `t_ans`.
    t_ans = _advance(t, RESPONSE_SECONDS)
    since = monotonic()
    cursor = _last_seq(t)
    finals: dict[tuple[str, int], _Final] = {}

    def record(qid: str, rows: dict[int, dict], make: Callable[[int], _Final | None]) -> None:
        for i, row in rows.items():
            final = make(i)
            if final is not None:
                final.uid = row["update_id"]
                finals[(qid, i)] = final

    def dk(i: int, parts: dict | None = None) -> _Final:
        return _Final("dont_know", 0, parts={} if parts is None else parts)

    # escolha única: marcar a alternativa (certa e errada), que só marca (✅), e depois "Enviar"; "Não sei"
    rows = _step(t, {**{i: (lambda s=by_i[i]: _tap(t, "single_choice", s, "m", "b")) for i in OK},
                     **{i: (lambda s=by_i[i]: _tap(t, "single_choice", s, "m", "a")) for i in BAD},
                     **{i: (lambda s=by_i[i]: _tap(t, "single_choice", s, "n")) for i in DK}},
                 {**{i: "answer_toggled" for i in OK + BAD}, **{i: "answer_dont_know" for i in DK}})
    record("single_choice", rows, lambda i: dk(i) if i in DK else None)
    for i in OK:
        _wait(lambda s=by_i[i]: _labels(_question(t, "single_choice", s))
              == [["(a)", "✅ (b)", "(c)", "(d)"], [SUBMIT], [DONT_KNOW]], f"a marca do aluno {i}")
    for i in BAD:
        _wait(lambda s=by_i[i]: _labels(_question(t, "single_choice", s))
              == [["✅ (a)", "(b)", "(c)", "(d)"], [SUBMIT], [DONT_KNOW]], f"a marca do aluno {i}")
    rows = _step(t, {i: (lambda s=by_i[i]: _tap(t, "single_choice", s, "s")) for i in OK + BAD},
                 {i: "answer_recorded" for i in OK + BAD})
    record("single_choice", rows, lambda i: _Final(
        "graded", 0, raw={"resposta": "b" if i in OK else "a"}, literal={"resposta": "b" if i in OK else "a"},
        chosen=("b",) if i in OK else ("a",)))
    # escolha múltipla: marcar, marcar, "Enviar" (a lista na ordem das opções; uma só marca como texto)
    rows = _step(t, {**{i: (lambda s=by_i[i]: _tap(t, "multiple_choice", s, "m", "a")) for i in OK},
                     **{i: (lambda s=by_i[i]: _tap(t, "multiple_choice", s, "m", "b")) for i in BAD},
                     **{i: (lambda s=by_i[i]: _tap(t, "multiple_choice", s, "n")) for i in DK}},
                 {**{i: "answer_toggled" for i in OK + BAD}, **{i: "answer_dont_know" for i in DK}})
    record("multiple_choice", rows, lambda i: dk(i) if i in DK else None)
    for i in OK:
        _wait(lambda s=by_i[i]: _labels(_question(t, "multiple_choice", s))
              == [["✅ (a)", "⬜ (b)", "⬜ (c)", "⬜ (d)"], [SUBMIT], [DONT_KNOW]], f"a marca do aluno {i}")
    for i in BAD:
        _wait(lambda s=by_i[i]: _labels(_question(t, "multiple_choice", s))
              == [["⬜ (a)", "✅ (b)", "⬜ (c)", "⬜ (d)"], [SUBMIT], [DONT_KNOW]], f"a marca do aluno {i}")
    _step(t, {i: (lambda s=by_i[i]: _tap(t, "multiple_choice", s, "m", "c")) for i in OK},
          {i: "answer_toggled" for i in OK})
    rows = _step(t, {i: (lambda s=by_i[i]: _tap(t, "multiple_choice", s, "s")) for i in OK + BAD},
                 {i: "answer_recorded" for i in OK + BAD})
    # o gabarito do preset é (a, c): as marcas por alternativa escolhida
    record("multiple_choice", rows, lambda i: _Final(
        "graded", 0, raw={"resposta": ["a", "c"] if i in OK else "b"},
        literal={"resposta": ["a", "c"] if i in OK else ["b"]}, chosen=("a", "c") if i in OK else ("b",),
        marks={"a": True, "c": True} if i in OK else {"b": False}))
    # número (com vírgula), inteiro e texto, em resposta ao pedido do campo; "Não sei" pelo botão
    for qid, right, wrong, raw_right, normalized in (("number", "6,5", "7", "6.5", ("resposta",)),
                                                     ("integer", "42", "41", "42", ()),
                                                     ("text", "azul", SPECIAL, "azul", ())):
        rows = _step(t, {**{i: (lambda s=by_i[i], q=qid, v=right: _reply(t, s, _prompt(t, q, s, 0).message_id, v))
                            for i in OK},
                         **{i: (lambda s=by_i[i], q=qid, v=wrong: _reply(t, s, _prompt(t, q, s, 0).message_id, v))
                            for i in BAD},
                         **{i: (lambda s=by_i[i], q=qid: _tap(t, q, s, "n")) for i in DK}},
                     {**{i: "answer_recorded" for i in OK + BAD}, **{i: "answer_dont_know" for i in DK}})
        record(qid, rows, lambda i, r=right, w=wrong, rr=raw_right, nf=normalized: dk(i) if i in DK else (
            _Final("graded", 0, raw={"resposta": rr}, literal={"resposta": r}, normalized=nf, echo=r) if i in OK
            else _Final("graded", 0, raw={"resposta": w}, literal={"resposta": w}, echo=w)))
    # várias partes: as digitáveis pelo pedido de cada campo, as de escolha pelos botões do campo corrente
    rows = _step(t, {**{i: (lambda s=by_i[i]: _reply(t, s, _prompt(t, "multipart", s, 0).message_id, "6,5"))
                        for i in OK},
                     **{i: (lambda s=by_i[i]: _reply(t, s, _prompt(t, "multipart", s, 0).message_id, "1"))
                        for i in BAD},
                     **{i: (lambda s=by_i[i]: _tap(t, "multipart", s, "n")) for i in DK}},
                 {**{i: "answer_part_recorded" for i in OK + BAD}, **{i: "answer_dont_know" for i in DK}})
    record("multipart", rows, lambda i: dk(i) if i in DK else None)
    for i in OK + BAD:
        delivery = _delivery(t, "multipart", by_i[i])
        assert (delivery["state"], delivery["current_field"], delivery["parts"]) == (
            "delivered", 1, {"num": "6,5" if i in OK else "1"})
    rows = _step(t, {**{i: (lambda s=by_i[i]: _reply(t, s, _prompt(t, "multipart", s, 1).message_id, "31"))
                        for i in OK},
                     **{i: (lambda s=by_i[i]: _tap(t, "multipart", s, "n", field=1)) for i in BAD}},
                 {**{i: "answer_part_recorded" for i in OK}, **{i: "answer_dont_know" for i in BAD}})
    record("multipart", rows, lambda i: dk(i, {"num": "1"}) if i in BAD else None)
    _step(t, {i: (lambda s=by_i[i]: _reply(t, s, _prompt(t, "multipart", s, 2).message_id, "azul")) for i in OK},
          {i: "answer_part_recorded" for i in OK})
    # a parte de escolha única: marcar e "Enviar"
    _step(t, {i: (lambda s=by_i[i]: _tap(t, "multipart", s, "m", "b", field=3)) for i in OK},
          {i: "answer_toggled" for i in OK})
    _step(t, {i: (lambda s=by_i[i]: _tap(t, "multipart", s, "s", field=3)) for i in OK},
          {i: "answer_part_recorded" for i in OK})
    _step(t, {i: (lambda s=by_i[i]: _tap(t, "multipart", s, "m", "a", field=4)) for i in OK},
          {i: "answer_toggled" for i in OK})
    _step(t, {i: (lambda s=by_i[i]: _tap(t, "multipart", s, "m", "c", field=4)) for i in OK},
          {i: "answer_toggled" for i in OK})
    rows = _step(t, {i: (lambda s=by_i[i]: _tap(t, "multipart", s, "s", field=4)) for i in OK},
                 {i: "answer_recorded" for i in OK})
    record("multipart", rows, lambda i: _Final(
        "graded", 0, raw={"num": "6.5", "int": "31", "txt": "azul", "mc": "b", "cb": ["a", "c"]},
        literal={"num": "6,5", "int": "31", "txt": "azul", "mc": "b", "cb": ["a", "c"]}, normalized=("num",)))

    def all_final() -> bool:
        for (qid, i), final in finals.items():
            subs = _submissions(t, _delivery(t, qid, by_i[i]))
            want = "graded" if final.kind == "graded" else "dont_know"
            if [x["outcome"] for x in subs if x["outcome"] != "refused"][-1:] != [want]:
                return False
        return True

    _wait(all_final, "as correções e os \"Não sei\"")
    _settle(t)
    assert len(finals) == 5 * 25 + 25, "controle: 150 entregas com desfecho"
    expected = []
    for (qid, i), final in finals.items():
        if final.kind == "graded":
            expected.append(_check_graded(t, qid, by_i[i], final, late=False, at=t_ans,
                                          response_ms=RESPONSE_SECONDS * 1000, since=since))
        else:
            expected.append(_check_dont_know(t, qid, by_i[i], final, at=t_ans, since=since))
    _same(_events(t, cursor, *DISPATCH_EVENT_TYPES), expected)
    for s in (by_i[i] for i in BAD):
        edit = _last_edit(t, "text", s, since)
        assert SPECIAL in (edit.after.plain_text or ""), "o aluno vê a própria resposta literal"
        assert "<script>" not in (edit.after.text or ""), "o eco vai escapado no Rich"
    before_close = {qid: _counts(answered=20, dont_know=5, delivered=5) for qid in QIDS}
    before_close["multipart"] = _counts(answered=10, dont_know=15, delivered=5)
    for qid in QIDS:
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "deliveries": before_close[qid]}

    # 4. Fechamento pelo relógio: a edição de fechamento (a revelação só às respondidas e às "Não sei") e
    # o `dispatch.closed`.
    before_close_mono = monotonic()
    leaked = [f"{c.method}#{c.seq}" for c in t.rich.calls() if c.started < before_close_mono
              and ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False)]
    assert leaked == [], f"o gabarito chegou ao aluno antes do fechamento: {leaked}"
    cursor = _last_seq(t)
    _advance(t, (t.closes - t.clock.now()).total_seconds() + 1)
    _close_all(t)
    _settle(t)
    closed_at = {qid: t.sql.execute("SELECT closed_at FROM dispatches WHERE dispatch_uuid = %s::uuid",
                                    (shown[qid]["dispatch_id"],)).fetchone()[0] for qid in QIDS}
    after_close = {qid: {**c, "no_answer": c["delivered"], "delivered": 0} for qid, c in before_close.items()}
    for qid in QIDS:
        for s in t.students:
            delivery = _delivery(t, qid, s)
            no_answer = s.i in LATE
            # a `no_answer` não é revelada (sem `revealed_at` e sem gabarito; ainda pode responder)
            assert delivery["revealed_at"] == (None if no_answer else closed_at[qid]), (qid, s.i)
            if no_answer:
                assert (delivery["state"], delivery["current_field"]) == ("no_answer", 0)
            edit = _last_edit(t, qid, s, before_close_mono)
            shown_text = edit.after.plain_text or ""
            assert CLOSED_NOTICE in shown_text, (qid, s.i)
            assert (ANSWER_SENTINEL in shown_text) is (not no_answer), (qid, s.i)
            assert (LATE_HOWTO in shown_text) is no_answer, (qid, s.i)
            assert _labels(edit.after) == (LATE_KEYBOARDS[qid] if no_answer else None), (qid, s.i)
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "state": "closed", "closed_at": iso(closed_at[qid]),
                        "deliveries": after_close[qid]}
        shown[qid] = body
    counts = {qid: {k: v for k, v in after_close[qid].items() if k != "grading_pending"} for qid in QIDS}
    _same(_events(t, cursor, *DISPATCH_EVENT_TYPES),
          [_ev("dispatch.closed", closed_at[qid], offering_id=offering_id, dispatch_id=shown[qid]["dispatch_id"],
               trigger="window", counts=counts[qid]) for qid in QIDS])

    # 5. Atrasadas: dois pelos botões da única fechada (marcar e "Enviar"; sem gabarito enquanto não é corrigida),
    # um pelo pedido do campo do número.
    t_late = _advance(t, 60)
    since_late = monotonic()
    cursor = _last_seq(t)
    late_ms = int((t_late - t_conf).total_seconds() * 1000)
    _step(t, {25: lambda: _tap(t, "single_choice", by_i[25], "m", "b"),
              26: lambda: _tap(t, "single_choice", by_i[26], "m", "b")},
          {25: "answer_toggled", 26: "answer_toggled"})
    for i in (25, 26):
        _wait(lambda s=by_i[i]: _labels(_question(t, "single_choice", s))
              == [["(a)", "✅ (b)", "(c)", "(d)"], [SUBMIT], [DONT_KNOW]], f"a marca atrasada do aluno {i}")
    rows = _step(t, {25: lambda: _tap(t, "single_choice", by_i[25], "s"),
                     26: lambda: _tap(t, "single_choice", by_i[26], "s"),
                     27: lambda: _reply(t, by_i[27], _prompt(t, "number", by_i[27], 0).message_id, "6,5")},
                 {25: "answer_recorded", 26: "answer_recorded", 27: "answer_recorded"})
    late_finals = {("single_choice", 25): _Final("graded", rows[25]["update_id"], raw={"resposta": "b"},
                                                 literal={"resposta": "b"}, chosen=("b",)),
                   ("single_choice", 26): _Final("graded", rows[26]["update_id"], raw={"resposta": "b"},
                                                 literal={"resposta": "b"}, chosen=("b",)),
                   ("number", 27): _Final("graded", rows[27]["update_id"], raw={"resposta": "6.5"},
                                          literal={"resposta": "6,5"}, normalized=("resposta",), echo="6,5")}
    _wait(lambda: all(_delivery(t, qid, by_i[i])["state"] == "answered_late" for qid, i in late_finals),
          "as três atrasadas")
    _settle(t)
    _same(_events(t, cursor, *DISPATCH_EVENT_TYPES),
          [_check_graded(t, qid, by_i[i], final, late=True, at=t_late, response_ms=late_ms, since=since_late)
           for (qid, i), final in late_finals.items()])
    for qid, late_counts in (("single_choice", _counts(answered=20, dont_know=5, no_answer=3, answered_late=2)),
                             ("number", _counts(answered=20, dont_know=5, no_answer=4, answered_late=1))):
        body = get_dispatch(t.client, ADMIN_TOKEN, CODE, shown[qid]["dispatch_id"])
        assert body == {**shown[qid], "deliveries": late_counts}

    # 6. Protocolo, política e toques no roteiro inteiro.
    taps = t.rich.taps()
    answers = t.rich.answers(t.bot)
    assert taps and sorted(a.callback_id for a in answers) == sorted(tp.callback_id for tp in taps), (
        "todo toque confirmado exatamente uma vez")
    _check_pacing(t, r5_mark)
    # A sentinela só sai depois do fechamento (controle positivo: as revelações às respondidas e às não sei, 150,
    # e as 3 atrasadas).
    carrying = [c for c in t.rich.calls() if ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False)]
    early = [f"{c.method}#{c.seq}" for c in carrying if c.started < before_close_mono or c.method not in SEND_METHODS]
    assert early == [], f"o gabarito saiu fora da revelação e das atrasadas: {early}"
    assert len(carrying) >= 6 * (N - len(LATE)) + 3, f"controle contra o vazio: {len(carrying)} chamadas com o gabarito"
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []
    assert t.engine_fake.protocol_errors() == []
    outbox = dict(t.sql.execute("SELECT state, count(*) FROM outbox_messages GROUP BY state").fetchall())
    assert set(outbox) <= {"sent", "cancelled"}, f"mensagens fora de sent/cancelled: {outbox}"
    assert t.sql.execute("SELECT count(*) FROM outbox_messages WHERE delivery_id IS NOT NULL AND text IS NOT NULL"
                         ).fetchone()[0] == 0
    assert t.sql.execute("SELECT count(*) FROM tasks WHERE last_error = %s", (NO_HANDLER_ERROR,)).fetchone()[0] == 0
    states = dict(t.sql.execute("SELECT state, count(*) FROM tasks GROUP BY state").fetchall())
    assert set(states) == {"done"}, f"tarefas fora de done: {states}"
