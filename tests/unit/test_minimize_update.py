"""Minimização dos updates na borda.


`cq_server.adapters.telegram.minimize_update` é uma função pura que recebe o update cru da Bot API e
devolve o `MinimalUpdate` com **só** a lista de inclusão:
- `message`: `chat_id`, `chat_type`, `sender_id` (`from.id`), `text`, `command`; `details` =
  `{message_id, date}`;
- `callback_query`: `chat_id` (`message.chat.id`, se houver), `sender_id`; `details` =
  `{callback_id, data, message_id}`;
- `my_chat_member`: `chat_id`, `chat_type`, `sender_id`; `details` =
  `{date, old_status, new_status}`;
- `other`: nenhuma coluna; `details` = `{}`.

Nome, sobrenome, `@username` e `language_code` nunca aparecem no resultado, em nível nenhum. Os
remetentes canário (`tests/helpers/canary.py`, prefixos `tg-*`) entram em `from`, `chat`,
`reply_to_message.from`, `entities[].user`, `forward_origin`, `external_reply` e `contact`; os
textos injetados não contêm alvos canário, e um caso separado confirma que o `text` literal é
preservado.

No esqueleto, `minimize_update` lança `NotImplementedError`, e todo teste daqui passa por ela.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from cq_server.adapters.telegram import minimize_update
from cq_server.ports.telegram import MinimalUpdate
from tests.helpers.bot_api import TelegramUser, make_user
from tests.helpers.canary import (
    BASE_PREFIXES,
    TG_FILISBINA,
    TG_PREFIXES,
    TG_TEODORICO,
    TG_ZEFIRO,
    find_canary,
)

# Horário dos updates (o `date` da Bot API é Unix em segundos), longe da data real.
DATE = int(datetime(2031, 1, 1, tzinfo=timezone.utc).timestamp())
# `update_id` de entrada; a saída tem de repeti-lo (a função é pura, não gera ids).
UPDATE_ID = 810_000_123

# O bot do chat (como a Bot API o descreve em `message.from` e `*_chat_member.user`).
BOT_USER: dict[str, Any] = {"id": 6_100_000_999, "is_bot": True, "first_name": "Bot de teste",
                            "username": "cq_teste_0999_bot"}

# Prefixos dos alvos de **nome** (da API base e do Telegram): nenhum pode aparecer no resultado.
NAME_PREFIXES = BASE_PREFIXES + ("tg-nome", "tg-palavra", "tg-username")

# Texto neutro com um trecho neutro ("o professor") sob a menção `text_mention`.
NEUTRAL_TEXT = "Pergunta para o professor sobre a lista 3"
MENTION_OFFSET = NEUTRAL_TEXT.index("o professor")
MENTION_LENGTH = len("o professor")


def _message(sender: Any, text: str | None = None, *, message_id: int = 41,
             chat: dict[str, Any] | None = None, entities: list[dict[str, Any]] | None = None,
             date: int = DATE, update_id: int = UPDATE_ID, **extra: Any) -> dict[str, Any]:
    """Update `message` cru, com o remetente em `from` e o chat privado dele (ou `chat`)."""
    message: dict[str, Any] = {
        "message_id": message_id,
        "from": sender.as_user(),
        "chat": chat if chat is not None else sender.as_private_chat(),
        "date": date,
    }
    if text is not None:
        message["text"] = text
    if entities is not None:
        message["entities"] = entities
    message.update(extra)
    return {"update_id": update_id, "message": message}


def _callback(sender: Any, data: str | None, *, with_message: bool = True,
              callback_id: str = "4382bfdwdsb323b2d9", message_id: int = 77,
              chat: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Update `callback_query` cru (botão numa mensagem do bot, ou inline, sem `message`)."""
    query: dict[str, Any] = {"id": callback_id, "from": sender.as_user(),
                             "chat_instance": "-8800000000000000001"}
    if with_message:
        query["message"] = {"message_id": message_id, "from": BOT_USER,
                            "chat": chat if chat is not None else sender.as_private_chat(),
                            "date": DATE - 30, "text": "Mensagem do bot"}
    else:
        query["inline_message_id"] = "AgAAAHwTAQDnAAAA"
    if data is not None:
        query["data"] = data
    query.update(extra)
    return {"update_id": UPDATE_ID, "callback_query": query}


