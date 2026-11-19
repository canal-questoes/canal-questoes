"""Configuração dos bots: leitura do ambiente, validação, padrões e a chave-mestra fora de `repr`
e `str`.

`BotSettings.from_env(env)` lê as variáveis dos bots; a mesma validação roda na construção direta
(`BotSettings(...)`, `dataclasses.replace`). Toda recusa é `ConfigError` com a mensagem **exata** do
projeto, sem o valor recebido. Vazio vale como ausente (como na API base). `repr` e `str` nunca mostram a
chave-mestra. O `Settings` da API base não muda (`tests/unit/test_config.py` continua valendo).

Precedência [I]: um valor que viola a regra do próprio campo (número inválido, não finito ou não
positivo; consulta fora de 1..50; URL; chave) dá a mensagem desse campo, mesmo que o invariante entre
campos também falhe, porque o invariante só se define sobre números válidos. Onde dá para evitar a
dúvida, os outros campos são ajustados para o invariante valer (ex.: consulta 51 com posse 200).
"""

from __future__ import annotations

import base64
import dataclasses
import string
import traceback
from collections.abc import Callable

import pytest

from cq_server.config import BotSettings, ConfigError, Settings
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY

# --- mensagens exatas -----------------------------------------------------------------------------

MSG_URL = ("CQ_TELEGRAM_API_URL deve usar https:// com host (http:// só em 127.0.0.1, localhost ou "
           "[::1]), sem credenciais, consulta, fragmento nem barra final.")
MSG_KEY = 'CQ_MASTER_KEY deve ter 32 bytes aleatórios em base64url, sem "=" (43 caracteres).'
MSG_POLL = "CQ_POLL_TIMEOUT_SECONDS deve ser um número inteiro de 1 a 50."
MSG_LEASE = ("CQ_BOT_LEASE_SECONDS menos CQ_BOT_RENEW_SECONDS deve ser pelo menos "
             "CQ_POLL_TIMEOUT_SECONDS mais CQ_TELEGRAM_TIMEOUT_SECONDS.")
MSG_RECONCILE = "CQ_BOT_RECONCILE_SECONDS não pode ser maior que CQ_BOT_RENEW_SECONDS."


def msg_number(var: str) -> str:
    """Número inválido, não finito ou não positivo (a mesma forma da API base)."""
    return f"{var} deve ser um número finito maior que zero."


# --- padrões e leitura ----------------------------------------------------------------------------

DEFAULTS = {
    "telegram_api_url": "https://api.telegram.org",
    "lease_seconds": 60,
    "renew_seconds": 20,
    "reconcile_seconds": 10,
    "poll_timeout_seconds": 25,
    "telegram_timeout_seconds": 10,
    "master_key": None,
}

# (variável, campo, valor cru, valor lido). Cada valor, sozinho com os padrões, respeita o
# invariante (60 − 20 ≥ 25 + 10 e 10 ≤ 20); todos juntos também (75,5 − 12,5 ≥ 15 + 7,5; 0,2 ≤ 12,5).
READ_CASES = [
    ("CQ_TELEGRAM_API_URL", "telegram_api_url", "http://127.0.0.1:8081", "http://127.0.0.1:8081"),
    ("CQ_BOT_LEASE_SECONDS", "lease_seconds", "75.5", 75.5),
    ("CQ_BOT_RENEW_SECONDS", "renew_seconds", "12.5", 12.5),
    ("CQ_BOT_RECONCILE_SECONDS", "reconcile_seconds", "0.2", 0.2),
    ("CQ_POLL_TIMEOUT_SECONDS", "poll_timeout_seconds", "15", 15),
    ("CQ_TELEGRAM_TIMEOUT_SECONDS", "telegram_timeout_seconds", "7.5", 7.5),
    ("CQ_MASTER_KEY", "master_key", MASTER_KEY, MASTER_KEY),
]
BOT_VARS = [var for var, *_ in READ_CASES]
FLOAT_VARS = ["CQ_BOT_LEASE_SECONDS", "CQ_BOT_RENEW_SECONDS", "CQ_BOT_RECONCILE_SECONDS",
              "CQ_TELEGRAM_TIMEOUT_SECONDS"]
ALL_BOT_ENV = {var: raw for var, _field, raw, _value in READ_CASES}
ALL_BOT_VALUES = {field: value for _var, field, _raw, value in READ_CASES}

