"""Canal de console no disparo e na resposta: linha estendida, formatador e entrada simulada do aluno



- **Linha do `ConsoleChannel`**: toda mensagem **sem campo novo**, com rota (`BotRoute`) ou sem rota,
  continua exatamente `{channel_ref, message_id, recipient, text}`, com `channel_ref` `console-<32 hex>`
  (como em `test_outbox_routes.py:785-825`); com **algum** campo novo (`keyboard`,
  `reply_request`, `reply_to_ref`, `edit_ref`, `markup_only`, `media` ou só `answerable = True`), a
  linha ganha só as chaves dos campos presentes (`reply_to`, `reply_request`, `edit_of`, `media` sem
  os bytes) e `channel_ref = str(message_id)`; nunca uma chave `chat` nem o `chat_id` da rota (pelo
  princípio da identidade só pela revelação). O `reply_to` e o `edit_of` levam a referência recebida; o `reply_request` mostra a
  dica (o console não tem a interface do `ForceReply`); o `media` mostra o arquivo, sem os bytes.
- **Canal sem edição**: com `can_edit = False`, a edição de uma entrega chega ao
  console como uma linha **nova** (papel `fallback`, com `reply_to` para a mensagem original); nenhuma
  linha escrita muda.
- **Formatador**: `ConsoleChannel.formatter` e `formatter_for("rich" | "html" | "plain")` são o
  `PlainFormatter` (de `cq_server.telegramrender`, reexportado por `adapters.console`); outro
  dialeto → `ValueError`. O `PlainFormatter.escape` não escapa nada (texto puro), conta em unidades
  UTF-16 e marca `fits = False` acima de 4 096.
- **`ConsoleInbox`**: `send_text`, `choose` (a escolha degradada: o texto da chave em resposta
  à questão) e `dont_know` (o texto `/naosei`) gravam por `reception.store_console_updates` a
  linha literal de `telegram_updates` (`kind = message`, `chat_type = private`, `sender_id` = o chat
  privado, `text`, `command` pelo mesmo critério da recepção do Telegram, `reply_to_message_id` = o número citado ou
  nulo, `update_id < 0`, `state = pending`) e a tarefa `telegram.update` dela, na mesma transação,
  como o `store_updates` da recepção do Telegram (`telegram.update:<id>`).

Os `update_id` gerados nunca são literais esperados: só o sinal, a distinção e a ordem. No esqueleto,
a linha com campo novo, o formatador e a `ConsoleInbox` lançam `NotImplementedError`; a linha original
(real no esqueleto) é afirmada na mesma função de um caso com campo novo.
"""

from __future__ import annotations

import base64
import io
import itertools
import json
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from cq_server import telegramrender
from cq_server.adapters import console as console_adapter
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox, PlainFormatter
from cq_server.ports.delivery import (
    BotRoute,
    Formatted,
    InlineButton,
    MediaPayload,
    OutgoingMessage,
    ReplyRequest,
    Sent,
)
from tests.helpers.clock import FakeClock
from tests.helpers.console_student import PLAIN_KEYS, ConsoleStudent, read_lines

PLAIN_REF = re.compile(r"^console-[0-9a-f]{32}$")
# O chat privado do aluno: um número que não coincide com nenhum `message_id` dos testes.
CHAT_ID = 5_550_123_456_789
QUESTION_REF = "900001"
PLACEHOLDER = "Número; ex.: 4.25, -17 ou 3/7"
MEDIA_BYTES = b"CQ-MEDIA-BYTES-SENTINEL-4F2A\x89PNG\r\n"
MEDIA = MediaPayload(media_id="m1", filename="figura1.png", content_type="image/png", as_document=False,
                     caption="Figura 1", data=MEDIA_BYTES)
KEYBOARD = ((InlineButton("(a)", "cq1:AAAAAAAAAAAAAAAAAAAAAA:c:0:1:a"),
             InlineButton("Não sei", "cq1:AAAAAAAAAAAAAAAAAAAAAA:n:0:1")),)

