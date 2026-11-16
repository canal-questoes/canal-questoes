"""Bots dos oferecimentos: runtime, registro, ativação, consulta, posse, perfil e entrega por bot
(um bot do Telegram por oferecimento; o token fica cifrado no banco com a chave-mestra).
O núcleo fala com o Telegram só pela porta; o adaptador HTTP vem de quem monta o runtime.

- `BotRuntime`: as configurações dos bots e a fábrica da porta do Telegram. A fábrica é
  obrigatória, porque o núcleo não importa o adaptador: quem monta (`__main__`, os testes) passa
  `adapters.telegram.http_factory(settings)`. `cipher` é `None` sem chave-mestra.
- **Registro** (`register_bot`): o `getMe` roda **fora** de qualquer transação; a
  gravação é uma transação só (`FOR NO KEY UPDATE` do oferecimento, reconferência do arquivamento, lock
  consultivo dos eventos, conflitos, gravação). O token só é guardado cifrado (`TokenCipher`, com o
  bot no dado associado); a troca grava um blob novo sem decifrar o antigo.
- **Ativação** (`activate`) e **consulta** (`get_bot`).
- **Posse** (na transação do chamador, com os eventos e o aviso da perda de acesso):
  `acquire_leases`, `renew_leases`, `release_leases`, `poll_allowed` e `mark_no_access`.
- **Perfil** (`profile_task_handler`): o tratador da tarefa `bot.sync_profile`.
- `TelegramChannel`: canal de entrega que manda as mensagens com rota pelo bot delas e as sem rota
  ao canal padrão.

Os resultados de regra de negócio voltam como sentinelas (`offerings.InvalidState`,
`offerings.BotRequired`, `TokenRefused`, `TelegramDown`, `BotConflict`); `None` quer dizer
"oferecimento inexistente" (ou, em `get_bot`, sem bot). Nenhum log, exceção nem evento leva o
token: o texto claro só existe na memória, entre a decifragem e a chamada à porta.

Núcleo sem HTTP: nada aqui importa o adaptador nem `httpx*` (o núcleo só conhece as portas).

Mensagens ricas (edição, mídia, teclado e dialeto):
`TelegramChannel(…, delivery=DeliverySettings())` (as capacidades e o formatador pelo modo
`CQ_TELEGRAM_FORMAT`), `TelegramChannel.formatter` e `formatter_for(text_format)` (os formatadores de
`cq_server.telegramrender`; nunca `cq_server.adapters`) e `answer_callback` (a confirmação do toque,
direto pela porta, fora da fila e do limitador). O despacho pela forma da mensagem:

- sem campo rico → `send_message(chat_id, text)`, o caminho simples (o `answerable` sozinho não conta);
- texto: `rich` → `send_rich_message({html, skip_entity_detection: true})`; `html` → `send_message` com
  `parse_mode = "HTML"` e a prévia de link desligada; `plain` → `send_message`; o teclado vira
  `inline_keyboard`, o pedido de resposta `force_reply` (com `input_field_placeholder`) e a resposta
  `reply_parameters` com `allow_sending_without_reply: true`;
- edição (`edit_ref`): `edit_message_text` no mesmo dialeto (sem teclado, a edição tira o teclado) ou,
  com `markup_only`, `edit_message_reply_markup`; `MessageNotModified` → `Sent(edit_ref)`; outro 400 →
  `Undeliverable("edit_rejected")`;
- mídia: foto (`send_photo`) ou documento (`send_document`); um 400 na foto leva a **uma**
  `send_document` com os mesmos bytes, nome e legenda na mesma chamada (sem passar de novo pelo ritmo);
  só o 400 do documento → `Undeliverable("media_rejected")`;
- os demais resultados são os do caminho simples (403 → `Undeliverable(blocked=True)`; 429 e 5xx →
  `RetryLater`; 401 → perda de acesso e `RetryLater`).
"""

from __future__ import annotations

import functools
import logging
import re
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg

from cq_server import botmessages, db
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import BotSettings, DeliverySettings, Settings
from cq_server.events import EVENTS_LOCK_KEY, EventType, append_event, load_known_identities
from cq_server.offerings import (
    POLLED_STATES,
    BotRequired,
    InvalidState,
    Offering,
    find_offering_row,
    load_offering,
    lock_offering,
    notify_bot_change,
    schedule_profile_sync,
)
from cq_server.ports.delivery import (
    ChannelCapabilities,
    DeliveryChannel,
    InlineButton,
    MessageFormatter,
    OutgoingMessage,
    ReplyRequest,
    RetryLater,
    Sent,
    Undeliverable,
)
from cq_server.ports.telegram import (
    ChatUnreachable,
    MessageNotModified,
    NotABot,
    RequestRejected,
    RetryAfter,
    TelegramBotApi,
    TelegramBotApiFactory,
    TelegramError,
    TokenRejected,
    UploadFile,
)
from cq_server.redaction import KnownIdentity, redact
from cq_server.secretbox import UNREADABLE_REASONS, TokenCipher, UnreadableToken
from cq_server.tasks import ClaimedTask, PermanentTaskError, RetryTaskLater
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter

logger = logging.getLogger("cq_server.worker")

# Formato local do token (a mesma forma que a redação reconhece, sem as
# fronteiras: aqui o texto inteiro é o token).
TOKEN_MAX_LENGTH = 80
TOKEN_PATTERN = re.compile(r"[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}")

# Problemas visíveis e as mensagens fixas.
PROBLEM_MESSAGES: dict[str, str] = {
    "token_rejected": "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
    "token_unreadable": "O token guardado não pode ser lido com a chave-mestra atual. Registre o token "
                        "de novo.",
    "telegram_unavailable": "O Telegram não respondeu; o sistema vai tentar de novo.",
    "rate_limited": "O Telegram pediu uma pausa; o sistema vai tentar de novo depois do tempo indicado.",
    "concurrent_consumer": "Outro programa está consultando este bot. Pare o outro programa.",
    "webhook_active": "Havia um webhook configurado neste bot; ele foi removido.",
}

