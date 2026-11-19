"""Helpers de contrato: esquemas congelados, envelope de erro, sem eco e forma achatada.

Fonte: os contratos da API.

Oferecimentos e bots: os 7 códigos novos em
`MESSAGES` e `STATUS_OF`; em `assert_error`, `fields` vale por código (`FIELDS_CODES`:
obrigatório em `invalid_request`, `conflict`, `already_exists` e `bot_conflict`, proibido nos
demais), e `state` é obrigatório em `invalid_state` e proibido nos demais.

Vínculo do aluno: os 2 códigos novos,
`list_conflict` e `role_conflict` (409), em `MESSAGES` e `STATUS_OF`, com `fields` obrigatório
(`FIELDS_CODES`).

Disparo e resposta:
os 3 códigos novos, `invalid_dispatch_state`, `window_expired` e
`grading_pending` (409), em `MESSAGES` e `STATUS_OF`, **sem** `fields`, `state` nem `permission`
(`FIELDS_CODES` e `OFFERING_STATES` sem mudança); `_marks` reconhece a marca nova `x-untrusted`
(conteúdo de terceiros, não confiável, sempre junto de `x-free-text`), que entra na forma
achatada como `x-untrusted`.

Relatórios: nenhum código de
erro nem marca nova (`MESSAGES`, `STATUS_OF`, `FIELDS_CODES` e `_marks` sem mudança). Fica aqui a validação
do 200 de `reports.get` pela mídia (`assert_report_response`): `application/json` passa por `assert_matches`
e devolve o objeto; `text/csv; charset=utf-8` é conferido pela forma esperada do relatório (UTF-8 com BOM, RFC
4180, `\\r\\n`, as 6 colunas na ordem, toda linha com 6 células, `on_time`/`with_late` numéricos ou vazios, as
células de texto neutralizadas e sem controles C0/C1) e devolve o texto inteiro, **com** o `\\ufeff` inicial,
para a comparação byte a byte e a releitura pelo teste.

CLI e expurgo: os 2 códigos novos,
`purge_pending` e `purge_changed` (409), em `MESSAGES` e `STATUS_OF`, sem `fields`, `state` nem `permission`
(`FIELDS_CODES` e `OFFERING_STATES` sem mudança); em `assert_error`, o argumento novo `pending` (o objeto
`{outbox, updates, tasks, freeze, due}` exato), obrigatório no `purge_pending` (`PENDING_CODES`) e proibido nos
demais; `assert_cli_json(command, payload)` valida a saída `--json` de um comando novo do CLI contra
`contracts/cli/v1/schemas/<comando>.output.schema.json`. `_marks` sem mudança (nenhuma marca nova).
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "contracts" / "api" / "v1"
SCHEMAS = CONTRACTS / "schemas"

MESSAGES = {
    "invalid_request": "Pedido inválido.",
    "unauthenticated": "Token ausente, inválido ou revogado.",
    "forbidden": "Este token não tem a permissão necessária.",
    "not_found": "Recurso não encontrado.",
    "method_not_allowed": "Método não permitido.",
    "conflict": "Já existe uma pessoa com esse RA ou e-mail.",
    "internal": "Erro interno. Tente de novo mais tarde.",
    "already_exists": "Já existe um oferecimento com esse código.",
    "invalid_state": "A operação não é permitida no estado atual do oferecimento.",
    "bot_conflict": "Esse bot não pode ser usado neste oferecimento.",
    "bot_required": "O oferecimento não tem um bot registrado com acesso.",
    "invalid_confirmation": "O código de confirmação é inválido ou venceu.",
    "unavailable": "O Telegram não respondeu. Tente de novo em instantes.",
    "not_configured": "O servidor não tem a chave-mestra dos bots configurada.",
    "list_conflict": ("Há linhas da lista em conflito com pessoas cadastradas ou com a equipe; "
                      "nada foi gravado."),
    "role_conflict": "A mesma pessoa não pode ser equipe e aluno do mesmo oferecimento.",
    # Disparo e resposta.
    "invalid_dispatch_state": "A operação não é permitida no estado atual do disparo.",
    "window_expired": "A janela do disparo não é mais válida; prepare um novo disparo.",
    "grading_pending": ("Há respostas aguardando correção ou mensagens ainda não processadas "
                        "neste oferecimento."),
    # CLI e expurgo.
    "purge_pending": ("Há trabalho pendente neste oferecimento; o expurgo só pode ser feito depois que ele "
                      "terminar."),
    "purge_changed": ("As contagens do expurgo mudaram desde a preparação; prepare de novo e confira o "
                      "impacto."),
}

STATUS_OF = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "internal": 500,
    "already_exists": 409,
    "invalid_state": 409,
    "bot_conflict": 409,
    "bot_required": 409,
    "invalid_confirmation": 409,
    "unavailable": 503,
    "not_configured": 503,
    "list_conflict": 409,
    "role_conflict": 409,
    # Disparo e resposta.
    "invalid_dispatch_state": 409,
    "window_expired": 409,
    "grading_pending": 409,
    # CLI e expurgo.
    "purge_pending": 409,
    "purge_changed": 409,
}

# Códigos em que `fields` é obrigatório (lista não vazia); nos demais, proibido.
# também em `list_conflict` e `role_conflict`.
FIELDS_CODES = frozenset({"invalid_request", "conflict", "already_exists", "bot_conflict",
                          "list_conflict", "role_conflict"})

# Estados do oferecimento, valores possíveis de `state` no `invalid_state`.
OFFERING_STATES = ("draft", "active", "closed", "archived")

# CLI e expurgo: códigos em que `pending` é obrigatório; nos demais, proibido. As cinco contagens do
# objeto, na ordem do contrato.
PENDING_CODES = frozenset({"purge_pending"})
PENDING_KEYS = ("outbox", "updates", "tasks", "freeze", "due")

# Os esquemas das saídas `--json` dos comandos novos do CLI.
CLI_CONTRACTS = REPO / "contracts" / "cli" / "v1"
CLI_SCHEMAS = CLI_CONTRACTS / "schemas"

_MISSING = object()


@lru_cache(maxsize=None)
def load_json(path: str) -> Any:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def load_catalog() -> dict:
    return load_json(str(CONTRACTS / "catalog.json"))


def load_schema(op_id: str, kind: str) -> dict:
    """`kind` é `request` ou `response`; `op_id == "error"` devolve o envelope de erro."""
    if op_id == "error":
        return load_json(str(SCHEMAS / "error.schema.json"))
    return load_json(str(SCHEMAS / f"{op_id}.{kind}.schema.json"))


def _validate(schema: dict, body: Any, label: str) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(body), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:10])
        raise AssertionError(f"{label} não valida contra o esquema congelado: {detail}")


def assert_matches(op_id: str, status: int, body: Any) -> None:
    """Valida `body` contra o esquema congelado adequado ao status."""
    if 200 <= status < 300:
        _validate(load_schema(op_id, "response"), body, f"{op_id} {status}")
    elif op_id == "system.health" and status == 503:
        _validate(load_schema("system.health", "response"), body, "system.health 503")
    else:
        _validate(load_schema("error", ""), body, f"{op_id} {status} (erro)")


def _pending_object(pending: Any) -> dict:
    """o `pending` esperado como objeto JSON: um mapeamento ou um objeto com os atributos de `PENDING_KEYS`
    (ex.: o `PurgePending` do núcleo), sempre com as cinco chaves e só elas."""
    if isinstance(pending, dict):
        assert set(pending) == set(PENDING_KEYS), (
            f"o `pending` esperado precisa ter exatamente {list(PENDING_KEYS)}: {sorted(pending)}")
        return {key: pending[key] for key in PENDING_KEYS}
    missing = [key for key in PENDING_KEYS if not hasattr(pending, key)]
    assert not missing, f"o `pending` esperado não tem {missing}"
    return {key: getattr(pending, key) for key in PENDING_KEYS}


def assert_error(resp: Any, status: int, code: str, *, fields: Any = _MISSING,
                 permission: Any = _MISSING, state: Any = _MISSING, pending: Any = _MISSING) -> None:
    """Confere o envelope de erro **exato** do contrato.

    `fields` (lista de pares `(field, reason)`) é obrigatório nos códigos de `FIELDS_CODES` e
    proibido nos demais; `permission` é obrigatório no 403 e proibido nos demais; `state` (o estado
    atual do oferecimento) é obrigatório no `invalid_state` e proibido nos demais.

    Expurgo: `pending` (as cinco contagens `{outbox, updates, tasks, freeze, due}`, num mapeamento ou num
    objeto com esses atributos) é obrigatório nos códigos de `PENDING_CODES` e proibido nos demais; o valor
    devolvido é comparado **exato** (as cinco chaves, inteiros iguais).
    """
    assert STATUS_OF[code] == status, f"código {code} não corresponde ao status {status}"
    assert resp.status_code == status, (
        f"status {resp.status_code} != {status}; corpo: {resp.text[:500]}")
    body = resp.json()
    assert_matches("error", status, body)
    expected: dict = {"code": code, "message": MESSAGES[code]}
    if code in FIELDS_CODES:
        assert fields is not _MISSING, f"o teste precisa dizer os `fields` esperados no {code}"
        expected["fields"] = [{"field": f, "reason": r} for (f, r) in fields]
    else:
        assert fields is _MISSING, f"`fields` só existe em {sorted(FIELDS_CODES)}"
    if status == 403:
        assert permission is not _MISSING, "o teste precisa dizer a `permission` esperada no 403"
        expected["permission"] = permission
    else:
        assert permission is _MISSING, "`permission` só existe no 403"
    if code == "invalid_state":
        assert state in OFFERING_STATES, "o teste precisa dizer o `state` esperado no invalid_state"
        expected["state"] = state
    else:
        assert state is _MISSING, "`state` só existe no invalid_state"
    # O `pending` exato no `purge_pending`.
    if code in PENDING_CODES:
        assert pending is not _MISSING, f"o teste precisa dizer o `pending` esperado no {code}"
        expected["pending"] = _pending_object(pending)
    else:
        assert pending is _MISSING, f"`pending` só existe em {sorted(PENDING_CODES)}"
    assert body == {"error": expected}, f"envelope {body} != {{'error': {expected}}}"
    if status == 401:
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


def load_cli_schema(command: str) -> dict:
    """o esquema congelado da saída `--json` de `command` (`contracts/cli/v1/schemas/<comando>.output.schema.json`;
    um comando de várias palavras, como `offerings purge prepare`, vira `offerings.purge.prepare`)."""
    name = ".".join(command.split())
    assert name, "assert_cli_json precisa do nome do comando"
    return load_json(str(CLI_SCHEMAS / f"{name}.output.schema.json"))


def assert_cli_json(command: str, payload: Any) -> Any:
    """Valida a saída `--json` de `command` contra o esquema congelado do CLI e
    devolve o objeto. `payload` é o texto do stdout (lido como um único documento JSON) ou o objeto já lido."""
    if isinstance(payload, (str, bytes)):
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise AssertionError(f"a saída --json de `{command}` não é um documento JSON: {exc}") from None
    _validate(load_cli_schema(command), payload, f"cq {command} --json")
    return payload


def _response_text(resp: Any) -> str:
    try:
        body = resp.json()
    except ValueError:
        body_text = resp.text
    else:
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            body = json.loads(json.dumps(body))
            body["error"].pop("permission", None)
        body_text = json.dumps(body, ensure_ascii=False)
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    return body_text + "\n" + headers


def assert_no_echo(resp: Any, *values: str) -> None:
    """Nenhum dos valores sentinela enviados aparece no corpo nem nos cabeçalhos da resposta.

    Os valores precisam ter pelo menos 8 caracteres. O campo `permission`
    do 403 é ignorado, porque o contrato o devolve.
    """
    text = _response_text(resp)
    folded = text.casefold()
    for value in values:
        assert len(value) >= 8, f"sentinela curto demais para a checagem de eco: {value!r}"
        escaped = json.dumps(value)[1:-1]
        assert value not in text and value.casefold() not in folded and escaped not in text, (
            f"a resposta ecoa o valor enviado {value!r}")


def _resolve(node: Any, doc: dict | None) -> Any:
    seen = 0
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        assert ref.startswith("#/"), f"$ref externo não suportado: {ref}"
        target: Any = doc
        for part in ref[2:].split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
        seen += 1
        assert seen < 50, "ciclo de $ref"
    return node


def _is_null_branch(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == "null" and len(
        [k for k in node if not k.startswith("x-") and k not in ("title", "description")]) == 1


def _marks(node: dict) -> set[str]:
    marks: set[str] = set()
    if "x-identity" in node:
        marks.add(f"x-identity:{node['x-identity']}")
    if node.get("x-secret") is True:
        marks.add("x-secret")
    if node.get("x-free-text") is True:
        marks.add("x-free-text")
    # A marca do conteúdo de terceiros, não confiável.
    if node.get("x-untrusted") is True:
        marks.add("x-untrusted")
    return marks


def flatten(schema: dict, doc: dict | None = None) -> set[tuple[str, frozenset[str]]]:
    """Forma achatada do contrato: pares (caminho, marcas), resolvendo `$ref` e combinadores."""
    acc: dict[str, set[str]] = {}

    def walk(node: Any, path: str, *, branch: bool = False) -> None:
        node = _resolve(node, doc)
        if not isinstance(node, dict):
            return
        if _is_null_branch(node):
            # Um ramo `null` de anulável não acrescenta nada; uma propriedade só `null` (ex.:
            # `revoked_at` na criação do token) continua existindo como caminho.
            if path and not branch:
                acc.setdefault(path, set())
            return
        if path:
            acc.setdefault(path, set()).update(_marks(node))
        for key in ("allOf", "anyOf", "oneOf"):
            for sub in node.get(key, []):
                walk(sub, path, branch=True)
        for name, child in node.get("properties", {}).items():
            walk(child, f"{path}.{name}" if path else name)
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]")

    walk(schema, "")
    return {(p, frozenset(m)) for p, m in acc.items()}


def _is_object(node: dict) -> bool:
    t = node.get("type")
    return t == "object" or (isinstance(t, list) and "object" in t)


def iter_objects(schema: Any, doc: dict | None = None, _seen: set[int] | None = None):
    """Percorre os subesquemas de **objeto** (`type: "object"`), para checar `additionalProperties`.

    Ramos de `allOf`/`anyOf`/`oneOf` sem `type` só refinam um pai fechado e não contam como objeto,
    mas são percorridos em busca de objetos aninhados.
    """
    _seen = set() if _seen is None else _seen
    node = _resolve(schema, doc)
    if not isinstance(node, dict) or id(node) in _seen:
        return
    _seen.add(id(node))
    if _is_object(node):
        yield node
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for branch in node.get(key, []) or []:
            yield from iter_objects(branch, doc, _seen)
    for child in node.get("properties", {}).values():
        yield from iter_objects(child, doc, _seen)
    for key in ("items", "additionalProperties", "not"):
        if isinstance(node.get(key), dict):
            yield from iter_objects(node[key], doc, _seen)


# --- a mídia do 200 de `reports.get` ------------------------

REPORT_JSON_MEDIA = "application/json"
REPORT_CSV_MEDIA = "text/csv; charset=utf-8"
REPORT_CSV_COLUMNS = ("section", "field", "key", "metric", "on_time", "with_late")
_CSV_BOM = b"\xef\xbb\xbf"
# Os numéricos do CSV (inteiros, frações com ponto e milissegundos, inclusive negativos).
_CSV_NUMBER = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?")
# O primeiro caractere que a neutralização (`csv_cell`) sempre precede de `'` numa célula de texto.
_CSV_FORMULA_START = frozenset("=+-@\t\r")
_CSV_TEXT_COLUMNS = REPORT_CSV_COLUMNS[:4]


def _media_type(resp: Any) -> str:
    return resp.headers.get("content-type", "")


def _is_control(char: str) -> bool:
    code = ord(char)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _text_cell_ok(cell: str) -> bool:
    """Uma célula de texto do CSV neutralizada: não começa com `=`, `+`, `-`, `@`,
    tabulação nem retorno (esses ganham `'` na frente) e não tem controle C0/C1, salvo a tabulação ou o
    retorno logo depois do `'` inicial da neutralização (os controles "restantes" viram o escape visível
    de seis caracteres, barra invertida, `u00` e os dois dígitos hexadecimais)."""
    if cell and cell[0] in _CSV_FORMULA_START:
        return False
    rest = cell[2:] if cell[:1] == "'" and cell[1:2] in ("\t", "\r") else cell
    return not any(_is_control(char) for char in rest)


def assert_report_csv(resp: Any) -> str:
    """Confere o 200 `text/csv; charset=utf-8` de `reports.get` e devolve o texto inteiro,
    com o BOM inicial (decodificado dos bytes como UTF-8, sem tirar o BOM: o texto come\u00e7a por U+FEFF)."""
    assert resp.status_code == 200, f"reports.get (csv): status {resp.status_code}; corpo: {resp.text[:300]}"
    assert _media_type(resp) == REPORT_CSV_MEDIA, (
        f"reports.get (csv): mídia {_media_type(resp)!r} != {REPORT_CSV_MEDIA!r}")
    raw = resp.content
    assert raw.startswith(_CSV_BOM), "reports.get (csv): o corpo não começa com o BOM do UTF-8"
    text = raw.decode("utf-8")
    body = text[1:]
    assert body.endswith("\r\n"), "reports.get (csv): o corpo não termina com CRLF"
    # O LF só aparece no fim de registro (CRLF): dentro de uma célula ele é controle C0 e vira escape.
    assert "\n" not in body.replace("\r\n", ""), "reports.get (csv): quebra de linha fora de CRLF"
    rows = list(csv.reader(io.StringIO(body, newline=""), strict=True))
    assert rows and tuple(rows[0]) == REPORT_CSV_COLUMNS, (
        f"reports.get (csv): cabeçalho {rows[:1]} != {list(REPORT_CSV_COLUMNS)}")
    for number, row in enumerate(rows[1:], start=2):
        assert len(row) == len(REPORT_CSV_COLUMNS), f"reports.get (csv): linha {number} com {len(row)} células"
        assert row[0] != "", f"reports.get (csv): linha {number} sem `section`"
        for name, cell in zip(_CSV_TEXT_COLUMNS, row[:4]):
            assert _text_cell_ok(cell), (
                f"reports.get (csv): linha {number}, `{name}` sem a neutralização (fórmula ou controle C0/C1)")
        for name, cell in zip(REPORT_CSV_COLUMNS[4:], row[4:]):
            assert cell == "" or _CSV_NUMBER.fullmatch(cell), (
                f"reports.get (csv): linha {number}, `{name}` não é número nem vazio")
    return text


def assert_report_response(resp: Any) -> Any:
    """Confere o 200 de `reports.get` pela mídia: `application/json` → `assert_matches` e devolve o
    objeto; `text/csv; charset=utf-8` → `assert_report_csv` e devolve o texto (com o BOM)."""
    assert resp.status_code == 200, f"reports.get: status {resp.status_code}; corpo: {resp.text[:300]}"
    media = _media_type(resp)
    if media == REPORT_CSV_MEDIA:
        return assert_report_csv(resp)
    assert media == REPORT_JSON_MEDIA, f"reports.get: mídia {media!r} fora de {REPORT_JSON_MEDIA!r} e {REPORT_CSV_MEDIA!r}"
    body = resp.json()
    assert_matches("reports.get", 200, body)
    return body
