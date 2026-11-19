"""Mensagens livres com redação (texto livre com redação automática; resposta fixa com limitador;
prioridade das respostas fixas; consulta própria por oferecimento).

Regras: a tabela `free_messages` (texto **redigido na gravação**, `replied`, `ambiguous`,
sem evento), `outbox_messages.priority` (1 só nas respostas fixas às mensagens livres), a ligação
do texto a um campo ("responder", campo digitável único, nenhum → mensagem livre, mais de um →
mensagem livre + "use responder"), a resposta fixa com **um limitador por tipo** (o
`FREE_MESSAGE_REPLY` e o `USE_REPLY`, cada um no máximo uma vez por intervalo e por endereço),
`DeliverySettings.free_reply_seconds` (padrão 600, a configuração inteira chega ao worker), os desfechos `free_message` e
`free_message_ambiguous` e os textos; `messages.list` (`{offering, items:
[{pseudo_id, alias, text, created_at}], next_cursor}`, paginado por `id`, o texto redigido de novo na
saída) e o 404 do oferecimento em qualquer estado.

Tudo passa pelo caminho real, só com dublês: o worker com `bots` (fixture `bot_worker_thread`), o motor
falso estrito (`engine_fake`, pelo adaptador real `HttpQuestionEngine`), a obtenção falsa
(`repo_fetcher`) e a Bot API falsa com a extensão do disparo (`rich_bot_api`). Os disparos são preparados e
confirmados pela API; a validação, a abertura e a entrega correm no worker. O aluno fala com o bot pela
Bot API falsa. O vínculo do aluno é montado por SQL, como `test_outbox_routes.py` faz com os
endereços: a lista entra pela API (`students.import`, que grava a identidade usada pela redação), e a
matrícula passa a `active` com o termo aceito e um endereço `active` no bot (o fluxo do `/start` e do
`/aceito` é do vínculo do aluno e fica em `test_student_join.py`). Quando o bot precisa escrever primeiro (a
questão), o aluno manda antes um `/start` sem código (resposta `HELP` do bot), para que o chat seja
conhecido da Bot API falsa.

Os textos esperados são os fixos do bot, escritos por extenso. Identidades neutras (fora de
`ALL_PREFIXES` e de `STUDENT_PREFIXES`); remetentes neutros (`make_user`). Horários pelo `FakeClock`
(`iso`); ids internos, cursores, `seq` e pseudônimos nunca como literal: o pseudônimo e o apelido vêm do
banco pelo RA, e os cursores de `messages.list` são os `id` de `free_messages` lidos por SQL.

No esqueleto, toda função falha por um destes motivos: a tabela
`free_messages` (ou a coluna `outbox_messages.priority`) ausente, a rota `dispatches.prepare` ou
`messages.list` ausente (404), ou o desfecho `free_message` ainda não gravado (o vínculo do aluno grava
`not_handled`). As asserções sobre o não aluno (que fica em `not_handled`) ficam na mesma
função do controle positivo do disparo. O caso do intervalo configurado (`free_reply_seconds=60`) só
fica verde quando entram as partes implementadas depois.

Mensagens livres opcionais por oferecimento, desligadas por padrão.
O
oferecimento tem a configuração "mensagens livres" (desligada por padrão) e o "contato do professor"
(`offerings.free_messages`, `offering_contacts`). Os sete casos acima são
do modo **ligado**: o `_setup` liga as mensagens livres **no
cadastro** (`free_messages=True`; `None` omite o campo, que é o padrão desligado), o que mantém o
`offering.created` como único evento do cadastro. Os demais casos são do modo **desligado**:
nenhuma linha em `free_messages`; o `telegram_updates.text` do update vira `[mensagem livre]`
(e o `command`, `NULL`) no mesmo `UPDATE` que o marca `processed`; a resposta
`answers_only_reply(email, buttons_open=…)` ao texto livre e `use_reply_masked(email)` ao ambíguo,
com os dois limitadores sobre os desfechos dos updates (o do contato e o do pedido de usar
"responder"; o e-mail sai ao aluno no máximo uma vez por intervalo); os cinco desfechos de
`answers.MASKED_OUTCOMES` (`free_message_masked_replied`, `free_message_masked`,
`free_message_ambiguous_masked_replied`, `free_message_ambiguous_masked_replied_plain`,
`free_message_ambiguous_masked`); a configuração lida no processamento; a troca e a remoção do
contato pela operação `offerings.update_settings` (`PATCH /v1/offerings/{ref}/settings`,
evento `offering.settings_changed`). Vermelho no esqueleto: o cadastro com `free_messages`
ou `contact_email` dá 400 `[{body, unknown_field}]`; sem esses campos, o texto vira
`free_message` com linha gravada, `FREE_MESSAGE_REPLY` e texto literal; a rota `PATCH .../settings` não
existe.

A mensagem livre num update
que não chega a ser processado com sucesso (nenhuma resposta perdida): a máscara só vale no
processamento confirmado. Um gatilho de teste no banco faz falhar, na mesma transação e depois da
classificação, o `UPDATE` que marca o update `processed` com o desfecho mascarado; o update que falha de vez
e depois é abandonado pela varredura fica com o `text` e o `command` literais, e o mesmo tipo de texto
processado com sucesso fica mascarado (o controle positivo, vermelho no esqueleto junto com o cadastro).
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import psycopg
import pytest

from cq_server.adapters.console import ConsoleChannel
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    auth,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    import_students,
    list_free_messages,
    marker,
    prepare_dispatch,
    register_bot,
    register_person,
    update_offering_settings,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error

ADMIN = auth(ADMIN_TOKEN)
WORKER = "worker-mensagens-livres"
COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
PROCESS_TIMEOUT = 20.0  # inclui a subida do worker (esquema, posse, perfil) e as três etapas da validação
ENGINE_TIMEOUT = 10.0  # dentro do invariante `timeout + 15 <= lease_seconds` (30 s)

# A questão dos disparos: um repositório no host de `TEST_REPO_SETTINGS` (a fixture `app`), um commit
# SHA-1 e os QIDs dos presets do motor falso, que existem na árvore `course` da obtenção falsa.
REPO = "https://repos.cq.test/disciplina/questoes-mensagens-livres.git"
COMMIT = "f7ee" * 10

# O ritmo curto (a configuração inteira chega ao worker) para os casos que não medem o ritmo.
FAST = DeliverySettings(chat_interval_seconds=0.05)
# O caso da prioridade: o intervalo por chat segura as mensagens seguintes ao mesmo chat.
PACE_SECONDS = 6.0

GROUP_CHAT = {"id": -4001234611, "type": "group", "title": "Turma de Teste das Mensagens Livres"}

# Os textos de entrega (`deliverytexts`).
FREE_MESSAGE_REPLY = ('Recebi a sua mensagem, mas ela não foi tomada como resposta. Para responder, use os '
                      'botões da questão ou toque em "Responder" na mensagem da questão.')
USE_REPLY = ('Há mais de uma questão aberta esperando texto. Toque em "Responder" na mensagem da questão '
             'certa e digite a resposta.')
# Modo desligado: a resposta, as frases que entram nela e a marca fixa do
# texto do update classificado como livre.
ANSWERS_ONLY_REPLY = "Este canal recebe só respostas às questões."
USE_BUTTONS_HOWTO = 'Para responder, use os botões da questão ou toque em "Responder" na mensagem dela.'
CONTACT_HOWTO = "Para falar com o professor, escreva para {email}."
FREE_MESSAGE_MASK = "[mensagem livre]"
# Contatos de professor fictícios (fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`).
PROF = "prof.livres@docentes.example.org"
PROF_NEW = "prof.trocado@docentes.example.org"

# Os tipos de evento (da API base ao disparo), para ler o log inteiro depois de um cursor; os de bot e de posse
# (`bot.*`) ficam de fora das comparações, como no vínculo do aluno (os saltos do relógio renovam a posse).
CORE_EVENT_TYPES = ("person.registered", "token.created", "token.revoked", "identity.revealed",
                  "identity.reveal_denied", "task.failed", "outbox.failed")
OFFERING_BOT_EVENT_TYPES = ("offering.created", "offering.state_changed", "offering.archive_prepared",
                  "bot.registered", "bot.token_replaced", "bot.profile_updated", "bot.lease_changed",
                  "bot.access_changed", "team.link_created", "team.link_revoked", "team.joined",
                  "team.join_refused", "endpoint.blocked", "outbox.reply_failed")
STUDENT_LINK_EVENT_TYPES = ("student.list_imported", "student.invites_prepared", "student.invites_queued",
                  "student.invite_sent", "student.invite_failed", "student.invite_cancelled",
                  "student.invite_revoked", "student.joined")
DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                  "dispatch.cancelled", "dispatch.opened", "dispatch.closed", "delivery.delivered",
                  "delivery.not_delivered", "delivery.question_failed", "delivery.dont_know",
                  "submission.graded", "submission.internal_failure", "offering.settings_changed")
ALL_EVENT_TYPES = CORE_EVENT_TYPES + OFFERING_BOT_EVENT_TYPES + STUDENT_LINK_EVENT_TYPES + DISPATCH_EVENT_TYPES

_codes = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Row:
    """Uma linha neutra da lista (fora de `ALL_PREFIXES` e de `STUDENT_PREFIXES`)."""

    name: str
    ra: str
    email: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "ra": self.ra, "email": self.email}


def _row(n: int) -> _Row:
    return _Row(f"Pessoa Aluna Livre {n:02d}", f"57100001{n:02d}", f"aluna.livre{n:02d}@turma.example.org")


@dataclasses.dataclass(frozen=True)
class _Student:
    """Um aluno importado: a linha, o remetente do Telegram, o pseudônimo, o apelido e `people.id`."""

    row: _Row
    user: Any
    pseudo_id: str
    alias: str
    person_id: int

    @property
    def chat_id(self) -> int:
        return self.user.user_id


@dataclasses.dataclass(frozen=True)
class _Ctx:
    """O oferecimento ativo com bot e os alunos importados (os `linked` com vínculo e endereço)."""

    offering: dict
    tg: int
    bot_row: int
    students: tuple[_Student, ...]

    @property
    def code(self) -> str:
        return self.offering["code"]


def _setup(client: Any, sql: psycopg.Connection, clock: Any, rich: Any, rows: list[_Row], *,
           linked: tuple[int, ...], free_messages: bool | None = True,
           contact_email: str | None = None) -> _Ctx:
    """Oferecimento ativo com bot; a lista importada pela API; os alunos de `linked` (índices em
    `rows`) com a matrícula `active`, o termo aceito e um endereço `active` no bot, por SQL.

    Mensagens livres opcionais: `free_messages` e `contact_email` vão ao cadastro só quando não são `None` (`None`
    omite o campo: o padrão, desligado e sem contato)."""
    settings_fields = {key: value for key, value in (("free_messages", free_messages),
                                                     ("contact_email", contact_email)) if value is not None}
    offering = create_offering(client, ADMIN_TOKEN, code=f"mc102-livres-{next(_codes)}", course=COURSE,
                               term=TERM, timezone=ZONE, **settings_fields)
    tg = rich.add_bot()
    register_bot(client, ADMIN_TOKEN, offering["code"], rich.token_of(tg))
    activate_bot(client, ADMIN_TOKEN, offering["code"])
    imported = import_students(client, ADMIN_TOKEN, offering["code"], [r.as_dict() for r in rows])
    assert (imported["people_created"], imported["enrollments_created"]) == (len(rows), len(rows))
    offering_row = sql.execute("SELECT id FROM offerings WHERE offering_id = %s",
                               (uuid.UUID(offering["offering_id"]),)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE offering_id = %s", (offering_row,)).fetchone()[0]
    students = []
    for index, row in enumerate(rows):
        person_id, pseudo_id, alias = sql.execute(
            "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
            "WHERE i.ra = %s", (row.ra,)).fetchone()
        user = make_user(4100 + next(_codes))
        if index in linked:
            _link(sql, clock, offering_row=offering_row, bot_row=bot_row, person_id=person_id,
                  chat_id=user.user_id)
        students.append(_Student(row=row, user=user, pseudo_id=pseudo_id, alias=alias, person_id=person_id))
    return _Ctx(offering=offering, tg=tg, bot_row=bot_row, students=tuple(students))


def _link(sql: psycopg.Connection, clock: Any, *, offering_row: int, bot_row: int, person_id: int,
          chat_id: int) -> None:
    """O vínculo do aluno: matrícula `active` com o termo aceito e o endereço
    vigente `active` no bot do oferecimento."""
    now = clock.now()
    updated = sql.execute(
        "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
        "terms_accepted_at = %s WHERE person_id = %s AND offering_id = %s AND role = 'student'",
        (now, now, person_id, offering_row)).rowcount
    assert updated == 1
    sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)", (person_id, bot_row, chat_id, now, now))


def _engine(engine_fake: Any) -> HttpQuestionEngine:
    """O adaptador real do motor sobre o motor falso. Sem conexão persistente (uma conexão por
    chamada), por isso o teste não o fecha."""
    return HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=ENGINE_TIMEOUT))


def _start_worker(bot_worker_thread: Any, settings: Any, engine_fake: Any, repo_fetcher: Any, rich: Any,
                  ctx: _Ctx, *, delivery: DeliverySettings | None = FAST) -> Any:
    """O worker com bots, motor e obtenção (`run_worker(…, engine=, fetcher=, delivery=)`); sem
    `delivery`, o padrão do `run_worker` (`DeliverySettings()`). Espera a primeira consulta do bot."""
    kwargs: dict[str, Any] = {"settings": dataclasses.replace(settings, worker_id=WORKER),
                              "channel": ConsoleChannel(io.StringIO()), "engine": _engine(engine_fake),
                              "fetcher": repo_fetcher}
    if delivery is not None:
        kwargs["delivery"] = delivery
    thread = bot_worker_thread(**kwargs)
    rich.wait_for(lambda: rich.pending_polls(ctx.tg), timeout=PROCESS_TIMEOUT)
    return thread


def _updates(sql: psycopg.Connection, tg: int, update_id: int) -> list[tuple]:
    """(id, text, state, outcome, received_at, processed_at) do update do bot."""
    return sql.execute(
        "SELECT u.id, u.text, u.state, u.outcome, u.received_at, u.processed_at FROM telegram_updates u "
        "JOIN bots b ON b.id = u.bot_id WHERE b.telegram_bot_id = %s AND u.update_id = %s ORDER BY u.id",
        (tg, update_id)).fetchall()


def _processed(rich: Any, sql: psycopg.Connection, tg: int, update_id: int | None) -> tuple:
    """Espera o update gravado e processado; devolve a única linha dele."""
    assert update_id is not None, "o dublê descartou o update"

    def ready() -> list[tuple] | None:
        rows = _updates(sql, tg, update_id)
        return rows if rows and all(r[2] == "processed" for r in rows) else None

    rows = rich.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0]


def _last_outbox(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(id), 0) FROM outbox_messages").fetchone()[0]


def _settled(rich: Any, sql: psycopg.Connection, after: int) -> None:
    """Espera toda mensagem da fila depois de `after` em estado final."""
    rich.wait_for(lambda: sql.execute(
        "SELECT count(*) FROM outbox_messages WHERE id > %s AND state IN ('pending', 'leased')",
        (after,)).fetchone()[0] == 0, timeout=PROCESS_TIMEOUT)


def _say(rich: Any, sql: psycopg.Connection, ctx: _Ctx, student: _Student, text: str, *,
         stored: str | None = None, **kwargs: Any) -> tuple:
    """O aluno manda `text` ao bot; espera o processamento e as mensagens que ele enfileirou; devolve
    `(update_id, id da linha, desfecho, received_at, processed_at)` e confere o texto gravado: literal
    por padrão (limitação conhecida: a redação é só em `free_messages`), ou `stored` (no modo
    desligado, a marca `[mensagem livre]`)."""
    out = _last_outbox(sql)
    update_id = rich.push_text(ctx.tg, student.user, text, **kwargs)
    row = _processed(rich, sql, ctx.tg, update_id)
    assert row[1] == (text if stored is None else stored)
    _settled(rich, sql, out)
    return update_id, row[0], row[3], row[4], row[5]


def _first_contact(rich: Any, sql: psycopg.Connection, ctx: _Ctx, *students: _Student) -> None:
    """Um `/start` sem código de cada aluno (resposta `HELP` do bot), para que o chat seja conhecido da
    Bot API falsa antes de o bot escrever primeiro."""
    for student in students:
        _say(rich, sql, ctx, student, "/start", bot_command=True)


def _free_rows(sql: psycopg.Connection, ctx: _Ctx) -> list[tuple]:
    """(update_id, chat do endereço, pseudônimo, texto, replied, ambiguous, received_at, created_at) das
    mensagens livres do oferecimento, em ordem de `id`."""
    return sql.execute(
        """
        SELECT u.update_id, d.chat_id, p.pseudo_id::text, f.text, f.replied, f.ambiguous, f.received_at,
               f.created_at
          FROM free_messages f
          JOIN telegram_updates u ON u.id = f.telegram_update_id
          JOIN delivery_endpoints d ON d.id = f.delivery_endpoint_id
          JOIN people p ON p.id = d.person_id
          JOIN offerings o ON o.id = f.offering_id
         WHERE o.offering_id = %s
         ORDER BY f.id
        """, (uuid.UUID(ctx.offering["offering_id"]),)).fetchall()


def _free_ids(sql: psycopg.Connection) -> list[int]:
    return [row[0] for row in sql.execute("SELECT id FROM free_messages ORDER BY id").fetchall()]


def _plain_outbox(sql: psycopg.Connection, after: int) -> list[tuple]:
    """(id, texto, prioridade, estado, bot) das mensagens da fila sem entrega depois de `after`."""
    return sql.execute(
        "SELECT o.id, o.text, o.priority, o.state, b.telegram_bot_id FROM outbox_messages o "
        "JOIN bots b ON b.id = o.bot_id WHERE o.id > %s AND o.delivery_id IS NULL ORDER BY o.id",
        (after,)).fetchall()


def _chat_texts(rich: Any, ctx: _Ctx, student: _Student) -> list[str | None]:
    """Os textos das mensagens do bot no chat privado do aluno, na ordem de criação."""
    return [m.text for m in rich.messages(ctx.tg, student.chat_id)]


def _submissions(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT count(*) FROM submissions").fetchone()[0]


def _last_seq(sql: psycopg.Connection) -> int:
    return sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events_after(client: Any, cursor: int) -> list[dict]:
    """Os eventos depois do cursor, sem os `bot.*`, sem o `seq`."""
    return [{k: v for k, v in e.items() if k != "seq"} for e in all_events(client, ADMIN_TOKEN, *ALL_EVENT_TYPES)
            if e["seq"] > cursor and not e["type"].startswith("bot.")]


def _ref(offering: dict) -> dict:
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _prepare(client: Any, rich: Any, ctx: _Ctx, qid: str, *, closes_at: datetime) -> str:
    """Prepara um disparo imediato da questão `qid` e espera a validação levá-lo a `scheduled`, sem
    confirmação e com a prévia; devolve o `dispatch_id`."""
    prepared = prepare_dispatch(client, ADMIN_TOKEN, ctx.code, repo=REPO, commit=COMMIT, qid=qid,
                                closes_at=iso(closes_at))
    assert prepared["state"] == "validating"
    dispatch_id = prepared["dispatch_id"]

    def validated() -> dict | None:
        body = get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
        return body if body["state"] != "validating" else None

    body = rich.wait_for(validated, timeout=PROCESS_TIMEOUT)
    assert (body["state"], body["confirmed"], body["rejection"]) == ("scheduled", False, None)
    return dispatch_id


def _deliveries(client: Any, ctx: _Ctx, dispatch_id: str) -> dict:
    return get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)["deliveries"]


def _open(client: Any, rich: Any, ctx: _Ctx, dispatch_id: str, *, delivered: int) -> None:
    """Confirma o disparo imediato e espera as `delivered` entregas `delivered`."""
    confirmed = confirm_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)
    assert (confirmed["state"], confirmed["confirmed"]) == ("scheduled", True)
    rich.wait_for(lambda: _deliveries(client, ctx, dispatch_id)["delivered"] == delivered,
                  timeout=PROCESS_TIMEOUT)


# ---------------------------------------------------------------------------------------------
# Gravação redigida e resposta fixa
# ---------------------------------------------------------------------------------------------


def test_free_message_without_open_field_is_stored_redacted_and_answered(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Mensagem livre: texto redigido **na gravação** (o nome completo, o RA e o e-mail dos
    alunos do oferecimento trocados pelo pseudônimo, e de novo na saída); nenhum campo digitável
    aberto → mensagem livre.

    Um aluno vinculado sem disparo nenhum manda um texto com o nome e o e-mail de uma colega e o RA de
    outro aluno do oferecimento (os dois só importados), e o nome de uma pessoa ainda não cadastrada:
    - o update fica `processed` com o desfecho `free_message`, e o `telegram_updates.text` continua
      literal (limitação conhecida);
    - uma linha em `free_messages` com o texto redigido (cada identidade vira `[pessoa <apelido>]`; o
      nome desconhecido fica), `replied = true`, `ambiguous = false`, `received_at` e `created_at` do
      relógio, ligada ao endereço do autor;
    - a resposta fixa `FREE_MESSAGE_REPLY` exata pela fila, com `priority = 1`, enviada ao chat do
      autor (a única mensagem nova nele); nenhuma submissão e nenhum evento;
    - a pessoa cadastrada **depois** da gravação é redigida na saída de `messages.list` (redação de novo
      na saída), e a linha gravada não muda."""
    rows = [_row(1), _row(2), _row(3)]
    late = _Row("Pessoa Tardia Livre 09", "5710000199", "tardia.livre09@turma.example.org")
    ctx = _setup(client, sql, clock, rich_bot_api, rows, linked=(0,))
    author, colleague, other = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)

    clock.advance(5)
    t1 = clock.now()
    text = (f"Oi! A {colleague.row.name} (RA {other.row.ra}) pediu para avisar: escreva para "
            f"{colleague.row.email}. Também falei com {late.name}.")
    stored = (f"Oi! A {marker(colleague.alias)} (RA {marker(other.alias)}) pediu para avisar: escreva para "
              f"{marker(colleague.alias)}. Também falei com {late.name}.")
    update_id, _row_id, outcome, received_at, processed_at = _say(rich_bot_api, sql, ctx, author, text)
    assert (outcome, received_at, processed_at) == ("free_message", t1, t1)
    assert _free_rows(sql, ctx) == [(update_id, author.chat_id, author.pseudo_id, stored, True, False, t1, t1)]
    replies = _plain_outbox(sql, out)
    assert [r[1:] for r in replies] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, author) == [FREE_MESSAGE_REPLY]
    assert _submissions(sql) == 0
    assert _events_after(client, cursor) == []

    # Redação de novo na saída: a pessoa cadastrada depois da gravação sai redigida; a linha não muda.
    late_person = register_person(client, ADMIN_TOKEN, **late.as_dict())
    listed = list_free_messages(client, ADMIN_TOKEN, ctx.code)
    (free_id,) = _free_ids(sql)
    assert listed == {"offering": _ref(ctx.offering),
                      "items": [{"pseudo_id": author.pseudo_id, "alias": author.alias,
                                 "text": stored.replace(late.name, marker(late_person["alias"])),
                                 "created_at": iso(t1)}],
                      "next_cursor": free_id}
    assert _free_rows(sql, ctx) == [(update_id, author.chat_id, author.pseudo_id, stored, True, False, t1, t1)]


