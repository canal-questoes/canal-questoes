"""Ponta a ponta dos relatórios e do acompanhamento pela Bot API falsa.


O mesmo roteiro do `test_console_e2e_reports.py`, agora pelo canal do Telegram, com a **prévia em Rich** e os
**botões inertes**: oferecimento → vínculo da equipe → lista e vínculo dos alunos → preparo (a equipe recebe
o cabeçalho e a questão da prévia em Rich; os botões da prévia tocados e confirmados com `PREVIEW_TAP`; a
resposta ao cabeçalho recebe a `PREVIEW_REPLY`) → confirmação → abertura (o aviso "aberto", atrás das
mensagens aos alunos) → respostas no prazo, "Não sei" e uma falha de questão na correção (o aviso de falha)
→ fechamento (versão 1 e o resumo) → resposta atrasada → relatório sob demanda com o par diferente da versão
1 → reenvio (versão 2 e o resumo atualizado) → resumo diário com o `FakeClock`.

Montagem:
- **Equipe** pela fixture `team_members` (o vínculo real da equipe pela Bot API falsa, com o worker temporário
  da própria fixture): dois membros com endereço ativo e um sem endereço (não recebe nada); o
  oferecimento `active` e o bot dela são os do roteiro. A `rich_bot_api` (a extensão do disparo e da resposta) é instalada
  **antes** da equipe (a ordem dos argumentos do teste), no mesmo dublê.
- **Lista** pela importação do vínculo do aluno; **convites** em duas fases com os e-mails pelo worker (`verification=` o
  adaptador SMTP real sobre o SMTP falso); **vínculo** pelo Telegram (`/start <código>` e `/aceito`).
- **Worker dos relatórios** pela fixture `report_worker` (`run_worker(…, bots=…)`, o caminho de produção, com o motor
  falso estrito, a obtenção falsa e o `DeliverySettings()` padrão: Rich, 12 botões, 1 mensagem por segundo
  por chat e 30 por segundo por bot), com um canal padrão que acusa qualquer mensagem sem rota.
- **Questão**: a referência `multipart` do curso de teste programada no motor falso com duas partes, uma
  escolha única (`mc`, gabarito `b`) e um texto (`txt`, gabarito "azul"), e o **título** com `<`, `&`, tags,
  URL e comando (`TITLE`); a prévia mostra a primeira parte (a escolha única, com o teclado do aluno) e o
  cabeçalho diz que a questão tem 2 partes.

Roteiro das respostas (os números do relatório esperados saem daqui, calculados à mão):
- A marca `(b)`, toca "Enviar" e responde "azul" ao pedido do texto (nota 1, correta); B toca "Não sei";
  C marca `(b)`, "Enviar" e responde "verde", e a correção recebe do motor falso um `broken` injetado
  (`question_failed/grading_broken`); D não responde no prazo e, depois da versão 1, marca `(a)`, "Enviar" e
  responde ao pedido do texto com `SPECIAL` (`<`, `&`, tags, URL e comando): nota 0, incorreta, atrasada.
- Alcance: alvo 4; entregues 3 (A, B, D); falha de questão 1 (C, estágio `grading`).
- Versão 1: respondidas e corretas (1, 1); "não sei" (1, 1); parciais e incorretas (0, 0); taxas sobre as 3
  entregues: corretas e resposta (1/3, 1/3); nota média (1, 1); tempo até responder com uma observação de
  30 s. Distribuição: a alternativa "Azul" (1, 1); os textos só em "outras" (1, 1).
- Depois da atrasada: respondidas (1, 2); corretas (1, 1); incorretas (0, 1); taxas: corretas (1/3, 1/3),
  incorretas (0, 1/3), resposta (1/3, 2/3); nota média (1, 0,5) (as notas 1 e 0 limitadas a [0, 1]);
  alternativas "Azul" (1, 1) e "Vermelho" (0, 1), empatadas no lado "com atrasadas" e ordenadas pelo hash;
  textos (1, 2) em "outras".

Protocolo e política: zero `protocol_errors()` e `policy_errors()` na `rich_bot_api` e zero `protocol_errors()`
no `engine_fake`; todo toque (dos alunos e da equipe) confirmado exatamente uma vez, os da prévia com o
texto `PREVIEW_TAP`; o dado dos botões da prévia na gramática `cq1:` com o token da prévia (distinto do das
entregas) e no máximo 64 bytes; o ritmo por chat (1 por segundo) e por bot (30 por segundo) nas chamadas de
envio e de edição; toda mensagem à equipe com prioridade 2, e o aviso "aberto" chega à Bot API falsa depois
da última questão aos alunos.

Leituras [I] deste arquivo (escolhas deste teste), além das do `test_console_e2e_reports.py` (linhas fixas dos modelos de
`noticetexts`, o par por extenso, as taxas fracionárias com `pytest.approx(rel=1e-12)`):
- o texto que a equipe vê é o `plain_text` da Bot API falsa (o Rich depois do parsing); o `TITLE` aparece
  literal nele e escapado no markup enviado (`<b>céu</b>` nunca sai como tag);
- a mensagem da questão na prévia é igual, em texto e nos rótulos do teclado, à do aluno A, e só o dado
  dos botões difere (fechamento informado);
- o ritmo é medido pelo `started` da Bot API falsa com a tolerância `PACE_TOLERANCE` (0,2 s) de
  encolhimento, como no `test_telegram_e2e.py`;
- a `PREVIEW_REPLY` sai pelo caminho comum (texto sem dialeto: `sendMessage` sem `parse_mode`).

O `FakeClock` só anda com o sistema parado e em saltos de até 10 s, conferida a posse do bot depois de cada
salto (como no disparo e na resposta).

No esqueleto, o vínculo da equipe passa, e a função falha na
primeira consulta a `team_notices` (tabela ausente: a migração `0005` ainda não existe); depois da migração,
pelos avisos, pela prévia e pelas rotas dos relatórios ainda não produzidos. Só passa com tudo implementado.
"""

