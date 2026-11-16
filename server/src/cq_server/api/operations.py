"""As operações da API v1, espelho do catálogo único (`contracts/api/v1/catalog.json`).

Cada operação diz o método, a rota, a permissão exigida, o que o OpenAPI publica dela (parâmetros e
corpo, já que a entrada é validada à mão em `cq_server.api.validation`) e os códigos de erro que ela
pode dar. No OpenAPI, a descrição de cada resposta de erro é por operação e código:
cada status lista os códigos daquela operação com as mensagens fixas. O catálogo congelado não
entra no pacote; a coerência entre os dois é conferida pelos testes de contrato.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cq_server.api import schemas
from cq_server.api.errors import MESSAGES, STATUS
from cq_server.api.validation import CLIENT_HEADER, CLIENT_KINDS, DEFAULT_CLIENT, MAX_CURSOR
from cq_server.events import DEFAULT_LIMIT, MAX_LIMIT, EventType
from cq_server.people import ALIAS_PATTERN, PSEUDO_ID_PATTERN
from cq_server.permissions import Permission
from cq_server.tokens import TOKEN_ID_PATTERN

# O teto é o mesmo que a validação aplica (`bigint`): acima dele, `out_of_range`.
_AFTER = {
    "name": "after", "in": "query", "required": False,
    "description": f"Cursor: só itens depois dele (0 a {MAX_CURSOR}, padrão 0).",
    "schema": {"type": "integer", "minimum": 0, "maximum": MAX_CURSOR, "default": 0},
}
_LIMIT = {
    "name": "limit", "in": "query", "required": False,
    "description": f"Máximo de itens (1 a {MAX_LIMIT}, padrão {DEFAULT_LIMIT}).",
    "schema": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT},
}
# `types` não é obrigatório no OpenAPI; a regra "obrigatório sem
# `person`" é da rota.
_TYPES = {
    "name": "types", "in": "query", "required": False, "style": "form", "explode": True,
    "description": "Tipos de evento, um ou mais, sem repetição (obrigatório sem `person`).",
    "schema": {"type": "array", "minItems": 1, "uniqueItems": True,
               "items": {"type": "string", "enum": [t.value for t in EventType]}},
}
# A pessoa citada (o mesmo padrão de `people.get`).
_PERSON = {
    "name": "person", "in": "query", "required": False,
    "description": "Pessoa citada nos eventos: apelido ou identificador (UUID v4 canônico minúsculo).",
    "schema": {"type": "string",
               "pattern": f"^(?:{ALIAS_PATTERN.pattern}|{PSEUDO_ID_PATTERN.pattern})$"},
}
_REF = {
    "name": "ref", "in": "path", "required": True,
    "description": "Pseudônimo: apelido ou identificador (UUID v4 canônico minúsculo).",
    "schema": {"type": "string",
               "pattern": f"^(?:{ALIAS_PATTERN.pattern}|{PSEUDO_ID_PATTERN.pattern})$"},
}
_TOKEN_ID = {
    "name": "token_id", "in": "path", "required": True,
    "description": "Identificador do token (UUID v4 canônico minúsculo).",
    "schema": {"type": "string", "pattern": f"^{TOKEN_ID_PATTERN.pattern}$"},
}
# O identificador ou o código do oferecimento (nunca no formato de UUID).
_OFFERING_REF = {
    "name": "ref", "in": "path", "required": True,
    "description": "Oferecimento: identificador (UUID v4 canônico minúsculo) ou código.",
    "schema": {"type": "string", "pattern": (
        f"^(?:{PSEUDO_ID_PATTERN.pattern}"
        "|(?![0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$)[a-z0-9][a-z0-9-]{2,39})$")},
}
# O identificador do disparo (UUID v4 canônico minúsculo).
_DISPATCH_ID = {
    "name": "dispatch_id", "in": "path", "required": True,
    "description": "Identificador do disparo (UUID v4 canônico minúsculo).",
    "schema": {"type": "string", "pattern": f"^{PSEUDO_ID_PATTERN.pattern}$"},
}
# A consulta de `reports.get` (estrita; validada à mão em `routes`).
_REPORT_FORMAT = {
    "name": "format", "in": "query", "required": False,
    "description": "Formato da resposta: `json` (padrão) ou `csv`.",
    "schema": {"type": "string", "enum": ["json", "csv"], "default": "json"},
}
_REPORT_VERSION = {
    "name": "version", "in": "query", "required": False,
    "description": "Versão congelada (número ou `latest`); ausente: o cálculo no momento.",
    "schema": {"type": "string", "pattern": "^([1-9][0-9]{0,9}|latest)$"},
}
_CLIENT = {
    "name": CLIENT_HEADER, "in": "header", "required": False,
    "description": f"Cliente declarado (padrão `{DEFAULT_CLIENT}`).",
    "schema": {"type": "string", "enum": list(CLIENT_KINDS), "default": DEFAULT_CLIENT},
}

# Códigos comuns a toda operação de `/v1` (a saúde só tem o 500).
_V1 = ("invalid_request", "unauthenticated", "forbidden", "internal")


@dataclass(frozen=True)
class Operation:
    id: str
    method: str
    path: str
    permission: Permission | None
    summary: str
    parameters: tuple[dict[str, Any], ...] = ()
    request_model: type[schemas.StrictModel] | None = None
    errors: tuple[str, ...] = field(default=())

    @property
    def authenticated(self) -> bool:
        return self.path.startswith("/v1/")

    def openapi_parameters(self) -> list[dict[str, Any]]:
        params = [dict(p) for p in self.parameters]
        if self.authenticated:
            params.append(dict(_CLIENT))
        return params

    def error_descriptions(self) -> dict[int, str]:
        """Status → descrição por código desta operação (`código: mensagem`, na ordem de `errors`)."""
        by_status: dict[int, list[str]] = {}
        for code in self.errors:
            by_status.setdefault(STATUS[code], []).append(f"`{code}`: {MESSAGES[code]}")
        return {status: " ".join(parts) for status, parts in sorted(by_status.items())}


OPERATIONS: tuple[Operation, ...] = (
    Operation("system.health", "GET", "/health", None, "Saúde do sistema", errors=("internal",)),
    Operation("people.register", "POST", "/v1/people", Permission.PEOPLE_WRITE,
              "Cadastrar uma pessoa", request_model=schemas.PersonRegistration,
              errors=(*_V1, "conflict")),
    Operation("people.list", "GET", "/v1/people", Permission.PEOPLE_READ, "Listar pessoas",
              parameters=(_AFTER, _LIMIT), errors=_V1),
    Operation("people.get", "GET", "/v1/people/{ref}", Permission.PEOPLE_READ,
              "Consultar uma pessoa pelo pseudônimo", parameters=(_REF,), errors=(*_V1, "not_found")),
    Operation("identity.reveal", "POST", "/v1/identity/reveal", Permission.IDENTITY_REVEAL,
              "Revelar identidades (auditado)", request_model=schemas.RevealRequest, errors=_V1),
    Operation("tokens.create", "POST", "/v1/tokens", Permission.TOKENS_MANAGE, "Criar um token",
              request_model=schemas.TokenCreation, errors=_V1),
    Operation("tokens.list", "GET", "/v1/tokens", Permission.TOKENS_MANAGE, "Listar os tokens",
              errors=_V1),
    Operation("tokens.revoke", "POST", "/v1/tokens/{token_id}/revoke", Permission.TOKENS_MANAGE,
              "Revogar um token", parameters=(_TOKEN_ID,), errors=(*_V1, "not_found")),
    Operation("events.list", "GET", "/v1/events", Permission.EVENTS_READ,
              "Consultar o log de eventos", parameters=(_TYPES, _AFTER, _LIMIT, _PERSON), errors=_V1),
    # Oferecimentos, bots e equipe
    Operation("offerings.create", "POST", "/v1/offerings", Permission.OFFERINGS_WRITE,
              "Cadastrar um oferecimento", request_model=schemas.OfferingCreation,
              errors=(*_V1, "already_exists")),
    Operation("offerings.list", "GET", "/v1/offerings", Permission.OFFERINGS_READ,
              "Listar os oferecimentos", parameters=(_AFTER, _LIMIT), errors=_V1),
    Operation("offerings.get", "GET", "/v1/offerings/{ref}", Permission.OFFERINGS_READ,
              "Consultar um oferecimento", parameters=(_OFFERING_REF,), errors=(*_V1, "not_found")),
    Operation("offerings.close", "POST", "/v1/offerings/{ref}/close", Permission.OFFERINGS_WRITE,
              "Encerrar um oferecimento", parameters=(_OFFERING_REF,),
              errors=(*_V1, "not_found", "invalid_state")),
    Operation("offerings.prepare_archive", "POST", "/v1/offerings/{ref}/archive/prepare",
              Permission.OFFERINGS_WRITE, "Preparar o arquivamento de um oferecimento",
              parameters=(_OFFERING_REF,), errors=(*_V1, "not_found", "invalid_state", "grading_pending")),
    Operation("offerings.archive", "POST", "/v1/offerings/{ref}/archive", Permission.OFFERINGS_WRITE,
              "Confirmar o arquivamento de um oferecimento", parameters=(_OFFERING_REF,),
              request_model=schemas.ArchiveConfirmation,
              errors=(*_V1, "not_found", "invalid_state", "grading_pending", "invalid_confirmation")),
    Operation("bots.register", "PUT", "/v1/offerings/{ref}/bot", Permission.BOTS_MANAGE,
              "Registrar (ou trocar) o token do bot", parameters=(_OFFERING_REF,),
              request_model=schemas.BotRegistration,
              errors=(*_V1, "not_found", "invalid_state", "bot_conflict", "unavailable",
                      "not_configured")),
    Operation("bots.activate", "POST", "/v1/offerings/{ref}/bot/activate", Permission.BOTS_MANAGE,
              "Ativar o oferecimento (o bot passa a ser consultado)", parameters=(_OFFERING_REF,),
              errors=(*_V1, "not_found", "invalid_state", "bot_required")),
    Operation("bots.get", "GET", "/v1/offerings/{ref}/bot", Permission.OFFERINGS_READ,
              "Consultar o bot do oferecimento", parameters=(_OFFERING_REF,),
              errors=(*_V1, "not_found")),
    Operation("team.link", "POST", "/v1/offerings/{ref}/team-links", Permission.OFFERINGS_WRITE,
              "Gerar o link de equipe de uma pessoa", parameters=(_OFFERING_REF,),
              request_model=schemas.TeamLinkRequest,
              errors=(*_V1, "not_found", "invalid_state", "bot_required", "role_conflict")),
    # Lista de alunos e convites, no fim do catálogo
    Operation("students.import", "POST", "/v1/offerings/{ref}/students", Permission.PEOPLE_WRITE,
              "Importar a lista de alunos do oferecimento", parameters=(_OFFERING_REF,),
              request_model=schemas.StudentsImportRequest,
              errors=(*_V1, "not_found", "invalid_state", "list_conflict")),
    Operation("students.summary", "GET", "/v1/offerings/{ref}/students/summary",
              Permission.OFFERINGS_READ, "Consultar a adesão dos alunos do oferecimento",
              parameters=(_OFFERING_REF,), errors=(*_V1, "not_found")),
    Operation("invites.prepare_send", "POST", "/v1/offerings/{ref}/invites/prepare",
              Permission.OFFERINGS_WRITE, "Preparar o envio dos convites aos alunos",
              parameters=(_OFFERING_REF,), request_model=schemas.InvitesPrepareRequest,
              errors=(*_V1, "not_found", "invalid_state", "bot_required")),
    Operation("invites.send", "POST", "/v1/offerings/{ref}/invites", Permission.OFFERINGS_WRITE,
              "Confirmar o envio dos convites aos alunos", parameters=(_OFFERING_REF,),
              request_model=schemas.InvitesSendRequest,
              errors=(*_V1, "not_found", "invalid_state", "bot_required", "invalid_confirmation")),
    Operation("invites.resend", "POST", "/v1/offerings/{ref}/invites/resend",
              Permission.OFFERINGS_WRITE, "Reenviar o convite a alunos, por pseudônimo",
              parameters=(_OFFERING_REF,), request_model=schemas.InvitesResendRequest,
              errors=(*_V1, "not_found", "invalid_state", "bot_required")),
    # Disparos, respostas e mensagens livres, no fim do catálogo
    Operation("dispatches.prepare", "POST", "/v1/offerings/{ref}/dispatches", Permission.OFFERINGS_WRITE,
              "Preparar um disparo de questão (validação no worker)", parameters=(_OFFERING_REF,),
              request_model=schemas.DispatchPrepareRequest,
              errors=(*_V1, "not_found", "invalid_state", "bot_required")),
    Operation("dispatches.get", "GET", "/v1/offerings/{ref}/dispatches/{dispatch_id}", Permission.OFFERINGS_READ,
              "Consultar um disparo, com a prévia", parameters=(_OFFERING_REF, _DISPATCH_ID),
              errors=(*_V1, "not_found")),
    Operation("dispatches.list", "GET", "/v1/offerings/{ref}/dispatches", Permission.OFFERINGS_READ,
              "Listar os disparos do oferecimento", parameters=(_OFFERING_REF, _AFTER, _LIMIT),
              errors=(*_V1, "not_found")),
    Operation("dispatches.confirm", "POST", "/v1/offerings/{ref}/dispatches/{dispatch_id}/confirm",
              Permission.OFFERINGS_WRITE, "Confirmar um disparo validado (envio à turma)",
              parameters=(_OFFERING_REF, _DISPATCH_ID),
              errors=(*_V1, "not_found", "invalid_state", "bot_required", "invalid_dispatch_state",
                      "window_expired")),
    Operation("dispatches.cancel", "POST", "/v1/offerings/{ref}/dispatches/{dispatch_id}/cancel",
              Permission.OFFERINGS_WRITE, "Cancelar um disparo que ainda não abriu",
              parameters=(_OFFERING_REF, _DISPATCH_ID), errors=(*_V1, "not_found", "invalid_dispatch_state")),
    Operation("messages.list", "GET", "/v1/offerings/{ref}/messages", Permission.OFFERINGS_READ,
              "Listar as mensagens livres dos alunos", parameters=(_OFFERING_REF, _AFTER, _LIMIT),
              errors=(*_V1, "not_found")),
    # Configuração das mensagens livres, no fim do catálogo
    Operation("offerings.update_settings", "PATCH", "/v1/offerings/{ref}/settings", Permission.OFFERINGS_WRITE,
              "Alterar a configuração de mensagens livres do oferecimento", parameters=(_OFFERING_REF,),
              request_model=schemas.OfferingSettingsUpdate,
              errors=(*_V1, "not_found", "invalid_state")),
    # Relatórios, no fim do catálogo
    Operation("reports.get", "GET", "/v1/offerings/{ref}/dispatches/{dispatch_id}/report", Permission.OFFERINGS_READ,
              "Consultar o relatório de um disparo (JSON ou CSV)",
              parameters=(_OFFERING_REF, _DISPATCH_ID, _REPORT_FORMAT, _REPORT_VERSION),
              errors=(*_V1, "not_found", "invalid_dispatch_state")),
    Operation("reports.list_versions", "GET", "/v1/offerings/{ref}/dispatches/{dispatch_id}/report/versions",
              Permission.OFFERINGS_READ, "Listar as versões congeladas do relatório de um disparo",
              parameters=(_OFFERING_REF, _DISPATCH_ID, _AFTER, _LIMIT),
              errors=(*_V1, "not_found", "invalid_dispatch_state")),
    Operation("reports.resend_summary", "POST", "/v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend",
              Permission.OFFERINGS_WRITE, "Congelar a versão seguinte do relatório e reenviar o resumo à equipe",
              parameters=(_OFFERING_REF, _DISPATCH_ID),
              errors=(*_V1, "not_found", "invalid_state", "invalid_dispatch_state")),
    Operation("offerings.get_digest", "GET", "/v1/offerings/{ref}/digest", Permission.OFFERINGS_READ,
              "Consultar a configuração do resumo diário do oferecimento", parameters=(_OFFERING_REF,),
              errors=(*_V1, "not_found")),
    Operation("offerings.update_digest", "PATCH", "/v1/offerings/{ref}/digest", Permission.OFFERINGS_WRITE,
              "Alterar a configuração do resumo diário do oferecimento", parameters=(_OFFERING_REF,),
              request_model=schemas.OfferingDigestUpdate,
              errors=(*_V1, "not_found", "invalid_state")),
    # Diagnóstico e expurgo, no fim do catálogo: o `system.doctor` e as duas fases do expurgo
    Operation("system.doctor", "GET", "/v1/system/doctor", Permission.OFFERINGS_READ,
              "Verificar a instalação pela parte da API do doctor (só leitura)", errors=_V1),
    Operation("offerings.prepare_purge", "POST", "/v1/offerings/{ref}/purge/prepare", Permission.PEOPLE_WRITE,
              "Preparar o expurgo das identidades de um oferecimento arquivado", parameters=(_OFFERING_REF,),
              errors=(*_V1, "not_found", "invalid_state", "purge_pending")),
    Operation("offerings.purge", "POST", "/v1/offerings/{ref}/purge", Permission.PEOPLE_WRITE,
              "Confirmar o expurgo das identidades de um oferecimento arquivado", parameters=(_OFFERING_REF,),
              request_model=schemas.PurgeConfirmation,
              errors=(*_V1, "not_found", "invalid_state", "purge_pending", "invalid_confirmation",
                      "purge_changed")),
)

BY_ID: dict[str, Operation] = {op.id: op for op in OPERATIONS}
