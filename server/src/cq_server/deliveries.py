"""Entregas: variante, conteúdo, renderização do estado, desfecho do envio e revelação.
Prepara cada entrega com a variante, guarda o conteúdo renderizado, registra o desfecho do envio pela
fila de saída e edita a mensagem com o gabarito permitido depois do fechamento.

- **Tratadores:** `delivery.prepare` (uma `create_variant` fora de transação; depois, numa transação
  só, o conteúdo, as mídias, `variant_at` e as mensagens na fila) e `delivery.reveal` (a rede
  de segurança da revelação devida). O `channel` é o canal com rota do worker, lido só
  ao processar uma tarefa.
- **Renderização:** `render_state` (a renderização completa do estado corrente, com versão) e
  `load_ir` (a IR inteira, com os bytes das mídias lidos de `media_blobs`, sem o motor).
- **Colaboradores da fila** (`outbox.Outbox(content=…, outcome=…)`): `DeliveryContent.load` lê o
  conteúdo de uma mensagem de entrega na posse (depois do commit da escolha, sem trava) e devolve a
  `OutgoingMessage` ou `Skip(reason)`; `DeliveryOutcome.record` grava o desfecho do envio na transação
  do resultado (e no esgotamento da posse, com `Exhausted`), travando o endereço e a entrega depois da
  linha da fila. As implementações são `DeliveryContentLoader(channel, *, delivery=None)` e
  `DeliveryOutcomeRecorder(channel, *, clock)`.
- `open_fields`: os campos abertos da pessoa no bot (a ligação do texto ao campo).

**Renderização**. Na abertura, `build_rendered` grava em
`delivery_contents.rendered` os derivados da IR no dialeto do canal da abertura (`format`, que vale para
toda mensagem e edição da entrega): o enunciado (as mídias como `[Figura N] alt` e os campos como
`[campo k: rótulo sufixo]`), o bloco de cada campo (as alternativas com as chaves, na ordem da variante,
as restrições de seleção e a dica da múltipla degradada), o gabarito e a explicação. `render_state`
compõe o estado corrente a partir dele (sem o motor): o enunciado, os avisos de figura indisponível, os campos,
o desfecho (feedback com ✅, ◐ ou ❌ e a nota, com o eco e as marcas por alternativa da múltipla, "Não sei"
ou falha de questão; o erro de formato sai numa mensagem própria da correção), o progresso nas várias partes,
a janela fechada com o gabarito permitido (revelada) e o aviso da atrasada, o prazo e, sem botões, o `/naosei`;
o teclado sai do campo corrente e da `controls_version`.

`render_state` **grava** o resultado: `deliveries.render_version += 1` e `delivery_contents.current_render`
(texto, dialeto, teclado, versão). O chamador já travou a entrega (`FOR NO KEY UPDATE`) e passa o formatador do
dialeto gravado (`channel.formatter_for(rendered["format"])`) e as capacidades do canal; depois,
enfileira a edição com `version = rendered.render_version`. `RenderedMessage.fits = False` quer dizer que
a renderização completa não coube e que a gravada saiu sem o feedback com o eco (quem enfileira
manda também o `notice` curto). `enqueue_state_update` faz esse passo (a edição, ou o `fallback` num canal
sem edição, e o `notice` quando não coube). Por defesa, a posse de uma `question`/`fallback`/edição cuja
versão pedida é mais nova que a `current_render` gravada renderiza o estado corrente em memória.

Não importa `answers`, `grading`, `dispatches` nem `telegramrender`; fala com o canal só pela porta.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import posixpath
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from cq_server import deliverytexts
from cq_server._leasing import LazyConnection
from cq_server.buttons import encode_callback
from cq_server.clock import Clock
from cq_server.config import DeliverySettings, Settings
from cq_server.events import EventType, append_event
from cq_server.outbox import cancel_delivery_messages, enqueue_delivery, enqueue_delivery_edit
from cq_server.ports.delivery import (
    ChannelCapabilities,
    DeliveryChannel,
    Formatted,
    InlineButton,
    MediaPayload,
    MessageFormatter,
    OutgoingMessage,
    ReplyRequest,
    Sent,
    Undeliverable,
)
from cq_server.ports.engine import (
    Broken,
    EngineNotFound,
    EngineRequestRejected,
    EngineUnavailable,
    QuestionEngine,
    QuestionRef,
    Unsupported,
    Variant,
)
from cq_server.tasks import ClaimedTask, PermanentTaskError, enqueue_in_transaction, finished_rounds, has_live_task

logger = logging.getLogger("cq_server.worker")

# Tipos de tarefa (payload `{"delivery_row_id": n}`).
PREPARE_TASK_TYPE = "delivery.prepare"
REVEAL_TASK_TYPE = "delivery.reveal"


class DeliveryState(StrEnum):
    """Estado da entrega."""

    PENDING = "pending"
    DELIVERED = "delivered"
    NOT_DELIVERED = "not_delivered"
    ANSWERED = "answered"
    DONT_KNOW = "dont_know"
    NO_ANSWER = "no_answer"
    ANSWERED_LATE = "answered_late"
    QUESTION_FAILED = "question_failed"


class UndeliveredReason(StrEnum):
    """Motivo de `not_delivered`."""

    ENDPOINT_BLOCKED = "endpoint_blocked"
    CHANNEL_REFUSED = "channel_refused"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    BOT_NO_ACCESS = "bot_no_access"
    CHANNEL_UNAVAILABLE = "channel_unavailable"


class QuestionFailure(StrEnum):
    """Motivo de `question_failed`."""

    VARIANT_BROKEN = "variant_broken"
    VARIANT_UNSUPPORTED = "variant_unsupported"
    CHANNEL_LIMITS_EXCEEDED = "channel_limits_exceeded"
    GRADING_BROKEN = "grading_broken"
    ENGINE_REJECTED = "engine_rejected"


# As transições permitidas (de → para); a saída `no_answer` →
# `dont_know` é o "Não sei" atrasado.
DELIVERY_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"delivered", "not_delivered", "question_failed"}),
    "delivered": frozenset({"answered", "dont_know", "no_answer", "question_failed"}),
    "no_answer": frozenset({"answered_late", "dont_know", "question_failed"}),
}
# Estados terminais (anulam `current_field`) e os que ainda andam (`deliveries_live_idx`).
TERMINAL_STATES = frozenset({"not_delivered", "answered", "dont_know", "answered_late", "question_failed"})
LIVE_STATES = frozenset({"pending", "delivered", "no_answer"})
# Papéis das mensagens de entrega.
ROLES = ("question", "media", "field_prompt", "fallback", "notice")

# Tipos de campo, fotos e teclado.
TYPEABLE_TYPES = frozenset({"number", "integer", "text"})
CHOICE_TYPES = frozenset({"single_choice", "multiple_choice"})
# Controles de uma escolha com botões além das alternativas: "Enviar" e "Não sei", nas duas escolhas
# (a única e a múltipla).
CHOICE_CONTROLS = {"single_choice": 2, "multiple_choice": 2}
PHOTO_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
BUTTONS_PER_ROW = 4
# Legenda e eco.
CAPTION_MAX_UNITS = 1024
ECHO_MAX_CHARS = 200
# O tipo da tarefa de nova obtenção (o mesmo `grading.REFETCH_TASK_TYPE`).
_REFETCH_TASK_TYPE = "question.refetch"
_EDIT_OPERATIONS = frozenset({"edit_text", "edit_markup"})
_BLOCK_HTML = re.compile(r"<(?:p|ul|ol|li|table|pre|blockquote|h[1-6])\b|<cq-math[^>]*display=\"block\"")
# Mensagens fixas das falhas (sem texto de terceiros).
_NO_CHANNEL = "canal sem rota"


# --------------------------------------------------------------------------------------------------
# Tipos de contrato
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedMessage:
    """A renderização completa do estado corrente de uma entrega (a `current_render`), com a versão."""

    text: str
    text_format: str
    keyboard: tuple[tuple[InlineButton, ...], ...] | None
    reply_request: ReplyRequest | None
    render_version: int
    fits: bool


@dataclass(frozen=True)
class OpenField:
    """Um campo corrente aberto de uma entrega `delivered` da pessoa no bot."""

    delivery_row_id: int
    delivery_id: uuid.UUID
    field_index: int
    field_type: str
    typeable: bool


@dataclass(frozen=True)
class DeliveryRow:
    """Uma linha de mensagem de entrega da fila, como a posse a vê."""

    message_id: int
    delivery_row_id: int
    role: str
    operation: str
    render_version: int | None
    media_id: str | None
    field_index: int | None
    bot_id: int
    endpoint_row_id: int
    recipient: uuid.UUID
    attempts: int


@dataclass(frozen=True)
class Skip:
    """A posse não envia a mensagem: ela vai a `cancelled` (ex.: `no_edit_target`)."""

    reason: str


@dataclass(frozen=True)
class Exhausted:
    """As tentativas da fila (ou a posse) se esgotaram sem entrega."""

    reason: str


class DeliveryContent(Protocol):
    """Lê, na posse, o conteúdo de uma mensagem de entrega (sem trava, fora de transação de escrita)."""

    def load(self, conn: psycopg.Connection, row: DeliveryRow) -> OutgoingMessage | Skip: ...


class DeliveryOutcome(Protocol):
    """Grava o desfecho do envio de uma mensagem de entrega, na transação do resultado."""

    def record(self, conn: psycopg.Connection, row: DeliveryRow,
               result: Sent | Undeliverable | Exhausted) -> None: ...


# --------------------------------------------------------------------------------------------------
# Derivados da abertura (`delivery_contents.rendered`)
# --------------------------------------------------------------------------------------------------


def _text_html(text: str) -> str:
    """Um texto literal como HTML canônico (o formatador o escreve escapado no dialeto)."""
    return html.escape(text, quote=False)


def _figure_number(media_id: str, index: int) -> int:
    digits = media_id[1:] if isinstance(media_id, str) else ""
    return int(digits) if digits.isdigit() else index


def _truncate(text: str) -> str:
    """O eco: os primeiros 200 pontos de código seguidos de "…"."""
    return text if len(text) <= ECHO_MAX_CHARS else text[:ECHO_MAX_CHARS] + "…"


def _select_rule(constraints: Mapping[str, Any], option_count: int) -> str | None:
    """As restrições de seleção da múltipla, em português."""
    low = constraints.get("min_select")
    high = constraints.get("max_select")
    if not isinstance(low, int) or not isinstance(high, int):
        return None
    high = min(high, option_count) if option_count else high
    if low == high:
        return f"Marque exatamente {low} alternativa{'s' if low != 1 else ''}."
    if low <= 0:
        return f"Marque até {high} alternativa{'s' if high != 1 else ''}."
    return f"Marque de {low} a {high} alternativas."


def choice_has_buttons(kind: str, option_count: int, capabilities: ChannelCapabilities) -> bool:
    """O campo de escolha cabe em botões: `max_buttons >= 1` e alternativas + controles (`CHOICE_CONTROLS`,
    "Enviar" e "Não sei" nas duas escolhas) `<= max_buttons`.
    A regra única da conta, usada na abertura (`build_rendered`), na recepção (`answers.field_typeable`)
    e na prévia (`dispatches._render_preview`). Um tipo que não é escolha não tem botões. Puro."""
    controls = CHOICE_CONTROLS.get(kind)
    if controls is None or capabilities.max_buttons < 1:
        return False
    return option_count + controls <= capabilities.max_buttons


def build_rendered(ir: Mapping[str, Any], *, formatter: MessageFormatter,
                   capabilities: ChannelCapabilities) -> dict[str, Any]:
    """Os derivados da IR no dialeto de `formatter`, decididos
    pelas capacidades do canal: o enunciado, os blocos dos campos, o gabarito e a explicação. Puro."""
    fields: list[Mapping[str, Any]] = list(ir.get("fields") or [])
    statement_html = str((ir.get("statement") or {}).get("html") or "")
    labels: dict[str, str] = {}
    media: list[dict[str, Any]] = []
    for index, item in enumerate(ir.get("media") or [], start=1):
        number = _figure_number(str(item.get("id")), index)
        labels[str(item.get("id"))] = f"Figura {number}"
        media.append({"id": str(item.get("id")), "n": number, "status": item.get("status"),
                      "alt": item.get("alt")})
    shown: list[dict[str, Any]] = []
    total = len(fields)
    for k, spec in enumerate(fields, start=1):
        label = formatter.plain(spec["label_html"]).strip() if spec.get("label_html") else ""
        suffix = formatter.plain(spec["suffix_html"]).strip() if spec.get("suffix_html") else ""
        marker = " ".join(part for part in (f"campo {k}:" if (label or suffix) else f"campo {k}", label, suffix)
                          if part)
        labels.setdefault(str(spec["name"]), marker)
        shown.append({"label": label or None, "suffix": suffix or None, "marker": marker})
    statement = formatter.from_ir(statement_html, media_labels=labels).markup
    has_buttons = capabilities.max_buttons >= 1
    out_fields: list[dict[str, Any]] = []
    for k, spec in enumerate(fields):
        kind = str(spec.get("type"))
        options = list(spec.get("options") or []) if kind in CHOICE_TYPES else []
        buttons = kind in CHOICE_TYPES and choice_has_buttons(kind, len(options), capabilities)
        typeable = kind in TYPEABLE_TYPES or (kind in CHOICE_TYPES and not buttons)
        parts: list[str] = []
        if re.search(r'<cq-field\b[^>]*\bname="' + re.escape(str(spec["name"])) + '"', statement_html) is None:
            parts.append(f"<p><strong>[{_text_html(shown[k]['marker'])}]</strong></p>")
        if options:
            items = "".join(f"<li>({_text_html(str(o['key']))}) {o.get('html') or ''}</li>" for o in options)
            parts.append(f"<ul>{items}</ul>")
        if kind == "multiple_choice":
            rule = _select_rule(spec.get("constraints") or {}, len(options))
            if rule:
                parts.append(f"<p>{_text_html(rule)}</p>")
            if not buttons:
                parts.append(f"<p>{_text_html(deliverytexts.TYPED_MULTIPLE_HOWTO)}</p>")
        block = formatter.from_ir("".join(parts), media_labels=labels).markup if parts else ""
        out_fields.append({
            "name": str(spec["name"]), "type": kind, "label": shown[k]["label"], "suffix": shown[k]["suffix"],
            "hint": spec.get("hint"), "buttons": buttons, "typeable": typeable, "block": block,
            "options": [{"key": str(o["key"]), "text": formatter.plain(str(o.get("html") or "")).strip()}
                        for o in options],
        })
    answer_key: list[dict[str, Any]] = []
    for entry in ir.get("answer_key") or []:
        display = entry.get("display_html")
        answer_key.append({"can_show": bool(entry.get("can_show")),
                           "markup": None if not display else _labelled(formatter, labels, "Gabarito:", display),
                           "name": entry.get("name")})
    explanation = ir.get("explanation")
    explained = None
    if isinstance(explanation, Mapping) and explanation.get("html"):
        explained = {"can_show": bool(explanation.get("can_show")),
                     "markup": _labelled(formatter, labels, "Explicação:", str(explanation["html"]))}
    return {"format": formatter.text_format, "statement": statement, "media": media, "fields": out_fields,
            "count": total, "answer_key": answer_key, "explanation": explained, "buttons": has_buttons,
            "can_edit": bool(capabilities.can_edit)}


def _labelled(formatter: MessageFormatter, labels: Mapping[str, str], label: str, body_html: str) -> str:
    """"Rótulo: <conteúdo da IR>" numa linha, ou o rótulo e o conteúdo em blocos, se ele tem blocos."""
    if _BLOCK_HTML.search(body_html) is None:
        return formatter.from_ir(f"<p>{_text_html(label)} {body_html}</p>", media_labels=labels).markup
    return formatter.from_ir(f"<p>{_text_html(label)}</p>{body_html}", media_labels=labels).markup


# --------------------------------------------------------------------------------------------------
# Renderização do estado
# --------------------------------------------------------------------------------------------------


@dataclass
class _View:
    """O estado de uma entrega que a renderização lê (sem o motor)."""

    state: str
    current_field: int | None
    controls_version: int
    render_version: int
    button_token: str
    revealed: bool
    dispatch_state: str
    closes_at: datetime | None
    timezone: str
    single_variant: bool
    rendered: Mapping[str, Any]
    selection: list[str] = field(default_factory=list)
    final: Mapping[str, Any] | None = None
    failed_media: set[str] = field(default_factory=set)
    # O oferecimento arquivado: a `no_answer` sai só com
    # "Janela fechada", sem controles nem avisos de atrasada (nada mais será processado).
    archived: bool = False


def _raw(formatter: MessageFormatter, markup: str) -> Formatted:
    """Um trecho já escrito no dialeto (a medida é refeita pelo `compose`)."""
    return Formatted(markup=markup, text_format=formatter.text_format, length=0, fits=True)


def _escape_lines(formatter: MessageFormatter, lines: Sequence[str]) -> Formatted:
    """Linhas de texto fixo numa mesma quebra (o texto é juntado antes do escape)."""
    return formatter.escape("\n".join(lines))


def correct_keys(value: Any) -> frozenset[str] | None:
    """As chaves corretas de uma múltipla, de `submissions.correct_answers[name]`:
    uma lista cujos itens são chaves (`str`) ou objetos com `key` (o `pl-checkbox` guarda `{key, html,
    feedback}`); outro formato, ou campo ausente, → `None` (sem marcas). Puro."""
    if not isinstance(value, list):
        return None
    keys: set[str] = set()
    for item in value:
        if isinstance(item, str):
            keys.add(item)
        elif isinstance(item, Mapping) and isinstance(item.get("key"), str):
            keys.add(item["key"])
        else:
            return None
    return frozenset(keys)


def _echo(formatter: MessageFormatter, spec: Mapping[str, Any], value: Any,
          correct: frozenset[str] | None = None) -> str:
    """O eco de uma parte, escapado (o `FEEDBACK`): numa escolha, a chave e o texto de
    cada alternativa escolhida, na ordem das opções; senão, o literal truncado em 200. Na múltipla com as
    chaves corretas conhecidas (`correct`), cada escolhida termina em " ✅" ou " ❌", depois do texto
    (com qualquer `partial_credit`)."""
    if spec.get("type") in CHOICE_TYPES and spec.get("options"):
        if isinstance(value, list):
            chosen = {str(v) for v in value}
        elif isinstance(value, str):
            chosen = {value} if spec.get("type") == "single_choice" else set(value)
        else:
            chosen = set()
        picked = [o for o in spec["options"] if o["key"] in chosen]
        if picked:
            keys = correct if spec.get("type") == "multiple_choice" else None

            def piece(option: Mapping[str, Any]) -> str:
                text = f"({option['key']}) {_truncate(option['text'])}"
                if keys is not None:
                    text += " " + deliverytexts.option_mark(option["key"] in keys)
                return formatter.escape(text).markup

            return ", ".join(piece(o) for o in picked)
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, list):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    return formatter.escape(_truncate(text)).markup


def _part_score(partial: Any, name: str) -> float:
    entry = partial.get(name) if isinstance(partial, Mapping) else None
    score = entry.get("score") if isinstance(entry, Mapping) else None
    return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0


def _score(value: Any) -> float:
    """A nota que o motor devolveu (sem nota numérica, 0); nenhuma reimplementação da nota do PL."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _field_correct(correct_answers: Any, spec: Mapping[str, Any]) -> frozenset[str] | None:
    """As chaves corretas de um campo de múltipla (`correct_keys`); `None` nos outros tipos."""
    if spec.get("type") != "multiple_choice" or not isinstance(correct_answers, Mapping):
        return None
    return correct_keys(correct_answers.get(spec["name"]))


