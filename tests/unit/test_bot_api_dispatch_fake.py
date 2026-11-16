"""Autoteste da extensão da Bot API falsa para o disparo e a resposta, e paridade com o adaptador
real.


A `DispatchExtension` (`tests/helpers/bot_api_dispatch.py`) e o gancho `install` do dublê (`tests/helpers/
bot_api.py`) são o oráculo dos testes do disparo que enviam botões, edições, mídia ou texto formatado. Se
ela aceitar o que o Telegram recusa (tag fora do modo, `callback_data` longo, edição igual, toque
fora do teclado), os testes do sistema passam no vazio. Cada validação e cada política tem aqui o
seu controle positivo; e, sem a extensão, o dublê continua o original (o autoteste congelado dele não
muda).

**Paridade dublê × adaptador real**:
em cada teste, o `HttpTelegramBotApi` real (`cq_server.adapters.telegram`) manda pelos
métodos novos da porta (`send_message` com os parâmetros novos, `send_rich_message`, `edit_message_text`,
`edit_message_reply_markup`, `answer_callback_query`, `send_photo`, `send_document` e
`get_updates_with_replies`) o mesmo que o teste manda cru: o que a extensão aceita do pedido cru ela
aceita do adaptador, com o mesmo registro e sem desvio novo; e o que ela recusa volta ao adaptador como a
exceção correspondente da porta (`MessageNotModified`, `EditTargetUnavailable`,
`CallbackQueryExpired`, `ChatUnreachable`, `RetryAfter`, `TelegramUnavailable` ou `RequestRejected`), sem a
descrição do Telegram. Assim a validação da extensão e o adaptador não divergem em silêncio.

No esqueleto, os métodos novos do adaptador (e o `send_message` com algum
parâmetro novo) lançam `NotImplementedError`; toda função deste arquivo chama pelo menos um deles.
As asserções sobre o próprio arnês ficam na mesma função de um caso de paridade.
"""

from __future__ import annotations

import ast
import contextlib
import json
import pathlib
import sys
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from cq_server.adapters.telegram import HttpTelegramBotApi
from cq_server.ports.telegram import (
    CallbackQueryExpired,
    ChatUnreachable,
    EditTargetUnavailable,
    MessageNotModified,
    RequestRejected,
    RetryAfter,
    TelegramUnavailable,
    UploadFile,
)
from tests.helpers.bot_api import (
    ERRORS,
    METHODS,
    FakeBotApi,
    Reply,
    SentMessage,
    make_user,
    monotonic,
)
from tests.helpers.bot_api_dispatch import (
    DISPATCH_ERRORS,
    DISPATCH_METHODS,
    Button,
    DispatchExtension,
    parse_markup,
)
from tests.helpers.clock import FakeClock
from tests.helpers.engine import png_bytes

HELPER = pathlib.Path(__file__).resolve().parents[1] / "helpers" / "bot_api_dispatch.py"
QUERY_TOO_OLD = DISPATCH_ERRORS["query_too_old"]


# ---------------------------------------------------------------------------------------------
# Fixtures e auxiliares
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server() -> Iterator[FakeBotApi]:
    fake = FakeBotApi()
    yield fake
    fake.close()


@pytest.fixture
def fake(server: FakeBotApi, clock: FakeClock) -> Iterator[FakeBotApi]:
    """O dublê do módulo, limpo e **sem** extensão a cada teste."""
    server.reset(clock=clock)
    yield server
    server.reset(clock=clock)


@pytest.fixture
def rich(fake: FakeBotApi) -> DispatchExtension:
    """O dublê com a extensão, como a fixture `rich_bot_api` faz."""
    return fake.install(DispatchExtension())


@pytest.fixture
def api(fake: FakeBotApi) -> Iterator[Callable[[str], HttpTelegramBotApi]]:
    """Fábrica do adaptador real contra o dublê do módulo (com ou sem a extensão); fecha todos no fim."""
    created: list[HttpTelegramBotApi] = []

    def make(token: str) -> HttpTelegramBotApi:
        adapter = HttpTelegramBotApi(token, base_url=fake.base_url, poll_timeout_seconds=1,
                                     timeout_seconds=2.0)
        created.append(adapter)
        return adapter

    yield make
    fake.release_hangs()
    for adapter in created:
        with contextlib.suppress(Exception):
            adapter.close()


def _rejected(call: Callable[[], Any], cls: type[Exception] = RequestRejected,
              *, hidden: str | None = None) -> Exception:
    """`call()` levanta exatamente `cls` (não uma subclasse), sem a descrição do Telegram."""
    with pytest.raises(cls) as info:
        call()
    assert type(info.value) is cls, type(info.value)
    if hidden is not None:
        assert hidden not in str(info.value) and hidden not in repr(info.value)
    return info.value


def _envelope(error: tuple[int, str]) -> dict[str, Any]:
    return {"ok": False, "error_code": error[0], "description": error[1]}


def _setup(rich: Any, index: int = 1) -> tuple[int, str, Any]:
    """Um bot e um aluno que já falou com ele (o chat passa a ser conhecido)."""
    bot = rich.add_bot()
    user = make_user(index)
    rich.push_text(bot, user, "oi")
    return bot, rich.token_of(bot), user


def _keyboard(*rows: list[str]) -> dict[str, Any]:
    return {"inline_keyboard": [[{"text": data.upper(), "callback_data": data} for data in row]
                                for row in rows]}


def _rich(html: str, **extra: Any) -> dict[str, Any]:
    return {"html": html, "skip_entity_detection": True, **extra}


def _multipart(fields: dict[str, str], files: dict[str, tuple[str, str, bytes]]) -> tuple[bytes,
                                                                                           str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     .encode() + value.encode("utf-8") + b"\r\n")
    for name, (filename, content_type, data) in files.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                     f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                     + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _upload(rich: Any, token: str, method: str, chat_id: int, name: str, filename: str,
            content_type: str, data: bytes, **fields: str) -> Reply:
    body, content = _multipart({"chat_id": str(chat_id), **fields},
                               {name: (filename, content_type, data)})
    return rich.request(token, method, body=body, content_type=content)


def _send_rich(rich: Any, token: str, chat_id: int, html: str, **extra: Any) -> Reply:
    return rich.request(token, "sendRichMessage", {"chat_id": chat_id, "rich_message": _rich(html),
                                                   **extra})


# ---------------------------------------------------------------------------------------------
# Independência, instalação e o dublê sem extensão
# ---------------------------------------------------------------------------------------------


