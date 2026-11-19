-- 0006_cli_purge: a operação pela linha de comando e o expurgo de um oferecimento arquivado — a
-- marca e o resultado do expurgo no oferecimento, o resumo do código de confirmação e o impacto
-- preparado, `delivery_endpoints.chat_id` anulável e os 2 tipos de evento
-- novos.
--
-- Aplicada como a 0001 a 0005: o executor (cq_server.migrate) roda este arquivo numa transação, com
-- lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou fecha
-- transação. Nenhuma migração anterior muda; esta só acrescenta.
--
-- Convenções da 0001 à 0005: horários em timestamptz, gravados pela aplicação a partir do relógio
-- injetável (Clock); nenhum CHECK de ordem entre horários. As colunas novas são anuláveis, sem
-- padrão: as linhas existentes continuam válidas sem mudança (nenhum oferecimento expurgado, nenhum código
-- pendente).
--
-- Princípio texto × CHECK (0002_offerings_bots.sql): `offerings` não guarda identidade, então admite
-- CHECK; `delivery_endpoints` guarda identidade e continua sem CHECK. O esquema estrito das contagens
-- (`purge_result`, `purge_prepared_counts`) fica na API, não no banco.

-- `offerings` -------------------------------------------------------------------------------------
-- `purged_at`/`purge_result`: o expurgo confirmado (só em `archived`; as duas juntas). `purge_code_*`
-- e `purge_prepared_counts`: a preparação pendente (as três juntas, só em `archived` ainda não
-- expurgado), como o `archive_code_*` da 0002.

ALTER TABLE offerings
    ADD COLUMN purged_at             timestamptz,
    ADD COLUMN purge_result          jsonb,
    ADD COLUMN purge_code_sha256     text,
    ADD COLUMN purge_code_expires_at timestamptz,
    ADD COLUMN purge_prepared_counts jsonb,
    ADD CONSTRAINT offerings_purge_check CHECK (
        (purged_at IS NULL OR state = 'archived')
        AND ((purged_at IS NULL) = (purge_result IS NULL))
        AND (purge_result IS NULL OR jsonb_typeof(purge_result) = 'object')),
    ADD CONSTRAINT offerings_purge_code_check CHECK (
        (purge_code_sha256 IS NULL) = (purge_code_expires_at IS NULL)
        AND (purge_code_sha256 IS NULL) = (purge_prepared_counts IS NULL)
        AND (purge_code_sha256 IS NULL OR (state = 'archived' AND purged_at IS NULL))
        AND (purge_prepared_counts IS NULL OR jsonb_typeof(purge_prepared_counts) = 'object'));

-- `delivery_endpoints` ----------------------------------------------------------------------------
-- Só o expurgo grava `chat_id` nulo. O índice único parcial
-- `delivery_endpoints_current_chat_idx (bot_id, chat_id) WHERE state IN ('active','blocked')` não
-- muda e aceita vários nulos.

ALTER TABLE delivery_endpoints ALTER COLUMN chat_id DROP NOT NULL;

-- `events` ----------------------------------------------------------------------------------------
-- `events_type_check` recriado com o mesmo nome e os 48 tipos (os 46 da 0005 + `offering.purge_prepared`
-- e `offering.purged`). Os gatilhos append-only não mudam.

ALTER TABLE events DROP CONSTRAINT events_type_check;

ALTER TABLE events ADD CONSTRAINT events_type_check CHECK (type IN (
    'person.registered',
    'token.created',
    'token.revoked',
    'identity.revealed',
    'identity.reveal_denied',
    'task.failed',
    'outbox.failed',
    'offering.created',
    'offering.state_changed',
    'offering.archive_prepared',
    'bot.registered',
    'bot.token_replaced',
    'bot.profile_updated',
    'bot.lease_changed',
    'bot.access_changed',
    'team.link_created',
    'team.link_revoked',
    'team.joined',
    'team.join_refused',
    'endpoint.blocked',
    'outbox.reply_failed',
    'student.list_imported',
    'student.invites_prepared',
    'student.invites_queued',
    'student.invite_sent',
    'student.invite_failed',
    'student.invite_cancelled',
    'student.invite_revoked',
    'student.joined',
    'dispatch.prepared',
    'dispatch.validated',
    'dispatch.rejected',
    'dispatch.confirmed',
    'dispatch.cancelled',
    'dispatch.opened',
    'dispatch.closed',
    'delivery.delivered',
    'delivery.not_delivered',
    'delivery.question_failed',
    'delivery.dont_know',
    'submission.graded',
    'submission.internal_failure',
    'offering.settings_changed',
    'report.frozen',
    'notice.recorded',
    'offering.digest_changed',
    'offering.purge_prepared',
    'offering.purged'
));
