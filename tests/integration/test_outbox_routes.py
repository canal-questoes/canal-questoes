"""Fila de saída por bot.

Cobre a extensão da fila da API base (fila de saída e porta de entrega), nas tabelas e
nos contratos:
- `Outbox.enqueue` com pessoa + bot (resolve o endereço de entrega `active` da pessoa naquele bot) e
  com `reply_to_update_id` (resposta ao chat de um update, sem pessoa); as demais combinações dão
  `ValueError`, sem erro no banco; a chave de idempotência compara `(recipient_person_id, bot_id,
  reply_to_update_id, text)` por `IS NOT DISTINCT FROM`;
- `OutboxRecord.bot_id`; `dispatch_once(…, include_routed=False)` (o padrão) só pega mensagens sem
  bot; com `include_routed=True`, a mensagem com bot sai com `route = BotRoute(bot_id, token_version,
  chat_id)`;
- o `TelegramChannel` (entrega sem rota ao canal padrão, com rota ao `sendMessage` da Bot API falsa),
  o bloqueio (`Undeliverable(blocked=True)`), o `retry_after`, a falha definitiva de uma resposta
  (`outbox.reply_failed`) e o token ilegível (`no_access` sem chamar o Telegram);
- `repr(OutgoingMessage)` e a linha do console sem o `chat_id` (a identidade só sai pela revelação).

**Montagem por SQL direto.** O oferecimento, o bot, os endereços de entrega e os updates entram nas
colunas das tabelas (a fila é fundacional e não depende das rotas de oferecimentos e bots).
O token do bot é cifrado **de forma independente**, com os primitivos da
`cryptography`, no formato v1 do token cifrado, com a chave-mestra dos testes (`MASTER_KEY`, a mesma do
`bot_settings`); o caso "chave trocada" cifra com `OTHER_MASTER_KEY`. As pessoas entram pela API base.
Chats e remetentes são os neutros da Bot API falsa (`make_user`, fora da faixa canário); um chat só
aceita `sendMessage` depois de mandar um update ao bot, por isso `_known_chat`.

Horários pelo `FakeClock`; ids internos (`bots.id`, `telegram_updates.id`, `message_id`) nunca como
literal esperado: vêm da montagem ou do dublê. Os eventos são lidos pela API (`events.list`, com o
esquema congelado) e conferidos por inteiro, sem o `seq` (só a ordem crescente).
"""

from __future__ import annotations

import base64
import dataclasses
import io
import itertools
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from cq_server.adapters.console import ConsoleChannel
from cq_server.bots import TelegramChannel
from cq_server.outbox import OutboxRecord
from cq_server.ports.delivery import BotRoute, OutgoingMessage, Sent, Undeliverable
from tests.helpers.api import ADMIN_TOKEN, all_events, register_person
from tests.helpers.bot_api import make_user
from tests.helpers.channels import ScriptedChannel
from tests.helpers.clock import iso
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY

LEASE_SECONDS = 30
LEASE = timedelta(seconds=LEASE_SECONDS)
CONSOLE_REF = re.compile(r"^console-[0-9a-f]{32}$")

# Transações do chamador que continuam usáveis depois de um `ValueError` (nenhum erro no banco).
USABLE = (TransactionStatus.IDLE, TransactionStatus.INTRANS)

_codes = itertools.count(1)
_update_ids = itertools.count(840_000_001)


@pytest.fixture(autouse=True)
def _harness_settings(settings: Any, bot_settings: Any) -> None:
    """Os valores fixos deste arquivo dependem do `settings` (posse 30 s, 3 tentativas, espera 2 s,
    teto 60 s) e da chave-mestra do `bot_settings` do conftest."""
    assert (settings.lease_seconds, settings.max_attempts, settings.backoff_base_seconds,
            settings.backoff_max_seconds) == (LEASE_SECONDS, 3, 2, 60)
    assert bot_settings.master_key == MASTER_KEY


def _s(seconds: float) -> timedelta:
    return timedelta(seconds=seconds)


# --- montagem -----------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Bot:
    row_id: int            # bots.id (interno)
    telegram_bot_id: int   # id do bot na Bot API falsa
    offering_id: uuid.UUID


def _seal(token: str, master_key: str, *, bot_uuid: uuid.UUID, telegram_bot_id: int) -> bytes:
    """O blob v1 do token cifrado, montado sem o código do servidor:
    `0x01 ‖ key_id(4) ‖ nonce(12) ‖ AESGCM(k).encrypt(nonce, token, aad)`."""
    ikm = base64.urlsafe_b64decode(master_key + "=")
    assert len(ikm) == 32
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"cq/bot-token/aes-256-gcm/v1").derive(ikm)
    key_id = HKDF(algorithm=hashes.SHA256(), length=4, salt=None,
                  info=b"cq/master-key-id/v1").derive(ikm)
    header = b"\x01" + key_id
    nonce = os.urandom(12)
    aad = (b"cq/bot-token/v1\x00" + header + bot_uuid.bytes
           + telegram_bot_id.to_bytes(8, "big", signed=True))
    return header + nonce + AESGCM(key).encrypt(nonce, token.encode("ascii"), aad)