from __future__ import annotations

import dataclasses
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.rows import dict_row

from cq_server import noticetexts
from cq_server.buttons import CALLBACK_DATA
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from cq_server.tasks import NO_HANDLER_ERROR
from tests.helpers.api import (
    ADMIN_TOKEN,
    confirm_dispatch,
    get_dispatch,
    get_offering_digest,
    get_report,
    import_students,
    list_events,
    list_report_versions,
    prepare_dispatch,
    prepare_invites,
    resend_report_summary,
    send_invites,
    students_summary,
    update_offering_digest,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.engine import ANSWER_SENTINEL, preset, single_choice_field, text_field
from tests.helpers.team import ACTIVE, NO_ENDPOINT, Team, TeamMember, team_messages

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

ZONE = "America/Sao_Paulo"  # o fuso padrão de `create_offering` (o oferecimento da equipe simulada)
WORKER_ID = "worker-telegram-relatorios"
REPO = "https://repos.cq.test/disciplina/questoes-relatorios-telegram.git"
COMMIT = "8d1f" * 10
COMMIT_SHORT = COMMIT[:12]
QID = "multipart"
TITLE = "Cor & <b>céu</b> 3 < 4 https://exemplo.test/?a=1&b=2 /start"
STATEMENT = "Responda às duas partes."
TEXT_LABEL = "Cor:"
SPECIAL = "<b>azul</b> & 3 < 4 <script>x</script> https://exemplo.test/?a=1&b=2 /start"
REPLY_TEXT = SPECIAL
ORIGIN = "integration"
WINDOW = timedelta(minutes=6)
WAIT = 60.0
CLOCK_STEP = 10
LEASE_MARGIN = 13  # consulta 1 s + Telegram 2 s + renovação 10 s (bot_settings dos testes)
RECHECK = 6  # avanço que vence `CLOSE_RECHECK_SECONDS` (5 s)
RESPONSE_SECONDS = 30
CHAT_INTERVAL = 1.0
BOT_PER_SECOND = 30
PACE_TOLERANCE = 0.2
USER_BASE = 7_600
CALLBACK_DATA_MAX_BYTES = 64
SEND_METHODS = frozenset({"sendMessage", "sendRichMessage", "editMessageText", "editMessageReplyMarkup",
                          "sendPhoto", "sendDocument"})
SUBMIT = "Enviar"
DONT_KNOW = "Não sei"
CHOICE_KEYBOARD = [["(a)", "(b)", "(c)", "(d)"], [SUBMIT], [DONT_KNOW]]
NO_TEACHER_CONTACT_MESSAGE = ("As mensagens livres estão desligadas e o oferecimento não tem contato do professor: "
                              "o aluno que escrever fora das questões não saberá a quem se dirigir.")
WELCOME_HEAD = "Pronto! Você aceitou o termo de uso"
GRADE_BROKEN = {"outcome": "broken",
                "reason": {"code": "question_code_error", "message": "O código da questão falhou ao corrigir.",
                           "phase": "grade"},
                "version_mismatch": {"engine": False, "pl": False}, "warnings": []}
# O preset `multipart` com outras partes, outro título e sem explicação (o `uuid` do preset fica).
QUESTION = preset("multipart", title=TITLE, statement=STATEMENT, fields=(single_choice_field("mc"), text_field("txt")),
                  explanation=None)

REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
               "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}
NO_REASONS = {"endpoint_blocked": 0, "channel_refused": 0, "engine_unavailable": 0, "bot_no_access": 0,
              "channel_unavailable": 0}


# --- montagem ----------------------------------------------------------------------------------------


@dataclasses.dataclass
class _S:
    key: str
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
    team: Team
    offering_row: int
    students: list[_S]
    dispatch: dict | None = None
    closes: datetime | None = None

    @property
    def code(self) -> str:
        return self.team.code

    @property
    def bot(self) -> int:
        return self.team.bot_id

    @property
    def bot_row(self) -> int:
        return self.team.bot_row

    @property
    def dispatch_id(self) -> str:
        assert self.dispatch is not None
        return str(self.dispatch["dispatch_id"])

    @property
    def offering_ref(self) -> dict:
        return {"offering_id": self.team.offering["offering_id"], "code": self.code}


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


def _ev(type_: str, at: datetime | str, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": at if isinstance(at, str) else iso(at), "actor_token_id": actor,
            "data": data}


def _notice_ev(t: _T, kind: str, at: datetime | str, *, dispatch: bool = True, report_version: int | None = None,
               local_date: str | None = None) -> dict:
    """O `notice.recorded` esperado de um aviso enfileirado para os 2 membros com endereço ativo."""
    return _ev("notice.recorded", at, offering_id=t.team.offering["offering_id"], notice_type=kind,
               dispatch_id=t.dispatch_id if dispatch else None, report_version=report_version, episode_seq=None,
               batch_id=None, window_started_at=None, local_date=local_date, state="queued", recipient_count=2)


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


def _pair(on_time: int, with_late: int) -> dict[str, int]:
    return {"on_time": on_time, "with_late": with_late}


def _rate(on_time: float | None, with_late: float | None) -> dict[str, Any]:
    def one(value: float | None) -> Any:
        return None if value is None else pytest.approx(value, rel=1e-12, abs=0.0)
    return {"on_time": one(on_time), "with_late": one(with_late)}


def _pair_text(on_time: int, with_late: int) -> str:
    return f"{on_time} no prazo · {with_late} com atrasadas"


def _fixed_lines(template: str, **values: Any) -> list[str]:
    lines = []
    for line in template.split("\n"):
        try:
            lines.append(line.format(**values))
        except KeyError:
            continue
    return lines


def _contains_lines(text: str, lines: Iterable[str], what: str) -> None:
    for line in lines:
        assert line in text, f"{what}: falta a linha {line!r} em {text!r}"


# --- Bot API falsa: updates, mensagens, botões ---------------------------------------------------------


def _update(t: _T, uid: int) -> dict | None:
    return _one(t.sql, "SELECT * FROM telegram_updates WHERE bot_id = %s AND update_id = %s", (t.bot_row, uid))


def _processed(t: _T, uid: int | None, outcome: str) -> dict:
    assert uid is not None, "o dublê descartou o update"
    row = _wait(lambda: (r := _update(t, uid)) is not None and r["state"] == "processed" and r,
                f"o update {uid} processado")
    assert row["outcome"] == outcome, (row["outcome"], outcome)
    return row


def _delivery(t: _T, s: _S) -> dict:
    row = _one(t.sql, """
        SELECT d.id, d.state, d.failure, d.button_token, d.principal_message_id
          FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id
          JOIN enrollments e ON e.id = d.enrollment_id
         WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s""", (t.dispatch_id, s.person_row))
    assert row is not None, f"sem entrega do aluno {s.key}"
    return row


def _question_id(t: _T, s: _S) -> int:
    delivery = _delivery(t, s)
    assert delivery["principal_message_id"] is not None, f"entrega de {s.key} sem principal"
    ref = t.sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s",
                        (delivery["principal_message_id"],)).fetchone()[0]
    return int(ref)


