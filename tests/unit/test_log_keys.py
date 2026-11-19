"""Deriva dos nomes de campo dos registros do servidor contra `SERVER_LOG_KEYS` (regra 2 da redação dos
registros).

Na regra 2 da redação do `stack_logs`, uma chave JSON fora da lista do evento só fica no texto (com o valor trocado
por `LOG_FIELD_MARKER`) se o nome estiver em `cq_client.log_redaction.SERVER_LOG_KEYS`; qualquer outro nome troca a
linha inteira por `LOG_LINE_MARKER`. O conjunto precisa ser **exatamente** o dos nomes que o servidor emite:
- as chaves literais de todo `extra=` das chamadas de registro de `server/src/cq_server/**` (os métodos de
  `LOG_METHODS`; o `ConfigDict(extra="forbid")` do Pydantic não é registro);
- mais as chaves literais que o `JsonFormatter` (`server/src/cq_server/logs.py`) põe no `payload`, fora da base
  (`JSON_BASE_KEYS`, que a regra 2 trata à parte): hoje `exc_type` e `where`.

Os arquivos do servidor são lidos como texto e analisados por AST, nunca importados para isto (o molde é o
`test_drift_server_events_are_listed`, em `tests/unit/test_log_redaction.py`). Um `extra=` de registro que não seja um
dicionário literal, ou com `**` ou chave calculada, é falha: a chave dele não entraria no inventário.

Na mesma função, o comportamento que o conjunto decide: cada nome do conjunto, fora da lista do evento, fica no
texto com o valor mascarado; a chave neutra `ana`, fora do conjunto, omite a linha.
"""

from __future__ import annotations

import ast
import json
import pathlib

from cq_client.log_redaction import JSON_BASE_KEYS, LOG_EVENTS, LOG_FIELD_MARKER, LOG_LINE_MARKER
from cq_client.stack import LOG_SERVICES

from tests.helpers.log_samples import compose

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVER_SRC = REPO / "server" / "src" / "cq_server"
FORMATTER_FILE = SERVER_SRC / "logs.py"

# Os métodos de registro do `logging` inspecionados (os mesmos do teste de deriva dos eventos).
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"})

# Um evento sem campos permitidos: toda chave fora da base fica fora da lista dele.
EMPTY_EVENT = "worker.stopped"
# Uma chave neutra: fora de `SERVER_LOG_KEYS`, sem acerto da segunda camada.
UNKNOWN_KEY = "ana"


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


def _extra_keys() -> tuple[dict[str, list[str]], int, list[str]]:
    """As chaves literais dos `extra=` de registro (chave → locais), o número de chamadas com `extra=` e os problemas
    (`extra` que não é dicionário literal, `**` ou chave calculada)."""
    keys: dict[str, list[str]] = {}
    calls = 0
    problems: list[str] = []
    for where, call in _logger_calls():
        # Um `**kwargs` na chamada pode levar um `extra` que o
        # inventário não vê.
        if any(kw.arg is None for kw in call.keywords):
            problems.append(f"{where}: ** nos argumentos da chamada de registro")
        extra = next((kw.value for kw in call.keywords if kw.arg == "extra"), None)
        if extra is None:
            continue
        calls += 1
        if not isinstance(extra, ast.Dict):
            problems.append(f"{where}: extra não é um dicionário literal")
            continue
        for key in extra.keys:
            if key is None:
                problems.append(f"{where}: ** no extra")
            elif not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                problems.append(f"{where}: chave calculada no extra")
            else:
                keys.setdefault(key.value, []).append(where)
    return keys, calls, problems


def _formatter_keys() -> set[str]:
    """As chaves literais que o `JsonFormatter` atribui ao `payload` (`payload["<chave>"] = …`)."""
    tree = ast.parse(FORMATTER_FILE.read_text(encoding="utf-8"), filename=str(FORMATTER_FILE))
    formatter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "JsonFormatter")
    keys: set[str] = set()
    for node in ast.walk(formatter):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == "payload" and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                keys.add(target.slice.value)
    return keys


def _line(key: str) -> str:
    """Uma linha do worker com a base válida, o evento sem campos permitidos e a chave `key` com valor neutro."""
    message = json.dumps({key: "x", "level": "INFO", "logger": "cq_server.worker", "event": EMPTY_EVENT,
                          "time": "2031-01-01T00:00:00.000Z"}, ensure_ascii=True)
    return compose("worker", message)


def test_drift_server_log_keys_are_exactly_the_server_ones() -> None:
    """Regra 2 da redação dos registros: por AST sobre `server/src/cq_server/**`, todo
    `extra=` de registro é um dicionário com chaves literais, e `SERVER_LOG_KEYS` é exatamente o conjunto dessas
    chaves mais as que o `JsonFormatter` acrescenta fora da base. Na mesma função: cada nome do conjunto, fora
    da lista do evento, sai com o nome e o valor mascarado (`"redacted"`); a chave neutra `ana`, fora do conjunto,
    troca a linha inteira pelo marcador (`"omitted"`), sem o nome dela no texto."""
    from cq_client.log_redaction import SERVER_LOG_KEYS, redact_line

    extra_keys, calls, problems = _extra_keys()
    assert problems == [], problems
    assert calls > 0 and {"event", "worker_id", "enqueued"} <= set(extra_keys), "controle: o inventário achou os extra"
    formatter_keys = _formatter_keys()
    assert set(JSON_BASE_KEYS) <= formatter_keys, "controle: o inventário achou as atribuições do JsonFormatter"
    expected = set(extra_keys) | (formatter_keys - set(JSON_BASE_KEYS))
    assert {"exc_type", "where"} <= expected, "controle: as chaves do JsonFormatter com exc_info"
    missing = {key: extra_keys.get(key, ["JsonFormatter"]) for key in sorted(expected - SERVER_LOG_KEYS)}
    assert missing == {}, f"nomes de campo do servidor fora de SERVER_LOG_KEYS: {missing}"
    extra = sorted(SERVER_LOG_KEYS - expected)
    assert extra == [], f"nomes em SERVER_LOG_KEYS que o servidor não emite: {extra}"

    assert LOG_EVENTS[EMPTY_EVENT] == {}, "controle: o evento não tem campos permitidos"
    for key in sorted(SERVER_LOG_KEYS - set(JSON_BASE_KEYS)):
        line = redact_line(_line(key), LOG_SERVICES)
        assert (line.service, line.outcome) == ("worker", "redacted"), (key, line.outcome, line.text)
        parsed = json.loads(line.text)
        assert parsed[key] == LOG_FIELD_MARKER, (key, line.text)
    assert UNKNOWN_KEY not in SERVER_LOG_KEYS
    raw = _line(UNKNOWN_KEY)
    assert f'"{UNKNOWN_KEY}"' in raw, "controle: a chave neutra está na linha crua"
    line = redact_line(raw, LOG_SERVICES)
    assert (line.service, line.text, line.outcome) == ("worker", LOG_LINE_MARKER, "omitted"), line.text