# Campo novo → (argumentos da mensagem, chaves novas da linha e o valor de cada uma, quando é a
# referência recebida).
RICH_FIELDS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "keyboard": ({"keyboard": KEYBOARD}, {}),
    "reply_request": ({"reply_request": ReplyRequest(PLACEHOLDER), "reply_to_ref": QUESTION_REF},
                      {"reply_request": ..., "reply_to": QUESTION_REF}),
    "reply_to_ref": ({"reply_to_ref": QUESTION_REF}, {"reply_to": QUESTION_REF}),
    "edit_ref": ({"edit_ref": QUESTION_REF}, {"edit_of": QUESTION_REF}),
    "markup_only": ({"markup_only": True}, {}),
    "media": ({"media": MEDIA}, {"media": ...}),
    "answerable": ({"answerable": True}, {}),
}

_codes = itertools.count(1)


def _route() -> BotRoute:
    return BotRoute(bot_id=7, token_version=1, chat_id=CHAT_ID)


def _only_line(stream: io.StringIO, start: int) -> dict[str, Any]:
    written = stream.getvalue()[start:]
    assert written.endswith("\n") and written.count("\n") == 1, repr(written)
    return json.loads(written)


def _assert_no_chat(output: str) -> None:
    """O `chat_id` da rota e uma chave `chat` nunca saem na linha."""
    assert str(CHAT_ID) not in output
    for line in output.splitlines():
        assert not any("chat" in key for key in json.loads(line)), line


@pytest.mark.parametrize("field", list(RICH_FIELDS))
def test_line_without_rich_field_is_plain_and_with_rich_field_gains_only_its_keys(field: str) -> None:
    """(a) Sem campo novo, com rota ou sem rota, a linha é exatamente a original
    (`console-<32 hex>`, 4 chaves) e o `Sent` traz a mesma referência; (b) com o campo novo `field`
    (e com rota), a linha tem as 4 chaves originais mais só as chaves do campo, `channel_ref =
    str(message_id)` (o mesmo do `Sent`), o `reply_to`/`edit_of` com a referência recebida, a dica no
    `reply_request`, o arquivo sem os bytes no `media`; (c) o `chat_id` da rota e uma chave `chat`
    nunca aparecem."""
    stream = io.StringIO()
    channel = ConsoleChannel(stream)
    recipient = uuid.uuid4()
    for message_id, route in ((101, None), (102, _route())):
        message = OutgoingMessage(message_id=message_id, recipient=recipient, text=f"texto {message_id}",
                                  route=route)
        start = len(stream.getvalue())
        result = channel.send(message)
        assert type(result) is Sent and PLAIN_REF.fullmatch(result.channel_ref), result
        assert _only_line(stream, start) == {"channel_ref": result.channel_ref, "message_id": message_id,
                                             "recipient": str(recipient), "text": f"texto {message_id}"}

    kwargs, extra = RICH_FIELDS[field]
    message = OutgoingMessage(message_id=103, recipient=recipient, text="Questão 1\n(a) Dois",
                              route=_route(), **kwargs)
    start = len(stream.getvalue())
    result = channel.send(message)
    assert result == Sent("103")
    line = _only_line(stream, start)
    assert set(line) == PLAIN_KEYS | set(extra)
    assert {key: line[key] for key in PLAIN_KEYS} == {"channel_ref": "103", "message_id": 103,
                                                  "recipient": str(recipient),
                                                  "text": "Questão 1\n(a) Dois"}
    for key, value in extra.items():
        if value is not ...:
            assert line[key] == value, key
    if "reply_request" in extra:
        assert PLACEHOLDER in json.dumps(line["reply_request"], ensure_ascii=False)
    if "media" in extra:
        shown = json.dumps(line["media"], ensure_ascii=False)
        assert "figura1.png" in shown
    output = stream.getvalue()
    assert "CQ-MEDIA-BYTES-SENTINEL" not in output
    assert base64.b64encode(MEDIA_BYTES).decode("ascii") not in output
    assert MEDIA_BYTES.hex() not in output
    _assert_no_chat(output)
    assert [ln.channel_ref for ln in read_lines(output)][2] == "103"


