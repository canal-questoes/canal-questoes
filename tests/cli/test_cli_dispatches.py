"""CLI `cq dispatches prepare/show/list/confirm/cancel` e `cq messages list` (o CLI do disparo
e da resposta).

O CLI roda em processo pelo `cli_invoke` do conftest (`CliRunner` com o `TestClient` da API em
`obj["http"]`). As saídas de sucesso são comparadas com o corpo da API (`--json`) ou com
o texto para pessoas do disparo; os erros seguem a forma comum do CLI (`Erro:
<message>`, a linha `Estado atual: <state>` no `invalid_state`, `Permissão necessária: …` no 403, uma linha
por campo no 400; com `--json`, o corpo de erro da API no stderr; uso incorreto com código 2 e sem eco).
Os 3 códigos do disparo (`invalid_dispatch_state`, `window_expired`, `grading_pending`) saem só com `Erro:
<message>` (sem `fields`, `state` nem `permission`).

Saídas fixadas aqui (com as convenções dos demais comandos):
- `dispatches prepare`: `Disparo preparado em <code>: <dispatch_id> (validando).` e `Consulte com: cq
  dispatches show <code> <dispatch_id>`; o CLI não manda `origin` (vale o `X-CQ-Client: cli`), e o corpo
  do pedido só leva as opções dadas, com os horários como digitados;
- `dispatches show`: uma linha com o `dispatch_id` e o estado; a confirmação (`não confirmado` ou
  `confirmado em <confirmed_at>`); `repo`, `commit` e `qid`; abertura e fechamento, cada um numa linha com
  o horário UTC da API **e** a hora local (`AAAA-MM-DD HH:MM`) com o nome do fuso do oferecimento (o
  formato do `team link`); a linha `origem: <origin> (declarada)`; o motivo (mensagem e detalhes);
  as contagens como `<chave>: <n>`; a prévia recuada (título, enunciado, campos com tipo, rótulo, sufixo e
  dica, avisos, cada um numa linha que começa com espaço);
- `dispatches list`: `<dispatch_id>  <state>  <opens_at>  <closes_at>  <qid>` por disparo (`-` no horário
  nulo, como o `_dash` do CLI) e, por último, `Próximo cursor: <next_cursor>` (convenção das listas
  do CLI; sem ela a paginação pelo texto não teria o cursor);
- `dispatches confirm`: `Disparo confirmado: <dispatch_id>; abre <em <opens_at> | agora>, fecha em
  <closes_at>; <target_count> alunos-alvo agora.`;
- `dispatches cancel`: `Disparo cancelado: <dispatch_id>.`;
- `messages list`: `<created_at>  <alias>  <texto>` por mensagem e `Próximo cursor: <next_cursor>`.

`_terminal_safe`: todo texto de terceiros ou de aluno
impresso (prévia, motivos, `repo`, `qid`, mensagens livres) sai com os controles C0/C1 (inclusive o ESC
das sequências ANSI) e as quebras de linha como escapes visíveis, na forma do `repr` do Python (`\\n`,
`\\x1b`, `\\x07`, `\\x9b`); nenhuma saída de texto leva um controle cru além da quebra de linha final de
cada linha. O `--json` sai como a API (o `json.dumps` já escapa). Os textos com controles vêm do motor
falso (título e aviso `console_output` da prévia; motivo de "não suportada") e de mensagens livres; cada
caso tem o controle positivo de que a API devolve o controle cru.

Montagem: oferecimento ativo com bot da Bot API falsa (atalhos da API); a validação roda só as tarefas
`dispatch.validate`, com o tratador real montado como o `run_worker`, pelo mesmo ajudante da semente
(`tests.helpers.calls._validate_seeded_dispatches`, que processa só as validações), sobre o motor falso estrito (`engine_fake`) e a
obtenção falsa (`repo_fetcher`); os alunos-alvo (matrícula `student` ativa com endereço vigente no bot)
e os updates das mensagens livres são gravados por SQL nas tabelas de oferecimentos e vínculo, como em
`tests/integration/test_outbox_routes.py`, e a mensagem livre pela interface interna
`freemessages.store_free_message`. Horários pelo
`FakeClock` (começa em 2031-01-01T00:00:00Z; em 2031, `America/Sao_Paulo` é `-03`). Ids, cursores e
pseudônimos nunca como literal: vêm da API.

No esqueleto, o CLI não tem os grupos `dispatches` nem
`messages` (a primeira chamada do CLI sai com código 2, `Uso incorreto: comando desconhecido.`), as rotas
do disparo não existem (os atalhos da API afirmam 200 e recebem 404) e `store_free_message` lança
`NotImplementedError`. Ficam verdes depois da implementação.
"""

from __future__ import annotations

import itertools
import json
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    cancel_dispatch,
    confirm_dispatch,
    create_offering,
    create_token,
    get_dispatch,
    list_dispatches,
    list_free_messages,
    marker,
    prepare_dispatch,
    register_bot,
    register_person,
    uuid4_with_prefix,
)
from tests.helpers.bot_api import make_user
from tests.helpers.calls import _validate_seeded_dispatches
from tests.helpers.clock import START, iso
from tests.helpers.contract import MESSAGES, assert_matches
from tests.helpers.engine import FakeReason, FakeWarning, preset

ADMIN = auth(ADMIN_TOKEN)
TZ = "America/Sao_Paulo"
REPO = "https://repos.cq.test/disciplina/questoes-cli.git"
COMMIT = "c1d2" * 10
QID = "number"
UNSUPPORTED_QID = "unsupported"
WINDOW_MINUTES = 90

PREPARE_RE = re.compile(r"Disparo preparado em (\S+): ([0-9a-f-]{36}) \(validando\)\.\n"
                        r"Consulte com: cq dispatches show (\S+) ([0-9a-f-]{36})\n")
# Controles crus que nunca podem sair no texto do CLI (a quebra de linha só como fim de linha).
RAW_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")

# Textos de terceiros com controles (sequências ANSI, BEL, CSI de 8 bits e quebra de linha).
CONTROL_TITLE = "Velocidade \x1b[31mmédia\x1b[0m\x07"
CONTROL_WARNING = "saída do generate\nlinha 2 \x1b[2J\x9b0m fim"
CONTROL_REASON = "A questão usa \x1b]8;;https://x.test\x07pl-matrix-input\x1b]8;;\x07.\nOutra linha."

_update_ids = itertools.count(910_001)
_users = itertools.count(1)


