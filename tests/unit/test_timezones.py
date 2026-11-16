"""Fuso do oferecimento.

`validate_timezone(value)` devolve o valor **sem mudança** se ele pertence à lista `zones` do pacote
`tzdata`, menos `Factory` (links aceitos; sem aparar espaços nem mudar a caixa); senão lança
`TimezoneError(reason)`, com `reason` em `invalid_format` (não é texto), `too_short` (vazio),
`too_long` (mais de 64) ou `invalid_value` (fora da lista). A checagem de pertinência vem antes de
qualquer `ZoneInfo`, e a mensagem nunca contém o valor. `load_zone(name)` carrega um nome já
validado; `IANA_VERSION` é a versão da base IANA do `tzdata`.

A lista de referência dos testes é lida aqui, de forma independente, do arquivo `zones` do pacote
`tzdata` (`importlib.resources`), sem passar por `cq_server`.
"""

from __future__ import annotations

import datetime as dt
import functools
import importlib.resources
import pathlib
import re
import traceback

import pytest
import tzdata
import zoneinfo

import cq_server.timezones as timezones_module
from cq_server.timezones import IANA_VERSION, TimezoneError, load_zone, validate_timezone

# Instante de referência para conferir que a zona carregada é a certa (sem horário de verão no
# Brasil desde 2019; a Argentina fica em -03:00).
REFERENCE_UTC = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)

# Aceitos, devolvidos iguais, e a hora local de
# `REFERENCE_UTC` em cada um.
ACCEPTED = [
    ("America/Sao_Paulo", "2026-11-20T09:00:00-03:00"),
    ("UTC", "2026-11-20T12:00:00+00:00"),
    ("Etc/UTC", "2026-11-20T12:00:00+00:00"),
    ("America/Argentina/ComodRivadavia", "2026-11-20T09:00:00-03:00"),
    ("Brazil/East", "2026-11-20T09:00:00-03:00"),
]

# recusados com `invalid_value` (id do caso, valor).
REFUSED_INVALID_VALUE = [
    # nome inexistente
    ("inexistente", "America/Campinas"),
    ("inexistente-canario", "America/Canario_Zzq"),
    ("abreviacao", "BRT"),
    ("deslocamento-gmt", "GMT+3"),
    ("deslocamento-utc", "UTC+3"),
    ("deslocamento-iso", "-03:00"),
    ("letra-z", "Z"),
    # variações de caixa
    ("minusculas", "america/sao_paulo"),
    ("maiusculas", "AMERICA/SAO_PAULO"),
    ("utc-minusculo", "utc"),
    ("utc-misto", "Utc"),
    ("etc-utc-minusculo", "etc/utc"),
    ("link-minusculo", "brazil/east"),
    # nomes do sistema e o `Factory`
    ("localtime", "localtime"),
    ("posixrules", "posixrules"),
    ("factory", "Factory"),
    # espaços
    ("espaco-antes", " America/Sao_Paulo"),
    ("espaco-depois", "America/Sao_Paulo "),
    ("espaco-no-meio", "America/Sao Paulo"),
    ("espaco-apos-barra", "America/ Sao_Paulo"),
    ("tab-antes", "\tUTC"),
    ("quebra-depois", "UTC\n"),
    ("nbsp", "\u00a0UTC"),
    # barras
    ("barra-final", "America/Sao_Paulo/"),
    ("barra-inicial", "/UTC"),
    ("barra-dupla", "America//Sao_Paulo"),
    ("contrabarra", "America\\Sao_Paulo"),
    # `..` e `.`
    ("ponto-barra", "./UTC"),
    ("sobe-diretorio", "../x"),
    ("so-pontos", ".."),
    ("sobe-no-meio", "America/../America/Sao_Paulo"),
    ("ponto-no-meio", "Etc/./UTC"),
    # caminhos absolutos
    ("absoluto-posix", "/etc/localtime"),
    ("absoluto-zoneinfo", "/usr/share/zoneinfo/America/Sao_Paulo"),
    ("absoluto-windows", "C:/Windows"),
    ("absoluto-windows-contrabarra", "C:\\Windows\\System32"),
    # diretórios do pacote `tzdata`
    ("diretorio-america", "America"),
    ("diretorio-etc", "Etc"),
    ("diretorio-argentina", "America/Argentina"),
    ("diretorio-zoneinfo", "zoneinfo"),
    ("caminho-no-pacote", "zoneinfo/UTC"),
    # arquivos do pacote `tzdata`
    ("init", "__init__.py"),
    ("init-subdiretorio", "America/__init__.py"),
    ("zone-tab", "zone.tab"),
    ("zone1970-tab", "zone1970.tab"),
    ("zonenow-tab", "zonenow.tab"),
    ("iso3166-tab", "iso3166.tab"),
    ("leapseconds", "leapseconds"),
    ("tzdata-zi", "tzdata.zi"),
    ("zones", "zones"),
    # NUL, substituto isolado e acento
    ("nul-final", "America/Sao_Paulo\x00"),
    ("so-nul", "\x00"),
    ("substituto-isolado", "\ud800"),
    ("substituto-no-fim", "America/Sao_Paulo\udc00"),
    ("acento", "America/São_Paulo"),
    # 64 caracteres (no limite do tamanho) fora da lista
    ("64-caracteres", "America/" + "Canario_Zzq" * 5 + "x"),
]