# O `Settings` da API base com só `CQ_DATABASE_URL` (tests/unit/test_config.py, `DEFAULTS`).
CORE_DATABASE_URL = "postgresql://cq@db.example:5432/cq"
CORE_SETTINGS_DEFAULTS = {
    "database_url": CORE_DATABASE_URL,
    "admin_token": None,
    "lease_seconds": 60,
    "max_attempts": 5,
    "backoff_base_seconds": 2,
    "backoff_max_seconds": 300,
    "poll_interval_seconds": 1,
    "worker_id": None,
    "console_output": "-",
}

# --- chaves ----------------------------------------------------------------------------------------

_B64URL = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
_RAW = base64.urlsafe_b64decode(MASTER_KEY + "=")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _non_canonical(key: str) -> str:
    """A mesma chave com os 2 bits finais (descartados na decodificação) diferentes de zero: decodifica
    para os mesmos 32 bytes, mas não é a forma canônica."""
    index = _B64URL.index(key[-1])
    assert index % 4 == 0
    return key[:-1] + _B64URL[index + 1]


# 32 bytes cuja forma canônica em base64url tem `-` e `_`.
KEY_WITH_DASH_AND_UNDERSCORE = _b64url(bytes([0xFB, 0xFF, 0xBF] * 10 + [0xFB, 0xF0]))

ACCEPTED_KEYS = [
    ("chave-da-sessao", MASTER_KEY),
    ("outra-chave", OTHER_MASTER_KEY),
    ("com-hifen-e-sublinhado", KEY_WITH_DASH_AND_UNDERSCORE),
]

# 42, 44, `=`, espaço, `+`, `/`, hex de 64, 22 caracteres, não
# canônica (e também `\n`, não ASCII e as chaves de 24 e 48 bytes).
MALFORMED_KEYS = [
    ("22-caracteres-16-bytes", _b64url(_RAW[:16])),
    ("32-caracteres-24-bytes", _b64url(_RAW[:24])),
    ("64-caracteres-48-bytes", _b64url(_RAW + _RAW[:16])),
    ("42-caracteres", MASTER_KEY[:42]),
    ("44-caracteres", MASTER_KEY + "A"),
    ("igual-no-fim-44", MASTER_KEY + "="),
    ("igual-no-lugar-do-ultimo", MASTER_KEY[:42] + "="),
    ("espaco-antes", " " + MASTER_KEY),
    ("espaco-depois", MASTER_KEY + " "),
    ("espaco-no-meio", MASTER_KEY[:21] + " " + MASTER_KEY[22:]),
    ("quebra-de-linha", MASTER_KEY + "\n"),
    ("mais", MASTER_KEY[:10] + "+" + MASTER_KEY[11:]),
    ("barra", MASTER_KEY[:10] + "/" + MASTER_KEY[11:]),
    ("hex-64", _RAW.hex()),
    ("nao-canonica", _non_canonical(MASTER_KEY)),
    ("nao-ascii", MASTER_KEY[:42] + "é"),
]

# --- URLs ------------------------------------------------------------------------------------------

ACCEPTED_URLS = [
    "https://api.telegram.org",
    "https://bot-api.example.test",
    "https://bot-api.example.test:8443",
    "http://127.0.0.1:8081",
    "http://127.0.0.1",
    "http://localhost:8081",
    "http://[::1]:8081",
]

REFUSED_URLS = [
    ("http-fora-de-loopback", "http://api.telegram.org"),
    ("http-ip-privado", "http://10.0.0.5:8081"),
    ("http-host-com-prefixo-de-loopback", "http://127.0.0.1.example.test:8081"),
    ("http-subdominio-de-localhost", "http://localhost.example.test"),
    ("credencial", "https://usuario:SenhaSentinelaUrl@api.telegram.org"),
    ("usuario-sem-senha", "https://usuario@api.telegram.org"),
    ("credencial-em-loopback", "http://usuario:SenhaSentinelaUrl@127.0.0.1:8081"),
    ("consulta", "https://api.telegram.org?chave=SentinelaConsulta"),
    ("consulta-em-loopback", "http://127.0.0.1:8081?chave=SentinelaConsulta"),
    ("fragmento", "https://api.telegram.org#SentinelaFragmento"),
    ("barra-final", "https://api.telegram.org/"),
    ("barra-final-em-loopback", "http://127.0.0.1:8081/"),
    ("sem-esquema", "api.telegram.org"),
    ("esquema-ftp", "ftp://api.telegram.org"),
    ("sem-host", "https://"),
    ("sem-host-com-caminho", "https:///bot"),
]
URL_SENTINELS = ("SenhaSentinelaUrl", "SentinelaConsulta", "SentinelaFragmento")

