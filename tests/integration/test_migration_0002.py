"""Migração `0002` (oferecimentos e bots) sobre o esquema da API base.

Cobre: a versão esperada `0002`; a `0002` aplicada sobre um banco em `0001` com dados da API base
(`tests/helpers/db.py::apply_up_to`) preserva as linhas, as sequências e a gravação das mensagens da
API base só com as colunas dela; e os CHECKs e índices únicos do esquema de oferecimentos e bots: estados e horários
coerentes do oferecimento, código de arquivamento só em `draft`/`closed` (e com a validade junto),
blob do token só na versão 1 e com 34 a 113 bytes, no máximo um convite `pending` por matrícula, um
endereço vigente por `(bot_id, chat_id)` e por `(person_id, channel, bot_id)`, o destino único das
mensagens (fila de saída por bot) e o CHECK de tipos de evento recriado com os 14 novos.

As linhas de apoio são gravadas por SQL direto, com as colunas do esquema de oferecimentos e bots (o esquema é o
contrato conferido em `test_migrations.py`); horários fixos em UTC; ids internos nunca como literal.
"""

from __future__ import annotations

import hashlib
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
from tests.helpers.db import apply_up_to, packaged_migration_files

T0 = datetime(2031, 1, 1, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)

# Os 7 tipos da API base e os 14 de oferecimentos e bots, como literais (e não `EventType`).
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")

# As colunas da API base, na ordem da chave de cada tabela.
CORE_COLUMNS: dict[str, tuple[str, ...]] = {
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
                        "created_at", "sent_at"),
}
OFFERING_BOT_TABLES = ("offerings", "bots", "enrollments", "invites", "delivery_endpoints", "telegram_updates")

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


def _count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))).fetchone()[0]


def _refused(label: str, error: type[psycopg.Error], insert: Callable[[], Any]) -> None:
    """O `INSERT` tem de ser recusado com exatamente `error` (a conexão é autocommit: a recusa
    desfaz só o próprio comando)."""
    try:
        insert()
    except error:
        return
    except psycopg.Error as exc:
        pytest.fail(f"[{label}] recusado com {type(exc).__name__}, e não com {error.__name__}")
    pytest.fail(f"[{label}] aceito, mas o esquema de oferecimentos e bots exige a recusa ({error.__name__})")


def _person(conn: psycopg.Connection) -> int:
    n = next(_counter)
    return _insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"abc{n:09x}", created_at=T0)


def _offering(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": uuid.uuid4(), "code": f"oferta-checagem-{n}", "course": "Disciplina Neutra",
        "term": "2031s1", "timezone": "America/Sao_Paulo", "response_window_minutes": 1440,
        "state": "draft", "created_at": T0, "activated_at": None, "closed_at": None,
        "archived_at": None, "archive_code_sha256": None, "archive_code_expires_at": None,
    }
    values.update(overrides)
    return _insert(conn, "offerings", **values)


def _blob(length: int, version: int = 1) -> bytes:
    """Blob com a forma do formato v1: versão + bytes aleatórios."""
    return bytes([version]) + os.urandom(length - 1)


