"""Migração `0006` (o CLI) sobre o esquema dos relatórios e do acompanhamento.


Cobre:
- o que a `0006` acrescenta ao esquema anterior, e só isso, medido por `apply_up_to(…, "0005")` e
  `apply_up_to(…, "0006") == ["0006"]` (`tests/helpers/db.py`), **sem** afirmar `EXPECTED_VERSION` (a
  versão vigente só é afirmada em outros arquivos de teste): exatamente as 5 colunas novas de
  `offerings` (`purged_at timestamptz NULL`, `purge_result jsonb NULL`, `purge_code_sha256 text NULL`,
  `purge_code_expires_at timestamptz NULL`, `purge_prepared_counts jsonb NULL`); `delivery_endpoints.chat_id`
  passando a aceitar nulo (a única coluna existente que muda); nenhuma tabela, índice, chave, FK nem gatilho
  novo; os CHECKs removidos são só o `events_type_check`, recriado com o mesmo nome, e os novos são só
  `offerings_purge_check` e `offerings_purge_code_check`;
- os dois CHECKs de `offerings` com controle positivo e negativo: `offerings_purge_check` (`purged_at` só
  com `state = 'archived'`; `(purged_at IS NULL) = (purge_result IS NULL)`; `purge_result` só objeto JSON)
  e `offerings_purge_code_check` (os três campos do código juntos; código só com `state = 'archived' AND
  purged_at IS NULL`; `purge_prepared_counts` só objeto JSON);
- `delivery_endpoints.chat_id` nulo aceito (no `INSERT` e no `UPDATE` que o expurgo faz) e o índice único
  parcial `delivery_endpoints_current_chat_idx`, sem mudança, aceitando dois nulos vigentes no mesmo bot e
  recusando dois iguais não nulos;
- o `events_type_check` recriado com o mesmo nome, aceitando os 46 tipos anteriores e os 2 novos
  (`offering.purge_prepared` e `offering.purged`) e recusando um desconhecido;
- o `DETAIL` de toda violação sem texto livre nem identidade (os textos de aluno e a identidade gravados no
  banco não aparecem; a unicidade do chat mostra só a chave);
- nenhuma linha anterior muda: cada tabela igual antes e depois em todas as colunas anteriores, as colunas
  novas nulas, `schema_migrations` com a `0006`, e aplicar de novo não muda nada.

As linhas de apoio são gravadas por SQL direto, com as colunas do esquema, da API base ao CLI; horários fixos
em UTC; ids internos nunca como literal esperado. Os nomes de restrição conferidos exatamente são os que a
migração declara. Cada teste começa por `apply_up_to(…, "0006")`, chamado no corpo do teste.

No esqueleto (sem a `0006`), todo teste deste arquivo falha pela migração ausente do pacote
(`apply_up_to(…, "0006")`: "migração 0006 não existe no pacote").

"""

from __future__ import annotations

import hashlib
import itertools
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
T3 = T0 + timedelta(hours=3)

# Os 29 tipos anteriores, os 14 do disparo, os 3 dos relatórios e os 2 do CLI, como
# literais (e não `EventType`).
PRE_DISPATCH_EVENT_TYPES = (
    "person.registered", "token.created", "token.revoked", "identity.revealed",
    "identity.reveal_denied", "task.failed", "outbox.failed",
    "offering.created", "offering.state_changed", "offering.archive_prepared", "bot.registered",
    "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed",
    "student.list_imported", "student.invites_prepared", "student.invites_queued",
    "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
    "student.invite_revoked", "student.joined",
)
DISPATCH_EVENT_TYPES = (
    "dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
    "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
    "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know", "submission.graded",
    "submission.internal_failure", "offering.settings_changed",
)
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
PURGE_EVENT_TYPES = ("offering.purge_prepared", "offering.purged")
assert (len(PRE_DISPATCH_EVENT_TYPES), len(DISPATCH_EVENT_TYPES), len(REPORT_EVENT_TYPES), len(PURGE_EVENT_TYPES)) == (
    29, 14, 3, 2)