def _buttons(message: Any) -> list[tuple[str, str, int, str | None, str]]:
    """(token, ação, campo, chave, dado) de cada botão, pela gramática `cq1:`."""
    out = []
    for button in message.buttons():
        data = button.data or ""
        match = CALLBACK_DATA.fullmatch(data)
        assert match is not None, f"botão fora da gramática cq1: {button.label!r}"
        assert 1 <= len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES
        out.append((match.group(1), match.group(2), int(match.group(3), 36), match.group(5), data))
    return out


def _labels(message: Any) -> list[list[str]] | None:
    return None if message.keyboard is None else [[b.label for b in row] for row in message.keyboard]


def _tap(t: _T, s: _S, action: str, key: str | None = None, *, field: int = 0) -> int:
    """Toque no botão (`action`, `key`) do campo `field` do teclado corrente da questão do aluno."""

    def ready() -> Any:
        message = t.rich.current(t.bot, s.chat, _question_id(t, s))
        found = [data for _tok, a, f, k, data in _buttons(message) if (a, f, k) == (action, field, key)]
        return (message, found[0]) if len(found) == 1 else None

    message, data = _wait(ready, f"o botão {action}/{key} do campo {field} do aluno {s.key}")
    return t.rich.tap(t.bot, s.user, message.message_id, data).update_id


def _prompt_id(t: _T, s: _S, index: int) -> int:
    """O último pedido enviado do campo `index` do aluno (o `message_id` na Bot API falsa)."""
    delivery = _delivery(t, s)
    rows = _wait(lambda: t.sql.execute(
        "SELECT channel_ref FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' "
        "AND field_index = %s AND state = 'sent' ORDER BY id", (delivery["id"], index)).fetchall(),
        f"o pedido do campo {index} do aluno {s.key}")
    return int(rows[-1][0])


def _reply(t: _T, user: Any, message_id: int, text: str) -> int:
    uid = t.rich.push_text(t.bot, user, text, reply_to_message_id=message_id)
    assert uid is not None
    return uid


def _submissions(t: _T, s: _S) -> list[dict]:
    return _all(t.sql, "SELECT outcome, valid, score, late, response_ms FROM submissions WHERE delivery_id = %s "
                "ORDER BY id", (_delivery(t, s)["id"],))


def _choose_and_submit(t: _T, s: _S, key: str) -> None:
    """A escolha única da primeira parte: marcar a alternativa e tocar "Enviar"."""
    _processed(t, _tap(t, s, "m", key), "answer_toggled")
    _processed(t, _tap(t, s, "s"), "answer_part_recorded")


def _close(t: _T, timeout: float = 3 * WAIT) -> None:
    def state() -> str:
        return t.sql.execute("SELECT state FROM dispatches WHERE dispatch_uuid = %s::uuid",
                             (t.dispatch_id,)).fetchone()[0]

    deadline = time.monotonic() + timeout
    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if state() == "closed":
                return
            time.sleep(0.05)
        if time.monotonic() > deadline:
            raise AssertionError(f"o disparo não fechou em {timeout} s: {state()}")
        _advance(t, RECHECK)


# --- avisos à equipe ---------------------------------------------------------------------------------


def _bot_message(t: _T, member: TeamMember, channel_ref: str | None) -> Any:
    assert channel_ref is not None and member.chat_id is not None
    return t.rich.current(t.bot, member.chat_id, int(channel_ref))


def _notice_messages(t: _T, kind: str) -> dict[str, Any]:
    """A mensagem de texto do aviso `kind` na Bot API falsa para cada membro com endereço ativo: exatamente
    uma por membro, enviada, com prioridade 2 e `text_format = "rich"`, o mesmo `notice_id` para todos, em
    Rich (`sendRichMessage`), sem o gabarito; o membro sem endereço não recebe nada."""
    by_member: dict[str, Any] = {}
    notice_ids = set()
    for member in t.team.active:
        found = _wait(lambda m=member: (x := team_messages(t.sql, kind, m)) and all(y.state == "sent" for y in x) and x,
                      f"o aviso {kind} de {member.label} enviado")
        assert len(found) == 1, f"{kind}: {len(found)} mensagens a {member.label}"
        message = found[0]
        assert (message.role, message.text_format, message.priority) == (None, "rich", 2), message
        notice_ids.add(message.notice_id)
        sent = _bot_message(t, member, message.channel_ref)
        assert (sent.method, sent.text_format) == ("sendRichMessage", "rich_html"), (sent.method, sent.text_format)
        assert ANSWER_SENTINEL not in (sent.plain_text or "")
        by_member[member.label] = sent
    assert len(notice_ids) == 1, f"{kind}: um aviso só para a equipe inteira"
    for member in t.team.in_state(NO_ENDPOINT):
        assert team_messages(t.sql, kind, member) == []
    return by_member


