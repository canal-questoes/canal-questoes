"""Comandos locais da pilha pelo CLI e a dica e a pergunta de subir a pilha local, com os casos de uso e as
situações de erro da pilha.


O `cq` roda em processo pelo `invoke_cli` (tests/helpers/cli_runner.py), com o ambiente limpo e o arquivo de perfil de
teste por `CQ_CONFIG`; o Docker é sempre dublê:
- **nível 1**: o `FakeStack` (tests/helpers/stack_fake.py) em `ctx.obj["stack"]`, com o relógio e o `sleep` falsos
  (`FakeSleep`) em `clock` e `sleep`: a espera de até 180 s do `up` roda sem esperar de verdade;
- **nível 2**: o `ComposeStack` real sobre o `FakeRunner` (tests/helpers/docker_fake.py) em `ctx.obj["runner"]`, para
  a limpeza dos registros e a classificação das falhas com o segredo-sentinela.
O diretório da pilha é do `tmp_path` (`make_stack_dir`), achado pelos marcadores a partir de `ctx.obj["cwd"]`.

Endereços (o loopback com porta explícita): os casos de sucesso de `up` e `status` usam o perfil local legado
`api_url = TEST_API_URL` (a porta do `FakeStack`), com a API dos testes por `http=`; a API inalcançável é
`http://127.0.0.1:9` (ou um `http=` dublê que recusa a conexão, sem rede); nunca a porta 8000.
Terminal simulado: só `stdin_is_terminal` e `stderr_is_terminal` substituídos (`invoke_cli(terminal=True)`); o
`ask_start_local_stack` roda real, lendo o stdin simulado (`answer`), salvo no Ctrl+C da pergunta.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import re
from typing import Any

import httpx2
import pytest

import cq_server
from cq_client import cli as cq_cli
from cq_client.doctor import API_ACCESS_FAILURE_MESSAGES
from cq_client.local_commands import (
    ASK_START_TEMPLATE,
    DOWN_DONE_TEMPLATE,
    FOREIGN_API_TEMPLATE,
    INTERRUPTED_MESSAGE,
    LOGS_FOLLOW_JSON_REFUSED,
    LOGS_SERVICE_REFUSED,
    PORT_MISMATCH_MESSAGE,
    RERUN_MESSAGE,
    SERVICE_LINE_TEMPLATE,
    UP_DONE_TEMPLATE,
    UP_PROGRESS_FIRST_BUILD,
    UP_PROGRESS_STARTING,
    UP_PROGRESS_WAITING,
)
from cq_client.stack import (
    LOG_SERVICES,
    SERVICES,
    VARIABLE_MISSING_UNNAMED,
    DockerUnavailable,
    RemoteProfileRefused,
    StackDirNotFound,
    StackEnvMissing,
    StackFailure,
    StackTargetRefused,
    StackVariableMissing,
)
from cq_server.migrate import EXPECTED_VERSION

from tests.helpers.api import ADMIN_TOKEN, create_offering, create_token, register_person
from tests.helpers.cli_runner import (
    ENV_SENTINEL,
    ENV_SENTINEL_TOKEN,
    UNREACHABLE_API_URL,
    invoke_cli,
    make_stack_dir,
    write_profile,
)
from tests.helpers.clock import FakeClock
from tests.helpers.contract import MESSAGES, assert_cli_json
from tests.helpers.docker_fake import (
    BUILD_FAILURES,
    DAEMON_FAILURES,
    DOCKER_MISSING,
    FAILURE_STDERR,
    LOGS_CLEAN,
    PORT_FAILURES,
    PORT_NONE,
    SENTINEL_SECRET,
    FakeRunner,
    assert_allowed,
    failure_samples,
)
from tests.helpers.stack_fake import (
    OTHER_API_PORT,
    TEST_API_PORT,
    TEST_API_URL,
    FakeSleep,
    FakeStack,
    foreign_api,
    port_mismatch,
    snapshot,
)

PROJECT = "cq"
LOCAL_PROFILE = f'api_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n'
REMOTE_PROFILE = f'[profiles.vm]\nkind = "remote"\napi_url = "https://vm.cq.test"\ntoken = "{ADMIN_TOKEN}"\n'
DEV_PROFILE = (f'[profiles.dev]\nkind = "local"\napi_url = "{UNREACHABLE_API_URL}"\ncompose_project = "cq-dev"\n')

UNREACHABLE_LINE = f"Erro: Não foi possível conectar à API em {UNREACHABLE_API_URL}."
LOCAL_UNREACHABLE_LINE = f"Erro: Não foi possível conectar à API em {TEST_API_URL}."
HINT = 'Dica: a pilha local (projeto Compose "{project}") parece parada; suba-a com "cq up".'
TARGET_REFUSED = StackTargetRefused().message

LOCAL_COMMAND_ARGS = {"up": ["up"], "down": ["down"], "status": ["status"], "logs": ["logs"]}


# --------------------------------------------------------------------------------------------------
# Apoio


def _no_traceback(result: Any) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.stdout and "Traceback" not in result.stderr


def _run(args: list[str], *, stack: Any = None, runner: Any = None, http: Any = None, cwd: pathlib.Path,
         config: pathlib.Path, json: bool = False, env: dict | None = None, terminal: bool = False,
         answer: str | None = None) -> tuple[Any, FakeSleep]:
    """Roda o `cq` com o `ctx.obj` completo dos comandos da pilha (pilha, executor, API, relógio, `sleep` e `cwd`)."""
    sleep = FakeSleep(FakeClock())
    obj: dict[str, Any] = {"clock": sleep.monotonic, "sleep": sleep, "cwd": cwd}
    if stack is not None:
        obj["stack"] = stack
    if runner is not None:
        obj["runner"] = runner
    if http is not None:
        obj["http"] = http
    result = invoke_cli(args, json=json, obj=obj, env=env, config=config, terminal=terminal, answer=answer)
    _no_traceback(result)
    return result, sleep


def _progress(project: str = PROJECT) -> set[str]:
    return {UP_PROGRESS_STARTING.format(project=project), UP_PROGRESS_FIRST_BUILD, UP_PROGRESS_WAITING}


# No texto para pessoas do `up` e do `status`, o estado e a saúde de cada serviço saem
# com os rótulos em português; o `--json` mantém o enum.
STATE_LABELS = {"running": "rodando", "stopped": "parado", "absent": "ausente"}
HEALTH_LABELS = {"healthy": "saudável", "unhealthy": "com falha", "starting": "iniciando", "none": "sem verificação"}
# com o Docker indisponível, cada serviço sai com este rótulo no lugar de "<estado>, <saúde>".
DOCKER_UNAVAILABLE_SERVICE_LABEL = "não verificado (Docker indisponível)"


def _service_lines(snap: Any) -> list[str]:
    return [SERVICE_LINE_TEMPLATE.format(service=s.service, state=STATE_LABELS[s.state],
                                         health=HEALTH_LABELS[s.health]) for s in snap.services]


def _unverified_service_lines() -> list[str]:
    """as 4 linhas de serviço do texto do `status` com o Docker indisponível, no formato da
    `SERVICE_LINE_TEMPLATE` (`  <serviço>: `) só com o rótulo fixo no lugar de "<estado>, <saúde>"."""
    prefix = SERVICE_LINE_TEMPLATE.split("{state}")[0]
    assert prefix == "  {service}: ", SERVICE_LINE_TEMPLATE
    return [prefix.format(service=s) + DOCKER_UNAVAILABLE_SERVICE_LABEL for s in SERVICES]


def _services_json(snap: Any) -> list[dict]:
    return [{"service": s.service, "state": s.state, "health": s.health} for s in snap.services]


def _up_summary(project: str = PROJECT) -> list[str]:
    return [UP_DONE_TEMPLATE.format(project=project), *_service_lines(snapshot("healthy"))]


def _assert_last_line(stderr: str, last: str, project: str = PROJECT) -> None:
    """O stderr termina na linha fixa `last`; antes dela, só as linhas fixas de progresso do `up`."""
    lines = stderr.splitlines()
    assert lines and lines[-1] == last, stderr
    assert set(lines[:-1]) <= _progress(project), stderr


def _assert_status_summary(result: Any) -> None:
    """O `status` com o código 1 imprime o relatório no stdout e, no
    stderr, uma linha fixa de resumo: exatamente uma linha não vazia (no texto e no `--json`), sem a dica nem a
    pergunta de subir a pilha local (o `status` não é folha do catálogo) e sem nome de outro projeto."""
    assert result.exit_code == 1
    assert result.stderr.endswith("\n"), result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1 and lines[0].strip() != "", result.stderr
    assert "Dica" not in result.stderr and "[s/N]" not in result.stderr
    assert "cq-demo" not in result.stderr


def _health(client: Any) -> dict:
    resp = client.get("/health")
    assert resp.status_code == 200
    return resp.json()


def _api_line(state: str = "saudável", schema: str = EXPECTED_VERSION) -> str:
    return f"API: {state}, versão {cq_server.__version__}, esquema {schema} (esperado {EXPECTED_VERSION})."


def _local_view(token_set: bool = True) -> dict:
    return {"name": None, "kind": "local", "api_url": TEST_API_URL, "token_set": token_set, "compose_project": PROJECT,
            "origins": {"name": "default", "kind": "inferred", "api_url": "file",
                        "token": "file" if token_set else "default", "compose_project": "default"}}


@contextlib.contextmanager
def _recorded(http: Any):
    seen: list[Any] = []
    hooks = http.event_hooks["request"]
    hooks.append(seen.append)
    try:
        yield seen
    finally:
        hooks.remove(seen.append)


class _ApiDownUntilUp:
    """O `http=` da API fora do ar: recusa a conexão (como o transporte faz com a porta fechada) até a pilha do
    `stack` receber `up()`; depois, repassa ao `TestClient`. Sem `stack`, fica sempre fora do ar. Nenhuma rede."""

    def __init__(self, client: Any, stack: FakeStack | None = None) -> None:
        self.client = client
        self.stack = stack
        self.paths: list[str] = []

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        self.paths.append(httpx2.URL(str(url)).path)
        if self.stack is None or "up" not in self.stack.names():
            raise httpx2.ConnectError("conexão recusada (dublê)")
        return self.client.request(method, url, **kwargs)

    def close(self) -> None:
        pass


class _FixedHealth:
    """O `http=` que responde `GET /health` com o 503 da API (corpo congelado de `system.health`)."""

    def __init__(self, body: dict) -> None:
        self.body = body

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        assert httpx2.URL(str(url)).path == "/health", url
        return httpx2.Response(503, json=self.body, request=httpx2.Request(method, str(url)))

    def close(self) -> None:
        pass


class _HealthResponse:
    """O `http=` que responde `GET /health` com o código e o corpo JSON dados (por exemplo, um 200 com um corpo fora
    do esquema `system.health.response`). Nenhuma rede."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self.body = body
        self.status = status

    def request(self, method: str, url: Any, **kwargs: Any) -> Any:
        assert httpx2.URL(str(url)).path == "/health", url
        return httpx2.Response(self.status, json=self.body, request=httpx2.Request(method, str(url)))

    def close(self) -> None:
        pass


