"""Chamadas por operação do catálogo (usadas pelas matrizes e pelo canário).

- `VALID_CALLS[op_id](client, state, headers, *, params=None, json=MISSING)`: uma chamada
  **válida** da operação, com os cabeçalhos dados (o teste escolhe o token). `params` se soma aos
  parâmetros próprios da operação; `json` substitui o corpo (útil para mandar corpo a uma rota que
  não recebe corpo).
- `CANARY_CALLS`: entradas inválidas (ou texto livre) com valor canário, por superfície de entrada.
- `DECLARED_SURFACES`: as superfícies cobertas por `CANARY_CALLS`, por operação. O canário compara
  esse conjunto com o conjunto **exigido**, derivado do OpenAPI publicado.
"""

# Oferecimentos e bots:
# - `seed_state(client, revoke_targets=40, *, bot_api=None, archive_targets=6)` cria, depois do que
#   a API base cria (intacto), com a Bot API falsa (sem `bot_api`, a da sessão, `session_fake()`): uma
#   pessoa para o link de equipe; um oferecimento em rascunho com bot (`draft_offering`: a 1ª
#   `bots.activate` o ativa, as seguintes são repetição → 200; também usado por `offerings.get` e
#   `bots.get`); um oferecimento `active` com bot (`active_offering`: a 1ª `offerings.close` o
#   encerra, as seguintes → 200; também usado por `team.link`); um rascunho com bot próprio só para
#   `bots.register` (`register_offering`, nunca ativado: nenhuma fase com worker consulta esse bot);
#   e `archive_targets` oferecimentos `closed` já preparados, `(code, confirmation_code)`. Toda
#   chamada de `offerings.prepare_archive`/`offerings.archive`, aceita ou recusada, consome um alvo.
# - `VALID_CALLS` das 10 operações novas são repetíveis e não mudam o estado de que as outras
#   dependem: `offerings.create` com código único por chamada; `bots.register` manda o token atual
#   do bot de `register_offering` e gira o token no dublê só depois de uma resposta 200; `team.link`
#   pode repetir (o link e o código de cada 201 entram em `remember_secret`).
# - Os códigos semeados não contêm identidade; `course` e `term` semeados são neutros (os textos
#   canário ficam em `SEEDED_FREE_TEXT["course"]` e `["term"]`, para o canário semear).

# Vínculo do aluno:
# - `seed_state(…, invite_targets=8)` cria, depois do que as partes anteriores criam (intacto), com nomes, RAs
#   e e-mails neutros (fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`) e a semente ansiosa, como
#   antes (no esqueleto, ela falha na primeira rota do vínculo do aluno, com 404, a falha esperada):
#   - `import_offering`: um rascunho sem bot só para `students.import` (cada chamada importa uma
#     linha nova, com RA e e-mail únicos: repetível);
#   - `resend_person`: uma pessoa cadastrada pelo `people.register` com RA e e-mail
#     (`RESEND_PERSON_ROW`) e reaproveitada na importação do `invite_offering`;
#   - `invite_offering`: um oferecimento `active` com bot e essa pessoa como aluna, usado por
#     `invites.prepare_send` (cada preparo substitui o anterior), `invites.resend` (o pseudônimo de
#     `resend_person`; cada pedido enfileira um envio novo) e `students.summary`; é distinto do
#     `active_offering` (que `offerings.close` encerra) e dos alvos;
#   - `invite_targets` oferecimentos `active`, cada um com bot próprio, um aluno novo importado e o
#     envio já preparado (`(code, confirmation_code)`): a importação e o preparo invalidam o código,
#     por isso um oferecimento por alvo. Toda chamada de `invites.send`, aceita ou recusada
#     (400/401/403), consome um alvo, inclusive as 6 `CANARY_CALLS` de `invites.send` (o `_api_phase`
#     do canário gasta 1 + 1 + 6 = 8).
#   Com o padrão, a semente registra 9 bots a mais que antes (tarefas `bot.sync_profile`) e cria 9
#   pessoas (1 pelo cadastro e 8 pela importação), 9 importações e 8 preparos; nenhum envio.
# - `VALID_CALLS` das 5 operações novas, repetíveis (200 em todas).
# - `CANARY_CALLS` das 5 operações novas, com o resultado esperado de cada uma (nenhum 409 fora de
#   `FIELDS_CODES`): `path` → 400 `ref`; `students.import`: `body_value` → 400
#   `rows[0] invalid_format`, `body_key` → 400 `body unknown_field`, `body_key_row` → 400
#   `rows[0] unknown_field`, `conflict` (RA de Zéfiro com o e-mail de Olímpio, os dois cadastrados
#   pelo canário) → 409 `list_conflict` `rows[0] duplicate`; `students.summary`: `path_ra` → 404;
#   `invites.prepare_send`: `body_value` → 400 `valid_hours invalid_format`; `invites.send`:
#   `body_value` → 400 `confirmation_code too_long`; `invites.resend`: `body_value` → 400
#   `pseudonyms[0]` e `pseudonyms[1]` `invalid_format`; `body_key` → 400 `body unknown_field`;
#   `header_auth` → 401, `header_client` e `query_key` → 400, como antes.
# - `SQL_SCAN` com a tabela `invite_sends` e as colunas novas de `enrollments`, `invites` e
#   `offerings` (cada tabela com linha no canário).

# Disparo e resposta (a API e o cliente; a semente processa só as validações, com o canal passado
# aos tratadores):
# - `TEST_REPO_SETTINGS = RepoSettings(allowed_hosts=frozenset({"repos.cq.test"}))`: a lista de hosts
#   que a fixture `app` passa ao `create_app`; os casos da lista vazia e do host fora da lista
#   montam o próprio app.
# - `SEED_REQUEST`: o `ContextVar` com o `request` do teste, guardado pela fixture `autouse`
#   `_seed_request` de `tests/conftest.py`; a semente de disparos pede a ele `settings`, `clock`,
#   `engine_fake`, `repo_fetcher` e `bots` (sem ele, a semente falha com `AssertionError`).
# - `seed_state(…, dispatch_targets=8)` cria, depois do que as partes anteriores criam (intacto), com a semente
#   ansiosa (no esqueleto, ela falha no primeiro `dispatches.prepare`, com 404, a falha esperada):
#   - `dispatch_offering`: um oferecimento `active` com bot próprio (código `apoio-disparo-<n>`), sem
#     aluno (ver a nota abaixo);
#   - pela API (`dispatches.prepare`, token inicial), `1 + dispatch_targets` disparos **agendados**
#     (abertura em T+1 dia no `FakeClock` do teste, fechamento pela janela do oferecimento) com a
#     questão neutra da semente (`SEED_REPO` no host `repos.cq.test`, `SEED_COMMIT`, `SEED_QID`, um
#     preset sem identidade do motor falso, na árvore `course` da obtenção falsa); o motor falso
#     recebe a programação exata dessa referência;
#   - a validação **na própria semente, sem `work_once`**: em laço, toma por SQL a
#     `dispatch.validate` disponível de menor `id` (`FOR UPDATE SKIP LOCKED`, dono `seed`), monta o
#     `ClaimedTask`, chama o `validate_task_handler` montado como o `run_worker` (o `HttpQuestionEngine`
#     sobre o `engine_fake`, o `repo_fetcher` e o canal com rota `TelegramChannel(…,
#     delivery=DeliverySettings())` sobre a fixture `bots`, com um `ConsoleChannel` em memória como
#     `default`) e conclui por `TaskQueue.complete(id, "seed")`, até não restar validação
#     disponível; exceção do tratador ou `complete` falso interrompem a semente com `AssertionError`
#     (só o tipo da tarefa e o nome da exceção). Controle: as tarefas de outros tipos (`id`, tipo,
#     estado, tentativas, disponibilidade) ficam iguais antes e depois do laço, e cada disparo validado
#     sai `scheduled`, sem confirmação e com prévia (`dispatches.get`);
#   - `dispatch_shown`: o 1º disparo validado, nunca confirmado nem cancelado (`dispatches.get` e as
#     `CANARY_CALLS` de consulta); `confirm_targets`: os `dispatch_targets` validados seguintes;
#     `cancel_targets`: `dispatch_targets` disparos preparados **depois** do laço, que ficam em
#     `validating` (a tarefa `dispatch.validate` deles fica `pending`: nenhum teste das partes anteriores sobe
#     worker depois da semente).
#   Toda chamada de `dispatches.confirm` (e de `dispatches.cancel`) com um `dispatch_id` real, aceita ou
#   recusada, consome um alvo: o maior consumo por teste é 6 (o `_api_phase` do canário: 1 válida + 1
#   com 403 + 4 `CANARY_CALLS`); os testes de `test_api_errors.py` gastam até 5 e os de
#   `test_tokens_api.py`, até 3. Com o padrão, a semente cria mais 1 oferecimento com bot (1 tarefa
#   `bot.sync_profile`), 17 disparos (9 `scheduled` e 8 `validating`) e as tarefas `dispatch.validate`
#   deles (as dos 9 validados, `done`; as dos 8 alvos de cancelamento, `pending`).
#   Nota [I]: o ideal seria o oferecimento com bot e aluno vinculado, mas o vínculo do aluno só
#   acontece pelo `/start` com o código de convite, processado pelo worker (recepção), que a semente não
#   roda; nada do preparo, da validação nem da confirmação depende do alvo (a prévia sai com o aviso
#   `empty_target` e o `target_count` é 0).
# - `VALID_CALLS` das 6 operações novas (200 em todas): `dispatches.prepare` (disparo imediato com a
#   questão da semente; repetível: cada chamada cria outro disparo em `validating`), `dispatches.get`
#   (`dispatch_shown`), `dispatches.list` e `messages.list` (repetíveis), `dispatches.confirm` (consome
#   um alvo de `confirm_targets`) e `dispatches.cancel` (consome um alvo de `cancel_targets`).
# - `CANARY_CALLS` das 6 operações novas, recusadas com 400 ou 401, nunca 409:
#   `path` (nome canário no `{ref}`) → 400 `ref invalid_format`; `path_dispatch` (nome canário no
#   `{dispatch_id}`) → 400 `dispatch_id invalid_format`; `query_value` (nome canário em `after`) → 400
#   `after invalid_format`; `dispatches.prepare`: `body_value` (`repo` com o nome canário, que
#   `REPO_URL` recusa pelo espaço e pela letra acentuada, no host permitido) → 400 `repo invalid_format`,
#   `body_value_secret` (`repo` com `?private_token=<REPO_PRIVATE_TOKEN>`, registrado por
#   `remember_secret` na chamada) → 400 `repo invalid_format`, `body_key` → 400 `body
#   unknown_field`; `header_auth` → 401; `header_client` e `query_key` → 400, como antes.
# - `SQL_SCAN` com as 7 tabelas novas (todas as colunas, inclusive as de texto do aluno;
#   `media_blobs.data` pela forma `escape`, que deixa o ASCII legível), as 7 colunas novas de
#   `outbox_messages` e `telegram_updates.reply_to_message_id` (cada tabela com linha no canário).

# Mensagens livres opcionais por oferecimento (a operação
# `offerings.update_settings` e o contato do professor):
# - `VALID_CALLS["offerings.update_settings"]`: `PATCH /v1/offerings/{ref}/settings` no rascunho
#   semeado (`draft_offering`), com `{"free_messages": true}`; repetível (a 1ª chamada liga as mensagens
#   livres e gera `offering.settings_changed`; as seguintes não mudam nada: 200 sem evento). Nunca define
#   contato (o e-mail do professor fica só no oferecimento da fase de disparos do canário).
# - `CANARY_CALLS`: as 7 superfícies da operação nova, todas recusadas, nunca 409: `path` (nome
#   canário no `{ref}`) → 400 `ref invalid_format`; `header_auth` → 401; `header_client`, `query_key` e
#   `body_key` → 400, como antes; `body_value` (o e-mail **válido** de `STUDENT_A`, aluno canário cadastrado
#   pela fase de alunos antes da fase da API) → 400 `[{contact_email, invalid_value}]`, sem eco e sem
#   gravar (a regra depois do 403); `body_value_invalid` (o nome canário no
#   `contact_email`) → 400 `[{contact_email, invalid_format}]`. No cadastro (`offerings.create`), mais duas:
#   `body_value_contact` (o e-mail válido de `STUDENT_A`) → `[{contact_email, invalid_value}]` e
#   `body_value_contact_invalid` (o nome canário) → `[{contact_email, invalid_format}]`.
# - `SQL_SCAN`: a tabela nova `offering_contacts` (com o `email`: com a recusa, nenhuma identidade de
#   aluno canário pode estar ali; o e-mail do professor canário fica fora dos prefixos) e a coluna nova
#   `offerings.free_messages`.