def _check_title(sent: Any) -> None:
    """O título variável sai escapado no markup e literal para quem lê."""
    assert TITLE in (sent.plain_text or ""), sent.plain_text
    assert "<b>céu</b>" not in (sent.text or ""), "o título com tag foi enviado sem escape"


# --- relatório ---------------------------------------------------------------------------------------


def _choice_hashes(t: _T) -> dict[str, str]:
    """O `content_hash` de cada alternativa da primeira parte (da IR emitida pelo motor falso)."""
    issued = t.engine_fake.issued_variants(QID)
    assert issued, "o motor falso não emitiu a variante"
    return {o["key"]: o["content_hash"] for o in issued[0].ir["fields"][0]["options"]}


def _report(t: _T, *, computed_at: str, frozen: dict | None, late: bool) -> dict:
    """O relatório esperado, com os números da docstring do módulo."""
    assert t.dispatch is not None and t.closes is not None
    hashes = _choice_hashes(t)
    choices = [{"content_hash": hashes["b"], "text": "Azul", "count": _pair(1, 1)}]
    if late:
        choices.append({"content_hash": hashes["a"], "text": "Vermelho", "count": _pair(0, 1)})
        choices.sort(key=lambda item: item["content_hash"])  # empate no lado "com atrasadas": pelo hash
    return {
        "report_schema_version": "1",
        "offering": t.offering_ref,
        "timezone": ZONE,
        "dispatch": {"dispatch_id": t.dispatch_id, "question": {"qid": QID, "commit_short": COMMIT_SHORT},
                     "title": TITLE, "state": "closed", "partial": False, "origin": ORIGIN,
                     "opens_at": t.dispatch["opens_at"], "closes_at": iso(t.closes)},
        "computed_at": computed_at,
        "frozen": frozen,
        "grading_pending": 0,
        "reach": {"target": 4, "delivered": 3, "not_delivered": 0, "not_delivered_by_reason": dict(NO_REASONS),
                  "question_failed": 1, "question_failed_by_stage": {"preparation": 0, "grading": 1}, "pending": 0},
        "performance": {
            "answered": _pair(1, 2) if late else _pair(1, 1), "correct": _pair(1, 1),
            "partially_correct": _pair(0, 0), "incorrect": _pair(0, 1) if late else _pair(0, 0),
            "dont_know": _pair(1, 1),
            "correct_rate": _rate(1 / 3, 1 / 3), "partially_correct_rate": _rate(0.0, 0.0),
            "incorrect_rate": _rate(0.0, 1 / 3 if late else 0.0),
            "response_rate": _rate(1 / 3, 2 / 3 if late else 1 / 3),
            "mean_score": _rate(1.0, 0.5 if late else 1.0)},
        "response_time": {"observations": 1, "negative": 0, "median_ms": RESPONSE_SECONDS * 1000,
                          "q1_ms": RESPONSE_SECONDS * 1000, "q3_ms": RESPONSE_SECONDS * 1000},
        "distribution": {"fields": [
            {"index": 0, "type": "single_choice", "label": None, "unavailable": None, "choices": choices,
             "values": None, "ratios": None, "unrated": None, "texts": None},
            {"index": 1, "type": "text", "label": TEXT_LABEL, "unavailable": None, "choices": None, "values": None,
             "ratios": None, "unrated": None,
             "texts": {"items": [], "others": _pair(1, 2) if late else _pair(1, 1)}}]},
        "friction": {"fields": [{"index": 0, "format_errors": _pair(0, 0), "decimal_comma": _pair(0, 0)},
                                {"index": 1, "format_errors": _pair(0, 0), "decimal_comma": _pair(0, 0)}],
                     "unmapped_format_errors": _pair(0, 0)},
    }


def _summary_lines(t: _T, template: str, *, late: bool, **extra: Any) -> list[str]:
    return _fixed_lines(template, title=TITLE, target=4, delivered=3, not_delivered=0, question_failed=1,
                        answered=_pair_text(1, 2 if late else 1), correct=_pair_text(1, 1),
                        partially_correct=_pair_text(0, 0), dont_know=_pair_text(1, 1), code=t.code,
                        dispatch_id=t.dispatch_id, **extra)


