"""Erros da API, falha fechada e entrada estrita.

Cobre os erros sem eco, os logs sem identidade nem segredo, o evento gravado na
mesma transação da mudança (se ele não confirma, a mudança também não vale), a revelação que não sai sem a
auditoria confirmada e, em `/v1/*`, a autenticação antes da rota,
mais a rota desconhecida e a entrada estrita.

A falha fechada usa um gatilho de restrição `DEFERRABLE INITIALLY DEFERRED` em `events`, criado pelo
próprio teste, que faz `RAISE EXCEPTION` com o nome e o RA canário no COMMIT de toda transação que
grava um evento do tipo escolhido.

Oferecimentos e bots:
- a falha fechada tem um caso por tipo de evento da área (14), com a Bot API falsa e o worker com
  bots: os tipos gravados pela API (`offering.*`, `bot.registered`, `bot.token_replaced`,
  `bot.access_changed` na recuperação do acesso, `bot.lease_changed` no arquivamento e
  `team.link_*` no `team.link`) dão 500; os tipos gravados pelos tratadores
  (`bot.profile_updated` em `bot.sync_profile`; `team.joined` e `team.join_refused` em
  `telegram.update`) deixam a tarefa `pending` com o `last_error` do tipo da exceção, o registro
  `task.processed` e nenhum `event=exception`; `endpoint.blocked` e `outbox.reply_failed`, gravados
  com o resultado da entrega, deixam a mensagem em posse e o `event=exception` do laço do worker,
  como `outbox.failed`. Em todos, nada de oferecimentos e bots fica gravado sem o evento (em `team.joined`: convite e
  matrícula `pending`, sem endereço, sem boas-vindas e com o update `pending`, tudo numa transação
  única), e sem o gatilho o controle passa;
- `_fingerprint` tem as contagens das 6 tabelas e instantâneos, em ordem de `id`, dos
  estados que oferecimentos e bots mudam (das filas, só os itens deles);
- as matrizes de entrada estrita percorrem também as 10 operações de oferecimentos e bots.

Vínculo do aluno
(lista e convites):
- a falha fechada tem um caso por tipo de evento da área (8), com o SMTP falso, o adaptador real
  (`mail_channel`) e, no `student.joined`, a Bot API falsa: os tipos gravados pela API
  (`student.list_imported`, `student.invites_prepared`, `student.invites_queued`) dão 500;
  na T1 da tarefa `invite.email` (`student.invite_cancelled`, `student.invite_revoked`), a transação
  volta atrás e a tarefa repete sem mudança; na T2 (`student.invite_sent`, `student.invite_failed`),
  a transação volta atrás, a tarefa repete e a T1 seguinte revoga o convite anterior (`replaced`) e
  gera outro código; `student.joined`, gravado pelo tratador `telegram.update`, segue o
  `team.joined` de oferecimentos e bots. O `student.invite_failed` da T1 (`not_configured`) fica na função nomeada
  `test_invite_failed_not_configured_fails_closed`, fora do `FAIL_CLOSED_CASES`;
- `_fingerprint` tem chaves próprias para `invite_sends`, para as colunas do vínculo em `enrollments`, `invites` e
  `offerings` e as tarefas `invite.email` (os casos de oferecimentos e bots que comparam as deles
  com literais não dependem delas);
- as matrizes de entrada estrita percorrem também as 5 operações do vínculo (200 em todas), e
  `students.summary` é a única delas sem corpo.

Disparo e resposta:
- a falha fechada tem um caso por tipo de evento da área (13), com o motor falso estrito, a obtenção
  falsa e a Bot API falsa estendida (`rich_bot_api`), pedidos pelo `request` do teste só nos casos do
  disparo. Os tipos gravados pela API (`dispatch.prepared`, `dispatch.confirmed` e `dispatch.cancelled` pelo
  operador) dão 500 exato sem efeito; os gravados numa tarefa do worker
  (`dispatch.validated`, `dispatch.rejected`, `dispatch.opened`, `dispatch.closed`,
  `delivery.not_delivered` da abertura e do fechamento, `delivery.question_failed`,
  `delivery.dont_know`, `submission.graded`, `submission.internal_failure`) deixam a tarefa `pending`
  com `last_error = "RaiseException"`, o registro `task.processed` e nenhum `event=exception`, com o
  disparo, a entrega e a submissão sem mudança; os gravados na transação de resultado da fila
  (`delivery.delivered` e o `delivery.not_delivered` do resultado do envio), no molde do
  `endpoint.blocked` e do `outbox.reply_failed`: a mensagem fica em posse, a entrega sem mudança, sem
  `outbox.failed`, com `event=exception` no laço do worker. Em todos, sem o gatilho, a tarefa (ou a
  chamada da API) repete e o controle passa, com um único evento. O aluno é vinculado por SQL (pessoa
  pela API, matrícula `student` ativa e endereço vigente), porque o vínculo do aluno já tem os seus
  casos fechados em falha;
- `_fingerprint` tem uma chave por tabela do disparo (`question_refs_rows`, `dispatches_rows`,
  `deliveries_rows`, `submissions_rows`, `free_messages_rows`, `outbox_delivery_rows` e
  `dispatch_tasks_rows`), em consultas e chaves próprias; o literal zerado do primeiro teste
  traz essas chaves, vazias;
- as matrizes de entrada estrita percorrem também as 6 operações do disparo (200 em todas); `dispatches.get`,
  `dispatches.list`, `dispatches.confirm`, `dispatches.cancel` e `messages.list` não têm corpo.
No esqueleto, os casos do disparo falham pela rota ausente (404 no preparo) e os demais casos de
`test_event_commit_failure_fails_closed`, pela comparação com `EventType`; os que usam
`_fingerprint`, pelas tabelas ausentes, e os que chamam `seed_state`, pela semente.

Mensagens livres opcionais por oferecimento e o aviso de falta de contato:
- no fechamento que falha do `delivery.not_delivered`, só o
  `dispatch.closed` de C é conferido como ausente (A e B podem fechar na mesma rodada);
- a falha fechada tem o caso `offering.settings_changed` (o 43º tipo; `PATCH
  /v1/offerings/{ref}/settings` com `{"free_messages": true}`); o corpo do `Offering` do cadastro traz
  `free_messages` e `contact_email_set` (falsos no padrão); as matrizes de entrada estrita percorrem
  também `offerings.update_settings` (pelo `VALID_CALLS`; tem corpo, fora de `BODYLESS_V1_OPS`);
- o `dispatch.validated` de um oferecimento no padrão (mensagens livres desligadas e sem contato)
  traz `no_teacher_contact` por último na lista de avisos.
No esqueleto: `EventType` sem `offering.settings_changed` (a igualdade com `FAIL_CLOSED_CASES` falha),
a rota `PATCH /v1/offerings/{ref}/settings` ausente, o `Offering` sem as duas chaves e a validação sem o
aviso `no_teacher_contact`.

Relatórios e acompanhamento
(os relatórios, os avisos à equipe e o resumo diário):
- a falha fechada tem um caso por tipo de evento da área (3), no molde dos anteriores (gatilho adiado no COMMIT
  do evento, com o nome e o RA canário), com a equipe simulada (`tests/helpers/team.py`: um membro com
  endereço ativo, vinculado pelo caminho real de oferecimentos e bots) e os dublês do disparo (`_dispatch_env`):
  `report.frozen` (i) pelo worker, no congelamento de um disparo fechado sem correção pendente: a tarefa
  `report.freeze` volta a `pending` com `last_error = "RaiseException"`, sem versão, sem aviso
  `dispatch_closed` e sem mensagem dele na fila; no controle, a tarefa repete e congela uma única versão
  `closing`; (ii) pela API, no reenvio (`reports.resend_summary`) desse disparo já congelado: 500 exato, sem
  versão 2 e sem aviso `summary_resent`; no controle, 200 com a versão 2; `notice.recorded`: um aviso de
  texto `dispatch_rejected` registrado `pending` (fase 1, `teamnotices.record`) para um disparo rejeitado
  na validação; a tarefa `notice.send` volta a `pending`, o aviso continua `pending` e nenhuma mensagem com
  `notice_id` vai à fila; no controle, o aviso fica `queued` com uma mensagem por destinatário;
  `offering.digest_changed`: a alteração do resumo diário (`offerings.update_digest`) com mudança → 500
  exato, sem linha em `offering_digests`; no controle, 200 com o `DigestView`. Em todos, um único evento
  exato no controle e os logs sem canário e sem `Traceback`, com `event: exception` só na API;
- `_fingerprint` tem uma chave por tabela dos relatórios (`dispatch_reports_rows`, `team_notices_rows`,
  `dispatch_previews_rows`, `offering_digests_rows`, `invite_batches_rows`, `invite_sends_batch_rows`,
  `outbox_notice_rows` e `report_tasks_rows`), em consultas e chaves próprias; o literal
  zerado do primeiro teste traz essas chaves, vazias;
- as matrizes de entrada estrita percorrem também as 5 operações dos relatórios pelo `VALID_CALLS` (200 em todas);
  `reports.get`, `reports.list_versions`, `reports.resend_summary` e `offerings.get_digest` não têm corpo
  (`offerings.update_digest` tem esquema de pedido).
No esqueleto, todos os casos de `test_event_commit_failure_fails_closed` falham pela comparação com
`EventType`; os que usam `_fingerprint`, pelas tabelas ausentes; e os que chamam `seed_state`, pela
semente.

Operação e expurgo pelo CLI
(o expurgo e o `system.doctor`):
- o corpo exato do `Offering` do cadastro (`_case_offering_created`) traz
  `"purged_at": None`;
- a falha fechada tem um caso por tipo de evento (2), no molde dos anteriores (gatilho adiado no COMMIT do
  evento, com o nome e o RA canário): `offering.purge_prepared` → `offerings.prepare_purge` num oferecimento
  arquivado sem pendência → 500 exato, sem código gravado (as três colunas do código nulas; instantâneo de
  `_fingerprint`); no controle, 200 com o corpo exato, só o resumo do código guardado e o evento exato;
  `offering.purged` → `offerings.purge` com o código válido, no cenário da semente do expurgo
  (`tests/helpers/purge.py`) → 500 exato, **nada** apagado (as seis tabelas do expurgo e `offerings`
  iguais pelo `snapshot_purge_tables`, `purged_at` nulo) e o código continua válido (o resumo guardado); no
  controle, com o mesmo código, 200 com o resultado exato da semente e um único `offering.purged`. Em todos, os
  logs sem canário e sem `Traceback`, com `event: exception` só na API;
- `_fingerprint` sem tabela nem chave própria (as colunas do expurgo em `offerings` são conferidas nos casos do CLI);
- as matrizes de entrada estrita percorrem também as 3 operações desta parte pelo `VALID_CALLS` (200 em todas);
  `system.doctor` e `offerings.prepare_purge` não têm corpo (`offerings.purge` tem esquema de pedido).
No esqueleto, todos os casos de `test_event_commit_failure_fails_closed` falham pela comparação com `EventType`
sem os 2 tipos; os que chamam `seed_state`, pela semente, que prepara os alvos do expurgo pela rota ausente;
e o corpo exato do `Offering`, pela falta de `purged_at`.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from datetime import datetime, timezone
from email.headerregistry import Address
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql as pgsql

from cq_server import people
from cq_server.events import EventType
from cq_server.outbox import Outbox
from cq_server.ports.delivery import Undeliverable
from cq_server.tasks import PermanentTaskError, TaskQueue
from tests.helpers.api import (ADMIN_TOKEN, TEAM_LINK_SHAPE, activate_bot, auth, create_offering,
                               create_team_link, create_token, link_code, register_bot,
                               register_person)
from tests.helpers.api import import_students, prepare_invites, resend_invites, send_invites
from tests.helpers.api import (cancel_dispatch, confirm_dispatch, get_dispatch,
                               prepare_dispatch)
from tests.helpers.api import resend_report_summary  # relatórios
from tests.helpers.bot_api import make_user
from tests.helpers.calls import VALID_CALLS, seed_state
from tests.helpers.canary import ZEFIRO, find_canary, find_secrets
from tests.helpers.canary import STUDENT_A, STUDENT_PREFIXES
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import FakeClock, iso
from tests.helpers.contract import (SCHEMAS, assert_error, assert_matches, assert_no_echo,
                                    load_catalog)
from tests.helpers.logs import Captured, capture_logs
from tests.helpers.secrets import all_secrets, remember_secret
from tests.helpers.secrets import find_invite_codes, invite_code_digest, invite_code_label
from tests.helpers.team import ACTIVE as TEAM_ACTIVE  # a equipe simulada
from tests.helpers.team import create_team_offering, link_team, team_messages

# Senha da `unreachable_db_url` do conftest.
SENTINEL_PASSWORD = "senha-que-nao-pode-vazar"

# Mensagem do `RAISE EXCEPTION` do gatilho da falha fechada: carrega o nome e o RA canário.
FORCED_FAILURE = f"falha simulada para {ZEFIRO.name} RA {ZEFIRO.ra}"
FAIL_TRIGGER = "cq_test_fail_on_commit"

CATALOG = {op["id"]: op for op in load_catalog()["operations"]}
CATALOG_V1_OPS = {op_id for op_id, op in CATALOG.items() if op["path"].startswith("/v1/")}
# Operações `/v1` de `VALID_CALLS`, na ordem do catálogo de chamadas.
V1_OPS = [op_id for op_id in VALID_CALLS if CATALOG[op_id]["path"].startswith("/v1/")]
# Operações `/v1` que não recebem corpo: as que não têm esquema de pedido congelado.
BODYLESS_V1_OPS = [op_id for op_id in V1_OPS
                   if not (SCHEMAS / f"{op_id}.request.schema.json").exists()]
SUCCESS_STATUS = {op_id: 201 if op_id in ("people.register", "tokens.create", "offerings.create",
                                          "team.link") else 200
                  for op_id in V1_OPS}

TASK_TYPE = "teste.falha_fechada"

# Disparo e resposta: as colunas dos instantâneos de disparos e entregas e os
# tipos de tarefa do disparo (escritos aqui, e não importados, para o teste não depender da implementação).
DISPATCH_COLUMNS = ("state", "origin", "immediate", "rejection", "cancel_reason", "validated_at",
                       "confirmed_at", "opens_at", "closes_at", "opened_at", "closed_at", "rejected_at",
                       "cancelled_at")
DELIVERY_COLUMNS = ("state", "undelivered_reason", "failure", "current_field", "render_version",
                       "controls_version", "variant_at", "sent_at", "answered_at", "revealed_at")
DISPATCH_TASK_TYPES = ("dispatch.validate", "dispatch.expire", "dispatch.open", "dispatch.close",
                 "delivery.prepare", "delivery.reveal", "submission.grade", "question.refetch")
DISPATCH_TASK_TYPES_SQL = ", ".join(f"'{task_type}'" for task_type in DISPATCH_TASK_TYPES)
# Os tipos de tarefa dos relatórios, escritos aqui como os do disparo.
REPORT_TASK_TYPES = ("report.freeze", "notice.send", "notice.dispatch_rejected", "notice.dispatch_cancelled",
                 "notice.dispatch_opened", "notice.question_failed", "notice.access_episode",
                 "notice.invite_batch", "notice.invite_window", "preview.send", "digest.daily")
REPORT_TASK_TYPES_SQL = ", ".join(f"'{task_type}'" for task_type in REPORT_TASK_TYPES)
UNDELIVERABLE_REASON = "destinatario recusou o canal"


# --- apoio -------------------------------------------------------------------------------------


def _explode_with_canary() -> uuid.UUID:
    raise RuntimeError(f"{ZEFIRO.name} {ZEFIRO.ra}")


# Linha do `raise` acima: o quadro mais interno do traceback (o campo `where` do log).
EXPLODE_LINE = _explode_with_canary.__code__.co_firstlineno + 1


def _exposed(resp: Any) -> str:
    """Corpo e cabeçalhos da resposta, como texto, para os detectores."""
    headers = "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
    return resp.text + "\n" + headers


def _records(captured: Captured, **expected: Any) -> list[dict]:
    """Registros de log que incluem o subconjunto `expected` (por inclusão)."""
    return [record for record in captured.records if expected.items() <= record.items()]


def _logged(call: Callable[[], Any]) -> tuple[Any, Captured]:
    with capture_logs() as captured:
        resp = call()
    return resp, captured


def _assert_one_request(captured: Captured, **expected: Any) -> None:
    """Exatamente um registro `request` do logger `cq_server.api`, que inclui `expected`."""
    found = _records(captured, logger="cq_server.api", event="request")
    assert len(found) == 1, f"esperado 1 registro request, vieram {len(found)}: {found}"
    assert expected.items() <= found[0].items(), f"{found[0]} não inclui {expected}"


def _assert_clean_logs(captured: Captured, *, exception_logger: str | None = None) -> None:
    """Logs sem canário, sem segredo de token e sem `Traceback`; com `event: exception` se pedido."""
    text = captured.text
    assert find_canary(text) == [], f"identidade canário nos logs: {find_canary(text)}"
    assert find_secrets(text, all_secrets()) == [], "segredo de token nos logs"
    assert "Traceback" not in text, "rastreamento de pilha nos logs"
    if exception_logger is not None:
        assert _records(captured, logger=exception_logger, event="exception"), (
            f"nenhum registro event=exception no logger {exception_logger}")


def _count(sql_conn: psycopg.Connection, table: str) -> int:
    query = pgsql.SQL("SELECT count(*) FROM {}").format(pgsql.Identifier(table))
    return sql_conn.execute(query).fetchone()[0]


def _event_count(sql_conn: psycopg.Connection, event_type: str | None = None) -> int:
    if event_type is None:
        return sql_conn.execute("SELECT count(*) FROM events").fetchone()[0]
    return sql_conn.execute("SELECT count(*) FROM events WHERE type = %s", (event_type,)).fetchone()[0]


def _rows(sql_conn: psycopg.Connection, query: str) -> list[tuple]:
    return [tuple(row) for row in sql_conn.execute(query).fetchall()]


def _fingerprint(sql_conn: psycopg.Connection) -> dict[str, Any]:
    """Contagens que qualquer mudança gravada altera (pessoas, tokens, revogações, eventos).

    Oferecimentos e bots: mais as contagens das 6 tabelas deles e os instantâneos, em ordem de `id`,
    dos estados que as operações e os tratadores deles mudam. Das filas, só os itens deles: as
    tarefas `bot.sync_profile` e `telegram.update` e as mensagens com bot (os outros casos enfileiram
    as suas de propósito, e as contagens delas não entram).

    Vínculo do aluno: mais chaves próprias, sem mudar as consultas nem as tuplas
    das outras chaves (os casos de oferecimentos e bots que as comparam com literais continuam valendo):
    `invite_sends_rows` (cada envio, em ordem de `id`: origem, validade, estado, desfecho, tipo do
    erro, códigos gerados e fim); `enrollments_terms_rows` (as matrículas de aluno: estado, versão do
    termo aceito e horário do aceite; as de equipe não entram); `invites_presentation_rows` (as
    colunas do vínculo em cada convite, com as relações como posições, sem ids internos: a posição do envio
    que o criou em `invite_sends` e a do update que apresentou o termo em `telegram_updates`, nulas nos
    convites de equipe, e a versão apresentada); `offerings_invite_rows` (as 4 colunas do preparo do
    envio de cada oferecimento) e `invite_email_tasks_rows` (o estado de cada tarefa `invite.email`,
    fora do filtro de `tasks_rows`).

    Disparo e resposta: mais chaves próprias, sem mudar as outras:
    `question_refs_rows` (QID, suporte e inspeção de cada referência), `dispatches_rows` (as colunas de
    estado e de horário de cada disparo, `DISPATCH_COLUMNS`), `deliveries_rows` (estado, motivos,
    campo corrente, versões e marcas de cada entrega, `DELIVERY_COLUMNS`), `submissions_rows`
    (desfecho, validade, recusa e correção), `free_messages_rows`, `outbox_delivery_rows` (papel,
    operação e estado das mensagens de entrega, fora do filtro de `outbox_messages_rows`, que só olha o
    bot) e `dispatch_tasks_rows` (tipo e estado das tarefas do disparo, `DISPATCH_TASK_TYPES`).

    Relatórios e acompanhamento: mais chaves próprias, sem mudar as outras:
    `dispatch_reports_rows` (versão, gatilho e horário de cada versão congelada), `team_notices_rows` (tipo,
    estado, destinatários, data local e horários de cada aviso), `dispatch_previews_rows` (cada amostra, com
    o preenchimento da prévia em `dispatch_preview_contents`), `offering_digests_rows` (a configuração do
    resumo diário de cada oferecimento), `invite_batches_rows` (cada lote), `invite_sends_batch_rows` (a
    coluna `batch_id` de cada envio, como presença), `outbox_notice_rows` (papel, dialeto, prioridade e
    estado das mensagens de aviso, as de `notice_id` não nulo) e `report_tasks_rows` (tipo e estado das tarefas
    dos relatórios, `REPORT_TASK_TYPES`).
    """
    return {
        "people": _count(sql_conn, "people"),
        "identities": _count(sql_conn, "identities"),
        "api_tokens": _count(sql_conn, "api_tokens"),
        "revoked_tokens": sql_conn.execute(
            "SELECT count(*) FROM api_tokens WHERE revoked_at IS NOT NULL").fetchone()[0],
        "events": _event_count(sql_conn),
        # Oferecimentos e bots
        "offerings": _count(sql_conn, "offerings"),
        "bots": _count(sql_conn, "bots"),
        "enrollments": _count(sql_conn, "enrollments"),
        "invites": _count(sql_conn, "invites"),
        "delivery_endpoints": _count(sql_conn, "delivery_endpoints"),
        "telegram_updates": _count(sql_conn, "telegram_updates"),
        "offerings_rows": _rows(sql_conn, "SELECT state, activated_at, closed_at, archived_at, "
                                          "archive_code_sha256 FROM offerings ORDER BY id"),
        "bots_rows": _rows(sql_conn, "SELECT state, access_problem, token_version, token_ciphertext, "
                                     "profile_state, lease_owner, lease_until FROM bots ORDER BY id"),
        "enrollments_rows": _rows(sql_conn, "SELECT state FROM enrollments ORDER BY id"),
        "invites_rows": _rows(sql_conn, "SELECT state FROM invites ORDER BY id"),
        "delivery_endpoints_rows": _rows(sql_conn, "SELECT state FROM delivery_endpoints ORDER BY id"),
        "telegram_updates_rows": _rows(sql_conn, "SELECT state, outcome FROM telegram_updates "
                                                 "ORDER BY id"),
        "outbox_messages_rows": _rows(sql_conn, "SELECT state, bot_id FROM outbox_messages "
                                                "WHERE bot_id IS NOT NULL ORDER BY id"),
        "tasks_rows": _rows(sql_conn, "SELECT task_type, state FROM tasks WHERE task_type IN "
                                      "('bot.sync_profile', 'telegram.update') ORDER BY id"),
        # Vínculo do aluno
        "invite_sends_rows": _rows(sql_conn, "SELECT origin, valid_hours, state, failure, cancel_reason, "
                                             "error_type, attempts, finished_at FROM invite_sends "
                                             "ORDER BY id"),
        "enrollments_terms_rows": _rows(sql_conn, "SELECT state, terms_version, terms_accepted_at "
                                                  "FROM enrollments WHERE role = 'student' ORDER BY id"),
        "invites_presentation_rows": _rows(
            sql_conn,
            "SELECT CASE WHEN i.invite_send_id IS NULL THEN NULL ELSE "
            "(SELECT count(*) FROM invite_sends s WHERE s.id <= i.invite_send_id) END, "
            "CASE WHEN i.presented_update_id IS NULL THEN NULL ELSE "
            "(SELECT count(*) FROM telegram_updates u WHERE u.id <= i.presented_update_id) END, "
            "i.presented_terms_version FROM invites i ORDER BY i.id"),
        "offerings_invite_rows": _rows(sql_conn, "SELECT invite_code_sha256, invite_code_expires_at, "
                                                 "invite_valid_hours, invite_eligible_count "
                                                 "FROM offerings ORDER BY id"),
        "invite_email_tasks_rows": _rows(sql_conn, "SELECT state FROM tasks WHERE task_type = "
                                                   "'invite.email' ORDER BY id"),
        # Disparo e resposta
        "question_refs_rows": _rows(sql_conn, "SELECT qid, supported, inspected_at FROM question_refs "
                                              "ORDER BY id"),
        "dispatches_rows": _rows(sql_conn, f"SELECT {', '.join(DISPATCH_COLUMNS)} FROM dispatches "
                                           "ORDER BY id"),
        "deliveries_rows": _rows(sql_conn, f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM deliveries "
                                           "ORDER BY id"),
        "submissions_rows": _rows(sql_conn, "SELECT outcome, valid, refusal, graded_at FROM submissions "
                                            "ORDER BY id"),
        "free_messages_rows": _rows(sql_conn, "SELECT replied, ambiguous FROM free_messages ORDER BY id"),
        "outbox_delivery_rows": _rows(sql_conn, "SELECT delivery_role, operation, state FROM outbox_messages "
                                                "WHERE delivery_id IS NOT NULL ORDER BY id"),
        "dispatch_tasks_rows": _rows(sql_conn, f"SELECT task_type, state FROM tasks WHERE task_type IN "
                                         f"({DISPATCH_TASK_TYPES_SQL}) ORDER BY id"),
        # Relatórios e acompanhamento
        "dispatch_reports_rows": _rows(sql_conn, "SELECT version, trigger, frozen_at FROM dispatch_reports "
                                                 "ORDER BY id"),
        "team_notices_rows": _rows(sql_conn, "SELECT kind, state, recipient_count, local_date, created_at, "
                                             "finished_at FROM team_notices ORDER BY id"),
        "dispatch_previews_rows": _rows(sql_conn, "SELECT p.created_at, c.media_refs IS NOT NULL, "
                                                  "c.rendered IS NOT NULL FROM dispatch_previews p "
                                                  "LEFT JOIN dispatch_preview_contents c ON c.preview_id = p.id "
                                                  "ORDER BY p.id"),
        "offering_digests_rows": _rows(sql_conn, "SELECT enabled, local_time, next_due_at, updated_at "
                                                 "FROM offering_digests ORDER BY id"),
        "invite_batches_rows": _rows(sql_conn, "SELECT created_at FROM invite_batches ORDER BY id"),
        "invite_sends_batch_rows": _rows(sql_conn, "SELECT batch_id IS NOT NULL FROM invite_sends ORDER BY id"),
        "outbox_notice_rows": _rows(sql_conn, "SELECT delivery_role, text_format, priority, state "
                                              "FROM outbox_messages WHERE notice_id IS NOT NULL ORDER BY id"),
        "report_tasks_rows": _rows(sql_conn, f"SELECT task_type, state FROM tasks WHERE task_type IN "
                                         f"({REPORT_TASK_TYPES_SQL}) ORDER BY id"),
    }


def _row(sql_conn: psycopg.Connection, query: str, params: tuple) -> dict[str, Any]:
    cur = sql_conn.execute(query, params)
    row = cur.fetchone()
    assert row is not None, f"linha inexistente: {params}"
    return dict(zip([d.name for d in cur.description], row))


def _task_row(sql_conn: psycopg.Connection, task_id: int) -> dict[str, Any]:
    return _row(sql_conn, "SELECT state, attempts, available_at, lease_owner, lease_until, "
                          "last_error, finished_at FROM tasks WHERE id = %s", (task_id,))


def _outbox_row(sql_conn: psycopg.Connection, message_id: int) -> dict[str, Any]:
    return _row(sql_conn, "SELECT state, attempts, available_at, lease_owner, lease_until, "
                          "channel_ref, last_error, sent_at FROM outbox_messages WHERE id = %s",
                (message_id,))


def _plpgsql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _install_commit_failure(sql_conn: psycopg.Connection, event_type: str) -> None:
    """Gatilho de restrição adiado: o COMMIT de um evento `event_type` falha com o canário."""
    body = (
        "BEGIN "
        f"IF NEW.type = {_plpgsql_literal(event_type)} THEN "
        f"RAISE EXCEPTION '%', {_plpgsql_literal(FORCED_FAILURE)}; "
        "END IF; "
        "RETURN NULL; "
        "END"
    )
    name = pgsql.Identifier(FAIL_TRIGGER)
    # Uma transação esquecida aberta em `events` vira erro claro em vez de travar o teste.
    sql_conn.execute("SET lock_timeout = '10s'")
    sql_conn.execute(pgsql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}")
                     .format(name, pgsql.Literal(body)))
    sql_conn.execute(pgsql.SQL(
        "CREATE CONSTRAINT TRIGGER {} AFTER INSERT ON events DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION {}()").format(name, name))
    # Vale em qualquer `session_replication_role` da conexão da aplicação.
    sql_conn.execute(pgsql.SQL("ALTER TABLE events ENABLE ALWAYS TRIGGER {}").format(name))


def _remove_commit_failure(sql_conn: psycopg.Connection) -> None:
    name = pgsql.Identifier(FAIL_TRIGGER)
    sql_conn.execute("SET lock_timeout = '10s'")
    sql_conn.execute(pgsql.SQL("DROP TRIGGER {} ON events").format(name))
    sql_conn.execute(pgsql.SQL("DROP FUNCTION {}()").format(name))


def _wait_for(predicate: Callable[[], Any], message: str, timeout: float = 10.0) -> None:
    """Espera real (threads do worker), sempre com prazo."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


