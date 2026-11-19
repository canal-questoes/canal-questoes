"""Adaptador HTTP da Bot API: os métodos do disparo e da resposta.


`cq_server.adapters.telegram.HttpTelegramBotApi` fala com a **Bot API falsa com a extensão do disparo**
(fixture `rich_bot_api`: `tests/helpers/bot_api.py` + `tests/helpers/bot_api_dispatch.py`), que registra cada
pedido (`BotApiCall`, sem o token), lê o multipart dos uploads e acusa em `protocol_errors()` e
`policy_errors()` qualquer desvio (parâmetro não documentado, tag fora do modo, `callback_data` fora de
1 a 64 bytes, `reply_parameters` sem `allow_sending_without_reply`…); a fixture exige as duas listas
vazias no fim de cada teste.

O que se confere aqui:
- cada método novo (`send_message` com os parâmetros opcionais, `send_rich_message`,
  `edit_message_text`, `edit_message_reply_markup`, `answer_callback_query`, `send_photo`,
  `send_document`) posta os **parâmetros exatos**, só os não nulos; o `sendMessage` sem os campos novos
  continua `{chat_id, text}` (regressão afirmada na mesma função de um caso
  novo);
- multipart (`httpx2.Request(data=, files=)`) **só** nos dois uploads, com `reply_parameters` em texto
  JSON; nenhum `httpx2.Client` (ele registra a URL com o token);
- os 400 classificados pela descrição, só no 400: `MessageNotModified`, `EditTargetUnavailable`,
  `CallbackQueryExpired`; os demais continuam `RequestRejected`; a hierarquia (subclasses de
  `RequestRejected`) afirmada na mesma função; a mensagem da exceção fixa, sem a descrição;
- `extract_reply_to(raw)`: o `message.reply_to_message.message_id` inteiro
  maior que 0, e `None` no resto; `minimize_update` sem mudança, afirmado na mesma
  função;
- `get_updates_with_replies(offset=…)`: a mesma chamada `getUpdates`, devolvendo também `update_id` →
  `reply_to_message_id` só dos não nulos; `get_updates` devolve o primeiro elemento.

Os parâmetros de objeto (`reply_markup`, `reply_parameters`, `link_preview_options`, `rich_message`)
vão no corpo JSON como objetos JSON aninhados, como o `setMyCommands` manda a lista de comandos
(só inclui no corpo o que não for `None`); no multipart, o `reply_parameters` vai
como texto JSON (a Bot API: "JSON-serialized").

No esqueleto, `send_message` com algum argumento novo não nulo, os métodos novos,
`get_updates_with_replies` e `extract_reply_to` lançam `NotImplementedError`: todo teste deste arquivo
chama pelo menos um deles.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest

from cq_server.adapters.telegram import HttpTelegramBotApi, extract_reply_to, minimize_update
from cq_server.ports.telegram import (
    CallbackQueryExpired,
    ChatUnreachable,
    EditTargetUnavailable,
    MessageNotModified,
    MinimalUpdate,
    RequestRejected,
    RetryAfter,
    TelegramError,
    TelegramUnavailable,
    UploadFile,
)
from tests.helpers.bot_api import ERRORS, make_user
from tests.helpers.bot_api_dispatch import DISPATCH_ERRORS
from tests.helpers.engine import png_bytes

# A lista exata de toda `getUpdates`.
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member"]
# toda resposta do sistema leva `allow_sending_without_reply: true`.
_KEYBOARD = {"inline_keyboard": [[{"text": "(a)", "callback_data": "cq-teste:c:a"},
                                  {"text": "(b)", "callback_data": "cq-teste:c:b"}],
                                 [{"text": "Não sei", "callback_data": "cq-teste:n"}]]}
_FORCE_REPLY = {"force_reply": True, "input_field_placeholder": "Número; ex.: 4.25, -17 ou 3/7"}
_NO_PREVIEW = {"is_disabled": True}


def _reply_to(message_id: int) -> dict[str, Any]:
    return {"message_id": message_id, "allow_sending_without_reply": True}


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


class _Apis:
    """Constrói adaptadores contra a Bot API falsa e fecha todos no fim do teste."""

    def __init__(self, fake: Any) -> None:
        self._fake = fake
        self.created: list[HttpTelegramBotApi] = []

    def __call__(self, token: str) -> HttpTelegramBotApi:
        api = HttpTelegramBotApi(token, base_url=self._fake.base_url, poll_timeout_seconds=1,
                                 timeout_seconds=2.0)
        self.created.append(api)
        return api


@pytest.fixture
def make_api(rich_bot_api: Any) -> Iterator[_Apis]:
    apis = _Apis(rich_bot_api)
    yield apis
    rich_bot_api.release_hangs()
    for api in apis.created:
        api.close()


def _setup(rich: Any, index: int) -> tuple[int, Any, int]:
    """Um bot e um aluno que já falou com ele (o chat passa a ser conhecido do dublê); devolve
    `(bot, aluno, chat_id)`."""
    bot = rich.add_bot()
    user = make_user(index)
    rich.push_text(bot, user, "oi")
    return bot, user, user.user_id


def _media_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _last_call(rich: Any, method: str) -> Any:
    calls = rich.calls(method)
    assert calls, f"nenhuma chamada {method}"
    return calls[-1]


def _exception_texts(exc: BaseException) -> list[str]:
    """`str`, `repr` e `args` da exceção e da cadeia (`__cause__`/`__context__`)."""
    texts: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        texts += [str(current), repr(current), repr(current.args)]
        pending += [current.__cause__, current.__context__]
    return texts


def _upload_info(data: bytes, filename: str, content_type: str) -> dict[str, Any]:
    """Como a extensão registra um arquivo recebido por multipart."""
    return {"filename": filename, "content_type": content_type, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


# ---------------------------------------------------------------------------------------------
# Parâmetros exatos, só os não nulos
# ---------------------------------------------------------------------------------------------


def test_send_message_posts_only_the_non_null_optional_params(rich_bot_api: Any,
                                                              make_api: _Apis) -> None:
    """Regressão: o `sendMessage` sem campos
    novos continua **exatamente** `{chat_id, text}`; com `parse_mode="HTML"`,
    `reply_markup` (teclado inline ou `ForceReply`), `reply_parameters` e `link_preview_options`, o
    corpo JSON leva exatamente os parâmetros não nulos, com os objetos aninhados iguais aos recebidos;
    cada chamada é um `POST` JSON (nunca multipart) e devolve o `message_id` inteiro da mensagem
    aceita."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 1)
    api = make_api(rich.token_of(bot))

    first = api.send_message(chat, "Olá, turma")
    html = "<b>Questão</b> com 2 &lt; 3"
    second = api.send_message(chat, html, parse_mode="HTML", reply_markup=_KEYBOARD,
                              reply_parameters=_reply_to(first), link_preview_options=_NO_PREVIEW)
    third = api.send_message(chat, "Campo 1 de 1: Velocidade m/s — digite a resposta.",
                             reply_markup=_FORCE_REPLY, reply_parameters=_reply_to(second))
    fourth = api.send_message(chat, "<i>Aviso</i>", parse_mode="HTML",
                              link_preview_options=_NO_PREVIEW)

    calls = rich.calls("sendMessage", bot_id=bot)
    assert [c.params for c in calls] == [
        {"chat_id": chat, "text": "Olá, turma"},
        {"chat_id": chat, "text": html, "parse_mode": "HTML", "reply_markup": _KEYBOARD,
         "reply_parameters": _reply_to(first), "link_preview_options": _NO_PREVIEW},
        {"chat_id": chat, "text": "Campo 1 de 1: Velocidade m/s — digite a resposta.",
         "reply_markup": _FORCE_REPLY, "reply_parameters": _reply_to(second)},
        {"chat_id": chat, "text": "<i>Aviso</i>", "parse_mode": "HTML",
         "link_preview_options": _NO_PREVIEW},
    ]
    for call in calls:
        assert (call.http_method, _media_type(call.content_type), call.has_query) == (
            "POST", "application/json", False)
        assert (call.outcome, call.status) == ("ok", 200)
        assert type(call.params["chat_id"]) is int
    ids = [first, second, third, fourth]
    assert all(type(i) is int for i in ids)
    assert ids == [m.message_id for m in rich.messages(bot, chat)]
    shown = rich.current(bot, chat, second)
    assert (shown.text_format, shown.plain_text, shown.reply_to_message_id) == (
        "html", "Questão com 2 < 3", first)
    assert rich.current(bot, chat, third).force_reply is True