def _bot(conn: psycopg.Connection, offering_id: int, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "bot_uuid": uuid.uuid4(), "offering_id": offering_id, "telegram_bot_id": 6_200_000_000 + n,
        "username": f"cq_checagem_{n:04d}_bot", "token_ciphertext": _blob(80), "token_version": 1,
        "state": "registered", "access_problem": None, "profile_state": "pending",
        "lease_owner": None, "lease_until": None, "registered_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "bots", **values)


def _enrollment(conn: psycopg.Connection, person_id: int, offering_id: int) -> int:
    return _insert(conn, "enrollments", person_id=person_id, offering_id=offering_id, role="team",
                   state="pending", created_at=T0, activated_at=None)


def _invite(conn: psycopg.Connection, enrollment_id: int, state: str) -> int:
    return _insert(conn, "invites", enrollment_id=enrollment_id,
                   code_sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest(), state=state,
                   expires_at=T0 + timedelta(days=7), created_at=T0,
                   used_at=T1 if state == "used" else None,
                   revoked_at=T1 if state == "revoked" else None)


def _endpoint(conn: psycopg.Connection, person_id: int, bot_id: int, chat_id: int, state: str) -> int:
    return _insert(conn, "delivery_endpoints", person_id=person_id, bot_id=bot_id, channel="telegram",
                   chat_id=chat_id, state=state, created_at=T0, changed_at=T0)


def _update(conn: psycopg.Connection, bot_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "telegram_updates", bot_id=bot_id, update_epoch=0, update_id=810_000_000 + n,
                   kind="message", chat_id=5_100_000_000 + n, chat_type="private",
                   sender_id=5_100_000_000 + n, text="/ajuda", command="/ajuda",
                   details=Jsonb({"message_id": 1, "date": 1924992000}), received_at=T0,
                   state="pending", outcome=None, processed_at=None)


def _message(conn: psycopg.Connection, **destination: Any) -> int:
    return _insert(conn, "outbox_messages", text="mensagem de checagem", state="pending", attempts=0,
                   available_at=T0, created_at=T0, **destination)


# ---------------------------------------------------------------------------------------------
# Versão e migração sobre dados da API base
# ---------------------------------------------------------------------------------------------


def test_expected_version_and_packaged_migrations(empty_db_url: str) -> None:
    """Nova migração versionada, aplicada como na API base, e as migrações seguintes
    (vínculo do aluno, disparo, relatórios, CLI, implantação e banco de questões), cada uma com a sua
    migração (cabeçalho: `EXPECTED_VERSION = "0008"`): o pacote tem exatamente `0001`, `0002`, `0003`,
    `0004`, `0005`, `0006`, `0007` e `0008`; num banco vazio, `apply_migrations` aplica as oito, em
    ordem, e a versão atual passa a `0008`."""
    # A versão do esquema é literal global.
    # A cada migração nova, só os literais de versão e de lista mudam
    # (a `0004` do disparo e resposta,
    # a `0005` dos relatórios e acompanhamento,
    # a `0006` do CLI,
    # a `0007` da implantação e piloto
    # e a `0008` do banco de questões local):
    # a versão atual é a `0008`,
    # e o pacote tem as oito migrações, em ordem.
    assert EXPECTED_VERSION == "0008"
    assert [version for version, _ in packaged_migration_files()] == ["0001", "0002", "0003", "0004",
                                                                      "0005", "0006", "0007", "0008"]
    assert apply_migrations(empty_db_url) == ["0001", "0002", "0003", "0004", "0005", "0006", "0007",
                                              "0008"]
    assert current_version(empty_db_url) == "0008"


def _seed_core(conn: psycopg.Connection) -> None:
    """Dados da API base por SQL direto, nas 6 tabelas dela, com estados variados."""
    people = [_insert(conn, "people", pseudo_id=uuid.uuid4(), alias=alias, created_at=T0)
              for alias in ("abcd01", "abcd02", "abcd03")]
    _insert(conn, "identities", key="person_id", person_id=people[0], full_name="Pessoa Migrada Um",
            ra="5500000101", email="pessoa.um@exemplo.invalid",
            email_normalized="pessoa.um@exemplo.invalid")
    _insert(conn, "identities", key="person_id", person_id=people[1], full_name="Pessoa Migrada Dois",
            ra=None, email=None, email_normalized=None)
    _insert(conn, "identities", key="person_id", person_id=people[2], full_name="Pessoa Migrada Tres",
            ra="5500000103", email=None, email_normalized=None)
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token migrado",
            secret_sha256=hashlib.sha256(b"segredo migrado").hexdigest(),
            permissions=["events:read", "people:read"], created_at=T0, revoked_at=None)
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token migrado revogado",
            secret_sha256=hashlib.sha256(b"outro segredo migrado").hexdigest(),
            permissions=["tokens:manage"], created_at=T0, revoked_at=T1)
    for event_type, data in (("person.registered", {"pseudo_id": str(uuid.uuid4()), "alias": "abcd01"}),
                             ("token.revoked", {"token_id": str(uuid.uuid4())}),
                             ("task.failed", {"task_id": 1, "task_type": "demo.migrada", "attempts": 3,
                                              "error": "falha migrada"})):
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb(data))
    for state, lease, finished in (("pending", None, None), ("leased", "worker-migrado", None),
                                   ("done", None, T2), ("failed", None, T2)):
        _insert(conn, "tasks", task_type="demo.migrada", payload=Jsonb({"estado": state}),
                idempotency_key=f"migrada:{state}", state=state, attempts=0 if state == "pending" else 1,
                available_at=T1, lease_owner=lease, lease_until=T2 if lease else None,
                last_error="falha migrada" if state == "failed" else None, created_at=T0,
                finished_at=finished)
    for state, sent in (("pending", None), ("sent", T2), ("failed", None)):
        _insert(conn, "outbox_messages", recipient_person_id=people[0], text=f"mensagem {state}",
                idempotency_key=f"migrada:{state}", state=state, attempts=0 if state == "pending" else 1,
                available_at=T1, lease_owner=None, lease_until=None,
                channel_ref="console:1" if state == "sent" else None,
                last_error="recusada" if state == "failed" else None, created_at=T0, sent_at=sent)