# Valores.
ACCESS_PROBLEMS = ("token_rejected", "token_unreadable")
RELEASE_REASONS = ("not_eligible", "shutdown", "archived", "no_access")


# --------------------------------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------------------------------


class BotRuntime:
    """Configurações dos bots e fábrica da porta do Telegram (tipo de contrato; `repr` sem a chave)."""

    def __init__(self, settings: BotSettings, *, api_factory: TelegramBotApiFactory) -> None:
        self.settings = settings
        self.api_factory = api_factory

    @functools.cached_property
    def cipher(self) -> TokenCipher | None:
        """A cifra da chave-mestra, calculada no primeiro uso; `None` sem chave-mestra."""
        key = self.settings.master_key
        return TokenCipher(key) if key else None

    def __repr__(self) -> str:
        return f"BotRuntime(settings={self.settings!r})"


# --------------------------------------------------------------------------------------------------
# Registros e resultados
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LeasedBot:
    """Bot cuja posse este worker acabou de assumir, com a versão do token em uso."""

    bot_id: int
    token_version: int


@dataclass(frozen=True)
class Bot:
    """O bot de um oferecimento; `problem` é o código, sem a mensagem."""

    offering_id: uuid.UUID
    code: str
    username: str
    telegram_bot_id: int
    state: str
    profile_state: str
    registered_at: datetime
    token_replaced_at: datetime | None
    lease_owner: str | None
    lease_until: datetime | None
    last_poll_ok_at: datetime | None
    problem: str | None


@dataclass(frozen=True)
class TokenRefused:
    """O Telegram recusou o token, ou ele não é de um bot (400 `[{token, invalid_value}]`)."""


@dataclass(frozen=True)
class TelegramDown:
    """O `getMe` não teve resposta útil (503 `unavailable`); nada foi gravado."""


@dataclass(frozen=True)
class BotConflict:
    """409 `bot_conflict`: `reason` é `duplicate` (bot de outro oferecimento) ou `invalid_value`
    (o oferecimento já tem outro bot)."""

    reason: str


class MasterKeyMissing(Exception):
    """O worker não tem a chave-mestra: o perfil não pode ser configurado agora (falha temporária)."""


def validate_token(value: str) -> str:
    """O token sem mudança; `ValueError` com o motivo do contrato (`too_short`, `too_long`,
    `invalid_format`), nunca com o valor."""
    if not value:
        raise ValueError("too_short")
    if len(value) > TOKEN_MAX_LENGTH:
        raise ValueError("too_long")
    if TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid_format")
    return value


# --------------------------------------------------------------------------------------------------
# Textos do bot
# --------------------------------------------------------------------------------------------------


def offering_text(conn: psycopg.Connection, name: str, *, bot_id: int,
                  identities: Sequence[KnownIdentity] | None = None) -> str:
    """O texto fixo `name` (`DESCRIPTION_ACTIVE`, `DESCRIPTION_ARCHIVED`, `HELP` ou `WELCOME_TEAM`)
    do oferecimento do bot, com a disciplina e o período redigidos de novo na saída (com as
    identidades do momento, ou as dadas)."""
    row = conn.execute(
        "SELECT o.course, o.term FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE b.id = %s",
        (bot_id,),
    ).fetchone()
    if row is None:
        raise LookupError("bot inexistente")
    known = load_known_identities(conn) if identities is None else identities
    return botmessages.compose(name, course=redact(row[0], known), term=redact(row[1], known))


# --------------------------------------------------------------------------------------------------
# Consulta
# --------------------------------------------------------------------------------------------------

_BOT_SELECT = (
    "SELECT o.offering_id, o.code, b.username, b.telegram_bot_id, b.state, b.profile_state, "
    "b.registered_at, b.token_replaced_at, b.lease_owner, b.lease_until, b.last_poll_ok_at, "
    "b.access_problem, b.last_problem FROM bots b JOIN offerings o ON o.id = b.offering_id "
)


def _bot(row: Sequence[Any]) -> Bot:
    state = row[4]
    problem = row[11] if state == "no_access" else row[12]
    return Bot(offering_id=row[0], code=row[1], username=row[2], telegram_bot_id=int(row[3]),
               state=state, profile_state=row[5], registered_at=row[6], token_replaced_at=row[7],
               lease_owner=row[8], lease_until=row[9], last_poll_ok_at=row[10], problem=problem)


def _load_bot(conn: psycopg.Connection, offering_row: int) -> Bot | None:
    row = conn.execute(_BOT_SELECT + "WHERE b.offering_id = %s", (offering_row,)).fetchone()
    return None if row is None else _bot(row)


def get_bot(conn: psycopg.Connection, ref: str) -> Bot | None:
    """O bot do oferecimento da referência; `None` se o oferecimento não existe ou não tem bot."""
    offering = find_offering_row(conn, ref)
    if offering is None:
        return None
    return _load_bot(conn, offering.id)


# --------------------------------------------------------------------------------------------------
# Registro e troca de token
# --------------------------------------------------------------------------------------------------