def _seed_bot(sql: psycopg.Connection, bot_api: Any, clock: Any, *, master_key: str = MASTER_KEY,
              lease_owner: str | None = None) -> _Bot:
    """Um oferecimento `active` com um bot `registered` da Bot API falsa (token cifrado)."""
    telegram_bot_id = bot_api.add_bot()
    token = bot_api.token_of(telegram_bot_id)
    offering_id = uuid.uuid4()
    now = clock.now()
    offering_row = sql.execute(
        "INSERT INTO offerings (offering_id, code, course, term, timezone, response_window_minutes, "
        "state, created_at, activated_at) VALUES (%s, %s, 'Disciplina da Fila', '2031s1', "
        "'America/Sao_Paulo', 1440, 'active', %s, %s) RETURNING id",
        (offering_id, f"fila-bot-{next(_codes)}", now, now)).fetchone()[0]
    bot_uuid = uuid.uuid4()
    lease_until = None if lease_owner is None else now + LEASE
    row_id = sql.execute(
        "INSERT INTO bots (bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, "
        "token_version, state, profile_state, lease_owner, lease_until, registered_at) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'registered', 'configured', %s, %s, %s) RETURNING id",
        (bot_uuid, offering_row, telegram_bot_id, bot_api.state(telegram_bot_id).username,
         _seal(token, master_key, bot_uuid=bot_uuid, telegram_bot_id=telegram_bot_id),
         lease_owner, lease_until, now)).fetchone()[0]
    return _Bot(row_id=row_id, telegram_bot_id=telegram_bot_id, offering_id=offering_id)


def _person(client: Any, sql: psycopg.Connection, name: str) -> tuple[uuid.UUID, int]:
    """Cadastra pela API base; devolve (`pseudo_id`, `people.id`)."""
    body = register_person(client, ADMIN_TOKEN, name=name)
    pid = uuid.UUID(body["pseudo_id"])
    row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (pid,)).fetchone()
    return pid, row[0]


def _endpoint(sql: psycopg.Connection, clock: Any, *, person_row: int, bot: _Bot, chat_id: int,
              state: str = "active") -> int:
    now = clock.now()
    return sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
        "changed_at) VALUES (%s, %s, 'telegram', %s, %s, %s, %s) RETURNING id",
        (person_row, bot.row_id, chat_id, state, now, now)).fetchone()[0]


def _update(sql: psycopg.Connection, clock: Any, *, bot: _Bot, chat_id: int,
            text: str = "/ajuda") -> int:
    """Um update `message` gravado, `pending`; devolve `telegram_updates.id`."""
    now = clock.now()
    return sql.execute(
        "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, "
        "sender_id, text, command, details, received_at, state) "
        "VALUES (%s, 0, %s, 'message', %s, 'private', %s, %s, %s, %s, %s, 'pending') RETURNING id",
        (bot.row_id, next(_update_ids), chat_id, chat_id, text, text.split(" ", 1)[0],
         Jsonb({"message_id": 1, "date": int(now.timestamp())}), now)).fetchone()[0]


def _known_chat(bot_api: Any, bot: _Bot, index: int) -> int:
    """Um chat neutro que já falou com o bot (senão o `sendMessage` dá 400 `chat not found`)."""
    user = make_user(index)
    bot_api.push_text(bot.telegram_bot_id, user, "Olá")
    return user.user_id


def _enqueue(outbox: Any, db_url: str, **kwargs: Any) -> int:
    """Enfileira numa transação própria, confirmada no fim do bloco."""
    with psycopg.connect(db_url) as conn:
        return outbox.enqueue(conn, **kwargs)


def _row(sql: psycopg.Connection, message_id: int) -> tuple:
    return sql.execute(
        "SELECT recipient_person_id, bot_id, delivery_endpoint_id, reply_to_update_id, text, "
        "idempotency_key FROM outbox_messages WHERE id = %s", (message_id,)).fetchone()


def _count(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0]


def _endpoint_state(sql: psycopg.Connection, endpoint_id: int) -> tuple:
    return sql.execute("SELECT state, changed_at FROM delivery_endpoints WHERE id = %s",
                       (endpoint_id,)).fetchone()


# --- estados esperados (os da API base, com `bot_id`) -----------------------------------

def _pending(message_id: int, recipient: uuid.UUID | None, text: str, at: datetime,
             bot_id: int | None) -> OutboxRecord:
    return OutboxRecord(id=message_id, recipient=recipient, text=text, state="pending", attempts=0,
                        available_at=at, lease_owner=None, lease_until=None, channel_ref=None,
                        last_error=None, sent_at=None, bot_id=bot_id)


def _leased(prev: OutboxRecord, owner: str, now: datetime) -> OutboxRecord:
    return dataclasses.replace(prev, state="leased", attempts=prev.attempts + 1, lease_owner=owner,
                               lease_until=now + LEASE)


def _sent(prev: OutboxRecord, channel_ref: str, now: datetime) -> OutboxRecord:
    return dataclasses.replace(prev, state="sent", lease_owner=None, lease_until=None,
                               channel_ref=channel_ref, sent_at=now)


def _retry(prev: OutboxRecord, now: datetime, wait_seconds: float, error: str) -> OutboxRecord:
    return dataclasses.replace(prev, state="pending", available_at=now + _s(wait_seconds),
                               lease_owner=None, lease_until=None, last_error=error)


def _failed(prev: OutboxRecord, error: str) -> OutboxRecord:
    return dataclasses.replace(prev, state="failed", lease_owner=None, lease_until=None,
                               last_error=error)


def _assert_record(outbox: Any, expected: OutboxRecord) -> None:
    actual = outbox.get(expected.id)
    assert actual == expected, f"OutboxRecord {actual!r} != {expected!r}"


def _events(client: Any, *types: str) -> list[dict]:
    """Os eventos dos tipos dados pela API, em ordem de `seq` (crescente), sem o `seq`."""
    events = all_events(client, ADMIN_TOKEN, *types)
    seqs = [e["seq"] for e in events]
    assert all(a < b for a, b in itertools.pairwise(seqs)), f"seq não cresce: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in events]