def _my_chat_member(sender: Any, *, old_status: str = "member", new_status: str = "kicked",
                    chat: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    """Update `my_chat_member` cru (no chat privado, o bloqueio: `member` → `kicked`)."""
    def member(status: str) -> dict[str, Any]:
        data: dict[str, Any] = {"user": BOT_USER, "status": status}
        if status == "kicked":
            data["until_date"] = 0
        return data

    body: dict[str, Any] = {"chat": chat if chat is not None else sender.as_private_chat(),
                            "from": sender.as_user(), "date": DATE,
                            "old_chat_member": member(old_status),
                            "new_chat_member": member(new_status)}
    body.update(extra)
    return {"update_id": UPDATE_ID, "my_chat_member": body}


def _other() -> MinimalUpdate:
    return MinimalUpdate(update_id=UPDATE_ID, kind="other", chat_id=None, chat_type=None,
                         sender_id=None, text=None, command=None, details={})


def _dump(result: MinimalUpdate) -> str:
    """Tudo o que o resultado carrega: os atributos da instância e os campos serializados."""
    return repr(vars(result)) + "\n" + json.dumps(dataclasses.asdict(result), ensure_ascii=False)


def _minimize(raw: dict[str, Any]) -> MinimalUpdate:
    """Chama a função e confere que ela é pura (não muda a entrada) e devolve um `MinimalUpdate`."""
    before = copy.deepcopy(raw)
    result = minimize_update(raw)
    assert raw == before, "minimize_update alterou o update de entrada"
    assert type(result) is MinimalUpdate
    assert type(result.details) is dict
    return result


# ---------------------------------------------------------------------------------------------
# Colunas e `details` exatos por `kind`
# ---------------------------------------------------------------------------------------------


def test_message_columns_and_details_are_exact() -> None:
    """Linha `message` da lista de inclusão: `chat_id`, `chat_type`, `sender_id` =
    `from.id`, o `text` literal, `command` (nulo sem entidade `bot_command`) e `details` exatamente
    `{message_id, date}`; num grupo, `sender_id` é o `from.id` e `chat_id` o do grupo."""
    user = make_user(11)
    result = _minimize(_message(user, "bom dia", message_id=41))
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=user.user_id,
                                   chat_type="private", sender_id=user.user_id, text="bom dia",
                                   command=None, details={"message_id": 41, "date": DATE})
    assert type(result.update_id) is int and type(result.chat_id) is int
    assert type(result.sender_id) is int

    group = {"id": -987_654_321, "type": "group", "title": "Turma de teste",
             "all_members_are_administrators": False}
    result = _minimize(_message(user, "/ajuda", message_id=9, chat=group,
                                entities=[{"offset": 0, "length": 6, "type": "bot_command"}]))
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=-987_654_321,
                                   chat_type="group", sender_id=user.user_id, text="/ajuda",
                                   command="/ajuda", details={"message_id": 9, "date": DATE})


def test_callback_query_columns_and_details_are_exact() -> None:
    """Linha `callback_query` da lista de inclusão: `chat_id` = `message.chat.id` (se
    houver), `sender_id` = `from.id`, as demais colunas nulas e `details` exatamente `{callback_id,
    data, message_id}`, com os ausentes (`message` de mensagem inline, `data` de jogo) em `null`."""
    user = make_user(12)
    result = _minimize(_callback(user, "resposta:a", message_id=77))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="callback_query", chat_id=user.user_id, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": "4382bfdwdsb323b2d9", "data": "resposta:a", "message_id": 77})

    result = _minimize(_callback(user, "resposta:b", with_message=False))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="callback_query", chat_id=None, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": "4382bfdwdsb323b2d9", "data": "resposta:b", "message_id": None})

    result = _minimize(_callback(user, None, message_id=78, game_short_name="jogo_teste"))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="callback_query", chat_id=user.user_id, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": "4382bfdwdsb323b2d9", "data": None, "message_id": 78})


def test_my_chat_member_columns_and_details_are_exact() -> None:
    """Linha `my_chat_member` da lista de inclusão: `chat_id`, `chat_type`, `sender_id`
    e `details` exatamente `{date, old_status, new_status}` (bloqueio `member` → `kicked` e
    desbloqueio `kicked` → `member`)."""
    user = make_user(13)
    result = _minimize(_my_chat_member(user))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="my_chat_member", chat_id=user.user_id, chat_type="private",
        sender_id=user.user_id, text=None, command=None,
        details={"date": DATE, "old_status": "member", "new_status": "kicked"})

    result = _minimize(_my_chat_member(user, old_status="kicked", new_status="member"))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="my_chat_member", chat_id=user.user_id, chat_type="private",
        sender_id=user.user_id, text=None, command=None,
        details={"date": DATE, "old_status": "kicked", "new_status": "member"})


