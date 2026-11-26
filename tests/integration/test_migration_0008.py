"""Migração `0008` (o banco de questões local) sobre o esquema da implantação e do piloto.


Cobre:
- o que a `0008` acrescenta ao esquema anterior, e só isso, medido por `apply_up_to(…, "0007")` e
  `apply_up_to(…, "0008") == ["0008"]` (`tests/helpers/db.py`): a tabela `question_banks` com as 13 colunas
  `(udt_name, anulável)`, `id` identity e chave primária, `bank_uuid` único, a unicidade `(name, version)`,
  nenhum padrão, nenhuma FK e nenhum gatilho; fora dela, só o `events_type_check` muda (recriado com o mesmo
  nome); nenhum índice, chave, FK ou gatilho de outra tabela muda;
- cada `CHECK` de `question_banks` recusando o valor fora da regra (o nome com 2 e com 41 caracteres, com
  maiúscula, começando por hífen; a versão com 63 hex e com maiúscula; a origem `zip`; `git` sem commit e
  `folder` com commit; o commit com 39 hex; `archive_bytes` diferente do tamanho do pacote, e o pacote vazio;
  0 e 20001 arquivos; os bytes fora de 0 a 100 MiB; `question_count` negativo e maior que `file_count`) e
  aceitando as bordas;
- a unicidade `(name, version)` e a de `bank_uuid`; o `id` gerado sempre pelo banco;
- o `events_type_check` aceitando exatamente os 49 tipos (os 48 anteriores e `bank.uploaded`) e recusando os
  vizinhos do novo;
- nenhuma linha anterior muda: um banco em `0007` com dados recebe a `0008`, cada tabela anterior fica igual em
  todas as colunas, `question_banks` nasce vazia, `schema_migrations` ganha a `0008`, e aplicar de novo não muda
  nada.

Os casos do esquema passam no esqueleto (a migração é real). O que não é esquema fica na mesma função de um caso
que falha no esqueleto: nenhum código do servidor faz `UPDATE`, `DELETE` ou `TRUNCATE` em `question_banks`
(varredura estática de `server/src/cq_server`), junto do envio pela API (`NotImplementedError`, visto como 500
`internal`, até a implementação da rota), que grava uma linha só e não a altera no reenvio.

As linhas de apoio são gravadas por SQL direto, com as colunas do esquema; horários fixos em UTC; ids internos
nunca como literal esperado. Os nomes de restrição conferidos são os que o Postgres dá às restrições da migração;
os das restrições de tabela sem nome são achados pela definição. Cada teste começa por `apply_up_to(…)`, chamado
no corpo do teste.
"""

from __future__ import annotations

import hashlib
import itertools
import pathlib
import re
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.types.json import Jsonb

from tests.helpers.db import apply_up_to

