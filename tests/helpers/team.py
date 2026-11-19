"""Equipe simulada dos relatórios (dublê dos testes).

Vincula membros da equipe a um oferecimento pelo caminho real dos bots (link de equipe pela API, `team.link`, e
`/start <código>` do chat privado do membro, pela Bot API falsa ou pela `ConsoleInbox`), deixa um membro com
o endereço `blocked` (o 403 da Bot API falsa na boas-vindas) ou sem endereço (link gerado, nenhum `/start`)
e lê as mensagens à equipe da fila de saída e as linhas do console de um membro.

- `create_team_offering(client, bot_api, *, code=None)`: um oferecimento `active` com bot próprio na Bot
  API falsa (nomes neutros; código `equipe-relatorio-<n>`); devolve `(offering, bot_id)` (o id do bot no dublê).
- `link_team(client, sql, offering, bot_id, states, *, bot_api=None, inbox=None, timeout=...)`: vincula um
  membro por item de `states` (`ACTIVE`, `BLOCKED` ou `NO_ENDPOINT`), em ordem, e devolve a `Team`. Com
  `bot_api`, o `/start` vai pela Bot API falsa (`push_start`); com `inbox` (a `ConsoleInbox`), pela entrada
  simulada do console (o `bot_id` da `ConsoleInbox` é o id interno do bot, lido do banco). **Precisa de um
  worker no ar** que processe o update (o teste ou a fixture `team_members` o sobe; o vínculo é dos oferecimentos e bots, sem
  nada dos relatórios): cada membro só sai pronto quando o endereço tem o estado esperado e a boas-vindas chegou a
  estado final (`sent`, ou `failed` no `BLOCKED`). `BLOCKED` só existe pela Bot API falsa: o chat é
  bloqueado no dublê antes do `/start`, e a boas-vindas recebe o 403 (`bot was blocked by the user`), que
  deixa o endereço `blocked`; o chat continua bloqueado no dublê depois disso.
- `team_messages(sql, notice_type=None, recipient=None, *, notice_id=None)`: as mensagens à equipe (as
  linhas de `outbox_messages` com `notice_id`), com o `kind` do aviso, na ordem de `outbox_messages.id`,
  filtradas pelo tipo do aviso, pelo destinatário (o `TeamMember` ou o `pseudo_id`) e pelo aviso;
  `team_messages_by_notice(...)` agrupa o mesmo resultado por `notice_id`, na ordem do primeiro envio.
- `team_lines(console, member)`: as linhas JSON do console cujo `recipient` é o
  `pseudo_id` do membro, na ordem em que foram escritas; `console` é a fonte das linhas (um `StringIO`, o
  caminho do arquivo ou uma função que devolve o texto, como em `tests/helpers/console_student.py`) ou um
  objeto com `all_lines()` (o `ConsoleStudent`).

Os nomes, RAs, códigos e remetentes são neutros (fora dos alvos canário): `Membro da Equipe <n>`, RA
`88<n:08d>`, remetentes `make_user(TEAM_USER_BASE + n)`. O rótulo do membro (`membro-<i>`) é só do teste.
Nenhuma importação do núcleo além dos ajudantes de teste e de `cq_server.adapters.console.ConsoleInbox`
(usada só como tipo). Ids internos, `channel_ref` e `update_id` nunca são literais esperados: só relações.
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from cq_server.adapters.console import ConsoleInbox
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, create_offering, create_team_link, link_code,
                               register_bot, register_person)
from tests.helpers.bot_api import TelegramUser, make_user
from tests.helpers.console_student import ConsoleLine, _reader, read_lines

T = TypeVar("T")

# Estados do membro na equipe simulada.
ACTIVE = "active"
BLOCKED = "blocked"
NO_ENDPOINT = "no_endpoint"
MEMBER_STATES = (ACTIVE, BLOCKED, NO_ENDPOINT)

# Canais do vínculo.
TELEGRAM = "telegram"
CONSOLE = "console"

# Remetentes neutros dos membros (`make_user`, fora da faixa canário e longe dos índices de outros testes,
# que usam até 9000 + n).
TEAM_USER_BASE = 70_000
# Espera do vínculo de um membro (inclui a subida do worker: esquema, posse, `deleteWebhook`).
LINK_TIMEOUT = 15.0
POLL_SECONDS = 0.02

_members = itertools.count(1)
_offerings = itertools.count(1)


@dataclass(frozen=True)
class TeamMember:
    """Um membro da equipe simulada.

    `label` é o rótulo neutro do teste (`membro-<i>`, na ordem de `states`); `person` é o corpo do
    `people.register` (com `alias` e `pseudo_id`); `recipient` é o `pseudo_id` em texto (o `recipient` das
    linhas do console e o destinatário das mensagens à equipe); `chat_id` é o chat privado do membro
    (`None` sem endereço); `user` é o remetente da Bot API falsa (`None` no console e sem endereço);
    `state` é o estado pedido (`ACTIVE`, `BLOCKED` ou `NO_ENDPOINT`)."""

    label: str
    person: dict
    state: str
    chat_id: int | None
    user: TelegramUser | None = field(default=None, repr=False)

    @property
    def pseudo_id(self) -> str:
        return str(self.person["pseudo_id"])

    @property
    def alias(self) -> str:
        return str(self.person["alias"])

    @property
    def recipient(self) -> str:
        return self.pseudo_id

    @property
    def has_endpoint(self) -> bool:
        return self.state != NO_ENDPOINT


@dataclass(frozen=True)
class Team:
    """A equipe vinculada a um oferecimento: o `offering` (corpo do `Offering`), o bot (`bot_id` no dublê
    e `bot_row`, o id interno), o canal do vínculo (`TELEGRAM` ou `CONSOLE`) e os membros, na ordem."""

    offering: dict
    bot_id: int
    bot_row: int
    channel: str
    members: tuple[TeamMember, ...]
    # A saída do console do worker que fez o vínculo (a fixture `team_members` a guarda; `None` fora dela).
    stream: Any = field(default=None, repr=False, compare=False)

    @property
    def code(self) -> str:
        return str(self.offering["code"])

    def member(self, label: str) -> TeamMember:
        found = [m for m in self.members if m.label == label]
        assert len(found) == 1, f"membro {label!r} inexistente na equipe simulada"
        return found[0]

    def in_state(self, state: str) -> list[TeamMember]:
        assert state in MEMBER_STATES, f"estado de membro desconhecido: {state!r}"
        return [m for m in self.members if m.state == state]

    @property
    def active(self) -> list[TeamMember]:
        """Os membros com endereço ativo (os destinatários dos avisos)."""
        return self.in_state(ACTIVE)

    @property
    def recipients(self) -> list[str]:
        """Os `pseudo_id` dos membros com endereço ativo, na ordem dos membros."""
        return [m.recipient for m in self.active]


@dataclass(frozen=True)
class TeamMessage:
    """Uma mensagem à equipe (linha de `outbox_messages` com `notice_id`), com o aviso.

    `kind` é o tipo do aviso (`team_notices.kind`); `recipient` é o `pseudo_id` do destinatário; `text` e
    `text_format` valem na forma de texto (avisos e resumos) e são `None` na forma da prévia, que tem
    `role` (`question`, `media`, `field_prompt`), `media_id` e `field_index`."""

    message_id: int
    notice_id: int
    kind: str
    recipient: str
    text: str | None
    text_format: str | None
    role: str | None
    media_id: str | None
    field_index: int | None
    state: str
    priority: int
    channel_ref: str | None


def create_team_offering(client: Any, bot_api: Any, *, code: str | None = None) -> tuple[dict, int]:
    """Um oferecimento `active` com bot próprio registrado na Bot API falsa (nomes neutros); devolve
    `(offering, bot_id)`, com o `bot_id` do dublê."""
    offering = create_offering(client, ADMIN_TOKEN, code=code or f"equipe-relatorio-{next(_offerings)}")
    bot_id = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot_id))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    return offering, bot_id


def _wait(predicate: Callable[[], T], what: str, timeout: float) -> T:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"equipe simulada: {what} não aconteceu em {timeout} s")
        time.sleep(POLL_SECONDS)


def _bot_row(sql: Any, bot_id: int) -> int:
    row = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot_id,)).fetchone()
    assert row is not None, "o bot do oferecimento não está registrado"
    return int(row[0])


def _member_settled(sql: Any, person: dict, bot_row: int, chat_id: int, expected: str) -> bool:
    """O endereço do membro no bot tem o estado esperado, com o chat dele, e toda mensagem da fila ao
    membro nesse bot (a boas-vindas) está em estado final (`sent`, ou `failed` no bloqueado)."""
    endpoint = sql.execute(
        "SELECT d.state, d.chat_id FROM delivery_endpoints d JOIN people p ON p.id = d.person_id "
        "WHERE p.pseudo_id = %s AND d.bot_id = %s AND d.state IN ('active', 'blocked')",
        (uuid.UUID(str(person["pseudo_id"])), bot_row)).fetchall()
    if endpoint != [(expected, chat_id)]:
        return False
    states = [row[0] for row in sql.execute(
        "SELECT o.state FROM outbox_messages o JOIN people p ON p.id = o.recipient_person_id "
        "WHERE p.pseudo_id = %s AND o.bot_id = %s ORDER BY o.id",
        (uuid.UUID(str(person["pseudo_id"])), bot_row)).fetchall()]
    final = "failed" if expected == BLOCKED else "sent"
    return bool(states) and all(state == final for state in states)


def link_team(client: Any, sql: Any, offering: dict, bot_id: int, states: Sequence[str], *,
              bot_api: Any = None, inbox: ConsoleInbox | None = None,
              timeout: float = LINK_TIMEOUT) -> Team:
    """Vincula um membro por item de `states`, em ordem, ao oferecimento `offering` com o bot `bot_id`
    (do dublê); devolve a `Team`. Exatamente um de `bot_api` (Bot API falsa) e `inbox` (`ConsoleInbox`).
    Um worker que processe o `/start` precisa estar no ar (ver o cabeçalho do módulo)."""
    assert (bot_api is None) != (inbox is None), "use bot_api= (Telegram) ou inbox= (console), um só"
    channel = TELEGRAM if bot_api is not None else CONSOLE
    unknown = [state for state in states if state not in MEMBER_STATES]
    assert not unknown, f"estados de membro desconhecidos: {unknown}"
    if channel == CONSOLE and BLOCKED in states:
        raise ValueError("o endereço bloqueado só existe pela Bot API falsa (o 403 da boas-vindas)")
    bot_row = _bot_row(sql, bot_id)
    members: list[TeamMember] = []
    for position, state in enumerate(states, start=1):
        n = next(_members)
        person = register_person(client, ADMIN_TOKEN, name=f"Membro da Equipe {n}", ra=f"88{n:08d}")
        code = link_code(create_team_link(client, ADMIN_TOKEN, offering["code"], person["alias"])["link"])
        label = f"membro-{position}"
        if state == NO_ENDPOINT:
            members.append(TeamMember(label=label, person=person, state=state, chat_id=None))
            continue
        user = make_user(TEAM_USER_BASE + n)
        if channel == TELEGRAM:
            if state == BLOCKED:
                bot_api.block_chat(bot_id, user)
            bot_api.push_start(bot_id, user, code)
        else:
            assert inbox is not None
            inbox.send_text(bot_row, user.user_id, f"/start {code}")
        _wait(lambda: _member_settled(sql, person, bot_row, user.user_id, state),
              f"o vínculo de {label} ({state})", timeout)
        members.append(TeamMember(label=label, person=person, state=state, chat_id=user.user_id,
                                  user=user if channel == TELEGRAM else None))
    return Team(offering=offering, bot_id=bot_id, bot_row=bot_row, channel=channel, members=tuple(members))


# --- Leitura -----------------------------------------------------------------------------------

_TEAM_MESSAGES_SQL = """
    SELECT m.id, m.notice_id, n.kind, p.pseudo_id::text, m.text, m.text_format, m.delivery_role,
           m.media_id, m.field_index, m.state, m.priority, m.channel_ref
      FROM outbox_messages m
      JOIN team_notices n ON n.id = m.notice_id
      JOIN people p ON p.id = m.recipient_person_id
     WHERE m.notice_id IS NOT NULL
       AND (%(kind)s::text IS NULL OR n.kind = %(kind)s::text)
       AND (%(recipient)s::uuid IS NULL OR p.pseudo_id = %(recipient)s::uuid)
       AND (%(notice)s::bigint IS NULL OR m.notice_id = %(notice)s::bigint)
     ORDER BY m.id