def test_send_rich_message_posts_exact_params(rich_bot_api: Any, make_api: _Apis) -> None:
    """`send_rich_message` posta
    `sendRichMessage` com `chat_id` e o `rich_message` exatamente como recebido (inclusive
    `skip_entity_detection: true`), e `reply_markup`/`reply_parameters` só quando dados; sem
    `parse_mode`; devolve o `message_id` inteiro."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 2)
    api = make_api(rich.token_of(bot))
    body = {"html": "<p>Calcule <tg-math>x^2</tg-math> &amp; responda.</p>",
            "skip_entity_detection": True}

    plain = api.send_rich_message(chat, body)
    full = api.send_rich_message(chat, {**body, "html": "<p>Outra</p>"}, reply_markup=_KEYBOARD,
                                 reply_parameters=_reply_to(plain))

    calls = rich.calls("sendRichMessage", bot_id=bot)
    assert [c.params for c in calls] == [
        {"chat_id": chat, "rich_message": body},
        {"chat_id": chat, "rich_message": {**body, "html": "<p>Outra</p>"}, "reply_markup": _KEYBOARD,
         "reply_parameters": _reply_to(plain)},
    ]
    for call in calls:
        assert (_media_type(call.content_type), call.outcome) == ("application/json", "ok")
    assert type(plain) is int and type(full) is int and full != plain
    first = rich.current(bot, chat, plain)
    assert (first.text_format, first.plain_text, first.keyboard) == ("rich_html",
                                                                     "Calcule x^2 & responda.", None)
    second = rich.current(bot, chat, full)
    assert [b.data for b in second.buttons()] == ["cq-teste:c:a", "cq-teste:c:b", "cq-teste:n"]
    assert second.reply_to_message_id == plain


def test_edit_message_text_posts_exact_params(rich_bot_api: Any, make_api: _Apis) -> None:
    """`edit_message_text` posta
    `editMessageText` com `chat_id`, `message_id` e só os não nulos entre `text`, `parse_mode`,
    `rich_message`, `reply_markup` e `link_preview_options` (o ramo do texto com `parse_mode`, ou o
    do `rich_message` sozinho); devolve `None`; a edição fica no estado da mensagem no dublê."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 3)
    api = make_api(rich.token_of(bot))
    html_id = api.send_message(chat, "Questão")
    rich_id = api.send_rich_message(chat, {"html": "<p>Questão</p>", "skip_entity_detection": True})

    assert api.edit_message_text(chat, html_id, text="<b>Parte 1 de 2</b>", parse_mode="HTML",
                                 reply_markup=_KEYBOARD, link_preview_options=_NO_PREVIEW) is None
    assert api.edit_message_text(chat, html_id, text="Janela fechada.") is None
    rich_body = {"html": "<p>Sua resposta: 6,5 · ✅ correta</p>", "skip_entity_detection": True}
    assert api.edit_message_text(chat, rich_id, rich_message=rich_body, reply_markup=_KEYBOARD) is None

    calls = rich.calls("editMessageText", bot_id=bot)
    assert [c.params for c in calls] == [
        {"chat_id": chat, "message_id": html_id, "text": "<b>Parte 1 de 2</b>", "parse_mode": "HTML",
         "reply_markup": _KEYBOARD, "link_preview_options": _NO_PREVIEW},
        {"chat_id": chat, "message_id": html_id, "text": "Janela fechada."},
        {"chat_id": chat, "message_id": rich_id, "rich_message": rich_body, "reply_markup": _KEYBOARD},
    ]
    for call in calls:
        assert (_media_type(call.content_type), call.outcome) == ("application/json", "ok")
        assert type(call.params["message_id"]) is int
    edited = rich.current(bot, chat, html_id)
    assert (edited.text_format, edited.plain_text, edited.keyboard, edited.edit_count) == (
        "plain", "Janela fechada.", None, 2)
    rich_edited = rich.current(bot, chat, rich_id)
    assert (rich_edited.text_format, rich_edited.plain_text) == ("rich_html",
                                                                 "Sua resposta: 6,5 · ✅ correta")