T0 = datetime(2031, 3, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVER_SRC = REPO_ROOT / "server" / "src" / "cq_server"

# Os 48 tipos anteriores, como literais (e não `EventType`), e o novo.
PREVIOUS_EVENT_TYPES = (
    "person.registered", "token.created", "token.revoked", "identity.revealed",
    "identity.reveal_denied", "task.failed", "outbox.failed",
    "offering.created", "offering.state_changed", "offering.archive_prepared", "bot.registered",
    "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed",
    "student.list_imported", "student.invites_prepared", "student.invites_queued",
    "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
    "student.invite_revoked", "student.joined",
    "dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
    "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
    "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know", "submission.graded",
    "submission.internal_failure", "offering.settings_changed",
    "report.frozen", "notice.recorded", "offering.digest_changed",
    "offering.purge_prepared", "offering.purged",
)
BANK_EVENT_TYPES = ("bank.uploaded",)
assert (len(PREVIOUS_EVENT_TYPES), len(set(PREVIOUS_EVENT_TYPES))) == (48, 48)
ALL_EVENT_TYPES = PREVIOUS_EVENT_TYPES + BANK_EVENT_TYPES

# As 30 tabelas do esquema da implantação; a `0008` acrescenta só `question_banks`.
DEPLOY_SCHEMA_TABLES = {
    "schema_migrations", "people", "identities", "api_tokens", "events", "tasks", "outbox_messages",
    "offerings", "bots", "enrollments", "invites", "delivery_endpoints", "telegram_updates", "invite_sends",
    "question_refs", "dispatches", "deliveries", "delivery_contents", "media_blobs", "submissions",
    "free_messages", "offering_contacts",
    "dispatch_reports", "team_notices", "team_notice_floors", "dispatch_previews",
    "dispatch_preview_contents", "offering_digests", "invite_batches", "mail_probe",
}

# As colunas de `question_banks`: (tipo em `udt_name`, anulável, identity).
BANK_COLUMNS: dict[str, tuple[str, bool, bool]] = {
    "id": ("int8", False, True),
    "bank_uuid": ("uuid", False, False),
    "name": ("text", False, False),
    "version": ("text", False, False),
    "source": ("text", False, False),
    "git_commit": ("text", True, False),
    "archive": ("bytea", False, False),
    "archive_bytes": ("int8", False, False),
    "file_count": ("int4", False, False),
    "byte_count": ("int8", False, False),
    "question_count": ("int4", False, False),
    "actor_token_id": ("uuid", True, False),
    "created_at": ("timestamptz", False, False),
}

# Os índices de `question_banks`: (chaves em ordem, único, primário).
BANK_INDEXES = {
    (("id",), True, True),
    (("bank_uuid",), True, False),
    (("name", "version"), True, False),
}

# As restrições de coluna com o nome que o Postgres dá; as de tabela são achadas pela definição.
NAME_CHECK = "question_banks_name_check"
VERSION_CHECK = "question_banks_version_check"
SOURCE_CHECK = "question_banks_source_check"
COMMIT_CHECK = "question_banks_git_commit_check"
FILE_COUNT_CHECK = "question_banks_file_count_check"
BYTE_COUNT_CHECK = "question_banks_byte_count_check"
NAME_VERSION_KEY = "question_banks_name_version_key"
BANK_UUID_KEY = "question_banks_bank_uuid_key"
# Fragmentos da definição (`pg_get_constraintdef`) que identificam cada restrição de tabela.
ARCHIVE_FRAGMENT = "octet_length(archive)"
QUESTION_COUNT_FRAGMENT = "question_count <= file_count"
SOURCE_COMMIT_FRAGMENT = "git_commit IS NOT NULL"

MAX_ARCHIVE_BYTES = 134_217_728
MAX_CONTENT_BYTES = 104_857_600
MAX_FILES = 20_000

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _hex(n: int, seed: str) -> str:
    """`n` caracteres hexadecimais minúsculos, montados em tempo de execução."""
    out = ""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).hexdigest()
        counter += 1
    return out[:n]


def _insert(conn: psycopg.Connection, table: str, key: str = "id", **values: Any) -> Any:
    """`INSERT` com as colunas dadas; devolve a chave gerada."""
    columns = list(values)
    query = pgsql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        pgsql.Identifier(table),
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
        pgsql.SQL(", ").join(pgsql.Placeholder() for _ in columns),
        pgsql.Identifier(key))
    return conn.execute(query, [values[c] for c in columns]).fetchone()[0]


def _bank_values(**overrides: Any) -> dict[str, Any]:
    """Uma linha válida de `question_banks` (origem `folder`), salvo `overrides`; o `archive_bytes` segue o
    pacote, a menos que seja dado."""
    n = next(_counter)
    archive = overrides.pop("archive", b"\x1f\x8b" + f"pacote-{n}".encode())
    values: dict[str, Any] = {
        "bank_uuid": uuid.uuid4(), "name": f"banco-teste-{n}", "version": _hex(64, f"versao-{n}"),
        "source": "folder", "git_commit": None, "archive": archive,
        "archive_bytes": 1 if archive is None else len(archive),
        "file_count": 3, "byte_count": 120, "question_count": 1, "actor_token_id": None, "created_at": T0,
    }
    values.update(overrides)
    return values


def _insert_bank(conn: psycopg.Connection, **overrides: Any) -> int:
    return _insert(conn, "question_banks", **_bank_values(**overrides))


