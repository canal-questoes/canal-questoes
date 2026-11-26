"""Rotas da API v1, uma por operação do catálogo.

Toda rota `/v1` segue esta ordem, depois da autenticação do middleware e do roteador (404 e
405): 1) validação do pedido inteiro (`X-CQ-Client`, caminho, consulta e corpo) → 400 com todos os
campos; 2) permissão → 403; 3) regras da operação → 404/409. A única rota que grava algo num 403 é a
revelação, que registra `identity.reveal_denied` antes de responder.

Oferecimentos, bots e equipe: nas regras, `not_configured` (só em `bots.register`, sem `BotRuntime` ou
sem chave-mestra) → 404 do oferecimento → 404 da pessoa (só em `team.link`) → `invalid_state` →
`bot_required` → chamadas ao Telegram (`unavailable`; token recusado é 400) → `bot_conflict`,
`already_exists`, `invalid_confirmation`. Os resultados sentinela do núcleo viram os erros aqui.

Lista de alunos e convites: nas regras, 404 do oferecimento → `invalid_state` → `bot_required` (só nas
operações de convite) → `invalid_confirmation` (só em `invites.send`) → `list_conflict` (só em
`students.import`); em `team.link`, o `role_conflict` vem depois de `bot_required`. Nenhuma resposta
dessas operações traz identidade nem liga linha da lista a pseudônimo; os erros por item da lista saem em
`rows[i]`, sem o valor.

Disparos: `dispatches.prepare` (404 do oferecimento → `invalid_state` →
`bot_required`), `dispatches.get` (404 → 404 do disparo), `dispatches.confirm` (404 → 404 →
`invalid_state` → `bot_required` → `invalid_dispatch_state` → `window_expired`), `dispatches.cancel`
(404 → 404 → `invalid_dispatch_state`), `dispatches.list` e `messages.list` (404 do oferecimento, em
qualquer estado); no arquivamento, `grading_pending` depois de `invalid_state`. O preparo recusa
o host fora de `RepoSettings.allowed_hosts` (do `create_app`; a lista vazia recusa tudo) com
`invalid_value`, e os horários sem deslocamento com `invalid_format`, sem eco. Uma referência `bank:<nome>`
(um banco enviado) não passa pela lista de hosts e leva a versão do banco (64 hexadecimais) no `commit`; a
forma `bank:` fora do padrão é `invalid_format`, e o preparo não confere se o banco existe (a validação do
disparo confere). `repo`, `qid`, a prévia,
os motivos do motor e as mensagens livres saem redigidos com as identidades do momento.

Relatórios: `reports.get` e `reports.list_versions` (404 do oferecimento → 404 do
disparo → `invalid_dispatch_state` → 404 da versão, só em `reports.get`) e `reports.resend_summary` (404 →
404 → `invalid_state` → `invalid_dispatch_state`; sem corpo). A consulta de `reports.get` é
estrita (`format` = `json`·`csv`; `version` = `^([1-9][0-9]{0,9}|latest)$`, até 2147483647); os corpos
só embrulham o `RenderedReport` da porta (`_report_dto`, `_report_csv`), e os erros saem sempre
no envelope JSON, também com `format=csv`.

Expurgo: `offerings.prepare_purge` (sem corpo) e `offerings.purge` (`{confirmation_code}`),
com `people:write`; nas regras, 404 do oferecimento → (confirmação: já expurgado devolve o resultado gravado) →
`invalid_state` → `purge_pending` (com `pending`) → `invalid_confirmation` → `purge_changed`. As respostas
levam só contagens, estados e horários. `system.doctor`: sem consulta e sem corpo, com
`offerings:read`, só leitura numa transação (`in_transaction`), sem evento e sem chamada externa; os limites são
os do `DoctorSettings` dado a `create_app`.

Banco de questões local: `banks.upload` (`POST /v1/question-banks`, com `offerings:write`) e `banks.list`
(`GET /v1/question-banks`, com `offerings:read`; `after`, `limit` e `name` na consulta). O envio é a única rota
com a permissão antes da forma do corpo, que ela lê em fluxo, com teto, nesta ordem: `X-CQ-Client` e consulta
(400) → permissão (403, sem ler o corpo) → `Content-Length` (não numérico → `{body, invalid_format}`; acima do
teto → `{body, too_long}`, sem ler) → a vaga de envio (um por vez por processo; o segundo espera sem ler) → a
leitura com o contador (`{body, too_long}` no primeiro pedaço acima do teto; a desconexão do cliente encerra o
pedido com um 400, sem 500 e sem registro de exceção) → numa thread do pool: o JSON, os campos na ordem do
esquema, o base64 estrito e o teto do pacote, o conteúdo do pacote com os tetos fixos da api (um só campo
`archive`), a versão recalculada (`{version, mismatch}`) e a gravação idempotente. Sucesso: 200 com `created`
(o reenvio devolve o primeiro envio). O nome e o `repo` saem redigidos no envio e na listagem.

As rotas não recebem parâmetros declarados (só o `Request`), para que o FastAPI não valide nada por
conta própria nem anuncie o 422; o OpenAPI de parâmetros e corpos vem de `operations` (ver `app`).
O trabalho com o banco roda numa thread do pool (`ApiContext.call`), no núcleo síncrono.
"""

from __future__ import annotations

import asyncio
import binascii
import dataclasses
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response

import cq_server
from cq_server import (
    banks,
    bots,
    digests,
    dispatches,
    doctor,
    events,
    freemessages,
    offerings,
    people,
    purge,
    questionrefs,
    reports,
    students,
    team,
    tokens,
)
from cq_server.api import schemas
from cq_server.api.context import ApiContext, in_transaction
from cq_server.api.errors import ApiError, FieldIssue
from cq_server.api.middleware import principal_of
from cq_server.api.operations import BY_ID
from cq_server.api.validation import INVALID, MAX_CURSOR, RequestChecker
from cq_server.events import DEFAULT_LIMIT, MAX_LIMIT, EventType
from cq_server.migrate import EXPECTED_VERSION, current_version
from cq_server.permissions import Permission
from cq_server.redaction import redact_operator_text
from cq_server.reportrender import CsvReportRenderer, JsonReportRenderer
from cq_server.timezones import TimezoneError, validate_timezone

MAX_PSEUDONYMS = 100
EVENT_TYPES = frozenset(t.value for t in EventType)

# Teto de linhas por importação e a
# validade padrão dos convites de aluno, em horas.
MAX_ROSTER_ROWS = 1000
DEFAULT_INVITE_VALID_HOURS = 168

# U+0000 e substitutos UTF-16 isolados (o código de confirmação com eles é `invalid_format`).
_UNSTORABLE = re.compile(r"[\x00\ud800-\udfff]")
CONFIRMATION_MAX_LENGTH = 64


def _require(principal: tokens.Principal, permission: Permission) -> None:
    if not principal.has(permission.value):
        raise ApiError("forbidden", permission=permission.value)


def _rule_error(result: object) -> None:
    """Converte os resultados sentinela de regra do núcleo (inclusive os de disparo e o
    `GradingPending` do arquivamento) no erro do contrato."""
    if result is None:
        raise ApiError("not_found")
    if isinstance(result, offerings.InvalidState):
        raise ApiError("invalid_state", state=result.state)
    if isinstance(result, offerings.BotRequired):
        raise ApiError("bot_required")
    if isinstance(result, offerings.GradingPending):
        raise ApiError("grading_pending")
    if isinstance(result, offerings.InvalidConfirmation):
        raise ApiError("invalid_confirmation")
    if isinstance(result, dispatches.DispatchStateConflict):
        raise ApiError("invalid_dispatch_state")
    if isinstance(result, dispatches.WindowExpired):
        raise ApiError("window_expired")
    if isinstance(result, reports.ReportVersionNotFound):
        raise ApiError("not_found")


def _purge_error(result: object) -> None:
    """As sentinelas do expurgo; as demais (404, `invalid_state`,
    `invalid_confirmation`) seguem `_rule_error`."""
    if isinstance(result, purge.PurgeBlocked):
        raise ApiError("purge_pending", pending=result.pending)
    if isinstance(result, purge.PurgeChanged):
        raise ApiError("purge_changed")
    _rule_error(result)


