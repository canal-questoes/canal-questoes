"""Amostras de registros da pilha para a redação do `stack_logs`.


Cada amostra é uma linha como o `local_commands.run_logs` do CLI a devolve: já limpa, com o prefixo do Compose
(`api-1  | `, `worker-1  | `, `postgres-1  | `). As linhas JSON saem do `JsonFormatter` **real** do servidor
(`cq_server.logs`; chaves `level`, `logger`, `event`, `time` e os extras, na ordem dele: os extras primeiro),
com o horário fixo do `FakeClock` (`START`). Identidades só do conjunto canário de `tests/helpers/canary.py`;
segredos só os sentinelas deste módulo (`SENTINELS`); nenhum dado real.

`LogSample.outcome` é o resultado que a regra de redação dos registros dá à linha, pela primeira regra que decide:
`"clean"`, `"redacted"`, `"omitted"` (a linha vira `LOG_LINE_MARKER` e fica em `lines`) ou `DROPPED` (regra 1:
sem item em `lines`, só soma em `omitted`). `rule` diz qual regra decide. O resultado vale com o serviço
da amostra entre os pedidos; com ele fora dos pedidos, toda amostra é `DROPPED`.

Conjuntos:
- `EVENT_LINES`: uma linha por evento de `LOG_EVENTS`, só com os extras permitidos do evento, cada valor no
  tipo; `"clean"`, salvo os eventos com campo de `ALWAYS_MASKED_FIELDS` (`"redacted"`).
- `OMITTED_EVENT_LINES`: uma linha por evento de `LOG_EVENTS_OMITTED` (hoje vazio).
- `JSON_VARIANTS`: campo fora da lista (com nome em `SERVER_LOG_KEYS`, `"redacted"`; com nome fora dele, inclusive a
  chave neutra `ana`, `"omitted"`), tipo errado, evento
  desconhecido, campo sempre mascarado, JSON inválido, JSON que não é objeto, base inválida, mensagem livre de
  biblioteca.
- `POSTGRES_TEMPLATE_LINES`: uma linha por modelo de `POSTGRES_TEMPLATES` (chave: o índice do modelo);
  `UNCOVERED_TEMPLATES`: os índices dos modelos sem amostra (vazio hoje; um modelo novo sem amostra aparece
  aqui, sem quebrar a coleta).
- `POSTGRES_VARIANTS`: mensagem sem modelo, `DETAIL` de unicidade com RA e e-mail canário, `STATEMENT` e
  `DETAIL: parameters` com valores, linha sem `POSTGRES_PREFIX`.
- `DROPPED_LINES`: linhas sem o prefixo do Compose ou de serviço fora de `PREFIX_PATTERN`.
- `SECOND_LAYER`: e-mail, RA, identificador do Telegram, token da API, token de bot, chave-mestra base64url,
  `postgresql://cq:<senha>@…` e a senha do SMTP, cada um no lugar que chega mais longe nas regras.
- `OUT_OF_REACH`: os casos fora do alcance da segunda camada: `bot.lease` com UUID de 5 ou mais
  dígitos seguidos (`"clean"`), Postgres com `[pid]` de 5 dígitos (`"clean"`) e `checkpoint complete` com
  número de 5 ou mais dígitos (`"omitted"`, falha fechada).
- `ALL_SAMPLES`: tudo, por nome. `SENTINELS`: os segredos-sentinela. `CANARY_IN_LOGS`: as identidades
  canário usadas (alvos de `find_canary(..., prefixes=CANARY_PREFIXES)`).
- `RESULT_LIMIT_CHARS`: o teto de 50.000 caracteres como literal (não a constante de produção).
- `bulk(n)`: linhas limpas e distintas (`task.processed` com `id` crescente) que, com `n = BULK_DEFAULT`,
  passam de `RESULT_LIMIT_CHARS` (com folga de 20%) só no texto das mensagens.
- `by_service(samples)`: o mapa serviço → linhas, para o `FakeStack(logs=…)`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cq_client.log_redaction import (
    ALWAYS_MASKED_FIELDS,
    LOG_EVENTS,
    LOG_EVENTS_OMITTED,
    POSTGRES_TEMPLATES,
    FieldKind,
)
from cq_client.mcp_server import LOGS_TAIL_MAX
from cq_server.logs import JsonFormatter

from tests.helpers.canary import ALL_PREFIXES, TG_TEODORICO, ZEFIRO
from tests.helpers.clock import START

DROPPED = "dropped"

# O teto do texto de um resultado, como literal fixo (50.000 caracteres serializados), e não a
# constante `MAX_RESULT_CHARS` do código de produção, que não é congelado: o volume de `bulk()` e os testes que medem
# o corte não podem crescer junto com a constante.
RESULT_LIMIT_CHARS = 50_000

# O prefixo do Compose de cada serviço (`<serviço>-<n>  | `; `PREFIX_PATTERN` aceita os espaços).
COMPOSE_PREFIX: Mapping[str, str] = {"api": "api-1  | ", "worker": "worker-1  | ", "postgres": "postgres-1  | "}

# O prefixo de uma linha do Postgres (`log_line_prefix` padrão da imagem: `%m [%p] `).
POSTGRES_TIME = "2031-01-01 00:00:00.123 UTC"


@dataclass(frozen=True)
class LogSample:
    """Uma linha da saída do Compose e o que a redação faz com ela (ver o docstring do módulo)."""

    name: str
    line: str
    service: str | None
    outcome: str
    rule: str

    @property
    def message(self) -> str:
        """A linha sem o prefixo do Compose (o `text` de uma linha `"clean"`, a menos da reserialização)."""
        if self.service is None:
            return self.line
        return self.line[len(COMPOSE_PREFIX[self.service]):]


# --------------------------------------------------------------------------------------------------
# Segredos-sentinela e identidades canário
# --------------------------------------------------------------------------------------------------

# Token da API (`cq_…`), também um identificador Python válido (chega ao tipo `ident`).
SENTINEL_API_TOKEN = "cq_sentinelaLogsMcp7Wq3Zr9Tx5Kv2"
# Token de bot (`<id>:<segredo>`).
SENTINEL_BOT_TOKEN = "5550001234:AAHsentinelaBotLogs_Mcp9Xq4Lw7Rt2Zk"
# Chave-mestra base64url de 43 caracteres, sem `-` e começando por letra (também um `ident` válido).
SENTINEL_MASTER_KEY = "Ksentinela_Chave_Mestra_Logs_Mcp_7Qw3Er9TyU"
# A senha do banco (no `postgresql://cq:<senha>@…`) e a senha do SMTP.
SENTINEL_DB_PASSWORD = "sentinela-banco-Hj6Pq2Vn8Lz4"
SENTINEL_SMTP_PASSWORD = "sentinela-smtp-Mb5Xc1Wd7Kf3"
SENTINEL_DB_URL = f"postgresql://cq:{SENTINEL_DB_PASSWORD}@postgres:5432/cq"

SENTINELS: tuple[str, ...] = (SENTINEL_API_TOKEN, SENTINEL_BOT_TOKEN, SENTINEL_MASTER_KEY, SENTINEL_DB_PASSWORD,
                              SENTINEL_SMTP_PASSWORD)

assert len(SENTINEL_MASTER_KEY) == 43, "a chave-mestra sentinela tem 43 caracteres"

# As identidades canário usadas nas amostras (nome, RA e e-mail de `ZEFIRO`; o id do Telegram de
# `TG_TEODORICO`); os alvos delas saem com `prefixes=CANARY_PREFIXES`.
CANARY_IN_LOGS: tuple[Any, ...] = (ZEFIRO, TG_TEODORICO)
CANARY_PREFIXES: tuple[str, ...] = ALL_PREFIXES

# --------------------------------------------------------------------------------------------------
# Linhas JSON pelo `JsonFormatter` real
# --------------------------------------------------------------------------------------------------

_FORMATTER = JsonFormatter()

_API_EVENTS = frozenset({"request", "http.unexpected_status", "migrations.applied", "config.invalid",
                         "usage.invalid", "interrupted", "unhandled_exception", "warning"})
# **[F]** os loggers do servidor (`cq_server/__main__.py:77`, `api/middleware.py:35`, `tasks.py:60` e os demais
# do worker, `py.warnings` em `__main__.py:138`).
_LOGGERS = {"request": "cq_server.api", "http.unexpected_status": "cq_server.api",
            "migrations.applied": "cq_server.main", "config.invalid": "cq_server.main",
            "usage.invalid": "cq_server.main", "interrupted": "cq_server.main",
            "unhandled_exception": "cq_server.main", "warning": "py.warnings"}
_LEVELS = {"config.invalid": "ERROR", "usage.invalid": "ERROR", "unhandled_exception": "ERROR",
           "exception": "ERROR", "warning": "WARNING", "http.unexpected_status": "WARNING",
           "bot.token_unreadable": "WARNING", "bot.poll_problem": "WARNING", "delivery.message_failed": "WARNING",
           "worker.staging_sweep_failed": "WARNING", "telegram.callback_answer_failed": "WARNING"}

# Valores no tipo, por campo (os demais saem do tipo, em `_value_for`); nenhum toca a segunda camada.
_FIELD_VALUES: Mapping[str, Any] = {
    "state": "done", "outcome": "processed", "reason": "lease_lost", "result": "sent", "role": "student",
    "operation": "send_message", "change": "acquired", "problem": "unauthorized", "expected": "head",
    "version": "head", "schema_version": "head", "detail": "missing_master_key", "error": "BadRequest",
    "error_type": "OSError", "category": "DeprecationWarning", "location": "cq_server/api/app.py:42",
    "exception": "RuntimeError", "route": "/v1/offerings/{offering_id}", "where": "cq_server/worker.py:182",
}
_EVENT_FIELD_VALUES: Mapping[tuple[str, str], Any] = {
    ("request", "status"): 200,
    ("http.unexpected_status", "status"): 502,
}
SAMPLE_UUID = "6f1c2a4e-3b7d-4c9a-8e2f-1a3b5c7d9e0f"


def _value_for(event: str, field: str, kind: FieldKind) -> Any:
    if (event, field) in _EVENT_FIELD_VALUES:
        return _EVENT_FIELD_VALUES[(event, field)]
    if kind.kind == "enum":
        return kind.values[0]
    if field in _FIELD_VALUES:
        return _FIELD_VALUES[field]
    return {"int": 7, "bool": True, "null": None, "uuid": SAMPLE_UUID, "token": "done", "ident": "ValueError",
            "route": "/v1/offerings/{offering_id}", "where": "cq_server/worker.py:182",
            "iso_time": "2031-01-01T00:00:00.000Z"}[kind.kind]


def _raise_sample() -> None:
    raise RuntimeError("mensagem da exceção que nunca sai")


def json_message(event: str | None, extras: Mapping[str, Any] | None = None, *, logger: str = "cq_server.worker",
                 level: str = "INFO", msg: str | None = None, with_exception: bool = False) -> str:
    """Uma linha do `JsonFormatter` real (sem o prefixo do Compose). `event=None` deixa o `event` sair da
    mensagem (`msg`), como numa linha de biblioteca; `with_exception` põe um `exc_info` real (o `event` vira
    `"exception"`, com `exc_type` e `where` deste módulo)."""
    record = logging.LogRecord(logger, getattr(logging, level), __file__, 1, msg if msg is not None else
                               (event or ""), (), None)
    record.created = START.timestamp()
    record.msecs = 0.0
    if event is not None:
        record.__dict__["event"] = event
    record.__dict__.update(dict(extras or {}))
    if with_exception:
        try:
            _raise_sample()
        except RuntimeError as exc:
            record.exc_info = (type(exc), exc, exc.__traceback__)
    return _FORMATTER.format(record)


def compose(service: str, message: str) -> str:
    """A linha com o prefixo do Compose do serviço."""
    return COMPOSE_PREFIX[service] + message


def _event_service(event: str) -> str:
    return "api" if event in _API_EVENTS else "worker"


def _event_extras(event: str) -> dict[str, Any]:
    fields = LOG_EVENTS[event]
    skipped = {"exc_type", "where"} if event == "exception" else set()  # saem do `exc_info` real
    return {name: _value_for(event, name, kind) for name, kind in fields.items() if name not in skipped}


def event_message(event: str, extras: Mapping[str, Any] | None = None, *, logger: str | None = None,
                  level: str | None = None) -> str:
    """A linha JSON do evento com o logger e o nível dele; `extras` por cima dos permitidos."""
    values = _event_extras(event)
    values.update(dict(extras or {}))
    return json_message(event if event != "exception" else None, values,
                        logger=logger or _LOGGERS.get(event, "cq_server.worker"),
                        level=level or _LEVELS.get(event, "INFO"), msg=event,
                        with_exception=event == "exception")


def _sample(name: str, service: str, message: str, outcome: str, rule: str) -> LogSample:
    return LogSample(name, compose(service, message), service, outcome, rule)


def _always_masked(event: str) -> bool:
    return any(masked_event == event for masked_event, _ in ALWAYS_MASKED_FIELDS)


EVENT_LINES: dict[str, LogSample] = {
    event: _sample(f"event:{event}", _event_service(event), event_message(event),
                   "redacted" if _always_masked(event) else "clean",
                   "2: campo de ALWAYS_MASKED_FIELDS" if _always_masked(event) else "2: evento e campos no tipo")
    for event in LOG_EVENTS
}

OMITTED_EVENT_LINES: dict[str, LogSample] = {
    event: _sample(f"event-omitted:{event}", _event_service(event),
                   json_message(event, {}, logger=_LOGGERS.get(event, "cq_server.worker")), "omitted",
                   "2: evento de LOG_EVENTS_OMITTED")
    for event in sorted(LOG_EVENTS_OMITTED)
}


def _base_line(**overrides: Any) -> str:
    base: dict[str, Any] = {"id": 7, "state": "done", "level": "INFO", "logger": "cq_server.worker",
                            "event": "task.processed", "time": "2031-01-01T00:00:00.000Z"}
    for key, value in overrides.items():
        if value is _ABSENT:
            base.pop(key, None)
        else:
            base[key] = value
    return json.dumps(base, ensure_ascii=True)


_ABSENT = object()

JSON_VARIANTS: dict[str, LogSample] = {sample.name: sample for sample in (
    _sample("field-outside-list", "worker", event_message("task.processed", {"worker_id": "worker_a"}),
            "redacted", "2: campo fora da lista do evento, com nome em SERVER_LOG_KEYS"),
    _sample("field-outside-list-canary", "worker",
            event_message("invite.email", {"to": ZEFIRO.email, "student": ZEFIRO.name}),
            "omitted", "2: campos fora da lista com e-mail e nome canário, com nome fora de SERVER_LOG_KEYS"),
    _sample("field-outside-list-db-url", "worker", event_message("worker.stopped", {"dsn": SENTINEL_DB_URL}),
            "omitted", "2: campo fora da lista com a URL do banco, com nome fora de SERVER_LOG_KEYS"),
    _sample("field-outside-list-unknown-key", "worker", event_message("task.processed", {"ana": "x"}),
            "omitted", "2: chave neutra fora da lista e fora de SERVER_LOG_KEYS"),
    _sample("scheduler-swept-enqueued", "worker",
            event_message("scheduler.swept", {"enqueued": {"dispatch_close": 2, "digest": 1}}),
            "redacted", "2: o dicionário `enqueued` fica fora da lista, com nome em SERVER_LOG_KEYS"),
    _sample("wrong-type-int-as-string", "worker", event_message("task.processed", {"id": "7"}),
            "redacted", "2: `int` com texto"),
    _sample("wrong-type-int-as-bool", "worker", event_message("task.processed", {"id": True}),
            "redacted", "2: `int` com booleano"),
    _sample("wrong-type-token-with-digits", "worker", event_message("outbox.dispatched", {"state": "state_99001"}),
            "redacted", "2: `token` com dígitos"),
    _sample("wrong-type-uuid-uppercase", "worker",
            event_message("bot.poll_problem", {"offering_id": SAMPLE_UUID.upper()}),
            "redacted", "2: `uuid` em maiúsculas"),
    _sample("wrong-type-route-with-digits", "api", event_message("request", {"route": "/v1/people/42"}),
            "redacted", "2: `route` com dígitos fora de {…}"),
    _sample("wrong-enum", "api", event_message("request", {"method": "BREW"}),
            "redacted", "2: `enum` fora dos valores"),
    _sample("unknown-event", "worker", json_message("cq.desconhecido", {"id": 7}),
            "omitted", "2: evento fora de LOG_EVENTS"),
    _sample("always-masked-outbox-skipped", "worker", event_message("outbox.skipped"),
            "redacted", "2: ALWAYS_MASKED_FIELDS (outbox.skipped, reason)"),
    _sample("always-masked-config-invalid", "api", event_message("config.invalid"),
            "redacted", "2: ALWAYS_MASKED_FIELDS (config.invalid, detail)"),
    _sample("invalid-json", "api", '{"level": "INFO", "logger": "cq_server.api", "event": "request", "ti',
            "omitted", "2: JSON inválido"),
    _sample("json-not-object", "api", '["request", 200]', "omitted", "2: JSON que não é objeto"),
    _sample("base-invalid-level", "worker", _base_line(level="TRACE"), "omitted", "2: `level` fora da base"),
    _sample("base-missing-time", "worker", _base_line(time=_ABSENT), "omitted", "2: sem `time`"),
    _sample("base-invalid-logger", "worker", _base_line(logger="cq server"), "omitted", "2: `logger` inválido"),
    _sample("library-free-text", "api", "INFO:     Started server process [1]", "omitted",
            "2: mensagem livre (não é JSON)"),
    _sample("library-free-text-url", "api",
            "INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)", "omitted",
            "2: mensagem livre (não é JSON)"),
    _sample("library-through-formatter", "api",
            json_message(None, logger="uvicorn.error", msg="Application startup complete."), "omitted",
            "2: `event` é a mensagem da biblioteca"),
)}

# --------------------------------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------------------------------


def postgres_message(severity: str, message: str, *, pid: int = 1) -> str:
    """Uma linha do Postgres (sem o prefixo do Compose): `<data> <hora> <fuso> [<pid>] <SEVERIDADE>:  <msg>`."""
    return f"{POSTGRES_TIME} [{pid}] {severity}:  {message}"


# Mensagens candidatas aos modelos (cada modelo recebe a primeira que casa); nenhuma toca a segunda camada.
_TEMPLATE_CANDIDATES: tuple[str, ...] = (
    "database system is ready to accept connections",
    "database system was shut down at 2031-01-01 00:00:00 UTC",
    "checkpoint starting: time",
    "checkpoint complete: wrote 3 buffers (0.0%); 0 wal file(s) added, 0 removed, 0 recycled; write=0.101 s, "
    "sync=0.002 s, total=0.110 s; sync files=2, longest=0.001 s, average=0.001 s",
    "received fast shutdown request",
    "received smart shutdown request",
    "aborting any active transactions",
    "shutting down",
    "database system is shut down",
    "background worker \"logical replication launcher\" (PID 63) exited with exit code 1",
    "listening on IPv4 address \"0.0.0.0\", port 5432",
    "listening on IPv6 address \"::\", port 5432",
    "listening on Unix socket \"/var/run/postgresql/.s.PGSQL.5432\"",
    "starting PostgreSQL 16.4 on x86_64-pc-linux-musl, compiled by gcc (Alpine 13.2.1) 13.2.1, 64-bit",
)


def _template_lines() -> tuple[dict[int, LogSample], tuple[int, ...]]:
    covered: dict[int, LogSample] = {}
    uncovered: list[int] = []
    for index, pattern in enumerate(POSTGRES_TEMPLATES):
        message = next((m for m in _TEMPLATE_CANDIDATES if pattern.match(m)), None)
        if message is None:
            uncovered.append(index)
            continue
        covered[index] = _sample(f"postgres-template:{index}", "postgres", postgres_message("LOG", message),
                                 "clean", "3: mensagem de POSTGRES_TEMPLATES")
    return covered, tuple(uncovered)


POSTGRES_TEMPLATE_LINES, UNCOVERED_TEMPLATES = _template_lines()

POSTGRES_VARIANTS: dict[str, LogSample] = {sample.name: sample for sample in (
    _sample("postgres-no-template", "postgres",
            postgres_message("ERROR", "duplicate key value violates unique constraint \"people_ra_key\"", pid=77),
            "redacted", "3: mensagem sem modelo"),
    _sample("postgres-fatal-auth", "postgres",
            postgres_message("FATAL", "password authentication failed for user \"cq\"", pid=78),
            "redacted", "3: mensagem sem modelo"),
    _sample("postgres-detail-unique", "postgres",
            postgres_message("DETAIL", f"Key (ra, email)=({ZEFIRO.ra}, {ZEFIRO.email}) already exists.", pid=77),
            "omitted", "3: severidade DETAIL"),
    _sample("postgres-statement", "postgres",
            postgres_message("STATEMENT", "INSERT INTO people (name, ra, email) VALUES "
                             f"('{ZEFIRO.name}', '{ZEFIRO.ra}', '{ZEFIRO.email}')", pid=77),
            "omitted", "3: severidade STATEMENT"),
    _sample("postgres-detail-parameters", "postgres",
            postgres_message("DETAIL", f"parameters: $1 = '{ZEFIRO.name}', $2 = '{ZEFIRO.ra}', "
                             f"$3 = '{ZEFIRO.email}'", pid=77),
            "omitted", "3: severidade DETAIL"),
    _sample("postgres-no-prefix", "postgres", "PostgreSQL init process complete; ready for start up.",
            "omitted", "3: sem POSTGRES_PREFIX"),
)}

# --------------------------------------------------------------------------------------------------
# Regra 1: descartadas
# --------------------------------------------------------------------------------------------------

DROPPED_LINES: dict[str, LogSample] = {sample.name: sample for sample in (
    LogSample("no-compose-prefix", "Attaching to api-1, postgres-1, worker-1", None, DROPPED,
              "1: sem PREFIX_PATTERN"),
    LogSample("no-compose-prefix-json", json_message("invite.email", {"to": ZEFIRO.email}), None, DROPPED,
              "1: sem PREFIX_PATTERN (JSON com e-mail canário)"),
    LogSample("other-service", f"engine-1  | aluno {ZEFIRO.name} RA {ZEFIRO.ra}", None, DROPPED,
              "1: serviço fora de PREFIX_PATTERN"),
)}

# --------------------------------------------------------------------------------------------------
# Segunda camada e o alcance dela
# --------------------------------------------------------------------------------------------------

SECOND_LAYER: dict[str, LogSample] = {sample.name: sample for sample in (
    # e-mail: nenhum tipo do alcance o aceita (o `@`); vai no `event` livre, que a regra 2 já omite.
    _sample("second-email", "worker",
            json_message(None, logger="cq_server.worker", msg=f"convite enviado a {ZEFIRO.email}"),
            "omitted", "2: `event` livre com e-mail canário"),
    _sample("second-ra-ident", "worker",
            event_message("telegram.callback_answer_failed", {"error": f"ra_{ZEFIRO.ra}"}),
            "omitted", "4: RA num `ident`"),
    _sample("second-telegram-id-ident", "worker",
            event_message("worker.staging_sweep_failed", {"error_type": f"chat_{TG_TEODORICO.user_id}"}),
            "omitted", "4: id do Telegram num `ident`"),
    _sample("second-api-token-ident", "worker",
            event_message("worker.staging_sweep_failed", {"error_type": SENTINEL_API_TOKEN}),
            "omitted", "4: token da API num `ident`"),
    _sample("second-master-key-logger", "worker",
            event_message("task.processed", logger=SENTINEL_MASTER_KEY),
            "omitted", "4: chave-mestra no `logger`"),
    # token de bot, URL do banco e senha do SMTP: nenhum tipo do alcance os aceita (`:`, `@`, `-`).
    _sample("second-bot-token", "worker",
            json_message(None, logger="httpx", msg="HTTP Request: POST https://api.telegram.org/bot"
                         f"{SENTINEL_BOT_TOKEN}/getUpdates \"HTTP/1.1 200 OK\""),
            "omitted", "2: `event` livre com token de bot"),
    _sample("second-db-url", "worker",
            f"sqlalchemy.exc.OperationalError: connection to {SENTINEL_DB_URL} failed", "omitted",
            "2: mensagem livre com a URL do banco"),
    _sample("second-smtp-password", "worker",
            "smtplib.SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted: "
            f"{SENTINEL_SMTP_PASSWORD}')", "omitted", "2: mensagem livre com a senha do SMTP"),
)}

# UUID v4 minúsculo com 8 dígitos seguidos (o tipo `uuid` exclui identidade: fora da segunda camada).
UUID_WITH_DIGIT_RUN = "12345678-1234-4123-8123-123456789012"

OUT_OF_REACH: dict[str, LogSample] = {sample.name: sample for sample in (
    _sample("bot-lease-uuid-digits", "worker", event_message("bot.lease", {"offering_id": UUID_WITH_DIGIT_RUN}),
            "clean", "4 fora do alcance: `uuid`"),
    _sample("postgres-pid-five-digits", "postgres",
            postgres_message("LOG", "database system is ready to accept connections", pid=12345),
            "clean", "4 fora do alcance: `[pid]` do prefixo"),
    _sample("postgres-checkpoint-five-digits", "postgres",
            postgres_message("LOG", "checkpoint complete: wrote 12345 buffers (75.3%); 0 wal file(s) added, "
                             "0 removed, 0 recycled; write=0.101 s, sync=0.002 s, total=0.110 s"),
            "omitted", "4: número de 5 ou mais dígitos na mensagem do modelo (falha fechada)"),
)}

ALL_SAMPLES: dict[str, LogSample] = {
    **{s.name: s for s in EVENT_LINES.values()},
    **{s.name: s for s in OMITTED_EVENT_LINES.values()},
    **JSON_VARIANTS,
    **{s.name: s for s in POSTGRES_TEMPLATE_LINES.values()},
    **POSTGRES_VARIANTS,
    **DROPPED_LINES,
    **SECOND_LAYER,
    **OUT_OF_REACH,
}

# --------------------------------------------------------------------------------------------------
# Volume e agrupamento
# --------------------------------------------------------------------------------------------------


def bulk_line(index: int) -> str:
    """Uma linha limpa e distinta (`task.processed` com `id = index`)."""
    return compose("worker", event_message("task.processed", {"id": index}))


def _bulk_default() -> int:
    total = 0
    count = 0
    while total <= RESULT_LIMIT_CHARS + RESULT_LIMIT_CHARS // 5:
        count += 1
        total += len(LogSample("", bulk_line(count), "worker", "clean", "").message)
    return count


BULK_DEFAULT: int = _bulk_default()
assert BULK_DEFAULT <= LOGS_TAIL_MAX, "o volume padrão precisa caber no `tail` máximo"


def bulk(n: int = BULK_DEFAULT) -> list[str]:
    """`n` linhas limpas, na ordem (as mais antigas primeiro), com `id` de 1 a `n`."""
    return [bulk_line(index) for index in range(1, n + 1)]


def lines(samples: Iterable[LogSample]) -> list[str]:
    """As linhas das amostras, na ordem dada."""
    return [sample.line for sample in samples]


def by_service(samples: Iterable[LogSample]) -> dict[str, list[str]]:
    """O mapa serviço → linhas (só as amostras com serviço), para o `FakeStack(logs=…)`."""
    grouped: dict[str, list[str]] = {}
    for sample in samples:
        if sample.service is not None:
            grouped.setdefault(sample.service, []).append(sample.line)
    return grouped


def select(names: Sequence[str]) -> list[LogSample]:
    """As amostras pelos nomes, na ordem dada."""
    return [ALL_SAMPLES[name] for name in names]
