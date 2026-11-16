"""Interface de operação da pilha local.


Dois níveis, para que os testes substituam o Docker sem subprocesso:
- **nível 1**, o `Protocol` `Stack` (estado, subir, parar, registros, versões), que os comandos locais
  e a parte local do `doctor` usam;
- **nível 2**, o `Protocol` `CommandRunner` (`run`/`stream` sobre um argv), que a implementação do
  Compose (`cq_client.stack_compose`) usa.

Os erros (`StackError` e subclasses) têm `exit_code` e `message` fixos e nunca
carregam o texto do Docker (o stderr é classificado, nunca repassado). As funções puras daqui
(descoberta do diretório, leitura do `ps`, limpeza dos registros, classificação do stderr, ambiente do
subprocesso) não rodam processo nenhum. Este módulo não importa `click` nem `subprocess`.

Tipos, `Protocol`s, erros, constantes, mensagens e as funções
puras ficam todos neste módulo.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from cq_client.profile import EffectiveProfile

# Os quatro serviços, os três com registros repassáveis (os do motor são
# sensíveis) e a versão mínima do Compose (aviso abaixo dela).
SERVICES = ("postgres", "api", "worker", "engine")
LOG_SERVICES = ("api", "worker", "postgres")
MIN_COMPOSE = (2, 27, 0)

# Estados e saúdes de um serviço (`unknown` = Docker indisponível, "não verificado").
ServiceStateName = Literal["running", "stopped", "absent", "unknown"]
ServiceHealth = Literal["healthy", "unhealthy", "starting", "none", "unknown"]
SERVICE_STATES = ("running", "stopped", "absent", "unknown")
SERVICE_HEALTHS = ("healthy", "unhealthy", "starting", "none", "unknown")

# O marcador do diretório da pilha (só existência, sem ler), relativo ao diretório candidato.
STACK_MARKERS = ("compose.yaml", "engine/Dockerfile", "server/pyproject.toml", "client/pyproject.toml")

# Arquivo de ambiente da pilha (só a existência é conferida) e a variável que aponta o
# diretório.
STACK_ENV_FILE = ".env"
STACK_DIR_ENV = "CQ_STACK_DIR"

# Só um nome com este formato vai à mensagem de variável ausente.
VARIABLE_NAME_PATTERN = re.compile(r"^CQ_[A-Z0-9_]+$")

# O que sai do ambiente do subprocesso e o que entra nele.
ENV_DROP_PREFIXES = ("COMPOSE_",)
ENV_DROP_NAMES = frozenset({"CQ_TOKEN", "CQ_API_URL", "CQ_CONFIG", "CQ_PROFILE", "CQ_STACK_DIR"})
ENV_FORCED = {"COMPOSE_ANSI": "never", "NO_COLOR": "1"}


# --------------------------------------------------------------------------------------------------
# Tipos
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceState:
    """Estado de um serviço: `service ∈ SERVICES`."""

    service: str
    state: str
    health: str


@dataclass(frozen=True)
class StackSnapshot:
    """O instantâneo da pilha: serviços, porta publicada da API e se a API do perfil é desta pilha."""

    services: tuple[ServiceState, ...]
    api_port: int | None
    api_owned: bool | None


@dataclass(frozen=True)
class RunResult:
    """Resultado de um comando do nível 2. O `repr` não mostra a saída (pode citar segredos)."""

    returncode: int
    stdout: bytes
    stderr: bytes

    def __repr__(self) -> str:
        return (f"RunResult(returncode={self.returncode!r}, stdout=<{len(self.stdout)} bytes>, "
                f"stderr=<{len(self.stderr)} bytes>)")


@runtime_checkable
class Stack(Protocol):
    """Nível 1."""

    def snapshot(self) -> StackSnapshot: ...

    def up(self, *, rebuild: bool) -> None: ...

    def down(self) -> None: ...

    def logs(self, services: Sequence[str], *, tail: int, follow: bool) -> Iterator[str]: ...

    def compose_version(self) -> tuple[int, int, int] | None: ...

    def engine_image_id(self) -> str | None: ...

    def docker_available(self) -> bool: ...


@runtime_checkable
class CommandRunner(Protocol):
    """Nível 2."""

    def run(self, argv: Sequence[str], *, timeout: float | None, env: Mapping[str, str]) -> RunResult: ...

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Iterator[bytes]: ...


# --------------------------------------------------------------------------------------------------
# Erros
# --------------------------------------------------------------------------------------------------


class StackError(Exception):
    """Falha da operação da pilha, com motivo fixo, sem texto do Docker. `exit_code` é o código de
    saída do comando local; `message`, a linha fixa do stderr."""

    exit_code: int = 1
    message: str = "O Docker Compose falhou ao operar a pilha."

    def __init__(self) -> None:
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(exit_code={self.exit_code!r})"


class DockerUnavailable(StackError):
    """Docker ausente, fora do `PATH` ou com o daemon parado."""

    exit_code = 1
    message = "O Docker não está disponível neste computador (instalado e em execução?)."


class StackEnvMissing(StackError):
    """O `.env` não existe no diretório da pilha."""

    exit_code = 2
    message = ("Arquivo de ambiente ausente no diretório da pilha: crie-o a partir do .env.example "
               "(veja docs/operacao/inicio-rapido.md).")


VARIABLE_MISSING_TEMPLATE = "Configuração da pilha incompleta: defina {name} no arquivo de ambiente."
# **[I]**: o nome fora de `^CQ_[A-Z0-9_]+$` é omitido, e o texto sem o nome é escolha da implementação.
VARIABLE_MISSING_UNNAMED = "Configuração da pilha incompleta: defina a variável ausente no arquivo de ambiente."


class StackVariableMissing(StackError):
    """Variável obrigatória ausente no `.env` (o Compose recusou a interpolação). `name` só fica
    quando casa `VARIABLE_NAME_PATTERN`; senão é `None`."""

    exit_code = 2

    def __init__(self, name: str | None) -> None:
        self.name = name if name is not None and VARIABLE_NAME_PATTERN.fullmatch(name) else None
        self.message = (VARIABLE_MISSING_TEMPLATE.format(name=self.name) if self.name
                        else VARIABLE_MISSING_UNNAMED)
        super().__init__()

    def __repr__(self) -> str:
        return f"StackVariableMissing(name={self.name!r}, exit_code={self.exit_code!r})"


StackFailureKind = Literal["port_in_use", "build_failed", "other"]

STACK_FAILURE_MESSAGES: Mapping[str, str] = {
    "port_in_use": "A porta da API já está em uso por outro processo ou projeto.",
    "build_failed": "A construção das imagens falhou; rode de novo com o Docker em ordem.",
    "other": "O Docker Compose falhou ao operar a pilha.",
}


class StackFailure(StackError):
    """O Compose falhou: `kind` ∈ `port_in_use`, `build_failed`, `other`."""

    exit_code = 1

    def __init__(self, kind: str) -> None:
        if kind not in STACK_FAILURE_MESSAGES:
            raise ValueError("tipo de falha da pilha desconhecido")
        self.kind = kind
        self.message = STACK_FAILURE_MESSAGES[kind]
        super().__init__()

    def __repr__(self) -> str:
        return f"StackFailure(kind={self.kind!r}, exit_code={self.exit_code!r})"


class StackTargetRefused(StackError):
    """`DOCKER_HOST` não local ou `DOCKER_CONTEXT` definida."""

    exit_code = 2
    message = ("O Docker aponta para um alvo que não é local (DOCKER_HOST ou DOCKER_CONTEXT); o CLI só "
               "opera a pilha deste computador.")


class StackDirNotFound(StackError):
    """Nem `stack_dir`, nem `CQ_STACK_DIR`, nem um diretório com `STACK_MARKERS`."""

    exit_code = 2
    message = ("Diretório da pilha não encontrado: defina stack_dir no perfil local ou CQ_STACK_DIR "
               "(veja docs/operacao/inicio-rapido.md).")


class RemoteProfileRefused(StackError):
    """`up`, `down` ou `logs` com o perfil remoto."""

    exit_code = 2
    message = "Este comando só opera a pilha local; o perfil em uso é remoto."


# --------------------------------------------------------------------------------------------------
# Funções puras (nenhuma roda processo)
# --------------------------------------------------------------------------------------------------

# Os esquemas do `DOCKER_HOST` que são o Docker deste computador.
_LOCAL_DOCKER_SCHEMES = ("unix://", "npipe://")


def check_docker_target(env: Mapping[str, str]) -> None:
    """Levanta `StackTargetRefused` com `DOCKER_HOST` não local ou `DOCKER_CONTEXT` definida."""
    # Vazias valem como ausentes; o valor nunca vai à mensagem (pode citar host e usuário).
    if env.get("DOCKER_CONTEXT", ""):
        raise StackTargetRefused()
    host = env.get("DOCKER_HOST", "")
    if host and not host.lower().startswith(_LOCAL_DOCKER_SCHEMES):
        raise StackTargetRefused()


def _configured_dir(value: Path, cwd: Path) -> Path:
    """O caminho configurado, com `~` expandido e, se relativo, sobre o diretório atual.
    **[I]** a base de um caminho relativo não é especificada; aqui é o diretório atual (o do shell)."""
    path = value.expanduser()
    return path if path.is_absolute() else Path(cwd) / path


def _has_markers(candidate: Path) -> bool:
    # Só a existência, sem ler.
    return all((candidate / marker).exists() for marker in STACK_MARKERS)


def locate_stack_dir(profile: EffectiveProfile, env: Mapping[str, str], cwd: Path) -> Path:
    """`stack_dir` do perfil > `CQ_STACK_DIR` > `cwd` ou o ancestral mais próximo com
    `STACK_MARKERS`; senão `StackDirNotFound`."""
    # O configurado (perfil ou variável; vazia = ausente) vale sozinho; se não existir, não cai
    # para o próximo, e a mensagem fixa não cita o caminho.
    configured: Path | None = profile.stack_dir
    if configured is None:
        from_env = env.get(STACK_DIR_ENV, "")
        configured = Path(from_env) if from_env else None
    if configured is not None:
        try:
            path = _configured_dir(configured, cwd)
            if path.is_dir():
                return path
        except (OSError, RuntimeError, ValueError):
            pass
        raise StackDirNotFound()
    try:
        start = Path(cwd).resolve()
        for candidate in (start, *start.parents):
            if _has_markers(candidate):
                return candidate
    except (OSError, RuntimeError, ValueError):
        pass
    raise StackDirNotFound()


# `State` do `ps --format json` → estado; `Health` → saúde.
_PS_HEALTHS = {"healthy": "healthy", "unhealthy": "unhealthy", "starting": "starting", "": "none"}
# O mesmo serviço em mais de uma linha (réplicas): o rodando vence o parado.
_STATE_RANK = {"running": 2, "stopped": 1}


def _ps_objects(stdout: bytes) -> list[object]:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        if text.startswith("["):
            parsed = json.loads(text)
            return list(parsed) if isinstance(parsed, list) else []
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        # Saída ilegível: a falha genérica fixa, sem repassar o texto do Docker.
        raise StackFailure("other") from None


def _map_entry(entry: Mapping[str, object]) -> tuple[str, str]:
    raw_state = entry.get("State")
    raw_health = entry.get("Health")
    state_text = raw_state.strip().lower() if isinstance(raw_state, str) else ""
    health_text = raw_health.strip().lower() if isinstance(raw_health, str) else ""
    if state_text == "restarting":
        # Laço de reinício: parado e com falha.
        return "stopped", "unhealthy"
    # `exited`, `created`, `dead`, `paused`, `removing` → parado; um estado fora da lista também (nunca
    # conta como "ok").
    state = "running" if state_text == "running" else "stopped"
    return state, _PS_HEALTHS.get(health_text, "unknown")


def parse_ps(stdout: bytes) -> tuple[ServiceState, ...]:
    """A saída de `ps --all --format json` (um JSON por linha ou um array; vazio aceito), mapeada
    nos estados e saúdes da pilha."""
    found: dict[str, tuple[str, str]] = {}
    for entry in _ps_objects(stdout):
        if not isinstance(entry, Mapping):
            continue
        service = entry.get("Service")
        if not isinstance(service, str) or service not in SERVICES:
            continue  # serviço estranho ao projeto: ignorado
        mapped = _map_entry(entry)
        current = found.get(service)
        if current is None or _STATE_RANK[mapped[0]] > _STATE_RANK[current[0]]:
            found[service] = mapped
    # Só `service`, `state` e `health` saem (nunca `Labels` nem outro campo); sem linha → `absent`.
    return tuple(ServiceState(service, *found.get(service, ("absent", "none"))) for service in SERVICES)


_VERSION_PATTERN = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def parse_compose_version(text: str) -> tuple[int, int, int] | None:
    """A versão de `docker compose version --short`, ou `None` se ilegível."""
    match = _VERSION_PATTERN.match(text.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


# `<host>:<porta>`, com o host IPv6 entre colchetes.
_PORT_PATTERN = re.compile(r"(?:\[[0-9A-Fa-f:.%]*\]|[^\s:\[\]]+):(\d{1,5})")


def parse_port(stdout: bytes) -> int | None:
    """A porta publicada por `P port api 8000`, ou `None`."""
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        match = _PORT_PATTERN.fullmatch(line.strip())
        if match is not None and 0 < int(match.group(1)) < 65536:
            return int(match.group(1))
    return None


# As sequências de controle de terminal, nas formas de 7 bits (ESC) e de 8 bits (C1).
# OSC: até BEL, ST (`ESC \` ou U+009C) ou o fim da linha; DCS, SOS, PM e APC: até ST ou o fim; CSI:
# parâmetros, intermediários e o final; os demais escapes: intermediários e o final.
_ESCAPE_PATTERN = re.compile(
    "(?:\x1b\\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\\\|\x9c)?"
    "|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\\\|\x9c)?"
    "|(?:\x1b\\[|\x9b)[0-?]*[ -/]*[@-~]?"
    "|\x1b[ -/]*[0-~]?"
)
# O que sobra: os controles C0 (menos o tab), o DEL e os C1.
_CONTROL_PATTERN = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f]")


def clean_log_line(raw: bytes) -> str:
    """Uma linha de registro sem sequências de controle e com os bytes inválidos trocados."""
    text = raw.decode("utf-8", errors="replace")
    return _CONTROL_PATTERN.sub("", _ESCAPE_PATTERN.sub("", text))


# Classificação do stderr, na ordem (**[I]** padrões a conferir). O
# `docker` fora do `PATH` é do executor (`SubprocessRunner`), não do stderr.
_VARIABLE_PATTERN = re.compile(r"required variable (\S*) is missing a value")
_UNAVAILABLE_PATTERNS = ("cannot connect to the docker daemon", "error during connect")
_PORT_PATTERNS = ("port is already allocated", "address already in use", "ports are not available",
                  "only one usage of each socket address")
_BUILD_PATTERNS = ("failed to solve", "failed to build")


def classify_failure(stderr: bytes) -> StackError:
    """O erro fixo correspondente ao stderr do Docker (classificação do stderr)."""
    text = stderr.decode("utf-8", errors="replace")
    match = _VARIABLE_PATTERN.search(text)
    if match is not None:
        # O nome só fica se casar `VARIABLE_NAME_PATTERN` (o construtor confere).
        return StackVariableMissing(match.group(1))
    lowered = text.lower()
    if any(pattern in lowered for pattern in _UNAVAILABLE_PATTERNS):
        return DockerUnavailable()
    if any(pattern in lowered for pattern in _PORT_PATTERNS):
        return StackFailure("port_in_use")
    if any(pattern in lowered for pattern in _BUILD_PATTERNS):
        return StackFailure("build_failed")
    return StackFailure("other")


def docker_env(env: Mapping[str, str]) -> dict[str, str]:
    """O ambiente do subprocesso: sem `COMPOSE_*` e sem as variáveis do CLI, com
    `ENV_FORCED`."""
    # Nomes comparados em maiúsculas: no Windows o ambiente não distingue caixa.
    forced = {name.upper() for name in ENV_FORCED}
    result = {
        name: value for name, value in env.items()
        if not name.upper().startswith(ENV_DROP_PREFIXES)
        and name.upper() not in ENV_DROP_NAMES and name.upper() not in forced
    }
    result.update(ENV_FORCED)
    return result


def compose_prefix(project: str, stack_dir: Path) -> list[str]:
    """O prefixo `docker compose -p <projeto> -f <dir>/compose.yaml --env-file <dir>/.env`."""
    return ["docker", "compose", "-p", project, "-f", str(stack_dir / "compose.yaml"), "--env-file",
            str(stack_dir / STACK_ENV_FILE)]
