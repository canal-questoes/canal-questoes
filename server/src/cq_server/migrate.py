"""Executor de migrações SQL versionadas.

- As migrações são arquivos `NNNN_<slug>.sql` do pacote `cq_server.migrations`, lidos com
  `importlib.resources` em ordem lexical; a versão é o prefixo `NNNN`.
- Uma conexão dedicada toma um lock consultivo de **sessão** com chave fixa: dois executores ao
  mesmo tempo (ex.: api e worker subindo juntos) se serializam, e o segundo vê as versões já
  aplicadas pelo primeiro.
- Cada migração roda numa transação própria, com `lock_timeout`, junto com o `INSERT` em
  `schema_migrations`: uma migração que falha no meio não deixa nada (DDL transacional).
- As funções abrem e fecham a própria conexão, com `connect_timeout` de 3 s (`cq_server.db`), e não
  deixam conexões abertas ao retornar.
"""

from __future__ import annotations

import importlib.resources
import re
from dataclasses import dataclass

import psycopg

from cq_server import db
from cq_server.clock import SystemClock

EXPECTED_VERSION = "0007"

MIGRATIONS_PACKAGE = "cq_server.migrations"
MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# Chave fixa do lock consultivo de sessão das migrações (a do log de eventos, na migração 0001, é
# outra: 6371000002).
MIGRATION_LOCK_KEY = 6371000001
LOCK_TIMEOUT = "5s"


class MigrationError(Exception):
    """Conjunto de migrações empacotadas inconsistente (ex.: versão repetida)."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str


def packaged_migrations() -> list[Migration]:
    """As migrações empacotadas, em ordem lexical do nome do arquivo."""
    root = importlib.resources.files(MIGRATIONS_PACKAGE)
    entries = sorted(
        (entry for entry in root.iterdir() if entry.is_file() and MIGRATION_FILE.fullmatch(entry.name)),
        key=lambda entry: entry.name,
    )
    migrations: list[Migration] = []
    seen: set[str] = set()
    for entry in entries:
        match = MIGRATION_FILE.fullmatch(entry.name)
        assert match is not None
        version = match.group(1)
        if version in seen:
            raise MigrationError(f"versão de migração repetida: {version}")
        seen.add(version)
        migrations.append(Migration(version=version, name=entry.name,
                                    sql=entry.read_text(encoding="utf-8")))
    return migrations


def _has_migrations_table(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    return bool(row and row[0])


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    if not _has_migrations_table(conn):
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def apply_migrations(conninfo: str) -> list[str]:
    """Aplica as migrações que faltam e devolve as versões aplicadas nesta chamada, em ordem.

    Idempotente: sem nada a aplicar, devolve `[]` e não muda o banco.
    """
    migrations = packaged_migrations()
    applied_now: list[str] = []
    with db.connect(conninfo, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            applied = _applied_versions(conn)
            for migration in migrations:
                if migration.version in applied:
                    continue
                with conn.transaction():
                    conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                    # Várias instruções, sem parâmetros: o psycopg envia o texto como está.
                    conn.execute(migration.sql)  # type: ignore[arg-type]
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                        (migration.version, SystemClock().now()),
                    )
                applied_now.append(migration.version)
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
            except psycopg.Error:
                # Conexão quebrada: o lock de sessão cai com ela ao fechar.
                pass
    return applied_now


def current_version(conninfo: str) -> str | None:
    """A maior versão aplicada, ou `None` se nenhuma (banco sem `schema_migrations` ou vazio)."""
    with db.connect(conninfo, autocommit=True) as conn:
        if not _has_migrations_table(conn):
            return None
        row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return row[0] if row else None
