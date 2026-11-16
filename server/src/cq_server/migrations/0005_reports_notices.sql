-- 0005_reports_notices: os relatórios por disparo e o acompanhamento da equipe — o relatório
-- congelado, os avisos à equipe e os pisos, a amostra e a prévia renderizada, o resumo diário, os
-- lotes de convite, a fila de saída estendida para os avisos e os 3 tipos
-- de evento novos.
--
-- Aplicada como a 0001 a 0004: o executor (cq_server.migrate) roda este arquivo numa transação, com
-- lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou fecha
-- transação. Nenhuma migração anterior muda.
--
-- Convenções da 0001 à 0004: horários em timestamptz, gravados pela aplicação a partir do relógio
-- injetável (Clock); nenhuma ordenação por horário e nenhum CHECK de ordem entre horários;
-- chaves bigint internas, que nunca saem (saem os uuid). As colunas novas das tabelas anteriores são
-- anuláveis: as linhas existentes continuam válidas sem mudança.
--
-- Princípio texto × CHECK (0004_dispatch_response.sql, cabeçalho): a violação de um CHECK mostra a
-- linha inteira no DETAIL do erro. Por isso as tabelas com texto livre, de aluno ou de terceiros
-- (`dispatch_reports`, com o relatório já redigido, e `dispatch_preview_contents`, com a IR da amostra
-- e o gabarito), não têm CHECK, e os invariantes delas ficam em índices únicos, cujo DETAIL mostra só
-- a chave. As tabelas de estado, sem texto, têm CHECK.

-- Relatório congelado ---------------------------------------------------------------------------
-- Uma linha por versão congelada de um disparo (1, 2, … pela aplicação, com o disparo travado); no
-- máximo uma versão do fechamento por disparo. `trigger` ∈ {closing, resend} é regra da
-- aplicação (sem CHECK). O `content` é o objeto `Report`, já redigido, com `frozen` nulo.

CREATE TABLE dispatch_reports (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_uuid uuid        NOT NULL UNIQUE,
    dispatch_id bigint      NOT NULL REFERENCES dispatches (id),
    version     integer     NOT NULL,
    trigger     text        NOT NULL,
    content     jsonb       NOT NULL,
    frozen_at   timestamptz NOT NULL,
    CONSTRAINT dispatch_reports_version_key UNIQUE (dispatch_id, version)
);

CREATE UNIQUE INDEX dispatch_reports_one_closing_idx ON dispatch_reports (dispatch_id)
    WHERE trigger = 'closing';

-- A versão congelada não muda. UPDATE e DELETE (inclusive MERGE) são recusados com mensagem
-- fixa, sem dados, como os gatilhos de `events` (0001_initial.sql); o TRUNCATE não é barrado (os
-- testes limpam o banco). ENABLE ALWAYS: vale também com session_replication_role = replica.
CREATE FUNCTION dispatch_reports_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'dispatch_reports is immutable';
END;
$$;

CREATE TRIGGER dispatch_reports_immutable
    BEFORE UPDATE OR DELETE ON dispatch_reports
    FOR EACH ROW EXECUTE FUNCTION dispatch_reports_reject_change();

ALTER TABLE dispatch_reports ENABLE ALWAYS TRIGGER dispatch_reports_immutable;

-- Aviso à equipe --------------------------------------------------------------------------------
-- Um aviso por (oferecimento, tipo, assunto): a chave de idempotência do aviso. A tabela não guarda
-- texto (o `data` é a fotografia agregada, sem identidade, pseudônimo nem texto livre): admite CHECK.
-- O assunto é canônico e sem identidade: `dispatch:<id>`, `report:<id>`, `episode:<seq>`,
-- `batch:<id>`, `window:<id>` ou `digest:<AAAA-MM-DD>`.