def _feedback(formatter: MessageFormatter, view: _View) -> Formatted | None:
    """O feedback da última submissão válida (o `FEEDBACK`): os ecos com as marcas
    por alternativa, o veredito e a nota e a marca de cada parte nas três faixas."""
    final = view.final
    if final is None:
        return None
    fields = list(view.rendered.get("fields") or [])
    raw = final.get("raw_input") if isinstance(final.get("raw_input"), Mapping) else {}
    correct = final.get("correct_answers")
    echoes = [_echo(formatter, spec, raw.get(spec["name"]), _field_correct(correct, spec)) for spec in fields]
    score = _score(final.get("score"))
    if len(echoes) <= 1:
        text = deliverytexts.feedback(echoes or [""], score=score)
    else:
        parts = [_part_score(final.get("partial_scores"), spec["name"]) for spec in fields]
        text = deliverytexts.feedback(echoes, score=score, part_scores=parts)
    return _raw(formatter, text)


def _allowed(entry: Mapping[str, Any] | None, view: _View) -> bool:
    """Gabarito permitido: `can_show and (not single_variant or score >= 1)`; sem
    submissão válida, a condição de variante única é falsa."""
    if not entry or not entry.get("can_show") or not entry.get("markup"):
        return False
    if not view.single_variant:
        return True
    score = view.final.get("score") if view.final is not None else None
    return isinstance(score, (int, float)) and not isinstance(score, bool) and score >= 1