def _outbox_failed(message_id: int, recipient: uuid.UUID, attempts: int, error: str,
                   at: datetime) -> dict:
    return {"type": "outbox.failed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"message_id": message_id, "recipient": str(recipient), "attempts": attempts,
                     "error": error}}


def _reply_failed(message_id: int, update_row: int, attempts: int, error: str, at: datetime) -> dict:
    return {"type": "outbox.reply_failed", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"message_id": message_id, "reply_to": update_row, "attempts": attempts,
                     "error": error}}


def _endpoint_blocked(bot: _Bot, person: uuid.UUID, at: datetime) -> dict:
    return {"type": "endpoint.blocked", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": str(bot.offering_id), "person": str(person),
                     "channel": "telegram"}}


FAILURE_TYPES = ("outbox.failed", "outbox.reply_failed", "endpoint.blocked")


def _telegram_channel(db_url: str, clock: Any, bots: Any, default: Any = None) -> TelegramChannel:
    return TelegramChannel(db_url, clock=clock, runtime=bots,
                           default=default if default is not None else ScriptedChannel([]))


# --- enfileirar ---------------------------------------------------------------------------------

def test_enqueue_to_person_by_bot_resolves_active_endpoint(client: Any, outbox: Any, db_url: str,
                                                           sql: psycopg.Connection, clock: Any,
                                                           bot_api: Any) -> None:
    """`Outbox.enqueue` com `recipient` e `bot_id`: a
    mensagem por bot guarda a pessoa, o bot e o endereço `active` da pessoa **naquele** bot (não o
    `inactive` do mesmo bot nem o de outro bot), sem update; `OutboxRecord.bot_id` é o bot, e a
    mensagem sem bot continua com `bot_id = None` e as colunas novas nulas."""
    bot = _seed_bot(sql, bot_api, clock)
    other_bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Destinataria Rota")
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=make_user(1).user_id, state="inactive")
    active = _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=make_user(2).user_id)
    elsewhere = _endpoint(sql, clock, person_row=prow, bot=other_bot, chat_id=make_user(2).user_id)
    t0 = clock.now()

    m1 = _enqueue(outbox, db_url, recipient=pid, text="Aviso pelo bot", bot_id=bot.row_id)
    m2 = _enqueue(outbox, db_url, recipient=pid, text="Aviso pelo outro bot", bot_id=other_bot.row_id)
    m3 = _enqueue(outbox, db_url, recipient=pid, text="Aviso sem bot")
    assert m1 < m2 < m3

    _assert_record(outbox, _pending(m1, pid, "Aviso pelo bot", t0, bot.row_id))
    _assert_record(outbox, _pending(m2, pid, "Aviso pelo outro bot", t0, other_bot.row_id))
    _assert_record(outbox, _pending(m3, pid, "Aviso sem bot", t0, None))
    assert _row(sql, m1) == (prow, bot.row_id, active, None, "Aviso pelo bot", None)
    assert _row(sql, m2) == (prow, other_bot.row_id, elsewhere, None, "Aviso pelo outro bot", None)
    assert _row(sql, m3) == (prow, None, None, None, "Aviso sem bot", None)


def test_enqueue_reply_to_update_has_no_person(client: Any, outbox: Any, db_url: str,
                                               sql: psycopg.Connection, clock: Any,
                                               bot_api: Any) -> None:
    """Resposta para quem não está vinculado, ao chat do update que está sendo respondido
    (`reply_to_update_id` com `bot_id`, `recipient` ausente): a resposta fica sem pessoa e
    sem endereço, com o bot e o update; `OutboxRecord.recipient` é `None` e `bot_id` é o bot."""
    bot = _seed_bot(sql, bot_api, clock)
    update_row = _update(sql, clock, bot=bot, chat_id=make_user(3).user_id)
    t0 = clock.now()

    mid = _enqueue(outbox, db_url, text="Resposta ao chat", bot_id=bot.row_id,
                   reply_to_update_id=update_row)

    _assert_record(outbox, _pending(mid, None, "Resposta ao chat", t0, bot.row_id))
    assert _row(sql, mid) == (None, bot.row_id, None, update_row, "Resposta ao chat", None)


def test_enqueue_by_bot_without_active_endpoint_is_value_error(client: Any, outbox: Any, db_url: str,
                                                               sql: psycopg.Connection, clock: Any,
                                                               bot_api: Any) -> None:
    """Pessoa com bot ("`recipient` com `bot_id`: resolve o endereço de entrega ativo … `ValueError`
    se não houver"; "sem erro no banco"): pessoa sem endereço, só com endereço `inactive`, só com
    `blocked`, só em outro bot, ou pessoa inexistente → `ValueError`; a transação do chamador segue
    usável e o que veio depois é confirmado; nenhuma mensagem é criada pelos casos recusados."""
    bot = _seed_bot(sql, bot_api, clock)
    other_bot = _seed_bot(sql, bot_api, clock)
    none_pid, _ = _person(client, sql, "Pessoa Sem Endereco")
    inactive_pid, inactive_row = _person(client, sql, "Pessoa Endereco Inativo")
    blocked_pid, blocked_row = _person(client, sql, "Pessoa Endereco Bloqueado")
    other_pid, other_row = _person(client, sql, "Pessoa Endereco Noutro Bot")
    _endpoint(sql, clock, person_row=inactive_row, bot=bot, chat_id=make_user(4).user_id,
              state="inactive")
    _endpoint(sql, clock, person_row=blocked_row, bot=bot, chat_id=make_user(5).user_id,
              state="blocked")
    _endpoint(sql, clock, person_row=other_row, bot=other_bot, chat_id=make_user(6).user_id)
    t0 = clock.now()

    with psycopg.connect(db_url) as conn:
        for recipient in (none_pid, inactive_pid, blocked_pid, other_pid, uuid.uuid4()):
            with pytest.raises(ValueError):
                outbox.enqueue(conn, recipient=recipient, text="Sem destino no bot", bot_id=bot.row_id)
            assert conn.info.transaction_status in USABLE
        kept = outbox.enqueue(conn, recipient=none_pid, text="Mensagem sem bot mantida")
        conn.execute("SELECT 1").fetchone()
        conn.commit()

    assert _count(sql) == 1
    _assert_record(outbox, _pending(kept, none_pid, "Mensagem sem bot mantida", t0, None))


