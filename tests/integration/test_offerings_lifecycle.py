"""Encerramento e arquivamento do oferecimento.

Cobre o encerramento e o arquivamento do oferecimento, com os efeitos do arquivamento;
a transição repetida, a que tentaria voltar atrás e o arquivamento com o bot sem acesso; e a
regra de que mensagens já na fila de um bot arquivado continuam sendo entregues. O que se confere:
transições e
ordem dos eventos, perfil e textos fixos, posse, recepção e
processamento no oferecimento encerrado.

O worker roda em thread (`bot_worker_thread`, com o `FakeClock` do teste e um `worker_id` fixo) contra
a Bot API falsa; as esperas são prazos do dublê (`bot_api.wait_for`), nunca `sleep` como prova. Os
horários vêm do `FakeClock` (`iso`); ids internos e `seq` nunca como literal. Os eventos são lidos
pela API (`events.list`, esquema congelado) depois de um cursor, por inteiro e na ordem de `seq`.

Os textos esperados do perfil e das mensagens são fixos e ficam escritos aqui por extenso
(a disciplina e o período padrão de `create_offering` não têm identidade, então a redação não os
muda).
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import re
import secrets
import uuid
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    create_offering,
    create_team_link,
    link_code,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo

ADMIN = auth(ADMIN_TOKEN)
WORKER = "worker-ciclo-de-vida"
LEASE = timedelta(seconds=30)  # `lease_seconds` do `bot_settings` do conftest
CONFIRMATION = timedelta(minutes=10)
CONFIRMATION_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

# Disciplina e período padrão de `create_offering` (tests/helpers/api.py).
COURSE = "Algoritmos e Programação"
TERM = "2031s1"

# Textos fixos do perfil e das mensagens.
DESCRIPTION_ACTIVE = (f"Bot do oferecimento {COURSE} ({TERM}). Para participar, toque no link de convite "
                      "que você recebeu da coordenação.")
DESCRIPTION_ARCHIVED = f"O oferecimento {COURSE} ({TERM}) foi encerrado. Este bot não recebe mais mensagens."
COMMANDS = [{"command": "start", "description": "Começar ou usar um link de convite"},
            {"command": "ajuda", "description": "Ver como usar este bot"}]
WELCOME_TEAM = (f"Pronto! Você faz parte da equipe do oferecimento {COURSE} ({TERM}). Por este chat você "
                "vai receber os avisos do oferecimento.")
TOKEN_REJECTED_MESSAGE = "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o."
HELP = (f"Este é o bot do oferecimento {COURSE} ({TERM}). Para participar, toque no link de convite que você "
        "recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")

# Todos os tipos de evento (da API base e de oferecimentos e bots), para ler o log inteiro depois de um cursor.
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
ALL_EVENT_TYPES = CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


def _worker_settings(settings: Any) -> Any:
    return dataclasses.replace(settings, worker_id=WORKER)


def _offering_with_bot(client: Any, bot_api: Any, **fields: Any) -> tuple[dict, dict]:
    """Um oferecimento em rascunho com um bot novo da Bot API falsa registrado; devolve os corpos de
    `offerings.create` e de `bots.register`."""
    offering = create_offering(client, ADMIN_TOKEN, **fields)
    telegram_bot_id = bot_api.add_bot()
    bot = register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(telegram_bot_id))
    assert bot["telegram_bot_id"] == telegram_bot_id
    return offering, bot


def _summary(bot: dict, profile_state: str, state: str = "registered") -> dict:
    """O resumo `bot` do `Offering`."""
    return {"username": bot["username"], "telegram_bot_id": bot["telegram_bot_id"], "state": state,
            "profile_state": profile_state}


def _offering(created: dict, *, state: str, activated_at: str | None = None, closed_at: str | None = None,
              archived_at: str | None = None, bot: dict | None = None) -> dict:
    """O `Offering` esperado a partir do corpo do cadastro."""
    return {**created, "state": state, "activated_at": activated_at, "closed_at": closed_at,
            "archived_at": archived_at, "bot": bot}


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _close(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/close", headers=ADMIN)


def _prepare(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN)


def _confirm(client: Any, ref: str, code: Any) -> Any:
    return client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": code}, headers=ADMIN)


def _activate(client: Any, ref: str) -> Any:
    return client.post(f"/v1/offerings/{ref}/bot/activate", headers=ADMIN)


def _get_offering(client: Any, ref: str) -> dict:
    return _ok(client.get(f"/v1/offerings/{ref}", headers=ADMIN), "offerings.get")


def _get_bot(client: Any, ref: str) -> dict:
    return _ok(client.get(f"/v1/offerings/{ref}/bot", headers=ADMIN), "bots.get")


def _prepared(client: Any, ref: str) -> dict:
    body = _ok(_prepare(client, ref), "offerings.prepare_archive")
    assert CONFIRMATION_RE.fullmatch(body["confirmation_code"]), body["confirmation_code"]
    return body


def _last_seq(client: Any) -> int:
    events = all_events(client, ADMIN_TOKEN, *ALL_EVENT_TYPES)
    return events[-1]["seq"] if events else 0


def _events_after(client: Any, cursor: int, types: tuple[str, ...] = ALL_EVENT_TYPES) -> list[dict]:
    """Os eventos depois do cursor, em ordem de `seq` (crescente), sem o `seq`."""
    events = [e for e in all_events(client, ADMIN_TOKEN, *types) if e["seq"] > cursor]
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _event(event_type: str, at: Any, data: dict, actor: str | None = "bootstrap") -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _state_changed(offering: dict, at: Any, from_state: str, to_state: str) -> dict:
    return _event("offering.state_changed", at, {"offering_id": offering["offering_id"],
                                                 "from_state": from_state, "to_state": to_state})


def _polls_after(bot_api: Any, telegram_bot_id: int, mark: float) -> list[Any]:
    return [c for c in bot_api.calls("getUpdates", bot_id=telegram_bot_id) if c.started > mark]


def _wait_polled(bot_api: Any, telegram_bot_id: int) -> None:
    bot_api.wait_for(lambda: bot_api.calls("getUpdates", bot_id=telegram_bot_id), timeout=10)


def _wait_profile(client: Any, bot_api: Any, ref: str, profile_state: str) -> None:
    bot_api.wait_for(lambda: _get_offering(client, ref)["bot"]["profile_state"] == profile_state,
                     timeout=10)


def _wait_profile_tasks_finished(bot_api: Any, sql: psycopg.Connection) -> None:
    """O worker grava o perfil antes de concluir a tarefa `bot.sync_profile`: antes de um salto do relógio maior que a
    posse da tarefa, espera nenhuma tarefa do perfil pendente ou em posse (senão a posse vence e a tarefa é refeita).
    O `pending` entra de propósito: uma tarefa ainda por tomar também correria depois do salto."""
    bot_api.wait_for(lambda: sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'bot.sync_profile' "
                                         "AND state IN ('pending', 'leased')").fetchone()[0] == 0, timeout=10)


UPDATE_COLUMNS = ("update_epoch", "update_id", "kind", "chat_id", "chat_type", "sender_id", "text", "command",
                  "details", "received_at", "state", "outcome", "processed_at")


def _update_rows(sql: psycopg.Connection, offering_id: str) -> list[dict]:
    """As linhas de `telegram_updates` do bot do oferecimento, em ordem de gravação, com o `id` e sem
    o `bot_id`."""
    cursor = sql.execute(
        f"SELECT u.id, {', '.join('u.' + c for c in UPDATE_COLUMNS)} FROM telegram_updates u "
        "JOIN bots b ON b.id = u.bot_id JOIN offerings o ON o.id = b.offering_id "
        "WHERE o.offering_id = %s ORDER BY u.id", (uuid.UUID(offering_id),))
    return [dict(zip(("id", *UPDATE_COLUMNS), row)) for row in cursor.fetchall()]


def _update_tasks(sql: psycopg.Connection) -> list[tuple]:
    """(chave, payload, estado) das tarefas `telegram.update`,
    em ordem de criação."""
    return [tuple(row) for row in sql.execute(
        "SELECT idempotency_key, payload, state FROM tasks WHERE task_type = 'telegram.update' ORDER BY id")]


def _outbox_rows(sql: psycopg.Connection) -> list[tuple]:
    """(pessoa, bot, endereço, update, texto, estado) de cada mensagem da saída,
    em ordem."""
    return [tuple(row) for row in sql.execute(
        "SELECT recipient_person_id, bot_id, delivery_endpoint_id, reply_to_update_id, text, state "
        "FROM outbox_messages ORDER BY id")]


def _bot_row(sql: psycopg.Connection, offering_id: str) -> int:
    return sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id "
                       "WHERE o.offering_id = %s", (uuid.UUID(offering_id),)).fetchone()[0]


def _delivered_message(bot_api: Any, telegram_bot_id: int, update_id: int) -> dict:
    """O `message` do update que a Bot API falsa entregou ao sistema (resposta `ok` de `getUpdates`)."""
    for call in bot_api.calls("getUpdates", bot_id=telegram_bot_id):
        if call.outcome == "ok":
            for update in call.result:
                if update["update_id"] == update_id:
                    return update["message"]
    raise AssertionError(f"o update {update_id} não foi entregue\n{bot_api.transcript()}")


def _stored_archive(sql: psycopg.Connection, code: str) -> tuple:
    return sql.execute(
        "SELECT state, archived_at, archive_code_sha256, archive_code_expires_at FROM offerings "
        "WHERE code = %s", (code,)).fetchone()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tables_with_text(sql: psycopg.Connection, text: str) -> list[str]:
    """As tabelas cujo texto de alguma linha (`row_to_json`) contém `text` (do
    código de confirmação, só o resumo é guardado)."""
    tables = [row[0] for row in sql.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE' ORDER BY table_name")]
    return [t for t in tables if sql.execute(
        f'SELECT count(*) FROM "{t}" x WHERE strpos(row_to_json(x)::text, %s) > 0', (text,)).fetchone()[0]]


# ---------------------------------------------------------------------------------------------
# Encerramento
# ---------------------------------------------------------------------------------------------


def test_close_keeps_the_bot_polled(client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any,
                                    settings: Any, bot_worker_thread: Any) -> None:
    """Sem parar a recepção; e a transição repetida:
    encerrar um oferecimento ativo → 200 com o `Offering` exato (`closed`, `closed_at` do relógio) e
    um `offering.state_changed {active → closed}`; o bot continua sendo consultado pelo mesmo dono
    (novos `getUpdates` depois do encerramento, posse renovada, `last_poll_ok_at` atual); um `/ajuda`
    enviado depois do encerramento é gravado e processado (linha exata
    `processed`/`help`, uma tarefa `telegram.update` concluída, a resposta `HELP` pela saída, ao chat
    do update, `sendMessage` só com `chat_id` e `text`) sem gerar evento; encerrar de
    novo → 200 com o mesmo corpo, sem mudança nem evento."""
    t0 = clock.now()
    created, bot = _offering_with_bot(client, bot_api)
    ref, tg = created["code"], bot["telegram_bot_id"]
    clock.advance(5)
    t_act = clock.now()
    activate_bot(client, ADMIN_TOKEN, ref)
    bot_worker_thread(settings=_worker_settings(settings))
    _wait_polled(bot_api, tg)
    _wait_profile(client, bot_api, ref, "configured")
    _wait_profile_tasks_finished(bot_api, sql)
    cursor = _last_seq(client)

    clock.advance(60)
    t_close = clock.now()
    closed = _ok(_close(client, ref), "offerings.close")
    expected = _offering(created, state="closed", activated_at=iso(t_act), closed_at=iso(t_close),
                         bot=_summary(bot, "configured"))
    assert closed == expected

    mark = monotonic()
    bot_api.wait_for(lambda: len(_polls_after(bot_api, tg, mark)) >= 2, timeout=10)
    assert _get_offering(client, ref) == expected
    assert _get_bot(client, ref) == {
        "offering": {"offering_id": created["offering_id"], "code": ref}, "username": bot["username"],
        "telegram_bot_id": tg, "state": "registered", "profile_state": "configured",
        "registered_at": iso(t0), "token_replaced_at": None,
        "lease": {"worker_id": WORKER, "until": iso(t_close + LEASE)},
        "last_poll_ok_at": iso(t_close), "problem": None}
    changed = [_state_changed(created, t_close, "active", "closed")]
    assert _events_after(client, cursor) == changed

    # a recepção continua no oferecimento encerrado (um `/ajuda` é gravado e respondido).
    user = make_user(201)
    update_id = bot_api.push_text(tg, user, "/ajuda", bot_command=True)
    assert update_id is not None
    bot_api.wait_for(lambda: [t[2] for t in _update_tasks(sql)] == ["done"]
                     and [r[-1] for r in _outbox_rows(sql)] == ["sent"], timeout=10)
    message = _delivered_message(bot_api, tg, update_id)
    rows = _update_rows(sql, created["offering_id"])
    assert [{k: v for k, v in row.items() if k != "id"} for row in rows] == [
        {"update_epoch": 0, "update_id": update_id, "kind": "message", "chat_id": user.user_id,
         "chat_type": "private", "sender_id": user.user_id, "text": "/ajuda", "command": "/ajuda",
         "details": {"message_id": message["message_id"], "date": message["date"]},
         "received_at": t_close, "state": "processed", "outcome": "help", "processed_at": t_close}]
    row_id = rows[0]["id"]
    assert _update_tasks(sql) == [(f"telegram.update:{row_id}", {"update_row_id": row_id}, "done")]
    assert _outbox_rows(sql) == [(None, _bot_row(sql, created["offering_id"]), None, row_id, HELP, "sent")]
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(tg)] == [(user.user_id, HELP)]
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=tg)] == [{"chat_id": user.user_id,
                                                                            "text": HELP}]
    assert _get_offering(client, ref) == expected
    assert _events_after(client, cursor) == changed

    clock.advance(10)
    assert _ok(_close(client, ref), "offerings.close") == expected
    assert _events_after(client, cursor) == changed


# ---------------------------------------------------------------------------------------------
# Preparação
# ---------------------------------------------------------------------------------------------


def test_prepare_archive_returns_the_exact_summary(client: Any, clock: Any, sql: psycopg.Connection,
                                                   bot_api: Any, settings: Any,
                                                   bot_worker_thread: Any) -> None:
    """Resumo do arquivamento (código de 22 caracteres válido por 10
    minutos, guardado só como resumo): num rascunho sem bot, o resumo é `0`/`0` com o `Offering`
    atual; num oferecimento encerrado com um membro da equipe vinculado (pelo `/start` com o link),
    um link pendente válido e um pendente **vencido**, `team_members_active = 1` e `pending_links = 1`;
    `expires_at` = agora + 10 min; o banco guarda o SHA-256 do código e a validade; nada muda no
    oferecimento; um `offering.archive_prepared {offering_id, expires_at}` por preparação."""
    draft = create_offering(client, ADMIN_TOKEN)
    cursor = _last_seq(client)
    clock.advance(7)
    t1 = clock.now()
    prepared = _prepared(client, draft["code"])
    code = prepared["confirmation_code"]
    assert prepared == {"offering": draft, "team_members_active": 0, "pending_links": 0,
                        "confirmation_code": code, "expires_at": iso(t1 + CONFIRMATION)}
    assert _get_offering(client, draft["code"]) == draft
    assert _stored_archive(sql, draft["code"]) == ("draft", None, _sha256(code), t1 + CONFIRMATION)
    assert _events_after(client, cursor) == [
        _event("offering.archive_prepared", t1, {"offering_id": draft["offering_id"],
                                                 "expires_at": iso(t1 + CONFIRMATION)})]

    member = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Vinculada")
    pending = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Pendente")
    expired = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Vencida")
    created, bot = _offering_with_bot(client, bot_api)
    ref, tg = created["code"], bot["telegram_bot_id"]
    create_team_link(client, ADMIN_TOKEN, ref, expired["alias"], valid_hours=1)
    member_link = create_team_link(client, ADMIN_TOKEN, ref, member["alias"])
    activate_bot(client, ADMIN_TOKEN, ref)
    worker = bot_worker_thread(settings=_worker_settings(settings))
    _wait_polled(bot_api, tg)
    _wait_profile(client, bot_api, ref, "configured")
    user = make_user(301)
    bot_api.push_start(tg, user, link_code(member_link["link"]))
    bot_api.wait_for(lambda: bot_api.sent_messages(tg, chat_id=user.user_id), timeout=10)
    assert [m.text for m in bot_api.sent_messages(tg, chat_id=user.user_id)] == [WELCOME_TEAM]
    create_team_link(client, ADMIN_TOKEN, ref, pending["alias"])
    assert bot_worker_thread.stop_one(worker) < 10
    closed = _ok(_close(client, ref), "offerings.close")
    assert closed == _offering(created, state="closed", activated_at=iso(t1), closed_at=iso(t1),
                               bot=_summary(bot, "configured"))

    clock.advance(3601)  # o link de 1 h vence; o de 168 h continua valendo
    t2 = clock.now()
    cursor = _last_seq(client)
    summary = _prepared(client, ref)
    code = summary["confirmation_code"]
    assert summary == {"offering": closed, "team_members_active": 1, "pending_links": 1,
                       "confirmation_code": code, "expires_at": iso(t2 + CONFIRMATION)}
    assert _get_offering(client, ref) == closed
    assert _stored_archive(sql, ref) == ("closed", None, _sha256(code), t2 + CONFIRMATION)
    assert _events_after(client, cursor) == [
        _event("offering.archive_prepared", t2, {"offering_id": created["offering_id"],
                                                 "expires_at": iso(t2 + CONFIRMATION)})]
    assert _tables_with_text(sql, code) == []
    assert _tables_with_text(sql, _sha256(code)) == ["offerings"]  # controle positivo: o resumo está lá


def test_prepare_again_invalidates_the_previous_code(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Preparar de novo invalida o código anterior: a segunda preparação troca o
    código e a validade; confirmar com o código anterior → 409 `invalid_confirmation`, sem mudança nem
    evento; com o código novo → arquivado."""
    created = create_offering(client, ADMIN_TOKEN)
    ref = created["code"]
    first = _prepared(client, ref)
    clock.advance(30)
    t1 = clock.now()
    second = _prepared(client, ref)
    assert second == {"offering": created, "team_members_active": 0, "pending_links": 0,
                      "confirmation_code": second["confirmation_code"], "expires_at": iso(t1 + CONFIRMATION)}
    assert second["confirmation_code"] != first["confirmation_code"]
    assert _stored_archive(sql, ref) == ("draft", None, _sha256(second["confirmation_code"]),
                                         t1 + CONFIRMATION)
    cursor = _last_seq(client)

    assert_error(_confirm(client, ref, first["confirmation_code"]), 409, "invalid_confirmation")
    assert _get_offering(client, ref) == created
    assert _stored_archive(sql, ref) == ("draft", None, _sha256(second["confirmation_code"]),
                                         t1 + CONFIRMATION)
    assert _events_after(client, cursor) == []

    archived = _ok(_confirm(client, ref, second["confirmation_code"]), "offerings.archive")
    assert archived == _offering(created, state="archived", archived_at=iso(t1))
    assert _events_after(client, cursor) == [_state_changed(created, t1, "draft", "archived")]


