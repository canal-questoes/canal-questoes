"""Migrações versionadas.

Os testes de migração usam um banco **vazio** (`empty_db_url`) e chamam `apply_migrations` direto.
O esquema esperado é o das migrações. As asserções sobre
o gatilho de lock consultivo, o `CACHE 1` da sequência e os gatilhos `ENABLE ALWAYS` conferem o
mecanismo fixado nas migrações (o mecanismo como contrato).

Oferecimentos e bots: o esquema esperado
ganha as 6 tabelas novas (`offerings`, `bots`, `enrollments`, `invites`, `delivery_endpoints`,
`telegram_updates`) e as 3 colunas novas de `outbox_messages`; `recipient_person_id` passa a aceitar
nulo; identity, chaves primárias, FKs e unicidades novas, inclusive a
composta `telegram_updates (bot_id, update_epoch, update_id)` e os índices únicos parciais. Os
CHECKs e a migração sobre dados da API base ficam em `test_migration_0002.py`.

Vínculo do aluno (lista e convites): o esquema esperado ganha a
tabela `invite_sends` e as colunas novas de `enrollments` (`terms_version`, `terms_accepted_at`),
`invites` (`invite_send_id`, `presented_update_id`, `presented_terms_version`) e `offerings`
(`invite_code_sha256`, `invite_code_expires_at`, `invite_valid_hours`, `invite_eligible_count`),
todas anuláveis; a coluna identity, a chave primária e as 3 FKs novas (`invite_sends.enrollment_id`,
`invites.invite_send_id` e `invites.presented_update_id`); nenhum índice único novo. Os CHECKs, os
índices não únicos e a migração sobre dados de oferecimentos e bots ficam em `test_migration_0003.py`.

Disparo e resposta (com `outbox_messages.text` anulável):
o esquema esperado ganha as 7 tabelas novas
(`question_refs`, `dispatches`, `deliveries`, `delivery_contents`, `media_blobs`, `submissions` e
`free_messages`), as 7 colunas novas de `outbox_messages` (`delivery_id`, `delivery_role`, `operation`,
`render_version`, `media_id`, `field_index` e `priority`, esta `NOT NULL` com padrão 0) e a coluna
`telegram_updates.reply_to_message_id`, todas as outras anuláveis; `outbox_messages.text` passa a aceitar
nulo (as mensagens de entrega não guardam texto, como já acontecia
com oferecimentos e bots em `recipient_person_id`); as 5 colunas identity, as 7 chaves primárias
(`delivery_contents` pela entrega e `media_blobs` pelo SHA-256), as unicidades de uma coluna
(`ref_uuid`, `dispatch_uuid`, `delivery_uuid`, `button_token`, `submission_uuid` e os dois
`telegram_update_id`), as multicoluna `question_refs_key (repo_sha256, commit_sha, qid)` e
`deliveries_dispatch_enrollment_key (dispatch_id, enrollment_id)`, as parciais
(`submissions_one_final_idx`, `submissions_one_grading_idx` e `outbox_messages_pending_edit_key`) e as
13 FKs novas, inclusive a circular `deliveries.principal_message_id → outbox_messages.id` ×
`outbox_messages.delivery_id → deliveries.id`. `test_apply_migrations_returns_packaged_versions_in_order`
deriva a lista do pacote. Os CHECKs, os índices não únicos, o delta exato da `0004` e a
migração sobre dados do vínculo do aluno ficam em `test_migration_0004.py`. No esqueleto (sem a `0004`), as três
comparações de esquema ficam vermelhas pela migração ausente.

Disparo e resposta, com as mensagens livres opcionais por oferecimento, desligadas por padrão:
a `0004` também acrescenta a coluna `offerings.free_messages` (`boolean NOT NULL
DEFAULT false`) e a tabela `offering_contacts` (`id` identity, chave primária; `offering_id` único, FK para
`offerings.id`; `email` e `updated_at`, todos não nulos, sem CHECK). O esquema esperado ganha a coluna, a
tabela, a coluna identity, a chave primária, a unicidade e a FK, e o padrão `false` da coluna nova. Sem a
coluna e a tabela, as três comparações de esquema ficam vermelhas.

Relatórios e acompanhamento (acréscimos):
o esquema esperado ganha as 7 tabelas novas (`dispatch_reports`, `team_notices`,
`team_notice_floors`, `dispatch_previews`, `dispatch_preview_contents`, `offering_digests` e
`invite_batches`), as colunas `outbox_messages.notice_id` e `text_format` e `invite_sends.batch_id`,
anuláveis; as 5 colunas identity novas (`team_notice_floors.id` é `smallint DEFAULT 1`, sem identity, e
`dispatch_preview_contents` é 1:1 pela prévia); os padrões `team_notices.recipient_count = 0`,
`team_notices.data = '{}'` e `team_notice_floors.id = 1`; as 7 chaves primárias; as unicidades de uma coluna
(`report_uuid`, `dispatch_previews.dispatch_id`, `button_token`, `offering_digests.offering_id`,
`batch_uuid`), as multicoluna `dispatch_reports_version_key (dispatch_id, version)` e
`team_notices_subject_key (offering_id, kind, subject_key)`, a parcial `dispatch_reports_one_closing_idx
(dispatch_id) WHERE trigger = 'closing'`; e as 10 FKs novas (nada aponta para `events`). Os CHECKs, os
índices não únicos, o gatilho de imutabilidade, o delta exato da `0005` e a migração sobre dados do disparo ficam
em `test_migration_0005.py`. No esqueleto (sem a `0005`), as três comparações de esquema ficam vermelhas por
a migração ausente.

Além das colunas, a `0005` dos relatórios entra
em `IDENTITY_COLUMNS` (5 entradas; a comparação é exata, então sem elas a função falha),
em `EXPECTED_UNIQUE_COLUMNS` (5; comparação por inclusão) e nas três asserções de padrão
acima (`team_notices.recipient_count`, `team_notices.data`, `team_notice_floors.id`), todas tiradas do
esquema da migração.

O CLI (com `delivery_endpoints.chat_id` anulável):
o esquema esperado ganha as 5 colunas
novas de `offerings` (`purged_at`, `purge_result`, `purge_code_sha256`, `purge_code_expires_at` e
`purge_prepared_counts`), todas anuláveis; nenhuma tabela, identity, chave, FK nem índice novo.
A função não enumera CHECKs: os dois novos (`offerings_purge_check` e
`offerings_purge_code_check`) ficam em `test_migration_0006.py`, com o delta exato da `0006`. A outra mudança:
`delivery_endpoints.chat_id` passa a aceitar nulo (o expurgo anula o identificador do chat, e a tabela é
referenciada por FK, sem como apagar a linha); a unicidade parcial `(bot_id, chat_id)` não muda. No
esqueleto (sem a `0006`), a comparação de colunas fica vermelha pela migração ausente.

Implantação e piloto (acréscimo):
o esquema esperado ganha a tabela `mail_probe` (`id` boolean, `result` text e
`checked_at` timestamptz, todas não nulas) e a chave primária em `id`; nenhuma identity, FK nem unicidade
nova. A função não enumera CHECKs: o `DEFAULT true` e os dois CHECKs (`id` sempre true e o domínio de
`result`) ficam em `test_migration_0007.py`. A `0007` é real no
esqueleto (o esquema é contrato), então as duas comparações passam já no esqueleto.

Banco de questões local (acréscimo):
o esquema esperado ganha a tabela `question_banks` (os pacotes enviados, uma linha por nome e versão), com
`id` identity e chave primária, `bank_uuid` único, a unicidade `(name, version)` e nenhuma FK; só
`git_commit` e `actor_token_id` são anuláveis. A função não enumera CHECKs: os da tabela e o
`events_type_check` com o tipo novo ficam em `test_migration_0008.py`. A `0008` é real no esqueleto (o
esquema é contrato), então as comparações passam já no esqueleto.

"""

