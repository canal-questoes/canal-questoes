-- 0004_dispatch_response: o disparo de questões e a resposta do aluno — referências de questão,
-- disparos, entregas, conteúdo das entregas, bytes das mídias, submissões, mensagens livres, a fila
-- de saída estendida para as mensagens de entrega e o número da mensagem respondida no
-- update.
--
-- Aplicada como a 0001, a 0002 e a 0003: o executor (cq_server.migrate) roda este arquivo numa
-- transação, com lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui
-- abre ou fecha transação.
--
-- Convenções da 0001 à 0003: horários em timestamptz, gravados pela aplicação a partir do relógio
-- injetável (Clock); nenhuma ordenação por horário e nenhum CHECK de ordem entre horários;
-- chaves bigint internas, que nunca saem (saem os uuid). As colunas novas das tabelas anteriores
-- são anuláveis ou têm padrão: as linhas existentes continuam válidas sem mudança.
--
-- Princípio texto × CHECK: a violação de um CHECK ou de um NOT NULL mostra a
-- linha inteira no DETAIL do erro. Por isso o **estado** fica em tabelas sem texto, com CHECK
-- (`dispatches`, `deliveries`, e `outbox_messages`, cujas mensagens de entrega não guardam texto), e
-- o texto do aluno ou de terceiros não redigido fica em tabelas **sem CHECK** (`question_refs`,
-- `delivery_contents`, `media_blobs`, `submissions`, `free_messages`), com os invariantes
-- críticos em índices únicos (parciais), cujo DETAIL mostra só a chave. As regras dessas tabelas são
-- da aplicação, antes do INSERT/UPDATE. As tabelas com identidade (`people`, `identities`,
-- `delivery_endpoints`, `telegram_updates`) continuam sem CHECK.

-- Referência de questão --------------------------------------------------------------------------
-- Uma linha por (repositório, commit, questão), compartilhada entre oferecimentos. `repo` e `qid`
-- são texto do operador guardado literal (a chave da obtenção e do motor); `title` é texto de
-- terceiros, redigido na gravação. A unicidade é pelo resumo do `repo` (SHA-256 hex do UTF-8): o
-- `UNIQUE (repo, …)` estouraria o B-tree com uma URL longa multibyte. Os campos
-- da inspeção nascem nulos e são preenchidos uma vez, na primeira validação que a conclui.

CREATE TABLE question_refs (
    id                  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ref_uuid            uuid        NOT NULL UNIQUE,
    repo                text        NOT NULL,
    repo_sha256         text        NOT NULL,
    commit_sha          text        NOT NULL,
    qid                 text        NOT NULL,
    supported           boolean,
    show_correct_answer boolean,
    single_variant      boolean,
    partial_credit      boolean,
    title               text,
    answers_names       text[],
    content_hash        text,
    inspected_at        timestamptz,
    created_at          timestamptz NOT NULL,
    CONSTRAINT question_refs_key UNIQUE (repo_sha256, commit_sha, qid)
);

-- Disparo ----------------------------------------------------------------------------------------
-- Sem texto de aluno; a prévia e os motivos do motor são texto de terceiros redigido na gravação, e
-- a tabela recebe CHECK. O disparo validado é `state = 'scheduled' AND confirmed_at
-- IS NULL`, sem estado próprio.

