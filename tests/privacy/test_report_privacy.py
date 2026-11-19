"""Privacidade do relatório, dos avisos, dos resumos e da prévia.


Um roteiro pelo console com os alunos canário do disparo e uma **questão canário**, e as saídas dos relatórios varridas, cada
redação com **controle positivo** (a identidade está na fonte e some da saída, com a marca de redação no lugar):

1. a identidade de `DISPATCH_B` (nome, RA e e-mail) respondida em **texto** e em **número** por `DISPATCH_A` e
   por um aluno neutro está em `submissions.raw_input` (controle positivo) e some do JSON, do CSV, das versões
   congeladas, do resumo de fechamento, do resumo reenviado, do resumo diário e da saída do CLI (`reports
   show`, `--json`, `--csv`, `--version`, `reports versions`, `reports resend`, `offerings digest show`/`set`),
   com `[pessoa <apelido de B>]` no lugar; os itens redigidos da distribuição conferidos por valor;
2. a **identidade cadastrada depois do congelamento** (um nome que dois alunos escreveram, importado como aluno
   depois da versão 1) fica literal no `content` gravado da versão 1 (controle positivo) e sai redigida na
   leitura da versão 1, no cálculo no momento e na versão 2 (gravada já redigida);
3. o título e as alternativas da questão canário (a identidade de `DISPATCH_B`) saem redigidos no relatório
   (título e texto das alternativas), no cabeçalho da prévia, nos avisos e nos resumos, e **literais** nas
   mensagens da questão da prévia e em `dispatch_preview_contents` (a exceção declarada do conteúdo da questão na prévia, com controle
   positivo e o conjunto exato dos alvos achados); o aviso do motor com a identidade sai no cabeçalho só pelo
   tipo, com o texto fixo;
4. nenhum pseudônimo como dado estruturado (nenhum `pseudo_id`, apelido fora da marca, id de entrega ou de
   submissão) nas saídas dos relatórios; nenhum nome de campo de gabarito no relatório nem item marcado como correto,
   e a sentinela do gabarito em nenhuma saída dos relatórios (controle positivo: ela está em
   `submissions.correct_answers`);
5. `team_notices.data` e os eventos novos (`report.frozen`, `notice.recorded`, `offering.digest_changed`)
   sem texto livre: só números, booleanos, nulos, uuids, horários, datas e identificadores de enumeração;
6. `find_secrets` (o token inicial, os segredos registrados e o token do bot) sem segredo nas saídas dos relatórios,
   nos eventos e nos logs.

Arnês: o oferecimento e o bot pela API de oferecimentos e bots (a `bot.sync_profile` processada antes do worker de console); os
quatro alunos (`DISPATCH_A`, `DISPATCH_B` e dois neutros) importados pela API do vínculo do aluno (a identidade da redação) e
vinculados por SQL (matrícula `active` com o termo aceito e o endereço `active`, como o
`test_dispatch_privacy.py`); o worker dos relatórios pela fixture `report_worker(console=True)`; a equipe (dois membros)
pela equipe simulada, com o `/start` pela `ConsoleInbox`. A questão canário é a referência `canary` do curso de
teste programada com três partes: escolha única com a identidade nas alternativas `(a)` nome, `(b)` "RA" + RA
e `(c)` e-mail, e `(d)` neutra (o gabarito); texto; número. O aviso `console_output` do preset canário fica,
com a identidade.

Respostas (todas no prazo): A e o neutro N1 marcam `(a)`, escrevem `IDENTITY` e informam o RA de B; o neutro
N2 marca `(b)`, escreve `LATER_NAME` e informa "6,5"; B marca `(d)`, escreve `LATER_NAME` e informa "6.5".
Assim, cada valor e texto aparece duas vezes (o mínimo de 2 do `TopList`), e todos saem como itens:
- texto: `IDENTITY` redigida (2, 2) e `LATER_NAME` (2, 2; redigido só depois do cadastro);
- número: o RA de B redigido (2, 2) e "6.5" (2, 2); razões "1.02e+09" (6648920317 / 6,5 = 1 022 910 818,
  com 3 algarismos significativos) (2, 2) e "1" (2, 2);
- escolhas: `(a)` redigida (2, 2), `(b)` "RA " + marca (1, 1), `(d)` (1, 1).
A ordem dos itens não é conferida aqui (é do `test_report_compute.py`): os itens são comparados ordenados.

Leituras [I] deste arquivo: a varredura do banco olha só as tabelas novas dos relatórios e as linhas da fila
com `notice_id` (as tabelas das fases anteriores são do canário e das varreduras do disparo, com as exceções delas); a
exceção da prévia vale para as colunas `ir` e `rendered` de `dispatch_preview_contents` (elas guardam
a IR da amostra e a renderização, sem redação), e o `content` da versão 1 pode guardar o nome cadastrado depois
(item 2), que não é alvo canário; o apelido conta como "fora da marca" quando aparece isolado (sem dígito
hexadecimal colado) depois de tiradas todas as marcas `[pessoa <apelido>]`.

No esqueleto, a montagem e a validação (das fases anteriores) passam, e a função
falha na primeira consulta às mensagens à equipe (tabela `team_notices` ausente: a migração `0005` ainda não
existe); depois, pelas rotas, pelos comandos e pelos avisos dos relatórios ainda não produzidos.
"""