# Não é texto (`invalid_format`).
NOT_TEXT = [
    ("bytes", b"America/Sao_Paulo"),
    ("bytearray", bytearray(b"UTC")),
    ("inteiro", -3),
    ("real", 1.5),
    ("booleano", True),
    ("lista", ["America/Sao_Paulo"]),
    ("dicionario", {"timezone": "UTC"}),
]

TOO_LONG_65 = "America/" + "Canario_Zzq" * 5 + "xy"
TOO_LONG_1000 = "America/" + "z" * 992


@functools.cache
def _packaged_zones() -> frozenset[str]:
    """As linhas do arquivo `zones` do `tzdata` (lidas aqui, sem `cq_server`)."""
    text = importlib.resources.files("tzdata").joinpath("zones").read_text(encoding="utf-8")
    return frozenset(line for line in text.splitlines() if line)


def _refusal(value: object) -> TimezoneError:
    with pytest.raises(TimezoneError) as excinfo:
        validate_timezone(value)
    return excinfo.value


def _assert_value_not_in_error(exc: TimezoneError, value: str) -> None:
    """A mensagem, o `repr` e os `args` do erro não contêm o valor."""
    rendered = [str(exc), repr(exc), *map(str, exc.args)]
    for text in rendered:
        assert value not in text, text


# --- aceitos ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "local"), ACCEPTED, ids=[name for name, _ in ACCEPTED])
def test_accepted_names_are_returned_unchanged_and_load(name: str, local: str) -> None:
    """Fuso válido da base de fusos: o nome aceito
    volta igual (inclusive o link `Brazil/East` e o nome de 32 caracteres), e `load_zone` carrega a
    zona certa (hora local exata de um instante UTC)."""
    assert name in _packaged_zones()
    result = validate_timezone(name)
    assert result == name
    assert type(result) is str
    zone = load_zone(name)
    assert isinstance(zone, zoneinfo.ZoneInfo)
    assert zone.key == name
    assert REFERENCE_UTC.astimezone(zone).isoformat() == local


def test_every_packaged_zone_except_factory_is_accepted_and_loads() -> None:
    """A regra é a lista `zones` do `tzdata` menos `Factory`: cada um
    dos nomes da lista (598 no `tzdata` 2026.4), menos `Factory`, volta igual e carrega com
    `load_zone`; `Factory` é recusado com `invalid_value`."""
    zones = _packaged_zones()
    assert "Factory" in zones
    names = sorted(zones - {"Factory"})
    assert len(names) == len(zones) - 1
    for name in names:
        assert validate_timezone(name) == name, name
        assert load_zone(name).key == name, name
    assert _refusal("Factory").reason == "invalid_value"


def test_iana_version_has_the_iana_form() -> None:
    """`IANA_VERSION` casa `^\\d{4}[a-z]$` e é a
    do pacote `tzdata`; na mesma função, `load_zone("Etc/UTC")` dá deslocamento zero."""
    assert re.fullmatch(r"\d{4}[a-z]", IANA_VERSION)
    assert IANA_VERSION == tzdata.IANA_VERSION
    assert load_zone("Etc/UTC").utcoffset(REFERENCE_UTC.replace(tzinfo=None)) == dt.timedelta(0)


# --- recusados -------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [v for _, v in REFUSED_INVALID_VALUE],
                         ids=[i for i, _ in REFUSED_INVALID_VALUE])
def test_names_outside_the_list_are_invalid_value(value: str) -> None:
    """Recusados com `invalid_value`: nome inexistente, variações de
    caixa, `localtime`, `posixrules`, `Factory`, espaços, barras, `..`, caminhos absolutos,
    diretórios e arquivos do pacote, NUL, substituto isolado, acento, `Z` e um nome de 64
    caracteres; sem o valor na mensagem."""
    assert value not in _packaged_zones() - {"Factory"}
    assert 1 <= len(value) <= 64
    exc = _refusal(value)
    assert exc.reason == "invalid_value"
    if len(value) >= 3:
        _assert_value_not_in_error(exc, value)


def test_empty_is_too_short() -> None:
    """Fuso obrigatório: `''` → `too_short`."""
    assert _refusal("").reason == "too_short"