def test_edit_message_reply_markup_posts_exact_params(rich_bot_api: Any, make_api: _Apis) -> None:
    """`edit_message_reply_markup` posta
    `editMessageReplyMarkup` com exatamente `{chat_id, message_id, reply_markup}` e devolve `None`; o
    teclado corrente da mensagem passa a ser o novo."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 4)
    api = make_api(rich.token_of(bot))
    message_id = api.send_message(chat, "Questão")
    marked = {"inline_keyboard": [[{"text": "✅ (a)", "callback_data": "cq-teste:u:a"},
                                   {"text": "⬜ (b)", "callback_data": "cq-teste:m:b"}],
                                  [{"text": "Enviar", "callback_data": "cq-teste:s"}]]}

    assert api.edit_message_reply_markup(chat, message_id, marked) is None

    call = _last_call(rich, "editMessageReplyMarkup")
    assert call.params == {"chat_id": chat, "message_id": message_id, "reply_markup": marked}
    assert (_media_type(call.content_type), call.outcome) == ("application/json", "ok")
    assert [b.data for b in rich.current(bot, chat, message_id).buttons()] == [
        "cq-teste:u:a", "cq-teste:m:b", "cq-teste:s"]


def test_answer_callback_query_posts_exact_params(rich_bot_api: Any, make_api: _Apis) -> None:
    """`answer_callback_query(id)` posta só
    `{callback_query_id}` (sem `text`, `show_alert`, `url` nem `cache_time`); com `text`, exatamente
    `{callback_query_id, text}`; devolve `None`; o dublê registra a confirmação de cada toque."""
    rich = rich_bot_api
    bot, user, chat = _setup(rich, 5)
    api = make_api(rich.token_of(bot))
    message_id = api.send_message(chat, "Questão")
    silent = rich.tap_raw(bot, user, message_id, "cq-teste:c:a")
    spoken = rich.tap_raw(bot, user, message_id, "cq-teste:c:b")

    assert api.answer_callback_query(silent.callback_id) is None
    assert api.answer_callback_query(spoken.callback_id, text="Esta parte já foi respondida.") is None

    calls = rich.calls("answerCallbackQuery", bot_id=bot)
    assert [c.params for c in calls] == [
        {"callback_query_id": silent.callback_id},
        {"callback_query_id": spoken.callback_id, "text": "Esta parte já foi respondida."},
    ]
    assert all((_media_type(c.content_type), c.outcome) == ("application/json", "ok") for c in calls)
    assert [(a.callback_id, a.text) for a in rich.answers(bot)] == [
        (silent.callback_id, None), (spoken.callback_id, "Esta parte já foi respondida.")]


@pytest.mark.parametrize("method", ["sendPhoto", "sendDocument"])
def test_uploads_post_multipart_with_the_bytes_and_json_reply_parameters(
        rich_bot_api: Any, make_api: _Apis, method: str) -> None:
    """`send_photo`/`send_document`
    postam `multipart/form-data` com `chat_id` e o arquivo (nome, tipo e bytes exatos do
    `UploadFile`, no campo `photo` ou `document`) e, só quando dados, `caption` e `reply_parameters`
    (este em texto JSON); nenhum `parse_mode`; devolvem o `message_id` inteiro. O `repr` do
    `UploadFile` não mostra os bytes (conteúdo de terceiros)."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 6)
    api = make_api(rich.token_of(bot))
    question = api.send_message(chat, "Questão")
    field = "photo" if method == "sendPhoto" else "document"
    data = png_bytes() if method == "sendPhoto" else b"%PDF-1.4\n% documento de teste\n%%EOF\n"
    content_type = "image/png" if method == "sendPhoto" else "application/pdf"
    filename = "figura.png" if method == "sendPhoto" else "anexo.pdf"
    upload = UploadFile(filename=filename, content_type=content_type, data=data)
    assert repr(data) not in repr(upload) and "data=" not in repr(upload)
    send = api.send_photo if method == "sendPhoto" else api.send_document

    bare = send(chat, upload)
    full = send(chat, upload, caption="Figura 1 — gráfico de v < t", reply_parameters=_reply_to(question))

    calls = rich.calls(method, bot_id=bot)
    assert len(calls) == 2
    for call in calls:
        assert call.http_method == "POST" and call.has_query is False
        assert _media_type(call.content_type) == "multipart/form-data"
        assert call.outcome == "ok"
    info = _upload_info(data, filename, content_type)
    assert calls[0].params == {"chat_id": str(chat), field: info}
    assert {k: v for k, v in calls[1].params.items() if k != "reply_parameters"} == {
        "chat_id": str(chat), field: info, "caption": "Figura 1 — gráfico de v < t"}
    assert isinstance(calls[1].params["reply_parameters"], str)
    assert json.loads(calls[1].params["reply_parameters"]) == _reply_to(question)

    assert type(bare) is int and type(full) is int and bare != full
    uploads = rich.uploads(bot, chat)
    assert [(u.method, u.field, u.filename, u.content_type, u.data, u.caption, u.message_id)
            for u in uploads] == [
        (method, field, filename, content_type, data, None, bare),
        (method, field, filename, content_type, data, "Figura 1 — gráfico de v < t", full)]
    assert rich.current(bot, chat, full).reply_to_message_id == question