from __future__ import annotations

import csv
import dataclasses
import html
import io
import json
import re
import time
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

from cq_server import noticetexts
from cq_server.adapters.console import ConsoleInbox
from cq_server.bots import profile_task_handler
from cq_server.tasks import TaskQueue
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    all_events,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    get_report,
    get_report_csv,
    import_students,
    list_report_versions,
    marker,
    prepare_dispatch,
    register_bot,
    update_offering_digest,
)
from tests.helpers.canary import DISPATCH_A, DISPATCH_B, DISPATCH_PREFIXES, find_canary, find_secrets
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleLine, ConsoleStudent, read_lines
from tests.helpers.db import scan_all_tables
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    FakeOption,
    number_field,
    preset,
    single_choice_field,
    text_field,
)
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import all_secrets
from tests.helpers.team import ACTIVE, Team, link_team, team_messages

# --- constantes (entradas do teste, não resultados) --------------------------------------------------

COURSE = "Algoritmos e Programação"
TERM = "2031s1"
ZONE = "America/Sao_Paulo"
CODE = "privacidade-relatorio-dados"
WORKER_ID = "worker-privacidade-dados"
PREP_WORKER_ID = "preparacao-privacidade-dados"
PROFILE = "bot.sync_profile"
REPO = "https://repos.cq.test/disciplina/questoes-privacidade-dados.git"
COMMIT = "c4e2" * 10
QID = "canary"
ORIGIN = "integration"
WINDOW = timedelta(minutes=30)
WAIT = 30.0

# A identidade de `DISPATCH_B` no texto (resposta, título, enunciado, alternativas); a forma redigida troca nome,
# RA e e-mail cada um pela marca (as três ocorrências não se encostam).
IDENTITY = f"{DISPATCH_B.name} (RA {DISPATCH_B.ra}, {DISPATCH_B.email})"
# O nome escrito por dois alunos e cadastrado só depois do congelamento (neutro: fora dos alvos canário).
LATER_NAME = "Teodolinda Posterior Araçá"
LATER_ROW = {"name": LATER_NAME, "ra": "5990000001", "email": "teodolinda.posterior@turma.example.org"}
NEUTRAL = ({"name": "Pessoa Privacidade Seis 01", "ra": "5990000101", "email": "privacidade.seis01@turma.example.org"},
           {"name": "Pessoa Privacidade Seis 02", "ra": "5990000102", "email": "privacidade.seis02@turma.example.org"})
CHAT_BASE = 5_990_000_000
TEXT_LABEL = "Nome:"
NUMBER_LABEL = "Matrícula:"
RATIO_OF_RA = "1.02e+09"  # 6648920317 / 6,5 = 1 022 910 818 → 3 algarismos significativos

QUESTION = preset(
    "canary", identity=IDENTITY, title=f"Questão de {IDENTITY}",
    statement=f"Responda, {html.escape(IDENTITY, quote=False)}: escolha, escreva e informe.",
    fields=(single_choice_field("mc", correct="d", options=(
        FakeOption("a", html.escape(DISPATCH_B.name, quote=False)), FakeOption("b", f"RA {DISPATCH_B.ra}"),
        FakeOption("c", html.escape(DISPATCH_B.email, quote=False)), FakeOption("d", "Nenhuma das anteriores"))),
            text_field("txt", label=TEXT_LABEL), number_field("num", label=NUMBER_LABEL, suffix=None)),
    explanation=None)

REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
# nenhum campo do relatório com estes nomes; nenhum item da distribuição com `correct`.
ANSWER_KEY_NAMES = frozenset({"correct_answer", "answer_key", "is_correct", "correct_value", "explanation"})
# Os textos sem texto livre (item 5): uuid, horário (com `Z` ou deslocamento), data, HH:MM ou enumeração.
_SAFE_STRING = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))?"
    r"|\d{2}:\d{2}"
    r"|[a-z][a-z0-9_]*")
# As tabelas novas dos relatórios e as colunas da exceção da prévia.
REPORT_TABLES = frozenset({"dispatch_reports", "team_notices", "team_notice_floors", "dispatch_previews",
                       "dispatch_preview_contents", "offering_digests", "invite_batches"})
_UUID_OR_HASH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|sha256:[0-9a-f]{64}")
PREVIEW_EXCEPTION = frozenset({("dispatch_preview_contents", "ir"), ("dispatch_preview_contents", "rendered")})