def test_edit_on_console_arrives_as_a_new_line_with_reply_to() -> None:
    """o console não edita (`can_edit = False`): a edição de uma
    entrega chega como mensagem nova de papel `fallback`, em resposta à original (`reply_to` = a
    referência da questão), numa linha nova; uma mensagem com `edit_ref` também só acrescenta uma
    linha (`edit_of`); as linhas anteriores não mudam, e o aluno simulado as acha pelo `recipient`."""
    stream = io.StringIO()
    channel = ConsoleChannel(stream)
    recipient = uuid.uuid4()
    assert channel.capabilities.can_edit is False
    question = OutgoingMessage(message_id=201, recipient=recipient, text="Questão\n(a) Dois\n(b) Nove",
                               route=_route(), answerable=True)
    assert channel.send(question) == Sent("201")
    first = stream.getvalue()
    fallback = OutgoingMessage(message_id=202, recipient=recipient,
                               text="Questão\n(a) Dois\n(b) Nove\nSua resposta: b · ❌ incorreta",
                               route=_route(), reply_to_ref="201", answerable=True)
    assert channel.send(fallback) == Sent("202")
    edit = OutgoingMessage(message_id=203, recipient=recipient, text="Questão\nJanela fechada.",
                           route=_route(), edit_ref="201", answerable=True)
    assert channel.send(edit) == Sent("203")
    output = stream.getvalue()
    assert output.startswith(first)
    # Só leitura: o aluno simulado não escreve aqui, então nenhuma `ConsoleInbox` é necessária.
    student = ConsoleStudent(object(), bot_id=1, chat_id=CHAT_ID,  # type: ignore[arg-type]
                             recipient=recipient, source=stream)
    lines = student.lines()
    assert [(ln.channel_ref, ln.message_id, ln.reply_to, ln.edit_of) for ln in lines] == [
        ("201", 201, None, None), ("202", 202, "201", None), ("203", 203, None, "201")]
    assert [set(ln.raw) for ln in lines] == [set(PLAIN_KEYS), set(PLAIN_KEYS) | {"reply_to"},
                                             set(PLAIN_KEYS) | {"edit_of"}]
    assert lines[1].text == "Questão\n(a) Dois\n(b) Nove\nSua resposta: b · ❌ incorreta"
    assert all(ln.answerable for ln in lines)
    _assert_no_chat(output)


def test_console_formatter_is_always_plain_and_unknown_dialect_is_value_error() -> None:
    """`ConsoleChannel.formatter` e `formatter_for` com `rich`,
    `html` e `plain` são o `PlainFormatter` (texto puro, limite 4 096), o de `cq_server.telegramrender`
    reexportado por `adapters.console`; um dialeto fora dos três → `ValueError`."""
    channel = ConsoleChannel(io.StringIO())
    assert console_adapter.PlainFormatter is telegramrender.PlainFormatter
    assert type(channel.formatter) is PlainFormatter
    for dialect in ("rich", "html", "plain"):
        formatter = channel.formatter_for(dialect)
        assert type(formatter) is PlainFormatter, dialect
        assert (formatter.text_format, formatter.max_length) == ("plain", 4096)
    for dialect in ("markdown", "rich_html", "RICH", "", "plain "):
        with pytest.raises(ValueError):
            channel.formatter_for(dialect)


@pytest.mark.parametrize(
    ("text", "length", "fits"),
    [("<b>1 & 2</b> /start https://exemplo.test", 40, True),
     ("𝛼² e 🙂", 8, True),
     ("x" * 4096, 4096, True),
     ("x" * 4097, 4097, False)],
    ids=["tags-and-command", "astral", "at-limit", "over-limit"])
