"""Encerramento e arquivamento do oferecimento com disparos (a recusa do arquivamento só existe com
disparo aberto ou fechado).

Contratos: o encerramento cancela os disparos `validating` e `scheduled`, confirmados
ou não, com `offering_closed`, um `dispatch.cancelled` por disparo com o token do encerramento; os
abertos seguem até o fechamento; o arquivamento, no preparo e na confirmação, é recusado com
`grading_pending` enquanto houver submissão `grading` ou update `pending` do bot, só em oferecimento com
disparo `open` ou `closed`; **só na confirmação**, com o bot sendo consultado, também quando
`bots.last_poll_ok_at` é anterior ao horário do preparo do arquivamento; aceito, fecha os abertos como no
horário, com `trigger: archive`, e o que não puder ser enviado fica como falha; o 403 numa
edição → só registro; as transições do disparo; o fechamento (`dispatch.closed
{trigger, counts}`) e os eventos; nos contratos, o `grading_pending` com a mensagem exata, **sem**
`fields`, `state` nem `permission`; a contagem sai em `dispatches.get`, `deliveries.grading_pending`.
O comportamento sem a recusa vale para oferecimentos sem disparo `open`/`closed` (a recusa só
existe com eles), afirmado na mesma função de um caso de disparo.

Tudo passa pelo caminho real, só com dublês: o worker com `bots` (`bot_worker_thread`), o motor falso
estrito (`engine_fake`, pelo adaptador real), a obtenção falsa (`repo_fetcher`) e a Bot API falsa com a
extensão do disparo (`rich_bot_api`). O vínculo do aluno é montado por SQL, como em
`test_free_messages.py`: a lista entra pela API e a matrícula passa a `active` com um endereço `active`
no bot; um `/start` sem código torna o chat conhecido da Bot API falsa antes de a questão sair. As
condições que o arquivamento confere e que o worker desfaria sozinho (um update `pending`, a posse viva
com a última consulta antiga) são montadas por SQL **com o worker parado**, como outros testes montam a
posse; a submissão `grading` vem do caminho real, com o motor falso recusando a correção (500) até o
teste liberar.

Horários pelo `FakeClock` (`iso`); ids internos, `seq` e pseudônimos nunca como literal. No esqueleto,
toda função falha no primeiro `dispatches.prepare` (rota ausente, 404).
O arquivamento que fecha os abertos só fica verde quando as partes de que depende
estiverem implementadas.

O gabarito só vai a quem já respondeu: o arquivamento fecha os
abertos como no horário, com a revelação só às `answered`/`dont_know`; a `no_answer` recebe a edição de
fechamento do arquivamento, só com "Janela fechada", sem gabarito, sem controles, sem o aviso de
atrasada e sem o `/naosei` (`test_archive_reveals_only_to_answered_not_to_no_answer`); a `no_answer` de um
disparo já fechado pelo horário não é editada nem revelada no arquivamento e fica com os controles da edição do
horário (limitação conhecida; `test_archive_does_not_edit_or_reveal_no_answer_of_an_already_closed_dispatch`).
As respostas que devolvem o `Offering` toleram só a ausência das chaves das mensagens livres (`_ok`).
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import io
import itertools
import json
import re
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
from jsonschema import Draft202012Validator
from psycopg.types.json import Jsonb

from cq_server import deliverytexts as texts
from cq_server.adapters.console import ConsoleChannel
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.ports.repos import FetchTemporaryFailure
from tests.helpers.api import (
    _FREE_MESSAGE_OFFERING_KEYS,
    ADMIN_TOKEN,
    _assert_offering_matches,
    activate_bot,
    all_events,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_dispatches,
    prepare_dispatch,
    register_bot,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, load_schema
from tests.helpers.engine import ANSWER_SENTINEL

ADMIN = auth(ADMIN_TOKEN)
ADMIN_ACTOR = "bootstrap"  # o `token_id` do token inicial nos eventos
WORKER = "worker-ciclo-disparos"
LIVE_READER = "leitor-vivo-disparos"
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
PROCESS_TIMEOUT = 20.0
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)
LEASE = timedelta(seconds=30)
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
FAST = DeliverySettings(chat_interval_seconds=0.05)
QUIET_WINDOW = 2.5  # segundos reais sem efeito esperado depois de um controle positivo

REPO = "https://repos.cq.test/disciplina/questoes-ciclo.git"
SLOW_REPO = "https://repos.cq.test/disciplina/questoes-ciclo-lentas.git"
COMMIT = "c1c1" * 10

DELIVERY_STATES = ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                   "answered_late", "question_failed")

DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                  "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
                  "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                  "submission.graded", "submission.internal_failure")
# Os eventos que o ciclo de vida compara: o do oferecimento e os do disparo. Os de bot e de posse ficam
# de fora (os saltos do relógio e a parada do worker mexem na posse), como nos testes do vínculo do aluno.
LIFECYCLE_EVENT_TYPES = ("offering.state_changed",) + DISPATCH_EVENT_TYPES

_codes = itertools.count(1)
_update_ids = itertools.count(880_000_001)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Ctx:
    """Um oferecimento ativo com bot e, se pedido, um aluno vinculado (`chat_id` do endereço)."""

    offering: dict
    tg: int
    offering_row: int
    bot_row: int
    student: Any = None
    person_id: int | None = None

    @property
    def code(self) -> str:
        return self.offering["code"]


def _setup(client: Any, sql: psycopg.Connection, clock: Any, rich: Any, *, student: int | None) -> _Ctx:
    """Oferecimento ativo com bot; com `student`, uma linha neutra importada pela API e o
    vínculo do aluno (matrícula `active` com o termo aceito e endereço `active` no bot), por SQL."""
    n = next(_codes)
    offering = create_offering(client, ADMIN_TOKEN, code=f"mc102-ciclo-disparos-{n}", course=COURSE, term=TERM,
                               timezone=ZONE)
    tg = rich.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], rich.token_of(tg))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                               (uuid.UUID(offering["offering_id"]),)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    ctx = _Ctx(offering=offering, tg=tg, offering_row=offering_row, bot_row=bot_row)
    if student is None:
        return ctx
    user, person_id = _link_student(client, sql, clock, ctx, student)
    return dataclasses.replace(ctx, student=user, person_id=person_id)


def _link_student(client: Any, sql: psycopg.Connection, clock: Any, ctx: _Ctx, student: int) -> tuple[Any, int]:
    """Uma linha neutra importada pela API no oferecimento de `ctx` e o vínculo do aluno (matrícula
    `active` com o termo aceito e endereço `active` no bot), por SQL; devolve `(usuário, person_id)`."""
    row = {"name": f"Pessoa Aluna Ciclo {student:02d}", "ra": f"57200001{student:02d}",
           "email": f"aluna.ciclo{student:02d}@turma.example.org"}
    imported = import_students(client, ADMIN_TOKEN, ctx.code, [row])
    assert (imported["people_created"], imported["enrollments_created"]) == (1, 1)
    person_id = sql.execute("SELECT person_id FROM identities WHERE ra = %s", (row["ra"],)).fetchone()[0]
    user = make_user(4200 + student)
    now = clock.now()
    assert sql.execute(
        "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
        "terms_accepted_at = %s WHERE person_id = %s AND offering_id = %s AND role = 'student'",
        (now, now, person_id, ctx.offering_row)).rowcount == 1
    sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_id, ctx.bot_row, user.user_id, now, now))
    return user, person_id


def _start_worker(bot_worker_thread: Any, settings: Any, sql: psycopg.Connection, engine_fake: Any,
                  repo_fetcher: Any, rich: Any, ctx: _Ctx) -> Any:
    """O worker com bots, motor (o adaptador real sobre o motor falso; sem conexão persistente, não é
    fechado) e obtenção; espera a primeira consulta do bot e, com aluno, um `/start` sem código dele
    (resposta `HELP`), para que o chat seja conhecido da Bot API falsa antes de a questão sair."""
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))
    thread = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER),
                               channel=ConsoleChannel(io.StringIO()), engine=engine, fetcher=repo_fetcher,
                               delivery=FAST)
    rich.wait_for(lambda: rich.pending_polls(ctx.tg), timeout=PROCESS_TIMEOUT)
    if ctx.student is not None:
        update_id = rich.push_text(ctx.tg, ctx.student, "/start", bot_command=True)
        _processed(rich, sql, ctx.tg, update_id)
        rich.wait_for(lambda: rich.messages(ctx.tg, ctx.student.user_id), timeout=PROCESS_TIMEOUT)
    return thread


def _processed(rich: Any, sql: psycopg.Connection, tg: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve `(id, outcome)`."""
    assert update_id is not None

    def ready() -> tuple | None:
        row = sql.execute(
            "SELECT u.id, u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (tg, update_id)).fetchone()
        return row if row is not None and row[1] == "processed" else None

    row = rich.wait_for(ready, timeout=PROCESS_TIMEOUT)
    return row[0], row[2]


def _prepare(client: Any, ctx: _Ctx, qid: str, *, repo: str = REPO, opens_at: datetime | None = None,
             closes_at: datetime) -> str:
    prepared = prepare_dispatch(client, ADMIN_TOKEN, ctx.code, repo=repo, commit=COMMIT, qid=qid,
                                opens_at=None if opens_at is None else iso(opens_at), closes_at=iso(closes_at))
    assert prepared["state"] == "validating"
    return prepared["dispatch_id"]


def _dispatch(client: Any, ctx: _Ctx, dispatch_id: str) -> dict:
    return get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)