def _keyboard(view: _View) -> tuple[tuple[InlineButton, ...], ...] | None:
    """Os controles do campo corrente: na única, `(a)` (desmarcada) ou `✅ (a)`
    (marcada, no máximo uma), todos com a ação `m`, e "Enviar" (`s`) em linha própria;
    na múltipla, `⬜ (a)` (`m`)/`✅ (a)` (`u`) e "Enviar"; a marcação vem de `view.selection`, também na
    atrasada; "Não sei" em toda entrega aberta e na `no_answer`; até 4 chaves por linha."""
    cf = view.current_field
    fields = list(view.rendered.get("fields") or [])
    if cf is None or not 0 <= cf < len(fields) or not view.rendered.get("buttons"):
        return None
    spec = fields[cf]
    token, version = view.button_token, view.controls_version
    rows: list[tuple[InlineButton, ...]] = []
    if spec.get("buttons"):
        keys = [o["key"] for o in spec.get("options") or []]
        marked = set(view.selection)
        if spec.get("type") == "single_choice":
            # Todo botão de alternativa marca (`m`, absoluto); a marcada leva ✅.
            buttons = [InlineButton(f"✅ ({key})" if key in marked else f"({key})",
                                    encode_callback(token, "m", cf, version, key)) for key in keys]
        else:
            buttons = [InlineButton(f"✅ ({key})", encode_callback(token, "u", cf, version, key)) if key in marked
                       else InlineButton(f"⬜ ({key})", encode_callback(token, "m", cf, version, key))
                       for key in keys]
        rows.extend(tuple(buttons[i:i + BUTTONS_PER_ROW]) for i in range(0, len(buttons), BUTTONS_PER_ROW))
        rows.append((InlineButton(deliverytexts.SUBMIT_BUTTON, encode_callback(token, "s", cf, version)),))
    rows.append((InlineButton(deliverytexts.DONT_KNOW_BUTTON, encode_callback(token, "n", cf, version)),))
    return tuple(rows)


def _render(view: _View, formatter: MessageFormatter) -> tuple[Formatted, tuple | None, bool]:
    """(texto composto, teclado, `fits` da renderização completa). Sem caber, o feedback com o eco sai
    da mensagem e o `fits` fica falso."""
    blocks, keyboard, feedback_at = _blocks(view, formatter)
    composed = formatter.compose(blocks)
    if composed.fits or feedback_at is None:
        return composed, keyboard, composed.fits
    shorter = formatter.compose([b for i, b in enumerate(blocks) if i != feedback_at])
    return shorter, keyboard, False


def _blocks(view: _View, formatter: MessageFormatter) -> tuple[list[Formatted], tuple | None, int | None]:
    rendered = view.rendered
    fields = list(rendered.get("fields") or [])
    blocks: list[Formatted] = [_raw(formatter, str(rendered.get("statement") or ""))]
    unavailable = [deliverytexts.MEDIA_UNAVAILABLE.format(n=m["n"]) for m in rendered.get("media") or []
                   if m.get("status") != "ok" or m.get("id") in view.failed_media]
    if unavailable:
        blocks.append(_escape_lines(formatter, unavailable))
    blocks.extend(_raw(formatter, spec["block"]) for spec in fields if spec.get("block"))

    state = view.state
    closed = view.dispatch_state == "closed" or state == "not_delivered"
    live = state in ("pending", "delivered") and view.dispatch_state == "open"
    # A `no_answer` (sem gabarito) leva os controles com o "Não sei" e o `LATE_HOWTO`; no
    # oferecimento arquivado, só "Janela fechada".
    late = state == "no_answer" and not view.archived
    feedback_at: int | None = None
    keyboard = None
    if state in ("answered", "answered_late"):
        feedback = _feedback(formatter, view)
        if feedback is not None:
            feedback_at = len(blocks)
            blocks.append(feedback)
    elif state == "dont_know":
        blocks.append(formatter.escape(deliverytexts.DONT_KNOW_RECORDED))
    elif state == "question_failed":
        blocks.append(formatter.escape(deliverytexts.QUESTION_FAILED))
    elif live or late:
        # O erro de formato não entra na renderização: a explicação traduzida sai numa mensagem própria
        # da correção, com o novo pedido do campo (o desfecho `Graded(invalid)`).
        if len(fields) > 1 and view.current_field is not None:
            blocks.append(formatter.escape(deliverytexts.progress(view.current_field + 1, len(fields))))
        keyboard = _keyboard(view)
    if closed:
        # A `question_failed` leva "Janela fechada" com o disparo fechado, nunca o gabarito (o desfecho
        # `Broken`).
        blocks.append(formatter.escape(deliverytexts.CLOSED_NOTICE))
        if view.revealed and state != "question_failed":
            keys = list(rendered.get("answer_key") or [])
            blocks.extend(_raw(formatter, entry["markup"]) for entry in keys if _allowed(entry, view))
            explanation = rendered.get("explanation")
            if _allowed(explanation, view):
                blocks.append(_raw(formatter, explanation["markup"]))
        if late:
            blocks.append(formatter.escape(deliverytexts.LATE_HOWTO))
    elif view.dispatch_state == "open" and view.closes_at is not None and state in ("pending", "delivered"):
        blocks.append(formatter.escape(deliverytexts.deadline(view.closes_at, view.timezone)))
    if (live or late) and not rendered.get("buttons"):
        # O `/naosei` no canal sem botões, na entrega aberta e na linha de fechamento da `no_answer`.
        blocks.append(formatter.escape(deliverytexts.DONT_KNOW_HOWTO))
    return blocks, keyboard, feedback_at


