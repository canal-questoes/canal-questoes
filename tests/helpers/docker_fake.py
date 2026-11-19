"""Dublê do executor de comandos do Docker, nível 2.


`FakeRunner(samples=…, version="2.26.1" | "2.27.0")` cumpre o `Protocol` `cq_client.stack.CommandRunner`:
registra cada chamada (`calls`: o argv, o ambiente recebido, o método e o `timeout`) e responde por regra
de argv, com amostras no formato do Compose. Nenhum processo roda.

Operações reconhecidas (a chave de `samples`), pela lista fechada do `ComposeStack`:
- `docker_version`: `docker version --format {{.Server.Version}}`;
- `compose_version`: `docker compose version --short` (a resposta sai de `version`);
- `image_inspect`: `docker image inspect --format {{.Id}} <projeto>-engine`;
- `ps`, `port`, `up`, `build`, `down`, `logs`: o subcomando depois do prefixo
  `docker compose -p <projeto> -f <dir>/compose.yaml --env-file <dir>/.env` (`up` cobre `up -d
  --no-recreate` e `up -d`).
Qualquer outro argv (inclusive os proibidos) responde com o código 125 e o stderr genérico, e o
`assert_allowed` o acusa.

Valor de cada amostra (o padrão é a pilha saudável, `HEALTHY_SAMPLES`):
- `str`: o nome de uma amostra da operação (`PS_SAMPLES`, `PORT_SAMPLES`, `LOGS_SAMPLES`,
  `IMAGE_SAMPLES`, `"ok"`) ou de uma falha (`FAILURES`: código 1 e o stderr da falha, que leva o
  `SENTINEL_SECRET`); `"docker_missing"` levanta `DockerUnavailable` (o `docker` fora do `PATH`, como o
  `SubprocessRunner` real faz);
- `int` (só `port`): a porta publicada, como `127.0.0.1:<porta>`;
- `bytes`: o stdout, com código 0 e o stderr de progresso;
- `RunResult`: devolvido como está;
- uma exceção (instância ou classe): levantada (ex.: `KeyboardInterrupt` no `logs --follow`);
- `list`: uma resposta por chamada, na ordem; a última se repete.

`failure_samples(kind)` dá o mapa realista de uma falha (quais operações ela atinge);
`ps_entry`/`ps_output` montam saídas do `ps` sob medida.

`stream(argv, *, env)` devolve as linhas do stdout da mesma regra, uma a uma (cada uma com `\\n`); com
código ≠ 0, depois do stdout, levanta `subprocess.CalledProcessError(returncode, argv, stderr=…)` (a
convenção do dublê para a falha de um comando em fluxo; **[I]** escolha deste dublê).

Segredo-sentinela: **toda** amostra de stderr deste módulo, inclusive o progresso de
sucesso, leva `SENTINEL_SECRET`; o `Labels` das amostras do `ps` também (caminho local), e nenhum dos dois
pode chegar ao stdout, ao stderr, ao `repr` de uma exceção nem ao `--json` do CLI.

`assert_allowed(calls, project, stack_dir)` confere o prefixo, a lista fechada do `ComposeStack`, as
proibições e o ambiente do subprocesso. `write_fake_docker(dir)` grava um `docker` falso
executável (`docker.cmd` no Windows) que registra cada chamada em `<dir>/docker-calls.jsonl` e responde com
as mesmas amostras, para o pacote instalado; `read_fake_docker_calls(dir)` lê o registro.

Importa do cliente só os tipos de `cq_client.stack`. As amostras foram escritas a partir da documentação
e do código do Compose 2.26.1 e ≥ 2.27, sem captura real (**[I]**).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from cq_client.stack import DockerUnavailable, RunResult

from tests.helpers.stack_fake import ENGINE_IMAGE_ID, OTHER_API_PORT, TEST_API_PORT

REPO_ROOT = Path(__file__).resolve().parents[2]

# O segredo-sentinela: nenhuma saída do sistema pode contê-lo.
SENTINEL_SECRET = "cq-sentinela-docker-7Hq2xV9pLm4R"

DEFAULT_VERSION = "2.27.0"
SUPPORTED_VERSIONS = ("2.26.1", "2.27.0")
DOCKER_SERVER_VERSION = "27.0.3"

OPERATIONS = ("docker_version", "compose_version", "image_inspect", "ps", "port", "up", "build", "down",
              "logs")
COMPOSE_OPERATIONS = ("ps", "port", "up", "build", "down", "logs")

# Linha do sentinela acrescentada a todo stderr (o Compose pode citar trechos do `.env`).
_SENTINEL_LINE = f"env file contém CQ_DB_PASSWORD={SENTINEL_SECRET}\n"


def _stderr(text: str) -> bytes:
    return (text + "\n" + _SENTINEL_LINE).encode("utf-8")


# --------------------------------------------------------------------------------------------------
# Falhas (stderr do Docker, pela classificação de `stack.classify_failure`). Cada uma isola um padrão.
# --------------------------------------------------------------------------------------------------

FAILURE_STDERR: dict[str, bytes] = {
    # variável obrigatória ausente (com nome no padrão ^CQ_[A-Z0-9_]+$ e fora dele)
    "variable_missing": _stderr(
        "error while interpolating services.postgres.environment.POSTGRES_PASSWORD: "
        "required variable CQ_DB_PASSWORD is missing a value: defina CQ_DB_PASSWORD no .env"),
    "variable_missing_unnamed": _stderr(
        "error while interpolating services.postgres.environment.POSTGRES_PASSWORD: "
        "required variable POSTGRES_PASSWORD is missing a value: defina no .env"),
    # Docker indisponível (daemon parado: Linux e Windows)
    "daemon_stopped": _stderr(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"),
    "daemon_stopped_windows": _stderr(
        "error during connect: this error may indicate that the docker daemon is not running: Get "
        "\"http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.46/version\": open "
        "//./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."),
    # porta ocupada: as 4 formas que `stack.classify_failure` reconhece, uma por amostra
    "port_allocated": _stderr(
        "Error response from daemon: driver failed programming external connectivity on endpoint "
        "cq-api-1 (5d1c0a): Bind for 127.0.0.1:8000 failed: port is already allocated"),
    "port_address_in_use": _stderr(
        "Error response from daemon: driver failed programming external connectivity on endpoint "
        "cq-api-1 (5d1c0a): listen tcp4 127.0.0.1:8000: bind: address already in use"),
    "port_not_available": _stderr(
        "Error response from daemon: Ports are not available: exposing port TCP 127.0.0.1:8000 -> "
        "0.0.0.0:0: listen tcp4 127.0.0.1:8000: bind: An attempt was made to access a socket in a way "
        "forbidden by its access permissions."),
    "port_only_one_usage": _stderr(
        "Error response from daemon: listen tcp 127.0.0.1:8000: bind: Only one usage of each socket "
        "address (protocol/network address/port) is normally permitted."),
    # construção falhou (as duas formas)
    "build_failed": _stderr(
        "failed to solve: process \"/bin/sh -c pip install --no-cache-dir .\" did not complete "
        "successfully: exit code: 1"),
    "build_failed_target": _stderr("target worker: failed to build: exit code: 1"),
    # outra falha (nenhum padrão acima)
    "generic": _stderr("service \"worker\" refers to undefined network interna: invalid compose project"),
}

# As formas de cada classe (para os testes iterarem).
PORT_FAILURES = ("port_allocated", "port_address_in_use", "port_not_available", "port_only_one_usage")
DAEMON_FAILURES = ("daemon_stopped", "daemon_stopped_windows")
BUILD_FAILURES = ("build_failed", "build_failed_target")
VARIABLE_FAILURES = ("variable_missing", "variable_missing_unnamed")
DOCKER_MISSING = "docker_missing"
FAILURES = tuple(FAILURE_STDERR) + (DOCKER_MISSING,)

# Quais operações cada falha atinge numa pilha real (o resto responde como saudável).
_FAILURE_SCOPE: dict[str, tuple[str, ...]] = {
    **{name: COMPOSE_OPERATIONS for name in VARIABLE_FAILURES},
    # `compose version --short` é só do cliente: responde mesmo com o daemon parado.
    **{name: ("docker_version", "image_inspect") + COMPOSE_OPERATIONS for name in DAEMON_FAILURES},
    **{name: ("up",) for name in PORT_FAILURES},
    **{name: ("up", "build") for name in BUILD_FAILURES},
    "generic": COMPOSE_OPERATIONS,
    DOCKER_MISSING: OPERATIONS,
}


def failure_samples(kind: str) -> dict[str, str]:
    """O mapa `samples` realista da falha `kind` (as operações que ela atinge numa pilha real)."""
    if kind not in _FAILURE_SCOPE:
        raise ValueError(f"falha desconhecida: {kind}")
    return {op: kind for op in _FAILURE_SCOPE[kind]}


# --------------------------------------------------------------------------------------------------
# `ps --all --format json`
# --------------------------------------------------------------------------------------------------

_IMAGES = {"postgres": "postgres:16-alpine", "api": "{p}-api", "worker": "{p}-worker",
           "engine": "{p}-engine"}
_STATUS = {"running": "Up 2 minutes", "exited": "Exited (1) 1 minute ago", "created": "Created",
           "dead": "Dead", "paused": "Up 2 minutes (Paused)", "removing": "Removal In Progress",
           "restarting": "Restarting (1) 5 seconds ago"}


def ps_entry(service: str, state: str, health: str = "", *, project: str = "cq") -> dict[str, Any]:
    """Um objeto do `ps --format json` como o Compose o emite (com `Labels`, que leva um caminho local
    com o `SENTINEL_SECRET`, e `Publishers`)."""
    status = _STATUS.get(state, state)
    if health and state == "running":
        status = f"{status} ({health})"
    publishers: list[dict[str, Any]] = []
    ports = ""
    if service == "api" and state == "running":
        publishers = [{"URL": "127.0.0.1", "TargetPort": 8000, "PublishedPort": TEST_API_PORT,
                       "Protocol": "tcp"}]
        ports = f"127.0.0.1:{TEST_API_PORT}->8000/tcp"
    return {
        "Command": "\"/entrypoint\"",
        "CreatedAt": "2031-01-01 00:00:00 +0000 UTC",
        "ExitCode": 1 if state in ("exited", "dead") else 0,
        "Health": health,
        "ID": (service.encode().hex() + "0" * 64)[:64],
        "Image": _IMAGES.get(service, f"{service}:latest").format(p=project),
        "Labels": (f"com.docker.compose.project={project},com.docker.compose.service={service},"
                   f"com.docker.compose.project.working_dir=/home/operador/{SENTINEL_SECRET}/canal-questoes,"
                   f"com.docker.compose.project.config_files=/home/operador/{SENTINEL_SECRET}/compose.yaml"),
        "LocalVolumes": "1" if service == "postgres" else "0",
        "Mounts": "",
        "Name": f"{project}-{service}-1",
        "Names": f"{project}-{service}-1",
        "Networks": f"{project}_default",
        "Ports": ports,
        "Project": project,
        "Publishers": publishers,
        "RunningFor": "2 minutes ago",
        "Service": service,
        "Size": "0B",
        "State": state,
        "Status": status,
    }


def ps_output(entries: Sequence[Mapping[str, Any]], *, fmt: str = "lines") -> bytes:
    """A saída do `ps`: um JSON por linha (Compose ≥ 2.21, `fmt="lines"`) ou um array (`fmt="array"`)."""
    if fmt == "lines":
        return b"".join(json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\n" for e in entries)
    if fmt == "array":
        return json.dumps(list(entries), ensure_ascii=False).encode("utf-8") + b"\n"
    raise ValueError("fmt deve ser 'lines' ou 'array'")


# Cada amostra: (lista de (serviço, State, Health), formato). Serviço ausente = sem linha.
_PS_SPECS: dict[str, tuple[tuple[tuple[str, str, str], ...], str]] = {
    "empty": ((), "lines"),
    # postgres rodando, worker `exited`; api e engine ausentes da saída
    "partial": ((("postgres", "running", "healthy"), ("worker", "exited", "")), "lines"),
    # os 4 serviços, o worker sem `Health` (sem healthcheck)
    "healthy": ((("postgres", "running", "healthy"), ("api", "running", "healthy"),
                 ("worker", "running", ""), ("engine", "running", "healthy")), "lines"),
    "healthy_array": ((("postgres", "running", "healthy"), ("api", "running", "healthy"),
                       ("worker", "running", ""), ("engine", "running", "healthy")), "array"),
    # um serviço estranho ao projeto (ignorado pelo `parse_ps`)
    "foreign_service": ((("postgres", "running", "healthy"), ("api", "running", "healthy"),
                         ("worker", "running", ""), ("engine", "running", "healthy"),
                         ("adminer", "running", "")), "lines"),
    # subindo: a saúde ainda `starting`
    "starting": ((("postgres", "running", "healthy"), ("api", "running", "starting"),
                  ("worker", "running", ""), ("engine", "running", "starting")), "lines"),
    # com falha: a api `unhealthy`
    "unhealthy": ((("postgres", "running", "healthy"), ("api", "running", "unhealthy"),
                   ("worker", "running", ""), ("engine", "running", "healthy")), "lines"),
    # laço de reinício: a api `restarting` (→ parado, com falha), sem `Health`
    "restart_loop": ((("postgres", "running", "healthy"), ("api", "restarting", ""),
                      ("worker", "running", ""), ("engine", "running", "healthy")), "lines"),
    # parados (depois de `stop`): todos `exited`
    "stopped": ((("postgres", "exited", ""), ("api", "exited", ""), ("worker", "exited", ""),
                 ("engine", "exited", "")), "lines"),
    # os demais estados que contam como `stopped`
    "stopped_states": ((("postgres", "created", ""), ("api", "dead", ""), ("worker", "paused", ""),
                        ("engine", "removing", "")), "lines"),
}
PS_SAMPLES = tuple(_PS_SPECS)


def ps_sample(name: str, *, project: str = "cq") -> bytes:
    """A amostra `name` de `PS_SAMPLES` para o projeto `project`."""
    entries, fmt = _PS_SPECS[name]
    return ps_output([ps_entry(s, st, h, project=project) for s, st, h in entries], fmt=fmt)


# --------------------------------------------------------------------------------------------------
# `port api 8000`, imagem do motor, registros
# --------------------------------------------------------------------------------------------------

PORT_SAMPLES: dict[str, bytes] = {
    "loopback": f"127.0.0.1:{TEST_API_PORT}\n".encode(),
    "any_8000": b"0.0.0.0:8000\n",
    "other": f"0.0.0.0:{OTHER_API_PORT}\n".encode(),
}
# Sem a api publicada: o Compose sai com 1 (sem stdout).
PORT_NONE = "none"
_PORT_NONE_STDERR = _stderr("no container found for api_1")

IMAGE_SAMPLES = ("present", "absent")
_IMAGE_ABSENT_STDERR = _stderr("Error response from daemon: No such image: cq-engine")

# Registros com `ESC[2K` (Compose 2.26.1, mesmo com `--no-color`), CSI, OSC (BEL e ST), C0, C1, CR e
# bytes inválidos; `LOGS_CLEAN` é o resultado esperado de `clean_log_line` linha a linha, sem o `\n`.
LOGS_RAW = (
    b"\x1b[2Kapi-1       | {\"level\": \"INFO\", \"msg\": \"API no ar\"}\n"
    b"\x1b[2Kworker-1    | \x1b[31mvermelho\x1b[0m e \x1b[1;32mnormal\x1b[0m\x1b[?25l\n"
    b"\x1b[2Kpostgres-1  | \x1b]0;titulo\x07pronto\n"
    b"\x1b[2Kapi-1       | link \x1b]8;;http://127.0.0.1/\x1b\\aqui\x1b]8;;\x1b\\ fim\n"
    b"\x1b[2Kworker-1    | sino\x07 nulo\x00 c1\xc2\x84 c1b\xc2\x8d fim\n"
    b"\x1b[2Kpostgres-1  | bytes \xff\xfe inv\xc3\x28lido\n"
    b"\x1b[2Kapi-1       | col1\tcol2\r\n"
)
LOGS_CLEAN = (
    "api-1       | {\"level\": \"INFO\", \"msg\": \"API no ar\"}",
    "worker-1    | vermelho e normal",
    "postgres-1  | pronto",
    "api-1       | link aqui fim",
    "worker-1    | sino nulo c1 c1b fim",
    "postgres-1  | bytes �� inv�(lido",
    "api-1       | col1\tcol2",
)
LOGS_SAMPLES: dict[str, bytes] = {"default": LOGS_RAW, "empty": b""}

# Progresso de sucesso no stderr (também com o sentinela; nunca repassado).
_PROGRESS_STDERR = {
    "up": _stderr(" Container cq-postgres-1  Started\n Container cq-api-1  Started"),
    "build": _stderr("#1 [api internal] load build definition from Dockerfile\n#1 DONE 0.0s"),
    "down": _stderr(" Container cq-api-1  Removed\n Network cq_default  Removed"),
}
_GENERIC_STDERR = FAILURE_STDERR["generic"]

HEALTHY_SAMPLES: dict[str, Any] = {
    "docker_version": "ok",
    "image_inspect": "present",
    "ps": "healthy",
    "port": "loopback",
    "up": "ok",
    "build": "ok",
    "down": "ok",
    "logs": "default",
}


# --------------------------------------------------------------------------------------------------
# Classificação do argv
# --------------------------------------------------------------------------------------------------

_PREFIX_OPTIONS = {"-p", "-f", "--env-file"}


def _compose_rest(argv: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """As opções do prefixo (`-p`, `-f`, `--env-file`) e o resto depois delas."""
    options: dict[str, str] = {}
    i = 2
    while i < len(argv) and argv[i] in _PREFIX_OPTIONS and i + 1 < len(argv):
        options[argv[i]] = argv[i + 1]
        i += 2
    return options, list(argv[i:])


def classify_argv(argv: Sequence[str]) -> str | None:
    """A operação reconhecida do argv (`OPERATIONS`), ou `None` (argv fora da lista)."""
    argv = list(argv)
    if len(argv) < 2 or argv[0] != "docker":
        return None
    if argv[1:] == ["version", "--format", "{{.Server.Version}}"]:
        return "docker_version"
    if argv[1:] == ["compose", "version", "--short"]:
        return "compose_version"
    if argv[1:5] == ["image", "inspect", "--format", "{{.Id}}"] and len(argv) == 6:
        return "image_inspect"
    if argv[1] == "compose":
        _, rest = _compose_rest(argv)
        if rest and rest[0] in COMPOSE_OPERATIONS:
            return rest[0]
    return None


def _project_of(argv: Sequence[str]) -> str:
    options, _ = _compose_rest(argv)
    return options.get("-p", "cq")


# --------------------------------------------------------------------------------------------------
# O dublê
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerCall:
    """Uma chamada registrada: o argv, o ambiente recebido, `run` ou `stream`, e o `timeout`."""

    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False)
    method: str = "run"
    timeout: float | None = None

    @property
    def operation(self) -> str | None:
        return classify_argv(self.argv)


class FakeRunner:
    """O `CommandRunner` dublê (ver o docstring do módulo)."""

    def __init__(self, samples: Mapping[str, Any] | None = None, version: str = DEFAULT_VERSION) -> None:
        if version not in SUPPORTED_VERSIONS:
            raise ValueError(f"versão do Compose sem amostra: {version}")
        unknown = set(samples or {}) - set(OPERATIONS)
        if unknown:
            raise ValueError(f"operações desconhecidas em samples: {sorted(unknown)}")
        self.version = version
        self.samples: dict[str, Any] = {**HEALTHY_SAMPLES, **dict(samples or {})}
        self.calls: list[RunnerCall] = []
        self._served: dict[str, int] = {}

    # -- configuração ----------------------------------------------------------------------------

    def set_sample(self, operation: str, value: Any) -> None:
        """Troca a amostra de uma operação (e recomeça a sequência dela, se for lista)."""
        if operation not in OPERATIONS:
            raise ValueError(f"operação desconhecida: {operation}")
        self.samples[operation] = value
        self._served.pop(operation, None)

    def argvs(self) -> list[list[str]]:
        """Os argv registrados, na ordem."""
        return [list(c.argv) for c in self.calls]

    def operations(self) -> list[str | None]:
        """As operações registradas, na ordem (`None` para argv fora da lista)."""
        return [c.operation for c in self.calls]

    # -- Protocol CommandRunner ----------------------------------------------------------------

    def run(self, argv: Sequence[str], *, timeout: float | None, env: Mapping[str, str]) -> RunResult:
        self.calls.append(RunnerCall(tuple(argv), dict(env), "run", timeout))
        return self._respond(list(argv))

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Iterator[bytes]:
        self.calls.append(RunnerCall(tuple(argv), dict(env), "stream", None))
        argv = list(argv)
        result = self._respond(argv)  # exceções (DockerUnavailable, KeyboardInterrupt) já aqui
        return self._lines(argv, result)

    @staticmethod
    def _lines(argv: list[str], result: RunResult) -> Iterator[bytes]:
        for line in result.stdout.splitlines(keepends=True):
            yield line
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, argv, output=None, stderr=result.stderr)

    # -- respostas -----------------------------------------------------------------------------

    def _next_value(self, operation: str) -> Any:
        value = self.samples[operation] if operation in self.samples else "ok"
        if isinstance(value, list):
            if not value:
                raise ValueError(f"sequência vazia para {operation}")
            index = self._served.get(operation, 0)
            self._served[operation] = index + 1
            return value[min(index, len(value) - 1)]
        return value

    def _respond(self, argv: list[str]) -> RunResult:
        operation = classify_argv(argv)
        if operation is None:
            return RunResult(125, b"", _GENERIC_STDERR)
        if operation == "compose_version":
            value = self.samples.get("compose_version", "ok")
            if value == "ok":
                return RunResult(0, f"{self.version}\n".encode(), b"")
        value = self._next_value(operation)
        return self._resolve(operation, value, argv)

    def _resolve(self, operation: str, value: Any, argv: list[str]) -> RunResult:
        if isinstance(value, BaseException) or (isinstance(value, type) and issubclass(value, BaseException)):
            raise value
        if isinstance(value, RunResult):
            return value
        if isinstance(value, bytes):
            return RunResult(0, value, _PROGRESS_STDERR.get(operation, b""))
        if isinstance(value, int) and not isinstance(value, bool):
            if operation != "port":
                raise ValueError("amostra inteira só vale para port")
            return RunResult(0, f"127.0.0.1:{value}\n".encode(), b"")
        if not isinstance(value, str):
            raise TypeError(f"amostra inválida para {operation}: {type(value).__name__}")
        if value == DOCKER_MISSING:
            raise DockerUnavailable()
        if value in FAILURE_STDERR:
            return RunResult(1, b"", FAILURE_STDERR[value])
        return self._named(operation, value, argv)

    def _named(self, operation: str, name: str, argv: list[str]) -> RunResult:
        if operation == "docker_version" and name == "ok":
            return RunResult(0, f"{DOCKER_SERVER_VERSION}\n".encode(), b"")
        if operation == "compose_version" and name == "ok":
            return RunResult(0, f"{self.version}\n".encode(), b"")
        if operation == "image_inspect":
            if name == "present":
                return RunResult(0, f"{ENGINE_IMAGE_ID}\n".encode(), b"")
            if name == "absent":
                return RunResult(1, b"", _IMAGE_ABSENT_STDERR)
        if operation == "ps" and name in _PS_SPECS:
            return RunResult(0, ps_sample(name, project=_project_of(argv)), b"")
        if operation == "port":
            if name in PORT_SAMPLES:
                return RunResult(0, PORT_SAMPLES[name], b"")
            if name == PORT_NONE:
                return RunResult(1, b"", _PORT_NONE_STDERR)
        if operation == "logs" and name in LOGS_SAMPLES:
            return RunResult(0, LOGS_SAMPLES[name], b"")
        if operation in ("up", "build", "down") and name == "ok":
            return RunResult(0, b"", _PROGRESS_STDERR[operation])
        raise ValueError(f"amostra desconhecida para {operation}: {name}")


# --------------------------------------------------------------------------------------------------
# Conferência do argv
# --------------------------------------------------------------------------------------------------

_FORBIDDEN_TOKENS = ("config", "exec", "run", "rm", "env", "-v", "--volumes", "--rmi")
_LOG_SERVICES = {"api", "worker", "postgres"}
_ENV_DROPPED = ("CQ_TOKEN", "CQ_API_URL", "CQ_CONFIG", "CQ_PROFILE", "CQ_STACK_DIR")


def _same_path(value: str, expected: PurePath) -> bool:
    # A tradução de caminhos do Windows pode usar `pathlib` (barra invertida).
    return Path(value) == Path(expected)


def _check_compose(argv: list[str], project: str, stack_dir: Path) -> str | None:
    if argv[1:4] != ["compose", "-p", project] or len(argv) < 9:
        return "prefixo do Compose diferente de 'docker compose -p <projeto> -f … --env-file …'"
    if argv[4] != "-f" or not _same_path(argv[5], stack_dir / "compose.yaml"):
        return "-f diferente de <dir>/compose.yaml"
    if argv[6] != "--env-file" or not _same_path(argv[7], stack_dir / ".env"):
        return "--env-file diferente de <dir>/.env"
    rest = argv[8:]
    if rest in (["ps", "--all", "--format", "json"], ["port", "api", "8000"], ["up", "-d", "--no-recreate"],
                ["build", "api", "worker"], ["up", "-d"], ["down"]):
        return None
    if rest[:3] == ["logs", "--no-color", "--tail"] and len(rest) >= 5 and rest[3].isdigit():
        services = rest[4:]
        if services and services[0] == "--follow":
            services = services[1:]
        if "engine" in services:
            return "logs com engine"
        if services and set(services) <= _LOG_SERVICES and len(set(services)) == len(services):
            return None
        return "logs sem a lista explícita de serviços ⊆ {api, worker, postgres}"
    return "subcomando fora da lista fechada"


def _check_env(env: Mapping[str, str]) -> str | None:
    dropped = [name for name in _ENV_DROPPED if name in env]
    if dropped:
        return f"ambiente com variáveis do cliente: {dropped}"
    compose = sorted(k for k in env if k.upper().startswith("COMPOSE_") and k != "COMPOSE_ANSI")
    if compose:
        return f"ambiente com COMPOSE_* herdadas: {compose}"
    if env.get("COMPOSE_ANSI") != "never" or env.get("NO_COLOR") != "1":
        return "ambiente sem COMPOSE_ANSI=never e NO_COLOR=1"
    return None


def assert_allowed(calls: Sequence[RunnerCall | Sequence[str]], project: str, stack_dir: Path) -> None:
    """Toda chamada está na lista fechada do `ComposeStack`, com o prefixo exato do projeto e do diretório;
    nenhum proibido (`config`, `exec`, `run`, `rm`, `env`, `inspect` fora da imagem, `-v`, `--volumes`,
    `--rmi`, `logs` com `engine`); `up -d` só depois de `build api worker`; e, nas chamadas com o ambiente
    registrado, o ambiente esperado do subprocesso."""
    problems: list[str] = []
    built = False
    for index, call in enumerate(calls):
        argv = list(call.argv) if isinstance(call, RunnerCall) else list(call)
        env = call.env if isinstance(call, RunnerCall) else None
        problem: str | None = None
        forbidden = [t for t in argv[1:] if t in _FORBIDDEN_TOKENS]
        if not argv or argv[0] != "docker":
            problem = "argv não começa por 'docker'"
        elif forbidden:
            problem = f"argv com proibido: {forbidden}"
        elif "inspect" in argv and argv[1:3] != ["image", "inspect"]:
            problem = "inspect fora da linha da imagem do motor"
        elif argv[1:] == ["version", "--format", "{{.Server.Version}}"]:
            problem = None
        elif argv[1:] == ["compose", "version", "--short"]:
            problem = None
        elif argv[1:3] == ["image", "inspect"]:
            if argv[1:] != ["image", "inspect", "--format", "{{.Id}}", f"{project}-engine"]:
                problem = "image inspect fora da forma permitida"
        elif len(argv) > 1 and argv[1] == "compose":
            problem = _check_compose(argv, project, stack_dir)
            rest = argv[8:]
            if problem is None and rest == ["build", "api", "worker"]:
                built = True
            elif problem is None and rest == ["up", "-d"] and not built:
                problem = "'up -d' sem 'build api worker' antes"
        else:
            problem = "argv fora da lista fechada"
        if problem is None and env is not None:
            problem = _check_env(env)
        if problem is not None:
            problems.append(f"chamada {index}: {problem}: {argv}")
    assert not problems, "chamadas ao Docker fora das regras do dublê:\n" + "\n".join(problems)


# --------------------------------------------------------------------------------------------------
# `docker` falso executável (pacote instalado)
# --------------------------------------------------------------------------------------------------

CALLS_FILE = "docker-calls.jsonl"
CONFIG_FILE = "fake-docker.json"
_RECORDED_ENV_PREFIXES = ("CQ_", "COMPOSE_", "DOCKER_")


def _json_sample(value: Any) -> Any:
    if isinstance(value, list):
        return [_json_sample(v) for v in value]
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    raise TypeError("o docker falso só aceita amostras por nome, porta ou lista delas")


def write_fake_docker(dir: Path, *, samples: Mapping[str, Any] | None = None,
                      version: str = DEFAULT_VERSION) -> Path:
    """Grava em `dir` um `docker` falso executável (`docker.cmd` no Windows, `docker` com `chmod +x` no
    POSIX) que roda com o Python dos testes, registra cada chamada (argv e as variáveis `CQ_*`,
    `COMPOSE_*`, `DOCKER_*` e `NO_COLOR` do ambiente) em `<dir>/docker-calls.jsonl` e responde com as
    amostras do `FakeRunner(samples, version)` (só por nome ou porta). Devolve o caminho do executável;
    o teste põe `dir` no início do `PATH`."""
    dir = Path(dir)
    dir.mkdir(parents=True, exist_ok=True)
    FakeRunner(samples, version)  # valida as amostras já aqui
    config = {"samples": {k: _json_sample(v) for k, v in (samples or {}).items()},
              "version": version, "calls": str(dir / CALLS_FILE)}
    config_path = dir / CONFIG_FILE
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    script = dir / "fake_docker.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "from tests.helpers.docker_fake import fake_docker_main\n"
        f"sys.exit(fake_docker_main({str(config_path)!r}, sys.argv[1:]))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        exe = dir / "docker.cmd"
        exe.write_text(f"@echo off\r\n\"{sys.executable}\" -I \"{script}\" %*\r\nexit /b %ERRORLEVEL%\r\n",
                       encoding="utf-8")
    else:
        exe = dir / "docker"
        exe.write_text(f"#!{sys.executable} -I\n" + script.read_text(encoding="utf-8"), encoding="utf-8")
        exe.chmod(0o755)
    return exe


def fake_docker_main(config_path: str, args: Sequence[str]) -> int:
    """Corpo do `docker` falso: registra a chamada e responde pela amostra."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    argv = ["docker", *args]
    env = {k: v for k, v in os.environ.items()
           if k.upper().startswith(_RECORDED_ENV_PREFIXES) or k.upper() == "NO_COLOR"}
    with open(config["calls"], "a", encoding="utf-8") as sink:
        sink.write(json.dumps({"argv": argv, "env": env}, ensure_ascii=False) + "\n")
    runner = FakeRunner(config["samples"], config["version"])
    try:
        result = runner.run(argv, timeout=None, env=dict(os.environ))
    except DockerUnavailable:
        sys.stderr.buffer.write(FAILURE_STDERR["daemon_stopped"])
        return 1
    except KeyboardInterrupt:
        return 130
    sys.stdout.buffer.write(result.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(result.stderr)
    sys.stderr.buffer.flush()
    return result.returncode


def read_fake_docker_calls(dir: Path) -> list[RunnerCall]:
    """As chamadas registradas pelo `docker` falso de `write_fake_docker(dir)`, na ordem. O ambiente
    registrado traz só as `CQ_*`, `COMPOSE_*`, `DOCKER_*` e `NO_COLOR`, as que o `assert_allowed` confere."""
    path = Path(dir) / CALLS_FILE
    if not path.exists():
        return []
    calls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            calls.append(RunnerCall(tuple(record["argv"]), dict(record["env"]), "run", None))
    return calls

