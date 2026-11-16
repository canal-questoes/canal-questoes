"""Middleware ASGI da API: autenticação em `/v1/*`, falha fechada e log por requisição.

É um middleware ASGI **puro** de usuário, que fica por dentro do `ServerErrorMiddleware` do
Starlette e por fora do `ExceptionMiddleware` e do roteador:

1. **Autenticação**: todo caminho que começa com `/v1/` exige
   `Authorization: Bearer <segredo>`, **antes** da rota (um `/v1/…` desconhecido, sem token, dá 401;
   com token, 404). Fora de `/v1/`, o cabeçalho é ignorado. O token inicial é conferido sem banco;
   só um segredo com a forma `cq_…` de 43 caracteres consulta o banco.
2. **Exceções**: qualquer `Exception` que escape das rotas vira o 500 `internal` do contrato e um
   registro `event=exception` no logger `cq_server.api`, só com o tipo e a posição (o
   `JsonFormatter` não escreve mensagem nem traceback). Nada escapa para o servidor, que registraria
   o traceback com a mensagem.
3. **Log**: um registro `event=request` por requisição, com `method`, `route` (o modelo da rota que
   casou o caminho, também no 405; `null` quando nenhuma casa; nunca a URL crua) e `status`. O
   método só sai como chegou se for um método HTTP padrão; qualquer outro vira `OTHER`, porque vem
   do cliente e poderia carregar um RA ou um nome.

O middleware não configura o logging.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.routing import Match, Router
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cq_server import tokens
from cq_server.api.context import ApiContext
from cq_server.api.errors import error_response

logger = logging.getLogger("cq_server.api")

PRINCIPAL_SCOPE_KEY = "cq.principal"
AUTHENTICATED_PREFIX = "/v1/"
LOGGED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"})
OTHER_METHOD = "OTHER"


def principal_of(scope: Scope) -> tokens.Principal:
    principal = scope.get(PRINCIPAL_SCOPE_KEY)
    if not isinstance(principal, tokens.Principal):
        raise RuntimeError("rota /v1 sem autenticação")
    return principal


def _route_path(scope: Scope) -> str:
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        return path[len(root_path):]
    return path


def bearer_credentials(scope: Scope) -> bytes | None:
    """O segredo de `Authorization: Bearer <segredo>` (bytes crus), ou `None`.

    Exige exatamente um cabeçalho, o esquema `Bearer` (sem diferenciar maiúsculas, RFC 6750), um
    espaço e um segredo não vazio.
    """
    values = [value for key, value in scope.get("headers", []) if key.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, separator, credentials = values[0].partition(b" ")
    if not separator or scheme.lower() != b"bearer" or not credentials:
        return None
    return credentials


class ApiMiddleware:
    def __init__(self, app: ASGIApp, *, context: ApiContext, router: Router) -> None:
        self.app = app
        self.context = context
        self.router = router

    def _route_template(self, scope: Scope) -> str | None:
        """O modelo da rota que casa o caminho (como o roteador escolheria), ou `None`."""
        partial: str | None = None
        for route in self.router.routes:
            match, _ = route.matches(scope)
            path = getattr(route, "path", None)
            if match is Match.FULL:
                return path
            if match is Match.PARTIAL and partial is None:
                partial = path
        return partial

    async def _authenticate(self, scope: Scope) -> tokens.Principal | None:
        presented = bearer_credentials(scope)
        if presented is None:
            return None
        admin_token = self.context.settings.admin_token
        if tokens.is_bootstrap_secret(presented, admin_token):
            return tokens.BOOTSTRAP
        if not tokens.may_be_secret(presented):
            return None
        return await run_in_threadpool(self.context.run, tokens.authenticate, presented,
                                       admin_token=admin_token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method")
        logged_method = method if method in LOGGED_METHODS else OTHER_METHOD
        route: str | None = None
        response: dict[str, Any] = {"status": None}

        async def send_and_record(message: Message) -> None:
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            await send(message)

        try:
            route = self._route_template(scope)
            if _route_path(scope).startswith(AUTHENTICATED_PREFIX):
                principal = await self._authenticate(scope)
                if principal is None:
                    await error_response("unauthenticated")(scope, receive, send_and_record)
                    return
                scope[PRINCIPAL_SCOPE_KEY] = principal
            await self.app(scope, receive, send_and_record)
        except Exception as exc:
            logger.error("exception", exc_info=exc)
            if response["status"] is None:
                await error_response("internal")(scope, receive, send_and_record)
        finally:
            logger.info("request", extra={"event": "request", "method": logged_method, "route": route,
                                          "status": response["status"]})
