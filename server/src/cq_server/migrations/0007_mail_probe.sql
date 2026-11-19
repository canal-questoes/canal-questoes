-- 0007_mail_probe: teste do servidor de e-mail — o resultado do último teste, gravado pelo
-- worker e lido pelo `doctor` da API.
--
-- Aplicada como a 0001 a 0006: o executor (cq_server.migrate) roda este arquivo numa transação, com
-- lock_timeout, e grava a versão em schema_migrations na mesma transação. Nada aqui abre ou fecha
-- transação. Nenhuma migração anterior muda; esta só acrescenta.
--
-- Uma linha no máximo (`id` fixo em true): o worker grava por INSERT … ON CONFLICT (id) DO UPDATE, com a
-- hora do relógio do worker. Sem host, porta, usuário, remetente nem texto do servidor; não é dado
-- de pessoa, então fica fora do expurgo e admite CHECK.

CREATE TABLE mail_probe (
    id         boolean     PRIMARY KEY DEFAULT true CHECK (id),
    result     text        NOT NULL CHECK (result IN ('ok', 'connection', 'tls', 'auth', 'not_configured')),
    checked_at timestamptz NOT NULL
);
