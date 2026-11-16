"""Configuração da entrega pelo Telegram.



`DeliverySettings.from_env(env)` lê as 6 variáveis do mapa recebido (nunca do ambiente do processo):

| Variável | Campo | Padrão | Regra |
| --- | --- | --- | --- |
| `CQ_TELEGRAM_FORMAT` | `telegram_format` | `rich` | `rich` · `html` |
| `CQ_TELEGRAM_MAX_BUTTONS` | `max_buttons` | 12 | inteiro de 1 a 100 |
| `CQ_SEND_CHAT_INTERVAL_SECONDS` | `chat_interval_seconds` | 1.0 | número finito > 0 |
| `CQ_SEND_BOT_PER_SECOND` | `bot_per_second` | 30 | inteiro > 0 |
| `CQ_TELEGRAM_PHOTO_MAX_BYTES` | `photo_max_bytes` | 10 000 000 | inteiro > 0 |
| `CQ_FREE_MESSAGE_REPLY_SECONDS` | `free_reply_seconds` | 600 | inteiro > 0 |

Vazio vale como ausente. Toda recusa é `ConfigError` com mensagem **fixa, sem o valor**.
Nada no produto fixa o texto; as mensagens abaixo seguem o molde das outras configurações [I]:
os números com as mesmas frases dos ajudantes da API base (`config._positive_int` e `_positive_float`), a
faixa dos botões como a de `CQ_POLL_TIMEOUT_SECONDS` dos bots e a enumeração como a de
`CQ_SMTP_SECURITY` do e-mail.

O `TelegramChannel(…, delivery=DeliverySettings(...))` declara as capacidades pelo modo:
`rich` → `(can_edit=True, max_buttons, supports_math=True, supports_rich_text=True)` com o
`TelegramRichFormatter`; `html` → `(True, max_buttons, False, False)` com o `TelegramHtmlFormatter`.
`formatter_for(text_format)` devolve o formatador de um dialeto gravado, independente do modo corrente:
`rich` → Rich, `html` → HTML simples, `plain` → `PlainFormatter`; outro → `ValueError`.

O worker lê `DeliverySettings.from_env(os.environ)` por último no `_cmd_worker`: um valor
inválido sai com código 2 e o `config.invalid` da API base, no molde dos outros erros de configuração.

No esqueleto, `from_env` lança `NotImplementedError`, as capacidades do `TelegramChannel` ficam zeradas
(tudo falso e 0) e `formatter`/`formatter_for` lançam; o worker ignora a variável e sobe. Os padrões da
dataclass e os 4 campos de `ChannelCapabilities`, reais no esqueleto, são afirmados na mesma função de
um `from_env`. Todo teste deste arquivo falha no esqueleto: por `NotImplementedError`, por
capacidades diferentes das esperadas ou, no subprocesso, porque o worker sobe em vez de sair com
código 2.
"""

from __future__ import annotations

import dataclasses
import io
import json
import time
import traceback
from collections.abc import Callable
from typing import Any

import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.bots import BotRuntime, TelegramChannel
from cq_server.config import BotSettings, ConfigError, DeliverySettings, Settings
from cq_server.ports.delivery import ChannelCapabilities
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter
from tests.helpers.clock import FakeClock
from tests.helpers.processes import spawn_worker

# --- mensagens exatas (fixas, sem o valor; o texto segue o molde das outras configurações) ----------

MSG_FORMAT = "CQ_TELEGRAM_FORMAT deve ser rich ou html."
MSG_MAX_BUTTONS = "CQ_TELEGRAM_MAX_BUTTONS deve ser um número inteiro de 1 a 100."
MSG_CHAT_INTERVAL = "CQ_SEND_CHAT_INTERVAL_SECONDS deve ser um número finito maior que zero."


def msg_positive_int(var: str) -> str:
    """Inteiro inválido ou não positivo (a mesma frase da API base, `config._positive_int`)."""
    return f"{var} deve ser um número inteiro maior que zero."


