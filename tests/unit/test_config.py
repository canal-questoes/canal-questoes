"""Configuração do servidor lida do ambiente (`config.Settings`)."""

from __future__ import annotations

import dataclasses
import traceback

import pytest

from cq_server.config import ConfigError, Settings

URL = "postgresql://cq@db.example:5432/cq"
TOKEN_32 = "Adm9QxV2" * 4
TOKEN_31 = TOKEN_32[:-1]
TOKEN_40 = "AdmSentinelaQ7vX2" + "p" * 23

# Padrões exatos de `Settings`.
DEFAULTS = {
    "database_url": URL,
    "admin_token": None,
    "lease_seconds": 60,
    "max_attempts": 5,
    "backoff_base_seconds": 2,
    "backoff_max_seconds": 300,
    "poll_interval_seconds": 1,
    "worker_id": None,
    "console_output": "-",
}

# (variável, campo, valor cru, valor lido)
READ_CASES = [
    ("CQ_ADMIN_TOKEN", "admin_token", TOKEN_40, TOKEN_40),
    ("CQ_LEASE_SECONDS", "lease_seconds", "45.5", 45.5),
    ("CQ_MAX_ATTEMPTS", "max_attempts", "7", 7),
    ("CQ_BACKOFF_BASE_SECONDS", "backoff_base_seconds", "0.5", 0.5),
    ("CQ_BACKOFF_MAX_SECONDS", "backoff_max_seconds", "120", 120.0),
    ("CQ_POLL_INTERVAL_SECONDS", "poll_interval_seconds", "0.05", 0.05),
    ("CQ_WORKER_ID", "worker_id", "worker-a1", "worker-a1"),
    ("CQ_CONSOLE_OUTPUT", "console_output", "/tmp/console.jsonl", "/tmp/console.jsonl"),
]

OPTIONAL_VARS = [var for var, *_ in READ_CASES]
FLOAT_VARS = ["CQ_LEASE_SECONDS", "CQ_BACKOFF_BASE_SECONDS", "CQ_BACKOFF_MAX_SECONDS",
              "CQ_POLL_INTERVAL_SECONDS"]


def test_only_database_url_gives_plan_defaults() -> None:
    """só `CQ_DATABASE_URL` → os padrões exatos; `Settings` é congelada."""
    settings = Settings.from_env({"CQ_DATABASE_URL": URL})
    assert isinstance(settings, Settings)
    assert dataclasses.asdict(settings) == DEFAULTS
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.max_attempts = 9  # type: ignore[misc]


def test_from_env_reads_only_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """`from_env(env)` lê o mapa recebido, e não o ambiente do processo."""
    monkeypatch.setenv("CQ_DATABASE_URL", "postgresql://processo@outro.example/outro")
    for var, _field, raw, _value in READ_CASES:
        monkeypatch.setenv(var, raw)
    assert dataclasses.asdict(Settings.from_env({"CQ_DATABASE_URL": URL})) == DEFAULTS
    with pytest.raises(ConfigError):
        Settings.from_env({})


def test_every_variable_is_read() -> None:
    """todas as variáveis juntas → cada campo com o valor lido e o tipo esperado."""
    env = {"CQ_DATABASE_URL": URL, **{var: raw for var, _f, raw, _v in READ_CASES}}
    settings = Settings.from_env(env)
    expected = {"database_url": URL, **{field: value for _var, field, _raw, value in READ_CASES}}
    assert dataclasses.asdict(settings) == expected
    for _var, field, _raw, value in READ_CASES:
        assert type(getattr(settings, field)) is type(value), field


@pytest.mark.parametrize(("var", "field", "raw", "value"), READ_CASES, ids=[c[0] for c in READ_CASES])
def test_each_variable_maps_to_its_own_field(var: str, field: str, raw: str, value: object) -> None:
    """cada variável, sozinha, muda só o seu campo (sem troca de variáveis)."""
    settings = Settings.from_env({"CQ_DATABASE_URL": URL, var: raw})
    assert dataclasses.asdict(settings) == {**DEFAULTS, field: value}
    assert type(getattr(settings, field)) is type(value)


