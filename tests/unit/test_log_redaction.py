"""Redação dos registros da pilha para a ferramenta `stack_logs`.


`cq_client.log_redaction` recebe as linhas que o `local_commands.run_logs` do CLI devolve (com o prefixo do Compose) e
devolve só o que passa pela lista de permissão; o resto vira marcador. As amostras são as de
`tests/helpers/log_samples.py` (linhas reais do `JsonFormatter` do servidor, linhas do Postgres, identidades canário e
segredos-sentinela), cada uma com o desfecho que a regra da redação lhe dá (`LogSample.outcome`).

- `redact_line`: o desfecho (`clean`, `redacted`, `omitted`) e o texto exatos de cada amostra (a linha sem o prefixo
  do Compose; `LOG_FIELD_MARKER` no campo trocado, chaves na ordem original; `LOG_LINE_MARKER` na linha inteira; no
  Postgres sem modelo, o prefixo e a severidade mantidos).
- `redact`: a ordem, as descartadas pela regra 1 (sem item em `lines`, somadas em `omitted`) e as contagens.
- `fit`: corta as mais antigas até o texto do resultado caber em `MAX_RESULT_CHARS`, com `truncated`.
- `logs_body`: o corpo `stack_logs.output`, sem o `outcome`.
- Nenhuma identidade canário nem segredo-sentinela na saída, com o controle positivo na entrada.
- O teste de deriva, por AST sobre `server/src/cq_server/**` (lido como texto, nunca importado para isto).

No esqueleto, `redact_line`, `redact`, `fit` e `logs_body` lançam `NotImplementedError`; as tabelas são reais. O teste
de deriva compara duas coisas que o esqueleto já tem e fica na mesma função de um `redact_line` sobre a amostra de
cada evento inventariado.
"""

from __future__ import annotations

import ast
import json
import pathlib

import jsonschema
import pytest

from cq_client.log_redaction import (
    LOG_EVENTS,
    LOG_EVENTS_OMITTED,
    LOG_FIELD_MARKER,
    LOG_LINE_MARKER,
    POSTGRES_PREFIX,
    LogsResult,
    RedactedLine,
)
from cq_client.mcp_server import MAX_RESULT_CHARS
from cq_client.stack import LOG_SERVICES

from tests.helpers.canary import find_canary, find_secrets
from tests.helpers.log_samples import (
    ALL_SAMPLES,
    BULK_DEFAULT,
    CANARY_PREFIXES,
    DROPPED,
    DROPPED_LINES,
    EVENT_LINES,
    SENTINEL_MASTER_KEY,
    SENTINELS,
    LogSample,
    bulk,
    compose,
    event_message,
    lines,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVER_SRC = REPO / "server" / "src" / "cq_server"
OUTPUT_SCHEMA = REPO / "contracts" / "mcp" / "v1" / "schemas" / "stack_logs.output.schema.json"

# Os campos trocados por `LOG_FIELD_MARKER` em cada amostra JSON `"redacted"` (regra 2 da redação).
REDACTED_FIELDS: dict[str, frozenset[str]] = {
    "event:outbox.skipped": frozenset({"reason"}),
    "event:config.invalid": frozenset({"detail"}),
    "field-outside-list": frozenset({"worker_id"}),
    "scheduler-swept-enqueued": frozenset({"enqueued"}),
    "wrong-type-int-as-string": frozenset({"id"}),
    "wrong-type-int-as-bool": frozenset({"id"}),
    "wrong-type-token-with-digits": frozenset({"state"}),
    "wrong-type-uuid-uppercase": frozenset({"offering_id"}),
    "wrong-type-route-with-digits": frozenset({"route"}),
    "wrong-enum": frozenset({"method"}),
    "always-masked-outbox-skipped": frozenset({"reason"}),
    "always-masked-config-invalid": frozenset({"detail"}),
}

# Os métodos de registro do `logging` que o teste de deriva inspeciona (o `extra` de outras chamadas, como o
# `ConfigDict(extra="forbid")` do pydantic, não é registro).
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})