@pytest.mark.parametrize("field, body", [
    ("edited_message", {"message_id": 41, "from": TG_TEODORICO.as_user(),
                        "chat": TG_TEODORICO.as_private_chat(), "date": DATE,
                        "edit_date": DATE + 60, "text": "bom dia (editado)"}),
    ("channel_post", {"message_id": 5, "sender_chat": {"id": -1_001_234_567_890, "type": "channel",
                                                         "title": "Canal de avisos"},
                      "chat": {"id": -1_001_234_567_890, "type": "channel",
                               "title": "Canal de avisos"},
                      "date": DATE, "text": "aviso", "author_signature": TG_FILISBINA.full_name}),
    ("inline_query", {"id": "88001", "from": TG_ZEFIRO.as_user(), "query": "lista",
                      "offset": ""}),
    ("chat_member", {"chat": {"id": -987_654_321, "type": "group", "title": "Turma"},
                     "from": TG_FILISBINA.as_user(), "date": DATE,
                     "old_chat_member": {"user": TG_TEODORICO.as_user(), "status": "member"},
                     "new_chat_member": {"user": TG_TEODORICO.as_user(), "status": "left"}}),
    ("poll_answer", {"poll_id": "5001", "user": TG_ZEFIRO.as_user(), "option_ids": [0]}),
    ("business_message", {"message_id": 3, "from": TG_TEODORICO.as_user(),
                          "chat": TG_TEODORICO.as_private_chat(), "date": DATE, "text": "oi",
                          "business_connection_id": "bc1"}),
    ("tipo_futuro_2027", {"from": TG_TEODORICO.as_user(), "campo": {"aninhado": [1, 2]}}),
], ids=lambda value: value if isinstance(value, str) else "corpo")
def test_other_kinds_keep_only_the_update_id(field: str, body: dict[str, Any]) -> None:
    """Linha `other` da lista de inclusão: qualquer tipo fora de
    `message`, `callback_query` e `my_chat_member` (inclusive um tipo futuro desconhecido) vira
    `kind = "other"`, sem nenhuma coluna e com `details == {}`; nada do corpo (nomes canário,
    assinatura, texto) sobra."""
    result = _minimize({"update_id": UPDATE_ID, field: body})
    assert result == _other()
    assert find_canary(_dump(result), prefixes=NAME_PREFIXES + ("tg-id",)) == []


def test_update_without_known_field_is_other() -> None:
    """Linha `other` da lista de inclusão: um update só com `update_id` também é
    `other`, sem colunas e com `details == {}`."""
    assert _minimize({"update_id": UPDATE_ID}) == _other()


# ---------------------------------------------------------------------------------------------
# Remetentes canário: nome, sobrenome e @username nunca chegam ao resultado
# ---------------------------------------------------------------------------------------------


def _rich_message() -> dict[str, Any]:
    """Mensagem de `TG_TEODORICO` com identidades canário em todo lugar que a Bot API as põe."""
    text_mention = {"offset": MENTION_OFFSET, "length": MENTION_LENGTH, "type": "text_mention",
                    "user": TG_ZEFIRO.as_user()}
    raw = _message(
        TG_TEODORICO, NEUTRAL_TEXT, message_id=501,
        entities=[text_mention],
        reply_to_message={
            "message_id": 499, "from": TG_FILISBINA.as_user(),
            "chat": TG_TEODORICO.as_private_chat(), "date": DATE - 60, "text": "Pode perguntar",
            "forward_origin": {"type": "hidden_user", "date": DATE - 90,
                               "sender_user_name": TG_ZEFIRO.full_name},
        },
        forward_origin={"type": "user", "date": DATE - 3600, "sender_user": TG_ZEFIRO.as_user()},
        external_reply={"origin": {"type": "hidden_user", "date": DATE - 7200,
                                   "sender_user_name": TG_TEODORICO.full_name}},
    )
    raw["message"]["from"]["is_premium"] = True
    return raw