CREATE TABLE dispatches (
    id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispatch_uuid     uuid        NOT NULL UNIQUE,
    offering_id       bigint      NOT NULL REFERENCES offerings (id),
    question_ref_id   bigint      NOT NULL REFERENCES question_refs (id),
    origin            text        NOT NULL,
    immediate         boolean     NOT NULL,
    opens_at          timestamptz,
    closes_at         timestamptz,
    window_minutes    integer,
    state             text        NOT NULL,
    preview           jsonb,
    rejection         text,
    rejection_reasons jsonb,
    cancel_reason     text,
    created_at        timestamptz NOT NULL,
    validated_at      timestamptz,
    confirm_deadline  timestamptz,
    confirmed_at      timestamptz,
    opened_at         timestamptz,
    closed_at         timestamptz,
    rejected_at       timestamptz,
    cancelled_at      timestamptz,
    CONSTRAINT dispatches_origin_check CHECK (origin IN ('cli', 'api', 'mcp', 'integration')),
    CONSTRAINT dispatches_state_check CHECK (
        state IN ('validating', 'rejected', 'scheduled', 'open', 'closed', 'cancelled')),
    CONSTRAINT dispatches_rejection_check CHECK (
        rejection IS NULL
        OR rejection IN ('repository_unreachable', 'repository_refused', 'commit_not_found',
                         'question_not_found', 'question_unsupported', 'question_broken',
                         'engine_unavailable', 'channel_limits_exceeded', 'internal_failure')),
    CONSTRAINT dispatches_cancel_reason_check CHECK (
        cancel_reason IS NULL
        OR cancel_reason IN ('operator', 'expired', 'offering_closed', 'window_missed',
                             'validation_overran')),
    -- A prévia é um objeto; os motivos do motor, uma lista (`[{code, message, phase?}]`).
    CONSTRAINT dispatches_preview_check CHECK (preview IS NULL OR jsonb_typeof(preview) = 'object'),
    CONSTRAINT dispatches_rejection_reasons_check CHECK (
        rejection_reasons IS NULL OR jsonb_typeof(rejection_reasons) = 'array'),
    -- O prazo de confirmação existe se e só se a validação concluiu (agendado: `opens_at`; imediato:
    -- `validated_at + 24 h`).
    CONSTRAINT dispatches_confirm_deadline_check CHECK (
        (confirm_deadline IS NULL) = (validated_at IS NULL)),
    -- Janela: `window_minutes` de 5 a 43 200; o agendado tem os dois horários desde o preparo e
    -- nenhum `window_minutes`; o imediato não confirmado tem exatamente uma fonte de fechamento
    -- (`closes_at` informado ou a janela padrão capturada); com os dois horários, a janela vai de 5
    -- minutos a 30 dias.
    CONSTRAINT dispatches_window_check CHECK (
        (window_minutes IS NULL OR window_minutes BETWEEN 5 AND 43200)
        AND (immediate
             OR (opens_at IS NOT NULL AND closes_at IS NOT NULL AND window_minutes IS NULL))
        AND (NOT immediate OR confirmed_at IS NOT NULL
             OR ((closes_at IS NULL) <> (window_minutes IS NULL)))
        AND (opens_at IS NULL OR closes_at IS NULL
             OR (closes_at - opens_at BETWEEN interval '5 minutes' AND interval '30 days'))
    ),
    -- Estado × horários e desfechos: `validating` sem nenhum horário de transição, prévia ou motivo;
    -- `rejected` com `rejected_at` e a classe; `scheduled` validado, com a prévia, e a marca de
    -- confirmação só com `opens_at`; `open`/`closed` validados, confirmados, abertos e com os dois
    -- horários (`closed_at` se e só se `closed`); `cancelled` com o horário e o motivo, `expired` só
    -- validado e sem confirmação, `window_missed` só confirmado e `validation_overran` só sem
    -- validação.
    CONSTRAINT dispatches_state_outcome_check CHECK (
        (confirmed_at IS NULL OR (validated_at IS NOT NULL AND opens_at IS NOT NULL))
        AND (
            (state = 'validating'
                AND validated_at IS NULL AND confirm_deadline IS NULL AND confirmed_at IS NULL
                AND opened_at IS NULL AND closed_at IS NULL AND rejected_at IS NULL
                AND cancelled_at IS NULL AND preview IS NULL AND rejection IS NULL
                AND rejection_reasons IS NULL AND cancel_reason IS NULL)
            OR (state = 'rejected'
                AND rejected_at IS NOT NULL AND rejection IS NOT NULL
                AND validated_at IS NULL AND confirmed_at IS NULL AND opened_at IS NULL
                AND closed_at IS NULL AND cancelled_at IS NULL AND preview IS NULL
                AND cancel_reason IS NULL)
            OR (state = 'scheduled'
                AND validated_at IS NOT NULL AND confirm_deadline IS NOT NULL AND preview IS NOT NULL
                AND opened_at IS NULL AND closed_at IS NULL AND rejected_at IS NULL
                AND cancelled_at IS NULL AND rejection IS NULL AND rejection_reasons IS NULL
                AND cancel_reason IS NULL)
            OR (state IN ('open', 'closed')
                AND validated_at IS NOT NULL AND preview IS NOT NULL AND confirmed_at IS NOT NULL
                AND opened_at IS NOT NULL AND opens_at IS NOT NULL AND closes_at IS NOT NULL
                AND (closed_at IS NOT NULL) = (state = 'closed')
                AND rejected_at IS NULL AND cancelled_at IS NULL AND rejection IS NULL
                AND rejection_reasons IS NULL AND cancel_reason IS NULL)
            OR (state = 'cancelled'
                AND cancelled_at IS NOT NULL AND cancel_reason IS NOT NULL
                AND opened_at IS NULL AND closed_at IS NULL AND rejected_at IS NULL
                AND rejection IS NULL AND rejection_reasons IS NULL
                AND (preview IS NULL) = (validated_at IS NULL)
                AND (cancel_reason <> 'expired'
                     OR (validated_at IS NOT NULL AND confirmed_at IS NULL))
                AND (cancel_reason <> 'window_missed' OR confirmed_at IS NOT NULL)
                AND (cancel_reason <> 'validation_overran' OR validated_at IS NULL))
        )
    )
);