from __future__ import annotations

import importlib.resources
import re
import threading
import time
from typing import Any

import psycopg
import pytest
from psycopg import conninfo as pg_conninfo

from cq_server.migrate import EXPECTED_VERSION, apply_migrations, current_version

MIGRATION_FILE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")

# Bits de `pg_trigger.tgtype` (src/include/catalog/pg_trigger.h).
TG_ROW = 1 << 0
TG_BEFORE = 1 << 1
TG_INSERT = 1 << 2
TG_DELETE = 1 << 3
TG_UPDATE = 1 << 4
TG_TRUNCATE = 1 << 5

ADVISORY_XACT_LOCK = re.compile(r"pg_advisory_xact_lock\s*\(", re.IGNORECASE)

# coluna -> (tipo em `udt_name`, anulável). `int8` = bigint, `int4` = integer,
# `_text` = text[].
EXPECTED_COLUMNS: dict[str, dict[str, tuple[str, bool]]] = {
    "schema_migrations": {
        "version": ("text", False),
        "applied_at": ("timestamptz", False),
    },
    "people": {
        "id": ("int8", False),
        "pseudo_id": ("uuid", False),
        "alias": ("text", False),
        "created_at": ("timestamptz", False),
    },
    "identities": {
        "person_id": ("int8", False),
        "full_name": ("text", False),
        "ra": ("text", True),
        "email": ("text", True),
        "email_normalized": ("text", True),
    },
    "api_tokens": {
        "id": ("int8", False),
        "token_id": ("uuid", False),
        "label": ("text", False),
        "secret_sha256": ("text", False),
        "permissions": ("_text", False),
        "created_at": ("timestamptz", False),
        "revoked_at": ("timestamptz", True),
    },
    "events": {
        "seq": ("int8", False),
        "type": ("text", False),
        "occurred_at": ("timestamptz", False),
        "actor_token_id": ("text", True),
        "data": ("jsonb", False),
    },
    "tasks": {
        "id": ("int8", False),
        "task_type": ("text", False),
        "payload": ("jsonb", False),
        "idempotency_key": ("text", True),
        "state": ("text", False),
        "attempts": ("int4", False),
        "available_at": ("timestamptz", False),
        "lease_owner": ("text", True),
        "lease_until": ("timestamptz", True),
        "last_error": ("text", True),
        "created_at": ("timestamptz", False),
        "finished_at": ("timestamptz", True),
    },
    "outbox_messages": {
        "id": ("int8", False),
        # Oferecimentos e bots: a resposta a quem não está vinculado não tem pessoa.
        "recipient_person_id": ("int8", True),
        # Disparo e resposta: as mensagens de entrega não guardam texto.
        "text": ("text", True),
        "idempotency_key": ("text", True),
        "state": ("text", False),
        "attempts": ("int4", False),
        "available_at": ("timestamptz", False),
        "lease_owner": ("text", True),
        "lease_until": ("timestamptz", True),
        "channel_ref": ("text", True),
        "last_error": ("text", True),
        "created_at": ("timestamptz", False),
        "sent_at": ("timestamptz", True),
        # Oferecimentos e bots: o destino por bot.
        "bot_id": ("int8", True),
        "delivery_endpoint_id": ("int8", True),
        "reply_to_update_id": ("int8", True),
        # Disparo e resposta: a mensagem de entrega (entrega, papel, operação, versão, mídia,
        # campo) e a prioridade (0, menos as respostas fixas às mensagens livres).
        "delivery_id": ("int8", True),
        "delivery_role": ("text", True),
        "operation": ("text", True),
        "render_version": ("int4", True),
        "media_id": ("text", True),
        "field_index": ("int2", True),
        "priority": ("int2", False),
        # Relatórios: o aviso à equipe e o dialeto do texto.
        "notice_id": ("int8", True),
        "text_format": ("text", True),
    },
    # Oferecimentos e bots: as 6 tabelas novas.
    "offerings": {
        "id": ("int8", False),
        "offering_id": ("uuid", False),
        "code": ("text", False),
        "course": ("text", False),
        "term": ("text", False),
        "timezone": ("text", False),
        "response_window_minutes": ("int4", False),
        "state": ("text", False),
        "created_at": ("timestamptz", False),
        "activated_at": ("timestamptz", True),
        "closed_at": ("timestamptz", True),
        "archived_at": ("timestamptz", True),
        "archive_code_sha256": ("text", True),
        "archive_code_expires_at": ("timestamptz", True),
        # Vínculo do aluno: o preparo do envio dos convites.
        "invite_code_sha256": ("text", True),
        "invite_code_expires_at": ("timestamptz", True),
        "invite_valid_hours": ("int4", True),
        "invite_eligible_count": ("int4", True),
        # Disparo e resposta: as mensagens livres do oferecimento, desligadas por padrão.
        "free_messages": ("bool", False),
        # CLI: a marca e o
        # resultado do expurgo e o código de confirmação pendente, todos anuláveis.
        "purged_at": ("timestamptz", True),
        "purge_result": ("jsonb", True),
        "purge_code_sha256": ("text", True),
        "purge_code_expires_at": ("timestamptz", True),
        "purge_prepared_counts": ("jsonb", True),
    },
    "bots": {
        "id": ("int8", False),
        "bot_uuid": ("uuid", False),
        "offering_id": ("int8", False),
        "telegram_bot_id": ("int8", False),
        "username": ("text", False),
        "token_ciphertext": ("bytea", False),
        "token_version": ("int4", False),
        "state": ("text", False),
        "access_problem": ("text", True),
        "profile_state": ("text", False),
        "lease_owner": ("text", True),
        "lease_until": ("timestamptz", True),
        "last_poll_ok_at": ("timestamptz", True),
        "last_problem": ("text", True),
        "last_problem_at": ("timestamptz", True),
        "update_epoch": ("int4", False),
        "last_update_at": ("timestamptz", True),
        "registered_at": ("timestamptz", False),
        "token_replaced_at": ("timestamptz", True),
    },
    "enrollments": {
        "id": ("int8", False),
        "person_id": ("int8", False),
        "offering_id": ("int8", False),
        "role": ("text", False),
        "state": ("text", False),
        "created_at": ("timestamptz", False),
        "activated_at": ("timestamptz", True),
        # Vínculo do aluno: o termo aceito.
        "terms_version": ("int4", True),
        "terms_accepted_at": ("timestamptz", True),
    },
    "invites": {
        "id": ("int8", False),
        "enrollment_id": ("int8", False),
        "code_sha256": ("text", False),
        "state": ("text", False),
        "expires_at": ("timestamptz", False),
        "created_at": ("timestamptz", False),
        "used_at": ("timestamptz", True),
        "revoked_at": ("timestamptz", True),
        # Vínculo do aluno: o envio que criou o convite e a apresentação do termo.
        "invite_send_id": ("int8", True),
        "presented_update_id": ("int8", True),
        "presented_terms_version": ("int4", True),
    },
    "delivery_endpoints": {
        "id": ("int8", False),
        "person_id": ("int8", False),
        "bot_id": ("int8", False),
        "channel": ("text", False),
        # CLI: `chat_id` anulável
        "chat_id": ("int8", True),
        "state": ("text", False),
        "created_at": ("timestamptz", False),
        "changed_at": ("timestamptz", False),
    },
    "telegram_updates": {
        "id": ("int8", False),
        "bot_id": ("int8", False),
        "update_epoch": ("int4", False),
        "update_id": ("int8", False),
        "kind": ("text", False),
        "chat_id": ("int8", True),
        "chat_type": ("text", True),
        "sender_id": ("int8", True),
        "text": ("text", True),
        "command": ("text", True),
        "details": ("jsonb", False),
        "received_at": ("timestamptz", False),
        "state": ("text", False),
        "outcome": ("text", True),
        "processed_at": ("timestamptz", True),
        # Disparo e resposta: o número da mensagem respondida.
        "reply_to_message_id": ("int8", True),
    },
    # Vínculo do aluno: a tabela nova dos envios de convite.
    "invite_sends": {
        "id": ("int8", False),
        "enrollment_id": ("int8", False),
        "origin": ("text", False),
        "valid_hours": ("int4", False),
        "state": ("text", False),
        "failure": ("text", True),
        "cancel_reason": ("text", True),
        "error_type": ("text", True),
        "attempts": ("int4", False),
        "created_at": ("timestamptz", False),
        "finished_at": ("timestamptz", True),
        # Relatórios: o lote de convites do envio.
        "batch_id": ("int8", True),
    },
    # Disparo e resposta: as 7 tabelas novas. `int2` = smallint, `float8` = double
    # precision, `bool` = boolean.
    "question_refs": {
        "id": ("int8", False),
        "ref_uuid": ("uuid", False),
        "repo": ("text", False),
        "repo_sha256": ("text", False),
        "commit_sha": ("text", False),
        "qid": ("text", False),
        "supported": ("bool", True),
        "show_correct_answer": ("bool", True),
        "single_variant": ("bool", True),
        "partial_credit": ("bool", True),
        "title": ("text", True),
        "answers_names": ("_text", True),
        "content_hash": ("text", True),
        "inspected_at": ("timestamptz", True),
        "created_at": ("timestamptz", False),
    },
    "dispatches": {
        "id": ("int8", False),
        "dispatch_uuid": ("uuid", False),
        "offering_id": ("int8", False),
        "question_ref_id": ("int8", False),
        "origin": ("text", False),
        "immediate": ("bool", False),
        "opens_at": ("timestamptz", True),
        "closes_at": ("timestamptz", True),
        "window_minutes": ("int4", True),
        "state": ("text", False),
        "preview": ("jsonb", True),
        "rejection": ("text", True),
        "rejection_reasons": ("jsonb", True),
        "cancel_reason": ("text", True),
        "created_at": ("timestamptz", False),
        "validated_at": ("timestamptz", True),
        "confirm_deadline": ("timestamptz", True),
        "confirmed_at": ("timestamptz", True),
        "opened_at": ("timestamptz", True),
        "closed_at": ("timestamptz", True),
        "rejected_at": ("timestamptz", True),
        "cancelled_at": ("timestamptz", True),
    },
    "deliveries": {
        "id": ("int8", False),
        "delivery_uuid": ("uuid", False),
        "dispatch_id": ("int8", False),
        "enrollment_id": ("int8", False),
        "delivery_endpoint_id": ("int8", False),
        "seed": ("int8", False),
        "button_token": ("text", False),
        "state": ("text", False),
        "undelivered_reason": ("text", True),
        "failure": ("text", True),
        "current_field": ("int2", True),
        "render_version": ("int4", False),
        "controls_version": ("int4", False),
        "principal_message_id": ("int8", True),
        "created_at": ("timestamptz", False),
        "variant_at": ("timestamptz", True),
        "sent_at": ("timestamptz", True),
        "answered_at": ("timestamptz", True),
        "revealed_at": ("timestamptz", True),
    },
    "delivery_contents": {
        "delivery_id": ("int8", False),
        "snapshot": ("jsonb", False),
        "ir": ("jsonb", False),
        "media_refs": ("jsonb", False),
        "rendered": ("jsonb", False),
        "parts": ("jsonb", False),
        "selection": ("jsonb", False),
        "current_render": ("jsonb", True),
    },
    "media_blobs": {
        "sha256": ("text", False),
        "content_type": ("text", False),
        "size_bytes": ("int4", False),
        "data": ("bytea", False),
        "created_at": ("timestamptz", False),
    },
    "submissions": {
        "id": ("int8", False),
        "submission_uuid": ("uuid", False),
        "delivery_id": ("int8", False),
        "telegram_update_id": ("int8", False),
        "outcome": ("text", False),
        "valid": ("bool", True),
        "refusal": ("text", True),
        "failure_code": ("text", True),
        "failure_phase": ("text", True),
        "late": ("bool", True),
        "normalized_fields": ("_text", False),
        "raw_input": ("jsonb", False),
        "submitted_answers": ("jsonb", True),
        "format_errors": ("jsonb", True),
        "feedback": ("jsonb", True),
        "params": ("jsonb", True),
        "partial_scores": ("jsonb", True),
        "correct_answers": ("jsonb", True),
        "score": ("float8", True),
        "part_derived": ("jsonb", True),
        "engine_mismatch": ("bool", True),
        "pl_mismatch": ("bool", True),
        "response_ms": ("int8", True),
        "delay_notice_sent": ("bool", False),
        "received_at": ("timestamptz", False),
        "created_at": ("timestamptz", False),
        "graded_at": ("timestamptz", True),
    },
    "free_messages": {
        "id": ("int8", False),
        "offering_id": ("int8", False),
        "delivery_endpoint_id": ("int8", False),
        "telegram_update_id": ("int8", False),
        "text": ("text", False),
        "replied": ("bool", False),
        # texto com mais de um campo digitável aberto (a resposta fixa dele é o `USE_REPLY`).
        "ambiguous": ("bool", False),
        "received_at": ("timestamptz", False),
        "created_at": ("timestamptz", False),
    },
    # Disparo e resposta (mensagens livres): o contato do professor, uma linha por oferecimento, sem CHECK.
    "offering_contacts": {
        "id": ("int8", False),
        "offering_id": ("int8", False),
        "email": ("text", False),
        "updated_at": ("timestamptz", False),
    },
    # Relatórios: as 7 tabelas novas. `date` = date.
    "dispatch_reports": {
        "id": ("int8", False),
        "report_uuid": ("uuid", False),
        "dispatch_id": ("int8", False),
        "version": ("int4", False),
        "trigger": ("text", False),
        "content": ("jsonb", False),
        "frozen_at": ("timestamptz", False),
    },
    "team_notices": {
        "id": ("int8", False),
        "offering_id": ("int8", False),
        "kind": ("text", False),
        "subject_key": ("text", False),
        "state": ("text", False),
        "recipient_count": ("int4", False),
        "dispatch_id": ("int8", True),
        "report_id": ("int8", True),
        "local_date": ("date", True),
        "data": ("jsonb", False),
        "created_at": ("timestamptz", False),
        "finished_at": ("timestamptz", True),
    },
    # [I] os pisos não nulos: a migração sempre os grava (0 no banco vazio).
    "team_notice_floors": {
        "id": ("int2", False),
        "event_floor": ("int8", False),
        "invite_send_floor": ("int8", False),
    },
    "dispatch_previews": {
        "id": ("int8", False),
        "dispatch_id": ("int8", False),
        "seed": ("int8", False),
        "button_token": ("text", False),
        "created_at": ("timestamptz", False),
    },
    # [I] `media_refs` e `rendered` anuláveis: preenchidos pela tarefa da prévia.
    "dispatch_preview_contents": {
        "preview_id": ("int8", False),
        "ir": ("jsonb", False),
        "media_refs": ("jsonb", True),
        "rendered": ("jsonb", True),
    },
    "offering_digests": {
        "id": ("int8", False),
        "offering_id": ("int8", False),
        "enabled": ("bool", False),
        "local_time": ("text", False),
        "next_due_at": ("timestamptz", True),
        "updated_at": ("timestamptz", False),
    },
    "invite_batches": {
        "id": ("int8", False),
        "batch_uuid": ("uuid", False),
        "offering_id": ("int8", False),
        "created_at": ("timestamptz", False),
    },
    # Implantação e piloto (acréscimo): a linha única
    # do último teste do servidor de e-mail. O `DEFAULT true` e os dois CHECKs ficam em
    # `test_migration_0007.py`.
    "mail_probe": {
        "id": ("bool", False),
        "result": ("text", False),
        "checked_at": ("timestamptz", False),
    },
    # Banco de questões local (acréscimo): os pacotes enviados, imutáveis. Os CHECKs ficam em
    # `test_migration_0008.py`.
    "question_banks": {
        "id": ("int8", False),
        "bank_uuid": ("uuid", False),
        "name": ("text", False),
        "version": ("text", False),
        "source": ("text", False),
        "git_commit": ("text", True),
        "archive": ("bytea", False),
        "archive_bytes": ("int8", False),
        "file_count": ("int4", False),
        "byte_count": ("int8", False),
        "question_count": ("int4", False),
        "actor_token_id": ("uuid", True),
        "created_at": ("timestamptz", False),
    },
}