# ---------------------------------------------------------------------------------------------
# Confirmação
# ---------------------------------------------------------------------------------------------


def test_confirm_archives_releases_lease_stops_polling_and_revokes_links(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, settings: Any,
        bot_worker_thread: Any) -> None:
    """Efeitos e ordem dos eventos:
    confirmar o arquivamento de um oferecimento encerrado, com o bot consultado por um worker →
    200 com o `Offering` exato (`archived`, `archived_at`); na mesma transação, a posse limpa
    (`bot.lease_changed {released, archived}` do dono), **todos** os convites pendentes revogados,
    inclusive o vencido (`team.link_revoked {archived}`, um por convite, em ordem de convite) e
    `offering.state_changed {closed → archived}` por último; o código deixa de existir; a consulta do
    bot para (nenhum `getUpdates` dele depois de dois ciclos de consulta de outro bot do mesmo worker,
    que continua); a tarefa do perfil troca só a descrição pela de oferecimento encerrado (parâmetro
    exato, sem novo `setMyCommands`) e grava `bot.profile_updated {configured, archived}`."""
    pending_person = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Pendente")
    expired_person = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Vencida")
    t0 = clock.now()
    created, bot = _offering_with_bot(client, bot_api)
    witness, witness_bot = _offering_with_bot(client, bot_api)
    ref, tg, tw = created["code"], bot["telegram_bot_id"], witness_bot["telegram_bot_id"]
    create_team_link(client, ADMIN_TOKEN, ref, expired_person["alias"], valid_hours=1)
    clock.advance(3601)
    t1 = clock.now()
    create_team_link(client, ADMIN_TOKEN, ref, pending_person["alias"])
    activate_bot(client, ADMIN_TOKEN, ref)
    activate_bot(client, ADMIN_TOKEN, witness["code"])
    bot_worker_thread(settings=_worker_settings(settings))
    for telegram_bot_id, offering_ref in ((tg, ref), (tw, witness["code"])):
        _wait_polled(bot_api, telegram_bot_id)
        _wait_profile(client, bot_api, offering_ref, "configured")
    _wait_profile_tasks_finished(bot_api, sql)

    clock.advance(60)
    t2 = clock.now()
    closed = _ok(_close(client, ref), "offerings.close")
    clock.advance(30)
    t3 = clock.now()
    mark = monotonic()
    bot_api.wait_for(lambda: len(_polls_after(bot_api, tg, mark)) >= 2, timeout=10)
    summary = _prepared(client, ref)
    assert summary == {"offering": closed, "team_members_active": 0, "pending_links": 1,
                       "confirmation_code": summary["confirmation_code"],
                       "expires_at": iso(t3 + CONFIRMATION)}
    cursor = _last_seq(client)

    archived = _ok(_confirm(client, ref, summary["confirmation_code"]), "offerings.archive")
    expected = _offering(created, state="archived", activated_at=iso(t1), closed_at=iso(t2),
                         archived_at=iso(t3), bot=_summary(bot, "configured"))
    assert archived == expected
    assert _stored_archive(sql, ref) == ("archived", t3, None, None)
    assert sql.execute("SELECT state, revoked_at FROM invites ORDER BY id").fetchall() == [
        ("revoked", t3), ("revoked", t3)]

    # A consulta do bot arquivado para; a do outro bot do mesmo worker continua.
    bot_api.wait_for(lambda: not bot_api.pending_polls(tg), timeout=10)
    quiet = monotonic()
    bot_api.wait_for(lambda: len(_polls_after(bot_api, tw, quiet)) >= 2, timeout=10)
    first_witness_poll = _polls_after(bot_api, tw, quiet)[0]
    assert first_witness_poll.ended is not None
    assert [c.seq for c in _polls_after(bot_api, tg, first_witness_poll.ended)] == []

    # O perfil troca só a descrição (arquivado).
    bot_api.wait_for(lambda: len(bot_api.calls("setMyDescription", bot_id=tg)) == 2, timeout=10)
    bot_api.wait_for(lambda: [e for e in _events_after(client, cursor) if e["type"] == "bot.profile_updated"],
                     timeout=10)
    assert [c.params for c in bot_api.calls("setMyDescription", bot_id=tg)] == [
        {"description": DESCRIPTION_ACTIVE}, {"description": DESCRIPTION_ARCHIVED}]
    assert [c.params for c in bot_api.calls("setMyCommands", bot_id=tg)] == [{"commands": COMMANDS}]

    oid = created["offering_id"]
    assert _events_after(client, cursor) == [
        _event("bot.lease_changed", t3, {"offering_id": oid, "worker_id": WORKER, "change": "released",
                                         "reason": "archived"}),
        _event("team.link_revoked", t3, {"offering_id": oid, "person": expired_person["pseudo_id"],
                                         "revoke_reason": "archived"}),
        _event("team.link_revoked", t3, {"offering_id": oid, "person": pending_person["pseudo_id"],
                                         "revoke_reason": "archived"}),
        _state_changed(created, t3, "closed", "archived"),
        _event("bot.profile_updated", t3, {"offering_id": oid, "profile_state": "configured",
                                           "purpose": "archived"}, actor=None),
    ]
    assert _get_offering(client, ref) == expected
    assert _get_bot(client, ref) == {
        "offering": {"offering_id": oid, "code": ref}, "username": bot["username"], "telegram_bot_id": tg,
        "state": "registered", "profile_state": "configured", "registered_at": iso(t0),
        "token_replaced_at": None, "lease": None, "last_poll_ok_at": iso(t3), "problem": None}