def test_0002_over_core_database_preserves_rows_and_sequences(empty_db_url: str) -> None:
    """As linhas da API base continuam válidas sem mudança com o esquema de oferecimentos e bots;
    migração sobre dados da API base: um banco em `0001` com esses dados recebe a `0002` pela
    função do sistema, que devolve `["0002", "0003", "0004", "0005", "0006", "0007", "0008"]`; cada
    linha do banco antigo fica
    igual (todas as colunas da API base), as 3 colunas novas de `outbox_messages` ficam nulas nas linhas
    antigas, as 6 tabelas novas nascem vazias, as sequências continuam depois do maior id e
    `schema_migrations` ganha a `0002`, a `0003` (vínculo do aluno), a `0004` (disparo), a `0005`
    (relatórios), a `0006` (CLI), a `0007` (implantação) e a `0008` (banco de questões); aplicar de
    novo não muda nada."""
    assert apply_up_to(empty_db_url, "0001") == ["0001"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_core(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in CORE_COLUMNS.items()}
        max_ids = {table: conn.execute(pgsql.SQL("SELECT max(id) FROM {}").format(
            pgsql.Identifier(table))).fetchone()[0] for table in ("people", "api_tokens", "tasks",
                                                                   "outbox_messages")}
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    assert current_version(empty_db_url) == "0001"

    # A função do sistema aplica também as migrações seguintes: a `0003` (vínculo do aluno),
    # a `0004` (disparo), a `0005` (relatórios),
    # a `0006` (CLI),
    # a `0007` (implantação)
    # e a `0008` (banco de questões).
    assert apply_migrations(empty_db_url) == ["0002", "0003", "0004", "0005", "0006", "0007", "0008"]
    assert current_version(empty_db_url) == "0008"

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after = {table: _snapshot(conn, table, columns) for table, columns in CORE_COLUMNS.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:1] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004",
                                                                  "0005", "0006", "0007", "0008"]
        assert conn.execute(
            "SELECT bot_id, delivery_endpoint_id, reply_to_update_id FROM outbox_messages ORDER BY id"
        ).fetchall() == [(None, None, None)] * 3
        assert {table: _count(conn, table) for table in OFFERING_BOT_TABLES} == dict.fromkeys(OFFERING_BOT_TABLES, 0)

        # As sequências continuam: o próximo id de cada tabela da API base vem depois do maior antigo.
        person = _person(conn)
        assert person > max_ids["people"]
        token = _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token novo",
                        secret_sha256=hashlib.sha256(b"segredo novo").hexdigest(),
                        permissions=["people:read"], created_at=T3, revoked_at=None)
        assert token > max_ids["api_tokens"]
        task = _insert(conn, "tasks", task_type="demo.nova", payload=Jsonb({}), state="pending",
                       attempts=0, available_at=T3, created_at=T3)
        assert task > max_ids["tasks"]
        seq = _insert(conn, "events", key="seq", type="person.registered", occurred_at=T3,
                      actor_token_id="bootstrap", data=Jsonb({"pseudo_id": str(uuid.uuid4()),
                                                              "alias": "abcd04"}))
        assert seq > max_seq
        # Mensagem da API base só com as colunas dela (destino (a) do CHECK de destino único).
        message = _insert(conn, "outbox_messages", recipient_person_id=person, text="mensagem nova",
                          state="pending", attempts=0, available_at=T3, created_at=T3)
        assert message > max_ids["outbox_messages"]
        assert conn.execute(
            "SELECT recipient_person_id, bot_id, delivery_endpoint_id, reply_to_update_id "
            "FROM outbox_messages WHERE id = %s", (message,)).fetchone() == (person, None, None, None)
        stable = {table: _snapshot(conn, table, columns) for table, columns in CORE_COLUMNS.items()}

    assert apply_migrations(empty_db_url) == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert {table: _snapshot(conn, table, columns)
                for table, columns in CORE_COLUMNS.items()} == stable