# --------------------------------------------------------------------------------------------------
# Apoio


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
    """A hora local `AAAA-MM-DD HH:MM` de um horário UTC da API no fuso `zone`."""
    return datetime.fromisoformat(moment.replace("Z", "+00:00")).astimezone(ZoneInfo(zone)).strftime(
        "%Y-%m-%d %H:%M")


def _dash(value: Any) -> str:
    return "-" if value is None else str(value)


def _api_error(code: str, **extra: Any) -> dict:
    return {"error": {"code": code, "message": MESSAGES[code], **extra}}


def _text_error(code: str, *extra_lines: str) -> str:
    return _out(f"Erro: {MESSAGES[code]}", *extra_lines)


def _assert_error(result: Any, json_mode: bool, code: str, *, text_lines: tuple[str, ...] = (),
                  **extra: Any) -> None:
    """Código 1, saída padrão vazia e o erro exato (texto ou corpo da API no stderr)."""
    _failed(result, 1)
    if json_mode:
        assert json.loads(result.stderr) == _api_error(code, **extra)
    else:
        assert result.stderr == _text_error(code, *text_lines)


class _Recorder:
    """Cliente HTTP que repassa ao `TestClient` e guarda cada pedido: método, caminho, corpo JSON,
    consulta e cabeçalhos (para afirmar o que o CLI manda)."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append({"method": method, "path": urllib.parse.urlsplit(url).path,
                              "json": kwargs.get("json"), "params": kwargs.get("params"),
                              "headers": dict(kwargs.get("headers") or {})})
        return self._client.request(method, url, **kwargs)


def _active_offering(client: Any, bot_api: Any, **fields: Any) -> dict:
    """Um oferecimento `active` com bot registrado na Bot API falsa; devolve o `Offering`."""
    offering = create_offering(client, ADMIN_TOKEN, timezone=TZ, response_window_minutes=WINDOW_MINUTES,
                               **fields)
    register_bot(client, ADMIN_TOKEN, offering["code"], bot_api.token_of(bot_api.add_bot()))
    active = activate_bot(client, ADMIN_TOKEN, offering["code"])
    assert active["state"] == "active"
    return active


def _row_ids(sql: Any, code: str) -> tuple[int, int]:
    """(`offerings.id`, `bots.id`) do oferecimento `code`."""
    row = sql.execute("SELECT o.id, b.id FROM offerings o JOIN bots b ON b.offering_id = o.id "
                      "WHERE o.code = %s", (code,)).fetchone()
    assert row is not None, f"oferecimento sem bot: {code}"
    return int(row[0]), int(row[1])


def _student(client: Any, sql: Any, clock: Any, offering: dict, name: str) -> tuple[dict, int, int]:
    """Um aluno-alvo: cadastrado pela API, com matrícula `student` ativa (termo
    aceito) e endereço `active` no bot do oferecimento, gravados por SQL. Devolve (`Person`,
    `delivery_endpoints.id`, `chat_id`)."""
    person = register_person(client, ADMIN_TOKEN, name=name)
    person_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s", (person["pseudo_id"],)).fetchone()[0]
    offering_row, bot_row = _row_ids(sql, offering["code"])
    now = clock.now()
    sql.execute("INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
                "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
                (person_row, offering_row, now, now, now))
    chat_id = make_user(next(_users)).user_id
    endpoint = sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
        "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
        (person_row, bot_row, chat_id, now, now)).fetchone()[0]
    return person, int(endpoint), chat_id


def _free_message(db_url: str, sql: Any, clock: Any, offering: dict, endpoint: int, chat_id: int,
                  text: str) -> None:
    """Uma mensagem livre: o update `message` processado (`free_message`) gravado por SQL e a linha de
    `free_messages` pela interface interna (`store_free_message`, redação na gravação)."""
    from cq_server.events import load_write_identities
    from cq_server.freemessages import store_free_message

    offering_row, bot_row = _row_ids(sql, offering["code"])
    now = clock.now()
    update_row = sql.execute(
        "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, sender_id, "
        "text, command, details, received_at, state, outcome, processed_at) "
        "VALUES (%s, 0, %s, 'message', %s, 'private', %s, %s, NULL, %s, %s, 'processed', 'free_message', %s) "
        "RETURNING id",
        (bot_row, next(_update_ids), chat_id, chat_id, text,
         Jsonb({"message_id": 1, "date": int(now.timestamp())}), now, now)).fetchone()[0]
    with psycopg.connect(db_url) as conn:
        store_free_message(conn, offering_row_id=offering_row, endpoint_row_id=endpoint,
                           update_row_id=int(update_row), text=text, ambiguous=False, received_at=now,
                           now=now, identities=load_write_identities(conn), reply_interval_seconds=600)


def _prepare_cli(cli_invoke: Any, ref: str, code: str, *extra: str, http: Any = None,
                 qid: str = QID) -> str:
    """`cq dispatches prepare` com sucesso; confere as duas linhas e devolve o `dispatch_id`."""
    args = ["dispatches", "prepare", ref, "--repo", REPO, "--commit", COMMIT, "--qid", qid, *extra]
    result = cli_invoke(args) if http is None else cli_invoke(args, http=http)
    _ok(result)
    match = PREPARE_RE.fullmatch(result.stdout)
    assert match, f"saída fora do formato: {result.stdout!r}"
    dispatch_id = match.group(2)
    assert result.stdout == _out(f"Disparo preparado em {code}: {dispatch_id} (validando).",
                                 f"Consulte com: cq dispatches show {code} {dispatch_id}")
    return dispatch_id


def _prepare_api(client: Any, code: str, *, qid: str = QID, **kwargs: Any) -> str:
    return prepare_dispatch(client, ADMIN_TOKEN, code, repo=REPO, commit=COMMIT, qid=qid, **kwargs)["dispatch_id"]


def _program(engine_fake: Any) -> None:
    """Os presets do motor falso e as questões desta referência: `number` com título e aviso
    `console_output` com controles, e `unsupported` com um motivo com controles."""
    engine_fake.program_presets()
    engine_fake.program((REPO, COMMIT, QID), preset(
        "number", title=CONTROL_TITLE,
        variant_warnings=(FakeWarning("console_output", CONTROL_WARNING, "generate"),)))
    engine_fake.program((REPO, COMMIT, UNSUPPORTED_QID), preset(
        "unsupported", inspect_reasons=(FakeReason("unsupported_elements", CONTROL_REASON,
                                                   ("pl-matrix-input",)),)))


def _validate(request: Any, clock: Any, expected: int) -> None:
    """Roda as tarefas `dispatch.validate` pendentes com o tratador real (o ajudante da semente)."""
    _validate_seeded_dispatches(request, clock, expected=expected)


def _assert_show(stdout: str, body: dict) -> None:
    """O texto de `dispatches show` para o `Dispatch` `body` (ver o cabeçalho do módulo)."""
    assert stdout.endswith("\n"), "a saída termina em quebra de linha"
    assert RAW_CONTROL.search(stdout) is None, f"controle cru na saída: {stdout!r}"
    lines = stdout.split("\n")[:-1]
    stripped = [line.strip() for line in lines]
    assert any(body["dispatch_id"] in line and body["state"] in line for line in lines), (
        f"falta a linha com o disparo e o estado {body['state']}: {stdout!r}")
    if body["confirmed"]:
        assert f"confirmado em {body['confirmed_at']}" in stdout
        assert "não confirmado" not in stdout
    else:
        assert "não confirmado" in stdout
    question = body["question"]
    for text in (question["repo"], question["commit"], question["qid"]):
        assert _safe(text) in stdout, f"falta {text!r} na saída"
    assert f"origem: {body['origin']} (declarada)" in stripped
    for key in ("opens_at", "closes_at"):
        moment = body[key]
        if moment is not None:
            local = _local(moment, body["timezone"])
            assert any(moment in line and local in line and body["timezone"] in line for line in lines), (
                f"{key}: falta a linha com {moment}, {local} e {body['timezone']}: {stdout!r}")
    rejection = body["rejection"]
    if rejection is not None:
        assert rejection["message"] in stdout
        for detail in rejection["details"]:
            assert _safe(detail) in stdout, f"falta o detalhe {detail!r} na saída"
    for key, count in body["deliveries"].items():
        assert re.search(rf"(?<![\w]){re.escape(key)}: {count}(?!\d)", stdout), f"falta a contagem {key}"
    preview = body["preview"]
    if preview is not None:
        texts = [preview["title"], preview["statement"]]
        for item in preview["fields"]:
            texts += [item["type"], item["label"], item["suffix"], item["hint"]]
        texts += [warning["message"] for warning in preview["warnings"]]
        for text in texts:
            if text is not None:
                assert any(_safe(text) in line and line.startswith(" ") for line in lines), (
                    f"falta a linha recuada da prévia com {text!r}: {stdout!r}")


def _list_line(item: dict) -> str:
    return (f"{item['dispatch_id']}  {item['state']}  {_dash(item['opens_at'])}  {_dash(item['closes_at'])}  "
            f"{_safe(item['question']['qid'])}")


def _message_line(item: dict) -> str:
    return f"{item['created_at']}  {item['alias']}  {_safe(item['text'])}"


# --------------------------------------------------------------------------------------------------
# `dispatches prepare` (as duas linhas; o CLI não manda `origin`)


@pytest.mark.parametrize("by", ["code", "offering_id"])
def test_text_prepare_prints_two_lines_and_sends_only_the_given_options(
        by: str, cli_invoke: Any, client: Any, clock: Any, bot_api: Any, engine_fake: Any,
        repo_fetcher: Any) -> None:
    """`cq dispatches prepare REF --repo --commit --qid` →
    `Disparo preparado em <code>: <dispatch_id> (validando).` e `Consulte com: cq dispatches show <code>
    <dispatch_id>` (com o `code` mesmo quando o `REF` é o `offering_id`); o pedido vai a `POST
    /v1/offerings/{ref}/dispatches` com `X-CQ-Client: cli` e o corpo só com as opções dadas (sem
    `origin`, sem chaves nulas; os horários como digitados); o disparo sai `validating`, com `origin =
    cli`, imediato sem horários e agendado com os horários em UTC; o preparo não chama o motor nem a
    obtenção."""
    offering = _active_offering(client, bot_api)
    ref = offering[by]
    code = offering["code"]
    http = _Recorder(client)
    immediate = _prepare_cli(cli_invoke, ref, code, http=http)
    scheduled = _prepare_cli(cli_invoke, ref, code, "--opens-at", "2031-01-02T09:00:00-03:00",
                             "--closes-at", "2031-01-02T10:30:00-03:00", http=http)
    assert immediate != scheduled

    posts = [r for r in http.requests if r["method"] == "POST"]
    path = f"/v1/offerings/{ref}/dispatches"
    assert [(r["path"], r["json"]) for r in posts] == [
        (path, {"repo": REPO, "commit": COMMIT, "qid": QID}),
        (path, {"repo": REPO, "commit": COMMIT, "qid": QID, "opens_at": "2031-01-02T09:00:00-03:00",
                "closes_at": "2031-01-02T10:30:00-03:00"}),
    ]
    assert all(r["headers"].get("X-CQ-Client") == "cli" for r in http.requests)

    first = get_dispatch(client, ADMIN_TOKEN, code, immediate)
    second = get_dispatch(client, ADMIN_TOKEN, code, scheduled)
    for body, opens_at, closes_at in ((first, None, None),
                                      (second, "2031-01-02T12:00:00Z", "2031-01-02T13:30:00Z")):
        assert (body["state"], body["origin"], body["confirmed"], body["preview"], body["rejection"]) == (
            "validating", "cli", False, None, None)
        assert (body["opens_at"], body["closes_at"], body["created_at"]) == (opens_at, closes_at, iso(START))
        assert body["question"] == {"question_ref_id": body["question"]["question_ref_id"], "repo": REPO,
                                    "commit": COMMIT, "qid": QID}
        assert body["offering"] == {"offering_id": offering["offering_id"], "code": code}
    assert first["question"]["question_ref_id"] == second["question"]["question_ref_id"]
    assert engine_fake.calls() == []
    assert repo_fetcher.calls() == []


def test_json_prepare_prints_the_api_body(cli_invoke: Any, client: Any, bot_api: Any) -> None:
    """Contrato (`--json`: o corpo da API): `cq --json dispatches prepare`
    imprime o `Dispatch` em `validating`, igual ao da consulta logo depois; repetir o pedido cria outro
    disparo (não idempotente)."""
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    args = ["dispatches", "prepare", code, "--repo", REPO, "--commit", COMMIT, "--qid", QID,
            "--closes-at", "2031-01-01T02:00:00+00:00"]
    result = cli_invoke(args, json=True)
    _ok(result)
    body = json.loads(result.stdout)
    assert_matches("dispatches.prepare", 200, body)
    assert body == get_dispatch(client, ADMIN_TOKEN, code, body["dispatch_id"])
    assert (body["state"], body["origin"], body["opens_at"], body["closes_at"], body["timezone"]) == (
        "validating", "cli", None, "2031-01-01T02:00:00Z", TZ)
    again = cli_invoke(args, json=True)
    _ok(again)
    other = json.loads(again.stdout)
    assert other["dispatch_id"] != body["dispatch_id"]
    assert [item["dispatch_id"] for item in list_dispatches(client, ADMIN_TOKEN, code)["items"]] == [
        body["dispatch_id"], other["dispatch_id"]]


@pytest.mark.parametrize("missing", ["--repo", "--commit", "--qid"])
def test_prepare_without_a_required_option_is_usage_error(missing: str, cli_invoke: Any, client: Any,
                                                          bot_api: Any) -> None:
    """Forma das folhas (`--repo`, `--commit` e `--qid` obrigatórias) e uso incorreto:
    sem uma delas → código 2 e `Uso incorreto: falta a opção <opção>.`, sem chamar a API (nenhum
    disparo criado), com e sem `--json`."""
    offering = _active_offering(client, bot_api)
    options = {"--repo": REPO, "--commit": COMMIT, "--qid": QID}
    args = ["dispatches", "prepare", offering["code"]]
    for option, value in options.items():
        if option != missing:
            args += [option, value]
    for json_mode in (False, True):
        http = _Recorder(client)
        result = cli_invoke(args, json=json_mode, http=http)
        _failed(result, 2)
        assert result.stderr == _out(f"Uso incorreto: falta a opção {missing}.")
        assert http.requests == []
    assert list_dispatches(client, ADMIN_TOKEN, offering["code"])["items"] == []


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
@pytest.mark.parametrize("option,field", [("--opens-at", "opens_at"), ("--closes-at", "closes_at")])
def test_prepare_time_without_offset_is_refused(option: str, field: str, json_mode: bool, cli_invoke: Any,
                                                client: Any, bot_api: Any) -> None:
    """Horários do pedido: sem deslocamento é `invalid_format`, e
    o CLI não converte hora local: `--opens-at`/`--closes-at` sem fuso → código 1, `Erro:
    Pedido inválido.` e `  - <campo>: invalid_format`, sem eco do valor; nada é criado."""
    offering = _active_offering(client, bot_api)
    result = cli_invoke(["dispatches", "prepare", offering["code"], "--repo", REPO, "--commit", COMMIT,
                         "--qid", QID, option, "2031-01-02T10:17:00"], json=json_mode)
    _assert_error(result, json_mode, "invalid_request", text_lines=(f"  - {field}: invalid_format",),
                  fields=[{"field": field, "reason": "invalid_format"}])
    assert "10:17" not in result.stderr
    assert list_dispatches(client, ADMIN_TOKEN, offering["code"])["items"] == []


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_prepare_api_errors_keep_the_base_form(json_mode: bool, cli_invoke: Any, client: Any,
                                                   bot_api: Any) -> None:
    """Mensagens exatas e forma dos erros: oferecimento inexistente → `Erro: Recurso não
    encontrado.`; `REF` malformado → `  - ref: invalid_format` sem eco; rascunho → `invalid_state` com
    `Estado atual: draft`; token sem `offerings:write` → `Permissão necessária: offerings:write`; `repo`
    `http://` e com `?private_token=` → `  - repo: invalid_format`, sem eco da referência; `commit`
    abreviado → `  - commit: invalid_format`. Todos com código 1 e nada criado."""
    offering = _active_offering(client, bot_api)
    draft = create_offering(client, ADMIN_TOKEN)
    secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"])

    def prepare(ref: str, *, repo: str = REPO, commit: str = COMMIT, token: str = ADMIN_TOKEN) -> Any:
        return cli_invoke(["dispatches", "prepare", ref, "--repo", repo, "--commit", commit, "--qid", QID],
                          json=json_mode, token=token)

    _assert_error(prepare("codigo-inexistente"), json_mode, "not_found")
    malformed = prepare("REFSENTINELA")
    _assert_error(malformed, json_mode, "invalid_request", text_lines=("  - ref: invalid_format",),
                  fields=[{"field": "ref", "reason": "invalid_format"}])
    assert "refsentinela" not in malformed.stderr.casefold()
    _assert_error(prepare(draft["code"]), json_mode, "invalid_state", text_lines=("Estado atual: draft",),
                  state="draft")
    _assert_error(prepare(offering["code"], token=secret), json_mode, "forbidden",
                  text_lines=("Permissão necessária: offerings:write",), permission="offerings:write")
    for repo in ("http://repos.cq.test/disciplina/questoes.git",
                 "https://repos.cq.test/disciplina/questoes.git?private_token=tokensentinela123"):
        refused = prepare(offering["code"], repo=repo)
        _assert_error(refused, json_mode, "invalid_request", text_lines=("  - repo: invalid_format",),
                      fields=[{"field": "repo", "reason": "invalid_format"}])
        assert "tokensentinela" not in refused.stderr and "questoes.git" not in refused.stderr
    _assert_error(prepare(offering["code"], commit="c1d2c1d"), json_mode, "invalid_request",
                  text_lines=("  - commit: invalid_format",),
                  fields=[{"field": "commit", "reason": "invalid_format"}])
    assert list_dispatches(client, ADMIN_TOKEN, offering["code"])["items"] == []
    assert list_dispatches(client, ADMIN_TOKEN, draft["code"])["items"] == []


# --------------------------------------------------------------------------------------------------
# `dispatches show` (estado, confirmação, questão, horários, origem, motivo, contagens, prévia)


def test_text_show_validated_dispatch_with_times_origin_counts_and_preview(
        request: Any, cli_invoke: Any, client: Any, clock: Any, bot_api: Any, engine_fake: Any,
        repo_fetcher: Any) -> None:
    """`cq dispatches show REF DISPATCH_ID` de um
    agendado validado e ainda não confirmado mostra a linha do disparo com `scheduled`, `não
    confirmado`, `repo`/`commit`/`qid`, a abertura e o fechamento em UTC e em `America/Sao_Paulo`
    (`2031-01-02 09:00` e `10:30`), `origem: cli (declarada)`, as 9 contagens e a prévia recuada; o
    mesmo pelo `offering_id`; o disparo imediato validado (sem horários) sai sem as linhas de horário
    e com a sua prévia."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    scheduled = _prepare_cli(cli_invoke, code, code, "--opens-at", "2031-01-02T09:00:00-03:00",
                             "--closes-at", "2031-01-02T10:30:00-03:00")
    immediate = _prepare_cli(cli_invoke, code, code)
    _validate(request, clock, expected=2)

    body = get_dispatch(client, ADMIN_TOKEN, code, scheduled)
    assert (body["state"], body["confirmed"], body["opens_at"], body["closes_at"]) == (
        "scheduled", False, "2031-01-02T12:00:00Z", "2031-01-02T13:30:00Z")
    assert (_local(body["opens_at"], TZ), _local(body["closes_at"], TZ)) == ("2031-01-02 09:00",
                                                                             "2031-01-02 10:30")
    assert body["preview"] is not None and body["preview"]["fields"], "a prévia validada tem campos"
    for ref in (code, offering["offering_id"]):
        result = cli_invoke(["dispatches", "show", ref, scheduled])
        _ok(result)
        _assert_show(result.stdout, body)
        assert "2031-01-02 09:00" in result.stdout and "2031-01-02 10:30" in result.stdout

    other = get_dispatch(client, ADMIN_TOKEN, code, immediate)
    assert (other["state"], other["opens_at"], other["closes_at"]) == ("scheduled", None, None)
    shown = cli_invoke(["dispatches", "show", code, immediate])
    _ok(shown)
    _assert_show(shown.stdout, other)