def test_dispatch_methods_are_a_tuple_of_their_own(rich: Any, api: Any) -> None:
    """Os métodos da extensão numa tupla própria, fora dos `METHODS` originais; a extensão, como o
    dublê, só usa a biblioteca padrão e o próprio dublê (nada de `cq_server` nem HTTP de terceiros).
    Paridade: os métodos da Bot API que os métodos novos do adaptador real usam são exatamente os que a
    extensão assume (`DispatchExtension.handles`), e todos os pedidos dele são aceitos sem desvio."""
    tree = ast.parse(HELPER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    outside = {name for name in imported
               if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"}
    assert outside == {"tests.helpers.bot_api"}
    assert not any(name.startswith("cq_") for name in imported)
    assert DISPATCH_METHODS == ("sendRichMessage", "editMessageText", "editMessageReplyMarkup",
                          "sendPhoto", "sendDocument")
    assert not set(DISPATCH_METHODS) & set(METHODS)
    assert DispatchExtension.upload_methods == {"sendPhoto", "sendDocument"}
    assert DispatchExtension.handles == set(DISPATCH_METHODS) | {"sendMessage", "answerCallbackQuery"}

    bot, token, user = _setup(rich)
    adapter = api(token)
    chat = user.user_id
    sent = adapter.send_rich_message(chat, _rich("<p>q</p>"), reply_markup=_keyboard(["cq1:a"]))
    adapter.send_message(chat, "Campo 1 de 1: <b>x</b>", parse_mode="HTML",
                         reply_markup={"force_reply": True, "input_field_placeholder": "Número"},
                         reply_parameters={"message_id": sent, "allow_sending_without_reply": True})
    adapter.edit_message_text(chat, sent, rich_message=_rich("<p>q ✅</p>"), reply_markup=_keyboard(["cq1:b"]))
    adapter.edit_message_reply_markup(chat, sent, _keyboard(["cq1:c"]))
    tap = rich.tap(bot, user, sent, "cq1:c")
    adapter.answer_callback_query(tap.callback_id, text="Resposta registrada.")
    adapter.send_photo(chat, UploadFile("figura.png", "image/png", png_bytes()), caption="Figura 1")
    adapter.send_document(chat, UploadFile("tabela.pdf", "application/pdf", b"%PDF-1.4"))
    assert {c.method for c in rich.calls()} == DispatchExtension.handles
    assert rich.protocol_errors() == [] and rich.policy_errors() == []


def test_without_extension_the_fake_is_the_base_one(fake: FakeBotApi, api: Any) -> None:
    """Sem `install`, os métodos da extensão e um método inventado (`metodoInexistente`) dão 404, o
    multipart é erro de protocolo, `fail`/`on_call` recusam os métodos da extensão e `after_effect`.
    Paridade: sem a extensão, o método novo do adaptador real é recusado como método desconhecido (404 →
    `RequestRejected`, sem as subclasses novas), e o `sendMessage` original dele segue aceito."""
    assert fake.extension is None
    bot = fake.add_bot()
    token = fake.token_of(bot)
    for method in (*DISPATCH_METHODS, "metodoInexistente"):
        reply = fake.request(token, method, {})
        assert (reply.status, reply.json()) == (404, _envelope(ERRORS["method_not_found"]))
    body, content = _multipart({"chat_id": "1"}, {"photo": ("a.png", "image/png", b"x")})
    assert fake.request(token, "sendPhoto", body=body, content_type=content).status == 404
    assert fake.protocol_errors() == [
        *[f"{m}: método desconhecido ou fora do dublê" for m in (*DISPATCH_METHODS, "metodoInexistente")],
        "sendPhoto: corpo multipart/form-data (esperado JSON)",
        "sendPhoto: método desconhecido ou fora do dublê",
    ]
    user = make_user(5)
    fake.push_text(bot, user, "oi")
    adapter = api(token)
    _rejected(lambda: adapter.send_rich_message(user.user_id, _rich("<p>x</p>")))
    assert fake.protocol_errors()[-1] == "sendRichMessage: método desconhecido ou fora do dublê"
    assert isinstance(adapter.send_message(user.user_id, "Olá!"), int)
    assert len(fake.protocol_errors()) == len(DISPATCH_METHODS) + 4
    for method in (*DISPATCH_METHODS, "metodoInexistente"):
        with pytest.raises(ValueError):
            fake.fail(method, status=500)
        with pytest.raises(ValueError):
            fake.fail_network(method, kind="reset")
        with pytest.raises(ValueError):
            fake.on_call(method, lambda call: None)
    with pytest.raises(ValueError):
        fake.fail("sendMessage", status=502, after_effect=True)
    fake.fail("sendMessage", status=502)  # o dublê original continua valendo


def test_install_is_undone_by_reset_and_cannot_be_doubled(fake: FakeBotApi, clock: FakeClock,
                                                          api: Any) -> None:
    extension = fake.install(DispatchExtension())
    assert fake.extension is extension
    with pytest.raises(ValueError):
        fake.install(DispatchExtension())
    bot = extension.add_bot()  # os atributos do dublê passam pela extensão
    assert extension.token_of(bot) == fake.token_of(bot)
    fake.reset(clock=clock)
    assert fake.extension is None
    bot = fake.add_bot()
    assert fake.request(fake.token_of(bot), "sendRichMessage", {}).status == 404
    # Paridade: o adaptador real vê a extensão sair e voltar (404 → `RequestRejected`; depois, aceito).
    user = make_user(4)
    fake.push_text(bot, user, "oi")
    adapter = api(fake.token_of(bot))
    _rejected(lambda: adapter.send_rich_message(user.user_id, _rich("<p>x</p>")))
    fake.install(DispatchExtension())
    assert fake.request(fake.token_of(bot), "sendRichMessage", {}).status == 400
    message_id = adapter.send_rich_message(user.user_id, _rich("<p>x</p>"))
    assert fake.extension.current(bot, user.user_id, message_id).text == "<p>x</p>"


def test_with_extension_an_invented_method_is_still_404(rich: Any, api: Any) -> None:
    """Com a extensão, o método inventado continua 404; `fail`/`fail_network`/`on_call` aceitam os
    métodos da extensão. Paridade: as falhas programadas nos métodos da extensão chegam ao adaptador
    real como as do dublê original (5xx e rede → `TelegramUnavailable`)."""
    bot, token, user = _setup(rich)
    reply = rich.request(token, "metodoInexistente", {})
    assert (reply.status, reply.json()) == (404, _envelope(ERRORS["method_not_found"]))
    assert rich.protocol_errors() == ["metodoInexistente: método desconhecido ou fora do dublê"]
    with pytest.raises(ValueError):
        rich.fail("metodoInexistente", status=500)
    rich.fail("editMessageText", status=500)  # o método da extensão é aceito
    rich.fail_network("sendPhoto", kind="reset")
    rich.on_call("sendRichMessage", lambda call: None)
    adapter = api(token)
    sent = adapter.send_rich_message(user.user_id, _rich("<p>q</p>"))
    _rejected(lambda: adapter.edit_message_text(user.user_id, sent, rich_message=_rich("<p>z</p>")),
              TelegramUnavailable)
    _rejected(lambda: adapter.send_photo(user.user_id, UploadFile("a.png", "image/png", png_bytes())),
              TelegramUnavailable)
    assert rich.current(bot, user.user_id, sent).text == "<p>q</p>" and rich.uploads() == []
    rich.clear_failures()
    assert rich.policy_errors() == []


# ---------------------------------------------------------------------------------------------
# sendMessage com a extensão
# ---------------------------------------------------------------------------------------------


def test_plain_send_message_answers_like_the_base_fake(fake: FakeBotApi, clock: FakeClock, api: Any) -> None:
    """O `sendMessage` original (`{chat_id, text}`) sai igual com e sem a extensão: resultado,
    `sent_messages()` e os mesmos textos de erro. Paridade: com a extensão, o `send_message` do adaptador
    real com os parâmetros novos (`parse_mode`, `link_preview_options`) é aceito e registrado no dialeto
    HTML, sem desvio novo."""
    def run() -> tuple[Any, ...]:
        bot = fake.add_bot()
        token = fake.token_of(bot)
        user = make_user(3)
        missing = fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": "x"})
        fake.push_text(bot, user, "oi")
        ok = fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": "Olá!"})
        empty = fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": ""})
        long = fake.request(token, "sendMessage", {"chat_id": user.user_id, "text": "x" * 4097})
        result = ok.json()["result"]
        result["from"].pop("username")
        result["from"].pop("id")
        sent = [(m.chat_id, m.message_id, m.text, m.clock_at) for m in fake.sent_messages(bot)]
        return (missing.json(), result, empty.json(), long.json(), sent, fake.protocol_errors())

    plain = run()
    fake.reset(clock=clock)
    fake.install(DispatchExtension())
    assert run() == plain
    assert plain[2:4] == (_envelope(DISPATCH_ERRORS["text_empty"]), _envelope(DISPATCH_ERRORS["too_long"]))
    assert plain[5] == ["sendMessage: text vazio",
                        "sendMessage: text com mais de 4096 unidades UTF-16"]
    bot = fake.add_bot()
    user = make_user(8)
    fake.push_text(bot, user, "oi")
    adapter = api(fake.token_of(bot))
    message_id = adapter.send_message(user.user_id, "<b>Olá</b>", parse_mode="HTML",
                                      link_preview_options={"is_disabled": True})
    message = fake.extension.current(bot, user.user_id, message_id)
    assert (message.text_format, message.text, message.parse_mode) == ("html", "<b>Olá</b>", "HTML")
    assert fake.calls("sendMessage")[-1].params == {"chat_id": user.user_id, "text": "<b>Olá</b>",
                                                    "parse_mode": "HTML",
                                                    "link_preview_options": {"is_disabled": True}}
    assert fake.protocol_errors() == plain[5]


def test_html_send_message_counts_after_parsing(rich: Any, api: Any) -> None:
    """Texto: 1 a 4096 unidades UTF-16 **depois do parsing**; `parse_mode` fora de HTML é protocolo.
    Paridade: o adaptador real manda o mesmo texto no limite (aceito, com o mesmo registro) e, acima
    dele, recebe `RequestRejected` sem a descrição."""
    bot, token, user = _setup(rich)
    text = "<b>" + "a" * 4090 + "</b>&amp;&lt;&gt;&quot;&#65;x"  # 4096 depois do parsing
    reply = rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": text,
                                                "parse_mode": "HTML"})
    assert reply.status == 200
    assert reply.json()["result"]["text"] == "a" * 4090 + "&<>\"Ax"
    [message] = rich.messages(bot)
    assert (message.text_format, message.text, message.parse_mode) == ("html", text, "HTML")
    assert rich.sent_messages(bot) == [SentMessage(bot, user.user_id, message.message_id, text,
                                                   message.clock_at, message.call_seq)]
    reply = rich.request(token, "sendMessage", {"chat_id": user.user_id, "parse_mode": "HTML",
                                                "text": "<b>" + "a" * 4097 + "</b>"})
    assert reply.json() == _envelope(DISPATCH_ERRORS["too_long"])
    reply = rich.request(token, "sendMessage", {"chat_id": user.user_id, "parse_mode": "HTML",
                                                "text": "<b></b>"})
    assert reply.json() == _envelope(DISPATCH_ERRORS["text_empty"])
    assert rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": "*x*",
                                               "parse_mode": "MarkdownV2"}).status == 200
    assert rich.protocol_errors() == [
        "sendMessage: text com mais de 4096 unidades UTF-16 depois do parsing",
        "sendMessage: text vazio depois do parsing",
        "sendMessage: parse_mode 'MarkdownV2' (o sistema só usa HTML)",
    ]
    adapter = api(token)
    message_id = adapter.send_message(user.user_id, text, parse_mode="HTML")
    copy = rich.current(bot, user.user_id, message_id)
    assert (copy.text_format, copy.text, copy.plain_text) == ("html", text, message.plain_text)
    _rejected(lambda: adapter.send_message(user.user_id, "<b>" + "a" * 4097 + "</b>", parse_mode="HTML"),
              hidden=DISPATCH_ERRORS["too_long"][1])
    assert rich.protocol_errors()[3:] == ["sendMessage: text com mais de 4096 unidades UTF-16 depois do parsing"]