def _refused(label: str, error: type[psycopg.Error], command: Callable[[], Any], *,
             constraint: str | None = None, table: str | None = None) -> psycopg.Error:
    """O comando tem de ser recusado com exatamente `error` (a conexão é autocommit: a recusa desfaz só o
    próprio comando), pela restrição `constraint` e na tabela `table`, quando dados; devolve a exceção.
    Tabela ou coluna ausente sobe como está."""
    try:
        command()
    except error as exc:
        if constraint is not None:
            assert exc.diag.constraint_name == constraint, (
                f"[{label}] recusado por {exc.diag.constraint_name!r}, e não por {constraint!r}")
        if table is not None:
            assert exc.diag.table_name == table, (
                f"[{label}] recusado na tabela {exc.diag.table_name!r}, e não em {table!r}")
        return exc
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise
    except psycopg.Error as exc:
        pytest.fail(f"[{label}] recusado com {type(exc).__name__}, e não com {error.__name__}")
    pytest.fail(f"[{label}] aceito, mas o esquema exige a recusa ({error.__name__})")


def _check_by_fragment(conn: psycopg.Connection, fragment: str) -> str:
    """O nome da única restrição `CHECK` de `question_banks` cuja definição contém `fragment`."""
    rows = conn.execute(
        "SELECT conname::text, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = 'question_banks'::regclass AND contype = 'c'").fetchall()
    names = [name for name, definition in rows if fragment in definition]
    assert len(names) == 1, (fragment, rows)
    return names[0]


def _columns(url: str) -> dict[tuple[str, str], tuple[str, bool, bool, str | None]]:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, udt_name, is_nullable, is_identity, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'").fetchall()
    return {(table, column): (udt, nullable == "YES", identity == "YES", default)
            for table, column, udt, nullable, identity, default in rows}


def _indexes(url: str) -> set[tuple[str, str, tuple[str, ...], bool, bool, str | None]]:
    """(tabela, índice, chaves em ordem, único, primário, predicado) de cada índice do esquema."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname::text, ic.relname::text, "
            "ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true) "
            "      FROM generate_series(1, i.indnkeyatts::int) AS k ORDER BY k), "
            "i.indisunique, i.indisprimary, pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'").fetchall()
    return {(table, index, tuple(keys), unique, primary, predicate)
            for table, index, keys, unique, primary, predicate in rows}


def _constraints(url: str) -> set[tuple[str, str, str, str]]:
    """(tabela, restrição, tipo, definição) das restrições do esquema `public`, menos as de `NOT NULL`
    (tipo `n` no Postgres 18), cuja nulidade é conferida pelas colunas."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.conrelid::regclass::text, c.conname::text, c.contype::text, "
            "pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' AND c.contype <> 'n'").fetchall()
    return {tuple(row) for row in rows}