def test_text_show_validating_and_rejected_dispatches(request: Any, cli_invoke: Any, client: Any, clock: Any,
                                                      bot_api: Any, engine_fake: Any,
                                                      repo_fetcher: Any) -> None:
    """um disparo em `validating` preparado pela API com
    `origin = integration` sai com `origem: integration (declarada)` e sem prévia; um rejeitado
    (`question_unsupported`) mostra o motivo: a mensagem fixa e os detalhes do motor pelo
    `_terminal_safe` (controle positivo: o detalhe chega cru na API, com ESC, BEL e quebra de linha)."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    rejected = _prepare_api(client, code, qid=UNSUPPORTED_QID)
    _validate(request, clock, expected=1)
    validating = _prepare_api(client, code, origin="integration")

    pending = get_dispatch(client, ADMIN_TOKEN, code, validating)
    assert (pending["state"], pending["origin"], pending["preview"]) == ("validating", "integration", None)
    result = cli_invoke(["dispatches", "show", code, validating])
    _ok(result)
    _assert_show(result.stdout, pending)
    assert "origem: integration (declarada)" in [line.strip() for line in result.stdout.splitlines()]

    body = get_dispatch(client, ADMIN_TOKEN, code, rejected)
    assert (body["state"], body["origin"], body["preview"]) == ("rejected", "api", None)
    assert (body["rejection"]["reason"], body["rejection"]["message"]) == (
        "question_unsupported", "A questão usa recursos que o sistema não suporta.")
    assert any("\x1b" in d and "\n" in d and "\x07" in d for d in body["rejection"]["details"]), (
        "controle positivo: o detalhe do motor chega cru na API")
    shown = cli_invoke(["dispatches", "show", code, rejected])
    _ok(shown)
    _assert_show(shown.stdout, body)
    assert _safe(CONTROL_REASON) in shown.stdout


def test_show_neutralizes_control_characters_in_third_party_text(
        request: Any, cli_invoke: Any, client: Any, clock: Any, bot_api: Any, engine_fake: Any,
        repo_fetcher: Any) -> None:
    """`_terminal_safe`: o título da questão e o aviso `console_output` com
    sequências ANSI, BEL, CSI de 8 bits e quebra de linha chegam crus na API (controle positivo) e saem
    no texto do CLI como escapes visíveis (`\\x1b`, `\\x07`, `\\x9b`, `\\n`), sem nenhum controle cru;
    com `--json`, a saída é o corpo da API (os controles escapados pelo JSON e devolvidos iguais)."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    dispatch_id = _prepare_cli(cli_invoke, code, code)
    _validate(request, clock, expected=1)

    body = get_dispatch(client, ADMIN_TOKEN, code, dispatch_id)
    preview = body["preview"]
    assert preview["title"] == CONTROL_TITLE
    assert CONTROL_WARNING in [w["message"] for w in preview["warnings"] if w["kind"] == "console_output"]
    result = cli_invoke(["dispatches", "show", code, dispatch_id])
    _ok(result)
    assert RAW_CONTROL.search(result.stdout) is None
    assert "Velocidade \\x1b[31mmédia\\x1b[0m\\x07" in result.stdout
    assert "saída do generate\\nlinha 2 \\x1b[2J\\x9b0m fim" in result.stdout
    _assert_show(result.stdout, body)

    as_json = cli_invoke(["dispatches", "show", code, dispatch_id], json=True)
    _ok(as_json)
    assert json.loads(as_json.stdout) == body
    assert "\x1b" not in as_json.stdout