ALL_EVENT_TYPES = PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES + REPORT_EVENT_TYPES + PURGE_EVENT_TYPES

# As 29 tabelas do esquema dos relatórios (as 22 do disparo e as 7 dos relatórios); a `0006` não cria nenhuma.
REPORT_SCHEMA_TABLES = {
    "schema_migrations", "people", "identities", "api_tokens", "events", "tasks", "outbox_messages",
    "offerings", "bots", "enrollments", "invites", "delivery_endpoints", "telegram_updates", "invite_sends",
    "question_refs", "dispatches", "deliveries", "delivery_contents", "media_blobs", "submissions",
    "free_messages", "offering_contacts",
    "dispatch_reports", "team_notices", "team_notice_floors", "dispatch_previews",
    "dispatch_preview_contents", "offering_digests", "invite_batches",
}

# O esquema do CLI: (tabela, coluna) -> (tipo em `udt_name`, anulável, identity, padrão), para cada
# coluna que a `0006` acrescenta.
PURGE_NEW_COLUMNS: dict[tuple[str, str], tuple[str, bool, bool, str | None]] = {
    ("offerings", "purged_at"): ("timestamptz", True, False, None),
    ("offerings", "purge_result"): ("jsonb", True, False, None),
    ("offerings", "purge_code_sha256"): ("text", True, False, None),
    ("offerings", "purge_code_expires_at"): ("timestamptz", True, False, None),
    ("offerings", "purge_prepared_counts"): ("jsonb", True, False, None),
}
# A única coluna existente que muda; antes `bigint NOT NULL`.
PURGE_CHANGED_COLUMNS = {
    ("delivery_endpoints", "chat_id"): (("int8", False, False, None), ("int8", True, False, None)),
}
PURGE_NEW_CHECKS = {("offerings", "offerings_purge_check"), ("offerings", "offerings_purge_code_check")}

# As contagens gravadas pelo expurgo e pela preparação, só números.
PURGE_RESULT = {"identities_deleted": 2, "identities_kept": 1, "endpoints": 2, "updates": 3,
                "free_messages_redacted": 1, "outbox_redacted": 2, "contact_email_deleted": True}
PREPARED_COUNTS = {"identities_to_delete": 2, "identities_kept": 1, "endpoints": 2, "updates": 3,
                   "contact_email_set": True}

# Textos sentinela (texto do aluno, identidade e o código de confirmação em claro) para a checagem do
# `DETAIL` das violações.
STUDENT_SENTINEL = "mensagem-sentinela-do-aluno-0006"
NAME_SENTINEL = "Pessoa Sentinela Zero Seis"
RA_SENTINEL = "5500060606"
EMAIL_SENTINEL = "sentinela.zero.seis@exemplo.invalid"
CONTACT_SENTINEL = "contato.sentinela.0006@exemplo.invalid"
CODE_SENTINEL = "codigo-sentinela-0006"
SENTINELS = (STUDENT_SENTINEL, NAME_SENTINEL, RA_SENTINEL, EMAIL_SENTINEL, CONTACT_SENTINEL, CODE_SENTINEL)

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


def _refused(label: str, error: type[psycopg.Error], command: Callable[[], Any], *,
             constraint: str | None = None, table: str | None = None) -> psycopg.Error:
    """O comando tem de ser recusado com exatamente `error` (a conexão é autocommit: a recusa desfaz só o
    próprio comando), pela restrição `constraint` (nome exato declarado na migração) e na tabela `table`,
    quando dados; devolve a exceção. Tabela ou coluna ausente sobe como está."""
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


def _assert_detail_without_text(exc: psycopg.Error, label: str, expected_detail: str | None = None) -> None:
    """O `DETAIL` (e a mensagem inteira) sem nenhum texto sentinela: nem texto do aluno, nem identidade,
    nem o contato, nem o código em claro; quando dado, o `DETAIL` exato."""
    detail = exc.diag.message_detail or ""
    if expected_detail is not None:
        assert detail == expected_detail, (label, detail)
    for sentinel in SENTINELS:
        assert sentinel not in detail and sentinel not in str(exc), (label, sentinel)


