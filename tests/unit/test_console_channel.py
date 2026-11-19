"""Canal de console.

Todo teste constrói o `ConsoleChannel` (no esqueleto, o construtor lança `NotImplementedError`).
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import uuid

import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.channels import FlushSpyStream

REF_PATTERN = re.compile(r"^console-[0-9a-f]{32}$")


def _message(message_id: int, text: str) -> OutgoingMessage:
    return OutgoingMessage(message_id=message_id, recipient=uuid.uuid4(), text=text)


def _expected_line(ref: str, message: OutgoingMessage) -> dict:
    return {"channel_ref": ref, "message_id": message.message_id, "recipient": str(message.recipient),
            "text": message.text}


def test_capabilities_fields_and_console_values() -> None:
    """`ChannelCapabilities` tem exatamente os quatro campos, é congelada, e o console
    declara `(can_edit=False, max_buttons=0, supports_math=False, supports_rich_text=False)`, com os
    tipos exatos."""
    channel = ConsoleChannel(io.StringIO())
    assert [f.name for f in dataclasses.fields(ChannelCapabilities)] == [
        "can_edit", "max_buttons", "supports_math", "supports_rich_text"]
    caps = channel.capabilities
    assert type(caps) is ChannelCapabilities
    assert caps == ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                       supports_rich_text=False)
    assert type(caps.can_edit) is bool and type(caps.supports_math) is bool
    assert type(caps.supports_rich_text) is bool
    assert type(caps.max_buttons) is int
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.max_buttons = 3  # type: ignore[misc]


def test_each_send_writes_one_json_line_and_flushes() -> None:
    """cada `send` → uma linha terminada num único `\\n`, sem quebras internas,
    com `json.loads(linha) == {channel_ref, message_id, recipient: str(pseudo_id), text}`; `flush()`
    depois de cada linha; devolve `Sent` com `channel_ref` no padrão."""
    stream = FlushSpyStream()
    channel = ConsoleChannel(stream)
    messages = [_message(1, "Olá, tudo bem?"), _message(2, "segunda"), _message(2**40, "x")]
    position = 0
    flushes = 0
    for message in messages:
        result = channel.send(message)
        assert type(result) is Sent
        assert REF_PATTERN.fullmatch(result.channel_ref), result.channel_ref
        written = stream.getvalue()[position:]
        assert written.endswith("\n") and not written.endswith("\n\n"), repr(written)
        assert "\n" not in written[:-1] and "\r" not in written, repr(written)
        parsed = json.loads(written)
        assert parsed == _expected_line(result.channel_ref, message)
        assert type(parsed["message_id"]) is int
        assert len(stream.flush_positions) > flushes, "sem flush() depois da linha"
        assert stream.flush_positions[-1] == len(stream.getvalue()), "flush() antes do fim da linha"
        flushes = len(stream.flush_positions)
        position = len(stream.getvalue())
    assert stream.getvalue().count("\n") == len(messages)


def test_hundred_sends_give_hundred_distinct_refs() -> None:
    """100 envios → 100 referências distintas, todas no padrão, e 100 linhas, cada uma
    com a referência devolvida pelo seu envio, na ordem."""
    stream = io.StringIO()
    channel = ConsoleChannel(stream)
    messages = [_message(i, f"mensagem {i}") for i in range(1, 101)]
    refs = [channel.send(m).channel_ref for m in messages]
    assert len(set(refs)) == 100
    assert all(REF_PATTERN.fullmatch(ref) for ref in refs)
    lines = stream.getvalue().split("\n")
    assert lines[-1] == "" and len(lines) == 101
    assert [json.loads(line) for line in lines[:-1]] == [
        _expected_line(ref, m) for ref, m in zip(refs, messages, strict=True)]


@pytest.mark.parametrize(
    "text",
    [
        "linha 1\nlinha 2\r\nlinha 3\n",
        "ação, coração, pão — “aspas” ✓ 𝛼² 数学 🙂",
        "tab\taqui e \"aspas\" e \\barra\\ e {chaves} [colchetes]",
        "x" * 4096,
    ],
    ids=["quebras_de_linha", "unicode", "escapes_json", "4096_caracteres"],
)
def test_text_with_line_breaks_and_unicode_is_preserved(text: str) -> None:
    """quebras de linha e Unicode do texto são preservados (conferidos por
    `json.loads`), e a saída continua uma linha só."""
    stream = io.StringIO()
    channel = ConsoleChannel(stream)
    message = _message(42, text)
    result = channel.send(message)
    output = stream.getvalue()
    assert output.count("\n") == 1 and output.endswith("\n")
    assert json.loads(output) == _expected_line(result.channel_ref, message)
    assert json.loads(output)["text"] == text