def _triggers(url: str) -> set[tuple[str, str, int, str]]:
    """(tabela, gatilho, `tgtype`, `tgenabled`) dos gatilhos não internos."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT tgrelid::regclass::text, tgname::text, tgtype::int, tgenabled::text FROM pg_trigger "
            "WHERE NOT tgisinternal").fetchall()
    return {tuple(row) for row in rows}


def _table_columns(conn: psycopg.Connection) -> dict[str, tuple[str, ...]]:
    """As colunas de cada tabela do esquema `public`, na ordem da tabela."""
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "ORDER BY table_name, ordinal_position").fetchall()
    out: dict[str, list[str]] = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return {table: tuple(columns) for table, columns in out.items()}


def _snapshot(conn: psycopg.Connection, table: str, columns: tuple[str, ...]) -> list[tuple]:
    query = pgsql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns), pgsql.Identifier(table),
        pgsql.Identifier(columns[0]))
    return conn.execute(query).fetchall()


def _count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))).fetchone()[0]


def _migrated(url: str) -> None:
    """Banco vazio levado até a `0008` pelas migrações do pacote."""
    assert apply_up_to(url, "0008") == ["0001", "0002", "0003", "0004", "0005", "0006", "0007", "0008"]


# ---------------------------------------------------------------------------------------------
# O que a 0008 acrescenta (delta por `apply_up_to`)
# ---------------------------------------------------------------------------------------------


def test_0008_adds_only_question_banks_with_its_columns_and_keys(empty_db_url: str) -> None:
    """A `0008` acrescenta só a tabela `question_banks`: as 13 colunas exatas (tipo, nulidade e identity),
    nenhum padrão fora do `id` identity, a chave primária em `id`, `bank_uuid` único e a unicidade
    `(name, version)`; nenhuma coluna de outra tabela muda; nenhuma FK nem gatilho na tabela nova."""
    assert apply_up_to(empty_db_url, "0007") == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        before_tables = set(_table_columns(conn))
    assert before_tables == DEPLOY_SCHEMA_TABLES
    before_columns = _columns(empty_db_url)

    assert apply_up_to(empty_db_url, "0008") == ["0008"]

    after_columns = _columns(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert set(_table_columns(conn)) == DEPLOY_SCHEMA_TABLES | {"question_banks"}
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()[0] \
            == "0008"
    assert {key: value for key, value in after_columns.items() if key[0] != "question_banks"} == before_columns
    bank = {column: value for (table, column), value in after_columns.items() if table == "question_banks"}
    assert {column: value[:3] for column, value in bank.items()} == BANK_COLUMNS
    assert {column for column, value in bank.items() if value[3] is not None} == set()

    indexes = {(keys, unique, primary) for table, _, keys, unique, primary, predicate in _indexes(empty_db_url)
               if table == "question_banks" and predicate is None}
    assert indexes == BANK_INDEXES
    assert not any(table == "question_banks" and kind == "f" for table, _, kind, _ in _constraints(empty_db_url))
    assert not any(table == "question_banks" for table, *_ in _triggers(empty_db_url))


def test_0008_changes_nothing_else_than_question_banks_and_the_event_type_check(empty_db_url: str) -> None:
    """Fora de `question_banks`, a `0008` só recria o `events_type_check` (com o mesmo nome e a definição que
    passa a citar `bank.uploaded`): nenhuma outra restrição, índice ou gatilho de outra tabela muda."""
    apply_up_to(empty_db_url, "0007")
    constraints_before = _constraints(empty_db_url)
    indexes_before = _indexes(empty_db_url)
    triggers_before = _triggers(empty_db_url)

    assert apply_up_to(empty_db_url, "0008") == ["0008"]

    constraints_after = {row for row in _constraints(empty_db_url) if row[0] != "question_banks"}
    removed = constraints_before - constraints_after
    added = constraints_after - constraints_before
    assert {(table, name) for table, name, _, _ in removed} == {("events", "events_type_check")}
    assert {(table, name) for table, name, _, _ in added} == {("events", "events_type_check")}
    (new_definition,) = [definition for _, _, _, definition in added]
    (old_definition,) = [definition for _, _, _, definition in removed]
    assert "'bank.uploaded'" in new_definition and "'bank.uploaded'" not in old_definition
    assert {row for row in _indexes(empty_db_url) if row[0] != "question_banks"} == indexes_before
    assert _triggers(empty_db_url) == triggers_before


# ---------------------------------------------------------------------------------------------
# As restrições de `question_banks`
# ---------------------------------------------------------------------------------------------


def test_question_banks_checks_refuse_values_outside_the_rules(empty_db_url: str) -> None:
    """Cada `CHECK` recusa o valor fora da regra, pela restrição certa: o nome (2 e 41 caracteres,
    maiúscula, hífen no início, sublinhado, espaço), a versão (63 e 65 hex, maiúscula, fora do hex), a origem
    (`zip`, outra caixa), o commit só na origem `git` (sem commit e `folder` com commit) e na forma de 40 ou
    64 hex (39 hex, 41 hex, maiúscula), `archive_bytes` igual ao tamanho do pacote e de 1 a 128 MiB (diferente
    para mais e para menos, pacote vazio), `file_count` de 1 a 20000 (0 e 20001), `byte_count` de 0 a 100 MiB
    (-1 e um acima) e `question_count` de 0 a `file_count` (-1 e um acima); nenhuma linha fica."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        archive_check = _check_by_fragment(conn, ARCHIVE_FRAGMENT)
        question_check = _check_by_fragment(conn, QUESTION_COUNT_FRAGMENT)
        source_commit_check = _check_by_fragment(conn, SOURCE_COMMIT_FRAGMENT)
        assert len({archive_check, question_check, source_commit_check}) == 3
        commit40 = _hex(40, "commit")
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("name-2", {"name": "ab"}, NAME_CHECK),
            ("name-41", {"name": "a" * 41}, NAME_CHECK),
            ("name-upper", {"name": "Banco-teste"}, NAME_CHECK),
            ("name-hyphen-first", {"name": "-banco"}, NAME_CHECK),
            ("name-underscore", {"name": "banco_teste"}, NAME_CHECK),
            ("name-space", {"name": "banco teste"}, NAME_CHECK),
            ("name-empty", {"name": ""}, NAME_CHECK),
            ("version-63", {"version": _hex(63, "v")}, VERSION_CHECK),
            ("version-65", {"version": _hex(65, "v")}, VERSION_CHECK),
            ("version-upper", {"version": _hex(64, "v").upper()}, VERSION_CHECK),
            ("version-not-hex", {"version": "g" * 64}, VERSION_CHECK),
            ("source-zip", {"source": "zip"}, SOURCE_CHECK),
            ("source-case", {"source": "Folder"}, SOURCE_CHECK),
            ("git-without-commit", {"source": "git", "git_commit": None}, source_commit_check),
            ("folder-with-commit", {"source": "folder", "git_commit": commit40}, source_commit_check),
            ("commit-39", {"source": "git", "git_commit": _hex(39, "c")}, COMMIT_CHECK),
            ("commit-41", {"source": "git", "git_commit": _hex(41, "c")}, COMMIT_CHECK),
            ("commit-63", {"source": "git", "git_commit": _hex(63, "c")}, COMMIT_CHECK),
            ("commit-upper", {"source": "git", "git_commit": commit40.upper()}, COMMIT_CHECK),
            ("archive-bytes-more", {"archive": b"\x1f\x8b\x08\x00", "archive_bytes": 5}, archive_check),
            ("archive-bytes-less", {"archive": b"\x1f\x8b\x08\x00", "archive_bytes": 3}, archive_check),
            ("archive-empty", {"archive": b"", "archive_bytes": 0}, archive_check),
            ("files-0", {"file_count": 0, "question_count": 0}, FILE_COUNT_CHECK),
            ("files-20001", {"file_count": MAX_FILES + 1}, FILE_COUNT_CHECK),
            ("bytes-negative", {"byte_count": -1}, BYTE_COUNT_CHECK),
            ("bytes-above", {"byte_count": MAX_CONTENT_BYTES + 1}, BYTE_COUNT_CHECK),
            ("questions-negative", {"question_count": -1}, question_check),
            ("questions-above-files", {"file_count": 3, "question_count": 4}, question_check),
        ]
        for label, overrides, constraint in cases:
            _refused(label, psycopg.errors.CheckViolation,
                     lambda o=overrides: _insert_bank(conn, **o),
                     constraint=constraint, table="question_banks")
        for label, column in (("null-name", "name"), ("null-version", "version"), ("null-source", "source"),
                              ("null-archive", "archive"), ("null-archive-bytes", "archive_bytes"),
                              ("null-files", "file_count"), ("null-bytes", "byte_count"),
                              ("null-questions", "question_count"), ("null-uuid", "bank_uuid"),
                              ("null-created", "created_at")):
            _refused(label, psycopg.errors.NotNullViolation,
                     lambda c=column: _insert_bank(conn, **{c: None}), table="question_banks")
        assert _count(conn, "question_banks") == 0