# --- apoio -------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _Pupil:
    key: str
    row: dict
    chat: int
    pseudo: str = ""
    alias: str = ""
    person_row: int = 0
    console: ConsoleStudent | None = None

    @property
    def io(self) -> ConsoleStudent:
        assert self.console is not None
        return self.console


@dataclasses.dataclass
class _W:
    client: Any
    sql: psycopg.Connection
    clock: Any
    stream: Any
    inbox: ConsoleInbox
    offering: dict
    bot_row: int
    pupils: dict[str, _Pupil]
    team: Team | None = None
    dispatch_id: str = ""
    exchanges: list[tuple[str, str, int, str]] = dataclasses.field(default_factory=list)

    @property
    def mark(self) -> str:
        return marker(self.pupils["B"].alias)

    @property
    def redacted(self) -> str:
        """`IDENTITY` redigida."""
        return f"{self.mark} (RA {self.mark}, {self.mark})"


def _all(sql: psycopg.Connection, query: str, params: tuple = ()) -> list[dict]:
    with sql.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


def _wait(predicate: Callable[[], Any], what: str, timeout: float = WAIT) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {timeout} s: {what}")
        time.sleep(0.02)


def _busy(w: _W) -> list[str]:
    now = w.clock.now()
    busy = [f"tarefa {t}" for (t,) in w.sql.execute(
        "SELECT task_type FROM tasks WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"mensagem {m}" for (m,) in w.sql.execute(
        "SELECT id FROM outbox_messages WHERE state = 'leased' OR (state = 'pending' AND available_at <= %s)",
        (now,)).fetchall()]
    busy += [f"update {u}" for (u,) in w.sql.execute(
        "SELECT id FROM telegram_updates WHERE state = 'pending'").fetchall()]
    return busy


def _settle(w: _W) -> None:
    for _ in range(10):
        _wait(lambda: not _busy(w), "o sistema parar")
        time.sleep(1.2)
        if not _busy(w):
            return
    raise AssertionError(f"o sistema não parou: {_busy(w)}")


def _hits(*texts: str) -> list[str]:
    """Os alvos canário do disparo achados nos textos (o detector independente da redação)."""
    return sorted({hit for text in texts for hit in find_canary(text, prefixes=DISPATCH_PREFIXES)})


def _record(client: Any, sink: list[tuple[str, str, int, str]]) -> None:
    """Grava (método, caminho, status, cabeçalhos + corpo) de toda resposta que passa pelo cliente HTTP do teste
    (API e CLI em processo)."""

    def hook(response: Any) -> None:
        response.read()
        request = response.request
        headers = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items())
        sink.append((request.method, request.url.path, response.status_code, f"{headers}\n{response.text}"))

    hooks = client.event_hooks
    client.event_hooks = {"request": list(hooks["request"]), "response": [*hooks["response"], hook]}


def _process_profile_tasks(db_url: str, clock: Any, settings: Any, bots: Any) -> None:
    queue = TaskQueue(db_url, clock=clock, settings=settings)
    handler = profile_task_handler(db_url, clock=clock, runtime=bots, settings=settings)
    try:
        while queue.work_once(PREP_WORKER_ID, {PROFILE: handler}) is not None:
            pass
    finally:
        close = getattr(handler, "close", None)
        if close is not None:
            close()
        queue.close()


def _link_student(w: _W, pupil: _Pupil, offering_row: int) -> None:
    """O vínculo do aluno por SQL: matrícula `active` com o termo aceito e o endereço `active` no bot."""
    now = w.clock.now()
    updated = w.sql.execute(
        "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, terms_accepted_at = %s "
        "WHERE person_id = %s AND offering_id = %s AND role = 'student'",
        (now, now, pupil.person_row, offering_row)).rowcount
    assert updated == 1
    w.sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
                  "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                  (pupil.person_row, w.bot_row, pupil.chat, now, now))


def _say(w: _W, pupil: _Pupil, act: Callable[[], None], *, outcome: str) -> None:
    mark = w.sql.execute("SELECT coalesce(max(id), 0) FROM telegram_updates").fetchone()[0]
    act()
    rows = _wait(lambda: (found := _all(w.sql, "SELECT state, outcome FROM telegram_updates WHERE id > %s AND "
                                        "chat_id = %s ORDER BY id", (mark, pupil.chat)))
                 and all(r["state"] == "processed" for r in found) and found, f"a entrada de {pupil.key}")
    assert [r["outcome"] for r in rows] == [outcome], rows


def _delivery_row(w: _W, pupil: _Pupil) -> dict:
    rows = _all(w.sql, "SELECT d.id, d.state, d.principal_message_id, d.delivery_uuid::text AS delivery_uuid "
                "FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id "
                "JOIN enrollments e ON e.id = d.enrollment_id "
                "WHERE x.dispatch_uuid = %s::uuid AND e.person_id = %s", (w.dispatch_id, pupil.person_row))
    assert len(rows) == 1
    return rows[0]


