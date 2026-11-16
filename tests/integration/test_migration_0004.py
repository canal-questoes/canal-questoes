"""Migração `0004` (o disparo e a resposta) sobre o esquema do vínculo do aluno (lista e convites).



Cobre:
- o que a `0004` acrescenta ao esquema anterior, e só isso, medido por `apply_up_to(…, "0003")` e
  `apply_up_to(…, "0004")` (`tests/helpers/db.py`), **sem** afirmar `EXPECTED_VERSION`
  (a versão vigente só é afirmada onde já está): as 7 tabelas novas, as colunas novas de
  `outbox_messages` e `telegram_updates` com tipo, nulidade, identity e padrão exatos, `outbox_messages.text`
  anulável, os índices e os CHECKs só onde declarados (nenhum CHECK nas tabelas com texto do aluno
  ou de terceiros não redigido: `question_refs`, `delivery_contents`, `media_blobs`,
  `submissions`, `free_messages`; nem nas tabelas com identidade), a chave primária de cada tabela nova e
  as 13 FKs novas;
- a `0004` aplicada sobre um banco em `0003` com dados anteriores preserva cada linha e as sequências, com as
  colunas novas nulas (e `priority = 0`);
- cada CHECK com controle positivo e linha-limite: `dispatches_window_check`,
  `dispatches_state_outcome_check`, `confirm_deadline` ⇔ `validated_at`, os de `deliveries` (motivos se e
  só se o estado, `(sent_at IS NULL) = (principal_message_id IS NULL)`, `answered_at`, `current_field`
  aceito numa `pending` com `variant_at` e recusado sem `variant_at` e em estado terminal,
  `revealed_at` aceito em `question_failed`), `outbox_messages_state_check` com `cancelled` e
  `outbox_messages_content_check`; os conjuntos de valores e os tipos JSON;
- `events_type_check` com os 42 tipos; os índices únicos parciais (`submissions_one_final_idx`,
  `submissions_one_grading_idx`, `outbox_messages_pending_edit_key`) e
  `deliveries_dispatch_enrollment_key` recusando a duplicata; `question_refs_key` pelo resumo do `repo`
  (um `repo` de 2048 caracteres multibyte cabe no índice);
- o `DETAIL` das violações nas tabelas com texto (índice único, chave primária e FK) sem o texto do aluno
  nem o texto de terceiros não redigido (nessas tabelas não há CHECK, cuja violação
  mostraria a linha inteira; a prévia e os motivos de `dispatches` são redigidos na gravação).

O esquema exato de colunas, chaves e FKs também é conferido em `test_migrations.py`, com a mudança
de `outbox_messages.text` para anulável. As linhas de apoio são gravadas por SQL direto, com as colunas do esquema, da API base ao
disparo; horários fixos em UTC; ids internos, seeds e `button_token` nunca como literal esperado. Os nomes de
restrição conferidos exatamente são os que a migração declara; os demais CHECKs de `dispatches` e
de `deliveries` são conferidos pelo prefixo da tabela. No esqueleto (sem a `0004`), todo teste deste
arquivo falha pela migração ou pela tabela ausente, ou pelo tipo de evento ausente.

Mensagens livres opcionais por oferecimento, desligadas por padrão: a `0004`
também acrescenta a coluna
`offerings.free_messages` (`boolean NOT NULL DEFAULT false`: as linhas anteriores ficam desligadas, e o `INSERT`
de oferecimento sem a coluna continua válido) e a tabela `offering_contacts` (sem CHECK: o e-mail do contato não é
redigido; chave primária `id`, `offering_id` único com FK para `offerings.id`), e o `events_type_check`
passa a 43 tipos (o 14º novo, `offering.settings_changed`). Por isso: `DISPATCH_NEW_COLUMNS` com a coluna e as 4
colunas novas, `DISPATCH_TABLES` com a tabela, `DISPATCH_NEW_INDEXES` com a chave primária e a unicidade, a FK nova na
contagem das restrições, as linhas anteriores de `offerings` com `free_messages = false` depois da migração, e a
função dos tipos de evento, `test_event_type_check_accepts_exactly_the_43_types`. O conjunto
das tabelas com CHECK novo não muda (`offering_contacts` fica sem CHECK). Sem a coluna, a tabela e o tipo,
esses casos falham.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import re
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
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
T4 = T0 + timedelta(hours=4)
# Horários do disparo agendado: a abertura e o fechamento informados no preparo.
OPENS = T0 + timedelta(days=1)
CLOSES = OPENS + timedelta(hours=1)

# Os 29 tipos anteriores e os 13 do disparo (do esquema de cada migração), como literais (e não `EventType`).
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
    "submission.internal_failure",
    # A configuração de mensagens livres do oferecimento alterada.
    "offering.settings_changed",
)
assert len(PRE_DISPATCH_EVENT_TYPES) == 29 and len(DISPATCH_EVENT_TYPES) == 14

DISPATCH_TABLES = {"question_refs", "dispatches", "deliveries", "delivery_contents", "media_blobs",
             "submissions", "free_messages",
             # O contato do professor.
             "offering_contacts"}

# O esquema do disparo: (tabela, coluna) -> (tipo em `udt_name`, anulável, identity, padrão), para cada
# coluna que a `0004` acrescenta. `int8` = bigint, `int4` = integer, `int2` = smallint, `float8` =
# double precision, `_text` = text[].
DISPATCH_NEW_COLUMNS: dict[tuple[str, str], tuple[str, bool, bool, str | None]] = {
    # `question_refs`
    ("question_refs", "id"): ("int8", False, True, None),
    ("question_refs", "ref_uuid"): ("uuid", False, False, None),
    ("question_refs", "repo"): ("text", False, False, None),
    ("question_refs", "repo_sha256"): ("text", False, False, None),
    ("question_refs", "commit_sha"): ("text", False, False, None),
    ("question_refs", "qid"): ("text", False, False, None),
    ("question_refs", "supported"): ("bool", True, False, None),
    ("question_refs", "show_correct_answer"): ("bool", True, False, None),
    ("question_refs", "single_variant"): ("bool", True, False, None),
    ("question_refs", "partial_credit"): ("bool", True, False, None),
    ("question_refs", "title"): ("text", True, False, None),
    ("question_refs", "answers_names"): ("_text", True, False, None),
    ("question_refs", "content_hash"): ("text", True, False, None),
    ("question_refs", "inspected_at"): ("timestamptz", True, False, None),
    ("question_refs", "created_at"): ("timestamptz", False, False, None),
    # `dispatches`
    ("dispatches", "id"): ("int8", False, True, None),
    ("dispatches", "dispatch_uuid"): ("uuid", False, False, None),
    ("dispatches", "offering_id"): ("int8", False, False, None),
    ("dispatches", "question_ref_id"): ("int8", False, False, None),
    ("dispatches", "origin"): ("text", False, False, None),
    ("dispatches", "immediate"): ("bool", False, False, None),
    ("dispatches", "opens_at"): ("timestamptz", True, False, None),
    ("dispatches", "closes_at"): ("timestamptz", True, False, None),
    ("dispatches", "window_minutes"): ("int4", True, False, None),
    ("dispatches", "state"): ("text", False, False, None),
    ("dispatches", "preview"): ("jsonb", True, False, None),
    ("dispatches", "rejection"): ("text", True, False, None),
    ("dispatches", "rejection_reasons"): ("jsonb", True, False, None),
    ("dispatches", "cancel_reason"): ("text", True, False, None),
    ("dispatches", "created_at"): ("timestamptz", False, False, None),
    ("dispatches", "validated_at"): ("timestamptz", True, False, None),
    ("dispatches", "confirm_deadline"): ("timestamptz", True, False, None),
    ("dispatches", "confirmed_at"): ("timestamptz", True, False, None),
    ("dispatches", "opened_at"): ("timestamptz", True, False, None),
    ("dispatches", "closed_at"): ("timestamptz", True, False, None),
    ("dispatches", "rejected_at"): ("timestamptz", True, False, None),
    ("dispatches", "cancelled_at"): ("timestamptz", True, False, None),
    # `deliveries`
    ("deliveries", "id"): ("int8", False, True, None),
    ("deliveries", "delivery_uuid"): ("uuid", False, False, None),
    ("deliveries", "dispatch_id"): ("int8", False, False, None),
    ("deliveries", "enrollment_id"): ("int8", False, False, None),
    ("deliveries", "delivery_endpoint_id"): ("int8", False, False, None),
    ("deliveries", "seed"): ("int8", False, False, None),
    ("deliveries", "button_token"): ("text", False, False, None),
    ("deliveries", "state"): ("text", False, False, None),
    ("deliveries", "undelivered_reason"): ("text", True, False, None),
    ("deliveries", "failure"): ("text", True, False, None),
    ("deliveries", "current_field"): ("int2", True, False, None),
    ("deliveries", "render_version"): ("int4", False, False, "0"),
    ("deliveries", "controls_version"): ("int4", False, False, "0"),
    ("deliveries", "principal_message_id"): ("int8", True, False, None),
    ("deliveries", "created_at"): ("timestamptz", False, False, None),
    ("deliveries", "variant_at"): ("timestamptz", True, False, None),
    ("deliveries", "sent_at"): ("timestamptz", True, False, None),
    ("deliveries", "answered_at"): ("timestamptz", True, False, None),
    ("deliveries", "revealed_at"): ("timestamptz", True, False, None),
    # `delivery_contents`
    ("delivery_contents", "delivery_id"): ("int8", False, False, None),
    ("delivery_contents", "snapshot"): ("jsonb", False, False, None),
    ("delivery_contents", "ir"): ("jsonb", False, False, None),
    ("delivery_contents", "media_refs"): ("jsonb", False, False, None),
    ("delivery_contents", "rendered"): ("jsonb", False, False, None),
    ("delivery_contents", "parts"): ("jsonb", False, False, None),
    ("delivery_contents", "selection"): ("jsonb", False, False, None),
    ("delivery_contents", "current_render"): ("jsonb", True, False, None),
    # `media_blobs`
    ("media_blobs", "sha256"): ("text", False, False, None),
    ("media_blobs", "content_type"): ("text", False, False, None),
    ("media_blobs", "size_bytes"): ("int4", False, False, None),
    ("media_blobs", "data"): ("bytea", False, False, None),
    ("media_blobs", "created_at"): ("timestamptz", False, False, None),
    # `submissions`
    ("submissions", "id"): ("int8", False, True, None),
    ("submissions", "submission_uuid"): ("uuid", False, False, None),
    ("submissions", "delivery_id"): ("int8", False, False, None),
    ("submissions", "telegram_update_id"): ("int8", False, False, None),
    ("submissions", "outcome"): ("text", False, False, None),
    ("submissions", "valid"): ("bool", True, False, None),
    ("submissions", "refusal"): ("text", True, False, None),
    ("submissions", "failure_code"): ("text", True, False, None),
    ("submissions", "failure_phase"): ("text", True, False, None),
    ("submissions", "late"): ("bool", True, False, None),
    ("submissions", "normalized_fields"): ("_text", False, False, "'{}'::text[]"),
    ("submissions", "raw_input"): ("jsonb", False, False, None),
    ("submissions", "submitted_answers"): ("jsonb", True, False, None),
    ("submissions", "format_errors"): ("jsonb", True, False, None),
    ("submissions", "feedback"): ("jsonb", True, False, None),
    ("submissions", "params"): ("jsonb", True, False, None),
    ("submissions", "partial_scores"): ("jsonb", True, False, None),
    ("submissions", "correct_answers"): ("jsonb", True, False, None),
    ("submissions", "score"): ("float8", True, False, None),
    ("submissions", "part_derived"): ("jsonb", True, False, None),
    ("submissions", "engine_mismatch"): ("bool", True, False, None),
    ("submissions", "pl_mismatch"): ("bool", True, False, None),
    ("submissions", "response_ms"): ("int8", True, False, None),
    ("submissions", "delay_notice_sent"): ("bool", False, False, "false"),
    ("submissions", "received_at"): ("timestamptz", False, False, None),
    ("submissions", "created_at"): ("timestamptz", False, False, None),
    ("submissions", "graded_at"): ("timestamptz", True, False, None),
    # `free_messages` (`ambiguous boolean not null`)
    ("free_messages", "id"): ("int8", False, True, None),
    ("free_messages", "offering_id"): ("int8", False, False, None),
    ("free_messages", "delivery_endpoint_id"): ("int8", False, False, None),
    ("free_messages", "telegram_update_id"): ("int8", False, False, None),
    ("free_messages", "text"): ("text", False, False, None),
    ("free_messages", "replied"): ("bool", False, False, None),
    ("free_messages", "ambiguous"): ("bool", False, False, None),
    ("free_messages", "received_at"): ("timestamptz", False, False, None),
    ("free_messages", "created_at"): ("timestamptz", False, False, None),
    # `outbox_messages` (colunas novas; `priority smallint NOT NULL DEFAULT 0`)
    ("outbox_messages", "delivery_id"): ("int8", True, False, None),
    ("outbox_messages", "delivery_role"): ("text", True, False, None),
    ("outbox_messages", "operation"): ("text", True, False, None),
    ("outbox_messages", "render_version"): ("int4", True, False, None),
    ("outbox_messages", "media_id"): ("text", True, False, None),
    ("outbox_messages", "field_index"): ("int2", True, False, None),
    ("outbox_messages", "priority"): ("int2", False, False, "0"),
    # `telegram_updates`
    ("telegram_updates", "reply_to_message_id"): ("int8", True, False, None),
    # `offerings` (mensagens livres opcionais; `free_messages boolean NOT NULL DEFAULT false`)
    ("offerings", "free_messages"): ("bool", False, False, "false"),
    # `offering_contacts` (sem CHECK)
    ("offering_contacts", "id"): ("int8", False, True, None),
    ("offering_contacts", "offering_id"): ("int8", False, False, None),
    ("offering_contacts", "email"): ("text", False, False, None),
    ("offering_contacts", "updated_at"): ("timestamptz", False, False, None),
}

# A única coluna existente que muda: `outbox_messages.text` passa a aceitar nulo.
DISPATCH_CHANGED_COLUMNS = {("outbox_messages", "text"): ("text", True, False, None)}

# O esquema do disparo: os índices que a `0004` cria, por (tabela, colunas-chave em ordem,
# único, primário, parcial), como lista ordenada (as duas unicidades parciais de `submissions` têm a
# mesma forma).
DISPATCH_NEW_INDEXES = sorted([
    ("question_refs", ("id",), True, True, False),
    ("question_refs", ("ref_uuid",), True, False, False),
    ("question_refs", ("repo_sha256", "commit_sha", "qid"), True, False, False),
    ("dispatches", ("id",), True, True, False),
    ("dispatches", ("dispatch_uuid",), True, False, False),
    ("dispatches", ("offering_id", "question_ref_id"), False, False, False),
    ("dispatches", ("id",), False, False, True),
    ("deliveries", ("id",), True, True, False),
    ("deliveries", ("delivery_uuid",), True, False, False),
    ("deliveries", ("button_token",), True, False, False),
    ("deliveries", ("dispatch_id", "enrollment_id"), True, False, False),
    ("deliveries", ("enrollment_id",), False, False, False),
    ("deliveries", ("dispatch_id",), False, False, True),
    ("delivery_contents", ("delivery_id",), True, True, False),
    ("media_blobs", ("sha256",), True, True, False),
    ("submissions", ("id",), True, True, False),
    ("submissions", ("submission_uuid",), True, False, False),
    ("submissions", ("telegram_update_id",), True, False, False),
    ("submissions", ("delivery_id",), False, False, False),
    ("submissions", ("delivery_id",), True, False, True),
    ("submissions", ("delivery_id",), True, False, True),
    ("free_messages", ("id",), True, True, False),
    ("free_messages", ("telegram_update_id",), True, False, False),
    ("free_messages", ("offering_id",), False, False, False),
    ("free_messages", ("delivery_endpoint_id",), False, False, False),
    ("outbox_messages", ("delivery_id", "delivery_role"), True, False, True),
    ("outbox_messages", ("delivery_id", "id"), False, False, True),
    ("outbox_messages", ("delivery_endpoint_id", "channel_ref"), False, False, True),
    ("telegram_updates", ("bot_id",), False, False, True),
    # `offering_contacts`: a chave primária e no máximo um contato por oferecimento.
    ("offering_contacts", ("id",), True, True, False),
    ("offering_contacts", ("offering_id",), True, False, False),
])
# Os índices com nome declarado no esquema do disparo: nome -> (tabela, colunas, único, parcial) e, nos
# parciais, os literais de texto do predicado (conferidos pelo conjunto, sem depender da formatação de
# `pg_get_expr`).
DISPATCH_NAMED_INDEXES: dict[str, tuple[str, tuple[str, ...], bool, bool]] = {
    "question_refs_key": ("question_refs", ("repo_sha256", "commit_sha", "qid"), True, False),
    "dispatches_offering_ref_idx": ("dispatches", ("offering_id", "question_ref_id"), False, False),
    "dispatches_live_idx": ("dispatches", ("id",), False, True),
    "deliveries_dispatch_enrollment_key": ("deliveries", ("dispatch_id", "enrollment_id"), True, False),
    "deliveries_enrollment_idx": ("deliveries", ("enrollment_id",), False, False),
    "deliveries_live_idx": ("deliveries", ("dispatch_id",), False, True),
    "submissions_delivery_idx": ("submissions", ("delivery_id",), False, False),
    "submissions_one_final_idx": ("submissions", ("delivery_id",), True, True),
    "submissions_one_grading_idx": ("submissions", ("delivery_id",), True, True),
    "outbox_messages_pending_edit_key": ("outbox_messages", ("delivery_id", "delivery_role"), True, True),
    "outbox_messages_delivery_open_idx": ("outbox_messages", ("delivery_id", "id"), False, True),
    "outbox_messages_endpoint_ref_idx": ("outbox_messages", ("delivery_endpoint_id", "channel_ref"),
                                         False, True),
    "telegram_updates_pending_idx": ("telegram_updates", ("bot_id",), False, True),
}
DISPATCH_PREDICATE_LITERALS: dict[str, set[str]] = {
    "dispatches_live_idx": {"validating", "scheduled", "open"},
    "deliveries_live_idx": {"pending", "delivered", "no_answer"},
    "submissions_one_final_idx": {"graded", "dont_know"},
    "submissions_one_grading_idx": {"grading"},
    "outbox_messages_pending_edit_key": {"edit_text", "edit_markup", "pending"},
    "outbox_messages_delivery_open_idx": {"pending", "leased"},
    "outbox_messages_endpoint_ref_idx": set(),
    "telegram_updates_pending_idx": {"pending"},
}

# As tabelas que guardam identidade e as que guardam texto do aluno ou de terceiros não
# redigido não recebem CHECK.
IDENTITY_TABLES = {"people", "identities", "delivery_endpoints", "telegram_updates"}
TEXT_TABLES = {"question_refs", "delivery_contents", "media_blobs", "submissions", "free_messages"}

# Os conjuntos de valores de `dispatches` e `deliveries`.
ORIGINS = ("cli", "api", "mcp", "integration")
REJECTIONS = ("repository_unreachable", "repository_refused", "commit_not_found", "question_not_found",
              "question_unsupported", "question_broken", "engine_unavailable", "channel_limits_exceeded",
              "internal_failure")
UNDELIVERED_REASONS = ("endpoint_blocked", "channel_refused", "engine_unavailable", "bot_no_access",
                       "channel_unavailable")
FAILURES = ("variant_broken", "variant_unsupported", "channel_limits_exceeded", "grading_broken",
            "engine_rejected")
DELIVERY_ROLES = ("question", "media", "field_prompt", "fallback", "notice")
OPERATIONS = ("send", "edit_text", "edit_markup")

# Textos sentinela (texto do aluno e texto de terceiros não redigido) para a checagem do `DETAIL`.
STUDENT_SENTINEL = "resposta-sentinela-do-aluno-5c1d"
THIRD_PARTY_SENTINEL = "enunciado-sentinela-terceiro-9e4b"

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


def _refused(label: str, error: type[psycopg.Error], insert: Callable[[], Any], *,
             constraint: str | None = None, prefix: str | None = None,
             table: str | None = None) -> psycopg.Error:
    """O comando tem de ser recusado com exatamente `error` (a conexão é autocommit: a recusa desfaz
    só o próprio comando), pela restrição `constraint` (nome exato declarado na migração) ou por uma
    restrição cujo nome começa com `prefix`, e na tabela `table`, quando dados; devolve a exceção.
    Tabela ou coluna ausente (esquema anterior à `0004`) sobe como está."""
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
        return exc
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        raise
    except psycopg.Error as exc:
        pytest.fail(f"[{label}] recusado com {type(exc).__name__}, e não com {error.__name__}")
    pytest.fail(f"[{label}] aceito, mas o esquema exige a recusa ({error.__name__})")


def _person(conn: psycopg.Connection) -> int:
    n = next(_counter)
    return _insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"abe{n:09x}", created_at=T0)


def _offering(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": uuid.uuid4(), "code": f"oferta-disparo-{n}", "course": "Disciplina Neutra",
        "term": "2031s1", "timezone": "America/Sao_Paulo", "response_window_minutes": 60,
        "state": "active", "created_at": T0, "activated_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "offerings", **values)


def _bot(conn: psycopg.Connection, offering_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "bots", bot_uuid=uuid.uuid4(), offering_id=offering_id,
                   telegram_bot_id=6_400_000_000 + n, username=f"cq_disparo_{n:04d}_bot",
                   token_ciphertext=bytes([1]) + secrets.token_bytes(79), token_version=1,
                   state="registered", access_problem=None, profile_state="configured",
                   registered_at=T0)


def _student(conn: psycopg.Connection, offering_id: int) -> int:
    """Uma matrícula de aluno `active` com o termo aceito; devolve o id."""
    return _insert(conn, "enrollments", person_id=_person(conn), offering_id=offering_id, role="student",
                   state="active", created_at=T0, activated_at=T1, terms_version=1,
                   terms_accepted_at=T1)


def _endpoint(conn: psycopg.Connection, person_id: int, bot_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "delivery_endpoints", person_id=person_id, bot_id=bot_id, channel="telegram",
                   chat_id=5_400_000_000 + n, state="active", created_at=T0, changed_at=T0)


def _update(conn: psycopg.Connection, bot_id: int, *, text: str = "texto neutro",
            reply_to_message_id: int | None = None) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "bot_id": bot_id, "update_epoch": 0, "update_id": 830_000_000 + n, "kind": "message",
        "chat_id": 5_400_900_000 + n, "chat_type": "private", "sender_id": 5_400_900_000 + n,
        "text": text, "command": None, "details": Jsonb({"message_id": n, "date": 1930564800}),
        "received_at": T1, "state": "processed", "outcome": "answer_recorded", "processed_at": T1,
    }
    if reply_to_message_id is not None:
        values["reply_to_message_id"] = reply_to_message_id
    return _insert(conn, "telegram_updates", **values)


def _plain_message(conn: psycopg.Connection, person_id: int, **overrides: Any) -> int:
    """Uma mensagem da API base (forma (a): pessoa, sem bot, sem endereço, sem update), com texto."""
    values: dict[str, Any] = {"recipient_person_id": person_id, "text": "mensagem neutra da API base",
                              "state": "pending", "attempts": 0, "available_at": T0, "created_at": T0}
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _question_ref(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    repo = overrides.pop("repo", f"https://repos.cq.test/curso-neutro-{n}.git")
    values: dict[str, Any] = {
        "ref_uuid": uuid.uuid4(), "repo": repo,
        "repo_sha256": hashlib.sha256(repo.encode("utf-8")).hexdigest(),
        "commit_sha": hashlib.sha1(f"commit-{n}".encode()).hexdigest(),
        "qid": f"questoes/neutra-{n}", "created_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "question_refs", **values)


_PREVIEW = {"title": "Questão neutra", "statement": "Enunciado neutro.",
            "fields": [{"type": "number", "label": None, "suffix": None, "hint": "um número"}],
            "target_count": 1, "warnings": []}
_REASONS = [{"code": "question_code_error", "message": "falha neutra", "phase": "generate"}]


def _dispatch_values(base: str, immediate: bool) -> dict[str, Any]:
    """As colunas de um disparo coerente no estado `base`: `validating`,
    `rejected`, `scheduled` (validado, sem confirmação), `confirmed` (`scheduled` com a marca de
    confirmação), `open`, `closed` e `cancelled` (pelo operador, durante a validação). O agendado tem os
    dois horários desde o preparo; o imediato não confirmado tem só a janela padrão capturada (60 min)."""
    values: dict[str, Any] = {
        "dispatch_uuid": uuid.uuid4(), "origin": "api", "immediate": immediate, "state": base,
        "opens_at": None if immediate else OPENS, "closes_at": None if immediate else CLOSES,
        "window_minutes": 60 if immediate else None, "preview": None, "rejection": None,
        "rejection_reasons": None, "cancel_reason": None, "created_at": T0, "validated_at": None,
        "confirm_deadline": None, "confirmed_at": None, "opened_at": None, "closed_at": None,
        "rejected_at": None, "cancelled_at": None,
    }
    validated = {"validated_at": T1, "confirm_deadline": T1 + timedelta(hours=24) if immediate else OPENS,
                 "preview": Jsonb(_PREVIEW)}
    confirmed: dict[str, Any] = {"confirmed_at": T2}
    if immediate:
        confirmed |= {"opens_at": T2, "closes_at": T2 + timedelta(minutes=60)}
    if base == "rejected":
        values |= {"rejected_at": T1, "rejection": "question_broken", "rejection_reasons": Jsonb(_REASONS)}
    elif base == "scheduled":
        values |= validated
    elif base == "confirmed":
        values |= validated | confirmed | {"state": "scheduled"}
    elif base in ("open", "closed"):
        values |= validated | confirmed
        values["opened_at"] = values["opens_at"]
        if base == "closed":
            values["closed_at"] = values["closes_at"]
    elif base == "cancelled":
        values |= {"cancelled_at": T1, "cancel_reason": "operator"}
    else:
        assert base == "validating", base
    return values


@dataclass(frozen=True)
class Ctx:
    """Um oferecimento ativo com bot, uma pessoa com endereço, uma referência e um disparo aberto."""

    offering: int
    bot: int
    person: int
    endpoint: int
    ref: int
    dispatch: int


def _ctx(conn: psycopg.Connection) -> Ctx:
    offering = _offering(conn)
    bot = _bot(conn, offering)
    person = _person(conn)
    endpoint = _endpoint(conn, person, bot)
    ref = _question_ref(conn)
    dispatch = _dispatch(conn, Ctx(offering, bot, person, endpoint, ref, 0), "open")
    return Ctx(offering, bot, person, endpoint, ref, dispatch)


def _dispatch(conn: psycopg.Connection, ctx: Ctx, base: str = "validating", /, *,
              immediate: bool = False, **overrides: Any) -> int:
    values = _dispatch_values(base, immediate)
    values.update(overrides)
    return _insert(conn, "dispatches", offering_id=ctx.offering, question_ref_id=ctx.ref, **values)


def _button_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


# A entrega coerente em cada estado; `pending_variant` é a `pending` com a
# variante gravada (a transação da variante) e `principal` pede uma mensagem principal.
_DELIVERY_BY_STATE: dict[str, dict[str, Any]] = {
    "pending": {},
    "pending_variant": {"variant_at": T1, "current_field": 0},
    "delivered": {"variant_at": T1, "sent_at": T2, "principal": True, "current_field": 0},
    "not_delivered": {"undelivered_reason": "endpoint_blocked"},
    "answered": {"variant_at": T1, "sent_at": T2, "principal": True, "answered_at": T3},
    "dont_know": {"variant_at": T1, "sent_at": T2, "principal": True, "answered_at": T3},
    "no_answer": {"variant_at": T1, "sent_at": T2, "principal": True, "current_field": 1},
    "answered_late": {"variant_at": T1, "sent_at": T2, "principal": True, "answered_at": T4},
    "question_failed": {"failure": "variant_broken"},
}


def _delivery(conn: psycopg.Connection, ctx: Ctx, base: str = "pending", /, **overrides: Any) -> int:
    """Uma entrega coerente no estado `base` (com uma matrícula nova, salvo `enrollment_id`), com as
    colunas de `overrides` trocadas (inclusive o próprio `state`)."""
    extra = dict(_DELIVERY_BY_STATE[base])
    values: dict[str, Any] = {
        "delivery_uuid": uuid.uuid4(), "dispatch_id": ctx.dispatch,
        "delivery_endpoint_id": ctx.endpoint, "seed": secrets.randbelow(2**32),
        "button_token": _button_token(), "state": "pending" if base == "pending_variant" else base,
        "undelivered_reason": None, "failure": None, "current_field": None, "principal_message_id": None,
        "created_at": T0, "variant_at": None, "sent_at": None, "answered_at": None, "revealed_at": None,
    }
    if extra.pop("principal", False):
        values["principal_message_id"] = _plain_message(conn, ctx.person)
    values.update(extra)
    values.update(overrides)
    if "enrollment_id" not in values:
        values["enrollment_id"] = _student(conn, ctx.offering)
    return _insert(conn, "deliveries", **values)


def _delivery_message(conn: psycopg.Connection, ctx: Ctx, delivery_id: int | None, *,
                      role: str | None = "question", operation: str | None = "send",
                      state: str = "pending", **overrides: Any) -> int:
    """Uma mensagem de entrega: `delivery_id` preenchido, `text` nulo, papel e
    operação preenchidos e a forma (b) de oferecimentos e bots (pessoa, bot e endereço); `render_version` só nas edições,
    `media_id` só no papel `media` e `field_index` só no `field_prompt`."""
    values: dict[str, Any] = {
        "recipient_person_id": ctx.person, "bot_id": ctx.bot, "delivery_endpoint_id": ctx.endpoint,
        "text": None, "delivery_id": delivery_id, "delivery_role": role, "operation": operation,
        "render_version": 2 if operation in ("edit_text", "edit_markup") else None,
        "media_id": "m1" if role == "media" else None,
        "field_index": 0 if role == "field_prompt" else None,
        "state": state, "attempts": 0, "available_at": T0, "created_at": T0,
    }
    if state == "leased":
        values |= {"attempts": 1, "lease_owner": "worker-disparo", "lease_until": T1}
    elif state == "sent":
        values |= {"attempts": 1, "channel_ref": str(next(_counter)), "sent_at": T1}
    elif state == "failed":
        values |= {"attempts": 5, "last_error": "RetryLater"}
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _submission(conn: psycopg.Connection, ctx: Ctx, delivery_id: int, outcome: str,
                **overrides: Any) -> int:
    """Uma submissão com um update próprio (`telegram_update_id` é único)."""
    values: dict[str, Any] = {
        "submission_uuid": uuid.uuid4(), "delivery_id": delivery_id,
        "telegram_update_id": _update(conn, ctx.bot), "outcome": outcome,
        "valid": True if outcome == "graded" else None,
        "refusal": "already_answered" if outcome == "refused" else None,
        "failure_code": {"broken": "question_code_error", "internal_failure": "invalid_request"}.get(outcome),
        "late": False, "raw_input": Jsonb({"x": "3.14"}), "received_at": T2, "created_at": T2,
        "graded_at": T3 if outcome in ("graded", "broken", "internal_failure") else None,
    }
    values.update(overrides)
    return _insert(conn, "submissions", **values)


def _columns(url: str) -> dict[tuple[str, str], tuple[str, bool, bool, str | None]]:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, udt_name, is_nullable, is_identity, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'").fetchall()
    return {(table, column): (udt, nullable == "YES", identity == "YES", default)
            for table, column, udt, nullable, identity, default in rows}


def _indexes(url: str) -> set[tuple[str, str, tuple[str, ...], bool, bool, bool, int, str | None]]:
    """(tabela, índice, colunas-chave em ordem, único, primário, parcial, nº de chaves, predicado)."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname::text, ic.relname::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "      WHERE k.ord <= i.indnkeyatts ORDER BY k.ord), "
            "i.indisunique, i.indisprimary, i.indpred IS NOT NULL, i.indnkeyatts, "
            "pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'").fetchall()
    return {(table, index, tuple(cols), unique, primary, partial, nkeys, predicate)
            for table, index, cols, unique, primary, partial, nkeys, predicate in rows}


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