@pytest.mark.parametrize(("text", "detail"), [
    ("<p>x</p>", 'Unsupported start tag "p" at byte offset 0'),
    ("<br>", 'Unsupported start tag "br" at byte offset 0'),
    ("<table><tr><td>x</td></tr></table>", 'Unsupported start tag "table" at byte offset 0'),
    ("<tg-math>x</tg-math>", 'Unsupported start tag "tg-math" at byte offset 0'),
    ("é < b", 'Unexpected "<" at byte offset 3'),
    ("a & b", 'Unexpected "&" at byte offset 2'),
    ("a &nbsp; b", 'Unsupported HTML entity "&nbsp;" at byte offset 2'),
    ("<b>x", 'Can\'t find end tag corresponding to start tag "b"'),
    ("<b><i>x</b></i>", 'Unmatched end tag at byte offset 7, expected "</i>", found "</b>"'),
    ("x</b>", 'Unmatched end tag at byte offset 1, found "</b>"'),
    ('<span class="x">y</span>', 'Tag "span" must have class "tg-spoiler" at byte offset 0'),
    ("<pre><b>x</b></pre>", 'Unsupported start tag "b" inside "pre" at byte offset 5'),
])
def test_html_mode_rejects_what_telegram_cannot_parse(rich: Any, api: Any, text: str, detail: str) -> None:
    """HTML simples: só as tags e as entidades do modo; `<` e `&` soltos → 400 `can't parse
    entities` e erro de protocolo. Paridade: o mesmo texto pelo adaptador real é `RequestRejected`, sem a
    descrição."""
    _, token, user = _setup(rich)
    reply = rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": text,
                                                "parse_mode": "HTML"})
    description = "Bad Request: can't parse entities: " + detail
    assert (reply.status, reply.json()) == (400, _envelope((400, description)))
    assert rich.protocol_errors() == [f"sendMessage: text: {description}"]
    assert rich.messages() == []
    _rejected(lambda: api(token).send_message(user.user_id, text, parse_mode="HTML"), hidden=detail)
    assert rich.protocol_errors() == [f"sendMessage: text: {description}"] * 2
    assert rich.messages() == []


def test_html_mode_accepts_its_tags_and_flags_what_telegram_only_fixes(rich: Any, api: Any) -> None:
    """HTML simples e o controle positivo: as tags e entidades do modo passam; `code` dentro de negrito,
    `blockquote` aninhado, linguagem fora de `pre` e atributo fora da lista só vão ao protocolo.
    Paridade: o texto com todas as tags do modo, pelo adaptador real, é aceito sem desvio."""
    bot, token, user = _setup(rich)
    ok = ('<b>b</b><strong>s</strong><i>i</i><em>e</em><u>u</u><ins>n</ins><s>s</s>'
          '<strike>k</strike><del>d</del><tg-spoiler>p</tg-spoiler>'
          '<span class="tg-spoiler">p</span><code>c</code>'
          '<pre><code class="language-python">x = 1</code></pre><blockquote>q</blockquote>'
          '<blockquote expandable>e</blockquote> &lt;&gt;&amp;&quot;&#233;&#xE9; >')
    assert rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": ok,
                                               "parse_mode": "HTML"}).status == 200
    assert rich.protocol_errors() == []
    message_id = api(token).send_message(user.user_id, ok, parse_mode="HTML")
    assert rich.current(bot, user.user_id, message_id).text == ok
    assert rich.protocol_errors() == []
    fixed = ('<b><code>x</code></b><blockquote><blockquote>y</blockquote></blockquote>'
             '<code class="language-c">z</code><b style="x">w</b>')
    assert rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": fixed,
                                               "parse_mode": "HTML"}).status == 200
    assert rich.protocol_errors() == [
        "sendMessage: text: <code> dentro de negrito, itálico, sublinhado, tachado ou spoiler",
        "sendMessage: text: <blockquote> aninhado",
        "sendMessage: text: linguagem em <code> fora de <pre>",
        'sendMessage: text: atributo "style" fora da lista em <b>',
    ]


# ---------------------------------------------------------------------------------------------
# sendRichMessage
# ---------------------------------------------------------------------------------------------


def test_rich_message_is_accepted_and_recorded(rich: Any, clock: FakeClock, api: Any) -> None:
    """O Rich com teclado é aceito e registrado. Paridade: o `send_rich_message` do adaptador real posta
    os mesmos parâmetros do pedido cru, e a extensão registra a mesma mensagem."""
    bot, token, user = _setup(rich)
    html = ("<h2>Questão</h2><p>Quanto é <tg-math>x^2</tg-math>?&nbsp;&hellip;</p>"
            "<table><tr><th>a</th><td colspan=\"2\">b</td></tr></table><hr/>"
            "<ul><li>um</li></ul><tg-math-block>E = mc^2</tg-math-block>")
    reply = _send_rich(rich, token, user.user_id, html, reply_markup=_keyboard(["a", "b"]))
    assert reply.status == 200
    result = reply.json()["result"]
    assert set(result) == {"message_id", "from", "chat", "date", "reply_markup"}
    assert result["date"] == int(clock.now().timestamp())
    [message] = rich.messages(bot, user.user_id)
    assert message == rich.current(bot, user.user_id, result["message_id"])
    assert (message.method, message.text_format, message.text) == (
        "sendRichMessage", "rich_html", html)
    assert message.plain_text == "QuestãoQuanto é x^2? …abumE = mc^2"
    assert message.keyboard == ((Button("A", "a"), Button("B", "b")),)
    assert message.clock_at == clock.now() and message.edit_count == 0
    assert rich.protocol_errors() == [] and rich.policy_errors() == []
    assert rich.sent_messages() == []  # `sent_messages` é só do sendMessage
    via = api(token).send_rich_message(user.user_id, _rich(html), reply_markup=_keyboard(["a", "b"]))
    copy = rich.current(bot, user.user_id, via)
    assert (copy.method, copy.text_format, copy.text, copy.plain_text, copy.keyboard) == (
        message.method, message.text_format, message.text, message.plain_text, message.keyboard)
    raw_call, adapter_call = rich.calls("sendRichMessage")
    assert adapter_call.params == raw_call.params
    assert rich.protocol_errors() == [] and rich.policy_errors() == []


def test_rich_message_without_skip_entity_detection_is_a_policy_error(rich: Any, api: Any) -> None:
    """O Rich sempre com `skip_entity_detection: true` (controle positivo: ausente e
    falso). Paridade: o adaptador real repassa o `rich_message` como recebeu: com a
    marca, nenhuma acusação; sem ela, a mesma acusação do pedido cru (o adaptador não conserta nem
    esconde)."""
    _, token, user = _setup(rich)
    for rich_message in ({"html": "<p>x</p>"}, {"html": "<p>x</p>",
                                                 "skip_entity_detection": False}):
        assert rich.request(token, "sendRichMessage", {"chat_id": user.user_id,
                                                       "rich_message": rich_message}).status == 200
    assert rich.policy_errors() == [
        "sendRichMessage: Rich sem skip_entity_detection: true"] * 2
    assert rich.protocol_errors() == []
    adapter = api(token)
    assert isinstance(adapter.send_rich_message(user.user_id, _rich("<p>x</p>")), int)
    assert len(rich.policy_errors()) == 2
    adapter.send_rich_message(user.user_id, {"html": "<p>x</p>"})
    assert rich.calls("sendRichMessage")[-1].params["rich_message"] == {"html": "<p>x</p>"}
    assert rich.policy_errors() == [
        "sendRichMessage: Rich sem skip_entity_detection: true"] * 3
    assert rich.protocol_errors() == []


