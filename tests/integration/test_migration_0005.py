"""Migração `0005` (os relatórios e o acompanhamento) sobre o esquema do disparo e da resposta.


Cobre:
- o que a `0005` acrescenta ao esquema anterior, e só isso, medido por `apply_up_to(…, "0004")` e
  `apply_up_to(…, "0005")` (`tests/helpers/db.py`), **sem** afirmar `EXPECTED_VERSION` (a versão vigente só
  é afirmada em outros arquivos de teste): as 7 tabelas novas (`dispatch_reports`, `team_notices`,
  `team_notice_floors`, `dispatch_previews`, `dispatch_preview_contents`, `offering_digests`,
  `invite_batches`) com as colunas, os tipos, as nulidades, as identities e os padrões, as colunas
  `outbox_messages.notice_id`, `outbox_messages.text_format` e `invite_sends.batch_id`, os índices
  declarados (`team_notices_pending_idx`, `outbox_messages_notice_order_idx`,
  `outbox_messages_notice_ref_idx`, `invite_sends_batch_idx`, `events_access_changed_idx`,
  `events_dispatch_fact_idx`) e os únicos (`dispatch_reports_version_key`,
  `dispatch_reports_one_closing_idx`, `team_notices_subject_key`), as chaves primárias e as 10 FKs
  novas, o gatilho `dispatch_reports_immutable` e os CHECKs só onde declarados (nenhum em
  `dispatch_reports` nem em `dispatch_preview_contents`: princípio texto × CHECK);
- os CHECKs com controle positivo e negativo (`team_notices`, `dispatch_previews`, `offering_digests`,
  `invite_sends_batch_check`, `team_notice_floors`); as três formas do `outbox_messages_content_check`
  recriado **com o mesmo nome**, aceitas e recusadas, inclusive as linhas
  "plain-without-text" e "plain-with-role" do congelado `test_outbox_content_check` continuando recusadas;
- o gatilho `dispatch_reports_immutable` recusando `UPDATE` e `DELETE` com mensagem fixa, sem dados (o
  `TRUNCATE` passa); o `DETAIL` das violações nas tabelas com texto (`dispatch_reports`,
  `dispatch_preview_contents`) só com a chave;
- os pisos (`team_notice_floors`) gravados pela migração: 0 no banco vazio; sobre um banco com dados do
  disparo, o maior `events.seq` e o maior `invite_sends.id`; nenhuma linha anterior muda;
- o `events_type_check` recriado com os 46 tipos (os 43 anteriores e os 3 novos).

As linhas de apoio são gravadas por SQL direto, com as colunas do esquema, da API base aos relatórios; horários fixos
em UTC; ids internos, seeds, `button_token`, `report_uuid` e `batch_uuid` nunca como literal esperado. Os
nomes de restrição conferidos exatamente são os que a migração declara; os demais CHECKs das
tabelas novas são conferidos pelo prefixo da tabela. [I] As escolhas deste arquivo:
`team_notice_floors.event_floor` e `invite_send_floor` `NOT NULL` (a migração sempre os grava, 0 no banco
vazio); `dispatch_preview_contents.media_refs` e `rendered` anuláveis (nascem na validação e são
preenchidas pela tarefa da prévia); o erro do gatilho é o `RAISE EXCEPTION` padrão (`P0001`), como
o dos gatilhos de `events` (`0001_initial.sql:77-93`).

No esqueleto (sem a `0005`), todo teste deste arquivo falha pela migração ausente do pacote
(`apply_up_to(…, "0005")`) ou pela tabela, coluna ou tipo de evento ausente.

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
from datetime import date, datetime, timedelta, timezone
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
OPENS = T0 + timedelta(days=1)
CLOSES = OPENS + timedelta(hours=1)
DAY = date(2031, 3, 1)

# Os 29 tipos anteriores, os 14 do disparo e os 3 dos relatórios, como literais (e não
# `EventType`).
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
assert len(PRE_DISPATCH_EVENT_TYPES) == 29 and len(DISPATCH_EVENT_TYPES) == 14 and len(REPORT_EVENT_TYPES) == 3

# As 22 tabelas do esquema do disparo (a `0004`, com as mensagens livres).
DISPATCH_SCHEMA_TABLES = {
    "schema_migrations", "people", "identities", "api_tokens", "events", "tasks", "outbox_messages",
    "offerings", "bots", "enrollments", "invites", "delivery_endpoints", "telegram_updates", "invite_sends",
    "question_refs", "dispatches", "deliveries", "delivery_contents", "media_blobs", "submissions",
    "free_messages", "offering_contacts",
}
REPORT_TABLES = {"dispatch_reports", "team_notices", "team_notice_floors", "dispatch_previews",
             "dispatch_preview_contents", "offering_digests", "invite_batches"}

# O esquema dos relatórios: (tabela, coluna) -> (tipo em `udt_name`, anulável, identity, padrão), para cada
# coluna que a `0005` acrescenta. `int8` = bigint, `int4` = integer, `int2` = smallint.
REPORT_NEW_COLUMNS: dict[tuple[str, str], tuple[str, bool, bool, str | None]] = {
    # `dispatch_reports`
    ("dispatch_reports", "id"): ("int8", False, True, None),
    ("dispatch_reports", "report_uuid"): ("uuid", False, False, None),
    ("dispatch_reports", "dispatch_id"): ("int8", False, False, None),
    ("dispatch_reports", "version"): ("int4", False, False, None),
    ("dispatch_reports", "trigger"): ("text", False, False, None),
    ("dispatch_reports", "content"): ("jsonb", False, False, None),
    ("dispatch_reports", "frozen_at"): ("timestamptz", False, False, None),
    # `team_notices` (`recipient_count integer NOT NULL DEFAULT 0`, `data jsonb NOT NULL DEFAULT '{}'`)
    ("team_notices", "id"): ("int8", False, True, None),
    ("team_notices", "offering_id"): ("int8", False, False, None),
    ("team_notices", "kind"): ("text", False, False, None),
    ("team_notices", "subject_key"): ("text", False, False, None),
    ("team_notices", "state"): ("text", False, False, None),
    ("team_notices", "recipient_count"): ("int4", False, False, "0"),
    ("team_notices", "dispatch_id"): ("int8", True, False, None),
    ("team_notices", "report_id"): ("int8", True, False, None),
    ("team_notices", "local_date"): ("date", True, False, None),
    ("team_notices", "data"): ("jsonb", False, False, "'{}'::jsonb"),
    ("team_notices", "created_at"): ("timestamptz", False, False, None),
    ("team_notices", "finished_at"): ("timestamptz", True, False, None),
    # `team_notice_floors` (`id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1)`; [I] pisos não nulos)
    ("team_notice_floors", "id"): ("int2", False, False, "1"),
    ("team_notice_floors", "event_floor"): ("int8", False, False, None),
    ("team_notice_floors", "invite_send_floor"): ("int8", False, False, None),
    # `dispatch_previews` e `dispatch_preview_contents` ([I] `media_refs` e `rendered` anuláveis)
    ("dispatch_previews", "id"): ("int8", False, True, None),
    ("dispatch_previews", "dispatch_id"): ("int8", False, False, None),
    ("dispatch_previews", "seed"): ("int8", False, False, None),
    ("dispatch_previews", "button_token"): ("text", False, False, None),
    ("dispatch_previews", "created_at"): ("timestamptz", False, False, None),
    ("dispatch_preview_contents", "preview_id"): ("int8", False, False, None),
    ("dispatch_preview_contents", "ir"): ("jsonb", False, False, None),
    ("dispatch_preview_contents", "media_refs"): ("jsonb", True, False, None),
    ("dispatch_preview_contents", "rendered"): ("jsonb", True, False, None),
    # `offering_digests`
    ("offering_digests", "id"): ("int8", False, True, None),
    ("offering_digests", "offering_id"): ("int8", False, False, None),
    ("offering_digests", "enabled"): ("bool", False, False, None),
    ("offering_digests", "local_time"): ("text", False, False, None),
    ("offering_digests", "next_due_at"): ("timestamptz", True, False, None),
    ("offering_digests", "updated_at"): ("timestamptz", False, False, None),
    # `invite_batches` e `invite_sends.batch_id`
    ("invite_batches", "id"): ("int8", False, True, None),
    ("invite_batches", "batch_uuid"): ("uuid", False, False, None),
    ("invite_batches", "offering_id"): ("int8", False, False, None),
    ("invite_batches", "created_at"): ("timestamptz", False, False, None),
    ("invite_sends", "batch_id"): ("int8", True, False, None),
    # `outbox_messages` (colunas novas, anuláveis)
    ("outbox_messages", "notice_id"): ("int8", True, False, None),
    ("outbox_messages", "text_format"): ("text", True, False, None),
}

# A expressão-chave de `events_dispatch_fact_idx` normalizada por `_key` (sem espaços, parênteses e
# `::text`).
FACT_KEY = "data->>'dispatch_id'"

# O esquema dos relatórios: os índices que a `0005` cria, por (tabela, chaves em ordem, único,
# primário, parcial), como lista ordenada.
REPORT_NEW_INDEXES = sorted([
    ("dispatch_reports", ("id",), True, True, False),
    ("dispatch_reports", ("report_uuid",), True, False, False),
    ("dispatch_reports", ("dispatch_id", "version"), True, False, False),
    ("dispatch_reports", ("dispatch_id",), True, False, True),
    ("team_notices", ("id",), True, True, False),
    ("team_notices", ("offering_id", "kind", "subject_key"), True, False, False),
    ("team_notices", ("id",), False, False, True),
    ("team_notice_floors", ("id",), True, True, False),
    ("dispatch_previews", ("id",), True, True, False),
    ("dispatch_previews", ("dispatch_id",), True, False, False),
    ("dispatch_previews", ("button_token",), True, False, False),
    ("dispatch_preview_contents", ("preview_id",), True, True, False),
    ("offering_digests", ("id",), True, True, False),
    ("offering_digests", ("offering_id",), True, False, False),
    ("invite_batches", ("id",), True, True, False),
    ("invite_batches", ("batch_uuid",), True, False, False),
    ("invite_sends", ("batch_id",), False, False, True),
    ("outbox_messages", ("delivery_endpoint_id", "id"), False, False, True),
    ("outbox_messages", ("delivery_endpoint_id", "channel_ref"), False, False, True),
    ("events", ("seq",), False, False, True),
    ("events", (FACT_KEY, "type", "seq"), False, False, True),
])
# Os índices com nome declarado no esquema dos relatórios: nome -> (tabela, chaves, único, parcial).
REPORT_NAMED_INDEXES: dict[str, tuple[str, tuple[str, ...], bool, bool]] = {
    "dispatch_reports_version_key": ("dispatch_reports", ("dispatch_id", "version"), True, False),
    "dispatch_reports_one_closing_idx": ("dispatch_reports", ("dispatch_id",), True, True),
    "team_notices_subject_key": ("team_notices", ("offering_id", "kind", "subject_key"), True, False),
    "team_notices_pending_idx": ("team_notices", ("id",), False, True),
    "invite_sends_batch_idx": ("invite_sends", ("batch_id",), False, True),
    "outbox_messages_notice_order_idx": ("outbox_messages", ("delivery_endpoint_id", "id"), False, True),
    "outbox_messages_notice_ref_idx": ("outbox_messages", ("delivery_endpoint_id", "channel_ref"),
                                       False, True),
    "events_access_changed_idx": ("events", ("seq",), False, True),
    "events_dispatch_fact_idx": ("events", (FACT_KEY, "type", "seq"), False, True),
}
# Os literais de texto do predicado de cada índice parcial (pelo conjunto, sem depender da formatação de
# `pg_get_expr`) e um trecho obrigatório do predicado.
FACT_TYPES = {"dispatch.rejected", "dispatch.cancelled", "dispatch.opened", "dispatch.closed",
              "delivery.question_failed"}
REPORT_PREDICATES: dict[str, tuple[set[str], str]] = {
    "dispatch_reports_one_closing_idx": ({"closing"}, "trigger"),
    "team_notices_pending_idx": ({"pending"}, "state"),
    "invite_sends_batch_idx": (set(), "batch_id IS NOT NULL"),
    "outbox_messages_notice_order_idx": ({"pending", "leased"}, "notice_id IS NOT NULL"),
    "outbox_messages_notice_ref_idx": (set(), "notice_id IS NOT NULL"),
    "events_access_changed_idx": ({"bot.access_changed"}, "type"),
    "events_dispatch_fact_idx": (FACT_TYPES, "type"),
}

# As tabelas novas com texto livre (o relatório, já redigido, e a IR da amostra com o gabarito) não têm
# CHECK (princípio texto × CHECK); as com CHECK novo são exatamente estas.
REPORT_TEXT_TABLES = {"dispatch_reports", "dispatch_preview_contents"}
REPORT_CHECK_TABLES = {"team_notices", "team_notice_floors", "dispatch_previews", "offering_digests",
                   "invite_sends", "outbox_messages", "events"}

# os 10 tipos e os 7 estados dos avisos; os tipos de disparo e os de resumo.
NOTICE_KINDS = ("dispatch_preview", "dispatch_rejected", "dispatch_opened", "dispatch_closed",
                "summary_resent", "dispatch_cancelled", "question_failed", "bot_access_restored",
                "invite_failures", "daily_digest")
NOTICE_STATES = ("pending", "queued", "no_recipient", "no_activity", "missed", "not_sent", "gave_up")
DISPATCH_NOTICE_KINDS = ("dispatch_preview", "dispatch_rejected", "dispatch_opened", "dispatch_cancelled",
                         "question_failed")
REPORT_NOTICE_KINDS = ("dispatch_closed", "summary_resent")

# Textos sentinela (texto do aluno e texto de terceiros) para a checagem do `DETAIL` e da mensagem do
# gatilho.
STUDENT_SENTINEL = "resposta-sentinela-do-aluno-6a2f"
THIRD_PARTY_SENTINEL = "enunciado-sentinela-terceiro-7d3c"

TG_ROW = 1 << 0
TG_BEFORE = 1 << 1
TG_INSERT = 1 << 2
TG_DELETE = 1 << 3
TG_UPDATE = 1 << 4
TG_TRUNCATE = 1 << 5

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
    Tabela ou coluna ausente (esquema anterior à `0005`) sobe como está."""
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
    return _insert(conn, "people", pseudo_id=uuid.uuid4(), alias=f"abr{n:09x}", created_at=T0)