# ---------------------------------------------------------------------------------------------
# Resposta fixa no máximo uma vez por intervalo
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("delivery", "interval"), [
    (None, 600),
    (DeliverySettings(free_reply_seconds=60), 60),
], ids=["padrao-600s", "configurado-60s"])
def test_free_message_reply_at_most_once_per_interval_per_student(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, delivery: DeliverySettings | None,
        interval: int) -> None:
    """Limitador (no máximo uma vez por intervalo por aluno, com padrão de 10 minutos, configurável;
    as mensagens desse intervalo são guardadas do mesmo jeito): o `FREE_MESSAGE_REPLY`
    só sai sem `replied` do mesmo endereço, não ambígua, com `created_at > now − intervalo`; a configuração
    chega aos tratadores (com `run_worker(…, delivery=DeliverySettings(free_reply_seconds=60))`, ela chega a
    `store_free_message`; sem `delivery`, o padrão de 600 s).

    Dois alunos vinculados, sem disparo. No instante t0, A manda duas mensagens seguidas e B uma: A e B
    recebem a resposta fixa uma vez cada (o limitador é por endereço), a segunda de A não. Em
    t0 + intervalo − 1 s, um comando desconhecido de A (`/duvida …`, também mensagem livre) é
    guardado sem resposta; em t0 + intervalo, a resposta volta. Todas as cinco ficam em `free_messages`
    (com o `replied` de cada uma), cada update com o desfecho `free_message`, e a fila só tem as três
    respostas, com `priority = 1`."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(11), _row(12)], linked=(0, 1))
    a, b = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx, delivery=delivery)
    out = _last_outbox(sql)

    t0 = clock.advance(5)
    sent: list[tuple] = []
    for student, text, kwargs in ((a, "Professor, a lista vale nota?", {}),
                                  (a, "Pergunto porque não achei no site.", {}),
                                  (b, "Bom dia, turma.", {})):
        update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, student, text, **kwargs)
        assert (outcome, received_at) == ("free_message", t0)
        sent.append((update_id, student, text, t0))
    t_before = clock.set(t0 + timedelta(seconds=interval - 1))
    update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, a, "/duvida sobre a lista 3",
                                                        bot_command=True)
    assert (outcome, received_at) == ("free_message", t_before)
    sent.append((update_id, a, "/duvida sobre a lista 3", t_before))
    t_after = clock.set(t0 + timedelta(seconds=interval))
    update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, a, "Obrigado!")
    assert (outcome, received_at) == ("free_message", t_after)
    sent.append((update_id, a, "Obrigado!", t_after))

    replied = [True, False, True, False, True]
    assert _free_rows(sql, ctx) == [
        (update_id, student.chat_id, student.pseudo_id, text, flag, False, at, at)
        for (update_id, student, text, at), flag in zip(sent, replied, strict=True)]
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg)] * 3
    assert _chat_texts(rich_bot_api, ctx, a) == [FREE_MESSAGE_REPLY, FREE_MESSAGE_REPLY]
    assert _chat_texts(rich_bot_api, ctx, b) == [FREE_MESSAGE_REPLY]
    assert _submissions(sql) == 0


# ---------------------------------------------------------------------------------------------
# Prioridade na fila
# ---------------------------------------------------------------------------------------------


def test_free_message_reply_has_priority_one_behind_delivery_messages(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Prioridade (as mensagens de questão, os pedidos de campo, o feedback e o gabarito têm
    prioridade sobre as respostas fixas às mensagens livres): `priority` 1 só nas
    respostas fixas; a posse por `ORDER BY priority, id`, a primeira que o limitador permite;
    1 mensagem por intervalo por chat.

    Com o intervalo por chat de 6 s, um aluno vinculado recebe a questão de escolha única de um disparo
    (sem campo digitável: um texto dele é mensagem livre). Logo depois do envio, com o chat ainda
    segurado pelo limitador, ele manda um texto livre (a resposta fixa entra na fila **antes**) e a
    coordenação confirma um segundo disparo (a questão dele entra **depois**). Quando o chat libera, a
    questão sai primeiro e a resposta fixa por último: `priority` 1 contra 0, e o `id` menor da
    resposta fixa não a adianta."""
    engine_fake.program("single_choice", "single_choice")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(21)], linked=(0,))
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx,
                  delivery=DeliverySettings(chat_interval_seconds=PACE_SECONDS))
    t0 = clock.now()
    first = _prepare(client, rich_bot_api, ctx, "single_choice", closes_at=t0 + timedelta(minutes=30))
    second = _prepare(client, rich_bot_api, ctx, "single_choice", closes_at=t0 + timedelta(minutes=30))
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, first, delivered=1)

    # O chat acabou de receber a questão do primeiro disparo: as próximas esperam o intervalo.
    out = _last_outbox(sql)
    update_id = rich_bot_api.push_text(ctx.tg, student.user, "Recebi, obrigado.")
    assert _processed(rich_bot_api, sql, ctx.tg, update_id)[3] == "free_message"
    confirm_dispatch(client, ADMIN_TOKEN, ctx.code, second)
    rich_bot_api.wait_for(lambda: _deliveries(client, ctx, second)["delivered"] == 1, timeout=PROCESS_TIMEOUT)
    _settled(rich_bot_api, sql, out)

    (reply,) = _plain_outbox(sql, out)
    assert reply[1:] == (FREE_MESSAGE_REPLY, 1, "sent", ctx.tg)
    questions = sql.execute(
        "SELECT o.id, o.priority, o.delivery_role, o.state, o.channel_ref FROM outbox_messages o "
        "JOIN deliveries d ON d.id = o.delivery_id JOIN dispatches x ON x.id = d.dispatch_id "
        "WHERE x.dispatch_uuid = %s ORDER BY o.id", (uuid.UUID(second),)).fetchall()
    assert [q[1:4] for q in questions] == [(0, "question", "sent")]
    assert reply[0] < questions[0][0], "a resposta fixa entrou na fila antes da questão"
    reply_ref = sql.execute("SELECT channel_ref FROM outbox_messages WHERE id = %s", (reply[0],)).fetchone()[0]
    by_ref = {str(m.message_id): m for m in rich_bot_api.messages(ctx.tg, student.chat_id)}
    question_message, reply_message = by_ref[questions[0][4]], by_ref[reply_ref]
    assert reply_message.text == FREE_MESSAGE_REPLY
    assert question_message.call_seq < reply_message.call_seq, (
        "a resposta fixa (prioridade 1) saiu antes da questão do segundo disparo (prioridade 0)")


