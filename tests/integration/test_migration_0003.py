"""Migração `0003` (vínculo do aluno) sobre o esquema de oferecimentos e bots.

Cobre o esquema do vínculo do aluno:
- a versão esperada `0003` e o arquivo `0003_student_enrollment.sql` no pacote;
- o que a `0003` acrescenta ao esquema anterior, e só isso: a tabela `invite_sends` e as colunas novas de
  `enrollments`, `invites` e `offerings`, com tipos, nulidade, identity e padrão exatos; os índices não
  únicos `invite_sends (enrollment_id)`, `invites (invite_send_id)` e `telegram_updates (bot_id,
  chat_id)` (nenhum índice único novo além da chave primária); os CHECKs só nas tabelas sem identidade
  (`enrollments_terms_check`, `invites_presentation_check`, `offerings_invite_code_check`, os de
  `invite_sends` e o `events_type_check` recriado), nenhum em `people`, `identities`,
  `delivery_endpoints` ou `telegram_updates`; a chave primária e as 3 FKs novas;
- a `0003` aplicada sobre um banco em `0002` com dados de oferecimentos e bots (`tests/helpers/db.py::apply_up_to`)
  preserva cada linha e as sequências, com as colunas novas nulas;
- os CHECKs novos aceitando e recusando linhas-limite (inclusive, em `failed`, `error_type` nulo se e
  só se `failure = not_configured`, e `cancel_reason` com `superseded`); as FKs novas; e
  o CHECK de tipos de evento com os 29 tipos.

O esquema exato de colunas, chaves primárias e FKs também é conferido em `test_migrations.py`, e a
troca da versão nos testes de oferecimentos e bots é conferida em `test_migration_0002.py`. As linhas de
apoio são gravadas por SQL direto, com as colunas do esquema de cada migração; horários fixos em
UTC; ids internos nunca como literal.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import itertools
import os
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg import sql as pgsql
from psycopg.types.json import Jsonb

from cq_server.migrate import EXPECTED_VERSION, apply_migrations, current_version
from tests.helpers.db import apply_up_to

T0 = datetime(2031, 3, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)

# Os 7 tipos da API base, os 14 de oferecimentos e bots e os 8 do vínculo do aluno, como literais (e não
# `EventType`).
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
STUDENT_LINK_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                  "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                  "student.invite_revoked", "student.joined")
assert len(CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_LINK_EVENT_TYPES) == 29

# Esquema do vínculo do aluno: (tabela, coluna) -> (tipo em `udt_name`, anulável, identity, padrão), para
# cada coluna que a `0003` acrescenta. `int8` = bigint, `int4` = integer.
STUDENT_LINK_NEW_COLUMNS: dict[tuple[str, str], tuple[str, bool, bool, str | None]] = {
    # `enrollments`
    ("enrollments", "terms_version"): ("int4", True, False, None),
    ("enrollments", "terms_accepted_at"): ("timestamptz", True, False, None),
    # `invites`
    ("invites", "invite_send_id"): ("int8", True, False, None),
    ("invites", "presented_update_id"): ("int8", True, False, None),
    ("invites", "presented_terms_version"): ("int4", True, False, None),
    # `invite_sends` (nova)
    ("invite_sends", "id"): ("int8", False, True, None),
    ("invite_sends", "enrollment_id"): ("int8", False, False, None),
    ("invite_sends", "origin"): ("text", False, False, None),
    ("invite_sends", "valid_hours"): ("int4", False, False, None),
    ("invite_sends", "state"): ("text", False, False, None),
    ("invite_sends", "failure"): ("text", True, False, None),
    ("invite_sends", "cancel_reason"): ("text", True, False, None),
    ("invite_sends", "error_type"): ("text", True, False, None),
    ("invite_sends", "attempts"): ("int4", False, False, "0"),
    ("invite_sends", "created_at"): ("timestamptz", False, False, None),
    ("invite_sends", "finished_at"): ("timestamptz", True, False, None),
    # `offerings`
    ("offerings", "invite_code_sha256"): ("text", True, False, None),
    ("offerings", "invite_code_expires_at"): ("timestamptz", True, False, None),
    ("offerings", "invite_valid_hours"): ("int4", True, False, None),
    ("offerings", "invite_eligible_count"): ("int4", True, False, None),
}

# Esquema do vínculo do aluno: os índices que a `0003` cria, por (tabela, colunas-chave em
# ordem, único, primário, parcial), e os nomes dos três não únicos.
STUDENT_LINK_NEW_INDEXES = {
    ("invite_sends", ("id",), True, True, False),
    ("invite_sends", ("enrollment_id",), False, False, False),
    ("invites", ("invite_send_id",), False, False, False),
    ("telegram_updates", ("bot_id", "chat_id"), False, False, False),
}
STUDENT_LINK_INDEX_NAMES = {"invite_sends_enrollment_idx", "invites_invite_send_idx",
                  "telegram_updates_bot_chat_idx"}

# As tabelas que guardam identidade não recebem CHECK (como antes).
IDENTITY_TABLES = {"people", "identities", "delivery_endpoints", "telegram_updates"}

# As colunas da API base e de oferecimentos e bots, na ordem da chave de cada tabela: as
# linhas antigas têm de ficar iguais nelas depois da `0003`.
OFFERING_BOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "schema_migrations": ("version", "applied_at"),
    "people": ("id", "pseudo_id", "alias", "created_at"),
    "identities": ("person_id", "full_name", "ra", "email", "email_normalized"),
    "api_tokens": ("id", "token_id", "label", "secret_sha256", "permissions", "created_at",
                   "revoked_at"),
    "events": ("seq", "type", "occurred_at", "actor_token_id", "data"),
    "tasks": ("id", "task_type", "payload", "idempotency_key", "state", "attempts", "available_at",
              "lease_owner", "lease_until", "last_error", "created_at", "finished_at"),
    "outbox_messages": ("id", "recipient_person_id", "text", "idempotency_key", "state", "attempts",
                        "available_at", "lease_owner", "lease_until", "channel_ref", "last_error",
                        "created_at", "sent_at", "bot_id", "delivery_endpoint_id",
                        "reply_to_update_id"),
    "offerings": ("id", "offering_id", "code", "course", "term", "timezone",
                  "response_window_minutes", "state", "created_at", "activated_at", "closed_at",
                  "archived_at", "archive_code_sha256", "archive_code_expires_at"),
    "bots": ("id", "bot_uuid", "offering_id", "telegram_bot_id", "username", "token_ciphertext",
             "token_version", "state", "access_problem", "profile_state", "lease_owner",
             "lease_until", "last_poll_ok_at", "last_problem", "last_problem_at", "update_epoch",
             "last_update_at", "registered_at", "token_replaced_at"),
    "enrollments": ("id", "person_id", "offering_id", "role", "state", "created_at",
                    "activated_at"),
    "invites": ("id", "enrollment_id", "code_sha256", "state", "expires_at", "created_at",
                "used_at", "revoked_at"),
    "delivery_endpoints": ("id", "person_id", "bot_id", "channel", "chat_id", "state",
                           "created_at", "changed_at"),
    "telegram_updates": ("id", "bot_id", "update_epoch", "update_id", "kind", "chat_id",
                         "chat_type", "sender_id", "text", "command", "details", "received_at",
                         "state", "outcome", "processed_at"),
}

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _insert(conn: psycopg.Connection, table: str, key: str = "id", **values: Any) -> Any:
    """`INSERT` com as colunas dadas; devolve a chave gerada."""
    columns = list(values)
    query = pgsql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
        pgsql.Identifier(table),
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
        pgsql.SQL(", ").join(pgsql.Placeholder() for _ in columns),
        pgsql.Identifier(key))
    return conn.execute(query, [values[c] for c in columns]).fetchone()[0]


def _snapshot(conn: psycopg.Connection, table: str, columns: tuple[str, ...]) -> list[tuple]:
    query = pgsql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns), pgsql.Identifier(table),
        pgsql.Identifier(columns[0]))
    return conn.execute(query).fetchall()


def _refused(label: str, error: type[psycopg.Error], insert: Callable[[], Any], *,
             constraint: str | None = None, prefix: str | None = None,
             table: str | None = None) -> None:
    """O `INSERT` tem de ser recusado com exatamente `error` (a conexão é autocommit: a recusa desfaz
    só o próprio comando), pela restrição `constraint` (nome exato declarado na migração) ou por uma
    restrição cujo nome começa com `prefix`, e na tabela `table`, quando dados. Tabela ou coluna
    ausente (esquema anterior à `0003`) sobe como está."""
    try:
        insert()
    except error as exc:
        name = exc.diag.constraint_name
        if constraint is not None:
            assert name == constraint, f"[{label}] recusado por {name!r}, e não por {constraint!r}"
        if prefix is not None:
            assert name is not None and name.startswith(prefix), (
                f"[{label}] recusado por {name!r}, fora das restrições {prefix}*")
        if table is not None:
            assert exc.diag.table_name == table, (
                f"[{label}] recusado na tabela {exc.diag.table_name!r}, e não em {table!r}")
        return
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise
    except psycopg.Error as exc:
        pytest.fail(f"[{label}] recusado com {type(exc).__name__}, e não com {error.__name__}")
    pytest.fail(f"[{label}] aceito, mas o esquema do vínculo do aluno exige a recusa ({error.__name__})")


def _person(conn: psycopg.Connection) -> int:
    n = next(_counter)
    return _insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"abd{n:09x}", created_at=T0)


def _offering(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": uuid.uuid4(), "code": f"oferta-migracao-{n}", "course": "Disciplina Neutra",
        "term": "2031s1", "timezone": "America/Sao_Paulo", "response_window_minutes": 1440,
        "state": "draft", "created_at": T0, "activated_at": None, "closed_at": None,
        "archived_at": None, "archive_code_sha256": None, "archive_code_expires_at": None,
    }
    values.update(overrides)
    return _insert(conn, "offerings", **values)


def _active_offering(conn: psycopg.Connection, **overrides: Any) -> int:
    return _offering(conn, state="active", activated_at=T1, **overrides)


def _blob(length: int) -> bytes:
    """Blob com a forma do formato v1: versão 1 + bytes aleatórios."""
    return bytes([1]) + os.urandom(length - 1)


def _bot(conn: psycopg.Connection, offering_id: int, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "bot_uuid": uuid.uuid4(), "offering_id": offering_id, "telegram_bot_id": 6_300_000_000 + n,
        "username": f"cq_migracao_{n:04d}_bot", "token_ciphertext": _blob(80), "token_version": 1,
        "state": "registered", "access_problem": None, "profile_state": "pending",
        "lease_owner": None, "lease_until": None, "registered_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "bots", **values)


def _enrollment(conn: psycopg.Connection, person_id: int, offering_id: int, *, role: str = "student",
                state: str = "pending", **extra: Any) -> int:
    values: dict[str, Any] = {"person_id": person_id, "offering_id": offering_id, "role": role,
                              "state": state, "created_at": T0,
                              "activated_at": T1 if state == "active" else None}
    values.update(extra)
    return _insert(conn, "enrollments", **values)


def _invite(conn: psycopg.Connection, enrollment_id: int, *, state: str = "pending",
            **extra: Any) -> int:
    return _insert(conn, "invites", enrollment_id=enrollment_id,
                   code_sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), state=state,
                   expires_at=T0 + timedelta(days=7), created_at=T0,
                   used_at=T1 if state == "used" else None,
                   revoked_at=T1 if state == "revoked" else None, **extra)


def _update(conn: psycopg.Connection, bot_id: int, *, chat_id: int | None = None) -> int:
    n = next(_counter)
    chat = chat_id if chat_id is not None else 5_200_000_000 + n
    return _insert(conn, "telegram_updates", bot_id=bot_id, update_epoch=0, update_id=820_000_000 + n,
                   kind="message", chat_id=chat, chat_type="private", sender_id=chat,
                   text="/ajuda", command="/ajuda",
                   details=Jsonb({"message_id": 1, "date": 1930564800}), received_at=T0,
                   state="processed", outcome="help", processed_at=T0)


# O envio coerente em cada estado.
_SEND_BY_STATE: dict[str, dict[str, Any]] = {
    "pending": {},
    "sent": {"attempts": 1, "finished_at": T1},
    "failed": {"attempts": 1, "finished_at": T1, "failure": "recipient_refused",
               "error_type": "SMTPRecipientsRefused"},
    "cancelled": {"finished_at": T1, "cancel_reason": "superseded"},
}


def _invite_send(conn: psycopg.Connection, enrollment_id: int, base: str = "pending", /,
                 **overrides: Any) -> int:
    """Um envio coerente no estado `base`, com as colunas de `overrides` trocadas (inclusive o
    próprio `state`)."""
    values: dict[str, Any] = {"origin": "batch", "valid_hours": 168, "state": base, "attempts": 0,
                              "failure": None, "cancel_reason": None, "error_type": None,
                              "created_at": T0, "finished_at": None}
    values.update(_SEND_BY_STATE[base])
    values.update(overrides)
    return _insert(conn, "invite_sends", enrollment_id=enrollment_id, **values)


def _columns(url: str) -> dict[tuple[str, str], tuple[str, bool, bool, str | None]]:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, udt_name, is_nullable, is_identity, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'").fetchall()
    return {(table, column): (udt, nullable == "YES", identity == "YES", default)
            for table, column, udt, nullable, identity, default in rows}


def _indexes(url: str) -> set[tuple[str, str, tuple[str, ...], bool, bool, bool, int]]:
    """(tabela, índice, colunas-chave em ordem, único, primário, parcial, nº de chaves)."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname::text, ic.relname::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "      WHERE k.ord <= i.indnkeyatts ORDER BY k.ord), "
            "i.indisunique, i.indisprimary, i.indpred IS NOT NULL, i.indnkeyatts "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'").fetchall()
    return {(table, index, tuple(cols), unique, primary, partial, nkeys)
            for table, index, cols, unique, primary, partial, nkeys in rows}


