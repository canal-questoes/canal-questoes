-- 0003_student_enrollment: vínculo do aluno ao oferecimento — termo aceito na matrícula, envios de
-- convite por e-mail, apresentação do termo no convite e preparo do envio em lote no
-- oferecimento.
--
-- Aplicada como a 0001 e a 0002: o executor (cq_server.migrate) roda este arquivo numa transação,
-- com lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou
-- fecha transação.
--
-- Convenções da 0001 e da 0002: horários em timestamptz, gravados pela aplicação a partir do relógio
-- injetável (Clock); nenhuma ordenação por horário (a ordem é sempre a de id); chaves bigint
-- internas. As colunas novas das tabelas da 0002 são anuláveis: as linhas anteriores continuam válidas sem
-- mudança.
--
-- Nenhum CHECK em tabela que guarda identidade (`people`, `identities`, `delivery_endpoints`,
-- `telegram_updates`): a violação de um CHECK mostra a linha inteira no DETAIL do erro. As tabelas
-- que recebem CHECK aqui (`enrollments`, `invites`, `invite_sends`, `offerings`, `events`) não
-- guardam identidade.

-- Envio de convite -------------------------------------------------------------------------------
-- Criada antes das colunas de `invites`, que a referenciam. A tarefa do envio é `invite.email`, com
-- a chave `invite.email:<id>`. `attempts` conta os códigos gerados por este envio (não é o
-- `attempts` da tarefa). `error_type` é só o nome do tipo da exceção, nunca o texto; em `failed`, é
-- nulo se e só se `failure = 'not_configured'`. Vários envios `pending` por matrícula são
-- permitidos (a tarefa do mais antigo, ao rodar, o cancela como `superseded`).

CREATE TABLE invite_sends (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    enrollment_id bigint      NOT NULL REFERENCES enrollments (id),
    origin        text        NOT NULL,
    valid_hours   integer     NOT NULL,
    state         text        NOT NULL,
    failure       text,
    cancel_reason text,
    error_type    text,
    attempts      integer     NOT NULL DEFAULT 0,
    created_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    CONSTRAINT invite_sends_origin_check CHECK (
        origin IN ('batch', 'coordination', 'self_service')),
    CONSTRAINT invite_sends_valid_hours_check CHECK (valid_hours BETWEEN 1 AND 720),
    CONSTRAINT invite_sends_state_check CHECK (
        state IN ('pending', 'sent', 'failed', 'cancelled')),
    CONSTRAINT invite_sends_attempts_check CHECK (attempts >= 0),
    CONSTRAINT invite_sends_failure_check CHECK (
        failure IS NULL
        OR failure IN ('recipient_refused', 'auth_refused', 'server_refused', 'not_configured',
                       'attempts_exhausted')),
    CONSTRAINT invite_sends_cancel_reason_check CHECK (
        cancel_reason IS NULL
        OR cancel_reason IN ('offering_inactive', 'already_linked', 'superseded')),
    -- Nome de um tipo (identificador ASCII de até 100 caracteres), nunca o texto da exceção.
    CONSTRAINT invite_sends_error_type_check CHECK (
        error_type IS NULL OR error_type ~ '^[A-Za-z_][A-Za-z0-9_]{0,99}$'),
    -- Desfecho coerente com o estado: `pending` sem desfecho nem fim; `sent` com o fim e ao menos
    -- um código gerado; `failed` com o motivo e o fim, e o tipo do erro nulo se e só se o motivo é
    -- `not_configured`; `cancelled` com o motivo do cancelamento e o fim, sem falha nem tipo.
    CONSTRAINT invite_sends_state_outcome_check CHECK (
        (state = 'pending'
            AND failure IS NULL AND cancel_reason IS NULL AND error_type IS NULL
            AND finished_at IS NULL)
        OR (state = 'sent'
            AND failure IS NULL AND cancel_reason IS NULL AND error_type IS NULL
            AND finished_at IS NOT NULL AND attempts >= 1)
        OR (state = 'failed'
            AND failure IS NOT NULL AND cancel_reason IS NULL AND finished_at IS NOT NULL
            AND (error_type IS NULL) = (failure = 'not_configured'))
        OR (state = 'cancelled'
            AND cancel_reason IS NOT NULL AND failure IS NULL AND error_type IS NULL
            AND finished_at IS NOT NULL)
    )
);