def test_plain_formatter_escape_keeps_text_and_counts_utf16(text: str, length: int, fits: bool) -> None:
    """No texto puro nada é escapado (tags, `&`,
    comandos e URLs saem como estão), o tamanho é contado em unidades UTF-16 e `fits` é falso acima de
    4 096."""
    assert PlainFormatter().escape(text) == Formatted(markup=text, text_format="plain", length=length,
                                                      fits=fits)


# --- ConsoleInbox ----------------------------------------------------------------------------------


def _seed_bot(sql: psycopg.Connection, clock: FakeClock) -> int:
    """Um oferecimento `active` com um bot `registered` (o token cifrado não é lido aqui)."""
    now = clock.now()
    code = next(_codes)
    offering_row = sql.execute(
        "INSERT INTO offerings (offering_id, code, course, term, timezone, response_window_minutes, "
        "state, created_at, activated_at) VALUES (%s, %s, 'Disciplina do Console', '2031s1', "
        "'America/Sao_Paulo', 1440, 'active', %s, %s) RETURNING id",
        (uuid.uuid4(), f"console-disparo-{code}", now, now)).fetchone()[0]
    return sql.execute(
        "INSERT INTO bots (bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, "
        "token_version, state, profile_state, registered_at) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'registered', 'configured', %s) RETURNING id",
        (uuid.uuid4(), offering_row, 880_000 + code, f"cq_console_{code}_bot", b"\x01" + bytes(40),
         now)).fetchone()[0]


_ROW_SQL = ("SELECT id, bot_id, update_id, kind, chat_id, chat_type, sender_id, text, command, "
            "reply_to_message_id, received_at, state, outcome, processed_at FROM telegram_updates "
            "ORDER BY id")


