"""CLI `cq reports show/versions/resend` e `cq offerings digest show/set` (os relatórios
e o resumo diário do acompanhamento, com o texto
para pessoas e os erros do CLI).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em `obj["http"]`),
e os erros seguem a forma comum do CLI (`Erro: <message>`, `Estado atual: <state>` no `invalid_state`,
`Permissão necessária: …` no 403, uma linha `  - <campo>: <motivo>` por campo no 400; com `--json`, o corpo de
erro da API no stderr; uso incorreto com código 2, `Uso incorreto: …` e nenhuma chamada à API).

**Saídas fixadas aqui** [I] (texto para pessoas, com as convenções dos demais comandos; a ordem
e o rótulo de cada linha são escolha deste teste):
- `reports show` (sem `--json`), uma linha por item, nesta ordem:
  `Relatório do disparo <dispatch_id> em <code>`; `Questão: <qid> @ <commit_short>`; `Título: <título>` (pelo
  `_terminal_safe`; `—` sem título); `Abertura: <opens_at> (<AAAA-MM-DD HH:MM> <fuso>)` e
  `Fechamento: <closes_at> (<AAAA-MM-DD HH:MM> <fuso>)` (UTC da API e hora local, como o `dispatches show` do
  disparo); `Estado: <state>`, com ` (parcial)` quando aberto; `origem: <origin> (declarada)`; `calculado agora` ou
  `versão <n> (<trigger>, <frozen_at>)`; `Alcance: <alvo> alunos-alvo, <entregues> entregues, <não entregues>
  não entregues, <falha> em falha de questão, <pendentes> pendentes`; `Não entregues por motivo:
  endpoint_blocked <n>, channel_refused <n>, engine_unavailable <n>, bot_no_access <n>, channel_unavailable
  <n>`; `Falha de questão por estágio: preparation <n>, grading <n>`; `Respondidas: <par>`, `Corretas: <par>`,
  `Parcialmente corretas: <par>`, `Incorretas: <par>`, `Não sei: <par>`; `Taxa de resposta: <par de taxas>`,
  `Taxa de corretas: …`, `Taxa de parcialmente corretas: …`, `Taxa de incorretas: …`, `Nota média: …`;
  `Tempo até responder, no prazo: mediana <d>, primeiro quartil <d>, terceiro quartil <d>; <n> observações,
  <k> negativas`; `Correções pendentes: <n>`; por último, `Distribuição e fricção: use --json ou --csv.`
  O par de contagens é `<a> no prazo · <b> com atrasadas`; o de taxas, o mesmo com cada lado em porcentagem
  com uma casa e vírgula (`50,0%`) ou `—` no nulo; a duração é `<ms arredondado> ms` ou `—`;
- `reports show --csv`: o corpo `text/csv` exatamente como veio (os bytes, com o BOM e o `\\r\\n`, sem o
  `_terminal_safe`, sem quebra de linha a mais); `--csv` com o `--json` global → código 2, sem chamada;
- `reports versions`: `<version>  <trigger>  <frozen_at>` por versão e, por último, `Próximo cursor: <c>`;
- `reports resend`: `Resumo reenviado: <dispatch_id>, versão <n>.`;
- `offerings digest show` e `set`: `Resumo diário de <code>: <ligado|desligado>, às <HH:MM> (<timezone>).`;
  `set` sem `--enabled` nem `--at` → código 2, sem chamada; `--enabled` aceita só `on`/`off` (outro valor →
  código 2, sem chamada); o `--at` vai como digitado (quem valida é a API).

Os pedidos são observados por um cliente HTTP que repassa ao `TestClient` (`_Recorder`): método, caminho,
consulta e corpo. O cliente (`cq_client.api.ApiClient`) é conferido direto nos métodos dos relatórios: `get_report`,
`get_report_csv` (pelo transporte `_call_text`: o texto do 200 `text/csv`; erro como o `_call`),
`list_report_versions`, `resend_report_summary`, `get_offering_digest` e `update_offering_digest`.

**Montagem**: oferecimento ativo com bot da Bot API falsa (atalhos da API); os disparos sem alvo pelos
tratadores reais do núcleo (validação, abertura, fechamento pela varredura do disparo e congelamento pela regra
de `REPORT_RULES`), com a posse pelo `TaskQueue.claim` só do tipo pedido; o cenário com respostas (2
alunos vinculados por SQL, o worker do console com rota e o `ConsoleStudent`), como em
`tests/integration/test_reports_api.py`: A responde `6.5` (correta) 10 s depois da entrega, B não responde.
Horários pelo `FakeClock` (2031-01-01T00:00:00Z; `America/Sao_Paulo` é `-03` em 2031). Ids, cursores de evento
e pseudônimos nunca como literal: vêm da API.

**Vermelho no esqueleto**: o CLI não tem os grupos `reports` nem `offerings digest` (código 2, comando
desconhecido), as rotas dos relatórios não existem (os atalhos da API afirmam 200 e recebem 404), os métodos novos do
`ApiClient` e o `reports.freeze_task_handler` lançam `NotImplementedError`. As asserções sobre o disparo ficam na
mesma função de um caso que falha.
Ficam verdes depois da implementação.
"""

from __future__ import annotations

import io
import itertools
import json
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest

from cq_client.api import ApiClient, ApiError
from cq_server import reports, scheduler
from cq_server.adapters.console import ConsoleChannel, ConsoleInbox
from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    confirm_dispatch,
    create_offering,
    create_token,
    get_dispatch,
    get_offering_digest,
    get_report,
    import_students,
    list_report_versions,
    prepare_dispatch,
    register_bot,
    resend_report_summary,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.console_student import ConsoleStudent
from tests.helpers.contract import MESSAGES, REPORT_CSV_MEDIA, assert_matches
from tests.helpers.engine import preset

ADMIN = auth(ADMIN_TOKEN)
BASE_URL = "http://testserver"
TZ = "America/Sao_Paulo"
REPO = "https://repos.cq.test/disciplina/questoes-cli-relatorio.git"
COMMIT = "c11e7a0b" * 5
QID = "number"
CONTROL_QID = "integer"
CONTROL_TITLE = "Velocidade \x1b[31mmédia\x1b[0m\x07\nlinha"
WINDOW = timedelta(minutes=10)
WORKER = "teste-cli-relatorios"
ENGINE_TIMEOUT = 10.0
WAIT = 20.0
EMPTY = "—"
LAST_LINE = "Distribuição e fricção: use --json ou --csv."
# Controles crus que nunca podem sair no texto do CLI (a quebra de linha só como fim de linha).
_RAW_CONTROL = "".join(chr(c) for c in [*range(0x00, 0x0A), *range(0x0B, 0x20), *range(0x7F, 0xA0)])
MISSING_DISPATCH_ID = "0d0d0d0d-0d0d-4d0d-8d0d-0d0d0d0d0d0d"

_counter = itertools.count(1)
_users = itertools.count(1500)


# ---------------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------------


def _out(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"exceção não tratada no CLI: {result.exception!r}")
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _ok(result: Any) -> None:
    _no_traceback(result)
    assert result.exit_code == 0, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stderr == ""


def _failed(result: Any, code: int) -> None:
    _no_traceback(result)
    assert result.exit_code == code, f"código {result.exit_code}; stderr: {result.stderr!r}"
    assert result.stdout == ""


def _usage(result: Any, http: Any) -> None:
    """Uso incorreto: código 2, `Uso incorreto: …` e nenhuma chamada à API."""
    _failed(result, 2)
    assert result.stderr.startswith("Uso incorreto:"), result.stderr
    assert http.requests == []


def _assert_error(result: Any, json_mode: bool, code: str, *, text_lines: tuple[str, ...] = (),
                  **extra: Any) -> None:
    """Código 1, saída padrão vazia e o erro exato (texto ou corpo da API no stderr)."""
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == {"error": {"code": code, "message": MESSAGES[code], **extra}}
    else:
        assert result.stderr == _out(f"Erro: {MESSAGES[code]}", *text_lines)