def test_json_show_prints_the_api_body(request: Any, cli_invoke: Any, client: Any, clock: Any, bot_api: Any,
                                       engine_fake: Any, repo_fetcher: Any) -> None:
    """Contrato (`--json`: o corpo da API): `cq --json dispatches show`
    imprime o `Dispatch` completo (com `offering`, `timezone` e a prévia), igual ao da API, para um
    disparo em `validating` e depois de validado."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    dispatch_id = _prepare_cli(cli_invoke, code, code, "--opens-at", "2031-01-03T00:00:00Z")
    for step in ("validating", "scheduled"):
        if step == "scheduled":
            _validate(request, clock, expected=1)
        result = cli_invoke(["dispatches", "show", offering["offering_id"], dispatch_id], json=True)
        _ok(result)
        body = json.loads(result.stdout)
        assert_matches("dispatches.get", 200, body)
        assert body == get_dispatch(client, ADMIN_TOKEN, code, dispatch_id)
        assert (body["state"], body["timezone"], body["offering"]) == (
            step, TZ, {"offering_id": offering["offering_id"], "code": code})


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_show_errors(json_mode: bool, cli_invoke: Any, client: Any, bot_api: Any) -> None:
    """`dispatches.get`: 404 do oferecimento → 404 do disparo: disparo
    inexistente e disparo de outro oferecimento → `Erro: Recurso não encontrado.`; oferecimento
    inexistente → o mesmo; token sem `offerings:read` → `Permissão necessária: offerings:read`."""
    offering = _active_offering(client, bot_api)
    other = _active_offering(client, bot_api)
    foreign = _prepare_api(client, other["code"])
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"])
    unknown = str(uuid4_with_prefix("d15a"))
    for ref, dispatch_id in ((offering["code"], unknown), (offering["code"], foreign),
                             ("codigo-inexistente", foreign)):
        _assert_error(cli_invoke(["dispatches", "show", ref, dispatch_id], json=json_mode), json_mode,
                      "not_found")
    _assert_error(cli_invoke(["dispatches", "show", other["code"], foreign], json=json_mode, token=secret),
                  json_mode, "forbidden", text_lines=("Permissão necessária: offerings:read",),
                  permission="offerings:read")


# --------------------------------------------------------------------------------------------------
# `dispatches confirm`


def test_text_confirm_scheduled_and_immediate(request: Any, cli_invoke: Any, client: Any, clock: Any,
                                              sql: Any, bot_api: Any, engine_fake: Any,
                                              repo_fetcher: Any) -> None:
    """`cq dispatches confirm REF DISPATCH_ID` → `Disparo confirmado:
    <id>; abre em <opens_at>, fecha em <closes_at>; 2 alunos-alvo agora.` no agendado e `…; abre agora,
    fecha em <agora + janela do oferecimento>; 2 alunos-alvo agora.` no imediato (2 alunos com
    matrícula ativa e endereço vigente; `target_count` é o alvo agora, que pode mudar desde a prévia); o disparo fica
    confirmado com `confirmed_at` e, no imediato, `opens_at` e `closes_at`."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    _student(client, sql, clock, offering, "Aluna Alvo Confirmacao Um")
    _student(client, sql, clock, offering, "Aluno Alvo Confirmacao Dois")
    scheduled = _prepare_api(client, code, opens_at="2031-01-02T12:00:00Z", closes_at="2031-01-02T13:30:00Z")
    immediate = _prepare_api(client, code)
    _validate(request, clock, expected=2)
    clock.advance(60)
    confirmed_at = clock.now()

    result = cli_invoke(["dispatches", "confirm", code, scheduled])
    _ok(result)
    assert result.stdout == _out(f"Disparo confirmado: {scheduled}; abre em 2031-01-02T12:00:00Z, fecha em "
                                 f"2031-01-02T13:30:00Z; 2 alunos-alvo agora.")
    body = get_dispatch(client, ADMIN_TOKEN, code, scheduled)
    assert (body["state"], body["confirmed"], body["confirmed_at"], body["target_count"]) == (
        "scheduled", True, iso(confirmed_at), 2)

    now = iso(confirmed_at)
    closes_at = iso(confirmed_at + timedelta(minutes=WINDOW_MINUTES))
    result = cli_invoke(["dispatches", "confirm", offering["offering_id"], immediate])
    _ok(result)
    assert result.stdout == _out(f"Disparo confirmado: {immediate}; abre agora, fecha em {closes_at}; "
                                 f"2 alunos-alvo agora.")
    body = get_dispatch(client, ADMIN_TOKEN, code, immediate)
    assert (body["confirmed"], body["confirmed_at"], body["opens_at"], body["closes_at"]) == (
        True, now, now, closes_at)