# Relatórios e acompanhamento:
# - `seed_state(…, report_targets=1)` cria, depois do que as partes anteriores criam (intacto), com a semente ansiosa e
#   nomes neutros:
#   - `report_offering`: um oferecimento `active` com bot próprio (código `apoio-relatorio-<n>`), sem aluno e
#     sem equipe (alvo vazio, sem entrega: o aviso de fechamento fica `no_recipient`);
#   - pela API (`dispatches.prepare`, token inicial), `report_targets` disparos **imediatos** da questão da
#     semente, com `closes_at` explícito `REPORT_WINDOW_MINUTES` (10) depois do `clock.now()` (ao menos os
#     `MIN_WINDOW_MINUTES` = 5 de `dispatches.py:77`), levados a `closed` com a versão `closing` congelada,
#     nesta ordem: (1) processa as `dispatch.validate` deles; (2) **confirma** cada um pela API
#     (`dispatches.confirm`, que cria o `dispatch.open` do imediato) e processa as `dispatch.open`; (3) avança
#     o relógio da semente além do `closes_at` e cria as tarefas que fora da varredura não têm produtor
#     (`dispatch.close` e, depois do fechamento, `report.freeze`) por `tasks.enqueue_in_transaction(conn,
#     <tipo>, {"dispatch_row_id": <id>}, now=…, idempotency_key=f"<tipo>:<id>:r1")`, o mesmo payload e a
#     mesma chave que a varredura usaria (`scheduler._dispatch_rule`, `_sweep_subject`), e processa cada uma;
#   - todo processamento é por SQL e **só** das tarefas `dispatch.validate`, `dispatch.open`, `dispatch.close`
#     e `report.freeze` dos disparos do `report_offering`, no molde da validação dos disparos (posse por
#     `UPDATE … FOR UPDATE SKIP LOCKED` da tarefa disponível de menor `id` daquele tipo, o tratador montado
#     como no `run_worker`, `TaskQueue(...).complete`), sem `work_once`;
#   - relógio: os tratadores e a fila da semente dos relatórios usam um `FakeClock` **próprio**, que começa no
#     `clock.now()` do teste e é o único que avança; o `FakeClock` da fixture `clock` (o da API e da
#     confirmação) **não** se move;
#   - controle na própria semente: as tarefas de outros tipos e de outros disparos iguais antes e depois;
#     cada disparo sai `scheduled` da validação, `open` da abertura e `closed` do fechamento
#     (`dispatches.get`), e a lista de versões (`reports.list_versions`) termina com exatamente a versão 1,
#     `closing`. Exceção ao montar ou ao rodar um tratador, ou `complete` falso, interrompem a semente com
#     `AssertionError` (só o tipo da tarefa e o nome da exceção): no esqueleto, o `report.freeze`
#     (`NotImplementedError`), a falha esperada até a implementação;
#   - `report_shown`: o 1º disparo fechado (`reports.*` das `VALID_CALLS` e das `CANARY_CALLS`);
#     `report_dispatches`: todos, na ordem do preparo.
#   Com o padrão, a semente cria mais 1 oferecimento com bot (1 tarefa `bot.sync_profile`, `pending`), 1
#   disparo fechado e as suas 4 tarefas (`dispatch.validate`, `dispatch.open`, `dispatch.close` e
#   `report.freeze`, todas `done`), mais o que o congelamento grava (a versão 1 e os avisos
#   `no_recipient`, sem mensagem).
# - `VALID_CALLS` das 5 operações novas (200 em todas): `reports.get` (cálculo no
#   momento) e `reports.list_versions` sobre o `report_shown`, repetíveis; `reports.resend_summary` sobre o
#   `report_shown`, repetível (cada chamada congela uma versão `resend`, com o relógio da API);
#   `offerings.get_digest` e `offerings.update_digest` no `report_offering` (o PATCH manda só
#   `{"local_time": "21:00"}`: a 1ª chamada grava a configuração, desligada, e gera
#   `offering.digest_changed`; as seguintes não mudam nada, 200 sem evento; o resumo nunca fica ligado, para
#   não agendar `digest.daily` nas fases com worker).
# - `CANARY_CALLS` das 5 operações novas, recusadas com 400 ou 401, nunca 409 nem 404 da regra (o nome
#   canário nunca chega ao domínio): `path` (nome canário no `{ref}`) → 400 `ref invalid_format`,
#   com `header_auth` → 401, `header_client` e `query_key` → 400, como nos disparos (`_dispatch_path_op_calls`);
#   `path_dispatch` (nome canário no `{dispatch_id}` de `reports.get`, `reports.list_versions` e
#   `reports.resend_summary`) → 400 `dispatch_id invalid_format`; `reports.get`: `query_value` (nome canário em
#   `version`) → 400 `version invalid_format` e `query_value_format` (nome canário em `format`) → 400 `format
#   invalid_value`; `reports.list_versions`: `query_value` (nome canário em `after`) → 400 `after
#   invalid_format`; `offerings.update_digest`: `body_value` (nome canário em `local_time`) → 400 `local_time
#   invalid_format` e `body_key` → 400 `body unknown_field`.
# - `SQL_SCAN` com as 7 tabelas novas (todas as colunas; `dispatch_reports.content`, `team_notices.data` e as
#   três colunas de `dispatch_preview_contents` em texto) e as colunas novas `outbox_messages.notice_id`,
#   `outbox_messages.text_format` e `invite_sends.batch_id`. Nenhuma coluna interna de identidade nova
#   (`INTERNAL_IDENTITY_COLUMNS` não muda). As exceções da varredura (o conteúdo da questão canário em
#   `dispatch_preview_contents`) ficam nomeadas no canário, com controle positivo.
# - a fase dos relatórios do canário reusa os alvos dos disparos (`DISPATCH_PREFIXES`), sem alvo próprio no canário.

# CLI e expurgo:
# - `seed_state(…, prepare_targets=1, purge_targets=2)` cria, depois do que as partes anteriores criam (intacto), com a
#   semente ansiosa e nomes neutros, oferecimentos **sem aluno e sem bot**, levados de rascunho a arquivado pelas
#   rotas (`offerings.prepare_archive` e `offerings.archive`, como
#   `tests/integration/test_bots_activate_api.py:304-307`), sem nenhuma tarefa, em dois conjuntos separados:
#   - `prepare_targets` (códigos `apoio-expurgo-preparo-<n>`): os alvos da `VALID_CALLS` de
#     `offerings.prepare_purge` e das chamadas canário dela; cada preparo substitui o anterior (repetível);
#   - `purge_targets` (códigos `apoio-expurgo-<n>`), `(code, confirmation_code)`, **já preparados** pela rota
#     `offerings.prepare_purge` na semente (o código do 200, no relógio da API, válido por 10 min): o 1º é o da
#     `VALID_CALLS` de `offerings.purge` (a 1ª chamada expurga um oferecimento sem pessoas; as seguintes devolvem o
#     resultado gravado, 200, sem conferir o código); o 2º é o das chamadas canário de `offerings.purge`, inclusive
#     a do `confirmation_code`, e continua arquivado, preparado e não expurgado (nenhuma chamada canário muda o
#     estado dele).
#   A preparação fica na semente, e não na `VALID_CALLS` da confirmação: o `_api_phase` congelado do canário roda
#   toda a `VALID_CALLS` com ADMIN, e as matrizes de 401 e 403 comparam o banco antes e depois. Por isso a semente
#   passa a depender da rota de preparação: no esqueleto, ela falha no primeiro `offerings.prepare_purge`, com 404
#   (a falha esperada até a implementação), em todo caso que chama `seed_state`. Com o padrão, a semente cria
#   mais 3 oferecimentos arquivados (3 `offering.state_changed`, 3 preparos de arquivamento) e 2 preparos de
#   expurgo (`offering.purge_prepared`).
# - `VALID_CALLS` das 3 operações novas (200 em todas): `system.doctor` (repetível,
#   só leitura), `offerings.prepare_purge` (no 1º dos `prepare_targets`, repetível) e `offerings.purge` (o 1º dos
#   `purge_targets`, com o código da semente). A `VALID_CALLS["events.list"]` **não leva o `person`** (ele daria
#   400 `unknown_field` nos casos da API base no esqueleto): o `person` fica nas `CANARY_CALLS` e nos testes novos.
# - `CANARY_CALLS` das operações novas e da consulta `person`, com o resultado esperado fixado por caso em
#   `DOCTOR_PURGE_CANARY_EXPECTED` (conferido com igualdade): `system.doctor` (sem `{ref}`, sem
#   consulta e sem corpo): `header_auth` → 401, `header_client` → 400 `[{X-CQ-Client, invalid_value}]` e
#   `query_key` (o nome canário numa consulta) → 400 `[{query, unknown_field}]`; `offerings.prepare_purge` (no 1º
#   dos `prepare_targets`) e `offerings.purge` (no 2º dos `purge_targets`), pelo `_dispatch_path_op_calls` dos disparos:
#   `path` (nome canário no `{ref}`) → 400 `[{ref, invalid_format}]`, `header_auth` → 401, `header_client` e
#   `query_key` → 400; `offerings.purge`: `body_value` (o nome canário como `confirmation_code`, de formato válido,
#   1 a 64 caracteres) → **409 `invalid_confirmation`**, sem eco e sem mudança de estado (a única recusa canário do
#   expurgo com 409) e `body_key` → 400 `[{body, unknown_field}]`; `events.list`: `query_value_person` (o nome canário
#   em `person`, com `types` válido) → 400 `[{person, invalid_format}]`, sem eco.
# - `SQL_SCAN` sem tabela nova, com as 5 colunas novas de `offerings`, como cada parte
#   anterior fez com as colunas novas de tabelas existentes. Nenhuma coluna interna de identidade nova.
# - `tests/helpers/canary.py` tem também as pessoas canário do expurgo (`PURGE_PREFIXES`), usadas pela
#   semente do expurgo (`tests/helpers/purge.py`) na fase do expurgo do canário; as chamadas daqui usam as da API base.

from __future__ import annotations

import contextlib
import io
import itertools
import urllib.parse
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable

import psycopg

from cq_server.config import RepoSettings
from tests.helpers.api import (ADMIN_TOKEN, TEAM_LINK_SHAPE, activate_bot, auth, create_offering,
                               create_token, register_bot, register_person)
from tests.helpers.bot_api import session_fake
from tests.helpers.canary import URSULA, ZEFIRO, fold
from tests.helpers.canary import OLIMPIO
from tests.helpers.canary import STUDENT_A  # mensagens livres: o e-mail válido de aluno canário, recusado como contato
from tests.helpers.clock import iso
from tests.helpers.clock import FakeClock  # o relógio próprio da semente de relatórios
from tests.helpers.contract import assert_matches
from tests.helpers.api import assert_matches_tolerant  # o `Offering` com `purged_at` tolerado na semente
from tests.helpers.secrets import remember_secret

_counter = itertools.count(1)

# Disparo: os hosts permitidos da fixture `app` (o domínio `.test` é reservado e nunca resolve).
TEST_REPO_SETTINGS = RepoSettings(allowed_hosts=frozenset({"repos.cq.test"}))

# Disparo: o `request` do teste, guardado pela fixture `autouse` `_seed_request` (tests/conftest.py).
SEED_REQUEST: ContextVar[Any] = ContextVar("cq_seed_request", default=None)


MISSING: Any = object()


@dataclass
class CallState:
    """Dados de apoio criados antes das chamadas (pelo token inicial)."""

    person: dict
    token_id: str
    revoke_targets: list[str] = field(default_factory=list)
    # Oferecimentos e bots (preenchidos por `seed_state`; ver o comentário do módulo)
    bot_api: Any = None
    link_person: dict | None = None
    draft_offering: dict | None = None
    draft_bot_id: int | None = None
    active_offering: dict | None = None
    active_bot_id: int | None = None
    register_offering: dict | None = None
    register_bot_id: int | None = None
    archive_targets: list[tuple[str, str]] = field(default_factory=list)
    # Vínculo do aluno (preenchidos por `seed_state`; ver o comentário do módulo)
    import_offering: dict | None = None
    invite_offering: dict | None = None
    invite_bot_id: int | None = None
    resend_person: dict | None = None
    invite_targets: list[tuple[str, str]] = field(default_factory=list)
    # Disparo e resposta (preenchidos por `seed_state`; ver o comentário do módulo)
    dispatch_offering: dict | None = None
    dispatch_bot_id: int | None = None
    dispatch_shown: str | None = None
    confirm_targets: list[str] = field(default_factory=list)
    cancel_targets: list[str] = field(default_factory=list)
    # Relatórios (preenchidos por `seed_state`; ver o comentário do módulo)
    report_offering: dict | None = None
    report_bot_id: int | None = None
    report_shown: str | None = None
    report_dispatches: list[str] = field(default_factory=list)
    # CLI e expurgo (preenchidos por `seed_state`; ver o comentário do módulo)
    prepare_targets: list[str] = field(default_factory=list)
    purge_targets: list[tuple[str, str]] = field(default_factory=list)