"""


def _recipient_of(recipient: TeamMember | str | uuid.UUID | None) -> str | None:
    if recipient is None:
        return None
    if isinstance(recipient, TeamMember):
        return recipient.pseudo_id
    return str(uuid.UUID(str(recipient)))


def team_messages(sql: Any, notice_type: str | None = None,
                  recipient: TeamMember | str | uuid.UUID | None = None, *,
                  notice_id: int | None = None) -> list[TeamMessage]:
    """As mensagens à equipe, na ordem de `outbox_messages.id`, com o `kind` do aviso; `notice_type`
    filtra pelo tipo do aviso, `recipient` pelo destinatário (`TeamMember` ou `pseudo_id`) e `notice_id`
    pelo aviso."""
    rows = sql.execute(_TEAM_MESSAGES_SQL, {"kind": notice_type, "recipient": _recipient_of(recipient),
                                            "notice": notice_id}).fetchall()
    return [TeamMessage(message_id=int(r[0]), notice_id=int(r[1]), kind=r[2], recipient=r[3], text=r[4],
                        text_format=r[5], role=r[6], media_id=r[7],
                        field_index=None if r[8] is None else int(r[8]), state=r[9], priority=int(r[10]),
                        channel_ref=r[11])
            for r in rows]


def team_messages_by_notice(sql: Any, notice_type: str | None = None,
                            recipient: TeamMember | str | uuid.UUID | None = None) -> dict[int, list[TeamMessage]]:
    """As mesmas mensagens de `team_messages`, agrupadas por `notice_id` (cada grupo na ordem de
    `outbox_messages.id`; os grupos na ordem da primeira mensagem de cada aviso)."""
    grouped: dict[int, list[TeamMessage]] = {}
    for message in team_messages(sql, notice_type, recipient):
        grouped.setdefault(message.notice_id, []).append(message)
    return grouped


def team_lines(console: Any, member: TeamMember | str | uuid.UUID) -> list[ConsoleLine]:
    """As linhas do console do membro (pelo `recipient`, o `pseudo_id`), na ordem em que foram escritas;
    `console` é a fonte das linhas (`StringIO`, caminho ou função) ou um objeto com `all_lines()`."""
    recipient = _recipient_of(member)
    if hasattr(console, "all_lines"):
        lines = console.all_lines()
    else:
        lines = read_lines(_reader(console)())
    return [line for line in lines if line.recipient == recipient]