def test_enqueue_rejects_invalid_destination_combinations(client: Any, outbox: Any, db_url: str,
                                                          sql: psycopg.Connection, clock: Any,
                                                          bot_api: Any) -> None:
    """Combinações ("outras combinações: `ValueError`, sem erro no banco"): só `bot_id`; só
    `reply_to_update_id`; pessoa + update; pessoa + bot + update; nada → `ValueError` com a transação
    do chamador usável; nenhuma mensagem é criada pelos casos recusados."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Combinacoes")
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=make_user(7).user_id)
    update_row = _update(sql, clock, bot=bot, chat_id=make_user(8).user_id)
    t0 = clock.now()
    invalid: list[dict[str, Any]] = [
        {"bot_id": bot.row_id},
        {"reply_to_update_id": update_row},
        {"recipient": pid, "reply_to_update_id": update_row},
        {"recipient": pid, "bot_id": bot.row_id, "reply_to_update_id": update_row},
        {},
    ]

    with psycopg.connect(db_url) as conn:
        for kwargs in invalid:
            with pytest.raises(ValueError):
                outbox.enqueue(conn, text="Combinação inválida", **kwargs)
            assert conn.info.transaction_status in USABLE, kwargs
        kept = outbox.enqueue(conn, recipient=pid, text="Mensagem valida depois", bot_id=bot.row_id)
        conn.commit()

    assert _count(sql) == 1
    _assert_record(outbox, _pending(kept, pid, "Mensagem valida depois", t0, bot.row_id))


def test_idempotency_key_compares_the_whole_destination(client: Any, outbox: Any, db_url: str,
                                                        sql: psycopg.Connection, clock: Any,
                                                        bot_api: Any) -> None:
    """Idempotência (com chave de idempotência repetida, o conteúdo é o mesmo se
    `(recipient_person_id, bot_id, reply_to_update_id, text)` coincidem por `IS NOT DISTINCT FROM`):
    a mesma chave com o mesmo destino e texto devolve o mesmo id (por bot, resposta e sem bot); com
    outro bot, sem bot, com bot, com outro update, com pessoa no lugar do update ou com outro texto →
    `ValueError`, sem erro no banco; nada é gravado a mais."""
    bot = _seed_bot(sql, bot_api, clock)
    other_bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Idempotencia Rota")
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=make_user(9).user_id)
    _endpoint(sql, clock, person_row=prow, bot=other_bot, chat_id=make_user(9).user_id)
    u1 = _update(sql, clock, bot=bot, chat_id=make_user(10).user_id)
    u2 = _update(sql, clock, bot=bot, chat_id=make_user(11).user_id)
    text = "Aviso com chave"
    t0 = clock.now()

    routed = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id,
                      idempotency_key="chave-rota")
    reply = _enqueue(outbox, db_url, text=text, bot_id=bot.row_id, reply_to_update_id=u1,
                     idempotency_key="chave-resposta")
    plain = _enqueue(outbox, db_url, recipient=pid, text=text, idempotency_key="chave-sem-bot")
    assert _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id,
                    idempotency_key="chave-rota") == routed
    assert _enqueue(outbox, db_url, text=text, bot_id=bot.row_id, reply_to_update_id=u1,
                    idempotency_key="chave-resposta") == reply
    assert _enqueue(outbox, db_url, recipient=pid, text=text, idempotency_key="chave-sem-bot") == plain

    conflicts: list[dict[str, Any]] = [
        {"recipient": pid, "idempotency_key": "chave-rota"},
        {"recipient": pid, "bot_id": other_bot.row_id, "idempotency_key": "chave-rota"},
        {"bot_id": bot.row_id, "reply_to_update_id": u1, "idempotency_key": "chave-rota"},
        {"bot_id": bot.row_id, "reply_to_update_id": u2, "idempotency_key": "chave-resposta"},
        {"recipient": pid, "bot_id": bot.row_id, "idempotency_key": "chave-resposta"},
        {"recipient": pid, "bot_id": bot.row_id, "idempotency_key": "chave-sem-bot"},
    ]
    with psycopg.connect(db_url) as conn:
        for kwargs in conflicts:
            with pytest.raises(ValueError):
                outbox.enqueue(conn, text=text, **kwargs)
            assert conn.info.transaction_status in USABLE, kwargs
        with pytest.raises(ValueError):
            outbox.enqueue(conn, recipient=pid, text="Aviso com outro texto", bot_id=bot.row_id,
                           idempotency_key="chave-rota")
        assert conn.info.transaction_status in USABLE
        after = outbox.enqueue(conn, recipient=pid, text="Aviso depois das recusas", bot_id=bot.row_id)
        conn.commit()

    assert routed < reply < plain < after
    assert _count(sql) == 4
    _assert_record(outbox, _pending(routed, pid, text, t0, bot.row_id))
    _assert_record(outbox, _pending(reply, None, text, t0, bot.row_id))
    _assert_record(outbox, _pending(plain, pid, text, t0, None))
    assert [_row(sql, m)[5] for m in (routed, reply, plain, after)] == [
        "chave-rota", "chave-resposta", "chave-sem-bot", None]


# --- despachar ----------------------------------------------------------------------------------

def test_dispatch_without_include_routed_takes_only_unrouted(client: Any, outbox: Any, db_url: str,
                                                             sql: psycopg.Connection, clock: Any,
                                                             bot_api: Any) -> None:
    """`dispatch_once(…, include_routed=False)`, o padrão, "só pega mensagens sem bot"
    (worker sem chave-mestra): com mensagens por bot e resposta enfileiradas antes, o
    despacho sem `include_routed` (e com `include_routed=False`) entrega só a mensagem sem bot e
    depois devolve `None`; as mensagens com bot ficam `pending`, sem tentativa."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Despacho Sem Rota")
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=make_user(12).user_id)
    update_row = _update(sql, clock, bot=bot, chat_id=make_user(13).user_id)
    t0 = clock.now()
    routed = _enqueue(outbox, db_url, recipient=pid, text="Aviso por bot", bot_id=bot.row_id)
    reply = _enqueue(outbox, db_url, text="Resposta por bot", bot_id=bot.row_id,
                     reply_to_update_id=update_row)
    plain = _enqueue(outbox, db_url, recipient=pid, text="Aviso sem bot")
    channel = ScriptedChannel([Sent("ref-sem-rota")])

    clock.advance(5)
    assert outbox.dispatch_once(channel, "worker-sem-rota") == plain
    assert outbox.dispatch_once(channel, "worker-sem-rota") is None
    assert outbox.dispatch_once(channel, "worker-sem-rota", include_routed=False) is None

    now = clock.now()
    assert channel.calls == [OutgoingMessage(message_id=plain, recipient=pid, text="Aviso sem bot")]
    _assert_record(outbox, _sent(_leased(_pending(plain, pid, "Aviso sem bot", t0, None), "worker-sem-rota",
                                         now), "ref-sem-rota", now))
    _assert_record(outbox, _pending(routed, pid, "Aviso por bot", t0, bot.row_id))
    _assert_record(outbox, _pending(reply, None, "Resposta por bot", t0, bot.row_id))