def _offering(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": uuid.uuid4(), "code": f"oferta-relatorio-{n}", "course": "Disciplina Neutra",
        "term": "2031s1", "timezone": "America/Sao_Paulo", "response_window_minutes": 60,
        "state": "active", "created_at": T0, "activated_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "offerings", **values)


def _bot(conn: psycopg.Connection, offering_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "bots", bot_uuid=uuid.uuid4(), offering_id=offering_id,
                   telegram_bot_id=6_500_000_000 + n, username=f"cq_relatorio_{n:04d}_bot",
                   token_ciphertext=bytes([1]) + secrets.token_bytes(79), token_version=1,
                   state="registered", access_problem=None, profile_state="configured",
                   registered_at=T0)


def _student(conn: psycopg.Connection, offering_id: int) -> int:
    """Uma matrícula de aluno `active` com o termo aceito; devolve o id."""
    return _insert(conn, "enrollments", person_id=_person(conn), offering_id=offering_id, role="student",
                   state="active", created_at=T0, activated_at=T1, terms_version=1,
                   terms_accepted_at=T1)


def _pending_student(conn: psycopg.Connection, offering_id: int) -> int:
    """Uma matrícula de aluno `pending` (com convite a enviar)."""
    return _insert(conn, "enrollments", person_id=_person(conn), offering_id=offering_id, role="student",
                   state="pending", created_at=T0)


def _endpoint(conn: psycopg.Connection, person_id: int, bot_id: int) -> int:
    n = next(_counter)
    return _insert(conn, "delivery_endpoints", person_id=person_id, bot_id=bot_id, channel="telegram",
                   chat_id=5_500_000_000 + n, state="active", created_at=T0, changed_at=T0)


def _update(conn: psycopg.Connection, bot_id: int, *, text: str = "texto neutro") -> int:
    n = next(_counter)
    return _insert(conn, "telegram_updates", bot_id=bot_id, update_epoch=0, update_id=840_000_000 + n,
                   kind="message", chat_id=5_500_900_000 + n, chat_type="private",
                   sender_id=5_500_900_000 + n, text=text, command=None,
                   details=Jsonb({"message_id": n, "date": 1930564800}), received_at=T1,
                   state="processed", outcome="answer_recorded", processed_at=T1)


def _plain_message(conn: psycopg.Connection, person_id: int | None, **overrides: Any) -> int:
    """Uma mensagem da API base (forma (a): pessoa, sem bot, sem endereço, sem update), com texto."""
    values: dict[str, Any] = {"recipient_person_id": person_id, "text": "mensagem neutra da API base",
                              "state": "pending", "attempts": 0, "available_at": T0, "created_at": T0}
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _question_ref(conn: psycopg.Connection, **overrides: Any) -> int:
    n = next(_counter)
    repo = overrides.pop("repo", f"https://repos.cq.test/curso-relatorio-{n}.git")
    values: dict[str, Any] = {
        "ref_uuid": uuid.uuid4(), "repo": repo,
        "repo_sha256": hashlib.sha256(repo.encode("utf-8")).hexdigest(),
        "commit_sha": hashlib.sha1(f"commit-{n}".encode()).hexdigest(),
        "qid": f"questoes/relatorio-{n}", "created_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "question_refs", **values)


_PREVIEW = {"title": "Questão neutra", "statement": "Enunciado neutro.",
            "fields": [{"type": "number", "label": None, "suffix": None, "hint": "um número"}],
            "target_count": 1, "warnings": []}
_REASONS = [{"code": "question_code_error", "message": "falha neutra", "phase": "generate"}]


def _dispatch_values(base: str) -> dict[str, Any]:
    """As colunas de um disparo agendado coerente no estado `base`: `scheduled`,
    `open`, `closed` e `rejected`."""
    values: dict[str, Any] = {
        "dispatch_uuid": uuid.uuid4(), "origin": "api", "immediate": False, "state": base,
        "opens_at": OPENS, "closes_at": CLOSES, "window_minutes": None, "preview": None,
        "rejection": None, "rejection_reasons": None, "cancel_reason": None, "created_at": T0,
        "validated_at": None, "confirm_deadline": None, "confirmed_at": None, "opened_at": None,
        "closed_at": None, "rejected_at": None, "cancelled_at": None,
    }
    validated = {"validated_at": T1, "confirm_deadline": OPENS, "preview": Jsonb(_PREVIEW)}
    if base == "rejected":
        values |= {"rejected_at": T1, "rejection": "question_broken", "rejection_reasons": Jsonb(_REASONS)}
    elif base == "scheduled":
        values |= validated
    elif base in ("open", "closed"):
        values |= validated | {"confirmed_at": T2, "opened_at": OPENS}
        if base == "closed":
            values["closed_at"] = CLOSES
    else:
        raise AssertionError(base)
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
    dispatch = _insert(conn, "dispatches", offering_id=offering, question_ref_id=ref,
                       **_dispatch_values("open"))
    return Ctx(offering, bot, person, endpoint, ref, dispatch)


def _dispatch(conn: psycopg.Connection, ctx: Ctx, base: str = "open", /, **overrides: Any) -> int:
    values = _dispatch_values(base)
    values.update(overrides)
    return _insert(conn, "dispatches", offering_id=ctx.offering, question_ref_id=ctx.ref, **values)


def _button_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode("ascii")


def _delivery(conn: psycopg.Connection, ctx: Ctx, **overrides: Any) -> int:
    """Uma entrega `pending` com a variante gravada, com matrícula nova."""
    values: dict[str, Any] = {
        "delivery_uuid": uuid.uuid4(), "dispatch_id": ctx.dispatch, "delivery_endpoint_id": ctx.endpoint,
        "seed": secrets.randbelow(2**32), "button_token": _button_token(), "state": "pending",
        "undelivered_reason": None, "failure": None, "current_field": 0, "principal_message_id": None,
        "created_at": T0, "variant_at": T1, "sent_at": None, "answered_at": None, "revealed_at": None,
    }
    values.update(overrides)
    if "enrollment_id" not in values:
        values["enrollment_id"] = _student(conn, ctx.offering)
    return _insert(conn, "deliveries", **values)


def _delivery_message(conn: psycopg.Connection, ctx: Ctx, delivery_id: int | None, *,
                      role: str | None = "question", state: str = "pending", **overrides: Any) -> int:
    """Uma mensagem de entrega do disparo (forma 2 do CHECK): `delivery_id`
    preenchido, `text` nulo, papel e operação `send` e a forma (b) de oferecimentos e bots (pessoa, bot e endereço)."""
    values: dict[str, Any] = {
        "recipient_person_id": ctx.person, "bot_id": ctx.bot, "delivery_endpoint_id": ctx.endpoint,
        "text": None, "delivery_id": delivery_id, "delivery_role": role, "operation": "send",
        "render_version": None, "media_id": "m1" if role == "media" else None,
        "field_index": 0 if role == "field_prompt" else None,
        "state": state, "attempts": 0, "available_at": T0, "created_at": T0,
    }
    if state == "sent":
        values |= {"attempts": 1, "channel_ref": str(next(_counter)), "sent_at": T1}
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _submission(conn: psycopg.Connection, ctx: Ctx, delivery_id: int, **overrides: Any) -> int:
    """Uma submissão `graded` válida com um update próprio."""
    values: dict[str, Any] = {
        "submission_uuid": uuid.uuid4(), "delivery_id": delivery_id,
        "telegram_update_id": _update(conn, ctx.bot), "outcome": "graded", "valid": True,
        "late": False, "raw_input": Jsonb({"x": "3.14"}), "score": 1.0, "received_at": T2,
        "created_at": T2, "graded_at": T3,
    }
    values.update(overrides)
    return _insert(conn, "submissions", **values)


def _report(conn: psycopg.Connection, dispatch_id: int, version: int = 1, trigger: str = "closing", /,
            **overrides: Any) -> int:
    """Uma versão congelada do relatório."""
    values: dict[str, Any] = {
        "report_uuid": uuid.uuid4(), "dispatch_id": dispatch_id, "version": version, "trigger": trigger,
        "content": Jsonb({"report_schema_version": "1", "dispatch": {"title": "Título neutro"},
                          "frozen": None}),
        "frozen_at": T3,
    }
    values.update(overrides)
    return _insert(conn, "dispatch_reports", **values)


def _notice(conn: psycopg.Connection, ctx: Ctx, kind: str, state: str = "queued", /,
            **overrides: Any) -> int:
    """Um aviso coerente do tipo `kind` no estado `state`: os tipos de disparo
    com um disparo novo (salvo `dispatch_id`) e o assunto `dispatch:<id>`; os resumos com uma versão
    nova (salvo `report_id`) e o assunto `report:<id>`; o episódio, o lote e o resumo diário com assunto
    próprio; `recipient_count` 2 em `queued` e 0 nos demais; `finished_at` nulo só em `pending`. As
    colunas de `overrides` são trocadas no fim."""
    n = next(_counter)
    values: dict[str, Any] = {
        "offering_id": ctx.offering, "kind": kind, "state": state,
        "recipient_count": 2 if state == "queued" else 0, "dispatch_id": None, "report_id": None,
        "local_date": None, "data": Jsonb({"target": 3, "delivered": 2}), "created_at": T1,
        "finished_at": None if state == "pending" else T2,
    }
    if kind in DISPATCH_NOTICE_KINDS:
        dispatch = overrides.get("dispatch_id") or _dispatch(conn, ctx, "open")
        values |= {"dispatch_id": dispatch, "subject_key": f"dispatch:{dispatch}"}
    elif kind in REPORT_NOTICE_KINDS:
        report = overrides.get("report_id") or _report(conn, _dispatch(conn, ctx, "closed"))
        row = conn.execute("SELECT dispatch_id FROM dispatch_reports WHERE id = %s", (report,)).fetchone()
        values |= {"dispatch_id": row[0] if row else ctx.dispatch, "report_id": report,
                   "subject_key": f"report:{report}"}
    elif kind == "bot_access_restored":
        values["subject_key"] = f"episode:{1000 + n}"
    elif kind == "invite_failures":
        values["subject_key"] = f"batch:{n}"
    elif kind == "daily_digest":
        day = DAY + timedelta(days=n)
        values |= {"local_date": day, "subject_key": f"digest:{day.isoformat()}"}
    else:
        raise AssertionError(kind)
    values.update(overrides)
    return _insert(conn, "team_notices", **values)


def _notice_text(conn: psycopg.Connection, ctx: Ctx, notice_id: int | None, *,
                 text_format: str | None = "html", **overrides: Any) -> int:
    """Uma mensagem de aviso de texto (forma 1 com `notice_id` e `text_format`)
    a uma pessoa por bot (forma (b) de oferecimentos e bots), com prioridade 2."""
    values: dict[str, Any] = {
        "recipient_person_id": ctx.person, "bot_id": ctx.bot, "delivery_endpoint_id": ctx.endpoint,
        "text": "Aviso neutro à equipe", "text_format": text_format, "notice_id": notice_id,
        "priority": 2, "state": "pending", "attempts": 0, "available_at": T0, "created_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _preview_message(conn: psycopg.Connection, ctx: Ctx, notice_id: int | None, *,
                     role: str | None = "question", **overrides: Any) -> int:
    """Uma mensagem da prévia (forma 3): `notice_id` preenchido, sem entrega,
    sem texto nem dialeto, pessoa + bot + endereço, papel `question` · `media` · `field_prompt`, operação
    `send`, sem versão; `media_id` só no papel `media` e `field_index` só no `field_prompt`."""
    values: dict[str, Any] = {
        "recipient_person_id": ctx.person, "bot_id": ctx.bot, "delivery_endpoint_id": ctx.endpoint,
        "text": None, "text_format": None, "notice_id": notice_id, "delivery_id": None,
        "delivery_role": role, "operation": "send", "render_version": None,
        "media_id": "m1" if role == "media" else None, "field_index": 0 if role == "field_prompt" else None,
        "priority": 2, "state": "pending", "attempts": 0, "available_at": T0, "created_at": T0,
    }
    values.update(overrides)
    return _insert(conn, "outbox_messages", **values)


def _preview(conn: psycopg.Connection, ctx: Ctx, **overrides: Any) -> int:
    """Uma prévia de um disparo agendado novo."""
    values: dict[str, Any] = {"seed": secrets.randbelow(2**32), "button_token": _button_token(),
                              "created_at": T1}
    values.update(overrides)
    if "dispatch_id" not in values:
        values["dispatch_id"] = _dispatch(conn, ctx, "scheduled")
    return _insert(conn, "dispatch_previews", **values)


def _digest(conn: psycopg.Connection, offering_id: int, **overrides: Any) -> int:
    """A configuração do resumo diário, ligada às 20:00 por padrão."""
    values: dict[str, Any] = {"offering_id": offering_id, "enabled": True, "local_time": "20:00",
                              "next_due_at": T2, "updated_at": T1}
    values.update(overrides)
    return _insert(conn, "offering_digests", **values)


def _batch(conn: psycopg.Connection, offering_id: int, **overrides: Any) -> int:
    values: dict[str, Any] = {"batch_uuid": uuid.uuid4(), "offering_id": offering_id, "created_at": T1}
    values.update(overrides)
    return _insert(conn, "invite_batches", **values)


def _invite_send(conn: psycopg.Connection, enrollment_id: int, origin: str = "batch", /,
                 **overrides: Any) -> int:
    """Um envio de convite `pending`, com a origem dada."""
    values: dict[str, Any] = {"enrollment_id": enrollment_id, "origin": origin, "valid_hours": 168,
                              "state": "pending", "attempts": 0, "created_at": T0}
    values.update(overrides)
    return _insert(conn, "invite_sends", **values)


def _columns(url: str) -> dict[tuple[str, str], tuple[str, bool, bool, str | None]]:
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT table_name, column_name, udt_name, is_nullable, is_identity, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'").fetchall()
    return {(table, column): (udt, nullable == "YES", identity == "YES", default)
            for table, column, udt, nullable, identity, default in rows}


def _key(definition: str) -> str:
    """Uma coluna-chave de índice como `pg_get_indexdef` a escreve, sem espaços, parênteses e `::text`
    (a coluna fica com o nome; a expressão `(data ->> 'dispatch_id'::text)` vira `FACT_KEY`)."""
    return re.sub(r"[\s()]", "", definition.replace("::text", ""))


def _indexes(url: str) -> set[tuple[str, str, tuple[str, ...], bool, bool, bool, str | None]]:
    """(tabela, índice, chaves em ordem, único, primário, parcial, predicado); as chaves de expressão
    entram pela definição normalizada (`_key`)."""
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname::text, ic.relname::text, "
            "ARRAY(SELECT pg_get_indexdef(i.indexrelid, k, true) "
            "      FROM generate_series(1, i.indnkeyatts::int) AS k ORDER BY k), "
            "i.indisunique, i.indisprimary, i.indpred IS NOT NULL, pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'").fetchall()
    return {(table, index, tuple(_key(k) for k in keys), unique, primary, partial, predicate)
            for table, index, keys, unique, primary, partial, predicate in rows}


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


# ---------------------------------------------------------------------------------------------
# O que a 0005 acrescenta (delta por `apply_up_to`, sem afirmar `EXPECTED_VERSION`)
# ---------------------------------------------------------------------------------------------


def test_0005_adds_exactly_the_new_tables_and_columns(empty_db_url: str) -> None:
    """As mudanças no banco entram como uma nova migração versionada, aplicada como as
    anteriores, conforme o esquema dos relatórios: num banco em `0004`, a `0005` devolve `["0005"]`,
    acrescenta exatamente as colunas de `REPORT_NEW_COLUMNS` (tipo, nulidade, identity e padrão exatos: as 7
    tabelas novas, `outbox_messages.notice_id` e `text_format` anuláveis e `invite_sends.batch_id`
    anulável), não muda nem remove nenhuma coluna anterior e cria exatamente as 7 tabelas."""
    assert apply_up_to(empty_db_url, "0004") == ["0001", "0002", "0003", "0004"]
    before = _columns(empty_db_url)
    assert {table for table, _ in before} == DISPATCH_SCHEMA_TABLES
    assert apply_up_to(empty_db_url, "0005") == ["0005"]
    after = _columns(empty_db_url)

    assert set(before) <= set(after), f"colunas removidas: {sorted(set(before) - set(after))}"
    changed = {key: (before[key], after[key]) for key in before if after[key] != before[key]}
    assert changed == {}
    added = {key: value for key, value in after.items() if key not in before}
    assert added == REPORT_NEW_COLUMNS
    assert {table for table, _ in after} - {table for table, _ in before} == REPORT_TABLES


def test_0005_adds_indexes_constraints_and_trigger_only_where_declared(empty_db_url: str) -> None:
    """Esquema dos relatórios (princípio texto × CHECK): os índices novos são
    exatamente os de `REPORT_NEW_INDEXES` (nenhum removido), com os nomes de `REPORT_NAMED_INDEXES` (inclusive o
    índice de expressão `events_dispatch_fact_idx`) e os predicados dos parciais com os literais
    da migração; os CHECKs removidos são só os recriados com o mesmo nome (`events_type_check`, agora
    com os 46 tipos, e `outbox_messages_content_check`); os novos ficam só em `team_notices`,
    `team_notice_floors`, `dispatch_previews` e `offering_digests` (pelo prefixo da tabela),
    `invite_sends` (exatamente `invite_sends_batch_check`), `outbox_messages` e `events`, e nenhum em
    `dispatch_reports`, `dispatch_preview_contents` nem `invite_batches`; as 7 chaves primárias, as 10 FKs
    novas e as unicidades declaradas; o único gatilho novo é `dispatch_reports_immutable`, `BEFORE UPDATE
    OR DELETE … FOR EACH ROW`, habilitado."""
    assert apply_up_to(empty_db_url, "0004") == ["0001", "0002", "0003", "0004"]
    indexes_before = _indexes(empty_db_url)
    constraints_before = _constraints(empty_db_url)
    triggers_before = _triggers(empty_db_url)
    assert apply_up_to(empty_db_url, "0005") == ["0005"]
    indexes_after = _indexes(empty_db_url)
    constraints_after = _constraints(empty_db_url)
    triggers_after = _triggers(empty_db_url)

    assert indexes_before - indexes_after == set()
    new_indexes = indexes_after - indexes_before
    assert sorted((table, keys, unique, primary, partial)
                  for table, _, keys, unique, primary, partial, _ in new_indexes) == REPORT_NEW_INDEXES
    by_name = {name: (table, keys, unique, partial, predicate)
               for table, name, keys, unique, _, partial, predicate in new_indexes}
    assert set(REPORT_NAMED_INDEXES) <= set(by_name), sorted(set(REPORT_NAMED_INDEXES) - set(by_name))
    for name, shape in REPORT_NAMED_INDEXES.items():
        assert by_name[name][:4] == shape, (name, by_name[name])
    for name, (literals, fragment) in REPORT_PREDICATES.items():
        predicate = by_name[name][4] or ""
        assert _string_literals(predicate) == literals, (name, predicate)
        assert fragment in predicate, (name, predicate)

    removed = constraints_before - constraints_after
    added = constraints_after - constraints_before
    assert {(table, name, kind) for table, name, kind, _ in removed} == {
        ("events", "events_type_check", "c"), ("outbox_messages", "outbox_messages_content_check", "c")}
    added_checks = {(table, name) for table, name, kind, _ in added if kind == "c"}
    assert {table for table, _ in added_checks} == REPORT_CHECK_TABLES
    assert {name for table, name in added_checks if table == "invite_sends"} == {"invite_sends_batch_check"}
    assert {name for table, name in added_checks if table == "outbox_messages"} == {
        "outbox_messages_content_check"}
    assert {name for table, name in added_checks if table == "events"} == {"events_type_check"}
    for table in ("team_notices", "team_notice_floors", "dispatch_previews", "offering_digests"):
        names = {name for t, name in added_checks if t == table}
        assert names and all(name.startswith(f"{table}_") for name in names), (table, names)
    all_checks = {table for table, _, kind, _ in constraints_after if kind == "c"}
    assert all_checks & (REPORT_TEXT_TABLES | {"invite_batches"}) == set()
    type_check = [definition for table, name, kind, definition in added if name == "events_type_check"]
    assert len(type_check) == 1
    assert _string_literals(type_check[0]) == set(PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES + REPORT_EVENT_TYPES)
    assert sorted((table, kind) for table, _, kind, _ in added if kind in ("p", "f")) == sorted(
        [(table, "p") for table in REPORT_TABLES]
        + [("dispatch_reports", "f")] + [("team_notices", "f")] * 3 + [("dispatch_previews", "f")]
        + [("dispatch_preview_contents", "f")] + [("offering_digests", "f")] + [("invite_batches", "f")]
        + [("outbox_messages", "f")] + [("invite_sends", "f")])
    unique_names = {name for _, name, kind, _ in added if kind == "u"}
    assert {"dispatch_reports_version_key", "team_notices_subject_key"} <= unique_names, unique_names
    assert {kind for _, _, kind, _ in added} <= {"c", "p", "f", "u"}, added

    assert triggers_before - triggers_after == set()
    new_triggers = triggers_after - triggers_before
    assert [(table, name) for table, name, _, _ in new_triggers] == [
        ("dispatch_reports", "dispatch_reports_immutable")]
    (_, _, tgtype, enabled), = new_triggers
    assert tgtype == TG_ROW | TG_BEFORE | TG_UPDATE | TG_DELETE, tgtype
    assert not tgtype & (TG_INSERT | TG_TRUNCATE)
    assert enabled in ("O", "A"), enabled


# ---------------------------------------------------------------------------------------------
# Migração sobre dados anteriores e pisos
# ---------------------------------------------------------------------------------------------


def _seed_dispatch(conn: psycopg.Connection) -> int:
    """Dados anteriores aos relatórios por SQL direto, nas tabelas do esquema anterior, com estados variados; devolve o `id` do
    envio de convite removido (o maior já alocado, acima do maior que fica)."""
    ctx = _ctx(conn)
    _insert(conn, "identities", key="person_id", person_id=ctx.person, full_name="Pessoa Migrada Sete",
            ra="5500000507", email="pessoa.sete@exemplo.invalid",
            email_normalized="pessoa.sete@exemplo.invalid")
    _insert(conn, "api_tokens", token_id=uuid.uuid4(), label="token anterior",
            secret_sha256=hashlib.sha256(b"segredo anterior").hexdigest(),
            permissions=["offerings:read", "offerings:write"], created_at=T0, revoked_at=None)
    _offering(conn, state="closed", activated_at=T1, closed_at=T2)
    _insert(conn, "enrollments", person_id=ctx.person, offering_id=ctx.offering, role="team",
            state="active", created_at=T0, activated_at=T1)
    sends = [_invite_send(conn, _pending_student(conn, ctx.offering), "batch", state="sent", attempts=1,
                          finished_at=T1),
             _invite_send(conn, _pending_student(conn, ctx.offering), "coordination", state="sent",
                          attempts=1, finished_at=T1),
             _invite_send(conn, _pending_student(conn, ctx.offering), "self_service")]
    closed = _dispatch(conn, ctx, "closed")
    _dispatch(conn, ctx, "rejected")
    _dispatch(conn, ctx, "scheduled")
    principal = _plain_message(conn, ctx.person, state="sent", attempts=1, channel_ref="telegram:1",
                               sent_at=T1)
    delivery = _delivery(conn, ctx, dispatch_id=closed, state="answered", current_field=None, sent_at=T2,
                         answered_at=T3, principal_message_id=principal)
    _insert(conn, "delivery_contents", key="delivery_id", delivery_id=delivery,
            snapshot=Jsonb({"params": {}}), ir=Jsonb({"fields": []}), media_refs=Jsonb({}),
            rendered=Jsonb({"format": "rich"}), parts=Jsonb({}), selection=Jsonb([]), current_render=None)
    _submission(conn, ctx, delivery)
    _delivery_message(conn, ctx, delivery, state="sent")
    data = b"\x89PNG" + b"neutro"
    _insert(conn, "media_blobs", key="sha256", sha256=hashlib.sha256(data).hexdigest(),
            content_type="image/png", size_bytes=len(data), data=data, created_at=T1)
    _insert(conn, "free_messages", offering_id=ctx.offering, delivery_endpoint_id=ctx.endpoint,
            telegram_update_id=_update(conn, ctx.bot), text="texto livre neutro", replied=True,
            ambiguous=False, received_at=T2, created_at=T2)
    _insert(conn, "offering_contacts", offering_id=ctx.offering, email="prof.neutro@exemplo.invalid",
            updated_at=T1)
    _plain_message(conn, ctx.person, bot_id=ctx.bot, delivery_endpoint_id=ctx.endpoint)
    _plain_message(conn, None, bot_id=ctx.bot, reply_to_update_id=_update(conn, ctx.bot), priority=1)
    for event_type in ("person.registered", "offering.created", "dispatch.opened", "dispatch.closed"):
        _insert(conn, "events", key="seq", type=event_type, occurred_at=T1, actor_token_id=None,
                data=Jsonb({"dispatch_id": str(uuid.uuid4())}))
    for state, finished in (("pending", None), ("done", T2)):
        _insert(conn, "tasks", task_type="dispatch.close", payload=Jsonb({"dispatch_row_id": closed}),
                idempotency_key=f"dispatch.close:{closed}:{state}", state=state,
                attempts=0 if state == "pending" else 1, available_at=T1, lease_owner=None,
                lease_until=None, last_error=None, created_at=T0, finished_at=finished)
    # O maior envio alocado sai: o piso é o maior `id` presente, e não o último da sequência.
    conn.execute("DELETE FROM invite_sends WHERE id = %s", (sends[-1],))
    return sends[-1]


def test_0005_over_dispatch_database_preserves_rows_and_records_floors(empty_db_url: str) -> None:
    """Esquema dos relatórios (os pisos gravados pela própria migração: o maior `events.seq` e o
    maior `invite_sends.id` no momento da migração; colunas novas anuláveis
    para os dados anteriores): um banco em `0004` com dados anteriores recebe a `0005`
    (`apply_up_to(…, "0005") == ["0005"]`); cada linha das 22 tabelas do esquema anterior fica igual em todas as
    colunas anteriores (o `outbox_messages_content_check` recriado aceita as linhas anteriores), as
    colunas novas ficam nulas, as tabelas novas nascem vazias, salvo `team_notice_floors`, com a única
    linha `(1, maior seq, maior id de envio)` (o envio removido antes da migração, acima do maior
    presente, não conta), `schema_migrations` ganha a `0005`, e aplicar de novo não muda nada."""
    assert apply_up_to(empty_db_url, "0004") == ["0001", "0002", "0003", "0004"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        deleted_send = _seed_dispatch(conn)
        r5_columns = _table_columns(conn)
        before = {table: _snapshot(conn, table, columns) for table, columns in r5_columns.items()}
        max_seq = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
        max_send = conn.execute("SELECT max(id) FROM invite_sends").fetchone()[0]
        counts = {table: _count(conn, table) for table in r5_columns}
    assert set(r5_columns) == DISPATCH_SCHEMA_TABLES
    assert max_seq is not None and max_send is not None and deleted_send > max_send
    assert all(counts[table] > 0 for table in DISPATCH_SCHEMA_TABLES - {"invites"}), counts

    assert apply_up_to(empty_db_url, "0005") == ["0005"]

    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        after = {table: _snapshot(conn, table, columns) for table, columns in r5_columns.items()}
        assert {k: v for k, v in after.items() if k != "schema_migrations"} == {
            k: v for k, v in before.items() if k != "schema_migrations"}
        assert after["schema_migrations"][:4] == before["schema_migrations"]
        assert [row[0] for row in after["schema_migrations"]] == ["0001", "0002", "0003", "0004", "0005"]
        assert conn.execute("SELECT notice_id, text_format FROM outbox_messages ORDER BY id").fetchall() == [
            (None, None)] * counts["outbox_messages"]
        assert conn.execute("SELECT batch_id FROM invite_sends ORDER BY id").fetchall() == [
            (None,)] * counts["invite_sends"]
        for table in sorted(REPORT_TABLES - {"team_notice_floors"}):
            assert _count(conn, table) == 0, table
        assert conn.execute(
            "SELECT id, event_floor, invite_send_floor FROM team_notice_floors ORDER BY id").fetchall() == [
            (1, max_seq, max_send)]
        stable_columns = _table_columns(conn)
        stable = {table: _snapshot(conn, table, columns) for table, columns in stable_columns.items()}

    assert apply_up_to(empty_db_url, "0005") == []
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _table_columns(conn) == stable_columns
        assert {table: _snapshot(conn, table, columns)
                for table, columns in stable_columns.items()} == stable


def test_0005_floors_are_zero_on_an_empty_database(empty_db_url: str) -> None:
    """Esquema dos relatórios (0 no banco vazio): num banco vazio em `0004` (sem eventos nem
    envios de convite), a `0005` grava a única linha de `team_notice_floors` como `(1, 0, 0)`."""
    assert apply_up_to(empty_db_url, "0004") == ["0001", "0002", "0003", "0004"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert _count(conn, "events") == 0 and _count(conn, "invite_sends") == 0
    assert apply_up_to(empty_db_url, "0005") == ["0005"]
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        assert conn.execute(
            "SELECT id, event_floor, invite_send_floor FROM team_notice_floors ORDER BY id").fetchall() == [
            (1, 0, 0)]


def test_team_notice_floors_has_only_the_row_with_id_1(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (`id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1)`: uma única linha):
    no banco migrado vazio, a linha `(1, 0, 0)`; outra linha com `id = 2` é recusada por um CHECK
    `team_notice_floors_*`, e uma segunda com `id = 1` (explícito ou pelo padrão) pela chave primária; a
    linha não muda."""
    rows = sql.execute("SELECT id, event_floor, invite_send_floor FROM team_notice_floors").fetchall()
    assert rows == [(1, 0, 0)]
    _refused("id-2", psycopg.errors.CheckViolation,
             lambda: _insert(sql, "team_notice_floors", id=2, event_floor=0, invite_send_floor=0),
             prefix="team_notice_floors_")
    _refused("id-0", psycopg.errors.CheckViolation,
             lambda: _insert(sql, "team_notice_floors", id=0, event_floor=0, invite_send_floor=0),
             prefix="team_notice_floors_")
    _refused("second-id-1", psycopg.errors.UniqueViolation,
             lambda: _insert(sql, "team_notice_floors", id=1, event_floor=5, invite_send_floor=5),
             table="team_notice_floors")
    _refused("second-by-default", psycopg.errors.UniqueViolation,
             lambda: _insert(sql, "team_notice_floors", event_floor=5, invite_send_floor=5),
             table="team_notice_floors")
    assert sql.execute("SELECT id, event_floor, invite_send_floor FROM team_notice_floors").fetchall() == rows


# ---------------------------------------------------------------------------------------------
# CHECKs de `team_notices`
# ---------------------------------------------------------------------------------------------


def test_team_notices_checks(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (`kind` nos 10 tipos; `state` nos 7;
    `recipient_count >= 0` e 0 fora de `queued`; `(finished_at IS NULL) = (state = 'pending')`;
    `local_date` se e só se `daily_digest`; `report_id` se e só se `dispatch_closed`/`summary_resent`;
    `dispatch_id` não nulo nos tipos de disparo; `no_activity` e `missed` só no `daily_digest`; `not_sent`
    só em `bot_access_restored` e `dispatch_closed`; `gave_up` só em `dispatch_preview`; `subject_key ~
    '^(dispatch|report|episode|batch|window|digest):[0-9-]{1,32}$'`): cada tipo coerente em `queued`
    (com as duas formas de assunto dos convites), cada estado onde é permitido e o assunto com 32
    caracteres são aceitos; cada forma incoerente (inclusive outra caixa, a quebra de linha no fim do
    assunto e o assunto com 33 caracteres) é recusada por um CHECK `team_notices_*`."""
    ctx = _ctx(sql)
    expected: list[tuple[str, str, int]] = []

    def accept(kind: str, state: str = "queued", **overrides: Any) -> int:
        notice = _notice(sql, ctx, kind, state, **overrides)
        expected.append((kind, state, 2 if state == "queued" else 0))
        return notice

    ids = [accept(kind) for kind in NOTICE_KINDS]
    ids.append(accept("invite_failures", subject_key="window:123456"))
    ids += [accept("dispatch_opened", "pending"), accept("daily_digest", "pending"),
            accept("dispatch_rejected", "no_recipient"), accept("daily_digest", "no_recipient"),
            accept("daily_digest", "no_activity"), accept("daily_digest", "missed"),
            accept("bot_access_restored", "not_sent"), accept("dispatch_closed", "not_sent"),
            accept("dispatch_preview", "gave_up"), accept("bot_access_restored", subject_key="episode:" + "9" * 32),
            accept("invite_failures", subject_key="batch:" + "1" * 32)]

    report = _report(sql, _dispatch(sql, ctx, "closed"))
    refused: list[tuple[str, str, str, dict[str, Any]]] = [
        ("kind-unknown", "dispatch_opened", "queued", {"kind": "dispatch_reopened"}),
        ("kind-other-case", "daily_digest", "queued", {"kind": "Daily_digest"}),
        ("kind-empty", "bot_access_restored", "queued", {"kind": ""}),
        ("state-unknown", "dispatch_opened", "queued", {"state": "sent"}),
        ("state-other-case", "dispatch_opened", "queued", {"state": "Queued"}),
        ("count-negative", "dispatch_opened", "queued", {"recipient_count": -1}),
        ("count-in-no-recipient", "dispatch_rejected", "no_recipient", {"recipient_count": 2}),
        ("count-in-pending", "dispatch_opened", "pending", {"recipient_count": 1}),
        ("count-in-not-sent", "dispatch_closed", "not_sent", {"recipient_count": 1}),
        ("pending-with-finished", "dispatch_opened", "pending", {"finished_at": T2}),
        ("queued-without-finished", "dispatch_opened", "queued", {"finished_at": None}),
        ("no-recipient-without-finished", "bot_access_restored", "no_recipient", {"finished_at": None}),
        ("digest-without-local-date", "daily_digest", "queued", {"local_date": None}),
        ("local-date-outside-digest", "dispatch_opened", "queued", {"local_date": DAY}),
        ("closed-without-report", "dispatch_closed", "queued", {"report_id": None}),
        ("resent-without-report", "summary_resent", "queued", {"report_id": None}),
        ("report-in-opened", "dispatch_opened", "queued", {"report_id": report}),
        ("report-in-digest", "daily_digest", "queued", {"report_id": report}),
        ("rejected-without-dispatch", "dispatch_rejected", "queued", {"dispatch_id": None}),
        ("preview-without-dispatch", "dispatch_preview", "queued", {"dispatch_id": None}),
        ("no-activity-outside-digest", "dispatch_opened", "no_activity", {}),
        ("missed-outside-digest", "bot_access_restored", "missed", {}),
        ("not-sent-in-opened", "dispatch_opened", "not_sent", {}),
        ("not-sent-in-digest", "daily_digest", "not_sent", {}),
        ("not-sent-in-resent", "summary_resent", "not_sent", {}),
        ("gave-up-in-opened", "dispatch_opened", "gave_up", {}),
        ("gave-up-in-digest", "daily_digest", "gave_up", {}),
        ("subject-unknown-prefix", "bot_access_restored", "queued", {"subject_key": "offering:1"}),
        ("subject-other-case", "bot_access_restored", "queued", {"subject_key": "Episode:1"}),
        ("subject-without-digits", "bot_access_restored", "queued", {"subject_key": "episode:"}),
        ("subject-letters", "bot_access_restored", "queued", {"subject_key": "episode:abc"}),
        ("subject-mixed", "bot_access_restored", "queued", {"subject_key": "episode:12a"}),
        ("subject-33-chars", "bot_access_restored", "queued", {"subject_key": "episode:" + "9" * 33}),
        ("subject-space", "bot_access_restored", "queued", {"subject_key": "episode: 1"}),
        ("subject-trailing-newline", "bot_access_restored", "queued", {"subject_key": "episode:1\n"}),
        ("subject-two-colons", "bot_access_restored", "queued", {"subject_key": "report:1:2"}),
        ("subject-without-colon", "bot_access_restored", "queued", {"subject_key": "episode1"}),
        ("subject-empty", "bot_access_restored", "queued", {"subject_key": ""}),
    ]
    for label, kind, state, overrides in refused:
        _refused(label, psycopg.errors.CheckViolation,
                 lambda k=kind, s=state, o=overrides: _notice(sql, ctx, k, s, **o), prefix="team_notices_")
    rows = sql.execute("SELECT id, kind, state, recipient_count FROM team_notices ORDER BY id").fetchall()
    assert rows == [(i, kind, state, count) for i, (kind, state, count) in zip(ids, expected)]
    assert sql.execute("SELECT kind, local_date IS NOT NULL, report_id IS NOT NULL, dispatch_id IS NOT NULL "
                       "FROM team_notices WHERE id = ANY(%s) ORDER BY id", (ids[:10],)).fetchall() == [
        (kind, kind == "daily_digest", kind in REPORT_NOTICE_KINDS,
         kind in DISPATCH_NOTICE_KINDS + REPORT_NOTICE_KINDS) for kind in NOTICE_KINDS]


def test_team_notices_subject_key_is_unique_per_offering_and_kind(sql: psycopg.Connection) -> None:
    """A chave de idempotência do aviso: tipo e assunto; esquema dos relatórios
    (`team_notices_subject_key UNIQUE (offering_id, kind, subject_key)`): o segundo aviso do mesmo tipo e
    assunto no mesmo oferecimento é recusado por essa restrição, com o `DETAIL` só com a chave; o mesmo
    assunto com outro tipo e o mesmo tipo e assunto em outro oferecimento são aceitos; o `INSERT … ON
    CONFLICT (offering_id, kind, subject_key) DO NOTHING RETURNING id` não grava nem falha."""
    ctx = _ctx(sql)
    other = _ctx(sql)
    dispatch = _dispatch(sql, ctx, "open")
    subject = f"dispatch:{dispatch}"
    first = _notice(sql, ctx, "dispatch_opened", "pending", dispatch_id=dispatch)
    exc = _refused("same-kind-and-subject", psycopg.errors.UniqueViolation,
                   lambda: _notice(sql, ctx, "dispatch_opened", "queued", dispatch_id=dispatch),
                   constraint="team_notices_subject_key", table="team_notices")
    assert exc.diag.message_detail == (
        f"Key (offering_id, kind, subject_key)=({ctx.offering}, dispatch_opened, {subject}) already exists.")
    other_kind = _notice(sql, ctx, "dispatch_cancelled", dispatch_id=dispatch)
    other_offering = _notice(sql, other, "dispatch_opened", dispatch_id=dispatch)
    inserted = sql.execute(
        "INSERT INTO team_notices (offering_id, kind, subject_key, state, dispatch_id, data, created_at) "
        "VALUES (%s, 'dispatch_opened', %s, 'pending', %s, %s, %s) "
        "ON CONFLICT (offering_id, kind, subject_key) DO NOTHING RETURNING id",
        (ctx.offering, subject, dispatch, Jsonb({}), T3)).fetchall()
    assert inserted == []
    assert sql.execute("SELECT id, offering_id, kind, subject_key, state FROM team_notices ORDER BY id"
                       ).fetchall() == [
        (first, ctx.offering, "dispatch_opened", subject, "pending"),
        (other_kind, ctx.offering, "dispatch_cancelled", subject, "queued"),
        (other_offering, other.offering, "dispatch_opened", subject, "queued")]
    # Os padrões do esquema: `recipient_count` 0 e `data` `{}`.
    defaulted = sql.execute(
        "INSERT INTO team_notices (offering_id, kind, subject_key, state, created_at) "
        "VALUES (%s, 'bot_access_restored', 'episode:77', 'pending', %s) "
        "RETURNING recipient_count, data, finished_at", (ctx.offering, T3)).fetchone()
    assert defaulted == (0, {}, None)


# ---------------------------------------------------------------------------------------------
# CHECKs de `dispatch_previews`, `offering_digests` e `invite_sends`
# ---------------------------------------------------------------------------------------------


def test_dispatch_previews_checks_and_uniques(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (`seed BETWEEN 0 AND 4294967295`; `button_token ~
    '^[A-Za-z0-9_-]{22}$'`; `dispatch_id` e `button_token` únicos): as seeds 0 e 4294967295 e os tokens de
    22 caracteres do alfabeto (inclusive `-` e `_`) são aceitos; 4294967296, -1 e os tokens fora da forma
    são recusados por um CHECK `dispatch_previews_*`; a segunda prévia do mesmo disparo e o token repetido
    são recusados por unicidade, com o `DETAIL` só com a chave; `dispatch_preview_contents` aceita a IR
    sem `media_refs` e sem `rendered` e depois a gravação deles."""
    ctx = _ctx(sql)
    tokens = ["A" * 22, "abcdefghijklmnopqrstu-", "_" * 12 + "0123456789", _button_token()]
    assert all(len(token) == 22 for token in tokens)
    accepted = [_preview(sql, ctx, seed=0, button_token=tokens[0]),
                _preview(sql, ctx, seed=4294967295, button_token=tokens[1]),
                _preview(sql, ctx, seed=7, button_token=tokens[2]),
                _preview(sql, ctx, seed=8, button_token=tokens[3])]
    refused: list[tuple[str, dict[str, Any]]] = [
        ("seed-2-32", {"seed": 4294967296}),
        ("seed-negative", {"seed": -1}),
        ("token-21", {"button_token": "A" * 21}),
        ("token-23", {"button_token": "A" * 23}),
        ("token-plus", {"button_token": "A" * 21 + "+"}),
        ("token-slash", {"button_token": "A" * 21 + "/"}),
        ("token-padding", {"button_token": "A" * 20 + "=="}),
        ("token-space", {"button_token": "A" * 21 + " "}),
        ("token-accent", {"button_token": "A" * 21 + "á"}),
        ("token-newline", {"button_token": "A" * 22 + "\n"}),
        ("token-empty", {"button_token": ""}),
    ]
    for label, columns in refused:
        _refused(label, psycopg.errors.CheckViolation, lambda c=columns: _preview(sql, ctx, **c),
                 prefix="dispatch_previews_")
    dispatch = sql.execute("SELECT dispatch_id FROM dispatch_previews WHERE id = %s",
                           (accepted[0],)).fetchone()[0]
    exc = _refused("same-dispatch", psycopg.errors.UniqueViolation,
                   lambda: _preview(sql, ctx, dispatch_id=dispatch), table="dispatch_previews")
    assert exc.diag.message_detail == f"Key (dispatch_id)=({dispatch}) already exists."
    exc = _refused("same-token", psycopg.errors.UniqueViolation,
                   lambda: _preview(sql, ctx, button_token=tokens[1]), table="dispatch_previews")
    assert exc.diag.message_detail == f"Key (button_token)=({tokens[1]}) already exists."
    assert sql.execute("SELECT id, seed, button_token FROM dispatch_previews ORDER BY id").fetchall() == [
        (accepted[0], 0, tokens[0]), (accepted[1], 4294967295, tokens[1]), (accepted[2], 7, tokens[2]),
        (accepted[3], 8, tokens[3])]

    _insert(sql, "dispatch_preview_contents", key="preview_id", preview_id=accepted[0],
            ir=Jsonb({"fields": [{"type": "number"}]}))
    assert sql.execute("SELECT media_refs, rendered FROM dispatch_preview_contents WHERE preview_id = %s",
                       (accepted[0],)).fetchone() == (None, None)
    sql.execute("UPDATE dispatch_preview_contents SET media_refs = %s, rendered = %s WHERE preview_id = %s",
                (Jsonb({"m1": "0" * 64}), Jsonb({"question": {"text_format": "rich"}}), accepted[0]))
    assert sql.execute("SELECT media_refs, rendered FROM dispatch_preview_contents WHERE preview_id = %s",
                       (accepted[0],)).fetchone() == ({"m1": "0" * 64}, {"question": {"text_format": "rich"}})


def test_offering_digests_checks_and_unique(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (`local_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'`;
    `next_due_at` não nulo se e só se `enabled`; uma linha por oferecimento): `00:00`, `09:05`, `20:00` e
    `23:59`, ligados com o próximo horário e desligados sem ele, são aceitos; `24:00`, `7:00`, `23:60` e
    as demais formas fora do padrão, o ligado sem próximo horário e o desligado com ele são recusados por
    um CHECK `offering_digests_*`; a segunda linha do mesmo oferecimento é recusada por unicidade, com o
    `DETAIL` só com a chave."""
    accepted_values = [("00:00", True, T2), ("09:05", False, None), ("20:00", True, T3), ("23:59", False, None)]
    offerings = [_offering(sql) for _ in accepted_values]
    ids = [_digest(sql, offering, local_time=local_time, enabled=enabled, next_due_at=due)
           for offering, (local_time, enabled, due) in zip(offerings, accepted_values)]
    refused: list[tuple[str, dict[str, Any]]] = [
        ("time-24-00", {"local_time": "24:00"}),
        ("time-one-digit-hour", {"local_time": "7:00"}),
        ("time-minute-60", {"local_time": "23:60"}),
        ("time-one-digit-minute", {"local_time": "20:0"}),
        ("time-with-seconds", {"local_time": "20:00:00"}),
        ("time-leading-space", {"local_time": " 20:00"}),
        ("time-trailing-newline", {"local_time": "20:00\n"}),
        ("time-letter", {"local_time": "2O:00"}),
        ("time-h", {"local_time": "20h00"}),
        ("time-empty", {"local_time": ""}),
        ("enabled-without-due", {"enabled": True, "next_due_at": None}),
        ("disabled-with-due", {"enabled": False, "next_due_at": T2}),
    ]
    for label, columns in refused:
        _refused(label, psycopg.errors.CheckViolation, lambda c=columns: _digest(sql, _offering(sql), **c),
                 prefix="offering_digests_")
    exc = _refused("same-offering", psycopg.errors.UniqueViolation,
                   lambda: _digest(sql, offerings[0], enabled=False, next_due_at=None),
                   table="offering_digests")
    assert exc.diag.message_detail == f"Key (offering_id)=({offerings[0]}) already exists."
    assert sql.execute("SELECT id, offering_id, local_time, enabled, next_due_at FROM offering_digests "
                       "ORDER BY id").fetchall() == [
        (i, offering, local_time, enabled, due)
        for i, offering, (local_time, enabled, due) in zip(ids, offerings, accepted_values)]


def test_invite_batches_and_invite_sends_batch_check(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (`invite_batches.batch_uuid` único; `invite_sends_batch_check CHECK
    (batch_id IS NULL OR origin = 'batch')`; as linhas anteriores com `batch_id` nulo): o envio de lote com
    o lote e sem ele e os de coordenação e de autosserviço sem lote são aceitos; os de coordenação e de
    autosserviço com lote são recusados por esse CHECK; o `batch_uuid` repetido é recusado por unicidade."""
    ctx = _ctx(sql)
    batch = _batch(sql, ctx.offering)
    accepted = [("batch", batch), ("batch", None), ("coordination", None), ("self_service", None)]
    ids = [_invite_send(sql, _pending_student(sql, ctx.offering), origin, batch_id=batch_id)
           for origin, batch_id in accepted]
    for label, origin in (("coordination-with-batch", "coordination"), ("self-service-with-batch", "self_service")):
        _refused(label, psycopg.errors.CheckViolation,
                 lambda o=origin: _invite_send(sql, _pending_student(sql, ctx.offering), o, batch_id=batch),
                 constraint="invite_sends_batch_check")
    batch_uuid = sql.execute("SELECT batch_uuid FROM invite_batches WHERE id = %s", (batch,)).fetchone()[0]
    exc = _refused("same-batch-uuid", psycopg.errors.UniqueViolation,
                   lambda: _batch(sql, ctx.offering, batch_uuid=batch_uuid), table="invite_batches")
    assert exc.diag.message_detail == f"Key (batch_uuid)=({batch_uuid}) already exists."
    assert sql.execute("SELECT id, origin, batch_id FROM invite_sends ORDER BY id").fetchall() == [
        (i, origin, batch_id) for i, (origin, batch_id) in zip(ids, accepted)]


# ---------------------------------------------------------------------------------------------
# `outbox_messages_content_check` recriado com o mesmo nome
# ---------------------------------------------------------------------------------------------


def test_outbox_content_check_three_forms(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (o `outbox_messages_content_check`
    recriado com o mesmo nome e três formas: (1) texto, com `text_format` nulo ou `plain` · `html` ·
    `rich` e `notice_id` nulo ou não, sem colunas de entrega; (2) entrega do disparo, com `notice_id` e
    `text_format` nulos; (3) mensagem da prévia: `notice_id`, sem entrega, sem texto nem dialeto, pessoa +
    bot + endereço sem update, papel `question` · `media` · `field_prompt`, operação `send`, sem versão,
    `media_id ~ '^m[1-9][0-9]{0,5}$'` só no `media` e `field_index >= 0` só no `field_prompt`): cada forma
    é aceita; cada condição de cada forma, quebrada, é recusada por esse CHECK, inclusive as linhas
    "plain-without-text" e "plain-with-role" do congelado `test_outbox_content_check`
    (`test_migration_0004.py:1316-1360`), que continuam recusadas; a prioridade 2 é aceita (fora do
    CHECK)."""
    ctx = _ctx(sql)
    delivery = _delivery(sql, ctx)
    update = _update(sql, ctx.bot)
    notice = _notice(sql, ctx, "dispatch_opened")
    preview = _notice(sql, ctx, "dispatch_preview")

    r2 = _plain_message(sql, ctx.person)
    r3_reply = _plain_message(sql, None, bot_id=ctx.bot, reply_to_update_id=update, priority=1)
    texts = [_notice_text(sql, ctx, notice, text_format=fmt) for fmt in ("plain", "html", "rich")]
    text_without_notice = _notice_text(sql, ctx, None, text_format="rich")
    text_without_format = _notice_text(sql, ctx, notice, text_format=None)
    delivery_message = _delivery_message(sql, ctx, delivery)
    previews = [_preview_message(sql, ctx, preview, role="question"),
                _preview_message(sql, ctx, preview, role="media"),
                _preview_message(sql, ctx, preview, role="media", media_id="m999999"),
                _preview_message(sql, ctx, preview, role="field_prompt"),
                _preview_message(sql, ctx, preview, role="field_prompt", field_index=3)]

    refused: list[tuple[str, Callable[[], Any]]] = [
        # As duas linhas do congelado, que continuam recusadas.
        ("plain-without-text", lambda: _plain_message(sql, ctx.person, text=None)),
        ("plain-with-role", lambda: _plain_message(sql, ctx.person, delivery_role="question")),
        # Forma 1 (texto).
        ("text-format-unknown", lambda: _notice_text(sql, ctx, notice, text_format="markdown")),
        ("text-format-other-case", lambda: _notice_text(sql, ctx, notice, text_format="HTML")),
        ("text-format-empty", lambda: _notice_text(sql, ctx, notice, text_format="")),
        ("text-format-without-text", lambda: _notice_text(sql, ctx, notice, text=None, text_format="plain")),
        ("notice-without-text", lambda: _notice_text(sql, ctx, notice, text=None, text_format=None)),
        ("text-with-role", lambda: _notice_text(sql, ctx, notice, delivery_role="question")),
        ("text-with-operation", lambda: _notice_text(sql, ctx, notice, operation="send")),
        ("text-with-media-id", lambda: _notice_text(sql, ctx, notice, media_id="m1")),
        ("text-with-field-index", lambda: _notice_text(sql, ctx, notice, field_index=0)),
        # Forma 2 (entrega do disparo).
        ("delivery-with-notice", lambda: _delivery_message(sql, ctx, delivery, notice_id=notice)),
        ("delivery-with-text-format", lambda: _delivery_message(sql, ctx, delivery, text_format="rich")),
        # Forma 3 (mensagem da prévia), cada condição.
        ("preview-with-text", lambda: _preview_message(sql, ctx, preview, text="texto guardado")),
        ("preview-with-text-format", lambda: _preview_message(sql, ctx, preview, text_format="rich")),
        ("preview-with-delivery", lambda: _preview_message(sql, ctx, preview, delivery_id=delivery)),
        ("preview-without-notice", lambda: _preview_message(sql, ctx, None)),
        ("preview-in-form-a",
         lambda: _preview_message(sql, ctx, preview, bot_id=None, delivery_endpoint_id=None)),
        ("preview-in-form-c",
         lambda: _preview_message(sql, ctx, preview, recipient_person_id=None, delivery_endpoint_id=None,
                                  reply_to_update_id=update)),
        ("preview-role-fallback", lambda: _preview_message(sql, ctx, preview, role="fallback")),
        ("preview-role-notice", lambda: _preview_message(sql, ctx, preview, role="notice")),
        ("preview-role-null", lambda: _preview_message(sql, ctx, preview, role=None)),
        ("preview-role-other-case", lambda: _preview_message(sql, ctx, preview, role="Question")),
        ("preview-edit-text",
         lambda: _preview_message(sql, ctx, preview, operation="edit_text", render_version=2)),
        ("preview-edit-markup",
         lambda: _preview_message(sql, ctx, preview, operation="edit_markup", render_version=2)),
        ("preview-without-operation", lambda: _preview_message(sql, ctx, preview, operation=None)),
        ("preview-operation-other-case", lambda: _preview_message(sql, ctx, preview, operation="Send")),
        ("preview-with-render-version", lambda: _preview_message(sql, ctx, preview, render_version=0)),
        ("preview-media-id-on-question", lambda: _preview_message(sql, ctx, preview, media_id="m1")),
        ("preview-media-id-m0", lambda: _preview_message(sql, ctx, preview, role="media", media_id="m0")),
        ("preview-media-id-7-digits",
         lambda: _preview_message(sql, ctx, preview, role="media", media_id="m1000000")),
        ("preview-media-id-letter", lambda: _preview_message(sql, ctx, preview, role="media", media_id="x1")),
        ("preview-field-index-on-question", lambda: _preview_message(sql, ctx, preview, field_index=0)),
        ("preview-field-index-on-media",
         lambda: _preview_message(sql, ctx, preview, role="media", field_index=0)),
        ("preview-field-index-negative",
         lambda: _preview_message(sql, ctx, preview, role="field_prompt", field_index=-1)),
    ]
    for label, insert in refused:
        _refused(label, psycopg.errors.CheckViolation, insert, constraint="outbox_messages_content_check")

    rows = sql.execute("SELECT id, text IS NULL, text_format, notice_id, delivery_id, delivery_role, "
                       "media_id, field_index, priority FROM outbox_messages ORDER BY id").fetchall()
    assert rows == [
        (r2, False, None, None, None, None, None, None, 0),
        (r3_reply, False, None, None, None, None, None, None, 1),
        (texts[0], False, "plain", notice, None, None, None, None, 2),
        (texts[1], False, "html", notice, None, None, None, None, 2),
        (texts[2], False, "rich", notice, None, None, None, None, 2),
        (text_without_notice, False, "rich", None, None, None, None, None, 2),
        (text_without_format, False, None, notice, None, None, None, None, 2),
        (delivery_message, True, None, None, delivery, "question", None, None, 0),
        (previews[0], True, None, preview, None, "question", None, None, 2),
        (previews[1], True, None, preview, None, "media", "m1", None, 2),
        (previews[2], True, None, preview, None, "media", "m999999", None, 2),
        (previews[3], True, None, preview, None, "field_prompt", None, 0, 2),
        (previews[4], True, None, preview, None, "field_prompt", None, 3, 2)]


# ---------------------------------------------------------------------------------------------
# `dispatch_reports`: gatilho de imutabilidade e tabelas com texto sem CHECK
# ---------------------------------------------------------------------------------------------


def test_dispatch_reports_immutable_trigger(sql: psycopg.Connection) -> None:
    """A versão congelada não muda; esquema dos relatórios (gatilho `dispatch_reports_immutable`
    `BEFORE UPDATE OR DELETE … FOR EACH ROW`, que lança exceção com mensagem fixa, sem dados, como os de
    `events`; o `TRUNCATE` não é barrado): todo `UPDATE` (de uma coluna, de uma linha ou de todas) e todo
    `DELETE` (de uma linha ou de todas) é recusado com `RaiseException`, sempre com a mesma mensagem,
    sem `DETAIL`, sem o conteúdo, sem o `report_uuid` e sem número; as linhas ficam como estavam; o
    `INSERT` de uma versão nova continua aceito e o `TRUNCATE … CASCADE` esvazia a tabela."""
    ctx = _ctx(sql)
    closed = _dispatch(sql, ctx, "closed")
    content = Jsonb({"dispatch": {"title": THIRD_PARTY_SENTINEL},
                     "distribution": {"texts": [STUDENT_SENTINEL]}})
    first = _report(sql, closed, 1, "closing", content=content)
    second = _report(sql, closed, 2, "resend", content=content)
    uuids = [str(row[0]) for row in sql.execute("SELECT report_uuid FROM dispatch_reports ORDER BY id")]
    columns = ("id", "report_uuid", "dispatch_id", "version", "trigger", "content", "frozen_at")
    before = _snapshot(sql, "dispatch_reports", columns)

    statements: list[tuple[str, str, tuple[Any, ...]]] = [
        ("update-content", "UPDATE dispatch_reports SET content = %s WHERE id = %s", (Jsonb({}), first)),
        ("update-version", "UPDATE dispatch_reports SET version = 9 WHERE id = %s", (second,)),
        ("update-trigger", "UPDATE dispatch_reports SET trigger = 'resend' WHERE id = %s", (first,)),
        ("update-all", "UPDATE dispatch_reports SET frozen_at = %s", (T4,)),
        ("update-noop", "UPDATE dispatch_reports SET version = version WHERE id = %s", (first,)),
        ("delete-one", "DELETE FROM dispatch_reports WHERE id = %s", (second,)),
        ("delete-all", "DELETE FROM dispatch_reports", ()),
    ]
    messages: set[str] = set()
    for label, statement, params in statements:
        with pytest.raises(psycopg.errors.RaiseException) as info:
            sql.execute(statement, params)
        diag = info.value.diag
        message = diag.message_primary or ""
        assert message, label
        assert diag.message_detail is None, (label, diag.message_detail)
        for forbidden in (THIRD_PARTY_SENTINEL, STUDENT_SENTINEL, *uuids):
            assert forbidden not in str(info.value), (label, forbidden)
        assert not re.search(r"[0-9]", message), (label, message)
        messages.add(message)
    assert len(messages) == 1, messages
    assert _snapshot(sql, "dispatch_reports", columns) == before

    third = _report(sql, closed, 3, "resend")
    assert [row[0] for row in sql.execute("SELECT id FROM dispatch_reports ORDER BY id")] == [
        first, second, third]
    sql.execute("TRUNCATE dispatch_reports CASCADE")
    assert _count(sql, "dispatch_reports") == 0


def _assert_detail_without_text(exc: psycopg.Error, expected_detail: str, label: str) -> None:
    detail = exc.diag.message_detail or ""
    assert detail == expected_detail, (label, detail)
    for sentinel in (STUDENT_SENTINEL, THIRD_PARTY_SENTINEL):
        assert sentinel not in str(exc) and sentinel not in detail, label


def test_text_tables_have_no_check_and_violations_do_not_show_text(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (princípio texto × CHECK: a violação de um
    CHECK mostra a linha inteira no `DETAIL`): `dispatch_reports` (o `content`, com o título
    de terceiros e os textos mais comuns dos alunos) e `dispatch_preview_contents` (a IR da amostra, com o
    gabarito, e a renderização) não têm CHECK; com texto sentinela do aluno e de terceiros gravado, as
    violações de unicidade (`dispatch_reports_version_key`, `dispatch_reports_one_closing_idx`, o
    `report_uuid`, a chave primária do conteúdo da prévia) e de FK mostram só a chave, sem nenhum dos
    dois textos."""
    checks = sql.execute(
        "SELECT c.conrelid::regclass::text, c.conname::text FROM pg_constraint c "
        "JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE n.nspname = 'public' AND c.contype = 'c' AND c.conrelid::regclass::text = ANY(%s)",
        (sorted(REPORT_TEXT_TABLES),)).fetchall()
    assert checks == []
    assert {row[0] for row in sql.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name = ANY(%s)", (sorted(REPORT_TEXT_TABLES),)).fetchall()} == REPORT_TEXT_TABLES

    ctx = _ctx(sql)
    closed = _dispatch(sql, ctx, "closed")
    content = Jsonb({"dispatch": {"title": THIRD_PARTY_SENTINEL},
                     "distribution": {"fields": [{"texts": {"items": [{"value": STUDENT_SENTINEL}]}}]}})
    report = _report(sql, closed, 1, "closing", content=content)
    report_uuid = sql.execute("SELECT report_uuid FROM dispatch_reports WHERE id = %s", (report,)).fetchone()[0]
    preview = _preview(sql, ctx)
    ir = Jsonb({"statement_html": THIRD_PARTY_SENTINEL, "answer_key": [{"display_html": THIRD_PARTY_SENTINEL}]})
    rendered = Jsonb({"question": {"markup": THIRD_PARTY_SENTINEL}})
    _insert(sql, "dispatch_preview_contents", key="preview_id", preview_id=preview, ir=ir,
            media_refs=Jsonb({}), rendered=rendered)
    missing_dispatch = sql.execute("SELECT max(id) + 1000 FROM dispatches").fetchone()[0]
    missing_preview = sql.execute("SELECT max(id) + 1000 FROM dispatch_previews").fetchone()[0]

    cases: list[tuple[str, type[psycopg.Error], Callable[[], Any], str]] = [
        ("report-same-version", psycopg.errors.UniqueViolation,
         lambda: _report(sql, closed, 1, "resend", content=content),
         f"Key (dispatch_id, version)=({closed}, 1) already exists."),
        ("report-second-closing", psycopg.errors.UniqueViolation,
         lambda: _report(sql, closed, 2, "closing", content=content),
         f"Key (dispatch_id)=({closed}) already exists."),
        ("report-same-uuid", psycopg.errors.UniqueViolation,
         lambda: _report(sql, closed, 3, "resend", report_uuid=report_uuid, content=content),
         f"Key (report_uuid)=({report_uuid}) already exists."),
        ("report-of-missing-dispatch", psycopg.errors.ForeignKeyViolation,
         lambda: _report(sql, missing_dispatch, 1, "closing", content=content),
         f'Key (dispatch_id)=({missing_dispatch}) is not present in table "dispatches".'),
        ("preview-contents-same-preview", psycopg.errors.UniqueViolation,
         lambda: _insert(sql, "dispatch_preview_contents", key="preview_id", preview_id=preview, ir=ir,
                         media_refs=Jsonb({}), rendered=rendered),
         f"Key (preview_id)=({preview}) already exists."),
        ("preview-contents-of-missing-preview", psycopg.errors.ForeignKeyViolation,
         lambda: _insert(sql, "dispatch_preview_contents", key="preview_id", preview_id=missing_preview,
                         ir=ir, media_refs=Jsonb({}), rendered=rendered),
         f'Key (preview_id)=({missing_preview}) is not present in table "dispatch_previews".'),
    ]
    for label, error, insert, detail in cases:
        exc = _refused(label, error, insert)
        _assert_detail_without_text(exc, detail, label)
    assert _refused("report-second-closing-name", psycopg.errors.UniqueViolation,
                    lambda: _report(sql, closed, 4, "closing"),
                    constraint="dispatch_reports_one_closing_idx", table="dispatch_reports")
    assert _refused("report-same-version-name", psycopg.errors.UniqueViolation,
                    lambda: _report(sql, closed, 1, "resend"),
                    constraint="dispatch_reports_version_key", table="dispatch_reports")
    resend = _report(sql, closed, 2, "resend", content=content)
    assert sql.execute("SELECT id, version, trigger FROM dispatch_reports ORDER BY id").fetchall() == [
        (report, 1, "closing"), (resend, 2, "resend")]
    assert _count(sql, "dispatch_preview_contents") == 1


def test_new_foreign_keys_refuse_missing_rows(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (as 10 FKs novas:
    `dispatch_reports.dispatch_id`, `team_notices.offering_id`/`dispatch_id`/`report_id`,
    `dispatch_previews.dispatch_id`, `dispatch_preview_contents.preview_id`,
    `offering_digests.offering_id`, `invite_batches.offering_id`, `outbox_messages.notice_id` e
    `invite_sends.batch_id`): com as linhas existentes, cada linha nova é aceita; cada linha que aponta
    para uma linha inexistente é recusada com `ForeignKeyViolation` na tabela certa."""
    ctx = _ctx(sql)
    report = _report(sql, _dispatch(sql, ctx, "closed"))
    notice = _notice(sql, ctx, "dispatch_closed", report_id=report)
    preview = _preview(sql, ctx)
    batch = _batch(sql, ctx.offering)
    accepted = [
        _insert(sql, "dispatch_preview_contents", key="preview_id", preview_id=preview, ir=Jsonb({})),
        _digest(sql, ctx.offering),
        _notice_text(sql, ctx, notice),
        _invite_send(sql, _pending_student(sql, ctx.offering), "batch", batch_id=batch),
    ]
    assert len(accepted) == 4
    missing = {table: sql.execute(pgsql.SQL("SELECT coalesce(max(id), 0) + 1000 FROM {}").format(
        pgsql.Identifier(table))).fetchone()[0]
        for table in ("offerings", "dispatches", "dispatch_reports", "team_notices", "dispatch_previews",
                      "invite_batches")}
    cases: list[tuple[str, Callable[[], Any], str]] = [
        ("report-of-missing-dispatch", lambda: _report(sql, missing["dispatches"]), "dispatch_reports"),
        ("notice-of-missing-offering",
         lambda: _notice(sql, ctx, "bot_access_restored", offering_id=missing["offerings"]), "team_notices"),
        ("notice-of-missing-dispatch",
         lambda: _notice(sql, ctx, "dispatch_opened", dispatch_id=missing["dispatches"]), "team_notices"),
        ("notice-of-missing-report",
         lambda: _notice(sql, ctx, "summary_resent", report_id=missing["dispatch_reports"]), "team_notices"),
        ("preview-of-missing-dispatch",
         lambda: _preview(sql, ctx, dispatch_id=missing["dispatches"]), "dispatch_previews"),
        ("preview-contents-of-missing-preview",
         lambda: _insert(sql, "dispatch_preview_contents", key="preview_id",
                         preview_id=missing["dispatch_previews"], ir=Jsonb({})), "dispatch_preview_contents"),
        ("digest-of-missing-offering",
         lambda: _digest(sql, missing["offerings"]), "offering_digests"),
        ("batch-of-missing-offering", lambda: _batch(sql, missing["offerings"]), "invite_batches"),
        ("message-of-missing-notice",
         lambda: _notice_text(sql, ctx, missing["team_notices"]), "outbox_messages"),
        ("send-of-missing-batch",
         lambda: _invite_send(sql, _pending_student(sql, ctx.offering), "batch",
                              batch_id=missing["invite_batches"]), "invite_sends"),
    ]
    for label, insert, table in cases:
        _refused(label, psycopg.errors.ForeignKeyViolation, insert, table=table)
    assert sql.execute("SELECT n.id, n.report_id, r.dispatch_id = n.dispatch_id FROM team_notices n "
                       "JOIN dispatch_reports r ON r.id = n.report_id ORDER BY n.id").fetchall() == [
        (notice, report, True)]


# ---------------------------------------------------------------------------------------------
# Tipos de evento
# ---------------------------------------------------------------------------------------------


def test_event_type_check_accepts_exactly_the_46_types(sql: psycopg.Connection) -> None:
    """Esquema dos relatórios (o `events_type_check` recriado com os 43 tipos e os 3 novos
    (`report.frozen`, `notice.recorded`, `offering.digest_changed`)): cada um dos 46 tipos é aceito; um tipo
    fora deles (outra caixa, um prefixo, um tipo de tarefa dos relatórios e um nome vizinho) é recusado por esse
    CHECK."""
    refused_types = []
    for event_type in PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES + REPORT_EVENT_TYPES:
        try:
            _insert(sql, "events", key="seq", type=event_type, occurred_at=T0, actor_token_id=None,
                    data=Jsonb({}))
        except psycopg.errors.CheckViolation:
            refused_types.append(event_type)
    assert refused_types == [], (
        "tipos recusados pelo CHECK de eventos (o esquema exige os 46)")
    for label, event_type in (("other-case", "Notice.recorded"), ("prefix", "notice."),
                              ("task-type", "notice.send"), ("task-type-digest", "digest.daily"),
                              ("task-type-freeze", "report.freeze"), ("neighbor", "report.created"),
                              ("neighbor-digest", "offering.digest_updated")):
        _refused(label, psycopg.errors.CheckViolation,
                 lambda t=event_type: _insert(sql, "events", key="seq", type=t, occurred_at=T0,
                                              actor_token_id=None, data=Jsonb({})),
                 constraint="events_type_check")
    assert [row[0] for row in sql.execute("SELECT type FROM events ORDER BY seq").fetchall()] == list(
        PRE_DISPATCH_EVENT_TYPES + DISPATCH_EVENT_TYPES + REPORT_EVENT_TYPES)