def _wait_state(client: Any, rich: Any, ctx: _Ctx, dispatch_id: str, state: str) -> dict:
    return rich.wait_for(lambda: (body := _dispatch(client, ctx, dispatch_id))["state"] == state and body,
                         timeout=PROCESS_TIMEOUT)


def _wait_validated(client: Any, rich: Any, ctx: _Ctx, dispatch_id: str) -> None:
    body = rich.wait_for(lambda: (b := _dispatch(client, ctx, dispatch_id))["state"] != "validating" and b,
                         timeout=PROCESS_TIMEOUT)
    assert (body["state"], body["confirmed"], body["rejection"]) == ("scheduled", False, None)


def _counts(**values: int) -> dict:
    counts = dict.fromkeys(DELIVERY_STATES, 0)
    counts.update(values)
    return counts


def _deliveries(grading_pending: int = 0, **values: int) -> dict:
    return {**_counts(**values), "grading_pending": grading_pending}


def _cancelled_view(body: dict) -> tuple:
    """Os campos do cancelamento e do envio de um disparo."""
    return (body["state"], body["cancel_reason"], body["cancelled_at"], body["opened_at"], body["closed_at"],
            body["deliveries"])


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos do ciclo de vida depois do cursor, sem o `seq`, em ordem de `seq`."""
    return [{k: v for k, v in e.items() if k != "seq"}
            for e in all_events(client, ADMIN_TOKEN, *LIFECYCLE_EVENT_TYPES) if e["seq"] > cursor]


def _event(event_type: str, at: Any, data: dict, actor: str | None = ADMIN_ACTOR) -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _state_changed(offering: dict, at: Any, from_state: str, to_state: str) -> dict:
    return _event("offering.state_changed", at, {"offering_id": offering["offering_id"],
                                                 "from_state": from_state, "to_state": to_state})


def _cancelled(offering: dict, dispatch_id: str, at: Any) -> dict:
    return _event("dispatch.cancelled", at, {"offering_id": offering["offering_id"], "dispatch_id": dispatch_id,
                                             "cancel_reason": "offering_closed"})


def _sorted(events: list[dict]) -> list[dict]:
    """A ordem entre os eventos de uma mesma transação não é contrato: compara por tipo e disparo."""
    return sorted(events, key=lambda e: (e["type"], e["data"].get("dispatch_id", "")))


# O esquema congelado do
# `Offering` exige `free_messages` e `contact_email_set` (mensagens livres e contato), que a
# implementação das mensagens livres devolve. Sem ela, com `assert_matches` estrito, `_close`, `_get_offering`,
# `_prepared_archive` e o arquivamento cairiam no esquema antes das asserções do gabarito, que ficariam encobertas.
# Como o ajudante compartilhado (`tests/helpers/api.py`),
# as respostas que devolvem o `Offering` conferem o esquema congelado inteiro (tipos, enums,
# padrões, `additionalProperties: false` e os demais obrigatórios) e só toleram a **ausência** das duas chaves das
# mensagens livres; presentes, continuam conferidas pelo tipo. Este arquivo não as tem como objeto (a forma do `Offering`
# é conferida estrita em test_offerings_api e test_offering_settings). As demais operações seguem estritas.
_OFFERING_OPS = frozenset({"offerings.get", "offerings.close", "offerings.archive", "offerings.prepare_archive"})


@functools.lru_cache(maxsize=None)
def _prepare_archive_schema_without_free_message_required() -> Draft202012Validator:
    """O esquema congelado de `offerings.prepare_archive` com as chaves das mensagens livres fora do `required` do
    `offering` aninhado (só isso muda); o ajudante compartilhado cobre o `Offering` na raiz."""
    schema = copy.deepcopy(load_schema("offerings.prepare_archive", "response"))
    nested = schema["properties"]["offering"]
    required = nested.get("required")
    assert isinstance(required, list) and "offering_id" in required, (
        "offerings.prepare_archive: o `offering` do esquema de resposta não é o `Offering` esperado")
    nested["required"] = [key for key in required if key not in _FREE_MESSAGE_OFFERING_KEYS]
    return Draft202012Validator(schema)


def _assert_offering_response(op_id: str, body: Any) -> None:
    """Confere a resposta 200 de uma operação de `_OFFERING_OPS` tolerando só a ausência das chaves das mensagens livres."""
    if op_id != "offerings.prepare_archive":
        _assert_offering_matches(op_id, 200, body, strict=False)
        return
    errors = sorted(_prepare_archive_schema_without_free_message_required().iter_errors(body), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"{list(e.path)}: {e.message[:200]}" for e in errors[:10])
        raise AssertionError(f"{op_id} 200 não valida contra o esquema congelado (sem exigir "
                             f"{', '.join(_FREE_MESSAGE_OFFERING_KEYS)} em `offering`): {detail}")


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    if op_id in _OFFERING_OPS:
        _assert_offering_response(op_id, body)
    else:
        assert_matches(op_id, 200, body)
    return body


def _get_offering(client: Any, ref: str) -> dict:
    return _ok(client.get(f"/v1/offerings/{ref}", headers=ADMIN), "offerings.get")


def _close(client: Any, ref: str) -> dict:
    return _ok(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")


def _prepare_archive(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)


def _archive(client: Any, ref: str, code: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": code}, headers=ADMIN)


def _prepared_archive(client: Any, clock: Any, offering: dict) -> str:
    """O preparo aceito, com o resumo exato; devolve o código."""
    body = _ok(_prepare_archive(client, offering["code"]), "offerings.prepare_archive")
    code = body["confirmation_code"]
    assert CONFIRMATION_RE.fullmatch(code), code
    assert body == {"offering": offering, "team_members_active": 0, "pending_links": 0,
                    "confirmation_code": code, "expires_at": iso(clock.now() + CONFIRMATION)}
    return code


def _pending_update(sql: psycopg.Connection, clock: Any, ctx: _Ctx) -> int:
    """Um update `message` gravado e ainda não processado do bot, do chat do
    aluno; devolve `telegram_updates.id`."""
    now = clock.now()
    epoch = sql.execute("SELECT update_epoch FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone()[0]
    return sql.execute(
        "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, sender_id, "
        "text, command, details, received_at, state) "
        "VALUES (%s, %s, %s, 'message', %s, 'private', %s, 'Ainda estou aqui.', NULL, %s, %s, 'pending') "
        "RETURNING id",
        (ctx.bot_row, epoch, next(_update_ids), ctx.student.user_id, ctx.student.user_id,
         Jsonb({"message_id": 1, "date": int(now.timestamp())}), now)).fetchone()[0]


def _mark_processed(sql: psycopg.Connection, clock: Any, update_row: int) -> None:
    assert sql.execute(
        "UPDATE telegram_updates SET state = 'processed', outcome = 'not_handled', processed_at = %s "
        "WHERE id = %s AND state = 'pending'", (clock.now(), update_row)).rowcount == 1


def _submission_outcomes(sql: psycopg.Connection) -> list[str]:
    return [row[0] for row in sql.execute("SELECT outcome FROM submissions ORDER BY id").fetchall()]


def _advance_until(clock: Any, rich: Any, predicate: Any, *, step: float = 61, rounds: int = 8) -> None:
    """Avança o relógio em passos de `step` s (mais que a espera máxima da fila, 60 s) até `predicate()`
    ficar verdadeiro; cada passo espera até 5 s reais."""
    for _ in range(rounds):
        clock.advance(step)
        try:
            rich.wait_for(predicate, timeout=5)
            return
        except AssertionError:
            continue
    raise AssertionError(f"condição não satisfeita depois de {rounds} passos de {step} s")


# ---------------------------------------------------------------------------------------------
# Encerramento
# ---------------------------------------------------------------------------------------------


def test_close_cancels_validating_and_scheduled_dispatches_and_open_ones_run_to_closing(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O encerramento do oferecimento cancela os disparos validando e agendados
    (confirmados ou não), com o motivo e evento, e os abertos seguem até o fechamento; e uma
    validação que termina depois do cancelamento não muda o estado; nada é enviado; a recusa do
    arquivamento só existe com disparo aberto ou fechado.

    Num oferecimento com um aluno vinculado: um disparo **aberto** (entregue ao aluno), um agendado
    confirmado, um agendado sem confirmação e um **validando** (a obtenção do repositório dele falha de
    forma repetível). O encerramento → 200 com o `Offering` exato; os três não abertos ficam `cancelled`
    com `offering_closed` e o horário do encerramento, com um `dispatch.cancelled` por disparo (o token
    do encerramento como ator), junto do `offering.state_changed`; o aberto continua `open`. Depois do
    fechamento dele e da abertura prevista dos agendados, o aberto fecha no horário (`trigger: window`,
    a entrega sem resposta como `no_answer`), e nenhum cancelado abre, é validado ou entrega algo.
    Oferecimento sem disparo (na mesma função): encerrar → 200 e só o
    `offering.state_changed`."""
    engine_fake.program("number", "number")
    repo_fetcher.program(SLOW_REPO, COMMIT, FetchTemporaryFailure("dns"))
    ctx = _setup(client, sql, clock, rich_bot_api, student=11)
    plain = _setup(client, sql, clock, rich_bot_api, student=None)
    _start_worker(bot_worker_thread, settings, sql, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    opened = _prepare(client, ctx, "number", closes_at=t0 + timedelta(minutes=10))
    confirmed = _prepare(client, ctx, "number", opens_at=t0 + timedelta(hours=1),
                         closes_at=t0 + timedelta(hours=2))
    unconfirmed = _prepare(client, ctx, "number", opens_at=t0 + timedelta(hours=1),
                           closes_at=t0 + timedelta(hours=2))
    for dispatch_id in (opened, confirmed, unconfirmed):
        _wait_validated(client, rich_bot_api, ctx, dispatch_id)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, opened)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, confirmed)
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, opened)["deliveries"]["delivered"] == 1,
                          timeout=PROCESS_TIMEOUT)
    validating = _prepare(client, ctx, "number", repo=SLOW_REPO, closes_at=t0 + timedelta(minutes=30))
    rich_bot_api.wait_for(lambda: repo_fetcher.count(SLOW_REPO, COMMIT) >= 1, timeout=PROCESS_TIMEOUT)
    assert _dispatch(client, ctx, validating)["state"] == "validating"

    clock.advance(5)
    t_close = clock.now()
    cursor = _last_seq(sql)
    before = _get_offering(client, ctx.code)
    closed = _close(client, ctx.code)
    assert closed == {**before, "state": "closed", "closed_at": iso(t_close)}
    oid = ctx.offering["offering_id"]
    assert _sorted(_events_after(client, cursor)) == _sorted([
        _state_changed(ctx.offering, t_close, "active", "closed"),
        _cancelled(ctx.offering, confirmed, t_close),
        _cancelled(ctx.offering, unconfirmed, t_close),
        _cancelled(ctx.offering, validating, t_close)])
    cancelled_view = ("cancelled", "offering_closed", iso(t_close), None, None, _deliveries())
    for dispatch_id, was_confirmed in ((confirmed, True), (unconfirmed, False), (validating, False)):
        body = _dispatch(client, ctx, dispatch_id)
        assert _cancelled_view(body) == cancelled_view
        assert body["confirmed"] is was_confirmed
    still_open = _dispatch(client, ctx, opened)
    assert (still_open["state"], still_open["cancel_reason"], still_open["deliveries"]) == (
        "open", None, _deliveries(delivered=1))
    listed = list_dispatches(client, ADMIN_TOKEN, ctx.code)
    assert {item["dispatch_id"]: item["state"] for item in listed["items"]} == {
        opened: "open", confirmed: "cancelled", unconfirmed: "cancelled", validating: "cancelled"}

    # O aberto segue até o fechamento; os cancelados não abrem nem entregam nada.
    cursor = _last_seq(sql)
    t_late = clock.set(t0 + timedelta(hours=1, minutes=1))
    # Espera: a 1ª rodada do `dispatch.close` pode correr antes da consulta
    # do leitor depois do salto (barreira (b) do fechamento) e terminar sem efeito; a rodada seguinte
    # só nasce com o relógio além de `CLOSE_RECHECK_SECONDS`. O fechamento vale no horário da transição.
    body = _wait_closed_by_window(client, clock, rich_bot_api, ctx, opened)
    # Mais estrito que a faixa de 48 s: o fechamento tem de
    # ter acontecido exatamente num dos horários que o relógio falso assumiu na espera (`t_late` mais 6 s por rodada
    # sem efeito; a espera só avança o relógio entre rodadas), nunca depois do horário corrente. Não se compara com
    # `clock.now()` direto: a rodada que fecha pode gravar entre o fim de uma espera de 5 s e o avanço seguinte.
    t_closed = datetime.fromisoformat(body["closed_at"].replace("Z", "+00:00"))
    steps, rest = divmod((t_closed - t_late).total_seconds(), 6)
    assert rest == 0 and 0 <= steps <= 8 and t_closed <= clock.now(), (t_late, t_closed, clock.now())
    assert (body["closed_at"], body["deliveries"]) == (iso(t_closed), _deliveries(no_answer=1))
    for dispatch_id in (confirmed, unconfirmed, validating):
        assert _cancelled_view(_dispatch(client, ctx, dispatch_id)) == cancelled_view
    assert _events_after(client, cursor) == [
        _event("dispatch.closed", t_closed, {"offering_id": oid, "dispatch_id": opened, "trigger": "window",
                                             "counts": _counts(no_answer=1)}, actor=None)]
    delivered = sql.execute(
        "SELECT x.dispatch_uuid::text, count(d.id) FROM dispatches x LEFT JOIN deliveries d "
        "ON d.dispatch_id = x.id GROUP BY x.dispatch_uuid").fetchall()
    assert dict(delivered) == {opened: 1, confirmed: 0, unconfirmed: 0, validating: 0}

    # Sem disparo aberto ou fechado: o oferecimento sem disparo encerra normalmente.
    cursor = _last_seq(sql)
    t_plain = clock.advance(5)
    before = _get_offering(client, plain.code)
    assert _close(client, plain.code) == {**before, "state": "closed", "closed_at": iso(t_plain)}
    assert _events_after(client, cursor) == [_state_changed(plain.offering, t_plain, "active", "closed")]