def test_multipart_only_in_uploads_and_never_an_httpx2_client(
        rich_bot_api: Any, make_api: _Apis, monkeypatch: pytest.MonkeyPatch) -> None:
    """cada método novo (e o `sendMessage`) monta **um**
    `httpx2.Request` `POST`; só `sendPhoto` e `sendDocument` usam `data=`/`files=` (multipart), e os
    demais mandam o corpo JSON por `content=` (sem `files` nem `data`); nenhum `httpx2.Client` nem
    `httpx2.AsyncClient` é criado. O dublê vê `multipart/form-data` só nos dois uploads. Controle
    positivo: um `httpx2.Client` criado pelo teste é visto pelo espião."""
    built: list[tuple[str, str, bool, bool, bool]] = []
    original_request = httpx2.Request.__init__

    def request_spy(self: Any, method: str, url: Any, *args: Any, **kwargs: Any) -> None:
        # Só o nome do método da Bot API (último segmento): a URL tem o token.
        built.append((str(method).upper(), str(url).rsplit("/", 1)[-1],
                      kwargs.get("files") is not None, kwargs.get("data") is not None,
                      kwargs.get("content") is not None))
        original_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx2.Request, "__init__", request_spy)
    created: list[str] = []
    for cls in (httpx2.Client, httpx2.AsyncClient):
        original = cls.__init__

        def spy(self: Any, *args: Any, _original: Any = original, _name: str = cls.__name__,
                **kwargs: Any) -> None:
            created.append(_name)
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", spy)

    rich = rich_bot_api
    bot, user, chat = _setup(rich, 7)
    api = make_api(rich.token_of(bot))
    question = api.send_message(chat, "Questão")
    api.send_message(chat, "<b>Questão</b>", parse_mode="HTML", reply_markup=_KEYBOARD,
                     link_preview_options=_NO_PREVIEW)
    api.send_rich_message(chat, {"html": "<p>Questão</p>", "skip_entity_detection": True})
    api.edit_message_text(chat, question, text="Parte 1 de 2")
    api.edit_message_reply_markup(chat, question, _KEYBOARD)
    tap = rich.tap(bot, user, question, "cq-teste:n")
    api.answer_callback_query(tap.callback_id, text="Registrado.")
    photo = UploadFile("figura.png", "image/png", png_bytes())
    api.send_photo(chat, photo, caption="Figura 1")
    api.send_document(chat, UploadFile("figura.png", "image/png", png_bytes()))

    assert [b[1] for b in built] == ["sendMessage", "sendMessage", "sendRichMessage",
                                     "editMessageText", "editMessageReplyMarkup",
                                     "answerCallbackQuery", "sendPhoto", "sendDocument"]
    for verb, name, has_files, has_data, has_content in built:
        assert verb == "POST"
        if name in ("sendPhoto", "sendDocument"):
            assert (has_files, has_data, has_content) == (True, True, False), name
        else:
            assert (has_files, has_data, has_content) == (False, False, True), name
    kinds = {(c.method, _media_type(c.content_type)) for c in rich.calls(bot_id=bot)
             if c.method != "getUpdates"}
    assert kinds == {("sendMessage", "application/json"), ("sendRichMessage", "application/json"),
                     ("editMessageText", "application/json"),
                     ("editMessageReplyMarkup", "application/json"),
                     ("answerCallbackQuery", "application/json"),
                     ("sendPhoto", "multipart/form-data"), ("sendDocument", "multipart/form-data")}
    assert created == []

    httpx2.Client(trust_env=False).close()
    assert created == ["Client"], "controle positivo"