@pytest.mark.parametrize(("html", "error", "problem"), [
    ("<p>" + "x" * 32_769 + "</p>", "rich_too_long",
     "Rich com mais de 32768 unidades UTF-16 depois do parsing"),
    ("<ul>" + "<li>x</li>" * 500 + "</ul>", "rich_blocks", "Rich com 501 blocos (máximo 500)"),
    ("<b>" * 17 + "x" + "</b>" * 17, "rich_depth", "Rich com 17 níveis (máximo 16)"),
    ("<table><tr>" + "<td>x</td>" * 21 + "</tr></table>", "rich_columns",
     "tabela com 21 colunas (máximo 20)"),
    ('<table><tr><td colspan="20">x</td><td>y</td></tr></table>', "rich_columns",
     "tabela com 21 colunas (máximo 20)"),
], ids=["texto", "blocos", "niveis", "colunas", "colspan"])
def test_rich_limits_are_enforced(rich: Any, api: Any, html: str, error: str, problem: str) -> None:
    """Limites do Rich: 32 768 unidades, 500 blocos, 16 níveis e 20 colunas (controle positivo no limite).
    Paridade: pelo adaptador real, o limite é aceito e o excesso é `RequestRejected`, sem a descrição."""
    _, token, user = _setup(rich)
    reply = _send_rich(rich, token, user.user_id, html)
    assert (reply.status, reply.json()) == (400, _envelope(DISPATCH_ERRORS[error]))
    assert rich.protocol_errors() == [f"sendRichMessage: {problem}"]
    at_limit = {"rich_too_long": "<p>" + "x" * 32_768 + "</p>",
                "rich_blocks": "<ul>" + "<li>x</li>" * 499 + "</ul>",
                "rich_depth": "<b>" * 16 + "x" + "</b>" * 16,
                "rich_columns": "<table><tr>" + "<td>x</td>" * 20 + "</tr></table>"}[error]
    assert _send_rich(rich, token, user.user_id, at_limit).status == 200
    assert len(rich.protocol_errors()) == 1
    adapter = api(token)
    assert isinstance(adapter.send_rich_message(user.user_id, _rich(at_limit)), int)
    _rejected(lambda: adapter.send_rich_message(user.user_id, _rich(html)), hidden=DISPATCH_ERRORS[error][1])
    assert rich.protocol_errors() == [f"sendRichMessage: {problem}"] * 2


@pytest.mark.parametrize(("html", "detail"), [
    ("<table><thead><tr><th>x</th></tr></thead></table>",
     'Unsupported start tag "thead" at byte offset 7'),
    ("<div>x</div>", 'Unsupported start tag "div" at byte offset 0'),
    ("<p>1 < 2</p>", 'Unexpected "<" at byte offset 5'),
    ("<p>a & b</p>", 'Unexpected "&" at byte offset 5'),
    ("<p>&copy;</p>", 'Unsupported HTML entity "&copy;" at byte offset 3'),
    ("<tg-math>x<b>y</b></tg-math>", 'Unsupported start tag "b" inside "tg-math" at byte offset 10'),
])
def test_rich_mode_rejects_what_telegram_cannot_parse(rich: Any, api: Any, html: str, detail: str) -> None:
    """Rich: "Only the tags mentioned above are currently supported" (sem `thead`/`tbody`/`div`).
    Paridade: pelo adaptador real, `RequestRejected` sem a descrição."""
    _, token, user = _setup(rich)
    reply = _send_rich(rich, token, user.user_id, html)
    description = "Bad Request: can't parse entities: " + detail
    assert reply.json() == _envelope((400, description))
    assert rich.protocol_errors() == [f"sendRichMessage: rich_message.html: {description}"]
    _rejected(lambda: api(token).send_rich_message(user.user_id, _rich(html)), hidden=detail)
    assert rich.protocol_errors() == [f"sendRichMessage: rich_message.html: {description}"] * 2


def test_rich_message_shape_is_checked(rich: Any, api: Any) -> None:
    """A forma do `sendRichMessage` é conferida. Paridade: o adaptador real posta só os parâmetros não
    nulos (`chat_id` e `rich_message`), na forma que a extensão aceita sem desvio."""
    _, token, user = _setup(rich)
    cases = [
        {"chat_id": user.user_id},
        {"chat_id": user.user_id, "rich_message": {"markdown": "*x*",
                                                   "skip_entity_detection": True}},
        {"chat_id": user.user_id, "rich_message": {"html": "<p>x</p>", "markdown": "x",
                                                   "skip_entity_detection": True}},
        {"chat_id": user.user_id, "rich_message": _rich("<p>x</p>", extra=1)},
    ]
    statuses = [rich.request(token, "sendRichMessage", params).status for params in cases]
    assert statuses == [400, 200, 400, 200]
    assert rich.protocol_errors() == [
        "sendRichMessage: parâmetro obrigatório ausente: 'rich_message'",
        "sendRichMessage: rich_message com markdown (o sistema só usa html)",
        "sendRichMessage: rich_message sem exatamente um de html, markdown ou blocks",
        "sendRichMessage: rich_message com campo não documentado: 'extra'",
    ]
    api(token).send_rich_message(user.user_id, _rich("<p>x</p>"))
    assert rich.calls("sendRichMessage")[-1].params == {"chat_id": user.user_id,
                                                        "rich_message": _rich("<p>x</p>")}
    assert len(rich.protocol_errors()) == 4


def test_parse_markup_measures_blocks_depth_and_columns(rich: Any, api: Any) -> None:
    """`parse_markup` mede texto plano, blocos, profundidade e colunas. Paridade: o texto plano medido é o
    que a extensão registra da mesma marcação enviada pelo adaptador real."""
    markup = ('solto <b>x</b><p>a</p><table><tr><td colspan="3">b</td><td>c</td>'
              "</tr></table>")
    measured = parse_markup('solto <b>x</b><p>a</p><table><tr><td colspan="3">b</td><td>c</td>'
                            "</tr></table>", "rich")
    assert (measured.error, measured.problems) == (None, [])
    assert (measured.plain, measured.blocks, measured.depth, measured.columns) == (
        "solto xabc", 4, 3, 4)
    assert parse_markup("<p>a<br>b</p>", "rich").plain == "a\nb"
    with pytest.raises(ValueError):
        parse_markup("x", "markdown")
    bot, token, user = _setup(rich)
    adapter = api(token)
    assert rich.current(bot, user.user_id, adapter.send_rich_message(
        user.user_id, _rich(markup))).plain_text == measured.plain
    assert rich.current(bot, user.user_id, adapter.send_rich_message(
        user.user_id, _rich("<p>a<br>b</p>"))).plain_text == "a\nb"
    assert rich.protocol_errors() == []


# ---------------------------------------------------------------------------------------------
# Teclados, pedido de campo e resposta
# ---------------------------------------------------------------------------------------------


def test_callback_data_must_have_1_to_64_bytes(rich: Any, api: Any) -> None:
    """`callback_data`: 1 a 64 **bytes** UTF-8 (controle positivo: 64 bytes, e 32 caracteres de 2 bytes).
    Paridade: pelo adaptador real, 64 bytes são aceitos e 65 são `RequestRejected`."""
    _, token, user = _setup(rich)

    def send(data: Any) -> Reply:
        markup = {"inline_keyboard": [[{"text": "x", "callback_data": data}]]}
        return _send_rich(rich, token, user.user_id, "<p>x</p>", reply_markup=markup)

    assert send("a" * 64).status == 200
    assert send("é" * 32).status == 200
    for data in ("a" * 65, "é" * 33, ""):
        assert send(data).json() == _envelope(DISPATCH_ERRORS["button_data_invalid"])
    assert rich.protocol_errors() == [
        "sendRichMessage: inline_keyboard[0][0].callback_data fora de 1 a 64 bytes UTF-8"] * 3
    adapter = api(token)

    def via(data: str) -> int:
        return adapter.send_rich_message(user.user_id, _rich("<p>x</p>"),
                                         reply_markup={"inline_keyboard": [[{"text": "x", "callback_data": data}]]})

    assert isinstance(via("é" * 32), int)
    _rejected(lambda: via("a" * 65))
    assert rich.protocol_errors() == [
        "sendRichMessage: inline_keyboard[0][0].callback_data fora de 1 a 64 bytes UTF-8"] * 4