def _migrated(url: str) -> None:
    """Banco vazio levado até a `0006` pelas migrações do pacote (sem `EXPECTED_VERSION`)."""
    assert apply_up_to(url, "0006") == ["0001", "0002", "0003", "0004", "0005", "0006"]


def _person(conn: psycopg.Connection) -> int:
    n = next(_counter)
    return _insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"exp{n:09x}", created_at=T0)


def _offering(conn: psycopg.Connection, **overrides: Any) -> int:
    """Um oferecimento arquivado a partir do rascunho (sem `activated_at` nem `closed_at`), salvo
    `overrides`."""
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": uuid.uuid4(), "code": f"oferta-expurgo-{n}", "course": "Disciplina Neutra",
        "term": "2031s1", "timezone": "America/Sao_Paulo", "response_window_minutes": 60,
        "state": "archived", "created_at": T0, "archived_at": T1,
    }
    values.update(overrides)
    return _insert(conn, "offerings", **values)


# Os estados fora de `archived`, com os horários coerentes (`offerings_state_times_check`).
OTHER_STATES: dict[str, dict[str, Any]] = {
    "draft": {"state": "draft", "archived_at": None},
    "active": {"state": "active", "activated_at": T0, "archived_at": None},
    "closed": {"state": "closed", "activated_at": T0, "closed_at": T1, "archived_at": None},
}


def _code(code: str = CODE_SENTINEL) -> dict[str, Any]:
    """Os três campos do código de confirmação pendente (o resumo, nunca o código em claro)."""
    return {"purge_code_sha256": hashlib.sha256(code.encode()).hexdigest(), "purge_code_expires_at": T3,
            "purge_prepared_counts": Jsonb(PREPARED_COUNTS)}


def _purged() -> dict[str, Any]:
    return {"purged_at": T2, "purge_result": Jsonb(PURGE_RESULT)}


def _bot(conn: psycopg.Connection, offering_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "bots", bot_uuid=uuid.uuid4(), offering_id=offering_id,
                   telegram_bot_id=6_600_000_000 + n, username=f"cq_expurgo_{n:04d}_bot",
                   token_ciphertext=bytes([1]) + secrets.token_bytes(79), token_version=1,
                   state="registered", access_problem=None, profile_state="configured",
                   registered_at=T0)


def _student(conn: psycopg.Connection, offering_id: int, person_id: int | None = None) -> int:
    """Uma matrícula de aluno `active` com o termo aceito; devolve o id."""
    return _insert(conn, "enrollments", person_id=person_id or _person(conn), offering_id=offering_id,
                   role="student", state="active", created_at=T0, activated_at=T1, terms_version=1,
                   terms_accepted_at=T1)


def _endpoint(conn: psycopg.Connection, person_id: int, bot_id: int, *, chat_id: int | None = -1,
              state: str = "active") -> int:
    """Um endereço de entrega; `chat_id=-1` sorteia um número novo."""
    n = next(_counter)
    return _insert(conn, "delivery_endpoints", person_id=person_id, bot_id=bot_id, channel="telegram",
                   chat_id=5_600_000_000 + n if chat_id == -1 else chat_id, state=state, created_at=T0,
                   changed_at=T0)


def _update(conn: psycopg.Connection, bot_id: int, *, text: str = "texto neutro") -> int:
    n = next(_counter)
    return _insert(conn, "telegram_updates", bot_id=bot_id, update_epoch=0, update_id=860_000_000 + n,
                   kind="message", chat_id=5_600_900_000 + n, chat_type="private",
                   sender_id=5_600_900_000 + n, text=text, command=None,
                   details=Jsonb({"message_id": n, "date": 1930564800}), received_at=T1,
                   state="processed", outcome="answer_recorded", processed_at=T1)


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


def _string_literals(text: str | None) -> set[str]:
    return set(re.findall(r"'([^']*)'", text or ""))


def _count(conn: psycopg.Connection, table: str) -> int:
    return conn.execute(pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))).fetchone()[0]


