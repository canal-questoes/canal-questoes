"""Validação da entrada em `/v1/*`, sem eco.

A validação é feita à mão, e não pelo 422 do FastAPI, porque o contrato fixa detalhes que o modelo
de erro do Pydantic não expressa sem olhar o valor recebido:
- **um só motivo por campo**, nesta precedência (tipo JSON errado → ausente/`null` →
  comprimento ou quantidade → formato ou valor → repetição);
- numa lista fora do limite, os itens não são validados;
- campo desconhecido **sem o nome** (o nome também é dado do cliente): uma única entrada
  `{body, unknown_field}` ou `{query, unknown_field}`, no fim da lista;
- corpo vazio → `{body, missing}`; corpo que não é JSON UTF-8 ou não é objeto → `{body,
  invalid_format}`; corpo numa rota sem corpo → `{body, unknown_field}`.

`RequestChecker` junta os problemas na ordem em que as rotas conferem (cabeçalho, caminho, consulta
e corpo, cada um na ordem do esquema) e levanta um só `ApiError(invalid_request)` com todos eles.
Nenhum problema guarda o valor recebido: só o caminho do campo e o motivo.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from starlette.datastructures import Headers, QueryParams

from cq_server.api.errors import ApiError, FieldIssue

CLIENT_HEADER = "X-CQ-Client"
CLIENT_KINDS = ("cli", "mcp", "other")
DEFAULT_CLIENT = "other"

_INTEGER = re.compile(r"-?[0-9]+")
# Um inteiro com mais dígitos que isto já está fora de qualquer faixa aceita (`bigint`).
_MAX_INTEGER_DIGITS = 19
MAX_CURSOR = 2**63 - 1

QUERY_UNKNOWN = FieldIssue("query", "unknown_field")
BODY_UNKNOWN = FieldIssue("body", "unknown_field")


class _Invalid:
    """Marca de valor recusado (o problema já foi registrado)."""

    def __repr__(self) -> str:
        return "INVALID"


INVALID: Any = _Invalid()


def _reject_constant(_: str) -> Any:
    raise ValueError("constante JSON não padronizada")


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("chave JSON repetida")
        obj[key] = value
    return obj


class RequestChecker:
    """Coleciona os problemas de um pedido e levanta o 400 do contrato (ver o docstring do módulo)."""

    def __init__(self) -> None:
        self._issues: list[FieldIssue] = []
        self._unknown_query = False
        self._unknown_body = False

    # --- resultado -------------------------------------------------------------------------------

    def add(self, field: str, reason: str) -> None:
        self._issues.append(FieldIssue(field, reason))

    @property
    def issues(self) -> list[FieldIssue]:
        issues = list(self._issues)
        if self._unknown_query:
            issues.append(QUERY_UNKNOWN)
        if self._unknown_body:
            issues.append(BODY_UNKNOWN)
        return issues

    def raise_if_invalid(self) -> None:
        issues = self.issues
        if issues:
            raise ApiError("invalid_request", fields=issues)

    # --- cabeçalho -------------------------------------------------------------------------------

    def client_kind(self, headers: Headers) -> str:
        """`X-CQ-Client`: `cli`, `mcp` ou `other` (diferencia maiúsculas); ausente vale `other`."""
        values = headers.getlist(CLIENT_HEADER)
        if not values:
            return DEFAULT_CLIENT
        if len(values) == 1 and values[0] in CLIENT_KINDS:
            return values[0]
        self.add(CLIENT_HEADER, "invalid_value")
        return DEFAULT_CLIENT

    # --- caminho ---------------------------------------------------------------------------------

    def path_param(self, path_params: Mapping[str, Any], name: str,
                   is_valid: Callable[[object], bool]) -> str | None:
        value = path_params.get(name)
        if not isinstance(value, str) or not is_valid(value):
            self.add(name, "invalid_format")
            return None
        return value

    # --- consulta --------------------------------------------------------------------------------

    def query_keys(self, params: QueryParams, allowed: Collection[str]) -> None:
        """Parâmetro de consulta desconhecido → `{query, unknown_field}` (no fim da lista)."""
        if any(key not in allowed for key, _ in params.multi_items()):
            self._unknown_query = True

    def query_int(self, params: QueryParams, name: str, *, default: int, minimum: int,
                  maximum: int) -> int | None:
        values = params.getlist(name)
        if not values:
            return default
        if len(values) > 1 or _INTEGER.fullmatch(values[0]) is None:
            self.add(name, "invalid_format")
            return None
        digits = values[0].lstrip("-").lstrip("0")
        if len(digits) > _MAX_INTEGER_DIGITS:
            self.add(name, "out_of_range")
            return None
        value = int(values[0])
        if not minimum <= value <= maximum:
            self.add(name, "out_of_range")
            return None
        return value

    def query_choices(self, params: QueryParams, name: str, choices: Collection[str]) -> list[str] | None:
        """Lista obrigatória de valores do conjunto dado, sem repetição (`types` de events.list)."""
        values = params.getlist(name)
        if not values:
            self.add(name, "missing")
            return None
        seen: set[str] = set()
        ok = True
        for index, value in enumerate(values):
            if value not in choices:
                self.add(f"{name}[{index}]", "invalid_value")
                ok = False
            elif value in seen:
                self.add(f"{name}[{index}]", "duplicate")
                ok = False
            else:
                seen.add(value)
        return values if ok else None

    def query_pattern(self, params: QueryParams, name: str, accept: Callable[[str], bool]) -> str | None:
        """`person` de events.list: parâmetro opcional único aceito por `accept`; repetido ou
        recusado → `(name, "invalid_format")`, sem eco do valor."""
        values = params.getlist(name)
        if not values:
            return None
        if len(values) > 1 or not accept(values[0]):
            self.add(name, "invalid_format")
            return None
        return values[0]

    # --- corpo -----------------------------------------------------------------------------------

    def reject_body(self, raw: bytes) -> None:
        """Rota sem corpo: qualquer corpo enviado (`Content-Length > 0`) → `{body, unknown_field}`."""
        if raw:
            self._unknown_body = True

    def json_object(self, raw: bytes) -> dict[str, Any] | None:
        """O corpo como objeto JSON (UTF-8 estrito), ou `None` com o problema registrado."""
        if not raw:
            self.add("body", "missing")
            return None
        try:
            text = raw.decode("utf-8")
            value = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            self.add("body", "invalid_format")
            return None
        if not isinstance(value, dict):
            self.add("body", "invalid_format")
            return None
        return value

    def body_keys(self, obj: Mapping[str, Any], allowed: Collection[str]) -> None:
        if any(key not in allowed for key in obj):
            self._unknown_body = True

    def required_text(self, obj: Mapping[str, Any], name: str) -> str | None:
        """Texto obrigatório: tipo errado → `invalid_format`; ausente ou `null` → `missing`."""
        value = obj.get(name)
        if value is None:
            self.add(name, "missing")
            return None
        if not isinstance(value, str):
            self.add(name, "invalid_format")
            return None
        return value

    def optional_text(self, obj: Mapping[str, Any], name: str) -> Any:
        """Texto opcional: ausente ou `null` → `None`; tipo errado → `INVALID` (registrado)."""
        value = obj.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            self.add(name, "invalid_format")
            return INVALID
        return value

    def required_list(self, obj: Mapping[str, Any], name: str, *, min_items: int,
                      max_items: int) -> list[Any] | None:
        """Lista obrigatória com quantidade entre os limites (os itens ficam com quem chama)."""
        value = obj.get(name)
        if value is None:
            self.add(name, "missing")
            return None
        if not isinstance(value, list):
            self.add(name, "invalid_format")
            return None
        if len(value) < min_items:
            self.add(name, "too_few")
            return None
        if len(value) > max_items:
            self.add(name, "too_many")
            return None
        return value