def test_inline_keyboard_shape_is_checked(rich: Any, api: Any) -> None:
    """A forma do teclado inline é conferida. Paridade: pelo adaptador real, o botão com `style` é aceito
    sem desvio e o botão com `url` é `RequestRejected`, com as mesmas acusações."""
    _, token, user = _setup(rich)
    markups = [
        {"inline_keyboard": [[{"text": "x", "url": "https://t.me"}]]},
        {"inline_keyboard": [[{"text": "", "callback_data": "a"}]]},
        {"inline_keyboard": [[{"text": "x", "callback_data": "a", "style": "success"}]]},
        {"inline_keyboard": [[{"text": "x", "callback_data": "a"}]], "force_reply": True},
        {"keyboard": [[{"text": "x"}]]},
        {"inline_keyboard": []},
    ]
    statuses = [_send_rich(rich, token, user.user_id, "<p>x</p>", reply_markup=m).status
                for m in markups]
    assert statuses == [400, 400, 200, 200, 200, 200]
    assert rich.protocol_errors() == [
        "sendRichMessage: inline_keyboard[0][0] com url (o sistema só usa callback_data)",
        "sendRichMessage: inline_keyboard[0][0] sem callback_data",
        "sendRichMessage: inline_keyboard[0][0].text vazio ou não é texto",
        "sendRichMessage: InlineKeyboardMarkup.force_reply (a questão usa o "
        "pedido de campo)",
        "sendRichMessage: teclado de resposta (ReplyKeyboardMarkup/Remove) fora do uso do sistema",
    ]
    before = rich.protocol_errors()
    adapter = api(token)
    assert isinstance(adapter.send_rich_message(user.user_id, _rich("<p>x</p>"), reply_markup=markups[2]), int)
    assert rich.protocol_errors() == before
    _rejected(lambda: adapter.send_rich_message(user.user_id, _rich("<p>x</p>"), reply_markup=markups[0]))
    assert rich.protocol_errors()[len(before):] == before[:2]


def test_force_reply_placeholder_must_have_1_to_64(rich: Any, api: Any) -> None:
    """`input_field_placeholder` de 1 a 64; o pedido de campo guarda o `ForceReply`. Paridade: o
    pedido de campo do adaptador real (`send_message` com `reply_markup`) é guardado igual, e o
    `placeholder` longo é `RequestRejected`."""
    bot, token, user = _setup(rich)

    def ask(hint: str) -> Reply:
        return rich.request(token, "sendMessage", {
            "chat_id": user.user_id, "text": "Campo 1 de 2: <b>x</b>", "parse_mode": "HTML",
            "reply_markup": {"force_reply": True, "input_field_placeholder": hint}})

    reply = ask("h" * 64)
    assert reply.status == 200
    message = rich.current(bot, user.user_id, reply.json()["result"]["message_id"])
    assert (message.force_reply, message.placeholder, message.keyboard) == (True, "h" * 64, None)
    for hint in ("h" * 65, ""):
        assert ask(hint).json() == _envelope(DISPATCH_ERRORS["placeholder"])
    assert rich.protocol_errors() == ["sendMessage: input_field_placeholder fora de 1 a 64"] * 2
    adapter = api(token)

    def ask_via(hint: str) -> int:
        return adapter.send_message(user.user_id, "Campo 1 de 2: <b>x</b>", parse_mode="HTML",
                                    reply_markup={"force_reply": True, "input_field_placeholder": hint})

    via = rich.current(bot, user.user_id, ask_via("h" * 64))
    assert (via.force_reply, via.placeholder, via.keyboard) == (True, "h" * 64, None)
    _rejected(lambda: ask_via("h" * 65))
    assert rich.protocol_errors() == ["sendMessage: input_field_placeholder fora de 1 a 64"] * 3


def test_reply_parameters_policy_and_missing_target(rich: Any, api: Any) -> None:
    """`reply_parameters`, a resposta a mensagem inexistente e a política do sistema: `allow_sending_without_reply: true` sempre. Paridade: a
    resposta do adaptador real com `reply_parameters` cumpre a política e fica ligada à mensagem; sem o
    alvo, sai sem a ligação."""
    bot, token, user = _setup(rich)
    first = _send_rich(rich, token, user.user_id, "<p>q</p>").json()["result"]["message_id"]

    def send(reply_parameters: dict[str, Any]) -> Reply:
        return rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": "r",
                                                   "reply_parameters": reply_parameters})

    ok = send({"message_id": first, "allow_sending_without_reply": True})
    assert ok.json()["result"]["reply_to_message"] == {"message_id": first}
    assert rich.current(bot, user.user_id, ok.json()["result"]["message_id"]
                        ).reply_to_message_id == first
    gone = send({"message_id": 999, "allow_sending_without_reply": True})
    assert gone.status == 200 and "reply_to_message" not in gone.json()["result"]
    assert rich.policy_errors() == []
    adapter = api(token)
    linked = adapter.send_message(user.user_id, "r",
                                  reply_parameters={"message_id": first, "allow_sending_without_reply": True})
    assert rich.current(bot, user.user_id, linked).reply_to_message_id == first
    unlinked = adapter.send_message(user.user_id, "r",
                                    reply_parameters={"message_id": 999, "allow_sending_without_reply": True})
    assert rich.current(bot, user.user_id, unlinked).reply_to_message_id is None
    assert rich.policy_errors() == []
    assert send({"message_id": first}).status == 200
    assert send({"message_id": 999}).json() == _envelope(DISPATCH_ERRORS["reply_not_found"])
    rich.delete_message(bot, user, first)
    assert send({"message_id": first, "allow_sending_without_reply": False}).json() == _envelope(
        DISPATCH_ERRORS["reply_not_found"])
    assert rich.policy_errors() == [
        "sendMessage: reply_parameters sem allow_sending_without_reply: true"] * 3
    assert rich.protocol_errors() == []


