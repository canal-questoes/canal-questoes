"""Conflito de papel: equipe e aluno no mesmo oferecimento (a mesma
pessoa como equipe e como aluno).

Contratos: o erro `role_conflict` (com `fields`
obrigatório `[{person, invalid_value}]`, e a ordem das verificações: em `team.link`, o
`role_conflict` vem depois de `bot_required`); o vínculo no banco e o domínio
(`team.create_link` passa a devolver também `RoleConflict()`); a ordem das verificações de
`team.link` (401 → 404/405 → 400 → 403 → 404 do oferecimento → 404 da pessoa →
`invalid_state` → `bot_required`).

Três funções:
- `test_team_link_for_a_student_of_the_offering_is_role_conflict`: o aluno entra pela importação
  (`students.import`, rota da lista) e o `team.link` dele no mesmo oferecimento dá 409 `role_conflict`
  sem gravar nada. No esqueleto, falha na primeira importação
  (rota ausente, 404).
- `test_team_link_for_a_linked_student_is_role_conflict`: o mesmo conflito com a aluna já vinculada
  (matrícula `active`, pelo fluxo inteiro: reenvio, e-mail pelo SMTP falso, `/start` e `/aceito` pela
  Bot API falsa). No esqueleto, falha na importação (rota ausente, 404), como a anterior.
- `test_team_invite_flow_is_unchanged`: regressão de oferecimentos e bots dentro deste arquivo (o fluxo do link
  de equipe, do `/start` e da ajuda, com o worker montado como no vínculo do aluno, com o canal de verificação).
  Passa já no esqueleto: só usa rotas, tipos de evento e colunas que não dependem do vínculo do aluno.

Convenções: corpo inteiro por `==` e `assert_matches` em toda resposta de sucesso;
horários pelo `FakeClock` e `iso`; ids internos nunca como literal (as linhas do banco são lidas por
junção com `pseudo_id` e `offering_id`); o link e o código de equipe entram em `remember_secret`
antes de qualquer outra conferência. Os tipos de evento aparecem como literais de texto (o
esqueleto ainda não tem os do vínculo do aluno).
"""

from __future__ import annotations

import contextlib
import dataclasses
import email.utils
import hashlib
import json
import re
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    TEAM_LINK_SHAPE,
    activate_bot,
    auth,
    create_offering,
    create_token,
    import_students,
    link_code,
    list_events,
    register_bot,
    register_person,
    resend_invites,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches
from tests.helpers.secrets import remember_secret
from tests.helpers.smtp import SENDER as SMTP_SENDER

ADMIN = auth(ADMIN_TOKEN)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
DEFAULT_HOURS = 168  # validade padrão do link de equipe

# Pseudônimo de formato válido que nunca pertence a alguém.
MISSING_ALIAS = "ffff0000ffff"

# Textos fixos do bot, por extenso.
WELCOME_TEAM = ("Pronto! Você faz parte da equipe do oferecimento Algoritmos e Programação (2031s1). Por "
                "este chat você vai receber os avisos do oferecimento.")
HELP = ("Este é o bot do oferecimento Algoritmos e Programação (2031s1). Para participar, toque no link "
        "de convite que você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação.")
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."

# Tipos de evento que o fluxo de equipe grava, como literais.
TEAM_EVENT_TYPES = ("team.link_created", "team.link_revoked", "team.joined", "team.join_refused",
                    "endpoint.blocked", "outbox.failed", "outbox.reply_failed")

PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, deleteWebhook)
STEP = 15  # salto do relógio com o worker no ar (menor que a posse de 30 s)

