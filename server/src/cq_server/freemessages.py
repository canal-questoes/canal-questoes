"""Mensagens livres dos alunos: o texto que não foi aplicado a campo nenhum, gravado redigido, e a
resposta fixa a ele.

- `store_free_message`: grava o texto de um aluno vinculado que não foi aplicado a campo, **redigido**
  (com as identidades que o chamador leu por `events.load_write_identities`, na mesma transação), com
  `ambiguous` (mais de um campo digitável aberto), e devolve o texto fixo a enfileirar (`USE_REPLY` com
  `ambiguous`, `FREE_MESSAGE_REPLY` senão) ou `None` pelo limitador do tipo (um por endereço a cada
  `reply_interval_seconds`). O encaminhamento, em `answers`, enfileira a resposta com `priority = 1`.
- `list_free_messages`: a consulta `messages.list`, paginada por `id`, com o texto redigido de novo.

Sem evento. Com as mensagens livres ligadas, o texto literal continua só em
`telegram_updates.text` (resíduo declarado). Nenhum SQL cita `identities`: a redação usa as
identidades recebidas.

Modo desligado: com as mensagens livres
desligadas (o padrão), nada é gravado aqui; a decisão do modo é de `answers._free_message`, e as
assinaturas de `store_free_message` e `list_free_messages` não dependem do modo.
- `FREE_MESSAGE_MASK`: a marca fixa que a recepção grava no `text` de um update cujo desfecho está em
  `answers.MASKED_OUTCOMES`, no mesmo `UPDATE` que o marca `processed` (nunca antes, nem num commit
  próprio, nem na máscara de reserva ou no abandono).
- `masked_reply_due`: o limitador do modo desligado, sobre os updates (um conjunto de desfechos por
  limitador).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from cq_server import deliverytexts
from cq_server._leasing import utc
from cq_server.offerings import find_offering_row
from cq_server.redaction import KnownIdentity, redact


# A marca do texto de um update classificado como livre no modo desligado.
FREE_MESSAGE_MASK = "[mensagem livre]"


@dataclass(frozen=True)
class FreeMessage:
    """Um item de `messages.list`: o texto já redigido."""

    pseudo_id: uuid.UUID
    alias: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class FreeMessagesPage:
    """Uma página de `messages.list` (paginação por `id`)."""

    offering_id: uuid.UUID
    code: str
    items: tuple[FreeMessage, ...]
    next_cursor: int | None


def store_free_message(conn: psycopg.Connection, *, offering_row_id: int, endpoint_row_id: int,
                       update_row_id: int, text: str, ambiguous: bool, received_at: datetime, now: datetime,
                       identities: Sequence[KnownIdentity], reply_interval_seconds: int) -> str | None:
    """Grava a mensagem livre (texto redigido) na transação de `conn` e devolve a resposta fixa a
    enfileirar, ou `None` (limitador por tipo).

    O limitador: a resposta do tipo (`ambiguous` ou não) só sai se não houver mensagem livre do mesmo
    endereço, do mesmo tipo, já respondida (`replied`) com `created_at > now − intervalo`. O chamador
    trava o endereço antes (`team.chat_endpoint`), o que serializa duas mensagens do mesmo aluno. A
    gravação é idempotente pelo update (`telegram_update_id` único): uma segunda gravação do mesmo
    update não grava nem responde."""
    stored = redact(text, identities)
    since = now - timedelta(seconds=int(reply_interval_seconds))
    recent = conn.execute(
        "SELECT 1 FROM free_messages WHERE delivery_endpoint_id = %s AND ambiguous = %s AND replied "
        "AND created_at > %s LIMIT 1",
        (endpoint_row_id, bool(ambiguous), since),
    ).fetchone()
    reply = recent is None
    inserted = conn.execute(
        "INSERT INTO free_messages (offering_id, delivery_endpoint_id, telegram_update_id, text, replied, "
        "ambiguous, received_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (telegram_update_id) DO NOTHING RETURNING id",
        (offering_row_id, endpoint_row_id, update_row_id, stored, reply, bool(ambiguous), received_at, now),
    ).fetchone()
    if inserted is None or not reply:
        return None
    return deliverytexts.USE_REPLY if ambiguous else deliverytexts.FREE_MESSAGE_REPLY


def masked_reply_due(conn: psycopg.Connection, *, bot_id: int, chat_id: int | None, outcomes: Sequence[str],
                     now: datetime, reply_interval_seconds: int) -> bool:
    """O limitador do modo desligado: a resposta sai só se nenhum update
    do mesmo bot e do mesmo `chat_id` com `processed_at > now − intervalo` tem um desfecho de
    `outcomes` (o conjunto do limitador). O chamador já trava o endereço (`team.chat_endpoint`), o que
    serializa duas mensagens do mesmo aluno; o update corrente ainda não tem desfecho nesta leitura.
    Contar numa janela não é ordenar por horário."""
    if chat_id is None:
        return True
    since = now - timedelta(seconds=int(reply_interval_seconds))
    recent = conn.execute(
        "SELECT 1 FROM telegram_updates WHERE bot_id = %s AND chat_id = %s AND outcome = ANY(%s) "
        "AND processed_at > %s LIMIT 1",
        (bot_id, chat_id, list(outcomes), since),
    ).fetchone()
    return recent is None


def list_free_messages(conn: psycopg.Connection, ref: str, *, after: int | None, limit: int,
                       identities: Sequence[KnownIdentity],
                       secrets: Sequence[str]) -> FreeMessagesPage | None:
    """As mensagens livres do oferecimento `ref` (código ou `offering_id`, em qualquer estado) com `id`
    maior que `after`, em ordem de `id`, no máximo `limit`; `None` se o oferecimento não existe.

    O texto sai redigido de novo com as identidades do momento e os segredos; o autor é a pessoa
    do endereço (pseudônimo e apelido). `next_cursor` é o `id` do último item, ou `after` (0 sem ele)
    numa página vazia."""
    offering = find_offering_row(conn, ref)
    if offering is None:
        return None
    start = 0 if after is None else int(after)
    rows = conn.execute(
        "SELECT f.id, p.pseudo_id, p.alias, f.text, f.created_at FROM free_messages f "
        "JOIN delivery_endpoints e ON e.id = f.delivery_endpoint_id JOIN people p ON p.id = e.person_id "
        "WHERE f.offering_id = %s AND f.id > %s ORDER BY f.id LIMIT %s",
        (offering.id, start, int(limit)),
    ).fetchall()
    secret_list = list(secrets)
    items = tuple(FreeMessage(pseudo_id=row[1], alias=row[2],
                              text=redact(row[3], identities, secrets=secret_list),
                              created_at=utc(row[4]))
                  for row in rows)
    next_cursor = int(rows[-1][0]) if rows else start
    return FreeMessagesPage(offering_id=offering.offering_id, code=offering.code, items=items,
                            next_cursor=next_cursor)
