"""Estado compartilhado pelas rotas e pelo middleware de um app (`create_app`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import psycopg
from starlette.concurrency import run_in_threadpool

from cq_server.api.database import Database
from cq_server.bots import BotRuntime
from cq_server.clock import Clock
from cq_server.config import RepoSettings, Settings
from cq_server.doctor import DoctorSettings
from cq_server.ports.delivery import MessageFormatter

T = TypeVar("T")


@dataclass
class ApiContext:
    settings: Settings
    clock: Clock
    database: Database
    bots: BotRuntime | None = None
    # A lista de hosts de repositório permitidos no preparo.
    repo_settings: RepoSettings = field(default_factory=RepoSettings)
    # O formatador do cálculo do relatório (só o `plain`, o mesmo nos três
    # dialetos), montado na raiz de composição (`create_app`).
    report_formatter: MessageFormatter | None = None
    # Os limites do `doctor`, dados a `create_app` (que não lê o ambiente).
    doctor_settings: DoctorSettings = field(default_factory=DoctorSettings)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Valores literais trocados por `[segredo]` na redação (o token inicial configurado)."""
        return tuple(value for value in (self.settings.admin_token,) if value)

    def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Roda `fn(conn, *args, **kwargs)` com uma conexão do app (código síncrono do núcleo)."""
        with self.database.connection() as conn:
            return fn(conn, *args, **kwargs)

    async def call(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """`run` numa thread do pool, para não bloquear o laço de eventos."""
        return await run_in_threadpool(self.run, fn, *args, **kwargs)


def in_transaction(fn: Callable[..., T]) -> Callable[..., T]:
    """Envolve uma leitura do núcleo numa transação (leituras coerentes entre si)."""

    def wrapper(conn: psycopg.Connection, /, *args: Any, **kwargs: Any) -> T:
        with conn.transaction():
            return fn(conn, *args, **kwargs)

    return wrapper