# E-mail do convite de aluno, por extenso.
INVITE_SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
INVITE_BODY = (
    "Olá!\n"
    "\n"
    f"Você está na lista da turma do oferecimento {COURSE} ({TERM}). As questões da disciplina chegam por "
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
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.\n")
INVITE_HEADER_NAMES = ("content-transfer-encoding", "content-type", "date", "from", "message-id",
                       "mime-version", "subject", "to")
MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
UTC_DATE_RE = re.compile(r".+ (?:\+0000|-0000|GMT|UT)")


# ---------------------------------------------------------------------------------------------
# Apoio


@contextlib.contextmanager
def _context(label: str) -> Iterator[None]:
    """Prefixa a falha com o caso do laço."""
    try:
        yield
    except AssertionError as exc:
        raise AssertionError(f"[{label}] {exc}") from exc


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot, então nada pode chegar
    aqui (conferido no fim)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _offering_with_bot(client: Any, bot_api: Any, code: str, username: str, *,
                       activate: bool) -> tuple[dict, int]:
    """Oferecimento com bot registrado na Bot API falsa (e ativado, se `activate`)."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=ZONE)
    bot = bot_api.add_bot(username)
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    if activate:
        activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _close(client: Any, ref: str) -> None:
    body = _ok(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")
    assert body["state"] == "closed"


def _archive(client: Any, ref: str) -> None:
    """Arquivamento em duas fases pela API."""
    prepared = _ok(client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN),
                   "offerings.prepare_archive")
    body = _ok(client.post(f"/v1/offerings/{ref}/archive",
                           json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN),
               "offerings.archive")
    assert body["state"] == "archived"


def _import_reused(client: Any, offering: dict, row: dict) -> None:
    """Importa a lista de uma linha só, de uma pessoa já cadastrada com o mesmo RA e o mesmo e-mail
    (reaproveitada): a resposta traz só as contagens exatas."""
    body = import_students(client, ADMIN_TOKEN, offering["code"], [row])
    assert body == {"offering": {"offering_id": offering["offering_id"], "code": offering["code"]},
                    "received": 1, "people_created": 0, "people_reused": 1, "enrollments_created": 1,
                    "already_enrolled": 0}


def _post_link(client: Any, ref: str, body: Any, *, token: str | None = ADMIN_TOKEN) -> Any:
    """`POST /v1/offerings/{ref}/team-links`; um link devolvido entra logo em `remember_secret`."""
    resp = client.post(f"/v1/offerings/{ref}/team-links", json=body, headers=auth(token))
    if resp.status_code == 201:
        link = resp.json().get("link")
        if isinstance(link, str):
            remember_secret(link)
            match = TEAM_LINK_SHAPE.fullmatch(link)
            if match is not None:
                remember_secret(match.group(2))
    return resp


def _created(resp: Any) -> dict:
    assert resp.status_code == 201, f"link de equipe falhou: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("team.link", 201, body)
    return body


def _expected_link(offering: dict, person: dict, username: str, code: str, expires_at: Any) -> dict:
    return {
        "link": f"https://t.me/{username}?start={code}",
        "expires_at": iso(expires_at),
        "person": {"pseudo_id": person["pseudo_id"], "alias": person["alias"]},
        "offering": {"offering_id": offering["offering_id"], "code": offering["code"],
                     "timezone": offering["timezone"]},
    }


def _enrollments(sql: Any) -> list[tuple]:
    """(pseudo_id, offering_id, role, state, created_at, activated_at), em ordem de matrícula; só as
    colunas que não dependem do vínculo do aluno."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, e.role, e.state, e.created_at, e.activated_at
          FROM enrollments e
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         ORDER BY e.id
        """).fetchall()


def _invites(sql: Any) -> list[tuple]:
    """(pseudo_id, offering_id, code_sha256, state, expires_at, created_at, used_at, revoked_at)."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, o.offering_id::text, i.code_sha256, i.state, i.expires_at,
               i.created_at, i.used_at, i.revoked_at
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         ORDER BY i.id
        """).fetchall()


def _state(sql: Any) -> dict:
    """O que um pedido recusado não pode mudar: matrículas, convites e eventos."""
    return {
        "enrollments": _enrollments(sql),
        "invites": _invites(sql),
        "events": sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall(),
    }


def _last_seq(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _last_outbox(sql: Any) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _events_after(client: Any, cursor: int, *types: str) -> list[dict]:
    """Os eventos dos tipos dados depois de `cursor`, sem o `seq` (só conferido como crescente)."""
    items = list_events(client, ADMIN_TOKEN, *types, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _event(type_: str, at: Any, actor: str | None = None, **data: Any) -> dict:
    return {"type": type_, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


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


def _private(sender: Any, text: str, outcome: str, at: Any, command: str) -> tuple:
    """A linha esperada (sem o id) de um comando do chat privado de `sender`."""
    return ("message", sender.user_id, "private", sender.user_id, text, command, "processed", outcome, at)


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


def _settled_outbox(bot_api: Any, sql: Any, after: int, count: int) -> list[tuple]:
    """Espera `count` mensagens novas na fila, todas em estado final (`sent` ou `failed`)."""

    def ready() -> list[tuple] | None:
        rows = _outbox_after(sql, after)
        return rows if len(rows) >= count and all(r[2] in ("sent", "failed") for r in rows) else None

    rows = bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == count, f"mensagens novas na fila: {[r[1:] for r in rows]}"
    return rows


def _sent_texts(bot_api: Any, bot: int, sender: Any) -> list[str]:
    return [m.text for m in bot_api.sent_messages(bot, chat_id=sender.user_id)]


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


def _validity(moment: datetime) -> str:
    """A validade no fuso: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = moment.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def _invite_code(smtp: Any, address: str, *, username: str, expires_at: datetime,
                 not_before: datetime) -> str:
    """A única mensagem capturada pelo SMTP falso, conferida por inteiro (como nos testes do vínculo;
    e-mail do convite): envelope, cabeçalhos exatos (o `Date` em UTC entre o início do teste e agora, em
    tempo real, e o `Message-ID` com o domínio do remetente, só por forma) e texto decodificado exato.
    Devolve o código do link, que `invite_links()` registra em `remember_secret`."""
    messages = smtp.messages()
    links = smtp.invite_links()
    assert [m.rcpt_to for m in messages] == [(address,)]
    assert [link.message_seq for link in links] == [messages[0].seq], "um link na mensagem"
    message, code = messages[0], links[0].code
    assert (message.mail_from, message.rcpt_to, message.acknowledged) == (SMTP_SENDER, (address,), True)
    assert sorted(name.lower() for name, _ in message.headers) == sorted(INVITE_HEADER_NAMES)
    headers = {name.lower(): value for name, value in message.headers}
    date_value = headers.pop("date")
    message_id = headers.pop("message-id")
    assert headers == {"from": f"Canal de Questões <{SMTP_SENDER}>", "to": address, "subject": INVITE_SUBJECT,
                       "mime-version": "1.0", "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    assert UTC_DATE_RE.fullmatch(date_value), f"Date fora de UTC: {date_value!r}"
    sent = email.utils.parsedate_to_datetime(date_value).replace(tzinfo=timezone.utc)
    assert not_before.replace(microsecond=0) <= sent <= datetime.now(timezone.utc)
    assert MESSAGE_ID_RE.fullmatch(message_id), f"Message-ID fora da forma: {message_id!r}"
    link = f"https://t.me/{username}?start={code}"
    assert message.text == INVITE_BODY.replace("{link}", link).replace("{validity}", _validity(expires_at))
    return code


def _linked_state(sql: Any) -> dict:
    """O que o `team.link` recusado não pode mudar, com a aluna vinculada: matrículas (com a versão e o
    horário do termo), convites, envios de convite, endereços, tarefas e eventos."""
    return {
        **_state(sql),
        "terms": sql.execute(
            "SELECT p.pseudo_id::text, e.terms_version, e.terms_accepted_at FROM enrollments e "
            "JOIN people p ON p.id = e.person_id ORDER BY e.id").fetchall(),
        "invite_sends": sql.execute("SELECT id, state FROM invite_sends ORDER BY id").fetchall(),
        "endpoints": _endpoints(sql),
        "tasks": sql.execute("SELECT id, task_type, state FROM tasks ORDER BY id").fetchall(),
    }


# ---------------------------------------------------------------------------------------------
# Conflito de papel


def test_team_link_for_a_student_of_the_offering_is_role_conflict(client: Any, clock: Any, sql: Any,
                                                                  bot_api: Any) -> None:
    """Gerar link de equipe para quem já é aluno do oferecimento é recusado como
    conflito de papel (equipe e aluno); contrato (`role_conflict`, 409,
    mensagem exata, `fields = [{person, invalid_value}]`; em `team.link`, o `role_conflict` vem
    depois de `bot_required`).

    Uma pessoa cadastrada pelo `people.register` entra como aluna, pela importação (reaproveitada
    pelo RA e pelo e-mail), em oferecimentos em cada estado. O `team.link` dela, pelo apelido e
    pelo identificador:
    - num oferecimento em que ela é aluna, com bot → 409 `role_conflict` `[{person, invalid_value}]`
      no ativo, no rascunho com bot, no encerrado e com o bot `no_access` (em todos esses
      quatro estados; só a falta de bot é `bot_required`);
    - a ordem do contrato vem antes: sem bot → `bot_required` (e não `role_conflict`); arquivado →
      `invalid_state` com `state = archived`; pessoa inexistente → 404; oferecimento inexistente → 404;
      pedido inválido → 400 antes do 403, e o 403 (sem `offerings:write`) antes das regras;
    - em nenhum desses casos nada é gravado: nenhuma matrícula nova (a matrícula de aluno continua a
      única, `student`/`pending`), nenhum convite, nenhum evento.
    Controles positivos: o link de equipe de outra pessoa (que não é aluna) no mesmo oferecimento e o
    da aluna num oferecimento em que ela não é aluna saem (201, corpo exato), com a matrícula
    `team` `pending`, o convite e o `team.link_created`."""
    identity = {"name": "Pessoa Aluna Papel", "ra": "5700000501", "email": "aluna.papel@turma.example.org"}
    student = register_person(client, ADMIN_TOKEN, **identity)
    teammate = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Papel", ra="5700000502",
                               email="equipe.papel@turma.example.org")
    t0 = clock.now()

    active, _ = _offering_with_bot(client, bot_api, "mc102-papel-ativo", "cq_papel_ativo_bot", activate=True)
    _import_reused(client, active, identity)
    draft, _ = _offering_with_bot(client, bot_api, "mc102-papel-rascunho", "cq_papel_rascunho_bot",
                                  activate=False)
    _import_reused(client, draft, identity)
    closed, _ = _offering_with_bot(client, bot_api, "mc102-papel-encerrado", "cq_papel_encerrado_bot",
                                   activate=True)
    _import_reused(client, closed, identity)
    _close(client, closed["code"])
    no_access, no_access_bot = _offering_with_bot(client, bot_api, "mc102-papel-sem-acesso",
                                                  "cq_papel_sem_acesso_bot", activate=False)
    _import_reused(client, no_access, identity)
    # Pré-condição direta no banco (nenhum worker nesta função), como nos testes dos bots: o Telegram recusou o token.
    sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                "WHERE telegram_bot_id = %s", (no_access_bot,))
    no_bot = create_offering(client, ADMIN_TOKEN, code="mc102-papel-sem-bot", course=COURSE, term=TERM,
                             timezone=ZONE)
    _import_reused(client, no_bot, identity)
    archived = create_offering(client, ADMIN_TOKEN, code="mc102-papel-arquivado", course=COURSE, term=TERM,
                               timezone=ZONE)
    _import_reused(client, archived, identity)
    _archive(client, archived["code"])
    free, _ = _offering_with_bot(client, bot_api, "mc102-papel-livre", "cq_papel_livre_bot", activate=True)
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read", "bots:manage", "people:write"],
                             label="sem escrita de oferecimento")

    student_rows = [(student["pseudo_id"], o["offering_id"], "student", "pending", t0, None)
                    for o in (active, draft, closed, no_access, no_bot, archived)]
    assert _enrollments(sql) == student_rows
    assert _invites(sql) == []
    before = _state(sql)

    conflict = ("role_conflict", None)
    cases = [
        ("ativo, pelo apelido", active["code"], {"person": student["alias"]}, ADMIN_TOKEN, 409, conflict),
        ("ativo, pelo identificador", active["offering_id"], {"person": student["pseudo_id"]}, ADMIN_TOKEN,
         409, conflict),
        ("ativo, com validade explícita", active["code"], {"person": student["alias"], "valid_hours": 24},
         ADMIN_TOKEN, 409, conflict),
        ("rascunho com bot", draft["code"], {"person": student["alias"]}, ADMIN_TOKEN, 409, conflict),
        ("encerrado", closed["code"], {"person": student["pseudo_id"]}, ADMIN_TOKEN, 409, conflict),
        ("bot sem acesso", no_access["code"], {"person": student["alias"]}, ADMIN_TOKEN, 409, conflict),
        ("sem bot: bot_required antes do conflito", no_bot["code"], {"person": student["alias"]}, ADMIN_TOKEN,
         409, ("bot_required", None)),
        ("arquivado: invalid_state antes do conflito", archived["code"], {"person": student["alias"]},
         ADMIN_TOKEN, 409, ("invalid_state", "archived")),
        ("pessoa inexistente num oferecimento com aluna", active["code"], {"person": MISSING_ALIAS},
         ADMIN_TOKEN, 404, ("not_found", None)),
        ("oferecimento inexistente", "mc999-papel-inexistente", {"person": student["alias"]}, ADMIN_TOKEN,
         404, ("not_found", None)),
        ("pedido inválido: 400 antes do conflito", active["code"], {"person": student["alias"],
                                                                    "valid_hours": 0},
         ADMIN_TOKEN, 400, ("invalid_request", [("valid_hours", "out_of_range")])),
        ("sem offerings:write: 403 antes do conflito", active["code"], {"person": student["alias"]}, reader,
         403, ("forbidden", "offerings:write")),
        ("sem token: 401", active["code"], {"person": student["alias"]}, None, 401, ("unauthenticated", None)),
    ]
    for label, ref, body, token, status, (code, extra) in cases:
        with _context(label):
            resp = _post_link(client, ref, body, token=token)
            if code == "role_conflict":
                assert_error(resp, 409, "role_conflict", fields=[("person", "invalid_value")])
            elif code == "invalid_state":
                assert_error(resp, status, code, state=extra)
            elif code == "invalid_request":
                assert_error(resp, status, code, fields=extra)
            elif code == "forbidden":
                assert_error(resp, status, code, permission=extra)
            else:
                assert_error(resp, status, code)
            assert _state(sql) == before

    # Controles positivos: quem não é aluno ganha o link de equipe; a aluna ganha o link num
    # oferecimento em que não é aluna.
    clock.advance(60)
    t1 = clock.now()
    expires_at = t1 + timedelta(hours=DEFAULT_HOURS)
    cursor = _last_seq(sql)
    body = _created(_post_link(client, active["code"], {"person": teammate["alias"]}))
    team_code = link_code(body["link"])
    assert body == _expected_link(active, teammate, "cq_papel_ativo_bot", team_code, expires_at)
    body = _created(_post_link(client, free["code"], {"person": student["alias"]}))
    free_code = link_code(body["link"])
    assert body == _expected_link(free, student, "cq_papel_livre_bot", free_code, expires_at)

    assert _enrollments(sql) == [*student_rows,
                                 (teammate["pseudo_id"], active["offering_id"], "team", "pending", t1, None),
                                 (student["pseudo_id"], free["offering_id"], "team", "pending", t1, None)]
    assert _invites(sql) == [
        (teammate["pseudo_id"], active["offering_id"], _sha256(team_code), "pending", expires_at, t1, None,
         None),
        (student["pseudo_id"], free["offering_id"], _sha256(free_code), "pending", expires_at, t1, None, None),
    ]
    assert _events_after(client, cursor, "team.link_created", "team.link_revoked") == [
        _event("team.link_created", t1, "bootstrap", offering_id=active["offering_id"],
               person=teammate["pseudo_id"], expires_at=iso(expires_at)),
        _event("team.link_created", t1, "bootstrap", offering_id=free["offering_id"],
               person=student["pseudo_id"], expires_at=iso(expires_at)),
    ]