# --- valores -------------------------------------------------------------------------------------------

DELIVERY_VARS = ["CQ_TELEGRAM_FORMAT", "CQ_TELEGRAM_MAX_BUTTONS", "CQ_SEND_CHAT_INTERVAL_SECONDS",
                 "CQ_SEND_BOT_PER_SECOND", "CQ_TELEGRAM_PHOTO_MAX_BYTES", "CQ_FREE_MESSAGE_REPLY_SECONDS"]
FIELDS = ["telegram_format", "max_buttons", "chat_interval_seconds", "bot_per_second", "photo_max_bytes",
          "free_reply_seconds"]
DEFAULTS = {"telegram_format": "rich", "max_buttons": 12, "chat_interval_seconds": 1.0,
            "bot_per_second": 30, "photo_max_bytes": 10_000_000, "free_reply_seconds": 600}
INT_FIELDS = ("max_buttons", "bot_per_second", "photo_max_bytes", "free_reply_seconds")

FULL_ENV = {
    "CQ_TELEGRAM_FORMAT": "html",
    "CQ_TELEGRAM_MAX_BUTTONS": "40",
    "CQ_SEND_CHAT_INTERVAL_SECONDS": "0.25",
    "CQ_SEND_BOT_PER_SECOND": "5",
    "CQ_TELEGRAM_PHOTO_MAX_BYTES": "5000000",
    "CQ_FREE_MESSAGE_REPLY_SECONDS": "120",
}
FULL_VALUES = {"telegram_format": "html", "max_buttons": 40, "chat_interval_seconds": 0.25,
               "bot_per_second": 5, "photo_max_bytes": 5_000_000, "free_reply_seconds": 120}

SUBPROCESS_TIMEOUT = 60.0  # subida de um processo Python novo com o pacote inteiro (como em outros testes)


def _assert_reads(env: dict[str, str], expected: dict[str, object]) -> DeliverySettings:
    """`from_env(env)` devolve um `DeliverySettings` com exatamente os campos esperados e os tipos da
    dataclass (inteiros como `int`, o intervalo como `float`)."""
    settings = DeliverySettings.from_env(env)
    assert type(settings) is DeliverySettings
    assert dataclasses.asdict(settings) == expected
    for name in INT_FIELDS:
        assert type(getattr(settings, name)) is int, name
    assert type(settings.chat_interval_seconds) is float
    assert type(settings.telegram_format) is str
    return settings


def _assert_config_error(call: Callable[[], object], message: str, *, hidden: tuple[str, ...]) -> None:
    """`call()` lança `ConfigError` com `message` exata; nem a mensagem, nem o `repr`, nem os `args`, nem
    o traceback formatado (com as exceções encadeadas) contêm os textos de `hidden` (o valor lido)."""
    with pytest.raises(ConfigError) as excinfo:
        call()
    exc = excinfo.value
    assert str(exc) == message
    rendered = [str(exc), repr(exc), *map(str, exc.args), "".join(traceback.format_exception(exc))]
    for text in rendered:
        for value in hidden:
            assert value not in text, value


# --- padrões e leitura ------------------------------------------------------------------------------------

def test_defaults_from_env_and_dataclass() -> None:
    """1 por segundo por chat e 30 por segundo por bot, por padrão; formatação
    rica por padrão (padrões `rich`, 12, 1.0, 30, 10 000 000, 600): `from_env({})`
    dá exatamente os padrões, igual a `DeliverySettings()`; a dataclass é congelada. Reais no
    esqueleto, afirmados aqui: os campos previstos, nesta ordem, com os padrões exatos; e
    `ChannelCapabilities` com exatamente os 4 campos (não ganha campo)."""
    settings = _assert_reads({}, DEFAULTS)
    assert settings == DeliverySettings()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.max_buttons = 3  # type: ignore[misc]
    # Padrões da dataclass.
    assert [f.name for f in dataclasses.fields(DeliverySettings)] == FIELDS
    defaults = {f.name: f.default for f in dataclasses.fields(DeliverySettings)}
    assert defaults == DEFAULTS
    assert dataclasses.asdict(DeliverySettings()) == DEFAULTS
    # `ChannelCapabilities` não muda.
    assert [f.name for f in dataclasses.fields(ChannelCapabilities)] == [
        "can_edit", "max_buttons", "supports_math", "supports_rich_text"]