def _seed_identity_and_texts(conn: psycopg.Connection) -> tuple[int, int, int]:
    """Um oferecimento arquivado com bot, um aluno com identidade sentinela, endereço, update e mensagem
    livre com o texto sentinela, e o contato sentinela; devolve (oferecimento, bot, pessoa)."""
    offering = _offering(conn)
    bot = _bot(conn, offering)
    person = _person(conn)
    _insert(conn, "identities", key="person_id", person_id=person, full_name=NAME_SENTINEL, ra=RA_SENTINEL,
            email=EMAIL_SENTINEL, email_normalized=EMAIL_SENTINEL)
    _student(conn, offering, person)
    endpoint = _endpoint(conn, person, bot)
    update = _update(conn, bot, text=STUDENT_SENTINEL)
    _insert(conn, "free_messages", offering_id=offering, delivery_endpoint_id=endpoint,
            telegram_update_id=update, text=STUDENT_SENTINEL, replied=False, ambiguous=False,
            received_at=T1, created_at=T1)
    _insert(conn, "offering_contacts", offering_id=offering, email=CONTACT_SENTINEL, updated_at=T0)
    return offering, bot, person


# ---------------------------------------------------------------------------------------------
# O que a 0006 acrescenta (delta por `apply_up_to`, sem afirmar `EXPECTED_VERSION`)
# ---------------------------------------------------------------------------------------------


def test_0006_adds_exactly_the_purge_columns_and_relaxes_chat_id(empty_db_url: str) -> None:
    """As mudanças no banco entram como uma nova migração versionada, aplicada como as anteriores;
    conforme o esquema do CLI: num banco em `0005`, a `0006` devolve `["0006"]`, acrescenta
    exatamente as 5 colunas de `PURGE_NEW_COLUMNS` (todas anuláveis, sem identity nem padrão), muda só
    `delivery_endpoints.chat_id` (de `NOT NULL` para anulável), não remove nenhuma coluna e não cria
    nenhuma tabela."""
    assert apply_up_to(empty_db_url, "0005") == ["0001", "0002", "0003", "0004", "0005"]
    before = _columns(empty_db_url)
    assert {table for table, _ in before} == REPORT_SCHEMA_TABLES
    assert apply_up_to(empty_db_url, "0006") == ["0006"]
    after = _columns(empty_db_url)

    assert set(before) <= set(after), f"colunas removidas: {sorted(set(before) - set(after))}"
    changed = {key: (before[key], after[key]) for key in before if after[key] != before[key]}
    assert changed == PURGE_CHANGED_COLUMNS
    added = {key: value for key, value in after.items() if key not in before}
    assert added == PURGE_NEW_COLUMNS
    assert {table for table, _ in after} == REPORT_SCHEMA_TABLES


def test_0006_adds_only_the_declared_checks_and_no_index_key_or_trigger(empty_db_url: str) -> None:
    """Esquema do CLI (os dois CHECKs de `offerings`, com os nomes declarados); o
    índice único parcial `delivery_endpoints_current_chat_idx` sem mudança; `events_type_check`
    recriado com o mesmo nome e os 48 tipos; nenhuma tabela nem índice novo): nenhum índice criado
    nem removido; as restrições removidas são só o `events_type_check` anterior; as acrescentadas são só
    `offerings_purge_check`, `offerings_purge_code_check` e o `events_type_check` novo (nenhuma chave, FK
    nem unicidade nova); nenhum gatilho muda."""
    assert apply_up_to(empty_db_url, "0005") == ["0001", "0002", "0003", "0004", "0005"]
    indexes_before = _indexes(empty_db_url)
    constraints_before = _constraints(empty_db_url)
    triggers_before = _triggers(empty_db_url)
    assert apply_up_to(empty_db_url, "0006") == ["0006"]
    indexes_after = _indexes(empty_db_url)
    constraints_after = _constraints(empty_db_url)
    triggers_after = _triggers(empty_db_url)

    assert indexes_after == indexes_before
    chat_index = [row for row in indexes_after if row[1] == "delivery_endpoints_current_chat_idx"]
    assert len(chat_index) == 1, chat_index
    (table, _, keys, unique, primary, predicate), = chat_index
    assert (table, keys, unique, primary) == ("delivery_endpoints", ("bot_id", "chat_id"), True, False)
    assert _string_literals(predicate) == {"active", "blocked"}, predicate

    removed = constraints_before - constraints_after
    added = constraints_after - constraints_before
    assert {(table, name, kind) for table, name, kind, _ in removed} == {("events", "events_type_check", "c")}
    assert {(table, name, kind) for table, name, kind, _ in added} == {
        (table, name, "c") for table, name in PURGE_NEW_CHECKS} | {("events", "events_type_check", "c")}
    type_check = [definition for table, name, _, definition in added if name == "events_type_check"]
    assert len(type_check) == 1
    assert _string_literals(type_check[0]) == set(ALL_EVENT_TYPES)
    assert triggers_after == triggers_before


