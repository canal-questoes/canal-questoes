"""Helpers de banco das varreduras e da migração dos oferecimentos e bots.


- `scan_all_tables(conninfo)`: **todas** as colunas de **todas** as tabelas (inclusive as que
  as migrações futuras criarem), lidas por `row_to_json`, para as varreduras genéricas de identidade
  e de segredo. Uma lista fixa de colunas deixaria escapar uma coluna nova.
- `apply_up_to(conninfo, "0001")`: aplica só as migrações do pacote até a versão dada (com o registro
  em `schema_migrations`), para montar um banco "da API base" com dados e depois aplicar a `0002` pela
  função do sistema (`apply_migrations`), que não muda.

Não usam `cq_server.migrate` para decidir o que aplicar: leem os arquivos do pacote
`cq_server.migrations` diretamente, como o executor faria.
"""

from __future__ import annotations

import importlib.resources
import json
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql

MIGRATIONS_PACKAGE = "cq_server.migrations"
_MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def _tables(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(esquema, tabela) das tabelas comuns e das temporárias **desta** sessão, em ordem."""
    rows = conn.execute(
        """
        SELECT table_schema, table_name
          FROM information_schema.tables
         WHERE table_type IN ('BASE TABLE', 'LOCAL TEMPORARY')
           AND table_schema NOT IN ('pg_catalog', 'information_schema')
         ORDER BY table_name, table_schema
        """).fetchall()
    return [(schema, name) for schema, name in rows]


def _bytea_columns(conn: psycopg.Connection, schema: str, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s AND data_type = 'bytea'
         ORDER BY ordinal_position
        """, (schema, table)).fetchall()
    return [row[0] for row in rows]


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _scan(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for schema, table in _tables(conn):
        ident = sql.Identifier(schema, table)
        # O apelido não pode colidir com uma coluna (numa tabela com coluna `t`, `row_to_json(t)`
        # receberia a coluna).
        query = sql.SQL(
            "SELECT row_to_json(cq_scan_row)::text FROM {} AS cq_scan_row ORDER BY 1").format(ident)
        for (row_json,) in conn.execute(query).fetchall():
            for column, value in json.loads(row_json).items():
                found.append((table, column, _as_text(value)))
        # `bytea`: além do hex de `row_to_json` (`\x…`), as formas `escape` e `base64`.
        for column in _bytea_columns(conn, schema, table):
            col = sql.Identifier(column)
            query = sql.SQL(
                "SELECT encode({col}, 'escape'), encode({col}, 'base64') FROM {tbl} "
                "WHERE {col} IS NOT NULL ORDER BY 1").format(col=col, tbl=ident)
            for escaped, b64 in conn.execute(query).fetchall():
                found.append((table, column, escaped))
                found.append((table, column, b64.replace("\n", "")))
    return found


def scan_all_tables(conninfo: str | psycopg.Connection) -> list[tuple[str, str, str]]:
    """Devolve `(tabela, coluna, texto)` para cada coluna de cada linha de cada tabela.

    O texto é o valor da coluna em `row_to_json` (o texto cru, se for texto; senão o JSON do valor,
    ex.: `jsonb` como objeto, `bytea` como `"\\\\x…"`); as colunas `bytea` rendem também as formas
    `escape` e `base64`. Com uma conexão aberta no lugar do `conninfo`, a varredura roda nela e vê
    também as tabelas temporárias dessa sessão (controle positivo da varredura).
    """
    if isinstance(conninfo, psycopg.Connection):
        return _scan(conninfo)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        return _scan(conn)


def packaged_migration_files() -> list[tuple[str, str]]:
    """(versão, SQL) das migrações do pacote, em ordem lexical do nome do arquivo."""
    root = importlib.resources.files(MIGRATIONS_PACKAGE)
    entries = sorted((e for e in root.iterdir() if e.is_file() and _MIGRATION_FILE.fullmatch(e.name)),
                     key=lambda e: e.name)
    out: list[tuple[str, str]] = []
    for entry in entries:
        match = _MIGRATION_FILE.fullmatch(entry.name)
        assert match is not None
        out.append((match.group(1), entry.read_text(encoding="utf-8")))
    return out


def apply_up_to(conninfo: str, version: str) -> list[str]:
    """Aplica, num banco vazio (ou já em versão anterior), as migrações do pacote até `version`
    inclusive, cada uma numa transação com o `INSERT` em `schema_migrations`; devolve as versões
    aplicadas. Não aplica nada depois de `version`; versão inexistente no pacote é erro do teste."""
    migrations = packaged_migration_files()
    assert version in {v for v, _ in migrations}, f"migração {version} não existe no pacote"
    applied: list[str] = []
    with psycopg.connect(conninfo, autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
        done: set[str] = set()
        if exists and exists[0]:
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for number, text in migrations:
            if number > version:
                break
            if number in done:
                continue
            with conn.transaction():
                conn.execute(text)  # type: ignore[arg-type]
                conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                             (number, datetime.now(timezone.utc)))
            applied.append(number)
    return applied
