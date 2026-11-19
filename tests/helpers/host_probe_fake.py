"""Dublês da porta de leitura do host do `doctor` local e invocação do `cq doctor` com eles.


- `FakeHostProbe(mounts, root_dir, usage_by_path, calls)` cumpre o `Protocol` `HostProbe` de
  `cq_client.host_probe`:
  - `mounts=None` → serviço ausente ou parado (`postgres_mounts` devolve `None`);
  - `root_dir=None` → Docker inalcançável (`docker_root_dir` devolve `None`);
  - caminho fora de `usage_by_path` → inexistente neste host (`disk_usage` devolve `None`);
  - cada chamada vai para `calls` (a lista dada, ou uma nova), como tupla `(método, argumento…)`.
- Construtores prontos: `volume_ok(project)`, `volume_anonymous()` (nome de 64 hex), `bind()`,
  `wrong_path(destination)` (as montagens do `postgres`, em tupla) e `usage(percent_free, total_gib)` (um
  `DiskUsage`). Os valores certos são os que o produto espera: tipo `volume`, nome `<projeto>_pgdata`, destino
  `/var/lib/postgresql`.
- `ScriptedRunner(responses)`: um `CommandRunner` falso com resposta por **argv exato** (um
  `RunResult` ou uma exceção, levantada); registra cada argv, o `env` e o `timeout` (`calls`); argv fora da
  tabela → `pytest.fail` (um `BaseException`, que o `except Exception` do produto não engole). Serve ao
  `DockerHostProbe` (o `FakeRunner` do CLI só conhece a lista fechada do `ComposeStack`, e o `docker_fake.py`
  é congelado). `stream` não é usado pelo probe: chamá-lo falha o teste.
- `invoke_doctor(args, *, obj, env, config, tmp_path, json=False)`: roda o `cq` como o `invoke_cli`
  de `tests/helpers/cli_runner.py`, reusando o `clean_env` dele **sem editá-lo**, com o `obj` restrito a
  `OBJ_KEYS | {"host_probe"}` (o `invoke_cli` e o `cli_invoke` recusam `host_probe`). **[I]** O
  `json=False` (que antepõe `--json`, como no `invoke_cli`) é um parâmetro a mais deste ajudante.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cq_client.host_probe import POSTGRES_DATA_DESTINATION, POSTGRES_VOLUME_SUFFIX, DiskUsage, Mount
from cq_client.stack import RunResult

from tests.helpers.cli_runner import OBJ_KEYS, _missing_config, clean_env

GIB = 1024 ** 3
ANONYMOUS_VOLUME_NAME = "3f" * 32
DOCTOR_OBJ_KEYS = OBJ_KEYS | {"host_probe"}


# --------------------------------------------------------------------------------------------------
# FakeHostProbe
# --------------------------------------------------------------------------------------------------


class FakeHostProbe:
    """O `HostProbe` falso (ver o docstring do módulo)."""

    def __init__(self, mounts: Sequence[Mount] | None = None, root_dir: str | None = None,
                 usage_by_path: Mapping[str, DiskUsage] | None = None,
                 calls: list[tuple[str, ...]] | None = None) -> None:
        self.mounts = tuple(mounts) if mounts is not None else None
        self.root_dir = root_dir
        self.usage_by_path = dict(usage_by_path or {})
        self.calls: list[tuple[str, ...]] = calls if calls is not None else []

    def postgres_mounts(self, project: str) -> tuple[Mount, ...] | None:
        self.calls.append(("postgres_mounts", project))
        return self.mounts

    def docker_root_dir(self) -> str | None:
        self.calls.append(("docker_root_dir",))
        return self.root_dir

    def disk_usage(self, path: str) -> DiskUsage | None:
        self.calls.append(("disk_usage", path))
        return self.usage_by_path.get(path)

    def __repr__(self) -> str:
        return "FakeHostProbe()"


def volume_ok(project: str) -> tuple[Mount, ...]:
    """O volume certo: nomeado `<projeto>_pgdata`, em `/var/lib/postgresql`."""
    return (Mount(type="volume", name=f"{project}{POSTGRES_VOLUME_SUFFIX}",
                  destination=POSTGRES_DATA_DESTINATION),)


def volume_anonymous() -> tuple[Mount, ...]:
    """Um volume anônimo (nome de 64 hex) no destino certo."""
    return (Mount(type="volume", name=ANONYMOUS_VOLUME_NAME, destination=POSTGRES_DATA_DESTINATION),)


def bind(destination: str = POSTGRES_DATA_DESTINATION) -> tuple[Mount, ...]:
    """Uma montagem `bind` (sem nome) no destino dado."""
    return (Mount(type="bind", name=None, destination=destination),)


def wrong_path(destination: str, *, project: str = "cq") -> tuple[Mount, ...]:
    """O volume nomeado certo, mas noutro destino (ex.: `/var/lib/postgresql/data`)."""
    return (Mount(type="volume", name=f"{project}{POSTGRES_VOLUME_SUFFIX}", destination=destination),)


def usage(percent_free: float, total_gib: float) -> DiskUsage:
    """Um disco de `total_gib` GiB com `percent_free` % livre (bytes inteiros)."""
    total = int(round(total_gib * GIB))
    free = int(round(total * percent_free / 100))
    return DiskUsage(total=total, free=free)


# --------------------------------------------------------------------------------------------------
# ScriptedRunner
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerCall:
    """Uma chamada recebida pelo `ScriptedRunner`."""

    argv: tuple[str, ...]
    env: dict[str, str]
    timeout: float | None


def result(stdout: bytes | str = b"", *, returncode: int = 0, stderr: bytes = b"") -> RunResult:
    """Conveniência: um `RunResult` (texto vira UTF-8)."""
    data = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    return RunResult(returncode=returncode, stdout=data, stderr=stderr)


class ScriptedRunner:
    """O `CommandRunner` falso com resposta por argv exato (ver o docstring do módulo)."""

    def __init__(self, responses: Mapping[Sequence[str], RunResult | BaseException]) -> None:
        self.responses: dict[tuple[str, ...], RunResult | BaseException] = {
            tuple(argv): response for argv, response in responses.items()
        }
        self.calls: list[RunnerCall] = []

    @property
    def argvs(self) -> list[list[str]]:
        """Os argv recebidos, em ordem, como listas (para `==`)."""
        return [list(call.argv) for call in self.calls]

    def run(self, argv: Sequence[str], *, timeout: float | None, env: Mapping[str, str]) -> RunResult:
        key = tuple(argv)
        self.calls.append(RunnerCall(argv=key, env=dict(env), timeout=timeout))
        if key not in self.responses:
            pytest.fail(f"argv fora da tabela do ScriptedRunner: {list(key)!r}")
        response = self.responses[key]
        if isinstance(response, BaseException):
            raise response
        return response

    def stream(self, argv: Sequence[str], *, env: Mapping[str, str]) -> Any:
        pytest.fail(f"ScriptedRunner.stream não é usado pelo probe: {list(argv)!r}")

    def __repr__(self) -> str:
        return f"ScriptedRunner(<{len(self.responses)} respostas>)"


# --------------------------------------------------------------------------------------------------
# invoke_doctor
# --------------------------------------------------------------------------------------------------


def invoke_doctor(args: Sequence[str], *, obj: Mapping[str, Any] | None = None,
                  env: Mapping[str, str | None] | None = None, config: Path | None = None,
                  tmp_path: Path | None = None, json: bool = False) -> Any:
    """Roda o `cq` com o `CliRunner`, como o `invoke_cli`, aceitando `host_probe` no `obj`; devolve o
    `click.testing.Result`."""
    from click.testing import CliRunner

    from cq_client import cli as cq_cli

    given = dict(obj or {})
    unknown = set(given) - DOCTOR_OBJ_KEYS
    if unknown:
        raise ValueError(f"chaves de obj fora das aceitas pelo CLI e de host_probe: {sorted(unknown)}")
    full_env = clean_env(config if config is not None else _missing_config(tmp_path), env)
    argv = (["--json"] if json else []) + list(args)
    return CliRunner().invoke(cq_cli.cli, argv, env=full_env, obj=given if obj is not None else None)