# ---------------------------------------------------------------------------------------------
# Campo digitável só de disparo fechado
# ---------------------------------------------------------------------------------------------


def test_typed_field_only_from_closed_dispatch_is_a_free_message_not_late(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Campo digitável só de disparo fechado (para ligar um texto a um campo, só contam as entregas `delivered`
    de disparo `open` com `closes_at > received_at`; a atrasada exige "responder" ou os botões).

    Um aluno recebe uma questão numérica (campo digitável) e não responde; o disparo fecha (a entrega
    vira `no_answer`). Depois, ele digita um número **sem** "responder": o texto é mensagem livre
    (`free_message`, `FREE_MESSAGE_REPLY`), nenhuma submissão é gravada, a entrega continua `no_answer`,
    e nenhum evento de submissão ou entrega sai."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(31)], linked=(0,))
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=10)
    dispatch_id = _prepare(client, rich_bot_api, ctx, "number", closes_at=closes_at)
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, dispatch_id, delivered=1)

    clock.set(closes_at + timedelta(seconds=1))
    # Fechamento pelo horário com o relógio avançando entre as rodadas (a barreira (b) do fechamento):
    # a espera parada no relógio não fecha o disparo.
    _closed_by_window(client, clock, rich_bot_api, ctx, dispatch_id)
    assert _deliveries(client, ctx, dispatch_id)["no_answer"] == 1
    _settled(rich_bot_api, sql, 0)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)
    before = len(_chat_texts(rich_bot_api, ctx, student))

    t2 = clock.advance(30)
    update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, student, "6.5")
    assert (outcome, received_at) == ("free_message", t2)
    assert _free_rows(sql, ctx) == [(update_id, student.chat_id, student.pseudo_id, "6.5", True, False, t2, t2)]
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student)[before:] == [FREE_MESSAGE_REPLY]
    assert _submissions(sql) == 0
    assert sql.execute("SELECT state FROM deliveries").fetchall() == [("no_answer",)]
    assert _events_after(client, cursor) == []


# ---------------------------------------------------------------------------------------------
# Quem não é aluno vinculado fica fora das mensagens livres (`not_handled`)
# ---------------------------------------------------------------------------------------------