def test_json_confirm_prints_the_api_body(request: Any, cli_invoke: Any, client: Any, clock: Any,
                                          bot_api: Any, engine_fake: Any, repo_fetcher: Any) -> None:
    """Contrato (`--json`: o corpo da API): `cq --json dispatches confirm`
    imprime o `Dispatch` confirmado (`confirmed = true`, `confirmed_at`, `target_count = 0` sem alunos),
    igual ao da consulta."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    dispatch_id = _prepare_api(client, code, opens_at="2031-01-02T00:00:00Z")
    _validate(request, clock, expected=1)
    clock.advance(30)
    result = cli_invoke(["dispatches", "confirm", code, dispatch_id], json=True)
    _ok(result)
    body = json.loads(result.stdout)
    assert_matches("dispatches.confirm", 200, body)
    assert body == get_dispatch(client, ADMIN_TOKEN, code, dispatch_id)
    assert (body["state"], body["confirmed"], body["confirmed_at"], body["target_count"]) == (
        "scheduled", True, iso(clock.now()), 0)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_confirm_refusals_have_fixed_messages(json_mode: bool, request: Any, cli_invoke: Any, client: Any,
                                              clock: Any, bot_api: Any, engine_fake: Any,
                                              repo_fetcher: Any) -> None:
    """Mensagens exatas: confirmar um disparo em `validating` e um já confirmado →
    `Erro: A operação não é permitida no estado atual do disparo.`; confirmar um imediato cujo fechamento
    informado ficou a menos de 5 min → `Erro: A janela do disparo não é mais válida; prepare um novo
    disparo.`; ambos sem `Estado atual`, sem `fields` (código 1; `--json`: o corpo exato da API); os
    disparos não mudam; disparo inexistente → `not_found`."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    confirmed = _prepare_api(client, code, opens_at="2031-01-02T00:00:00Z")
    short = _prepare_api(client, code, closes_at="2031-01-01T00:10:00Z")
    _validate(request, clock, expected=2)
    confirm_dispatch(client, ADMIN_TOKEN, code, confirmed)
    validating = _prepare_api(client, code)
    clock.advance(6 * 60)
    before = {d: get_dispatch(client, ADMIN_TOKEN, code, d) for d in (confirmed, short, validating)}

    for dispatch_id in (validating, confirmed):
        _assert_error(cli_invoke(["dispatches", "confirm", code, dispatch_id], json=json_mode), json_mode,
                      "invalid_dispatch_state")
    _assert_error(cli_invoke(["dispatches", "confirm", code, short], json=json_mode), json_mode,
                  "window_expired")
    _assert_error(cli_invoke(["dispatches", "confirm", code, str(uuid4_with_prefix("c0f1"))], json=json_mode),
                  json_mode, "not_found")
    assert {d: get_dispatch(client, ADMIN_TOKEN, code, d) for d in before} == before