# ---------------------------------------------------------------------------------------------
# Classificação dos 400 pela descrição (só no 400)
# ---------------------------------------------------------------------------------------------


def _call_method(api: HttpTelegramBotApi, method: str, chat: int, target: int) -> Any:
    """Chama o método da porta que corresponde ao método da Bot API (com parâmetros válidos)."""
    if method == "editMessageText":
        return api.edit_message_text(chat, target, text="Texto novo da edição")
    if method == "editMessageReplyMarkup":
        return api.edit_message_reply_markup(chat, target, _KEYBOARD)
    if method == "answerCallbackQuery":
        return api.answer_callback_query("9900000000000000999")
    if method == "sendRichMessage":
        return api.send_rich_message(chat, {"html": "<p>Questão</p>", "skip_entity_detection": True},
                                     reply_parameters=_reply_to(target))
    if method == "sendMessage":
        return api.send_message(chat, "Aviso", parse_mode="HTML", reply_parameters=_reply_to(target))
    if method == "sendPhoto":
        return api.send_photo(chat, UploadFile("figura.png", "image/png", png_bytes()))
    if method == "sendDocument":
        return api.send_document(chat, UploadFile("anexo.pdf", "application/pdf", b"%PDF-1.4\n"))
    raise AssertionError(f"método desconhecido: {method}")


CLASSIFIED = [
    pytest.param("editMessageText", 400, DISPATCH_ERRORS["not_modified"][1], MessageNotModified,
                 id="400-not-modified"),
    pytest.param("editMessageText", 400, "Bad Request: MESSAGE IS NOT MODIFIED", MessageNotModified,
                 id="400-not-modified-maiusculas"),
    pytest.param("editMessageText", 400, DISPATCH_ERRORS["edit_not_found"][1], EditTargetUnavailable,
                 id="400-edit-not-found"),
    pytest.param("editMessageReplyMarkup", 400, DISPATCH_ERRORS["edit_not_found"][1],
                 EditTargetUnavailable, id="400-markup-edit-not-found"),
    pytest.param("editMessageReplyMarkup", 400, DISPATCH_ERRORS["cant_be_edited"][1],
                 EditTargetUnavailable, id="400-cant-be-edited"),
    pytest.param("answerCallbackQuery", 400, DISPATCH_ERRORS["query_too_old"][1], CallbackQueryExpired,
                 id="400-query-too-old"),
    pytest.param("editMessageText", 400, DISPATCH_ERRORS["no_text"][1], RequestRejected,
                 id="400-no-text-e-outro"),
    pytest.param("sendRichMessage", 400, DISPATCH_ERRORS["reply_not_found"][1], RequestRejected,
                 id="400-reply-not-found"),
    pytest.param("sendRichMessage", 400, DISPATCH_ERRORS["rich_too_long"][1], RequestRejected,
                 id="400-rich-too-long"),
    pytest.param("sendMessage", 400, "Bad Request: can't parse entities: Unsupported start tag",
                 RequestRejected, id="400-parse-entities"),
    pytest.param("sendPhoto", 400, DISPATCH_ERRORS["photo_type"][1], RequestRejected,
                 id="400-foto-recusada"),
    pytest.param("sendDocument", 400, DISPATCH_ERRORS["file_too_big"][1], RequestRejected,
                 id="400-documento-grande"),
    pytest.param("editMessageText", 403, DISPATCH_ERRORS["not_modified"][1], RequestRejected,
                 id="403-com-frase-do-400"),
    pytest.param("answerCallbackQuery", 404, DISPATCH_ERRORS["query_too_old"][1], RequestRejected,
                 id="404-com-frase-do-400"),
    pytest.param("sendRichMessage", 403, ERRORS["blocked"][1], ChatUnreachable,
                 id="403-bloqueio-como-no-envio-de-texto"),
    pytest.param("editMessageText", 502, "Bad Gateway", TelegramUnavailable, id="502-como-no-envio-de-texto"),
]