def test_non_students_follow_the_bot_flow_and_store_no_free_message(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Quem não é aluno vinculado do oferecimento fica fora das mensagens livres:
    `answers.process_message` devolve `None` → `not_handled`.

    Um membro da equipe com endereço no bot, um desconhecido, um aluno só importado (matrícula
    `pending`, sem endereço) e um aluno vinculado escrevendo num **grupo** mandam texto sem comando:
    cada update fica `processed` com `not_handled`, sem resposta na fila e sem linha em
    `free_messages`. Controle positivo, na mesma função: o mesmo aluno vinculado, no chat privado, gera
    a mensagem livre com a resposta fixa."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(41), _row(42)], linked=(0,))
    linked, pending = ctx.students
    member = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Livre 43")
    member_user = make_user(4943)
    now = clock.now()
    member_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                             (uuid.UUID(member["pseudo_id"]),)).fetchone()[0]
    offering_row = sql.execute("SELECT offering_id FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone()[0]
    sql.execute("INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at) "
                "VALUES (%s, %s, 'team', 'active', %s, %s)", (member_row, offering_row, now, now))
    sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                (member_row, ctx.bot_row, member_user.user_id, now, now))
    stranger = make_user(4944)
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    for sender, text, kwargs in ((member_user, "Olá, sou da equipe.", {}),
                                 (stranger, "Oi, alguém aí?", {}),
                                 (pending.user, "Ainda não aceitei o termo.", {}),
                                 (linked.user, "Mensagem no grupo da turma.", {"chat": GROUP_CHAT})):
        update_id = rich_bot_api.push_text(ctx.tg, sender, text, **kwargs)
        row = _processed(rich_bot_api, sql, ctx.tg, update_id)
        assert row[1:] == (text, "processed", "not_handled", t1, t1)
    _settled(rich_bot_api, sql, out)
    assert _free_rows(sql, ctx) == []
    assert _plain_outbox(sql, out) == []
    assert rich_bot_api.messages(ctx.tg) == []

    # Controle positivo: o aluno vinculado, no chat privado.
    update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, linked, "Mensagem no privado.")
    assert (outcome, received_at) == ("free_message", t1)
    assert _free_rows(sql, ctx) == [(update_id, linked.chat_id, linked.pseudo_id, "Mensagem no privado.", True,
                                     False, t1, t1)]
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg)]
    assert [(m.chat_id, m.text) for m in rich_bot_api.messages(ctx.tg)] == [(linked.chat_id, FREE_MESSAGE_REPLY)]
    assert _events_after(client, cursor) == []


# ---------------------------------------------------------------------------------------------
# Consulta própria
# ---------------------------------------------------------------------------------------------


def test_messages_list_pages_redacted_items_without_events(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Consulta própria por oferecimento, paginada, com o texto redigido, o pseudônimo do autor e
    o horário: `after`, `limit`, `next_cursor` = `id` do último item, ou
    `after` sem itens; ordem por `id`; 404 do oferecimento; o oferecimento em qualquer estado;
    sem evento.

    Três mensagens livres de dois alunos (a terceira com o nome de um terceiro aluno do oferecimento):
    `limit = 2` devolve as duas primeiras e o cursor da segunda; `after` com ele devolve a terceira,
    redigida; depois dela, a página vazia devolve o próprio `after`; sem parâmetros, as três. A consulta
    pelo `offering_id` devolve o mesmo. Um oferecimento em rascunho sem mensagens → lista vazia com
    `next_cursor = 0`; um oferecimento inexistente → 404 `not_found`. Nenhum evento sai das mensagens
    livres nem das consultas."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(51), _row(52), _row(53)], linked=(0, 1))
    a, b, c = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    cursor = _last_seq(sql)

    said = []
    for student, text in ((a, "Primeira mensagem."), (b, "Segunda mensagem."),
                          (a, f"Terceira: a {c.row.name} faltou hoje.")):
        clock.advance(10)
        _say(rich_bot_api, sql, ctx, student, text)
        said.append((student, text, clock.now()))
    ids = _free_ids(sql)
    assert len(ids) == 3
    items = [{"pseudo_id": student.pseudo_id, "alias": student.alias, "text": text, "created_at": iso(at)}
             for student, text, at in said]
    items[2]["text"] = f"Terceira: a {marker(c.alias)} faltou hoje."

    ref = _ref(ctx.offering)
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code, limit=2) == {
        "offering": ref, "items": items[:2], "next_cursor": ids[1]}
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code, after=ids[1], limit=2) == {
        "offering": ref, "items": items[2:], "next_cursor": ids[2]}
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code, after=ids[2]) == {
        "offering": ref, "items": [], "next_cursor": ids[2]}
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code) == {
        "offering": ref, "items": items, "next_cursor": ids[2]}
    assert list_free_messages(client, ADMIN_TOKEN, ctx.offering["offering_id"]) == {
        "offering": ref, "items": items, "next_cursor": ids[2]}

    draft = create_offering(client, ADMIN_TOKEN, code=f"mc102-rascunho-livres-{next(_codes)}")
    assert list_free_messages(client, ADMIN_TOKEN, draft["code"]) == {
        "offering": _ref(draft), "items": [], "next_cursor": 0}
    assert_error(client.get("/v1/offerings/mc102-inexistente-livres/messages", headers=ADMIN), 404, "not_found")
    assert [e["type"] for e in _events_after(client, cursor)] == ["offering.created"]


# ---------------------------------------------------------------------------------------------
# Texto ambíguo: "use responder" com limitador próprio
# ---------------------------------------------------------------------------------------------


def test_ambiguous_text_gets_use_reply_once_with_its_own_limiter(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Ambíguo (com mais de um campo digitável aberto e texto sem "responder", o texto não vai a
    campo nenhum: fica como mensagem livre, e o bot pede que o aluno use "responder"): a ligação a um campo
    com mais de uma entrega, um limitador por tipo (o `USE_REPLY` só sai sem `replied`
    ambígua do mesmo endereço no intervalo) e o desfecho `free_message_ambiguous`.

    O aluno manda uma mensagem livre sem questão aberta (`FREE_MESSAGE_REPLY`). Dentro do mesmo
    intervalo, dois disparos simultâneos abrem com campos digitáveis (número e inteiro). Um texto sem
    "responder" → `free_message_ambiguous`, `ambiguous = true`, `replied = true` e o `USE_REPLY` sai,
    apesar da resposta fixa recente (limitador próprio); um segundo texto ambíguo no mesmo intervalo é
    guardado sem repetir o `USE_REPLY`. Nenhuma submissão; as duas entregas continuam `delivered`."""
    engine_fake.program("number", "number")
    engine_fake.program("integer", "integer")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(61)], linked=(0,))
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    number = _prepare(client, rich_bot_api, ctx, "number", closes_at=t0 + timedelta(minutes=30))
    integer = _prepare(client, rich_bot_api, ctx, "integer", closes_at=t0 + timedelta(minutes=30))
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    first, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Bom dia!")
    assert outcome == "free_message"
    _open(client, rich_bot_api, ctx, number, delivered=1)
    _open(client, rich_bot_api, ctx, integer, delivered=1)
    _settled(rich_bot_api, sql, 0)

    t2 = clock.advance(60)
    ambiguous, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "42")
    assert outcome == "free_message_ambiguous"
    t3 = clock.advance(30)
    again, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "43")
    assert outcome == "free_message_ambiguous"

    assert _free_rows(sql, ctx) == [
        (first, student.chat_id, student.pseudo_id, "Bom dia!", True, False, t1, t1),
        (ambiguous, student.chat_id, student.pseudo_id, "42", True, True, t2, t2),
        (again, student.chat_id, student.pseudo_id, "43", False, True, t3, t3)]
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg),
                                                        (USE_REPLY, 1, "sent", ctx.tg)]
    texts = _chat_texts(rich_bot_api, ctx, student)
    assert [t for t in texts if t in (FREE_MESSAGE_REPLY, USE_REPLY)] == [FREE_MESSAGE_REPLY, USE_REPLY]
    assert texts[-1] == USE_REPLY
    assert _submissions(sql) == 0
    assert (_deliveries(client, ctx, number)["delivered"], _deliveries(client, ctx, integer)["delivered"]) == (1, 1)


# =============================================================================================
# Modo desligado (mensagens livres opcionais, desligadas por padrão)
# =============================================================================================


def _answers_only(email: str | None = None, *, buttons_open: bool = False) -> str:
    """`deliverytexts.answers_only_reply(email, buttons_open=…)` por extenso:
    `ANSWERS_ONLY_REPLY`, mais `USE_BUTTONS_HOWTO` com `buttons_open`, mais a frase do
    contato com e-mail; o e-mail literal, sem escape (texto puro)."""
    text = ANSWERS_ONLY_REPLY
    if buttons_open:
        text += " " + USE_BUTTONS_HOWTO
    if email is not None:
        text += " " + CONTACT_HOWTO.format(email=email)
    return text


def _use_reply_masked(email: str | None) -> str:
    """`deliverytexts.use_reply_masked(email)` por extenso: o `USE_REPLY`
    seguido da frase do contato, sozinho sem contato."""
    return USE_REPLY if email is None else USE_REPLY + " " + CONTACT_HOWTO.format(email=email)


def _update_cols(sql: psycopg.Connection, tg: int, update_id: int) -> tuple:
    """(text, command, state, outcome) do update do bot."""
    return sql.execute(
        "SELECT u.text, u.command, u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (tg, update_id)).fetchone()


def _principal(sql: psycopg.Connection, dispatch_id: str, student: _Student) -> int:
    """O `message_id` da mensagem principal (a da questão) da entrega do aluno no disparo."""
    ref = sql.execute(
        "SELECT o.channel_ref FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
        "JOIN enrollments e ON e.id = d.enrollment_id JOIN outbox_messages o ON o.id = d.principal_message_id "
        "WHERE x.dispatch_uuid = %s AND e.person_id = %s", (uuid.UUID(dispatch_id), student.person_id)).fetchone()
    assert ref is not None and ref[0] is not None, "a mensagem da questão ainda não saiu"
    return int(ref[0])


def _fixed_replies(rich: Any, ctx: _Ctx, student: _Student, *expected: str) -> list[str | None]:
    """Os textos do chat do aluno que são respostas fixas do conjunto `expected` (descarta a questão, os
    pedidos de campo e o `HELP` do `/start`)."""
    wanted = set(expected)
    return [t for t in _chat_texts(rich, ctx, student) if t in wanted]


def _settings_events(client: Any, cursor: int) -> list[tuple[str, dict]]:
    """(tipo, dados) dos eventos depois do cursor (sem os `bot.*`)."""
    return [(e["type"], e["data"]) for e in _events_after(client, cursor)]


class _Gate:
    """Embrulha o tratador `telegram.update` do worker: fechada, segura a tarefa em curso até `release`;
    aberta, só repassa (como em `test_dont_know.py`)."""

    def __init__(self, handler: Callable[[Any], None]) -> None:
        self._handler = handler
        self._open = threading.Event()
        self._open.set()
        self.entered = threading.Event()

    def close(self) -> None:
        self.entered.clear()
        self._open.clear()

    def release(self) -> None:
        self._open.set()

    def __call__(self, task: Any) -> None:
        if not self._open.is_set():
            self.entered.set()
            self._open.wait(3 * PROCESS_TIMEOUT)
        self._handler(task)


@pytest.fixture
def gated_updates(settings: Any, clock: Any, bots: Any) -> Iterator[_Gate]:
    """O tratador `telegram.update` montado como o `run_worker` o monta, com o ritmo curto
    `FAST`, atrás de uma `_Gate`; passado ao worker por `handlers=`."""
    from cq_server import reception
    from cq_server.bots import TelegramChannel

    channel = TelegramChannel(settings.database_url, clock=clock, runtime=bots,
                              default=ConsoleChannel(io.StringIO()), delivery=FAST)
    handler = reception.update_task_handler(settings.database_url, clock=clock, runtime=bots,
                                            settings=settings, channel=channel, delivery=FAST)
    gate = _Gate(handler)
    yield gate
    gate.release()
    handler.close()
    channel.close()


def _start_gated(bot_worker_thread: Any, settings: Any, engine_fake: Any, repo_fetcher: Any, rich: Any,
                 ctx: _Ctx, gate: _Gate) -> Any:
    """O worker como em `_start_worker`, com o tratador `telegram.update` atrás da `gate`."""
    thread = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER),
                               channel=ConsoleChannel(io.StringIO()), engine=_engine(engine_fake),
                               fetcher=repo_fetcher, delivery=FAST, handlers={"telegram.update": gate})
    rich.wait_for(lambda: rich.pending_polls(ctx.tg), timeout=PROCESS_TIMEOUT)
    return thread


# ---------------------------------------------------------------------------------------------
# Texto livre no padrão desligado
# ---------------------------------------------------------------------------------------------


def test_off_by_default_free_text_gets_answers_only_reply_and_is_masked(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Desligado por padrão: o texto livre recebe a resposta fixa e é mascarado; prioridade da resposta;
    a máscara, o modo desligado, os desfechos e os textos.

    Cadastro **sem** `free_messages` (o padrão) e com o contato do professor; um aluno vinculado sem
    campo aberto manda um texto com o nome, o RA e o e-mail de colegas:
    - o update fica `processed` com `free_message_masked_replied` e `telegram_updates.text` vira
      `[mensagem livre]`;
    - nenhuma linha em `free_messages`; `messages.list` vazio;
    - uma mensagem na fila, `priority = 1`, com o texto exato "Este canal recebe só respostas às questões.
      Para falar com o professor, escreva para prof.livres@docentes.example.org.", no chat do aluno;
    - nenhuma submissão e nenhum evento depois do cadastro; o `Offering` mostra só os booleanos."""
    rows = [_row(71), _row(72), _row(73)]
    ctx = _setup(client, sql, clock, rich_bot_api, rows, linked=(0,), free_messages=None, contact_email=PROF)
    author, colleague, other = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    text = (f"Oi! A {colleague.row.name} (RA {other.row.ra}) pediu para avisar: escreva para "
            f"{colleague.row.email}.")
    update_id, _row_id, outcome, received_at, processed_at = _say(rich_bot_api, sql, ctx, author, text,
                                                                  stored=FREE_MESSAGE_MASK)
    assert (outcome, received_at, processed_at) == ("free_message_masked_replied", t1, t1)
    assert _update_cols(sql, ctx.tg, update_id) == (FREE_MESSAGE_MASK, None, "processed",
                                                    "free_message_masked_replied")
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, author) == [_answers_only(PROF)]
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code) == {
        "offering": _ref(ctx.offering), "items": [], "next_cursor": 0}
    assert _submissions(sql) == 0
    assert _events_after(client, cursor) == []
    assert (ctx.offering.get("free_messages"), ctx.offering.get("contact_email_set")) == (False, True)
    assert PROF not in json.dumps(ctx.offering, ensure_ascii=False)