def test_invalid_confirmation_codes_change_nothing(client: Any, clock: Any, sql: psycopg.Connection) -> None:
    """Vencido, substituído, de outro oferecimento ou desconhecido → recusado sem
    mudança (vencimento no processamento, pelo relógio; fronteira `agora <
    expires_at`): o código de outro oferecimento, um inventado, qualquer código num oferecimento sem
    preparação e o código no instante exato do vencimento → 409 `invalid_confirmation`, sem mudança
    nem evento; 1 µs antes do vencimento, o mesmo tipo de código ainda arquiva."""
    x = create_offering(client, ADMIN_TOKEN)
    y = create_offering(client, ADMIN_TOKEN)
    z = create_offering(client, ADMIN_TOKEN)
    t0 = clock.now()
    px = _prepared(client, x["code"])
    py = _prepared(client, y["code"])
    cursor = _last_seq(client)

    for ref, code in ((y["code"], px["confirmation_code"]),
                      (y["code"], secrets.token_urlsafe(16)),
                      (z["code"], py["confirmation_code"]),
                      (z["code"], "codigo-inventado")):
        resp = _confirm(client, ref, code)
        assert_error(resp, 409, "invalid_confirmation")
        assert_no_echo(resp, code)

    clock.set(t0 + CONFIRMATION - timedelta(microseconds=1))
    just_before = clock.now()
    archived = _ok(_confirm(client, x["code"], px["confirmation_code"]), "offerings.archive")
    assert archived == _offering(x, state="archived", archived_at=iso(just_before))

    clock.set(t0 + CONFIRMATION)
    assert_error(_confirm(client, y["code"], py["confirmation_code"]), 409, "invalid_confirmation")
    assert _get_offering(client, y["code"]) == y
    assert _get_offering(client, z["code"]) == z
    assert _stored_archive(sql, y["code"]) == ("draft", None, _sha256(py["confirmation_code"]),
                                               t0 + CONFIRMATION)
    assert _stored_archive(sql, z["code"]) == ("draft", None, None, None)
    assert _events_after(client, cursor) == [_state_changed(x, just_before, "draft", "archived")]