CREATE INDEX invite_sends_enrollment_idx ON invite_sends (enrollment_id);

-- Matrícula: versão e horário do último aceite do termo ------------------------------------------
-- Equipe e aluno pendente sem termo; aluno ativo com a versão (>= 1) e o horário do aceite. Num
-- aceite seguinte (troca de conta), mudam só as duas colunas; o `activated_at` fica o do primeiro.

ALTER TABLE enrollments
    ADD COLUMN terms_version integer,
    ADD COLUMN terms_accepted_at timestamptz;

ALTER TABLE enrollments
    ADD CONSTRAINT enrollments_terms_check CHECK (
        (role = 'team'
            AND terms_version IS NULL AND terms_accepted_at IS NULL)
        OR (role = 'student' AND state = 'pending'
            AND terms_version IS NULL AND terms_accepted_at IS NULL)
        OR (role = 'student' AND state = 'active'
            AND terms_version IS NOT NULL AND terms_version >= 1
            AND terms_accepted_at IS NOT NULL)
    );

-- Convite: o envio que o criou e a última apresentação do termo ----------------------------------
-- A tabela continua sem `chat_id`: o chat da amarração é o do update que apresentou o termo. O
-- envio é sempre da mesma matrícula do convite (regra da aplicação, sem FK composta). Só
-- convites de aluno (com envio) apresentam termo.

ALTER TABLE invites
    ADD COLUMN invite_send_id bigint REFERENCES invite_sends (id),
    ADD COLUMN presented_update_id bigint REFERENCES telegram_updates (id),
    ADD COLUMN presented_terms_version integer;

ALTER TABLE invites
    ADD CONSTRAINT invites_presentation_check CHECK (
        (presented_update_id IS NULL) = (presented_terms_version IS NULL)
        AND (presented_terms_version IS NULL OR presented_terms_version >= 1)
        AND (presented_update_id IS NULL OR invite_send_id IS NOT NULL)
    );

CREATE INDEX invites_invite_send_idx ON invites (invite_send_id);

-- Oferecimento: preparo do envio em lote ---------------------------------------------------------
-- No molde do código de arquivamento: resumo do código de confirmação, validade, horas de validade
-- dos convites do lote e alunos contados no preparo, as quatro juntas e só em `active`. Limpam as
-- quatro: um novo preparo, a confirmação, a importação aceita e o encerramento (no mesmo UPDATE que
-- grava `closed`).

ALTER TABLE offerings
    ADD COLUMN invite_code_sha256 text,
    ADD COLUMN invite_code_expires_at timestamptz,
    ADD COLUMN invite_valid_hours integer,
    ADD COLUMN invite_eligible_count integer;

ALTER TABLE offerings
    ADD CONSTRAINT offerings_invite_code_check CHECK (
        (invite_code_sha256 IS NULL AND invite_code_expires_at IS NULL
            AND invite_valid_hours IS NULL AND invite_eligible_count IS NULL)
        OR (invite_code_sha256 IS NOT NULL AND invite_code_expires_at IS NOT NULL
            AND invite_valid_hours IS NOT NULL AND invite_eligible_count IS NOT NULL
            AND state = 'active'
            AND invite_valid_hours BETWEEN 1 AND 720
            AND invite_eligible_count >= 0)
    );

-- Update recebido --------------------------------------------------------------------------------
-- Índice não único para o candidato do termo e o limite do autosserviço (por bot e chat). A tabela
-- continua sem CHECK (guarda identidade).

CREATE INDEX telegram_updates_bot_chat_idx ON telegram_updates (bot_id, chat_id);

-- Tipos de evento --------------------------------------------------------------------------------
-- O CHECK de tipos é recriado com os 7 da 0001, os 14 da 0002 e os 8 desta
-- (cq_server.events.EventType): 29 tipos. O log continua append-only: a checagem das linhas
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
    'student.joined'
));