-- Aviso de questão repetida no oferecimento; disparos vivos (varredura e encerramento).
CREATE INDEX dispatches_offering_ref_idx ON dispatches (offering_id, question_ref_id);

CREATE INDEX dispatches_live_idx ON dispatches (id)
    WHERE state IN ('validating', 'scheduled', 'open');

-- Entrega ----------------------------------------------------------------------------------------
-- Sem texto: recebe CHECK. Uma linha por matrícula do alvo, criada na transação da abertura, com o
-- endereço vigente na abertura. `principal_message_id` aponta a mensagem com os controles (a FK
-- circular com `outbox_messages.delivery_id`, criada abaixo; as duas colunas são anuláveis). A
-- seed do motor é a base 36 canônica de `seed`; o `button_token` são 16 bytes aleatórios em
-- base64url.

CREATE TABLE deliveries (
    id                   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_uuid        uuid        NOT NULL UNIQUE,
    dispatch_id          bigint      NOT NULL REFERENCES dispatches (id),
    enrollment_id        bigint      NOT NULL REFERENCES enrollments (id),
    delivery_endpoint_id bigint      NOT NULL REFERENCES delivery_endpoints (id),
    seed                 bigint      NOT NULL,
    button_token         text        NOT NULL UNIQUE,
    state                text        NOT NULL,
    undelivered_reason   text,
    failure              text,
    current_field        smallint,
    render_version       integer     NOT NULL DEFAULT 0,
    controls_version     integer     NOT NULL DEFAULT 0,
    principal_message_id bigint      REFERENCES outbox_messages (id),
    created_at           timestamptz NOT NULL,
    variant_at           timestamptz,
    sent_at              timestamptz,
    answered_at          timestamptz,
    revealed_at          timestamptz,
    CONSTRAINT deliveries_dispatch_enrollment_key UNIQUE (dispatch_id, enrollment_id),
    CONSTRAINT deliveries_state_check CHECK (
        state IN ('pending', 'delivered', 'not_delivered', 'answered', 'dont_know', 'no_answer',
                  'answered_late', 'question_failed')),
    CONSTRAINT deliveries_undelivered_reason_check CHECK (
        undelivered_reason IS NULL
        OR undelivered_reason IN ('endpoint_blocked', 'channel_refused', 'engine_unavailable',
                                  'bot_no_access', 'channel_unavailable')),
    CONSTRAINT deliveries_failure_check CHECK (
        failure IS NULL
        OR failure IN ('variant_broken', 'variant_unsupported', 'channel_limits_exceeded',
                       'grading_broken', 'engine_rejected')),
    CONSTRAINT deliveries_seed_check CHECK (seed BETWEEN 0 AND 4294967295),
    CONSTRAINT deliveries_button_token_check CHECK (button_token ~ '^[A-Za-z0-9_-]{22}$'),
    CONSTRAINT deliveries_counters_check CHECK (
        render_version >= 0 AND controls_version >= 0
        AND (current_field IS NULL OR current_field >= 0)),
    -- Cada motivo presente se e só se o estado correspondente.
    CONSTRAINT deliveries_reasons_check CHECK (
        (undelivered_reason IS NOT NULL) = (state = 'not_delivered')
        AND (failure IS NOT NULL) = (state = 'question_failed')),
    -- Envio aceito da mensagem com os controles: o horário se e só se a principal, e só depois da
    -- variante; os estados que passam por `delivered` têm a variante e o envio; `pending` e
    -- `not_delivered` não têm envio (um envio aceito depois do fechamento não é transição).
    CONSTRAINT deliveries_send_check CHECK (
        (sent_at IS NULL) = (principal_message_id IS NULL)
        AND (sent_at IS NULL OR variant_at IS NOT NULL)
        AND (state NOT IN ('delivered', 'answered', 'dont_know', 'no_answer', 'answered_late')
             OR (variant_at IS NOT NULL AND sent_at IS NOT NULL))
        AND (state NOT IN ('pending', 'not_delivered') OR sent_at IS NULL)),
    -- O horário da resposta se e só se a entrega foi encerrada pelo aluno.
    CONSTRAINT deliveries_answered_check CHECK (
        (answered_at IS NOT NULL) = (state IN ('answered', 'dont_know', 'answered_late'))),
    -- O campo corrente só com a variante e fora dos estados terminais.
    CONSTRAINT deliveries_current_field_check CHECK (
        current_field IS NULL
        OR (state IN ('pending', 'delivered', 'no_answer') AND variant_at IS NOT NULL)),
    -- A revelação só nas entregas que a recebem; a `no_answer` revelada no fechamento pode chegar a
    -- `question_failed` por uma correção atrasada `broken`.
    CONSTRAINT deliveries_revealed_check CHECK (
        revealed_at IS NULL
        OR state IN ('answered', 'dont_know', 'no_answer', 'answered_late', 'question_failed'))
);