def _keyboard_json(keyboard: tuple[tuple[InlineButton, ...], ...] | None) -> list | None:
    if keyboard is None:
        return None
    return [[[button.label, button.data] for button in row] for row in keyboard]


def _keyboard_from_json(value: Any) -> tuple[tuple[InlineButton, ...], ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(tuple(InlineButton(str(label), str(data)) for label, data in row) for row in value)


def _load_view(conn: psycopg.Connection, delivery_row_id: int) -> _View:
    """O estado da entrega para a renderização; `LookupError` sem entrega ou sem conteúdo."""
    row = conn.execute(
        "SELECT d.state, d.current_field, d.controls_version, d.render_version, d.button_token, "
        "d.revealed_at IS NOT NULL, s.state, s.closes_at, o.timezone, coalesce(q.single_variant, false), "
        "c.rendered, c.selection, o.state = 'archived' "
        "FROM deliveries d JOIN dispatches s ON s.id = d.dispatch_id "
        "JOIN offerings o ON o.id = s.offering_id JOIN question_refs q ON q.id = s.question_ref_id "
        "JOIN delivery_contents c ON c.delivery_id = d.id WHERE d.id = %s",
        (delivery_row_id,),
    ).fetchone()
    if row is None:
        raise LookupError("entrega sem conteúdo")
    final = conn.execute(
        "SELECT raw_input, score, partial_scores, correct_answers FROM submissions WHERE delivery_id = %s "
        "AND outcome = 'graded' AND valid ORDER BY id DESC LIMIT 1",
        (delivery_row_id,),
    ).fetchone()
    failed = conn.execute(
        "SELECT media_id FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'media' "
        "AND state = 'failed'",
        (delivery_row_id,),
    ).fetchall()
    selection = row[11] if isinstance(row[11], list) else []
    return _View(
        state=row[0], current_field=None if row[1] is None else int(row[1]), controls_version=int(row[2]),
        render_version=int(row[3]), button_token=row[4], revealed=bool(row[5]), dispatch_state=row[6],
        closes_at=row[7], timezone=row[8], single_variant=bool(row[9]), rendered=row[10] or {},
        selection=[str(k) for k in selection],
        final=None if final is None else {"raw_input": final[0], "score": final[1], "partial_scores": final[2],
                                          "correct_answers": final[3]},
        failed_media={m[0] for m in failed if m[0]}, archived=bool(row[12]),
    )


def _store(conn: psycopg.Connection, delivery_row_id: int, view: _View, formatter: MessageFormatter, *,
           bump: bool) -> RenderedMessage:
    """Renderiza `view`, grava `current_render` (e `render_version + 1`, com `bump`) e devolve."""
    composed, keyboard, fits = _render(view, formatter)
    version = view.render_version + (1 if bump else 0)
    if bump:
        conn.execute("UPDATE deliveries SET render_version = %s WHERE id = %s", (version, delivery_row_id))
    current = {"version": version, "format": formatter.text_format, "text": composed.markup,
               "keyboard": _keyboard_json(keyboard), "reply_request": None, "fits": fits}
    conn.execute("UPDATE delivery_contents SET current_render = %s WHERE delivery_id = %s",
                 (Jsonb(current), delivery_row_id))
    return RenderedMessage(text=composed.markup, text_format=formatter.text_format, keyboard=keyboard,
                           reply_request=None, render_version=version, fits=fits)


def render_state(conn: psycopg.Connection, delivery_row_id: int, *, formatter: MessageFormatter,
                 capabilities: ChannelCapabilities, now: datetime) -> RenderedMessage:
    """A renderização completa do estado corrente, a partir do que a entrega
    guardou (sem o motor), gravada: `render_version += 1` e `current_render`; devolve a nova versão.

    O chamador trava a entrega antes (`FOR NO KEY UPDATE`) e passa o formatador do dialeto gravado
    (`channel.formatter_for(rendered["format"])`). `fits = False`: a renderização completa não
    coube e a gravada saiu sem o feedback com o eco. `LookupError` sem conteúdo (sem variante).
    As capacidades já decidiram o teclado e a degradação na abertura (`rendered`); `now` fica para as
    regras que dependem do horário."""
    del capabilities, now
    return _store(conn, delivery_row_id, _load_view(conn, delivery_row_id), formatter, bump=True)


def load_ir(conn: psycopg.Connection, delivery_row_id: int) -> dict:
    """A IR inteira da variante: a guardada, com o `data_base64` de cada mídia lido de
    `media_blobs`, sem o motor; `LookupError` sem conteúdo."""
    row = conn.execute("SELECT ir, media_refs FROM delivery_contents WHERE delivery_id = %s",
                       (delivery_row_id,)).fetchone()
    if row is None:
        raise LookupError("entrega sem conteúdo")
    ir, refs = row[0], row[1] or {}
    for item in ir.get("media") or []:
        sha = refs.get(item.get("id"))
        if sha is None:
            continue
        blob = conn.execute("SELECT data FROM media_blobs WHERE sha256 = %s", (sha,)).fetchone()
        if blob is not None:
            item["data_base64"] = base64.b64encode(bytes(blob[0])).decode("ascii")
    return ir


def open_fields(conn: psycopg.Connection, *, bot_id: int, person_id: int,
                received_at: datetime | None = None) -> list[OpenField]:
    """Os campos correntes abertos da pessoa no bot (a ligação do texto ao campo): entregas
    `delivered` de disparo `open` do oferecimento do bot, sem correção pendente, com campo corrente; a
    ligação do texto livre ao campo usa só as `typeable`. Com `received_at` (aditivo, só-nomeado), só os disparos
    com `closes_at > received_at` (a atrasada exige "responder")."""
    rows = conn.execute(
        "SELECT d.id, d.delivery_uuid, d.current_field, c.rendered FROM deliveries d "
        "JOIN enrollments e ON e.id = d.enrollment_id JOIN dispatches s ON s.id = d.dispatch_id "
        "JOIN bots b ON b.offering_id = s.offering_id JOIN delivery_contents c ON c.delivery_id = d.id "
        "WHERE e.person_id = %(person)s AND b.id = %(bot)s AND d.state = 'delivered' AND s.state = 'open' "
        "AND d.current_field IS NOT NULL "
        "AND (%(received)s::timestamptz IS NULL OR s.closes_at > %(received)s::timestamptz) "
        "AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.delivery_id = d.id AND x.outcome = 'grading') "
        "ORDER BY d.id",
        {"person": person_id, "bot": bot_id, "received": received_at},
    ).fetchall()
    out: list[OpenField] = []
    for delivery_row, delivery_uuid, current, rendered in rows:
        fields = list((rendered or {}).get("fields") or [])
        if not 0 <= int(current) < len(fields):
            continue
        spec = fields[int(current)]
        out.append(OpenField(delivery_row_id=int(delivery_row), delivery_id=delivery_uuid,
                             field_index=int(current), field_type=str(spec.get("type")),
                             typeable=bool(spec.get("typeable"))))
    return out


# --------------------------------------------------------------------------------------------------
# Eventos e transições
# --------------------------------------------------------------------------------------------------


def _event_data(conn: psycopg.Connection, delivery_row_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT o.offering_id, s.dispatch_uuid, d.delivery_uuid, p.pseudo_id FROM deliveries d "
        "JOIN dispatches s ON s.id = d.dispatch_id JOIN offerings o ON o.id = s.offering_id "
        "JOIN enrollments e ON e.id = d.enrollment_id JOIN people p ON p.id = e.person_id WHERE d.id = %s",
        (delivery_row_id,),
    ).fetchone()
    assert row is not None
    return {"offering_id": row[0], "dispatch_id": row[1], "delivery_id": row[2], "person": row[3]}


def _not_delivered(conn: psycopg.Connection, delivery_row_id: int, reason: str, now: datetime) -> bool:
    """`pending` → `not_delivered/<reason>`, com as mensagens `pending` da entrega
    canceladas na mesma transação e `delivery.not_delivered`; `False` se não estava `pending`."""
    changed = conn.execute(
        "UPDATE deliveries SET state = 'not_delivered', undelivered_reason = %s, current_field = NULL "
        "WHERE id = %s AND state = 'pending' RETURNING id",
        (reason, delivery_row_id),
    ).fetchone()
    if changed is None:
        return False
    cancel_delivery_messages(conn, delivery_row_id=delivery_row_id)
    append_event(conn, EventType.DELIVERY_NOT_DELIVERED,
                 {**_event_data(conn, delivery_row_id), "undelivered_reason": reason},
                 occurred_at=now, actor_token_id=None)
    return True


def _log_failure(row: DeliveryRow, reason: str) -> None:
    """O registro de uma mensagem de entrega que falhou de vez sem mudar a entrega:
    só o papel, a operação e o código, sem texto nem identidade."""
    logger.info("delivery.message_failed", extra={
        "event": "delivery.message_failed", "id": row.message_id, "role": row.role,
        "operation": row.operation, "reason": reason if re.fullmatch(r"[a-z_]{1,40}", reason) else "other"})


# --------------------------------------------------------------------------------------------------
# Colaboradores da fila
# --------------------------------------------------------------------------------------------------


def _original(conn: psycopg.Connection, delivery_row_id: int, role: str = "question") -> tuple[str, int] | None:
    """A mensagem original de um papel: na `question`, a principal e, sem principal, a última
    `question` `sent` da entrega; nos demais, a última `send` `sent` do papel. `(channel_ref,
    endereço)` ou `None`."""
    if role == "question":
        principal = conn.execute(
            "SELECT m.channel_ref, m.delivery_endpoint_id FROM deliveries d "
            "JOIN outbox_messages m ON m.id = d.principal_message_id WHERE d.id = %s AND m.channel_ref IS NOT NULL",
            (delivery_row_id,),
        ).fetchone()
        if principal is not None:
            return str(principal[0]), int(principal[1])
    last = conn.execute(
        "SELECT channel_ref, delivery_endpoint_id FROM outbox_messages WHERE delivery_id = %s "
        "AND delivery_role = %s AND operation = 'send' AND state = 'sent' AND channel_ref IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (delivery_row_id, role),
    ).fetchone()
    return None if last is None else (str(last[0]), int(last[1]))


def _caption(number: int, alt: Any) -> str:
    """"Figura N" + `alt` em texto puro, até 1024 unidades UTF-16."""
    text = f"Figura {number}: {alt}" if isinstance(alt, str) and alt.strip() else f"Figura {number}"
    units = 0
    out: list[str] = []
    for ch in text:
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > CAPTION_MAX_UNITS:
            break
        out.append(ch)
    return "".join(out)


class DeliveryContentLoader:
    """A implementação de `DeliveryContent`: `DeliveryContentLoader(channel, *, delivery=None)`, com o
    canal com rota do worker (o formatador do dialeto gravado) e a configuração da entrega (o
    limite das fotos; `None` = `DeliverySettings()`)."""

    def __init__(self, channel: DeliveryChannel, *, delivery: DeliverySettings | None = None) -> None:
        self._channel = channel
        self._delivery = delivery if delivery is not None else DeliverySettings()

    def load(self, conn: psycopg.Connection, row: DeliveryRow) -> OutgoingMessage | Skip:
        found = conn.execute(
            "SELECT d.state, d.current_field, c.current_render, c.rendered, c.ir, c.media_refs, d.render_version "
            "FROM deliveries d JOIN delivery_contents c ON c.delivery_id = d.id WHERE d.id = %s",
            (row.delivery_row_id,),
        ).fetchone()
        if found is None:
            return Skip("no_content")
        state, current_field, current, rendered, ir, refs, version = found
        rendered = rendered or {}
        if row.role in ("question", "fallback") and not _current_enough(current, version, row.render_version):
            # Defesa: a versão pedida (ou a da entrega) é mais nova que a `current_render` gravada (quem
            # mudou o estado não chamou `render_state`); a renderização sai do estado corrente, só em
            # memória (a posse não escreve), no dialeto gravado.
            view = _load_view(conn, row.delivery_row_id)
            composed, keyboard, _ = _render(view, self._channel.formatter_for(str(rendered.get("format"))))
            current = {"version": version, "format": str(rendered.get("format")), "text": composed.markup,
                       "keyboard": _keyboard_json(keyboard)}
        base = OutgoingMessage(message_id=row.message_id, recipient=row.recipient, text="", answerable=True)
        if row.operation in _EDIT_OPERATIONS:
            target = _original(conn, row.delivery_row_id, row.role)
            if target is None:
                return Skip("no_edit_target")
            if not isinstance(current, Mapping):
                return Skip("no_render")
            return _replace(base, text=str(current.get("text") or ""), text_format=str(current.get("format")),
                            keyboard=_keyboard_from_json(current.get("keyboard")), edit_ref=target[0],
                            markup_only=row.operation == "edit_markup")
        if row.role == "media":
            return self._media(conn, row, base, ir or {}, refs or {})
        if row.role == "field_prompt":
            return self._field_prompt(conn, row, base, state, current_field, rendered)
        if row.role == "notice":
            return self._notice(conn, row, base, rendered)
        if not isinstance(current, Mapping):
            return Skip("no_render")
        message = _replace(base, text=str(current.get("text") or ""), text_format=str(current.get("format")),
                           keyboard=_keyboard_from_json(current.get("keyboard")))
        if row.role == "fallback":
            original = _original(conn, row.delivery_row_id)
            if original is not None and original[1] == row.endpoint_row_id:
                message = _replace(message, reply_to_ref=original[0])
        return message

    def _media(self, conn: psycopg.Connection, row: DeliveryRow, base: OutgoingMessage,
               ir: Mapping[str, Any], refs: Mapping[str, Any]) -> OutgoingMessage | Skip:
        items = list(ir.get("media") or [])
        index = next((i for i, m in enumerate(items, start=1) if m.get("id") == row.media_id), None)
        sha = refs.get(row.media_id)
        if index is None or sha is None:
            return Skip("media_missing")
        blob = conn.execute("SELECT content_type, size_bytes, data FROM media_blobs WHERE sha256 = %s",
                            (sha,)).fetchone()
        if blob is None:
            return Skip("media_missing")
        item = items[index - 1]
        content_type = str(item.get("content_type") or blob[0])
        data = bytes(blob[2])
        as_document = content_type not in PHOTO_TYPES or len(data) > self._delivery.photo_max_bytes
        caption = _caption(_figure_number(str(row.media_id), index), item.get("alt"))
        filename = posixpath.basename(str(item.get("filename") or "")) or f"{row.media_id}"
        media = MediaPayload(media_id=str(row.media_id), filename=filename, content_type=content_type,
                             as_document=as_document, caption=caption, data=data)
        return _replace(base, text=caption, media=media)

    def _field_prompt(self, conn: psycopg.Connection, row: DeliveryRow, base: OutgoingMessage, state: str,
                      current_field: int | None, rendered: Mapping[str, Any]) -> OutgoingMessage | Skip:
        fields = list(rendered.get("fields") or [])
        index = row.field_index
        if (state not in LIVE_STATES or current_field is None or index is None or index != current_field
                or not 0 <= index < len(fields)):
            return Skip("field_not_current")
        spec = fields[index]
        formatter = self._channel.formatter_for(_short_dialect(rendered))
        label = formatter.escape(spec["label"]).markup if spec.get("label") else None
        suffix = formatter.escape(spec["suffix"]).markup if spec.get("suffix") else None
        text = formatter.compose([_raw(formatter, deliverytexts.field_request(index + 1, len(fields), label,
                                                                              suffix))])
        hint = spec.get("hint")
        original = _original(conn, row.delivery_row_id)
        reply = original[0] if original is not None and original[1] == row.endpoint_row_id else None
        return _replace(base, text=text.markup, text_format=formatter.text_format,
                        reply_request=ReplyRequest(hint if isinstance(hint, str) and hint else None),
                        reply_to_ref=reply)

    def _notice(self, conn: psycopg.Connection, row: DeliveryRow, base: OutgoingMessage,
                rendered: Mapping[str, Any]) -> OutgoingMessage | Skip:
        """O feedback curto, da última submissão válida: nas várias partes,
        "Parte {k}: {✅|◐|❌}" por parte; e "Resultado: {veredito} · {nota}"; sem eco nem
        marcas por alternativa. As mesmas funções do `FEEDBACK` (`deliverytexts`)."""
        final = conn.execute(
            "SELECT score, partial_scores FROM submissions WHERE delivery_id = %s AND outcome = 'graded' "
            "AND valid ORDER BY id DESC LIMIT 1",
            (row.delivery_row_id,),
        ).fetchone()
        if final is None:
            return Skip("no_feedback")
        fields = list(rendered.get("fields") or [])
        score = _score(final[0])
        lines = [f"Resultado: {deliverytexts.verdict(score)} · {deliverytexts.score_text(score)}"]
        if len(fields) > 1:
            marks = [deliverytexts.part_mark(_part_score(final[1], spec["name"])) for spec in fields]
            lines = [f"Parte {k}: {mark}" for k, mark in enumerate(marks, start=1)] + lines
        formatter = self._channel.formatter_for(_short_dialect(rendered))
        text = formatter.compose([_escape_lines(formatter, lines)])
        original = _original(conn, row.delivery_row_id)
        reply = original[0] if original is not None and original[1] == row.endpoint_row_id else None
        return _replace(base, text=text.markup, text_format=formatter.text_format, reply_to_ref=reply)


def _short_dialect(rendered: Mapping[str, Any]) -> str:
    """O dialeto das mensagens curtas de texto fixo (pedido de campo e `notice`): o dialeto gravado na
    entrega, que vale para toda mensagem dela (`rendered.format`; no pedido de campo,
    `send_message`/`send_rich_message`). No Rich, o `sendRichMessage` (o `sendMessage`
    em texto puro fica com as respostas fixas ao aluno); no HTML simples, `parse_mode=HTML`; no
    console, texto puro."""
    text_format = rendered.get("format")
    return str(text_format) if text_format in ("rich", "html") else "plain"


def _current_enough(current: Any, delivery_version: Any, wanted: int | None) -> bool:
    """A `current_render` gravada mostra pelo menos a versão da entrega e a que a edição pede."""
    if not isinstance(current, Mapping):
        return False
    shown = current.get("version")
    if not isinstance(shown, int):
        return False
    return shown >= int(delivery_version or 0) and (wanted is None or shown >= wanted)


def _replace(message: OutgoingMessage, **changes: Any) -> OutgoingMessage:
    from dataclasses import replace

    return replace(message, **changes)


class DeliveryOutcomeRecorder:
    """A implementação de `DeliveryOutcome`: `DeliveryOutcomeRecorder(channel, *, clock)`, com o canal
    com rota do worker (capacidades e formatador do dialeto gravado) e o `Clock` da fila.

    Trava o endereço e a entrega (nessa ordem, depois da linha da fila, antes do lock dos eventos) e
    aplica o desfecho do envio e a revelação devida (ver `record`)."""

    def __init__(self, channel: DeliveryChannel, *, clock: Clock) -> None:
        self._channel = channel
        self._clock = clock

    def record(self, conn: psycopg.Connection, row: DeliveryRow,
               result: Sent | Undeliverable | Exhausted) -> None:
        """- `question` enviada numa entrega `pending` → `delivered` (principal, `sent_at`,
          `delivery.delivered`); numa entrega que já não está `pending` → só registro, `current_render`
          com a janela fechada e sem teclado (`render_version + 1`) e a edição dela;
        - `question` recusada → `not_delivered` (`endpoint_blocked` com `blocked` ou pelo `reason`
          `endpoint_blocked`; senão `channel_refused`); esgotada → `channel_unavailable`; as
          mensagens `pending` da entrega canceladas;
        - `media` bloqueada (403) → `not_delivered/endpoint_blocked`; outra falha definitiva → o aviso
          de figura indisponível na renderização corrente (a `question` ainda não saiu);
        - edição recusada ou esgotada → `fallback` em resposta à original, com as edições `pending` do
          mesmo papel canceladas; bloqueio → só registro; `endpoint_inactive` → o
          `fallback` no endereço vigente, se há;
        - `fallback` enviado com os controles → a principal; falha de `fallback`, `notice` ou
          `field_prompt` → só registro."""
        now = self._clock.now()
        # Endereço e entrega em FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs
        # (as travas seguem a ordem fixa).
        conn.execute("SELECT 1 FROM delivery_endpoints WHERE id = %s FOR NO KEY UPDATE", (row.endpoint_row_id,))
        found = conn.execute(
            "SELECT d.state, d.variant_at IS NOT NULL, c.rendered ->> 'format', c.current_render "
            "FROM deliveries d LEFT JOIN delivery_contents c ON c.delivery_id = d.id WHERE d.id = %s "
            "FOR NO KEY UPDATE OF d",
            (row.delivery_row_id,),
        ).fetchone()
        if found is None:
            return
        state, has_variant, text_format, current = found
        if row.operation in _EDIT_OPERATIONS:
            self._edit_outcome(conn, row, result, has_variant, now)
        elif row.role == "question":
            self._question_outcome(conn, row, result, state, text_format, now)
        elif row.role == "media":
            self._media_outcome(conn, row, result, state, text_format)
        elif row.role == "fallback" and isinstance(result, Sent):
            keyboard = current.get("keyboard") if isinstance(current, Mapping) else None
            if keyboard and state in ("delivered", "no_answer"):
                conn.execute("UPDATE deliveries SET principal_message_id = %s WHERE id = %s",
                             (row.message_id, row.delivery_row_id))
        elif not isinstance(result, Sent):
            _log_failure(row, result.reason)

    def _question_outcome(self, conn: psycopg.Connection, row: DeliveryRow,
                          result: Sent | Undeliverable | Exhausted, state: str, text_format: str | None,
                          now: datetime) -> None:
        if isinstance(result, Sent):
            if state == "pending":
                conn.execute(
                    "UPDATE deliveries SET state = 'delivered', sent_at = %s, principal_message_id = %s "
                    "WHERE id = %s",
                    (now, row.message_id, row.delivery_row_id),
                )
                append_event(conn, EventType.DELIVERY_DELIVERED, _event_data(conn, row.delivery_row_id),
                             occurred_at=now, actor_token_id=None)
            elif text_format is not None:
                self._late_send(conn, row, text_format, now)
            return
        if state != "pending":
            _log_failure(row, result.reason)
            return
        if isinstance(result, Exhausted):
            reason = UndeliveredReason.CHANNEL_UNAVAILABLE
        elif result.blocked or result.reason == "endpoint_blocked":
            reason = UndeliveredReason.ENDPOINT_BLOCKED
        else:
            reason = UndeliveredReason.CHANNEL_REFUSED
        _not_delivered(conn, row.delivery_row_id, str(reason), now)

    def _late_send(self, conn: psycopg.Connection, row: DeliveryRow, text_format: str, now: datetime) -> None:
        """A `question` aceita numa entrega que já não está `pending` (virou `not_delivered`
        no fechamento): sem transição; a renderização "questão + janela fechada" sem teclado e a edição
        dela, cujo alvo é esta `question` (sem principal); num canal sem edição, o `fallback`."""
        logger.info("delivery.late_send", extra={"event": "delivery.late_send", "id": row.message_id})
        formatter = self._channel.formatter_for(text_format)
        rendered = _store(conn, row.delivery_row_id, _load_view(conn, row.delivery_row_id), formatter, bump=True)
        if self._channel.capabilities.can_edit:
            enqueue_delivery_edit(conn, delivery_row_id=row.delivery_row_id, recipient=row.recipient,
                                  bot_id=row.bot_id, endpoint_row_id=row.endpoint_row_id, role="question",
                                  markup_only=False, version=rendered.render_version, now=now)
        else:
            enqueue_delivery(conn, delivery_row_id=row.delivery_row_id, recipient=row.recipient, bot_id=row.bot_id,
                             endpoint_row_id=row.endpoint_row_id, role="fallback", idempotency_key=None, now=now)

    def _media_outcome(self, conn: psycopg.Connection, row: DeliveryRow,
                       result: Sent | Undeliverable | Exhausted, state: str, text_format: str | None) -> None:
        if isinstance(result, Sent):
            return
        now = self._clock.now()
        if isinstance(result, Undeliverable) and (result.blocked or result.reason == "endpoint_blocked"):
            if state == "pending":
                _not_delivered(conn, row.delivery_row_id, str(UndeliveredReason.ENDPOINT_BLOCKED), now)
                return
        _log_failure(row, result.reason)
        if state in LIVE_STATES and text_format is not None and row.media_id is not None:
            # A mídia falhou de vez: o aviso textual entra na renderização corrente, que a `question`
            # (ainda na fila, pela ordem por entrega) lê na posse.
            view = _load_view(conn, row.delivery_row_id)
            view.failed_media.add(row.media_id)
            _store(conn, row.delivery_row_id, view, self._channel.formatter_for(text_format), bump=False)

    def _edit_outcome(self, conn: psycopg.Connection, row: DeliveryRow, result: Sent | Undeliverable | Exhausted,
                      has_variant: bool, now: datetime) -> None:
        if isinstance(result, Sent):
            return
        _log_failure(row, result.reason)
        if not has_variant:
            return
        if isinstance(result, Undeliverable) and (result.blocked or result.reason == "endpoint_blocked"):
            return  # o bloqueio só registra
        endpoint = row.endpoint_row_id
        if isinstance(result, Undeliverable) and result.reason == "endpoint_inactive":
            # A mensagem nova no endereço vigente da pessoa no bot; sem vigente, só o registro.
            current = conn.execute(
                "SELECT n.id FROM delivery_endpoints o JOIN delivery_endpoints n ON n.person_id = o.person_id "
                "AND n.bot_id = o.bot_id AND n.state = 'active' WHERE o.id = %s ORDER BY n.id DESC LIMIT 1",
                (row.endpoint_row_id,),
            ).fetchone()
            if current is None or int(current[0]) == row.endpoint_row_id:
                return
            endpoint = int(current[0])
        # A mensagem nova, em resposta à original, com a renderização completa (lida na posse);
        # as edições `pending` da mesma mensagem ficam canceladas.
        cancel_delivery_messages(conn, delivery_row_id=row.delivery_row_id, role=row.role,
                                 operations=tuple(sorted(_EDIT_OPERATIONS)))
        enqueue_delivery(conn, delivery_row_id=row.delivery_row_id, recipient=row.recipient, bot_id=row.bot_id,
                         endpoint_row_id=endpoint, role="fallback", idempotency_key=None, now=now)


# --------------------------------------------------------------------------------------------------
# Tratadores
# --------------------------------------------------------------------------------------------------


class _TaskHandler:
    """Tratador de tarefa da entrega (chamável, com `close()`)."""

    def __call__(self, task: ClaimedTask) -> None:
        raise NotImplementedError("não implementado")

    def close(self) -> None:
        raise NotImplementedError("não implementado")


def _base36(value: int) -> str:
    """A seed do motor: a base 36 canônica (minúscula, sem zeros à esquerda) de `deliveries.seed`."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = int(value)
    out = ""
    while True:
        value, rest = divmod(value, 36)
        out = digits[rest] + out
        if value == 0:
            return out


def _payload_row(task: ClaimedTask) -> int:
    value = task.payload.get("delivery_row_id") if isinstance(task.payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentTaskError("payload sem delivery_row_id")
    return value


class _PrepareHandler(_TaskHandler):
    """`delivery.prepare`: a variante, o conteúdo e as mensagens da entrega."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                 channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._engine = engine
        self._channel = channel

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        delivery_row_id = _payload_row(task)
        conn = self._db.get()
        # Pré-checagem sem trava: fora de `pending` ou já com variante, termina sem o motor.
        row = conn.execute(
            "SELECT d.state, d.variant_at, d.seed, q.repo, q.commit_sha, q.qid, q.id FROM deliveries d "
            "JOIN dispatches s ON s.id = d.dispatch_id JOIN question_refs q ON q.id = s.question_ref_id "
            "WHERE d.id = %s",
            (delivery_row_id,),
        ).fetchone()
        if row is None:
            raise PermanentTaskError("entrega inexistente")
        state, variant_at, seed, repo, commit, qid, ref_row = row
        if state != "pending" or variant_at is not None:
            return
        channel = self._channel
        if channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        if self._engine is None:
            raise EngineUnavailable("unconfigured")
        ref = QuestionRef(repo=repo, commit=commit, qid=qid)
        try:
            result = self._engine.create_variant(ref, _base36(seed))
        except EngineRequestRejected:
            self._fail(conn, delivery_row_id, QuestionFailure.ENGINE_REJECTED)
            return
        except EngineNotFound:
            self._refetch(conn, int(ref_row))
            raise
        if isinstance(result, Unsupported):
            self._fail(conn, delivery_row_id, QuestionFailure.VARIANT_UNSUPPORTED)
        elif isinstance(result, Broken):
            self._fail(conn, delivery_row_id, QuestionFailure.VARIANT_BROKEN)
        elif isinstance(result, Variant):
            self._store(conn, delivery_row_id, result, channel)
        else:
            raise TypeError("resultado do motor fora da porta")

    def _fail(self, conn: psycopg.Connection, delivery_row_id: int, failure: QuestionFailure) -> None:
        """`pending` → `question_failed/<failure>` com `delivery.question_failed` (disparo `FOR SHARE` →
        entrega `FOR NO KEY UPDATE`); sem efeito se a entrega já não está `pending` sem variante."""
        now = self._clock.now()
        with conn.transaction():
            _lock_for_prepare(conn, delivery_row_id)
            self._fail_locked(conn, delivery_row_id, failure, now)

    @staticmethod
    def _fail_locked(conn: psycopg.Connection, delivery_row_id: int, failure: QuestionFailure,
                     now: datetime) -> None:
        changed = conn.execute(
            "UPDATE deliveries SET state = 'question_failed', failure = %s, current_field = NULL "
            "WHERE id = %s AND state = 'pending' AND variant_at IS NULL RETURNING id",
            (str(failure), delivery_row_id),
        ).fetchone()
        if changed is not None:
            append_event(conn, EventType.DELIVERY_QUESTION_FAILED,
                         {**_event_data(conn, delivery_row_id), "failure": str(failure)},
                         occurred_at=now, actor_token_id=None)

    def _refetch(self, conn: psycopg.Connection, ref_row: int) -> None:
        """`not_found`: `question.refetch:<ref>:r<k>` se não há tarefa viva do tipo para
        a referência; a entrega continua `pending`, e a tarefa repete."""
        subject = {"question_ref_row_id": ref_row}
        with conn.transaction():
            if not has_live_task(conn, _REFETCH_TASK_TYPE, subject):
                done, _ = finished_rounds(conn, _REFETCH_TASK_TYPE, subject)
                enqueue_in_transaction(conn, _REFETCH_TASK_TYPE, subject, now=self._clock.now(),
                                       idempotency_key=f"{_REFETCH_TASK_TYPE}:{ref_row}:r{done + 1}")

    def _store(self, conn: psycopg.Connection, delivery_row_id: int, variant: Variant,
               channel: DeliveryChannel) -> None:
        """A variante, o conteúdo, as mídias, `variant_at`, as versões, o campo corrente e as mensagens
        na fila, numa transação só; fora dos limites do canal, `channel_limits_exceeded`."""
        formatter = channel.formatter
        capabilities = channel.capabilities
        ir = _plain_json(variant.ir)
        snapshot = _plain_json(variant.snapshot)
        rendered = build_rendered(ir, formatter=formatter, capabilities=capabilities)
        blobs: list[tuple[str, str, bytes]] = []
        refs: dict[str, str] = {}
        stored_ir = dict(ir)
        stored_media = []
        for item in ir.get("media") or []:
            item = dict(item)
            encoded = item.pop("data_base64", None)
            if item.get("status") == "ok" and isinstance(encoded, str):
                data = base64.b64decode(encoded)
                sha = hashlib.sha256(data).hexdigest()
                refs[str(item["id"])] = sha
                blobs.append((sha, str(item.get("content_type")), data))
            stored_media.append(item)
        stored_ir["media"] = stored_media
        now = self._clock.now()
        with conn.transaction():
            locked = _lock_for_prepare(conn, delivery_row_id)
            if locked is None:
                return
            (dispatch_state, closes_at, timezone, single_variant, token, endpoint_row, bot_id, pseudo_id) = locked
            view = _View(state="pending", current_field=0, controls_version=1, render_version=0,
                         button_token=token, revealed=False, dispatch_state=dispatch_state, closes_at=closes_at,
                         timezone=timezone, single_variant=bool(single_variant), rendered=rendered)
            composed, keyboard, fits = _render(view, formatter)
            fields = rendered["fields"]
            if fields and fields[0]["typeable"]:
                prompt = formatter.escape(deliverytexts.field_request(1, len(fields), fields[0]["label"],
                                                                      fields[0]["suffix"]))
                fits = fits and prompt.fits
            if not fits or not fields:
                self._fail_locked(conn, delivery_row_id, QuestionFailure.CHANNEL_LIMITS_EXCEEDED, now)
                return
            # Em ordem de sha256: dois preparos com as mesmas mídias não se esperam em ordens opostas na inserção
            # especulativa da PK.
            for sha, content_type, data in sorted(blobs, key=lambda blob: blob[0]):
                conn.execute(
                    "INSERT INTO media_blobs (sha256, content_type, size_bytes, data, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
                    (sha, content_type, len(data), data, now),
                )
            current = {"version": 1, "format": formatter.text_format, "text": composed.markup,
                       "keyboard": _keyboard_json(keyboard), "reply_request": None, "fits": True}
            conn.execute(
                "UPDATE deliveries SET variant_at = %s, render_version = 1, controls_version = 1, "
                "current_field = 0 WHERE id = %s",
                (now, delivery_row_id),
            )
            conn.execute(
                "INSERT INTO delivery_contents (delivery_id, snapshot, ir, media_refs, rendered, parts, selection, "
                "current_render) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (delivery_row_id, Jsonb(snapshot), Jsonb(stored_ir), Jsonb(refs), Jsonb(rendered), Jsonb({}),
                 Jsonb([]), Jsonb(current)),
            )
            target = {"delivery_row_id": delivery_row_id, "recipient": pseudo_id, "bot_id": bot_id,
                      "endpoint_row_id": endpoint_row, "now": now}
            for item in stored_media:
                if item["id"] in refs:
                    enqueue_delivery(conn, role="media", media_id=str(item["id"]),
                                     idempotency_key=f"{PREPARE_TASK_TYPE}:{delivery_row_id}:media:{item['id']}",
                                     **target)
            enqueue_delivery(conn, role="question", idempotency_key=f"{PREPARE_TASK_TYPE}:{delivery_row_id}:question",
                             **target)
            if fields[0]["typeable"]:
                enqueue_delivery(conn, role="field_prompt", field_index=0,
                                 idempotency_key=f"{PREPARE_TASK_TYPE}:{delivery_row_id}:field_prompt:0",
                                 **target)


