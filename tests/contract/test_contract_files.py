"""Arquivos de contrato congelados em `contracts/api/v1/`: o catálogo de operações, os esquemas,
a tabela de erros, os estados, os padrões e os eventos.

Verdes desde o início: estes
testes conferem artefatos que já existem antes da implementação e ficam congelados com os testes.
Eles protegem os esquemas contra regressões, porque o teste do OpenAPI e todas as validações
de resposta (`assert_matches`) partem deles.

Oferecimentos e bots: os laços percorrem `CORE_OPERATIONS` (as 9 operações gerais)
e as 10 de oferecimentos e bots (19 operações);
há os 7
códigos de erro de oferecimentos e bots, `state` só em `invalid_state` e `fields` por código (`FIELDS_CODES`);
as 8 permissões; os padrões de oferecimentos e bots, com o código do oferecimento conferido por
caminho (`EXACT_PATH_PATTERNS`); `x-free-text` em `course` e `term`; `x-secret` em exatamente 3
pontos; os 14 tipos de evento de oferecimentos e bots; `problem.code`/`problem.message`
pareados; `mcp.confirm` em exatamente `CONFIRM_OPS`. São verdes desde o
início, **exceto** `test_events_schema_variants_match_data_model` e
`test_permission_enums_match_everywhere`, que comparam com `EventType` e `Permission` e ficam
vermelhos no esqueleto até a implementação ter esses tipos de evento e essas permissões.

Vínculo do aluno (lista e convites):
os laços
percorrem também as 5 operações de alunos e convites (`STUDENT_LINK_OPERATIONS`), das quais 4 recebem
corpo (`STUDENT_LINK_REQUEST_OPERATIONS`); os 2 códigos de erro do vínculo (`list_conflict` e `role_conflict`, 409,
com `fields` obrigatório); as respostas do vínculo sem marca e sem anulável; `x-identity` no pedido
de `students.import` (`rows[].name`, `rows[].ra` e `rows[].email`) e nenhum `x-secret` a mais (3
pontos); os 8 tipos de evento do vínculo (com conjuntos de campos distintos e os valores
enumerados do vínculo do aluno); `mcp.confirm` também em `invites.send`, e as destrutivas
`offerings.archive` e `invites.send`; os campos `rows`, `rows[i]` e `pseudonyms[i]` no padrão de
campo comum. Continuam verdes desde o esqueleto (os arquivos de contrato do vínculo do aluno já existem),
**exceto** `test_events_schema_variants_match_data_model`, que compara com `EventType` e fica vermelho
no esqueleto até a implementação ter os tipos novos.

Disparo e resposta:
os laços
percorrem também as 6 operações de disparos e mensagens livres (`DISPATCH_OPERATIONS`), das
quais só `dispatches.prepare` recebe corpo (`DISPATCH_REQUEST_OPERATIONS`); os 3 códigos de erro do disparo
(`invalid_dispatch_state`, `window_expired` e `grading_pending`, 409, sem `fields`, `state` nem
`permission`), com os campos do pedido de preparo no padrão de campo comum; os identificadores
do disparo (`dispatch_id`, `delivery_id`, `submission_id`, `question_ref_id`, UUID v4) e os horários dele pelo
nome; as marcas das respostas do disparo, com a marca `x-untrusted` (sempre com `x-free-text`) no
conteúdo de terceiros e no texto livre de aluno, e só `x-free-text` em `question.repo` e `question.qid`;
os anuláveis do `Dispatch` e de `parts[]` do `submission.graded`; nenhum `x-identity` (9) nem `x-secret` (3)
a mais; os 13 tipos de evento do disparo (com conjuntos de campos distintos e os valores enumerados
do disparo); `mcp.confirm` também em `dispatches.confirm`, a terceira
destrutiva; `messages.list` fora do MCP. As conferências próprias do disparo ficam em
`test_contract_dispatch.py`. `test_events_schema_variants_match_data_model` compara com `EventType` e fica
vermelho no esqueleto até a implementação ter os tipos novos. Três outras funções
(`test_schema_files_cover_the_catalog`, `test_error_schema_matches_contract_table` e
`test_mcp_confirm_only_on_identity_reveal`) conferem também que a implementação bate com os arquivos
(para que essas três não passem no esqueleto): o
registro de operações (`cq_server.api.operations`), o OpenAPI publicado (`create_app(...).openapi()`, sem
banco) e a tabela de erros (`cq_server.api.errors`); vermelhas no esqueleto porque as 6 operações novas
não estão no registro nem no OpenAPI e os 3 códigos novos não estão na tabela de
erros. As demais funções rodam como regressão sobre os arquivos.

Mensagens livres opcionais por oferecimento, desligadas por padrão (no disparo e
na resposta):
a operação `offerings.update_settings` está em `DISPATCH_OPERATIONS` e, com
pedido, em `DISPATCH_REQUEST_OPERATIONS`; o pedido de `offerings.create` tem `free_messages` e
`contact_email` (`x-identity: email`), e o `Offering` de toda resposta tem `free_messages` e
`contact_email_set` (booleanos, obrigatórios, não anuláveis, sem marca); `x-identity` em 11 pontos (os dois
`contact_email` de pedido); a resposta da operação com os anuláveis e as marcas do `Offering`; o tipo de evento
`offering.settings_changed` `{offering_id, free_messages, contact_email_set}`, sem marca; a
operação exposta ao MCP, sem `confirm`, não destrutiva; e o aviso `no_teacher_contact` entre os
valores aceitos de `warnings` do `dispatch.validated` (amostras válidas). Vermelho no esqueleto nas
funções que conferem a implementação: a operação nova fora do registro e do OpenAPI publicado
(`test_schema_files_cover_the_catalog`, `test_mcp_confirm_only_on_identity_reveal`) e `EventType` sem o 43º
tipo (`test_events_schema_variants_match_data_model`) até a implementação; e
`test_extension_keys_are_known_and_well_formed` confere os 2 pontos de
`x-identity` também no pedido publicado. As demais conferem só os arquivos congelados
(`test_offering_bot_states_and_fields_match_contract`, por constantes do módulo). As conferências próprias
das mensagens livres ficam em `test_contract_offering_settings.py`.

Relatórios e acompanhamento:
os laços percorrem também
as 5 operações de relatórios e do resumo diário (`REPORT_OPERATIONS`), das quais só
`offerings.update_digest` recebe corpo (`REPORT_REQUEST_OPERATIONS`); as marcas das respostas dos relatórios
(`x-untrusted` sempre com `x-free-text` no título, nos rótulos, nas alternativas e nos valores das
distribuições; só `x-free-text` em `dispatch.question.qid`); os anuláveis do `Report` (`RatePair`, tempo até
responder, `frozen`, título e os campos da distribuição), o `next_cursor` de `reports.list_versions` e os
campos anuláveis de `notice.recorded`; os padrões dos relatórios pelo nome (`computed_at`, `frozen_at`,
`content_hash`, `commit_short`, `local_time`); os 3 tipos de evento dos relatórios, sem marca (com conjuntos de
campos distintos); as 5 operações expostas ao MCP. Os relatórios não mexem em `CONTRACT_ERRORS`,
`FIELDS_CODES`, `PERMISSIONS`, `CONFIRM_OPS`, as destrutivas, `x-identity` (11) e `x-secret` (3).
As conferências próprias dos relatórios ficam em `test_contract_reports.py`. Falha esperada no esqueleto,
porque `test_schema_files_cover_the_catalog` e `test_mcp_confirm_only_on_identity_reveal` conferem o
registro de operações e o OpenAPI publicado, sem as 5 operações até a implementação delas;
`test_events_schema_variants_match_data_model` compara com `EventType`, sem os 3 tipos até a implementação
deles. As demais funções que percorrem as tabelas rodam como regressão sobre os arquivos.

CLI (operação e expurgo):
os laços percorrem
também as 3 operações do `doctor` e do expurgo (`DOCTOR_PURGE_OPERATIONS`), das quais só `offerings.purge` recebe
corpo (`DOCTOR_PURGE_REQUEST_OPERATIONS`); os 2 códigos de erro do expurgo (`purge_pending` e `purge_changed`, 409, sem `fields`,
`state` nem `permission`), com `pending` (as 5 contagens) obrigatório no `purge_pending` e proibido nos
outros 20 códigos por uma regra no objeto `error`, sem tocar os 19 ramos congelados, conferido no
`error.schema.json` e no envelope publicado (`ErrorDetail` do OpenAPI de
`create_app`); o `purged_at` (horário ou nulo) em cada resposta que embute o `Offering` e os anuláveis do
`doctor` (`checks[].offering` e `checks[].count`); os padrões de horário pelo nome (`checked_at`, `purged_at`); as 3
respostas sem marca; os 2 tipos de evento do expurgo, sem marca (com conjuntos de campos distintos);
`mcp.confirm` também em `offerings.purge`, a quarta destrutiva. O expurgo não mexe em `PERMISSIONS`,
`FIELDS_CODES`, `x-identity` (11) e `x-secret` (3). As conferências próprias do expurgo e do `doctor` ficam em
`test_contract_cli.py`. Falha esperada no esqueleto, porque `test_schema_files_cover_the_catalog` e
`test_mcp_confirm_only_on_identity_reveal` conferem o registro de operações e o OpenAPI publicado, sem as 3
operações até a implementação delas; `test_error_schema_matches_contract_table` compara com
`cq_server.api.errors` e com o envelope publicado, sem os 2 códigos nem o `pending` até a implementação;
`test_events_schema_variants_match_data_model` compara com `EventType`, sem os 2 tipos até a implementação. As
demais funções que percorrem as tabelas rodam como regressão sobre os arquivos.
"""

from __future__ import annotations

import copy
import pathlib
import re
from collections.abc import Iterator
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for

from tests.helpers.contract import (
    CONTRACTS,
    FIELDS_CODES,
    MESSAGES,
    OFFERING_STATES,
    SCHEMAS,
    STATUS_OF,
    flatten,
    iter_objects,
    load_catalog,
    load_json,
)

DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
CATALOG_SCHEMA = CONTRACTS / "catalog.schema.json"

# As 9 operações gerais e as que recebem corpo.
CORE_OPERATIONS = (
    "system.health", "people.register", "people.list", "people.get", "identity.reveal",
    "tokens.create", "tokens.list", "tokens.revoke", "events.list",
)
REQUEST_OPERATIONS = {"people.register", "identity.reveal", "tokens.create"}

# as 10 operações de oferecimentos e bots e as que recebem corpo.
OFFERING_BOT_OPERATIONS = (
    "offerings.create", "offerings.list", "offerings.get", "offerings.close",
    "offerings.prepare_archive", "offerings.archive", "bots.register", "bots.activate", "bots.get",
    "team.link",
)
OFFERING_BOT_REQUEST_OPERATIONS = {"offerings.create", "offerings.archive", "bots.register", "team.link"}
ALL_OPERATIONS = CORE_OPERATIONS + OFFERING_BOT_OPERATIONS

# as 5 operações do vínculo do aluno e as que recebem corpo
# (`students.summary` é GET, sem corpo).
STUDENT_LINK_OPERATIONS = ("students.import", "students.summary", "invites.prepare_send", "invites.send",
                 "invites.resend")
STUDENT_LINK_REQUEST_OPERATIONS = {"students.import", "invites.prepare_send", "invites.send", "invites.resend"}
ALL_OPERATIONS += STUDENT_LINK_OPERATIONS

# as 6 operações do disparo e da resposta e a única que recebe corpo
# (a consulta, a lista, a confirmação, o cancelamento e as mensagens livres são sem corpo).
DISPATCH_OPERATIONS = ("dispatches.prepare", "dispatches.get", "dispatches.list", "dispatches.confirm",
                 "dispatches.cancel", "messages.list")
DISPATCH_REQUEST_OPERATIONS = {"dispatches.prepare"}
# A configuração das mensagens livres do oferecimento (opcionais, desligadas por padrão),
# com pedido.
DISPATCH_OPERATIONS += ("offerings.update_settings",)
DISPATCH_REQUEST_OPERATIONS |= {"offerings.update_settings"}
ALL_OPERATIONS += DISPATCH_OPERATIONS

# as 5 operações dos relatórios e do acompanhamento (`ids[31:36]`) e a única que recebe corpo
# (a consulta, as versões, o reenvio e a leitura do resumo diário são sem corpo).
REPORT_OPERATIONS = ("reports.get", "reports.list_versions", "reports.resend_summary", "offerings.get_digest",
                 "offerings.update_digest")
REPORT_REQUEST_OPERATIONS = {"offerings.update_digest"}
ALL_OPERATIONS += REPORT_OPERATIONS

# as 3 operações do CLI (`ids[36:39]`, as últimas) e a única que recebe corpo
# (o `doctor` e a preparação do expurgo são sem corpo).
DOCTOR_PURGE_OPERATIONS = ("system.doctor", "offerings.prepare_purge", "offerings.purge")
DOCTOR_PURGE_REQUEST_OPERATIONS = {"offerings.purge"}
ALL_OPERATIONS += DOCTOR_PURGE_OPERATIONS

# `mcp.confirm` verdadeiro em exatamente estas operações.
CONFIRM_OPS = {"identity.reveal", "offerings.archive"}
# Vínculo do aluno: também a confirmação do envio em lote dos convites.
CONFIRM_OPS |= {"invites.send"}
# Disparo e resposta: também a confirmação do disparo (envio em massa à turma).
CONFIRM_OPS |= {"dispatches.confirm"}
# CLI: também a confirmação do expurgo (destrutiva).
CONFIRM_OPS |= {"offerings.purge"}

# Erros da API: código → (status, mensagem exata).
CONTRACT_ERRORS = {
    "invalid_request": (400, "Pedido inválido."),
    "unauthenticated": (401, "Token ausente, inválido ou revogado."),
    "forbidden": (403, "Este token não tem a permissão necessária."),
    "not_found": (404, "Recurso não encontrado."),
    "method_not_allowed": (405, "Método não permitido."),
    "conflict": (409, "Já existe uma pessoa com esse RA ou e-mail."),
    "internal": (500, "Erro interno. Tente de novo mais tarde."),
    # Oferecimentos e bots.
    "already_exists": (409, "Já existe um oferecimento com esse código."),
    "invalid_state": (409, "A operação não é permitida no estado atual do oferecimento."),
    "bot_conflict": (409, "Esse bot não pode ser usado neste oferecimento."),
    "bot_required": (409, "O oferecimento não tem um bot registrado com acesso."),
    "invalid_confirmation": (409, "O código de confirmação é inválido ou venceu."),
    "unavailable": (503, "O Telegram não respondeu. Tente de novo em instantes."),
    "not_configured": (503, "O servidor não tem a chave-mestra dos bots configurada."),
    # Vínculo do aluno: `fields` obrigatório nos dois.
    "list_conflict": (409, "Há linhas da lista em conflito com pessoas cadastradas ou com a equipe; "
                           "nada foi gravado."),
    "role_conflict": (409, "A mesma pessoa não pode ser equipe e aluno do mesmo oferecimento."),
    # Disparo e resposta: sem `fields`, `state` nem `permission` nos três.
    "invalid_dispatch_state": (409, "A operação não é permitida no estado atual do disparo."),
    "window_expired": (409, "A janela do disparo não é mais válida; prepare um novo disparo."),
    "grading_pending": (409, "Há respostas aguardando correção ou mensagens ainda não processadas neste "
                             "oferecimento."),
    # Expurgo: sem `fields`, `state` nem `permission` nos dois; `pending` (as 5
    # contagens) obrigatório no `purge_pending` e proibido em todos os outros códigos.
    "purge_pending": (409, "Há trabalho pendente neste oferecimento; o expurgo só pode ser feito depois que ele "
                           "terminar."),
    "purge_changed": (409, "As contagens do expurgo mudaram desde a preparação; prepare de novo e confira o "
                           "impacto."),
}
# Expurgo: as chaves do objeto `pending` do `purge_pending`, na ordem do contrato.
PENDING_KEYS = ["outbox", "updates", "tasks", "freeze", "due"]
CONTRACT_REASONS = {
    "missing", "unknown_field", "invalid_format", "too_short", "too_long", "too_few", "too_many",
    "out_of_range", "invalid_value", "duplicate",
}
PERMISSIONS = {"tokens:manage", "people:write", "people:read", "events:read", "identity:reveal",
               "offerings:read", "offerings:write", "bots:manage"}

