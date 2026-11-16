"""Dado dos botões das entregas (`callback_data`).


Gramática (ASCII, no máximo 53 bytes, dentro dos 1 a 64 da Bot API):
`cq1:<button_token>:<ação>:<campo base 36>:<controls_version base 36 mod 36⁴>[:<chave>]`, com o token
de 22 caracteres base64url (`^[A-Za-z0-9_-]{22}$`), as ações `c` (escolher), `m` (marcar), `u`
(desmarcar), `s` (enviar) e `n` (não sei); `c`, `m` e `u` exigem a chave (`[A-Za-z0-9]{1,16}`), `s` e
`n` a proíbem. O campo tem 1 ou 2 dígitos base 36 (0 a 1295) e a versão, de 1 a 4 (módulo 36⁴); os
dígitos base 36 são `0-9a-z`, sem zeros à esquerda [I: a forma canônica de `int → base 36`, a única
que o `encode_callback` produz]. `decode_callback` devolve `None` para todo dado fora da gramática (não
é do sistema: segue o caminho de `not_handled`).

O dado leva **só** o token opaco da entrega, a ação, o índice do campo, a versão e a chave: nenhum
identificador sequencial, nenhum `pseudo_id`.

No esqueleto, `encode_callback` e `decode_callback` lançam `NotImplementedError`; o prefixo `cq1:`, a
regex `CALLBACK_DATA`, `MAX_CALLBACK_BYTES`, `VERSION_MODULUS` e `CallbackAction` são reais e são
afirmados na mesma função de um caso que chama `encode_callback` ou `decode_callback`. Todo
teste deste arquivo falha no esqueleto por `NotImplementedError`.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import re

import pytest

from cq_server.buttons import (
    CALLBACK_DATA,
    CALLBACK_PREFIX,
    MAX_CALLBACK_BYTES,
    VERSION_MODULUS,
    CallbackAction,
    decode_callback,
    encode_callback,
)

# Tokens fixos no formato de `deliveries.button_token` (16 bytes em base64url, sem `=`): os testes
# passam o token, não o geram, então o literal é a entrada e não um valor gerado pelo sistema.
TOKEN = "Ab3_-Zz09yXw_Qp-Lm7Kd2"
TOKEN_2 = base64.urlsafe_b64encode(bytes(range(16))).rstrip(b"=").decode("ascii")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

MAX_FIELD = 36 ** 2 - 1  # "zz"
MAX_VERSION = VERSION_MODULUS - 1  # "zzzz"
MAX_KEY = "Z" * 16

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(value: int) -> str:
    """Base 36 canônica (`0-9a-z`, sem zeros à esquerda; `0` → `"0"`), só para montar o esperado."""
    if value == 0:
        return "0"
    out = ""
    while value:
        value, digit = divmod(value, 36)
        out = _DIGITS[digit] + out
    return out


def _expected(token: str, action: str, field: int, version: int, key: str | None) -> str:
    parts = ["cq1", token, action, _b36(field), _b36(version % VERSION_MODULUS)]
    if key is not None:
        parts.append(key)
    return ":".join(parts)


# --- codificação exata ------------------------------------------------------------------------------

EXACT = [
    ("escolher-0-0", (TOKEN, "c", 0, 0, "a"), f"cq1:{TOKEN}:c:0:0:a"),
    ("marcar-z-10", (TOKEN, "m", 35, 36, "b"), f"cq1:{TOKEN}:m:z:10:b"),
    ("desmarcar-10-zz", (TOKEN, "u", 36, 36 * 36 - 1, "C3"), f"cq1:{TOKEN}:u:10:zz:C3"),
    ("enviar-maximos", (TOKEN, "s", MAX_FIELD, MAX_VERSION, None), f"cq1:{TOKEN}:s:zz:zzzz"),
    ("nao-sei-1-1", (TOKEN_2, "n", 1, 1, None), f"cq1:{TOKEN_2}:n:1:1"),
    ("versao-modulo-36-4", (TOKEN, "n", 2, VERSION_MODULUS, None), f"cq1:{TOKEN}:n:2:0"),
    ("versao-modulo-36-4-mais-37", (TOKEN, "s", 3, VERSION_MODULUS + 37, None), f"cq1:{TOKEN}:s:3:11"),
    ("versao-grande", (TOKEN, "c", 4, 5 * VERSION_MODULUS + 1295, "k"), f"cq1:{TOKEN}:c:4:zz:k"),
    ("chave-16", (TOKEN, "c", 0, 0, MAX_KEY), f"cq1:{TOKEN}:c:0:0:{MAX_KEY}"),
]


@pytest.mark.parametrize(("args", "expected"), [(a, e) for _, a, e in EXACT], ids=[i for i, *_ in EXACT])
def test_encode_callback_follows_the_cq1_grammar_exactly(args: tuple, expected: str) -> None:
    """`encode_callback(token, action, field,
    version, key)` dá exatamente `cq1:<token>:<ação>:<campo base 36>:<versão base 36 mod 36⁴>[:<chave>]`
    (base 36 minúscula, sem zeros à esquerda; a versão reduzida módulo 36⁴); o dado casa por inteiro
    com a regex `CALLBACK_DATA`, é ASCII, tem de 1 a 53 bytes e começa pelo prefixo real `cq1:`;
    o prefixo, `MAX_CALLBACK_BYTES` e `VERSION_MODULUS`, reais no esqueleto, ficam afirmados aqui."""
    data = encode_callback(*args)
    assert data == expected
    assert type(data) is str
    assert CALLBACK_DATA.fullmatch(data) is not None, data
    assert data.isascii()
    assert 1 <= len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
    assert data.startswith(CALLBACK_PREFIX)
    # Constantes reais no esqueleto.
    assert CALLBACK_PREFIX == "cq1:"
    assert MAX_CALLBACK_BYTES == 53
    assert VERSION_MODULUS == 36 ** 4


def test_largest_field_version_and_key_fit_in_64_bytes() -> None:
    """Dado de 1 a 64 bytes ([F] `callback_data` tem 1-64 bytes; o formato
    tem no máximo 3+1+22+1+1+1+2+1+4+1+16 = 53): com o maior campo (`zz` = 1295), a maior versão (`zzzz`
    = 36⁴ − 1) e a maior chave (16 caracteres), em cada ação que leva a chave, o dado tem exatamente 53
    bytes UTF-8, ≤ 64; sem chave (`s`, `n`), 36 bytes."""
    for action in ("c", "m", "u"):
        data = encode_callback(TOKEN, action, MAX_FIELD, MAX_VERSION, MAX_KEY)
        assert data == f"cq1:{TOKEN}:{action}:zz:zzzz:{MAX_KEY}"
        assert len(data.encode("utf-8")) == MAX_CALLBACK_BYTES == 53
        assert len(data.encode("utf-8")) <= 64
    for action in ("s", "n"):
        data = encode_callback(TOKEN, action, MAX_FIELD, MAX_VERSION)
        assert data == f"cq1:{TOKEN}:{action}:zz:zzzz"
        assert len(data.encode("utf-8")) == 36


def test_callback_data_carries_only_token_action_field_version_and_key() -> None:
    """O dado identifica só (um identificador opaco e aleatório da entrega, **não
    sequencial e diferente do `pseudo_id`**, a ação, o índice do campo, uma versão curta e a chave; sem
    identidade nem segredo): `encode_callback` não recebe id da entrega, `pseudo_id` nem chat
    (a assinatura tem exatamente `token, action, field, version, key`); o dado, partido em `:`, tem
    exatamente os segmentos derivados dos argumentos, e nada mais; `CallbackAction` tem exatamente os
    5 campos (a assinatura e os campos, reais no esqueleto, afirmados junto de um `encode`)."""
    data = encode_callback(TOKEN_2, "m", 7, 123456, "x9")
    assert data.split(":") == ["cq1", TOKEN_2, "m", "7", _b36(123456 % VERSION_MODULUS), "x9"]
    assert data == _expected(TOKEN_2, "m", 7, 123456, "x9")
    # Sem `n`: o dado é o mesmo, dos mesmos argumentos, sempre (nenhum contador ou valor oculto).
    assert encode_callback(TOKEN_2, "m", 7, 123456, "x9") == data
    assert encode_callback(TOKEN, "m", 7, 123456, "x9") != data
    # Reais no esqueleto.
    assert list(inspect.signature(encode_callback).parameters) == ["token", "action", "field", "version",
                                                                   "key"]
    assert inspect.signature(encode_callback).parameters["key"].default is None
    assert [f.name for f in dataclasses.fields(CallbackAction)] == ["token", "action", "field", "version",
                                                                    "key"]
    assert TOKEN_RE.fullmatch(TOKEN) and TOKEN_RE.fullmatch(TOKEN_2)


# --- ida e volta ------------------------------------------------------------------------------------

ROUND_TRIP_FIELDS = [0, 1, 9, 10, 35, 36, 100, MAX_FIELD]
ROUND_TRIP_VERSIONS = [0, 1, 35, 36, 1295, 1296, MAX_VERSION, VERSION_MODULUS, VERSION_MODULUS * 3 + 5]
ROUND_TRIP_KEYS = ["a", "Z", "0", "b7", "Ab9", MAX_KEY]


@pytest.mark.parametrize("action", ["c", "m", "u", "s", "n"])
def test_decode_is_the_inverse_of_encode(action: str) -> None:
    """para toda combinação de campo (0 a 1295), versão (inclusive acima
    de 36⁴, que volta reduzida módulo 36⁴) e chave (1 a 16 caracteres, só nas ações `c`, `m` e `u`),
    `decode_callback(encode_callback(...))` devolve exatamente o `CallbackAction(token, ação, campo,
    versão mod 36⁴, chave)`, com campo e versão `int` e a chave `None` em `s` e `n`."""
    keys: list[str | None] = ROUND_TRIP_KEYS if action in ("c", "m", "u") else [None]
    for token in (TOKEN, TOKEN_2):
        for field in ROUND_TRIP_FIELDS:
            for version in ROUND_TRIP_VERSIONS:
                for key in keys:
                    data = encode_callback(token, action, field, version, key)
                    decoded = decode_callback(data)
                    assert decoded == CallbackAction(token=token, action=action, field=field,
                                                     version=version % VERSION_MODULUS, key=key), data
                    assert type(decoded.field) is int and type(decoded.version) is int


DECODE_EXACT = [
    ("escolher", f"cq1:{TOKEN}:c:0:0:a", CallbackAction(TOKEN, "c", 0, 0, "a")),
    ("marcar", f"cq1:{TOKEN}:m:z:10:b", CallbackAction(TOKEN, "m", 35, 36, "b")),
    ("desmarcar", f"cq1:{TOKEN}:u:10:zz:C3", CallbackAction(TOKEN, "u", 36, 1295, "C3")),
    ("enviar", f"cq1:{TOKEN}:s:zz:zzzz", CallbackAction(TOKEN, "s", MAX_FIELD, MAX_VERSION, None)),
    ("nao-sei", f"cq1:{TOKEN_2}:n:1:1", CallbackAction(TOKEN_2, "n", 1, 1, None)),
    ("chave-16", f"cq1:{TOKEN}:c:0:0:{MAX_KEY}", CallbackAction(TOKEN, "c", 0, 0, MAX_KEY)),
]


@pytest.mark.parametrize(("data", "expected"), [(d, e) for _, d, e in DECODE_EXACT],
                         ids=[i for i, *_ in DECODE_EXACT])
def test_decode_callback_reads_each_segment(data: str, expected: CallbackAction) -> None:
    """`decode_callback` de um dado na gramática devolve o
    `CallbackAction` exato (campo e versão lidos em base 36)."""
    assert decode_callback(data) == expected


# --- fora da gramática: `None` ----------------------------------------------------------------------

VALID = f"cq1:{TOKEN}:c:3:1a:b"

NOT_OURS = [
    # Fora do prefixo `cq1:`.
    ("vazio", ""),
    ("texto-qualquer", "ok"),
    ("dado-join-sem-prefixo", "join:123"),
    ("prefixo-cq2", VALID.replace("cq1:", "cq2:", 1)),
    ("prefixo-maiusculo", VALID.replace("cq1:", "CQ1:", 1)),
    ("sem-prefixo", VALID[len("cq1:"):]),
    ("so-o-prefixo", "cq1:"),
    ("espaco-antes", " " + VALID),
    ("quebra-depois", VALID + "\n"),
    # Truncado.
    ("token-cortado", f"cq1:{TOKEN[:15]}"),
    ("sem-acao", f"cq1:{TOKEN}"),
    ("sem-acao-com-dois-pontos", f"cq1:{TOKEN}:"),
    ("sem-campo", f"cq1:{TOKEN}:c"),
    ("sem-versao", f"cq1:{TOKEN}:c:3"),
    ("sem-versao-com-dois-pontos", f"cq1:{TOKEN}:c:3:"),
    ("escolher-sem-chave", f"cq1:{TOKEN}:c:3:1a"),
    ("chave-vazia", f"cq1:{TOKEN}:c:3:1a:"),
    ("enviar-com-dois-pontos-final", f"cq1:{TOKEN}:s:3:1a:"),
    # Ação desconhecida.
    ("acao-x", f"cq1:{TOKEN}:x:3:1a:b"),
    ("acao-alternar-t", f"cq1:{TOKEN}:t:3:1a:b"),
    ("acao-maiuscula", f"cq1:{TOKEN}:C:3:1a:b"),
    ("acao-longa", f"cq1:{TOKEN}:cc:3:1a:b"),
    ("acao-vazia", f"cq1:{TOKEN}::3:1a:b"),
    # Token fora de `^[A-Za-z0-9_-]{22}$`.
    ("token-21", f"cq1:{TOKEN[:21]}:c:3:1a:b"),
    ("token-23", f"cq1:{TOKEN}x:c:3:1a:b"),
    ("token-com-mais", f"cq1:{TOKEN[:21]}+:c:3:1a:b"),
    ("token-com-barra", f"cq1:{TOKEN[:21]}/:c:3:1a:b"),
    ("token-com-igual", f"cq1:{TOKEN[:21]}=:c:3:1a:b"),
    ("token-com-ponto", f"cq1:{TOKEN[:21]}.:c:3:1a:b"),
    ("token-nao-ascii", f"cq1:{TOKEN[:21]}é:c:3:1a:b"),
    ("token-vazio", "cq1::c:3:1a:b"),
    # Índice do campo não numérico (base 36 minúscula, 1 a 2 dígitos).
    ("campo-negativo", f"cq1:{TOKEN}:c:-1:1a:b"),
    ("campo-maiusculo", f"cq1:{TOKEN}:c:A:1a:b"),
    ("campo-3-digitos", f"cq1:{TOKEN}:c:100:1a:b"),
    ("campo-vazio", f"cq1:{TOKEN}:c::1a:b"),
    ("campo-com-sublinhado", f"cq1:{TOKEN}:c:_:1a:b"),
    ("campo-com-espaco", f"cq1:{TOKEN}:c: 3:1a:b"),
    # Versão não numérica (base 36 minúscula, 1 a 4 dígitos).
    ("versao-5-digitos", f"cq1:{TOKEN}:c:3:10000:b"),
    ("versao-maiuscula", f"cq1:{TOKEN}:c:3:1A:b"),
    ("versao-negativa", f"cq1:{TOKEN}:c:3:-1:b"),
    ("versao-vazia", f"cq1:{TOKEN}:c:3::b"),
    ("versao-com-ponto", f"cq1:{TOKEN}:c:3:1.5:b"),
    # Chave: exigida em `c`, `m`, `u`; proibida em `s`, `n`; `[A-Za-z0-9]{1,16}`.
    ("marcar-sem-chave", f"cq1:{TOKEN}:m:3:1a"),
    ("desmarcar-sem-chave", f"cq1:{TOKEN}:u:3:1a"),
    ("enviar-com-chave", f"cq1:{TOKEN}:s:3:1a:b"),
    ("nao-sei-com-chave", f"cq1:{TOKEN}:n:3:1a:b"),
    ("chave-17", f"cq1:{TOKEN}:c:3:1a:{'k' * 17}"),
    ("chave-com-hifen", f"cq1:{TOKEN}:c:3:1a:a-b"),
    ("chave-com-sublinhado", f"cq1:{TOKEN}:c:3:1a:a_b"),
    ("chave-nao-ascii", f"cq1:{TOKEN}:c:3:1a:á"),
    ("segmento-extra", f"cq1:{TOKEN}:c:3:1a:b:c"),
    ("segmento-extra-sem-chave", f"cq1:{TOKEN}:s:3:1a:b:c"),
    # Longo demais para a Bot API.
    ("acima-de-64-bytes", VALID + "x" * 64),
]


@pytest.mark.parametrize("data", [d for _, d in NOT_OURS], ids=[i for i, _ in NOT_OURS])
def test_data_outside_the_grammar_decodes_to_none(data: str) -> None:
    """`fullmatch`; fora da gramática → `None`, e o toque segue o caminho de `not_handled`:
    dado fora de `cq1:`, truncado, com ação desconhecida, token fora de `^[A-Za-z0-9_-]{22}$`, índice
    ou versão não numéricos (base 36 minúscula, 1-2 e 1-4 dígitos), chave ausente em `c`/`m`/`u`,
    presente em `s`/`n` ou fora de `[A-Za-z0-9]{1,16}`, segmento a mais e dado acima de 64 bytes →
    `None`. Controle positivo: o dado válido de base desses casos decifra."""
    assert decode_callback(VALID) == CallbackAction(TOKEN, "c", 3, 46, "b")
    assert decode_callback(data) is None