def _lock_for_prepare(conn: psycopg.Connection, delivery_row_id: int) -> tuple | None:
    """Disparo `FOR SHARE` → entrega `FOR NO KEY UPDATE … WHERE state = 'pending' AND variant_at IS NULL`
    (a ordem fixa das travas), com o que a gravação precisa; `None` se a entrega já andou."""
    dispatch = conn.execute(
        "SELECT s.id FROM deliveries d JOIN dispatches s ON s.id = d.dispatch_id WHERE d.id = %s",
        (delivery_row_id,),
    ).fetchone()
    if dispatch is None:
        return None
    conn.execute("SELECT 1 FROM dispatches WHERE id = %s FOR SHARE", (dispatch[0],))
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    return conn.execute(
        "SELECT s.state, s.closes_at, o.timezone, q.single_variant, d.button_token, d.delivery_endpoint_id, "
        "e.bot_id, p.pseudo_id FROM deliveries d JOIN dispatches s ON s.id = d.dispatch_id "
        "JOIN offerings o ON o.id = s.offering_id JOIN question_refs q ON q.id = s.question_ref_id "
        "JOIN delivery_endpoints e ON e.id = d.delivery_endpoint_id "
        "JOIN enrollments n ON n.id = d.enrollment_id JOIN people p ON p.id = n.person_id "
        "WHERE d.id = %s AND d.state = 'pending' AND d.variant_at IS NULL FOR NO KEY UPDATE OF d",
        (delivery_row_id,),
    ).fetchone()