# --------------------------------------------------------------------------------------------------
# `messages list`


def test_text_messages_list_one_line_per_message_with_terminal_safe_text(
        cli_invoke: Any, client: Any, clock: Any, sql: Any, db_url: str, bot_api: Any) -> None:
    """`cq messages list REF` → `<created_at>  <alias>
    <texto>` por mensagem, na ordem, e `Próximo cursor: <n>`; o texto redigido (o nome do aluno vira a
    marca do pseudônimo) e pelo `_terminal_safe` (controle positivo: a API devolve ESC, BEL, CSI e
    quebra de linha crus); `--limit 2` e `--after` paginam como a API; oferecimento sem mensagens → só
    `Próximo cursor: 0`."""
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    empty = _active_offering(client, bot_api)
    name = "Joana Mensagem Livre"
    person, endpoint, chat_id = _student(client, sql, clock, offering, name)
    texts = ["Não entendi a questão\x1b[31m vermelha\x07", "linha um\nlinha dois \x9b2J",
             f"Sou {name} e tenho uma dúvida."]
    moments = []
    for text in texts:
        clock.advance(60)
        moments.append(iso(clock.now()))
        _free_message(db_url, sql, clock, offering, endpoint, chat_id, text)

    api = list_free_messages(client, ADMIN_TOKEN, code)
    expected_texts = [texts[0], texts[1], f"Sou {marker(person['alias'])} e tenho uma dúvida."]
    assert [(i["pseudo_id"], i["alias"], i["text"], i["created_at"]) for i in api["items"]] == [
        (person["pseudo_id"], person["alias"], text, moment) for text, moment in zip(expected_texts, moments)]
    result = cli_invoke(["messages", "list", code])
    _ok(result)
    assert RAW_CONTROL.search(result.stdout) is None
    assert result.stdout == _out(*(_message_line(i) for i in api["items"]), f"Próximo cursor: {api['next_cursor']}")
    assert _out(f"{moments[0]}  {person['alias']}  Não entendi a questão\\x1b[31m vermelha\\x07",
                f"{moments[1]}  {person['alias']}  linha um\\nlinha dois \\x9b2J") in result.stdout
    assert name not in result.stdout

    first = list_free_messages(client, ADMIN_TOKEN, code, limit=2)
    paged = cli_invoke(["messages", "list", offering["offering_id"], "--limit", "2"])
    _ok(paged)
    assert paged.stdout == _out(*(_message_line(i) for i in first["items"]),
                                f"Próximo cursor: {first['next_cursor']}")
    rest = list_free_messages(client, ADMIN_TOKEN, code, after=first["next_cursor"])
    assert [i["created_at"] for i in first["items"] + rest["items"]] == moments
    after = cli_invoke(["messages", "list", code, "--after", str(first["next_cursor"])])
    _ok(after)
    assert after.stdout == _out(*(_message_line(i) for i in rest["items"]), f"Próximo cursor: {rest['next_cursor']}")

    none = cli_invoke(["messages", "list", empty["code"]])
    _ok(none)
    assert none.stdout == _out("Próximo cursor: 0")