def _contact_message() -> dict[str, Any]:
    """Mensagem de `TG_TEODORICO` que compartilha o contato de `TG_ZEFIRO` (sem texto)."""
    return _message(
        TG_TEODORICO, None, message_id=502,
        contact={"phone_number": "+55 19 99999-0000", "first_name": TG_ZEFIRO.first_name,
                 "last_name": TG_ZEFIRO.last_name, "user_id": TG_ZEFIRO.user_id,
                 "vcard": f"BEGIN:VCARD\nVERSION:3.0\nFN:{TG_ZEFIRO.full_name}\nEND:VCARD"},
    )


def _group_message() -> dict[str, Any]:
    """Mensagem de `TG_ZEFIRO` num supergrupo cujo título tem o nome de `TG_TEODORICO`."""
    chat = {"id": -1_001_234_567_890, "type": "supergroup",
            "title": f"Turma do {TG_TEODORICO.full_name}", "is_forum": False}
    return _message(TG_ZEFIRO, "Alguém tem a lista?", message_id=77, chat=chat,
                    reply_to_message={"message_id": 70, "from": TG_FILISBINA.as_user(),
                                      "chat": chat, "date": DATE - 10, "text": "Lista 3"})


def _canary_callback() -> dict[str, Any]:
    """Botão tocado por `TG_FILISBINA` numa mensagem do bot que responde a `TG_ZEFIRO` e menciona
    `TG_TEODORICO`."""
    raw = _callback(TG_FILISBINA, "resposta:c", message_id=80)
    message = raw["callback_query"]["message"]
    message["entities"] = [{"offset": 0, "length": 8, "type": "text_mention",
                            "user": TG_TEODORICO.as_user()}]
    message["reply_to_message"] = {"message_id": 79, "from": TG_ZEFIRO.as_user(),
                                   "chat": TG_FILISBINA.as_private_chat(), "date": DATE - 40,
                                   "text": "Qual a resposta?"}
    return raw


def _canary_member() -> dict[str, Any]:
    """Bloqueio do bot por `TG_ZEFIRO` no chat privado."""
    return _my_chat_member(TG_ZEFIRO)


CANARY_CASES = [
    pytest.param(
        _rich_message,
        MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=TG_TEODORICO.user_id,
                      chat_type="private", sender_id=TG_TEODORICO.user_id, text=NEUTRAL_TEXT,
                      command=None, details={"message_id": 501, "date": DATE}),
        [f"tg-id:{TG_TEODORICO.user_id}"],
        {f"tg-nome:{TG_TEODORICO.first_name}", f"tg-username:{TG_TEODORICO.username}",
         f"tg-nome:{TG_FILISBINA.first_name}", f"tg-nome:{TG_ZEFIRO.first_name}",
         f"tg-username:{TG_ZEFIRO.username}", f"tg-id:{TG_ZEFIRO.user_id}",
         f"tg-id:{TG_FILISBINA.user_id}", f"tg-id:{TG_TEODORICO.user_id}"},
        id="mensagem-com-mencao-resposta-e-encaminhamento"),
    pytest.param(
        _contact_message,
        MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=TG_TEODORICO.user_id,
                      chat_type="private", sender_id=TG_TEODORICO.user_id, text=None,
                      command=None, details={"message_id": 502, "date": DATE}),
        [f"tg-id:{TG_TEODORICO.user_id}"],
        {f"tg-nome:{TG_ZEFIRO.full_name}", f"tg-id:{TG_ZEFIRO.user_id}",
         f"tg-username:{TG_TEODORICO.username}"},
        id="contato"),
    pytest.param(
        _group_message,
        MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=-1_001_234_567_890,
                      chat_type="supergroup", sender_id=TG_ZEFIRO.user_id,
                      text="Alguém tem a lista?", command=None,
                      details={"message_id": 77, "date": DATE}),
        [f"tg-id:{TG_ZEFIRO.user_id}"],
        {f"tg-nome:{TG_TEODORICO.full_name}", f"tg-nome:{TG_FILISBINA.first_name}",
         f"tg-username:{TG_ZEFIRO.username}"},
        id="grupo-com-titulo"),
    pytest.param(
        _canary_callback,
        MinimalUpdate(update_id=UPDATE_ID, kind="callback_query", chat_id=TG_FILISBINA.user_id,
                      chat_type=None, sender_id=TG_FILISBINA.user_id, text=None, command=None,
                      details={"callback_id": "4382bfdwdsb323b2d9", "data": "resposta:c",
                               "message_id": 80}),
        [f"tg-id:{TG_FILISBINA.user_id}"],
        {f"tg-nome:{TG_FILISBINA.first_name}", f"tg-id:{TG_FILISBINA.user_id}",
         f"tg-username:{TG_ZEFIRO.username}", f"tg-username:{TG_TEODORICO.username}",
         f"tg-id:{TG_TEODORICO.user_id}"},
        id="callback"),
    pytest.param(
        _canary_member,
        MinimalUpdate(update_id=UPDATE_ID, kind="my_chat_member", chat_id=TG_ZEFIRO.user_id,
                      chat_type="private", sender_id=TG_ZEFIRO.user_id, text=None, command=None,
                      details={"date": DATE, "old_status": "member", "new_status": "kicked"}),
        [f"tg-id:{TG_ZEFIRO.user_id}"],
        {f"tg-nome:{TG_ZEFIRO.first_name}", f"tg-username:{TG_ZEFIRO.username}"},
        id="my-chat-member"),
]


