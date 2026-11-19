"""Termo e vínculo do aluno pelo bot: o código amarrado ao chat, o chat de outra pessoa, dois links
no mesmo chat, a troca de conta, o oferecimento encerrado, os comandos do aluno fora de lugar e a
mudança na versão do
termo.

Contratos: o banco (matrícula e termo aceito; convite: `presented_update_id` e
`presented_terms_version`); as respostas ao `/start <código>` e ao `/aceito`, o candidato e o
destino das respostas; `student.joined`, `student.invite_revoked`; nenhuma recusa de link de aluno
gera evento, e o código desconhecido ou de outro oferecimento segue o fluxo de equipe, com `team.join_refused
{invalid}`; desfechos e textos; e os textos e o fluxo de
equipe, que continuam).

Tudo passa pelo caminho real, só com dublês: a lista entra pela API (`students.import`), o
envio em duas fases (`invites.prepare_send` e `invites.send`; o reenvio, `invites.resend`, só na troca
de conta) enfileira as tarefas `invite.email`, um worker com `bots` e `verification` (o adaptador SMTP
real, fixture `mail_channel`) manda os e-mails ao SMTP falso, e o código é lido do link do e-mail
(`smtp.invite_links()`, que o registra em `remember_secret`). O aluno fala com o bot pela Bot API falsa;
cada caso confere o `outcome` do update, o convite, a matrícula, o endereço, as linhas da fila de saída
(destino: endereço ou chat do update), as mensagens exatas recebidas (`sent_messages`) e os eventos
exatos, na ordem (os do fluxo: os 8 tipos do vínculo do aluno e os de equipe, endereço e fila; os de bot e de
posse ficam de fora, como nos testes dos bots).

Os textos esperados são os do aluno e os da equipe, escritos aqui por
extenso (não vêm do código). Identidades neutras (fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`);
remetentes neutros (`make_user`). Horários pelo `FakeClock`; com o worker no ar, os saltos são de 15 s
(menos que a posse de 30 s), salvo onde o caso exige vencer o convite. Ids internos, códigos e
pseudônimos nunca como literal: as linhas são lidas por junção, o `presented_update_id` é comparado com
o id da linha do update, e o pseudônimo vem do banco pelo RA da linha da lista (a importação não liga
linha a pseudônimo).

No esqueleto, toda função falha na primeira chamada de
`students.import` (rota ausente, 404); as asserções sobre o comportamento que o esqueleto já tem (o
código desconhecido seguindo o fluxo de equipe; `/aceito` em grupo e para outro bot) ficam na mesma função de um
caso que falha. A troca de conta usa o reenvio pela coordenação e só fica verde depois
da implementação desse reenvio.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import secrets as stdlib_secrets
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from cq_server import botmessages
from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    import_students,
    link_code,
    list_events,
    prepare_invites,
    register_bot,
    register_person,
    resend_invites,
    send_invites,
)
from tests.helpers.bot_api import make_user, monotonic
from tests.helpers.clock import iso
from tests.helpers.contract import assert_matches
from tests.helpers.smtp import SENDER

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
OTHER_TERM = "2031s2"
ZONE = "America/Sao_Paulo"
DEFAULT_HOURS = 168

BOT_USERNAME = "cq_aluno_vinculo_bot"
SECOND_BOT_USERNAME = "cq_aluno_segundo_bot"
OTHER_BOT_USERNAME = "outro_bot_da_turma"
WORKER_ID = "worker-aluno-vinculo"
GROUP_CHAT = {"id": -4001234588, "type": "group", "title": "Turma de Teste do Termo"}
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
DELIVERY_DEADLINE_SECONDS = 5.0
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)
MESSAGE_ID = re.compile(r"<[^<>@\s]+@cq\.example\.org>")  # domínio do remetente (`SENDER`)
SIZE_PARAM = re.compile(r"SIZE=[0-9]+", re.IGNORECASE)


def _fill(template: str, *, term: str = TERM) -> str:
    return template.replace("{course}", COURSE).replace("{term}", term)


# Textos do aluno, por extenso; `{version}` só no termo.
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
WELCOME_STUDENT_TEMPLATE = (
    "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {course} ({term}). As questões "
    "da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação.")
# Textos da equipe, por extenso (não mudam).
WELCOME_TEAM_TEMPLATE = ("Pronto! Você faz parte da equipe do oferecimento {course} ({term}). Por este chat "
                         "você vai receber os avisos do oferecimento.")
HELP_TEMPLATE = ("Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite "
                 "que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."
ALREADY_LINKED = "Você já está vinculado a este oferecimento."
OTHER_PERSON = ("Esta conta do Telegram já está vinculada a outra pessoa neste oferecimento. Fale com a "
                "coordenação.")


# E-mail do convite, por extenso; `{link}` e `{validity}` trocados por caso.
SUBJECT_TEMPLATE = "Seu link para o bot do oferecimento {course} ({term})"
BODY_TEMPLATE = (
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


def _terms(version: int = 1, *, term: str = TERM) -> str:
    return _fill(TERMS_TEMPLATE, term=term).replace("{version}", str(version))


TERMS = _terms(1)
WELCOME_STUDENT = _fill(WELCOME_STUDENT_TEMPLATE)
WELCOME_TEAM = _fill(WELCOME_TEAM_TEMPLATE)
HELP = _fill(HELP_TEMPLATE)

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
    return _Student(f"Pessoa Aluna Vinculo {n:02d}", f"57000003{n:02d}", f"aluna.vinculo{n:02d}@turma.example.org")


def _ballast(n: int) -> _Student:
    """Identidades de lastro para medir o custo da redação, num oferecimento à parte."""
    return _Student(f"Pessoa Lastro Vinculo {n:03d}", f"5800000{n:03d}",
                    f"lastro.vinculo{n:03d}@turma.example.org")


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


def _offering(client: Any, bot_api: Any, code: str, username: str = BOT_USERNAME, *,
              term: str = TERM) -> tuple[dict, int]:
    """Oferecimento ativo, com bot registrado na Bot API falsa."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=term, timezone=ZONE)
    bot = bot_api.add_bot(username)
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


def _send(client: Any, clock: Any, bot_api: Any, sql: Any, smtp: Any, offering: dict, *, eligible: int,
          total: int, valid_hours: int | None = None) -> tuple[datetime, datetime]:
    """Envio em lote (duas fases) e a espera dos e-mails; devolve a janela real do envio."""
    before = _real_now()
    _batch(client, clock, offering, eligible=eligible, valid_hours=valid_hours)
    _wait_sent(bot_api, sql, smtp, total)
    return before, _real_now()


def _real_now() -> datetime:
    """O relógio real (o `Date` do e-mail é conferido contra ele, não contra o `FakeClock`)."""
    return datetime.now(timezone.utc)