# Chamadas de registro com `extra` não literal ou com `event` não literal, declaradas à mão (local
# "<arquivo relativo>:<linha>" → justificativa). Vazio: nenhuma chamada declarada; uma entrada nova exige justificativa.
DECLARED_NON_LITERAL: dict[str, str] = {}

REDACTABLE = [s for s in ALL_SAMPLES.values() if s.outcome != DROPPED]


def _is_postgres(sample: LogSample) -> bool:
    return sample.service == "postgres"


def _text_of(result: LogsResult) -> str:
    """O texto do resultado da ferramenta: o JSON do corpo, compacto."""
    from cq_client.log_redaction import logs_body

    return json.dumps(logs_body(result), ensure_ascii=False, separators=(",", ":"))


def _assert_text(sample: LogSample, line: RedactedLine) -> None:
    """O texto exato de `line` para a amostra (ver o docstring do módulo)."""
    message = sample.message
    if sample.outcome == "omitted":
        assert line.text == LOG_LINE_MARKER, (sample.name, line.text)
        return
    if _is_postgres(sample):
        if sample.outcome == "clean":
            assert line.text == message, (sample.name, line.text)
        else:
            match = POSTGRES_PREFIX.match(message)
            assert match is not None, sample.name
            assert line.text == message[:match.start("message")] + LOG_FIELD_MARKER, (sample.name, line.text)
        return
    original = json.loads(message)
    parsed = json.loads(line.text)
    assert isinstance(parsed, dict), (sample.name, line.text)
    assert list(parsed) == list(original), f"{sample.name}: as chaves mudaram de ordem ou de conjunto"
    if sample.outcome == "clean":
        assert parsed == original, (sample.name, line.text)
        return
    assert sample.name in REDACTED_FIELDS, f"amostra redigida sem os campos esperados na tabela: {sample.name}"
    masked = REDACTED_FIELDS[sample.name]
    expected = {key: (LOG_FIELD_MARKER if key in masked else value) for key, value in original.items()}
    assert parsed == expected, (sample.name, line.text)


# --------------------------------------------------------------------------------------------------
# redact_line: cada regra da redação


@pytest.mark.parametrize("sample", REDACTABLE, ids=[s.name for s in REDACTABLE])
def test_redact_line_outcome_and_text(sample: LogSample) -> None:
    """Regras 2 a 4 da redação: cada amostra (JSON de cada evento de `LOG_EVENTS`, campo fora da lista,
    tipo errado, campo sempre mascarado, evento desconhecido, JSON inválido, base inválida, mensagem de biblioteca;
    Postgres com modelo, sem modelo, `DETAIL`, `STATEMENT`, sem prefixo; segunda camada; fora do alcance dela) sai
    com o serviço, o desfecho e o texto exatos, sem identidade canário nem segredo-sentinela no texto."""
    from cq_client.log_redaction import redact_line

    line = redact_line(sample.line, LOG_SERVICES)
    assert isinstance(line, RedactedLine)
    assert line.service == sample.service
    assert line.outcome == sample.outcome, (sample.name, sample.rule, line.outcome, line.text)
    _assert_text(sample, line)
    assert find_canary(line.text, prefixes=CANARY_PREFIXES) == [], sample.name
    assert find_secrets(line.text, list(SENTINELS)) == [], sample.name


def test_redact_line_second_layer_outranks_redaction() -> None:
    """Regra 4 da redação: um acerto da segunda camada (a chave-mestra sentinela no `logger`) numa linha que
    também teria campo mascarado (`outbox.skipped`, `reason`) troca a linha inteira pelo marcador: omitida, e não
    redigida; nas contagens do `redact`, não conta em `redacted`."""
    from cq_client.log_redaction import redact, redact_line

    raw = compose("worker", event_message("outbox.skipped", logger=SENTINEL_MASTER_KEY))
    assert SENTINEL_MASTER_KEY in raw
    line = redact_line(raw, LOG_SERVICES)
    assert (line.service, line.text, line.outcome) == ("worker", LOG_LINE_MARKER, "omitted")
    result = redact([raw], LOG_SERVICES)
    assert (result.returned, result.redacted, result.omitted) == (1, 0, 1)


