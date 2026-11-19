"""Envelope de erro da API v1.

`{"error": {"code", "message", "permission"?, "state"?, "fields"?}}`:
- `permission` só no 403 (`forbidden`);
- `state` (o estado atual do oferecimento) só no 409 `invalid_state`;
- `fields` (lista não vazia) só nos códigos de `FIELDS_CODES`: `invalid_request`, `conflict`,
  `already_exists` e `bot_conflict` (a regra vale por código, não por status);
  também `list_conflict` (as linhas da lista em conflito, `rows[i]`) e `role_conflict` (`person`).

Disparos: `invalid_dispatch_state`, `window_expired` e `grading_pending` (409), no
mesmo envelope, sem `fields`, `state` nem `permission`.

Expurgo: `purge_pending` e `purge_changed` (409), sem `fields`, `state`
nem `permission`; `pending` (as 5 contagens do trabalho pendente, `purge.PurgePending`) obrigatório no
`purge_pending` (`PENDING_CODES`) e proibido nos demais códigos.

As mensagens são fixas e **nunca** ecoam valores enviados: nenhum erro carrega texto vindo
do pedido, só nomes de campo do esquema, motivos do contrato e o estado do oferecimento.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from cq_server.purge import PurgePending

MESSAGES: dict[str, str] = {
    "invalid_request": "Pedido inválido.",
    "unauthenticated": "Token ausente, inválido ou revogado.",
    "forbidden": "Este token não tem a permissão necessária.",
    "not_found": "Recurso não encontrado.",
    "method_not_allowed": "Método não permitido.",
    "conflict": "Já existe uma pessoa com esse RA ou e-mail.",
    "internal": "Erro interno. Tente de novo mais tarde.",
    # Oferecimentos, bots e equipe
    "already_exists": "Já existe um oferecimento com esse código.",
    "invalid_state": "A operação não é permitida no estado atual do oferecimento.",
    "bot_conflict": "Esse bot não pode ser usado neste oferecimento.",
    "bot_required": "O oferecimento não tem um bot registrado com acesso.",
    "invalid_confirmation": "O código de confirmação é inválido ou venceu.",
    "unavailable": "O Telegram não respondeu. Tente de novo em instantes.",
    "not_configured": "O servidor não tem a chave-mestra dos bots configurada.",
    # Lista de alunos e papéis
    "list_conflict": ("Há linhas da lista em conflito com pessoas cadastradas ou com a equipe; "
                      "nada foi gravado."),
    "role_conflict": "A mesma pessoa não pode ser equipe e aluno do mesmo oferecimento.",
    # Disparos: sem `fields`, `state` nem `permission`.
    "invalid_dispatch_state": "A operação não é permitida no estado atual do disparo.",
    "window_expired": "A janela do disparo não é mais válida; prepare um novo disparo.",
    "grading_pending": ("Há respostas aguardando correção ou mensagens ainda não processadas neste "
                        "oferecimento."),
    # Expurgo: sem `fields`, `state` nem `permission`; `pending` só no `purge_pending`.
    "purge_pending": ("Há trabalho pendente neste oferecimento; o expurgo só pode ser feito depois que "
                      "ele terminar."),
    "purge_changed": ("As contagens do expurgo mudaram desde a preparação; prepare de novo e confira o "
                      "impacto."),
}

STATUS: dict[str, int] = {
    "invalid_request": 400,
    "unauthenticated": 401,
    "forbidden": 403,
    "not_found": 404,
    "method_not_allowed": 405,
    "conflict": 409,
    "internal": 500,
    # Oferecimentos, bots e equipe
    "already_exists": 409,
    "invalid_state": 409,
    "bot_conflict": 409,
    "bot_required": 409,
    "invalid_confirmation": 409,
    "unavailable": 503,
    "not_configured": 503,
    # Lista de alunos e papéis
    "list_conflict": 409,
    "role_conflict": 409,
    # Disparos
    "invalid_dispatch_state": 409,
    "window_expired": 409,
    "grading_pending": 409,
    # Expurgo
    "purge_pending": 409,
    "purge_changed": 409,
}

# Códigos em que `fields` é obrigatório (lista não vazia); nos demais, proibido
# (a regra vale por código).
FIELDS_CODES = frozenset({"invalid_request", "conflict", "already_exists", "bot_conflict",
                          "list_conflict", "role_conflict"})

# Códigos em que `pending` é obrigatório; nos demais, proibido.
PENDING_CODES = frozenset({"purge_pending"})

# Ordem das contagens de `pending` no envelope.
PENDING_KEYS = ("outbox", "updates", "tasks", "freeze", "due")

# Estados do oferecimento, valores de `state` no `invalid_state`.
OFFERING_STATES = ("draft", "active", "closed", "archived")


@dataclass(frozen=True)
class FieldIssue:
    """Um campo recusado: o caminho do esquema (`name`, `pseudonyms[2]`, `query`…) e o motivo."""

    field: str
    reason: str


class ApiError(Exception):
    """Erro do contrato, convertido no envelope pelo tratador registrado no app.

    A mensagem da exceção é só o código: nada do pedido entra nela.
    """

    def __init__(self, code: str, *, fields: Sequence[FieldIssue] = (), permission: str | None = None,
                 state: str | None = None, headers: Mapping[str, str] | None = None,
                 pending: PurgePending | None = None) -> None:
        if code not in MESSAGES:
            raise ValueError("código de erro desconhecido")
        super().__init__(code)
        self.code = code
        self.fields = tuple(fields)
        self.permission = permission
        self.state = state
        self.headers = dict(headers or {})
        self.pending = pending

    @property
    def status_code(self) -> int:
        return STATUS[self.code]


def _pending_body(pending: PurgePending) -> dict[str, int]:
    """As 5 contagens do `pending`, na ordem do contrato; só inteiros não negativos."""
    out: dict[str, int] = {}
    for key in PENDING_KEYS:
        value = getattr(pending, key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("contagem do pending inválida")
        out[key] = value
    return out


def error_body(code: str, *, fields: Sequence[FieldIssue] = (), permission: str | None = None,
               state: str | None = None, pending: PurgePending | None = None) -> dict:
    error: dict = {"code": code, "message": MESSAGES[code]}
    if code == "forbidden":
        if permission is None:
            raise ValueError("o 403 precisa da permissão exigida")
        error["permission"] = permission
    if code == "invalid_state":
        if state not in OFFERING_STATES:
            raise ValueError("o invalid_state precisa do estado atual do oferecimento")
        error["state"] = state
    if code in FIELDS_CODES:
        if not fields:
            raise ValueError(f"o {code} precisa de pelo menos um campo")
        error["fields"] = [{"field": issue.field, "reason": issue.reason} for issue in fields]
    if code in PENDING_CODES:
        if pending is None:
            raise ValueError(f"o {code} precisa das contagens pendentes")
        error["pending"] = _pending_body(pending)
    return {"error": error}


def error_response(code: str, *, fields: Sequence[FieldIssue] = (), permission: str | None = None,
                   state: str | None = None, headers: Mapping[str, str] | None = None,
                   pending: PurgePending | None = None) -> JSONResponse:
    all_headers = dict(headers or {})
    if code == "unauthenticated":
        all_headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(error_body(code, fields=fields, permission=permission, state=state,
                                   pending=pending),
                        status_code=STATUS[code], headers=all_headers or None)


def from_api_error(exc: ApiError) -> JSONResponse:
    return error_response(exc.code, fields=exc.fields, permission=exc.permission, state=exc.state,
                          headers=exc.headers, pending=exc.pending)
