"""Extensão da Bot API falsa para o disparo e a resposta.

Instalável por teste (`FakeBotApi.install(DispatchExtension())`, desfeita pelo `reset()`; fixture
`rich_bot_api`), para que o dublê base e o autoteste congelado dele não mudem. Como o
dublê, só usa a biblioteca padrão e **não** importa `cq_server`: se o adaptador errar, a extensão
continua capaz de acusar.

**Métodos** (`DISPATCH_METHODS`, tupla própria somada a `METHODS`): `sendRichMessage`,
`editMessageText`, `editMessageReplyMarkup`, `sendPhoto` e `sendDocument` (os dois últimos com
multipart, lido com `email.parser`), com os parâmetros documentados da Bot API 10.3. A extensão também
**assume** `sendMessage` (com `parse_mode`, `reply_markup`, `reply_parameters` e
`link_preview_options`; sem eles, as mesmas regras e os mesmos textos do dublê base) e `answerCallbackQuery`
(confere o id e o prazo).

**Validações** (tudo que desvia vai para `protocol_errors()` do dublê):
- `callback_data` de 1 a 64 bytes UTF-8 (400 `BUTTON_DATA_INVALID`); `input_field_placeholder` de 1 a
  64 unidades UTF-16;
- `text` de 1 a 4096 unidades UTF-16 **depois do parsing** (tags fora, entidades decodificadas);
- Rich: ≤ 32 768 unidades UTF-16 depois do parsing, ≤ 500 blocos, ≤ 16 níveis e ≤ 20 colunas;
- `caption` ≤ 1024; foto ≤ 10 000 000 bytes, em JPEG, PNG ou WebP; documento ≤ 50 000 000 bytes;
- `reply_markup` de edição só inline;
- HTML só com as tags e as entidades do modo (Rich ou HTML simples): tag fora da lista, tag
  sem fechamento ou fechada fora de ordem, `<` ou `&` soltos e entidade nomeada fora da lista → 400
  `can't parse entities: …`; atributo fora da lista e aninhamento que o Telegram só conserta
  (`b/i/u/s` com `code`/`pre`, `blockquote` aninhado, linguagem em `code` fora de `pre`) → só erro de
  protocolo, sem 400 [I: o TDLib corrige essas formas em silêncio].

**Violações de política do sistema** (`policy_errors()`, separadas de `protocol_errors()`): Rich sem
`skip_entity_detection: true` e `reply_parameters` sem `allow_sending_without_reply: true`.


**Estado por mensagem** (bot, chat, id), com texto, formato, teclado e mídia:
- edição igual → 400 `message is not modified…`; mensagem apagada (`delete_message`) ou
  desconhecida → 400 `message to edit not found`; id de mensagem do aluno → 400 `message can't
  be edited`; `editMessageText` numa foto ou documento → 400 `there is no text in the message to edit`;
- edição sem `reply_markup` tira o teclado inline [I: comportamento do Telegram; quem quer manter o
  teclado o reenvia];
- resposta a mensagem inexistente sem `allow_sending_without_reply` → 400 `message to be replied not
  found`; chat bloqueado → 403, como no dublê base.

**Toques**: `tap(bot, sender, message_id, data)` só com um dado do teclado corrente daquela
mensagem; `tap_raw(…)` com um dado qualquer (obsoleto, forjado, de outra mensagem ou fora de `cq1:`).
Os dois usam o `push_callback` do dublê base e registram o id como emitido; os ids gerados pelo
`push_callback` chamado direto são desconhecidos da extensão. `answerCallbackQuery` aceita cada id
emitido **uma** vez e responde 400 `query is too old…` num id desconhecido, repetido, expirado
(`expire_callback`) ou vencido (`callback_timeout`, desligado por padrão); nada disso é erro de
protocolo (a repetição da tarefa é inofensiva).

**Falhas:** 429 com `retry_after`, 5xx e rede, pelo `fail`/`fail_network` do dublê, que aceitam os
métodos da extensão; `fail(…, after_effect=True)` aplica o efeito (a mensagem existe, com o teclado) e
só então responde a falha (a mensagem duplicada).

**Consultas:** `messages`, `current`, `edits`, `answers`, `uploads` e `taps`, cada registro com
`started` (o `monotonic()` do dublê na chegada do pedido) e `clock_at` (o relógio do `reset`), para os
testes do ritmo. Os demais atributos (`add_bot`, `token_of`, `fail`, `calls`,
`protocol_errors`, `wait_for`…) vêm do dublê em que a extensão está instalada.

**Desvios deliberados** [I]: as descrições dos 400 de limite do Rich, do teclado, do placeholder, da
edição sem teclado inline e do tipo de foto são do dublê (a referência não as documenta); a conta de
blocos do Rich soma as tags de bloco e cada trecho de texto solto no nível de cima; a foto não tem as
dimensões conferidas (o teste programa o 400 pelo `fail`); o `reply_to_message` do resultado leva só o
`message_id`.
"""

from __future__ import annotations

import copy
import email.parser
import email.policy
import hashlib
import itertools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tests.helpers.bot_api import (
    _PARAMS,
    ERRORS,
    ExtensionReply,
    FakeBotApi,
    SentMessage,
    _chat_id_of,
    _is_int,
    _utf16_len,
    monotonic,
)

# ---------------------------------------------------------------------------------------------
# Constantes (Bot API 10.3)
# ---------------------------------------------------------------------------------------------

DISPATCH_METHODS: tuple[str, ...] = (
    "sendRichMessage", "editMessageText", "editMessageReplyMarkup", "sendPhoto", "sendDocument",
)
UPLOAD_METHODS: frozenset[str] = frozenset({"sendPhoto", "sendDocument"})
# Os métodos do dublê base que a extensão assume quando instalada.
TAKEN_BASE_METHODS: tuple[str, ...] = ("sendMessage", "answerCallbackQuery")

_SEND_COMMON = frozenset({
    "business_connection_id", "chat_id", "message_thread_id", "direct_messages_topic_id",
    "ephemeral_message_parameters", "disable_notification", "protect_content",
    "allow_paid_broadcast", "message_effect_id", "suggested_post_parameters", "reply_parameters",
    "reply_markup"})
DISPATCH_PARAMS: dict[str, frozenset[str]] = {
    "sendRichMessage": _SEND_COMMON | {"rich_message"},
    "editMessageText": frozenset({
        "business_connection_id", "chat_id", "message_id", "inline_message_id", "text",
        "parse_mode", "entities", "link_preview_options", "rich_message", "reply_markup"}),
    "editMessageReplyMarkup": frozenset({
        "business_connection_id", "chat_id", "message_id", "inline_message_id", "reply_markup"}),
    "sendPhoto": _SEND_COMMON | {"photo", "caption", "parse_mode", "caption_entities",
                                 "show_caption_above_media", "has_spoiler"},
    "sendDocument": _SEND_COMMON | {"document", "thumbnail", "caption", "parse_mode",
                                    "caption_entities", "disable_content_type_detection"},
}

CALLBACK_DATA_MAX_BYTES = 64
PLACEHOLDER_MAX = 64
TEXT_MAX = 4096
CAPTION_MAX = 1024
RICH_TEXT_MAX = 32_768
RICH_BLOCKS_MAX = 500
RICH_DEPTH_MAX = 16
RICH_COLUMNS_MAX = 20
PHOTO_MAX_BYTES = 10_000_000
DOCUMENT_MAX_BYTES = 50_000_000
PHOTO_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