def _safe(text: str) -> str:
    """O `_terminal_safe` esperado: controles C0/C1 e quebras de linha como escapes do `repr`."""
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _local(moment: str, zone: str) -> str:
    return datetime.fromisoformat(moment.replace("Z", "+00:00")).astimezone(ZoneInfo(zone)).strftime(
        "%Y-%m-%d %H:%M")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _Recorder:
    """Cliente HTTP que repassa ao `TestClient` e guarda cada pedido (método, caminho, consulta e corpo)."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        split = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(split.query)
        params = kwargs.get("params")
        if isinstance(params, dict):
            query += [(k, str(v)) for k, v in params.items()]
        elif params:
            query += [(k, str(v)) for k, v in params]
        self.requests.append({"method": method, "path": split.path, "query": sorted(query),
                              "json": kwargs.get("json")})
        return self._client.request(method, url, **kwargs)


# --- texto esperado de `reports show` -----------------------------------------------------------------


def _pair(count: dict) -> str:
    return f"{count['on_time']} no prazo · {count['with_late']} com atrasadas"


def _percent(value: float | None) -> str:
    return EMPTY if value is None else f"{value * 100:.1f}".replace(".", ",") + "%"


def _rates(rate: dict) -> str:
    return f"{_percent(rate['on_time'])} no prazo · {_percent(rate['with_late'])} com atrasadas"


def _duration(ms: float | None) -> str:
    return EMPTY if ms is None else f"{ms:.0f} ms"


def _report_text(body: dict) -> str:
    """O texto de `reports show` para o `Report` `body` (ver o cabeçalho do módulo)."""
    d, reach, perf, times = body["dispatch"], body["reach"], body["performance"], body["response_time"]
    zone = body["timezone"]
    frozen = body["frozen"]
    by_reason = reach["not_delivered_by_reason"]
    by_stage = reach["question_failed_by_stage"]
    return _out(
        f"Relatório do disparo {d['dispatch_id']} em {body['offering']['code']}",
        f"Questão: {_safe(d['question']['qid'])} @ {d['question']['commit_short']}",
        f"Título: {EMPTY if d['title'] is None else _safe(d['title'])}",
        f"Abertura: {d['opens_at']} ({_local(d['opens_at'], zone)} {zone})",
        f"Fechamento: {d['closes_at']} ({_local(d['closes_at'], zone)} {zone})",
        f"Estado: {d['state']}" + (" (parcial)" if d["partial"] else ""),
        f"origem: {d['origin']} (declarada)",
        "calculado agora" if frozen is None else f"versão {frozen['version']} ({frozen['trigger']}, {frozen['frozen_at']})",
        f"Alcance: {reach['target']} alunos-alvo, {reach['delivered']} entregues, {reach['not_delivered']} não "
        f"entregues, {reach['question_failed']} em falha de questão, {reach['pending']} pendentes",
        "Não entregues por motivo: " + ", ".join(f"{k} {v}" for k, v in by_reason.items()),
        "Falha de questão por estágio: " + ", ".join(f"{k} {v}" for k, v in by_stage.items()),
        f"Respondidas: {_pair(perf['answered'])}",
        f"Corretas: {_pair(perf['correct'])}",
        f"Parcialmente corretas: {_pair(perf['partially_correct'])}",
        f"Incorretas: {_pair(perf['incorrect'])}",
        f"Não sei: {_pair(perf['dont_know'])}",
        f"Taxa de resposta: {_rates(perf['response_rate'])}",
        f"Taxa de corretas: {_rates(perf['correct_rate'])}",
        f"Taxa de parcialmente corretas: {_rates(perf['partially_correct_rate'])}",
        f"Taxa de incorretas: {_rates(perf['incorrect_rate'])}",
        f"Nota média: {_rates(perf['mean_score'])}",
        f"Tempo até responder, no prazo: mediana {_duration(times['median_ms'])}, primeiro quartil "
        f"{_duration(times['q1_ms'])}, terceiro quartil {_duration(times['q3_ms'])}; {times['observations']} "
        f"observações, {times['negative']} negativas",
        f"Correções pendentes: {body['grading_pending']}",
        LAST_LINE,
    )


def _assert_show(result: Any, body: dict) -> None:
    _ok(result)
    assert result.stdout == _report_text(body)
    assert not any(ch in result.stdout.replace("\n", "") for ch in _RAW_CONTROL), "controle cru na saída"


# --- montagem -------------------------------------------------------------------------------------------


class _Env:
    def __init__(self, request: Any) -> None:
        self.request = request
        self.client = request.getfixturevalue("client")
        self.sql = request.getfixturevalue("sql")
        self.db_url = request.getfixturevalue("db_url")
        self.clock = request.getfixturevalue("clock")
        self.settings = request.getfixturevalue("settings")
        self.bot_api = request.getfixturevalue("bot_api")
        self.engine_fake = request.getfixturevalue("engine_fake")
        self.repo_fetcher = request.getfixturevalue("repo_fetcher")
        self.closers: list[Callable[[], Any]] = []

    def engine(self) -> HttpQuestionEngine:
        engine = HttpQuestionEngine(EngineSettings(engine_url=self.engine_fake.base_url,
                                                   timeout_seconds=ENGINE_TIMEOUT))
        self.closers.append(engine.close)
        return engine


@pytest.fixture
def env(request: pytest.FixtureRequest, worker_thread: Any) -> Iterator[_Env]:
    made = _Env(request)
    made.engine_fake.program((REPO, COMMIT, QID), preset("number"))
    made.engine_fake.program((REPO, COMMIT, CONTROL_QID), preset("integer", title=CONTROL_TITLE))
    yield made
    worker_thread.stop()
    for close in reversed(made.closers):
        close()


def _wait(predicate: Callable[[], Any], what: str) -> Any:
    deadline = time.monotonic() + WAIT
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"não aconteceu em {WAIT} s: {what}")
        time.sleep(0.02)


def _active_offering(env: _Env) -> dict:
    offering = create_offering(env.client, ADMIN_TOKEN, code=f"cli-relatorio-{next(_counter)}", timezone=TZ)
    register_bot(env.client, ADMIN_TOKEN, offering["code"], env.bot_api.token_of(env.bot_api.add_bot()))
    assert activate_bot(env.client, ADMIN_TOKEN, offering["code"])["state"] == "active"
    return offering


def _drain(env: _Env, task_type: str, handler: Any) -> int:
    from cq_server.tasks import TaskQueue

    queue = TaskQueue(env.db_url, clock=env.clock, settings=env.settings)
    processed = 0
    try:
        while (task := queue.claim(WORKER, task_types=[task_type])) is not None:
            handler(task)
            assert queue.complete(task.id, WORKER)
            processed += 1
            assert processed <= 40
        return processed
    finally:
        queue.close()
        handler.close()


def _sweep(env: _Env, rules: Any) -> None:
    with psycopg.connect(env.db_url, autocommit=True) as conn:
        scheduler.sweep_once(conn, now=env.clock.now(), retry_gap=float(env.settings.backoff_max_seconds),
                             rules=rules)


def _prepare(env: _Env, code: str, *, qid: str = QID) -> str:
    return prepare_dispatch(env.client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid=qid,
                            closes_at=iso(env.clock.now() + WINDOW))["dispatch_id"]


def _validate(env: _Env) -> None:
    from cq_server.dispatches import VALIDATE_TASK_TYPE, validate_task_handler

    _drain(env, VALIDATE_TASK_TYPE, validate_task_handler(
        env.db_url, clock=env.clock, settings=env.settings, engine=env.engine(), fetcher=env.repo_fetcher,
        channel=ConsoleChannel(io.StringIO())))


def _open(env: _Env, code: str, *, qid: str = QID) -> str:
    from cq_server.dispatches import OPEN_TASK_TYPE, open_task_handler

    dispatch_id = _prepare(env, code, qid=qid)
    _validate(env)
    confirm_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)
    _drain(env, OPEN_TASK_TYPE, open_task_handler(env.db_url, clock=env.clock, settings=env.settings))
    assert get_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)["state"] == "open"
    return dispatch_id


def _close(env: _Env, code: str, dispatch_id: str) -> None:
    from cq_server.dispatches import CLOSE_TASK_TYPE, close_task_handler

    closes_at = _at(get_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)["closes_at"])
    env.clock.set(max(env.clock.now(), closes_at + timedelta(seconds=1)))
    _sweep(env, scheduler.DISPATCH_RULES)
    _drain(env, CLOSE_TASK_TYPE, close_task_handler(env.db_url, clock=env.clock, settings=env.settings,
                                                    channel=ConsoleChannel(io.StringIO())))
    assert get_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)["state"] == "closed"


def _freeze(env: _Env) -> int:
    handler = reports.freeze_task_handler(env.db_url, clock=env.clock, settings=env.settings,
                                          channel=ConsoleChannel(io.StringIO()))
    _sweep(env, [rule for rule in scheduler.REPORT_RULES if rule.task_type == reports.FREEZE_TASK_TYPE])
    return _drain(env, reports.FREEZE_TASK_TYPE, handler)


def _closed_and_frozen(env: _Env, code: str, *, qid: str = QID) -> tuple[str, datetime]:
    dispatch_id = _open(env, code, qid=qid)
    _close(env, code, dispatch_id)
    frozen_at = env.clock.now()
    assert _freeze(env) == 1
    return dispatch_id, frozen_at


def _archive(env: _Env, code: str) -> None:
    offering = env.client.get(f"/v1/offerings/{code}", headers=ADMIN).json()
    if offering["state"] == "active":
        assert env.client.post(f"/v1/offerings/{code}/close", headers=ADMIN).status_code == 200
    prepared = env.client.post(f"/v1/offerings/{code}/archive/prepare", headers=ADMIN)
    assert prepared.status_code == 200, prepared.text[:300]
    archived = env.client.post(f"/v1/offerings/{code}/archive",
                               json={"confirmation_code": prepared.json()["confirmation_code"]}, headers=ADMIN)
    assert archived.status_code == 200 and archived.json()["state"] == "archived"


def _answered(env: _Env, worker_thread: Any) -> tuple[dict, str]:
    """O cenário com respostas (ver o docstring do módulo): o disparo aberto, A respondeu, B não."""
    offering = _active_offering(env)
    code = offering["code"]
    bot_row = int(env.sql.execute("SELECT b.id FROM bots b JOIN offerings o ON o.id = b.offering_id "
                                  "WHERE o.code = %s", (code,)).fetchone()[0])
    n = next(_counter)
    rows = [{"name": f"Pessoa Cli Relatorio {n:03d} {k}", "ra": f"56{n:04d}{k:04d}",
             "email": f"pessoa.cli.relatorio.{n}.{k}@turma.example.org"} for k in range(2)]
    import_students(env.client, ADMIN_TOKEN, code, rows)
    stream = io.StringIO()
    students = []
    for row in rows:
        person_row, pseudo, enrollment = env.sql.execute(
            "SELECT p.id, p.pseudo_id::text, e.id FROM identities i JOIN people p ON p.id = i.person_id "
            "JOIN enrollments e ON e.person_id = p.id JOIN offerings o ON o.id = e.offering_id "
            "WHERE i.ra = %s AND o.code = %s", (row["ra"], code)).fetchone()
        now = env.clock.now()
        env.sql.execute("UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                        "terms_accepted_at = %s WHERE id = %s", (now, now, enrollment))
        chat_id = make_user(next(_users)).user_id
        env.sql.execute("INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                        "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
                        (person_row, bot_row, chat_id, now, now))
        inbox = ConsoleInbox(env.db_url, clock=env.clock)
        env.closers.append(inbox.close)
        students.append((person_row, ConsoleStudent(inbox, bot_id=bot_row, chat_id=chat_id, recipient=pseudo,
                                                    source=stream)))
    worker_thread(channel=ConsoleChannel(stream), console_routes=True, engine=env.engine(),
                  fetcher=env.repo_fetcher, delivery=DeliverySettings())
    dispatch_id = prepare_dispatch(env.client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid=QID,
                                   closes_at=iso(env.clock.now() + WINDOW))["dispatch_id"]
    _wait(lambda: get_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)["state"] == "scheduled", "a validação")
    confirm_dispatch(env.client, ADMIN_TOKEN, code, dispatch_id)
    person_row, student = students[0]

    def prompt() -> Any:
        lines = student.lines()
        questions = [line for line in lines if line.answerable and not line.has_rich_fields]
        if len(questions) != 1:
            return None
        prompts = [line for line in lines if line.reply_request is not None
                   and str(line.reply_to) == questions[0].channel_ref]
        return prompts[-1] if prompts else None

    target = _wait(prompt, "a questão e o pedido do campo de A")
    _wait(lambda: [r[0] for r in env.sql.execute(
        "SELECT d.state FROM deliveries d JOIN dispatches x ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s",
        (uuid.UUID(dispatch_id),)).fetchall()] == ["delivered", "delivered"], "as duas entregas")
    sent_at = env.sql.execute(
        "SELECT d.sent_at FROM deliveries d JOIN enrollments e ON e.id = d.enrollment_id JOIN dispatches x "
        "ON x.id = d.dispatch_id WHERE x.dispatch_uuid = %s AND e.person_id = %s",
        (uuid.UUID(dispatch_id), person_row)).fetchone()[0]
    env.clock.set(sent_at + timedelta(seconds=10))
    student.reply(target, "6.5")
    _wait(lambda: env.sql.execute(
        "SELECT s.outcome, s.valid FROM submissions s JOIN deliveries d ON d.id = s.delivery_id "
        "JOIN enrollments e ON e.id = d.enrollment_id WHERE e.person_id = %s", (person_row,)).fetchall()
        == [("graded", True)], "a correção de A")
    return offering, dispatch_id


# ---------------------------------------------------------------------------------------------------
# reports show / reports versions
# ---------------------------------------------------------------------------------------------------


def test_text_show_of_an_open_report_with_pairs_rates_and_times(env: _Env, cli_invoke: Any,
                                                               worker_thread: Any) -> None:
    """Contrato: `cq reports show REF DISPATCH_ID` do disparo aberto do
    cenário com respostas imprime o texto exato do cabeçalho do módulo (pelo código e pelo `offering_id`), com
    `Estado: open (parcial)`, `calculado agora` e os números do cenário (A correta no prazo, B sem resposta):
    `Respondidas: 1 no prazo · 1 com atrasadas`, `Taxa de resposta: 50,0% no prazo · 50,0% com atrasadas`,
    `Nota média: 100,0% …`, o tempo de A (10 000 ms) nos três e a última linha da orientação para `--json` e
    `--csv` (nada da distribuição)."""
    offering, dispatch_id = _answered(env, worker_thread)
    code = offering["code"]
    body = get_report(env.client, code, dispatch_id)
    for ref in (code, offering["offering_id"]):
        result = cli_invoke(["reports", "show", ref, dispatch_id])
        _assert_show(result, body)
    lines = cli_invoke(["reports", "show", code, dispatch_id]).stdout.splitlines()
    for line in ("Estado: open (parcial)", "calculado agora", "Respondidas: 1 no prazo · 1 com atrasadas",
                 "Corretas: 1 no prazo · 1 com atrasadas", "Incorretas: 0 no prazo · 0 com atrasadas",
                 "Taxa de resposta: 50,0% no prazo · 50,0% com atrasadas",
                 "Taxa de corretas: 50,0% no prazo · 50,0% com atrasadas",
                 "Nota média: 100,0% no prazo · 100,0% com atrasadas",
                 "Tempo até responder, no prazo: mediana 10000 ms, primeiro quartil 10000 ms, terceiro quartil "
                 "10000 ms; 1 observações, 0 negativas",
                 "origem: api (declarada)", "Correções pendentes: 0", LAST_LINE):
        assert line in lines, f"falta a linha {line!r}"
    assert lines[-1] == LAST_LINE
    assert "6.5" not in "\n".join(lines), "a distribuição não sai no texto"


def test_text_show_of_the_frozen_version_with_terminal_safe_title_and_empty_values(env: _Env,
                                                                                  cli_invoke: Any) -> None:
    """Contrato (`_terminal_safe` no título; `—` no vazio): disparo sem alvo
    da questão com título com ESC, BEL e quebra de linha (controle positivo: a API devolve o título cru),
    fechado e congelado: `--version 1` e `--version latest` imprimem `versão 1 (closing, <frozen_at>)`,
    `Estado: closed`, as taxas e as durações com `—` e o título escapado; sem `--version`, `calculado agora`;
    nenhum controle cru na saída; o `--version` vai na consulta como digitado."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, frozen_at = _closed_and_frozen(env, code, qid=CONTROL_QID)
    frozen = get_report(env.client, code, dispatch_id, version=1)
    assert frozen["dispatch"]["title"] == CONTROL_TITLE, "controle positivo: o título chega cru na API"
    assert frozen["frozen"] == {"version": 1, "trigger": "closing", "frozen_at": iso(frozen_at)}
    http = _Recorder(env.client)
    for version in ("1", "latest"):
        result = cli_invoke(["reports", "show", code, dispatch_id, "--version", version], http=http)
        _assert_show(result, frozen)
        assert f"versão 1 (closing, {iso(frozen_at)})" in result.stdout.splitlines()
        assert f"Título: {_safe(CONTROL_TITLE)}" in result.stdout.splitlines()
        assert "Taxa de resposta: — no prazo · — com atrasadas" in result.stdout.splitlines()
        assert http.requests[-1]["query"] == [("version", version)]
    now_body = get_report(env.client, code, dispatch_id)
    _assert_show(cli_invoke(["reports", "show", code, dispatch_id]), now_body)
    assert "Estado: closed" in cli_invoke(["reports", "show", code, dispatch_id]).stdout.splitlines()


