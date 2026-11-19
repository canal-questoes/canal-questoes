"""Recepção dos updates do Telegram: gravação e tratador `telegram.update` (o update é gravado antes
de processado, deduplicado e processado uma vez, pelos fluxos da equipe e do aluno).

Núcleo sem HTTP. Grava os `MinimalUpdate` da porta (já minimizados pelo adaptador, na borda) antes
de o `offset` avançar, com deduplicação por (bot, época, número), e processa cada update pela tarefa
idempotente `telegram.update`. As interfaces deste módulo são detalhe de implementação, fora do
contrato público: os testes passam pelo worker e pela Bot API falsa.

**Consulta** (as funções que a thread de consulta do worker chama, cada uma numa transação curta):
- `poll_token`: lê e decifra o token da versão em uso; um token
  ilegível abre o episódio de perda de acesso (`bots.mark_no_access`, `token_unreadable`) sem chamar
  o Telegram;
- `poll_status`: a conferência antes de cada `getUpdates` (`bots.poll_allowed`), com o motivo quando
  não pode consultar: `wait` (falta a folga; espera a renovação), `lost`, `token_changed` ou
  `not_eligible`;
- `store_updates`: toda resposta `ok` (vazia ou não), numa transação: confere a posse (basta ser a
  dona), grava `last_poll_ok_at`, limpa o último problema e, com updates, a época (48 h sem update
  novo → `update_epoch + 1`), as linhas novas (`ON CONFLICT DO NOTHING`) e uma tarefa
  `telegram.update` por linha nova (chave `telegram.update:<id>`) — tudo antes de o `offset`
  avançar, que só acontece depois do commit, na thread;
- `record_poll_problem`: o último problema transitório visível em `bots.get`.

**Processamento** (`update_task_handler`, o tratador da tarefa `telegram.update`): trava a linha do
update (`FOR NO KEY UPDATE`, a exceção da ordem geral de travas); se já está `processed`, nada
faz; senão aplica o efeito do update (`_apply`) e grava `outcome`, `processed_at` e `state`, na mesma
transação do efeito (vínculo, revogação, respostas pela fila de saída). Registra `telegram.update`
com `update_row_id` e `outcome`, sem texto, chat nem remetente.

Alunos: o `/start <código>` de convite de aluno, o `/aceito`, o `/recuso` e o
`/convite` são despachados para `cq_server.students` (que nunca importa este módulo); a máscara do
`/convite` sai no mesmo UPDATE que marca `processed` e, numa falha de vez, numa transação de reserva.

Todo horário vem do `Clock` do chamador; nenhuma ordenação depende dele.

Entrega das questões:
`store_updates(…, reply_to=None)` (o `reply_to_message_id` de cada update, pelo `update_id`),
`store_console_updates` (a entrada simulada do console, com `update_id` negativos) e
`update_task_handler(…, channel=None, delivery=None)` (o canal com rota e a configuração de entrega das
respostas do aluno; `None` = sem elas). O `_apply` despacha para `answers` sem mudar a ordem dos
outros fluxos. As duas gravações (`store_updates` e `store_console_updates`) escrevem a mesma linha literal,
com o `reply_to_message_id`, e a tarefa dela na mesma transação.

Mensagem livre no modo desligado: o update cujo desfecho está em
`answers.MASKED_OUTCOMES` (mensagem livre no modo desligado) tem o `text` trocado por
`freemessages.FREE_MESSAGE_MASK` e o `command` por NULL no mesmo UPDATE que o marca `processed`.
`mask_unprocessed` e o abandono pela varredura não mascaram (nenhum processamento confirmado o
classificou como livre).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from cq_server import answers, freemessages, students, team
from cq_server._leasing import LazyConnection, utc
from cq_server.bots import BotRuntime, answer_callback, mark_no_access, offering_text, poll_allowed
from cq_server.clock import Clock
from cq_server.config import BotSettings, DeliverySettings, Settings
from cq_server.offerings import POLLED_STATES
from cq_server.outbox import Outbox
from cq_server.ports.delivery import DeliveryChannel
from cq_server.ports.telegram import MinimalUpdate
from cq_server.secretbox import TokenCipher, UnreadableToken
from cq_server.tasks import ClaimedTask, PermanentTaskError, enqueue_in_transaction

logger = logging.getLogger("cq_server.worker")

UPDATE_TASK_TYPE = "telegram.update"

# Mais de 48 h sem update novo abre uma época de deduplicação nova.
EPOCH_IDLE = timedelta(hours=48)

# Os problemas transitórios da consulta (`bots.last_problem`).
POLL_PROBLEMS = ("telegram_unavailable", "rate_limited", "concurrent_consumer", "webhook_active")

# Os `outcome` fora do vínculo (os do vínculo vêm de `team`).
NOT_HANDLED = "not_handled"
HELP = "help"

START_COMMAND = "/start"
HELP_COMMAND = "/ajuda"

# `poll_status`: o que a thread de consulta faz antes de cada getUpdates.
POLL_OK = "ok"
POLL_WAIT = "wait"
POLL_LOST = "lost"
POLL_TOKEN_CHANGED = "token_changed"
POLL_NOT_ELIGIBLE = "not_eligible"

_ELIGIBLE_SQL = ("b.state = 'registered' AND o.state IN ("
                 + ", ".join(f"'{state}'" for state in POLLED_STATES) + ")")


# --------------------------------------------------------------------------------------------------
# Consulta: credencial, conferência, gravação e problema
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PollStop:
    """A consulta não começa: `reason` é `token_changed`, `not_eligible` ou `no_access` (token
    ilegível; `opened` diz se esta leitura abriu o episódio de perda de acesso)."""

    reason: str
    opened: bool = False
    unreadable: str | None = None  # motivo técnico do token ilegível, só para o log


def poll_token(conn: psycopg.Connection, *, bot_id: int, token_version: int, cipher: TokenCipher,
               now: datetime) -> str | PollStop:
    """O token do bot na versão `token_version`, decifrado; ou por que
    não há consulta. Um token ilegível (`malformed`, `key_mismatch`, `tampered`) abre a perda de acesso
    (`mark_no_access(…, problem="token_unreadable")`), sem chamar o Telegram."""
    row = conn.execute(
        "SELECT state, token_version, token_ciphertext, bot_uuid, telegram_bot_id FROM bots WHERE id = %s",
        (bot_id,),
    ).fetchone()
    if row is None:
        return PollStop(POLL_NOT_ELIGIBLE)
    state, version, blob, bot_uuid, telegram_bot_id = row
    if int(version) != token_version:
        return PollStop(POLL_TOKEN_CHANGED)
    if state != "registered":
        return PollStop(POLL_NOT_ELIGIBLE)
    try:
        return cipher.decrypt(bytes(blob), bot_uuid=bot_uuid, telegram_bot_id=int(telegram_bot_id))
    except UnreadableToken as exc:
        opened = mark_no_access(conn, bot_id=bot_id, token_version=token_version,
                                problem="token_unreadable", now=now)
        return PollStop("no_access", opened=opened, unreadable=exc.reason)


def poll_status(conn: psycopg.Connection, *, bot_id: int, worker_id: str, token_version: int,
                now: datetime, settings: BotSettings) -> str:
    """A conferência antes de cada `getUpdates`: `ok` se `bots.poll_allowed`;
    senão `lost` (não é mais a dona), `token_changed`, `not_eligible` ou `wait` (é a dona, mas sem a
    folga `poll_timeout_seconds + telegram_timeout_seconds`: espera a renovação)."""
    if poll_allowed(conn, bot_id=bot_id, worker_id=worker_id, token_version=token_version, now=now,
                    settings=settings):
        return POLL_OK
    row = conn.execute(
        "SELECT b.lease_owner, b.token_version, b.state, o.state FROM bots b "
        "JOIN offerings o ON o.id = b.offering_id WHERE b.id = %s",
        (bot_id,),
    ).fetchone()
    if row is None:
        return POLL_LOST
    owner, version, bot_state, offering_state = row
    if owner != worker_id:
        return POLL_LOST
    if int(version) != token_version:
        return POLL_TOKEN_CHANGED
    if bot_state != "registered" or offering_state not in POLLED_STATES:
        return POLL_NOT_ELIGIBLE
    return POLL_WAIT


def store_updates(conn: psycopg.Connection, *, bot_id: int, worker_id: str,
                  updates: Sequence[MinimalUpdate], now: datetime,
                  reply_to: Mapping[int, int] | None = None) -> list[int] | None:
    """Uma resposta `ok` do `getUpdates`, numa transação: devolve os ids
    das linhas novas (em ordem de gravação), ou `None` se este worker não é mais o dono (nada
    gravado; o lote é descartado e o `offset` não avança).

    `reply_to` (`update_id` → `reply_to_message_id`): a mensagem citada de cada update,
    gravada na linha dele (sem entrada, nula)."""
    with conn.transaction():
        row = conn.execute(
            "SELECT lease_owner, update_epoch, last_update_at FROM bots WHERE id = %s FOR NO KEY UPDATE",
            (bot_id,),
        ).fetchone()
        if row is None or row[0] != worker_id:
            return None
        epoch, last_update_at = int(row[1]), row[2]
        if updates and last_update_at is not None and now - last_update_at > EPOCH_IDLE:
            epoch += 1
        conn.execute(
            "UPDATE bots SET last_poll_ok_at = %s, last_problem = NULL, last_problem_at = NULL, "
            "update_epoch = %s WHERE id = %s",
            (now, epoch, bot_id),
        )
        new_rows: list[int] = []
        for update in updates:
            row_id = _insert_update(conn, bot_id=bot_id, epoch=epoch, update_id=update.update_id,
                                    update=update, reply_to=_quoted(reply_to, update), now=now)
            if row_id is not None:
                new_rows.append(row_id)
        _enqueue_updates(conn, new_rows, now=now)
        if new_rows:
            conn.execute("UPDATE bots SET last_update_at = %s WHERE id = %s", (now, bot_id))
    return new_rows


def store_console_updates(conn: psycopg.Connection, *, bot_id: int, updates: Sequence[MinimalUpdate],
                          reply_to: Mapping[int, int] | None, now: datetime) -> list[int]:
    """A entrada simulada do aluno: grava as entradas do console como o `store_updates`
    (linha literal e tarefa `telegram.update` na mesma transação), sem a posse do leitor, com
    `update_id` negativos (−1, −2, …) calculados sob a trava do bot; devolve os ids das linhas.

    O `update_id` de cada entrada de `updates` só serve de chave em `reply_to` (a mensagem citada); o
    gravado é o próximo negativo do bot, em qualquer época (nunca colide com os do Telegram, que são
    positivos). A época é a corrente do bot, e o `last_update_at` não muda (o console não é o leitor)."""
    with conn.transaction():
        row = conn.execute("SELECT update_epoch FROM bots WHERE id = %s FOR NO KEY UPDATE",
                           (bot_id,)).fetchone()
        if row is None:
            raise LookupError("bot inexistente")
        epoch = int(row[0])
        lowest = conn.execute(
            "SELECT COALESCE(MIN(update_id), 0) FROM telegram_updates WHERE bot_id = %s AND update_id < 0",
            (bot_id,),
        ).fetchone()[0]
        new_rows: list[int] = []
        for offset, update in enumerate(updates, start=1):
            row_id = _insert_update(conn, bot_id=bot_id, epoch=epoch, update_id=int(lowest) - offset,
                                    update=update, reply_to=_quoted(reply_to, update), now=now)
            if row_id is None:  # pragma: no cover - a trava do bot garante o número novo
                raise RuntimeError("update_id do console repetido")
            new_rows.append(row_id)
        _enqueue_updates(conn, new_rows, now=now)
    return new_rows


def _quoted(reply_to: Mapping[int, int] | None, update: MinimalUpdate) -> int | None:
    """A mensagem citada pelo update (`reply_to_message_id`), se houver."""
    if not reply_to:
        return None
    value = reply_to.get(update.update_id)
    return int(value) if value is not None else None


def _insert_update(conn: psycopg.Connection, *, bot_id: int, epoch: int, update_id: int,
                   update: MinimalUpdate, reply_to: int | None, now: datetime) -> int | None:
    """A linha literal de `telegram_updates` (`pending`); `None` se o update já estava gravado."""
    inserted = conn.execute(
        "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, "
        "sender_id, text, command, details, reply_to_message_id, received_at, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending') "
        "ON CONFLICT (bot_id, update_epoch, update_id) DO NOTHING RETURNING id",
        (bot_id, epoch, update_id, update.kind, update.chat_id, update.chat_type, update.sender_id,
         update.text, update.command, Jsonb(dict(update.details)), reply_to, now),
    ).fetchone()
    return None if inserted is None else int(inserted[0])


def _enqueue_updates(conn: psycopg.Connection, row_ids: Sequence[int], *, now: datetime) -> None:
    """Uma tarefa `telegram.update` por linha nova, na transação da gravação."""
    for row_id in row_ids:
        enqueue_in_transaction(conn, UPDATE_TASK_TYPE, {"update_row_id": row_id}, now=now,
                               idempotency_key=f"{UPDATE_TASK_TYPE}:{row_id}")


def record_poll_problem(conn: psycopg.Connection, *, bot_id: int, worker_id: str, problem: str,
                        now: datetime) -> bool:
    """Grava o último problema transitório da consulta (`last_problem`, `last_problem_at`), se este
    worker ainda é o dono; `True` se gravou. Limpo pela próxima resposta `ok` (`store_updates`)."""
    if problem not in POLL_PROBLEMS:
        raise ValueError("problema de consulta desconhecido")
    with conn.transaction():
        row = conn.execute(
            "UPDATE bots SET last_problem = %s, last_problem_at = %s WHERE id = %s AND lease_owner = %s "
            "RETURNING id",
            (problem, now, bot_id, worker_id),
        ).fetchone()
    return row is not None


# --------------------------------------------------------------------------------------------------
# Posses do worker (para a reconciliação)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnLease:
    """Uma posse deste worker no banco, com a versão do token e se o bot ainda é elegível."""

    bot_id: int
    token_version: int
    eligible: bool
    lease_until: datetime | None
    offering_id: uuid.UUID


def own_leases(conn: psycopg.Connection, *, worker_id: str) -> dict[int, OwnLease]:
    """As posses de `worker_id` no banco, por id do bot (elegíveis ou não)."""
    rows = conn.execute(
        f"SELECT b.id, b.token_version, ({_ELIGIBLE_SQL}), b.lease_until, o.offering_id FROM bots b "
        "JOIN offerings o ON o.id = b.offering_id WHERE b.lease_owner = %s",
        (worker_id,),
    ).fetchall()
    return {int(row[0]): OwnLease(bot_id=int(row[0]), token_version=int(row[1]), eligible=bool(row[2]),
                                  lease_until=row[3], offering_id=row[4]) for row in rows}


def offering_ids(conn: psycopg.Connection, bot_ids: Collection[int]) -> dict[int, uuid.UUID]:
    """O `offering_id` (UUID) do oferecimento de cada bot (para os registros de log)."""
    ids = [int(bot_id) for bot_id in bot_ids]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT b.id, o.offering_id FROM bots b JOIN offerings o ON o.id = b.offering_id "
        "WHERE b.id = ANY(%s::bigint[])",
        (ids,),
    ).fetchall()
    return {int(bot_id): offering_id for bot_id, offering_id in rows}


# --------------------------------------------------------------------------------------------------
# Processamento
# --------------------------------------------------------------------------------------------------


def _command_target(command: str | None, username: str) -> tuple[str | None, bool]:
    """(nome do comando, é para este bot): `/start@<username>` compara o username sem diferenciar
    maiúsculas; sem `@`, o comando é deste bot."""
    if command is None:
        return None, False
    name, at, target = command.partition("@")
    if at and target.lower() != username.lower():
        return name, False
    return name, True


def _start_code(text: str, command: str) -> str:
    """O código do `/start`: o texto depois do primeiro espaço que segue o comando, sem os espaços
    das pontas; vazio se não houver."""
    rest = text[len(command):] if text.startswith(command) else ""
    space = rest.find(" ")
    return rest[space + 1:].strip() if space >= 0 else ""


@dataclass(frozen=True)
class _Update:
    id: int
    bot_id: int
    kind: str
    chat_id: int | None
    chat_type: str | None
    text: str | None
    command: str | None
    username: str
    sender_id: int | None = None
    details: Mapping | None = None
    reply_to_message_id: int | None = None
    received_at: datetime | None = None

    def inbound(self) -> answers.Inbound:
        """O update como `answers` o lê: o toque traz `callback_id`, `data` e o
        `message_id` da mensagem do botão; a mensagem, o `message_id` dela e a citada."""
        details = self.details if isinstance(self.details, Mapping) else {}
        tap = self.kind == "callback_query"
        message_id = details.get("message_id")
        return answers.Inbound(
            update_row_id=self.id, bot_id=self.bot_id, kind=self.kind, chat_id=self.chat_id,
            sender_id=self.sender_id, text=self.text,
            callback_id=details.get("callback_id") if tap else None,
            data=details.get("data") if tap else None,
            message_id=message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
            reply_to_message_id=self.reply_to_message_id, received_at=self.received_at)


@dataclass(frozen=True)
class _Answering:
    """O que as respostas do aluno precisam no processamento: o canal com rota e a
    configuração de entrega. Sem ele (`None`), o `_apply` só tem os fluxos da equipe e dos alunos."""

    channel: DeliveryChannel
    delivery: DeliverySettings


@dataclass(frozen=True)
class _Confirmation:
    """A confirmação de um toque, mandada depois do commit."""

    bot_id: int
    callback_id: str
    text: str | None = field(repr=False)


@dataclass(frozen=True)
class _Processed:
    """O resultado de um processamento: o desfecho gravado e a confirmação do toque, se houver."""

    outcome: str
    confirmation: _Confirmation | None


def _apply(conn: psycopg.Connection, update: _Update, *, now: datetime, outbox: Outbox,
           answering: _Answering | None = None) -> str:
    """O efeito do update pelos fluxos da equipe e pelos do aluno (o `/start`, o `/aceito`, o
    `/recuso` e o `/convite`), na transação do chamador; devolve o `outcome`.

    Fora do chat privado, ou com o `/start` dirigido a outro bot, a revogação por exposição dos códigos
    (também dos convites de aluno); `/aceito`, `/recuso` e `/convite` (sem diferenciar maiúsculas) só
    valem no chat privado e dirigidos a este bot, e fora disso ficam `not_handled`, como qualquer
    comando de outro bot. O `/start <código>` passa primeiro por `students.start_student`, que só
    assume o código de um convite de aluno do oferecimento deste bot; nos demais casos, segue o fluxo
    da equipe. `/aceito` e `/recuso` sem termo apresentado no chat respondem `HELP`, com o `help` da equipe.

    Respostas (com `answering`): no chat privado, o texto sem comando, a mensagem sem texto e
    o comando desconhecido dirigido a este bot (inclusive o `/naosei`) vão a `answers.process_message`,
    depois dos comandos conhecidos; se o remetente não é aluno vinculado, fica o `not_handled`."""
    if update.kind != "message":
        return NOT_HANDLED
    name, ours = _command_target(update.command, update.username)
    if update.chat_type != "private" or update.chat_id is None:
        # Primeiro caso: um código de convite pendente num chat que não é tratado é revogado,
        # para que nenhum código utilizável fique no texto gravado.
        team.revoke_exposed_codes(conn, bot_id=update.bot_id, text=update.text, now=now)
        return NOT_HANDLED
    if name == START_COMMAND and not ours:
        team.revoke_exposed_codes(conn, bot_id=update.bot_id, text=update.text, now=now)
        return NOT_HANDLED
    if name is None and answering is not None:
        return _answer_message(conn, update, now=now, outbox=outbox, answering=answering)
    if not ours:
        return NOT_HANDLED
    student = students.student_command(name)
    target = {"bot_id": update.bot_id, "update_row_id": update.id, "chat_id": update.chat_id, "now": now,
              "outbox": outbox}
    if student == students.ACCEPT_COMMAND:
        outcome = students.accept_terms(conn, **target)
        if outcome is not None:
            return outcome
    elif student == students.REFUSE_COMMAND:
        outcome = students.refuse_terms(conn, **target)
        if outcome is not None:
            return outcome
    elif student == students.INVITE_REQUEST_COMMAND:
        argument = students.invite_request_argument(update.text, update.command)
        return students.request_invite(conn, argument=argument or "", **target)
    elif name == START_COMMAND:
        assert update.command is not None and update.text is not None
        code = _start_code(update.text, update.command)
        if code:
            outcome = students.start_student(conn, code=code, **target)
            if outcome is not None:
                return outcome
            return team.process_start(conn, bot_id=update.bot_id, update_row_id=update.id,
                                      sender_chat_id=update.chat_id, code=code, now=now, outbox=outbox)
    elif name != HELP_COMMAND:
        if answering is not None:
            return _answer_message(conn, update, now=now, outbox=outbox, answering=answering)
        return NOT_HANDLED
    text = offering_text(conn, "HELP", bot_id=update.bot_id)
    outbox.enqueue(conn, text=text, bot_id=update.bot_id, reply_to_update_id=update.id)
    return HELP


def _answer_message(conn: psycopg.Connection, update: _Update, *, now: datetime, outbox: Outbox,
                    answering: _Answering) -> str:
    """O texto (ou a mensagem sem texto) do chat privado, por `answers.process_message`; `None`
    (não é aluno vinculado) fica `not_handled`, como nos demais fluxos.

    Prévia: antes, a resposta por "responder" a uma mensagem da prévia
    (`previews.process_reply`) recebe `PREVIEW_REPLY` pela fila e o desfecho `preview_replied`."""
    from cq_server import previews  # só aqui (o domínio da prévia não é carregado com a recepção)

    if previews.process_reply(conn, update.inbound(), now=now, outbox=outbox):
        return answers.PREVIEW_REPLIED
    outcome = answers.process_message(conn, update.inbound(), now=now, outbox=outbox, channel=answering.channel,
                                      delivery=answering.delivery)
    return NOT_HANDLED if outcome is None else outcome


def _apply_tap(conn: psycopg.Connection, update: _Update, *, now: datetime, outbox: Outbox,
               answering: _Answering | None) -> tuple[str, _Confirmation | None]:
    """O `callback_query`: só o dado `cq1:` é das respostas (`answers.process_tap`, sempre
    confirmado depois do commit); fora dele, ou sem as respostas do aluno, o `not_handled`, sem
    confirmação."""
    inbound = update.inbound()
    if answering is None or answers.parse_callback_data(inbound.data or "") is None:
        return NOT_HANDLED, None
    result = answers.process_tap(conn, inbound, now=now, outbox=outbox, channel=answering.channel)
    confirmation = None
    if inbound.callback_id is not None:
        confirmation = _Confirmation(bot_id=update.bot_id, callback_id=inbound.callback_id,
                                     text=result.answer_text)
    return result.outcome, confirmation


def _archived_tap_confirmation(update: _Update) -> _Confirmation | None:
    """O toque `cq1:` no oferecimento já
    `archived` não é processado, mas é confirmado depois do commit, sem texto (como o toque
    ignorado), para o cliente do aluno não ficar carregando. A confirmação não é efeito proibido."""
    if update.kind != "callback_query":
        return None
    inbound = update.inbound()
    if inbound.callback_id is None or not (inbound.data or "").startswith("cq1:"):
        return None
    return _Confirmation(bot_id=update.bot_id, callback_id=inbound.callback_id, text=None)


def _process(conn: psycopg.Connection, *, update_row_id: int, now: datetime, outbox: Outbox,
             answering: _Answering | None = None) -> _Processed | None:
    """O processamento de `process_update`, com a confirmação do toque. `None` se o update já
    estava `processed` ou se a entrada espera a transição do fechamento (`answers.AwaitTransition`:
    a transação volta atrás, nada é gravado e o update continua `pending`).

    Arquivamento: com as respostas do aluno e o oferecimento do bot já `archived`,
    com disparo `open` ou `closed` (sem disparo, seguem os outros fluxos), uma entrada do console ou
    um update gravado na corrida com o arquivamento termina `processed`/`not_handled` sem efeito: nenhuma
    submissão, estado, evento ou mensagem nova; a linha gravada fica (nada se perde), com a máscara do `/convite`."""
    try:
        with conn.transaction():
            # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
            row = conn.execute(
                "SELECT u.id, u.bot_id, u.kind, u.chat_id, u.chat_type, u.text, u.command, u.state, b.username, "
                "u.sender_id, u.details, u.reply_to_message_id, u.received_at, o.state = 'archived' AND EXISTS "
                "(SELECT 1 FROM dispatches d WHERE d.offering_id = o.id AND d.state IN ('open', 'closed')) "
                "FROM telegram_updates u JOIN bots b ON b.id = u.bot_id JOIN offerings o ON o.id = b.offering_id "
                "WHERE u.id = %s FOR NO KEY UPDATE OF u",
                (update_row_id,),
            ).fetchone()
            if row is None:
                raise LookupError("update inexistente")
            if row[7] == "processed":
                return None
            update = _Update(id=int(row[0]), bot_id=int(row[1]), kind=row[2],
                             chat_id=None if row[3] is None else int(row[3]), chat_type=row[4], text=row[5],
                             command=row[6], username=row[8],
                             sender_id=None if row[9] is None else int(row[9]), details=row[10],
                             reply_to_message_id=None if row[11] is None else int(row[11]),
                             received_at=utc(row[12]))
            confirmation = None
            if answering is not None and row[13]:
                outcome = NOT_HANDLED
                confirmation = _archived_tap_confirmation(update)
            elif update.kind == "callback_query":
                outcome, confirmation = _apply_tap(conn, update, now=now, outbox=outbox, answering=answering)
            else:
                outcome = _apply(conn, update, now=now, outbox=outbox, answering=answering)
            masked = students.invite_request_mask(update.text, update.command)
            if masked is None and outcome in answers.MASKED_OUTCOMES:
                # Mensagem livre: o texto classificado como livre no modo desligado
                # vira a marca, e o `command` (o trecho digitado do comando desconhecido) vira NULL, no mesmo
                # UPDATE que marca `processed`: só o processamento confirmado mascara.
                conn.execute(
                    "UPDATE telegram_updates SET state = 'processed', outcome = %s, processed_at = %s, text = %s, "
                    "command = NULL WHERE id = %s",
                    (outcome, now, freemessages.FREE_MESSAGE_MASK, update.id),
                )
            elif masked is None:
                conn.execute(
                    "UPDATE telegram_updates SET state = 'processed', outcome = %s, processed_at = %s WHERE id = %s",
                    (outcome, now, update.id),
                )
            else:
                conn.execute(
                    "UPDATE telegram_updates SET state = 'processed', outcome = %s, processed_at = %s, text = %s "
                    "WHERE id = %s",
                    (outcome, now, masked, update.id),
                )
    except answers.AwaitTransition:
        return None
    return _Processed(outcome=outcome, confirmation=confirmation)


def process_update(conn: psycopg.Connection, *, update_row_id: int, now: datetime,
                   outbox: Outbox) -> str | None:
    """Processa o update `update_row_id` numa transação: trava a linha; se já está
    `processed`, nada faz e devolve `None`; senão aplica o efeito, grava `outcome`, `processed_at` e
    `state = processed`, e devolve o `outcome`. `LookupError` se o update não existe.

    Alunos: no mesmo UPDATE que marca `processed`, o texto de um `/convite`
    com argumento vira a marca `<prefixo> [e-mail]` (`students.invite_request_mask`), em qualquer chat
    e para qualquer bot de destino, também sem a entidade `bot_command`.

    Esta função não trata as respostas do aluno; o tratador `telegram.update` usa `_process`, com elas."""
    processed = _process(conn, update_row_id=update_row_id, now=now, outbox=outbox)
    return None if processed is None else processed.outcome


def mask_unprocessed(conn: psycopg.Connection, *, update_row_id: int) -> None:
    """A transação de reserva da máscara do `/convite`: numa falha de
    vez do processamento (última tentativa ou falha definitiva), o texto de um update ainda não
    `processed` vira a marca, sem marcar `processed` (nem `outcome`, nem `processed_at`)."""
    with conn.transaction():
        # FOR NO KEY UPDATE: não barra o FOR KEY SHARE das FKs.
        row = conn.execute(
            "SELECT text, command, state FROM telegram_updates WHERE id = %s FOR NO KEY UPDATE",
            (update_row_id,),
        ).fetchone()
        if row is None or row[2] == "processed":
            return
        masked = students.invite_request_mask(row[0], row[1])
        if masked is not None:
            conn.execute("UPDATE telegram_updates SET text = %s WHERE id = %s", (masked, update_row_id))


def _update_row_id(task: ClaimedTask) -> int:
    payload = task.payload
    row_id = payload.get("update_row_id") if isinstance(payload, dict) else None
    if isinstance(row_id, bool) or not isinstance(row_id, int):
        raise PermanentTaskError("payload sem update_row_id")
    return row_id


class UpdateTaskHandler:
    """O tratador da tarefa `telegram.update` (ver o docstring do módulo), para `run_worker`.

    Usado só pela thread do laço do worker: tem a sua conexão (`_leasing.LazyConnection`), fechada
    em `close()`. As respostas saem pela fila de saída (`Outbox.enqueue`, na transação do update).

    Com `channel`, as respostas do aluno (`answers`); a confirmação de um toque sai depois do commit,
    direto pela porta (`bots.answer_callback`, com o `runtime`), fora da fila e do ritmo. O
    canal só é lido ao processar."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, runtime: BotRuntime | None = None,
                 channel: DeliveryChannel | None = None, delivery: DeliverySettings | None = None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._max_attempts = settings.max_attempts
        self._outbox = Outbox(conninfo, clock=clock, settings=settings)
        self._runtime = runtime
        self._answering = None if channel is None else _Answering(channel=channel,
                                                                 delivery=delivery or DeliverySettings())

    def __call__(self, task: ClaimedTask) -> None:
        row_id = _update_row_id(task)
        try:
            try:
                processed = _process(self._db.get(), update_row_id=row_id, now=self._clock.now(),
                                     outbox=self._outbox, answering=self._answering)
            except LookupError:
                raise PermanentTaskError("update inexistente") from None
        except Exception as exc:
            # Falha de vez (definitiva, ou temporária na última tentativa, que a fila marca `failed`):
            # a máscara do `/convite` numa transação de reserva, e a falha segue para a fila.
            if isinstance(exc, PermanentTaskError) or task.attempts >= self._max_attempts:
                self._mask_after_failure(row_id)
            raise
        if processed is None:
            return
        logger.info("telegram.update", extra={"event": "telegram.update", "update_row_id": row_id,
                                              "outcome": processed.outcome})
        confirmation = processed.confirmation
        if confirmation is not None and self._runtime is not None:
            # Depois do commit: a falha vira o registro
            # `telegram.callback_answer_failed` dentro de `answer_callback`, sem efeito.
            answer_callback(self._runtime, confirmation.bot_id, confirmation.callback_id, confirmation.text,
                            conn=self._db.get())

    def _mask_after_failure(self, row_id: int) -> None:
        """`mask_unprocessed` de melhor esforço: se a reserva também falha, o texto fica literal (como
        no resíduo da posse esgotada), e a falha que segue para a fila é a original."""
        try:
            mask_unprocessed(self._db.get(), update_row_id=row_id)
        except Exception:  # noqa: BLE001 - a falha que vale é a do processamento, registrada pela fila
            pass

    def close(self) -> None:
        """Fecha as conexões do tratador (idempotente)."""
        self._db.close()
        self._outbox.close()


def update_task_handler(conninfo: str, *, clock: Clock, runtime: BotRuntime | None,
                        settings: Settings, channel: DeliveryChannel | None = None,
                        delivery: DeliverySettings | None = None) -> UpdateTaskHandler:
    """O tratador da tarefa `telegram.update`, para `run_worker`. As respostas vão à
    fila de saída; o `runtime` só serve à confirmação dos toques das respostas (`bots.answer_callback`).

    `channel` (o canal com rota, que renderiza as respostas; `None` = sem as
    respostas do aluno) e `delivery` (`None` = `DeliverySettings()`; o `free_reply_seconds` chega a
    `freemessages.store_free_message`)."""
    return UpdateTaskHandler(conninfo, clock=clock, settings=settings, runtime=runtime, channel=channel,
                             delivery=delivery)
