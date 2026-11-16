"""Redação dos registros da pilha para a ferramenta `stack_logs`.


Módulo puro, sem Click, sem `mcp`, sem `cq_server` e sem `subprocess`: recebe as linhas que
`local_commands.run_logs` devolve (já limpas pela pilha, com o prefixo do Compose `<serviço>-<n> | `) e devolve
só o que passa pela lista de permissão; o resto vira marcador. A regra por linha (o primeiro que decide):
1. sem `PREFIX_PATTERN` ou de serviço não pedido → descartada (só soma em `omitted`);
2. api/worker: JSON objeto com a base válida e `event` em `LOG_EVENTS`; cada campo fora do tipo, em
   `ALWAYS_MASKED_FIELDS` ou fora da lista do evento com nome em `SERVER_LOG_KEYS` vira `LOG_FIELD_MARKER`, e um
   campo fora da lista do evento com nome fora de `SERVER_LOG_KEYS` troca a linha inteira por `LOG_LINE_MARKER`
   (um nome desconhecido pode ser identidade); senão (não é esse JSON), a linha inteira vira `LOG_LINE_MARKER`;
3. postgres: `POSTGRES_PREFIX` com severidade fora de `POSTGRES_OMITTED_SEVERITIES`; a mensagem que não casa
   `POSTGRES_TEMPLATES` vira `LOG_FIELD_MARKER`;
4. segunda camada (`SECOND_LAYER_PATTERNS`): um acerto troca a linha inteira pelo marcador.

Estrutura: as tabelas, os tipos e as constantes, e depois o comportamento (`redact_line`, `redact`, `fit`
e `logs_body`). Além das regras acima, a implementação fecha por conta própria (**[I]**, sempre
pelo lado mais estrito): chave JSON repetida e chave fora da lista com nome que não é identificador ou que acerta a
segunda camada omitem a linha; no `route`, cada segmento é `{<identificador>}`, a versão (`v1`) ou literal
sem dígito; o `logger` é validado como `IDENT` (identificador pontuado), mais estrito que o padrão mínimo
(por defesa a mais).

`LOG_EVENTS` vem do inventário **[F]** por AST de `server/src/cq_server/**` (as chamadas com `extra={"event":
…}` literal e as chamadas `logger.exception`/`exc_info`, que saem com `event = "exception"`, `exc_type` e
`where`, `server/src/cq_server/logs.py:61-69`), lido como texto em 2026-10-28, nunca importado. Os tipos de
cada campo são **[I]** da implementação, sempre pelo lado fechado: um campo cujo valor não cabe num `FieldKind`
(o dicionário `enqueued` de `scheduler.swept`, a lista `applied` de `migrations.applied`, o `worker_id`
configurável) fica fora da lista e sai como `LOG_FIELD_MARKER`.

`SERVER_LOG_KEYS` são
os nomes de campo que o servidor emite: as chaves literais dos `extra=` das chamadas de registro de
`server/src/cq_server/**` e as que o `JsonFormatter` acrescenta (`exc_type`, `where`), do inventário **[F]** por AST
de 2026-10-29. Só esses nomes ficam no texto com o valor mascarado; o nome de qualquer outra chave fora da lista
(de uma biblioteca de terceiros, ou um nome de pessoa) pode carregar identidade, e a linha inteira sai. O teste de
deriva, nos testes de unidade do cliente, mantém o conjunto igual ao do servidor.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

# Os marcadores da linha inteira e do campo.
LOG_LINE_MARKER = "[linha omitida]"
LOG_FIELD_MARKER = "[omitido]"

# O prefixo do Compose (`<serviço>-<n> | <mensagem>`), já sem cor (a pilha força `--no-color`).
PREFIX_PATTERN = re.compile(r"^(?P<service>api|worker|postgres)-\d+\s*\| (?P<message>.*)$")

LogService = Literal["api", "worker", "postgres"]
LineOutcome = Literal["clean", "redacted", "omitted"]

FieldKindName = Literal["int", "bool", "null", "uuid", "enum", "token", "route", "where", "ident", "iso_time"]


@dataclass(frozen=True)
class FieldKind:
    """O tipo de um campo permitido. `values` só no `enum` (comparação exata); `nullable`
    aceita também o `null` do JSON."""

    kind: FieldKindName
    values: tuple[str, ...] = ()
    nullable: bool = False


# Os padrões de cada tipo (usados pela implementação com `fullmatch`).
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
TOKEN_PATTERN = re.compile(r"^[a-z_]{1,40}$")
# Modelo de rota: sem dígitos fora de `{…}` (conferido à parte na implementação).
ROUTE_PATTERN = re.compile(r"^/[A-Za-z0-9_/{}.-]*$")
WHERE_PATTERN = re.compile(r"^[A-Za-z0-9_/.-]+\.py:\d+$")
IDENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
ISO_TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

INT = FieldKind("int")
BOOL = FieldKind("bool")
NULL = FieldKind("null")
UUID = FieldKind("uuid")
TOKEN = FieldKind("token")
ROUTE = FieldKind("route")
WHERE = FieldKind("where")
IDENT = FieldKind("ident")
ISO_TIME = FieldKind("iso_time")


def enum(*values: str, nullable: bool = False) -> FieldKind:
    """Um `FieldKind` `enum` com os valores exatos (construtor puro)."""
    return FieldKind("enum", tuple(values), nullable)


def nullable(kind: FieldKind) -> FieldKind:
    """O mesmo tipo, aceitando também `null` (construtor puro)."""
    return FieldKind(kind.kind, kind.values, True)


# As chaves da base de toda linha JSON do `JsonFormatter` (`level`, `logger`, `event`, `time`).
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
JSON_BASE_KEYS: Mapping[str, FieldKind] = {
    "level": enum(*LOG_LEVELS),
    "logger": IDENT,
    "event": IDENT,  # e, além do tipo, `event ∈ LOG_EVENTS` (regra 2)
    "time": ISO_TIME,
}

# **[F]** `server/src/cq_server/api/middleware.py:39-40`.
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT", "OTHER")
# **[F]** as fases dos `logger.exception` do worker (`worker.py`) e os comandos do `cq-server` (`__main__.py:102-108`).
_EXCEPTION_PHASES = ("schema_check", "notice_listener", "loop", "poll", "store", "problem", "no_access",
                     "bots_notices", "shutdown", "bots", "grade_pump")
_SERVER_COMMANDS = ("migrate", "serve", "worker")

# evento → campos extras permitidos, com o tipo (inventário **[F]** por AST, 2026-10-28).
LOG_EVENTS: Mapping[str, Mapping[str, FieldKind]] = {
    "request": {"method": enum(*_HTTP_METHODS), "route": nullable(ROUTE), "status": nullable(INT)},
    "task.processed": {"id": INT, "state": TOKEN},
    "outbox.dispatched": {"id": INT, "state": TOKEN},
    "outbox.result_discarded": {"id": INT, "outcome": TOKEN},
    "outbox.skipped": {"id": INT, "reason": TOKEN},  # `reason` sempre mascarado (ALWAYS_MASKED_FIELDS)
    "telegram.update": {"update_row_id": INT, "outcome": TOKEN},
    "scheduler.swept": {"abandoned": INT},  # `enqueued` (dicionário) fica fora: sai mascarado
    "invite.email": {"invite_send_id": INT, "result": TOKEN},
    "invite.email_superseded": {"invite_send_id": INT},
    "delivery.message_failed": {"id": INT, "role": TOKEN, "operation": TOKEN, "reason": TOKEN},
    "delivery.late_send": {"id": INT},
    "bot.lease": {"offering_id": UUID, "change": TOKEN, "reason": TOKEN},
    "bot.token_unreadable": {"offering_id": nullable(UUID), "reason": TOKEN},
    "bot.poll_problem": {"offering_id": UUID, "problem": TOKEN},
    "telegram.callback_answer_failed": {"bot_id": INT, "error": IDENT},
    "worker.started": {},  # `worker_id` é configurável pelo operador: sai mascarado
    "worker.waiting_schema": {"version": nullable(TOKEN), "expected": TOKEN},
    "worker.stop_requested": {"signal": enum("SIGTERM", "SIGINT")},
    "worker.stopped": {},
    "worker.bots_disabled": {},
    "worker.engine_disabled": {},
    "worker.mail_disabled": {},
    "worker.repos_disabled": {},
    "worker.staging_sweep_failed": {"error_type": IDENT},
    "worker.staging_swept": {"removed": INT},
    "migrations.applied": {"schema_version": nullable(TOKEN)},  # `applied` (lista) fica fora: sai mascarado
    "config.invalid": {"detail": TOKEN},  # `detail` sempre mascarado (ALWAYS_MASKED_FIELDS)
    "usage.invalid": {"detail": enum("argumentos inválidos; veja cq-server --help")},
    "interrupted": {"command": nullable(enum(*_SERVER_COMMANDS))},
    "http.unexpected_status": {"status": INT},
    "warning": {"category": IDENT, "location": WHERE},
    "unhandled_exception": {"exception": IDENT},
    "exception": {"exc_type": IDENT, "where": nullable(WHERE), "phase": enum(*_EXCEPTION_PHASES),
                  "command": nullable(enum(*_SERVER_COMMANDS))},
}

# Eventos do servidor omitidos de propósito (a linha inteira vira o marcador). Vazio no inventário de
# 2026-10-28: todo evento literal tem lista de campos.
LOG_EVENTS_OMITTED: frozenset[str] = frozenset()

# Os nomes de campo que o servidor emite. Uma chave fora da lista do evento
# só fica no texto, com o valor mascarado, se o nome estiver aqui; senão, a linha inteira sai (regra 2). Inventário
# **[F]** por AST de `server/src/cq_server/**`, lido como texto em 2026-10-29: 55 chamadas de registro com `extra=`,
# todas com chaves literais (o `ConfigDict(extra="forbid")` de `api/schemas.py:106` não é registro).
SERVER_LOG_KEYS: frozenset[str] = frozenset({
    # as 34 chaves literais dos `extra=` de registro (inclui `event`, que a regra 2 trata como base)
    "abandoned", "applied", "bot_id", "category", "change", "command", "detail", "enqueued", "error", "error_type",
    "event", "exception", "expected", "id", "invite_send_id", "location", "method", "offering_id", "operation",
    "outcome", "phase", "problem", "reason", "removed", "result", "role", "route", "schema_version", "signal",
    "state", "status", "update_row_id", "version", "worker_id",
    # as que o `JsonFormatter` acrescenta com `exc_info` (`server/src/cq_server/logs.py:63-66`)
    "exc_type", "where",
})

# Campos que saem sempre como `LOG_FIELD_MARKER`, mesmo dentro do tipo (texto de terceiros).
ALWAYS_MASKED_FIELDS: tuple[tuple[str, str], ...] = (("outbox.skipped", "reason"), ("config.invalid", "detail"))

# A linha do Postgres (`<data> <hora> <fuso> [<pid>] <SEVERIDADE>:  <mensagem>`).
POSTGRES_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)? [A-Z]{2,5} \[\d+\] (?P<severity>[A-Z]+):\s+(?P<message>.*)$")

# Modelos fechados da mensagem do Postgres, sem valores livres; ancorados.
POSTGRES_TEMPLATES: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"^database system is ready to accept connections$",
    r"^database system was shut down at \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)? [A-Z]{2,5}$",
    r"^checkpoint starting: [a-z ]+$",
    r"^checkpoint complete: [0-9a-z ,.=%()/;:-]+$",
    r"^received fast shutdown request$",
    r"^received smart shutdown request$",
    r"^aborting any active transactions$",
    r"^shutting down$",
    r"^database system is shut down$",
    r"^background worker \"logical replication launcher\" \(PID \d+\) exited with exit code \d+$",
    r"^listening on IPv4 address \"0\.0\.0\.0\", port 5432$",
    r"^listening on IPv6 address \"::\", port 5432$",
    r"^listening on Unix socket \"/var/run/postgresql/\.s\.PGSQL\.5432\"$",
    r"^starting PostgreSQL [0-9.]+ on [a-z0-9_-]+-[a-z0-9_-]+-linux-(gnu|musl), compiled by [A-Za-z0-9 ().,_-]+, "
    r"\d+-bit$",
))

POSTGRES_OMITTED_SEVERITIES = frozenset({"DETAIL", "STATEMENT", "CONTEXT", "HINT", "QUERY"})

# Regra 4: a segunda camada (um acerto troca a linha inteira pelo marcador).
SECOND_LAYER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # e-mail
    re.compile(r"cq_[A-Za-z0-9_-]{20,}"),  # token da API
    re.compile(r"(?<![0-9A-Za-z_-])[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}(?![0-9A-Za-z_-])"),  # token de bot
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),  # chave base64url de 43 caracteres
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),  # credencial em URL
    re.compile(r"\d{5,}"),  # 5 ou mais dígitos seguidos (fora de campo `int` tipado)
)


# --------------------------------------------------------------------------------------------------
# Tipos
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactedLine:
    """Uma linha devolvida: o serviço, o texto (redigido ou o marcador) e o resultado da redação."""

    service: LogService
    text: str
    outcome: LineOutcome


@dataclass(frozen=True)
class LogsResult:
    """O resultado da redação: `returned == len(lines)`; `omitted` soma as linhas trocadas
    pelo marcador e as descartadas pela regra 1."""

    lines: list[RedactedLine]
    returned: int
    redacted: int
    omitted: int
    truncated: bool


# --------------------------------------------------------------------------------------------------
# Comportamento
# --------------------------------------------------------------------------------------------------


class _DuplicateKey(ValueError):
    """Chave repetida num objeto JSON: a reserialização perderia uma das ocorrências (falha fechada)."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise _DuplicateKey(key)
        obj[key] = value
    return obj