@pytest.mark.parametrize("method, status, description, expected", CLASSIFIED)
def test_400_descriptions_select_the_new_subclasses(rich_bot_api: Any, make_api: _Apis, method: str,
                                                    status: int, description: str,
                                                    expected: type[TelegramError]) -> None:
    """Só no **400**, a
    descrição (sem diferenciar maiúsculas) separa `MessageNotModified` ("message is not modified"),
    `EditTargetUnavailable` ("message to edit not found" ou "message can't be edited") e
    `CallbackQueryExpired` ("query is too old"); os demais 400 continuam `RequestRejected` (tipo
    exato), e a mesma frase num 403 ou 404 não muda a classificação. Depois de **uma** chamada (o
    adaptador não repete); a exceção leva uma frase fixa, sem a descrição do Telegram, e sem
    `__cause__`/`__context__`. A hierarquia das três (subclasses de `RequestRejected`, sem relação
    entre si) é afirmada aqui."""
    for cls in (MessageNotModified, EditTargetUnavailable, CallbackQueryExpired):
        assert issubclass(cls, RequestRejected) and issubclass(cls, TelegramError)
        others = {MessageNotModified, EditTargetUnavailable, CallbackQueryExpired} - {cls}
        assert not any(issubclass(cls, other) for other in others)
    assert not issubclass(RequestRejected, (MessageNotModified, EditTargetUnavailable,
                                            CallbackQueryExpired))

    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 8)
    api = make_api(rich.token_of(bot))
    target = api.send_message(chat, "Questão")
    rich.fail(method, status=status, description=description)
    before = len(rich.calls(method, bot_id=bot))

    with pytest.raises(TelegramError) as info:
        _call_method(api, method, chat, target)

    assert type(info.value) is expected
    calls = rich.calls(method, bot_id=bot)[before:]
    assert [(c.outcome, c.status, c.description) for c in calls] == [("error", status, description)]
    assert info.value.__cause__ is None and info.value.__context__ is None
    lowered = description.lower()
    for text in _exception_texts(info.value):
        assert lowered not in text.lower()
        assert "bad request" not in text.lower()


def test_same_class_keeps_one_fixed_message(rich_bot_api: Any, make_api: _Apis) -> None:
    """a mensagem de cada exceção nova é fixa: as duas descrições que dão
    `EditTargetUnavailable` produzem o mesmo `str()`, e as três classes novas têm mensagens próprias
    (sem a descrição)."""
    rich = rich_bot_api
    bot, _user, chat = _setup(rich, 9)
    api = make_api(rich.token_of(bot))
    target = api.send_message(chat, "Questão")
    messages: dict[str, list[str]] = {}
    for key, method in (("edit_not_found", "editMessageText"), ("cant_be_edited", "editMessageText"),
                        ("not_modified", "editMessageText"), ("query_too_old", "answerCallbackQuery")):
        status, description = DISPATCH_ERRORS[key]
        rich.fail(method, status=status, description=description)
        with pytest.raises(RequestRejected) as info:
            _call_method(api, method, chat, target)
        messages.setdefault(type(info.value).__name__, []).append(str(info.value))

    assert set(messages) == {"EditTargetUnavailable", "MessageNotModified", "CallbackQueryExpired"}
    assert len(set(messages["EditTargetUnavailable"])) == 1
    fixed = [messages[name][0] for name in sorted(messages)]
    assert len(set(fixed)) == 3 and all(fixed)