@pytest.mark.parametrize(
    "env",
    [{}, {"CQ_DATABASE_URL": ""}, {"CQ_ADMIN_TOKEN": TOKEN_40, "CQ_MAX_ATTEMPTS": "3"}],
    ids=["ausente", "vazia", "ausente_com_outras"],
)
def test_missing_or_empty_database_url_is_config_error(env: dict[str, str]) -> None:
    """`CQ_DATABASE_URL` ausente ou vazia → `ConfigError`."""
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_admin_token_with_31_characters_is_config_error() -> None:
    """`CQ_ADMIN_TOKEN` com 31 caracteres → `ConfigError`."""
    assert len(TOKEN_31) == 31
    with pytest.raises(ConfigError):
        Settings.from_env({"CQ_DATABASE_URL": URL, "CQ_ADMIN_TOKEN": TOKEN_31})


def test_admin_token_with_32_characters_is_accepted() -> None:
    """`CQ_ADMIN_TOKEN` com 32 caracteres → aceito, com o valor exato."""
    assert len(TOKEN_32) == 32
    settings = Settings.from_env({"CQ_DATABASE_URL": URL, "CQ_ADMIN_TOKEN": TOKEN_32})
    assert dataclasses.asdict(settings) == {**DEFAULTS, "admin_token": TOKEN_32}


@pytest.mark.parametrize("env_extra", [{}, {"CQ_ADMIN_TOKEN": ""}], ids=["ausente", "vazio"])
def test_admin_token_absent_or_empty_is_none(env_extra: dict[str, str]) -> None:
    """`CQ_ADMIN_TOKEN` vazio ou ausente → `None` (a API funciona só com os tokens do
    banco)."""
    settings = Settings.from_env({"CQ_DATABASE_URL": URL, **env_extra})
    assert settings.admin_token is None
    assert dataclasses.asdict(settings) == DEFAULTS


@pytest.mark.parametrize("var", OPTIONAL_VARS)
def test_empty_optional_variable_gives_default(var: str) -> None:
    """opcional vazio → o padrão (vale como ausente)."""
    settings = Settings.from_env({"CQ_DATABASE_URL": URL, var: ""})
    assert dataclasses.asdict(settings) == DEFAULTS


def test_all_optional_variables_empty_give_defaults() -> None:
    """todos os opcionais vazios ao mesmo tempo → todos os padrões."""
    settings = Settings.from_env({"CQ_DATABASE_URL": URL, **{var: "" for var in OPTIONAL_VARS}})
    assert dataclasses.asdict(settings) == DEFAULTS


@pytest.mark.parametrize("raw", ["3.0", "abc", "0", "-1"])
def test_invalid_integer_is_config_error(raw: str) -> None:
    """`CQ_MAX_ATTEMPTS` só por `int()` (`3.0` não é inteiro) e maior que zero."""
    with pytest.raises(ConfigError):
        Settings.from_env({"CQ_DATABASE_URL": URL, "CQ_MAX_ATTEMPTS": raw})


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "0", "0.0", "-1", "abc", "1e400"])
@pytest.mark.parametrize("var", FLOAT_VARS)
def test_invalid_float_is_config_error(var: str, raw: str) -> None:
    """número de ponto flutuante infinito, `nan`, zero, negativo ou inválido → `ConfigError`."""
    with pytest.raises(ConfigError):
        Settings.from_env({"CQ_DATABASE_URL": URL, var: raw})


def test_config_error_message_does_not_contain_admin_token() -> None:
    """o erro do token curto não mostra o valor do token (mensagem, `repr`, `args` e
    traceback formatado, com as exceções encadeadas)."""
    env = {"CQ_DATABASE_URL": URL, "CQ_ADMIN_TOKEN": TOKEN_31}
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env(env)
    exc = excinfo.value
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        assert TOKEN_31 not in text
        assert TOKEN_31[:8] not in text


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://cq:SenhaSentinelaDb9@db.example:5432/cq",
        "host=db.example port=5432 user=cq password=SenhaSentinelaDb9 dbname=cq",
    ],
    ids=["url", "chave_valor"],
)
def test_repr_hides_admin_token_and_database_password(database_url: str) -> None:
    """`repr(settings)` (e `str`) não mostra o token inicial nem a senha da URL do
    banco, lida do ambiente ou construída direto."""
    from_env = Settings.from_env({"CQ_DATABASE_URL": database_url, "CQ_ADMIN_TOKEN": TOKEN_40})
    direct = Settings(database_url=database_url, admin_token=TOKEN_40)
    for settings in (from_env, direct):
        for text in (repr(settings), str(settings)):
            assert TOKEN_40 not in text
            assert "AdmSentinelaQ7vX2" not in text
            assert "SenhaSentinelaDb9" not in text
    assert from_env.admin_token == TOKEN_40
    assert from_env.database_url == database_url
