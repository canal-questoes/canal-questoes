"""Recusa do termo pelo aluno.


Contratos: o tratamento do `/recuso` (sem candidato → `HELP`; oferecimento fora de `active` ou convite
vencido → `LINK_INVALID`; senão `TERMS_REFUSED` ao chat do update, `student_terms_refused`, nenhuma
mudança e nenhum evento; `/Recuso` sem diferenciar maiúsculas; em grupo ou `/recuso@<outro bot>` →
`not_handled`), o "Candidato" (o convite `pending` de aluno de maior `presented_update_id` entre os
apresentados neste bot e neste chat), os eventos (a recusa não gera evento), os desfechos e os textos.

Tudo passa pelo caminho real, só com dublês: a lista entra pela API (`students.import`), o envio em
duas fases (`invites.prepare_send` e `invites.send`) enfileira as tarefas `invite.email`, um worker
com `bots` e `verification` (o adaptador SMTP real, fixture `mail_channel`) manda os e-mails ao SMTP
falso, e o código é lido do link do e-mail (`smtp.invite_links()`, que o registra em
`remember_secret`). O aluno fala com o bot pela Bot API falsa. "Nenhuma mudança" é conferida por um
instantâneo de **todas** as colunas de **todas** as linhas de `invites`, `enrollments` e
`delivery_endpoints` (`row_to_json`), antes e depois.

Os textos esperados são os do aluno e os de oferecimentos e bots, escritos aqui por
extenso (não vêm do código). Identidades e remetentes neutros. Horários pelo `FakeClock`; com o
worker no ar, os saltos são de 15 s (menos que a posse de 30 s), salvo onde o caso exige vencer o
convite. Ids internos, códigos e pseudônimos nunca como literal.

No esqueleto, toda função falha na primeira chamada de `students.import` (rota ausente, 404); os
casos que o esqueleto já trata (`/recuso` em grupo e para outro bot) ficam na mesma função
de um `/recuso` com candidato.
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    import_students,
    list_events,
    prepare_invites,
    register_bot,
    send_invites,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.smtp import SENDER

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
DEFAULT_HOURS = 168

BOT_USERNAME = "cq_aluno_recusa_bot"
OTHER_BOT_USERNAME = "outro_bot_da_turma"
WORKER_ID = "worker-aluno-recusa"
GROUP_CHAT = {"id": -4001234599, "type": "group", "title": "Turma de Teste da Recusa"}
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
MESSAGE_ID = re.compile(r"<[^<>@\s]+@cq\.example\.org>")  # domínio do remetente (`SENDER`)
SIZE_PARAM = re.compile(r"SIZE=[0-9]+", re.IGNORECASE)


def _fill(template: str) -> str:
    return template.replace("{course}", COURSE).replace("{term}", TERM)


# Os textos do aluno, por extenso.
TERMS_TEMPLATE = "\n".join([
    "Termo de uso do bot do oferecimento {course} ({term}), versão {version}",
    "",
    "Leia até o fim: as opções para aceitar ou recusar estão no final.",
    "",
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a elas. As "
    "respostas são registradas para o ensino na disciplina e para o piloto de um Trabalho de Conclusão "
    "de Curso (TCC) de Ciência da Computação da Unicamp, que avalia este sistema.",
    "",
    "Quais dados são guardados:",
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;",
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do perfil e o "
    "telefone não são guardados);",
    "• as suas respostas e as mensagens que você mandar a este bot.",
    "",
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno aparece "
    "por um código. A ligação entre o código e o seu nome só é consultada quando necessário, e cada "
    "consulta fica registrada. No TCC, os resultados aparecem só de forma agregada, sem identificar "
    "ninguém.",
    "",
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste oferecimento, "
    "definida pela coordenação. Depois desse prazo, os seus dados de identificação (nome, RA, e-mail e "
    "conta do Telegram) são apagados, e as respostas continuam guardadas sem eles.",
    "",
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste piloto, estas "
    "questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail continua valendo até "
    "vencer.",
    "",
    "Para aceitar, toque em /aceito",
    "Para recusar, toque em /recuso",
])
TERMS = _fill(TERMS_TEMPLATE).replace("{version}", "1")
TERMS_REFUSED = _fill(
    "Tudo bem: você recusou o termo de uso. Nada foi vinculado, e você não vai receber questões do "
    "oferecimento {course} ({term}) por este chat.\n"
    "\n"
    "Se mudar de ideia, toque de novo no link do e-mail, nesta mesma conta do Telegram, enquanto ele "
    "valer. Se o link vencer, mande /convite seguido de um espaço e do seu e-mail institucional (o mesmo "
    "da lista da turma) para receber um novo link por e-mail.")
WELCOME_STUDENT = _fill(
    "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {course} ({term}). As questões "
    "da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação.")
# O e-mail do convite, por extenso; `{link}` e `{validity}` trocados por caso.
SUBJECT = _fill("Seu link para o bot do oferecimento {course} ({term})")
BODY_TEMPLATE = _fill(
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da disciplina chegam por "
    "um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot vai "
    "mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu e-mail "
    "institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.")
# Os textos de oferecimentos e bots, por extenso.
HELP = _fill(
    "Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite que você "
    "recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."

# Tipos de evento do fluxo, como literais (o esqueleto não tem os do vínculo do aluno).
STUDENT_LINK_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                  "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                  "student.invite_revoked", "student.joined")
FLOW_EVENT_TYPES = STUDENT_LINK_EVENT_TYPES + ("team.link_created", "team.link_revoked", "team.joined",
                                     "team.join_refused", "endpoint.blocked", "outbox.failed",
                                     "outbox.reply_failed")


# ---------------------------------------------------------------------------------------------
# Apoio


@dataclasses.dataclass(frozen=True)
class _Student:
    """Uma linha neutra da lista (fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`)."""

    name: str
    ra: str
    email: str

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


def _student(n: int) -> _Student:
    return _Student(f"Pessoa Aluna Recusa {n:02d}", f"57000004{n:02d}", f"aluna.recusa{n:02d}@turma.example.org")


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot, então nada pode chegar
    aqui (conferido no fim de cada teste)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _offering(client: Any, bot_api: Any, code: str) -> tuple[dict, int]:
    """Oferecimento ativo, com bot registrado na Bot API falsa."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
    bot = bot_api.add_bot(BOT_USERNAME)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _import(client: Any, offering: dict, students: list[_Student]) -> None:
    """Importa pessoas novas; a resposta traz só as contagens exatas."""
    body = import_students(client, ADMIN_TOKEN, offering["code"], [s.row() for s in students])
    n = len(students)
    assert body == {"offering": _ref(offering), "received": n, "people_created": n, "people_reused": 0,
                    "enrollments_created": n, "already_enrolled": 0}


def _batch(client: Any, clock: Any, offering: dict, *, eligible: int, valid_hours: int | None = None) -> None:
    """Envio em lote em duas fases, com as respostas exatas."""
    hours = DEFAULT_HOURS if valid_hours is None else valid_hours
    prepared = prepare_invites(client, ADMIN_TOKEN, offering["code"], valid_hours=valid_hours)
    confirmation = prepared["confirmation_code"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", confirmation)
    assert prepared == {"offering": _ref(offering), "eligible": eligible, "valid_hours": hours,
                        "confirmation_code": confirmation,
                        "expires_at": iso(clock.now() + timedelta(minutes=10))}
    queued = send_invites(client, ADMIN_TOKEN, offering["code"], confirmation)
    assert queued == {"offering": _ref(offering), "queued": eligible, "valid_hours": hours}


def _start_worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel, verification: Any) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=WORKER_ID),
                             verification=verification)


def _wait_polling(bot_api: Any, bot: int) -> None:
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)


def _wait_sent(bot_api: Any, sql: Any, smtp: Any, total: int) -> None:
    """Espera `total` envios de convite, todos `sent`, com a tarefa `invite.email:<id>` `done`; o SMTP
    falso recebeu exatamente um e-mail por envio."""

    def ready() -> list[tuple] | None:
        rows = sql.execute(
            "SELECT s.state, t.state FROM invite_sends s "
            "LEFT JOIN tasks t ON t.idempotency_key = 'invite.email:' || s.id ORDER BY s.id").fetchall()
        return rows if len(rows) == total and all(r == ("sent", "done") for r in rows) else None

    bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(smtp.messages()) == total


def _real_now() -> datetime:
    """O relógio real (o `Date` do e-mail é conferido contra ele, não contra o `FakeClock`)."""
    return datetime.now(timezone.utc)


def _send(client: Any, clock: Any, bot_api: Any, sql: Any, smtp: Any, offering: dict, *, eligible: int,
          valid_hours: int | None = None) -> tuple[datetime, datetime]:
    """Envio em lote (duas fases) e a espera dos e-mails; devolve a janela real do envio."""
    before = _real_now()
    _batch(client, clock, offering, eligible=eligible, valid_hours=valid_hours)
    _wait_sent(bot_api, sql, smtp, eligible)
    return before, _real_now()


def _validity(expires_at: datetime) -> str:
    """`AAAA-MM-DD HH:MM <abreviação> (<fuso>)`, no fuso do oferecimento."""
    local = expires_at.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def _mail_code(smtp: Any, student: _Student, *, expires: datetime, window: tuple[datetime, datetime]) -> str:
    """O código do último e-mail de convite do aluno, com a mensagem conferida por inteiro:
    envelope (`MAIL FROM` sem parâmetro além do `SIZE`, `RCPT TO`),
    cabeçalhos e texto decodificado exatos; `Date` (UTC, dentro da janela real do envio) e `Message-ID`
    (domínio do remetente) por forma. O link é lido por `invite_links()`, que registra o código em
    `remember_secret`."""
    links = [link for link in smtp.invite_links() if link.rcpt_to == (student.email,)]
    assert links, "nenhum e-mail de convite para o aluno"
    link = links[-1]
    assert (link.bot_username, link.acknowledged) == (BOT_USERNAME, True)
    message = next(m for m in smtp.messages() if m.seq == link.message_seq)
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated, message.acknowledged) == (
        SENDER, (student.email,), False, True, True)
    assert [p for p in message.mail_params if not SIZE_PARAM.fullmatch(p)] == []
    names = [name.lower() for name, _ in message.headers]
    assert len(names) == len(set(names)), f"cabeçalho repetido: {names}"
    headers = {name.lower(): value for name, value in message.headers}
    date, message_id = headers.pop("date"), headers.pop("message-id")
    assert headers == {"from": f"Canal de Questões <{SENDER}>", "to": student.email, "subject": SUBJECT,
                       "mime-version": "1.0", "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    sent_at = parsedate_to_datetime(date)
    assert sent_at.utcoffset() == timedelta(0), f"Date fora de UTC: {date}"
    assert window[0].replace(microsecond=0) <= sent_at <= window[1]
    assert MESSAGE_ID.fullmatch(message_id), f"Message-ID fora da forma: {message_id}"
    assert link.url == f"https://t.me/{BOT_USERNAME}?start={link.code}"
    body = BODY_TEMPLATE.replace("{link}", link.url).replace("{validity}", _validity(expires))
    assert message.text == body + "\n"
    assert message.raw.isascii()  # quoted-printable, sem caminho SMTPUTF8
    return link.code


def _person(sql: Any, student: _Student) -> dict:
    """O pseudônimo da pessoa da linha (lido pelo RA; a importação não o devolve)."""
    row = sql.execute("SELECT p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (student.ra,)).fetchone()
    assert row is not None
    return {"pseudo_id": row[0], "alias": row[1]}


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _last_outbox(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _updates(sql: Any, bot: int, update_id: int) -> list[tuple]:
    """(id, kind, chat_id, chat_type, sender_id, text, command, state, outcome, processed_at)."""
    return sql.execute(
        """
        SELECT u.id, u.kind, u.chat_id, u.chat_type, u.sender_id, u.text, u.command, u.state,
               u.outcome, u.processed_at
          FROM telegram_updates u JOIN bots b ON b.id = u.bot_id
         WHERE b.telegram_bot_id = %s AND u.update_id = %s
         ORDER BY u.id
        """, (bot, update_id)).fetchall()


def _processed(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update (tipo fora do allowed_updates)"

    def ready() -> list[tuple] | None:
        rows = _updates(sql, bot, update_id)
        return rows if rows and all(row[7] == "processed" for row in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _command(bot_api: Any, sql: Any, bot: int, sender: Any, text: str, *, outcome: str, at: Any,
             chat: dict | None = None) -> tuple[int, int]:
    """Manda `text` com a entidade `bot_command` sobre a 1ª palavra e espera o processamento; confere
    a linha gravada (texto literal, `command`, `outcome` e `processed_at`); devolve o `update_id` e o
    id da linha."""
    uid = bot_api.push_text(bot, sender, text, bot_command=True, chat=chat)
    row = _processed(bot_api, sql, bot, uid)
    where = chat if chat is not None else {"id": sender.user_id, "type": "private"}
    assert row[1:] == ("message", where["id"], where["type"], sender.user_id, text, text.split(" ", 1)[0],
                       "processed", outcome, at)
    return uid, row[0]


def _outbox_after(sql: Any, after: int) -> list[tuple]:
    """(id, text, state, pseudo_id do destinatário, bot, chat do endereço, update respondido)."""
    return sql.execute(
        """
        SELECT o.id, o.text, o.state, p.pseudo_id::text, b.telegram_bot_id, d.chat_id, u.update_id
          FROM outbox_messages o
          LEFT JOIN people p ON p.id = o.recipient_person_id
          LEFT JOIN bots b ON b.id = o.bot_id
          LEFT JOIN delivery_endpoints d ON d.id = o.delivery_endpoint_id
          LEFT JOIN telegram_updates u ON u.id = o.reply_to_update_id
         WHERE o.id > %s
         ORDER BY o.id
        """, (after,)).fetchall()


def _replies(bot_api: Any, sql: Any, after: int, expected: list[tuple]) -> None:
    """Espera as mensagens novas da fila em estado final e confere `(text, state, pessoa, bot, chat do
    endereço, update respondido)` de cada uma, na ordem."""

    def ready() -> list[tuple] | None:
        rows = _outbox_after(sql, after)
        return rows if len(rows) >= len(expected) and all(r[2] in ("sent", "failed") for r in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert [r[1:] for r in rows] == expected


def _sent_texts(bot_api: Any, bot: int, sender: Any) -> list[str]:
    return [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)]


def _invite(sql: Any, code: str) -> list[tuple]:
    """(state, expires_at, created_at, used_at, revoked_at, presented_update_id,
    presented_terms_version) do convite do código."""
    return sql.execute(
        "SELECT state, expires_at, created_at, used_at, revoked_at, presented_update_id, "
        "presented_terms_version FROM invites WHERE code_sha256 = %s", (_sha256(code),)).fetchall()


def _enrollment(sql: Any, person: dict, offering: dict) -> list[tuple]:
    """(role, state, created_at, activated_at, terms_version, terms_accepted_at) da matrícula."""
    return sql.execute(
        """
        SELECT e.role, e.state, e.created_at, e.activated_at, e.terms_version, e.terms_accepted_at
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE p.pseudo_id = %s::uuid AND o.offering_id = %s::uuid
        """, (person["pseudo_id"], offering["offering_id"])).fetchall()


def _endpoints(sql: Any) -> list[tuple]:
    return sql.execute(
        """
        SELECT p.pseudo_id::text, b.telegram_bot_id, d.channel, d.chat_id, d.state, d.created_at,
               d.changed_at
          FROM delivery_endpoints d
          JOIN people p ON p.id = d.person_id
          JOIN bots b ON b.id = d.bot_id
         ORDER BY d.id
        """).fetchall()


def _snapshot(sql: Any) -> dict[str, list[tuple]]:
    """Todas as colunas de todas as linhas de convite, matrícula e endereço."""
    return {table: sql.execute(f"SELECT row_to_json(t)::text FROM {table} t ORDER BY t.id").fetchall()
            for table in ("invites", "enrollments", "delivery_endpoints")}


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos do fluxo depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *FLOW_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _joined(offering: dict, person: dict, at: Any) -> dict:
    return {"type": "student.joined", "occurred_at": iso(at), "actor_token_id": None,
            "data": {"offering_id": offering["offering_id"], "person": person["pseudo_id"],
                     "terms_version": 1, "endpoint_replaced": False}}


def _close(client: Any, offering: dict) -> None:
    resp = client.post(f"/v1/offerings/{offering['code']}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"encerramento falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.close", 200, body)
    assert body["state"] == "closed"


# ---------------------------------------------------------------------------------------------
# Recusa


def test_recuso_changes_nothing_and_the_same_link_links_afterwards(client: Any, clock: Any, sql: Any,
                                                                  bot_api: Any, bot_worker_thread: Any,
                                                                  settings: Any, smtp: Any,
                                                                  mail_channel: Any) -> None:
    """Recusa com e sem candidato (`/recuso` e "Candidato").

    Com um aluno já vinculado noutro chat (para que haja endereço no instantâneo) e o termo
    apresentado a outro aluno no chat X:
    - `/recuso` num chat sem termo apresentado → `HELP` ao chat do update (`help`), sem outra mudança;
    - `/recuso` em grupo e `/recuso@<outro bot>` no privado de X → `not_handled`, sem resposta nem
      mudança (comportamento do esqueleto, na mesma função de um `/recuso` com candidato);
    - `/recuso`, `/Recuso` e `/recuso@<este bot>` em X → `TERMS_REFUSED` exato ao chat do update,
      `student_terms_refused`, e **nenhuma** linha de `invites`, `enrollments` ou `delivery_endpoints`
      muda (instantâneo de todas as colunas, antes e depois), sem evento;
    - depois, o `/start` com o mesmo link reapresenta o termo (a apresentação passa a ser o update
      novo) e o `/aceito` vincula (`student_joined`, `WELCOME_STUDENT` ao endereço,
      `student.joined`); vinculado, um `/recuso` seguinte não tem candidato → `HELP`."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-recusa")
    linked_student, refusing_student = _student(1), _student(2)
    _import(client, offering, [linked_student, refusing_student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=2)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    linked_code, code = (_mail_code(smtp, s, expires=expires, window=window)
                         for s in (linked_student, refusing_student))
    linked, person = _person(sql, linked_student), _person(sql, refusing_student)

    # Um aluno vinculado no chat Y: há endereço para o instantâneo.
    clock.advance(STEP)
    t1 = clock.now()
    other_chat = make_user(601)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, other_chat, f"/start {linked_code}", outcome="student_terms_presented",
                      at=t1)
    _command(bot_api, sql, bot, other_chat, "/aceito", outcome="student_joined", at=t1)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid),
        (WELCOME_STUDENT, "sent", linked["pseudo_id"], bot, other_chat.user_id, None)])
    assert _endpoints(sql) == [(linked["pseudo_id"], bot, "telegram", other_chat.user_id, "active", t1, t1)]

    clock.advance(STEP)
    t2 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)

    # `/recuso` sem candidato: `HELP` ao chat do update.
    lost = make_user(602)
    uid, _ = _command(bot_api, sql, bot, lost, "/recuso", outcome="help", at=t2)
    _replies(bot_api, sql, out_cursor, [(HELP, "sent", None, bot, None, uid)])
    assert _sent_texts(bot_api, bot, lost) == [HELP]

    # O termo apresentado ao aluno no chat X.
    sender = make_user(603)
    out_cursor = _last_outbox(sql)
    uid, start_row = _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_terms_presented",
                              at=t2)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid)])
    presented = [("pending", expires, t0, None, None, start_row, 1)]
    assert _invite(sql, code) == presented
    before = _snapshot(sql)

    # Em grupo e para outro bot: `not_handled`, sem resposta nem mudança.
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, "/recuso", outcome="not_handled", at=t2, chat=GROUP_CHAT)
    _command(bot_api, sql, bot, sender, f"/recuso@{OTHER_BOT_USERNAME}", outcome="not_handled", at=t2)
    assert _outbox_after(sql, out_cursor) == []
    assert bot_api.sent_messages(bot, chat_id=GROUP_CHAT["id"]) == []
    assert _snapshot(sql) == before

    # `/recuso`, `/Recuso` e `/recuso@<este bot>`: `TERMS_REFUSED`, nada muda.
    expected_texts = [TERMS]
    for text in ("/recuso", "/Recuso", f"/recuso@{BOT_USERNAME}"):
        out_cursor = _last_outbox(sql)
        uid, _ = _command(bot_api, sql, bot, sender, text, outcome="student_terms_refused", at=t2)
        _replies(bot_api, sql, out_cursor, [(TERMS_REFUSED, "sent", None, bot, None, uid)])
        expected_texts.append(TERMS_REFUSED)
        assert _sent_texts(bot_api, bot, sender) == expected_texts, text
        assert _snapshot(sql) == before, text
        assert _invite(sql, code) == presented, text
        assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)], text
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)
            if c.params.get("chat_id") == sender.user_id] == [
        ({"chat_id": sender.user_id, "text": TERMS}, 200),
        *[({"chat_id": sender.user_id, "text": TERMS_REFUSED}, 200)] * 3]
    assert _events_after(client, cursor) == []

    # O mesmo link, dentro da validade: o termo de novo, e o `/aceito` vincula.
    clock.advance(STEP)
    t3 = clock.now()
    out_cursor = _last_outbox(sql)
    uid, again_row = _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_terms_presented",
                              at=t3)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid)])
    assert _invite(sql, code) == [("pending", expires, t0, None, None, again_row, 1)]
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, "/aceito", outcome="student_joined", at=t3)
    _replies(bot_api, sql, out_cursor, [(WELCOME_STUDENT, "sent", person["pseudo_id"], bot, sender.user_id,
                                         None)])
    assert _invite(sql, code) == [("used", expires, t0, t3, None, again_row, 1)]
    assert _enrollment(sql, person, offering) == [("student", "active", t0, t3, 1, t3)]
    assert _endpoints(sql) == [(linked["pseudo_id"], bot, "telegram", other_chat.user_id, "active", t1, t1),
                               (person["pseudo_id"], bot, "telegram", sender.user_id, "active", t3, t3)]
    assert _events_after(client, cursor) == [_joined(offering, person, t3)]

    # Vinculado, não há candidato: `/recuso` → `HELP`, sem mudança.
    after_link = _snapshot(sql)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, sender, "/recuso", outcome="help", at=t3)
    _replies(bot_api, sql, out_cursor, [(HELP, "sent", None, bot, None, uid)])
    assert _snapshot(sql) == after_link
    assert _sent_texts(bot_api, bot, sender) == [*expected_texts, TERMS, WELCOME_STUDENT, HELP]
    assert _events_after(client, cursor) == [_joined(offering, person, t3)]
    assert channel.calls == []


def test_recuso_does_not_extend_the_validity_and_after_expiry_is_link_invalid(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Validade (a recusa do termo não pode prorrogar a validade, avaliada no processamento pelo
    relógio do sistema); `/recuso` com convite vencido → `LINK_INVALID`.

    Convite de 1 hora (preparo com `valid_hours = 1`), termo apresentado no chat X e recusado antes do
    vencimento (`TERMS_REFUSED`): o `expires_at` do convite não muda. No instante exato do vencimento
    (a fronteira é estrita: vale só com `agora < expires_at`), o `/recuso` → `LINK_INVALID` ao chat do
    update (`student_refused_invalid`), e o `/start` com o mesmo link e o `/aceito` também →
    `LINK_INVALID`; em todos, o convite continua `pending` e apresentado (o vencido é calculado), a
    matrícula `pending`, nenhum endereço e nenhum evento."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-recusa-vence")
    student = _student(3)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, valid_hours=1)
    expires = t0 + timedelta(hours=1)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)

    clock.advance(STEP)
    t1 = clock.now()
    sender = make_user(611)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid, start_row = _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_terms_presented",
                              at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid)])
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, sender, "/recuso", outcome="student_terms_refused", at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS_REFUSED, "sent", None, bot, None, uid)])
    presented = [("pending", expires, t0, None, None, start_row, 1)]
    assert _invite(sql, code) == presented
    before = _snapshot(sql)

    clock.set(expires)
    at = clock.now()
    for text in ("/recuso", f"/start {code}", "/aceito"):
        out_cursor = _last_outbox(sql)
        uid, _ = _command(bot_api, sql, bot, sender, text, outcome="student_refused_invalid", at=at)
        _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
        assert _invite(sql, code) == presented, text
        assert _snapshot(sql) == before, text
    assert _sent_texts(bot_api, bot, sender) == [TERMS, TERMS_REFUSED, LINK_INVALID, LINK_INVALID, LINK_INVALID]
    assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)]
    assert _endpoints(sql) == []
    assert _events_after(client, cursor) == []
    assert channel.calls == []


def test_recuso_in_an_offering_that_is_not_active_is_link_invalid(client: Any, clock: Any, sql: Any,
                                                                  bot_api: Any, bot_worker_thread: Any,
                                                                  settings: Any, smtp: Any,
                                                                  mail_channel: Any) -> None:
    """Com o oferecimento não ativo, `LINK_INVALID`, sem mudança; a ordem das conferências
    (`/recuso`: sem candidato → `HELP`, antes do estado do oferecimento; oferecimento fora de `active`
    → `LINK_INVALID`).

    Termo apresentado no chat X com o oferecimento ativo; depois o oferecimento é encerrado (o bot
    continua consultado, `POLLED_STATES`): `/recuso` em X → `LINK_INVALID` ao chat do update
    (`student_refused_invalid`), e o `/aceito` também; `/recuso` num chat sem termo apresentado →
    `HELP` (`help`). Nenhuma linha de convite, matrícula ou endereço muda, e nenhum evento."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-recusa-encerrado")
    student = _student(4)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)

    clock.advance(STEP)
    t1 = clock.now()
    sender = make_user(621)
    out_cursor = _last_outbox(sql)
    uid, start_row = _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_terms_presented",
                              at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid)])
    presented = [("pending", expires, t0, None, None, start_row, 1)]
    assert _invite(sql, code) == presented

    _close(client, offering)
    clock.advance(STEP)
    t2 = clock.now()
    before = _snapshot(sql)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    for text in ("/recuso", "/aceito"):
        out_cursor = _last_outbox(sql)
        uid, _ = _command(bot_api, sql, bot, sender, text, outcome="student_refused_invalid", at=t2)
        _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
        assert _snapshot(sql) == before, text
    lost = make_user(622)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, lost, "/recuso", outcome="help", at=t2)
    _replies(bot_api, sql, out_cursor, [(HELP, "sent", None, bot, None, uid)])
    assert _snapshot(sql) == before
    assert _invite(sql, code) == presented
    assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)]
    assert _endpoints(sql) == []
    assert _sent_texts(bot_api, bot, sender) == [TERMS, LINK_INVALID, LINK_INVALID]
    assert _sent_texts(bot_api, bot, lost) == [HELP]
    assert _events_after(client, cursor) == []
    assert channel.calls == []
