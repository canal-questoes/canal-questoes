"""Montagem da API HTTP: o app FastAPI, o envelope de erro e o OpenAPI publicado.

`create_app(settings, *, clock=None, bots=None)`:
- sobe **sem** banco: nenhuma conexão na criação nem na subida; cada operação conecta sob demanda,
  com `connect_timeout` de 3 s (`Database`), e a saída do lifespan fecha as conexões guardadas;
- não configura o logging (só o `cq-server` chama `configure_logging`);
- não tem `/docs` nem `/redoc`, e não redireciona barra final (`redirect_slashes=False`: um caminho
  com barra no fim é desconhecido, e nenhum 3xx sai da API);
- converte 404, 405, erros do contrato e respostas fora do esquema no envelope de erro da API;
- publica em `/openapi.json` um OpenAPI 3.1 sem 422 nem `ValidationError`, com os parâmetros e os
  corpos de `operations` e as marcas de contrato dos esquemas;
- guarda o `BotRuntime` de `bots` no contexto (`None`: sem bots). As operações de
  oferecimentos, bots e equipe existem sempre; sem `bots` (ou sem chave-mestra), o registro do bot
  responde 503 `not_configured`, e as demais funcionam (nenhuma outra fala com o Telegram);
- `repo_settings` (padrão `RepoSettings()`, a
  lista de hosts vazia, que recusa todo repositório no preparo); o `create_app` não lê o ambiente (só o
  `__main__` lê `RepoSettings.from_env`);
- `doctor_settings` (padrão `DoctorSettings()`); o `create_app` não
  lê o ambiente (só o `__main__` lê `DoctorSettings.from_env`); guardado no contexto para a rota
  `system.doctor`. Sem `doctor_settings`, o padrão
  leva a cifra do `bots` (`DoctorSettings(token_cipher=…)`), com que o `collect` testa a chave-mestra; com ele, o
  objeto vai como veio.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic.json_schema import models_json_schema
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.routing import Match

import cq_server
from cq_server.api import schemas
from cq_server.api.context import ApiContext
from cq_server.api.database import Database
from cq_server.api.errors import ApiError, FieldIssue, error_response, from_api_error
from cq_server.api.middleware import ApiMiddleware
from cq_server.api.operations import BY_ID
from cq_server.api.routes import register_routes
from cq_server.bots import BotRuntime
from cq_server.clock import Clock, SystemClock
from cq_server.config import RepoSettings, Settings
from cq_server.doctor import DoctorSettings
from cq_server.telegramrender import PlainFormatter

logger = logging.getLogger("cq_server.api")

REF_TEMPLATE = "#/components/schemas/{model}"
BEARER_SCHEME = "bearer"

DESCRIPTION = (
    "API do canal-questoes (v1). As operações sob `/v1` exigem `Authorization: Bearer <segredo>`; "
    "`/health` e `/openapi.json` não exigem token. Erros no envelope `{\"error\": {...}}`."
)


# --- tratadores de exceção (dentro do ExceptionMiddleware) ---------------------------------------


async def _api_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    return from_api_error(exc)


def _allowed_methods(request: Request) -> str | None:
    """Os métodos de **todas** as rotas do caminho (o Starlette só cita a primeira que casou)."""
    methods: set[str] = set()
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is not Match.NONE:
            methods |= set(getattr(route, "methods", None) or ())
    return ", ".join(sorted(methods)) or None


async def _http_error(request: Request, exc: Exception) -> Response:
    """404 e 405 do roteador no envelope do contrato (o 405 com o cabeçalho `Allow`)."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return error_response("not_found")
    if exc.status_code == 405:
        allow = _allowed_methods(request)
        return error_response("method_not_allowed", headers={"Allow": allow} if allow else None)
    logger.warning("http.unexpected_status", extra={"event": "http.unexpected_status",
                                                    "status": exc.status_code})
    return error_response("internal")


async def _request_validation_error(request: Request, exc: Exception) -> Response:
    """Defensivo: as rotas não declaram parâmetros, então o FastAPI não valida nada sozinho. Se
    validar, o 400 sai sem nenhum detalhe do Pydantic (que ecoaria o valor)."""
    return error_response("invalid_request", fields=[FieldIssue("body", "invalid_format")])


async def _response_validation_error(request: Request, exc: Exception) -> Response:
    """Resposta fora do esquema: falha fechada, com o 500 do contrato."""
    logger.error("exception", exc_info=exc)
    return error_response("internal")


# --- OpenAPI -------------------------------------------------------------------------------------


def _install_openapi(app: FastAPI) -> None:
    """Completa o OpenAPI gerado pelo FastAPI com parâmetros, corpos e segurança de `operations`."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        document = get_openapi(title=app.title, version=app.version,
                               openapi_version=app.openapi_version, description=app.description,
                               routes=app.routes)
        components = document.setdefault("components", {})
        component_schemas = components.setdefault("schemas", {})
        refs, definitions = models_json_schema(
            [(model, "validation") for model in schemas.REQUEST_MODELS], ref_template=REF_TEMPLATE)
        for name, schema in definitions.get("$defs", {}).items():
            existing = component_schemas.setdefault(name, schema)
            if existing != schema:
                raise RuntimeError(f"componente do OpenAPI com dois esquemas diferentes: {name}")

        for route in app.routes:
            if not isinstance(route, APIRoute) or route.operation_id not in BY_ID:
                continue
            op = BY_ID[route.operation_id]
            operation = document["paths"][op.path][op.method.lower()]
            parameters = op.openapi_parameters()
            if parameters:
                operation["parameters"] = parameters
            if op.request_model is not None:
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": refs[(op.request_model, "validation")]}},
                }
            if op.authenticated:
                operation["security"] = [{BEARER_SCHEME: []}]
        components["securitySchemes"] = {BEARER_SCHEME: {"type": "http", "scheme": "bearer"}}
        app.openapi_schema = document
        return document

    app.openapi = openapi  # type: ignore[method-assign]


# --- app -----------------------------------------------------------------------------------------


def create_app(settings: Settings, *, clock: Clock | None = None,
               bots: BotRuntime | None = None,
               repo_settings: RepoSettings | None = None,
               doctor_settings: DoctorSettings | None = None) -> FastAPI:
    database = Database(settings.database_url)
    # Sem `doctor_settings`, a composição põe a cifra do runtime dos bots
    # (com que o `collect` testa a chave-mestra); com ele, o objeto vai como veio (a rota nunca o troca).
    if doctor_settings is None:
        doctor_settings = DoctorSettings(token_cipher=bots.cipher if bots is not None else None)
    # O cálculo do relatório só usa o `plain` do formatador; a API usa o do
    # texto simples, sem parâmetro próprio no `create_app`.
    context = ApiContext(settings=settings, clock=clock or SystemClock(), database=database,
                         bots=bots, repo_settings=repo_settings or RepoSettings(),
                         report_formatter=PlainFormatter(),
                         doctor_settings=doctor_settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.open()
        try:
            yield
        finally:
            database.close()

    app = FastAPI(
        title="canal-questoes",
        version=cq_server.__version__,
        description=DESCRIPTION,
        openapi_url="/openapi.json",
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
        debug=False,
        lifespan=lifespan,
        exception_handlers={
            ApiError: _api_error,
            StarletteHTTPException: _http_error,
            RequestValidationError: _request_validation_error,
            ResponseValidationError: _response_validation_error,
        },
    )
    register_routes(app, context)
    app.add_middleware(ApiMiddleware, context=context, router=app.router)
    _install_openapi(app)
    return app
