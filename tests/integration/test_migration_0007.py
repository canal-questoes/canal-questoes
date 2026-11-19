"""Migração `0007` (a implantação e o piloto) sobre o esquema do CLI e da operação por agente.


Cobre:
- a `0007` sobre a `0006` acrescenta **só** a tabela `mail_probe` (o conjunto de tabelas antes e depois, medido
  por `apply_up_to(…, "0006")` e `apply_up_to(…, "0007") == ["0007"]`, de `tests/helpers/db.py`), com as 3 colunas
  `(udt_name, anulável)`, a chave primária em `id` e o padrão de `id` igual a `true`
  (`information_schema.columns.column_default`; as colunas e a chave também são conferidas em
  `test_migrations.py`);
- os `CHECK`s recusam `id = false` e um `result` fora de `ok`, `connection`, `tls`, `auth`, `not_configured`, e
  aceitam cada um dos cinco; `result` e `checked_at` não aceitam nulo;
- uma segunda linha é impossível (`PRIMARY KEY` com `DEFAULT true CHECK (id)`), e `INSERT … ON CONFLICT (id) DO
  UPDATE` mantém uma linha só, com os valores novos.

Os casos do esquema passam no esqueleto (a migração é real).
O que não é esquema fica na mesma função do caso de uma linha: `cq_server.mail_probe.record_probe` e
`read_probe` (`NotImplementedError` até a implementação), gravando duas vezes e lendo a última.

Horários fixos em UTC. Cada teste começa por `apply_up_to(…, "0007")` (ou `"0006"`), chamado no corpo do teste.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from tests.helpers.db import apply_up_to

T0 = datetime(2031, 3, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=6)

RESULTS = ("ok", "connection", "tls", "auth", "not_configured")

# as colunas `(udt_name, anulável)`.
MAIL_PROBE_COLUMNS = {"id": ("bool", False), "result": ("text", False), "checked_at": ("timestamptz", False)}


def _tables(conn: psycopg.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE'").fetchall()}


def _count(conn: psycopg.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM mail_probe").fetchone()[0])


def test_0007_adds_only_mail_probe_with_its_columns_key_and_default(empty_db_url: str) -> None:
    """A `0007` acrescenta só `mail_probe` (nenhuma outra tabela muda de conjunto), com as 3 colunas exatas, a chave
    primária em `id` e o padrão `true`."""
    assert apply_up_to(empty_db_url, "0006")
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        before = _tables(conn)
    assert "mail_probe" not in before
    assert apply_up_to(empty_db_url, "0007") == ["0007"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _tables(conn) == before | {"mail_probe"}
        columns = {name: (udt, nullable == "YES") for name, udt, nullable in conn.execute(
            "SELECT column_name, udt_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'mail_probe'").fetchall()}
        assert columns == MAIL_PROBE_COLUMNS
        default = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'mail_probe' AND column_name = 'id'").fetchone()[0]
        assert default == "true"
        others = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
            "AND table_name = 'mail_probe' AND column_default IS NOT NULL AND column_name <> 'id'").fetchall()
        assert others == []
        key = conn.execute(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'mail_probe'::regclass AND i.indisprimary").fetchall()
        assert [row[0] for row in key] == ["id"]
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()[0] \
            == "0007"


def test_mail_probe_checks(empty_db_url: str) -> None:
    """Os `CHECK`s: `id = false` e um `result` fora do conjunto recusados; os cinco resultados aceitos; `result` e
    `checked_at` nulos recusados."""
    apply_up_to(empty_db_url, "0007")
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO mail_probe (id, result, checked_at) VALUES (false, 'ok', %s)", (T0,))
        for bad in ("OK", "smtp", "", "unknown", "never", "stale"):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s)", (bad, T0))
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES (NULL, %s)", (T0,))
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES ('ok', NULL)")
        with pytest.raises(psycopg.errors.NotNullViolation):
            conn.execute("INSERT INTO mail_probe (id, result, checked_at) VALUES (NULL, 'ok', %s)", (T0,))
        assert _count(conn) == 0
        for result in RESULTS:
            conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s)", (result, T0))
            assert conn.execute("SELECT id, result FROM mail_probe").fetchall() == [(True, result)]
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("UPDATE mail_probe SET result = 'smtp'")
            conn.execute("DELETE FROM mail_probe")


def test_mail_probe_holds_at_most_one_row(empty_db_url: str) -> None:
    """Uma linha no máximo: a segunda inserção é recusada pela chave; `INSERT … ON CONFLICT (id) DO UPDATE` mantém
    uma linha com os valores novos. Falha no esqueleto: `record_probe` duas vezes e `read_probe`
    com o último `(result, checked_at)`, sempre uma linha."""
    apply_up_to(empty_db_url, "0007")
    upsert = ("INSERT INTO mail_probe (result, checked_at) VALUES (%s, %s) "
              "ON CONFLICT (id) DO UPDATE SET result = EXCLUDED.result, checked_at = EXCLUDED.checked_at")
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES ('ok', %s)", (T0,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO mail_probe (result, checked_at) VALUES ('tls', %s)", (T1,))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO mail_probe (id, result, checked_at) VALUES (true, 'tls', %s)", (T1,))
        conn.execute(upsert, ("auth", T1))
        conn.execute(upsert, ("connection", T1 + timedelta(minutes=1)))
        assert conn.execute("SELECT id, result, checked_at FROM mail_probe").fetchall() == [
            (True, "connection", T1 + timedelta(minutes=1))]
        conn.execute("DELETE FROM mail_probe")

    # O caso vermelho da mesma função: a gravação e a leitura do núcleo.
    from cq_server.mail_probe import read_probe, record_probe

    with psycopg.connect(empty_db_url) as conn:
        assert read_probe(conn) is None
        record_probe(conn, "tls", T0)
        conn.commit()
        record_probe(conn, "ok", T1)
        conn.commit()
        assert read_probe(conn) == ("ok", T1)
        assert _count(conn) == 1