def test_json_show_equals_the_api_and_csv_prints_the_body_as_received(env: _Env, cli_invoke: Any) -> None:
    """Contrato: `--json` (global) imprime o `Report` da API (no momento e com
    `--version 1`); `--csv` imprime exatamente os bytes do corpo `text/csv` da API (BOM, `\\r\\n`, sem
    `_terminal_safe` nem quebra a mais), no momento e com `--version 1`, pedindo `format=csv`."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, _ = _closed_and_frozen(env, code, qid=CONTROL_QID)
    for extra, version in (([], None), (["--version", "1"], 1)):
        result = cli_invoke(["reports", "show", code, dispatch_id, *extra], json=True)
        _ok(result)
        body = json.loads(result.stdout)
        assert_matches("reports.get", 200, body)
        assert body == get_report(env.client, code, dispatch_id, version=version)
        http = _Recorder(env.client)
        shown = cli_invoke(["reports", "show", code, dispatch_id, *extra, "--csv"], http=http)
        _ok(shown)
        params = {"format": "csv", **({} if version is None else {"version": version})}
        api = env.client.get(f"/v1/offerings/{code}/dispatches/{dispatch_id}/report", params=params, headers=ADMIN)
        assert (api.status_code, api.headers["content-type"]) == (200, REPORT_CSV_MEDIA)
        assert shown.stdout_bytes == api.content
        assert shown.stdout_bytes.startswith(b"\xef\xbb\xbf") and shown.stdout_bytes.endswith(b"\r\n")
        assert [(r["method"], r["query"]) for r in http.requests] == [
            ("GET", sorted((k, str(v)) for k, v in params.items()))]


@pytest.mark.parametrize("extra", [[], ["--version", "1"]], ids=["agora", "versao"])
def test_csv_with_the_global_json_is_a_usage_error_without_calling_the_api(env: _Env, cli_invoke: Any,
                                                                           extra: list[str]) -> None:
    """Contrato: `cq --json reports show REF DISPATCH_ID --csv` → código 2, `Uso
    incorreto: …`, saída vazia e nenhuma chamada à API; sem o `--json`, o mesmo `--csv` funciona (controle)."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, _ = _closed_and_frozen(env, code)
    http = _Recorder(env.client)
    _usage(cli_invoke(["reports", "show", code, dispatch_id, *extra, "--csv"], json=True, http=http), http)
    _ok(cli_invoke(["reports", "show", code, dispatch_id, *extra, "--csv"]))


