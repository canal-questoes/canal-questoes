"""Teste canário de privacidade.

Cadastra as identidades canário (`tests/helpers/canary.py`), semeia os textos livres de
`SEEDED_FREE_TEXT` e então exercita, nesta ordem, o worker, a API e o CLI. No fim varre:
- todo corpo e cabeçalho de resposta da API (exceto o corpo do 200 da revelação);
- toda saída do CLI (exceto a de `identity reveal` com sucesso);
- toda linha de log capturada com o `JsonFormatter` real;
- a saída padrão e a de erro do processo (fd 1 e 2, inclusive das threads do worker e do portal do
  `TestClient`), que no Compose também são log técnico, e os avisos (`warnings`) emitidos;
- as colunas de `SQL_SCAN` no banco.

Nada disso pode conter identidade canário nem segredo de token. Os controles positivos provam que o
detector acharia um vazamento, e as checagens "contra o vazio" provam que as superfícies foram de
fato exercitadas.

**Oferecimentos e bots.** Entre o worker e a API roda uma
**fase de bot**: um oferecimento
com a disciplina e o período canário (nome, RA e e-mail), o bot registrado e ativado contra a Bot API
falsa, e remetentes canário do Telegram (`TG_TEODORICO`, `TG_FILISBINA`, `TG_ZEFIRO`: nome, sobrenome
e `@username` inconfundíveis) mandando `/start` válido e inválido, `/ajuda`, texto com menção e
mensagem respondida/encaminhada, botão (`callback_query`) e bloqueio (`my_chat_member`), com o chat
bloqueado, a espera pedida pelo Telegram (429) e a revogação do token. Os textos injetados não têm
alvos canário. Depois, a API e o CLI cobrem as 10 operações e as 10 folhas, com sucesso e com
erro. As varreduras incluem:
- os alvos dos remetentes do Telegram (`tg-nome`, `tg-palavra`, `tg-username`, `tg-id`) em respostas,
  saídas do CLI, logs, saída do processo e parâmetros enviados ao Telegram (inclusive
  `setMyDescription`, `setMyCommands` e o texto do `sendMessage`); só o `chat_id` do `sendMessage`
  fica de fora, com controle positivo;
- a varredura genérica do banco (`scan_all_tables`, todas as colunas de todas as tabelas): nenhum
  alvo de nome, palavra, RA, e-mail ou `@username` fora de `identities` (área restrita) e de
  `outbox_messages.text` (conteúdo entregue), e o `tg-id` só nas colunas internas de identidade
  (`INTERNAL_IDENTITY_COLUMNS`), cada uma com controle positivo;
- os segredos (tokens de bot, chaves-mestras, links e códigos de convite) em respostas (corpo,
  cabeçalhos e URL do pedido), CLI, logs e saída do processo; a única exceção é o corpo do 201 de
  `team.link`, que pode conter só o próprio link e o seu código (igualdade exata e controle positivo).

**Vínculo do aluno.** Entre a fase de bot e a de API roda uma
**fase de alunos**, com
oferecimento e bot próprios, o SMTP falso e a Bot API falsa, com os alunos canário
(`tests/helpers/canary.py`: A com 2 contas do Telegram, B com 1), em 8 passos fixos que produzem os 8
desfechos da recepção e os 8 tipos de evento do vínculo (lote, termo e aceite, link repetido, link de
outra pessoa, troca de conta, autosserviço com o controle positivo do texto literal, recusa e link
vencido; falhas de e-mail com eco do endereço e dois reenvios com o worker parado para o cancelamento
por envio superado). Ela termina com a parada do próprio worker, antes da API; o CLI usa oferecimentos
próprios para as 5 folhas do vínculo, sem tirar alvos do `state`. As varreduras incluem:
- os alvos dos alunos canário (`prefixes=STUDENT_PREFIXES`) em respostas, saídas do CLI, logs, saída
  do processo, parâmetros enviados ao Telegram (fora o `chat_id` do `sendMessage`), mensagens do SMTP
  falso (fora o `RCPT TO` e o `To`) e todas as colunas de todas as tabelas, com as exceções exatas da
  identidade do aluno (a lista em `identities`; o id do Telegram nas 3 colunas internas), cada uma com controle
  positivo; o nome, o sobrenome e o `@username` do perfil do Telegram dos alunos sem exceção nenhuma;
- o detector genérico de código de convite de aluno (toda janela de 22 caracteres, contra os resumos de
  `invites.code_sha256`), que acha também os códigos nunca entregues, em todas as saídas e colunas, fora
  o e-mail entregue (só o próprio código) e o texto literal do `/start <código>`;
- a senha do SMTP falso e as suas formas no fio (`mail_secrets()`) nas saídas e, por subcadeia, em todas
  as colunas; o assunto e o corpo dos e-mails sem nenhum alvo (os das fases anteriores e os dos alunos).
As contagens exatas de tarefas, mensagens da fila de saída e updates incluem a fase de alunos e
as chamadas do vínculo.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import time
import uuid
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import click
import psycopg
from fastapi.testclient import TestClient

import cq_client.cli as cq_cli
import cq_server
from cq_server.adapters.console import ConsoleChannel
from cq_server.events import EventType
from cq_server.logs import JsonFormatter
from cq_server.migrate import EXPECTED_VERSION
from cq_server.ports.delivery import OutgoingMessage, Undeliverable
from cq_server.tasks import ClaimedTask, PermanentTaskError, TaskRecord
from tests.helpers.api import (ADMIN_TOKEN, all_events, auth, create_token, list_events, marker,
                               register_person)
from tests.helpers.calls import (CANARY_ASCII, CANARY_CALLS, CANARY_NAME_PATH, DECLARED_SURFACES,
                                 SEEDED_FREE_TEXT, SQL_SCAN, VALID_CALLS, CallState, CanaryCall,
                                 seed_state)
from tests.helpers.canary import (CANARY_PEOPLE, OLIMPIO, URSULA, ZEFIRO, CanaryPerson, find_canary,
                                  find_secrets, fold)
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.contract import (MESSAGES, assert_error, assert_matches, assert_no_echo,
                                    load_catalog)
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import all_secrets, remember_secret

# Oferecimentos e bots
import dataclasses
import hashlib
from datetime import timedelta

from cq_server.botmessages import (ALREADY_LINKED, COMMANDS, DESCRIPTION_ACTIVE, HELP,
                                   LINK_INVALID, OTHER_PERSON, WELCOME_TEAM)
from tests.helpers.api import (TEAM_LINK_SHAPE, activate_bot, create_offering,
                               create_team_link, link_code, register_bot)
from tests.helpers.calls import DUMMY_BOT_TOKEN, INTERNAL_IDENTITY_COLUMNS
from tests.helpers.canary import (ALL_PREFIXES, CANARY_TELEGRAM_USERS, TG_FILISBINA,
                                  TG_PREFIXES, TG_TEODORICO, TG_ZEFIRO)
from tests.helpers.db import scan_all_tables
from tests.helpers.secrets import bot_secrets

# Vínculo do aluno
import unicodedata
from collections.abc import Collection
from datetime import timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from psycopg import sql as pgsql

from cq_server import botmessages
from cq_server.botmessages import INVITE_REQUESTED, TERMS, TERMS_REFUSED, WELCOME_STUDENT
from tests.helpers.api import (import_students, prepare_invites, resend_invites, send_invites,
                               students_summary, write_roster)
from tests.helpers.contract import FIELDS_CODES
from tests.helpers.canary import (CANARY_STUDENTS, STUDENT_A, STUDENT_B, STUDENT_PREFIXES,
                                  STUDENT_TARGETS, TG_STUDENT_A1, TG_STUDENT_A2, TG_STUDENT_B,
                                  CanaryStudent)
from tests.helpers.secrets import (find_invite_codes, invite_code_digest, invite_code_label,
                                   mail_secrets)
from tests.helpers.smtp import SENDER as SMTP_SENDER

# Disparo e resposta
from cq_server import deliverytexts
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.scheduler import CLOSE_RECHECK_SECONDS
from tests.helpers.api import (cancel_dispatch, confirm_dispatch, get_dispatch, list_dispatches,
                               list_free_messages, prepare_dispatch)
from tests.helpers.calls import REPO_PRIVATE_TOKEN
from tests.helpers.canary import (CANARY_DISPATCH_STUDENTS, DISPATCH_A, DISPATCH_B, DISPATCH_PREFIXES, DISPATCH_TARGETS,
                                  TG_DISPATCH_A1, TG_DISPATCH_B)
from tests.helpers.engine import (ANSWER_SENTINEL, TECHNICAL_DETAIL_SENTINEL, FakeOption, FakeReason,
                                  multiple_choice_field, number_field, preset, text_field)

# Mensagens livres opcionais: a alteração da configuração
# do oferecimento (o `PATCH` que desliga as mensagens livres no passo 5.9 da fase de disparos).
from tests.helpers.api import update_offering_settings

# Relatórios e acompanhamento: a fase de relatórios e avisos à equipe (o
# remetente neutro dos membros da equipe, as questões do motor falso próprio da fase e a leitura do console).
from tests.helpers.bot_api import make_user
from tests.helpers.console_student import read_lines
from tests.helpers.engine import FakeQuestion, single_choice_field

ADMIN = auth(ADMIN_TOKEN)
WORKER_ID = "w-privacy-sweep"
EVENT_TYPES = [t.value for t in EventType]
SUCCESS_STATUS = {"people.register": 201, "tokens.create": 201}
SUCCESS_STATUS |= {"offerings.create": 201, "team.link": 201}  # oferecimentos e bots
UNREACHABLE_API = "http://127.0.0.1:9"
UNREACHABLE_MESSAGE = f"Não foi possível conectar à API em {UNREACHABLE_API}."
# Opção desconhecida cujo **nome** é o nome canário (o CLI nunca ecoa a opção).
UNKNOWN_OPTION = "--" + fold(ZEFIRO.name).replace(" ", "-")
DB_PASSWORD_SENTINEL = "senha-que-nao-pode-vazar"
UUID4_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
ALIAS_RE = r"(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}"
SECRET_RE = r"cq_[A-Za-z0-9_-]{43}"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
# Sentinelas de eco (8+ caracteres) enviadas pelas CANARY_CALLS.
ECHO_SENTINELS = (ZEFIRO.name, CANARY_ASCII, CANARY_NAME_PATH, ZEFIRO.ra, ZEFIRO.email, URSULA.ra)
# o token de bot de formato válido que as CANARY_CALLS de `bots.register` mandam.
ECHO_SENTINELS += (DUMMY_BOT_TOKEN,)
# Vínculo do aluno: o nome, o RA e o e-mail dos alunos canário, que nenhuma resposta das
# CANARY_CALLS pode conter (a identidade de aluno só sai pela revelação, que o canário não faz).
ECHO_SENTINELS += tuple(value for student in CANARY_STUDENTS for value in (student.name, student.ra, student.email))

# Pseudônimos de **formato válido** que carregam o RA canário (a regra: um item com formato de
# pseudônimo pode carregar um RA). Ao contrário das CANARY_CALLS, eles passam pela validação e
# chegam ao domínio: 404 da consulta e da revogação, `not_found` da revelação concedida e 403 da
# negada. Os logs, a saída do processo e o banco são varridos também nesses caminhos.
RA_ALIAS = f"{ZEFIRO.ra}a"
RA_UUID = f"c0ffee00-0000-4000-8000-{ZEFIRO.ra}ab"
assert re.fullmatch(ALIAS_RE, RA_ALIAS) and re.fullmatch(UUID4_RE, RA_UUID)
# Identidade da pessoa de apoio de `seed_state` (tests/helpers/calls.py), devolvida pela revelação.
SUPPORT_IDENTITY = {"name": "Pessoa de Apoio Semente", "ra": "1234500001", "email": None}
VALID_RA_REASON = "conferencia de pseudonimos de formato valido"

# Resultado exato de cada CANARY_CALL (pelos contratos da API): (status, code, extra), com `extra` = os
# `fields` no 400/409, a `permission` no 403 e None nos demais.
_UNKNOWN_BODY = [("body", "unknown_field")]
_UNKNOWN_QUERY = [("query", "unknown_field")]
_BAD_CLIENT = [("X-CQ-Client", "invalid_value")]
_UNAUTHENTICATED = (401, "unauthenticated", None)
_CREATED_WITH_REDACTED_LABEL = "created-with-redacted-label"
_HEALTH_OK = "health-ok"
_OPENAPI_OK = "openapi-ok"
_REVEALED_WITH_NOT_FOUND = "revealed-with-not-found"
EXPECTED_CANARY: dict[tuple[str, str], Any] = {
    ("people.register", "body_value"): (400, "invalid_request", [("email", "invalid_format")]),
    ("people.register", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("people.register", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("people.register", "header_auth"): _UNAUTHENTICATED,
    ("people.register", "conflict"): (409, "conflict", [("ra", "duplicate"), ("email", "duplicate")]),
    ("people.register", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("people.list", "query_value"): (400, "invalid_request", [("limit", "invalid_format")]),
    ("people.list", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("people.list", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("people.list", "header_auth"): _UNAUTHENTICATED,
    ("people.get", "path"): (400, "invalid_request", [("ref", "invalid_format")]),
    ("people.get", "path_ra"): (400, "invalid_request", [("ref", "invalid_format")]),
    ("people.get", "header_auth"): _UNAUTHENTICATED,
    ("people.get", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("people.get", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("identity.reveal", "body_value"): (
        400, "invalid_request", [("pseudonyms[0]", "invalid_format"), ("pseudonyms[1]", "invalid_format")]),
    ("identity.reveal", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("identity.reveal", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("identity.reveal", "header_auth"): _UNAUTHENTICATED,
    ("identity.reveal", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("tokens.create", "body_value"): _CREATED_WITH_REDACTED_LABEL,
    ("tokens.create", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("tokens.create", "header_auth"): _UNAUTHENTICATED,
    ("tokens.create", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("tokens.create", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("tokens.list", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("tokens.list", "header_auth"): _UNAUTHENTICATED,
    ("tokens.list", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("tokens.revoke", "path"): (400, "invalid_request", [("token_id", "invalid_format")]),
    ("tokens.revoke", "header_auth"): _UNAUTHENTICATED,
    ("tokens.revoke", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("tokens.revoke", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("tokens.revoke", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("events.list", "query_value"): (400, "invalid_request", [("types[0]", "invalid_value")]),
    ("events.list", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("events.list", "header_auth"): _UNAUTHENTICATED,
    ("events.list", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("route.unknown", "path"): (404, "not_found", None),
    ("route.method_not_allowed", "path"): (405, "method_not_allowed", None),
    ("route.trailing_slash", "path"): (404, "not_found", None),
    ("system.health", "header_auth"): _HEALTH_OK,
    ("route.openapi", "header_auth"): _OPENAPI_OK,
    ("people.register", "body_non_utf8"): (400, "invalid_request", [("body", "invalid_format")]),
    ("system.health", "query_key"): _HEALTH_OK,
    ("system.health", "header_client"): _HEALTH_OK,
    ("system.health", "method_body"): (405, "method_not_allowed", None),
    ("route.openapi", "query_key"): _OPENAPI_OK,
    ("tokens.create", "body_value_invalid"): (400, "invalid_request", [("permissions[0]", "invalid_value")]),
    ("identity.reveal", "body_value_long"): (400, "invalid_request", [("reason", "too_long")]),
    # _valid_ra_calls: pseudônimo de formato válido com o RA canário (chega ao domínio)
    ("people.get", "path_valid_ra"): (404, "not_found", None),
    ("tokens.revoke", "path_valid_ra"): (404, "not_found", None),
    ("identity.reveal", "body_valid_ra"): _REVEALED_WITH_NOT_FOUND,
    ("identity.reveal", "body_valid_ra_denied"): (403, "forbidden", "identity:reveal"),
}

# Oferecimentos e bots (tests/helpers/calls.py, as CANARY_CALLS dessa parte): o resultado exato de cada
# uma delas. O `{ref}` com o nome canário não tem o formato de código nem de UUID (400); o RA
# canário tem o formato de código e chega ao domínio (404); o código com o RA de pessoa cadastrada é
# recusado sem eco; a disciplina e o período canário são aceitos e saem redigidos.
_BAD_REF = [("ref", "invalid_format")]
_CREATED_OFFERING_REDACTED = "created-offering-redacted"
EXPECTED_CANARY.update({
    ("offerings.create", "body_value"): _CREATED_OFFERING_REDACTED,
    ("offerings.create", "body_value_code"): (400, "invalid_request", [("code", "invalid_value")]),
    ("offerings.create", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("offerings.create", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.create", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.create", "header_auth"): _UNAUTHENTICATED,
    ("offerings.list", "query_value"): (400, "invalid_request", [("limit", "invalid_format")]),
    ("offerings.list", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.list", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.list", "header_auth"): _UNAUTHENTICATED,
    ("offerings.get", "path"): (400, "invalid_request", _BAD_REF),
    ("offerings.get", "path_ra"): (404, "not_found", None),
    ("offerings.get", "header_auth"): _UNAUTHENTICATED,
    ("offerings.get", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.get", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.close", "path"): (400, "invalid_request", _BAD_REF),
    ("offerings.close", "header_auth"): _UNAUTHENTICATED,
    ("offerings.close", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.close", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.prepare_archive", "path"): (400, "invalid_request", _BAD_REF),
    ("offerings.prepare_archive", "header_auth"): _UNAUTHENTICATED,
    ("offerings.prepare_archive", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.prepare_archive", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("bots.activate", "path"): (400, "invalid_request", _BAD_REF),
    ("bots.activate", "header_auth"): _UNAUTHENTICATED,
    ("bots.activate", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("bots.activate", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("bots.get", "path"): (400, "invalid_request", _BAD_REF),
    ("bots.get", "header_auth"): _UNAUTHENTICATED,
    ("bots.get", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("bots.get", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.archive", "path"): (400, "invalid_request", _BAD_REF),
    ("offerings.archive", "body_value"): (400, "invalid_request", [("confirmation_code", "too_long")]),
    ("offerings.archive", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("offerings.archive", "header_auth"): _UNAUTHENTICATED,
    ("offerings.archive", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("offerings.archive", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("bots.register", "path"): (400, "invalid_request", _BAD_REF),
    ("bots.register", "body_value"): (400, "invalid_request", [("token", "invalid_format")]),
    ("bots.register", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("bots.register", "header_auth"): _UNAUTHENTICATED,
    ("bots.register", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("bots.register", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("team.link", "path"): (400, "invalid_request", _BAD_REF),
    ("team.link", "body_value"): (400, "invalid_request", [("person", "invalid_format")]),
    ("team.link", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("team.link", "header_auth"): _UNAUTHENTICATED,
    ("team.link", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("team.link", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
})

# Vínculo do aluno (tests/helpers/calls.py, as CANARY_CALLS dessa parte): o resultado exato de
# cada uma delas. O `{ref}` com o nome canário é recusado sem eco (400); o RA canário tem o formato
# de código e chega ao domínio (404); a linha com a identidade canário é recusada item a item, sem eco; o
# RA de Zéfiro com o e-mail de Olímpio (os dois cadastrados) é o 409 `list_conflict`. Nenhuma chamada
# canário do vínculo do aluno dá um 409 fora de `FIELDS_CODES` (`invalid_state`, `bot_required` e
# `invalid_confirmation` ficam de fora).
EXPECTED_CANARY.update({
    ("students.import", "path"): (400, "invalid_request", _BAD_REF),
    ("students.import", "body_value"): (400, "invalid_request", [("rows[0]", "invalid_format")]),
    ("students.import", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("students.import", "body_key_row"): (400, "invalid_request", [("rows[0]", "unknown_field")]),
    ("students.import", "conflict"): (409, "list_conflict", [("rows[0]", "duplicate")]),
    ("students.import", "header_auth"): _UNAUTHENTICATED,
    ("students.import", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("students.import", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("students.summary", "path"): (400, "invalid_request", _BAD_REF),
    ("students.summary", "path_ra"): (404, "not_found", None),
    ("students.summary", "header_auth"): _UNAUTHENTICATED,
    ("students.summary", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("students.summary", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("invites.prepare_send", "path"): (400, "invalid_request", _BAD_REF),
    ("invites.prepare_send", "body_value"): (400, "invalid_request", [("valid_hours", "invalid_format")]),
    ("invites.prepare_send", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("invites.prepare_send", "header_auth"): _UNAUTHENTICATED,
    ("invites.prepare_send", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("invites.prepare_send", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("invites.send", "path"): (400, "invalid_request", _BAD_REF),
    ("invites.send", "body_value"): (400, "invalid_request", [("confirmation_code", "too_long")]),
    ("invites.send", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("invites.send", "header_auth"): _UNAUTHENTICATED,
    ("invites.send", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("invites.send", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("invites.resend", "path"): (400, "invalid_request", _BAD_REF),
    ("invites.resend", "body_value"): (
        400, "invalid_request", [("pseudonyms[0]", "invalid_format"), ("pseudonyms[1]", "invalid_format")]),
    ("invites.resend", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("invites.resend", "header_auth"): _UNAUTHENTICATED,
    ("invites.resend", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("invites.resend", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
})

# Piso da cobertura exigida, tirado dos contratos da API (quais operações têm corpo, consulta e caminho).
# Impede que um OpenAPI que esconda parâmetros encolha em silêncio o conjunto exigido.
_BASE_SURFACES = {"header_auth", "header_client", "query_key"}
MIN_REQUIRED_SURFACES: dict[str, set[str]] = {
    "people.register": _BASE_SURFACES | {"body_value", "body_key", "conflict"},
    "people.list": _BASE_SURFACES | {"query_value"},
    "people.get": _BASE_SURFACES | {"path"},
    "identity.reveal": _BASE_SURFACES | {"body_value", "body_key"},
    "tokens.create": _BASE_SURFACES | {"body_value", "body_key"},
    "tokens.list": set(_BASE_SURFACES),
    "tokens.revoke": _BASE_SURFACES | {"path"},
    "events.list": _BASE_SURFACES | {"query_value"},
    "route.unknown": {"path"},
}
# Oferecimentos e bots: corpo em create, archive, register e team.link; consulta em
# offerings.list; `{ref}` no caminho de todas as operações de um oferecimento.
MIN_REQUIRED_SURFACES.update({
    "offerings.create": _BASE_SURFACES | {"body_value", "body_key"},
    "offerings.list": _BASE_SURFACES | {"query_value"},
    "offerings.get": _BASE_SURFACES | {"path"},
    "offerings.close": _BASE_SURFACES | {"path"},
    "offerings.prepare_archive": _BASE_SURFACES | {"path"},
    "offerings.archive": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "bots.register": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "bots.activate": _BASE_SURFACES | {"path"},
    "bots.get": _BASE_SURFACES | {"path"},
    "team.link": _BASE_SURFACES | {"path", "body_value", "body_key"},
})
# Vínculo do aluno: corpo em import, prepare, send e resend; `{ref}` no caminho das 5; nenhuma
# consulta.
MIN_REQUIRED_SURFACES.update({
    "students.import": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "students.summary": _BASE_SURFACES | {"path"},
    "invites.prepare_send": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "invites.send": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "invites.resend": _BASE_SURFACES | {"path", "body_value", "body_key"},
})

# constantes da fase de bot e das folhas do CLI.
BOT_WORKER_ID = "w-privacy-bots"
BOT_OFFERING = "privacidade-bot"
CLI_OFFERING = "cli-oferta-texto"
CLI_OFFERING_JSON = "cli-oferta-json"
CLI_OFFERING_UNREACHABLE = "cli-oferta-inalcancavel"
TEAM_LINK_VALIDITY = timedelta(hours=168)  # padrão de `valid_hours`
ARCHIVE_CODE_VALIDITY = timedelta(minutes=10)  # validade do código de confirmação do arquivamento
# Textos injetados pelos remetentes canário (sem alvos canário: conferido na fase de bot).
INVALID_CODE = "CodigoQueNaoExiste0000"  # 22 caracteres do alfabeto do código, sem convite
PLAIN_TEXT = "mensagem sem comando para o bot"
MENTION_TEXT = "resposta encaminhada ao professor"  # menção `text_mention` sobre "professor"
PREVIOUS_TEXT = "mensagem anterior do chat"
CALLBACK_DATA = "botao-privacidade"
# Token de formato local válido que não existe na Bot API falsa (o `getMe` responde 401).
UNKNOWN_BOT_TOKEN = "1234568:" + "Zz9_-" * 7
TOKEN_REJECTED_MESSAGE = (
    "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.")
OFFERING_BOT_EVENT_TYPES = (
    "offering.created", "offering.state_changed", "offering.archive_prepared", "bot.registered",
    "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed")
OFFERING_BOT_CLI_LEAVES = frozenset({
    "offerings create", "offerings list", "offerings show", "offerings close",
    "offerings archive prepare", "offerings archive confirm", "bots register", "bots activate",
    "bots show", "team link"})
assert re.fullmatch(r"[A-Za-z0-9_-]{22}", INVALID_CODE)
assert MENTION_TEXT[24:33] == "professor"

# constantes da fase de alunos e das folhas do CLI.
STUDENT_WORKER_ID = "w-privacy-students"
STUDENT_OFFERING = "privacidade-alunos"
CLI_STUDENTS_OFFERING = "cli-alunos"
CLI_STUDENTS_DRAFT = "cli-alunos-rascunho"
INVITE_VALIDITY = timedelta(hours=168)  # padrão de `valid_hours`
CONFIRMATION_VALIDITY = timedelta(minutes=10)  # código de confirmação do envio
# Avanço do relógio que libera a 2ª tentativa de uma tarefa (a 1ª espera da fila de tarefas é de 2 s).
RETRY_ADVANCE_SECONDS = 5
# Avanço que vence o convite do autosserviço (validade de 168 h desde a geração do código).
EXPIRY_ADVANCE = INVITE_VALIDITY + timedelta(minutes=1)
OUTSIDE_EMAIL = "fora.da.lista@turma.example.org"  # e-mail neutro, fora da lista
MASKED_CONVITE = "/convite [e-mail]"  # a marca do `/convite` depois do processamento
CONVITE_FAILURE_TRIGGER = "cq_teste_falha_envio_convite"  # gatilho de falha no INSERT de invite_sends
OFFERING_ZONE = "America/Sao_Paulo"
MAIL_FROM_NAME = "Canal de Questões"  # nome do remetente dos e-mails
INVITE_HEADER_NAMES = ("content-transfer-encoding", "content-type", "date", "from", "message-id",
                       "mime-version", "subject", "to")
INVITE_SUBJECT = "Seu link para o bot do oferecimento {course} ({term})"
INVITE_BODY = (
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da disciplina chegam "
    "por um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot "
    "vai mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
    "e-mail institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la."
)
_LINK_SLOT = "\x00link\x00"  # marca de posição do link no corpo esperado (nunca aparece no e-mail)
STUDENT_LINK_EVENT_TYPES = (
    "student.list_imported", "student.invites_prepared", "student.invites_queued", "student.invite_sent",
    "student.invite_failed", "student.invite_cancelled", "student.invite_revoked", "student.joined")
STUDENT_LINK_CLI_LEAVES = frozenset({
    "students import", "students summary", "invites send prepare", "invites send confirm",
    "invites resend"})
# Os 8 desfechos da recepção do vínculo do aluno.
STUDENT_LINK_OUTCOMES = frozenset({
    "student_terms_presented", "student_joined", "student_endpoint_replaced", "student_already_linked",
    "student_refused_invalid", "student_refused_other_person", "student_terms_refused",
    "invite_requested"})
# Os prefixos dos alvos da lista da turma (a exceção de `identities`); os `aluno-tg-*` das contas do
# Telegram não têm exceção em `identities`.
STUDENT_LIST_PREFIXES = ("aluno-nome", "aluno-palavra", "aluno-ra", "aluno-email", "aluno-email-local",
                         "aluno-email-dominio")
assert set(STUDENT_LIST_PREFIXES) < set(STUDENT_PREFIXES)
# As 5 operações do vínculo do aluno. Nenhuma chamada canário delas pode dar um 409 cujo código
# fica fora de `FIELDS_CODES`: o verificador congelado só aceita, no 409, o `list_conflict`.
STUDENT_LINK_OPERATIONS = ("students.import", "students.summary", "invites.prepare_send", "invites.send", "invites.resend")
assert {key[0] for key in EXPECTED_CANARY if key[0] in STUDENT_LINK_OPERATIONS} == set(STUDENT_LINK_OPERATIONS)
assert [key for key, expected in EXPECTED_CANARY.items() if key[0] in STUDENT_LINK_OPERATIONS and isinstance(expected, tuple)
        and expected[0] == 409 and expected[1] not in FIELDS_CODES] == []
assert [expected for key, expected in EXPECTED_CANARY.items() if key[0] in STUDENT_LINK_OPERATIONS
        and not isinstance(expected, tuple)] == [], "toda chamada canário do vínculo é recusada (sem sucesso)"

# Disparo e resposta (tests/helpers/calls.py, as CANARY_CALLS dessa parte): o resultado exato de cada
# uma delas. O nome canário
# no `{ref}`, no `{dispatch_id}` e no cursor `after` é recusado pela forma (400, sem eco); o `repo` com o
# nome canário, ou com o token privado registrado na consulta, é `repo invalid_format`. Nenhuma
# chamada canário do disparo dá 409 (restrição congelada), nem sucesso.
DISPATCH_OPERATIONS = ("dispatches.prepare", "dispatches.get", "dispatches.list", "dispatches.confirm",
                 "dispatches.cancel", "messages.list",
                 # Mensagens livres opcionais: a operação da configuração do oferecimento
                 "offerings.update_settings")
_BAD_REPO = [("repo", "invalid_format")]
_BAD_DISPATCH_ID = [("dispatch_id", "invalid_format")]
_BAD_AFTER = [("after", "invalid_format")]
EXPECTED_CANARY.update({
    ("dispatches.prepare", "body_value"): (400, "invalid_request", _BAD_REPO),
    ("dispatches.prepare", "body_value_secret"): (400, "invalid_request", _BAD_REPO),
    ("dispatches.prepare", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("dispatches.get", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("dispatches.list", "query_value"): (400, "invalid_request", _BAD_AFTER),
    ("dispatches.confirm", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("dispatches.cancel", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("messages.list", "query_value"): (400, "invalid_request", _BAD_AFTER),
})
for _op in DISPATCH_OPERATIONS:
    EXPECTED_CANARY.update({
        (_op, "path"): (400, "invalid_request", _BAD_REF),
        (_op, "header_auth"): _UNAUTHENTICATED,
        (_op, "header_client"): (400, "invalid_request", _BAD_CLIENT),
        (_op, "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    })
# Mensagens livres opcionais (tests/helpers/calls.py, as CANARY_CALLS dessa parte): o
# corpo da operação da configuração (as 4 superfícies globais e o `path` vêm do laço acima) e o contato no cadastro. O
# e-mail **válido** de `STUDENT_A` (aluno canário cadastrado pela fase de alunos, que roda antes da fase da
# API) é recusado pela regra depois do 403 (`invalid_value`, sem eco, nada gravado); o nome canário no
# contato é recusado pela forma (`invalid_format`).
EXPECTED_CANARY.update({
    ("offerings.update_settings", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("offerings.update_settings", "body_value"): (400, "invalid_request", [("contact_email", "invalid_value")]),
    ("offerings.update_settings", "body_value_invalid"): (
        400, "invalid_request", [("contact_email", "invalid_format")]),
    ("offerings.create", "body_value_contact"): (400, "invalid_request", [("contact_email", "invalid_value")]),
    ("offerings.create", "body_value_contact_invalid"): (
        400, "invalid_request", [("contact_email", "invalid_format")]),
})
# Disparo e resposta: corpo só no preparo; consulta em `dispatches.list` e `messages.list`; `{ref}` no
# caminho das 6 (e `{dispatch_id}` em get, confirm e cancel, coberto pelo `path_dispatch`).
MIN_REQUIRED_SURFACES.update({
    "dispatches.prepare": _BASE_SURFACES | {"path", "body_value", "body_key"},
    "dispatches.get": _BASE_SURFACES | {"path"},
    "dispatches.list": _BASE_SURFACES | {"path", "query_value"},
    "dispatches.confirm": _BASE_SURFACES | {"path"},
    "dispatches.cancel": _BASE_SURFACES | {"path"},
    "messages.list": _BASE_SURFACES | {"path", "query_value"},
})
# Mensagens livres opcionais: corpo e `{ref}` no caminho, sem consulta. O piso só tem as superfícies que
# o OpenAPI publicado exige (`test_canary_surfaces_cover_published_openapi`); as chamadas a mais
# (`body_value_invalid` aqui e `body_value_contact*` no cadastro) entram pela igualdade das chamadas executadas
# com `EXPECTED_CANARY` (`_api_phase`).
MIN_REQUIRED_SURFACES.update({
    "offerings.update_settings": _BASE_SURFACES | {"path", "body_value", "body_key"},
})
assert {key[0] for key in EXPECTED_CANARY if key[0] in DISPATCH_OPERATIONS} == set(DISPATCH_OPERATIONS)
assert [key for key, expected in EXPECTED_CANARY.items() if key[0] in DISPATCH_OPERATIONS and isinstance(expected, tuple)
        and expected[0] == 409 and expected[1] not in FIELDS_CODES] == [], "chamada canário do disparo com 409"
assert [expected for key, expected in EXPECTED_CANARY.items() if key[0] in DISPATCH_OPERATIONS
        and not isinstance(expected, tuple)] == [], "toda chamada canário do disparo é recusada (sem sucesso)"
# Disparo e resposta: o nome, o RA e o e-mail dos alunos canário do disparo, que nenhuma resposta das
# CANARY_CALLS pode conter.
ECHO_SENTINELS += tuple(value for student in CANARY_DISPATCH_STUDENTS
                        for value in (student.name, student.ra, student.email))

# constantes da fase de disparos e das folhas do CLI.
DISPATCH_EVENT_TYPES = (
    "dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed", "dispatch.cancelled",
    "dispatch.opened", "dispatch.closed", "delivery.delivered", "delivery.not_delivered",
    "delivery.question_failed", "delivery.dont_know", "submission.graded", "submission.internal_failure",
    # Mensagens livres opcionais: a alteração da configuração (o `PATCH` do passo 5.9 da fase)
    "offering.settings_changed")
DISPATCH_CLI_LEAVES = frozenset({"dispatches prepare", "dispatches show", "dispatches list", "dispatches confirm",
                           "dispatches cancel", "messages list",
                           # a folha da configuração do oferecimento
                           "offerings settings"})
# Os desfechos da recepção que a fase de disparos produz.
DISPATCH_OUTCOMES = frozenset({"answer_recorded", "answer_part_recorded", "answer_dont_know", "answer_refused",
                         "answer_stale", "answer_not_text", "free_message", "free_message_ambiguous",
                         # Mensagens livres: os dois do modo desligado do passo 5.9 (os dois ambíguos mascarados
                         # ficam em `test_free_messages.py`)
                         "free_message_masked_replied", "free_message_masked"})
DISPATCH_WORKER_ID = "w-privacy-dispatches"
DISPATCH_OFFERING = "privacidade-disparo"
DISPATCH_COURSE = "Algoritmos e Programação"  # neutra: a redação da disciplina vem dos oferecimentos
DISPATCH_TERM = "2031s1"
DISPATCH_ORIGIN = "api"
# A questão da fase: um repositório no host de `TEST_REPO_SETTINGS` (a fixture `app`), um commit SHA-1 e os
# QIDs dos presets do motor falso, que existem na árvore `course` da obtenção falsa.
# O caminho não leva a palavra "canario" (alvo `palavra:canario` de `ZEFIRO`).
DISPATCH_REPO = "https://repos.cq.test/privacidade/questoes-disparo.git"
DISPATCH_COMMIT = "c4a7" * 10
DISPATCH_WINDOW = timedelta(minutes=10)  # o fechamento informado dos disparos imediatos da fase
DISPATCH_TIMEOUT = 20.0  # prazo real de cada espera da fase (inclui as etapas da validação)
DISPATCH_CLOSE_TIMEOUT = 60.0  # prazo real do laço do fechamento
DISPATCH_ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
DISPATCH_DELIVERY = DeliverySettings(chat_interval_seconds=0.05)  # ritmo curto
# O texto de identidade de `DISPATCH_B` na questão canário e no motivo do motor e o que
# `DISPATCH_A` digita: a resposta (o eco volta ao próprio chat de A) e as mensagens livres,
# com o nome, o RA, o e-mail, o `@username`, o nome e o id do Telegram de B. A resposta cabe nos 200
# caracteres do eco, para que o eco leve a identidade inteira.
DISPATCH_IDENTITY = f"{DISPATCH_B.name} (RA {DISPATCH_B.ra}, {DISPATCH_B.email})"
DISPATCH_TG_IDENTITY = f"@{TG_DISPATCH_B.username} {TG_DISPATCH_B.full_name} {TG_DISPATCH_B.user_id}"
DISPATCH_ANSWER = f"{DISPATCH_IDENTITY} {DISPATCH_TG_IDENTITY}"
DISPATCH_FREE_TEXTS = (f"Oi, a {DISPATCH_B.name} (RA {DISPATCH_B.ra}, {DISPATCH_B.email}) pediu ajuda.",
                       f"No Telegram ela é {DISPATCH_TG_IDENTITY}.")
DISPATCH_UNSUPPORTED = f"A questão de {DISPATCH_IDENTITY} usa o elemento pl-matrix-input, que o canal não suporta."
# A mensagem livre de A com dois campos digitáveis abertos (o 3º e o 4º disparos): o "use responder".
DISPATCH_AMBIGUOUS = "Qual das duas questões eu respondo agora?"
# A correção presa no motor falso (tempo real) enquanto A toca "Não sei" (a recusa `grading_pending`): cabe
# folgada no prazo do adaptador (`DISPATCH_ENGINE_TIMEOUT`), então a correção nunca vira `EngineUnavailable`.
DISPATCH_GRADE_HOLD = 6.0
DISPATCH_REJECTION_MESSAGE = "A questão usa recursos que o sistema não suporta."  # a mensagem fixa da rejeição
DISPATCH_ZERO_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
                        "no_answer": 0, "answered_late": 0, "question_failed": 0}
# Mensagens livres opcionais: o e-mail do professor canário, cadastrado como contato do oferecimento da fase de
# disparos. Fica fora de todos os prefixos canário (conferido no começo da fase): só pode aparecer em
# `offering_contacts.email`, no `outbox_messages.text` da resposta do modo desligado e no `sendMessage` dela ao
# chat de A (`_check_professor_email`). O endereço evita a palavra "canario", porque ela é
# alvo `palavra:canario` de `ZEFIRO`, e o endereço precisa ficar fora de
# `ALL_PREFIXES` e de `STUDENT_PREFIXES`.
PROFESSOR_EMAIL = "professora.contato@docentes.example.org"
# Passo 5.9: as duas mensagens livres de A com as mensagens livres desligadas, com a identidade de B
# (a da lista na 1ª, a do Telegram na 2ª); nenhuma vira registro, e o texto do update é mascarado.
DISPATCH_FREE_OFF_TEXTS = (f"Professor, a {DISPATCH_B.name} (RA {DISPATCH_B.ra}, {DISPATCH_B.email}) está sem acesso.",
                           f"O Telegram dela é {DISPATCH_TG_IDENTITY}.")
# Nota parcial (passo 5.6b): a resposta digitada de A ao 5º disparo (a múltipla degradada, gabarito a, b, d, f, j, l,
# `net-correct`): a e b certas, c errada → (2 − 1)/6, a nota parcial 1/6 ("◐", "nota 16%").
DISPATCH_D5_ANSWER = "abc"
DISPATCH_D5_SCORE = 1 / 6
# Os prefixos dos alvos da lista do disparo (a exceção de `identities`); os `disparo-tg-*` não têm exceção lá.
DISPATCH_LIST_PREFIXES = ("disparo-nome", "disparo-palavra", "disparo-ra", "disparo-email", "disparo-email-local",
                    "disparo-email-dominio")
assert set(DISPATCH_LIST_PREFIXES) < set(DISPATCH_PREFIXES)
# A lista fechada (com `submissions.partial_scores`) das colunas em que
# o texto digitado pelo aluno pode ficar.
DISPATCH_STUDENT_TEXT_COLUMNS = frozenset({
    ("telegram_updates", "text"),
    ("submissions", "raw_input"), ("submissions", "submitted_answers"), ("submissions", "format_errors"),
    ("submissions", "feedback"), ("submissions", "params"), ("submissions", "partial_scores"),
    ("delivery_contents", "parts"), ("delivery_contents", "current_render"),
})
# Os métodos de envio e de edição da Bot API: só o `chat_id` deles leva o id do Telegram do aluno.
DISPATCH_CHAT_METHODS = frozenset({"sendMessage", "sendRichMessage", "editMessageText", "editMessageReplyMarkup",
                             "sendPhoto", "sendDocument"})
# Os textos do bot nas entregas, conferidos
# assim: toda constante e todo modelo da lista de textos do bot sai ao menos uma vez na fase de disparos,
# numa mensagem, numa edição ou numa confirmação de toque do bot da fase, salvo as exceções declaradas por
# nome em `DISPATCH_TEXT_EXCEPTIONS`. Os modelos entram pelo começo fixo (`FORMAT_ERROR`: a explicação vem da
# tradução do motor) ou preenchidos com o valor que a fase produz (`MEDIA_UNAVAILABLE`: a figura 1 do
# 4º disparo, ausente). As funções (`feedback`, `progress`, `field_request`, `deadline`) entram pelos trechos
# fixos (`DISPATCH_PHASE_PATTERNS`); os rótulos dos botões, pelos teclados (`DISPATCH_PHASE_BUTTONS`);
# o comando do "Não sei" digitado, pelo texto gravado do update (`DISPATCH_PHASE_COMMANDS`).
# Também: a `TYPED_MULTIPLE_HOWTO` (a adaptação de canal da múltipla
# digitada) sai na fase, na mensagem da questão do 5º disparo (múltipla degradada: mais alternativas que
# `max_buttons`). Nada aqui lê `deliverytexts` na importação: a conferência dos nomes e a composição dos
# textos ficam dentro do teste (`_check_dispatch_text_names`, `_dispatch_phase_texts`), para que um nome da lista ainda
# ausente (ou um detalhe interno do módulo) nunca quebre a coleta deste arquivo.
# `WINDOW_CLOSED` fica sem gatilho, porque o "Não sei" depois do fechamento não é
# recusado: vai para `DISPATCH_TEXT_EXCEPTIONS`; `VERDICT_PARTIAL` e `SCORE` saem no 5º disparo respondido com nota
# parcial; `ANSWERS_ONLY_REPLY` e `CONTACT_HOWTO` são a resposta do modo desligado do passo 5.9, com as mensagens
# livres opcionais.
DISPATCH_PHASE_TEXT_NAMES = (
    "FREE_MESSAGE_REPLY", "ALREADY_ANSWERED", "DONT_KNOW_RECORDED", "ANSWER_KEPT", "QUESTION_FAILED",
    "CLOSED_NOTICE", "VERDICT_INCORRECT", "VERDICT_CORRECT", "GRADING_IN_PROGRESS", "UNAVAILABLE",
    "PART_ALREADY_ANSWERED", "REPLY_WITH_TEXT", "LATE_HOWTO", "USE_REPLY", "FORMAT_ERROR",
    "MEDIA_UNAVAILABLE", "TYPED_MULTIPLE_HOWTO",
    "VERDICT_PARTIAL", "SCORE", "ANSWERS_ONLY_REPLY", "CONTACT_HOWTO")
DISPATCH_PHASE_BUTTONS = ("DONT_KNOW_BUTTON", "SUBMIT_BUTTON")
DISPATCH_PHASE_COMMANDS = ("DONT_KNOW_COMMAND",)
# As exceções, por nome, com o motivo (o texto sai no teste próprio indicado).
DISPATCH_TEXT_EXCEPTIONS = {
    # Tempo real: o aviso só sai com a correção sem resposta do motor (fora do ar ou além do prazo do
    # adaptador), e o número de rodadas até ele depende de tempo real.
    "GRADING_DELAYED": "tempo real",
    # Canal sem botões: o anúncio do `/naosei` só sai com `max_buttons = 0` (o
    # console); o bot do Telegram sempre mostra o botão "Não sei" (`DONT_KNOW_BUTTON`, conferido aqui). Sai na
    # ponta a ponta do console.
    "DONT_KNOW_HOWTO": "canal sem botões",
    # O "Não sei" depois do fechamento é aceito, e o
    # texto fica na tabela sem uso.
    "WINDOW_CLOSED": "sem gatilho: o 'Não sei' depois do fechamento é aceito",
    # Mensagens livres: a frase só sai no modo desligado a quem tem aberta só escolha com botões e nenhum campo
    # digitável; no passo 5.9, A não tem entrega aberta. Sai em `test_free_messages.py`.
    "USE_BUTTONS_HOWTO": "modo desligado com escolha com botões aberta",
}
DISPATCH_PHASE_PATTERNS = {
    "feedback": r"Sua resposta: ",  # "Sua resposta: {eco} · {veredito}"
    "progress": r"Parte [12] de 2",  # "Parte {k} de {n}", no 4º disparo (duas partes)
    "field_request": r"Campo 1 de [12]: .+ — digite a resposta\.",  # "Campo {k} de {n}: {rótulo}{sufixo} — …"
    "deadline": r"Prazo: \S",  # "Prazo: {data e hora no fuso do oferecimento}"
}
# Os nomes dos textos do bot, como constante ou modelo de texto (os 4 nomes restantes,
# `FEEDBACK`, `PROGRESS`, `FIELD_REQUEST` e `DEADLINE`, são as funções de `DISPATCH_PHASE_PATTERNS`, com os nomes do
# esqueleto). A conferência contra o vazio vale só para eles: um nome privado (com `_`), uma
# constante auxiliar ou uma função pública nova do módulo não têm de ter destino aqui.
DISPATCH_TABLE_TEXT_NAMES = (
    "ALREADY_ANSWERED", "GRADING_DELAYED", "GRADING_IN_PROGRESS", "ANSWER_KEPT", "QUESTION_FAILED", "UNAVAILABLE",
    "PART_ALREADY_ANSWERED", "REPLY_WITH_TEXT", "WINDOW_CLOSED", "DONT_KNOW_RECORDED", "FORMAT_ERROR",
    "CLOSED_NOTICE", "LATE_HOWTO", "MEDIA_UNAVAILABLE", "FREE_MESSAGE_REPLY", "USE_REPLY", "DONT_KNOW_HOWTO",
    "TYPED_MULTIPLE_HOWTO",
    # Os textos da nota parcial (as três faixas e a nota) e das mensagens livres
    # opcionais (a resposta do modo desligado e a frase de usar os botões).
    "VERDICT_CORRECT", "VERDICT_INCORRECT", "VERDICT_PARTIAL", "SCORE", "ANSWERS_ONLY_REPLY", "CONTACT_HOWTO",
    "USE_BUTTONS_HOWTO")
# As 13 alternativas da múltipla degradada do 5º disparo (a chave e um texto neutro): com o "Não sei" (e o
# "Enviar"), passam de `max_buttons` do Telegram (padrão 12, `DeliverySettings`) em qualquer contagem dos
# controles, e o campo degrada para a resposta digitada.
DISPATCH_MANY_OPTIONS = tuple(FakeOption(key, text) for key, text in zip("abcdefghijklm", (
    "Dois", "Três", "Quatro", "Cinco", "Seis", "Sete", "Oito", "Nove", "Dez", "Onze", "Doze", "Treze", "Catorze")))


def _phase_text(name: str) -> str:
    value = getattr(deliverytexts, name)
    if name == "FORMAT_ERROR":
        return value.split("{", 1)[0]
    if name == "MEDIA_UNAVAILABLE":
        return value.format(n=1)
    if name == "SCORE":  # a nota parcial do 5º disparo, ⌊(1/6) × 100⌋
        return value.format(n=16)
    if name == "CONTACT_HOWTO":  # o contato do oferecimento da fase
        return value.format(email=PROFESSOR_EMAIL)
    return value


def _dispatch_phase_texts() -> tuple[str, ...]:
    """O texto (ou o começo fixo) de cada nome de `DISPATCH_PHASE_TEXT_NAMES`, lido do `deliverytexts` na hora."""
    texts = tuple(_phase_text(name) for name in DISPATCH_PHASE_TEXT_NAMES)
    assert "{" not in "".join(texts) and all(texts), texts
    return texts


def _check_dispatch_text_names() -> None:
    """Contra o vazio: toda constante e todo modelo da
    lista de textos do bot tem destino no canário (sai na fase ou é exceção por nome); todo nome com destino
    é público e existe no `deliverytexts` como texto não vazio; toda função da lista existe e é chamável. Não
    exige igualdade com o conteúdo do módulo (nomes privados, constantes e funções auxiliares ficam livres)."""
    destinations = set(DISPATCH_PHASE_TEXT_NAMES) | set(DISPATCH_PHASE_BUTTONS) | set(DISPATCH_PHASE_COMMANDS) | set(DISPATCH_TEXT_EXCEPTIONS)
    assert set(DISPATCH_PHASE_TEXT_NAMES).isdisjoint(DISPATCH_TEXT_EXCEPTIONS)
    assert sorted(set(DISPATCH_TABLE_TEXT_NAMES) - destinations) == [], "texto da lista do bot sem destino no canário"
    assert [name for name in destinations if name.startswith("_")] == []
    missing = sorted(name for name in destinations
                     if not (isinstance(getattr(deliverytexts, name, None), str) and getattr(deliverytexts, name)))
    assert missing == [], f"texto do bot ausente em deliverytexts: {missing}"
    assert sorted(name for name in DISPATCH_PHASE_PATTERNS if not callable(getattr(deliverytexts, name, None))) == [], (
        "função da lista de textos ausente em deliverytexts")
    _dispatch_phase_texts()


# ---------------------------------------------------------------------------------------------
# Relatórios e acompanhamento: constantes das chamadas canário, da fase de
# relatórios e das folhas do CLI (fora das contagens globais)
# ---------------------------------------------------------------------------------------------

# O resultado exato de cada CANARY_CALL de relatórios (tests/helpers/calls.py, as CANARY_CALLS dessa parte): o
# nome canário no `{ref}`, no `{dispatch_id}`, na consulta (`version`, `format`, `after`) e no corpo
# (`local_time`) é recusado pela forma (400, sem eco), e as superfícies globais como antes. Nenhuma chamada
# canário de relatórios dá 409 fora de `FIELDS_CODES` (nem 409 nenhum), nem sucesso, nem 404 da regra.
REPORT_OPERATIONS = ("reports.get", "reports.list_versions", "reports.resend_summary", "offerings.get_digest",
                 "offerings.update_digest")
_BAD_VERSION = [("version", "invalid_format")]
_BAD_FORMAT = [("format", "invalid_value")]
_BAD_LOCAL_TIME = [("local_time", "invalid_format")]
EXPECTED_CANARY.update({
    ("reports.get", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("reports.get", "query_value"): (400, "invalid_request", _BAD_VERSION),
    ("reports.get", "query_value_format"): (400, "invalid_request", _BAD_FORMAT),
    ("reports.list_versions", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("reports.list_versions", "query_value"): (400, "invalid_request", _BAD_AFTER),
    ("reports.resend_summary", "path_dispatch"): (400, "invalid_request", _BAD_DISPATCH_ID),
    ("offerings.update_digest", "body_value"): (400, "invalid_request", _BAD_LOCAL_TIME),
    ("offerings.update_digest", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
})
for _op in REPORT_OPERATIONS:
    EXPECTED_CANARY.update({
        (_op, "path"): (400, "invalid_request", _BAD_REF),
        (_op, "header_auth"): _UNAUTHENTICATED,
        (_op, "header_client"): (400, "invalid_request", _BAD_CLIENT),
        (_op, "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    })
# Relatórios: consulta em `reports.get` e `reports.list_versions`; corpo só em `update_digest` (o
# reenvio não tem corpo, como `dispatches.confirm`); `{ref}` no caminho das 5 (e `{dispatch_id}` nas 3 de
# relatório, coberto pelo `path_dispatch`).
MIN_REQUIRED_SURFACES.update({
    "reports.get": _BASE_SURFACES | {"path", "query_value"},
    "reports.list_versions": _BASE_SURFACES | {"path", "query_value"},
    "reports.resend_summary": _BASE_SURFACES | {"path"},
    "offerings.get_digest": _BASE_SURFACES | {"path"},
    "offerings.update_digest": _BASE_SURFACES | {"path", "body_value", "body_key"},
})
assert {key[0] for key in EXPECTED_CANARY if key[0] in REPORT_OPERATIONS} == set(REPORT_OPERATIONS)
assert [key for key, expected in EXPECTED_CANARY.items() if key[0] in REPORT_OPERATIONS and isinstance(expected, tuple)
        and expected[0] == 409 and expected[1] not in FIELDS_CODES] == [], "chamada canário de relatórios com 409"
assert [expected for key, expected in EXPECTED_CANARY.items() if key[0] in REPORT_OPERATIONS
        and not (isinstance(expected, tuple) and expected[0] in (400, 401))] == [], (
    "toda chamada canário de relatórios é recusada pela forma (400) ou pela autenticação (401)")

# Relatórios e acompanhamento: os 3 tipos de evento, as 5 folhas, os 10 tipos de aviso, os
# estados finais e os 2 desfechos da recepção; as 7 tabelas.
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
REPORT_CLI_LEAVES = frozenset({"reports show", "reports versions", "reports resend", "offerings digest show",
                           "offerings digest set"})
REPORT_OUTCOMES = frozenset({"preview_tapped", "preview_replied"})
REPORT_NOTICE_KINDS = ("dispatch_preview", "dispatch_rejected", "dispatch_opened", "dispatch_closed", "summary_resent",
                   "dispatch_cancelled", "question_failed", "bot_access_restored", "invite_failures",
                   "daily_digest")
REPORT_NOTICE_FINAL_STATES = ("queued", "no_recipient", "no_activity", "missed", "not_sent", "gave_up")
REPORT_TABLES = ("dispatch_reports", "team_notices", "team_notice_floors", "dispatch_previews",
             "dispatch_preview_contents", "offering_digests", "invite_batches")
# Relatórios: as colunas da amostra da prévia, a exceção nomeada das varreduras do banco
# (a IR literal da validação e a renderização entregue à equipe).
REPORT_SAMPLE_COLUMNS = (("dispatch_preview_contents", "ir"), ("dispatch_preview_contents", "rendered"))
# Os textos de `noticetexts` com destino no canário: cada um sai ao menos uma vez nas
# mensagens à equipe da fase de relatórios (ou na confirmação do toque) ou é exceção por nome, com o motivo; os
# dicionários entram valor a valor (`NOME[chave]`). A conferência dos nomes roda dentro do teste.
REPORT_TEXT_NAMES = ("PREVIEW_HEADER", "PREVIEW_IMMEDIATE", "PREVIEW_PARTS", "PREVIEW_FIELD_HINT", "PREVIEW_TAP",
                 "PREVIEW_REPLY", "REJECTED", "OPENED", "CANCELLED", "CANCEL_REASONS[expired]", "QUESTION_FAILED",
                 "CLOSED_SUMMARY", "RESENT_SUMMARY", "ACCESS_RESTORED", "DAILY_DIGEST", "DIGEST_DISPATCH_LINE",
                 "PREVIEW_ENGINE_WARNINGS[console_output]", "EMPTY", "UNTITLED")
REPORT_TEXT_EXCEPTIONS = {
    # Só com uma entrega `pending` no "aberto" (os 10 min da regra; tempo real do envio): test_notices_dispatch.
    "OPENED_PENDING": "pendentes no aviso aberto",
    # Só com correção pendente no congelamento (a espera de até 10 min): test_reports_freeze.
    "GRADING_PENDING_LINE": "correção pendente no resumo",
    # A fase provoca o cancelamento por `expired`; os outros dois motivos automáticos ficam nos testes do aviso.
    "CANCEL_REASONS[window_missed]": "janela perdida na abertura",
    "CANCEL_REASONS[validation_overran]": "validação além da abertura",
    # A questão canário só traz o aviso `console_output` do motor.
    "PREVIEW_ENGINE_WARNINGS[missing_media]": "figura ausente na prévia",
    "PREVIEW_ENGINE_WARNINGS[media_error]": "figura com erro na prévia",
    # Um convite com falha na fase de relatórios mudaria os registros `invite.email` que `_check_student_logs`
    # (congelado) confere exatamente; o tipo `invite_failures` sai pela janela da fase de alunos (sem
    # destinatário), e os dois textos, nos testes dos avisos de convite.
    "INVITE_FAILURES_BATCH": "falha de convite em lote",
    "INVITE_FAILURES_WINDOW": "falha de convite na janela",
}
REPORT_WORKER_ID = "w-privacy-reports"
REPORT_CONSOLE_WORKER_ID = "w-privacy-reports-console"
REPORT_OFFERING = "privacidade-relatorio"
REPORT_ARCHIVE_OFFERING = "privacidade-relatorio-arquivo"
REPORT_REPO = "https://repos.cq.test/privacidade/questoes-relatorio.git"
REPORT_COMMIT = "e6d6" * 10
REPORT_WINDOW = timedelta(minutes=10)  # o fechamento informado dos disparos imediatos da fase
REPORT_LEAD = timedelta(minutes=10)  # a abertura do disparo agendado que vence sem confirmação
REPORT_TIMEOUT = 30.0  # prazo real de cada espera da fase
REPORT_PREVIEW_HOLD = 4.0  # a `create_variant` da prévia presa no motor (abaixo do prazo do adaptador, 10 s)
REPORT_MORNING = 9  # a hora local em que a fase começa (o resumo diário cai no mesmo dia dos disparos)
REPORT_TEAM_RAS = ("8800007101", "8800007102")
REPORT_TEAM_REPLY = "Recebi a prévia, obrigado."
REPORT_DIGEST_LATER = "21:00"
REPORT_UNKNOWN_OFFERING = "relatorio-inexistente"
REPORT_ACTIVITY_LEAD = timedelta(minutes=5)  # o horário do resumo diário, depois do último disparo do dia
# A questão canário da fase: a identidade de `DISPATCH_B` no título, no enunciado e numa alternativa
# (a escolha única), com o nome na dica e o aviso `console_output` do motor (preset `canary`).
REPORT_CANARY_OPTIONS = (FakeOption("a", f"A opção de {DISPATCH_IDENTITY}"), FakeOption("b", "Outra opção"),
                         FakeOption("c", "Nenhuma das anteriores"))
# O disparo agendado da fase: duas partes (a 1ª, escolha com botões; a prévia mostra só ela, `PREVIEW_PARTS`).
REPORT_TWO_PARTS = FakeQuestion("Cor e velocidade", "Responda às duas partes.",
                                (single_choice_field("cor"), number_field("velocidade")))

# CLI (operação e expurgo): as 3 operações,
# os 2 tipos de evento, as 3 folhas do catálogo e os 4 comandos locais (`LOCAL_COMMANDS`, fora do catálogo).
# O resultado exato de cada CANARY_CALL do CLI, fixado por caso (tests/helpers/calls.py): o nome
# canário no `{ref}` → 400 `[{ref, invalid_format}]`; no `confirmation_code` (formato válido) sobre o 2º alvo
# preparado → **409 `invalid_confirmation`**, sem eco e sem mudança de estado (a única recusa canário do CLI com 409);
# no valor de `person` → 400 `[{person, invalid_format}]`; numa consulta do `doctor` → 400 `[{query, unknown_field}]`.
from cq_client.hint import HINT_TEMPLATE
from cq_client.local_commands import LOCAL_COMMANDS
from tests.helpers.canary import (CANARY_PURGE_PEOPLE, PURGE_CONTACT_CURRENT, PURGE_CONTACT_OLD, PURGE_D,
                                  PURGE_PREFIXES)
from tests.helpers.cli_runner import UNREACHABLE_API_URL, align_clock_to_db, make_stack_dir, write_profile
from tests.helpers.stack_fake import ENGINE_IMAGE_ID, TEST_API_URL, FakeStack

DOCTOR_PURGE_OPERATIONS = ("system.doctor", "offerings.prepare_purge", "offerings.purge")
PURGE_EVENT_TYPES = ("offering.purge_prepared", "offering.purged")
DOCTOR_PURGE_CLI_LEAVES = frozenset({"doctor", "offerings purge prepare", "offerings purge confirm"})
_INVALID_CONFIRMATION = "invalid-confirmation"  # o 409 `invalid_confirmation`, sem `fields` (fora de FIELDS_CODES)
EXPECTED_CANARY.update({
    ("system.doctor", "header_auth"): _UNAUTHENTICATED,
    ("system.doctor", "header_client"): (400, "invalid_request", _BAD_CLIENT),
    ("system.doctor", "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    ("offerings.purge", "body_value"): _INVALID_CONFIRMATION,
    ("offerings.purge", "body_key"): (400, "invalid_request", _UNKNOWN_BODY),
    ("events.list", "query_value_person"): (400, "invalid_request", [("person", "invalid_format")]),
})
for _op in ("offerings.prepare_purge", "offerings.purge"):
    EXPECTED_CANARY.update({
        (_op, "path"): (400, "invalid_request", _BAD_REF),
        (_op, "header_auth"): _UNAUTHENTICATED,
        (_op, "header_client"): (400, "invalid_request", _BAD_CLIENT),
        (_op, "query_key"): (400, "invalid_request", _UNKNOWN_QUERY),
    })
# CLI: sem parâmetro no `doctor`; `{ref}` no caminho do preparo e da confirmação; corpo só na
# confirmação. A consulta `person` de `events.list` entra pela igualdade das chamadas executadas com
# `EXPECTED_CANARY` (`_api_phase`), como as chamadas a mais das mensagens livres: o piso só tem o que o OpenAPI exige.
MIN_REQUIRED_SURFACES.update({
    "system.doctor": set(_BASE_SURFACES),
    "offerings.prepare_purge": _BASE_SURFACES | {"path"},
    "offerings.purge": _BASE_SURFACES | {"path", "body_value", "body_key"},
})
assert {key[0] for key in EXPECTED_CANARY if key[0] in DOCTOR_PURGE_OPERATIONS} == set(DOCTOR_PURGE_OPERATIONS)
assert [key for key, expected in EXPECTED_CANARY.items() if key[0] in DOCTOR_PURGE_OPERATIONS
        and expected == _INVALID_CONFIRMATION] == [("offerings.purge", "body_value")], "o único 409 canário do CLI"
assert [expected for key, expected in EXPECTED_CANARY.items() if key[0] in DOCTOR_PURGE_OPERATIONS
        and expected != _INVALID_CONFIRMATION and not (isinstance(expected, tuple) and expected[0] in (400, 401))
        ] == [], "toda outra chamada canário do CLI é recusada pela forma (400) ou pela autenticação (401)"

# CLI e expurgo: esta fase roda num banco próprio (`db_factory`), com app, cliente, oferecimentos, bots
# e equipe próprios (a semente `seed_purge_scenario`, com as pessoas canário do expurgo, `PURGE_PREFIXES`).
# Por escolha deste teste: fora do banco do canário e depois do bloco de captura das fases anteriores,
# para que as contagens exatas congeladas (`tasks`, `outbox_messages`, `telegram_updates`, eventos por fase) e a
# igualdade congelada entre os registros `request` e as trocas do `client` não mudem; os registros e as trocas da
# fase têm captura e lista próprias, varridas pelos mesmos detectores em `_check_cli_surfaces`. No banco do canário,
# o oferecimento expurgado e o alvo preparado vêm do `_api_phase` (os `purge_targets`).
CLI_REMOTE_URL = "https://cq.example.org"
PURGE_WRONG_CODE = "CodigoQueNaoExiste0000"  # 22 caracteres do alfabeto do código, nunca emitido
DOCTOR_ENGINE_DETAIL = ENGINE_IMAGE_ID.removeprefix("sha256:")[:12]


# ---------------------------------------------------------------------------------------------
# Gravação das respostas da API e varreduras
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Exchange:
    """Uma resposta da API vista pelo cliente HTTP do teste (corpo e cabeçalhos, como texto)."""

    method: str
    path: str
    status: int
    headers: str
    body: str
    # A URL do pedido, varrida por segredos (nunca um token na URL).
    url: str = ""

    @property
    def is_reveal_success(self) -> bool:
        return self.method == "POST" and self.path == "/v1/identity/reveal" and self.status == 200

    @property
    def is_token_creation(self) -> bool:
        return self.method == "POST" and self.path == "/v1/tokens" and self.status == 201

    @property
    def is_team_link_creation(self) -> bool:
        """o 201 de `team.link`, a única resposta que pode conter o link."""
        return (self.method == "POST" and self.status == 201
                and re.fullmatch(r"/v1/offerings/[^/]+/team-links", self.path) is not None)


def _own_link_secrets(ex: _Exchange) -> set[str]:
    """o link e o código de convite do próprio 201 de `team.link` (a exceção exata do link de equipe).

    Um corpo sem link no padrão do contrato não tem exceção nenhuma (e o esquema o recusa).
    """
    try:
        link = json.loads(ex.body).get("link")
    except (ValueError, AttributeError):
        return set()
    match = TEAM_LINK_SHAPE.fullmatch(link) if isinstance(link, str) else None
    return set() if match is None else {link, match.group(2)}


def _record_exchanges(http: Any, sink: list[_Exchange]) -> None:
    """Grava toda resposta que passa pelo cliente HTTP (API, helpers e CLI em processo).

    O segredo de todo 201 de `tokens.create` entra no registro de segredos, inclusive os criados
    pelas CANARY_CALLS e pelo CLI.
    """

    def hook(response: Any) -> None:
        response.read()
        request = response.request
        sink.append(_Exchange(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            headers="\n".join(f"{k}: {v}" for k, v in response.headers.multi_items()),
            body=response.text,
            url=str(request.url),
        ))
        if request.method == "POST" and request.url.path == "/v1/tokens" and response.status_code == 201:
            remember_secret(response.json()["secret"])
        # o link e o código de todo 201 de `team.link` (inclusive os do CLI) entram no registro.
        if sink[-1].is_team_link_creation:
            for secret in sorted(_own_link_secrets(sink[-1])):
                remember_secret(secret)

    hooks = http.event_hooks
    http.event_hooks = {"request": list(hooks["request"]), "response": [*hooks["response"], hook]}


def _exchange_canary_hits(ex: _Exchange) -> list[str]:
    """Alvos canário no cabeçalho e no corpo; só o corpo do 200 da revelação fica de fora."""
    hits = set(find_canary(ex.headers))
    if not ex.is_reveal_success:
        hits.update(find_canary(ex.body))
    return sorted(hits)


def _exchange_secret_hits(ex: _Exchange, secrets: list[str]) -> list[str]:
    """Segredos no cabeçalho e no corpo; só o corpo do 201 de `tokens.create` fica de fora."""
    hits = set(find_secrets(ex.headers, secrets))
    if ex.is_team_link_creation:
        # No corpo do 201 de `team.link` ficam de fora só o próprio link e o seu
        # código de convite (igualdade exata); qualquer outro segredo continua acusado.
        own = _own_link_secrets(ex)
        secrets = [secret for secret in secrets if secret not in own]
    if not ex.is_token_creation:
        hits.update(find_secrets(ex.body, secrets))
    return sorted(hits)


def _scan_table(sql: psycopg.Connection, table: str, columns: list[str]) -> tuple[int, dict[str, set[str]]]:
    """Varredura SQL do canário: devolve (linhas lidas, {coluna: alvos achados})."""
    rows = sql.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    hits: dict[str, set[str]] = {}
    for row in rows:
        for column, value in zip(columns, row, strict=True):
            if value is None:
                continue
            found = find_canary(str(value))
            if found:
                hits.setdefault(column, set()).update(found)
    return len(rows), hits


def _has_record(records: list[dict], expected: dict) -> bool:
    return any(expected.items() <= record.items() for record in records)


def _only(items: list[dict], what: str) -> dict:
    assert len(items) == 1, f"esperado exatamente um {what}; achados: {items}"
    return items[0]


def _wait_until(predicate: Callable[[], bool], timeout: float, what: str) -> None:
    """Espera real por uma thread, com prazo (o resultado vem do worker, não do relógio)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"prazo de {timeout} s esgotado: {what}"
        time.sleep(0.05)


def _check_process_output(capfd: Any, caught: list[warnings.WarningMessage], secrets: list[str],
                          probe_secret: str, invite_digests: Collection[str] = ()) -> None:
    """Varre a saída padrão e a de erro do processo e os avisos emitidos durante o teste.

    `capfd` pega os fd 1 e 2 desde a montagem das fixtures (um `print`, um `traceback.print_exc()` ou
    um `sys.stderr.write` da API, do worker ou das threads); nada é descartado antes da varredura.
    `caught` tem os avisos gravados sob os filtros do pytest, que continuam valendo (ex.: um aviso
    de serialização que carregue o valor de entrada). Depois, os controles positivos provam que o
    detector acharia um vazamento por esses canais: o nome canário e `probe_secret` escritos em
    `sys.stderr` e direto no fd 2, e o nome num `warnings.warn`. No fim, os avisos gravados são
    reemitidos para o resumo do pytest.

    Vínculo do aluno: as mesmas saídas e os avisos também não podem ter alvo de
    aluno canário (`STUDENT_PREFIXES`) nem, com `invite_digests` (os resumos de `invites.code_sha256` dos
    convites de aluno), código de convite de aluno, mesmo o nunca entregue; controle positivo com o nome
    de um aluno canário na saída de erro.

    Disparo: nem alvo dos alunos canário do disparo (`DISPATCH_PREFIXES`), com controle positivo do
    nome de `DISPATCH_A` na saída de erro.
    """
    out, err = capfd.readouterr()
    for channel, text in (("saída padrão", out), ("saída de erro", err)):
        assert find_canary(text) == [], f"identidade canário na {channel} do processo: {find_canary(text)}"
        assert find_secrets(text, secrets) == [], f"segredo na {channel} do processo: {find_secrets(text, secrets)}"
        # Nem os remetentes canário do Telegram (nome, `@username`, id).
        tg_hits = find_canary(text, prefixes=TG_PREFIXES)
        assert tg_hits == [], f"remetente canário do Telegram na {channel} do processo: {tg_hits}"
        # Nem os alunos canário (lista e contas do Telegram), nem códigos de convite de aluno.
        student_hits = find_canary(text, prefixes=STUDENT_PREFIXES)
        assert student_hits == [], f"aluno canário na {channel} do processo: {student_hits}"
        if invite_digests:
            code_hits = find_invite_codes(text, invite_digests)
            assert code_hits == [], f"código de convite de aluno na {channel} do processo: {code_hits}"
        # Nem os alunos canário do disparo (lista e contas do Telegram).
        dispatch_hits = find_canary(text, prefixes=DISPATCH_PREFIXES)
        assert dispatch_hits == [], f"aluno canário do disparo na {channel} do processo: {dispatch_hits}"
    warned = [(w.category.__name__, str(w.message)) for w in caught]
    warn_leaks = [(category, find_canary(message), find_secrets(message, secrets)) for category, message in warned]
    assert [leak for leak in warn_leaks if leak[1] or leak[2]] == [], f"vazamento em avisos: {warn_leaks}"
    # Nem aluno canário nem código de convite de aluno nos avisos.
    student_warn = [(category, find_canary(message, prefixes=STUDENT_PREFIXES),
                     find_invite_codes(message, invite_digests) if invite_digests else [])
                    for category, message in warned]
    assert [leak for leak in student_warn if leak[1] or leak[2]] == [], f"aluno ou código em avisos: {student_warn}"
    # Nem aluno canário do disparo nos avisos.
    dispatch_warn = [(category, find_canary(message, prefixes=DISPATCH_PREFIXES)) for category, message in warned]
    assert [leak for leak in dispatch_warn if leak[1]] == [], f"aluno do disparo em avisos: {dispatch_warn}"

    # Controles positivos (depois da varredura, para não contaminá-la).
    sys.stderr.write(f"sintetico {ZEFIRO.name}\n")
    sys.stderr.flush()
    os.write(2, f"sintetico fd {ZEFIRO.name} {probe_secret}\n".encode())
    out, err = capfd.readouterr()
    assert out == "", "controle positivo: nada foi escrito na saída padrão"
    assert err.count(f"sintetico {ZEFIRO.name}") == 1 and err.count(f"sintetico fd {ZEFIRO.name}") == 1, (
        "a captura dos fd não pegou as escritas sintéticas")
    assert f"nome:{ZEFIRO.name}" in find_canary(err), "o detector não achou o nome na saída de erro"
    assert find_secrets(err, [probe_secret]), "o detector não achou o segredo na saída de erro"
    with warnings.catch_warnings(record=True) as probe:
        warnings.warn(f"aviso sintetico de {ZEFIRO.name}", UserWarning, stacklevel=1)
    assert [f"nome:{ZEFIRO.name}" in find_canary(str(w.message)) for w in probe] == [True], (
        "um aviso com o nome canário não foi gravado ou não foi achado")
    # controle positivo do detector de alunos na saída de erro (fd 2).
    os.write(2, f"sintetico aluno {STUDENT_A.name}\n".encode())
    out, err = capfd.readouterr()
    assert out == "" and err.count(f"sintetico aluno {STUDENT_A.name}") == 1, "a captura do fd 2 não pegou o aluno"
    assert f"aluno-nome:{STUDENT_A.name}" in find_canary(err, prefixes=STUDENT_PREFIXES), (
        "o detector não achou o aluno canário na saída de erro")
    # Controle positivo do detector dos alunos do disparo na saída de erro (fd 2).
    os.write(2, f"sintetico disparo {DISPATCH_A.name}\n".encode())
    out, err = capfd.readouterr()
    assert out == "" and err.count(f"sintetico disparo {DISPATCH_A.name}") == 1, "a captura do fd 2 não pegou o aluno"
    assert f"disparo-nome:{DISPATCH_A.name}" in find_canary(err, prefixes=DISPATCH_PREFIXES), (
        "o detector não achou o aluno canário do disparo na saída de erro")

    for w in caught:
        warnings.warn_explicit(w.message, w.category, w.filename, w.lineno, source=w.source)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _CliRun:
    leaf: str
    args: tuple[str, ...]
    json: bool
    exit_code: int
    stdout: str
    stderr: str
    output: str

    @property
    def is_reveal_success(self) -> bool:
        return self.leaf == "identity reveal" and self.exit_code == 0

    @property
    def texts(self) -> tuple[str, str, str]:
        return (self.stdout, self.stderr, self.output)


def _cli_leaves(group: click.Group, prefix: tuple[str, ...] = ()) -> set[str]:
    """Folhas da árvore de comandos do `cli` (ex.: `people list`)."""
    ctx = click.Context(group)
    leaves: set[str] = set()
    for name in group.list_commands(ctx):
        command = group.get_command(ctx, name)
        path = (*prefix, name)
        if isinstance(command, click.Group):
            leaves |= _cli_leaves(command, path)
        elif command is not None:
            leaves.add(" ".join(path))
    return leaves


def _leaf_of(args: list[str]) -> str | None:
    command: click.Command = cq_cli.cli
    path: list[str] = []
    for arg in args:
        if not isinstance(command, click.Group):
            break
        sub = command.get_command(click.Context(command), arg)
        if sub is None:
            return None
        path.append(arg)
        command = sub
    if not path or isinstance(command, click.Group):
        return None
    return " ".join(path)


def _cli_canary_hits(run: _CliRun) -> list[str]:
    """Alvos canário em toda saída do CLI; só `identity reveal` com sucesso fica de fora."""
    if run.is_reveal_success:
        return []
    hits: set[str] = set()
    for text in run.texts:
        hits.update(find_canary(text))
    return sorted(hits)


def _cli_secret_hits(run: _CliRun, secrets: list[str]) -> list[str]:
    hits: set[str] = set()
    for text in run.texts:
        hits.update(find_secrets(text, secrets))
    return sorted(hits)


def _remember_secret_file(path: Any) -> None:
    """Lê o `--secret-file` (fora da varredura) e põe o conteúdo no registro de segredos."""
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n"), "o arquivo de segredo termina com uma quebra de linha"
    secret = content[:-1]
    assert re.fullmatch(SECRET_RE, secret), "o arquivo de --secret-file não tem um segredo válido"
    assert secret in all_secrets(), "o segredo do arquivo não é o devolvido pela API na criação"
    remember_secret(secret)


def _remember_link_file(path: Any, username: str) -> None:
    """lê o link de equipe do `--secret-file` (fora da varredura), confere o padrão do link de equipe
    com o bot do oferecimento e põe o link e o código no registro de segredos."""
    content = path.read_text(encoding="utf-8")
    assert content.endswith("\n") and content.count("\n") == 1, "o arquivo do link tem uma linha só"
    link = content[:-1]
    match = TEAM_LINK_SHAPE.fullmatch(link)
    assert match is not None and match.group(1) == username, "o arquivo de --secret-file não tem o link do bot"
    assert link in all_secrets(), "o link do arquivo não é o devolvido pela API na criação"
    remember_secret(link)
    remember_secret(match.group(2))


# ---------------------------------------------------------------------------------------------
# Semente, worker, API e CLI
# ---------------------------------------------------------------------------------------------


@dataclass
class _Seed:
    people: dict[str, dict]
    expected: dict[str, str]
    read_secret: str
    read_token: dict
    events_secret: str
    events_token: dict
    label_token: dict
    started: datetime

    def pseudo(self, person: CanaryPerson) -> str:
        return self.people[person.name]["pseudo_id"]

    def alias(self, person: CanaryPerson) -> str:
        return self.people[person.name]["alias"]

    def reveal_refs(self) -> list[str]:
        # Mistura apelidos e identificadores; a resposta segue a ordem da primeira referência.
        return [self.alias(ZEFIRO), self.pseudo(URSULA), self.alias(OLIMPIO)]

    def reveal_body(self) -> dict:
        return {
            "people": [{"pseudo_id": self.pseudo(p), "alias": self.alias(p), "name": p.name, "ra": p.ra,
                        "email": p.email} for p in CANARY_PEOPLE],
            "not_found": [],
        }

    def reveal_lines(self) -> list[str]:
        return [f"{self.alias(p)}  {p.name}  RA: {p.ra or '-'}  e-mail: {p.email or '-'}"
                for p in CANARY_PEOPLE]


def _redact_seeded(text: str, people: dict[str, dict]) -> str:
    """Redação esperada de um texto de SEEDED_FREE_TEXT, que só tem alvos inteiros e com fronteira."""
    for person in CANARY_PEOPLE:
        mark = marker(people[person.name]["alias"])
        for target in (person.email, person.name, person.ra):
            if target:
                text = text.replace(target, mark)
    return text


def _seed(client: Any, clock: Any) -> _Seed:
    """Semente: as pessoas canário e, depois, os textos livres de SEEDED_FREE_TEXT pela API."""
    started = clock.now()
    people = {p.name: register_person(client, ADMIN_TOKEN, **p.payload()) for p in CANARY_PEOPLE}
    expected = {key: _redact_seeded(text, people) for key, text in SEEDED_FREE_TEXT.items()}
    for key, text in SEEDED_FREE_TEXT.items():
        assert find_canary(text), f"o texto semeado {key} precisa conter um alvo canário"
        assert find_canary(expected[key]) == [], f"a redação esperada de {key} ainda tem alvo canário"
    read_secret, read_token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token somente leitura")
    events_secret, events_token = create_token(client, ADMIN_TOKEN, ["events:read"], label="token somente eventos")
    _, label_token = create_token(client, ADMIN_TOKEN, ["people:read"], label=SEEDED_FREE_TEXT["label"])
    assert label_token["label"] == expected["label"]
    seed = _Seed(people, expected, read_secret, read_token, events_secret, events_token, label_token, started)

    granted = client.post("/v1/identity/reveal", headers=ADMIN,
                          json={"pseudonyms": seed.reveal_refs(), "reason": SEEDED_FREE_TEXT["reason"]})
    assert granted.status_code == 200, granted.text[:300]
    assert_matches("identity.reveal", 200, granted.json())
    assert granted.json() == seed.reveal_body()
    denied = client.post("/v1/identity/reveal", headers=auth(read_secret),
                         json={"pseudonyms": [seed.alias(ZEFIRO), seed.pseudo(URSULA)],
                               "reason": SEEDED_FREE_TEXT["reason"]})
    assert_error(denied, 403, "forbidden", permission="identity:reveal")
    return seed


@dataclass
class _Work:
    t1: int
    t2: int
    m1: int
    m2: int
    m3: int
    m3_text: str
    started: datetime
    m2_failed_at: datetime
    console_text: str


def _enqueue_message(outbox: Any, db_url: str, recipient: uuid.UUID, text: str) -> int:
    with psycopg.connect(db_url) as conn:  # confirma ao sair do bloco
        return outbox.enqueue(conn, recipient=recipient, text=text)


def _worker_phase(queue: Any, outbox: Any, worker_thread: Any, clock: Any, db_url: str,
                  seed: _Seed) -> _Work:
    """Worker, em sequência fixa (passos 1 a 5)."""
    started = clock.now()
    z_id, u_id = uuid.UUID(seed.pseudo(ZEFIRO)), uuid.UUID(seed.pseudo(URSULA))

    # 1. fail(error=SEEDED_FREE_TEXT["task_error"], permanent=True)
    t1 = queue.enqueue("privacy.direct_fail", {"step": 1})
    assert queue.claim(WORKER_ID) == ClaimedTask(id=t1, task_type="privacy.direct_fail", payload={"step": 1},
                                                 attempts=1)
    assert queue.fail(t1, WORKER_ID, SEEDED_FREE_TEXT["task_error"], permanent=True) is True
    assert queue.get(t1) == TaskRecord(
        id=t1, task_type="privacy.direct_fail", state="failed", attempts=1, available_at=started,
        lease_owner=None, lease_until=None, last_error=seed.expected["task_error"], finished_at=started)

    # 2. tratador que levanta PermanentTaskError(<nome canário>)
    t2 = queue.enqueue("privacy.permanent", {"step": 2})
    handled: list[int] = []

    def permanent(task: ClaimedTask) -> None:
        handled.append(task.id)
        raise PermanentTaskError(ZEFIRO.name)

    assert queue.work_once(WORKER_ID, {"privacy.permanent": permanent}) == t2
    assert handled == [t2]
    assert queue.get(t2) == TaskRecord(
        id=t2, task_type="privacy.permanent", state="failed", attempts=1, available_at=started,
        lease_owner=None, lease_until=None, last_error="PermanentTaskError", finished_at=started)

    # 3. m1 com Undeliverable(SEEDED_FREE_TEXT["outbox_reason"])
    m1 = _enqueue_message(outbox, db_url, z_id, "mensagem de teste um")
    undeliverable = ScriptedChannel([Undeliverable(SEEDED_FREE_TEXT["outbox_reason"])])
    assert outbox.dispatch_once(undeliverable, WORKER_ID) == m1
    assert undeliverable.calls == [OutgoingMessage(message_id=m1, recipient=z_id, text="mensagem de teste um")]
    record = outbox.get(m1)
    assert (record.state, record.attempts, record.last_error, record.channel_ref, record.sent_at) == (
        "failed", 1, seed.expected["outbox_reason"], None, None)

    # 4. m2 com RuntimeError(f"{nome canário} {ADMIN_TOKEN}") até esgotar as tentativas (3)
    m2 = _enqueue_message(outbox, db_url, u_id, "mensagem de teste dois")
    crashing = ScriptedChannel([RuntimeError(f"{ZEFIRO.name} {ADMIN_TOKEN}") for _ in range(3)])
    for attempt in (1, 2, 3):
        if attempt > 1:
            clock.advance(60)  # além da espera (2 s e depois 4 s)
        assert outbox.dispatch_once(crashing, WORKER_ID) == m2
        record = outbox.get(m2)
        assert (record.state, record.attempts, record.last_error) == (
            "pending" if attempt < 3 else "failed", attempt, "RuntimeError")
    assert len(crashing.calls) == 3
    m2_failed_at = clock.now()

    # 5. m3, com o nome canário no texto, entregue pelo worker com ConsoleChannel(StringIO)
    m3_text = f"Olá, {ZEFIRO.name}! Sua mensagem de teste chegou."
    m3 = _enqueue_message(outbox, db_url, z_id, m3_text)
    console = io.StringIO()
    thread = worker_thread(channel=ConsoleChannel(console), handlers={})
    _wait_until(lambda: outbox.get(m3).state == "sent", 10, "o worker não entregou m3")
    worker_thread.stop()
    assert not thread.is_alive(), "run_worker não terminou depois de stop"
    lines = console.getvalue().splitlines()
    assert len(lines) == 1, f"o console devia ter uma linha: {lines}"
    delivered = json.loads(lines[0])
    assert set(delivered) == {"channel_ref", "message_id", "recipient", "text"}
    assert (delivered["message_id"], delivered["recipient"], delivered["text"]) == (m3, str(z_id), m3_text)
    assert re.fullmatch(r"console-[0-9a-f]{32}", delivered["channel_ref"])
    record = outbox.get(m3)
    assert (record.state, record.attempts, record.channel_ref, record.sent_at, record.last_error) == (
        "sent", 1, delivered["channel_ref"], clock.now(), None)
    return _Work(t1, t2, m1, m2, m3, m3_text, started, m2_failed_at, console.getvalue())


# ---------------------------------------------------------------------------------------------
# fase de bot
# ---------------------------------------------------------------------------------------------


@dataclass
class _BotPhase:
    offering: dict
    bot_id: int  # id do bot no Telegram (o prefixo do token na Bot API falsa)
    username: str
    started: datetime
    resumed: datetime  # fim da espera pedida pelo Telegram (429), depois do avanço do relógio
    course: str  # disciplina e período já redigidos
    term: str
    z_code: str  # convite de Zéfiro (usado por TG_TEODORICO)
    u_code: str  # convite de Úrsula (recusado: a conta já é endereço de outra pessoa)
    updates: dict[str, int]  # rótulo → `update_id` do Telegram


def _compose(template: str, course: str, term: str) -> str:
    """Um texto fixo de `botmessages` com a disciplina e o período redigidos,
    montado pelo teste (sem `botmessages.compose`); os textos daqui cabem nos limites."""
    return template.replace("{course}", course).replace("{term}", term)


def _update_state(sql: psycopg.Connection, update_id: int) -> tuple | None:
    return sql.execute("SELECT state, outcome FROM telegram_updates WHERE update_id = %s",
                       (update_id,)).fetchone()


def _bot_row(sql: psycopg.Connection, columns: str, bot_id: int) -> tuple | None:
    return sql.execute(f"SELECT {columns} FROM bots WHERE telegram_bot_id = %s", (bot_id,)).fetchone()


def _advance_until(clock: Any, seconds: float, predicate: Callable[[], Any], what: str, *,
                   rounds: int = 5, wait: float = 5.0) -> None:
    """Libera uma espera do sistema avançando o `FakeClock` e espera, com
    prazo real, o efeito. Se a thread do sistema ainda não tinha marcado o prazo quando o relógio andou
    (corrida entre o teste e a thread), o prazo ficou à frente: o relógio anda de novo, até `rounds`."""
    for _ in range(rounds):
        clock.advance(seconds)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
    raise AssertionError(f"{what}: nada depois de {rounds} avanços de {seconds} s no relógio")


def _bot_phase(client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, bot_worker_thread: Any,
               settings: Any, seed: _Seed) -> _BotPhase:
    """Fase de bot, em sequência fixa.

    Um worker com bots (`run_worker(…, bots=…)`, `worker_id` fixo) consulta o bot de um oferecimento
    cuja disciplina e período têm nome, RA e e-mail canário. Os remetentes canário do Telegram mandam,
    esperando o processamento de cada update: `/start` válido (vínculo de Zéfiro por TG_TEODORICO),
    `/start` com código inexistente, `/ajuda`, texto sem comando, texto com menção, resposta e
    encaminhamento de remetentes canário, botão, bloqueio (`my_chat_member`), `/start` com o convite de
    Úrsula pela conta já vinculada (outra pessoa), `/ajuda` do chat bloqueado (a resposta falha:
    `outbox.reply_failed`) e `/start` de novo pela conta vinculada com o chat bloqueado (endereço
    `blocked`). Depois vêm a espera pedida pelo Telegram (429, liberada com `clock.advance`) e a
    revogação do token (sem acesso), e o worker para. Nenhum texto injetado tem alvo canário.
    """
    started = clock.now()
    for text in (INVALID_CODE, PLAIN_TEXT, MENTION_TEXT, PREVIOUS_TEXT, CALLBACK_DATA, "/ajuda"):
        assert find_canary(text, prefixes=ALL_PREFIXES) == [], f"texto injetado com alvo canário: {text!r}"
    course, term = seed.expected["course"], seed.expected["term"]
    t_id, f_id, z_id = TG_TEODORICO.user_id, TG_FILISBINA.user_id, TG_ZEFIRO.user_id

    # 1. Cadastro com a disciplina e o período canário, registro do bot, ativação e dois links.
    offering = create_offering(client, ADMIN_TOKEN, code=BOT_OFFERING, course=SEEDED_FREE_TEXT["course"],
                               term=SEEDED_FREE_TEXT["term"], timezone="America/Sao_Paulo")
    offering_id = offering["offering_id"]
    assert re.fullmatch(UUID4_RE, offering_id), offering_id
    assert offering == {
        "offering_id": offering_id, "code": BOT_OFFERING, "course": course, "term": term,
        "timezone": "America/Sao_Paulo", "response_window_minutes": 1440, "state": "draft",
        "created_at": iso(started), "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
        "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` inteiro
    bot = bot_api.add_bot()
    username = bot_api.state(bot).username
    assert isinstance(username, str)
    assert register_bot(client, ADMIN_TOKEN, BOT_OFFERING, bot_api.token_of(bot)) == {
        "offering": {"offering_id": offering_id, "code": BOT_OFFERING}, "username": username,
        "telegram_bot_id": bot, "state": "registered", "profile_state": "pending",
        "registered_at": iso(started), "token_replaced_at": None, "lease": None, "last_poll_ok_at": None,
        "problem": None}
    assert activate_bot(client, ADMIN_TOKEN, BOT_OFFERING) == {
        **offering, "state": "active", "activated_at": iso(started),
        "bot": {"username": username, "telegram_bot_id": bot, "state": "registered", "profile_state": "pending"}}
    codes: dict[str, str] = {}
    for person in (ZEFIRO, URSULA):
        body = create_team_link(client, ADMIN_TOKEN, BOT_OFFERING, seed.alias(person))
        assert body == {
            "link": body["link"], "expires_at": iso(started + TEAM_LINK_VALIDITY),
            "person": {"pseudo_id": seed.pseudo(person), "alias": seed.alias(person)},
            "offering": {"offering_id": offering_id, "code": BOT_OFFERING, "timezone": "America/Sao_Paulo"}}
        match = TEAM_LINK_SHAPE.fullmatch(body["link"])
        assert match is not None and match.group(1) == username, "link fora do padrão ou de outro bot"
        codes[person.name] = link_code(body["link"])
    z_code, u_code = codes[ZEFIRO.name], codes[URSULA.name]
    assert z_code != u_code

    # 2. O worker assume o bot, configura o perfil e passa a consultar.
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=BOT_WORKER_ID),
                               channel=ConsoleChannel(io.StringIO()), handlers={})
    _wait_until(lambda: _bot_row(sql, "profile_state", bot) == ("configured",), 15, "perfil não configurado")
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=15)

    updates: dict[str, int] = {}

    def processed(label: str, update_id: int | None, outcome: str) -> None:
        assert update_id is not None, f"{label}: a Bot API falsa descartou o update"
        updates[label] = update_id
        _wait_until(lambda: _update_state(sql, update_id) == ("processed", outcome), 15,
                    f"{label}: update não processado como {outcome}")

    def delivered(user: Any, count: int) -> None:
        bot_api.wait_for(lambda: len(bot_api.sent_messages(bot, chat_id=user.user_id)) >= count, timeout=15)

    # 3. Remetentes canário.
    processed("join", bot_api.push_start(bot, TG_TEODORICO, z_code), "team_joined")
    delivered(TG_TEODORICO, 1)
    processed("invalid", bot_api.push_start(bot, TG_FILISBINA, INVALID_CODE), "team_refused_invalid")
    delivered(TG_FILISBINA, 1)
    processed("help", bot_api.push_text(bot, TG_ZEFIRO, "/ajuda", bot_command=True), "help")
    delivered(TG_ZEFIRO, 1)
    processed("plain", bot_api.push_text(bot, TG_ZEFIRO, PLAIN_TEXT), "not_handled")
    date = int(started.timestamp())
    processed("mention", bot_api.push_text(
        bot, TG_ZEFIRO, MENTION_TEXT,
        entities=[{"offset": 24, "length": 9, "type": "text_mention", "user": TG_TEODORICO.as_user()}],
        extra={"reply_to_message": {"message_id": 1, "from": TG_FILISBINA.as_user(),
                                    "chat": TG_ZEFIRO.as_private_chat(), "date": date, "text": PREVIOUS_TEXT},
               "forward_origin": {"type": "user", "date": date, "sender_user": TG_TEODORICO.as_user()}}),
        "not_handled")
    processed("callback", bot_api.push_callback(bot, TG_TEODORICO, CALLBACK_DATA), "not_handled")
    processed("member", bot_api.push_my_chat_member(bot, TG_FILISBINA), "not_handled")  # bloqueia o chat
    processed("other_person", bot_api.push_start(bot, TG_TEODORICO, u_code), "team_refused_other_person")
    delivered(TG_TEODORICO, 2)
    # A resposta ao chat que bloqueou o bot falha de vez (`outbox.reply_failed`).
    processed("help_blocked", bot_api.push_text(bot, TG_FILISBINA, "/ajuda", bot_command=True), "help")
    _wait_until(lambda: sql.execute(
        "SELECT o.state FROM outbox_messages o JOIN telegram_updates u ON u.id = o.reply_to_update_id "
        "WHERE u.update_id = %s", (updates["help_blocked"],)).fetchall() == [("failed",)], 15,
        "a resposta ao chat bloqueado não falhou de vez")
    # O chat da conta vinculada bloqueia o bot: o envio ao endereço falha e o endereço fica `blocked`.
    bot_api.block_chat(bot, TG_TEODORICO)
    processed("already", bot_api.push_start(bot, TG_TEODORICO, z_code), "team_already_linked")
    _wait_until(lambda: sql.execute("SELECT state FROM delivery_endpoints WHERE chat_id = %s",
                                    (t_id,)).fetchall() == [("blocked",)], 15, "o endereço não ficou blocked")

    # 4. Espera pedida pelo Telegram na consulta: liberada avançando o relógio.
    bot_api.fail("getUpdates", retry_after=5, bot_id=bot)
    limited = bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.programmed],
                               timeout=15)
    assert [(c.status, c.retry_after) for c in limited] == [(429, 5)]
    _wait_until(lambda: _bot_row(sql, "last_problem", bot) == ("rate_limited",), 15, "sem o problema rate_limited")
    _advance_until(clock, 6, lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot)
                                      if c.seq > limited[0].seq and c.outcome == "ok"],
                   "a consulta não voltou depois da espera pedida (429)")
    resumed = clock.now()
    _wait_until(lambda: _bot_row(sql, "last_problem, last_poll_ok_at", bot) == (None, resumed), 15,
                "a consulta não voltou depois da espera")

    # 5. Revogação do token: sem acesso, posse limpa; o worker para.
    bot_api.revoke(bot)
    _wait_until(lambda: _bot_row(sql, "state, access_problem, lease_owner", bot) == (
        "no_access", "token_rejected", None), 15, "o bot revogado não ficou sem acesso")
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker com bots não parou em 10 s"
    assert worker.error is None and worker.returned

    # --- O que saiu para o Telegram: textos fixos com a disciplina e o período redigidos ---
    welcome, help_text = _compose(WELCOME_TEAM, course, term), _compose(HELP, course, term)
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(bot)] == [
        (t_id, welcome), (f_id, LINK_INVALID), (z_id, help_text), (t_id, OTHER_PERSON)]
    assert [(c.params, c.outcome, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ({"chat_id": t_id, "text": welcome}, "ok", 200),
        ({"chat_id": f_id, "text": LINK_INVALID}, "ok", 200),
        ({"chat_id": z_id, "text": help_text}, "ok", 200),
        ({"chat_id": t_id, "text": OTHER_PERSON}, "ok", 200),
        ({"chat_id": f_id, "text": help_text}, "error", 403),
        ({"chat_id": t_id, "text": ALREADY_LINKED}, "error", 403)]
    assert [c.params for c in bot_api.calls("setMyDescription", bot_id=bot)] == [
        {"description": _compose(DESCRIPTION_ACTIVE, course, term)}]
    assert [c.params for c in bot_api.calls("setMyCommands", bot_id=bot)] == [
        {"commands": [{"command": c.command, "description": c.description} for c in COMMANDS]}]

    # --- Gravação mínima: colunas internas com o id do remetente, texto literal ---
    rows = {label: sql.execute(
        "SELECT kind, chat_type, chat_id, sender_id, text, command, state, outcome FROM telegram_updates "
        "WHERE update_id = %s", (update_id,)).fetchall() for label, update_id in updates.items()}
    assert rows == {
        "join": [("message", "private", t_id, t_id, f"/start {z_code}", "/start", "processed", "team_joined")],
        "invalid": [("message", "private", f_id, f_id, f"/start {INVALID_CODE}", "/start", "processed",
                     "team_refused_invalid")],
        "help": [("message", "private", z_id, z_id, "/ajuda", "/ajuda", "processed", "help")],
        "plain": [("message", "private", z_id, z_id, PLAIN_TEXT, None, "processed", "not_handled")],
        "mention": [("message", "private", z_id, z_id, MENTION_TEXT, None, "processed", "not_handled")],
        "callback": [("callback_query", None, t_id, t_id, None, None, "processed", "not_handled")],
        "member": [("my_chat_member", "private", f_id, f_id, None, None, "processed", "not_handled")],
        "other_person": [("message", "private", t_id, t_id, f"/start {u_code}", "/start", "processed",
                          "team_refused_other_person")],
        "help_blocked": [("message", "private", f_id, f_id, "/ajuda", "/ajuda", "processed", "help")],
        "already": [("message", "private", t_id, t_id, f"/start {z_code}", "/start", "processed",
                     "team_already_linked")],
    }
    assert sql.execute(
        "SELECT p.pseudo_id::text, x.role, x.state FROM enrollments x JOIN people p ON p.id = x.person_id "
        "JOIN offerings o ON o.id = x.offering_id WHERE o.code = %s ORDER BY x.id", (BOT_OFFERING,)).fetchall() == [
        (seed.pseudo(ZEFIRO), "team", "active"), (seed.pseudo(URSULA), "team", "pending")]
    assert sql.execute(
        "SELECT p.pseudo_id::text, i.state, i.code_sha256 FROM invites i JOIN enrollments x "
        "ON x.id = i.enrollment_id JOIN people p ON p.id = x.person_id JOIN offerings o ON o.id = x.offering_id "
        "WHERE o.code = %s ORDER BY i.id", (BOT_OFFERING,)).fetchall() == [
        (seed.pseudo(ZEFIRO), "used", hashlib.sha256(z_code.encode()).hexdigest()),
        (seed.pseudo(URSULA), "revoked", hashlib.sha256(u_code.encode()).hexdigest())]
    assert sql.execute(
        "SELECT p.pseudo_id::text, d.channel, d.chat_id, d.state FROM delivery_endpoints d "
        "JOIN people p ON p.id = d.person_id JOIN bots b ON b.id = d.bot_id WHERE b.telegram_bot_id = %s "
        "ORDER BY d.id", (bot,)).fetchall() == [(seed.pseudo(ZEFIRO), "telegram", t_id, "blocked")]
    assert sql.execute(
        "SELECT o.state, o.attempts, o.text, o.last_error, o.recipient_person_id IS NULL, u.update_id "
        "FROM outbox_messages o JOIN bots b ON b.id = o.bot_id "
        "LEFT JOIN telegram_updates u ON u.id = o.reply_to_update_id "
        "WHERE b.telegram_bot_id = %s ORDER BY o.id", (bot,)).fetchall() == [
        ("sent", 1, welcome, None, False, None),
        ("sent", 1, LINK_INVALID, None, True, updates["invalid"]),
        ("sent", 1, help_text, None, True, updates["help"]),
        ("sent", 1, OTHER_PERSON, None, True, updates["other_person"]),
        ("failed", 1, help_text, "chat_unreachable", True, updates["help_blocked"]),
        ("failed", 1, ALREADY_LINKED, "chat_unreachable", False, None)]

    # --- bots.get: sem acesso, sem posse, a última consulta e o problema fixo ---
    resp = client.get(f"/v1/offerings/{BOT_OFFERING}/bot", headers=ADMIN)
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("bots.get", 200, resp.json())
    assert resp.json() == {
        "offering": {"offering_id": offering_id, "code": BOT_OFFERING}, "username": username,
        "telegram_bot_id": bot, "state": "no_access", "profile_state": "configured",
        "registered_at": iso(started), "token_replaced_at": None, "lease": None,
        "last_poll_ok_at": iso(resumed), "problem": {"code": "token_rejected", "message": TOKEN_REJECTED_MESSAGE}}
    return _BotPhase(offering, bot, username, started, resumed, course, term, z_code, u_code, updates)


def _check_bot_events(api: _Api, seed: _Seed, bot_run: _BotPhase, sql: psycopg.Connection) -> None:
    """Os eventos exatos da fase de bot, na ordem, só com pseudônimos.

    A posse e o perfil correm em threads diferentes do worker: entre os dois não há ordem.
    """
    events = sorted(api.events, key=lambda e: e["seq"])
    t0, t1 = iso(bot_run.started), iso(bot_run.resumed)
    oid = bot_run.offering["offering_id"]
    pz, pu = seed.pseudo(ZEFIRO), seed.pseudo(URSULA)
    expires = iso(bot_run.started + TEAM_LINK_VALIDITY)
    mine = [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in events
            if e["data"].get("offering_id") == oid]
    assert mine[:5] == [
        ("offering.created", t0, "bootstrap", {"offering_id": oid, "code": BOT_OFFERING}),
        ("bot.registered", t0, "bootstrap",
         {"offering_id": oid, "telegram_bot_id": bot_run.bot_id, "username": bot_run.username}),
        ("offering.state_changed", t0, "bootstrap", {"offering_id": oid, "from_state": "draft", "to_state": "active"}),
        ("team.link_created", t0, "bootstrap", {"offering_id": oid, "person": pz, "expires_at": expires}),
        ("team.link_created", t0, "bootstrap", {"offering_id": oid, "person": pu, "expires_at": expires}),
    ], mine
    assert sorted(mine[5:7], key=repr) == sorted([
        ("bot.lease_changed", t0, None,
         {"offering_id": oid, "worker_id": BOT_WORKER_ID, "change": "acquired", "reason": "acquired"}),
        ("bot.profile_updated", t0, None, {"offering_id": oid, "profile_state": "configured", "purpose": "active"}),
    ], key=repr), mine
    assert mine[7:] == [
        ("team.joined", t0, None, {"offering_id": oid, "person": pz, "endpoint_replaced": False}),
        ("team.join_refused", t0, None, {"offering_id": oid, "refusal": "invalid"}),
        ("team.link_revoked", t0, None, {"offering_id": oid, "person": pu, "revoke_reason": "refused"}),
        ("team.join_refused", t0, None, {"offering_id": oid, "refusal": "other_person"}),
        ("endpoint.blocked", t0, None, {"offering_id": oid, "person": pz, "channel": "telegram"}),
        ("bot.lease_changed", t1, None,
         {"offering_id": oid, "worker_id": BOT_WORKER_ID, "change": "released", "reason": "no_access"}),
        ("bot.access_changed", t1, None, {"offering_id": oid, "access": "lost", "problem": "token_rejected"}),
    ], mine

    reply_row, reply_message = sql.execute(
        "SELECT u.id, o.id FROM outbox_messages o JOIN telegram_updates u ON u.id = o.reply_to_update_id "
        "WHERE u.update_id = %s", (bot_run.updates["help_blocked"],)).fetchone()
    reply = _only([e for e in events if e["type"] == "outbox.reply_failed"], "outbox.reply_failed")
    assert reply == {"seq": reply["seq"], "type": "outbox.reply_failed", "occurred_at": t0, "actor_token_id": None,
                     "data": {"message_id": reply_message, "reply_to": reply_row, "attempts": 1,
                              "error": "chat_unreachable"}}
    (linked_message,) = sql.execute(
        "SELECT o.id FROM outbox_messages o JOIN bots b ON b.id = o.bot_id WHERE b.telegram_bot_id = %s "
        "AND o.recipient_person_id IS NOT NULL AND o.state = 'failed'", (bot_run.bot_id,)).fetchone()
    failed = _only([e for e in events if e["type"] == "outbox.failed" and e["data"]["message_id"] == linked_message],
                   "outbox.failed do endereço bloqueado")
    assert failed == {"seq": failed["seq"], "type": "outbox.failed", "occurred_at": t0, "actor_token_id": None,
                      "data": {"message_id": linked_message, "recipient": pz, "attempts": 1,
                               "error": "chat_unreachable"}}
    # Só o `endpoint.blocked` do oferecimento da fase (o da fase de disparos, do
    # 403 de B, é conferido em `_check_dispatch_events`).
    blocked = _only([e for e in events if e["type"] == "endpoint.blocked" and e["data"].get("offering_id") == oid],
                    "endpoint.blocked")
    assert failed["seq"] < blocked["seq"], "outbox.failed vem antes de endpoint.blocked"


# ---------------------------------------------------------------------------------------------
# fase de alunos
# ---------------------------------------------------------------------------------------------


@dataclass
class _StudentPhase:
    offering: dict
    bot_id: int  # id do bot no Telegram (o prefixo do token na Bot API falsa)
    username: str
    course: str  # disciplina e período já redigidos
    term: str
    started: datetime  # t0: lote, termo, aceite e as falhas de e-mail
    retried: datetime  # t1: depois da espera da falha temporária do e-mail (2ª tentativa)
    requested: datetime  # t2: depois da espera da falha temporária do `/convite`
    expired: datetime  # t3: depois da validade do convite do autosserviço
    people: dict[str, dict]  # pseudônimo → {pseudo_id, alias}
    people_order: list[str]  # pseudônimos na ordem de `people.id` (embaralhada)
    batch_order: list[str]  # pseudônimos na ordem de `enrollments.id`
    sends: dict[str, int]  # rótulo → `invite_sends.id`
    codes: dict[str, str]  # rótulo do envio → código lido do e-mail
    updates: dict[str, int]  # rótulo → `update_id` do Telegram
    convite_literal: str  # o texto literal do `/convite`, lido com a tarefa `pending`

    def pseudo(self, student: CanaryStudent) -> str:
        return _pseudo_of(self.people, student)

    def alias(self, student: CanaryStudent) -> str:
        return self.people[self.pseudo(student)]["alias"]


def _pseudo_of(people: dict[str, dict], student: CanaryStudent) -> str:
    matches = [pseudo for pseudo, person in people.items() if person["ra"] == student.ra]
    assert len(matches) == 1, f"aluno canário sem pessoa única: {len(matches)}"
    return matches[0]


def _fill_slots(template: str, values: dict[str, str]) -> str:
    """Troca `{nome}` pelos `values` numa passada só, como o `compose`: o que
    vem nos valores fica literal, e as chaves sem valor ficam como estão."""
    return re.sub(r"\{([a-z]+)\}", lambda match: values.get(match.group(1), match.group(0)), template)


def _compose_student(template: str, course: str, term: str) -> str:
    """Um texto do bot ao aluno com a disciplina e o período redigidos e, no `TERMS`, a
    versão vigente, montado pelo teste (sem `botmessages.compose`); os textos daqui cabem em 4.096
    unidades UTF-16 (sem corte)."""
    return _fill_slots(template, {"course": course, "term": term, "version": str(botmessages.TERMS_VERSION)})


def _subject_part(value: str) -> str:
    """A disciplina ou o período no assunto: `Cc`, `Zl` e `Zp` viram espaço, as sequências de
    espaço viram um só e as pontas são aparadas."""
    spaced = "".join(" " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in value)
    return re.sub(" +", " ", spaced).strip(" ")


def _invite_subject(course: str, term: str) -> str:
    return _fill_slots(INVITE_SUBJECT, {"course": _subject_part(course), "term": _subject_part(term)})


def _invite_body(course: str, term: str, link: str, validity: str) -> str:
    return _fill_slots(INVITE_BODY, {"course": course, "term": term, "link": link, "validity": validity})


def _format_local(moment: datetime, zone: str) -> str:
    """A validade no fuso do oferecimento: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = moment.astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({zone})"


def _envelope_address(email: str) -> str:
    """O endereço de envio: o `ascii_email` do `email_validator`, com a parte local
    na caixa gravada e o domínio (ASCII) em minúsculas."""
    local, domain = email.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def _invite_email_code(message: Any, student: CanaryStudent, *, username: str, course: str, term: str,
                       expires: datetime, before: datetime, after: datetime) -> str:
    """Confere um e-mail de convite capturado pelo SMTP falso por igualdade
    no envelope, nos cabeçalhos (`Date` e `Message-ID` por forma: UTC, entre `before` e `after` em
    tempo real, e o domínio do remetente) e no texto decodificado, e devolve o código do link (22
    caracteres base64url). O bruto é só ASCII (quoted-printable) e o MAIL só leva o `SIZE` (sem
    `SMTPUTF8`)."""
    address = _envelope_address(student.email)
    validity = _format_local(expires, OFFERING_ZONE)
    head, tail = _invite_body(course, term, _LINK_SLOT, validity).split(_LINK_SLOT)
    text = message.text
    assert isinstance(text, str) and text.startswith(head) and text.endswith(tail + "\n"), (
        "o texto do e-mail não segue o modelo do convite")
    link = text[len(head):len(text) - len(tail) - 1]
    match = re.fullmatch(rf"https://t\.me/{re.escape(username)}\?start=([A-Za-z0-9_-]{{22}})", link)
    assert match is not None, "o link do e-mail não é o do bot do oferecimento"
    assert text == _invite_body(course, term, link, validity) + "\n"
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated, message.acknowledged) == (
        SMTP_SENDER, (address,), False, True, True)
    assert [p for p in message.mail_params if not re.fullmatch(r"SIZE=[0-9]+", p, re.IGNORECASE)] == []
    names = [name.lower() for name, _ in message.headers]
    assert sorted(names) == sorted(INVITE_HEADER_NAMES), f"cabeçalhos do e-mail: {names}"
    headers = {name.lower(): value for name, value in message.headers}
    date, message_id = headers.pop("date"), headers.pop("message-id")
    assert headers == {"from": f"{MAIL_FROM_NAME} <{SMTP_SENDER}>", "to": address,
                       "subject": _invite_subject(course, term), "mime-version": "1.0",
                       "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    sent_at = parsedate_to_datetime(date)
    assert sent_at.utcoffset() == timedelta(0), "o Date do e-mail não está em UTC"
    assert before.replace(microsecond=0) <= sent_at <= after, "Date fora do intervalo do envio"
    assert re.fullmatch(r"<[^<>@\s]+@cq\.example\.org>", message_id), "Message-ID fora da forma"
    assert message.raw.isascii(), "o bruto do e-mail não é só ASCII (quoted-printable)"
    return match.group(1)


def _student_people(sql: psycopg.Connection) -> dict[str, dict]:
    """Pseudônimo → {pseudo_id, alias, ra} dos alunos canário, lidos do banco pelo RA (o teste é o
    oráculo: a importação só devolve contagens)."""
    rows = sql.execute(
        "SELECT p.pseudo_id::text, p.alias, i.ra FROM people p JOIN identities i ON i.person_id = p.id "
        "WHERE i.ra = ANY(%s) ORDER BY p.id", ([student.ra for student in CANARY_STUDENTS],)).fetchall()
    return {pseudo: {"pseudo_id": pseudo, "alias": alias, "ra": ra} for pseudo, alias, ra in rows}


def _sends(sql: psycopg.Connection, code: str) -> list[tuple[int, str]]:
    """(id, pseudônimo) dos envios de convite do oferecimento `code`, em ordem de `invite_sends.id`."""
    rows = sql.execute(
        "SELECT s.id, p.pseudo_id::text FROM invite_sends s JOIN enrollments x ON x.id = s.enrollment_id "
        "JOIN offerings o ON o.id = x.offering_id JOIN people p ON p.id = x.person_id WHERE o.code = %s "
        "ORDER BY s.id", (code,)).fetchall()
    return [(int(send_id), pseudo) for send_id, pseudo in rows]


def _send_task(sql: psycopg.Connection, send_id: int) -> tuple | None:
    """(estado, tentativas, `last_error`) da tarefa `invite.email` do envio (chave `invite.email:<id>`)."""
    return sql.execute("SELECT state, attempts, last_error FROM tasks WHERE idempotency_key = %s",
                       (f"invite.email:{send_id}",)).fetchone()


def _update_task(sql: psycopg.Connection, update_id: int) -> tuple | None:
    """(estado, tentativas, `last_error`) da tarefa `telegram.update` do update."""
    return sql.execute(
        "SELECT t.state, t.attempts, t.last_error FROM tasks t JOIN telegram_updates u "
        "ON t.idempotency_key = 'telegram.update:' || u.id WHERE u.update_id = %s", (update_id,)).fetchone()


def _install_convite_failure(sql: psycopg.Connection) -> None:
    """gatilho de teste no INSERT de `invite_sends`; o processamento de um `/convite` que cria
    um envio falha de forma temporária (a tarefa `telegram.update` volta a `pending`)."""
    name = pgsql.Identifier(CONVITE_FAILURE_TRIGGER)
    body = "BEGIN RAISE EXCEPTION 'falha de teste no envio de convite'; END"
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS {}")
                .format(name, pgsql.Literal(body)))
    sql.execute(pgsql.SQL("CREATE TRIGGER {} BEFORE INSERT ON invite_sends FOR EACH ROW EXECUTE FUNCTION {}()")
                .format(name, name))
    sql.execute(pgsql.SQL("ALTER TABLE invite_sends ENABLE ALWAYS TRIGGER {}").format(name))


def _remove_convite_failure(sql: psycopg.Connection) -> None:
    name = pgsql.Identifier(CONVITE_FAILURE_TRIGGER)
    sql.execute("SET lock_timeout = '10s'")
    sql.execute(pgsql.SQL("DROP TRIGGER {} ON invite_sends").format(name))
    sql.execute(pgsql.SQL("DROP FUNCTION {}()").format(name))


def _student_phase(client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, bot_worker_thread: Any,
                   settings: Any, smtp: Any, mail_channel: Any, seed: _Seed) -> _StudentPhase:
    """Fase de alunos, em sequência fixa: um worker com bots e o e-mail (`run_worker(…,
    bots=…, verification=…)`, `worker_id` fixo) e um oferecimento próprio, com a disciplina e o período
    canário da API base e um bot próprio; os alunos canário (A com as contas A1 e A2, B com uma conta):
    1. lote: importação de A e B, preparo, confirmação e os dois e-mails pelo SMTP falso;
    2. `/start` e `/aceito` de A1 (`student_terms_presented`, `student_joined`);
    3. o link de A de novo em A1 (`student_already_linked`); depois, reenvios da coordenação a A (já
       vinculado) com falhas de e-mail com eco do endereço: RCPT 550 (`recipient_refused`), AUTH 535
       (`auth_refused`) e RCPT 450 (temporária; a 2ª tentativa entrega, com outro código);
    4. o link do lote de B, nunca apresentado, em A1, o chat vigente de A (`student_refused_other_person`;
       revoga o convite de B); com o worker parado, dois reenvios da coordenação a B; religado, a primeira transação da tarefa do
       primeiro o cancela (`superseded`, sem código) e o segundo entrega;
    5. reenvio da coordenação a A e `/start` + `/aceito` de A2 (`student_endpoint_replaced`);
    6. `/convite` de B com um e-mail fora da lista e, com o gatilho de falha do envio, com o e-mail de B: a
       tarefa fica `pending` com o texto literal (controle positivo), o gatilho sai, o relógio anda e o
       update é processado e mascarado, com o envio S de B (`invite_requested`);
    7. `/start` do link de S e `/recuso` de B (`student_terms_presented`, `student_terms_refused`); a
       adesão é lida (sem efeito);
    8. por último, o relógio passa da validade e o link de S, o único pendente, é usado de novo em B
       (`student_refused_invalid`); o worker para.
    Nenhum `endpoint.blocked` nem `outbox.reply_failed`; todo update termina processado.
    """
    started = clock.now()
    course, term = seed.expected["course"], seed.expected["term"]
    a1, a2, b = TG_STUDENT_A1.user_id, TG_STUDENT_A2.user_id, TG_STUDENT_B.user_id
    for text in (OUTSIDE_EMAIL, "/aceito", "/recuso", MASKED_CONVITE, f"/convite {OUTSIDE_EMAIL}"):
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == [], f"texto injetado com alvo: {text!r}"

    # 0. Oferecimento, bot e worker com o e-mail (o `verification` vai pelos kwargs).
    offering = create_offering(client, ADMIN_TOKEN, code=STUDENT_OFFERING, course=SEEDED_FREE_TEXT["course"],
                               term=SEEDED_FREE_TEXT["term"], timezone=OFFERING_ZONE)
    offering_id = offering["offering_id"]
    assert re.fullmatch(UUID4_RE, offering_id), offering_id
    assert offering == {
        "offering_id": offering_id, "code": STUDENT_OFFERING, "course": course, "term": term,
        "timezone": OFFERING_ZONE, "response_window_minutes": 1440, "state": "draft",
        "created_at": iso(started), "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
        "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` inteiro
    bot = bot_api.add_bot()
    username = bot_api.state(bot).username
    assert isinstance(username, str)
    assert register_bot(client, ADMIN_TOKEN, STUDENT_OFFERING, bot_api.token_of(bot)) == {
        "offering": {"offering_id": offering_id, "code": STUDENT_OFFERING}, "username": username,
        "telegram_bot_id": bot, "state": "registered", "profile_state": "pending",
        "registered_at": iso(started), "token_replaced_at": None, "lease": None, "last_poll_ok_at": None,
        "problem": None}
    assert activate_bot(client, ADMIN_TOKEN, STUDENT_OFFERING) == {
        **offering, "state": "active", "activated_at": iso(started),
        "bot": {"username": username, "telegram_bot_id": bot, "state": "registered", "profile_state": "pending"}}
    ref = {"offering_id": offering_id, "code": STUDENT_OFFERING}
    worker_settings = dataclasses.replace(settings, worker_id=STUDENT_WORKER_ID)

    def start_worker() -> Any:
        return bot_worker_thread(settings=worker_settings, channel=ConsoleChannel(io.StringIO()), handlers={},
                                 verification=mail_channel)

    worker = start_worker()
    _wait_until(lambda: _bot_row(sql, "profile_state", bot) == ("configured",), 15, "perfil não configurado")
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=15)

    updates: dict[str, int] = {}
    sends: dict[str, int] = {}
    codes: dict[str, str] = {}

    def processed(label: str, update_id: int | None, outcome: str) -> None:
        assert update_id is not None, f"{label}: a Bot API falsa descartou o update"
        updates[label] = update_id
        _wait_until(lambda: _update_state(sql, update_id) == ("processed", outcome), 15,
                    f"{label}: update não processado como {outcome}")

    def delivered(user: Any, count: int) -> None:
        bot_api.wait_for(lambda: len(bot_api.sent_messages(bot, chat_id=user.user_id)) >= count, timeout=15)

    def emails(count: int) -> list[Any]:
        smtp.wait_for(lambda: len(smtp.messages()) >= count, timeout=15)
        messages = smtp.messages()
        assert len(messages) == count, f"o SMTP falso recebeu {len(messages)} e-mails, e não {count}"
        return messages

    def settled(label: str, task: tuple) -> None:
        _wait_until(lambda: _send_task(sql, sends[label]) == task, 15, f"envio {label}: tarefa não ficou {task}")

    def record_sends(*expected: tuple[str, str]) -> None:
        """Os envios novos do oferecimento (em ordem de id), com os rótulos e os donos esperados."""
        fresh = [(send_id, owner) for send_id, owner in _sends(sql, STUDENT_OFFERING) if send_id not in sends.values()]
        assert [owner for _, owner in fresh] == [owner for _, owner in expected], "envios novos inesperados"
        for (label, _), (send_id, _) in zip(expected, fresh, strict=True):
            sends[label] = send_id

    def real_now() -> datetime:
        return datetime.now(timezone.utc)

    def email_code(message: Any, student: CanaryStudent, generated_at: datetime, before: datetime) -> str:
        """O e-mail (já recebido) confere com o modelo; `before` é o tempo real antes do passo que o
        disparou, e o depois é agora (o `Date` fica entre os dois)."""
        return _invite_email_code(message, student, username=username, course=course, term=term,
                                  expires=generated_at + INVITE_VALIDITY, before=before, after=real_now())

    # 1. Lote: importação, preparo, confirmação e os e-mails de A e B.
    imported = import_students(client, ADMIN_TOKEN, STUDENT_OFFERING, [student.row() for student in CANARY_STUDENTS])
    assert imported == {"offering": ref, "received": 2, "people_created": 2, "people_reused": 0,
                        "enrollments_created": 2, "already_enrolled": 0}
    people = _student_people(sql)
    people_order = list(people)
    pa, pb = _pseudo_of(people, STUDENT_A), _pseudo_of(people, STUDENT_B)
    student_of = {pa: STUDENT_A, pb: STUDENT_B}
    batch_order = [row[0] for row in sql.execute(
        "SELECT p.pseudo_id::text FROM enrollments x JOIN people p ON p.id = x.person_id "
        "JOIN offerings o ON o.id = x.offering_id WHERE o.code = %s ORDER BY x.id", (STUDENT_OFFERING,)).fetchall()]
    assert sorted(people_order) == sorted(batch_order) == sorted([pa, pb])
    prepared = prepare_invites(client, ADMIN_TOKEN, STUDENT_OFFERING)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", prepared["confirmation_code"]), prepared
    assert prepared == {"offering": ref, "eligible": 2, "valid_hours": 168,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(started + CONFIRMATION_VALIDITY)}
    before = real_now()
    assert send_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, prepared["confirmation_code"]) == {
        "offering": ref, "queued": 2, "valid_hours": 168}
    batch_labels = ["a_batch" if pseudo == pa else "b_batch" for pseudo in batch_order]
    record_sends(*zip(batch_labels, batch_order, strict=True))
    for message, label, pseudo in zip(emails(2), batch_labels, batch_order, strict=True):
        codes[label] = email_code(message, student_of[pseudo], started, before)
    for label in batch_labels:
        settled(label, ("done", 1, None))

    # 2. Termo e aceite de A1.
    processed("a1_start", bot_api.push_start(bot, TG_STUDENT_A1, codes["a_batch"]), "student_terms_presented")
    delivered(TG_STUDENT_A1, 1)
    processed("a1_accept", bot_api.push_text(bot, TG_STUDENT_A1, "/aceito", bot_command=True), "student_joined")
    delivered(TG_STUDENT_A1, 2)

    # 3. O mesmo link no chat vigente de A.
    processed("a1_again", bot_api.push_start(bot, TG_STUDENT_A1, codes["a_batch"]), "student_already_linked")
    delivered(TG_STUDENT_A1, 3)

    # Entre 3 e 4: reenvios da coordenação a A (vinculado) com falhas de e-mail com o endereço
    # na resposta do servidor (só o tipo do erro é guardado ou registrado).
    address_a = _envelope_address(STUDENT_A.email)
    resent = {"offering": ref, "queued": 1, "valid_hours": 168, "not_students": [], "not_found_count": 0}
    smtp.fail("rcpt", 550)  # o texto padrão repete `<endereço>`
    assert resend_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, [people[pa]["alias"]]) == resent
    record_sends(("a_rcpt", pa))
    settled("a_rcpt", ("failed", 1, "PermanentTaskError"))
    smtp.fail("auth", 535, text=f"5.7.8 Authentication credentials invalid for <{address_a}>")
    assert resend_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, [people[pa]["alias"]]) == resent
    record_sends(("a_auth", pa))
    settled("a_auth", ("failed", 1, "PermanentTaskError"))
    smtp.fail("rcpt", 450, text=f"4.2.2 <{address_a}>: Mailbox full")
    assert resend_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, [people[pa]["alias"]]) == resent
    record_sends(("a_retry", pa))
    settled("a_retry", ("pending", 1, "RetryTaskLater"))
    assert len(smtp.messages()) == 2, "as recusas não entregam mensagem"
    before = real_now()
    clock.advance(RETRY_ADVANCE_SECONDS)
    retried = clock.now()
    codes["a_retry"] = email_code(emails(3)[2], STUDENT_A, retried, before)
    settled("a_retry", ("done", 2, "RetryTaskLater"))

    # 4. O link do lote de B no chat vigente de A (outra pessoa; revoga o convite de B).
    processed("a1_other", bot_api.push_start(bot, TG_STUDENT_A1, codes["b_batch"]), "student_refused_other_person")
    delivered(TG_STUDENT_A1, 4)

    # Entre 4 e 5: dois reenvios da coordenação a B com o worker parado.
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker da fase de alunos não parou em 10 s"
    assert worker.error is None and worker.returned
    assert _bot_row(sql, "lease_owner", bot) == (None,)
    for label, pseudonym in (("b_superseded", people[pb]["alias"]), ("b_resend", pb)):
        assert resend_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, [pseudonym]) == resent
        record_sends((label, pb))
    assert [_send_task(sql, sends[label]) for label in ("b_superseded", "b_resend")] == [("pending", 0, None)] * 2
    assert len(smtp.connections()) == 6, "com o worker parado, nenhum envio conecta"
    before = real_now()
    worker = start_worker()
    _wait_until(lambda: _bot_row(sql, "lease_owner", bot) == (STUDENT_WORKER_ID,), 15,
                "o worker religado não assumiu o bot")
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=15)
    settled("b_superseded", ("done", 1, None))
    settled("b_resend", ("done", 1, None))
    codes["b_resend"] = email_code(emails(4)[3], STUDENT_B, retried, before)

    # 5. Troca de conta de A: reenvio da coordenação e termo e aceite em A2.
    before = real_now()
    assert resend_invites(client, ADMIN_TOKEN, STUDENT_OFFERING, [pa]) == resent
    record_sends(("a_resend", pa))
    settled("a_resend", ("done", 1, None))
    codes["a_resend"] = email_code(emails(5)[4], STUDENT_A, retried, before)
    processed("a2_start", bot_api.push_start(bot, TG_STUDENT_A2, codes["a_resend"]), "student_terms_presented")
    delivered(TG_STUDENT_A2, 1)
    processed("a2_accept", bot_api.push_text(bot, TG_STUDENT_A2, "/aceito", bot_command=True),
              "student_endpoint_replaced")
    delivered(TG_STUDENT_A2, 2)

    # 6. Autosserviço em B: fora da lista (sem envio) e, com o gatilho de falha do envio, o e-mail
    # de B em minúsculas (a comparação não diferencia maiúsculas).
    processed("b_outside", bot_api.push_text(bot, TG_STUDENT_B, f"/convite {OUTSIDE_EMAIL}", bot_command=True),
              "invite_requested")
    delivered(TG_STUDENT_B, 1)
    assert [send_id for send_id, _ in _sends(sql, STUDENT_OFFERING)] == sorted(sends.values()), (
        "o e-mail fora da lista gerou envio")
    typed = STUDENT_B.email.lower()
    _install_convite_failure(sql)
    update_id = bot_api.push_text(bot, TG_STUDENT_B, f"/convite {typed}", bot_command=True)
    assert update_id is not None, "b_convite: a Bot API falsa descartou o update"
    updates["b_convite"] = update_id
    _wait_until(lambda: _update_task(sql, update_id) == ("pending", 1, "RaiseException"), 15,
                "o /convite não falhou de forma temporária pelo gatilho")
    literal = sql.execute("SELECT text, state, outcome FROM telegram_updates WHERE update_id = %s",
                          (update_id,)).fetchone()
    # Controle positivo: enquanto a tarefa está `pending`, o texto literal tem o e-mail.
    assert literal == (f"/convite {typed}", "pending", None)
    b_email_labels = {f"{prefix}:{STUDENT_B.email}" for prefix in ("aluno-email", "aluno-email-local",
                                                                    "aluno-email-dominio")}
    assert b_email_labels <= set(find_canary(literal[0], prefixes=STUDENT_PREFIXES)), "controle positivo do literal"
    assert [send_id for send_id, _ in _sends(sql, STUDENT_OFFERING)] == sorted(sends.values())
    assert [m.text for m in bot_api.sent_messages(bot, chat_id=b)] == [
        _compose_student(INVITE_REQUESTED, course, term)], "a falha temporária não responde"
    _remove_convite_failure(sql)
    before = real_now()
    clock.advance(RETRY_ADVANCE_SECONDS)
    requested = clock.now()
    _wait_until(lambda: _update_state(sql, update_id) == ("processed", "invite_requested"), 15,
                "o /convite não foi processado depois da remoção do gatilho")
    delivered(TG_STUDENT_B, 2)
    record_sends(("b_self", pb))
    settled("b_self", ("done", 1, None))
    codes["b_self"] = email_code(emails(6)[5], STUDENT_B, requested, before)

    # 7. Termo e recusa de B e a adesão (leitura, sem efeito).
    processed("b_start", bot_api.push_start(bot, TG_STUDENT_B, codes["b_self"]), "student_terms_presented")
    delivered(TG_STUDENT_B, 3)
    processed("b_refuse", bot_api.push_text(bot, TG_STUDENT_B, "/recuso", bot_command=True), "student_terms_refused")
    delivered(TG_STUDENT_B, 4)
    assert students_summary(client, ADMIN_TOKEN, STUDENT_OFFERING) == {
        "offering": ref, "enrolled": 2, "no_invite": 0, "sending": 0, "invite_valid": 0, "terms_presented": 1,
        "invite_lapsed": 0, "send_failed": 0, "linked": 1}

    # 8. O link de S, vencido, em B; o worker para.
    clock.advance(EXPIRY_ADVANCE.total_seconds())
    expired = clock.now()
    processed("b_expired", bot_api.push_start(bot, TG_STUDENT_B, codes["b_self"]), "student_refused_invalid")
    delivered(TG_STUDENT_B, 5)
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker da fase de alunos não parou em 10 s"
    assert worker.error is None and worker.returned

    # --- O que saiu para o Telegram: os textos do vínculo com a disciplina e o período redigidos ---
    terms, welcome = _compose_student(TERMS, course, term), _compose_student(WELCOME_STUDENT, course, term)
    refused, requested_text = (_compose_student(TERMS_REFUSED, course, term),
                               _compose_student(INVITE_REQUESTED, course, term))
    expected_sent = [(a1, terms), (a1, welcome), (a1, ALREADY_LINKED), (a1, OTHER_PERSON), (a2, terms),
                     (a2, welcome), (b, requested_text), (b, requested_text), (b, terms), (b, refused),
                     (b, LINK_INVALID)]
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(bot)] == expected_sent
    assert [(c.params, c.outcome, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ({"chat_id": chat, "text": text}, "ok", 200) for chat, text in expected_sent]
    assert [c.params for c in bot_api.calls("setMyDescription", bot_id=bot)] == [
        {"description": _compose(DESCRIPTION_ACTIVE, course, term)}]
    assert [c.params for c in bot_api.calls("setMyCommands", bot_id=bot)] == [
        {"commands": [{"command": c.command, "description": c.description} for c in COMMANDS]}]

    # --- Gravação: updates (o `/convite` mascarado), envios, convites, matrículas, endereços ---
    def row(chat: int, text: str, command: str, outcome: str) -> list[tuple]:
        return [("message", "private", chat, chat, text, command, "processed", outcome)]

    assert {label: sql.execute(
        "SELECT kind, chat_type, chat_id, sender_id, text, command, state, outcome FROM telegram_updates "
        "WHERE update_id = %s", (uid,)).fetchall() for label, uid in updates.items()} == {
        "a1_start": row(a1, f"/start {codes['a_batch']}", "/start", "student_terms_presented"),
        "a1_accept": row(a1, "/aceito", "/aceito", "student_joined"),
        "a1_again": row(a1, f"/start {codes['a_batch']}", "/start", "student_already_linked"),
        "a1_other": row(a1, f"/start {codes['b_batch']}", "/start", "student_refused_other_person"),
        "a2_start": row(a2, f"/start {codes['a_resend']}", "/start", "student_terms_presented"),
        "a2_accept": row(a2, "/aceito", "/aceito", "student_endpoint_replaced"),
        "b_outside": row(b, MASKED_CONVITE, "/convite", "invite_requested"),
        "b_convite": row(b, MASKED_CONVITE, "/convite", "invite_requested"),
        "b_start": row(b, f"/start {codes['b_self']}", "/start", "student_terms_presented"),
        "b_refuse": row(b, "/recuso", "/recuso", "student_terms_refused"),
        "b_expired": row(b, f"/start {codes['b_self']}", "/start", "student_refused_invalid"),
    }
    assert {label: _update_task(sql, uid) for label, uid in updates.items()} == {
        label: ("done", 2, "RaiseException") if label == "b_convite" else ("done", 1, None) for label in updates}
    assert [send_id for send_id, _ in _sends(sql, STUDENT_OFFERING)] == sorted(sends.values())
    assert {label: _send_task(sql, send_id) for label, send_id in sends.items()} == {
        "a_batch": ("done", 1, None), "b_batch": ("done", 1, None),
        "a_rcpt": ("failed", 1, "PermanentTaskError"), "a_auth": ("failed", 1, "PermanentTaskError"),
        "a_retry": ("done", 2, "RetryTaskLater"), "b_superseded": ("done", 1, None), "b_resend": ("done", 1, None),
        "a_resend": ("done", 1, None), "b_self": ("done", 1, None)}
    # A tarefa não leva código nem e-mail: o payload é só o envio.
    assert {label: sql.execute("SELECT task_type, payload FROM tasks WHERE idempotency_key = %s",
                               (f"invite.email:{send_id}",)).fetchone() for label, send_id in sends.items()} == {
        label: ("invite.email", {"invite_send_id": send_id}) for label, send_id in sends.items()}

    def sent_row(origin: str, pseudo: str, created: datetime, finished: datetime, attempts: int = 1) -> tuple:
        return (pseudo, origin, 168, "sent", None, None, None, attempts, created, finished)

    assert {label: sql.execute(
        "SELECT p.pseudo_id::text, s.origin, s.valid_hours, s.state, s.failure, s.cancel_reason, s.error_type, "
        "s.attempts, s.created_at, s.finished_at FROM invite_sends s JOIN enrollments x ON x.id = s.enrollment_id "
        "JOIN people p ON p.id = x.person_id WHERE s.id = %s", (send_id,)).fetchone()
        for label, send_id in sends.items()} == {
        "a_batch": sent_row("batch", pa, started, started),
        "b_batch": sent_row("batch", pb, started, started),
        "a_rcpt": (pa, "coordination", 168, "failed", "recipient_refused", None, "SMTPRecipientsRefused", 1,
                   started, started),
        "a_auth": (pa, "coordination", 168, "failed", "auth_refused", None, "SMTPAuthenticationError", 1,
                   started, started),
        "a_retry": sent_row("coordination", pa, started, retried, attempts=2),
        "b_superseded": (pb, "coordination", 168, "cancelled", None, "superseded", None, 0, retried, retried),
        "b_resend": sent_row("coordination", pb, retried, retried),
        "a_resend": sent_row("coordination", pa, retried, retried),
        "b_self": sent_row("self_service", pb, requested, requested),
    }

    invite_rows = sql.execute(
        "SELECT p.pseudo_id::text, i.invite_send_id, i.state, i.code_sha256, i.expires_at, i.used_at, i.revoked_at, "
        "u.update_id, i.presented_terms_version FROM invites i JOIN enrollments x ON x.id = i.enrollment_id "
        "JOIN people p ON p.id = x.person_id JOIN offerings o ON o.id = x.offering_id "
        "LEFT JOIN telegram_updates u ON u.id = i.presented_update_id WHERE o.code = %s ORDER BY i.id",
        (STUDENT_OFFERING,)).fetchall()
    digest = invite_code_digest
    batch_invites = {
        pa: (pa, sends["a_batch"], "used", digest(codes["a_batch"]), started + INVITE_VALIDITY, started, None,
             updates["a1_start"], 1),
        pb: (pb, sends["b_batch"], "revoked", digest(codes["b_batch"]), started + INVITE_VALIDITY, None, retried,
             None, None)}
    never_delivered = [row[3] for row in invite_rows[2:5]]  # RCPT 550, AUTH 535 e a 1ª tentativa do RCPT 450
    assert invite_rows == [batch_invites[pseudo] for pseudo in batch_order] + [
        (pa, sends["a_rcpt"], "revoked", never_delivered[0], started + INVITE_VALIDITY, None, started, None, None),
        (pa, sends["a_auth"], "revoked", never_delivered[1], started + INVITE_VALIDITY, None, started, None, None),
        (pa, sends["a_retry"], "revoked", never_delivered[2], started + INVITE_VALIDITY, None, retried, None, None),
        (pa, sends["a_retry"], "revoked", digest(codes["a_retry"]), retried + INVITE_VALIDITY, None, retried, None,
         None),
        (pb, sends["b_resend"], "revoked", digest(codes["b_resend"]), retried + INVITE_VALIDITY, None, requested,
         None, None),
        (pa, sends["a_resend"], "used", digest(codes["a_resend"]), retried + INVITE_VALIDITY, retried, None,
         updates["a2_start"], 1),
        (pb, sends["b_self"], "pending", digest(codes["b_self"]), requested + INVITE_VALIDITY, None, None,
         updates["b_start"], 1),
    ]
    all_digests = [row[3] for row in invite_rows]
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in all_digests) and len(set(all_digests)) == 9
    enrollment = {pa: (pa, "student", "active", started, 1, retried), pb: (pb, "student", "pending", None, None, None)}
    assert sql.execute(
        "SELECT p.pseudo_id::text, x.role, x.state, x.activated_at, x.terms_version, x.terms_accepted_at "
        "FROM enrollments x JOIN people p ON p.id = x.person_id JOIN offerings o ON o.id = x.offering_id "
        "WHERE o.code = %s ORDER BY x.id", (STUDENT_OFFERING,)).fetchall() == [enrollment[p] for p in batch_order]
    assert sql.execute(
        "SELECT p.pseudo_id::text, d.channel, d.chat_id, d.state FROM delivery_endpoints d "
        "JOIN people p ON p.id = d.person_id JOIN bots b ON b.id = d.bot_id WHERE b.telegram_bot_id = %s "
        "ORDER BY d.id", (bot,)).fetchall() == [(pa, "telegram", a1, "inactive"), (pa, "telegram", a2, "active")]
    assert sql.execute(
        "SELECT o.state, o.attempts, o.text, o.last_error, o.recipient_person_id IS NULL, u.update_id "
        "FROM outbox_messages o JOIN bots b ON b.id = o.bot_id "
        "LEFT JOIN telegram_updates u ON u.id = o.reply_to_update_id "
        "WHERE b.telegram_bot_id = %s ORDER BY o.id", (bot,)).fetchall() == [
        ("sent", 1, terms, None, True, updates["a1_start"]),
        ("sent", 1, welcome, None, False, None),
        ("sent", 1, ALREADY_LINKED, None, False, None),
        ("sent", 1, OTHER_PERSON, None, True, updates["a1_other"]),
        ("sent", 1, terms, None, True, updates["a2_start"]),
        ("sent", 1, welcome, None, False, None),
        ("sent", 1, requested_text, None, True, updates["b_outside"]),
        ("sent", 1, requested_text, None, True, updates["b_convite"]),
        ("sent", 1, terms, None, True, updates["b_start"]),
        ("sent", 1, refused, None, True, updates["b_refuse"]),
        ("sent", 1, LINK_INVALID, None, True, updates["b_expired"])]

    # --- O SMTP falso: 6 e-mails, uma conexão por envio que conectou; os códigos entram no registro ---
    address = {pa: address_a, pb: _envelope_address(STUDENT_B.email)}
    delivered_to = [*batch_order, pa, pb, pa, pb]
    delivered_codes = [codes[label] for label in (*batch_labels, "a_retry", "b_resend", "a_resend", "b_self")]
    assert [(link.rcpt_to, link.bot_username, link.code, link.acknowledged) for link in smtp.invite_links()] == [
        ((address[pseudo],), username, code, True) for pseudo, code in zip(delivered_to, delivered_codes, strict=True)]
    assert len(smtp.connections()) == 9, "uma conexão por tentativa de envio (o envio superado não conecta)"
    assert set(delivered_codes) <= set(all_secrets()), "os códigos lidos do e-mail entram no registro"
    return _StudentPhase(offering, bot, username, course, term, started, retried, requested, expired, people,
                         people_order, batch_order, sends, codes, updates, literal[0])


def _report_phase_event(e: dict) -> bool:
    """Relatórios: a exceção nomeada dos recortes de eventos por oferecimento do vínculo do aluno e do disparo:
    os eventos (`REPORT_EVENT_TYPES`, que levam `offering_id`) e a posse do worker com bots da fase de relatórios
    (`REPORT_WORKER_ID`, que consulta todos os bots elegíveis, como o da fase de disparos). Cada recorte que a usa
    confere, na mesma função, o subconjunto excluído com a contagem exata."""
    return e["type"] in REPORT_EVENT_TYPES or (e["type"] == "bot.lease_changed"
                                           and e["data"].get("worker_id") == REPORT_WORKER_ID)


def _report_lease_pair(oid: str, report_run: _ReportPhase) -> list[tuple]:
    """A posse do worker com bots da fase de relatórios sobre um bot elegível de outra fase: assumida na
    subida (`started`) e solta na parada ordenada (`bots_stopped`), uma de cada."""
    def lease(change: str, reason: str, at: datetime) -> tuple:
        return ("bot.lease_changed", iso(at), None, {"offering_id": oid, "worker_id": REPORT_WORKER_ID,
                                                     "change": change, "reason": reason})

    return [lease("acquired", "acquired", report_run.started), lease("released", "shutdown", report_run.bots_stopped)]


def _check_student_events(api: _Api, run: _StudentPhase, sql: psycopg.Connection,
                          report_run: _ReportPhase) -> None:
    """Os eventos exatos da fase de alunos, na ordem, só com
    pseudônimos, sem código, e-mail nem chat.

    A posse e o perfil correm em threads diferentes do worker, e, ao religar o worker, a posse corre
    junto das tarefas pendentes: nessas duas janelas não há ordem entre a posse e o resto.

    Relatórios: o recorte por oferecimento exclui a exceção nomeada `_report_phase_event` (os eventos dos relatórios
    e a posse do worker da fase de relatórios), e o subconjunto excluído é conferido exatamente na
    mesma função: o aviso `invite_failures` da janela das duas falhas de e-mail da coordenação (`a_rcpt` e `a_auth`,
    no relógio `t0`; regra `notice.invite_window`), `no_recipient` (o oferecimento não tem
    equipe), registrado por um dos workers com bots depois de `t0 + 900 s` (o do salto de `EXPIRY_ADVANCE` ou um
    seguinte; o horário não é fixado), e a posse assumida e solta pelo worker da fase de relatórios.
    """
    events = sorted(api.events, key=lambda e: e["seq"])
    oid = run.offering["offering_id"]
    t0, t1, t2, t3 = (iso(moment) for moment in (run.started, run.retried, run.requested, run.expired))
    pa, pb = run.pseudo(STUDENT_A), run.pseudo(STUDENT_B)
    first, second = run.batch_order
    # Fora a posse que o worker da fase de disparos assume e solta depois, com o
    # bot desta fase ainda elegível (todos os bots elegíveis são consultados); é a posse de outra fase.
    mine_full = [e for e in events if e["data"].get("offering_id") == oid
                 and not (e["type"] == "bot.lease_changed" and e["data"].get("worker_id") == DISPATCH_WORKER_ID)
                 and not _report_phase_event(e)]
    mine = [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in mine_full]
    # Relatórios: o subconjunto excluído, exato (tipos, quantidade e dados; o horário do aviso
    # não é fixado). No esqueleto, não há evento dos relatórios nem fase de relatórios.
    excluded = [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in events
                if e["data"].get("offering_id") == oid and _report_phase_event(e)]
    window_notice = ("notice.recorded", None, {
        "offering_id": oid, "notice_type": "invite_failures", "dispatch_id": None, "report_version": None,
        "episode_seq": None, "batch_id": None, "window_started_at": t0, "local_date": None, "state": "no_recipient",
        "recipient_count": 0})
    assert [(kind, actor, data) for kind, _, actor, data in excluded if kind == "notice.recorded"] == [window_notice], (
        excluded)
    assert [item for item in excluded if item[0] != "notice.recorded"] == _report_lease_pair(oid, report_run), excluded
    assert len(excluded) == 3, excluded

    def queued(origin: str, count: int, at: str) -> tuple:
        return ("student.invites_queued", at, "bootstrap", {"offering_id": oid, "origin": origin, "queued": count})

    def sent(person: str, origin: str, at: datetime) -> tuple:
        return ("student.invite_sent", iso(at), None, {"offering_id": oid, "person": person, "origin": origin,
                                                       "expires_at": iso(at + INVITE_VALIDITY)})

    def revoked(person: str, origin: str, reason: str, at: str) -> tuple:
        return ("student.invite_revoked", at, None, {"offering_id": oid, "person": person, "origin": origin,
                                                     "revoke_reason": reason})

    def failed(failure: str) -> tuple:
        return ("student.invite_failed", t0, None, {"offering_id": oid, "person": pa, "origin": "coordination",
                                                    "failure": failure})

    def joined(replaced: bool, at: str) -> tuple:
        return ("student.joined", at, None, {"offering_id": oid, "person": pa, "terms_version": 1,
                                             "endpoint_replaced": replaced})

    def lease(change: str, reason: str, at: str) -> tuple:
        return ("bot.lease_changed", at, None, {"offering_id": oid, "worker_id": STUDENT_WORKER_ID, "change": change,
                                                "reason": reason})

    assert len(mine) == 32, mine
    assert mine[:3] == [
        ("offering.created", t0, "bootstrap", {"offering_id": oid, "code": STUDENT_OFFERING}),
        ("bot.registered", t0, "bootstrap",
         {"offering_id": oid, "telegram_bot_id": run.bot_id, "username": run.username}),
        ("offering.state_changed", t0, "bootstrap", {"offering_id": oid, "from_state": "draft", "to_state": "active"}),
    ], mine
    assert sorted(mine[3:5], key=repr) == sorted([
        lease("acquired", "acquired", t0),
        ("bot.profile_updated", t0, None, {"offering_id": oid, "profile_state": "configured", "purpose": "active"}),
    ], key=repr), mine
    assert mine[5:22] == [
        ("student.list_imported", t0, "bootstrap", {"offering_id": oid, "received": 2, "people_created": 2,
                                                    "people_reused": 0, "enrollments_created": 2,
                                                    "already_enrolled": 0}),
        ("student.invites_prepared", t0, "bootstrap", {"offering_id": oid, "eligible": 2, "valid_hours": 168,
                                                       "expires_at": iso(run.started + CONFIRMATION_VALIDITY)}),
        queued("batch", 2, t0),
        sent(first, "batch", run.started),
        sent(second, "batch", run.started),
        joined(False, t0),
        queued("coordination", 1, t0),
        failed("recipient_refused"),
        queued("coordination", 1, t0),
        failed("auth_refused"),
        queued("coordination", 1, t0),
        revoked(pa, "coordination", "replaced", t1),
        sent(pa, "coordination", run.retried),
        revoked(pb, "batch", "refused", t1),
        lease("released", "shutdown", t1),
        queued("coordination", 1, t1),
        queued("coordination", 1, t1),
    ], mine
    acquired = lease("acquired", "acquired", t1)
    window = mine[22:25]
    assert acquired in window and [e for e in window if e != acquired] == [
        ("student.invite_cancelled", t1, None, {"offering_id": oid, "person": pb, "origin": "coordination",
                                                "cancel_reason": "superseded"}),
        sent(pb, "coordination", run.retried),
    ], mine
    assert mine[25:] == [
        queued("coordination", 1, t1),
        revoked(pa, "coordination", "replaced", t1),
        sent(pa, "coordination", run.retried),
        joined(True, t1),
        revoked(pb, "coordination", "replaced", t2),
        sent(pb, "self_service", run.requested),
        lease("released", "shutdown", t3),
    ], mine

    # A importação grava um `person.registered` por aluno criado, na ordem embaralhada de `people.id`,
    # na transação da importação (antes do `student.list_imported`).
    registered = [e for e in events if e["type"] == "person.registered" and e["data"]["pseudo_id"] in (pa, pb)]
    assert [(e["occurred_at"], e["actor_token_id"], e["data"]) for e in registered] == [
        (t0, "bootstrap", {"pseudo_id": pseudo, "alias": run.people[pseudo]["alias"]}) for pseudo in run.people_order]
    assert all(mine_full[4]["seq"] < e["seq"] < mine_full[5]["seq"] for e in registered), (
        "person.registered fora da importação")
    # As falhas definitivas de e-mail deixam a tarefa `failed`, com o `task.failed` da fila de tarefas (só o tipo).
    for label in ("a_rcpt", "a_auth"):
        (task_id,) = sql.execute("SELECT id FROM tasks WHERE idempotency_key = %s",
                                 (f"invite.email:{run.sends[label]}",)).fetchone()
        task_failed = _only([e for e in events if e["type"] == "task.failed" and e["data"]["task_id"] == task_id],
                            f"task.failed do envio {label}")
        assert task_failed == {"seq": task_failed["seq"], "type": "task.failed", "occurred_at": t0,
                               "actor_token_id": None, "data": {"task_id": task_id, "task_type": "invite.email",
                                                                "attempts": 1, "error": "PermanentTaskError"}}
    assert len([e for e in events if e["type"] == "task.failed"
                and e["data"]["task_type"] in ("invite.email", "telegram.update")]) == 2
    # Nenhuma falha de entrega nem resposta perdida na fase (sem bloqueio nem chat inalcançável).
    messages = {row[0] for row in sql.execute(
        "SELECT o.id FROM outbox_messages o JOIN bots b ON b.id = o.bot_id WHERE b.telegram_bot_id = %s",
        (run.bot_id,)).fetchall()}
    assert len(messages) == 11
    assert [e for e in events if e["type"] in ("outbox.failed", "outbox.reply_failed")
            and e["data"]["message_id"] in messages] == []


def _check_student_logs(records: list[dict], run: _StudentPhase) -> None:
    """Desfechos da tarefa `invite.email`: exatamente um `invite.email` (INFO, só `invite_send_id` e `result`)
    por execução da tarefa que chegou ao fim, na ordem, e nenhum `invite.email_superseded`."""
    s = run.sends
    pa = run.pseudo(STUDENT_A)
    batch = [("a_batch" if pseudo == pa else "b_batch") for pseudo in run.batch_order]
    expected = [(s[label], "sent") for label in batch] + [
        (s["a_rcpt"], "failed"), (s["a_auth"], "failed"), (s["a_retry"], "retry"), (s["a_retry"], "sent"),
        (s["b_superseded"], "cancelled"), (s["b_resend"], "sent"), (s["a_resend"], "sent"), (s["b_self"], "sent")]
    email_records = [r for r in records if r.get("event") in ("invite.email", "invite.email_superseded")]
    assert all("time" in r for r in email_records)
    assert [{k: v for k, v in r.items() if k != "time"} for r in email_records] == [
        {"level": "INFO", "logger": "cq_server.worker", "event": "invite.email", "invite_send_id": send_id,
         "result": result} for send_id, result in expected]


def _student_invite_digests(sql: psycopg.Connection) -> list[str]:
    """Os resumos (`invites.code_sha256`) de todos os convites de aluno, inclusive os dos códigos nunca
    entregues: o conjunto do detector genérico de código de convite."""
    return [row[0] for row in sql.execute(
        "SELECT i.code_sha256 FROM invites i JOIN enrollments x ON x.id = i.enrollment_id "
        "WHERE x.role = 'student' ORDER BY i.id").fetchall()]


def _student_label_allowed(key: tuple[str, str], label: str) -> bool:
    """A exceção exata da identidade do aluno no banco: `identities` só com os alvos da lista da
    turma; o id do Telegram do aluno só nas colunas internas do chat; nada mais."""
    prefix = label.split(":", 1)[0]
    if key[0] == "identities":
        return prefix in STUDENT_LIST_PREFIXES
    if key in INTERNAL_IDENTITY_COLUMNS:
        return prefix == "aluno-tg-id"
    return False


def _check_student_surfaces(exchanges: list[_Exchange], runs: list[_CliRun], lines: list[str], bot_api: Any,
                            smtp: Any, db_url: str, run: _StudentPhase, digests: list[str]) -> None:
    """Vínculo do aluno: alvos de aluno e códigos de
    convite de aluno em todas as superfícies, com as exceções exatas e os controles positivos."""

    def student_hits(*texts: str) -> list[str]:
        return sorted({hit for text in texts for hit in find_canary(text, prefixes=STUDENT_PREFIXES)})

    def code_hits(*texts: str) -> list[str]:
        return sorted({hit for text in texts for hit in find_invite_codes(text, digests)})

    # Respostas da API (cabeçalhos, corpo e URL do pedido), sem exceção nenhuma (não há revelação de aluno),
    # fora o segmento `{ref}` da URL do pedido que o próprio teste manda com o RA do aluno A como referência
    # (`students summary <RA>` em `_student_cli_phase`: a superfície `path_ra`, 404 `not_found` sem eco).
    # O resto da URL, os cabeçalhos e o corpo desse pedido continuam varridos; os códigos, na URL inteira.
    path_ra = f"/v1/offerings/{STUDENT_A.ra}/students/summary"

    def is_path_ra(ex: _Exchange) -> bool:
        return (ex.method, ex.path, ex.status) == ("GET", path_ra, 404)

    def api_student_hits(ex: _Exchange) -> list[str]:
        url = ex.url.replace(path_ra, "/v1/offerings/{ref}/students/summary", 1) if is_path_ra(ex) else ex.url
        return student_hits(ex.headers, ex.body, url)

    assert [(ex.method, ex.path, ex.status, hits) for ex in exchanges
            if (hits := api_student_hits(ex))] == [], "aluno canário em respostas da API"
    assert [(ex.method, ex.path, ex.status, hits) for ex in exchanges
            if (hits := code_hits(ex.headers, ex.body, ex.url))] == [], "código de convite em respostas da API"
    # Controles positivos da exceção: cobre exatamente 1 pedido, o 404 `not_found` cuja URL inteira tem o RA
    # (e só ele); nesse pedido, o corpo, os cabeçalhos e o resto da URL continuam varridos; fora dela (outro
    # método, outro estado, outro aluno), a URL inteira é acusada.
    excepted = [ex for ex in exchanges if is_path_ra(ex)]
    assert len(excepted) == 1, f"a exceção da URL cobre {len(excepted)} pedidos, e não 1"
    assert json.loads(excepted[0].body)["error"]["code"] == "not_found"
    assert student_hits(excepted[0].url) == [f"aluno-ra:{STUDENT_A.ra}"]
    for probe in (dataclasses.replace(excepted[0], body=json.dumps({"ref": STUDENT_A.ra})),
                  dataclasses.replace(excepted[0], headers=f"{excepted[0].headers}\nX-Eco: {STUDENT_A.ra}"),
                  dataclasses.replace(excepted[0], url=f"{excepted[0].url}?eco={STUDENT_A.ra}"),
                  dataclasses.replace(excepted[0], method="POST"), dataclasses.replace(excepted[0], status=200)):
        assert api_student_hits(probe) == [f"aluno-ra:{STUDENT_A.ra}"], probe
    other = dataclasses.replace(excepted[0], path=excepted[0].path.replace(STUDENT_A.ra, STUDENT_B.ra),
                                url=excepted[0].url.replace(STUDENT_A.ra, STUDENT_B.ra))
    assert api_student_hits(other) == [f"aluno-ra:{STUDENT_B.ra}"]
    # Saídas do CLI.
    assert [(r.args, r.json, hits) for r in runs if (hits := student_hits(*r.texts))] == [], "aluno no CLI"
    assert [(r.args, r.json, hits) for r in runs if (hits := code_hits(*r.texts))] == [], "código no CLI"
    # Logs (API e worker).
    assert [(i, hits) for i, line in enumerate(lines) if (hits := student_hits(line))] == [], "aluno nos logs"
    assert [(i, hits) for i, line in enumerate(lines) if (hits := code_hits(line))] == [], "código nos logs"
    # Parâmetros enviados à Bot API falsa; só o `chat_id` do `sendMessage` fica de fora (o destino).
    telegram_leaks = []
    for call in bot_api.calls():
        params = dict(call.params)
        if call.method == "sendMessage":
            params.pop("chat_id", None)
        text = json.dumps(params, ensure_ascii=False)
        if hits := student_hits(text) + code_hits(text):
            telegram_leaks.append((call.method, call.seq, hits))
    assert telegram_leaks == [], f"aluno ou código nos parâmetros enviados ao Telegram: {telegram_leaks}"
    # Controle positivo: o destino do `sendMessage` é o id do chat de cada conta de aluno.
    destinations = {hit for c in bot_api.calls("sendMessage", bot_id=run.bot_id)
                    for hit in find_canary(json.dumps(c.params["chat_id"]), prefixes=("aluno-tg-id",))}
    assert destinations == {f"aluno-tg-id:{u.user_id}" for u in (TG_STUDENT_A1, TG_STUDENT_A2, TG_STUDENT_B)}

    # Mensagens capturadas pelo SMTP falso: o e-mail do aluno só no `RCPT TO` e no `To`; o assunto e o
    # corpo sem nenhum alvo (anteriores e de alunos); no corpo, só o próprio código (o e-mail entregue).
    own = {link.message_seq: link.code for link in smtp.invite_links()}
    messages = smtp.messages()
    assert len(messages) == 6 and sorted(own) == sorted(m.seq for m in messages)
    for message in messages:
        not_to = [value for name, value in message.headers if name.lower() != "to"]
        assert student_hits(message.mail_from, *not_to, message.text) == [], f"aluno no e-mail #{message.seq}"
        for part in (message.header("Subject"), message.text):
            assert (find_canary(part), find_canary(part, prefixes=ALL_PREFIXES),
                    find_canary(part, prefixes=STUDENT_PREFIXES)) == ([], [], []), f"alvo no e-mail #{message.seq}"
        assert code_hits(message.mail_from, *[value for _, value in message.headers]) == []
        assert find_invite_codes(message.text, digests) == [invite_code_label(invite_code_digest(own[message.seq]))]
        (address,) = message.rcpt_to
        student = STUDENT_A if address == _envelope_address(STUDENT_A.email) else STUDENT_B
        email_labels = {f"{prefix}:{student.email}" for prefix in ("aluno-email", "aluno-email-local",
                                                                   "aluno-email-dominio")}
        assert email_labels <= set(student_hits(address)) and email_labels <= set(student_hits(message.header("To")))

    # Banco: todas as colunas de todas as tabelas.
    mail = mail_secrets(smtp)
    student_db: dict[tuple[str, str], set[str]] = {}
    code_leaks: list[tuple[str, str, list[str]]] = []
    start_codes: set[str] = set()
    mail_leaks: list[tuple[str, str, list[str]]] = []
    for table, column, text in scan_all_tables(db_url):
        found = find_canary(text, prefixes=STUDENT_PREFIXES)
        if found:
            student_db.setdefault((table, column), set()).update(found)
        codes_found = find_invite_codes(text, digests)
        start = re.fullmatch(r"/start ([A-Za-z0-9_-]{22})", text)
        if codes_found and (table, column) == ("telegram_updates", "text") and start is not None and (
                codes_found == [invite_code_label(invite_code_digest(start.group(1)))]):
            start_codes.update(codes_found)  # a exceção exata do `/start <código>` gravado
        elif codes_found:
            code_leaks.append((table, column, codes_found))
        if leaked := [f"smtp:{i}" for i, secret in enumerate(mail) if secret in text]:
            mail_leaks.append((table, column, leaked))
    outside = {key: sorted(label for label in labels if not _student_label_allowed(key, label))
               for key, labels in student_db.items()}
    assert {key: labels for key, labels in outside.items() if labels} == {}, "aluno canário fora das exceções"
    assert code_leaks == [], f"código de convite de aluno no banco: {code_leaks}"
    assert mail_leaks == [], f"senha do SMTP no banco: {mail_leaks}"
    # Controles positivos de cada exceção.
    list_labels = {label for label, _ in STUDENT_TARGETS if label.split(":", 1)[0] in STUDENT_LIST_PREFIXES}
    assert set().union(*(labels for key, labels in student_db.items() if key[0] == "identities")) == list_labels
    ids = {f"aluno-tg-id:{u.user_id}" for u in (TG_STUDENT_A1, TG_STUDENT_A2, TG_STUDENT_B)}
    assert student_db.get(("telegram_updates", "chat_id")) == ids
    assert student_db.get(("telegram_updates", "sender_id")) == ids
    assert student_db.get(("delivery_endpoints", "chat_id")) == {
        f"aluno-tg-id:{u.user_id}" for u in (TG_STUDENT_A1, TG_STUDENT_A2)}
    assert start_codes == {invite_code_label(invite_code_digest(run.codes[label]))
                           for label in ("a_batch", "b_batch", "a_resend", "b_self")}
    assert [f"smtp:{i}" for i, secret in enumerate(mail) if secret in f"coluna sintetica {mail[0]}"] == ["smtp:0"]
    assert run.convite_literal == f"/convite {STUDENT_B.email.lower()}"

    # Controles positivos sintéticos: resposta, log, perfil do Telegram e código colado.
    synthetic = _Exchange("GET", "/sintetico", 200, f"X-Eco: {STUDENT_A.ra}", json.dumps({"x": STUDENT_B.name}))
    assert {f"aluno-ra:{STUDENT_A.ra}", f"aluno-nome:{STUDENT_B.name}"} <= set(
        student_hits(synthetic.headers, synthetic.body))
    formatted_line = JsonFormatter().format(logging.makeLogRecord(
        {"name": "cq_server.worker", "levelname": "INFO", "levelno": logging.INFO, "msg": "sintetico",
         "event": f"sintetico {STUDENT_A.email}"}))
    assert f"aluno-email:{STUDENT_A.email}" in student_hits(formatted_line)
    synthetic_tg = f"X-Eco: {TG_STUDENT_A2.full_name} @{TG_STUDENT_A2.username}"
    assert {f"aluno-tg-nome:{TG_STUDENT_A2.full_name}", f"aluno-tg-username:{TG_STUDENT_A2.username}"} <= set(
        find_canary(synthetic_tg, prefixes=STUDENT_PREFIXES))
    code = run.codes["b_self"]
    assert code_hits(f"x{code}y") == [invite_code_label(invite_code_digest(code))]


def _summary_lines(code: str, body: dict) -> list[str]:
    """A saída em texto de `students summary`."""
    return [f"Adesão de {code}: {body['enrolled']} alunos matriculados.",
            f"  sem convite: {body['no_invite']}", f"  envio em andamento: {body['sending']}",
            f"  convite válido: {body['invite_valid']}", f"  termo apresentado: {body['terms_presented']}",
            f"  convite vencido ou revogado: {body['invite_lapsed']}", f"  falha no envio: {body['send_failed']}",
            f"  vinculados: {body['linked']}"]


def _student_cli_phase(cli_invoke: Any, client: Any, clock: Any, seed: _Seed, tmp_path: Any, bot_api: Any,
                       run_phase: _StudentPhase) -> list[_CliRun]:
    """Vínculo do aluno: as 5 folhas do vínculo com sucesso (texto e `--json`), com erro
    da API (código 1), com a API inalcançável (código 3) e com uso incorreto (código 2), sempre fora de
    terminal, em oferecimentos próprios (nenhum alvo do `state`). O arquivo da lista tem os alunos
    canário (a identidade entra só pelo arquivo; as saídas têm só contagens e pseudônimos)."""
    runs: list[_CliRun] = []

    def run(args: list[str], exit_code: int, *, json_mode: bool = False, **kwargs: Any) -> Any:
        result = cli_invoke(list(args), json=json_mode, **kwargs)
        leaf = _leaf_of(args)
        assert leaf is not None, f"{args} não é um comando folha do cli"
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{args}: o CLI levantou {type(result.exception).__name__}")
        assert result.exit_code == exit_code, (
            f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
        for text in (result.stdout, result.stderr):
            assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
        if exit_code != 0:
            assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
        runs.append(_CliRun(leaf, tuple(args), json_mode, result.exit_code, result.stdout, result.stderr,
                            result.output))
        return result

    def unreachable(args: list[str], *, json_mode: bool = False) -> Any:
        return run(args, 3, json_mode=json_mode, http=None, env={"CQ_API_URL": UNREACHABLE_API})

    now = clock.now()
    code = CLI_STUDENTS_OFFERING
    offering = create_offering(client, ADMIN_TOKEN, code=code, course="Laboratório de Programação", term="2031s2",
                               timezone=OFFERING_ZONE)
    assert offering == {
        "offering_id": offering["offering_id"], "code": code, "course": "Laboratório de Programação",
        "term": "2031s2", "timezone": OFFERING_ZONE, "response_window_minutes": 1440, "state": "draft",
        "created_at": iso(now), "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
        "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` inteiro
    bot = bot_api.add_bot()
    username = bot_api.state(bot).username
    assert register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot)) == {
        "offering": {"offering_id": offering["offering_id"], "code": code}, "username": username,
        "telegram_bot_id": bot, "state": "registered", "profile_state": "pending", "registered_at": iso(now),
        "token_replaced_at": None, "lease": None, "last_poll_ok_at": None, "problem": None}
    assert activate_bot(client, ADMIN_TOKEN, code) == {
        **offering, "state": "active", "activated_at": iso(now),
        "bot": {"username": username, "telegram_bot_id": bot, "state": "registered", "profile_state": "pending"}}
    draft = create_offering(client, ADMIN_TOKEN, code=CLI_STUDENTS_DRAFT, course="Laboratório de Programação",
                            term="2031s2", timezone=OFFERING_ZONE)
    assert draft == {**offering, "offering_id": draft["offering_id"], "code": CLI_STUDENTS_DRAFT}
    assert draft["offering_id"] != offering["offering_id"] and re.fullmatch(UUID4_RE, draft["offering_id"])
    ref = {"offering_id": offering["offering_id"], "code": code}
    pa, pb = run_phase.pseudo(STUDENT_A), run_phase.pseudo(STUDENT_B)
    aa = run_phase.alias(STUDENT_A)
    z_alias, z_pseudo = seed.alias(ZEFIRO), seed.pseudo(ZEFIRO)
    roster = write_roster(tmp_path / "lista-alunos.csv", [student.row() for student in CANARY_STUDENTS])
    # Controle positivo: o arquivo da lista (entrada do CLI) tem a identidade dos alunos canário.
    roster_labels = {label for s in CANARY_STUDENTS for label in (f"aluno-nome:{s.name}", f"aluno-email:{s.email}")}
    assert roster_labels <= set(find_canary(roster.read_text(encoding="utf-8"), prefixes=STUDENT_PREFIXES))

    # --- sucesso (texto e --json) ---
    r = run(["students", "import", code, "--file", str(roster)], 0)
    assert r.stdout.splitlines() == [
        f"Lista importada em {code}: 2 linhas; 0 pessoas novas, 2 já cadastradas; 2 matrículas novas, "
        "0 já matriculados."]
    r = run(["students", "import", code, "--file", str(roster)], 0, json_mode=True)
    imported = json.loads(r.stdout)
    assert_matches("students.import", 200, imported)
    assert imported == {"offering": ref, "received": 2, "people_created": 0, "people_reused": 2,
                        "enrollments_created": 0, "already_enrolled": 2}
    before = {"offering": ref, "enrolled": 2, "no_invite": 2, "sending": 0, "invite_valid": 0, "terms_presented": 0,
              "invite_lapsed": 0, "send_failed": 0, "linked": 0}
    r = run(["students", "summary", code], 0)
    assert r.stdout.splitlines() == _summary_lines(code, before)

    expires = iso(now + CONFIRMATION_VALIDITY)
    r = run(["invites", "send", "prepare", code, "--valid-hours", "48"], 0)
    lines = r.stdout.splitlines()
    assert len(lines) == 2, lines
    assert lines[0] == f"Enviar convites de {code}: 2 alunos, validade de 48 h a partir do envio.", lines
    confirmation = re.fullmatch(rf"Código de confirmação \(válido até {re.escape(expires)}\): ([A-Za-z0-9_-]{{22}})",
                                lines[1])
    assert confirmation is not None, lines[1]
    r = run(["invites", "send", "confirm", code, "--confirmation", confirmation.group(1)], 0)
    assert r.stdout.splitlines() == [f"Convites enfileirados em {code}: 2."]
    # Os dois alunos estão com envio em andamento (sem worker): nenhum elegível no novo preparo.
    r = run(["invites", "send", "prepare", code], 0, json_mode=True)
    prepared = json.loads(r.stdout)
    assert_matches("invites.prepare_send", 200, prepared)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", prepared["confirmation_code"]), prepared
    assert prepared == {"offering": ref, "eligible": 0, "valid_hours": 168,
                        "confirmation_code": prepared["confirmation_code"], "expires_at": expires}
    r = run(["invites", "send", "confirm", code, "--confirmation", prepared["confirmation_code"]], 0, json_mode=True)
    confirmed = json.loads(r.stdout)
    assert_matches("invites.send", 200, confirmed)
    assert confirmed == {"offering": ref, "queued": 0, "valid_hours": 168}
    after = {**before, "no_invite": 0, "sending": 2}
    r = run(["students", "summary", code], 0, json_mode=True)
    assert json.loads(r.stdout) == after
    assert_matches("students.summary", 200, json.loads(r.stdout))

    # Reenvio com A pelo apelido e pelo identificador (conta uma vez), B, uma pessoa que não é aluna e um
    # pseudônimo de formato válido com o RA canário, sem pessoa (só contado, nunca ecoado).
    resend_args = ["invites", "resend", code, aa, pb, pa, z_alias, RA_ALIAS]
    r = run(resend_args, 0)
    assert r.stdout.splitlines() == [f"Reenvios enfileirados em {code}: 2.", f"Não é aluno do oferecimento: {z_alias}",
                                     "Sem pessoa: 1"]
    r = run([*resend_args, "--valid-hours", "24"], 0, json_mode=True)
    resent = json.loads(r.stdout)
    assert_matches("invites.resend", 200, resent)
    assert resent == {"offering": ref, "queued": 2, "valid_hours": 24,
                      "not_students": [{"pseudo_id": z_pseudo, "alias": z_alias}], "not_found_count": 1}

    # --- erro da API (código 1), com a identidade dos alunos canário na entrada ---
    conflict_file = write_roster(tmp_path / "lista-conflito.csv",
                                 [{"name": STUDENT_A.name, "ra": STUDENT_A.ra, "email": STUDENT_B.email}])
    r = run(["students", "import", code, "--file", str(conflict_file)], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['list_conflict']}", "  - linha 2: duplicate"]
    r = run(["students", "import", code, "--file", str(conflict_file)], 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "list_conflict", "message": MESSAGES["list_conflict"],
                                              "fields": [{"field": "rows[0]", "reason": "duplicate"}]}}
    r = run(["students", "import", code, "--file", str(roster)], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: people:write"]
    r = run(["students", "summary", STUDENT_A.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["students", "summary", code], 1, token=seed.events_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]
    r = run(["invites", "send", "prepare", BOT_OFFERING], 1)  # bot sem acesso (fase de bot)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['bot_required']}"]
    r = run(["invites", "send", "prepare", CLI_STUDENTS_DRAFT], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_state']}", "Estado atual: draft"]
    r = run(["invites", "send", "prepare", CLI_STUDENTS_DRAFT], 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "invalid_state", "message": MESSAGES["invalid_state"],
                                              "state": "draft"}}
    r = run(["invites", "send", "confirm", code, "--confirmation", STUDENT_A.name], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_confirmation']}"]
    r = run(["invites", "resend", code, STUDENT_A.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - pseudonyms[0]: invalid_format"]
    r = run(["invites", "resend", code, aa], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:write"]

    # --- API inalcançável (código 3), uma vez por folha ---
    for args in (["students", "import", code, "--file", str(roster)], ["students", "summary", code],
                 ["invites", "send", "prepare", code],
                 ["invites", "send", "confirm", code, "--confirmation", STUDENT_A.name],
                 ["invites", "resend", code, aa]):
        r = unreachable(args)
        assert r.stderr.splitlines() == [f"Erro: {UNREACHABLE_MESSAGE}"], (args, r.stderr)
    r = unreachable(["students", "import", code, "--file", str(roster)], json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "api_unreachable", "message": UNREACHABLE_MESSAGE}}

    # --- uso incorreto (código 2): arquivo fora do formato ou ausente, opção tipada, argumento ---
    malformed = write_roster(tmp_path / "lista-colunas.csv",
                             [[STUDENT_A.ra, STUDENT_A.name, STUDENT_A.email, STUDENT_B.email]])
    r = run(["students", "import", code, "--file", str(malformed)], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: a linha 2 do arquivo da lista não tem as 3 colunas do cabeçalho."]
    r = run(["students", "import", code, "--file", str(tmp_path / "lista-inexistente.csv")], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: não foi possível ler o arquivo da lista."]
    for args in (["invites", "send", "prepare", code, "--valid-hours", STUDENT_A.name],
                 ["invites", "resend", code, aa, "--valid-hours", STUDENT_B.name],
                 ["students", "import", code],
                 ["invites", "resend", code],
                 ["students", "summary", code, STUDENT_A.ra]):
        r = run(args, 2)
        assert r.stderr.startswith("Uso incorreto:"), (args, r.stderr)
    return runs


# ---------------------------------------------------------------------------------------------
# fase de disparos
# ---------------------------------------------------------------------------------------------


@dataclass
class _DispatchPhase:
    offering: dict
    offering_row: int
    bot_id: int  # id do bot no Telegram (o prefixo do token na Bot API falsa)
    username: str
    course: str
    term: str
    started: datetime  # t0: preparo, validação, abertura, respostas e mensagens livres do 1º disparo
    second: datetime  # o relógio ao preparar o 2º disparo (depois do fechamento do 1º)
    third: datetime  # o relógio ao preparar o 3º, o 4º e o 5º disparos (depois do fechamento do 2º)
    people: dict[str, dict]  # "a"/"b" → {pseudo_id, alias, person_id}
    dispatches: dict[str, dict]  # rótulo → corpo final de `dispatches.get`
    dispatch_rows: dict[str, int]  # rótulo → `dispatches.id`
    deliveries: dict[tuple[str, str], str]  # (disparo, aluno) → `delivery_id`
    # (disparo, aluno, marca) → `submission_id`; a marca é o desfecho, com `graded` só na válida,
    # `format_error` na `graded` inválida e `refused:<recusa>` nas recusadas.
    submissions: dict[tuple[str, str, str], str]
    updates: dict[str, int]  # rótulo → `update_id` do Telegram
    preview: dict  # a prévia da questão canário, lida antes do cancelamento
    messages: dict  # `messages.list` do oferecimento
    last_seq_before_close: int  # a última chamada à Bot API falsa antes do 1º fechamento
    # O relógio nos passos 5.8 (o "Não sei" atrasado do 4º) e 5.9 (o
    # `PATCH` que desliga as mensagens livres), parado depois do fechamento do 3º ao 5º.
    after_close: datetime | None = None

    def pseudo(self, label: str) -> str:
        return self.people[label]["pseudo_id"]

    def mark(self, label: str) -> str:
        return marker(self.people[label]["alias"])


def _dispatch_labels(*texts: str) -> set[str]:
    return {hit for text in texts for hit in find_canary(text, prefixes=DISPATCH_PREFIXES)}


def _bot_outputs(rich: Any, bot_id: int) -> list[tuple[int, str]]:
    """Os textos do bot da fase como o aluno os viu, com o chat: cada mensagem (estado corrente, ou a legenda),
    cada edição (o depois), cada `sendMessage` e cada confirmação de toque (no chat do toque)."""
    outputs = [(m.chat_id, m.plain_text or m.caption or "") for m in rich.messages(bot_id)]
    outputs += [(e.chat_id, e.after.plain_text or "") for e in rich.edits(bot_id)]
    outputs += [(m.chat_id, m.text or "") for m in rich.sent_messages(bot_id)]
    outputs += [(a.tap.chat_id, a.text or "") for a in rich.answers(bot_id)]
    return outputs


def _bot_button_labels(rich: Any, bot_id: int) -> set[str]:
    """Os rótulos de todos os botões que o bot da fase mostrou (mensagens e os dois lados de cada edição)."""
    labels = {b.label for m in rich.messages(bot_id) for b in m.buttons()}
    return labels | {b.label for e in rich.edits(bot_id) for b in (*e.before.buttons(), *e.after.buttons())}


def _dispatch_phase(client: Any, clock: Any, sql: psycopg.Connection, rich: Any, bot_worker_thread: Any,
                    settings: Any, engine_fake: Any, repo_fetcher: Any) -> _DispatchPhase:
    """Fase de disparos, em sequência fixa: um oferecimento próprio, com
    bot próprio na Bot API falsa estendida (`rich_bot_api`), o motor falso estrito (`engine_fake`) e a
    obtenção falsa (`repo_fetcher`), e um worker próprio com bots (`run_worker(…, bots=…, engine=,
    fetcher=, delivery=)`, `worker_id` fixo); os alunos canário do disparo importados pela API (a identidade que
    a redação usa) e vinculados por SQL (matrícula `active`, termo aceito e endereço `active`: A pela conta
    A1, B pela sua conta; o fluxo do vínculo é o da fase de alunos). Todos os disparos são imediatos,
    no mesmo repositório e commit, com `origin = "api"`:
    0. `/start` sem código de A1 e de B (resposta `HELP` do bot: o chat fica conhecido da Bot API falsa);
    1. a **questão canário** (título, enunciado, rótulo e aviso com o nome, o RA e o e-mail de B; o nome na
       dica) é preparada, validada (prévia redigida, controle positivo) e cancelada pela coordenação;
    2. a questão **não suportada** com a mesma identidade no motivo do motor é rejeitada (motivo redigido);
    3. o **1º disparo** (texto com figura) abre para A e B: A responde por "responder" ao pedido do campo
       com a identidade de B (o eco volta só ao chat de A), a correção sai (`submission.graded`, ❌); B toca
       "Não sei" (`delivery.dont_know`) e toca de novo no botão antigo (recusa `already_answered`); A
       escreve duas mensagens livres com a identidade de B (a resposta fixa sai uma vez); o disparo fecha
       pelo roteiro determinístico e as duas entregas são reveladas (o gabarito com a sentinela);
    4. o **2º disparo** (numérico, quebrado na correção) abre com o chat de B bloqueado na Bot API falsa (o
       envio recebe 403: `not_delivered/endpoint_blocked`); A responde, o motor recusa a correção (400:
       `submission.internal_failure`, a entrega é reaberta), A responde de novo e a correção quebra
       (`delivery.question_failed/grading_broken`); o disparo fecha pelo mesmo roteiro.
    5. o **3º disparo** (numérico, `number`) e o **4º** (`missing_media` com duas partes: número e escolha
       múltipla pelos botões; a figura ausente vira `[Figura 1 indisponível]`) abrem juntos; o endereço de B
       continua bloqueado desde o 2º (`not_delivered/endpoint_blocked` na abertura, sem mensagem), então só
       A recebe. Com os dois campos digitáveis abertos, A escreve sem "responder" (`USE_REPLY`, mensagem
       livre ambígua) e manda uma figurinha em resposta ao pedido do 4º (`REPLY_WITH_TEXT`); no 3º, A
       responde com a identidade de B (erro de formato: a explicação traduzida, `FORMAT_ERROR`, e o novo
       pedido), toca o "Não sei" da versão antiga (`PART_ALREADY_ANSWERED`), responde certo com a correção
       presa no motor (`DISPATCH_GRADE_HOLD`) e toca "Não sei" durante ela (`GRADING_IN_PROGRESS`), e a
       correção sai ✅ (`VERDICT_CORRECT`); no 4º, A responde só a 1ª parte (o progresso e o botão
       "Enviar"). O **5º disparo** (`multiple_choice` com 13 alternativas, `DISPATCH_MANY_OPTIONS`) abre junto
       com o 3º e o 4º: as alternativas mais os controles
       passam de `max_buttons`, então a múltipla degrada (aviso `too_many_options` na prévia; alternativas
       listadas, sem botão de alternativa nem "Enviar", com o "Não sei") e a dica `TYPED_MULTIPLE_HOWTO` sai
       na mensagem da questão, depois das alternativas.
       A responde o 5º no prazo, digitando "abc" ao pedido do campo: a nota
       parcial 1/6, com "◐ parcialmente correta · nota 16%" (`VERDICT_PARTIAL`, `SCORE`) e as marcas por
       alternativa. Os três fecham pelo mesmo roteiro: a entrega sem resposta do 4º vira `no_answer`, **sem** o
       gabarito, com o `LATE_HOWTO` e o "Não sei" na edição do fechamento; o 3º e o 5º, respondidos,
       recebem a revelação. Depois do fechamento, A toca o "Não sei" antigo do 4º (de antes do fechamento:
       numa `no_answer`, o "Não sei" vale de qualquer versão: o "Não sei" atrasado, `dont_know` com a
       revelação na mesma edição e a confirmação sem texto) e manda `/naosei` em resposta à questão dele
       (`ALREADY_ANSWERED`, pela fila); toca o "Não sei" antigo da entrega `question_failed` do 2º
       (`UNAVAILABLE`).
       Por fim (passo 5.9), a
       coordenação desliga as mensagens livres pela API (`offering.settings_changed`), e A manda duas
       mensagens livres com a identidade de B: a 1ª recebe a resposta com o contato do professor
       (`ANSWERS_ONLY_REPLY` e `CONTACT_HOWTO`; `free_message_masked_replied`), a 2ª, no mesmo intervalo, nada
       (`free_message_masked`); nenhuma vira registro, e o texto dos dois updates é mascarado. O oferecimento
       da fase é cadastrado com as mensagens livres ligadas e com o contato `PROFESSOR_EMAIL`, então as
       prévias não ganham o aviso `no_teacher_contact`.
    Emite os 14 tipos de evento do disparo, faz sair todo texto do bot (`DISPATCH_PHASE_TEXT_NAMES`, fora as exceções
    por nome de `DISPATCH_TEXT_EXCEPTIONS`) e termina parando o próprio worker. Nenhum caso cujo número de
    rodadas dependa de tempo real (a entrada gravada depois do horário e o motor fora do ar na correção
    ficam em outros testes); a única parcela medida é a das rodadas `dispatch.close`. A correção
    presa do 3º disparo espera `DISPATCH_GRADE_HOLD` s reais, sem mudar o número de rodadas: o toque é
    processado antes de ela sair (conferido pela chamada ainda aberta no motor falso).
    """
    started = clock.now()
    for text in (*DISPATCH_FREE_TEXTS, DISPATCH_ANSWER, DISPATCH_UNSUPPORTED):
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == [], f"texto com alvo anterior: {text!r}"
        assert _dispatch_labels(text), f"controle positivo: o texto injetado tem alvo disparo-*: {text!r}"
    # Mensagens livres opcionais: as mensagens do modo desligado têm a identidade de B (controle positivo) e nenhum
    # alvo das fases anteriores; o e-mail do professor e a resposta de A ao 5º, nenhum alvo canário.
    for text in DISPATCH_FREE_OFF_TEXTS:
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == [], f"texto com alvo anterior: {text!r}"
        assert _dispatch_labels(text), f"controle positivo: o texto injetado tem alvo disparo-*: {text!r}"
    for text in (PROFESSOR_EMAIL, DISPATCH_D5_ANSWER):
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES + DISPATCH_PREFIXES) == [], text
    a_chat, b_chat = TG_DISPATCH_A1.user_id, TG_DISPATCH_B.user_id

    # 0. Oferecimento, bot, alunos (lista pela API, vínculo por SQL) e o worker com motor e obtenção. Mensagens livres:
    # o cadastro liga as mensagens livres e define o contato do professor (os passos 3 a 5.8 rodam no modo ligado,
    # o 5.9, no desligado); as saídas mostram só `contact_email_set`.
    offering = create_offering(client, ADMIN_TOKEN, code=DISPATCH_OFFERING, course=DISPATCH_COURSE,
                               term=DISPATCH_TERM, timezone=OFFERING_ZONE, free_messages=True,
                               contact_email=PROFESSOR_EMAIL)
    assert (offering["free_messages"], offering["contact_email_set"]) == (True, True), offering
    offering_id = offering["offering_id"]
    ref = {"offering_id": offering_id, "code": DISPATCH_OFFERING}
    bot = rich.add_bot()
    username = rich.state(bot).username
    assert register_bot(client, ADMIN_TOKEN, DISPATCH_OFFERING, rich.token_of(bot))["state"] == "registered"
    assert activate_bot(client, ADMIN_TOKEN, DISPATCH_OFFERING)["state"] == "active"
    # A consulta do oferecimento com contato mostra só o booleano (o e-mail nunca sai na API; a
    # varredura de `_check_professor_email` cobre esta resposta e a da lista).
    shown_offering = client.get(f"/v1/offerings/{DISPATCH_OFFERING}", headers=ADMIN)
    assert shown_offering.status_code == 200, shown_offering.text[:300]
    assert (shown_offering.json()["free_messages"], shown_offering.json()["contact_email_set"]) == (True, True)
    listed_offerings = client.get("/v1/offerings", params={"limit": 500}, headers=ADMIN)
    assert listed_offerings.status_code == 200, listed_offerings.text[:300]
    assert [(o["free_messages"], o["contact_email_set"]) for o in listed_offerings.json()["items"]
            if o["code"] == DISPATCH_OFFERING] == [(True, True)]
    assert import_students(client, ADMIN_TOKEN, DISPATCH_OFFERING, [s.row() for s in CANARY_DISPATCH_STUDENTS]) == {
        "offering": ref, "received": 2, "people_created": 2, "people_reused": 0, "enrollments_created": 2,
        "already_enrolled": 0}
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s", (offering_id,)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    people: dict[str, dict] = {}
    for label, student, chat in (("a", DISPATCH_A, a_chat), ("b", DISPATCH_B, b_chat)):
        person_id, pseudo, alias = sql.execute(
            "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
            "WHERE i.ra = %s", (student.ra,)).fetchone()
        now = clock.now()
        assert sql.execute(
            "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, terms_accepted_at = %s "
            "WHERE person_id = %s AND offering_id = %s AND role = 'student'",
            (now, now, person_id, offering_row)).rowcount == 1
        sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_id, bot_row, chat, now, now))
        people[label] = {"pseudo_id": pseudo, "alias": alias, "person_id": person_id}
    b_mark = marker(people["b"]["alias"])
    redacted = f"{b_mark} (RA {b_mark}, {b_mark})"

    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "canary"),
                        preset("canary", identity=DISPATCH_IDENTITY, hint=DISPATCH_B.name))
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "unsupported"), preset("unsupported", inspect_reasons=(
        FakeReason("unsupported_elements", DISPATCH_UNSUPPORTED, ("pl-matrix-input",)),)))
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "with_media"),
                        preset("with_media", fields=(text_field(),), explanation="O céu limpo é azul."))
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "broken_on_grade"), "broken_on_grade")
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "number"), preset("number"))
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "missing_media"), preset(
        "missing_media", fields=(number_field("num"), multiple_choice_field("cb"))))
    # O 5º disparo: a múltipla com mais alternativas que `max_buttons` (degradada).
    engine_fake.program((DISPATCH_REPO, DISPATCH_COMMIT, "multiple_choice"), preset(
        "multiple_choice", fields=(multiple_choice_field(correct=("a", "b", "d", "f", "j", "l"),
                                                         options=DISPATCH_MANY_OPTIONS),)))
    for text in (DISPATCH_AMBIGUOUS,):
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES + DISPATCH_PREFIXES) == [], text
    worker = bot_worker_thread(
        settings=dataclasses.replace(settings, worker_id=DISPATCH_WORKER_ID), channel=ConsoleChannel(io.StringIO()),
        engine=HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                                 timeout_seconds=DISPATCH_ENGINE_TIMEOUT)),
        fetcher=repo_fetcher, delivery=DISPATCH_DELIVERY)
    _wait_until(lambda: _bot_row(sql, "profile_state", bot) == ("configured",), 15, "perfil não configurado")
    rich.wait_for(lambda: rich.pending_polls(bot), timeout=15)

    updates: dict[str, int] = {}
    dispatches: dict[str, str] = {}

    def processed(label: str, update_id: int | None, outcome: str) -> None:
        assert update_id is not None, f"{label}: a Bot API falsa descartou o update"
        updates[label] = update_id
        _wait_until(lambda: _update_state(sql, update_id) == ("processed", outcome), DISPATCH_TIMEOUT,
                    f"{label}: update não processado como {outcome}")

    def settled() -> None:
        """Nenhum update do bot `pending`, nenhuma tarefa viva e a fila sem linha `pending`/`leased`."""
        _wait_until(lambda: sql.execute(
            "SELECT (SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "        WHERE b.telegram_bot_id = %s AND u.state = 'pending') + "
            "       (SELECT count(*) FROM tasks WHERE state IN ('pending', 'leased')) + "
            "       (SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased'))",
            (bot,)).fetchone()[0] == 0, DISPATCH_TIMEOUT, "a fase não assentou")

    def body(label: str) -> dict:
        return get_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, dispatches[label])

    def counts(label: str) -> dict:
        return body(label)["deliveries"]

    def prepare(label: str, qid: str, closes_at: datetime) -> dict:
        prepared = prepare_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, repo=DISPATCH_REPO,
                                    commit=DISPATCH_COMMIT, qid=qid, closes_at=iso(closes_at), origin=DISPATCH_ORIGIN)
        assert (prepared["state"], prepared["origin"]) == ("validating", DISPATCH_ORIGIN)
        dispatches[label] = prepared["dispatch_id"]
        _wait_until(lambda: body(label)["state"] != "validating", DISPATCH_TIMEOUT, f"{label}: a validação não terminou")
        settled()
        return body(label)

    def prompts(chat: int) -> list[Any]:
        return [m for m in rich.messages(bot, chat) if m.force_reply]

    def refs(label: str, person: str, role: str) -> list[int]:
        """Os `message_id` das mensagens de entrega enviadas (`send`, `sent`) do papel, na entrega de `person`
        no disparo `label`, na ordem da fila (o `channel_ref` é o `message_id` do Telegram)."""
        rows = sql.execute(
            "SELECT o.channel_ref FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id JOIN dispatches x "
            "ON x.id = d.dispatch_id JOIN enrollments e ON e.id = d.enrollment_id WHERE x.dispatch_uuid = %s "
            "AND e.person_id = %s AND o.delivery_role = %s AND o.operation = 'send' AND o.state = 'sent' "
            "ORDER BY o.id", (dispatches[label], people[person]["person_id"], role)).fetchall()
        return [int(row[0]) for row in rows]

    def dont_know_of(message_id: int) -> str:
        """O dado do botão "Não sei" do teclado corrente da mensagem de A."""
        (data,) = [button.data for button in rich.current(bot, a_chat, message_id).buttons()
                   if button.label == deliverytexts.DONT_KNOW_BUTTON]
        return data

    def close(labels: tuple[str, ...], closes_at: datetime) -> None:
        """O fechamento determinístico: com tudo assentado, o relógio passa de `closes_at` e, em laço
        com prazo real, espera o `dispatch.closed` de cada disparo, avançando `CLOSE_RECHECK_SECONDS` a cada
        espera sem fechamento; depois espera as edições da revelação saírem."""
        settled()
        clock.set(closes_at + timedelta(seconds=1))
        deadline = time.monotonic() + DISPATCH_CLOSE_TIMEOUT

        def all_closed() -> bool:
            return all(body(label)["state"] == "closed" for label in labels)

        while True:
            wait_until = time.monotonic() + 2.0
            while time.monotonic() < wait_until and not all_closed():
                time.sleep(0.1)
            if all_closed():
                break
            assert time.monotonic() < deadline, f"{labels}: o disparo não fechou em {DISPATCH_CLOSE_TIMEOUT} s"
            clock.advance(CLOSE_RECHECK_SECONDS)
        settled()

    # 0. Primeiro contato (o chat de A1 e o de B ficam conhecidos da Bot API falsa).
    processed("a_start", rich.push_text(bot, TG_DISPATCH_A1, "/start", bot_command=True), "help")
    processed("b_start", rich.push_text(bot, TG_DISPATCH_B, "/start", bot_command=True), "help")
    settled()

    # 1. A questão canário: validada com a prévia redigida (controle positivo) e cancelada.
    canary = prepare("canary", "canary", started + timedelta(minutes=30))
    assert canary["state"] == "scheduled" and canary["confirmed"] is False
    preview = canary["preview"]
    issued = [v for v in engine_fake.issued_variants() if v.qid == "canary"]
    assert len(issued) == 1
    assert {f"disparo-nome:{DISPATCH_B.name}", f"disparo-ra:{DISPATCH_B.ra}", f"disparo-email:{DISPATCH_B.email}"} <= (
        _dispatch_labels(json.dumps(issued[0].ir, ensure_ascii=False))), "controle positivo: a identidade saiu do motor"
    assert (preview["title"], preview["fields"], preview["target_count"], preview["warnings"]) == (
        f"Questão de {redacted}", [{"type": "number", "label": f"{redacted}:", "suffix": "m/s", "hint": b_mark}], 2,
        [{"kind": "console_output", "message": redacted}])
    assert redacted in preview["statement"] and _dispatch_labels(preview["statement"]) == set()
    cancelled = cancel_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, dispatches["canary"])
    assert (cancelled["state"], cancelled["cancel_reason"], cancelled["cancelled_at"]) == (
        "cancelled", "operator", iso(started))

    # 2. A questão não suportada: rejeitada, com o motivo do motor redigido.
    rejected = prepare("rejected", "unsupported", started + timedelta(minutes=30))
    assert (rejected["state"], rejected["rejection"]) == ("rejected", {
        "reason": "question_unsupported", "message": DISPATCH_REJECTION_MESSAGE,
        "details": [DISPATCH_UNSUPPORTED.replace(DISPATCH_IDENTITY, redacted)]})

    # 3. O 1º disparo (texto com figura), para A e B.
    first_closes = started + DISPATCH_WINDOW
    d1 = prepare("d1", "with_media", first_closes)
    assert (d1["state"], d1["preview"]["warnings"], d1["preview"]["target_count"]) == ("scheduled", [], 2)
    confirmed = confirm_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, dispatches["d1"])
    assert (confirmed["confirmed"], confirmed["opens_at"], confirmed["closes_at"], confirmed["target_count"]) == (
        True, iso(started), iso(first_closes), 2)
    _wait_until(lambda: counts("d1")["delivered"] == 2, DISPATCH_TIMEOUT, "o 1º disparo não entregou às duas")
    settled()
    (a_prompt,) = prompts(a_chat)
    processed("a_answer", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_ANSWER, reply_to_message_id=a_prompt.message_id),
              "answer_recorded")
    _wait_until(lambda: counts("d1")["answered"] == 1, DISPATCH_TIMEOUT, "a resposta de A não foi corrigida")
    settled()
    (b_question,) = [m for m in rich.messages(bot, b_chat) if m.keyboard]
    (dont_know,) = [button for button in b_question.buttons() if button.label == deliverytexts.DONT_KNOW_BUTTON]
    tap = rich.tap(bot, TG_DISPATCH_B, b_question.message_id, dont_know.data)
    processed("b_dont_know", tap.update_id, "answer_dont_know")
    _wait_until(lambda: counts("d1")["dont_know"] == 1, DISPATCH_TIMEOUT, "o \"Não sei\" de B não foi gravado")
    settled()
    again = rich.tap_raw(bot, TG_DISPATCH_B, b_question.message_id, dont_know.data)
    processed("b_again", again.update_id, "answer_refused")
    for index, text in enumerate(DISPATCH_FREE_TEXTS):
        processed(f"a_free_{index}", rich.push_text(bot, TG_DISPATCH_A1, text), "free_message")
    settled()
    rich.wait_for(lambda: len(rich.answers(bot)) == 2, timeout=DISPATCH_TIMEOUT)
    assert [(answer.callback_id, answer.text) for answer in rich.answers(bot)][1] == (
        again.callback_id, deliverytexts.ALREADY_ANSWERED)
    assert {answer.callback_id for answer in rich.answers(bot)} == {tap.callback_id, again.callback_id}
    last_seq_before_close = max(call.seq for call in rich.calls())
    close(("d1",), first_closes)

    # 4. O 2º disparo (numérico, quebrado na correção), com o chat de B bloqueado na Bot API falsa.
    second = clock.now()
    second_closes = second + DISPATCH_WINDOW
    rich.block_chat(bot, TG_DISPATCH_B)
    d2 = prepare("d2", "broken_on_grade", second_closes)
    assert (d2["state"], d2["preview"]["warnings"], d2["preview"]["target_count"]) == ("scheduled", [], 2)
    confirmed = confirm_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, dispatches["d2"])
    assert (confirmed["opens_at"], confirmed["closes_at"], confirmed["target_count"]) == (
        iso(second), iso(second_closes), 2)
    _wait_until(lambda: (counts("d2")["delivered"], counts("d2")["not_delivered"]) == (1, 1), DISPATCH_TIMEOUT,
                "o 2º disparo não entregou a A nem recusou B")
    settled()
    (d2_question,) = refs("d2", "a", "question")
    d2_old_dont_know = dont_know_of(d2_question)  # tocado depois, com a entrega já `question_failed` (passo 5)
    engine_fake.fail("grade", error="invalid_request", times=1)
    first_prompt = prompts(a_chat)[-1]
    processed("a_d2_first", rich.push_text(bot, TG_DISPATCH_A1, "6.5", reply_to_message_id=first_prompt.message_id),
              "answer_recorded")
    _wait_until(lambda: sql.execute(
        "SELECT count(*) FROM submissions s JOIN deliveries d ON d.id = s.delivery_id JOIN dispatches x "
        "ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s AND s.outcome = 'internal_failure'",
        (dispatches["d2"],)).fetchone()[0] == 1, DISPATCH_TIMEOUT, "a falha interna da correção não foi gravada")
    settled()
    reopened_prompt = prompts(a_chat)[-1]
    assert reopened_prompt.message_id != first_prompt.message_id, "a entrega reaberta pede o campo de novo"
    processed("a_d2_second", rich.push_text(bot, TG_DISPATCH_A1, "7", reply_to_message_id=reopened_prompt.message_id),
              "answer_recorded")
    _wait_until(lambda: counts("d2")["question_failed"] == 1, DISPATCH_TIMEOUT, "a correção quebrada não falhou a questão")
    close(("d2",), second_closes)

    # 5. O 3º (número), o 4º (duas partes, figura ausente) e o 5º (múltipla degradada) disparos, abertos juntos; o
    # endereço de B continua bloqueado (a abertura o dá `not_delivered/endpoint_blocked`, sem mensagem), então só A
    # recebe.
    third = clock.now()
    third_closes = third + DISPATCH_WINDOW
    d3 = prepare("d3", "number", third_closes)
    assert (d3["state"], d3["preview"]["warnings"], d3["preview"]["target_count"]) == ("scheduled", [], 2)
    d4 = prepare("d4", "missing_media", third_closes)
    assert (d4["state"], [w["kind"] for w in d4["preview"]["warnings"]], d4["preview"]["target_count"]) == (
        "scheduled", ["missing_media"], 2)
    d5 = prepare("d5", "multiple_choice", third_closes)
    assert (d5["state"], [w["kind"] for w in d5["preview"]["warnings"]], d5["preview"]["target_count"]) == (
        "scheduled", ["too_many_options"], 2)
    for label in ("d3", "d4", "d5"):
        confirmed = confirm_dispatch(client, ADMIN_TOKEN, DISPATCH_OFFERING, dispatches[label])
        assert (confirmed["opens_at"], confirmed["closes_at"], confirmed["target_count"]) == (
            iso(third), iso(third_closes), 2)
    _wait_until(lambda: all((counts(label)["delivered"], counts(label)["not_delivered"]) == (1, 1)
                            for label in ("d3", "d4", "d5")), DISPATCH_TIMEOUT,
                "o 3º, o 4º ou o 5º disparo não entregou a A")
    settled()
    (d3_question,), (d4_question,) = refs("d3", "a", "question"), refs("d4", "a", "question")
    (d3_prompt,), (d4_prompt,) = refs("d3", "a", "field_prompt"), refs("d4", "a", "field_prompt")
    d3_old_dont_know, d4_old_dont_know = dont_know_of(d3_question), dont_know_of(d4_question)
    assert deliverytexts.MEDIA_UNAVAILABLE.format(n=1) in (rich.current(bot, a_chat, d4_question).plain_text or "")
    # O 5º: a múltipla degradada. A mensagem da questão lista as 13
    # alternativas e, depois delas, a dica `TYPED_MULTIPLE_HOWTO` (uma vez); o único botão é o "Não sei" (sem
    # botão de alternativa nem "Enviar"); a escolha é digitada, então sai o pedido do campo. A dica não sai na
    # questão do 3º (número) nem na do 4º (múltipla com botões).
    (d5_question,) = refs("d5", "a", "question")
    assert len(refs("d5", "a", "field_prompt")) == 1, "a múltipla degradada é digitada: sai o pedido do campo"
    d5_text = rich.current(bot, a_chat, d5_question).plain_text or ""
    howto = deliverytexts.TYPED_MULTIPLE_HOWTO
    assert [option.html for option in DISPATCH_MANY_OPTIONS if option.html not in d5_text] == [], d5_text
    assert d5_text.count(howto) == 1, d5_text
    assert d5_text.index(howto) > max(d5_text.index(option.html) for option in DISPATCH_MANY_OPTIONS), d5_text
    assert [b.label for b in rich.current(bot, a_chat, d5_question).buttons()] == [deliverytexts.DONT_KNOW_BUTTON]
    assert [label for label, message_id in (("d3", d3_question), ("d4", d4_question))
            if howto in (rich.current(bot, a_chat, message_id).plain_text or "")] == []

    # 5.1 Dois campos digitáveis abertos: o texto sem "responder" é mensagem livre ambígua ("use responder").
    processed("a_ambiguous", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_AMBIGUOUS), "free_message_ambiguous")
    settled()
    # 5.2 Uma figurinha em resposta ao pedido do 4º: "responda com texto", sem submissão.
    now_ts = int(clock.now().timestamp())
    sticker = {"message_id": 900_001, "from": TG_DISPATCH_A1.as_user(), "chat": TG_DISPATCH_A1.as_private_chat(),
               "date": now_ts, "sticker": {"file_id": "cq-figurinha-canario", "file_unique_id": "cq-figurinha-canario",
                                           "type": "regular", "width": 512, "height": 512, "is_animated": False,
                                           "is_video": False},
               "reply_to_message": {"message_id": d4_prompt, "from": {"id": bot, "is_bot": True,
                                                                       "first_name": "Bot de teste"},
                                    "chat": TG_DISPATCH_A1.as_private_chat(), "date": now_ts, "text": "Campo"}}
    processed("a_sticker", rich.push_update(bot, {"message": sticker}), "answer_not_text")
    settled()
    # 5.3 A responde ao 3º com a identidade de B: erro de formato (a explicação traduzida e o novo pedido).
    processed("a_d3_format", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_ANSWER, reply_to_message_id=d3_prompt),
              "answer_recorded")
    _wait_until(lambda: len(refs("d3", "a", "field_prompt")) == 2, DISPATCH_TIMEOUT,
                "o erro de formato do 3º disparo não pediu o campo de novo")
    settled()
    d3_reprompt = refs("d3", "a", "field_prompt")[1]
    # 5.4 O "Não sei" da versão antiga do 3º (antes do erro de formato): "esta parte já foi respondida".
    stale = rich.tap_raw(bot, TG_DISPATCH_A1, d3_question, d3_old_dont_know)
    processed("a_d3_stale", stale.update_id, "answer_stale")
    settled()
    # 5.5 A resposta certa, com a correção presa no motor; o "Não sei" durante ela: "sua resposta está sendo
    # corrigida"; a correção sai ✅.
    grades_before = len(engine_fake.calls("grade"))
    engine_fake.slow("grade", DISPATCH_GRADE_HOLD, times=1)
    processed("a_d3_answer", rich.push_text(bot, TG_DISPATCH_A1, "6.5", reply_to_message_id=d3_reprompt),
              "answer_recorded")
    engine_fake.wait_for(lambda: len(engine_fake.calls("grade")) > grades_before, timeout=DISPATCH_TIMEOUT)
    held = rich.tap(bot, TG_DISPATCH_A1, d3_question, dont_know_of(d3_question))
    processed("a_d3_grading", held.update_id, "answer_refused")
    assert engine_fake.calls("grade")[-1].outcome == "pending", (
        f"a recusa precisa sair com a correção ainda presa no motor ({DISPATCH_GRADE_HOLD} s reais)")
    _wait_until(lambda: counts("d3")["answered"] == 1, DISPATCH_TIMEOUT + DISPATCH_GRADE_HOLD,
                "a resposta certa do 3º disparo não foi corrigida")
    settled()
    # 5.6 A 1ª parte do 4º: o progresso e a escolha múltipla pelos botões (com o "Enviar"), sem mais resposta.
    processed("a_d4_part", rich.push_text(bot, TG_DISPATCH_A1, "6.5", reply_to_message_id=d4_prompt),
              "answer_part_recorded")
    settled()
    assert deliverytexts.SUBMIT_BUTTON in [b.label for b in rich.current(bot, a_chat, d4_question).buttons()]
    # 5.6b (nota parcial) A responde o 5º no prazo, digitando "abc" em resposta ao pedido
    # do campo (a múltipla degradada é digitada e vai literal ao motor): a nota parcial 1/6, com o veredito
    # "◐ parcialmente correta" e a nota "nota 16%" no feedback, sem novo pedido do campo.
    (d5_prompt,) = refs("d5", "a", "field_prompt")
    processed("a_d5_answer", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_D5_ANSWER, reply_to_message_id=d5_prompt),
              "answer_recorded")
    _wait_until(lambda: counts("d5")["answered"] == 1, DISPATCH_TIMEOUT,
                "a resposta de A ao 5º disparo não foi corrigida")
    settled()
    d5_feedback = rich.current(bot, a_chat, d5_question).plain_text or ""
    partial_verdict = getattr(deliverytexts, "VERDICT_PARTIAL", None)
    score_template = getattr(deliverytexts, "SCORE", None)
    assert partial_verdict and partial_verdict in d5_feedback, d5_feedback
    assert score_template and score_template.format(n=16) in d5_feedback, d5_feedback
    assert len(refs("d5", "a", "field_prompt")) == 1, "a resposta válida do 5º não pede o campo de novo"
    # 5.7 Os três fecham: a entrega do 4º, sem resposta, vira `no_answer`, sem o gabarito,
    # com o `LATE_HOWTO` e o "Não sei" na edição do fechamento;
    # o 3º e o 5º, respondidos, recebem a revelação.
    close(("d3", "d4", "d5"), third_closes)
    assert deliverytexts.DONT_KNOW_BUTTON in [b.label for b in rich.current(bot, a_chat, d4_question).buttons()], (
        "a `no_answer` mantém o \"Não sei\" depois do fechamento")
    # 5.8 (gabarito só para quem já respondeu) Depois do fechamento: o "Não sei" antigo do
    # 4º (de antes do fechamento: numa `no_answer`, vale de qualquer versão) é o "Não sei" atrasado (`dont_know`,
    # `late = true`, a revelação na mesma edição, a confirmação sem texto); o `/naosei` em resposta à questão dele
    # é recusado com `ALREADY_ANSWERED` (pela fila);
    # o "Não sei" antigo da entrega `question_failed` do 2º
    # ("não está disponível").
    after_close = clock.now()
    window = rich.tap_raw(bot, TG_DISPATCH_A1, d4_question, d4_old_dont_know)
    processed("a_d4_window", window.update_id, "answer_dont_know")
    _wait_until(lambda: counts("d4")["dont_know"] == 1, DISPATCH_TIMEOUT,
                "o \"Não sei\" atrasado do 4º não foi gravado")
    settled()
    processed("a_d4_naosei", rich.push_text(bot, TG_DISPATCH_A1, deliverytexts.DONT_KNOW_COMMAND, bot_command=True,
                                            reply_to_message_id=d4_question), "answer_refused")
    unavailable = rich.tap_raw(bot, TG_DISPATCH_A1, d2_question, d2_old_dont_know)
    processed("a_d2_unavailable", unavailable.update_id, "answer_refused")
    settled()
    rich.wait_for(lambda: len(rich.answers(bot)) == 6, timeout=DISPATCH_TIMEOUT)
    confirmations = {answer.callback_id: answer.text for answer in rich.answers(bot)}
    assert window.callback_id in confirmations, "o toque do \"Não sei\" atrasado foi confirmado"
    assert [confirmations.get(t.callback_id) for t in (stale, held, window, unavailable)] == [
        deliverytexts.PART_ALREADY_ANSWERED, deliverytexts.GRADING_IN_PROGRESS, None, deliverytexts.UNAVAILABLE]
    # 5.9 (mensagens livres opcionais) A coordenação desliga as mensagens livres (o contato continua);
    # sem campo digitável aberto (o 4º `dont_know`, o 5º `answered`), o texto de A sem "responder" é mensagem livre
    # não ambígua: a 1ª recebe a resposta com o contato (limitador do contato livre), a 2ª, no mesmo
    # intervalo, nada; nenhuma vira linha de `free_messages`, e o texto dos dois updates é mascarado.
    assert clock.now() == after_close, "o relógio fica parado entre os passos 5.8 e 5.9"
    changed = update_offering_settings(client, ADMIN_TOKEN, DISPATCH_OFFERING, free_messages=False)
    assert (changed["free_messages"], changed["contact_email_set"]) == (False, True), changed
    processed("a_free_off", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_FREE_OFF_TEXTS[0]),
              "free_message_masked_replied")
    settled()
    processed("a_free_off_again", rich.push_text(bot, TG_DISPATCH_A1, DISPATCH_FREE_OFF_TEXTS[1]),
              "free_message_masked")
    settled()
    messages = list_free_messages(client, ADMIN_TOKEN, DISPATCH_OFFERING)
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker da fase de disparos não parou em 10 s"
    assert worker.error is None and worker.returned

    # --- Os disparos, as entregas e as submissões ---
    final = {label: body(label) for label in dispatches}
    assert {label: final[label]["state"] for label in final} == {
        "canary": "cancelled", "rejected": "rejected", "d1": "closed", "d2": "closed", "d3": "closed", "d4": "closed",
        "d5": "closed"}
    assert final["d1"]["deliveries"] == {**DISPATCH_ZERO_COUNTS, "answered": 1, "dont_know": 1, "grading_pending": 0}
    assert final["d2"]["deliveries"] == {**DISPATCH_ZERO_COUNTS, "not_delivered": 1, "question_failed": 1,
                                         "grading_pending": 0}
    assert final["d3"]["deliveries"] == {**DISPATCH_ZERO_COUNTS, "not_delivered": 1, "answered": 1,
                                         "grading_pending": 0}
    # O 4º termina `dont_know` (o "Não sei" atrasado do passo 5.8) e o 5º,
    # `answered` (a resposta do passo 5.6b).
    assert final["d4"]["deliveries"] == {**DISPATCH_ZERO_COUNTS, "not_delivered": 1, "dont_know": 1,
                                         "grading_pending": 0}
    assert final["d5"]["deliveries"] == {**DISPATCH_ZERO_COUNTS, "not_delivered": 1, "answered": 1,
                                         "grading_pending": 0}
    rows = {label: sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s", (dispatch_id,)).fetchone()[0]
            for label, dispatch_id in dispatches.items()}
    by_pseudo = {people[label]["pseudo_id"]: label for label in people}
    label_of = {dispatch_id: label for label, dispatch_id in dispatches.items()}
    delivery_rows = sql.execute(
        "SELECT x.dispatch_uuid::text, p.pseudo_id::text, d.delivery_uuid::text, d.state, d.undelivered_reason, "
        "d.failure FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id JOIN enrollments e "
        "ON e.id = d.enrollment_id JOIN people p ON p.id = e.person_id WHERE x.offering_id = %s ORDER BY d.id",
        (offering_row,)).fetchall()
    assert sorted((label_of[r[0]], by_pseudo[r[1]], r[3], r[4], r[5]) for r in delivery_rows) == [
        ("d1", "a", "answered", None, None), ("d1", "b", "dont_know", None, None),
        ("d2", "a", "question_failed", None, "grading_broken"), ("d2", "b", "not_delivered", "endpoint_blocked", None),
        ("d3", "a", "answered", None, None), ("d3", "b", "not_delivered", "endpoint_blocked", None),
        ("d4", "a", "dont_know", None, None), ("d4", "b", "not_delivered", "endpoint_blocked", None),
        ("d5", "a", "answered", None, None), ("d5", "b", "not_delivered", "endpoint_blocked", None)]
    deliveries = {(label_of[r[0]], by_pseudo[r[1]]): r[2] for r in delivery_rows}
    submission_rows = sql.execute(
        "SELECT x.dispatch_uuid::text, p.pseudo_id::text, s.submission_uuid::text, s.outcome, s.valid, s.refusal, "
        "s.failure_code FROM submissions s JOIN deliveries d ON d.id = s.delivery_id JOIN dispatches x "
        "ON x.id = d.dispatch_id JOIN enrollments e ON e.id = d.enrollment_id JOIN people p ON p.id = e.person_id "
        "WHERE x.offering_id = %s ORDER BY s.id", (offering_row,)).fetchall()
    assert [(label_of[r[0]], by_pseudo[r[1]], r[3], r[4], r[5], r[6]) for r in submission_rows] == [
        ("d1", "a", "graded", True, None, None), ("d1", "b", "dont_know", None, None, None),
        ("d1", "b", "refused", None, "already_answered", None),
        ("d2", "a", "internal_failure", None, None, "invalid_request"),
        ("d2", "a", "broken", None, None, "question_code_error"),
        ("d3", "a", "graded", False, None, None),  # o erro de formato (a identidade de B no campo numérico)
        ("d3", "a", "graded", True, None, None), ("d3", "a", "refused", None, "grading_pending", None),
        # A resposta de A ao 5º (passo 5.6b), antes do fechamento; depois
        # dele, o "Não sei" atrasado do 4º (o toque antigo)
        # e a recusa do `/naosei` seguinte.
        ("d5", "a", "graded", True, None, None),
        ("d4", "a", "dont_know", None, None, None), ("d4", "a", "refused", None, "already_answered", None),
        ("d2", "a", "refused", None, "unavailable", None)]

    def submission_mark(outcome: str, valid: bool | None, refusal: str | None) -> str:
        if outcome == "graded":
            return "graded" if valid else "format_error"
        return f"refused:{refusal}" if outcome == "refused" else outcome

    submissions = {(label_of[r[0]], by_pseudo[r[1]], submission_mark(r[3], r[4], r[5])): r[2] for r in submission_rows}

    # --- Os updates gravados (texto literal) e o que saiu para o Telegram ---
    assert {label: sql.execute("SELECT kind, chat_id, text, state, outcome FROM telegram_updates WHERE update_id = %s",
                               (update_id,)).fetchall() for label, update_id in updates.items()} == {
        "a_start": [("message", a_chat, "/start", "processed", "help")],
        "b_start": [("message", b_chat, "/start", "processed", "help")],
        "a_answer": [("message", a_chat, DISPATCH_ANSWER, "processed", "answer_recorded")],
        "b_dont_know": [("callback_query", b_chat, None, "processed", "answer_dont_know")],
        "b_again": [("callback_query", b_chat, None, "processed", "answer_refused")],
        "a_free_0": [("message", a_chat, DISPATCH_FREE_TEXTS[0], "processed", "free_message")],
        "a_free_1": [("message", a_chat, DISPATCH_FREE_TEXTS[1], "processed", "free_message")],
        "a_d2_first": [("message", a_chat, "6.5", "processed", "answer_recorded")],
        "a_d2_second": [("message", a_chat, "7", "processed", "answer_recorded")],
        "a_ambiguous": [("message", a_chat, DISPATCH_AMBIGUOUS, "processed", "free_message_ambiguous")],
        "a_sticker": [("message", a_chat, None, "processed", "answer_not_text")],
        "a_d3_format": [("message", a_chat, DISPATCH_ANSWER, "processed", "answer_recorded")],
        "a_d3_stale": [("callback_query", a_chat, None, "processed", "answer_stale")],
        "a_d3_answer": [("message", a_chat, "6.5", "processed", "answer_recorded")],
        "a_d3_grading": [("callback_query", a_chat, None, "processed", "answer_refused")],
        "a_d4_part": [("message", a_chat, "6.5", "processed", "answer_part_recorded")],
        # A resposta ao 5º; o toque antigo do 4º é o "Não sei" atrasado
        # (não um `answer_refused`); o `/naosei` seguinte é `answer_refused`,
        # por `already_answered`; as duas mensagens do modo desligado, com o texto mascarado.
        "a_d5_answer": [("message", a_chat, DISPATCH_D5_ANSWER, "processed", "answer_recorded")],
        "a_d4_window": [("callback_query", a_chat, None, "processed", "answer_dont_know")],
        "a_d4_naosei": [("message", a_chat, deliverytexts.DONT_KNOW_COMMAND, "processed", "answer_refused")],
        "a_d2_unavailable": [("callback_query", a_chat, None, "processed", "answer_refused")],
        "a_free_off": [("message", a_chat, "[mensagem livre]", "processed", "free_message_masked_replied")],
        "a_free_off_again": [("message", a_chat, "[mensagem livre]", "processed", "free_message_masked")],
    }
    help_text = _compose(HELP, DISPATCH_COURSE, DISPATCH_TERM)
    # As respostas com texto fixo pela fila, ao chat do update: a da mensagem livre, a do "use responder", a da
    # figurinha e a do `/naosei` depois do fechamento (as recusas do toque saem na confirmação, fora da fila).
    # A do `/naosei` é `ALREADY_ANSWERED`, e não `WINDOW_CLOSED` (o
    # "Não sei" atrasado já encerrou a entrega); no fim, a resposta do modo desligado com o contato (só
    # à 1ª mensagem do passo 5.9).
    # Conferido parcela a parcela:
    # os textos fixos sem texto do aluno saem pelo caminho comum, em texto puro (`sendMessage`), na ordem dos passos
    # (cada um separado por `settled()`): os dois `HELP` do passo 0 (ao chat do update); a resposta fixa à 1ª
    # mensagem livre (passo 3); o `ANSWER_KEPT` da falha interna do 2º disparo (passo 4; a regra, para o
    # `EngineRequestRejected`: "`ANSWER_KEPT` uma vez por entrega", um aviso, como o `GRADING_DELAYED`, e não parte
    # da edição da entrega; e a regra de que as mensagens assíncronas
    # (feedback, erro, avisos) saem ao endereço vigente da pessoa, `Outbox.enqueue(recipient, bot_id)`); o
    # `USE_REPLY` (5.1); o `REPLY_WITH_TEXT` (5.2); a explicação do erro de formato do 3º (5.3: o `FORMAT_ERROR`
    # com a tradução de `NUMBER_INVALID_TEXT` do motor falso; uma mensagem a cada erro de formato,
    # não um trecho da edição da questão: o congelado `test_answers_typed.py::test_comma_with_point_or_two_commas…`
    # exige, no Telegram, duas aparições novas da mesma explicação em dois erros seguidos, `at_least=2`, o que uma
    # edição cujo "antes" já a traz não dá; e a regra de que o texto ao aluno começa com `FORMAT_ERROR`
    # e é seguido do novo pedido do campo); o `ALREADY_ANSWERED` do `/naosei` (5.8); e a resposta com o contato
    # (5.9).
    contact_reply = getattr(deliverytexts, "answers_only_reply", None)
    assert callable(contact_reply), "deliverytexts.answers_only_reply ausente"
    format_error_text = _phase_text("FORMAT_ERROR") + (
        "Não consegui ler a resposta como número. Não use variáveis nem outros símbolos.")
    assert [(m.chat_id, m.text) for m in rich.sent_messages(bot)] == [
        (a_chat, help_text), (b_chat, help_text), (a_chat, deliverytexts.FREE_MESSAGE_REPLY),
        (a_chat, deliverytexts.ANSWER_KEPT),
        (a_chat, deliverytexts.USE_REPLY), (a_chat, deliverytexts.REPLY_WITH_TEXT), (a_chat, format_error_text),
        (a_chat, deliverytexts.ALREADY_ANSWERED), (a_chat, contact_reply(PROFESSOR_EMAIL))]
    # A fila do bot: as mensagens de entrega sem texto, pelo papel; as
    # respostas comuns (como o `HELP`) e a resposta fixa com texto. As edições (texto ou teclado) contam juntas.
    queue = Counter(
        (role, None if operation is None else ("edit" if operation.startswith("edit") else operation), state,
         has_text, has_person)
        for role, operation, state, has_text, has_person in sql.execute(
            "SELECT o.delivery_role, o.operation, o.state, o.text IS NOT NULL, o.recipient_person_id IS NOT NULL "
            "FROM outbox_messages o WHERE o.bot_id = %s", (bot_row,)).fetchall())
    # Contagens: o passo 5 tem o
    # ALREADY_ANSWERED do `/naosei` (não o WINDOW_CLOSED) e a edição do "Não sei" atrasado do 4º
    # e o feedback ◐ do 5º; o passo 5.9 soma a resposta com o contato.
    # Conferido parcela a parcela, passo a passo pelo roteiro acima e pelas
    # regras da fila de saída. Derivação por passo (nenhuma edição se funde
    # com outra: cada uma sai antes do passo seguinte, que começa depois de `settled()`; as recusas de toque saem na
    # confirmação, `answerCallbackQuery`, fora da fila):
    # - passo 0: o `HELP` a A1 e a B (o `/start` sem código: ao chat do update, sem pessoa) → 2 textos;
    # - passo 3 (1º disparo, A e B; `with_media`: uma mídia `ok` e um campo digitável): por entrega, `media`,
    #   `question` e `field_prompt` → 6 envios; a correção de A (❌) → 1 edição; o "Não
    #   sei" de B → 1 edição; o toque repetido de B → só a confirmação; a 1ª mensagem livre → o
    #   `FREE_MESSAGE_REPLY` (a 2ª cai no limitador) → 1 texto; o fechamento → as revelações de A e de B
    #   → 2 edições;
    # - passo 4 (2º disparo, `broken_on_grade`, sem mídia): na abertura o endereço de B ainda está `active` (o
    #   bloqueio é só da Bot API falsa), então as duas entregas preparam `question` e `field_prompt`; A: os dois
    #   enviados; B: a `question` recebe o 403 (`failed`) e o `field_prompt` dela é cancelado com a entrega
    #   `not_delivered`; a 1ª resposta de A → `EngineRequestRejected`: a edição da
    #   reabertura, o novo `field_prompt` e o `ANSWER_KEPT` (1 texto, a 1ª falha interna da entrega); a 2ª →
    #   `Broken`: o `QUESTION_FAILED` numa edição; o fechamento não edita a `question_failed` nem a `not_delivered`;
    # - passo 5 (o 3º, o 4º e o 5º; B `not_delivered` na abertura, sem mensagem): A recebe, por disparo, a
    #   `question` e o `field_prompt` do 1º campo (o 4º sem `media`: a figura é `missing`; a múltipla degradada
    #   do 5º é digitável) → 6 envios; 5.1 o `USE_REPLY` (1 texto); 5.2 o `REPLY_WITH_TEXT` (1 texto); 5.3 o erro
    #   de formato do 3º: o `FORMAT_ERROR` (1 texto), a edição da reabertura e o novo `field_prompt`; 5.4 só a
    #   confirmação; 5.5 o feedback ✅ do 3º (1 edição; o toque durante a correção, só a confirmação); 5.6 o
    #   progresso do 4º (1 edição; o 2º campo é escolha com botões, sem pedido); 5.6b o feedback ◐ do 5º (1
    #   edição); 5.7 a revelação do 3º, a edição de fechamento da `no_answer` do 4º (sem o gabarito) e
    #   a revelação do 5º (3 edições); 5.8 a edição do "Não sei" atrasado do 4º, com a revelação (1 edição),
    #   o `ALREADY_ANSWERED` do `/naosei` pela fila (1 texto) e o toque no 2º, só a confirmação;
    # - passo 5.9: a 1ª mensagem → a resposta com o contato (1 texto); a 2ª → nada (limitador do
    #   contato).
    # Os textos fixos a A saem pelo endereço vigente dela, com a pessoa (a regra:
    # a resposta fixa sai pela fila com `priority = 1`, pelo endereço vigente, `_reply`; a forma (b):
    # pessoa, bot e endereço; os avisos da correção pelo mesmo endereço), inclusive o
    # `ANSWER_KEPT` e o
    # `FORMAT_ERROR`.
    assert queue == Counter({
        # os dois `HELP` do passo 0, ao chat do update (sem pessoa)
        (None, None, "sent", True, False): 2,
        # ao endereço de A (com pessoa): no passo 3, o `FREE_MESSAGE_REPLY` (prioridade 1); no 4, o `ANSWER_KEPT`
        # (2); no passo 5, o `USE_REPLY` (prioridade 1), o `REPLY_WITH_TEXT`, o `FORMAT_ERROR` e o
        # `ALREADY_ANSWERED` do `/naosei` (e não o WINDOW_CLOSED) (4); no 5.9, a resposta do modo
        # desligado com o contato (prioridade 1) (1)
        (None, None, "sent", True, True): 2 + 4 + 1,
        ("media", "send", "sent", False, True): 2,  # a figura do 1º disparo, a A e a B (a do 4º é ausente)
        # a questão do 1º disparo (A, B) e a do 2º (A); no passo 5, a do 3º e a do 4º (A); e a do 5º (A;
        # múltipla degradada)
        ("question", "send", "sent", False, True): 3 + 2 + 1,
        # o pedido do 1º (A, B) e o do 2º (A, e de novo); no passo 5, o do 3º (A, e de novo depois do erro de
        # formato) e o da 1ª parte do 4º (A; a 2ª parte é escolha pelos botões, sem pedido); e o do 5º (A: a
        # múltipla degradada é campo digitável)
        ("field_prompt", "send", "sent", False, True): 4 + 3 + 1,
        # 1º disparo: o feedback de A, o "Não sei" de B e as duas revelações; 2º: a reabertura e a falha de A
        # (6); 3º: o erro de formato, o feedback ✅ e a revelação; 4º: o progresso, a edição de fechamento da
        # `no_answer` (sem o gabarito) e a do "Não sei" atrasado, com a revelação (6); 5º: o
        # feedback ◐ e a revelação (2).
        # Conferido parcela a parcela,
        # passo a passo pelo roteiro.
        ("question", "edit", "sent", False, True): 6 + 6 + 2,
        ("question", "send", "failed", False, True): 1,  # a questão do 2º disparo a B (403)
        ("field_prompt", "send", "cancelled", False, True): 1,  # o pedido do 2º disparo a B, cancelado
    })
    # As linhas e a consulta continuam só com as 3 mensagens do modo ligado (o passo 5.9 não grava
    # nada: asserção negativa do modo desligado).
    free_rows = sql.execute("SELECT id, text, replied, ambiguous, received_at FROM free_messages "
                            "WHERE offering_id = %s ORDER BY id", (offering_row,)).fetchall()
    redacted_free = [DISPATCH_FREE_TEXTS[0].replace(DISPATCH_B.name, b_mark).replace(DISPATCH_B.ra, b_mark)
                     .replace(DISPATCH_B.email, b_mark), DISPATCH_FREE_TEXTS[1]]
    assert [row[1:] for row in free_rows] == [(redacted_free[0], True, False, started),
                                             (redacted_free[1], False, False, started),
                                             (DISPATCH_AMBIGUOUS, True, True, third)]
    assert messages == {"offering": ref, "items": [
        {"pseudo_id": people["a"]["pseudo_id"], "alias": people["a"]["alias"], "text": text, "created_at": iso(at)}
        for text, at in ((redacted_free[0], started), (redacted_free[1], started), (DISPATCH_AMBIGUOUS, third))],
        "next_cursor": free_rows[-1][0]}
    return _DispatchPhase(offering, offering_row, bot, username, DISPATCH_COURSE, DISPATCH_TERM, started, second,
                          third, people, final, rows, deliveries, submissions, updates, preview, messages,
                          last_seq_before_close, after_close=after_close)


def _check_dispatch_events(api: _Api, run: _DispatchPhase, sql: psycopg.Connection) -> None:
    """Os eventos exatos da fase de disparos, só com pseudônimos
    e identificadores, sem identidade, texto digitado, URL do repositório nem detalhe técnico.

    Por disparo, os eventos do disparo saem na ordem dos passos; entre as duas entregas de um disparo não há
    ordem (as tarefas `delivery.prepare` e os envios correm em paralelo com o ritmo), e elas são
    comparadas como conjunto. Os `bot.*` (posse e perfil) ficam de fora, como no vínculo do aluno."""
    events = sorted(api.events, key=lambda e: e["seq"])
    oid = run.offering["offering_id"]
    mine = [e for e in events if e["data"].get("offering_id") == oid and not e["type"].startswith("bot.")]
    t0, t1, t2 = iso(run.started), iso(run.second), iso(run.third)
    closed = {label: run.dispatches[label]["closed_at"] for label in ("d1", "d2", "d3", "d4", "d5")}
    pa, pb = run.pseudo("a"), run.pseudo("b")

    def dispatch_id(label: str) -> str:
        return run.dispatches[label]["dispatch_id"]

    def ref_id(label: str) -> str:
        return run.dispatches[label]["question"]["question_ref_id"]

    def base(label: str) -> dict:
        return {"offering_id": oid, "dispatch_id": dispatch_id(label)}

    def delivery(label: str, person: str, **data: Any) -> dict:
        return {**base(label), "delivery_id": run.deliveries[(label, person)], "person": run.pseudo(person), **data}

    def prepared(label: str, at: str) -> tuple:
        return ("dispatch.prepared", at, "bootstrap",
                {**base(label), "question_ref_id": ref_id(label), "origin": DISPATCH_ORIGIN})

    def seq_of(label: str) -> list[tuple]:
        # `.get`, porque o `offering.settings_changed`, em
        # `DISPATCH_EVENT_TYPES`, não tem `dispatch_id` (conferido à parte, abaixo).
        return [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in mine
                if e["type"] in DISPATCH_EVENT_TYPES and e["data"].get("dispatch_id") == dispatch_id(label)]

    # Mensagens livres opcionais: o `PATCH` do passo 5.9 (desliga; o contato continua), com o token de quem pediu.
    t_after = iso(run.after_close)
    assert [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in mine
            if e["type"] == "offering.settings_changed"] == [
        ("offering.settings_changed", t_after, "bootstrap",
         {"offering_id": oid, "free_messages": False, "contact_email_set": True}),
    ]

    # Fora dos 14 tipos do disparo (e dos `bot.*`): o oferecimento, a lista e o bloqueio do endereço de B pelo 403
    # do 2º disparo. O cadastro com as mensagens livres e o contato gera só o
    # `offering.created`.
    # Relatórios: fora também a exceção nomeada dos eventos dos relatórios (`REPORT_EVENT_TYPES`; os
    # `bot.*` já estão fora de `mine`), com o subconjunto exato conferido logo depois de `others`.
    others = [(e["type"], e["occurred_at"], e["actor_token_id"], e["data"]) for e in mine
              if e["type"] not in DISPATCH_EVENT_TYPES and e["type"] not in REPORT_EVENT_TYPES]
    assert others == [
        ("offering.created", t0, "bootstrap", {"offering_id": oid, "code": DISPATCH_OFFERING}),
        ("offering.state_changed", t0, "bootstrap", {"offering_id": oid, "from_state": "draft", "to_state": "active"}),
        ("student.list_imported", t0, "bootstrap", {"offering_id": oid, "received": 2, "people_created": 2,
                                                    "people_reused": 0, "enrollments_created": 2,
                                                    "already_enrolled": 0}),
        ("endpoint.blocked", t1, None, {"offering_id": oid, "person": pb, "channel": "telegram"}),
    ], others
    # Relatórios: o subconjunto excluído de `others`, exato (o oferecimento
    # da fase não tem equipe, então todo aviso termina sem destinatário): um `report.frozen`
    # (versão 1, `closing`) por disparo fechado (do 1º ao 5º); os `notice.recorded` da prévia de cada disparo
    # validado (a canário e do 1º ao 5º: `no_recipient`, ou `gave_up` se a tarefa da prévia só correu depois do
    # cancelamento ou da abertura), da rejeição, do "aberto" (do 1º ao 5º), da falha de
    # questão do 2º (`grading_broken`) e do resumo de fechamento (do 1º ao 5º, versão 1); nenhum aviso de
    # cancelamento (o da canário é da coordenação, fora dos motivos automáticos). 5 + 6 + 1 + 5 + 1 + 5 = 23. Os
    # horários não são fixados (correm com a varredura). No esqueleto, não há evento dos relatórios.
    def report_notice(kind: str, label: str, state: str = "no_recipient", version: int | None = None) -> tuple:
        return ("notice.recorded", None, {
            "offering_id": oid, "notice_type": kind, "dispatch_id": dispatch_id(label), "report_version": version,
            "episode_seq": None, "batch_id": None, "window_started_at": None, "local_date": None, "state": state,
            "recipient_count": 0})

    opened_labels = ("d1", "d2", "d3", "d4", "d5")
    r6_events = [(e["type"], e["actor_token_id"], e["data"]) for e in mine if e["type"] in REPORT_EVENT_TYPES]
    preview_states = {data["dispatch_id"]: data["state"] for kind, _, data in r6_events
                      if kind == "notice.recorded" and data["notice_type"] == "dispatch_preview"}
    assert sorted(preview_states) == sorted(dispatch_id(label) for label in ("canary", *opened_labels)), r6_events
    assert set(preview_states.values()) <= {"no_recipient", "gave_up"}, preview_states
    expected_r6 = [
        *(("report.frozen", None, {**base(label), "version": 1, "trigger": "closing"}) for label in opened_labels),
        *(report_notice("dispatch_preview", label, preview_states[dispatch_id(label)])
          for label in ("canary", *opened_labels)),
        report_notice("dispatch_rejected", "rejected"),
        *(report_notice("dispatch_opened", label) for label in opened_labels),
        report_notice("question_failed", "d2"),
        *(report_notice("dispatch_closed", label, version=1) for label in opened_labels),
    ]
    assert len(expected_r6) == 23
    assert sorted(json.dumps(item, sort_keys=True) for item in r6_events) == sorted(
        json.dumps(item, sort_keys=True) for item in expected_r6), r6_events

    assert seq_of("canary") == [
        prepared("canary", t0),
        ("dispatch.validated", t0, None, {**base("canary"), "target_count": 2, "warnings": ["console_output"]}),
        ("dispatch.cancelled", t0, "bootstrap", {**base("canary"), "cancel_reason": "operator"}),
    ]
    assert seq_of("rejected") == [
        prepared("rejected", t0),
        ("dispatch.rejected", t0, None, {**base("rejected"), "rejection_reason": "question_unsupported"}),
    ]
    d1 = seq_of("d1")
    assert d1[:4] == [
        prepared("d1", t0),
        ("dispatch.validated", t0, None, {**base("d1"), "target_count": 2, "warnings": []}),
        ("dispatch.confirmed", t0, "bootstrap", {**base("d1"), "target_count": 2, "opens_at": t0,
                                                 "closes_at": iso(run.started + DISPATCH_WINDOW)}),
        ("dispatch.opened", t0, None, {**base("d1"), "target_count": 2}),
    ], d1
    assert sorted(d1[4:6], key=repr) == sorted([
        ("delivery.delivered", t0, None, delivery("d1", "a")),
        ("delivery.delivered", t0, None, delivery("d1", "b")),
    ], key=repr), d1
    graded = {**base("d1"), "delivery_id": run.deliveries[("d1", "a")],
              "submission_id": run.submissions[("d1", "a", "graded")], "question_ref_id": ref_id("d1"), "person": pa,
              "valid": True, "late": False, "normalized": False, "score": 0,
              "parts": [{"index": 0, "score": 0, "choices": [], "ratio": None, "residual": None}]}
    assert d1[6:] == [
        ("submission.graded", t0, None, graded),
        ("delivery.dont_know", t0, None, {**delivery("d1", "b"),
                                          "submission_id": run.submissions[("d1", "b", "dont_know")]}),
        ("dispatch.closed", closed["d1"], None, {**base("d1"), "trigger": "window", "counts": {
            **DISPATCH_ZERO_COUNTS, "answered": 1, "dont_know": 1}}),
    ], d1
    d2 = seq_of("d2")
    assert d2[:4] == [
        prepared("d2", t1),
        ("dispatch.validated", t1, None, {**base("d2"), "target_count": 2, "warnings": []}),
        ("dispatch.confirmed", t1, "bootstrap", {**base("d2"), "target_count": 2, "opens_at": t1,
                                                 "closes_at": iso(run.second + DISPATCH_WINDOW)}),
        ("dispatch.opened", t1, None, {**base("d2"), "target_count": 2}),
    ], d2
    assert sorted(d2[4:6], key=repr) == sorted([
        ("delivery.delivered", t1, None, delivery("d2", "a")),
        ("delivery.not_delivered", t1, None, delivery("d2", "b", undelivered_reason="endpoint_blocked")),
    ], key=repr), d2
    assert d2[6:] == [
        ("submission.internal_failure", t1, None, {
            **delivery("d2", "a"), "submission_id": run.submissions[("d2", "a", "internal_failure")],
            "engine_error": "invalid_request"}),
        ("delivery.question_failed", t1, None, delivery("d2", "a", failure="grading_broken")),
        ("dispatch.closed", closed["d2"], None, {**base("d2"), "trigger": "window", "counts": {
            **DISPATCH_ZERO_COUNTS, "not_delivered": 1, "question_failed": 1}}),
    ], d2
    # O 3º, o 4º e o 5º disparos (passo 5): o endereço de B, bloqueado desde o 2º, dá
    # `not_delivered/endpoint_blocked` na própria abertura; entre a abertura e as duas entregas não há ordem fixada
    # (conjunto). Não geram evento: o erro de formato, as recusas, o "esta parte já foi respondida", a figurinha, a
    # parte do 4º e a mensagem livre. O 5º (múltipla degradada) valida com o aviso
    # `too_many_options`.
    for label, kinds in (("d3", []), ("d4", ["missing_media"]), ("d5", ["too_many_options"])):
        seq = seq_of(label)
        assert seq[:3] == [
            prepared(label, t2),
            ("dispatch.validated", t2, None, {**base(label), "target_count": 2, "warnings": kinds}),
            ("dispatch.confirmed", t2, "bootstrap", {**base(label), "target_count": 2, "opens_at": t2,
                                                     "closes_at": iso(run.third + DISPATCH_WINDOW)}),
        ], seq
        assert sorted(seq[3:6], key=repr) == sorted([
            ("dispatch.opened", t2, None, {**base(label), "target_count": 2}),
            ("delivery.delivered", t2, None, delivery(label, "a")),
            ("delivery.not_delivered", t2, None, delivery(label, "b", undelivered_reason="endpoint_blocked")),
        ], key=repr), seq
    d3 = seq_of("d3")
    assert [(t, at, actor) for t, at, actor, _ in d3[6:]] == [
        ("submission.graded", t2, None), ("dispatch.closed", closed["d3"], None)], d3
    graded_d3 = d3[6][3]
    assert {key: value for key, value in graded_d3.items() if key != "parts"} == {
        **base("d3"), "delivery_id": run.deliveries[("d3", "a")],
        "submission_id": run.submissions[("d3", "a", "graded")],
        "question_ref_id": ref_id("d3"), "person": pa, "valid": True, "late": False, "normalized": False, "score": 1}
    # `ratio` e `residual` do campo numérico vêm de `submissions.part_derived` (o valor não é fixado pela
    # documentação); o resto da parte é exato.
    assert [{key: part[key] for key in ("index", "score", "choices")} for part in graded_d3["parts"]] == [
        {"index": 0, "score": 1, "choices": []}]
    assert [set(part) for part in graded_d3["parts"]] == [{"index", "score", "choices", "ratio", "residual"}]
    assert d3[7][3] == {**base("d3"), "trigger": "window", "counts": {
        **DISPATCH_ZERO_COUNTS, "not_delivered": 1, "answered": 1}}, d3
    # O 4º fecha com a `no_answer` e depois recebe o "Não sei" atrasado
    # (`delivery.dont_know` depois do `dispatch.closed`, sem `late`); o 5º tem a correção da resposta de A
    # (nota parcial 1/6, no prazo) antes do `dispatch.closed`, com a entrega `answered`.
    # Conferido evento a evento.
    # No 4º,
    # depois dos 6 primeiros (preparo, validação, confirmação, abertura e as duas entregas), a parte de A não gera
    # evento (nem a parte nem a edição do progresso); a transição leva A a `no_answer` e B fica
    # `not_delivered` (`counts`); o "Não sei" atrasado do 5.8 sai no horário do relógio parado (`after_close`),
    # depois do `dispatch.closed`. No 5º, a correção válida de "abc" sai no horário do passo 5
    # (`third`, o relógio só anda no `close`) e antes do `dispatch.closed`, com `late = false`.
    d4 = seq_of("d4")
    assert d4[6:] == [
        ("dispatch.closed", closed["d4"], None, {**base("d4"), "trigger": "window", "counts": {
            **DISPATCH_ZERO_COUNTS, "not_delivered": 1, "no_answer": 1}}),
        ("delivery.dont_know", t_after, None, {**delivery("d4", "a"),
                                               "submission_id": run.submissions[("d4", "a", "dont_know")]}),
    ], d4
    d5 = seq_of("d5")
    assert [(t, at, actor) for t, at, actor, _ in d5[6:]] == [
        ("submission.graded", t2, None), ("dispatch.closed", closed["d5"], None)], d5
    graded_d5 = d5[6][3]
    assert {key: value for key, value in graded_d5.items() if key not in ("score", "parts")} == {
        **base("d5"), "delivery_id": run.deliveries[("d5", "a")],
        "submission_id": run.submissions[("d5", "a", "graded")],
        "question_ref_id": ref_id("d5"), "person": pa, "valid": True, "late": False, "normalized": False}, d5
    assert abs(graded_d5["score"] - DISPATCH_D5_SCORE) < 1e-9, graded_d5
    # A parte única (a múltipla): a nota parcial e as 3 alternativas escolhidas pelo resumo do conteúdo;
    # `ratio` e `residual` ficam como os do 3º, sem valor fixado.
    assert [set(part) for part in graded_d5["parts"]] == [{"index", "score", "choices", "ratio", "residual"}]
    (part_d5,) = graded_d5["parts"]
    assert part_d5["index"] == 0 and abs(part_d5["score"] - DISPATCH_D5_SCORE) < 1e-9, part_d5
    assert len(part_d5["choices"]) == 3 and all(re.fullmatch(r"sha256:[0-9a-f]{64}", choice)
                                                for choice in part_d5["choices"]), part_d5
    assert d5[7][3] == {**base("d5"), "trigger": "window", "counts": {
        **DISPATCH_ZERO_COUNTS, "not_delivered": 1, "answered": 1}}, d5
    assert {e["type"] for e in mine if e["type"] in DISPATCH_EVENT_TYPES} == set(DISPATCH_EVENT_TYPES), (
        "a fase de disparos não emitiu os 14 tipos do disparo")
    # As rodadas `dispatch.close`: ao menos uma por disparo aberto, todas `done`, um único
    # `dispatch.closed` por disparo.
    opened = ("d1", "d2", "d3", "d4", "d5")
    close_rounds = Counter(payload["dispatch_row_id"] for state, payload in sql.execute(
        "SELECT state, payload FROM tasks WHERE task_type = 'dispatch.close'").fetchall() if state == "done")
    # Relatórios: as rodadas dos disparos da fase, separadas por id das dos disparos que a
    # semente de relatórios e a fase de relatórios fecham, conferidas exatamente: os 4 disparos fechados dos dois
    # oferecimentos (1 da semente, `report_targets` padrão, com uma única rodada `r1` cada; os 3 imediatos da
    # fase de relatórios, ao menos uma rodada cada). No esqueleto, elas não chegam aqui.
    r5_rows = {run.dispatch_rows[label] for label in opened}
    r6_rows = {row[0]: row[1] for row in sql.execute(
        "SELECT d.id, o.code FROM dispatches d JOIN offerings o ON o.id = d.offering_id "
        "WHERE o.code = ANY(%s) AND d.state = 'closed'", ([REPORT_OFFERING, api.state.report_offering["code"]],)
    ).fetchall()}
    assert sorted(Counter(r6_rows.values()).items()) == sorted(
        {REPORT_OFFERING: 3, api.state.report_offering["code"]: 1}.items()), r6_rows
    assert {row for row in close_rounds if row not in r5_rows} == set(r6_rows), close_rounds
    assert {row: close_rounds[row] for row, code in r6_rows.items() if code != REPORT_OFFERING} == {
        row: 1 for row, code in r6_rows.items() if code != REPORT_OFFERING}, close_rounds
    close_rounds = Counter({row: count for row, count in close_rounds.items() if row in r5_rows})
    assert set(close_rounds) == {run.dispatch_rows[label] for label in opened}
    assert Counter(e["data"]["dispatch_id"] for e in events if e["type"] == "dispatch.closed"
                   and e["data"]["offering_id"] == oid) == Counter({dispatch_id(label): 1 for label in opened})


def _check_dispatch_surfaces(exchanges: list[_Exchange], runs: list[_CliRun], lines: list[str], rich: Any,
                             engine_fake: Any, db_url: str, run: _DispatchPhase, events: list[dict],
                             secrets: list[str]) -> None:
    """Os alvos dos alunos canário do
    disparo (`DISPATCH_PREFIXES`) em todas as superfícies, só nas exceções declaradas, cada uma com controle
    positivo:
    - o id do Telegram do aluno só no `chat_id` dos métodos de envio e de edição (`DISPATCH_CHAT_METHODS`);
    - o texto digitado pelo aluno só nas colunas da lista (`DISPATCH_STUDENT_TEXT_COLUMNS`);
    - o `@username`, o nome e o id do Telegram de outro aluno só em `messages.list`, `cq messages list`,
      `free_messages.text` e `telegram_updates.text`;
    - o corpo do `grade` enviado ao motor falso (a resposta crua);
    - o eco da própria resposta só no conteúdo das mensagens ao chat do próprio aluno.
    Superfícies: respostas da API (cabeçalhos, corpo e URL), saídas do CLI, logs, eventos, parâmetros
    enviados à Bot API falsa e ao motor falso e todas as colunas de todas as tabelas (inclusive tarefas e
    `outbox_messages`). Também a sentinela do gabarito (nunca na API, no CLI, nos eventos, nos logs nem
    antes do fechamento na Bot API falsa; presente na edição do fechamento), o `technical_detail` do motor
    (em lugar nenhum) e os segredos nas chamadas ao motor falso (`find_secrets`).

    Relatórios: a questão canário do disparo é validada até `scheduled` (o passo 1 da fase), e a
    validação grava a amostra da prévia (`dispatch_preview_contents.ir`, a IR literal da variante, sem redação;
    a prévia para o professor); a questão canário da fase de relatórios (a mesma identidade de B no título, no
    enunciado e numa alternativa, e o nome na dica) também grava a amostra e, entregue na prévia ao membro da
    equipe, a renderização (`dispatch_preview_contents.rendered`). Na varredura do banco, as duas colunas da amostra
    são a única exceção nomeada a mais (a do conteúdo da prévia, nas varreduras do disparo), com controle positivo e
    contagem exata: exatamente os rótulos da identidade da questão canário em cada uma."""
    tg_labels = _dispatch_labels(DISPATCH_TG_IDENTITY)
    list_labels_b = _dispatch_labels(DISPATCH_IDENTITY)
    typed = _dispatch_labels(DISPATCH_ANSWER, *DISPATCH_FREE_TEXTS)
    eco = _dispatch_labels(DISPATCH_ANSWER)
    assert tg_labels and list_labels_b and tg_labels.isdisjoint(list_labels_b) and eco == tg_labels | list_labels_b
    messages_path = f"/v1/offerings/{DISPATCH_OFFERING}/messages"

    # Respostas da API: só o 200 de `messages.list` do oferecimento da fase leva os alvos do Telegram de B.
    def is_messages(ex: _Exchange) -> bool:
        return (ex.method, ex.path, ex.status) == ("GET", messages_path, 200)

    # No molde da superfície `path_ra`: fora o segmento `{ref}` da URL dos dois
    # pedidos que o próprio teste manda com um RA em `_dispatch_cli_phase` (`dispatches show <code> <RA de B>`, 400
    # `invalid_format`, e `messages list <RA de A>`, 404 `not_found`, ambos sem eco). O resto da URL, os cabeçalhos
    # e o corpo desses pedidos continuam varridos.
    typed_ra = {("GET", f"/v1/offerings/{DISPATCH_OFFERING}/dispatches/{DISPATCH_B.ra}", 400): DISPATCH_B.ra,
                ("GET", f"/v1/offerings/{DISPATCH_A.ra}/messages", 404): DISPATCH_A.ra}

    def api_url(ex: _Exchange) -> str:
        ra = typed_ra.get((ex.method, ex.path, ex.status))
        return ex.url if ra is None else ex.url.replace(ex.path, ex.path.replace(ra, "{ref}"), 1)

    leaks = [(ex.method, ex.path, ex.status, sorted(found)) for ex in exchanges
             if (found := _dispatch_labels(ex.headers, ex.body, api_url(ex)))
             and not (is_messages(ex) and found <= tg_labels)]
    assert leaks == [], f"aluno canário do disparo em respostas da API: {leaks}"
    # Controle positivo da exceção: cobre exatamente 1 pedido de cada, cuja URL inteira tem o RA.
    for key, ra in typed_ra.items():
        excepted = [ex for ex in exchanges if (ex.method, ex.path, ex.status) == key]
        assert len(excepted) == 1 and f"disparo-ra:{ra}" in _dispatch_labels(excepted[0].url), (key, len(excepted))
        assert _dispatch_labels(api_url(excepted[0])) == set(), key
    listed = [ex for ex in exchanges if is_messages(ex)]
    assert listed and all(_dispatch_labels(ex.body) == tg_labels for ex in listed), "controle positivo"
    assert all(_dispatch_labels(ex.headers, ex.url) == set() for ex in listed)
    # Saídas do CLI: só `messages list` com sucesso leva os alvos do Telegram de B.
    cli_leaks = [(r.args, r.json, sorted(found)) for r in runs if (found := _dispatch_labels(*r.texts))
                 and not (r.leaf == "messages list" and r.exit_code == 0 and found <= tg_labels)]
    assert cli_leaks == [], f"aluno canário do disparo na saída do CLI: {cli_leaks}"
    assert any(r.leaf == "messages list" and r.exit_code == 0 and _dispatch_labels(r.stdout) == tg_labels for r in runs)
    # Logs e eventos: nada.
    assert [(i, sorted(found)) for i, line in enumerate(lines) if (found := _dispatch_labels(line))] == []
    dumped_events = json.dumps(events, ensure_ascii=False)
    assert _dispatch_labels(dumped_events) == set(), "aluno canário do disparo nos eventos"

    # Parâmetros enviados à Bot API falsa: o `chat_id` dos métodos de envio e de edição fica de fora; o eco
    # da resposta de A só no conteúdo das mensagens ao chat de A.
    a_chat, b_chat = TG_DISPATCH_A1.user_id, TG_DISPATCH_B.user_id
    telegram_leaks, chat_ids, echoed = [], set(), []
    for call in rich.calls():
        params = json.loads(json.dumps(call.params, ensure_ascii=False, default=str))
        chat = params.get("chat_id") if call.method in DISPATCH_CHAT_METHODS else None
        if call.method in DISPATCH_CHAT_METHODS:
            chat_ids |= _dispatch_labels(json.dumps(params.pop("chat_id", None)))
            if isinstance(params.get("reply_parameters"), dict):
                params["reply_parameters"].pop("chat_id", None)
        found = _dispatch_labels(json.dumps(params, ensure_ascii=False))
        allowed = eco if call.bot_id == run.bot_id and str(chat) == str(a_chat) else set()
        if found - allowed:
            telegram_leaks.append((call.method, call.seq, sorted(found - allowed)))
        if found and call.bot_id == run.bot_id and str(chat) == str(a_chat):
            echoed.append(call.seq)
    assert telegram_leaks == [], f"aluno canário do disparo nos parâmetros enviados ao Telegram: {telegram_leaks}"
    assert chat_ids == {f"disparo-tg-id:{a_chat}", f"disparo-tg-id:{b_chat}"}, chat_ids
    assert echoed, "controle positivo: o eco da resposta de A saiu ao chat de A"
    # O erro de formato provocado por A com a identidade de B no campo numérico (passo 5 da fase; a regra inclui
    # os erros): a explicação traduzida saiu só ao chat de A, e a identidade que ela levar cabe no eco da própria
    # resposta; a varredura acima já cobre os parâmetros dessa mensagem. Controle positivo: a resposta crua
    # com a identidade foi ao motor duas vezes (a do 1º disparo e a do erro de formato).
    format_prefix = _phase_text("FORMAT_ERROR")
    format_outputs = [(chat, text) for chat, text in _bot_outputs(rich, run.bot_id) if format_prefix in text]
    assert format_outputs and {chat for chat, _ in format_outputs} == {a_chat}, format_outputs
    assert all(_dispatch_labels(text) <= eco for _, text in format_outputs), format_outputs
    assert len([c for c in engine_fake.calls("grade")
                if list_labels_b <= _dispatch_labels(json.dumps(c.request, ensure_ascii=False))]) == 2

    # Chamadas ao motor falso: só o corpo do `grade` (a resposta crua de A) leva a identidade; segredos em
    # nenhum pedido nem resposta; o detalhe técnico só na resposta crua do motor (controle positivo).
    engine_leaks = [(c.op, c.seq, sorted(found)) for c in engine_fake.calls()
                    if (found := _dispatch_labels(json.dumps(c.request, ensure_ascii=False)))
                    and not (c.op == "grade" and found <= eco)]
    assert engine_leaks == [], f"aluno canário do disparo nos pedidos ao motor: {engine_leaks}"
    assert any(c.op == "grade" and list_labels_b <= _dispatch_labels(json.dumps(c.request, ensure_ascii=False))
               for c in engine_fake.calls()), "controle positivo: a resposta crua foi ao motor"
    engine_secrets = [(c.op, c.seq) for c in engine_fake.calls()
                      if find_secrets(json.dumps([c.request, c.response], ensure_ascii=False), secrets)]
    assert engine_secrets == [], f"segredo nas chamadas ao motor falso: {engine_secrets}"
    assert any(TECHNICAL_DETAIL_SENTINEL in json.dumps(c.response or {}) for c in engine_fake.calls("grade")), (
        "controle positivo: o motor mandou o technical_detail na correção quebrada")

    # A sentinela do gabarito e o detalhe técnico.
    for ex in exchanges:
        assert ANSWER_SENTINEL not in ex.body and TECHNICAL_DETAIL_SENTINEL not in ex.body, (ex.method, ex.path)
    for r in runs:
        for text in r.texts:
            assert ANSWER_SENTINEL not in text and TECHNICAL_DETAIL_SENTINEL not in text, r.args
    joined = "\n".join(lines)
    assert ANSWER_SENTINEL not in joined and TECHNICAL_DETAIL_SENTINEL not in joined
    assert ANSWER_SENTINEL not in dumped_events and TECHNICAL_DETAIL_SENTINEL not in dumped_events
    early = [(c.method, c.seq) for c in rich.calls(bot_id=run.bot_id) if c.seq <= run.last_seq_before_close
             and ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False)]
    assert early == [], f"o gabarito chegou ao aluno antes do fechamento: {early}"
    revealed = {e.chat_id for e in rich.edits(run.bot_id) if e.call_seq > run.last_seq_before_close
                and ANSWER_SENTINEL in (e.after.plain_text or "")}
    assert revealed == {a_chat, b_chat}, "controle positivo: o gabarito na edição do fechamento"
    telegram_detail = [(c.method, c.seq) for c in rich.calls()
                       if TECHNICAL_DETAIL_SENTINEL in json.dumps(c.params, ensure_ascii=False)]
    assert telegram_detail == []

    # Banco: todas as colunas de todas as tabelas, com as exceções exatas.
    def allowed_in(key: tuple[str, str], label: str) -> bool:
        prefix = label.split(":", 1)[0]
        if key[0] == "identities":
            return prefix in DISPATCH_LIST_PREFIXES
        if key in INTERNAL_IDENTITY_COLUMNS:
            return prefix == "disparo-tg-id"
        if key == ("free_messages", "text"):
            return label in tg_labels
        if key in DISPATCH_STUDENT_TEXT_COLUMNS:
            return label in typed
        # Relatórios: a exceção nomeada das colunas da amostra da prévia, só com os rótulos da
        # identidade da questão canário (controle positivo e contagem exata abaixo).
        if key in REPORT_SAMPLE_COLUMNS:
            return label in canary_labels
        return False

    canary_labels = _dispatch_labels(DISPATCH_IDENTITY, DISPATCH_B.name)
    assert list_labels_b and list_labels_b <= canary_labels, "controle positivo: a identidade da questão canário"
    found_db: dict[tuple[str, str], set[str]] = {}
    detail_db: list[tuple[str, str]] = []
    for table, column, text in scan_all_tables(db_url):
        if hits := _dispatch_labels(text):
            found_db.setdefault((table, column), set()).update(hits)
        if TECHNICAL_DETAIL_SENTINEL in text:
            detail_db.append((table, column))
    outside = {key: sorted(label for label in labels if not allowed_in(key, label)) for key, labels in found_db.items()}
    assert {key: labels for key, labels in outside.items() if labels} == {}, "aluno do disparo fora das exceções"
    assert detail_db == [], f"technical_detail gravado: {detail_db}"
    # Controles positivos de cada exceção.
    list_labels = {label for label, _ in DISPATCH_TARGETS if label.split(":", 1)[0] in DISPATCH_LIST_PREFIXES}
    assert set().union(*(labels for key, labels in found_db.items() if key[0] == "identities")) == list_labels
    ids = {f"disparo-tg-id:{a_chat}", f"disparo-tg-id:{b_chat}"}
    assert found_db.get(("telegram_updates", "chat_id")) == ids
    assert found_db.get(("telegram_updates", "sender_id")) == ids
    assert found_db.get(("delivery_endpoints", "chat_id")) == ids
    assert found_db.get(("free_messages", "text")) == tg_labels
    assert typed <= found_db.get(("telegram_updates", "text"), set())
    assert list_labels_b <= found_db.get(("submissions", "raw_input"), set())
    # Relatórios: o controle positivo e a contagem exata da exceção da amostra: em cada uma das
    # duas colunas, exatamente os rótulos da identidade da questão canário (no esqueleto, sem a tabela).
    assert {key: found_db.get(key) for key in REPORT_SAMPLE_COLUMNS} == dict.fromkeys(REPORT_SAMPLE_COLUMNS, canary_labels), (
        {key: found_db.get(key) for key in REPORT_SAMPLE_COLUMNS})

    # Controles positivos sintéticos: resposta, log e o `@username` de uma conta.
    synthetic = _Exchange("GET", "/sintetico", 200, f"X-Eco: {DISPATCH_B.ra}", json.dumps({"x": DISPATCH_A.name}))
    assert {f"disparo-ra:{DISPATCH_B.ra}", f"disparo-nome:{DISPATCH_A.name}"} <= _dispatch_labels(synthetic.headers,
                                                                                            synthetic.body)
    formatted_line = JsonFormatter().format(logging.makeLogRecord(
        {"name": "cq_server.worker", "levelname": "INFO", "levelno": logging.INFO, "msg": "sintetico",
         "event": f"sintetico {DISPATCH_B.email}"}))
    assert f"disparo-email:{DISPATCH_B.email}" in _dispatch_labels(formatted_line)
    assert f"disparo-tg-username:{TG_DISPATCH_B.username}" in _dispatch_labels(f"@{TG_DISPATCH_B.username}")


def _professor_in(*texts: str | None) -> bool:
    """O detector do e-mail do professor canário (subcadeia, sem diferença de caixa)."""
    folded = PROFESSOR_EMAIL.casefold()
    return any(folded in (text or "").casefold() for text in texts)


def _check_professor_email(exchanges: list[_Exchange], runs: list[_CliRun], lines: list[str], rich: Any,
                           bot_api: Any, db_url: str, sql: psycopg.Connection, run: _DispatchPhase,
                           events: list[dict]) -> None:
    """Mensagens livres opcionais: o e-mail do professor
    canário, cadastrado como contato do oferecimento da fase de disparos, aparece **só** em
    `offering_contacts.email`, no `outbox_messages.text` da resposta do modo desligado (passo 5.9) e no
    `sendMessage` dela ao chat de A, com controle positivo nos três; em nenhuma resposta da API (as do
    oferecimento mostram só `contact_email_set: true`), evento, log, tarefa nem saída do CLI. O e-mail válido de
    `STUDENT_A` informado como contato (as `CANARY_CALLS` do cadastro e da alteração) não fica gravado: o único
    contato é o do professor."""
    a_chat = TG_DISPATCH_A1.user_id
    # Respostas da API (cabeçalhos, corpo e URL), saídas do CLI, logs e eventos: nada.
    assert [(ex.method, ex.path, ex.status) for ex in exchanges if _professor_in(ex.headers, ex.body, ex.url)] == []
    assert any(ex.status == 200 and '"contact_email_set":true' in ex.body.replace(" ", "") for ex in exchanges), (
        "controle contra o vazio: uma resposta da API com o oferecimento com contato")
    assert [(r.args, r.json) for r in runs if _professor_in(*r.texts)] == []
    assert [i for i, line in enumerate(lines) if _professor_in(line)] == []
    assert not _professor_in(json.dumps(events, ensure_ascii=False)), "e-mail do professor nos eventos"
    # Parâmetros enviados ao Telegram: só o `sendMessage` da resposta do passo 5.9, ao chat de A.
    sent = [(c.method, c.bot_id, str(c.params.get("chat_id"))) for c in rich.calls()
            if _professor_in(json.dumps(c.params, ensure_ascii=False, default=str))]
    assert sent == [("sendMessage", run.bot_id, str(a_chat))], sent
    # `rich` delega `calls` ao mesmo dublê (`DispatchExtension.__getattr__`); aqui, as
    # chamadas dos outros bots (as do bot da fase já foram conferidas acima).
    assert [c.seq for c in bot_api.calls() if c.bot_id != run.bot_id
            and _professor_in(json.dumps(c.params, ensure_ascii=False, default=str))] == []
    # Banco (todas as colunas de todas as tabelas, inclusive `tasks`): só as duas exceções, cada uma com a linha
    # exata (controle positivo).
    found = sorted({(table, column) for table, column, text in scan_all_tables(db_url) if _professor_in(text)})
    assert found == [("offering_contacts", "email"), ("outbox_messages", "text")], found
    contact_reply = getattr(deliverytexts, "answers_only_reply", None)
    assert callable(contact_reply), "deliverytexts.answers_only_reply ausente"
    # A resposta sai pelo endereço vigente de A, com a pessoa
    # (a regra: pela fila com `priority = 1`, pelo endereço vigente, `_reply`; a forma (b)),
    # e nunca com `recipient_person_id` nulo.
    assert [row for row in sql.execute(
        "SELECT o.text, o.recipient_person_id, o.state FROM outbox_messages o JOIN bots b ON b.id = o.bot_id "
        "WHERE b.telegram_bot_id = %s AND position(lower(%s) in lower(o.text)) > 0",
        (run.bot_id, PROFESSOR_EMAIL)).fetchall()] == [
            (contact_reply(PROFESSOR_EMAIL), run.people["a"]["person_id"], "sent")]
    assert sql.execute("SELECT o.code, c.email FROM offering_contacts c JOIN offerings o ON o.id = c.offering_id "
                       "ORDER BY c.id").fetchall() == [(DISPATCH_OFFERING, PROFESSOR_EMAIL)], (
        "contato gravado fora do oferecimento da fase (o e-mail de aluno canário é recusado)")
    # Controle positivo sintético do detector.
    assert _professor_in(json.dumps({"x": PROFESSOR_EMAIL.upper()})) and not _professor_in(STUDENT_A.email)


def _dispatch_cli_phase(cli_invoke: Any, client: Any, clock: Any, seed: _Seed, state: CallState,
                        run_phase: _DispatchPhase) -> list[_CliRun]:
    """As 6 folhas do disparo com sucesso (texto e `--json`), com erro da API
    (código 1), com a API inalcançável (código 3) e com uso incorreto (código 2), sempre fora de terminal.
    As consultas usam o oferecimento da fase de disparos (a prévia redigida da questão canário e as
    mensagens livres com a exceção das identidades); o preparo e o cancelamento, disparos próprios; a confirmação,
    os dois últimos alvos de `seed_state` (agendados, sem aluno). O `repo` com o token privado registrado
    é recusado sem eco. Mensagens livres opcionais: a folha `offerings settings`, com sucesso (texto e `--json`)
    no oferecimento da fase e com uso incorreto (código 2)."""
    runs: list[_CliRun] = []

    def run(args: list[str], exit_code: int, *, json_mode: bool = False, **kwargs: Any) -> Any:
        result = cli_invoke(list(args), json=json_mode, **kwargs)
        leaf = _leaf_of(args)
        assert leaf is not None, f"{args} não é um comando folha do cli"
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{args}: o CLI levantou {type(result.exception).__name__}")
        assert result.exit_code == exit_code, (
            f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
        for text in (result.stdout, result.stderr):
            assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
        if exit_code != 0:
            assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
        runs.append(_CliRun(leaf, tuple(args), json_mode, result.exit_code, result.stdout, result.stderr,
                            result.output))
        return result

    def unreachable(args: list[str], *, json_mode: bool = False) -> Any:
        return run(args, 3, json_mode=json_mode, http=None, env={"CQ_API_URL": UNREACHABLE_API})

    code = DISPATCH_OFFERING
    canary_id = run_phase.dispatches["canary"]["dispatch_id"]
    closed_id = run_phase.dispatches["d1"]["dispatch_id"]
    b_mark = run_phase.mark("b")

    # --- sucesso (texto e --json) ---
    shown = get_dispatch(client, ADMIN_TOKEN, code, canary_id)
    r = run(["dispatches", "show", code, canary_id], 0)
    assert b_mark in r.stdout and canary_id in r.stdout, "a prévia redigida (controle positivo) no CLI"
    r = run(["dispatches", "show", code, canary_id], 0, json_mode=True)
    assert json.loads(r.stdout) == shown and shown["preview"] == run_phase.preview
    listed = list_dispatches(client, ADMIN_TOKEN, code)
    r = run(["dispatches", "list", code], 0)
    assert all(item["dispatch_id"] in r.stdout for item in listed["items"])
    r = run(["dispatches", "list", code], 0, json_mode=True)
    assert json.loads(r.stdout) == listed
    messages = list_free_messages(client, ADMIN_TOKEN, code)
    assert messages == run_phase.messages
    r = run(["messages", "list", code], 0)
    assert r.stdout.splitlines() == [f"{item['created_at']}  {item['alias']}  {item['text']}"
                                     for item in messages["items"]] + [f"Próximo cursor: {messages['next_cursor']}"]
    r = run(["messages", "list", code], 0, json_mode=True)
    assert json.loads(r.stdout) == messages

    prepare_args = ["dispatches", "prepare", code, "--repo", DISPATCH_REPO, "--commit", DISPATCH_COMMIT,
                    "--qid", "with_media"]
    r = run(prepare_args, 0)
    match = re.fullmatch(rf"Disparo preparado em {re.escape(code)}: ({UUID4_RE}) \(validando\)\.\n"
                         rf"Consulte com: cq dispatches show {re.escape(code)} \1\n", r.stdout)
    assert match is not None, r.stdout
    text_prepared = match.group(1)
    r = run(prepare_args, 0, json_mode=True)
    json_prepared = json.loads(r.stdout)
    assert (json_prepared["state"], json_prepared["origin"], json_prepared["created_at"]) == (
        "validating", "cli", iso(clock.now()))
    r = run(["dispatches", "cancel", code, text_prepared], 0)
    assert r.stdout.splitlines() == [f"Disparo cancelado: {text_prepared}."]
    r = run(["dispatches", "cancel", code, json_prepared["dispatch_id"]], 0, json_mode=True)
    cancelled = json.loads(r.stdout)
    assert (cancelled["dispatch_id"], cancelled["state"], cancelled["cancel_reason"], cancelled["cancelled_at"]) == (
        json_prepared["dispatch_id"], "cancelled", "operator", iso(clock.now()))

    seed_code = state.dispatch_offering["code"]
    text_target, json_target = state.confirm_targets.pop(), state.confirm_targets.pop()
    before = get_dispatch(client, ADMIN_TOKEN, seed_code, text_target)
    r = run(["dispatches", "confirm", seed_code, text_target], 0)
    assert r.stdout.splitlines() == [f"Disparo confirmado: {text_target}; abre em {before['opens_at']}, fecha em "
                                     f"{before['closes_at']}; 0 alunos-alvo agora."]
    r = run(["dispatches", "confirm", seed_code, json_target], 0, json_mode=True)
    confirmed = json.loads(r.stdout)
    assert (confirmed["dispatch_id"], confirmed["state"], confirmed["confirmed"], confirmed["confirmed_at"]) == (
        json_target, "scheduled", True, iso(clock.now()))

    # Mensagens livres opcionais: `offerings settings` no oferecimento da fase
    # (desligado no passo 5.9, com contato): religa pelo texto e repete pelo `--json` (sem mudança); a linha
    # exata não traz o e-mail (só "definido"), e o corpo, só `contact_email_set`.
    r = run(["offerings", "settings", code, "--free-messages", "on"], 0)
    assert r.stdout.splitlines() == [
        f"Configuração de {code}: mensagens livres ligadas; contato do professor definido."], r.stdout
    r = run(["offerings", "settings", code, "--free-messages", "on"], 0, json_mode=True)
    settings_body = json.loads(r.stdout)
    assert_matches("offerings.update_settings", 200, settings_body)
    assert (settings_body["code"], settings_body["free_messages"], settings_body["contact_email_set"]) == (
        code, True, True)
    assert settings_body == client.get(f"/v1/offerings/{code}", headers=ADMIN).json()

    # --- erro da API (código 1), com valores canário e o token privado na entrada ---
    remember_secret(REPO_PRIVATE_TOKEN)
    secret_args = ["dispatches", "prepare", code, "--repo", f"{DISPATCH_REPO}?private_token={REPO_PRIVATE_TOKEN}",
                   "--commit", DISPATCH_COMMIT, "--qid", "with_media"]
    r = run(secret_args, 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - repo: invalid_format"]
    r = run(secret_args, 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                                              "fields": [{"field": "repo", "reason": "invalid_format"}]}}
    r = run(["dispatches", "prepare", code, "--repo", f"https://repos.cq.test/{DISPATCH_B.ra}.git", "--commit",
             DISPATCH_COMMIT, "--qid", "with_media"], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:write"]
    r = run(["dispatches", "show", code, str(uuid.uuid4())], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["dispatches", "show", code, DISPATCH_B.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - dispatch_id: invalid_format"]
    r = run(["dispatches", "list", code], 1, token=seed.events_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]
    for leaf in ("confirm", "cancel"):
        r = run(["dispatches", leaf, code, closed_id], 1)
        assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_dispatch_state']}"]
        r = run(["dispatches", leaf, code, closed_id], 1, json_mode=True)
        assert json.loads(r.stderr) == {"error": {"code": "invalid_dispatch_state",
                                                  "message": MESSAGES["invalid_dispatch_state"]}}
    r = run(["messages", "list", code], 1, token=seed.events_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]
    r = run(["messages", "list", DISPATCH_A.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]

    # --- API inalcançável (código 3), uma vez por folha ---
    for args in (prepare_args, ["dispatches", "show", code, canary_id], ["dispatches", "list", code],
                 ["dispatches", "confirm", code, canary_id], ["dispatches", "cancel", code, canary_id],
                 ["messages", "list", code]):
        r = unreachable(args)
        assert r.stderr.splitlines() == [f"Erro: {UNREACHABLE_MESSAGE}"], (args, r.stderr)
    r = unreachable(["messages", "list", code], json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "api_unreachable", "message": UNREACHABLE_MESSAGE}}

    # --- uso incorreto (código 2): opção obrigatória ausente, opção tipada e argumento a mais ---
    for args in (["dispatches", "prepare", code, "--commit", DISPATCH_COMMIT, "--qid", "with_media"],
                 ["dispatches", "list", code, "--limit", DISPATCH_B.name],
                 ["messages", "list", code, "--after", DISPATCH_B.name],
                 ["dispatches", "show", code],
                 ["dispatches", "cancel", code, canary_id, DISPATCH_B.ra],
                 # mensagens livres opcionais: sem nenhuma opção, sem chamada à API
                 ["offerings", "settings", code]):
        r = run(args, 2)
        assert r.stderr.startswith("Uso incorreto:"), (args, r.stderr)
    return runs


# ---------------------------------------------------------------------------------------------
# fase de relatórios e avisos à equipe
# (a prévia, os relatórios, os avisos e o resumo diário)
# ---------------------------------------------------------------------------------------------


@dataclass
class _ReportPhase:
    offering: dict
    offering_row: int
    bot_id: int  # id do bot no Telegram (o prefixo do token na Bot API falsa)
    bot_row: int
    archive_offering: dict
    archive_bot_id: int
    team: list[dict]  # os 2 membros: rótulo, `pseudo_id`, `person_row`, `user` e o estado do endereço
    people: dict[str, dict]  # "a"/"b" → {pseudo_id, alias, person_id}
    dispatches: dict[str, str]  # rótulo → `dispatch_id`
    dispatch_rows: dict[str, int]  # rótulo → `dispatches.id`
    stream: io.StringIO  # o console do worker de console da fase (parte C)
    engine: Any  # o motor falso próprio da fase
    runs: list[_CliRun]
    started: datetime  # o relógio na subida do worker com bots (a posse dos bots elegíveis)
    bots_stopped: datetime  # o relógio na parada ordenada do worker com bots (a soltura das posses)
    tap: Any  # o toque no botão da prévia (parte A)
    digest_dates: dict[str, str]  # estado do resumo diário → data local (AAAA-MM-DD)
    local_time: str  # o horário do resumo diário ligado na fase

    def pseudo(self, label: str) -> str:
        return self.people[label]["pseudo_id"]

    def mark(self, label: str) -> str:
        return marker(self.people[label]["alias"])

    @property
    def member(self) -> dict:
        """O membro da equipe com endereço ativo (o destinatário dos avisos)."""
        return self.team[0]


def _report_morning(clock: Any) -> datetime:
    """Leva o `FakeClock` à próxima `REPORT_MORNING` hora local do fuso dos oferecimentos (estritamente depois
    do agora): os disparos, o resumo diário e as datas locais da fase ficam no mesmo dia."""
    zone = ZoneInfo(OFFERING_ZONE)
    local = clock.now().astimezone(zone)
    target = local.replace(hour=REPORT_MORNING, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return clock.set(target.astimezone(timezone.utc))


def _next_local(after: datetime, local_time: str, *, strictly: bool) -> datetime:
    """A próxima ocorrência de `local_time` (HH:MM, no fuso dos oferecimentos) a partir de `after` (ou
    estritamente depois, com `strictly`), em UTC; o modelo do teste para o `next_occurrence` dos relatórios
    e do acompanhamento, sem chamar o núcleo."""
    zone = ZoneInfo(OFFERING_ZONE)
    hour, minute = (int(part) for part in local_time.split(":"))
    local = after.astimezone(zone)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate < local or (strictly and candidate == local):
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _template_regex(template: str) -> re.Pattern[str]:
    """O padrão de um modelo de `noticetexts`: o texto fixo literal, `.*?` no lugar de cada `{campo}` e, entre
    duas linhas do modelo, quaisquer linhas opcionais (as constantes `*_LINE`, `PREVIEW_PARTS`, os avisos)."""
    lines = []
    for line in template.split("\n"):
        parts = re.split(r"\{[a-z_][a-z0-9_]*\}", line)
        lines.append(".*?".join(re.escape(part) for part in parts))
    return re.compile(r"\n(?:[^\n]*\n)*?".join(lines))


def _report_text_value(name: str) -> str:
    """O texto (ou o modelo) de um nome de `REPORT_TEXT_NAMES`/`REPORT_TEXT_EXCEPTIONS`, lido do `noticetexts` na hora
    (`NOME[chave]` lê o valor do dicionário)."""
    from cq_server import noticetexts

    match = re.fullmatch(r"([A-Z_]+)\[([a-z_]+)\]", name)
    if match is None:
        value = getattr(noticetexts, name)
    else:
        value = getattr(noticetexts, match.group(1))[match.group(2)]
    assert isinstance(value, str) and value, f"texto de noticetexts ausente ou vazio: {name}"
    return value


def _check_report_text_names() -> None:
    """Contra o vazio: toda constante de texto de `noticetexts` (os dicionários, chave a
    chave) tem destino no canário: sai na fase ou é exceção por nome; nada fica sem destino."""
    from cq_server import noticetexts

    destinations = set(REPORT_TEXT_NAMES) | set(REPORT_TEXT_EXCEPTIONS)
    assert set(REPORT_TEXT_NAMES).isdisjoint(REPORT_TEXT_EXCEPTIONS)
    published = set()
    for name in dir(noticetexts):
        value = getattr(noticetexts, name)
        if not name.isupper() or name == "COMMAND_LEAVES":
            continue
        if isinstance(value, str):
            published.add(name)
        elif isinstance(value, dict):
            published |= {f"{name}[{key}]" for key in value}
    assert sorted(published - destinations) == [], "texto de noticetexts sem destino no canário"
    assert sorted(destinations - published) == [], "destino do canário fora de noticetexts"
    for name in destinations:
        _report_text_value(name)


def _report_phase(client: Any, clock: Any, sql: psycopg.Connection, rich: Any, bot_worker_thread: Any,
                  worker_thread: Any, settings: Any, engine_fake: Any, repo_fetcher: Any, cli_invoke: Any,
                  db_url: str) -> _ReportPhase:
    """Fase de relatórios e avisos à equipe, em sequência fixa,
    depois da fase de disparos e antes da da API, com o relógio na manhã seguinte (`_report_morning`).

    Oferecimento, bot e motor próprios: o oferecimento `REPORT_OFFERING` com bot próprio na Bot API falsa
    estendida (`rich_bot_api`, a mesma das fases de bot, de alunos e de disparos: o worker com bots consulta todos os bots
    elegíveis, então um dublê separado tiraria o acesso dos bots das outras fases) e um **motor falso
    próprio** (`FakeEngine` na mesma área de repositórios), para que as chamadas da fase não entrem nas
    contagens exatas de `_check_dispatch_surfaces`; os alunos canário do disparo importados pela API (B pendente) e
    A vinculado por SQL na conta A1 (como na fase de disparos); a equipe de 2 membros cadastrados pela API, com
    matrícula `team` ativa e endereço por SQL, o 2º `blocked` (nenhum vínculo de equipe pelo bot: o 201 de `team.link` e o
    `/start` mudariam contagens congeladas da fase de bot). Duas partes, cada uma com um worker próprio:
    A. **Telegram** (worker com bots, `REPORT_WORKER_ID`): o chat do membro fica conhecido (o `my_chat_member`
       de desbloqueio); o disparo agendado de duas partes é validado e a prévia chega ao membro ativo
       (cabeçalho com `PREVIEW_PARTS` e a questão com os botões inertes); o toque num botão (`preview_tapped`,
       a confirmação `PREVIEW_TAP`) e a resposta ao cabeçalho (`preview_replied`, `PREVIEW_REPLY`); um 2º
       disparo validado tem a `create_variant` da prévia presa no motor e é cancelado pela coordenação durante
       ela (a prévia termina `gave_up`, sem mensagem); a questão não suportada é rejeitada (aviso de
       rejeição); o relógio passa da abertura do agendado sem confirmação (`expired`, aviso de cancelamento);
       o token do bot é revogado e trocado (aviso de bot sem acesso, no retorno); o bot do 2º oferecimento
       (`REPORT_ARCHIVE_OFFERING`) perde o acesso e o oferecimento é arquivado sem a volta (aviso `not_sent`).
    B. **Console** (worker de console, `REPORT_CONSOLE_WORKER_ID`; as mensagens com a identidade vão ao console
       da fase, fora da Bot API falsa): a **questão canário** (identidade de B no título, no enunciado e numa
       alternativa) é validada e entregue na prévia ao membro (`PREVIEW_IMMEDIATE`), que responde a ela
       (`preview_replied`), e é cancelada; três disparos imediatos (texto, número e o quebrado na correção)
       abrem para A (o aviso "aberto"): A responde o texto e o número com a identidade de B (o número dá erro
       de formato e depois vale), e a correção do 3º quebra (aviso de falha de questão); os três fecham pelo
       roteiro determinístico e congelam (os resumos de fechamento); o relatório em JSON e em CSV, no
       momento e congelado, as versões e o reenvio (versão 2, o resumo reenviado); o resumo diário ligado: no
       dia, com atividade (`queued`), num dia pulado (`missed`) e no dia seguinte sem atividade
       (`no_activity`); as 5 folhas do CLI com sucesso (`--csv` e `--json` incluídos) e com erro.
    Termina parando os próprios workers, sem update pendente, sem tarefa viva e com a fila sem `pending` nem
    `leased` antes de cada avanço do relógio (as rodadas `dispatch.close` entram no `n_close` medido do teste).
    """
    from cq_server import noticetexts
    from cq_server.adapters.console import ConsoleInbox
    from tests.helpers.api import (get_offering_digest, get_report, get_report_csv, list_report_versions,
                                   resend_report_summary, update_offering_digest)
    from tests.helpers.console_student import ConsoleStudent
    from tests.helpers.engine import FakeEngine
    from tests.helpers.team import team_messages

    for text in (REPORT_TEAM_REPLY, *(option.html for option in REPORT_CANARY_OPTIONS[1:])):
        assert find_canary(text, prefixes=ALL_PREFIXES + STUDENT_PREFIXES + DISPATCH_PREFIXES) == [], text
    assert _dispatch_labels(DISPATCH_IDENTITY) <= _dispatch_labels(REPORT_CANARY_OPTIONS[0].html), (
        "controle positivo: a alternativa da questão canário tem a identidade de B")
    assert find_canary(REPORT_CANARY_OPTIONS[0].html, prefixes=ALL_PREFIXES + STUDENT_PREFIXES) == []
    started = _report_morning(clock)
    own = FakeEngine(repos_root=engine_fake.repos_root)
    try:
        return _report_phase_run(client, clock, sql, rich, bot_worker_thread, worker_thread, settings, own,
                                 repo_fetcher, cli_invoke, db_url, started, noticetexts=noticetexts,
                                 console_inbox=ConsoleInbox, console_student=ConsoleStudent,
                                 team_messages=team_messages,
                                 api={"get_report": get_report, "get_report_csv": get_report_csv,
                                      "list_report_versions": list_report_versions,
                                      "resend_report_summary": resend_report_summary,
                                      "get_offering_digest": get_offering_digest,
                                      "update_offering_digest": update_offering_digest})
    finally:
        own.close()


def _report_phase_run(client: Any, clock: Any, sql: psycopg.Connection, rich: Any, bot_worker_thread: Any,
                      worker_thread: Any, settings: Any, own: Any, repo_fetcher: Any, cli_invoke: Any, db_url: str,
                      started: datetime, *, noticetexts: Any, console_inbox: Any, console_student: Any,
                      team_messages: Callable[..., list], api: dict[str, Callable[..., Any]]) -> _ReportPhase:
    """O roteiro de `_report_phase` (ver o docstring dela), com o motor falso próprio já no ar."""
    zone = ZoneInfo(OFFERING_ZONE)
    engine = HttpQuestionEngine(EngineSettings(engine_url=own.base_url, timeout_seconds=DISPATCH_ENGINE_TIMEOUT))
    for qid, question in (("multipart", REPORT_TWO_PARTS), ("integer", preset("integer")),
                          ("unsupported", preset("unsupported")),
                          ("canary", preset("canary", identity=DISPATCH_IDENTITY, hint=DISPATCH_B.name,
                                            fields=(single_choice_field(options=REPORT_CANARY_OPTIONS),))),
                          ("text", preset("text", explanation="O céu limpo é azul.")), ("number", preset("number")),
                          ("broken_on_grade", "broken_on_grade")):
        own.program((REPORT_REPO, REPORT_COMMIT, qid), question)

    # 0. Os dois oferecimentos com bot, os alunos e a equipe.
    offering = create_offering(client, ADMIN_TOKEN, code=REPORT_OFFERING, course=DISPATCH_COURSE,
                               term=DISPATCH_TERM, timezone=OFFERING_ZONE)
    offering_id = offering["offering_id"]
    ref = {"offering_id": offering_id, "code": REPORT_OFFERING}
    bot = rich.add_bot()
    assert register_bot(client, ADMIN_TOKEN, REPORT_OFFERING, rich.token_of(bot))["state"] == "registered"
    assert activate_bot(client, ADMIN_TOKEN, REPORT_OFFERING)["state"] == "active"
    archive_offering = create_offering(client, ADMIN_TOKEN, code=REPORT_ARCHIVE_OFFERING, course=DISPATCH_COURSE,
                                       term=DISPATCH_TERM, timezone=OFFERING_ZONE)
    archive_bot = rich.add_bot()
    assert register_bot(client, ADMIN_TOKEN, REPORT_ARCHIVE_OFFERING,
                        rich.token_of(archive_bot))["state"] == "registered"
    assert activate_bot(client, ADMIN_TOKEN, REPORT_ARCHIVE_OFFERING)["state"] == "active"
    assert import_students(client, ADMIN_TOKEN, REPORT_OFFERING, [s.row() for s in CANARY_DISPATCH_STUDENTS]) == {
        "offering": ref, "received": 2, "people_created": 0, "people_reused": 2, "enrollments_created": 2,
        "already_enrolled": 0}
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s", (offering_id,)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    people: dict[str, dict] = {}
    for label, student in (("a", DISPATCH_A), ("b", DISPATCH_B)):
        person_id, pseudo, alias = sql.execute(
            "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
            "WHERE i.ra = %s", (student.ra,)).fetchone()
        people[label] = {"pseudo_id": pseudo, "alias": alias, "person_id": person_id}
    now = clock.now()
    assert sql.execute(
        "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, terms_accepted_at = %s "
        "WHERE person_id = %s AND offering_id = %s AND role = 'student'",
        (now, now, people["a"]["person_id"], offering_row)).rowcount == 1
    sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
                "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                (people["a"]["person_id"], bot_row, TG_DISPATCH_A1.user_id, now, now))
    team: list[dict] = []
    for index, (ra, endpoint) in enumerate(zip(REPORT_TEAM_RAS, ("active", "blocked"), strict=True), start=1):
        person = register_person(client, ADMIN_TOKEN, name=f"Membro da Equipe Relatorio {index}", ra=ra)
        person_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (person["pseudo_id"],)).fetchone()[0]
        user = make_user(71_000 + index)
        sql.execute("INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at) "
                    "VALUES (%s, %s, 'team', 'active', %s, %s)", (person_row, offering_row, now, now))
        sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                    "changed_at) VALUES (%s, %s, 'telegram', %s, %s, %s, %s)",
                    (person_row, bot_row, user.user_id, endpoint, now, now))
        team.append({"label": f"membro-{index}", "pseudo_id": person["pseudo_id"], "person_row": person_row,
                     "user": user, "endpoint": endpoint})
    member = team[0]
    member_chat = member["user"].user_id

    dispatches: dict[str, str] = {}

    def body(label: str) -> dict:
        return get_dispatch(client, ADMIN_TOKEN, REPORT_OFFERING, dispatches[label])

    def counts(label: str) -> dict:
        return body(label)["deliveries"]

    def prepare(label: str, qid: str, *, opens_at: datetime | None = None,
                closes_at: datetime | None = None) -> dict:
        prepared = prepare_dispatch(client, ADMIN_TOKEN, REPORT_OFFERING, repo=REPORT_REPO, commit=REPORT_COMMIT,
                                    qid=qid, opens_at=None if opens_at is None else iso(opens_at),
                                    closes_at=None if closes_at is None else iso(closes_at), origin=DISPATCH_ORIGIN)
        assert (prepared["state"], prepared["origin"]) == ("validating", DISPATCH_ORIGIN)
        dispatches[label] = prepared["dispatch_id"]
        _wait_until(lambda: body(label)["state"] != "validating", REPORT_TIMEOUT, f"{label}: a validação não terminou")
        return body(label)

    def processed(label: str, update_id: int | None, outcome: str) -> None:
        assert update_id is not None, f"{label}: a Bot API falsa descartou o update"
        _wait_until(lambda: _update_state(sql, update_id) == ("processed", outcome), REPORT_TIMEOUT,
                    f"{label}: update não processado como {outcome}")

    def settled() -> None:
        """Nenhum update `pending`, nenhuma tarefa viva e a fila sem linha `pending`/`leased`."""
        _wait_until(lambda: sql.execute(
            "SELECT (SELECT count(*) FROM telegram_updates WHERE state = 'pending') + "
            "       (SELECT count(*) FROM tasks WHERE state IN ('pending', 'leased')) + "
            "       (SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased'))").fetchone()[0] == 0,
            REPORT_TIMEOUT, "a fase de relatórios não assentou")

    def notices(kind: str, code: str = REPORT_OFFERING) -> list[tuple]:
        """Os avisos do tipo no oferecimento: (estado, destinatários, `dispatch_id`, data local), por `id`."""
        return [tuple(row) for row in sql.execute(
            "SELECT n.state, n.recipient_count, d.dispatch_uuid::text, n.local_date::text FROM team_notices n "
            "JOIN offerings o ON o.id = n.offering_id LEFT JOIN dispatches d ON d.id = n.dispatch_id "
            "WHERE o.code = %s AND n.kind = %s ORDER BY n.id", (code, kind)).fetchall()]

    def team_sent(kind: str, count: int) -> list:
        """Espera as `count` mensagens à equipe do tipo de aviso, todas ao membro ativo e `sent`."""
        def ready() -> list:
            rows = team_messages(sql, kind)
            return rows if len(rows) >= count and all(m.state == "sent" for m in rows) else []
        rows = _wait_until_value(ready, REPORT_TIMEOUT, f"as mensagens à equipe do aviso {kind}")
        assert len(rows) == count and {m.recipient for m in rows} == {member["pseudo_id"]}, (kind, rows)
        return rows

    def preview_messages(label: str) -> list:
        """As mensagens da prévia do disparo (o aviso `dispatch_preview` dele), na ordem da fila."""
        found = sql.execute(
            "SELECT n.id FROM team_notices n JOIN dispatches d ON d.id = n.dispatch_id "
            "WHERE n.kind = 'dispatch_preview' AND d.dispatch_uuid = %s", (dispatches[label],)).fetchone()
        if found is None:
            return []
        (notice_row,) = found
        return team_messages(sql, notice_id=notice_row)

    # ============================== A. Telegram (worker com bots) ==============================
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=REPORT_WORKER_ID),
                               channel=ConsoleChannel(io.StringIO()), engine=engine, fetcher=repo_fetcher,
                               delivery=DISPATCH_DELIVERY)
    for tg in (bot, archive_bot):
        _wait_until(lambda tg=tg: _bot_row(sql, "profile_state", tg) == ("configured",), REPORT_TIMEOUT,
                    "perfil não configurado")
        rich.wait_for(lambda tg=tg: rich.pending_polls(tg), timeout=REPORT_TIMEOUT)
    # A posse dos 4 bots elegíveis (os das fases de alunos e de disparos também; todo bot elegível é
    # consultado), no relógio da subida.
    _wait_until(lambda: sql.execute(
        "SELECT count(*) FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE o.code IN (%s, %s, %s, %s) "
        "AND b.lease_owner = %s", (STUDENT_OFFERING, DISPATCH_OFFERING, REPORT_OFFERING, REPORT_ARCHIVE_OFFERING,
                                   REPORT_WORKER_ID)).fetchone()[0] == 4, REPORT_TIMEOUT,
        "o worker da fase não assumiu os bots elegíveis")
    processed("member_known", rich.push_my_chat_member(bot, member["user"], old_status="kicked",
                                                       new_status="member"), "not_handled")
    settled()

    # A1. O disparo agendado de duas partes: a prévia ao membro ativo (o bloqueado não recebe), o toque num
    # botão inerte e a resposta ao cabeçalho.
    exp_opens = clock.now() + REPORT_LEAD
    scheduled = prepare("expired", "multipart", opens_at=exp_opens, closes_at=exp_opens + REPORT_WINDOW)
    assert (scheduled["state"], scheduled["confirmed"]) == ("scheduled", False), scheduled
    header, question = team_sent("dispatch_preview", 2)
    assert (header.role, question.role) == (None, "question"), (header, question)
    settled()
    shown = rich.current(bot, member_chat, int(question.channel_ref))
    assert shown.buttons(), "a questão da prévia leva o teclado do aluno (botões inertes)"
    blocked_chat = team[1]["user"].user_id
    assert [c.seq for c in rich.calls() if c.bot_id == bot and str(c.params.get("chat_id")) == str(blocked_chat)] == [], (
        "o membro com endereço bloqueado não recebe")
    tap = rich.tap(bot, member["user"], int(question.channel_ref), shown.buttons()[0].data)
    processed("member_tap", tap.update_id, "preview_tapped")
    answers = rich.wait_for(lambda: [a for a in rich.answers(bot) if a.callback_id == tap.callback_id],
                            timeout=REPORT_TIMEOUT)
    assert [a.text for a in answers] == [noticetexts.PREVIEW_TAP]
    processed("member_reply", rich.push_text(bot, member["user"], REPORT_TEAM_REPLY,
                                             reply_to_message_id=int(header.channel_ref)), "preview_replied")
    rich.wait_for(lambda: any(text == noticetexts.PREVIEW_REPLY for chat, text in _bot_outputs(rich, bot)
                              if chat == member_chat), timeout=REPORT_TIMEOUT)
    settled()

    # A2. A prévia que desiste: a `create_variant` da prévia (a 2ª da questão; a 1ª é a da validação) presa no
    # motor; a coordenação cancela o disparo durante ela; a gravação final reconfere o estado (`gave_up`).
    integer_variants: list[int] = []

    def preview_variant(call: Any) -> bool:
        if ((call.request or {}).get("question_ref") or {}).get("qid") != "integer":
            return False
        integer_variants.append(call.seq)
        return len(integer_variants) == 2

    own.slow("create_variant", REPORT_PREVIEW_HOLD, times=1, when=preview_variant)
    held = prepare("gave_up", "integer", closes_at=clock.now() + REPORT_WINDOW)
    assert held["state"] == "scheduled", held
    own.wait_for(lambda: [c for c in own.calls("create_variant") if c.outcome == "pending"], timeout=REPORT_TIMEOUT)
    cancelled = cancel_dispatch(client, ADMIN_TOKEN, REPORT_OFFERING, dispatches["gave_up"])
    assert (cancelled["state"], cancelled["cancel_reason"]) == ("cancelled", "operator")
    _wait_until(lambda: [n for n in notices("dispatch_preview") if n[2] == dispatches["gave_up"]] == [
        ("gave_up", 0, dispatches["gave_up"], None)], REPORT_TIMEOUT, "a prévia não desistiu")
    settled()

    # A3. A questão não suportada: rejeitada, com o aviso de rejeição.
    rejected = prepare("rejected", "unsupported", closes_at=clock.now() + REPORT_WINDOW)
    assert rejected["state"] == "rejected", rejected
    team_sent("dispatch_rejected", 1)
    settled()

    # A4. O agendado sem confirmação vence na abertura (`expired`): o aviso de cancelamento.
    clock.set(exp_opens + timedelta(minutes=1))
    _wait_until(lambda: body("expired")["state"] == "cancelled", REPORT_TIMEOUT, "o agendado não venceu")
    assert body("expired")["cancel_reason"] == "expired"
    team_sent("dispatch_cancelled", 1)
    settled()

    # A5. Bot sem acesso e de volta (token revogado e trocado): o aviso no retorno, pelo bot que voltou.
    rich.revoke(bot)
    _wait_until(lambda: _bot_row(sql, "state, access_problem, lease_owner", bot) == ("no_access", "token_rejected", None),
                REPORT_TIMEOUT, "o bot revogado não ficou sem acesso")
    assert register_bot(client, ADMIN_TOKEN, REPORT_OFFERING, rich.rotate_token(bot))["state"] == "registered"
    _wait_until(lambda: _bot_row(sql, "state, lease_owner, profile_state", bot) == (
        "registered", REPORT_WORKER_ID, "configured"), REPORT_TIMEOUT, "o bot de volta não foi reassumido")
    team_sent("bot_access_restored", 1)
    settled()

    # A6. O 2º oferecimento perde o acesso do bot e é arquivado sem a volta: o aviso `not_sent`, sem mensagem.
    rich.revoke(archive_bot)
    _wait_until(lambda: _bot_row(sql, "state, access_problem, lease_owner", archive_bot) == (
        "no_access", "token_rejected", None), REPORT_TIMEOUT, "o bot do 2º oferecimento não ficou sem acesso")
    closed_archive = client.post(f"/v1/offerings/{REPORT_ARCHIVE_OFFERING}/close", headers=ADMIN)
    assert closed_archive.status_code == 200 and closed_archive.json()["state"] == "closed", closed_archive.text[:300]
    prepared_archive = client.post(f"/v1/offerings/{REPORT_ARCHIVE_OFFERING}/archive/prepare", headers=ADMIN)
    assert prepared_archive.status_code == 200, prepared_archive.text[:300]
    archived = client.post(f"/v1/offerings/{REPORT_ARCHIVE_OFFERING}/archive", headers=ADMIN,
                           json={"confirmation_code": prepared_archive.json()["confirmation_code"]})
    assert archived.status_code == 200 and archived.json()["state"] == "archived", archived.text[:300]
    _wait_until(lambda: notices("bot_access_restored", REPORT_ARCHIVE_OFFERING) == [("not_sent", 0, None, None)],
                REPORT_TIMEOUT, "o aviso do episódio sem volta não ficou not_sent")
    settled()
    bots_stopped = clock.now()
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker da fase de relatórios não parou em 10 s"
    assert worker.error is None and worker.returned

    # ============================== B. Console (worker de console) ==============================
    stream = io.StringIO()
    inbox = console_inbox(db_url, clock=clock)
    runs: list[_CliRun] = []
    try:
        worker_thread(channel=ConsoleChannel(stream), console_routes=True, engine=engine, fetcher=repo_fetcher,
                      delivery=DISPATCH_DELIVERY,
                      settings=dataclasses.replace(settings, worker_id=REPORT_CONSOLE_WORKER_ID))
        member_io = console_student(inbox, bot_id=bot_row, chat_id=member_chat, recipient=member["pseudo_id"],
                                    source=stream)
        student_io = console_student(inbox, bot_id=bot_row, chat_id=TG_DISPATCH_A1.user_id,
                                     recipient=people["a"]["pseudo_id"], source=stream)

        def line_of(io_: Any, message_id: int) -> Any:
            return io_.wait_for(lambda: next((ln for ln in io_.lines() if ln.message_id == message_id), None),
                                timeout=REPORT_TIMEOUT)

        def delivery_message(label: str, role: str, nth: int = 0) -> int:
            """O id da `nth` mensagem enviada do papel na entrega de A no disparo (a ordem da fila)."""
            def ready() -> list[int]:
                return [row[0] for row in sql.execute(
                    "SELECT o.id FROM outbox_messages o JOIN deliveries d ON d.id = o.delivery_id "
                    "JOIN dispatches x ON x.id = d.dispatch_id JOIN enrollments e ON e.id = d.enrollment_id "
                    "WHERE x.dispatch_uuid = %s AND e.person_id = %s AND o.delivery_role = %s "
                    "AND o.state = 'sent' ORDER BY o.id", (dispatches[label], people["a"]["person_id"], role)
                ).fetchall()]
            return _wait_until_value(lambda: (rows := ready()) and len(rows) > nth and rows, REPORT_TIMEOUT,
                                     f"a mensagem {role} #{nth} de A em {label}")[nth]

        # B1. A questão canário: validada (a amostra com a identidade), entregue na prévia ao membro
        # (cabeçalho do imediato sem fechamento informado, a questão e o pedido do campo da escolha
        # degradada), que responde a ela; depois, cancelada.
        canary = prepare("canary", "canary")
        assert canary["state"] == "scheduled", canary
        b_mark = marker(people["b"]["alias"])
        assert canary["preview"]["title"] == f"Questão de {b_mark} (RA {b_mark}, {b_mark})", canary["preview"]
        canary_messages = _wait_until_value(
            lambda: (rows := preview_messages("canary")) and len(rows) == 3 and all(m.state == "sent" for m in rows)
            and rows, REPORT_TIMEOUT, "a prévia da questão canário")
        assert [m.role for m in canary_messages] == [None, "question", "field_prompt"], canary_messages
        member_io.reply(line_of(member_io, canary_messages[1].message_id), REPORT_TEAM_REPLY)
        _wait_until(lambda: sql.execute(
            "SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND chat_id = %s AND outcome = 'preview_replied'",
            (bot_row, member_chat)).fetchone()[0] == 2, REPORT_TIMEOUT, "a resposta à prévia pelo console")
        _wait_until(lambda: sql.execute("SELECT count(*) FROM outbox_messages WHERE text = %s AND state = 'sent'",
                                        (noticetexts.PREVIEW_REPLY,)).fetchone()[0] == 2, REPORT_TIMEOUT,
                    "a resposta fixa à resposta à prévia pelo console")
        cancel_dispatch(client, ADMIN_TOKEN, REPORT_OFFERING, dispatches["canary"])
        settled()

        # B2. Três disparos imediatos para A: a prévia de cada um ao membro e, na abertura, o aviso "aberto".
        labels = ("text", "number", "broken")
        closes = clock.now() + REPORT_WINDOW
        for label, qid in zip(labels, ("text", "number", "broken_on_grade"), strict=True):
            assert prepare(label, qid, closes_at=closes)["state"] == "scheduled"
        for label in labels:
            _wait_until(lambda label=label: (rows := preview_messages(label)) and len(rows) == 3
                        and all(m.state == "sent" for m in rows), REPORT_TIMEOUT, f"a prévia de {label}")
        settled()
        for label in labels:
            confirmed = confirm_dispatch(client, ADMIN_TOKEN, REPORT_OFFERING, dispatches[label])
            assert (confirmed["confirmed"], confirmed["closes_at"], confirmed["target_count"]) == (True, iso(closes), 1)
        _wait_until(lambda: all(counts(label)["delivered"] == 1 for label in labels), REPORT_TIMEOUT,
                    "os três disparos não entregaram a A")
        team_sent("dispatch_opened", 3)
        settled()
        # A responde com a identidade de B: o texto (corrigido, errado) e o número (erro de formato e, depois,
        # a resposta certa); a correção do 3º quebra (falha de questão, com o aviso).
        student_io.reply(line_of(student_io, delivery_message("text", "field_prompt")), DISPATCH_ANSWER)
        _wait_until(lambda: counts("text")["answered"] == 1, REPORT_TIMEOUT, "a resposta de texto não foi corrigida")
        settled()
        student_io.reply(line_of(student_io, delivery_message("number", "field_prompt")), DISPATCH_ANSWER)
        reprompt = delivery_message("number", "field_prompt", 1)
        settled()
        student_io.reply(line_of(student_io, reprompt), "6.5")
        _wait_until(lambda: counts("number")["answered"] == 1, REPORT_TIMEOUT, "a resposta numérica não foi corrigida")
        settled()
        student_io.reply(line_of(student_io, delivery_message("broken", "field_prompt")), "7")
        _wait_until(lambda: counts("broken")["question_failed"] == 1, REPORT_TIMEOUT, "a correção não quebrou")
        team_sent("question_failed", 1)
        settled()
        # O fechamento determinístico: o relógio passa de `closes_at` e, em laço com prazo real, avança
        # `CLOSE_RECHECK_SECONDS` a cada espera sem fechamento; depois, os resumos de fechamento.
        clock.set(closes + timedelta(seconds=1))
        deadline = time.monotonic() + DISPATCH_CLOSE_TIMEOUT
        while not all(body(label)["state"] == "closed" for label in labels):
            wait_until = time.monotonic() + 2.0
            while time.monotonic() < wait_until and not all(body(label)["state"] == "closed" for label in labels):
                time.sleep(0.1)
            if all(body(label)["state"] == "closed" for label in labels):
                break
            assert time.monotonic() < deadline, f"os disparos da fase não fecharam em {DISPATCH_CLOSE_TIMEOUT} s"
            clock.advance(CLOSE_RECHECK_SECONDS)
        team_sent("dispatch_closed", 3)
        settled()

        # B3. O relatório pela API: no momento (JSON e CSV), congelado (a versão 1 e a última), as versões e o
        # reenvio (a versão 2 e o resumo reenviado).
        text_id = dispatches["text"]
        computed = api["get_report"](client, REPORT_OFFERING, text_id)
        assert (computed["report_schema_version"], computed["frozen"], computed["dispatch"]["state"],
                computed["offering"]) == ("1", None, "closed", ref), computed
        csv_now = api["get_report_csv"](client, REPORT_OFFERING, text_id)
        assert csv_now.startswith("﻿section,field,key,metric,on_time,with_late\r\n"), csv_now[:80]
        frozen = api["get_report"](client, REPORT_OFFERING, text_id, version=1)
        assert (frozen["frozen"]["version"], frozen["frozen"]["trigger"]) == (1, "closing"), frozen["frozen"]
        csv_latest = api["get_report_csv"](client, REPORT_OFFERING, text_id, version="latest")
        assert csv_latest.startswith("﻿section,field,key,metric,on_time,with_late\r\n"), csv_latest[:80]
        versions = api["list_report_versions"](client, REPORT_OFFERING, text_id)
        assert [(item["version"], item["trigger"]) for item in versions["items"]] == [(1, "closing")], versions
        resent = api["resend_report_summary"](client, REPORT_OFFERING, text_id)
        assert (resent["dispatch_id"], resent["frozen"]["version"], resent["frozen"]["trigger"]) == (
            text_id, 2, "resend"), resent
        team_sent("summary_resent", 1)
        settled()

        # B4. O resumo diário: ligado, com o horário logo depois do último disparo do dia (com atividade,
        # `queued`); um dia pulado (`missed`); o dia seguinte, sem atividade (`no_activity`).
        due = (clock.now() + REPORT_ACTIVITY_LEAD).astimezone(zone)
        local_time = f"{due.hour:02d}:{due.minute:02d}"
        assert due.date() == clock.now().astimezone(zone).date(), "o resumo do dia cai no mesmo dia local"
        expected_digest = {"offering": ref, "timezone": OFFERING_ZONE, "enabled": True, "local_time": local_time}
        assert api["update_offering_digest"](client, REPORT_OFFERING, enabled=True,
                                             local_time=local_time) == expected_digest
        assert api["get_offering_digest"](client, REPORT_OFFERING) == expected_digest
        first_due = _next_local(clock.now(), local_time, strictly=False)
        day = first_due.astimezone(zone).date()
        clock.set(first_due)
        _wait_until(lambda: notices("daily_digest") == [("queued", 1, None, day.isoformat())], REPORT_TIMEOUT,
                    "o resumo diário do dia não saiu")
        team_sent("daily_digest", 1)
        settled()
        skipped = datetime(day.year, day.month, day.day, 10, 0, tzinfo=zone) + timedelta(days=2)
        clock.set(skipped.astimezone(timezone.utc))
        missed_day = (day + timedelta(days=1)).isoformat()
        _wait_until(lambda: [n[0] for n in notices("daily_digest")] == ["queued", "missed"], REPORT_TIMEOUT,
                    "o resumo do dia pulado não ficou missed")
        settled()
        quiet_due = _next_local(max(_next_local(first_due, local_time, strictly=True), clock.now()), local_time,
                                strictly=True)
        clock.set(quiet_due)
        quiet_day = quiet_due.astimezone(zone).date().isoformat()
        _wait_until(lambda: notices("daily_digest") == [
            ("queued", 1, None, day.isoformat()), ("missed", 0, None, missed_day), ("no_activity", 0, None, quiet_day)],
            REPORT_TIMEOUT, "o resumo do dia sem atividade não ficou no_activity")
        settled()

        # B5. As 5 folhas do CLI: com sucesso (texto, `--csv` e `--json`) e com erro (código 1 e uso
        # incorreto, código 2), no oferecimento da fase.
        def run(args: list[str], exit_code: int, *, json_mode: bool = False, **kwargs: Any) -> Any:
            result = cli_invoke(list(args), json=json_mode, **kwargs)
            leaf = _leaf_of(args)
            assert leaf is not None, f"{args} não é um comando folha do cli"
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                f"{args}: o CLI levantou {type(result.exception).__name__}")
            assert result.exit_code == exit_code, (
                f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
            for text in (result.stdout, result.stderr):
                assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
            if exit_code != 0:
                assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
            runs.append(_CliRun(leaf, tuple(args), json_mode, result.exit_code, result.stdout, result.stderr,
                                result.output))
            return result

        code, number_id = REPORT_OFFERING, dispatches["number"]
        r = run(["reports", "show", code, text_id], 0)
        assert text_id in r.stdout and r.stdout.splitlines()[-1] == "Distribuição e fricção: use --json ou --csv.", (
            r.stdout)
        r = run(["reports", "show", code, text_id, "--csv"], 0)
        assert r.stdout.lstrip("﻿").splitlines()[0] == "section,field,key,metric,on_time,with_late", r.stdout[:80]
        r = run(["reports", "show", code, text_id, "--version", "1"], 0, json_mode=True)
        assert (json.loads(r.stdout)["frozen"]["version"], json.loads(r.stdout)["frozen"]["trigger"]) == (1, "closing")
        r = run(["reports", "versions", code, text_id], 0)
        assert [line.split()[:2] for line in r.stdout.splitlines()[:2]] == [["1", "closing"], ["2", "resend"]], r.stdout
        assert r.stdout.splitlines()[-1] == "Próximo cursor: 2", r.stdout
        r = run(["reports", "resend", code, number_id], 0)
        assert r.stdout.splitlines() == [f"Resumo reenviado: {number_id}, versão 2."], r.stdout
        r = run(["offerings", "digest", "show", code], 0)
        assert r.stdout.splitlines() == [f"Resumo diário de {code}: ligado, às {local_time} ({OFFERING_ZONE})."]
        r = run(["offerings", "digest", "set", code, "--at", REPORT_DIGEST_LATER], 0)
        assert r.stdout.splitlines() == [
            f"Resumo diário de {code}: ligado, às {REPORT_DIGEST_LATER} ({OFFERING_ZONE})."], r.stdout
        r = run(["reports", "show", code, str(uuid.uuid4())], 1)
        assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"], r.stderr
        r = run(["reports", "versions", code, "disparo-invalido"], 1)
        assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - dispatch_id: invalid_format"]
        r = run(["reports", "resend", code, dispatches["canary"]], 1, json_mode=True)
        assert json.loads(r.stderr) == {"error": {"code": "invalid_dispatch_state",
                                                  "message": MESSAGES["invalid_dispatch_state"]}}
        r = run(["offerings", "digest", "show", REPORT_UNKNOWN_OFFERING], 1)
        assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"], r.stderr
        r = run(["offerings", "digest", "set", code, "--at", "24:00"], 1)
        assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - local_time: invalid_format"]
        for args, json_mode in ((["reports", "show", code, text_id, "--csv"], True),
                                (["offerings", "digest", "set", code], False)):
            r = run(args, 2, json_mode=json_mode)
            assert r.stderr.startswith("Uso incorreto:"), (args, r.stderr)
        team_sent("summary_resent", 2)
        settled()
    finally:
        worker_thread.stop()
        inbox.close()
    assert own.protocol_errors() == [], f"desvios de protocolo no motor falso da fase: {own.protocol_errors()}"
    rows = {label: sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s", (dispatch_id,)).fetchone()[0]
            for label, dispatch_id in dispatches.items()}
    return _ReportPhase(offering, offering_row, bot, bot_row, archive_offering, archive_bot, team, people, dispatches,
                        rows, stream, own, runs, started, bots_stopped, tap,
                        {"queued": day.isoformat(), "missed": missed_day, "no_activity": quiet_day}, local_time)


def _wait_until_value(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    """Como `_wait_until`, devolvendo o valor verdadeiro do predicado."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what}: não aconteceu em {timeout} s")
        time.sleep(0.05)


def _check_report_surfaces(exchanges: list[_Exchange], runs: list[_CliRun], lines: list[str], rich: Any, db_url: str,
                       sql: psycopg.Connection, run: _ReportPhase, events: list[dict], secrets: list[str]) -> None:
    """Relatórios: os alvos dos alunos canário do disparo (`DISPATCH_PREFIXES`,
    os mesmos da fase de disparos) em todas as superfícies da fase de relatórios, só nas exceções declaradas,
    cada uma com controle positivo:
    - as das fases anteriores (no banco: `identities`, as colunas internas do chat, o texto digitado pelo aluno nas
      colunas da lista do disparo, os alvos do Telegram de B em `free_messages.text`; nas mensagens ao aluno, o eco
      da própria resposta, só no console ao próprio A; no motor, só o corpo do `grade`);
    - o conteúdo da questão canário nas mensagens da prévia ao membro da equipe (papéis da questão e do pedido
      do campo) e nas colunas da amostra, `dispatch_preview_contents.ir` e `.rendered`, com exatamente os
      rótulos da identidade da questão);
    - a marca de redação do texto livre no lugar da identidade no cabeçalho da prévia (o texto redigido).
    Superfícies: respostas da API (JSON e CSV), saídas do CLI, logs, eventos, o console da fase, os
    parâmetros enviados à Bot API falsa pelos bots da fase e ao motor falso próprio, as tarefas, a fila de
    saída e todas as colunas de todas as tabelas; e a sentinela do gabarito em nenhuma mensagem à equipe, nem
    no relatório, nos eventos ou nos logs (só na amostra, controle positivo)."""
    from tests.helpers.team import team_messages

    canary_labels = _dispatch_labels(DISPATCH_IDENTITY, DISPATCH_B.name)
    list_labels_b = _dispatch_labels(DISPATCH_IDENTITY)
    tg_labels = _dispatch_labels(DISPATCH_TG_IDENTITY)
    typed = _dispatch_labels(DISPATCH_ANSWER, *DISPATCH_FREE_TEXTS)
    eco = _dispatch_labels(DISPATCH_ANSWER)
    assert list_labels_b and canary_labels == list_labels_b | _dispatch_labels(DISPATCH_B.name) and eco <= typed
    member, a_pseudo = run.member, run.pseudo("a")
    report_paths = (f"/v1/offerings/{REPORT_OFFERING}/", f"/v1/offerings/{REPORT_ARCHIVE_OFFERING}/")

    # Respostas da API da fase (inclusive o CSV) e saídas do CLI das folhas da fase: nada.
    mine = [ex for ex in exchanges if ex.path.startswith(report_paths) or ex.path.rstrip("/").endswith(
        (REPORT_OFFERING, REPORT_ARCHIVE_OFFERING))]
    assert mine, "controle contra o vazio: a fase chamou a API"
    assert [(ex.method, ex.path, ex.status, sorted(found)) for ex in mine
            if (found := _dispatch_labels(ex.headers, ex.body, ex.url))] == [], "aluno canário do disparo na API da fase"
    assert [(ex.method, ex.path) for ex in mine if ANSWER_SENTINEL in ex.body] == [], "gabarito na API da fase"
    csv_bodies = [ex.body for ex in mine if ex.path.endswith("/report") and ex.status == 200
                  and ex.body.lstrip("﻿").startswith("section,field,key,metric,on_time,with_late")]
    assert len(csv_bodies) >= 2, "controle contra o vazio: o relatório em CSV (no momento e congelado)"
    assert [(r.args, sorted(found)) for r in runs if r.leaf in REPORT_CLI_LEAVES
            and (found := _dispatch_labels(*r.texts))] == [], "aluno canário do disparo nas folhas do CLI"
    # Logs e eventos: nada (os eventos dos relatórios levam só uuids, contagens, estados e datas).
    assert [(i, sorted(found)) for i, line in enumerate(lines) if (found := _dispatch_labels(line))] == []
    dumped = json.dumps([e for e in events if e["type"] in REPORT_EVENT_TYPES], ensure_ascii=False)
    assert _dispatch_labels(dumped) == set() and ANSWER_SENTINEL not in dumped
    assert all(set(e["data"]).isdisjoint({"pseudo_id", "person", "recipient", "text", "title"})
               for e in events if e["type"] in REPORT_EVENT_TYPES), "evento dos relatórios com pseudônimo ou texto"

    # Parâmetros enviados à Bot API falsa pelos bots da fase: nada (a parte A não leva identidade; a questão
    # canário vai pelo console); controle positivo: a confirmação do toque com `PREVIEW_TAP`.
    phase_bots = {run.bot_id, run.archive_bot_id}
    leaks = [(c.method, c.seq, sorted(found)) for c in rich.calls() if c.bot_id in phase_bots
             and (found := _dispatch_labels(json.dumps(c.params, ensure_ascii=False, default=str)))]
    assert leaks == [], f"aluno canário do disparo nos parâmetros dos bots da fase: {leaks}"
    assert [c.seq for c in rich.calls() if c.bot_id in phase_bots
            and ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False, default=str)] == []
    assert [a.text for a in rich.answers(run.bot_id) if a.callback_id == run.tap.callback_id] == [
        _report_text_value("PREVIEW_TAP")]

    # O console da fase: o conteúdo da questão canário só nas mensagens da prévia dela ao membro (a questão e o
    # pedido do campo), e o eco da resposta de A só nas linhas a A.
    (canary_notice,) = sql.execute(
        "SELECT n.id FROM team_notices n JOIN dispatches d ON d.id = n.dispatch_id "
        "WHERE n.kind = 'dispatch_preview' AND d.dispatch_uuid = %s", (run.dispatches["canary"],)).fetchone()
    canary_messages = team_messages(sql, notice_id=canary_notice)
    content_ids = {m.message_id for m in canary_messages if m.role is not None}
    console = read_lines(run.stream.getvalue())
    console_leaks, content_found, echoed = [], set(), []
    for line in console:
        found = _dispatch_labels(json.dumps(line.raw, ensure_ascii=False))
        if line.recipient == member["pseudo_id"] and line.message_id in content_ids:
            content_found |= found
            allowed = canary_labels
        elif line.recipient == a_pseudo:
            allowed = eco
            if found:
                echoed.append(line.index)
        else:
            allowed = set()
        if found - allowed:
            console_leaks.append((line.index, line.recipient, sorted(found - allowed)))
        assert ANSWER_SENTINEL not in line.text or line.recipient == a_pseudo, (line.index, "gabarito à equipe")
    assert console_leaks == [], f"aluno canário do disparo no console da fase: {console_leaks}"
    assert content_found == canary_labels, f"controle positivo: a questão canário na prévia ({sorted(content_found)})"
    assert echoed, "controle positivo: o eco da resposta de A saiu no console a A"
    (header,) = [m for m in canary_messages if m.role is None]
    b_mark = run.mark("b")
    assert header.text is not None and b_mark in header.text and _dispatch_labels(header.text) == set(), (
        "controle positivo: a marca de redação no lugar da identidade no cabeçalho da prévia")
    assert all(m.text is None for m in canary_messages if m.role is not None), "a forma 3 da fila não guarda texto"

    # O motor falso próprio: só o corpo do `grade` (a resposta crua de A) leva a identidade; nenhum segredo.
    engine_leaks = [(c.op, c.seq, sorted(found)) for c in run.engine.calls()
                    if (found := _dispatch_labels(json.dumps(c.request, ensure_ascii=False)))
                    and not (c.op == "grade" and found <= eco)]
    assert engine_leaks == [], f"aluno canário do disparo nos pedidos ao motor da fase: {engine_leaks}"
    assert any(c.op == "grade" and list_labels_b <= _dispatch_labels(json.dumps(c.request, ensure_ascii=False))
               for c in run.engine.calls()), "controle positivo: a resposta crua de A foi ao motor"
    assert [(c.op, c.seq) for c in run.engine.calls()
            if find_secrets(json.dumps([c.request, c.response], ensure_ascii=False), secrets)] == []

    # Banco: todas as colunas de todas as tabelas, com as exceções exatas (as do disparo e as da amostra).
    def allowed_in(key: tuple[str, str], label: str) -> bool:
        prefix = label.split(":", 1)[0]
        if key[0] == "identities":
            return prefix in DISPATCH_LIST_PREFIXES
        if key in INTERNAL_IDENTITY_COLUMNS:
            return prefix == "disparo-tg-id"
        if key == ("free_messages", "text"):
            return label in tg_labels
        if key in DISPATCH_STUDENT_TEXT_COLUMNS:
            return label in typed
        if key in (("dispatch_preview_contents", "ir"), ("dispatch_preview_contents", "rendered")):
            return label in canary_labels
        return False

    found_db: dict[tuple[str, str], set[str]] = {}
    sample_sentinel: list[tuple[str, str]] = []
    for table, column, text in scan_all_tables(db_url):
        if hits := _dispatch_labels(text):
            found_db.setdefault((table, column), set()).update(hits)
        if table in REPORT_TABLES and ANSWER_SENTINEL in text:
            sample_sentinel.append((table, column))
    outside = {key: sorted(label for label in labels if not allowed_in(key, label)) for key, labels in found_db.items()}
    assert {key: labels for key, labels in outside.items() if labels} == {}, "aluno do disparo fora das exceções"
    assert found_db.get(("dispatch_preview_contents", "ir")) == canary_labels, "controle positivo da amostra (ir)"
    assert found_db.get(("dispatch_preview_contents", "rendered")) == canary_labels, (
        "controle positivo da amostra (rendered)")
    assert set(sample_sentinel) == {("dispatch_preview_contents", "ir")}, (
        f"gabarito nas tabelas dos relatórios fora da amostra: {sample_sentinel}")
    for table in ("dispatch_reports", "team_notices", "offering_digests", "invite_batches", "dispatch_previews"):
        assert [key for key in found_db if key[0] == table] == [], f"aluno canário do disparo em {table}"


def _check_report_notices_and_texts(sql: psycopg.Connection, rich: Any, run: _ReportPhase, api: _Api) -> None:
    """Relatórios: contra o vazio dos avisos (cada um dos 10 tipos e cada estado final, com os avisos da fase
    exatos) e dos textos de `noticetexts` (cada um saiu ao menos uma vez nas mensagens à equipe da fase, na
    confirmação do toque ou é exceção por nome, que não pode ter saído)."""
    from tests.helpers.team import team_messages

    rows = sql.execute("SELECT DISTINCT n.kind, n.state FROM team_notices n").fetchall()
    assert set(REPORT_NOTICE_KINDS) <= {kind for kind, _ in rows}, f"tipo de aviso que não saiu: {sorted(rows)}"
    assert set(REPORT_NOTICE_FINAL_STATES) <= {state for _, state in rows}, f"estado final que não saiu: {sorted(rows)}"
    assert "pending" not in {state for kind, state in rows if kind != "summary_resent"}, rows
    # O único aviso `pending` no fim é o reenvio de `VALID_CALLS` (sem worker depois da fase da API).
    seed_code = api.state.report_offering["code"]
    assert sql.execute(
        "SELECT o.code, n.kind FROM team_notices n JOIN offerings o ON o.id = n.offering_id WHERE n.state = 'pending'"
    ).fetchall() == [(seed_code, "summary_resent")]
    phase = Counter((kind, state, count) for kind, state, count in sql.execute(
        "SELECT n.kind, n.state, n.recipient_count FROM team_notices n JOIN offerings o ON o.id = n.offering_id "
        "WHERE o.code IN (%s, %s)", (REPORT_OFFERING, REPORT_ARCHIVE_OFFERING)).fetchall())
    assert phase == Counter({
        ("dispatch_preview", "queued", 1): 5, ("dispatch_preview", "gave_up", 0): 1,
        ("dispatch_rejected", "queued", 1): 1, ("dispatch_cancelled", "queued", 1): 1,
        ("bot_access_restored", "queued", 1): 1, ("bot_access_restored", "not_sent", 0): 1,
        ("dispatch_opened", "queued", 1): 3, ("question_failed", "queued", 1): 1, ("dispatch_closed", "queued", 1): 3,
        ("summary_resent", "queued", 1): 2, ("daily_digest", "queued", 1): 1, ("daily_digest", "missed", 0): 1,
        ("daily_digest", "no_activity", 0): 1}), phase
    for table in REPORT_TABLES:
        assert table in SQL_SCAN, f"tabela fora de SQL_SCAN: {table}"
        assert sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0, f"tabela vazia: {table}"
    outcomes = {row[0] for row in sql.execute("SELECT DISTINCT outcome FROM telegram_updates").fetchall()}
    assert REPORT_OUTCOMES <= outcomes, f"desfechos que não apareceram: {sorted(REPORT_OUTCOMES - outcomes)}"

    # Os textos: as mensagens à equipe da fase (o texto na fila; as da forma 3 não guardam texto e saem pelo
    # console ou pela Bot API falsa, que a varredura de superfícies cobre), as confirmações e respostas fixas.
    team_texts = [m.text for m in team_messages(sql) if m.text is not None]
    team_texts += [text for chat, text in _bot_outputs(rich, run.bot_id) if chat == run.member["user"].user_id]
    team_texts += [line.text for line in read_lines(run.stream.getvalue())
                   if line.recipient == run.member["pseudo_id"]]
    team_texts += [a.text or "" for a in rich.answers(run.bot_id)]
    team_texts += [row[0] for row in sql.execute(
        "SELECT text FROM outbox_messages WHERE text IS NOT NULL AND reply_to_update_id IS NOT NULL").fetchall()]

    def out(name: str) -> bool:
        value = _report_text_value(name)
        if name == "ACCESS_RESTORED":  # a última linha só sai com resumo de fechamento perdido
            value = value.rsplit("\n", 1)[0]
        if "{" not in value:
            return any(value in text for text in team_texts)
        pattern = _template_regex(value)
        return any(pattern.search(text) for text in team_texts)

    assert [name for name in REPORT_TEXT_NAMES if not out(name)] == [], "texto de noticetexts que não saiu"
    assert [name for name in REPORT_TEXT_EXCEPTIONS if out(name)] == [], (
        "exceção declarada que saiu (a exceção deixa de valer)")
    untitled = [text for text in team_texts if _report_text_value("UNTITLED") in text]
    rejected_pattern = _template_regex(_report_text_value("REJECTED"))
    assert untitled and all(rejected_pattern.search(text) and "unsupported@" in text for text in untitled), (
        "questão sem título fora do aviso de rejeição do A3", untitled)


def _generic_db_scan(db_url: str) -> dict[tuple[str, str], set[str]]:
    """a varredura genérica (`scan_all_tables`) com todos os alvos, das pessoas da API base e dos remetentes do
    Telegram: `(tabela, coluna)` → rótulos achados."""
    hits: dict[tuple[str, str], set[str]] = {}
    for table, column, text in scan_all_tables(db_url):
        found = find_canary(text, prefixes=ALL_PREFIXES)
        if found:
            hits.setdefault((table, column), set()).update(found)
    return hits


def _telegram_params_hits(bot_api: Any) -> list[tuple[str, int, list[str]]]:
    """alvos canário (das pessoas da API base e do Telegram) nos parâmetros enviados à Bot API falsa; só o `chat_id` do
    `sendMessage` (o destino, identidade interna) fica de fora."""
    leaks = []
    for call in bot_api.calls():
        params = dict(call.params)
        if call.method == "sendMessage":
            params.pop("chat_id", None)
        found = find_canary(json.dumps(params, ensure_ascii=False), prefixes=ALL_PREFIXES)
        if found:
            leaks.append((call.method, call.seq, found))
    return leaks


def _no_prompt(label: str) -> str:
    raise AssertionError(f"o canário roda fora de terminal: prompt_secret({label!r}) não pode ser chamado")


def _valid_ra_calls(seed: _Seed) -> tuple[CanaryCall, ...]:
    """Pseudônimos de formato válido que carregam o RA canário (RA_ALIAS e RA_UUID).

    Passam pela validação e chegam ao domínio: people.get e tokens.revoke dão 404; a revelação
    concedida devolve os dois em `not_found`; a negada (token só de eventos, para não mexer no
    `identity.reveal_denied` semeado) dá 403. Um log que registre o `ref` não encontrado ou os itens
    de `not_found` é pego pela varredura de logs, da saída do processo e do banco.
    """

    def reveal(headers: dict) -> Callable[[Any, CallState], Any]:
        return lambda c, s: c.post("/v1/identity/reveal", headers=headers, json={
            "pseudonyms": [s.person["alias"], RA_ALIAS, RA_UUID], "reason": VALID_RA_REASON})

    return (
        CanaryCall("people.get", "path_valid_ra", lambda c, s: c.get(f"/v1/people/{RA_ALIAS}", headers=ADMIN)),
        CanaryCall("tokens.revoke", "path_valid_ra", lambda c, s: c.post(
            f"/v1/tokens/{RA_UUID}/revoke", headers=ADMIN)),
        CanaryCall("identity.reveal", "body_valid_ra", reveal(ADMIN)),
        CanaryCall("identity.reveal", "body_valid_ra_denied", reveal(auth(seed.events_secret))),
    )


def _check_canary_response(op_id: str, surface: str, resp: Any, clock: Any, seed: _Seed,
                           state: CallState) -> None:
    expected = EXPECTED_CANARY[(op_id, surface)]
    if expected == _REVEALED_WITH_NOT_FOUND:
        # Revelação concedida: o corpo é a saída permitida da identidade e fica fora da checagem de
        # eco e da varredura; os logs, a saída do processo e o banco continuam varridos.
        assert resp.status_code == 200, f"{op_id}/{surface}: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert_matches("identity.reveal", 200, body)
        assert body == {
            "people": [{"pseudo_id": state.person["pseudo_id"], "alias": state.person["alias"],
                        **SUPPORT_IDENTITY}],
            "not_found": [RA_ALIAS, RA_UUID]}
        return
    assert_no_echo(resp, *ECHO_SENTINELS)
    if expected == _INVALID_CONFIRMATION:
        # O código canário de formato válido é recusado sem eco, sem `fields`.
        assert_error(resp, 409, "invalid_confirmation")
        return
    if expected == _CREATED_OFFERING_REDACTED:
        # A disciplina e o período canário saem redigidos.
        assert resp.status_code == 201, f"{op_id}/{surface}: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert_matches("offerings.create", 201, body)
        assert re.fullmatch(UUID4_RE, body["offering_id"]), body["offering_id"]
        assert re.fullmatch(r"oferta-sonda-[0-9]+", body["code"]), body["code"]
        assert body == {
            "offering_id": body["offering_id"], "code": body["code"], "course": seed.expected["course"],
            "term": seed.expected["term"], "timezone": "America/Sao_Paulo", "response_window_minutes": 1440,
            "state": "draft", "created_at": iso(clock.now()), "activated_at": None, "closed_at": None,
            "archived_at": None, "bot": None,
            "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` com as mensagens livres e o contato
        return
    if expected == _CREATED_WITH_REDACTED_LABEL:
        assert resp.status_code == 201, f"{op_id}/{surface}: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        assert_matches("tokens.create", 201, body)
        z_mark = marker(seed.alias(ZEFIRO))
        assert {k: v for k, v in body.items() if k not in ("token_id", "secret")} == {
            "label": f"token de {z_mark} (RA {z_mark})", "permissions": ["people:read"],
            "created_at": iso(clock.now()), "revoked_at": None}
        return
    if expected == _HEALTH_OK:
        assert resp.status_code == 200, f"{op_id}/{surface}: {resp.status_code} {resp.text[:300]}"
        assert_matches("system.health", 200, resp.json())
        assert resp.json()["status"] == "ok"
        return
    if expected == _OPENAPI_OK:
        assert resp.status_code == 200, f"{op_id}/{surface}: {resp.status_code} {resp.text[:300]}"
        assert isinstance(resp.json()["paths"], dict)
        return
    status, code, extra = expected
    if status in (400, 409):
        assert_error(resp, status, code, fields=extra)
    elif status == 403:
        assert_error(resp, status, code, permission=extra)
    else:
        assert extra is None, f"{op_id}/{surface}: extra inesperado para {status}"
        assert_error(resp, status, code)


@dataclass
class _Api:
    state: CallState
    events: list[dict]
    revoked_token_id: str
    at: datetime


def _api_phase(client: Any, clock: Any, seed: _Seed, bot_api: Any) -> _Api:
    """API: VALID_CALLS (sucesso e 403), /health, /openapi.json, CANARY_CALLS, os pseudônimos de
    formato válido com o RA canário (_valid_ra_calls) e events.list."""
    at = clock.now()
    state = seed_state(client, bot_api=bot_api)
    revoked_token_id = state.revoke_targets[0]  # o alvo que VALID_CALLS["tokens.revoke"] usa
    for op_id, call in VALID_CALLS.items():
        resp = call(client, state, ADMIN)
        status = SUCCESS_STATUS.get(op_id, 200)
        assert resp.status_code == status, f"{op_id}: {resp.status_code} {resp.text[:300]}"
        assert_matches(op_id, status, resp.json())
        if op_id == "tokens.revoke":
            assert (resp.json()["token_id"], resp.json()["revoked_at"]) == (revoked_token_id, iso(at))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "version": cq_server.__version__, "database": "ok",
                             "schema_version": EXPECTED_VERSION, "expected_schema_version": EXPECTED_VERSION}
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200
    assert isinstance(openapi.json()["paths"], dict)

    # Erro de permissão em toda operação /v1: um token sem a permissão exigida.
    for op in load_catalog()["operations"]:
        if op["permission"] is None:
            continue
        token = seed.read_secret if op["permission"] == "events:read" else seed.events_secret
        resp = VALID_CALLS[op["id"]](client, state, auth(token))
        assert_error(resp, 403, "forbidden", permission=op["permission"])

    executed: list[tuple[str, str]] = []
    for canary in (*CANARY_CALLS, *_valid_ra_calls(seed)):
        resp = canary.call(client, state)
        _check_canary_response(canary.op_id, canary.surface, resp, clock, seed, state)
        executed.append((canary.op_id, canary.surface))
    assert Counter(executed) == Counter(EXPECTED_CANARY.keys()), (
        "CANARY_CALLS + _valid_ra_calls e EXPECTED_CANARY divergem")

    events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)
    return _Api(state, events, revoked_token_id, at)


def _check_seeded_events(api: _Api, seed: _Seed, work: _Work) -> None:
    """Contra o vazio: os 7 tipos saíram, e os 4 textos livres saem redigidos com marker(alias)."""
    events = api.events
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "seq não cresce estritamente"
    by_type: dict[str, list[dict]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event)
    assert set(by_type) == set(EVENT_TYPES), f"tipos devolvidos: {sorted(by_type)}"

    zefiro = _only([e for e in by_type["person.registered"] if e["data"]["pseudo_id"] == seed.pseudo(ZEFIRO)],
                   "person.registered da pessoa canário")
    assert zefiro == {"seq": zefiro["seq"], "type": "person.registered", "occurred_at": iso(seed.started),
                      "actor_token_id": "bootstrap",
                      "data": {"pseudo_id": seed.pseudo(ZEFIRO), "alias": seed.alias(ZEFIRO)}}

    label = _only([e for e in by_type["token.created"] if e["data"]["token_id"] == seed.label_token["token_id"]],
                  "token.created do rótulo semeado")
    assert label == {"seq": label["seq"], "type": "token.created", "occurred_at": iso(seed.started),
                     "actor_token_id": "bootstrap",
                     "data": {"token_id": seed.label_token["token_id"], "label": seed.expected["label"],
                              "permissions": ["people:read"]}}

    revealed_ids = [seed.pseudo(p) for p in CANARY_PEOPLE]
    revealed = _only([e for e in by_type["identity.revealed"] if e["data"]["revealed"] == revealed_ids],
                     "identity.revealed da justificativa semeada")
    assert revealed == {"seq": revealed["seq"], "type": "identity.revealed", "occurred_at": iso(seed.started),
                        "actor_token_id": "bootstrap",
                        "data": {"client": "other", "revealed": revealed_ids, "not_found_count": 0,
                                 "reason": seed.expected["reason"]}}

    denied = _only([e for e in by_type["identity.reveal_denied"]
                    if e["actor_token_id"] == seed.read_token["token_id"]], "identity.reveal_denied semeado")
    assert denied == {"seq": denied["seq"], "type": "identity.reveal_denied", "occurred_at": iso(seed.started),
                      "actor_token_id": seed.read_token["token_id"],
                      "data": {"client": "other", "requested": [seed.pseudo(ZEFIRO), seed.pseudo(URSULA)],
                               "not_found_count": 0}}

    # _valid_ra_calls: os itens não encontrados (que carregam o RA) entram só pela contagem.
    support = api.state.person["pseudo_id"]
    ra_revealed = _only([e for e in by_type["identity.revealed"] if e["data"]["not_found_count"] == 2],
                        "identity.revealed dos pseudônimos com RA")
    assert ra_revealed == {"seq": ra_revealed["seq"], "type": "identity.revealed", "occurred_at": iso(api.at),
                           "actor_token_id": "bootstrap",
                           "data": {"client": "other", "revealed": [support], "not_found_count": 2,
                                    "reason": VALID_RA_REASON}}
    ra_denied = _only([e for e in by_type["identity.reveal_denied"] if e["data"]["not_found_count"] == 2],
                      "identity.reveal_denied dos pseudônimos com RA")
    assert ra_denied == {"seq": ra_denied["seq"], "type": "identity.reveal_denied", "occurred_at": iso(api.at),
                         "actor_token_id": seed.events_token["token_id"],
                         "data": {"client": "other", "requested": [support], "not_found_count": 2}}

    task_failures = {e["data"]["task_id"]: e for e in by_type["task.failed"]}
    assert task_failures[work.t1] == {
        "seq": task_failures[work.t1]["seq"], "type": "task.failed", "occurred_at": iso(work.started),
        "actor_token_id": None, "data": {"task_id": work.t1, "task_type": "privacy.direct_fail", "attempts": 1,
                                         "error": seed.expected["task_error"]}}
    assert task_failures[work.t2] == {
        "seq": task_failures[work.t2]["seq"], "type": "task.failed", "occurred_at": iso(work.started),
        "actor_token_id": None, "data": {"task_id": work.t2, "task_type": "privacy.permanent", "attempts": 1,
                                         "error": "PermanentTaskError"}}

    outbox_failures = {e["data"]["message_id"]: e for e in by_type["outbox.failed"]}
    assert outbox_failures[work.m1] == {
        "seq": outbox_failures[work.m1]["seq"], "type": "outbox.failed", "occurred_at": iso(work.started),
        "actor_token_id": None, "data": {"message_id": work.m1, "recipient": seed.pseudo(ZEFIRO), "attempts": 1,
                                         "error": seed.expected["outbox_reason"]}}
    assert outbox_failures[work.m2] == {
        "seq": outbox_failures[work.m2]["seq"], "type": "outbox.failed", "occurred_at": iso(work.m2_failed_at),
        "actor_token_id": None, "data": {"message_id": work.m2, "recipient": seed.pseudo(URSULA), "attempts": 3,
                                         "error": "RuntimeError"}}
    revoked = _only([e for e in by_type["token.revoked"] if e["data"]["token_id"] == api.revoked_token_id],
                    "token.revoked de VALID_CALLS")
    assert revoked == {"seq": revoked["seq"], "type": "token.revoked", "occurred_at": iso(api.at),
                       "actor_token_id": "bootstrap", "data": {"token_id": api.revoked_token_id}}


def _cli_phase(cli_invoke: Any, client: Any, clock: Any, seed: _Seed, state: CallState,
               tmp_path: Any, bot_api: Any, bot_run: _BotPhase) -> list[_CliRun]:
    """CLI: todas as folhas com sucesso e com erro, sempre fora de terminal."""
    runs: list[_CliRun] = []

    def run(args: list[str], exit_code: int, *, json_mode: bool = False, **kwargs: Any) -> Any:
        result = cli_invoke(list(args), json=json_mode, **kwargs)
        leaf = _leaf_of(args)
        assert leaf is not None, f"{args} não é um comando folha do cli"
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{args}: o CLI levantou {type(result.exception).__name__}")
        assert result.exit_code == exit_code, (
            f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
        for text in (result.stdout, result.stderr):
            assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
        if exit_code != 0:
            assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
        runs.append(_CliRun(leaf, tuple(args), json_mode, result.exit_code, result.stdout, result.stderr,
                            result.output))
        return result

    def unreachable(args: list[str], *, json_mode: bool = False) -> Any:
        return run(args, 3, json_mode=json_mode, http=None, env={"CQ_API_URL": UNREACHABLE_API})

    z_alias, z_pseudo = seed.alias(ZEFIRO), seed.pseudo(ZEFIRO)
    type_args = [arg for t in EVENT_TYPES for arg in ("--type", t)]
    secret_files = iter(tmp_path / f"segredo-{i}.txt" for i in range(100))

    # --- sucesso (texto e --json) ---
    r = run(["health"], 0)
    assert r.stdout.splitlines() == [f"API no ar: versão {cq_server.__version__}, esquema {EXPECTED_VERSION}."]
    r = run(["health"], 0, json_mode=True)
    assert json.loads(r.stdout) == {"status": "ok", "version": cq_server.__version__, "database": "ok",
                                    "schema_version": EXPECTED_VERSION, "expected_schema_version": EXPECTED_VERSION}

    r = run(["people", "list"], 0, json_mode=True)
    people_page = client.get("/v1/people", headers=ADMIN).json()
    assert json.loads(r.stdout) == people_page
    r = run(["people", "list"], 0)
    assert r.stdout.splitlines() == [f"{p['alias']}  {p['pseudo_id']}  {p['created_at']}"
                                     for p in people_page["items"]] + [
        f"Próximo cursor: {people_page['next_cursor']}"]

    r = run(["people", "show", z_alias], 0)
    assert r.stdout.splitlines() == [f"{z_alias}  {z_pseudo}  {iso(seed.started)}"]
    r = run(["people", "show", z_alias], 0, json_mode=True)
    assert json.loads(r.stdout) == seed.people[ZEFIRO.name]

    reveal_args = ["identity", "reveal", *seed.reveal_refs(), "--reason", f"conferencia pelo cli de {OLIMPIO.name}"]
    r = run(reveal_args, 0)
    assert r.stdout.splitlines() == seed.reveal_lines()
    r = run(reveal_args, 0, json_mode=True)
    assert json.loads(r.stdout) == seed.reveal_body()

    text_file = next(secret_files)
    r = run(["tokens", "create", "--label", f"cli de {ZEFIRO.name}", "--permission", "people:read",
             "--secret-file", str(text_file)], 0)
    lines = r.stdout.splitlines()
    assert len(lines) == 2 and re.fullmatch(rf"Token criado: {UUID4_RE} \(people:read\)\.", lines[0]), lines
    assert lines[1] == f"Segredo gravado em {text_file}."
    _remember_secret_file(text_file)
    json_file = next(secret_files)
    r = run(["tokens", "create", "--label", f"cli de {ZEFIRO.name}", "--permission", "people:read",
             "--secret-file", str(json_file)], 0, json_mode=True)
    created = json.loads(r.stdout)
    assert set(created) == {"token_id", "label", "permissions", "created_at", "revoked_at", "secret_file"}
    assert (created["label"], created["permissions"], created["created_at"], created["revoked_at"],
            created["secret_file"]) == (f"cli de {marker(z_alias)}", ["people:read"], iso(clock.now()), None,
                                        str(json_file))
    _remember_secret_file(json_file)

    r = run(["tokens", "list"], 0, json_mode=True)
    tokens_page = client.get("/v1/tokens", headers=ADMIN).json()
    assert json.loads(r.stdout) == tokens_page
    r = run(["tokens", "list"], 0)
    assert r.stdout.splitlines() == [
        f"{t['token_id']}  {t['label']}  {','.join(t['permissions'])}  {t['created_at']}  {t['revoked_at'] or 'ativo'}"
        for t in tokens_page["items"]]

    target = state.revoke_targets.pop()
    r = run(["tokens", "revoke", target], 0)
    assert r.stdout.splitlines() == [f"Token revogado: {target} em {iso(clock.now())}."]
    target = state.revoke_targets.pop()
    r = run(["tokens", "revoke", target], 0, json_mode=True)
    revoked = json.loads(r.stdout)
    assert_matches("tokens.revoke", 200, revoked)
    assert (revoked["token_id"], revoked["revoked_at"]) == (target, iso(clock.now()))

    r = run(["events", "list", *type_args], 0, json_mode=True)
    events_page = list_events(client, ADMIN_TOKEN, *EVENT_TYPES)
    assert json.loads(r.stdout) == events_page
    r = run(["events", "list", *type_args], 0)
    assert r.stdout.splitlines() == [f"{e['seq']}  {e['occurred_at']}  {e['type']}" for e in events_page["items"]] + [
        f"Próximo cursor: {events_page['next_cursor']}"]

    # --- oferecimentos e bots: as 10 folhas com sucesso (texto e --json) ---
    now = iso(clock.now())
    u_alias = seed.alias(URSULA)
    cli_course = f"Curso de {ZEFIRO.name}"  # canário na disciplina: sai redigido
    redacted_course = f"Curso de {marker(z_alias)}"
    r = run(["offerings", "create", CLI_OFFERING, "--course", cli_course, "--term", "2031s2",
             "--timezone", "America/Sao_Paulo"], 0)
    text_offering = client.get(f"/v1/offerings/{CLI_OFFERING}", headers=ADMIN).json()
    assert r.stdout.splitlines() == [
        f"Oferecimento cadastrado: {CLI_OFFERING} ({text_offering['offering_id']}), em rascunho."]
    assert re.fullmatch(UUID4_RE, text_offering["offering_id"]), text_offering
    assert text_offering == {
        "offering_id": text_offering["offering_id"], "code": CLI_OFFERING, "course": redacted_course,
        "term": "2031s2", "timezone": "America/Sao_Paulo", "response_window_minutes": 1440, "state": "draft",
        "created_at": now, "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
        "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` com as mensagens livres e o contato
    r = run(["offerings", "create", CLI_OFFERING_JSON, "--course", cli_course, "--term", "2031s2",
             "--timezone", "UTC", "--response-window", "60"], 0, json_mode=True)
    json_offering = json.loads(r.stdout)
    assert_matches("offerings.create", 201, json_offering)
    assert json_offering == {
        "offering_id": json_offering["offering_id"], "code": CLI_OFFERING_JSON, "course": redacted_course,
        "term": "2031s2", "timezone": "UTC", "response_window_minutes": 60, "state": "draft",
        "created_at": now, "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
        "free_messages": False, "contact_email_set": False, "purged_at": None}  # o `Offering` com as mensagens livres e o contato
    assert client.get(f"/v1/offerings/{CLI_OFFERING_JSON}", headers=ADMIN).json() == json_offering

    offerings_page = client.get("/v1/offerings", headers=ADMIN).json()
    r = run(["offerings", "list"], 0, json_mode=True)
    assert json.loads(r.stdout) == offerings_page
    r = run(["offerings", "list"], 0)
    assert r.stdout.splitlines() == [f"{o['code']}  {o['state']}  {o['course']}  {o['term']}"
                                     for o in offerings_page["items"]] + [
        f"Próximo cursor: {offerings_page['next_cursor']}"]
    r = run(["offerings", "show", CLI_OFFERING], 0)
    assert r.stdout.splitlines() == [f"{CLI_OFFERING}  draft  {redacted_course}  2031s2  bot: -"]
    r = run(["offerings", "show", text_offering["offering_id"]], 0, json_mode=True)
    assert json.loads(r.stdout) == text_offering

    # bots register: token só por arquivo (fora de terminal), com espaços e quebras nas pontas.
    text_bot, json_bot = bot_api.add_bot(), bot_api.add_bot()
    text_user, json_user = bot_api.state(text_bot).username, bot_api.state(json_bot).username
    text_token_file, json_token_file = tmp_path / "token-texto.txt", tmp_path / "token-json.txt"
    text_token_file.write_text(f"  {bot_api.token_of(text_bot)} \n\n", encoding="utf-8")
    json_token_file.write_text(bot_api.token_of(json_bot) + "\n", encoding="utf-8")
    r = run(["bots", "register", CLI_OFFERING, "--token-file", str(text_token_file)], 0)
    assert r.stdout.splitlines() == [f"Bot registrado: @{text_user} no oferecimento {CLI_OFFERING} (perfil: pending)."]
    r = run(["bots", "register", CLI_OFFERING_JSON, "--token-file", str(json_token_file)], 0, json_mode=True)
    json_registered = json.loads(r.stdout)
    assert_matches("bots.register", 200, json_registered)
    assert json_registered == {
        "offering": {"offering_id": json_offering["offering_id"], "code": CLI_OFFERING_JSON},
        "username": json_user, "telegram_bot_id": json_bot, "state": "registered", "profile_state": "pending",
        "registered_at": now, "token_replaced_at": None, "lease": None, "last_poll_ok_at": None,
        "problem": None}

    r = run(["bots", "activate", CLI_OFFERING], 0)
    assert r.stdout.splitlines() == [f"{CLI_OFFERING}  active  {redacted_course}  2031s2  bot: {text_user}"]
    r = run(["bots", "activate", CLI_OFFERING_JSON], 0, json_mode=True)
    json_active = {**json_offering, "state": "active", "activated_at": now,
                   "bot": {"username": json_user, "telegram_bot_id": json_bot, "state": "registered",
                           "profile_state": "pending"}}
    assert json.loads(r.stdout) == json_active

    r = run(["bots", "show", CLI_OFFERING], 0)
    assert r.stdout.splitlines() == [
        f"@{text_user}  registered  perfil: pending  posse: nenhuma  última consulta: -"]
    r = run(["bots", "show", BOT_OFFERING], 0)
    assert r.stdout.splitlines() == [
        f"@{bot_run.username}  no_access  perfil: configured  posse: nenhuma  "
        f"última consulta: {iso(bot_run.resumed)}",
        f"Problema: {TOKEN_REJECTED_MESSAGE}"]
    r = run(["bots", "show", CLI_OFFERING_JSON], 0, json_mode=True)
    assert json.loads(r.stdout) == json_registered

    # team link: fora de terminal, só com --secret-file; o link vai para o arquivo, nunca para a saída.
    link_file = next(secret_files)
    r = run(["team", "link", CLI_OFFERING, "--person", u_alias, "--secret-file", str(link_file)], 0,
            json_mode=True)
    assert json.loads(r.stdout) == {
        "expires_at": iso(clock.now() + TEAM_LINK_VALIDITY),
        "person": {"pseudo_id": seed.pseudo(URSULA), "alias": u_alias},
        "offering": {"offering_id": text_offering["offering_id"], "code": CLI_OFFERING,
                     "timezone": "America/Sao_Paulo"},
        "secret_file": str(link_file)}
    _remember_link_file(link_file, text_user)

    r = run(["offerings", "close", CLI_OFFERING], 0)
    assert r.stdout.splitlines() == [f"{CLI_OFFERING}  closed  {redacted_course}  2031s2  bot: {text_user}"]
    r = run(["offerings", "close", CLI_OFFERING_JSON], 0, json_mode=True)
    assert json.loads(r.stdout) == {**json_active, "state": "closed", "closed_at": now}

    # Arquivamento em duas fases de dois alvos `closed` de `seed_state` (código novo a cada preparo).
    text_target, _ = state.archive_targets.pop()
    json_target, _ = state.archive_targets.pop()
    expires = iso(clock.now() + ARCHIVE_CODE_VALIDITY)
    r = run(["offerings", "archive", "prepare", text_target], 0)
    lines = r.stdout.splitlines()
    assert len(lines) == 2 and lines[0] == f"Arquivar {text_target}: 0 na equipe, 0 links pendentes.", lines
    confirmation = re.fullmatch(rf"Código de confirmação \(válido até {re.escape(expires)}\): ([A-Za-z0-9_-]{{22}})",
                                lines[1])
    assert confirmation is not None, lines[1]
    json_target_offering = client.get(f"/v1/offerings/{json_target}", headers=ADMIN).json()
    r = run(["offerings", "archive", "prepare", json_target], 0, json_mode=True)
    prepared = json.loads(r.stdout)
    assert_matches("offerings.prepare_archive", 200, prepared)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", prepared["confirmation_code"]), prepared
    assert prepared == {"offering": json_target_offering, "team_members_active": 0, "pending_links": 0,
                        "confirmation_code": prepared["confirmation_code"], "expires_at": expires}
    r = run(["offerings", "archive", "confirm", text_target, "--confirmation", confirmation.group(1)], 0)
    assert r.stdout.splitlines() == [f"Oferecimento arquivado: {text_target} em {now}."]
    r = run(["offerings", "archive", "confirm", json_target, "--confirmation", prepared["confirmation_code"]], 0,
            json_mode=True)
    assert json.loads(r.stdout) == {**json_target_offering, "state": "archived", "archived_at": now}

    # Por último, para não mudar o desempate da redação nas saídas acima: nome repetido é aceito.
    r = run(["people", "add", "--name", ZEFIRO.name], 0)
    assert re.fullmatch(rf"Pessoa cadastrada: {ALIAS_RE} \({UUID4_RE}\)\.", r.stdout.strip()), r.stdout
    r = run(["people", "add", "--name", URSULA.name], 0, json_mode=True)
    added = json.loads(r.stdout)
    assert_matches("people.register", 201, added)
    assert added["created_at"] == iso(clock.now())

    # --- erro da API (código 1), com valores canário na entrada ---
    identity_args = ["people", "add", "--name", ZEFIRO.name, "--ra", ZEFIRO.ra, "--email", ZEFIRO.email]
    r = run(identity_args, 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['conflict']}", "  - ra: duplicate", "  - email: duplicate"]
    r = run(identity_args, 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "conflict", "message": MESSAGES["conflict"],
                                              "fields": [{"field": "ra", "reason": "duplicate"},
                                                         {"field": "email", "reason": "duplicate"}]}}
    r = run(["people", "list"], 1, token=CANARY_ASCII)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['unauthenticated']}"]
    r = run(["people", "show", ZEFIRO.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - ref: invalid_format"]
    # Pseudônimo de formato válido que carrega o RA canário: chega ao domínio (404, 403).
    r = run(["people", "show", RA_ALIAS], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["people", "show", RA_ALIAS], 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "not_found", "message": MESSAGES["not_found"]}}
    r = run(["identity", "reveal", RA_ALIAS, RA_UUID, "--reason", "motivo qualquer"], 1, token=seed.events_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: identity:reveal"]
    r = run(["identity", "reveal", z_alias, "--reason", f"motivo com {ZEFIRO.name}"], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: identity:reveal"]
    r = run(["identity", "reveal", ZEFIRO.ra, "--reason", "motivo qualquer"], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - pseudonyms[0]: invalid_format"]
    r = run(["tokens", "create", "--label", f"rotulo de {ZEFIRO.name}", "--permission", "people:read",
             "--secret-file", str(next(secret_files))], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: tokens:manage"]
    r = run(["tokens", "list"], 1, token=ZEFIRO.ra)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['unauthenticated']}"]
    r = run(["tokens", "revoke", str(uuid.uuid4())], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["tokens", "revoke", RA_UUID], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["tokens", "revoke", ZEFIRO.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - token_id: invalid_format"]
    r = run(["events", "list", "--type", "person.registered"], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: events:read"]
    # Oferecimentos e bots: as folhas com erro da API, com valores canário na entrada.
    ra_code = ["offerings", "create", f"oferta-{ZEFIRO.ra}", "--course", "Curso qualquer", "--term", "2031s2",
               "--timezone", "America/Sao_Paulo"]
    r = run(ra_code, 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - code: invalid_value"]
    r = run(ra_code, 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "invalid_request", "message": MESSAGES["invalid_request"],
                                              "fields": [{"field": "code", "reason": "invalid_value"}]}}
    r = run(["offerings", "list"], 1, token=seed.read_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]
    r = run(["offerings", "show", ZEFIRO.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["offerings", "close", state.register_offering["code"]], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_state']}", "Estado atual: draft"]
    r = run(["offerings", "archive", "prepare", BOT_OFFERING], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_state']}", "Estado atual: active"]
    r = run(["offerings", "archive", "prepare", BOT_OFFERING], 1, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "invalid_state", "message": MESSAGES["invalid_state"],
                                              "state": "active"}}
    r = run(["offerings", "archive", "confirm", CLI_OFFERING, "--confirmation", ZEFIRO.name], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_confirmation']}"]
    remember_secret(UNKNOWN_BOT_TOKEN)
    unknown_token_file = tmp_path / "token-desconhecido.txt"
    unknown_token_file.write_text(UNKNOWN_BOT_TOKEN + "\n", encoding="utf-8")
    r = run(["bots", "register", state.register_offering["code"], "--token-file", str(unknown_token_file)], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - token: invalid_value"]
    r = run(["bots", "activate", ZEFIRO.ra], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['not_found']}"]
    r = run(["bots", "show", CLI_OFFERING], 1, token=seed.events_secret)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['forbidden']}", "Permissão necessária: offerings:read"]
    r = run(["team", "link", CLI_OFFERING, "--person", ZEFIRO.ra, "--secret-file", str(next(secret_files))], 1)
    assert r.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - person: invalid_format"]

    # --- API inalcançável (código 3), uma vez por folha ---
    unreachable_calls = [
        ["health"],
        identity_args,
        ["people", "list"],
        ["people", "show", z_alias],
        ["identity", "reveal", z_alias, "--reason", f"motivo com {ZEFIRO.name}"],
        ["tokens", "create", "--label", f"rotulo de {ZEFIRO.name}", "--permission", "people:read",
         "--secret-file", str(next(secret_files))],
        ["tokens", "list"],
        ["tokens", "revoke", str(uuid.uuid4())],
        ["events", "list", "--type", "person.registered"],
        # as folhas de oferecimentos e bots
        ["offerings", "create", CLI_OFFERING_UNREACHABLE, "--course", cli_course, "--term", "2031s2",
         "--timezone", "America/Sao_Paulo"],
        ["offerings", "list"],
        ["offerings", "show", CLI_OFFERING],
        ["offerings", "close", CLI_OFFERING],
        ["offerings", "archive", "prepare", CLI_OFFERING],
        ["offerings", "archive", "confirm", CLI_OFFERING, "--confirmation", ZEFIRO.name],
        ["bots", "register", CLI_OFFERING, "--token-file", str(unknown_token_file)],
        ["bots", "activate", CLI_OFFERING],
        ["bots", "show", CLI_OFFERING],
        ["team", "link", CLI_OFFERING, "--person", z_alias, "--secret-file", str(next(secret_files))],
    ]
    for args in unreachable_calls:
        r = unreachable(args)
        assert r.stderr.splitlines() == [f"Erro: {UNREACHABLE_MESSAGE}"], (args, r.stderr)
    r = unreachable(identity_args, json_mode=True)
    assert json.loads(r.stderr) == {"error": {"code": "api_unreachable", "message": UNREACHABLE_MESSAGE}}

    # --- uso incorreto (código 2), com o valor canário em opção tipada, argumento e nome de opção ---
    usage_calls = [
        ["people", "list", "--limit", ZEFIRO.name],
        ["events", "list", "--type", "person.registered", "--limit", ZEFIRO.name],
        ["people", "list", ZEFIRO.name],
        ["tokens", "revoke", state.token_id, ZEFIRO.ra],
        ["people", "show", z_alias, ZEFIRO.ra],
        *[[*leaf.split(" "), UNKNOWN_OPTION] for leaf in sorted(_cli_leaves(cq_cli.cli))],
        # opções tipadas e argumento a mais nas folhas de oferecimentos e bots
        ["offerings", "list", "--limit", ZEFIRO.name],
        ["offerings", "create", "cli-uso-incorreto", "--course", "Curso", "--term", "2031s2", "--timezone", "UTC",
         "--response-window", ZEFIRO.name],
        ["team", "link", CLI_OFFERING, "--person", z_alias, "--valid-hours", ZEFIRO.name,
         "--secret-file", str(next(secret_files))],
        ["offerings", "show", CLI_OFFERING, ZEFIRO.ra],
    ]
    for args in usage_calls:
        r = run(args, 2)
        assert r.stderr.startswith("Uso incorreto:"), (args, r.stderr)
    r = run(["tokens", "create", "--label", ZEFIRO.name, "--permission", "people:read"], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: fora de um terminal, informe --secret-file."]
    # Oferecimentos e bots (token do bot e link de equipe): sem terminal, sem arquivo → código 2.
    r = run(["bots", "register", CLI_OFFERING], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: fora de um terminal, informe --token-file."]
    r = run(["bots", "register", CLI_OFFERING, "--token-file", str(tmp_path / "token-inexistente.txt")], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: não foi possível ler o arquivo de --token-file."]
    r = run(["team", "link", CLI_OFFERING, "--person", z_alias], 2)
    assert r.stderr.splitlines() == ["Uso incorreto: fora de um terminal, informe --secret-file."]

    # Operação por agente: a folha `mcp`
    # (o servidor MCP por stdio). Uma invocação de sucesso com a entrada vazia (EOF imediato): código 0, saída padrão
    # vazia (o canal do protocolo) e cada linha da saída de erro uma das linhas fixas de `MCP_STDERR_MESSAGES`, sem a
    # de falha interna; ela entra em `runs` e passa pelas mesmas varreduras de canário e de segredo. A invocação com
    # erro já existe: a folha entra sozinha na lista de uso incorreto acima (toda folha de `_cli_leaves` com
    # `UNKNOWN_OPTION`); aqui só se confere que ela rodou para `mcp`, com o código 2 e sem o canário.
    # No esqueleto, a folha `mcp` ainda não existe.
    from cq_client.mcp_server import MCP_STDERR_MESSAGES

    assert _leaf_of([cq_cli.MCP_COMMAND]) == cq_cli.MCP_COMMAND, "a folha `mcp` não está na árvore do cq"
    mcp_ok = run([cq_cli.MCP_COMMAND], 0)
    assert mcp_ok.stdout == "", "cq mcp com a entrada vazia escreveu na saída padrão"
    stderr_lines = mcp_ok.stderr.splitlines()
    assert [line for line in stderr_lines if line not in MCP_STDERR_MESSAGES.values()] == [], stderr_lines
    assert MCP_STDERR_MESSAGES["internal"] not in stderr_lines, "falha interna no cq mcp com a entrada vazia"
    mcp_ok_run = runs[-1]
    assert (mcp_ok_run.leaf, mcp_ok_run.args, mcp_ok_run.exit_code) == (cq_cli.MCP_COMMAND, (cq_cli.MCP_COMMAND,), 0)
    assert _cli_canary_hits(mcp_ok_run) == [] and _cli_secret_hits(mcp_ok_run, all_secrets()) == []
    mcp_errors = [r for r in runs if r.leaf == cq_cli.MCP_COMMAND and r.exit_code != 0]
    assert [(r.args, r.exit_code, r.stdout) for r in mcp_errors] == [
        ((cq_cli.MCP_COMMAND, UNKNOWN_OPTION), 2, "")], "a invocação de uso incorreto não rodou para a folha mcp"
    assert mcp_errors[0].stderr.startswith("Uso incorreto:"), mcp_errors[0].stderr
    assert _cli_canary_hits(mcp_errors[0]) == [] and _cli_secret_hits(mcp_errors[0], all_secrets()) == []
    return runs


# ---------------------------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------
# CLI: o expurgo, o `doctor` e os comandos locais
# ---------------------------------------------------------------------------------------------


@dataclass
class _PurgeCliPhase:
    """O que a fase do expurgo e do CLI produziu, no banco próprio dela (ver o comentário de `DOCTOR_PURGE_OPERATIONS`)."""

    runs: list[_CliRun]
    exchanges: list[_Exchange]   # as trocas dos clientes da fase (fora da asserção de host do `client`)
    lines: list[str]             # os registros capturados na fase
    caught: list[Any]            # os avisos da fase
    scenario: Any                # o `PurgeScenario` da semente do expurgo
    db_url: str
    events: list[dict]
    deleted_labels: set[str]     # os alvos das pessoas apagadas (fora os da mantida)
    kept_labels: set[str]        # os alvos da mantida (`aluno_b`): em toda varredura das saídas, fora só do banco vivo
    d_labels: set[str]           # os alvos de `citada` (a pessoa apagada citada no outro oferecimento)
    contact_labels: set[str]     # os alvos dos dois e-mails de contato do professor
    current_labels: set[str]     # os do contato vigente, igual ao de outro oferecimento
    other_offering_report_paths: frozenset[str]   # o caminho do relatório do disparo do outro oferecimento


PURGE_LIVE_TABLES = (("delivery_endpoints", "id"), ("telegram_updates", "id"), ("free_messages", "id"),
                  ("outbox_messages", "id"), ("identities", "person_id"), ("offering_contacts", "offering_id"))


def _purge_person_labels(person: Any) -> set[str]:
    """Os rótulos `expurgo-*` de uma pessoa canário do expurgo (nome, palavras, RA, e-mail e contas do Telegram)."""
    parts = [person.name, person.ra, person.email]
    for account in person.accounts:
        parts += [account.full_name, account.first_name, account.last_name or "", str(account.user_id)]
        if account.username:
            parts.append(f"@{account.username}")
    return set(find_canary(" | ".join(parts), prefixes=PURGE_PREFIXES))


def _purge_contact_labels(*emails: str) -> set[str]:
    return set(find_canary(" | ".join(emails), prefixes=PURGE_PREFIXES))


def _purge_live_hits(conn: psycopg.Connection, labels: set[str]) -> dict[tuple[str, Any], set[str]]:
    """A varredura do banco vivo depois do expurgo nas seis tabelas do expurgo, **inteiras**: `(tabela, chave
    da linha)` → os rótulos de `labels` achados na linha."""
    hits: dict[tuple[str, Any], set[str]] = {}
    for table, key in PURGE_LIVE_TABLES:
        rows = conn.execute(f"SELECT {key}, row_to_json(t)::text FROM {table} t ORDER BY {key}").fetchall()
        for row_key, text in rows:
            found = set(find_canary(text, prefixes=PURGE_PREFIXES)) & labels
            if found:
                hits[(table, row_key)] = found
    return hits


def _cli_phase_runner(cli_invoke: Any, runs: list[_CliRun]) -> Callable[..., Any]:
    """O `run` das fases do CLI, com `report=True` para o `status` e o `doctor` com o código 1 (o relatório no
    stdout)."""

    def run(args: list[str], exit_code: int, *, json_mode: bool = False, report: bool = False, **kwargs: Any) -> Any:
        result = cli_invoke(list(args), json=json_mode, **kwargs)
        leaf = _leaf_of(args)
        assert leaf is not None, f"{args} não é um comando folha do cli"
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{args}: o CLI levantou {type(result.exception).__name__}")
        assert result.exit_code == exit_code, (
            f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
        for text in (result.stdout, result.stderr):
            assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
        if exit_code != 0 and not report:
            assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
        runs.append(_CliRun(leaf, tuple(args), json_mode, result.exit_code, result.stdout, result.stderr,
                            result.output))
        return result

    return run


DEPLOY_SERVER_GLOBALS = ("bots.master_key", "tasks.overdue", "clock.skew", "backup.age", "smtp.server",
                     "retention.overdue")  # as 6 globais do servidor, nessa ordem
DEPLOY_RETENTION_DAYS = 30  # o padrão de `CQ_RETENTION_DAYS` (o canário não o define)


def _deploy_retention_expected(sql: psycopg.Connection, now: datetime) -> dict:
    """Implantação: a `retention.overdue` que a semente produz no banco do canário, pela regra
    de retenção (vencido = `archived`, sem expurgo e `archived_at < now − 30 dias`; o `offering` é o de menor
    `(archived_at, id)`). [I] O oráculo é a regra aplicada às linhas da semente, e não um resultado fixo: o estado
    final dos oferecimentos arquivados da semente depende do relógio das fases anteriores."""
    from cq_server.doctor import DOCTOR_REASON_MESSAGES

    rows = sql.execute(
        "SELECT code FROM offerings WHERE state = 'archived' AND purged_at IS NULL AND archived_at < %s "
        "ORDER BY archived_at, id", (now - timedelta(days=DEPLOY_RETENTION_DAYS),)).fetchall()
    if not rows:
        return {"id": "retention.overdue", "part": "api", "result": "ok",
                "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")], "offering": None, "count": 0,
                "detail": None}
    return {"id": "retention.overdue", "part": "api", "result": "warning",
            "message": DOCTOR_REASON_MESSAGES[("retention.overdue", "warning", "overdue")], "offering": rows[0][0],
            "count": len(rows), "detail": None}


def _cli_canary_db_runs(cli_invoke: Any, seed: _Seed, clock: Any, sql: psycopg.Connection) -> list[_CliRun]:
    """CLI: no banco do canário, a invocação com **erro** do `doctor` (o `BOT_OFFERING` fica `active`
    com o bot `no_access` desde a fase de bot: exatamente uma `bot.access = failure`, a dele; o relatório no stdout
    com o código 1, varrido como saída) e o `events list --person` com sucesso e com erro (o nome canário no valor de
    `person`, recusado sem eco).

    Implantação e piloto: as 6 globais do servidor logo
    depois das 4 do cliente, cada uma exatamente uma vez, com as 3 últimas nos resultados que a semente produz:
    `backup.age` "aviso, não configurado" (o canário não aponta o registro do backup), `smtp.server` "aviso, ainda
    não testado" (nenhum worker do canário recebe a sonda: nenhuma linha em `mail_probe`) e `retention.overdue`
    pela regra sobre as linhas da semente; nenhuma das 4 reservadas do CLI sai "não verificado" na parte `api` (as do
    host ficam na parte `local`). A única falha é a `bot.access` do `BOT_OFFERING`."""
    from cq_client.doctor import CHECK_LINE_TEMPLATE, RESULT_LABELS
    from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

    runs: list[_CliRun] = []
    run = _cli_phase_runner(cli_invoke, runs)
    failure_line = f"[falha] bot.access ({BOT_OFFERING}): {DOCTOR_MESSAGES[('bot.access', 'failure')]}"
    retention = _deploy_retention_expected(sql, clock.now())
    text = run(["doctor"], 1, report=True)
    assert [line for line in text.stdout.splitlines() if line.startswith("[falha]")] == [failure_line]
    as_json = run(["doctor"], 1, json_mode=True, report=True)
    assert [(c["id"], c["offering"]) for c in json.loads(as_json.stdout)["checks"] if c["result"] == "failure"] == [
        ("bot.access", BOT_OFFERING)]
    # As globais na ordem fixa do servidor, logo depois das 4 do cliente; as 3 últimas exatas.
    checks = json.loads(as_json.stdout)["checks"]
    assert [c["id"] for c in checks[4:10]] == list(DEPLOY_SERVER_GLOBALS)
    assert [c["id"] for c in checks if c["id"] in DEPLOY_SERVER_GLOBALS] == list(DEPLOY_SERVER_GLOBALS)
    assert checks[7:10] == [
        {"id": "backup.age", "part": "api", "result": "warning", "offering": None, "count": None, "detail": None,
         "message": DOCTOR_REASON_MESSAGES[("backup.age", "warning", "not_configured")]},
        {"id": "smtp.server", "part": "api", "result": "warning", "offering": None, "count": None, "detail": None,
         "message": DOCTOR_REASON_MESSAGES[("smtp.server", "warning", "never")]},
        retention]
    # as 4 reservadas do CLI não saem como "não verificado" fixas na parte `api`.
    parts = [(c["id"], c["part"]) for c in checks
             if c["id"] in ("backup.age", "smtp.server", "postgres.volume", "disk.space")]
    assert parts == [("backup.age", "api"), ("smtp.server", "api"), ("postgres.volume", "local"),
                     ("disk.space", "local")]
    # O texto traz as mesmas 3 linhas, no formato da linha do CLI (`count` no `extra`, o código no lugar do
    # `offering`).
    for check in checks[7:10]:
        extra = f" ({check['count']})" if check["count"] is not None else ""
        offering = f" ({check['offering']})" if check["offering"] is not None else ""
        line = CHECK_LINE_TEMPLATE.format(label=RESULT_LABELS[check["result"]], id=check["id"], offering=offering,
                                          message=check["message"], extra=extra)
        assert text.stdout.splitlines().count(line) == 1, (line, text.stdout)
    run(["events", "list", "--person", seed.alias(ZEFIRO)], 0)
    run(["events", "list", "--person", seed.alias(ZEFIRO)], 0, json_mode=True)
    run(["events", "list", "--type", "person.registered", "--person", ZEFIRO.name], 1)
    return runs


def _cli_phase_profile(tmp_path: Any) -> tuple[Any, Any]:
    """O arquivo de perfil da fase: o local (`TEST_API_URL`, loopback com porta explícita), o local inalcançável (`http://127.0.0.1:9`,
    para a dica) e o remoto; as chaves de topo apontam para a API inalcançável (nunca o padrão `127.0.0.1:8000`)."""
    folder = tmp_path / "cli-perfil"
    stack_dir = make_stack_dir(folder)
    text = "\n".join([
        f'api_url = "{UNREACHABLE_API_URL}"', "",
        "[profiles.local]", 'kind = "local"', f'api_url = "{TEST_API_URL}"', f"stack_dir = '{stack_dir}'", "",
        "[profiles.inalcancavel]", 'kind = "local"', f'api_url = "{UNREACHABLE_API_URL}"', f"stack_dir = '{stack_dir}'",
        "", "[profiles.remoto]", 'kind = "remote"', f'api_url = "{CLI_REMOTE_URL}"', ""])
    config = write_profile(folder, text)
    if os.name != "nt":
        os.chmod(config, 0o600)  # o teste restringe o próprio arquivo; o `doctor` nunca faz `chmod`
    return config, stack_dir


def _doctor_expected() -> list[dict]:
    """O relatório exato do `doctor` com sucesso: a API sobre um banco novo, sem bot (`bots.master_key`
    aviso, o único aviso possível além do relógio, alinhado ao banco), a pilha saudável e o perfil local.

    Implantação e piloto: sem as 4 fixas "não verificado";
    as 6 globais do servidor na ordem fixa, com as 3 últimas nos resultados desse banco novo
    (`backup.age` "aviso, não configurado": a API da fase não aponta o registro do backup; `smtp.server` "aviso,
    ainda não testado": nenhuma linha em `mail_probe`; `retention.overdue` "ok" com 0: nenhum oferecimento); as 2 do
    host (`HOST_CHECK_IDS`, parte `local`) logo depois das 10 locais do CLI, "não verificado" com o motivo fixo
    `no_host_probe` (o `FakeStack` injetado e nenhum `host_probe`: o CLI não monta o probe real). O ramo por
    plataforma é do `profile.permissions` (sem leitura dos bits no Windows), não das reservadas."""
    from cq_client.doctor import CLIENT_MESSAGES, HOST_CHECK_IDS, LOCAL_CHECK_IDS, NOT_VERIFIED_MESSAGES
    from cq_server.doctor import DOCTOR_MESSAGES, DOCTOR_REASON_MESSAGES

    def check(check_id: str, part: str, result: str, message: str, **extra: Any) -> dict:
        return {"id": check_id, "part": part, "result": result, "message": message, "offering": None,
                "count": None, "detail": None, **extra}

    checks = [check(i, "api", "ok", CLIENT_MESSAGES[(i, "ok")])
              for i in ("api.reachable", "database.reachable", "database.schema", "api.access")]
    checks += [check("bots.master_key", "api", "warning", DOCTOR_MESSAGES[("bots.master_key", "warning")]),
               check("tasks.overdue", "api", "ok", DOCTOR_MESSAGES[("tasks.overdue", "ok")], count=0),
               check("clock.skew", "api", "ok", DOCTOR_MESSAGES[("clock.skew", "ok")])]
    checks += [check("backup.age", "api", "warning",
                     DOCTOR_REASON_MESSAGES[("backup.age", "warning", "not_configured")]),
               check("smtp.server", "api", "warning", DOCTOR_REASON_MESSAGES[("smtp.server", "warning", "never")]),
               check("retention.overdue", "api", "ok", DOCTOR_REASON_MESSAGES[("retention.overdue", "ok", "ok")],
                     count=0)]
    checks += [check(i, "local", "ok", CLIENT_MESSAGES[(i, "ok")],
                     detail=DOCTOR_ENGINE_DETAIL if i == "engine.image" else None) for i in LOCAL_CHECK_IDS]
    checks += [check(i, "local", "not_verified", NOT_VERIFIED_MESSAGES["no_host_probe"]) for i in HOST_CHECK_IDS]
    permissions = "not_verified" if os.name == "nt" else "ok"
    checks += [check("profile.permissions", "profile", permissions,
                     CLIENT_MESSAGES[("profile.permissions", permissions)]),
               check("profile.git", "profile", "ok", CLIENT_MESSAGES[("profile.git", "ok")])]
    return checks


def _purge_second_cited_answer(client: Any, sql: psycopg.Connection, clock: Any, other: Any) -> tuple[int, int]:
    """Texto de outro oferecimento depois do expurgo: uma segunda resposta de texto em `O2`, igual à da semente (cita
    `citada`), de uma pessoa neutra, para o item passar do mínimo de 2 ocorrências da distribuição `texts` do
    relatório no momento de `O2` (a lista dos relatórios), no molde de `_second_cited_answer`
    (`tests/privacy/test_purge_privacy.py`); devolve (update, submissão)."""
    from tests.helpers.purge import _activate_student, _answered, _endpoint, _import, _part, _person_by_ra

    neutral = {"name": "Pessoa Neutra Segunda Resposta", "ra": "4480000001",
               "email": "pessoa.segunda.resposta@turma.example.org"}
    _import(client, other.code, [neutral])
    person_row, _, _ = _person_by_ra(sql, neutral["ra"])
    now = clock.now()
    chat = 9_480_000_001
    enrollment = _activate_student(sql, person_row, other.offering_row, now - timedelta(hours=3))
    endpoint = _endpoint(sql, person_row, other.bot_row, chat, "active", now - timedelta(hours=3))
    answer = f"igual à de {PURGE_D.name}"  # o texto da resposta de `O2` na semente
    opens = now - timedelta(hours=2)
    _, submission, update = _answered(
        sql, dispatch_row=other.dispatch_row, enrollment_row=enrollment, person_row=person_row,
        bot_row=other.bot_row, endpoint_row=endpoint, chat_id=chat, question=preset("text"),
        raw={"resposta": answer}, text=answer, score=0.0, part_derived=[_part(0)], opens=opens,
        answered=opens + timedelta(minutes=30), revealed=now - timedelta(minutes=59))
    return update, submission


def _purge_and_cli_phase(cli_invoke: Any, clock: Any, settings: Any, bots: Any, db_factory: Any, tmp_path: Any,
              monkeypatch: Any) -> _PurgeCliPhase:
    """A fase do expurgo e do CLI: num banco próprio, a semente do expurgo com as pessoas canário (e o contato igual de outro
    oferecimento, `shared_contact=True`) e uma segunda resposta de texto em `O2` que cita `citada`,
    o expurgo pelas folhas do CLI (com sucesso e com erro), toda leitura
    do catálogo depois dele, `events list --person`, os comandos locais pelo `FakeStack` (com sucesso e com erro), a
    dica da API inalcançável e o `doctor` com o código 0 sobre uma API própria num banco novo."""
    from cq_server.api import create_app
    from tests.helpers import purge as purge_helpers
    from tests.helpers.calls import TEST_REPO_SETTINGS
    from tests.helpers.purge import seed_purge_scenario

    saved_now = clock.now()
    r7_db = db_factory()
    r7_settings = dataclasses.replace(settings, database_url=r7_db)
    original_fixture = purge_helpers._fixture
    # A semente lê `settings` (o banco das tarefas que ela drena) pelo `request`; aqui, o banco da fase.
    monkeypatch.setattr(purge_helpers, "_fixture",
                        lambda name: r7_settings if name == "settings" else original_fixture(name))
    config, _stack_dir = _cli_phase_profile(tmp_path)
    local_env: dict[str, str | None] = {"CQ_API_URL": None, "CQ_PROFILE": "local", "CQ_CONFIG": str(config)}
    exchanges: list[_Exchange] = []
    runs: list[_CliRun] = []
    run = _cli_phase_runner(cli_invoke, runs)
    p731: set[str] = set()

    with capture_logs() as captured, warnings.catch_warnings(record=True) as caught:
        app = create_app(r7_settings, clock=clock, bots=bots, repo_settings=TEST_REPO_SETTINGS)
        with TestClient(app, raise_server_exceptions=False) as r7, psycopg.connect(r7_db, autocommit=True) as r7_sql:
            _record_exchanges(r7, exchanges)
            scenario = seed_purge_scenario(r7, r7_sql, shared_contact=True)
            assert sorted(label for label, p in scenario.people.items() if p.deleted) == ["aluno_a", "citada",
                                                                                          "equipe"]
            assert scenario.other is not None and scenario.shared_contact is not None
            assert scenario.other.dispatch_row is not None
            # Texto de outro oferecimento: a segunda resposta igual em `O2`, antes do expurgo; o update e a submissão novos entram
            # nas contagens exatas das exceções do banco vivo (`_check_cli_surfaces`).
            extra_update, extra_submission = _purge_second_cited_answer(r7, r7_sql, clock, scenario.other)
            scenario = dataclasses.replace(scenario, other=dataclasses.replace(
                scenario.other, update_rows=(*scenario.other.update_rows, extra_update),
                submission_rows=(*scenario.other.submission_rows, extra_submission)))
            deleted = [p for p in scenario.people.values() if p.deleted]
            kept = scenario.person("aluno_b")
            # A mantida fica fora só da varredura do banco vivo (a identidade dela continua lá, por regra);
            # nas saídas da fase, os alvos dela entram em toda varredura (`_check_cli_surfaces`).
            kept_labels = _purge_person_labels(kept.identity)
            deleted_labels = set().union(*(_purge_person_labels(p.identity) for p in deleted)) - kept_labels
            contact_labels = _purge_contact_labels(PURGE_CONTACT_OLD, PURGE_CONTACT_CURRENT)
            current_labels = _purge_contact_labels(PURGE_CONTACT_CURRENT)
            # Controles positivos antes do expurgo: a varredura acha as apagadas em `identities` e o contato em
            # `offering_contacts`; a revelação as mostra.
            before = _purge_live_hits(r7_sql, deleted_labels | contact_labels)
            assert {table for table, _ in before} >= {"identities", "offering_contacts", "telegram_updates"}, before
            revealed = r7.post("/v1/identity/reveal", json={"pseudonyms": [p.pseudo_id for p in deleted],
                                                            "reason": VALID_RA_REASON}, headers=ADMIN)
            assert revealed.status_code == 200, revealed.text[:300]
            assert f"expurgo-nome:{PURGE_D.name}" in find_canary(revealed.text, prefixes=PURGE_PREFIXES)

            # O expurgo pelo CLI: preparação (texto e JSON), confirmação com código errado, com o código certo e
            # repetida (o resultado gravado), e a preparação de novo (já expurgado: `invalid_state`).
            code = scenario.code
            run(["offerings", "purge", "prepare", code], 0, http=r7)
            prepared = json.loads(run(["offerings", "purge", "prepare", code], 0, json_mode=True, http=r7).stdout)
            assert_matches("offerings.prepare_purge", 200, prepared)
            run(["offerings", "purge", "confirm", code, "--confirmation", PURGE_WRONG_CODE], 1, http=r7)
            run(["offerings", "purge", "prepare", "expurgo-inexistente"], 1, http=r7)
            run(["offerings", "purge", "confirm", code, "--confirmation", prepared["confirmation_code"]], 0, http=r7)
            again = run(["offerings", "purge", "confirm", code, "--confirmation", prepared["confirmation_code"]], 0,
                        json_mode=True, http=r7)
            assert_matches("offerings.purge", 200, json.loads(again.stdout))
            run(["offerings", "purge", "prepare", code], 1, http=r7)

            # Toda leitura do catálogo depois do expurgo, nos três oferecimentos do cenário.
            paths = {op["id"]: op["path"] for op in load_catalog()["operations"]}

            def get(path: str, params: dict | None = None) -> Any:
                resp = r7.get(path, params=params, headers=ADMIN)
                assert resp.status_code == 200, f"{path} {params}: {resp.status_code} {resp.text[:300]}"
                return resp

            for op_id in ("people.list", "tokens.list", "offerings.list"):
                get(paths[op_id])
            get("/health")
            for person in scenario.people.values():
                get(paths["people.get"].replace("{ref}", person.pseudo_id))
                get("/v1/events", {"person": person.pseudo_id})
                get("/v1/events", {"person": person.alias, "types": "offering.purged"})
            refs = (code, scenario.other.code, scenario.shared_contact.code)
            for ref in refs:
                for op_id in ("offerings.get", "bots.get", "students.summary", "dispatches.list", "messages.list",
                              "offerings.get_digest"):
                    get(paths[op_id].replace("{ref}", ref))
            for ref, dispatch_id in ((code, scenario.dispatch_id), (scenario.other.code, scenario.other.dispatch_id)):
                assert dispatch_id is not None
                report = paths["reports.get"].replace("{ref}", ref).replace("{dispatch_id}", dispatch_id)
                get(paths["dispatches.get"].replace("{ref}", ref).replace("{dispatch_id}", dispatch_id))
                get(report)
                get(report, {"format": "csv"})
                get(report, {"version": "latest"})
                get(paths["reports.list_versions"].replace("{ref}", ref).replace("{dispatch_id}", dispatch_id))
                if ref == scenario.other.code:
                    p731.add(report)
            events = all_events(r7, ADMIN_TOKEN, *EVENT_TYPES)
            # Controle: a revelação depois do expurgo devolve só a mantida; as apagadas em `not_found`.
            after = r7.post("/v1/identity/reveal", json={"pseudonyms": [*(p.pseudo_id for p in deleted),
                                                                        kept.pseudo_id],
                                                         "reason": VALID_RA_REASON}, headers=ADMIN)
            assert after.status_code == 200, after.text[:300]
            assert [p["pseudo_id"] for p in after.json()["people"]] == [kept.pseudo_id]
            assert sorted(after.json()["not_found"]) == sorted(p.pseudo_id for p in deleted)

            # `events list --person` no banco da fase (a pessoa mantida e uma apagada).
            run(["events", "list", "--person", kept.alias], 0, http=r7)
            run(["events", "list", "--person", deleted[0].pseudo_id], 0, json_mode=True, http=r7)

            # Os comandos locais pelo `FakeStack`: com sucesso, o perfil local em `TEST_API_URL` e a API da
            # fase por `http=`; com erro, o perfil remoto (2) e o Docker indisponível (1).
            remote_env = {**local_env, "CQ_PROFILE": "remoto"}
            run(["up"], 0, http=r7, stack=FakeStack(["absent", "healthy"]), env=local_env)
            run(["status"], 0, http=r7, stack=FakeStack(["healthy"]), env=local_env)
            run(["status"], 0, json_mode=True, http=r7, stack=FakeStack(["healthy"]), env=local_env)
            run(["logs"], 0, http=r7, stack=FakeStack(["healthy"]), env=local_env)
            run(["logs"], 0, json_mode=True, http=r7, stack=FakeStack(["healthy"]), env=local_env)
            run(["down"], 0, http=r7, stack=FakeStack(["healthy"]), env=local_env)
            run(["up"], 2, http=r7, stack=FakeStack(["healthy"]), env=remote_env)
            run(["logs"], 2, http=r7, stack=FakeStack(["healthy"]), env=remote_env)
            run(["down"], 1, http=r7, stack=FakeStack.unavailable(), env=local_env)
            run(["status"], 1, report=True, http=r7, stack=FakeStack.unavailable(), env=local_env)
            run(["doctor"], 1, report=True, json_mode=True, http=r7, stack=FakeStack.unavailable(), env=local_env)
            # A dica da API inalcançável (controle contra o vazio): uma folha do catálogo sem `http=`, com o perfil local de
            # teste em `http://127.0.0.1:9` (nunca o padrão `127.0.0.1:8000`).
            hint = run(["offerings", "list"], 3, http=None, env={**local_env, "CQ_PROFILE": "inalcancavel"})
            assert HINT_TEMPLATE.format(project="cq") in hint.stderr.splitlines(), hint.stderr

        # O `doctor` com o código 0: uma API própria sobre um banco novo, sem bot, o mesmo relógio.
        doctor_db = db_factory()
        doctor_app = create_app(dataclasses.replace(settings, database_url=doctor_db), clock=clock, bots=None,
                                repo_settings=TEST_REPO_SETTINGS)
        with TestClient(doctor_app, raise_server_exceptions=False) as dc, \
                psycopg.connect(doctor_db, autocommit=True) as doctor_sql:
            _record_exchanges(dc, exchanges)
            expected = _doctor_expected()
            for json_mode in (False, True):
                align_clock_to_db(clock, doctor_sql)
                result = run(["doctor"], 0, json_mode=json_mode, http=dc, stack=FakeStack(["healthy"]), env=local_env)
                if json_mode:
                    assert json.loads(result.stdout) == {
                        "profile": {"name": "local", "kind": "local", "api_url": TEST_API_URL, "token_set": True,
                                    "compose_project": "cq",
                                    "origins": {"name": "env", "kind": "profile", "api_url": "profile", "token": "env",
                                                "compose_project": "default"}},
                        "checks": expected}
                else:
                    # Os avisos são 3 (`bots.master_key`, `backup.age` e `smtp.server`).
                    assert result.stdout.splitlines()[-1] == "Resultado: 0 falha(s), 3 aviso(s)."
                    assert len(result.stdout.splitlines()) == 2 + len(expected) + 1
        clock.set(saved_now)

    return _PurgeCliPhase(runs=runs, exchanges=exchanges, lines=list(captured.lines), caught=list(caught),
                    scenario=scenario, db_url=r7_db, events=events, deleted_labels=deleted_labels,
                    kept_labels=kept_labels, d_labels=_purge_person_labels(PURGE_D), contact_labels=contact_labels,
                    current_labels=current_labels, other_offering_report_paths=frozenset(p731))


def _check_cli_surfaces(run: _PurgeCliPhase, secrets: list[str]) -> None:
    """Expurgo: todas as pessoas canário do expurgo (as apagadas **e a mantida**, `aluno_b`) e os dois
    e-mails do contato em nenhuma saída da fase fora do corpo da revelação (respostas da API, saídas do CLI,
    registros, avisos e eventos); no banco vivo, as apagadas e o contato (a mantida fica fora só desta varredura: a
    identidade dela continua no banco, por regra). Controle positivo da mantida: o mesmo detector a acha na
    revelação depois do expurgo, com exatamente os alvos de nome, palavras, RA e e-mail dela. As exceções
    declaradas, cada uma com controle positivo e contagem exata:
    - texto de outro oferecimento: o `raw_input` do outro oferecimento (`O2`) que cita a pessoa apagada (`citada`) no relatório **no
      momento** (`reports.get` sem `version`, JSON e CSV): exatamente essas 2 respostas, só com os alvos de `citada`;
    - a cópia dessas mesmas respostas em `submissions.submitted_answers`,
      retida no banco vivo (nenhuma rota a expõe): exatamente as mesmas submissões, só com os alvos de `citada`;
    - retenção de outros oferecimentos (a): o `telegram_updates.text` do bot de `O2` (não expurgado): exatamente as linhas das duas respostas de
      texto (a da semente e a segunda, de `_purge_second_cited_answer`) e da mensagem livre que citam `citada`,
      com o nome literal; (b): o contato vigente igual ao de `O3` (não
      expurgado), em `offering_contacts` e nas linhas de `outbox_messages` do bot dele, e nenhuma outra;
    - o log de eventos (texto livre gravado antes do cadastro) e o texto de operador: **0** ocorrências nesta fase (a
      semente não grava texto livre em evento nem identidade em `repo`); os controles positivos dessas duas ficam na
      testes de privacidade do expurgo (`tests/privacy/test_purge_privacy.py`).
    Nas linhas do oferecimento e do bot expurgados, nenhuma ocorrência, sem exceção. Também: nenhuma identidade das fases
    anteriores e nenhum segredo nas trocas, nas saídas do CLI, nos registros e nos avisos da fase."""
    scenario = run.scenario
    # `labels` (as apagadas e o contato) é o recorte do banco vivo, onde a mantida fica por regra;
    # `output_labels` (também a mantida) é o de toda saída da fase: a identidade só sai pela revelação.
    labels = run.deleted_labels | run.contact_labels
    output_labels = labels | run.kept_labels
    assert run.deleted_labels and run.contact_labels and run.d_labels and run.kept_labels
    assert run.kept_labels.isdisjoint(labels), run.kept_labels & labels
    d_name = f"expurgo-nome:{PURGE_D.name}"
    assert d_name in run.d_labels and run.current_labels <= run.contact_labels

    # Controle positivo da mantida (contra a varredura vazia): o detector das saídas acha a identidade dela na
    # revelação depois do expurgo, a segunda das duas revelações da fase (a primeira, antes do expurgo, só pede as
    # apagadas), com exatamente os alvos de nome, palavras, RA e e-mail dela.
    kept = scenario.person("aluno_b").identity
    kept_revealed = set(find_canary(" | ".join((kept.name, kept.ra, kept.email)), prefixes=PURGE_PREFIXES))
    assert f"expurgo-nome:{kept.name}" in kept_revealed and kept_revealed <= run.kept_labels, kept_revealed
    reveals = [set(find_canary(ex.body, prefixes=PURGE_PREFIXES)) & output_labels
               for ex in run.exchanges if ex.is_reveal_success]
    assert len(reveals) == 2, f"revelações com sucesso na fase: {len(reveals)}, esperadas 2"
    assert d_name in reveals[0] and reveals[0] <= run.deleted_labels, reveals[0]
    assert reveals[1] == kept_revealed, f"a revelação depois do expurgo: {reveals[1]} != {kept_revealed}"

    # --- respostas da API da fase ---
    p731_hits = []
    for ex in run.exchanges:
        assert _exchange_canary_hits(ex) == [], (ex.method, ex.path, _exchange_canary_hits(ex))
        assert _exchange_secret_hits(ex, secrets) == [], (ex.method, ex.path)
        assert find_secrets(ex.url, secrets) == [], ex.path
        # Só o corpo do 200 da revelação fica de fora (como em `_exchange_canary_hits`); o cabeçalho, nunca.
        found = set(find_canary(ex.headers, prefixes=PURGE_PREFIXES)) & output_labels
        if ex.is_reveal_success:
            assert found == set(), f"pessoa canário do expurgo ou contato no cabeçalho da revelação: {ex.url} {found}"
            continue
        found |= set(find_canary(ex.body, prefixes=PURGE_PREFIXES)) & output_labels
        if ex.path in run.other_offering_report_paths and "version=" not in ex.url and ex.status == 200:
            assert found <= run.d_labels and d_name in found, (ex.url, found)  # controle positivo
            p731_hits.append(ex.url)
            continue
        assert found == set(), f"pessoa canário do expurgo ou contato numa resposta da fase: {ex.method} {ex.url} {found}"
    assert len(p731_hits) == 2, f"texto de outro oferecimento: o relatório no momento do outro oferecimento (JSON e CSV): {p731_hits}"

    # --- saídas do CLI, registros e avisos da fase ---
    for cli_run in run.runs:
        for text in cli_run.texts:
            assert set(find_canary(text, prefixes=PURGE_PREFIXES)) & output_labels == set(), (cli_run.args, text[:300])
    for index, line in enumerate(run.lines):
        assert set(find_canary(line, prefixes=PURGE_PREFIXES)) & output_labels == set(), (index, line[:300])
        assert find_canary(line) == [] and find_secrets(line, secrets) == [], (index, line[:300])
    for warning in run.caught:
        text = str(warning.message)
        assert find_canary(text) == [] and set(find_canary(text, prefixes=PURGE_PREFIXES)) & output_labels == set(), text
        assert find_secrets(text, secrets) == [], text

    # --- eventos (0 ocorrências nesta fase) ---
    assert set(PURGE_EVENT_TYPES) <= {e["type"] for e in run.events}, "evento do expurgo que não saiu na fase"
    event_hits = [e["seq"] for e in run.events if set(find_canary(json.dumps(e, ensure_ascii=False),
                                                                  prefixes=PURGE_PREFIXES)) & output_labels]
    assert event_hits == [], f"pessoa canário do expurgo ou contato no log de eventos: {event_hits}"

    with psycopg.connect(run.db_url, autocommit=True) as conn:
        # O oferecimento expurgado da fase tem `purged_at` e `purge_result` preenchidos, antes da varredura.
        row = conn.execute("SELECT purged_at IS NOT NULL, purge_result IS NOT NULL FROM offerings WHERE id = %s",
                           (scenario.offering_row,)).fetchone()
        assert row == (True, True), row
        # --- banco vivo, as seis tabelas inteiras, com `labels` (sem a mantida) ---
        hits = _purge_live_hits(conn, labels)
        other, shared = scenario.other, scenario.shared_contact
        expected_a = {("telegram_updates", row_id) for row_id in other.update_rows}
        expected_b = {("offering_contacts", shared.offering_row)} | {("outbox_messages", r) for r in shared.outbox_rows}
        assert expected_a and len(expected_a) == len(other.update_rows), "exceção (a) do banco vivo sem linha semeada"
        assert expected_b and len(shared.outbox_rows) >= 1, "exceção (b) do banco vivo sem linha semeada"
        assert set(hits) == expected_a | expected_b, f"banco vivo fora das exceções: {hits}"
        for key in expected_a:
            assert hits[key] <= run.d_labels and d_name in hits[key], (key, hits[key])  # controle positivo (a)
        for key in expected_b:
            assert hits[key] <= run.current_labels and hits[key], (key, hits[key])  # controle positivo (b)
        # Nas linhas do bot expurgado, nada: nem identidade, nem `chat_id`, nem texto de update.
        assert conn.execute("SELECT count(*) FROM delivery_endpoints WHERE bot_id = %s AND chat_id IS NOT NULL",
                            (scenario.bot_row,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM telegram_updates WHERE bot_id = %s AND (chat_id IS NOT NULL OR "
                            "sender_id IS NOT NULL OR text IS NOT NULL)", (scenario.bot_row,)).fetchone()[0] == 0
        # Texto de outro oferecimento no banco: o `raw_input` do outro oferecimento, exatamente as submissões semeadas que citam `citada`.
        sub_hits = {row_id for row_id, text in conn.execute(
            "SELECT id, raw_input::text FROM submissions WHERE raw_input IS NOT NULL ORDER BY id").fetchall()
            if set(find_canary(text, prefixes=PURGE_PREFIXES)) & labels}
        assert sub_hits == set(other.submission_rows), f"texto de outro oferecimento no banco: {sub_hits}"
        # Retenção aprovada no expurgo: a cópia em `submitted_answers` dessas mesmas submissões, retida no banco, exatamente as
        # mesmas linhas, cada uma só com os alvos de `citada` (controle positivo: o nome dela está lá).
        answers_hits = {row_id: set(find_canary(text, prefixes=PURGE_PREFIXES)) & labels for row_id, text in conn.execute(
            "SELECT id, submitted_answers::text FROM submissions WHERE submitted_answers IS NOT NULL ORDER BY id"
        ).fetchall()}
        answers_hits = {row_id: found for row_id, found in answers_hits.items() if found}
        assert set(answers_hits) == set(other.submission_rows), f"cópia retida no banco: {sorted(answers_hits)}"
        for row_id, found in answers_hits.items():
            assert found <= run.d_labels and d_name in found, (row_id, found)
        # Todas as colunas do banco da fase (tarefas, `outbox_messages`, eventos e o resto): só nas exceções acima.
        allowed = {("telegram_updates", "text"), ("outbox_messages", "text"), ("submissions", "raw_input"),
                   ("submissions", "submitted_answers"), ("offering_contacts", "email")}
        everywhere = {(table, column) for table, column, text in scan_all_tables(conn)
                      if set(find_canary(text, prefixes=PURGE_PREFIXES)) & labels}
        assert everywhere <= allowed, f"pessoa apagada ou contato fora das exceções: {sorted(everywhere - allowed)}"
        # Nenhuma identidade das fases anteriores no banco da fase (os alvos de todos os prefixos anteriores).
        assert _generic_db_scan(run.db_url) == {}, _generic_db_scan(run.db_url)


def test_canary_sweep_finds_no_identity_or_secret_outside_reveal(
        client: Any, sql: psycopg.Connection, queue: Any, outbox: Any, worker_thread: Any, cli_invoke: Any,
        clock: Any, db_url: str, tmp_path: Any, monkeypatch: Any, capfd: Any, bot_api: Any,
        bot_worker_thread: Any, settings: Any, smtp: Any, mail_channel: Any, rich_bot_api: Any, engine_fake: Any,
        repo_fetcher: Any, db_factory: Any, bots: Any) -> None:
    """Canário completo: semente → worker → API → CLI, e varredura de respostas, CLI, logs, saída do
    processo, avisos e banco.

    Erros sem eco, textos semeados redigidos e os logs técnicos
    (inclusive a saída padrão e a de erro do processo); o princípio da identidade só pela revelação (em qualquer
    saída); a varredura SQL; controles positivos e checagens contra o vazio.
    Pseudônimos de formato válido que carregam o RA canário chegam ao domínio
    (404, `not_found`, 403) pela API e pelo CLI, sob as mesmas varreduras.

    Oferecimentos e bots: as 10 operações e as 10 folhas, com sucesso e com erro,
    e o processamento de updates de remetentes canário; a minimização (nome, sobrenome e
    `@username` nunca gravados); disciplina e período redigidos na saída (inclusive na descrição
    do bot e nas mensagens enviadas; nenhuma saída com o id do chat fora do destino do `sendMessage`);
    token de bot, chave-mestra e links fora de respostas, URL do pedido, CLI, logs e saída do
    processo; o uso do bot pelos usuários na fase de bot;
    eventos exatos da fase de bot; o código do
    oferecimento com o RA canário (sem eco) e o token em lugar errado (token com o nome
    canário, sem eco); contagens exatas de `tasks` e `outbox_messages`; controles contra o vazio (todo
    tipo de evento saiu, toda folha do CLI foi invocada, um registro de pedido por operação).
    A minimização na varredura genérica do banco: em `identities`, dos alvos do
    Telegram só os que coincidem com uma pessoa da semente base (os de `TG_ZEFIRO`, que é `ZEFIRO`), com
    igualdade exata; nenhum alvo de `TG_TEODORICO` nem de `TG_FILISBINA`, nenhum `@username` e nenhum
    `tg-id`.

    Vínculo do aluno: as 5 operações e as 5 folhas
    do CLI, com sucesso e com erro, os 4 textos do bot, o e-mail do convite e os 8 desfechos
    da recepção, sem identidade de aluno fora das exceções exatas e sem código de convite nem
    senha do SMTP fora das exceções; as regras do vínculo
    na fase de alunos;
    a troca de conta, o link de outra pessoa e o link
    vencido; o nome, o sobrenome e o
    `@username` do perfil do Telegram dos alunos em lugar nenhum, nem em `identities`; contagens exatas
    de `tasks`, `outbox_messages` e `telegram_updates` com a fase de alunos e as chamadas do vínculo;
    controles contra o vazio (os 4 textos saíram, o SMTP falso recebeu convites,
    os 8 desfechos e os 8 tipos de evento apareceram, as 5 folhas rodaram).

    Disparo e resposta: as 6
    operações e as 6 folhas, com sucesso e com erro; os 13 tipos de evento, todos emitidos
    pela fase de disparos, `_dispatch_phase`, entre a fase de alunos e a da API: os
    alvos dos alunos canário do disparo (`DISPATCH_PREFIXES`) só nas exceções declaradas de
    `_check_dispatch_surfaces`, cada uma com controle positivo; a sentinela do gabarito só na edição do
    fechamento; o `technical_detail` do motor em lugar nenhum; o token privado do repositório fora
    de toda saída; `find_secrets` também sobre as chamadas do motor falso; os eventos exatos da fase
    (`_check_dispatch_events`); controles contra o vazio (cada texto do bot que a fase produz, cada
    tipo de evento, cada folha com sucesso e com erro, cada tabela com linha, os desfechos
    da recepção); nenhuma chamada canário do disparo dá 409. O fechamento da fase é determinístico:
    a parcela das rodadas `dispatch.close` é medida (`n_close`). Contagens: as
    globais de `tasks`, `outbox_messages` e `telegram_updates` somam a fase de
    disparos e a semente de disparos (`seed_state`), parcela por parcela nos comentários. Além disso,
    o controle contra o vazio cobre todo texto
    do bot (`DISPATCH_PHASE_TEXT_NAMES`, os trechos das funções, os rótulos dos botões e o `/naosei`), com as
    exceções por nome de `DISPATCH_TEXT_EXCEPTIONS` (`GRADING_DELAYED`, tempo real; `DONT_KNOW_HOWTO`, só canal sem
    botões); o passo 5 da fase provoca os que faltavam, inclusive o erro de formato de A com a identidade de
    B, varrido em `_check_dispatch_surfaces`, e as contagens exatas somam as suas parcelas. A
    conferência dos nomes da lista de textos do bot roda aqui dentro (`_check_dispatch_text_names`),
    não na importação, e a `TYPED_MULTIPLE_HOWTO` sai no 5º disparo da fase (múltipla degradada).

    Três comportamentos entram nas contagens: as mensagens livres
    opcionais, o gabarito só para quem já respondeu e a nota parcial. Mensagens livres: o oferecimento da
    fase é cadastrado com as mensagens livres ligadas e com o contato do professor canário (`PROFESSOR_EMAIL`);
    o passo 5.9 desliga pela API e cobre o modo desligado (a resposta com o contato, a máscara, nenhum
    registro); a alteração e o contato no cadastro entram nas chamadas canário (o e-mail **válido** de aluno
    canário, recusado sem eco) e no CLI (`offerings settings`); o e-mail do professor só nas exceções
    declaradas (`_check_professor_email`); `offering_contacts` e `offerings.free_messages` na varredura SQL; os
    corpos do `Offering` com os dois booleanos. Gabarito só para quem já respondeu: a `no_answer`
    do 4º fica sem o gabarito e com o "Não sei"; o toque antigo depois do fechamento é o "Não sei" atrasado, e o
    `/naosei` seguinte recebe `ALREADY_ANSWERED`; `WINDOW_CLOSED` é exceção por nome. Nota parcial:
    A responde o 5º com nota parcial (`VERDICT_PARTIAL`, `SCORE`). As contagens globais e as da fila
    do bot seguem as parcelas dos comentários.

    Relatórios e acompanhamento:
    a fase de relatórios e avisos à equipe (`_report_phase`), depois da fase de disparos e antes da API, com
    oferecimento, bot, equipe de 2 membros (um com endereço bloqueado), workers, `rich_bot_api` e motor falso
    próprios: a questão canário (identidade de `DISPATCH_B` no título e nas alternativas) entregue na prévia à
    equipe, com toque e resposta; `DISPATCH_A` responde textos e números com a identidade de `DISPATCH_B`;
    congelamento, reenvio, o relatório em JSON e em CSV (no momento e congelado), as 5 folhas do CLI com
    sucesso e com erro (`--csv` incluído), cada tipo de aviso (inclusive `no_recipient`, `gave_up`, `not_sent`,
    `no_activity` e `missed`) e o resumo diário; `_check_report_surfaces` com `DISPATCH_PREFIXES` e as exceções declaradas,
    cada uma com controle positivo (a marca de redação no texto redigido; o conteúdo da questão nas mensagens da
    prévia e em `dispatch_preview_contents`); `len(EVENT_TYPES) == 46`; controles contra o vazio (cada tipo
    de aviso, cada estado final, cada evento, cada folha com sucesso e com erro, cada tabela com
    linha, `REPORT_OUTCOMES`, cada texto de `noticetexts` ou exceção por nome); nenhuma chamada canário dos relatórios dá 409.
    Contagens: as globais de `tasks`, `outbox_messages` e `telegram_updates`
    somam a fase de relatórios, a semente de relatórios (`seed_state`) e as tarefas que os relatórios criam nas fases
    de bot, de alunos e de disparos, parcela por parcela nos comentários, as parcelas dependentes do tempo real medidas e nomeadas
    (`n_close`, `n_opened_rule`, `n_notice_send`). Separação por id: o bloco do `n_close` separa as
    rodadas `dispatch.close` da semente e da fase de relatórios por id, com a contagem exata. Amostra da prévia:
    em `_check_dispatch_surfaces`, a exceção nomeada das colunas da amostra, com controle positivo e contagem exata.

    CLI e expurgo:
    Os corpos exatos do `Offering` levam a chave `"purged_at": None`. No banco do
    canário, o `doctor` com erro (exatamente uma `bot.access = failure`, a do `BOT_OFFERING`) e o `events list
    --person` com sucesso e com erro; as chamadas canário do CLI com o status e o código fixados (a do
    `confirmation_code`, o único 409, `invalid_confirmation`); as 5 colunas `purge*` de `offerings` preenchidas antes da
    varredura. A fase do expurgo (`_purge_and_cli_phase`), num banco próprio e com captura própria: a semente do expurgo com as pessoas
    canário, o expurgo pelas folhas do CLI, toda leitura do catálogo depois dele, os comandos locais pelo
    `FakeStack`, a dica da API inalcançável e o `doctor` com o código 0 numa API própria; `_check_cli_surfaces` com as
    exceções do texto de outro oferecimento e da retenção de outros oferecimentos, cada uma com controle positivo e contagem exata. `len(EVENT_TYPES) == 48`;
    `tree` igual à união do catálogo com `LOCAL_COMMANDS`; controles contra o vazio (cada evento, cada folha
    e cada comando local com sucesso e com erro, a dica emitida).

    Operação por agente: `tree` igual à união
    do catálogo, de `LOCAL_COMMANDS` e de `{MCP_COMMAND}`, com `MCP_COMMAND` disjunto dos outros dois; na fase do
    CLI, `cq mcp` com a entrada vazia (0, saída padrão vazia, só as linhas de `MCP_STDERR_MESSAGES`, varrida pelos
    mesmos detectores) e a conferência de que a invocação de uso incorreto de toda folha também rodou para `mcp` (2,
    sem o canário).

    Implantação e piloto: a seção do `doctor`. No
    banco do canário, as 6 globais do servidor (as 3 últimas nos resultados que a semente produz, a retenção pela regra
    de retenção sobre as linhas da semente) e as 4 reservadas do CLI fora da parte `api`; no `doctor` com o
    código 0, o relatório exato sem as fixas, com as 6 globais e as 2 do host "não verificado" (`no_host_probe`),
    e 3 avisos no resumo. Varridos pelos mesmos detectores, sem exclusão a mais de varredura ou de superfície.
    No esqueleto, o `collect` não emite as 3 últimas globais e o cliente põe as fixas.
    """
    # Disparo e resposta: todo texto da lista de textos do bot tem destino.
    _check_dispatch_text_names()
    # Relatórios: todo texto de `noticetexts` tem destino no canário.
    _check_report_text_names()
    monkeypatch.setattr(cq_cli, "stdout_is_terminal", lambda: False)
    monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: False)  # token do bot só por arquivo
    monkeypatch.setattr(cq_cli, "prompt_secret", _no_prompt)
    exchanges: list[_Exchange] = []
    _record_exchanges(client, exchanges)

    with capture_logs() as captured, warnings.catch_warnings(record=True) as caught:
        seed = _seed(client, clock)
        work = _worker_phase(queue, outbox, worker_thread, clock, db_url, seed)
        bot_run = _bot_phase(client, clock, sql, bot_api, bot_worker_thread, settings, seed)
        # Vínculo do aluno: a fase de alunos, antes da API (os 29 tipos de evento já existem na leitura).
        student_run = _student_phase(client, clock, sql, bot_api, bot_worker_thread, settings, smtp,
                                     mail_channel, seed)
        # Disparo e resposta: a fase de disparos, depois da de alunos e antes da API (os 43 tipos de evento já
        # existem na leitura); termina com o próprio worker parado.
        dispatch_run = _dispatch_phase(client, clock, sql, rich_bot_api, bot_worker_thread, settings, engine_fake,
                                       repo_fetcher)
        # Relatórios: a fase de relatórios e avisos à equipe, depois da de disparos e antes da API (os 46 tipos
        # de evento já existem na leitura); termina com os próprios workers parados.
        report_run = _report_phase(client, clock, sql, rich_bot_api, bot_worker_thread, worker_thread, settings,
                                   engine_fake, repo_fetcher, cli_invoke, db_url)
        api = _api_phase(client, clock, seed, bot_api)
        runs = _cli_phase(cli_invoke, client, clock, seed, api.state, tmp_path, bot_api, bot_run)
        # Vínculo do aluno: as 5 folhas, em oferecimentos próprios (sem tirar alvos do `state`).
        runs += _student_cli_phase(cli_invoke, client, clock, seed, tmp_path, bot_api, student_run)
        # Disparo e resposta: as 6 folhas (os dois últimos alvos de confirmação da semente de disparos).
        runs += _dispatch_cli_phase(cli_invoke, client, clock, seed, api.state, dispatch_run)
        # Relatórios: as 5 folhas, rodadas na fase de relatórios (sucesso e erro, `--csv` incluído).
        runs += report_run.runs
        # CLI: no banco do canário, o `doctor` com erro (o bot sem acesso do `BOT_OFFERING`) e o
        # `events list --person`, pelo `client` do canário.
        runs += _cli_canary_db_runs(cli_invoke, seed, clock, sql)  # implantação: o relógio e o banco do oráculo

    # CLI: a fase do expurgo, do `doctor` com sucesso e dos comandos locais, num banco próprio e com captura
    # própria (ver o comentário de `DOCTOR_PURGE_OPERATIONS`); as saídas do CLI dela entram nas varreduras de `runs`.
    r7_run = _purge_and_cli_phase(cli_invoke, clock, settings, bots, db_factory, tmp_path, monkeypatch)
    runs += r7_run.runs

    _check_seeded_events(api, seed, work)
    _check_bot_events(api, seed, bot_run, sql)
    # Relatórios (separação por id): a fase de alunos e a de disparos recebem, só para a exceção nomeada dos
    # eventos dos relatórios e da posse do worker da fase de relatórios (com a contagem exata), a fase de relatórios.
    _check_student_events(api, student_run, sql, report_run)  # vínculo do aluno
    _check_dispatch_events(api, dispatch_run, sql)  # disparo e resposta
    secrets = all_secrets()
    secrets += bot_secrets()  # as chaves-mestras de teste (fora de `all_secrets()`)
    # Vínculo do aluno: a senha do SMTP falso e as suas formas no fio (fora de `all_secrets()`), nas saídas; os
    # códigos lidos dos e-mails já estão em `all_secrets()` (`invite_links()`).
    secrets += mail_secrets(smtp)
    digests = _student_invite_digests(sql)  # os resumos de todos os convites de aluno

    # --- respostas da API: corpo e cabeçalhos ---
    api_canary = [(ex.method, ex.path, ex.status, hits) for ex in exchanges if (hits := _exchange_canary_hits(ex))]
    assert api_canary == [], f"identidade canário em respostas da API: {api_canary}"
    api_secrets = [(ex.method, ex.path, ex.status, hits) for ex in exchanges
                   if (hits := _exchange_secret_hits(ex, secrets))]
    assert api_secrets == [], f"segredo em respostas da API: {api_secrets}"
    # remetentes do Telegram nas respostas (fora a revelação) e segredos na URL dos pedidos.
    api_tg = [(ex.method, ex.path, ex.status, hits) for ex in exchanges if not ex.is_reveal_success
              and (hits := sorted(set(find_canary(ex.headers, prefixes=TG_PREFIXES))
                                  | set(find_canary(ex.body, prefixes=TG_PREFIXES))))]
    assert api_tg == [], f"remetente canário do Telegram em respostas da API: {api_tg}"
    url_secrets = [(ex.method, ex.path, hits) for ex in exchanges if (hits := find_secrets(ex.url, secrets))]
    assert url_secrets == [], f"segredo na URL de pedidos à API: {url_secrets}"

    # --- saídas do CLI ---
    cli_canary = [(r.args, r.json, hits) for r in runs if (hits := _cli_canary_hits(r))]
    assert cli_canary == [], f"identidade canário na saída do CLI: {cli_canary}"
    cli_secrets = [(r.args, r.json, hits) for r in runs if (hits := _cli_secret_hits(r, secrets))]
    assert cli_secrets == [], f"segredo na saída do CLI: {cli_secrets}"
    cli_tg = [(r.args, r.json, hits) for r in runs if not r.is_reveal_success
              and (hits := sorted({h for text in r.texts for h in find_canary(text, prefixes=TG_PREFIXES)}))]
    assert cli_tg == [], f"remetente canário do Telegram na saída do CLI: {cli_tg}"

    # --- logs (API e worker) ---
    records = captured.records
    assert all("_raw" not in record for record in records), "linha de log que não é JSON"
    log_canary = [(i, hits) for i, line in enumerate(captured.lines) if (hits := find_canary(line))]
    assert log_canary == [], f"identidade canário nos logs: {log_canary}"
    log_secrets = [(i, hits) for i, line in enumerate(captured.lines) if (hits := find_secrets(line, secrets))]
    assert log_secrets == [], f"segredo nos logs: {log_secrets}"
    log_tg = [(i, hits) for i, line in enumerate(captured.lines) if (hits := find_canary(line, prefixes=TG_PREFIXES))]
    assert log_tg == [], f"remetente canário do Telegram nos logs: {log_tg}"

    # --- saída padrão e de erro do processo e avisos (com controles positivos) ---
    _check_process_output(capfd, caught, secrets, ADMIN_TOKEN, digests)

    # --- bots: parâmetros enviados ao Telegram (descrição, comandos, texto das mensagens) ---
    assert _telegram_params_hits(bot_api) == [], "identidade canário nos parâmetros enviados ao Telegram"
    telegram_secrets = [(c.method, c.seq) for c in bot_api.calls()
                        if find_secrets(json.dumps(c.params, ensure_ascii=False), secrets)]
    assert telegram_secrets == [], f"segredo nos parâmetros enviados ao Telegram: {telegram_secrets}"
    # Controle positivo: o destino do `sendMessage` é o id do chat (identidade interna, fora da varredura).
    destinations = {hit for c in bot_api.calls("sendMessage")
                    for hit in find_canary(json.dumps(c.params["chat_id"]), prefixes=("tg-id",))}
    assert destinations == {f"tg-id:{u.user_id}" for u in CANARY_TELEGRAM_USERS}, destinations

    # Expurgo: as 5 colunas do expurgo em `offerings` têm linha preenchida no banco varrido, antes da varredura: o
    # oferecimento expurgado pelo `_api_phase` (o 1º dos `purge_targets`) e o alvo preparado e não expurgado (o 2º,
    # intacto depois da chamada canário do `confirmation_code`, que dá 409 sem mudança de estado).
    assert sql.execute("SELECT count(*) FROM offerings WHERE purged_at IS NOT NULL AND purge_result IS NOT NULL"
                       ).fetchone()[0] == 1
    assert sql.execute("SELECT purged_at IS NULL, purge_result IS NULL, purge_code_sha256 IS NOT NULL, "
                       "purge_code_expires_at IS NOT NULL, purge_prepared_counts IS NOT NULL FROM offerings "
                       "WHERE code = %s", (api.state.purge_targets[1][0],)).fetchone() == (True, True, True, True, True)
    # --- banco (SQL_SCAN) ---
    for table, columns in SQL_SCAN.items():
        count, hits = _scan_table(sql, table, columns)
        assert count > 0, f"a varredura de {table} não leu nenhuma linha"
        assert hits == {}, f"identidade canário em {table}: {hits}"
    # Disparo e resposta: a parcela medida das rodadas `dispatch.close` da fase de disparos,
    # lida antes das contagens. Ao menos uma rodada por disparo aberto da fase (do 1º ao 5º), todas
    # `done` (a barreira que não deixa fechar termina `done` sem efeito), e um único
    # `dispatch.closed` por disparo. Nenhum outro disparo abre no canário (os da semente são agendados para
    # T+1 dia e não há worker depois da fase), então toda `dispatch.close` é da fase.
    # Relatórios (separação por id): a semente de relatórios (`seed_state`: 1 disparo fechado, com uma única
    # rodada `dispatch.close` `r1` processada por SQL) e a fase de relatórios (os 3 imediatos fechados pelo roteiro
    # determinístico, ao menos uma rodada cada) também fecham disparos: as rodadas e os `dispatch.closed` deles são
    # separados por id dos da fase de disparos e conferidos exatamente logo abaixo; `n_close` continua a contar
    # todas as rodadas (a parcela medida das três origens).
    opened_labels = ("d1", "d2", "d3", "d4", "d5")
    close_rows = sql.execute("SELECT state, payload FROM tasks WHERE task_type = 'dispatch.close'").fetchall()
    n_close = len(close_rows)
    assert n_close >= len(opened_labels), f"rodadas dispatch.close: {n_close}"
    assert {state for state, _ in close_rows} == {"done"}, close_rows
    seed_close_rows = {row[0] for row in sql.execute(
        "SELECT id FROM dispatches WHERE dispatch_uuid = ANY(%s::uuid[])", (api.state.report_dispatches,)).fetchall()}
    report_close_rows = {report_run.dispatch_rows[label] for label in ("text", "number", "broken")}
    assert len(seed_close_rows) == 1 and len(report_close_rows) == 3, (seed_close_rows, report_close_rows)
    close_counts = Counter(payload["dispatch_row_id"] for _, payload in close_rows)
    assert {row: close_counts[row] for row in seed_close_rows} == {row: 1 for row in seed_close_rows}, close_counts
    assert all(close_counts[row] >= 1 for row in report_close_rows), close_counts
    assert {row for row in close_counts if row in seed_close_rows | report_close_rows} == (
        seed_close_rows | report_close_rows), close_counts
    close_rows = [(state, payload) for state, payload in close_rows
                  if payload["dispatch_row_id"] not in seed_close_rows | report_close_rows]
    assert {payload["dispatch_row_id"] for _, payload in close_rows} == {
        dispatch_run.dispatch_rows[label] for label in opened_labels}
    r6_closed_ids = {*api.state.report_dispatches, *(report_run.dispatches[label] for label in ("text", "number",
                                                                                                "broken"))}
    assert Counter(e["data"]["dispatch_id"] for e in api.events if e["type"] == "dispatch.closed"
                   and e["data"]["dispatch_id"] in r6_closed_ids) == Counter(dict.fromkeys(r6_closed_ids, 1))
    assert Counter(e["data"]["dispatch_id"] for e in api.events if e["type"] == "dispatch.closed"
                   and e["data"]["dispatch_id"] not in r6_closed_ids) == Counter({
        dispatch_run.dispatches[label]["dispatch_id"]: 1 for label in opened_labels})
    # Vínculo do aluno: 28 → 67 tarefas e 9 → 20 mensagens, com a fase de alunos e as chamadas
    # do vínculo (ver as contagens por tipo e estado abaixo).
    # Disparo e resposta: 67 → 117 + n_close tarefas e 20 → 40 mensagens, com a fase de disparos
    # (27 tarefas + n_close; 20 mensagens) e a semente de disparos com as chamadas do disparo (23 tarefas; nenhuma
    # mensagem): ver as parcelas por tipo e estado e por destino abaixo.
    # Disparo e resposta: o passo 5 da fase de disparos (o 3º e o 4º
    # disparos, com os textos que faltavam) soma 20 tarefas (117 → 137 + n_close) e 13 mensagens (40 → 53).
    # Disparo e resposta: o 5º disparo da fase (a múltipla degradada, sem
    # resposta) soma 4 tarefas (137 → 141 + n_close; as rodadas `dispatch.close` dele entram em `n_close`) e 3
    # mensagens (53 → 56).
    # Mensagens livres, gabarito e nota parcial (com o passo 5.8, o do "Não sei" de antes do fechamento):
    # as mensagens livres opcionais, o gabarito só a quem respondeu e a nota parcial, no roteiro da fase de disparos
    # (a confirmação na escolha única não entra: a fase não a tem, e a múltipla do 4º tem o "Enviar"). Tarefas,
    # 141 + n_close → 145 + n_close: + 2 da nota parcial (o update da resposta de A ao 5º, passo 5.6b, e a correção dela)
    # e + 2 das mensagens livres (os updates das duas mensagens do modo desligado, passo 5.9; o `PATCH` não cria tarefa); a
    # regra do gabarito não soma tarefa (o toque antigo e o `/naosei` do passo 5.8 já eram updates; o "Não sei" atrasado não
    # corrige). Mensagens, 56 → 59: + 1 da regra do gabarito no passo 5 (a edição do "Não sei" atrasado do 4º; o
    # ALREADY_ANSWERED do `/naosei` toma o lugar do WINDOW_CLOSED, sem mudar o total; a edição de fechamento da
    # `no_answer` do 4º, sem o gabarito, toma o lugar da revelação dela), + 1 da nota parcial no 5º (o feedback ◐; a
    # revelação no fechamento vai à entrega respondida) e + 1 das mensagens livres (a resposta com o contato à 1ª
    # mensagem do passo 5.9; a 2ª não tem resposta, pelo limitador do contato).
    # Conferido parcela a parcela, passo a passo pelo roteiro de `_dispatch_phase`
    # e pelas regras da fila. Tarefas: conferidas. A
    # fase de disparos soma 55 + n_close: 1 `bot.sync_profile` (o registro do bot da fase); 22 `telegram.update`
    # (um por update, ver `update_rows` abaixo); 14 `dispatch.validate` (um passo externo por execução: 3 da
    # canário, com a obtenção nova, a inspeção e a variante; 1 da não suportada, obtenção reaproveitada e inspeção
    # que rejeita; 2 de cada um dos 5 disparos abertos, inspeção e variante, porque cada `qid` é uma referência
    # nova no mesmo `repo@commit`); 5 `dispatch.open` (um por confirmação de imediato); 7
    # `delivery.prepare` (uma por entrega `pending` na abertura: A e B no 1º e no 2º, porque o
    # endereço de B só fica `blocked` com o 403; só A no 3º, no 4º e no 5º); 6 `submission.grade` (uma por
    # submissão `grading`: a de A ao 1º, as duas ao 2º, as duas ao 3º e a do 5º; a parte do 4º, os "Não sei", as
    # recusas, a figurinha e as mensagens livres não corrigem); nenhuma `delivery.reveal` (toda revelação sai na
    # transação do fechamento ou do "Não sei" atrasado) nem `question.refetch`; e `n_close`. 27 + 20 + 4 + 2 + 2 =
    # 55. A semente de disparos e as chamadas do disparo: 23 (ver as parcelas por tipo abaixo). Mensagens: a fase soma
    # 41: 2 `HELP`, 7 textos fixos a A (o `FREE_MESSAGE_REPLY`, o `ANSWER_KEPT` da falha interna do 2º,
    # o `USE_REPLY`, o `REPLY_WITH_TEXT`, o `FORMAT_ERROR` do 3º, o `ALREADY_ANSWERED` do `/naosei` e a resposta
    # com o contato), 2 figuras, 6 questões, 8 pedidos de campo e 14 edições enviados, a questão de B no 2º
    # (`failed`) e o pedido de B no 2º (`cancelled`);
    # a derivação por passo está na fila do bot,
    # em `_dispatch_phase`.
    # Relatórios: 145 + n_close → 145 + n_close + 72 + n_opened_rule + n_notice_send tarefas e
    # 61 → 61 + 43 mensagens, parcela por parcela (as tarefas por tipo e estado e as
    # mensagens por destino estão abaixo):
    # - nas fases de bot, de alunos e de disparos (os workers com bots já têm os tratadores dos relatórios; nenhuma das três fases tem membro da
    #   equipe com endereço ativo, então nenhuma mensagem nova): na fase de alunos, 1 `notice.invite_window` (a janela
    #   das falhas `a_rcpt` e `a_auth`; registrada por um dos workers com bots depois de `t0 + 900 s`, o da própria
    #   fase depois do salto de `EXPIRY_ADVANCE` ou um seguinte, sempre uma única vez); na fase de disparos, 6
    #   `preview.send` (a canário e do 1º ao 5º; sem destinatário, uma rodada cada, sem chamar o motor), 1
    #   `notice.dispatch_rejected`, 1 `notice.question_failed` (o 2º) e 5 `report.freeze` (do 1º ao 5º): 14;
    # - na semente de relatórios (`report_targets` padrão, sem worker): 1 `bot.sync_profile` pendente (o
    #   registro do oferecimento novo), 1 `dispatch.validate` (só a variante: a obtenção e a inspeção da questão da
    #   semente já existem), 1 `dispatch.open` e 1 `report.freeze`, concluídas (a `dispatch.close` dela entra em
    #   `n_close`; o "aberto" registrado pelo gancho do fechamento é entregue pelo congelamento, sem tarefa): 4;
    # - na fase de relatórios (`_report_phase`): `bot.sync_profile` 3 concluídas (os dois registros e a troca de token) e 1
    #   falha (o perfil arquivado do bot sem acesso); 8 `telegram.update` (o desbloqueio, o
    #   toque e a resposta ao cabeçalho pelo Telegram; a resposta à prévia da canário e as 4 respostas de A pelo
    #   console); 14 `dispatch.validate` (3 da obtenção nova, inspeção e variante do 1º; 2 de cada um dos outros 5
    #   suportados; 1 da não suportada); 1 `dispatch.expire`; 6 `preview.send` (uma rodada cada: 5 entregues e a que
    #   desiste); 1 `notice.dispatch_rejected`; 1 `notice.dispatch_cancelled` (`expired`); 2 `notice.access_episode`
    #   (a volta do bot e o arquivamento sem a volta); 3 `dispatch.open`; 3 `delivery.prepare` (só A); 4
    #   `submission.grade` (o texto, o erro de formato e a resposta certa do número, a quebrada); 1
    #   `notice.question_failed`; 3 `report.freeze`; 3 `digest.daily` (`queued`, `missed`, `no_activity`): 54;
    # - medidas (o "aberto" é registrado pela regra `notice.dispatch_opened` ou, se a varredura não passou antes do
    #   fechamento, pelo gancho do fechamento, entregue pelo congelamento ou por uma `notice.send`):
    #   `n_opened_rule`, as rodadas da regra (ao menos as 3 da fase de relatórios, que espera o aviso antes de fechar; no
    #   máximo as 5 da fase de disparos a mais), e `n_notice_send`, as da regra `notice.send` (os 2 reenvios da fase de
    #   relatórios e, no máximo, um por "aberto" registrado pelo gancho numa fase com worker).
    # Mensagens: só a fase de relatórios enfileira (ao membro com endereço ativo; o bloqueado não é destinatário): pelo
    # Telegram, a prévia do agendado (cabeçalho e questão), a rejeição, o cancelamento e a volta do bot (5) e a
    # `PREVIEW_REPLY` (1); pelo console, as 4 prévias (cabeçalho, questão e pedido do campo: 12), a `PREVIEW_REPLY`
    # (1), os 3 "aberto", a falha de questão, os 3 resumos de fechamento, os 2 reenviados e o resumo diário (10); a A,
    # as mensagens de entrega (o texto: questão, pedido e as linhas do feedback e da revelação, 4; o número: questão,
    # dois pedidos e as linhas do erro de formato, do feedback e da revelação, 6; a quebrada: questão, pedido e a linha
    # da falha, 3) e o `FORMAT_ERROR` (1): 6 + 23 + 14 = 43.
    n_opened_rule = sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'notice.dispatch_opened'").fetchone()[0]
    n_notice_send = sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'notice.send'").fetchone()[0]
    assert 3 <= n_opened_rule <= 3 + 5, f"rodadas notice.dispatch_opened: {n_opened_rule}"
    assert 2 <= n_notice_send <= 2 + (3 + 5 - n_opened_rule), f"rodadas notice.send: {n_notice_send}"
    assert sql.execute("SELECT count(*) FROM tasks").fetchone()[0] == (
        67 + 50 + 20 + 4 + 2 + 2 + n_close + (14 + 4 + 54) + n_opened_rule + n_notice_send)
    assert sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == (
        20 + (20 + 1) + (13 + 1 + 1) + (3 + 1) + 1 + (6 + 23 + 14))
    stored_types = {row[0] for row in sql.execute("SELECT DISTINCT type FROM events").fetchall()}
    assert stored_types <= set(EVENT_TYPES), f"tipos fora de EventType: {stored_types - set(EVENT_TYPES)}"
    # as contagens por tipo e estado. `bot.sync_profile`: 1 da fase de bot (concluída), 9 dos
    # registros de `seed_state`, 1 da troca de token e 1 do arquivamento de VALID_CALLS, 2 registros e
    # 2 arquivamentos pelo CLI (sem worker: pendentes); `telegram.update`: os 10 updates da fase de bot.
    # Vínculo do aluno: `bot.sync_profile` + 1 da fase de alunos (concluída), + 9 dos registros
    # do vínculo em `seed_state` (o `invite_offering` e os 8 alvos de envio) e + 1 do oferecimento do CLI de
    # alunos (pendentes); `telegram.update` + os 11 updates da fase de alunos; `invite.email`: 7
    # concluídas e 2 falhas definitivas na fase de alunos (ver `_student_phase`), e 8 pendentes (sem
    # worker): 1 da confirmação e 1 do reenvio de VALID_CALLS, e 2 da confirmação e 4 dos dois reenvios
    # pelo CLI.
    # Disparo e resposta: `bot.sync_profile` + 1 da fase de disparos (concluída: o worker da fase
    # configura o perfil) e + 1 do registro do `dispatch_offering` de `seed_state` (pendente);
    # `telegram.update` + os 9 updates da fase de disparos (os dois `/start`, a resposta de A, os dois
    # toques de B, as duas mensagens livres e as duas respostas de A ao 2º disparo); `dispatch.validate`
    # (um passo externo por execução; a obtenção nova é um passo, a reaproveitada segue
    # na mesma execução, e a inspeção é pulada na referência já inspecionada): na fase, 3 da questão
    # canário (obtenção nova, inspeção, variante), 1 da não suportada (obtenção reaproveitada e inspeção
    # que rejeita), 2 do 1º disparo e 2 do 2º (inspeção, variante), todas concluídas; na semente, 3 do 1º
    # disparo validado e 1 de cada um dos outros 8, concluídas, e as 8 dos alvos de cancelamento,
    # pendentes (sem worker depois da semente); + 1 pendente do `dispatches.prepare` de VALID_CALLS e + 2
    # pendentes dos dois preparos pelo CLI (o cancelamento não mexe na tarefa); `dispatch.open`: 1 por
    # confirmação de disparo imediato da fase (2; a confirmação dos agendados da semente não enfileira);
    # `delivery.prepare`: 1 por entrega aberta (2 + 2); `submission.grade`: a resposta de A ao 1º disparo e
    # as duas ao 2º (a recusada pelo motor e a quebrada); `dispatch.close`: a parcela medida `n_close`.
    # Disparo e resposta (passo 5 da fase): `telegram.update` + os 10 updates novos (a mensagem livre ambígua, a
    # figurinha, as duas respostas ao 3º e a da 1ª parte do 4º, o `/naosei` e os quatro toques: a versão antiga
    # do 3º, o "Não sei" durante a correção, o "Não sei" antigo do 4º e o do 2º); `dispatch.validate` + 2 do 3º
    # e 2 do 4º (inspeção, variante; a obtenção é reaproveitada); `dispatch.open` + 2; `delivery.prepare` + 1
    # por disparo (só a entrega de A: a de B, com o endereço bloqueado, vira `not_delivered` na abertura, sem
    # tarefa); `submission.grade` + 2 (o erro de formato e a resposta certa do 3º; a correção presa espera no
    # motor dentro da mesma execução, sem nova rodada). A parte do 4º, as recusas e a figurinha não corrigem.
    # Disparo e resposta (o 5º disparo da fase): `dispatch.validate` + 2 (inspeção, variante; a obtenção é
    # reaproveitada); `dispatch.open` + 1; `delivery.prepare` + 1 (só a entrega de A); nenhum update nem correção
    # (A não responde).
    # Mensagens livres e nota parcial: `telegram.update` + 1 da nota parcial (a resposta de A ao 5º,
    # passo 5.6b) e + 2 das mensagens livres (as duas mensagens do modo desligado, passo 5.9); `submission.grade` + 1 da nota parcial
    # (a correção da resposta ao 5º, nota 1/6). O passo 5.8, do gabarito só a quem respondeu, não muda as tarefas.
    # Conferido parcela a parcela pelo roteiro.
    # `telegram.update` 22 na fase (2 + 5 + 2 + 7 + 1 + 3 + 2: os dois `/start`; no 1º, a resposta de A,
    # os dois toques de B e as duas mensagens livres; as duas respostas ao 2º; no passo 5, a ambígua, a figurinha,
    # as duas respostas ao 3º, o toque antigo e o toque durante a correção do 3º e a parte do 4º; a resposta ao 5º;
    # o toque antigo no 4º, o `/naosei` e o toque no 2º; as duas do 5.9), uma tarefa por update (nenhum espera a
    # transição: as entradas no prazo chegam antes do fechamento, e as do passo 5.8 depois dele); na
    # semente, 3 + 8 `dispatch.validate` concluídas (a obtenção nova e a inspeção acontecem uma vez para os 9
    # disparos da mesma questão, e cada um termina com a sua variante: 9 + 2), 8 pendentes dos alvos de
    # cancelamento, 1 do `dispatches.prepare` de VALID_CALLS e 2 dos preparos pelo CLI (sem worker depois de
    # `seed_state`, que roda no `_api_phase`); `offerings.update_settings` (o `PATCH` do 5.9, o de VALID_CALLS e as
    # chamadas canário) não cria tarefa.
    # Relatórios: por tipo e estado, as parcelas do comentário das contagens globais acima.
    # `bot.sync_profile`: + 3 concluídas e 1 falha da fase de relatórios e + 1 pendente da semente de relatórios;
    # `telegram.update` + 8 da fase de relatórios; `dispatch.validate` + 1 da semente e + 14 da fase de relatórios;
    # `dispatch.open` + 1 da semente e + 3 da fase de relatórios; `delivery.prepare` + 3 e `submission.grade` + 4 da mesma
    # fase; os 13 tipos dos relatórios (`report.freeze`, `preview.send`, `digest.daily` e as 8 regras de `teamnotices`,
    # das quais `notice.invite_batch` não sai: nenhum lote com falha): `preview.send` 6 da fase de disparos + 6 da de
    # relatórios; `report.freeze` 5 da fase de disparos + 1 da semente + 3 da fase de relatórios; `notice.dispatch_rejected` 1 + 1;
    # `notice.question_failed` 1 + 1; `notice.dispatch_cancelled` 1, `notice.access_episode` 2 e `digest.daily` 3,
    # da fase de relatórios; `notice.invite_window` 1, da janela da fase de alunos; e as medidas `n_opened_rule` e
    # `n_notice_send` (ambas positivas: ao menos 3 e 2). A `dispatch.close` da semente e as da fase de relatórios entram em
    # `n_close` (bloco do `n_close` acima).
    assert {(t, s): n for t, s, n in sql.execute(
        "SELECT task_type, state, count(*) FROM tasks GROUP BY task_type, state").fetchall()} == {
        ("privacy.direct_fail", "failed"): 1, ("privacy.permanent", "failed"): 1,
        ("bot.sync_profile", "done"): 2 + 1 + 3, ("bot.sync_profile", "pending"): 25 + 1 + 1,
        ("bot.sync_profile", "failed"): 1,
        ("telegram.update", "done"): 21 + 9 + 10 + 1 + 2 + 8,
        ("invite.email", "done"): 7, ("invite.email", "failed"): 2, ("invite.email", "pending"): 8,
        ("dispatch.validate", "done"): (3 + 1 + 2 + 2 + 2 + 2 + 2) + (3 + 8) + 1 + 14,
        ("dispatch.validate", "pending"): 8 + 1 + 2,
        ("dispatch.open", "done"): 2 + 2 + 1 + 1 + 3, ("delivery.prepare", "done"): 2 + 2 + 1 + 1 + 1 + 3,
        ("submission.grade", "done"): 1 + 2 + 2 + 1 + 4, ("dispatch.close", "done"): n_close,
        ("dispatch.expire", "done"): 1, ("preview.send", "done"): 6 + 6, ("report.freeze", "done"): 5 + 1 + 3,
        ("notice.dispatch_rejected", "done"): 1 + 1, ("notice.question_failed", "done"): 1 + 1,
        ("notice.dispatch_cancelled", "done"): 1, ("notice.access_episode", "done"): 2,
        ("notice.invite_window", "done"): 1, ("digest.daily", "done"): 3,
        ("notice.dispatch_opened", "done"): n_opened_rule, ("notice.send", "done"): n_notice_send}
    # As 3 mensagens da API base e as 6 da fase de bot: (estado, com bot, com pessoa).
    # Vínculo do aluno: + as 11 da fase de alunos, todas entregues: 3 ao endereço da pessoa
    # (as duas boas-vindas e o "já vinculado") e 8 ao chat do update.
    # Disparo e resposta: + as 20 da fase de disparos (ver a fila do bot em `_dispatch_phase`): ao
    # chat do update, entregues, os dois `HELP` e a resposta fixa à 1ª mensagem livre (3); ao endereço da
    # pessoa (as mensagens de entrega, sem texto), entregues: as 2 figuras, as 3 questões, os 4 pedidos de
    # campo e as 6 edições (15); a questão do 2º disparo a B, recusada com 403 (1, `failed`); o pedido de
    # campo do 2º disparo a B, cancelado com a entrega `not_delivered` (1, `cancelled`). A semente de
    # disparos e as chamadas do disparo não enfileiram mensagem (nenhum disparo delas abre).
    # Disparo e resposta (passo 5 da fase): + 13, todas entregues: ao endereço de A (as mensagens de entrega, sem
    # texto), a questão do 3º e a do 4º, os 3 pedidos de campo (o do 3º, o dele de novo depois do erro de
    # formato e o da 1ª parte do 4º) e as 5 edições (o erro de formato, o feedback ✅ e a revelação do 3º; o
    # progresso e a revelação do 4º) (10); ao chat do update, as respostas fixas USE_REPLY, REPLY_WITH_TEXT e o
    # WINDOW_CLOSED do `/naosei` (3). As entregas de B no 3º e no 4º não enfileiram nada (endereço bloqueado).
    # Disparo e resposta (o 5º disparo da fase): + 3, entregues ao endereço de A: a questão (com a dica
    # `TYPED_MULTIPLE_HOWTO`), o pedido do campo e a revelação (`no_answer`). A entrega de B não enfileira nada.
    # Mensagens livres, gabarito e nota parcial: ao endereço de A, + 1 no passo 5 (gabarito: o 4º tem o progresso,
    # a edição de fechamento da `no_answer`, sem o gabarito, e a edição do "Não sei" atrasado, com a revelação; 10
    # → 11) e + 1 no 5º (nota parcial: a questão, o pedido do campo, o feedback ◐ e a revelação da entrega respondida; 3 →
    # 4); ao chat do update, o ALREADY_ANSWERED do `/naosei` no lugar do WINDOW_CLOSED (pela mesma regra; os 3 do passo 5
    # continuam 3) e + 1 das mensagens livres (a resposta com o contato do passo 5.9, sem pessoa).
    # Conferido parcela a parcela (a derivação por passo está na fila do bot, em
    # `_dispatch_phase`): ao chat do update, sem pessoa, ficam só os dois `HELP` do passo 0 (o `/start` sem código
    # segue a fase de bot); os textos fixos a A saem pelo endereço vigente dela, com a pessoa (pelo
    # endereço vigente, `_reply`, a forma (b) dos bots; os avisos da correção pelo mesmo endereço),
    # e entram o `ANSWER_KEPT` do passo 4 e o `FORMAT_ERROR` do passo 5. Com a pessoa e
    # entregues, por fase: 15 + 2 no 1º e no 2º disparos (as 15 mensagens de entrega; o `FREE_MESSAGE_REPLY` e o
    # `ANSWER_KEPT`); 11 + 4 no passo 5 (as 11 mensagens de entrega do 3º e do 4º; o `USE_REPLY`, o
    # `REPLY_WITH_TEXT`, o `FORMAT_ERROR` e o `ALREADY_ANSWERED`); 4 no 5º;
    # 1 no 5.9 (a resposta com o contato).
    # Relatórios (esta comparação por destino é contagem global de tabela):
    # + as 43 da fase de relatórios, todas entregues. Com a pessoa (a forma (b): o
    # membro com endereço ativo, ou A): os avisos e as prévias ao membro (5 pelo Telegram; 22 pelo console) e, a A, as
    # 13 mensagens de entrega e o `FORMAT_ERROR` (41); ao chat do update, sem pessoa, as 2 `PREVIEW_REPLY` (pelo
    # `Outbox.enqueue` "ao chat do update", com `reply_to_update_id`, como o `HELP` da fase de bot).
    assert {(s, b, p): n for s, b, p, n in sql.execute(
        "SELECT state, bot_id IS NOT NULL, recipient_person_id IS NOT NULL, count(*) FROM outbox_messages "
        "GROUP BY 1, 2, 3").fetchall()} == {
        ("failed", False, True): 2, ("sent", False, True): 1,
        ("sent", True, True): 4 + (15 + 2) + (11 + 4) + 4 + 1 + (5 + 22 + 14),
        ("sent", True, False): 11 + 2 + 2, ("failed", True, False): 1, ("failed", True, True): 1 + 1,
        ("cancelled", True, True): 1}

    # --- varredura genérica do banco (todas as colunas de todas as tabelas) ---
    db_hits = _generic_db_scan(db_url)
    tg_ids = {key: {h for h in found if h.startswith("tg-id:")} for key, found in db_hits.items()}
    others = {key: {h for h in found if not h.startswith("tg-id:")} for key, found in db_hits.items()}
    ids_outside = {key: sorted(found) for key, found in tg_ids.items()
                   if found and key not in INTERNAL_IDENTITY_COLUMNS}
    assert ids_outside == {}, f"id de remetente do Telegram fora das colunas internas: {ids_outside}"
    allowed = {("outbox_messages", "text")}
    names_outside = {key: sorted(found) for key, found in others.items()
                     if found and key[0] != "identities" and key not in allowed}
    assert names_outside == {}, f"identidade canário no banco fora de identities e da entrega: {names_outside}"
    # Controles positivos: cada coluna interna tem o id dos remetentes; identities tem as identidades.
    all_tg_ids = {f"tg-id:{u.user_id}" for u in CANARY_TELEGRAM_USERS}
    assert tg_ids.get(("telegram_updates", "chat_id")) == all_tg_ids
    assert tg_ids.get(("telegram_updates", "sender_id")) == all_tg_ids
    assert tg_ids.get(("delivery_endpoints", "chat_id")) == {f"tg-id:{TG_TEODORICO.user_id}"}
    in_identities = set().union(*(found for key, found in others.items() if key[0] == "identities"))
    assert {f"nome:{p.name}" for p in CANARY_PEOPLE} | {f"tg-nome:{TG_ZEFIRO.full_name}"} <= in_identities, (
        sorted(in_identities))
    # A exceção de identities é exata para os alvos do Telegram: só os que coincidem com uma
    # pessoa da semente base (os nomes e as palavras de TG_ZEFIRO, que é ZEFIRO); nenhum alvo de TG_TEODORICO nem
    # de TG_FILISBINA, nenhum @username e nenhum tg-id.
    assert TG_ZEFIRO.full_name == ZEFIRO.name
    tg_in_identities = {hit for key, found in db_hits.items() if key[0] == "identities" for hit in found
                        if hit.split(":", 1)[0] in TG_PREFIXES}
    assert tg_in_identities == {
        f"tg-nome:{TG_ZEFIRO.full_name}", f"tg-nome:{TG_ZEFIRO.first_name}", f"tg-nome:{TG_ZEFIRO.last_name}",
        "tg-palavra:zefiro", "tg-palavra:canario", "tg-palavra:quintela"}, sorted(tg_in_identities)
    assert f"nome:{ZEFIRO.name}" in others.get(("outbox_messages", "text"), set()), "a entrega m3 tem o nome"

    # --- controles positivos: o mesmo detector acha o que deve achar ---
    names = {f"nome:{p.name}" for p in CANARY_PEOPLE}
    reveal_bodies = [ex.body for ex in exchanges if ex.is_reveal_success]
    assert reveal_bodies, "nenhuma revelação concedida foi gravada"
    assert names <= set(find_canary(reveal_bodies[0])), "o detector não achou a identidade na revelação da API"
    cli_reveals = [r for r in runs if r.is_reveal_success]
    assert sorted(r.json for r in cli_reveals) == [False, True]
    for reveal_run in cli_reveals:
        assert names <= set(find_canary(reveal_run.stdout)), f"revelação pelo CLI (json={reveal_run.json})"
    assert f"nome:{ZEFIRO.name}" in find_canary(work.console_text), "o console é a entrega e tem o nome"
    escaped_line = json.dumps({"level": "INFO", "logger": "cq_server.api", "event": "request",
                               "detail": ZEFIRO.name})
    assert "\\u00e9" in escaped_line
    assert f"nome:{ZEFIRO.name}" in find_canary(escaped_line)
    formatted_line = JsonFormatter().format(logging.makeLogRecord(
        {"name": "cq_server.api", "levelname": "INFO", "levelno": logging.INFO, "msg": "sintetico",
         "event": f"sintetico {ZEFIRO.name}"}))
    assert f"nome:{ZEFIRO.name}" in find_canary(formatted_line)
    for header in (f"X-Eco: {CANARY_ASCII}", f"X-Eco: {ZEFIRO.name}"):
        synthetic = _Exchange("GET", "/sintetico", 200, header, "{}")
        assert f"nome:{ZEFIRO.name}" in _exchange_canary_hits(synthetic), header
    assert find_canary(UNKNOWN_OPTION), "a opção desconhecida precisa carregar o nome canário"
    identity_rows, identity_hits = _scan_table(sql, "identities", ["full_name", "ra", "email", "email_normalized"])
    found = set().union(*identity_hits.values()) if identity_hits else set()
    expected_identity = names | {f"ra:{p.ra}" for p in CANARY_PEOPLE if p.ra} | {
        f"email:{p.email}" for p in CANARY_PEOPLE if p.email}
    assert identity_rows >= 3 and expected_identity <= found, f"varredura de identities achou {sorted(found)}"
    creations = [ex for ex in exchanges if ex.is_token_creation]
    assert creations and all(find_secrets(ex.body, secrets) for ex in creations), (
        "o detector de segredos não achou o segredo no 201 de tokens.create")
    # o 201 de `team.link` tem o próprio link (a exceção é exata: só ele e o seu código).
    link_creations = [ex for ex in exchanges if ex.is_team_link_creation]
    assert len(link_creations) == 4, "201 de team.link: 2 da fase de bot, 1 de VALID_CALLS e 1 do CLI"
    for ex in link_creations:
        own = _own_link_secrets(ex)
        assert len(own) == 2 and own <= set(secrets), "o link do 201 de team.link não está no registro"
        assert find_secrets(ex.body, sorted(own)), "o detector não achou o link no 201 de team.link"
        assert _exchange_secret_hits(dataclasses.replace(ex, path="/v1/outra-rota"), secrets), (
            "fora do 201 de team.link, o link é acusado")
    # a URL de cada pedido foi gravada, e o detector de remetentes acha nome e `@username`.
    assert {ex.url.split("/")[2] for ex in exchanges} == {"testserver"}, "URL dos pedidos não gravada"
    synthetic_tg = f"X-Eco: {TG_TEODORICO.full_name} @{TG_TEODORICO.username}"
    assert {f"tg-nome:{TG_TEODORICO.full_name}", f"tg-username:{TG_TEODORICO.username}"} <= set(
        find_canary(synthetic_tg, prefixes=TG_PREFIXES))

    # --- contra o vazio ---
    tree = _cli_leaves(cq_cli.cli)
    # CLI: as folhas do catálogo e os comandos locais, conjuntos disjuntos.
    assert not LOCAL_COMMANDS & {op["cli"] for op in load_catalog()["operations"]}
    # Operação por agente: a folha `mcp`, fora do catálogo e dos comandos locais.
    assert cq_cli.MCP_COMMAND not in {op["cli"] for op in load_catalog()["operations"]} | LOCAL_COMMANDS
    assert tree == {op["cli"] for op in load_catalog()["operations"]} | LOCAL_COMMANDS | {cq_cli.MCP_COMMAND}
    assert {r.leaf for r in runs if r.exit_code == 0} == tree, "folhas sem invocação com sucesso"
    assert {r.leaf for r in runs if r.exit_code != 0} == tree, "folhas sem invocação com erro"
    # as 10 folhas (`OFFERING_BOT_CLI_LEAVES`) estão na árvore, e os 14 tipos (`OFFERING_BOT_EVENT_TYPES`) existem e saíram.
    assert OFFERING_BOT_CLI_LEAVES <= tree, f"folhas fora do CLI: {sorted(OFFERING_BOT_CLI_LEAVES - tree)}"
    assert set(OFFERING_BOT_EVENT_TYPES) <= set(EVENT_TYPES), (
        f"tipos fora de EventType: {sorted(set(OFFERING_BOT_EVENT_TYPES) - set(EVENT_TYPES))}")
    assert set(OFFERING_BOT_EVENT_TYPES) <= {e["type"] for e in api.events}, "tipo de evento que não saiu"

    requests = [r for r in records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]
    assert Counter((r.get("method"), r.get("status")) for r in requests) == Counter(
        (ex.method, ex.status) for ex in exchanges), "o log não tem um registro `request` por requisição"
    for op in load_catalog()["operations"]:
        expected_request = {"logger": "cq_server.api", "event": "request", "method": op["method"],
                            "route": op["path"], "status": SUCCESS_STATUS.get(op["id"], 200)}
        assert _has_record(requests, expected_request), f"sem registro de request para {op['id']}"
    assert _has_record(requests, {"logger": "cq_server.api", "event": "request", "method": "GET",
                                  "route": None, "status": 404}), "sem registro da rota inexistente"
    # Pseudônimo de formato válido com o RA canário: o 404 veio do domínio, na rota do catálogo.
    for method, route in (("GET", "/v1/people/{ref}"), ("POST", "/v1/tokens/{token_id}/revoke")):
        assert _has_record(requests, {"logger": "cq_server.api", "event": "request", "method": method,
                                      "route": route, "status": 404}), f"sem registro do 404 de {route}"

    assert _has_record(records, {"logger": "cq_server.worker", "event": "task.processed", "id": work.t2,
                                 "state": "failed"})
    for message_id, final_state in ((work.m1, "failed"), (work.m2, "pending"), (work.m2, "failed"),
                                    (work.m3, "sent")):
        assert _has_record(records, {"logger": "cq_server.worker", "event": "outbox.dispatched",
                                     "id": message_id, "state": final_state}), (message_id, final_state)
    # Bots (por inclusão): os registros do worker com bots também foram varridos.
    update_rows = sql.execute("SELECT id, outcome FROM telegram_updates ORDER BY id").fetchall()
    # Vínculo do aluno: 10 da fase de bot + 11 da fase de alunos.
    # Disparo e resposta: + os 9 da fase de disparos.
    # Disparo e resposta (passo 5 da fase): + os 10 do passo 5 da fase de disparos.
    # Mensagens livres e nota parcial: + 1 da nota parcial (resposta de A ao 5º, passo 5.6b) e + 2 das mensagens livres
    # (as duas mensagens do modo desligado, passo 5.9).
    # Conferido parcela a parcela. A fase de disparos
    # grava 22 updates, os 22 rótulos de `updates` em `_dispatch_phase`: 2 do passo 0, 5 do 3º (a resposta de A,
    # os dois toques de B e as duas mensagens livres), 2 do 2º, 7 do passo 5 antes do 5.6b (a ambígua, a
    # figurinha, as duas respostas ao 3º, os dois toques no 3º e a parte do 4º), 1 do 5.6b, 3 do 5.8 (o toque
    # antigo no 4º, o `/naosei` e o toque no 2º) e 2 do 5.9; 9 + 10 + 1 + 2 = 22. Nenhuma outra fase grava update
    # depois da de disparos (sem worker nem leitor).
    # Relatórios: + os 8 da fase de relatórios, depois da de disparos: pelo Telegram, o
    # desbloqueio do chat do membro (`not_handled`), o toque no botão da prévia (`preview_tapped`) e a resposta ao
    # cabeçalho (`preview_replied`); pelo console (`update_id` negativo), a resposta do membro à prévia da canário
    # (`preview_replied`) e as 4 respostas de A (o texto, o número com a identidade de B, que dá erro de formato, o
    # número certo e a resposta à quebrada). Nenhuma outra fase grava update depois da de relatórios.
    assert len(update_rows) == 21 + 9 + 10 + 1 + 2 + 8
    for row_id, outcome in update_rows:
        assert _has_record(records, {"logger": "cq_server.worker", "event": "telegram.update",
                                     "update_row_id": row_id, "outcome": outcome}), (row_id, outcome)
    assert _has_record(records, {"logger": "cq_server.worker", "event": "bot.lease", "change": "acquired"}), (
        "sem registro bot.lease da posse assumida")
    assert _has_record(records, {"logger": "cq_server.worker", "event": "bot.poll_problem",
                                 "problem": "rate_limited"}), "sem registro bot.poll_problem da espera"

    # --- vínculo do aluno: logs da tarefa `invite.email`, alvos de aluno e códigos em todas as superfícies ---
    _check_student_logs(records, student_run)
    _check_student_surfaces(exchanges, runs, captured.lines, bot_api, smtp, db_url, student_run, digests)

    # --- vínculo do aluno: contra o vazio ---
    assert STUDENT_LINK_CLI_LEAVES <= tree, f"folhas fora do CLI: {sorted(STUDENT_LINK_CLI_LEAVES - tree)}"
    assert {op["cli"] for op in load_catalog()["operations"] if op["id"] in STUDENT_LINK_OPERATIONS} == STUDENT_LINK_CLI_LEAVES
    assert set(STUDENT_LINK_EVENT_TYPES) <= set(EVENT_TYPES), (
        f"tipos fora de EventType: {sorted(set(STUDENT_LINK_EVENT_TYPES) - set(EVENT_TYPES))}")
    assert set(STUDENT_LINK_EVENT_TYPES) <= {e["type"] for e in api.events}, "tipo de evento que não saiu"
    course, term = student_run.course, student_run.term
    new_texts = {_compose_student(t, course, term) for t in (TERMS, WELCOME_STUDENT, TERMS_REFUSED, INVITE_REQUESTED)}
    assert len(new_texts) == 4
    sent_texts = {c.params["text"] for c in bot_api.calls("sendMessage", bot_id=student_run.bot_id)
                  if c.outcome == "ok"}
    assert new_texts <= sent_texts, "texto do bot que não saiu pelo sendMessage"
    assert {link.bot_username for link in smtp.invite_links()} == {student_run.username}, "sem e-mail de convite"
    outcomes = {row[0] for row in sql.execute("SELECT DISTINCT outcome FROM telegram_updates").fetchall()}
    assert STUDENT_LINK_OUTCOMES <= outcomes, f"desfechos que não apareceram: {sorted(STUDENT_LINK_OUTCOMES - outcomes)}"
    assert {r.leaf for r in runs if r.exit_code == 0} >= STUDENT_LINK_CLI_LEAVES, "folha sem invocação com sucesso"
    assert {r.leaf for r in runs if r.exit_code != 0} >= STUDENT_LINK_CLI_LEAVES, "folha sem invocação com erro"
    assert {(c.op_id, c.surface) for c in CANARY_CALLS if c.op_id in STUDENT_LINK_OPERATIONS} == {
        key for key in EXPECTED_CANARY if key[0] in STUDENT_LINK_OPERATIONS}, "chamada canário do vínculo do aluno sem resultado esperado"

    # --- disparo e resposta: alvos dos alunos canário do disparo, gabarito, detalhe técnico e segredos em todas as
    # superfícies, com as exceções declaradas e os controles positivos ---
    _check_dispatch_surfaces(exchanges, runs, captured.lines, rich_bot_api, engine_fake, db_url, dispatch_run,
                             api.events, secrets)
    # Mensagens livres opcionais: o e-mail do professor canário só nas três exceções, com controle positivo; o e-mail de
    # aluno canário recusado como contato não fica gravado.
    _check_professor_email(exchanges, runs, captured.lines, rich_bot_api, bot_api, db_url, sql, dispatch_run,
                           api.events)
    # O token privado do repositório está no registro e em nenhuma saída (as varreduras acima usam
    # `secrets`); controle positivo do detector.
    assert REPO_PRIVATE_TOKEN in secrets
    assert find_secrets(f"{DISPATCH_REPO}?private_token={REPO_PRIVATE_TOKEN}", secrets), "controle positivo"

    # --- disparo e resposta: contra o vazio ---
    assert DISPATCH_CLI_LEAVES <= tree, f"folhas fora do CLI: {sorted(DISPATCH_CLI_LEAVES - tree)}"
    assert {op["cli"] for op in load_catalog()["operations"] if op["id"] in DISPATCH_OPERATIONS} == DISPATCH_CLI_LEAVES
    assert {r.leaf for r in runs if r.exit_code == 0} >= DISPATCH_CLI_LEAVES, "folha sem invocação com sucesso"
    assert {r.leaf for r in runs if r.exit_code != 0} >= DISPATCH_CLI_LEAVES, "folha sem invocação com erro"
    assert set(DISPATCH_EVENT_TYPES) <= set(EVENT_TYPES), (
        f"tipos fora de EventType: {sorted(set(DISPATCH_EVENT_TYPES) - set(EVENT_TYPES))}")
    assert len(EVENT_TYPES) == 48, (
        "os 29 tipos da API base ao vínculo do aluno, os 14 do disparo (com offering.settings_changed), os 3 dos relatórios e os 2 do CLI")
    assert set(DISPATCH_EVENT_TYPES) <= {e["type"] for e in api.events}, "tipo de evento que não saiu"
    assert DISPATCH_OUTCOMES <= outcomes, f"desfechos que não apareceram: {sorted(DISPATCH_OUTCOMES - outcomes)}"
    # Cada texto do bot saiu ao menos uma vez: toda constante e todo modelo da lista de textos do bot
    # numa mensagem, numa edição ou numa confirmação de toque do bot da fase, fora as exceções por
    # nome de `DISPATCH_TEXT_EXCEPTIONS`; o trecho fixo de cada função de texto; os rótulos dos botões; o `/naosei`
    # digitado. Os nomes foram conferidos no começo do teste (`_check_dispatch_text_names`).
    bot_texts = [text for _, text in _bot_outputs(rich_bot_api, dispatch_run.bot_id)]
    missing = [name for name, text in zip(DISPATCH_PHASE_TEXT_NAMES, _dispatch_phase_texts()) if not any(text in sent
                                                                                             for sent in bot_texts)]
    assert missing == [], f"texto do bot que não saiu: {missing}"
    missing_patterns = [name for name, pattern in DISPATCH_PHASE_PATTERNS.items()
                        if not any(re.search(pattern, sent) for sent in bot_texts)]
    assert missing_patterns == [], f"texto do bot (função) que não saiu: {missing_patterns}"
    labels = _bot_button_labels(rich_bot_api, dispatch_run.bot_id)
    assert [name for name in DISPATCH_PHASE_BUTTONS if getattr(deliverytexts, name) not in labels] == [], labels
    typed_commands = {row[0] for row in sql.execute(
        "SELECT u.text FROM telegram_updates u JOIN bots b ON b.id = u.bot_id WHERE b.telegram_bot_id = %s "
        "AND u.outcome LIKE 'answer_%%'", (dispatch_run.bot_id,)).fetchall()}
    assert [name for name in DISPATCH_PHASE_COMMANDS if getattr(deliverytexts, name) not in typed_commands] == []
    # As exceções declaradas não saíram na fase (se sair, a exceção deixa de valer e o texto entra na lista).
    assert [name for name in DISPATCH_TEXT_EXCEPTIONS
            if any(getattr(deliverytexts, name) in sent for sent in bot_texts)] == []
    # Cada tabela do disparo com linha (a varredura SQL acima também exige `count > 0` em cada uma).
    for table in ("question_refs", "dispatches", "deliveries", "delivery_contents", "media_blobs", "submissions",
                  "free_messages", "offering_contacts"):  # `offering_contacts`, das mensagens livres opcionais
        assert table in SQL_SCAN, f"tabela fora de SQL_SCAN: {table}"
        assert sql.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0, f"tabela vazia: {table}"
    # As chamadas canário do disparo rodaram (o resultado exato de cada uma, conferido em `_api_phase`, é 400 ou
    # 401: nenhuma dá 409 fora de FIELDS_CODES; ver as asserções de módulo junto de EXPECTED_CANARY).
    assert {(c.op_id, c.surface) for c in CANARY_CALLS if c.op_id in DISPATCH_OPERATIONS} == {
        key for key in EXPECTED_CANARY if key[0] in DISPATCH_OPERATIONS}, "chamada canário do disparo sem resultado esperado"

    # --- relatórios: alvos dos alunos canário do disparo, gabarito e segredos nas superfícies da fase de relatórios,
    # com as exceções declaradas e os controles positivos ---
    _check_report_surfaces(exchanges, runs, captured.lines, rich_bot_api, db_url, sql, report_run, api.events, secrets)

    # --- relatórios: contra o vazio ---
    assert REPORT_CLI_LEAVES <= tree, f"folhas fora do CLI: {sorted(REPORT_CLI_LEAVES - tree)}"
    assert {op["cli"] for op in load_catalog()["operations"] if op["id"] in REPORT_OPERATIONS} == REPORT_CLI_LEAVES
    assert {r.leaf for r in report_run.runs if r.exit_code == 0} == REPORT_CLI_LEAVES, "folha sem sucesso"
    assert {r.leaf for r in report_run.runs if r.exit_code != 0} == REPORT_CLI_LEAVES, "folha sem erro"
    assert any(r.leaf == "reports show" and "--csv" in r.args and r.exit_code == 0 for r in report_run.runs), (
        "controle contra o vazio: o relatório em CSV pelo CLI")
    assert set(REPORT_EVENT_TYPES) <= set(EVENT_TYPES), (
        f"tipos fora de EventType: {sorted(set(REPORT_EVENT_TYPES) - set(EVENT_TYPES))}")
    assert set(REPORT_EVENT_TYPES) <= {e["type"] for e in api.events}, "tipo de evento que não saiu"
    assert {e["data"]["state"] for e in api.events if e["type"] == "notice.recorded"} >= set(REPORT_NOTICE_FINAL_STATES)
    # Cada um dos 10 tipos de aviso e cada estado final, os textos de `noticetexts`, as 7 tabelas com linha e
    # os 2 desfechos da recepção (`REPORT_OUTCOMES`).
    _check_report_notices_and_texts(sql, rich_bot_api, report_run, api)
    # As chamadas canário dos relatórios rodaram (o resultado exato de cada uma, conferido em `_api_phase`, é 400 ou 401:
    # nenhuma dá 409; ver as asserções de módulo junto de EXPECTED_CANARY).
    assert {(c.op_id, c.surface) for c in CANARY_CALLS if c.op_id in REPORT_OPERATIONS} == {
        key for key in EXPECTED_CANARY if key[0] in REPORT_OPERATIONS}, "chamada canário dos relatórios sem resultado esperado"

    # --- expurgo: as pessoas apagadas e o contato nas superfícies da fase do expurgo, com as exceções declaradas e os
    # controles positivos ---
    _check_cli_surfaces(r7_run, secrets)

    # --- CLI e expurgo: contra o vazio ---
    assert DOCTOR_PURGE_CLI_LEAVES <= tree, f"folhas fora do CLI: {sorted(DOCTOR_PURGE_CLI_LEAVES - tree)}"
    assert LOCAL_COMMANDS <= tree, f"comandos locais fora do CLI: {sorted(LOCAL_COMMANDS - tree)}"
    assert {op["cli"] for op in load_catalog()["operations"] if op["id"] in DOCTOR_PURGE_OPERATIONS} == DOCTOR_PURGE_CLI_LEAVES
    for leaves in (DOCTOR_PURGE_CLI_LEAVES, LOCAL_COMMANDS):
        assert {r.leaf for r in runs if r.exit_code == 0} >= leaves, "folha sem invocação com sucesso"
        assert {r.leaf for r in runs if r.exit_code != 0} >= leaves, "folha sem invocação com erro"
    assert any("--person" in r.args and r.exit_code == 0 for r in runs), "events list --person sem sucesso"
    assert any("--person" in r.args and r.exit_code != 0 for r in runs), "events list --person sem erro"
    assert set(PURGE_EVENT_TYPES) <= set(EVENT_TYPES), (
        f"tipos fora de EventType: {sorted(set(PURGE_EVENT_TYPES) - set(EVENT_TYPES))}")
    assert set(PURGE_EVENT_TYPES) <= {e["type"] for e in api.events}, "tipo de evento que não saiu no canário"
    hint = HINT_TEMPLATE.format(project="cq")
    assert sum(1 for r in r7_run.runs if hint in r.stderr.splitlines()) >= 1, "a dica da API inalcançável não saiu"
    # As chamadas canário do CLI e do expurgo rodaram (o resultado exato de cada uma, conferido em `_api_phase`: 400, 401 ou o
    # único 409 `invalid_confirmation`; ver as asserções de módulo junto de EXPECTED_CANARY).
    assert {(c.op_id, c.surface) for c in CANARY_CALLS if c.op_id in DOCTOR_PURGE_OPERATIONS} | {
        (c.op_id, c.surface) for c in CANARY_CALLS if (c.op_id, c.surface) == ("events.list", "query_value_person")
    } == {key for key in EXPECTED_CANARY if key[0] in DOCTOR_PURGE_OPERATIONS or key == ("events.list", "query_value_person")}


def test_canary_surfaces_cover_published_openapi(client: Any) -> None:
    """Cobertura exigida do canário, derivada do OpenAPI publicado: DECLARED_SURFACES ⊇ exigido.

    Cobertura exigida: 100% das operações.
    """
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    catalog = {(op["method"], op["path"]): op["id"] for op in load_catalog()["operations"]}

    required: dict[str, set[str]] = {"route.unknown": {"path"}}
    published: set[str] = set()
    for path, item in doc["paths"].items():
        if path == "/openapi.json":
            continue
        shared = item.get("parameters", [])
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            key = (method.upper(), path)
            assert key in catalog, f"rota publicada fora do catálogo: {key}"
            op_id = catalog[key]
            published.add(op_id)
            if not path.startswith("/v1/"):
                continue
            params = [_resolve_ref(p, doc) for p in [*shared, *operation.get("parameters", [])]]
            need = {"header_auth", "header_client", "query_key"}
            if "requestBody" in operation:
                need |= {"body_value", "body_key"}
            if any(p.get("in") == "query" for p in params):
                need.add("query_value")
            if any(p.get("in") == "path" for p in params):
                need.add("path")
            if op_id == "people.register":
                need.add("conflict")
            required[op_id] = need

    assert published == set(catalog.values()), f"operações do catálogo fora do OpenAPI: {set(catalog.values()) - published}"
    assert set(required) == set(MIN_REQUIRED_SURFACES)
    shrunk = {op_id: sorted(minimum - required[op_id]) for op_id, minimum in MIN_REQUIRED_SURFACES.items()
              if not minimum <= required[op_id]}
    assert shrunk == {}, f"o OpenAPI publicado esconde corpo ou parâmetros: {shrunk}"
    missing = {op_id: sorted(need - DECLARED_SURFACES.get(op_id, set())) for op_id, need in required.items()
               if not need <= DECLARED_SURFACES.get(op_id, set())}
    assert missing == {}, f"superfícies exigidas sem CANARY_CALL: {missing}"


def _resolve_ref(node: Any, doc: dict) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        target: Any = doc
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        node = target
    return node


def test_canary_unreachable_database_leaks_nothing(
        app_factory: Any, unreachable_db_url: str, cli_invoke: Any, monkeypatch: Any, capfd: Any,
        bot_api: Any, tmp_path: Any) -> None:
    """API sobre banco inalcançável: /health e operações /v1 sem identidade, segredo ou senha do banco
    nas respostas, no CLI, nos logs, na saída do processo e nos avisos.

    API sobre banco inalcançável: erro 500/503 sem vazamento; o princípio da identidade só pela revelação.
    Bots (na ordem do registro):
    `bots.register` com o banco fora, pela API e pelo `cq bots register --token-file`, dá 500 sem
    nenhuma chamada ao Telegram (o oferecimento é lido antes do `getMe`), e o token do bot não aparece
    em respostas, CLI, logs nem na saída do processo.
    """
    monkeypatch.setattr(cq_cli, "stdout_is_terminal", lambda: False)
    monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: False)  # token do bot só por arquivo
    bot = bot_api.add_bot()  # o token entra no registro de segredos
    token_file = tmp_path / "token-do-bot.txt"
    token_file.write_text(bot_api.token_of(bot) + "\n", encoding="utf-8")
    secrets = all_secrets(DB_PASSWORD_SENTINEL)
    secrets += bot_secrets()  # as chaves-mestras de teste (fora de `all_secrets()`)
    assert bot_api.token_of(bot) in secrets
    assert DB_PASSWORD_SENTINEL in unreachable_db_url
    assert find_secrets(unreachable_db_url, secrets), "controle positivo: a senha sentinela é detectável"

    exchanges: list[_Exchange] = []
    with capture_logs() as captured, warnings.catch_warnings(record=True) as caught:
        # A montagem do app com a URL que tem a senha também fica sob a captura.
        app = app_factory(database_url=unreachable_db_url)
        with TestClient(app, raise_server_exceptions=False) as http:
            _record_exchanges(http, exchanges)
            health = http.get("/health")
            listing = http.get("/v1/people", headers=ADMIN)
            registering = http.post("/v1/people", json=ZEFIRO.payload(), headers=ADMIN)
            cli_health = cli_invoke(["health"], http=http)
            cli_listing = cli_invoke(["people", "list"], http=http)
            # registro do bot com o banco fora (API e CLI).
            registering_bot = http.put("/v1/offerings/oferta-banco-fora/bot",
                                       json={"token": bot_api.token_of(bot)}, headers=ADMIN)
            cli_bot = cli_invoke(["bots", "register", "oferta-banco-fora", "--token-file", str(token_file)],
                                 http=http)

    # 500 sem chamar o Telegram; o CLI mostra o erro interno.
    assert_error(registering_bot, 500, "internal")
    assert (cli_bot.exit_code, cli_bot.stdout, cli_bot.stderr.splitlines()) == (
        1, "", [f"Erro: {MESSAGES['internal']}"])
    assert bot_api.calls() == [], "o registro do bot com o banco fora chamou o Telegram"
    for text in (cli_bot.stdout, cli_bot.stderr, cli_bot.output):
        assert find_canary(text) == [] and find_secrets(text, secrets) == [], text
        assert "Traceback" not in text

    assert health.status_code == 503
    assert_matches("system.health", 503, health.json())
    assert health.json() == {"status": "error", "version": cq_server.__version__, "database": "unreachable",
                             "schema_version": None, "expected_schema_version": EXPECTED_VERSION}
    assert_error(listing, 500, "internal")
    assert_error(registering, 500, "internal")
    assert_no_echo(registering, ZEFIRO.name, ZEFIRO.ra, ZEFIRO.email)
    assert (cli_health.exit_code, cli_health.stdout, cli_health.stderr.splitlines()) == (
        1, "", ["Erro: API com problema (banco: unreachable, esquema: nenhum)."])
    assert (cli_listing.exit_code, cli_listing.stdout, cli_listing.stderr.splitlines()) == (
        1, "", [f"Erro: {MESSAGES['internal']}"])

    leaks = [(ex.method, ex.path, ex.status, _exchange_canary_hits(ex), _exchange_secret_hits(ex, secrets))
             for ex in exchanges]
    assert [leak for leak in leaks if leak[3] or leak[4]] == [], f"vazamento em respostas: {leaks}"
    for result in (cli_health, cli_listing):
        for text in (result.stdout, result.stderr, result.output):
            assert find_canary(text) == [] and find_secrets(text, secrets) == [], text
            assert "Traceback" not in text
    assert all("_raw" not in record for record in captured.records)
    log_leaks = [(i, find_canary(line), find_secrets(line, secrets)) for i, line in enumerate(captured.lines)]
    assert [leak for leak in log_leaks if leak[1] or leak[2]] == [], f"vazamento nos logs: {log_leaks}"
    _check_process_output(capfd, caught, secrets, DB_PASSWORD_SENTINEL)

    requests = [r for r in captured.records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]
    assert Counter((r.get("method"), r.get("status")) for r in requests) == Counter(
        (ex.method, ex.status) for ex in exchanges)
    for method, route, status in (("GET", "/health", 503), ("GET", "/v1/people", 500), ("POST", "/v1/people", 500)):
        assert _has_record(requests, {"logger": "cq_server.api", "event": "request", "method": method,
                                      "route": route, "status": status}), (method, route, status)
    # o registro do bot (API e CLI) também deixou um `request` com 500, na rota do catálogo.
    bot_requests = [r for r in requests if r.get("route") == "/v1/offerings/{ref}/bot"]
    assert [(r.get("method"), r.get("status")) for r in bot_requests] == [("PUT", 500), ("PUT", 500)]