def test_off_without_contact_reply_is_only_the_first_sentence(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Sem contato cadastrado, a resposta do modo desligado é só a primeira frase
    (`answers_only_reply(None)` = `ANSWERS_ONLY_REPLY`).

    Oferecimento no padrão (desligado, sem contato): o texto livre do aluno vinculado sai com
    `free_message_masked_replied`, mascarado, sem linha, e a resposta exata "Este canal recebe só
    respostas às questões." com `priority = 1`."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(74)], linked=(0,), free_messages=None)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    _update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, student, "Quando é a prova?",
                                                         stored=FREE_MESSAGE_MASK)
    assert (outcome, received_at) == ("free_message_masked_replied", t1)
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(ANSWERS_ONLY_REPLY, 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student) == [ANSWERS_ONLY_REPLY]
    assert _submissions(sql) == 0


# ---------------------------------------------------------------------------------------------
# Limitador do modo desligado
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("delivery", "interval"), [
    (None, 600),
    (DeliverySettings(free_reply_seconds=60), 60),
], ids=["padrao-600s", "configurado-60s"])
def test_off_reply_at_most_once_per_interval_per_student(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, delivery: DeliverySettings | None,
        interval: int) -> None:
    """Limitador no modo desligado (nenhuma guardada, todas mascaradas; a resposta no máximo uma vez por intervalo
    por aluno): o limitador do contato sobre os desfechos dos updates do
    mesmo bot e `chat_id` com `processed_at > now − intervalo`, e o `free_reply_seconds`.

    Desligado com contato; dois alunos. Em t0, A manda duas mensagens e B uma: A e B recebem a resposta
    com o contato uma vez cada (o limitador é por chat), a segunda de A sai `free_message_masked`. Em
    t0 + intervalo − 1 s, um comando desconhecido de A sai `free_message_masked` (com o `command` nulo);
    em t0 + intervalo, a resposta volta. Todos os updates mascarados; nenhuma linha em `free_messages`;
    a fila só com as três respostas, `priority = 1`."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(75), _row(76)], linked=(0, 1), free_messages=False,
                 contact_email=PROF)
    a, b = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx, delivery=delivery)
    out = _last_outbox(sql)

    t0 = clock.advance(5)
    outcomes = []
    for student, text in ((a, "Professor, a lista vale nota?"), (a, "Pergunto porque não achei no site."),
                          (b, "Bom dia, turma.")):
        _update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, student, text,
                                                             stored=FREE_MESSAGE_MASK)
        assert received_at == t0
        outcomes.append(outcome)
    clock.set(t0 + timedelta(seconds=interval - 1))
    command_id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "/duvida sobre a lista 3",
                                                       stored=FREE_MESSAGE_MASK, bot_command=True)
    outcomes.append(outcome)
    clock.set(t0 + timedelta(seconds=interval))
    _update_id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "Obrigado!",
                                                       stored=FREE_MESSAGE_MASK)
    outcomes.append(outcome)

    assert outcomes == ["free_message_masked_replied", "free_message_masked", "free_message_masked_replied",
                        "free_message_masked", "free_message_masked_replied"]
    assert _update_cols(sql, ctx.tg, command_id)[:2] == (FREE_MESSAGE_MASK, None)
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg)] * 3
    assert _chat_texts(rich_bot_api, ctx, a) == [_answers_only(PROF)] * 2
    assert _chat_texts(rich_bot_api, ctx, b) == [_answers_only(PROF)]
    assert _submissions(sql) == 0


# ---------------------------------------------------------------------------------------------
# Texto ambíguo no modo desligado
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("contact", [PROF, None], ids=["com-contato", "sem-contato"])
def test_off_ambiguous_text_gets_use_reply_masked_without_row(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, contact: str | None) -> None:
    """Ambíguo no modo desligado (nada é guardado e o texto do update é mascarado); o pedido de usar
    "responder", com limitador próprio; o e-mail do professor no máximo uma vez por intervalo; o limitador do
    pedido de usar "responder", o limitador do contato e os desfechos.

    Desligado, com e sem contato; dois disparos simultâneos com campos digitáveis (número e inteiro). Um
    texto sem "responder" → com contato, `free_message_ambiguous_masked_replied` e o `USE_REPLY` seguido
    da frase do contato; sem contato, `free_message_ambiguous_masked_replied_plain` e só o `USE_REPLY`;
    mascarado, sem linha. Um segundo texto ambíguo no mesmo intervalo → `free_message_ambiguous_masked`,
    sem resposta. Nenhuma submissão; as duas entregas continuam `delivered`. (A independência entre o
    limitador do pedido e o do contato está em `test_off_contact_email_goes_out_at_most_once_per_interval`.)"""
    engine_fake.program("number", "number")
    engine_fake.program("integer", "integer")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(77)], linked=(0,), free_messages=False,
                 contact_email=contact)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    number = _prepare(client, rich_bot_api, ctx, "number", closes_at=t0 + timedelta(minutes=30))
    integer = _prepare(client, rich_bot_api, ctx, "integer", closes_at=t0 + timedelta(minutes=30))
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, number, delivered=1)
    _open(client, rich_bot_api, ctx, integer, delivered=1)
    _settled(rich_bot_api, sql, 0)
    out = _last_outbox(sql)

    expected_outcome = ("free_message_ambiguous_masked_replied" if contact is not None
                        else "free_message_ambiguous_masked_replied_plain")
    clock.advance(60)
    _first, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "42", stored=FREE_MESSAGE_MASK)
    assert outcome == expected_outcome
    clock.advance(30)
    _again, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "43", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_ambiguous_masked"

    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_use_reply_masked(contact), 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student)[-1] == _use_reply_masked(contact)
    assert _submissions(sql) == 0
    assert (_deliveries(client, ctx, number)["delivered"], _deliveries(client, ctx, integer)["delivered"]) == (1, 1)


# ---------------------------------------------------------------------------------------------
# Texto que é resposta não é mascarado
# ---------------------------------------------------------------------------------------------


def test_off_masks_free_text_but_not_the_text_that_is_an_answer(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O texto que é resposta segue como resposta nos dois modos, sem máscara (a
    máscara nunca se aplica ao texto ligado a um campo); as regras de ligação a um campo
    valem antes da classificação como livre.

    Modo desligado (o padrão) e com contato, no mesmo teste:
    (1) sem campo aberto, um texto livre → `free_message_masked_replied`, `[mensagem livre]`, a resposta
        com o contato, nenhuma linha em `free_messages`;
    (2) aberta uma questão numérica, o número sem "responder" → `answer_recorded`, texto **literal**;
    (3) aberta uma questão de inteiro, o número por "responder" à questão → `answer_recorded`, literal.
    (2) e (3) são o controle de que a máscara não alcança o texto que é resposta."""
    engine_fake.program("number", "number")
    engine_fake.program("integer", "integer")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(78)], linked=(0,), free_messages=None, contact_email=PROF)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    number = _prepare(client, rich_bot_api, ctx, "number", closes_at=t0 + timedelta(minutes=30))
    integer = _prepare(client, rich_bot_api, ctx, "integer", closes_at=t0 + timedelta(minutes=30))
    _first_contact(rich_bot_api, sql, ctx, student)
    out = _last_outbox(sql)

    # (1) Texto livre, sem campo aberto.
    clock.advance(5)
    _update_id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Bom dia, professor.",
                                                       stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg)]

    # (2) O campo digitável único, sem "responder": resposta, texto literal.
    _open(client, rich_bot_api, ctx, number, delivered=1)
    _settled(rich_bot_api, sql, 0)
    clock.advance(5)
    typed, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "6.5")
    assert outcome == "answer_recorded"
    assert _update_cols(sql, ctx.tg, typed)[0] == "6.5"
    rich_bot_api.wait_for(lambda: _deliveries(client, ctx, number)["answered"] == 1, timeout=PROCESS_TIMEOUT)

    # (3) "Responder" à mensagem da questão: resposta, texto literal.
    _open(client, rich_bot_api, ctx, integer, delivered=1)
    _settled(rich_bot_api, sql, 0)
    clock.advance(5)
    replied, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "7",
                                                    reply_to_message_id=_principal(sql, integer, student))
    assert outcome == "answer_recorded"
    assert _update_cols(sql, ctx.tg, replied)[0] == "7"
    assert _free_rows(sql, ctx) == []
    assert _submissions(sql) == 2