DISPATCH_ERRORS: dict[str, tuple[int, str]] = {
    "not_modified": (400, "Bad Request: message is not modified: specified new message content and "
                          "reply markup are exactly the same as a current content and reply markup "
                          "of the message"),
    "edit_not_found": (400, "Bad Request: message to edit not found"),
    "cant_be_edited": (400, "Bad Request: message can't be edited"),
    "no_text": (400, "Bad Request: there is no text in the message to edit"),
    "reply_not_found": (400, "Bad Request: message to be replied not found"),
    "query_too_old": (400, "Bad Request: query is too old and response timeout expired or query ID "
                           "is invalid"),
    "button_data_invalid": (400, "Bad Request: BUTTON_DATA_INVALID"),
    "file_too_big": (400, "Bad Request: file is too big"),
    "text_empty": (400, "Bad Request: message text is empty"),
    "too_long": (400, "Bad Request: message is too long"),
    "caption_too_long": (400, "Bad Request: message caption is too long"),
    # Textos do dublê (a referência não os documenta):
    "rich_too_long": (400, "Bad Request: rich message is too long"),
    "rich_blocks": (400, "Bad Request: too many blocks in the rich message"),
    "rich_depth": (400, "Bad Request: rich message is nested too deeply"),
    "rich_columns": (400, "Bad Request: too many columns in a table"),
    "rich_empty": (400, "Bad Request: rich message is empty"),
    "placeholder": (400, "Bad Request: input field placeholder must have 1-64 characters"),
    "inline_expected": (400, "Bad Request: inline keyboard expected"),
    "bad_markup": (400, "Bad Request: can't parse reply keyboard markup JSON object"),
    "bad_reply_parameters": (400, "Bad Request: can't parse ReplyParameters JSON object"),
    "bad_rich": (400, "Bad Request: can't parse InputRichMessage JSON object"),
    "photo_type": (400, "Bad Request: IMAGE_PROCESS_FAILED"),
    "file_missing": (400, "Bad Request: there is no file in the request"),
}
PARSE_ERROR_PREFIX = "Bad Request: can't parse entities: "

# ---------------------------------------------------------------------------------------------
# Tags e entidades por modo (Rich e HTML simples, pelas seções de formatação da documentação da Bot API)
# ---------------------------------------------------------------------------------------------

HTML_TAGS: frozenset[str] = frozenset({
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span", "tg-spoiler", "a",
    "tg-emoji", "tg-time", "code", "pre", "blockquote"})
HTML_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href"}), "span": frozenset({"class"}), "code": frozenset({"class"}),
    "blockquote": frozenset({"expandable"}), "tg-emoji": frozenset({"emoji-id"}),
    "tg-time": frozenset({"unix", "format"})}
HTML_ENTITIES: dict[str, str] = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}

RICH_TAGS: frozenset[str] = frozenset({
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "mark", "sub", "sup",
    "tg-spoiler", "tg-reference", "tg-emoji", "img", "tg-time", "tg-math", "h1", "h2", "h3", "h4",
    "h5", "h6", "p", "pre", "footer", "hr", "ul", "ol", "li", "input", "blockquote", "br", "cite",
    "aside", "video", "audio", "tg-document", "figure", "figcaption", "tg-map", "tg-collage",
    "tg-slideshow", "table", "caption", "tr", "th", "td", "details", "summary", "tg-math-block",
    "tg-button", "tg-button-row"})
RICH_VOID: frozenset[str] = frozenset({"br", "hr", "img", "input", "tg-map"})
_CELL_ATTRS = frozenset({"colspan", "rowspan", "align", "valign"})
RICH_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "name"}), "ol": frozenset({"start", "type", "reversed"}),
    "li": frozenset({"value", "type"}), "input": frozenset({"type", "checked"}),
    "blockquote": frozenset({"expandable"}), "img": frozenset({"src", "alt", "tg-spoiler"}),
    "video": frozenset({"src", "tg-spoiler"}), "audio": frozenset({"src"}),
    "tg-document": frozenset({"src"}), "code": frozenset({"class"}),
    "table": frozenset({"bordered", "striped", "compact"}), "td": _CELL_ATTRS, "th": _CELL_ATTRS,
    "details": frozenset({"open"}), "tg-emoji": frozenset({"emoji-id"}),
    "tg-time": frozenset({"unix", "format"}), "tg-reference": frozenset({"name"}),
    "tg-map": frozenset({"lat", "long", "zoom"}),
    "tg-button": frozenset({"type", "style", "url", "data", "forward-text", "request-write-access",
                            "query", "allow-user-chats", "allow-bot-chats", "allow-group-chats",
                            "allow-channel-chats", "text"}),
    "tg-button-row": frozenset({"align"})}
RICH_ENTITIES: dict[str, str] = {
    **HTML_ENTITIES, "apos": "'", "nbsp": " ", "hellip": "…", "mdash": "—",
    "ndash": "–", "lsquo": "‘", "rsquo": "’", "ldquo": "“", "rdquo": "”"}
# Blocos do Rich (Bot API: "including nested blocks, list items, … table rows, quotation blocks, and
# details blocks").
RICH_BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "footer", "hr", "ul", "ol", "li", "blockquote",
    "aside", "figure", "table", "tr", "details", "tg-math-block", "img", "video", "audio",
    "tg-document", "tg-map", "tg-collage", "tg-slideshow", "tg-button-row"})

_FORMATTING = frozenset({"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "span",
                         "tg-spoiler"})
_HTML_EXCLUSIVE = frozenset({"a", "code", "pre", "tg-emoji", "tg-time"})
_RAW_TEXT_TAGS = frozenset({"code", "tg-math", "tg-math-block"})
_LANGUAGE_RE = re.compile(r"language-[A-Za-z0-9_+#-]{1,32}")
_TAG_RE = re.compile(
    r"<(/?)([A-Za-z][A-Za-z0-9-]*)"
    r"((?:\s+[^\s\"'<>/=]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?)*)\s*(/?)>")
_ATTR_RE = re.compile(r"([^\s\"'<>/=]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?")
_ENTITY_RE = re.compile(r"&(?:#([0-9]{1,8})|#[xX]([0-9A-Fa-f]{1,7})|([A-Za-z][A-Za-z0-9]*));")


@dataclass
class Markup:
    """O resultado da leitura de um texto HTML de um modo (`html` ou `rich`)."""

    plain: str = ""
    problems: list[str] = field(default_factory=list)  # só erro de protocolo (sem 400)
    error: str | None = None  # descrição do 400 `can't parse entities: …` (também é protocolo)
    blocks: int = 0
    depth: int = 0
    columns: int = 0