def _rows(sql: psycopg.Connection) -> list[dict[str, Any]]:
    cursor = sql.execute(_ROW_SQL)
    names = [column.name for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _tasks(sql: psycopg.Connection) -> list[tuple[Any, ...]]:
    return sql.execute("SELECT task_type, payload, idempotency_key, state FROM tasks ORDER BY id").fetchall()


# ação → (chamada na `ConsoleInbox`, texto gravado, `command`, `reply_to_message_id`).
INBOX_ACTIONS: dict[str, tuple[Callable[[ConsoleInbox, int], None], str, str | None, int | None]] = {
    "start-command": (lambda inbox, bot: inbox.send_text(bot, CHAT_ID, "/start X"), "/start X", "/start",
                      None),
    "plain-text": (lambda inbox, bot: inbox.send_text(bot, CHAT_ID, "3,14"), "3,14", None, None),
    "text-in-reply": (lambda inbox, bot: inbox.send_text(bot, CHAT_ID, " 6,5 ", reply_to="321"), " 6,5 ",
                      None, 321),
    "choose-key": (lambda inbox, bot: inbox.choose(bot, CHAT_ID, "321", "b"), "b", None, 321),
    "dont-know": (lambda inbox, bot: inbox.dont_know(bot, CHAT_ID), "/naosei", "/naosei", None),
    "dont-know-in-reply": (lambda inbox, bot: inbox.dont_know(bot, CHAT_ID, "4321"), "/naosei", "/naosei",
                           4321),
}


@pytest.mark.parametrize("action", list(INBOX_ACTIONS))
def test_console_inbox_records_the_literal_row_and_its_task(action: str, db_url: str,
                                                            sql: psycopg.Connection,
                                                            clock: FakeClock) -> None:
    """Cada entrada simulada grava uma linha
    exata de `telegram_updates` (`kind = message`, `chat_type = private`, `chat_id` e `sender_id` do
    chat privado, o texto literal, `command` `/start` no `/start X`, `/naosei` no `dont_know` e nulo no
    texto comum e na escolha, `reply_to_message_id` = o número citado ou nulo, `update_id < 0`,
    `pending`, recebida agora) e a tarefa `telegram.update` dela (`{"update_row_id": id}`, chave
    `telegram.update:<id>`, `pending`)."""
    bot = _seed_bot(sql, clock)
    call, text, command, reply_to = INBOX_ACTIONS[action]
    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        call(inbox, bot)
    finally:
        inbox.close()
    rows = _rows(sql)
    assert len(rows) == 1
    row = rows[0]
    assert row["update_id"] < 0
    assert {k: v for k, v in row.items() if k not in ("id", "update_id")} == {
        "bot_id": bot, "kind": "message", "chat_id": CHAT_ID, "chat_type": "private",
        "sender_id": CHAT_ID, "text": text, "command": command, "reply_to_message_id": reply_to,
        "received_at": clock.now(), "state": "pending", "outcome": None, "processed_at": None}
    assert _tasks(sql) == [("telegram.update", {"update_row_id": row["id"]},
                            f"telegram.update:{row['id']}", "pending")]


def test_console_update_ids_are_negative_distinct_and_decreasing(db_url: str, sql: psycopg.Connection,
                                                                 clock: FakeClock) -> None:
    """As entradas simuladas de um bot têm `update_id` negativos,
    distintos e decrescentes na ordem de gravação (nunca colidem com os do Telegram, que são
    positivos), cada uma com a sua tarefa; o horário de cada linha é o do relógio na gravação."""
    bot = _seed_bot(sql, clock)
    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        inbox.send_text(bot, CHAT_ID, "primeira")
        clock.advance(5)
        inbox.choose(bot, CHAT_ID, "77", "a")
        clock.advance(5)
        inbox.dont_know(bot, CHAT_ID)
    finally:
        inbox.close()
    rows = _rows(sql)
    assert [row["text"] for row in rows] == ["primeira", "a", "/naosei"]
    ids = [row["update_id"] for row in rows]
    assert all(value < 0 for value in ids) and ids == sorted(ids, reverse=True) and len(set(ids)) == 3
    start = clock.now()
    assert [row["received_at"] for row in rows] == [start - timedelta(seconds=10),
                                                    start - timedelta(seconds=5), start]
    assert _tasks(sql) == [("telegram.update", {"update_row_id": row["id"]},
                            f"telegram.update:{row['id']}", "pending") for row in rows]


def test_simulated_student_replies_to_the_numeric_ref_of_the_line(db_url: str, sql: psycopg.Connection,
                                                                  clock: FakeClock) -> None:
    """A mensagem de entrega (`answerable = True`) sai com `channel_ref =
    str(message_id)`; o aluno simulado acha a linha pelo `recipient` (sem `chat_id` na linha) e
    responde por ela: o `reply_to_message_id` gravado é o `message_id` da linha; a escolha pela chave
    também."""
    bot = _seed_bot(sql, clock)
    stream = io.StringIO()
    channel = ConsoleChannel(stream)
    recipient = uuid.uuid4()
    other = uuid.uuid4()
    channel.send(OutgoingMessage(message_id=301, recipient=other, text="de outro aluno", answerable=True))
    channel.send(OutgoingMessage(message_id=302, recipient=recipient, text="Questão", route=_route(),
                                 answerable=True))
    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        student = ConsoleStudent(inbox, bot_id=bot, chat_id=CHAT_ID, recipient=recipient, source=stream)
        line = student.last()
        assert (line.channel_ref, line.message_id, line.answerable) == ("302", 302, True)
        student.reply(line, "6,5")
        student.choose(line, "c")
        student.dont_know(line)
    finally:
        inbox.close()
    assert [(row["text"], row["command"], row["reply_to_message_id"]) for row in _rows(sql)] == [
        ("6,5", None, 302), ("c", None, 302), ("/naosei", "/naosei", 302)]
    _assert_no_chat(stream.getvalue())