# as colunas "bigint identity".
IDENTITY_COLUMNS = {
    ("people", "id"),
    ("api_tokens", "id"),
    ("events", "seq"),
    ("tasks", "id"),
    ("outbox_messages", "id"),
    # Oferecimentos e bots
    ("offerings", "id"),
    ("bots", "id"),
    ("enrollments", "id"),
    ("invites", "id"),
    ("delivery_endpoints", "id"),
    ("telegram_updates", "id"),
    # Vínculo do aluno
    ("invite_sends", "id"),
    # Disparo e resposta: `delivery_contents` e `media_blobs` não têm identity.
    ("question_refs", "id"),
    ("dispatches", "id"),
    ("deliveries", "id"),
    ("submissions", "id"),
    ("free_messages", "id"),
    # Disparo e resposta (mensagens livres)
    ("offering_contacts", "id"),
    # Relatórios: `team_notice_floors` (`smallint DEFAULT 1`) e
    # `dispatch_preview_contents` (pela prévia) não têm identity.
    ("dispatch_reports", "id"),
    ("team_notices", "id"),
    ("dispatch_previews", "id"),
    ("offering_digests", "id"),
    ("invite_batches", "id"),
    # Banco de questões local: o `id` é o cursor da listagem.
    ("question_banks", "id"),
}