# Estados, na ordem do contrato.
OFFERING_STATE_ENUM = ["draft", "active", "closed", "archived"]
BOT_STATE_ENUM = ["registered", "no_access"]
PROFILE_STATE_ENUM = ["pending", "configured", "failed"]

# `problem` do `Bot`: código → mensagem fixa.
BOT_PROBLEMS = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "token_unreadable": "O token guardado não pode ser lido com a chave-mestra atual. Registre o token de novo.",
    "telegram_unavailable": "O Telegram não respondeu; o sistema vai tentar de novo.",
    "rate_limited": "O Telegram pediu uma pausa; o sistema vai tentar de novo depois do tempo indicado.",
    "concurrent_consumer": "Outro programa está consultando este bot. Pare o outro programa.",
    "webhook_active": "Havia um webhook configurado neste bot; ele foi removido.",
}

# Padrões fixados no contrato (horário) e nos demais campos (apelido, identificador e outros).
TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
ALIAS_PATTERN = r"^(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}$"
SECRET_PATTERN = r"^cq_[A-Za-z0-9_-]{43}$"
TASK_TYPE_PATTERN = r"^[a-z][a-z0-9_.]{0,99}$"
FIELD_PATTERN = r"^(body|query|X-CQ-Client|[a-z_]+(\[[0-9]+\])?)$"
# código do oferecimento, `username` do bot, link de equipe,
# código de confirmação, token do bot no pedido e pseudônimo no
# pedido de `team.link` (apelido ou pseudo_id da API).
CODE_PATTERN = r"^[a-z0-9][a-z0-9-]{2,39}$"
USERNAME_PATTERN = r"^[A-Za-z0-9_]{5,32}$"
LINK_PATTERN = r"^https://t\.me/[A-Za-z0-9_]{5,32}\?start=[A-Za-z0-9_-]{22}$"
CONFIRMATION_CODE_PATTERN = r"^[A-Za-z0-9_-]{22}$"
BOT_TOKEN_PATTERN = r"^[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}$"
PERSON_REF_PATTERN = f"^(?:{ALIAS_PATTERN[1:-1]}|{UUID4_PATTERN[1:-1]})$"
# Relatórios: o hash do conteúdo (o formato do disparo), o commit curto e o horário local.
CONTENT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
COMMIT_SHORT_PATTERN = r"^[0-9a-f]{12}$"
LOCAL_TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

EXACT_PATTERNS = {
    "created_at": TIME_PATTERN,
    "revoked_at": TIME_PATTERN,
    "occurred_at": TIME_PATTERN,
    "pseudo_id": UUID4_PATTERN,
    "token_id": UUID4_PATTERN,
    "recipient": UUID4_PATTERN,
    "revealed[]": UUID4_PATTERN,
    "requested[]": UUID4_PATTERN,
    "alias": ALIAS_PATTERN,
    "secret": SECRET_PATTERN,
    "task_type": TASK_TYPE_PATTERN,
    # Oferecimentos e bots.
    "offering_id": UUID4_PATTERN,
    "username": USERNAME_PATTERN,
    "link": LINK_PATTERN,
    "confirmation_code": CONFIRMATION_CODE_PATTERN,
    "activated_at": TIME_PATTERN,
    "closed_at": TIME_PATTERN,
    "archived_at": TIME_PATTERN,
    "expires_at": TIME_PATTERN,
    "registered_at": TIME_PATTERN,
    "token_replaced_at": TIME_PATTERN,
    "last_poll_ok_at": TIME_PATTERN,
    "until": TIME_PATTERN,
    # Disparo e resposta: os identificadores de domínio (UUID v4
    # canônicos minúsculos) e os horários do `Dispatch` e de `dispatch.confirmed`.
    "dispatch_id": UUID4_PATTERN,
    "delivery_id": UUID4_PATTERN,
    "submission_id": UUID4_PATTERN,
    "question_ref_id": UUID4_PATTERN,
    "opens_at": TIME_PATTERN,
    "closes_at": TIME_PATTERN,
    "validated_at": TIME_PATTERN,
    "confirmed_at": TIME_PATTERN,
    "rejected_at": TIME_PATTERN,
    "opened_at": TIME_PATTERN,
    "cancelled_at": TIME_PATTERN,
    # Relatórios: os padrões por nome de folha dos relatórios.
    "computed_at": TIME_PATTERN,
    "frozen_at": TIME_PATTERN,
    "content_hash": CONTENT_HASH_PATTERN,
    "commit_short": COMMIT_SHORT_PATTERN,
    "local_time": LOCAL_TIME_PATTERN,
    # Expurgo e diagnóstico: os horários pelo nome.
    "checked_at": TIME_PATTERN,
    "purged_at": TIME_PATTERN,
}
# o código do oferecimento é conferido por caminho, porque o `problem.code` do `Bot` é um enum
# no contrato do `Bot`; idem o `person` dos eventos, que é um pseudo_id, e o
# `person` de `team.link`, que é um objeto.
EXACT_PATH_PATTERNS = {
    "code": CODE_PATTERN,
    "items[].code": CODE_PATTERN,
    "offering.code": CODE_PATTERN,
    "items[].data.code": CODE_PATTERN,
    "items[].data.person": UUID4_PATTERN,
}

# Disparo e resposta: no `Dispatch` completo, `x-free-text` em `question.repo` e
# `question.qid` (texto do operador, redigido só na saída) e `x-free-text` + `x-untrusted` no conteúdo de
# terceiros (a prévia e os motivos do motor); o `DispatchSummary` da lista não tem a prévia.
_DISPATCH_UNTRUSTED = {"x-free-text", "x-untrusted"}
_DISPATCH_FIELD_MARKS: dict[str, set[str]] = {
    "question.repo": {"x-free-text"},
    "question.qid": {"x-free-text"},
    "rejection.details[]": _DISPATCH_UNTRUSTED,
    "preview.title": _DISPATCH_UNTRUSTED,
    "preview.statement": _DISPATCH_UNTRUSTED,
    "preview.fields[].label": _DISPATCH_UNTRUSTED,
    "preview.fields[].suffix": _DISPATCH_UNTRUSTED,
    "preview.fields[].hint": _DISPATCH_UNTRUSTED,
    "preview.warnings[].message": _DISPATCH_UNTRUSTED,
}

# Marcas exatas nas respostas: operação → {caminho achatado: marcas}.
EXPECTED_RESPONSE_MARKS: dict[str, dict[str, set[str]]] = {
    "system.health": {},
    "people.register": {},
    "people.list": {},
    "people.get": {},
    "identity.reveal": {
        "people[].name": {"x-identity:name"},
        "people[].ra": {"x-identity:ra"},
        "people[].email": {"x-identity:email"},
    },
    "tokens.create": {"label": {"x-free-text"}, "secret": {"x-secret"}},
    "tokens.list": {"items[].label": {"x-free-text"}},
    "tokens.revoke": {"label": {"x-free-text"}},
    "events.list": {
        "items[].data.label": {"x-free-text"},
        "items[].data.reason": {"x-free-text"},
        "items[].data.error": {"x-free-text"},
    },
    # Oferecimentos e bots: `course` e `term` em toda resposta com `Offering`; `link` secreto.
    "offerings.create": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    "offerings.list": {"items[].course": {"x-free-text"}, "items[].term": {"x-free-text"}},
    "offerings.get": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    "offerings.close": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    "offerings.prepare_archive": {"offering.course": {"x-free-text"}, "offering.term": {"x-free-text"}},
    "offerings.archive": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    "bots.register": {},
    "bots.activate": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    "bots.get": {},
    "team.link": {"link": {"x-secret"}},
    # Vínculo do aluno: nenhuma resposta do vínculo tem marca (sem identidade, segredo nem texto
    # livre; o oferecimento sai como `OfferingRef`).
    "students.import": {},
    "students.summary": {},
    "invites.prepare_send": {},
    "invites.send": {},
    "invites.resend": {},
    # Disparo e resposta: nenhum `x-identity` nem `x-secret` a mais.
    "dispatches.prepare": _DISPATCH_FIELD_MARKS,
    "dispatches.get": _DISPATCH_FIELD_MARKS,
    "dispatches.list": {"items[].question.repo": {"x-free-text"}, "items[].question.qid": {"x-free-text"},
                        "items[].rejection.details[]": _DISPATCH_UNTRUSTED},
    "dispatches.confirm": _DISPATCH_FIELD_MARKS,
    "dispatches.cancel": _DISPATCH_FIELD_MARKS,
    "messages.list": {"items[].text": _DISPATCH_UNTRUSTED},
    # Mensagens livres: o `Offering`, com `course` e `term` marcados; os dois booleanos
    # sem marca.
    "offerings.update_settings": {"course": {"x-free-text"}, "term": {"x-free-text"}},
    # Relatórios: no `Report`, `x-free-text` + `x-untrusted` no título e nos textos das
    # distribuições, só `x-free-text` no `qid`; as outras 4 respostas sem marca (nenhum `x-identity` nem
    # `x-secret` a mais).
    "reports.get": {
        "dispatch.question.qid": {"x-free-text"},
        "dispatch.title": _DISPATCH_UNTRUSTED,
        "distribution.fields[].label": _DISPATCH_UNTRUSTED,
        "distribution.fields[].choices[].text": _DISPATCH_UNTRUSTED,
        "distribution.fields[].values.items[].value": _DISPATCH_UNTRUSTED,
        "distribution.fields[].ratios.items[].value": _DISPATCH_UNTRUSTED,
        "distribution.fields[].texts.items[].value": _DISPATCH_UNTRUSTED,
    },
    "reports.list_versions": {},
    "reports.resend_summary": {},
    "offerings.get_digest": {},
    "offerings.update_digest": {},
    # Expurgo e diagnóstico: as 3 respostas sem marca (as mensagens do `doctor` são texto fixo do
    # servidor; nenhum `x-identity` nem `x-secret` a mais; o `confirmation_code` do expurgo não é segredo).
    "system.doctor": {},
    "offerings.prepare_purge": {},
    "offerings.purge": {},
}
# Por variante de evento: só `label`, `reason` e `error` são texto livre.
EXPECTED_EVENT_MARKS: dict[str, dict[str, set[str]]] = {
    "person.registered": {},
    "token.created": {"data.label": {"x-free-text"}},
    "token.revoked": {},
    "identity.revealed": {"data.reason": {"x-free-text"}},
    "identity.reveal_denied": {},
    "task.failed": {"data.error": {"x-free-text"}},
    "outbox.failed": {"data.error": {"x-free-text"}},
    # Oferecimentos e bots: só o `error` de `outbox.reply_failed`.
    "offering.created": {},
    "offering.state_changed": {},
    "offering.archive_prepared": {},
    "bot.registered": {},
    "bot.token_replaced": {},
    "bot.profile_updated": {},
    "bot.lease_changed": {},
    "bot.access_changed": {},
    "team.link_created": {},
    "team.link_revoked": {},
    "team.joined": {},
    "team.join_refused": {},
    "endpoint.blocked": {},
    "outbox.reply_failed": {"data.error": {"x-free-text"}},
    # Vínculo do aluno: os 8 tipos, sem texto livre nem identidade.
    "student.list_imported": {},
    "student.invites_prepared": {},
    "student.invites_queued": {},
    "student.invite_sent": {},
    "student.invite_failed": {},
    "student.invite_cancelled": {},
    "student.invite_revoked": {},
    "student.joined": {},
    # Disparo e resposta: os 13 tipos, sem texto livre, identidade, texto digitado nem URL.
    "dispatch.prepared": {},
    "dispatch.validated": {},
    "dispatch.rejected": {},
    "dispatch.confirmed": {},
    "dispatch.cancelled": {},
    "dispatch.opened": {},
    "dispatch.closed": {},
    "delivery.delivered": {},
    "delivery.not_delivered": {},
    "delivery.question_failed": {},
    "delivery.dont_know": {},
    "submission.graded": {},
    "submission.internal_failure": {},
    # Mensagens livres: sem o e-mail e sem texto livre.
    "offering.settings_changed": {},
    # Relatórios: os 3 tipos, sem texto livre nem identidade.
    "report.frozen": {},
    "notice.recorded": {},
    "offering.digest_changed": {},
    # Expurgo: os 2 tipos, só contagens, sem texto livre nem identidade.
    "offering.purge_prepared": {},
    "offering.purged": {},
}
# Dados de cada tipo de evento.
EVENT_DATA_KEYS: dict[str, set[str]] = {
    "person.registered": {"pseudo_id", "alias"},
    "token.created": {"token_id", "label", "permissions"},
    "token.revoked": {"token_id"},
    "identity.revealed": {"client", "revealed", "not_found_count", "reason"},
    "identity.reveal_denied": {"client", "requested", "not_found_count"},
    "task.failed": {"task_id", "task_type", "attempts", "error"},
    "outbox.failed": {"message_id", "recipient", "attempts", "error"},
    # Oferecimentos e bots.
    "offering.created": {"offering_id", "code"},
    "offering.state_changed": {"offering_id", "from_state", "to_state"},
    "offering.archive_prepared": {"offering_id", "expires_at"},
    "bot.registered": {"offering_id", "telegram_bot_id", "username"},
    "bot.token_replaced": {"offering_id", "telegram_bot_id", "token_version"},
    "bot.profile_updated": {"offering_id", "profile_state", "purpose"},
    "bot.lease_changed": {"offering_id", "worker_id", "change", "reason"},
    "bot.access_changed": {"offering_id", "access", "problem"},
    "team.link_created": {"offering_id", "person", "expires_at"},
    "team.link_revoked": {"offering_id", "person", "revoke_reason"},
    "team.joined": {"offering_id", "person", "endpoint_replaced"},
    "team.join_refused": {"offering_id", "refusal"},
    "endpoint.blocked": {"offering_id", "person", "channel"},
    "outbox.reply_failed": {"message_id", "reply_to", "attempts", "error"},
    # Vínculo do aluno: 8 tipos, cada um com conjunto de campos distinto.
    "student.list_imported": {"offering_id", "received", "people_created", "people_reused",
                              "enrollments_created", "already_enrolled"},
    "student.invites_prepared": {"offering_id", "eligible", "valid_hours", "expires_at"},
    "student.invites_queued": {"offering_id", "origin", "queued"},
    "student.invite_sent": {"offering_id", "person", "origin", "expires_at"},
    "student.invite_failed": {"offering_id", "person", "origin", "failure"},
    "student.invite_cancelled": {"offering_id", "person", "origin", "cancel_reason"},
    "student.invite_revoked": {"offering_id", "person", "origin", "revoke_reason"},
    "student.joined": {"offering_id", "person", "terms_version", "endpoint_replaced"},
    # Disparo e resposta: 13 tipos, cada um com conjunto de campos distinto.
    "dispatch.prepared": {"offering_id", "dispatch_id", "question_ref_id", "origin"},
    "dispatch.validated": {"offering_id", "dispatch_id", "target_count", "warnings"},
    "dispatch.rejected": {"offering_id", "dispatch_id", "rejection_reason"},
    "dispatch.confirmed": {"offering_id", "dispatch_id", "target_count", "opens_at", "closes_at"},
    "dispatch.cancelled": {"offering_id", "dispatch_id", "cancel_reason"},
    "dispatch.opened": {"offering_id", "dispatch_id", "target_count"},
    "dispatch.closed": {"offering_id", "dispatch_id", "trigger", "counts"},
    "delivery.delivered": {"offering_id", "dispatch_id", "delivery_id", "person"},
    "delivery.not_delivered": {"offering_id", "dispatch_id", "delivery_id", "person", "undelivered_reason"},
    "delivery.question_failed": {"offering_id", "dispatch_id", "delivery_id", "person", "failure"},
    "delivery.dont_know": {"offering_id", "dispatch_id", "delivery_id", "submission_id", "person"},
    "submission.graded": {"offering_id", "dispatch_id", "delivery_id", "submission_id", "question_ref_id",
                          "person", "valid", "late", "normalized", "score", "parts"},
    "submission.internal_failure": {"offering_id", "dispatch_id", "delivery_id", "submission_id", "person",
                                    "engine_error"},
    # Mensagens livres: conjunto distinto dos demais; o e-mail nunca entra.
    "offering.settings_changed": {"offering_id", "free_messages", "contact_email_set"},
    # Relatórios: 3 tipos, cada um com conjunto de campos
    # distinto dos demais; o assunto do aviso sem chave interna.
    "report.frozen": {"offering_id", "dispatch_id", "version", "trigger"},
    "notice.recorded": {"offering_id", "notice_type", "dispatch_id", "report_version", "episode_seq", "batch_id",
                        "window_started_at", "local_date", "state", "recipient_count"},
    "offering.digest_changed": {"offering_id", "enabled", "local_time"},
    # Expurgo.
    "offering.purge_prepared": {"offering_id", "expires_at", "identities_to_delete", "identities_kept", "endpoints",
                                "updates", "contact_email_set"},
    "offering.purged": {"offering_id", "identities_deleted", "identities_kept", "endpoints", "updates",
                        "free_messages_redacted", "outbox_redacted", "contact_email_deleted"},
}
# Valores dos campos enumerados dos eventos de oferecimentos e bots: tipo → campo →
# valores aceitos (`None` é o nulo do JSON).
OFFERING_BOT_EVENT_DATA_VALUES: dict[str, dict[str, set]] = {
    "offering.state_changed": {"from_state": set(OFFERING_STATES), "to_state": set(OFFERING_STATES)},
    "bot.profile_updated": {"profile_state": {"configured", "failed"}, "purpose": {"active", "archived"}},
    "bot.lease_changed": {"change": {"acquired", "released"},
                          "reason": {"acquired", "not_eligible", "shutdown", "archived", "no_access"}},
    "bot.access_changed": {"access": {"lost", "restored"},
                           "problem": {"token_rejected", "token_unreadable", None}},
    "team.link_revoked": {"revoke_reason": {"replaced", "archived", "refused"}},
    "team.join_refused": {"refusal": {"invalid", "other_person"}},
    "endpoint.blocked": {"channel": {"telegram"}},
}
# Vínculo do aluno: `origin` em `batch` · `coordination` · `self_service` (só
# `batch` · `coordination` em `student.invites_queued`), `failure`, `cancel_reason` (com `superseded`)
# e `revoke_reason`.
INVITE_ORIGINS = {"batch", "coordination", "self_service"}
STUDENT_LINK_EVENT_DATA_VALUES: dict[str, dict[str, set]] = {
    "student.invites_queued": {"origin": {"batch", "coordination"}},
    "student.invite_sent": {"origin": INVITE_ORIGINS},
    "student.invite_failed": {"origin": INVITE_ORIGINS,
                              "failure": {"recipient_refused", "auth_refused", "server_refused",
                                          "not_configured", "attempts_exhausted"}},
    "student.invite_cancelled": {"origin": INVITE_ORIGINS,
                                 "cancel_reason": {"offering_inactive", "already_linked", "superseded"}},
    "student.invite_revoked": {"origin": INVITE_ORIGINS,
                               "revoke_reason": {"replaced", "archived", "refused"}},
}
# Disparo e resposta: os campos enumerados dos 13 tipos do disparo (`valid` é
# `const: true`). O corpo congelado de `test_events_schema_variants_match_data_model` percorre
# `OFFERING_BOT_EVENT_DATA_VALUES | STUDENT_LINK_EVENT_DATA_VALUES`; os do disparo entram somados ao dicionário do vínculo.
DISPATCH_EVENT_DATA_VALUES: dict[str, dict[str, set]] = {
    "dispatch.prepared": {"origin": {"cli", "api", "mcp", "integration"}},
    "dispatch.rejected": {"rejection_reason": {
        "repository_unreachable", "repository_refused", "commit_not_found", "question_not_found",
        "question_unsupported", "question_broken", "engine_unavailable", "channel_limits_exceeded",
        "internal_failure"}},
    "dispatch.cancelled": {"cancel_reason": {"operator", "expired", "offering_closed", "window_missed",
                                             "validation_overran"}},
    "dispatch.closed": {"trigger": {"window", "archive"}},
    "delivery.not_delivered": {"undelivered_reason": {"endpoint_blocked", "channel_refused",
                                                      "engine_unavailable", "bot_no_access",
                                                      "channel_unavailable"}},
    "delivery.question_failed": {"failure": {"variant_broken", "variant_unsupported",
                                             "channel_limits_exceeded", "grading_broken", "engine_rejected"}},
    "submission.graded": {"valid": {True}},
    "submission.internal_failure": {"engine_error": {"invalid_request", "payload_too_large"}},
}
STUDENT_LINK_EVENT_DATA_VALUES.update(DISPATCH_EVENT_DATA_VALUES)
# Disparo e resposta: os anuláveis do `Dispatch` (por tipo, `[X, "null"]`): os horários de
# abertura, fechamento e transição, o motivo da rejeição, a razão do cancelamento, o alvo e a prévia (e,
# nela, o título e o rótulo e o sufixo de cada campo); o `DispatchSummary` sem a prévia.
_DISPATCH_SUMMARY_NULLABLE: dict[str, set[str]] = {
    **{name: {"string", "null"} for name in ("opens_at", "closes_at", "validated_at", "confirmed_at",
                                             "rejected_at", "opened_at", "closed_at", "cancelled_at")},
    "rejection": {"object", "null"},
    "cancel_reason": {"string", "null"},
    "target_count": {"integer", "null"},
}
_DISPATCH_DETAIL_NULLABLE: dict[str, set[str]] = {
    **_DISPATCH_SUMMARY_NULLABLE,
    "preview": {"object", "null"},
    "preview.title": {"string", "null"},
    "preview.fields[].label": {"string", "null"},
    "preview.fields[].suffix": {"string", "null"},
}