def test_team_link_for_a_linked_student_is_role_conflict(client: Any, clock: Any, sql: Any, bot_api: Any,
                                                         bot_worker_thread: Any, stop_one: Any,
                                                         settings: Any, smtp: Any,
                                                         mail_channel: Any) -> None:
    """Gerar link de equipe para quem já é aluno do oferecimento é recusado como
    conflito de papel, qualquer que seja o estado da matrícula de aluno (equipe
    e aluno); contrato (`role_conflict`, 409, mensagem exata, `fields = [{person,
    invalid_value}]`); a matrícula ativa guarda a versão e
    o horário do termo).

    Uma pessoa cadastrada pelo `people.register` entra como aluna pela importação (reaproveitada) e se
    vincula pelo fluxo inteiro: reenvio pela coordenação (`invites.resend`, 200 exato), e-mail do
    convite pelo SMTP falso (conferido por inteiro), `/start <código>` (`student_terms_presented`) e
    `/aceito` (`student_joined`): matrícula `student` `active` (`activated_at` do aceite), convite
    `used`, endereço `active` e `student.joined {terms_version: 1, endpoint_replaced: false}`. Com o
    worker parado, o `team.link` dela no mesmo oferecimento (pelo apelido, pelo identificador e com
    validade explícita) → 409 `role_conflict` `[{person, invalid_value}]`, sem gravar nada: matrículas
    (com o termo), convites, envios, endereços, tarefas e eventos iguais. Controle positivo: o link de
    equipe de outra pessoa (que não é aluna) no mesmo oferecimento sai (201, corpo exato), com a
    matrícula `team` `pending`, o convite e o `team.link_created`. Depois de encerrar o oferecimento
    (o link de equipe é aceito no encerrado), o link da aluna vinculada continua `role_conflict`, sem gravar
    nada."""
    identity = {"name": "Pessoa Aluna Vinculada Papel", "ra": "5700000521",
                "email": "aluna.vinculada.papel@turma.example.org"}
    student = register_person(client, ADMIN_TOKEN, **identity)
    teammate = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Vinculada Papel", ra="5700000522")
    username = "cq_papel_vinculada_bot"
    offering, bot = _offering_with_bot(client, bot_api, "mc102-papel-vinculada", username, activate=True)
    not_before = datetime.now(timezone.utc)
    t0 = clock.now()
    week = t0 + timedelta(hours=DEFAULT_HOURS)
    ref = {"offering_id": offering["offering_id"], "code": offering["code"]}

    _import_reused(client, offering, identity)
    assert resend_invites(client, ADMIN_TOKEN, offering["code"], [student["alias"]]) == {
        "offering": ref, "queued": 1, "valid_hours": DEFAULT_HOURS, "not_students": [], "not_found_count": 0}
    channel = _DefaultChannel()
    worker = bot_worker_thread(channel=channel,
                               settings=dataclasses.replace(settings, worker_id="worker-papel-vinculada"),
                               verification=mail_channel)
    bot_api.wait_for(lambda: sql.execute(
        "SELECT s.state, t.state FROM invite_sends s JOIN tasks t ON t.idempotency_key = 'invite.email:' || s.id"
    ).fetchall() == [("sent", "done")], timeout=PROCESS_TIMEOUT)
    code = _invite_code(smtp, identity["email"], username=username, expires_at=week, not_before=not_before)

    # A aluna se vincula: `/start <código>` e `/aceito` no chat privado.
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)
    clock.advance(STEP)
    t1 = clock.now()
    cursor = _last_seq(sql)
    account = make_user(521)
    row = _processed(bot_api, sql, bot, bot_api.push_start(bot, account, code))
    assert row[1:] == _private(account, f"/start {code}", "student_terms_presented", t1, "/start")
    row = _processed(bot_api, sql, bot, bot_api.push_text(bot, account, "/aceito", bot_command=True))
    assert row[1:] == _private(account, "/aceito", "student_joined", t1, "/aceito")
    bot_api.wait_for(lambda: sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0] == 0,
        timeout=PROCESS_TIMEOUT)
    assert stop_one(worker) != float("inf")

    student_row = (student["pseudo_id"], offering["offering_id"], "student", "active", t0, t1)
    student_invite = (student["pseudo_id"], offering["offering_id"], _sha256(code), "used", week, t0, t1, None)
    assert _enrollments(sql) == [student_row]
    assert _invites(sql) == [student_invite]
    assert _endpoints(sql) == [(student["pseudo_id"], bot, "telegram", account.user_id, "active", t1, t1)]
    assert _events_after(client, cursor, "student.joined") == [
        _event("student.joined", t1, offering_id=offering["offering_id"], person=student["pseudo_id"],
               terms_version=1, endpoint_replaced=False)]
    before = _linked_state(sql)
    assert before["terms"] == [(student["pseudo_id"], 1, t1)]

    conflict_cases = [
        ("vinculada, pelo apelido", offering["code"], {"person": student["alias"]}),
        ("vinculada, pelo identificador", offering["offering_id"], {"person": student["pseudo_id"]}),
        ("vinculada, com validade explícita", offering["code"], {"person": student["alias"], "valid_hours": 24}),
    ]
    for label, ref_path, body in conflict_cases:
        with _context(label):
            assert_error(_post_link(client, ref_path, body), 409, "role_conflict",
                         fields=[("person", "invalid_value")])
            assert _linked_state(sql) == before

    # Controle positivo: quem não é aluno ganha o link de equipe no mesmo oferecimento.
    clock.advance(60)
    t2 = clock.now()
    team_expires = t2 + timedelta(hours=DEFAULT_HOURS)
    cursor = _last_seq(sql)
    created = _created(_post_link(client, offering["code"], {"person": teammate["alias"]}))
    team_code = link_code(created["link"])
    assert created == _expected_link(offering, teammate, username, team_code, team_expires)
    assert _enrollments(sql) == [student_row,
                                 (teammate["pseudo_id"], offering["offering_id"], "team", "pending", t2, None)]
    assert _invites(sql) == [student_invite, (teammate["pseudo_id"], offering["offering_id"], _sha256(team_code),
                                              "pending", team_expires, t2, None, None)]
    assert _events_after(client, cursor, "team.link_created", "team.link_revoked") == [
        _event("team.link_created", t2, "bootstrap", offering_id=offering["offering_id"],
               person=teammate["pseudo_id"], expires_at=iso(team_expires))]

    # Encerrado: o link de equipe é aceito, e a aluna vinculada continua em conflito.
    _close(client, offering["code"])
    before = _linked_state(sql)
    with _context("vinculada, oferecimento encerrado"):
        assert_error(_post_link(client, offering["code"], {"person": student["pseudo_id"]}), 409,
                     "role_conflict", fields=[("person", "invalid_value")])
        assert _linked_state(sql) == before
    assert len(smtp.messages()) == 1
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Regressão do fluxo de equipe dos bots