# --- o roteiro -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("team_members", [(ACTIVE, ACTIVE, NO_ENDPOINT)], indirect=True)
def test_telegram_end_to_end_reports_with_rich_preview_and_inert_buttons(
        rich_bot_api: Any, team_members: Team, client: Any, sql: psycopg.Connection, clock: Any, settings: Any,
        smtp: Any, mail_channel: Any, engine_fake: Any, repo_fetcher: Any, report_worker: Any) -> None:
    """o roteiro ponta a ponta pela Bot API falsa, com a prévia em Rich, os botões inertes
    tocados e confirmados com `PREVIEW_TAP`, a resposta ao cabeçalho com a `PREVIEW_REPLY`, o título e as
    respostas com `<`, `&`, tags, URLs e comandos; zero `protocol_errors()` e `policy_errors()` na
    `rich_bot_api` e zero `protocol_errors()` no `engine_fake`; todo toque confirmado; o ritmo por chat e por
    bot respeitado, com os avisos atrás das mensagens aos alunos."""
    rich = rich_bot_api
    team = team_members
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s::uuid",
                               (team.offering["offering_id"],)).fetchone()[0]
    assert sql.execute("SELECT count(*) FROM team_notices").fetchone()[0] == 0, "nenhum aviso antes do disparo"
    students = [_S(key=key, user=make_user(USER_BASE + n), name=f"Pessoa Telegram Seis {n:02d}",
                   ra=f"59700007{n:02d}", email=f"telegram.seis{n:02d}@turma.example.org")
                for n, key in enumerate("ABCD", start=1)]
    body = import_students(client, ADMIN_TOKEN, team.code, [s.row() for s in students])
    assert (body["received"], body["enrollments_created"]) == (4, 4), body
    for s in students:
        s.person_row, s.pseudo = sql.execute(
            "SELECT p.id, p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
            (s.ra,)).fetchone()
    t = _T(client=client, sql=sql, clock=clock, rich=rich, engine_fake=engine_fake, team=team,
           offering_row=offering_row, students=students)
    default_channel = _NoRouteChannel()
    try:
        report_worker(channel=default_channel, verification=mail_channel,
                  settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        _wait(lambda: rich.pending_polls(t.bot), "o leitor do bot consultando")
        start_mark = max((c.seq for c in rich.calls()), default=0)
        _link(t, smtp)
        _flow(t, repo_fetcher, start_mark)
        assert default_channel.calls == []
    finally:
        report_worker.stop()


def _link(t: _T, smtp: Any) -> None:
    """Convites pelo vínculo do aluno (e-mails pelo worker) e o vínculo pelo Telegram: `/start <código>` e `/aceito`."""
    prepared = prepare_invites(t.client, ADMIN_TOKEN, t.code)
    assert prepared["eligible"] == 4
    assert send_invites(t.client, ADMIN_TOKEN, t.code, prepared["confirmation_code"])["queued"] == 4
    _wait(lambda: len(smtp.messages()) == 4, "os quatro e-mails de convite")
    _advance(t, 10)
    for s in t.students:
        links = [link for link in smtp.invite_links() if link.rcpt_to == (s.email,)]
        assert len(links) == 1
        _processed(t, t.rich.push_start(t.bot, s.user, links[0].code), "student_terms_presented")
        _processed(t, t.rich.push_text(t.bot, s.user, "/aceito", bot_command=True), "student_joined")
        _wait(lambda s=s: [m for m in t.rich.messages(t.bot, s.chat) if WELCOME_HEAD in (m.plain_text or "")],
              f"as boas-vindas do aluno {s.key}")
    _settle(t)
    assert get_offering_digest(t.client, t.code) == {"offering": t.offering_ref, "timezone": ZONE, "enabled": False,
                                                     "local_time": "20:00"}


def _flow(t: _T, repo_fetcher: Any, start_mark: int) -> None:
    offering_id = t.team.offering["offering_id"]
    a, b, c, d = t.students
    t.engine_fake.program((REPO, COMMIT, QID), QUESTION)
    repo_fetcher.add(REPO, COMMIT)

    # 1. Preparo e validação; a prévia em Rich à equipe; os botões inertes; a resposta ao cabeçalho.
    t_prep = _advance(t, 10)
    t.closes = t_prep + WINDOW
    cursor = _last_seq(t)
    t.dispatch = prepare_dispatch(t.client, ADMIN_TOKEN, t.code, repo=REPO, commit=COMMIT, qid=QID,
                                  closes_at=iso(t.closes), origin=ORIGIN)
    _wait(lambda: get_dispatch(t.client, ADMIN_TOKEN, t.code, t.dispatch_id)["state"] != "validating", "a validação")
    _settle(t)
    scheduled = get_dispatch(t.client, ADMIN_TOKEN, t.code, t.dispatch_id)
    assert (scheduled["state"], scheduled["target_count"], scheduled["preview"]["title"]) == ("scheduled", 4, TITLE)
    preview = _check_preview(t)
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(t, "dispatch_preview", t_prep)]
    _check_inert_buttons(t, preview["membro-1"])
    _check_preview_reply(t, preview["membro-2"][0])
    _settle(t)

    # 2. Confirmação e abertura: as entregas e o aviso "aberto" atrás delas.
    t_conf = _advance(t, 10)
    cursor = _last_seq(t)
    t.dispatch = confirm_dispatch(t.client, ADMIN_TOKEN, t.code, t.dispatch_id)
    assert (t.dispatch["confirmed"], t.dispatch["opens_at"]) == (True, iso(t_conf))
    _wait(lambda: [r for (r,) in t.sql.execute(
        "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s::uuid",
        (t.dispatch_id,)).fetchall()] == ["delivered"] * 4, "as quatro entregas")
    _settle(t)
    assert get_dispatch(t.client, ADMIN_TOKEN, t.code, t.dispatch_id)["deliveries"] == {**ZERO_COUNTS, "delivered": 4}
    opened = _notice_messages(t, "dispatch_opened")
    questions = [t.rich.current(t.bot, s.chat, _question_id(t, s)) for s in t.students]
    for sent in opened.values():
        _check_title(sent)
        _contains_lines(sent.plain_text or "", _fixed_lines(
            noticetexts.OPENED, title=TITLE, target=4, delivered=4, not_delivered=0, question_failed=0, pending=0,
            code=t.code, dispatch_id=t.dispatch_id), "aviso aberto")
        assert sent.started > max(q.started for q in questions), "o aviso chega depois das questões aos alunos"
    _check_same_render(t, preview["membro-1"][1], questions[0])
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(t, "dispatch_opened", t_conf)]

    # 3. Respostas no prazo: A acerta as duas partes; B toca "Não sei"; a correção de C falha.
    t_ans = _advance(t, RESPONSE_SECONDS)
    cursor = _last_seq(t)
    _choose_and_submit(t, a, "b")
    _processed(t, _reply(t, a.user, _prompt_id(t, a, 1), "azul"), "answer_recorded")
    _wait(lambda: [x["outcome"] for x in _submissions(t, a)] == ["graded"], "a correção de A")
    _processed(t, _tap(t, b, "n"), "answer_dont_know")
    _choose_and_submit(t, c, "b")
    t.engine_fake.inject("grade", body=GRADE_BROKEN, times=1)
    _processed(t, _reply(t, c.user, _prompt_id(t, c, 1), "verde"), "answer_recorded")
    _wait(lambda: _delivery(t, c)["state"] == "question_failed", "a falha de questão de C")
    _settle(t)
    assert _submissions(t, a) == [{"outcome": "graded", "valid": True, "score": 1.0, "late": False,
                                   "response_ms": RESPONSE_SECONDS * 1000}]
    assert _delivery(t, c)["failure"] == "grading_broken"
    for sent in _notice_messages(t, "question_failed").values():
        _check_title(sent)
        _contains_lines(sent.plain_text or "", _fixed_lines(noticetexts.QUESTION_FAILED, title=TITLE, code=t.code,
                                                            dispatch_id=t.dispatch_id), "aviso de falha de questão")
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [_notice_ev(t, "question_failed", t_ans)]

    # 4. Fechamento: a versão 1 e o resumo de fechamento.
    cursor = _last_seq(t)
    _advance(t, (t.closes - t.clock.now()).total_seconds() + 1)
    _close(t)
    _settle(t)
    t_closed = t.clock.now()
    assert get_dispatch(t.client, ADMIN_TOKEN, t.code, t.dispatch_id)["deliveries"] == {
        **ZERO_COUNTS, "answered": 1, "dont_know": 1, "no_answer": 1, "question_failed": 1}
    versions = list_report_versions(t.client, t.code, t.dispatch_id)
    assert [(i["version"], i["trigger"]) for i in versions["items"]] == [(1, "closing")], versions
    v1_ref = versions["items"][0]
    assert versions == {"offering": t.offering_ref, "dispatch_id": t.dispatch_id, "items": [v1_ref], "next_cursor": 1}
    assert t.closes < datetime.fromisoformat(v1_ref["frozen_at"].replace("Z", "+00:00")) <= t_closed
    v1 = get_report(t.client, t.code, t.dispatch_id, version=1)
    assert v1 == _report(t, computed_at=v1_ref["frozen_at"], frozen=v1_ref, late=False)
    for sent in _notice_messages(t, "dispatch_closed").values():
        _check_title(sent)
        _contains_lines(sent.plain_text or "", _summary_lines(t, noticetexts.CLOSED_SUMMARY, late=False),
                        "resumo de fechamento")
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [
        _ev("report.frozen", v1_ref["frozen_at"], offering_id=offering_id, dispatch_id=t.dispatch_id, version=1,
            trigger="closing"),
        _notice_ev(t, "dispatch_closed", v1_ref["frozen_at"], report_version=1)]

    # 5. Atrasada de D, com a resposta de texto com `<`, `&`, tags, URL e comando.
    t_late = _advance(t, 60)
    cursor = _last_seq(t)
    _choose_and_submit(t, d, "a")
    _processed(t, _reply(t, d.user, _prompt_id(t, d, 1), SPECIAL), "answer_recorded")
    _wait(lambda: _delivery(t, d)["state"] == "answered_late", "a atrasada de D")
    _settle(t)
    assert [(x["outcome"], x["valid"], x["score"], x["late"]) for x in _submissions(t, d)] == [
        ("graded", True, 0.0, True)]
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == []

    # 6. Relatório sob demanda (o par muda; a versão 1 não) e o reenvio (versão 2 e o resumo atualizado).
    now_report = get_report(t.client, t.code, t.dispatch_id)
    assert now_report == _report(t, computed_at=iso(t_late), frozen=None, late=True)
    assert now_report["performance"]["answered"] != v1["performance"]["answered"]
    assert get_report(t.client, t.code, t.dispatch_id, version=1) == v1
    t_resend = _advance(t, 10)
    cursor = _last_seq(t)
    v2_ref = {"version": 2, "trigger": "resend", "frozen_at": iso(t_resend)}
    assert resend_report_summary(t.client, t.code, t.dispatch_id) == {
        "offering": t.offering_ref, "dispatch_id": t.dispatch_id, "frozen": v2_ref}
    _settle(t)
    assert list_report_versions(t.client, t.code, t.dispatch_id)["items"] == [v1_ref, v2_ref]
    assert get_report(t.client, t.code, t.dispatch_id, version=2) == _report(
        t, computed_at=iso(t_resend), frozen=v2_ref, late=True)
    for sent in _notice_messages(t, "summary_resent").values():
        _check_title(sent)
        _contains_lines(sent.plain_text or "", _summary_lines(t, noticetexts.RESENT_SUMMARY, late=True, version=2),
                        "resumo reenviado")
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [
        _ev("report.frozen", t_resend, "bootstrap", offering_id=offering_id, dispatch_id=t.dispatch_id, version=2,
            trigger="resend"),
        _notice_ev(t, "summary_resent", t_resend, report_version=2)]

    # 7. Resumo diário, ligado para o minuto local seguinte a dois minutos depois do relógio.
    t_set = _advance(t, 10)
    cursor = _last_seq(t)
    zone = ZoneInfo(ZONE)
    due_local = (t_set + timedelta(minutes=2)).astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    assert due_local.date() == t_set.astimezone(zone).date(), "o roteiro cabe num só dia local"
    local_time = due_local.strftime("%H:%M")
    assert update_offering_digest(t.client, t.code, enabled=True, local_time=local_time) == {
        "offering": t.offering_ref, "timezone": ZONE, "enabled": True, "local_time": local_time}
    adhesion = students_summary(t.client, ADMIN_TOKEN, t.code)
    # O relógio para 5 s antes do horário, com o sistema parado; um salto só (6 s) o leva 1 s além dele, e o
    # resumo sai com o relógio parado nesse instante.
    _advance(t, (due_local - t.clock.now()).total_seconds() - 5)
    _settle(t)
    assert team_messages(t.sql, "daily_digest") == [], "o resumo não sai antes do horário"
    t_due = _advance(t, 6)
    _settle(t)
    digest_lines = _fixed_lines(noticetexts.DAILY_DIGEST, late=1, dispatches=1,
                                **{k: adhesion[k] for k in ("linked", "sending", "terms_presented", "invite_valid",
                                                            "send_failed", "invite_lapsed", "no_invite")})
    digest_lines += _fixed_lines(noticetexts.DIGEST_DISPATCH_LINE, title=TITLE, answered=_pair_text(1, 2),
                                 correct=_pair_text(1, 1), partially_correct=_pair_text(0, 0),
                                 dont_know=_pair_text(1, 1))
    for sent in _notice_messages(t, "daily_digest").values():
        _check_title(sent)
        _contains_lines(sent.plain_text or "", digest_lines, "resumo diário")
    assert _events(t, cursor, *REPORT_EVENT_TYPES) == [
        _ev("offering.digest_changed", t_set, "bootstrap", offering_id=offering_id, enabled=True,
            local_time=local_time),
        _notice_ev(t, "daily_digest", t_due, dispatch=False, local_date=due_local.date().isoformat())]

    # 8. Protocolo, política e toques no roteiro inteiro.
    _final_checks(t, start_mark)