CREATE INDEX deliveries_enrollment_idx ON deliveries (enrollment_id);

CREATE INDEX deliveries_live_idx ON deliveries (dispatch_id)
    WHERE state IN ('pending', 'delivered', 'no_answer');

-- Conteúdo da entrega ----------------------------------------------------------------------------
-- 1:1 com a entrega, criado na transação da variante. Sem CHECK: guarda texto de terceiros
-- (snapshot, IR, renderização) e do aluno (partes em andamento, renderização corrente com o eco).

CREATE TABLE delivery_contents (
    delivery_id    bigint PRIMARY KEY REFERENCES deliveries (id),
    snapshot       jsonb  NOT NULL,
    ir             jsonb  NOT NULL,
    media_refs     jsonb  NOT NULL,
    rendered       jsonb  NOT NULL,
    parts          jsonb  NOT NULL,
    selection      jsonb  NOT NULL,
    current_render jsonb
);

-- Bytes das mídias -------------------------------------------------------------------------------
-- Pelo SHA-256 do conteúdo, gravados por `INSERT … ON CONFLICT DO NOTHING`. Sem CHECK (o DETAIL
-- levaria os bytes).

CREATE TABLE media_blobs (
    sha256       text        PRIMARY KEY,
    content_type text        NOT NULL,
    size_bytes   integer     NOT NULL,
    data         bytea       NOT NULL,
    created_at   timestamptz NOT NULL
);