# ---------------------------------------------------------------------------------------------
# Arquivamento
# ---------------------------------------------------------------------------------------------


def test_archive_refused_with_grading_pending_until_clear_then_closes_open_dispatches(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Arquivamento recusado como conflito de estado enquanto houver, em disparos do
    oferecimento, submissão aguardando correção ou update do bot gravado e não processado; aceito, fecha
    os abertos como no horário; `grading_pending`, mensagem exata, sem `fields`; a
    contagem em `dispatches.get`; a barreira (b) só na confirmação, com o horário do
    preparo lido sob a trava.

    Com um disparo aberto e o oferecimento encerrado (o disparo continua `open`):
    1. o preparo sem pendência é aceito; o aluno responde e a correção fica pendente (o motor falso
       recusa o `grade` com 500): a **confirmação** com esse código → 409 `grading_pending`, e o
       **preparo** de novo → 409 `grading_pending`; `dispatches.get` mostra `deliveries.grading_pending
       = 1` com a entrega ainda `delivered`;
    2. liberado o motor, a correção termina (`answered`); com o worker parado, um update `pending` do bot
       → o preparo → 409; processado, o preparo → 200;
    3. um update gravado **entre o preparo e a confirmação** → a confirmação → 409;
    4. com o leitor do bot vivo (posse de outro worker) e a última consulta **anterior ao preparo**: o
       preparo é aceito (a barreira (b) não vale no preparo), e a confirmação → 409;
    5. com a consulta depois do preparo, a confirmação → 200 `archived`, e o disparo aberto fecha na
       mesma hora com `trigger: archive` (a entrega respondida contada como `answered`).
    Nenhuma recusa muda o oferecimento nem o disparo."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, student=21)
    worker = _start_worker(bot_worker_thread, settings, sql, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    dispatch_id = _prepare(client, ctx, "number", closes_at=t0 + timedelta(hours=1))
    _wait_validated(client, rich_bot_api, ctx, dispatch_id)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, dispatch_id)["deliveries"]["delivered"] == 1,
                          timeout=PROCESS_TIMEOUT)
    clock.advance(5)
    closed = _close(client, ctx.code)
    assert closed["state"] == "closed"
    assert _dispatch(client, ctx, dispatch_id)["state"] == "open"

    # 1. Correção pendente: recusada na confirmação e no preparo.
    code_before = _prepared_archive(client, clock, closed)
    engine_fake.fail("grade", error=500, times=None)
    update_id = rich_bot_api.push_text(ctx.tg, ctx.student, "6.5")
    assert _processed(rich_bot_api, sql, ctx.tg, update_id)[1] == "answer_recorded"
    rich_bot_api.wait_for(lambda: engine_fake.calls("grade"), timeout=PROCESS_TIMEOUT)
    assert _submission_outcomes(sql) == ["grading"]
    cursor = _last_seq(sql)
    assert_error(_archive(client, ctx.code, code_before), 409, "grading_pending")
    assert_error(_prepare_archive(client, ctx.code), 409, "grading_pending")
    body = _dispatch(client, ctx, dispatch_id)
    assert (body["state"], body["deliveries"]) == ("open", _deliveries(grading_pending=1, delivered=1))
    assert _get_offering(client, ctx.code) == closed
    assert _events_after(client, cursor) == []

    # 2. A correção termina; com o worker parado, um update `pending` recusa o preparo.
    engine_fake.clear_failures()
    _advance_until(clock, rich_bot_api, lambda: _submission_outcomes(sql) == ["graded"])
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, dispatch_id)["deliveries"] == _deliveries(answered=1),
                          timeout=PROCESS_TIMEOUT)
    assert bot_worker_thread.stop_one(worker) < 10
    clock.advance(5)
    cursor = _last_seq(sql)
    pending = _pending_update(sql, clock, ctx)
    assert_error(_prepare_archive(client, ctx.code), 409, "grading_pending")
    _mark_processed(sql, clock, pending)
    code = _prepared_archive(client, clock, closed)

    # 3. Um update gravado entre o preparo e a confirmação recusa a confirmação.
    clock.advance(1)
    between = _pending_update(sql, clock, ctx)
    assert_error(_archive(client, ctx.code, code), 409, "grading_pending")
    _mark_processed(sql, clock, between)

    # 4. Leitor vivo com a última consulta anterior ao preparo: o preparo é aceito, a confirmação não.
    clock.advance(1)
    t_prepare = clock.now()
    assert sql.execute(
        "UPDATE bots SET lease_owner = %s, lease_until = %s, last_poll_ok_at = %s WHERE id = %s",
        (LIVE_READER, t_prepare + LEASE, t_prepare - timedelta(seconds=1), ctx.bot_row)).rowcount == 1
    code = _prepared_archive(client, clock, closed)
    clock.advance(1)
    assert_error(_archive(client, ctx.code, code), 409, "grading_pending")
    assert _get_offering(client, ctx.code) == closed
    assert _dispatch(client, ctx, dispatch_id)["state"] == "open"
    assert _events_after(client, cursor) == []

    # 5. Com a consulta depois do preparo, o arquivamento é aceito e fecha o disparo aberto.
    assert sql.execute("UPDATE bots SET last_poll_ok_at = %s, lease_until = %s WHERE id = %s",
                       (t_prepare + timedelta(seconds=1), clock.now() + LEASE, ctx.bot_row)).rowcount == 1
    t_archive = clock.advance(1)
    expected = {**_get_offering(client, ctx.code), "state": "archived", "archived_at": iso(t_archive)}
    assert _ok(_archive(client, ctx.code, code), "offerings.archive") == expected
    body = _dispatch(client, ctx, dispatch_id)
    assert (body["state"], body["closed_at"], body["deliveries"]) == (
        "closed", iso(t_archive), _deliveries(answered=1))
    events = _events_after(client, cursor)
    assert _sorted([{k: e[k] for k in ("type", "occurred_at", "data")} for e in events]) == _sorted([
        {"type": "offering.state_changed", "occurred_at": iso(t_archive),
         "data": {"offering_id": ctx.offering["offering_id"], "from_state": "closed", "to_state": "archived"}},
        {"type": "dispatch.closed", "occurred_at": iso(t_archive),
         "data": {"offering_id": ctx.offering["offering_id"], "dispatch_id": dispatch_id, "trigger": "archive",
                  "counts": _counts(answered=1)}}])