INVALID_CONFIRM_BODIES = [
    pytest.param({}, [("confirmation_code", "missing")], id="ausente"),
    pytest.param({"confirmation_code": None}, [("confirmation_code", "missing")], id="null"),
    pytest.param({"confirmation_code": ""}, [("confirmation_code", "too_short")], id="vazio"),
    pytest.param({"confirmation_code": "c" * 65}, [("confirmation_code", "too_long")], id="65-caracteres"),
    pytest.param({"confirmation_code": 12345678}, [("confirmation_code", "invalid_format")], id="inteiro"),
    pytest.param({"confirmation_code": "CodigoSentinela\x00NulNoMeio"}, [("confirmation_code", "invalid_format")],
                 id="nul"),
    pytest.param({"confirmation_code": "CodigoSentinela\ud800SubstitutoIsolado"},
                 [("confirmation_code", "invalid_format")], id="substituto-isolado"),
    pytest.param({"confirmation_code": "codigo-qualquer", "campo_extra_sentinela": 1},
                 [("body", "unknown_field")], id="campo-extra"),
]


@pytest.mark.parametrize(("body", "fields"), INVALID_CONFIRM_BODIES)
def test_confirm_rejects_invalid_body(client: Any, clock: Any, sql: psycopg.Connection, body: dict,
                                      fields: list[tuple[str, str]]) -> None:
    """Pedido `{confirmation_code}` de 1 a 64 caracteres (fora: `too_short`/`too_long`;
    não texto, U+0000 ou substituto isolado: `invalid_format`) e entrada estrita: 400 com `fields`
    exatos, sem eco (dos textos sentinela e do valor de 65 caracteres), e o oferecimento preparado não
    muda. Controle: 64 caracteres passam da validação e dão 409 `invalid_confirmation`."""
    created = create_offering(client, ADMIN_TOKEN)
    ref = created["code"]
    t0 = clock.now()
    prepared = _prepared(client, ref)
    resp = client.post(f"/v1/offerings/{ref}/archive", content=json.dumps(body).encode("ascii"),
                       headers={**ADMIN, "Content-Type": "application/json"})
    assert_error(resp, 400, "invalid_request", fields=fields)
    assert_no_echo(resp, "CodigoSentinela", "NulNoMeio", "SubstitutoIsolado", "campo_extra_sentinela", "c" * 65)
    assert_error(_confirm(client, ref, "c" * 64), 409, "invalid_confirmation")
    assert _stored_archive(sql, ref) == ("draft", None, _sha256(prepared["confirmation_code"]),
                                         t0 + CONFIRMATION)
    assert _get_offering(client, ref) == created


