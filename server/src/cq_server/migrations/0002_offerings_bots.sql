-- 0002_offerings_bots: oferecimentos, bots, matrículas da equipe, convites, endereços de entrega e
-- updates recebidos do Telegram, mais a fila de saída estendida (resposta a quem não está vinculado)
-- e os novos tipos de evento.
--
-- Aplicada como a 0001: o executor (cq_server.migrate) roda este arquivo numa transação, com
-- lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou fecha
-- transação.
--
-- Convenções da 0001: horários em timestamptz, gravados pela aplicação a partir do relógio injetável
-- (Clock); nenhuma ordenação por horário (a ordem é sempre a de id); chaves bigint internas.
--
-- Nenhum CHECK que leve identidade ao DETAIL de um erro: a violação de
-- um CHECK mostra a linha inteira no DETAIL. Por isso `delivery_endpoints` (chat_id) e
-- `telegram_updates` (chat_id, sender_id, texto literal) não têm CHECK: as regras delas são da
-- aplicação, antes do INSERT/UPDATE, como as de `identities` na 0001. Os índices únicos dessas tabelas
-- são conferidos pela aplicação antes da gravação, como a unicidade do RA na 0001.

-- Oferecimento -----------------------------------------------------------------------------------
-- `course` e `term` são texto livre gravado já redigido, que pode passar dos limites da entrada:
-- sem CHECK de tamanho (como api_tokens.label). O fuso é validado no núcleo
-- (cq_server.timezones); o banco só confere o tamanho e nunca o usa.