def _check_preview(t: _T) -> dict[str, list[Any]]:
    """A prévia em Rich de cada membro com endereço ativo: o cabeçalho
    (texto `rich`) e a questão (papel `question`, com o teclado do aluno e o token da prévia), sem pedido de
    campo (a primeira parte é de escolha), enviados com prioridade 2; devolve as duas mensagens da Bot API
    falsa por membro."""
    token = t.sql.execute("SELECT p.button_token FROM dispatch_previews p JOIN dispatches x ON x.id = p.dispatch_id "
                          "WHERE x.dispatch_uuid = %s::uuid", (t.dispatch_id,)).fetchone()[0]
    by_member: dict[str, list[Any]] = {}
    notice_ids = set()
    for member in t.team.active:
        found = _wait(lambda m=member: (x := team_messages(t.sql, "dispatch_preview", m)) and len(x) >= 2
                      and all(y.state == "sent" for y in x) and x, f"a prévia de {member.label}")
        assert [(x.role, x.text_format, x.priority) for x in found] == [(None, "rich", 2), ("question", None, 2)], found
        notice_ids |= {x.notice_id for x in found}
        header, question = (_bot_message(t, member, x.channel_ref) for x in found)
        assert (header.method, header.text_format) == ("sendRichMessage", "rich_html")
        _check_title(header)
        _contains_lines(header.plain_text or "", [
            "Alunos-alvo: 4", noticetexts.PREVIEW_PARTS.format(parts=2),
            "As mensagens a seguir mostram a questão como os alunos vão recebê-la; os botões não registram resposta.",
            f"Para confirmar o disparo: cq dispatches confirm {t.code} {t.dispatch_id}", NO_TEACHER_CONTACT_MESSAGE],
            "cabeçalho da prévia")
        assert f"Prévia do disparo: {TITLE}" in (header.plain_text or "")
        assert header.keyboard is None, "o cabeçalho não tem botão de confirmação"
        assert STATEMENT in (question.plain_text or "")
        assert _labels(question) == CHOICE_KEYBOARD
        tokens = {tok for tok, *_rest in _buttons(question)}
        assert tokens == {token}, "os botões da prévia levam o token da prévia"
        for message in (header, question):
            assert ANSWER_SENTINEL not in (message.plain_text or "") and ANSWER_SENTINEL not in (message.text or "")
        by_member[member.label] = [header, question]
    assert len(notice_ids) == 1
    for member in t.team.in_state(NO_ENDPOINT):
        assert team_messages(t.sql, "dispatch_preview", member) == []
    assert t.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0, "a prévia não cria entrega"
    return by_member