EXPECTED_PRIMARY_KEYS = {
    "schema_migrations": ("version",),
    "people": ("id",),
    "identities": ("person_id",),
    "api_tokens": ("id",),
    "events": ("seq",),
    "tasks": ("id",),
    "outbox_messages": ("id",),
    # Oferecimentos e bots
    "offerings": ("id",),
    "bots": ("id",),
    "enrollments": ("id",),
    "invites": ("id",),
    "delivery_endpoints": ("id",),
    "telegram_updates": ("id",),
    # Vínculo do aluno
    "invite_sends": ("id",),
    # Disparo e resposta: o conteúdo 1:1 pela entrega; os bytes pelo SHA-256.
    "question_refs": ("id",),
    "dispatches": ("id",),
    "deliveries": ("id",),
    "delivery_contents": ("delivery_id",),
    "media_blobs": ("sha256",),
    "submissions": ("id",),
    "free_messages": ("id",),
    # Disparo e resposta (mensagens livres)
    "offering_contacts": ("id",),
    # Relatórios: a linha única dos pisos pelo `id = 1`; o conteúdo da prévia 1:1
    # pela prévia.
    "dispatch_reports": ("id",),
    "team_notices": ("id",),
    "team_notice_floors": ("id",),
    "dispatch_previews": ("id",),
    "dispatch_preview_contents": ("preview_id",),
    "offering_digests": ("id",),
    "invite_batches": ("id",),
    # Implantação e piloto (acréscimo): a linha única pelo `id` (boolean, sempre true).
    "mail_probe": ("id",),
    # Banco de questões local (acréscimo).
    "question_banks": ("id",),
}