def test_confirm_on_archived_is_idempotent_with_any_code(client: Any, clock: Any) -> None:
    """Transição repetida (num oferecimento já arquivado, a
    confirmação devolve o estado atual sem conferir o código): confirmar de novo, com o código usado,
    um inventado ou um qualquer, horas depois → 200 com o mesmo corpo (o `archived_at` original), sem
    evento."""
    created = create_offering(client, ADMIN_TOKEN)
    ref = created["code"]
    t0 = clock.now()
    prepared = _prepared(client, ref)
    archived = _ok(_confirm(client, ref, prepared["confirmation_code"]), "offerings.archive")
    assert archived == _offering(created, state="archived", archived_at=iso(t0))
    cursor = _last_seq(client)

    clock.advance(3600)
    for code in (prepared["confirmation_code"], secrets.token_urlsafe(16), "qualquer-codigo"):
        assert _ok(_confirm(client, ref, code), "offerings.archive") == archived
    assert _get_offering(client, ref) == archived
    assert _events_after(client, cursor) == []


# ---------------------------------------------------------------------------------------------
# Fora da ordem e sem volta
# ---------------------------------------------------------------------------------------------


def test_archive_and_close_out_of_order_are_invalid_state(client: Any, clock: Any, sql: psycopg.Connection,
                                                          bot_api: Any) -> None:
    """Preparar só a partir de `draft` ou `closed`:
    num oferecimento ativo, preparar e confirmar o arquivamento → 409 `invalid_state` com `state:
    active` (é preciso encerrar antes; o estado vem antes do código); num rascunho, encerrar → 409
    `invalid_state` com `state: draft`; nada muda e nenhum evento é gravado."""
    created, bot = _offering_with_bot(client, bot_api)
    draft = create_offering(client, ADMIN_TOKEN)
    ref = created["code"]
    clock.advance(5)
    t1 = clock.now()
    active = activate_bot(client, ADMIN_TOKEN, ref)
    assert active == _offering(created, state="active", activated_at=iso(t1), bot=_summary(bot, "pending"))
    cursor = _last_seq(client)

    assert_error(_prepare(client, ref), 409, "invalid_state", state="active")
    assert_error(_confirm(client, ref, secrets.token_urlsafe(16)), 409, "invalid_state", state="active")
    assert_error(_close(client, draft["code"]), 409, "invalid_state", state="draft")
    assert _get_offering(client, ref) == active
    assert _get_offering(client, draft["code"]) == draft
    assert _stored_archive(sql, ref) == ("active", None, None, None)
    assert _events_after(client, cursor) == []