def test_blocked_and_unknown_chats(rich: Any, api: Any) -> None:
    """403 no chat bloqueado e 400 `chat not found`, como o `sendMessage` original, também no Rich e
    na edição. Paridade: pelo adaptador real, o chat desconhecido é `RequestRejected` e o bloqueado é
    `ChatUnreachable`, no envio e na edição, sem mensagem nem edição nova."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>x</p>").json()["result"]["message_id"]
    stranger = make_user(99)
    assert _send_rich(rich, token, stranger.user_id, "<p>x</p>").json() == _envelope(
        ERRORS["chat_not_found"])
    adapter = api(token)
    _rejected(lambda: adapter.send_rich_message(stranger.user_id, _rich("<p>x</p>")),
              hidden=ERRORS["chat_not_found"][1])
    rich.block_chat(bot, user)
    assert _send_rich(rich, token, user.user_id, "<p>y</p>").json() == _envelope(ERRORS["blocked"])
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": sent, "rich_message": _rich("<p>z</p>")}
    ).json() == _envelope(ERRORS["blocked"])
    _rejected(lambda: adapter.send_rich_message(user.user_id, _rich("<p>y</p>")), ChatUnreachable)
    _rejected(lambda: adapter.edit_message_text(user.user_id, sent, rich_message=_rich("<p>z</p>")),
              ChatUnreachable)
    assert len(rich.messages()) == 1 and rich.edits() == []


# ---------------------------------------------------------------------------------------------
# Edições
# ---------------------------------------------------------------------------------------------


def test_edits_change_state_and_equal_edit_is_not_modified(rich: Any, clock: FakeClock, api: Any) -> None:
    """Edições: a edição muda o estado; igual → 400 `not modified`; apagada ou
    desconhecida → `to edit not found`; mensagem do aluno → `can't be edited`; sem `reply_markup`,
    o teclado sai. Paridade: pelo adaptador real, a edição muda o estado (sem `reply_markup`, o teclado
    sai), a igual é `MessageNotModified` e a desconhecida, a do aluno e a apagada são
    `EditTargetUnavailable`."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>",
                      reply_markup=_keyboard(["a", "b"])).json()["result"]["message_id"]

    def edit(**params: Any) -> Reply:
        return rich.request(token, "editMessageText",
                            {"chat_id": user.user_id, "message_id": sent, **params})

    reply = edit(rich_message=_rich("<p>q ✅</p>"), reply_markup=_keyboard(["a"]))
    assert reply.status == 200 and reply.json()["result"]["reply_markup"] == _keyboard(["a"])
    assert edit(rich_message=_rich("<p>q ✅</p>"), reply_markup=_keyboard(["a"])).json() == \
        _envelope(DISPATCH_ERRORS["not_modified"])
    clock.advance(5)
    assert edit(rich_message=_rich("<p>q ✅</p>")).status == 200  # o teclado sai
    current = rich.current(bot, user.user_id, sent)
    assert (current.text, current.keyboard, current.edit_count) == ("<p>q ✅</p>", None, 2)
    [first, second] = rich.edits(bot, user.user_id, sent)
    assert (first.before.text, first.after.text) == ("<p>q</p>", "<p>q ✅</p>")
    assert first.before.keyboard == _keyboard_buttons(["a", "b"])
    assert second.after.keyboard is None and second.clock_at == clock.now()
    assert second.started > first.started
    assert edit(text="<b>html</b>", parse_mode="HTML").status == 200
    assert rich.current(bot, user.user_id, sent).text_format == "html"
    assert rich.push_text(bot, user, "resposta") is not None
    from_student = sent + 1  # o id seguinte do chat é o da mensagem do aluno
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": from_student, "text": "x"}).json() == _envelope(
        DISPATCH_ERRORS["cant_be_edited"])
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": 9999, "text": "x"}).json() == _envelope(
        DISPATCH_ERRORS["edit_not_found"])
    adapter = api(token)
    _rejected(lambda: adapter.edit_message_text(user.user_id, from_student, text="x"), EditTargetUnavailable)
    rich.delete_message(bot, user, sent)
    assert edit(text="depois").json() == _envelope(DISPATCH_ERRORS["edit_not_found"])
    assert rich.current(bot, user.user_id, sent).deleted is True
    assert rich.protocol_errors() == [] and rich.policy_errors() == []
    fresh = adapter.send_rich_message(user.user_id, _rich("<p>n</p>"), reply_markup=_keyboard(["a"]))
    assert adapter.edit_message_text(user.user_id, fresh, rich_message=_rich("<p>n ✅</p>")) is None
    assert rich.current(bot, user.user_id, fresh).keyboard is None
    _rejected(lambda: adapter.edit_message_text(user.user_id, fresh, rich_message=_rich("<p>n ✅</p>")),
              MessageNotModified, hidden=DISPATCH_ERRORS["not_modified"][1])
    _rejected(lambda: adapter.edit_message_text(user.user_id, 9999, text="x"), EditTargetUnavailable)
    _rejected(lambda: adapter.edit_message_text(user.user_id, sent, text="depois"), EditTargetUnavailable)
    assert rich.current(bot, user.user_id, fresh).edit_count == 1
    assert rich.protocol_errors() == [] and rich.policy_errors() == []


def _keyboard_buttons(*rows: list[str]) -> tuple[tuple[Button, ...], ...]:
    return tuple(tuple(Button(d.upper(), d) for d in row) for row in rows)