# --- números ---------------------------------------------------------------------------------------

INVALID_NUMBERS = ["abc", "SentinelaNum9x", "0", "0.0", "-1", "-0.5", "inf", "-inf", "Infinity",
                   "nan", "1e400"]
# Consulta fora de 1..50 ou não inteira; a posse de 200 mantém o invariante (200 − 20 ≥ 51 + 10).
INVALID_POLL = ["0", "51", "3.5", "-1", "abc", "SentinelaPoll7", "1e1", "25.0"]
_BIG_LEASE = {"CQ_BOT_LEASE_SECONDS": "200"}

# Tempos curtos do worker morto: 4 − 1 ≥ 1 + 0,5 e 0,2 ≤ 1.
WORKER_ENV = {
    "CQ_BOT_LEASE_SECONDS": "4",
    "CQ_BOT_RENEW_SECONDS": "1",
    "CQ_BOT_RECONCILE_SECONDS": "0.2",
    "CQ_POLL_TIMEOUT_SECONDS": "1",
    "CQ_TELEGRAM_TIMEOUT_SECONDS": "0.5",
}


def _assert_config_error(call: Callable[[], object], message: str, *,
                         hidden: tuple[str, ...] = ()) -> None:
    """`call()` lança `ConfigError` com `message` exata; nem a mensagem, nem o `repr`, nem os `args`,
    nem o traceback formatado (com as exceções encadeadas) contêm os textos de `hidden`."""
    with pytest.raises(ConfigError) as excinfo:
        call()
    exc = excinfo.value
    assert str(exc) == message
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        for value in hidden:
            assert value not in text


# --- padrões ---------------------------------------------------------------------------------------

def test_empty_env_gives_plan_defaults() -> None:
    """Padrões: posse 60 s, renovação 20 s, consulta 25 s; reconciliação 10 s;
    `from_env({})` dá os padrões exatos, igual a `BotSettings()`, sem chave-mestra;
    a dataclass é congelada."""
    settings = BotSettings.from_env({})
    assert isinstance(settings, BotSettings)
    assert dataclasses.asdict(settings) == DEFAULTS
    assert settings == BotSettings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.lease_seconds = 90  # type: ignore[misc]