def _string_literals(predicate: str | None) -> set[str]:
    return set(re.findall(r"'([^']*)'", predicate or ""))


# ---------------------------------------------------------------------------------------------
# O que a 0004 acrescenta (delta por `apply_up_to`, sem afirmar `EXPECTED_VERSION`)
# ---------------------------------------------------------------------------------------------


def test_0004_adds_exactly_the_new_tables_and_columns(empty_db_url: str) -> None:
    """As mudanças no banco entram como uma nova migração versionada, aplicada como na API base;
    no esquema (as 7 tabelas novas; as 7 colunas novas de `outbox_messages`, só
    anuláveis ou com padrão, restrição da API base; `telegram_updates.reply_to_message_id`; `free_messages.ambiguous
    boolean not null`; `outbox_messages.text` passando a aceitar nulo): num banco em
    `0003`, a `0004` devolve `["0004"]`, acrescenta exatamente as colunas de `DISPATCH_NEW_COLUMNS` (tipo,
    nulidade, identity e padrão exatos) e as 7 tabelas, muda só a nulidade de `outbox_messages.text` e não
    remove nenhuma coluna anterior."""
    assert apply_up_to(empty_db_url, "0003") == ["0001", "0002", "0003"]
    before = _columns(empty_db_url)
    assert apply_up_to(empty_db_url, "0004") == ["0004"]
    after = _columns(empty_db_url)

    assert set(before) <= set(after), f"colunas removidas: {sorted(set(before) - set(after))}"
    changed = {key: after[key] for key in before if after[key] != before[key]}
    assert changed == DISPATCH_CHANGED_COLUMNS
    assert before[("outbox_messages", "text")] == ("text", False, False, None)
    added = {key: value for key, value in after.items() if key not in before}
    assert added == DISPATCH_NEW_COLUMNS
    assert {table for table, _ in after} - {table for table, _ in before} == DISPATCH_TABLES
    assert after[("free_messages", "ambiguous")] == ("bool", False, False, None)