# --- 500 ---------------------------------------------------------------------------------------


def test_unhandled_exception_becomes_internal_error_without_leaking(
        app: Any, sql: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """com `generate_pseudo_id` levantando `RuntimeError` com o
    nome e o RA canário, o cadastro → 500 `internal` exato, sem a exceção escapar do app mesmo com
    `raise_server_exceptions=True`; log ⊇ `{event: exception, exc_type: RuntimeError, logger:
    cq_server.api}` com `where` no quadro mais interno, o registro `request` com status 500, sem
    canário e sem `Traceback`; nada gravado (também nas tabelas e nas filas de oferecimentos e bots;
    em `invite_sends`, nas colunas do vínculo e nas tarefas `invite.email`; nas tabelas
    do disparo, nas mensagens de entrega e nas tarefas dele; nas tabelas dos relatórios, nas mensagens de
    aviso e nas tarefas deles)."""
    monkeypatch.setattr(people, "generate_pseudo_id", _explode_with_canary)
    escaped: str | None = None
    with capture_logs() as captured:
        with TestClient(app, raise_server_exceptions=True) as c:
            try:
                resp = c.post("/v1/people", json=ZEFIRO.payload(), headers=auth(ADMIN_TOKEN))
            except Exception as exc:  # noqa: BLE001 - só o tipo vai para a falha, sem o canário
                escaped = type(exc).__name__

    assert escaped is None, f"a exceção escapou da API ({escaped})"
    assert_error(resp, 500, "internal")
    assert find_canary(_exposed(resp)) == []

    exceptions = _records(captured, logger="cq_server.api", event="exception",
                          exc_type="RuntimeError")
    assert exceptions, "sem registro event=exception (RuntimeError) no logger cq_server.api"
    for record in exceptions:
        where = record.get("where", "")
        assert re.fullmatch(r".+\.py:\d+", where), f"`where` fora do formato: {where!r}"
        assert where.endswith(f"test_api_errors.py:{EXPLODE_LINE}"), (
            f"`where` não é o quadro mais interno: {where!r}")
    _assert_one_request(captured, method="POST", route="/v1/people", status=500)
    _assert_clean_logs(captured)
    assert _fingerprint(sql) == {"people": 0, "identities": 0, "api_tokens": 0,
                                 "revoked_tokens": 0, "events": 0,
                                 # as tabelas de oferecimentos e bots vazias e nenhum item deles nas filas
                                 "offerings": 0, "bots": 0, "enrollments": 0, "invites": 0,
                                 "delivery_endpoints": 0, "telegram_updates": 0,
                                 "offerings_rows": [], "bots_rows": [], "enrollments_rows": [],
                                 "invites_rows": [], "delivery_endpoints_rows": [],
                                 "telegram_updates_rows": [], "outbox_messages_rows": [],
                                 "tasks_rows": [],
                                 # a tabela e as colunas do vínculo sem linhas
                                 "invite_sends_rows": [], "enrollments_terms_rows": [],
                                 "invites_presentation_rows": [], "offerings_invite_rows": [],
                                 "invite_email_tasks_rows": [],
                                 # as tabelas do disparo sem linhas e nenhuma tarefa nem mensagem dele
                                 "question_refs_rows": [], "dispatches_rows": [], "deliveries_rows": [],
                                 "submissions_rows": [], "free_messages_rows": [],
                                 "outbox_delivery_rows": [], "dispatch_tasks_rows": [],
                                 # as tabelas dos relatórios sem linhas e nenhuma tarefa nem mensagem deles
                                 "dispatch_reports_rows": [], "team_notices_rows": [],
                                 "dispatch_previews_rows": [], "offering_digests_rows": [],
                                 "invite_batches_rows": [], "invite_sends_batch_rows": [],
                                 "outbox_notice_rows": [], "report_tasks_rows": []}


# --- banco inalcançável ------------------------------------------------------------------------


def test_unreachable_database_returns_internal_without_leaking_password(
        app_factory: Callable[..., Any], unreachable_db_url: str) -> None:
    """O `create_app` sobe sem banco: com o banco inalcançável, `GET
    /v1/people` com o `ADMIN_TOKEN` → 500 `internal` exato, sem a exceção escapar do app; a senha
    sentinela da URL do banco fica fora do corpo, dos cabeçalhos e dos logs (que têm o registro
    `request` com status 500, para a checagem não passar no vazio)."""
    assert SENTINEL_PASSWORD in unreachable_db_url
    escaped: str | None = None
    with capture_logs() as captured:
        app = app_factory(database_url=unreachable_db_url)
        with TestClient(app, raise_server_exceptions=True) as c:
            try:
                resp = c.get("/v1/people", headers=auth(ADMIN_TOKEN))
            except Exception as exc:  # noqa: BLE001 - só o tipo vai para a falha
                escaped = type(exc).__name__

    assert escaped is None, f"a exceção escapou da API ({escaped})"
    assert_error(resp, 500, "internal")
    assert_no_echo(resp, SENTINEL_PASSWORD)
    _assert_one_request(captured, method="GET", route="/v1/people", status=500)
    text = captured.text
    assert SENTINEL_PASSWORD not in text
    assert SENTINEL_PASSWORD.casefold() not in text.casefold()
    assert find_secrets(text, all_secrets(SENTINEL_PASSWORD)) == []
    assert "Traceback" not in text


# --- falha fechada -----------------------------------------------------------------------------


@dataclass
class _FailClosedCtx:
    client: Any
    sql: psycopg.Connection
    clock: FakeClock
    settings: Any
    db_url: str
    queue: Any
    outbox: Any
    worker_thread: Any
    # Oferecimentos e bots
    bot_api: Any
    bot_worker_thread: Any
    # o SMTP falso, o `MailSettings` que aponta para ele e o adaptador real (fixtures de e-mail)
    smtp: Any
    smtp_settings: Any
    mail_channel: Any
    # o `request` do teste, para os casos do disparo pedirem `engine_fake`, `repo_fetcher` e
    # `rich_bot_api` só quando precisam (a extensão da Bot API falsa não entra nos casos anteriores)
    request: Any = None

    @property
    def lease(self) -> timedelta:
        return timedelta(seconds=self.settings.lease_seconds)


def _api_fails_closed(ctx: _FailClosedCtx, event_type: str, method: str, route: str,
                      call: Callable[[], Any]) -> None:
    """Roda `call` com o COMMIT do evento falhando: 500 exato, nada gravado, logs limpos."""
    before = _fingerprint(ctx.sql)
    _install_commit_failure(ctx.sql, event_type)
    with capture_logs() as captured:
        resp = call()
    _remove_commit_failure(ctx.sql)

    assert_error(resp, 500, "internal")
    exposed = _exposed(resp)
    assert find_canary(exposed) == [], "identidade na resposta da operação que falhou"
    assert re.search(r"cq_[A-Za-z0-9_-]{43}", exposed) is None, "segredo na resposta que falhou"
    assert _fingerprint(ctx.sql) == before, "a mudança ficou gravada sem o evento"
    assert _event_count(ctx.sql, event_type) == 0
    _assert_one_request(captured, method=method, route=route, status=500)
    _assert_clean_logs(captured, exception_logger="cq_server.api")


def _reveal_body(alias: str) -> dict:
    return {"pseudonyms": [alias], "reason": "conferencia da falha fechada"}


def _case_identity_revealed(ctx: _FailClosedCtx) -> None:
    person = register_person(ctx.client, ADMIN_TOKEN, **ZEFIRO.payload())

    def call() -> Any:
        return ctx.client.post("/v1/identity/reveal", json=_reveal_body(person["alias"]),
                               headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "identity.revealed", "POST", "/v1/identity/reveal", call)

    # Controle: sem o gatilho, a mesma revelação sai e é auditada.
    ok = call()
    assert ok.status_code == 200, ok.status_code
    assert_matches("identity.reveal", 200, ok.json())
    assert ok.json() == {
        "people": [{"pseudo_id": person["pseudo_id"], "alias": person["alias"],
                    "name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": ZEFIRO.email}],
        "not_found": [],
    }
    assert _event_count(ctx.sql, "identity.revealed") == 1


def _case_identity_reveal_denied(ctx: _FailClosedCtx) -> None:
    person = register_person(ctx.client, ADMIN_TOKEN, **ZEFIRO.payload())
    secret, _ = create_token(ctx.client, ADMIN_TOKEN, ["people:read"], label="token sem revelacao")

    def call() -> Any:
        return ctx.client.post("/v1/identity/reveal", json=_reveal_body(person["alias"]),
                               headers=auth(secret))

    _api_fails_closed(ctx, "identity.reveal_denied", "POST", "/v1/identity/reveal", call)

    # Controle: sem o gatilho, a negativa é o 403 e fica auditada.
    denied = call()
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    assert _event_count(ctx.sql, "identity.reveal_denied") == 1


def _case_person_registered(ctx: _FailClosedCtx) -> None:
    def call() -> Any:
        return ctx.client.post("/v1/people", json=ZEFIRO.payload(), headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "person.registered", "POST", "/v1/people", call)
    assert _count(ctx.sql, "people") == 0
    assert _count(ctx.sql, "identities") == 0

    # Controle: sem o gatilho, o mesmo cadastro passa, porque nada ficou para dar conflito.
    ok = call()
    assert ok.status_code == 201, ok.status_code
    assert_matches("people.register", 201, ok.json())
    assert _count(ctx.sql, "people") == 1
    assert _count(ctx.sql, "identities") == 1
    assert _event_count(ctx.sql, "person.registered") == 1


def _case_token_created(ctx: _FailClosedCtx) -> None:
    body = {"label": "rotulo da falha fechada", "permissions": ["people:read"]}

    def call() -> Any:
        return ctx.client.post("/v1/tokens", json=body, headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "token.created", "POST", "/v1/tokens", call)
    # Sem segredo devolvido (envelope exato) e sem token utilizável: nenhum token existe.
    assert _count(ctx.sql, "api_tokens") == 0
    listed = ctx.client.get("/v1/tokens", headers=auth(ADMIN_TOKEN))
    assert listed.status_code == 200, listed.text[:300]
    assert listed.json() == {"items": []}

    # Controle: sem o gatilho, o token é criado e funciona.
    ok = call()
    assert ok.status_code == 201, ok.text[:300]
    created = ok.json()
    assert_matches("tokens.create", 201, created)
    remember_secret(created["secret"])
    works = ctx.client.get("/v1/people", headers=auth(created["secret"]))
    assert works.status_code == 200, works.text[:300]
    assert _event_count(ctx.sql, "token.created") == 1


def _case_token_revoked(ctx: _FailClosedCtx) -> None:
    secret, token = create_token(ctx.client, ADMIN_TOKEN, ["people:read"], label="token a revogar")

    def call() -> Any:
        return ctx.client.post(f"/v1/tokens/{token['token_id']}/revoke", headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "token.revoked", "POST", "/v1/tokens/{token_id}/revoke", call)
    revoked_at = ctx.sql.execute("SELECT revoked_at FROM api_tokens WHERE token_id = %s::uuid",
                                 (token["token_id"],)).fetchone()
    assert revoked_at == (None,)
    still = ctx.client.get("/v1/people", headers=auth(secret))
    assert still.status_code == 200, "o token deixou de valer com a revogação que falhou"

    # Controle: sem o gatilho, a revogação vale, com o horário da nova tentativa.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("tokens.revoke", 200, ok.json())
    expected = {k: v for k, v in token.items() if k != "secret"}
    assert ok.json() == {**expected, "revoked_at": iso(ctx.clock.now())}
    assert _event_count(ctx.sql, "token.revoked") == 1
    assert_error(ctx.client.get("/v1/people", headers=auth(secret)), 401, "unauthenticated")


def _run_worker_until_exception(ctx: _FailClosedCtx, **kwargs: Any) -> Captured:
    """Sobe o worker, espera o `event: exception` do laço e para o worker; devolve os logs."""
    with capture_logs() as captured:
        thread = ctx.worker_thread(**kwargs)
        _wait_for(lambda: _records(captured, logger="cq_server.worker", event="exception"),
                  "o worker não registrou event=exception em 10 s")
        alive = thread.is_alive()
        ctx.worker_thread.stop()
    assert alive, "o worker caiu com a falha do COMMIT do evento"
    return captured


def _case_task_failed(ctx: _FailClosedCtx) -> None:
    task_id = ctx.queue.enqueue(TASK_TYPE, {"n": 1})
    claimed = ctx.queue.claim("w1")
    assert claimed is not None and claimed.id == task_id
    t0 = ctx.clock.now()
    leased = {"state": "leased", "attempts": 1, "available_at": t0, "lease_owner": "w1",
              "lease_until": t0 + ctx.lease, "last_error": None, "finished_at": None}
    assert _task_row(ctx.sql, task_id) == leased
    before = _fingerprint(ctx.sql)

    # Direto: `fail(permanent=True)` com o COMMIT do evento falhando → a exceção sobe.
    _install_commit_failure(ctx.sql, "task.failed")
    with capture_logs() as captured:
        with pytest.raises(Exception) as raised:
            ctx.queue.fail(task_id, "w1", "erro qualquer de teste", permanent=True)
    assert not isinstance(raised.value, (AssertionError, NotImplementedError))
    assert _task_row(ctx.sql, task_id) == leased
    assert _fingerprint(ctx.sql) == before
    _assert_clean_logs(captured)

    # Pelo worker: o tratador falha de vez, o COMMIT do evento falha e o laço registra a exceção.
    handled: list[int] = []

    def handler(task: Any) -> None:
        handled.append(task.id)
        raise PermanentTaskError("falha definitiva de teste")

    second = ctx.queue.enqueue(TASK_TYPE, {"n": 2})
    captured = _run_worker_until_exception(ctx, handlers={TASK_TYPE: handler},
                                           channel=ScriptedChannel([]))
    assert set(handled) == {second}
    second_row = _task_row(ctx.sql, second)
    assert second_row["state"] != "failed" and second_row["finished_at"] is None, second_row
    assert _fingerprint(ctx.sql) == before
    _assert_clean_logs(captured, exception_logger="cq_server.worker")

    # Controle: sem o gatilho, a mesma falha definitiva grava o estado e o evento.
    _remove_commit_failure(ctx.sql)
    fresh = TaskQueue(ctx.db_url, clock=ctx.clock, settings=ctx.settings)
    try:
        assert fresh.fail(task_id, "w1", "erro qualquer de teste", permanent=True) is True
    finally:
        fresh.close()
    assert _task_row(ctx.sql, task_id) == {
        **leased, "state": "failed", "lease_owner": None, "lease_until": None,
        "last_error": "erro qualquer de teste", "finished_at": ctx.clock.now()}
    assert _event_count(ctx.sql, "task.failed") == 1


def _enqueue_message(ctx: _FailClosedCtx, recipient: uuid.UUID, text: str) -> int:
    with psycopg.connect(ctx.db_url) as conn:
        message_id = ctx.outbox.enqueue(conn, recipient=recipient, text=text)
    return message_id


def _case_outbox_failed(ctx: _FailClosedCtx) -> None:
    person = register_person(ctx.client, ADMIN_TOKEN, name="Destinataria Da Falha Fechada")
    recipient = uuid.UUID(person["pseudo_id"])
    first = _enqueue_message(ctx, recipient, "mensagem da falha fechada")
    before = _fingerprint(ctx.sql)
    t0 = ctx.clock.now()

    # Direto: `Undeliverable` com o COMMIT do evento falhando → a exceção sobe.
    _install_commit_failure(ctx.sql, "outbox.failed")
    channel = ScriptedChannel([Undeliverable(UNDELIVERABLE_REASON)])
    with capture_logs() as captured:
        with pytest.raises(Exception) as raised:
            ctx.outbox.dispatch_once(channel, "w1")
    assert not isinstance(raised.value, (AssertionError, NotImplementedError))
    assert [m.message_id for m in channel.calls] == [first]
    leased = {"state": "leased", "attempts": 1, "available_at": t0, "lease_owner": "w1",
              "lease_until": t0 + ctx.lease, "channel_ref": None, "last_error": None,
              "sent_at": None}
    assert _outbox_row(ctx.sql, first) == leased
    assert _fingerprint(ctx.sql) == before
    _assert_clean_logs(captured)

    # Pelo worker: o canal diz `Undeliverable`, o COMMIT do evento falha e o laço registra a exceção.
    second = _enqueue_message(ctx, recipient, "segunda mensagem da falha fechada")
    worker_channel = ScriptedChannel([Undeliverable(UNDELIVERABLE_REASON)])
    captured = _run_worker_until_exception(ctx, channel=worker_channel, handlers={})
    assert {m.message_id for m in worker_channel.calls} == {second}
    second_row = _outbox_row(ctx.sql, second)
    assert second_row["state"] != "failed" and second_row["sent_at"] is None, second_row
    assert _fingerprint(ctx.sql) == before
    _assert_clean_logs(captured, exception_logger="cq_server.worker")

    # Controle: sem o gatilho, depois de vencida a posse, a mensagem é retomada e falha de vez.
    _remove_commit_failure(ctx.sql)
    ctx.clock.advance(ctx.settings.lease_seconds + 1)
    fresh = Outbox(ctx.db_url, clock=ctx.clock, settings=ctx.settings)
    try:
        assert fresh.dispatch_once(ScriptedChannel([Undeliverable(UNDELIVERABLE_REASON)]),
                                   "w2") == first
    finally:
        fresh.close()
    assert _outbox_row(ctx.sql, first) == {
        **leased, "state": "failed", "attempts": 2, "lease_owner": None, "lease_until": None,
        "last_error": UNDELIVERABLE_REASON}
    assert _event_count(ctx.sql, "outbox.failed") == 1


# --- falha fechada de oferecimentos e bots ------------------------------------------------------------

# Textos fixos dos bots, com a disciplina e o período neutros de `create_offering`.
OFFERING_COURSE = "Algoritmos e Programação"
OFFERING_TERM = "2031s1"
WELCOME_TEAM = (f"Pronto! Você faz parte da equipe do oferecimento {OFFERING_COURSE} ({OFFERING_TERM}). Por este "
                "chat você vai receber os avisos do oferecimento.")
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."
# Código de convite com o formato certo (22 caracteres base64url) que nenhum convite tem.
UNKNOWN_INVITE_CODE = "Zz0_-Zz0_-Zz0_-Zz0_-Aa"
# Dono de posse gravado direto no banco (o caso do arquivamento não precisa de worker).
LEASE_OWNER = "worker-da-falha-fechada"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def _offering_with_bot(ctx: _FailClosedCtx, *, with_bot: bool = False,
                 active: bool = False) -> tuple[dict, int | None]:
    """Oferecimento em rascunho (com bot registrado na Bot API falsa, e ativado, se pedido)."""
    offering = create_offering(ctx.client, ADMIN_TOKEN)
    assert (offering["course"], offering["term"]) == (OFFERING_COURSE, OFFERING_TERM)
    bot: int | None = None
    if with_bot or active:
        bot = ctx.bot_api.add_bot()
        register_bot(ctx.client, ADMIN_TOKEN, offering["code"], ctx.bot_api.token_of(bot))
    if active:
        activate_bot(ctx.client, ADMIN_TOKEN, offering["code"])
    return offering, bot


def _bot_summary(ctx: _FailClosedCtx, bot: int, *, profile_state: str = "pending") -> dict:
    """O resumo `bot` do `Offering`."""
    return {"username": ctx.bot_api.state(bot).username, "telegram_bot_id": bot,
            "state": "registered", "profile_state": profile_state}


def _post_ok(ctx: _FailClosedCtx, op_id: str, path: str, body: Any = None) -> dict:
    resp = ctx.client.post(path, headers=auth(ADMIN_TOKEN), **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _event_data(sql_conn: psycopg.Connection, event_type: str) -> list[tuple[Any, dict]]:
    """(`actor_token_id`, `data`) dos eventos do tipo, em ordem de `seq`."""
    return [(row[0], row[1]) for row in sql_conn.execute(
        "SELECT actor_token_id, data FROM events WHERE type = %s ORDER BY seq", (event_type,)).fetchall()]


def _wait_rows(ctx: _FailClosedCtx, query: str, predicate: Callable[[list[tuple]], bool],
               message: str, timeout: float = 10.0) -> list[tuple]:
    """Espera (real, com prazo; threads do worker) que as linhas de `query` satisfaçam `predicate`;
    devolve as linhas."""
    deadline = time.monotonic() + timeout
    while True:
        rows = _rows(ctx.sql, query)
        if predicate(rows):
            return rows
        assert time.monotonic() < deadline, f"{message}; última leitura: {rows}"
        time.sleep(0.02)


def _wait_bot_ready(ctx: _FailClosedCtx, bot: int) -> None:
    """O perfil do bot já foi configurado pelo worker e a consulta começou (posse assumida)."""
    _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'bot.sync_profile'",
               lambda rows: rows == [("done",)], "a tarefa bot.sync_profile não terminou em 10 s")
    ctx.bot_api.wait_for(lambda: ctx.bot_api.calls("getUpdates", bot_id=bot), timeout=10)


def _task_attempted(ctx: _FailClosedCtx, captured: Captured, task_type: str) -> tuple:
    """Espera a 1ª tentativa da única tarefa do tipo (e o registro `task.processed` dela);
    devolve `(id, state, attempts, last_error, available_at)`."""
    rows = _wait_rows(
        ctx, f"SELECT id, state, attempts, last_error, available_at FROM tasks "
             f"WHERE task_type = '{task_type}' ORDER BY id",
        lambda rows: len(rows) == 1 and rows[0][2] >= 1 and rows[0][1] != "leased",
        f"a tarefa {task_type} não foi tentada em 10 s")
    task_id = rows[0][0]
    _wait_for(lambda: _records(captured, logger="cq_server.worker", event="task.processed", id=task_id),
              f"sem registro task.processed da tarefa {task_type}")
    return rows[0]


def _assert_handler_failed(ctx: _FailClosedCtx, captured: Captured, task: tuple) -> None:
    """O caminho dos tratadores: a exceção do COMMIT é falha temporária da fila — a
    tarefa volta a `pending` com o `last_error` do tipo da exceção, depois da espera da fila —, com o
    registro `task.processed` (estado `pending`) e sem nenhum `event=exception`."""
    task_id, state, attempts, last_error, available_at = task
    assert (state, attempts, last_error) == ("pending", 1, "RaiseException")
    assert available_at == ctx.clock.now() + timedelta(seconds=ctx.settings.backoff_base_seconds)
    processed = _records(captured, logger="cq_server.worker", event="task.processed", id=task_id)
    assert [record.get("state") for record in processed] == ["pending"], processed
    assert _records(captured, event="exception") == [], "o tratador que falhou virou event=exception"
    _assert_clean_logs(captured)


def _case_offering_created(ctx: _FailClosedCtx) -> None:
    body = {"code": "oferta-falha-fechada", "course": "Disciplina Da Falha Fechada", "term": "2031s1",
            "timezone": "America/Sao_Paulo"}

    def call() -> Any:
        return ctx.client.post("/v1/offerings", json=body, headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.created", "POST", "/v1/offerings", call)
    assert _count(ctx.sql, "offerings") == 0

    # Controle: sem o gatilho, o mesmo cadastro passa, porque o código ficou livre.
    ok = call()
    assert ok.status_code == 201, ok.text[:300]
    created = ok.json()
    assert_matches("offerings.create", 201, created)
    assert uuid.UUID(created["offering_id"]).version == 4
    assert created == {"offering_id": created["offering_id"], **body, "response_window_minutes": 1440,
                       "state": "draft", "created_at": iso(ctx.clock.now()), "activated_at": None,
                       "closed_at": None, "archived_at": None, "bot": None,
                       # Mensagens livres opcionais por oferecimento: o padrão, desligado e sem contato.
                       "free_messages": False, "contact_email_set": False,
                       # O expurgo: o oferecimento não expurgado.
                       "purged_at": None}
    assert _event_count(ctx.sql, "offering.created") == 1


def _case_offering_state_changed(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, with_bot=True)
    assert bot is not None

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{offering['code']}/bot/activate",
                               headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.state_changed", "POST", "/v1/offerings/{ref}/bot/activate", call)
    assert _rows(ctx.sql, "SELECT state, activated_at FROM offerings") == [("draft", None)]

    # Controle: sem o gatilho, a ativação vale, com o horário da nova tentativa.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("bots.activate", 200, ok.json())
    assert ok.json() == {**offering, "state": "active", "activated_at": iso(ctx.clock.now()),
                         "bot": _bot_summary(ctx, bot)}
    assert _event_count(ctx.sql, "offering.state_changed") == 1


def _case_offering_archive_prepared(ctx: _FailClosedCtx) -> None:
    offering, _ = _offering_with_bot(ctx)

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{offering['code']}/archive/prepare",
                               headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.archive_prepared", "POST",
                      "/v1/offerings/{ref}/archive/prepare", call)
    assert _rows(ctx.sql, "SELECT archive_code_sha256, archive_code_expires_at FROM offerings") == [
        (None, None)]

    # Controle: sem o gatilho, a preparação devolve o código, guardado só pelo resumo.
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    prepared = ok.json()
    assert_matches("offerings.prepare_archive", 200, prepared)
    code = prepared["confirmation_code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", code)
    expires = ctx.clock.now() + timedelta(minutes=10)
    assert prepared == {"offering": offering, "team_members_active": 0, "pending_links": 0,
                        "confirmation_code": code, "expires_at": iso(expires)}
    assert _rows(ctx.sql, "SELECT archive_code_sha256, archive_code_expires_at FROM offerings") == [
        (_sha256_hex(code), expires)]
    assert _event_count(ctx.sql, "offering.archive_prepared") == 1


def _case_offering_settings_changed(ctx: _FailClosedCtx) -> None:
    """Mensagens livres opcionais por oferecimento: ligar as mensagens livres
    com o COMMIT do `offering.settings_changed` falhando → 500 exato, a coluna continua `false` e nenhum
    contato gravado; sem o gatilho, 200 com o `Offering` e o evento exato com o ator."""
    offering, _ = _offering_with_bot(ctx)

    def call() -> Any:
        return ctx.client.patch(f"/v1/offerings/{offering['code']}/settings", json={"free_messages": True},
                                headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.settings_changed", "PATCH", "/v1/offerings/{ref}/settings", call)
    assert _rows(ctx.sql, "SELECT free_messages FROM offerings") == [(False,)]
    assert _count(ctx.sql, "offering_contacts") == 0

    # Controle: sem o gatilho, a mesma alteração vale e é auditada.
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("offerings.update_settings", 200, ok.json())
    assert ok.json() == {**offering, "free_messages": True, "contact_email_set": False}
    assert _rows(ctx.sql, "SELECT free_messages FROM offerings") == [(True,)]
    assert _count(ctx.sql, "offering_contacts") == 0
    assert _event_data(ctx.sql, "offering.settings_changed") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "free_messages": True,
                       "contact_email_set": False})]