def parse_markup(text: str, mode: str) -> Markup:
    """Lê `text` como o Telegram leria no modo `html` (`parse_mode="HTML"`) ou `rich` (Rich HTML).

    Devolve o texto depois do parsing (tags fora, entidades decodificadas, `<br>` como `\\n`), os
    problemas de protocolo, a descrição do 400 (se o Telegram recusaria) e as medidas do Rich.
    """
    if mode not in ("html", "rich"):
        raise ValueError("mode deve ser html ou rich")
    rich = mode == "rich"
    tags = RICH_TAGS if rich else HTML_TAGS
    attrs_of = RICH_ATTRS if rich else HTML_ATTRS
    entities = RICH_ENTITIES if rich else HTML_ENTITIES
    out = Markup()
    plain: list[str] = []
    stack: list[str] = []
    rows: list[int] = []  # colunas da linha `tr` aberta, por tabela
    top_run = False

    def offset(i: int) -> int:
        return len(text[:i].encode("utf-8"))

    def fail(message: str) -> Markup:
        out.error = PARSE_ERROR_PREFIX + message
        out.plain = "".join(plain)
        return out

    def note_top_inline() -> None:
        nonlocal top_run
        if rich and not stack and not top_run:
            out.blocks += 1
            top_run = True

    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "<":
            match = _TAG_RE.match(text, i)
            if match is None:
                return fail(f'Unexpected "<" at byte offset {offset(i)}')
            closing, name, raw_attrs, self_closing = match.groups()
            name = name.lower()
            if name not in tags:
                kind = "end" if closing else "start"
                return fail(f'Unsupported {kind} tag "{name}" at byte offset {offset(i)}')
            if closing:
                if raw_attrs.strip() or self_closing:
                    return fail(f'Unexpected end tag at byte offset {offset(i)}')
                if not stack:
                    return fail(f'Unmatched end tag at byte offset {offset(i)}, found "</{name}>"')
                if stack[-1] != name:
                    return fail(f'Unmatched end tag at byte offset {offset(i)}, expected '
                                f'"</{stack[-1]}>", found "</{name}>"')
                stack.pop()
                if name == "tr" and rows:
                    out.columns = max(out.columns, rows.pop())
                i = match.end()
                continue
            # tag de abertura
            parent = stack[-1] if stack else None
            if parent in _RAW_TEXT_TAGS:
                return fail(f'Unsupported start tag "{name}" inside "{parent}" at byte offset '
                            f'{offset(i)}')
            if parent == "pre" and name != "code":
                return fail(f'Unsupported start tag "{name}" inside "pre" at byte offset '
                            f'{offset(i)}')
            attrs = {m.group(1).lower(): _attr_value(m.group(2))
                     for m in _ATTR_RE.finditer(raw_attrs)}
            allowed = attrs_of.get(name, frozenset())
            for attr in attrs:
                if attr not in allowed:
                    out.problems.append(f'atributo "{attr}" fora da lista em <{name}>')
            if name == "code" and "class" in attrs:
                if parent != "pre":
                    out.problems.append("linguagem em <code> fora de <pre>")
                elif not _LANGUAGE_RE.fullmatch(attrs["class"] or ""):
                    out.problems.append("class de <code> fora de language-<nome>")
            if not rich and name == "span" and attrs.get("class") != "tg-spoiler":
                return fail(f'Tag "span" must have class "tg-spoiler" at byte offset {offset(i)}')
            if name in ("td", "th"):
                for key in ("colspan", "rowspan"):
                    if key in attrs and not re.fullmatch(r"[1-9][0-9]{0,3}", attrs[key] or ""):
                        out.problems.append(f"{key} fora de inteiro ≥ 1 em <{name}>")
                if rows:
                    span = attrs.get("colspan") or "1"
                    rows[-1] += int(span) if span.isdigit() else 1
            if name == "tr":
                rows.append(0)
            if not rich:
                if name in ("pre", "code") and any(t in _FORMATTING for t in stack):
                    out.problems.append(f"<{name}> dentro de negrito, itálico, sublinhado, "
                                        f"tachado ou spoiler")
                if name == "blockquote" and "blockquote" in stack:
                    out.problems.append("<blockquote> aninhado")
                if (name in _HTML_EXCLUSIVE and not (name == "code" and parent == "pre")
                        and any(t in _HTML_EXCLUSIVE for t in stack)):
                    out.problems.append(f"<{name}> dentro de outra entidade exclusiva")
            if rich:
                if name in RICH_BLOCK_TAGS:
                    out.blocks += 1
                    if not stack:
                        top_run = False
                else:
                    note_top_inline()
            out.depth = max(out.depth, len(stack) + 1)
            if self_closing:
                if not rich:
                    return fail(f'Unexpected self-closing tag "{name}" at byte offset '
                                f'{offset(i)}')
            elif rich and name in RICH_VOID:
                pass
            else:
                stack.append(name)
            if name == "br":
                plain.append("\n")
            i = match.end()
            continue
        if char == "&":
            match = _ENTITY_RE.match(text, i)
            if match is None:
                return fail(f'Unexpected "&" at byte offset {offset(i)}')
            decimal, hexa, named = match.groups()
            if named is not None:
                if named not in entities:
                    return fail(f'Unsupported HTML entity "&{named};" at byte offset {offset(i)}')
                decoded = entities[named]
            else:
                code = int(decimal) if decimal is not None else int(hexa, 16)
                if not 0 < code <= 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                    return fail(f"Invalid numeric HTML entity at byte offset {offset(i)}")
                decoded = chr(code)
            plain.append(decoded)
            note_top_inline()
            i = match.end()
            continue
        start = i
        while i < n and text[i] not in "<&":
            i += 1
        chunk = text[start:i]
        plain.append(chunk)
        if chunk.strip():
            note_top_inline()
    if stack:
        return fail(f'Can\'t find end tag corresponding to start tag "{stack[-1]}"')
    out.plain = "".join(plain)
    return out


def _attr_value(raw: str | None) -> str | None:
    if raw is None:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


# ---------------------------------------------------------------------------------------------
# Tipos públicos
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Button:
    """Um botão inline: `label` é o `text` do botão; `data`, o `callback_data`."""

    label: str
    data: str | None


@dataclass(frozen=True)
class Upload:
    """Um arquivo recebido por `sendPhoto` ou `sendDocument` aceito (os bytes, `repr=False`)."""

    bot_id: int
    chat_id: int
    message_id: int
    method: str
    field: str  # "photo" ou "document"
    filename: str
    content_type: str
    size: int
    sha256: str
    caption: str | None
    call_seq: int
    started: float
    clock_at: datetime
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class RichMessage:
    """Retrato de uma mensagem do bot aceita pela extensão (estado corrente ou histórico).

    `text_format` segue os nomes do sistema: `plain` (`sendMessage` sem `parse_mode`), `html`
    (`parse_mode="HTML"`), `rich_html` (Rich) ou `None` (foto e documento). `text` é o texto ou o
    HTML como foi enviado; `plain_text`, depois do parsing. `keyboard` são as linhas do teclado
    inline corrente (`None` sem teclado inline); `force_reply`/`placeholder` vêm de um `ForceReply`.
    `started`/`clock_at`/`call_seq` são do pedido que criou a mensagem; `edit_count` conta as
    edições aceitas.
    """

    bot_id: int
    chat_id: int
    message_id: int
    method: str
    text_format: str | None
    text: str | None
    plain_text: str | None
    rich_message: dict[str, Any] | None
    parse_mode: str | None
    reply_markup: dict[str, Any] | None
    keyboard: tuple[tuple[Button, ...], ...] | None
    force_reply: bool
    placeholder: str | None
    reply_to_message_id: int | None
    reply_parameters: dict[str, Any] | None
    link_preview_options: dict[str, Any] | None
    caption: str | None
    upload: Upload | None
    deleted: bool
    edit_count: int
    call_seq: int
    started: float
    clock_at: datetime

    def buttons(self) -> list[Button]:
        """Os botões do teclado inline, em ordem de leitura."""
        return [b for row in (self.keyboard or ()) for b in row]


