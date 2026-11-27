-- 0008_question_banks: o banco de questões local — a tabela dos pacotes enviados (uma linha por nome e
-- versão, imutável) e o tipo de evento novo.
--
-- Aplicada como a 0001 a 0007: o executor (cq_server.migrate) roda este arquivo numa transação, com
-- lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou fecha
-- transação. Nenhuma migração anterior muda; esta só acrescenta.
--
-- `question_banks` não guarda identidade (o nome é do operador, e o pacote é conteúdo de curso), então
-- admite CHECK. O pacote guardado é o recebido, já validado pela api; o primeiro envio de um nome e uma
-- versão vence, e nenhum código faz UPDATE ou DELETE nesta tabela (sem gatilho: a imutabilidade é
-- conferida por teste). Os horários em timestamptz, gravados pela aplicação a partir do relógio injetável.

-- `question_banks` --------------------------------------------------------------------------------
-- `id`: o cursor da listagem (a ordem do primeiro envio). `bank_uuid`: o identificador externo, reservado
-- (não sai nas respostas). `version`: o SHA-256 hex do manifesto do conteúdo. `git_commit`: o commit de
-- origem, só na origem `git`. `actor_token_id`: o token que enviou (NULL para o token inicial).

CREATE TABLE question_banks (
    id             bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bank_uuid      uuid        NOT NULL UNIQUE,
    name           text        NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
    version        text        NOT NULL CHECK (version ~ '^[0-9a-f]{64}$'),
    source         text        NOT NULL CHECK (source IN ('folder', 'git')),
    git_commit     text        NULL
        CHECK (git_commit IS NULL OR git_commit ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'),
    archive        bytea       NOT NULL,
    archive_bytes  bigint      NOT NULL
        CHECK (archive_bytes = octet_length(archive) AND archive_bytes BETWEEN 1 AND 134217728),
    file_count     integer     NOT NULL CHECK (file_count BETWEEN 1 AND 20000),
    byte_count     bigint      NOT NULL CHECK (byte_count BETWEEN 0 AND 104857600),
    question_count integer     NOT NULL CHECK (question_count BETWEEN 0 AND file_count),
    actor_token_id uuid        NULL,
    created_at     timestamptz NOT NULL,
    CHECK ((source = 'git') = (git_commit IS NOT NULL)),
    UNIQUE (name, version)
);

-- `events` ----------------------------------------------------------------------------------------
-- `events_type_check` recriado com o mesmo nome e os 49 tipos (os 48 da 0006 + `bank.uploaded`). Os
-- gatilhos append-only não mudam.

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
    'offering.purged',
    'bank.uploaded'
));