EXPECTED_UNIQUE_COLUMNS = {
    ("people", "pseudo_id"),
    ("people", "alias"),
    ("identities", "ra"),
    ("identities", "email_normalized"),
    ("api_tokens", "token_id"),
    ("api_tokens", "secret_sha256"),
    ("tasks", "idempotency_key"),
    ("outbox_messages", "idempotency_key"),
    # Oferecimentos e bots (um bot do Telegram, um oferecimento, sempre)
    ("offerings", "offering_id"),
    ("offerings", "code"),
    ("bots", "bot_uuid"),
    ("bots", "offering_id"),
    ("bots", "telegram_bot_id"),
    ("invites", "code_sha256"),
    # Disparo e resposta: os `uuid` que saem, o `button_token` e o update que
    # origina uma submissão ou uma mensagem livre (gravação idempotente).
    ("question_refs", "ref_uuid"),
    ("dispatches", "dispatch_uuid"),
    ("deliveries", "delivery_uuid"),
    ("deliveries", "button_token"),
    ("submissions", "submission_uuid"),
    ("submissions", "telegram_update_id"),
    ("free_messages", "telegram_update_id"),
    # Disparo e resposta (mensagens livres): no máximo um contato por oferecimento.
    ("offering_contacts", "offering_id"),
    # Relatórios: os `uuid` que saem, uma prévia por disparo, o
    # `button_token` da prévia e uma configuração do resumo diário por oferecimento.
    ("dispatch_reports", "report_uuid"),
    ("dispatch_previews", "dispatch_id"),
    ("dispatch_previews", "button_token"),
    ("offering_digests", "offering_id"),
    ("invite_batches", "batch_uuid"),
    # Banco de questões local: o identificador externo, reservado.
    ("question_banks", "bank_uuid"),
}

# Oferecimentos e bots: as unicidades de várias colunas sobre a tabela inteira (sem
# predicado), como conjuntos de colunas.
EXPECTED_MULTI_COLUMN_UNIQUES = {
    ("enrollments", frozenset({"person_id", "offering_id"})),
    ("telegram_updates", frozenset({"bot_id", "update_epoch", "update_id"})),
    # Disparo e resposta: `question_refs_key` (pelo resumo do `repo`) e
    # `deliveries_dispatch_enrollment_key` (nunca duas entregas por aluno e disparo).
    ("question_refs", frozenset({"repo_sha256", "commit_sha", "qid"})),
    ("deliveries", frozenset({"dispatch_id", "enrollment_id"})),
    # Relatórios: `dispatch_reports_version_key` (a numeração das versões por
    # disparo) e `team_notices_subject_key` (a idempotência do aviso).
    ("dispatch_reports", frozenset({"dispatch_id", "version"})),
    ("team_notices", frozenset({"offering_id", "kind", "subject_key"})),
    # Banco de questões local: uma linha por nome e versão (o primeiro envio vence).
    ("question_banks", frozenset({"name", "version"})),
}

# Oferecimentos e bots: os índices únicos parciais (com predicado): no máximo um convite
# `pending` por matrícula; um endereço vigente (`active` ou `blocked`) por `(person_id, channel,
# bot_id)` e um por `(bot_id, chat_id)`.
EXPECTED_PARTIAL_UNIQUES = {
    ("invites", frozenset({"enrollment_id"})),
    ("delivery_endpoints", frozenset({"person_id", "channel", "bot_id"})),
    ("delivery_endpoints", frozenset({"bot_id", "chat_id"})),
    # Disparo e resposta: no máximo um desfecho final e uma correção pendente por
    # entrega (`submissions_one_final_idx` e `submissions_one_grading_idx`, os dois sobre
    # `delivery_id`) e uma edição pendente por (entrega, papel) (`outbox_messages_pending_edit_key`).
    ("submissions", frozenset({"delivery_id"})),
    ("outbox_messages", frozenset({"delivery_id", "delivery_role"})),
    # Relatórios: no máximo uma versão do fechamento por disparo
    # (`dispatch_reports_one_closing_idx`).
    ("dispatch_reports", frozenset({"dispatch_id"})),
}

