"""Porta de leitura do host para o `doctor` local: a montagem do Postgres e o disco.


- `Mount`, `DiskUsage`: os dados lidos, sem caminho nem nome de volume nas mensagens do `doctor`;
- `HostProbe` (`Protocol`): `postgres_mounts(project)` (`None` = serviço ausente ou parado),
  `docker_root_dir()` (`None` = Docker inalcançável) e `disk_usage(path)` (`None` = caminho inexistente
  neste host);
- `DockerHostProbe`: o real, sobre o `CommandRunner` da pilha (o prefixo do Compose; cada chamada com
  `env=docker_env(env)`, como o `ComposeStack`, e com o prazo `timeout`). Código ≠ 0, saída inválida ou
  exceção do executor → `None`.

Este módulo não importa `cq_server`, `click` nem `subprocess` (o `CommandRunner` é o único caminho até o
Docker; os testes de contrato das fronteiras do cliente conferem isso).

Escolhas **[I]** (inferência, conferidas só contra os testes do probe,
não contra uma saída real do Docker):
- `ps -q postgres` sem nenhuma linha → `None` (serviço parado ou ausente), sem chamar o `inspect`; a primeira
  linha é o identificador do contêiner, aceito só como 12 a 64 dígitos hexadecimais minúsculos (o que não
  casar é saída inválida → `None`; assim nada começado por `-` vira opção do `docker inspect`);
- o JSON das montagens tem de ser uma lista de objetos com `Type` e `Destination` textuais não vazios; `Name`
  ausente, nulo ou vazio (o `bind`) vira `name=None`; qualquer item fora disso invalida a saída inteira;
- o `DockerRootDir` vem sem espaços em volta, e a saída vazia é inválida;
- `disk_usage` chama `shutil.disk_usage` pelo atributo do módulo e devolve `None` para `OSError` (inclusive
  `FileNotFoundError` e `PermissionError`) e `ValueError` (caminho com caractere nulo), sem o executor.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from cq_client.stack import CommandRunner, compose_prefix, docker_env

# **[I]** O prazo de cada comando do probe, em segundos.
HOST_PROBE_TIMEOUT_SECONDS = 10

# O destino certo do volume do Postgres e o sufixo do nome do volume nomeado.
POSTGRES_DATA_DESTINATION = "/var/lib/postgresql"
POSTGRES_VOLUME_SUFFIX = "_pgdata"

# O serviço do Postgres no compose.yaml da pilha (cq_client.stack.SERVICES).
_POSTGRES_SERVICE = "postgres"
# Os argv fixos.
_INSPECT_MOUNTS_ARGV = ("docker", "inspect", "--format", "{{json .Mounts}}")
_INFO_ROOT_DIR_ARGV = ("docker", "info", "--format", "{{.DockerRootDir}}")
# **[I]** O identificador do contêiner devolvido pelo `ps -q` (curto ou completo).
_CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{12,64}")


@dataclass(frozen=True)
class Mount:
    """Uma montagem do contêiner (`docker inspect --format '{{json .Mounts}}'`): `type` (`bind`, `volume`,
    `tmpfs`, `npipe`, `cluster` ou outro), `name` (`None` sem nome) e `destination`."""

    type: str
    name: str | None
    destination: str


@dataclass(frozen=True)
class DiskUsage:
    """Total e livre de um disco, em bytes."""

    total: int
    free: int


@runtime_checkable
class HostProbe(Protocol):
    """A porta."""

    def postgres_mounts(self, project: str) -> tuple[Mount, ...] | None: ...

    def docker_root_dir(self) -> str | None: ...

    def disk_usage(self, path: str) -> DiskUsage | None: ...


class DockerHostProbe:
    """O `HostProbe` real pelo Docker local (ver o docstring do módulo)."""

    def __init__(self, runner: CommandRunner, stack_dir: Path, *, env: Mapping[str, str],
                 timeout: float = HOST_PROBE_TIMEOUT_SECONDS) -> None:
        self.runner = runner
        self.stack_dir = stack_dir
        self.env = env
        self.timeout = timeout

    def _run(self, argv: Sequence[str]) -> str | None:
        """Roda `argv` pelo executor, com `env=docker_env(env)` e o prazo; a saída decodificada, ou `None` com
        código ≠ 0, saída que não é UTF-8 ou exceção do executor (Docker indisponível, `OSError`, prazo)."""
        try:
            result = self.runner.run(list(argv), timeout=self.timeout, env=docker_env(self.env))
            if result.returncode != 0:
                return None
            return result.stdout.decode("utf-8")
        except Exception:  # noqa: BLE001 — exceção do executor → None, sem texto do Docker
            return None

    def postgres_mounts(self, project: str) -> tuple[Mount, ...] | None:
        listed = self._run([*compose_prefix(project, self.stack_dir), "ps", "-q", _POSTGRES_SERVICE])
        if listed is None:
            return None
        lines = [line.strip() for line in listed.splitlines() if line.strip()]
        if not lines:
            return None
        container = lines[0]
        if _CONTAINER_ID_PATTERN.fullmatch(container) is None:
            return None
        inspected = self._run([*_INSPECT_MOUNTS_ARGV, container])
        if inspected is None:
            return None
        return _parse_mounts(inspected)

    def docker_root_dir(self) -> str | None:
        output = self._run(_INFO_ROOT_DIR_ARGV)
        if output is None:
            return None
        root_dir = output.strip()
        return root_dir or None

    def disk_usage(self, path: str) -> DiskUsage | None:
        try:
            usage = shutil.disk_usage(path)
        except (OSError, ValueError):
            return None
        return DiskUsage(total=int(usage.total), free=int(usage.free))

    def __repr__(self) -> str:
        # Sem o ambiente (pode levar variáveis do usuário) e sem o diretório.
        return f"DockerHostProbe(timeout={self.timeout!r})"


def _parse_mounts(text: str) -> tuple[Mount, ...] | None:
    """As montagens do `docker inspect --format '{{json .Mounts}}'`, ou `None` com saída inválida."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    mounts: list[Mount] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        kind = item.get("Type")
        destination = item.get("Destination")
        name = item.get("Name")
        if not isinstance(kind, str) or not kind or not isinstance(destination, str) or not destination:
            return None
        if name is not None and not isinstance(name, str):
            return None
        mounts.append(Mount(type=kind, name=name or None, destination=destination))
    return tuple(mounts)
