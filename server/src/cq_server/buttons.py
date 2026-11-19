"""Dado dos botões (`callback_data`) das entregas.
O dado identifica a entrega, a ação, o campo e a versão dos controles.

Gramática (ASCII, no máximo 53 bytes, dentro dos 64 da Bot API):
`cq1:<button_token>:<ação>:<campo base 36>:<controls_version base 36 mod 36⁴>[:<chave>]`, com o
token de 22 caracteres base64url, as ações `c` (escolher; reservada, sem botão que a produza
hoje), `m` (marcar), `u` (desmarcar), `s` (enviar) e `n` (não sei); `c`, `m` e `u` exigem a chave
(`[A-Za-z0-9]{1,16}`), `s` e `n` a proíbem. A gramática é uma só; só muda quais ações cada
tipo de campo aceita (`answers._tap_effect`): `single_choice` → `m` e `s`;
`multiple_choice` → `m`, `u` e `s`; `n` em todo campo.
Um dado fora dessa gramática não é do sistema (`decode_callback` → `None`): segue o tratamento comum (`not_handled`,
sem confirmação).

Núcleo puro. Os dígitos base 36 são `0-9a-z`, na forma canônica (sem zeros à esquerda): a única que
`encode_callback` produz e a que `decode_callback` lê.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CALLBACK_PREFIX = "cq1:"
CALLBACK_DATA = re.compile(
    r"cq1:([A-Za-z0-9_-]{22}):([cmusn]):([0-9a-z]{1,2}):([0-9a-z]{1,4})(?::([A-Za-z0-9]{1,16}))?")
MAX_CALLBACK_BYTES = 53
# Ações que exigem a chave e as que a proíbem.
ACTIONS_WITH_KEY = frozenset({"c", "m", "u"})
ACTIONS_WITHOUT_KEY = frozenset({"s", "n"})
ACTIONS = ACTIONS_WITH_KEY | ACTIONS_WITHOUT_KEY
# A versão vai no dado módulo 36⁴ (1 a 4 caracteres em base 36).
VERSION_MODULUS = 36 ** 4


@dataclass(frozen=True)
class CallbackAction:
    """Um dado de botão decifrado: o token da entrega, a ação, o índice do campo, a
    `controls_version` (módulo 36⁴) e a chave (só em `c`, `m` e `u`)."""

    token: str
    action: str
    field: int
    version: int
    key: str | None


_BASE36_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_KEY = re.compile(r"[A-Za-z0-9]{1,16}")


def _base36(value: int) -> str:
    """`value` (≥ 0) em base 36 minúscula, sem zeros à esquerda (`0` → `"0"`)."""
    if value == 0:
        return "0"
    digits = []
    while value:
        value, digit = divmod(value, 36)
        digits.append(_BASE36_DIGITS[digit])
    return "".join(reversed(digits))


def encode_callback(token: str, action: str, field: int, version: int, key: str | None = None) -> str:
    """O `callback_data` de um botão (≤ 53 bytes ASCII)."""
    if action not in ACTIONS:
        raise ValueError("ação de botão desconhecida")
    if (key is not None) != (action in ACTIONS_WITH_KEY):
        raise ValueError("chave obrigatória em c, m e u, e proibida em s e n")
    if key is not None and _KEY.fullmatch(key) is None:
        raise ValueError("chave fora de [A-Za-z0-9]{1,16}")
    if not 0 <= field < 36 ** 2:
        raise ValueError("índice do campo fora de 0 a 1295")
    if version < 0:
        raise ValueError("versão negativa")
    parts = ["cq1", token, action, _base36(field), _base36(version % VERSION_MODULUS)]
    if key is not None:
        parts.append(key)
    data = ":".join(parts)
    if CALLBACK_DATA.fullmatch(data) is None or len(data.encode("ascii")) > MAX_CALLBACK_BYTES:
        raise ValueError("dado de botão fora da gramática cq1:")
    return data


def _canonical_base36(text: str) -> bool:
    return text == "0" or not text.startswith("0")


def decode_callback(data: str) -> CallbackAction | None:
    """O dado decifrado, ou `None` se ele não segue a gramática `cq1:`."""
    if not isinstance(data, str) or len(data) > MAX_CALLBACK_BYTES or not data.isascii():
        return None
    match = CALLBACK_DATA.fullmatch(data)
    if match is None:
        return None
    token, action, field, version, key = match.groups()
    if (key is not None) != (action in ACTIONS_WITH_KEY):
        return None
    if not (_canonical_base36(field) and _canonical_base36(version)):
        return None
    return CallbackAction(token=token, action=action, field=int(field, 36), version=int(version, 36),
                          key=key)