def test_from_env_reads_only_the_given_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Como nas outras configurações (`from_env(env)` lê o mapa recebido, e não o
    ambiente do processo): com as 6 variáveis no ambiente do processo (e uma inválida), `from_env({})`
    continua com os padrões."""
    for var, value in FULL_ENV.items():
        monkeypatch.setenv(var, value)
    monkeypatch.setenv("CQ_TELEGRAM_FORMAT", "markdown")
    _assert_reads({}, DEFAULTS)


def test_every_variable_is_read() -> None:
    """as 6 variáveis juntas → cada campo com o valor lido."""
    _assert_reads(FULL_ENV, FULL_VALUES)


READ_CASES = [
    ("formato-rich", {"CQ_TELEGRAM_FORMAT": "rich"}, {"telegram_format": "rich"}),
    ("formato-html", {"CQ_TELEGRAM_FORMAT": "html"}, {"telegram_format": "html"}),
    ("botoes-1", {"CQ_TELEGRAM_MAX_BUTTONS": "1"}, {"max_buttons": 1}),
    ("botoes-40", {"CQ_TELEGRAM_MAX_BUTTONS": "40"}, {"max_buttons": 40}),
    ("botoes-100", {"CQ_TELEGRAM_MAX_BUTTONS": "100"}, {"max_buttons": 100}),
    ("intervalo-0.25", {"CQ_SEND_CHAT_INTERVAL_SECONDS": "0.25"}, {"chat_interval_seconds": 0.25}),
    ("intervalo-2", {"CQ_SEND_CHAT_INTERVAL_SECONDS": "2"}, {"chat_interval_seconds": 2.0}),
    ("intervalo-minusculo", {"CQ_SEND_CHAT_INTERVAL_SECONDS": "0.001"}, {"chat_interval_seconds": 0.001}),
    ("bot-1", {"CQ_SEND_BOT_PER_SECOND": "1"}, {"bot_per_second": 1}),
    ("bot-5", {"CQ_SEND_BOT_PER_SECOND": "5"}, {"bot_per_second": 5}),
    ("foto-1", {"CQ_TELEGRAM_PHOTO_MAX_BYTES": "1"}, {"photo_max_bytes": 1}),
    ("foto-5000000", {"CQ_TELEGRAM_PHOTO_MAX_BYTES": "5000000"}, {"photo_max_bytes": 5_000_000}),
    ("livre-1", {"CQ_FREE_MESSAGE_REPLY_SECONDS": "1"}, {"free_reply_seconds": 1}),
    ("livre-120", {"CQ_FREE_MESSAGE_REPLY_SECONDS": "120"}, {"free_reply_seconds": 120}),
]


@pytest.mark.parametrize(("env", "changes"), [(e, c) for _, e, c in READ_CASES],
                         ids=[i for i, *_ in READ_CASES])
def test_each_variable_maps_to_its_own_field(env: dict[str, str], changes: dict[str, object]) -> None:
    """cada variável, sozinha, muda só o seu campo (sem troca de variáveis),
    inclusive nos limites aceitos (1 e 100 botões; 1 nos inteiros; intervalo fracionário)."""
    _assert_reads(env, {**DEFAULTS, **changes})


EMPTY_CASES = [(var, {var: ""}) for var in DELIVERY_VARS] + [
    ("todas", {var: "" for var in DELIVERY_VARS})]


@pytest.mark.parametrize("env", [e for _, e in EMPTY_CASES], ids=[i for i, _ in EMPTY_CASES])
def test_empty_variable_counts_as_absent(env: dict[str, str]) -> None:
    """O Compose repassa as variáveis por `${VAR:-}` (nos três `from_env`, uma
    variável vazia vale como ausente): cada variável vazia, sozinha, e todas vazias → os padrões."""
    _assert_reads(env, DEFAULTS)


def test_other_variables_do_not_feed_delivery() -> None:
    """`Settings` e `BotSettings` não mudam (a configuração nova vai em
    `DeliverySettings`): as variáveis da API base e dos bots no mesmo mapa (inclusive as de tempo do Telegram)
    não mudam `DeliverySettings`, e as 6 variáveis da entrega não mudam `BotSettings`."""
    others = {"CQ_DATABASE_URL": "postgresql://cq@db.example:5432/cq", "CQ_LEASE_SECONDS": "45",
              "CQ_POLL_INTERVAL_SECONDS": "0.5", "CQ_TELEGRAM_TIMEOUT_SECONDS": "3",
              "CQ_POLL_TIMEOUT_SECONDS": "20", "CQ_BOT_LEASE_SECONDS": "80"}
    _assert_reads(others, DEFAULTS)
    assert dataclasses.asdict(BotSettings.from_env(FULL_ENV)) == dataclasses.asdict(BotSettings())
    r2 = Settings.from_env({"CQ_DATABASE_URL": others["CQ_DATABASE_URL"], **FULL_ENV})
    assert dataclasses.asdict(r2) == dataclasses.asdict(Settings(database_url=others["CQ_DATABASE_URL"]))


# --- recusas ---------------------------------------------------------------------------------------------

INVALID_FORMATS = ["markdown", "MarkdownV2", "RICH", "Rich", "HTML", "plain", "rich_html", " rich", "rich ",
                   "SentinelaFormato7"]


@pytest.mark.parametrize("raw", INVALID_FORMATS)
def test_unknown_telegram_format_is_config_error(raw: str) -> None:
    """Dois modos, `rich` e `html` (mensagens sem o
    valor): outro dialeto (inclusive `plain`, que é só do console, maiúsculas e espaços) →
    `ConfigError` com a mensagem exata, sem o valor."""
    hidden = (raw,) if raw.startswith("Sentinela") or raw == "markdown" else ()
    _assert_config_error(lambda: DeliverySettings.from_env({"CQ_TELEGRAM_FORMAT": raw}), MSG_FORMAT,
                         hidden=hidden)


INVALID_MAX_BUTTONS = ["0", "101", "-1", "1000", "abc", "1.5", "12.0", "1e2", "SentinelaBotoes3"]


@pytest.mark.parametrize("raw", INVALID_MAX_BUTTONS)
def test_max_buttons_outside_1_to_100_is_config_error(raw: str) -> None:
    """Número de botões declarado pelo canal (inteiro de 1 a 100): 0,
    101, negativo, texto e não inteiros → `ConfigError` com a mensagem exata, sem o valor."""
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: DeliverySettings.from_env({"CQ_TELEGRAM_MAX_BUTTONS": raw}),
                         MSG_MAX_BUTTONS, hidden=hidden)


INVALID_INTERVALS = ["0", "0.0", "-1", "-0.5", "nan", "inf", "-inf", "abc", "SentinelaIntervalo9"]


@pytest.mark.parametrize("raw", INVALID_INTERVALS)
def test_chat_interval_must_be_finite_and_positive(raw: str) -> None:
    """Intervalo por chat (`CQ_SEND_CHAT_INTERVAL_SECONDS` finito, > 0): 0, negativo, `nan`,
    `inf` e texto → `ConfigError` com a mensagem exata, sem o valor."""
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    _assert_config_error(lambda: DeliverySettings.from_env({"CQ_SEND_CHAT_INTERVAL_SECONDS": raw}),
                         MSG_CHAT_INTERVAL, hidden=hidden)


POSITIVE_INT_VARS = ["CQ_SEND_BOT_PER_SECOND", "CQ_TELEGRAM_PHOTO_MAX_BYTES", "CQ_FREE_MESSAGE_REPLY_SECONDS"]
INVALID_POSITIVE_INTS = ["0", "-1", "-30", "abc", "2.5", "1e3", "nan", "SentinelaInteiro4"]


@pytest.mark.parametrize("raw", INVALID_POSITIVE_INTS)
@pytest.mark.parametrize("var", POSITIVE_INT_VARS)
def test_positive_integer_variables_reject_other_values(var: str, raw: str) -> None:
    """30 por segundo por bot, limite de foto e intervalo da resposta fixa
    (inteiros > 0): 0, negativos, texto e não inteiros → `ConfigError` com a mensagem
    exata da variável, sem o valor; as outras variáveis válidas no mesmo mapa não mudam a recusa."""
    hidden = (raw,) if raw.startswith("Sentinela") else ()
    env = {**FULL_ENV, var: raw}
    _assert_config_error(lambda: DeliverySettings.from_env(env), msg_positive_int(var), hidden=hidden)


# --- capacidades e formatadores do `TelegramChannel` -------------------------------------------------------

def _no_api(token: str) -> Any:
    raise AssertionError("o teste das capacidades não fala com o Telegram")


def _channel(delivery: DeliverySettings | None = None) -> TelegramChannel:
    """Um `TelegramChannel` sem banco nem Bot API (a conexão é preguiçosa e não é usada aqui)."""
    runtime = BotRuntime(BotSettings(), api_factory=_no_api)
    kwargs: dict[str, Any] = {} if delivery is None else {"delivery": delivery}
    return TelegramChannel("postgresql://cq@db.invalid:5432/cq", clock=FakeClock(), runtime=runtime,
                           default=ConsoleChannel(io.StringIO()), **kwargs)


MODES = [
    ("rich-padrao", None, ChannelCapabilities(can_edit=True, max_buttons=12, supports_math=True,
                                              supports_rich_text=True), TelegramRichFormatter),
    ("rich-7", DeliverySettings(telegram_format="rich", max_buttons=7),
     ChannelCapabilities(can_edit=True, max_buttons=7, supports_math=True, supports_rich_text=True),
     TelegramRichFormatter),
    ("html-12", DeliverySettings(telegram_format="html"),
     ChannelCapabilities(can_edit=True, max_buttons=12, supports_math=False, supports_rich_text=False),
     TelegramHtmlFormatter),
    ("html-100", DeliverySettings(telegram_format="html", max_buttons=100),
     ChannelCapabilities(can_edit=True, max_buttons=100, supports_math=False, supports_rich_text=False),
     TelegramHtmlFormatter),
]


@pytest.mark.parametrize(("delivery", "capabilities", "formatter"), [(d, c, f) for _, d, c, f in MODES],
                         ids=[i for i, *_ in MODES])
def test_telegram_channel_capabilities_and_formatter_follow_the_mode(
        delivery: DeliverySettings | None, capabilities: ChannelCapabilities, formatter: type) -> None:
    """O núcleo decide pelas capacidades declaradas (editar, número de botões, fórmulas,
    formatação rica), nos modos `rich`, o padrão, e `html`:
    `rich` → edita, `max_buttons` da configuração,
    fórmulas e formatação rica, com o `TelegramRichFormatter`; `html` → edita, `max_buttons`, sem
    fórmulas nem formatação rica, com o `TelegramHtmlFormatter`; sem `delivery`, o padrão (`rich`, 12).
    As capacidades são o tipo exato `ChannelCapabilities`, com os tipos exatos dos campos."""
    channel = _channel(delivery)
    try:
        caps = channel.capabilities
        assert type(caps) is ChannelCapabilities
        assert caps == capabilities
        assert type(caps.can_edit) is bool and type(caps.supports_math) is bool
        assert type(caps.supports_rich_text) is bool and type(caps.max_buttons) is int
        assert type(channel.formatter) is formatter
        assert channel.formatter.text_format == ("rich" if formatter is TelegramRichFormatter else "html")
    finally:
        channel.close()


FORMATTER_FOR = [("rich", TelegramRichFormatter), ("html", TelegramHtmlFormatter), ("plain", PlainFormatter)]
UNKNOWN_DIALECTS = ["markdown", "", "RICH", "Html", "rich_html", "texto", " plain"]


@pytest.mark.parametrize("mode", ["rich", "html"])
def test_telegram_channel_formatter_for_each_recorded_dialect(mode: str) -> None:
    """O formatador de uma entrega aberta é o do dialeto gravado, mesmo que o modo mude
    (`TelegramChannel.formatter_for`: `rich` → `TelegramRichFormatter`, `html` →
    `TelegramHtmlFormatter`, `plain` → `PlainFormatter`; dialeto fora dos três → `ValueError`;
    os três de `cq_server.telegramrender`): nos dois modos do canal, cada dialeto dá o seu formatador
    (com o `text_format` do dialeto), e os desconhecidos dão `ValueError`."""
    channel = _channel(DeliverySettings(telegram_format=mode))
    try:
        for dialect, expected in FORMATTER_FOR:
            formatter = channel.formatter_for(dialect)
            assert type(formatter) is expected, (mode, dialect)
            assert formatter.text_format == dialect
        for dialect in UNKNOWN_DIALECTS:
            with pytest.raises(ValueError):
                channel.formatter_for(dialect)
    finally:
        channel.close()


# --- o worker com a configuração inválida --------------------------------------------------------

def _json_records(out: str) -> list[dict]:
    """As linhas JSON da saída padrão do processo (tudo sai em JSON)."""
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _wait(predicate: Callable[[], Any], message: str, timeout: float) -> Any:
    """Espera real, sempre com prazo; devolve o valor verdadeiro."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