CREATE TABLE team_notices (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offering_id     bigint      NOT NULL REFERENCES offerings (id),
    kind            text        NOT NULL,
    subject_key     text        NOT NULL,
    state           text        NOT NULL,
    recipient_count integer     NOT NULL DEFAULT 0,
    dispatch_id     bigint      REFERENCES dispatches (id),
    report_id       bigint      REFERENCES dispatch_reports (id),
    local_date      date,
    data            jsonb       NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    CONSTRAINT team_notices_subject_key UNIQUE (offering_id, kind, subject_key),
    CONSTRAINT team_notices_kind_check CHECK (kind IN (
        'dispatch_preview', 'dispatch_rejected', 'dispatch_opened', 'dispatch_closed',
        'summary_resent', 'dispatch_cancelled', 'question_failed', 'bot_access_restored',
        'invite_failures', 'daily_digest')),
    CONSTRAINT team_notices_state_check CHECK (state IN (
        'pending', 'queued', 'no_recipient', 'no_activity', 'missed', 'not_sent', 'gave_up')),
    CONSTRAINT team_notices_subject_key_check CHECK (
        subject_key ~ '^(dispatch|report|episode|batch|window|digest):[0-9-]{1,32}$'),
    -- Destinatários só no `queued` (os demais estados finais não enfileiram nada).
    CONSTRAINT team_notices_recipient_count_check CHECK (
        recipient_count >= 0 AND (state = 'queued' OR recipient_count = 0)),
    CONSTRAINT team_notices_finished_check CHECK ((finished_at IS NULL) = (state = 'pending')),
    CONSTRAINT team_notices_local_date_check CHECK ((local_date IS NOT NULL) = (kind = 'daily_digest')),
    CONSTRAINT team_notices_report_check CHECK (
        (report_id IS NOT NULL) = (kind IN ('dispatch_closed', 'summary_resent'))),
    CONSTRAINT team_notices_dispatch_check CHECK (
        dispatch_id IS NOT NULL
        OR kind NOT IN ('dispatch_preview', 'dispatch_rejected', 'dispatch_opened',
                        'dispatch_cancelled', 'question_failed')),
    -- Os estados por tipo: `no_activity` e `missed` só no resumo diário; `not_sent` (fato no piso ou
    -- abaixo) só na volta do acesso e no resumo de fechamento; `gave_up` só na prévia.
    CONSTRAINT team_notices_state_kind_check CHECK (
        (state NOT IN ('no_activity', 'missed') OR kind = 'daily_digest')
        AND (state <> 'not_sent' OR kind IN ('bot_access_restored', 'dispatch_closed'))
        AND (state <> 'gave_up' OR kind = 'dispatch_preview'))
);

-- A regra `notice.send` da varredura: os avisos ainda pendentes.
CREATE INDEX team_notices_pending_idx ON team_notices (id) WHERE state = 'pending';

-- Pisos -----------------------------------------------------------------------------------------
-- Uma única linha, gravada aqui e nunca mais escrita: o maior `events.seq` e o maior
-- `invite_sends.id` no momento da migração (0 no banco vazio). Um fato no piso ou abaixo (anterior
-- ao deploy) não gera aviso.

CREATE TABLE team_notice_floors (
    id                smallint NOT NULL DEFAULT 1 PRIMARY KEY,
    event_floor       bigint   NOT NULL,
    invite_send_floor bigint   NOT NULL,
    CONSTRAINT team_notice_floors_id_check CHECK (id = 1)
);

INSERT INTO team_notice_floors (id, event_floor, invite_send_floor)
SELECT 1,
       COALESCE((SELECT max(seq) FROM events), 0),
       COALESCE((SELECT max(id) FROM invite_sends), 0);

-- Amostra e prévia ------------------------------------------------------------------------------
-- `dispatch_previews` (estado, sem texto, com CHECK): a seed da validação como inteiro (como
-- `deliveries.seed`) e o token dos botões inertes da prévia. `dispatch_preview_contents` (sem CHECK:
-- texto de terceiros, com o gabarito dentro da IR, como `delivery_contents`): a IR da amostra, sem
-- `data_base64`; `media_refs` e `rendered` são preenchidos pela tarefa da prévia.

CREATE TABLE dispatch_previews (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispatch_id  bigint      NOT NULL UNIQUE REFERENCES dispatches (id),
    seed         bigint      NOT NULL,
    button_token text        NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL,
    CONSTRAINT dispatch_previews_seed_check CHECK (seed BETWEEN 0 AND 4294967295),
    CONSTRAINT dispatch_previews_button_token_check CHECK (button_token ~ '^[A-Za-z0-9_-]{22}$')
);

CREATE TABLE dispatch_preview_contents (
    preview_id bigint PRIMARY KEY REFERENCES dispatch_previews (id),
    ir         jsonb  NOT NULL,
    media_refs jsonb,
    rendered   jsonb
);

-- Configuração do resumo diário -----------------------------------------------------------------
-- No máximo uma linha por oferecimento; sem linha = desligado às 20:00. O próximo horário
-- devido existe se e só se o resumo está ligado.