def test_from_env_reads_only_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Como na API base: `from_env(env)` lê o mapa recebido, e não o ambiente do processo."""
    for var, _field, raw, _value in READ_CASES:
        monkeypatch.setenv(var, raw)
    assert dataclasses.asdict(BotSettings.from_env({})) == DEFAULTS


# --- leitura ---------------------------------------------------------------------------------------

def test_every_variable_is_read() -> None:
    """Chave-mestra lida do ambiente: todas as variáveis juntas → cada campo com
    o valor lido e o tipo previsto (`poll_timeout_seconds` inteiro; tempos reais; textos)."""
    settings = BotSettings.from_env(ALL_BOT_ENV)
    assert dataclasses.asdict(settings) == ALL_BOT_VALUES
    for _var, field, _raw, value in READ_CASES:
        assert type(getattr(settings, field)) is type(value), field


@pytest.mark.parametrize(("var", "field", "raw", "value"), READ_CASES, ids=BOT_VARS)
def test_each_variable_maps_to_its_own_field(var: str, field: str, raw: str, value: object) -> None:
    """cada variável, sozinha, muda só o seu campo (sem troca de
    variáveis)."""
    settings = BotSettings.from_env({var: raw})
    assert dataclasses.asdict(settings) == {**DEFAULTS, field: value}
    assert type(getattr(settings, field)) is type(value)


@pytest.mark.parametrize("var", BOT_VARS)
def test_empty_variable_counts_as_absent(var: str) -> None:
    """Vazio vale como ausente (a mesma regra da API base): cada variável vazia → o padrão
    (`CQ_MASTER_KEY` vazia → `None`); todas vazias juntas → todos os padrões."""
    assert dataclasses.asdict(BotSettings.from_env({var: ""})) == DEFAULTS
    assert dataclasses.asdict(BotSettings.from_env({v: "" for v in BOT_VARS})) == DEFAULTS


def test_core_variables_do_not_feed_bot_settings() -> None:
    """`BotSettings` à parte do `Settings`: as variáveis da API base com nomes parecidos
    (`CQ_LEASE_SECONDS`, `CQ_POLL_INTERVAL_SECONDS` …) não mudam nenhum campo dos bots."""
    env = {
        "CQ_DATABASE_URL": CORE_DATABASE_URL,
        "CQ_LEASE_SECONDS": "45.5",
        "CQ_POLL_INTERVAL_SECONDS": "0.05",
        "CQ_MAX_ATTEMPTS": "7",
        "CQ_ADMIN_TOKEN": "AdmSentinelaQ7vX2" + "p" * 23,
        "CQ_WORKER_ID": "worker-a1",
    }
    assert dataclasses.asdict(BotSettings.from_env(env)) == DEFAULTS


def test_core_settings_unchanged_with_bot_variables_in_the_environment() -> None:
    """O `Settings` da API base não muda: com `CQ_MASTER_KEY` e as demais
    variáveis dos bots no ambiente, `dataclasses.asdict(Settings.from_env(...))` é exatamente o dos padrões
    e o `repr` dele não mostra a chave; na mesma função, `BotSettings.from_env` do mesmo ambiente
    lê os bots."""
    env = {"CQ_DATABASE_URL": CORE_DATABASE_URL, **ALL_BOT_ENV}
    settings = Settings.from_env(env)
    assert dataclasses.asdict(settings) == CORE_SETTINGS_DEFAULTS
    for text in (repr(settings), str(settings)):
        assert MASTER_KEY not in text
    assert dataclasses.asdict(BotSettings.from_env(env)) == ALL_BOT_VALUES


# --- números ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", INVALID_NUMBERS)
@pytest.mark.parametrize("var", FLOAT_VARS)
def test_invalid_number_is_config_error(var: str, raw: str) -> None:
    """Número inválido, não finito ou não positivo: `ConfigError` com a mensagem
    exata `<VAR> deve ser um número finito maior que zero.`, sem o valor (inclusive no traceback)."""
    env = {var: raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: BotSettings.from_env(env), msg_number(var), hidden=hidden)


@pytest.mark.parametrize("raw", INVALID_POLL)
def test_poll_timeout_must_be_integer_from_1_to_50(raw: str) -> None:
    """Consulta de no máximo 25 s por padrão (`CQ_POLL_TIMEOUT_SECONDS`
    inteiro de 1 a 50): 0, 51, `3.5` (e outros não inteiros) → mensagem exata, sem o valor."""
    env = {**_BIG_LEASE, "CQ_POLL_TIMEOUT_SECONDS": raw}
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_POLL, hidden=hidden)


def test_poll_timeout_boundaries_are_accepted() -> None:
    """1 e 50 são aceitos (50 com a posse de 100: 100 − 20 ≥ 50 + 10), como inteiros."""
    low = BotSettings.from_env({"CQ_POLL_TIMEOUT_SECONDS": "1"})
    assert dataclasses.asdict(low) == {**DEFAULTS, "poll_timeout_seconds": 1}
    high = BotSettings.from_env({"CQ_BOT_LEASE_SECONDS": "100", "CQ_POLL_TIMEOUT_SECONDS": "50"})
    assert dataclasses.asdict(high) == {**DEFAULTS, "lease_seconds": 100.0,
                                        "poll_timeout_seconds": 50}
    assert type(high.poll_timeout_seconds) is int


# --- URL -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_accepted_telegram_api_url(url: str) -> None:
    """URL aceita (`https` com host; `http` só em `127.0.0.1`, `localhost` ou `[::1]`): a URL
    é lida sem mudança, na leitura e na construção direta."""
    settings = BotSettings.from_env({"CQ_TELEGRAM_API_URL": url})
    assert dataclasses.asdict(settings) == {**DEFAULTS, "telegram_api_url": url}
    assert BotSettings(telegram_api_url=url).telegram_api_url == url


@pytest.mark.parametrize("url", [u for _, u in REFUSED_URLS], ids=[i for i, _ in REFUSED_URLS])
def test_refused_telegram_api_url(url: str) -> None:
    """`http` fora de loopback, credencial, consulta, fragmento, barra final, sem
    esquema `https`/`http` ou sem host → `ConfigError` com a mensagem exata, sem o valor (a senha
    da credencial não aparece nem no traceback)."""
    env = {"CQ_TELEGRAM_API_URL": url}
    hidden = tuple(s for s in URL_SENTINELS if s in url)
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_URL, hidden=hidden)


# --- chave-mestra ----------------------------------------------------------------------------------

@pytest.mark.parametrize("key", [k for _, k in ACCEPTED_KEYS], ids=[i for i, _ in ACCEPTED_KEYS])
def test_accepted_master_key(key: str) -> None:
    """Chave-mestra aceita (43 caracteres base64url sem `=`, forma canônica de 32 bytes): a
    chave é lida sem mudança."""
    assert len(key) == 43
    assert _b64url(base64.urlsafe_b64decode(key + "=")) == key
    settings = BotSettings.from_env({"CQ_MASTER_KEY": key})
    assert dataclasses.asdict(settings) == {**DEFAULTS, "master_key": key}


@pytest.mark.parametrize("key", [k for _, k in MALFORMED_KEYS], ids=[i for i, _ in MALFORMED_KEYS])
def test_malformed_master_key_is_config_error(key: str) -> None:
    """Chave-mestra malformada (casos: 22 caracteres, longa, com `=`, com espaço ou `\n`, `+/`,
    hex de 64, não canônica e não ASCII): `ConfigError` com a mensagem exata, sem o valor nem um
    pedaço da chave da sessão (inclusive no traceback)."""
    env = {"CQ_MASTER_KEY": key}
    hidden = (key, key.strip()[:12], MASTER_KEY[:12])
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_KEY, hidden=hidden)


def test_non_canonical_key_decodes_to_the_same_bytes_and_is_refused() -> None:
    """Chave não canônica (o base64 da biblioteca padrão não é estrito: `…Hh8` e `…Hh9` dão os mesmos
    32 bytes; por isso a recodificação): a chave não canônica decodifica para os mesmos bytes da
    chave da sessão e mesmo assim é recusada."""
    non_canonical = _non_canonical(MASTER_KEY)
    assert non_canonical != MASTER_KEY
    assert base64.urlsafe_b64decode(non_canonical + "=") == _RAW
    env = {"CQ_MASTER_KEY": non_canonical}
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_KEY,
                         hidden=(non_canonical, MASTER_KEY[:12]))


# --- invariante da posse e reconciliação -----------------------------------------------------------

LEASE_VIOLATIONS = [
    ("posse-curta", {"CQ_BOT_LEASE_SECONDS": "54.9"}),
    ("renovacao-longa", {"CQ_BOT_RENEW_SECONDS": "25.1"}),
    ("consulta-longa", {"CQ_POLL_TIMEOUT_SECONDS": "31"}),
    ("timeout-longo", {"CQ_TELEGRAM_TIMEOUT_SECONDS": "15.5"}),
    ("tempos-curtos", {**WORKER_ENV, "CQ_POLL_TIMEOUT_SECONDS": "3"}),
]

RECONCILE_VIOLATIONS = [
    ("reconciliacao-longa", {"CQ_BOT_RECONCILE_SECONDS": "20.5"}),
    ("renovacao-curta", {"CQ_BOT_RENEW_SECONDS": "12", "CQ_BOT_RECONCILE_SECONDS": "12.01"}),
]


@pytest.mark.parametrize("env", [e for _, e in LEASE_VIOLATIONS], ids=[i for i, _ in LEASE_VIOLATIONS])
def test_lease_invariant_violation_is_config_error(env: dict[str, str]) -> None:
    """A posse cobre a consulta (invariante `lease − renew ≥
    poll_timeout + telegram_timeout`): cada violação → `ConfigError` com a mensagem exata."""
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_LEASE)


@pytest.mark.parametrize("env", [e for _, e in RECONCILE_VIOLATIONS],
                         ids=[i for i, _ in RECONCILE_VIOLATIONS])
def test_reconcile_greater_than_renew_is_config_error(env: dict[str, str]) -> None:
    """Reconciliação no máximo igual à renovação (`reconcile ≤ renew`): reconciliação maior que a renovação →
    `ConfigError` com a mensagem exata."""
    _assert_config_error(lambda: BotSettings.from_env(env), MSG_RECONCILE)


def test_invariant_boundaries_are_accepted() -> None:
    """as igualdades valem (`55 − 20 = 25 + 10`; `60 − 20 = 30 + 10`; reconciliação igual à
    renovação), os tempos curtos do worker e os dos testes (conftest) são aceitos, na
    leitura e na construção direta."""
    assert BotSettings.from_env({"CQ_BOT_LEASE_SECONDS": "55"}).lease_seconds == 55.0
    assert BotSettings.from_env({"CQ_POLL_TIMEOUT_SECONDS": "30"}).poll_timeout_seconds == 30
    assert BotSettings.from_env({"CQ_BOT_RECONCILE_SECONDS": "20"}).reconcile_seconds == 20.0
    worker = BotSettings.from_env(WORKER_ENV)
    assert dataclasses.asdict(worker) == {**DEFAULTS, "lease_seconds": 4.0, "renew_seconds": 1.0,
                                          "reconcile_seconds": 0.2, "poll_timeout_seconds": 1,
                                          "telegram_timeout_seconds": 0.5}
    direct = BotSettings(telegram_api_url="http://127.0.0.1:8081", master_key=MASTER_KEY,
                         lease_seconds=30, renew_seconds=10, reconcile_seconds=0.05,
                         poll_timeout_seconds=1, telegram_timeout_seconds=2)
    assert dataclasses.asdict(direct) == {
        "telegram_api_url": "http://127.0.0.1:8081", "lease_seconds": 30, "renew_seconds": 10,
        "reconcile_seconds": 0.05, "poll_timeout_seconds": 1, "telegram_timeout_seconds": 2,
        "master_key": MASTER_KEY,
    }
    assert BotSettings(lease_seconds=55).lease_seconds == 55


# --- construção direta -----------------------------------------------------------------------------

DIRECT_CASES = [
    ("posse-curta", {"lease_seconds": 54.9}, MSG_LEASE),
    ("renovacao-longa", {"renew_seconds": 25.1}, MSG_LEASE),
    ("consulta-longa", {"poll_timeout_seconds": 31}, MSG_LEASE),
    ("timeout-longo", {"telegram_timeout_seconds": 15.5}, MSG_LEASE),
    ("reconciliacao-longa", {"reconcile_seconds": 20.5}, MSG_RECONCILE),
    ("timeout-zero", {"telegram_timeout_seconds": 0}, msg_number("CQ_TELEGRAM_TIMEOUT_SECONDS")),
    ("timeout-negativo", {"telegram_timeout_seconds": -0.5},
     msg_number("CQ_TELEGRAM_TIMEOUT_SECONDS")),
    ("reconciliacao-negativa", {"reconcile_seconds": -1}, msg_number("CQ_BOT_RECONCILE_SECONDS")),
    ("consulta-zero", {"poll_timeout_seconds": 0}, MSG_POLL),
    ("consulta-nao-inteira", {"poll_timeout_seconds": 3.5}, MSG_POLL),
    ("url-http-fora-de-loopback", {"telegram_api_url": "http://api.telegram.org"}, MSG_URL),
    ("url-barra-final", {"telegram_api_url": "https://api.telegram.org/"}, MSG_URL),
    ("chave-42", {"master_key": MASTER_KEY[:42]}, MSG_KEY),
    ("chave-nao-canonica", {"master_key": _non_canonical(MASTER_KEY)}, MSG_KEY),
]


@pytest.mark.parametrize(("kwargs", "message"), [(k, m) for _, k, m in DIRECT_CASES],
                         ids=[i for i, _, _ in DIRECT_CASES])
def test_direct_construction_runs_the_same_validation(kwargs: dict, message: str) -> None:
    """Na construção direta (`BotSettings(...)`), a mesma validação roda, com os
    nomes das variáveis nas mensagens: cada violação, também por `dataclasses.replace`, →
    `ConfigError` com a mensagem exata, sem a chave."""
    hidden = (MASTER_KEY[:12],)
    _assert_config_error(lambda: BotSettings(**kwargs), message, hidden=hidden)
    base = BotSettings()
    _assert_config_error(lambda: dataclasses.replace(base, **kwargs), message, hidden=hidden)


# --- `repr` e `str` --------------------------------------------------------------------------------

def test_repr_and_str_never_show_the_master_key() -> None:
    """A chave não aparece em logs (`repr` mostra no máximo o `key_id`):
    `repr` e `str` de `BotSettings`, lida do ambiente ou construída direto, não contêm a chave nem um
    pedaço dela, e o campo guarda a chave exata."""
    from_env = BotSettings.from_env({"CQ_MASTER_KEY": MASTER_KEY})
    direct = BotSettings(master_key=OTHER_MASTER_KEY)
    assert from_env.master_key == MASTER_KEY
    assert direct.master_key == OTHER_MASTER_KEY
    for settings, key in ((from_env, MASTER_KEY), (direct, OTHER_MASTER_KEY)):
        for text in (repr(settings), str(settings)):
            assert key not in text
            assert key[:8] not in text
            assert key[-8:] not in text