def register_bot(conn: psycopg.Connection, ref: str, *, token: str, runtime: BotRuntime,
                 now: datetime, actor_token_id: str | None,
                 ) -> Bot | InvalidState | TokenRefused | TelegramDown | BotConflict | None:
    """Registra o token do bot do oferecimento (ou troca o do mesmo bot), nesta ordem:
    oferecimento (`None`) → arquivado (`InvalidState`) → `getMe` (fora de transação) → conflitos
    (`BotConflict`) → gravação, com os eventos, a tarefa `bot.sync_profile` e o aviso `bot`.

    `conn` não pode estar numa transação (o `getMe` roda sem nenhuma aberta). Sem chave-mestra no
    `runtime`, `RuntimeError` (a rota responde `not_configured` antes de chamar)."""
    validate_token(token)
    cipher = runtime.cipher
    if cipher is None:
        raise RuntimeError("chave-mestra ausente")

    offering = find_offering_row(conn, ref)
    if offering is None:
        return None
    if offering.state == "archived":
        return InvalidState("archived")

    api = runtime.api_factory(token)
    try:
        identity = api.get_me()
    except (TokenRejected, NotABot, RequestRejected):
        return TokenRefused()
    except TelegramError:
        return TelegramDown()
    finally:
        api.close()

    with conn.transaction():
        locked = lock_offering(conn, str(offering.offering_id))
        assert locked is not None  # oferecimentos nunca são apagados
        if locked.state == "archived":
            return InvalidState("archived")
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        current = conn.execute(
            "SELECT id, bot_uuid, telegram_bot_id, token_version, state FROM bots "
            "WHERE offering_id = %s FOR NO KEY UPDATE",
            (locked.id,),
        ).fetchone()
        # O lock dos eventos antes das conferências: dois registros concorrentes do mesmo bot do
        # Telegram em oferecimentos diferentes são serializados (sem erro no banco).
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENTS_LOCK_KEY,))
        owner = conn.execute("SELECT offering_id FROM bots WHERE telegram_bot_id = %s",
                             (identity.telegram_bot_id,)).fetchone()
        if owner is not None and int(owner[0]) != locked.id:
            return BotConflict("duplicate")
        if current is not None and int(current[2]) != identity.telegram_bot_id:
            return BotConflict("invalid_value")

        if current is None:
            bot_uuid = uuid.uuid4()
            blob = cipher.encrypt(token, bot_uuid=bot_uuid, telegram_bot_id=identity.telegram_bot_id)
            row = conn.execute(
                "INSERT INTO bots (bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, "
                "token_version, state, profile_state, registered_at) "
                "VALUES (%s, %s, %s, %s, %s, 1, 'registered', 'pending', %s) RETURNING id",
                (bot_uuid, locked.id, identity.telegram_bot_id, identity.username, blob, now),
            ).fetchone()
            assert row is not None
            bot_id, version = int(row[0]), 1
            append_event(conn, EventType.BOT_REGISTERED,
                         {"offering_id": locked.offering_id, "telegram_bot_id": identity.telegram_bot_id,
                          "username": identity.username},
                         occurred_at=now, actor_token_id=actor_token_id)
        else:
            bot_id, bot_uuid, _, old_version, old_state = current
            bot_id, version = int(bot_id), int(old_version) + 1
            blob = cipher.encrypt(token, bot_uuid=bot_uuid, telegram_bot_id=identity.telegram_bot_id)
            conn.execute(
                "UPDATE bots SET username = %s, token_ciphertext = %s, token_version = %s, "
                "state = 'registered', access_problem = NULL, profile_state = 'pending', "
                "token_replaced_at = %s WHERE id = %s",
                (identity.username, blob, version, now, bot_id),
            )
            append_event(conn, EventType.BOT_TOKEN_REPLACED,
                         {"offering_id": locked.offering_id, "telegram_bot_id": identity.telegram_bot_id,
                          "token_version": version},
                         occurred_at=now, actor_token_id=actor_token_id)
            if old_state == "no_access":
                append_event(conn, EventType.BOT_ACCESS_CHANGED,
                             {"offering_id": locked.offering_id, "access": "restored", "problem": None},
                             occurred_at=now, actor_token_id=actor_token_id)
        schedule_profile_sync(conn, bot_id=bot_id, token_version=version, purpose="active", now=now)
        notify_bot_change(conn, locked.offering_id)
        bot = _load_bot(conn, locked.id)
    assert bot is not None
    return bot


# --------------------------------------------------------------------------------------------------
# Ativação
# --------------------------------------------------------------------------------------------------


def activate(conn: psycopg.Connection, ref: str, *, now: datetime, actor_token_id: str | None,
             secrets: Sequence[str | None] = ()) -> Offering | InvalidState | BotRequired | None:
    """`draft` → `active`, nesta ordem: `closed`/`archived` → `InvalidState`;
    `active` → o estado atual, sem mudança (mesmo com o bot `no_access`); `draft` sem bot ou com bot
    `no_access` → `BotRequired`. Na ativação: `activated_at`, o código de arquivamento pendente
    limpo, `offering.state_changed` e o aviso `bot`, na mesma transação."""
    with conn.transaction():
        row = lock_offering(conn, ref)
        if row is None:
            return None
        if row.state in ("closed", "archived"):
            return InvalidState(row.state)
        if row.state == "draft":
            # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa
            # das travas continua a valer).
            bot = conn.execute("SELECT state FROM bots WHERE offering_id = %s FOR NO KEY UPDATE",
                               (row.id,)).fetchone()
            if bot is None or bot[0] != "registered":
                return BotRequired()
            conn.execute(
                "UPDATE offerings SET state = 'active', activated_at = %s, archive_code_sha256 = NULL, "
                "archive_code_expires_at = NULL WHERE id = %s",
                (now, row.id),
            )
            append_event(conn, EventType.OFFERING_STATE_CHANGED,
                         {"offering_id": row.offering_id, "from_state": "draft", "to_state": "active"},
                         occurred_at=now, actor_token_id=actor_token_id)
            notify_bot_change(conn, row.offering_id)
        return load_offering(conn, row.id, secrets=secrets)


# --------------------------------------------------------------------------------------------------
# Posse e perda de acesso
# --------------------------------------------------------------------------------------------------

# Elegível para consulta: bot `registered` de oferecimento `active` ou `closed`.
_ELIGIBLE = ("b.state = 'registered' AND o.state IN ("
             + ", ".join(f"'{state}'" for state in POLLED_STATES) + ")")