def _with_identities(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Uma leitura do núcleo numa transação, com as identidades do momento para a redação na saída."""

    def wrapper(conn: psycopg.Connection, /, *args: Any, **kwargs: Any) -> Any:
        with conn.transaction():
            return fn(conn, *args, identities=events.load_known_identities(conn), **kwargs)

    return wrapper


# --- DTOs de resposta ----------------------------------------------------------------------------


def _person_dto(person: people.Person) -> schemas.Person:
    return schemas.Person(pseudo_id=str(person.pseudo_id), alias=person.alias,
                          created_at=schemas.timestamp(person.created_at))


def _token_dto(token: tokens.Token) -> schemas.Token:
    return schemas.Token(
        token_id=str(token.token_id), label=token.label, permissions=list(token.permissions),
        created_at=schemas.timestamp(token.created_at),
        revoked_at=schemas.timestamp(token.revoked_at) if token.revoked_at is not None else None,
    )


def _event_dto(event: events.StoredEvent) -> Any:
    return schemas.EVENT_ADAPTER.validate_python({
        "seq": event.seq, "type": event.type, "occurred_at": schemas.timestamp(event.occurred_at),
        "actor_token_id": event.actor_token_id, "data": event.data,
    })


def _optional_timestamp(value: Any) -> str | None:
    return None if value is None else schemas.timestamp(value)


def _offering_dto(offering: offerings.Offering) -> schemas.Offering:
    bot = offering.bot
    return schemas.Offering(
        offering_id=str(offering.offering_id), code=offering.code, course=offering.course,
        term=offering.term, timezone=offering.timezone,
        response_window_minutes=offering.response_window_minutes, state=offering.state,
        created_at=schemas.timestamp(offering.created_at),
        activated_at=_optional_timestamp(offering.activated_at),
        closed_at=_optional_timestamp(offering.closed_at),
        archived_at=_optional_timestamp(offering.archived_at),
        bot=None if bot is None else schemas.BotSummary(
            username=bot.username, telegram_bot_id=bot.telegram_bot_id, state=bot.state,
            profile_state=bot.profile_state),
        free_messages=offering.free_messages, contact_email_set=offering.contact_email_set,
        purged_at=_optional_timestamp(offering.purged_at),
    )


def _offering_ref(result: Any) -> schemas.OfferingRef:
    """O `OfferingRef` das respostas da lista de alunos e dos convites, de um resultado do núcleo com
    `offering_id` e `code`."""
    return schemas.OfferingRef(offering_id=str(result.offering_id), code=result.code)


def _bot_dto(bot: bots.Bot) -> schemas.Bot:
    lease = None
    if bot.lease_owner is not None and bot.lease_until is not None:
        lease = schemas.Lease(worker_id=bot.lease_owner, until=schemas.timestamp(bot.lease_until))
    problem = None
    if bot.problem is not None:
        problem = schemas.BotProblem(code=bot.problem, message=bots.PROBLEM_MESSAGES[bot.problem])
    return schemas.Bot(
        offering=schemas.OfferingRef(offering_id=str(bot.offering_id), code=bot.code),
        username=bot.username, telegram_bot_id=bot.telegram_bot_id, state=bot.state,
        profile_state=bot.profile_state, registered_at=schemas.timestamp(bot.registered_at),
        token_replaced_at=_optional_timestamp(bot.token_replaced_at), lease=lease,
        last_poll_ok_at=_optional_timestamp(bot.last_poll_ok_at), problem=problem,
    )


# --- corpos de pedido (validados na precedência do envelope de erro) ------------------------------


def _person_registration(check: RequestChecker,
                         obj: dict[str, Any]) -> tuple[str, str | None, str | None] | None:
    full_name: str | None = None
    name = check.required_text(obj, "name")
    if name is not None:
        try:
            full_name = people.normalize_full_name(name)
        except people.InvalidIdentity as exc:
            check.add("name", exc.reason)

    ra = check.optional_text(obj, "ra")
    if isinstance(ra, str):
        try:
            people.validate_ra(ra)
        except people.InvalidIdentity as exc:
            check.add("ra", exc.reason)
            ra = INVALID

    email = check.optional_text(obj, "email")
    if isinstance(email, str):
        try:
            email = people.validate_email(email).address
        except people.InvalidIdentity as exc:
            check.add("email", exc.reason)
            email = INVALID

    check.body_keys(obj, ("name", "ra", "email"))
    if full_name is None or ra is INVALID or email is INVALID:
        return None
    return full_name, ra, email


def _pseudonym_list(check: RequestChecker, obj: dict[str, Any]) -> list[str] | None:
    """`pseudonyms`: lista de 1 a 100 itens, contados com os repetidos (fora do limite, os itens não
    são validados), e um `pseudonyms[i]` `invalid_format` por item fora do formato (também no
    reenvio)."""
    items = check.required_list(obj, "pseudonyms", min_items=1, max_items=MAX_PSEUDONYMS)
    if items is None:
        return None
    bad = [index for index, item in enumerate(items) if not people.is_pseudonym(item)]
    for index in bad:
        check.add(f"pseudonyms[{index}]", "invalid_format")
    return None if bad else items


def _reveal_request(check: RequestChecker, obj: dict[str, Any]) -> tuple[list[str], str] | None:
    refs = _pseudonym_list(check, obj)

    reason: str | None = None
    text = check.required_text(obj, "reason")
    if text is not None:
        try:
            reason = people.normalize_reason(text)
        except people.InvalidIdentity as exc:
            check.add("reason", exc.reason)

    check.body_keys(obj, ("pseudonyms", "reason"))
    if refs is None or reason is None:
        return None
    return refs, reason


def _token_creation(check: RequestChecker, obj: dict[str, Any]) -> tuple[str, list[str]] | None:
    label: str | None = None
    text = check.required_text(obj, "label")
    if text is not None:
        try:
            label = tokens.normalize_label(text)
        except tokens.InvalidLabel as exc:
            check.add("label", exc.reason)

    permissions: list[str] | None = None
    items = check.required_list(obj, "permissions", min_items=tokens.MIN_PERMISSIONS,
                                max_items=tokens.MAX_PERMISSIONS)
    if items is not None:
        seen: set[str] = set()
        ok = True
        for index, item in enumerate(items):
            if not isinstance(item, str):
                check.add(f"permissions[{index}]", "invalid_format")
            elif item not in tokens.ALL_PERMISSIONS:
                check.add(f"permissions[{index}]", "invalid_value")
            elif item in seen:
                check.add(f"permissions[{index}]", "duplicate")
            else:
                seen.add(item)
                continue
            ok = False
        if ok:
            permissions = items

    check.body_keys(obj, ("label", "permissions"))
    if label is None or permissions is None:
        return None
    return label, permissions


# --- corpos de pedido de oferecimentos, bots e equipe ---------------------------------------------


def _optional_int(check: RequestChecker, obj: dict[str, Any], name: str, *, default: int,
                  validate: Any) -> Any:
    """Inteiro opcional: só a chave ausente vale `default`; qualquer valor presente, inclusive `null`,
    passa por `validate`, que levanta `ValueError(motivo)` (o `null` é `invalid_format`: o esquema do
    pedido não é anulável)."""
    if name not in obj:
        return default
    value = obj[name]
    try:
        return validate(value)
    except ValueError as exc:
        check.add(name, str(exc))
        return INVALID


def _offering_creation(check: RequestChecker, obj: dict[str, Any]) -> dict[str, Any] | None:
    parsed: dict[str, Any] = {}
    ok = True
    for name, normalize in (("code", offerings.validate_code), ("course", offerings.normalize_course),
                            ("term", offerings.normalize_term)):
        text = check.required_text(obj, name)
        if text is None:
            ok = False
            continue
        try:
            parsed[name] = normalize(text)
        except offerings.InvalidOffering as exc:
            check.add(name, exc.reason)
            ok = False

    if obj.get("timezone") is None:
        check.add("timezone", "missing")
        ok = False
    else:
        try:
            parsed["timezone"] = validate_timezone(obj["timezone"])
        except TimezoneError as exc:
            check.add("timezone", exc.reason)
            ok = False

    def window(value: object) -> int:
        try:
            return offerings.validate_response_window(value)
        except offerings.InvalidOffering as exc:
            raise ValueError(exc.reason) from None

    minutes = _optional_int(check, obj, "response_window_minutes",
                            default=offerings.DEFAULT_RESPONSE_WINDOW, validate=window)
    # Os dois campos das mensagens livres, opcionais, conferidos depois dos
    # demais, na ordem do esquema; omitidos: desligado e sem contato.
    free_messages = _optional_bool(check, obj, "free_messages", default=False)
    contact_email = _contact_email(check, obj, default=None)
    check.body_keys(obj, ("code", "course", "term", "timezone", "response_window_minutes",
                          "free_messages", "contact_email"))
    if not ok or minutes is INVALID or free_messages is INVALID or contact_email is INVALID:
        return None
    parsed["response_window_minutes"] = minutes
    parsed["free_messages"] = free_messages
    parsed["contact_email"] = contact_email
    return parsed


# --- configuração de mensagens livres ------------------------------------------------------------


def _optional_bool(check: RequestChecker, obj: dict[str, Any], name: str, *, default: Any) -> Any:
    """Booleano opcional: só a chave ausente vale `default`; qualquer outro valor que não seja booleano,
    inclusive `null`, é `invalid_format` (o esquema do pedido não é anulável)."""
    if name not in obj:
        return default
    value = obj[name]
    if not isinstance(value, bool):
        check.add(name, "invalid_format")
        return INVALID
    return value


def _contact_email(check: RequestChecker, obj: dict[str, Any], *, default: Any) -> Any:
    """`contact_email`: chave ausente → `default`; `null` → `None` (sem contato, ou remover); não texto →
    `invalid_format`; texto → `people.validate_email` (aparado; `too_short`, `too_long`,
    `invalid_format`, sem eco). Devolve o `Email.address` validado."""
    if "contact_email" not in obj:
        return default
    value = check.optional_text(obj, "contact_email")
    if isinstance(value, str):
        try:
            return people.validate_email(value).address
        except people.InvalidIdentity as exc:
            check.add("contact_email", exc.reason)
            return INVALID
    return value


def _settings_request(check: RequestChecker, obj: dict[str, Any]) -> dict[str, Any] | None:
    """Pedido de `offerings.update_settings`: `{free_messages?, contact_email?}`,
    com pelo menos um campo (`{}` → `{body, too_few}`); os campos omitidos não vão ao núcleo."""
    if not obj:
        check.add("body", "too_few")
    free_messages = _optional_bool(check, obj, "free_messages", default=offerings.UNSET)
    contact_email = _contact_email(check, obj, default=offerings.UNSET)
    check.body_keys(obj, ("free_messages", "contact_email"))
    if not obj or free_messages is INVALID or contact_email is INVALID:
        return None
    parsed: dict[str, Any] = {}
    if free_messages is not offerings.UNSET:
        parsed["free_messages"] = free_messages
    if contact_email is not offerings.UNSET:
        parsed["contact_email"] = contact_email
    return parsed


def _confirmation_request(check: RequestChecker, obj: dict[str, Any]) -> str | None:
    """`{confirmation_code}` de 1 a 64 caracteres (`offerings.archive` e `invites.send`):
    vazio → `too_short`; acima de 64 → `too_long`; U+0000 ou substituto isolado → `invalid_format`."""
    code = check.required_text(obj, "confirmation_code")
    if code is not None:
        if not code:
            check.add("confirmation_code", "too_short")
            code = None
        elif len(code) > CONFIRMATION_MAX_LENGTH:
            check.add("confirmation_code", "too_long")
            code = None
        elif _UNSTORABLE.search(code):
            check.add("confirmation_code", "invalid_format")
            code = None
    check.body_keys(obj, ("confirmation_code",))
    return code


def _bot_registration(check: RequestChecker, obj: dict[str, Any]) -> str | None:
    token = check.required_text(obj, "token")
    if token is not None:
        try:
            bots.validate_token(token)
        except ValueError as exc:
            check.add("token", str(exc))
            token = None
    check.body_keys(obj, ("token",))
    return token


def _team_link_request(check: RequestChecker, obj: dict[str, Any]) -> tuple[str, int] | None:
    person = check.required_text(obj, "person")
    if person is not None and not people.is_pseudonym(person):
        check.add("person", "invalid_format")
        person = None
    hours = _optional_int(check, obj, "valid_hours", default=team.DEFAULT_VALID_HOURS,
                          validate=team.validate_valid_hours)
    check.body_keys(obj, ("person", "valid_hours"))
    if person is None or hours is INVALID:
        return None
    return person, hours


# --- corpos de pedido da lista de alunos e dos convites --------------------------------------------


def _roster_request(check: RequestChecker, obj: dict[str, Any]) -> list[students.RosterRow] | None:
    """`{rows}` de `students.import`: lista de 1 a 1.000 itens (fora do
    limite, os itens não são validados); dentro dela, um motivo por item, em `rows[i]` e em ordem de
    índice, pela validação do núcleo (`students.check_rows`), sem o valor recebido."""
    rows: list[students.RosterRow] | None = None
    items = check.required_list(obj, "rows", min_items=1, max_items=MAX_ROSTER_ROWS)
    if items is not None:
        valid, issues = students.check_rows(items)
        for index, reason in issues:
            check.add(f"rows[{index}]", reason)
        if not issues:
            rows = valid
    check.body_keys(obj, ("rows",))
    return rows


def _invite_valid_hours(check: RequestChecker, obj: dict[str, Any]) -> Any:
    """`valid_hours` opcional do preparo e do reenvio: inteiro de 1 a
    720, padrão 168; fora do intervalo → `out_of_range`; não inteiro ou booleano → `invalid_format`
    (as regras do link de equipe). Devolve `INVALID` com o problema registrado."""
    return _optional_int(check, obj, "valid_hours", default=DEFAULT_INVITE_VALID_HOURS,
                         validate=team.validate_valid_hours)


def _prepare_request(check: RequestChecker, obj: dict[str, Any]) -> int | None:
    hours = _invite_valid_hours(check, obj)
    check.body_keys(obj, ("valid_hours",))
    return None if hours is INVALID else hours


def _resend_request(check: RequestChecker, obj: dict[str, Any]) -> tuple[list[str], int] | None:
    refs = _pseudonym_list(check, obj)
    hours = _invite_valid_hours(check, obj)
    check.body_keys(obj, ("pseudonyms", "valid_hours"))
    if refs is None or hours is INVALID:
        return None
    return refs, hours


# --- corpos de pedido e DTOs dos disparos -------------------------------------------------------

# Horário ISO 8601 com deslocamento explícito.
_OFFSET_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
MAX_OPENS_AHEAD = timedelta(days=dispatches.MAX_OPENS_AHEAD_DAYS)
# `X-CQ-Client` → a origem declarada padrão.
_CLIENT_ORIGIN = {"cli": "cli", "mcp": "mcp", "other": "api"}
# `validate_ref_repo` → o motivo do contrato (a forma `bank:` fora do padrão é um formato inválido).
_REPO_REASONS = {"repo_length": "too_long", "repo_syntax": "invalid_format", "repo_dot_segment": "invalid_format",
                 "host_not_allowed": "invalid_value", "bank_name": "invalid_format"}
# A versão de um banco enviado no papel do commit.
_BANK_COMMIT = re.compile(r"[0-9a-f]{64}")


def _offset_time(check: RequestChecker, obj: dict[str, Any], name: str) -> Any:
    """Horário opcional com deslocamento: ausente → `None`; fora da forma (inclusive `null` ou sem
    fuso) → `INVALID` com `invalid_format`; senão o instante em UTC."""
    if name not in obj:
        return None
    value = obj[name]
    if not isinstance(value, str) or _OFFSET_TIME.fullmatch(value) is None:
        check.add(name, "invalid_format")
        return INVALID
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        check.add(name, "invalid_format")
        return INVALID
    if moment.tzinfo is None or moment.utcoffset() is None:  # pragma: no cover - a forma exige o fuso
        check.add(name, "invalid_format")
        return INVALID
    return moment.astimezone(timezone.utc)


def _dispatch_prepare_request(check: RequestChecker, obj: dict[str, Any], *, now: datetime,
                              allowed_hosts: frozenset[str], client: str) -> dispatches.DispatchRequest | None:
    """`{repo, commit, qid, opens_at?, closes_at?, origin?}`: um
    motivo por campo, na ordem do esquema, sem eco; os horários relativos a `now` (abertura futura e até
    90 dias; janela de 5 min a 30 dias, e no imediato `closes_at - now` no mesmo intervalo). O `repo` pela
    forma da referência (`https` na lista de hosts, ou `bank:<nome>` sem ela); com `bank:`, o `commit` só com
    64 hexadecimais. Nenhuma consulta ao banco de dados."""
    ok = True
    repo = check.required_text(obj, "repo")
    if repo is not None:
        if not repo:
            check.add("repo", "too_short")
            repo = None
        else:
            problem = questionrefs.validate_ref_repo(repo, allowed_hosts)
            if problem is not None:
                check.add("repo", _REPO_REASONS[problem])
                repo = None
    # Numa referência de banco enviado, o commit é a versão do banco (só 64 hexadecimais).
    commit_form = (_BANK_COMMIT if repo is not None and questionrefs.BANK_REPO.fullmatch(repo) is not None
                   else questionrefs.COMMIT)
    commit = check.required_text(obj, "commit")
    if commit is not None and commit_form.fullmatch(commit) is None:
        check.add("commit", "invalid_format")
        commit = None
    qid = check.required_text(obj, "qid")
    if qid is not None:
        if len(qid) > questionrefs.QID_MAX_LENGTH:
            check.add("qid", "too_long")
            qid = None
        elif questionrefs.QID.fullmatch(qid) is None:
            check.add("qid", "invalid_format")
            qid = None
    opens_at = _offset_time(check, obj, "opens_at")
    if isinstance(opens_at, datetime) and not now < opens_at <= now + MAX_OPENS_AHEAD:
        check.add("opens_at", "out_of_range")
        opens_at = INVALID
    closes_at = _offset_time(check, obj, "closes_at")
    if isinstance(closes_at, datetime) and opens_at is not INVALID:
        start = now if opens_at is None else opens_at
        if not dispatches.MIN_WINDOW <= closes_at - start <= dispatches.MAX_WINDOW:
            check.add("closes_at", "out_of_range")
            closes_at = INVALID
    origin = _CLIENT_ORIGIN.get(client, "api")
    if "origin" in obj:
        value = obj["origin"]
        if not isinstance(value, str) or value not in {o.value for o in dispatches.Origin}:
            check.add("origin", "invalid_value")
            ok = False
        else:
            origin = value
    check.body_keys(obj, ("repo", "commit", "qid", "opens_at", "closes_at", "origin"))
    if (not ok or repo is None or commit is None or qid is None or opens_at is INVALID
            or closes_at is INVALID):
        return None
    return dispatches.DispatchRequest(repo=repo, commit=commit, qid=qid, opens_at=opens_at, closes_at=closes_at,
                                      origin=origin)


def _is_dispatch_id(value: object) -> bool:
    return isinstance(value, str) and people.PSEUDO_ID_PATTERN.fullmatch(value) is not None


def _dispatch_summary_dto(summary: dispatches.DispatchSummary) -> schemas.DispatchSummary:
    return schemas.DispatchSummary(**_summary_fields(summary))


def _summary_fields(summary: dispatches.DispatchSummary) -> dict[str, Any]:
    rejection = None
    if summary.rejection is not None:
        rejection = schemas.DispatchRejection(
            reason=summary.rejection.reason, message=dispatches.REJECTION_MESSAGES[summary.rejection.reason],
            details=list(summary.rejection.details))
    counts = summary.deliveries
    return {
        "dispatch_id": str(summary.dispatch_id),
        "question": schemas.DispatchQuestion(question_ref_id=str(summary.question_ref_id), repo=summary.repo,
                                             commit=summary.commit, qid=summary.qid),
        "state": summary.state, "confirmed": summary.confirmed_at is not None, "origin": summary.origin,
        "opens_at": _optional_timestamp(summary.opens_at), "closes_at": _optional_timestamp(summary.closes_at),
        "created_at": schemas.timestamp(summary.created_at),
        "validated_at": _optional_timestamp(summary.validated_at),
        "confirmed_at": _optional_timestamp(summary.confirmed_at),
        "rejected_at": _optional_timestamp(summary.rejected_at),
        "opened_at": _optional_timestamp(summary.opened_at), "closed_at": _optional_timestamp(summary.closed_at),
        "cancelled_at": _optional_timestamp(summary.cancelled_at), "rejection": rejection,
        "cancel_reason": summary.cancel_reason, "target_count": summary.target_count,
        "deliveries": schemas.DispatchDeliveries(
            pending=counts.pending, delivered=counts.delivered, not_delivered=counts.not_delivered,
            answered=counts.answered, dont_know=counts.dont_know, no_answer=counts.no_answer,
            answered_late=counts.answered_late, question_failed=counts.question_failed,
            grading_pending=counts.grading_pending),
    }


def _dispatch_dto(dispatch: dispatches.Dispatch) -> schemas.Dispatch:
    preview = None
    if dispatch.preview is not None:
        p = dispatch.preview
        preview = schemas.DispatchPreview(
            title=p.get("title"), statement=p.get("statement", ""),
            fields=[schemas.PreviewField(type=f["type"], label=f.get("label"), suffix=f.get("suffix"),
                                         hint=f.get("hint", "")) for f in p.get("fields", [])],
            target_count=p.get("target_count", 0),
            warnings=[schemas.PreviewWarning(kind=w["kind"], message=w.get("message", ""))
                      for w in p.get("warnings", [])])
    return schemas.Dispatch(offering=schemas.OfferingRef(offering_id=str(dispatch.offering_id), code=dispatch.code),
                            timezone=dispatch.timezone, preview=preview, **_summary_fields(dispatch.summary))


def _free_message_dto(message: freemessages.FreeMessage) -> schemas.FreeMessage:
    return schemas.FreeMessage(pseudo_id=str(message.pseudo_id), alias=message.alias, text=message.text,
                               created_at=schemas.timestamp(message.created_at))


# --- relatório do disparo -------------------------------------------------------------------------

REPORT_FORMATS = ("json", "csv")
# A forma de `version` e o teto do `integer` da coluna.
_REPORT_VERSION = re.compile(r"[1-9][0-9]{0,9}|latest")
MAX_REPORT_VERSION = 2_147_483_647


def _report_format(check: RequestChecker, params: Any) -> str | None:
    """`format`: ausente → `json`; fora de `json`/`csv` ou repetido → `invalid_value`, sem eco."""
    values = params.getlist("format")
    if not values:
        return "json"
    if len(values) > 1 or values[0] not in REPORT_FORMATS:
        check.add("format", "invalid_value")
        return None
    return values[0]


def _report_version(check: RequestChecker, params: Any) -> int | str | None:
    """`version`: ausente → `None` (o cálculo no momento); fora da forma ou repetido →
    `invalid_format`; acima de 2147483647 → `out_of_range`; senão o número ou `latest`."""
    values = params.getlist("version")
    if not values:
        return None
    if len(values) > 1 or _REPORT_VERSION.fullmatch(values[0]) is None:
        check.add("version", "invalid_format")
        return INVALID
    if values[0] == "latest":
        return "latest"
    number = int(values[0])
    if number > MAX_REPORT_VERSION:
        check.add("version", "out_of_range")
        return INVALID
    return number


def _report_dto(report: reports.Report) -> Response:
    """O 200 `application/json`: o corpo do `JsonReportRenderer`, conferido contra o esquema estrito
    (falha fechada)."""
    rendered = JsonReportRenderer().render(report)
    schemas.Report.model_validate_json(rendered.body)
    return Response(content=rendered.body.encode("utf-8"), media_type=rendered.media_type)


def _report_csv(report: reports.Report) -> Response:
    """O 200 `text/csv`: o corpo do `CsvReportRenderer` sobre o `Report` do esquema estrito."""
    schemas.Report.model_validate(reports.report_to_dict(report))
    rendered = CsvReportRenderer().render(report)
    return Response(content=rendered.body.encode("utf-8"), media_type=rendered.media_type)


def _frozen_dto(frozen: reports.FrozenRef) -> schemas.FrozenRef:
    return schemas.FrozenRef(version=frozen.version, trigger=frozen.trigger,
                             frozen_at=schemas.timestamp(frozen.frozen_at))


def _versions_dto(page: reports.ReportVersionsPage) -> schemas.ReportVersionsPage:
    return schemas.ReportVersionsPage(
        offering=schemas.OfferingRef(offering_id=str(page.offering_id), code=page.code),
        dispatch_id=str(page.dispatch_id), items=[_frozen_dto(item) for item in page.items],
        next_cursor=page.next_cursor)


def _resend_dto(result: reports.ResendResult) -> schemas.ReportResend:
    return schemas.ReportResend(
        offering=schemas.OfferingRef(offering_id=str(result.offering_id), code=result.code),
        dispatch_id=str(result.dispatch_id), frozen=_frozen_dto(result.frozen))


def _digest_dto(view: digests.DigestView) -> schemas.OfferingDigest:
    return schemas.OfferingDigest(
        offering=schemas.OfferingRef(offering_id=str(view.offering_id), code=view.code), timezone=view.timezone,
        enabled=view.enabled, local_time=view.local_time)


def _digest_request(check: RequestChecker, obj: dict[str, Any]) -> dict[str, Any] | None:
    """Pedido de `offerings.update_digest`: `{enabled?, local_time?}`, fechado, com
    pelo menos um campo (`{}` → `{body, too_few}`); `enabled` não booleano (inclusive `null`) e `local_time`
    fora de `HH:MM` (00:00 a 23:59; inclusive `null` e não texto) → `invalid_format`. Os campos omitidos
    não vão ao núcleo (`None` = não muda)."""
    if not obj:
        check.add("body", "too_few")
    enabled = _optional_bool(check, obj, "enabled", default=None)
    local_time: Any = None
    if "local_time" in obj:
        local_time = obj["local_time"]
        if not isinstance(local_time, str) or not digests.LOCAL_TIME.fullmatch(local_time):
            check.add("local_time", "invalid_format")
            local_time = INVALID
    check.body_keys(obj, ("enabled", "local_time"))
    if not obj or enabled is INVALID or local_time is INVALID:
        return None
    return {"enabled": enabled, "local_time": local_time}


# --- banco de questões local ---------------------------------------------------------------------

# O `Content-Length` aceito: só dígitos decimais (sem sinal, espaço nem notação científica).
_CONTENT_LENGTH = re.compile(r"[0-9]+")
# Mais dígitos que isto já passa de qualquer teto do corpo (e o `int` não precisa ver o texto).
_MAX_LENGTH_DIGITS = 19
# A versão do banco (64 hexadecimais minúsculos) e o commit de origem (40 ou 64).
_BANK_VERSION = re.compile(r"[0-9a-f]{64}")
_BANK_GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# O texto base64 do pacote: o menor (um grupo de 4) e o maior (o base64 do teto do pacote, com preenchimento).
_MIN_ARCHIVE_TEXT = 4
_MAX_ARCHIVE_TEXT = 4 * ((banks.MAX_ARCHIVE_BYTES + 2) // 3)
_BANK_UPLOAD_FIELDS = ("name", "version", "source", "git_commit", "archive")


def _is_bank_name(value: str) -> bool:
    return banks.BANK_NAME.fullmatch(value) is not None


def _content_length(check: RequestChecker, request: Request) -> None:
    """O `Content-Length` do envio, conferido antes de ler o corpo: ausente vale (o contador da leitura
    confere); repetido ou não numérico → `{body, invalid_format}`; acima do teto do corpo → `{body, too_long}`."""
    values = request.headers.getlist("content-length")
    if not values:
        return
    if len(values) != 1 or _CONTENT_LENGTH.fullmatch(values[0]) is None:
        check.add("body", "invalid_format")
        return
    digits = values[0].lstrip("0")
    if len(digits) > _MAX_LENGTH_DIGITS or int(digits or "0") > banks.MAX_BODY_BYTES:
        check.add("body", "too_long")


def _archive_bytes(check: RequestChecker, text: str) -> bytes | None:
    """O pacote em base64 padrão, estrito: só o alfabeto padrão, com o preenchimento só no fim, em grupos de 4
    e na forma canônica (os bits que sobram no último grupo zerados); senão `invalid_format`. Acima do teto do
    pacote (pelo tamanho do texto, antes de decodificar, ou pelo decodificado) → `too_long`."""
    if len(text) > _MAX_ARCHIVE_TEXT:
        check.add("archive", "too_long")
        return None
    if len(text) < _MIN_ARCHIVE_TEXT:
        check.add("archive", "invalid_format")
        return None
    try:
        data = binascii.a2b_base64(text, strict_mode=True)
        # A forma canônica do último grupo (o decodificador aceita bits de sobra não nulos).
        canonical = binascii.b2a_base64(binascii.a2b_base64(text[-4:], strict_mode=True), newline=False)
    except (binascii.Error, ValueError):
        check.add("archive", "invalid_format")
        return None
    if canonical.decode("ascii") != text[-4:]:
        check.add("archive", "invalid_format")
        return None
    if len(data) > banks.MAX_ARCHIVE_BYTES:
        check.add("archive", "too_long")
        return None
    return data


def _bank_upload_request(check: RequestChecker, obj: dict[str, Any]) -> tuple[str, str, str, str | None,
                                                                              bytes] | None:
    """Pedido de `banks.upload`: `{name, version, source, git_commit?, archive}`, fechado, com um motivo por
    campo na ordem do esquema, sem eco. Um campo ausente é `missing`; presente com o tipo errado (inclusive
    `null`), `invalid_format` (a origem: `invalid_value`). `git_commit` é obrigatório e não nulo na origem `git`
    e ausente ou nulo na `folder` (um valor na forma certa é `invalid_value`); com a origem inválida, só a forma
    dele é conferida."""
    name: str | None = None
    if "name" not in obj:
        check.add("name", "missing")
    elif not isinstance(obj["name"], str):
        check.add("name", "invalid_format")
    elif len(obj["name"]) < 3:
        check.add("name", "too_short")
    elif len(obj["name"]) > 40:
        check.add("name", "too_long")
    elif not _is_bank_name(obj["name"]):
        check.add("name", "invalid_format")
    else:
        name = obj["name"]

    version: str | None = None
    if "version" not in obj:
        check.add("version", "missing")
    elif not isinstance(obj["version"], str) or _BANK_VERSION.fullmatch(obj["version"]) is None:
        check.add("version", "invalid_format")
    else:
        version = obj["version"]

    source: str | None = None
    if "source" not in obj:
        check.add("source", "missing")
    elif not isinstance(obj["source"], str) or obj["source"] not in banks.SOURCES:
        check.add("source", "invalid_value")
    else:
        source = obj["source"]

    git_commit: Any = obj.get("git_commit")
    commit_ok = True
    if git_commit is None:
        if source == "git":
            check.add("git_commit", "missing")
            commit_ok = False
    elif not isinstance(git_commit, str) or _BANK_GIT_COMMIT.fullmatch(git_commit) is None:
        check.add("git_commit", "invalid_format")
        commit_ok = False
    elif source == "folder":
        check.add("git_commit", "invalid_value")
        commit_ok = False

    archive: bytes | None = None
    if "archive" not in obj:
        check.add("archive", "missing")
    elif not isinstance(obj["archive"], str):
        check.add("archive", "invalid_format")
    else:
        archive = _archive_bytes(check, obj["archive"])

    check.body_keys(obj, _BANK_UPLOAD_FIELDS)
    if name is None or version is None or source is None or not commit_ok or archive is None:
        return None
    return name, version, source, git_commit, archive


def _bank_version_dto(stored: banks.BankVersion) -> dict[str, Any]:
    return {"name": stored.name, "version": stored.version, "repo": stored.repo, "source": stored.source,
            "git_commit": stored.git_commit, "file_count": stored.file_count, "byte_count": stored.byte_count,
            "question_count": stored.question_count, "created_at": schemas.timestamp(stored.created_at)}


def _store_bank(conn: psycopg.Connection, /, *, name: str, version: str, source: str, git_commit: str | None,
                archive: bytes, summary: banks.ArchiveSummary, now: datetime, actor_token_id: str,
                secrets: tuple[str, ...]) -> tuple[banks.BankVersion, bool]:
    """A gravação (idempotente) e a versão guardada com o nome e o `repo` redigidos pelas identidades do momento,
    numa só transação. O valor gravado fica literal."""
    with conn.transaction():
        stored, created = banks.ensure_bank(conn, name=name, version=version, source=source,
                                            git_commit=git_commit, archive=archive, summary=summary, now=now,
                                            actor_token_id=actor_token_id)
        identities = events.load_known_identities(conn)
    shown = dataclasses.replace(stored, name=redact_operator_text(stored.name, identities, secrets=secrets),
                                repo=redact_operator_text(stored.repo, identities, secrets=secrets))
    return shown, created


def _bank_upload(ctx: ApiContext, raw: bytearray, *, actor_token_id: str) -> tuple[banks.BankVersion, bool]:
    """O envio depois da leitura do corpo, numa thread do pool: o JSON e os campos (um 400 com todos), o pacote
    (com os tetos fixos da api; a recusa é um 400 com um só campo), a versão recalculada e a gravação. Nenhuma
    recusa leva texto de exceção nem nada do pacote."""
    check = RequestChecker()
    obj = check.json_object(raw)  # o `bytearray` lido, sem cópia
    # O texto cru não serve mais: a memória do corpo volta antes de decodificar o pacote.
    raw.clear()
    parsed = _bank_upload_request(check, obj) if obj is not None else None
    del obj
    check.raise_if_invalid()
    assert parsed is not None
    name, version, source, git_commit, archive = parsed

    refusal: FieldIssue | None = None
    try:
        summary = banks.read_archive(archive, max_files=banks.MAX_FILES, max_bytes=banks.MAX_CONTENT_BYTES)
    except banks.BankArchiveError as exc:
        refusal = FieldIssue(exc.field, exc.reason)
    if refusal is not None:
        raise ApiError("invalid_request", fields=[refusal])
    if banks.bank_version(summary.entries) != version:
        raise ApiError("invalid_request", fields=[FieldIssue("version", "mismatch")])
    return ctx.run(_store_bank, name=name, version=version, source=source, git_commit=git_commit,
                   archive=archive, summary=summary, now=ctx.clock.now(), actor_token_id=actor_token_id,
                   secrets=ctx.secrets)


async def _read_bank_body(request: Request) -> bytearray:
    """O corpo do envio em fluxo, com o contador: recusa (`{body, too_long}`) no primeiro pedaço que passa do
    teto, sem ler o resto. A desconexão do cliente no meio encerra o pedido com um 400 sem registro de exceção
    (nada foi guardado, e a resposta não chega a ninguém)."""
    buffer = bytearray()
    too_long = disconnected = False
    stream = request.stream()
    try:
        async for chunk in stream:
            if len(buffer) + len(chunk) > banks.MAX_BODY_BYTES:
                too_long = True
                break
            buffer.extend(chunk)
    except ClientDisconnect:
        disconnected = True
    finally:
        await stream.aclose()
    if too_long or disconnected:
        buffer.clear()
        raise ApiError("invalid_request", fields=[FieldIssue("body", "too_long" if too_long else "invalid_format")])
    return buffer


# --- saúde ---------------------------------------------------------------------------------------


def health_status(database_url: str) -> schemas.HealthStatus:
    """`ok` se e só se o banco responde e o esquema está na versão esperada."""
    try:
        version = current_version(database_url)
        database = "ok"
    except (psycopg.Error, OSError):
        version = None
        database = "unreachable"
    ok = database == "ok" and version == EXPECTED_VERSION
    return schemas.HealthStatus(status="ok" if ok else "error", version=cq_server.__version__,
                                database=database, schema_version=version,
                                expected_schema_version=EXPECTED_VERSION)


# --- rotas ---------------------------------------------------------------------------------------


def register_routes(app: FastAPI, ctx: ApiContext) -> None:
    """Registra as rotas direto no app (sem `include_router`: as rotas ficam planas em `app.routes`,
    como o middleware e os testes de catálogo as percorrem)."""

    def route(op_id: str, *, response_model: Any, status_code: int = 200,
              extra_responses: dict[int | str, dict[str, Any]] | None = None) -> Any:
        op = BY_ID[op_id]
        responses: dict[int | str, dict[str, Any]] = {
            status: {"model": schemas.ErrorEnvelope, "description": description}
            for status, description in op.error_descriptions().items()
        }
        responses.update(extra_responses or {})

        def decorator(endpoint: Any) -> Any:
            app.add_api_route(op.path, endpoint, methods=[op.method], name=op.id,
                              operation_id=op.id, summary=op.summary,
                              response_model=response_model, status_code=status_code,
                              response_description="Sucesso.", responses=responses)
            return endpoint

        return decorator

    @route("system.health", response_model=schemas.HealthStatus,
           extra_responses={503: {"model": schemas.HealthStatus,
                                  "description": "Banco inalcançável ou esquema fora da versão."}})
    async def health() -> Response:
        # Fora de /v1: ignora consulta, corpo, X-CQ-Client e Authorization.
        status = await run_in_threadpool(health_status, ctx.settings.database_url)
        return JSONResponse(status.model_dump(mode="json"),
                            status_code=200 if status.status == "ok" else 503)

    @route("people.register", response_model=schemas.Person, status_code=201)
    async def register_person(request: Request) -> schemas.Person:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        registration = _person_registration(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert registration is not None
        _require(principal, Permission.PEOPLE_WRITE)

        full_name, ra, email = registration
        result = await ctx.call(people.register_person, full_name=full_name, ra=ra, email=email,
                                now=ctx.clock.now(), actor_token_id=principal.actor)
        if isinstance(result, people.Duplicate):
            raise ApiError("conflict", fields=[FieldIssue(name, "duplicate") for name in result.fields])
        return _person_dto(result)

    @route("people.list", response_model=schemas.PeoplePage)
    async def list_people(request: Request) -> schemas.PeoplePage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        params = request.query_params
        check.query_keys(params, ("after", "limit"))
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert after is not None and limit is not None
        _require(principal, Permission.PEOPLE_READ)

        page = await ctx.call(people.list_people, after=after, limit=limit)
        return schemas.PeoplePage(items=[_person_dto(p) for p in page.items],
                                  next_cursor=page.next_cursor)

    @route("people.get", response_model=schemas.Person)
    async def get_person(request: Request) -> schemas.Person:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", people.is_pseudonym)
        check.query_keys(request.query_params, ())
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.PEOPLE_READ)

        person = await ctx.call(people.get_person, ref)
        if person is None:
            raise ApiError("not_found")
        return _person_dto(person)

    @route("identity.reveal", response_model=schemas.Revelation)
    async def reveal_identities(request: Request) -> schemas.Revelation:
        principal = principal_of(request.scope)
        check = RequestChecker()
        client = check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _reveal_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert parsed is not None
        refs, reason = parsed

        now = ctx.clock.now()
        if not principal.has(Permission.IDENTITY_REVEAL.value):
            # A negativa fica auditada (sem a justificativa) antes do 403.
            await ctx.call(people.record_denied_reveal, refs, client=client, now=now,
                           actor_token_id=principal.actor)
            raise ApiError("forbidden", permission=Permission.IDENTITY_REVEAL.value)

        result = await ctx.call(people.reveal_identities, refs, reason=reason, client=client,
                                now=now, actor_token_id=principal.actor, secrets=ctx.secrets)
        return schemas.Revelation(
            people=[schemas.RevealedPerson(pseudo_id=str(p.pseudo_id), alias=p.alias,
                                           name=p.full_name, ra=p.ra, email=p.email)
                    for p in result.people],
            not_found=result.not_found,
        )

    @route("tokens.create", response_model=schemas.CreatedToken, status_code=201)
    async def create_token(request: Request) -> schemas.CreatedToken:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _token_creation(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert parsed is not None
        _require(principal, Permission.TOKENS_MANAGE)
        label, permissions = parsed
        missing = tokens.missing_permissions(principal, permissions)
        if missing:
            raise ApiError("forbidden", permission=missing[0])

        created = await ctx.call(tokens.create_token, label=label, permissions=permissions,
                                 now=ctx.clock.now(), actor_token_id=principal.actor,
                                 secrets=ctx.secrets)
        token = created.token
        return schemas.CreatedToken(
            token_id=str(token.token_id), label=token.label, permissions=list(token.permissions),
            created_at=schemas.timestamp(token.created_at), revoked_at=None, secret=created.secret,
        )

    @route("tokens.list", response_model=schemas.TokenList)
    async def list_tokens(request: Request) -> schemas.TokenList:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        check.reject_body(await request.body())
        check.raise_if_invalid()
        _require(principal, Permission.TOKENS_MANAGE)

        items = await ctx.call(tokens.list_tokens, secrets=ctx.secrets)
        return schemas.TokenList(items=[_token_dto(t) for t in items])

    @route("tokens.revoke", response_model=schemas.Token)
    async def revoke_token(request: Request) -> schemas.Token:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        token_id = check.path_param(request.path_params, "token_id", tokens.is_token_id)
        check.query_keys(request.query_params, ())
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert token_id is not None
        _require(principal, Permission.TOKENS_MANAGE)

        token = await ctx.call(tokens.revoke_token, uuid.UUID(token_id), now=ctx.clock.now(),
                               actor_token_id=principal.actor, secrets=ctx.secrets)
        if token is None:
            raise ApiError("not_found")
        return _token_dto(token)

    @route("events.list", response_model=schemas.EventsPage)
    async def list_events(request: Request) -> schemas.EventsPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        params = request.query_params
        check.query_keys(params, ("types", "after", "limit", "person"))
        # `person` opcional; sem ele, `types` é obrigatório e os
        # erros são os de `query_choices`; com ele, `types` pode faltar (a interseção quando vem).
        has_person = bool(params.getlist("person"))
        if has_person and not params.getlist("types"):
            types: list[str] | None = []
        else:
            types = check.query_choices(params, "types", EVENT_TYPES)
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        person = check.query_pattern(params, "person", people.is_pseudonym)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert types is not None and after is not None and limit is not None
        _require(principal, Permission.EVENTS_READ)

        # O apelido vira pseudônimo na mesma transação da consulta.
        page = await ctx.call(in_transaction(events.query_events), types, after=after, limit=limit,
                              secrets=ctx.secrets, person=person)
        return schemas.EventsPage(items=[_event_dto(e) for e in page.items],
                                  next_cursor=page.next_cursor)

    # --- doctor, parte da API ---------------------------------------------------------------------

    @route("system.doctor", response_model=schemas.SystemDoctor)
    async def system_doctor(request: Request) -> schemas.SystemDoctor:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        check.reject_body(await request.body())
        check.raise_if_invalid()
        _require(principal, Permission.OFFERINGS_READ)

        # Só leitura: nenhum `append_event` e nenhuma chamada ao Telegram; a chave-mestra conta como
        # configurada quando o runtime dos bots tem a cifra. `doctor.collect` é
        # buscado no módulo a cada chamada, para que o teste de fronteira o troque por um dublê.
        bots_runtime = ctx.bots
        master_key_configured = bots_runtime is not None and bots_runtime.cipher is not None
        now = ctx.clock.now()
        checks = await ctx.call(in_transaction(doctor.collect), now=now,
                                master_key_configured=master_key_configured, settings=ctx.doctor_settings)
        return schemas.SystemDoctor(
            checked_at=schemas.timestamp(now),
            checks=[schemas.DoctorCheckOut(id=c.id, result=c.result, message=c.message, offering=c.offering,
                                           count=c.count) for c in checks],
        )

    # --- oferecimentos ----------------------------------------------------------------------------

    def _ref_request(request: Request, check: RequestChecker, *, body: bytes) -> str | None:
        """Cabeçalho, caminho (`ref`), consulta estrita e rota sem corpo (a ordem de validação)."""
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        check.reject_body(body)
        return ref

    @route("offerings.create", response_model=schemas.Offering, status_code=201)
    async def create_offering(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _offering_creation(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(offerings.create_offering, **parsed, now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        if isinstance(result, offerings.CodeRejected):
            raise ApiError("invalid_request", fields=[FieldIssue("code", "invalid_value")])
        if isinstance(result, offerings.CodeTaken):
            raise ApiError("already_exists", fields=[FieldIssue("code", "duplicate")])
        if isinstance(result, offerings.ContactRejected):
            raise ApiError("invalid_request", fields=[FieldIssue("contact_email", "invalid_value")])
        return _offering_dto(result)

    @route("offerings.list", response_model=schemas.OfferingsPage)
    async def list_offerings(request: Request) -> schemas.OfferingsPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        params = request.query_params
        check.query_keys(params, ("after", "limit"))
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert after is not None and limit is not None
        _require(principal, Permission.OFFERINGS_READ)

        page = await ctx.call(offerings.list_offerings, after=after, limit=limit, secrets=ctx.secrets)
        return schemas.OfferingsPage(items=[_offering_dto(o) for o in page.items],
                                     next_cursor=page.next_cursor)

    @route("offerings.get", response_model=schemas.Offering)
    async def get_offering(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_READ)

        result = await ctx.call(offerings.get_offering, ref, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, offerings.Offering)
        return _offering_dto(result)

    @route("offerings.close", response_model=schemas.Offering)
    async def close_offering(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(offerings.close_offering, ref, now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, offerings.Offering)
        return _offering_dto(result)

    @route("offerings.prepare_archive", response_model=schemas.ArchivePreparation)
    async def prepare_archive(request: Request) -> schemas.ArchivePreparation:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(offerings.prepare_archive, ref, now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, offerings.ArchivePreparation)
        return schemas.ArchivePreparation(
            offering=_offering_dto(result.offering), team_members_active=result.team_members_active,
            pending_links=result.pending_links, confirmation_code=result.confirmation_code,
            expires_at=schemas.timestamp(result.expires_at))

    @route("offerings.archive", response_model=schemas.Offering)
    async def archive_offering(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        code = _confirmation_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and code is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(offerings.archive_offering, ref, confirmation_code=code,
                                now=ctx.clock.now(), actor_token_id=principal.actor,
                                secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, offerings.Offering)
        return _offering_dto(result)

    # --- expurgo ----------------------------------------------------------------------------------
    # Ordem das regras: 404 do oferecimento → (confirmação: já expurgado devolve o resultado gravado) →
    # `invalid_state` → `purge_pending` → (confirmação) `invalid_confirmation` → `purge_changed`; a ordem é
    # a do núcleo (`purge`), e aqui só se convertem as sentinelas.

    @route("offerings.prepare_purge", response_model=schemas.PurgePreparationOut)
    async def prepare_purge(request: Request) -> schemas.PurgePreparationOut:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.PEOPLE_WRITE)

        result = await ctx.call(purge.prepare_purge, ref, now=ctx.clock.now(),
                                actor_token_id=principal.actor)
        _purge_error(result)
        assert isinstance(result, purge.PurgePreparation)
        impact = result.impact
        return schemas.PurgePreparationOut(
            offering=_offering_ref(result),
            impact=schemas.PurgeImpactOut(
                identities_to_delete=impact.identities_to_delete, identities_kept=impact.identities_kept,
                endpoints=impact.endpoints, updates=impact.updates, contact_email_set=impact.contact_email_set),
            confirmation_code=result.confirmation_code, expires_at=schemas.timestamp(result.expires_at))

    @route("offerings.purge", response_model=schemas.PurgeResultOut)
    async def purge_offering(request: Request) -> schemas.PurgeResultOut:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        code = _confirmation_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and code is not None
        _require(principal, Permission.PEOPLE_WRITE)

        result = await ctx.call(purge.confirm_purge, ref, confirmation_code=code, now=ctx.clock.now(),
                                actor_token_id=principal.actor)
        _purge_error(result)
        assert isinstance(result, purge.PurgeResult)
        outcome = result.outcome
        return schemas.PurgeResultOut(
            offering=_offering_ref(result), purged_at=schemas.timestamp(result.purged_at),
            result=schemas.PurgeOutcomeOut(
                identities_deleted=outcome.identities_deleted, identities_kept=outcome.identities_kept,
                endpoints=outcome.endpoints, updates=outcome.updates,
                free_messages_redacted=outcome.free_messages_redacted, outbox_redacted=outcome.outbox_redacted,
                contact_email_deleted=outcome.contact_email_deleted))

    # --- configuração de mensagens livres ---------------------------------------------------------

    @route("offerings.update_settings", response_model=schemas.Offering)
    async def update_offering_settings(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _settings_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(offerings.update_settings, ref, **parsed, now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        if isinstance(result, offerings.ContactRejected):
            raise ApiError("invalid_request", fields=[FieldIssue("contact_email", "invalid_value")])
        assert isinstance(result, offerings.Offering)
        return _offering_dto(result)

    # --- bots -------------------------------------------------------------------------------------

    @route("bots.register", response_model=schemas.Bot)
    async def register_bot(request: Request) -> schemas.Bot:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        token = _bot_registration(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and token is not None
        _require(principal, Permission.BOTS_MANAGE)
        runtime = ctx.bots
        if runtime is None or runtime.cipher is None:
            raise ApiError("not_configured")

        result = await ctx.call(bots.register_bot, ref, token=token, runtime=runtime,
                                now=ctx.clock.now(), actor_token_id=principal.actor)
        _rule_error(result)
        if isinstance(result, bots.TokenRefused):
            raise ApiError("invalid_request", fields=[FieldIssue("token", "invalid_value")])
        if isinstance(result, bots.TelegramDown):
            raise ApiError("unavailable")
        if isinstance(result, bots.BotConflict):
            raise ApiError("bot_conflict", fields=[FieldIssue("token", result.reason)])
        assert isinstance(result, bots.Bot)
        return _bot_dto(result)

    @route("bots.activate", response_model=schemas.Offering)
    async def activate_bot(request: Request) -> schemas.Offering:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.BOTS_MANAGE)

        result = await ctx.call(bots.activate, ref, now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, offerings.Offering)
        return _offering_dto(result)

    @route("bots.get", response_model=schemas.Bot)
    async def get_bot(request: Request) -> schemas.Bot:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_READ)

        result = await ctx.call(in_transaction(bots.get_bot), ref)
        _rule_error(result)
        assert isinstance(result, bots.Bot)
        return _bot_dto(result)

    # --- equipe -----------------------------------------------------------------------------------

    @route("team.link", response_model=schemas.TeamLink, status_code=201)
    async def create_team_link(request: Request) -> schemas.TeamLink:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _team_link_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        person, valid_hours = parsed
        result = await ctx.call(team.create_link, ref, person=person, valid_hours=valid_hours,
                                now=ctx.clock.now(), actor_token_id=principal.actor)
        if isinstance(result, team.PersonNotFound):
            raise ApiError("not_found")
        _rule_error(result)
        if isinstance(result, team.RoleConflict):
            # A pessoa já é aluna do oferecimento.
            raise ApiError("role_conflict", fields=[FieldIssue("person", "invalid_value")])
        assert isinstance(result, team.TeamLink)
        return schemas.TeamLink(
            link=result.link, expires_at=schemas.timestamp(result.expires_at),
            person=schemas.TeamLinkPerson(pseudo_id=str(result.pseudo_id), alias=result.alias),
            offering=schemas.TeamLinkOffering(offering_id=str(result.offering_id), code=result.code,
                                              timezone=result.timezone))

    # --- alunos e convites ------------------------------------------------------------------------

    @route("students.import", response_model=schemas.StudentsImport)
    async def import_students(request: Request) -> schemas.StudentsImport:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        rows = _roster_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and rows is not None
        _require(principal, Permission.PEOPLE_WRITE)

        result = await ctx.call(students.import_roster, ref, rows, now=ctx.clock.now(),
                                actor_token_id=principal.actor)
        _rule_error(result)
        if isinstance(result, students.ListConflict):
            raise ApiError("list_conflict", fields=[FieldIssue(f"rows[{index}]", reason)
                                                    for index, reason in result.fields])
        assert isinstance(result, students.ImportResult)
        return schemas.StudentsImport(
            offering=_offering_ref(result), received=result.received,
            people_created=result.people_created, people_reused=result.people_reused,
            enrollments_created=result.enrollments_created, already_enrolled=result.already_enrolled)

    @route("students.summary", response_model=schemas.StudentsSummary)
    async def get_students_summary(request: Request) -> schemas.StudentsSummary:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref = _ref_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_READ)

        result = await ctx.call(in_transaction(students.adhesion), ref, now=ctx.clock.now())
        _rule_error(result)
        assert isinstance(result, students.Adhesion)
        return schemas.StudentsSummary(
            offering=_offering_ref(result), enrolled=result.enrolled, no_invite=result.no_invite,
            sending=result.sending, invite_valid=result.invite_valid,
            terms_presented=result.terms_presented, invite_lapsed=result.invite_lapsed,
            send_failed=result.send_failed, linked=result.linked)

    @route("invites.prepare_send", response_model=schemas.InvitesPreparation)
    async def prepare_send_invites(request: Request) -> schemas.InvitesPreparation:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        valid_hours = _prepare_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and valid_hours is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(students.prepare_invites, ref, valid_hours=valid_hours,
                                now=ctx.clock.now(), actor_token_id=principal.actor)
        _rule_error(result)
        assert isinstance(result, students.InvitePreparation)
        return schemas.InvitesPreparation(
            offering=_offering_ref(result), eligible=result.eligible, valid_hours=result.valid_hours,
            confirmation_code=result.confirmation_code,
            expires_at=schemas.timestamp(result.expires_at))

    @route("invites.send", response_model=schemas.InvitesQueued)
    async def send_invites(request: Request) -> schemas.InvitesQueued:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        code = _confirmation_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and code is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(students.confirm_invites, ref, confirmation_code=code,
                                now=ctx.clock.now(), actor_token_id=principal.actor)
        _rule_error(result)
        assert isinstance(result, students.InvitesQueued)
        return schemas.InvitesQueued(offering=_offering_ref(result), queued=result.queued,
                                     valid_hours=result.valid_hours)

    @route("invites.resend", response_model=schemas.InvitesResend)
    async def resend_invites(request: Request) -> schemas.InvitesResend:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _resend_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        pseudonyms, valid_hours = parsed
        result = await ctx.call(students.resend_invites, ref, pseudonyms=pseudonyms,
                                valid_hours=valid_hours, now=ctx.clock.now(),
                                actor_token_id=principal.actor)
        _rule_error(result)
        assert isinstance(result, students.ResendResult)
        return schemas.InvitesResend(
            offering=_offering_ref(result), queued=result.queued, valid_hours=result.valid_hours,
            not_students=[schemas.NotStudent(pseudo_id=str(pseudo_id), alias=alias)
                          for pseudo_id, alias in result.not_students],
            not_found_count=result.not_found_count)

    # --- disparos e mensagens livres --------------------------------------------------------------

    def _dispatch_request(request: Request, check: RequestChecker, *, body: bytes) -> tuple[str | None, str | None]:
        """Cabeçalho, caminho (`ref` e `dispatch_id`), consulta estrita e rota sem corpo."""
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        dispatch_id = check.path_param(request.path_params, "dispatch_id", _is_dispatch_id)
        check.query_keys(request.query_params, ())
        check.reject_body(body)
        return ref, dispatch_id

    def _page_request(request: Request, check: RequestChecker, *, body: bytes) -> tuple[str | None, int | None,
                                                                                         int | None]:
        """Cabeçalho, caminho (`ref`), `after`, `limit` e rota sem corpo."""
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        params = request.query_params
        check.query_keys(params, ("after", "limit"))
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        check.reject_body(body)
        return ref, after, limit

    @route("dispatches.prepare", response_model=schemas.Dispatch)
    async def prepare_dispatch(request: Request) -> schemas.Dispatch:
        principal = principal_of(request.scope)
        check = RequestChecker()
        client = check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        now = ctx.clock.now()
        parsed = (_dispatch_prepare_request(check, obj, now=now, allowed_hosts=ctx.repo_settings.allowed_hosts,
                                            client=client) if obj is not None else None)
        check.raise_if_invalid()
        assert ref is not None and parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(dispatches.prepare_dispatch, ref, parsed, now=now, actor_token_id=principal.actor,
                                secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, dispatches.Dispatch)
        return _dispatch_dto(result)

    @route("dispatches.get", response_model=schemas.Dispatch)
    async def get_dispatch(request: Request) -> schemas.Dispatch:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, dispatch_id = _dispatch_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None
        _require(principal, Permission.OFFERINGS_READ)

        result = await ctx.call(_with_identities(dispatches.get_dispatch), ref, uuid.UUID(dispatch_id),
                                now=ctx.clock.now(), secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, dispatches.Dispatch)
        return _dispatch_dto(result)

    @route("dispatches.list", response_model=schemas.DispatchesPage)
    async def list_dispatches(request: Request) -> schemas.DispatchesPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, after, limit = _page_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and after is not None and limit is not None
        _require(principal, Permission.OFFERINGS_READ)

        page = await ctx.call(_with_identities(dispatches.list_dispatches), ref, after=after, limit=limit,
                              secrets=ctx.secrets)
        _rule_error(page)
        assert isinstance(page, dispatches.DispatchesPage)
        return schemas.DispatchesPage(
            offering=schemas.OfferingRef(offering_id=str(page.offering_id), code=page.code),
            timezone=page.timezone, items=[_dispatch_summary_dto(item) for item in page.items],
            next_cursor=after if page.next_cursor is None else page.next_cursor)

    @route("dispatches.confirm", response_model=schemas.Dispatch)
    async def confirm_dispatch(request: Request) -> schemas.Dispatch:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, dispatch_id = _dispatch_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(dispatches.confirm_dispatch, ref, uuid.UUID(dispatch_id), now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, dispatches.Dispatch)
        return _dispatch_dto(result)

    @route("dispatches.cancel", response_model=schemas.Dispatch)
    async def cancel_dispatch(request: Request) -> schemas.Dispatch:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, dispatch_id = _dispatch_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(dispatches.cancel_dispatch, ref, uuid.UUID(dispatch_id), now=ctx.clock.now(),
                                actor_token_id=principal.actor, secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, dispatches.Dispatch)
        return _dispatch_dto(result)

    @route("messages.list", response_model=schemas.FreeMessagesPage)
    async def list_messages(request: Request) -> schemas.FreeMessagesPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, after, limit = _page_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and after is not None and limit is not None
        _require(principal, Permission.OFFERINGS_READ)

        page = await ctx.call(_with_identities(freemessages.list_free_messages), ref, after=after, limit=limit,
                              secrets=ctx.secrets)
        _rule_error(page)
        assert isinstance(page, freemessages.FreeMessagesPage)
        return schemas.FreeMessagesPage(
            offering=schemas.OfferingRef(offering_id=str(page.offering_id), code=page.code),
            items=[_free_message_dto(item) for item in page.items],
            next_cursor=after if page.next_cursor is None else page.next_cursor)

    # --- relatório do disparo ---------------------------------------------------------------------

    @route("reports.get", response_model=schemas.Report,
           extra_responses={200: {"content": {"text/csv": {"schema": dict(schemas.REPORT_CSV_SCHEMA)}}}})
    async def get_report(request: Request) -> Response:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        dispatch_id = check.path_param(request.path_params, "dispatch_id", _is_dispatch_id)
        params = request.query_params
        check.query_keys(params, ("format", "version"))
        fmt = _report_format(check, params)
        version = _report_version(check, params)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None and fmt is not None and version is not INVALID
        _require(principal, Permission.OFFERINGS_READ)

        result = await ctx.call(_with_identities(reports.get_report), ref, uuid.UUID(dispatch_id), version=version,
                                now=ctx.clock.now(), secrets=ctx.secrets, formatter=ctx.report_formatter)
        _rule_error(result)
        assert isinstance(result, reports.Report)
        return _report_csv(result) if fmt == "csv" else _report_dto(result)

    @route("reports.list_versions", response_model=schemas.ReportVersionsPage)
    async def list_report_versions(request: Request) -> schemas.ReportVersionsPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        dispatch_id = check.path_param(request.path_params, "dispatch_id", _is_dispatch_id)
        params = request.query_params
        check.query_keys(params, ("after", "limit"))
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None and after is not None and limit is not None
        _require(principal, Permission.OFFERINGS_READ)

        page = await ctx.call(in_transaction(reports.list_report_versions), ref, uuid.UUID(dispatch_id),
                              after=after, limit=limit)
        _rule_error(page)
        assert isinstance(page, reports.ReportVersionsPage)
        return _versions_dto(page)

    @route("reports.resend_summary", response_model=schemas.ReportResend)
    async def resend_report_summary(request: Request) -> schemas.ReportResend:
        principal = principal_of(request.scope)
        check = RequestChecker()
        ref, dispatch_id = _dispatch_request(request, check, body=await request.body())
        check.raise_if_invalid()
        assert ref is not None and dispatch_id is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        # A transação é a do núcleo; o resumo é composto e enviado pelo worker.
        result = await ctx.call(reports.resend_summary, ref, uuid.UUID(dispatch_id), now=ctx.clock.now(),
                                actor_token_id=principal.actor, formatter=ctx.report_formatter,
                                secrets=ctx.secrets)
        _rule_error(result)
        assert isinstance(result, reports.ResendResult)
        return _resend_dto(result)

    # --- resumo diário ----------------------------------------------------------------------------

    @route("offerings.get_digest", response_model=schemas.OfferingDigest)
    async def get_offering_digest(request: Request) -> schemas.OfferingDigest:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert ref is not None
        _require(principal, Permission.OFFERINGS_READ)

        view = await ctx.call(in_transaction(digests.get_digest), ref)
        _rule_error(view)
        assert isinstance(view, digests.DigestView)
        return _digest_dto(view)

    @route("offerings.update_digest", response_model=schemas.OfferingDigest)
    async def update_offering_digest(request: Request) -> schemas.OfferingDigest:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        ref = check.path_param(request.path_params, "ref", offerings.is_ref)
        check.query_keys(request.query_params, ())
        obj = check.json_object(await request.body())
        parsed = _digest_request(check, obj) if obj is not None else None
        check.raise_if_invalid()
        assert ref is not None and parsed is not None
        _require(principal, Permission.OFFERINGS_WRITE)

        result = await ctx.call(digests.update_digest, ref, **parsed, now=ctx.clock.now(),
                                actor_token_id=principal.actor)
        _rule_error(result)
        assert isinstance(result, digests.DigestView)
        return _digest_dto(result)

    # --- banco de questões local ------------------------------------------------------------------

    # Um envio por vez por processo: o segundo espera a vaga sem ler o corpo; a vaga só é solta depois da
    # gravação (ou da recusa).
    app.state.bank_upload_slot = asyncio.Semaphore(1)

    @route("banks.upload", response_model=schemas.BankUploadResult)
    async def upload_bank(request: Request) -> schemas.BankUploadResult:
        # A única rota com a permissão antes da forma do corpo: o corpo custa até centenas de MB.
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        check.query_keys(request.query_params, ())
        check.raise_if_invalid()
        _require(principal, Permission.OFFERINGS_WRITE)
        _content_length(check, request)
        check.raise_if_invalid()

        async with app.state.bank_upload_slot:
            raw = await _read_bank_body(request)
            stored, created = await run_in_threadpool(_bank_upload, ctx, raw, actor_token_id=principal.actor)
        return schemas.BankUploadResult(**_bank_version_dto(stored), created=created)

    @route("banks.list", response_model=schemas.BanksPage)
    async def list_banks(request: Request) -> schemas.BanksPage:
        principal = principal_of(request.scope)
        check = RequestChecker()
        check.client_kind(request.headers)
        params = request.query_params
        check.query_keys(params, ("after", "limit", "name"))
        after = check.query_int(params, "after", default=0, minimum=0, maximum=MAX_CURSOR)
        limit = check.query_int(params, "limit", default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
        name = check.query_pattern(params, "name", _is_bank_name)
        check.reject_body(await request.body())
        check.raise_if_invalid()
        assert after is not None and limit is not None
        _require(principal, Permission.OFFERINGS_READ)

        page = await ctx.call(_with_identities(banks.list_banks), name=name, after=after, limit=limit,
                              secrets=ctx.secrets)
        return schemas.BanksPage(items=[schemas.BankVersion(**_bank_version_dto(item)) for item in page.items],
                                 next_cursor=page.next_cursor)