# Campos anuláveis de cada resposta: caminho achatado → tipos aceitos.
EXPECTED_NULLABLE: dict[str, dict[str, set[str]]] = {
    "system.health": {"schema_version": {"string", "null"}},
    "people.register": {},
    "people.list": {},
    "people.get": {},
    "identity.reveal": {"people[].ra": {"string", "null"}, "people[].email": {"string", "null"}},
    "tokens.create": {"revoked_at": {"null"}},
    "tokens.list": {"items[].revoked_at": {"string", "null"}},
    "tokens.revoke": {},
    "events.list": {"items[].actor_token_id": {"string", "null"}, "items[].data.problem": {"string", "null"}},
    # Oferecimentos e bots: horários das transições, resumo do bot, posse, última consulta e
    # problema do `Bot`.
    "offerings.create": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                         "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    "offerings.list": {"items[].activated_at": {"string", "null"}, "items[].closed_at": {"string", "null"},
                       "items[].archived_at": {"string", "null"}, "items[].bot": {"object", "null"}},
    "offerings.get": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                      "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    "offerings.close": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                        "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    "offerings.prepare_archive": {
        "offering.activated_at": {"string", "null"}, "offering.closed_at": {"string", "null"},
        "offering.archived_at": {"string", "null"}, "offering.bot": {"object", "null"}},
    "offerings.archive": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                          "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    "bots.register": {"token_replaced_at": {"string", "null"}, "lease": {"object", "null"},
                      "last_poll_ok_at": {"string", "null"}, "problem": {"object", "null"}},
    "bots.activate": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                      "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    "bots.get": {"token_replaced_at": {"string", "null"}, "lease": {"object", "null"},
                 "last_poll_ok_at": {"string", "null"}, "problem": {"object", "null"}},
    "team.link": {},
    # Vínculo do aluno: nenhum campo anulável nas respostas do vínculo.
    "students.import": {},
    "students.summary": {},
    "invites.prepare_send": {},
    "invites.send": {},
    "invites.resend": {},
    # Disparo e resposta: o `Dispatch` e o `DispatchSummary`; nada anulável em `messages.list`.
    "dispatches.prepare": _DISPATCH_DETAIL_NULLABLE,
    "dispatches.get": _DISPATCH_DETAIL_NULLABLE,
    "dispatches.list": {f"items[].{path}": types for path, types in _DISPATCH_SUMMARY_NULLABLE.items()},
    "dispatches.confirm": _DISPATCH_DETAIL_NULLABLE,
    "dispatches.cancel": _DISPATCH_DETAIL_NULLABLE,
    "messages.list": {},
    # Mensagens livres: o `Offering`; `free_messages` e `contact_email_set` não são anuláveis.
    "offerings.update_settings": {"activated_at": {"string", "null"}, "closed_at": {"string", "null"},
                                  "archived_at": {"string", "null"}, "bot": {"object", "null"}},
    # Relatórios: no `Report`, o título, a versão congelada (`frozen`), as taxas
    # (`RatePair`), a mediana e os quartis, e o rótulo, a indisponibilidade e as listas de cada campo da
    # distribuição (o `Pair` nunca é anulável); o cursor das versões; o resumo diário sem anulável.
    "reports.get": {
        "dispatch.title": {"string", "null"},
        "frozen": {"object", "null"},
        **{f"performance.{rate}.{side}": {"number", "null"}
           for rate in ("correct_rate", "partially_correct_rate", "incorrect_rate", "response_rate", "mean_score")
           for side in ("on_time", "with_late")},
        "response_time.median_ms": {"number", "null"},
        "response_time.q1_ms": {"number", "null"},
        "response_time.q3_ms": {"number", "null"},
        "distribution.fields[].label": {"string", "null"},
        "distribution.fields[].unavailable": {"string", "null"},
        "distribution.fields[].choices": {"array", "null"},
        "distribution.fields[].values": {"object", "null"},
        "distribution.fields[].ratios": {"object", "null"},
        "distribution.fields[].unrated": {"object", "null"},
        "distribution.fields[].texts": {"object", "null"},
    },
    "reports.list_versions": {"next_cursor": {"integer", "null"}},
    "reports.resend_summary": {},
    "offerings.get_digest": {},
    "offerings.update_digest": {},
}
# Disparo e resposta: em `submission.graded`, `ratio` e `residual` de cada parte são anuláveis.
EXPECTED_NULLABLE["events.list"] |= {"items[].data.parts[].ratio": {"number", "null"},
                                     "items[].data.parts[].residual": {"number", "null"}}
# Relatórios: o assunto de `notice.recorded` (os campos que não se aplicam
# ao tipo do aviso são nulos).
EXPECTED_NULLABLE["events.list"] |= {"items[].data.dispatch_id": {"string", "null"},
                                     "items[].data.report_version": {"integer", "null"},
                                     "items[].data.episode_seq": {"integer", "null"},
                                     "items[].data.batch_id": {"string", "null"},
                                     "items[].data.window_started_at": {"string", "null"},
                                     "items[].data.local_date": {"string", "null"}}
# Expurgo: `purged_at` (horário ou nulo) em toda resposta que embute o `Offering`
# (o caminho achatado do `Offering` em cada uma); as respostas do expurgo sem anulável; no `doctor`, o código do
# oferecimento (só em `bot.*`) e a contagem (só em `tasks.overdue`).
for _op_id, _prefix in (("offerings.create", ""), ("offerings.list", "items[]."), ("offerings.get", ""),
                        ("offerings.close", ""), ("offerings.prepare_archive", "offering."),
                        ("offerings.archive", ""), ("bots.activate", ""), ("offerings.update_settings", "")):
    EXPECTED_NULLABLE[_op_id] = {**EXPECTED_NULLABLE[_op_id], f"{_prefix}purged_at": {"string", "null"}}
EXPECTED_NULLABLE |= {
    "system.doctor": {"checks[].offering": {"string", "null"}, "checks[].count": {"integer", "null"}},
    "offerings.prepare_purge": {},
    "offerings.purge": {},
}

MARK_KEYS = {"x-identity", "x-secret", "x-free-text"}
# Disparo e resposta: a marca do conteúdo de terceiros, não confiável, sempre com `x-free-text`.
MARK_KEYS |= {"x-untrusted"}
COMBINATORS = ("allOf", "anyOf", "oneOf")
REFINEMENTS = ("allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependentSchemas")

UUID_SAMPLE = "3f2b8c1e-9a4d-4e7f-8b2a-1c0d9e8f7a6b"


def _published_doc() -> dict:
    """O OpenAPI que a app publica (`create_app`; montar o documento não abre conexão com o banco)."""
    from cq_server.api import create_app
    from cq_server.config import Settings

    return create_app(Settings(database_url="postgresql://cq-contrato.invalid/cq")).openapi()


def _published_operations(doc: dict) -> dict[str, dict]:
    """`operationId` → operação publicada."""
    return {item[method]["operationId"]: item[method] for item in doc["paths"].values()
            for method in ("get", "put", "post", "delete", "patch") if method in item
            and "operationId" in item[method]}


def _schema_files() -> list[pathlib.Path]:
    return sorted(SCHEMAS.glob("*.json"))


def _all_schema_files() -> list[pathlib.Path]:
    return [*_schema_files(), CATALOG_SCHEMA]


def _load(path: pathlib.Path) -> Any:
    return load_json(str(path))


def _response_file(op_id: str) -> pathlib.Path:
    return SCHEMAS / f"{op_id}.response.schema.json"