def _lease_event(conn: psycopg.Connection, offering_id: uuid.UUID, worker_id: str, change: str,
                 reason: str, now: datetime) -> None:
    append_event(conn, EventType.BOT_LEASE_CHANGED,
                 {"offering_id": offering_id, "worker_id": worker_id, "change": change, "reason": reason},
                 occurred_at=now, actor_token_id=None)


def _bot_ids(bot_ids: Collection[int]) -> list[int]:
    """Os ids de bot como inteiros (antes de qualquer SQL; booleano não é id)."""
    if any(isinstance(bot_id, bool) or not isinstance(bot_id, int) for bot_id in bot_ids):
        raise ValueError("ids de bot devem ser inteiros")
    return [int(bot_id) for bot_id in bot_ids]


def acquire_leases(conn: psycopg.Connection, *, worker_id: str, now: datetime,
                   lease_seconds: float, held: Collection[int] = ()) -> list[LeasedBot]:
    """Assume a posse dos bots elegíveis sem dono, com posse alheia vencida (`lease_until < agora`)
    ou com posse própria fora de `held`: `lease_until = agora + lease_seconds` e um
    `bot.lease_changed {acquired, acquired}` por bot, em ordem de id; `FOR NO KEY UPDATE SKIP LOCKED`, na
    transação do chamador. Devolve os bots assumidos, com a versão do token."""
    if not isinstance(worker_id, str) or not worker_id:
        raise ValueError("worker_id deve ser um texto não vazio")
    held_ids = _bot_ids(held)
    until = now + timedelta(seconds=lease_seconds)
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        rows = conn.execute(
            "SELECT b.id, o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id "
            f"WHERE {_ELIGIBLE} AND (b.lease_owner IS NULL "
            "   OR (b.lease_until < %(now)s AND b.lease_owner <> %(worker)s) "
            "   OR (b.lease_owner = %(worker)s AND NOT (b.id = ANY(%(held)s::bigint[])))) "
            "ORDER BY b.id FOR NO KEY UPDATE OF b SKIP LOCKED",
            {"now": now, "worker": worker_id, "held": held_ids},
        ).fetchall()
        if not rows:
            return []
        offering_of = {int(bot_id): offering_id for bot_id, offering_id in rows}
        updated = conn.execute(
            "UPDATE bots SET lease_owner = %s, lease_until = %s WHERE id = ANY(%s::bigint[]) "
            "RETURNING id, token_version",
            (worker_id, until, list(offering_of)),
        ).fetchall()
        versions = {int(bot_id): int(version) for bot_id, version in updated}
        leased = [LeasedBot(bot_id=bot_id, token_version=versions[bot_id]) for bot_id in sorted(versions)]
        for bot in leased:
            _lease_event(conn, offering_of[bot.bot_id], worker_id, "acquired", "acquired", now)
    return leased


def renew_leases(conn: psycopg.Connection, *, worker_id: str, bot_ids: Sequence[int],
                 now: datetime, lease_seconds: float) -> set[int]:
    """Renova as posses deste worker nos bots dados, se ainda elegíveis, mesmo vencidas, sem
    evento; devolve os ids que continuam deste worker."""
    ids = _bot_ids(bot_ids)
    if not ids:
        return set()
    until = now + timedelta(seconds=lease_seconds)
    with conn.transaction():
        rows = conn.execute(
            "UPDATE bots b SET lease_until = %(until)s FROM offerings o "
            f"WHERE o.id = b.offering_id AND b.id = ANY(%(ids)s::bigint[]) AND b.lease_owner = %(worker)s "
            f"AND {_ELIGIBLE} RETURNING b.id",
            {"until": until, "ids": ids, "worker": worker_id},
        ).fetchall()
    return {int(row[0]) for row in rows}


def release_leases(conn: psycopg.Connection, *, worker_id: str, bot_ids: Sequence[int],
                   now: datetime, reason: str) -> list[int]:
    """Solta as posses deste worker nos bots dados (só as próprias), com `bot.lease_changed
    {released, <reason>}` por bot e o aviso `bot`; devolve os ids soltos, em ordem."""
    if reason not in RELEASE_REASONS:
        raise ValueError("motivo de soltura desconhecido")
    ids = _bot_ids(bot_ids)
    if not ids:
        return []
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        rows = conn.execute(
            "SELECT b.id, o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id "
            "WHERE b.id = ANY(%s::bigint[]) AND b.lease_owner = %s ORDER BY b.id FOR NO KEY UPDATE OF b",
            (ids, worker_id),
        ).fetchall()
        if not rows:
            return []
        released = [int(bot_id) for bot_id, _ in rows]
        conn.execute("UPDATE bots SET lease_owner = NULL, lease_until = NULL WHERE id = ANY(%s::bigint[])",
                     (released,))
        for _, offering_id in rows:
            _lease_event(conn, offering_id, worker_id, "released", reason, now)
        for offering_id in dict.fromkeys(offering_id for _, offering_id in rows):
            notify_bot_change(conn, offering_id)
    return released


def poll_allowed(conn: psycopg.Connection, *, bot_id: int, worker_id: str, token_version: int,
                 now: datetime, settings: BotSettings) -> bool:
    """Antes de cada `getUpdates`: este worker é o dono, o bot é elegível, a versão
    do token é a mesma e `lease_until − agora ≥ poll_timeout_seconds + telegram_timeout_seconds`."""
    row = conn.execute(
        "SELECT b.lease_owner, b.lease_until, b.token_version, b.state, o.state FROM bots b "
        "JOIN offerings o ON o.id = b.offering_id WHERE b.id = %s",
        (bot_id,),
    ).fetchone()
    if row is None:
        return False
    owner, until, version, bot_state, offering_state = row
    if owner != worker_id or until is None or int(version) != token_version:
        return False
    if bot_state != "registered" or offering_state not in POLLED_STATES:
        return False
    margin = timedelta(seconds=settings.poll_timeout_seconds + settings.telegram_timeout_seconds)
    return until - now >= margin