def test_0004_adds_indexes_and_checks_only_where_declared(empty_db_url: str) -> None:
    """Esquema do disparo (princípio texto × CHECK): os índices
    novos são exatamente os de `DISPATCH_NEW_INDEXES` (nenhum removido), com os nomes declarados em
    `DISPATCH_NAMED_INDEXES` e os predicados dos parciais com os literais da migração; os CHECKs removidos
    são só os recriados (`events_type_check` com os 42 tipos e `outbox_messages_state_check` com
    `cancelled`); os novos ficam só em `dispatches` (nomes `dispatches_*`, inclusive
    `dispatches_window_check` e `dispatches_state_outcome_check`), `deliveries` (`deliveries_*`) e
    `outbox_messages` (exatamente `outbox_messages_state_check` e `outbox_messages_content_check`), e
    nenhum CHECK em `question_refs`, `delivery_contents`, `media_blobs`, `submissions`,
    `free_messages` nem nas tabelas com identidade; de resto, as 7 chaves primárias e as 13 FKs novas."""
    assert apply_up_to(empty_db_url, "0003") == ["0001", "0002", "0003"]
    indexes_before = _indexes(empty_db_url)
    constraints_before = _constraints(empty_db_url)
    assert apply_up_to(empty_db_url, "0004") == ["0004"]
    indexes_after = _indexes(empty_db_url)
    constraints_after = _constraints(empty_db_url)

    assert indexes_before - indexes_after == set()
    new_indexes = indexes_after - indexes_before
    assert all(len(cols) == nkeys for _, _, cols, _, _, _, nkeys, _ in new_indexes), new_indexes
    assert sorted((table, cols, unique, primary, partial)
                  for table, _, cols, unique, primary, partial, _, _ in new_indexes) == DISPATCH_NEW_INDEXES
    by_name = {name: (table, cols, unique, partial, predicate)
               for table, name, cols, unique, _, partial, _, predicate in new_indexes}
    assert set(DISPATCH_NAMED_INDEXES) <= set(by_name), sorted(set(DISPATCH_NAMED_INDEXES) - set(by_name))
    for name, shape in DISPATCH_NAMED_INDEXES.items():
        assert by_name[name][:4] == shape, (name, by_name[name])
    for name, literals in DISPATCH_PREDICATE_LITERALS.items():
        assert _string_literals(by_name[name][4]) == literals, (name, by_name[name][4])
    assert "delivery_id IS NOT NULL" in (by_name["outbox_messages_endpoint_ref_idx"][4] or "")
    assert "valid" in (by_name["submissions_one_final_idx"][4] or "")

    removed = constraints_before - constraints_after
    added = constraints_after - constraints_before
    assert {(table, name, kind) for table, name, kind, _ in removed} == {
        ("events", "events_type_check", "c"), ("outbox_messages", "outbox_messages_state_check", "c")}
    added_checks = {(table, name) for table, name, kind, _ in added if kind == "c"}
    assert {table for table, _ in added_checks} == {"dispatches", "deliveries", "outbox_messages", "events"}
    assert {name for table, name in added_checks if table == "outbox_messages"} == {
        "outbox_messages_state_check", "outbox_messages_content_check"}
    assert {name for table, name in added_checks if table == "events"} == {"events_type_check"}
    dispatch_checks = {name for table, name in added_checks if table == "dispatches"}
    assert {"dispatches_window_check", "dispatches_state_outcome_check"} <= dispatch_checks
    assert all(name.startswith("dispatches_") for name in dispatch_checks), dispatch_checks
    delivery_checks = {name for table, name in added_checks if table == "deliveries"}
    assert delivery_checks and all(name.startswith("deliveries_") for name in delivery_checks), delivery_checks
    all_checks = {table for table, _, kind, _ in constraints_after if kind == "c"}
    assert all_checks & (TEXT_TABLES | IDENTITY_TABLES) == set(), all_checks & (TEXT_TABLES | IDENTITY_TABLES)
    assert sorted((table, kind) for table, _, kind, _ in added if kind in ("p", "f")) == sorted(
        [(table, "p") for table in DISPATCH_TABLES]
        + [("dispatches", "f")] * 2 + [("deliveries", "f")] * 4 + [("delivery_contents", "f")]
        + [("submissions", "f")] * 2 + [("free_messages", "f")] * 3 + [("outbox_messages", "f")]
        # O contato do professor: `offering_contacts.offering_id` → `offerings.id`.
        + [("offering_contacts", "f")])
    assert {kind for _, _, kind, _ in added} <= {"c", "p", "f", "u"}, added