def test_versions_text_and_json(env: _Env, cli_invoke: Any) -> None:
    """Contrato: `cq reports versions` do aberto → só `Próximo cursor: 0`; do
    congelado com dois reenvios → `<version>  <trigger>  <frozen_at>` por versão e `Próximo cursor: 3`;
    `--after 1 --limit 1` → só a versão 2 e `Próximo cursor: 2`, com `after` e `limit` na consulta; `--json` →
    o corpo da API."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, frozen_at = _closed_and_frozen(env, code)
    opened = _open(env, code)
    result = cli_invoke(["reports", "versions", code, opened])
    _ok(result)
    assert result.stdout == _out("Próximo cursor: 0")
    resend_report_summary(env.client, code, dispatch_id)
    resend_report_summary(env.client, code, dispatch_id)
    at = iso(frozen_at)
    result = cli_invoke(["reports", "versions", code, dispatch_id])
    _ok(result)
    assert result.stdout == _out(f"1  closing  {at}", f"2  resend  {at}", f"3  resend  {at}", "Próximo cursor: 3")
    http = _Recorder(env.client)
    paged = cli_invoke(["reports", "versions", code, dispatch_id, "--after", "1", "--limit", "1"], http=http)
    _ok(paged)
    assert paged.stdout == _out(f"2  resend  {at}", "Próximo cursor: 2")
    assert http.requests[0]["query"] == [("after", "1"), ("limit", "1")]
    as_json = cli_invoke(["reports", "versions", code, dispatch_id], json=True)
    _ok(as_json)
    assert json.loads(as_json.stdout) == list_report_versions(env.client, code, dispatch_id)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_show_and_versions_errors_keep_the_base_form(env: _Env, cli_invoke: Any, json_mode: bool) -> None:
    """Mensagens exatas e forma dos erros: disparo inexistente, de outro oferecimento e oferecimento
    inexistente → `Erro: Recurso não encontrado.`; disparo em `validating` → `invalid_dispatch_state`;
    `--version 2` sem a versão 2 → `not_found`; `--version 0` → `  - version: invalid_format`; `DISPATCH_ID`
    malformado → `  - dispatch_id: invalid_format`, sem eco; `versions --limit 0` → `  - limit: out_of_range`;
    token sem `offerings:read` → `Permissão necessária: offerings:read`. Todos com código 1."""
    offering = _active_offering(env)
    other = _active_offering(env)
    code = offering["code"]
    dispatch_id, _ = _closed_and_frozen(env, code)
    validating = _prepare(env, code)
    no_read, _ = create_token(env.client, ADMIN_TOKEN, ["people:read"])

    def show(*args: str, token: str = ADMIN_TOKEN) -> Any:
        return cli_invoke(["reports", "show", *args], json=json_mode, token=token)

    for ref, did in ((code, MISSING_DISPATCH_ID), (other["code"], dispatch_id), ("codigo-inexistente", dispatch_id)):
        _assert_error(show(ref, did), json_mode, "not_found")
        _assert_error(cli_invoke(["reports", "versions", ref, did], json=json_mode), json_mode, "not_found")
    _assert_error(show(code, validating), json_mode, "invalid_dispatch_state")
    _assert_error(show(code, dispatch_id, "--version", "2"), json_mode, "not_found")
    _assert_error(show(code, dispatch_id, "--version", "0"), json_mode, "invalid_request",
                  text_lines=("  - version: invalid_format",), fields=[{"field": "version", "reason": "invalid_format"}])
    malformed = show(code, "disparosentinela")
    _assert_error(malformed, json_mode, "invalid_request", text_lines=("  - dispatch_id: invalid_format",),
                  fields=[{"field": "dispatch_id", "reason": "invalid_format"}])
    assert "disparosentinela" not in malformed.stderr
    _assert_error(cli_invoke(["reports", "versions", code, dispatch_id, "--limit", "0"], json=json_mode), json_mode,
                  "invalid_request", text_lines=("  - limit: out_of_range",),
                  fields=[{"field": "limit", "reason": "out_of_range"}])
    _assert_error(show(code, dispatch_id, token=no_read), json_mode, "forbidden",
                  text_lines=("Permissão necessária: offerings:read",), permission="offerings:read")


def test_api_client_report_methods(env: _Env) -> None:
    """Contrato do cliente: `get_report` devolve o `Report` da API (com `version`);
    `get_report_csv` devolve o texto do 200 `text/csv` (com o BOM), pelo `_call_text`; um erro dele vira
    `ApiError` com o envelope da API (404 e 409), como o `_call`; `list_report_versions` (com `after`/`limit`) e
    `resend_report_summary` devolvem os corpos da API; o cliente manda `format=csv` só no CSV."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, _ = _closed_and_frozen(env, code)
    validating = _prepare(env, code)
    seen: list[Any] = []
    env.client.event_hooks["request"].append(seen.append)
    try:
        api = ApiClient(BASE_URL, ADMIN_TOKEN, http=env.client)
        assert api.get_report(code, dispatch_id) == get_report(env.client, code, dispatch_id)
        assert api.get_report(code, dispatch_id, version=1) == get_report(env.client, code, dispatch_id, version=1)
        expected_csv = env.client.get(f"/v1/offerings/{code}/dispatches/{dispatch_id}/report",
                                      params={"format": "csv"}, headers=ADMIN).content.decode("utf-8")
        seen.clear()
        assert api.get_report_csv(code, dispatch_id) == expected_csv
        assert sorted(seen[0].url.params.multi_items()) == [("format", "csv")]
        assert expected_csv.startswith("﻿")
        for did, status, code_name in ((MISSING_DISPATCH_ID, 404, "not_found"),
                                       (validating, 409, "invalid_dispatch_state")):
            with pytest.raises(ApiError) as raised:
                api.get_report_csv(code, did)
            assert (raised.value.status, raised.value.body) == (
                status, {"error": {"code": code_name, "message": MESSAGES[code_name]}})
        resent = api.resend_report_summary(code, dispatch_id)
        assert resent["frozen"]["version"] == 2
        assert_matches("reports.resend_summary", 200, resent)
        assert api.list_report_versions(code, dispatch_id, after=1, limit=1) == list_report_versions(
            env.client, code, dispatch_id, after=1, limit=1)
    finally:
        env.client.event_hooks["request"].remove(seen.append)