@pytest.mark.parametrize("build, expected, ids_kept, control", CANARY_CASES)
def test_canary_sender_data_never_reaches_the_result(build: Any, expected: MinimalUpdate,
                                                     ids_kept: list[str],
                                                     control: set[str]) -> None:
    """com remetentes canário em `from`, `chat`,
    `reply_to_message.from`, `entities[].user` (`text_mention` sobre "o professor"),
    `forward_origin`, `external_reply`, `contact` e título de grupo, o resultado é exatamente o da
    lista de inclusão; nome, sobrenome, `@username` (e as palavras dos nomes da API base) não aparecem em
    nível nenhum (atributos e campos serializados), nem `language_code`, `is_premium` ou telefone; o
    único id canário que sobra é o de `chat_id`/`sender_id`. Controle positivo: o detector acha os
    alvos no update cru, e o texto injetado não tem alvo canário. A função não muda a entrada."""
    raw = build()
    raw_text = json.dumps(raw, ensure_ascii=False)
    assert control <= set(find_canary(raw_text, prefixes=TG_PREFIXES)), "controle positivo"
    literal = raw.get("message", {}).get("text")
    if literal is not None:
        assert find_canary(literal, prefixes=BASE_PREFIXES + TG_PREFIXES) == []

    result = _minimize(raw)
    assert result == expected
    dump = _dump(result)
    assert find_canary(dump, prefixes=NAME_PREFIXES) == []
    assert find_canary(dump, prefixes=("tg-id",)) == ids_kept
    rest = json.dumps({"kind": result.kind, "chat_type": result.chat_type, "text": result.text,
                       "command": result.command, "details": result.details},
                      ensure_ascii=False)
    assert find_canary(rest, prefixes=("tg-id",)) == []
    for leftover in ("language_code", "pt-br", "is_premium", "first_name", "last_name",
                     "username", "+55 19", "VCARD", "sender_user_name", "title"):
        assert leftover not in dump


# ---------------------------------------------------------------------------------------------
# Texto literal e comando
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    pytest.param("  espaços nas pontas  ", id="espacos-nas-pontas"),
    pytest.param("linha 1\nlinha 2\r\nlinha 3\tcom tab", id="quebras-e-tab"),
    pytest.param("Composta: ação, pão, Ünïcödé; decomposta: Joa\N{COMBINING TILDE}o, "
                 "cafe\N{COMBINING ACUTE ACCENT}", id="formas-composta-e-decomposta"),
    pytest.param("Fora do BMP: 😀🧮 e sequência com ZWJ: 👩\N{ZERO WIDTH JOINER}💻",
                 id="fora-do-bmp-e-zwj"),
    pytest.param("Marcação não interpretada: *negrito* _itálico_ <b>html</b> "
                 "[link](http://x.invalid)", id="marcacao-literal"),
    pytest.param("Direita para a esquerda: שלום عربى", id="rtl"),
    pytest.param("😀" * 2048, id="4096-unidades-utf16"),
    pytest.param("/help sem a entidade de comando", id="help-sem-entidade"),
])
def test_text_is_preserved_literally(text: str) -> None:
    """O texto literal da mensagem; nenhuma resposta perdida: o `text` sai exatamente como
    chegou (espaços, quebras, formas composta e decomposta sem normalizar, fora do BMP, ZWJ,
    marcação, RTL, 4096 unidades UTF-16), sem alvos canário no texto."""
    assert find_canary(text, prefixes=BASE_PREFIXES + TG_PREFIXES) == []
    result = _minimize(_message(make_user(21), text, message_id=5))
    assert result.text == text
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message",
                                   chat_id=make_user(21).user_id, chat_type="private",
                                   sender_id=make_user(21).user_id, text=text, command=None,
                                   details={"message_id": 5, "date": DATE})