def test_every_operation_on_archived_is_invalid_state(client: Any, clock: Any, bot_api: Any) -> None:
    """Nenhuma transição volta atrás, nem se repete a partir do arquivado;
    ordem das verificações (o estado antes de qualquer chamada ao
    Telegram): num oferecimento arquivado (a partir do rascunho, com bot), ativar, encerrar, preparar,
    registrar o bot (com um token válido do mesmo bot) e gerar link de equipe → 409 `invalid_state`
    com `state: archived`, sem `getMe` e sem evento; as consultas continuam respondendo."""
    person = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Arquivada")
    t0 = clock.now()
    created, bot = _offering_with_bot(client, bot_api)
    ref, tg = created["code"], bot["telegram_bot_id"]
    prepared = _prepared(client, ref)
    archived = _ok(_confirm(client, ref, prepared["confirmation_code"]), "offerings.archive")
    assert archived == _offering(created, state="archived", archived_at=iso(t0), bot=_summary(bot, "pending"))
    new_token = bot_api.rotate_token(tg)
    get_me_calls = len(bot_api.calls("getMe"))
    cursor = _last_seq(client)
    clock.advance(60)

    assert_error(_activate(client, ref), 409, "invalid_state", state="archived")
    assert_error(_close(client, ref), 409, "invalid_state", state="archived")
    assert_error(_prepare(client, ref), 409, "invalid_state", state="archived")
    resp = client.put(f"/v1/offerings/{ref}/bot", json={"token": new_token}, headers=ADMIN)
    assert_error(resp, 409, "invalid_state", state="archived")
    assert_no_echo(resp, new_token)
    resp = client.post(f"/v1/offerings/{ref}/team-links", json={"person": person["alias"]}, headers=ADMIN)
    assert_error(resp, 409, "invalid_state", state="archived")

    assert len(bot_api.calls("getMe")) == get_me_calls
    assert _get_offering(client, ref) == archived
    assert _get_bot(client, ref) == {
        "offering": {"offering_id": created["offering_id"], "code": ref}, "username": bot["username"],
        "telegram_bot_id": tg, "state": "registered", "profile_state": "pending", "registered_at": iso(t0),
        "token_replaced_at": None, "lease": None, "last_poll_ok_at": None, "problem": None}
    assert _events_after(client, cursor) == []