def _delivery_rows(sql: psycopg.Connection, ctx: _Ctx, person_id: int) -> list[tuple]:
    """As mensagens da fila da entrega da pessoa no oferecimento: `(id, papel, operação, estado)`, em
    ordem de `id`."""
    return sql.execute(
        "SELECT m.id, m.delivery_role, m.operation, m.state FROM outbox_messages m "
        "JOIN deliveries d ON d.id = m.delivery_id JOIN enrollments e ON e.id = d.enrollment_id "
        "WHERE e.person_id = %s AND e.offering_id = %s ORDER BY m.id", (person_id, ctx.offering_row)).fetchall()


def _delivery_state(sql: psycopg.Connection, ctx: _Ctx, person_id: int) -> str:
    return sql.execute(
        "SELECT d.state FROM deliveries d JOIN enrollments e ON e.id = d.enrollment_id "
        "WHERE e.person_id = %s AND e.offering_id = %s", (person_id, ctx.offering_row)).fetchone()[0]


def test_archive_with_open_dispatch_reveals_records_unsendable_edit_as_failure_and_ignores_later_answer(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Arquivamento com disparos abertos (aceito, ele fecha os disparos
    abertos como no horário de fechamento; o que não puder ser enviado fica registrado como falha,
    e respostas depois do arquivamento não são processadas); a revelação do
    fechamento; o que não puder ser enviado fica como falha; o 403 de
    bloqueio numa edição → só registro, sem mensagem nova; a linha continua `failed` com
    os eventos da fila; a entrega pela fila não depende da consulta, e o bot de oferecimento
    arquivado deixa de ser consultado.

    [I] O que não puder ser enviado é lido como a edição de revelação que o canal recusa de vez (o
    aluno bloqueou o bot: 403). O bot sem acesso não entra aqui: as mensagens de entrega
    dele ficam `pending` sem consumir tentativa, e nada no produto diz outra coisa para o arquivamento.

    Um disparo aberto entregue a dois alunos (nenhum responde), o oferecimento encerrado, o aluno B
    bloqueia o bot; com o leitor vivo e uma consulta depois do preparo, o arquivamento é aceito:
    - o disparo fecha na hora com `trigger: archive` e as duas entregas `no_answer`;
    - a revelação do aluno A sai (edição com `CLOSED_NOTICE`, controle positivo de que a fila segue
      depois do arquivamento);
    - a do aluno B fica registrada como falha: a edição `failed`, com o `outbox.failed` dela (o
      pseudônimo de B como destinatário), sem mensagem nova (`fallback`), e a entrega continua
      `no_answer`;
    - uma resposta de A depois do arquivamento (com "responder" à mensagem revelada) não é processada:
      nenhuma submissão, nenhuma mudança de estado, nenhum evento do ciclo de vida e nada novo no chat
      de A, enquanto o bot de outro oferecimento ativo processa um update enviado depois dela
      (controle positivo de que o worker segue lendo)."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, student=41)
    other, other_person = _link_student(client, sql, clock, ctx, 42)
    control = _setup(client, sql, clock, rich_bot_api, student=None)
    _start_worker(bot_worker_thread, settings, sql, engine_fake, repo_fetcher, rich_bot_api, ctx)
    _processed(rich_bot_api, sql, ctx.tg, rich_bot_api.push_text(ctx.tg, other, "/start", bot_command=True))
    rich_bot_api.wait_for(lambda: rich_bot_api.messages(ctx.tg, other.user_id), timeout=PROCESS_TIMEOUT)
    t0 = clock.now()
    dispatch_id = _prepare(client, ctx, "number", closes_at=t0 + timedelta(hours=1))
    _wait_validated(client, rich_bot_api, ctx, dispatch_id)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, dispatch_id)["deliveries"]["delivered"] == 2,
                          timeout=PROCESS_TIMEOUT)
    rich_bot_api.block_chat(ctx.tg, other)
    clock.advance(5)
    closed = _close(client, ctx.code)
    assert _dispatch(client, ctx, dispatch_id)["state"] == "open"

    # O arquivamento, com o leitor vivo e uma consulta depois do preparo.
    clock.advance(1)
    t_prepare = clock.now()
    code = _prepared_archive(client, clock, closed)
    rich_bot_api.wait_for(lambda: sql.execute("SELECT last_poll_ok_at FROM bots WHERE id = %s",
                                              (ctx.bot_row,)).fetchone()[0] >= t_prepare, timeout=PROCESS_TIMEOUT)
    cursor = _last_seq(sql)
    t_archive = clock.advance(1)
    expected = {**_get_offering(client, ctx.code), "state": "archived", "archived_at": iso(t_archive)}
    assert _ok(_archive(client, ctx.code, code), "offerings.archive") == expected
    body = _dispatch(client, ctx, dispatch_id)
    assert (body["state"], body["closed_at"], body["deliveries"]) == (
        "closed", iso(t_archive), _deliveries(no_answer=2))
    closed_events = [e for e in _events_after(client, cursor) if e["type"] == "dispatch.closed"]
    assert [{k: e[k] for k in ("type", "occurred_at", "data")} for e in closed_events] == [
        {"type": "dispatch.closed", "occurred_at": iso(t_archive),
         "data": {"offering_id": ctx.offering["offering_id"], "dispatch_id": dispatch_id, "trigger": "archive",
                  "counts": _counts(no_answer=2)}}]

    # A revelação de A sai; a de B (o bot bloqueado por ele) fica registrada como falha.
    def reveal_to_a() -> list:
        return [e for e in rich_bot_api.edits(ctx.tg, ctx.student.user_id)
                if e.clock_at >= t_archive and texts.CLOSED_NOTICE in (e.after.plain_text or "")]

    def settled_b() -> bool:
        rows = _delivery_rows(sql, ctx, other_person)
        return any(op == "edit_text" for _i, _r, op, _s in rows) and all(
            state not in ("pending", "leased") for _i, _r, op, state in rows if op != "send")

    _advance_until(clock, rich_bot_api, lambda: reveal_to_a() and settled_b())
    (reveal,) = reveal_to_a()
    rows_b = _delivery_rows(sql, ctx, other_person)
    edits_b = [(row_id, state) for row_id, _role, op, state in rows_b if op == "edit_text"]
    assert [state for _id, state in edits_b] == ["failed"], rows_b
    assert [role for _i, role, _o, _s in rows_b].count("fallback") == 0, rows_b
    assert rich_bot_api.edits(ctx.tg, other.user_id) == []
    pseudo_b = str(sql.execute("SELECT pseudo_id FROM people WHERE id = %s", (other_person,)).fetchone()[0])
    failures = [e["data"] for e in all_events(client, ADMIN_TOKEN, "outbox.failed")
                if e["data"]["message_id"] == edits_b[0][0]]
    assert [(d["message_id"], d["recipient"]) for d in failures] == [(edits_b[0][0], pseudo_b)]
    assert (_delivery_state(sql, ctx, ctx.person_id), _delivery_state(sql, ctx, other_person)) == (
        "no_answer", "no_answer")

    # Uma resposta de A depois do arquivamento não é processada.
    cursor = _last_seq(sql)
    submissions = sql.execute("SELECT count(*) FROM submissions").fetchone()[0]
    seen_edits = len(rich_bot_api.edits(ctx.tg, ctx.student.user_id))
    seen_messages = len(rich_bot_api.messages(ctx.tg, ctx.student.user_id))
    late = rich_bot_api.push_text(ctx.tg, ctx.student, "6.5", reply_to_message_id=reveal.message_id)
    probe = rich_bot_api.push_text(control.tg, make_user(4299), "/start", bot_command=True)
    _processed(rich_bot_api, sql, control.tg, probe)
    time.sleep(QUIET_WINDOW)  # mais que duas consultas do leitor (`bot_settings`: consulta de 1 s)
    late_row = sql.execute(
        "SELECT u.state FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (ctx.tg, late)).fetchone()
    assert late_row is None or late_row[0] != "processed"
    assert sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == submissions
    assert _dispatch(client, ctx, dispatch_id)["deliveries"] == _deliveries(no_answer=2)
    assert _delivery_state(sql, ctx, ctx.person_id) == "no_answer"
    assert _events_after(client, cursor) == []
    assert len(rich_bot_api.edits(ctx.tg, ctx.student.user_id)) == seen_edits
    assert len(rich_bot_api.messages(ctx.tg, ctx.student.user_id)) == seen_messages
    assert _get_offering(client, ctx.code) == expected


def _reveal_view(sql: psycopg.Connection, ctx: _Ctx, person_id: int) -> tuple[str, datetime | None]:
    """(estado, `revealed_at`) da entrega da pessoa no oferecimento."""
    return sql.execute(
        "SELECT d.state, d.revealed_at FROM deliveries d JOIN enrollments e ON e.id = d.enrollment_id "
        "WHERE e.person_id = %s AND e.offering_id = %s", (person_id, ctx.offering_row)).fetchone()


def _calls_with_sentinel(rich: Any, chat_id: int) -> list[int]:
    """Os `seq` das chamadas à Bot API falsa dirigidas ao chat cujos parâmetros trazem a sentinela do
    gabarito (`ANSWER_SENTINEL`, tests/helpers/engine.py)."""
    return [c.seq for c in rich.calls() if str(c.params.get("chat_id")) == str(chat_id)
            and ANSWER_SENTINEL in json.dumps(c.params, ensure_ascii=False, default=str)]


def test_archive_reveals_only_to_answered_not_to_no_answer(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O gabarito só vai a quem já respondeu:
    o arquivamento fecha o disparo aberto como no horário, mas o gabarito vai só a
    quem respondeu.

    Um disparo aberto (`number`) entregue a dois alunos: A responde "6.5" no prazo (sem "responder",
    pelo campo único), B não responde. O oferecimento é encerrado e, com o leitor vivo e uma consulta
    depois do preparo, arquivado:
    - `dispatch.closed {trigger: archive, counts: answered 1, no_answer 1}`;
    - A recebe a revelação: a edição com a sentinela do gabarito e `revealed_at == t_archive`;
    - B recebe a edição de fechamento do arquivamento com `CLOSED_NOTICE`, sem a sentinela, **sem
      botões** (nenhum "Não sei"), sem `LATE_HOWTO` e sem `DONT_KNOW_HOWTO` (nada mais será
      processado), e a entrega fica `no_answer` com `revealed_at` nulo;
    - nenhuma chamada à Bot API dirigida a B traz a sentinela.
    O defeito que o teste pega: o fechamento revelar também a `no_answer` (`revealed_at == t_archive`, a
    sentinela e o `LATE_HOWTO` na edição de B)."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, student=51)
    other, other_person = _link_student(client, sql, clock, ctx, 52)
    _start_worker(bot_worker_thread, settings, sql, engine_fake, repo_fetcher, rich_bot_api, ctx)
    _processed(rich_bot_api, sql, ctx.tg, rich_bot_api.push_text(ctx.tg, other, "/start", bot_command=True))
    rich_bot_api.wait_for(lambda: rich_bot_api.messages(ctx.tg, other.user_id), timeout=PROCESS_TIMEOUT)
    t0 = clock.now()
    dispatch_id = _prepare(client, ctx, "number", closes_at=t0 + timedelta(hours=1))
    _wait_validated(client, rich_bot_api, ctx, dispatch_id)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, dispatch_id)["deliveries"]["delivered"] == 2,
                          timeout=PROCESS_TIMEOUT)

    # A responde no prazo; B não.
    clock.advance(5)
    answer = rich_bot_api.push_text(ctx.tg, ctx.student, "6.5")
    assert _processed(rich_bot_api, sql, ctx.tg, answer)[1] == "answer_recorded"
    rich_bot_api.wait_for(
        lambda: _dispatch(client, ctx, dispatch_id)["deliveries"] == _deliveries(answered=1, delivered=1),
        timeout=PROCESS_TIMEOUT)
    clock.advance(5)
    closed = _close(client, ctx.code)
    assert _dispatch(client, ctx, dispatch_id)["state"] == "open"

    # O arquivamento, com o leitor vivo e uma consulta depois do preparo.
    clock.advance(1)
    t_prepare = clock.now()
    code = _prepared_archive(client, clock, closed)
    rich_bot_api.wait_for(lambda: sql.execute("SELECT last_poll_ok_at FROM bots WHERE id = %s",
                                              (ctx.bot_row,)).fetchone()[0] >= t_prepare, timeout=PROCESS_TIMEOUT)
    cursor = _last_seq(sql)
    t_archive = clock.advance(1)
    expected = {**_get_offering(client, ctx.code), "state": "archived", "archived_at": iso(t_archive)}
    assert _ok(_archive(client, ctx.code, code), "offerings.archive") == expected
    body = _dispatch(client, ctx, dispatch_id)
    assert (body["state"], body["closed_at"], body["deliveries"]) == (
        "closed", iso(t_archive), _deliveries(answered=1, no_answer=1))
    closed_events = [e for e in _events_after(client, cursor) if e["type"] == "dispatch.closed"]
    assert [{k: e[k] for k in ("type", "occurred_at", "data")} for e in closed_events] == [
        {"type": "dispatch.closed", "occurred_at": iso(t_archive),
         "data": {"offering_id": ctx.offering["offering_id"], "dispatch_id": dispatch_id, "trigger": "archive",
                  "counts": _counts(answered=1, no_answer=1)}}]

    # As edições do fechamento saem pela fila depois do arquivamento.
    def closing_edits(chat_id: int) -> list:
        return [e for e in rich_bot_api.edits(ctx.tg, chat_id)
                if e.clock_at >= t_archive and texts.CLOSED_NOTICE in (e.after.plain_text or "")]

    rich_bot_api.wait_for(lambda: closing_edits(ctx.student.user_id) and closing_edits(other.user_id),
                          timeout=PROCESS_TIMEOUT)

    # A (respondida): a revelação, com o gabarito (controle positivo).
    (edit_a,) = closing_edits(ctx.student.user_id)
    assert ANSWER_SENTINEL in edit_a.after.plain_text
    assert _reveal_view(sql, ctx, ctx.person_id) == ("answered", t_archive)

    # B (sem resposta): só "Janela fechada", sem gabarito, sem controles e sem os avisos de atrasada.
    (edit_b,) = closing_edits(other.user_id)
    assert _reveal_view(sql, ctx, other_person) == ("no_answer", None)
    assert ANSWER_SENTINEL not in edit_b.after.plain_text
    assert edit_b.after.buttons() == [], [b.label for b in edit_b.after.buttons()]
    assert texts.LATE_HOWTO not in edit_b.after.plain_text
    assert texts.DONT_KNOW_HOWTO not in edit_b.after.plain_text
    assert _calls_with_sentinel(rich_bot_api, other.user_id) == []