# ---------------------------------------------------------------------------------------------
# Migração sobre dados anteriores
# ---------------------------------------------------------------------------------------------


def _seed_student_link(conn: psycopg.Connection) -> None:
    """Dados anteriores ao disparo por SQL direto, nas 14 tabelas do esquema anterior, com estados variados."""
    people = [_person(conn) for _ in range(5)]
    _insert(conn, "identities", key="person_id", person_id=people[0], full_name="Pessoa Migrada Seis",
            ra="5500000306", email="pessoa.seis@exemplo.invalid",
            email_normalized="pessoa.seis@exemplo.invalid")
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token anterior",
            secret_sha256=hashlib.sha256(b"segredo anterior").hexdigest(),
            permissions=["offerings:read", "offerings:write"], created_at=T0, revoked_at=None)
    active = _offering(conn, invite_code_sha256=hashlib.sha256(b"envio").hexdigest(),
                       invite_code_expires_at=T1, invite_valid_hours=168, invite_eligible_count=2)
    _offering(conn, state="closed", activated_at=T1, closed_at=T2)
    _offering(conn, state="draft", activated_at=None)
    bot = _bot(conn, active)
    team = _insert(conn, "enrollments", person_id=people[1], offering_id=active, role="team",
                   state="active", created_at=T0, activated_at=T1)
    pending = _insert(conn, "enrollments", person_id=people[2], offering_id=active, role="student",
                      state="pending", created_at=T0)
    _insert(conn, "enrollments", person_id=people[3], offering_id=active, role="student",
            state="active", created_at=T0, activated_at=T2, terms_version=1, terms_accepted_at=T2)
    send = _insert(conn, "invite_sends", enrollment_id=pending, origin="batch", valid_hours=168,
                   state="sent", attempts=1, created_at=T0, finished_at=T1)
    update = _update(conn, bot, text="/aceito")
    _insert(conn, "invites", enrollment_id=team, code_sha256=hashlib.sha256(b"equipe").hexdigest(),
            state="used", expires_at=T3, created_at=T0, used_at=T1)
    _insert(conn, "invites", enrollment_id=pending, code_sha256=hashlib.sha256(b"aluno").hexdigest(),
            state="pending", expires_at=T3, created_at=T0, invite_send_id=send,
            presented_update_id=update, presented_terms_version=1)
    endpoint = _endpoint(conn, people[3], bot)
    _insert(conn, "telegram_updates", bot_id=bot, update_epoch=0, update_id=831_900_000, kind="message",
            chat_id=5_400_999_999, chat_type="private", sender_id=5_400_999_999, text="/start",
            command="/start", details=Jsonb({"message_id": 2, "date": 1930564801}), received_at=T1,
            state="pending", outcome=None, processed_at=None)
    _plain_message(conn, people[0])
    _insert(conn, "outbox_messages", recipient_person_id=people[3], bot_id=bot,
            delivery_endpoint_id=endpoint, text="boas-vindas", state="sent", attempts=1,
            available_at=T0, channel_ref="telegram:1", created_at=T0, sent_at=T1)
    _insert(conn, "outbox_messages", bot_id=bot, reply_to_update_id=update, text="resposta",
            idempotency_key="reply:migrada-0004", state="failed", attempts=3, available_at=T0,
            last_error="Forbidden", created_at=T0)
    for event_type in ("person.registered", "offering.created", "student.joined"):
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb({"origem": "migracao"}))
    for state, lease, finished in (("pending", None, None), ("leased", "worker-0004", None),
                                   ("done", None, T2)):
        _insert(conn, "tasks", task_type="invite.email", payload=Jsonb({"estado": state}),
                idempotency_key=f"migrada-0004:{state}", state=state,
                attempts=0 if state == "pending" else 1, available_at=T1, lease_owner=lease,
                lease_until=T2 if lease else None, last_error=None, created_at=T0,
                finished_at=finished)