-- Submissão --------------------------------------------------------------------------------------
-- Uma linha por tentativa que chega a uma entrega; um update gera no máximo uma. Sem CHECK (texto do
-- aluno em quase toda coluna): a validade das combinações é da aplicação, e os invariantes críticos
-- são índices únicos parciais — no máximo um desfecho final (válida ou "Não sei") e no
-- máximo uma correção pendente por entrega.

CREATE TABLE submissions (
    id                 bigint           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    submission_uuid    uuid             NOT NULL UNIQUE,
    delivery_id        bigint           NOT NULL REFERENCES deliveries (id),
    telegram_update_id bigint           NOT NULL UNIQUE REFERENCES telegram_updates (id),
    outcome            text             NOT NULL,
    valid              boolean,
    refusal            text,
    failure_code       text,
    failure_phase      text,
    late               boolean,
    normalized_fields  text[]           NOT NULL DEFAULT '{}',
    raw_input          jsonb            NOT NULL,
    submitted_answers  jsonb,
    format_errors      jsonb,
    feedback           jsonb,
    params             jsonb,
    partial_scores     jsonb,
    correct_answers    jsonb,
    score              double precision,
    part_derived       jsonb,
    engine_mismatch    boolean,
    pl_mismatch        boolean,
    response_ms        bigint,
    delay_notice_sent  boolean          NOT NULL DEFAULT false,
    received_at        timestamptz      NOT NULL,
    created_at         timestamptz      NOT NULL,
    graded_at          timestamptz
);

CREATE INDEX submissions_delivery_idx ON submissions (delivery_id);

CREATE UNIQUE INDEX submissions_one_final_idx ON submissions (delivery_id)
    WHERE (outcome = 'graded' AND valid) OR outcome = 'dont_know';

CREATE UNIQUE INDEX submissions_one_grading_idx ON submissions (delivery_id)
    WHERE outcome = 'grading';

-- Mensagem livre ---------------------------------------------------------------------------------
-- Texto que não é resposta, redigido na gravação e de novo na saída. Sem CHECK. A pessoa é a do
-- endereço; a gravação é idempotente pelo update.

CREATE TABLE free_messages (
    id                   bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offering_id          bigint      NOT NULL REFERENCES offerings (id),
    delivery_endpoint_id bigint      NOT NULL REFERENCES delivery_endpoints (id),
    telegram_update_id   bigint      NOT NULL UNIQUE REFERENCES telegram_updates (id),
    text                 text        NOT NULL,
    replied              boolean     NOT NULL,
    ambiguous            boolean     NOT NULL,
    received_at          timestamptz NOT NULL,
    created_at           timestamptz NOT NULL
);

CREATE INDEX free_messages_offering_idx ON free_messages (offering_id);

CREATE INDEX free_messages_endpoint_idx ON free_messages (delivery_endpoint_id);

-- Fila de saída, estendida -----------------------------------------------------------------------
-- As mensagens de uma entrega não guardam texto: a linha aponta a entrega e o papel, e o conteúdo é
-- lido na posse, da renderização corrente da entrega. `text` passa a aceitar nulo,
-- sob o CHECK de conteúdo. `priority` vale 0 para tudo, menos as respostas fixas às
-- mensagens livres (1). As linhas anteriores continuam válidas sem mudança (colunas
-- novas nulas e `priority = 0`).

ALTER TABLE outbox_messages
    ALTER COLUMN text DROP NOT NULL,
    ADD COLUMN delivery_id bigint REFERENCES deliveries (id),
    ADD COLUMN delivery_role text,
    ADD COLUMN operation text,
    ADD COLUMN render_version integer,
    ADD COLUMN media_id text,
    ADD COLUMN field_index smallint,
    ADD COLUMN priority smallint NOT NULL DEFAULT 0;

-- Estados: + `cancelled` (mensagens de entrega `pending` no fechamento e o `Skip` da posse).
ALTER TABLE outbox_messages DROP CONSTRAINT outbox_messages_state_check;

ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_state_check CHECK (
        state IN ('pending', 'leased', 'sent', 'failed', 'cancelled'));