def test_json_messages_list_prints_the_api_body(cli_invoke: Any, client: Any, clock: Any, sql: Any,
                                                db_url: str, bot_api: Any) -> None:
    """Contrato (`--json`: a API, com os controles escapados pelo JSON):
    `cq --json messages list REF [--limit]` imprime a página igual à da API (`offering`, `items` com
    `{pseudo_id, alias, text, created_at}` e `next_cursor`)."""
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    _, endpoint, chat_id = _student(client, sql, clock, offering, "Pedro Mensagem Json")
    for text in ("primeira\x1b[0m", "segunda"):
        clock.advance(30)
        _free_message(db_url, sql, clock, offering, endpoint, chat_id, text)
    for extra, kwargs in (([], {}), (["--limit", "1"], {"limit": 1})):
        result = cli_invoke(["messages", "list", code, *extra], json=True)
        _ok(result)
        body = json.loads(result.stdout)
        assert_matches("messages.list", 200, body)
        assert body == list_free_messages(client, ADMIN_TOKEN, code, **kwargs)
        assert body["offering"] == {"offering_id": offering["offering_id"], "code": code}
    assert [i["text"] for i in json.loads(cli_invoke(["messages", "list", code], json=True).stdout)["items"]] == [
        "primeira\x1b[0m", "segunda"]


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_messages_list_errors(json_mode: bool, cli_invoke: Any, client: Any, bot_api: Any) -> None:
    """`messages.list`: 404 do oferecimento, em qualquer estado; uso incorreto:
    oferecimento inexistente → `not_found`; `REF` malformado → `  - ref: invalid_format` sem eco;
    token sem `offerings:read` → `Permissão necessária: offerings:read`; num rascunho, a lista vazia."""
    offering = _active_offering(client, bot_api)
    draft = create_offering(client, ADMIN_TOKEN)
    secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"])
    _assert_error(cli_invoke(["messages", "list", "codigo-inexistente"], json=json_mode), json_mode, "not_found")
    malformed = cli_invoke(["messages", "list", "REFSENTINELA"], json=json_mode)
    _assert_error(malformed, json_mode, "invalid_request", text_lines=("  - ref: invalid_format",),
                  fields=[{"field": "ref", "reason": "invalid_format"}])
    assert "refsentinela" not in malformed.stderr.casefold()
    _assert_error(cli_invoke(["messages", "list", offering["code"]], json=json_mode, token=secret), json_mode,
                  "forbidden", text_lines=("Permissão necessária: offerings:read",), permission="offerings:read")
    listed = cli_invoke(["messages", "list", draft["code"]], json=json_mode)
    _ok(listed)
    if json_mode:
        assert json.loads(listed.stdout) == list_free_messages(client, ADMIN_TOKEN, draft["code"])
    else:
        assert listed.stdout == _out("Próximo cursor: 0")


# --------------------------------------------------------------------------------------------------
# `dispatches list` e `dispatches cancel`


