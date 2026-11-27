"""As duas ferramentas do banco de questões local pelo servidor MCP: `upload_question_bank` e `list_question_banks`.

O Claude Code é o dublê em processo (`tests/helpers/mcp_client.py`), nas duas eras (`MODES`). A API é a dos testes (o
`client` da API base) injetada como `ServerDeps.http`, observada pelos ganchos `event_hooks` do próprio cliente HTTP;
os casos que não devem chegar à API usam uma API dublê (`FakeApi`) que anota cada pedido e responde 500. O perfil é
sempre um arquivo do `tmp_path` por `CQ_CONFIG`.

O que se confere:
- a lista de ferramentas: as duas na lista, com o `inputSchema` igual ao arquivo de entrada de `tools.json`, as
  anotações de `tools.json` (o envio idempotente e não destrutivo; a listagem só leitura) e o resultado válido contra
  o `outputSchema` publicado;
- o envio: o servidor MCP lê e empacota a pasta no computador do operador e manda um único `POST`, com o nome, a
  versão de referência (`tests/helpers/banks.py`), a origem, o commit e o pacote (os arquivos dentro dele iguais aos
  da pasta); o resultado é exatamente a resposta da API, sem o caminho da pasta; o reenvio devolve a mesma versão
  com `created` falso;
- o `git_rev` opcional: com ele, o commit do git local (a pasta de trabalho suja fica fora); sem ele, a pasta como
  está;
- o caminho relativo, UNC e de dispositivo recusados como na importação da lista, sem pedido à API;
- toda recusa local só com a mensagem fixa do motivo (`[bank.read] <mensagem>`), sem o caminho da pasta e sem o
  caminho relativo do arquivo recusado, e sem pedido à API;
- o marcador único (`unique_marker`) dentro de um arquivo do banco e no **nome** de um arquivo recusado (um link
  com o marcador no nome) fora de todo resultado e de toda mensagem de erro;
- a listagem só lê: um `GET` sem corpo, com a consulta dos argumentos, e o resultado igual à mesma leitura na API.

Escolhas deste arquivo ([I]):
- o link recusado é uma junção no Windows (criada sem privilégio pelo `_winapi.CreateJunction`) e um link simbólico
  nos outros sistemas; o motivo esperado é `junction` ou `symlink`, conforme o caso;
- no Windows, o caminho UNC (`\\\\servidor\\...`) e o de dispositivo (`\\\\?\\...`, `\\\\.\\...`) são absolutos para o
  `pathlib`, e a recusa esperada é `unc_path`; nos outros sistemas eles são relativos, e a recusa é `not_absolute`
  (a ordem da importação da lista: o caminho absoluto primeiro);
- o corpo do envio aceita o `git_commit` ausente ou nulo na origem `folder` (os dois valem no contrato do pedido).

No esqueleto, as duas ferramentas lançam `NotImplementedError` no despacho (o erro `mcp_internal`): cada função falha
por ele.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import tarfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx2
import pytest
from jsonschema import Draft202012Validator

from cq_client.banks import LOCAL_MESSAGES
from cq_client.mcp_server import ERROR_TEXT_TEMPLATE, INVALID_PARAMS_CODE, PROTOCOL_MESSAGES

from tests.helpers import banks as bank_fixtures
from tests.helpers import git_fixture
from tests.helpers.api import ADMIN_TOKEN, all_events, auth
from tests.helpers.cli_runner import write_profile
from tests.helpers.contract import load_schema
from tests.helpers.mcp_client import MODES, Outcome, call, in_process, list_tools, make_deps, run, scan
from tests.helpers.secrets import all_secrets
from tests.helpers.stack_fake import TEST_API_URL

UPLOAD_TOOL = "upload_question_bank"
LIST_TOOL = "list_question_banks"
BANKS_PATH = "/v1/question-banks"
TOOLS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "mcp" / "v1"

# Nomes de banco neutros (fora dos alvos canário), no padrão do nome.
BANK_NAME = "banco-agente"
GIT_BANK_NAME = "banco-agente-git"
MARKED_BANK_NAME = "banco-agente-marcado"
# Um arquivo do banco que nenhuma questão cita, com o marcador dentro.
MARKED_FILE = "anotacoes/leia.txt"


# --------------------------------------------------------------------------------------------------
# Observação, perfil e asserções
# --------------------------------------------------------------------------------------------------


@dataclass
class Seen:
    """Os pedidos e as respostas observados pelos ganchos do cliente HTTP."""

    requests: list[httpx2.Request] = field(default_factory=list)
    responses: list[httpx2.Response] = field(default_factory=list)


@contextlib.contextmanager
def observed(http: Any) -> Iterator[Seen]:
    """Liga os ganchos de pedido e de resposta do cliente HTTP (o `TestClient` da API) enquanto o bloco roda."""
    seen = Seen()

    def on_request(request: httpx2.Request) -> None:
        seen.requests.append(request)

    def on_response(response: httpx2.Response) -> None:
        response.read()
        seen.responses.append(response)

    http.event_hooks["request"].append(on_request)
    http.event_hooks["response"].append(on_response)
    try:
        yield seen
    finally:
        http.event_hooks["request"].remove(on_request)
        http.event_hooks["response"].remove(on_response)


class FakeApi:
    """A API dublê: anota cada pedido e responde 500 `internal` (um pedido inesperado nunca vira sucesso)."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        body = {"error": {"code": "internal", "message": "Erro interno. Tente de novo mais tarde."}}
        Draft202012Validator(load_schema("error", "")).validate(body)
        self.http = httpx2.Client(transport=httpx2.MockTransport(
            lambda request: self.requests.append(request) or httpx2.Response(500, json=body)))