def _line_of(w: _W, message_id: int) -> ConsoleLine:
    found = [line for line in read_lines(w.stream.getvalue()) if line.message_id == message_id]
    assert len(found) == 1, f"linhas da mensagem {message_id}: {len(found)}"
    return found[0]


def _prompt(w: _W, pupil: _Pupil, index: int) -> ConsoleLine:
    delivery = _delivery_row(w, pupil)
    rows = _wait(lambda: w.sql.execute(
        "SELECT id FROM outbox_messages WHERE delivery_id = %s AND delivery_role = 'field_prompt' AND field_index = %s "
        "AND state = 'sent' ORDER BY id", (delivery["id"], index)).fetchall(), f"o pedido {index} de {pupil.key}")
    return _line_of(w, int(rows[-1][0]))


def _answer(w: _W, pupil: _Pupil, choice: str, text: str, number: str) -> None:
    """As três partes, no prazo: a escolha pela chave em resposta à questão, o texto e o número em resposta ao
    pedido de cada campo; espera a correção."""
    question = _line_of(w, int(_delivery_row(w, pupil)["principal_message_id"]))
    _say(w, pupil, lambda: pupil.io.choose(question, choice), outcome="answer_part_recorded")
    _say(w, pupil, lambda: pupil.io.reply(_prompt(w, pupil, 1), text), outcome="answer_part_recorded")
    _say(w, pupil, lambda: pupil.io.reply(_prompt(w, pupil, 2), number), outcome="answer_recorded")
    _wait(lambda: w.sql.execute("SELECT count(*) FROM submissions WHERE delivery_id = %s AND outcome = 'graded'",
                                (_delivery_row(w, pupil)["id"],)).fetchone()[0] == 1, f"a correção de {pupil.key}")


def _team_texts(w: _W, kind: str) -> list[str]:
    """Os textos do aviso de texto `kind` enviados aos dois membros (um por membro)."""
    assert w.team is not None
    found = _wait(lambda: (x := team_messages(w.sql, kind)) and len(x) == len(w.team.active)
                  and all(m.state == "sent" for m in x) and x, f"o aviso {kind} enviado")
    assert {m.recipient for m in found} == set(w.team.recipients)
    texts = []
    for message in found:
        assert message.text is not None
        assert _line_of(w, message.message_id).text == message.text
        texts.append(message.text)
    return texts