# ---------------------------------------------------------------------------------------------
# Disparo fechado e comando desconhecido no modo desligado
# ---------------------------------------------------------------------------------------------


def test_off_closed_dispatch_text_without_reply_is_masked_and_redirected(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Disparo fechado no modo desligado (a resposta com o e-mail do contato, sem registro e com o update mascarado);
    só contam os disparos abertos para ligar o texto a um campo.

    Uma questão numérica não respondida; o disparo fecha (a entrega vira `no_answer`). O número digitado
    **sem** "responder" não é resposta atrasada: `free_message_masked_replied`, `[mensagem livre]`, a
    resposta com o contato, nenhuma linha, nenhuma submissão, a entrega continua `no_answer` e nenhum
    evento sai."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(79)], linked=(0,), free_messages=False, contact_email=PROF)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=10)
    dispatch_id = _prepare(client, rich_bot_api, ctx, "number", closes_at=closes_at)
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, dispatch_id, delivered=1)

    clock.set(closes_at + timedelta(seconds=1))
    # Fechamento pelo horário com o relógio avançando entre as rodadas (a barreira (b) do fechamento):
    # a espera parada no relógio não fecha o disparo.
    _closed_by_window(client, clock, rich_bot_api, ctx, dispatch_id)
    assert _deliveries(client, ctx, dispatch_id)["no_answer"] == 1
    _settled(rich_bot_api, sql, 0)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)
    before = len(_chat_texts(rich_bot_api, ctx, student))

    t2 = clock.advance(30)
    _update_id, _row_id, outcome, received_at, _at = _say(rich_bot_api, sql, ctx, student, "6.5",
                                                         stored=FREE_MESSAGE_MASK)
    assert (outcome, received_at) == ("free_message_masked_replied", t2)
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student)[before:] == [_answers_only(PROF)]
    assert _submissions(sql) == 0
    assert sql.execute("SELECT state FROM deliveries").fetchall() == [("no_answer",)]
    assert _events_after(client, cursor) == []


def test_off_unknown_command_from_student_is_masked_and_redirected(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O comando desconhecido dirigido ao bot é classificado como livre (a máscara
    cobre também o `command` do update): no mesmo `UPDATE`, `text` vira
    `[mensagem livre]` e `command` vira `NULL`.

    No padrão desligado (sem contato), o aluno vinculado manda `/ra123456 sobre a lista` como comando:
    `free_message_masked_replied`, `text = '[mensagem livre]'`, `command IS NULL` (o trecho digitado não
    fica), nenhuma linha e a resposta "Este canal recebe só respostas às questões."."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(80)], linked=(0,), free_messages=None)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    out = _last_outbox(sql)

    clock.advance(5)
    update_id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "/ra123456 sobre a lista",
                                                      stored=FREE_MESSAGE_MASK, bot_command=True)
    assert outcome == "free_message_masked_replied"
    assert _update_cols(sql, ctx.tg, update_id) == (FREE_MESSAGE_MASK, None, "processed",
                                                    "free_message_masked_replied")
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(ANSWERS_ONLY_REPLY, 1, "sent", ctx.tg)]


# ---------------------------------------------------------------------------------------------
# Quem não é aluno vinculado também fica fora das mensagens livres no modo desligado
# ---------------------------------------------------------------------------------------------


def test_off_non_students_follow_the_bot_flow_and_nothing_is_masked(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Em qualquer modo, nada é guardado como mensagem livre e nada é mascarado no modo desligado,
    para quem não é aluno vinculado.

    Desligado e com contato: um membro da equipe com endereço no bot, um desconhecido, um aluno só
    importado e o aluno vinculado escrevendo num grupo → `not_handled`, texto literal, nenhuma resposta
    com o contato. Controle positivo: o aluno vinculado, no privado, recebe a resposta com o contato e
    tem o update mascarado."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(81), _row(82)], linked=(0,), free_messages=False,
                 contact_email=PROF)
    linked, pending = ctx.students
    member = register_person(client, ADMIN_TOKEN, name="Pessoa Equipe Livre 83")
    member_user = make_user(4983)
    now = clock.now()
    member_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                             (uuid.UUID(member["pseudo_id"]),)).fetchone()[0]
    offering_row = sql.execute("SELECT offering_id FROM bots WHERE id = %s", (ctx.bot_row,)).fetchone()[0]
    sql.execute("INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at) "
                "VALUES (%s, %s, 'team', 'active', %s, %s)", (member_row, offering_row, now, now))
    sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                (member_row, ctx.bot_row, member_user.user_id, now, now))
    stranger = make_user(4984)
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    for sender, text, kwargs in ((member_user, "Olá, sou da equipe.", {}),
                                 (stranger, "Oi, alguém aí?", {}),
                                 (pending.user, "Ainda não aceitei o termo.", {}),
                                 (linked.user, "Mensagem no grupo da turma.", {"chat": GROUP_CHAT})):
        update_id = rich_bot_api.push_text(ctx.tg, sender, text, **kwargs)
        row = _processed(rich_bot_api, sql, ctx.tg, update_id)
        assert row[1:] == (text, "processed", "not_handled", t1, t1)
    _settled(rich_bot_api, sql, out)
    assert _free_rows(sql, ctx) == []
    assert _plain_outbox(sql, out) == []
    assert rich_bot_api.messages(ctx.tg) == []

    # Controle positivo: o aluno vinculado, no chat privado.
    _update_id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, linked, "Mensagem no privado.",
                                                       stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert _free_rows(sql, ctx) == []
    assert [(m.chat_id, m.text) for m in rich_bot_api.messages(ctx.tg)] == [(linked.chat_id, _answers_only(PROF))]


# ---------------------------------------------------------------------------------------------
# Configuração lida no processamento; troca de modo e de contato
# ---------------------------------------------------------------------------------------------


def test_mode_is_read_at_processing_and_switching_keeps_stored_messages(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any, gated_updates: _Gate) -> None:
    """A configuração vale para as mensagens processadas a partir daí; as já guardadas continuam
    na consulta; um evento registra a mudança;
    limitadores independentes por modo; a configuração
    mudada com mensagens na fila de recepção.

    Ligado no cadastro: A grava "Primeira." (`free_message`, `FREE_MESSAGE_REPLY`). `PATCH` desliga: A
    manda "Segunda." → `free_message_masked_replied`, mascarada, sem linha (o limitador do desligado não
    é segurado pela resposta do ligado); `messages.list` ainda mostra a primeira. `PATCH` religa:
    "Terceira." é gravada (`replied = false`, o limitador do ligado). Com o worker segurado, "Quarta."
    é gravada com a opção ligada; `PATCH` desliga antes do processamento; ela segue o desligado
    (`free_message_masked`, mascarada, sem linha). Três `offering.settings_changed`, sem o e-mail."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(84)], linked=(0,), free_messages=True)
    (student,) = ctx.students
    _start_gated(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx, gated_updates)
    cursor = _last_seq(sql)
    out = _last_outbox(sql)
    ref = ctx.offering["offering_id"]

    t1 = clock.advance(5)
    first, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Primeira.")
    assert outcome == "free_message"
    off = update_offering_settings(client, ADMIN_TOKEN, ctx.code, free_messages=False)
    assert (off["free_messages"], off["contact_email_set"]) == (False, False)

    clock.advance(10)
    second, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Segunda.",
                                                   stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    (free_id,) = _free_ids(sql)
    assert list_free_messages(client, ADMIN_TOKEN, ctx.code) == {
        "offering": _ref(ctx.offering),
        "items": [{"pseudo_id": student.pseudo_id, "alias": student.alias, "text": "Primeira.",
                   "created_at": iso(t1)}],
        "next_cursor": free_id}

    on = update_offering_settings(client, ADMIN_TOKEN, ctx.code, free_messages=True)
    assert on["free_messages"] is True
    t3 = clock.advance(10)
    third, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Terceira.")
    assert outcome == "free_message"

    # O update gravado com a opção ligada e processado depois de desligar segue o desligado.
    clock.advance(10)
    gated_updates.close()
    pending_out = _last_outbox(sql)
    fourth = rich_bot_api.push_text(ctx.tg, student.user, "Quarta.")
    rich_bot_api.wait_for(gated_updates.entered.is_set, timeout=PROCESS_TIMEOUT)
    update_offering_settings(client, ADMIN_TOKEN, ctx.code, free_messages=False)
    gated_updates.release()
    row = _processed(rich_bot_api, sql, ctx.tg, fourth)
    _settled(rich_bot_api, sql, pending_out)
    assert (row[1], row[3]) == (FREE_MESSAGE_MASK, "free_message_masked")

    assert _free_rows(sql, ctx) == [
        (first, student.chat_id, student.pseudo_id, "Primeira.", True, False, t1, t1),
        (third, student.chat_id, student.pseudo_id, "Terceira.", False, False, t3, t3)]
    assert _update_cols(sql, ctx.tg, second)[0] == FREE_MESSAGE_MASK
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(FREE_MESSAGE_REPLY, 1, "sent", ctx.tg),
                                                        (ANSWERS_ONLY_REPLY, 1, "sent", ctx.tg)]
    assert _settings_events(client, cursor) == [
        ("offering.settings_changed", {"offering_id": ref, "free_messages": False, "contact_email_set": False}),
        ("offering.settings_changed", {"offering_id": ref, "free_messages": True, "contact_email_set": False}),
        ("offering.settings_changed", {"offering_id": ref, "free_messages": False, "contact_email_set": False})]


def test_contact_change_applies_to_the_next_reply(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Definir, trocar ou remover o contato vale a partir daí; a resposta da operação mostra só se
    há contato (`contact_email` texto troca, `null` remove).

    Desligado com contato: a resposta leva o e-mail. Troca do contato → a próxima resposta (fora do
    intervalo) leva o e-mail novo; remoção (`contact_email: null`) → só a primeira frase. As respostas
    do `PATCH` nunca trazem o e-mail."""
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(85)], linked=(0,), free_messages=False, contact_email=PROF)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    out = _last_outbox(sql)

    t1 = clock.advance(5)
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Primeira dúvida.",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    changed = update_offering_settings(client, ADMIN_TOKEN, ctx.code, contact_email=PROF_NEW)
    assert (changed["free_messages"], changed["contact_email_set"]) == (False, True)
    assert PROF_NEW not in json.dumps(changed, ensure_ascii=False)

    t2 = clock.set(t1 + timedelta(seconds=601))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Segunda dúvida.",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    removed = update_offering_settings(client, ADMIN_TOKEN, ctx.code, contact_email=None)
    assert (removed["free_messages"], removed["contact_email_set"]) == (False, False)

    clock.set(t2 + timedelta(seconds=601))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "Terceira dúvida.",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg),
                                                        (_answers_only(PROF_NEW), 1, "sent", ctx.tg),
                                                        (ANSWERS_ONLY_REPLY, 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student) == [_answers_only(PROF), _answers_only(PROF_NEW),
                                                       ANSWERS_ONLY_REPLY]