def test_core_message_is_still_enqueued_with_core_columns_only(sql: psycopg.Connection, db_url: str,
                                                           outbox: Any) -> None:
    """As linhas da API base continuam válidas sem mudança com o esquema de oferecimentos e bots; fila
    de saída e porta de entrega (`recipient` sem `bot_id` é a mensagem da API base): no esquema
    `0002`, o `Outbox.enqueue` da API base grava a mensagem com a pessoa e as 3 colunas novas nulas, e um
    `INSERT` só com as colunas da API base também passa."""
    # A versão do esquema é literal global. No disparo (a `0004`), as
    # colunas novas da fila são anuláveis ou com padrão, e o CHECK de destino não muda.
    # Nos relatórios (a `0005`), as colunas novas da
    # fila (`notice_id`, `text_format`) são anuláveis, e a forma de texto do CHECK aceita a linha da API base.
    # O CLI (a `0006`), a implantação (a `0007`) e o banco de questões (a `0008`) não mudam a fila.
    # A versão atual é a `0008`.
    assert current_version(db_url) == "0008"
    pseudo_id = uuid.uuid4()
    person = _insert(sql, "people", pseudo_id=pseudo_id, alias="abcdef01", created_at=T0)
    with psycopg.connect(db_url) as conn:
        enqueued = outbox.enqueue(conn, recipient=pseudo_id, text="mensagem da API base no esquema novo")
    inserted = _insert(sql, "outbox_messages", recipient_person_id=person, text="mensagem por SQL",
                       state="pending", attempts=0, available_at=T0, created_at=T0)
    assert sql.execute(
        "SELECT id, recipient_person_id, bot_id, delivery_endpoint_id, reply_to_update_id, state "
        "FROM outbox_messages ORDER BY id").fetchall() == [
        (enqueued, person, None, None, None, "pending"),
        (inserted, person, None, None, None, "pending"),
    ]


# ---------------------------------------------------------------------------------------------
# CHECKs do oferecimento
# ---------------------------------------------------------------------------------------------

# (rótulo, colunas) de estados e horários coerentes: cada um é aceito.
COHERENT_OFFERINGS = [
    ("draft", {"state": "draft"}),
    ("active", {"state": "active", "activated_at": T1}),
    ("closed", {"state": "closed", "activated_at": T1, "closed_at": T2}),
    ("archived-from-draft", {"state": "archived", "archived_at": T3}),
    ("archived-from-closed", {"state": "archived", "activated_at": T1, "closed_at": T2,
                              "archived_at": T3}),
]