def _case_bot_registered(ctx: _FailClosedCtx) -> None:
    offering, _ = _offering_with_bot(ctx)
    bot = ctx.bot_api.add_bot()
    token = ctx.bot_api.token_of(bot)

    def call() -> Any:
        return ctx.client.put(f"/v1/offerings/{offering['code']}/bot", json={"token": token},
                              headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "bot.registered", "PUT", "/v1/offerings/{ref}/bot", call)
    assert _count(ctx.sql, "bots") == 0
    assert _fingerprint(ctx.sql)["tasks_rows"] == []

    # Controle: sem o gatilho, o registro vale e agenda o perfil.
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("bots.register", 200, ok.json())
    assert ok.json() == {"offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
                         "username": ctx.bot_api.state(bot).username, "telegram_bot_id": bot,
                         "state": "registered", "profile_state": "pending",
                         "registered_at": iso(ctx.clock.now()), "token_replaced_at": None,
                         "lease": None, "last_poll_ok_at": None, "problem": None}
    assert _fingerprint(ctx.sql)["tasks_rows"] == [("bot.sync_profile", "pending")]
    assert _event_count(ctx.sql, "bot.registered") == 1


def _replace_token_case(ctx: _FailClosedCtx, event_type: str, *, no_access: bool) -> None:
    """Troca do token do mesmo bot com o COMMIT de `event_type` falhando."""
    offering, bot = _offering_with_bot(ctx, with_bot=True)
    assert bot is not None
    registered_at = ctx.clock.now()
    if no_access:
        # Bot sem acesso, gravado direto no banco: o caminho que leva a
        # `no_access` é conferido em test_bot_failures.py; aqui só importa a recuperação.
        ctx.sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                        "WHERE telegram_bot_id = %s", (bot,))
    ctx.clock.advance(60)
    new_token = ctx.bot_api.rotate_token(bot)

    def call() -> Any:
        return ctx.client.put(f"/v1/offerings/{offering['code']}/bot", json={"token": new_token},
                              headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, event_type, "PUT", "/v1/offerings/{ref}/bot", call)
    expected_state = ("no_access", "token_rejected") if no_access else ("registered", None)
    assert _rows(ctx.sql, "SELECT state, access_problem, token_version, token_replaced_at FROM bots") == [
        (*expected_state, 1, None)]
    assert _event_count(ctx.sql, "bot.token_replaced") == 0
    assert _event_count(ctx.sql, "bot.access_changed") == 0

    # Controle: sem o gatilho, a troca vale (e recupera o acesso, se era o caso).
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("bots.register", 200, ok.json())
    assert ok.json() == {"offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
                         "username": ctx.bot_api.state(bot).username, "telegram_bot_id": bot,
                         "state": "registered", "profile_state": "pending",
                         "registered_at": iso(registered_at), "token_replaced_at": iso(ctx.clock.now()),
                         "lease": None, "last_poll_ok_at": None, "problem": None}
    assert _rows(ctx.sql, "SELECT state, access_problem, token_version FROM bots") == [
        ("registered", None, 2)]
    assert _event_count(ctx.sql, "bot.token_replaced") == 1
    assert _event_count(ctx.sql, "bot.access_changed") == (1 if no_access else 0)


def _case_bot_token_replaced(ctx: _FailClosedCtx) -> None:
    _replace_token_case(ctx, "bot.token_replaced", no_access=False)


def _case_bot_access_changed(ctx: _FailClosedCtx) -> None:
    _replace_token_case(ctx, "bot.access_changed", no_access=True)


def _case_bot_lease_changed(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    code_ref = offering["code"]
    activated_at = ctx.clock.now()
    closed_at = ctx.clock.advance(60)
    closed = {**offering, "state": "closed", "activated_at": iso(activated_at),
              "closed_at": iso(closed_at), "bot": _bot_summary(ctx, bot)}
    assert _post_ok(ctx, "offerings.close", f"/v1/offerings/{code_ref}/close") == closed
    ctx.clock.advance(60)
    prepared = _post_ok(ctx, "offerings.prepare_archive", f"/v1/offerings/{code_ref}/archive/prepare")
    confirmation = prepared["confirmation_code"]
    assert prepared == {"offering": closed, "team_members_active": 0, "pending_links": 0,
                        "confirmation_code": confirmation,
                        "expires_at": iso(ctx.clock.now() + timedelta(minutes=10))}
    # Posse de um worker, gravada direto no banco: o arquivamento a
    # limpa na mesma transação, com o evento `bot.lease_changed` (`released`/`archived`).
    lease_until = ctx.clock.now() + timedelta(seconds=30)
    ctx.sql.execute("UPDATE bots SET lease_owner = %s, lease_until = %s WHERE telegram_bot_id = %s",
                    (LEASE_OWNER, lease_until, bot))
    tasks_before = _fingerprint(ctx.sql)["tasks_rows"]

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{code_ref}/archive",
                               json={"confirmation_code": confirmation}, headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "bot.lease_changed", "POST", "/v1/offerings/{ref}/archive", call)
    assert _rows(ctx.sql, "SELECT state, archive_code_sha256 FROM offerings") == [
        ("closed", _sha256_hex(confirmation))]
    assert _rows(ctx.sql, "SELECT lease_owner, lease_until FROM bots") == [(LEASE_OWNER, lease_until)]

    # Controle: sem o gatilho, o mesmo código (ainda válido) arquiva e limpa a posse (ainda vigente).
    ctx.clock.advance(10)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("offerings.archive", 200, ok.json())
    assert ok.json() == {**offering, "state": "archived", "activated_at": iso(activated_at),
                         "closed_at": iso(closed_at), "archived_at": iso(ctx.clock.now()),
                         "bot": _bot_summary(ctx, bot)}
    assert _rows(ctx.sql, "SELECT lease_owner, lease_until FROM bots") == [(None, None)]
    assert _event_data(ctx.sql, "bot.lease_changed") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "worker_id": LEASE_OWNER,
                       "change": "released", "reason": "archived"})]
    assert _fingerprint(ctx.sql)["tasks_rows"] == [*tasks_before, ("bot.sync_profile", "pending")]


def _team_link_call(ctx: _FailClosedCtx, offering: dict, person: dict) -> Callable[[], Any]:
    def call() -> Any:
        resp = ctx.client.post(f"/v1/offerings/{offering['code']}/team-links",
                               json={"person": person["alias"]}, headers=auth(ADMIN_TOKEN))
        if resp.status_code == 201 and isinstance(resp.json().get("link"), str):
            remember_secret(resp.json()["link"])
            match = TEAM_LINK_SHAPE.fullmatch(resp.json()["link"])
            if match is not None:
                remember_secret(match.group(2))
        return resp

    return call


def _assert_link_created(ctx: _FailClosedCtx, resp: Any, offering: dict, person: dict,
                         bot: int) -> str:
    """201 exato de `team.link`; devolve o código do convite."""
    assert resp.status_code == 201, resp.text[:300]
    body = resp.json()
    assert_matches("team.link", 201, body)
    match = TEAM_LINK_SHAPE.fullmatch(body["link"])
    assert match is not None and match.group(1) == ctx.bot_api.state(bot).username
    assert body == {"link": body["link"], "expires_at": iso(ctx.clock.now() + timedelta(hours=168)),
                    "person": {"pseudo_id": person["pseudo_id"], "alias": person["alias"]},
                    "offering": {"offering_id": offering["offering_id"], "code": offering["code"],
                                 "timezone": "America/Sao_Paulo"}}
    return match.group(2)


def _case_team_link_created(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, with_bot=True)
    assert bot is not None
    person = register_person(ctx.client, ADMIN_TOKEN, name="Professora Da Falha Fechada")
    call = _team_link_call(ctx, offering, person)

    _api_fails_closed(ctx, "team.link_created", "POST", "/v1/offerings/{ref}/team-links", call)
    assert (_count(ctx.sql, "enrollments"), _count(ctx.sql, "invites")) == (0, 0)

    # Controle: sem o gatilho, o link sai, com a matrícula e o convite pendentes.
    code = _assert_link_created(ctx, call(), offering, person, bot)
    assert _rows(ctx.sql, "SELECT role, state FROM enrollments") == [("team", "pending")]
    assert _rows(ctx.sql, "SELECT state, code_sha256 FROM invites") == [("pending", _sha256_hex(code))]
    assert _event_count(ctx.sql, "team.link_created") == 1


def _case_team_link_revoked(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, with_bot=True)
    assert bot is not None
    person = register_person(ctx.client, ADMIN_TOKEN, name="Professora Da Falha Fechada")
    first = link_code(create_team_link(ctx.client, ADMIN_TOKEN, offering["code"], person["alias"])["link"])
    call = _team_link_call(ctx, offering, person)

    _api_fails_closed(ctx, "team.link_revoked", "POST", "/v1/offerings/{ref}/team-links", call)
    assert _rows(ctx.sql, "SELECT state, code_sha256 FROM invites") == [("pending", _sha256_hex(first))]

    # Controle: sem o gatilho, o link novo sai e o anterior fica revogado.
    ctx.clock.advance(60)
    second = _assert_link_created(ctx, call(), offering, person, bot)
    assert _rows(ctx.sql, "SELECT state, code_sha256 FROM invites ORDER BY id") == [
        ("revoked", _sha256_hex(first)), ("pending", _sha256_hex(second))]
    assert _event_count(ctx.sql, "team.link_revoked") == 1
    assert _event_count(ctx.sql, "team.link_created") == 2


def _case_bot_profile_updated(ctx: _FailClosedCtx) -> None:
    # Rascunho com bot: o worker só roda a tarefa do perfil (nada é consultado).
    offering, bot = _offering_with_bot(ctx, with_bot=True)
    assert bot is not None
    before = _fingerprint(ctx.sql)
    assert before["tasks_rows"] == [("bot.sync_profile", "pending")]
    assert before["bots_rows"][0][4] == "pending"
    _install_commit_failure(ctx.sql, "bot.profile_updated")
    with capture_logs() as captured:
        ctx.bot_worker_thread()
        task = _task_attempted(ctx, captured, "bot.sync_profile")
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    assert after == before, "o perfil ficou configurado sem o evento"

    # Controle: sem o gatilho, depois da espera, a nova tentativa configura o perfil.
    ctx.clock.advance(5)
    _wait_rows(ctx, "SELECT state, attempts FROM tasks WHERE task_type = 'bot.sync_profile'",
               lambda rows: rows == [("done", 2)], "a nova tentativa do perfil não terminou em 10 s")
    assert _rows(ctx.sql, "SELECT profile_state FROM bots") == [("configured",)]
    assert _event_count(ctx.sql, "bot.profile_updated") == 1
    ctx.bot_worker_thread.stop()


def _join_setup(ctx: _FailClosedCtx) -> tuple[dict, int, dict, str]:
    """Oferecimento ativo com bot, uma pessoa e o link de equipe dela."""
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    person = register_person(ctx.client, ADMIN_TOKEN, name="Professora Da Falha Fechada")
    code = link_code(create_team_link(ctx.client, ADMIN_TOKEN, offering["code"], person["alias"])["link"])
    return offering, bot, person, code


def _case_team_joined(ctx: _FailClosedCtx) -> None:
    offering, bot, person, code = _join_setup(ctx)
    sender = make_user(301)
    ctx.bot_worker_thread()
    _wait_bot_ready(ctx, bot)
    before = _fingerprint(ctx.sql)
    assert (before["enrollments_rows"], before["invites_rows"], before["delivery_endpoints"],
            before["telegram_updates"], before["outbox_messages_rows"]) == (
        [("pending",)], [("pending",)], 0, 0, [])
    _install_commit_failure(ctx.sql, "team.joined")
    with capture_logs() as captured:
        ctx.bot_api.push_start(bot, sender, code)
        task = _task_attempted(ctx, captured, "telegram.update")
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    # convite e matrícula pendentes, sem endereço, sem boas-vindas e o update pendente.
    assert after == {**before, "telegram_updates": 1, "telegram_updates_rows": [("pending", None)],
                     "tasks_rows": [*before["tasks_rows"], ("telegram.update", "pending")]}
    assert ctx.bot_api.calls("sendMessage", bot_id=bot) == []

    # Controle: sem o gatilho, depois da espera, a nova tentativa faz o vínculo e dá boas-vindas.
    ctx.clock.advance(5)
    _wait_rows(ctx, "SELECT state FROM outbox_messages WHERE bot_id IS NOT NULL",
               lambda rows: rows == [("sent",)], "as boas-vindas não foram entregues em 10 s")
    assert [(m.chat_id, m.text) for m in ctx.bot_api.sent_messages(bot)] == [(sender.user_id, WELCOME_TEAM)]
    final = _fingerprint(ctx.sql)
    assert (final["invites_rows"], final["enrollments_rows"], final["delivery_endpoints_rows"],
            final["telegram_updates_rows"]) == (
        [("used",)], [("active",)], [("active",)], [("processed", "team_joined")])
    assert _event_data(ctx.sql, "team.joined") == [
        (None, {"offering_id": offering["offering_id"], "person": person["pseudo_id"],
                "endpoint_replaced": False})]
    ctx.bot_worker_thread.stop()


def _case_team_join_refused(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    sender = make_user(302)
    ctx.bot_worker_thread()
    _wait_bot_ready(ctx, bot)
    before = _fingerprint(ctx.sql)
    assert (before["telegram_updates"], before["outbox_messages_rows"]) == (0, [])
    _install_commit_failure(ctx.sql, "team.join_refused")
    with capture_logs() as captured:
        ctx.bot_api.push_start(bot, sender, UNKNOWN_INVITE_CODE)
        task = _task_attempted(ctx, captured, "telegram.update")
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    assert after == {**before, "telegram_updates": 1, "telegram_updates_rows": [("pending", None)],
                     "tasks_rows": [*before["tasks_rows"], ("telegram.update", "pending")]}
    assert ctx.bot_api.calls("sendMessage", bot_id=bot) == []

    # Controle: sem o gatilho, depois da espera, a recusa sai, com a resposta ao chat do update.
    ctx.clock.advance(5)
    _wait_rows(ctx, "SELECT state FROM outbox_messages WHERE bot_id IS NOT NULL",
               lambda rows: rows == [("sent",)], "a resposta LINK_INVALID não foi entregue em 10 s")
    assert [(m.chat_id, m.text) for m in ctx.bot_api.sent_messages(bot)] == [(sender.user_id, LINK_INVALID)]
    assert _fingerprint(ctx.sql)["telegram_updates_rows"] == [("processed", "team_refused_invalid")]
    assert _event_data(ctx.sql, "team.join_refused") == [
        (None, {"offering_id": offering["offering_id"], "refusal": "invalid"})]
    ctx.bot_worker_thread.stop()


def _delivery_fails_closed(ctx: _FailClosedCtx, event_type: str, bot: int, sender: Any,
                           code: str) -> tuple[dict, dict, Captured]:
    """Com o chat de `sender` bloqueado (403 no `sendMessage`), o `/start` é processado e a resposta
    falha de vez; o COMMIT de `event_type`, gravado com o resultado da
    entrega, falha, e o laço do worker registra a exceção. Devolve (antes, depois, logs)."""
    ctx.bot_api.block_chat(bot, sender)
    ctx.bot_worker_thread()
    _wait_bot_ready(ctx, bot)
    before = _fingerprint(ctx.sql)
    _install_commit_failure(ctx.sql, event_type)
    with capture_logs() as captured:
        ctx.bot_api.push_start(bot, sender, code)
        _wait_for(lambda: _records(captured, logger="cq_server.worker", event="exception"),
                  "o worker não registrou event=exception em 10 s")
        _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'telegram.update'",
                   lambda rows: rows == [("done",)], "o /start não foi processado em 10 s")
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)
    _assert_clean_logs(captured, exception_logger="cq_server.worker")
    # A mensagem continua em posse, na 1ª tentativa, e o Telegram recusou o único envio.
    assert _rows(ctx.sql, "SELECT state, attempts, sent_at FROM outbox_messages "
                          "WHERE bot_id IS NOT NULL") == [("leased", 1, None)]
    assert [(c.status, c.outcome) for c in ctx.bot_api.calls("sendMessage", bot_id=bot)] == [(403, "error")]
    assert ctx.bot_api.sent_messages(bot) == []
    return before, after, captured


def _case_endpoint_blocked(ctx: _FailClosedCtx) -> None:
    offering, bot, person, code = _join_setup(ctx)
    sender = make_user(303)
    before, after, _ = _delivery_fails_closed(ctx, "endpoint.blocked", bot, sender, code)
    bot_row = _rows(ctx.sql, "SELECT id FROM bots")[0][0]
    # O vínculo (outra transação) ficou; a falha da entrega e o endereço bloqueado, não.
    assert after == {**before, "events": before["events"] + 1, "delivery_endpoints": 1,
                     "telegram_updates": 1, "enrollments_rows": [("active",)],
                     "invites_rows": [("used",)], "delivery_endpoints_rows": [("active",)],
                     "telegram_updates_rows": [("processed", "team_joined")],
                     "outbox_messages_rows": [("leased", bot_row)],
                     "tasks_rows": [*before["tasks_rows"], ("telegram.update", "done")]}
    assert _event_count(ctx.sql, "outbox.failed") == 0

    # Controle: sem o gatilho, vencida a posse da mensagem, ela é retomada e falha de vez, com o
    # endereço bloqueado e os eventos `outbox.failed` e `endpoint.blocked`, nessa ordem.
    ctx.clock.advance(ctx.settings.lease_seconds + 1)
    rows = _wait_rows(ctx, "SELECT id, state, attempts, last_error FROM outbox_messages "
                           "WHERE bot_id IS NOT NULL",
                      lambda rows: len(rows) == 1 and rows[0][1] == "failed",
                      "a mensagem não falhou de vez em 10 s")
    message_id = rows[0][0]
    assert rows == [(message_id, "failed", 2, "chat_unreachable")]
    assert _rows(ctx.sql, "SELECT state FROM delivery_endpoints") == [("blocked",)]
    assert _rows(ctx.sql, "SELECT type, actor_token_id, data FROM events WHERE type IN "
                          "('outbox.failed', 'endpoint.blocked') ORDER BY seq") == [
        ("outbox.failed", None, {"message_id": message_id, "recipient": person["pseudo_id"],
                                 "attempts": 2, "error": "chat_unreachable"}),
        ("endpoint.blocked", None, {"offering_id": offering["offering_id"],
                                    "person": person["pseudo_id"], "channel": "telegram"}),
    ]
    ctx.bot_worker_thread.stop()