def _wait_closed_by_window(client: Any, clock: Any, rich: Any, ctx: _Ctx, dispatch_id: str, *,
                           rounds: int = 8) -> dict:
    """Espera o fechamento pelo horário (com `closes_at` já passado no relógio) e devolve o `Dispatch`
    fechado. Sem ele em 5 s reais, avança o relógio além de `CLOSE_RECHECK_SECONDS` (5 s): a barreira que
    ainda não passou (a consulta do leitor depois de `closes_at`) termina a tarefa sem efeito, e a varredura
    cria a rodada seguinte depois desse intervalo."""
    for _ in range(rounds):
        try:
            return rich.wait_for(lambda: (b := _dispatch(client, ctx, dispatch_id))["state"] == "closed" and b,
                                 timeout=5)
        except AssertionError:
            clock.advance(6)
    raise AssertionError(f"o disparo não fechou pelo horário em {rounds} rodadas")


def _edit_calls_after(rich: Any, chat_id: int, since: datetime) -> list[int]:
    """Os `seq` das chamadas de edição (`editMessage*`) à Bot API falsa dirigidas ao chat com `clock_at >=
    since`, aceitas ou não."""
    return [c.seq for c in rich.calls() if c.method.startswith("editMessage")
            and str(c.params.get("chat_id")) == str(chat_id) and c.clock_at >= since]