# (rótulo, colunas) incoerentes: cada um é recusado com CheckViolation.
INCOHERENT_OFFERINGS = [
    ("unknown-state", {"state": "paused"}),
    ("draft-with-activated_at", {"state": "draft", "activated_at": T1}),
    ("draft-with-closed_at", {"state": "draft", "closed_at": T2}),
    ("draft-with-archived_at", {"state": "draft", "archived_at": T3}),
    ("active-without-activated_at", {"state": "active"}),
    ("active-with-closed_at", {"state": "active", "activated_at": T1, "closed_at": T2}),
    ("active-with-archived_at", {"state": "active", "activated_at": T1, "archived_at": T3}),
    ("closed-without-closed_at", {"state": "closed", "activated_at": T1}),
    ("closed-without-activated_at", {"state": "closed", "closed_at": T2}),
    ("closed-with-archived_at", {"state": "closed", "activated_at": T1, "closed_at": T2,
                                 "archived_at": T3}),
    ("archived-without-archived_at", {"state": "archived"}),
    ("archived-activated-without-closed_at", {"state": "archived", "activated_at": T1,
                                              "archived_at": T3}),
]


def test_offering_state_and_times_must_be_coherent(sql: psycopg.Connection) -> None:
    """Cada transição grava o próprio horário; no esquema de oferecimentos e bots (CHECKs: `draft` com
    os três horários nulos; `active` só com `activated_at`; `closed` com `activated_at` e
    `closed_at`; `archived` com `archived_at`, e `closed_at` se houver `activated_at`): os estados
    coerentes são aceitos; um estado fora dos quatro e cada combinação incoerente são recusados."""
    for label, columns in COHERENT_OFFERINGS:
        assert isinstance(_offering(sql, **columns), int), label
    for label, columns in INCOHERENT_OFFERINGS:
        _refused(label, psycopg.errors.CheckViolation, lambda c=columns: _offering(sql, **c))
    assert [row[0] for row in sql.execute("SELECT state FROM offerings ORDER BY id").fetchall()] == [
        "draft", "active", "closed", "archived", "archived"]


def test_archive_code_only_in_draft_or_closed_and_with_its_validity(sql: psycopg.Connection) -> None:
    """Código de confirmação guardado só como resumo, com validade; no esquema de oferecimentos e bots
    (`archive_code_sha256` e `archive_code_expires_at` nulos juntos, e nulos fora de `draft` e
    `closed`): o código pendente é aceito em `draft` e em `closed`; em `active` e em `archived`, e
    sem a validade (ou a validade sem o código), é recusado."""
    digest = hashlib.sha256(b"codigo de confirmacao").hexdigest()
    code = {"archive_code_sha256": digest, "archive_code_expires_at": T1 + timedelta(minutes=10)}
    accepted = [_offering(sql, state="draft", **code),
                _offering(sql, state="closed", activated_at=T1, closed_at=T2, **code)]
    assert all(isinstance(value, int) for value in accepted)
    refused = [
        ("active-with-code", {"state": "active", "activated_at": T1, **code}),
        ("archived-with-code", {"state": "archived", "archived_at": T3, **code}),
        ("archived-closed-with-code", {"state": "archived", "activated_at": T1, "closed_at": T2,
                                       "archived_at": T3, **code}),
        ("draft-code-without-validity", {"state": "draft", "archive_code_sha256": digest}),
        ("draft-validity-without-code", {"state": "draft",
                                         "archive_code_expires_at": T1 + timedelta(minutes=10)}),
        ("closed-code-without-validity", {"state": "closed", "activated_at": T1, "closed_at": T2,
                                          "archive_code_sha256": digest}),
    ]
    for label, columns in refused:
        _refused(label, psycopg.errors.CheckViolation, lambda c=columns: _offering(sql, **c))
    assert sql.execute("SELECT count(*) FROM offerings").fetchone()[0] == 2


# ---------------------------------------------------------------------------------------------
# Blob do token
# ---------------------------------------------------------------------------------------------