# --------------------------------------------------------------------------------------------------
# redact: ordem, descartadas e contagens


def _expected_counts(samples: list[LogSample]) -> tuple[int, int, int]:
    kept = [s for s in samples if s.outcome != DROPPED]
    redacted = sum(1 for s in kept if s.outcome == "redacted")
    omitted = sum(1 for s in kept if s.outcome == "omitted") + sum(1 for s in samples if s.outcome == DROPPED)
    return len(kept), redacted, omitted


def _assert_invariants(result: LogsResult) -> None:
    markers = sum(1 for line in result.lines if line.text == LOG_LINE_MARKER)
    assert result.returned == len(result.lines)
    assert result.redacted + markers <= result.returned
    assert markers <= result.omitted
    assert result.redacted == sum(1 for line in result.lines if line.outcome == "redacted")


def test_redact_all_samples_counts_and_order() -> None:
    """todas as amostras, na ordem da saída do Compose (inclusive as sem o prefixo do
    Compose e a de serviço fora de `PREFIX_PATTERN`): as linhas na mesma ordem, sem as descartadas; `returned ==
    len(lines)`; `redacted` = as redigidas; `omitted` = as trocadas pelo marcador mais as descartadas; `redacted` +
    marcadores ≤ `returned`; marcadores ≤ `omitted`; `truncated` falso."""
    from cq_client.log_redaction import redact

    samples = list(ALL_SAMPLES.values())
    assert any(s.outcome == DROPPED for s in samples)
    result = redact(lines(samples), LOG_SERVICES)
    kept = [s for s in samples if s.outcome != DROPPED]
    assert [(line.service, line.outcome) for line in result.lines] == [(s.service, s.outcome) for s in kept]
    for sample, line in zip(kept, result.lines):
        _assert_text(sample, line)
    assert (result.returned, result.redacted, result.omitted) == _expected_counts(samples)
    assert result.truncated is False
    _assert_invariants(result)


@pytest.mark.parametrize("requested", [("worker",), ("api", "postgres"), ("postgres",)],
                         ids=["worker", "api_postgres", "postgres"])
def test_redact_drops_unrequested_services(requested: tuple[str, ...]) -> None:
    """Regra 1 da redação: a linha de serviço não pedido é descartada (sem item em `lines`) e somada em
    `omitted`, como a linha sem o prefixo do Compose."""
    from cq_client.log_redaction import redact

    samples = list(ALL_SAMPLES.values())
    result = redact(lines(samples), requested)
    as_requested = [s if s.service in requested else LogSample(s.name, s.line, None, DROPPED, "1: não pedido")
                    for s in samples]
    assert {line.service for line in result.lines} <= set(requested)
    kept = [s for s in as_requested if s.outcome != DROPPED]
    assert [(line.service, line.outcome) for line in result.lines] == [(s.service, s.outcome) for s in kept]
    assert (result.returned, result.redacted, result.omitted) == _expected_counts(as_requested)
    _assert_invariants(result)


def test_redact_only_dropped_and_empty() -> None:
    """só linhas descartadas → `lines` vazio e todas em `omitted`; nenhuma linha → `lines`
    vazio e contagens 0."""
    from cq_client.log_redaction import redact

    dropped = redact(lines(DROPPED_LINES.values()), LOG_SERVICES)
    assert dropped.lines == []
    assert (dropped.returned, dropped.redacted, dropped.omitted, dropped.truncated) == (
        0, 0, len(DROPPED_LINES), False)
    empty = redact([], LOG_SERVICES)
    assert (empty.lines, empty.returned, empty.redacted, empty.omitted, empty.truncated) == ([], 0, 0, 0, False)


