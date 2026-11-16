"""Os 14 tipos de evento de oferecimentos e bots pela API de eventos.

Cobre a auditoria (todo passo relevante gera evento na mesma transação, com pseudônimos e sem segredo) e
os tipos de oferecimentos e bots (os 14 tipos e o `data` fechado de cada um): cada tipo aparece em
`events.list` com o `data` exato do seu fluxo, o `occurred_at` do `FakeClock` e o `actor_token_id`
certo (`bootstrap` na API; `null` no worker, como na API base), e valida no esquema congelado
(`all_events` passa cada página por `assert_matches`). Os fluxos são os de oferecimentos e bots:
cadastro e ciclo de vida do oferecimento, registro e troca do token, perfil,
posse, vínculo de equipe e falhas de entrega e de acesso.

Convenções: `seq`, ids internos e `update_id` nunca como literal (os ids internos que
aparecem em `outbox.reply_failed` e em `outbox.failed` vêm do banco); horários por `iso(dt)`, com o
relógio avançado entre os passos só depois de cada efeito observado; esperas com prazo, sobre o banco
ou a Bot API falsa (`wait_for`), nunca `sleep` fixo como prova.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg

from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, auth, create_offering,
                               create_team_link, link_code, register_bot, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches

# Os 14 tipos de oferecimentos e bots, escritos aqui (e não importados de `EventType`).
OFFERING_BOT_TYPES = (
    "offering.created",
    "offering.state_changed",
    "offering.archive_prepared",
    "bot.registered",
    "bot.token_replaced",
    "bot.profile_updated",
    "bot.lease_changed",
    "bot.access_changed",
    "team.link_created",
    "team.link_revoked",
    "team.joined",
    "team.join_refused",
    "endpoint.blocked",
    "outbox.reply_failed",
)

# Disciplina e período neutros de `create_offering`, e os textos fixos do bot.
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
WELCOME_TEAM = (f"Pronto! Você faz parte da equipe do oferecimento {COURSE} ({TERM}). Por este chat "
                "você vai receber os avisos do oferecimento.")
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."
OTHER_PERSON = ("Esta conta do Telegram já está vinculada a outra pessoa neste oferecimento. Fale com "
                "a coordenação.")

# Código de convite com o formato certo (22 caracteres base64url) que nenhum convite tem.
UNKNOWN_INVITE_CODE = "Zz0_-Zz0_-Zz0_-Zz0_-Aa"
WORKER_ID = "worker-eventos-ofertas"
LINK_HOURS = timedelta(hours=168)
ARCHIVE_CODE_MINUTES = timedelta(minutes=10)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _events(client: Any, *types: str) -> list[dict]:
    """Os eventos dos tipos dados (validados no esquema), sem `seq`, conferido o `seq` crescente."""
    items = all_events(client, ADMIN_TOKEN, *types)
    seqs = [item["seq"] for item in items]
    assert all(isinstance(s, int) and not isinstance(s, bool) for s in seqs), seqs
    assert all(a < b for a, b in zip(seqs, seqs[1:], strict=False)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in item.items() if k != "seq"} for item in items]


def _event(event_type: str, at: Any, actor: str | None, **data: Any) -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _post(client: Any, op_id: str, path: str, body: Any = None) -> dict:
    """`POST` com o token inicial; afirma 200 e o esquema congelado."""
    resp = client.post(path, headers=auth(ADMIN_TOKEN), **({} if body is None else {"json": body}))
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    out = resp.json()
    assert_matches(op_id, 200, out)
    return out


def _wait_rows(sql: psycopg.Connection, query: str, predicate: Callable[[list[tuple]], bool],
               message: str, timeout: float = 10.0) -> list[tuple]:
    """Espera (real, com prazo; threads do worker) que as linhas de `query` satisfaçam
    `predicate`; devolve as linhas."""
    deadline = time.monotonic() + timeout
    while True:
        rows = [tuple(row) for row in sql.execute(query).fetchall()]
        if predicate(rows):
            return rows
        assert time.monotonic() < deadline, f"{message}; última leitura: {rows}"
        time.sleep(0.02)


def _bot_summary(bot_api: Any, bot: int, profile_state: str = "pending") -> dict:
    return {"username": bot_api.state(bot).username, "telegram_bot_id": bot, "state": "registered",
            "profile_state": profile_state}


def _active_offering(client: Any, bot_api: Any, code: str) -> tuple[dict, int]:
    """Oferecimento com bot registrado na Bot API falsa e ativado (tudo no instante atual)."""
    offering = create_offering(client, ADMIN_TOKEN, code=code)
    assert (offering["course"], offering["term"]) == (COURSE, TERM)
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _start_worker(bot_worker_thread: Any, settings: Any) -> Any:
    """O worker com bots, com um `worker_id` conhecido (o do `bot.lease_changed`)."""
    return bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID))


def _wait_ready(sql: psycopg.Connection, bot_api: Any, bot: int) -> None:
    """O perfil já foi configurado, este worker tem a posse e a consulta começou."""
    _wait_rows(sql, "SELECT state FROM tasks WHERE task_type = 'bot.sync_profile'",
               lambda rows: rows == [("done",)], "o perfil do bot não foi configurado em 10 s")
    _wait_rows(sql, "SELECT lease_owner FROM bots", lambda rows: rows == [(WORKER_ID,)],
               "o worker não assumiu a posse em 10 s")
    bot_api.wait_for(lambda: bot_api.calls("getUpdates", bot_id=bot), timeout=10)


def _wait_sent(bot_api: Any, bot: int, count: int) -> list[tuple[int, str]]:
    """Espera `count` mensagens aceitas pelo `sendMessage` do bot; devolve `(chat_id, texto)`."""
    bot_api.wait_for(lambda: len(bot_api.sent_messages(bot)) >= count, timeout=10)
    return [(m.chat_id, m.text) for m in bot_api.sent_messages(bot)]


# ---------------------------------------------------------------------------------------------
# Pela API: oferecimento, bot e link de equipe
# ---------------------------------------------------------------------------------------------


def test_api_flow_events_are_exact(client: Any, clock: Any, bot_api: Any) -> None:
    """Os eventos dos fluxos pela API, com os tipos e os dados de oferecimentos e bots:
    cadastro (`offering.created`), registro (`bot.registered`), troca do token do mesmo bot
    (`bot.token_replaced`, versão 2), link de equipe e link novo para a mesma pessoa
    (`team.link_created`; `team.link_revoked` `replaced` antes do novo `team.link_created`),
    preparação no rascunho (`offering.archive_prepared`, validade de 10 min), ativação, encerramento,
    nova preparação e arquivamento (o convite pendente revogado com `archived` antes do
    `offering.state_changed` final) geram exatamente estes 12 eventos, em ordem de `seq`, com
    `actor_token_id = "bootstrap"`, o `occurred_at` de cada passo e o `data` exato."""
    t0 = clock.now()
    offering = create_offering(client, ADMIN_TOKEN, code="eventos-ofertas-api")
    oid, code = offering["offering_id"], offering["code"]

    t1 = clock.advance(60)
    bot = bot_api.add_bot()
    registered = register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    username = bot_api.state(bot).username
    bot_body = {"offering": {"offering_id": oid, "code": code}, "username": username,
                "telegram_bot_id": bot, "state": "registered", "profile_state": "pending",
                "registered_at": iso(t1), "token_replaced_at": None, "lease": None,
                "last_poll_ok_at": None, "problem": None}
    assert registered == bot_body

    t2 = clock.advance(60)
    replaced = register_bot(client, ADMIN_TOKEN, code, bot_api.rotate_token(bot))
    assert replaced == {**bot_body, "token_replaced_at": iso(t2)}

    t3 = clock.advance(60)
    person = register_person(client, ADMIN_TOKEN, name="Professora Dos Eventos")
    create_team_link(client, ADMIN_TOKEN, code, person["alias"])
    t4 = clock.advance(60)
    create_team_link(client, ADMIN_TOKEN, code, person["alias"])

    summary = _bot_summary(bot_api, bot)
    t5 = clock.advance(60)
    prepared_draft = _post(client, "offerings.prepare_archive", f"/v1/offerings/{code}/archive/prepare")
    assert prepared_draft == {"offering": {**offering, "bot": summary}, "team_members_active": 0,
                              "pending_links": 1,
                              "confirmation_code": prepared_draft["confirmation_code"],
                              "expires_at": iso(t5 + ARCHIVE_CODE_MINUTES)}

    t6 = clock.advance(60)
    assert activate_bot(client, ADMIN_TOKEN, code) == {**offering, "state": "active",
                                                       "activated_at": iso(t6), "bot": summary}
    t7 = clock.advance(60)
    active = {**offering, "state": "active", "activated_at": iso(t6), "bot": summary}
    closed = {**active, "state": "closed", "closed_at": iso(t7)}
    assert _post(client, "offerings.close", f"/v1/offerings/{code}/close") == closed
    t8 = clock.advance(60)
    prepared = _post(client, "offerings.prepare_archive", f"/v1/offerings/{code}/archive/prepare")
    assert prepared == {"offering": closed, "team_members_active": 0, "pending_links": 1,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(t8 + ARCHIVE_CODE_MINUTES)}
    t9 = clock.advance(60)
    archived = _post(client, "offerings.archive", f"/v1/offerings/{code}/archive",
                     {"confirmation_code": prepared["confirmation_code"]})
    assert archived == {**closed, "state": "archived", "archived_at": iso(t9)}

    pseudo = person["pseudo_id"]
    assert _events(client, *OFFERING_BOT_TYPES) == [
        _event("offering.created", t0, "bootstrap", offering_id=oid, code=code),
        _event("bot.registered", t1, "bootstrap", offering_id=oid, telegram_bot_id=bot,
               username=username),
        _event("bot.token_replaced", t2, "bootstrap", offering_id=oid, telegram_bot_id=bot,
               token_version=2),
        _event("team.link_created", t3, "bootstrap", offering_id=oid, person=pseudo,
               expires_at=iso(t3 + LINK_HOURS)),
        _event("team.link_revoked", t4, "bootstrap", offering_id=oid, person=pseudo,
               revoke_reason="replaced"),
        _event("team.link_created", t4, "bootstrap", offering_id=oid, person=pseudo,
               expires_at=iso(t4 + LINK_HOURS)),
        _event("offering.archive_prepared", t5, "bootstrap", offering_id=oid,
               expires_at=iso(t5 + ARCHIVE_CODE_MINUTES)),
        _event("offering.state_changed", t6, "bootstrap", offering_id=oid, from_state="draft",
               to_state="active"),
        _event("offering.state_changed", t7, "bootstrap", offering_id=oid, from_state="active",
               to_state="closed"),
        _event("offering.archive_prepared", t8, "bootstrap", offering_id=oid,
               expires_at=iso(t8 + ARCHIVE_CODE_MINUTES)),
        _event("team.link_revoked", t9, "bootstrap", offering_id=oid, person=pseudo,
               revoke_reason="archived"),
        _event("offering.state_changed", t9, "bootstrap", offering_id=oid, from_state="closed",
               to_state="archived"),
    ]


# ---------------------------------------------------------------------------------------------
# Pelo worker: perfil e posse
# ---------------------------------------------------------------------------------------------


def test_profile_and_lease_events_are_exact(client: Any, clock: Any, bot_api: Any, sql: Any,
                                            settings: Any, bot_worker_thread: Any,
                                            stop_one: Any) -> None:
    """Perfil e posse do bot: o worker
    configura o perfil (`bot.profile_updated` `configured`/`active`) e assume a posse
    (`bot.lease_changed` `acquired`/`acquired`, com o seu `worker_id`), os dois com `actor_token_id`
    nulo; o arquivamento pela API, com o bot em posse, solta a posse na mesma transação
    (`bot.lease_changed` `released`/`archived`, com o `worker_id` do dono e o autor `bootstrap`), e a
    tarefa do perfil arquivado gera `bot.profile_updated` `configured`/`archived`. A parada seguinte
    não gera outro `bot.lease_changed` (a posse já não era dele)."""
    t0 = clock.now()
    offering, bot = _active_offering(client, bot_api, "eventos-ofertas-posse")
    oid, code = offering["offering_id"], offering["code"]
    worker = _start_worker(bot_worker_thread, settings)
    _wait_ready(sql, bot_api, bot)

    t1 = clock.advance(5)
    closed = {**offering, "state": "closed", "activated_at": iso(t0), "closed_at": iso(t1),
              "bot": _bot_summary(bot_api, bot, "configured")}
    assert _post(client, "offerings.close", f"/v1/offerings/{code}/close") == closed
    t2 = clock.advance(5)
    prepared = _post(client, "offerings.prepare_archive", f"/v1/offerings/{code}/archive/prepare")
    assert prepared == {"offering": closed, "team_members_active": 0, "pending_links": 0,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(t2 + ARCHIVE_CODE_MINUTES)}
    t3 = clock.advance(5)
    archived = _post(client, "offerings.archive", f"/v1/offerings/{code}/archive",
                     {"confirmation_code": prepared["confirmation_code"]})
    # O `profile_state` logo depois do arquivamento é de test_offerings_lifecycle.py (a tarefa do
    # perfil arquivado ainda vai rodar); aqui, todo o resto do corpo.
    assert {**archived, "bot": {k: v for k, v in archived["bot"].items() if k != "profile_state"}} == {
        **closed, "state": "archived", "archived_at": iso(t3),
        "bot": {k: v for k, v in closed["bot"].items() if k != "profile_state"}}
    _wait_rows(sql, "SELECT state FROM tasks WHERE task_type = 'bot.sync_profile' ORDER BY id",
               lambda rows: rows == [("done",), ("done",)],
               "a tarefa do perfil arquivado não terminou em 10 s")
    assert stop_one(worker) <= 10
    assert worker.error is None

    assert _events(client, "bot.profile_updated") == [
        _event("bot.profile_updated", t0, None, offering_id=oid, profile_state="configured",
               purpose="active"),
        _event("bot.profile_updated", t3, None, offering_id=oid, profile_state="configured",
               purpose="archived"),
    ]
    assert _events(client, "bot.lease_changed") == [
        _event("bot.lease_changed", t0, None, offering_id=oid, worker_id=WORKER_ID,
               change="acquired", reason="acquired"),
        _event("bot.lease_changed", t3, "bootstrap", offering_id=oid, worker_id=WORKER_ID,
               change="released", reason="archived"),
    ]


def test_access_events_are_exact(client: Any, clock: Any, bot_api: Any, sql: Any, settings: Any,
                                 bot_worker_thread: Any, stop_one: Any) -> None:
    """Um evento de perda de acesso por episódio, a troca pelo mesmo bot tira
    do `no_access`, e a parada ordenada solta a posse:
    com o token revogado no meio da consulta, o worker solta a posse e perde o acesso
    (`bot.lease_changed` `released`/`no_access` antes de `bot.access_changed` `lost`/`token_rejected`,
    sem autor); o registro do token girado pela API gera `bot.token_replaced` (versão 2) e
    `bot.access_changed` `restored` com `problem` nulo (autor `bootstrap`); o worker reassume
    (`acquired`) e, na parada ordenada, solta (`released`/`shutdown`)."""
    t0 = clock.now()
    offering, bot = _active_offering(client, bot_api, "eventos-ofertas-acesso")
    oid, code = offering["offering_id"], offering["code"]
    worker = _start_worker(bot_worker_thread, settings)
    _wait_ready(sql, bot_api, bot)

    t1 = clock.advance(5)
    bot_api.revoke(bot)
    _wait_rows(sql, "SELECT state, access_problem, lease_owner FROM bots",
               lambda rows: rows == [("no_access", "token_rejected", None)],
               "o bot não ficou sem acesso em 10 s")

    t2 = clock.advance(5)
    register_bot(client, ADMIN_TOKEN, code, bot_api.rotate_token(bot))
    _wait_rows(sql, "SELECT state, lease_owner FROM bots",
               lambda rows: rows == [("registered", WORKER_ID)], "o worker não reassumiu a posse em 10 s")
    bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.token_version == 2],
                     timeout=10)

    t3 = clock.advance(5)
    assert stop_one(worker) <= 10
    assert worker.error is None

    assert _events(client, "bot.lease_changed", "bot.access_changed", "bot.token_replaced") == [
        _event("bot.lease_changed", t0, None, offering_id=oid, worker_id=WORKER_ID,
               change="acquired", reason="acquired"),
        _event("bot.lease_changed", t1, None, offering_id=oid, worker_id=WORKER_ID,
               change="released", reason="no_access"),
        _event("bot.access_changed", t1, None, offering_id=oid, access="lost",
               problem="token_rejected"),
        _event("bot.token_replaced", t2, "bootstrap", offering_id=oid, telegram_bot_id=bot,
               token_version=2),
        _event("bot.access_changed", t2, "bootstrap", offering_id=oid, access="restored",
               problem=None),
        _event("bot.lease_changed", t2, None, offering_id=oid, worker_id=WORKER_ID,
               change="acquired", reason="acquired"),
        _event("bot.lease_changed", t3, None, offering_id=oid, worker_id=WORKER_ID,
               change="released", reason="shutdown"),
    ]


# ---------------------------------------------------------------------------------------------
# Pelo worker: vínculo de equipe e falhas de entrega
# ---------------------------------------------------------------------------------------------


def test_team_events_are_exact(client: Any, clock: Any, bot_api: Any, sql: Any, settings: Any,
                               bot_worker_thread: Any) -> None:
    """Vínculo de equipe; troca de conta do professor e
    conta do Telegram já vinculada: o `/start` com o link vincula
    (`team.joined`, `endpoint_replaced` falso); um código desconhecido é recusado (`team.join_refused`
    `invalid`); um link novo da mesma pessoa usado por outra conta troca o endereço (`team.joined`,
    `endpoint_replaced` verdadeiro), sem revogar o link já usado; o link de outra pessoa usado pela
    conta vinculada é recusado e revogado (`team.link_revoked` `refused` antes de `team.join_refused`
    `other_person`). Os eventos do worker não têm autor; os do `team.link`, `bootstrap`. As respostas
    exatas vão ao chat de cada `/start`."""
    t0 = clock.now()
    offering, bot = _active_offering(client, bot_api, "eventos-ofertas-equipe")
    oid, code = offering["offering_id"], offering["code"]
    teacher = register_person(client, ADMIN_TOKEN, name="Professora Dos Eventos")
    monitor = register_person(client, ADMIN_TOKEN, name="Monitora Dos Eventos")
    first_link = create_team_link(client, ADMIN_TOKEN, code, teacher["alias"])["link"]
    first_account, second_account = make_user(401), make_user(402)
    _start_worker(bot_worker_thread, settings)
    _wait_ready(sql, bot_api, bot)

    bot_api.push_start(bot, first_account, link_code(first_link))
    assert _wait_sent(bot_api, bot, 1) == [(first_account.user_id, WELCOME_TEAM)]

    t1 = clock.advance(5)
    bot_api.push_start(bot, second_account, UNKNOWN_INVITE_CODE)
    assert _wait_sent(bot_api, bot, 2)[1:] == [(second_account.user_id, LINK_INVALID)]

    t2 = clock.advance(5)
    second_link = create_team_link(client, ADMIN_TOKEN, code, teacher["alias"])["link"]
    bot_api.push_start(bot, second_account, link_code(second_link))
    assert _wait_sent(bot_api, bot, 3)[2:] == [(second_account.user_id, WELCOME_TEAM)]

    t3 = clock.advance(5)
    monitor_link = create_team_link(client, ADMIN_TOKEN, code, monitor["alias"])["link"]
    bot_api.push_start(bot, second_account, link_code(monitor_link))
    assert _wait_sent(bot_api, bot, 4)[3:] == [(second_account.user_id, OTHER_PERSON)]
    _wait_rows(sql, "SELECT state, outcome FROM telegram_updates ORDER BY id",
               lambda rows: len(rows) == 4 and all(state == "processed" for state, _ in rows),
               "os 4 updates não foram processados em 10 s")
    assert [row[0] for row in sql.execute(
        "SELECT outcome FROM telegram_updates ORDER BY id").fetchall()] == [
        "team_joined", "team_refused_invalid", "team_endpoint_replaced", "team_refused_other_person"]
    bot_worker_thread.stop()

    teacher_id, monitor_id = teacher["pseudo_id"], monitor["pseudo_id"]
    assert _events(client, "team.link_created", "team.link_revoked", "team.joined",
                   "team.join_refused") == [
        _event("team.link_created", t0, "bootstrap", offering_id=oid, person=teacher_id,
               expires_at=iso(t0 + LINK_HOURS)),
        _event("team.joined", t0, None, offering_id=oid, person=teacher_id, endpoint_replaced=False),
        _event("team.join_refused", t1, None, offering_id=oid, refusal="invalid"),
        _event("team.link_created", t2, "bootstrap", offering_id=oid, person=teacher_id,
               expires_at=iso(t2 + LINK_HOURS)),
        _event("team.joined", t2, None, offering_id=oid, person=teacher_id, endpoint_replaced=True),
        _event("team.link_created", t3, "bootstrap", offering_id=oid, person=monitor_id,
               expires_at=iso(t3 + LINK_HOURS)),
        _event("team.link_revoked", t3, None, offering_id=oid, person=monitor_id,
               revoke_reason="refused"),
        _event("team.join_refused", t3, None, offering_id=oid, refusal="other_person"),
    ]


def test_delivery_failure_events_are_exact(client: Any, clock: Any, bot_api: Any, sql: Any,
                                           settings: Any, bot_worker_thread: Any) -> None:
    """Bloqueio (a mensagem falha de vez, com evento, e o endereço fica
    bloqueado): com o chat do professor bloqueado, as
    boas-vindas falham de vez (`outbox.failed`, com o pseudônimo, antes de `endpoint.blocked`); com o
    chat de quem não está vinculado bloqueado, a resposta `LINK_INVALID` falha de vez
    (`outbox.reply_failed`, com o `id` da mensagem e o do update respondido). Todos sem autor, com
    `error = "chat_unreachable"` e 1 tentativa."""
    t0 = clock.now()
    offering, bot = _active_offering(client, bot_api, "eventos-ofertas-entrega")
    oid, code = offering["offering_id"], offering["code"]
    teacher = register_person(client, ADMIN_TOKEN, name="Professora Dos Eventos")
    link = create_team_link(client, ADMIN_TOKEN, code, teacher["alias"])["link"]
    teacher_account, stranger = make_user(411), make_user(412)
    bot_api.block_chat(bot, teacher_account)
    bot_api.block_chat(bot, stranger)
    _start_worker(bot_worker_thread, settings)
    _wait_ready(sql, bot_api, bot)

    bot_api.push_start(bot, teacher_account, link_code(link))
    _wait_rows(sql, "SELECT state FROM outbox_messages ORDER BY id", lambda rows: rows == [("failed",)],
               "as boas-vindas não falharam de vez em 10 s")
    t1 = clock.advance(5)
    bot_api.push_start(bot, stranger, UNKNOWN_INVITE_CODE)
    _wait_rows(sql, "SELECT state FROM outbox_messages ORDER BY id",
               lambda rows: rows == [("failed",), ("failed",)], "a resposta não falhou de vez em 10 s")
    bot_worker_thread.stop()

    updates = [row[0] for row in sql.execute("SELECT id FROM telegram_updates ORDER BY id").fetchall()]
    messages = sql.execute("SELECT id, reply_to_update_id FROM outbox_messages ORDER BY id").fetchall()
    assert len(updates) == 2 and [reply for _, reply in messages] == [None, updates[1]]
    assert sql.execute("SELECT state FROM delivery_endpoints").fetchall() == [("blocked",)]
    assert bot_api.sent_messages(bot) == []

    assert _events(client, "outbox.failed", "endpoint.blocked", "outbox.reply_failed") == [
        _event("outbox.failed", t0, None, message_id=messages[0][0], recipient=teacher["pseudo_id"],
               attempts=1, error="chat_unreachable"),
        _event("endpoint.blocked", t0, None, offering_id=oid, person=teacher["pseudo_id"],
               channel="telegram"),
        _event("outbox.reply_failed", t1, None, message_id=messages[1][0], reply_to=updates[1],
               attempts=1, error="chat_unreachable"),
    ]


# ---------------------------------------------------------------------------------------------
# Contra o vazio
# ---------------------------------------------------------------------------------------------


def test_each_offering_bot_type_is_accepted_and_covered_by_a_flow_above(client: Any) -> None:
    """Tipo desconhecido é `invalid_value` na API base; com os tipos de oferecimentos e bots, a
    consulta com os 14 tipos é aceita e, sem nenhum fluxo, vem vazia (`next_cursor == 0`);
    controle contra o vazio: cada um dos 14 tipos é esperado, por nome, em algum teste deste arquivo,
    e o único tipo esperado fora de `OFFERING_BOT_TYPES` é o `outbox.failed` da API base (a ordem antes de
    `endpoint.blocked`)."""
    resp = client.get("/v1/events", params=[("types", t) for t in OFFERING_BOT_TYPES], headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, resp.text[:300]
    assert_matches("events.list", 200, resp.json())
    assert resp.json() == {"items": [], "next_cursor": 0}

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    expected = set(re.findall(r'_event\("([a-z_.]+)"', source))
    assert expected == {*OFFERING_BOT_TYPES, "outbox.failed"}
