"""Fuso do oferecimento.

Um fuso é válido se o valor **exato** pertence à lista `zones` do pacote `tzdata`, menos `Factory`
(links aceitos; sem aparar espaços nem mudar a caixa). A checagem de pertinência vem antes de
qualquer `ZoneInfo`, e nenhuma mensagem contém o valor recebido. `TimezoneError.reason` é o motivo
do contrato: `invalid_format`, `too_short`, `too_long` ou `invalid_value`.

`format_local` escreve um instante no fuso do oferecimento (o e-mail do convite, os avisos e os resumos),
`AAAA-MM-DD HH:MM <abreviação> (<fuso>)`.
"""

from __future__ import annotations

import functools
import importlib.resources
import zoneinfo
from datetime import datetime

import tzdata

MAX_LENGTH = 64

# Versão da base IANA do pacote `tzdata` (ex.: `2026d`).
IANA_VERSION: str = tzdata.IANA_VERSION

# O fuso "desconhecido" da base IANA (eggert/tz `factory`): está na lista, mas não é um fuso.
_EXCLUDED = frozenset({"Factory"})


class TimezoneError(ValueError):
    """Fuso fora das regras; `reason` é o motivo do contrato."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@functools.cache
def _valid_names() -> frozenset[str]:
    """As linhas do arquivo `zones` do pacote `tzdata`, menos `Factory`.

    É a lista que o próprio CPython usa (`zoneinfo._tzpath`), igual no host e na imagem; não depende
    do `TZPATH` nem dos arquivos do sistema.
    """
    text = importlib.resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(line for line in text.splitlines() if line) - _EXCLUDED


def validate_timezone(value: object) -> str:
    """O valor, sem mudança, se ele é um nome da lista (ver o docstring do módulo); senão
    `TimezoneError` com o motivo, sem o valor e sem tocar no `zoneinfo`."""
    if not isinstance(value, str):
        raise TimezoneError("invalid_format")
    if not value:
        raise TimezoneError("too_short")
    if len(value) > MAX_LENGTH:
        raise TimezoneError("too_long")
    if value not in _valid_names():
        raise TimezoneError("invalid_value")
    return value


def load_zone(name: str) -> zoneinfo.ZoneInfo:
    """A zona de um nome já validado; confere a pertinência de novo antes do `ZoneInfo`, para que
    nenhum caminho fora da lista chegue a ele."""
    return zoneinfo.ZoneInfo(validate_timezone(name))


def format_local(moment: datetime, zone: str) -> str:
    """O instante no fuso `zone` (já validado), como `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`
    (ex.: `2026-10-03 14:00 -03 (America/Sao_Paulo)`): o formato do CLI dos oferecimentos,
    com os segundos truncados, e o fuso entre parênteses. Horário sem fuso é `ValueError`."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("horário sem fuso")
    local = moment.astimezone(load_zone(zone))
    return f"{local.strftime('%Y-%m-%d %H:%M %Z')} ({zone})"