def test_dispatch_with_include_routed_carries_bot_route(client: Any, outbox: Any, db_url: str,
                                                        sql: psycopg.Connection, clock: Any,
                                                        bot_api: Any) -> None:
    """`OutgoingMessage.route`, `BotRoute(bot_id, token_version, chat_id)`:
    com `include_routed=True`, em ordem de id, a mensagem por bot sai com a rota do endereço da
    pessoa, a resposta com a rota do chat do update (sem pessoa) e a sem bot sem rota; a versão do token
    é a atual do bot; cada uma fica `sent` com o `channel_ref` devolvido."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Despacho Com Rota")
    person_chat = make_user(14).user_id
    update_chat = make_user(15).user_id
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=person_chat)
    update_row = _update(sql, clock, bot=bot, chat_id=update_chat)
    sql.execute("UPDATE bots SET token_version = 2, token_replaced_at = %s WHERE id = %s",
                (clock.now(), bot.row_id))
    t0 = clock.now()
    routed = _enqueue(outbox, db_url, recipient=pid, text="Aviso com rota", bot_id=bot.row_id)
    reply = _enqueue(outbox, db_url, text="Resposta com rota", bot_id=bot.row_id,
                     reply_to_update_id=update_row)
    plain = _enqueue(outbox, db_url, recipient=pid, text="Aviso sem rota")
    channel = ScriptedChannel([Sent("ref-rota-1"), Sent("ref-rota-2"), Sent("ref-rota-3")])

    clock.advance(3)
    for expected in (routed, reply, plain):
        assert outbox.dispatch_once(channel, "worker-rotas", include_routed=True) == expected
    assert outbox.dispatch_once(channel, "worker-rotas", include_routed=True) is None

    assert channel.calls == [
        OutgoingMessage(message_id=routed, recipient=pid, text="Aviso com rota",
                        route=BotRoute(bot_id=bot.row_id, token_version=2, chat_id=person_chat)),
        OutgoingMessage(message_id=reply, recipient=None, text="Resposta com rota",
                        route=BotRoute(bot_id=bot.row_id, token_version=2, chat_id=update_chat)),
        OutgoingMessage(message_id=plain, recipient=pid, text="Aviso sem rota"),
    ]
    now = clock.now()
    for mid, recipient, text, bot_id, ref in (
            (routed, pid, "Aviso com rota", bot.row_id, "ref-rota-1"),
            (reply, None, "Resposta com rota", bot.row_id, "ref-rota-2"),
            (plain, pid, "Aviso sem rota", None, "ref-rota-3")):
        _assert_record(outbox, _sent(_leased(_pending(mid, recipient, text, t0, bot_id),
                                             "worker-rotas", now), ref, now))
    assert _events(client, *FAILURE_TYPES) == []


def test_telegram_channel_sends_by_bot_and_unrouted_to_default(client: Any, outbox: Any, db_url: str,
                                                               sql: psycopg.Connection, clock: Any,
                                                               bot_api: Any, bots: Any) -> None:
    """Rota (a rota sem bot vai ao canal padrão; `Sent.channel_ref` é
    `str(message_id)` do `sendMessage`; `sendMessage` só com `{chat_id, text}`,
    sem posse): a mensagem sem bot vai ao canal padrão; a por bot e a resposta saem pelo
    `sendMessage` do bot delas, com o `chat_id` do endereço e o do update, e nenhuma outra chamada."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Canal Telegram")
    person_chat = _known_chat(bot_api, bot, 16)
    update_chat = _known_chat(bot_api, bot, 17)
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=person_chat)
    update_row = _update(sql, clock, bot=bot, chat_id=update_chat)
    t0 = clock.now()
    plain = _enqueue(outbox, db_url, recipient=pid, text="Aviso pelo canal padrão")
    routed = _enqueue(outbox, db_url, recipient=pid, text="Aviso pelo bot do oferecimento",
                      bot_id=bot.row_id)
    reply = _enqueue(outbox, db_url, text="Resposta ao chat do update", bot_id=bot.row_id,
                     reply_to_update_id=update_row)
    default = ScriptedChannel([Sent("ref-canal-padrao")])
    channel = _telegram_channel(db_url, clock, bots, default)

    clock.advance(2)
    for expected in (plain, routed, reply):
        assert outbox.dispatch_once(channel, "worker-telegram", include_routed=True) == expected
    assert outbox.dispatch_once(channel, "worker-telegram", include_routed=True) is None

    assert default.calls == [OutgoingMessage(message_id=plain, recipient=pid,
                                             text="Aviso pelo canal padrão")]
    calls = bot_api.calls()
    assert [(c.method, c.bot_id, c.params, c.outcome) for c in calls] == [
        ("sendMessage", bot.telegram_bot_id, {"chat_id": person_chat,
                                              "text": "Aviso pelo bot do oferecimento"}, "ok"),
        ("sendMessage", bot.telegram_bot_id, {"chat_id": update_chat,
                                              "text": "Resposta ao chat do update"}, "ok"),
    ]
    sent = bot_api.sent_messages(bot.telegram_bot_id)
    assert [(s.chat_id, s.text) for s in sent] == [
        (person_chat, "Aviso pelo bot do oferecimento"), (update_chat, "Resposta ao chat do update")]
    now = clock.now()
    _assert_record(outbox, _sent(_leased(_pending(plain, pid, "Aviso pelo canal padrão", t0, None),
                                         "worker-telegram", now), "ref-canal-padrao", now))
    _assert_record(outbox, _sent(_leased(_pending(routed, pid, "Aviso pelo bot do oferecimento", t0,
                                                  bot.row_id), "worker-telegram", now),
                                 str(sent[0].message_id), now))
    _assert_record(outbox, _sent(_leased(_pending(reply, None, "Resposta ao chat do update", t0,
                                                  bot.row_id), "worker-telegram", now),
                                 str(sent[1].message_id), now))
    assert _events(client, *FAILURE_TYPES) == []


