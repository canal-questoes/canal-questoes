"""Canário de privacidade do banco de questões local.

Arquivo à parte do canário congelado (`tests/privacy/test_canary.py`); os detectores (`tests/helpers/canary.py`,
`tests/helpers/secrets.py`), a captura dos registros (`tests/helpers/logs.py`) e o banco sintético com os pacotes
montados à mão (`tests/helpers/banks.py`) são reusados. Todas as superfícies passam pelo mesmo cliente HTTP (o `client`
da API base): as chamadas diretas à API, o `cq` em processo (`cli_invoke`) e o servidor MCP em processo
(`ServerDeps.http`); toda resposta é gravada por um gancho do próprio cliente.

Três casos:
- **o nome do banco com forma de identidade**: o RA de uma pessoa canário e a parte local do e-mail fictício de uma
  pessoa cadastrada (montado em tempo de execução, num domínio reservado) como nomes de banco, enviados pela API, pelo
  `cq` e pelo servidor MCP, listados pelas três superfícies e usados num disparo com `bank:`. O nome e a referência
  (`bank:<nome>`) saem redigidos, com a marca da pessoa, no envio, na listagem, no evento `bank.uploaded`, no disparo
  e nas saídas do `cq` e do MCP; a parte local do e-mail só é alvo pela redação do texto do operador (a do `repo` do
  disparo, que também procura a parte local de cada e-mail);
- **o marcador único dentro de um arquivo do banco** que o motor não renderiza (nenhuma questão o cita; a prévia e a
  entrega de uma questão que o citasse seriam a exceção declarada da regra): ausente das respostas da API, das saídas
  do `cq`, dos resultados do MCP, dos eventos e dos registros, também nas recusas (a versão errada, um link no
  pacote, a pasta sem `infoCourse.json`); nenhuma rota devolve o pacote;
- **o marcador no nome de um arquivo recusado** (um link com o marcador no nome, na pasta e no pacote; um nome com
  caractere de controle no pacote): ausente da API, do MCP, dos eventos e dos registros; só a saída do `cq` cita o
  caminho relativo dentro do banco, por desenho (o controle positivo).

Escolhas deste arquivo ([I]):
- o link da pasta é uma junção no Windows (sem privilégio) e um link simbólico nos outros sistemas;
- a varredura das respostas, das saídas e dos registros começa depois do cadastro das pessoas (as respostas do
  cadastro não são do banco de questões); os eventos são lidos inteiros, de todos os tipos.

No esqueleto, a rota do envio responde 500 (`NotImplementedError`), e as duas folhas do `cq` e as duas ferramentas do
MCP lançam `NotImplementedError`: cada função falha pelo primeiro envio.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cq_client.banks import LOCAL_MESSAGES
from cq_client.mcp_server import ERROR_TEXT_TEMPLATE
from cq_server.events import EventType
from tests.helpers import banks as bank_fixtures
from tests.helpers import mcp_client
from tests.helpers.api import (ADMIN_TOKEN, activate_bot, all_events, auth, create_offering, get_dispatch,
                               list_dispatches, marker, prepare_dispatch, register_bot, register_person)
from tests.helpers.canary import CANARY_PEOPLE, ZEFIRO, find_canary
from tests.helpers.cli_runner import write_profile
from tests.helpers.contract import SCHEMAS, assert_error, assert_matches, load_catalog
from tests.helpers.logs import capture_logs
from tests.helpers.secrets import all_secrets
from tests.helpers.stack_fake import TEST_API_URL

ADMIN = auth(ADMIN_TOKEN)
BANKS_PATH = "/v1/question-banks"
EVENT_TYPES = [t.value for t in EventType]
SCAN_PREFIXES = mcp_client.SCAN_PREFIXES
QID = "number"  # uma questão do curso de fixture
# A pessoa dona do e-mail fictício cuja parte local vira nome de banco (nome neutro, domínio reservado).
HOLDER_NAME = "Pessoa Neutra Do Banco"
HOLDER_DOMAIN = "turma.example.org"
MARKED_FILE = "anotacoes/leia.txt"  # nenhuma questão cita este arquivo


# ---------------------------------------------------------------------------------------------
# Gravação e varredura
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Exchange:
    method: str
    path: str
    status: int
    headers: str
    body: str

    @property
    def text(self) -> str:
        """Os cabeçalhos e o corpo da resposta."""
        return self.headers + "\n" + self.body


def _record(http: Any, sink: list[_Exchange]) -> None:
    """Grava toda resposta que passa pelo cliente HTTP (a API, o `cq` e o MCP em processo)."""

    def hook(response: Any) -> None:
        response.read()
        headers = "\n".join(f"{k}: {v}" for k, v in response.headers.multi_items())
        sink.append(_Exchange(response.request.method, response.request.url.path, response.status_code, headers,
                              response.text))

    hooks = http.event_hooks
    http.event_hooks = {"request": list(hooks["request"]), "response": [*hooks["response"], hook]}


@dataclass(frozen=True)
class _Cli:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout + "\n" + self.stderr


def _cli(cli_invoke: Any, args: list[str], exit_code: int, sink: list[_Cli], *, json_mode: bool = False) -> _Cli:
    """Roda o `cq` em processo, confere o código de saída (sem exceção que não seja a saída) e grava a saída."""
    result = cli_invoke(list(args), json=json_mode)
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"{args}: o CLI levantou {type(result.exception).__name__}")
    assert result.exit_code == exit_code, f"{args}: código {result.exit_code} != {exit_code}; {result.stderr[:300]}"
    if exit_code != 0:
        assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
    run = _Cli(tuple(args), result.exit_code, result.stdout, result.stderr)
    sink.append(run)
    return run


def _mcp_deps(tmp_path: Path, client: Any) -> Any:
    """O servidor MCP em processo com a API dos testes e um perfil do `tmp_path` (o endereço local e o token)."""
    folder = tmp_path / "perfil-mcp"
    path = write_profile(folder, f'api_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
    return mcp_client.make_deps(folder, env={"CQ_CONFIG": str(path)}, http=client)


def _mcp_calls(deps: Any, calls: Iterable[tuple[str, dict]]) -> list[mcp_client.Outcome]:
    """As chamadas dadas, em ordem, numa conexão em processo (a primeira era)."""
    calls = list(calls)

    async def scenario() -> list[mcp_client.Outcome]:
        async with mcp_client.in_process(deps, mcp_client.MODES[0]) as mc:
            return [await mcp_client.call(mc, tool, arguments) for tool, arguments in calls]

    return mcp_client.run(scenario)


def _outcome_text(outcome: mcp_client.Outcome) -> str:
    return (json.dumps(outcome.raw, ensure_ascii=False) + json.dumps(outcome.raw, ensure_ascii=True)
            + (outcome.text or "") + (outcome.protocol_message or ""))


def _bank_read(reason: str) -> str:
    return ERROR_TEXT_TEMPLATE.format(code="bank.read", message=LOCAL_MESSAGES[reason])


def _upload(client: Any, name: str, files: dict[str, bytes], *, archive: bytes | None = None,
            version: str | None = None) -> Any:
    """`POST /v1/question-banks` com o pacote montado à mão (por padrão, o pacote válido de `files`)."""
    body = bank_fixtures.upload_body(name, version or bank_fixtures.reference_version(files), "folder", None,
                                     archive if archive is not None else bank_fixtures.archive_of(files))
    return client.post(BANKS_PATH, json=body, headers=ADMIN)


def _ok(resp: Any, op_id: str) -> dict:
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _dispatch_offering(client: Any, bot_api: Any, code: str) -> str:
    """Um oferecimento com bot ativado, pronto para preparar disparos (disciplina e período neutros)."""
    create_offering(client, ADMIN_TOKEN, code=code, course="Algoritmos e Programação", term="2031s1",
                    timezone="America/Sao_Paulo")
    bot_id = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, code, bot_api.token_of(bot_id))
    activate_bot(client, ADMIN_TOKEN, code)
    return code


def _sorted(files: dict[str, bytes]) -> dict[str, bytes]:
    return dict(sorted(files.items(), key=lambda item: item[0].encode("utf-8")))


def _add_link(folder: Path, name: str) -> None:
    """Um link de nome `name` na raiz da pasta, para `questions`: junção no Windows, link simbólico fora dele."""
    link = folder / name
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(folder / "questions"), str(link))
    else:
        os.symlink("questions", link)


def _keys(value: Any) -> set[str]:
    """Todas as chaves de objeto de um JSON (em qualquer nível)."""
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def _schema_properties(node: Any) -> set[str]:
    """Os nomes de propriedade declarados num esquema JSON (em qualquer nível)."""
    if isinstance(node, dict):
        own = set(node.get("properties", {})) if isinstance(node.get("properties"), dict) else set()
        return own | set().union(*(_schema_properties(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_schema_properties(v) for v in node)) if node else set()
    return set()


# ---------------------------------------------------------------------------------------------
# O nome do banco com forma de identidade
# ---------------------------------------------------------------------------------------------


def test_bank_name_with_identity_shape_is_redacted_everywhere(client: Any, bot_api: Any, cli_invoke: Any,
                                                               tmp_path: Path) -> None:
    """Dois nomes de banco com forma de identidade: o RA da pessoa canário e a parte local do e-mail fictício de uma
    pessoa cadastrada. Enviados pela API (a versão nova), pelo `cq` e pelo MCP (a mesma versão), listados pelas três
    superfícies e usados num disparo com `bank:` (pela API e pelo `cq`): o nome e a referência saem com a marca da
    pessoa no envio, na listagem, no evento `bank.uploaded` (um por nome), no disparo (o preparo, a consulta e a
    lista) e nas saídas do `cq` e do MCP. Nenhuma resposta, saída, evento ou registro tem o RA (nem outro alvo canário)
    ou a parte local do e-mail; os controles positivos provam que o detector acharia o RA na referência e que a parte
    local é mesmo a do e-mail cadastrado."""
    people = {p.name: register_person(client, ADMIN_TOKEN, **p.payload()) for p in CANARY_PEOPLE}
    local = f"conta-{secrets.token_hex(5)}"
    holder = register_person(client, ADMIN_TOKEN, name=HOLDER_NAME, email=f"{local}@{HOLDER_DOMAIN}")
    marks = {ZEFIRO.ra: marker(people[ZEFIRO.name]["alias"]), local: marker(holder["alias"])}
    code = _dispatch_offering(client, bot_api, "oferta-banco-privacidade")
    files = bank_fixtures.course_files()
    version = bank_fixtures.reference_version(files)
    folder = bank_fixtures.copy_course(tmp_path / "pasta-banco")
    # Controles positivos: o detector acha o RA na referência, e a parte local é a do e-mail.
    assert f"ra:{ZEFIRO.ra}" in find_canary(f"bank:{ZEFIRO.ra}")
    assert f"{local}@{HOLDER_DOMAIN}".split("@", 1)[0] == local and len(local) <= 40

    exchanges: list[_Exchange] = []
    runs: list[_Cli] = []
    _record(client, exchanges)
    with capture_logs() as captured:
        for name, mark in marks.items():
            body = _ok(_upload(client, name, files), "banks.upload")
            assert (body["name"], body["repo"], body["version"], body["created"]) == (mark, f"bank:{mark}", version,
                                                                                     True)
        listing = _ok(client.get(BANKS_PATH, headers=ADMIN), "banks.list")
        assert sorted((item["name"], item["repo"]) for item in listing["items"]) == sorted(
            (mark, f"bank:{mark}") for mark in marks.values())
        for name, mark in marks.items():
            filtered = _ok(client.get(BANKS_PATH, params={"name": name}, headers=ADMIN), "banks.list")
            assert [(item["name"], item["repo"]) for item in filtered["items"]] == [(mark, f"bank:{mark}")]

        prepared = {}
        for name, mark in marks.items():
            dispatch = prepare_dispatch(client, ADMIN_TOKEN, code, repo=f"bank:{name}", commit=version, qid=QID)
            question = dispatch["question"]
            assert (question["repo"], question["commit"], question["qid"]) == (f"bank:{mark}", version, QID)
            assert get_dispatch(client, ADMIN_TOKEN, code, dispatch["dispatch_id"])["question"]["repo"] == (
                f"bank:{mark}")
            prepared[dispatch["dispatch_id"]] = mark
        listed = list_dispatches(client, ADMIN_TOKEN, code)
        assert {item["dispatch_id"]: item["question"]["repo"] for item in listed["items"]
                if item["dispatch_id"] in prepared} == {
            dispatch_id: f"bank:{mark}" for dispatch_id, mark in prepared.items()}

        for name, mark in marks.items():
            text = _cli(cli_invoke, ["banks", "upload", str(folder), "--name", name], 0, runs)
            assert mark in text.stdout
            shown = json.loads(_cli(cli_invoke, ["banks", "upload", str(folder), "--name", name], 0, runs,
                                    json_mode=True).stdout)
            assert (shown["name"], shown["repo"], shown["created"]) == (mark, f"bank:{mark}", False)
            _cli(cli_invoke, ["banks", "list", "--name", name], 0, runs)
            _cli(cli_invoke, ["dispatches", "prepare", code, "--repo", f"bank:{name}", "--commit", version,
                              "--qid", QID], 0, runs)
        assert json.loads(_cli(cli_invoke, ["banks", "list"], 0, runs, json_mode=True).stdout) == _ok(
            client.get(BANKS_PATH, headers=ADMIN), "banks.list")

        deps = _mcp_deps(tmp_path, client)
        calls = [("upload_question_bank", {"path": str(folder), "name": name}) for name in marks]
        calls += [("list_question_banks", {"name": name}) for name in marks]
        calls += [("list_question_banks", {})]
        outcomes = _mcp_calls(deps, calls)
        for (name, mark), outcome in zip(marks.items(), outcomes[:2]):
            assert outcome.is_error is False, outcome.text
            assert (outcome.structured["name"], outcome.structured["repo"]) == (mark, f"bank:{mark}")
        for (name, mark), outcome in zip(marks.items(), outcomes[2:4]):
            assert outcome.is_error is False, outcome.text
            assert [(i["name"], i["repo"]) for i in outcome.structured["items"]] == [(mark, f"bank:{mark}")]
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    bank_events = [e for e in events if e["type"] == "bank.uploaded"]
    assert sorted(e["data"]["bank"] for e in bank_events) == sorted(marks.values())
    assert all(e["data"]["version"] == version for e in bank_events)

    forbidden = (ZEFIRO.ra, local)
    for ex in exchanges:
        assert find_canary(ex.text, prefixes=SCAN_PREFIXES) == [], (ex.method, ex.path, ex.status)
        assert not [value for value in forbidden if value in ex.text], (ex.method, ex.path, ex.status)
    for run in runs:
        assert find_canary(run.text, prefixes=SCAN_PREFIXES) == [], run.args
        assert not [value for value in forbidden if value in run.text], run.args
    for outcome in outcomes:
        assert mcp_client.scan(outcome, all_secrets()) == []
        assert not [value for value in forbidden if value in _outcome_text(outcome)]
    dumped_events = json.dumps(events, ensure_ascii=False)
    assert find_canary(dumped_events, prefixes=SCAN_PREFIXES) == []
    assert not [value for value in forbidden if value in dumped_events]
    for line in captured.lines:
        assert find_canary(line, prefixes=SCAN_PREFIXES) == [], line[:300]
        assert not [value for value in forbidden if value in line], line[:300]
    assert any(ex.path == BANKS_PATH for ex in exchanges), "contra o vazio: nenhuma resposta do banco gravada"
    assert captured.lines, "contra o vazio: nenhum registro capturado"


# ---------------------------------------------------------------------------------------------
# O marcador dentro de um arquivo do banco
# ---------------------------------------------------------------------------------------------


def test_marker_inside_an_unrendered_bank_file_never_leaves(client: Any, bot_api: Any, cli_invoke: Any,
                                                            tmp_path: Path) -> None:
    """O marcador único dentro de um arquivo do banco que nenhuma questão cita: o envio pela API, pelo `cq` e pelo
    MCP, as listagens, o disparo com `bank:` (o preparo, a consulta e a lista) e as recusas (pela API: a versão
    errada, um link no pacote e o pacote sem `infoCourse.json`; pelo `cq` e pelo MCP: a pasta sem `infoCourse.json`)
    não levam o marcador a nenhuma resposta da API, saída do `cq`, resultado do MCP, evento ou registro. Nenhuma
    rota devolve o pacote: nenhuma resposta tem a chave `archive` nem um trecho do pacote em base64, e nenhum esquema
    de resposta do catálogo declara a propriedade `archive`."""
    mark = bank_fixtures.unique_marker()
    files = _sorted({**bank_fixtures.course_files(), MARKED_FILE: f"anotacao interna {mark}\n".encode("ascii")})
    version = bank_fixtures.reference_version(files)
    archive = bank_fixtures.archive_of(files)
    encoded = base64.b64encode(archive).decode("ascii")
    folder = bank_fixtures.write_tree(tmp_path / "pasta-marcada", files)
    no_course = {path: data for path, data in files.items() if path != "infoCourse.json"}
    no_course_folder = bank_fixtures.write_tree(tmp_path / "pasta-sem-curso", no_course)
    code = _dispatch_offering(client, bot_api, "oferta-banco-marcador")

    exchanges: list[_Exchange] = []
    runs: list[_Cli] = []
    _record(client, exchanges)
    with capture_logs() as captured:
        body = _ok(_upload(client, "banco-marcado-api", files, archive=archive), "banks.upload")
        assert (body["version"], body["file_count"]) == (version, len(files))
        _ok(client.get(BANKS_PATH, headers=ADMIN), "banks.list")
        dispatch = prepare_dispatch(client, ADMIN_TOKEN, code, repo="bank:banco-marcado-api", commit=version, qid=QID)
        get_dispatch(client, ADMIN_TOKEN, code, dispatch["dispatch_id"])
        list_dispatches(client, ADMIN_TOKEN, code)
        # As recusas pela API, cada uma com o motivo exato e sem guardar nada.
        wrong_version = _upload(client, "banco-marcado-errado", files, archive=archive, version="0" * 64)
        assert_error(wrong_version, 400, "invalid_request", fields=[("version", "mismatch")])
        with_link = bank_fixtures.make_archive([*bank_fixtures.members_of(files), bank_fixtures.Member(
            f"link-{mark}", type=b"2", linkname=b"questions")])
        assert_error(_upload(client, "banco-marcado-link", files, archive=with_link), 400, "invalid_request",
                     fields=[("archive", "entry_not_allowed")])
        assert_error(_upload(client, "banco-marcado-sem-curso", no_course), 400, "invalid_request",
                     fields=[("archive", "not_a_course")])

        _cli(cli_invoke, ["banks", "upload", str(folder), "--name", "banco-marcado-cli"], 0, runs)
        _cli(cli_invoke, ["banks", "upload", str(folder), "--name", "banco-marcado-cli"], 0, runs, json_mode=True)
        _cli(cli_invoke, ["banks", "list"], 0, runs)
        _cli(cli_invoke, ["banks", "list"], 0, runs, json_mode=True)
        refused = _cli(cli_invoke, ["banks", "upload", str(no_course_folder), "--name", "banco-marcado-cli"], 2, runs)
        assert LOCAL_MESSAGES["not_a_course"] in refused.stderr

        outcomes = _mcp_calls(_mcp_deps(tmp_path, client), [
            ("upload_question_bank", {"path": str(folder), "name": "banco-marcado-mcp"}),
            ("list_question_banks", {}),
            ("upload_question_bank", {"path": str(no_course_folder), "name": "banco-marcado-mcp"}),
        ])
        assert outcomes[0].is_error is False and outcomes[1].is_error is False, (outcomes[0].text, outcomes[1].text)
        assert outcomes[0].structured["version"] == version
        assert (outcomes[2].is_error, outcomes[2].text) == (True, _bank_read("not_a_course"))
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    names = sorted(item["name"] for item in _ok(client.get(BANKS_PATH, headers=ADMIN), "banks.list")["items"])
    assert names == ["banco-marcado-api", "banco-marcado-cli", "banco-marcado-mcp"], names
    piece = encoded[len(encoded) // 2: len(encoded) // 2 + 64]
    for ex in exchanges:
        assert mark not in ex.text, (ex.method, ex.path, ex.status)
        assert piece not in ex.text, (ex.method, ex.path, ex.status)
        try:
            parsed = json.loads(ex.body)
        except ValueError:
            parsed = None
        assert "archive" not in _keys(parsed), (ex.method, ex.path)
    for run in runs:
        assert mark not in run.text, run.args
        assert MARKED_FILE not in run.text, run.args
    for outcome in outcomes:
        assert mark not in _outcome_text(outcome)
        assert MARKED_FILE not in _outcome_text(outcome)
        assert mcp_client.scan(outcome, all_secrets()) == []
    assert mark not in json.dumps(events, ensure_ascii=False)
    assert mark not in captured.text
    assert [e["data"]["bank"] for e in events if e["type"] == "bank.uploaded"] == [
        "banco-marcado-api", "banco-marcado-cli", "banco-marcado-mcp"]
    # Nenhuma rota devolve o pacote: nenhum esquema de resposta do catálogo declara a propriedade `archive`.
    for op in load_catalog()["operations"]:
        schema_path = SCHEMAS / f"{op['id']}.response.schema.json"
        if schema_path.exists():
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            assert "archive" not in _schema_properties(schema), op["id"]


# ---------------------------------------------------------------------------------------------
# O marcador no nome de um arquivo recusado
# ---------------------------------------------------------------------------------------------


def test_marker_in_a_refused_file_name_stays_out_of_api_mcp_events_and_logs(client: Any, cli_invoke: Any,
                                                                              tmp_path: Path) -> None:
    """O marcador no nome de um arquivo recusado: um link com o marcador no nome na pasta (pelo `cq` e pelo MCP) e no
    pacote montado à mão (pela API), e um nome com caractere de controle e o marcador no pacote (pela API). A API
    recusa com o motivo exato e sem o nome; o MCP recusa só com a mensagem fixa do motivo, sem pedido à API; nenhum
    evento nem registro tem o marcador. O `cq` cita o caminho relativo dentro do banco, por desenho (o controle
    positivo: o marcador sai na saída de erro dele, e só nela)."""
    mark = bank_fixtures.unique_marker()
    files = bank_fixtures.course_files()
    folder = bank_fixtures.copy_course(tmp_path / "pasta-com-link")
    _add_link(folder, f"link-{mark}")
    link_reason = "junction" if os.name == "nt" else "symlink"
    with_link = bank_fixtures.make_archive([*bank_fixtures.members_of(files), bank_fixtures.Member(
        f"link-{mark}", type=b"2", linkname=b"questions")])
    control_files = _sorted({**files, f"questions/{mark}\x01.txt": b"conteudo\n"})

    exchanges: list[_Exchange] = []
    runs: list[_Cli] = []
    _record(client, exchanges)
    with capture_logs() as captured:
        assert_error(_upload(client, "banco-nome-link", files, archive=with_link), 400, "invalid_request",
                     fields=[("archive", "entry_not_allowed")])
        assert_error(_upload(client, "banco-nome-controle", control_files), 400, "invalid_request",
                     fields=[("archive", "path_not_allowed")])
        before = len(exchanges)
        refused = _cli(cli_invoke, ["banks", "upload", str(folder), "--name", "banco-nome-cli"], 2, runs)
        assert LOCAL_MESSAGES[link_reason] in refused.stderr
        outcomes = _mcp_calls(_mcp_deps(tmp_path, client), [
            ("upload_question_bank", {"path": str(folder), "name": "banco-nome-mcp"})])
        assert [ex for ex in exchanges[before:] if ex.path == BANKS_PATH] == [], "recusa local com pedido à API"
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    # O controle positivo: só a saída de erro do `cq` cita o caminho relativo (com o marcador).
    assert mark in refused.stderr and mark not in refused.stdout
    assert str(folder) not in refused.text and folder.as_posix() not in refused.text
    assert (outcomes[0].is_error, outcomes[0].text) == (True, _bank_read(link_reason))
    assert mark not in _outcome_text(outcomes[0])
    for ex in exchanges:
        assert mark not in ex.text, (ex.method, ex.path, ex.status)
    assert mark not in json.dumps(events, ensure_ascii=False)
    assert [e for e in events if e["type"] == "bank.uploaded"] == []
    assert mark not in captured.text
    assert _ok(client.get(BANKS_PATH, headers=ADMIN), "banks.list")["items"] == []