# ---------------------------------------------------------------------------------------------
# CHECKs de `offerings`
# ---------------------------------------------------------------------------------------------


def test_offerings_purge_check(empty_db_url: str) -> None:
    """O oferecimento expurgado continua `archived`; esquema do CLI
    (`offerings_purge_check`): `purged_at` com `purge_result` objeto é aceito em `archived` (vindo do
    rascunho e do encerrado) e as duas nulas em qualquer estado; são recusados por esse CHECK, com o
    `DETAIL` sem texto: `purged_at` em `draft`, `active` e `closed`; `purged_at` sem `purge_result` e o
    inverso; `purge_result` que não é objeto (lista, número, texto, `null` JSON); e a volta de um
    oferecimento expurgado a `closed` por `UPDATE`."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_identity_and_texts(conn)
        accepted = [
            _offering(conn, **_purged()),
            _offering(conn, activated_at=T0, closed_at=T1, archived_at=T2, purged_at=T3,
                      purge_result=Jsonb(PURGE_RESULT)),
            _offering(conn),
            *(_offering(conn, **values) for values in OTHER_STATES.values()),
        ]
        refused: list[tuple[str, dict[str, Any]]] = [
            *((f"purged-in-{state}", {**values, **_purged()}) for state, values in OTHER_STATES.items()),
            ("purged-without-result", {"purged_at": T2}),
            ("result-without-purged", {"purge_result": Jsonb(PURGE_RESULT)}),
            ("result-list", {"purged_at": T2, "purge_result": Jsonb([PURGE_RESULT])}),
            ("result-number", {"purged_at": T2, "purge_result": Jsonb(0)}),
            ("result-text", {"purged_at": T2, "purge_result": Jsonb("expurgado")}),
            ("result-json-null", {"purged_at": T2, "purge_result": Jsonb(None)}),
        ]
        for label, values in refused:
            exc = _refused(label, psycopg.errors.CheckViolation, lambda v=values: _offering(conn, **v),
                           constraint="offerings_purge_check", table="offerings")
            _assert_detail_without_text(exc, label)
        exc = _refused("purged-back-to-closed", psycopg.errors.CheckViolation,
                       lambda: conn.execute("UPDATE offerings SET state = 'closed', archived_at = NULL "
                                            "WHERE id = %s", (accepted[1],)),
                       constraint="offerings_purge_check", table="offerings")
        _assert_detail_without_text(exc, "purged-back-to-closed")
        rows = conn.execute("SELECT id, state, purged_at, purge_result FROM offerings WHERE id = ANY(%s) "
                            "ORDER BY id", (accepted,)).fetchall()
    assert rows == sorted([
        (accepted[0], "archived", T2, PURGE_RESULT), (accepted[1], "archived", T3, PURGE_RESULT),
        (accepted[2], "archived", None, None),
        *((offering, state, None, None) for offering, state in zip(accepted[3:], OTHER_STATES))])


def test_offerings_purge_code_check(empty_db_url: str) -> None:
    """O código de confirmação do expurgo; esquema do CLI
    (`offerings_purge_code_check`): os três campos do código (`purge_code_sha256`, `purge_code_expires_at`
    e `purge_prepared_counts` objeto) juntos são aceitos em `archived` sem `purged_at`, e um `UPDATE` que
    limpa os três e grava o expurgo é aceito; são recusados por esse CHECK, com o `DETAIL` sem texto (nem o
    código em claro): cada campo sozinho e cada par sem o terceiro; o código em `draft`, `active` e
    `closed`; o código num oferecimento já expurgado; `purge_prepared_counts` que não é objeto; e um
    `UPDATE` que grava o expurgo mantendo o código."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_identity_and_texts(conn)
        prepared = _offering(conn, **_code())
        code = _code()
        refused: list[tuple[str, dict[str, Any]]] = [
            ("only-sha", {"purge_code_sha256": code["purge_code_sha256"]}),
            ("only-expiry", {"purge_code_expires_at": T3}),
            ("only-counts", {"purge_prepared_counts": Jsonb(PREPARED_COUNTS)}),
            ("sha-and-expiry", {k: code[k] for k in ("purge_code_sha256", "purge_code_expires_at")}),
            ("sha-and-counts", {k: code[k] for k in ("purge_code_sha256", "purge_prepared_counts")}),
            ("expiry-and-counts", {k: code[k] for k in ("purge_code_expires_at", "purge_prepared_counts")}),
            *((f"code-in-{state}", {**values, **_code()}) for state, values in OTHER_STATES.items()),
            ("code-after-purge", {**_purged(), **_code()}),
            ("counts-list", {**_code(), "purge_prepared_counts": Jsonb([PREPARED_COUNTS])}),
            ("counts-number", {**_code(), "purge_prepared_counts": Jsonb(3)}),
            ("counts-json-null", {**_code(), "purge_prepared_counts": Jsonb(None)}),
        ]
        for label, values in refused:
            exc = _refused(label, psycopg.errors.CheckViolation, lambda v=values: _offering(conn, **v),
                           constraint="offerings_purge_code_check", table="offerings")
            _assert_detail_without_text(exc, label)
        exc = _refused("purge-keeping-the-code", psycopg.errors.CheckViolation,
                       lambda: conn.execute("UPDATE offerings SET purged_at = %s, purge_result = %s WHERE id = %s",
                                            (T2, Jsonb(PURGE_RESULT), prepared)),
                       constraint="offerings_purge_code_check", table="offerings")
        _assert_detail_without_text(exc, "purge-keeping-the-code")
        assert conn.execute(
            "SELECT purge_code_sha256, purge_code_expires_at, purge_prepared_counts, purged_at FROM offerings "
            "WHERE id = %s", (prepared,)).fetchone() == (code["purge_code_sha256"], T3, PREPARED_COUNTS, None)
        conn.execute("UPDATE offerings SET purge_code_sha256 = NULL, purge_code_expires_at = NULL, "
                     "purge_prepared_counts = NULL, purged_at = %s, purge_result = %s WHERE id = %s",
                     (T2, Jsonb(PURGE_RESULT), prepared))
        assert conn.execute(
            "SELECT purge_code_sha256, purge_code_expires_at, purge_prepared_counts, purged_at, purge_result "
            "FROM offerings WHERE id = %s", (prepared,)).fetchone() == (None, None, None, T2, PURGE_RESULT)