CREATE TABLE offerings (
    id                      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offering_id             uuid        NOT NULL UNIQUE,
    code                    text        NOT NULL UNIQUE,
    course                  text        NOT NULL,
    term                    text        NOT NULL,
    timezone                text        NOT NULL,
    response_window_minutes integer     NOT NULL,
    state                   text        NOT NULL,
    created_at              timestamptz NOT NULL,
    activated_at            timestamptz,
    closed_at               timestamptz,
    archived_at             timestamptz,
    archive_code_sha256     text,
    archive_code_expires_at timestamptz,
    CONSTRAINT offerings_code_check CHECK (code ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
    CONSTRAINT offerings_timezone_check CHECK (char_length(timezone) BETWEEN 1 AND 64),
    CONSTRAINT offerings_response_window_check CHECK (response_window_minutes BETWEEN 5 AND 43200),
    CONSTRAINT offerings_state_check CHECK (state IN ('draft', 'active', 'closed', 'archived')),
    -- Estados e horários coerentes: `draft` sem nenhum dos três; `active` só com `activated_at`;
    -- `closed` com `activated_at` e `closed_at`; `archived` com `archived_at` e, se veio de
    -- `closed`, com `activated_at` e `closed_at` (de `draft`, sem os dois).
    CONSTRAINT offerings_state_times_check CHECK (
        (state = 'draft'
            AND activated_at IS NULL AND closed_at IS NULL AND archived_at IS NULL)
        OR (state = 'active'
            AND activated_at IS NOT NULL AND closed_at IS NULL AND archived_at IS NULL)
        OR (state = 'closed'
            AND activated_at IS NOT NULL AND closed_at IS NOT NULL AND archived_at IS NULL)
        OR (state = 'archived'
            AND archived_at IS NOT NULL AND (activated_at IS NULL) = (closed_at IS NULL))
    ),
    -- Código de confirmação do arquivamento: resumo e validade juntos, e só em `draft` e `closed`.
    CONSTRAINT offerings_archive_code_check CHECK (
        (archive_code_sha256 IS NULL) = (archive_code_expires_at IS NULL)
        AND (archive_code_sha256 IS NULL OR state IN ('draft', 'closed'))
    )
);

-- Bot do oferecimento ----------------------------------------------------------------------------
-- Um bot por oferecimento, e um bot do Telegram pertence a um único oferecimento, para sempre
-- (o vínculo não muda). O token fica só cifrado, no formato v1: 0x01,
-- key_id (4), nonce (12), texto cifrado e tag (16), com 33 + len(token) bytes (token de 1 a 80).

CREATE TABLE bots (
    id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bot_uuid          uuid        NOT NULL UNIQUE,
    offering_id       bigint      NOT NULL UNIQUE REFERENCES offerings (id),
    telegram_bot_id   bigint      NOT NULL UNIQUE,
    username          text        NOT NULL,
    token_ciphertext  bytea       NOT NULL,
    token_version     integer     NOT NULL,
    state             text        NOT NULL,
    access_problem    text,
    profile_state     text        NOT NULL,
    lease_owner       text,
    lease_until       timestamptz,
    last_poll_ok_at   timestamptz,
    last_problem      text,
    last_problem_at   timestamptz,
    update_epoch      integer     NOT NULL DEFAULT 0,
    last_update_at    timestamptz,
    registered_at     timestamptz NOT NULL,
    token_replaced_at timestamptz,
    CONSTRAINT bots_telegram_bot_id_check CHECK (telegram_bot_id > 0),
    -- O CASE evita que `get_byte` falhe (erro que não é de CHECK) num blob vazio: a ordem de
    -- avaliação de um AND não é garantida.
    CONSTRAINT bots_token_ciphertext_check CHECK (
        CASE WHEN octet_length(token_ciphertext) BETWEEN 34 AND 113
             THEN get_byte(token_ciphertext, 0) = 1
             ELSE false
        END
    ),
    CONSTRAINT bots_token_version_check CHECK (token_version >= 1),
    CONSTRAINT bots_state_check CHECK (state IN ('registered', 'no_access')),
    CONSTRAINT bots_access_problem_check CHECK (
        (state = 'registered' AND access_problem IS NULL)
        OR (state = 'no_access' AND access_problem IN ('token_rejected', 'token_unreadable'))
    ),
    CONSTRAINT bots_profile_state_check CHECK (profile_state IN ('pending', 'configured', 'failed')),
    -- Posse: dono e fim juntos, e nenhuma posse sem acesso.
    CONSTRAINT bots_lease_check CHECK (
        (lease_owner IS NULL) = (lease_until IS NULL)
        AND (lease_owner IS NULL OR state = 'registered')
    ),
    -- Último problema transitório da consulta.
    CONSTRAINT bots_last_problem_check CHECK (
        (last_problem IS NULL) = (last_problem_at IS NULL)
        AND (last_problem IS NULL OR last_problem IN
             ('telegram_unavailable', 'rate_limited', 'concurrent_consumer', 'webhook_active'))
    ),
    CONSTRAINT bots_update_epoch_check CHECK (update_epoch >= 0)
);

-- Matrícula --------------------------------------------------------------------------------------
-- Esta migração só cria `team`; a matrícula `student` vem na 0003.

CREATE TABLE enrollments (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id    bigint      NOT NULL REFERENCES people (id),
    offering_id  bigint      NOT NULL REFERENCES offerings (id),
    role         text        NOT NULL,
    state        text        NOT NULL,
    created_at   timestamptz NOT NULL,
    activated_at timestamptz,
    CONSTRAINT enrollments_person_offering_key UNIQUE (person_id, offering_id),
    CONSTRAINT enrollments_role_check CHECK (role IN ('team', 'student')),
    CONSTRAINT enrollments_state_check CHECK (
        (state = 'pending' AND activated_at IS NULL)
        OR (state = 'active' AND activated_at IS NOT NULL)
    )
);

CREATE INDEX enrollments_offering_idx ON enrollments (offering_id);

-- Convite ----------------------------------------------------------------------------------------
-- Só o SHA-256 do código é guardado; "vencido" é calculado pela validade, no processamento.

CREATE TABLE invites (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enrollment_id bigint      NOT NULL REFERENCES enrollments (id),
    code_sha256   text        NOT NULL UNIQUE,
    state         text        NOT NULL,
    expires_at    timestamptz NOT NULL,
    created_at    timestamptz NOT NULL,
    used_at       timestamptz,
    revoked_at    timestamptz,
    CONSTRAINT invites_state_check CHECK (
        (state = 'pending' AND used_at IS NULL AND revoked_at IS NULL)
        OR (state = 'used' AND used_at IS NOT NULL AND revoked_at IS NULL)
        OR (state = 'revoked' AND used_at IS NULL AND revoked_at IS NOT NULL)
    )
);

-- No máximo um convite pendente por matrícula.
CREATE UNIQUE INDEX invites_one_pending_per_enrollment_idx ON invites (enrollment_id)
    WHERE state = 'pending';

-- Endereço de entrega ----------------------------------------------------------------------------
-- `chat_id` é identidade, de uso interno do worker: nunca sai, e a tabela não tem
-- CHECK (ver o cabeçalho). Valores: `channel` = 'telegram'; `state` em 'active', 'inactive' e
-- 'blocked', conferidos pela aplicação.

CREATE TABLE delivery_endpoints (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_id  bigint      NOT NULL REFERENCES people (id),
    bot_id     bigint      NOT NULL REFERENCES bots (id),
    channel    text        NOT NULL,
    chat_id    bigint      NOT NULL,
    state      text        NOT NULL,
    created_at timestamptz NOT NULL,
    changed_at timestamptz NOT NULL
);

-- Um endereço vigente (`active` ou `blocked`) por pessoa, canal e bot, e um por chat em cada bot.
CREATE UNIQUE INDEX delivery_endpoints_current_person_idx
    ON delivery_endpoints (person_id, channel, bot_id) WHERE state IN ('active', 'blocked');

CREATE UNIQUE INDEX delivery_endpoints_current_chat_idx
    ON delivery_endpoints (bot_id, chat_id) WHERE state IN ('active', 'blocked');

-- Update recebido --------------------------------------------------------------------------------
-- Minimizado na borda (uma lista de inclusão); `chat_id`, `sender_id` e `text` são
-- identidade de uso interno, e a tabela não tem CHECK (ver o cabeçalho). Valores: `kind` em
-- 'message', 'callback_query', 'my_chat_member' e 'other'; `state` em 'pending' e 'processed', com
-- `outcome` e `processed_at` preenchidos se e só se 'processed'. Deduplicação por bot, época e
-- número do update.

CREATE TABLE telegram_updates (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    bot_id       bigint      NOT NULL REFERENCES bots (id),
    update_epoch integer     NOT NULL,
    update_id    bigint      NOT NULL,
    kind         text        NOT NULL,
    chat_id      bigint,
    chat_type    text,
    sender_id    bigint,
    text         text,
    command      text,
    details      jsonb       NOT NULL,
    received_at  timestamptz NOT NULL,
    state        text        NOT NULL,
    outcome      text,
    processed_at timestamptz,
    CONSTRAINT telegram_updates_bot_epoch_update_key UNIQUE (bot_id, update_epoch, update_id)
);

-- Fila de saída, estendida -----------------------------------------------------------------------
-- A resposta a quem não está vinculado não tem pessoa: `recipient_person_id` passa a aceitar nulo,
-- com o CHECK de destino único. As linhas da 0001 continuam válidas sem mudança (colunas novas nulas).

ALTER TABLE outbox_messages
    ALTER COLUMN recipient_person_id DROP NOT NULL,
    ADD COLUMN bot_id bigint REFERENCES bots (id),
    ADD COLUMN delivery_endpoint_id bigint REFERENCES delivery_endpoints (id),
    ADD COLUMN reply_to_update_id bigint REFERENCES telegram_updates (id);

-- (a) mensagem da 0001: pessoa, sem bot, sem endereço, sem update; (b) mensagem a pessoa por bot:
-- pessoa, bot e endereço, sem update; (c) resposta ao chat de um update: bot e update, sem pessoa e
-- sem endereço.
ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_destination_check CHECK (
        (recipient_person_id IS NOT NULL AND bot_id IS NULL
            AND delivery_endpoint_id IS NULL AND reply_to_update_id IS NULL)
        OR (recipient_person_id IS NOT NULL AND bot_id IS NOT NULL
            AND delivery_endpoint_id IS NOT NULL AND reply_to_update_id IS NULL)
        OR (recipient_person_id IS NULL AND bot_id IS NOT NULL
            AND delivery_endpoint_id IS NULL AND reply_to_update_id IS NOT NULL)
    );

CREATE INDEX outbox_messages_bot_idx ON outbox_messages (bot_id);

-- Tipos de evento --------------------------------------------------------------------------------
-- O CHECK de tipos é recriado com os 7 da 0001 e os 14 desta (cq_server.events.EventType). O log
-- continua append-only: a checagem das linhas existentes lê a tabela, sem UPDATE.

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
    'outbox.reply_failed'
));