CREATE TABLE offering_digests (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offering_id bigint      NOT NULL UNIQUE REFERENCES offerings (id),
    enabled     boolean     NOT NULL,
    local_time  text        NOT NULL,
    next_due_at timestamptz,
    updated_at  timestamptz NOT NULL,
    CONSTRAINT offering_digests_local_time_check CHECK (
        local_time ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    CONSTRAINT offering_digests_next_due_check CHECK ((next_due_at IS NOT NULL) = enabled)
);

-- Lotes de convite ------------------------------------------------------------------------------
-- Um lote por confirmação de convites (`students.confirm_invites`); o `batch_uuid` identifica o lote
-- no evento `notice.recorded`. Os envios da 0003 ficam sem lote.

CREATE TABLE invite_batches (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    batch_uuid  uuid        NOT NULL UNIQUE,
    offering_id bigint      NOT NULL REFERENCES offerings (id),
    created_at  timestamptz NOT NULL
);

ALTER TABLE invite_sends
    ADD COLUMN batch_id bigint REFERENCES invite_batches (id),
    ADD CONSTRAINT invite_sends_batch_check CHECK (batch_id IS NULL OR origin = 'batch');

CREATE INDEX invite_sends_batch_idx ON invite_sends (batch_id) WHERE batch_id IS NOT NULL;

-- Fila de saída ---------------------------------------------------------------------------------
-- O aviso de origem de cada mensagem à equipe e o dialeto do texto (nulo: o texto das migrações anteriores, sem
-- formatação). O CHECK de destino (0002) não muda: todo aviso é a forma (b), pessoa + bot + endereço.
-- `priority` continua sem CHECK (o código passa a aceitar 2).

ALTER TABLE outbox_messages
    ADD COLUMN notice_id bigint REFERENCES team_notices (id),
    ADD COLUMN text_format text;

-- Conteúdo, recriado com o mesmo nome e três formas: (1) texto (o das migrações anteriores e os avisos de texto); (2)
-- entrega (igual à da 0004, sem aviso nem dialeto); (3) mensagem da prévia (sem entrega e sem
-- texto: o conteúdo vem de `dispatch_preview_contents`).
ALTER TABLE outbox_messages DROP CONSTRAINT outbox_messages_content_check;

ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_content_check CHECK (
        (delivery_id IS NULL
            AND text IS NOT NULL
            AND delivery_role IS NULL AND operation IS NULL AND render_version IS NULL
            AND media_id IS NULL AND field_index IS NULL
            AND (text_format IS NULL OR text_format IN ('plain', 'html', 'rich')))
        OR (delivery_id IS NOT NULL
            AND notice_id IS NULL AND text_format IS NULL
            AND text IS NULL
            AND recipient_person_id IS NOT NULL AND bot_id IS NOT NULL
            AND delivery_endpoint_id IS NOT NULL AND reply_to_update_id IS NULL
            -- `IS NOT NULL` explícito: um `IN` sobre nulo daria nulo, e o CHECK aceitaria a linha.
            AND delivery_role IS NOT NULL AND operation IS NOT NULL
            AND delivery_role IN ('question', 'media', 'field_prompt', 'fallback', 'notice')
            AND operation IN ('send', 'edit_text', 'edit_markup')
            AND (operation = 'send' OR render_version IS NOT NULL)
            AND (render_version IS NULL OR render_version >= 0)
            AND (media_id IS NULL
                 OR (delivery_role = 'media' AND media_id ~ '^m[1-9][0-9]{0,5}$'))
            AND (field_index IS NULL OR (delivery_role = 'field_prompt' AND field_index >= 0)))
        OR (notice_id IS NOT NULL
            AND delivery_id IS NULL
            AND text IS NULL AND text_format IS NULL
            AND recipient_person_id IS NOT NULL AND bot_id IS NOT NULL
            AND delivery_endpoint_id IS NOT NULL AND reply_to_update_id IS NULL
            -- Idem: papel e operação não nulos antes do `IN` e da igualdade.
            AND delivery_role IS NOT NULL AND operation IS NOT NULL
            AND delivery_role IN ('question', 'media', 'field_prompt')
            AND operation = 'send'
            AND render_version IS NULL
            AND (media_id IS NULL
                 OR (delivery_role = 'media' AND media_id ~ '^m[1-9][0-9]{0,5}$'))
            AND (field_index IS NULL OR (delivery_role = 'field_prompt' AND field_index >= 0)))
    );

-- A ordem por endereço entre as mensagens de aviso na posse.
CREATE INDEX outbox_messages_notice_order_idx ON outbox_messages (delivery_endpoint_id, id)
    WHERE notice_id IS NOT NULL AND state IN ('pending', 'leased');

-- Que mensagem de aviso tem este número neste chat (a resposta a uma mensagem da prévia).
CREATE INDEX outbox_messages_notice_ref_idx ON outbox_messages (delivery_endpoint_id, channel_ref)
    WHERE notice_id IS NOT NULL;

-- Tipos de evento e índices ---------------------------------------------------------------------
-- O CHECK de tipos é recriado com os 43 da 0001 à 0004 e os 3 desta (cq_server.events.EventType): 46
-- tipos. O log continua append-only: a checagem das linhas existentes e a criação dos índices leem a
-- tabela, sem UPDATE.

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
    'offering.digest_changed'
));

-- Os episódios de bot sem acesso.
CREATE INDEX events_access_changed_idx ON events (seq) WHERE type = 'bot.access_changed';

-- O piso por fato dos avisos de disparo: o evento da transição de um disparo, pelo `dispatch_id`
-- (uuid) em `data`.
CREATE INDEX events_dispatch_fact_idx ON events ((data->>'dispatch_id'), type, seq)
    WHERE type IN ('dispatch.rejected', 'dispatch.cancelled', 'dispatch.opened', 'dispatch.closed',
                   'delivery.question_failed');