def profile_env(tmp_path: Path) -> dict[str, str]:
    """O `env` do `ServerDeps`: um arquivo de perfil do `tmp_path` com o endereço local de teste e o token inicial."""
    path = write_profile(tmp_path, f'api_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n')
    return {"CQ_CONFIG": str(path)}


def compact(body: Any) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def assert_success(outcome: Outcome, body: Any) -> None:
    """O corpo inteiro no `structuredContent` e o mesmo JSON, compacto, no único `TextContent`."""
    assert not outcome.is_protocol_error, (outcome.protocol_code, outcome.protocol_message)
    assert outcome.is_error is False, outcome.text
    assert outcome.structured == body
    assert [item["type"] for item in outcome.raw["content"]] == ["text"]
    assert outcome.text == compact(body)


def bank_read_text(reason: str) -> str:
    """O texto exato de uma recusa local: a chave `bank.read` e só a mensagem fixa do motivo."""
    return ERROR_TEXT_TEMPLATE.format(code="bank.read", message=LOCAL_MESSAGES[reason])


def assert_refused(outcome: Outcome, reason: str) -> None:
    """Erro de execução com o texto exato da recusa local e nenhum estruturado."""
    assert not outcome.is_protocol_error, (outcome.protocol_code, outcome.protocol_message)
    assert outcome.is_error is True, outcome.text
    assert outcome.structured is None
    assert [item["type"] for item in outcome.raw["content"]] == ["text"]
    assert outcome.text == bank_read_text(reason)


def dumped(outcome: Outcome) -> str:
    """Tudo o que o cliente MCP vê de uma chamada, num texto só (o bruto, o texto e a mensagem de protocolo)."""
    return (json.dumps(outcome.raw, ensure_ascii=False) + json.dumps(outcome.raw, ensure_ascii=True)
            + (outcome.text or "") + (outcome.protocol_message or ""))


def assert_no_echo(outcome: Outcome, *values: str) -> None:
    text = dumped(outcome)
    for value in values:
        assert value not in text, f"eco de {value!r}"


def path_forms(path: Path) -> tuple[str, ...]:
    """O caminho absoluto da pasta nas formas que um eco usaria (o texto, a forma POSIX e o JSON escapado)."""
    text = str(path)
    return (text, path.as_posix(), json.dumps(text)[1:-1])


def assert_clean(outcome: Outcome) -> None:
    """Nenhuma identidade canário nem segredo no texto, no estruturado e no bruto."""
    assert scan(outcome, all_secrets()) == []


def query_of(request: httpx2.Request) -> dict[str, list[str]]:
    query: dict[str, list[str]] = {}
    for key, value in request.url.params.multi_items():
        query.setdefault(key, []).append(value)
    return query