def seed_state(client: Any, revoke_targets: int = 40, *, bot_api: Any = None,
               archive_targets: int = 6, invite_targets: int = 8, dispatch_targets: int = 8,
               report_targets: int = 1, prepare_targets: int = 1, purge_targets: int = 2) -> CallState:
    """Cria a pessoa e os tokens de apoio **antes** das chamadas (nada é criado dentro delas)."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa de Apoio Semente", ra="1234500001")
    _, token = create_token(client, ADMIN_TOKEN, ["people:read"], label="token de apoio")
    targets = [create_token(client, ADMIN_TOKEN, ["people:read"], label="alvo de revogacao")[1]["token_id"]
               for _ in range(revoke_targets)]
    state = CallState(person=person, token_id=token["token_id"], revoke_targets=targets)
    _seed_offerings(client, state, session_fake() if bot_api is None else bot_api, archive_targets)
    _seed_students(client, state, invite_targets)
    _seed_dispatches(client, state, dispatch_targets)
    _seed_reports(client, state, report_targets)
    _seed_purges(client, state, prepare_targets, purge_targets)
    return state


def _unique_code(prefix: str) -> str:
    """Código de oferecimento único e neutro (sem identidade de pessoa nem palavra canário)."""
    return f"{prefix}-{next(_counter)}"


def _offering_with_bot(client: Any, bot_api: Any, prefix: str) -> tuple[dict, int]:
    offering = create_offering(client, ADMIN_TOKEN, code=_unique_code(prefix),
                               course="Algoritmos e Programação", term="2031s1",
                               timezone="America/Sao_Paulo")
    bot_id = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot_id))
    return offering, bot_id


def _post_ok(client: Any, path: str, op_id: str) -> dict:
    resp = client.post(path, headers=ADMIN)
    assert resp.status_code == 200, f"{op_id} falhou na semente: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _seed_offerings(client: Any, state: CallState, bot_api: Any, archive_targets: int) -> None:
    state.bot_api = bot_api
    state.link_person = register_person(client, ADMIN_TOKEN, name="Pessoa de Apoio Equipe", ra="1234500002")
    state.draft_offering, state.draft_bot_id = _offering_with_bot(client, bot_api, "apoio-rascunho")
    state.active_offering, state.active_bot_id = _offering_with_bot(client, bot_api, "apoio-ativo")
    activate_bot(client, ADMIN_TOKEN, state.active_offering["code"])
    state.register_offering, state.register_bot_id = _offering_with_bot(client, bot_api, "apoio-registro")
    for _ in range(archive_targets):
        offering, _bot = _offering_with_bot(client, bot_api, "apoio-arquivo")
        code = offering["code"]
        activate_bot(client, ADMIN_TOKEN, code)
        _post_ok(client, f"/v1/offerings/{code}/close", "offerings.close")
        prepared = _post_ok(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
        state.archive_targets.append((code, prepared["confirmation_code"]))


# a pessoa do reenvio, cadastrada pelo `people.register` e reaproveitada na importação (neutra).
RESEND_PERSON_ROW = {"name": "Pessoa de Apoio Reenvio", "ra": "1234500003",
                     "email": "pessoa.reenvio@turma.example.org"}


def _neutral_row(name: str, local: str) -> dict:
    """uma linha nova da lista, neutra (fora dos alvos canário), com RA e e-mail únicos."""
    return {"name": name, "ra": _unique_ra(), "email": f"{local}.{next(_counter)}@turma.example.org"}


def _seed_call(client: Any, method: str, path: str, op_id: str, body: Any = None) -> dict:
    """uma chamada da semente com o token inicial; afirma 200 e o esquema congelado. A mensagem de
    falha cita a operação, o método e o caminho (no esqueleto: o 404 de uma rota do vínculo do aluno, a falha esperada)."""
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    resp = client.request(method, path, headers=ADMIN, **kwargs)
    assert resp.status_code == 200, (
        f"{op_id} falhou na semente ({method} {path}): {resp.status_code} {resp.text[:300]}")
    result = resp.json()
    assert_matches(op_id, 200, result)
    return result


def _offering_ref_of(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _seed_students(client: Any, state: CallState, invite_targets: int) -> None:
    """os oferecimentos, a pessoa e os alvos das 5 operações novas (ver o comentário do módulo)."""
    bot_api = state.bot_api
    state.import_offering = create_offering(client, ADMIN_TOKEN, code=_unique_code("apoio-lista"),
                                            course="Algoritmos e Programação", term="2031s1",
                                            timezone="America/Sao_Paulo")
    state.resend_person = register_person(client, ADMIN_TOKEN, **RESEND_PERSON_ROW)
    state.invite_offering, state.invite_bot_id = _offering_with_bot(client, bot_api, "apoio-convite")
    code = state.invite_offering["code"]
    activate_bot(client, ADMIN_TOKEN, code)
    imported = _seed_call(client, "POST", f"/v1/offerings/{code}/students", "students.import",
                          {"rows": [RESEND_PERSON_ROW]})
    assert imported == {"offering": _offering_ref_of(state.invite_offering), "received": 1,
                        "people_created": 0, "people_reused": 1, "enrollments_created": 1,
                        "already_enrolled": 0}, f"a semente não reaproveitou a pessoa: {imported}"
    for _ in range(invite_targets):
        offering, _bot = _offering_with_bot(client, bot_api, "apoio-envio")
        code = offering["code"]
        activate_bot(client, ADMIN_TOKEN, code)
        imported = _seed_call(client, "POST", f"/v1/offerings/{code}/students", "students.import",
                              {"rows": [_neutral_row("Aluno de Apoio Envio", "aluno.envio")]})
        assert (imported["people_created"], imported["enrollments_created"]) == (1, 1), imported
        prepared = _seed_call(client, "POST", f"/v1/offerings/{code}/invites/prepare",
                              "invites.prepare_send", {})
        assert (prepared["offering"], prepared["eligible"], prepared["valid_hours"]) == (
            _offering_ref_of(offering), 1, 168), f"preparo da semente inesperado: {prepared}"
        state.invite_targets.append((code, prepared["confirmation_code"]))


# a questão da semente, neutra (sem identidade nem palavra canário): um repositório no host de
# `TEST_REPO_SETTINGS`, um commit SHA-1 e o QID de um preset sem identidade do motor falso, que existe
# na árvore `course` da obtenção falsa (`tests/fixtures/repos/course/questions/<qid>/`).
SEED_REPO = "https://repos.cq.test/apoio/questoes-semente.git"
SEED_COMMIT = "5eed" * 10
SEED_QID = "number"
SEED_QUESTION = {"repo": SEED_REPO, "commit": SEED_COMMIT, "qid": SEED_QID}
SEED_WORKER = "seed"
# Prazo do adaptador do motor na semente: dentro do invariante `timeout + 15 <= lease_seconds` (30 s).
_SEED_ENGINE_TIMEOUT = 10.0

# Disparo: a posse, por SQL, só da `dispatch.validate` disponível de menor `id`, com a mesma forma
# do `TaskQueue.claim` (posse do `settings`, tentativa contada).
_CLAIM_VALIDATE_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")
# O controle da semente: as tarefas dos outros tipos (as das partes anteriores) não mudam com o laço.
_OTHER_TASKS_SQL = ("SELECT id, task_type, state, attempts, available_at FROM tasks "
                    "WHERE task_type <> %s ORDER BY id")


def _current_request() -> Any:
    request = SEED_REQUEST.get()
    assert request is not None, (
        "a semente de disparos precisa do request do teste (fixture autouse _seed_request de tests/conftest.py)")
    return request


def _prepare_seed_dispatch(client: Any, code: str, opens_at: str) -> str:
    """prepara um disparo agendado da questão da semente pela API; devolve o `dispatch_id`."""
    prepared = _seed_call(client, "POST", f"/v1/offerings/{code}/dispatches", "dispatches.prepare",
                          {**SEED_QUESTION, "opens_at": opens_at})
    assert prepared["state"] == "validating", f"preparo da semente fora de validating: {prepared['state']}"
    return prepared["dispatch_id"]


def _seed_dispatches(client: Any, state: CallState, dispatch_targets: int) -> None:
    """o oferecimento, os disparos validados e os alvos das 6 operações novas (ver o comentário do
    módulo). A validação roda na própria semente, só com as tarefas `dispatch.validate`."""
    request = _current_request()
    clock = request.getfixturevalue("clock")
    state.dispatch_offering, state.dispatch_bot_id = _offering_with_bot(client, state.bot_api, "apoio-disparo")
    code = state.dispatch_offering["code"]
    activate_bot(client, ADMIN_TOKEN, code)
    opens_at = iso(clock.now() + timedelta(days=1))
    validated = [_prepare_seed_dispatch(client, code, opens_at) for _ in range(1 + dispatch_targets)]

    _validate_seeded_dispatches(request, clock, expected=len(validated))
    for dispatch_id in validated:
        body = _seed_call(client, "GET", f"/v1/offerings/{code}/dispatches/{dispatch_id}", "dispatches.get")
        rejection = (body.get("rejection") or {}).get("reason")
        assert (body["state"], body["confirmed"], body["preview"] is not None) == ("scheduled", False, True), (
            f"a semente não validou o disparo: estado {body['state']}, rejeição {rejection}")
    state.dispatch_shown, state.confirm_targets = validated[0], validated[1:]
    # Os alvos de cancelamento ficam em `validating`: preparados depois do laço, sem validação.
    state.cancel_targets = [_prepare_seed_dispatch(client, code, opens_at) for _ in range(dispatch_targets)]


def _validate_seeded_dispatches(request: Any, clock: Any, *, expected: int) -> None:
    """Disparo: processa só as `dispatch.validate` disponíveis, com o tratador montado como o
    `run_worker`, sem `work_once`; confere que as tarefas dos outros tipos não mudaram."""
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.bots import TelegramChannel
    from cq_server.config import DeliverySettings, EngineSettings
    from cq_server.dispatches import VALIDATE_TASK_TYPE, validate_task_handler
    from cq_server.tasks import ClaimedTask, TaskQueue

    settings = request.getfixturevalue("settings")
    engine_fake = request.getfixturevalue("engine_fake")
    fetcher = request.getfixturevalue("repo_fetcher")
    bots = request.getfixturevalue("bots")
    engine_fake.program((SEED_REPO, SEED_COMMIT, SEED_QID), SEED_QID)

    conninfo = settings.database_url
    lease = timedelta(seconds=settings.lease_seconds)
    # Teto do laço: obtenção, inspeção e variante por disparo, com folga.
    limit = 4 * expected + 4
    processed = 0
    with contextlib.ExitStack() as resources:
        engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                                   timeout_seconds=_SEED_ENGINE_TIMEOUT))
        resources.callback(engine.close)
        channel = TelegramChannel(conninfo, clock=clock, runtime=bots,
                                  default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
        resources.callback(channel.close)
        handler = validate_task_handler(conninfo, clock=clock, settings=settings, engine=engine,
                                        fetcher=fetcher, channel=channel)
        resources.callback(handler.close)
        queue = TaskQueue(conninfo, clock=clock, settings=settings)
        resources.callback(queue.close)
        conn = resources.enter_context(psycopg.connect(conninfo, autocommit=True))
        before = conn.execute(_OTHER_TASKS_SQL, (VALIDATE_TASK_TYPE,)).fetchall()
        while True:
            now = clock.now()
            row = conn.execute(_CLAIM_VALIDATE_SQL, {"owner": SEED_WORKER, "until": now + lease,
                                                     "type": VALIDATE_TASK_TYPE, "now": now}).fetchone()
            if row is None:
                break
            task = ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3]))
            try:
                handler(task)
            except Exception as exc:  # noqa: BLE001 - a semente só relata o tipo da tarefa e da exceção
                raise AssertionError(
                    f"a semente falhou ao processar {task.task_type}: {type(exc).__name__}") from None
            assert queue.complete(task.id, SEED_WORKER), (
                f"a semente não concluiu {task.task_type} (posse perdida)")
            processed += 1
            assert processed <= limit, f"a semente processou mais de {limit} tarefas {VALIDATE_TASK_TYPE}"
        after = conn.execute(_OTHER_TASKS_SQL, (VALIDATE_TASK_TYPE,)).fetchall()
    assert after == before, "a validação da semente mudou tarefas de outros tipos"


# Relatórios: a janela dos disparos da semente de relatórios (minutos depois do `clock.now()`; ao menos
# os `MIN_WINDOW_MINUTES` = 5 de `dispatches.py:77`) e a folga do relógio da semente além do `closes_at`.
REPORT_WINDOW_MINUTES = 10
_REPORT_CLOSE_MARGIN = timedelta(minutes=1)
# A posse, por SQL, da tarefa disponível de menor `id` de um tipo, só dos disparos dados (o `dispatch_row_id`
# do payload, em texto), com a mesma forma do `TaskQueue.claim` (posse do `settings`, tentativa contada).
_CLAIM_SUBJECT_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s AND payload->>'dispatch_row_id' = ANY(%(subjects)s) "
    "            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")
# O controle da semente dos relatórios: as tarefas fora dos 4 tipos ou de outros disparos não mudam.
_OTHER_REPORT_TASKS_SQL = (
    "SELECT id, task_type, state, attempts, available_at FROM tasks "
    "WHERE NOT (task_type = ANY(%(types)s) AND coalesce(payload->>'dispatch_row_id', '') = ANY(%(subjects)s)) "
    "ORDER BY id")


# Relatórios: o registro do bot da semente de relatórios tolera só o 503 `unavailable` da Bot API falsa
# sob carga (o `getMe` do `bots.register_bot` estoura o `telegram_timeout_seconds` de 2 s da fixture
# `bot_settings`). O `getMe` roda fora de transação e antes de qualquer escrita, então a repetição não deixa
# resto (nem tarefa `bot.sync_profile` a mais). Qualquer outra resposta, ou o 503 em todas as
# `_REPORT_REGISTER_ATTEMPTS` tentativas, interrompe a semente com a mesma mensagem do `register_bot`. As
# outras sementes (`_offering_with_bot`) não repetem o registro.
_REPORT_REGISTER_ATTEMPTS = 3


def _report_offering_with_bot(client: Any, bot_api: Any, prefix: str) -> tuple[dict, int]:
    offering = create_offering(client, ADMIN_TOKEN, code=_unique_code(prefix),
                               course="Algoritmos e Programação", term="2031s1",
                               timezone="America/Sao_Paulo")
    bot_id = bot_api.add_bot()
    bot_token = bot_api.token_of(bot_id)
    remember_secret(bot_token)
    for attempt in range(1, _REPORT_REGISTER_ATTEMPTS + 1):
        resp = client.put(f"/v1/offerings/{offering['code']}/bot", json={"token": bot_token}, headers=ADMIN)
        transient = (resp.status_code == 503
                     and ((resp.json() or {}).get("error") or {}).get("code") == "unavailable")
        if not (transient and attempt < _REPORT_REGISTER_ATTEMPTS):
            break
    assert resp.status_code == 200, f"registro do bot falhou: {resp.status_code} {resp.text[:300]}"
    assert_matches("bots.register", 200, resp.json())
    return offering, bot_id


def _seed_reports(client: Any, state: CallState, report_targets: int) -> None:
    """Relatórios: o oferecimento e os disparos fechados com a versão `closing` (ver o comentário do módulo)."""
    request = _current_request()
    clock = request.getfixturevalue("clock")
    state.report_offering, state.report_bot_id = _report_offering_with_bot(client, state.bot_api, "apoio-relatorio")
    code = state.report_offering["code"]
    activate_bot(client, ADMIN_TOKEN, code)
    closes_at = iso(clock.now() + timedelta(minutes=REPORT_WINDOW_MINUTES))
    prepared = []
    for _ in range(report_targets):
        body = _seed_call(client, "POST", f"/v1/offerings/{code}/dispatches", "dispatches.prepare",
                          {**SEED_QUESTION, "closes_at": closes_at})
        assert body["state"] == "validating", f"preparo da semente fora de validating: {body['state']}"
        prepared.append(body["dispatch_id"])
    if prepared:
        _close_seeded_reports(client, request, clock, code, prepared)
    state.report_dispatches = list(prepared)
    state.report_shown = prepared[0] if prepared else None


def _seed_dispatch_state(client: Any, code: str, dispatch_id: str) -> dict:
    return _seed_call(client, "GET", f"/v1/offerings/{code}/dispatches/{dispatch_id}", "dispatches.get")


def _close_seeded_reports(client: Any, request: Any, clock: Any, code: str, dispatch_ids: list[str]) -> None:
    """Relatórios: valida, confirma, abre, fecha e congela os disparos da semente, só com as tarefas deles e
    com o relógio próprio da semente; confere que as tarefas de outros tipos e de outros disparos não mudaram
    e que cada disparo terminou com exatamente a versão 1, `closing`."""
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.bots import TelegramChannel
    from cq_server.config import DeliverySettings, EngineSettings
    from cq_server.dispatches import (CLOSE_TASK_TYPE, OPEN_TASK_TYPE, VALIDATE_TASK_TYPE, close_task_handler,
                                      open_task_handler, validate_task_handler)
    from cq_server.reports import FREEZE_TASK_TYPE, freeze_task_handler
    from cq_server.tasks import ClaimedTask, TaskQueue, enqueue_in_transaction

    settings = request.getfixturevalue("settings")
    engine_fake = request.getfixturevalue("engine_fake")
    fetcher = request.getfixturevalue("repo_fetcher")
    bots = request.getfixturevalue("bots")
    engine_fake.program((SEED_REPO, SEED_COMMIT, SEED_QID), SEED_QID)

    conninfo = settings.database_url
    lease = timedelta(seconds=settings.lease_seconds)
    own = FakeClock(clock.now())  # o relógio próprio da semente dos relatórios; o da fixture não se move
    types = [VALIDATE_TASK_TYPE, OPEN_TASK_TYPE, CLOSE_TASK_TYPE, FREEZE_TASK_TYPE]
    count = len(dispatch_ids)
    with contextlib.ExitStack() as resources:
        conn = resources.enter_context(psycopg.connect(conninfo, autocommit=True))
        rows = {}
        for dispatch_id in dispatch_ids:
            found = conn.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s", (dispatch_id,)).fetchone()
            assert found is not None, "a semente não achou o disparo preparado"
            rows[dispatch_id] = int(found[0])
        subjects = [str(row_id) for row_id in rows.values()]
        control = {"types": types, "subjects": subjects}
        before = conn.execute(_OTHER_REPORT_TASKS_SQL, control).fetchall()

        engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url,
                                                   timeout_seconds=_SEED_ENGINE_TIMEOUT))
        resources.callback(engine.close)
        channel = TelegramChannel(conninfo, clock=own, runtime=bots,
                                  default=ConsoleChannel(io.StringIO()), delivery=DeliverySettings())
        resources.callback(channel.close)
        queue = TaskQueue(conninfo, clock=own, settings=settings)
        resources.callback(queue.close)

        def build(task_type: str, factory: Callable[[], Any]) -> Any:
            try:
                handler = factory()
            except Exception as exc:  # noqa: BLE001 - a semente só relata o tipo da tarefa e da exceção
                raise AssertionError(
                    f"a semente falhou ao montar o tratador {task_type}: {type(exc).__name__}") from None
            resources.callback(handler.close)
            return handler

        def drain(task_type: str, handler: Any, limit: int) -> int:
            processed = 0
            while True:
                now = own.now()
                row = conn.execute(_CLAIM_SUBJECT_SQL, {"owner": SEED_WORKER, "until": now + lease,
                                                        "type": task_type, "now": now,
                                                        "subjects": subjects}).fetchone()
                if row is None:
                    return processed
                task = ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3]))
                try:
                    handler(task)
                except Exception as exc:  # noqa: BLE001 - a semente só relata o tipo da tarefa e da exceção
                    raise AssertionError(
                        f"a semente falhou ao processar {task.task_type}: {type(exc).__name__}") from None
                assert queue.complete(task.id, SEED_WORKER), (
                    f"a semente não concluiu {task.task_type} (posse perdida)")
                processed += 1
                assert processed <= limit, f"a semente processou mais de {limit} tarefas {task_type}"

        def enqueue_all(task_type: str) -> None:
            with conn.transaction():
                for row_id in rows.values():
                    enqueue_in_transaction(conn, task_type, {"dispatch_row_id": row_id}, now=own.now(),
                                           idempotency_key=f"{task_type}:{row_id}:r1")

        def expect_state(expected: str, *, confirmed: bool) -> None:
            for dispatch_id in dispatch_ids:
                body = _seed_dispatch_state(client, code, dispatch_id)
                rejection = (body.get("rejection") or {}).get("reason")
                assert (body["state"], body["confirmed"]) == (expected, confirmed), (
                    f"a semente de relatórios esperava {expected}: estado {body['state']}, rejeição {rejection}")

        # (1) validação
        drain(VALIDATE_TASK_TYPE, build(VALIDATE_TASK_TYPE, lambda: validate_task_handler(
            conninfo, clock=own, settings=settings, engine=engine, fetcher=fetcher, channel=channel)),
            limit=4 * count + 4)
        expect_state("scheduled", confirmed=False)
        # (2) confirmação pela API (cria o `dispatch.open` do imediato) e abertura
        for dispatch_id in dispatch_ids:
            confirmed = _seed_call(client, "POST", f"/v1/offerings/{code}/dispatches/{dispatch_id}/confirm",
                                   "dispatches.confirm")
            assert (confirmed["state"], confirmed["confirmed"]) == ("scheduled", True), (
                f"a confirmação da semente não valeu: estado {confirmed['state']}")
        drain(OPEN_TASK_TYPE, build(OPEN_TASK_TYPE, lambda: open_task_handler(
            conninfo, clock=own, settings=settings)), limit=count)
        expect_state("open", confirmed=True)
        # (3) o relógio da semente além do `closes_at`; fechamento e congelamento
        last_close = max(_seed_dispatch_closes_at(client, code, dispatch_id) for dispatch_id in dispatch_ids)
        own.set(last_close + _REPORT_CLOSE_MARGIN)
        enqueue_all(CLOSE_TASK_TYPE)
        drain(CLOSE_TASK_TYPE, build(CLOSE_TASK_TYPE, lambda: close_task_handler(
            conninfo, clock=own, settings=settings, channel=channel)), limit=count)
        expect_state("closed", confirmed=True)
        enqueue_all(FREEZE_TASK_TYPE)
        drain(FREEZE_TASK_TYPE, build(FREEZE_TASK_TYPE, lambda: freeze_task_handler(
            conninfo, clock=own, settings=settings, channel=channel)), limit=count)
        after = conn.execute(_OTHER_REPORT_TASKS_SQL, control).fetchall()
    assert after == before, "a semente de relatórios mudou tarefas de outros tipos ou de outros disparos"
    for dispatch_id in dispatch_ids:
        versions = _seed_call(client, "GET", f"/v1/offerings/{code}/dispatches/{dispatch_id}/report/versions",
                              "reports.list_versions")
        frozen = [(item["version"], item["trigger"]) for item in versions["items"]]
        assert frozen == [(1, "closing")], f"a semente não congelou só a versão 1 (closing): {frozen}"


def _seed_dispatch_closes_at(client: Any, code: str, dispatch_id: str) -> Any:
    """O `closes_at` efetivo do disparo, como `datetime` com fuso (lido pela API)."""
    from datetime import datetime

    value = _seed_dispatch_state(client, code, dispatch_id)["closes_at"]
    assert isinstance(value, str), "disparo aberto sem closes_at"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# Expurgo: os alvos do expurgo, sem aluno e sem bot (ver o comentário do módulo).
def _seed_post_tolerant(client: Any, path: str, op_id: str, body: Any = None) -> dict:
    """uma chamada de sucesso da semente (200), com o esquema congelado tolerando só a ausência das chaves do
    `Offering` de `_FREE_MESSAGE_OFFERING_KEYS` (`assert_matches_tolerant`): a parte do expurgo não falha por isso antes da
    rota do expurgo."""
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    resp = client.post(path, headers=ADMIN, **kwargs)
    assert resp.status_code == 200, (
        f"{op_id} falhou na semente (POST {path}): {resp.status_code} {resp.text[:300]}")
    result = resp.json()
    assert_matches_tolerant(op_id, 200, result)
    return result


def _archived_without_people(client: Any, prefix: str) -> str:
    """um oferecimento neutro, sem aluno e sem bot, levado de rascunho a arquivado (sem nenhuma tarefa)."""
    offering = create_offering(client, ADMIN_TOKEN, strict_schema=False, code=_unique_code(prefix),
                               course="Algoritmos e Programação", term="2031s1", timezone="America/Sao_Paulo")
    code = offering["code"]
    prepared = _seed_post_tolerant(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
    archived = _seed_post_tolerant(client, f"/v1/offerings/{code}/archive", "offerings.archive",
                                   {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived", f"a semente não arquivou o alvo do expurgo: {archived['state']}"
    return code


def _seed_purges(client: Any, state: CallState, prepare_targets: int, purge_targets: int) -> None:
    """Expurgo: os alvos da preparação e os alvos já preparados da confirmação (ver o comentário do módulo).
    No esqueleto, o primeiro `offerings.prepare_purge` dá 404 (a falha esperada até a implementação)."""
    for _ in range(prepare_targets):
        state.prepare_targets.append(_archived_without_people(client, "apoio-expurgo-preparo"))
    for _ in range(purge_targets):
        code = _archived_without_people(client, "apoio-expurgo")
        prepared = _seed_call(client, "POST", f"/v1/offerings/{code}/purge/prepare", "offerings.prepare_purge")
        assert prepared["offering"]["code"] == code, f"preparo do expurgo da semente inesperado: {prepared['offering']}"
        state.purge_targets.append((code, prepared["confirmation_code"]))


def _unique_ra() -> str:
    return f"77{next(_counter):08d}"


def _next_target(state: CallState) -> str:
    assert state.revoke_targets, "acabaram os alvos de revogação pré-criados em seed_state"
    return state.revoke_targets.pop(0)


def _next_archive(state: CallState) -> tuple[str, str]:
    assert state.archive_targets, "acabaram os alvos de arquivamento pré-criados em seed_state"
    return state.archive_targets.pop(0)


def _merge(base: dict | None, extra: dict | None) -> dict | None:
    if base is None and extra is None:
        return None
    return {**(base or {}), **(extra or {})}


def _body(default: Any, override: Any, content: Any = MISSING) -> dict:
    if content is not MISSING:
        return {"content": content}
    if override is not MISSING:
        return {"json": override}
    return {} if default is None else {"json": default}


def _tokens_create(c: Any, s: CallState, h: dict, *, params: dict | None = None, json: Any = MISSING) -> Any:
    resp = c.post("/v1/tokens", params=params, headers=h,
                  **_body({"label": "rotulo valido", "permissions": ["people:read"]}, json))
    if resp.status_code == 201:
        remember_secret(resp.json()["secret"])
    return resp


def _get(c: Any, url: str, params: dict | None, headers: dict, json: Any) -> Any:
    # `TestClient.get` não aceita corpo; `request("GET", …)` aceita (corpo em rota sem corpo).
    return c.request("GET", url, params=params, headers=headers, **_body(None, json))


def _offerings_create(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                      json: Any = MISSING) -> Any:
    return c.post("/v1/offerings", params=params, headers=h, **_body(
        {"code": _unique_code("oferta-valida"), "course": "Disciplina Valida Chamada", "term": "2031s1",
         "timezone": "America/Sao_Paulo"}, json))


def _offerings_prepare_archive(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                               json: Any = MISSING) -> Any:
    code, _ = _next_archive(s)
    return c.post(f"/v1/offerings/{code}/archive/prepare", params=params, headers=h, **_body(None, json))


def _offerings_archive(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                       json: Any = MISSING) -> Any:
    code, confirmation = _next_archive(s)
    return c.post(f"/v1/offerings/{code}/archive", params=params, headers=h,
                  **_body({"confirmation_code": confirmation}, json))


def _bots_register(c: Any, s: CallState, h: dict, *, params: dict | None = None, json: Any = MISSING) -> Any:
    assert s.register_offering is not None and s.register_bot_id is not None, "seed_state sem bots"
    resp = c.put(f"/v1/offerings/{s.register_offering['code']}/bot", params=params, headers=h,
                 **_body({"token": s.bot_api.token_of(s.register_bot_id)}, json))
    if resp.status_code == 200:
        s.bot_api.rotate_token(s.register_bot_id)
    return resp


def _team_link(c: Any, s: CallState, h: dict, *, params: dict | None = None, json: Any = MISSING) -> Any:
    assert s.active_offering is not None and s.link_person is not None, "seed_state sem bots"
    resp = c.post(f"/v1/offerings/{s.active_offering['code']}/team-links", params=params, headers=h,
                  **_body({"person": s.link_person["alias"]}, json))
    if resp.status_code == 201:
        link = resp.json().get("link")
        if isinstance(link, str):
            remember_secret(link)
            match = TEAM_LINK_SHAPE.fullmatch(link)
            if match is not None:
                remember_secret(match.group(2))
    return resp


def _draft(s: CallState) -> str:
    assert s.draft_offering is not None, "seed_state sem bots"
    return s.draft_offering["code"]


def _active(s: CallState) -> str:
    assert s.active_offering is not None, "seed_state sem bots"
    return s.active_offering["code"]


# referências e alvos das 5 operações novas (ver o comentário do módulo).
def _import_ref(s: CallState) -> str:
    assert s.import_offering is not None, "seed_state sem alunos"
    return s.import_offering["code"]


def _invite_ref(s: CallState) -> str:
    assert s.invite_offering is not None, "seed_state sem alunos"
    return s.invite_offering["code"]


def _resend_alias(s: CallState) -> str:
    assert s.resend_person is not None, "seed_state sem alunos"
    return s.resend_person["alias"]


def _next_invite_target(s: CallState) -> tuple[str, str]:
    assert s.invite_targets, "acabaram os alvos de envio pré-criados em seed_state"
    return s.invite_targets.pop(0)


def _students_import(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                     json: Any = MISSING) -> Any:
    return c.post(f"/v1/offerings/{_import_ref(s)}/students", params=params, headers=h, **_body(
        {"rows": [_neutral_row("Aluno Valido Chamada", "aluno.chamada")]}, json))


def _invites_send(c: Any, s: CallState, h: dict, *, params: dict | None = None, json: Any = MISSING) -> Any:
    code, confirmation = _next_invite_target(s)
    return c.post(f"/v1/offerings/{code}/invites", params=params, headers=h,
                  **_body({"confirmation_code": confirmation}, json))


# referências e alvos das 6 operações novas (ver o comentário do módulo).
def _dispatch_ref(s: CallState) -> str:
    assert s.dispatch_offering is not None, "seed_state sem disparos"
    return s.dispatch_offering["code"]


def _shown_dispatch(s: CallState) -> str:
    assert s.dispatch_shown is not None, "seed_state sem disparos"
    return s.dispatch_shown


def _next_confirm_target(s: CallState) -> str:
    assert s.confirm_targets, "acabaram os alvos de confirmação pré-criados em seed_state"
    return s.confirm_targets.pop(0)


def _next_cancel_target(s: CallState) -> str:
    assert s.cancel_targets, "acabaram os alvos de cancelamento pré-criados em seed_state"
    return s.cancel_targets.pop(0)


# referências das 5 operações novas (ver o comentário do módulo).
def _report_ref(s: CallState) -> str:
    assert s.report_offering is not None, "seed_state sem relatórios"
    return s.report_offering["code"]


def _report_dispatch(s: CallState) -> str:
    assert s.report_shown is not None, "seed_state sem disparo fechado (report_targets=0)"
    return s.report_shown


def _report_url(s: CallState, suffix: str = "") -> str:
    return f"/v1/offerings/{_report_ref(s)}/dispatches/{_report_dispatch(s)}/report{suffix}"


# o corpo válido de `offerings.update_digest` nas chamadas de apoio: só o horário, com o resumo desligado
# (nunca agenda `digest.daily`; ver o comentário do módulo).
REPORT_DIGEST_BODY = {"local_time": "21:00"}


def _dispatches_confirm(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                        json: Any = MISSING) -> Any:
    return c.post(f"/v1/offerings/{_dispatch_ref(s)}/dispatches/{_next_confirm_target(s)}/confirm",
                  params=params, headers=h, **_body(None, json))


def _dispatches_cancel(c: Any, s: CallState, h: dict, *, params: dict | None = None,
                       json: Any = MISSING) -> Any:
    return c.post(f"/v1/offerings/{_dispatch_ref(s)}/dispatches/{_next_cancel_target(s)}/cancel",
                  params=params, headers=h, **_body(None, json))


VALID_CALLS: dict[str, Callable[..., Any]] = {
    "system.health": lambda c, s, h, *, params=None, json=MISSING: _get(c, "/health", params, h, json),
    "people.register": lambda c, s, h, *, params=None, json=MISSING: c.post(
        "/v1/people", params=params, headers=h,
        **_body({"name": "Pessoa Valida Chamada", "ra": _unique_ra()}, json)),
    "people.list": lambda c, s, h, *, params=None, json=MISSING: _get(c, "/v1/people", params, h, json),
    "people.get": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/people/{s.person['alias']}", params, h, json),
    "identity.reveal": lambda c, s, h, *, params=None, json=MISSING: c.post(
        "/v1/identity/reveal", params=params, headers=h,
        **_body({"pseudonyms": [s.person["alias"]], "reason": "conferencia de apoio"}, json)),
    "tokens.create": _tokens_create,
    "tokens.list": lambda c, s, h, *, params=None, json=MISSING: _get(c, "/v1/tokens", params, h, json),
    "tokens.revoke": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/tokens/{_next_target(s)}/revoke", params=params, headers=h, **_body(None, json)),
    "events.list": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, "/v1/events", _merge({"types": "person.registered"}, params), h, json),
    # Oferecimentos e bots
    "offerings.create": _offerings_create,
    "offerings.list": lambda c, s, h, *, params=None, json=MISSING: _get(c, "/v1/offerings", params, h, json),
    "offerings.get": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_draft(s)}", params, h, json),
    "offerings.close": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_active(s)}/close", params=params, headers=h, **_body(None, json)),
    "offerings.prepare_archive": _offerings_prepare_archive,
    "offerings.archive": _offerings_archive,
    "bots.register": _bots_register,
    "bots.activate": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_draft(s)}/bot/activate", params=params, headers=h, **_body(None, json)),
    "bots.get": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_draft(s)}/bot", params, h, json),
    "team.link": _team_link,
    # Vínculo do aluno
    "students.import": _students_import,
    "students.summary": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_invite_ref(s)}/students/summary", params, h, json),
    "invites.prepare_send": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_invite_ref(s)}/invites/prepare", params=params, headers=h, **_body({}, json)),
    "invites.send": _invites_send,
    "invites.resend": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_invite_ref(s)}/invites/resend", params=params, headers=h,
        **_body({"pseudonyms": [_resend_alias(s)]}, json)),
    # Disparo e resposta
    "dispatches.prepare": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_dispatch_ref(s)}/dispatches", params=params, headers=h,
        **_body(dict(SEED_QUESTION), json)),
    "dispatches.get": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_dispatch_ref(s)}/dispatches/{_shown_dispatch(s)}", params, h, json),
    "dispatches.list": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_dispatch_ref(s)}/dispatches", params, h, json),
    "dispatches.confirm": _dispatches_confirm,
    "dispatches.cancel": _dispatches_cancel,
    "messages.list": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_dispatch_ref(s)}/messages", params, h, json),
    # Mensagens livres: no rascunho semeado, só o booleano (nunca o contato)
    "offerings.update_settings": lambda c, s, h, *, params=None, json=MISSING: c.request(
        "PATCH", f"/v1/offerings/{_draft(s)}/settings", params=params, headers=h,
        **_body({"free_messages": True}, json)),
    # Relatórios: sobre o disparo fechado e o oferecimento da semente de relatórios
    "reports.get": lambda c, s, h, *, params=None, json=MISSING: _get(c, _report_url(s), params, h, json),
    "reports.list_versions": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, _report_url(s, "/versions"), params, h, json),
    "reports.resend_summary": lambda c, s, h, *, params=None, json=MISSING: c.post(
        _report_url(s, "/resend"), params=params, headers=h, **_body(None, json)),
    "offerings.get_digest": lambda c, s, h, *, params=None, json=MISSING: _get(
        c, f"/v1/offerings/{_report_ref(s)}/digest", params, h, json),
    "offerings.update_digest": lambda c, s, h, *, params=None, json=MISSING: c.request(
        "PATCH", f"/v1/offerings/{_report_ref(s)}/digest", params=params, headers=h,
        **_body(dict(REPORT_DIGEST_BODY), json)),
}


# CLI e expurgo: referências das 3 operações novas (ver o comentário do módulo).
def _purge_prepare_ref(s: CallState) -> str:
    assert s.prepare_targets, "seed_state sem alvos de preparação do expurgo (prepare_targets=0)"
    return s.prepare_targets[0]


def _purge_target(s: CallState, index: int) -> tuple[str, str]:
    assert len(s.purge_targets) > index, "seed_state sem alvos preparados do expurgo suficientes (purge_targets)"
    return s.purge_targets[index]


def _offerings_purge(c: Any, s: CallState, h: dict, *, params: dict | None = None, json: Any = MISSING) -> Any:
    code, confirmation = _purge_target(s, 0)
    return c.post(f"/v1/offerings/{code}/purge", params=params, headers=h,
                  **_body({"confirmation_code": confirmation}, json))


VALID_CALLS.update({
    # CLI e expurgo: `system.doctor` só lê; `offerings.prepare_purge` no 1º alvo de preparação (cada chamada substitui
    # o código anterior dele); `offerings.purge` no 1º alvo preparado, com o código da semente (a 1ª chamada
    # expurga; as seguintes devolvem o resultado gravado).
    "system.doctor": lambda c, s, h, *, params=None, json=MISSING: _get(c, "/v1/system/doctor", params, h, json),
    "offerings.prepare_purge": lambda c, s, h, *, params=None, json=MISSING: c.post(
        f"/v1/offerings/{_purge_prepare_ref(s)}/purge/prepare", params=params, headers=h, **_body(None, json)),
    "offerings.purge": _offerings_purge,
})

# Valor canário em ASCII para cabeçalhos HTTP (o detector acha a forma sem acento).
CANARY_ASCII = fold(ZEFIRO.name).title()
CANARY_NAME_PATH = urllib.parse.quote(ZEFIRO.name)
ADMIN = auth(ADMIN_TOKEN)

# token de bot de formato local válido que não existe na Bot API falsa (as CANARY_CALLS que o
# usam são recusadas antes de qualquer chamada ao Telegram); entra em `remember_secret` no uso.
DUMMY_BOT_TOKEN = "1234567:" + "Aa0_-" * 7
# Código de confirmação de formato válido (22 caracteres), que não confere com nenhum preparado.
_CONFIRMATION = "Aa0_-" * 4 + "Zz"
# Marca de corpo de `team.link` com a pessoa de apoio (resolvida na chamada).
_LINK_PERSON = object()


def _dummy_bot_token() -> str:
    remember_secret(DUMMY_BOT_TOKEN)
    return DUMMY_BOT_TOKEN


def _offering_body(**fields: Any) -> dict:
    """Corpo válido de `offerings.create` com código único e neutro; `fields` troca campos."""
    return {"code": _unique_code("oferta-sonda"), "course": "Disciplina Sonda", "term": "2031s1",
            "timezone": "America/Sao_Paulo", **fields}


@dataclass(frozen=True)
class CanaryCall:
    op_id: str
    surface: str
    call: Callable[[Any, CallState], Any]


def _path_op_calls(op_id: str, method: str, suffix: str, body: Any = None) -> tuple[CanaryCall, ...]:
    """as superfícies globais de `/v1` (`header_auth`, `header_client`, `query_key`) de uma
    operação com `{ref}` no caminho, sobre o rascunho semeado (`offerings.close` e `team.link`:
    o oferecimento ativo), sempre recusadas antes de qualquer regra; `body` é o corpo válido."""
    ref_of = _active if op_id in ("offerings.close", "team.link") else _draft

    def call(headers_of: Callable[[], dict], params: dict | None = None) -> Callable[[Any, CallState], Any]:
        def run(c: Any, s: CallState) -> Any:
            kwargs: dict[str, Any] = {"headers": headers_of()}
            if params is not None:
                kwargs["params"] = params
            if body is _LINK_PERSON:
                kwargs["json"] = {"person": s.link_person["alias"]}
            elif body is not None:
                if body.get("token") == DUMMY_BOT_TOKEN:
                    remember_secret(DUMMY_BOT_TOKEN)
                kwargs["json"] = body
            return c.request(method, f"/v1/offerings/{ref_of(s)}{suffix}", **kwargs)
        return run

    return (
        CanaryCall(op_id, "header_auth", call(lambda: auth(ZEFIRO.ra))),
        CanaryCall(op_id, "header_client", call(lambda: {**ADMIN, "X-CQ-Client": CANARY_ASCII})),
        CanaryCall(op_id, "query_key", call(lambda: ADMIN, {ZEFIRO.name: "1"})),
    )


# o `{ref}` e o corpo válido de cada operação nova, lidos na hora da chamada (`None`: sem corpo).
def _import_target(s: CallState) -> tuple[str, Any]:
    return _import_ref(s), {"rows": [_neutral_row("Aluno Sonda Lista", "aluno.sonda")]}


def _summary_target(s: CallState) -> tuple[str, Any]:
    return _invite_ref(s), None


def _prepare_target(s: CallState) -> tuple[str, Any]:
    return _invite_ref(s), {}


def _send_target(s: CallState) -> tuple[str, Any]:
    """Consome um alvo de `invites.send` (o oferecimento e o código do preparo da semente)."""
    code, confirmation = _next_invite_target(s)
    return code, {"confirmation_code": confirmation}


def _resend_target(s: CallState) -> tuple[str, Any]:
    return _invite_ref(s), {"pseudonyms": [_resend_alias(s)]}


def _student_link_path_op_calls(op_id: str, method: str, suffix: str,
                      target: Callable[[CallState], tuple[str, Any]]) -> tuple[CanaryCall, ...]:
    """as superfícies globais de `/v1` (`header_auth`, `header_client`, `query_key`) de uma
    operação nova, sempre recusadas antes de qualquer regra; `target(s)` dá o `{ref}` e o corpo
    válido na hora da chamada (em `invites.send`, consome um alvo, com o código de verdade)."""

    def call(headers_of: Callable[[], dict], params: dict | None = None) -> Callable[[Any, CallState], Any]:
        def run(c: Any, s: CallState) -> Any:
            ref, body = target(s)
            kwargs: dict[str, Any] = {"headers": headers_of()}
            if params is not None:
                kwargs["params"] = params
            if body is not None:
                kwargs["json"] = body
            return c.request(method, f"/v1/offerings/{ref}{suffix}", **kwargs)
        return run

    return (
        CanaryCall(op_id, "header_auth", call(lambda: auth(ZEFIRO.ra))),
        CanaryCall(op_id, "header_client", call(lambda: {**ADMIN, "X-CQ-Client": CANARY_ASCII})),
        CanaryCall(op_id, "query_key", call(lambda: ADMIN, {ZEFIRO.name: "1"})),
    )


def _student_link_body_call(method: str, suffix: str, target: Callable[[CallState], tuple[str, Any]],
                  extra: dict) -> Callable[[Any, CallState], Any]:
    """a chamada com o corpo válido de `target` mais as chaves de `extra` (ex.: a chave canário)."""
    def run(c: Any, s: CallState) -> Any:
        ref, body = target(s)
        return c.request(method, f"/v1/offerings/{ref}{suffix}", json={**body, **extra}, headers=ADMIN)
    return run


def _student_link_canary_ref_call(method: str, suffix: str,
                        target: Callable[[CallState], tuple[str, Any]]) -> Callable[[Any, CallState], Any]:
    """o corpo válido de `target` com o nome canário no lugar do `{ref}` (400 sem eco)."""
    def run(c: Any, s: CallState) -> Any:
        _, body = target(s)
        kwargs: dict[str, Any] = {} if body is None else {"json": body}
        return c.request(method, f"/v1/offerings/{CANARY_NAME_PATH}{suffix}", headers=ADMIN, **kwargs)
    return run


# Disparo: um token de acesso privado de formato plausível na consulta do `repo`;
# entra em `remember_secret` no uso e nunca pode aparecer numa saída.
REPO_PRIVATE_TOKEN = "glpat-" + "Qx7_Rk2-" * 3


def _repo_private_token() -> str:
    remember_secret(REPO_PRIVATE_TOKEN)
    return REPO_PRIVATE_TOKEN


# o caminho sob `/v1/offerings/{ref}` e o corpo válido de cada operação nova, lidos na hora da
# chamada (`None`: sem corpo); em `dispatches.confirm` e `dispatches.cancel`, consome um alvo.
def _dispatch_prepare_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), "/dispatches", dict(SEED_QUESTION)


def _dispatch_get_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), f"/dispatches/{_shown_dispatch(s)}", None


def _dispatch_list_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), "/dispatches", None


def _dispatch_confirm_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), f"/dispatches/{_next_confirm_target(s)}/confirm", None


def _dispatch_cancel_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), f"/dispatches/{_next_cancel_target(s)}/cancel", None


def _dispatch_messages_target(s: CallState) -> tuple[str, str, Any]:
    return _dispatch_ref(s), "/messages", None


# Mensagens livres: o rascunho semeado e o corpo válido de `offerings.update_settings` (só o booleano).
def _dispatch_settings_target(s: CallState) -> tuple[str, str, Any]:
    return _draft(s), "/settings", {"free_messages": True}


# Relatórios: o caminho sob `/v1/offerings/{ref}` e o corpo válido de cada operação nova (no formato dos
# alvos dos disparos, para o `_dispatch_call` e o `_dispatch_path_op_calls`), lidos na hora da chamada.
def _report_get_target(suffix: str) -> Callable[[CallState], tuple[str, str, Any]]:
    def target(s: CallState) -> tuple[str, str, Any]:
        return _report_ref(s), f"/dispatches/{_report_dispatch(s)}/report{suffix}", None
    return target


def _digest_get_target(s: CallState) -> tuple[str, str, Any]:
    return _report_ref(s), "/digest", None


def _digest_set_target(s: CallState) -> tuple[str, str, Any]:
    return _report_ref(s), "/digest", dict(REPORT_DIGEST_BODY)


def _report_canary_id_call(method: str, suffix: str = "") -> Callable[[Any, CallState], Any]:
    """o nome canário no `{dispatch_id}` das rotas de relatório (400 `dispatch_id invalid_format`, sem
    eco); `suffix` é o que vem depois de `/report` (`""`, `/versions`, `/resend`)."""
    def run(c: Any, s: CallState) -> Any:
        return c.request(method, f"/v1/offerings/{_report_ref(s)}/dispatches/{CANARY_NAME_PATH}/report{suffix}",
                         headers=ADMIN)
    return run


def _dispatch_call(method: str, target: Callable[[CallState], tuple[str, str, Any]], *,
             headers_of: Callable[[], dict] = lambda: ADMIN, params: dict | None = None,
             canary_ref: bool = False, body: Callable[[Any], Any] | None = None) -> Callable[[Any, CallState], Any]:
    """a chamada de `target(s)` com os cabeçalhos de `headers_of()` e a consulta `params`;
    `canary_ref` troca o `{ref}` pelo nome canário (400 sem eco); `body(corpo_válido)` troca o corpo."""
    def run(c: Any, s: CallState) -> Any:
        ref, suffix, valid = target(s)
        payload = valid if body is None else body(valid)
        kwargs: dict[str, Any] = {"headers": headers_of()}
        if params is not None:
            kwargs["params"] = params
        if payload is not None:
            kwargs["json"] = payload
        return c.request(method, f"/v1/offerings/{CANARY_NAME_PATH if canary_ref else ref}{suffix}", **kwargs)
    return run


def _dispatch_path_op_calls(op_id: str, method: str,
                      target: Callable[[CallState], tuple[str, str, Any]]) -> tuple[CanaryCall, ...]:
    """as superfícies globais de `/v1` (`header_auth`, `header_client`, `query_key`) e o nome canário
    no `{ref}` (`path`) de uma operação nova, sempre recusados antes de qualquer regra."""
    return (
        CanaryCall(op_id, "path", _dispatch_call(method, target, canary_ref=True)),
        CanaryCall(op_id, "header_auth", _dispatch_call(method, target, headers_of=lambda: auth(ZEFIRO.ra))),
        CanaryCall(op_id, "header_client", _dispatch_call(
            method, target, headers_of=lambda: {**ADMIN, "X-CQ-Client": CANARY_ASCII})),
        CanaryCall(op_id, "query_key", _dispatch_call(method, target, params={ZEFIRO.name: "1"})),
    )


def _dispatch_canary_id_call(method: str, action: str = "") -> Callable[[Any, CallState], Any]:
    """o nome canário no `{dispatch_id}` (400 `dispatch_id invalid_format`, sem eco), sem consumir
    alvo; `action` é o sufixo (`/confirm`, `/cancel`) ou vazio (`dispatches.get`)."""
    def run(c: Any, s: CallState) -> Any:
        return c.request(method, f"/v1/offerings/{_dispatch_ref(s)}/dispatches/{CANARY_NAME_PATH}{action}",
                         headers=ADMIN)
    return run


CANARY_CALLS: tuple[CanaryCall, ...] = (
    # people.register
    CanaryCall("people.register", "body_value", lambda c, s: c.post(
        "/v1/people", json={"name": "Pessoa Com Email Canario",
                            "email": f"{ZEFIRO.name} <{ZEFIRO.email}>"}, headers=ADMIN)),
    CanaryCall("people.register", "body_key", lambda c, s: c.post(
        "/v1/people", json={"name": "Pessoa Com Chave Canario", ZEFIRO.name: ZEFIRO.ra}, headers=ADMIN)),
    CanaryCall("people.register", "header_client", lambda c, s: c.post(
        "/v1/people", json={"name": "Pessoa Cabecalho", "ra": _unique_ra()},
        headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("people.register", "header_auth", lambda c, s: c.post(
        "/v1/people", json={"name": "Pessoa Auth"}, headers=auth(URSULA.ra))),
    CanaryCall("people.register", "conflict", lambda c, s: c.post(
        "/v1/people", json=ZEFIRO.payload(), headers=ADMIN)),
    # people.list
    CanaryCall("people.list", "query_value", lambda c, s: c.get(
        "/v1/people", params={"limit": ZEFIRO.name}, headers=ADMIN)),
    CanaryCall("people.list", "query_key", lambda c, s: c.get(
        "/v1/people", params={ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("people.list", "header_client", lambda c, s: c.get(
        "/v1/people", headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("people.list", "header_auth", lambda c, s: c.get(
        "/v1/people", headers=auth(CANARY_ASCII))),
    # people.get
    CanaryCall("people.get", "path", lambda c, s: c.get(
        f"/v1/people/{CANARY_NAME_PATH}", headers=ADMIN)),
    CanaryCall("people.get", "path_ra", lambda c, s: c.get(
        f"/v1/people/{ZEFIRO.ra}", headers=ADMIN)),
    CanaryCall("people.get", "header_auth", lambda c, s: c.get(
        f"/v1/people/{s.person['alias']}", headers=auth(ZEFIRO.ra))),
    # identity.reveal
    CanaryCall("identity.reveal", "body_value", lambda c, s: c.post(
        "/v1/identity/reveal", json={"pseudonyms": [ZEFIRO.name, ZEFIRO.ra], "reason": "motivo qualquer"},
        headers=ADMIN)),
    CanaryCall("identity.reveal", "body_key", lambda c, s: c.post(
        "/v1/identity/reveal", json={"pseudonyms": [s.person["alias"]], "reason": "motivo qualquer",
                                     ZEFIRO.name: 1}, headers=ADMIN)),
    CanaryCall("identity.reveal", "header_client", lambda c, s: c.post(
        "/v1/identity/reveal", json={"pseudonyms": [s.person["alias"]], "reason": "motivo qualquer"},
        headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("identity.reveal", "header_auth", lambda c, s: c.post(
        "/v1/identity/reveal", json={"pseudonyms": [s.person["alias"]], "reason": "motivo qualquer"},
        headers=auth(ZEFIRO.email))),
    # tokens.create (o rótulo com nome canário é aceito e sai redigido)
    CanaryCall("tokens.create", "body_value", lambda c, s: c.post(
        "/v1/tokens", json={"label": f"token de {ZEFIRO.name} (RA {ZEFIRO.ra})", "permissions": ["people:read"]},
        headers=ADMIN)),
    CanaryCall("tokens.create", "body_key", lambda c, s: c.post(
        "/v1/tokens", json={"label": "rotulo", "permissions": ["people:read"], ZEFIRO.name: True},
        headers=ADMIN)),
    CanaryCall("tokens.create", "header_auth", lambda c, s: c.post(
        "/v1/tokens", json={"label": "rotulo", "permissions": ["people:read"]}, headers=auth(ZEFIRO.ra))),
    # tokens.list
    CanaryCall("tokens.list", "query_key", lambda c, s: c.get(
        "/v1/tokens", params={ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("tokens.list", "header_auth", lambda c, s: c.get(
        "/v1/tokens", headers=auth(CANARY_ASCII))),
    # tokens.revoke
    CanaryCall("tokens.revoke", "path", lambda c, s: c.post(
        f"/v1/tokens/{CANARY_NAME_PATH}/revoke", headers=ADMIN)),
    CanaryCall("tokens.revoke", "header_auth", lambda c, s: c.post(
        f"/v1/tokens/{s.token_id}/revoke", headers=auth(ZEFIRO.ra))),
    # events.list
    CanaryCall("events.list", "query_value", lambda c, s: c.get(
        "/v1/events", params={"types": ZEFIRO.name}, headers=ADMIN)),
    CanaryCall("events.list", "query_key", lambda c, s: c.get(
        "/v1/events", params={"types": "person.registered", ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("events.list", "header_auth", lambda c, s: c.get(
        "/v1/events", params={"types": "person.registered"}, headers=auth(ZEFIRO.email))),
    # superfícies globais de /v1 que faltavam acima (X-CQ-Client e consulta estrita)
    CanaryCall("people.register", "query_key", lambda c, s: c.post(
        "/v1/people", params={ZEFIRO.name: "1"}, json={"name": "Pessoa Consulta", "ra": _unique_ra()},
        headers=ADMIN)),
    CanaryCall("people.get", "header_client", lambda c, s: c.get(
        f"/v1/people/{s.person['alias']}", headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("people.get", "query_key", lambda c, s: c.get(
        f"/v1/people/{s.person['alias']}", params={ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("identity.reveal", "query_key", lambda c, s: c.post(
        "/v1/identity/reveal", params={ZEFIRO.name: "1"},
        json={"pseudonyms": [s.person["alias"]], "reason": "motivo qualquer"}, headers=ADMIN)),
    CanaryCall("tokens.create", "header_client", lambda c, s: c.post(
        "/v1/tokens", json={"label": "rotulo", "permissions": ["people:read"]},
        headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("tokens.create", "query_key", lambda c, s: c.post(
        "/v1/tokens", params={ZEFIRO.name: "1"}, json={"label": "rotulo", "permissions": ["people:read"]},
        headers=ADMIN)),
    CanaryCall("tokens.list", "header_client", lambda c, s: c.get(
        "/v1/tokens", headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("tokens.revoke", "header_client", lambda c, s: c.post(
        f"/v1/tokens/{s.token_id}/revoke", headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("tokens.revoke", "query_key", lambda c, s: c.post(
        f"/v1/tokens/{s.token_id}/revoke", params={ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("tokens.revoke", "body_key", lambda c, s: c.post(
        f"/v1/tokens/{s.token_id}/revoke", json={ZEFIRO.name: ZEFIRO.ra}, headers=ADMIN)),
    CanaryCall("events.list", "header_client", lambda c, s: c.get(
        "/v1/events", params={"types": "person.registered"}, headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    # rota inexistente com o nome canário no caminho
    CanaryCall("route.unknown", "path", lambda c, s: c.get(
        f"/v1/{CANARY_NAME_PATH}", headers=ADMIN)),
    # 405 sob /v1 com o nome canário no caminho (rota existe, método não)
    CanaryCall("route.method_not_allowed", "path", lambda c, s: c.request(
        "DELETE", f"/v1/people/{CANARY_NAME_PATH}", headers=ADMIN)),
    # fora de /v1: /health e /openapi.json ignoram consulta, corpo e X-CQ-Client
    CanaryCall("system.health", "query_key", lambda c, s: c.get(
        "/health", params={ZEFIRO.name: ZEFIRO.ra})),
    CanaryCall("system.health", "header_client", lambda c, s: c.get(
        "/health", headers={"X-CQ-Client": CANARY_ASCII})),
    CanaryCall("system.health", "method_body", lambda c, s: c.post(
        "/health", json={ZEFIRO.name: ZEFIRO.email})),
    CanaryCall("route.openapi", "query_key", lambda c, s: c.get(
        "/openapi.json", params={ZEFIRO.name: ZEFIRO.ra}, headers={"X-CQ-Client": CANARY_ASCII})),
    # barra final: caminho desconhecido, sem redirecionamento
    CanaryCall("route.trailing_slash", "path", lambda c, s: c.get(
        f"/v1/people/{CANARY_NAME_PATH}/", headers=ADMIN, follow_redirects=False)),
    # Authorization fora de /v1 é ignorado
    CanaryCall("system.health", "header_auth", lambda c, s: c.get("/health", headers=auth(ZEFIRO.ra))),
    CanaryCall("route.openapi", "header_auth", lambda c, s: c.get(
        "/openapi.json", headers=auth(CANARY_ASCII))),
    # corpo JSON que não é UTF-8, com a identidade dentro
    CanaryCall("people.register", "body_non_utf8", lambda c, s: c.post(
        "/v1/people", content=('{"name": "' + ZEFIRO.name + '"}').encode("latin-1"),
        headers={**ADMIN, "Content-Type": "application/json"})),
    # valores inválidos com identidade dentro (erro sem eco)
    CanaryCall("tokens.create", "body_value_invalid", lambda c, s: c.post(
        "/v1/tokens", json={"label": "rotulo", "permissions": [ZEFIRO.name]}, headers=ADMIN)),
    CanaryCall("identity.reveal", "body_value_long", lambda c, s: c.post(
        "/v1/identity/reveal", json={"pseudonyms": [s.person["alias"]],
                                     "reason": (ZEFIRO.name + " ") * 25}, headers=ADMIN)),
    # --- Oferecimentos e bots ---
    # offerings.create (disciplina e período canário são aceitos e saem redigidos)
    CanaryCall("offerings.create", "body_value", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(course=SEEDED_FREE_TEXT["course"],
                                             term=SEEDED_FREE_TEXT["term"]), headers=ADMIN)),
    CanaryCall("offerings.create", "body_value_code", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(code=f"oferta-{ZEFIRO.ra}"), headers=ADMIN)),
    CanaryCall("offerings.create", "body_key", lambda c, s: c.post(
        "/v1/offerings", json={**_offering_body(), ZEFIRO.name: ZEFIRO.ra}, headers=ADMIN)),
    CanaryCall("offerings.create", "query_key", lambda c, s: c.post(
        "/v1/offerings", params={ZEFIRO.name: "1"}, json=_offering_body(), headers=ADMIN)),
    CanaryCall("offerings.create", "header_client", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(), headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("offerings.create", "header_auth", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(), headers=auth(ZEFIRO.ra))),
    # offerings.list
    CanaryCall("offerings.list", "query_value", lambda c, s: c.get(
        "/v1/offerings", params={"limit": ZEFIRO.name}, headers=ADMIN)),
    CanaryCall("offerings.list", "query_key", lambda c, s: c.get(
        "/v1/offerings", params={ZEFIRO.name: "1"}, headers=ADMIN)),
    CanaryCall("offerings.list", "header_client", lambda c, s: c.get(
        "/v1/offerings", headers={**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("offerings.list", "header_auth", lambda c, s: c.get(
        "/v1/offerings", headers=auth(CANARY_ASCII))),
    # offerings.get (o RA tem o formato de código: chega ao domínio e dá 404)
    CanaryCall("offerings.get", "path", lambda c, s: c.get(
        f"/v1/offerings/{CANARY_NAME_PATH}", headers=ADMIN)),
    CanaryCall("offerings.get", "path_ra", lambda c, s: c.get(
        f"/v1/offerings/{ZEFIRO.ra}", headers=ADMIN)),
    *_path_op_calls("offerings.get", "GET", ""),
    # offerings.close, offerings.prepare_archive, bots.activate e bots.get (sem corpo)
    CanaryCall("offerings.close", "path", lambda c, s: c.post(
        f"/v1/offerings/{CANARY_NAME_PATH}/close", headers=ADMIN)),
    *_path_op_calls("offerings.close", "POST", "/close"),
    CanaryCall("offerings.prepare_archive", "path", lambda c, s: c.post(
        f"/v1/offerings/{CANARY_NAME_PATH}/archive/prepare", headers=ADMIN)),
    *_path_op_calls("offerings.prepare_archive", "POST", "/archive/prepare"),
    CanaryCall("bots.activate", "path", lambda c, s: c.post(
        f"/v1/offerings/{CANARY_NAME_PATH}/bot/activate", headers=ADMIN)),
    *_path_op_calls("bots.activate", "POST", "/bot/activate"),
    CanaryCall("bots.get", "path", lambda c, s: c.get(
        f"/v1/offerings/{CANARY_NAME_PATH}/bot", headers=ADMIN)),
    *_path_op_calls("bots.get", "GET", "/bot"),
    # offerings.archive (código de confirmação longo demais com o nome canário: 400 sem eco)
    CanaryCall("offerings.archive", "path", lambda c, s: c.post(
        f"/v1/offerings/{CANARY_NAME_PATH}/archive", json={"confirmation_code": _CONFIRMATION},
        headers=ADMIN)),
    CanaryCall("offerings.archive", "body_value", lambda c, s: c.post(
        f"/v1/offerings/{_draft(s)}/archive", json={"confirmation_code": (ZEFIRO.name + " ") * 3},
        headers=ADMIN)),
    CanaryCall("offerings.archive", "body_key", lambda c, s: c.post(
        f"/v1/offerings/{_draft(s)}/archive", json={"confirmation_code": _CONFIRMATION, ZEFIRO.name: 1},
        headers=ADMIN)),
    *_path_op_calls("offerings.archive", "POST", "/archive", {"confirmation_code": _CONFIRMATION}),
    # bots.register (token com o nome canário: formato local inválido, sem chamar o Telegram)
    CanaryCall("bots.register", "path", lambda c, s: c.put(
        f"/v1/offerings/{CANARY_NAME_PATH}/bot", json={"token": _dummy_bot_token()}, headers=ADMIN)),
    CanaryCall("bots.register", "body_value", lambda c, s: c.put(
        f"/v1/offerings/{_draft(s)}/bot", json={"token": f"{ZEFIRO.ra}:{ZEFIRO.name}"}, headers=ADMIN)),
    CanaryCall("bots.register", "body_key", lambda c, s: c.put(
        f"/v1/offerings/{_draft(s)}/bot", json={"token": _dummy_bot_token(), ZEFIRO.name: ZEFIRO.ra},
        headers=ADMIN)),
    *_path_op_calls("bots.register", "PUT", "/bot", {"token": DUMMY_BOT_TOKEN}),
    # team.link
    CanaryCall("team.link", "path", lambda c, s: c.post(
        f"/v1/offerings/{CANARY_NAME_PATH}/team-links", json={"person": s.link_person["alias"]},
        headers=ADMIN)),
    CanaryCall("team.link", "body_value", lambda c, s: c.post(
        f"/v1/offerings/{_active(s)}/team-links", json={"person": ZEFIRO.name}, headers=ADMIN)),
    CanaryCall("team.link", "body_key", lambda c, s: c.post(
        f"/v1/offerings/{_active(s)}/team-links",
        json={"person": s.link_person["alias"], ZEFIRO.name: ZEFIRO.ra}, headers=ADMIN)),
    *_path_op_calls("team.link", "POST", "/team-links", _LINK_PERSON),
    # --- Vínculo do aluno (resultados no comentário do módulo) ---
    # students.import (no rascunho `import_offering`; a linha com a identidade canário é recusada sem
    # eco; o RA de Zéfiro com o e-mail de Olímpio, os dois cadastrados, dá 409 `list_conflict`)
    CanaryCall("students.import", "path", _student_link_canary_ref_call("POST", "/students", _import_target)),
    CanaryCall("students.import", "body_value", lambda c, s: c.post(
        f"/v1/offerings/{_import_ref(s)}/students",
        json={"rows": [{"name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": f"{ZEFIRO.name} <{ZEFIRO.email}>"}]},
        headers=ADMIN)),
    CanaryCall("students.import", "body_key", _student_link_body_call("POST", "/students", _import_target,
                                                            {ZEFIRO.name: ZEFIRO.ra})),
    CanaryCall("students.import", "body_key_row", lambda c, s: c.post(
        f"/v1/offerings/{_import_ref(s)}/students",
        json={"rows": [{**_neutral_row("Aluno Sonda Lista", "aluno.sonda"), ZEFIRO.name: ZEFIRO.ra}]},
        headers=ADMIN)),
    CanaryCall("students.import", "conflict", lambda c, s: c.post(
        f"/v1/offerings/{_import_ref(s)}/students",
        json={"rows": [{"name": ZEFIRO.name, "ra": ZEFIRO.ra, "email": OLIMPIO.email}]}, headers=ADMIN)),
    *_student_link_path_op_calls("students.import", "POST", "/students", _import_target),
    # students.summary (sem corpo nem consulta; o RA canário tem o formato de código: 404)
    CanaryCall("students.summary", "path", _student_link_canary_ref_call("GET", "/students/summary", _summary_target)),
    CanaryCall("students.summary", "path_ra", lambda c, s: c.get(
        f"/v1/offerings/{ZEFIRO.ra}/students/summary", headers=ADMIN)),
    *_student_link_path_op_calls("students.summary", "GET", "/students/summary", _summary_target),
    # invites.prepare_send (no `invite_offering`)
    CanaryCall("invites.prepare_send", "path",
               _student_link_canary_ref_call("POST", "/invites/prepare", _prepare_target)),
    CanaryCall("invites.prepare_send", "body_value", lambda c, s: c.post(
        f"/v1/offerings/{_invite_ref(s)}/invites/prepare", json={"valid_hours": ZEFIRO.name}, headers=ADMIN)),
    CanaryCall("invites.prepare_send", "body_key", _student_link_body_call("POST", "/invites/prepare", _prepare_target,
                                                                 {ZEFIRO.name: ZEFIRO.ra})),
    *_student_link_path_op_calls("invites.prepare_send", "POST", "/invites/prepare", _prepare_target),
    # invites.send (cada chamada consome um alvo, com o código de verdade do preparo da semente)
    CanaryCall("invites.send", "path", _student_link_canary_ref_call("POST", "/invites", _send_target)),
    CanaryCall("invites.send", "body_value", _student_link_body_call("POST", "/invites", _send_target,
                                                          {"confirmation_code": (ZEFIRO.name + " ") * 3})),
    CanaryCall("invites.send", "body_key", _student_link_body_call("POST", "/invites", _send_target, {ZEFIRO.name: 1})),
    *_student_link_path_op_calls("invites.send", "POST", "/invites", _send_target),
    # invites.resend (no `invite_offering`, com o pseudônimo de `resend_person`)
    CanaryCall("invites.resend", "path", _student_link_canary_ref_call("POST", "/invites/resend", _resend_target)),
    CanaryCall("invites.resend", "body_value", lambda c, s: c.post(
        f"/v1/offerings/{_invite_ref(s)}/invites/resend", json={"pseudonyms": [ZEFIRO.name, ZEFIRO.ra]},
        headers=ADMIN)),
    CanaryCall("invites.resend", "body_key", _student_link_body_call("POST", "/invites/resend", _resend_target,
                                                           {ZEFIRO.name: ZEFIRO.ra})),
    *_student_link_path_op_calls("invites.resend", "POST", "/invites/resend", _resend_target),
    # --- Disparo e resposta (resultados no comentário do módulo) ---
    # dispatches.prepare (no `dispatch_offering`; o `repo` com o nome canário ou com o token privado na
    # consulta é recusado pela forma, sem eco, antes do host)
    CanaryCall("dispatches.prepare", "body_value", _dispatch_call(
        "POST", _dispatch_prepare_target, body=lambda valid: {**valid, "repo": f"https://repos.cq.test/{ZEFIRO.name}.git"})),
    CanaryCall("dispatches.prepare", "body_value_secret", _dispatch_call(
        "POST", _dispatch_prepare_target,
        body=lambda valid: {**valid, "repo": f"{SEED_REPO}?private_token={_repo_private_token()}"})),
    CanaryCall("dispatches.prepare", "body_key", _dispatch_call(
        "POST", _dispatch_prepare_target, body=lambda valid: {**valid, ZEFIRO.name: ZEFIRO.ra})),
    *_dispatch_path_op_calls("dispatches.prepare", "POST", _dispatch_prepare_target),
    # dispatches.get (o `dispatch_shown`)
    CanaryCall("dispatches.get", "path_dispatch", _dispatch_canary_id_call("GET")),
    *_dispatch_path_op_calls("dispatches.get", "GET", _dispatch_get_target),
    # dispatches.list (o nome canário no cursor)
    CanaryCall("dispatches.list", "query_value", _dispatch_call("GET", _dispatch_list_target, params={"after": ZEFIRO.name})),
    *_dispatch_path_op_calls("dispatches.list", "GET", _dispatch_list_target),
    # dispatches.confirm (cada chamada com um `dispatch_id` real consome um alvo de `confirm_targets`)
    CanaryCall("dispatches.confirm", "path_dispatch", _dispatch_canary_id_call("POST", "/confirm")),
    *_dispatch_path_op_calls("dispatches.confirm", "POST", _dispatch_confirm_target),
    # dispatches.cancel (cada chamada com um `dispatch_id` real consome um alvo de `cancel_targets`)
    CanaryCall("dispatches.cancel", "path_dispatch", _dispatch_canary_id_call("POST", "/cancel")),
    *_dispatch_path_op_calls("dispatches.cancel", "POST", _dispatch_cancel_target),
    # messages.list (o nome canário no cursor)
    CanaryCall("messages.list", "query_value", _dispatch_call("GET", _dispatch_messages_target, params={"after": ZEFIRO.name})),
    *_dispatch_path_op_calls("messages.list", "GET", _dispatch_messages_target),
    # --- Mensagens livres (resultados no comentário do módulo) ---
    # offerings.create: o e-mail válido de um aluno canário cadastrado como contato é recusado sem eco
    # (a regra depois do 403); o nome canário no contato é recusado pela forma
    CanaryCall("offerings.create", "body_value_contact", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(contact_email=STUDENT_A.email), headers=ADMIN)),
    CanaryCall("offerings.create", "body_value_contact_invalid", lambda c, s: c.post(
        "/v1/offerings", json=_offering_body(contact_email=ZEFIRO.name), headers=ADMIN)),
    # offerings.update_settings (no rascunho semeado; nenhuma chamada canário muda a configuração)
    CanaryCall("offerings.update_settings", "body_value", _dispatch_call(
        "PATCH", _dispatch_settings_target, body=lambda valid: {"contact_email": STUDENT_A.email})),
    CanaryCall("offerings.update_settings", "body_value_invalid", _dispatch_call(
        "PATCH", _dispatch_settings_target, body=lambda valid: {"contact_email": ZEFIRO.name})),
    CanaryCall("offerings.update_settings", "body_key", _dispatch_call(
        "PATCH", _dispatch_settings_target, body=lambda valid: {**valid, ZEFIRO.name: ZEFIRO.ra})),
    *_dispatch_path_op_calls("offerings.update_settings", "PATCH", _dispatch_settings_target),
    # --- Relatórios (resultados no comentário do módulo) ---
    # reports.get (o `report_shown`; o nome canário em `version` e em `format`)
    CanaryCall("reports.get", "path_dispatch", _report_canary_id_call("GET")),
    CanaryCall("reports.get", "query_value", _dispatch_call("GET", _report_get_target(""), params={"version": ZEFIRO.name})),
    CanaryCall("reports.get", "query_value_format", _dispatch_call(
        "GET", _report_get_target(""), params={"format": ZEFIRO.name})),
    *_dispatch_path_op_calls("reports.get", "GET", _report_get_target("")),
    # reports.list_versions (o nome canário no cursor)
    CanaryCall("reports.list_versions", "path_dispatch", _report_canary_id_call("GET", "/versions")),
    CanaryCall("reports.list_versions", "query_value", _dispatch_call(
        "GET", _report_get_target("/versions"), params={"after": ZEFIRO.name})),
    *_dispatch_path_op_calls("reports.list_versions", "GET", _report_get_target("/versions")),
    # reports.resend_summary (nenhuma chamada canário chega à regra nem congela versão)
    CanaryCall("reports.resend_summary", "path_dispatch", _report_canary_id_call("POST", "/resend")),
    *_dispatch_path_op_calls("reports.resend_summary", "POST", _report_get_target("/resend")),
    # offerings.get_digest (sem corpo nem consulta)
    *_dispatch_path_op_calls("offerings.get_digest", "GET", _digest_get_target),
    # offerings.update_digest (o nome canário no horário; nenhuma chamada canário muda a configuração)
    CanaryCall("offerings.update_digest", "body_value", _dispatch_call(
        "PATCH", _digest_set_target, body=lambda valid: {"local_time": ZEFIRO.name})),
    CanaryCall("offerings.update_digest", "body_key", _dispatch_call(
        "PATCH", _digest_set_target, body=lambda valid: {**valid, ZEFIRO.name: ZEFIRO.ra})),
    *_dispatch_path_op_calls("offerings.update_digest", "PATCH", _digest_set_target),
)


# CLI e expurgo: o caminho sob `/v1/offerings/{ref}` e o corpo válido de cada operação nova com `{ref}` (no formato dos
# alvos dos disparos, para o `_dispatch_call` e o `_dispatch_path_op_calls`), lidos na hora da chamada. As chamadas canário de
# `offerings.purge` usam o 2º dos `purge_targets` (arquivado, preparado e não expurgado), nunca o da `VALID_CALLS`.
def _purge_prepare_call_target(s: CallState) -> tuple[str, str, Any]:
    return _purge_prepare_ref(s), "/purge/prepare", None


def _purge_confirm_call_target(s: CallState) -> tuple[str, str, Any]:
    code, confirmation = _purge_target(s, 1)
    return code, "/purge", {"confirmation_code": confirmation}


def _doctor_call(headers_of: Callable[[], dict], params: dict | None = None) -> Callable[[Any, CallState], Any]:
    """`GET /v1/system/doctor` (sem `{ref}`, sem consulta e sem corpo) com os cabeçalhos e a consulta dados."""
    def run(c: Any, s: CallState) -> Any:
        kwargs: dict[str, Any] = {"headers": headers_of()}
        if params is not None:
            kwargs["params"] = params
        return c.get("/v1/system/doctor", **kwargs)
    return run


CANARY_CALLS = CANARY_CALLS + (
    # --- CLI e expurgo (resultados em `DOCTOR_PURGE_CANARY_EXPECTED`) ---
    # system.doctor (as superfícies globais de /v1; o nome canário numa consulta)
    CanaryCall("system.doctor", "header_auth", _doctor_call(lambda: auth(ZEFIRO.ra))),
    CanaryCall("system.doctor", "header_client", _doctor_call(lambda: {**ADMIN, "X-CQ-Client": CANARY_ASCII})),
    CanaryCall("system.doctor", "query_key", _doctor_call(lambda: ADMIN, {ZEFIRO.name: "1"})),
    # offerings.prepare_purge (sem corpo; no 1º alvo de preparação)
    *_dispatch_path_op_calls("offerings.prepare_purge", "POST", _purge_prepare_call_target),
    # offerings.purge (no 2º alvo preparado): o nome canário como `confirmation_code` de formato válido → 409
    # `invalid_confirmation`, sem eco e sem mudança de estado
    CanaryCall("offerings.purge", "body_value", _dispatch_call(
        "POST", _purge_confirm_call_target, body=lambda valid: {"confirmation_code": ZEFIRO.name})),
    CanaryCall("offerings.purge", "body_key", _dispatch_call(
        "POST", _purge_confirm_call_target, body=lambda valid: {**valid, ZEFIRO.name: ZEFIRO.ra})),
    *_dispatch_path_op_calls("offerings.purge", "POST", _purge_confirm_call_target),
    # events.list: o nome canário no valor de `person` (com `types` válido)
    CanaryCall("events.list", "query_value_person", lambda c, s: c.get(
        "/v1/events", params={"types": "person.registered", "person": ZEFIRO.name}, headers=ADMIN)),
)

# CLI e expurgo: o resultado exato de cada chamada canário dessa parte, `(status, code, fields)`, com
# `fields` = os pares `(field, reason)` no 400 e `None` no 401 e no 409. A conferência é por igualdade.
_DOCTOR_PURGE_UNKNOWN_QUERY = [("query", "unknown_field")]
_DOCTOR_PURGE_BAD_CLIENT = [("X-CQ-Client", "invalid_value")]
_DOCTOR_PURGE_BAD_REF = [("ref", "invalid_format")]
DOCTOR_PURGE_CANARY_EXPECTED: dict[tuple[str, str], tuple[int, str, list[tuple[str, str]] | None]] = {
    ("system.doctor", "header_auth"): (401, "unauthenticated", None),
    ("system.doctor", "header_client"): (400, "invalid_request", _DOCTOR_PURGE_BAD_CLIENT),
    ("system.doctor", "query_key"): (400, "invalid_request", _DOCTOR_PURGE_UNKNOWN_QUERY),
    ("offerings.purge", "body_value"): (409, "invalid_confirmation", None),
    ("offerings.purge", "body_key"): (400, "invalid_request", [("body", "unknown_field")]),
    ("events.list", "query_value_person"): (400, "invalid_request", [("person", "invalid_format")]),
}
for _op in ("offerings.prepare_purge", "offerings.purge"):
    DOCTOR_PURGE_CANARY_EXPECTED.update({
        (_op, "path"): (400, "invalid_request", _DOCTOR_PURGE_BAD_REF),
        (_op, "header_auth"): (401, "unauthenticated", None),
        (_op, "header_client"): (400, "invalid_request", _DOCTOR_PURGE_BAD_CLIENT),
        (_op, "query_key"): (400, "invalid_request", _DOCTOR_PURGE_UNKNOWN_QUERY),
    })
del _op
assert {(c.op_id, c.surface) for c in CANARY_CALLS[-len(DOCTOR_PURGE_CANARY_EXPECTED):]} == set(DOCTOR_PURGE_CANARY_EXPECTED), (
    "chamada canário do expurgo sem resultado esperado")

# Textos livres semeados no canário: só alvos inteiros, com fronteira, para que a
# redação correta os troque por completo.
SEEDED_FREE_TEXT = {
    "label": f"token de {ZEFIRO.name} (RA {ZEFIRO.ra})",
    "reason": f"conferir {URSULA.name} e {ZEFIRO.email}",
    "task_error": f"falhou para {ZEFIRO.name} RA {ZEFIRO.ra}",
    "outbox_reason": f"destinatário {URSULA.name} bloqueou (RA {URSULA.ra})",
    # disciplina e período do oferecimento (nome, e-mail e RA canário; o período cabe em 40)
    "course": f"Seminário de {ZEFIRO.name} ({ZEFIRO.email})",
    "term": f"2031s1 RA {URSULA.ra}",
}

# Varredura SQL do canário: tabela → colunas varridas com `find_canary`. `identities` é a
# área restrita (controle positivo); `outbox_messages.text` é o conteúdo da entrega (fica fora, como
# o registro do console); `api_tokens.secret_sha256` é o resumo do segredo (fica fora de
# `find_secrets`, que só varre saídas).
SQL_SCAN: dict[str, list[str]] = {
    "people": ["id::text", "pseudo_id::text", "alias", "created_at::text"],
    "api_tokens": ["token_id::text", "label", "permissions::text", "created_at::text", "revoked_at::text"],
    "events": ["seq::text", "type", "occurred_at::text", "actor_token_id", "data::text"],
    "tasks": ["id::text", "task_type", "payload::text", "idempotency_key", "state", "last_error"],
    "outbox_messages": ["id::text", "recipient_person_id::text", "idempotency_key", "state",
                        "channel_ref", "last_error",
                        "bot_id::text", "delivery_endpoint_id::text", "reply_to_update_id::text"],
    # Oferecimentos e bots: todas as colunas das tabelas novas, fora as internas de identidade
    # (`INTERNAL_IDENTITY_COLUMNS`, com a sua própria varredura e controle positivo).
    "offerings": ["id::text", "offering_id::text", "code", "course", "term", "timezone",
                  "response_window_minutes::text", "state", "created_at::text", "activated_at::text",
                  "closed_at::text", "archived_at::text", "archive_code_sha256",
                  "archive_code_expires_at::text"],
    "bots": ["id::text", "bot_uuid::text", "offering_id::text", "telegram_bot_id::text", "username",
             "token_ciphertext::text", "token_version::text", "state", "access_problem",
             "profile_state", "lease_owner", "lease_until::text", "last_poll_ok_at::text",
             "last_problem", "last_problem_at::text", "update_epoch::text", "last_update_at::text",
             "registered_at::text", "token_replaced_at::text"],
    "enrollments": ["id::text", "person_id::text", "offering_id::text", "role", "state",
                    "created_at::text", "activated_at::text"],
    "invites": ["id::text", "enrollment_id::text", "code_sha256", "state", "expires_at::text",
                "created_at::text", "used_at::text", "revoked_at::text"],
    "delivery_endpoints": ["id::text", "person_id::text", "bot_id::text", "channel", "state",
                           "created_at::text", "changed_at::text"],
    "telegram_updates": ["id::text", "bot_id::text", "update_epoch::text", "update_id::text", "kind",
                         "chat_type", "text", "command", "details::text", "received_at::text", "state",
                         "outcome", "processed_at::text"],
}
# Vínculo do aluno: as colunas novas das tabelas de oferecimentos e bots e a tabela nova `invite_sends`, todas sem
# identidade. O `offerings.invite_code_sha256` é o resumo do código de confirmação do envio, que não é
# segredo. Cada tabela tem linha no canário (a semente importa alunos e prepara
# envios; `VALID_CALLS` confirma um envio e reenvia).
SQL_SCAN["enrollments"] += ["terms_version::text", "terms_accepted_at::text"]
SQL_SCAN["invites"] += ["invite_send_id::text", "presented_update_id::text", "presented_terms_version::text"]
SQL_SCAN["offerings"] += ["invite_code_sha256", "invite_code_expires_at::text", "invite_valid_hours::text",
                          "invite_eligible_count::text"]
SQL_SCAN["invite_sends"] = ["id::text", "enrollment_id::text", "origin", "valid_hours::text", "state",
                            "failure", "cancel_reason", "error_type", "attempts::text", "created_at::text",
                            "finished_at::text"]
# Disparo e resposta: as 7 tabelas novas, com todas as colunas,
# inclusive as de texto do aluno da lista fechada (`submissions.raw_input`,
# `submitted_answers`, `format_errors`, `feedback`, `params`, `partial_scores`;
# `delivery_contents.parts` e `current_render`) e o texto de terceiros (`question_refs.title`,
# `dispatches.preview` e `rejection_reasons`, redigidos; `delivery_contents.snapshot`, `ir` e
# `rendered`, literais); `media_blobs.data` pela forma `escape` (o ASCII fica legível); as 7 colunas
# novas de `outbox_messages` e a coluna nova de `telegram_updates`. Nenhuma coluna interna de
# identidade nova (`INTERNAL_IDENTITY_COLUMNS` não muda). Cada tabela tem linha no canário (a semente
# valida disparos; a fase do disparo abre, entrega, recebe respostas e mensagens livres).
SQL_SCAN["question_refs"] = ["id::text", "ref_uuid::text", "repo", "repo_sha256", "commit_sha", "qid",
                             "supported::text", "show_correct_answer::text", "single_variant::text",
                             "partial_credit::text", "title", "answers_names::text", "content_hash",
                             "inspected_at::text", "created_at::text"]
SQL_SCAN["dispatches"] = ["id::text", "dispatch_uuid::text", "offering_id::text", "question_ref_id::text",
                          "origin", "immediate::text", "opens_at::text", "closes_at::text",
                          "window_minutes::text", "state", "preview::text", "rejection",
                          "rejection_reasons::text", "cancel_reason", "created_at::text", "validated_at::text",
                          "confirm_deadline::text", "confirmed_at::text", "opened_at::text", "closed_at::text",
                          "rejected_at::text", "cancelled_at::text"]
SQL_SCAN["deliveries"] = ["id::text", "delivery_uuid::text", "dispatch_id::text", "enrollment_id::text",
                          "delivery_endpoint_id::text", "seed::text", "button_token", "state",
                          "undelivered_reason", "failure", "current_field::text", "render_version::text",
                          "controls_version::text", "principal_message_id::text", "created_at::text",
                          "variant_at::text", "sent_at::text", "answered_at::text", "revealed_at::text"]
SQL_SCAN["delivery_contents"] = ["delivery_id::text", "snapshot::text", "ir::text", "media_refs::text",
                                 "rendered::text", "parts::text", "selection::text", "current_render::text"]
SQL_SCAN["media_blobs"] = ["sha256", "content_type", "size_bytes::text", "encode(data, 'escape')",
                           "created_at::text"]
SQL_SCAN["submissions"] = ["id::text", "submission_uuid::text", "delivery_id::text", "telegram_update_id::text",
                           "outcome", "valid::text", "refusal", "failure_code", "failure_phase", "late::text",
                           "normalized_fields::text", "raw_input::text", "submitted_answers::text",
                           "format_errors::text", "feedback::text", "params::text", "partial_scores::text",
                           "correct_answers::text", "score::text", "part_derived::text",
                           "engine_mismatch::text", "pl_mismatch::text", "response_ms::text",
                           "delay_notice_sent::text", "received_at::text", "created_at::text",
                           "graded_at::text"]
SQL_SCAN["free_messages"] = ["id::text", "offering_id::text", "delivery_endpoint_id::text",
                             "telegram_update_id::text", "text", "replied::text", "ambiguous::text",
                             "received_at::text", "created_at::text"]
SQL_SCAN["outbox_messages"] += ["delivery_id::text", "delivery_role", "operation", "render_version::text",
                                "media_id", "field_index::text", "priority::text"]
SQL_SCAN["telegram_updates"] += ["reply_to_message_id::text"]
# Mensagens livres: a coluna nova de `offerings` e a tabela nova `offering_contacts`,
# com o `email` (a recusa do contato que a redação alteraria deixa só o e-mail do professor canário, fora
# dos prefixos). Tem linha no canário: o oferecimento da fase de disparos é cadastrado com contato.
SQL_SCAN["offerings"] += ["free_messages::text"]
SQL_SCAN["offering_contacts"] = ["id::text", "offering_id::text", "email", "updated_at::text"]
# Relatórios: as 7 tabelas novas, com todas as colunas (o `content` do relatório congelado,
# já redigido; a fotografia agregada `team_notices.data`; a amostra da prévia, `dispatch_preview_contents`,
# literal, cuja exceção para o conteúdo da questão canário fica nomeada no canário) e as colunas
# novas de `outbox_messages` e de `invite_sends`. Nenhuma coluna interna de identidade nova.
SQL_SCAN["dispatch_reports"] = ["id::text", "report_uuid::text", "dispatch_id::text", "version::text", "trigger",
                                "content::text", "frozen_at::text"]
SQL_SCAN["team_notices"] = ["id::text", "offering_id::text", "kind", "subject_key", "state",
                            "recipient_count::text", "dispatch_id::text", "report_id::text", "local_date::text",
                            "data::text", "created_at::text", "finished_at::text"]
SQL_SCAN["team_notice_floors"] = ["id::text", "event_floor::text", "invite_send_floor::text"]
SQL_SCAN["dispatch_previews"] = ["id::text", "dispatch_id::text", "seed::text", "button_token",
                                 "created_at::text"]
SQL_SCAN["dispatch_preview_contents"] = ["preview_id::text", "ir::text", "media_refs::text", "rendered::text"]
SQL_SCAN["offering_digests"] = ["id::text", "offering_id::text", "enabled::text", "local_time",
                                "next_due_at::text", "updated_at::text"]
SQL_SCAN["invite_batches"] = ["id::text", "batch_uuid::text", "offering_id::text", "created_at::text"]
SQL_SCAN["outbox_messages"] += ["notice_id::text", "text_format"]
SQL_SCAN["invite_sends"] += ["batch_id::text"]
# CLI e expurgo: as 5 colunas novas de `offerings` (o horário e o resultado do expurgo, só
# contagens; o resumo do código de confirmação pendente, que não é segredo; a validade; o impacto da preparação).
# Nenhuma tabela nova, nenhuma coluna interna de identidade nova. Têm linha preenchida no canário: o oferecimento
# expurgado (fase do expurgo e `_api_phase`) e o 2º dos `purge_targets` (preparado e não expurgado).
SQL_SCAN["offerings"] += ["purged_at::text", "purge_result::text", "purge_code_sha256",
                          "purge_code_expires_at::text", "purge_prepared_counts::text"]

# Oferecimentos e bots: colunas internas de identidade (o id do chat e do
# remetente do Telegram). Ficam fora de `SQL_SCAN` e da varredura genérica de `tg-id`; o canário as
# varre à parte, com controle positivo (o `tg-id` do remetente canário aparece nelas).
INTERNAL_IDENTITY_COLUMNS: frozenset[tuple[str, str]] = frozenset({
    ("telegram_updates", "chat_id"),
    ("telegram_updates", "sender_id"),
    ("delivery_endpoints", "chat_id"),
})

DECLARED_SURFACES: dict[str, set[str]] = {}
for _call in CANARY_CALLS:
    DECLARED_SURFACES.setdefault(_call.op_id, set()).add(_call.surface)