def _setup(tmp_path: pathlib.Path, profile: str = LOCAL_PROFILE, *, env_file: bool = True
           ) -> tuple[pathlib.Path, pathlib.Path]:
    """O arquivo de perfil e o diretório da pilha (com os marcadores e, se pedido, o `.env`)."""
    return write_profile(tmp_path, profile), make_stack_dir(tmp_path, env_file=env_file)


# --------------------------------------------------------------------------------------------------
# up


def test_up_from_stopped_stack(tmp_path, client) -> None:
    """`up` da pilha parada sobe os 4 serviços (`up` sem reconstruir),
    espera a saúde só depois do Compose, confere a porta e imprime o resumo exato (código 0); o progresso vai ao
    stderr só com as linhas fixas; no `--json`, o `up.output` exato."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped", "starting", "healthy"])
    result, sleep = _run(["up"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "".join(f"{line}\n" for line in _up_summary())
    lines = result.stderr.splitlines()
    assert UP_PROGRESS_STARTING.format(project=PROJECT) in lines
    assert set(lines) <= _progress()
    names = stack.names()
    assert names.count("up") == 1 and ("up", {"rebuild": False}) in stack.calls
    assert "snapshot" in names[names.index("up") + 1:]
    assert "down" not in names
    assert sleep.count >= 1
    stack_json = FakeStack(["stopped", "starting", "healthy"])
    as_json, _ = _run(["up"], json=True, stack=stack_json, http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 0, as_json.stderr
    body = assert_cli_json("up", as_json.stdout)
    assert body == {"project": PROJECT, "services": _services_json(snapshot("healthy")), "api_port": TEST_API_PORT}


def test_up_idempotent_and_partial(tmp_path, client) -> None:
    """com a pilha já saudável, `up` termina com o mesmo resumo (nada é derrubado nem recriado);
    com a pilha parcial, sobe só o que falta (um `up` sem reconstruir)."""
    config, stack_dir = _setup(tmp_path)
    healthy = FakeStack(["healthy"])
    first, _ = _run(["up"], stack=healthy, http=client, cwd=stack_dir, config=config)
    assert first.exit_code == 0, first.stderr
    assert first.stdout == "".join(f"{line}\n" for line in _up_summary())
    assert "down" not in healthy.names()
    assert all(kwargs == {"rebuild": False} for name, kwargs in healthy.calls if name == "up")
    partial = FakeStack(["partial", "healthy"])
    second, _ = _run(["up"], stack=partial, http=client, cwd=stack_dir, config=config)
    assert second.exit_code == 0, second.stderr
    assert second.stdout == first.stdout
    assert [c for c in partial.calls if c[0] in ("up", "down")] == [("up", {"rebuild": False})]


def test_up_rebuild(tmp_path, client) -> None:
    """`up --rebuild` pede a reconstrução (`rebuild=True`)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    result, _ = _run(["up", "--rebuild"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    assert [c for c in stack.calls if c[0] == "up"] == [("up", {"rebuild": True})]


@pytest.mark.parametrize(
    ("timeline", "args", "seconds", "not_ok"),
    [(["stopped", "restart_loop"], ["up"], 180, ("api",)),
     (["stopped", "starting"], ["up", "--timeout", "5"], 5, ("api", "engine"))],
    ids=["api_em_laco_180s", "subindo_5s"])
def test_up_wait_exhausted(tmp_path, client, timeline, args, seconds, not_ok) -> None:
    """a api em laço de reinício (ou a saúde que não chega) até esgotar a
    espera → 1, a linha fixa com o tempo e os serviços que não ficaram "ok" e `cq logs`/`cq doctor`, sem derrubar
    nada, stdout vazio; a espera roda no relógio e no `sleep` falsos."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(timeline)
    result, sleep = _run(args, stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 1
    assert result.stdout == ""
    last = result.stderr.splitlines()[-1:]
    assert last and last[0].startswith(f"A pilha não ficou saudável em {seconds} s: ")
    assert last[0].endswith('. Veja "cq logs" e "cq doctor".')
    named = last[0].removeprefix(f"A pilha não ficou saudável em {seconds} s: ").split(".")[0]
    assert {s.strip() for s in named.split(",")} == set(not_ok)
    assert set(result.stderr.splitlines()[:-1]) <= _progress()
    assert "down" not in stack.names()
    assert seconds - 1 <= sleep.total <= seconds + 1


def test_up_port_mismatch(tmp_path, client) -> None:
    """Porta da API diferente da padrão: os 4 serviços "ok", mas a api
    publicada noutra porta que a do perfil → 1, a linha fixa da porta divergente, stdout vazio, nada derrubado."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped", port_mismatch()])
    result, _ = _run(["up"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 1
    assert result.stdout == ""
    _assert_last_line(result.stderr, PORT_MISMATCH_MESSAGE)
    assert "down" not in stack.names()


@pytest.mark.parametrize("value", ["0", "3601", "-5", "abc"])
def test_up_timeout_out_of_range(tmp_path, client, value) -> None:
    """`--timeout` fora de 1 a 3600 → 2, `Uso incorreto:` sem eco do valor e nenhuma chamada à
    pilha; os limites 1 e 3600 são aceitos (controle positivo)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    result, _ = _run(["up", "--timeout", value], stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr.startswith("Uso incorreto: ") and len(result.stderr.splitlines()) == 1
    assert value not in result.stderr
    assert stack.calls == []
    for edge in ("1", "3600"):
        accepted, _ = _run(["up", "--timeout", edge], stack=FakeStack(["healthy"]), http=client, cwd=stack_dir,
                           config=config)
        assert accepted.exit_code == 0, accepted.stderr


def test_foreign_api_is_never_healthy(tmp_path, client) -> None:
    """a API no endereço do perfil é de outro projeto
    (`api_owned` falso): o `status` não a declara saudável (aviso fixo `foreign_api`, que nomeia só o projeto do
    perfil; código 1; a linha de resumo no stderr), e o `up` também não: ele pede a subida do próprio projeto (um
    `up` sem reconstruir) e sai com 1, sem o resumo e sem derrubar nada; com a porta ocupada pela outra pilha
    (`StackFailure("port_in_use")`, o caminho real da porta ocupada), a última linha é a da porta ocupada, sem o nome do
    outro projeto."""
    config, stack_dir = _setup(tmp_path)
    status, _ = _run(["status"], stack=FakeStack([foreign_api()]), http=client, cwd=stack_dir, config=config)
    assert status.exit_code == 1
    assert FOREIGN_API_TEMPLATE.format(project=PROJECT) in status.stdout.splitlines()
    assert "cq-demo" not in status.stdout + status.stderr
    _assert_status_summary(status)
    as_json, _ = _run(["status"], json=True, stack=FakeStack([foreign_api()]), http=client, cwd=stack_dir,
                      config=config)
    assert as_json.exit_code == 1
    body = assert_cli_json("status", as_json.stdout)
    assert body["warnings"] == ["foreign_api"]
    assert body["api"]["owned_by_stack"] is False
    _assert_status_summary(as_json)
    # O up pede a subida do próprio projeto; a API de fora nunca conta como a pilha saudável.
    stack = FakeStack([foreign_api(), snapshot("healthy", api_owned=False)])
    up, _ = _run(["up", "--timeout", "3"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert up.exit_code == 1
    assert up.stdout == ""
    assert UP_DONE_TEMPLATE.format(project=PROJECT) not in up.stderr
    assert [c for c in stack.calls if c[0] == "up"] == [("up", {"rebuild": False})]
    assert "down" not in stack.names()
    # A porta ocupada pela outra pilha: a subida do próprio projeto falha com a linha fixa da porta ocupada.
    busy = FakeStack([foreign_api()])
    busy.fail("up", StackFailure("port_in_use"))
    failed, _ = _run(["up"], stack=busy, http=client, cwd=stack_dir, config=config)
    assert (failed.exit_code, failed.stdout) == (1, "")
    _assert_last_line(failed.stderr, StackFailure("port_in_use").message)
    assert [c for c in busy.calls if c[0] == "up"] == [("up", {"rebuild": False})]
    assert "cq-demo" not in failed.stderr
    assert "down" not in busy.names()


# --------------------------------------------------------------------------------------------------
# Recusas e falhas fixas


@pytest.mark.parametrize("command", ["up", "down", "logs"])
def test_env_file_missing_refused_without_call(tmp_path, client, command) -> None:
    """sem o `.env` no diretório da pilha, `up`, `down` e `logs` → 2, a linha fixa,
    stdout vazio, nenhuma chamada à pilha, e o `.env` continua ausente."""
    config, stack_dir = _setup(tmp_path, env_file=False)
    stack = FakeStack(["healthy"])
    result, _ = _run(LOCAL_COMMAND_ARGS[command], stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == StackEnvMissing().message + "\n"
    assert stack.calls == []
    assert not (stack_dir / ".env").exists()


def test_status_with_env_file_missing(tmp_path, client) -> None:
    """o `status` sem o `.env` → 1, o relatório no stdout com os 4 serviços
    "não verificado" (`unknown`) e o aviso `env_file_missing` (no texto, a mesma linha fixa), sem chamar a pilha; a
    parte da API roda."""
    config, stack_dir = _setup(tmp_path, env_file=False)
    stack = FakeStack(["healthy"])
    text, _ = _run(["status"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert text.exit_code == 1
    lines = text.stdout.splitlines()
    assert StackEnvMissing().message in lines
    assert _api_line() in lines
    _assert_status_summary(text)
    as_json, _ = _run(["status"], json=True, stack=stack, http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 1
    _assert_status_summary(as_json)
    body = assert_cli_json("status", as_json.stdout)
    assert body["services"] == [{"service": s, "state": "unknown", "health": "unknown"} for s in SERVICES]
    assert body["warnings"] == ["env_file_missing"]
    assert body["api"]["reachable"] is True and body["api"]["health"] == _health(client)
    assert stack.calls == []


@pytest.mark.parametrize(("command", "method"),
                         [("up", "up"), ("down", "down"), ("status", "snapshot"), ("logs", "logs")])
@pytest.mark.parametrize("name", ["CQ_DB_PASSWORD", None], ids=["com_nome", "sem_nome"])
def test_variable_missing(tmp_path, client, command, method, name) -> None:
    """a variável obrigatória ausente no `.env` → 2, a linha fixa que cita o nome (só no
    padrão `CQ_…`), sem valor, e stdout vazio (também no `status`)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped"])
    stack.fail(method, StackVariableMissing(name))
    result, _ = _run(LOCAL_COMMAND_ARGS[command], stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    expected = (f"Configuração da pilha incompleta: defina {name} no arquivo de ambiente." if name
                else VARIABLE_MISSING_UNNAMED)
    _assert_last_line(result.stderr, expected)
    assert ENV_SENTINEL not in result.stderr


@pytest.mark.parametrize("command", ["up", "down", "logs"])
def test_docker_unavailable(tmp_path, client, command) -> None:
    """o Docker indisponível → 1, a mensagem fixa, stdout vazio, nenhum
    rastreamento."""
    config, stack_dir = _setup(tmp_path)
    result, _ = _run(LOCAL_COMMAND_ARGS[command], stack=FakeStack.unavailable(), http=client, cwd=stack_dir,
                     config=config)
    assert (result.exit_code, result.stdout) == (1, "")
    _assert_last_line(result.stderr, DockerUnavailable().message)


@pytest.mark.parametrize("kind", ["port_in_use", "build_failed", "other"])
def test_up_compose_failure(tmp_path, client, kind) -> None:
    """Porta ocupada por outro projeto, sem tentar outra porta: o `up` com a
    porta ocupada, a construção falha ou outra falha do Compose → 1, a linha fixa da classe, sem texto do Compose,
    stdout vazio."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped"])
    stack.fail("up", StackFailure(kind))
    result, _ = _run(["up"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (1, "")
    _assert_last_line(result.stderr, StackFailure(kind).message)
    assert stack.count("up") == 1


@pytest.mark.parametrize("command", ["up", "down", "logs"])
def test_remote_profile_refused(tmp_path, client, command) -> None:
    """`up`, `down` e `logs` com o perfil remoto → 2, a linha fixa, nenhuma chamada."""
    config, stack_dir = _setup(tmp_path, REMOTE_PROFILE)
    stack = FakeStack(["healthy"])
    result, _ = _run(["--profile", "vm", *LOCAL_COMMAND_ARGS[command]], stack=stack, http=client, cwd=stack_dir,
                     config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == RemoteProfileRefused().message + "\n"
    assert stack.calls == []


@pytest.mark.parametrize("env", [{"DOCKER_HOST": "tcp://10.0.0.5:2376"}, {"DOCKER_CONTEXT": "contexto-remoto"}],
                         ids=["docker_host_tcp", "docker_context"])
@pytest.mark.parametrize("command", ["up", "down", "status", "logs"])
def test_non_local_docker_target_refused(tmp_path, client, command, env) -> None:
    """`DOCKER_HOST` não local ou `DOCKER_CONTEXT` definida → 2, a linha fixa sem eco
    do valor, stdout vazio (também no `status`) e nenhuma chamada à pilha."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    result, _ = _run(LOCAL_COMMAND_ARGS[command], stack=stack, http=client, cwd=stack_dir, config=config, env=env)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == TARGET_REFUSED + "\n"
    assert "10.0.0.5" not in result.stderr and "contexto-remoto" not in result.stderr
    assert stack.calls == []


@pytest.mark.parametrize("command", ["up", "down", "status", "logs"])
def test_missing_stack_dir_refused(tmp_path, client, command) -> None:
    """O perfil nomeado `local` com `stack_dir` inexistente →
    2, a mensagem fixa de `StackDirNotFound` sem o caminho, stdout vazio e nenhuma chamada (mesmo com uma pilha
    no diretório atual)."""
    missing = tmp_path / "pilha-sumida-Wv3"
    profile = (f'[profiles.local]\nkind = "local"\napi_url = "{TEST_API_URL}"\ntoken = "{ADMIN_TOKEN}"\n'
               f'stack_dir = "{missing.as_posix()}"\n')
    config, stack_dir = _setup(tmp_path, profile)
    stack = FakeStack(["healthy"])
    result, _ = _run(["--profile", "local", *LOCAL_COMMAND_ARGS[command]], stack=stack, http=client, cwd=stack_dir,
                     config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == StackDirNotFound().message + "\n"
    assert "pilha-sumida-Wv3" not in result.stderr
    assert stack.calls == []


# --------------------------------------------------------------------------------------------------
# logs


def test_logs_default_and_json(tmp_path, client) -> None:
    """`logs` sem opções pede os registros da api, do worker e do postgres (nunca o
    engine), 200 linhas, sem seguir, e os imprime uma por linha; no `--json`, `logs.output`."""
    config, stack_dir = _setup(tmp_path)
    lines = ["api-1    | linha 1", "worker-1 | linha 2", "postgres-1 | linha 3"]
    stack = FakeStack(["healthy"], logs=lines)
    result, _ = _run(["logs"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "".join(f"{line}\n" for line in lines)
    logs_calls = [kwargs for name, kwargs in stack.calls if name == "logs"]
    assert [(set(k["services"]), len(k["services"]), k["tail"], k["follow"]) for k in logs_calls] == [
        (set(LOG_SERVICES), 3, 200, False)]
    as_json, _ = _run(["logs", "--tail", "2", "--service", "worker", "--service", "api"], json=True,
                      stack=FakeStack(["healthy"], logs=lines), http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 0, as_json.stderr
    assert assert_cli_json("logs", as_json.stdout) == {"lines": lines[-2:]}


@pytest.mark.parametrize("services", [["engine"], ["API"], ["cq-engine-1"], ["api", "engine"], ["worker", "Postgres"]],
                         ids=["engine", "caixa", "nome_do_conteiner", "lista_mista", "lista_com_caixa"])
def test_logs_service_refused(tmp_path, client, services) -> None:
    """`--service` fora de {api, worker, postgres}, comparado exatamente → 2, a linha fixa
    sem eco do valor e nenhuma chamada; `--service api --service worker` é aceito (controle positivo)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    args = ["logs"] + [token for s in services for token in ("--service", s)]
    result, _ = _run(args, stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    assert result.stderr == f"Uso incorreto: {LOGS_SERVICE_REFUSED}\n"
    for value in services:
        if value not in ("engine", "api", "worker"):
            assert value not in result.stderr
    assert stack.calls == []
    accepted = FakeStack(["healthy"])
    ok, _ = _run(["logs", "--service", "api", "--service", "worker"], stack=accepted, http=client, cwd=stack_dir,
                 config=config)
    assert ok.exit_code == 0, ok.stderr
    assert [set(k["services"]) for n, k in accepted.calls if n == "logs"] == [{"api", "worker"}]


@pytest.mark.parametrize(("args", "json_mode"),
                         [(["logs", "--tail", "0"], False), (["logs", "--tail", "10001"], False),
                          (["logs", "--follow"], True)],
                         ids=["tail_zero", "tail_acima", "json_com_follow"])
def test_logs_usage_errors(tmp_path, client, args, json_mode) -> None:
    """`--tail` fora de 1 a 10000 e `--follow` com o `--json` global → 2, sem chamada; os limites
    1 e 10000 são aceitos (controle positivo)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    result, _ = _run(args, json=json_mode, stack=stack, http=client, cwd=stack_dir, config=config)
    assert (result.exit_code, result.stdout) == (2, "")
    if json_mode:
        assert result.stderr == f"Uso incorreto: {LOGS_FOLLOW_JSON_REFUSED}\n"
    else:
        assert result.stderr.startswith("Uso incorreto: ") and args[-1] not in result.stderr
    assert stack.calls == []
    for edge in ("1", "10000"):
        accepted = FakeStack(["healthy"])
        ok, _ = _run(["logs", "--tail", edge], stack=accepted, http=client, cwd=stack_dir, config=config)
        assert ok.exit_code == 0, ok.stderr
        assert [k["tail"] for n, k in accepted.calls if n == "logs"] == [int(edge)]


def test_logs_of_stopped_service(tmp_path, client) -> None:
    """os registros de um serviço parado são mostrados (código 0)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped"], logs={"worker": ["worker-1 | saiu com código 1"]})
    result, _ = _run(["logs", "--service", "worker"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == "worker-1 | saiu com código 1\n"


# --------------------------------------------------------------------------------------------------
# down


def test_down(tmp_path, client) -> None:
    """`down` para e remove a pilha mantendo os dados (código 0, a linha
    fixa); com a pilha já parada, também 0; no `--json`, `down.output`."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["healthy"])
    result, _ = _run(["down"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == DOWN_DONE_TEMPLATE.format(project=PROJECT) + "\n"
    assert stack.count("down") == 1 and stack.count("up") == 0
    absent = FakeStack(["absent"])
    again, _ = _run(["down"], stack=absent, http=client, cwd=stack_dir, config=config)
    assert again.exit_code == 0, again.stderr
    assert again.stdout == result.stdout
    as_json, _ = _run(["down"], json=True, stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 0, as_json.stderr
    assert assert_cli_json("down", as_json.stdout) == {"project": PROJECT}


# --------------------------------------------------------------------------------------------------
# status


def test_status_healthy(tmp_path, client) -> None:
    """com a pilha saudável e a API da pilha no endereço do perfil, o
    `status` dá 0, com o perfil, a origem, os serviços e a API, no texto e no `--json` exatos."""
    config, stack_dir = _setup(tmp_path)
    text, _ = _run(["status"], stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert text.exit_code == 0, text.stderr
    expected = [
        f"Perfil: implícito (local), API em {TEST_API_URL}, token definido.",
        "Origem: nome implícito, tipo inferido do endereço, endereço topo do arquivo, token topo do arquivo.",
        f"Pilha local (projeto {PROJECT}):",
        *_service_lines(snapshot("healthy")),
        _api_line(),
    ]
    assert text.stdout == "".join(f"{line}\n" for line in expected)
    assert ADMIN_TOKEN not in text.stdout + text.stderr
    as_json, _ = _run(["status"], json=True, stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 0, as_json.stderr
    body = assert_cli_json("status", as_json.stdout)
    assert body == {"profile": _local_view(), "services": _services_json(snapshot("healthy")),
                    "api_port": TEST_API_PORT,
                    "api": {"reachable": True, "health": _health(client), "owned_by_stack": True}, "warnings": []}


def test_status_never_created_stack(tmp_path) -> None:
    """a pilha nunca criada e a API inalcançável → 1, com o relatório no stdout (os
    serviços ausentes e a API inalcançável), no texto e no `--json`."""
    config, stack_dir = _setup(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
    text, _ = _run(["status"], stack=FakeStack(["absent"]), cwd=stack_dir, config=config)
    assert text.exit_code == 1
    lines = text.stdout.splitlines()
    assert f"Pilha local (projeto {PROJECT}):" in lines
    assert any(line.startswith("API: inalcançável") for line in lines)
    _assert_status_summary(text)
    as_json, _ = _run(["status"], json=True, stack=FakeStack(["absent"]), cwd=stack_dir, config=config)
    assert as_json.exit_code == 1
    _assert_status_summary(as_json)
    body = assert_cli_json("status", as_json.stdout)
    assert body["services"] == [{"service": s, "state": "absent", "health": "none"} for s in SERVICES]
    assert body["api"]["reachable"] is False and body["api"]["health"] is None


def test_status_docker_unavailable(tmp_path, client) -> None:
    """com o Docker indisponível, o `status` dá 1, com o relatório:
    os serviços "não verificado" (no texto, logo depois da linha da pilha, as 4 linhas
    `  <serviço>: não verificado (Docker indisponível)`), o aviso `docker_unavailable` e a saúde da API consultada;
    no stderr, a mensagem fixa do Docker indisponível como a única linha (a linha de resumo)."""
    config, stack_dir = _setup(tmp_path)
    text, _ = _run(["status"], stack=FakeStack.unavailable(), http=client, cwd=stack_dir, config=config)
    assert text.exit_code == 1
    lines = text.stdout.splitlines()
    assert _api_line() in lines
    header = lines.index(f"Pilha local (projeto {PROJECT}):")
    assert lines[header + 1:header + 1 + len(SERVICES)] == _unverified_service_lines(), text.stdout
    assert text.stderr == DockerUnavailable().message + "\n"
    _assert_status_summary(text)
    as_json, _ = _run(["status"], json=True, stack=FakeStack.unavailable(), http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 1
    _assert_status_summary(as_json)
    body = assert_cli_json("status", as_json.stdout)
    assert body["services"] == [{"service": s, "state": "unknown", "health": "unknown"} for s in SERVICES]
    assert body["warnings"] == ["docker_unavailable"]
    assert body["api"] == {"reachable": True, "health": _health(client), "owned_by_stack": None}
    assert body["api_port"] is None


def test_status_remote_profile_does_not_touch_stack(tmp_path, client) -> None:
    """com o perfil remoto, o `status` mostra só o perfil e a API (`services = null`), sem
    nenhuma chamada à pilha."""
    config, stack_dir = _setup(tmp_path, REMOTE_PROFILE)
    stack = FakeStack(["healthy"])
    result, _ = _run(["--profile", "vm", "status"], json=True, stack=stack, http=client, cwd=stack_dir, config=config)
    assert result.exit_code == 0, result.stderr
    body = assert_cli_json("status", result.stdout)
    assert (body["services"], body["api_port"], body["warnings"]) == (None, None, [])
    assert stack.calls == []


def test_status_with_failing_api_health(tmp_path) -> None:
    """Os 4 serviços saudáveis e o `health` 503 (o esquema
    atrasado, no corpo congelado da API) → `API: com falha, …`, código 1, o relatório no stdout, no texto e no
    `--json` (`api.health` igual ao corpo do 503), e nenhuma linha da dica de subir a pilha local."""
    body503 = {"status": "error", "version": cq_server.__version__, "database": "ok", "schema_version": "0001",
               "expected_schema_version": EXPECTED_VERSION}
    config, stack_dir = _setup(tmp_path)
    text, _ = _run(["status"], stack=FakeStack(["healthy"]), http=_FixedHealth(body503), cwd=stack_dir, config=config)
    assert text.exit_code == 1
    assert _api_line("com falha", "0001") in text.stdout.splitlines()
    assert "Dica" not in text.stdout + text.stderr
    _assert_status_summary(text)
    as_json, _ = _run(["status"], json=True, stack=FakeStack(["healthy"]), http=_FixedHealth(body503), cwd=stack_dir,
                      config=config)
    assert as_json.exit_code == 1
    body = assert_cli_json("status", as_json.stdout)
    assert body["api"]["reachable"] is True and body["api"]["health"] == body503
    assert "Dica" not in as_json.stderr
    _assert_status_summary(as_json)


_OFF_SCHEMA_SENTINEL = "corpo-fora-do-esquema-sentinela"


@pytest.mark.parametrize(
    "body",
    [{"status": "ok", "version": cq_server.__version__, "database": "ok", "schema_version": EXPECTED_VERSION,
      "expected_schema_version": EXPECTED_VERSION, "debug": _OFF_SCHEMA_SENTINEL},
     {"status": "ok", "detail": _OFF_SCHEMA_SENTINEL}],
    ids=["chave_estranha", "sem_campos_obrigatorios"])
def test_status_health_body_outside_schema(tmp_path, body) -> None:
    """Saída estrita: o endereço do perfil responde ao `GET /health` com
    um corpo JSON fora do esquema `system.health.response` (uma chave estranha, ou sem os campos obrigatórios) → no
    `--json`, `api.reachable` verdadeiro e `api.health` nulo, sem repassar o corpo (nem a chave estranha nem o seu
    valor) no stdout ou no stderr; a saída valida no esquema estrito do `status.output`. No texto, o corpo também
    não é repassado."""
    config, stack_dir = _setup(tmp_path)
    as_json, _ = _run(["status"], json=True, stack=FakeStack(["healthy"]), http=_HealthResponse(body),
                      cwd=stack_dir, config=config)
    parsed = json.loads(as_json.stdout)
    assert parsed["api"]["reachable"] is True
    assert parsed["api"]["health"] is None, parsed["api"]
    assert _OFF_SCHEMA_SENTINEL not in as_json.stdout + as_json.stderr
    assert_cli_json("status", as_json.stdout)
    text, _ = _run(["status"], stack=FakeStack(["healthy"]), http=_HealthResponse(body), cwd=stack_dir,
                   config=config)
    assert _OFF_SCHEMA_SENTINEL not in text.stdout + text.stderr


def test_status_port_mismatch(tmp_path, client) -> None:
    """Porta da API diferente da padrão: o `api_port` da pilha
    diferente da porta do perfil → o aviso fixo de porta divergente, `warnings == ["port_mismatch"]` e código 1."""
    config, stack_dir = _setup(tmp_path)
    text, _ = _run(["status"], stack=FakeStack([port_mismatch()]), http=client, cwd=stack_dir, config=config)
    assert text.exit_code == 1
    assert PORT_MISMATCH_MESSAGE in text.stdout.splitlines()
    _assert_status_summary(text)
    as_json, _ = _run(["status"], json=True, stack=FakeStack([port_mismatch()]), http=client, cwd=stack_dir,
                      config=config)
    assert as_json.exit_code == 1
    _assert_status_summary(as_json)
    body = assert_cli_json("status", as_json.stdout)
    assert body["warnings"] == ["port_mismatch"]
    assert body["api_port"] == OTHER_API_PORT


# --------------------------------------------------------------------------------------------------
# Ctrl+C


def test_interrupt_in_up_and_logs_follow(tmp_path, client) -> None:
    """Ctrl+C (`KeyboardInterrupt`) no `up` e no `logs --follow` → 130,
    `Interrompido.`, nada derrubado."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped"])
    stack.fail("up", KeyboardInterrupt())
    up, _ = _run(["up"], stack=stack, http=client, cwd=stack_dir, config=config)
    assert (up.exit_code, up.stdout) == (130, "")
    _assert_last_line(up.stderr, INTERRUPTED_MESSAGE)
    assert "down" not in stack.names()
    lines = ["api-1 | um", "api-1 | dois", "api-1 | três"]
    follow_stack = FakeStack(["healthy"], logs=lines)
    follow_stack.fail("logs", KeyboardInterrupt(), after=2)
    follow, _ = _run(["logs", "--follow"], stack=follow_stack, http=client, cwd=stack_dir, config=config)
    assert follow.exit_code == 130
    assert follow.stdout.splitlines() == lines[: len(follow.stdout.splitlines())]
    assert len(follow.stdout.splitlines()) <= 2
    assert follow.stderr.splitlines()[-1:] == [INTERRUPTED_MESSAGE]
    assert [k["follow"] for n, k in follow_stack.calls if n == "logs"] == [True]
    assert "down" not in follow_stack.names()


# --------------------------------------------------------------------------------------------------
# A dica e a pergunta de subir a pilha local


def test_hint_on_unreachable_with_local_profile_only(tmp_path, client) -> None:
    """numa folha do catálogo com a API inalcançável e o perfil local (o implícito
    legado, local por inferência, ou um nomeado `local`), fora de terminal: 3, a mensagem fixa e a linha da dica
    com o projeto, stdout vazio. Com `CQ_API_URL`, com o perfil remoto ou com `--json`, a saída é byte a byte a mesma
    sem a dica; e a dica não sai no 401, no 403, no 409 nem no 503 do `health`."""
    legacy = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
    stack_dir = make_stack_dir(tmp_path)
    result, _ = _run(["people", "list"], cwd=stack_dir, config=legacy)
    assert (result.exit_code, result.stdout) == (3, "")
    assert result.stderr == f"{UNREACHABLE_LINE}\n{HINT.format(project='cq')}\n"
    named = write_profile(tmp_path, DEV_PROFILE, name="dev.toml")
    dev, _ = _run(["--profile", "dev", "people", "list"], cwd=stack_dir, config=named)
    assert (dev.exit_code, dev.stdout) == (3, "")
    assert dev.stderr == f"{UNREACHABLE_LINE}\n{HINT.format(project='cq-dev')}\n"

    # Byte a byte a mesma saída sem a dica: com CQ_API_URL, com o perfil remoto e com --json.
    with_env, _ = _run(["people", "list"], cwd=stack_dir, config=legacy, env={"CQ_API_URL": UNREACHABLE_API_URL})
    assert (with_env.exit_code, with_env.stdout, with_env.stderr) == (3, "", f"{UNREACHABLE_LINE}\n")
    remote = write_profile(tmp_path, REMOTE_PROFILE, name="remoto.toml")
    down = _ApiDownUntilUp(client)
    remote_result, _ = _run(["--profile", "vm", "people", "list"], http=down, cwd=stack_dir, config=remote)
    assert (remote_result.exit_code, remote_result.stdout) == (3, "")
    assert remote_result.stderr == "Erro: Não foi possível conectar à API em https://vm.cq.test.\n"
    as_json, _ = _run(["people", "list"], json=True, cwd=stack_dir, config=legacy)
    assert (as_json.exit_code, as_json.stdout) == (3, "")
    assert json.loads(as_json.stderr) == {"error": {"code": "api_unreachable",
                                                    "message": UNREACHABLE_LINE.removeprefix("Erro: ")}}

    # Sem dica fora da falha de conexão.
    no_token = write_profile(tmp_path, f'api_url = "{TEST_API_URL}"\n', name="sem-token.toml")
    r401, _ = _run(["people", "list"], http=client, cwd=stack_dir, config=no_token)
    assert r401.exit_code == 1 and r401.stderr == f"Erro: {MESSAGES['unauthenticated']}\n"
    secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:read"], label="token sem people:read")
    limited = write_profile(tmp_path, f'api_url = "{TEST_API_URL}"\ntoken = "{secret}"\n', name="limitado.toml")
    r403, _ = _run(["people", "list"], http=client, cwd=stack_dir, config=limited)
    assert r403.exit_code == 1 and "Dica" not in r403.stderr
    draft = create_offering(client, ADMIN_TOKEN)
    full = write_profile(tmp_path, LOCAL_PROFILE, name="local.toml")
    r409, _ = _run(["offerings", "close", draft["code"]], http=client, cwd=stack_dir, config=full)
    assert r409.exit_code == 1 and "Dica" not in r409.stderr
    body503 = {"status": "error", "version": cq_server.__version__, "database": "unreachable",
               "schema_version": None, "expected_schema_version": EXPECTED_VERSION}
    r503, _ = _run(["health"], http=_FixedHealth(body503), cwd=stack_dir, config=full)
    assert r503.exit_code == 1 and "Dica" not in r503.stderr


def _question(project: str) -> str:
    return ASK_START_TEMPLATE.format(project=project)


@pytest.mark.parametrize("case", ["json", "cq_api_url", "perfil_remoto"])
def test_no_hint_nor_question_in_terminal_outside_local_conditions(tmp_path, client, case) -> None:
    """Num terminal (stdin e stderr) e com "s" pronto no stdin,
    cada condição sozinha (`--json` com o perfil local; `CQ_API_URL` com o perfil local; o perfil remoto) tira a
    dica e a pergunta: a saída é byte a byte a mesma sem a dica (código 3, stdout vazio e o stderr igual ao de fora do
    terminal), sem `[s/N]`, sem a pergunta e nenhuma subida. Na mesma função (para não passar sem conferir nada), o
    controle positivo: no mesmo terminal, com o perfil local, sem `CQ_API_URL` e sem `--json`, a dica e a pergunta
    saem (com "n", nenhuma subida)."""
    stack_dir = make_stack_dir(tmp_path)
    local = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n', name="controle.toml")
    control_stack = FakeStack(["stopped"])
    control, _ = _run(["people", "list"], stack=control_stack, cwd=stack_dir, config=local, terminal=True,
                      answer="n\n")
    assert (control.exit_code, control.stdout) == (3, "")
    assert control.stderr.startswith(f"{UNREACHABLE_LINE}\n{HINT.format(project='cq')}\n{_question('cq')}"), \
        control.stderr
    assert control_stack.count("up") == 0

    if case == "perfil_remoto":
        config = write_profile(tmp_path, REMOTE_PROFILE)
        args, env, json_mode = ["--profile", "vm", "people", "list"], None, False
        expected = "Erro: Não foi possível conectar à API em https://vm.cq.test.\n"
    else:
        config = write_profile(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
        args = ["people", "list"]
        env = {"CQ_API_URL": UNREACHABLE_API_URL} if case == "cq_api_url" else None
        json_mode = case == "json"
        expected = None if json_mode else f"{UNREACHABLE_LINE}\n"
    stderrs = []
    for terminal in (False, True):
        stack = FakeStack(["stopped"])
        http = _ApiDownUntilUp(client, stack) if case == "perfil_remoto" else None
        result, _ = _run(args, json=json_mode, stack=stack, http=http, cwd=stack_dir, config=config, env=env,
                         terminal=terminal, answer="s\n" if terminal else None)
        assert (result.exit_code, result.stdout) == (3, ""), result.stderr
        if expected is not None:
            assert result.stderr == expected
        else:
            assert result.stderr.count("\n") == 1 and result.stderr.endswith("\n"), result.stderr
            assert json.loads(result.stderr) == {"error": {"code": "api_unreachable",
                                                           "message": UNREACHABLE_LINE.removeprefix("Erro: ")}}
        assert "[s/N]" not in result.stderr and "Dica" not in result.stderr
        assert ASK_START_TEMPLATE.split("{project}")[0] not in result.stderr
        assert stack.count("up") == 0
        stderrs.append(result.stderr)
    assert stderrs[0] == stderrs[1]


@pytest.mark.parametrize("answer", ["n\n", "\n", "talvez\n", None], ids=["nao", "vazio", "outra_resposta", "eof"])
@pytest.mark.parametrize(("profile", "args", "project"),
                         [(f'api_url = "{UNREACHABLE_API_URL}"\n', [], "cq"),
                          (DEV_PROFILE, ["--profile", "dev"], "cq-dev")],
                         ids=["legado", "nomeado"])
def test_question_answers_other_than_yes(tmp_path, profile, args, project, answer) -> None:
    """num terminal (stdin e stderr), depois da dica, a pergunta exata no stderr, com
    o projeto do perfil; "não", vazio, outra resposta e EOF → 3, nada no stdout e nenhuma subida."""
    config, stack_dir = _setup(tmp_path, profile)
    stack = FakeStack(["stopped"])
    result, _ = _run([*args, "people", "list"], stack=stack, cwd=stack_dir, config=config, terminal=True,
                     answer=answer)
    assert (result.exit_code, result.stdout) == (3, "")
    head = f"{UNREACHABLE_LINE}\n{HINT.format(project=project)}\n{_question(project)}"
    assert result.stderr.startswith(head), result.stderr
    assert result.stderr.removeprefix(head).strip() == ""
    assert stack.count("up") == 0


@pytest.mark.parametrize("answer", ["s\n", "sim\n", "SIM\n"])
def test_question_yes_runs_up_and_asks_to_rerun(tmp_path, client, answer) -> None:
    """"sim" roda a mesma rotina do `cq up` (resumo no stderr) e
    termina com `Rode o comando de novo.` e o código 3, sem repetir o comando original (um único pedido da folha)."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped", "starting", "healthy"])
    down = _ApiDownUntilUp(client, stack)
    result, _ = _run(["people", "list"], stack=stack, http=down, cwd=stack_dir, config=config, terminal=True,
                     answer=answer)
    assert (result.exit_code, result.stdout) == (3, "")
    lines = result.stderr.splitlines()
    assert lines[:2] == [LOCAL_UNREACHABLE_LINE, HINT.format(project="cq")]
    assert lines[-1] == RERUN_MESSAGE
    assert UP_DONE_TEMPLATE.format(project="cq") in lines
    assert _question("cq") in result.stderr
    assert [c for c in stack.calls if c[0] == "up"] == [("up", {"rebuild": False})]
    assert down.paths.count("/v1/people") == 1


@pytest.mark.parametrize(("failure", "env_file", "code", "last"),
                         [(StackFailure("port_in_use"), True, 1, StackFailure("port_in_use").message),
                          (None, False, 2, StackEnvMissing().message),
                          (KeyboardInterrupt(), True, 130, INTERRUPTED_MESSAGE)],
                         ids=["up_falha", "up_recusa", "up_interrompido"])
def test_question_yes_with_failing_up(tmp_path, client, failure, env_file, code, last) -> None:
    """Com "sim" e o `up` que falha (1), recusa (2) ou é interrompido (130) → o
    código do `up`, com a linha fixa dele, stdout vazio e sem repetir o comando."""
    config, stack_dir = _setup(tmp_path, env_file=env_file)
    stack = FakeStack(["stopped", "starting", "healthy"])
    if failure is not None:
        stack.fail("up", failure)
    down = _ApiDownUntilUp(client, stack)
    result, _ = _run(["people", "list"], stack=stack, http=down, cwd=stack_dir, config=config, terminal=True,
                     answer="s\n")
    assert (result.exit_code, result.stdout) == (code, "")
    assert result.stderr.splitlines()[-1:] == [last]
    assert down.paths.count("/v1/people") == 1


def test_question_ctrl_c_and_terminal_requirements(tmp_path, monkeypatch) -> None:
    """Ctrl+C na pergunta → 130 (o `ask_start_local_stack` substituído por um dublê que levanta
    `KeyboardInterrupt`, que o `input` do `CliRunner` não simula); só com o stdin num terminal (o stderr fora), não
    há pergunta (3, só a dica)."""
    config, stack_dir = _setup(tmp_path, f'api_url = "{UNREACHABLE_API_URL}"\n')
    only_stdin = FakeStack(["stopped"])
    monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: True)
    monkeypatch.setattr(cq_cli, "stderr_is_terminal", lambda: False)
    result, _ = _run(["people", "list"], stack=only_stdin, cwd=stack_dir, config=config, answer="s\n")
    assert (result.exit_code, result.stdout) == (3, "")
    assert result.stderr == f"{UNREACHABLE_LINE}\n{HINT.format(project='cq')}\n"
    assert only_stdin.count("up") == 0
    monkeypatch.undo()

    def interrupted(project: str) -> bool:
        raise KeyboardInterrupt

    monkeypatch.setattr(cq_cli, "ask_start_local_stack", interrupted)
    stack = FakeStack(["stopped"])
    ctrl_c, _ = _run(["people", "list"], stack=stack, cwd=stack_dir, config=config, terminal=True)
    assert (ctrl_c.exit_code, ctrl_c.stdout) == (130, "")
    assert stack.count("up") == 0


# --------------------------------------------------------------------------------------------------
# Roteiros da pilha e da recuperação em dois comandos


def test_stack_walkthrough(tmp_path, client) -> None:
    """Roteiro da pilha: só com o perfil local mínimo, uma folha falha com a dica; `cq up`, `cq status` (0) e
    `cq down`, cada um com código 0."""
    config, stack_dir = _setup(tmp_path)
    stack = FakeStack(["stopped", "starting", "healthy"], after_down="stopped")
    fail, _ = _run(["people", "list"], stack=stack, cwd=stack_dir, config=config)
    assert (fail.exit_code, fail.stdout) == (3, "")
    assert fail.stderr == f"{LOCAL_UNREACHABLE_LINE}\n{HINT.format(project='cq')}\n"
    for command in ("up", "status", "down"):
        result, _ = _run([command], stack=stack, http=client, cwd=stack_dir, config=config)
        assert result.exit_code == 0, (command, result.stderr)
    assert stack.names().count("up") == 1 and stack.names().count("down") == 1


def test_recovery_two_commands_after_the_original(tmp_path, client) -> None:
    """Roteiro da recuperação (a parte automatizável): a mesma folha do catálogo com o perfil local
    (1) falha com a dica com a pilha parada e a API inalcançável (`http=None`, fora de terminal); (2) `cq up` dá 0;
    (3) a mesma folha, agora com a API dos testes, dá 0 e o stdout da folha; o arquivo de perfil não muda e são
    exatamente 2 invocações além da original."""
    register_person(client, ADMIN_TOKEN, name="Ana Inventada Prado")
    config, stack_dir = _setup(tmp_path)
    before = config.read_bytes()
    stack = FakeStack(["stopped", "starting", "healthy"])
    invocations = []
    original, _ = _run(["offerings", "list"], stack=stack, cwd=stack_dir, config=config)
    invocations.append(original)
    assert (original.exit_code, original.stdout) == (3, "")
    assert original.stderr.splitlines() == [LOCAL_UNREACHABLE_LINE, HINT.format(project="cq")]
    up, _ = _run(["up"], stack=stack, http=client, cwd=stack_dir, config=config)
    invocations.append(up)
    assert up.exit_code == 0, up.stderr
    again, _ = _run(["offerings", "list"], stack=stack, http=client, cwd=stack_dir, config=config)
    invocations.append(again)
    assert again.exit_code == 0, again.stderr
    direct = invoke_cli(["offerings", "list"], obj={"http": client}, config=config)
    assert again.stdout == direct.stdout
    assert config.read_bytes() == before
    assert len(invocations) - 1 == 2


# --------------------------------------------------------------------------------------------------
# O CLI não cria, não altera nem lê o .env


@pytest.mark.parametrize("env_file", [False, True], ids=["sem_env", "com_env"])
def test_env_file_never_created_or_changed(tmp_path, client, env_file) -> None:
    """Depois de `up`, `down`, `status`, `logs` e
    `doctor`, o `.env` ausente continua ausente, e o presente fica com os bytes iguais; o conteúdo-sentinela dele
    não aparece em nenhuma saída."""
    config, stack_dir = _setup(tmp_path, env_file=env_file)
    env_path = stack_dir / ".env"
    before = env_path.read_bytes() if env_file else None
    outputs = []
    for command in (["up"], ["down"], ["status"], ["logs"], ["doctor"]):
        for json_mode in (False, True):
            if command == ["logs"] and json_mode:
                continue
            result, _ = _run(command, json=json_mode, stack=FakeStack(["stopped", "starting", "healthy"]), http=client,
                             cwd=stack_dir, config=config)
            outputs.append(result.stdout + result.stderr)
            if env_file:
                assert env_path.read_bytes() == before, command
            else:
                assert not env_path.exists(), command
    assert all(ENV_SENTINEL not in text for text in outputs)
    ok, _ = _run(["status"], stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert ok.exit_code == (0 if env_file else 1)


def test_admin_token_in_env_file_is_never_used(tmp_path, client) -> None:
    """Com `CQ_ADMIN_TOKEN=<sentinela>` no `.env` e o perfil local sem
    token, a folha do catálogo vai **sem** autenticação e sai com o 401 (1), e o `doctor` dá
    `api.access = failure` "sem token"; o sentinela fica fora de toda saída."""
    config, stack_dir = _setup(tmp_path, f'api_url = "{TEST_API_URL}"\n')
    with _recorded(client) as seen:
        leaf, _ = _run(["people", "list"], stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert (leaf.exit_code, leaf.stdout) == (1, "")
    assert leaf.stderr == f"Erro: {MESSAGES['unauthenticated']}\n"
    assert [r.headers.get("Authorization") for r in seen] == [None]
    doctor, _ = _run(["doctor"], json=True, stack=FakeStack(["healthy"]), http=client, cwd=stack_dir, config=config)
    assert doctor.exit_code == 1
    body = assert_cli_json("doctor", doctor.stdout)
    access = [c for c in body["checks"] if c["id"] == "api.access"]
    assert [(c["result"], c["message"]) for c in access] == [("failure", API_ACCESS_FAILURE_MESSAGES["no_token"])]
    assert body["profile"] == _local_view(token_set=False)
    for result in (leaf, doctor):
        output = result.stdout + result.stderr
        assert ENV_SENTINEL not in output and ENV_SENTINEL_TOKEN not in output


# --------------------------------------------------------------------------------------------------
# Nível 2 pelo CLI: o ComposeStack real sobre o FakeRunner


def test_level2_logs_are_cleaned(tmp_path, client) -> None:
    """`cq logs` pelo `ComposeStack` real sobre o `FakeRunner`, com
    `ESC[2K`, CSI, OSC, C0/C1 e bytes inválidos na amostra → a saída limpa, com U+FFFD, no texto e no `--json`; só
    argv da lista fechada."""
    config, stack_dir = _setup(tmp_path)
    runner = FakeRunner()
    text, _ = _run(["logs"], runner=runner, http=client, cwd=stack_dir, config=config)
    assert text.exit_code == 0, text.stderr
    assert text.stdout == "".join(f"{line}\n" for line in LOGS_CLEAN)
    as_json, _ = _run(["logs"], json=True, runner=runner, http=client, cwd=stack_dir, config=config)
    assert as_json.exit_code == 0, as_json.stderr
    assert assert_cli_json("logs", as_json.stdout) == {"lines": list(LOGS_CLEAN)}
    assert "\x1b" not in text.stdout + as_json.stdout
    assert_allowed(runner.calls, PROJECT, stack_dir)


def _level2_expected(kind: str) -> tuple[int, str]:
    if kind == "variable_missing":
        return 2, StackVariableMissing("CQ_DB_PASSWORD").message
    if kind == "variable_missing_unnamed":
        return 2, VARIABLE_MISSING_UNNAMED
    if kind in DAEMON_FAILURES or kind == DOCKER_MISSING:
        return 1, DockerUnavailable().message
    if kind in PORT_FAILURES:
        return 1, StackFailure("port_in_use").message
    if kind in BUILD_FAILURES:
        return 1, StackFailure("build_failed").message
    return 1, StackFailure("other").message


_LEVEL2_CASES = (
    [("up", kind) for kind in (*sorted(FAILURE_STDERR), DOCKER_MISSING)]
    + [(command, kind) for command in ("down", "logs")
       for kind in ("variable_missing", "variable_missing_unnamed", "daemon_stopped", "generic", DOCKER_MISSING)]
    + [("status", kind) for kind in ("variable_missing", "daemon_stopped", "daemon_stopped_windows", DOCKER_MISSING)]
)


@pytest.mark.parametrize(("command", "kind"), _LEVEL2_CASES, ids=[f"{c}-{k}" for c, k in _LEVEL2_CASES])
def test_level2_failures_fixed_lines_without_docker_text(tmp_path, client, command, kind) -> None:
    """`up`, `down`, `logs` e `status` pelo `ComposeStack`
    real sobre cada amostra de falha do `FakeRunner` (porta ocupada nas 4 formas, variável ausente, daemon parado,
    construção falha, genérica, `docker` ausente) → o código e a linha fixa da classe (no `status`, o
    relatório da pilha com o código dele); o `SENTINEL_SECRET` fora do stdout, do stderr e do `--json`;
    só argv da lista fechada."""
    config, stack_dir = _setup(tmp_path)
    samples: dict[str, Any] = dict(failure_samples(kind))
    if kind in PORT_FAILURES or kind in BUILD_FAILURES:
        samples.update({"ps": "stopped", "port": PORT_NONE})
    code, message = _level2_expected(kind)
    outputs = []
    for json_mode in (False, True):
        runner = FakeRunner(samples)
        result, _ = _run(LOCAL_COMMAND_ARGS[command], json=json_mode, runner=runner, http=client, cwd=stack_dir,
                         config=config)
        outputs.append(result.stdout + result.stderr)
        assert_allowed(runner.calls, PROJECT, stack_dir)
        if command == "status" and code == 1:
            assert result.exit_code == 1
            if json_mode:
                body = assert_cli_json("status", result.stdout)
                assert body["warnings"] == ["docker_unavailable"]
                assert {s["state"] for s in body["services"]} == {"unknown"}
            else:
                assert result.stdout != ""
            continue
        assert (result.exit_code, result.stdout) == (code, "")
        if not json_mode:
            _assert_last_line(result.stderr, message)
    assert all(SENTINEL_SECRET not in text for text in outputs)
    assert all(not re.search(r"Cannot connect|port is already allocated|failed to solve|interpolating", text)
               for text in outputs)
