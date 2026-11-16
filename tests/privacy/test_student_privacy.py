"""Controle positivo da redação nas saídas novas do vínculo do aluno.


A redação da disciplina e do período tem controle positivo **fora do canário**, num
teste de privacidade à parte, para não alargar a varredura do banco do canário: aqui, um
oferecimento cuja disciplina e período contêm a identidade (nome, e-mail e RA) de uma pessoa cadastrada
no próprio teste. As saídas novas que levam a disciplina e o período, o assunto e o corpo do e-mail do
convite e os 4 textos novos do bot (`TERMS`, `TERMS_REFUSED`, `WELCOME_STUDENT` e
`INVITE_REQUESTED`), saem com a marca de redação da pessoa (`[pessoa <apelido>]`), e a
identidade não aparece em nenhuma forma (nome, palavras do nome, RA, e-mail e parte local), com
controle positivo do detector nos textos de entrada.

Tudo passa pelo caminho real, só com dublês: a lista entra pela API, o envio em duas fases enfileira a
tarefa `invite.email`, um worker com `bots` e `verification` (o adaptador SMTP real, fixture
`mail_channel`) manda o e-mail ao SMTP falso, e o aluno (neutro) fala com o bot pela Bot API falsa.
Horários pelo `FakeClock`; o código do convite é lido do link do e-mail (`smtp.invite_links()`, que o
registra em `remember_secret`), e o pseudônimo, o apelido e os ids nunca entram como literal.

No esqueleto, o teste falha na primeira chamada de
`students.import` (rota ausente, 404).

Mensagens livres opcionais: o
`Offering` traz `free_messages` e `contact_email_set`; o corpo exato esperado do oferecimento cadastrado sem
esses campos traz os dois, falsos (o padrão). Sem as chaves na resposta, a comparação falha.
"""

from __future__ import annotations

import dataclasses
import io
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from cq_server.adapters.console import ConsoleChannel
from cq_server.botmessages import (INVITE_REQUESTED, TERMS, TERMS_REFUSED, TERMS_VERSION,
                                   WELCOME_STUDENT)
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, create_offering, import_students, marker,
                               prepare_invites, register_bot, register_person, send_invites)
from tests.helpers.bot_api import make_user
from tests.helpers.canary import fold
from tests.helpers.clock import iso
from tests.helpers.smtp import SENDER

# A pessoa cuja identidade vai na disciplina e no período (cadastrada no próprio teste; neutra, fora
# dos alvos canário).
PERSON = {"name": "Rosalva Tupinambá Esteves", "ra": "5512340987", "email": "rosalva.esteves@redacao.example.org"}
# O aluno do oferecimento (neutro; nada dele entra na disciplina nem no período).
STUDENT = {"name": "Aluna Neutra da Redação", "ra": "6623450001", "email": "aluna.neutra@turma.example.org"}
OFFERING_CODE = "privacidade-redacao"
ZONE = "America/Sao_Paulo"
COURSE_TEMPLATE = "Oficina de {name} ({email})"
TERM_TEMPLATE = "2031s1 RA {ra}"
WORKER_ID = "w-privacidade-redacao"
OUTSIDE_EMAIL = "fora.da.lista@turma.example.org"
DEFAULT_HOURS = 168
CONFIRMATION_VALIDITY = timedelta(minutes=10)
PROCESS_TIMEOUT = 15.0  # inclui a subida do worker (esquema, posse, perfil)
FROM_HEADER = f"Canal de Questões <{SENDER}>"
SUBJECT_TEMPLATE = "Seu link para o bot do oferecimento {course} ({term})"
BODY_TEMPLATE = (
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da disciplina chegam "
    "por um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O bot "
    "vai mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
    "e-mail institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la."
)


def _fill(template: str, values: dict[str, str]) -> str:
    """Troca `{nome}` pelos `values` numa passada só, como o `compose`."""
    return re.sub(r"\{([a-z]+)\}", lambda match: values.get(match.group(1), match.group(0)), template)


def _identity_forms() -> list[tuple[str, str]]:
    """(rótulo, alvo normalizado) da identidade de `PERSON`, no molde dos alvos da API base (nome, cada palavra
    de 5 letras ou mais, RA, e-mail e parte local), comparados sem acento nem caixa (`fold`)."""
    forms = [("nome", fold(PERSON["name"]))]
    forms += [(f"palavra:{word}", word) for word in fold(PERSON["name"]).split(" ") if len(word) >= 5]
    forms += [("ra", PERSON["ra"]), ("email", fold(PERSON["email"])),
              ("email-local", fold(PERSON["email"].split("@")[0]))]
    return forms