# --- falhas -------------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["roteiro", "telegram"])
def test_blocked_chat_fails_message_and_blocks_endpoint(mode: str, client: Any, outbox: Any,
                                                        db_url: str, sql: psycopg.Connection,
                                                        clock: Any, bot_api: Any, bots: Any) -> None:
    """Chat bloqueado (`ChatUnreachable` → `Undeliverable("chat_unreachable",
    blocked=True)`: mensagem `failed`, endereço `blocked`, eventos `outbox.failed` e
    `endpoint.blocked`, nessa ordem, na mesma transação): pelo `Undeliverable(blocked=True)` de um
    canal roteirizado e pelo 403 `bot was blocked by the user` da Bot API falsa. Depois, a pessoa não
    tem endereço `active` no bot, e enfileirar para ela por esse bot dá `ValueError`."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Chat Bloqueado")
    chat = _known_chat(bot_api, bot, 18)
    endpoint = _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=chat)
    text = "Aviso para um chat bloqueado"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id)
    if mode == "roteiro":
        reason = "bloqueio informado pelo roteiro"
        channel: Any = ScriptedChannel([Undeliverable(reason, blocked=True)])
    else:
        reason = "chat_unreachable"
        bot_api.block_chat(bot.telegram_bot_id, chat)
        channel = _telegram_channel(db_url, clock, bots)

    clock.advance(5)
    assert outbox.dispatch_once(channel, "worker-bloqueio", include_routed=True) == mid
    now = clock.now()

    _assert_record(outbox, _failed(_leased(_pending(mid, pid, text, t0, bot.row_id),
                                           "worker-bloqueio", now), reason))
    assert _endpoint_state(sql, endpoint) == ("blocked", now)
    assert _events(client, *FAILURE_TYPES) == [
        _outbox_failed(mid, pid, 1, reason, now), _endpoint_blocked(bot, pid, now)]
    if mode == "roteiro":
        assert channel.calls == [OutgoingMessage(message_id=mid, recipient=pid, text=text,
                                                 route=BotRoute(bot_id=bot.row_id, token_version=1,
                                                                chat_id=chat))]
    else:
        assert [(c.method, c.params, c.status, c.description) for c in bot_api.calls()] == [
            ("sendMessage", {"chat_id": chat, "text": text}, 403,
             "Forbidden: bot was blocked by the user")]
        assert bot_api.sent_messages() == []

    clock.advance(86400)
    assert outbox.dispatch_once(channel, "worker-bloqueio", include_routed=True) is None
    with pytest.raises(ValueError):
        _enqueue(outbox, db_url, recipient=pid, text="Outro aviso", bot_id=bot.row_id)
    assert _count(sql) == 1


@pytest.mark.parametrize("mode", ["roteiro", "telegram"])
def test_undeliverable_without_block_keeps_endpoint_active(mode: str, client: Any, outbox: Any,
                                                           db_url: str, sql: psycopg.Connection,
                                                           clock: Any, bot_api: Any,
                                                           bots: Any) -> None:
    """`RequestRejected` → `Undeliverable("request_rejected")`, sem `blocked`:
    pelo `Undeliverable` sem bloqueio de um canal roteirizado e pelo 400 `chat not found` da Bot API
    falsa (chat que nunca falou com o bot), a mensagem fica `failed` com um só `outbox.failed`, e o
    endereço continua `active`, sem `endpoint.blocked`."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Recusa Sem Bloqueio")
    chat = make_user(19).user_id  # nunca falou com o bot
    endpoint = _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=chat)
    created = clock.now()
    text = "Aviso recusado sem bloqueio"
    mid = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id)
    if mode == "roteiro":
        reason = "recusa definitiva do roteiro"
        channel: Any = ScriptedChannel([Undeliverable(reason)])
    else:
        reason = "request_rejected"
        channel = _telegram_channel(db_url, clock, bots)

    clock.advance(5)
    assert outbox.dispatch_once(channel, "worker-recusa", include_routed=True) == mid
    now = clock.now()

    _assert_record(outbox, _failed(_leased(_pending(mid, pid, text, created, bot.row_id),
                                           "worker-recusa", now), reason))
    assert _endpoint_state(sql, endpoint) == ("active", created)
    assert _events(client, *FAILURE_TYPES) == [_outbox_failed(mid, pid, 1, reason, now)]
    if mode == "telegram":
        assert [(c.method, c.params, c.status, c.description) for c in bot_api.calls()] == [
            ("sendMessage", {"chat_id": chat, "text": text}, 400, "Bad Request: chat not found")]