def test_natural_telegram_rejections_are_classified(rich_bot_api: Any, make_api: _Apis) -> None:
    """Com o estado da Bot API falsa (sem
    falha programada), a edição igual dá `MessageNotModified`; a edição de mensagem apagada pelo
    aluno (texto e teclado) e a de uma mensagem do aluno dão `EditTargetUnavailable`; a segunda
    confirmação do mesmo toque, a de um toque expirado e a de um id desconhecido dão
    `CallbackQueryExpired`; a primeira confirmação é aceita. Nenhuma dessas respostas é erro de
    protocolo (a fixture confere)."""
    rich = rich_bot_api
    bot, user, chat = _setup(rich, 10)
    api = make_api(rich.token_of(bot))
    question = api.send_message(chat, "Questão")
    api.edit_message_text(chat, question, text="Questão editada")

    with pytest.raises(MessageNotModified):
        api.edit_message_text(chat, question, text="Questão editada")

    deleted = api.send_message(chat, "Será apagada")
    rich.delete_message(bot, user, deleted)
    with pytest.raises(EditTargetUnavailable):
        api.edit_message_text(chat, deleted, text="Nova")
    with pytest.raises(EditTargetUnavailable):
        api.edit_message_reply_markup(chat, deleted, _KEYBOARD)
    # O "oi" do aluno (`_setup`) é a mensagem 1 do chat: não é do bot.
    with pytest.raises(EditTargetUnavailable):
        api.edit_message_text(chat, 1, text="Nova")

    tap = rich.tap_raw(bot, user, question, "cq-teste:n")
    assert api.answer_callback_query(tap.callback_id) is None
    with pytest.raises(CallbackQueryExpired):
        api.answer_callback_query(tap.callback_id)
    expired = rich.tap_raw(bot, user, question, "cq-teste:c:a")
    rich.expire_callback(expired.callback_id)
    with pytest.raises(CallbackQueryExpired):
        api.answer_callback_query(expired.callback_id)
    with pytest.raises(CallbackQueryExpired):
        api.answer_callback_query("9900000000000000777")

    outcomes = [(c.method, c.outcome, c.status) for c in rich.calls(bot_id=bot)
                if c.method in ("editMessageText", "editMessageReplyMarkup", "answerCallbackQuery")]
    assert outcomes == [
        ("editMessageText", "ok", 200), ("editMessageText", "error", 400),
        ("editMessageText", "error", 400), ("editMessageReplyMarkup", "error", 400),
        ("editMessageText", "error", 400),
        ("answerCallbackQuery", "ok", 200), ("answerCallbackQuery", "error", 400),
        ("answerCallbackQuery", "error", 400), ("answerCallbackQuery", "error", 400),
    ]


def test_retry_after_and_block_on_new_methods_keep_the_base_rules(rich_bot_api: Any,
                                                                make_api: _Apis) -> None:
    """Nos métodos novos, o 429 com `retry_after` continua
    `RetryAfter` com os segundos, e o 403 de bloqueio continua `ChatUnreachable` (o chat bloqueado no
    dublê), como no `sendMessage`."""
    rich = rich_bot_api
    bot, user, chat = _setup(rich, 11)
    api = make_api(rich.token_of(bot))
    question = api.send_message(chat, "Questão")
    rich.fail("editMessageText", retry_after=7)
    with pytest.raises(RetryAfter) as info:
        api.edit_message_text(chat, question, text="Parte 1 de 2")
    assert info.value.seconds == 7

    rich.block_chat(bot, user)
    with pytest.raises(ChatUnreachable):
        api.send_rich_message(chat, {"html": "<p>Questão</p>", "skip_entity_detection": True})
    with pytest.raises(ChatUnreachable):
        api.send_photo(chat, UploadFile("figura.png", "image/png", png_bytes()))


# ---------------------------------------------------------------------------------------------
# `extract_reply_to` e `get_updates_with_replies`
# ---------------------------------------------------------------------------------------------


def _message_update(update_id: int, reply: Any = None, *, with_reply: bool = True) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 41, "date": 1_924_992_000,
        "from": {"id": 5_100_000_031, "is_bot": False, "first_name": "Pessoa"},
        "chat": {"id": 5_100_000_031, "type": "private", "first_name": "Pessoa"},
        "text": "6,5",
    }
    if with_reply:
        message["reply_to_message"] = reply
    return {"update_id": update_id, "message": message}


def _reply(message_id: Any) -> dict[str, Any]:
    return {"message_id": message_id, "date": 1_924_991_000, "text": "Campo 1 de 1",
            "from": {"id": 6_100_000_001, "is_bot": True, "first_name": "Bot",
                     "username": "cq_teste_bot"},
            "chat": {"id": 5_100_000_031, "type": "private"}}


def _callback_update(update_id: int) -> dict[str, Any]:
    return {"update_id": update_id, "callback_query": {
        "id": "9900000000000000001", "data": "cq-teste:n", "chat_instance": "-1",
        "from": {"id": 5_100_000_031, "is_bot": False, "first_name": "Pessoa"},
        "message": {"message_id": 42, "date": 1_924_992_000,
                    "chat": {"id": 5_100_000_031, "type": "private"},
                    "reply_to_message": _reply(40)}}}