def _entity(offset: int, length: int, kind: str = "bot_command", **extra: Any) -> dict[str, Any]:
    return {"offset": offset, "length": length, "type": kind, **extra}


@pytest.mark.parametrize("text, entities, command", [
    pytest.param("/start abcdefghijklmnopqrstuv", [_entity(0, 6)], "/start", id="start-com-codigo"),
    pytest.param("/start@Cq_Teste_Bot abc", [_entity(0, 19)], "/start@Cq_Teste_Bot",
                 id="start-com-username"),
    pytest.param("/ajuda", [_entity(0, 6)], "/ajuda", id="ajuda"),
    pytest.param("🙂 /ajuda", [_entity(3, 6)], None, id="emoji-antes-do-comando"),
    pytest.param("/ajuda 🙂🙂", [_entity(0, 6)], "/ajuda", id="emoji-depois-do-comando"),
    pytest.param("/😀start x", [_entity(0, 8)], "/😀start", id="comprimento-em-utf16"),
    pytest.param("/start abc", [_entity(0, 6, "bold")], None, id="outra-entidade-no-zero"),
    pytest.param("/start para o professor",
                 [_entity(7, 16, "text_mention", user=make_user(31).as_user()), _entity(0, 6)],
                 "/start", id="comando-nao-e-a-primeira-entidade"),
])
def test_command_comes_from_bot_command_entity_at_utf16_offset_zero(
        text: str, entities: list[dict[str, Any]], command: str | None) -> None:
    """O comando (`MinimalUpdate.command`): `command` é o trecho do
    texto coberto por uma entidade `bot_command` no deslocamento 0, contado em unidades UTF-16 (com
    emoji antes, a entidade não está no 0 e não há comando; um caractere fora do BMP conta 2);
    outra entidade no 0 não é comando; a ordem da lista de entidades não importa; o `text` fica
    inteiro."""
    user = make_user(22)
    result = _minimize(_message(user, text, message_id=6, entities=entities))
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=user.user_id,
                                   chat_type="private", sender_id=user.user_id, text=text,
                                   command=command, details={"message_id": 6, "date": DATE})


@pytest.mark.parametrize("text", ["/start", "/start abcdefghijklmnopqrstuv", "/ajuda",
                                  "/start@Cq_Teste_Bot abc"])
def test_start_without_entity_is_not_a_command(text: str) -> None:
    """Sem entidade, é texto comum: um `/start` (ou `/ajuda`)
    sem entidade `bot_command` tem `command = None`, e o texto fica gravado literal."""
    user = make_user(23)
    result = _minimize(_message(user, text, message_id=8))
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=user.user_id,
                                   chat_type="private", sender_id=user.user_id, text=text,
                                   command=None, details={"message_id": 8, "date": DATE})


def test_message_without_text_or_sender_has_null_columns() -> None:
    """uma mensagem sem `text` (foto com legenda que parece
    comando) tem `text` e `command` nulos e a legenda não é gravada; uma sem `from` (caso de borda
    da Bot API) tem `sender_id` nulo; o resto segue a lista de inclusão."""
    user = make_user(24)
    photo = _message(user, None, message_id=12,
                     photo=[{"file_id": "AgACAgEAAx", "file_unique_id": "AQAD", "width": 90,
                             "height": 90, "file_size": 1024}],
                     caption="/start legenda", caption_entities=[_entity(0, 6)])
    assert _minimize(photo) == MinimalUpdate(
        update_id=UPDATE_ID, kind="message", chat_id=user.user_id, chat_type="private",
        sender_id=user.user_id, text=None, command=None, details={"message_id": 12, "date": DATE})

    group = {"id": -1_001_111_222_333, "type": "supergroup", "title": "Turma"}
    anonymous = _message(user, "aviso da coordenação", message_id=13, chat=group)
    del anonymous["message"]["from"]
    assert _minimize(anonymous) == MinimalUpdate(
        update_id=UPDATE_ID, kind="message", chat_id=-1_001_111_222_333, chat_type="supergroup",
        sender_id=None, text="aviso da coordenação", command=None,
        details={"message_id": 13, "date": DATE})