def test_contact_that_became_a_student_identity_is_not_sent(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Contato que virou identidade (se um contato já guardado passar a coincidir com a identidade de um aluno cadastrado
    depois, a resposta do modo desligado sai sem a frase do contato): tratado como ausente na
    classificação; a identidade só sai pela revelação; `free_message_settings` confere o
    contato contra `load_write_identities`.

    Desligado com um contato aceito. Controle: o texto livre de A recebe a resposta com o contato.
    Depois, uma pessoa é cadastrada (`people.register`) com esse e-mail; o texto livre de B recebe só a
    primeira frase, e o e-mail não aparece em `outbox_messages.text` nem em chamada à Bot API falsa."""
    contact = "prof.depois@docentes.example.org"
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(86), _row(87)], linked=(0, 1), free_messages=False,
                 contact_email=contact)
    a, b = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    out = _last_outbox(sql)

    clock.advance(5)
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "Oi, professor.", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert _chat_texts(rich_bot_api, ctx, a) == [_answers_only(contact)], "controle: antes, a resposta tinha o contato"

    register_person(client, ADMIN_TOKEN, name="Pessoa Docente Depois 88", email=contact)
    after_out = _last_outbox(sql)
    last_call = max((c.seq for c in rich_bot_api.calls()), default=0)
    clock.advance(5)
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, b, "Oi, professor.", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert _chat_texts(rich_bot_api, ctx, b) == [ANSWERS_ONLY_REPLY]
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(contact), 1, "sent", ctx.tg),
                                                        (ANSWERS_ONLY_REPLY, 1, "sent", ctx.tg)]
    assert sql.execute("SELECT count(*) FROM outbox_messages WHERE id > %s AND position(%s in text) > 0",
                       (after_out, contact)).fetchone()[0] == 0
    leaked = [(c.method, c.seq) for c in rich_bot_api.calls()
              if c.seq > last_call and contact in json.dumps(c.params, ensure_ascii=False)]
    assert leaked == [], f"o contato que virou identidade saiu ao aluno: {leaked}"


# ---------------------------------------------------------------------------------------------
# O e-mail do contato no máximo uma vez por intervalo
# ---------------------------------------------------------------------------------------------


def test_off_contact_email_goes_out_at_most_once_per_interval(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """O e-mail do contato sai no máximo uma vez a cada 10 minutos por aluno:
    os limitadores (o do contato,
    conjunto {`free_message_masked_replied`, `free_message_ambiguous_masked_replied`}; limitador do
    pedido de usar "responder", conjunto {`free_message_ambiguous_masked_replied`,
    `free_message_ambiguous_masked_replied_plain`}) e os desfechos.

    Desligado com contato; dois alunos; dois disparos com campos digitáveis abertos:
    (1) A, antes da abertura, manda um texto livre → `free_message_masked_replied`, resposta com o contato;
    (2) aberto o par de disparos, A manda um ambíguo no mesmo intervalo → só o `USE_REPLY`,
        `free_message_ambiguous_masked_replied_plain` (a resposta ao texto livre não segura o pedido);
    (3) B manda primeiro o ambíguo → `USE_REPLY` com o contato (`free_message_ambiguous_masked_replied`);
        responde às duas questões por "responder" e, no intervalo, manda um texto livre →
        `free_message_masked`, sem resposta;
    (4) depois do intervalo, o contato volta: o texto livre de B e o ambíguo de A o trazem.
    O e-mail aparece no máximo uma vez por aluno em cada intervalo nas mensagens enviadas."""
    engine_fake.program("number", "number")
    engine_fake.program("integer", "integer")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(89), _row(90)], linked=(0, 1), free_messages=False,
                 contact_email=PROF)
    a, b = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    number = _prepare(client, rich_bot_api, ctx, "number", closes_at=t0 + timedelta(minutes=60))
    integer = _prepare(client, rich_bot_api, ctx, "integer", closes_at=t0 + timedelta(minutes=60))
    _first_contact(rich_bot_api, sql, ctx, a, b)

    # (1) Texto livre de A, sem campo aberto.
    t1 = clock.advance(5)
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "Professor, posso escrever aqui?",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    _open(client, rich_bot_api, ctx, number, delivered=2)
    _open(client, rich_bot_api, ctx, integer, delivered=2)
    _settled(rich_bot_api, sql, 0)

    # (2) Ambíguo de A no mesmo intervalo: o pedido sai, sem o contato.
    clock.set(t1 + timedelta(seconds=30))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "42", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_ambiguous_masked_replied_plain"

    # (3) B: primeiro o ambíguo (com o contato); depois, no intervalo, o texto livre fica sem resposta.
    t3 = clock.set(t1 + timedelta(seconds=60))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, b, "42", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_ambiguous_masked_replied"
    for dispatch_id, answer in ((number, "6.5"), (integer, "7")):
        _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, b, answer,
                                                    reply_to_message_id=_principal(sql, dispatch_id, b))
        assert outcome == "answer_recorded"
        rich_bot_api.wait_for(lambda d=dispatch_id: _deliveries(client, ctx, d)["answered"] == 1,
                              timeout=PROCESS_TIMEOUT)
    clock.set(t3 + timedelta(seconds=10))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, b, "Obrigado, professor.",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked"

    # (4) Depois do intervalo, o contato volta.
    clock.set(t3 + timedelta(seconds=601))
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, b, "Mais uma dúvida.",
                                                stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    _id, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, a, "43", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_ambiguous_masked_replied"

    replies = (_answers_only(PROF), USE_REPLY, _use_reply_masked(PROF))
    assert _fixed_replies(rich_bot_api, ctx, a, *replies) == [_answers_only(PROF), USE_REPLY, _use_reply_masked(PROF)]
    assert _fixed_replies(rich_bot_api, ctx, b, *replies) == [_use_reply_masked(PROF), _answers_only(PROF)]
    for student in (a, b):
        with_email = [t for t in _chat_texts(rich_bot_api, ctx, student) if t and PROF in t]
        assert len(with_email) == 2, (student.row.name, with_email)  # um em cada intervalo
    assert _free_rows(sql, ctx) == []
    assert _submissions(sql) == 2


# ---------------------------------------------------------------------------------------------
# Texto sem "responder" com só uma escolha com botões aberta
# ---------------------------------------------------------------------------------------------


def test_off_text_without_reply_to_a_buttoned_choice_is_masked_and_redirected(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Texto sem "responder" a uma escolha com botões, no modo desligado;
    a ligação a um campo: a escolha com botões não é campo
    digitável; e `answers_only_reply(email, buttons_open=True)`, com a frase `USE_BUTTONS_HOWTO`
    entre as duas.

    Desligado com contato; o aluno tem aberta só uma escolha única **com botões**. "b" sem "responder" →
    `free_message_masked_replied`, `[mensagem livre]`, a resposta "Este canal recebe só respostas às
    questões. Para responder, use os botões da questão ou toque em "Responder" na mensagem dela. Para
    falar com o professor, escreva para prof.livres@docentes.example.org.", nenhuma submissão. Controle:
    o mesmo "b" por "responder" à questão → `answer_recorded`, texto literal, uma submissão."""
    engine_fake.program("single_choice", "single_choice")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(91)], linked=(0,), free_messages=False, contact_email=PROF)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    dispatch_id = _prepare(client, rich_bot_api, ctx, "single_choice", closes_at=t0 + timedelta(minutes=30))
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, dispatch_id, delivered=1)
    _settled(rich_bot_api, sql, 0)
    out = _last_outbox(sql)

    clock.advance(5)
    _masked, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "b", stored=FREE_MESSAGE_MASK)
    assert outcome == "free_message_masked_replied"
    assert _free_rows(sql, ctx) == []
    expected = _answers_only(PROF, buttons_open=True)
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(expected, 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student)[-1] == expected
    assert _submissions(sql) == 0

    # Controle: por "responder" à questão, o mesmo "b" é resposta (escolha digitada), literal.
    clock.advance(5)
    answered, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student, "b",
                                                     reply_to_message_id=_principal(sql, dispatch_id, student))
    assert outcome == "answer_recorded"
    assert _update_cols(sql, ctx.tg, answered)[0] == "b"
    assert _submissions(sql) == 1


# ---------------------------------------------------------------------------------------------
# A máscara só no processamento confirmado
# ---------------------------------------------------------------------------------------------


def _update_tasks(sql: psycopg.Connection, update_row_id: int) -> list[tuple]:
    """(idempotency_key, state, attempts) das tarefas `telegram.update` do update, em ordem de `id`."""
    return [tuple(r) for r in sql.execute(
        "SELECT idempotency_key, state, attempts FROM tasks WHERE task_type = 'telegram.update' "
        "AND payload @> jsonb_build_object('update_row_id', %s::bigint) ORDER BY id",
        (update_row_id,)).fetchall()]