def test_question_banks_checks_accept_the_edges(empty_db_url: str) -> None:
    """As bordas são aceitas: o nome com 3 e com 40 caracteres, só dígitos e com hífen depois do início; a
    origem `git` com commit de 40 e de 64 hex; `folder` sem commit; 1 e 20000 arquivos; 0 e 100 MiB de
    conteúdo; 0 questões e `question_count` igual a `file_count`; `archive_bytes` de 1 byte; o
    `actor_token_id` nulo e preenchido. O `id` é sempre do banco: um `id` dado é recusado."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        accepted = [
            {"name": "abc"},
            {"name": "a" * 40},
            {"name": "123"},
            {"name": "0a-b-c-"},
            {"source": "git", "git_commit": _hex(40, "commit-40")},
            {"source": "git", "git_commit": _hex(64, "commit-64")},
            {"source": "folder", "git_commit": None},
            {"file_count": 1, "question_count": 1},
            {"file_count": MAX_FILES, "question_count": MAX_FILES},
            {"byte_count": 0, "question_count": 0},
            {"byte_count": MAX_CONTENT_BYTES},
            {"archive": b"\x00"},
            {"actor_token_id": uuid.uuid4()},
        ]
        ids = [_insert_bank(conn, **overrides) for overrides in accepted]
        assert len(set(ids)) == len(accepted)
        assert _count(conn, "question_banks") == len(accepted)
        _refused("explicit-id", psycopg.errors.GeneratedAlways,
                 lambda: _insert(conn, "question_banks", id=max(ids) + 100, **_bank_values()))
        assert _count(conn, "question_banks") == len(accepted)


def test_question_banks_unique_name_and_version(empty_db_url: str) -> None:
    """`UNIQUE (name, version)`: a mesma versão do mesmo nome é recusada (outra origem, outro pacote, outro
    `bank_uuid`), e a primeira linha fica intacta; o mesmo nome com outra versão e outro nome com a mesma
    versão são aceitos; o mesmo `bank_uuid` é recusado."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        version = _hex(64, "unica")
        first = _bank_values(name="banco-unico", version=version)
        first_id = _insert(conn, "question_banks", **first)
        _refused("same-name-version", psycopg.errors.UniqueViolation,
                 lambda: _insert_bank(conn, name="banco-unico", version=version, source="git",
                                      git_commit=_hex(40, "outro"), archive=b"outro pacote"),
                 constraint=NAME_VERSION_KEY, table="question_banks")
        _insert_bank(conn, name="banco-unico", version=_hex(64, "outra"))
        _insert_bank(conn, name="banco-vizinho", version=version)
        _refused("same-uuid", psycopg.errors.UniqueViolation,
                 lambda: _insert_bank(conn, bank_uuid=first["bank_uuid"]),
                 constraint=BANK_UUID_KEY, table="question_banks")
        row = conn.execute(
            "SELECT bank_uuid, name, version, source, git_commit, archive FROM question_banks WHERE id = %s",
            (first_id,)).fetchone()
        assert row == (first["bank_uuid"], "banco-unico", version, "folder", None, first["archive"])
        assert _count(conn, "question_banks") == 3