def _response_ops() -> list[str]:
    return [op["id"] for op in load_catalog()["operations"]]


def _type_set(node: dict) -> set[str]:
    t = node.get("type")
    if t is None:
        return set()
    return {t} if isinstance(t, str) else set(t)


def _is_object(node: dict) -> bool:
    return "object" in _type_set(node)


def _property_nodes(schema: dict) -> Iterator[tuple[str, dict]]:
    """(caminho achatado, subesquema) de cada propriedade e item, atravessando combinadores."""

    def walk(node: Any, path: str) -> Iterator[tuple[str, dict]]:
        if not isinstance(node, dict):
            return
        if path:
            yield path, node
        for key in COMBINATORS:
            for branch in node.get(key, []) or []:
                yield from walk(branch, path)
        for name, child in (node.get("properties") or {}).items():
            yield from walk(child, f"{path}.{name}" if path else name)
        if isinstance(node.get("items"), dict):
            yield from walk(node["items"], f"{path}[]")

    yield from walk(schema, "")


def _extension_keys(node: Any, pointer: str = "", in_names: bool = False) -> Iterator[tuple[str, str, Any]]:
    """Toda palavra-chave `x-*` do documento: (ponteiro, chave, valor).

    Dentro de `properties`/`$defs`, as chaves são nomes de propriedades, não palavras-chave.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if not in_names and key.startswith("x-"):
                yield pointer, key, value
            yield from _extension_keys(value, f"{pointer}/{key}", key in ("properties", "$defs") and not in_names)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _extension_keys(item, f"{pointer}/{i}")


def _open_object_problems(schema: dict) -> list[str]:
    """Objetos que aceitam campos fora do esquema ou dicionários livres."""
    problems: list[str] = []
    for obj in iter_objects(schema, schema):
        where = obj.get("title") or sorted((obj.get("properties") or {}).keys())
        if obj.get("additionalProperties") is not False:
            problems.append(f"objeto {where} sem additionalProperties: false")
        if obj.get("patternProperties"):
            problems.append(f"objeto {where} com patternProperties (dicionário livre)")
        if "unevaluatedProperties" in obj and obj["unevaluatedProperties"] is not False:
            problems.append(f"objeto {where} com unevaluatedProperties aberto")

    # Um subesquema com `properties` fora de um ramo de refinamento precisa declarar `type: object`;
    # senão `iter_objects` não o veria e um objeto aberto passaria despercebido.
    def walk(node: Any, pointer: str, refinement: bool) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{pointer}/{i}", refinement)
            return
        if not isinstance(node, dict):
            return
        shaped = any(k in node for k in ("properties", "additionalProperties", "patternProperties"))
        if shaped and not refinement and not _is_object(node):
            problems.append(f"{pointer or '/'}: tem properties mas não declara type: object")
        for key, value in node.items():
            if key in REFINEMENTS:
                walk(value, f"{pointer}/{key}", True)
            elif key in ("properties", "$defs", "patternProperties") and isinstance(value, dict):
                for name, child in value.items():
                    walk(child, f"{pointer}/{key}/{name}", False)
            elif key in ("items", "additionalProperties", "prefixItems", "contains"):
                walk(value, f"{pointer}/{key}", False)

    walk(schema, "", False)
    return problems


# ---------------------------------------------------------------------------------------------
# Arquivos e validade dos esquemas
# ---------------------------------------------------------------------------------------------


def test_schema_files_cover_the_catalog() -> None:
    """cada operação do catálogo tem esquema de resposta; pedido só onde há corpo.

    Oferecimentos e bots: só
    `offerings.create`, `offerings.archive`, `bots.register` e `team.link` somam esquema de pedido.

    Vínculo do aluno:
    `students.import`, `invites.prepare_send`, `invites.send` e `invites.resend` somam esquema de
    pedido (`students.summary` não).

    Disparo e resposta: só
    `dispatches.prepare` soma esquema de pedido.

    Mensagens livres:
    `offerings.update_settings` soma esquema de pedido. Vermelho: a operação nova não está no registro
    nem no OpenAPI publicado.

    Relatórios: só `offerings.update_digest` soma esquema de
    pedido. Vermelho: as 5 operações novas não estão no registro nem no OpenAPI publicado até a
    implementação.

    CLI: 39 operações, e só `offerings.purge` soma esquema de pedido.
    Vermelho: as 3 operações novas não estão no registro nem no OpenAPI publicado até a implementação."""
    catalog_ids = {op["id"] for op in load_catalog()["operations"]}
    assert set(CORE_OPERATIONS) <= catalog_ids
    assert catalog_ids == set(ALL_OPERATIONS)
    assert len(ALL_OPERATIONS) == len(set(ALL_OPERATIONS)) == 39
    names = {p.name for p in _schema_files()}

    for op_id in catalog_ids:
        assert f"{op_id}.response.schema.json" in names, f"{op_id} sem esquema de resposta"
    assert "error.schema.json" in names

    pattern = re.compile(r"^(?P<op>[a-z]+\.[a-z_]+)\.(?P<kind>request|response)\.schema\.json$")
    requests: set[str] = set()
    for name in names - {"error.schema.json"}:
        match = pattern.fullmatch(name)
        assert match, f"arquivo de esquema fora da convenção <operação>.<request|response>: {name}"
        assert match["op"] in catalog_ids, f"esquema {name} de operação fora do catálogo"
        if match["kind"] == "request":
            requests.add(match["op"])
    assert requests & set(CORE_OPERATIONS) == REQUEST_OPERATIONS
    assert requests == (REQUEST_OPERATIONS | OFFERING_BOT_REQUEST_OPERATIONS | STUDENT_LINK_REQUEST_OPERATIONS
                        | DISPATCH_REQUEST_OPERATIONS | REPORT_REQUEST_OPERATIONS | DOCTOR_PURGE_REQUEST_OPERATIONS)

    # a implementação bate com os arquivos: o registro de operações tem exatamente as operações do
    # catálogo, com modelo de pedido exatamente onde há esquema de pedido, e o OpenAPI publica todas,
    # com corpo exatamente nelas.
    from cq_server.api.operations import OPERATIONS

    registry = {op.id: op for op in OPERATIONS}
    assert sorted(catalog_ids - set(registry)) == [], "operações do catálogo fora do registro"
    assert set(registry) == catalog_ids
    assert {op_id for op_id, op in registry.items() if op.request_model is not None} == requests
    published = _published_operations(_published_doc())
    assert sorted(catalog_ids - set(published)) == [], "operações do catálogo fora do OpenAPI publicado"
    assert {op_id for op_id, op in published.items() if "requestBody" in op} == requests


@pytest.mark.parametrize("path", _all_schema_files(), ids=lambda p: p.name)
def test_every_file_is_a_valid_2020_12_schema(path: pathlib.Path) -> None:
    """cada arquivo passa em `check_schema` do JSON Schema 2020-12."""
    schema = _load(path)
    assert isinstance(schema, dict)
    assert schema.get("$schema") == DRAFT_2020_12
    assert validator_for(schema) is Draft202012Validator
    Draft202012Validator.check_schema(schema)
    # Os padrões compilam como expressão regular (o formato `regex` do metaesquema já os confere).
    for _, node in _property_nodes(schema):
        if "pattern" in node:
            re.compile(node["pattern"])


def test_schema_ids_are_unique() -> None:
    """`$id` presente e único em cada arquivo (sem colisão no registro de esquemas)."""
    ids = [_load(p).get("$id") for p in _all_schema_files()]
    assert all(isinstance(i, str) and i for i in ids)
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------------------------
# Objetos fechados
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", _all_schema_files(), ids=lambda p: p.name)
def test_every_object_is_closed(path: pathlib.Path) -> None:
    """todo objeto (pedido e resposta) é fechado, sem dicionário livre."""
    schema = _load(path)
    assert _is_object(schema), "a raiz de todo esquema de corpo é um objeto"
    assert list(iter_objects(schema, schema)), "nenhum objeto encontrado (checagem no vazio)"
    assert _open_object_problems(schema) == []


@pytest.mark.parametrize("schema, expected", [
    ({"type": "object", "additionalProperties": False,
      "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}}}}},
     ["objeto ['b'] sem additionalProperties: false"]),
    ({"type": "object", "additionalProperties": {"type": "string"}},
     ["objeto [] sem additionalProperties: false"]),
    ({"type": "object", "additionalProperties": False, "patternProperties": {"^x": {}}},
     ["objeto [] com patternProperties (dicionário livre)"]),
    ({"type": "object", "additionalProperties": False,
      "properties": {"a": {"properties": {"b": {}}}}},
     ["/properties/a: tem properties mas não declara type: object"]),
    ({"type": "object", "additionalProperties": False,
      "properties": {"a": {"type": ["object", "null"], "additionalProperties": False, "properties": {}}}},
     []),
])
def test_closure_check_detects_open_objects(schema: dict, expected: list[str]) -> None:
    """Controle positivo da checagem de fechamento: ela acusa objetos abertos (contra o vazio)."""
    assert _open_object_problems(schema) == expected


# ---------------------------------------------------------------------------------------------
# Respostas: obrigatoriedade e anuláveis
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _response_ops())
def test_response_properties_are_all_required(op_id: str) -> None:
    """em respostas, toda propriedade é `required`; anulável é `type: [X, "null"]`."""
    schema = _load(_response_file(op_id))
    objects = list(iter_objects(schema, schema))
    assert objects
    for obj in objects:
        props = set((obj.get("properties") or {}).keys())
        assert set(obj.get("required", [])) == props, f"{op_id}: required != properties em {sorted(props)}"
    for path, node in _property_nodes(schema):
        for key in COMBINATORS:
            for branch in node.get(key, []) or []:
                assert _type_set(branch) != {"null"}, (
                    f"{op_id}.{path}: anulável por combinador; use type: [X, \"null\"]")


def test_response_nullable_fields_match_contract() -> None:
    """No contrato: os campos anuláveis de cada resposta são exatamente os previstos.

    Oferecimentos e bots: também nas 10 respostas e no `problem` de
    `bot.access_changed`.

    Vínculo do aluno: as 5 respostas e os 8 tipos de
    evento, sem nenhum campo anulável."""
    actual: dict[str, dict[str, set[str]]] = {}
    for op_id in ALL_OPERATIONS:
        schema = _load(_response_file(op_id))
        nullable: dict[str, set[str]] = {}
        for path, node in _property_nodes(schema):
            types = _type_set(node)
            if "null" in types:
                nullable.setdefault(path, set()).update(types)
        actual[op_id] = nullable
    assert actual == EXPECTED_NULLABLE


# ---------------------------------------------------------------------------------------------
# Marcas
# ---------------------------------------------------------------------------------------------


def test_extension_keys_are_known_and_well_formed() -> None:
    """No contrato: só `x-identity` (name/ra/email), `x-secret: true` e `x-free-text: true`.

    Oferecimentos e bots: `x-secret` em
    exatamente 3 pontos: o `secret` da resposta de `tokens.create`, o `token` do **pedido** de
    `bots.register` e o `link` da resposta de `team.link` (o `confirmation_code` não é segredo).

    Vínculo do aluno: `x-secret` nos mesmos 3 pontos (o `confirmation_code` de
    `invites.prepare_send` não é segredo; o link e o código do convite de aluno nunca saem numa
    resposta); `x-identity` em exatamente 9 pontos: a resposta de `identity.reveal` e o pedido de
    `people.register` e o pedido de `students.import` (`rows[].name`, `rows[].ra` e
    `rows[].email`), cada campo com o seu tipo.

    Mensagens livres: `x-identity: email` em mais 2 pontos, ambos em
    pedido (`contact_email` de `offerings.create` e de `offerings.update_settings`): 11 pontos; nenhum
    `x-secret` a mais (são 3). Os 2 pontos são conferidos também no OpenAPI publicado.
    Vermelho: o pedido publicado de `offerings.create` não tem `contact_email`, e `offerings.update_settings`
    não está publicado."""
    seen = 0
    secret_points: list[tuple[str, str]] = []
    identity_points: list[tuple[str, str, str]] = []
    for path in _all_schema_files():
        for pointer, key, value in _extension_keys(_load(path)):
            seen += 1
            where = f"{path.name}{pointer}"
            assert key in MARK_KEYS, f"palavra-chave de extensão desconhecida {key} em {where}"
            if key == "x-identity":
                assert value in ("name", "ra", "email"), f"x-identity inválido em {where}"
                identity_points.append((path.name, pointer, value))
            else:
                assert value is True, f"{key} precisa ser true em {where}"
            if key == "x-secret":
                secret_points.append((path.name, pointer))
    assert seen > 0, "nenhuma marca encontrada (checagem no vazio)"
    assert sorted(secret_points) == [
        ("bots.register.request.schema.json", "/properties/token"),
        ("team.link.response.schema.json", "/properties/link"),
        ("tokens.create.response.schema.json", "/properties/secret"),
    ]
    # `x-identity` em exatamente estes pontos, com o tipo do próprio campo. Mensagens livres: mais os dois
    # `contact_email` de pedido (11 pontos).
    assert sorted(identity_points) == [
        ("identity.reveal.response.schema.json", "/properties/people/items/properties/email", "email"),
        ("identity.reveal.response.schema.json", "/properties/people/items/properties/name", "name"),
        ("identity.reveal.response.schema.json", "/properties/people/items/properties/ra", "ra"),
        ("offerings.create.request.schema.json", "/properties/contact_email", "email"),
        ("offerings.update_settings.request.schema.json", "/properties/contact_email", "email"),
        ("people.register.request.schema.json", "/properties/email", "email"),
        ("people.register.request.schema.json", "/properties/name", "name"),
        ("people.register.request.schema.json", "/properties/ra", "ra"),
        ("students.import.request.schema.json", "/properties/rows/items/properties/email", "email"),
        ("students.import.request.schema.json", "/properties/rows/items/properties/name", "name"),
        ("students.import.request.schema.json", "/properties/rows/items/properties/ra", "ra"),
    ]
    # Mensagens livres (paridade com a implementação, sem casos já verdes no esqueleto): os 2 pontos também no
    # OpenAPI que a app publica, no pedido das duas operações.
    doc = _published_doc()
    published = _published_operations(doc)
    for op_id in ("offerings.create", "offerings.update_settings"):
        assert op_id in published, f"{op_id} ausente do OpenAPI publicado"
        flat = dict(_published_request_flat(doc, published[op_id], op_id))
        assert flat.get("contact_email") == frozenset({"x-identity:email"}), (
            f"{op_id}: contact_email sem x-identity: email no pedido publicado ({flat.get('contact_email')})")


def _published_request_flat(doc: dict, op: dict, op_id: str) -> set[tuple[str, frozenset[str]]]:
    """Forma achatada (caminho, marcas) do corpo JSON do pedido publicado de uma operação."""
    assert "requestBody" in op, f"{op_id} sem requestBody no OpenAPI publicado"
    body = op["requestBody"]
    while "$ref" in body:
        target: Any = doc
        for part in body["$ref"][2:].split("/"):
            target = target[part]
        body = target
    content = body.get("content", {})
    assert "application/json" in content, f"{op_id}: pedido publicado sem application/json"
    return flatten(content["application/json"]["schema"], doc)


def test_response_marks_match_contract() -> None:
    """marcas nas respostas exatamente como o contrato; identidade só
    na revelação, segredo só na criação de token, texto livre só nos rótulos e em `data`.

    Oferecimentos e bots: `course` e `term` marcados `x-free-text` em toda
    resposta com `Offering`; nas respostas, segredo só no `secret` de `tokens.create` e no `link` de
    `team.link`; nenhuma resposta dessa área com identidade.

    Vínculo do aluno: as 5 respostas sem marca nenhuma (sem identidade,
    sem segredo e sem texto livre; o oferecimento sai como `OfferingRef`)."""
    actual: dict[str, dict[str, set[str]]] = {}
    for op_id in ALL_OPERATIONS:
        schema = _load(_response_file(op_id))
        actual[op_id] = {path: set(marks) for path, marks in flatten(schema, schema) if marks}
        # Nenhuma marca escondida fora dos caminhos que a forma achatada percorre.
        raw = list(_extension_keys(schema))
        expected_raw = sum(len(v) for v in EXPECTED_EVENT_MARKS.values()) if op_id == "events.list" else sum(
            len(m) for m in EXPECTED_RESPONSE_MARKS[op_id].values())
        assert len(raw) == expected_raw, f"{op_id}: marcas em lugares inesperados: {raw}"
    assert actual == EXPECTED_RESPONSE_MARKS

    error = _load(SCHEMAS / "error.schema.json")
    assert list(_extension_keys(error)) == [], "o envelope de erro não tem campo marcado"

    identity_ops = {op for op, marks in actual.items()
                    if any(m.startswith("x-identity") for ms in marks.values() for m in ms)}
    assert identity_ops == {"identity.reveal"}


def test_event_variant_marks_match_data_model() -> None:
    """No contrato: em cada tipo de evento, só o texto livre do tipo é marcado.

    Oferecimentos e bots: nos 14 tipos, só o `error` de
    `outbox.reply_failed`.

    Vínculo do aluno: os 8 tipos sem marca nenhuma (sem texto livre nem
    identidade)."""
    schema = _load(_response_file("events.list"))
    actual: dict[str, dict[str, set[str]]] = {}
    for variant in schema["properties"]["items"]["items"]["oneOf"]:
        event_type = variant["properties"]["type"]["const"]
        actual[event_type] = {path: set(marks) for path, marks in flatten(variant, schema) if marks}
    assert actual == EXPECTED_EVENT_MARKS


# ---------------------------------------------------------------------------------------------
# Padrões
# ---------------------------------------------------------------------------------------------


def test_response_patterns_match_contract() -> None:
    """horário, pseudônimos, segredo, `token_id`, `task_type`,
    `requested[]`, `not_found[]` e `actor_token_id` com o padrão do contrato.

    Oferecimentos e bots:
    `offering_id`, `username`, `link`, `confirmation_code` e os horários
    pelo nome; o código do oferecimento (e o `person` dos eventos) por caminho
    (`EXACT_PATH_PATTERNS`); o único outro `code` das respostas é o `problem.code` do `Bot`, um enum
    sem padrão.

    Vínculo do aluno: nas 5 respostas e nos 8
    eventos, `offering_id`, `pseudo_id`, `alias`, `confirmation_code` (22 caracteres, sem
    `x-secret`) e `expires_at` pelo nome, e o código do `OfferingRef` (`offering.code`) e o `person`
    dos eventos por caminho, com os padrões gerais e os de oferecimentos e bots."""
    seen: set[str] = set()
    seen_paths: set[str] = set()
    compound: dict[str, list[dict]] = {"not_found[]": [], "actor_token_id": []}
    for op_id in ALL_OPERATIONS:
        schema = _load(_response_file(op_id))
        for path, node in _property_nodes(schema):
            name = path.rsplit(".", 1)[-1]
            if name in EXACT_PATTERNS and _type_set(node) != {"null"}:
                assert node.get("pattern") == EXACT_PATTERNS[name], f"{op_id}.{path}: padrão errado"
                seen.add(name)
            if name in compound:
                compound[name].append(node)
            if path in EXACT_PATH_PATTERNS:
                assert node.get("pattern") == EXACT_PATH_PATTERNS[path], f"{op_id}.{path}: padrão errado"
                assert _type_set(node) == {"string"}, f"{op_id}.{path}: tipo {_type_set(node)}"
                seen_paths.add(path)
            elif name == "code":
                assert path == "problem.code" and "pattern" not in node, f"{op_id}.{path}: `code` inesperado"
    assert seen == set(EXACT_PATTERNS), f"campos sem ocorrência: {set(EXACT_PATTERNS) - seen}"
    assert seen_paths == set(EXACT_PATH_PATTERNS), (
        f"caminhos sem ocorrência: {set(EXACT_PATH_PATTERNS) - seen_paths}")

    code_rx = re.compile(CODE_PATTERN)
    for ok in ("mc102-2026s1", "abc", "0ab", "a-b", "a" * 40, "9" * 40):
        assert code_rx.search(ok), ok
    for bad in ("ab", "a" * 41, "-abc", "MC102-2026", "mc102_2026", "mc 102", "mc102é", "", "Zefiro"):
        assert not code_rx.search(bad), bad
    link_rx = re.compile(LINK_PATTERN)
    start = "Ab0_-" * 4 + "xy"
    assert len(start) == 22
    for ok in (f"https://t.me/cq_bot_teste?start={start}", f"https://t.me/ABCDE?start={start}"):
        assert link_rx.search(ok), ok
    for bad in (f"http://t.me/cq_bot_teste?start={start}", f"https://t.me/cq_b?start={start}",
                f"https://t.me/cq_bot_teste?start={start[:-1]}", f"https://t.me/cq_bot_teste?start={start}x",
                f"https://telegram.me/cq_bot_teste?start={start}", f"https://t.me/cq_bot_teste?start={start}&a=1",
                f"https://t.me/cq-bot-teste?start={start}", f"https://tXme/cq_bot_teste?start={start}"):
        assert not link_rx.search(bad), bad
    confirmation_rx = re.compile(CONFIRMATION_CODE_PATTERN)
    assert confirmation_rx.search(start)
    for bad in (start[:-1], start + "a", start[:-1] + "=", start[:-1] + " "):
        assert not confirmation_rx.search(bad), bad

    assert compound["not_found[]"] and compound["actor_token_id"]
    for node in compound["not_found[]"]:
        rx = re.compile(node["pattern"])
        for ok in ("a3f9", "0000000a", "abcdef0123456789abcdef0123456789", UUID_SAMPLE):
            assert rx.search(ok), ok
        for bad in ("123456", "9900112233", "abc", "ABCD", UUID_SAMPLE.upper(), "a3f9 ", "Zefiro",
                    "abcdef0123456789abcdef01234567890"):
            assert not rx.search(bad), bad
    for node in compound["actor_token_id"]:
        rx = re.compile(node["pattern"])
        for ok in ("bootstrap", UUID_SAMPLE):
            assert rx.search(ok), ok
        for bad in ("Bootstrap", "bootstrapx", "xbootstrap", UUID_SAMPLE.upper(), "", "a3f9"):
            assert not rx.search(bad), bad


# ---------------------------------------------------------------------------------------------
# estados, campos e problema do bot (contrato de oferecimentos e bots)
# ---------------------------------------------------------------------------------------------

# Forma exata de campos das 10 respostas de oferecimentos e bots, pelo nome: `course` e
# `term` sem `maxLength` na resposta (o texto redigido pode crescer), como o `label` dos tokens.
OFFERING_BOT_RESPONSE_FIELDS: dict[str, dict] = {
    "course": {"type": "string", "minLength": 1, "x-free-text": True},
    "term": {"type": "string", "minLength": 1, "x-free-text": True},
    "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
    "response_window_minutes": {"type": "integer", "minimum": 5, "maximum": 43200},
    "telegram_bot_id": {"type": "integer", "minimum": 1},
    "profile_state": {"enum": PROFILE_STATE_ENUM},
    "team_members_active": {"type": "integer", "minimum": 0},
    "pending_links": {"type": "integer", "minimum": 0},
    "worker_id": {"type": "string", "minLength": 1},
}
# Mensagens livres: os dois booleanos do
# `Offering`, obrigatórios, não anuláveis e sem marca.
OFFERING_BOT_RESPONSE_FIELDS |= {"free_messages": {"type": "boolean"}, "contact_email_set": {"type": "boolean"}}
# Os pedidos, propriedade a propriedade:
# (required, properties), conforme o contrato.
OFFERING_BOT_REQUEST_SCHEMAS: dict[str, tuple[list[str], dict[str, dict]]] = {
    "offerings.create": (["code", "course", "term", "timezone"], {
        "code": {"type": "string", "minLength": 3, "maxLength": 40, "pattern": CODE_PATTERN},
        "course": {"type": "string", "minLength": 1, "maxLength": 200},
        "term": {"type": "string", "minLength": 1, "maxLength": 40},
        "timezone": {"type": "string", "minLength": 1, "maxLength": 64},
        "response_window_minutes": {"type": "integer", "minimum": 5, "maximum": 43200},
    }),
    "offerings.archive": (["confirmation_code"], {
        "confirmation_code": {"type": "string", "minLength": 1, "maxLength": 64},
    }),
    "bots.register": (["token"], {
        "token": {"type": "string", "minLength": 1, "maxLength": 80, "pattern": BOT_TOKEN_PATTERN,
                  "x-secret": True},
    }),
    "team.link": (["person"], {
        "person": {"type": "string", "pattern": PERSON_REF_PATTERN},
        "valid_hours": {"type": "integer", "minimum": 1, "maximum": 720},
    }),
}
# Mensagens livres: o pedido de cadastro tem `free_messages` e
# `contact_email` (texto ou `null`, `x-identity: email`, sem limites no esquema: as regras do validador
# do vínculo do aluno ficam no servidor); `required` sem os dois.
OFFERING_BOT_REQUEST_SCHEMAS["offerings.create"][1].update({
    "free_messages": {"type": "boolean"},
    "contact_email": {"type": ["string", "null"], "x-identity": "email"},
})


def test_offering_bot_states_and_fields_match_contract() -> None:
    """Oferecimentos e bots, conforme o contrato:
    nas 10 respostas, os estados como enum na ordem do
    contrato (oferecimento `draft`·`active`·`closed`·`archived`; bot `registered`·`no_access`; perfil
    `pending`·`configured`·`failed`) e a forma exata de `course`, `term` (sem `maxLength`),
    `timezone`, `response_window_minutes`, `telegram_bot_id`, contagens e `worker_id`; os 4 pedidos
    propriedade a propriedade, com o token do bot no formato local esperado.

    Mensagens livres: o `Offering` com `free_messages` e
    `contact_email_set` (`{"type": "boolean"}`) e o pedido de `offerings.create` com `free_messages` e
    `contact_email` (em `OFFERING_BOT_RESPONSE_FIELDS` e `OFFERING_BOT_REQUEST_SCHEMAS`)."""
    seen: set[str] = set()
    states: dict[str, list] = {}
    for op_id in OFFERING_BOT_OPERATIONS:
        schema = _load(_response_file(op_id))
        for path, node in _property_nodes(schema):
            name = path.rsplit(".", 1)[-1]
            if name in OFFERING_BOT_RESPONSE_FIELDS:
                assert node == OFFERING_BOT_RESPONSE_FIELDS[name], f"{op_id}.{path}: {node}"
                seen.add(name)
            if name == "state":
                kind = "bot" if path.endswith("bot.state") or op_id in ("bots.register", "bots.get") \
                    else "offering"
                expected = BOT_STATE_ENUM if kind == "bot" else OFFERING_STATE_ENUM
                assert node == {"enum": expected}, f"{op_id}.{path}: {node}"
                states.setdefault(kind, []).append(f"{op_id}.{path}")
    assert seen == set(OFFERING_BOT_RESPONSE_FIELDS), f"campos sem ocorrência: {set(OFFERING_BOT_RESPONSE_FIELDS) - seen}"
    assert set(states) == {"bot", "offering"}

    for op_id, (required, properties) in OFFERING_BOT_REQUEST_SCHEMAS.items():
        schema = _load(SCHEMAS / f"{op_id}.request.schema.json")
        assert schema["type"] == "object" and schema["additionalProperties"] is False, op_id
        assert schema["required"] == required, op_id
        assert schema["properties"] == properties, op_id

    register = _load(SCHEMAS / "bots.register.request.schema.json")
    secret = "Ab0_-" * 7
    assert len(secret) == 35
    for ok in (f"6100000001:{secret}", f"1:{'a' * 30}", f"{'9' * 16}:{'Z' * 63}"):
        assert _is_valid(register, {"token": ok}), ok
    for bad in (f"6100000001:{secret[:29]}", f"0123:{secret}", f"bot6100000001:{secret}",
                f"{'9' * 17}:{secret}", f"6100000001:{secret}=", f"6100000001 :{secret}",
                f"{'9' * 16}:{'Z' * 64}", "", f" 6100000001:{secret}"):
        assert not _is_valid(register, {"token": bad}), bad
    assert not _is_valid(register, {"token": f"6100000001:{secret}", "bot_token": "x"})

    team = _load(SCHEMAS / "team.link.request.schema.json")
    for ok in ("a3f9", UUID_SAMPLE):
        assert _is_valid(team, {"person": ok}), ok
    for bad in ("123456", UUID_SAMPLE.upper(), "Zefiro", "a3f9 ", ""):
        assert not _is_valid(team, {"person": bad}), bad
    for hours, valid in ((1, True), (720, True), (0, False), (721, False), (1.5, False), (True, False)):
        assert _is_valid(team, {"person": "a3f9", "valid_hours": hours}) is valid, hours


def test_bot_problem_code_and_message_are_paired() -> None:
    """No `Bot` (`bots.register`
    e `bots.get`), `problem` é `null` ou `{code, message}` com os dois como enums dos 6 problemas,
    pareados um a um (`anyOf` de pares `const`); par trocado, código ou mensagem fora da tabela e
    campo a mais são recusados."""
    for op_id in ("bots.register", "bots.get"):
        problem = _load(_response_file(op_id))["properties"]["problem"]
        assert problem["type"] == ["object", "null"], op_id
        assert problem["required"] == ["code", "message"], op_id
        assert problem["properties"] == {"code": {"enum": list(BOT_PROBLEMS)},
                                         "message": {"enum": list(BOT_PROBLEMS.values())}}, op_id
        assert problem["anyOf"] == [
            {"properties": {"code": {"const": code}, "message": {"const": message}}}
            for code, message in BOT_PROBLEMS.items()], op_id

        assert _is_valid(problem, None), op_id
        for code, message in BOT_PROBLEMS.items():
            assert _is_valid(problem, {"code": code, "message": message}), (op_id, code)
            for other in BOT_PROBLEMS.values():
                if other != message:
                    assert not _is_valid(problem, {"code": code, "message": other}), (op_id, code, other)
            assert not _is_valid(problem, {"code": code, "message": message + " "}), (op_id, code)
            assert not _is_valid(problem, {"code": code}), (op_id, code)
            assert not _is_valid(problem, {"code": code, "message": message, "detail": "x"}), (op_id, code)
        assert not _is_valid(problem, {"code": "bot_no_access", "message": BOT_PROBLEMS["token_rejected"]})


# ---------------------------------------------------------------------------------------------
# Envelope de erro
# ---------------------------------------------------------------------------------------------


def _error_envelope(code: str) -> dict:
    status, message = CONTRACT_ERRORS[code]
    error: dict[str, Any] = {"code": code, "message": message}
    if code in FIELDS_CODES:
        error["fields"] = [{"field": "name", "reason": "missing"}]
    if status == 403:
        error["permission"] = "people:write"
    if code == "invalid_state":
        error["state"] = "active"
    # Expurgo: o `pending` do `purge_pending`, com as 5 contagens.
    if code == "purge_pending":
        error["pending"] = {"outbox": 1, "updates": 0, "tasks": 2, "freeze": 0, "due": 3}
    return {"error": error}


def _is_valid(schema: dict, body: Any) -> bool:
    return not list(Draft202012Validator(schema).iter_errors(body))


def test_error_schema_matches_contract_table() -> None:
    """No contrato: um ramo por código, mensagem exata, `fields` por código (`FIELDS_CODES`),
    `permission` só no 403, `reason` em `enum` e `field` com padrão.

    Oferecimentos e bots: os 7 códigos
    com a mensagem exata; `fields` obrigatório (lista não vazia) em exatamente `invalid_request`,
    `conflict`, `already_exists` e `bot_conflict` e proibido nos demais (inclusive nos 409 dessa área);
    `state` (enum dos 4 estados) obrigatório no `invalid_state` e proibido nos demais; `permission`
    com as 8 permissões.

    Vínculo do aluno: os 2 códigos (`list_conflict` e
    `role_conflict`, 409) com a mensagem exata e `fields` obrigatório (16 códigos); os motivos e o
    padrão de campo comum, com `rows`, `rows[i]` (até `rows[999]`) e `pseudonyms[i]` (até
    `pseudonyms[99]`) aceitos e `rows[i].<coluna>` recusado; os envelopes do contrato
    (`list_conflict` com `duplicate` e `invalid_value` por linha; `role_conflict` com
    `[{person, invalid_value}]`) válidos.

    Disparo e resposta: os 3 códigos
    (`invalid_dispatch_state`, `window_expired` e `grading_pending`, 409) com a mensagem exata e sem
    `fields`, `state` nem `permission` (19 códigos); os campos do pedido de preparo (`repo`, `commit`,
    `qid`, `opens_at`, `closes_at`) no padrão de campo comum.

    Expurgo: os 2 códigos (`purge_pending` e
    `purge_changed`, 409) com a mensagem exata e sem `fields`, `state` nem `permission` (21 códigos); `pending`
    (as 5 contagens `{outbox, updates, tasks, freeze, due}`, inteiros ≥ 0, objeto fechado) obrigatório no
    `purge_pending` e proibido nos outros 20 códigos (no `error.schema.json`, por uma regra `if`/`else` no objeto
    `error`, sem tocar os 19 ramos congelados) e no envelope publicado (o
    `ErrorDetail` do OpenAPI de `create_app`, com os 21 códigos e a propriedade `pending`). Vermelho:
    `cq_server.api.errors` e o envelope publicado sem os 2 códigos nem o `pending` até a implementação."""
    assert MESSAGES == {code: message for code, (_, message) in CONTRACT_ERRORS.items()}
    assert STATUS_OF == {code: status for code, (status, _) in CONTRACT_ERRORS.items()}
    assert len(CONTRACT_ERRORS) == 21
    # a tabela de erros da implementação (`cq_server.api.errors`) é a do contrato, com os 3 códigos
    # do disparo (409, a mensagem exata) e os mesmos códigos com `fields`.
    from cq_server.api import errors as server_errors

    assert sorted(set(CONTRACT_ERRORS) - set(server_errors.MESSAGES)) == [], (
        "códigos do contrato ausentes de cq_server.api.errors")
    assert server_errors.MESSAGES == {code: message for code, (_, message) in CONTRACT_ERRORS.items()}
    assert server_errors.STATUS == {code: status for code, (status, _) in CONTRACT_ERRORS.items()}
    assert set(server_errors.FIELDS_CODES) == FIELDS_CODES
    assert FIELDS_CODES == {"invalid_request", "conflict", "already_exists", "bot_conflict",
                            "list_conflict", "role_conflict"}
    assert list(OFFERING_STATES) == OFFERING_STATE_ENUM

    schema = _load(SCHEMAS / "error.schema.json")
    error = schema["properties"]["error"]
    assert set(error["properties"]["code"]["enum"]) == set(CONTRACT_ERRORS)
    assert len(error["oneOf"]) == len(CONTRACT_ERRORS)
    assert set(error["properties"]["fields"]["items"]["properties"]["reason"]["enum"]) == CONTRACT_REASONS
    assert error["properties"]["fields"]["items"]["properties"]["field"]["pattern"] == FIELD_PATTERN
    assert set(error["properties"]["permission"]["enum"]) == PERMISSIONS
    assert error["properties"]["state"] == {"enum": OFFERING_STATE_ENUM}

    for code, (status, message) in CONTRACT_ERRORS.items():
        good = _error_envelope(code)
        assert _is_valid(schema, good), code

        wrong_message = copy.deepcopy(good)
        wrong_message["error"]["message"] = message + " "
        assert not _is_valid(schema, wrong_message), f"{code}: aceita outra mensagem"

        extra = copy.deepcopy(good)
        extra["error"]["detail"] = "valor"
        assert not _is_valid(schema, extra), f"{code}: aceita campo extra"
        assert not _is_valid(schema, {**good, "detail": "valor"}), f"{code}: envelope aberto"

        if code in FIELDS_CODES:
            without = copy.deepcopy(good)
            del without["error"]["fields"]
            assert not _is_valid(schema, without), f"{code}: aceita sem fields"
            empty = copy.deepcopy(good)
            empty["error"]["fields"] = []
            assert not _is_valid(schema, empty), f"{code}: aceita fields vazio"
        else:
            with_fields = copy.deepcopy(good)
            with_fields["error"]["fields"] = [{"field": "name", "reason": "missing"}]
            assert not _is_valid(schema, with_fields), f"{code}: aceita fields fora de FIELDS_CODES"

        if code == "invalid_state":
            for state in OFFERING_STATE_ENUM:
                other = copy.deepcopy(good)
                other["error"]["state"] = state
                assert _is_valid(schema, other), f"{code}: recusa o estado {state}"
            without = copy.deepcopy(good)
            del without["error"]["state"]
            assert not _is_valid(schema, without), f"{code}: aceita sem state"
            for unknown in ("deleted", "Active", "", None, ["active"]):
                other = copy.deepcopy(good)
                other["error"]["state"] = unknown
                assert not _is_valid(schema, other), f"{code}: aceita o estado {unknown!r}"
        else:
            with_state = copy.deepcopy(good)
            with_state["error"]["state"] = "active"
            assert not _is_valid(schema, with_state), f"{code}: aceita state fora do invalid_state"

        if status == 403:
            without = copy.deepcopy(good)
            del without["error"]["permission"]
            assert not _is_valid(schema, without), f"{code}: aceita sem permission"
            unknown = copy.deepcopy(good)
            unknown["error"]["permission"] = "people:delete"
            assert not _is_valid(schema, unknown), f"{code}: aceita permissão desconhecida"
        else:
            with_permission = copy.deepcopy(good)
            with_permission["error"]["permission"] = "people:read"
            assert not _is_valid(schema, with_permission), f"{code}: aceita permission fora do 403"

        # Expurgo: `pending` obrigatório no `purge_pending` e proibido nos outros 20 ramos.
        if code == "purge_pending":
            without = copy.deepcopy(good)
            del without["error"]["pending"]
            assert not _is_valid(schema, without), f"{code}: aceita sem pending"
            for key in PENDING_KEYS:
                missing = copy.deepcopy(good)
                del missing["error"]["pending"][key]
                assert not _is_valid(schema, missing), f"{code}: aceita pending sem {key}"
                for bad in (-1, 1.5, "1", None, True):
                    other = copy.deepcopy(good)
                    other["error"]["pending"][key] = bad
                    assert not _is_valid(schema, other), f"{code}: aceita pending.{key} = {bad!r}"
            zero = copy.deepcopy(good)
            zero["error"]["pending"] = dict.fromkeys(PENDING_KEYS, 0)
            assert _is_valid(schema, zero), f"{code}: recusa pending com contagens zero"
            opened = copy.deepcopy(good)
            opened["error"]["pending"]["names"] = 0
            assert not _is_valid(schema, opened), f"{code}: aceita pending aberto"
        else:
            with_pending = copy.deepcopy(good)
            with_pending["error"]["pending"] = dict.fromkeys(PENDING_KEYS, 0)
            assert not _is_valid(schema, with_pending), f"{code}: aceita pending fora do purge_pending"

    base = _error_envelope("invalid_request")
    for reason in sorted(CONTRACT_REASONS):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": "name", "reason": reason}]
        assert _is_valid(schema, body), reason
    for field in ("body", "query", "X-CQ-Client", "name", "pseudonyms[2]", "permissions[0]",
                  "types[1]", "token_id", "ref"):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": field, "reason": "invalid_format"}]
        assert _is_valid(schema, body), field
    for field in ("Zefiro", "x-cq-client", "name.first", "pseudonyms[]", "", "body ", "types[-1]",
                  "Nome Canário"):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": field, "reason": "invalid_format"}]
        assert not _is_valid(schema, body), field
    # os campos dos pedidos e o `ref` das rotas casam com o padrão.
    for field in ("code", "course", "term", "timezone", "response_window_minutes", "confirmation_code",
                  "token", "person", "valid_hours", "after", "limit"):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": field, "reason": "invalid_format"}]
        assert _is_valid(schema, body), field
    body = copy.deepcopy(base)
    body["error"]["fields"] = [{"field": "name", "reason": "bad"}]
    assert not _is_valid(schema, body)

    # Vínculo do aluno: os campos dos pedidos do vínculo casam com o padrão comum; o erro por
    # linha da importação é `rows[i]`, sem a coluna. Disparo e resposta: os campos do pedido de
    # preparo também.
    for field in ("rows", "rows[0]", "rows[999]", "pseudonyms", "pseudonyms[0]", "pseudonyms[99]",
                  "valid_hours", "confirmation_code", "person", "repo", "commit", "qid", "opens_at",
                  "closes_at"):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": field, "reason": "invalid_format"}]
        assert _is_valid(schema, body), field
    for field in ("rows[0].email", "rows[].ra", "rows[-1]", "Rows[0]", "rows[0] "):
        body = copy.deepcopy(base)
        body["error"]["fields"] = [{"field": field, "reason": "invalid_format"}]
        assert not _is_valid(schema, body), field
    list_conflict = _error_envelope("list_conflict")
    list_conflict["error"]["fields"] = [{"field": "rows[3]", "reason": "duplicate"},
                                        {"field": "rows[7]", "reason": "invalid_value"}]
    assert _is_valid(schema, list_conflict)
    role_conflict = _error_envelope("role_conflict")
    role_conflict["error"]["fields"] = [{"field": "person", "reason": "invalid_value"}]
    assert _is_valid(schema, role_conflict)
    for code in ("list_conflict", "role_conflict"):
        swapped = _error_envelope(code)
        swapped["error"]["message"] = MESSAGES["conflict"]
        assert not _is_valid(schema, swapped), f"{code}: aceita a mensagem do conflict"

    # Expurgo: o objeto `pending` exato do esquema; o ramo do `purge_pending` o exige
    # e não o proíbe; nos outros 20 códigos, a proibição é a regra exata no objeto `error` (`if` o código é
    # `purge_pending`, nada; `else`, o `pending` proibido; sem `then`), sem tocar os 19 ramos congelados; e o
    # envelope que a app publica (`ErrorDetail` no OpenAPI de `create_app`) com os 21 códigos e a propriedade
    # `pending`, opcional.
    assert error["properties"]["pending"] == {
        "type": "object", "additionalProperties": False, "required": PENDING_KEYS,
        "properties": {key: {"type": "integer", "minimum": 0} for key in PENDING_KEYS}}
    branch_codes = []
    for branch in error["oneOf"]:
        branch_code = branch["properties"]["code"]["const"]
        branch_codes.append(branch_code)
        forbidden = [name for alternative in branch.get("not", {}).get("anyOf", [])
                     for name in alternative.get("required", [])]
        if branch_code == "purge_pending":
            assert "pending" in branch.get("required", []), "purge_pending sem pending obrigatório no ramo"
            assert "pending" not in forbidden, "purge_pending com pending proibido no ramo"
    assert sorted(branch_codes) == sorted(CONTRACT_ERRORS)
    assert error.get("if") == {"properties": {"code": {"const": "purge_pending"}}}, "regra do pending ausente"
    assert error.get("else") == {"not": {"required": ["pending"]}}, "pending não proibido fora do purge_pending"
    assert "then" not in error, "regra do pending com then"
    detail = _published_doc()["components"]["schemas"].get("ErrorDetail")
    assert detail is not None, "ErrorDetail ausente do OpenAPI publicado"
    published_codes = detail["properties"]["code"].get("enum", [])
    assert sorted(set(CONTRACT_ERRORS) - set(published_codes)) == [], (
        "códigos de erro ausentes do envelope publicado")
    assert set(published_codes) == set(CONTRACT_ERRORS)
    assert "pending" in detail["properties"], "o envelope publicado não tem a propriedade pending"
    assert "pending" not in detail.get("required", []), "pending obrigatório em todo erro publicado"


# ---------------------------------------------------------------------------------------------
# Eventos
# ---------------------------------------------------------------------------------------------

_SAMPLE_DATA: dict[str, dict] = {
    "person.registered": {"pseudo_id": UUID_SAMPLE, "alias": "a3f9"},
    "token.created": {"token_id": UUID_SAMPLE, "label": "rotulo", "permissions": ["people:read"]},
    "token.revoked": {"token_id": UUID_SAMPLE},
    "identity.revealed": {"client": "cli", "revealed": [UUID_SAMPLE], "not_found_count": 0,
                          "reason": "conferencia"},
    "identity.reveal_denied": {"client": "other", "requested": [UUID_SAMPLE], "not_found_count": 1},
    "task.failed": {"task_id": 1, "task_type": "demo.task", "attempts": 3, "error": "ValueError"},
    "outbox.failed": {"message_id": 1, "recipient": UUID_SAMPLE, "attempts": 1, "error": "bloqueado"},
    # Oferecimentos e bots.
    "offering.created": {"offering_id": UUID_SAMPLE, "code": "mc102-2026s1"},
    "offering.state_changed": {"offering_id": UUID_SAMPLE, "from_state": "draft", "to_state": "active"},
    "offering.archive_prepared": {"offering_id": UUID_SAMPLE, "expires_at": "2031-01-01T00:10:00Z"},
    "bot.registered": {"offering_id": UUID_SAMPLE, "telegram_bot_id": 6100000001, "username": "cq_bot_teste"},
    "bot.token_replaced": {"offering_id": UUID_SAMPLE, "telegram_bot_id": 6100000001, "token_version": 2},
    "bot.profile_updated": {"offering_id": UUID_SAMPLE, "profile_state": "configured", "purpose": "active"},
    "bot.lease_changed": {"offering_id": UUID_SAMPLE, "worker_id": "worker-a", "change": "acquired",
                          "reason": "acquired"},
    "bot.access_changed": {"offering_id": UUID_SAMPLE, "access": "lost", "problem": "token_rejected"},
    "team.link_created": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE,
                          "expires_at": "2031-01-08T00:00:00Z"},
    "team.link_revoked": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "revoke_reason": "replaced"},
    "team.joined": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "endpoint_replaced": False},
    "team.join_refused": {"offering_id": UUID_SAMPLE, "refusal": "invalid"},
    "endpoint.blocked": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "channel": "telegram"},
    "outbox.reply_failed": {"message_id": 1, "reply_to": 1, "attempts": 3, "error": "request_rejected"},
    # Vínculo do aluno.
    "student.list_imported": {"offering_id": UUID_SAMPLE, "received": 3, "people_created": 1,
                              "people_reused": 1, "enrollments_created": 2, "already_enrolled": 1},
    "student.invites_prepared": {"offering_id": UUID_SAMPLE, "eligible": 2, "valid_hours": 168,
                                 "expires_at": "2031-01-01T00:10:00Z"},
    "student.invites_queued": {"offering_id": UUID_SAMPLE, "origin": "batch", "queued": 2},
    "student.invite_sent": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "origin": "batch",
                            "expires_at": "2031-01-08T00:00:00Z"},
    "student.invite_failed": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "origin": "coordination",
                              "failure": "recipient_refused"},
    "student.invite_cancelled": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "origin": "batch",
                                 "cancel_reason": "superseded"},
    "student.invite_revoked": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "origin": "self_service",
                               "revoke_reason": "replaced"},
    "student.joined": {"offering_id": UUID_SAMPLE, "person": UUID_SAMPLE, "terms_version": 1,
                       "endpoint_replaced": False},
    # Disparo e resposta.
    "dispatch.prepared": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE,
                          "question_ref_id": UUID_SAMPLE, "origin": "cli"},
    "dispatch.validated": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "target_count": 3,
                           "warnings": ["repeated_question"]},
    "dispatch.rejected": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE,
                          "rejection_reason": "question_broken"},
    "dispatch.confirmed": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "target_count": 3,
                           "opens_at": "2031-01-01T00:00:00Z", "closes_at": "2031-01-01T01:00:00Z"},
    "dispatch.cancelled": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "cancel_reason": "operator"},
    "dispatch.opened": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "target_count": 3},
    "dispatch.closed": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "trigger": "window",
                        "counts": {"pending": 0, "delivered": 0, "not_delivered": 1, "answered": 1,
                                   "dont_know": 0, "no_answer": 1, "answered_late": 0, "question_failed": 0}},
    "delivery.delivered": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "delivery_id": UUID_SAMPLE,
                           "person": UUID_SAMPLE},
    "delivery.not_delivered": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE,
                               "delivery_id": UUID_SAMPLE, "person": UUID_SAMPLE,
                               "undelivered_reason": "endpoint_blocked"},
    "delivery.question_failed": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE,
                                 "delivery_id": UUID_SAMPLE, "person": UUID_SAMPLE, "failure": "variant_broken"},
    "delivery.dont_know": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "delivery_id": UUID_SAMPLE,
                           "submission_id": UUID_SAMPLE, "person": UUID_SAMPLE},
    "submission.graded": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "delivery_id": UUID_SAMPLE,
                          "submission_id": UUID_SAMPLE, "question_ref_id": UUID_SAMPLE, "person": UUID_SAMPLE,
                          "valid": True, "late": False, "normalized": False, "score": 1.0,
                          "parts": [{"index": 0, "score": 1.0, "choices": ["sha256:" + "a" * 64],
                                     "ratio": None, "residual": None}]},
    "submission.internal_failure": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE,
                                    "delivery_id": UUID_SAMPLE, "submission_id": UUID_SAMPLE,
                                    "person": UUID_SAMPLE, "engine_error": "invalid_request"},
    # Mensagens livres.
    "offering.settings_changed": {"offering_id": UUID_SAMPLE, "free_messages": True, "contact_email_set": False},
    # Relatórios.
    "report.frozen": {"offering_id": UUID_SAMPLE, "dispatch_id": UUID_SAMPLE, "version": 1, "trigger": "closing"},
    "notice.recorded": {"offering_id": UUID_SAMPLE, "notice_type": "dispatch_closed", "dispatch_id": UUID_SAMPLE,
                        "report_version": 1, "episode_seq": None, "batch_id": None, "window_started_at": None,
                        "local_date": None, "state": "queued", "recipient_count": 2},
    "offering.digest_changed": {"offering_id": UUID_SAMPLE, "enabled": True, "local_time": "20:00"},
    # Expurgo.
    "offering.purge_prepared": {"offering_id": UUID_SAMPLE, "expires_at": "2031-01-01T00:10:00Z",
                                "identities_to_delete": 2, "identities_kept": 1, "endpoints": 2, "updates": 5,
                                "contact_email_set": True},
    "offering.purged": {"offering_id": UUID_SAMPLE, "identities_deleted": 2, "identities_kept": 1, "endpoints": 2,
                        "updates": 5, "free_messages_redacted": 3, "outbox_redacted": 1,
                        "contact_email_deleted": True},
}

# combinações recusadas pelos refinamentos das variantes: `reason =
# acquired` só com `change = acquired`; `problem` nulo só em `restored`; e valores fora do enum.
_OFFERING_BOT_INVALID_DATA: list[tuple[str, dict]] = [
    ("bot.lease_changed", {"change": "acquired", "reason": "shutdown"}),
    ("bot.lease_changed", {"change": "released", "reason": "acquired"}),
    ("bot.lease_changed", {"change": "renewed", "reason": "acquired"}),
    ("bot.access_changed", {"access": "lost", "problem": None}),
    ("bot.access_changed", {"access": "restored", "problem": "token_rejected"}),
    ("bot.access_changed", {"access": "lost", "problem": "telegram_unavailable"}),
    ("offering.state_changed", {"to_state": "deleted"}),
    ("bot.profile_updated", {"profile_state": "pending"}),
    ("bot.token_replaced", {"token_version": 1}),
    ("bot.registered", {"telegram_bot_id": 0}),
    ("bot.registered", {"username": "@cq_bot_teste"}),
    ("team.joined", {"endpoint_replaced": "false"}),
    ("endpoint.blocked", {"channel": "email"}),
    ("offering.created", {"code": "MC102"}),
    ("outbox.reply_failed", {"reply_to": 0}),
]
_OFFERING_BOT_VALID_DATA: list[tuple[str, dict]] = [
    ("bot.lease_changed", {"change": "released", "reason": reason})
    for reason in ("not_eligible", "shutdown", "archived", "no_access")
] + [
    ("bot.access_changed", {"access": "lost", "problem": "token_unreadable"}),
    ("bot.access_changed", {"access": "restored", "problem": None}),
    ("team.joined", {"endpoint_replaced": True}),
    ("team.join_refused", {"refusal": "other_person"}),
    ("bot.profile_updated", {"profile_state": "failed", "purpose": "archived"}),
]

# Vínculo do aluno: valores fora dos enums e dos limites recusados; o tipo do
# erro (ex.: `SMTPRecipientsRefused`) nunca entra no evento, só a classe da falha.
_STUDENT_LINK_INVALID_DATA: list[tuple[str, dict]] = [
    ("student.invites_queued", {"origin": "self_service"}),
    ("student.invites_queued", {"queued": -1}),
    ("student.invites_queued", {"queued": 1.5}),
    ("student.invite_sent", {"origin": "self-service"}),
    ("student.invite_sent", {"person": "a3f9"}),
    ("student.invite_sent", {"expires_at": "2031-01-08T00:00:00+00:00"}),
    ("student.invite_failed", {"failure": "SMTPRecipientsRefused"}),
    ("student.invite_failed", {"failure": "temporary"}),
    ("student.invite_cancelled", {"cancel_reason": "replaced"}),
    ("student.invite_cancelled", {"cancel_reason": "archived"}),
    ("student.invite_revoked", {"revoke_reason": "superseded"}),
    ("student.invite_revoked", {"revoke_reason": "offering_inactive"}),
    ("student.joined", {"terms_version": 0}),
    ("student.joined", {"terms_version": "1"}),
    ("student.joined", {"endpoint_replaced": "false"}),
    ("student.invites_prepared", {"valid_hours": 0}),
    ("student.invites_prepared", {"valid_hours": 721}),
    ("student.invites_prepared", {"eligible": -1}),
    ("student.list_imported", {"received": -1}),
    ("student.list_imported", {"offering_id": "mc102-2026s1"}),
]
_STUDENT_LINK_VALID_DATA: list[tuple[str, dict]] = [
    ("student.invites_queued", {"origin": "coordination", "queued": 0}),
    ("student.invite_sent", {"origin": "self_service"}),
    ("student.invite_sent", {"origin": "coordination"}),
    ("student.invite_revoked", {"origin": "batch"}),
    ("student.joined", {"terms_version": 2, "endpoint_replaced": True}),
    ("student.invites_prepared", {"eligible": 0, "valid_hours": 1}),
    ("student.invites_prepared", {"valid_hours": 720}),
    ("student.list_imported", {"received": 0, "people_created": 0, "people_reused": 0,
                               "enrollments_created": 0, "already_enrolled": 0}),
] + [
    ("student.invite_failed", {"failure": failure})
    for failure in ("auth_refused", "server_refused", "not_configured", "attempts_exhausted")
] + [
    ("student.invite_cancelled", {"cancel_reason": reason})
    for reason in ("offering_inactive", "already_linked")
] + [
    ("student.invite_revoked", {"revoke_reason": reason}) for reason in ("archived", "refused")
]

# Disparo e resposta: valores fora dos enums (inclusive os nomes
# superados), `counts` sem uma chave de estado ou com chave a mais, a parte com o nome do campo do PL ou
# com o resumo fora do formato, `valid` falso e horário com deslocamento recusados; os valores de cada
# enum, a nota fora de [0, 1], as listas vazias e as contagens 0 aceitos. O corpo congelado percorre
# `_OFFERING_BOT_* + _STUDENT_LINK_*`; os do disparo entram somados às listas do vínculo do aluno.
_DISPATCH_COUNTS = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0, "no_answer": 0,
              "answered_late": 0, "question_failed": 0}
_DISPATCH_PART = {"index": 0, "score": 1.0, "choices": ["sha256:" + "a" * 64], "ratio": None, "residual": None}
_DISPATCH_INVALID_DATA: list[tuple[str, dict]] = [
    ("dispatch.prepared", {"origin": "web"}),
    ("dispatch.prepared", {"origin": "CLI"}),
    ("dispatch.prepared", {"question_ref_id": "a3f9"}),
    ("dispatch.validated", {"warnings": ["options_degraded"]}),
    ("dispatch.validated", {"warnings": "repeated_question"}),
    ("dispatch.validated", {"target_count": -1}),
    ("dispatch.rejected", {"rejection_reason": "repo_unreachable"}),
    ("dispatch.rejected", {"rejection_reason": "internal_error"}),
    ("dispatch.confirmed", {"opens_at": "2031-01-01T00:00:00+00:00"}),
    ("dispatch.confirmed", {"closes_at": None}),
    ("dispatch.cancelled", {"cancel_reason": "superseded"}),
    ("dispatch.cancelled", {"cancel_reason": "opening_passed_during_validation"}),
    ("dispatch.opened", {"target_count": 1.5}),
    ("dispatch.closed", {"trigger": "manual"}),
    ("dispatch.closed", {"counts": {k: v for k, v in _DISPATCH_COUNTS.items() if k != "question_failed"}}),
    ("dispatch.closed", {"counts": {**_DISPATCH_COUNTS, "grading_pending": 0}}),
    ("dispatch.closed", {"counts": {**_DISPATCH_COUNTS, "pending": -1}}),
    ("delivery.delivered", {"person": "a3f9"}),
    ("delivery.delivered", {"delivery_id": UUID_SAMPLE.upper()}),
    ("delivery.not_delivered", {"undelivered_reason": "blocked"}),
    ("delivery.not_delivered", {"undelivered_reason": "window_closed"}),
    ("delivery.question_failed", {"failure": "grading_error"}),
    ("delivery.dont_know", {"submission_id": "1"}),
    ("submission.graded", {"valid": False}),
    ("submission.graded", {"score": "1"}),
    ("submission.graded", {"late": "false"}),
    ("submission.graded", {"parts": [{**_DISPATCH_PART, "choices": ["sha256:abc"]}]}),
    ("submission.graded", {"parts": [{**_DISPATCH_PART, "index": -1}]}),
    ("submission.graded", {"parts": [{**_DISPATCH_PART, "name": "x"}]}),
    ("submission.graded", {"parts": [{**_DISPATCH_PART, "ratio": "0.5"}]}),
    ("submission.internal_failure", {"engine_error": "engine_rejected_request"}),
]
_DISPATCH_VALID_DATA: list[tuple[str, dict]] = [
    ("dispatch.prepared", {"origin": origin}) for origin in ("cli", "api", "mcp", "integration")
] + [
    ("dispatch.validated", {"target_count": 0, "warnings": []}),
    ("dispatch.validated", {"warnings": ["console_output", "missing_media", "media_error", "empty_target",
                                         "too_many_options", "repeated_question"]}),
    ("dispatch.confirmed", {"target_count": 0, "opens_at": "2031-01-01T00:00:00.123456Z"}),
    ("dispatch.opened", {"target_count": 0}),
    ("dispatch.closed", {"trigger": "archive", "counts": _DISPATCH_COUNTS}),
    ("submission.graded", {"score": 1.5, "late": True, "normalized": True, "parts": []}),
    ("submission.graded", {"score": -0.25}),
    ("submission.graded", {"parts": [{"index": 1, "score": 0.5, "choices": [], "ratio": 0.5,
                                      "residual": -0.1}, _DISPATCH_PART]}),
    ("submission.internal_failure", {"engine_error": "payload_too_large"}),
] + [
    ("dispatch.rejected", {"rejection_reason": reason})
    for reason in ("repository_unreachable", "repository_refused", "commit_not_found", "question_not_found",
                   "question_unsupported", "question_broken", "engine_unavailable",
                   "channel_limits_exceeded", "internal_failure")
] + [
    ("dispatch.cancelled", {"cancel_reason": reason})
    for reason in ("operator", "expired", "offering_closed", "window_missed", "validation_overran")
] + [
    ("delivery.not_delivered", {"undelivered_reason": reason})
    for reason in ("endpoint_blocked", "channel_refused", "engine_unavailable", "bot_no_access",
                   "channel_unavailable")
] + [
    ("delivery.question_failed", {"failure": failure})
    for failure in ("variant_broken", "variant_unsupported", "channel_limits_exceeded", "grading_broken",
                    "engine_rejected")
]
_STUDENT_LINK_INVALID_DATA += _DISPATCH_INVALID_DATA
_STUDENT_LINK_VALID_DATA += _DISPATCH_VALID_DATA
# Mensagens livres: booleanos estritos, o oferecimento por UUID e o e-mail nunca no evento.
# Contato do professor: `no_teacher_contact` é o 7º aviso aceito em `warnings`.
_STUDENT_LINK_INVALID_DATA += [
    ("offering.settings_changed", {"free_messages": "true"}),
    ("offering.settings_changed", {"free_messages": None}),
    ("offering.settings_changed", {"contact_email_set": 1}),
    ("offering.settings_changed", {"offering_id": "mc102-2026s1"}),
    ("offering.settings_changed", {"contact_email": "prof.livres@docentes.example.org"}),
    ("dispatch.validated", {"warnings": ["teacher_contact_missing"]}),
]
_STUDENT_LINK_VALID_DATA += [
    ("offering.settings_changed", {"free_messages": False, "contact_email_set": True}),
    ("offering.settings_changed", {"free_messages": True, "contact_email_set": True}),
    ("dispatch.validated", {"warnings": ["no_teacher_contact"]}),
    ("dispatch.validated", {"warnings": ["console_output", "missing_media", "media_error", "empty_target",
                                         "too_many_options", "repeated_question", "no_teacher_contact"]}),
]


def _event_page(event_type: str, data: dict) -> dict:
    return {"items": [{"seq": 1, "type": event_type, "occurred_at": "2031-01-01T00:00:00Z",
                       "actor_token_id": None, "data": data}], "next_cursor": 1}


def test_events_schema_variants_match_data_model() -> None:
    """No contrato: `items.items.oneOf` com uma variante por tipo, `type: {const}`,
    `data` fechado com as chaves exatas; `EventType` com os mesmos tipos.

    Oferecimentos e bots: os 14 tipos de oferecimentos e bots, depois dos 7 gerais,
    cada um com um conjunto de campos distinto dos demais; os valores enumerados exatos; os
    refinamentos (`reason = acquired` só com `change = acquired`; `problem` nulo só em `restored`).
    Vermelho no esqueleto: `EventType` só ganha os 14 tipos na implementação.

    Vínculo do aluno: os 8 tipos de alunos,
    cada um com um conjunto de campos distinto; os valores
    enumerados exatos (`origin`, só `batch` e `coordination` em `student.invites_queued`; `failure`;
    `cancel_reason` com `superseded`; `revoke_reason`); `terms_version` inteiro ≥ 1; `valid_hours` de 1
    a 720; contagens inteiras ≥ 0. Vermelho no esqueleto: `EventType` só ganha os 8 tipos na
    implementação.

    Disparo e resposta: os
    13 tipos de disparos, entregas e submissões, cada um com um conjunto de campos distinto dos demais; os
    valores enumerados exatos (`origin`, `rejection_reason`, `cancel_reason`, `trigger`,
    `undelivered_reason`, `failure`, `engine_error`; `valid` só `true`); `warnings` como lista dos 6
    avisos; `counts` com exatamente as 8 chaves de estado; `parts[]` por índice, com `choices` no
    formato `sha256:<64 hex>`, `ratio`/`residual` anuláveis e `score` sem teto; horários em UTC com `Z`.
    Vermelho no esqueleto: `EventType` só ganha os 13 tipos na implementação.

    Mensagens livres: `offering.settings_changed`
    `{offering_id, free_messages, contact_email_set}` (booleanos, sem o e-mail); `warnings` do
    `dispatch.validated` aceita também `no_teacher_contact`. Vermelho: `EventType` não tem o 43º tipo.


    Relatórios: `report.frozen`,
    `notice.recorded` e `offering.digest_changed`, cada um com conjunto de campos distinto dos
    demais, e uma amostra por tipo (`_SAMPLE_DATA`). Vermelho: `EventType` só ganha os 3 tipos na
    implementação.

    Expurgo: 48 tipos, com `offering.purge_prepared` e `offering.purged`
    (só contagens, o horário de vencimento e os booleanos do contato), cada um com conjunto de campos distinto
    dos demais, e uma amostra por tipo. Vermelho: `EventType` só ganha os 2 tipos na implementação."""
    from cq_server.events import EventType

    assert {e.value for e in EventType} == set(EVENT_DATA_KEYS)
    schema = _load(_response_file("events.list"))
    variants = schema["properties"]["items"]["items"]["oneOf"]
    assert len(variants) == len(EVENT_DATA_KEYS)
    types = []
    for variant in variants:
        props = variant["properties"]
        assert set(props) == {"seq", "type", "occurred_at", "actor_token_id", "data"}
        assert set(props["type"]) == {"const"}
        event_type = props["type"]["const"]
        types.append(event_type)
        data = props["data"]
        assert data.get("type") == "object" and data.get("additionalProperties") is False
        assert set(data["properties"]) == EVENT_DATA_KEYS[event_type], event_type
        assert set(data["required"]) == EVENT_DATA_KEYS[event_type], event_type
    assert sorted(types) == sorted(EVENT_DATA_KEYS)

    for event_type, data in _SAMPLE_DATA.items():
        assert _is_valid(schema, _event_page(event_type, data)), event_type
        for other_type, other_data in _SAMPLE_DATA.items():
            if other_type != event_type:
                assert not _is_valid(schema, _event_page(event_type, other_data)), (event_type, other_type)
        with_name = {**data, "name": "Nome Qualquer"}
        assert not _is_valid(schema, _event_page(event_type, with_name)), f"{event_type}: data aberto"
    assert not _is_valid(schema, _event_page("person.unknown", _SAMPLE_DATA["person.registered"]))

    # Oferecimentos e bots: os 14 tipos, com conjuntos de campos distintos, e uma amostra por tipo.
    # Vínculo do aluno: os 8 tipos de alunos.
    # Disparo e resposta: os 13 tipos de disparos, entregas e submissões.
    # Mensagens livres: `offering.settings_changed`.
    # Relatórios: `report.frozen`, `notice.recorded` e `offering.digest_changed`.
    # Expurgo: `offering.purge_prepared` e `offering.purged`.
    assert len({frozenset(keys) for keys in EVENT_DATA_KEYS.values()}) == len(EVENT_DATA_KEYS) == 48
    assert set(_SAMPLE_DATA) == set(EVENT_DATA_KEYS)
    by_type = {v["properties"]["type"]["const"]: v["properties"]["data"] for v in variants}
    for event_type, fields in (OFFERING_BOT_EVENT_DATA_VALUES | STUDENT_LINK_EVENT_DATA_VALUES).items():
        for name, values in fields.items():
            node = by_type[event_type]["properties"][name]
            accepted = set(node["enum"]) if "enum" in node else {node["const"]}
            assert accepted == values, (event_type, name, accepted)
    for event_type, change in _OFFERING_BOT_VALID_DATA + _STUDENT_LINK_VALID_DATA:
        page = _event_page(event_type, {**_SAMPLE_DATA[event_type], **change})
        assert _is_valid(schema, page), (event_type, change)
    for event_type, change in _OFFERING_BOT_INVALID_DATA + _STUDENT_LINK_INVALID_DATA:
        page = _event_page(event_type, {**_SAMPLE_DATA[event_type], **change})
        assert not _is_valid(schema, page), (event_type, change)


def test_permission_enums_match_everywhere() -> None:
    """as permissões são as mesmas no código, nos esquemas e no catálogo.

    Oferecimentos e bots: as 8 permissões (as 5 gerais e `offerings:read`,
    `offerings:write`, `bots:manage`), em ordem alfabética no catálogo e no envelope de erro, e
    `maxItems: 8` nas listas de permissões de `tokens.*` e dos eventos. Vermelho no esqueleto:
    `Permission` só ganha as 3 novas na implementação."""
    from cq_server.permissions import Permission

    assert {p.value for p in Permission} == PERMISSIONS
    catalog_schema = _load(CATALOG_SCHEMA)
    op_schema = catalog_schema["properties"]["operations"]["items"]
    assert set(op_schema["properties"]["permission"]["enum"]) == PERMISSIONS | {None}
    for op_id in ("tokens.create", "tokens.list", "tokens.revoke", "events.list"):
        schema = _load(_response_file(op_id))
        enums = [node["items"]["enum"] for path, node in _property_nodes(schema)
                 if path.endswith("permissions") and isinstance(node.get("items"), dict)]
        assert enums, f"{op_id}: permissões sem enum"
        for enum in enums:
            assert set(enum) == PERMISSIONS, op_id
        # a lista tem de 1 a 8 permissões distintas, na ordem alfabética do contrato.
        for path, node in _property_nodes(schema):
            if path.endswith("permissions") and isinstance(node.get("items"), dict):
                assert (node.get("minItems"), node.get("maxItems"), node.get("uniqueItems")) == (1, 8, True), \
                    f"{op_id}.{path}"
                assert node["items"]["enum"] == sorted(PERMISSIONS), f"{op_id}.{path}"
    assert op_schema["properties"]["permission"]["enum"] == [*sorted(PERMISSIONS), None]
    error = _load(SCHEMAS / "error.schema.json")
    assert error["properties"]["error"]["properties"]["permission"]["enum"] == sorted(PERMISSIONS)


# ---------------------------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------------------------


def test_catalog_validates_against_its_schema() -> None:
    """`catalog.json` valida contra `catalog.schema.json`, que recusa catálogos malformados."""
    schema = _load(CATALOG_SCHEMA)
    catalog = load_catalog()
    errors = [f"{list(e.path)}: {e.message}" for e in Draft202012Validator(schema).iter_errors(catalog)]
    assert errors == []

    exposed_index = next(i for i, op in enumerate(catalog["operations"]) if op["mcp"]["exposed"])
    hidden_index = next(i for i, op in enumerate(catalog["operations"]) if not op["mcp"]["exposed"])
    mutations = []
    m = copy.deepcopy(catalog)
    del m["operations"][exposed_index]["mcp"]["confirm"]
    mutations.append(("exposta sem confirm", m))
    m = copy.deepcopy(catalog)
    del m["operations"][hidden_index]["mcp"]["reason"]
    mutations.append(("não exposta sem motivo", m))
    m = copy.deepcopy(catalog)
    m["operations"][0]["extra"] = True
    mutations.append(("campo extra na operação", m))
    m = copy.deepcopy(catalog)
    m["operations"][0]["permission"] = "people:delete"
    mutations.append(("permissão desconhecida", m))
    for label, mutated in mutations:
        assert not _is_valid(schema, mutated), f"o esquema do catálogo aceita: {label}"


def test_mcp_confirm_only_on_identity_reveal() -> None:
    """`mcp.confirm` verdadeiro só nas operações de `CONFIRM_OPS` (ferramenta
    própria).

    Oferecimentos e bots: `CONFIRM_OPS` tem
    `identity.reveal` e `offerings.archive`; o arquivamento é a operação
    destrutiva; `bots.register` e `team.link` não são expostas.

    Vínculo do aluno: `CONFIRM_OPS` tem também `invites.send`, destrutiva depois de
    `offerings.archive`; as 5 operações são expostas,
    e o reenvio (`invites.resend`) não tem confirmação nem é destrutivo.

    Disparo e resposta: `CONFIRM_OPS` tem também `dispatches.confirm`, a
    terceira destrutiva, depois de `offerings.archive` e `invites.send`; as
    operações do disparo são expostas, menos `messages.list` (texto livre de aluno fora do MCP por enquanto).

    Mensagens livres: `offerings.update_settings` é exposta como
    `update_offering_settings`, sem `confirm` e não destrutiva, fora de `CONFIRM_OPS`.
    Vermelho: a operação nova não está no OpenAPI publicado.

    Relatórios: as 5 operações são expostas (sem `confirm`, pela
    regra do laço; o reenvio do resumo só vai à equipe e não é destrutivo), fora de `CONFIRM_OPS` e das
    destrutivas. Vermelho: as 5 operações não estão no OpenAPI publicado até a implementação, no molde
    das mensagens livres.

    Expurgo: `CONFIRM_OPS` tem também `offerings.purge`, a quarta destrutiva
    (`offerings.archive`, `invites.send`, `dispatches.confirm` e `offerings.purge`, nesta ordem); as 3
    operações são expostas (`run_doctor`, `prepare_purge_offering` e `purge_offering`), e só a
    confirmação do expurgo tem `confirm`. Vermelho: as 3 operações não estão no OpenAPI publicado até a
    implementação."""
    operations = load_catalog()["operations"]
    exposed = {op["id"]: op["mcp"] for op in operations if op["mcp"]["exposed"] is True}
    assert "identity.reveal" in exposed
    assert CONFIRM_OPS == {"identity.reveal", "offerings.archive", "invites.send", "dispatches.confirm",
                           "offerings.purge"}
    assert CONFIRM_OPS <= set(exposed)
    assert {op_id for op_id, mcp in exposed.items() if mcp["confirm"] is True} == CONFIRM_OPS
    for op_id, mcp in exposed.items():
        assert mcp["confirm"] is (op_id in CONFIRM_OPS), op_id
    assert [op["id"] for op in operations if op["destructive"] is True] == [
        "offerings.archive", "invites.send", "dispatches.confirm", "offerings.purge"]
    assert set(STUDENT_LINK_OPERATIONS) <= set(exposed)
    assert set(DISPATCH_OPERATIONS) - {"messages.list"} <= set(exposed)
    assert set(REPORT_OPERATIONS) <= set(exposed)  # relatórios
    # Expurgo e diagnóstico: as 3 ferramentas, com a confirmação só no expurgo.
    assert {op_id: exposed.get(op_id) for op_id in DOCTOR_PURGE_OPERATIONS} == {
        "system.doctor": {"exposed": True, "tool": "run_doctor", "confirm": False},
        "offerings.prepare_purge": {"exposed": True, "tool": "prepare_purge_offering", "confirm": False},
        "offerings.purge": {"exposed": True, "tool": "purge_offering", "confirm": True}}
    assert {"bots.register", "team.link"}.isdisjoint(exposed)
    # a das mensagens livres, exposta, sem confirmação e não destrutiva.
    assert exposed["offerings.update_settings"] == {"exposed": True, "tool": "update_offering_settings",
                                                    "confirm": False}
    assert next(op for op in operations if op["id"] == "offerings.update_settings")["destructive"] is False
    for op in operations:
        if op["mcp"]["exposed"] is not True:
            assert "confirm" not in op["mcp"], op["id"]
    # a implementação publica as operações com confirmação e as do disparo; a confirmação do disparo, a
    # terceira destrutiva, é o próprio pedido pelo caminho, sem corpo, ao contrário da
    # revelação, do arquivamento e do envio dos convites.
    published = _published_operations(_published_doc())
    assert sorted((CONFIRM_OPS | set(DISPATCH_OPERATIONS)) - set(published)) == [], (
        "operações com confirmação ou do disparo fora do OpenAPI publicado")
    # Relatórios: as 5 operações também no OpenAPI que a app publica.
    assert sorted(set(REPORT_OPERATIONS) - set(published)) == [], "operações dos relatórios fora do OpenAPI publicado"
    # Expurgo e diagnóstico: as 3 operações também no OpenAPI que a app publica.
    assert sorted(set(DOCTOR_PURGE_OPERATIONS) - set(published)) == [], "operações do CLI fora do OpenAPI publicado"
    assert {op_id for op_id in CONFIRM_OPS if "requestBody" in published[op_id]} == (
        CONFIRM_OPS - {"dispatches.confirm"})