# ---------------------------------------------------------------------------------------------
# `delivery_endpoints.chat_id` anulável
# ---------------------------------------------------------------------------------------------


def test_endpoint_chat_id_accepts_null_and_the_partial_unique_index_still_holds(empty_db_url: str) -> None:
    """O expurgo anula o identificador do chat (esquema do CLI): um endereço com
    `chat_id` nulo é aceito no `INSERT` e no `UPDATE`; dois endereços vigentes (`active` e `blocked`) com
    `chat_id` nulo no mesmo bot são aceitos pelo índice único parcial `delivery_endpoints_current_chat_idx`,
    e dois vigentes com o mesmo `chat_id` não nulo no mesmo bot continuam recusados por ele, com o `DETAIL`
    só com a chave; o mesmo `chat_id` num endereço `inactive` e em outro bot continua aceito."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _, bot, person = _seed_identity_and_texts(conn)
        other_bot = _bot(conn, _offering(conn))
        nulls = [_endpoint(conn, _person(conn), bot, chat_id=None),
                 _endpoint(conn, _person(conn), bot, chat_id=None),
                 _endpoint(conn, _person(conn), bot, chat_id=None, state="blocked")]
        chat = 5_699_000_001
        first = _endpoint(conn, _person(conn), bot, chat_id=chat)
        exc = _refused("same-chat-same-bot", psycopg.errors.UniqueViolation,
                       lambda: _endpoint(conn, _person(conn), bot, chat_id=chat, state="blocked"),
                       constraint="delivery_endpoints_current_chat_idx", table="delivery_endpoints")
        _assert_detail_without_text(exc, "same-chat-same-bot",
                                    f"Key (bot_id, chat_id)=({bot}, {chat}) already exists.")
        inactive = _endpoint(conn, _person(conn), bot, chat_id=chat, state="inactive")
        elsewhere = _endpoint(conn, _person(conn), other_bot, chat_id=chat)
        # O `UPDATE` do expurgo: o endereço vigente do aluno perde o chat, e o número fica livre no bot.
        conn.execute("UPDATE delivery_endpoints SET chat_id = NULL WHERE person_id = %s", (person,))
        conn.execute("UPDATE delivery_endpoints SET chat_id = NULL WHERE id = %s", (first,))
        again = _endpoint(conn, _person(conn), bot, chat_id=chat)
        rows = conn.execute("SELECT id, chat_id, state FROM delivery_endpoints WHERE id = ANY(%s) ORDER BY id",
                            ([*nulls, first, inactive, elsewhere, again],)).fetchall()
        assert conn.execute("SELECT count(*) FROM delivery_endpoints WHERE person_id = %s AND chat_id IS NULL",
                            (person,)).fetchone() == (1,)
    assert rows == [(nulls[0], None, "active"), (nulls[1], None, "active"), (nulls[2], None, "blocked"),
                    (first, None, "active"), (inactive, chat, "inactive"), (elsewhere, chat, "active"),
                    (again, chat, "active")]


# ---------------------------------------------------------------------------------------------
# Tipos de evento
# ---------------------------------------------------------------------------------------------


def test_event_type_check_accepts_exactly_the_48_types(empty_db_url: str) -> None:
    """Esquema do CLI (o `events_type_check` recriado com o mesmo nome e os 48 tipos,
    (`offering.purge_prepared` e `offering.purged`)): cada um dos 46 tipos anteriores e dos 2 novos é aceito;
    um tipo fora deles (outra caixa, um prefixo, nomes vizinhos dos novos e o nome do comando) é recusado
    por esse CHECK, com o `DETAIL` sem texto."""
    _migrated(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        refused_types = []
        for event_type in ALL_EVENT_TYPES:
            try:
                _insert(conn, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                        data=Jsonb({}))
            except psycopg.errors.CheckViolation:
                refused_types.append(event_type)
        assert refused_types == [], "tipos recusados pelo CHECK de eventos (o esquema exige os 48)"
        for label, event_type in (("other-case", "Offering.purged"), ("prefix", "offering.purge"),
                                  ("neighbor-confirmed", "offering.purge_confirmed"),
                                  ("neighbor-prepared", "offering.purge_prepare"),
                                  ("neighbor-plural", "offering.purges"), ("command", "offerings.purge"),
                                  ("trailing-space", "offering.purged ")):
            exc = _refused(label, psycopg.errors.CheckViolation,
                           lambda t=event_type: _insert(conn, "events", key="seq", type=t, occurred_at=T0,
                                                        actor_token_id=None, data=Jsonb({})),
                           constraint="events_type_check", table="events")
            _assert_detail_without_text(exc, label)
        types = [row[0] for row in conn.execute("SELECT type FROM events ORDER BY seq").fetchall()]
    assert types == list(ALL_EVENT_TYPES)


# ---------------------------------------------------------------------------------------------
# Migração sobre dados anteriores
# ---------------------------------------------------------------------------------------------


def _seed_report(conn: psycopg.Connection) -> None:
    """Dados anteriores ao CLI por SQL direto nas tabelas que o expurgo toca e nas vizinhas, com estados
    variados: oferecimentos nos 4 estados (o encerrado com o código de arquivamento pendente), bots, alunos
    com identidade, endereços (`active` e `blocked`), updates, mensagens livres, fila de saída, contato e
    eventos."""
    _, _, person = _seed_identity_and_texts(conn)
    for values in OTHER_STATES.values():
        other = _offering(conn, **values)
        other_bot = _bot(conn, other)
        student = _person(conn)
        _insert(conn, "identities", key="person_id", person_id=student, full_name="Pessoa Neutra",
                ra=None, email=None, email_normalized=None)
        _student(conn, other, student)
        _endpoint(conn, student, other_bot, state="blocked" if values["state"] == "closed" else "active")
        _update(conn, other_bot)
    conn.execute("UPDATE offerings SET archive_code_sha256 = %s, archive_code_expires_at = %s "
                 "WHERE state = 'closed'", (hashlib.sha256(b"arquivamento").hexdigest(), T3))
    _insert(conn, "outbox_messages", recipient_person_id=person, text="mensagem neutra da API base",
            state="sent", attempts=1, available_at=T0, created_at=T0, sent_at=T1, channel_ref="console:1")
    _insert(conn, "outbox_messages", recipient_person_id=person, text="mensagem neutra pendente",
            state="pending", attempts=0, available_at=T0, created_at=T0)
    for event_type, data in (("offering.created", {"offering_id": str(uuid.uuid4())}),
                             ("report.frozen", {"dispatch_id": str(uuid.uuid4())}),
                             ("offering.digest_changed", {"offering_id": str(uuid.uuid4())})):
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb(data))


def test_0006_over_report_database_preserves_rows(empty_db_url: str) -> None:
    """Esquema do CLI (a migração só acrescenta; as anteriores não mudam):
    um banco em `0005` com dados anteriores recebe a `0006` (`apply_up_to(…, "0006") == ["0006"]`); cada
    linha das 29 tabelas do esquema anterior fica igual em todas as colunas anteriores (os CHECKs novos e o
    `events_type_check` recriado aceitam as linhas existentes), as 5 colunas novas de `offerings` ficam
    nulas, nenhum `chat_id` muda, `schema_migrations` ganha a `0006`, e aplicar de novo não muda nada."""
    assert apply_up_to(empty_db_url, "0005") == ["0001", "0002", "0003", "0004", "0005"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_report(conn)
        r6_columns = _table_columns(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in r6_columns.items()}
        counts = {table: _count(conn, table) for table in r6_columns}
    assert set(r6_columns) == REPORT_SCHEMA_TABLES
    seeded = {"people", "identities", "events", "outbox_messages", "offerings", "bots", "enrollments",
              "delivery_endpoints", "telegram_updates", "free_messages", "offering_contacts"}
    assert all(counts[table] > 0 for table in seeded), counts
    assert counts["offerings"] == 4

    assert apply_up_to(empty_db_url, "0006") == ["0006"]

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after = {table: _snapshot(conn, table, columns) for table, columns in r6_columns.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:5] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004", "0005",
                                                                  "0006"]
        assert conn.execute(
            "SELECT purged_at, purge_result, purge_code_sha256, purge_code_expires_at, purge_prepared_counts "
            "FROM offerings ORDER BY id").fetchall() == [(None,) * 5] * counts["offerings"]
        assert conn.execute("SELECT count(*) FROM delivery_endpoints WHERE chat_id IS NULL").fetchone() == (0,)
        stable_columns = _table_columns(conn)
        stable = {table: _snapshot(conn, table, columns) for table, columns in stable_columns.items()}
    assert set(stable_columns) == REPORT_SCHEMA_TABLES

    assert apply_up_to(empty_db_url, "0006") == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _table_columns(conn) == stable_columns
        assert {table: _snapshot(conn, table, columns)
                for table, columns in stable_columns.items()} == stable