def test_event_type_check_accepts_exactly_the_49_types(empty_db_url: str) -> None:
    """O `events_type_check` recriado com o mesmo nome e os 49 tipos: cada um dos 48 anteriores e
    `bank.uploaded` é aceito; um tipo vizinho do novo (outra caixa, singular e plural trocados, o nome da
    operação, um espaço no fim) é recusado por esse CHECK."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        refused_types = []
        for event_type in ALL_EVENT_TYPES:
            try:
                _insert(conn, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                        data=Jsonb({}))
            except psycopg.errors.CheckViolation:
                refused_types.append(event_type)
        assert refused_types == [], "tipos recusados pelo CHECK de eventos (o esquema exige os 49)"
        for label, event_type in (("other-case", "Bank.uploaded"), ("prefix", "bank.upload"),
                                  ("plural", "banks.uploaded"), ("operation", "banks.upload"),
                                  ("listing", "banks.list"), ("neighbor", "bank.listed"),
                                  ("trailing-space", "bank.uploaded ")):
            _refused(label, psycopg.errors.CheckViolation,
                     lambda t=event_type: _insert(conn, "events", key="seq", type=t, occurred_at=T0,
                                                  actor_token_id=None, data=Jsonb({})),
                     constraint="events_type_check", table="events")
        types = [row[0] for row in conn.execute("SELECT type FROM events ORDER BY seq").fetchall()]
    assert types == list(ALL_EVENT_TYPES)
    assert len(types) == 49


# ---------------------------------------------------------------------------------------------
# Migração sobre dados anteriores
# ---------------------------------------------------------------------------------------------


def _seed_deploy(conn: psycopg.Connection) -> None:
    """Dados anteriores ao banco de questões, por SQL direto: pessoas e identidade, um oferecimento com bot e
    aluno, um token, uma tarefa, a fila de saída, uma referência de questão, o último teste do servidor de
    e-mail e um evento de cada um dos 48 tipos anteriores."""
    people = [_insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"mig8{n:06x}", created_at=T0)
              for n in range(3)]
    _insert(conn, "identities", key="person_id", person_id=people[0], full_name="Pessoa Neutra",
            ra=None, email=None, email_normalized=None)
    offering = _insert(conn, "offerings", offering_id=uuid.uuid4(), code="oferta-migracao-oito",
                       course="Disciplina Neutra", term="2031s1", timezone="America/Sao_Paulo",
                       response_window_minutes=60, state="active", created_at=T0, activated_at=T0)
    _insert(conn, "bots", bot_uuid=uuid.uuid4(), offering_id=offering, telegram_bot_id=6_600_000_808,
                  username="cq_migracao_0008_bot", token_ciphertext=bytes([1]) + secrets.token_bytes(79),
                  token_version=1, state="registered", access_problem=None, profile_state="configured",
                  registered_at=T0)
    _insert(conn, "enrollments", person_id=people[1], offering_id=offering, role="student", state="active",
            created_at=T0, activated_at=T1, terms_version=1, terms_accepted_at=T1)
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token neutro",
            secret_sha256=hashlib.sha256(b"segredo neutro").hexdigest(), permissions=["offerings:read"],
            created_at=T0, revoked_at=None)
    _insert(conn, "tasks", task_type="demo.neutra", payload=Jsonb({}), state="pending", attempts=0,
            available_at=T1, created_at=T0)
    _insert(conn, "outbox_messages", recipient_person_id=people[0], text="mensagem neutra", state="pending",
            attempts=0, available_at=T0, created_at=T0)
    _insert(conn, "mail_probe", key="id", result="ok", checked_at=T1)
    for event_type in PREVIOUS_EVENT_TYPES:
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb({"neutro": True}))


def test_0008_over_deploy_database_preserves_rows(empty_db_url: str) -> None:
    """A migração só acrescenta: um banco em `0007` com dados recebe a `0008`
    (`apply_up_to(…, "0008") == ["0008"]`); cada linha das 30 tabelas do esquema anterior fica igual em todas
    as colunas (o `events_type_check` recriado aceita os 48 tipos já gravados), `question_banks` nasce vazia,
    `schema_migrations` ganha a `0008`, a sequência dos eventos continua, e aplicar de novo não muda nada."""
    assert apply_up_to(empty_db_url, "0007") == ["0001", "0002", "0003", "0004", "0005", "0006", "0007"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_deploy(conn)
        previous_columns = _table_columns(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in previous_columns.items()}
        counts = {table: _count(conn, table) for table in previous_columns}
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    assert set(previous_columns) == DEPLOY_SCHEMA_TABLES
    seeded = {"people", "identities", "offerings", "bots", "enrollments", "api_tokens", "tasks",
              "outbox_messages", "mail_probe", "events"}
    assert all(counts[table] > 0 for table in seeded), counts
    assert counts["events"] == 48

    assert apply_up_to(empty_db_url, "0008") == ["0008"]

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after_columns = _table_columns(conn)
        assert set(after_columns) == DEPLOY_SCHEMA_TABLES | {"question_banks"}
        assert {table: after_columns[table] for table in previous_columns} == previous_columns
        after = {table: _snapshot(conn, table, columns) for table, columns in previous_columns.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:7] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004", "0005",
                                                                  "0006", "0007", "0008"]
        assert _count(conn, "question_banks") == 0
        seq = _insert(conn, "events", key="seq", type="bank.uploaded", occurred_at=T2, actor_token_id=None,
                      data=Jsonb({"neutro": True}))
        assert seq > max_seq
        stable_columns = _table_columns(conn)
        stable = {table: _snapshot(conn, table, columns) for table, columns in stable_columns.items()}

    assert apply_up_to(empty_db_url, "0008") == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _table_columns(conn) == stable_columns
        assert {table: _snapshot(conn, table, columns)
                for table, columns in stable_columns.items()} == stable


# ---------------------------------------------------------------------------------------------
# A imutabilidade: o código do servidor e o envio pela API
# ---------------------------------------------------------------------------------------------

# Um comando que altera ou apaga linhas de `question_banks`, com ou sem aspas e esquema no nome; o
# `ON CONFLICT … DO UPDATE` num `INSERT` da tabela também conta.
_MUTATION = re.compile(
    r"\b(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?)\s+(?:ONLY\s+)?(?:\"?public\"?\s*\.\s*)?\"?question_banks\b",
    re.IGNORECASE)
_UPSERT = re.compile(r"INSERT\s+INTO\s+\"?question_banks\b[^;]*?\bDO\s+UPDATE\b", re.IGNORECASE | re.DOTALL)


def _server_mutations() -> tuple[list[str], int]:
    """Os trechos de `server/src/cq_server` (Python e SQL) que alteram ou apagam `question_banks`, e quantos arquivos
    citam a tabela."""
    found: list[str] = []
    citing = 0
    for path in sorted(SERVER_SRC.rglob("*")):
        if path.suffix not in {".py", ".sql"} or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "question_banks" in text:
            citing += 1
        for pattern in (_MUTATION, _UPSERT):
            for match in pattern.finditer(text):
                found.append(f"{path.relative_to(REPO_ROOT).as_posix()}: {match.group(0)[:80]!r}")
    return found, citing


def test_no_server_code_changes_question_banks_and_upload_keeps_the_first_row(
        client: Any, sql: psycopg.Connection, tmp_path: pathlib.Path) -> None:
    """Nenhum código do servidor faz `UPDATE`, `DELETE`, `TRUNCATE` ou `INSERT … DO UPDATE` em
    `question_banks` (varredura estática de `server/src/cq_server`, que acha a tabela ao menos na migração). Pela API, o
    envio de um banco válido grava uma linha com o pacote recebido e os números do conteúdo; o reenvio do
    mesmo conteúdo, de outra origem, responde `created: false` com a origem do primeiro envio e não toca a
    linha (o mesmo `xmin`, o mesmo pacote)."""
    mutations, citing = _server_mutations()
    assert citing >= 1, "a varredura não achou `question_banks` em `server/src/cq_server`"
    assert mutations == [], f"código do servidor que altera ou apaga question_banks: {mutations}"
    assert _MUTATION.search('UPDATE "question_banks" SET name = 1')
    assert _MUTATION.search("delete from public.question_banks")
    assert _UPSERT.search("INSERT INTO question_banks (a) VALUES (1) ON CONFLICT (name, version) DO UPDATE SET a=1")

    from tests.helpers import banks as bank_helpers
    from tests.helpers.api import ADMIN_TOKEN, auth

    course = tmp_path / "course"
    bank_helpers.copy_course(course)
    files = {path.relative_to(course).as_posix(): path.read_bytes()
             for path in sorted(course.rglob("*")) if path.is_file()}
    version = bank_helpers.reference_version(files)
    archive = bank_helpers.archive_of(files)
    questions = bank_helpers.reference_question_count(files)
    assert questions >= 1
    name = "banco-migracao"

    response = client.post("/v1/question-banks", headers=auth(ADMIN_TOKEN),
                           json=bank_helpers.upload_body(name, version, "folder", None, archive))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert (body["version"], body["source"], body["git_commit"]) == (version, "folder", None)
    assert body["file_count"] == len(files)
    assert body["byte_count"] == sum(len(data) for data in files.values())
    assert body["question_count"] == questions

    rows = sql.execute(
        "SELECT xmin::text, name, version, source, git_commit, archive, archive_bytes, file_count, byte_count, "
        "question_count FROM question_banks").fetchall()
    assert len(rows) == 1
    xmin, *stored = rows[0]
    assert stored[:5] == [name, version, "folder", None, archive]
    assert stored[5:8] == [len(archive), len(files), sum(len(data) for data in files.values())]
    assert stored[8] == body["question_count"]

    again = client.post("/v1/question-banks", headers=auth(ADMIN_TOKEN),
                        json=bank_helpers.upload_body(name, version, "git", _hex(40, "outra-origem"), archive))
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False
    assert (again.json()["source"], again.json()["git_commit"]) == ("folder", None)
    after = sql.execute(
        "SELECT xmin::text, name, version, source, git_commit, archive, archive_bytes, file_count, byte_count, "
        "question_count FROM question_banks").fetchall()
    assert after == rows
