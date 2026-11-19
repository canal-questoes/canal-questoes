-- 0001_initial: esquema inicial (versões aplicadas, pessoas, identidades, tokens, log de eventos e
-- as filas de tarefas e de saída).
--
-- O executor (cq_server.migrate) roda este arquivo numa transação, com lock_timeout, e grava a
-- versão em schema_migrations na mesma transação. Nada aqui abre ou fecha transação.
--
-- Horários: timestamptz, gravados pela aplicação a partir do relógio injetável (Clock), em UTC.
-- Nenhuma ordenação usa horário: a ordem é sempre a da coluna de sequência (id ou seq).

-- Versões aplicadas ------------------------------------------------------------------------------

CREATE TABLE schema_migrations (
    version    text        PRIMARY KEY,
    applied_at timestamptz NOT NULL
);

-- Pessoa (pseudônimo) ----------------------------------------------------------------------------
-- id é chave substituta interna (nunca derivada de identidade); fora do banco a pessoa só aparece
-- pelo pseudo_id (UUIDv4 aleatório) e pelo apelido.

CREATE TABLE people (
    id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    pseudo_id  uuid        NOT NULL UNIQUE,
    alias      text        NOT NULL UNIQUE,
    created_at timestamptz NOT NULL
);

-- Identidade (área restrita) ---------------------------------------------------------------------
-- Sem CHECK de formato de propósito: a validação é da aplicação, antes do INSERT, e uma violação
-- de CHECK levaria a linha (com a identidade) ao DETAIL do erro.

CREATE TABLE identities (
    person_id        bigint PRIMARY KEY REFERENCES people (id),
    full_name        text   NOT NULL,
    ra               text   UNIQUE,
    email            text,
    email_normalized text   UNIQUE
);

-- Tokens de API -----------------------------------------------------------------------------------
-- Só o SHA-256 do segredo é guardado; o token inicial (CQ_ADMIN_TOKEN) não fica aqui.

CREATE TABLE api_tokens (
    id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_id      uuid        NOT NULL UNIQUE,
    label         text        NOT NULL,
    secret_sha256 text        NOT NULL UNIQUE,
    permissions   text[]      NOT NULL,
    created_at    timestamptz NOT NULL,
    revoked_at    timestamptz
);

-- Log de eventos (append-only) --------------------------------------------------------------------
-- seq com CACHE 1: com o lock consultivo por comando (abaixo), a ordem de seq é a ordem de
-- confirmação.

CREATE TABLE events (
    seq            bigint      GENERATED ALWAYS AS IDENTITY (CACHE 1) PRIMARY KEY,
    type           text        NOT NULL,
    occurred_at    timestamptz NOT NULL,
    actor_token_id text,
    data           jsonb       NOT NULL,
    CONSTRAINT events_type_check CHECK (type IN (
        'person.registered',
        'token.created',
        'token.revoked',
        'identity.revealed',
        'identity.reveal_denied',
        'task.failed',
        'outbox.failed'
    )),
    CONSTRAINT events_data_is_object CHECK (jsonb_typeof(data) = 'object')
);

-- O log é append-only: UPDATE, DELETE (inclusive MERGE) e TRUNCATE (inclusive CASCADE) são
-- recusados, também com session_replication_role = replica (ENABLE ALWAYS).
CREATE FUNCTION events_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'events is append-only';
END;
$$;

CREATE TRIGGER events_append_only
    BEFORE UPDATE OR DELETE ON events
    FOR EACH ROW EXECUTE FUNCTION events_reject_change();

CREATE TRIGGER events_no_truncate
    BEFORE TRUNCATE ON events
    FOR EACH STATEMENT EXECUTE FUNCTION events_reject_change();

ALTER TABLE events ENABLE ALWAYS TRIGGER events_append_only;
ALTER TABLE events ENABLE ALWAYS TRIGGER events_no_truncate;

-- Ordem de confirmação: todo escritor de eventos (inclusive SQL cru) toma o
-- lock consultivo de transação ANTES de o nextval de seq rodar (gatilho por comando, não por
-- linha), e o segura até o COMMIT/ROLLBACK. A chave é fixa e própria do log de eventos.
CREATE FUNCTION events_serialize_writers() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_xact_lock(6371000002);
    RETURN NULL;
END;
$$;

CREATE TRIGGER events_serialize_writers
    BEFORE INSERT ON events
    FOR EACH STATEMENT EXECUTE FUNCTION events_serialize_writers();

ALTER TABLE events ENABLE ALWAYS TRIGGER events_serialize_writers;

-- Fila de tarefas ---------------------------------------------------------------------------------

CREATE TABLE tasks (
    id              bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_type       text        NOT NULL,
    payload         jsonb       NOT NULL,
    idempotency_key text        UNIQUE,
    state           text        NOT NULL,
    attempts        integer     NOT NULL DEFAULT 0,
    available_at    timestamptz NOT NULL,
    lease_owner     text,
    lease_until     timestamptz,
    last_error      text,
    created_at      timestamptz NOT NULL,
    finished_at     timestamptz,
    CONSTRAINT tasks_state_check CHECK (state IN ('pending', 'leased', 'done', 'failed')),
    CONSTRAINT tasks_task_type_check CHECK (task_type ~ '^[a-z][a-z0-9_.]{0,99}$'),
    CONSTRAINT tasks_attempts_check CHECK (attempts >= 0)
);

-- Posse: a busca pega a de menor id entre as pendentes e as em
-- posse (vencida), com FOR UPDATE SKIP LOCKED; o índice parcial cobre só essas.
CREATE INDEX tasks_claimable_idx ON tasks (id) WHERE state IN ('pending', 'leased');

-- Fila de saída -----------------------------------------------------------------------------------

CREATE TABLE outbox_messages (
    id                  bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    recipient_person_id bigint      NOT NULL REFERENCES people (id),
    text                text        NOT NULL,
    idempotency_key     text        UNIQUE,
    state               text        NOT NULL,
    attempts            integer     NOT NULL DEFAULT 0,
    available_at        timestamptz NOT NULL,
    lease_owner         text,
    lease_until         timestamptz,
    channel_ref         text,
    last_error          text,
    created_at          timestamptz NOT NULL,
    sent_at             timestamptz,
    CONSTRAINT outbox_messages_state_check CHECK (state IN ('pending', 'leased', 'sent', 'failed')),
    CONSTRAINT outbox_messages_attempts_check CHECK (attempts >= 0)
);

CREATE INDEX outbox_messages_claimable_idx ON outbox_messages (id)
    WHERE state IN ('pending', 'leased');

CREATE INDEX outbox_messages_recipient_idx ON outbox_messages (recipient_person_id);