# Os segmentos aceitos num modelo de rota (**[I]**, lado fechado): o parâmetro `{<identificador>}`, a versão da API
# (`v1`; **[F]** todo caminho do catálogo começa por `/v1`, salvo `/health`) e o literal sem dígito. A regra geral
# diz "sem dígitos fora de `{…}`"; a versão é a única exceção, e a amostra de teste `/v1/offerings/{offering_id}`
# sai limpa (`tests/helpers/log_samples.py`).
_ROUTE_SEGMENT = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|v\d+|[A-Za-z_.-]*")


def _route_ok(value: str) -> bool:
    """Modelo de rota: o padrão do tipo e cada segmento entre `/` num dos aceitos."""
    if ROUTE_PATTERN.fullmatch(value) is None:
        return False
    return all(_ROUTE_SEGMENT.fullmatch(segment) for segment in value.split("/")[1:])


_STRING_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "uuid": UUID_PATTERN, "token": TOKEN_PATTERN, "where": WHERE_PATTERN, "ident": IDENT_PATTERN,
    "iso_time": ISO_TIME_PATTERN,
}

# Regra 4: os tipos cujo valor ainda passa pela segunda camada.
_SECOND_LAYER_KINDS = frozenset({"ident", "token", "enum"})


def _fits(value: object, kind: FieldKind) -> bool:
    """O valor cabe no tipo (comparação exata, `fullmatch`; o booleano nunca vale como `int`)."""
    if value is None:
        return kind.nullable or kind.kind == "null"
    if kind.kind == "int":
        return type(value) is int
    if kind.kind == "bool":
        return type(value) is bool
    if kind.kind == "null" or not isinstance(value, str):
        return False
    if kind.kind == "enum":
        return value in kind.values
    if kind.kind == "route":
        return _route_ok(value)
    return _STRING_PATTERNS[kind.kind].fullmatch(value) is not None