# ---------------------------------------------------------------------------------------------------
# reports resend
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_resend_text_json_and_errors(env: _Env, cli_invoke: Any, json_mode: bool) -> None:
    """Contrato: `cq reports resend REF DISPATCH_ID` → `Resumo reenviado:
    <dispatch_id>, versão 2.` (texto) ou o corpo da API (`--json`, a versão 2 com `trigger = resend`), com um
    `POST` sem corpo; o disparo aberto → `invalid_dispatch_state`; token só de leitura → `Permissão necessária:
    offerings:write`; o oferecimento arquivado → `invalid_state` com `Estado atual: archived`."""
    offering = _active_offering(env)
    code = offering["code"]
    dispatch_id, frozen_at = _closed_and_frozen(env, code)
    opened = _open(env, code)
    reader, _ = create_token(env.client, ADMIN_TOKEN, ["offerings:read"])
    http = _Recorder(env.client)
    result = cli_invoke(["reports", "resend", code, dispatch_id], json=json_mode, http=http)
    _ok(result)
    now = env.clock.now()
    expected = {"offering": {"offering_id": offering["offering_id"], "code": code}, "dispatch_id": dispatch_id,
                "frozen": {"version": 2, "trigger": "resend", "frozen_at": iso(now)}}
    if json_mode:
        assert json.loads(result.stdout) == expected
    else:
        assert result.stdout == _out(f"Resumo reenviado: {dispatch_id}, versão 2.")
    assert [(r["method"], r["path"], r["json"]) for r in http.requests] == [
        ("POST", f"/v1/offerings/{code}/dispatches/{dispatch_id}/report/resend", None)]
    assert [i["version"] for i in list_report_versions(env.client, code, dispatch_id)["items"]] == [1, 2]

    _assert_error(cli_invoke(["reports", "resend", code, opened], json=json_mode), json_mode, "invalid_dispatch_state")
    _assert_error(cli_invoke(["reports", "resend", code, dispatch_id], json=json_mode, token=reader), json_mode,
                  "forbidden", text_lines=("Permissão necessária: offerings:write",), permission="offerings:write")
    _close(env, code, opened)
    _archive(env, code)
    _assert_error(cli_invoke(["reports", "resend", code, dispatch_id], json=json_mode), json_mode, "invalid_state",
                  text_lines=("Estado atual: archived",), state="archived")