def _plain_json(value: Any) -> Any:
    """O JSON cru do motor como `dict`/`list` simples (o `Mapping` da porta pode ser imutável)."""
    if isinstance(value, Mapping):
        return {str(k): _plain_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(v) for v in value]
    return value


def prepare_task_handler(conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                         channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador de `delivery.prepare`: a pré-checagem sem trava, a
    variante (`create_variant(ref, base36(seed))`) fora de transação e, numa transação só, o conteúdo,
    as mídias (`ON CONFLICT DO NOTHING`), `variant_at`, `render_version = controls_version = 1`,
    `current_field = 0` e as mensagens na fila (as mídias `ok`, a `question` e o pedido do primeiro
    campo digitável). `Unsupported`/`Broken`/`EngineRequestRejected` e a renderização fora dos limites →
    `question_failed`; `EngineUnavailable` e `EngineNotFound` (com a `question.refetch`) repetem. A
    conexão abre no primeiro uso; `channel = None` numa tarefa que precisa renderizar →
    `PermanentTaskError("canal sem rota")`."""
    return _PrepareHandler(conninfo, clock=clock, settings=settings, engine=engine, channel=channel)


class _RevealHandler(_TaskHandler):
    """`delivery.reveal` (a revelação devida): a rede de segurança
    da revelação que o fechamento ou o desfecho da correção pendente não fizeram; nunca numa `no_answer`
    (ela recebe só a edição de fechamento)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings,
                 channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._channel = channel

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        delivery_row_id = _payload_row(task)
        channel = self._channel
        if channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        conn = self._db.get()
        now = self._clock.now()
        with conn.transaction():
            dispatch = conn.execute("SELECT dispatch_id FROM deliveries WHERE id = %s",
                                    (delivery_row_id,)).fetchone()
            if dispatch is None:
                raise PermanentTaskError("entrega inexistente")
            # Disparo antes da entrega; a revelação devida é conferida sob as travas.
            conn.execute("SELECT 1 FROM dispatches WHERE id = %s FOR SHARE", (dispatch[0],))
            # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
            due = conn.execute(
                "SELECT c.rendered ->> 'format' FROM deliveries d JOIN dispatches s ON s.id = d.dispatch_id "
                "JOIN delivery_contents c ON c.delivery_id = d.id "
                "WHERE d.id = %s AND s.state = 'closed' AND d.revealed_at IS NULL "
                "AND d.state IN ('answered', 'dont_know', 'answered_late') "
                "AND NOT EXISTS (SELECT 1 FROM submissions x WHERE x.delivery_id = d.id AND x.outcome = 'grading') "
                "FOR NO KEY UPDATE OF d",
                (delivery_row_id,),
            ).fetchone()
            if due is None:
                return
            conn.execute("UPDATE deliveries SET controls_version = controls_version + 1, revealed_at = %s "
                         "WHERE id = %s", (now, delivery_row_id))
            rendered = render_state(conn, delivery_row_id, formatter=channel.formatter_for(str(due[0])),
                                    capabilities=channel.capabilities, now=now)
            enqueue_state_update(conn, delivery_row_id, rendered, capabilities=channel.capabilities, now=now)


def reveal_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                        channel: DeliveryChannel | None) -> _TaskHandler:
    """O tratador de `delivery.reveal` (só a varredura o cria): com o disparo `closed`, a entrega
    `answered`, `dont_know` ou `answered_late` (nunca `no_answer`), sem `revealed_at` e sem
    correção pendente, `controls_version`/`render_version += 1`, `revealed_at` e a edição com a
    renderização completa (janela fechada e gabarito permitido, pela variante única); senão, nada. A
    conexão abre no primeiro uso."""
    return _RevealHandler(conninfo, clock=clock, settings=settings, channel=channel)