# ---------------------------------------------------------------------------------------------
# Campos desconhecidos, ids grandes, tipo de chat desconhecido
# ---------------------------------------------------------------------------------------------


def test_unknown_fields_are_accepted_and_dropped() -> None:
    """Lista de inclusão, não de exclusão: campos que a Bot API
    ainda não tem (em `from`, `chat`, `message`, `callback_query`, `my_chat_member`) são aceitos e
    descartados; o resultado é o mesmo do update sem eles."""
    user = make_user(25)
    plain = _message(user, "oi", message_id=14)
    extended = copy.deepcopy(plain)
    extended["message"]["from"].update({"is_premium": True, "added_to_attachment_menu": True,
                                        "campo_futuro": {"x": [1, 2]}})
    extended["message"]["chat"].update({"campo_futuro_chat": True})
    extended["message"].update({"link_preview_options": {"is_disabled": True},
                                "effect_id": "5104841245755180586",
                                "campo_futuro_msg": [{"a": 1}]})
    expected = MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=user.user_id,
                             chat_type="private", sender_id=user.user_id, text="oi", command=None,
                             details={"message_id": 14, "date": DATE})
    assert _minimize(plain) == expected
    assert _minimize(extended) == expected

    callback = _callback(user, "resposta:d", message_id=90, campo_futuro={"y": 1})
    callback["callback_query"]["message"]["campo_futuro_msg"] = True
    assert _minimize(callback) == MinimalUpdate(
        update_id=UPDATE_ID, kind="callback_query", chat_id=user.user_id, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": "4382bfdwdsb323b2d9", "data": "resposta:d", "message_id": 90})

    invite_link = {"invite_link": "https://t.me/+AbCdEf", "creates_join_request": False,
                   "is_primary": True, "is_revoked": False, "creator": TG_TEODORICO.as_user()}
    member = _my_chat_member(user, via_join_request=False, campo_futuro="z",
                             invite_link=invite_link)
    result = _minimize(member)
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="my_chat_member", chat_id=user.user_id, chat_type="private",
        sender_id=user.user_id, text=None, command=None,
        details={"date": DATE, "old_status": "member", "new_status": "kicked"})
    assert find_canary(_dump(result), prefixes=NAME_PREFIXES + ("tg-id",)) == []


def test_negative_and_large_chat_ids_are_preserved() -> None:
    """Ids grandes (`bigint`): `chat_id` negativo (grupo e
    supergrupo) e ids maiores que 2^32 (chat e remetente) saem iguais, como `int`."""
    big = TelegramUser(2**33 + 7, "Pessoa grande")
    supergroup = {"id": -1_009_876_543_210, "type": "supergroup", "title": "Turma"}
    cases = [
        (_message(big, "oi", message_id=1),
         MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=2**33 + 7,
                       chat_type="private", sender_id=2**33 + 7, text="oi", command=None,
                       details={"message_id": 1, "date": DATE})),
        (_message(big, "oi", message_id=2, chat=supergroup),
         MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=-1_009_876_543_210,
                       chat_type="supergroup", sender_id=2**33 + 7, text="oi", command=None,
                       details={"message_id": 2, "date": DATE})),
        (_callback(big, "x", message_id=3, chat=supergroup),
         MinimalUpdate(update_id=UPDATE_ID, kind="callback_query", chat_id=-1_009_876_543_210,
                       chat_type=None, sender_id=2**33 + 7, text=None, command=None,
                       details={"callback_id": "4382bfdwdsb323b2d9", "data": "x",
                                "message_id": 3})),
        (_my_chat_member(big, chat={"id": -2**40, "type": "group", "title": "Turma"}),
         MinimalUpdate(update_id=UPDATE_ID, kind="my_chat_member", chat_id=-2**40,
                       chat_type="group", sender_id=2**33 + 7, text=None, command=None,
                       details={"date": DATE, "old_status": "member", "new_status": "kicked"})),
    ]
    for raw, expected in cases:
        result = _minimize(raw)
        assert result == expected
        assert type(result.chat_id) is int and type(result.sender_id) is int