def _validity(expires_at: datetime) -> str:
    """`AAAA-MM-DD HH:MM <abreviação> (<fuso>)`, no fuso do oferecimento."""
    local = expires_at.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def _mail_code(smtp: Any, student: _Student, *, expires: datetime, window: tuple[datetime, datetime],
               username: str = BOT_USERNAME, term: str = TERM) -> str:
    """O código do último e-mail de convite do aluno, com a mensagem conferida por inteiro (como nos
    testes do vínculo): envelope (`MAIL FROM` sem parâmetro além do `SIZE`, `RCPT TO`),
    cabeçalhos e texto decodificado exatos; `Date` (UTC, dentro da janela real do envio) e `Message-ID`
    (domínio do remetente) por forma. O link é lido por `invite_links()`, que registra o código em
    `remember_secret`."""
    links = [link for link in smtp.invite_links() if link.rcpt_to == (student.email,)]
    assert links, "nenhum e-mail de convite para o aluno"
    link = links[-1]
    assert (link.bot_username, link.acknowledged) == (username, True)
    message = next(m for m in smtp.messages() if m.seq == link.message_seq)
    assert (message.mail_from, message.rcpt_to, message.tls, message.authenticated, message.acknowledged) == (
        SENDER, (student.email,), False, True, True)
    assert [p for p in message.mail_params if not SIZE_PARAM.fullmatch(p)] == []
    names = [name.lower() for name, _ in message.headers]
    assert len(names) == len(set(names)), f"cabeçalho repetido: {names}"
    headers = {name.lower(): value for name, value in message.headers}
    date, message_id = headers.pop("date"), headers.pop("message-id")
    assert headers == {"from": f"Canal de Questões <{SENDER}>", "to": student.email,
                       "subject": _fill(SUBJECT_TEMPLATE, term=term), "mime-version": "1.0",
                       "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    sent_at = parsedate_to_datetime(date)
    assert sent_at.utcoffset() == timedelta(0), f"Date fora de UTC: {date}"
    assert window[0].replace(microsecond=0) <= sent_at <= window[1]
    assert MESSAGE_ID.fullmatch(message_id), f"Message-ID fora da forma: {message_id}"
    assert link.url == f"https://t.me/{username}?start={link.code}"
    body = _fill(BODY_TEMPLATE, term=term).replace("{link}", link.url).replace("{validity}", _validity(expires))
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


def _expected_row(sender: Any, text: str, outcome: str, at: Any, *, chat: dict | None = None) -> tuple:
    """A linha esperada (sem o id) de uma mensagem de texto com a entidade `bot_command` sobre a 1ª
    palavra, gravada com o texto literal e o `command` = essa palavra."""
    where = chat if chat is not None else {"id": sender.user_id, "type": "private"}
    return ("message", where["id"], where["type"], sender.user_id, text, text.split(" ", 1)[0], "processed",
            outcome, at)


def _command(bot_api: Any, sql: Any, bot: int, sender: Any, text: str, *, outcome: str, at: Any,
             chat: dict | None = None) -> tuple[int, int]:
    """Manda `text` com a entidade `bot_command` sobre a 1ª palavra e espera o processamento; confere
    a linha gravada; devolve o `update_id` e o id da linha."""
    uid = bot_api.push_text(bot, sender, text, bot_command=True, chat=chat)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _expected_row(sender, text, outcome, at, chat=chat)
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


def _replies(bot_api: Any, sql: Any, after: int, expected: list[tuple]) -> list[tuple]:
    """Espera as mensagens novas da fila em estado final (`sent` ou `failed`) e confere `(text, state,
    pessoa, bot, chat do endereço, update respondido)` de cada uma, na ordem; devolve as linhas."""

    def ready() -> list[tuple] | None:
        rows = _outbox_after(sql, after)
        return rows if len(rows) >= len(expected) and all(r[2] in ("sent", "failed") for r in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert [r[1:] for r in rows] == expected
    return rows


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
    """(pseudo_id, bot, channel, chat_id, state, created_at, changed_at), em ordem de criação."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, b.telegram_bot_id, d.channel, d.chat_id, d.state, d.created_at,
               d.changed_at
          FROM delivery_endpoints d
          JOIN people p ON p.id = d.person_id
          JOIN bots b ON b.id = d.bot_id
         ORDER BY d.id
        """).fetchall()


def _update_tasks(bot_api: Any, sql: Any, row_id: int) -> list[tuple]:
    """As tarefas `telegram.update` do update, depois de concluídas."""

    def ready() -> list[tuple] | None:
        rows = sql.execute("SELECT task_type, state, idempotency_key FROM tasks WHERE payload = %s::jsonb "
                           "ORDER BY id", (json.dumps({"update_row_id": row_id}),)).fetchall()
        return rows if rows and all(r[1] in ("done", "failed") for r in rows) else None

    return bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos do fluxo depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *FLOW_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: Any, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": None, "data": data}


def _joined(offering: dict, person: dict, at: Any, *, version: int = 1, replaced: bool = False) -> dict:
    return _event("student.joined", at, offering_id=offering["offering_id"], person=person["pseudo_id"],
                  terms_version=version, endpoint_replaced=replaced)


def _revoked(offering: dict, person: dict, at: Any, reason: str, origin: str = "batch") -> dict:
    return _event("student.invite_revoked", at, offering_id=offering["offering_id"], person=person["pseudo_id"],
                  origin=origin, revoke_reason=reason)


def _refused(offering: dict, at: Any) -> dict:
    return _event("team.join_refused", at, offering_id=offering["offering_id"], refusal="invalid")


def _close(client: Any, offering: dict) -> None:
    resp = client.post(f"/v1/offerings/{offering['code']}/close", headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"encerramento falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("offerings.close", 200, body)
    assert body["state"] == "closed"


def _redeliver_and_settle(bot_api: Any, bot: int, update_id: int) -> None:
    """Reentrega o update e espera a resposta que o levou e a consulta seguinte (o lote é gravado antes
    de o `offset` avançar), como nos testes dos bots."""
    mark = monotonic()
    bot_api.redeliver(bot, update_id)

    def delivered() -> list[Any]:
        return [c for c in bot_api.calls("getUpdates", bot_id=bot)
                if c.ended is not None and c.ended > mark and c.outcome == "ok"
                and any(u.get("update_id") == update_id for u in (c.result or []))]

    carrier = bot_api.wait_for(delivered, timeout=PROCESS_TIMEOUT)[0]
    bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.seq > carrier.seq],
                     timeout=PROCESS_TIMEOUT)


def _only(events: list[dict], type_: str) -> list[dict]:
    return [e for e in events if e["type"] == type_]


# ---------------------------------------------------------------------------------------------
# Termo e vínculo