def mark_no_access(conn: psycopg.Connection, *, bot_id: int, token_version: int, problem: str,
                   now: datetime) -> bool:
    """Perda de acesso: se o bot está `registered` com a `token_version` da
    chamada, fica `no_access` com o `problem`, sem posse; `bot.lease_changed {released, no_access,
    <dono anterior>}` se havia dono, depois `bot.access_changed {lost, <problem>}` e o aviso `bot`.
    Devolve `True` se abriu um episódio (senão nada muda)."""
    if problem not in ACCESS_PROBLEMS:
        raise ValueError("problema de acesso desconhecido")
    with conn.transaction():
        # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa das travas
        # continua a valer).
        row = conn.execute(
            "SELECT b.lease_owner, o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id "
            "WHERE b.id = %s AND b.token_version = %s AND b.state = 'registered' FOR NO KEY UPDATE OF b",
            (bot_id, token_version),
        ).fetchone()
        if row is None:
            return False
        owner, offering_id = row
        conn.execute(
            "UPDATE bots SET state = 'no_access', access_problem = %s, lease_owner = NULL, "
            "lease_until = NULL WHERE id = %s",
            (problem, bot_id),
        )
        if owner is not None:
            _lease_event(conn, offering_id, owner, "released", "no_access", now)
        append_event(conn, EventType.BOT_ACCESS_CHANGED,
                     {"offering_id": offering_id, "access": "lost", "problem": problem},
                     occurred_at=now, actor_token_id=None)
        notify_bot_change(conn, offering_id)
    return True


def log_token_unreadable(conn: psycopg.Connection, *, bot_id: int, reason: str) -> None:
    """Registro `bot.token_unreadable` (WARNING) do episódio aberto por um token ilegível: o motivo
    técnico só vai para o log (`malformed`, `key_mismatch` ou `tampered`), sem token,
    blob nem chave. Chamado depois do commit da perda de acesso."""
    row = conn.execute(
        "SELECT o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE b.id = %s",
        (bot_id,),
    ).fetchone()
    logger.warning("bot.token_unreadable", extra={
        "event": "bot.token_unreadable", "offering_id": None if row is None else str(row[0]),
        "reason": reason if reason in UNREADABLE_REASONS else "malformed"})


# --------------------------------------------------------------------------------------------------
# Credencial (a leitura do token)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Credential:
    bot_id: int
    token_version: int
    state: str
    blob: bytes
    bot_uuid: uuid.UUID
    telegram_bot_id: int


def _decrypt(cipher: TokenCipher, credential: _Credential) -> str:
    return cipher.decrypt(credential.blob, bot_uuid=credential.bot_uuid,
                          telegram_bot_id=credential.telegram_bot_id)


# --------------------------------------------------------------------------------------------------
# Perfil
# --------------------------------------------------------------------------------------------------