def test_text_dispatches_list_one_line_per_dispatch_and_pagination(cli_invoke: Any, client: Any,
                                                                   bot_api: Any) -> None:
    """`cq dispatches list REF` → `<dispatch_id>  <state>  <opens_at>
    <closes_at>  <qid>` por disparo, em ordem de preparo (`-` nos horários nulos), e `Próximo cursor:
    <n>`; `--limit 2` e `--after` paginam como a API; oferecimento sem disparo → só `Próximo cursor:
    0`."""
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    empty = _active_offering(client, bot_api)
    scheduled = _prepare_api(client, code, opens_at="2031-01-02T12:00:00Z", closes_at="2031-01-02T13:30:00Z")
    immediate = _prepare_api(client, code)
    closing = _prepare_api(client, code, closes_at="2031-01-01T03:00:00Z")
    cancel_dispatch(client, ADMIN_TOKEN, code, immediate)

    api = list_dispatches(client, ADMIN_TOKEN, code)
    assert [_list_line(i) for i in api["items"]] == [
        f"{scheduled}  validating  2031-01-02T12:00:00Z  2031-01-02T13:30:00Z  {QID}",
        f"{immediate}  cancelled  -  -  {QID}",
        f"{closing}  validating  -  2031-01-01T03:00:00Z  {QID}",
    ]
    result = cli_invoke(["dispatches", "list", code])
    _ok(result)
    assert result.stdout == _out(*(_list_line(i) for i in api["items"]), f"Próximo cursor: {api['next_cursor']}")

    first = list_dispatches(client, ADMIN_TOKEN, code, limit=2)
    paged = cli_invoke(["dispatches", "list", offering["offering_id"], "--limit", "2"])
    _ok(paged)
    assert paged.stdout == _out(_list_line(first["items"][0]), _list_line(first["items"][1]),
                                f"Próximo cursor: {first['next_cursor']}")
    rest = list_dispatches(client, ADMIN_TOKEN, code, after=first["next_cursor"])
    assert [i["dispatch_id"] for i in rest["items"]] == [closing]
    after = cli_invoke(["dispatches", "list", code, "--after", str(first["next_cursor"])])
    _ok(after)
    assert after.stdout == _out(_list_line(rest["items"][0]), f"Próximo cursor: {rest['next_cursor']}")

    none = cli_invoke(["dispatches", "list", empty["code"]])
    _ok(none)
    assert none.stdout == _out("Próximo cursor: 0")


def test_json_dispatches_list_prints_the_api_body(cli_invoke: Any, client: Any, bot_api: Any) -> None:
    """`cq --json dispatches list REF [--limit]` imprime a
    página da API (`offering`, `timezone`, `items` de `DispatchSummary` sem prévia e `next_cursor`)."""
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    ids = [_prepare_api(client, code) for _ in range(3)]
    for extra, kwargs in (([], {}), (["--limit", "2"], {"limit": 2})):
        result = cli_invoke(["dispatches", "list", code, *extra], json=True)
        _ok(result)
        body = json.loads(result.stdout)
        assert_matches("dispatches.list", 200, body)
        assert body == list_dispatches(client, ADMIN_TOKEN, code, **kwargs)
        assert (body["offering"], body["timezone"]) == ({"offering_id": offering["offering_id"], "code": code}, TZ)
        assert all("preview" not in item for item in body["items"])
    assert [i["dispatch_id"] for i in json.loads(
        cli_invoke(["dispatches", "list", code], json=True).stdout)["items"]] == ids


def test_text_and_json_cancel(request: Any, cli_invoke: Any, client: Any, clock: Any, bot_api: Any,
                              engine_fake: Any, repo_fetcher: Any) -> None:
    """`cq dispatches cancel REF DISPATCH_ID` de um disparo em
    `validating` → `Disparo cancelado: <id>.`; de um agendado validado e confirmado, com `--json` → o
    `Dispatch` em `cancelled`, igual ao da consulta; os dois ficam `cancelled/operator` com
    `cancelled_at`."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    code = offering["code"]
    confirmed = _prepare_api(client, code, opens_at="2031-01-02T00:00:00Z")
    _validate(request, clock, expected=1)
    confirm_dispatch(client, ADMIN_TOKEN, code, confirmed)
    validating = _prepare_api(client, code)
    clock.advance(45)

    result = cli_invoke(["dispatches", "cancel", code, validating])
    _ok(result)
    assert result.stdout == _out(f"Disparo cancelado: {validating}.")
    body = get_dispatch(client, ADMIN_TOKEN, code, validating)
    assert (body["state"], body["cancel_reason"], body["cancelled_at"], body["confirmed"]) == (
        "cancelled", "operator", iso(clock.now()), False)

    clock.advance(15)
    as_json = cli_invoke(["dispatches", "cancel", offering["offering_id"], confirmed], json=True)
    _ok(as_json)
    cancelled = json.loads(as_json.stdout)
    assert_matches("dispatches.cancel", 200, cancelled)
    assert cancelled == get_dispatch(client, ADMIN_TOKEN, code, confirmed)
    assert (cancelled["state"], cancelled["cancel_reason"], cancelled["cancelled_at"], cancelled["confirmed"]) == (
        "cancelled", "operator", iso(clock.now()), True)


@pytest.mark.parametrize("json_mode", [False, True], ids=["texto", "json"])
def test_cancel_refusals_have_fixed_messages(json_mode: bool, request: Any, cli_invoke: Any, client: Any,
                                             clock: Any, bot_api: Any, engine_fake: Any,
                                             repo_fetcher: Any) -> None:
    """`dispatches.cancel`: 404 → 404 → `invalid_dispatch_state`:
    cancelar um já cancelado e um rejeitado → `Erro: A operação não é permitida no estado atual do
    disparo.` (sem `Estado atual`); disparo inexistente, de outro oferecimento e oferecimento
    inexistente → `not_found`; `dispatches list` de oferecimento inexistente → `not_found`; nada muda."""
    _program(engine_fake)
    offering = _active_offering(client, bot_api)
    other = _active_offering(client, bot_api)
    code = offering["code"]
    rejected = _prepare_api(client, code, qid=UNSUPPORTED_QID)
    _validate(request, clock, expected=1)
    cancelled = _prepare_api(client, code)
    cancel_dispatch(client, ADMIN_TOKEN, code, cancelled)
    foreign = _prepare_api(client, other["code"])
    before = {d: get_dispatch(client, ADMIN_TOKEN, code, d) for d in (rejected, cancelled)}
    assert [before[d]["state"] for d in (rejected, cancelled)] == ["rejected", "cancelled"]

    for dispatch_id in (cancelled, rejected):
        _assert_error(cli_invoke(["dispatches", "cancel", code, dispatch_id], json=json_mode), json_mode,
                      "invalid_dispatch_state")
    for ref, dispatch_id in ((code, str(uuid4_with_prefix("ca9c"))), (code, foreign),
                             ("codigo-inexistente", cancelled)):
        _assert_error(cli_invoke(["dispatches", "cancel", ref, dispatch_id], json=json_mode), json_mode,
                      "not_found")
    _assert_error(cli_invoke(["dispatches", "list", "codigo-inexistente"], json=json_mode), json_mode, "not_found")
    assert {d: get_dispatch(client, ADMIN_TOKEN, code, d) for d in before} == before
    assert get_dispatch(client, ADMIN_TOKEN, other["code"], foreign)["state"] == "validating"