def _check_inert_buttons(t: _T, preview: list[Any]) -> None:
    """Os toques do membro 1 numa alternativa, no "Enviar" e no "Não sei" da prévia: cada
    um → `preview_tapped`, confirmado uma vez com `PREVIEW_TAP`, sem edição da mensagem e sem registro
    além do update."""
    member = t.team.member("membro-1")
    assert member.user is not None and member.chat_id is not None
    _header, question = preview
    before = (t.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0],
              t.sql.execute("SELECT count(*) FROM submissions").fetchone()[0])
    wanted = [("m", "a"), ("s", None), ("n", None)]
    for action, key in wanted:
        current = t.rich.current(t.bot, member.chat_id, question.message_id)
        data = [d for _tok, a, _f, k, d in _buttons(current) if (a, k) == (action, key)]
        assert len(data) == 1, (action, key)
        tap = t.rich.tap(t.bot, member.user, question.message_id, data[0])
        _processed(t, tap.update_id, "preview_tapped")
        answers = _wait(lambda cb=tap.callback_id: [x for x in t.rich.answers(t.bot) if x.callback_id == cb],
                        "a confirmação do toque na prévia")
        assert [x.text for x in answers] == [noticetexts.PREVIEW_TAP]
    assert len(noticetexts.PREVIEW_TAP) <= 200
    _settle(t)
    assert t.rich.edits(t.bot, member.chat_id, question.message_id) == [], "o toque não edita a prévia"
    assert t.rich.current(t.bot, member.chat_id, question.message_id).keyboard == question.keyboard
    after = (t.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0],
             t.sql.execute("SELECT count(*) FROM submissions").fetchone()[0])
    assert after == before, "o toque na prévia não cria mensagem nem submissão"