def _constraints(url: str) -> set[tuple[str, str, str, str]]:
    """(tabela, restrição, tipo, definição) das restrições do esquema `public`, menos as de `NOT
    NULL` (tipo `n` no Postgres 18), cuja nulidade é conferida pelas colunas."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.conrelid::regclass::text, c.conname::text, c.contype::text, "
            "pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE n.nspname = 'public' AND c.contype <> 'n'").fetchall()
    return {tuple(row) for row in rows}


# ---------------------------------------------------------------------------------------------
# Versão e o que a 0003 acrescenta
# ---------------------------------------------------------------------------------------------


def test_expected_version_and_migration_files_are_packaged() -> None:
    """As mudanças no banco entram como uma nova migração versionada, aplicada como na API base:
    migração nova `0003_student_enrollment.sql` (vínculo do aluno);
    migração nova `0004_dispatch_response.sql` (disparo); migração
    nova `0005_reports_notices.sql` (relatórios); migração nova
    `0006_cli_purge.sql` (CLI); migração nova `0007_mail_probe.sql` (implantação;
    `EXPECTED_VERSION = "0007"`): a versão esperada é `0007`, e o pacote tem exatamente os sete
    arquivos, em ordem."""
    # A versão do esquema é literal global; cada migração tem o seu arquivo no pacote:
    # a `0004` (disparo e resposta), com o arquivo `0004_dispatch_response.sql`;
    # a `0005` (relatórios e acompanhamento),
    # com o arquivo `0005_reports_notices.sql`;
    # a `0006` (CLI),
    # com o arquivo `0006_cli_purge.sql`;
    # e a `0007` (implantação e piloto), a versão atual,
    # com o arquivo `0007_mail_probe.sql`.
    assert EXPECTED_VERSION == "0007"
    root = importlib.resources.files("cq_server.migrations")
    names = sorted(entry.name for entry in root.iterdir()
                   if entry.is_file() and entry.name.endswith(".sql"))
    assert names == ["0001_initial.sql", "0002_offerings_bots.sql", "0003_student_enrollment.sql",
                     "0004_dispatch_response.sql", "0005_reports_notices.sql", "0006_cli_purge.sql",
                     "0007_mail_probe.sql"]


def test_0003_adds_exactly_the_new_table_and_columns(empty_db_url: str) -> None:
    """Esquema do vínculo do aluno (colunas novas anuláveis de `enrollments`, `invites` e
    `offerings`; a tabela `invite_sends` com `id` identity, `attempts` com padrão 0 e as demais
    colunas com a nulidade da tabela): num banco em `0002`, a `0003` devolve `["0003"]`, acrescenta
    exatamente as colunas de `STUDENT_LINK_NEW_COLUMNS` (tipo, nulidade, identity e padrão exatos) e a tabela
    `invite_sends`, e não muda nem remove nenhuma coluna já existente."""
    assert apply_up_to(empty_db_url, "0002") == ["0001", "0002"]
    before = _columns(empty_db_url)
    # Só até a `0003`, para o delta medido não incluir a `0004`.
    assert apply_up_to(empty_db_url, "0003") == ["0003"]
    after = _columns(empty_db_url)

    assert {key: after.get(key) for key in before} == before
    added = {key: value for key, value in after.items() if key not in before}
    assert added == STUDENT_LINK_NEW_COLUMNS
    assert {table for table, _ in after} - {table for table, _ in before} == {"invite_sends"}


def test_0003_adds_indexes_and_checks_only_where_declared(empty_db_url: str) -> None:
    """Esquema do vínculo do aluno (nenhum CHECK em tabela que guarda identidade):
    índices não únicos `invite_sends_enrollment_idx (enrollment_id)`, `invites_invite_send_idx
    (invite_send_id)` e `telegram_updates_bot_chat_idx (bot_id, chat_id)`,
    mais a chave primária de `invite_sends`, e nenhum outro índice (nenhum único novo, nenhum
    removido); CHECKs novos só `enrollments_terms_check`, `invites_presentation_check`,
    `offerings_invite_code_check` e os de `invite_sends` (nomes `invite_sends_*`), e o
    `events_type_check` recriado, o único removido; nenhum CHECK em `people`, `identities`,
    `delivery_endpoints` e `telegram_updates`; de resto, só a chave primária e as 3 FKs novas (a de
    `invite_sends` e as duas de `invites`)."""
    assert apply_up_to(empty_db_url, "0002") == ["0001", "0002"]
    indexes_before = _indexes(empty_db_url)
    constraints_before = _constraints(empty_db_url)
    # Só até a `0003`, para o delta medido não incluir a `0004`.
    assert apply_up_to(empty_db_url, "0003") == ["0003"]
    indexes_after = _indexes(empty_db_url)
    constraints_after = _constraints(empty_db_url)

    assert indexes_before - indexes_after == set()
    new_indexes = indexes_after - indexes_before
    assert all(len(cols) == nkeys for _, _, cols, _, _, _, nkeys in new_indexes), new_indexes
    assert {(table, cols, unique, primary, partial)
            for table, _, cols, unique, primary, partial, _ in new_indexes} == STUDENT_LINK_NEW_INDEXES
    assert {name for _, name, _, _, primary, _, _ in new_indexes if not primary} == STUDENT_LINK_INDEX_NAMES

    removed = constraints_before - constraints_after
    added = constraints_after - constraints_before
    assert {(table, name, kind) for table, name, kind, _ in removed} == {
        ("events", "events_type_check", "c")}
    added_checks = {(table, name) for table, name, kind, _ in added if kind == "c"}
    assert {entry for entry in added_checks if entry[0] != "invite_sends"} == {
        ("enrollments", "enrollments_terms_check"), ("invites", "invites_presentation_check"),
        ("offerings", "offerings_invite_code_check"), ("events", "events_type_check")}
    send_checks = {name for table, name in added_checks if table == "invite_sends"}
    assert send_checks and all(name.startswith("invite_sends_") for name in send_checks), send_checks
    assert {table for table, _, kind, _ in added | removed if kind == "c"} & IDENTITY_TABLES == set()
    assert sorted((table, kind) for table, _, kind, _ in added if kind != "c") == [
        ("invite_sends", "f"), ("invite_sends", "p"), ("invites", "f"), ("invites", "f")]


# ---------------------------------------------------------------------------------------------
# Migração sobre dados de oferecimentos e bots
# ---------------------------------------------------------------------------------------------


def _seed_offering_bot(conn: psycopg.Connection) -> None:
    """Dados de oferecimentos e bots por SQL direto, nas 13 tabelas da API base e de oferecimentos e bots, com estados variados (até aqui só
    há matrículas de equipe)."""
    people = [_person(conn) for _ in range(4)]
    _insert(conn, "identities", key="person_id", person_id=people[0],
            full_name="Pessoa Migrada Quatro", ra="5500000204",
            email="pessoa.quatro@exemplo.invalid", email_normalized="pessoa.quatro@exemplo.invalid")
    _insert(conn, "identities", key="person_id", person_id=people[1],
            full_name="Pessoa Migrada Cinco", ra=None, email=None, email_normalized=None)
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token antigo",
            secret_sha256=hashlib.sha256(b"segredo antigo").hexdigest(),
            permissions=["offerings:read", "offerings:write"], created_at=T0, revoked_at=None)

    draft = _offering(conn, archive_code_sha256=hashlib.sha256(b"arquivar").hexdigest(),
                      archive_code_expires_at=T1)
    active = _active_offering(conn)
    closed = _offering(conn, state="closed", activated_at=T1, closed_at=T2)
    _offering(conn, state="archived", activated_at=T1, closed_at=T2, archived_at=T3)
    bot = _bot(conn, active, lease_owner="worker-0003", lease_until=T2, last_poll_ok_at=T1,
               last_update_at=T1, update_epoch=1)
    _bot(conn, closed, state="no_access", access_problem="token_rejected", profile_state="failed",
         last_problem="rate_limited", last_problem_at=T1, token_replaced_at=T2)
    _bot(conn, draft, profile_state="configured")

    pending = _enrollment(conn, people[0], active, role="team", state="pending")
    linked = _enrollment(conn, people[1], active, role="team", state="active")
    _enrollment(conn, people[2], closed, role="team", state="pending")
    _invite(conn, pending, state="pending")
    _invite(conn, pending, state="revoked")
    _invite(conn, linked, state="used")
    endpoint = _insert(conn, "delivery_endpoints", person_id=people[1], bot_id=bot,
                       channel="telegram", chat_id=5_200_000_001, state="active", created_at=T1,
                       changed_at=T1)
    _insert(conn, "delivery_endpoints", person_id=people[1], bot_id=bot, channel="telegram",
            chat_id=5_200_000_002, state="inactive", created_at=T0, changed_at=T1)
    processed = _update(conn, bot, chat_id=5_200_000_001)
    _insert(conn, "telegram_updates", bot_id=bot, update_epoch=1, update_id=820_900_000,
            kind="message", chat_id=5_200_000_003, chat_type="private", sender_id=5_200_000_003,
            text="/start", command="/start", details=Jsonb({"message_id": 2, "date": 1930564801}),
            received_at=T1, state="pending", outcome=None, processed_at=None)

    _insert(conn, "outbox_messages", recipient_person_id=people[0], text="mensagem antiga",
            state="pending", attempts=0, available_at=T0, created_at=T0)
    _insert(conn, "outbox_messages", recipient_person_id=people[1], bot_id=bot,
            delivery_endpoint_id=endpoint, text="aviso da equipe", state="sent", attempts=1,
            available_at=T0, channel_ref="telegram:1", created_at=T0, sent_at=T1)
    _insert(conn, "outbox_messages", bot_id=bot, reply_to_update_id=processed, text="resposta",
            idempotency_key="reply:migrada", state="failed", attempts=3, available_at=T0,
            last_error="Forbidden", created_at=T0)

    for event_type in ("person.registered", "offering.created", "team.joined", "outbox.reply_failed"):
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb({"origem": "migracao"}))
    for state, lease, finished in (("pending", None, None), ("leased", "worker-0003", None),
                                   ("done", None, T2)):
        _insert(conn, "tasks", task_type="telegram.update", payload=Jsonb({"estado": state}),
                idempotency_key=f"migrada-0003:{state}", state=state,
                attempts=0 if state == "pending" else 1, available_at=T1, lease_owner=lease,
                lease_until=T2 if lease else None, last_error=None, created_at=T0,
                finished_at=finished)


def test_0003_over_offering_bot_database_preserves_rows_and_sequences(empty_db_url: str) -> None:
    """Nova migração versionada, aplicada como na API base; colunas novas anuláveis
    (para os dados anteriores): um banco em `0002` (só `0001` e `0002`
    aplicadas) com dados de oferecimentos e bots recebe a `0003` pela função do sistema, que devolve `["0003", "0004",
    "0005", "0006", "0007"]` (a `0004` do disparo, a `0005` dos relatórios, a `0006` do CLI
    e a `0007` da implantação); cada linha já existente fica igual em todas as colunas que já tinha,
    as colunas novas ficam nulas nas linhas antigas, `invite_sends` nasce vazia,
    `schema_migrations` ganha a `0003`, a `0004`, a `0005`, a `0006` e a `0007`, as sequências
    continuam depois do maior id e aplicar de novo não muda nada."""
    assert apply_up_to(empty_db_url, "0002") == ["0001", "0002"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_offering_bot(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in OFFERING_BOT_COLUMNS.items()}
        max_ids = {table: conn.execute(pgsql.SQL("SELECT max(id) FROM {}").format(
            pgsql.Identifier(table))).fetchone()[0]
            for table in ("offerings", "enrollments", "invites", "telegram_updates")}
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    assert current_version(empty_db_url) == "0002"

    # A função do sistema aplica também as migrações seguintes:
    # a `0004` (disparo), a `0005` (relatórios),
    # a `0006` (CLI)
    # e a `0007` (implantação).
    assert apply_migrations(empty_db_url) == ["0003", "0004", "0005", "0006", "0007"]
    assert current_version(empty_db_url) == "0007"

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after = {table: _snapshot(conn, table, columns) for table, columns in OFFERING_BOT_COLUMNS.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:2] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004",
                                                                  "0005", "0006", "0007"]
        assert conn.execute(
            "SELECT terms_version, terms_accepted_at FROM enrollments ORDER BY id").fetchall() == [
            (None, None)] * 3
        assert conn.execute(
            "SELECT invite_send_id, presented_update_id, presented_terms_version FROM invites "
            "ORDER BY id").fetchall() == [(None, None, None)] * 3
        assert conn.execute(
            "SELECT invite_code_sha256, invite_code_expires_at, invite_valid_hours, "
            "invite_eligible_count FROM offerings ORDER BY id").fetchall() == [
            (None, None, None, None)] * 4
        assert conn.execute("SELECT count(*) FROM invite_sends").fetchone()[0] == 0

        # As sequências continuam: o próximo id de cada tabela estendida vem depois do maior antigo.
        offering = _active_offering(conn)
        assert offering > max_ids["offerings"]
        enrollment = _enrollment(conn, _person(conn), offering)
        assert enrollment > max_ids["enrollments"]
        send = _invite_send(conn, enrollment)
        invite = _invite(conn, enrollment, invite_send_id=send)
        assert invite > max_ids["invites"]
        update = _update(conn, _bot(conn, offering))
        assert update > max_ids["telegram_updates"]
        seq = _insert(conn, "events", key="seq", type="student.joined", occurred_at=T3,
                      actor_token_id=None, data=Jsonb({"origem": "migracao"}))
        assert seq > max_seq
        stable = {table: _snapshot(conn, table, columns) for table, columns in OFFERING_BOT_COLUMNS.items()}
        stable_sends = conn.execute("SELECT * FROM invite_sends ORDER BY id").fetchall()

    assert apply_migrations(empty_db_url) == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert {table: _snapshot(conn, table, columns)
                for table, columns in OFFERING_BOT_COLUMNS.items()} == stable
        assert conn.execute("SELECT * FROM invite_sends ORDER BY id").fetchall() == stable_sends


# ---------------------------------------------------------------------------------------------
# CHECK da matrícula
# ---------------------------------------------------------------------------------------------


def test_enrollment_terms_check(sql: psycopg.Connection) -> None:
    """O aceite grava a versão do termo e o horário
    (`enrollments_terms_check`: equipe com `terms_version` e `terms_accepted_at` nulos; aluno
    `pending` com os dois nulos; aluno `active` com `terms_version >= 1` e `terms_accepted_at`
    preenchido): as formas coerentes são aceitas (inclusive a versão 2 com um aceite posterior ao
    `activated_at`, a troca de conta); cada forma incoerente é recusada por esse CHECK."""
    offering = _active_offering(sql)
    accepted = [
        ("team", "pending", {}),
        ("team", "active", {}),
        ("student", "pending", {}),
        ("student", "active", {"terms_version": 1, "terms_accepted_at": T1}),
        ("student", "active", {"terms_version": 2, "terms_accepted_at": T2}),
    ]
    for role, state, extra in accepted:
        _enrollment(sql, _person(sql), offering, role=role, state=state, **extra)
    terms = {"terms_version": 1, "terms_accepted_at": T1}
    refused = [
        ("team-pending-with-terms", "team", "pending", terms),
        ("team-active-with-terms", "team", "active", terms),
        ("team-active-with-version-only", "team", "active", {"terms_version": 1}),
        ("team-pending-with-accepted_at-only", "team", "pending", {"terms_accepted_at": T1}),
        ("student-pending-with-terms", "student", "pending", terms),
        ("student-pending-with-version-only", "student", "pending", {"terms_version": 1}),
        ("student-pending-with-accepted_at-only", "student", "pending", {"terms_accepted_at": T1}),
        ("student-active-without-terms", "student", "active", {}),
        ("student-active-without-accepted_at", "student", "active", {"terms_version": 1}),
        ("student-active-without-version", "student", "active", {"terms_accepted_at": T1}),
        ("student-active-version-0", "student", "active",
         {"terms_version": 0, "terms_accepted_at": T1}),
        ("student-active-version-negative", "student", "active",
         {"terms_version": -1, "terms_accepted_at": T1}),
    ]
    for label, role, state, extra in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda r=role, s=state, e=extra: _enrollment(sql, _person(sql), offering, role=r,
                                                              state=s, **e),
                 constraint="enrollments_terms_check")
    assert sql.execute("SELECT role, state, terms_version, terms_accepted_at FROM enrollments "
                       "ORDER BY id").fetchall() == [
        ("team", "pending", None, None), ("team", "active", None, None),
        ("student", "pending", None, None), ("student", "active", 1, T1),
        ("student", "active", 2, T2)]


# ---------------------------------------------------------------------------------------------
# CHECK da apresentação do termo
# ---------------------------------------------------------------------------------------------


def test_invite_presentation_check(sql: psycopg.Connection) -> None:
    """O termo apresentado fica amarrado ao update e à versão
    (`invites_presentation_check`: `presented_update_id` e `presented_terms_version` nulos juntos;
    `presented_terms_version >= 1` quando preenchida; `presented_update_id` só com `invite_send_id`
    preenchido): o convite de equipe de oferecimentos e bots (tudo nulo), o de aluno não apresentado e o apresentado
    (versões 1 e 2, inclusive já usado) são aceitos; cada forma incoerente é recusada por esse
    CHECK."""
    offering = _active_offering(sql)
    update = _update(sql, _bot(sql, offering))

    def student_invite(state: str = "pending", **extra: Any) -> int:
        enrollment = _enrollment(sql, _person(sql), offering)
        return _invite(sql, enrollment, state=state, invite_send_id=_invite_send(sql, enrollment),
                       **extra)

    team = _invite(sql, _enrollment(sql, _person(sql), offering, role="team"))
    not_presented = student_invite()
    presented_v1 = student_invite(presented_update_id=update, presented_terms_version=1)
    presented_v2 = student_invite(presented_update_id=update, presented_terms_version=2)
    used = student_invite("used", presented_update_id=update, presented_terms_version=1)

    refused: list[tuple[str, Callable[[], Any]]] = [
        ("update-without-version", lambda: student_invite(presented_update_id=update)),
        ("version-without-update", lambda: student_invite(presented_terms_version=1)),
        ("version-0", lambda: student_invite(presented_update_id=update, presented_terms_version=0)),
        ("version-negative",
         lambda: student_invite(presented_update_id=update, presented_terms_version=-1)),
        ("presented-without-send",
         lambda: _invite(sql, _enrollment(sql, _person(sql), offering),
                         presented_update_id=update, presented_terms_version=1)),
        ("team-presented",
         lambda: _invite(sql, _enrollment(sql, _person(sql), offering, role="team"),
                         presented_update_id=update, presented_terms_version=1)),
    ]
    for label, insert in refused:
        _refused(label, psycopg.errors.CheckViolation, insert,
                 constraint="invites_presentation_check")
    rows = sql.execute("SELECT id, invite_send_id IS NOT NULL, presented_update_id, "
                       "presented_terms_version, state FROM invites ORDER BY id").fetchall()
    assert rows == [(team, False, None, None, "pending"), (not_presented, True, None, None, "pending"),
                    (presented_v1, True, update, 1, "pending"),
                    (presented_v2, True, update, 2, "pending"), (used, True, update, 1, "used")]


# ---------------------------------------------------------------------------------------------
# CHECK do preparo do envio
# ---------------------------------------------------------------------------------------------


def test_offering_invite_code_check(sql: psycopg.Connection) -> None:
    """Envio em duas fases, com o código de confirmação guardado só como resumo e com
    validade (`offerings_invite_code_check`: as quatro colunas nulas juntas;
    preenchidas só com `state = 'active'`, `invite_valid_hours` de 1 a 720 e `invite_eligible_count >=
    0`): sem preparo, em qualquer estado, e o preparo em `active` com as linhas-limite (1 e 720 horas,
    0 elegíveis) são aceitos; o preparo em `draft`, `closed` e `archived`, com alguma das quatro
    faltando, ou fora dos limites, é recusado por esse CHECK."""
    digest = hashlib.sha256(b"codigo do envio").hexdigest()
    prepared = {"invite_code_sha256": digest, "invite_code_expires_at": T1 + timedelta(minutes=10),
                "invite_valid_hours": 168, "invite_eligible_count": 0}
    active = {"state": "active", "activated_at": T1}
    closed = {"state": "closed", "activated_at": T1, "closed_at": T2}
    accepted = [
        {"state": "draft"},
        active,
        closed,
        {"state": "archived", "archived_at": T3},
        {**active, **prepared},
        {**active, **prepared, "invite_valid_hours": 1, "invite_eligible_count": 5},
        {**active, **prepared, "invite_valid_hours": 720, "invite_eligible_count": 1000},
    ]
    ids = [_offering(sql, **columns) for columns in accepted]

    def without(key: str) -> dict[str, Any]:
        return {**active, **{k: v for k, v in prepared.items() if k != key}}

    refused = [
        ("draft-prepared", {"state": "draft", **prepared}),
        ("closed-prepared", {**closed, **prepared}),
        ("archived-prepared", {"state": "archived", "archived_at": T3, **prepared}),
        ("archived-from-closed-prepared", {**closed, "state": "archived", "archived_at": T3,
                                           **prepared}),
        ("active-without-digest", without("invite_code_sha256")),
        ("active-without-expires_at", without("invite_code_expires_at")),
        ("active-without-valid-hours", without("invite_valid_hours")),
        ("active-without-eligible-count", without("invite_eligible_count")),
        ("active-digest-only", {**active, "invite_code_sha256": digest}),
        ("active-valid-hours-0", {**active, **prepared, "invite_valid_hours": 0}),
        ("active-valid-hours-721", {**active, **prepared, "invite_valid_hours": 721}),
        ("active-eligible-count-negative", {**active, **prepared, "invite_eligible_count": -1}),
    ]
    for label, columns in refused:
        _refused(label, psycopg.errors.CheckViolation, lambda c=columns: _offering(sql, **c),
                 constraint="offerings_invite_code_check")
    assert sql.execute(
        "SELECT id, state, invite_code_sha256, invite_valid_hours, invite_eligible_count "
        "FROM offerings ORDER BY id").fetchall() == [
        (ids[0], "draft", None, None, None), (ids[1], "active", None, None, None),
        (ids[2], "closed", None, None, None), (ids[3], "archived", None, None, None),
        (ids[4], "active", digest, 168, 0), (ids[5], "active", digest, 1, 5),
        (ids[6], "active", digest, 720, 1000)]


# ---------------------------------------------------------------------------------------------
# CHECKs de `invite_sends`
# ---------------------------------------------------------------------------------------------


def test_invite_sends_value_sets_and_ranges(sql: psycopg.Connection) -> None:
    """O envio e o seu desfecho, com envios concorrentes da mesma matrícula (`origin` em
    `batch` · `coordination` · `self_service`; `state` em `pending` · `sent` · `failed` · `cancelled`;
    `valid_hours` de 1 a 720; `attempts >= 0`, com padrão 0; `failure` em `recipient_refused` ·
    `auth_refused` · `server_refused` · `not_configured` · `attempts_exhausted`; `cancel_reason` em
    `offering_inactive` · `already_linked` · `superseded`): cada valor dos conjuntos e as
    linhas-limite são aceitos; valor fora dos conjuntos (inclusive em outra caixa) e fora dos limites
    é recusado por um CHECK `invite_sends_*`."""
    enrollment = _enrollment(sql, _person(sql), _active_offering(sql))
    default_attempts = _insert(sql, "invite_sends", enrollment_id=enrollment, origin="batch",
                               valid_hours=168, state="pending", created_at=T0)
    accepted = [
        ("pending", {"origin": "coordination"}),
        ("pending", {"origin": "self_service"}),
        ("pending", {"valid_hours": 1}),
        ("pending", {"valid_hours": 720}),
        ("pending", {"attempts": 2}),
        ("failed", {"failure": "recipient_refused", "error_type": "SMTPRecipientsRefused"}),
        ("failed", {"failure": "auth_refused", "error_type": "SMTPAuthenticationError"}),
        ("failed", {"failure": "server_refused", "error_type": "SMTPDataError"}),
        ("failed", {"failure": "attempts_exhausted", "error_type": "SmtpDeadline", "attempts": 3}),
        ("failed", {"failure": "not_configured", "error_type": None, "attempts": 0}),
        ("cancelled", {"cancel_reason": "offering_inactive", "attempts": 0}),
        ("cancelled", {"cancel_reason": "already_linked", "attempts": 1}),
        ("cancelled", {"cancel_reason": "superseded", "attempts": 0}),
    ]
    ids = [_invite_send(sql, enrollment, state, **extra) for state, extra in accepted]
    refused = [
        ("origin-unknown", "pending", {"origin": "api"}),
        ("origin-other-case", "pending", {"origin": "Batch"}),
        ("origin-empty", "pending", {"origin": ""}),
        ("state-unknown", "pending", {"state": "queued"}),
        ("state-other-case", "pending", {"state": "Pending"}),
        ("valid-hours-0", "pending", {"valid_hours": 0}),
        ("valid-hours-721", "pending", {"valid_hours": 721}),
        ("valid-hours-negative", "pending", {"valid_hours": -1}),
        ("attempts-negative", "pending", {"attempts": -1}),
        ("failure-unknown", "failed", {"failure": "timeout", "error_type": "TimeoutError"}),
        ("failure-other-case", "failed", {"failure": "Recipient_Refused"}),
        ("cancel-reason-unknown", "cancelled", {"cancel_reason": "manual"}),
        ("cancel-reason-other-case", "cancelled", {"cancel_reason": "Superseded"}),
    ]
    for label, state, extra in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda s=state, e=extra: _invite_send(sql, enrollment, s, **e),
                 prefix="invite_sends_")
    rows = sql.execute("SELECT id, origin, valid_hours, state, attempts, failure, cancel_reason, "
                       "error_type FROM invite_sends ORDER BY id").fetchall()
    assert rows == [
        (default_attempts, "batch", 168, "pending", 0, None, None, None),
        (ids[0], "coordination", 168, "pending", 0, None, None, None),
        (ids[1], "self_service", 168, "pending", 0, None, None, None),
        (ids[2], "batch", 1, "pending", 0, None, None, None),
        (ids[3], "batch", 720, "pending", 0, None, None, None),
        (ids[4], "batch", 168, "pending", 2, None, None, None),
        (ids[5], "batch", 168, "failed", 1, "recipient_refused", None, "SMTPRecipientsRefused"),
        (ids[6], "batch", 168, "failed", 1, "auth_refused", None, "SMTPAuthenticationError"),
        (ids[7], "batch", 168, "failed", 1, "server_refused", None, "SMTPDataError"),
        (ids[8], "batch", 168, "failed", 3, "attempts_exhausted", None, "SmtpDeadline"),
        (ids[9], "batch", 168, "failed", 0, "not_configured", None, None),
        (ids[10], "batch", 168, "cancelled", 0, None, "offering_inactive", None),
        (ids[11], "batch", 168, "cancelled", 1, None, "already_linked", None),
        (ids[12], "batch", 168, "cancelled", 0, None, "superseded", None),
    ]


def test_invite_sends_state_coherence(sql: psycopg.Connection) -> None:
    """Esquema do envio (coerência de `state` com `failure`, `cancel_reason`,
    `error_type` e `finished_at`: `pending` com os quatro nulos; `sent` com `finished_at` e `attempts
    >= 1`, o resto nulo; `failed` com `failure` e `finished_at`, `cancel_reason` nulo; `cancelled` com
    `cancel_reason` e `finished_at`, `failure` e `error_type` nulos; `finished_at` preenchido se e só
    se `state` ≠ `pending`): uma linha coerente de cada estado é aceita; cada mistura é recusada por
    um CHECK `invite_sends_*`."""
    enrollment = _enrollment(sql, _person(sql), _active_offering(sql))
    ids = {state: _invite_send(sql, enrollment, state) for state in _SEND_BY_STATE}
    refused = [
        ("pending-finished", "pending", {"finished_at": T1}),
        ("pending-with-failure", "pending",
         {"failure": "recipient_refused", "error_type": "SMTPRecipientsRefused"}),
        ("pending-with-not-configured", "pending", {"failure": "not_configured"}),
        ("pending-with-cancel-reason", "pending", {"cancel_reason": "superseded"}),
        ("pending-with-error-type", "pending", {"error_type": "SMTPDataError"}),
        ("sent-attempts-0", "sent", {"attempts": 0}),
        ("sent-unfinished", "sent", {"finished_at": None}),
        ("sent-with-failure", "sent",
         {"failure": "recipient_refused", "error_type": "SMTPRecipientsRefused"}),
        ("sent-with-cancel-reason", "sent", {"cancel_reason": "superseded"}),
        ("sent-with-error-type", "sent", {"error_type": "SMTPDataError"}),
        ("failed-without-failure", "failed", {"failure": None}),
        ("failed-without-failure-and-error-type", "failed", {"failure": None, "error_type": None}),
        ("failed-unfinished", "failed", {"finished_at": None}),
        ("failed-with-cancel-reason", "failed", {"cancel_reason": "superseded"}),
        ("cancelled-without-reason", "cancelled", {"cancel_reason": None}),
        ("cancelled-unfinished", "cancelled", {"finished_at": None}),
        ("cancelled-with-failure", "cancelled", {"failure": "not_configured"}),
        ("cancelled-with-error-type", "cancelled", {"error_type": "SMTPDataError"}),
    ]
    for label, state, extra in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda s=state, e=extra: _invite_send(sql, enrollment, s, **e),
                 prefix="invite_sends_")
    assert sql.execute("SELECT id, state, attempts, failure, cancel_reason, error_type, finished_at "
                       "FROM invite_sends ORDER BY id").fetchall() == [
        (ids["pending"], "pending", 0, None, None, None, None),
        (ids["sent"], "sent", 1, None, None, None, T1),
        (ids["failed"], "failed", 1, "recipient_refused", None, "SMTPRecipientsRefused", T1),
        (ids["cancelled"], "cancelled", 0, None, "superseded", None, T1),
    ]


def test_invite_sends_error_type_null_iff_not_configured(sql: psycopg.Connection) -> None:
    """Sem e-mail configurado, o envio falha de vez como `not_configured`;
    tipo do erro (`error_type`: nome do tipo da exceção, `^[A-Za-z_][A-Za-z0-9_]{0,99}$`,
    nunca o texto; em `failed`, nulo se e só se `failure = 'not_configured'`; em `attempts_exhausted`,
    o da última `TemporaryFailure`; `not_configured` guarda os códigos gerados antes, 0 ou mais);
    CHECK de identificador: `not_configured` sem tipo (com 0 e com 1 código
    gerado) e as demais falhas com tipo (inclusive as linhas-limite `_` e 100 caracteres) são
    aceitas; `not_configured` com tipo, as demais falhas sem tipo e um tipo fora do padrão de
    identificador (espaço, dígito no início, 101 caracteres, vazio, fora do ASCII, ponto) são
    recusados por um CHECK `invite_sends_*`."""
    enrollment = _enrollment(sql, _person(sql), _active_offering(sql))
    longest = "A" + "b" * 99
    accepted = [
        {"failure": "not_configured", "error_type": None, "attempts": 0},
        {"failure": "not_configured", "error_type": None, "attempts": 1},
        {"failure": "recipient_refused", "error_type": "EmailSyntaxError", "attempts": 1},
        {"failure": "attempts_exhausted", "error_type": "SmtpDeadline", "attempts": 5},
        {"failure": "server_refused", "error_type": "_"},
        {"failure": "auth_refused", "error_type": longest},
    ]
    ids = [_invite_send(sql, enrollment, "failed", **extra) for extra in accepted]
    refused = [
        ("not-configured-with-type", {"failure": "not_configured",
                                      "error_type": "SMTPServerDisconnected"}),
        ("recipient-refused-without-type", {"failure": "recipient_refused", "error_type": None}),
        ("auth-refused-without-type", {"failure": "auth_refused", "error_type": None}),
        ("server-refused-without-type", {"failure": "server_refused", "error_type": None}),
        ("attempts-exhausted-without-type", {"failure": "attempts_exhausted", "error_type": None}),
        ("type-with-space", {"failure": "server_refused", "error_type": "SMTP Error"}),
        ("type-starting-with-digit", {"failure": "server_refused", "error_type": "1Error"}),
        ("type-with-101-characters", {"failure": "server_refused", "error_type": longest + "c"}),
        ("type-empty", {"failure": "server_refused", "error_type": ""}),
        ("type-non-ascii", {"failure": "server_refused", "error_type": "ErroÇ"}),
        ("type-with-dot", {"failure": "server_refused", "error_type": "smtplib.SMTPDataError"}),
    ]
    for label, extra in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda e=extra: _invite_send(sql, enrollment, "failed", **e),
                 prefix="invite_sends_")
    assert sql.execute("SELECT id, failure, error_type, attempts FROM invite_sends "
                       "ORDER BY id").fetchall() == [
        (ids[0], "not_configured", None, 0), (ids[1], "not_configured", None, 1),
        (ids[2], "recipient_refused", "EmailSyntaxError", 1),
        (ids[3], "attempts_exhausted", "SmtpDeadline", 5), (ids[4], "server_refused", "_", 1),
        (ids[5], "auth_refused", longest, 1)]


# ---------------------------------------------------------------------------------------------
# FKs novas
# ---------------------------------------------------------------------------------------------


def test_new_foreign_keys_refuse_missing_rows(sql: psycopg.Connection) -> None:
    """O envio é de uma matrícula; o convite de aluno nasce de um envio
    (FKs `invite_sends.enrollment_id → enrollments.id`, `invites.invite_send_id →
    invite_sends.id` e `invites.presented_update_id → telegram_updates.id`): com as linhas
    existentes, o envio e o convite apresentado são aceitos; um envio de matrícula inexistente, e um
    convite de envio ou de update inexistentes, são recusados com `ForeignKeyViolation` na tabela
    certa."""
    offering = _active_offering(sql)
    update = _update(sql, _bot(sql, offering))
    enrollment = _enrollment(sql, _person(sql), offering)
    send = _invite_send(sql, enrollment)
    invite = _invite(sql, enrollment, invite_send_id=send, presented_update_id=update,
                     presented_terms_version=1)
    missing_enrollment = sql.execute("SELECT max(id) + 1000 FROM enrollments").fetchone()[0]
    missing_send = sql.execute("SELECT max(id) + 1000 FROM invite_sends").fetchone()[0]
    missing_update = sql.execute("SELECT max(id) + 1000 FROM telegram_updates").fetchone()[0]
    other = _enrollment(sql, _person(sql), offering)

    _refused("send-of-missing-enrollment", psycopg.errors.ForeignKeyViolation,
             lambda: _invite_send(sql, missing_enrollment), table="invite_sends")
    _refused("invite-of-missing-send", psycopg.errors.ForeignKeyViolation,
             lambda: _invite(sql, other, invite_send_id=missing_send), table="invites")
    _refused("invite-presented-by-missing-update", psycopg.errors.ForeignKeyViolation,
             lambda: _invite(sql, other, invite_send_id=send, presented_update_id=missing_update,
                             presented_terms_version=1), table="invites")
    assert sql.execute("SELECT id, enrollment_id FROM invite_sends ORDER BY id").fetchall() == [
        (send, enrollment)]
    assert sql.execute("SELECT id, enrollment_id, invite_send_id, presented_update_id FROM invites "
                       "ORDER BY id").fetchall() == [(invite, enrollment, send, update)]


# ---------------------------------------------------------------------------------------------
# Tipos de evento
# ---------------------------------------------------------------------------------------------


def test_event_type_check_accepts_exactly_the_29_types(sql: psycopg.Connection) -> None:
    """Oito tipos novos de aluno (o CHECK `events_type_check`
    recriado com os 21 tipos anteriores e os 8 novos: 29): cada um dos 29 tipos é aceito; um
    tipo fora deles (inclusive um tipo do vínculo do aluno em outra caixa ou com outro nome) é recusado por esse
    CHECK."""
    refused_types = []
    for event_type in CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_LINK_EVENT_TYPES:
        try:
            _insert(sql, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                    data=Jsonb({}))
        except psycopg.errors.CheckViolation:
            refused_types.append(event_type)
    assert refused_types == [], (
        "tipos recusados pelo CHECK de eventos (o esquema exige os 29)")
    for label, event_type in (("unknown", "student.deleted"), ("other-case", "Student.joined"),
                              ("singular", "student.invite_prepared"), ("prefix", "student.")):
        _refused(label, psycopg.errors.CheckViolation,
                 lambda t=event_type: _insert(sql, "events", key="seq", type=t, occurred_at=T0,
                                              actor_token_id=None, data=Jsonb({})),
                 constraint="events_type_check")
    assert [row[0] for row in sql.execute("SELECT type FROM events ORDER BY seq").fetchall()] == list(
        CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_LINK_EVENT_TYPES)