# --------------------------------------------------------------------------------------------------
# fit e logs_body


def test_fit_cuts_oldest_until_text_fits() -> None:
    """com linhas limpas que passam de `MAX_RESULT_CHARS` no texto, `fit` corta as
    mais antigas (do início) até o texto do resultado caber, marca `truncated` e mantém `returned == len(lines)`;
    corta só o necessário (com mais uma linha antiga, não caberia); as linhas que ficam são as originais, inteiras
    (nunca o JSON cortado no meio)."""
    from cq_client.log_redaction import fit, redact

    full = redact(bulk(), LOG_SERVICES)
    assert full.returned == BULK_DEFAULT
    assert len(_text_of(full)) > MAX_RESULT_CHARS, "controle: o volume passa do teto"
    fitted = fit(full, MAX_RESULT_CHARS)
    kept = len(fitted.lines)
    assert 0 < kept < len(full.lines)
    assert fitted.lines == full.lines[-kept:], "as linhas mantidas não são as mais recentes, inteiras"
    assert fitted.truncated is True
    assert fitted.returned == kept
    assert len(_text_of(fitted)) <= MAX_RESULT_CHARS
    one_more = LogsResult(lines=full.lines[-(kept + 1):], returned=kept + 1, redacted=fitted.redacted,
                          omitted=fitted.omitted, truncated=True)
    assert len(_text_of(one_more)) > MAX_RESULT_CHARS, "fit cortou mais linhas que o necessário"
    for line in fitted.lines:
        assert json.loads(line.text)["event"] == "task.processed"
    assert json.loads(fitted.lines[-1].text)["id"] == BULK_DEFAULT


def test_fit_keeps_result_that_fits() -> None:
    """um resultado que já cabe sai igual, com `truncated` falso."""
    from cq_client.log_redaction import fit, redact

    small = redact(bulk(5), LOG_SERVICES)
    fitted = fit(small, MAX_RESULT_CHARS)
    assert fitted.lines == small.lines
    assert (fitted.returned, fitted.redacted, fitted.omitted, fitted.truncated) == (
        small.returned, small.redacted, small.omitted, False)


