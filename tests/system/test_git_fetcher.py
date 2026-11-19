"""Teste de sistema do adaptador git real (os casos do adaptador:
gitlink → `FetchRefused("submodule")`, sem submódulos; prazo
esgotado → `FetchTemporaryFailure("timeout")`, pelos tetos padrão da obtenção; e o servidor git local
nos testes).

O pytest no host constrói a imagem do servidor (o `Dockerfile` da raiz, com a tag própria
`cq-test-fetch-<id>`), roda **um** container `cq-test-fetch-<id>` dela com `--network none` (o loopback
existe e nada sai), com o roteiro `tests/system/fetch_cases.py` montado como somente leitura, lê a linha
JSON do relatório e remove o container e a tag no `finally` (só o que ele mesmo criou; nunca `prune`). O
roteiro (ver o docstring dele) sobe o `git http-backend` atrás de HTTPS em 127.0.0.1 com certificado
gerado na hora, roda cada caso com o uid 999 do worker e confere a leitura pelo uid 65534 do motor.

Casos (um teste por grupo; cada um confere antes o ambiente da imagem):
- commit que não é ponta, inteiro, com `tree_oid` e contagens exatos, modos 0644/0755 com `umask 077`,
  legível pelo uid 65534; o reuso sem rede; commit SHA-256;
- commit e repositório inexistentes; 301 para outro host → `redirect` (sem pedido ao outro host); link
  absoluto e cadeia de links que sai da árvore; gitlink → `submodule` (nada do submódulo é buscado);
  `.gitattributes` (`eol=crlf`, `export-ignore`, `ident`) sem efeito; tetos de bytes e de arquivos;
- prazo (servidor que não responde) → `timeout`, sem processo `git` sobrando;
- duas chamadas simultâneas → as duas `Fetched` (uma publica, a outra acha pronta) e a árvore parcial
  nunca visível;
- o ambiente do processo `git` sem `CQ_*` nem o que o processo tinha, com os protocolos restritos e o
  proxy inalcançável (`https_proxy=http://127.0.0.1:9`, `no_proxy=repos.cq.test`, só com
  `test_options`);
- CA errada recusada; em produção (sem `test_options`), `repos.cq.test → 127.0.0.1` recusado com
  `address_not_global` sem nenhuma conexão ao servidor;
- configuração hostil nos arquivos padrão do git, `/etc/gitconfig` (sistema) e `~/.gitconfig` do HOME do
  processo (usuário), com `credential.helper`, `url.<outro>.insteadOf`, `core.hooksPath`,
  `core.fsmonitor` e `filter.lfs.*` que gravam marcador (a regra: sem ajudante de credenciais e sem as
  configurações de git do sistema e do usuário, sem submódulos, LFS nem hooks): a
  obtenção dá certo (ou, no caminho que pede credencial, falha sem credencial) sem executar nenhum deles;
  controle positivo: um `git` puro no mesmo ambiente honra a mesma configuração.

Sem Docker, ou sem conseguir construir a imagem, o teste **falha** com mensagem de ambiente (nunca
`skip`). No esqueleto, a imagem ainda não tem o `git` nem o `/repos` e o `ensure` lança
`NotImplementedError`: todos os casos falham pela asserção do ambiente da imagem.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.system

REPO = pathlib.Path(__file__).resolve().parents[2]
CASES_SCRIPT = pathlib.Path(__file__).resolve().parent / "fetch_cases.py"
BUILD_TIMEOUT = 1800
RUN_TIMEOUT = 900
WORKER_UID = 999
DEADLINE_TIMEOUT = 5.0  # `fetch_cases.DEADLINE_TIMEOUT`
HOST = "repos.cq.test"
OTHER_HOST = "outro.cq.test"
PROTECTED_PREFIX = "/protegido"  # `fetch_cases.PROTECTED_PREFIX`: 401 sem `Authorization`
# `fetch_cases.LFS_POINTER`: o blob `modelo.bin` do repositório `atributos` (com `filter=lfs`).
LFS_POINTER = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
               "size 12345\n")
UNREACHABLE_PROXY = "http://127.0.0.1:9"
SHA1 = re.compile(r"\A[0-9a-f]{40}\Z")
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")


def _docker(*args: str, timeout: float | None = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    return subprocess.run(["docker", *args], cwd=REPO, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _environment_error() -> str | None:
    try:
        info = _docker("info", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return (f"Docker indisponível para o teste de sistema ({type(exc).__name__}). Instale/ligue o Docker ou "
                "rode `pytest -m 'not system'`.")
    if info.returncode != 0:
        return "Docker indisponível para o teste de sistema (`docker info` falhou). Ligue o Docker."
    return None


@pytest.fixture(scope="module")
def fetch_report() -> dict[str, Any]:
    """Constrói a imagem, roda o roteiro num container `--network none` e devolve o relatório; os erros
    de ambiente voltam como `{"environment_error": …}` para cada teste falhar com a mensagem (nunca como
    erro de fixture). O container e a tag são removidos antes de devolver, e a remoção fica anotada."""
    problem = _environment_error()
    if problem:
        return {"environment_error": problem}
    ident = uuid.uuid4().hex[:12]
    name = f"cq-test-fetch-{ident}"
    tag = f"{name}:imagem"
    report: dict[str, Any] = {}
    try:
        build = _docker("build", "-t", tag, "-f", str(REPO / "Dockerfile"), str(REPO), timeout=BUILD_TIMEOUT)
        if build.returncode != 0:
            return {"environment_error": "não foi possível construir a imagem do servidor para o teste de sistema "
                                         f"(`docker build` saiu com {build.returncode}):\n{build.stderr[-2000:]}"}
        run = _docker("run", "--name", name, "--network", "none", "--user", "0:0",
                      "--mount", f"type=bind,source={CASES_SCRIPT},target=/cases/fetch_cases.py,readonly",
                      tag, "python", "/cases/fetch_cases.py", "main", timeout=RUN_TIMEOUT)
        lines = [line for line in run.stdout.splitlines() if line.strip()]
        try:
            report = json.loads(lines[-1])
        except (IndexError, ValueError):
            report = {"script_error": f"o roteiro saiu com {run.returncode} sem relatório JSON:\n"
                                      f"{run.stdout[-1500:]}\n{run.stderr[-2500:]}"}
        report["docker_run_exit"] = run.returncode
        report["script_stderr_tail"] = run.stderr[-3000:]
    finally:
        _docker("rm", "-f", name)
        _docker("image", "rm", tag)
        left = _docker("ps", "-a", "-q", "--filter", f"name=^/{name}$")
        report["container_left"] = left.stdout.strip()
        report["container_name"] = name
    return report


def _require_image(report: dict[str, Any]) -> None:
    """O ambiente de teste e a imagem: sem Docker ou sem imagem, falha com a mensagem de ambiente; a
    imagem do servidor tem de trazer o `git` do Debian, o `git-http-backend`, o `prlimit` e o `/repos`
    0755 999:999."""
    if "environment_error" in report:
        pytest.fail(report["environment_error"])
    assert "script_error" not in report, report.get("script_error")
    env = report["environment"]
    assert env is not None, report.get("setup_error")
    assert env["git"], "a imagem do servidor não tem o `git` (o `git` do Debian vai na etapa final do Dockerfile)"
    assert env["http_backend"], "a imagem do servidor não tem o `git-http-backend` (vem com o pacote `git`)"
    assert env["prlimit"], "a imagem do servidor não tem o `prlimit` (teto de bytes)"
    assert env["repos"] == {"is_dir": True, "uid": WORKER_UID, "gid": WORKER_UID, "mode": "0o755"}, (
        f"/repos da imagem: {env['repos']} (esperado: `install -d -o 999 -g 999 -m 0755 /repos`)")
    assert report["setup_error"] is None, report["setup_error"]


def _case(report: dict[str, Any], name: str) -> dict[str, Any]:
    _require_image(report)
    entry = report["cases"][name]
    assert "runner_error" not in entry["run"], entry["run"]
    return entry


def _fetched(expected: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
    if reused:
        return {"type": "Fetched", "tree_oid": None, "file_count": None, "byte_count": None, "reused": True}
    return {"type": "Fetched", "tree_oid": expected["tree_oid"], "file_count": expected["file_count"],
            "byte_count": expected["byte_count"], "reused": False}


def _assert_nothing_left(entry: dict[str, Any]) -> None:
    """Nenhuma árvore publicada e nenhuma sobra de staging (`rmtree` no `finally`)."""
    run = entry["run"]
    assert run["tree"] is None, f"árvore publicada num caso recusado: {entry['final_path']}"
    assert run["key_dir"] in (None, []), run["key_dir"]
    assert run["staging"] in (None, []), f"sobra de staging: {run['staging']}"


def _assert_published(entry: dict[str, Any]) -> None:
    run = entry["run"]
    assert run["tree"] == entry["expected"]["tree"]
    assert run["key_dir"] == [entry["config"]["commit"]]
    assert run["staging"] in (None, []), f"sobra de staging: {run['staging']}"


def _hosts(entry: dict[str, Any]) -> set[str]:
    return _request_hosts(entry["requests"])


def _request_hosts(requests: list[dict[str, Any]]) -> set[str]:
    return {str(r["host"]).split(":")[0] for r in requests}


def _hostile_control(report: dict[str, Any]) -> dict[str, Any]:
    """O controle positivo da configuração hostil: a plantação rodou sem erro, e o `git` puro no mesmo
    HOME e com o mesmo `/etc/gitconfig` rodou (o relatório traz os passos, os pedidos e os marcadores)."""
    _require_image(report)
    hostile = report["hostile"]
    assert hostile["error"] is None, hostile["error"]
    control = hostile["control"]
    assert control is not None, "o controle positivo da configuração hostil não rodou"
    return control


# ---------------------------------------------------------------------------------------------
# Imagem e container
# ---------------------------------------------------------------------------------------------


def test_server_image_has_git_and_repos_dir_and_container_is_removed(fetch_report: dict[str, Any]) -> None:
    """A imagem do servidor com o `git` do Debian, o `git-http-backend`,
    o `prlimit` e `/repos` 0755 999:999, e o container `cq-test-fetch-<id>` com
    `--network none`, removido no `finally`: o roteiro rodou como root, montou tudo sem erro, e o
    container não existe mais."""
    _require_image(fetch_report)
    assert fetch_report["environment"]["uid"] == 0
    assert fetch_report["docker_run_exit"] == 0, fetch_report["script_stderr_tail"]
    assert fetch_report["container_left"] == "", f"o container {fetch_report['container_name']} sobrou"
    assert set(fetch_report["cases"]) == {
        "not_tip", "reuse", "sha256", "missing_commit", "missing_repository", "redirect", "absolute_symlink",
        "escaping_symlink_chain", "gitlink", "gitattributes", "max_bytes", "max_files", "deadline", "concurrent",
        "environment", "wrong_ca", "production", "hostile_config", "hostile_config_auth"}


# ---------------------------------------------------------------------------------------------
# Obtenção bem-sucedida
# ---------------------------------------------------------------------------------------------


def test_commit_that_is_not_the_tip_is_published_whole_with_modes_readable_by_the_engine(
        fetch_report: dict[str, Any]) -> None:
    """A árvore aparece inteira em `<repos>/<repo_key>/<commit>/`, com a busca por
    SHA de um commit que não é ponta, pelo protocolo v2, os bytes de cada blob, sem checkout (com
    as contagens das entradas de arquivo e link), e os modos explícitos 0644/0755 (sem depender do
    `umask`, que o caso põe em 077; o motor lê como uid 65534): `Fetched(tree_oid, file_count,
    byte_count, reused=False)` exatos, a árvore igual à do git (com `scripts/run.sh` 0755), só o commit
    sob a chave, nenhuma sobra de staging, pedidos só a `repos.cq.test`, e o uid 65534 lê cada arquivo
    com o mesmo conteúdo."""
    entry = _case(fetch_report, "not_tip")
    expected = entry["expected"]
    assert SHA1.match(entry["config"]["commit"])
    assert entry["run"]["outcomes"] == [_fetched(expected)]
    assert expected["tree"]["scripts/run.sh"]["mode"] == "0o755"
    assert expected["tree"]["README.md"]["mode"] == "0o644"
    _assert_published(entry)
    assert entry["requests"], "nenhum pedido chegou ao servidor"
    assert _hosts(entry) == {HOST}
    read = entry["engine_read"]
    assert read["errors"] == []
    assert read["files"] == {path: item["sha256"] for path, item in expected["tree"].items()
                             if item["type"] == "file"}


def test_existing_tree_is_reused_without_network(fetch_report: dict[str, Any]) -> None:
    """A árvore que já existe → `Fetched(reused=True)` sem rede, com as
    contagens nulas (é a idempotência e a repetição da obtenção): a segunda chamada na mesma raiz não
    faz nenhum pedido ao servidor e deixa a árvore como estava."""
    entry = _case(fetch_report, "reuse")
    assert entry["run"]["outcomes"] == [_fetched(entry["expected"], reused=True)]
    assert entry["requests"] == []
    _assert_published(entry)


def test_sha256_commit_is_fetched(fetch_report: dict[str, Any]) -> None:
    """Commit de 40 ou 64 hexadecimais (o repositório descartável no
    formato escolhido pelo tamanho do hash): um repositório SHA-256 é obtido inteiro, com o `tree_oid` de
    64 hexadecimais."""
    entry = _case(fetch_report, "sha256")
    assert SHA256.match(entry["config"]["commit"])
    assert SHA256.match(entry["expected"]["tree_oid"])
    assert entry["run"]["outcomes"] == [_fetched(entry["expected"])]
    _assert_published(entry)


def test_gitattributes_have_no_effect_on_the_published_bytes(fetch_report: dict[str, Any]) -> None:
    """O conteúdo gravado é exatamente o dos blobs (sem `text`/`eol`, `ident` nem
    `export-ignore`), sem LFS (nenhum *checkout* nem filtro): com `* text eol=crlf`,
    `ignorado.txt export-ignore`, `*.txt ident` e `*.bin filter=lfs`, a árvore publicada tem os bytes
    dos blobs (fim de linha LF, `$Id$` sem expansão, o arquivo "ignorado" presente e o `modelo.bin` com
    o próprio ponteiro do LFS)."""
    entry = _case(fetch_report, "gitattributes")
    tree = entry["expected"]["tree"]
    assert {"linhas.txt", "ignorado.txt", ".gitattributes", "modelo.bin"} <= set(tree)
    assert tree["modelo.bin"]["sha256"] == hashlib.sha256(LFS_POINTER.encode("ascii")).hexdigest()
    assert entry["run"]["outcomes"] == [_fetched(entry["expected"])]
    _assert_published(entry)


# ---------------------------------------------------------------------------------------------
# Inexistentes e recusas da política
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "outcome"), [
    pytest.param("missing_commit", {"type": "FetchNotFound", "what": "commit"}, id="commit-inexistente"),
    pytest.param("missing_repository", {"type": "FetchNotFound", "what": "repository"},
                 id="repositorio-inexistente"),
])
def test_missing_commit_and_repository_are_not_found(fetch_report: dict[str, Any], name: str,
                                                     outcome: dict[str, str]) -> None:
    """A classificação dos erros (`not our ref` → commit inexistente; `repository … not found` → repositório
    inacessível, nas classes do domínio): o desfecho exato, sem árvore nem staging."""
    entry = _case(fetch_report, name)
    assert entry["run"]["outcomes"] == [outcome]
    _assert_nothing_left(entry)
    assert entry["requests"], "nenhum pedido chegou ao servidor"


def test_redirect_to_another_host_is_refused_without_following(fetch_report: dict[str, Any]) -> None:
    """Sem redirecionamento (`http.followRedirects=false`; o 301 →
    `FetchRefused("redirect")`): o servidor responde 301 para `outro.cq.test`, e nenhum pedido chega a
    ele (controle positivo: o pedido redirecionado chegou)."""
    entry = _case(fetch_report, "redirect")
    assert entry["run"]["outcomes"] == [{"type": "FetchRefused", "rule": "redirect"}]
    _assert_nothing_left(entry)
    assert any(r["path"].startswith("/redirecionar/") for r in entry["requests"]), entry["requests"]
    assert OTHER_HOST not in _hosts(entry)


@pytest.mark.parametrize(("name", "rule"), [
    pytest.param("absolute_symlink", "symlink_absolute", id="link-absoluto"),
    pytest.param("escaping_symlink_chain", "symlink_escape", id="cadeia-de-links"),
    pytest.param("gitlink", "submodule", id="gitlink"),
    pytest.param("max_bytes", "max_bytes", id="teto-de-bytes"),
    pytest.param("max_files", "max_files", id="teto-de-arquivos"),
])
def test_policy_violations_are_refused_without_publishing(fetch_report: dict[str, Any], name: str,
                                                          rule: str) -> None:
    """Link simbólico que sai da árvore, submódulo e tetos rejeitam
    (link absoluto; a cadeia `a/l1 -> ..` + `l2 -> a/l1/..`, interna à letra, sai da árvore pelo
    `realpath`; teto de 30 arquivos com `max_files=10`; blob de 3 MB com `max_bytes=1 000 000`) e
    o gitlink recusa o repositório com `submodule`: o desfecho `FetchRefused(<regra>)`
    exato, sem árvore nem staging; o gitlink não leva a nenhum pedido fora do próprio repositório."""
    entry = _case(fetch_report, name)
    assert entry["run"]["outcomes"] == [{"type": "FetchRefused", "rule": rule}]
    _assert_nothing_left(entry)
    if name == "gitlink":
        assert entry["requests"], "nenhum pedido chegou ao servidor"
        assert all(r["path"].startswith("/gitlink.git/") for r in entry["requests"]), entry["requests"]


def test_deadline_is_a_temporary_timeout_that_kills_the_whole_git_process_group(
        fetch_report: dict[str, Any]) -> None:
    """Prazo total `CQ_REPO_FETCH_TIMEOUT_SECONDS` (o grupo de processos inteiro morto
    no fim do prazo; prazo → `FetchTemporaryFailure("timeout")`; o núcleo
    rejeita como `repository_refused` na última tentativa): contra um servidor que nunca responde, com
    prazo de 5 s, o desfecho é `timeout` entre 4,5 e 15 s, sem árvore nem staging e sem nenhum processo
    `git` do uid 999 sobrando."""
    entry = _case(fetch_report, "deadline")
    run = entry["run"]
    assert run["outcomes"] == [{"type": "FetchTemporaryFailure", "error_type": "timeout"}]
    assert DEADLINE_TIMEOUT * 0.9 <= run["elapsed"] <= DEADLINE_TIMEOUT + 10, run["elapsed"]
    _assert_nothing_left(entry)
    assert any(r["path"].startswith("/lento/") for r in entry["requests"]), entry["requests"]
    assert entry["leftover_git_processes"] == []


def test_concurrent_calls_both_succeed_and_partial_tree_is_never_visible(fetch_report: dict[str, Any]) -> None:
    """A árvore aparece inteira ou não aparece (o `rename` atômico: numa
    corrida, um vence e o outro acha a árvore pronta; as duas chamadas dão `Fetched`): duas chamadas
    simultâneas no mesmo repositório de 300 arquivos → uma publica (`reused=False`, contagens exatas) e a
    outra `Fetched(reused=True)`; um vigia que olhou a árvore final a cada milissegundo nunca a viu
    parcial nem viu outro nome sob a chave; nenhuma sobra de staging."""
    entry = _case(fetch_report, "concurrent")
    run = entry["run"]
    outcomes = run["outcomes"]
    assert [o["type"] for o in outcomes] == ["Fetched", "Fetched"], outcomes
    assert sorted(o["reused"] for o in outcomes) == [False, True]
    [winner] = [o for o in outcomes if not o["reused"]]
    assert winner == _fetched(entry["expected"])
    _assert_published(entry)
    assert run["watch"]["observations"] > 0
    assert run["watch"]["partial"] == []
    assert run["watch"]["foreign_names"] == []


# ---------------------------------------------------------------------------------------------
# Ambiente do processo git, TLS e produção
# ---------------------------------------------------------------------------------------------


def test_git_process_environment_is_minimal_with_restricted_protocols_and_dead_proxy(
        fetch_report: dict[str, Any]) -> None:
    """O ambiente do `git` é construído do zero (sem `CQ_*`,
    `GIT_SSL_NO_VERIFY`, `GIT_TRACE*`, `GIT_CURL_VERBOSE`, `SSLKEYLOGFILE` nem o proxy do processo; só
    `https`; sem prompt; nos testes, o processo git roda com os protocolos
    restritos e um proxy inalcançável, `https_proxy=http://127.0.0.1:9` e `no_proxy=repos.cq.test`, só
    com `test_options`): com um `git_binary` que grava o próprio ambiente e chama o git real, a obtenção
    dá certo, e nenhuma chamada do git leva as variáveis (com a marca `sentinela`) que o processo do caso
    tinha."""
    entry = _case(fetch_report, "environment")
    assert entry["run"]["outcomes"] == [_fetched(entry["expected"])]
    _assert_published(entry)
    calls = entry["git_calls"]
    assert calls, "o git_binary configurado não foi chamado"
    fetches = [call for call in calls if "fetch" in call["args"]]
    assert len(fetches) == 1, [call["args"][:3] for call in calls]
    for call in calls:
        env = call["env"]
        assert [key for key in env if key.upper().startswith("CQ_")] == []
        assert [key for key in env if key in ("GIT_SSL_NO_VERIFY", "GIT_TRACE", "GIT_CURL_VERBOSE",
                                              "SSLKEYLOGFILE")] == []
        assert [key for key, value in env.items() if "sentinela" in value] == []
    env = fetches[0]["env"]
    assert env.get("GIT_ALLOW_PROTOCOL") == "https"
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    proxies = {key: value for key, value in env.items() if key.lower() == "https_proxy"}
    assert proxies and set(proxies.values()) == {UNREACHABLE_PROXY}, proxies
    no_proxy = {key: value for key, value in env.items() if key.lower() == "no_proxy"}
    assert no_proxy and set(no_proxy.values()) == {HOST}, no_proxy


def test_wrong_ca_is_refused_by_tls_verification(fetch_report: dict[str, Any]) -> None:
    """TLS verificado (`SSL certificate problem` → repositório
    inacessível): com uma CA que não assinou o certificado do servidor, o desfecho é
    `FetchNotFound("repository")`, nenhum pedido HTTP chega ao servidor, sem árvore nem staging."""
    entry = _case(fetch_report, "wrong_ca")
    assert entry["run"]["outcomes"] == [{"type": "FetchNotFound", "what": "repository"}]
    assert entry["requests"] == []
    _assert_nothing_left(entry)


def test_production_refuses_loopback_without_any_connection(fetch_report: dict[str, Any]) -> None:
    """A configuração de produção recusa o loopback, com teste que mostra a recusa:
    `GitRepoFetcher(settings)` **sem** `test_options`, com `repos.cq.test`
    resolvido para 127.0.0.1, → `FetchRefused("address_not_global")`, e o servidor de teste não recebe
    nenhum pedido; sem árvore nem staging."""
    entry = _case(fetch_report, "production")
    assert entry["config"]["production"] is True
    assert entry["run"]["outcomes"] == [{"type": "FetchRefused", "rule": "address_not_global"}]
    assert entry["requests"] == []
    _assert_nothing_left(entry)


# ---------------------------------------------------------------------------------------------
# Configuração hostil do sistema e do usuário (nenhum segredo exposto)
# ---------------------------------------------------------------------------------------------


def test_hostile_system_and_user_git_config_is_not_honored(fetch_report: dict[str, Any]) -> None:
    """A regra (sem as configurações de git do sistema e do usuário, sem submódulos, LFS nem hooks,
    cada regra com teste de controle positivo) e a construção do ambiente (`GIT_CONFIG_NOSYSTEM`,
    `GIT_CONFIG_SYSTEM`/`GIT_CONFIG_GLOBAL=/dev/null`, `core.hooksPath=/dev/null`,
    `core.fsmonitor=false`, `--template=`, nenhum filtro): com o `/etc/gitconfig` e o `~/.gitconfig` do
    HOME do processo `run` plantados com `url.https://outro.cq.test:<porta>/.insteadOf` para o host do
    teste, `core.hooksPath` e `core.fsmonitor` com scripts que gravam marcador e `filter.lfs.*` que grava
    marcador, a obtenção do repositório `atributos` (que tem `filter=lfs`) dá `Fetched` exato e publica
    os bytes dos blobs; nenhum marcador aparece, e todo pedido vai a `repos.cq.test`, nenhum a
    `outro.cq.test`, e nenhum leva `Authorization`.
    Controle positivo: um `git` puro no mesmo HOME e com o mesmo `/etc/gitconfig` honra tudo isso; o
    `clone` vai a `outro.cq.test` e roda os hooks e o filtro, e o `status` no clone roda o fsmonitor."""
    entry = _case(fetch_report, "hostile_config")
    assert entry["config"]["hostile"] is True
    assert entry["run"]["outcomes"] == [_fetched(entry["expected"])]
    _assert_published(entry)
    assert entry["requests"], "nenhum pedido chegou ao servidor"
    assert _hosts(entry) == {HOST}, entry["requests"]
    assert [r for r in entry["requests"] if r["auth"]] == []
    assert entry["markers"] == [], f"a configuração hostil foi executada: {entry['markers']}"

    control = _hostile_control(fetch_report)
    steps = control["steps"]
    assert steps["clone"]["exit"] == 0, steps["clone"]["stderr"]
    assert steps["clone"]["requests"], "o clone do controle não chegou ao servidor"
    assert _request_hosts(steps["clone"]["requests"]) == {OTHER_HOST}, (
        f"o `insteadOf` do ~/.gitconfig não teve efeito no git puro: {steps['clone']['requests']}")
    assert steps["status"]["exit"] == 0, steps["status"]["stderr"]
    markers = set(control["markers"] or [])
    assert {"hook-post-checkout", "hook-reference-transaction"} <= markers, (
        f"o `core.hooksPath` do /etc/gitconfig não teve efeito no git puro: {sorted(markers)}")
    assert "fsmonitor" in markers, f"o `core.fsmonitor` não teve efeito no git puro: {sorted(markers)}"
    assert "filtro-smudge" in markers, f"o `filter.lfs.smudge` não teve efeito no git puro: {sorted(markers)}"


def test_hostile_credential_helpers_are_not_run_and_no_credential_is_sent(fetch_report: dict[str, Any]) -> None:
    """A regra (nenhuma credencial nem prompt, sem ajudante de credenciais) e a construção do ambiente
    (`credential.helper=` vazio, `GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS`) com a classificação (`terminal prompts
    disabled` → `FetchNotFound("repository")`); nenhum segredo exposto (o worker tem segredos no ambiente): com
    um `credential.helper` em cada nível (sistema e usuário) que grava marcador, e o do usuário devolve
    usuário e senha, o caminho `/protegido/`, que responde 401 sem `Authorization`, dá
    `FetchNotFound("repository")` sem árvore nem staging; nenhum ajudante roda, nenhum pedido leva
    `Authorization`, e nenhum vai a `outro.cq.test`.
    Controle positivo: o `clone` do mesmo caminho com um `git` puro no mesmo ambiente roda os dois
    ajudantes, manda `Authorization` e dá certo."""
    entry = _case(fetch_report, "hostile_config_auth")
    assert entry["config"]["hostile"] is True
    assert entry["run"]["outcomes"] == [{"type": "FetchNotFound", "what": "repository"}]
    _assert_nothing_left(entry)
    assert entry["requests"], "nenhum pedido chegou ao servidor"
    assert all(r["path"].startswith(f"{PROTECTED_PREFIX}/") for r in entry["requests"]), entry["requests"]
    assert _hosts(entry) == {HOST}, entry["requests"]
    assert [r for r in entry["requests"] if r["auth"]] == [], "um pedido levou credencial"
    assert entry["markers"] == [], f"a configuração hostil foi executada: {entry['markers']}"

    control = _hostile_control(fetch_report)
    step = control["steps"]["clone_protected"]
    assert step["exit"] == 0, step["stderr"]
    assert any(r["auth"] for r in step["requests"]), (
        f"o ajudante do ~/.gitconfig não mandou credencial no git puro: {step['requests']}")
    markers = set(control["markers"] or [])
    assert {"credencial-sistema", "credencial-usuario"} <= markers, (
        f"os ajudantes de credenciais não rodaram no git puro: {sorted(markers)}")