def _second_layer_hit(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECOND_LAYER_PATTERNS)


def _omitted(service: LogService) -> RedactedLine:
    return RedactedLine(service, LOG_LINE_MARKER, "omitted")


def _redact_json(service: LogService, message: str) -> RedactedLine:
    """Regras 2 e 4 para uma linha da api ou do worker."""
    try:
        obj = json.loads(message, object_pairs_hook=_unique_object)
    except ValueError:  # JSON inválido (inclui `json.JSONDecodeError`) ou chave repetida
        return _omitted(service)
    if not isinstance(obj, dict):
        return _omitted(service)
    for key, kind in JSON_BASE_KEYS.items():
        if key not in obj or not _fits(obj[key], kind):
            return _omitted(service)
    event = obj["event"]
    if event not in LOG_EVENTS or event in LOG_EVENTS_OMITTED:
        return _omitted(service)
    # Regra 4 nos valores livres da base (o `level` é enum fechado e o `time` fica fora do alcance).
    if _second_layer_hit(obj["logger"]) or _second_layer_hit(event):
        return _omitted(service)
    allowed = LOG_EVENTS[event]
    masked_here = {field for masked_event, field in ALWAYS_MASKED_FIELDS if masked_event == event}
    out: dict[str, object] = {}
    changed = False
    for key, value in obj.items():
        if key in JSON_BASE_KEYS:
            out[key] = value
            continue
        kind = allowed.get(key)
        if kind is None:
            # Chave fora da lista do evento: o nome fica no texto, com o valor mascarado, só se for um dos nomes que o
            # servidor emite (`SERVER_LOG_KEYS`); um
            # nome desconhecido pode ser identidade (o nome de uma pessoa como chave) e omite a linha. Ficam também,
            # como defesa a mais, a exigência de identificador e a segunda camada no nome (**[I]**, falha fechada
            # além da regra geral).
            if key not in SERVER_LOG_KEYS or IDENT_PATTERN.fullmatch(key) is None or _second_layer_hit(key):
                return _omitted(service)
        if kind is None or key in masked_here or not _fits(value, kind):
            out[key] = LOG_FIELD_MARKER
            changed = True
            continue
        if kind.kind in _SECOND_LAYER_KINDS and isinstance(value, str) and _second_layer_hit(value):
            return _omitted(service)
        out[key] = value
    text = json.dumps(out, ensure_ascii=True)
    return RedactedLine(service, text, "redacted" if changed else "clean")