# só estas FKs; `events`, `tasks` e `api_tokens` não têm FK, e nada aponta para
# `events` (nenhuma remoção em cascata no log).
EXPECTED_FOREIGN_KEYS = {
    ("identities", ("person_id",), "people", ("id",)),
    ("outbox_messages", ("recipient_person_id",), "people", ("id",)),
    # Oferecimentos e bots: nada aponta para `events`, e `events`, `tasks` e `api_tokens`
    # continuam sem FK.
    ("bots", ("offering_id",), "offerings", ("id",)),
    ("enrollments", ("person_id",), "people", ("id",)),
    ("enrollments", ("offering_id",), "offerings", ("id",)),
    ("invites", ("enrollment_id",), "enrollments", ("id",)),
    ("delivery_endpoints", ("person_id",), "people", ("id",)),
    ("delivery_endpoints", ("bot_id",), "bots", ("id",)),
    ("telegram_updates", ("bot_id",), "bots", ("id",)),
    ("outbox_messages", ("bot_id",), "bots", ("id",)),
    ("outbox_messages", ("delivery_endpoint_id",), "delivery_endpoints", ("id",)),
    ("outbox_messages", ("reply_to_update_id",), "telegram_updates", ("id",)),
    # Vínculo do aluno (sem FK composta): nada aponta para `events`.
    ("invite_sends", ("enrollment_id",), "enrollments", ("id",)),
    ("invites", ("invite_send_id",), "invite_sends", ("id",)),
    ("invites", ("presented_update_id",), "telegram_updates", ("id",)),
    # Disparo e resposta: as 13 FKs novas, inclusive a circular entre a entrega e a
    # mensagem principal; `question_refs` e `media_blobs` não têm FK, e nada aponta para `events`.
    ("dispatches", ("offering_id",), "offerings", ("id",)),
    ("dispatches", ("question_ref_id",), "question_refs", ("id",)),
    ("deliveries", ("dispatch_id",), "dispatches", ("id",)),
    ("deliveries", ("enrollment_id",), "enrollments", ("id",)),
    ("deliveries", ("delivery_endpoint_id",), "delivery_endpoints", ("id",)),
    ("deliveries", ("principal_message_id",), "outbox_messages", ("id",)),
    ("delivery_contents", ("delivery_id",), "deliveries", ("id",)),
    ("submissions", ("delivery_id",), "deliveries", ("id",)),
    ("submissions", ("telegram_update_id",), "telegram_updates", ("id",)),
    ("free_messages", ("offering_id",), "offerings", ("id",)),
    ("free_messages", ("delivery_endpoint_id",), "delivery_endpoints", ("id",)),
    ("free_messages", ("telegram_update_id",), "telegram_updates", ("id",)),
    ("outbox_messages", ("delivery_id",), "deliveries", ("id",)),
    # Disparo e resposta (mensagens livres): o contato aponta para o oferecimento.
    ("offering_contacts", ("offering_id",), "offerings", ("id",)),
    # Relatórios: as 10 FKs novas; `team_notice_floors` não tem FK, e nada aponta
    # para `events`.
    ("dispatch_reports", ("dispatch_id",), "dispatches", ("id",)),
    ("team_notices", ("offering_id",), "offerings", ("id",)),
    ("team_notices", ("dispatch_id",), "dispatches", ("id",)),
    ("team_notices", ("report_id",), "dispatch_reports", ("id",)),
    ("dispatch_previews", ("dispatch_id",), "dispatches", ("id",)),
    ("dispatch_preview_contents", ("preview_id",), "dispatch_previews", ("id",)),
    ("offering_digests", ("offering_id",), "offerings", ("id",)),
    ("invite_batches", ("offering_id",), "offerings", ("id",)),
    ("outbox_messages", ("notice_id",), "team_notices", ("id",)),
    ("invite_sends", ("batch_id",), "invite_batches", ("id",)),
}


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _packaged_versions() -> list[str]:
    """Versões das migrações empacotadas, em ordem lexical dos arquivos."""
    root = importlib.resources.files("cq_server.migrations")
    names = sorted(entry.name for entry in root.iterdir()
                   if entry.is_file() and MIGRATION_FILE.fullmatch(entry.name))
    return [name.split("_", 1)[0] for name in names]


def _dbname(url: str) -> str:
    return pg_conninfo.conninfo_to_dict(url)["dbname"]


def _client_backends(pg_dsn: str, dbname: str, *, timeout: float = 5.0) -> int:
    """Conexões de cliente ainda abertas em `dbname`, esperando até `timeout` que elas terminem.

    O fim do processo servidor depois do `close()` do cliente é assíncrono; a espera tem prazo.
    """
    deadline = time.monotonic() + timeout
    with psycopg.connect(pg_dsn, autocommit=True) as admin:
        while True:
            count = admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s "
                "AND backend_type = 'client backend' AND pid <> pg_backend_pid()",
                (dbname,),
            ).fetchone()[0]
            if count == 0 or time.monotonic() >= deadline:
                return count
            time.sleep(0.05)


def _migration_rows(url: str) -> list[str]:
    with psycopg.connect(url, autocommit=True) as conn:
        return [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]


_USER_NAMESPACE = "n.nspname NOT IN ('information_schema') AND n.nspname !~ '^pg_'"


def _schema_snapshot(url: str) -> dict[str, list[tuple[Any, ...]]]:
    """Instantâneo do esquema e do conteúdo do banco, para comparar antes e depois."""
    with psycopg.connect(url, autocommit=True) as conn:
        snap: dict[str, list[tuple[Any, ...]]] = {}
        snap["schemas"] = conn.execute(
            f"SELECT n.nspname FROM pg_namespace n WHERE {_USER_NAMESPACE} ORDER BY 1").fetchall()
        snap["relations"] = conn.execute(
            f"SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c "
            f"JOIN pg_namespace n ON n.oid = c.relnamespace WHERE {_USER_NAMESPACE} ORDER BY 1, 2"
        ).fetchall()
        snap["columns"] = conn.execute(
            "SELECT table_schema, table_name, column_name, ordinal_position, udt_name, is_nullable, "
            "column_default, is_identity, identity_generation, identity_start, identity_increment "
            "FROM information_schema.columns "
            "WHERE table_schema <> 'information_schema' AND table_schema !~ '^pg_' "
            "ORDER BY 1, 2, 4"
        ).fetchall()
        snap["constraints"] = conn.execute(
            f"SELECT n.nspname, c.conrelid::regclass::text, c.conname, c.contype::text, "
            f"pg_get_constraintdef(c.oid) FROM pg_constraint c "
            f"JOIN pg_namespace n ON n.oid = c.connamespace WHERE {_USER_NAMESPACE} ORDER BY 1, 2, 3"
        ).fetchall()
        snap["indexes"] = conn.execute(
            "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes "
            "WHERE schemaname <> 'information_schema' AND schemaname !~ '^pg_' ORDER BY 1, 2, 3"
        ).fetchall()
        snap["triggers"] = conn.execute(
            "SELECT tgrelid::regclass::text, tgname, tgenabled::text, pg_get_triggerdef(oid) "
            "FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1, 2"
        ).fetchall()
        snap["functions"] = conn.execute(
            f"SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid), "
            f"md5(coalesce(p.prosrc, '')) FROM pg_proc p "
            f"JOIN pg_namespace n ON n.oid = p.pronamespace WHERE {_USER_NAMESPACE} ORDER BY 1, 2, 3"
        ).fetchall()
        snap["sequences"] = conn.execute(
            "SELECT schemaname, sequencename, data_type::text, start_value, increment_by, "
            "cache_size, last_value FROM pg_sequences ORDER BY 1, 2"
        ).fetchall()
        snap["extensions"] = conn.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY 1").fetchall()
        snap["schema_migrations"] = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version").fetchall()
        tables = [row[0] for row in conn.execute(
            "SELECT format('%I.%I', table_schema, table_name) FROM information_schema.tables "
            "WHERE table_type = 'BASE TABLE' AND table_schema <> 'information_schema' "
            "AND table_schema !~ '^pg_' ORDER BY 1").fetchall()]
        snap["row_counts"] = [
            (table, conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) for table in tables]
    return snap