def test_logs_body_shape_and_schema() -> None:
    """`logs_body` dá o corpo `stack_logs.output`: as linhas só com `service` e `text` (sem o
    `outcome`), na ordem, e as contagens; válido contra o esquema congelado; sem identidade nem segredo."""
    from cq_client.log_redaction import logs_body, redact

    result = redact(lines(ALL_SAMPLES.values()), LOG_SERVICES)
    body = logs_body(result)
    assert body == {"lines": [{"service": line.service, "text": line.text} for line in result.lines],
                    "returned": result.returned, "redacted": result.redacted, "omitted": result.omitted,
                    "truncated": result.truncated}
    schema = json.loads(OUTPUT_SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(body)
    empty = logs_body(LogsResult(lines=[], returned=0, redacted=0, omitted=0, truncated=False))
    assert empty == {"lines": [], "returned": 0, "redacted": 0, "omitted": 0, "truncated": False}
    jsonschema.Draft202012Validator(schema).validate(empty)


def test_no_canary_nor_sentinel_in_output() -> None:
    """nenhuma identidade canário nem segredo-sentinela no texto do resultado (o corpo
    inteiro), com o controle positivo na entrada (cada sentinela e as identidades estão nas linhas cruas)."""
    from cq_client.log_redaction import redact

    raw = lines(ALL_SAMPLES.values())
    joined = "\n".join(raw)
    assert find_canary(joined, prefixes=CANARY_PREFIXES), "controle: as amostras têm identidades canário"
    for secret in SENTINELS:
        assert find_secrets(joined, [secret]), f"controle: o sentinela {secret[:6]}… não está nas amostras"
    text = _text_of(redact(raw, LOG_SERVICES))
    assert find_canary(text, prefixes=CANARY_PREFIXES) == []
    assert find_secrets(text, list(SENTINELS)) == []


# --------------------------------------------------------------------------------------------------
# Deriva: os eventos do servidor contra a lista


def _logger_calls() -> list[tuple[str, ast.Call]]:
    calls: list[tuple[str, ast.Call]] = []
    for path in sorted(SERVER_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(SERVER_SRC).as_posix()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in LOG_METHODS):
                calls.append((f"{where}:{node.lineno}", node))
    return calls


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_exception_call(call: ast.Call) -> bool:
    exc_info = _keyword(call, "exc_info")
    falsy = isinstance(exc_info, ast.Constant) and not exc_info.value
    return call.func.attr == "exception" or (exc_info is not None and not falsy)  # type: ignore[union-attr]


def _inventory() -> tuple[dict[str, list[str]], dict[str, set[str]], list[str]]:
    """Os eventos literais (evento → locais), as chaves de `extra` das chamadas de exceção (local → chaves) e os
    problemas (`event` ou `extra` não literal fora de `DECLARED_NON_LITERAL`)."""
    events: dict[str, list[str]] = {}
    exception_keys: dict[str, set[str]] = {}
    problems: list[str] = []
    for where, call in _logger_calls():
        extra = _keyword(call, "extra")
        if extra is None:
            continue
        if not isinstance(extra, ast.Dict) or any(not isinstance(k, ast.Constant) for k in extra.keys):
            if where not in DECLARED_NON_LITERAL:
                problems.append(f"{where}: extra não literal")
            continue
        keys = {k.value for k in extra.keys if isinstance(k, ast.Constant)}
        if _is_exception_call(call):
            exception_keys[where] = keys - {"event"}
            continue
        for key, value in zip(extra.keys, extra.values):
            if isinstance(key, ast.Constant) and key.value == "event":
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    events.setdefault(value.value, []).append(where)
                elif where not in DECLARED_NON_LITERAL:
                    problems.append(f"{where}: event não literal")
    return events, exception_keys, problems


def test_drift_server_events_are_listed() -> None:
    """Invariante de deriva da redação: por AST sobre `server/src/cq_server/**`, todo `extra={"event":
    <literal>}` está em `LOG_EVENTS ∪ LOG_EVENTS_OMITTED`; um `event` (ou um `extra`) não literal é falha, salvo
    declarado em `DECLARED_NON_LITERAL`; as chaves de `extra` das chamadas `logger.exception(…)` ou com `exc_info`
    (que saem com `event = "exception"`) estão na lista do evento `exception`. Na mesma função: a amostra de
    cada evento inventariado passa pelo `redact_line` com o desfecho esperado."""
    from cq_client.log_redaction import redact_line

    events, exception_keys, problems = _inventory()
    assert problems == [], problems
    assert "request" in events and "task.processed" in events, "controle: o inventário achou os eventos"
    assert exception_keys, "controle: o inventário achou chamadas de exceção com extra"
    known = set(LOG_EVENTS) | set(LOG_EVENTS_OMITTED)
    unknown = {event: where for event, where in events.items() if event not in known}
    assert unknown == {}, f"eventos do servidor fora de LOG_EVENTS ∪ LOG_EVENTS_OMITTED: {unknown}"
    allowed = set(LOG_EVENTS["exception"])
    outside = {where: sorted(keys - allowed) for where, keys in exception_keys.items() if keys - allowed}
    assert outside == {}, f"chaves de extra de exceção fora da lista do evento exception: {outside}"
    for event in sorted(set(events) | {"exception"}):
        if event in LOG_EVENTS_OMITTED:
            continue
        sample = EVENT_LINES[event]
        line = redact_line(sample.line, LOG_SERVICES)
        assert (line.service, line.outcome) == (sample.service, sample.outcome), (event, line.text)
        _assert_text(sample, line)