def test_edit_reply_markup_and_inline_only(rich: Any, api: Any) -> None:
    """`editMessageReplyMarkup` só com teclado inline; o de edição do texto também. Paridade:
    pelo adaptador real, a troca do teclado é aceita, a igual é `MessageNotModified` e a que não é inline é
    `RequestRejected`, com a mesma acusação."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>",
                      reply_markup=_keyboard(["a"])).json()["result"]["message_id"]

    def edit(markup: Any) -> Reply:
        return rich.request(token, "editMessageReplyMarkup",
                            {"chat_id": user.user_id, "message_id": sent, "reply_markup": markup})

    assert edit(_keyboard(["b"])).status == 200
    assert edit(_keyboard(["b"])).json() == _envelope(DISPATCH_ERRORS["not_modified"])
    assert edit({"force_reply": True}).json() == _envelope(DISPATCH_ERRORS["inline_expected"])
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": sent, "text": "x",
        "reply_markup": {"force_reply": True}}).json() == _envelope(DISPATCH_ERRORS["inline_expected"])
    assert rich.current(bot, user.user_id, sent).keyboard == _keyboard_buttons(["b"])
    assert [e.method for e in rich.edits()] == ["editMessageReplyMarkup"]
    assert rich.protocol_errors() == [
        "editMessageReplyMarkup: reply_markup de edição que não é inline",
        "editMessageText: reply_markup de edição que não é inline",
    ]
    adapter = api(token)
    assert adapter.edit_message_reply_markup(user.user_id, sent, _keyboard(["c"])) is None
    assert rich.current(bot, user.user_id, sent).keyboard == _keyboard_buttons(["c"])
    _rejected(lambda: adapter.edit_message_reply_markup(user.user_id, sent, _keyboard(["c"])),
              MessageNotModified)
    _rejected(lambda: adapter.edit_message_reply_markup(user.user_id, sent, {"force_reply": True}))
    assert rich.protocol_errors()[2:] == [
        "editMessageReplyMarkup: reply_markup de edição que não é inline"]


def test_edit_text_shape_is_checked(rich: Any, api: Any) -> None:
    """A forma do `editMessageText` é conferida. Paridade: o adaptador real posta só os parâmetros não
    nulos (o texto ou o Rich, nunca os dois), sem desvio."""
    _, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>").json()["result"]["message_id"]
    base = {"chat_id": user.user_id, "message_id": sent}
    both = rich.request(token, "editMessageText", {**base, "text": "x",
                                                   "rich_message": _rich("<p>y</p>")})
    assert both.status == 400
    assert rich.request(token, "editMessageText", base).json() == _envelope(
        DISPATCH_ERRORS["text_empty"])
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "text": "x", "inline_message_id": "1"}).status == 400
    assert rich.protocol_errors() == [
        "editMessageText: text e rich_message juntos",
        "editMessageText: parâmetro obrigatório ausente: 'text'",
        "editMessageText: inline_message_id (o sistema edita pela mensagem do chat)",
        "editMessageText: message_id ausente ou não é inteiro",
    ]
    adapter = api(token)
    adapter.edit_message_text(user.user_id, sent, text="x")
    assert rich.calls("editMessageText")[-1].params == {**base, "text": "x"}
    adapter.edit_message_text(user.user_id, sent, rich_message=_rich("<p>y</p>"))
    assert rich.calls("editMessageText")[-1].params == {**base, "rich_message": _rich("<p>y</p>")}
    assert len(rich.protocol_errors()) == 4


# ---------------------------------------------------------------------------------------------
# Mídia (multipart)
# ---------------------------------------------------------------------------------------------


def test_photo_and_document_upload_by_multipart(rich: Any, clock: FakeClock, api: Any) -> None:
    """Foto e documento: multipart só nesses dois; os bytes chegam inteiros; `uploads()` os registra. Paridade:
    o `send_photo`/`send_document` do adaptador real chega com os mesmos parâmetros do multipart cru, os
    bytes inteiros e o `reply_parameters` em texto JSON."""
    bot, token, user = _setup(rich)
    data = bytes(range(256)) * 40 + b"\r\n--x\r\n\r\n"
    reply = _upload(rich, token, "sendPhoto", user.user_id, "photo", "figura-1.png", "image/png",
                    data, caption="Figura 1 — α")
    assert reply.status == 200
    result = reply.json()["result"]
    assert result["caption"] == "Figura 1 — α" and result["photo"][0]["file_size"] == len(data)
    reply = _upload(rich, token, "sendDocument", user.user_id, "document", "tabela.pdf",
                    "application/pdf", b"%PDF-1.4",
                    reply_parameters=json.dumps({"message_id": result["message_id"],
                                                 "allow_sending_without_reply": True}))
    assert reply.status == 200
    assert reply.json()["result"]["document"]["file_name"] == "tabela.pdf"
    photo, document = rich.uploads(bot, user.user_id)
    assert (photo.field, photo.filename, photo.content_type, photo.data) == (
        "photo", "figura-1.png", "image/png", data)
    assert (document.field, document.data, document.caption) == ("document", b"%PDF-1.4", None)
    assert photo.clock_at == clock.now() and "data" not in repr(photo)
    messages = rich.messages(bot)
    assert [m.method for m in messages] == ["sendPhoto", "sendDocument"]
    assert messages[0].upload == photo and messages[0].text_format is None
    assert messages[1].reply_to_message_id == result["message_id"]
    call = rich.calls("sendPhoto")[-1]
    assert call.params["photo"] == {"filename": "figura-1.png", "content_type": "image/png",
                                    "size": len(data), "sha256": photo.sha256}
    json.dumps([c.params for c in rich.calls()])  # a varredura da fixture `bot_api` lê em JSON
    assert rich.protocol_errors() == [] and rich.policy_errors() == []
    adapter = api(token)
    adapter.send_photo(user.user_id, UploadFile("figura-1.png", "image/png", data), caption="Figura 1 — α")
    assert rich.calls("sendPhoto")[-1].params == call.params
    reply_parameters = {"message_id": result["message_id"], "allow_sending_without_reply": True}
    via_document = adapter.send_document(user.user_id, UploadFile("tabela.pdf", "application/pdf", b"%PDF-1.4"),
                                         reply_parameters=reply_parameters)
    raw_document, adapter_document = rich.calls("sendDocument")

    def norm(params: dict[str, Any]) -> dict[str, Any]:  # o texto JSON do multipart, lido
        return {k: json.loads(v) if k == "reply_parameters" and isinstance(v, str) else v
                for k, v in params.items()}

    assert norm(adapter_document.params) == norm(raw_document.params)
    assert rich.current(bot, user.user_id, via_document).reply_to_message_id == result["message_id"]
    assert [(u.field, u.filename, u.data) for u in rich.uploads(bot, user.user_id)[2:]] == [
        ("photo", "figura-1.png", data), ("document", "tabela.pdf", b"%PDF-1.4")]
    assert rich.protocol_errors() == [] and rich.policy_errors() == []


def test_media_limits_and_rules(rich: Any, api: Any) -> None:
    """Os limites da foto (≤ 10 MB, legenda ≤ 1024), o tipo da foto e o arquivo sempre em
    multipart. Paridade: pelo adaptador real, a foto GIF e a legenda longa são `RequestRejected`, com as
    mesmas acusações, e a legenda no limite é aceita."""
    bot, token, user = _setup(rich)
    photo = _upload(rich, token, "sendPhoto", user.user_id, "photo", "a.png", "image/png",
                    b"x" * 10_000_000)
    assert photo.status == 200
    big = _upload(rich, token, "sendPhoto", user.user_id, "photo", "a.png", "image/png",
                  b"x" * 10_000_001)
    assert big.json() == _envelope(DISPATCH_ERRORS["file_too_big"])
    gif = _upload(rich, token, "sendPhoto", user.user_id, "photo", "a.gif", "image/gif", b"GIF8")
    assert gif.json() == _envelope(DISPATCH_ERRORS["photo_type"])
    caption = _upload(rich, token, "sendDocument", user.user_id, "document", "a.svg",
                      "image/svg+xml", b"<svg/>", caption="c" * 1025)
    assert caption.json() == _envelope(DISPATCH_ERRORS["caption_too_long"])
    assert _upload(rich, token, "sendDocument", user.user_id, "document", "a.svg",
                   "image/svg+xml", b"<svg/>", caption="c" * 1024).status == 200
    by_json = rich.request(token, "sendPhoto", {"chat_id": user.user_id, "photo": "file-id"})
    assert by_json.json() == _envelope(DISPATCH_ERRORS["file_missing"])
    assert rich.protocol_errors() == [
        "sendPhoto: foto com mais de 10000000 bytes",
        "sendPhoto: foto em image/gif (só JPEG, PNG ou WebP)",
        "sendDocument: caption com mais de 1024 unidades UTF-16",
        "sendPhoto: photo não veio como arquivo em multipart (o sistema sempre envia os "
        "bytes)",
    ]
    assert [u.size for u in rich.uploads(bot)] == [10_000_000, 6]
    body, content = _multipart({"chat_id": str(user.user_id)}, {})
    assert rich.request(token, "sendMessage", body=body, content_type=content).status == 400
    assert rich.protocol_errors()[-3:] == [
        "sendMessage: corpo multipart/form-data (esperado JSON)",
        "sendMessage: parâmetro obrigatório ausente: 'chat_id'",
        "sendMessage: parâmetro obrigatório ausente: 'text'",
    ]
    adapter = api(token)
    _rejected(lambda: adapter.send_photo(user.user_id, UploadFile("a.gif", "image/gif", b"GIF8")),
              hidden=DISPATCH_ERRORS["photo_type"][1])
    _rejected(lambda: adapter.send_document(user.user_id, UploadFile("a.svg", "image/svg+xml", b"<svg/>"),
                                            caption="c" * 1025))
    assert isinstance(adapter.send_document(user.user_id, UploadFile("a.svg", "image/svg+xml", b"<svg/>"),
                                            caption="c" * 1024), int)
    assert rich.protocol_errors()[-2:] == ["sendPhoto: foto em image/gif (só JPEG, PNG ou WebP)",
                                           "sendDocument: caption com mais de 1024 unidades UTF-16"]


# ---------------------------------------------------------------------------------------------
# Toques e confirmações
# ---------------------------------------------------------------------------------------------


def test_tap_only_with_data_of_the_current_keyboard(rich: Any, api: Any) -> None:
    """`tap` só com um dado do teclado corrente; depois da edição (aqui, pelo adaptador real), o
    dado velho só por `tap_raw`. O toque chega ao sistema pelo `getUpdates` como o `callback_query` do
    dublê original, também pelo `get_updates_with_replies` do adaptador (sem resposta a mensagem)."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>",
                      reply_markup=_keyboard(["cq1:a", "cq1:b"])).json()["result"]["message_id"]
    tap = rich.tap(bot, user, sent, "cq1:b")
    assert (tap.chat_id, tap.message_id, tap.data, tap.raw) == (user.user_id, sent, "cq1:b", False)
    updates = rich.request(token, "getUpdates", {"offset": 0}).json()["result"]
    query = updates[-1]["callback_query"]
    assert (updates[-1]["update_id"], query["id"], query["data"], query["message"]["message_id"]
            ) == (tap.update_id, tap.callback_id, "cq1:b", sent)
    with pytest.raises(AssertionError):
        rich.tap(bot, user, sent, "cq1:z")
    with pytest.raises(AssertionError):
        rich.tap(bot, user, sent + 50, "cq1:a")
    adapter = api(token)
    polled, replies = adapter.get_updates_with_replies(offset=None)
    assert [u.update_id for u in polled][-1] == tap.update_id and replies == {}
    adapter.edit_message_reply_markup(user.user_id, sent, _keyboard(["cq1:c"]))
    with pytest.raises(AssertionError):
        rich.tap(bot, user, sent, "cq1:a")
    stale = rich.tap_raw(bot, user, sent, "cq1:a")
    assert stale.raw is True and [t.callback_id for t in rich.taps()] == [
        tap.callback_id, stale.callback_id]
    rich.delete_message(bot, user, sent)
    with pytest.raises(AssertionError):
        rich.tap(bot, user, sent, "cq1:c")