def _case_outbox_reply_failed(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    sender = make_user(304)
    before, after, _ = _delivery_fails_closed(ctx, "outbox.reply_failed", bot, sender,
                                              UNKNOWN_INVITE_CODE)
    bot_row = _rows(ctx.sql, "SELECT id FROM bots")[0][0]
    update_row = _rows(ctx.sql, "SELECT id FROM telegram_updates")[0][0]
    # A recusa do vínculo (outra transação) ficou; a falha da resposta, não.
    assert after == {**before, "events": before["events"] + 1, "telegram_updates": 1,
                     "telegram_updates_rows": [("processed", "team_refused_invalid")],
                     "outbox_messages_rows": [("leased", bot_row)],
                     "tasks_rows": [*before["tasks_rows"], ("telegram.update", "done")]}
    assert _event_count(ctx.sql, "team.join_refused") == 1

    # Controle: sem o gatilho, vencida a posse da resposta, ela é retomada e falha de vez.
    ctx.clock.advance(ctx.settings.lease_seconds + 1)
    rows = _wait_rows(ctx, "SELECT id, state, attempts, last_error, reply_to_update_id "
                           "FROM outbox_messages WHERE bot_id IS NOT NULL",
                      lambda rows: len(rows) == 1 and rows[0][1] == "failed",
                      "a resposta não falhou de vez em 10 s")
    message_id = rows[0][0]
    assert rows == [(message_id, "failed", 2, "chat_unreachable", update_row)]
    assert _event_data(ctx.sql, "outbox.reply_failed") == [
        (None, {"message_id": message_id, "reply_to": update_row, "attempts": 2,
                "error": "chat_unreachable"})]
    assert _count(ctx.sql, "delivery_endpoints") == 0
    ctx.bot_worker_thread.stop()


# --- falha fechada do vínculo do aluno ---------------------------------------------------------------

# O aluno da lista é o aluno canário A (tests/helpers/canary.py): a varredura com
# `STUDENT_PREFIXES` tem alvo nas saídas destes casos. O endereço de envio
# é o `ascii_email` do `email_validator`: a parte local na caixa gravada e o domínio em minúsculas.
INVITED_STUDENT = STUDENT_A
INVITED_ADDRESS = (INVITED_STUDENT.email.rpartition("@")[0] + "@"
              + INVITED_STUDENT.email.rpartition("@")[2].lower())
INVITE_TIMEZONE = "America/Sao_Paulo"  # o fuso de `create_offering`
INVITE_VALID_HOURS = 168  # a validade padrão do preparo e do reenvio
INVITE_PREPARER = "preparo-da-falha-fechada"  # dono da tarefa do perfil consumida antes do worker sem bots

# Textos fixos do termo e do convite, por extenso (não vêm do código), com a disciplina e o
# período neutros de `create_offering` e a versão 1 do termo.
LINK_TERMS = "\n".join((
    f"Termo de uso do bot do oferecimento {OFFERING_COURSE} ({OFFERING_TERM}), versão 1",
    "",
    "Leia até o fim: as opções para aceitar ou recusar estão no final.",
    "",
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a elas. As "
    "respostas são registradas para o ensino na disciplina e para o piloto de um Trabalho de Conclusão "
    "de Curso (TCC) de Ciência da Computação da Unicamp, que avalia este sistema.",
    "",
    "Quais dados são guardados:",
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;",
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do perfil e o "
    "telefone não são guardados);",
    "• as suas respostas e as mensagens que você mandar a este bot.",
    "",
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno aparece por "
    "um código. A ligação entre o código e o seu nome só é consultada quando necessário, e cada consulta "
    "fica registrada. No TCC, os resultados aparecem só de forma agregada, sem identificar ninguém.",
    "",
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste oferecimento, "
    "definida pela coordenação. Depois desse prazo, os seus dados de identificação (nome, RA, e-mail e "
    "conta do Telegram) são apagados, e as respostas continuam guardadas sem eles.",
    "",
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste piloto, estas "
    "questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail continua valendo até "
    "vencer.",
    "",
    "Para aceitar, toque em /aceito",
    "Para recusar, toque em /recuso",
))
LINK_WELCOME_STUDENT = (f"Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {OFFERING_COURSE} "
                      f"({OFFERING_TERM}). As questões da disciplina vão chegar por este chat. Se precisar de "
                      "ajuda, fale com a coordenação.")
INVITE_EMAIL_SUBJECT = f"Seu link para o bot do oferecimento {OFFERING_COURSE} ({OFFERING_TERM})"
INVITE_EMAIL_HEADERS = ["content-transfer-encoding", "content-type", "date", "from", "message-id",
                    "mime-version", "subject", "to"]

# Os eventos de aluno gravados pela tarefa `invite.email`.
_INVITE_TASK_EVENTS_SQL = ("SELECT type, actor_token_id, data FROM events WHERE type IN "
                           "('student.invite_sent', 'student.invite_failed', "
                           "'student.invite_cancelled', 'student.invite_revoked', 'task.failed') "
                           "ORDER BY seq")


def _offering_ref(offering: dict) -> dict:
    """O `OfferingRef` das respostas do vínculo do aluno."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _assert_no_student_identity(*texts: str) -> None:
    """Nenhum alvo dos alunos canário (`STUDENT_PREFIXES`) em `texts`, com o controle positivo
    do detector no endereço do aluno A e contra o vazio."""
    assert find_canary(INVITED_STUDENT.email, prefixes=("aluno-email",)) == [f"aluno-email:{INVITED_STUDENT.email}"]
    for text in texts:
        assert text, "varredura de identidade de aluno no vazio"
        found = find_canary(text, prefixes=STUDENT_PREFIXES)
        assert found == [], f"identidade de aluno canário: {found}"


def _student_link_api_fails_closed(ctx: _FailClosedCtx, event_type: str, method: str, route: str,
                         call: Callable[[], Any]) -> None:
    """`_api_fails_closed` (500 exato, nada gravado, logs limpos), mais a varredura dos alunos canário
    na resposta que falhou e nos logs da chamada."""
    responses: list[Any] = []

    def recorded() -> Any:
        responses.append(call())
        return responses[-1]

    with capture_logs() as captured:
        _api_fails_closed(ctx, event_type, method, route, recorded)
    assert len(responses) == 1
    _assert_no_student_identity(_exposed(responses[0]), captured.text)


def _import_student(ctx: _FailClosedCtx, offering: dict) -> dict:
    """Importa o aluno canário A no oferecimento (`students.import`, sem gatilho); devolve o
    pseudônimo da pessoa criada (`{pseudo_id, alias}`, do `person.registered` da importação: a
    resposta só traz contagens)."""
    imported = import_students(ctx.client, ADMIN_TOKEN, offering["code"], [INVITED_STUDENT.row()])
    assert imported == {"offering": _offering_ref(offering), "received": 1, "people_created": 1,
                        "people_reused": 0, "enrollments_created": 1, "already_enrolled": 0}
    registered = _event_data(ctx.sql, "person.registered")
    assert [actor for actor, _ in registered] == ["bootstrap"]
    person = registered[0][1]
    assert person == {"pseudo_id": person["pseudo_id"], "alias": person["alias"]}
    assert uuid.UUID(person["pseudo_id"]).version == 4
    return person


def _queue_batch(ctx: _FailClosedCtx, offering: dict) -> None:
    """Prepara e confirma o envio do lote (`invites.prepare_send` e `invites.send`, sem gatilho), com
    o aluno importado elegível: um envio `batch` e a sua tarefa `invite.email`."""
    prepared = prepare_invites(ctx.client, ADMIN_TOKEN, offering["code"])
    confirmation = prepared["confirmation_code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", confirmation)
    assert prepared == {"offering": _offering_ref(offering), "eligible": 1, "valid_hours": INVITE_VALID_HOURS,
                        "confirmation_code": confirmation,
                        "expires_at": iso(ctx.clock.now() + timedelta(minutes=10))}
    queued = send_invites(ctx.client, ADMIN_TOKEN, offering["code"], confirmation)
    assert queued == {"offering": _offering_ref(offering), "queued": 1, "valid_hours": INVITE_VALID_HOURS}


def _drain_profile_task(ctx: _FailClosedCtx) -> None:
    """Os casos do vínculo do aluno com o worker sem bots não consultam o Telegram: a tarefa `bot.sync_profile` do
    registro do bot é consumida antes, sem efeito, por `queue.work_once` (sem tratador, o worker sem bots a
    falharia de vez, com `task.failed`), e o instantâneo fica estável enquanto o worker roda."""
    assert ctx.queue.work_once(INVITE_PREPARER, {"bot.sync_profile": lambda task: None}) is not None
    assert _fingerprint(ctx.sql)["tasks_rows"] == [("bot.sync_profile", "done")]


def _start_mail_worker(ctx: _FailClosedCtx) -> None:
    """Worker sem bots com o adaptador SMTP real sobre o SMTP falso: o `verification=` vai pelos
    kwargs que a fixture `worker_thread` repassa ao `run_worker`."""
    ctx.worker_thread(channel=ScriptedChannel([]), verification=ctx.mail_channel)


def _nth_task_attempted(ctx: _FailClosedCtx, captured: Captured, task_type: str, count: int) -> tuple:
    """Como `_task_attempted`, para a `count`-ésima tarefa do tipo (as anteriores já terminaram):
    espera a 1ª tentativa dela e o registro `task.processed`; devolve `(id, state, attempts,
    last_error, available_at)`."""
    rows = _wait_rows(
        ctx, f"SELECT id, state, attempts, last_error, available_at FROM tasks "
             f"WHERE task_type = '{task_type}' ORDER BY id",
        lambda rows: len(rows) == count and rows[-1][2] >= 1 and rows[-1][1] != "leased",
        f"a {count}ª tarefa {task_type} não foi tentada em 10 s")
    task_id = rows[-1][0]
    _wait_for(lambda: _records(captured, logger="cq_server.worker", event="task.processed", id=task_id),
              f"sem registro task.processed da {count}ª tarefa {task_type}")
    return rows[-1]


def _invite_email_records(captured: Captured) -> list[tuple]:
    """(`invite_send_id`, `result`) dos registros `invite.email`, na ordem; todos
    no nível INFO."""
    records = _records(captured, logger="cq_server.worker", event="invite.email")
    assert [record.get("level") for record in records] == ["INFO"] * len(records), records
    return [(record.get("invite_send_id"), record.get("result")) for record in records]


def _send_ids(ctx: _FailClosedCtx) -> list[int]:
    """Os ids internos de `invite_sends`, em ordem (só para as relações, nunca como literal)."""
    return [row[0] for row in _rows(ctx.sql, "SELECT id FROM invite_sends ORDER BY id")]


def _done_task(available_at: datetime, finished_at: datetime) -> dict[str, Any]:
    """A linha de uma tarefa concluída na 2ª tentativa, depois da falha do COMMIT na 1ª (na fila, o
    `last_error` da falha fica)."""
    return {"state": "done", "attempts": 2, "available_at": available_at, "lease_owner": None,
            "lease_until": None, "last_error": "RaiseException", "finished_at": finished_at}


def _validity(moment: datetime) -> str:
    """A validade no fuso do oferecimento."""
    local = moment.astimezone(ZoneInfo(INVITE_TIMEZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({INVITE_TIMEZONE})"


def _invite_body(link: str, expires: datetime) -> str:
    """O corpo do e-mail do convite, por extenso."""
    return "\n".join((
        "Olá!",
        "",
        f"Você está na lista da turma do oferecimento {OFFERING_COURSE} ({OFFERING_TERM}). As questões da disciplina "
        "chegam por um bot do Telegram, e este é o seu link pessoal para começar:",
        "",
        link,
        "",
        "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot "
        "vai mostrar o termo de uso, que você pode aceitar ou recusar.",
        "",
        f"O link vale até {_validity(expires)}. Não o repasse: ele é só seu.",
        "",
        "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
        "e-mail institucional (o mesmo da lista da turma) para receber um novo link.",
        "",
        "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.",
    ))


def _assert_invite_email(ctx: _FailClosedCtx, message: Any, link: Any, *, bot: int, expires: datetime,
                         sent_from: datetime, sent_until: datetime) -> str:
    """O e-mail do convite exato: envelope (`MAIL FROM` do remetente,
    sem parâmetro além do `SIZE` do `smtplib`, nunca o `SMTPUTF8`, e `RCPT TO` só com o endereço de
    envio), autenticado, sem TLS (modo `none` da fixture) e entregue; os cabeçalhos exatos, sem
    repetição, com `Date` (UTC, entre `sent_from` e `sent_until`, em tempo real) e `Message-ID`
    (domínio do remetente) conferidos por forma; o texto decodificado igual ao corpo (o pacote `email`
    termina o corpo com uma quebra de linha), com o link do bot do oferecimento e a validade no fuso;
    o bruto só em ASCII (quoted-printable); o código do link nas saídas só como resumo. Devolve o
    resumo SHA-256 do código."""
    sender = ctx.smtp_settings.sender
    username = ctx.bot_api.state(bot).username
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated,
            message.acknowledged) == (sender, (INVITED_ADDRESS,), False, True, True)
    assert [param for param in message.mail_params
            if not re.fullmatch(r"SIZE=[0-9]+", param, re.IGNORECASE)] == []
    assert (link.message_seq, link.rcpt_to, link.bot_username, link.acknowledged) == (
        message.seq, (INVITED_ADDRESS,), username, True)
    assert link.url == f"https://t.me/{username}?start={link.code}"
    assert sorted(name.lower() for name in message.header_names()) == INVITE_EMAIL_HEADERS
    headers = {name.lower(): value for name, value in message.headers}
    date, message_id = headers.pop("date"), headers.pop("message-id")
    assert headers == {
        "from": str(Address(display_name="Canal de Questões", addr_spec=sender)),
        "to": INVITED_ADDRESS, "subject": INVITE_EMAIL_SUBJECT, "mime-version": "1.0",
        "content-type": 'text/plain; charset="utf-8"', "content-transfer-encoding": "quoted-printable"}
    sent_at = parsedate_to_datetime(date)
    assert sent_at.utcoffset() == timedelta(0), f"Date fora de UTC: {date}"
    assert sent_from.replace(microsecond=0) <= sent_at <= sent_until, date
    domain = re.escape(sender.rpartition("@")[2])
    assert re.fullmatch(rf"<[^<>@\s]+@{domain}>", message_id), message_id
    assert message.text == _invite_body(link.url, expires) + "\n"
    assert message.raw.isascii()
    digest = invite_code_digest(link.code)
    # Controle positivo do detector de código: o código é achado no e-mail entregue.
    assert find_invite_codes(message.text, [digest]) == [invite_code_label(digest)]
    return digest


def _rcpt_codes(ctx: _FailClosedCtx) -> list[list[int | None]]:
    """Os códigos de resposta a cada RCPT, por conexão ao SMTP falso, em ordem."""
    return [[command.code for command in conn.commands if command.verb == "RCPT"]
            for conn in ctx.smtp.connections()]


def _case_student_list_imported(ctx: _FailClosedCtx) -> None:
    offering, _ = _offering_with_bot(ctx)
    body = {"rows": [INVITED_STUDENT.row()]}

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{offering['code']}/students", json=body,
                               headers=auth(ADMIN_TOKEN))

    _student_link_api_fails_closed(ctx, "student.list_imported", "POST", "/v1/offerings/{ref}/students", call)
    assert (_count(ctx.sql, "people"), _count(ctx.sql, "identities"), _count(ctx.sql, "enrollments")) == (
        0, 0, 0)
    assert _event_count(ctx.sql, "person.registered") == 0

    # Controle: sem o gatilho, a mesma importação passa, com a pessoa, a matrícula e os eventos.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("students.import", 200, ok.json())
    counts = {"received": 1, "people_created": 1, "people_reused": 0, "enrollments_created": 1,
              "already_enrolled": 0}
    assert ok.json() == {"offering": _offering_ref(offering), **counts}
    assert (_count(ctx.sql, "people"), _count(ctx.sql, "identities")) == (1, 1)
    assert _rows(ctx.sql, "SELECT role, state FROM enrollments") == [("student", "pending")]
    assert _fingerprint(ctx.sql)["enrollments_terms_rows"] == [("pending", None, None)]
    assert [row[0] for row in _rows(ctx.sql, "SELECT type FROM events ORDER BY seq")] == [
        "offering.created", "person.registered", "student.list_imported"]
    assert _event_data(ctx.sql, "student.list_imported") == [
        ("bootstrap", {"offering_id": offering["offering_id"], **counts})]


def _case_student_invites_prepared(ctx: _FailClosedCtx) -> None:
    offering, _ = _offering_with_bot(ctx, active=True)
    _import_student(ctx, offering)

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{offering['code']}/invites/prepare", json={},
                               headers=auth(ADMIN_TOKEN))

    _student_link_api_fails_closed(ctx, "student.invites_prepared", "POST", "/v1/offerings/{ref}/invites/prepare",
                         call)
    assert _fingerprint(ctx.sql)["offerings_invite_rows"] == [(None, None, None, None)]

    # Controle: sem o gatilho, o preparo devolve o código, guardado só pelo resumo.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    prepared = ok.json()
    assert_matches("invites.prepare_send", 200, prepared)
    code = prepared["confirmation_code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", code)
    expires = ctx.clock.now() + timedelta(minutes=10)
    assert prepared == {"offering": _offering_ref(offering), "eligible": 1, "valid_hours": INVITE_VALID_HOURS,
                        "confirmation_code": code, "expires_at": iso(expires)}
    assert _fingerprint(ctx.sql)["offerings_invite_rows"] == [
        (_sha256_hex(code), expires, INVITE_VALID_HOURS, 1)]
    assert _event_data(ctx.sql, "student.invites_prepared") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "eligible": 1,
                       "valid_hours": INVITE_VALID_HOURS, "expires_at": iso(expires)})]


def _case_student_invites_queued(ctx: _FailClosedCtx) -> None:
    offering, _ = _offering_with_bot(ctx, active=True)
    _import_student(ctx, offering)
    prepared = prepare_invites(ctx.client, ADMIN_TOKEN, offering["code"])
    confirmation = prepared["confirmation_code"]
    expires = ctx.clock.now() + timedelta(minutes=10)
    assert prepared == {"offering": _offering_ref(offering), "eligible": 1, "valid_hours": INVITE_VALID_HOURS,
                        "confirmation_code": confirmation, "expires_at": iso(expires)}

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{offering['code']}/invites",
                               json={"confirmation_code": confirmation}, headers=auth(ADMIN_TOKEN))

    _student_link_api_fails_closed(ctx, "student.invites_queued", "POST", "/v1/offerings/{ref}/invites", call)
    # O código de confirmação não foi consumido, e nada foi enfileirado.
    failed = _fingerprint(ctx.sql)
    assert (failed["offerings_invite_rows"], failed["invite_sends_rows"],
            failed["invite_email_tasks_rows"]) == (
        [(_sha256_hex(confirmation), expires, INVITE_VALID_HOURS, 1)], [], [])

    # Controle: sem o gatilho, o mesmo código (ainda válido) confirma e enfileira o envio.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("invites.send", 200, ok.json())
    assert ok.json() == {"offering": _offering_ref(offering), "queued": 1, "valid_hours": INVITE_VALID_HOURS}
    final = _fingerprint(ctx.sql)
    assert (final["offerings_invite_rows"], final["invite_sends_rows"], final["invite_email_tasks_rows"],
            final["enrollments_terms_rows"], final["invites_rows"]) == (
        [(None, None, None, None)], [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 0, None)],
        [("pending",)], [("pending", None, None)], [])
    [send_id] = _send_ids(ctx)
    assert _rows(ctx.sql, "SELECT created_at FROM invite_sends") == [(ctx.clock.now(),)]
    assert _rows(ctx.sql, "SELECT payload, idempotency_key, attempts, available_at FROM tasks "
                          "WHERE task_type = 'invite.email'") == [
        ({"invite_send_id": send_id}, f"invite.email:{send_id}", 0, ctx.clock.now())]
    assert _event_data(ctx.sql, "student.invites_queued") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "origin": "batch", "queued": 1})]


def _case_student_invite_cancelled(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    activated_at = ctx.clock.now()
    _drain_profile_task(ctx)
    person = _import_student(ctx, offering)
    _queue_batch(ctx, offering)
    # O oferecimento é encerrado antes de a tarefa rodar: a T1 cancela o envio (`offering_inactive`).
    # O perfil do bot fica `pending` (a tarefa do perfil foi consumida sem efeito).
    ctx.clock.advance(60)
    closed = _post_ok(ctx, "offerings.close", f"/v1/offerings/{offering['code']}/close")
    assert closed == {**offering, "state": "closed", "activated_at": iso(activated_at),
                      "closed_at": iso(ctx.clock.now()), "bot": _bot_summary(ctx, bot)}
    before = _fingerprint(ctx.sql)
    assert (before["invite_sends_rows"], before["invite_email_tasks_rows"], before["invites_rows"]) == (
        [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 0, None)], [("pending",)], [])
    _install_commit_failure(ctx.sql, "student.invite_cancelled")
    with capture_logs() as captured:
        _start_mail_worker(ctx)
        task = _nth_task_attempted(ctx, captured, "invite.email", 1)
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    # A T1 voltou atrás inteira: o envio continua `pending`, sem código, e nenhum e-mail saiu.
    assert after == before, "o cancelamento ficou gravado sem o evento"
    assert _invite_email_records(captured) == []
    assert ctx.smtp.connections() == []
    _assert_no_student_identity(captured.text)

    # Controle: sem o gatilho, depois da espera, a nova tentativa cancela o envio, com o evento.
    failed_at = ctx.clock.now()
    retried_at = ctx.clock.advance(5)
    with capture_logs() as control:
        _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'invite.email'",
                   lambda rows: rows == [("done",)], "o cancelamento não terminou em 10 s")
    [send_id] = _send_ids(ctx)
    assert _invite_email_records(control) == [(send_id, "cancelled")]
    assert _fingerprint(ctx.sql) == {
        **before, "events": before["events"] + 1, "invite_email_tasks_rows": [("done",)],
        "invite_sends_rows": [("batch", INVITE_VALID_HOURS, "cancelled", None, "offering_inactive", None, 0,
                               retried_at)]}
    assert _task_row(ctx.sql, task[0]) == _done_task(
        failed_at + timedelta(seconds=ctx.settings.backoff_base_seconds), retried_at)
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == [
        ("student.invite_cancelled", None, {"offering_id": offering["offering_id"],
                                            "person": person["pseudo_id"], "origin": "batch",
                                            "cancel_reason": "offering_inactive"})]
    assert ctx.smtp.connections() == []
    _assert_no_student_identity(control.text)
    ctx.worker_thread.stop()


def _case_student_invite_revoked(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    _drain_profile_task(ctx)
    person = _import_student(ctx, offering)
    _queue_batch(ctx, offering)
    batch_at = ctx.clock.now()
    # O envio do lote sai (sem gatilho): o convite dele fica pendente.
    sent_from = datetime.now(timezone.utc)
    _start_mail_worker(ctx)
    _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'invite.email'",
               lambda rows: rows == [("done",)], "o envio do lote não terminou em 10 s")
    sent_until = datetime.now(timezone.utc)
    ctx.worker_thread.stop()
    [first] = ctx.smtp.messages()
    [first_link] = ctx.smtp.invite_links()
    first_digest = _assert_invite_email(ctx, first, first_link, bot=bot,
                                        expires=batch_at + timedelta(hours=INVITE_VALID_HOURS),
                                        sent_from=sent_from, sent_until=sent_until)
    assert _rows(ctx.sql, "SELECT state, code_sha256 FROM invites") == [("pending", first_digest)]

    # O reenvio pela coordenação cria o envio cuja T1 revoga o convite do lote (`replaced`).
    ctx.clock.advance(60)
    resent = resend_invites(ctx.client, ADMIN_TOKEN, offering["code"], [person["alias"]])
    assert resent == {"offering": _offering_ref(offering), "queued": 1, "valid_hours": INVITE_VALID_HOURS,
                      "not_students": [], "not_found_count": 0}
    before = _fingerprint(ctx.sql)
    assert (before["invite_sends_rows"], before["invite_email_tasks_rows"]) == (
        [("batch", INVITE_VALID_HOURS, "sent", None, None, None, 1, batch_at),
         ("coordination", INVITE_VALID_HOURS, "pending", None, None, None, 0, None)],
        [("done",), ("pending",)])
    _install_commit_failure(ctx.sql, "student.invite_revoked")
    with capture_logs() as captured:
        _start_mail_worker(ctx)
        task = _nth_task_attempted(ctx, captured, "invite.email", 2)
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    # A T1 voltou atrás inteira: o convite do lote continua pendente, sem código novo nem e-mail.
    assert after == before, "a revogação ficou gravada sem o evento"
    assert _rows(ctx.sql, "SELECT state, code_sha256 FROM invites") == [("pending", first_digest)]
    assert _invite_email_records(captured) == []
    assert (len(ctx.smtp.connections()), len(ctx.smtp.messages())) == (1, 1)
    assert find_invite_codes(captured.text, [first_digest]) == []
    _assert_no_student_identity(captured.text)

    # Controle: sem o gatilho, depois da espera, a nova tentativa revoga o convite do lote, gera outro
    # código e manda o e-mail; só o link do último código vale.
    failed_at = ctx.clock.now()
    retried_at = ctx.clock.advance(5)
    sent_from = datetime.now(timezone.utc)
    with capture_logs() as control:
        _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'invite.email' ORDER BY id",
                   lambda rows: rows == [("done",), ("done",)], "o reenvio não terminou em 10 s")
    sent_until = datetime.now(timezone.utc)
    batch_id, resend_id = _send_ids(ctx)
    assert _invite_email_records(control) == [(resend_id, "sent")]
    first_again, second = ctx.smtp.messages()
    assert first_again == first
    _, second_link = ctx.smtp.invite_links()
    second_digest = _assert_invite_email(ctx, second, second_link, bot=bot,
                                         expires=retried_at + timedelta(hours=INVITE_VALID_HOURS),
                                         sent_from=sent_from, sent_until=sent_until)
    assert second_digest != first_digest
    assert _rows(ctx.sql, "SELECT state, code_sha256, expires_at FROM invites ORDER BY id") == [
        ("revoked", first_digest, batch_at + timedelta(hours=INVITE_VALID_HOURS)),
        ("pending", second_digest, retried_at + timedelta(hours=INVITE_VALID_HOURS))]
    final = _fingerprint(ctx.sql)
    assert (final["invite_sends_rows"], final["invites_presentation_rows"],
            final["invite_email_tasks_rows"]) == (
        [("batch", INVITE_VALID_HOURS, "sent", None, None, None, 1, batch_at),
         ("coordination", INVITE_VALID_HOURS, "sent", None, None, None, 1, retried_at)],
        [(1, None, None), (2, None, None)], [("done",), ("done",)])
    assert _task_row(ctx.sql, task[0]) == _done_task(
        failed_at + timedelta(seconds=ctx.settings.backoff_base_seconds), retried_at)
    ref = {"offering_id": offering["offering_id"], "person": person["pseudo_id"]}
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == [
        ("student.invite_sent", None, {**ref, "origin": "batch",
                                       "expires_at": iso(batch_at + timedelta(hours=INVITE_VALID_HOURS))}),
        ("student.invite_revoked", None, {**ref, "origin": "batch", "revoke_reason": "replaced"}),
        ("student.invite_sent", None, {**ref, "origin": "coordination",
                                       "expires_at": iso(retried_at + timedelta(hours=INVITE_VALID_HOURS))})]
    assert find_invite_codes(control.text, [first_digest, second_digest]) == []
    _assert_no_student_identity(control.text)
    ctx.worker_thread.stop()


def _case_student_invite_sent(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    _drain_profile_task(ctx)
    person = _import_student(ctx, offering)
    _queue_batch(ctx, offering)
    first_at = ctx.clock.now()
    # A validade no formato dos textos fixos (ex.: `2026-10-03 14:00 -03 (America/Sao_Paulo)`).
    assert _validity(first_at + timedelta(hours=INVITE_VALID_HOURS)) == "2031-01-07 21:00 -03 (America/Sao_Paulo)"
    before = _fingerprint(ctx.sql)
    assert (before["invite_sends_rows"], before["invite_email_tasks_rows"], before["invites_rows"]) == (
        [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 0, None)], [("pending",)], [])
    _install_commit_failure(ctx.sql, "student.invite_sent")
    sent_from = datetime.now(timezone.utc)
    with capture_logs() as captured:
        _start_mail_worker(ctx)
        task = _nth_task_attempted(ctx, captured, "invite.email", 1)
    sent_until = datetime.now(timezone.utc)
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    # O e-mail saiu (entre a T1 e a T2); o código dele entra em `remember_secret` antes da varredura.
    [first] = ctx.smtp.messages()
    [first_link] = ctx.smtp.invite_links()
    first_digest = _assert_invite_email(ctx, first, first_link, bot=bot,
                                        expires=first_at + timedelta(hours=INVITE_VALID_HOURS),
                                        sent_from=sent_from, sent_until=sent_until)
    _assert_handler_failed(ctx, captured, task)
    # A T1 ficou gravada (o convite pendente, 1 código gerado); a T2 voltou atrás (o envio `pending`).
    assert after == {**before, "invites": 1, "invites_rows": [("pending",)],
                     "invites_presentation_rows": [(1, None, None)],
                     "invite_sends_rows": [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 1, None)]}
    assert _rows(ctx.sql, "SELECT code_sha256, expires_at FROM invites") == [
        (first_digest, first_at + timedelta(hours=INVITE_VALID_HOURS))]
    assert _invite_email_records(captured) == []
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == []
    assert find_invite_codes(captured.text, [first_digest]) == []
    _assert_no_student_identity(captured.text)

    # Controle: sem o gatilho, depois da espera, a nova tentativa revoga o convite anterior
    # (`replaced`), gera outro código e manda outro e-mail: o aluno recebe dois, e só o último vale.
    failed_at = ctx.clock.now()
    retried_at = ctx.clock.advance(5)
    sent_from = datetime.now(timezone.utc)
    with capture_logs() as control:
        _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'invite.email'",
                   lambda rows: rows == [("done",)], "a nova tentativa não terminou em 10 s")
    sent_until = datetime.now(timezone.utc)
    [send_id] = _send_ids(ctx)
    assert _invite_email_records(control) == [(send_id, "sent")]
    first_again, second = ctx.smtp.messages()
    assert first_again == first
    _, second_link = ctx.smtp.invite_links()
    second_digest = _assert_invite_email(ctx, second, second_link, bot=bot,
                                         expires=retried_at + timedelta(hours=INVITE_VALID_HOURS),
                                         sent_from=sent_from, sent_until=sent_until)
    assert second_digest != first_digest
    assert _rows(ctx.sql, "SELECT state, code_sha256, expires_at FROM invites ORDER BY id") == [
        ("revoked", first_digest, first_at + timedelta(hours=INVITE_VALID_HOURS)),
        ("pending", second_digest, retried_at + timedelta(hours=INVITE_VALID_HOURS))]
    assert _fingerprint(ctx.sql) == {
        **after, "events": after["events"] + 2, "invites": 2, "invites_rows": [("revoked",), ("pending",)],
        "invites_presentation_rows": [(1, None, None), (1, None, None)],
        "invite_sends_rows": [("batch", INVITE_VALID_HOURS, "sent", None, None, None, 2, retried_at)],
        "invite_email_tasks_rows": [("done",)]}
    assert _task_row(ctx.sql, task[0]) == _done_task(
        failed_at + timedelta(seconds=ctx.settings.backoff_base_seconds), retried_at)
    ref = {"offering_id": offering["offering_id"], "person": person["pseudo_id"]}
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == [
        ("student.invite_revoked", None, {**ref, "origin": "batch", "revoke_reason": "replaced"}),
        ("student.invite_sent", None, {**ref, "origin": "batch",
                                       "expires_at": iso(retried_at + timedelta(hours=INVITE_VALID_HOURS))})]
    assert find_invite_codes(control.text, [first_digest, second_digest]) == []
    _assert_no_student_identity(control.text)
    ctx.worker_thread.stop()


def _case_student_invite_failed(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    _drain_profile_task(ctx)
    person = _import_student(ctx, offering)
    _queue_batch(ctx, offering)
    # Todo RCPT recusado com 550: `PermanentFailure("recipient", "SMTPRecipientsRefused")` na T2
    # (a falha permanente do destinatário).
    ctx.smtp.fail("rcpt", 550, times=None)
    before = _fingerprint(ctx.sql)
    assert (before["invite_sends_rows"], before["invite_email_tasks_rows"], before["invites_rows"]) == (
        [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 0, None)], [("pending",)], [])
    _install_commit_failure(ctx.sql, "student.invite_failed")
    with capture_logs() as captured:
        _start_mail_worker(ctx)
        task = _nth_task_attempted(ctx, captured, "invite.email", 1)
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    # A T1 ficou gravada (convite pendente, `attempts = 1`); a T2 voltou atrás: o envio continua
    # `pending`, e o convite desta tentativa não foi revogado.
    assert after == {**before, "invites": 1, "invites_rows": [("pending",)],
                     "invites_presentation_rows": [(1, None, None)],
                     "invite_sends_rows": [("batch", INVITE_VALID_HOURS, "pending", None, None, None, 1, None)]}
    assert _invite_email_records(captured) == []
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == []
    assert _rcpt_codes(ctx) == [[550]]
    assert ctx.smtp.messages() == []
    digests = [row[0] for row in _rows(ctx.sql, "SELECT code_sha256 FROM invites")]
    assert find_invite_codes(captured.text, digests) == []
    _assert_no_student_identity(captured.text)

    # Controle: sem o gatilho, depois da espera, a nova tentativa revoga o convite anterior
    # (`replaced`), gera outro código, tem o RCPT recusado de novo e grava `failed`/`recipient_refused`
    # (a revogação do convite desta tentativa é coberta pelo `student.invite_failed`).
    failed_at = ctx.clock.now()
    retried_at = ctx.clock.advance(5)
    with capture_logs() as control:
        _wait_rows(ctx, "SELECT state FROM tasks WHERE task_type = 'invite.email'",
                   lambda rows: rows == [("failed",)], "a nova tentativa não terminou em 10 s")
    [send_id] = _send_ids(ctx)
    assert _invite_email_records(control) == [(send_id, "failed")]
    assert _rcpt_codes(ctx) == [[550], [550]]
    assert ctx.smtp.messages() == []
    assert _fingerprint(ctx.sql) == {
        **after, "events": after["events"] + 3, "invites": 2, "invites_rows": [("revoked",), ("revoked",)],
        "invites_presentation_rows": [(1, None, None), (1, None, None)],
        "invite_sends_rows": [("batch", INVITE_VALID_HOURS, "failed", "recipient_refused", None,
                               "SMTPRecipientsRefused", 2, retried_at)],
        "invite_email_tasks_rows": [("failed",)]}
    assert _task_row(ctx.sql, task[0]) == {
        "state": "failed", "attempts": 2,
        "available_at": failed_at + timedelta(seconds=ctx.settings.backoff_base_seconds),
        "lease_owner": None, "lease_until": None, "last_error": "PermanentTaskError",
        "finished_at": retried_at}
    ref = {"offering_id": offering["offering_id"], "person": person["pseudo_id"]}
    assert _rows(ctx.sql, _INVITE_TASK_EVENTS_SQL) == [
        ("student.invite_revoked", None, {**ref, "origin": "batch", "revoke_reason": "replaced"}),
        ("student.invite_failed", None, {**ref, "origin": "batch", "failure": "recipient_refused"}),
        ("task.failed", None, {"task_id": task[0], "task_type": "invite.email", "attempts": 2,
                               "error": "PermanentTaskError"})]
    digests = [row[0] for row in _rows(ctx.sql, "SELECT code_sha256 FROM invites ORDER BY id")]
    assert len(set(digests)) == 2
    assert find_invite_codes(control.text, digests) == []
    _assert_no_student_identity(control.text)
    ctx.worker_thread.stop()


def _case_student_joined(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    person = _import_student(ctx, offering)
    # A conta do Telegram do aluno A: a varredura dos logs tem o id e o perfil como alvo.
    sender = INVITED_STUDENT.accounts[0]
    ctx.bot_worker_thread(verification=ctx.mail_channel)
    _wait_bot_ready(ctx, bot)
    # O convite sai pelo lote (sem gatilho), e o `/start` do link apresenta o termo.
    invited_at = ctx.clock.now()
    sent_from = datetime.now(timezone.utc)
    _queue_batch(ctx, offering)
    _wait_rows(ctx, "SELECT state FROM invite_sends", lambda rows: rows == [("sent",)],
               "o convite não foi enviado em 10 s")
    sent_until = datetime.now(timezone.utc)
    [message] = ctx.smtp.messages()
    [link] = ctx.smtp.invite_links()
    _assert_invite_email(ctx, message, link, bot=bot, expires=invited_at + timedelta(hours=INVITE_VALID_HOURS),
                         sent_from=sent_from, sent_until=sent_until)
    ctx.bot_api.push_start(bot, sender, link.code)
    _wait_rows(ctx, "SELECT state FROM outbox_messages WHERE bot_id IS NOT NULL",
               lambda rows: rows == [("sent",)], "o termo não foi entregue em 10 s")
    assert [(m.chat_id, m.text) for m in ctx.bot_api.sent_messages(bot)] == [(sender.user_id, LINK_TERMS)]
    before = _fingerprint(ctx.sql)
    assert (before["enrollments_rows"], before["invites_rows"], before["delivery_endpoints"],
            before["telegram_updates_rows"], before["enrollments_terms_rows"],
            before["invites_presentation_rows"]) == (
        [("pending",)], [("pending",)], 0, [("processed", "student_terms_presented")],
        [("pending", None, None)], [(1, 1, 1)])
    _install_commit_failure(ctx.sql, "student.joined")
    with capture_logs() as captured:
        ctx.bot_api.push_text(bot, sender, "/aceito", bot_command=True)
        task = _nth_task_attempted(ctx, captured, "telegram.update", 2)
    after = _fingerprint(ctx.sql)
    _remove_commit_failure(ctx.sql)

    _assert_handler_failed(ctx, captured, task)
    # Transação única: convite e matrícula pendentes, sem termo aceito, sem endereço, sem
    # boas-vindas e o update pendente.
    assert after == {**before, "telegram_updates": 2,
                     "telegram_updates_rows": [*before["telegram_updates_rows"], ("pending", None)],
                     "tasks_rows": [*before["tasks_rows"], ("telegram.update", "pending")]}
    assert [(m.chat_id, m.text) for m in ctx.bot_api.sent_messages(bot)] == [(sender.user_id, LINK_TERMS)]
    _assert_no_student_identity(captured.text)

    # Controle: sem o gatilho, depois da espera, a nova tentativa faz o vínculo e dá boas-vindas.
    accepted_at = ctx.clock.advance(5)
    _wait_rows(ctx, "SELECT state FROM outbox_messages WHERE bot_id IS NOT NULL ORDER BY id",
               lambda rows: rows == [("sent",), ("sent",)], "as boas-vindas não foram entregues em 10 s")
    assert [(m.chat_id, m.text) for m in ctx.bot_api.sent_messages(bot)] == [
        (sender.user_id, LINK_TERMS), (sender.user_id, LINK_WELCOME_STUDENT)]
    final = _fingerprint(ctx.sql)
    assert (final["invites_rows"], final["enrollments_rows"], final["delivery_endpoints_rows"],
            final["telegram_updates_rows"], final["enrollments_terms_rows"],
            final["invites_presentation_rows"]) == (
        [("used",)], [("active",)], [("active",)],
        [("processed", "student_terms_presented"), ("processed", "student_joined")],
        [("active", 1, accepted_at)], [(1, 1, 1)])
    assert _event_data(ctx.sql, "student.joined") == [
        (None, {"offering_id": offering["offering_id"], "person": person["pseudo_id"], "terms_version": 1,
                "endpoint_replaced": False})]
    ctx.bot_worker_thread.stop()


# --- falha fechada do disparo e da resposta -------------------------------------------------------

# A questão dos casos do disparo: o preset `number` do motor falso (um campo `number`, resposta certa 6,5),
# num repositório do host permitido de `TEST_REPO_SETTINGS`; a árvore `course` da obtenção falsa tem o
# QID de mesmo nome. Os disparos são imediatos, com o fechamento informado (10 min depois do preparo).
DISPATCH_REPO = "https://repos.cq.test/turma/questoes-falha-fechada.git"
DISPATCH_COMMIT = "fa11c105ed" * 4
DISPATCH_QID = "number"
DISPATCH_QUESTION = {"repo": DISPATCH_REPO, "commit": DISPATCH_COMMIT, "qid": DISPATCH_QID}
DISPATCH_ANSWER = "6.5"
DISPATCH_ORIGIN = "integration"
DISPATCH_WINDOW = timedelta(minutes=10)
# Prazo do adaptador do motor: dentro do invariante `timeout + 15 <= lease_seconds` (30 s).
DISPATCH_ENGINE_TIMEOUT = 10.0
# `scheduler.CLOSE_RECHECK_SECONDS`: a rodada seguinte do fechamento sem efeito.
DISPATCH_CLOSE_RECHECK_SECONDS = 5
# As 8 chaves de `counts` do `dispatch.closed`.
DISPATCH_ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
                  "no_answer": 0, "answered_late": 0, "question_failed": 0}
# O dado do botão "Não sei" (ação `n`; `cq_server.buttons`).
DONT_KNOW_DATA = re.compile(r"cq1:[A-Za-z0-9_-]{22}:n:[0-9a-z]{1,2}:[0-9a-z]{1,4}")
# A mensagem com os controles e o pedido do campo, na ordem da fila.
DISPATCH_SENT_MESSAGES = [("question", "send", "sent"), ("field_prompt", "send", "sent")]


@dataclass
class _DispatchEnv:
    """Os dublês do disparo e o adaptador real do motor sobre o motor falso."""

    rich: Any
    engine_fake: Any
    fetcher: Any
    engine: Any


@dataclass
class _DispatchStudent:
    person: dict
    user: Any


@dataclass
class _DispatchOpen:
    """Um disparo aberto com uma entrega `delivered` (a questão e o pedido do campo enviados)."""

    offering: dict
    bot: int
    student: _DispatchStudent
    dispatch: dict
    delivery_id: str


@contextlib.contextmanager
def _dispatch_env(ctx: _FailClosedCtx) -> Any:
    """Pede os dublês do disparo ao `request` do teste, programa a questão e monta o `HttpQuestionEngine`;
    no fim, para o worker e fecha o adaptador (nessa ordem)."""
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.config import EngineSettings

    rich = ctx.request.getfixturevalue("rich_bot_api")
    engine_fake = ctx.request.getfixturevalue("engine_fake")
    fetcher = ctx.request.getfixturevalue("repo_fetcher")
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, DISPATCH_QID), DISPATCH_QID)
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                               timeout_seconds=DISPATCH_ENGINE_TIMEOUT))
    try:
        yield _DispatchEnv(rich=rich, engine_fake=engine_fake, fetcher=fetcher, engine=engine)
    finally:
        ctx.bot_worker_thread.stop()
        engine.close()


def _start_dispatch_worker(ctx: _FailClosedCtx, env: _DispatchEnv) -> None:
    """O worker com bots e os tratadores do disparo (o motor e a obtenção pelos kwargs do `run_worker`)."""
    ctx.bot_worker_thread(engine=env.engine, fetcher=env.fetcher)


def _dispatch_rows(ctx: _FailClosedCtx, query: str, params: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in ctx.sql.execute(query, params).fetchall()]


def _dispatch_wait(ctx: _FailClosedCtx, query: str, params: tuple, predicate: Callable[[list[tuple]], bool],
             message: str, timeout: float = 20.0) -> list[tuple]:
    """Espera (real, com prazo; threads do worker) que as linhas satisfaçam `predicate`."""
    deadline = time.monotonic() + timeout
    while True:
        rows = _dispatch_rows(ctx, query, params)
        if predicate(rows):
            return rows
        assert time.monotonic() < deadline, f"{message}; última leitura: {rows}"
        time.sleep(0.02)


def _dispatch_hello(ctx: _FailClosedCtx, bot: int, index: int) -> Any:
    """Um remetente neutro que já falou com o bot (senão o envio dá 400 `chat not found`); o texto é
    processado pelos bots antes do vínculo (quem não está vinculado segue o comportamento dos bots)."""
    user = make_user(index)
    ctx.bot_api.push_text(bot, user, "Olá")
    return user


def _dispatch_wait_ready(ctx: _FailClosedCtx, bots: list[int], hellos: int) -> None:
    """Os perfis configurados, a consulta de cada bot em curso e os `hellos` textos já processados,
    sem mensagem dos bots ainda na fila."""
    _dispatch_wait(ctx, "SELECT state FROM tasks WHERE task_type = 'bot.sync_profile'", (),
             lambda rows: len(rows) == len(bots) and all(row == ("done",) for row in rows),
             "os perfis dos bots não foram configurados em 20 s")
    for bot in bots:
        ctx.bot_api.wait_for(lambda bot=bot: ctx.bot_api.calls("getUpdates", bot_id=bot), timeout=10)
    _dispatch_wait(ctx, "SELECT state FROM telegram_updates", (),
             lambda rows: len(rows) == hellos and all(row == ("processed",) for row in rows),
             "os textos antes do vínculo não foram processados em 20 s")
    _dispatch_wait(ctx, "SELECT state FROM outbox_messages WHERE bot_id IS NOT NULL", (),
             lambda rows: all(row[0] not in ("pending", "leased") for row in rows),
             "as respostas dos bots não saíram em 20 s")


def _dispatch_link_student(ctx: _FailClosedCtx, offering: dict, bot: int, user: Any, name: str, *,
             endpoint_state: str = "active") -> _DispatchStudent:
    """Aluno vinculado por SQL (sem o caminho do vínculo): a pessoa pela API, a matrícula `student` ativa com
    o termo aceito e o endereço vigente (`active` ou `blocked`) no chat do remetente."""
    person = register_person(ctx.client, ADMIN_TOKEN, name=name)
    now = ctx.clock.now()
    person_row = ctx.sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                                 (uuid.UUID(person["pseudo_id"]),)).fetchone()[0]
    offering_row = ctx.sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                   (uuid.UUID(offering["offering_id"]),)).fetchone()[0]
    bot_row = ctx.sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()[0]
    ctx.sql.execute(
        "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
        "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
        (person_row, offering_row, now, now, now))
    ctx.sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
        "changed_at) VALUES (%s, %s, 'telegram', %s, %s, %s, %s)",
        (person_row, bot_row, user.user_id, endpoint_state, now, now))
    return _DispatchStudent(person=person, user=user)


def _dispatch_prepare(ctx: _FailClosedCtx, offering: dict) -> dict:
    """Disparo imediato com o fechamento informado (`DISPATCH_WINDOW`), preparado pela API (sem gatilho)."""
    dispatch = prepare_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], **DISPATCH_QUESTION,
                                closes_at=iso(ctx.clock.now() + DISPATCH_WINDOW), origin=DISPATCH_ORIGIN)
    assert (dispatch["state"], dispatch["origin"]) == ("validating", DISPATCH_ORIGIN)
    return dispatch


def _dispatch_row_of(ctx: _FailClosedCtx, dispatch: dict) -> dict[str, Any]:
    return _row(ctx.sql, f"SELECT {', '.join(DISPATCH_COLUMNS)} FROM dispatches WHERE dispatch_uuid = %s",
                (uuid.UUID(dispatch["dispatch_id"]),))


def _dispatch_wait_row(ctx: _FailClosedCtx, dispatch: dict, state: str) -> None:
    _dispatch_wait(ctx, "SELECT state FROM dispatches WHERE dispatch_uuid = %s",
             (uuid.UUID(dispatch["dispatch_id"]),), lambda rows: rows == [(state,)],
             f"o disparo não chegou a {state} em 20 s")


def _dispatch_deliveries(ctx: _FailClosedCtx, dispatch: dict) -> list[dict[str, Any]]:
    """As entregas do disparo, em ordem de `id`: o `delivery_id` e as colunas de `DELIVERY_COLUMNS`."""
    cur = ctx.sql.execute(
        f"SELECT d.delivery_uuid::text AS delivery_id, "
        f"{', '.join('d.' + column for column in DELIVERY_COLUMNS)} FROM deliveries d "
        "JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s ORDER BY d.id",
        (uuid.UUID(dispatch["dispatch_id"]),))
    names = [d.name for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _dispatch_wait_delivery(ctx: _FailClosedCtx, dispatch: dict, state: str) -> dict[str, Any]:
    _dispatch_wait(ctx, "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
                  "WHERE x.dispatch_uuid = %s", (uuid.UUID(dispatch["dispatch_id"]),),
             lambda rows: rows == [(state,)], f"a entrega não chegou a {state} em 20 s")
    [delivery] = _dispatch_deliveries(ctx, dispatch)
    return delivery


def _dispatch_messages(ctx: _FailClosedCtx, dispatch: dict) -> list[tuple]:
    """As mensagens de entrega do disparo (papel, operação, estado, tentativas), em ordem de `id`."""
    return _dispatch_rows(ctx, "SELECT m.delivery_role, m.operation, m.state, m.attempts FROM outbox_messages m "
                         "JOIN deliveries d ON d.id = m.delivery_id JOIN dispatches x ON x.id = d.dispatch_id "
                         "WHERE x.dispatch_uuid = %s ORDER BY m.id", (uuid.UUID(dispatch["dispatch_id"]),))


def _dispatch_wait_messages(ctx: _FailClosedCtx, dispatch: dict, expected: list[tuple], message: str) -> None:
    _dispatch_wait(ctx, "SELECT m.delivery_role, m.operation, m.state FROM outbox_messages m "
                  "JOIN deliveries d ON d.id = m.delivery_id JOIN dispatches x ON x.id = d.dispatch_id "
                  "WHERE x.dispatch_uuid = %s ORDER BY m.id", (uuid.UUID(dispatch["dispatch_id"]),),
             lambda rows: rows == expected, message)


def _dispatch_task_failed(ctx: _FailClosedCtx, captured: Captured, task_type: str) -> tuple:
    """Espera a tarefa do tipo que o gatilho derrubou (`pending` com `last_error` `RaiseException`) e o
    registro `task.processed` dela; devolve `(id, state, attempts, last_error, available_at)`."""
    rows = _dispatch_wait(ctx, "SELECT id, state, attempts, last_error, available_at FROM tasks "
                         "WHERE task_type = %s ORDER BY id", (task_type,),
                    lambda rows: any(row[1] == "pending" and row[3] == "RaiseException" for row in rows),
                    f"nenhuma tarefa {task_type} falhou pelo gatilho em 20 s")
    task = [row for row in rows if row[1] == "pending" and row[3] == "RaiseException"][-1]
    _wait_for(lambda: _records(captured, logger="cq_server.worker", event="task.processed", id=task[0]),
              f"sem registro task.processed da tarefa {task_type}")
    return task


def _dispatch_other_tasks(ctx: _FailClosedCtx, task_type: str, task_id: int) -> list[str]:
    """Os estados das outras tarefas do tipo (as rodadas e as continuações anteriores)."""
    return [row[0] for row in _dispatch_rows(ctx, "SELECT state FROM tasks WHERE task_type = %s AND id <> %s "
                                            "ORDER BY id", (task_type, task_id))]


def _dispatch_delivered(ctx: _FailClosedCtx, env: _DispatchEnv, index: int) -> _DispatchOpen:
    """Oferecimento com bot, aluno vinculado, disparo validado, confirmado e aberto, e a entrega
    `delivered` com a questão e o pedido do campo enviados (sem gatilho)."""
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    user = _dispatch_hello(ctx, bot, index)
    _start_dispatch_worker(ctx, env)
    _dispatch_wait_ready(ctx, [bot], hellos=1)
    student = _dispatch_link_student(ctx, offering, bot, user, f"Aluna Neutra Da Falha Fechada {index}")
    dispatch = _dispatch_prepare(ctx, offering)
    _dispatch_wait_row(ctx, dispatch, "scheduled")
    confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
    delivery = _dispatch_wait_delivery(ctx, dispatch, "delivered")
    _dispatch_wait_messages(ctx, dispatch, DISPATCH_SENT_MESSAGES, "a questão e o pedido do campo não saíram em 20 s")
    return _DispatchOpen(offering=offering, bot=bot, student=student, dispatch=dispatch,
                   delivery_id=delivery["delivery_id"])


def _dispatch_question_message(env: _DispatchEnv, opened: _DispatchOpen) -> tuple[Any, str]:
    """A mensagem com os controles no Telegram (a única com o botão "Não sei") e o dado desse botão."""
    found = [(message, button.data) for message in env.rich.messages(opened.bot, opened.student.user.user_id)
             for button in message.buttons()
             if button.data is not None and DONT_KNOW_DATA.fullmatch(button.data)]
    assert len(found) == 1, f"esperada 1 mensagem com o botão Não sei, vieram {len(found)}"
    return found[0]


def _dispatch_submission(ctx: _FailClosedCtx, opened: _DispatchOpen) -> dict[str, Any]:
    [row] = _dispatch_rows(ctx, "SELECT s.submission_uuid::text, s.outcome FROM submissions s "
                          "JOIN deliveries d ON d.id = s.delivery_id WHERE d.delivery_uuid = %s",
                     (uuid.UUID(opened.delivery_id),))
    return {"submission_id": row[0], "outcome": row[1]}


def _dispatch_event_ref(opened_or_offering: dict, dispatch: dict) -> dict[str, str]:
    """O começo comum dos eventos do disparo: o oferecimento e o disparo."""
    return {"offering_id": opened_or_offering["offering_id"], "dispatch_id": dispatch["dispatch_id"]}


def _dispatch_delivery_ref(opened: _DispatchOpen) -> dict[str, str]:
    return {**_dispatch_event_ref(opened.offering, opened.dispatch), "delivery_id": opened.delivery_id,
            "person": opened.student.person["pseudo_id"]}


_DISPATCH_CLOSE_TASKS = ("SELECT t.state, t.last_error FROM tasks t JOIN dispatches x "
                   "ON t.payload @> jsonb_build_object('dispatch_row_id', x.id) "
                   "WHERE t.task_type = 'dispatch.close' AND x.dispatch_uuid = %s ORDER BY t.id")


def _dispatch_run_close(ctx: _FailClosedCtx, dispatch: dict, closes_at: datetime, *,
                  until: Callable[[], bool]) -> None:
    """Leva o relógio além do fechamento e espera `until()`; se uma rodada `dispatch.close` do disparo
    terminar `done` sem efeito (a barreira do leitor ainda não liberou), avança
    `CLOSE_RECHECK_SECONDS` uma vez por rodada terminada, para a varredura criar a seguinte."""
    ctx.clock.set(closes_at + timedelta(seconds=1))
    deadline = time.monotonic() + 30
    seen = 0
    while not until():
        rows = _dispatch_rows(ctx, _DISPATCH_CLOSE_TASKS, (uuid.UUID(dispatch["dispatch_id"]),))
        state = _dispatch_row_of(ctx, dispatch)["state"]
        if (state == "open" and len(rows) > seen and all(row[0] in ("done", "failed") for row in rows)):
            seen = len(rows)
            ctx.clock.advance(DISPATCH_CLOSE_RECHECK_SECONDS)
        assert time.monotonic() < deadline, f"o fechamento não aconteceu em 30 s; rodadas: {rows}"
        time.sleep(0.02)


def _dispatch_close_failed(ctx: _FailClosedCtx, dispatch: dict) -> bool:
    """Uma rodada `dispatch.close` do disparo foi derrubada pelo gatilho."""
    return ("pending", "RaiseException") in _dispatch_rows(ctx, _DISPATCH_CLOSE_TASKS,
                                                     (uuid.UUID(dispatch["dispatch_id"]),))


def _case_dispatch_prepared(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    path = f"/v1/offerings/{offering['code']}/dispatches"
    body = {**DISPATCH_QUESTION, "origin": DISPATCH_ORIGIN}
    _api_fails_closed(ctx, "dispatch.prepared", "POST", "/v1/offerings/{ref}/dispatches",
                      lambda: ctx.client.post(path, json=body, headers=auth(ADMIN_TOKEN)))
    before = _fingerprint(ctx.sql)
    assert (before["question_refs_rows"], before["dispatches_rows"], before["dispatch_tasks_rows"]) == ([], [], [])

    # Controle: sem o gatilho, o preparo grava a referência, o disparo `validating`, a tarefa de
    # validação e o evento, sem chamar o motor.
    dispatch = prepare_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], **DISPATCH_QUESTION, origin=DISPATCH_ORIGIN)
    at = ctx.clock.now()
    assert {k: dispatch[k] for k in ("offering", "state", "confirmed", "origin", "opens_at", "closes_at",
                                     "created_at", "validated_at", "confirmed_at", "rejection",
                                     "cancel_reason", "preview")} == {
        "offering": _offering_ref(offering), "state": "validating", "confirmed": False,
        "origin": DISPATCH_ORIGIN, "opens_at": None, "closes_at": None, "created_at": iso(at),
        "validated_at": None, "confirmed_at": None, "rejection": None, "cancel_reason": None,
        "preview": None}
    assert {k: dispatch["question"][k] for k in ("repo", "commit", "qid")} == DISPATCH_QUESTION
    assert _fingerprint(ctx.sql) == {
        **before, "events": before["events"] + 1, "question_refs_rows": [(DISPATCH_QID, None, None)],
        "dispatches_rows": [("validating", DISPATCH_ORIGIN, True, None, None, None, None, None, None, None, None,
                             None, None)],
        "dispatch_tasks_rows": [("dispatch.validate", "pending")]}
    assert _event_data(ctx.sql, "dispatch.prepared") == [
        ("bootstrap", {**_dispatch_event_ref(offering, dispatch), "question_ref_id": dispatch["question"]["question_ref_id"],
                       "origin": DISPATCH_ORIGIN})]


def _case_dispatch_cancelled(ctx: _FailClosedCtx) -> None:
    offering, bot = _offering_with_bot(ctx, active=True)
    assert bot is not None
    dispatch = _dispatch_prepare(ctx, offering)
    before = _fingerprint(ctx.sql)
    path = f"/v1/offerings/{offering['code']}/dispatches/{dispatch['dispatch_id']}/cancel"
    _api_fails_closed(ctx, "dispatch.cancelled", "POST", "/v1/offerings/{ref}/dispatches/{dispatch_id}/cancel",
                      lambda: ctx.client.post(path, headers=auth(ADMIN_TOKEN)))
    assert _fingerprint(ctx.sql) == before
    assert before["dispatches_rows"][0][0] == "validating"

    # Controle: sem o gatilho, o cancelamento pelo operador grava o estado e o evento.
    cancelled = cancel_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
    at = ctx.clock.now()
    assert (cancelled["state"], cancelled["cancel_reason"], cancelled["cancelled_at"],
            cancelled["confirmed"]) == ("cancelled", "operator", iso(at), False)
    closes_at = ctx.clock.now() + DISPATCH_WINDOW
    assert _fingerprint(ctx.sql) == {
        **before, "events": before["events"] + 1,
        "dispatches_rows": [("cancelled", DISPATCH_ORIGIN, True, None, "operator", None, None, None, closes_at,
                             None, None, None, at)]}
    assert _event_data(ctx.sql, "dispatch.cancelled") == [
        ("bootstrap", {**_dispatch_event_ref(offering, dispatch), "cancel_reason": "operator"})]


def _case_dispatch_confirmed(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        _start_dispatch_worker(ctx, env)
        dispatch = _dispatch_prepare(ctx, offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        ctx.bot_worker_thread.stop()
        before = _fingerprint(ctx.sql)
        path = f"/v1/offerings/{offering['code']}/dispatches/{dispatch['dispatch_id']}/confirm"
        _api_fails_closed(ctx, "dispatch.confirmed", "POST",
                          "/v1/offerings/{ref}/dispatches/{dispatch_id}/confirm",
                          lambda: ctx.client.post(path, headers=auth(ADMIN_TOKEN)))
        assert _fingerprint(ctx.sql) == before
        row = _dispatch_row_of(ctx, dispatch)
        assert (row["state"], row["confirmed_at"], row["opens_at"]) == ("scheduled", None, None)

        # Controle: sem o gatilho, a confirmação grava a marca, a abertura agora, a tarefa
        # `dispatch.open` e o evento com o alvo agora (0: nenhum aluno).
        confirmed = confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
        at = ctx.clock.now()
        closes_at = iso(row["closes_at"])
        assert (confirmed["state"], confirmed["confirmed"], confirmed["confirmed_at"], confirmed["opens_at"],
                confirmed["closes_at"], confirmed["target_count"]) == (
            "scheduled", True, iso(at), iso(at), closes_at, 0)
        after = _fingerprint(ctx.sql)
        expected_row = dict(row, confirmed_at=at, opens_at=at)
        assert after == {
            **before, "events": before["events"] + 1,
            "dispatches_rows": [tuple(expected_row[column] for column in DISPATCH_COLUMNS)],
            "dispatch_tasks_rows": [*before["dispatch_tasks_rows"], ("dispatch.open", "pending")]}
        assert _event_data(ctx.sql, "dispatch.confirmed") == [
            ("bootstrap", {**_dispatch_event_ref(offering, dispatch), "target_count": 0, "opens_at": iso(at),
                           "closes_at": closes_at})]


def _case_dispatch_validated(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        _start_dispatch_worker(ctx, env)
        _dispatch_wait_ready(ctx, [bot], hellos=0)
        _install_commit_failure(ctx.sql, "dispatch.validated")
        with capture_logs() as captured:
            dispatch = _dispatch_prepare(ctx, offering)
            task = _dispatch_task_failed(ctx, captured, "dispatch.validate")
        after = _fingerprint(ctx.sql)
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O último passo voltou atrás: o disparo continua `validating`, sem prévia nem evento; os
        # passos anteriores (obtenção e inspeção) terminaram.
        row = _dispatch_row_of(ctx, dispatch)
        assert (row["state"], row["validated_at"], row["rejection"], row["cancel_reason"]) == (
            "validating", None, None, None)
        assert set(_dispatch_other_tasks(ctx, "dispatch.validate", task[0])) <= {"done"}
        assert after["question_refs_rows"] == [(DISPATCH_QID, True, ctx.clock.now())]
        assert _event_count(ctx.sql, "dispatch.validated") == 0
        assert get_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])["preview"] is None

        # Controle: sem o gatilho, depois da espera da fila, a nova tentativa retoma pelo estado e chega a
        # `scheduled`, com um único evento (alvo vazio: o aviso `empty_target`; o oferecimento no padrão,
        # desligado e sem contato, acrescenta `no_teacher_contact` por último).
        validated_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        shown = get_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
        assert (shown["state"], shown["confirmed"], shown["validated_at"], shown["preview"] is None) == (
            "scheduled", False, iso(validated_at), False)
        assert _event_data(ctx.sql, "dispatch.validated") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "target_count": 0,
                    "warnings": ["empty_target", "no_teacher_contact"]})]


def _case_dispatch_rejected(ctx: _FailClosedCtx) -> None:
    from cq_server.ports.repos import FetchNotFound

    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        env.fetcher.program(DISPATCH_REPO, DISPATCH_COMMIT, FetchNotFound("repository"))
        _start_dispatch_worker(ctx, env)
        _dispatch_wait_ready(ctx, [bot], hellos=0)
        _install_commit_failure(ctx.sql, "dispatch.rejected")
        prepared_at = ctx.clock.now()
        with capture_logs() as captured:
            dispatch = _dispatch_prepare(ctx, offering)
            task = _dispatch_task_failed(ctx, captured, "dispatch.validate")
        after = _fingerprint(ctx.sql)
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O primeiro passo (obtenção) rejeitaria o disparo: a rejeição voltou atrás.
        assert after["dispatches_rows"] == [("validating", DISPATCH_ORIGIN, True, None, None, None, None, None,
                                             prepared_at + DISPATCH_WINDOW, None, None, None, None)]
        assert _dispatch_other_tasks(ctx, "dispatch.validate", task[0]) == []
        assert _event_count(ctx.sql, "dispatch.rejected") == 0
        assert env.engine_fake.calls() == [], "o motor foi chamado sem a árvore"

        # Controle: sem o gatilho, a nova tentativa rejeita com a classe e um único evento.
        rejected_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch, "rejected")
        shown = get_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
        assert (shown["state"], shown["rejected_at"], shown["rejection"], shown["preview"]) == (
            "rejected", iso(rejected_at),
            {"reason": "repository_unreachable", "message": "Não foi possível obter o repositório da questão.",
             "details": []}, None)
        assert _event_data(ctx.sql, "dispatch.rejected") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "rejection_reason": "repository_unreachable"})]


def _case_dispatch_opened(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        _start_dispatch_worker(ctx, env)
        dispatch = _dispatch_prepare(ctx, offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        _install_commit_failure(ctx.sql, "dispatch.opened")
        with capture_logs() as captured:
            confirmed = confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
            task = _dispatch_task_failed(ctx, captured, "dispatch.open")
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # A abertura voltou atrás: o disparo continua `scheduled` e confirmado, sem entrega.
        row = _dispatch_row_of(ctx, dispatch)
        confirmed_at = ctx.clock.now()
        assert (row["state"], row["confirmed_at"], row["opens_at"], row["opened_at"]) == (
            "scheduled", confirmed_at, confirmed_at, None)
        assert confirmed["confirmed_at"] == iso(confirmed_at)
        assert _dispatch_deliveries(ctx, dispatch) == []
        assert _event_count(ctx.sql, "dispatch.opened") == 0

        # Controle: sem o gatilho, depois da espera da fila, a abertura grava o estado e um único evento
        # (alvo vazio: 0).
        opened_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch, "open")
        assert _dispatch_row_of(ctx, dispatch)["opened_at"] == opened_at
        assert _event_data(ctx.sql, "dispatch.opened") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "target_count": 0})]


def _case_dispatch_closed(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        _start_dispatch_worker(ctx, env)
        dispatch = _dispatch_prepare(ctx, offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
        _dispatch_wait_row(ctx, dispatch, "open")
        closes_at = _dispatch_row_of(ctx, dispatch)["closes_at"]
        _install_commit_failure(ctx.sql, "dispatch.closed")
        with capture_logs() as captured:
            _dispatch_run_close(ctx, dispatch, closes_at, until=lambda: _dispatch_close_failed(ctx, dispatch))
            task = _dispatch_task_failed(ctx, captured, "dispatch.close")
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O fechamento voltou atrás: o disparo continua `open`, sem `closed_at` nem evento.
        row = _dispatch_row_of(ctx, dispatch)
        assert (row["state"], row["closed_at"]) == ("open", None)
        assert set(_dispatch_other_tasks(ctx, "dispatch.close", task[0])) <= {"done"}
        assert _event_count(ctx.sql, "dispatch.closed") == 0

        # Controle: sem o gatilho, depois da espera da fila, o fechamento grava o estado e um único
        # evento (sem entregas: as 8 contagens em 0).
        closed_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch, "closed")
        assert _dispatch_row_of(ctx, dispatch)["closed_at"] == closed_at
        assert _event_data(ctx.sql, "dispatch.closed") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "trigger": "window", "counts": DISPATCH_ZERO_COUNTS})]


def _case_delivery_delivered(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        user = _dispatch_hello(ctx, bot, 511)
        _start_dispatch_worker(ctx, env)
        _dispatch_wait_ready(ctx, [bot], hellos=1)
        student = _dispatch_link_student(ctx, offering, bot, user, "Aluna Neutra Da Entrega Aceita")
        dispatch = _dispatch_prepare(ctx, offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        _install_commit_failure(ctx.sql, "delivery.delivered")
        with capture_logs() as captured:
            confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
            _wait_for(lambda: _records(captured, logger="cq_server.worker", event="exception"),
                      "o worker não registrou event=exception em 20 s", timeout=20)
            # A questão fica em posse; o pedido do campo espera por ela (ordem por entrega).
            _dispatch_wait(ctx, "SELECT m.delivery_role, m.state, m.attempts FROM outbox_messages m "
                          "WHERE m.delivery_id IS NOT NULL ORDER BY m.id", (),
                     lambda rows: rows == [("question", "leased", 1), ("field_prompt", "pending", 0)],
                     "a questão não ficou em posse em 20 s")
        _remove_commit_failure(ctx.sql)
        _assert_clean_logs(captured, exception_logger="cq_server.worker")

        # O resultado do envio voltou atrás: a entrega continua `pending` (com a variante), sem
        # `sent_at`, sem principal e sem evento; nenhum `outbox.failed`.
        [delivery] = _dispatch_deliveries(ctx, dispatch)
        prepared_at = ctx.clock.now()
        assert {k: v for k, v in delivery.items() if k != "delivery_id"} == {
            "state": "pending", "undelivered_reason": None, "failure": None, "current_field": 0,
            "render_version": 1, "controls_version": 1, "variant_at": prepared_at, "sent_at": None,
            "answered_at": None, "revealed_at": None}
        assert _dispatch_rows(ctx, "SELECT principal_message_id FROM deliveries") == [(None,)]
        assert _dispatch_rows(ctx, "SELECT state, sent_at FROM outbox_messages WHERE delivery_role = 'question'") == [
            ("leased", None)]
        assert _event_count(ctx.sql, "delivery.delivered") == 0
        assert _event_count(ctx.sql, "outbox.failed") == 0
        # O Telegram aceitou o único envio da questão (a mensagem com os controles).
        assert len([m for m in env.rich.messages(bot, user.user_id) if m.keyboard]) == 1

        # Controle: sem o gatilho, vencida a posse, a questão é retomada e enviada de novo, a
        # entrega vira `delivered` com um único evento e o pedido do campo sai depois.
        delivered_at = ctx.clock.advance(ctx.settings.lease_seconds + 1)
        _dispatch_wait_messages(ctx, dispatch, DISPATCH_SENT_MESSAGES, "a questão não foi retomada em 20 s")
        delivery = _dispatch_wait_delivery(ctx, dispatch, "delivered")
        assert (delivery["sent_at"], delivery["current_field"]) == (delivered_at, 0)
        assert _dispatch_messages(ctx, dispatch)[0] == ("question", "send", "sent", 2)
        assert _event_data(ctx.sql, "delivery.delivered") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "delivery_id": delivery["delivery_id"],
                    "person": student.person["pseudo_id"]})]
        assert _event_count(ctx.sql, "outbox.failed") == 0


def _case_delivery_not_delivered(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        # Três oferecimentos, um por caminho do evento: a abertura (endereço bloqueado), o resultado do
        # envio (403 de chat bloqueado) e o fechamento (entrega sem variante com o motor fora do ar).
        offering_a, bot_a = _offering_with_bot(ctx, active=True)
        offering_b, bot_b = _offering_with_bot(ctx, active=True)
        offering_c, bot_c = _offering_with_bot(ctx, active=True)
        assert None not in (bot_a, bot_b, bot_c)
        user_a, user_b, user_c = (_dispatch_hello(ctx, bot_a, 521), _dispatch_hello(ctx, bot_b, 522),
                                  _dispatch_hello(ctx, bot_c, 523))
        _start_dispatch_worker(ctx, env)
        _dispatch_wait_ready(ctx, [bot_a, bot_b, bot_c], hellos=3)
        student_a = _dispatch_link_student(ctx, offering_a, bot_a, user_a, "Aluna Neutra Do Endereco Bloqueado",
                             endpoint_state="blocked")
        student_b = _dispatch_link_student(ctx, offering_b, bot_b, user_b, "Aluna Neutra Do Chat Bloqueado")
        student_c = _dispatch_link_student(ctx, offering_c, bot_c, user_c, "Aluna Neutra Sem Variante")
        ctx.bot_api.block_chat(bot_b, user_b)

        # 1. Abertura: o endereço `blocked` vai direto a `not_delivered`, sem chamar o canal.
        dispatch_a = _dispatch_prepare(ctx, offering_a)
        _dispatch_wait_row(ctx, dispatch_a, "scheduled")
        _install_commit_failure(ctx.sql, "delivery.not_delivered")
        with capture_logs() as captured:
            confirm_dispatch(ctx.client, ADMIN_TOKEN, offering_a["code"], dispatch_a["dispatch_id"])
            task = _dispatch_task_failed(ctx, captured, "dispatch.open")
        _remove_commit_failure(ctx.sql)
        _assert_handler_failed(ctx, captured, task)
        assert (_dispatch_row_of(ctx, dispatch_a)["state"], _dispatch_deliveries(ctx, dispatch_a)) == ("scheduled", [])
        assert _event_count(ctx.sql, "delivery.not_delivered") == 0
        assert _event_count(ctx.sql, "dispatch.opened") == 0
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch_a, "open")
        [delivery_a] = _dispatch_deliveries(ctx, dispatch_a)
        assert {k: v for k, v in delivery_a.items() if k != "delivery_id"} == {
            "state": "not_delivered", "undelivered_reason": "endpoint_blocked", "failure": None,
            "current_field": None, "render_version": 0, "controls_version": 0, "variant_at": None,
            "sent_at": None, "answered_at": None, "revealed_at": None}
        assert _event_data(ctx.sql, "delivery.not_delivered") == [
            (None, {**_dispatch_event_ref(offering_a, dispatch_a), "delivery_id": delivery_a["delivery_id"],
                    "person": student_a.person["pseudo_id"], "undelivered_reason": "endpoint_blocked"})]
        assert _event_data(ctx.sql, "dispatch.opened") == [
            (None, {**_dispatch_event_ref(offering_a, dispatch_a), "target_count": 1})]

        # 2. Resultado do envio: o 403 do chat bloqueado leva a `not_delivered/endpoint_blocked` na
        # transação do resultado, que falha: a questão fica em posse.
        dispatch_b = _dispatch_prepare(ctx, offering_b)
        _dispatch_wait_row(ctx, dispatch_b, "scheduled")
        _install_commit_failure(ctx.sql, "delivery.not_delivered")
        with capture_logs() as captured:
            confirm_dispatch(ctx.client, ADMIN_TOKEN, offering_b["code"], dispatch_b["dispatch_id"])
            _wait_for(lambda: _records(captured, logger="cq_server.worker", event="exception"),
                      "o worker não registrou event=exception em 20 s", timeout=20)
            _dispatch_wait(ctx, "SELECT m.delivery_role, m.state, m.attempts FROM outbox_messages m "
                          "JOIN deliveries d ON d.id = m.delivery_id JOIN dispatches x ON x.id = d.dispatch_id "
                          "WHERE x.dispatch_uuid = %s ORDER BY m.id", (uuid.UUID(dispatch_b["dispatch_id"]),),
                     lambda rows: rows == [("question", "leased", 1), ("field_prompt", "pending", 0)],
                     "a questão não ficou em posse em 20 s")
        _remove_commit_failure(ctx.sql)
        _assert_clean_logs(captured, exception_logger="cq_server.worker")
        [delivery_b] = _dispatch_deliveries(ctx, dispatch_b)
        assert (delivery_b["state"], delivery_b["undelivered_reason"], delivery_b["sent_at"]) == (
            "pending", None, None)
        assert _dispatch_rows(ctx, "SELECT state FROM delivery_endpoints WHERE chat_id = %s", (user_b.user_id,)) == [
            ("active",)]
        refused = [(c.status, c.outcome) for c in ctx.bot_api.calls(bot_id=bot_b)
                   if c.method in ("sendRichMessage", "sendMessage")]
        assert refused[-1:] == [(403, "error")]
        assert _event_count(ctx.sql, "delivery.not_delivered") == 1  # só o da abertura acima
        assert _event_count(ctx.sql, "outbox.failed") == 0
        assert _event_count(ctx.sql, "endpoint.blocked") == 0
        ctx.clock.advance(ctx.settings.lease_seconds + 1)
        _dispatch_wait_messages(ctx, dispatch_b, [("question", "send", "failed"), ("field_prompt", "send", "cancelled")],
                          "a questão retomada não falhou de vez em 20 s")
        delivery_b = _dispatch_wait_delivery(ctx, dispatch_b, "not_delivered")
        assert delivery_b["undelivered_reason"] == "endpoint_blocked"
        assert _dispatch_rows(ctx, "SELECT state FROM delivery_endpoints WHERE chat_id = %s", (user_b.user_id,)) == [
            ("blocked",)]
        assert _event_data(ctx.sql, "delivery.not_delivered")[1:] == [
            (None, {**_dispatch_event_ref(offering_b, dispatch_b), "delivery_id": delivery_b["delivery_id"],
                    "person": student_b.person["pseudo_id"], "undelivered_reason": "endpoint_blocked"})]

        # 3. Fechamento: a entrega sem variante (motor fora do ar na criação da variante) vira
        # `not_delivered/engine_unavailable` na transição, que falha.
        dispatch_c = _dispatch_prepare(ctx, offering_c)
        _dispatch_wait_row(ctx, dispatch_c, "scheduled")
        env.engine_fake.fail("create_variant", error="internal_error", times=None)
        confirm_dispatch(ctx.client, ADMIN_TOKEN, offering_c["code"], dispatch_c["dispatch_id"])
        _dispatch_wait_row(ctx, dispatch_c, "open")
        _dispatch_wait(ctx, "SELECT t.state, t.attempts FROM tasks t JOIN deliveries d "
                      "ON t.payload @> jsonb_build_object('delivery_row_id', d.id) "
                      "JOIN dispatches x ON x.id = d.dispatch_id "
                      "WHERE t.task_type = 'delivery.prepare' AND x.dispatch_uuid = %s",
                 (uuid.UUID(dispatch_c["dispatch_id"]),),
                 lambda rows: len(rows) == 1 and rows[0][1] >= 1 and rows[0][0] == "pending",
                 "a criação da variante não foi tentada em 20 s")
        closes_at = _dispatch_row_of(ctx, dispatch_c)["closes_at"]
        _install_commit_failure(ctx.sql, "delivery.not_delivered")
        with capture_logs() as captured:
            _dispatch_run_close(ctx, dispatch_c, closes_at, until=lambda: _dispatch_close_failed(ctx, dispatch_c))
            task = _dispatch_task_failed(ctx, captured, "dispatch.close")
        _remove_commit_failure(ctx.sql)
        _assert_handler_failed(ctx, captured, task)
        [delivery_c] = _dispatch_deliveries(ctx, dispatch_c)
        assert (_dispatch_row_of(ctx, dispatch_c)["state"], delivery_c["state"], delivery_c["variant_at"]) == (
            "open", "pending", None)
        assert _event_count(ctx.sql, "delivery.not_delivered") == 2
        # A e B, já sem entrega `pending`, podem fechar nessa rodada (o
        # relógio passou do horário deles) sem `delivery.not_delivered`; só o fechamento de C volta atrás.
        assert [data for _, data in _event_data(ctx.sql, "dispatch.closed")
                if data["dispatch_id"] == dispatch_c["dispatch_id"]] == []
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_row(ctx, dispatch_c, "closed")
        [delivery_c] = _dispatch_deliveries(ctx, dispatch_c)
        assert (delivery_c["state"], delivery_c["undelivered_reason"]) == ("not_delivered", "engine_unavailable")
        assert _event_data(ctx.sql, "delivery.not_delivered")[2:] == [
            (None, {**_dispatch_event_ref(offering_c, dispatch_c), "delivery_id": delivery_c["delivery_id"],
                    "person": student_c.person["pseudo_id"], "undelivered_reason": "engine_unavailable"})]
        # Os disparos A e B também fecham (o relógio passou do horário deles), sem evento de entrega.
        assert [(actor, data) for actor, data in _event_data(ctx.sql, "dispatch.closed")
                if data["dispatch_id"] == dispatch_c["dispatch_id"]] == [
            (None, {**_dispatch_event_ref(offering_c, dispatch_c), "trigger": "window",
                    "counts": {**DISPATCH_ZERO_COUNTS, "not_delivered": 1}})]


def _case_delivery_question_failed(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        offering, bot = _offering_with_bot(ctx, active=True)
        assert bot is not None
        user = _dispatch_hello(ctx, bot, 531)
        _start_dispatch_worker(ctx, env)
        _dispatch_wait_ready(ctx, [bot], hellos=1)
        student = _dispatch_link_student(ctx, offering, bot, user, "Aluna Neutra Da Variante Recusada")
        dispatch = _dispatch_prepare(ctx, offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        # 400 na criação da variante da entrega → `question_failed/engine_rejected`.
        env.engine_fake.fail("create_variant", error=400, times=None)
        _install_commit_failure(ctx.sql, "delivery.question_failed")
        with capture_logs() as captured:
            confirm_dispatch(ctx.client, ADMIN_TOKEN, offering["code"], dispatch["dispatch_id"])
            task = _dispatch_task_failed(ctx, captured, "delivery.prepare")
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        [delivery] = _dispatch_deliveries(ctx, dispatch)
        assert {k: v for k, v in delivery.items() if k != "delivery_id"} == {
            "state": "pending", "undelivered_reason": None, "failure": None, "current_field": None,
            "render_version": 0, "controls_version": 0, "variant_at": None, "sent_at": None,
            "answered_at": None, "revealed_at": None}
        assert _dispatch_messages(ctx, dispatch) == []
        assert _event_count(ctx.sql, "delivery.question_failed") == 0

        # Controle: sem o gatilho, a nova tentativa grava a falha de questão e um único evento.
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        delivery = _dispatch_wait_delivery(ctx, dispatch, "question_failed")
        assert (delivery["failure"], delivery["variant_at"], delivery["current_field"]) == (
            "engine_rejected", None, None)
        assert _event_data(ctx.sql, "delivery.question_failed") == [
            (None, {**_dispatch_event_ref(offering, dispatch), "delivery_id": delivery["delivery_id"],
                    "person": student.person["pseudo_id"], "failure": "engine_rejected"})]


def _case_delivery_dont_know(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        opened = _dispatch_delivered(ctx, env, 541)
        question, data = _dispatch_question_message(env, opened)
        tapped_at = ctx.clock.now()
        before = _fingerprint(ctx.sql)
        _install_commit_failure(ctx.sql, "delivery.dont_know")
        with capture_logs() as captured:
            env.rich.tap(opened.bot, opened.student.user, question.message_id, data)
            task = _dispatch_task_failed(ctx, captured, "telegram.update")
        after = _fingerprint(ctx.sql)
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O "Não sei" voltou atrás: a entrega continua `delivered`, sem submissão, com o update
        # `pending` e sem confirmação do toque (ela sai depois do commit).
        assert after == {**before, "telegram_updates": before["telegram_updates"] + 1,
                         "telegram_updates_rows": [*before["telegram_updates_rows"], ("pending", None)],
                         "tasks_rows": [*before["tasks_rows"], ("telegram.update", "pending")]}
        assert env.rich.answers(opened.bot) == []
        assert _event_count(ctx.sql, "delivery.dont_know") == 0

        # Controle: sem o gatilho, a nova tentativa grava o "Não sei", confirma o toque e grava um único
        # evento.
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        delivery = _dispatch_wait_delivery(ctx, opened.dispatch, "dont_know")
        # `answered_at` é o `received_at` do update (gravado no toque, antes da espera).
        assert (delivery["current_field"], delivery["answered_at"]) == (None, tapped_at)
        submission = _dispatch_submission(ctx, opened)
        assert submission["outcome"] == "dont_know"
        env.rich.wait_for(lambda: env.rich.answers(opened.bot), timeout=10)
        assert len(env.rich.answers(opened.bot)) == 1
        assert _event_data(ctx.sql, "delivery.dont_know") == [
            (None, {**_dispatch_delivery_ref(opened), "submission_id": submission["submission_id"]})]


def _case_submission_graded(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        opened = _dispatch_delivered(ctx, env, 551)
        _install_commit_failure(ctx.sql, "submission.graded")
        with capture_logs() as captured:
            # A resposta certa, sem "responder": o único campo digitável aberto.
            ctx.bot_api.push_text(opened.bot, opened.student.user, DISPATCH_ANSWER)
            task = _dispatch_task_failed(ctx, captured, "submission.grade")
        after = _fingerprint(ctx.sql)
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O desfecho da correção voltou atrás: a submissão continua `grading` e a entrega `delivered`.
        assert after["submissions_rows"] == [("grading", None, None, None)]
        assert [row[0] for row in after["deliveries_rows"]] == ["delivered"]
        assert [(call.status, call.request["raw_submitted_answers"]) for call in env.engine_fake.calls("grade")] == [
            (200, {"resposta": DISPATCH_ANSWER})]
        assert _event_count(ctx.sql, "submission.graded") == 0

        # Controle: sem o gatilho, a nova tentativa corrige de novo e grava a submissão, a entrega
        # `answered` e um único evento (a razão e o resíduo da resposta exata são 1 e 0).
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait_delivery(ctx, opened.dispatch, "answered")
        submission = _dispatch_submission(ctx, opened)
        assert submission["outcome"] == "graded"
        assert len(env.engine_fake.calls("grade")) == 2
        assert _event_data(ctx.sql, "submission.graded") == [
            (None, {**_dispatch_delivery_ref(opened), "submission_id": submission["submission_id"],
                    "question_ref_id": opened.dispatch["question"]["question_ref_id"], "valid": True,
                    "late": False, "normalized": False, "score": 1.0,
                    "parts": [{"index": 0, "score": 1.0, "choices": [], "ratio": 1.0, "residual": 0.0}]})]


def _case_submission_internal_failure(ctx: _FailClosedCtx) -> None:
    with _dispatch_env(ctx) as env:
        opened = _dispatch_delivered(ctx, env, 561)
        # 400 na correção → `internal_failure`.
        env.engine_fake.fail("grade", error=400, times=None)
        _install_commit_failure(ctx.sql, "submission.internal_failure")
        with capture_logs() as captured:
            ctx.bot_api.push_text(opened.bot, opened.student.user, DISPATCH_ANSWER)
            task = _dispatch_task_failed(ctx, captured, "submission.grade")
        after = _fingerprint(ctx.sql)
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        assert after["submissions_rows"] == [("grading", None, None, None)]
        assert [row[0] for row in after["deliveries_rows"]] == ["delivered"]
        assert _event_count(ctx.sql, "submission.internal_failure") == 0

        # Controle: sem o gatilho, a nova tentativa grava a falha interna, reabre a entrega no primeiro
        # campo e grava um único evento.
        ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait(ctx, "SELECT outcome FROM submissions", (), lambda rows: rows == [("internal_failure",)],
                 "a falha interna não foi gravada em 20 s")
        [delivery] = _dispatch_deliveries(ctx, opened.dispatch)
        assert (delivery["state"], delivery["current_field"]) == ("delivered", 0)
        submission = _dispatch_submission(ctx, opened)
        assert _event_data(ctx.sql, "submission.internal_failure") == [
            (None, {**_dispatch_delivery_ref(opened), "submission_id": submission["submission_id"],
                    "engine_error": "invalid_request"})]


# --- falha fechada dos relatórios e do acompanhamento ---------------------------------------------

# O horário do resumo diário da alteração com mudança (o padrão, sem linha, é `20:00` desligado).
REPORT_DIGEST_TIME = "07:30"
# As colunas de um aviso conferidas nos casos dos relatórios.
_REPORT_NOTICE_SQL = ("SELECT kind, state, recipient_count, created_at, finished_at FROM team_notices "
                  "WHERE id = %s")


def _report_team(ctx: _FailClosedCtx, env: _DispatchEnv) -> Any:
    """Um oferecimento `active` com bot próprio e **um** membro da equipe com endereço ativo, vinculado pelo
    caminho real dos bots (`tests/helpers/team.py`: link de equipe e `/start` pela Bot API falsa), com o worker
    com bots e os dublês do disparo já no ar (e que continua no ar); devolve a `Team`."""
    offering, bot = create_team_offering(ctx.client, ctx.bot_api)
    _start_dispatch_worker(ctx, env)
    team = link_team(ctx.client, ctx.sql, offering, bot, (TEAM_ACTIVE,), bot_api=ctx.bot_api)
    assert len(team.recipients) == 1, team.recipients
    return team


def _report_row_ids(ctx: _FailClosedCtx, offering: dict, dispatch: dict) -> tuple[int, int]:
    """Os ids internos do oferecimento e do disparo (só para o registro direto do aviso; nunca esperados)."""
    offering_row = ctx.sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                                   (uuid.UUID(offering["offering_id"]),)).fetchone()[0]
    dispatch_row = ctx.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s",
                                   (uuid.UUID(dispatch["dispatch_id"]),)).fetchone()[0]
    return int(offering_row), int(dispatch_row)


def _report_versions(ctx: _FailClosedCtx, dispatch: dict) -> list[tuple]:
    """As versões congeladas do disparo (versão, gatilho, horário), em ordem de versão."""
    return _dispatch_rows(ctx, "SELECT r.version, r.trigger, r.frozen_at FROM dispatch_reports r "
                         "JOIN dispatches x ON x.id = r.dispatch_id WHERE x.dispatch_uuid = %s "
                         "ORDER BY r.version", (uuid.UUID(dispatch["dispatch_id"]),))


def _report_notices(ctx: _FailClosedCtx, kind: str) -> list[tuple]:
    """Os avisos do tipo (estado e destinatários), em ordem de `id`."""
    return _dispatch_rows(ctx, "SELECT state, recipient_count FROM team_notices WHERE kind = %s ORDER BY id", (kind,))


def _report_api_fails_closed(ctx: _FailClosedCtx, event_type: str, method: str, route: str,
                         call: Callable[[], Any]) -> None:
    """`_api_fails_closed` para um tipo que já tem eventos gravados antes da chamada (o `report.frozen` do
    congelamento): 500 exato, nada gravado (o `_fingerprint` inteiro, com o total de eventos), o número de
    eventos do tipo igual ao de antes, a resposta sem canário nem segredo e os logs limpos, com `event:
    exception` no logger da API."""
    before = _fingerprint(ctx.sql)
    count = _event_count(ctx.sql, event_type)
    _install_commit_failure(ctx.sql, event_type)
    with capture_logs() as captured:
        resp = call()
    _remove_commit_failure(ctx.sql)

    assert_error(resp, 500, "internal")
    exposed = _exposed(resp)
    assert find_canary(exposed) == [], "identidade na resposta da operação que falhou"
    assert re.search(r"cq_[A-Za-z0-9_-]{43}", exposed) is None, "segredo na resposta que falhou"
    assert _fingerprint(ctx.sql) == before, "a mudança ficou gravada sem o evento"
    assert _event_count(ctx.sql, event_type) == count
    _assert_one_request(captured, method=method, route=route, status=500)
    _assert_clean_logs(captured, exception_logger="cq_server.api")


def _case_report_frozen(ctx: _FailClosedCtx) -> None:
    """O congelamento e o reenvio do relatório: (i) o congelamento pelo worker (`report.freeze`) de um disparo fechado
    sem correção pendente e (ii) o reenvio pela API (`reports.resend_summary`) do mesmo disparo, já
    congelado, com o COMMIT do `report.frozen` falhando."""
    with _dispatch_env(ctx) as env:
        team = _report_team(ctx, env)
        dispatch = _dispatch_prepare(ctx, team.offering)
        _dispatch_wait_row(ctx, dispatch, "scheduled")
        confirm_dispatch(ctx.client, ADMIN_TOKEN, team.code, dispatch["dispatch_id"])
        _dispatch_wait_row(ctx, dispatch, "open")
        closes_at = _dispatch_row_of(ctx, dispatch)["closes_at"]

        # (i) Pelo worker: o disparo fecha (alvo vazio, nenhuma submissão em correção), e o congelamento,
        # que vence logo, falha no COMMIT do evento.
        _install_commit_failure(ctx.sql, "report.frozen")
        with capture_logs() as captured:
            _dispatch_run_close(ctx, dispatch, closes_at,
                          until=lambda: _dispatch_row_of(ctx, dispatch)["state"] == "closed")
            task = _dispatch_task_failed(ctx, captured, "report.freeze")
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # O congelamento voltou atrás inteiro: nenhuma versão, nenhum aviso `dispatch_closed` e nenhuma
        # mensagem dele na fila; o disparo continua `closed` (o estado do disparo não muda).
        assert _report_versions(ctx, dispatch) == []
        assert _count(ctx.sql, "dispatch_reports") == 0
        assert _report_notices(ctx, "dispatch_closed") == []
        assert team_messages(ctx.sql, "dispatch_closed") == []
        assert _event_count(ctx.sql, "report.frozen") == 0
        assert _dispatch_row_of(ctx, dispatch)["state"] == "closed"
        assert _dispatch_other_tasks(ctx, "report.freeze", task[0]) == []

        # Controle: sem o gatilho, depois da espera da fila, a tarefa repete e congela uma única versão
        # `closing`, com um único evento; o aviso `dispatch_closed` vai ao membro na mesma transação.
        frozen_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait(ctx, "SELECT r.version FROM dispatch_reports r JOIN dispatches x ON x.id = r.dispatch_id "
                      "WHERE x.dispatch_uuid = %s", (uuid.UUID(dispatch["dispatch_id"]),),
                 lambda rows: rows != [], "o congelamento não aconteceu em 20 s")
        assert _report_versions(ctx, dispatch) == [(1, "closing", frozen_at)]
        assert _event_data(ctx.sql, "report.frozen") == [
            (None, {**_dispatch_event_ref(team.offering, dispatch), "version": 1, "trigger": "closing"})]
        assert _report_notices(ctx, "dispatch_closed") == [("queued", 1)]
        assert [m.recipient for m in team_messages(ctx.sql, "dispatch_closed")] == team.recipients
        _dispatch_wait(ctx, "SELECT m.state FROM outbox_messages m JOIN team_notices n ON n.id = m.notice_id "
                      "WHERE n.kind = 'dispatch_closed'", (), lambda rows: rows == [("sent",)],
                 "o resumo de fechamento não saiu em 20 s")
        _dispatch_wait(ctx, "SELECT state FROM tasks WHERE task_type = 'report.freeze' ORDER BY id", (),
                 lambda rows: rows == [("done",)], "a tarefa report.freeze não terminou em 20 s")
        # O worker para antes da parte da API: nada mais muda o banco entre os dois instantâneos.
        ctx.bot_worker_thread.stop()

        # (ii) Pela API: o reenvio do resumo do disparo congelado, com o COMMIT do evento falhando.
        path = f"/v1/offerings/{team.code}/dispatches/{dispatch['dispatch_id']}/report/resend"
        _report_api_fails_closed(ctx, "report.frozen", "POST",
                             "/v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend",
                             lambda: ctx.client.post(path, headers=auth(ADMIN_TOKEN)))
        # Sem a versão 2 e sem o aviso `summary_resent`.
        assert _report_versions(ctx, dispatch) == [(1, "closing", frozen_at)]
        assert _report_notices(ctx, "summary_resent") == []
        assert _event_count(ctx.sql, "report.frozen") == 1

        # Controle: sem o gatilho, 200 com a versão 2 (`resend`), o aviso `summary_resent` registrado
        # `pending` (só a fase 1) e o evento exato com o ator.
        resent_at = ctx.clock.advance(1)
        resent = resend_report_summary(ctx.client, team.code, dispatch["dispatch_id"])
        assert resent == {"offering": _offering_ref(team.offering), "dispatch_id": dispatch["dispatch_id"],
                          "frozen": {"version": 2, "trigger": "resend", "frozen_at": iso(resent_at)}}
        assert _report_versions(ctx, dispatch) == [(1, "closing", frozen_at), (2, "resend", resent_at)]
        assert _report_notices(ctx, "summary_resent") == [("pending", 0)]
        assert _event_data(ctx.sql, "report.frozen") == [
            (None, {**_dispatch_event_ref(team.offering, dispatch), "version": 1, "trigger": "closing"}),
            ("bootstrap", {**_dispatch_event_ref(team.offering, dispatch), "version": 2, "trigger": "resend"})]


def _case_notice_recorded(ctx: _FailClosedCtx) -> None:
    """A entrega de um aviso à equipe (fase 2, tarefa `notice.send`) de um aviso de texto `dispatch_rejected`
    registrado `pending` (fase 1, `teamnotices.record`) para um disparo rejeitado na validação, com o COMMIT do
    `notice.recorded` falhando. O aviso é registrado com o worker parado, antes da rejeição, para que a regra
    de detecção (`notice.dispatch_rejected`, que faria as duas fases numa transação) encontre o aviso já
    registrado e não corra com o registro; a fotografia (`data`) é a classe da rejeição."""
    from cq_server.ports.repos import FetchNotFound
    from cq_server.teamnotices import NoticeKind, record

    with _dispatch_env(ctx) as env:
        team = _report_team(ctx, env)
        ctx.bot_worker_thread.stop()
        env.fetcher.program(DISPATCH_REPO, DISPATCH_COMMIT, FetchNotFound("repository"))
        dispatch = _dispatch_prepare(ctx, team.offering)
        offering_row, dispatch_row = _report_row_ids(ctx, team.offering, dispatch)
        recorded_at = ctx.clock.now()
        with psycopg.connect(ctx.db_url) as conn:
            notice_id = record(conn, offering_row_id=offering_row, kind=NoticeKind.DISPATCH_REJECTED,
                               subject_key=f"dispatch:{dispatch_row}",
                               data={"rejection_reason": "repository_unreachable"}, now=recorded_at,
                               dispatch_row_id=dispatch_row)
        assert isinstance(notice_id, int) and not isinstance(notice_id, bool), notice_id
        pending = ("dispatch_rejected", "pending", 0, recorded_at, None)
        assert _row(ctx.sql, _REPORT_NOTICE_SQL, (notice_id,)) == dict(zip(
            ("kind", "state", "recipient_count", "created_at", "finished_at"), pending))
        assert _event_count(ctx.sql, "notice.recorded") == 0

        _install_commit_failure(ctx.sql, "notice.recorded")
        with capture_logs() as captured:
            _start_dispatch_worker(ctx, env)
            _dispatch_wait_row(ctx, dispatch, "rejected")
            task = _dispatch_task_failed(ctx, captured, "notice.send")
        _remove_commit_failure(ctx.sql)

        _assert_handler_failed(ctx, captured, task)
        # A entrega voltou atrás inteira: o aviso continua `pending`, sem `finished_at` e sem destinatário
        # contado, nenhuma mensagem com `notice_id` na fila e nenhum outro aviso registrado.
        assert tuple(_row(ctx.sql, _REPORT_NOTICE_SQL, (notice_id,)).values()) == pending
        assert _dispatch_rows(ctx, "SELECT count(*) FROM outbox_messages WHERE notice_id IS NOT NULL") == [(0,)]
        assert _dispatch_rows(ctx, "SELECT id FROM team_notices ORDER BY id") == [(notice_id,)]
        assert _event_count(ctx.sql, "notice.recorded") == 0
        assert _dispatch_other_tasks(ctx, "notice.send", task[0]) == []

        # Controle: sem o gatilho, depois da espera da fila, a tarefa repete e o aviso fica `queued`, com uma
        # mensagem de texto por destinatário (prioridade 2) e um único `notice.recorded` exato.
        queued_at = ctx.clock.advance(ctx.settings.backoff_base_seconds)
        _dispatch_wait(ctx, "SELECT state FROM team_notices WHERE id = %s", (notice_id,),
                 lambda rows: rows == [("queued",)], "o aviso não foi enfileirado em 20 s")
        assert tuple(_row(ctx.sql, _REPORT_NOTICE_SQL, (notice_id,)).values()) == (
            "dispatch_rejected", "queued", 1, recorded_at, queued_at)
        messages = team_messages(ctx.sql, "dispatch_rejected")
        assert [(m.notice_id, m.recipient, m.role, m.media_id, m.field_index, m.priority) for m in messages] == [
            (notice_id, recipient, None, None, None, 2) for recipient in team.recipients]
        assert all(m.text and m.text_format in ("plain", "html", "rich") for m in messages), messages
        assert _event_data(ctx.sql, "notice.recorded") == [
            (None, {"offering_id": team.offering["offering_id"], "notice_type": "dispatch_rejected",
                    "dispatch_id": dispatch["dispatch_id"], "report_version": None, "episode_seq": None,
                    "batch_id": None, "window_started_at": None, "local_date": None, "state": "queued",
                    "recipient_count": 1})]


def _digest_next_occurrence(after: datetime, local_time: str, zone: str) -> datetime:
    """A primeira ocorrência de `local_time` no fuso `zone` em `after` ou depois, em UTC (o fuso dos
    testes não tem horário de verão)."""
    hour, minute = (int(part) for part in local_time.split(":"))
    local = after.astimezone(ZoneInfo(zone))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < local:
        candidate = (local + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate.astimezone(timezone.utc)


def _case_offering_digest_changed(ctx: _FailClosedCtx) -> None:
    """O resumo diário: ligar o resumo diário com outro horário (uma mudança em
    relação ao padrão, sem linha) com o COMMIT do `offering.digest_changed` falhando → 500 exato e nenhuma
    linha em `offering_digests`; sem o gatilho, 200 com o `DigestView` e o evento exato com o ator."""
    offering, _ = _offering_with_bot(ctx)
    body = {"enabled": True, "local_time": REPORT_DIGEST_TIME}

    def call() -> Any:
        return ctx.client.patch(f"/v1/offerings/{offering['code']}/digest", json=body, headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.digest_changed", "PATCH", "/v1/offerings/{ref}/digest", call)
    assert _count(ctx.sql, "offering_digests") == 0
    assert _fingerprint(ctx.sql)["offering_digests_rows"] == []

    # Controle: sem o gatilho, a mesma alteração vale (com a próxima ocorrência do horário no fuso do
    # oferecimento) e é auditada.
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("offerings.update_digest", 200, ok.json())
    assert ok.json() == {"offering": _offering_ref(offering), "timezone": offering["timezone"],
                         "enabled": True, "local_time": REPORT_DIGEST_TIME}
    now = ctx.clock.now()
    assert _fingerprint(ctx.sql)["offering_digests_rows"] == [
        (True, REPORT_DIGEST_TIME, _digest_next_occurrence(now, REPORT_DIGEST_TIME, offering["timezone"]), now)]
    assert _event_data(ctx.sql, "offering.digest_changed") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "enabled": True, "local_time": REPORT_DIGEST_TIME})]


# O expurgo: a validade do código do expurgo e o impacto zero de um oferecimento arquivado
# sem bot, pessoas nem contato (escritos aqui, e não importados, para o teste não depender da implementação).
PURGE_CODE_TTL = timedelta(minutes=10)
PURGE_ZERO_IMPACT = {"identities_to_delete": 0, "identities_kept": 0, "endpoints": 0, "updates": 0,
                  "contact_email_set": False}
PURGE_COLUMNS_SQL = ("SELECT state, purged_at, purge_result, purge_code_sha256, purge_code_expires_at, "
                        "purge_prepared_counts FROM offerings WHERE code = %s")


def _purge_archived_offering(ctx: _FailClosedCtx) -> dict:
    """um oferecimento arquivado direto do rascunho (só se arquiva a partir de `draft` ou `closed`), sem
    bot, pessoas nem contato, e portanto sem trabalho pendente; devolve o `Offering` do arquivamento."""
    offering, _ = _offering_with_bot(ctx)
    prepared = _post_ok(ctx, "offerings.prepare_archive", f"/v1/offerings/{offering['code']}/archive/prepare")
    archived = _post_ok(ctx, "offerings.archive", f"/v1/offerings/{offering['code']}/archive",
                        {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived", archived
    return archived


def _case_offering_purge_prepared(ctx: _FailClosedCtx) -> None:
    """Preparar o expurgo de um oferecimento arquivado sem
    pendência com o COMMIT do `offering.purge_prepared` falhando → 500 exato, nenhum código gravado (as três
    colunas do código nulas) e nada mais (`_fingerprint`); sem o gatilho, 200 com o corpo exato (impacto zero,
    o código de 22 caracteres e o vencimento em 10 minutos), só o resumo SHA-256 do código guardado e o evento
    exato com o ator."""
    offering = _purge_archived_offering(ctx)
    code = offering["code"]

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{code}/purge/prepare", headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.purge_prepared", "POST", "/v1/offerings/{ref}/purge/prepare", call)
    assert _row(ctx.sql, PURGE_COLUMNS_SQL, (code,)) == {
        "state": "archived", "purged_at": None, "purge_result": None, "purge_code_sha256": None,
        "purge_code_expires_at": None, "purge_prepared_counts": None}

    # Controle: sem o gatilho, a mesma preparação devolve o código, guardado só pelo resumo.
    ctx.clock.advance(30)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    prepared = ok.json()
    assert_matches("offerings.prepare_purge", 200, prepared)
    confirmation = prepared["confirmation_code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", confirmation)
    expires = ctx.clock.now() + PURGE_CODE_TTL
    assert prepared == {"offering": _offering_ref(offering), "impact": PURGE_ZERO_IMPACT,
                        "confirmation_code": confirmation, "expires_at": iso(expires)}
    assert _row(ctx.sql, PURGE_COLUMNS_SQL, (code,)) == {
        "state": "archived", "purged_at": None, "purge_result": None,
        "purge_code_sha256": _sha256_hex(confirmation), "purge_code_expires_at": expires,
        "purge_prepared_counts": PURGE_ZERO_IMPACT}
    assert _event_data(ctx.sql, "offering.purge_prepared") == [
        ("bootstrap", {"offering_id": offering["offering_id"], "expires_at": iso(expires), **PURGE_ZERO_IMPACT})]


def _case_offering_purged(ctx: _FailClosedCtx) -> None:
    """Confirmar o expurgo do cenário da
    semente (pessoas a apagar e a manter, endereços, updates, mensagens livres, fila com o contato e o contato
    vigente) com o código válido e o COMMIT do `offering.purged` falhando → 500 exato e **nada** apagado: as seis
    tabelas do expurgo e `offerings` iguais (`snapshot_purge_tables`), `purged_at` nulo e o código
    preparado ainda guardado; sem o gatilho, o **mesmo** código confirma: 200 com o resultado exato da semente
    (`scenario.outcome`), as colunas do expurgo gravadas e um único `offering.purged` exato."""
    import dataclasses

    from tests.helpers.purge import seed_purge_scenario, snapshot_purge_tables

    scenario = seed_purge_scenario(ctx.client, ctx.sql)
    prepared = _post_ok(ctx, "offerings.prepare_purge", f"/v1/offerings/{scenario.code}/purge/prepare")
    confirmation = prepared["confirmation_code"]
    expires = ctx.clock.now() + PURGE_CODE_TTL
    impact = dataclasses.asdict(scenario.impact)
    stored = {"state": "archived", "purged_at": None, "purge_result": None,
              "purge_code_sha256": _sha256_hex(confirmation), "purge_code_expires_at": expires,
              "purge_prepared_counts": impact}
    assert _row(ctx.sql, PURGE_COLUMNS_SQL, (scenario.code,)) == stored
    before = snapshot_purge_tables(ctx.sql)

    def call() -> Any:
        return ctx.client.post(f"/v1/offerings/{scenario.code}/purge", json={"confirmation_code": confirmation},
                               headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "offering.purged", "POST", "/v1/offerings/{ref}/purge", call)
    assert snapshot_purge_tables(ctx.sql) == before, "o expurgo apagou algo sem o evento"
    assert _row(ctx.sql, PURGE_COLUMNS_SQL, (scenario.code,)) == stored

    # Controle: sem o gatilho, o mesmo código (ainda válido) confirma o expurgo, auditado uma única vez.
    ctx.clock.advance(60)
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    purged = ok.json()
    assert_matches("offerings.purge", 200, purged)
    outcome = dataclasses.asdict(scenario.outcome)
    now = ctx.clock.now()
    assert purged == {"offering": {"offering_id": scenario.offering_id, "code": scenario.code},
                      "purged_at": iso(now), "result": outcome}
    assert _row(ctx.sql, PURGE_COLUMNS_SQL, (scenario.code,)) == {
        "state": "archived", "purged_at": now, "purge_result": outcome, "purge_code_sha256": None,
        "purge_code_expires_at": None, "purge_prepared_counts": None}
    assert _event_data(ctx.sql, "offering.purged") == [
        ("bootstrap", {"offering_id": scenario.offering_id, **outcome})]


def _case_bank_uploaded(ctx: _FailClosedCtx) -> None:
    """O banco de questões local: o envio de um banco novo (o curso de fixture, empacotado à mão) com o COMMIT do
    `bank.uploaded` falhando → 500 exato e nenhuma linha em `question_banks`; sem o gatilho, o mesmo envio responde
    200 com `created: true`, uma linha e o evento exato com o ator. No esqueleto, a rota lança antes de gravar, e o
    controle recebe 500."""
    from tests.helpers import banks as bank_fixtures

    files = bank_fixtures.course_files()
    version = bank_fixtures.reference_version(files)
    body = bank_fixtures.upload_body("banco-falha-fechada", version, "folder", None,
                                     bank_fixtures.archive_of(files))

    def call() -> Any:
        return ctx.client.post("/v1/question-banks", json=body, headers=auth(ADMIN_TOKEN))

    _api_fails_closed(ctx, "bank.uploaded", "POST", "/v1/question-banks", call)
    assert _count(ctx.sql, "question_banks") == 0

    # Controle: sem o gatilho, o mesmo envio guarda o banco e é auditado.
    ok = call()
    assert ok.status_code == 200, ok.text[:300]
    assert_matches("banks.upload", 200, ok.json())
    counts = {"file_count": len(files), "byte_count": sum(len(data) for data in files.values()),
              "question_count": bank_fixtures.reference_question_count(files)}
    assert ok.json() == {"name": "banco-falha-fechada", "version": version, "repo": "bank:banco-falha-fechada",
                         "source": "folder", "git_commit": None, **counts, "created_at": iso(ctx.clock.now()),
                         "created": True}
    assert _count(ctx.sql, "question_banks") == 1
    assert _event_data(ctx.sql, "bank.uploaded") == [
        ("bootstrap", {"bank": "banco-falha-fechada", "version": version, "source": "folder", "git_commit": None,
                       **counts})]


FAIL_CLOSED_CASES: dict[str, Callable[[_FailClosedCtx], None]] = {
    "identity.revealed": _case_identity_revealed,
    "identity.reveal_denied": _case_identity_reveal_denied,
    "person.registered": _case_person_registered,
    "token.created": _case_token_created,
    "token.revoked": _case_token_revoked,
    "task.failed": _case_task_failed,
    "outbox.failed": _case_outbox_failed,
    # Oferecimentos e bots
    "offering.created": _case_offering_created,
    "offering.state_changed": _case_offering_state_changed,
    "offering.archive_prepared": _case_offering_archive_prepared,
    "bot.registered": _case_bot_registered,
    "bot.token_replaced": _case_bot_token_replaced,
    "bot.profile_updated": _case_bot_profile_updated,
    "bot.lease_changed": _case_bot_lease_changed,
    "bot.access_changed": _case_bot_access_changed,
    "team.link_created": _case_team_link_created,
    "team.link_revoked": _case_team_link_revoked,
    "team.joined": _case_team_joined,
    "team.join_refused": _case_team_join_refused,
    "endpoint.blocked": _case_endpoint_blocked,
    "outbox.reply_failed": _case_outbox_reply_failed,
    # Vínculo do aluno
    "student.list_imported": _case_student_list_imported,
    "student.invites_prepared": _case_student_invites_prepared,
    "student.invites_queued": _case_student_invites_queued,
    "student.invite_sent": _case_student_invite_sent,
    "student.invite_failed": _case_student_invite_failed,
    "student.invite_cancelled": _case_student_invite_cancelled,
    "student.invite_revoked": _case_student_invite_revoked,
    "student.joined": _case_student_joined,
    # Disparo e resposta
    "dispatch.prepared": _case_dispatch_prepared,
    "dispatch.validated": _case_dispatch_validated,
    "dispatch.rejected": _case_dispatch_rejected,
    "dispatch.confirmed": _case_dispatch_confirmed,
    "dispatch.cancelled": _case_dispatch_cancelled,
    "dispatch.opened": _case_dispatch_opened,
    "dispatch.closed": _case_dispatch_closed,
    "delivery.delivered": _case_delivery_delivered,
    "delivery.not_delivered": _case_delivery_not_delivered,
    "delivery.question_failed": _case_delivery_question_failed,
    "delivery.dont_know": _case_delivery_dont_know,
    "submission.graded": _case_submission_graded,
    "submission.internal_failure": _case_submission_internal_failure,
    # Mensagens livres opcionais por oferecimento (o 43º tipo)
    "offering.settings_changed": _case_offering_settings_changed,
    # Relatórios e acompanhamento
    "report.frozen": _case_report_frozen,
    "notice.recorded": _case_notice_recorded,
    "offering.digest_changed": _case_offering_digest_changed,
    # CLI (expurgo)
    "offering.purge_prepared": _case_offering_purge_prepared,
    "offering.purged": _case_offering_purged,
    # Banco de questões local
    "bank.uploaded": _case_bank_uploaded,
}


@pytest.mark.parametrize("event_type", list(FAIL_CLOSED_CASES))
def test_event_commit_failure_fails_closed(
        event_type: str, client: Any, sql: psycopg.Connection, clock: FakeClock, settings: Any,
        db_url: str, queue: Any, outbox: Any, worker_thread: Any, bot_api: Any,
        bot_worker_thread: Any, smtp: Any, smtp_settings: Any, mail_channel: Any,
        request: pytest.FixtureRequest) -> None:
    """Falha fechada (o evento na mesma transação, a revelação só com a auditoria confirmada, os logs limpos):
    com o COMMIT do evento do tipo dado falhando (gatilho adiado com nome e RA canário):
    `identity.revealed` → 500 sem identidade e sem evento; `identity.reveal_denied` → 500;
    `person.registered` → 500 sem linhas em `people`/`identities`; `token.created` → 500 sem segredo
    e sem token utilizável; `token.revoked` → 500 com `revoked_at` nulo; `task.failed`
    (`queue.fail(…, permanent=True)`) e `outbox.failed` (`Undeliverable`) → a exceção sobe, o item
    continua `leased` e não há evento. Em todos, o log fica sem canário e sem `Traceback`, com
    `event: exception` (na API, ou no laço do worker para as filas); sem o gatilho, o controle
    passa.

    Oferecimentos e bots, um caso por tipo:
    `offering.created` (cadastro), `offering.state_changed` (ativação), `offering.archive_prepared`,
    `bot.registered`, `bot.token_replaced`, `bot.access_changed` (troca que recupera o acesso),
    `bot.lease_changed` (arquivamento com posse), `team.link_created` e `team.link_revoked` (link
    novo) → 500 exato, nada de oferecimentos e bots gravado (instantâneos de `_fingerprint`) e o controle passa;
    `bot.profile_updated`, `team.joined` e `team.join_refused` → a tarefa do tratador volta a
    `pending` com `last_error` `RaiseException`, registro `task.processed` e nenhum
    `event=exception`, sem perfil configurado, sem vínculo (convite e matrícula `pending`, sem
    endereço nem boas-vindas, update `pending`) e sem resposta; `endpoint.blocked` e
    `outbox.reply_failed` → a mensagem fica em posse, o endereço vigente, sem `outbox.failed`, com
    `event=exception` no laço do worker; sem o gatilho, cada controle passa.

    Vínculo do aluno (lista e convites), um caso por tipo,
    com o aluno
    canário A na lista, o SMTP falso e o adaptador real: `student.list_imported` (importação),
    `student.invites_prepared` (preparo) e `student.invites_queued` (confirmação do lote) → 500 exato,
    nada gravado (pessoas, matrículas, preparo, envios, tarefas `invite.email`; o código de confirmação
    não é consumido) e o controle passa com a resposta e o evento exatos; na T1 da tarefa `invite.email`
    (`student.invite_cancelled`: envio de um oferecimento encerrado antes da tarefa;
    `student.invite_revoked`: reenvio pela coordenação que substitui o convite do lote), a transação
    volta atrás e a
    tarefa repete sem mudança (`pending`, `last_error` `RaiseException`, nenhum código novo, nenhum
    e-mail, nenhum registro `invite.email`); na T2 (`student.invite_sent`, com o e-mail entregue;
    `student.invite_failed`, com a `PermanentFailure` do RCPT recusado com 550), a transação volta
    atrás, a tarefa fica `pending` com `last_error` `RaiseException`, o envio `pending` com
    `attempts = 1`, o convite da T1 pendente e nenhum registro `invite.email` (ele sai depois do
    COMMIT); no controle, a T1 seguinte revoga o convite anterior (`replaced`) e gera outro código: no
    `student.invite_sent`, o aluno recebe dois e-mails exatos, e só o convite do último código fica
    pendente; no `student.invite_failed`, duas conexões ao SMTP falso, as duas com o RCPT
    recusado e sem mensagem capturada, e `failed`/`recipient_refused` com o `task.failed` da fila;
    `student.joined` (o `/aceito` depois do termo apresentado) segue o `team.joined`: a tarefa volta a
    `pending`, sem termo aceito, vínculo, endereço nem boas-vindas, e o controle vincula e dá
    boas-vindas; nos logs, nenhum alvo dos alunos canário nem código de convite. O caminho da T1 do
    `student.invite_failed` (`not_configured`) fica em `test_invite_failed_not_configured_fails_closed`,
    fora do `FAIL_CLOSED_CASES`.

    Disparo e resposta, um
    caso por tipo, com o motor falso, a obtenção falsa e a Bot API falsa estendida (pedidos pelo
    `request` só nos casos do disparo): `dispatch.prepared`, `dispatch.confirmed` e `dispatch.cancelled` (pelo
    operador) → 500 exato, nada gravado (referência, disparo, tarefa) e o controle passa com a resposta e
    o evento exatos; `dispatch.validated` (último passo da validação), `dispatch.rejected` (obtenção sem
    o repositório), `dispatch.opened`, `dispatch.closed` (com a rodada da varredura depois do horário),
    `delivery.not_delivered` da abertura (endereço bloqueado) e do fechamento (entrega sem variante com o
    motor fora do ar), `delivery.question_failed` (400 na criação da variante), `delivery.dont_know`
    (toque no "Não sei"), `submission.graded` e `submission.internal_failure` (400 na correção) → a
    tarefa volta a `pending` com `last_error` `RaiseException`, o registro `task.processed` e nenhum
    `event=exception`, sem mudança no disparo, na entrega nem na submissão, e sem o evento; no controle,
    a tarefa repete depois da espera da fila e grava o estado e um único evento exato;
    `delivery.delivered` (envio aceito da questão) e o `delivery.not_delivered` do resultado do envio (403
    de chat bloqueado) → a mensagem fica em posse, a entrega `pending`, o endereço vigente, sem
    `outbox.failed`, com `event=exception` no laço do worker; no controle, vencida a posse, a mensagem é
    retomada e a entrega muda com um único evento. Em todos, nenhum desvio de protocolo nos dublês.

    Mensagens livres opcionais por oferecimento:
    `offering.settings_changed` (ligar as mensagens livres pela alteração da configuração) → 500 exato,
    `offerings.free_messages` continua falso e nenhuma linha em `offering_contacts`; sem o gatilho, 200
    com o `Offering` e o evento exato.

    Relatórios e acompanhamento,
    um caso por tipo, com a equipe simulada (um membro com endereço ativo) e os
    dublês do disparo: `report.frozen` → (i) pelo worker, no congelamento de um disparo fechado sem correção
    pendente: a tarefa `report.freeze` volta a `pending` com `last_error` `RaiseException`, o registro
    `task.processed` e nenhum `event=exception`, sem linha em `dispatch_reports`, sem aviso `dispatch_closed`
    e sem mensagem dele na fila; no controle, a tarefa repete depois da espera da fila e congela uma única
    versão `closing` com um único `report.frozen`; (ii) pela API, sobre esse disparo já congelado:
    `reports.resend_summary` → 500 exato, sem versão 2 e sem aviso `summary_resent`; sem o gatilho, 200 com
    a versão 2 e o evento exato; `notice.recorded` → um aviso de texto `dispatch_rejected` registrado
    `pending` (rejeição na validação): a tarefa `notice.send` volta a `pending` com `RaiseException`, o aviso
    continua `pending`, sem `finished_at`, nenhuma linha com `notice_id` na fila; no controle, o aviso fica
    `queued` com uma mensagem por destinatário e um único `notice.recorded`; `offering.digest_changed` →
    `offerings.update_digest` com mudança → 500 exato, sem linha em `offering_digests` (instantâneo de
    `_fingerprint`); sem o gatilho, 200 com o `DigestView` e o evento exato. Em todos, o log sem canário e
    sem `Traceback`, com `event: exception` só na API.

    CLI (operação e expurgo),
    um caso por tipo: `offering.purge_prepared` → `offerings.prepare_purge` num oferecimento
    arquivado sem pendência → 500 exato, sem código gravado (as três colunas do código nulas, instantâneo de
    `_fingerprint`); sem o gatilho, 200 com o corpo exato e o evento exato; `offering.purged` →
    `offerings.purge` com o código válido, no cenário da semente do expurgo → 500 exato, nada apagado (as seis
    tabelas do expurgo e `offerings` iguais pelo `snapshot_purge_tables`, `purged_at` nulo) e o código
    continua válido; sem o gatilho, com o mesmo código, 200 com o resultado exato e um único
    `offering.purged`. Em todos, o log sem canário e sem `Traceback`, com `event: exception` só na API.

    Banco de questões local: `bank.uploaded` → `banks.upload` de um banco novo → 500 exato, sem linha em
    `question_banks`; sem o gatilho, 200 com `created: true`, uma linha e o evento exato. No esqueleto, a rota do
    envio lança `NotImplementedError`, e o controle recebe 500.
    No esqueleto: `EventType` sem os 2 tipos."""
    assert set(FAIL_CLOSED_CASES) == {t.value for t in EventType}
    FAIL_CLOSED_CASES[event_type](_FailClosedCtx(
        client=client, sql=sql, clock=clock, settings=settings, db_url=db_url, queue=queue,
        outbox=outbox, worker_thread=worker_thread, bot_api=bot_api,
        bot_worker_thread=bot_worker_thread, smtp=smtp, smtp_settings=smtp_settings,
        mail_channel=mail_channel, request=request))


# --- rotas -------------------------------------------------------------------------------------


def test_unknown_routes_and_methods_use_the_error_envelope(client: Any) -> None:
    """Rota desconhecida: `/v1/nada` sem token → 401
    e com token → 404; `DELETE /v1/people` com token → 405 `method_not_allowed` (sem token, 401);
    rota e método vêm antes da validação; fora de `/v1/`, caminho desconhecido → 404 sem token; o
    registro `request` traz o modelo da rota (`null` sem rota; no 405, a rota que casou), nunca a
    URL crua; sem eco."""
    admin = auth(ADMIN_TOKEN)
    invalid_input = {"params": {"extra_param_x": "1"}, "headers": {**admin, "X-CQ-Client": "CLI"}}

    resp, captured = _logged(lambda: client.get("/v1/nada"))
    assert_error(resp, 401, "unauthenticated")
    _assert_one_request(captured, method="GET", route=None, status=401)

    resp, _ = _logged(lambda: client.get("/v1/nada", headers=auth("token-desconhecido-qualquer")))
    assert_error(resp, 401, "unauthenticated")
    assert_no_echo(resp, "token-desconhecido-qualquer")

    resp, captured = _logged(lambda: client.get("/v1/nada", headers=admin))
    assert_error(resp, 404, "not_found")
    _assert_one_request(captured, method="GET", route=None, status=404)

    resp, _ = _logged(lambda: client.post("/v1/nada", json={"campo": "valor"}, headers=admin))
    assert_error(resp, 404, "not_found")

    # A rota vem antes da validação: consulta desconhecida e X-CQ-Client inválido não viram 400.
    resp, _ = _logged(lambda: client.get("/v1/nada", **invalid_input))
    assert_error(resp, 404, "not_found")

    resp, _ = _logged(lambda: client.get("/v1/"))
    assert_error(resp, 401, "unauthenticated")
    resp, _ = _logged(lambda: client.get("/v1/", headers=admin))
    assert_error(resp, 404, "not_found")

    resp, captured = _logged(lambda: client.get("/v1/caminho-sentinela-xyz", headers=admin))
    assert_error(resp, 404, "not_found")
    assert_no_echo(resp, "caminho-sentinela-xyz")
    _assert_one_request(captured, method="GET", route=None, status=404)
    assert "caminho-sentinela-xyz" not in captured.text, "o log trouxe a URL crua"

    # Fora de /v1/, nada exige token.
    resp, captured = _logged(lambda: client.get("/nada"))
    assert_error(resp, 404, "not_found")
    _assert_one_request(captured, method="GET", route=None, status=404)

    resp, captured = _logged(lambda: client.delete("/v1/people", headers=admin))
    assert_error(resp, 405, "method_not_allowed")
    _assert_one_request(captured, method="DELETE", route="/v1/people", status=405)

    resp, _ = _logged(lambda: client.delete("/v1/people"))
    assert_error(resp, 401, "unauthenticated")

    # O método vem antes da validação.
    resp, _ = _logged(lambda: client.delete("/v1/people", **invalid_input))
    assert_error(resp, 405, "method_not_allowed")

    resp, captured = _logged(lambda: client.delete("/v1/people/ABCD", headers=admin))
    assert_error(resp, 405, "method_not_allowed")
    _assert_one_request(captured, method="DELETE", route="/v1/people/{ref}", status=405)

    resp, _ = _logged(lambda: client.put("/v1/tokens", json={}, headers=admin))
    assert_error(resp, 405, "method_not_allowed")

    resp, captured = _logged(lambda: client.get(f"/v1/tokens/{uuid.uuid4()}/revoke", headers=admin))
    assert_error(resp, 405, "method_not_allowed")
    _assert_one_request(captured, method="GET", route="/v1/tokens/{token_id}/revoke", status=405)


def test_trailing_slash_is_unknown_path(client: Any, sql: psycopg.Connection) -> None:
    """Rota desconhecida: um caminho do
    catálogo com barra final é um caminho fora do catálogo, e não um redirecionamento. Com token →
    404 `not_found` no envelope, sem cabeçalho `Location`, com `route: null` no registro `request`,
    sem eco do caminho e sem efeito (o `POST` não cadastra, não cria token nem revela; a revogação
    não revoga). Sem token → 401. Fora de `/v1/`, sem token → 404. Os pedidos **não** seguem
    redirecionamentos (o `TestClient` segue por padrão)."""
    admin = auth(ADMIN_TOKEN)
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Ficticia Da Barra Final")
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token da barra final")
    before = _fingerprint(sql)

    def check(method: str, path: str, status: int, code: str, *, headers: dict | None = None,
              sentinels: tuple[str, ...] = (), **kwargs: Any) -> None:
        resp, captured = _logged(lambda: client.request(method, path, headers=headers,
                                                        follow_redirects=False, **kwargs))
        label = f"{method} {path}"
        assert resp.status_code == status, (
            f"{label}: status {resp.status_code} != {status}; Location: "
            f"{'presente' if 'location' in resp.headers else 'ausente'}")
        assert "location" not in resp.headers, f"{label}: a resposta redirecionou"
        assert_error(resp, status, code)
        _assert_one_request(captured, method=method, route=None, status=status)
        if sentinels:
            assert_no_echo(resp, *sentinels)
            for value in sentinels:
                assert value not in captured.text, f"{label}: o log trouxe a URL crua"
        assert find_canary(_exposed(resp)) == [], f"{label}: identidade na resposta"

    check("GET", "/v1/people/", 404, "not_found", headers=admin)
    check("GET", "/v1/tokens/", 404, "not_found", headers=admin)
    check("GET", "/v1/events/", 404, "not_found", headers=admin,
          params={"types": "person.registered"})
    check("GET", f"/v1/people/{person['alias']}/", 404, "not_found", headers=admin)
    check("GET", f"/v1/people/{person['pseudo_id']}/", 404, "not_found", headers=admin,
          sentinels=(person["pseudo_id"],))
    check("GET", "/v1/people/caminho-sentinela-barra/", 404, "not_found", headers=admin,
          sentinels=("caminho-sentinela-barra",))
    # Pedidos que, redirecionados e refeitos, mudariam o banco ou revelariam identidade.
    check("POST", "/v1/people/", 404, "not_found", headers=admin,
          json={"name": "Pessoa Ficticia Nova Da Barra"}, sentinels=("Pessoa Ficticia Nova Da Barra",))
    check("POST", "/v1/tokens/", 404, "not_found", headers=admin,
          json={"label": "rotulo-sentinela-barra", "permissions": ["people:read"]},
          sentinels=("rotulo-sentinela-barra",))
    check("POST", "/v1/identity/reveal/", 404, "not_found", headers=admin,
          json={"pseudonyms": [person["alias"]], "reason": "revelacao-sentinela-barra"},
          sentinels=("revelacao-sentinela-barra", "Pessoa Ficticia Da Barra Final"))
    check("POST", f"/v1/tokens/{token['token_id']}/revoke/", 404, "not_found", headers=admin,
          sentinels=(token["token_id"],))

    assert _fingerprint(sql) == before, "um pedido com barra final teve efeito"
    assert sql.execute("SELECT revoked_at FROM api_tokens WHERE token_id = %s::uuid",
                       (token["token_id"],)).fetchone() == (None,)

    # Sob /v1/, a autenticação vem antes da rota.
    check("GET", "/v1/people/", 401, "unauthenticated")
    check("POST", "/v1/people/", 401, "unauthenticated",
          json={"name": "Pessoa Ficticia Sem Token Da Barra"})

    # Fora de /v1/, nada exige token.
    check("GET", "/health/", 404, "not_found")
    check("GET", "/openapi.json/", 404, "not_found")
    assert _fingerprint(sql) == before


# --- entrada estrita ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", V1_OPS)
def test_unknown_query_parameter_is_rejected(op_id: str, client: Any,
                                             sql: psycopg.Connection) -> None:
    """Entrada estrita: a chamada válida da operação com
    `?extra_param_x=1` → 400 `[{query, unknown_field}]`, sem eco do nome e sem efeito; a mesma
    chamada sem o parâmetro passa (controle). Oferecimentos e bots: também as 10
    operações deles (201 em `offerings.create` e `team.link`), sem efeito nas tabelas deles. Vínculo do
    aluno: também as 5 operações dele (200 em todas), sem efeito em
    `invite_sends`, nas colunas do vínculo nem nas tarefas `invite.email`. Mensagens livres opcionais: também
    `offerings.update_settings` (200)."""
    assert set(V1_OPS) == CATALOG_V1_OPS
    state = seed_state(client, revoke_targets=2)
    admin = auth(ADMIN_TOKEN)
    before = _fingerprint(sql)

    resp = VALID_CALLS[op_id](client, state, admin, params={"extra_param_x": "1"})
    assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert_no_echo(resp, "extra_param_x")
    assert _fingerprint(sql) == before

    ok = VALID_CALLS[op_id](client, state, admin)
    assert ok.status_code == SUCCESS_STATUS[op_id], ok.text[:300]
    assert_matches(op_id, ok.status_code, ok.json())


def test_unknown_query_parameters_collapse_into_one_trailing_entry(client: Any) -> None:
    """Entrada estrita (outros casos): dois parâmetros
    desconhecidos, antes e depois de um conhecido inválido, viram **uma** entrada
    `{query, unknown_field}`, no fim de `fields`, sem eco."""
    resp = client.get("/v1/people", headers=auth(ADMIN_TOKEN),
                      params=[("extra_param_x", "1"), ("limit", "0"), ("outro_param_y", "2")])
    assert_error(resp, 400, "invalid_request",
                 fields=[("limit", "out_of_range"), ("query", "unknown_field")])
    assert_no_echo(resp, "extra_param_x", "outro_param_y")


class _BodyOnAnyMethod:
    """Manda o corpo dado em `request`/`get`/`post` (o `TestClient.get` não aceita corpo; as
    chamadas GET de `VALID_CALLS` usam `request("GET", …)`, e `tokens.revoke` usa `post`). As
    chamadas válidas das operações sem corpo não passam `json`/`content`, então não há chave
    duplicada."""

    def __init__(self, client: Any, **body: Any) -> None:
        self._client = client
        self._body = body

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        assert not {"json", "content", "data"} & kwargs.keys(), kwargs.keys()
        return self._client.request(method, url, **kwargs, **self._body)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)


BODIES = (
    {"json": {"campo_extra_x": "valor-sentinela-corpo"}},
    {"json": {}},
    {"content": b"texto-sentinela-cru"},
)


@pytest.mark.parametrize("op_id", BODYLESS_V1_OPS)
def test_body_on_bodyless_route_is_rejected(op_id: str, client: Any,
                                            sql: psycopg.Connection) -> None:
    """Entrada estrita: corpo (`Content-Length > 0`,
    JSON ou não, mesmo `{}`) numa rota `/v1` que não recebe corpo, inclusive `tokens.revoke` → 400
    `[{body, unknown_field}]`, sem eco e sem efeito; sem corpo, a chamada passa (controle). Oferecimentos
    e bots: as 6 operações sem corpo (`offerings.list`, `offerings.get`,
    `offerings.close`, `offerings.prepare_archive`, `bots.activate` e `bots.get`). Vínculo do aluno:
    `students.summary`, a única operação do vínculo sem corpo (as outras 4 têm
    esquema de pedido). Disparo e resposta: `dispatches.get`, `dispatches.list`,
    `dispatches.confirm`, `dispatches.cancel` e `messages.list` (só `dispatches.prepare` tem esquema de
    pedido). Mensagens livres: `offerings.update_settings` tem esquema de pedido e fica fora do conjunto.
    Relatórios: `reports.get`, `reports.list_versions`,
    `reports.resend_summary` e `offerings.get_digest` (só `offerings.update_digest` tem esquema de pedido). CLI:
    `system.doctor` e `offerings.prepare_purge` (só
    `offerings.purge` tem esquema de pedido). Banco de questões local: `banks.list` (só `banks.upload` tem esquema
    de pedido); no esqueleto, a rota lança `NotImplementedError` (500)."""
    assert set(BODYLESS_V1_OPS) == {"people.list", "people.get", "tokens.list", "tokens.revoke",
                                    "events.list",
                                    # Oferecimentos e bots
                                    "offerings.list", "offerings.get", "offerings.close",
                                    "offerings.prepare_archive", "bots.activate", "bots.get",
                                    # Vínculo do aluno
                                    "students.summary",
                                    # Disparo e resposta
                                    "dispatches.get", "dispatches.list", "dispatches.confirm",
                                    "dispatches.cancel", "messages.list",
                                    # Relatórios e acompanhamento
                                    "reports.get", "reports.list_versions", "reports.resend_summary",
                                    "offerings.get_digest",
                                    # CLI
                                    "system.doctor", "offerings.prepare_purge",
                                    # Banco de questões local
                                    "banks.list"}
    state = seed_state(client, revoke_targets=len(BODIES) + 1)
    admin = auth(ADMIN_TOKEN)
    before = _fingerprint(sql)

    for body in BODIES:
        resp = VALID_CALLS[op_id](_BodyOnAnyMethod(client, **body), state, admin)
        assert_error(resp, 400, "invalid_request", fields=[("body", "unknown_field")])
        assert_no_echo(resp, "campo_extra_x", "valor-sentinela-corpo", "texto-sentinela-cru")
    assert _fingerprint(sql) == before

    ok = VALID_CALLS[op_id](client, state, admin)
    assert ok.status_code == 200, ok.text[:300]
    assert_matches(op_id, 200, ok.json())


@pytest.mark.parametrize("op_id", V1_OPS)
def test_invalid_client_header_is_rejected(op_id: str, client: Any,
                                           sql: psycopg.Connection) -> None:
    """Entrada estrita: `X-CQ-Client` diferencia
    maiúsculas e vale em todo `/v1/*`: `CLI` (inclusive em `people.list`) ou outro valor →
    400 `[{X-CQ-Client, invalid_value}]`, sem eco e sem efeito; `cli`, `mcp` e `other` passam. Oferecimentos
    e bots: também as 10 operações deles. Vínculo do aluno: e as 5 operações dele
    (cada chamada de `invites.send` consome um alvo da semente, inclusive as recusadas). Mensagens livres:
    também `offerings.update_settings`."""
    state = seed_state(client, revoke_targets=5)
    admin = auth(ADMIN_TOKEN)
    before = _fingerprint(sql)

    upper = VALID_CALLS[op_id](client, state, {**admin, "X-CQ-Client": "CLI"})
    assert_error(upper, 400, "invalid_request", fields=[("X-CQ-Client", "invalid_value")])
    other = VALID_CALLS[op_id](client, state, {**admin, "X-CQ-Client": "cliente-desconhecido"})
    assert_error(other, 400, "invalid_request", fields=[("X-CQ-Client", "invalid_value")])
    assert_no_echo(other, "cliente-desconhecido")
    assert _fingerprint(sql) == before

    for value in ("cli", "mcp", "other"):
        ok = VALID_CALLS[op_id](client, state, {**admin, "X-CQ-Client": value})
        assert ok.status_code == SUCCESS_STATUS[op_id], (value, ok.text[:300])
        assert_matches(op_id, ok.status_code, ok.json())