# ---------------------------------------------------------------------------------------------------
# offerings digest show / set
# ---------------------------------------------------------------------------------------------------


def _digest_line(code: str, enabled: bool, local_time: str) -> str:
    return _out(f"Resumo diário de {code}: {'ligado' if enabled else 'desligado'}, às {local_time} ({TZ}).")


def test_digest_show_and_set_text_and_json(env: _Env, cli_invoke: Any) -> None:
    """Contrato: `cq offerings digest show REF` sem configuração →
    `Resumo diário de <code>: desligado, às 20:00 (America/Sao_Paulo).`; `set --enabled on --at 07:30` → `ligado,
    às 07:30`, com o `PATCH` de corpo exato `{"enabled": true, "local_time": "07:30"}`; `set --at 21:15` → só
    `{"local_time": "21:15"}`; `set --enabled off` → só `{"enabled": false}`; pelo `offering_id` também;
    `--json` → o corpo da API; os métodos `get_offering_digest` e `update_offering_digest` do cliente."""
    offering = _active_offering(env)
    code = offering["code"]
    result = cli_invoke(["offerings", "digest", "show", code])
    _ok(result)
    assert result.stdout == _digest_line(code, False, "20:00")
    for args, body, enabled, local_time in (
            (["--enabled", "on", "--at", "07:30"], {"enabled": True, "local_time": "07:30"}, True, "07:30"),
            (["--at", "21:15"], {"local_time": "21:15"}, True, "21:15"),
            (["--enabled", "off"], {"enabled": False}, False, "21:15")):
        http = _Recorder(env.client)
        result = cli_invoke(["offerings", "digest", "set", code, *args], http=http)
        _ok(result)
        assert result.stdout == _digest_line(code, enabled, local_time)
        assert [(r["method"], r["path"], r["json"]) for r in http.requests] == [
            ("PATCH", f"/v1/offerings/{code}/digest", body)]
    shown = cli_invoke(["offerings", "digest", "show", offering["offering_id"]])
    _ok(shown)
    assert shown.stdout == _digest_line(code, False, "21:15")
    as_json = cli_invoke(["offerings", "digest", "show", code], json=True)
    _ok(as_json)
    assert json.loads(as_json.stdout) == get_offering_digest(env.client, code) == {
        "offering": {"offering_id": offering["offering_id"], "code": code}, "timezone": TZ, "enabled": False,
        "local_time": "21:15"}
    set_json = cli_invoke(["offerings", "digest", "set", code, "--enabled", "on"], json=True)
    _ok(set_json)
    assert json.loads(set_json.stdout) == {**get_offering_digest(env.client, code), "enabled": True}
    api = ApiClient(BASE_URL, ADMIN_TOKEN, http=env.client)
    assert api.get_offering_digest(code) == get_offering_digest(env.client, code)
    assert api.update_offering_digest(code, local_time="06:45")["local_time"] == "06:45"
    assert api.update_offering_digest(code, enabled=False) == get_offering_digest(env.client, code)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_digest_set_usage_errors_do_not_call_the_api(env: _Env, cli_invoke: Any, json_mode: bool) -> None:
    """Contrato: `offerings digest set REF` sem `--enabled` nem `--at` e `--enabled talvez` →
    código 2, `Uso incorreto: …`, nenhuma chamada; a configuração não muda."""
    offering = _active_offering(env)
    code = offering["code"]
    before = get_offering_digest(env.client, code)
    for args in ([], ["--enabled", "talvez"], ["--enabled", "ON"]):
        http = _Recorder(env.client)
        _usage(cli_invoke(["offerings", "digest", "set", code, *args], json=json_mode, http=http), http)
    assert get_offering_digest(env.client, code) == before


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_digest_errors_keep_the_base_form(env: _Env, cli_invoke: Any, json_mode: bool) -> None:
    """Contrato: `--at 7h` vai como digitado e a API recusa (`  - local_time:
    invalid_format`); oferecimento inexistente → `not_found` (show e set); o arquivado → `invalid_state` com
    `Estado atual: archived` no `set`, e o `show` continua respondendo; token só de leitura no `set` →
    `Permissão necessária: offerings:write`; token sem `offerings:read` no `show` → `offerings:read`."""
    offering = _active_offering(env)
    code = offering["code"]
    reader, _ = create_token(env.client, ADMIN_TOKEN, ["offerings:read"])
    no_read, _ = create_token(env.client, ADMIN_TOKEN, ["people:read"])
    http = _Recorder(env.client)
    refused = cli_invoke(["offerings", "digest", "set", code, "--at", "7h"], json=json_mode, http=http)
    _assert_error(refused, json_mode, "invalid_request", text_lines=("  - local_time: invalid_format",),
                  fields=[{"field": "local_time", "reason": "invalid_format"}])
    assert [r["json"] for r in http.requests] == [{"local_time": "7h"}]
    for args in (["show", "codigo-inexistente"], ["set", "codigo-inexistente", "--enabled", "on"]):
        _assert_error(cli_invoke(["offerings", "digest", *args], json=json_mode), json_mode, "not_found")
    _assert_error(cli_invoke(["offerings", "digest", "set", code, "--enabled", "on"], json=json_mode, token=reader),
                  json_mode, "forbidden", text_lines=("Permissão necessária: offerings:write",),
                  permission="offerings:write")
    _assert_error(cli_invoke(["offerings", "digest", "show", code], json=json_mode, token=no_read), json_mode,
                  "forbidden", text_lines=("Permissão necessária: offerings:read",), permission="offerings:read")
    draft = create_offering(env.client, ADMIN_TOKEN, code=f"cli-resumo-arquivado-{next(_counter)}", timezone=TZ)
    _archive(env, draft["code"])
    _assert_error(cli_invoke(["offerings", "digest", "set", draft["code"], "--at", "08:00"], json=json_mode),
                  json_mode, "invalid_state", text_lines=("Estado atual: archived",), state="archived")
    shown = cli_invoke(["offerings", "digest", "show", draft["code"]])
    _ok(shown)
    assert shown.stdout == _digest_line(draft["code"], False, "20:00")