def _check_preview_reply(t: _T, header: Any) -> None:
    """A resposta ("responder") do membro 2 ao cabeçalho, com `<`, `&`, tags, URL e comando →
    `preview_replied` e a `PREVIEW_REPLY` ao chat dele."""
    member = t.team.member("membro-2")
    assert member.user is not None and member.chat_id is not None
    update = _processed(t, _reply(t, member.user, header.message_id, REPLY_TEXT), "preview_replied")
    rows = _wait(lambda: (x := _all(t.sql, "SELECT channel_ref, text, state, priority, notice_id FROM outbox_messages "
                                   "WHERE reply_to_update_id = %s", (update["id"],))) and all(
        r["state"] == "sent" for r in x) and x, "a resposta fixa da prévia")
    assert [{k: r[k] for k in ("text", "state", "priority", "notice_id")} for r in rows] == [
        {"text": noticetexts.PREVIEW_REPLY, "state": "sent", "priority": 2, "notice_id": None}]
    sent = _bot_message(t, member, rows[0]["channel_ref"])
    assert (sent.method, sent.plain_text) == ("sendMessage", noticetexts.PREVIEW_REPLY)
    assert t.sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == 0


def _check_same_render(t: _T, preview_question: Any, student_question: Any) -> None:
    """a questão da prévia é igual, em texto e nos rótulos do teclado, à do aluno A; só o dado dos
    botões difere (o token da prévia × o da entrega)."""
    assert preview_question.plain_text == student_question.plain_text
    assert _labels(preview_question) == _labels(student_question)
    preview_tokens = {tok for tok, *_rest in _buttons(preview_question)}
    student_tokens = {tok for tok, *_rest in _buttons(student_question)}
    assert preview_tokens.isdisjoint(student_tokens)
    all_delivery_tokens = {r for (r,) in t.sql.execute("SELECT button_token FROM deliveries").fetchall()}
    assert preview_tokens.isdisjoint(all_delivery_tokens)


def _check_pacing(t: _T, after_seq: int) -> None:
    """nas chamadas de envio e de edição, no máximo uma por segundo por chat e 30 por segundo
    por bot, medidas na Bot API falsa (a confirmação do toque fica fora)."""
    calls = sorted((c for c in t.rich.calls(bot_id=t.bot) if c.seq > after_seq and c.method in SEND_METHODS),
                   key=lambda c: c.started)
    assert calls, "controle contra o vazio"
    by_chat: dict[int, list[float]] = defaultdict(list)
    for call in calls:
        by_chat[int(call.params["chat_id"])].append(call.started)
    # Controle contra o vazio do ritmo por chat: a marca `after_seq` é tomada antes do vínculo, e o chat de
    # cada membro com endereço ativo recebe depois dela ao menos o cabeçalho e a questão da prévia (duas
    # chamadas de envio distintas, conferidas em `_check_preview`); logo há intervalos a conferir.
    assert any(len(starts) >= 2 for starts in by_chat.values()), "nenhum chat com duas chamadas: ritmo sem conferência"
    for member in t.team.active:
        assert member.chat_id is not None
        assert len(by_chat[member.chat_id]) >= 2, (
            f"o chat de {member.label} tem {len(by_chat[member.chat_id])} chamada(s): ritmo sem conferência")
    for chat, starts in by_chat.items():
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
        assert not gaps or min(gaps) >= CHAT_INTERVAL - PACE_TOLERANCE, (
            f"ritmo por chat violado no chat {chat}: intervalo de {min(gaps):.3f} s")
    starts = [c.started for c in calls]
    windows = [starts[k + BOT_PER_SECOND] - starts[k] for k in range(len(starts) - BOT_PER_SECOND)]
    assert not windows or min(windows) >= 1.0 - PACE_TOLERANCE, (
        f"ritmo por bot violado: {BOT_PER_SECOND + 1} envios em {min(windows):.3f} s")


def _final_checks(t: _T, start_mark: int) -> None:
    taps = t.rich.taps()
    answers = t.rich.answers(t.bot)
    assert taps and sorted(x.callback_id for x in answers) == sorted(x.callback_id for x in taps), (
        "todo toque confirmado exatamente uma vez")
    _check_pacing(t, start_mark)
    kinds = ["dispatch_preview", "dispatch_preview", "dispatch_opened", "question_failed", "dispatch_closed",
             "summary_resent", "daily_digest"]
    for member in t.team.active:
        messages = team_messages(t.sql, recipient=member)
        assert [m.kind for m in messages] == kinds, (member.label, [m.kind for m in messages])
        assert {(m.state, m.priority) for m in messages} == {("sent", 2)}
    for member in t.team.in_state(NO_ENDPOINT):
        assert team_messages(t.sql, recipient=member) == []
    team_chats = {m.chat_id for m in t.team.active}
    for message in t.rich.messages(t.bot):
        if message.chat_id in team_chats:
            assert ANSWER_SENTINEL not in (message.plain_text or ""), "o gabarito chegou à equipe"
    assert t.rich.protocol_errors() == [] and t.rich.policy_errors() == []
    assert t.engine_fake.protocol_errors() == []
    outbox = dict(t.sql.execute("SELECT state, count(*) FROM outbox_messages GROUP BY state").fetchall())
    assert set(outbox) <= {"sent", "cancelled"}, f"mensagens fora de sent/cancelled: {outbox}"
    assert t.sql.execute("SELECT count(*) FROM tasks WHERE last_error = %s", (NO_HANDLER_ERROR,)).fetchone()[0] == 0
    states = dict(t.sql.execute("SELECT state, count(*) FROM tasks GROUP BY state").fetchall())
    assert set(states) == {"done"}, f"tarefas fora de done: {states}"