def _redact_postgres(message: str) -> RedactedLine:
    """Regras 3 e 4 para uma linha do Postgres."""
    match = POSTGRES_PREFIX.fullmatch(message)
    if match is None or match.group("severity") in POSTGRES_OMITTED_SEVERITIES:
        return _omitted("postgres")
    body = match.group("message")
    if not any(template.fullmatch(body) for template in POSTGRES_TEMPLATES):
        # O prefixo (data, fuso, `[pid]`, severidade) fica para o diagnóstico; a mensagem sai.
        return RedactedLine("postgres", message[:match.start("message")] + LOG_FIELD_MARKER, "redacted")
    if _second_layer_hit(body):  # o prefixo fica fora do alcance
        return _omitted("postgres")
    return RedactedLine("postgres", message, "clean")


def _split(raw: str, services: Sequence[str]) -> tuple[LogService, str] | None:
    """Regra 1: o serviço e a mensagem, ou `None` (sem `PREFIX_PATTERN` ou serviço não pedido)."""
    match = PREFIX_PATTERN.fullmatch(raw)
    if match is None or match.group("service") not in services:
        return None
    service: LogService = match.group("service")  # type: ignore[assignment]  # o padrão só casa os três
    return service, match.group("message")


def redact_line(raw: str, services: Sequence[str]) -> RedactedLine:
    """Redige uma linha com prefixo do Compose de um dos `services` (regras 2 a 4).

    A linha que a regra 1 descartaria (sem o prefixo ou de serviço não pedido) não tem `RedactedLine`:
    `ValueError`, sem o conteúdo da linha na mensagem. Quem lê a saída inteira usa `redact`."""
    split = _split(raw, services)
    if split is None:
        raise ValueError("linha sem o prefixo do Compose ou de serviço não pedido")
    service, message = split
    if service == "postgres":
        return _redact_postgres(message)
    return _redact_json(service, message)