def _strings(value: Any) -> Iterable[str]:
    """As chaves e os textos de um JSON, recursivamente."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _assert_no_free_text(value: Any, what: str) -> None:
    """Item 5: só números, booleanos, nulos e textos em `_SAFE_STRING` (chaves inclusive)."""
    bad = [text for text in _strings(value) if _SAFE_STRING.fullmatch(text) is None]
    assert bad == [], f"{what}: texto livre {bad}"


def _sorted_items(top: dict) -> dict:
    return {"items": sorted(top["items"], key=lambda item: item["value"]), "others": top["others"]}


def _pair(on_time: int, with_late: int) -> dict[str, int]:
    return {"on_time": on_time, "with_late": with_late}


def _csv_rows(text: str) -> list[list[str]]:
    assert text.startswith("﻿")
    return list(csv.reader(io.StringIO(text[1:], newline="")))


# --- o roteiro -----------------------------------------------------------------------------------------


def test_report_notices_summaries_preview_and_cli_redact_identities_with_positive_controls(
        client: Any, sql: psycopg.Connection, clock: Any, settings: Any, db_url: str, bot_api: Any, bots: Any,
        engine_fake: Any, repo_fetcher: Any, report_worker: Any, cli_invoke: Any) -> None:
    """os itens 1 a 6 da docstring do módulo, cada
    redação com controle positivo, a exceção declarada da prévia com o conjunto exato dos alvos e as varreduras
    das saídas dos relatórios (API em JSON e CSV, versões congeladas, resumos, avisos, prévia, CLI, eventos novos,
    `team_notices.data`, tabelas novas, logs) sem alvo canário, sem pseudônimo estruturado e sem segredo."""
    offering = create_offering(client, ADMIN_TOKEN, code=CODE, course=COURSE, term=TERM, timezone=ZONE)
    tg_bot = bot_api.add_bot()
    bot_token = bot_api.token_of(tg_bot)
    register_bot(client, ADMIN_TOKEN, CODE, bot_token)
    activate_bot(client, ADMIN_TOKEN, CODE)
    offering_row, bot_row = sql.execute(
        "SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id WHERE o.offering_id = %s::uuid",
        (offering["offering_id"],)).fetchone()
    _process_profile_tasks(db_url, clock, settings, bots)
    pupils = {"A": _Pupil("A", DISPATCH_A.row(), CHAT_BASE + 1), "B": _Pupil("B", DISPATCH_B.row(), CHAT_BASE + 2),
              "N1": _Pupil("N1", NEUTRAL[0], CHAT_BASE + 3), "N2": _Pupil("N2", NEUTRAL[1], CHAT_BASE + 4)}
    imported = import_students(client, ADMIN_TOKEN, CODE, [p.row for p in pupils.values()])
    assert imported["enrollments_created"] == 4, imported
    inbox = ConsoleInbox(db_url, clock=clock)
    try:
        worker = report_worker(console=True, settings=dataclasses.replace(settings, worker_id=WORKER_ID))
        w = _W(client=client, sql=sql, clock=clock, stream=worker.stream, inbox=inbox, offering=offering,
               bot_row=bot_row, pupils=pupils)
        for pupil in pupils.values():
            pupil.person_row, pupil.pseudo, pupil.alias = sql.execute(
                "SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                "WHERE i.ra = %s", (pupil.row["ra"],)).fetchone()
            _link_student(w, pupil, offering_row)
            pupil.console = ConsoleStudent(inbox, bot_id=bot_row, chat_id=pupil.chat, recipient=pupil.pseudo,
                                           source=worker.stream)
        w.team = link_team(client, sql, offering, tg_bot, (ACTIVE, ACTIVE), inbox=inbox)
        _record(client, w.exchanges)
        with capture_logs() as logs:
            outputs = _run(w, engine_fake, repo_fetcher, cli_invoke)
        _scan(w, outputs, logs.text, bot_token)
    finally:
        report_worker.stop()
        inbox.close()


def _run(w: _W, engine_fake: Any, repo_fetcher: Any, cli_invoke: Any) -> dict[str, Any]:
    """O roteiro; devolve as saídas dos relatórios para as varreduras."""
    a, b, n1, n2 = (w.pupils[k] for k in ("A", "B", "N1", "N2"))
    out: dict[str, Any] = {}

    # Preparo e validação da questão canário; a prévia à equipe.
    engine_fake.program((REPO, COMMIT, QID), QUESTION)
    repo_fetcher.add(REPO, COMMIT)
    t_prep = w.clock.advance(60)
    closes = t_prep + WINDOW
    w.dispatch_id = prepare_dispatch(w.client, ADMIN_TOKEN, CODE, repo=REPO, commit=COMMIT, qid=QID,
                                     closes_at=iso(closes), origin=ORIGIN)["dispatch_id"]
    _wait(lambda: get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)["state"] != "validating", "a validação")
    _settle(w)
    assert get_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)["state"] == "scheduled"
    out["preview"] = _check_preview(w)

    # Confirmação e abertura; o aviso "aberto" com o título redigido.
    w.clock.advance(60)
    confirm_dispatch(w.client, ADMIN_TOKEN, CODE, w.dispatch_id)
    _wait(lambda: w.sql.execute("SELECT count(*) FROM deliveries WHERE state = 'delivered'").fetchone()[0] == 4,
          "as quatro entregas")
    _settle(w)
    out["opened"] = _team_texts(w, "dispatch_opened")

    # As respostas com a identidade de B (texto e número) e o nome que será cadastrado depois.
    w.clock.advance(30)
    _answer(w, a, "a", IDENTITY, DISPATCH_B.ra)
    _answer(w, n1, "a", IDENTITY, DISPATCH_B.ra)
    _answer(w, n2, "b", LATER_NAME, "6,5")
    _answer(w, b, "d", LATER_NAME, "6.5")
    _settle(w)
    raw_inputs = json.dumps([r for (r,) in w.sql.execute("SELECT raw_input FROM submissions ORDER BY id").fetchall()],
                            ensure_ascii=False)
    identity_labels = set(_hits(IDENTITY))
    assert identity_labels and identity_labels <= set(_hits(raw_inputs)), "controle positivo: a identidade respondida"
    assert LATER_NAME in raw_inputs
    correct = json.dumps([r for (r,) in w.sql.execute("SELECT correct_answers FROM submissions").fetchall()])
    assert ANSWER_SENTINEL in correct, "controle positivo: a sentinela está nas submissões"

    # Fechamento: a versão 1 (com o nome ainda não cadastrado, literal) e o resumo de fechamento.
    w.clock.set(closes + timedelta(seconds=1))
    _close(w)
    _settle(w)
    out["closed"] = _team_texts(w, "dispatch_closed")
    v1_before = get_report(w.client, CODE, w.dispatch_id, version=1)
    _check_distribution(w, v1_before, later=LATER_NAME)
    stored_v1 = w.sql.execute("SELECT content::text FROM dispatch_reports WHERE version = 1").fetchone()[0]
    assert LATER_NAME in stored_v1, "controle positivo: o nome ainda não era identidade no congelamento"
    assert _hits(stored_v1) == [] and w.mark in stored_v1

    # O nome passa a ser identidade: a versão 1 sai redigida na leitura; o `content` gravado não muda. As
    # respostas da API daqui em diante não podem mais trazer o nome (item 2).
    out["exchanges_before_later"] = len(w.exchanges)
    imported = import_students(w.client, ADMIN_TOKEN, CODE, [LATER_ROW])
    assert imported["people_created"] == 1
    later_alias = w.sql.execute("SELECT p.alias FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
                                (LATER_ROW["ra"],)).fetchone()[0]
    later_mark = marker(later_alias)
    out["later"] = (LATER_NAME, later_mark, w.sql.execute(
        "SELECT p.pseudo_id::text FROM identities i JOIN people p ON p.id = i.person_id WHERE i.ra = %s",
        (LATER_ROW["ra"],)).fetchone()[0], later_alias)
    out["v1"] = get_report(w.client, CODE, w.dispatch_id, version=1)
    _check_distribution(w, out["v1"], later=later_mark)
    assert w.sql.execute("SELECT content::text FROM dispatch_reports WHERE version = 1").fetchone()[0] == stored_v1, (
        "a versão 1 é imutável: a redação nova é só na leitura")
    out["v1_csv"] = get_report_csv(w.client, CODE, w.dispatch_id, version=1)
    out["now"] = get_report(w.client, CODE, w.dispatch_id)
    _check_distribution(w, out["now"], later=later_mark)
    out["now_csv"] = get_report_csv(w.client, CODE, w.dispatch_id)
    for name in ("v1_csv", "now_csv"):
        keys = [row[2] for row in _csv_rows(out[name])[1:]]
        # A coluna `key` leva o valor redigido (o RA de B no número, a identidade no texto, o nome cadastrado
        # depois); a das escolhas é o `content_hash`.
        assert {w.mark, w.redacted, later_mark} <= set(keys), (name, "controle positivo do CSV")
        assert LATER_NAME not in out[name]

    # O reenvio pelo CLI (a versão 2, gravada já redigida) e o resumo reenviado.
    w.clock.advance(60)
    cli: dict[str, Any] = {"resend": cli_invoke(["reports", "resend", CODE, w.dispatch_id])}
    _settle(w)
    out["resent"] = _team_texts(w, "summary_resent")
    stored_v2 = w.sql.execute("SELECT content::text FROM dispatch_reports WHERE version = 2").fetchone()[0]
    assert later_mark in stored_v2 and LATER_NAME not in stored_v2 and _hits(stored_v2) == []
    out["v2"] = get_report(w.client, CODE, w.dispatch_id, version=2)
    _check_distribution(w, out["v2"], later=later_mark)
    out["versions"] = list_report_versions(w.client, CODE, w.dispatch_id)

    # O resumo diário (o título redigido na linha do disparo).
    t_set = w.clock.advance(60)
    zone = ZoneInfo(ZONE)
    due = (t_set + timedelta(minutes=2)).astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    local_time = due.strftime("%H:%M")
    out["digest_set"] = update_offering_digest(w.client, CODE, enabled=True, local_time=local_time)
    _settle(w)
    w.clock.set(due.astimezone(t_set.tzinfo) + timedelta(seconds=1))
    _settle(w)
    out["digest"] = _team_texts(w, "daily_digest")

    # O CLI (sucesso; o `digest set` igual ao vigente, sem mudança).
    cli.update({
        "show": cli_invoke(["reports", "show", CODE, w.dispatch_id]),
        "show_json": cli_invoke(["reports", "show", CODE, w.dispatch_id], json=True),
        "show_v1": cli_invoke(["reports", "show", CODE, w.dispatch_id, "--version", "1"]),
        "show_csv": cli_invoke(["reports", "show", CODE, w.dispatch_id, "--csv"]),
        "versions": cli_invoke(["reports", "versions", CODE, w.dispatch_id]),
        "digest_show": cli_invoke(["offerings", "digest", "show", CODE]),
        "digest_set": cli_invoke(["offerings", "digest", "set", CODE, "--enabled", "on", "--at", local_time]),
    })
    for name, result in cli.items():
        assert result.exit_code == 0, (name, result.output)
    assert w.mark in cli["show"].stdout, "controle positivo: o título redigido no `reports show`"
    assert json.loads(cli["show_json"].stdout)["dispatch"]["title"] == f"Questão de {w.redacted}"
    assert w.mark in cli["show_csv"].stdout and later_mark in cli["show_csv"].stdout
    out["cli"] = cli

    # Títulos redigidos nos avisos e nos resumos (controle positivo da marca).
    for name in ("opened", "closed", "resent", "digest"):
        for text in out[name]:
            assert f"Questão de {w.redacted}" in text, (name, text)
    return out


def _close(w: _W, timeout: float = 3 * WAIT) -> None:
    deadline = time.monotonic() + timeout

    def state() -> str:
        return w.sql.execute("SELECT state FROM dispatches WHERE dispatch_uuid = %s::uuid",
                             (w.dispatch_id,)).fetchone()[0]

    while True:
        window = time.monotonic() + 2.5
        while time.monotonic() < window:
            if state() == "closed":
                return
            time.sleep(0.05)
        assert time.monotonic() < deadline, f"o disparo não fechou: {state()}"
        w.clock.advance(6)


def _check_preview(w: _W) -> list[str]:
    """Item 3 na prévia: o cabeçalho com o título redigido e o aviso do motor só pelo tipo; a mensagem da questão
    com a identidade literal (a exceção do conteúdo da questão na prévia, controle positivo). Devolve os textos do cabeçalho."""
    assert w.team is not None
    headers = []
    for member in w.team.active:
        found = _wait(lambda m=member: (x := team_messages(w.sql, "dispatch_preview", m)) and len(x) >= 3
                      and all(y.state == "sent" for y in x) and x, f"a prévia de {member.label}")
        assert [m.role for m in found] == [None, "question", "field_prompt"], found
        header, question = (_line_of(w, m.message_id) for m in found[:2])
        assert found[2].field_index == 0, found[2]
        prompt_text = _line_of(w, found[2].message_id).text
        assert _hits(prompt_text) == [], _hits(prompt_text)
        assert f"Prévia do disparo: Questão de {w.redacted}" in header.text, header.text
        assert noticetexts.PREVIEW_ENGINE_WARNINGS["console_output"] in header.text
        assert _hits(header.text) == [], _hits(header.text)
        assert set(_hits(question.text)) >= {f"disparo-nome:{DISPATCH_B.name}", f"disparo-ra:{DISPATCH_B.ra}",
                                             f"disparo-email:{DISPATCH_B.email}"}, (
            "controle positivo: a questão da prévia é a do aluno, sem redação")
        assert w.mark not in question.text
        headers.append(header.text)
    return headers


def _check_distribution(w: _W, report: dict, *, later: str) -> None:
    """Item 1 e 3 no relatório: o título e os itens redigidos, conferidos por valor (ordenados)."""
    assert report["dispatch"]["title"] == f"Questão de {w.redacted}"
    choice, text, number = report["distribution"]["fields"]
    ir = w.sql.execute("SELECT c.ir FROM delivery_contents c JOIN deliveries d ON d.id = c.delivery_id "
                       "ORDER BY d.id LIMIT 1").fetchone()[0]
    hashes = {o["key"]: o["content_hash"] for o in ir["fields"][0]["options"]}
    assert sorted(choice["choices"], key=lambda item: item["content_hash"]) == sorted([
        {"content_hash": hashes["a"], "text": w.mark, "count": _pair(2, 2)},
        {"content_hash": hashes["b"], "text": f"RA {w.mark}", "count": _pair(1, 1)},
        {"content_hash": hashes["d"], "text": "Nenhuma das anteriores", "count": _pair(1, 1)},
    ], key=lambda item: item["content_hash"])
    assert (text["label"], number["label"]) == (TEXT_LABEL, NUMBER_LABEL)
    assert _sorted_items(text["texts"]) == _sorted_items(
        {"items": [{"value": w.redacted, "count": _pair(2, 2)}, {"value": later, "count": _pair(2, 2)}],
         "others": _pair(0, 0)})
    assert _sorted_items(number["values"]) == _sorted_items(
        {"items": [{"value": w.mark, "count": _pair(2, 2)}, {"value": "6.5", "count": _pair(2, 2)}],
         "others": _pair(0, 0)})
    assert _sorted_items(number["ratios"]) == _sorted_items(
        {"items": [{"value": RATIO_OF_RA, "count": _pair(2, 2)}, {"value": "1", "count": _pair(2, 2)}],
         "others": _pair(0, 0)})
    text_json = json.dumps(report, ensure_ascii=False)
    assert _hits(text_json) == [], _hits(text_json)
    assert ANSWER_SENTINEL not in text_json
    assert ANSWER_KEY_NAMES.isdisjoint(set(_keys(report))), "nenhum campo de gabarito"
    for field in report["distribution"]["fields"]:
        for item in field["choices"] or []:
            assert "correct" not in item, "nenhum item marcado como correto"


def _scan(w: _W, out: dict[str, Any], logs: str, bot_token: str) -> None:
    """Itens 3 a 6: as varreduras de todas as saídas dos relatórios."""
    assert w.team is not None
    later_name, later_mark, later_pseudo, later_alias = out["later"]
    texts: dict[str, str] = {}
    after_later: set[str] = set()
    for index, (method, path, status, body) in enumerate(list(w.exchanges)):
        if "/report" in path or "/digest" in path or path.startswith("/v1/events"):
            name = f"api {index} {method} {path} {status}"
            texts[name] = body
            if index >= out["exchanges_before_later"]:
                after_later.add(name)
    for name, result in out["cli"].items():
        texts[f"cli {name}"] = f"{result.stdout}\n{result.stderr}"
    for name in ("opened", "closed", "resent", "digest", "preview"):
        for index, text in enumerate(out[name]):
            texts[f"aviso {name} {index}"] = text
    question_ids = {m.message_id for m in team_messages(w.sql, "dispatch_preview") if m.role == "question"}
    assert len(question_ids) == len(w.team.active)
    for member in w.team.active:
        for line in read_lines(w.stream.getvalue()):
            if line.recipient == member.recipient and line.message_id not in question_ids:
                texts[f"console {member.label} {line.message_id}"] = line.text
    events = all_events(w.client, ADMIN_TOKEN, *REPORT_EVENT_TYPES)
    assert {e["type"] for e in events} == set(REPORT_EVENT_TYPES), "controle contra o vazio: os 3 eventos novos"
    texts["eventos"] = json.dumps(events, ensure_ascii=False)
    notice_data = [r for (r,) in w.sql.execute("SELECT data FROM team_notices ORDER BY id").fetchall()]
    assert len(notice_data) == 5, "controle contra o vazio: prévia, aberto, fechado, reenviado e resumo diário"
    texts["team_notices.data"] = json.dumps(notice_data, ensure_ascii=False)
    texts["dispatch_reports.content"] = json.dumps(
        [r for (r,) in w.sql.execute("SELECT content FROM dispatch_reports WHERE version > 1").fetchall()],
        ensure_ascii=False)

    # Item 3 (fora da exceção) e 1: nenhuma saída dos relatórios com alvo canário; o nome cadastrado depois não sai.
    leaks = {name: hits for name, text in texts.items() if (hits := _hits(text))}
    assert leaks == {}, f"alvo canário em saída dos relatórios: {leaks}"
    # O nome cadastrado depois do congelamento só pode estar nas respostas anteriores ao cadastro (controle
    # positivo: a versão 1 lida antes dele o trazia).
    later_seen = {name for name, text in texts.items() if later_name in text}
    assert later_seen and later_seen.isdisjoint(after_later), later_seen & after_later
    assert {n for n in later_seen if not n.startswith("api ")} == set(), later_seen
    assert [name for name, text in texts.items() if ANSWER_SENTINEL in text] == []

    # Item 3, a exceção declarada: nas tabelas novas, os alvos só em `dispatch_preview_contents`
    # (`ir` e `rendered`), com o conjunto exato dos alvos da identidade da questão canário.
    expected = set(_hits(IDENTITY))
    found: dict[tuple[str, str], set[str]] = {}
    for table, column, text in scan_all_tables(w.sql):
        if table in REPORT_TABLES and (hits := _hits(text)):
            found.setdefault((table, column), set()).update(hits)
    notice_rows = [t for (t,) in w.sql.execute("SELECT text FROM outbox_messages WHERE notice_id IS NOT NULL "
                                               "AND text IS NOT NULL").fetchall()]
    assert notice_rows and _hits(*notice_rows) == [], "as mensagens de texto à equipe sem alvo canário"
    assert set(found) == PREVIEW_EXCEPTION, f"alvo canário fora da exceção da prévia: {found}"
    assert all(hits == expected for hits in found.values()), (found, expected)

    # Item 4: nenhum pseudônimo como dado estruturado.
    pseudos = [p.pseudo for p in w.pupils.values()] + [m.pseudo_id for m in w.team.members] + [later_pseudo]
    aliases = [p.alias for p in w.pupils.values()] + [m.alias for m in w.team.members] + [later_alias]
    ids = [r for (r,) in w.sql.execute("SELECT delivery_uuid::text FROM deliveries").fetchall()]
    ids += [r for (r,) in w.sql.execute("SELECT submission_uuid::text FROM submissions").fetchall()]
    assert len(ids) == 8, "controle contra o vazio: 4 entregas e 4 submissões"
    for name, text in texts.items():
        for value in pseudos + ids:
            assert value not in text, f"{name}: pseudônimo ou id interno {value}"
        # As marcas, os uuids e os hashes de conteúdo saem antes (um apelido curto pode coincidir com um trecho
        # hexadecimal deles; os `pseudo_id` já foram conferidos inteiros acima).
        stripped = _UUID_OR_HASH.sub("", re.sub(r"\[pessoa [0-9a-f]{4,32}\]", "", text))
        for alias in aliases:
            assert re.search(rf"(?<![0-9a-f]){alias}(?![0-9a-f])", stripped) is None, f"{name}: apelido fora da marca"
    assert w.mark in texts["dispatch_reports.content"] and later_mark in texts["dispatch_reports.content"]

    # Item 5: `team_notices.data` e os eventos novos sem texto livre.
    _assert_no_free_text(notice_data, "team_notices.data")
    _assert_no_free_text([e["data"] for e in events], "eventos novos")

    # Item 6: nenhum segredo, nem nos logs; os logs sem alvo canário.
    secrets = all_secrets(bot_token)
    found_secrets = {name: hits for name, text in {**texts, "logs": logs}.items()
                     if (hits := find_secrets(text, secrets))}
    assert found_secrets == {}, found_secrets
    assert _hits(logs) == [], _hits(logs)