def test_worker_process_with_invalid_telegram_format_exits_2(db_url: str, bot_api: Any) -> None:
    """Configuração da entrega até os tratadores (o `_cmd_worker` lê `DeliverySettings.from_env(os.environ)` por último, depois do
    e-mail, do motor e da obtenção; `ConfigError` → `config.invalid` e código 2), no molde dos
    outros erros de configuração: `python -m cq_server worker` com `CQ_TELEGRAM_FORMAT=markdown`, sem
    e-mail, motor nem obtenção (os três ausentes: só os avisos de desligado), sai com código 2 sem
    `worker.started`, com exatamente um registro `{level: ERROR, logger: cq_server.main, event:
    config.invalid, detail: <mensagem exata>}` e sem o valor em fd 1 e 2; o `worker.mail_disabled` do
    e-mail sai antes, uma vez (não se afirma a contagem total de registros)."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-formato-invalido",
                         "CQ_TELEGRAM_FORMAT": "markdown"})
    try:
        # O processo tem de sair antes de subir o laço: um `worker.started` já é falha.
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0],
              f"o cq-server worker não saiu nem subiu em {SUBPROCESS_TIMEOUT} s", SUBPROCESS_TIMEOUT)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o cq-server worker não saiu com código 2 (saiu: {code}; registros: {events})"
    assert "worker.started" not in events, events
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    expected = {"level": "ERROR", "logger": "cq_server.main", "event": "config.invalid",
                "detail": MSG_FORMAT}
    assert expected.items() <= invalid[0].items(), invalid[0]
    assert events.count("worker.mail_disabled") == 1, events
    assert events.index("worker.mail_disabled") < events.index("config.invalid"), events
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert "markdown" not in text, f"valor de CQ_TELEGRAM_FORMAT na {surface}"