def test_archive_does_not_edit_or_reveal_no_answer_of_an_already_closed_dispatch(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O gabarito só vai a quem já respondeu, também no arquivamento com disparos abertos:
    no arquivamento, as entregas
    sem resposta e sem "Não sei" dos disparos **já fechados** pelo horário não recebem o gabarito nem são
    editadas de novo (as entregas dos disparos já fechados antes não são editadas no arquivamento e continuam com os
    controles da edição do horário, uma limitação conhecida).

    Um disparo `number` entregue a dois alunos: A responde "6.5" no prazo, B não responde. O relógio passa do
    fechamento, e o worker fecha o disparo pelo horário:
    - `dispatch.closed {trigger: window, counts: answered 1, no_answer 1}`, sem ator;
    - controle positivo: A recebe a revelação (a edição com `CLOSED_NOTICE` e a sentinela do gabarito,
      `revealed_at == closed_at`), e B recebe a edição de fechamento com `CLOSED_NOTICE`, sem a sentinela,
      com os controles de atrasada (o botão "Não sei" e o `LATE_HOWTO`), e a entrega fica
      `no_answer` com `revealed_at` nulo.
    O oferecimento é então encerrado e, com o leitor vivo e uma consulta depois do preparo, arquivado (200,
    `archived`). Depois do arquivamento, com o relógio além da espera máxima da fila e o bot de outro
    oferecimento ativo respondendo a um `/start` pela fila (controle positivo de que o worker e a fila
    seguem):
    - nenhuma chamada de edição ao chat de B nem ao de A com `clock_at >= t_archive`; nenhuma mensagem
      nova da fila nas entregas dos dois, e as mensagens dos dois chats continuam como estavam (B com os
      controles da edição do horário);
    - B continua `no_answer` com `revealed_at` nulo, A continua `answered` com o `revealed_at` do
      fechamento, e o disparo continua `closed` com o `closed_at` e as contagens do horário;
    - o arquivamento só gera o `offering.state_changed` (nenhum `dispatch.closed` nem outro evento do disparo);
    - nenhuma chamada à Bot API dirigida a B traz a sentinela.
    O defeito que o teste pega: o fechamento pelo horário revelar também a `no_answer` (`revealed_at` igual ao
    `closed_at` e a sentinela na edição de fechamento de B), antes de chegar ao arquivamento."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, student=61)
    other, other_person = _link_student(client, sql, clock, ctx, 62)
    control = _setup(client, sql, clock, rich_bot_api, student=None)
    _start_worker(bot_worker_thread, settings, sql, engine_fake, repo_fetcher, rich_bot_api, ctx)
    _processed(rich_bot_api, sql, ctx.tg, rich_bot_api.push_text(ctx.tg, other, "/start", bot_command=True))
    rich_bot_api.wait_for(lambda: rich_bot_api.messages(ctx.tg, other.user_id), timeout=PROCESS_TIMEOUT)
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=10)
    dispatch_id = _prepare(client, ctx, "number", closes_at=closes_at)
    _wait_validated(client, rich_bot_api, ctx, dispatch_id)
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
    rich_bot_api.wait_for(lambda: _dispatch(client, ctx, dispatch_id)["deliveries"]["delivered"] == 2,
                          timeout=PROCESS_TIMEOUT)

    # A responde no prazo; B não.
    clock.advance(5)
    answer = rich_bot_api.push_text(ctx.tg, ctx.student, "6.5")
    assert _processed(rich_bot_api, sql, ctx.tg, answer)[1] == "answer_recorded"
    rich_bot_api.wait_for(
        lambda: _dispatch(client, ctx, dispatch_id)["deliveries"] == _deliveries(answered=1, delivered=1),
        timeout=PROCESS_TIMEOUT)

    # O relógio passa do fechamento: o worker fecha o disparo pelo horário (`trigger: window`).
    cursor = _last_seq(sql)
    t_due = clock.set(closes_at + timedelta(minutes=1))
    closed_dispatch = _wait_closed_by_window(client, clock, rich_bot_api, ctx, dispatch_id)
    closed_at = closed_dispatch["closed_at"]
    assert closed_dispatch["deliveries"] == _deliveries(answered=1, no_answer=1)
    assert _events_after(client, cursor) == [
        {"type": "dispatch.closed", "occurred_at": closed_at, "actor_token_id": None,
         "data": {"offering_id": ctx.offering["offering_id"], "dispatch_id": dispatch_id, "trigger": "window",
                  "counts": _counts(answered=1, no_answer=1)}}]

    def closing_edits(chat_id: int) -> list:
        return [e for e in rich_bot_api.edits(ctx.tg, chat_id)
                if e.clock_at >= t_due and texts.CLOSED_NOTICE in (e.after.plain_text or "")]

    rich_bot_api.wait_for(lambda: closing_edits(ctx.student.user_id) and closing_edits(other.user_id),
                          timeout=PROCESS_TIMEOUT)

    # Controle positivo do horário. A (respondida): a revelação, com o gabarito.
    (edit_a,) = closing_edits(ctx.student.user_id)
    assert ANSWER_SENTINEL in edit_a.after.plain_text
    state_a, revealed_a = _reveal_view(sql, ctx, ctx.person_id)
    assert (state_a, None if revealed_a is None else iso(revealed_a)) == ("answered", closed_at)

    # B (sem resposta): a edição de fechamento do horário, sem gabarito e com os controles de atrasada.
    (edit_b,) = closing_edits(other.user_id)
    assert _reveal_view(sql, ctx, other_person) == ("no_answer", None)
    assert ANSWER_SENTINEL not in edit_b.after.plain_text
    assert texts.DONT_KNOW_BUTTON in [b.label for b in edit_b.after.buttons()], (
        [b.label for b in edit_b.after.buttons()])
    assert texts.LATE_HOWTO in edit_b.after.plain_text
    assert _calls_with_sentinel(rich_bot_api, other.user_id) == []

    # O oferecimento é encerrado e arquivado, com o leitor vivo e uma consulta depois do preparo.
    clock.advance(5)
    closed = _close(client, ctx.code)
    assert _dispatch(client, ctx, dispatch_id)["state"] == "closed"
    clock.advance(1)
    t_prepare = clock.now()
    code = _prepared_archive(client, clock, closed)
    rich_bot_api.wait_for(lambda: sql.execute("SELECT last_poll_ok_at FROM bots WHERE id = %s",
                                              (ctx.bot_row,)).fetchone()[0] >= t_prepare, timeout=PROCESS_TIMEOUT)
    rich_bot_api.wait_for(lambda: all(
        state not in ("pending", "leased") for person in (ctx.person_id, other_person)
        for _i, _r, _o, state in _delivery_rows(sql, ctx, person)), timeout=PROCESS_TIMEOUT)
    rows_a = _delivery_rows(sql, ctx, ctx.person_id)
    rows_b = _delivery_rows(sql, ctx, other_person)
    chat_a = rich_bot_api.messages(ctx.tg, ctx.student.user_id)
    chat_b = rich_bot_api.messages(ctx.tg, other.user_id)
    cursor = _last_seq(sql)
    t_archive = clock.advance(1)
    expected = {**_get_offering(client, ctx.code), "state": "archived", "archived_at": iso(t_archive)}
    assert _ok(_archive(client, ctx.code, code), "offerings.archive") == expected
    # Nenhuma edição nova na fila das entregas, na transação do arquivamento.
    assert _delivery_rows(sql, ctx, ctx.person_id) == rows_a
    assert _delivery_rows(sql, ctx, other_person) == rows_b

    # Depois do arquivamento: o relógio além da espera máxima da fila (60 s) e um controle positivo de que o
    # worker e a fila seguem (o `/start` no bot de outro oferecimento ativo é respondido pela fila).
    clock.advance(61)
    probe_user = make_user(4298)
    probe = rich_bot_api.push_text(control.tg, probe_user, "/start", bot_command=True)
    _processed(rich_bot_api, sql, control.tg, probe)
    rich_bot_api.wait_for(lambda: rich_bot_api.messages(control.tg, probe_user.user_id), timeout=PROCESS_TIMEOUT)
    time.sleep(QUIET_WINDOW)

    assert _edit_calls_after(rich_bot_api, other.user_id, t_archive) == []
    assert _edit_calls_after(rich_bot_api, ctx.student.user_id, t_archive) == []
    assert _delivery_rows(sql, ctx, other_person) == rows_b
    assert _delivery_rows(sql, ctx, ctx.person_id) == rows_a
    assert rich_bot_api.messages(ctx.tg, other.user_id) == chat_b
    assert rich_bot_api.messages(ctx.tg, ctx.student.user_id) == chat_a
    assert _reveal_view(sql, ctx, other_person) == ("no_answer", None)
    state_a, revealed_a = _reveal_view(sql, ctx, ctx.person_id)
    assert (state_a, None if revealed_a is None else iso(revealed_a)) == ("answered", closed_at)
    body = _dispatch(client, ctx, dispatch_id)
    assert (body["state"], body["closed_at"], body["deliveries"]) == (
        "closed", closed_at, _deliveries(answered=1, no_answer=1))
    assert _events_after(client, cursor) == [_state_changed(ctx.offering, t_archive, "closed", "archived")]
    assert _calls_with_sentinel(rich_bot_api, other.user_id) == []
    assert _get_offering(client, ctx.code) == expected