def test_bot_token_blob_must_be_version_1_with_34_to_113_bytes(sql: psycopg.Connection) -> None:
    """CHECK de `token_ciphertext`: `get_byte(…, 0) = 1` e tamanho de 34
    a 113 bytes (formato v1): versão 1 com 34, 80 e 113 bytes é aceita; versão 2 (e 0) com
    tamanho válido, 33 bytes (curto) e 114 bytes (longo) são recusados."""
    for length in (34, 80, 113):
        assert isinstance(_bot(sql, _offering(sql), token_ciphertext=_blob(length)), int), length
    for label, blob in (("version-2", _blob(60, version=2)), ("version-0", _blob(60, version=0)),
                        ("33-bytes", _blob(33)), ("114-bytes", _blob(114))):
        offering = _offering(sql)
        _refused(label, psycopg.errors.CheckViolation,
                 lambda o=offering, b=blob: _bot(sql, o, token_ciphertext=b))
    assert sql.execute(
        "SELECT octet_length(token_ciphertext), get_byte(token_ciphertext, 0) FROM bots ORDER BY id"
    ).fetchall() == [(34, 1), (80, 1), (113, 1)]


# ---------------------------------------------------------------------------------------------
# Índices únicos parciais
# ---------------------------------------------------------------------------------------------


def test_at_most_one_pending_invite_per_enrollment(sql: psycopg.Connection) -> None:
    """Gerar outro link para a mesma matrícula revoga o convite pendente anterior;
    índice único parcial (no máximo um convite `pending` por matrícula): um
    segundo `pending` na mesma matrícula é recusado; `used` e `revoked` ao lado de um `pending`, e um
    `pending` noutra matrícula, são aceitos."""
    person = _person(sql)
    offering = _offering(sql)
    enrollment = _enrollment(sql, person, offering)
    other = _enrollment(sql, _person(sql), offering)
    _invite(sql, enrollment, "pending")
    _refused("second-pending", psycopg.errors.UniqueViolation,
             lambda: _invite(sql, enrollment, "pending"))
    _invite(sql, enrollment, "used")
    _invite(sql, enrollment, "revoked")
    _invite(sql, enrollment, "revoked")
    _invite(sql, other, "pending")
    rows = sql.execute("SELECT enrollment_id, state FROM invites ORDER BY id").fetchall()
    assert rows == [(enrollment, "pending"), (enrollment, "used"), (enrollment, "revoked"),
                    (enrollment, "revoked"), (other, "pending")]


def test_one_current_endpoint_per_chat_and_per_person_in_each_bot(sql: psycopg.Connection) -> None:
    """A troca de conta deixa o endereço anterior inativo; no esquema de oferecimentos e bots
    (índices únicos parciais nos estados `active` e `blocked`: um endereço vigente por `(bot_id,
    chat_id)` e um por `(person_id, channel, bot_id)`): dois vigentes do mesmo chat no mesmo bot são
    recusados (inclusive `active` + `blocked`), e dois vigentes da mesma pessoa no mesmo bot também;
    `inactive` ao lado de um vigente, o mesmo chat noutro bot e a mesma pessoa noutro bot passam."""
    bot = _bot(sql, _offering(sql))
    other_bot = _bot(sql, _offering(sql))
    ana, bia, cia = _person(sql), _person(sql), _person(sql)
    chat, other_chat = 5_100_000_001, 5_100_000_002

    _endpoint(sql, ana, bot, chat, "active")
    _refused("same-chat-active-active", psycopg.errors.UniqueViolation,
             lambda: _endpoint(sql, bia, bot, chat, "active"))
    _refused("same-chat-active-blocked", psycopg.errors.UniqueViolation,
             lambda: _endpoint(sql, bia, bot, chat, "blocked"))
    _refused("same-person-two-current", psycopg.errors.UniqueViolation,
             lambda: _endpoint(sql, ana, bot, other_chat, "blocked"))
    _endpoint(sql, bia, bot, chat, "inactive")
    _endpoint(sql, ana, bot, other_chat, "inactive")
    _endpoint(sql, bia, other_bot, chat, "blocked")
    _endpoint(sql, ana, other_bot, other_chat, "active")
    _refused("same-chat-blocked-active", psycopg.errors.UniqueViolation,
             lambda: _endpoint(sql, cia, other_bot, chat, "active"))
    rows = sql.execute(
        "SELECT person_id, bot_id, chat_id, state FROM delivery_endpoints ORDER BY id").fetchall()
    assert rows == [(ana, bot, chat, "active"), (bia, bot, chat, "inactive"),
                    (ana, bot, other_chat, "inactive"), (bia, other_bot, chat, "blocked"),
                    (ana, other_bot, other_chat, "active")]