def test_send_message_retry_after_is_respected(client: Any, outbox: Any, db_url: str,
                                              sql: psycopg.Connection, clock: Any, bot_api: Any,
                                              bots: Any) -> None:
    """Limite de envio (`RetryAfter(n)` → `RetryLater("rate_limited", n)`): 429 com
    `retry_after=5` no `sendMessage` → `pending` com `last_error = "rate_limited"` e
    `available_at = agora + 5 s` (acima da espera crescente de 2 s); com o `FakeClock` 1 s antes,
    nenhuma nova chamada; em 5 s, a 2ª chamada sai e a mensagem fica `sent`."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Espera Do Telegram")
    chat = _known_chat(bot_api, bot, 20)
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=chat)
    text = "Aviso que espera o Telegram"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id)
    bot_api.fail("sendMessage", retry_after=5, bot_id=bot.telegram_bot_id)
    channel = _telegram_channel(db_url, clock, bots)

    assert outbox.dispatch_once(channel, "worker-espera", include_routed=True) == mid
    state = _retry(_leased(_pending(mid, pid, text, t0, bot.row_id), "worker-espera", t0), t0, 5,
                   "rate_limited")
    _assert_record(outbox, state)
    first = bot_api.calls("sendMessage")
    assert [(c.params, c.status, c.retry_after, c.clock_at) for c in first] == [
        ({"chat_id": chat, "text": text}, 429, 5, t0)]

    clock.advance(4)
    assert outbox.dispatch_once(channel, "worker-espera", include_routed=True) is None
    assert len(bot_api.calls("sendMessage")) == 1
    _assert_record(outbox, state)

    clock.advance(1)
    assert outbox.dispatch_once(channel, "worker-espera", include_routed=True) == mid
    now = clock.now()
    calls = bot_api.calls("sendMessage")
    assert [(c.params, c.outcome, c.clock_at) for c in calls[1:]] == [
        ({"chat_id": chat, "text": text}, "ok", t0 + _s(5))]
    sent = bot_api.sent_messages(bot.telegram_bot_id)
    assert [(s.chat_id, s.text) for s in sent] == [(chat, text)]
    _assert_record(outbox, _sent(_leased(state, "worker-espera", now), str(sent[0].message_id), now))
    assert _events(client, *FAILURE_TYPES) == []


@pytest.mark.parametrize("mode", ["recusa", "tentativas-esgotadas"])
def test_reply_definitive_failure_emits_reply_failed(mode: str, client: Any, outbox: Any,
                                                     db_url: str, sql: psycopg.Connection,
                                                     clock: Any, bot_api: Any, bots: Any) -> None:
    """Resposta sem pessoa ("a falha definitiva de uma resposta sem pessoa gera
    `outbox.reply_failed`"): a resposta a um chat que nunca falou com o bot (400 → `request_rejected`,
    1 tentativa) e a que só recebe 5xx até as 3 tentativas acabarem (`telegram_unavailable`, esperas
    de 2 s e 4 s) ficam `failed` com um único `outbox.reply_failed {message_id, reply_to, attempts,
    error}` e nenhum `outbox.failed` nem `endpoint.blocked`."""
    bot = _seed_bot(sql, bot_api, clock)
    if mode == "recusa":
        chat = make_user(21).user_id  # nunca falou com o bot
    else:
        chat = _known_chat(bot_api, bot, 22)
        bot_api.fail("sendMessage", status=500, times=3, bot_id=bot.telegram_bot_id)
    update_row = _update(sql, clock, bot=bot, chat_id=chat)
    text = "Resposta que não chega"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, text=text, bot_id=bot.row_id, reply_to_update_id=update_row)
    channel = _telegram_channel(db_url, clock, bots)

    state = _pending(mid, None, text, t0, bot.row_id)
    if mode == "recusa":
        clock.advance(5)
        assert outbox.dispatch_once(channel, "worker-resposta", include_routed=True) == mid
        error, attempts = "request_rejected", 1
        state = _failed(_leased(state, "worker-resposta", clock.now()), error)
    else:
        error, attempts = "telegram_unavailable", 3
        for wait in (2, 4):
            assert outbox.dispatch_once(channel, "worker-resposta", include_routed=True) == mid
            state = _retry(_leased(state, "worker-resposta", clock.now()), clock.now(), wait, error)
            _assert_record(outbox, state)
            assert _events(client, *FAILURE_TYPES) == []
            clock.advance(wait)
        assert outbox.dispatch_once(channel, "worker-resposta", include_routed=True) == mid
        state = _failed(_leased(state, "worker-resposta", clock.now()), error)
    now = clock.now()

    _assert_record(outbox, state)
    assert state.attempts == attempts
    assert [c.params for c in bot_api.calls("sendMessage")] == [{"chat_id": chat, "text": text}] * attempts
    assert _events(client, *FAILURE_TYPES) == [_reply_failed(mid, update_row, attempts, error, now)]
    clock.advance(3600)
    assert outbox.dispatch_once(channel, "worker-resposta", include_routed=True) is None


def test_unreadable_token_marks_bot_without_access_and_never_calls_telegram(
        client: Any, outbox: Any, db_url: str, sql: psycopg.Connection, clock: Any, bot_api: Any,
        bots: Any) -> None:
    """Chave-mestra trocada (na leitura,
    token ilegível → perda de acesso com `token_unreadable` + `RetryLater`, sem chamar; bot
    `no_access` → `RetryLater("bot_no_access")` sem chamar): o token cifrado com outra chave deixa o
    bot `no_access` (`token_unreadable`), com a posse limpa e os eventos `bot.lease_changed
    {released, no_access, <dono anterior>}` e `bot.access_changed {lost, token_unreadable}`, nessa
    ordem (um episódio); as tentativas seguintes não abrem outro episódio, e a mensagem falha de vez
    na 3ª, com `outbox.failed`; nenhuma chamada à Bot API falsa."""
    bot = _seed_bot(sql, bot_api, clock, master_key=OTHER_MASTER_KEY, lease_owner="worker-dono-anterior")
    pid, prow = _person(client, sql, "Pessoa Token Ilegivel")
    chat = _known_chat(bot_api, bot, 23)
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=chat)
    text = "Aviso de bot com chave trocada"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id)
    channel = _telegram_channel(db_url, clock, bots)
    access_types = ("bot.lease_changed", "bot.access_changed", *FAILURE_TYPES)

    assert outbox.dispatch_once(channel, "worker-cifra", include_routed=True) == mid
    state = _retry(_leased(_pending(mid, pid, text, t0, bot.row_id), "worker-cifra", t0), t0, 2,
                   "token_unreadable")
    _assert_record(outbox, state)
    assert sql.execute("SELECT state, access_problem, lease_owner, lease_until, token_version "
                       "FROM bots WHERE id = %s", (bot.row_id,)).fetchone() == (
        "no_access", "token_unreadable", None, None, 1)
    episode = [
        {"type": "bot.lease_changed", "occurred_at": iso(t0), "actor_token_id": None,
         "data": {"offering_id": str(bot.offering_id), "worker_id": "worker-dono-anterior",
                  "change": "released", "reason": "no_access"}},
        {"type": "bot.access_changed", "occurred_at": iso(t0), "actor_token_id": None,
         "data": {"offering_id": str(bot.offering_id), "access": "lost",
                  "problem": "token_unreadable"}},
    ]
    assert _events(client, *access_types) == episode

    clock.advance(2)
    assert outbox.dispatch_once(channel, "worker-cifra", include_routed=True) == mid
    state = _retry(_leased(state, "worker-cifra", clock.now()), clock.now(), 4, "bot_no_access")
    _assert_record(outbox, state)
    assert _events(client, *access_types) == episode

    clock.advance(4)
    assert outbox.dispatch_once(channel, "worker-cifra", include_routed=True) == mid
    now = clock.now()
    _assert_record(outbox, _failed(_leased(state, "worker-cifra", now), "bot_no_access"))
    assert _events(client, *access_types) == [*episode, _outbox_failed(mid, pid, 3, "bot_no_access", now)]
    assert bot_api.calls() == []


def test_outgoing_message_repr_and_console_line_hide_chat_id(client: Any, outbox: Any, db_url: str,
                                                             sql: psycopg.Connection, clock: Any,
                                                             bot_api: Any) -> None:
    """Proteção da identidade (o `ConsoleChannel` continua escrevendo as mesmas 4 chaves e
    nunca escreve o `chat_id`; `BotRoute.chat_id` com `repr=False`): a mensagem por bot
    entregue a um console recebe a rota com o chat certo, mas nem o `repr`/`str` da mensagem e da rota
    nem a linha do console contêm o `chat_id`; a linha tem as 4 chaves da API base."""
    bot = _seed_bot(sql, bot_api, clock)
    pid, prow = _person(client, sql, "Pessoa Linha Do Console")
    chat = make_user(4242).user_id
    _endpoint(sql, clock, person_row=prow, bot=bot, chat_id=chat)
    text = "Aviso com rota no console"
    t0 = clock.now()
    mid = _enqueue(outbox, db_url, recipient=pid, text=text, bot_id=bot.row_id)
    stream = io.StringIO()
    console = ConsoleChannel(stream)
    seen: list[OutgoingMessage] = []

    class _Tee:
        capabilities = console.capabilities

        def send(self, message: OutgoingMessage) -> Any:
            seen.append(message)
            return console.send(message)

    assert outbox.dispatch_once(_Tee(), "worker-console", include_routed=True) == mid

    assert seen == [OutgoingMessage(message_id=mid, recipient=pid, text=text,
                                    route=BotRoute(bot_id=bot.row_id, token_version=1, chat_id=chat))]
    message = seen[0]
    for shown in (repr(message), str(message), repr(message.route), str(message.route)):
        assert str(chat) not in shown, shown
    out = stream.getvalue()
    assert str(chat) not in out
    line = json.loads(out)
    assert CONSOLE_REF.fullmatch(line["channel_ref"])
    assert line == {"channel_ref": line["channel_ref"], "message_id": mid, "recipient": str(pid),
                    "text": text}
    _assert_record(outbox, _sent(_leased(_pending(mid, pid, text, t0, bot.row_id), "worker-console",
                                         t0), line["channel_ref"], t0))