def test_start_presents_the_terms_once_and_aceito_links_the_student(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O termo apresentado uma vez e o `/aceito` que vincula;
    o `/start` que apresenta o termo e o `/aceito` sem termo apresentado ou que vincula.

    Com pelo menos 100 identidades cadastradas antes das medições (lastro num oferecimento à parte):
    - código desconhecido → segue o fluxo de equipe: `team_refused_invalid`, `LINK_INVALID` ao chat do update
      e `team.join_refused {invalid}` (comportamento do esqueleto, na mesma função);
    - `/aceito` num chat sem termo apresentado → `HELP` ao chat do update, `outcome = help`, sem outra
      mudança;
    - `/start <código>` do aluno no privado → `TERMS` exato (versão 1) ao chat do update, uma vez, com o
      `sendMessage` só com `chat_id` e `text` em até 5 s; `student_terms_presented`; o convite
      continua `pending`, com `presented_update_id` = a linha do update e `presented_terms_version = 1`
      (amarrado ao chat); nenhum endereço, matrícula `pending` e nenhum evento;
    - `/aceito@<outro bot>` no privado e `/aceito` em grupo → `not_handled`, sem resposta nem mudança
      (comportamento do esqueleto, na mesma função);
    - `/aceito` → em até 5 s, `WELCOME_STUDENT` exato ao endereço (pessoa + bot), uma vez;
      `student_joined`; convite `used`; matrícula `active` com `activated_at`, `terms_version = 1` e
      `terms_accepted_at`; endereço `active` no chat; `student.joined {terms_version: 1,
      endpoint_replaced: false}` sem ator;
    - os dois updates reentregues não mudam nada (uma linha e uma tarefa `telegram.update` cada, nenhuma
      mensagem nova)."""
    t0 = clock.now()
    ballast = create_offering(client, ADMIN_TOKEN, code="mc102-aluno-lastro", course=COURSE, term=TERM,
                              timezone=ZONE)
    _import(client, ballast, [_ballast(n) for n in range(100)])
    offering, bot = _offering(client, bot_api, "mc102-aluno-termo")
    student = _student(1)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    clock.advance(STEP)
    t_send = clock.now()
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1)
    expires = t_send + timedelta(hours=DEFAULT_HOURS)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)
    pseudo = person["pseudo_id"]
    pending = [("pending", expires, t_send, None, None, None, None)]
    enrollment = [("student", "pending", t0, None, None, None)]
    assert _invite(sql, code) == pending
    assert _enrollment(sql, person, offering) == enrollment
    assert sql.execute("SELECT count(*) FROM identities").fetchone()[0] == 101

    clock.advance(STEP)
    t1 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)

    # Código desconhecido: o fluxo de equipe.
    stranger = make_user(701)
    unknown = stdlib_secrets.token_urlsafe(16)
    uid, _ = _command(bot_api, sql, bot, stranger, f"/start {unknown}", outcome="team_refused_invalid", at=t1)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
    assert _events_after(client, cursor) == [_refused(offering, t1)]
    assert _invite(sql, code) == pending

    # `/aceito` sem termo apresentado no chat: `HELP` ao chat do update.
    lost = make_user(702)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, lost, "/aceito", outcome="help", at=t1)
    _replies(bot_api, sql, out_cursor, [(HELP, "sent", None, bot, None, uid)])
    assert _sent_texts(bot_api, bot, lost) == [HELP]
    assert _invite(sql, code) == pending and _endpoints(sql) == []

    # `/start <código>`: o termo, uma vez, ao chat do update.
    sender = make_user(703)
    out_cursor = _last_outbox(sql)
    pushed_at = monotonic()
    start_uid = bot_api.push_text(bot, sender, f"/start {code}", bot_command=True)
    sent = bot_api.wait_for(lambda: bot_api.sent_messages(bot, chat_id=sender.user_id), timeout=DELIVERY_DEADLINE_SECONDS)
    call = next(c for c in bot_api.calls("sendMessage", bot_id=bot) if c.seq == sent[0].call_seq)
    assert call.started - pushed_at <= DELIVERY_DEADLINE_SECONDS
    start_row = _processed(bot_api, sql, bot, start_uid)
    assert start_row[1:] == _expected_row(sender, f"/start {code}", "student_terms_presented", t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, start_uid)])
    assert _sent_texts(bot_api, bot, sender) == [TERMS]
    presented = [("pending", expires, t_send, None, None, start_row[0], 1)]
    assert _invite(sql, code) == presented
    assert _enrollment(sql, person, offering) == enrollment
    assert _endpoints(sql) == []
    assert _events_after(client, cursor) == [_refused(offering, t1)]

    # Fora de lugar: `/aceito@<outro bot>` no privado e `/aceito` em grupo → `not_handled`.
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, f"/aceito@{OTHER_BOT_USERNAME}", outcome="not_handled", at=t1)
    _command(bot_api, sql, bot, sender, "/aceito", outcome="not_handled", at=t1, chat=GROUP_CHAT)
    assert _outbox_after(sql, out_cursor) == []
    assert bot_api.sent_messages(bot, chat_id=GROUP_CHAT["id"]) == []
    assert _invite(sql, code) == presented
    assert _enrollment(sql, person, offering) == enrollment
    assert _endpoints(sql) == []
    assert _events_after(client, cursor) == [_refused(offering, t1)]

    # `/aceito`: o vínculo, e a boas-vindas uma vez ao endereço.
    clock.advance(STEP)
    t2 = clock.now()
    out_cursor = _last_outbox(sql)
    pushed_at = monotonic()
    accept_uid = bot_api.push_text(bot, sender, "/aceito", bot_command=True)
    sent = bot_api.wait_for(lambda: bot_api.sent_messages(bot, chat_id=sender.user_id)[1:],
                            timeout=DELIVERY_DEADLINE_SECONDS)
    call = next(c for c in bot_api.calls("sendMessage", bot_id=bot) if c.seq == sent[0].call_seq)
    assert call.started - pushed_at <= DELIVERY_DEADLINE_SECONDS
    accept_row = _processed(bot_api, sql, bot, accept_uid)
    assert accept_row[1:] == _expected_row(sender, "/aceito", "student_joined", t2)
    welcome = _replies(bot_api, sql, out_cursor, [(WELCOME_STUDENT, "sent", pseudo, bot, sender.user_id, None)])
    assert _sent_texts(bot_api, bot, sender) == [TERMS, WELCOME_STUDENT]
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)
            if c.params.get("chat_id") == sender.user_id] == [
        ({"chat_id": sender.user_id, "text": TERMS}, 200),
        ({"chat_id": sender.user_id, "text": WELCOME_STUDENT}, 200)]
    used = [("used", expires, t_send, t2, None, start_row[0], 1)]
    linked = [("student", "active", t0, t2, 1, t2)]
    endpoints = [(pseudo, bot, "telegram", sender.user_id, "active", t2, t2)]
    assert _invite(sql, code) == used
    assert _enrollment(sql, person, offering) == linked
    assert _endpoints(sql) == endpoints
    assert _events_after(client, cursor) == [_refused(offering, t1), _joined(offering, person, t2)]
    for row in (start_row, accept_row):
        assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}")]

    # Os updates reentregues não mudam nada.
    for uid, row in ((start_uid, start_row), (accept_uid, accept_row)):
        _redeliver_and_settle(bot_api, bot, uid)
        assert _updates(sql, bot, uid) == [row]
        assert _update_tasks(bot_api, sql, row[0]) == [("telegram.update", "done", f"telegram.update:{row[0]}")]
    assert _outbox_after(sql, out_cursor) == welcome
    assert _sent_texts(bot_api, bot, sender) == [TERMS, WELCOME_STUDENT]
    assert _invite(sql, code) == used
    assert _enrollment(sql, person, offering) == linked
    assert _endpoints(sql) == endpoints
    assert _events_after(client, cursor) == [_refused(offering, t1), _joined(offering, person, t2)]
    assert channel.calls == []


def test_aceito_ignores_case_and_argument_and_accepts_this_bot_username(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O reconhecimento dos comandos (os nomes `aceito`, `recuso` e `convite` sem diferenciar
    maiúsculas; `/cmd` ou `/cmd@<este bot>`, o username também sem diferenciar maiúsculas;
    o argumento de `/aceito` é ignorado): `/Aceito`, `/ACEITO@<USERNAME DO BOT>` e `/aceito <texto>`,
    cada um com a entidade `bot_command`, vinculam três alunos (`student_joined`, `WELCOME_STUDENT` ao
    endereço, `student.joined`), com o texto e o `command` gravados como chegaram."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-caixa")
    students = [_student(11), _student(12), _student(13)]
    _import(client, offering, students)
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=3, total=3)
    codes = [_mail_code(smtp, s, expires=t0 + timedelta(hours=DEFAULT_HOURS), window=window)
             for s in students]
    clock.advance(STEP)
    t1 = clock.now()
    cursor = _last_seq(sql)

    variants = ("/Aceito", f"/ACEITO@{BOT_USERNAME.upper()}", "/aceito com um argumento ignorado")
    expected_events: list[dict] = []
    expected_endpoints: list[tuple] = []
    for index, (student, code, text) in enumerate(zip(students, codes, variants, strict=True)):
        person = _person(sql, student)
        sender = make_user(711 + index)
        out_cursor = _last_outbox(sql)
        start_uid, start_row = _command(bot_api, sql, bot, sender, f"/start {code}",
                                        outcome="student_terms_presented", at=t1)
        _command(bot_api, sql, bot, sender, text, outcome="student_joined", at=t1)
        _replies(bot_api, sql, out_cursor, [
            (TERMS, "sent", None, bot, None, start_uid),
            (WELCOME_STUDENT, "sent", person["pseudo_id"], bot, sender.user_id, None)])
        assert _sent_texts(bot_api, bot, sender) == [TERMS, WELCOME_STUDENT], text
        assert _invite(sql, code) == [("used", t0 + timedelta(hours=DEFAULT_HOURS), t0, t1, None, start_row, 1)]
        assert _enrollment(sql, person, offering) == [("student", "active", t0, t1, 1, t1)], text
        expected_endpoints.append((person["pseudo_id"], bot, "telegram", sender.user_id, "active", t1, t1))
        expected_events.append(_joined(offering, person, t1))
        assert _endpoints(sql) == expected_endpoints, text
        assert _events_after(client, cursor) == expected_events, text
    assert channel.calls == []


def test_start_in_an_offering_that_is_not_active_is_link_invalid(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Oferecimento encerrado: o `/start` e o `/aceito` dão link inválido
    (o oferecimento não ativo é conferido antes do convite usado).

    Com três alunos (A vinculado, B com o convite só enviado, C com o termo apresentado) e uma pessoa
    da equipe com link pendente, o oferecimento é encerrado (o bot continua consultado):
    - o link de A no próprio chat (o endereço vigente dele) → `LINK_INVALID` ao chat do update,
      `student_refused_invalid`, e não `ALREADY_LINKED`; o endereço de A continua `active`;
    - o link de B → `LINK_INVALID`; o convite continua `pending`, sem apresentação;
    - o `/aceito` de C → `LINK_INVALID`; o convite continua `pending` e apresentado;
    - nenhum evento do aluno; nada muda;
    - o vínculo da equipe continua funcionando: o `/start` do link de equipe → `team_joined`,
      `WELCOME_TEAM` e `team.joined`."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-encerrado")
    a, b, c = _student(21), _student(22), _student(23)
    _import(client, offering, [a, b, c])
    teammate = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Encerrado", ra="5700000324")
    team_code = link_code(create_team_link(client, ADMIN_TOKEN, offering["code"], teammate["alias"])["link"])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=3, total=3)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code_a, code_b, code_c = (_mail_code(smtp, s, expires=expires, window=window) for s in (a, b, c))
    person_a, person_b, person_c = _person(sql, a), _person(sql, b), _person(sql, c)

    clock.advance(STEP)
    t1 = clock.now()
    chat_a, chat_b, chat_c, chat_team = make_user(721), make_user(722), make_user(723), make_user(724)
    out_cursor = _last_outbox(sql)
    uid_a, row_a = _command(bot_api, sql, bot, chat_a, f"/start {code_a}", outcome="student_terms_presented",
                            at=t1)
    _command(bot_api, sql, bot, chat_a, "/aceito", outcome="student_joined", at=t1)
    uid_c, row_c = _command(bot_api, sql, bot, chat_c, f"/start {code_c}", outcome="student_terms_presented",
                            at=t1)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid_a),
        (WELCOME_STUDENT, "sent", person_a["pseudo_id"], bot, chat_a.user_id, None),
        (TERMS, "sent", None, bot, None, uid_c)])

    _close(client, offering)
    clock.advance(STEP)
    t2 = clock.now()
    invites = {"a": [("used", expires, t0, t1, None, row_a, 1)],
               "b": [("pending", expires, t0, None, None, None, None)],
               "c": [("pending", expires, t0, None, None, row_c, 1)]}
    enrollments = {"a": [("student", "active", t0, t1, 1, t1)],
                   "b": [("student", "pending", t0, None, None, None)],
                   "c": [("student", "pending", t0, None, None, None)]}
    endpoints = [(person_a["pseudo_id"], bot, "telegram", chat_a.user_id, "active", t1, t1)]
    cursor = _last_seq(sql)

    for label, sender, text in (("a", chat_a, f"/start {code_a}"), ("b", chat_b, f"/start {code_b}"),
                                ("c", chat_c, "/aceito")):
        out_cursor = _last_outbox(sql)
        uid, _ = _command(bot_api, sql, bot, sender, text, outcome="student_refused_invalid", at=t2)
        _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
        assert _invite(sql, code_a) == invites["a"], label
        assert _invite(sql, code_b) == invites["b"], label
        assert _invite(sql, code_c) == invites["c"], label
        assert _enrollment(sql, person_a, offering) == enrollments["a"], label
        assert _enrollment(sql, person_b, offering) == enrollments["b"], label
        assert _enrollment(sql, person_c, offering) == enrollments["c"], label
        assert _endpoints(sql) == endpoints, label
        assert _events_after(client, cursor) == [], label
    assert _sent_texts(bot_api, bot, chat_a) == [TERMS, WELCOME_STUDENT, LINK_INVALID]
    assert _sent_texts(bot_api, bot, chat_b) == [LINK_INVALID]
    assert _sent_texts(bot_api, bot, chat_c) == [TERMS, LINK_INVALID]

    # O vínculo da equipe continua funcionando.
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, chat_team, f"/start {team_code}", outcome="team_joined", at=t2)
    _replies(bot_api, sql, out_cursor, [(WELCOME_TEAM, "sent", teammate["pseudo_id"], bot, chat_team.user_id,
                                         None)])
    assert _endpoints(sql) == [*endpoints,
                               (teammate["pseudo_id"], bot, "telegram", chat_team.user_id, "active", t2, t2)]
    assert _events_after(client, cursor) == [
        _event("team.joined", t2, offering_id=offering["offering_id"], person=teammate["pseudo_id"],
               endpoint_replaced=False)]
    assert channel.calls == []


def test_chat_of_another_person_revokes_the_invite_and_the_revoked_link_is_invalid(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Chat de outra pessoa; o `/start` e o `/aceito` com o link de outra pessoa ou já revogado
    e os eventos.

    A vinculado no chat X; o link de B usado em X (o endereço vigente de outra pessoa no bot) →
    `OTHER_PERSON` ao chat do update, `student_refused_other_person`, o convite de B `revoked` e
    `student.invite_revoked {origin: batch, revoke_reason: refused}` sem ator; os endereços não mudam.
    Depois, o link revogado de B no chat dele → `LINK_INVALID`, sem evento, e o `/aceito` de
    B → `HELP` (sem candidato): é preciso outro link."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-outra-pessoa")
    a, b = _student(31), _student(32)
    _import(client, offering, [a, b])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=2, total=2)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code_a, code_b = (_mail_code(smtp, s, expires=expires, window=window) for s in (a, b))
    person_a, person_b = _person(sql, a), _person(sql, b)

    clock.advance(STEP)
    t1 = clock.now()
    chat_a, chat_b = make_user(731), make_user(732)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, chat_a, f"/start {code_a}", outcome="student_terms_presented", at=t1)
    _command(bot_api, sql, bot, chat_a, "/aceito", outcome="student_joined", at=t1)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid),
        (WELCOME_STUDENT, "sent", person_a["pseudo_id"], bot, chat_a.user_id, None)])
    endpoints = [(person_a["pseudo_id"], bot, "telegram", chat_a.user_id, "active", t1, t1)]
    assert _endpoints(sql) == endpoints

    clock.advance(STEP)
    t2 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, chat_a, f"/start {code_b}", outcome="student_refused_other_person",
                      at=t2)
    _replies(bot_api, sql, out_cursor, [(OTHER_PERSON, "sent", None, bot, None, uid)])
    revoked = [("revoked", expires, t0, None, t2, None, None)]
    assert _invite(sql, code_b) == revoked
    assert _enrollment(sql, person_b, offering) == [("student", "pending", t0, None, None, None)]
    assert _endpoints(sql) == endpoints
    assert _events_after(client, cursor) == [_revoked(offering, person_b, t2, "refused")]
    assert _sent_texts(bot_api, bot, chat_a) == [TERMS, WELCOME_STUDENT, OTHER_PERSON]

    cursor = _last_seq(sql)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, chat_b, f"/start {code_b}", outcome="student_refused_invalid", at=t2)
    uid_help, _ = _command(bot_api, sql, bot, chat_b, "/aceito", outcome="help", at=t2)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid),
                                        (HELP, "sent", None, bot, None, uid_help)])
    assert _sent_texts(bot_api, bot, chat_b) == [LINK_INVALID, HELP]
    assert _invite(sql, code_b) == revoked
    assert _endpoints(sql) == endpoints
    assert _events_after(client, cursor) == []
    assert channel.calls == []


def test_used_link_answers_already_linked_only_from_the_current_endpoint(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O `/start` com convite `used` (se o chat é o endereço
    vigente → `ALREADY_LINKED` ao endereço e reativação do endereço `blocked`; se o chat não é o
    endereço vigente → `LINK_INVALID`; um convite usado nunca é tratado como vencido).

    - O aluno aceita num chat que bloqueou o bot: `student_joined`, a boas-vindas `failed`, o endereço
      nasce `blocked`, com `student.joined`, `outbox.failed {error: chat_unreachable}` e
      `endpoint.blocked`;
    - desbloqueado, o mesmo link no mesmo chat → `student_already_linked`, o endereço volta a `active`
      (`changed_at` = agora), `ALREADY_LINKED` ao endereço, sem evento; nada mais muda;
    - o mesmo link noutro chat → `LINK_INVALID` ao chat do update, `student_refused_invalid`, sem
      evento nem mudança;
    - depois do vencimento do convite (1 hora), o link no chat vigente continua `student_already_linked`."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-ja-vinculado")
    student = _student(41)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1, valid_hours=1)
    expires = t0 + timedelta(hours=1)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)
    pseudo = person["pseudo_id"]

    # 1) Aceite num chat que bloqueou o bot: o endereço nasce `blocked`.
    clock.advance(STEP)
    t1 = clock.now()
    sender = make_user(741)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    start_uid, start_row = _command(bot_api, sql, bot, sender, f"/start {code}",
                                    outcome="student_terms_presented", at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, start_uid)])
    bot_api.block_chat(bot, sender)
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, "/aceito", outcome="student_joined", at=t1)
    rows = _replies(bot_api, sql, out_cursor, [(WELCOME_STUDENT, "failed", pseudo, bot, sender.user_id, None)])
    assert _endpoints(sql) == [(pseudo, bot, "telegram", sender.user_id, "blocked", t1, t1)]
    assert _events_after(client, cursor) == [
        _joined(offering, person, t1),
        _event("outbox.failed", t1, message_id=rows[0][0], recipient=pseudo, attempts=1, error="chat_unreachable"),
        _event("endpoint.blocked", t1, offering_id=offering["offering_id"], person=pseudo, channel="telegram")]
    used = [("used", expires, t0, t1, None, start_row, 1)]
    linked = [("student", "active", t0, t1, 1, t1)]
    assert _invite(sql, code) == used
    assert _enrollment(sql, person, offering) == linked
    assert _sent_texts(bot_api, bot, sender) == [TERMS]

    # 2) Desbloqueado, o mesmo link no mesmo chat reativa o endereço (`student_already_linked`).
    clock.advance(STEP)
    t2 = clock.now()
    bot_api.unblock_chat(bot, sender)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_already_linked", at=t2)
    _replies(bot_api, sql, out_cursor, [(ALREADY_LINKED, "sent", pseudo, bot, sender.user_id, None)])
    endpoints = [(pseudo, bot, "telegram", sender.user_id, "active", t1, t2)]
    assert _endpoints(sql) == endpoints
    assert _invite(sql, code) == used
    assert _enrollment(sql, person, offering) == linked
    assert _sent_texts(bot_api, bot, sender) == [TERMS, ALREADY_LINKED]
    assert _events_after(client, cursor) == []

    # 3) O mesmo link noutro chat: não é o endereço vigente da pessoa → `LINK_INVALID`.
    stranger = make_user(742)
    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, stranger, f"/start {code}", outcome="student_refused_invalid", at=t2)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
    assert _sent_texts(bot_api, bot, stranger) == [LINK_INVALID]
    assert _endpoints(sql) == endpoints
    assert _invite(sql, code) == used
    assert _events_after(client, cursor) == []

    # 4) Depois do vencimento, o convite usado continua reconhecido no chat vigente.
    clock.set(t0 + timedelta(hours=2))
    t3 = clock.now()
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_already_linked", at=t3)
    _replies(bot_api, sql, out_cursor, [(ALREADY_LINKED, "sent", pseudo, bot, sender.user_id, None)])
    assert _endpoints(sql) == endpoints
    assert _invite(sql, code) == used
    assert _enrollment(sql, person, offering) == linked
    assert _sent_texts(bot_api, bot, sender) == [TERMS, ALREADY_LINKED, ALREADY_LINKED]
    assert _events_after(client, cursor) == []
    assert channel.calls == []


def test_expired_invite_is_link_invalid_at_start_and_at_aceito(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Vencimento avaliado no processamento, pelo relógio do sistema;
    o convite vencido no `/start` e no `/aceito`.

    Convites de 1 hora. No instante exato do vencimento dos convites de A e B (a fronteira é estrita:
    vale só com `agora < expires_at`):
    - o link de A, nunca apresentado → `LINK_INVALID`, `student_refused_invalid`;
    - o `/aceito` de B, com o termo apresentado antes do vencimento → `LINK_INVALID`, sem vincular; o
      `/start` de novo, no mesmo chat → `LINK_INVALID`;
    - os convites continuam `pending` (o vencido é calculado) e como estavam; nenhum endereço nem evento;
    - o convite de C, enviado 15 s depois, ainda vale no mesmo instante: o termo e o vínculo."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-vencido")
    a, b, c = _student(51), _student(52), _student(53)
    _import(client, offering, [a, b])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=2, total=2, valid_hours=1)
    expires = t0 + timedelta(hours=1)
    code_a, code_b = (_mail_code(smtp, s, expires=expires, window=window) for s in (a, b))
    person_a, person_b = _person(sql, a), _person(sql, b)

    clock.advance(STEP)
    t1 = clock.now()
    chat_a, chat_b, chat_c = make_user(751), make_user(752), make_user(753)
    out_cursor = _last_outbox(sql)
    uid_b, row_b = _command(bot_api, sql, bot, chat_b, f"/start {code_b}", outcome="student_terms_presented",
                            at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid_b)])
    _import(client, offering, [c])
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=3, valid_hours=1)
    code_c = _mail_code(smtp, c, expires=t1 + timedelta(hours=1), window=window)
    person_c = _person(sql, c)

    clock.set(expires)
    at = clock.now()
    cursor = _last_seq(sql)
    invite_a = [("pending", expires, t0, None, None, None, None)]
    invite_b = [("pending", expires, t0, None, None, row_b, 1)]
    for sender, text in ((chat_a, f"/start {code_a}"), (chat_b, "/aceito"), (chat_b, f"/start {code_b}")):
        out_cursor = _last_outbox(sql)
        uid, _ = _command(bot_api, sql, bot, sender, text, outcome="student_refused_invalid", at=at)
        _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid)])
        assert _invite(sql, code_a) == invite_a
        assert _invite(sql, code_b) == invite_b
        assert _endpoints(sql) == []
        assert _events_after(client, cursor) == []
    assert _enrollment(sql, person_a, offering) == [("student", "pending", t0, None, None, None)]
    assert _enrollment(sql, person_b, offering) == [("student", "pending", t0, None, None, None)]
    assert _sent_texts(bot_api, bot, chat_a) == [LINK_INVALID]
    assert _sent_texts(bot_api, bot, chat_b) == [TERMS, LINK_INVALID, LINK_INVALID]

    out_cursor = _last_outbox(sql)
    uid_c, row_c = _command(bot_api, sql, bot, chat_c, f"/start {code_c}", outcome="student_terms_presented",
                            at=at)
    _command(bot_api, sql, bot, chat_c, "/aceito", outcome="student_joined", at=at)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid_c),
        (WELCOME_STUDENT, "sent", person_c["pseudo_id"], bot, chat_c.user_id, None)])
    assert _invite(sql, code_c) == [("used", t1 + timedelta(hours=1), t1, at, None, row_c, 1)]
    assert _enrollment(sql, person_c, offering) == [("student", "active", t1, at, 1, at)]
    assert _endpoints(sql) == [(person_c["pseudo_id"], bot, "telegram", chat_c.user_id, "active", at, at)]
    assert _events_after(client, cursor) == [_joined(offering, person_c, at)]
    assert channel.calls == []


def test_presented_invite_is_bound_to_its_chat(client: Any, clock: Any, sql: Any, bot_api: Any,
                                              bot_worker_thread: Any, settings: Any, smtp: Any,
                                              mail_channel: Any) -> None:
    """Código amarrado ao chat da apresentação;
    o chat da amarração é o do `presented_update_id`; o `/start` noutro chat e no mesmo chat e o
    candidato do `/aceito`.

    O termo apresentado no chat X e apresentado de novo no mesmo chat (a apresentação passa a ser o
    update novo, `TERMS` de novo); o mesmo link noutro chat → `LINK_INVALID`, `student_refused_invalid`,
    e o convite continua amarrado a X; o `/aceito` desse outro chat não tem candidato → `HELP`; o
    `/aceito` em X vincula no chat X."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-amarrado")
    student = _student(61)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)

    clock.advance(STEP)
    t1 = clock.now()
    chat_x, chat_y = make_user(761), make_user(762)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid1, row1 = _command(bot_api, sql, bot, chat_x, f"/start {code}", outcome="student_terms_presented", at=t1)
    assert _invite(sql, code) == [("pending", expires, t0, None, None, row1, 1)]
    clock.advance(STEP)
    t2 = clock.now()
    uid2, row2 = _command(bot_api, sql, bot, chat_x, f"/start {code}", outcome="student_terms_presented", at=t2)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid1),
                                        (TERMS, "sent", None, bot, None, uid2)])
    bound = [("pending", expires, t0, None, None, row2, 1)]
    assert _invite(sql, code) == bound

    out_cursor = _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, chat_y, f"/start {code}", outcome="student_refused_invalid", at=t2)
    uid_help, _ = _command(bot_api, sql, bot, chat_y, "/aceito", outcome="help", at=t2)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid),
                                        (HELP, "sent", None, bot, None, uid_help)])
    assert _invite(sql, code) == bound
    assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)]
    assert _endpoints(sql) == []
    assert _events_after(client, cursor) == []

    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, chat_x, "/aceito", outcome="student_joined", at=t2)
    _replies(bot_api, sql, out_cursor, [(WELCOME_STUDENT, "sent", person["pseudo_id"], bot, chat_x.user_id,
                                         None)])
    assert _invite(sql, code) == [("used", expires, t0, t2, None, row2, 1)]
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", chat_x.user_id, "active", t2, t2)]
    assert _sent_texts(bot_api, bot, chat_x) == [TERMS, TERMS, WELCOME_STUDENT]
    assert _sent_texts(bot_api, bot, chat_y) == [LINK_INVALID, HELP]
    assert _events_after(client, cursor) == [_joined(offering, person, t2)]
    assert channel.calls == []