def api_list(client: Any, query: Mapping[str, Sequence[str]] | None = None) -> Any:
    """A mesma listagem, direto na API, com o token inicial."""
    params = [(key, value) for key, values in (query or {}).items() for value in values]
    resp = client.get(BANKS_PATH, params=params, headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"GET {BANKS_PATH}: {resp.status_code} {resp.text[:300]}"
    return resp.json()


def published(tools: list[dict]) -> dict[str, dict]:
    return {tool["name"]: tool for tool in tools}


def tools_entries() -> dict[str, dict]:
    """As entradas de `tools.json` (o arquivo congelado do contrato), pelo nome."""
    table = json.loads((TOOLS_DIR / "tools.json").read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in table["tools"]}


def input_schema_file(name: str) -> dict:
    entry = tools_entries()[name]
    return json.loads((TOOLS_DIR / entry["input_schema"]).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# Os bancos de teste
# --------------------------------------------------------------------------------------------------


def archive_files(encoded: Any) -> dict[str, bytes]:
    """Os arquivos de um pacote em base64 padrão: todo membro é arquivo regular; `{caminho: conteúdo}`."""
    assert isinstance(encoded, str), "o pacote não veio como texto base64"
    data = base64.b64decode(encoded, validate=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = tar.getmembers()
        assert all(member.isreg() for member in members), [m.name for m in members if not m.isreg()]
        out: dict[str, bytes] = {}
        for member in members:
            extracted = tar.extractfile(member)
            assert extracted is not None
            out[member.name] = extracted.read()
    return out


def assert_upload_body(request: httpx2.Request, *, name: str, files: Mapping[str, bytes], source: str,
                       git_commit: str | None) -> None:
    """O corpo do único `POST`: o nome, a versão de referência dos arquivos, a origem, o commit (ausente ou nulo na
    origem `folder`) e o pacote com exatamente esses arquivos."""
    body = json.loads(request.content)
    assert isinstance(body, dict)
    expected_keys = {"name", "version", "source", "archive"} | ({"git_commit"} if git_commit is not None else set())
    assert expected_keys <= set(body) <= expected_keys | {"git_commit"}, sorted(body)
    assert body["name"] == name
    assert body["version"] == bank_fixtures.reference_version(files)
    assert body["source"] == source
    assert body.get("git_commit") == git_commit
    assert archive_files(body["archive"]) == dict(files)


def expected_bank(name: str, files: Mapping[str, bytes], *, source: str, git_commit: str | None) -> dict:
    """Os campos do `BankVersion` que não dependem do horário."""
    return {"name": name, "version": bank_fixtures.reference_version(files), "repo": f"bank:{name}",
            "source": source, "git_commit": git_commit, "file_count": len(files),
            "byte_count": sum(len(data) for data in files.values()),
            "question_count": bank_fixtures.reference_question_count(files)}


def marked_bank(tmp_path: Path, marker: str, *, name: str = "banco-marcado") -> tuple[Path, dict[str, bytes]]:
    """O curso de fixture mais um arquivo que nenhuma questão cita, com o marcador dentro."""
    files = {**bank_fixtures.course_files(), MARKED_FILE: f"anotacao interna {marker}\n".encode("ascii")}
    folder = bank_fixtures.write_tree(tmp_path / name, files)
    return folder, dict(sorted(files.items(), key=lambda item: item[0].encode("utf-8")))


def add_link(folder: Path, name: str) -> str:
    """Um link de nome `name` na raiz da pasta, apontando para `questions`: uma junção no Windows (sem privilégio)
    e um link simbólico nos outros sistemas. Devolve o motivo local esperado."""
    link = folder / name
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(folder / "questions"), str(link))
        return "junction"
    os.symlink("questions", link)
    return "symlink"


# --------------------------------------------------------------------------------------------------
# A lista de ferramentas e o envio pela pasta
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_upload_lists_both_tools_and_returns_the_api_response(mode: str, client: Any, tmp_path: Path) -> None:
    """As duas ferramentas na lista, com o `inputSchema` igual ao arquivo de entrada de `tools.json` e as anotações
    de lá (o envio não é só leitura, não é destrutivo e é idempotente; a listagem é só leitura); o envio da pasta
    pelo caminho absoluto manda um único `POST /v1/question-banks` com `X-CQ-Client: mcp`, o token do perfil, nenhuma
    consulta e o corpo com a versão de referência e o pacote dos arquivos da pasta; o resultado é a resposta da
    API, válida contra o `outputSchema` publicado, sem o caminho da pasta; o reenvio devolve a mesma versão com
    `created` falso, e o evento `bank.uploaded` é um só."""
    folder = bank_fixtures.copy_course(tmp_path / "pasta-do-operador")
    files = bank_fixtures.course_files()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[dict[str, dict], list[Outcome], Seen]:
        async with in_process(deps, mode) as mc:
            tools = published(await list_tools(mc))
            with observed(client) as seen:
                outcomes = [await call(mc, UPLOAD_TOOL, {"path": str(folder), "name": BANK_NAME})
                            for _ in range(2)]
            return tools, outcomes, seen

    tools, (first, again), seen = run(scenario)

    entries = tools_entries()
    for name in (UPLOAD_TOOL, LIST_TOOL):
        assert name in tools, f"{name} fora da lista de ferramentas"
        assert tools[name]["inputSchema"] == input_schema_file(name), name
        hints = {key: tools[name]["annotations"][key] for key in entries[name]["annotations"]}
        assert hints == entries[name]["annotations"], name
    assert entries[UPLOAD_TOOL]["annotations"] == {"readOnlyHint": False, "destructiveHint": False,
                                                   "idempotentHint": True, "openWorldHint": False}
    assert entries[LIST_TOOL]["annotations"]["readOnlyHint"] is True
    assert input_schema_file(UPLOAD_TOOL)["required"] == ["path", "name"]

    assert [(r.method, r.url.path) for r in seen.requests] == [("POST", BANKS_PATH)] * 2
    for request in seen.requests:
        assert request.headers.get("X-CQ-Client") == "mcp"
        assert request.headers.get("Authorization") == f"Bearer {ADMIN_TOKEN}"
        assert query_of(request) == {}
        assert_upload_body(request, name=BANK_NAME, files=files, source="folder", git_commit=None)
    assert [r.status_code for r in seen.responses] == [200, 200]
    bodies = [r.json() for r in seen.responses]
    assert_success(first, bodies[0])
    assert_success(again, bodies[1])
    Draft202012Validator(tools[UPLOAD_TOOL]["outputSchema"]).validate(first.structured)
    expected = expected_bank(BANK_NAME, files, source="folder", git_commit=None)
    assert {k: v for k, v in first.structured.items() if k not in ("created_at", "created")} == expected
    assert (first.structured["created"], again.structured["created"]) == (True, False)
    assert again.structured["created_at"] == first.structured["created_at"]
    for outcome in (first, again):
        assert_no_echo(outcome, *path_forms(folder), folder.name)
        assert_clean(outcome)
    events = [e["data"] for e in all_events(client, ADMIN_TOKEN, "bank.uploaded")]
    assert events == [{"bank": expected["name"], **{k: expected[k] for k in ("version", "source", "git_commit",
                                                                              "file_count", "byte_count",
                                                                              "question_count")}}]


@pytest.mark.parametrize("mode", MODES)
def test_marker_inside_a_bank_file_never_leaves_the_result(mode: str, client: Any, tmp_path: Path) -> None:
    """O marcador único dentro de um arquivo do banco que nenhuma questão cita: o pacote enviado o leva (é o
    conteúdo do banco), mas nem o resultado da ferramenta, nem a resposta da API, nem a listagem pela ferramenta,
    nem o evento `bank.uploaded` o têm."""
    marker = bank_fixtures.unique_marker()
    folder, files = marked_bank(tmp_path, marker)
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[Outcome, Outcome, Seen]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                uploaded = await call(mc, UPLOAD_TOOL, {"path": str(folder), "name": MARKED_BANK_NAME})
                listed = await call(mc, LIST_TOOL, {"name": MARKED_BANK_NAME})
            return uploaded, listed, seen

    uploaded, listed, seen = run(scenario)
    assert [(r.method, r.url.path) for r in seen.requests] == [("POST", BANKS_PATH), ("GET", BANKS_PATH)]
    assert_upload_body(seen.requests[0], name=MARKED_BANK_NAME, files=files, source="folder", git_commit=None)
    assert marker.encode("ascii") in archive_files(json.loads(seen.requests[0].content)["archive"])[MARKED_FILE]
    assert_success(uploaded, seen.responses[0].json())
    assert_success(listed, seen.responses[1].json())
    for outcome in (uploaded, listed):
        assert_no_echo(outcome, marker, MARKED_FILE, *path_forms(folder))
        assert_clean(outcome)
    for response in seen.responses:
        assert marker not in response.text
    assert marker not in json.dumps(all_events(client, ADMIN_TOKEN, "bank.uploaded"), ensure_ascii=False)


# --------------------------------------------------------------------------------------------------
# O `git_rev` opcional
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_git_rev_is_optional_and_reads_the_commit(mode: str, client: Any, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Com o `git_rev`, o envio lê o commit da revisão no git local (a origem `git`, o commit inteiro e a versão do
    conteúdo do commit; um arquivo novo na pasta de trabalho suja fica fora); sem ele, a mesma pasta vai como está
    (a origem `folder`, sem commit, com o arquivo novo e sem a pasta `.git`). Os dois resultados são as respostas da
    API, sem o caminho. Um `git_rev` fora do padrão do esquema (começando por hífen) é erro de protocolo, sem pedido."""
    git_fixture.require_git()
    git_fixture.isolate_git_home(monkeypatch, tmp_path)
    files = bank_fixtures.course_files()
    repo = git_fixture.make_bank_repo(tmp_path, files, name="banco-git")
    commit = git_fixture.rev_parse(repo, "HEAD")
    dirty = {"anotacoes/rascunho.txt": b"fora do commit\n"}
    bank_fixtures.write_tree(repo, dirty)
    worktree = dict(sorted({**files, **dirty}.items(), key=lambda item: item[0].encode("utf-8")))
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)

    async def scenario() -> tuple[list[Outcome], Seen, Outcome]:
        async with in_process(deps, mode) as mc:
            with observed(client) as seen:
                outcomes = [await call(mc, UPLOAD_TOOL, {"path": str(repo), "name": GIT_BANK_NAME, "git_rev": "main"}),
                            await call(mc, UPLOAD_TOOL, {"path": str(repo), "name": GIT_BANK_NAME})]
            with observed(client) as refused_seen:
                refused = await call(mc, UPLOAD_TOOL, {"path": str(repo), "name": GIT_BANK_NAME, "git_rev": "-x"})
            assert refused_seen.requests == []
            return outcomes, seen, refused

    (from_git, from_folder), seen, refused = run(scenario)
    assert [(r.method, r.url.path) for r in seen.requests] == [("POST", BANKS_PATH)] * 2
    assert_upload_body(seen.requests[0], name=GIT_BANK_NAME, files=files, source="git", git_commit=commit)
    assert_upload_body(seen.requests[1], name=GIT_BANK_NAME, files=worktree, source="folder", git_commit=None)
    assert_success(from_git, seen.responses[0].json())
    assert_success(from_folder, seen.responses[1].json())
    assert {k: v for k, v in from_git.structured.items() if k not in ("created_at", "created")} == expected_bank(
        GIT_BANK_NAME, files, source="git", git_commit=commit)
    assert {k: v for k, v in from_folder.structured.items() if k not in ("created_at", "created")} == expected_bank(
        GIT_BANK_NAME, worktree, source="folder", git_commit=None)
    for outcome in (from_git, from_folder):
        assert_no_echo(outcome, *path_forms(repo))
        assert_clean(outcome)
    assert refused.is_protocol_error and refused.protocol_code == INVALID_PARAMS_CODE
    assert refused.protocol_message == PROTOCOL_MESSAGES["invalid_arguments"]
    assert_no_echo(refused, *path_forms(repo))


# --------------------------------------------------------------------------------------------------
# A forma do caminho e as recusas locais
# --------------------------------------------------------------------------------------------------


def _shape_cases(bank: Path) -> list[tuple[str, str]]:
    """Os caminhos fora da forma aceita e o motivo esperado (ver as escolhas do docstring do módulo)."""
    absolute_reason = "unc_path" if os.name == "nt" else "not_absolute"
    return [
        ("pasta-op", "not_absolute"),
        ("./pasta-op", "not_absolute"),
        ("C:pasta-op", "not_absolute"),
        ("\\\\servidor-teste\\compartilhado\\pasta-op", absolute_reason),
        ("\\\\?\\" + str(bank), absolute_reason),
        ("\\\\.\\" + str(bank), absolute_reason),
    ]


@pytest.mark.parametrize("mode", MODES)
def test_relative_unc_and_device_paths_are_refused_without_request(mode: str, tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """O caminho relativo (mesmo quando a pasta existe a partir do diretório atual), o caminho UNC e o de dispositivo
    (que, no Windows, apontam para uma pasta válida) são recusados como na importação da lista, só com a mensagem
    fixa do motivo, sem eco do caminho e sem pedido à API."""
    monkeypatch.chdir(tmp_path)
    bank = bank_fixtures.copy_course(tmp_path / "pasta-op")
    api = FakeApi()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)
    cases = _shape_cases(bank)

    async def scenario() -> list[Outcome]:
        async with in_process(deps, mode) as mc:
            return [await call(mc, UPLOAD_TOOL, {"path": path, "name": BANK_NAME}) for path, _ in cases]

    for (path, reason), outcome in zip(cases, run(scenario)):
        assert_refused(outcome, reason)
        assert_no_echo(outcome, path, *path_forms(bank))
        assert_clean(outcome)
    assert api.requests == []


def _not_a_course(root: Path, marker: str) -> Path:
    """Uma pasta sem `infoCourse.json` na raiz, com o marcador no conteúdo de um arquivo."""
    return bank_fixtures.write_tree(root, {"questions/q1/info.json": b"{}\n",
                                           MARKED_FILE: f"anotacao {marker}\n".encode("ascii")})


@pytest.mark.parametrize("mode", MODES)
def test_local_refusals_carry_only_the_fixed_message(mode: str, tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada recusa local sai só com a mensagem fixa do motivo, pela chave `bank.read`, sem pedido à API: a pasta
    inexistente (`not_found`), um arquivo no lugar da pasta (`not_a_directory`), a pasta sem `infoCourse.json`
    (`not_a_course`), um link com o marcador no nome (`junction` ou `symlink`), o `git_rev` numa pasta fora de
    repositório (`not_a_repository`), uma revisão inexistente (`revision_not_found`) e um commit com um link simbólico
    com o marcador no nome (`git_symlink`). Nenhuma mensagem tem o caminho da pasta, o caminho relativo do arquivo
    recusado nem o marcador (no nome ou no conteúdo)."""
    git_fixture.require_git()
    git_fixture.isolate_git_home(monkeypatch, tmp_path)
    marker = bank_fixtures.unique_marker()
    missing = tmp_path / "banco-inexistente"
    a_file = tmp_path / "um-arquivo.txt"
    a_file.write_bytes(b"nao e pasta\n")
    not_course = _not_a_course(tmp_path / "sem-curso", marker)
    linked, _ = marked_bank(tmp_path, marker, name="com-link")
    link_reason = add_link(linked, f"link-{marker}")
    plain = bank_fixtures.copy_course(tmp_path / "fora-de-repositorio")
    repo = git_fixture.make_bank_repo(tmp_path, bank_fixtures.course_files(), name="repositorio")
    linked_repo = git_fixture.make_bank_repo(tmp_path, bank_fixtures.course_files(), name="repositorio-link")
    git_fixture.add_symlink(linked_repo, f"questions/link-{marker}", "../infoCourse.json")
    cases: list[tuple[dict[str, Any], str, Path]] = [
        ({"path": str(missing)}, "not_found", missing),
        ({"path": str(a_file)}, "not_a_directory", a_file),
        ({"path": str(not_course)}, "not_a_course", not_course),
        ({"path": str(linked)}, link_reason, linked),
        ({"path": str(plain), "git_rev": "main"}, "not_a_repository", plain),
        ({"path": str(repo), "git_rev": "ramo-inexistente"}, "revision_not_found", repo),
        ({"path": str(linked_repo), "git_rev": "main"}, "git_symlink", linked_repo),
    ]
    api = FakeApi()
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=api.http)

    async def scenario() -> list[Outcome]:
        async with in_process(deps, mode) as mc:
            return [await call(mc, UPLOAD_TOOL, {**arguments, "name": BANK_NAME}) for arguments, _, _ in cases]

    for (arguments, reason, folder), outcome in zip(cases, run(scenario)):
        assert_refused(outcome, reason)
        assert_no_echo(outcome, marker, f"link-{marker}", MARKED_FILE, "questions/q1", *path_forms(folder),
                       folder.name)
        assert_clean(outcome)
    assert api.requests == []
    assert all(marker not in message for message in LOCAL_MESSAGES.values())


# --------------------------------------------------------------------------------------------------
# A listagem
# --------------------------------------------------------------------------------------------------


def stored_state(sql: Any) -> tuple[list[tuple], list[tuple]]:
    """O retrato do que a listagem não pode mudar: todas as linhas de `events` (de todo tipo) e de `question_banks`."""
    return (sql.execute("SELECT * FROM events ORDER BY seq").fetchall(),
            sql.execute("SELECT * FROM question_banks ORDER BY id").fetchall())


@pytest.mark.parametrize("mode", MODES)
def test_list_question_banks_only_reads(mode: str, client: Any, sql: Any, tmp_path: Path) -> None:
    """A listagem só lê: cada chamada é um `GET /v1/question-banks` sem corpo, com `X-CQ-Client: mcp` e a consulta
    exata dos argumentos dados (`name`, `after`, `limit`); o resultado é a mesma leitura na API, válido contra o
    `outputSchema` publicado; a listagem não muda os bancos nem grava evento (o retrato das linhas de `events`, de
    todo tipo, e de `question_banks` é tirado depois do envio e antes da primeira listagem, e comparado com o de
    depois da última)."""
    deps = make_deps(tmp_path, env=profile_env(tmp_path), http=client)
    folder = bank_fixtures.copy_course(tmp_path / "banco")

    async def scenario() -> tuple[dict[str, dict], Outcome, list[Outcome], Seen, Any, Any]:
        async with in_process(deps, mode) as mc:
            tools = published(await list_tools(mc))
            uploaded = await call(mc, UPLOAD_TOOL, {"path": str(folder), "name": BANK_NAME})
            before = stored_state(sql)
            with observed(client) as seen:
                outcomes = [await call(mc, LIST_TOOL, {}),
                            await call(mc, LIST_TOOL, {"name": BANK_NAME, "after": 0, "limit": 5}),
                            await call(mc, LIST_TOOL, {"name": "banco-sem-versao"})]
            after = stored_state(sql)
            return tools, uploaded, outcomes, seen, before, after

    tools, uploaded, outcomes, seen, before, after = run(scenario)
    assert uploaded.is_error is False, uploaded.text
    assert after == before
    assert len(before[1]) == 1
    assert [(r.method, r.url.path, r.content) for r in seen.requests] == [("GET", BANKS_PATH, b"")] * 3
    assert all(r.headers.get("X-CQ-Client") == "mcp" for r in seen.requests)
    assert query_of(seen.requests[1]) == {"name": [BANK_NAME], "after": ["0"], "limit": ["5"]}
    assert query_of(seen.requests[2]) == {"name": ["banco-sem-versao"]} or query_of(seen.requests[2]) == {
        "name": ["banco-sem-versao"], "after": ["0"]}
    for outcome, response in zip(outcomes, seen.responses):
        assert_success(outcome, response.json())
        Draft202012Validator(tools[LIST_TOOL]["outputSchema"]).validate(outcome.structured)
        assert_clean(outcome)
    assert outcomes[0].structured == api_list(client)
    assert outcomes[1].structured == api_list(client, {"name": [BANK_NAME], "after": ["0"], "limit": ["5"]})
    assert [item["name"] for item in outcomes[1].structured["items"]] == [BANK_NAME]
    assert outcomes[2].structured["items"] == []
    assert len(all_events(client, ADMIN_TOKEN, "bank.uploaded")) == 1
