"""Fila de saída transacional.

- **Enfileirar** (`enqueue`): sempre na transação do chamador, junto com a mudança que gerou a
  mensagem (atomicidade): uma transação desfeita não deixa mensagem. As validações vêm antes de
  qualquer escrita, e a chave de idempotência é conferida por consulta, com `ON CONFLICT DO NOTHING`
  só para a corrida: um `ValueError` não deixa erro no banco.
- **Despachar** (`dispatch_once`): a posse é tomada e confirmada numa transação própria, **antes** do
  envio; o `send` do canal roda fora de qualquer transação; o resultado é registrado noutra
  transação, se a mensagem ainda estiver em posse deste worker com o mesmo `attempts` (ninguém a
  retomou), mesmo que a posse tenha vencido durante o `send`. Se outro worker a retomou, o resultado
  é descartado e só vai para o log técnico (`outbox.result_discarded`, sem o `reason`).
- **Exceção do canal:** uma `Exception` no `send` vale como `RetryLater` sem `retry_after`, com o
  texto `type(exc).__name__`; um resultado fora da porta vale como a exceção `TypeError`. Uma
  `BaseException` que não é `Exception` (queda) escapa e deixa a mensagem em posse.
- Ao virar `failed`, o `last_error` (redigido) e o evento `outbox.failed` entram na mesma transação.
  Todo resultado com erro lê as identidades da redação depois do lock consultivo dos eventos
  (`events.load_write_identities`), sempre depois da trava da linha.
- U+0000 ou substituto UTF-16 isolado no texto ou na chave é recusado no `enqueue` (`ValueError`);
  no `channel_ref` e no `reason` do canal, vira U+FFFD (`_leasing.storable_text`), para que
  registrar o resultado nunca falhe (a falha deixaria a mensagem em posse).

**Bots:**
- **Destinos** (CHECK de destino único): (a) a pessoa, sem bot (a mensagem sem rota); (b) a pessoa por
  um bot: `enqueue(recipient=…, bot_id=…)` resolve o endereço de entrega `active` da pessoa naquele
  bot (`ValueError` se não houver); (c) a resposta ao chat de um update:
  `enqueue(bot_id=…, reply_to_update_id=…)`, sem pessoa. Outras combinações: `ValueError`, sem erro
  no banco. Com a chave repetida, o conteúdo é o mesmo se `(recipient_person_id, bot_id,
  reply_to_update_id, text)` coincidem por `IS NOT DISTINCT FROM`.
- `dispatch_once(…, include_routed=False)` (o padrão, o comportamento sem bots) só pega mensagens sem
  bot; com `True`, a mensagem com bot sai com `route = BotRoute(bot_id, token_version, chat_id)`
  (a versão atual do token do bot e o chat do endereço ou do update).
- `Undeliverable(blocked=True)`: a mensagem falha de vez e o endereço `active` dela fica `blocked`,
  com `outbox.failed` e depois `endpoint.blocked`, na mesma transação. A falha definitiva de uma
  resposta sem pessoa gera `outbox.reply_failed` no lugar de `outbox.failed`.

Todo horário vem do `Clock` injetado; a ordem é sempre a de `id`. Cada instância é usada por
uma thread e tem a sua conexão (`_leasing.LazyConnection`).

**Entregas:**
as mensagens de uma entrega não guardam texto (o
conteúdo é lido na posse pelo `DeliveryContent`, e o desfecho vai ao `DeliveryOutcome` na transação do
resultado); `enqueue_delivery`, `enqueue_delivery_edit` (fusão das edições pendentes) e
`cancel_delivery_messages`; `enqueue(…, priority=0)`; o `SendPacer` na posse. Com `content`, `outcome`
e `pacer` nulos (o padrão), o comportamento é o da posse legada (de uma transação, que só não toma as
mensagens de entrega, que não têm texto). Com algum deles, a posse tem duas
transações:
1. **posse esgotada**, uma linha por vez (`FOR NO KEY UPDATE SKIP LOCKED LIMIT 1`): numa linha com
   `delivery_id`, o `DeliveryOutcome` (`Exhausted`) roda **antes** do `_mark_failed`;
2. **escolha**: até 32 candidatas em `ORDER BY priority, id` (`FOR NO KEY UPDATE OF m SKIP LOCKED`), a primeira
   que o `SendPacer` permite (só as com rota passam por ele); uma mensagem de entrega só é candidata se
   nenhuma anterior da mesma entrega está `pending`/`leased` e se o bot dela está `registered`.
   Essa transação não trava endereço nem entrega e não chama o domínio.
Depois do commit da escolha, o `DeliveryContent.load` lê o conteúdo sem trava; um `Skip` leva a linha a
`cancelled` numa transação curta própria. No resultado de uma linha com `delivery_id`, o
`DeliveryOutcome.record` roda antes de `_mark_failed`, `_block_endpoint` e `load_write_identities`; o
`RetryLater` (ou a exceção do canal) numa edição absorve a edição `pending` da mesma (entrega, papel)
(`_absorb_pending_edit`). Um `RetryLater` com `retry_after` numa mensagem com rota chama `SendPacer.penalize`.

As funções de módulo das mensagens de entrega (`enqueue_delivery`, `enqueue_delivery_edit`,
`cancel_delivery_messages`) rodam na transação do chamador, que já travou a entrega.
O horário (`available_at`, `created_at`) vem do argumento só-nomeado `now` (o `Clock` do chamador,
injetado); sem ele, é o horário mais recente já gravado na entrega (nunca o relógio do banco).

**Avisos à equipe e prévia:**
`PRIORITIES = (0, 1, 2)` (a prioridade 2 dos avisos à equipe e da `PREVIEW_REPLY`,
aceita também pelo `priority` de `enqueue`); `NoticeRow` e o `Protocol` `NoticeContent` (a carga das
mensagens da prévia na posse); as funções de módulo `enqueue_notice_text` (a linha de texto do aviso,
com `notice_id` e `text_format`) e `enqueue_notice_content` (a forma 3, sem texto), ambas com
`priority = 2`, no molde de `enqueue_delivery`. Na posse:
- de duas transações, uma mensagem de aviso só é candidata se nenhuma mensagem de aviso anterior do
  mesmo endereço está `pending`/`leased` (a ordem por endereço); a forma 3 só é candidata com o
  `notice_content`, e é carregada por ele depois do commit da escolha, como a de entrega;
- legada (`content`, `outcome` e `pacer` nulos, `_legacy` verdadeiro): só linhas com texto
  (`m.text IS NOT NULL`), então a forma 3 nunca sai por ela, nem com o `notice_content`;
- a linha de texto com `text_format` sai como `OutgoingMessage(text=…, text_format=…)`, e a de um aviso
  `dispatch_preview`, com `answerable = True`; sem `text_format`, a mensagem comum de texto.
As regras de falha são as mesmas para todas.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

import psycopg

from cq_server._leasing import (
    LEASE_EXHAUSTED_ERROR,
    MAX_WAIT_SECONDS,
    LazyConnection,
    backoff_seconds,
    has_unstorable,
    storable_text,
    utc,
)
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.events import EventType, append_event, load_write_identities, redact_free_text
from cq_server.ports.delivery import (
    BotRoute,
    DeliveryChannel,
    OutgoingMessage,
    RetryLater,
    Sent,
    Undeliverable,
)

if TYPE_CHECKING:
    from cq_server.deliveries import DeliveryContent, DeliveryOutcome, DeliveryRow, Skip
    from cq_server.pacing import SendPacer

logger = logging.getLogger("cq_server.worker")

MIN_TEXT_LENGTH = 1
MAX_TEXT_LENGTH = 4096
# As prioridades aceitas (0 para tudo; 1 para as respostas fixas às mensagens livres;
# 2 para os avisos à equipe, a prévia e a `PREVIEW_REPLY`).
PRIORITIES = (0, 1, 2)
# Quantas candidatas a escolha da posse considera de uma vez.
CLAIM_CANDIDATES = 32
EDIT_OPERATIONS = ("edit_text", "edit_markup")


@dataclass(frozen=True)
class OutboxRecord:
    id: int
    recipient: uuid.UUID | None
    text: str
    state: str
    attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_until: datetime | None
    channel_ref: str | None
    last_error: str | None
    sent_at: datetime | None
    bot_id: int | None = None


@dataclass(frozen=True)
class _Claimed:
    id: int
    recipient: uuid.UUID | None
    text: str
    attempts: int
    route: BotRoute | None = None
    endpoint_id: int | None = None
    reply_to: int | None = None
    # A linha de entrega (`deliveries.DeliveryRow`), quando a mensagem tem `delivery_id`.
    delivery: DeliveryRow | None = None
    # O dialeto gravado da linha de texto (`None` sem `text_format`), se ela aceita resposta (o cabeçalho
    # da prévia) e a linha da prévia sem texto (forma 3), lida pelo `NoticeContent`.
    text_format: str | None = None
    answerable: bool = False
    notice: NoticeRow | None = None


@dataclass(frozen=True)
class _Result:
    outcome: str  # `sent`, `retry` ou `undeliverable`
    detail: str   # o `channel_ref` (enviada) ou o `reason`
    retry_after: float | None = None
    blocked: bool = False


def _as_uuid(value: object) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise ValueError("destinatário inválido: esperado um pseudo_id") from None


def _as_id(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} deve ser um inteiro")
    return value


def _normalize_result(result: object) -> _Result:
    """O desfecho de um resultado do canal (`sent`, `retry`, `undeliverable`).

    Para `sent`, o texto é o `channel_ref`; para os outros, o `reason`. Um resultado fora da porta
    vale como a exceção `TypeError` do canal (só o nome do tipo).
    """
    if isinstance(result, Sent) and isinstance(result.channel_ref, str):
        return _Result("sent", storable_text(result.channel_ref))
    if isinstance(result, RetryLater) and isinstance(result.reason, str):
        retry_after = result.retry_after_seconds
        if isinstance(retry_after, bool) or not isinstance(retry_after, (int, float)) \
                or not math.isfinite(retry_after):
            retry_after = None
        return _Result("retry", storable_text(result.reason), retry_after)
    if isinstance(result, Undeliverable) and isinstance(result.reason, str):
        return _Result("undeliverable", storable_text(result.reason), blocked=result.blocked is True)
    return _Result("retry", TypeError.__name__)


@dataclass(frozen=True)
class NoticeRow:
    """Uma mensagem da prévia (a forma 3: `notice_id`, sem texto), como a
    posse a vê, no molde de `deliveries.DeliveryRow`."""

    message_id: int
    notice_row_id: int
    role: str
    media_id: str | None
    field_index: int | None
    bot_id: int
    endpoint_row_id: int
    recipient: uuid.UUID
    attempts: int


class NoticeContent(Protocol):
    """Lê, na posse, o conteúdo de uma mensagem da prévia (sem trava, fora de transação de escrita)."""

    def load(self, conn: psycopg.Connection, row: NoticeRow) -> OutgoingMessage | Skip: ...


class Outbox:
    """Fila de saída sobre a tabela `outbox_messages` (ver o docstring do módulo)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings,
                 content: DeliveryContent | None = None, outcome: DeliveryOutcome | None = None,
                 pacer: SendPacer | None = None, notice_content: NoticeContent | None = None) -> None:
        self._content = content
        # O carregador das mensagens da prévia (a forma 3); sem ele, elas não são candidatas.
        self._notice_content = notice_content
        self._outcome = outcome
        self._pacer = pacer
        self._conninfo = conninfo
        self._clock = clock
        self._settings = settings
        self._db = LazyConnection(conninfo)
        self._secrets = (settings.admin_token,)

    # --- enfileirar -----------------------------------------------------------------------------

    def enqueue(self, conn: psycopg.Connection, *, recipient: uuid.UUID | None = None, text: str,
                idempotency_key: str | None = None, bot_id: int | None = None,
                reply_to_update_id: int | None = None, priority: int = 0) -> int:
        """Enfileira uma mensagem na transação de `conn` e devolve o id (o da existente, se a chave
        repetir com o mesmo destino e texto). Destinos: `recipient`, `recipient` + `bot_id`
        (pelo endereço `active` da pessoa no bot) ou `bot_id` + `reply_to_update_id` (ao chat do
        update). `ValueError` se o texto não tiver de 1 a 4096 caracteres, se o texto ou a chave
        tiver NUL ou substituto UTF-16 isolado, se o destino for outra combinação ou não existir
        (pessoa, endereço ativo, update do bot com chat) ou se a chave repetir com outro conteúdo.

        `priority` (0 para tudo, 1 para as respostas fixas às mensagens livres); outro
        valor é `ValueError`. A chave repetida devolve a mensagem existente sem mudar a prioridade.
        A prioridade 2 também vale, para a `PREVIEW_REPLY` (`previews.process_reply`)."""
        if isinstance(priority, bool) or priority not in PRIORITIES:
            raise ValueError("priority deve ser 0, 1 ou 2")
        if not isinstance(text, str) or not MIN_TEXT_LENGTH <= len(text) <= MAX_TEXT_LENGTH:
            raise ValueError(f"o texto deve ter de {MIN_TEXT_LENGTH} a {MAX_TEXT_LENGTH} caracteres")
        # O substituto isolado não tem UTF-8 (o psycopg não o codifica): é recusado aqui, antes de
        # qualquer SQL, como o NUL.
        if has_unstorable(text):
            raise ValueError("o texto não pode conter o caractere NUL nem substituto UTF-16 isolado")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str):
                raise ValueError("idempotency_key deve ser texto")
            if has_unstorable(idempotency_key):
                raise ValueError("idempotency_key não pode conter o caractere NUL nem substituto "
                                 "UTF-16 isolado")
        if bot_id is None and reply_to_update_id is None:
            person_id = self._person_id(conn, recipient)
            reply_to: int | None = None
        elif bot_id is not None and reply_to_update_id is None and recipient is not None:
            bot_id = _as_id(bot_id, "bot_id")
            person_id = self._person_id(conn, recipient)
            reply_to = None
        elif bot_id is not None and reply_to_update_id is not None and recipient is None:
            bot_id = _as_id(bot_id, "bot_id")
            reply_to = _as_id(reply_to_update_id, "reply_to_update_id")
            person_id = None
            found = conn.execute(
                "SELECT 1 FROM telegram_updates WHERE id = %s AND bot_id = %s AND chat_id IS NOT NULL",
                (reply_to, bot_id),
            ).fetchone()
            if found is None:
                raise ValueError("update inexistente, de outro bot ou sem chat")
        else:
            raise ValueError("destino inválido: use recipient; recipient e bot_id; ou bot_id e "
                             "reply_to_update_id")

        if idempotency_key is not None:
            existing = self._existing(conn, person_id, bot_id, reply_to, text, idempotency_key)
            if existing is not None:
                return existing
        endpoint_id: int | None = None
        if bot_id is not None and person_id is not None:
            row = conn.execute(
                "SELECT id FROM delivery_endpoints WHERE person_id = %s AND bot_id = %s "
                "AND channel = 'telegram' AND state = 'active'",
                (person_id, bot_id),
            ).fetchone()
            if row is None:
                raise ValueError("a pessoa não tem endereço de entrega ativo nesse bot")
            endpoint_id = int(row[0])
        now = self._clock.now()
        row = conn.execute(
            "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, "
            "reply_to_update_id, text, idempotency_key, state, attempts, available_at, created_at, priority) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
            (person_id, bot_id, endpoint_id, reply_to, text, idempotency_key, now, now, int(priority)),
        ).fetchone()
        if row is not None:
            return int(row[0])
        # Corrida: outra transação gravou a mesma chave entre a consulta e o INSERT.
        assert idempotency_key is not None
        existing = self._existing(conn, person_id, bot_id, reply_to, text, idempotency_key)
        if existing is None:  # pragma: no cover - só com isolamento acima de READ COMMITTED
            raise ValueError("idempotency_key em uso por uma transação concorrente")
        return existing

    @staticmethod
    def _person_id(conn: psycopg.Connection, recipient: object) -> int:
        pseudo_id = _as_uuid(recipient)
        row = conn.execute("SELECT id FROM people WHERE pseudo_id = %s", (pseudo_id,)).fetchone()
        if row is None:
            raise ValueError("destinatário inexistente")
        return int(row[0])

    @staticmethod
    def _existing(conn: psycopg.Connection, person_id: int | None, bot_id: int | None,
                  reply_to: int | None, text: str, idempotency_key: str) -> int | None:
        row = conn.execute(
            "SELECT id, recipient_person_id IS NOT DISTINCT FROM %s::bigint "
            "AND bot_id IS NOT DISTINCT FROM %s::bigint "
            "AND reply_to_update_id IS NOT DISTINCT FROM %s::bigint AND text = %s "
            "FROM outbox_messages WHERE idempotency_key = %s",
            (person_id, bot_id, reply_to, text, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        message_id, same = row
        if not same:
            raise ValueError("idempotency_key já usada com outro conteúdo")
        return int(message_id)

    # --- despachar ------------------------------------------------------------------------------

    def dispatch_once(self, channel: DeliveryChannel, worker_id: str, *,
                      include_routed: bool = False) -> int | None:
        """Pega uma mensagem, entrega pelo `channel` e registra o resultado; devolve o id, ou `None`
        se não houver mensagem disponível. Registra `outbox.dispatched` com o estado final.

        Com `include_routed=False` (o padrão), só as mensagens sem bot; com `True`, também as com
        bot, que saem com a rota (`OutgoingMessage.route`).

        Com `content`, `outcome` ou `pacer`, a posse é a de duas transações (docstring do módulo);
        a mensagem de entrega sai com o conteúdo do `DeliveryContent`, e um `Skip` a leva a `cancelled`
        sem chamar o canal (devolve o id, sem `outbox.dispatched`)."""
        if self._legacy:
            claimed = self._claim(worker_id, include_routed=include_routed)
        else:
            self._expire_exhausted(include_routed=include_routed)
            claimed = self._choose(worker_id, include_routed=include_routed)
        if claimed is None:
            return None
        message = OutgoingMessage(message_id=claimed.id, recipient=claimed.recipient, text=claimed.text,
                                  route=claimed.route)
        if claimed.text_format is not None:
            # A linha de texto do aviso sai no dialeto gravado; a do
            # cabeçalho da prévia aceita resposta. Sem `text_format`, a mensagem comum de texto.
            message = dataclasses.replace(message, text_format=claimed.text_format,
                                          answerable=claimed.answerable)
        result: _Result | None = None
        if claimed.delivery is not None or claimed.notice is not None:
            loaded = self._load(claimed)
            if isinstance(loaded, _Result):
                result = loaded
            elif isinstance(loaded, OutgoingMessage):
                message = dataclasses.replace(loaded, message_id=claimed.id, recipient=claimed.recipient,
                                              route=claimed.route)
            else:
                self._cancel_skipped(claimed, worker_id, loaded.reason)
                return claimed.id
        if result is None:
            try:
                sent: object = channel.send(message)
            except Exception as exc:  # noqa: BLE001 - falha temporária do canal; só o nome do tipo
                result = _Result("retry", type(exc).__name__)
            else:
                result = _normalize_result(sent)
        if (self._pacer is not None and claimed.route is not None and result.outcome == "retry"
                and result.retry_after is not None and result.retry_after > 0):
            # O `retry_after` de um 429 segura o chat e o bot no limitador.
            self._pacer.penalize(claimed.route.bot_id, claimed.route.chat_id, float(result.retry_after))

        state = self._record_result(claimed, worker_id, result)
        if state is None:
            logger.info("outbox.result_discarded",
                        extra={"event": "outbox.result_discarded", "id": claimed.id,
                               "outcome": result.outcome})
        else:
            logger.info("outbox.dispatched",
                        extra={"event": "outbox.dispatched", "id": claimed.id, "state": state})
        return claimed.id

    @property
    def _legacy(self) -> bool:
        """A posse legada, de uma transação: sem `content`, `outcome` nem `pacer`."""
        return self._content is None and self._outcome is None and self._pacer is None

    def _claim(self, worker_id: str, *, include_routed: bool = False) -> _Claimed | None:
        """Toma a posse da próxima mensagem disponível, numa transação própria já confirmada (a posse
        legada; as mensagens de entrega, sem texto, ficam de fora)."""
        conn = self._db.get()
        lease = timedelta(seconds=self._settings.lease_seconds)
        while True:
            now = self._clock.now()
            with conn.transaction():
                # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
                row = conn.execute(
                    "SELECT m.id, m.state, m.attempts, p.pseudo_id, m.reply_to_update_id "
                    "FROM outbox_messages m LEFT JOIN people p ON p.id = m.recipient_person_id "
                    "WHERE ((m.state = 'pending' AND m.available_at <= %(now)s) "
                    "   OR (m.state = 'leased' AND m.lease_until < %(now)s)) "
                    "  AND (%(routed)s OR m.bot_id IS NULL) AND m.delivery_id IS NULL "
                    # A forma 3 (aviso sem texto) nunca sai pela posse legada.
                    "  AND m.text IS NOT NULL "
                    "ORDER BY m.id LIMIT 1 FOR NO KEY UPDATE OF m SKIP LOCKED",
                    {"now": now, "routed": bool(include_routed)},
                ).fetchone()
                if row is None:
                    return None
                message_id, state, attempts, pseudo_id, reply_to = row
                if state == "leased" and attempts >= self._settings.max_attempts:
                    # Posse esgotada: falha sem incrementar attempts e sem chamar o canal; a busca segue.
                    self._mark_failed(conn, message_id, pseudo_id, reply_to, attempts,
                                      LEASE_EXHAUSTED_ERROR, now)
                    continue
                claimed = conn.execute(
                    "UPDATE outbox_messages SET state = 'leased', lease_owner = %s, lease_until = %s, "
                    "attempts = attempts + 1 WHERE id = %s "
                    "RETURNING id, text, attempts, bot_id, delivery_endpoint_id, text_format, notice_id",
                    (worker_id, now + lease, message_id),
                ).fetchone()
                assert claimed is not None
                text_format, answerable = _text_shape(conn, claimed[5], claimed[6])
                route = None
                bot_id, endpoint_id = claimed[3], claimed[4]
                if bot_id is not None:
                    target = conn.execute(
                        "SELECT b.token_version, coalesce(d.chat_id, u.chat_id) FROM bots b "
                        "LEFT JOIN delivery_endpoints d ON d.id = %s "
                        "LEFT JOIN telegram_updates u ON u.id = %s WHERE b.id = %s",
                        (endpoint_id, reply_to, bot_id),
                    ).fetchone()
                    assert target is not None and target[1] is not None
                    route = BotRoute(bot_id=int(bot_id), token_version=int(target[0]),
                                     chat_id=int(target[1]))
                return _Claimed(id=int(claimed[0]), recipient=pseudo_id, text=claimed[1],
                                attempts=int(claimed[2]), route=route,
                                endpoint_id=None if endpoint_id is None else int(endpoint_id),
                                reply_to=None if reply_to is None else int(reply_to),
                                text_format=text_format, answerable=answerable)

    # --- a posse em duas transações ----------------------------------------------------------------

    def _expire_exhausted(self, *, include_routed: bool) -> None:
        """A posse esgotada, uma linha por vez, cada uma na sua transação: com `delivery_id`,
        o `DeliveryOutcome` (`Exhausted`) trava o endereço e a entrega **antes** do `_mark_failed` (que
        toma o lock dos eventos); sem `delivery_id`, como na posse legada. Sem `outcome`, as mensagens de entrega
        ficam de fora."""
        conn = self._db.get()
        while True:
            now = self._clock.now()
            with conn.transaction():
                # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
                row = conn.execute(
                    "SELECT m.id, m.attempts, p.pseudo_id, m.reply_to_update_id, m.delivery_id "
                    "FROM outbox_messages m LEFT JOIN people p ON p.id = m.recipient_person_id "
                    "WHERE m.state = 'leased' AND m.lease_until < %(now)s AND m.attempts >= %(max)s "
                    "  AND (%(routed)s OR m.bot_id IS NULL) AND (m.delivery_id IS NULL OR %(outcome)s) "
                    "ORDER BY m.id LIMIT 1 FOR NO KEY UPDATE OF m SKIP LOCKED",
                    {"now": now, "max": self._settings.max_attempts, "routed": bool(include_routed),
                     "outcome": self._outcome is not None},
                ).fetchone()
                if row is None:
                    return
                message_id, attempts, pseudo_id, reply_to, delivery_id = row
                if delivery_id is not None and self._outcome is not None:
                    from cq_server.deliveries import Exhausted

                    self._outcome.record(conn, _delivery_row(conn, int(message_id)),
                                         Exhausted(LEASE_EXHAUSTED_ERROR))
                self._mark_failed(conn, message_id, pseudo_id, reply_to, attempts, LEASE_EXHAUSTED_ERROR, now)

    def _choose(self, worker_id: str, *, include_routed: bool) -> _Claimed | None:
        """A escolha da posse: até `CLAIM_CANDIDATES` candidatas em `ORDER BY
        priority, id`, e a primeira que o `SendPacer` permite; numa transação própria, já confirmada, que
        não trava endereço nem entrega e não chama o domínio."""
        conn = self._db.get()
        lease = timedelta(seconds=self._settings.lease_seconds)
        now = self._clock.now()
        deliveries_ok = self._content is not None and self._outcome is not None
        with conn.transaction():
            # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
            rows = conn.execute(
                "SELECT m.id, m.bot_id, coalesce(e.chat_id, u.chat_id) FROM outbox_messages m "
                "LEFT JOIN delivery_endpoints e ON e.id = m.delivery_endpoint_id "
                "LEFT JOIN telegram_updates u ON u.id = m.reply_to_update_id "
                "WHERE ((m.state = 'pending' AND m.available_at <= %(now)s) "
                "   OR (m.state = 'leased' AND m.lease_until < %(now)s AND m.attempts < %(max)s)) "
                "  AND (%(routed)s OR m.bot_id IS NULL) "
                "  AND (m.delivery_id IS NULL OR (%(deliveries)s "
                "       AND NOT EXISTS (SELECT 1 FROM outbox_messages p WHERE p.delivery_id = m.delivery_id "
                "                       AND p.id < m.id AND p.state IN ('pending', 'leased')) "
                "       AND EXISTS (SELECT 1 FROM bots b WHERE b.id = m.bot_id AND b.state = 'registered'))) "
                # A forma 3 só com o `notice_content`; e a ordem por
                # endereço entre as mensagens de aviso.
                "  AND (m.notice_id IS NULL OR m.text IS NOT NULL OR %(notices)s) "
                "  AND (m.notice_id IS NULL OR NOT EXISTS (SELECT 1 FROM outbox_messages p "
                "       WHERE p.notice_id IS NOT NULL AND p.delivery_endpoint_id = m.delivery_endpoint_id "
                "       AND p.id < m.id AND p.state IN ('pending', 'leased'))) "
                "ORDER BY m.priority, m.id LIMIT %(limit)s FOR NO KEY UPDATE OF m SKIP LOCKED",
                {"now": now, "max": self._settings.max_attempts, "routed": bool(include_routed),
                 "deliveries": deliveries_ok, "notices": self._notice_content is not None,
                 "limit": CLAIM_CANDIDATES},
            ).fetchall()
            chosen: int | None = None
            # `chat_id: int | None` (o do endereço pode ser `NULL`
            # depois do expurgo); sem chat, a mensagem não passa pelo `SendPacer`.
            for message_id, bot_id, chat_id in rows:
                if (self._pacer is None or bot_id is None or chat_id is None
                        or self._pacer.allows(int(bot_id), int(chat_id))):
                    chosen = int(message_id)
                    break
            if chosen is None:
                return None
            claimed = self._lease(conn, chosen, worker_id, now + lease)
            if self._pacer is not None and claimed.route is not None:
                self._pacer.record(claimed.route.bot_id, claimed.route.chat_id)
            return claimed

    def _lease(self, conn: psycopg.Connection, message_id: int, worker_id: str,
               until: datetime) -> _Claimed:
        """Toma a posse da linha já travada: `leased`, `attempts + 1`, com a rota e a linha de entrega."""
        claimed = conn.execute(
            "UPDATE outbox_messages SET state = 'leased', lease_owner = %s, lease_until = %s, "
            "attempts = attempts + 1 WHERE id = %s "
            "RETURNING id, text, attempts, bot_id, delivery_endpoint_id, reply_to_update_id, "
            "recipient_person_id, delivery_id, text_format, notice_id",
            (worker_id, until, message_id),
        ).fetchone()
        assert claimed is not None
        bot_id, endpoint_id, reply_to, person_id = claimed[3], claimed[4], claimed[5], claimed[6]
        pseudo_id = None
        if person_id is not None:
            found = conn.execute("SELECT pseudo_id FROM people WHERE id = %s", (person_id,)).fetchone()
            pseudo_id = None if found is None else found[0]
        route = None
        if bot_id is not None:
            target = conn.execute(
                "SELECT b.token_version, coalesce(d.chat_id, u.chat_id) FROM bots b "
                "LEFT JOIN delivery_endpoints d ON d.id = %s "
                "LEFT JOIN telegram_updates u ON u.id = %s WHERE b.id = %s",
                (endpoint_id, reply_to, bot_id),
            ).fetchone()
            assert target is not None and target[1] is not None
            route = BotRoute(bot_id=int(bot_id), token_version=int(target[0]), chat_id=int(target[1]))
        delivery = _delivery_row(conn, int(claimed[0])) if claimed[7] is not None else None
        # A linha da prévia sem texto vira `NoticeRow`; a de texto
        # leva o dialeto gravado e, no cabeçalho da prévia, `answerable`.
        notice = None
        text_format, answerable = None, False
        if claimed[9] is not None and claimed[1] is None:
            notice = _notice_row(conn, int(claimed[0]))
        else:
            text_format, answerable = _text_shape(conn, claimed[8], claimed[9])
        return _Claimed(id=int(claimed[0]), recipient=pseudo_id, text=claimed[1] or "",
                        attempts=int(claimed[2]), route=route,
                        endpoint_id=None if endpoint_id is None else int(endpoint_id),
                        reply_to=None if reply_to is None else int(reply_to), delivery=delivery,
                        text_format=text_format, answerable=answerable, notice=notice)

    def _load(self, claimed: _Claimed) -> Any:
        """O conteúdo da mensagem de entrega (ou da mensagem da prévia sem texto, pelo
        `NoticeContent`), lido depois do commit da escolha, sem trava: a
        `OutgoingMessage`, o `Skip` ou, numa falha da leitura, a falha temporária do canal (só o nome do
        tipo)."""
        if claimed.notice is not None:
            assert self._notice_content is not None
        else:
            assert self._content is not None and claimed.delivery is not None
        try:
            if claimed.notice is not None:
                loaded = self._notice_content.load(self._db.get(), claimed.notice)
            else:
                loaded = self._content.load(self._db.get(), claimed.delivery)
        except Exception as exc:  # noqa: BLE001 - falha temporária; só o nome do tipo
            return _Result("retry", type(exc).__name__)
        from cq_server.deliveries import Skip

        if isinstance(loaded, (OutgoingMessage, Skip)):
            return loaded
        return _Result("retry", TypeError.__name__)

    def _cancel_skipped(self, claimed: _Claimed, worker_id: str, reason: str) -> None:
        """`Skip` → `cancelled`, numa transação curta própria, sem travar endereço nem entrega e sem
        chamar o canal."""
        conn = self._db.get()
        with conn.transaction():
            conn.execute(
                "UPDATE outbox_messages SET state = 'cancelled', lease_owner = NULL, lease_until = NULL "
                "WHERE id = %s AND state = 'leased' AND lease_owner = %s AND attempts = %s",
                (claimed.id, worker_id, claimed.attempts),
            )
        logger.info("outbox.skipped", extra={"event": "outbox.skipped", "id": claimed.id,
                                             "reason": storable_text(str(reason))})

    def _record_result(self, claimed: _Claimed, worker_id: str, result: _Result) -> str | None:
        """Registra o resultado do envio e devolve o novo estado, ou `None` se foi descartado."""
        conn = self._db.get()
        now = self._clock.now()
        with conn.transaction():
            # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
            row = conn.execute(
                "SELECT attempts FROM outbox_messages WHERE id = %s AND state = 'leased' "
                "AND lease_owner = %s AND attempts = %s FOR NO KEY UPDATE",
                (claimed.id, worker_id, claimed.attempts),
            ).fetchone()
            if row is None:
                return None  # outro worker retomou a mensagem
            attempts = int(row[0])
            # Numa linha com `delivery_id`, o domínio grava o desfecho na mesma transação, antes de
            # qualquer trava do endereço pela fila e do lock dos eventos.
            delivery = claimed.delivery if self._outcome is not None else None
            if result.outcome == "sent":
                if delivery is not None:
                    self._outcome.record(conn, delivery, Sent(result.detail))
                conn.execute(
                    "UPDATE outbox_messages SET state = 'sent', lease_owner = NULL, lease_until = NULL, "
                    "channel_ref = %s, sent_at = %s WHERE id = %s",
                    (result.detail, now, claimed.id),
                )
                return "sent"
            if result.outcome == "undeliverable" or attempts >= self._settings.max_attempts:
                if delivery is not None:
                    from cq_server.deliveries import Exhausted

                    final: Undeliverable | Exhausted = (
                        Undeliverable(result.detail, blocked=result.blocked)
                        if result.outcome == "undeliverable" else Exhausted(result.detail))
                    self._outcome.record(conn, delivery, final)
                blocking = None
                if result.outcome == "undeliverable" and result.blocked and claimed.endpoint_id is not None:
                    # A trava do endereço vem antes do lock dos eventos.
                    # FOR NO KEY UPDATE (não muda chave): não barra o FOR KEY SHARE das FKs (a ordem fixa
                    # das travas continua a valer).
                    blocking = conn.execute(
                        "SELECT d.id, o.offering_id, p.pseudo_id FROM delivery_endpoints d "
                        "JOIN bots b ON b.id = d.bot_id JOIN offerings o ON o.id = b.offering_id "
                        "JOIN people p ON p.id = d.person_id "
                        "WHERE d.id = %s AND d.state = 'active' FOR NO KEY UPDATE OF d",
                        (claimed.endpoint_id,),
                    ).fetchone()
                self._mark_failed(conn, claimed.id, claimed.recipient, claimed.reply_to, attempts,
                                  result.detail, now)
                if blocking is not None:
                    self._block_endpoint(conn, blocking, now)
                return "failed"
            wait = backoff_seconds(self._settings, attempts)
            if result.retry_after is not None and result.retry_after > wait:
                wait = min(float(result.retry_after), MAX_WAIT_SECONDS)
            # A edição em voo que volta a `pending` absorve a edição `pending` da mesma
            # (entrega, papel), travando a entrega depois da linha e antes do lock dos eventos.
            absorbed = None
            if (claimed.delivery is not None and claimed.delivery.operation in EDIT_OPERATIONS
                    and self._content is not None):
                absorbed = _absorb_pending_edit(conn, claimed.delivery)
            # Mesmo sem evento, o `last_error` é redigido na gravação com as identidades lidas
            # depois do lock dos eventos (ver `events.load_write_identities`).
            identities = load_write_identities(conn)
            conn.execute(
                "UPDATE outbox_messages SET state = 'pending', available_at = %s, lease_owner = NULL, "
                "lease_until = NULL, last_error = %s WHERE id = %s",
                (now + timedelta(seconds=wait),
                 redact_free_text(conn, result.detail, secrets=self._secrets, identities=identities),
                 claimed.id),
            )
            if absorbed is not None:
                version, operation = absorbed
                conn.execute(
                    "UPDATE outbox_messages SET render_version = greatest(render_version, %s), "
                    "operation = CASE WHEN operation = 'edit_text' OR %s = 'edit_text' THEN 'edit_text' "
                    "ELSE 'edit_markup' END WHERE id = %s",
                    (version, operation, claimed.id),
                )
            return "pending"

    def _mark_failed(self, conn: psycopg.Connection, message_id: int, recipient: uuid.UUID | None,
                     reply_to: int | None, attempts: int, error: str, now: datetime) -> None:
        """`failed` + `outbox.failed` (ou, numa resposta sem pessoa, `outbox.reply_failed`), na
        transação de `conn`, com o erro redigido nos dois.

        As identidades são lidas depois do lock dos eventos, e não só no INSERT do evento: um
        cadastro concorrente confirmado antes do evento entra na redação (ver
        `events.load_write_identities`). A posse da linha já foi tomada; o lock vem depois dela.
        """
        identities = load_write_identities(conn)
        redacted = redact_free_text(conn, error, secrets=self._secrets, identities=identities)
        conn.execute(
            "UPDATE outbox_messages SET state = 'failed', lease_owner = NULL, lease_until = NULL, "
            "last_error = %s WHERE id = %s",
            (redacted, message_id),
        )
        if recipient is None:
            assert reply_to is not None
            append_event(
                conn, EventType.OUTBOX_REPLY_FAILED,
                {"message_id": int(message_id), "reply_to": int(reply_to), "attempts": int(attempts),
                 "error": redacted},
                occurred_at=now, actor_token_id=None, secrets=self._secrets, identities=identities,
            )
            return
        append_event(
            conn, EventType.OUTBOX_FAILED,
            {"message_id": int(message_id), "recipient": recipient, "attempts": int(attempts),
             "error": redacted},
            occurred_at=now, actor_token_id=None, secrets=self._secrets, identities=identities,
        )

    @staticmethod
    def _block_endpoint(conn: psycopg.Connection, blocking: tuple, now: datetime) -> None:
        """Endereço `active` → `blocked`, com `endpoint.blocked`."""
        endpoint_id, offering_id, pseudo_id = blocking
        conn.execute("UPDATE delivery_endpoints SET state = 'blocked', changed_at = %s WHERE id = %s",
                     (now, endpoint_id))
        append_event(conn, EventType.ENDPOINT_BLOCKED,
                     {"offering_id": offering_id, "person": pseudo_id, "channel": "telegram"},
                     occurred_at=now, actor_token_id=None)

    # --- consultar e fechar ---------------------------------------------------------------------

    def get(self, message_id: int) -> OutboxRecord:
        """O registro da mensagem; `LookupError` se não existir."""
        row = self._db.get().execute(
            "SELECT m.id, p.pseudo_id, m.text, m.state, m.attempts, m.available_at, m.lease_owner, "
            "m.lease_until, m.channel_ref, m.last_error, m.sent_at, m.bot_id FROM outbox_messages m "
            "LEFT JOIN people p ON p.id = m.recipient_person_id WHERE m.id = %s",
            (message_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"mensagem inexistente: {message_id}")
        return OutboxRecord(id=int(row[0]), recipient=row[1], text=row[2], state=row[3],
                            attempts=int(row[4]), available_at=utc(row[5]), lease_owner=row[6],
                            lease_until=utc(row[7]), channel_ref=row[8], last_error=row[9],
                            sent_at=utc(row[10]), bot_id=None if row[11] is None else int(row[11]))

    def close(self) -> None:
        """Fecha a conexão da fila (idempotente)."""
        self._db.close()


# --------------------------------------------------------------------------------------------------
# Mensagens de entrega
# --------------------------------------------------------------------------------------------------

# Papéis e operações das mensagens de entrega.
DELIVERY_ROLES = ("question", "media", "field_prompt", "fallback", "notice")
DELIVERY_OPERATIONS = ("send", "edit_text", "edit_markup")
_MEDIA_ID = re.compile(r"m[1-9][0-9]{0,5}")


def _delivery_row(conn: psycopg.Connection, message_id: int) -> DeliveryRow:
    """A linha de entrega da fila (`deliveries.DeliveryRow`), como a posse a vê."""
    from cq_server.deliveries import DeliveryRow

    row = conn.execute(
        "SELECT m.id, m.delivery_id, m.delivery_role, m.operation, m.render_version, m.media_id, "
        "m.field_index, m.bot_id, m.delivery_endpoint_id, p.pseudo_id, m.attempts "
        "FROM outbox_messages m JOIN people p ON p.id = m.recipient_person_id "
        "WHERE m.id = %s AND m.delivery_id IS NOT NULL",
        (message_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"mensagem de entrega inexistente: {message_id}")
    return DeliveryRow(message_id=int(row[0]), delivery_row_id=int(row[1]), role=row[2], operation=row[3],
                       render_version=None if row[4] is None else int(row[4]), media_id=row[5],
                       field_index=None if row[6] is None else int(row[6]), bot_id=int(row[7]),
                       endpoint_row_id=int(row[8]), recipient=_as_uuid(row[9]), attempts=int(row[10]))


def _absorb_pending_edit(conn: psycopg.Connection, row: DeliveryRow) -> tuple[int, str] | None:
    """Trava a entrega (linha → entrega) e leva a `cancelled` a edição `pending` da mesma
    (entrega, papel), devolvendo a versão e a operação dela para a edição em voo absorver; `None` se não
    há pendente."""
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    conn.execute("SELECT 1 FROM deliveries WHERE id = %s FOR NO KEY UPDATE", (row.delivery_row_id,))
    pending = conn.execute(
        "SELECT id, render_version, operation FROM outbox_messages WHERE delivery_id = %s "
        "AND delivery_role = %s AND operation IN ('edit_text', 'edit_markup') AND state = 'pending' "
        "AND id <> %s FOR NO KEY UPDATE",
        (row.delivery_row_id, row.role, row.message_id),
    ).fetchone()
    if pending is None:
        return None
    conn.execute("UPDATE outbox_messages SET state = 'cancelled' WHERE id = %s", (pending[0],))
    return int(pending[1] or 0), str(pending[2])


def _moment(conn: psycopg.Connection, delivery_row_id: int, now: datetime | None) -> datetime:
    """O horário do enfileiramento: o `now` do chamador (o `Clock`) ou, sem ele, o horário mais
    recente já gravado na entrega (nunca o relógio do banco). `ValueError` se a entrega não existe."""
    row = conn.execute(
        "SELECT greatest(created_at, variant_at, sent_at, answered_at, revealed_at) FROM deliveries "
        "WHERE id = %s",
        (delivery_row_id,),
    ).fetchone()
    if row is None:
        raise ValueError("entrega inexistente")
    if now is not None:
        if now.tzinfo is None:
            raise ValueError("now deve ter fuso")
        return now
    return row[0]


def _delivery_target(conn: psycopg.Connection, *, delivery_row_id: int, recipient: object, bot_id: object,
                     endpoint_row_id: object) -> tuple[int, int, int, int]:
    """(entrega, pessoa, bot, endereço), conferidos antes de qualquer escrita: o endereço é da pessoa
    naquele bot (o vigente ou o da mensagem original); `ValueError` senão."""
    delivery = _as_id(delivery_row_id, "delivery_row_id")
    bot = _as_id(bot_id, "bot_id")
    endpoint = _as_id(endpoint_row_id, "endpoint_row_id")
    person = Outbox._person_id(conn, recipient)
    found = conn.execute(
        "SELECT 1 FROM delivery_endpoints WHERE id = %s AND person_id = %s AND bot_id = %s",
        (endpoint, person, bot),
    ).fetchone()
    if found is None:
        raise ValueError("endereço de entrega de outra pessoa ou de outro bot")
    return delivery, person, bot, endpoint


def enqueue_delivery(conn: psycopg.Connection, *, delivery_row_id: int, recipient: uuid.UUID, bot_id: int,
                     endpoint_row_id: int, role: str, media_id: str | None = None,
                     field_index: int | None = None, idempotency_key: str | None,
                     now: datetime | None = None) -> int:
    """Enfileira um envio (`send`) de uma entrega, sem texto, na transação de `conn`; devolve o id.

    O conteúdo é lido na posse (`DeliveryContent`). `media_id` só no papel `media` e `field_index` só
    no `field_prompt` (obrigatórios neles). Com a chave repetida, devolve a mensagem existente se ela é
    da mesma entrega, papel, mídia e campo (senão, `ValueError`). `now` (aditivo, só-nomeado): o horário
    do `Clock` do chamador (ver o docstring do módulo). `ValueError` sem escrita em entrada inválida."""
    if role not in DELIVERY_ROLES:
        raise ValueError("papel de mensagem de entrega desconhecido")
    if (media_id is not None) != (role == "media"):
        raise ValueError("media_id é obrigatório no papel media e proibido nos demais")
    if media_id is not None and (not isinstance(media_id, str) or _MEDIA_ID.fullmatch(media_id) is None):
        raise ValueError("media_id fora do formato mN")
    if (field_index is not None) != (role == "field_prompt"):
        raise ValueError("field_index é obrigatório no papel field_prompt e proibido nos demais")
    if field_index is not None and (_as_id(field_index, "field_index") < 0):
        raise ValueError("field_index negativo")
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or has_unstorable(idempotency_key)):
        raise ValueError("idempotency_key deve ser texto sem NUL nem substituto UTF-16 isolado")
    delivery, person, bot, endpoint = _delivery_target(conn, delivery_row_id=delivery_row_id,
                                                       recipient=recipient, bot_id=bot_id,
                                                       endpoint_row_id=endpoint_row_id)
    moment = _moment(conn, delivery, now)
    if idempotency_key is not None:
        existing = _existing_delivery(conn, idempotency_key, delivery, role, media_id, field_index)
        if existing is not None:
            return existing
    row = conn.execute(
        "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, text, idempotency_key, "
        "state, attempts, available_at, created_at, priority, delivery_id, delivery_role, operation, "
        "media_id, field_index) VALUES (%s, %s, %s, NULL, %s, 'pending', 0, %s, %s, 0, %s, %s, 'send', %s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        (person, bot, endpoint, idempotency_key, moment, moment, delivery, role, media_id, field_index),
    ).fetchone()
    if row is not None:
        return int(row[0])
    assert idempotency_key is not None
    existing = _existing_delivery(conn, idempotency_key, delivery, role, media_id, field_index)
    if existing is None:  # pragma: no cover - só com isolamento acima de READ COMMITTED
        raise ValueError("idempotency_key em uso por uma transação concorrente")
    return existing


def _existing_delivery(conn: psycopg.Connection, idempotency_key: str, delivery: int, role: str,
                       media_id: str | None, field_index: int | None) -> int | None:
    row = conn.execute(
        "SELECT id, delivery_id IS NOT DISTINCT FROM %s::bigint AND delivery_role IS NOT DISTINCT FROM %s "
        "AND operation IS NOT DISTINCT FROM 'send' AND media_id IS NOT DISTINCT FROM %s "
        "AND field_index IS NOT DISTINCT FROM %s::smallint FROM outbox_messages WHERE idempotency_key = %s",
        (delivery, role, media_id, field_index, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if not row[1]:
        raise ValueError("idempotency_key já usada com outro conteúdo")
    return int(row[0])


def enqueue_delivery_edit(conn: psycopg.Connection, *, delivery_row_id: int, recipient: uuid.UUID,
                          bot_id: int, endpoint_row_id: int, role: str = "question", markup_only: bool,
                          version: int, now: datetime | None = None) -> int:
    """Enfileira uma edição da mensagem `role` da entrega, fundindo com a edição `pending` da mesma
    (entrega, papel): `render_version = greatest(...)` e `edit_text` se alguma é de texto.

    Trava a entrega (`FOR NO KEY UPDATE`; o chamador já a travou, na ordem fixa) e só então a edição
    `pending`: a edição em voo (`leased`) nunca é alterada, e a nova vira outra linha, que espera a em
    voo pela ordem por entrega. Devolve o id da linha pendente (a fundida ou a nova). `now`
    (aditivo, só-nomeado): o horário do `Clock` do chamador (docstring do módulo)."""
    if role not in DELIVERY_ROLES:
        raise ValueError("papel de mensagem de entrega desconhecido")
    if not isinstance(markup_only, bool):
        raise ValueError("markup_only deve ser booleano")
    if _as_id(version, "version") < 0:
        raise ValueError("version negativa")
    delivery, person, bot, endpoint = _delivery_target(conn, delivery_row_id=delivery_row_id,
                                                       recipient=recipient, bot_id=bot_id,
                                                       endpoint_row_id=endpoint_row_id)
    moment = _moment(conn, delivery, now)
    # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
    conn.execute("SELECT 1 FROM deliveries WHERE id = %s FOR NO KEY UPDATE", (delivery,))
    merged = conn.execute(
        "UPDATE outbox_messages SET render_version = greatest(render_version, %(version)s), "
        "operation = CASE WHEN operation = 'edit_text' OR NOT %(markup)s THEN 'edit_text' "
        "ELSE 'edit_markup' END "
        "WHERE delivery_id = %(delivery)s AND delivery_role = %(role)s "
        "AND operation IN ('edit_text', 'edit_markup') AND state = 'pending' RETURNING id",
        {"version": version, "markup": markup_only, "delivery": delivery, "role": role},
    ).fetchone()
    if merged is not None:
        return int(merged[0])
    row = conn.execute(
        "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, text, state, attempts, "
        "available_at, created_at, priority, delivery_id, delivery_role, operation, render_version) "
        "VALUES (%s, %s, %s, NULL, 'pending', 0, %s, %s, 0, %s, %s, %s, %s) RETURNING id",
        (person, bot, endpoint, moment, moment, delivery, role, "edit_markup" if markup_only else "edit_text",
         version),
    ).fetchone()
    assert row is not None
    return int(row[0])


def cancel_delivery_messages(conn: psycopg.Connection, *, delivery_row_id: int, role: str | None = None,
                             operations: tuple[str, ...] | None = None,
                             keep_field_index: int | None = None) -> int:
    """Cancela as mensagens `pending` da entrega (sem filtros, todas; com `role`/`operations`, só
    aquelas; `keep_field_index` preserva o `field_prompt` daquele campo); devolve quantas.

    Nunca toca uma linha em voo (`leased`) nem uma já enviada; roda na transação do chamador, que já
    travou a entrega."""
    delivery = _as_id(delivery_row_id, "delivery_row_id")
    if role is not None and role not in DELIVERY_ROLES:
        raise ValueError("papel de mensagem de entrega desconhecido")
    ops: list[str] | None = None
    if operations is not None:
        ops = list(operations)
        if not ops or any(op not in DELIVERY_OPERATIONS for op in ops):
            raise ValueError("operação de mensagem de entrega desconhecida")
    keep = None if keep_field_index is None else _as_id(keep_field_index, "keep_field_index")
    cur = conn.execute(
        "UPDATE outbox_messages SET state = 'cancelled' WHERE delivery_id = %(delivery)s AND state = 'pending' "
        "AND (%(role)s::text IS NULL OR delivery_role = %(role)s::text) "
        "AND (%(ops)s::text[] IS NULL OR operation = ANY(%(ops)s::text[])) "
        "AND NOT (%(keep)s::smallint IS NOT NULL AND delivery_role = 'field_prompt' "
        "         AND field_index IS NOT DISTINCT FROM %(keep)s::smallint)",
        {"delivery": delivery, "role": role, "ops": ops, "keep": keep},
    )
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------------------------
# Mensagens de aviso à equipe
# ---------------------------------------------------------------------------------------------


# Os papéis das mensagens da prévia sem texto (a forma 3) e os dialetos da linha de texto.
NOTICE_CONTENT_ROLES = ("question", "media", "field_prompt")
TEXT_FORMATS = ("plain", "html", "rich")
NOTICE_PRIORITY = 2
# O tipo de aviso cuja linha de texto (o cabeçalho da prévia) aceita resposta.
_ANSWERABLE_NOTICE_KIND = "dispatch_preview"


def _text_shape(conn: psycopg.Connection, text_format: str | None, notice_id: int | None) -> tuple[str | None, bool]:
    """O dialeto gravado da linha de texto e se ela aceita resposta: só a linha de texto de um aviso
    `dispatch_preview` (o cabeçalho da prévia). Sem `text_format`, `(None, False)` (a mensagem comum de texto)."""
    if text_format is None:
        return None, False
    answerable = False
    if notice_id is not None:
        found = conn.execute("SELECT kind FROM team_notices WHERE id = %s", (notice_id,)).fetchone()
        answerable = found is not None and found[0] == _ANSWERABLE_NOTICE_KIND
    return str(text_format), answerable


def _notice_row(conn: psycopg.Connection, message_id: int) -> NoticeRow:
    """A linha da prévia sem texto (a forma 3), como a posse a vê, no molde de
    `_delivery_row`."""
    row = conn.execute(
        "SELECT m.id, m.notice_id, m.delivery_role, m.media_id, m.field_index, m.bot_id, m.delivery_endpoint_id, "
        "p.pseudo_id, m.attempts FROM outbox_messages m JOIN people p ON p.id = m.recipient_person_id "
        "WHERE m.id = %s AND m.notice_id IS NOT NULL AND m.text IS NULL",
        (message_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"mensagem da prévia inexistente: {message_id}")
    return NoticeRow(message_id=int(row[0]), notice_row_id=int(row[1]), role=row[2], media_id=row[3],
                     field_index=None if row[4] is None else int(row[4]), bot_id=int(row[5]),
                     endpoint_row_id=int(row[6]), recipient=_as_uuid(row[7]), attempts=int(row[8]))


def _notice_target(conn: psycopg.Connection, *, notice_row_id: object, recipient: object, bot_id: object,
                   endpoint_row_id: object, idempotency_key: object,
                   now: object) -> tuple[int, int, int, int, str, datetime]:
    """(aviso, pessoa, bot, endereço, chave, horário), conferidos antes de qualquer escrita: o aviso
    existe, o endereço é da pessoa naquele bot, a chave é texto armazenável e o `now` tem fuso;
    `ValueError` senão."""
    notice = _as_id(notice_row_id, "notice_row_id")
    bot = _as_id(bot_id, "bot_id")
    endpoint = _as_id(endpoint_row_id, "endpoint_row_id")
    if not isinstance(idempotency_key, str) or not idempotency_key or has_unstorable(idempotency_key):
        raise ValueError("idempotency_key deve ser texto não vazio sem NUL nem substituto UTF-16 isolado")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now deve ser um horário com fuso")
    if conn.execute("SELECT 1 FROM team_notices WHERE id = %s", (notice,)).fetchone() is None:
        raise ValueError("aviso inexistente")
    person = Outbox._person_id(conn, recipient)
    found = conn.execute(
        "SELECT 1 FROM delivery_endpoints WHERE id = %s AND person_id = %s AND bot_id = %s",
        (endpoint, person, bot),
    ).fetchone()
    if found is None:
        raise ValueError("endereço de entrega de outra pessoa ou de outro bot")
    return notice, person, bot, endpoint, idempotency_key, now


def _existing_notice(conn: psycopg.Connection, idempotency_key: str, *, notice: int, person: int, bot: int,
                     endpoint: int, text: str | None, text_format: str | None, role: str | None,
                     operation: str | None, media_id: str | None, field_index: int | None) -> int | None:
    """A mensagem com a chave, se o conteúdo é o mesmo; `ValueError` com outro conteúdo; `None` sem ela."""
    row = conn.execute(
        "SELECT id, notice_id IS NOT DISTINCT FROM %s::bigint "
        "AND recipient_person_id IS NOT DISTINCT FROM %s::bigint AND bot_id IS NOT DISTINCT FROM %s::bigint "
        "AND delivery_endpoint_id IS NOT DISTINCT FROM %s::bigint AND reply_to_update_id IS NULL "
        "AND delivery_id IS NULL AND text IS NOT DISTINCT FROM %s::text "
        "AND text_format IS NOT DISTINCT FROM %s::text AND delivery_role IS NOT DISTINCT FROM %s::text "
        "AND operation IS NOT DISTINCT FROM %s::text AND media_id IS NOT DISTINCT FROM %s::text "
        "AND field_index IS NOT DISTINCT FROM %s::smallint FROM outbox_messages WHERE idempotency_key = %s",
        (notice, person, bot, endpoint, text, text_format, role, operation, media_id, field_index,
         idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if not row[1]:
        raise ValueError("idempotency_key já usada com outro conteúdo")
    return int(row[0])


def _enqueue_notice(conn: psycopg.Connection, key: str, shape: dict[str, Any], now: datetime) -> int:
    """A chave repetida devolve a existente (ou `ValueError` com outro conteúdo); senão, o INSERT da
    mensagem de aviso (`priority = 2`, `pending`), com `ON CONFLICT` só para a corrida."""
    existing = _existing_notice(conn, key, **shape)
    if existing is not None:
        return existing
    row = conn.execute(
        "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, reply_to_update_id, text, "
        "text_format, idempotency_key, state, attempts, available_at, created_at, priority, notice_id, "
        "delivery_role, operation, media_id, field_index) "
        "VALUES (%(person)s, %(bot)s, %(endpoint)s, NULL, %(text)s, %(text_format)s, %(key)s, 'pending', 0, "
        "%(now)s, %(now)s, %(priority)s, %(notice)s, %(role)s, %(operation)s, %(media_id)s, %(field_index)s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING id",
        {**shape, "key": key, "now": now, "priority": NOTICE_PRIORITY},
    ).fetchone()
    if row is not None:
        return int(row[0])
    # Corrida: outra transação gravou a mesma chave entre a consulta e o INSERT.
    existing = _existing_notice(conn, key, **shape)
    if existing is None:  # pragma: no cover - só com isolamento acima de READ COMMITTED
        raise ValueError("idempotency_key em uso por uma transação concorrente")
    return existing


def enqueue_notice_text(conn: psycopg.Connection, *, notice_row_id: int, recipient: uuid.UUID, bot_id: int,
                        endpoint_row_id: int, text: str, text_format: str, idempotency_key: str,
                        now: datetime) -> int:
    """Enfileira a linha de texto de um aviso (a forma 1 com `notice_id` e `text_format`, `priority = 2`)
    na transação de `conn`; devolve o id (o da existente com a chave repetida e o mesmo conteúdo;
    `ValueError` sem escrita com outro conteúdo). `now` é o horário do `Clock` do chamador.

    `ValueError` sem escrita também se o texto não tiver de 1 a 4096 caracteres ou tiver NUL ou
    substituto UTF-16 isolado, se o dialeto não for `plain`, `html` ou `rich`, se o aviso não existir ou
    se o endereço não for da pessoa naquele bot."""
    if not isinstance(text, str) or not MIN_TEXT_LENGTH <= len(text) <= MAX_TEXT_LENGTH:
        raise ValueError(f"o texto deve ter de {MIN_TEXT_LENGTH} a {MAX_TEXT_LENGTH} caracteres")
    if has_unstorable(text):
        raise ValueError("o texto não pode conter o caractere NUL nem substituto UTF-16 isolado")
    if text_format not in TEXT_FORMATS:
        raise ValueError("text_format deve ser plain, html ou rich")
    notice, person, bot, endpoint, key, moment = _notice_target(
        conn, notice_row_id=notice_row_id, recipient=recipient, bot_id=bot_id, endpoint_row_id=endpoint_row_id,
        idempotency_key=idempotency_key, now=now)
    shape = {"notice": notice, "person": person, "bot": bot, "endpoint": endpoint, "text": text,
             "text_format": text_format, "role": None, "operation": None, "media_id": None, "field_index": None}
    return _enqueue_notice(conn, key, shape, moment)


def enqueue_notice_content(conn: psycopg.Connection, *, notice_row_id: int, recipient: uuid.UUID, bot_id: int,
                           endpoint_row_id: int, role: str, media_id: str | None = None,
                           field_index: int | None = None, idempotency_key: str, now: datetime) -> int:
    """Enfileira uma mensagem da prévia sem texto (a forma 3, `priority = 2`), lida na posse pelo
    `NoticeContent`; mesmas regras de `enqueue_notice_text`. `media_id` só no papel `media` e
    `field_index` só no `field_prompt` (obrigatórios neles), como em `enqueue_delivery`."""
    if role not in NOTICE_CONTENT_ROLES:
        raise ValueError("papel de mensagem da prévia desconhecido")
    if (media_id is not None) != (role == "media"):
        raise ValueError("media_id é obrigatório no papel media e proibido nos demais")
    if media_id is not None and (not isinstance(media_id, str) or _MEDIA_ID.fullmatch(media_id) is None):
        raise ValueError("media_id fora do formato mN")
    if (field_index is not None) != (role == "field_prompt"):
        raise ValueError("field_index é obrigatório no papel field_prompt e proibido nos demais")
    if field_index is not None and not 0 <= _as_id(field_index, "field_index") <= 32767:
        raise ValueError("field_index fora do intervalo")
    notice, person, bot, endpoint, key, moment = _notice_target(
        conn, notice_row_id=notice_row_id, recipient=recipient, bot_id=bot_id, endpoint_row_id=endpoint_row_id,
        idempotency_key=idempotency_key, now=now)
    shape = {"notice": notice, "person": person, "bot": bot, "endpoint": endpoint, "text": None,
             "text_format": None, "role": role, "operation": "send", "media_id": media_id,
             "field_index": field_index}
    return _enqueue_notice(conn, key, shape, moment)