def test_0004_over_student_link_database_preserves_rows_and_sequences(empty_db_url: str) -> None:
    """Esquema do disparo (colunas novas anuláveis ou com padrão, para
    os dados anteriores; restrição da API base): um banco em `0003` com dados anteriores recebe a
    `0004` (`apply_up_to(…, "0004") == ["0004"]`); cada linha das 14 tabelas fica igual em todas as
    colunas anteriores, as colunas novas de `outbox_messages` ficam nulas com `priority = 0`, a de
    `telegram_updates` fica nula, as 7 tabelas novas nascem vazias, `schema_migrations` ganha a `0004`, as
    sequências continuam depois do maior id, uma mensagem da API base (com texto e sem as colunas novas)
    continua aceita, e aplicar de novo não muda nada."""
    assert apply_up_to(empty_db_url, "0003") == ["0001", "0002", "0003"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        _seed_student_link(conn)
        r4_columns = _table_columns(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in r4_columns.items()}
        max_ids = {table: conn.execute(pgsql.SQL("SELECT max(id) FROM {}").format(
            pgsql.Identifier(table))).fetchone()[0]
            for table in ("people", "offerings", "outbox_messages", "telegram_updates", "tasks")}
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    assert set(r4_columns) == {"schema_migrations", "people", "identities", "api_tokens", "events",
                               "tasks", "outbox_messages", "offerings", "bots", "enrollments", "invites",
                               "delivery_endpoints", "telegram_updates", "invite_sends"}

    assert apply_up_to(empty_db_url, "0004") == ["0004"]

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after = {table: _snapshot(conn, table, columns) for table, columns in r4_columns.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:3] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004"]
        assert conn.execute(
            "SELECT delivery_id, delivery_role, operation, render_version, media_id, field_index, "
            "priority FROM outbox_messages ORDER BY id").fetchall() == [
            (None, None, None, None, None, None, 0)] * 3
        assert conn.execute(
            "SELECT reply_to_message_id FROM telegram_updates ORDER BY id").fetchall() == [(None,)] * 2
        # As linhas anteriores ficam com as mensagens livres desligadas.
        assert conn.execute("SELECT free_messages FROM offerings ORDER BY id").fetchall() == [(False,)] * 3
        for table in sorted(DISPATCH_TABLES):
            count = conn.execute(pgsql.SQL("SELECT count(*) FROM {}").format(
                pgsql.Identifier(table))).fetchone()[0]
            assert count == 0, table

        # As sequências continuam, e as formas anteriores continuam aceitas com as colunas de antes.
        person = _person(conn)
        assert person > max_ids["people"]
        offering = _offering(conn)
        assert offering > max_ids["offerings"]
        # o `INSERT` de oferecimento sem a coluna nova continua válido e fica desligado.
        assert conn.execute("SELECT free_messages FROM offerings WHERE id = %s",
                            (offering,)).fetchone() == (False,)
        message = _plain_message(conn, person)
        assert message > max_ids["outbox_messages"]
        assert conn.execute("SELECT text, priority, delivery_id FROM outbox_messages WHERE id = %s",
                            (message,)).fetchone() == ("mensagem neutra da API base", 0, None)
        update = _update(conn, _bot(conn, offering))
        assert update > max_ids["telegram_updates"]
        task = _insert(conn, "tasks", task_type="telegram.update", payload=Jsonb({}),
                       idempotency_key="depois-da-0004", state="pending", attempts=0, available_at=T3,
                       created_at=T3)
        assert task > max_ids["tasks"]
        seq = _insert(conn, "events", key="seq", type="student.joined", occurred_at=T3,
                      actor_token_id=None, data=Jsonb({"origem": "migracao"}))
        assert seq > max_seq
        stable_columns = _table_columns(conn)
        stable = {table: _snapshot(conn, table, columns) for table, columns in stable_columns.items()}

    assert apply_up_to(empty_db_url, "0004") == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _table_columns(conn) == stable_columns
        assert {table: _snapshot(conn, table, columns)
                for table, columns in stable_columns.items()} == stable


# ---------------------------------------------------------------------------------------------
# CHECKs de `dispatches`
# ---------------------------------------------------------------------------------------------


def test_dispatch_value_sets_and_json_types(sql: psycopg.Connection) -> None:
    """Esquema do disparo (CHECKs: origem, estado, classes e razões nos conjuntos;
    JSON com o tipo certo): cada `origin` (`cli` · `api` · `mcp` · `integration`) e cada classe de
    rejeição (as 9) são aceitas; valor fora dos conjuntos (inclusive os nomes superados
    e outra caixa), a prévia que não é objeto e os motivos que não são lista são recusados
    por um CHECK `dispatches_*`."""
    ctx = _ctx(sql)
    origins = [_dispatch(sql, ctx, origin=origin) for origin in ORIGINS]
    rejected = [_dispatch(sql, ctx, "rejected", rejection=reason) for reason in REJECTIONS]
    refused = [
        ("origin-unknown", "validating", {"origin": "web"}),
        ("origin-other-case", "validating", {"origin": "CLI"}),
        ("origin-empty", "validating", {"origin": ""}),
        ("state-unknown", "validating", {"state": "validated"}),
        ("state-other-case", "validating", {"state": "Validating"}),
        ("rejection-superseded-name", "rejected", {"rejection": "repo_unreachable"}),
        ("rejection-internal-error", "rejected", {"rejection": "internal_error"}),
        ("rejection-other-case", "rejected", {"rejection": "Question_broken"}),
        ("cancel-reason-unknown", "cancelled", {"cancel_reason": "superseded"}),
        ("cancel-reason-other-case", "cancelled", {"cancel_reason": "Operator"}),
        ("preview-array", "scheduled", {"preview": Jsonb([_PREVIEW])}),
        ("preview-string", "scheduled", {"preview": Jsonb("prévia")}),
        ("reasons-object", "rejected", {"rejection_reasons": Jsonb(_REASONS[0])}),
        ("reasons-string", "rejected", {"rejection_reasons": Jsonb("motivo")}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _dispatch(sql, ctx, b, **c), prefix="dispatches_")
    rows = sql.execute("SELECT id, state, origin, rejection FROM dispatches WHERE id <> %s ORDER BY id",
                       (ctx.dispatch,)).fetchall()
    assert rows == [(i, "validating", origin, None) for i, origin in zip(origins, ORIGINS)] + [
        (i, "rejected", "api", reason) for i, reason in zip(rejected, REJECTIONS)]


def test_dispatch_window_check(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`dispatches_window_check`: `window_minutes` de 5
    a 43 200; o agendado tem os dois horários desde o preparo e nenhum `window_minutes`; o imediato não
    confirmado tem exatamente uma fonte de fechamento; com os dois horários, `closes_at - opens_at` de 5
    minutos a 30 dias, sem outro CHECK de ordem entre horários): as formas coerentes e as
    linhas-limite (exatamente 5 minutos e 30 dias; 5 e 43 200 minutos) são aceitas; cada forma
    incoerente e cada valor fora dos limites (inclusive 1 s aquém e além) é recusado por esse CHECK."""
    ctx = _ctx(sql)
    second = timedelta(seconds=1)
    accepted = [
        ("validating", False, {}),
        ("validating", False, {"closes_at": OPENS + timedelta(minutes=5)}),
        ("validating", False, {"closes_at": OPENS + timedelta(days=30)}),
        ("validating", True, {}),
        ("validating", True, {"window_minutes": 5}),
        ("validating", True, {"window_minutes": 43200}),
        ("validating", True, {"window_minutes": None, "closes_at": T0 + timedelta(hours=2)}),
        ("confirmed", True, {}),
        ("confirmed", True, {"window_minutes": None, "closes_at": T2 + timedelta(minutes=5)}),
        ("confirmed", True, {"closes_at": T2 + timedelta(days=30)}),
    ]
    ids = [_dispatch(sql, ctx, base, immediate=immediate, **columns)
           for base, immediate, columns in accepted]
    refused = [
        ("window-4-minutes", True, {"window_minutes": 4}),
        ("window-0-minutes", True, {"window_minutes": 0}),
        ("window-43201-minutes", True, {"window_minutes": 43201}),
        ("scheduled-with-window", False, {"window_minutes": 60}),
        ("scheduled-without-closes", False, {"closes_at": None}),
        ("scheduled-without-opens", False, {"opens_at": None}),
        ("scheduled-without-both", False, {"opens_at": None, "closes_at": None}),
        ("immediate-with-two-sources", True, {"closes_at": T0 + timedelta(hours=2)}),
        ("immediate-without-source", True, {"window_minutes": None}),
        ("span-5-minutes-minus-1s", False, {"closes_at": OPENS + timedelta(minutes=5) - second}),
        ("span-30-days-plus-1s", False, {"closes_at": OPENS + timedelta(days=30) + second}),
        ("span-zero", False, {"closes_at": OPENS}),
        ("span-negative", False, {"closes_at": OPENS - timedelta(hours=1)}),
        ("immediate-span-4-minutes", True, {"opens_at": T2, "closes_at": T2 + timedelta(minutes=4)}),
    ]
    for label, immediate, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda i=immediate, c=columns: _dispatch(sql, ctx, immediate=i, **c),
                 constraint="dispatches_window_check")
    rows = sql.execute("SELECT id, immediate, window_minutes, closes_at - opens_at FROM dispatches "
                       "WHERE id <> %s ORDER BY id", (ctx.dispatch,)).fetchall()
    hour = timedelta(hours=1)
    assert rows == [
        (ids[0], False, None, hour), (ids[1], False, None, timedelta(minutes=5)),
        (ids[2], False, None, timedelta(days=30)), (ids[3], True, 60, None), (ids[4], True, 5, None),
        (ids[5], True, 43200, None), (ids[6], True, None, None), (ids[7], True, 60, hour),
        (ids[8], True, None, timedelta(minutes=5)), (ids[9], True, 60, timedelta(days=30))]


def test_dispatch_state_outcome_check(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`dispatches_state_outcome_check`:
    `validating` sem nenhum horário de transição, prévia ou motivo; `rejected` com `rejected_at` e
    `rejection`; `scheduled` com `validated_at`, `confirm_deadline` e `preview`, e `confirmed_at` só com
    `opens_at`; `open`/`closed` com `validated_at`, `preview`, `confirmed_at`, `opened_at`, `opens_at` e
    `closes_at`, e `closed_at` se e só se `closed`; `cancelled` com `cancelled_at` e `cancel_reason`,
    `expired` só sem confirmação e depois da validação, `window_missed` só confirmado, `validation_overran`
    só sem validação): um disparo coerente de cada estado e de cada razão de cancelamento (agendado e
    imediato; rejeitado com e sem motivos do motor) é aceito; cada forma incoerente é recusada por esse
    CHECK."""
    ctx = _ctx(sql)
    accepted = [
        ("validating", False, {}),
        ("validating", True, {}),
        ("rejected", False, {}),
        ("rejected", True, {"rejection": "repository_unreachable", "rejection_reasons": None}),
        ("scheduled", False, {}),
        ("scheduled", True, {}),
        ("confirmed", False, {}),
        ("confirmed", True, {}),
        ("open", False, {}),
        ("open", True, {}),
        ("closed", False, {}),
        ("closed", True, {}),
        ("cancelled", False, {}),
        ("cancelled", True, {}),
        ("scheduled", False, {"state": "cancelled", "cancelled_at": T2, "cancel_reason": "operator",
                              "confirmed_at": T1}),
        ("scheduled", False, {"state": "cancelled", "cancelled_at": T2, "cancel_reason": "offering_closed"}),
        ("scheduled", False, {"state": "cancelled", "cancelled_at": OPENS, "cancel_reason": "expired"}),
        ("scheduled", True, {"state": "cancelled", "cancelled_at": T2, "cancel_reason": "expired"}),
        ("confirmed", False, {"state": "cancelled", "cancelled_at": OPENS, "cancel_reason": "window_missed"}),
        ("cancelled", False, {"cancel_reason": "validation_overran", "cancelled_at": OPENS}),
    ]
    ids = [_dispatch(sql, ctx, base, immediate=immediate, **columns)
           for base, immediate, columns in accepted]
    refused = [
        ("validating-with-validated_at", False, "validating",
         {"validated_at": T1, "confirm_deadline": OPENS}),
        ("validating-with-preview", False, "validating", {"preview": Jsonb(_PREVIEW)}),
        ("validating-with-rejection", False, "validating", {"rejection": "question_broken"}),
        ("validating-with-reasons", False, "validating", {"rejection_reasons": Jsonb(_REASONS)}),
        ("validating-with-cancel_reason", False, "validating", {"cancel_reason": "operator"}),
        ("validating-with-rejected_at", False, "validating", {"rejected_at": T1}),
        ("validating-with-cancelled_at", False, "validating", {"cancelled_at": T1}),
        ("validating-with-confirmed_at", False, "validating", {"confirmed_at": T1}),
        ("validating-with-opened_at", False, "validating", {"opened_at": T1}),
        ("validating-with-closed_at", False, "validating", {"closed_at": T1}),
        ("rejected-without-rejected_at", False, "rejected", {"rejected_at": None}),
        ("rejected-without-rejection", False, "rejected", {"rejection": None}),
        ("scheduled-without-validation", False, "scheduled", {"validated_at": None, "confirm_deadline": None}),
        ("scheduled-without-preview", False, "scheduled", {"preview": None}),
        ("scheduled-immediate-confirmed-without-opens", True, "scheduled", {"confirmed_at": T2}),
        ("open-without-opened_at", False, "open", {"opened_at": None}),
        ("open-without-confirmed_at", False, "open", {"confirmed_at": None}),
        ("open-without-preview", False, "open", {"preview": None}),
        ("open-with-closed_at", False, "open", {"closed_at": CLOSES}),
        ("open-immediate-without-times", True, "open",
         {"opens_at": None, "closes_at": None, "opened_at": T2}),
        ("closed-without-closed_at", False, "closed", {"closed_at": None}),
        ("closed-without-opened_at", False, "closed", {"opened_at": None}),
        ("cancelled-without-cancelled_at", False, "cancelled", {"cancelled_at": None}),
        ("cancelled-without-reason", False, "cancelled", {"cancel_reason": None}),
        ("cancelled-expired-unvalidated", False, "cancelled", {"cancel_reason": "expired"}),
        ("cancelled-expired-confirmed", False, "confirmed",
         {"state": "cancelled", "cancelled_at": OPENS, "cancel_reason": "expired"}),
        ("cancelled-window-missed-unconfirmed", False, "scheduled",
         {"state": "cancelled", "cancelled_at": OPENS, "cancel_reason": "window_missed"}),
        ("cancelled-validation-overran-validated", False, "scheduled",
         {"state": "cancelled", "cancelled_at": OPENS, "cancel_reason": "validation_overran"}),
    ]
    for label, immediate, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda i=immediate, b=base, c=columns: _dispatch(sql, ctx, b, immediate=i, **c),
                 constraint="dispatches_state_outcome_check")
    rows = sql.execute("SELECT id, state, confirmed_at IS NOT NULL, cancel_reason, rejection "
                       "FROM dispatches WHERE id <> %s ORDER BY id", (ctx.dispatch,)).fetchall()
    assert rows == [
        (ids[0], "validating", False, None, None), (ids[1], "validating", False, None, None),
        (ids[2], "rejected", False, None, "question_broken"),
        (ids[3], "rejected", False, None, "repository_unreachable"),
        (ids[4], "scheduled", False, None, None), (ids[5], "scheduled", False, None, None),
        (ids[6], "scheduled", True, None, None), (ids[7], "scheduled", True, None, None),
        (ids[8], "open", True, None, None), (ids[9], "open", True, None, None),
        (ids[10], "closed", True, None, None), (ids[11], "closed", True, None, None),
        (ids[12], "cancelled", False, "operator", None), (ids[13], "cancelled", False, "operator", None),
        (ids[14], "cancelled", True, "operator", None), (ids[15], "cancelled", False, "offering_closed", None),
        (ids[16], "cancelled", False, "expired", None), (ids[17], "cancelled", False, "expired", None),
        (ids[18], "cancelled", True, "window_missed", None),
        (ids[19], "cancelled", False, "validation_overran", None)]


def test_dispatch_confirm_deadline_iff_validated(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`confirm_deadline IS NOT NULL` se e só se `validated_at IS NOT
    NULL`; agendado: `opens_at`; imediato: `validated_at + 24 h`): os validados com o prazo (agendado e
    imediato, inclusive cancelados depois da validação) e os não validados sem ele são aceitos; um
    validado sem prazo e um prazo sem validação são recusados por um CHECK `dispatches_*`."""
    ctx = _ctx(sql)
    scheduled = _dispatch(sql, ctx, "scheduled")
    immediate = _dispatch(sql, ctx, "scheduled", immediate=True)
    cancelled = _dispatch(sql, ctx, "scheduled", state="cancelled", cancelled_at=T2,
                          cancel_reason="operator")
    not_validated = _dispatch(sql, ctx)
    refused = [
        ("scheduled-without-deadline", "scheduled", {"confirm_deadline": None}),
        ("open-without-deadline", "open", {"confirm_deadline": None}),
        ("cancelled-validated-without-deadline", "scheduled",
         {"state": "cancelled", "cancelled_at": T2, "cancel_reason": "operator", "confirm_deadline": None}),
        ("validating-with-deadline", "validating", {"confirm_deadline": OPENS}),
        ("cancelled-unvalidated-with-deadline", "cancelled", {"confirm_deadline": OPENS}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _dispatch(sql, ctx, b, **c), prefix="dispatches_")
    rows = sql.execute("SELECT id, validated_at, confirm_deadline FROM dispatches WHERE id <> %s "
                       "ORDER BY id", (ctx.dispatch,)).fetchall()
    assert rows == [(scheduled, T1, OPENS), (immediate, T1, T1 + timedelta(hours=24)),
                    (cancelled, T1, OPENS), (not_validated, None, None)]


# ---------------------------------------------------------------------------------------------
# CHECKs de `deliveries`
# ---------------------------------------------------------------------------------------------


def test_delivery_value_sets_counters_and_defaults(sql: psycopg.Connection) -> None:
    """Esquema do disparo (estado nos 8 valores; `undelivered_reason` nos 5
    e `failure` nos 5; `render_version >= 0`, `controls_version >= 0`, `current_field >= 0`; as duas
    versões com padrão 0): uma entrega coerente de cada estado, cada motivo de não entregue e cada falha
    de questão, e as versões e o campo nas linhas-limite, são aceitos (as versões omitidas valem 0);
    valor fora dos conjuntos (inclusive os nomes superados e outra caixa) e
    contador negativo são recusados por um CHECK `deliveries_*`."""
    ctx = _ctx(sql)
    states = [base for base in _DELIVERY_BY_STATE]
    by_state = [_delivery(sql, ctx, base) for base in states]
    reasons = [_delivery(sql, ctx, "not_delivered", undelivered_reason=r) for r in UNDELIVERED_REASONS]
    failures = [_delivery(sql, ctx, "question_failed", failure=f) for f in FAILURES]
    limits = _delivery(sql, ctx, "delivered", render_version=0, controls_version=0, current_field=0)
    grown = _delivery(sql, ctx, "delivered", render_version=7, controls_version=3, current_field=5)
    refused = [
        ("state-unknown", "pending", {"state": "undelivered"}),
        ("state-superseded-name", "no_answer", {"state": "unanswered"}),
        ("state-other-case", "pending", {"state": "Pending"}),
        ("undelivered-reason-unknown", "not_delivered", {"undelivered_reason": "blocked"}),
        ("undelivered-reason-window", "not_delivered", {"undelivered_reason": "window_closed"}),
        ("undelivered-reason-other-case", "not_delivered", {"undelivered_reason": "Endpoint_blocked"}),
        ("failure-unknown", "question_failed", {"failure": "grading_error"}),
        ("failure-other-case", "question_failed", {"failure": "Variant_broken"}),
        ("render-version-negative", "delivered", {"render_version": -1}),
        ("controls-version-negative", "delivered", {"controls_version": -1}),
        ("current-field-negative", "delivered", {"current_field": -1}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _delivery(sql, ctx, b, **c), prefix="deliveries_")
    rows = sql.execute("SELECT id, state, undelivered_reason, failure, render_version, controls_version, "
                       "current_field FROM deliveries ORDER BY id").fetchall()
    expected_states = ["pending" if base == "pending_variant" else base for base in states]
    fields = {"pending_variant": 0, "delivered": 0, "no_answer": 1}
    assert rows == [
        (i, state, "endpoint_blocked" if state == "not_delivered" else None,
         "variant_broken" if state == "question_failed" else None, 0, 0, fields.get(base))
        for i, state, base in zip(by_state, expected_states, states)] + [
        (i, "not_delivered", reason, None, 0, 0, None) for i, reason in zip(reasons, UNDELIVERED_REASONS)] + [
        (i, "question_failed", None, failure, 0, 0, None) for i, failure in zip(failures, FAILURES)] + [
        (limits, "delivered", None, None, 0, 0, 0), (grown, "delivered", None, None, 7, 3, 5)]


def test_delivery_reasons_iff_state(sql: psycopg.Connection) -> None:
    """Esquema do disparo (cada motivo presente se e só se o estado correspondente:
    `undelivered_reason` só e sempre em `not_delivered`; `failure` só e sempre em `question_failed`):
    cada estado sem o seu motivo, ou com o motivo do outro estado, é recusado por um CHECK
    `deliveries_*`."""
    ctx = _ctx(sql)
    refused = [
        ("not-delivered-without-reason", "not_delivered", {"undelivered_reason": None}),
        ("question-failed-without-failure", "question_failed", {"failure": None}),
        ("not-delivered-with-failure", "not_delivered", {"failure": "variant_broken"}),
        ("question-failed-with-reason", "question_failed", {"undelivered_reason": "channel_refused"}),
        ("pending-with-reason", "pending", {"undelivered_reason": "engine_unavailable"}),
        ("pending-with-failure", "pending", {"failure": "variant_broken"}),
        ("delivered-with-reason", "delivered", {"undelivered_reason": "channel_refused"}),
        ("answered-with-failure", "answered", {"failure": "grading_broken"}),
        ("no-answer-with-reason", "no_answer", {"undelivered_reason": "channel_unavailable"}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _delivery(sql, ctx, b, **c), prefix="deliveries_")
    assert sql.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0


def test_delivery_send_and_principal_message(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`(sent_at IS NULL) = (principal_message_id IS NULL)`; os
    estados que passam por `delivered` (`delivered`, `answered`, `dont_know`, `no_answer`,
    `answered_late`) têm `variant_at` e `sent_at`; `pending` e `not_delivered` não têm `sent_at`): a
    `question_failed` com e sem envio (a correção `broken` depois da entrega, e a variante quebrada) e a
    `not_delivered` com variante são aceitas; o envio sem a principal, a principal sem o envio, cada
    estado depois de `delivered` sem variante ou sem envio, e `pending`/`not_delivered` com envio são
    recusados por um CHECK `deliveries_*`."""
    ctx = _ctx(sql)
    failed_after_send = _delivery(sql, ctx, "delivered", state="question_failed", failure="grading_broken",
                                  current_field=None)
    failed_before_send = _delivery(sql, ctx, "question_failed")
    undelivered_with_variant = _delivery(sql, ctx, "not_delivered", variant_at=T1,
                                         undelivered_reason="channel_refused")
    principal = _plain_message(sql, ctx.person)
    refused = [
        ("delivered-without-principal", "delivered", {"principal_message_id": None}),
        ("pending-with-principal-only", "pending", {"principal_message_id": principal}),
        ("delivered-without-sent", "delivered", {"sent_at": None, "principal_message_id": None}),
        ("delivered-without-variant", "delivered", {"variant_at": None, "current_field": None}),
        ("answered-without-sent", "answered", {"sent_at": None, "principal_message_id": None}),
        ("answered-without-variant", "answered", {"variant_at": None}),
        ("dont-know-without-sent", "dont_know", {"sent_at": None, "principal_message_id": None}),
        ("dont-know-without-variant", "dont_know", {"variant_at": None}),
        ("no-answer-without-sent", "no_answer", {"sent_at": None, "principal_message_id": None}),
        ("no-answer-without-variant", "no_answer", {"variant_at": None, "current_field": None}),
        ("answered-late-without-sent", "answered_late", {"sent_at": None, "principal_message_id": None}),
        ("answered-late-without-variant", "answered_late", {"variant_at": None}),
        ("pending-with-sent", "pending_variant", {"sent_at": T2, "principal_message_id": principal}),
        ("not-delivered-with-sent", "not_delivered",
         {"variant_at": T1, "sent_at": T2, "principal_message_id": principal}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _delivery(sql, ctx, b, **c), prefix="deliveries_")
    rows = sql.execute("SELECT id, state, variant_at, sent_at, principal_message_id IS NOT NULL "
                       "FROM deliveries ORDER BY id").fetchall()
    assert rows == [(failed_after_send, "question_failed", T1, T2, True),
                    (failed_before_send, "question_failed", None, None, False),
                    (undelivered_with_variant, "not_delivered", T1, None, False)]


def test_delivery_answered_at(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`answered_at` preenchido se e só se `answered`,
    `dont_know` ou `answered_late`): esses três com `answered_at` são aceitos; cada um deles sem, e cada
    outro estado com `answered_at`, são recusados por um CHECK `deliveries_*`."""
    ctx = _ctx(sql)
    ids = [_delivery(sql, ctx, base) for base in ("answered", "dont_know", "answered_late")]
    refused = [(f"{base}-without-answered_at", base, {"answered_at": None})
               for base in ("answered", "dont_know", "answered_late")] + [
        (f"{base}-with-answered_at", base, {"answered_at": T3})
        for base in ("pending", "pending_variant", "delivered", "not_delivered", "no_answer",
                     "question_failed")]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _delivery(sql, ctx, b, **c), prefix="deliveries_")
    assert sql.execute("SELECT id, state, answered_at FROM deliveries ORDER BY id").fetchall() == [
        (ids[0], "answered", T3), (ids[1], "dont_know", T3), (ids[2], "answered_late", T4)]


def test_delivery_current_field_only_while_open_with_variant(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`current_field` só em
    `pending`, `delivered` ou `no_answer`, e só com `variant_at`; nulo em todo estado terminal):
    controle positivo, a `pending` com `variant_at` e `current_field` (a transação da variante) é
    aceita, como `delivered` e `no_answer` com o campo; a `pending` sem `variant_at` com o campo e cada
    estado terminal com o campo (mesmo com `variant_at`) são recusados por um CHECK `deliveries_*`."""
    ctx = _ctx(sql)
    pending = _delivery(sql, ctx, "pending_variant")
    delivered = _delivery(sql, ctx, "delivered", current_field=2)
    no_answer = _delivery(sql, ctx, "no_answer", current_field=0)
    refused = [
        ("pending-without-variant", "pending", {"current_field": 0}),
        ("answered", "answered", {"current_field": 0}),
        ("dont-know", "dont_know", {"current_field": 0}),
        ("answered-late", "answered_late", {"current_field": 1}),
        ("not-delivered-with-variant", "not_delivered", {"variant_at": T1, "current_field": 0}),
        ("question-failed-with-variant", "question_failed", {"variant_at": T1, "current_field": 0}),
    ]
    for label, base, columns in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda b=base, c=columns: _delivery(sql, ctx, b, **c), prefix="deliveries_")
    assert sql.execute("SELECT id, state, variant_at, current_field FROM deliveries "
                       "ORDER BY id").fetchall() == [
        (pending, "pending", T1, 0), (delivered, "delivered", T1, 2), (no_answer, "no_answer", T1, 0)]


def test_delivery_revealed_at_states(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`revealed_at` só em `answered`,
    `dont_know`, `no_answer`, `answered_late` ou `question_failed`: a `no_answer` revelada no fechamento
    pode chegar a `question_failed` por uma correção atrasada `broken`): a revelação é aceita nesses 5
    estados (controle positivo em `question_failed`, depois do envio) e recusada em `pending`,
    `delivered` e `not_delivered` por um CHECK `deliveries_*`."""
    ctx = _ctx(sql)
    accepted = [
        ("answered", {}), ("dont_know", {}), ("no_answer", {"current_field": None}), ("answered_late", {}),
        ("delivered", {"state": "question_failed", "failure": "grading_broken", "current_field": None}),
    ]
    ids = [_delivery(sql, ctx, base, revealed_at=T4, **columns) for base, columns in accepted]
    for base in ("pending", "pending_variant", "delivered", "not_delivered"):
        _refused(f"{base}-revealed", psycopg.errors.CheckViolation,
                 lambda b=base: _delivery(sql, ctx, b, revealed_at=T4), prefix="deliveries_")
    assert sql.execute("SELECT id, state, revealed_at FROM deliveries ORDER BY id").fetchall() == [
        (ids[0], "answered", T4), (ids[1], "dont_know", T4), (ids[2], "no_answer", T4),
        (ids[3], "answered_late", T4), (ids[4], "question_failed", T4)]


def test_delivery_one_per_dispatch_and_enrollment(sql: psycopg.Connection) -> None:
    """Nunca duas entregas por aluno e disparo; esquema do disparo
    (`deliveries_dispatch_enrollment_key UNIQUE (dispatch_id, enrollment_id)`; `button_token` único):
    a mesma matrícula em outro disparo e outra matrícula no mesmo disparo são aceitas; a segunda entrega
    da mesma matrícula no mesmo disparo é recusada por esse índice, com o `DETAIL` só com a chave; um
    `button_token` repetido é recusado em `deliveries`."""
    ctx = _ctx(sql)
    enrollment = _student(sql, ctx.offering)
    other_dispatch = _dispatch(sql, ctx, "open")
    first = _delivery(sql, ctx, enrollment_id=enrollment)
    same_student_other_dispatch = _delivery(sql, ctx, enrollment_id=enrollment, dispatch_id=other_dispatch)
    other_student = _delivery(sql, ctx)
    exc = _refused("same-dispatch-and-enrollment", psycopg.errors.UniqueViolation,
                   lambda: _delivery(sql, ctx, "not_delivered", enrollment_id=enrollment),
                   constraint="deliveries_dispatch_enrollment_key", table="deliveries")
    assert exc.diag.message_detail == (
        f"Key (dispatch_id, enrollment_id)=({ctx.dispatch}, {enrollment}) already exists.")
    token = sql.execute("SELECT button_token FROM deliveries WHERE id = %s", (first,)).fetchone()[0]
    _refused("repeated-button-token", psycopg.errors.UniqueViolation,
             lambda: _delivery(sql, ctx, button_token=token), table="deliveries")
    assert sql.execute("SELECT id, dispatch_id, enrollment_id = %s FROM deliveries ORDER BY id",
                       (enrollment,)).fetchall() == [
        (first, ctx.dispatch, True), (same_student_other_dispatch, other_dispatch, True),
        (other_student, ctx.dispatch, False)]


# ---------------------------------------------------------------------------------------------
# `outbox_messages`
# ---------------------------------------------------------------------------------------------


def test_outbox_state_check_accepts_cancelled(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`outbox_messages_state_check` recriado com `cancelled`, sem o
    `superseded`, um nome superado): uma mensagem de entrega em cada um dos
    5 estados e as da API base nos 4 estados dela são aceitas; `superseded` e outra caixa são recusados por esse
    CHECK."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx, "pending_variant")
    states = ("pending", "leased", "sent", "failed", "cancelled")
    ids = [_delivery_message(sql, ctx, delivery, state=state) for state in states]
    r2 = [_plain_message(sql, ctx.person, state=state) for state in ("pending", "sent", "failed", "leased")]
    for label, state in (("superseded", "superseded"), ("other-case", "Cancelled"), ("unknown", "expired")):
        _refused(label, psycopg.errors.CheckViolation,
                 lambda s=state: _delivery_message(sql, ctx, delivery, state=s),
                 constraint="outbox_messages_state_check")
    rows = sql.execute("SELECT id, state, delivery_id FROM outbox_messages WHERE id = ANY(%s) "
                       "ORDER BY id", (ids + r2,)).fetchall()
    assert rows == [(i, state, delivery) for i, state in zip(ids, states)] + [
        (i, state, None) for i, state in zip(r2, ("pending", "sent", "failed", "leased"))]


def test_outbox_content_check(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`outbox_messages_content_check`: ou
    `delivery_id` nulo com `text` preenchido e as colunas de entrega nulas, as formas (a), (b) e (c) do
    esquema anterior sem mudança; ou `delivery_id` preenchido com `text` nulo, `delivery_role` (`question` · `media` ·
    `field_prompt` · `fallback` · `notice`) e `operation` (`send` · `edit_text` · `edit_markup`)
    preenchidos e a forma (b), pessoa, bot e endereço; o CHECK de destino anterior não muda; `priority`
    fora do CHECK): as três formas anteriores (inclusive a (c) com `priority = 1`, a resposta fixa à mensagem
    livre), cada papel e cada operação são aceitos; mensagem da API base sem texto ou com coluna de entrega,
    mensagem de entrega com texto, sem papel, sem operação, fora da forma (b), com papel ou operação
    fora dos conjuntos são recusadas por esse CHECK."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx, "pending_variant")
    update = _update(sql, ctx.bot)
    form_a = _plain_message(sql, ctx.person)
    form_b = _plain_message(sql, ctx.person, bot_id=ctx.bot, delivery_endpoint_id=ctx.endpoint)
    form_c = _plain_message(sql, None, bot_id=ctx.bot, reply_to_update_id=update, priority=1)
    roles = [_delivery_message(sql, ctx, delivery, role=role) for role in DELIVERY_ROLES]
    operations = [_delivery_message(sql, ctx, delivery, role="fallback", operation=op, state="sent")
                  for op in OPERATIONS]
    refused: list[tuple[str, Callable[[], Any]]] = [
        ("plain-without-text", lambda: _plain_message(sql, ctx.person, text=None)),
        ("plain-with-role", lambda: _plain_message(sql, ctx.person, delivery_role="question")),
        ("plain-with-operation", lambda: _plain_message(sql, ctx.person, operation="send")),
        ("plain-with-render-version", lambda: _plain_message(sql, ctx.person, render_version=1)),
        ("plain-with-media-id", lambda: _plain_message(sql, ctx.person, media_id="m1")),
        ("plain-with-field-index", lambda: _plain_message(sql, ctx.person, field_index=0)),
        ("delivery-with-text", lambda: _delivery_message(sql, ctx, delivery, text="texto guardado")),
        ("delivery-without-role", lambda: _delivery_message(sql, ctx, delivery, role=None)),
        ("delivery-without-operation", lambda: _delivery_message(sql, ctx, delivery, operation=None)),
        ("delivery-in-form-a",
         lambda: _delivery_message(sql, ctx, delivery, bot_id=None, delivery_endpoint_id=None)),
        ("delivery-in-form-c",
         lambda: _delivery_message(sql, ctx, delivery, recipient_person_id=None, delivery_endpoint_id=None,
                                   reply_to_update_id=update)),
        ("role-unknown", lambda: _delivery_message(sql, ctx, delivery, role="edit")),
        ("role-other-case", lambda: _delivery_message(sql, ctx, delivery, role="Question")),
        ("operation-unknown", lambda: _delivery_message(sql, ctx, delivery, operation="edit")),
        ("operation-other-case", lambda: _delivery_message(sql, ctx, delivery, operation="Send")),
    ]
    for label, insert in refused:
        _refused(label, psycopg.errors.CheckViolation, insert, constraint="outbox_messages_content_check")
    rows = sql.execute("SELECT id, text IS NULL, delivery_id, delivery_role, operation, priority "
                       "FROM outbox_messages ORDER BY id").fetchall()
    assert rows == [(form_a, False, None, None, None, 0), (form_b, False, None, None, None, 0),
                    (form_c, False, None, None, None, 1)] + [
        (i, True, delivery, role, "send", 0) for i, role in zip(roles, DELIVERY_ROLES)] + [
        (i, True, delivery, "fallback", op, 0) for i, op in zip(operations, OPERATIONS)]


def test_outbox_pending_edit_key(sql: psycopg.Connection) -> None:
    """As edições pendentes da mesma mensagem se fundem; esquema do disparo
    (`outbox_messages_pending_edit_key UNIQUE (delivery_id, delivery_role) WHERE operation IN
    ('edit_text', 'edit_markup') AND state = 'pending'`): uma edição pendente por (entrega, papel) é
    aceita, junto de uma em voo (`leased`), de uma já enviada, de uma pendente de outro papel ou de outra
    entrega e de envios pendentes do mesmo papel; a segunda edição pendente do mesmo par (de texto ou de
    teclado) é recusada por esse índice, com o `DETAIL` só com a chave; depois de a pendente sair, uma
    nova é aceita."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx, "delivered")
    other = _delivery(sql, ctx, "delivered")
    pending = _delivery_message(sql, ctx, delivery, operation="edit_text")
    accepted = [
        _delivery_message(sql, ctx, delivery, operation="edit_text", state="leased"),
        _delivery_message(sql, ctx, delivery, operation="edit_markup", state="sent"),
        _delivery_message(sql, ctx, delivery, operation="edit_text", state="cancelled"),
        _delivery_message(sql, ctx, delivery, role="fallback", operation="edit_text"),
        _delivery_message(sql, ctx, other, operation="edit_markup"),
        _delivery_message(sql, ctx, delivery, operation="send"),
        _delivery_message(sql, ctx, delivery, operation="send"),
    ]
    for label, operation in (("second-edit-text", "edit_text"), ("edit-markup-over-edit-text", "edit_markup")):
        exc = _refused(label, psycopg.errors.UniqueViolation,
                       lambda o=operation: _delivery_message(sql, ctx, delivery, operation=o),
                       constraint="outbox_messages_pending_edit_key", table="outbox_messages")
        assert exc.diag.message_detail == (
            f"Key (delivery_id, delivery_role)=({delivery}, question) already exists.")
    sql.execute("UPDATE outbox_messages SET state = 'sent', attempts = 1, sent_at = %s WHERE id = %s",
                (T2, pending))
    again = _delivery_message(sql, ctx, delivery, operation="edit_markup")
    rows = sql.execute("SELECT id, delivery_role, operation, state FROM outbox_messages "
                       "WHERE delivery_id IS NOT NULL AND operation <> 'send' AND state = 'pending' "
                       "ORDER BY id").fetchall()
    assert rows == [(accepted[3], "fallback", "edit_text", "pending"),
                    (accepted[4], "question", "edit_markup", "pending"),
                    (again, "question", "edit_markup", "pending")]


# ---------------------------------------------------------------------------------------------
# Índices únicos parciais de `submissions`
# ---------------------------------------------------------------------------------------------


def test_submissions_one_final_and_one_grading(sql: psycopg.Connection) -> None:
    """Uma resposta válida por disparo; esquema do disparo (`submissions_one_final_idx UNIQUE (delivery_id)
    WHERE (outcome = 'graded' AND valid) OR outcome = 'dont_know'`; `submissions_one_grading_idx UNIQUE
    (delivery_id) WHERE outcome = 'grading'`): numa entrega, as tentativas com erro de formato
    (`graded` inválida), as recusadas, as quebradas e as de falha interna se repetem; uma válida e uma
    correção pendente são aceitas; a segunda válida, o "Não sei" depois da válida, a válida depois do
    "Não sei" e a segunda correção pendente são recusados pelo índice certo, com o `DETAIL` só com a
    chave; depois do desfecho da pendente, uma nova correção pendente é aceita."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx, "delivered")
    other = _delivery(sql, ctx, "delivered")
    accepted = [
        _submission(sql, ctx, delivery, "graded", valid=False),
        _submission(sql, ctx, delivery, "graded", valid=False),
        _submission(sql, ctx, delivery, "refused"),
        _submission(sql, ctx, delivery, "refused", refusal="grading_pending"),
        _submission(sql, ctx, delivery, "broken"),
        _submission(sql, ctx, delivery, "internal_failure"),
        _submission(sql, ctx, delivery, "internal_failure"),
    ]
    valid = _submission(sql, ctx, delivery, "graded", score=1.0)
    grading = _submission(sql, ctx, delivery, "grading")
    dont_know = _submission(sql, ctx, other, "dont_know")
    refused = [
        ("second-valid", delivery, "graded", {"score": 0.5}, "submissions_one_final_idx"),
        ("dont-know-after-valid", delivery, "dont_know", {}, "submissions_one_final_idx"),
        ("valid-after-dont-know", other, "graded", {"score": 1.0}, "submissions_one_final_idx"),
        ("second-dont-know", other, "dont_know", {}, "submissions_one_final_idx"),
        ("second-grading", delivery, "grading", {}, "submissions_one_grading_idx"),
    ]
    for label, target, outcome, columns, index in refused:
        exc = _refused(label, psycopg.errors.UniqueViolation,
                       lambda t=target, o=outcome, c=columns: _submission(sql, ctx, t, o, **c),
                       constraint=index, table="submissions")
        assert exc.diag.message_detail == f"Key (delivery_id)=({target}) already exists.", label
    sql.execute("UPDATE submissions SET outcome = 'graded', valid = false, graded_at = %s WHERE id = %s",
                (T3, grading))
    grading_again = _submission(sql, ctx, delivery, "grading")
    rows = sql.execute("SELECT id, delivery_id, outcome, valid FROM submissions ORDER BY id").fetchall()
    assert rows == [
        (accepted[0], delivery, "graded", False), (accepted[1], delivery, "graded", False),
        (accepted[2], delivery, "refused", None), (accepted[3], delivery, "refused", None),
        (accepted[4], delivery, "broken", None), (accepted[5], delivery, "internal_failure", None),
        (accepted[6], delivery, "internal_failure", None), (valid, delivery, "graded", True),
        (grading, delivery, "graded", False), (dont_know, other, "dont_know", None),
        (grading_again, delivery, "grading", None)]
    assert sql.execute("SELECT normalized_fields, delay_notice_sent FROM submissions WHERE id = %s",
                       (valid,)).fetchone() == ([], False)


# ---------------------------------------------------------------------------------------------
# Tipos de evento
# ---------------------------------------------------------------------------------------------


def test_event_type_check_accepts_exactly_the_43_types(sql: psycopg.Connection) -> None:
    """Esquema do disparo (o CHECK `events_type_check` recriado com os 29 tipos anteriores
    e os 14 novos: 43, com o `offering.settings_changed` das mensagens livres): cada um dos 43 tipos é aceito; um
    tipo fora deles (inclusive um nome superado, outra caixa, um desfecho que não gera evento e um prefixo)
    é recusado por esse CHECK."""
    refused_types = []
    for event_type in PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES:
        try:
            _insert(sql, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                    data=Jsonb({}))
        except psycopg.errors.CheckViolation:
            refused_types.append(event_type)
    assert refused_types == [], (
        "tipos recusados pelo CHECK de eventos (o esquema exige os 43)")
    for label, event_type in (("unknown", "dispatch.validating"), ("other-case", "Dispatch.prepared"),
                              ("no-event-outcome", "submission.refused"),
                              ("no-event-reveal", "delivery.revealed"), ("prefix", "dispatch."),
                              ("superseded", "delivery.undelivered")):
        _refused(label, psycopg.errors.CheckViolation,
                 lambda t=event_type: _insert(sql, "events", key="seq", type=t, occurred_at=T0,
                                              actor_token_id=None, data=Jsonb({})),
                 constraint="events_type_check")
    assert [row[0] for row in sql.execute("SELECT type FROM events ORDER BY seq").fetchall()] == list(
        PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES)


# ---------------------------------------------------------------------------------------------
# Tabelas com texto: sem CHECK, e o `DETAIL` das violações sem o texto
# ---------------------------------------------------------------------------------------------


def _assert_detail_without_text(exc: psycopg.Error, expected_detail: str, label: str) -> None:
    detail = exc.diag.message_detail or ""
    assert detail == expected_detail, (label, detail)
    for sentinel in (STUDENT_SENTINEL, THIRD_PARTY_SENTINEL):
        assert sentinel not in str(exc) and sentinel not in detail, label


def test_text_tables_have_no_check_and_violations_do_not_show_text(sql: psycopg.Connection) -> None:
    """Esquema do disparo (princípio texto × CHECK: a violação de um CHECK
    mostra a linha inteira no `DETAIL`; o texto do aluno e o de terceiros não redigido ficam em tabelas
    sem CHECK, com os invariantes críticos em índices únicos, cujo `DETAIL` mostra só a chave):
    `question_refs`, `delivery_contents`, `media_blobs`, `submissions` e `free_messages` não
    têm CHECK; com texto sentinela do aluno (entrada, respostas, feedback, erros de formato, partes, eco)
    e de terceiros (título, snapshot, IR, renderização, bytes da mídia) gravado, as violações de
    unicidade (índice parcial, `telegram_update_id`, chave primária) e de FK mostram só a chave, sem
    nenhum dos dois textos."""
    checks = sql.execute(
        "SELECT c.conrelid::regclass::text, c.conname::text FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'c' AND c.conrelid::regclass::text = ANY(%s)",
        (sorted(TEXT_TABLES),)).fetchall()
    assert checks == []
    assert {row[0] for row in sql.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name = ANY(%s)", (sorted(TEXT_TABLES),)).fetchall()} == TEXT_TABLES

    ctx = _ctx(sql)
    student_text = {"x": STUDENT_SENTINEL}
    ref = _question_ref(sql, title=THIRD_PARTY_SENTINEL, supported=True, show_correct_answer=True,
                        single_variant=False, partial_credit=True, answers_names=["x"],
                        content_hash="sha256:" + "0" * 64, inspected_at=T1)
    delivery = _delivery(sql, ctx, "delivered")
    _insert(sql, "delivery_contents", key="delivery_id", delivery_id=delivery,
            snapshot=Jsonb({"params": THIRD_PARTY_SENTINEL}), ir=Jsonb({"html": THIRD_PARTY_SENTINEL}),
            media_refs=Jsonb({}), rendered=Jsonb({"format": "rich", "statement": THIRD_PARTY_SENTINEL}),
            parts=Jsonb(student_text), selection=Jsonb([]),
            current_render=Jsonb({"echo": STUDENT_SENTINEL}))
    data = b"\x89PNG" + THIRD_PARTY_SENTINEL.encode()
    digest = hashlib.sha256(data).hexdigest()
    _insert(sql, "media_blobs", key="sha256", sha256=digest, content_type="image/png",
            size_bytes=len(data), data=data, created_at=T1)
    update = _update(sql, ctx.bot, text=STUDENT_SENTINEL)
    _submission(sql, ctx, delivery, "graded", telegram_update_id=update, raw_input=Jsonb(student_text),
                submitted_answers=Jsonb(student_text), feedback=Jsonb({"x": STUDENT_SENTINEL}),
                format_errors=Jsonb({}), params=Jsonb({"p": THIRD_PARTY_SENTINEL}),
                partial_scores=Jsonb({"x": {"score": 1, "feedback": STUDENT_SENTINEL}}), score=1.0)
    free_update = _update(sql, ctx.bot, text=STUDENT_SENTINEL)
    _insert(sql, "free_messages", offering_id=ctx.offering, delivery_endpoint_id=ctx.endpoint,
            telegram_update_id=free_update, text=STUDENT_SENTINEL, replied=True, ambiguous=False,
            received_at=T2, created_at=T2)
    missing_delivery = sql.execute("SELECT max(id) + 1000 FROM deliveries").fetchone()[0]

    cases: list[tuple[str, type[psycopg.Error], Callable[[], Any], str]] = [
        ("submission-second-valid", psycopg.errors.UniqueViolation,
         lambda: _submission(sql, ctx, delivery, "graded", raw_input=Jsonb(student_text), score=1.0),
         f"Key (delivery_id)=({delivery}) already exists."),
        ("submission-same-update", psycopg.errors.UniqueViolation,
         lambda: _submission(sql, ctx, delivery, "refused", telegram_update_id=update,
                             raw_input=Jsonb(student_text)),
         f"Key (telegram_update_id)=({update}) already exists."),
        ("submission-missing-delivery", psycopg.errors.ForeignKeyViolation,
         lambda: _submission(sql, ctx, missing_delivery, "refused", raw_input=Jsonb(student_text)),
         f'Key (delivery_id)=({missing_delivery}) is not present in table "deliveries".'),
        ("free-message-same-update", psycopg.errors.UniqueViolation,
         lambda: _insert(sql, "free_messages", offering_id=ctx.offering,
                         delivery_endpoint_id=ctx.endpoint, telegram_update_id=free_update,
                         text=STUDENT_SENTINEL, replied=False, ambiguous=True, received_at=T3,
                         created_at=T3),
         f"Key (telegram_update_id)=({free_update}) already exists."),
        ("contents-same-delivery", psycopg.errors.UniqueViolation,
         lambda: _insert(sql, "delivery_contents", key="delivery_id", delivery_id=delivery,
                         snapshot=Jsonb({"params": THIRD_PARTY_SENTINEL}),
                         ir=Jsonb({"html": THIRD_PARTY_SENTINEL}), media_refs=Jsonb({}),
                         rendered=Jsonb({"statement": THIRD_PARTY_SENTINEL}),
                         parts=Jsonb(student_text), selection=Jsonb([]), current_render=None),
         f"Key (delivery_id)=({delivery}) already exists."),
        ("media-same-digest", psycopg.errors.UniqueViolation,
         lambda: _insert(sql, "media_blobs", key="sha256", sha256=digest, content_type="image/png",
                         size_bytes=len(data), data=data, created_at=T2),
         f"Key (sha256)=({digest}) already exists."),
    ]
    for label, error, insert, detail in cases:
        _assert_detail_without_text(_refused(label, error, insert), detail, label)
    assert sql.execute("SELECT title FROM question_refs WHERE id = %s", (ref,)).fetchone() == (
        THIRD_PARTY_SENTINEL,)
    assert sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == 1
    assert sql.execute("SELECT count(*) FROM free_messages").fetchone()[0] == 1


# ---------------------------------------------------------------------------------------------
# `question_refs_key` e FKs novas
# ---------------------------------------------------------------------------------------------


def test_question_refs_key_by_digest(sql: psycopg.Connection) -> None:
    """Esquema do disparo (`question_refs_key UNIQUE (repo_sha256,
    commit_sha, qid)`: pelo resumo, porque o `UNIQUE (repo, …)` estoura o B-tree com URL longa
    multibyte; a referência nasce por `INSERT … ON CONFLICT DO NOTHING` + `SELECT`): um `repo` de 2048
    caracteres multibyte aleatórios e um `qid` de 512 são aceitos; o mesmo trio é recusado por esse
    índice; o `INSERT … ON CONFLICT (repo_sha256, commit_sha, qid) DO NOTHING` não grava nem falha; o
    mesmo `repo` com outro commit ou outra questão é outra referência."""
    repo = "https://repos.cq.test/" + "".join(chr(0x4E00 + secrets.randbelow(0x5000)) for _ in range(2026))
    assert len(repo) == 2048
    commit = "b" * 64
    qid = "q" * 512
    ref = _question_ref(sql, repo=repo, commit_sha=commit, qid=qid)
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()
    _refused("same-key", psycopg.errors.UniqueViolation,
             lambda: _question_ref(sql, repo=repo, commit_sha=commit, qid=qid),
             constraint="question_refs_key", table="question_refs")
    inserted = sql.execute(
        "INSERT INTO question_refs (ref_uuid, repo, repo_sha256, commit_sha, qid, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (repo_sha256, commit_sha, qid) DO NOTHING "
        "RETURNING id", (uuid.uuid4(), repo, digest, commit, qid, T1)).fetchall()
    assert inserted == []
    other_commit = _question_ref(sql, repo=repo, commit_sha="c" * 40, qid=qid)
    other_qid = _question_ref(sql, repo=repo, commit_sha=commit, qid="q" * 511)
    rows = sql.execute("SELECT id, repo = %s, repo_sha256, commit_sha, char_length(qid) FROM question_refs "
                       "ORDER BY id", (repo,)).fetchall()
    assert rows == [(ref, True, digest, commit, 512), (other_commit, True, digest, "c" * 40, 512),
                    (other_qid, True, digest, commit, 511)]


def test_new_foreign_keys_refuse_missing_rows(sql: psycopg.Connection) -> None:
    """Esquema do disparo (as 13 FKs novas, inclusive a
    circular `deliveries.principal_message_id → outbox_messages.id` × `outbox_messages.delivery_id →
    deliveries.id`): com as linhas existentes, a entrega com a mensagem principal da própria entrega é
    aceita (a FK circular funciona com as colunas anuláveis); cada linha que aponta para uma linha
    inexistente é recusada com `ForeignKeyViolation` na tabela certa."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx, "pending_variant")
    question = _delivery_message(sql, ctx, delivery, state="sent")
    sql.execute("UPDATE deliveries SET state = 'delivered', sent_at = %s, principal_message_id = %s "
                "WHERE id = %s", (T2, question, delivery))
    missing = {table: sql.execute(pgsql.SQL("SELECT coalesce(max(id), 0) + 1000 FROM {}").format(
        pgsql.Identifier(table))).fetchone()[0]
        for table in ("offerings", "question_refs", "dispatches", "deliveries", "enrollments",
                      "delivery_endpoints", "outbox_messages", "telegram_updates")}
    cases: list[tuple[str, Callable[[], Any], str]] = [
        ("dispatch-of-missing-offering",
         lambda: _insert(sql, "dispatches", offering_id=missing["offerings"], question_ref_id=ctx.ref,
                         **_dispatch_values("validating", False)), "dispatches"),
        ("dispatch-of-missing-ref",
         lambda: _insert(sql, "dispatches", offering_id=ctx.offering, question_ref_id=missing["question_refs"],
                         **_dispatch_values("validating", False)), "dispatches"),
        ("delivery-of-missing-dispatch", lambda: _delivery(sql, ctx, dispatch_id=missing["dispatches"]),
         "deliveries"),
        ("delivery-of-missing-enrollment", lambda: _delivery(sql, ctx, enrollment_id=missing["enrollments"]),
         "deliveries"),
        ("delivery-of-missing-endpoint",
         lambda: _delivery(sql, ctx, delivery_endpoint_id=missing["delivery_endpoints"]), "deliveries"),
        ("delivery-with-missing-principal",
         lambda: _delivery(sql, ctx, "delivered", principal_message_id=missing["outbox_messages"]),
         "deliveries"),
        ("contents-of-missing-delivery",
         lambda: _insert(sql, "delivery_contents", key="delivery_id", delivery_id=missing["deliveries"],
                         snapshot=Jsonb({}), ir=Jsonb({}), media_refs=Jsonb({}), rendered=Jsonb({}),
                         parts=Jsonb({}), selection=Jsonb([])), "delivery_contents"),
        ("submission-of-missing-delivery",
         lambda: _submission(sql, ctx, missing["deliveries"], "refused"), "submissions"),
        ("submission-of-missing-update",
         lambda: _submission(sql, ctx, delivery, "refused", telegram_update_id=missing["telegram_updates"]),
         "submissions"),
        ("free-message-of-missing-offering",
         lambda: _insert(sql, "free_messages", offering_id=missing["offerings"],
                         delivery_endpoint_id=ctx.endpoint, telegram_update_id=_update(sql, ctx.bot),
                         text="texto neutro", replied=False, ambiguous=False, received_at=T2, created_at=T2),
         "free_messages"),
        ("free-message-of-missing-endpoint",
         lambda: _insert(sql, "free_messages", offering_id=ctx.offering,
                         delivery_endpoint_id=missing["delivery_endpoints"],
                         telegram_update_id=_update(sql, ctx.bot), text="texto neutro", replied=False,
                         ambiguous=False, received_at=T2, created_at=T2), "free_messages"),
        ("free-message-of-missing-update",
         lambda: _insert(sql, "free_messages", offering_id=ctx.offering, delivery_endpoint_id=ctx.endpoint,
                         telegram_update_id=missing["telegram_updates"], text="texto neutro", replied=False,
                         ambiguous=False, received_at=T2, created_at=T2), "free_messages"),
        ("message-of-missing-delivery",
         lambda: _delivery_message(sql, ctx, missing["deliveries"]), "outbox_messages"),
    ]
    for label, insert, table in cases:
        _refused(label, psycopg.errors.ForeignKeyViolation, insert, table=table)
    assert sql.execute("SELECT d.id, d.state, m.delivery_id = d.id FROM deliveries d "
                       "JOIN outbox_messages m ON m.id = d.principal_message_id ORDER BY d.id").fetchall() == [
        (delivery, "delivered", True)]