def test_changed_terms_version_presents_the_terms_again(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                       bot_worker_thread: Any, settings: Any, smtp: Any,
                                                       mail_channel: Any, monkeypatch: Any) -> None:
    """Mudança na versão do termo: se a versão vigente do termo não for a
    apresentada, o `/aceito` apresenta o termo de novo, sem vincular
    (as tabelas e o texto do termo, com `TERMS_VERSION` lido na hora da chamada).

    Termo da versão 1 apresentado no chat X; a versão vigente passa a 2 (`monkeypatch` de
    `botmessages.TERMS_VERSION`); o `/aceito` → `TERMS` de novo, com o texto da versão 2 ("versão 2" na
    primeira linha), ao chat do update, `student_terms_presented`, o convite com `presented_update_id` =
    o update do `/aceito` e `presented_terms_version = 2`, sem vincular e sem evento; o `/aceito`
    seguinte vincula com `terms_version = 2` na matrícula e no `student.joined`."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-versao")
    student = _student(71)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)

    clock.advance(STEP)
    t1 = clock.now()
    sender = make_user(771)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid1, row1 = _command(bot_api, sql, bot, sender, f"/start {code}", outcome="student_terms_presented", at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid1)])
    assert _invite(sql, code) == [("pending", expires, t0, None, None, row1, 1)]

    monkeypatch.setattr(botmessages, "TERMS_VERSION", 2)
    terms_v2 = _terms(2)
    assert terms_v2.splitlines()[0] == f"Termo de uso do bot do oferecimento {COURSE} ({TERM}), versão 2"
    clock.advance(STEP)
    t2 = clock.now()
    out_cursor = _last_outbox(sql)
    uid2, row2 = _command(bot_api, sql, bot, sender, "/aceito", outcome="student_terms_presented", at=t2)
    _replies(bot_api, sql, out_cursor, [(terms_v2, "sent", None, bot, None, uid2)])
    assert _invite(sql, code) == [("pending", expires, t0, None, None, row2, 2)]
    assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)]
    assert _endpoints(sql) == []
    assert _events_after(client, cursor) == []

    clock.advance(STEP)
    t3 = clock.now()
    out_cursor = _last_outbox(sql)
    _command(bot_api, sql, bot, sender, "/aceito", outcome="student_joined", at=t3)
    _replies(bot_api, sql, out_cursor, [(WELCOME_STUDENT, "sent", person["pseudo_id"], bot, sender.user_id,
                                         None)])
    assert _invite(sql, code) == [("used", expires, t0, t3, None, row2, 2)]
    assert _enrollment(sql, person, offering) == [("student", "active", t0, t3, 2, t3)]
    assert _endpoints(sql) == [(person["pseudo_id"], bot, "telegram", sender.user_id, "active", t3, t3)]
    assert _sent_texts(bot_api, bot, sender) == [TERMS, terms_v2, WELCOME_STUDENT]
    assert _events_after(client, cursor) == [_joined(offering, person, t3, version=2)]
    assert channel.calls == []


def test_aceito_in_a_chat_that_became_another_persons_endpoint_is_other_person(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O `/aceito` num chat que passou a ser o endereço de outra pessoa,
    e depois o link revogado no `/start`.

    Termo apresentado a um aluno no chat X; um membro da equipe se vincula no mesmo chat pelo link de
    equipe (`team_joined`, `WELCOME_TEAM`, `team.joined`); o `/aceito` em X → `OTHER_PERSON` ao chat
    do update, `student_refused_other_person`, o convite do aluno `revoked` e
    `student.invite_revoked {origin: batch, revoke_reason: refused}`. Depois, sem candidato, o
    `/aceito` → `HELP`, e o link revogado → `LINK_INVALID`. A matrícula do aluno continua `student`
    `pending`, a do membro `team` `active` (a mesma pessoa nunca tem os dois papéis, e só o convite de
    equipe vincula como equipe)."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-equipe-no-chat")
    student = _student(81)
    _import(client, offering, [student])
    teammate = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe No Chat", ra="5700000382")
    team_code = link_code(create_team_link(client, ADMIN_TOKEN, offering["code"], teammate["alias"])["link"])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1)
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code = _mail_code(smtp, student, expires=expires, window=window)
    person = _person(sql, student)

    clock.advance(STEP)
    t1 = clock.now()
    chat = make_user(781)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid, row = _command(bot_api, sql, bot, chat, f"/start {code}", outcome="student_terms_presented", at=t1)
    _command(bot_api, sql, bot, chat, f"/start {team_code}", outcome="team_joined", at=t1)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid),
        (WELCOME_TEAM, "sent", teammate["pseudo_id"], bot, chat.user_id, None)])
    assert _events_after(client, cursor) == [
        _event("team.joined", t1, offering_id=offering["offering_id"], person=teammate["pseudo_id"],
               endpoint_replaced=False)]
    endpoints = [(teammate["pseudo_id"], bot, "telegram", chat.user_id, "active", t1, t1)]
    assert _endpoints(sql) == endpoints
    assert _invite(sql, code) == [("pending", expires, t0, None, None, row, 1)]

    clock.advance(STEP)
    t2 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid, _ = _command(bot_api, sql, bot, chat, "/aceito", outcome="student_refused_other_person", at=t2)
    _replies(bot_api, sql, out_cursor, [(OTHER_PERSON, "sent", None, bot, None, uid)])
    revoked = [("revoked", expires, t0, None, t2, row, 1)]
    assert _invite(sql, code) == revoked
    assert _events_after(client, cursor) == [_revoked(offering, person, t2, "refused")]

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid_help, _ = _command(bot_api, sql, bot, chat, "/aceito", outcome="help", at=t2)
    uid, _ = _command(bot_api, sql, bot, chat, f"/start {code}", outcome="student_refused_invalid", at=t2)
    _replies(bot_api, sql, out_cursor, [(HELP, "sent", None, bot, None, uid_help),
                                        (LINK_INVALID, "sent", None, bot, None, uid)])
    assert _invite(sql, code) == revoked
    assert _enrollment(sql, person, offering) == [("student", "pending", t0, None, None, None)]
    assert _enrollment(sql, teammate, offering) == [("team", "active", t0, t1, None, None)]
    assert _endpoints(sql) == endpoints
    assert _sent_texts(bot_api, bot, chat) == [TERMS, WELCOME_TEAM, OTHER_PERSON, HELP, LINK_INVALID]
    assert _events_after(client, cursor) == []
    assert channel.calls == []


def test_two_invites_presented_in_the_same_chat_the_last_one_counts(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Dois links no mesmo chat: o candidato é o convite
    de maior `presented_update_id` deste bot e deste chat; no `/aceito` que vincula, os demais convites de
    aluno `pending` **pelo estado gravado**, inclusive os vencidos, apresentados neste chat e neste bot,
    de outras pessoas, são revogados com `student.invite_revoked {refused}`, em ordem de `invites.id`.

    No chat X do bot do oferecimento: o termo do convite de A (de 1 hora), depois o de C; no bot de
    outro oferecimento, o de D; o convite de A vence; por último, o termo de B. O `/aceito` em X vincula
    B (o último apresentado); os convites de A (vencido, ainda `pending` no banco) e de C ficam `revoked`,
    com um `student.invite_revoked {origin: batch, revoke_reason: refused}` cada, na ordem de
    `invites.id` (A, do primeiro lote, antes de C); o de D, apresentado em X noutro bot, não muda, e o
    `/aceito` em X no outro bot vincula D. Os links revogados de A e de C dão `LINK_INVALID` depois."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-dois-links")
    second, bot2 = _offering(client, bot_api, "mc102-aluno-outro-bot", SECOND_BOT_USERNAME, term=OTHER_TERM)
    a, b, c, d = _student(91), _student(92), _student(93), _student(94)
    _import(client, offering, [a])
    _import(client, second, [d])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    _wait_polling(bot_api, bot2)
    before = _real_now()
    _batch(client, clock, offering, eligible=1, valid_hours=1)
    _batch(client, clock, second, eligible=1)
    _wait_sent(bot_api, sql, smtp, 2)
    first_window = (before, _real_now())
    code_a = _mail_code(smtp, a, expires=t0 + timedelta(hours=1), window=first_window)
    code_d = _mail_code(smtp, d, expires=t0 + timedelta(hours=DEFAULT_HOURS), window=first_window,
                        username=SECOND_BOT_USERNAME, term=OTHER_TERM)
    clock.advance(STEP)
    t1 = clock.now()
    _import(client, offering, [b, c])
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=2, total=4)
    code_b, code_c = (_mail_code(smtp, s, expires=t1 + timedelta(hours=DEFAULT_HOURS), window=window)
                      for s in (b, c))
    person_a, person_b, person_c, person_d = (_person(sql, s) for s in (a, b, c, d))

    clock.advance(STEP)
    t2 = clock.now()
    chat = make_user(791)
    out_cursor = _last_outbox(sql)
    uid_a, row_a = _command(bot_api, sql, bot, chat, f"/start {code_a}", outcome="student_terms_presented",
                            at=t2)
    uid_c, row_c = _command(bot_api, sql, bot, chat, f"/start {code_c}", outcome="student_terms_presented",
                            at=t2)
    uid_d, row_d = _command(bot_api, sql, bot2, chat, f"/start {code_d}", outcome="student_terms_presented",
                            at=t2)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid_a),
                                        (TERMS, "sent", None, bot, None, uid_c),
                                        (_terms(1, term=OTHER_TERM), "sent", None, bot2, None, uid_d)])
    assert _invite(sql, code_a) == [("pending", t0 + timedelta(hours=1), t0, None, None, row_a, 1)]
    clock.set(t0 + timedelta(hours=1))
    t3 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid_b, row_b = _command(bot_api, sql, bot, chat, f"/start {code_b}", outcome="student_terms_presented", at=t3)
    _command(bot_api, sql, bot, chat, "/aceito", outcome="student_joined", at=t3)
    _replies(bot_api, sql, out_cursor, [
        (TERMS, "sent", None, bot, None, uid_b),
        (WELCOME_STUDENT, "sent", person_b["pseudo_id"], bot, chat.user_id, None)])

    expires_c = t1 + timedelta(hours=DEFAULT_HOURS)
    assert _invite(sql, code_b) == [("used", expires_c, t1, t3, None, row_b, 1)]
    assert _invite(sql, code_a) == [("revoked", t0 + timedelta(hours=1), t0, None, t3, row_a, 1)]
    assert _invite(sql, code_c) == [("revoked", expires_c, t1, None, t3, row_c, 1)]
    presented_d = [("pending", t0 + timedelta(hours=DEFAULT_HOURS), t0, None, None, row_d, 1)]
    assert _invite(sql, code_d) == presented_d
    invite_ids = dict(sql.execute("SELECT code_sha256, id FROM invites").fetchall())
    assert invite_ids[_sha256(code_a)] < invite_ids[_sha256(code_c)]
    events = _events_after(client, cursor)
    # A regra fixa a ordem das revogações entre si (por `invites.id`), não a posição delas em
    # relação ao `student.joined`: cada tipo é conferido exatamente e na ordem, e o total é exato.
    assert _only(events, "student.joined") == [_joined(offering, person_b, t3)]
    assert _only(events, "student.invite_revoked") == [_revoked(offering, person_a, t3, "refused"),
                                                       _revoked(offering, person_c, t3, "refused")]
    assert len(events) == 3
    assert _enrollment(sql, person_a, offering) == [("student", "pending", t0, None, None, None)]
    assert _enrollment(sql, person_c, offering) == [("student", "pending", t1, None, None, None)]
    assert _enrollment(sql, person_d, second) == [("student", "pending", t0, None, None, None)]
    endpoints = [(person_b["pseudo_id"], bot, "telegram", chat.user_id, "active", t3, t3)]
    assert _endpoints(sql) == endpoints

    # No outro bot, o convite de D continua apresentado em X, e o `/aceito` de lá o vincula.
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    _command(bot_api, sql, bot2, chat, "/aceito", outcome="student_joined", at=t3)
    _replies(bot_api, sql, out_cursor, [(_fill(WELCOME_STUDENT_TEMPLATE, term=OTHER_TERM), "sent",
                                         person_d["pseudo_id"], bot2, chat.user_id, None)])
    assert _invite(sql, code_d) == [("used", t0 + timedelta(hours=DEFAULT_HOURS), t0, t3, None, row_d, 1)]
    assert _endpoints(sql) == [*endpoints,
                               (person_d["pseudo_id"], bot2, "telegram", chat.user_id, "active", t3, t3)]
    assert _events_after(client, cursor) == [_joined(second, person_d, t3)]

    # Os links revogados não valem mais.
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    chat_a, chat_c = make_user(792), make_user(793)
    uid_a, _ = _command(bot_api, sql, bot, chat_a, f"/start {code_a}", outcome="student_refused_invalid", at=t3)
    uid_c, _ = _command(bot_api, sql, bot, chat_c, f"/start {code_c}", outcome="student_refused_invalid", at=t3)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid_a),
                                        (LINK_INVALID, "sent", None, bot, None, uid_c)])
    assert _events_after(client, cursor) == []
    assert _sent_texts(bot_api, bot, chat) == [TERMS, TERMS, TERMS, WELCOME_STUDENT]
    assert _sent_texts(bot_api, bot2, chat) == [_terms(1, term=OTHER_TERM),
                                                _fill(WELCOME_STUDENT_TEMPLATE, term=OTHER_TERM)]
    assert channel.calls == []


def test_student_code_of_another_offering_follows_the_team_refusal(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """O código de aluno de outro oferecimento segue
    o fluxo de equipe; e os eventos.

    O código de aluno de um oferecimento mandado ao bot de outro → o fluxo de equipe: `team_refused_invalid`,
    `LINK_INVALID` ao chat do update e `team.join_refused {offering_id: <o do bot>, refusal: invalid}`,
    sem nenhuma mudança no convite de origem (continua `pending`, sem apresentação), nos dois sentidos.
    Controle positivo: cada código no bot do próprio oferecimento apresenta o termo daquele
    oferecimento."""
    t0 = clock.now()
    first, bot1 = _offering(client, bot_api, "mc102-aluno-primeiro")
    second, bot2 = _offering(client, bot_api, "mc102-aluno-segundo", SECOND_BOT_USERNAME, term=OTHER_TERM)
    a, d = _student(95), _student(96)
    _import(client, first, [a])
    _import(client, second, [d])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot1)
    _wait_polling(bot_api, bot2)
    before = _real_now()
    _batch(client, clock, first, eligible=1)
    _batch(client, clock, second, eligible=1)
    _wait_sent(bot_api, sql, smtp, 2)
    window = (before, _real_now())
    expires = t0 + timedelta(hours=DEFAULT_HOURS)
    code_a = _mail_code(smtp, a, expires=expires, window=window)
    code_d = _mail_code(smtp, d, expires=expires, window=window, username=SECOND_BOT_USERNAME,
                        term=OTHER_TERM)
    untouched = [("pending", expires, t0, None, None, None, None)]

    clock.advance(STEP)
    t1 = clock.now()
    chat = make_user(795)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid1, _ = _command(bot_api, sql, bot1, chat, f"/start {code_d}", outcome="team_refused_invalid", at=t1)
    uid2, _ = _command(bot_api, sql, bot2, chat, f"/start {code_a}", outcome="team_refused_invalid", at=t1)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot1, None, uid1),
                                        (LINK_INVALID, "sent", None, bot2, None, uid2)])
    assert _events_after(client, cursor) == [_refused(first, t1), _refused(second, t1)]
    assert _invite(sql, code_a) == untouched
    assert _invite(sql, code_d) == untouched
    assert _endpoints(sql) == []

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid1, row1 = _command(bot_api, sql, bot1, chat, f"/start {code_a}", outcome="student_terms_presented", at=t1)
    uid2, row2 = _command(bot_api, sql, bot2, chat, f"/start {code_d}", outcome="student_terms_presented", at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot1, None, uid1),
                                        (_terms(1, term=OTHER_TERM), "sent", None, bot2, None, uid2)])
    assert _invite(sql, code_a) == [("pending", expires, t0, None, None, row1, 1)]
    assert _invite(sql, code_d) == [("pending", expires, t0, None, None, row2, 1)]
    assert _sent_texts(bot_api, bot1, chat) == [LINK_INVALID, TERMS]
    assert _sent_texts(bot_api, bot2, chat) == [LINK_INVALID, _terms(1, term=OTHER_TERM)]
    assert _events_after(client, cursor) == []
    assert channel.calls == []


def test_account_change_with_a_coordination_resend_replaces_the_endpoint(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, smtp: Any,
        mail_channel: Any) -> None:
    """Troca de conta: num aceite
    seguinte mudam só `terms_version` e `terms_accepted_at`; o `activated_at` fica o do primeiro; o
    `/aceito` com o endereço noutro chat e o `/start` com o convite usado. Depende do reenvio
    pela coordenação: verde depois da implementação desse reenvio.

    O aluno vinculado na conta X1 recebe um convite novo pela coordenação (`invites.resend`, resposta
    exata) e o usa na conta X2: `TERMS` e, no `/aceito`, `student_endpoint_replaced`, o endereço antigo
    `inactive`, o novo `active`, `WELCOME_STUDENT` ao endereço novo e
    `student.joined {endpoint_replaced: true}`; a matrícula mantém o `activated_at` do primeiro aceite,
    com o `terms_accepted_at` novo. Depois, o link antigo usado de novo em X1 (que ficou `inactive`) →
    `LINK_INVALID`, o link novo em X1 também, e o link antigo em X2 (o endereço vigente) →
    `ALREADY_LINKED` ao endereço."""
    t0 = clock.now()
    offering, bot = _offering(client, bot_api, "mc102-aluno-troca-conta")
    student = _student(97)
    _import(client, offering, [student])
    channel = _DefaultChannel()
    _start_worker(bot_worker_thread, settings, channel, mail_channel)
    _wait_polling(bot_api, bot)
    window = _send(client, clock, bot_api, sql, smtp, offering, eligible=1, total=1)
    old_code = _mail_code(smtp, student, expires=t0 + timedelta(hours=DEFAULT_HOURS), window=window)
    person = _person(sql, student)
    pseudo = person["pseudo_id"]

    clock.advance(STEP)
    t1 = clock.now()
    old_chat, new_chat = make_user(797), make_user(798)
    out_cursor = _last_outbox(sql)
    uid, old_row = _command(bot_api, sql, bot, old_chat, f"/start {old_code}",
                            outcome="student_terms_presented", at=t1)
    _command(bot_api, sql, bot, old_chat, "/aceito", outcome="student_joined", at=t1)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid),
                                        (WELCOME_STUDENT, "sent", pseudo, bot, old_chat.user_id, None)])
    assert _endpoints(sql) == [(pseudo, bot, "telegram", old_chat.user_id, "active", t1, t1)]
    assert _enrollment(sql, person, offering) == [("student", "active", t0, t1, 1, t1)]

    clock.advance(STEP)
    t2 = clock.now()
    before = _real_now()
    body = resend_invites(client, ADMIN_TOKEN, offering["code"], [person["alias"]])
    assert body == {"offering": _ref(offering), "queued": 1, "valid_hours": DEFAULT_HOURS, "not_students": [],
                    "not_found_count": 0}
    _wait_sent(bot_api, sql, smtp, 2)
    new_code = _mail_code(smtp, student, expires=t2 + timedelta(hours=DEFAULT_HOURS),
                          window=(before, _real_now()))
    assert new_code != old_code

    clock.advance(STEP)
    t3 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid, new_row = _command(bot_api, sql, bot, new_chat, f"/start {new_code}", outcome="student_terms_presented",
                            at=t3)
    _command(bot_api, sql, bot, new_chat, "/aceito", outcome="student_endpoint_replaced", at=t3)
    _replies(bot_api, sql, out_cursor, [(TERMS, "sent", None, bot, None, uid),
                                        (WELCOME_STUDENT, "sent", pseudo, bot, new_chat.user_id, None)])
    endpoints = [(pseudo, bot, "telegram", old_chat.user_id, "inactive", t1, t3),
                 (pseudo, bot, "telegram", new_chat.user_id, "active", t3, t3)]
    assert _endpoints(sql) == endpoints
    linked = [("student", "active", t0, t1, 1, t3)]
    assert _enrollment(sql, person, offering) == linked
    old_used = [("used", t0 + timedelta(hours=DEFAULT_HOURS), t0, t1, None, old_row, 1)]
    new_used = [("used", t2 + timedelta(hours=DEFAULT_HOURS), t2, t3, None, new_row, 1)]
    assert _invite(sql, old_code) == old_used
    assert _invite(sql, new_code) == new_used
    assert _events_after(client, cursor) == [_joined(offering, person, t3, replaced=True)]
    assert _sent_texts(bot_api, bot, new_chat) == [TERMS, WELCOME_STUDENT]

    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid1, _ = _command(bot_api, sql, bot, old_chat, f"/start {old_code}", outcome="student_refused_invalid",
                       at=t3)
    uid2, _ = _command(bot_api, sql, bot, old_chat, f"/start {new_code}", outcome="student_refused_invalid",
                       at=t3)
    _command(bot_api, sql, bot, new_chat, f"/start {old_code}", outcome="student_already_linked", at=t3)
    _replies(bot_api, sql, out_cursor, [(LINK_INVALID, "sent", None, bot, None, uid1),
                                        (LINK_INVALID, "sent", None, bot, None, uid2),
                                        (ALREADY_LINKED, "sent", pseudo, bot, new_chat.user_id, None)])
    assert _sent_texts(bot_api, bot, old_chat) == [TERMS, WELCOME_STUDENT, LINK_INVALID, LINK_INVALID]
    assert _sent_texts(bot_api, bot, new_chat) == [TERMS, WELCOME_STUDENT, ALREADY_LINKED]
    assert _endpoints(sql) == endpoints
    assert _enrollment(sql, person, offering) == linked
    assert _invite(sql, old_code) == old_used
    assert _invite(sql, new_code) == new_used
    assert _events_after(client, cursor) == []
    assert channel.calls == []