# ---------------------------------------------------------------------------------------------
# Mensagens na fila e bot sem acesso
# ---------------------------------------------------------------------------------------------


def test_queued_messages_are_delivered_after_archive(client: Any, clock: Any, db_url: str,
                                                     sql: psycopg.Connection, outbox: Any, bot_api: Any,
                                                     settings: Any, bot_worker_thread: Any) -> None:
    """Regra (no arquivamento, as mensagens enfileiradas continuam sendo entregues): com o
    professor vinculado (boas-vindas entregue), uma mensagem por bot enfileirada com o worker parado
    continua na fila durante o encerramento e o arquivamento; um worker novo a entrega pelo bot
    arquivado, ao chat do endereço, e ela fica `sent`."""
    member = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Com Fila")
    created, bot = _offering_with_bot(client, bot_api)
    ref, tg = created["code"], bot["telegram_bot_id"]
    member_link = create_team_link(client, ADMIN_TOKEN, ref, member["alias"])
    t0 = clock.now()
    activate_bot(client, ADMIN_TOKEN, ref)
    first = bot_worker_thread(settings=_worker_settings(settings))
    _wait_polled(bot_api, tg)
    _wait_profile(client, bot_api, ref, "configured")
    user = make_user(401)
    bot_api.push_start(tg, user, link_code(member_link["link"]))
    bot_api.wait_for(lambda: bot_api.sent_messages(tg, chat_id=user.user_id), timeout=10)
    assert bot_worker_thread.stop_one(first) < 10

    bot_row = sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id "
                          "WHERE o.offering_id = %s", (uuid.UUID(created["offering_id"]),)).fetchone()[0]
    text = "Aviso que já estava na fila"
    clock.advance(5)
    enqueued_at = clock.now()
    with psycopg.connect(db_url) as conn:
        message_id = outbox.enqueue(conn, recipient=uuid.UUID(member["pseudo_id"]), text=text, bot_id=bot_row)
    _ok(_close(client, ref), "offerings.close")
    clock.advance(5)
    t_arch = clock.now()
    prepared = _prepared(client, ref)
    archived = _ok(_confirm(client, ref, prepared["confirmation_code"]), "offerings.archive")
    assert archived == _offering(created, state="archived", activated_at=iso(t0), closed_at=iso(enqueued_at),
                                 archived_at=iso(t_arch), bot=_summary(bot, "configured"))
    queued = {"id": message_id, "recipient": uuid.UUID(member["pseudo_id"]), "text": text, "state": "pending",
              "attempts": 0, "available_at": enqueued_at, "lease_owner": None, "lease_until": None,
              "channel_ref": None, "last_error": None, "sent_at": None, "bot_id": bot_row}
    assert dataclasses.asdict(outbox.get(message_id)) == queued

    bot_worker_thread(settings=_worker_settings(settings))
    bot_api.wait_for(lambda: len(bot_api.sent_messages(tg, chat_id=user.user_id)) == 2, timeout=10)
    sent = bot_api.sent_messages(tg, chat_id=user.user_id)
    assert [m.text for m in sent] == [WELCOME_TEAM, text]
    # O dublê registra o envio antes de responder; o worker grava `sent` depois da resposta.
    bot_api.wait_for(lambda: outbox.get(message_id).state != "leased", timeout=10)
    assert dataclasses.asdict(outbox.get(message_id)) == {
        **queued, "state": "sent", "attempts": 1, "channel_ref": str(sent[1].message_id), "sent_at": t_arch}
    assert [c.params for c in bot_api.calls("sendMessage", bot_id=tg)][-1] == {"chat_id": user.user_id,
                                                                             "text": text}