-- Conteúdo: ou a mensagem com texto das migrações anteriores (as formas (a), (b) e (c) do CHECK de destino, sem
-- nenhuma coluna de entrega), ou a mensagem de entrega sem texto, com papel e operação, na forma (b)
-- (pessoa, bot e endereço); `render_version` (a versão mínima que a edição mostra) em toda edição,
-- `media_id` só no papel `media` e `field_index` só no `field_prompt`. O CHECK de destino da 0002 não
-- muda.
ALTER TABLE outbox_messages
    ADD CONSTRAINT outbox_messages_content_check CHECK (
        (delivery_id IS NULL
            AND text IS NOT NULL
            AND delivery_role IS NULL AND operation IS NULL AND render_version IS NULL
            AND media_id IS NULL AND field_index IS NULL)
        OR (delivery_id IS NOT NULL
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
    );

-- No máximo uma edição pendente por mensagem (entrega, papel): as novas se fundem nela.
CREATE UNIQUE INDEX outbox_messages_pending_edit_key ON outbox_messages (delivery_id, delivery_role)
    WHERE operation IN ('edit_text', 'edit_markup') AND state = 'pending';

-- A ordem por entrega na posse: uma mensagem só sai depois das anteriores da mesma entrega.
CREATE INDEX outbox_messages_delivery_open_idx ON outbox_messages (delivery_id, id)
    WHERE state IN ('pending', 'leased');

-- Que mensagem de entrega tem este número neste chat ("responder" e toque).
CREATE INDEX outbox_messages_endpoint_ref_idx ON outbox_messages (delivery_endpoint_id, channel_ref)
    WHERE delivery_id IS NOT NULL;

-- Update recebido --------------------------------------------------------------------------------
-- O número da mensagem respondida (só em `kind = 'message'`, inteiro maior que 0; nada mais do
-- `reply_to_message` é lido) e o índice dos updates pendentes por bot (barreira do fechamento,
-- arquivamento e varredura). A tabela continua sem CHECK (guarda identidade).

ALTER TABLE telegram_updates
    ADD COLUMN reply_to_message_id bigint;

CREATE INDEX telegram_updates_pending_idx ON telegram_updates (bot_id)
    WHERE state = 'pending';

-- Oferecimento, estendido ------------------------------------------------------------------------
-- A configuração "mensagens livres" do oferecimento, desligada por padrão: as linhas existentes ficam
-- desligadas, e o INSERT anterior sem a coluna continua válido. Nenhum CHECK novo e nenhum texto novo em
-- `offerings` (o e-mail do contato fica em `offering_contacts`).

ALTER TABLE offerings
    ADD COLUMN free_messages boolean NOT NULL DEFAULT false;

-- Contato do professor ---------------------------------------------------------------------------
-- No máximo uma linha por oferecimento; sem linha = sem contato. Sem CHECK (princípio texto × CHECK):
-- o e-mail é identidade e não é redigido, e a violação de um CHECK o levaria ao DETAIL do erro. O
-- e-mail só é lido pela classificação da mensagem livre e pela alteração da configuração; as saídas
-- usam EXISTS (o booleano `contact_email_set`). A aplicação recusa, antes do INSERT, o e-mail que a
-- redação alteraria (identidade de aluno cadastrado).

CREATE TABLE offering_contacts (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    offering_id bigint      NOT NULL UNIQUE REFERENCES offerings (id),
    email       text        NOT NULL,
    updated_at  timestamptz NOT NULL
);

-- Tipos de evento --------------------------------------------------------------------------------
-- O CHECK de tipos é recriado com os 7 da 0001, os 14 da 0002, os 8 da 0003 e os 14 desta
-- (cq_server.events.EventType; o 14º é o `offering.settings_changed`): 43 tipos. O log continua append-only: a checagem das linhas
-- existentes lê a tabela, sem UPDATE.

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
    'offering.settings_changed'
));