def _event_triggers(url: str) -> list[tuple[str, int, str, str]]:
    """(nome, tgtype, tgenabled, código da função) dos gatilhos não internos de `events`."""
    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(
            "SELECT t.tgname, t.tgtype::int, t.tgenabled::text, p.prosrc FROM pg_trigger t "
            "JOIN pg_proc p ON p.oid = t.tgfoid "
            "WHERE t.tgrelid = 'public.events'::regclass AND NOT t.tgisinternal ORDER BY t.tgname"
        ).fetchall()


@pytest.fixture
def migrated_url(empty_db_url: str) -> str:
    """Banco vazio que acabou de receber `apply_migrations`."""
    apply_migrations(empty_db_url)
    return empty_db_url


# ---------------------------------------------------------------------------------------------
# Versões
# ---------------------------------------------------------------------------------------------


def test_current_version_is_none_on_empty_database(pg_dsn: str, empty_db_url: str) -> None:
    """banco vazio (sem `schema_migrations`) → `current_version` é `None`, sem deixar conexão."""
    assert current_version(empty_db_url) is None
    assert _client_backends(pg_dsn, _dbname(empty_db_url)) == 0, (
        "current_version deixou conexão aberta ao retornar")


def test_apply_migrations_returns_packaged_versions_in_order(pg_dsn: str, empty_db_url: str) -> None:
    """`apply_migrations` num banco vazio devolve as versões empacotadas, em ordem
    lexical, a última é `EXPECTED_VERSION`, e depois `current_version == EXPECTED_VERSION`."""
    applied = apply_migrations(empty_db_url)
    assert _client_backends(pg_dsn, _dbname(empty_db_url)) == 0, (
        "apply_migrations deixou conexão aberta ao retornar")

    expected = _packaged_versions()
    assert expected, "nenhum arquivo NNNN_<slug>.sql em cq_server.migrations"
    assert len(set(expected)) == len(expected), f"versões repetidas entre os arquivos: {expected}"
    assert re.fullmatch(r"\d{4}", EXPECTED_VERSION), EXPECTED_VERSION
    assert applied == expected
    assert applied == sorted(applied)
    assert applied[-1] == EXPECTED_VERSION
    assert _migration_rows(empty_db_url) == expected

    assert current_version(empty_db_url) == EXPECTED_VERSION
    assert _client_backends(pg_dsn, _dbname(empty_db_url)) == 0, (
        "current_version deixou conexão aberta ao retornar")


def test_current_version_is_highest_applied_version(migrated_url: str) -> None:
    """`current_version` é a maior versão aplicada: uma versão menor gravada depois não
    muda o resultado; uma maior passa a ser a atual."""
    assert current_version(migrated_url) == EXPECTED_VERSION
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES ('0000', now())")
    assert current_version(migrated_url) == EXPECTED_VERSION
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES ('9999', now())")
    assert current_version(migrated_url) == "9999"


# ---------------------------------------------------------------------------------------------
# Esquema produzido
# ---------------------------------------------------------------------------------------------


def test_schema_has_exactly_the_expected_tables_and_columns(empty_db_url: str) -> None:
    """Horários em `timestamptz`; esquema: as 7 tabelas da API base e, com
    oferecimentos e bots, as 6 novas, cada uma com o conjunto exato de colunas, tipos,
    nulidade e colunas identity; `outbox_messages` com as 3 colunas novas e `recipient_person_id`
    anulável; `attempts` com padrão 0 e `bots.update_epoch` com padrão 0."""
    apply_migrations(empty_db_url)
    with psycopg.connect(empty_db_url, autocommit=True) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchall()}
        rows = conn.execute(
            "SELECT table_name, column_name, udt_name, is_nullable, is_identity, column_default "
            "FROM information_schema.columns WHERE table_schema = 'public'").fetchall()

    assert tables == set(EXPECTED_COLUMNS)

    actual: dict[str, dict[str, tuple[str, bool]]] = {}
    identity: set[tuple[str, str]] = set()
    defaults: dict[tuple[str, str], str | None] = {}
    for table, column, udt, nullable, is_identity, default in rows:
        actual.setdefault(table, {})[column] = (udt, nullable == "YES")
        if is_identity == "YES":
            identity.add((table, column))
        defaults[(table, column)] = default
    assert actual == EXPECTED_COLUMNS
    assert identity == IDENTITY_COLUMNS
    assert defaults[("tasks", "attempts")] == "0"
    assert defaults[("outbox_messages", "attempts")] == "0"
    # Oferecimentos e bots: a época de deduplicação começa em 0.
    assert defaults[("bots", "update_epoch")] == "0"
    # Disparo e resposta: as mensagens livres desligadas por padrão.
    assert defaults[("offerings", "free_messages")] == "false"
    # Relatórios: o aviso nasce sem destinatários e com a fotografia vazia; a linha
    # única dos pisos tem `id = 1`.
    assert defaults[("team_notices", "recipient_count")] == "0"
    assert defaults[("team_notices", "data")] == "'{}'::jsonb"
    assert defaults[("team_notice_floors", "id")] == "1"


