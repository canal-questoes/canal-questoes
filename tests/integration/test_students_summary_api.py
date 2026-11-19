"""Adesão agregada do oferecimento.

Cobre a adesão agregada e a sua permissão (um convite só conta como válido, ou com
termo apresentado, se veio de um envio `sent`; o último envio que falhou, inclusive por posse esgotada,
conta como falha no envio, salvo a precedência de um convite ainda válido de um envio anterior `sent`; a
adesão ignora o estado do oferecimento), com a injeção de falhas só pelos mecanismos declarados.
Contratos: a rota e os erros de `students.summary`; o envio do convite
(a tarefa `invite.email`, só como meio de montar os estados), os estados e a precedência entre eles e
o e-mail do convite; e as convenções gerais da API (ordem das verificações, sem eco).

Convenções: corpo inteiro por `==` e `assert_matches` (pelo atalho `students_summary`) em
toda resposta de sucesso; erros por `assert_error`; horários pelo `FakeClock`; ids internos, códigos e
pseudônimos nunca como literal (lidos do banco pelo RA da lista ou do e-mail capturado). Os estados da
adesão são montados só pelos fluxos e pelos mecanismos declarados de injeção de falhas nos
testes: o envio pelo worker (`bot_worker_thread` com `verification=`) contra o SMTP falso, a falha
definitiva pelo SMTP falso (`fail("rcpt", 550)`), a queda depois da primeira transação do envio pelo `Crash` do
`ScriptedVerificationChannel` (antes de delegar ou depois de o SMTP falso aceitar), o envio segurado no
gancho `on_send` do mesmo canal (depois do commit da primeira transação, com a tarefa em posse do worker), a posse
esgotada por SQL em `tasks` e o termo e o vínculo pela Bot API falsa; nunca `UPDATE invite_sends` por SQL.
Todo e-mail capturado é conferido por inteiro.

No esqueleto, as rotas `GET /v1/offerings/{ref}/students/summary`, `POST …/students` e
`POST …/invites/resend` não existem: cada função falha no primeiro pedido a uma dessas rotas (404 no lugar do 200 ou
do 400/403 esperado). As asserções sobre o que o esqueleto já tem
(401 sem token; 404 de oferecimento inexistente, que no esqueleto coincide com o 404 da rota ausente)
ficam na mesma função de um caso que falha.
"""

from __future__ import annotations

import dataclasses
import email.utils
import itertools
import queue
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from cq_server.ports.delivery import ChannelCapabilities, OutgoingMessage, Sent
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    create_offering,
    create_team_link,
    create_token,
    import_students,
    register_bot,
    register_person,
    resend_invites,
    students_summary,
)
from tests.helpers.bot_api import make_user
from tests.helpers.channels import Crash, Delegate, ScriptedVerificationChannel
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.smtp import SENDER as SMTP_SENDER

ADMIN = auth(ADMIN_TOKEN)

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
TZ = "America/Sao_Paulo"
DEFAULT_HOURS = 168
PROCESS_TIMEOUT = 15.0
EXPIRE = 31  # avanço que vence a posse de 30 s (a fronteira é estrita: lease_until < agora)
LEASE_EXHAUSTED = "posse vencida sem conclusão"  # `last_error` da posse esgotada

MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"
GROUP_CHAT = {"id": -4001234599, "type": "group", "title": "Turma de Teste da Adesao"}