# ---------------------------------------------------------------------------------------------
# Destino único da mensagem (fila de saída por bot)
# ---------------------------------------------------------------------------------------------


def test_outbox_message_has_exactly_one_kind_of_destination(sql: psycopg.Connection) -> None:
    """Destino único da mensagem (CHECK de destino único: (a) pessoa, sem bot, endereço nem
    update; (b) pessoa, bot e endereço, sem update; (c) bot e update, sem pessoa nem endereço), a
    contrapartida da nulidade de `recipient_person_id`: as três formas são
    aceitas; sem destino, ou com qualquer mistura, a mensagem é recusada."""
    person = _person(sql)
    bot = _bot(sql, _offering(sql))
    endpoint = _endpoint(sql, person, bot, 5_100_000_003, "active")
    update = _update(sql, bot)

    accepted = {
        "a-person": {"recipient_person_id": person},
        "b-person-bot-endpoint": {"recipient_person_id": person, "bot_id": bot,
                                  "delivery_endpoint_id": endpoint},
        "c-bot-update": {"bot_id": bot, "reply_to_update_id": update},
    }
    ids = {label: _message(sql, **destination) for label, destination in accepted.items()}
    refused = {
        "nothing": {},
        "bot-only": {"bot_id": bot},
        "update-only": {"reply_to_update_id": update},
        "endpoint-only": {"delivery_endpoint_id": endpoint},
        "person-bot-without-endpoint": {"recipient_person_id": person, "bot_id": bot},
        "person-endpoint-without-bot": {"recipient_person_id": person,
                                        "delivery_endpoint_id": endpoint},
        "person-bot-endpoint-update": {"recipient_person_id": person, "bot_id": bot,
                                       "delivery_endpoint_id": endpoint, "reply_to_update_id": update},
        "person-bot-update": {"recipient_person_id": person, "bot_id": bot,
                              "reply_to_update_id": update},
        "bot-endpoint-update": {"bot_id": bot, "delivery_endpoint_id": endpoint,
                                "reply_to_update_id": update},
        "person-update": {"recipient_person_id": person, "reply_to_update_id": update},
    }
    for label, destination in refused.items():
        _refused(label, psycopg.errors.CheckViolation, lambda d=destination: _message(sql, **d))
    rows = sql.execute("SELECT id, recipient_person_id, bot_id, delivery_endpoint_id, "
                       "reply_to_update_id FROM outbox_messages ORDER BY id").fetchall()
    assert rows == [
        (ids["a-person"], person, None, None, None),
        (ids["b-person-bot-endpoint"], person, bot, endpoint, None),
        (ids["c-bot-update"], None, bot, None, update),
    ]


# ---------------------------------------------------------------------------------------------
# Tipos de evento
# ---------------------------------------------------------------------------------------------


def test_event_type_check_accepts_exactly_the_21_types(sql: psycopg.Connection) -> None:
    """O CHECK de tipos de evento recriado com os 14
    novos: cada um dos 7 tipos da API base e dos 14 de oferecimentos e bots é aceito; um tipo fora deles é recusado."""
    for event_type in CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES:
        _insert(sql, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                data=Jsonb({}))
    _refused("unknown-type", psycopg.errors.CheckViolation,
             lambda: _insert(sql, "events", key="seq", type="offering.deleted", occurred_at=T0,
                             actor_token_id=None, data=Jsonb({})))
    assert [row[0] for row in sql.execute("SELECT type FROM events ORDER BY seq").fetchall()] == list(
        CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES)