def redact(raw_lines: Iterable[str], services: Sequence[str]) -> LogsResult:
    """Redige as linhas na ordem da saída do Compose, com as contagens (regras 1 a 4)."""
    kept: list[RedactedLine] = []
    dropped = 0
    for raw in raw_lines:
        split = _split(raw, services)
        if split is None:
            dropped += 1
            continue
        service, message = split
        kept.append(_redact_postgres(message) if service == "postgres" else _redact_json(service, message))
    return _result(kept, dropped, truncated=False)


def _result(lines: list[RedactedLine], dropped: int, *, truncated: bool) -> LogsResult:
    redacted = sum(1 for line in lines if line.outcome == "redacted")
    omitted = sum(1 for line in lines if line.outcome == "omitted") + dropped
    return LogsResult(lines=lines, returned=len(lines), redacted=redacted, omitted=omitted, truncated=truncated)


def _compact(value: object) -> str:
    """O texto do resultado: o JSON compacto, sem escapar o que não é ASCII."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fit(result: LogsResult, max_chars: int) -> LogsResult:
    """Corta as linhas mais antigas até o texto do resultado caber em `max_chars` (`truncated`).

    O resultado que já cabe sai igual. Cortado, as contagens passam a valer para as linhas que ficaram (**[I]**:
    mantém `redacted` = as redigidas de `lines` e `omitted` = as com o marcador mais as descartadas da regra 1;
    como na saída do `stack_logs`); nenhuma linha sai pela metade. Se nem uma linha couber, `lines` fica vazio."""
    if len(_compact(logs_body(result))) <= max_chars:
        return result
    markers = sum(1 for line in result.lines if line.outcome == "omitted")
    dropped = max(result.omitted - markers, 0)
    items = [len(_compact({"service": line.service, "text": line.text})) for line in result.lines]
    total = len(items)
    # O tamanho com as `k` mais recentes: o corpo sem linhas (com as contagens delas), mais os itens e as `k - 1`
    # vírgulas entre eles.
    suffix = redacted = markers_kept = keep = 0
    for k in range(1, total + 1):
        line = result.lines[total - k]
        suffix += items[total - k]
        redacted += line.outcome == "redacted"
        markers_kept += line.outcome == "omitted"
        counts = LogsResult(lines=[], returned=k, redacted=redacted, omitted=markers_kept + dropped, truncated=True)
        if len(_compact(logs_body(counts))) + suffix + (k - 1) > max_chars:
            break
        keep = k
    return _result(list(result.lines[total - keep:]) if keep else [], dropped, truncated=True)


def logs_body(result: LogsResult) -> dict:
    """O corpo `stack_logs.output`, sem o `outcome` das linhas."""
    return {"lines": [{"service": line.service, "text": line.text} for line in result.lines],
            "returned": result.returned, "redacted": result.redacted, "omitted": result.omitted,
            "truncated": result.truncated}