def test_schema_keys_and_foreign_keys(migrated_url: str) -> None:
    """chaves primárias exatas, unicidades declaradas e
    exatamente as duas FKs para `people` (nenhuma para `events`). Com oferecimentos e
    bots, as chaves primárias, as unicidades de uma coluna e o conjunto exato de FKs passam a incluir
    as das 6 tabelas novas e as 3 de `outbox_messages` (ainda nenhuma para `events`)."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        indexes = conn.execute(
            "SELECT c.relname::text, i.indisprimary, "
            "ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "      WHERE k.ord <= i.indnkeyatts ORDER BY k.ord) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND i.indisunique").fetchall()
        foreign = conn.execute(
            "SELECT c.conrelid::regclass::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum ORDER BY k.ord), "
            "c.confrelid::regclass::text, "
            "ARRAY(SELECT a.attname::text FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum ORDER BY k.ord) "
            "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
            "WHERE c.contype = 'f' AND n.nspname = 'public'").fetchall()

    primary = {table: tuple(cols) for table, is_primary, cols in indexes if is_primary}
    assert primary == EXPECTED_PRIMARY_KEYS
    unique_single = {(table, cols[0]) for table, is_primary, cols in indexes
                     if not is_primary and len(cols) == 1}
    assert EXPECTED_UNIQUE_COLUMNS <= unique_single, (
        f"faltam unicidades: {sorted(EXPECTED_UNIQUE_COLUMNS - unique_single)}")
    fks = {(src, tuple(src_cols), dst, tuple(dst_cols)) for src, src_cols, dst, dst_cols in foreign}
    assert fks == EXPECTED_FOREIGN_KEYS


def test_schema_multi_column_and_partial_unique_indexes(migrated_url: str) -> None:
    """Deduplicação por bot, época e número do update; em oferecimentos e bots,
    as unicidades de várias colunas sem predicado são exatamente `enrollments (person_id,
    offering_id)` e `telegram_updates (bot_id, update_epoch, update_id)`; os índices únicos com
    predicado são exatamente o do convite pendente por matrícula e os dois do endereço vigente. O
    efeito de cada predicado é conferido em `test_migration_0002.py`."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT c.relname::text, i.indpred IS NOT NULL, "
            "ARRAY(SELECT a.attname::text FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            "      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "      WHERE k.ord <= i.indnkeyatts ORDER BY k.ord), "
            "i.indnkeyatts "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND i.indisunique AND NOT i.indisprimary").fetchall()

    # Toda coluna-chave de um índice único é uma coluna da tabela (nenhuma expressão).
    assert all(len(cols) == nkeys for _, _, cols, nkeys in rows), rows
    multi = {(table, frozenset(cols)) for table, partial, cols, _ in rows
             if not partial and len(cols) > 1}
    assert multi == EXPECTED_MULTI_COLUMN_UNIQUES
    partial_uniques = {(table, frozenset(cols)) for table, partial, cols, _ in rows if partial}
    assert partial_uniques == EXPECTED_PARTIAL_UNIQUES


# ---------------------------------------------------------------------------------------------
# Idempotência e concorrência
# ---------------------------------------------------------------------------------------------


def test_second_apply_returns_empty_and_changes_nothing(empty_db_url: str) -> None:
    """Aplicá-las de novo não muda nada: a segunda chamada devolve `[]` e o
    instantâneo do esquema (inclusive `schema_migrations` e as contagens de linhas) fica idêntico."""
    first = apply_migrations(empty_db_url)
    before = _schema_snapshot(empty_db_url)
    second = apply_migrations(empty_db_url)
    after = _schema_snapshot(empty_db_url)

    assert first == _packaged_versions() and first
    assert [version for version, _ in before["schema_migrations"]] == first
    assert second == []
    changed = sorted(key for key in before if before[key] != after[key])
    assert not changed, f"a segunda aplicação mudou: {changed}"
    assert after == before
    assert current_version(empty_db_url) == EXPECTED_VERSION


# A sobreposição das duas threads é probabilística; cada rodada usa um banco vazio próprio, e as
# rodadas repetidas tornam improvável que um executor sem lock passe por sorte.
@pytest.mark.parametrize("attempt", range(5))
def test_concurrent_apply_one_thread_applies_and_the_other_gets_empty(
        empty_db_url: str, attempt: int) -> None:
    """Lock consultivo de sessão: duas threads ao mesmo tempo → uma recebe a lista,
    a outra `[]`, nenhuma com erro, e cada versão fica gravada uma vez."""
    expected = _packaged_versions()
    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]
    errors: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            results[index] = apply_migrations(empty_db_url)
        except BaseException as exc:  # noqa: BLE001 - a exceção volta para a thread do teste
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads), "apply_migrations não terminou em 60 s"
    if errors:
        raise errors[0]

    assert expected, "nenhum arquivo NNNN_<slug>.sql em cq_server.migrations"
    assert sorted(results, key=len) == [[], expected]
    assert _migration_rows(empty_db_url) == expected
    assert current_version(empty_db_url) == EXPECTED_VERSION


# ---------------------------------------------------------------------------------------------
# Mecanismos do log de eventos
# ---------------------------------------------------------------------------------------------


def test_events_seq_sequence_has_cache_1(migrated_url: str) -> None:
    """a sequência de `events.seq` tem `CACHE 1`."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        sequence = conn.execute("SELECT pg_get_serial_sequence('public.events', 'seq')").fetchone()[0]
        assert sequence is not None, "events.seq não é alimentada por uma sequência"
        rows = conn.execute(
            "SELECT s.cache_size FROM pg_sequences s "
            "WHERE format('%%I.%%I', s.schemaname, s.sequencename)::regclass = %s::regclass",
            (sequence,),
        ).fetchall()
    assert rows == [(1,)]


def test_events_update_delete_truncate_triggers_are_enabled_always(migrated_url: str) -> None:
    """há gatilhos não internos de UPDATE, DELETE e TRUNCATE em
    `events` (por `tgtype`), e todos os que cobrem essas operações têm `tgenabled = 'A'`."""
    triggers = _event_triggers(migrated_url)
    for bit, label in ((TG_UPDATE, "UPDATE"), (TG_DELETE, "DELETE"), (TG_TRUNCATE, "TRUNCATE")):
        covering = [(name, enabled) for name, tgtype, enabled, _ in triggers if tgtype & bit]
        assert covering, f"nenhum gatilho de {label} em events"
        assert all(enabled == "A" for _, enabled in covering), (
            f"gatilhos de {label} sem ENABLE ALWAYS: {covering}")


def test_events_has_statement_level_insert_lock_trigger(migrated_url: str) -> None:
    """existe um gatilho `BEFORE INSERT … FOR EACH STATEMENT` em
    `events`, habilitado, cuja função toma `pg_advisory_xact_lock`."""
    triggers = _event_triggers(migrated_url)
    statement_inserts = [(name, enabled, source) for name, tgtype, enabled, source in triggers
                         if tgtype & TG_INSERT and tgtype & TG_BEFORE and not tgtype & TG_ROW]
    assert statement_inserts, (
        f"nenhum gatilho BEFORE INSERT por comando em events: {[t[:3] for t in triggers]}")
    assert any(enabled in ("O", "A") and ADVISORY_XACT_LOCK.search(source or "")
               for _, enabled, source in statement_inserts), (
        "o gatilho de INSERT por comando não está habilitado ou não toma pg_advisory_xact_lock: "
        f"{[(name, enabled) for name, enabled, _ in statement_inserts]}")