# E-mail do convite, por extenso.
SUBJECT = f"Seu link para o bot do oferecimento {COURSE} ({TERM})"
EMAIL_BODY = (
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
EMAIL_HEADER_NAMES = ("content-transfer-encoding", "content-type", "date", "from", "message-id",
                      "mime-version", "subject", "to")
MESSAGE_ID_RE = re.compile(r"<[^<>@\s]+@cq\.example\.org>")
UTC_DATE_RE = re.compile(r".+ (?:\+0000|-0000|GMT|UT)")


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


class _DefaultChannel:
    """Canal padrão do worker: toda mensagem deste arquivo tem rota por bot, então nada pode chegar
    aqui (conferido no fim dos testes com worker)."""

    def __init__(self) -> None:
        self.capabilities = ChannelCapabilities(can_edit=False, max_buttons=0, supports_math=False,
                                                supports_rich_text=False)
        self.calls: list[OutgoingMessage] = []

    def send(self, message: OutgoingMessage) -> Sent:
        self.calls.append(message)
        return Sent(f"padrao-{len(self.calls)}")


@dataclasses.dataclass(frozen=True)
class _Student:
    """Uma linha neutra da lista (fora dos alvos canário)."""

    name: str
    ra: str
    email: str

    def row(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


_counter = itertools.count(1)


def _student(label: str) -> _Student:
    n = next(_counter)
    return _Student(name=f"Aluno Adesao {label}", ra=f"665{n:07d}",
                    email=f"aluno.adesao.{label.lower().replace(' ', '.')}.{n}@turma.example.org")


def _ref(offering: dict) -> dict:
    """O `OfferingRef` das respostas do vínculo do aluno."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _offering(client: Any, bot_api: Any, code: str, username: str | None = None, *,
              activate: bool = True) -> tuple[dict, int]:
    """Oferecimento (disciplina, período e fuso padrão) com um bot novo da Bot API falsa registrado e,
    salvo `activate=False`, ativado; devolve o corpo do cadastro e o id do bot no Telegram."""
    offering = create_offering(client, ADMIN_TOKEN, code=code, course=COURSE, term=TERM, timezone=TZ)
    bot = bot_api.add_bot(username) if username is not None else bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot))
    if activate:
        activate_bot(client, ADMIN_TOKEN, code)
    return offering, bot


def _import(client: Any, offering: dict, students: list[_Student]) -> None:
    """Importa alunos novos (`students.import`) e confere a resposta inteira (só contagens)."""
    n = len(students)
    body = import_students(client, ADMIN_TOKEN, offering["code"], [s.row() for s in students])
    assert body == {"offering": _ref(offering), "received": n, "people_created": n, "people_reused": 0,
                    "enrollments_created": n, "already_enrolled": 0}


def _person(sql: Any, student: _Student) -> dict[str, str]:
    """`{pseudo_id, alias}` da pessoa criada pela importação, achada pelo RA (leitura de teste)."""
    row = sql.execute("SELECT p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (student.ra,)).fetchone()
    assert row is not None, "aluno da lista sem pessoa depois da importação"
    return {"pseudo_id": row[0], "alias": row[1]}


def _summary(offering: dict, *, no_invite: int = 0, sending: int = 0, invite_valid: int = 0,
             terms_presented: int = 0, invite_lapsed: int = 0, send_failed: int = 0, linked: int = 0) -> dict:
    """A resposta 200 exata de `students.summary`: só contagens, com `enrolled` = a
    soma dos outros sete, sem identidade nem pseudônimo."""
    counts = {"no_invite": no_invite, "sending": sending, "invite_valid": invite_valid,
              "terms_presented": terms_presented, "invite_lapsed": invite_lapsed, "send_failed": send_failed,
              "linked": linked}
    return {"offering": _ref(offering), "enrolled": sum(counts.values()), **counts}


def _get_summary(client: Any, ref: str, *, token: str | None = ADMIN_TOKEN, params: Any = None) -> Any:
    return client.get(f"/v1/offerings/{ref}/students/summary", params=params, headers=auth(token))


def _resend(client: Any, offering: dict, people: list[dict], *, valid_hours: int | None = None) -> None:
    """Reenvio pela coordenação (`invites.resend`) aos alunos dados, pelo apelido; confere a resposta."""
    body = resend_invites(client, ADMIN_TOKEN, offering["code"], [p["alias"] for p in people],
                          valid_hours=valid_hours)
    assert body == {"offering": _ref(offering), "queued": len(people),
                    "valid_hours": DEFAULT_HOURS if valid_hours is None else valid_hours,
                    "not_students": [], "not_found_count": 0}


def _sends(sql: Any, offering: dict) -> list[tuple]:
    """(id, pseudo_id, valid_hours, state, failure, error_type, attempts, estado da tarefa) dos envios do
    oferecimento, em ordem de `invite_sends.id`."""
    return sql.execute(
        """
        SELECT s.id, p.pseudo_id::text, s.valid_hours, s.state, s.failure, s.error_type, s.attempts, t.state
          FROM invite_sends s
          JOIN enrollments e ON e.id = s.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
          JOIN tasks t ON t.idempotency_key = 'invite.email:' || s.id
         WHERE o.offering_id = %s
         ORDER BY s.id
        """, (uuid.UUID(offering["offering_id"]),)).fetchall()


def _last_send_id(sql: Any, offering: dict, person: dict) -> int:
    rows = [row[0] for row in _sends(sql, offering) if row[1] == person["pseudo_id"]]
    assert rows, "a pessoa não tem envio"
    return rows[-1]


def _task_of(sql: Any, send_id: int) -> tuple:
    """(state, attempts, last_error) da única tarefa `invite.email:<id>` do envio."""
    rows = sql.execute("SELECT state, attempts, last_error FROM tasks WHERE idempotency_key = %s",
                       (f"invite.email:{send_id}",)).fetchall()
    assert len(rows) == 1, f"tarefas do envio: {rows}"
    return rows[0]


def _exhaust_lease(sql: Any, settings: Any, clock: Any, send_id: int) -> None:
    """Posse esgotada por SQL (um dos mecanismos declarados de injeção de falhas): a tarefa do envio fica
    `leased`, com `attempts = max_attempts` e a posse vencida; o próximo `claim` da fila a marca `failed`
    sem chamar o tratador. Nunca toca em `invite_sends`."""
    rows = sql.execute(
        "UPDATE tasks SET state = 'leased', attempts = %s, lease_owner = 'worker-sem-posse', lease_until = %s "
        "WHERE idempotency_key = %s AND state IN ('pending', 'leased') RETURNING id",
        (settings.max_attempts, clock.now() - timedelta(seconds=1), f"invite.email:{send_id}")).fetchall()
    assert len(rows) == 1, "a tarefa do envio não estava pendente nem em posse"


def _ok(resp: Any, op_id: str) -> dict:
    """200 com o corpo validado pelo esquema congelado da operação."""
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _validity(moment: datetime, zone: str = TZ) -> str:
    """A validade no fuso: `AAAA-MM-DD HH:MM <abreviação> (<fuso>)`."""
    local = moment.astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({zone})"


def _assert_invite_email(message: Any, *, address: str, username: str, code: str, expires_at: datetime,
                         not_before: datetime) -> None:
    """Uma mensagem capturada pelo SMTP falso, por inteiro (o e-mail do convite):
    envelope, cabeçalhos exatos (o `Date` em UTC entre o início do teste e agora, em tempo real, e o
    `Message-ID` com o domínio do remetente, só por forma) e texto decodificado exato."""
    assert (message.mail_from, message.rcpt_to, message.acknowledged) == (SMTP_SENDER, (address,), True)
    assert sorted(name.lower() for name, _ in message.headers) == sorted(EMAIL_HEADER_NAMES)
    headers = {name.lower(): value for name, value in message.headers}
    date_value = headers.pop("date")
    message_id = headers.pop("message-id")
    assert headers == {"from": f"Canal de Questões <{SMTP_SENDER}>", "to": address, "subject": SUBJECT,
                       "mime-version": "1.0", "content-type": 'text/plain; charset="utf-8"',
                       "content-transfer-encoding": "quoted-printable"}
    assert UTC_DATE_RE.fullmatch(date_value), f"Date fora de UTC: {date_value!r}"
    sent = email.utils.parsedate_to_datetime(date_value).replace(tzinfo=timezone.utc)
    assert not_before.replace(microsecond=0) <= sent <= datetime.now(timezone.utc)
    assert MESSAGE_ID_RE.fullmatch(message_id), f"Message-ID fora da forma: {message_id!r}"
    link = f"https://t.me/{username}?start={code}"
    assert message.text == EMAIL_BODY.replace("{link}", link).replace("{validity}", _validity(expires_at))


def _emails(smtp: Any, username: str, expected: list[tuple[str, datetime]], not_before: datetime) -> dict[str, str]:
    """Confere, na ordem de chegada, todas as mensagens capturadas contra `expected` (endereço e
    vencimento do convite) e devolve o código de cada endereço (os códigos entram em `remember_secret`
    pelo `invite_links()`)."""
    messages = smtp.messages()
    links = smtp.invite_links()
    assert [m.rcpt_to for m in messages] == [(address,) for address, _ in expected]
    assert [link.message_seq for link in links] == [m.seq for m in messages], "um link por mensagem"
    for message, link, (address, expires_at) in zip(messages, links, expected):
        _assert_invite_email(message, address=address, username=username, code=link.code,
                             expires_at=expires_at, not_before=not_before)
    return {address: link.code for link, (address, _) in zip(links, expected)}


def _worker(bot_worker_thread: Any, settings: Any, channel: _DefaultChannel, name: str, verification: Any) -> Any:
    return bot_worker_thread(channel=channel, settings=dataclasses.replace(settings, worker_id=name),
                             verification=verification)


def _wait(bot_api: Any, predicate: Any) -> Any:
    return bot_api.wait_for(predicate, timeout=PROCESS_TIMEOUT)


def _outcome(bot_api: Any, sql: Any, bot: int, update_id: int | None) -> str:
    """Espera o update gravado e processado e devolve o `outcome` da única linha dele."""
    assert update_id is not None, "o dublê descartou o update"

    def ready() -> list[tuple] | None:
        rows = sql.execute(
            "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (bot, update_id)).fetchall()
        return rows if rows and all(r[0] == "processed" for r in rows) else None

    rows = _wait(bot_api, ready)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0][1]


def _outbox_settled(bot_api: Any, sql: Any) -> None:
    _wait(bot_api, lambda: sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')").fetchone()[0] == 0)


def _events(sql: Any) -> list[tuple]:
    return sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall()


def _invites_of(sql: Any, offering: dict) -> list[tuple]:
    """(pseudo_id, state, expires_at, presented_terms_version) dos convites do oferecimento, em ordem de
    `invites.id`."""
    return sql.execute(
        """
        SELECT p.pseudo_id::text, i.state, i.expires_at, i.presented_terms_version
          FROM invites i
          JOIN enrollments e ON e.id = i.enrollment_id
          JOIN people p ON p.id = e.person_id
          JOIN offerings o ON o.id = e.offering_id
         WHERE o.offering_id = %s
         ORDER BY i.id
        """, (uuid.UUID(offering["offering_id"]),)).fetchall()


def _enrollment_states(sql: Any, offering: dict) -> dict[str, tuple]:
    """{pseudo_id: (role, state)} das matrículas do oferecimento."""
    rows = sql.execute(
        "SELECT p.pseudo_id::text, e.role, e.state FROM enrollments e JOIN people p ON p.id = e.person_id "
        "JOIN offerings o ON o.id = e.offering_id WHERE o.offering_id = %s",
        (uuid.UUID(offering["offering_id"]),)).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


class _HeldSends:
    """Gancho `on_send` do `ScriptedVerificationChannel` (injeção de falhas nos testes): cada
    envio espera dentro do `send`, isto é, depois do commit da primeira transação e com a tarefa `invite.email` em posse
    do worker (nenhuma chamada externa dentro de transação), até o teste soltá-lo.
    `next()` devolve o endereço do próximo envio segurado e a trava dele; `release_all()` solta todos,
    inclusive os que chegarem depois (fim do teste, também numa falha)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._arrivals: queue.Queue[tuple[str, threading.Event]] = queue.Queue()
        self._gates: list[threading.Event] = []
        self._closed = False

    def __call__(self, message: Any) -> None:
        gate = threading.Event()
        with self._lock:
            self._gates.append(gate)
            if self._closed:
                gate.set()
        self._arrivals.put((message.address, gate))
        gate.wait(PROCESS_TIMEOUT)

    def next(self) -> tuple[str, threading.Event]:
        try:
            return self._arrivals.get(timeout=PROCESS_TIMEOUT)
        except queue.Empty:
            raise AssertionError(f"nenhum envio chegou ao canal em {PROCESS_TIMEOUT} s") from None

    def release_all(self) -> None:
        with self._lock:
            self._closed = True
            for gate in self._gates:
                gate.set()


# ---------------------------------------------------------------------------------------------
# Os sete estados
# ---------------------------------------------------------------------------------------------


def test_summary_counts_every_adhesion_state(client: Any, clock: Any, sql: Any, bot_api: Any,
                                             bot_worker_thread: Any, stop_one: Any, settings: Any,
                                             smtp: Any, mail_channel: Any) -> None:
    """Cada aluno em exatamente um estado (`enrolled` = soma; sem identidade nem
    pseudônimo); primeiro estado verdadeiro, na ordem de precedência; só mecanismos declarados.

    Montagem, num só oferecimento, com 13 alunos importados (pelo reenvio da coordenação, que escolhe
    cada aluno pelo pseudônimo):
    - L (`/start` + `/aceito`: `linked`), T (`/start`: `terms_presented`) e V (`invite_valid`): envios
      entregues, com 168 h;
    - F: 550 no RCPT pelo SMTP falso (`failed`/`recipient_refused`: `send_failed`);
    - AB: envio A entregue (convite pendente e válido) e envio B mais novo com a posse esgotada antes da
      primeira transação → `invite_valid`, não `send_failed` (a precedência dos estados);
    - X e TX: envios entregues com 1 h; TX apresenta o termo; depois que a hora passa, os dois ficam em
      `invite_lapsed` (inclusive o termo apresentado com o convite vencido);
    - RV: envio entregue, e o código colado num grupo (`/start <código>` fora do chat privado:
      `not_handled` e o convite revogado por exposição) → `invite_lapsed`
      (convite revogado sem substituto, com o último envio `sent`);
    - E2: a primeira transação grava o convite, o SMTP falso aceita o e-mail e o worker cai (`Crash` depois de delegar);
      a posse é esgotada e o aluno apresenta o termo com o código do e-mail: o convite `pending`
      apresentado é de um envio que não está `sent` → `send_failed`, não `terms_presented`;
    - E3: a primeira transação grava o convite e o worker cai antes de enviar (`Crash`); posse esgotada → `send_failed`,
      não `invite_valid`;
    - E1: posse esgotada antes da primeira transação (sem convite) → `send_failed`;
    - G: reenvio sem worker no ar (envio e tarefa pendentes) → `sending`;
    - N: nada → `no_invite`.
    A adesão é lida em quatro momentos (depois dos primeiros envios; com todos os estados montados;
    depois de a hora passar; com o oferecimento encerrado, que dá o mesmo resultado)."""
    not_before = datetime.now(timezone.utc)
    username = "cq_adesao_estados_bot"
    offering, bot = _offering(client, bot_api, "adesao-estados", username)
    lnk, trm, val, fail, ab = (_student(label) for label in ("Vinculado", "Termo", "Valido", "Falha", "Precedencia"))
    rv = _student("Revogado")
    x, tx, ex2, ex3, ex1 = (_student(label) for label in ("Vence", "Termo Vence", "Queda Depois", "Queda Antes",
                                                          "Posse Antes"))
    snd, none = _student("Enviando"), _student("Sem Convite")
    everyone = [lnk, trm, val, fail, ab, rv, x, tx, ex2, ex3, ex1, snd, none]
    t0 = clock.now()
    _import(client, offering, everyone)
    person = {s.ra: _person(sql, s) for s in everyone}
    email_of = {person[s.ra]["pseudo_id"]: s.email for s in everyone}
    ref = offering["code"]
    channel = _DefaultChannel()
    week, hour = t0 + timedelta(hours=DEFAULT_HOURS), t0 + timedelta(hours=1)
    assert students_summary(client, ADMIN_TOKEN, ref) == _summary(offering, no_invite=13)

    # Primeiros envios (168 h e 1 h), com F recusado de vez pelo servidor.
    smtp.fail("rcpt", 550, when=lambda ctx: ctx.argument == fail.email, times=None)
    _resend(client, offering, [person[s.ra] for s in (lnk, trm, val, fail, ab, rv)])
    _resend(client, offering, [person[s.ra] for s in (x, tx)], valid_hours=1)
    first = [(row[1], row[2]) for row in _sends(sql, offering)]
    w1 = _worker(bot_worker_thread, settings, channel, "worker-adesao-1", mail_channel)
    _wait(bot_api, lambda: [row[3] for row in _sends(sql, offering)].count("pending") == 0)
    delivered = [(email_of[pid], week if hours == DEFAULT_HOURS else hour) for pid, hours in first
                 if pid != person[fail.ra]["pseudo_id"]]
    smtp.wait_for(lambda: len(smtp.messages()) >= 7, timeout=PROCESS_TIMEOUT)
    codes = _emails(smtp, username, delivered, not_before)

    t_user, l_user, tx_user, e2_user = make_user(4501), make_user(4502), make_user(4503), make_user(4504)
    rv_user = make_user(4505)
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, t_user, codes[trm.email])) == "student_terms_presented"
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, l_user, codes[lnk.email])) == "student_terms_presented"
    assert _outcome(bot_api, sql, bot, bot_api.push_text(bot, l_user, "/aceito", bot_command=True)) == "student_joined"
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, tx_user, codes[tx.email])) == "student_terms_presented"
    exposed = bot_api.push_start(bot, rv_user, codes[rv.email], chat=GROUP_CHAT)
    assert _outcome(bot_api, sql, bot, exposed) == "not_handled"
    _outbox_settled(bot_api, sql)
    assert students_summary(client, ADMIN_TOKEN, ref) == _summary(
        offering, no_invite=5, invite_valid=3, terms_presented=2, invite_lapsed=1, send_failed=1, linked=1)
    assert stop_one(w1) != float("inf")

    # E2: queda depois de o SMTP falso aceitar; E3: queda antes de enviar. As posses são esgotadas.
    _resend(client, offering, [person[ex2.ra]])
    w2 = _worker(bot_worker_thread, settings, channel, "worker-adesao-2",
                 ScriptedVerificationChannel([Delegate(then=Crash())], delegate=mail_channel))
    _wait(bot_api, lambda: not w2.is_alive())
    assert isinstance(w2.error, Crash), f"esperada a queda por Crash, veio {w2.error!r}"
    _exhaust_lease(sql, settings, clock, _last_send_id(sql, offering, person[ex2.ra]))
    _resend(client, offering, [person[ex3.ra]])
    w3 = _worker(bot_worker_thread, settings, channel, "worker-adesao-3", ScriptedVerificationChannel([Crash()]))
    _wait(bot_api, lambda: not w3.is_alive())
    assert isinstance(w3.error, Crash), f"esperada a queda por Crash, veio {w3.error!r}"
    _exhaust_lease(sql, settings, clock, _last_send_id(sql, offering, person[ex3.ra]))
    codes = _emails(smtp, username, delivered + [(ex2.email, week)], not_before)

    # A posse do bot (da thread que caiu) vence; o aluno E2 apresenta o termo com o código recebido.
    clock.advance(EXPIRE)
    w4 = _worker(bot_worker_thread, settings, channel, "worker-adesao-4", mail_channel)
    _wait(bot_api, lambda: _task_of(sql, _last_send_id(sql, offering, person[ex3.ra]))[0] == "failed")
    assert _outcome(bot_api, sql, bot, bot_api.push_start(bot, e2_user, codes[ex2.email])) == "student_terms_presented"
    _outbox_settled(bot_api, sql)
    assert stop_one(w4) != float("inf")

    # AB (envio B) e E1: posse esgotada antes da primeira transação; G: envio e tarefa pendentes, sem worker.
    _resend(client, offering, [person[ab.ra]])
    _exhaust_lease(sql, settings, clock, _last_send_id(sql, offering, person[ab.ra]))
    _resend(client, offering, [person[ex1.ra]])
    _exhaust_lease(sql, settings, clock, _last_send_id(sql, offering, person[ex1.ra]))
    w5 = _worker(bot_worker_thread, settings, channel, "worker-adesao-5", mail_channel)
    _wait(bot_api, lambda: _task_of(sql, _last_send_id(sql, offering, person[ab.ra]))[0] == "failed"
          and _task_of(sql, _last_send_id(sql, offering, person[ex1.ra]))[0] == "failed")
    assert stop_one(w5) != float("inf")
    _resend(client, offering, [person[snd.ra]])

    by_person: dict[str, list[tuple]] = {}
    for row in _sends(sql, offering):
        by_person.setdefault(row[1], []).append(row[2:])
    assert by_person == {
        person[lnk.ra]["pseudo_id"]: [(DEFAULT_HOURS, "sent", None, None, 1, "done")],
        person[trm.ra]["pseudo_id"]: [(DEFAULT_HOURS, "sent", None, None, 1, "done")],
        person[val.ra]["pseudo_id"]: [(DEFAULT_HOURS, "sent", None, None, 1, "done")],
        person[fail.ra]["pseudo_id"]: [(DEFAULT_HOURS, "failed", "recipient_refused", "SMTPRecipientsRefused", 1,
                                        "failed")],
        person[ab.ra]["pseudo_id"]: [(DEFAULT_HOURS, "sent", None, None, 1, "done"),
                                     (DEFAULT_HOURS, "pending", None, None, 0, "failed")],
        person[rv.ra]["pseudo_id"]: [(DEFAULT_HOURS, "sent", None, None, 1, "done")],
        person[x.ra]["pseudo_id"]: [(1, "sent", None, None, 1, "done")],
        person[tx.ra]["pseudo_id"]: [(1, "sent", None, None, 1, "done")],
        person[ex2.ra]["pseudo_id"]: [(DEFAULT_HOURS, "pending", None, None, 1, "failed")],
        person[ex3.ra]["pseudo_id"]: [(DEFAULT_HOURS, "pending", None, None, 1, "failed")],
        person[ex1.ra]["pseudo_id"]: [(DEFAULT_HOURS, "pending", None, None, 0, "failed")],
        person[snd.ra]["pseudo_id"]: [(DEFAULT_HOURS, "pending", None, None, 0, "pending")],
    }
    for student in (ex2, ex3, ex1, ab):
        exhausted = _task_of(sql, _last_send_id(sql, offering, person[student.ra]))
        assert exhausted == ("failed", settings.max_attempts, LEASE_EXHAUSTED)
    invites = sql.execute(
        """
        SELECT p.pseudo_id::text, i.state, i.expires_at, i.presented_terms_version
          FROM invites i JOIN enrollments e ON e.id = i.enrollment_id JOIN people p ON p.id = e.person_id
         ORDER BY i.id
        """).fetchall()
    assert sorted(invites) == sorted([
        (person[lnk.ra]["pseudo_id"], "used", week, 1),
        (person[trm.ra]["pseudo_id"], "pending", week, 1),
        (person[val.ra]["pseudo_id"], "pending", week, None),
        (person[fail.ra]["pseudo_id"], "revoked", week, None),
        (person[ab.ra]["pseudo_id"], "pending", week, None),
        (person[rv.ra]["pseudo_id"], "revoked", week, None),
        (person[x.ra]["pseudo_id"], "pending", hour, None),
        (person[tx.ra]["pseudo_id"], "pending", hour, 1),
        (person[ex2.ra]["pseudo_id"], "pending", week, 1),
        (person[ex3.ra]["pseudo_id"], "pending", week, None),
    ])
    assert len(smtp.messages()) == 8, "e-mails só dos envios entregues e do E2 (aceito antes da queda)"

    expected = _summary(offering, no_invite=1, sending=1, invite_valid=3, terms_presented=2, invite_lapsed=1,
                        send_failed=4, linked=1)
    before = _events(sql)
    assert students_summary(client, ADMIN_TOKEN, ref) == expected
    assert students_summary(client, ADMIN_TOKEN, offering["offering_id"]) == expected

    # Passa a hora: os convites de X e TX vencem (TX tinha o termo apresentado) e somam-se a RV.
    clock.advance(3600)
    lapsed = _summary(offering, no_invite=1, sending=1, invite_valid=2, terms_presented=1, invite_lapsed=3,
                      send_failed=4, linked=1)
    assert students_summary(client, ADMIN_TOKEN, ref) == lapsed
    assert _events(sql) == before, "a consulta da adesão não grava nada"

    # A adesão ignora o estado do oferecimento: encerrado, a mesma tabela.
    closed = _ok(client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")
    assert closed["state"] == "closed"
    assert students_summary(client, ADMIN_TOKEN, ref) == lapsed
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Precedência de `linked`; `sending` com a tarefa em posse
# ---------------------------------------------------------------------------------------------


def test_summary_linked_takes_precedence_and_a_leased_task_is_sending(
        client: Any, clock: Any, sql: Any, bot_api: Any, bot_worker_thread: Any, stop_one: Any,
        settings: Any, smtp: Any, mail_channel: Any) -> None:
    """Cada aluno em exatamente um estado (`enrolled` = soma); primeiro estado
    verdadeiro, na ordem de precedência: `linked` vem antes de `sending`, de `invite_valid` e de
    `send_failed`; `sending` vale com a tarefa `pending` **ou `leased`**; o envio do convite (primeira transação:
    a origem `coordination` envia mesmo a aluno vinculado; segunda transação: a falha definitiva grava
    `failed`); só mecanismos declarados.

    Montagem, num só oferecimento, com 4 alunos importados:
    - L1 e L2 recebem o convite (reenvio da coordenação, 168 h) e se vinculam (`/start` + `/aceito`); F
      tem o envio recusado de vez pelo SMTP falso (550 no RCPT): `linked` 2, `send_failed` 1 (F é o
      controle: a mesma falha, num aluno não vinculado, conta como `send_failed`) e `no_invite` 1 (S);
    - reenvio a L2, recusado de vez pelo SMTP falso (o último envio de L2 fica `failed`/
      `recipient_refused`, com a tarefa `failed`): L2 continua `linked`, não `send_failed`;
    - reenvio a L1 sem worker no ar (envio e tarefa `pending`): L1 continua `linked`, não `sending`;
    - um worker com o `ScriptedVerificationChannel` (delegando ao SMTP falso) segura o envio no gancho
      `on_send`: com o envio de L1 `pending` (`attempts` 1, o convite da primeira transação gravado) e a tarefa
      `leased`, L1 continua `linked`; solto, o envio fica `sent` (a tarefa `done`), e L1 continua
      `linked`, não `invite_valid`, com o convite novo pendente e válido de um envio `sent`;
    - reenvio a S, segurado da mesma forma: com o envio `pending` e a tarefa `leased` (posse vigente),
      S conta como `sending`; solto, `invite_valid`.
    As contagens são exatas em cada momento. No fim, os envios, os convites e as matrículas por aluno
    são os esperados, e os 4 e-mails capturados (L1 e L2, L1 de novo e S; o 550 não gera mensagem) são
    conferidos por inteiro."""
    not_before = datetime.now(timezone.utc)
    username = "cq_adesao_precedencia_bot"
    offering, bot = _offering(client, bot_api, "adesao-precedencia", username)
    l1, l2 = _student("Vinculado Reenvio"), _student("Vinculado Falha")
    fail, snd = _student("Falha Controle"), _student("Posse Vigente")
    everyone = [l1, l2, fail, snd]
    t0 = clock.now()
    week = t0 + timedelta(hours=DEFAULT_HOURS)
    _import(client, offering, everyone)
    person = {s.ra: _person(sql, s) for s in everyone}
    pid = {s.ra: person[s.ra]["pseudo_id"] for s in everyone}
    email_of = {pid[s.ra]: s.email for s in everyone}
    ref = offering["code"]
    channel = _DefaultChannel()
    assert students_summary(client, ADMIN_TOKEN, ref) == _summary(offering, no_invite=4)

    # L1 e L2 recebem o convite e se vinculam; F é recusado de vez pelo servidor (controle).
    smtp.fail("rcpt", 550, when=lambda ctx: ctx.argument == fail.email, times=None)
    _resend(client, offering, [person[s.ra] for s in (l1, l2, fail)])
    first = [row[1] for row in _sends(sql, offering)]
    w1 = _worker(bot_worker_thread, settings, channel, "worker-precedencia-1", mail_channel)
    _wait(bot_api, lambda: [row[3] for row in _sends(sql, offering)].count("pending") == 0)
    smtp.wait_for(lambda: len(smtp.messages()) >= 2, timeout=PROCESS_TIMEOUT)
    delivered = [(email_of[p], week) for p in first if p != pid[fail.ra]]
    codes = _emails(smtp, username, delivered, not_before)
    for n, student in enumerate((l1, l2)):
        user = make_user(4601 + n)
        pushed = bot_api.push_start(bot, user, codes[student.email])
        assert _outcome(bot_api, sql, bot, pushed) == "student_terms_presented"
        pushed = bot_api.push_text(bot, user, "/aceito", bot_command=True)
        assert _outcome(bot_api, sql, bot, pushed) == "student_joined"
    _outbox_settled(bot_api, sql)
    assert _enrollment_states(sql, offering) == {
        pid[l1.ra]: ("student", "active"), pid[l2.ra]: ("student", "active"),
        pid[fail.ra]: ("student", "pending"), pid[snd.ra]: ("student", "pending")}
    linked_two = _summary(offering, no_invite=1, send_failed=1, linked=2)
    assert students_summary(client, ADMIN_TOKEN, ref) == linked_two

    # `linked` antes de `send_failed`: o último envio de L2 falha de vez.
    smtp.fail("rcpt", 550, when=lambda ctx: ctx.argument == l2.email, times=None)
    _resend(client, offering, [person[l2.ra]])
    l2_failed = _last_send_id(sql, offering, person[l2.ra])
    _wait(bot_api, lambda: _task_of(sql, l2_failed)[0] == "failed")
    assert _task_of(sql, l2_failed) == ("failed", 1, "PermanentTaskError")
    assert students_summary(client, ADMIN_TOKEN, ref) == linked_two
    assert stop_one(w1) != float("inf")

    # `linked` antes de `sending` (tarefa `pending`): reenvio a L1 sem worker no ar.
    _resend(client, offering, [person[l1.ra]])
    l1_pending = _last_send_id(sql, offering, person[l1.ra])
    assert _task_of(sql, l1_pending) == ("pending", 0, None)
    assert [row[2:] for row in _sends(sql, offering) if row[0] == l1_pending] == [
        (DEFAULT_HOURS, "pending", None, None, 0, "pending")]
    assert students_summary(client, ADMIN_TOKEN, ref) == linked_two

    held = _HeldSends()
    try:
        # `linked` antes de `sending` (tarefa `leased`): o envio de L1 segurado no canal.
        w2 = _worker(bot_worker_thread, settings, channel, "worker-precedencia-2",
                     ScriptedVerificationChannel(delegate=mail_channel, on_send=held))
        address, gate = held.next()
        assert address == l1.email
        assert _task_of(sql, l1_pending) == ("leased", 1, None)
        assert [row[2:] for row in _sends(sql, offering) if row[0] == l1_pending] == [
            (DEFAULT_HOURS, "pending", None, None, 1, "leased")]
        assert students_summary(client, ADMIN_TOKEN, ref) == linked_two
        gate.set()
        _wait(bot_api, lambda: _task_of(sql, l1_pending)[0] == "done")
        # `linked` antes de `invite_valid`: o convite novo de L1 é de um envio `sent`.
        assert students_summary(client, ADMIN_TOKEN, ref) == linked_two

        # `sending` com a tarefa `leased`: o envio de S segurado no canal.
        _resend(client, offering, [person[snd.ra]])
        snd_send = _last_send_id(sql, offering, person[snd.ra])
        address, gate = held.next()
        assert address == snd.email
        assert _task_of(sql, snd_send) == ("leased", 1, None)
        assert [row[2:] for row in _sends(sql, offering) if row[0] == snd_send] == [
            (DEFAULT_HOURS, "pending", None, None, 1, "leased")]
        assert students_summary(client, ADMIN_TOKEN, ref) == _summary(
            offering, sending=1, send_failed=1, linked=2)
        gate.set()
        _wait(bot_api, lambda: _task_of(sql, snd_send)[0] == "done")
    finally:
        held.release_all()
    assert students_summary(client, ADMIN_TOKEN, ref) == _summary(
        offering, invite_valid=1, send_failed=1, linked=2)
    assert stop_one(w2) != float("inf")

    by_person: dict[str, list[tuple]] = {}
    for row in _sends(sql, offering):
        by_person.setdefault(row[1], []).append(row[2:])
    sent = (DEFAULT_HOURS, "sent", None, None, 1, "done")
    refused = (DEFAULT_HOURS, "failed", "recipient_refused", "SMTPRecipientsRefused", 1, "failed")
    assert by_person == {pid[l1.ra]: [sent, sent], pid[l2.ra]: [sent, refused], pid[fail.ra]: [refused],
                         pid[snd.ra]: [sent]}
    assert sorted(_invites_of(sql, offering)) == sorted([
        (pid[l1.ra], "used", week, 1), (pid[l1.ra], "pending", week, None),
        (pid[l2.ra], "used", week, 1), (pid[l2.ra], "revoked", week, None),
        (pid[fail.ra], "revoked", week, None),
        (pid[snd.ra], "pending", week, None),
    ])
    assert _enrollment_states(sql, offering) == {
        pid[l1.ra]: ("student", "active"), pid[l2.ra]: ("student", "active"),
        pid[fail.ra]: ("student", "pending"), pid[snd.ra]: ("student", "pending")}
    _emails(smtp, username, delivered + [(l1.email, week), (snd.email, week)], not_before)
    assert channel.calls == []


# ---------------------------------------------------------------------------------------------
# Sem alunos, só a equipe e qualquer estado; recusas
# ---------------------------------------------------------------------------------------------


def test_summary_counts_only_students_in_any_offering_state(client: Any, sql: Any, bot_api: Any) -> None:
    """Só as matrículas de aluno do oferecimento (a equipe não entra); `students.summary` (vale em
    qualquer estado do oferecimento; erros 400 do `ref` e da consulta, 404) e as regras da API base (consulta
    desconhecida → `[{query, unknown_field}]`, sem eco): um oferecimento sem alunos dá tudo zero; um
    rascunho com 3 alunos importados e um membro da equipe dá `enrolled` 3, todos `no_invite`, também
    depois de arquivado; oferecimento inexistente (por código e por identificador) → 404; `ref` fora do
    formato → 400; parâmetro de consulta desconhecido → 400. A consulta não grava nada."""
    empty = create_offering(client, ADMIN_TOKEN, code="adesao-vazia", course=COURSE, term=TERM, timezone=TZ)
    assert students_summary(client, ADMIN_TOKEN, empty["code"]) == _summary(empty)

    offering, _ = _offering(client, bot_api, "adesao-rascunho", activate=False)
    _import(client, offering, [_student("Um"), _student("Dois"), _student("Tres")])
    team = register_person(client, ADMIN_TOKEN, name="Pessoa Adesao Equipe", ra="6659000001")
    create_team_link(client, ADMIN_TOKEN, offering["code"], team["alias"])
    before = _events(sql)
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == _summary(offering, no_invite=3)
    assert students_summary(client, ADMIN_TOKEN, offering["offering_id"]) == _summary(offering, no_invite=3)
    assert _events(sql) == before

    prepared = _ok(client.post(f"/v1/offerings/{offering['code']}/archive/prepare", headers=ADMIN),
                   "offerings.prepare_archive")
    archived = _ok(client.post(f"/v1/offerings/{offering['code']}/archive",
                               json={"confirmation_code": prepared["confirmation_code"]}, headers=ADMIN),
                   "offerings.archive")
    assert archived["state"] == "archived"
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == _summary(offering, no_invite=3)

    for ref in ("adesao-inexistente", MISSING_OFFERING_ID):
        assert_error(_get_summary(client, ref), 404, "not_found")
    resp = _get_summary(client, "Oferta_Formato_Invalido")
    assert_error(resp, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_no_echo(resp, "Oferta_Formato_Invalido")
    resp = _get_summary(client, offering["code"], params={"parametro_sentinela": "valor_sentinela"})
    assert_error(resp, 400, "invalid_request", fields=[("query", "unknown_field")])
    assert_no_echo(resp, "parametro_sentinela", "valor_sentinela")


def test_summary_requires_offerings_read(client: Any, sql: Any, bot_api: Any) -> None:
    """Permissão `offerings:read` (sem permissão própria); ordem da API base (401 →
    400 → 403 → regras): sem token ou com token desconhecido → 401; um token com `offerings:write`,
    `people:read` e `bots:manage` (sem `offerings:read`) → 403 com `permission = offerings:read`,
    inclusive num oferecimento inexistente; o `ref` fora do formato é 400 antes do 403; um token só com
    `offerings:read` lê a adesão, com o mesmo corpo do token inicial."""
    offering, _ = _offering(client, bot_api, "adesao-permissao")
    _import(client, offering, [_student("Permissao")])
    reader, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="leitura de oferecimento")
    other, _ = create_token(client, ADMIN_TOKEN, ["offerings:write", "people:read", "bots:manage"],
                            label="sem leitura de oferecimento")
    expected = _summary(offering, no_invite=1)

    assert students_summary(client, reader, offering["code"]) == expected
    assert students_summary(client, ADMIN_TOKEN, offering["code"]) == expected
    for token in (None, "cq_" + "z" * 43):
        assert_error(_get_summary(client, offering["code"], token=token), 401, "unauthenticated")
    for ref in (offering["code"], "adesao-inexistente"):
        assert_error(_get_summary(client, ref, token=other), 403, "forbidden", permission="offerings:read")
    assert_error(_get_summary(client, "Oferta_Formato_Invalido", token=other), 400, "invalid_request",
                 fields=[("ref", "invalid_format")])