@pytest.mark.parametrize("value", [TOO_LONG_65, TOO_LONG_1000], ids=["65", "1000"])
def test_more_than_64_characters_is_too_long(value: str) -> None:
    """`too_long`: mais de 64; 65 caracteres →
    `too_long` (64 é `invalid_value`, caso `64-caracteres`); sem o valor na mensagem."""
    assert len(value) > 64
    exc = _refusal(value)
    assert exc.reason == "too_long"
    _assert_value_not_in_error(exc, value)


@pytest.mark.parametrize("value", [v for _, v in NOT_TEXT], ids=[i for i, _ in NOT_TEXT])
def test_non_text_is_invalid_format(value: object) -> None:
    """`invalid_format`: não é texto."""
    assert _refusal(value).reason == "invalid_format"


def test_error_never_carries_the_value_nor_the_zoneinfo_message() -> None:
    """A mensagem nunca contém o valor (a exceção sai sem a
    mensagem do `zoneinfo`, que ecoa a chave): nem a mensagem, nem o `repr`, nem os `args`, nem o
    traceback formatado (com as exceções encadeadas) contêm o valor recusado."""
    for value, reason in (("America/Canario_Zzq", "invalid_value"), (TOO_LONG_65, "too_long")):
        exc = _refusal(value)
        assert exc.reason == reason
        rendered = [str(exc), repr(exc), *map(str, exc.args),
                    "".join(traceback.format_exception(exc))]
        for text in rendered:
            assert value not in text
            assert "Canario_Zzq" not in text
        assert exc.__cause__ is None
        assert exc.__context__ is None or exc.__suppress_context__


# --- a recusa não chama `ZoneInfo` -----------------------------------------------------------------

class _ZoneInfoCalled(Exception):
    """O dublê de `ZoneInfo` foi chamado."""


def _zoneinfo_spy(calls: list[tuple]) -> type:
    """Dublê de `zoneinfo.ZoneInfo` que registra toda tentativa de carregar uma zona e falha."""

    class _Spy:
        def __new__(cls, *args: object, **kwargs: object) -> _Spy:
            calls.append(("ZoneInfo", args, kwargs))
            raise _ZoneInfoCalled("ZoneInfo")

        @classmethod
        def no_cache(cls, *args: object, **kwargs: object) -> _Spy:
            calls.append(("no_cache", args, kwargs))
            raise _ZoneInfoCalled("no_cache")

        @classmethod
        def from_file(cls, *args: object, **kwargs: object) -> _Spy:
            calls.append(("from_file", args, kwargs))
            raise _ZoneInfoCalled("from_file")

    return _Spy


def test_refusal_never_calls_zoneinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nunca chama `ZoneInfo` antes da checagem de pertinência
    (`ZoneInfo()` não valida: aceita `utc` e `America\\Sao_Paulo`, lê diretórios e arquivos do
    pacote e ecoa a chave no erro): com um dublê de `ZoneInfo` que falha, cada recusa sai com o
    motivo certo e o dublê não é chamado nenhuma vez."""
    calls: list[tuple] = []
    spy = _zoneinfo_spy(calls)
    monkeypatch.setattr(zoneinfo, "ZoneInfo", spy)
    monkeypatch.setattr(timezones_module, "ZoneInfo", spy, raising=False)
    cases: list[tuple[object, str]] = [(v, "invalid_value") for _, v in REFUSED_INVALID_VALUE]
    cases += [("", "too_short"), (TOO_LONG_65, "too_long"), (TOO_LONG_1000, "too_long")]
    cases += [(v, "invalid_format") for _, v in NOT_TEXT]
    for value, reason in cases:
        assert _refusal(value).reason == reason, repr(value)
    assert calls == []


# --- independência do `TZPATH` ---------------------------------------------------------------------

def test_membership_does_not_depend_on_tzpath(tmp_path: pathlib.Path) -> None:
    """Independência do `TZPATH` (a imagem tinha `localtime` e `posixrules` no diretório do sistema;
    a regra é a lista do `tzdata`, não o `TZPATH`): com o `TZPATH` apontado para um diretório com um
    TZif `localtime`, `posixrules` e `America/Campinas` (controle positivo: o `ZoneInfo` os
    carrega), os três continuam recusados com `invalid_value`, e `America/Sao_Paulo` continua
    aceito."""
    utc_tzif = (importlib.resources.files("tzdata").joinpath("zoneinfo").joinpath("UTC")
                .read_bytes())
    (tmp_path / "America").mkdir()
    for relative in ("localtime", "posixrules", "America/Campinas"):
        (tmp_path / relative).write_bytes(utc_tzif)
    saved = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(to=[str(tmp_path)])
    try:
        for name in ("localtime", "posixrules", "America/Campinas"):
            assert zoneinfo.ZoneInfo.no_cache(name).key == name
        for name in ("localtime", "posixrules", "America/Campinas"):
            assert _refusal(name).reason == "invalid_value"
        assert validate_timezone("America/Sao_Paulo") == "America/Sao_Paulo"
    finally:
        zoneinfo.reset_tzpath(to=saved)
    assert zoneinfo.TZPATH == saved