def enqueue_state_update(conn: psycopg.Connection, delivery_row_id: int, rendered: RenderedMessage, *,
                         capabilities: ChannelCapabilities, markup_only: bool = False,
                         now: datetime | None = None) -> int:
    """Leva ao aluno a renderização recém-gravada por `render_state`, na transação do chamador (que já
    travou a entrega): a edição da mensagem principal (`enqueue_delivery_edit`, com a fusão) ou, num
    canal sem edição (`can_edit = False`), um `fallback` com a renderização completa em resposta à
    original; com `rendered.fits = False` (o feedback não coube), também o
    `notice` curto. O endereço é o da abertura (sem o desvio de `answers.refresh`). Devolve o id da edição (ou
    do `fallback`)."""
    target = conn.execute(
        "SELECT d.delivery_endpoint_id, e.bot_id, p.pseudo_id FROM deliveries d "
        "JOIN delivery_endpoints e ON e.id = d.delivery_endpoint_id JOIN enrollments n ON n.id = d.enrollment_id "
        "JOIN people p ON p.id = n.person_id WHERE d.id = %s",
        (delivery_row_id,),
    ).fetchone()
    if target is None:
        raise LookupError("entrega inexistente")
    where = {"delivery_row_id": delivery_row_id, "recipient": target[2], "bot_id": int(target[1]),
             "endpoint_row_id": int(target[0]), "now": now}
    if capabilities.can_edit:
        message_id = enqueue_delivery_edit(conn, role="question", markup_only=markup_only,
                                           version=rendered.render_version, **where)
    else:
        message_id = enqueue_delivery(conn, role="fallback", idempotency_key=None, **where)
    if not rendered.fits:
        enqueue_delivery(conn, role="notice", idempotency_key=None, **where)
    return message_id