def test_archive_with_bot_without_access_completes_and_profile_fails(client: Any, clock: Any,
                                                                     bot_api: Any, settings: Any,
                                                                     bot_worker_thread: Any) -> None:
    """Arquivamento com o bot sem acesso
    (bot `no_access` … na execução → `failed` e `bot.profile_updated {failed}`, sem chamar o
    Telegram): com o token revogado, a configuração do perfil deixa o bot `no_access` e o perfil
    `failed`; preparar e confirmar o arquivamento funcionam (o resumo mostra o bot sem acesso); a
    tarefa do perfil arquivado falha de vez, com `bot.profile_updated {failed, archived}`, sem nenhuma
    chamada nova à Bot API falsa (a única chamada do perfil ativo é a descrição ou os comandos, em
    qualquer ordem, com os parâmetros exatos do método chamado)."""
    t0 = clock.now()
    created, bot = _offering_with_bot(client, bot_api)
    ref, tg = created["code"], bot["telegram_bot_id"]
    bot_api.revoke(tg)
    bot_worker_thread(settings=_worker_settings(settings))
    _wait_profile(client, bot_api, ref, "failed")
    oid = created["offering_id"]
    lost = {"offering": {"offering_id": oid, "code": ref}, "username": bot["username"], "telegram_bot_id": tg,
            "state": "no_access", "profile_state": "failed", "registered_at": iso(t0), "token_replaced_at": None,
            "lease": None, "last_poll_ok_at": None,
            "problem": {"code": "token_rejected", "message": TOKEN_REJECTED_MESSAGE}}
    assert _get_bot(client, ref) == lost
    cursor = _last_seq(client)

    clock.advance(20)
    t1 = clock.now()
    summary = _prepared(client, ref)
    no_access = _summary(bot, "failed", state="no_access")
    assert summary == {"offering": _offering(created, state="draft", bot=no_access), "team_members_active": 0,
                       "pending_links": 0, "confirmation_code": summary["confirmation_code"],
                       "expires_at": iso(t1 + CONFIRMATION)}
    archived = _ok(_confirm(client, ref, summary["confirmation_code"]), "offerings.archive")
    expected = _offering(created, state="archived", archived_at=iso(t1), bot=no_access)
    assert archived == expected

    bot_api.wait_for(lambda: [e for e in _events_after(client, cursor, OFFERING_BOT_EVENT_TYPES)
                              if e["type"] == "bot.profile_updated"], timeout=10)
    assert _events_after(client, cursor, OFFERING_BOT_EVENT_TYPES) == [
        _event("offering.archive_prepared", t1, {"offering_id": oid, "expires_at": iso(t1 + CONFIRMATION)}),
        _state_changed(created, t1, "draft", "archived"),
        _event("bot.profile_updated", t1, {"offering_id": oid, "profile_state": "failed", "purpose": "archived"},
               actor=None),
    ]
    # A ordem entre a descrição e os comandos não é contrato (test_bot_profile.py): a
    # única chamada do perfil ativo pode ser qualquer uma das duas, com os parâmetros exatos dela.
    calls = bot_api.calls(bot_id=tg)
    assert len(calls) == 2, bot_api.transcript()
    assert (calls[0].method, calls[0].status) == ("getMe", 200)
    profile_call = calls[1]
    assert (profile_call.token_state, profile_call.status) == ("revoked", 401)
    assert (profile_call.method, profile_call.params) in [("setMyDescription", {"description": DESCRIPTION_ACTIVE}),
                                                          ("setMyCommands", {"commands": COMMANDS})]
    assert _get_offering(client, ref) == expected
    assert _get_bot(client, ref) == lost