@dataclass(frozen=True)
class Edit:
    """Uma edição aceita (`editMessageText` ou `editMessageReplyMarkup`), com o antes e o depois."""

    bot_id: int
    chat_id: int
    message_id: int
    method: str
    before: RichMessage
    after: RichMessage
    call_seq: int
    started: float
    clock_at: datetime


@dataclass(frozen=True)
class Tap:
    """Um toque emitido por `tap` ou `tap_raw` (o `callback_query` injetado)."""

    bot_id: int
    chat_id: int
    message_id: int
    data: str
    callback_id: str
    update_id: int
    raw: bool
    emitted: float


@dataclass(frozen=True)
class CallbackAnswer:
    """Um `answerCallbackQuery` aceito (o id era emitido, sem resposta anterior e no prazo)."""

    bot_id: int
    callback_id: str
    text: str | None
    show_alert: bool | None
    tap: Tap
    call_seq: int
    started: float
    clock_at: datetime

    @property
    def delay(self) -> float:
        """Segundos (relógio `monotonic()`) entre o toque e a confirmação."""
        return self.started - self.tap.emitted


# ---------------------------------------------------------------------------------------------
# Estado interno
# ---------------------------------------------------------------------------------------------


@dataclass
class _Msg:
    bot_id: int
    chat_id: int
    message_id: int
    method: str
    text_format: str | None
    text: str | None
    plain_text: str | None
    rich_message: dict[str, Any] | None
    parse_mode: str | None
    reply_markup: dict[str, Any] | None
    reply_to_message_id: int | None
    reply_parameters: dict[str, Any] | None
    link_preview_options: dict[str, Any] | None
    caption: str | None
    upload: Upload | None
    call_seq: int
    started: float
    clock_at: datetime
    deleted: bool = False
    edit_count: int = 0

    def snapshot(self) -> RichMessage:
        markup = self.reply_markup
        keyboard: tuple[tuple[Button, ...], ...] | None = None
        force_reply = False
        placeholder: str | None = None
        if isinstance(markup, dict) and isinstance(markup.get("inline_keyboard"), list):
            keyboard = tuple(tuple(Button(b.get("text"), b.get("callback_data")) for b in row)
                             for row in markup["inline_keyboard"])
        elif isinstance(markup, dict) and markup.get("force_reply") is True:
            force_reply = True
            placeholder = markup.get("input_field_placeholder")
        return RichMessage(
            bot_id=self.bot_id, chat_id=self.chat_id, message_id=self.message_id,
            method=self.method, text_format=self.text_format, text=self.text,
            plain_text=self.plain_text, rich_message=copy.deepcopy(self.rich_message),
            parse_mode=self.parse_mode, reply_markup=copy.deepcopy(markup), keyboard=keyboard,
            force_reply=force_reply, placeholder=placeholder,
            reply_to_message_id=self.reply_to_message_id,
            reply_parameters=copy.deepcopy(self.reply_parameters),
            link_preview_options=copy.deepcopy(self.link_preview_options), caption=self.caption,
            upload=self.upload, deleted=self.deleted, edit_count=self.edit_count,
            call_seq=self.call_seq, started=self.started, clock_at=self.clock_at)

    def content_key(self) -> tuple[Any, ...]:
        """O que a edição compara: conteúdo e teclado."""
        return (self.text_format, self.text, json.dumps(self.reply_markup, sort_keys=True))


@dataclass
class _Callback:
    tap: Tap
    answered: bool = False
    expired: bool = False


class _Checker:
    """Acumula os problemas de protocolo, o primeiro 400 e as violações de política de um pedido."""

    def __init__(self) -> None:
        self.problems: list[str] = []
        self.bad: str | None = None
        self.policy: list[str] = []

    def flag(self, problem: str, bad: tuple[int, str] | str | None = None) -> None:
        self.problems.append(problem)
        if bad is not None and self.bad is None:
            self.bad = bad[1] if isinstance(bad, tuple) else bad

    def result(self) -> tuple[list[str], str | None]:
        return self.problems, self.bad