def _exhaust_attempts(rich: Any, sql: psycopg.Connection, clock: Any, settings: Any, key: str) -> None:
    """Conduz a tarefa `key` pelas `settings.max_attempts` tentativas, todas falhando, até `failed`: a cada
    falha temporária, espera a tarefa voltar a `pending` (sem posse, com a tentativa contada) e só então
    avança o relógio falso pela espera da fila (`min(base · 2^(n−1), max)`). Nada corre
    com o tempo real: sem o avanço, a tarefa não volta a ser pega; e, com ela viva, a varredura não
    enfileira outra rodada."""
    for attempt in range(1, settings.max_attempts + 1):
        final = attempt == settings.max_attempts
        wanted = ("failed" if final else "pending", attempt, None)

        def settled(wanted: tuple = wanted) -> tuple | None:
            row = sql.execute("SELECT state, attempts, lease_owner FROM tasks WHERE idempotency_key = %s",
                              (key,)).fetchone()
            return row if row is not None and tuple(row) == wanted else None

        rich.wait_for(settled, timeout=PROCESS_TIMEOUT)
        if not final:
            clock.advance(min(settings.backoff_base_seconds * 2 ** (attempt - 1), settings.backoff_max_seconds))


def _closed_by_window(client: Any, clock: Any, rich: Any, ctx: _Ctx, dispatch_id: str, *, rounds: int = 8) -> None:
    """Espera o fechamento pelo horário, com `closes_at` já passado no relógio (como
    `test_offerings_lifecycle_dispatches._wait_closed_by_window`): sem ele em 5 s reais, avança o relógio além de
    `CLOSE_RECHECK_SECONDS` (5 s), porque a rodada `dispatch.close` que rodou antes de a consulta do leitor
    passar de `closes_at` termina sem efeito e a seguinte só sai depois desse intervalo (a barreira (b)
    do fechamento). O resultado não depende do tempo real: só o quanto o relógio andou."""
    for _ in range(rounds):
        try:
            rich.wait_for(lambda: get_dispatch(client, ADMIN_TOKEN, ctx.code, dispatch_id)["state"] == "closed",
                          timeout=5)
            return
        except AssertionError:
            clock.advance(6)
    raise AssertionError(f"o disparo não fechou pelo horário em {rounds} rodadas")


def test_off_mask_only_on_confirmed_processing_not_on_failure_or_abandonment(
        client: Any, clock: Any, sql: psycopg.Connection, rich_bot_api: Any, bot_worker_thread: Any,
        settings: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """A máscara não vale para o update que não chega a ser processado com
    sucesso, inclusive o de uma mensagem livre; update
    abandonado sem evento; nenhuma resposta perdida (a entrada literal persistida); a recepção mascara o
    `text` e o `command` **no mesmo `UPDATE` que marca `processed`**; o abandono: `processed`,
    `abandoned`, sem evento.

    Desligado com contato; o aluno tem só a entrega `no_answer` de um disparo fechado (o que deixa o update
    sob a regra `telegram.update` da varredura). O mesmo tipo de texto, um comando desconhecido dirigido ao
    bot (classificado como livre), em três situações:
    (1) **falha depois da classificação**, na mesma transação: um gatilho de teste no banco faz falhar o
        `UPDATE` que grava o update `processed` com `free_message_masked_replied` (o desfecho já decidido
        pela classificação, com a resposta com o contato já posta na fila, na mesma transação). A tarefa
        da recepção esgota as tentativas e termina `failed`: o update continua `pending`, com o `text` e o
        `command` **literais**; nenhuma linha em `free_messages`; nenhuma mensagem na fila nem no chat;
        o gatilho disparou uma vez por tentativa (a classificação chegou ao fim em cada uma);
    (2) **abandono pela varredura**: as rodadas `r2` e `r3` falham do mesmo jeito; esgotadas as três
        rodadas `failed`, a varredura grava `processed` com `abandoned` e `processed_at = now`: o texto e o
        `command` continuam literais, e nada sai além dos três `task.failed`;
    (3) **controle positivo**: sem o gatilho, o mesmo tipo de texto processado com sucesso fica
        `free_message_masked_replied`, com `text = '[mensagem livre]'` e `command` nulo, e recebe a
        resposta com o contato (o abandono não ocupa o limitador do contato).
    Uma máscara gravada fora da transação que marca `processed` (num commit próprio na classificação, na
    reserva depois da falha ou no abandono) deixa (1) ou (2) vermelho; (3) é o que falha sem a máscara."""
    engine_fake.program("number", "number")
    ctx = _setup(client, sql, clock, rich_bot_api, [_row(92)], linked=(0,), free_messages=False, contact_email=PROF)
    (student,) = ctx.students
    _start_worker(bot_worker_thread, settings, engine_fake, repo_fetcher, rich_bot_api, ctx)
    t0 = clock.now()
    closes_at = t0 + timedelta(minutes=10)
    dispatch_id = _prepare(client, rich_bot_api, ctx, "number", closes_at=closes_at)
    _first_contact(rich_bot_api, sql, ctx, student)
    _open(client, rich_bot_api, ctx, dispatch_id, delivered=1)
    clock.set(closes_at + timedelta(seconds=1))
    _closed_by_window(client, clock, rich_bot_api, ctx, dispatch_id)
    assert _deliveries(client, ctx, dispatch_id)["no_answer"] == 1
    _settled(rich_bot_api, sql, 0)

    # A falha injetada: só no `UPDATE` que marca `processed` com o desfecho da classificação mascarada (o
    # abandono, com `abandoned`, passa). A sequência fica fora da transação desfeita e conta os disparos.
    sql.execute("CREATE SEQUENCE cq_test_masked_marks")
    sql.execute("CREATE FUNCTION cq_test_fail_masked_mark() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN PERFORM nextval('cq_test_masked_marks'); "
                "RAISE EXCEPTION 'falha de teste depois da classificacao'; END $$")
    sql.execute("CREATE TRIGGER cq_test_fail_masked_mark BEFORE UPDATE ON telegram_updates FOR EACH ROW "
                "WHEN (NEW.state = 'processed' AND NEW.outcome = 'free_message_masked_replied') "
                "EXECUTE FUNCTION cq_test_fail_masked_mark()")
    cursor = _last_seq(sql)
    out = _last_outbox(sql)
    before = len(_chat_texts(rich_bot_api, ctx, student))

    def marks() -> tuple:
        return tuple(sql.execute("SELECT last_value, is_called FROM cq_test_masked_marks").fetchone())

    # (1) Falha depois da classificação: a tarefa da recepção esgota as tentativas.
    t1 = clock.advance(30)
    failing = "/duvida sobre a nota da lista"
    update_id = rich_bot_api.push_text(ctx.tg, student.user, failing, bot_command=True)
    assert update_id is not None, "o dublê descartou o update"
    row_id = rich_bot_api.wait_for(lambda: (_updates(sql, ctx.tg, update_id) or [[None]])[0][0],
                                   timeout=PROCESS_TIMEOUT)
    _exhaust_attempts(rich_bot_api, sql, clock, settings, f"telegram.update:{row_id}")
    assert _update_tasks(sql, row_id) == [(f"telegram.update:{row_id}", "failed", settings.max_attempts)]
    assert marks() == (settings.max_attempts, True)
    assert _update_cols(sql, ctx.tg, update_id) == (failing, "/duvida", "pending", None)
    assert [r[1:] for r in _updates(sql, ctx.tg, update_id)] == [(failing, "pending", None, t1, None)]
    assert _free_rows(sql, ctx) == []
    assert _plain_outbox(sql, out) == []
    assert sql.execute("SELECT count(*) FROM outbox_messages WHERE id > %s AND strpos(text, %s) > 0",
                       (out, PROF)).fetchone()[0] == 0
    assert len(_chat_texts(rich_bot_api, ctx, student)) == before

    # (2) Abandono pela varredura: as rodadas `r2` e `r3` falham do mesmo jeito; depois, o abandono.
    for k in (2, 3):
        clock.advance(settings.backoff_max_seconds)
        _exhaust_attempts(rich_bot_api, sql, clock, settings, f"telegram.update:{row_id}:r{k}")
        assert _update_cols(sql, ctx.tg, update_id) == (failing, "/duvida", "pending", None), f"rodada r{k}"
    assert marks() == (3 * settings.max_attempts, True)
    abandoned_at = clock.advance(settings.backoff_max_seconds)
    abandoned = _processed(rich_bot_api, sql, ctx.tg, update_id)
    assert abandoned[1:] == (failing, "processed", "abandoned", t1, abandoned_at)
    assert _update_cols(sql, ctx.tg, update_id) == (failing, "/duvida", "processed", "abandoned")
    assert [(key, state) for key, state, _attempts in _update_tasks(sql, row_id)] == [
        (f"telegram.update:{row_id}", "failed"), (f"telegram.update:{row_id}:r2", "failed"),
        (f"telegram.update:{row_id}:r3", "failed")]
    assert _free_rows(sql, ctx) == []
    assert _plain_outbox(sql, out) == []
    assert sql.execute("SELECT count(*) FROM outbox_messages WHERE id > %s AND strpos(text, %s) > 0",
                       (out, PROF)).fetchone()[0] == 0
    assert len(_chat_texts(rich_bot_api, ctx, student)) == before
    assert [(e["type"], e["data"]["task_type"], e["data"]["attempts"]) for e in _events_after(client, cursor)] == [
        ("task.failed", "telegram.update", settings.max_attempts)] * 3

    # (3) Controle positivo: sem o gatilho, o mesmo tipo de texto, processado com sucesso, fica mascarado.
    sql.execute("DROP TRIGGER cq_test_fail_masked_mark ON telegram_updates")
    sql.execute("DROP FUNCTION cq_test_fail_masked_mark()")
    clock.advance(5)
    confirmed, _row_id, outcome, _received, _at = _say(rich_bot_api, sql, ctx, student,
                                                      "/duvida sobre a nota da prova",
                                                      stored=FREE_MESSAGE_MASK, bot_command=True)
    assert outcome == "free_message_masked_replied"
    assert _update_cols(sql, ctx.tg, confirmed) == (FREE_MESSAGE_MASK, None, "processed",
                                                    "free_message_masked_replied")
    assert _free_rows(sql, ctx) == []
    assert [r[1:] for r in _plain_outbox(sql, out)] == [(_answers_only(PROF), 1, "sent", ctx.tg)]
    assert _chat_texts(rich_bot_api, ctx, student)[before:] == [_answers_only(PROF)]
    # O update abandonado continua literal depois do processamento confirmado do outro.
    assert _update_cols(sql, ctx.tg, update_id) == (failing, "/duvida", "processed", "abandoned")
    assert _submissions(sql) == 0