def test_answer_callback_query_checks_the_id_once_and_the_deadline(rich: Any, api: Any) -> None:
    """`answerCallbackQuery`: o id emitido (por `tap` ou `tap_raw`) é aceito uma vez; repetido, desconhecido (o
    `push_callback` puro), expirado → 400 `query is too old…`, sem erro de protocolo. Paridade: pelo
    adaptador real, a primeira confirmação é aceita (com o texto) e a repetida ou expirada é
    `CallbackQueryExpired`."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>",
                      reply_markup=_keyboard(["cq1:a"])).json()["result"]["message_id"]

    def answer(callback_id: str, **extra: Any) -> Reply:
        return rich.request(token, "answerCallbackQuery",
                            {"callback_query_id": callback_id, **extra})

    tap = rich.tap(bot, user, sent, "cq1:a")
    assert answer(tap.callback_id, text="Resposta registrada.").json() == {"ok": True,
                                                                          "result": True}
    assert answer(tap.callback_id).json() == _envelope(QUERY_TOO_OLD)
    raw = rich.tap_raw(bot, user, sent, "forjado")
    assert answer(raw.callback_id).status == 200
    assert answer(raw.callback_id).json() == _envelope(QUERY_TOO_OLD)
    rich.push_callback(bot, user, "cq1:a", message_id=sent)
    pure = rich.request(token, "getUpdates", {"offset": 0}).json()["result"][-1]
    assert answer(pure["callback_query"]["id"]).json() == _envelope(QUERY_TOO_OLD)
    expired = rich.tap(bot, user, sent, "cq1:a")
    rich.expire_callback(expired.callback_id)
    assert answer(expired.callback_id).json() == _envelope(QUERY_TOO_OLD)
    with pytest.raises(KeyError):
        rich.expire_callback("desconhecido")
    [first, second] = rich.answers(bot)
    assert (first.callback_id, first.text, first.tap) == (tap.callback_id,
                                                          "Resposta registrada.", tap)
    assert second.tap == raw and first.delay >= 0
    assert answer(tap.callback_id, text="x" * 201).status == 400  # a regra original continua
    assert rich.protocol_errors() == ["answerCallbackQuery: text com mais de 200 unidades UTF-16"]
    adapter = api(token)
    again = rich.tap(bot, user, sent, "cq1:a")
    assert adapter.answer_callback_query(again.callback_id, text="Registrado.") is None
    _rejected(lambda: adapter.answer_callback_query(again.callback_id), CallbackQueryExpired,
              hidden=QUERY_TOO_OLD[1])
    late = rich.tap(bot, user, sent, "cq1:a")
    rich.expire_callback(late.callback_id)
    _rejected(lambda: adapter.answer_callback_query(late.callback_id), CallbackQueryExpired)
    assert [(a.callback_id, a.text) for a in rich.answers(bot)][2:] == [(again.callback_id, "Registrado.")]
    assert rich.protocol_errors() == ["answerCallbackQuery: text com mais de 200 unidades UTF-16"]


def test_callback_timeout_expires_late_answers(fake: FakeBotApi, api: Any) -> None:
    """O prazo do toque vence e a confirmação tardia é recusada. Paridade: pelo adaptador real, a
    confirmação tardia é `CallbackQueryExpired`, sem resposta registrada."""
    rich = fake.install(DispatchExtension(callback_timeout=0.05))
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>",
                      reply_markup=_keyboard(["a"])).json()["result"]["message_id"]
    tap = rich.tap(bot, user, sent, "a")
    rich.wait_for(lambda: monotonic() > tap.emitted + 0.06, timeout=2)
    assert rich.request(token, "answerCallbackQuery", {"callback_query_id": tap.callback_id}
                        ).json() == _envelope(QUERY_TOO_OLD)
    assert rich.answers() == []
    late = rich.tap(bot, user, sent, "a")
    rich.wait_for(lambda: monotonic() > late.emitted + 0.06, timeout=2)
    _rejected(lambda: api(token).answer_callback_query(late.callback_id), CallbackQueryExpired)
    assert rich.answers() == []
    with pytest.raises(ValueError):
        DispatchExtension(callback_timeout=0)


# ---------------------------------------------------------------------------------------------
# Falhas programadas nos métodos da extensão e o efeito antes da falha
# ---------------------------------------------------------------------------------------------


def test_fail_after_effect_leaves_the_message_and_answers_the_failure(rich: Any, api: Any) -> None:
    """`fail("sendRichMessage", status=502, after_effect=True)`: a mensagem existe, com o teclado
    (pode ser tocada), e a resposta é a falha programada; a próxima chamada segue normal. Paridade
    : pelo adaptador real, a falha depois do efeito é `TelegramUnavailable`, e a mensagem fica
    com o teclado."""
    bot, token, user = _setup(rich)
    rich.fail("sendRichMessage", status=502, after_effect=True)
    failed = _send_rich(rich, token, user.user_id, "<p>q</p>", reply_markup=_keyboard(["cq1:a"]))
    assert (failed.status, failed.json()) == (502, _envelope((502, "Bad Gateway")))
    [copy] = rich.messages(bot)
    assert copy.keyboard == _keyboard_buttons(["cq1:a"])
    rich.tap(bot, user, copy.message_id, "cq1:a")
    again = _send_rich(rich, token, user.user_id, "<p>q</p>", reply_markup=_keyboard(["cq1:a"]))
    assert again.status == 200 and again.json()["result"]["message_id"] == copy.message_id + 1
    assert [c.programmed for c in rich.calls("sendRichMessage")] == [True, False]
    rich.fail("sendMessage", status=500, after_effect=True)  # o sendMessage assumido também
    assert rich.request(token, "sendMessage", {"chat_id": user.user_id, "text": "x"}).status == 500
    assert [m.text for m in rich.sent_messages(bot)] == ["x"]
    with pytest.raises(ValueError):
        rich.fail("*", status=500, after_effect=True)
    with pytest.raises(ValueError):
        rich.fail("getMe", status=500, after_effect=True)
    rich.fail("sendRichMessage", status=502, after_effect=True)
    count = len(rich.messages(bot))
    _rejected(lambda: api(token).send_rich_message(user.user_id, _rich("<p>q</p>"),
                                                   reply_markup=_keyboard(["cq1:a"])), TelegramUnavailable)
    assert len(rich.messages(bot)) == count + 1
    assert rich.messages(bot)[-1].keyboard == _keyboard_buttons(["cq1:a"])
    assert rich.protocol_errors() == [] and rich.policy_errors() == []


def test_fail_on_extension_methods_without_effect(rich: Any, api: Any) -> None:
    """Falhas programadas: 429 com `retry_after` e 5xx programáveis em `editMessageText` e nos demais métodos da
    extensão; sem `after_effect`, nada muda no estado. Paridade: pelo adaptador real, o 429 com
    `retry_after` é `RetryAfter` com os segundos e o 5xx é `TelegramUnavailable`, sem mudar o estado."""
    bot, token, user = _setup(rich)
    sent = _send_rich(rich, token, user.user_id, "<p>q</p>").json()["result"]["message_id"]
    rich.fail("editMessageText", retry_after=3)
    reply = rich.request(token, "editMessageText", {"chat_id": user.user_id, "message_id": sent,
                                                    "rich_message": _rich("<p>z</p>")})
    assert (reply.status, reply.headers["retry-after"], reply.json()["parameters"]) == (
        429, "3", {"retry_after": 3})
    rich.fail("editMessageText", status=500, after_effect=False)
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": sent, "rich_message": _rich("<p>z</p>")}
    ).status == 500
    assert rich.edits() == [] and rich.current(bot, user.user_id, sent).text == "<p>q</p>"
    rich.fail("editMessageText", status=502, after_effect=True)
    assert rich.request(token, "editMessageText", {
        "chat_id": user.user_id, "message_id": sent, "rich_message": _rich("<p>z</p>")}
    ).status == 502
    assert rich.current(bot, user.user_id, sent).text == "<p>z</p>"
    rich.fail_network("sendPhoto", kind="not_json")
    assert _upload(rich, token, "sendPhoto", user.user_id, "photo", "a.png", "image/png",
                   b"x").status == 502
    assert rich.uploads() == []
    adapter = api(token)
    rich.fail("editMessageText", retry_after=3)
    assert _rejected(lambda: adapter.edit_message_text(user.user_id, sent, rich_message=_rich("<p>w</p>")),
                     RetryAfter).seconds == 3
    rich.fail("editMessageReplyMarkup", status=500)
    _rejected(lambda: adapter.edit_message_reply_markup(user.user_id, sent, _keyboard(["a"])),
              TelegramUnavailable)
    assert rich.current(bot, user.user_id, sent).text == "<p>z</p>" and len(rich.edits()) == 1


# ---------------------------------------------------------------------------------------------
# push_text com resposta
# ---------------------------------------------------------------------------------------------


def test_push_text_reply_to_message_id(fake: FakeBotApi, clock: FakeClock, api: Any) -> None:
    """O texto "respondendo" a uma mensagem do bot leva `reply_to_message`; sem o argumento, a
    mensagem é a original (sem a chave). Paridade: o `get_updates_with_replies` do adaptador
    real devolve, da mesma consulta, o mapa `update_id` → mensagem respondida só do texto com resposta."""
    bot = fake.add_bot()
    token = fake.token_of(bot)
    user = make_user(7)
    fake.push_text(bot, user, "sem resposta")
    fake.push_text(bot, user, "3,14", reply_to_message_id=1)
    updates = fake.request(token, "getUpdates", {"offset": 0}).json()["result"]
    plain, replied = (u["message"] for u in updates)
    assert "reply_to_message" not in plain
    assert replied["reply_to_message"] == {
        "message_id": 1,
        "from": {"id": bot, "is_bot": True, "first_name": "Bot de teste",
                 "username": fake.state(bot).username},
        "chat": user.as_private_chat(),
        "date": int(clock.now().timestamp()),
        "text": "Mensagem do bot"}
    assert list(replied) == ["message_id", "from", "chat", "date", "text", "reply_to_message"]
    with pytest.raises(ValueError):
        fake.push_text(bot, user, "x", reply_to_message_id=0)
    with pytest.raises(ValueError):
        fake.push_text(bot, user, "x", reply_to_message_id=1,
                       extra={"reply_to_message": {"message_id": 1}})
    polled, replies = api(token).get_updates_with_replies(offset=None)
    assert [u.update_id for u in polled] == [u["update_id"] for u in updates]
    assert replies == {updates[1]["update_id"]: 1}
    assert fake.protocol_errors() == []