def _json_object(value: Any) -> Any:
    """Os campos "JSON-serialized" chegam como objeto (JSON) ou como texto (multipart)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return _INVALID
    return value


_INVALID: Any = object()


def _as_chat_id(raw: Any) -> int | None:
    if _is_int(raw):
        return raw
    if isinstance(raw, str) and re.fullmatch(r"-?[0-9]{1,19}", raw):
        return int(raw)
    return None


def _is_upload(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"filename", "content_type", "size", "sha256"}


# ---------------------------------------------------------------------------------------------
# A extensão
# ---------------------------------------------------------------------------------------------


class DispatchExtension:
    """A extensão da Bot API falsa para o disparo e a resposta (ver o docstring do módulo).

    Uso: `rich = bot_api.install(DispatchExtension())`; `rich` responde aos atributos do dublê (`add_bot`,
    `token_of`, `fail`, `calls`, `protocol_errors`, `wait_for`…) e aos da extensão (`messages`,
    `current`, `edits`, `answers`, `uploads`, `taps`, `tap`, `tap_raw`, `delete_message`,
    `expire_callback`, `policy_errors`). `callback_timeout` (segundos de `monotonic()`, `None` =
    sem prazo automático) faz o `answerCallbackQuery` tardio receber o 400 `query is too old…`.
    """

    methods: tuple[str, ...] = DISPATCH_METHODS
    handles: frozenset[str] = frozenset(DISPATCH_METHODS) | frozenset(TAKEN_BASE_METHODS)
    upload_methods: frozenset[str] = UPLOAD_METHODS

    def __init__(self, *, callback_timeout: float | None = None) -> None:
        if callback_timeout is not None and callback_timeout <= 0:
            raise ValueError("callback_timeout deve ser positivo ou None")
        self._callback_timeout = callback_timeout
        self._fake: FakeBotApi | None = None
        self._clear()

    def _clear(self) -> None:
        self._messages: dict[tuple[int, int, int], _Msg] = {}
        self._order: list[tuple[int, int, int]] = []
        self._edits: list[Edit] = []
        self._uploads: list[Upload] = []
        self._callbacks: dict[str, _Callback] = {}
        self._taps: list[Tap] = []
        self._answers: list[CallbackAnswer] = []
        self._policy: list[str] = []
        self._blobs: dict[str, bytes] = {}
        self._upload_ids = itertools.count(1)

    # -- ligação ao dublê ----------------------------------------------------------------------

    def bind(self, fake: FakeBotApi) -> None:
        """Chamado pelo `FakeBotApi.install`; começa sem estado."""
        self._fake = fake
        self._clear()

    def __getattr__(self, name: str) -> Any:
        fake = self.__dict__.get("_fake")
        if fake is None or name.startswith("__"):
            raise AttributeError(name)
        return getattr(fake, name)

    @property
    def fake(self) -> FakeBotApi:
        if self._fake is None:
            raise RuntimeError("DispatchExtension não instalada (FakeBotApi.install)")
        return self._fake

    def _live(self) -> bool:
        """A extensão ainda é a instalada (o `reset` do dublê a desfaz)."""
        return self._fake is not None and self._fake._extension is self

    # -- consultas -----------------------------------------------------------------------------

    def policy_errors(self) -> list[str]:
        with self.fake._cond:
            return list(self._policy)

    def messages(self, bot_id: int | None = None, chat_id: int | None = None) -> list[RichMessage]:
        """As mensagens do bot aceitas desde a instalação, na ordem de criação, no estado atual."""
        with self.fake._cond:
            return [self._messages[k].snapshot() for k in self._order
                    if (bot_id is None or k[0] == bot_id) and (chat_id is None or k[1] == chat_id)]

    def current(self, bot_id: int, chat_id: int, message_id: int) -> RichMessage:
        with self.fake._cond:
            try:
                return self._messages[(bot_id, chat_id, message_id)].snapshot()
            except KeyError:
                raise KeyError(f"mensagem {message_id} do bot {bot_id} no chat não existe") from None

    def edits(self, bot_id: int | None = None, chat_id: int | None = None,
              message_id: int | None = None) -> list[Edit]:
        with self.fake._cond:
            return [e for e in self._edits if (bot_id is None or e.bot_id == bot_id)
                    and (chat_id is None or e.chat_id == chat_id)
                    and (message_id is None or e.message_id == message_id)]

    def answers(self, bot_id: int | None = None) -> list[CallbackAnswer]:
        with self.fake._cond:
            return [a for a in self._answers if bot_id is None or a.bot_id == bot_id]

    def uploads(self, bot_id: int | None = None, chat_id: int | None = None) -> list[Upload]:
        with self.fake._cond:
            return [u for u in self._uploads if (bot_id is None or u.bot_id == bot_id)
                    and (chat_id is None or u.chat_id == chat_id)]

    def taps(self) -> list[Tap]:
        with self.fake._cond:
            return list(self._taps)

    # -- ações do teste ------------------------------------------------------------------------

    def delete_message(self, bot_id: int, chat: Any, message_id: int) -> None:
        """O aluno apaga a mensagem: as edições seguintes recebem 400 `message to edit not found`,
        e uma resposta a ela sem `allow_sending_without_reply`, 400."""
        with self.fake._cond:
            key = (bot_id, _chat_id_of(chat), message_id)
            if key not in self._messages:
                raise KeyError("mensagem desconhecida da extensão")
            self._messages[key].deleted = True

    def expire_callback(self, callback_id: str) -> None:
        """O prazo do toque venceu: o `answerCallbackQuery` dele recebe 400 `query is too old…`."""
        with self.fake._cond:
            if callback_id not in self._callbacks:
                raise KeyError("callback_query_id não emitido pela extensão")
            self._callbacks[callback_id].expired = True

    def tap(self, bot_id: int, sender: Any, message_id: int, data: str) -> Tap:
        """Toque de `sender` num botão **do teclado corrente** da mensagem `message_id` (no chat
        privado dele); `AssertionError` se a mensagem não existe, foi apagada ou não tem esse
        botão agora."""
        chat_id = _chat_id_of(sender)
        with self.fake._cond:
            msg = self._messages.get((bot_id, chat_id, message_id))
            if msg is None or msg.deleted:
                raise AssertionError(f"tap: a mensagem {message_id} não existe (ou foi apagada)")
            current = [b.data for b in msg.snapshot().buttons()]
            if data not in current:
                raise AssertionError(f"tap: {data!r} fora do teclado corrente da mensagem "
                                     f"{message_id}: {current!r}")
        return self._emit(bot_id, sender, message_id, data, raw=False)

    def tap_raw(self, bot_id: int, sender: Any, message_id: int, data: str) -> Tap:
        """Toque com um dado qualquer (obsoleto, forjado, de outra mensagem ou fora de `cq1:`),
        pelo `push_callback`; o id fica registrado como emitido."""
        return self._emit(bot_id, sender, message_id, data, raw=True)

    def _emit(self, bot_id: int, sender: Any, message_id: int, data: str, *, raw: bool) -> Tap:
        fake = self.fake
        update_id = fake.push_callback(bot_id, sender, data, message_id=message_id)
        if update_id is None:
            raise ValueError("callback_query fora do allowed_updates do bot: nada foi emitido")
        with fake._cond:
            query = fake._bot(bot_id).history[update_id]["callback_query"]
            tap = Tap(bot_id=bot_id, chat_id=query["message"]["chat"]["id"],
                      message_id=message_id, data=data, callback_id=query["id"],
                      update_id=update_id, raw=raw, emitted=monotonic())
            self._callbacks[tap.callback_id] = _Callback(tap)
            self._taps.append(tap)
            fake._cond.notify_all()
            return tap

    # -- gancho: parâmetros --------------------------------------------------------------------

    def check_params(self, method: str, params: Mapping[str, Any]) -> tuple[list[str],
                                                                            str | None] | None:
        """Chamado pelo dublê (sob o lock) na chegada do pedido; `None` = regra do dublê base."""
        if method == "answerCallbackQuery":
            return None
        check = _Checker()
        documented = DISPATCH_PARAMS.get(method) or _PARAMS[method]
        for name in params:
            if name not in documented:
                check.flag(f"parâmetro não documentado: {name!r}")
        if method in ("sendMessage", "sendRichMessage", "sendPhoto", "sendDocument"):
            self._check_chat(check, params)
            self._check_reply_parameters(check, params)
            self._check_reply_markup(check, params, edit=False)
        if method == "sendMessage":
            self._check_text(check, params, required=True)
            self._check_link_preview(check, params)
        elif method == "sendRichMessage":
            if "rich_message" not in params:
                check.flag("parâmetro obrigatório ausente: 'rich_message'",
                           DISPATCH_ERRORS["bad_rich"])
            else:
                self._check_rich(check, params["rich_message"])
        elif method in ("sendPhoto", "sendDocument"):
            self._check_file(check, method, params)
        elif method in ("editMessageText", "editMessageReplyMarkup"):
            self._check_target(check, params)
            self._check_reply_markup(check, params, edit=True)
            if method == "editMessageText":
                has_text, has_rich = "text" in params, "rich_message" in params
                if has_text and has_rich:
                    check.flag("text e rich_message juntos", PARSE_ERROR_PREFIX +
                               "text and rich_message can't be used together")
                elif has_rich:
                    self._check_rich(check, params["rich_message"])
                    for name in ("parse_mode", "link_preview_options", "entities"):
                        if name in params:
                            check.flag(f"{name} junto com rich_message")
                else:
                    self._check_text(check, params, required=True)
                    self._check_link_preview(check, params)
        if check.policy:
            self._policy.extend(f"{method}: {p}" for p in check.policy)
        return check.result()

    @staticmethod
    def _check_chat(check: _Checker, params: Mapping[str, Any]) -> None:
        if "chat_id" not in params:
            check.flag("parâmetro obrigatório ausente: 'chat_id'", "Bad Request: chat_id is empty")
        elif not (_is_int(params["chat_id"]) or isinstance(params["chat_id"], str)):
            check.flag("chat_id não é inteiro nem texto", ERRORS["chat_not_found"])

    @staticmethod
    def _check_target(check: _Checker, params: Mapping[str, Any]) -> None:
        if "inline_message_id" in params:
            check.flag("inline_message_id (o sistema edita pela mensagem do chat)")
        DispatchExtension._check_chat(check, params)
        if not _is_int(params.get("message_id")):
            check.flag("message_id ausente ou não é inteiro", DISPATCH_ERRORS["edit_not_found"])

    @staticmethod
    def _check_text(check: _Checker, params: Mapping[str, Any], *, required: bool) -> None:
        if "text" not in params:
            if required:
                check.flag("parâmetro obrigatório ausente: 'text'", DISPATCH_ERRORS["text_empty"])
            return
        text = params["text"]
        if not isinstance(text, str):
            check.flag("text não é texto", DISPATCH_ERRORS["text_empty"])
            return
        parse_mode = params.get("parse_mode")
        suffix = ""
        if parse_mode is None:
            plain = text  # sem parse_mode: as mesmas regras e os mesmos textos do dublê base
        elif parse_mode != "HTML":
            check.flag(f"parse_mode {parse_mode!r} (o sistema só usa HTML)")
            plain = text
        else:
            markup = parse_markup(text, "html")
            for problem in markup.problems:
                check.flag(f"text: {problem}")
            if markup.error is not None:
                check.flag(f"text: {markup.error}", markup.error)
                return
            plain = markup.plain
            suffix = " depois do parsing"
        if _utf16_len(plain) == 0:
            check.flag("text vazio" + suffix, DISPATCH_ERRORS["text_empty"])
        elif _utf16_len(plain) > TEXT_MAX:
            check.flag("text com mais de 4096 unidades UTF-16" + suffix, DISPATCH_ERRORS["too_long"])

    @staticmethod
    def _check_link_preview(check: _Checker, params: Mapping[str, Any]) -> None:
        if "link_preview_options" not in params:
            return
        value = _json_object(params["link_preview_options"])
        keys = {"is_disabled", "url", "prefer_small_media", "prefer_large_media",
                "show_above_text"}
        if not isinstance(value, dict) or not set(value) <= keys:
            check.flag("link_preview_options não é um LinkPreviewOptions")
        elif "is_disabled" in value and not isinstance(value["is_disabled"], bool):
            check.flag("link_preview_options.is_disabled não é booleano")

    @staticmethod
    def _check_rich(check: _Checker, raw: Any) -> None:
        rich = _json_object(raw)
        if not isinstance(rich, dict):
            check.flag("rich_message não é um InputRichMessage", DISPATCH_ERRORS["bad_rich"])
            return
        keys = {"html", "markdown", "blocks", "media", "is_rtl", "skip_entity_detection"}
        for key in rich:
            if key not in keys:
                check.flag(f"rich_message com campo não documentado: {key!r}")
        bodies = [k for k in ("html", "markdown", "blocks") if k in rich]
        if len(bodies) != 1:
            check.flag("rich_message sem exatamente um de html, markdown ou blocks",
                       DISPATCH_ERRORS["bad_rich"])
            return
        if bodies != ["html"]:
            check.flag(f"rich_message com {bodies[0]} (o sistema só usa html)")
            return
        if rich.get("skip_entity_detection") is not True:
            check.policy.append("Rich sem skip_entity_detection: true")
        if "media" in rich:
            check.flag("rich_message com media (as mídias saem em mensagens próprias)")
        html = rich["html"]
        if not isinstance(html, str):
            check.flag("rich_message.html não é texto", DISPATCH_ERRORS["bad_rich"])
            return
        markup = parse_markup(html, "rich")
        for problem in markup.problems:
            check.flag(f"rich_message.html: {problem}")
        if markup.error is not None:
            check.flag(f"rich_message.html: {markup.error}", markup.error)
            return
        if not markup.plain.strip() and markup.blocks == 0:
            check.flag("rich_message.html vazio", DISPATCH_ERRORS["rich_empty"])
        if _utf16_len(markup.plain) > RICH_TEXT_MAX:
            check.flag("Rich com mais de 32768 unidades UTF-16 depois do parsing",
                       DISPATCH_ERRORS["rich_too_long"])
        if markup.blocks > RICH_BLOCKS_MAX:
            check.flag(f"Rich com {markup.blocks} blocos (máximo 500)", DISPATCH_ERRORS["rich_blocks"])
        if markup.depth > RICH_DEPTH_MAX:
            check.flag(f"Rich com {markup.depth} níveis (máximo 16)", DISPATCH_ERRORS["rich_depth"])
        if markup.columns > RICH_COLUMNS_MAX:
            check.flag(f"tabela com {markup.columns} colunas (máximo 20)",
                       DISPATCH_ERRORS["rich_columns"])

    @staticmethod
    def _check_reply_parameters(check: _Checker, params: Mapping[str, Any]) -> None:
        if "reply_parameters" not in params:
            return
        value = _json_object(params["reply_parameters"])
        keys = {"message_id", "chat_id", "allow_sending_without_reply", "quote",
                "quote_parse_mode", "quote_entities", "quote_position", "checklist_task_id"}
        if not isinstance(value, dict) or not _is_int(value.get("message_id")):
            check.flag("reply_parameters não é um ReplyParameters com message_id",
                       DISPATCH_ERRORS["bad_reply_parameters"])
            return
        for key in value:
            if key not in keys:
                check.flag(f"reply_parameters com campo não documentado: {key!r}")
        allow = value.get("allow_sending_without_reply")
        if "allow_sending_without_reply" in value and not isinstance(allow, bool):
            check.flag("reply_parameters.allow_sending_without_reply não é booleano")
        if allow is not True:
            check.policy.append("reply_parameters sem allow_sending_without_reply: true")

    @staticmethod
    def _check_reply_markup(check: _Checker, params: Mapping[str, Any], *, edit: bool) -> None:
        if "reply_markup" not in params:
            return
        markup = _json_object(params["reply_markup"])
        if not isinstance(markup, dict):
            check.flag("reply_markup não é um objeto", DISPATCH_ERRORS["bad_markup"])
            return
        if "inline_keyboard" in markup:
            _check_inline_keyboard(check, markup)
            return
        if edit:
            check.flag("reply_markup de edição que não é inline",
                       DISPATCH_ERRORS["inline_expected"])
            return
        if "force_reply" in markup:
            for key in markup:
                if key not in ("force_reply", "input_field_placeholder", "selective"):
                    check.flag(f"ForceReply com campo não documentado: {key!r}")
            if markup["force_reply"] is not True:
                check.flag("ForceReply.force_reply diferente de true", DISPATCH_ERRORS["bad_markup"])
            if "input_field_placeholder" in markup:
                hint = markup["input_field_placeholder"]
                if not isinstance(hint, str) or not 1 <= _utf16_len(hint) <= PLACEHOLDER_MAX:
                    check.flag("input_field_placeholder fora de 1 a 64", DISPATCH_ERRORS["placeholder"])
            return
        if "keyboard" in markup or "remove_keyboard" in markup:
            check.flag("teclado de resposta (ReplyKeyboardMarkup/Remove) fora do uso do sistema")
            return
        check.flag("reply_markup sem tipo conhecido", DISPATCH_ERRORS["bad_markup"])

    def _check_file(self, check: _Checker, method: str, params: Mapping[str, Any]) -> None:
        name = "photo" if method == "sendPhoto" else "document"
        value = params.get(name)
        if value is None:
            check.flag(f"parâmetro obrigatório ausente: {name!r}", DISPATCH_ERRORS["file_missing"])
        elif not _is_upload(value):
            check.flag(f"{name} não veio como arquivo em multipart (o sistema sempre envia os "
                       f"bytes)", DISPATCH_ERRORS["file_missing"])
        else:
            if method == "sendPhoto":
                if value["content_type"] not in PHOTO_TYPES:
                    check.flag(f"foto em {value['content_type']} (só JPEG, PNG ou WebP)",
                               DISPATCH_ERRORS["photo_type"])
                if value["size"] > PHOTO_MAX_BYTES:
                    check.flag("foto com mais de 10000000 bytes", DISPATCH_ERRORS["file_too_big"])
            elif value["size"] > DOCUMENT_MAX_BYTES:
                check.flag("documento com mais de 50000000 bytes", DISPATCH_ERRORS["file_too_big"])
        if "caption" in params:
            caption = params["caption"]
            if not isinstance(caption, str):
                check.flag("caption não é texto")
            else:
                plain = caption
                if params.get("parse_mode") == "HTML":
                    markup = parse_markup(caption, "html")
                    for problem in markup.problems:
                        check.flag(f"caption: {problem}")
                    if markup.error is not None:
                        check.flag(f"caption: {markup.error}", markup.error)
                    plain = markup.plain
                elif "parse_mode" in params:
                    check.flag(f"parse_mode {params['parse_mode']!r} (o sistema só usa HTML)")
                if _utf16_len(plain) > CAPTION_MAX:
                    check.flag("caption com mais de 1024 unidades UTF-16",
                               DISPATCH_ERRORS["caption_too_long"])

    # -- gancho: multipart ---------------------------------------------------------------------

    def parse_multipart(self, method: str, content_type: str,
                        body: bytes) -> tuple[dict[str, Any], list[str]]:
        """Lê o corpo multipart (`email.parser`): campos de texto em UTF-8 e arquivos como
        `{"filename", "content_type", "size", "sha256"}` (os bytes ficam com a extensão)."""
        problems: list[str] = []
        try:
            header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
            message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                header.encode("latin-1") + body)
        except Exception as exc:  # noqa: BLE001 - vira erro de protocolo
            return {}, [f"multipart ilegível: {type(exc).__name__}"]
        if not message.is_multipart() or message.defects:
            return {}, ["multipart ilegível"]
        params: dict[str, Any] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not isinstance(name, str) or not name:
                problems.append("parte do multipart sem nome")
                continue
            if name in params:
                problems.append(f"parte repetida no multipart: {name!r}")
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is None:
                try:
                    params[name] = payload.decode("utf-8")
                except UnicodeDecodeError:
                    problems.append(f"parte {name!r} fora de UTF-8")
                continue
            digest = hashlib.sha256(payload).hexdigest()
            params[name] = {"filename": filename, "content_type": part.get_content_type(),
                            "size": len(payload), "sha256": digest}
            with self.fake._cond:
                self._blobs[digest] = bytes(payload)
        return params, problems

    # -- gancho: atendimento -------------------------------------------------------------------

    def handle(self, method: str, call: Any, bot_id: int, params: dict[str, Any]) -> ExtensionReply:
        """Chamado pelo dublê (fora do lock) com o pedido já conferido."""
        if method == "answerCallbackQuery":
            return self._answer(call, bot_id, params)
        if method in ("editMessageText", "editMessageReplyMarkup"):
            return self._edit(method, call, bot_id, params)
        return self._send(method, call, bot_id, params)

    def _chat_error(self, bot: Any, chat_id: int | None) -> tuple[int, str] | None:
        if chat_id is not None and chat_id in bot.blocked:
            return ERRORS["user_deactivated" if bot.blocked[chat_id] == "deactivated"
                          else "blocked"]
        if chat_id is None or chat_id not in bot.known_chats:
            return ERRORS["chat_not_found"]
        return None

    def _message_exists(self, bot: Any, chat_id: int, message_id: int) -> bool:
        msg = self._messages.get((bot.bot_id, chat_id, message_id))
        if msg is not None:
            return not msg.deleted
        return 0 < message_id <= bot.message_ids.get(chat_id, 0)

    def _send(self, method: str, call: Any, bot_id: int, params: dict[str, Any]) -> ExtensionReply:
        fake = self.fake
        chat_id = _as_chat_id(params.get("chat_id"))
        reply_parameters = _json_object(params.get("reply_parameters"))
        if not isinstance(reply_parameters, dict):
            reply_parameters = None
        markup = _json_object(params.get("reply_markup"))
        if not isinstance(markup, dict):
            markup = None
        with fake._cond:
            bot = fake._bot(bot_id)
            error = self._chat_error(bot, chat_id)
            if error is not None:
                return ExtensionReply(error=error)
            assert chat_id is not None
            reply_to: int | None = None
            if reply_parameters is not None:
                target = reply_parameters["message_id"]
                if self._message_exists(bot, chat_id, target):
                    reply_to = target
                elif reply_parameters.get("allow_sending_without_reply") is not True:
                    return ExtensionReply(error=DISPATCH_ERRORS["reply_not_found"])
            message_id = fake._next_message_id(bot, chat_id)
            result: dict[str, Any] = {
                "message_id": message_id, "from": bot.user(),
                "chat": {"id": chat_id, "type": bot.known_chats[chat_id]},
                "date": fake._date(None)}
            text: str | None = None
            plain: str | None = None
            rich_message: dict[str, Any] | None = None
            parse_mode: str | None = None
            text_format: str | None = None
            caption: str | None = None
            upload: Upload | None = None
            if method == "sendMessage":
                text = params["text"]
                parse_mode = params.get("parse_mode")
                text_format = "html" if parse_mode == "HTML" else "plain"
                plain = parse_markup(text, "html").plain if parse_mode == "HTML" else text
                result["text"] = plain
                if self._live():
                    fake._sent.append(SentMessage(bot_id, chat_id, message_id, text,
                                                  call.clock_at, call.seq))
            elif method == "sendRichMessage":
                rich_message = copy.deepcopy(_json_object(params["rich_message"]))
                html = rich_message.get("html") if isinstance(rich_message, dict) else None
                text = html if isinstance(html, str) else None
                plain = parse_markup(text, "rich").plain if text is not None else None
                text_format = "rich_html"
            else:
                name = "photo" if method == "sendPhoto" else "document"
                info = params[name]
                caption = params.get("caption")
                upload = Upload(
                    bot_id=bot_id, chat_id=chat_id, message_id=message_id, method=method,
                    field=name, filename=info["filename"], content_type=info["content_type"],
                    size=info["size"], sha256=info["sha256"], caption=caption,
                    call_seq=call.seq, started=call.started, clock_at=call.clock_at,
                    data=self._blobs.get(info["sha256"], b""))
                file_id = f"cq-fake-{name}-{next(self._upload_ids)}"
                if name == "photo":
                    result["photo"] = [{"file_id": file_id, "file_unique_id": file_id,
                                        "width": 1, "height": 1, "file_size": info["size"]}]
                else:
                    result["document"] = {"file_id": file_id, "file_unique_id": file_id,
                                          "file_name": info["filename"],
                                          "mime_type": info["content_type"],
                                          "file_size": info["size"]}
                if caption is not None:
                    result["caption"] = caption
                if self._live():
                    self._uploads.append(upload)
            if reply_to is not None:
                result["reply_to_message"] = {"message_id": reply_to}
            if markup is not None and "inline_keyboard" in markup:
                result["reply_markup"] = copy.deepcopy(markup)
            link = _json_object(params.get("link_preview_options"))
            msg = _Msg(
                bot_id=bot_id, chat_id=chat_id, message_id=message_id, method=method,
                text_format=text_format, text=text, plain_text=plain, rich_message=rich_message,
                parse_mode=parse_mode, reply_markup=copy.deepcopy(markup),
                reply_to_message_id=reply_to, reply_parameters=copy.deepcopy(reply_parameters),
                link_preview_options=copy.deepcopy(link) if isinstance(link, dict) else None,
                caption=caption, upload=upload, call_seq=call.seq, started=call.started,
                clock_at=call.clock_at)
            if self._live():
                key = (bot_id, chat_id, message_id)
                self._messages[key] = msg
                self._order.append(key)
            fake._cond.notify_all()
        return ExtensionReply(result=result)

    def _edit(self, method: str, call: Any, bot_id: int, params: dict[str, Any]) -> ExtensionReply:
        fake = self.fake
        chat_id = _as_chat_id(params.get("chat_id"))
        message_id = _as_chat_id(params.get("message_id"))
        markup = _json_object(params.get("reply_markup"))
        new_markup = copy.deepcopy(markup) if isinstance(markup, dict) else None
        with fake._cond:
            bot = fake._bot(bot_id)
            error = self._chat_error(bot, chat_id)
            if error is not None:
                return ExtensionReply(error=error)
            assert chat_id is not None and message_id is not None
            msg = self._messages.get((bot_id, chat_id, message_id))
            if msg is None:
                known = 0 < message_id <= bot.message_ids.get(chat_id, 0)
                return ExtensionReply(error=DISPATCH_ERRORS["cant_be_edited" if known
                                                      else "edit_not_found"])
            if msg.deleted:
                return ExtensionReply(error=DISPATCH_ERRORS["edit_not_found"])
            before = msg.snapshot()
            old_key = msg.content_key()
            changes: dict[str, Any] = {"reply_markup": new_markup}
            if method == "editMessageText":
                if msg.text_format is None:
                    return ExtensionReply(error=DISPATCH_ERRORS["no_text"])
                if "rich_message" in params:
                    rich = copy.deepcopy(_json_object(params["rich_message"]))
                    html = rich.get("html") if isinstance(rich, dict) else None
                    html = html if isinstance(html, str) else None
                    changes.update(text_format="rich_html", text=html,
                                   plain_text=(parse_markup(html, "rich").plain
                                               if html is not None else None),
                                   rich_message=rich, parse_mode=None,
                                   link_preview_options=None)
                else:
                    parse_mode = params.get("parse_mode")
                    text = params["text"]
                    link = _json_object(params.get("link_preview_options"))
                    changes.update(
                        text_format="html" if parse_mode == "HTML" else "plain", text=text,
                        plain_text=(parse_markup(text, "html").plain if parse_mode == "HTML"
                                    else text),
                        rich_message=None, parse_mode=parse_mode,
                        link_preview_options=copy.deepcopy(link) if isinstance(link, dict)
                        else None)
            candidate = copy.copy(msg)
            for name, value in changes.items():
                setattr(candidate, name, value)
            if candidate.content_key() == old_key:
                return ExtensionReply(error=DISPATCH_ERRORS["not_modified"])
            if self._live():
                for name, value in changes.items():
                    setattr(msg, name, value)
                msg.edit_count += 1
                self._edits.append(Edit(bot_id=bot_id, chat_id=chat_id, message_id=message_id,
                                        method=method, before=before, after=msg.snapshot(),
                                        call_seq=call.seq, started=call.started,
                                        clock_at=call.clock_at))
            result: dict[str, Any] = {
                "message_id": message_id, "from": bot.user(),
                "chat": {"id": chat_id, "type": bot.known_chats[chat_id]},
                "date": fake._date(None), "edit_date": fake._date(None)}
            if candidate.text_format in ("plain", "html"):
                result["text"] = candidate.plain_text
            if isinstance(new_markup, dict) and "inline_keyboard" in new_markup:
                result["reply_markup"] = copy.deepcopy(new_markup)
            fake._cond.notify_all()
        return ExtensionReply(result=result)

    def _answer(self, call: Any, bot_id: int, params: dict[str, Any]) -> ExtensionReply:
        fake = self.fake
        with fake._cond:
            entry = self._callbacks.get(params["callback_query_id"])
            if (entry is None or entry.answered or entry.expired or entry.tap.bot_id != bot_id
                    or (self._callback_timeout is not None
                        and call.started - entry.tap.emitted > self._callback_timeout)):
                return ExtensionReply(error=DISPATCH_ERRORS["query_too_old"])
            if self._live():
                entry.answered = True
                self._answers.append(CallbackAnswer(
                    bot_id=bot_id, callback_id=entry.tap.callback_id, text=params.get("text"),
                    show_alert=params.get("show_alert"), tap=entry.tap, call_seq=call.seq,
                    started=call.started, clock_at=call.clock_at))
            fake._cond.notify_all()
        return ExtensionReply(result=True)


def _check_inline_keyboard(check: _Checker, markup: Mapping[str, Any]) -> None:
    """`InlineKeyboardMarkup`: só botões com `callback_data` de 1 a 64 bytes UTF-8."""
    for key in markup:
        if key == "force_reply":
            check.flag("InlineKeyboardMarkup.force_reply (a questão usa o "
                       "pedido de campo)")
        elif key != "inline_keyboard":
            check.flag(f"InlineKeyboardMarkup com campo não documentado: {key!r}")
    rows = markup["inline_keyboard"]
    if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
        check.flag("inline_keyboard não é uma lista de linhas", DISPATCH_ERRORS["bad_markup"])
        return
    button_keys = {"text", "callback_data", "style", "icon_custom_emoji_id", "url",
                   "login_url", "web_app", "switch_inline_query",
                   "switch_inline_query_current_chat", "switch_inline_query_chosen_chat",
                   "copy_text", "callback_game", "pay"}
    for r, row in enumerate(rows):
        for c, button in enumerate(row):
            where = f"inline_keyboard[{r}][{c}]"
            if not isinstance(button, dict):
                check.flag(f"{where} não é um objeto", DISPATCH_ERRORS["bad_markup"])
                continue
            for key in button:
                if key not in button_keys:
                    check.flag(f"{where} com campo não documentado: {key!r}")
                elif key not in ("text", "callback_data", "style"):
                    check.flag(f"{where} com {key} (o sistema só usa callback_data)")
            label = button.get("text")
            if not isinstance(label, str) or not label:
                check.flag(f"{where}.text vazio ou não é texto", DISPATCH_ERRORS["bad_markup"])
            if "style" in button and button["style"] not in ("danger", "success", "primary"):
                check.flag(f"{where}.style fora de danger, success e primary")
            data = button.get("callback_data")
            if not isinstance(data, str):
                check.flag(f"{where} sem callback_data", DISPATCH_ERRORS["button_data_invalid"])
            elif not 1 <= len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES:
                check.flag(f"{where}.callback_data fora de 1 a 64 bytes UTF-8",
                           DISPATCH_ERRORS["button_data_invalid"])