def test_archive_refusal_needs_an_open_or_closed_dispatch(client: Any, clock: Any, sql: psycopg.Connection,
                                                          rich_bot_api: Any) -> None:
    """A recusa do arquivamento só com disparo `open` ou `closed`;
    os oferecimentos sem disparo aberto ou fechado arquivam sem ela.

    Sem worker: num oferecimento cujo único disparo foi cancelado pelo encerramento (nunca abriu) e com
    um update `pending` do bot, o preparo e a confirmação do arquivamento são aceitos (200, `archived`),
    e o disparo continua `cancelled`. O mesmo vale para um oferecimento sem disparo
    nenhum com um update `pending` (na mesma função)."""
    t0 = clock.now()
    with_dispatch = _setup(client, sql, clock, rich_bot_api, student=31)
    dispatch_id = _prepare(client, with_dispatch, "number", closes_at=t0 + timedelta(minutes=30))
    without = _setup(client, sql, clock, rich_bot_api, student=32)

    for ctx in (with_dispatch, without):
        clock.advance(5)
        closed = _close(client, ctx.code)
        _pending_update(sql, clock, ctx)
        code = _prepared_archive(client, clock, closed)
        t_archive = clock.advance(1)
        archived = _ok(_archive(client, ctx.code, code), "offerings.archive")
        assert archived == {**closed, "state": "archived", "archived_at": iso(t_archive)}
    body = _dispatch(client, with_dispatch, dispatch_id)
    assert (body["state"], body["cancel_reason"], body["deliveries"]) == (
        "cancelled", "offering_closed", _deliveries())