@pytest.mark.parametrize("chat_type, expected", [
    ("private", "private"), ("group", "group"), ("supergroup", "supergroup"),
    ("channel", "channel"), ("secret", None), ("tipo_futuro", None), ("", None),
])
def test_unknown_chat_type_becomes_none(chat_type: str, expected: str | None) -> None:
    """`chat.type` fora de `private`·`group`·`supergroup`·`channel`
    vira `chat_type = None` (em `message` e em `my_chat_member`); os conhecidos saem iguais."""
    user = make_user(26)
    chat = {"id": -555_000_111, "type": chat_type, "title": "Chat"}
    assert _minimize(_message(user, "oi", message_id=4, chat=chat)) == MinimalUpdate(
        update_id=UPDATE_ID, kind="message", chat_id=-555_000_111, chat_type=expected,
        sender_id=user.user_id, text="oi", command=None, details={"message_id": 4, "date": DATE})
    assert _minimize(_my_chat_member(user, chat=chat)) == MinimalUpdate(
        update_id=UPDATE_ID, kind="my_chat_member", chat_id=-555_000_111, chat_type=expected,
        sender_id=user.user_id, text=None, command=None,
        details={"date": DATE, "old_status": "member", "new_status": "kicked"})


# ---------------------------------------------------------------------------------------------
# U+0000 e substitutos isolados; repr
# ---------------------------------------------------------------------------------------------


def test_nul_and_lone_surrogates_become_replacement_character() -> None:
    """U+0000 e substitutos UTF-16 isolados (alto e baixo, no meio
    e no fim) em `text` e em `callback_query.data` viram U+FFFD, um por um; o resto do texto
    (inclusive um emoji fora do BMP) fica igual; esse é o único desvio do texto literal."""
    user = make_user(27)
    text = "a\x00b\ud800c\udfffd 😀 fim\ud83d"
    result = _minimize(_message(user, text, message_id=15))
    assert result.text == "a\ufffdb\ufffdc\ufffdd 😀 fim\ufffd"
    assert result == MinimalUpdate(update_id=UPDATE_ID, kind="message", chat_id=user.user_id,
                                   chat_type="private", sender_id=user.user_id,
                                   text="a\ufffdb\ufffdc\ufffdd 😀 fim\ufffd", command=None,
                                   details={"message_id": 15, "date": DATE})

    only_nul = _minimize(_message(user, "\x00", message_id=16))
    assert only_nul.text == "\ufffd"

    result = _minimize(_callback(user, "x\x00y\udbffz", message_id=17))
    assert result == MinimalUpdate(
        update_id=UPDATE_ID, kind="callback_query", chat_id=user.user_id, chat_type=None,
        sender_id=user.user_id, text=None, command=None,
        details={"callback_id": "4382bfdwdsb323b2d9", "data": "x\ufffdy\ufffdz",
                 "message_id": 17})


def test_minimal_update_repr_hides_chat_sender_and_text() -> None:
    """O `repr` de `MinimalUpdate` sem `chat_id`, `sender_id` nem `text`; pelo princípio
    da identidade só pela revelação: o `repr` do resultado de `minimize_update` (mensagem de remetente canário e callback) não
    mostra o id do chat, o do remetente nem o texto; controle positivo: os valores estão no
    objeto."""
    text = "texto-literal-do-remetente 42"
    result = _minimize(_message(TG_TEODORICO, text, message_id=18))
    assert (result.chat_id, result.sender_id, result.text) == (
        TG_TEODORICO.user_id, TG_TEODORICO.user_id, text)
    shown = repr(result)
    assert str(TG_TEODORICO.user_id) not in shown
    assert text not in shown and "texto-literal" not in shown
    assert find_canary(shown, prefixes=NAME_PREFIXES + ("tg-id",)) == []

    group_user = TelegramUser(2**33 + 9, "Pessoa")
    callback = _minimize(_callback(group_user, "resposta:e", message_id=19,
                                   chat={"id": -1_004_444_555_666, "type": "supergroup",
                                         "title": "Turma"}))
    assert (callback.chat_id, callback.sender_id) == (-1_004_444_555_666, 2**33 + 9)
    shown = repr(callback)
    assert str(-1_004_444_555_666) not in shown and "1004444555666" not in shown
    assert str(2**33 + 9) not in shown