EXTRACT_CASES = [
    pytest.param(_message_update(1, _reply(40)), 40, id="resposta-valida"),
    pytest.param(_message_update(2, _reply(2_147_483_647)), 2_147_483_647, id="id-grande"),
    pytest.param(_message_update(3, with_reply=False), None, id="sem-resposta"),
    pytest.param(_message_update(4, _reply(0)), None, id="zero"),
    pytest.param(_message_update(5, _reply(-7)), None, id="negativo"),
    pytest.param(_message_update(6, _reply("40")), None, id="texto"),
    pytest.param(_message_update(7, _reply(40.0)), None, id="float"),
    pytest.param(_message_update(8, _reply(True)), None, id="booleano"),
    pytest.param(_message_update(9, _reply(None)), None, id="nulo"),
    pytest.param(_message_update(10, {"date": 1_924_991_000}), None, id="sem-message-id"),
    pytest.param(_message_update(11, [40]), None, id="reply-nao-e-objeto"),
    pytest.param(_message_update(12, None), None, id="reply-nulo"),
    pytest.param(_callback_update(13), None, id="callback"),
    pytest.param({"update_id": 14, "my_chat_member": {
        "chat": {"id": 5_100_000_031, "type": "private"}, "date": 1_924_992_000,
        "from": {"id": 5_100_000_031, "is_bot": False, "first_name": "Pessoa"},
        "old_chat_member": {"status": "member"}, "new_chat_member": {"status": "kicked"}}},
        None, id="my-chat-member"),
    pytest.param({"update_id": 15, "edited_message": _message_update(15, _reply(40))["message"]},
                 None, id="outro-tipo"),
]


@pytest.mark.parametrize("raw, expected", EXTRACT_CASES)
def test_extract_reply_to_reads_only_a_positive_integer(raw: dict[str, Any], expected: int | None) -> None:
    """`extract_reply_to(raw)` devolve o
    `message.reply_to_message.message_id` do update cru quando ele é inteiro (não booleano) maior que
    0, e `None` em todo o resto: sem resposta, 0, negativo, texto, `float`, booleano, nulo, sem
    `message_id`, `reply_to_message` que não é objeto, `callback_query` (mesmo com
    `message.reply_to_message`), `my_chat_member` e outro tipo. A função é pura (não muda o update).
    Regressão: o `minimize_update` do mesmo update não muda com a resposta — o resultado é
    igual ao do update sem `reply_to_message`, e os `details` de uma mensagem continuam só
    `message_id` e `date`."""
    before = copy.deepcopy(raw)
    result = extract_reply_to(raw)
    assert result == expected
    assert type(result) is (int if expected is not None else type(None))
    assert raw == before

    minimal = minimize_update(raw)
    stripped = copy.deepcopy(raw)
    for kind in ("message", "callback_query"):
        stripped.get(kind, {}).pop("reply_to_message", None)
        stripped.get(kind, {}).get("message", {}).pop("reply_to_message", None)
    assert minimal == minimize_update(stripped)
    if minimal.kind == "message":
        assert set(minimal.details) == {"message_id", "date"}
        assert minimal == MinimalUpdate(update_id=raw["update_id"], kind="message",
                                        chat_id=5_100_000_031, chat_type="private",
                                        sender_id=5_100_000_031, text="6,5", command=None,
                                        details={"message_id": 41, "date": 1_924_992_000})


def test_get_updates_with_replies_returns_the_reply_map_from_one_call(rich_bot_api: Any,
                                                                      make_api: _Apis) -> None:
    """`get_updates_with_replies(offset=None)` faz **uma**
    chamada `getUpdates`, com os mesmos parâmetros do `get_updates` (`timeout`, `limit=100`,
    `allowed_updates`, sem `offset`), e devolve `(updates, respostas)`: os updates minimizados, na
    ordem, iguais aos que `get_updates` devolve em seguida para o mesmo `offset`, e o dicionário
    `update_id → reply_to_message_id` só com os não nulos (a resposta com id 0 e o toque ficam de
    fora). Com `offset`, o parâmetro vai como inteiro e, sem updates novos, o resultado é `([], {})`."""
    rich = rich_bot_api
    bot, user, chat = _setup(rich, 12)
    api = make_api(rich.token_of(bot))
    question = api.send_message(chat, "Questão")
    replied = rich.push_text(bot, user, "6,5", reply_to_message_id=question)
    zero = rich.push_text(bot, user, "resposta", extra={"reply_to_message": {"message_id": 0}})
    tapped = rich.push_callback(bot, user, "cq-teste:n", message_id=question)

    polls_before = len(rich.calls("getUpdates", bot_id=bot))
    updates, replies = api.get_updates_with_replies(offset=None)
    polls = rich.calls("getUpdates", bot_id=bot)[polls_before:]

    assert len(polls) == 1
    assert polls[0].params == {"timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES}
    assert type(updates) is list and type(replies) is dict
    assert [u.update_id for u in updates][-3:] == [replied, zero, tapped]
    assert updates == [minimize_update(raw) for raw in polls[0].result]
    assert replies == {replied: question}
    assert all(type(k) is int and type(v) is int for k, v in replies.items())

    again = api.get_updates(offset=None)
    assert again == updates

    last = updates[-1].update_id
    empty = api.get_updates_with_replies(offset=last + 1)
    assert empty == ([], {})
    assert rich.calls("getUpdates", bot_id=bot)[-1].params == {
        "offset": last + 1, "timeout": 1, "limit": 100, "allowed_updates": ALLOWED_UPDATES}