def test_team_invite_flow_is_unchanged(client: Any, clock: Any, sql: Any, bot_api: Any,
                                          bot_worker_thread: Any, settings: Any, smtp: Any,
                                          mail_channel: Any) -> None:
    """Regressão do fluxo de equipe dentro deste arquivo: os códigos de
    equipe seguem o fluxo próprio, sem mudança, e só um convite de equipe vincula como equipe;
    o vínculo da equipe e os textos do bot não mudam.

    Com o worker montado como no vínculo do aluno (`bots` e o canal de verificação, o adaptador SMTP real sobre o
    SMTP falso, pelo kwarg `verification=` que o `bot_worker_thread` repassa ao `run_worker`):
    - o `team.link` responde 201 com o corpo exato e grava a matrícula `team` `pending`, o
      convite pelo resumo e o `team.link_created`;
    - o `/start <código>` no privado vincula como equipe (`team_joined`): convite `used`, matrícula
      `team` `active`, endereço `active`, `WELCOME_TEAM` ao endereço (pessoa + bot), um `sendMessage`
      só com `chat_id` e `text`, e `team.joined {endpoint_replaced: false}`; nada de termo de uso;
    - `/ajuda` no mesmo chat → `HELP` ao chat do update (`help`);
    - o mesmo código usado por outra conta → `team_refused_invalid`, `LINK_INVALID` ao chat do update
      e `team.join_refused {invalid}`;
    - um link novo para outra pessoa da equipe revoga o pendente dela (`team.link_revoked
      {replaced}`); o código revogado dá `LINK_INVALID` e o novo vincula;
    - o fluxo de equipe não fala com o servidor de e-mail: nenhuma conexão nem mensagem no SMTP falso.
    Só usa rotas, colunas e tipos de evento que não dependem do vínculo do aluno (por isso passa no esqueleto)."""
    offering = create_offering(client, ADMIN_TOKEN, code="mc102-equipe-conflito", course=COURSE, term=TERM,
                               timezone=ZONE)
    bot = bot_api.add_bot("cq_equipe_regressao_bot")
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    member = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Regressao", ra="5700000511")
    other = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Substituta", ra="5700000512")
    t0 = clock.now()
    expires_at = t0 + timedelta(hours=DEFAULT_HOURS)

    body = _created(_post_link(client, offering["code"], {"person": member["alias"]}))
    code = link_code(body["link"])
    assert body == _expected_link(offering, member, "cq_equipe_regressao_bot", code, expires_at)
    assert _enrollments(sql) == [(member["pseudo_id"], offering["offering_id"], "team", "pending", t0, None)]
    assert _invites(sql) == [(member["pseudo_id"], offering["offering_id"], _sha256(code), "pending",
                              expires_at, t0, None, None)]

    channel = _DefaultChannel()
    bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id="worker-equipe-conflito"),
                      verification=mail_channel)
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)
    clock.advance(STEP)
    t1 = clock.now()
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    account = make_user(501)

    uid = bot_api.push_start(bot, account, code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private(account, f"/start {code}", "team_joined", t1, "/start")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME_TEAM, "sent", member["pseudo_id"], bot, account.user_id, None)]
    assert _sent_texts(bot_api, bot, account) == [WELCOME_TEAM]
    assert [(c.params, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ({"chat_id": account.user_id, "text": WELCOME_TEAM}, 200)]
    assert _invites(sql) == [(member["pseudo_id"], offering["offering_id"], _sha256(code), "used", expires_at,
                              t0, t1, None)]
    assert _enrollments(sql) == [(member["pseudo_id"], offering["offering_id"], "team", "active", t0, t1)]
    assert _endpoints(sql) == [(member["pseudo_id"], bot, "telegram", account.user_id, "active", t1, t1)]
    assert _events_after(client, cursor, *TEAM_EVENT_TYPES) == [
        _event("team.joined", t1, offering_id=offering["offering_id"], person=member["pseudo_id"],
               endpoint_replaced=False)]

    def update_tasks() -> list[tuple] | None:
        tasks = sql.execute("SELECT task_type, state, idempotency_key FROM tasks WHERE payload = %s::jsonb",
                            (json.dumps({"update_row_id": row[0]}),)).fetchall()
        return tasks if tasks and all(t[1] in ("done", "failed") for t in tasks) else None

    assert bot_api.wait_for(update_tasks, timeout=PROCESS_TIMEOUT) == [
        ("telegram.update", "done", f"telegram.update:{row[0]}")]

    # `/ajuda` do mesmo chat: `HELP` ao chat do update.
    out_cursor = _last_outbox(sql)
    uid = bot_api.push_text(bot, account, "/ajuda", bot_command=True)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private(account, "/ajuda", "help", t1, "/ajuda")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(HELP, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, account) == [WELCOME_TEAM, HELP]

    # O mesmo código por outra conta: recusado como inválido, com o `team.join_refused`.
    intruder = make_user(502)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, intruder, code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private(intruder, f"/start {code}", "team_refused_invalid", t1, "/start")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, bot, None, uid)]
    assert _sent_texts(bot_api, bot, intruder) == [LINK_INVALID]
    assert _events_after(client, cursor, *TEAM_EVENT_TYPES) == [
        _event("team.join_refused", t1, offering_id=offering["offering_id"], refusal="invalid")]
    assert _endpoints(sql) == [(member["pseudo_id"], bot, "telegram", account.user_id, "active", t1, t1)]

    # Link novo para outra pessoa da equipe: o pendente dela é revogado; só o novo vincula.
    clock.advance(STEP)
    t2 = clock.now()
    cursor = _last_seq(sql)
    old_code = link_code(_created(_post_link(client, offering["code"], {"person": other["alias"]}))["link"])
    clock.advance(STEP)
    t3 = clock.now()
    new_code = link_code(_created(_post_link(client, offering["code"], {"person": other["pseudo_id"]}))["link"])
    assert new_code != old_code
    assert _events_after(client, cursor, *TEAM_EVENT_TYPES) == [
        _event("team.link_created", t2, "bootstrap", offering_id=offering["offering_id"],
               person=other["pseudo_id"], expires_at=iso(t2 + timedelta(hours=DEFAULT_HOURS))),
        _event("team.link_revoked", t3, "bootstrap", offering_id=offering["offering_id"],
               person=other["pseudo_id"], revoke_reason="replaced"),
        _event("team.link_created", t3, "bootstrap", offering_id=offering["offering_id"],
               person=other["pseudo_id"], expires_at=iso(t3 + timedelta(hours=DEFAULT_HOURS))),
    ]
    second = make_user(503)
    cursor, out_cursor = _last_seq(sql), _last_outbox(sql)
    uid = bot_api.push_start(bot, second, old_code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private(second, f"/start {old_code}", "team_refused_invalid", t3, "/start")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(LINK_INVALID, "sent", None, bot, None, uid)]
    out_cursor = _last_outbox(sql)
    uid = bot_api.push_start(bot, second, new_code)
    row = _processed(bot_api, sql, bot, uid)
    assert row[1:] == _private(second, f"/start {new_code}", "team_joined", t3, "/start")
    rows = _settled_outbox(bot_api, sql, out_cursor, 1)
    assert [r[1:] for r in rows] == [(WELCOME_TEAM, "sent", other["pseudo_id"], bot, second.user_id, None)]
    assert _sent_texts(bot_api, bot, second) == [LINK_INVALID, WELCOME_TEAM]
    assert _events_after(client, cursor, *TEAM_EVENT_TYPES) == [
        _event("team.join_refused", t3, offering_id=offering["offering_id"], refusal="invalid"),
        _event("team.joined", t3, offering_id=offering["offering_id"], person=other["pseudo_id"],
               endpoint_replaced=False),
    ]
    assert _invites(sql) == [
        (member["pseudo_id"], offering["offering_id"], _sha256(code), "used", expires_at, t0, t1, None),
        (other["pseudo_id"], offering["offering_id"], _sha256(old_code), "revoked",
         t2 + timedelta(hours=DEFAULT_HOURS), t2, None, t3),
        (other["pseudo_id"], offering["offering_id"], _sha256(new_code), "used",
         t3 + timedelta(hours=DEFAULT_HOURS), t3, t3, None),
    ]
    assert _enrollments(sql) == [(member["pseudo_id"], offering["offering_id"], "team", "active", t0, t1),
                                 (other["pseudo_id"], offering["offering_id"], "team", "active", t2, t3)]
    assert _endpoints(sql) == [(member["pseudo_id"], bot, "telegram", account.user_id, "active", t1, t1),
                               (other["pseudo_id"], bot, "telegram", second.user_id, "active", t3, t3)]

    # O fluxo de equipe nunca fala com o servidor de e-mail, e nada chega ao canal padrão.
    assert smtp.connections() == []
    assert smtp.messages() == []
    assert channel.calls == []