def _record_profile(conn: psycopg.Connection, bot_id: int, profile_state: str, purpose: str,
                    now: datetime) -> None:
    """Grava o perfil e `bot.profile_updated`, na mesma transação."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE bots b SET profile_state = %s FROM offerings o WHERE o.id = b.offering_id "
            "AND b.id = %s RETURNING o.offering_id",
            (profile_state, bot_id),
        ).fetchone()
        if row is None:
            raise LookupError("bot inexistente")
        append_event(conn, EventType.BOT_PROFILE_UPDATED,
                     {"offering_id": row[0], "profile_state": profile_state, "purpose": purpose},
                     occurred_at=now, actor_token_id=None)


def _profile_bot_id(task: ClaimedTask) -> int:
    payload = task.payload
    bot_id = payload.get("bot_id") if isinstance(payload, dict) else None
    if isinstance(bot_id, bool) or not isinstance(bot_id, int):
        raise PermanentTaskError("payload sem bot_id")
    return bot_id


def profile_task_handler(conninfo: str, *, clock: Clock, runtime: BotRuntime | None,
                         settings: Settings) -> Callable[[ClaimedTask], None]:
    """O tratador da tarefa `bot.sync_profile`, para `run_worker`.

    Lê o estado **atual** do oferecimento: não arquivado → `setMyDescription(DESCRIPTION_ACTIVE)` e
    `setMyCommands(COMMANDS)`; arquivado → só `setMyDescription(DESCRIPTION_ARCHIVED)` (`purpose`
    efetivo). Os resultados:
    - sucesso → `configured` e `bot.profile_updated {configured}`;
    - `RetryAfter(n)` → `RetryTaskLater(n)`; `TelegramUnavailable` (e os demais erros transitórios da
      porta) → falha temporária com a própria exceção;
    - `TokenRejected` → perda de acesso, `failed` e `bot.profile_updated {failed}`, falha
      definitiva; `RequestRejected` → `failed`, falha definitiva;
    - bot `no_access` ou token ilegível → (ilegível: perda de acesso com `token_unreadable`) `failed`,
      falha definitiva, sem chamar o Telegram;
    - sem chave-mestra (`runtime` ou chave ausente) → falha temporária `MasterKeyMissing`, sem
      chamar o Telegram;
    - numa falha temporária da última tentativa (`attempts >= max_attempts`), o tratador grava
      `failed` e `bot.profile_updated {failed}` antes de relançar.

    Cada chamada abre a própria conexão; nenhuma chamada ao Telegram roda dentro de transação."""

    def handle(task: ClaimedTask) -> None:
        bot_id = _profile_bot_id(task)
        last_attempt = task.attempts >= settings.max_attempts
        with db.connect(conninfo, autocommit=True) as conn:
            row = conn.execute(
                "SELECT b.state, b.token_version, b.token_ciphertext, b.bot_uuid, b.telegram_bot_id, "
                "o.state FROM bots b JOIN offerings o ON o.id = b.offering_id WHERE b.id = %s",
                (bot_id,),
            ).fetchone()
            if row is None:
                raise PermanentTaskError("bot inexistente")
            credential = _Credential(bot_id=bot_id, token_version=int(row[1]), state=row[0],
                                     blob=bytes(row[2]), bot_uuid=row[3], telegram_bot_id=int(row[4]))
            purpose = "archived" if row[5] == "archived" else "active"

            if credential.state != "registered":
                _record_profile(conn, bot_id, "failed", purpose, clock.now())
                raise PermanentTaskError("bot sem acesso")
            cipher = runtime.cipher if runtime is not None else None
            if runtime is None or cipher is None:
                if last_attempt:
                    _record_profile(conn, bot_id, "failed", purpose, clock.now())
                raise MasterKeyMissing("worker sem chave-mestra")
            try:
                token = _decrypt(cipher, credential)
            except UnreadableToken as exc:
                with conn.transaction():
                    opened = mark_no_access(conn, bot_id=bot_id, token_version=credential.token_version,
                                            problem="token_unreadable", now=clock.now())
                    _record_profile(conn, bot_id, "failed", purpose, clock.now())
                if opened:
                    log_token_unreadable(conn, bot_id=bot_id, reason=exc.reason)
                raise PermanentTaskError("token ilegível") from None

            name = "DESCRIPTION_ARCHIVED" if purpose == "archived" else "DESCRIPTION_ACTIVE"
            description = offering_text(conn, name, bot_id=bot_id)
            failure: Exception | None = None
            api = runtime.api_factory(token)
            try:
                api.set_my_description(description)
                if purpose == "active":
                    api.set_my_commands(botmessages.COMMANDS)
            except TokenRejected:
                with conn.transaction():
                    mark_no_access(conn, bot_id=bot_id, token_version=credential.token_version,
                                   problem="token_rejected", now=clock.now())
                    _record_profile(conn, bot_id, "failed", purpose, clock.now())
                failure = PermanentTaskError("token recusado")
            except RequestRejected:
                _record_profile(conn, bot_id, "failed", purpose, clock.now())
                failure = PermanentTaskError("pedido recusado")
            except RetryAfter as exc:
                if last_attempt:
                    _record_profile(conn, bot_id, "failed", purpose, clock.now())
                failure = RetryTaskLater(exc.seconds)
            except TelegramError as exc:
                if last_attempt:
                    _record_profile(conn, bot_id, "failed", purpose, clock.now())
                failure = exc
            finally:
                api.close()
            # Fora do `except`: a falha não leva a cadeia da porta (que já não tem token nem URL).
            if failure is not None:
                raise failure from None
            _record_profile(conn, bot_id, "configured", purpose, clock.now())

    return handle


# --------------------------------------------------------------------------------------------------
# Entrega por bot
# --------------------------------------------------------------------------------------------------

# O formatador de cada dialeto gravado; os três vêm do núcleo
# (`telegramrender`), nunca de `cq_server.adapters`.
_FORMATTERS: dict[str, Callable[[], MessageFormatter]] = {
    "rich": TelegramRichFormatter, "html": TelegramHtmlFormatter, "plain": PlainFormatter}


def telegram_capabilities(delivery: DeliverySettings) -> ChannelCapabilities:
    """As capacidades do canal pelo modo `CQ_TELEGRAM_FORMAT`: `rich` edita, tem
    botões, fórmulas e formatação rica; `html` edita e tem botões, sem fórmulas nem formatação rica."""
    if delivery.telegram_format == "rich":
        return ChannelCapabilities(can_edit=True, max_buttons=delivery.max_buttons, supports_math=True,
                                   supports_rich_text=True)
    if delivery.telegram_format == "html":
        return ChannelCapabilities(can_edit=True, max_buttons=delivery.max_buttons, supports_math=False,
                                   supports_rich_text=False)
    raise ValueError(f"modo do Telegram desconhecido: {delivery.telegram_format!r}")


class TelegramChannel:
    """Canal de entrega pelos bots; sem rota, a mensagem vai ao `default`.

    Com rota: endereço `inactive`/`blocked` → `Undeliverable` sem chamar o Telegram; bot `no_access`
    → `RetryLater("bot_no_access")`; token ilegível → perda de acesso (`token_unreadable`) e
    `RetryLater`; senão, a chamada pelo bot da mensagem, pela forma dela (docstring do módulo), com os
    `reason` listados no docstring do módulo. Tem a sua conexão (`LazyConnection`) e é usado por uma thread só.

    O canal deixa a sua `conninfo` no `runtime` (se ele ainda não tem uma), para que
    `answer_callback`, chamado sem conexão, leia o token do bot."""

    capabilities: ChannelCapabilities

    def __init__(self, conninfo: str, *, clock: Clock, runtime: BotRuntime,
                 default: DeliveryChannel, delivery: DeliverySettings = DeliverySettings()) -> None:
        self._clock = clock
        self._runtime = runtime
        self._default = default
        self._delivery = delivery
        self._db = LazyConnection(conninfo)
        self.capabilities = telegram_capabilities(delivery)
        self._formatter = self.formatter_for(delivery.telegram_format)
        if runtime is not None and getattr(runtime, _CALLBACK_CONNINFO, None) is None:
            setattr(runtime, _CALLBACK_CONNINFO, conninfo)

    @property
    def formatter(self) -> MessageFormatter:
        """O formatador do modo corrente (`rich` → `TelegramRichFormatter`, `html` →
        `TelegramHtmlFormatter`)."""
        return self._formatter

    def formatter_for(self, text_format: str) -> MessageFormatter:
        """O formatador de um dialeto gravado (`rich`, `html`, `plain`; outro → `ValueError`), que
        vale para a entrega aberta mesmo que o modo mude depois."""
        factory = _FORMATTERS.get(text_format) if isinstance(text_format, str) else None
        if factory is None:
            raise ValueError(f"dialeto de texto desconhecido: {text_format!r}")
        return factory()

    def send(self, message: OutgoingMessage) -> Sent | RetryLater | Undeliverable:
        route = message.route
        if route is None:
            return self._default.send(message)
        conn = self._db.get()
        row = conn.execute(
            "SELECT b.state, b.token_version, b.token_ciphertext, b.bot_uuid, b.telegram_bot_id, "
            "(SELECT d.state FROM outbox_messages m JOIN delivery_endpoints d "
            "   ON d.id = m.delivery_endpoint_id WHERE m.id = %s) "
            "FROM bots b WHERE b.id = %s",
            (message.message_id, route.bot_id),
        ).fetchone()
        if row is None:  # pragma: no cover - a chave estrangeira garante o bot da mensagem
            raise LookupError("bot inexistente")
        endpoint_state = row[5]
        if endpoint_state == "inactive":
            return Undeliverable("endpoint_inactive")
        if endpoint_state == "blocked":
            return Undeliverable("endpoint_blocked")
        credential = _Credential(bot_id=route.bot_id, token_version=int(row[1]), state=row[0],
                                 blob=bytes(row[2]), bot_uuid=row[3], telegram_bot_id=int(row[4]))
        if credential.state != "registered":
            return RetryLater("bot_no_access")
        cipher = self._runtime.cipher
        if cipher is None:
            return RetryLater("master_key_missing")
        try:
            token = _decrypt(cipher, credential)
        except UnreadableToken as exc:
            if mark_no_access(conn, bot_id=route.bot_id, token_version=credential.token_version,
                              problem="token_unreadable", now=self._clock.now()):
                log_token_unreadable(conn, bot_id=route.bot_id, reason=exc.reason)
            return RetryLater("token_unreadable")

        api = self._runtime.api_factory(token)
        try:
            if _has_rich_fields(message):
                outcome = self._deliver_rich(api, route.chat_id, message)
            else:
                outcome = self._deliver(api, route.chat_id, message.text)
        finally:
            api.close()
        if outcome is None:
            # 401: perda de acesso (um episódio) e falha temporária.
            mark_no_access(conn, bot_id=route.bot_id, token_version=credential.token_version,
                           problem="token_rejected", now=self._clock.now())
            return RetryLater("token_rejected")
        return outcome

    @staticmethod
    def _deliver(api: TelegramBotApi, chat_id: int, text: str) -> Sent | RetryLater | Undeliverable | None:
        """O resultado do `sendMessage` no caminho simples (docstring do módulo); `None` no `TokenRejected`."""
        try:
            message_ref = api.send_message(chat_id, text)
        except TokenRejected:
            return None
        except RetryAfter as exc:
            return RetryLater("rate_limited", exc.seconds)
        except ChatUnreachable:
            return Undeliverable("chat_unreachable", blocked=True)
        except RequestRejected:
            return Undeliverable("request_rejected")
        except TelegramError:
            # `TelegramUnavailable` e os demais erros transitórios da porta.
            return RetryLater("telegram_unavailable")
        return Sent(str(message_ref))

    # -- o despacho pela forma da mensagem ------------------------------

    def _deliver_rich(self, api: TelegramBotApi, chat_id: int,
                    message: OutgoingMessage) -> Sent | RetryLater | Undeliverable | None:
        """A mensagem com algum campo rico: edição, mídia ou texto, com os resultados do docstring
        do módulo; `None` no `TokenRejected`."""
        try:
            if message.edit_ref is not None:
                return self._edit(api, chat_id, message)
            if message.media is not None:
                return self._send_media(api, chat_id, message)
            return Sent(str(self._send_text(api, chat_id, message)))
        except TokenRejected:
            return None
        except RetryAfter as exc:
            return RetryLater("rate_limited", exc.seconds)
        except ChatUnreachable:
            return Undeliverable("chat_unreachable", blocked=True)
        except RequestRejected:
            return Undeliverable("request_rejected")
        except TelegramError:
            return RetryLater("telegram_unavailable")

    @staticmethod
    def _send_text(api: TelegramBotApi, chat_id: int, message: OutgoingMessage) -> int:
        markup = _reply_markup(message.keyboard, message.reply_request)
        reply = _reply_parameters(message.reply_to_ref)
        if message.text_format == "rich":
            return api.send_rich_message(chat_id, _rich(message.text), reply_markup=markup,
                                         reply_parameters=reply)
        if message.text_format == "html":
            return api.send_message(chat_id, message.text, parse_mode="HTML", reply_markup=markup,
                                    reply_parameters=reply, link_preview_options=_NO_PREVIEW)
        return api.send_message(chat_id, message.text, reply_markup=markup, reply_parameters=reply)

    @staticmethod
    def _edit(api: TelegramBotApi, chat_id: int, message: OutgoingMessage) -> Sent | Undeliverable:
        """A edição da mensagem `edit_ref` no dialeto dela: sem teclado, a edição de
        texto tira o teclado; `MessageNotModified` é sucesso; outro 400 é `edit_rejected`."""
        edit_ref = str(message.edit_ref)
        if not edit_ref.isdigit():
            return Undeliverable("edit_rejected")
        target = int(edit_ref)
        keyboard = _inline_keyboard(message.keyboard)
        try:
            if message.markup_only:
                api.edit_message_reply_markup(chat_id, target,
                                              keyboard if keyboard is not None else {"inline_keyboard": []})
            elif message.text_format == "rich":
                api.edit_message_text(chat_id, target, rich_message=_rich(message.text), reply_markup=keyboard)
            elif message.text_format == "html":
                api.edit_message_text(chat_id, target, text=message.text, parse_mode="HTML",
                                      reply_markup=keyboard, link_preview_options=_NO_PREVIEW)
            else:
                api.edit_message_text(chat_id, target, text=message.text, reply_markup=keyboard)
        except MessageNotModified:
            return Sent(edit_ref)
        except RequestRejected:
            return Undeliverable("edit_rejected")
        return Sent(edit_ref)

    @staticmethod
    def _send_media(api: TelegramBotApi, chat_id: int, message: OutgoingMessage) -> Sent | Undeliverable:
        """A mídia como foto ou documento: um 400 na foto leva a **uma** nova
        tentativa como documento, com os mesmos bytes, nome e legenda, nesta mesma chamada."""
        media = message.media
        assert media is not None
        upload = UploadFile(filename=media.filename, content_type=media.content_type, data=media.data)
        reply = _reply_parameters(message.reply_to_ref)
        if not media.as_document:
            try:
                return Sent(str(api.send_photo(chat_id, upload, caption=media.caption, reply_parameters=reply)))
            except RequestRejected:
                pass  # o 400 da foto não entregou nada: segue como documento
        try:
            return Sent(str(api.send_document(chat_id, upload, caption=media.caption, reply_parameters=reply)))
        except RequestRejected:
            return Undeliverable("media_rejected")

    def close(self) -> None:
        """Fecha a conexão do canal (idempotente)."""
        self._db.close()


def _has_rich_fields(message: OutgoingMessage) -> bool:
    """Algum campo rico fora do padrão; sem nenhum, o caminho é o simples. O
    `answerable` sozinho não muda o despacho do Telegram."""
    return (message.text_format != "plain" or message.keyboard is not None
            or message.reply_request is not None or message.reply_to_ref is not None
            or message.edit_ref is not None or message.markup_only or message.media is not None)


# A prévia de link desligada no HTML simples.
_NO_PREVIEW = {"is_disabled": True}
# O atributo do `BotRuntime` com a `conninfo` do canal (para `answer_callback` sem conexão).
_CALLBACK_CONNINFO = "_callback_conninfo"


def _rich(text: str) -> dict[str, Any]:
    """O `rich_message` do Rich: sempre com `skip_entity_detection`."""
    return {"html": text, "skip_entity_detection": True}


def _inline_keyboard(keyboard: tuple[tuple[InlineButton, ...], ...] | None) -> dict[str, Any] | None:
    if keyboard is None:
        return None
    return {"inline_keyboard": [[{"text": button.label, "callback_data": button.data} for button in row]
                                for row in keyboard if row]}


def _reply_markup(keyboard: tuple[tuple[InlineButton, ...], ...] | None,
                  reply_request: ReplyRequest | None) -> dict[str, Any] | None:
    """O teclado inline ou, sem ele, o `ForceReply` do pedido de campo."""
    inline = _inline_keyboard(keyboard)
    if inline is not None:
        return inline
    if reply_request is None:
        return None
    markup: dict[str, Any] = {"force_reply": True}
    if reply_request.placeholder:
        markup["input_field_placeholder"] = reply_request.placeholder
    return markup


def _reply_parameters(reply_to_ref: str | None) -> dict[str, Any] | None:
    """`reply_parameters` sempre com `allow_sending_without_reply: true`."""
    if reply_to_ref is None or not str(reply_to_ref).isdigit():
        return None
    return {"message_id": int(reply_to_ref), "allow_sending_without_reply": True}


def answer_callback(runtime: BotRuntime, bot_id: int, callback_id: str, text: str | None, *,
                    conn: psycopg.Connection | None = None) -> None:
    """A confirmação de um toque, direto pela porta
    (`answer_callback_query`), depois do commit, fora da fila e do limitador; uma falha vira o registro
    `telegram.callback_answer_failed`, sem efeito.

    O token é lido do banco pela `conn` (só-nomeado, aditivo; fora de transação de escrita) ou, sem ela,
    por uma conexão curta com a `conninfo` que o `TelegramChannel` do worker deixou no `runtime`. O
    registro leva só o `bot_id` interno e o nome do tipo do erro (nunca o id do toque, o chat, o texto
    nem o token)."""
    try:
        token = _callback_token(runtime, int(bot_id), conn)
        api = runtime.api_factory(token)
        del token
        try:
            api.answer_callback_query(callback_id, text=text)
        finally:
            api.close()
    except Exception as exc:  # noqa: BLE001 - a confirmação nunca impede nada
        logger.warning("telegram.callback_answer_failed", extra={
            "event": "telegram.callback_answer_failed", "bot_id": int(bot_id), "error": type(exc).__name__})


def _callback_token(runtime: BotRuntime, bot_id: int, conn: psycopg.Connection | None) -> str:
    """O token do bot para a confirmação: bot `registered` e chave-mestra; senão, `LookupError`."""
    cipher = runtime.cipher
    if cipher is None:
        raise MasterKeyMissing("worker sem chave-mestra")
    query = ("SELECT state, token_version, token_ciphertext, bot_uuid, telegram_bot_id FROM bots "
             "WHERE id = %s")
    if conn is not None:
        row = conn.execute(query, (bot_id,)).fetchone()
    else:
        conninfo = getattr(runtime, _CALLBACK_CONNINFO, None)
        if not conninfo:
            raise LookupError("sem conexão para ler o token")
        with db.connect(conninfo, autocommit=True) as own:
            row = own.execute(query, (bot_id,)).fetchone()
    if row is None or row[0] != "registered":
        raise LookupError("bot sem acesso")
    credential = _Credential(bot_id=bot_id, token_version=int(row[1]), state=row[0], blob=bytes(row[2]),
                             bot_uuid=row[3], telegram_bot_id=int(row[4]))
    return _decrypt(cipher, credential)