def _identity_hits(text: str) -> list[str]:
    """Os rótulos das formas da identidade de `PERSON` achadas em `text` (lista vazia se nenhuma)."""
    folded = fold(text)
    return sorted(label for label, target in _identity_forms() if target in folded)


def _wait_until(predicate: Callable[[], Any], what: str, timeout: float = PROCESS_TIMEOUT) -> Any:
    """Espera real por uma thread do worker, com prazo (o resultado vem do worker, não do relógio)."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"prazo de {timeout} s esgotado: {what}"
        time.sleep(0.05)


def _update_row(sql: psycopg.Connection, bot: int, update_id: int) -> tuple | None:
    return sql.execute(
        "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (bot, update_id)).fetchone()


def _validity(moment: datetime) -> str:
    """`AAAA-MM-DD HH:MM <abreviação> (<fuso>)`, no fuso do oferecimento."""
    local = moment.astimezone(ZoneInfo(ZONE))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname()} ({ZONE})"


def test_course_and_term_are_redacted_in_the_email_and_in_the_new_bot_texts(
        client: Any, clock: Any, sql: psycopg.Connection, bot_api: Any, bot_worker_thread: Any, settings: Any,
        smtp: Any, mail_channel: Any) -> None:
    """Controle positivo da redação (a redação da disciplina e do período tem
    controle positivo num teste de privacidade à parte); o assunto e o corpo do
    e-mail levam a disciplina e o período redigidos, e nem o nome nem o RA da pessoa; os textos
    novos do bot com a disciplina e o período redigidos; a redação dos oferecimentos na gravação e na
    saída do oferecimento.

    Uma pessoa é cadastrada e o oferecimento é criado com o nome e o e-mail dela na disciplina e o RA
    no período (controle positivo: o detector acha as três formas nos textos de entrada). O oferecimento
    sai com a marca `[pessoa <apelido>]` no lugar de cada uma; o e-mail do convite de um aluno neutro
    (assunto e corpo, exatos) e os 4 textos novos do bot, recebidos pelo aluno na ordem do fluxo (termo,
    recusa, termo de novo, boas-vindas e a resposta do `/convite`), têm a marca na disciplina e no
    período e nenhuma forma da identidade; nenhum parâmetro enviado ao Telegram e nenhuma parte do
    e-mail capturado (cabeçalhos, texto e bruto) tem a identidade."""
    started = clock.now()
    course_raw = _fill(COURSE_TEMPLATE, PERSON)
    term_raw = _fill(TERM_TEMPLATE, PERSON)
    # Controle positivo do detector: a entrada tem a identidade inteira (nome, palavras, e-mail e RA).
    assert _identity_hits(course_raw) == sorted(label for label, _ in _identity_forms() if label != "ra")
    assert _identity_hits(term_raw) == ["ra"]

    person = register_person(client, ADMIN_TOKEN, **PERSON)
    mark = marker(person["alias"])
    course, term = f"Oficina de {mark} ({mark})", f"2031s1 RA {mark}"
    assert _identity_hits(course + term) == [] and mark in course and mark in term

    # O oferecimento, com a disciplina e o período redigidos na saída.
    offering = create_offering(client, ADMIN_TOKEN, code=OFFERING_CODE, course=course_raw, term=term_raw,
                               timezone=ZONE)
    offering_id = offering["offering_id"]
    assert offering == {
        "offering_id": offering_id, "code": OFFERING_CODE, "course": course, "term": term, "timezone": ZONE,
        "response_window_minutes": 1440, "state": "draft", "created_at": iso(started), "activated_at": None,
        "closed_at": None, "archived_at": None, "bot": None, "free_messages": False, "contact_email_set": False,
        # Expurgo: o `Offering` traz `purged_at`, nulo sem expurgo.
        "purged_at": None}
    ref = {"offering_id": offering_id, "code": OFFERING_CODE}
    bot = bot_api.add_bot()
    username = bot_api.state(bot).username
    assert isinstance(username, str)
    register_bot(client, ADMIN_TOKEN, OFFERING_CODE, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, OFFERING_CODE)
    worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID),
                               channel=ConsoleChannel(io.StringIO()), handlers={}, verification=mail_channel)
    _wait_until(lambda: sql.execute("SELECT profile_state FROM bots WHERE telegram_bot_id = %s",
                                    (bot,)).fetchone() == ("configured",), "perfil do bot não configurado")
    bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=PROCESS_TIMEOUT)

    # A lista (um aluno neutro), o preparo e a confirmação: um e-mail de convite.
    assert import_students(client, ADMIN_TOKEN, OFFERING_CODE, [STUDENT]) == {
        "offering": ref, "received": 1, "people_created": 1, "people_reused": 0, "enrollments_created": 1,
        "already_enrolled": 0}
    prepared = prepare_invites(client, ADMIN_TOKEN, OFFERING_CODE)
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", prepared["confirmation_code"]), prepared
    assert prepared == {"offering": ref, "eligible": 1, "valid_hours": DEFAULT_HOURS,
                        "confirmation_code": prepared["confirmation_code"],
                        "expires_at": iso(started + CONFIRMATION_VALIDITY)}
    assert send_invites(client, ADMIN_TOKEN, OFFERING_CODE, prepared["confirmation_code"]) == {
        "offering": ref, "queued": 1, "valid_hours": DEFAULT_HOURS}
    (message,) = smtp.wait_for(lambda: smtp.messages(), timeout=PROCESS_TIMEOUT)
    (link,) = smtp.invite_links()
    assert (link.bot_username, link.rcpt_to, link.acknowledged) == (username, (STUDENT["email"],), True)
    assert link.url == f"https://t.me/{username}?start={link.code}"

    # O e-mail: assunto e corpo exatos, com a marca na disciplina e no período e sem a identidade.
    subject = _fill(SUBJECT_TEMPLATE, {"course": course, "term": term})
    body = _fill(BODY_TEMPLATE, {"course": course, "term": term, "link": link.url,
                                 "validity": _validity(started + timedelta(hours=DEFAULT_HOURS))})
    assert (message.mail_from, message.rcpt_to) == (SENDER, (STUDENT["email"],))
    assert (message.header("From"), message.header("To"), message.header("Subject")) == (
        FROM_HEADER, STUDENT["email"], subject)
    assert message.text == body + "\n"
    for part in (subject, message.text):
        assert part.count(mark) == 3, "a marca da pessoa na disciplina (2 vezes) e no período (1 vez)"
    assert [(name, hits) for name, value in message.headers if (hits := _identity_hits(value))] == []
    assert _identity_hits(message.text) == [] and _identity_hits(message.raw.decode("latin-1")) == []

    # Os 4 textos novos do bot, pela recepção real.
    chat, other = make_user(701), make_user(702)

    def processed(update_id: int | None, outcome: str) -> None:
        assert update_id is not None, "a Bot API falsa descartou o update"
        _wait_until(lambda: _update_row(sql, bot, update_id) == ("processed", outcome),
                    f"update não processado como {outcome}")

    def received(user: Any, count: int) -> None:
        bot_api.wait_for(lambda: len(bot_api.sent_messages(bot, chat_id=user.user_id)) >= count,
                         timeout=PROCESS_TIMEOUT)

    processed(bot_api.push_start(bot, chat, link.code), "student_terms_presented")
    received(chat, 1)
    processed(bot_api.push_text(bot, chat, "/recuso", bot_command=True), "student_terms_refused")
    received(chat, 2)
    processed(bot_api.push_start(bot, chat, link.code), "student_terms_presented")
    received(chat, 3)
    processed(bot_api.push_text(bot, chat, "/aceito", bot_command=True), "student_joined")
    received(chat, 4)
    processed(bot_api.push_text(bot, other, f"/convite {OUTSIDE_EMAIL}", bot_command=True), "invite_requested")
    received(other, 1)
    assert bot_worker_thread.stop_one(worker) < 10, "run_worker não parou em 10 s"
    assert worker.error is None and worker.returned

    values = {"course": course, "term": term, "version": str(TERMS_VERSION)}
    terms, refused, welcome, requested = (_fill(t, values) for t in (TERMS, TERMS_REFUSED, WELCOME_STUDENT,
                                                                       INVITE_REQUESTED))
    assert [(m.chat_id, m.text) for m in bot_api.sent_messages(bot)] == [
        (chat.user_id, terms), (chat.user_id, refused), (chat.user_id, terms), (chat.user_id, welcome),
        (other.user_id, requested)]
    for text in (terms, refused, welcome, requested):
        assert text.count(mark) == 3, f"a marca da pessoa no texto novo: {text[:80]!r}"
        assert course in text and term in text
        assert _identity_hits(text) == []
    # Nenhum parâmetro enviado ao Telegram tem a identidade (descrição, comandos e mensagens).
    assert [(c.method, c.seq, hits) for c in bot_api.calls(bot_id=bot)
            if (hits := _identity_hits(json.dumps(c.params, ensure_ascii=False)))] == []
    # O aluno não é a pessoa da disciplina (controle: a marca vem da pessoa cadastrada no teste).
    assert _identity_hits(repr(STUDENT)) == [] and _identity_hits(repr(PERSON)) != []
