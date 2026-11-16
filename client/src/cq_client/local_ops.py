"""As peças locais sem Click que o `cq` e o servidor MCP compartilham (a pilha, a sonda
de saúde e os corpos `--json`).

O mesmo comportamento nos dois: a pilha do perfil local com as recusas na ordem fixa
(perfil remoto → alvo do Docker → diretório → `.env`, sem Docker em nenhuma recusa), a sonda de saúde da
espera do `up` e os corpos `--json` (`status.output`, `up.output`, `down.output`). O
ambiente e o diretório atual vêm por argumento (o CLI passa `os.environ` e o `cwd` do `ctx.obj`; o servidor MCP,
os do `ServerDeps`).

Implementação: a pilha, a sonda, o `ProfileView`, as linhas de serviço e os corpos de `status`, `up` e `down`
têm uma só versão, aqui; o `cli.py` (nos comandos locais e no `doctor`) e o servidor MCP só as
chamam. Sem `click`, `mcp`, `cq_server` nem `subprocess`: o `ComposeStack`
vem de `stack_compose` (o único módulo com `subprocess`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_client.local_commands import StatusReport, UpSummary
from cq_client.profile import EffectiveProfile
from cq_client.stack import (
    STACK_ENV_FILE,
    CommandRunner,
    RemoteProfileRefused,
    ServiceState,
    Stack,
    StackEnvMissing,
    check_docker_target,
    locate_stack_dir,
)
from cq_client.stack_compose import ComposeStack

# As chaves de `origins` no `ProfileView`.
_VIEW_ORIGIN_KEYS = ("name", "kind", "api_url", "token", "compose_project")


def local_stack(profile: EffectiveProfile, env: Mapping[str, str], *, cwd: Path, stack: Stack | None = None,
                runner: CommandRunner | None = None, require_env_file: bool = True) -> tuple[Stack, bool]:
    """A pilha do perfil local e se o `.env` existe, depois das recusas sem nenhuma chamada ao Docker, na
    ordem: perfil remoto (`RemoteProfileRefused`), alvo do Docker não local (`StackTargetRefused`),
    diretório da pilha não encontrado (`StackDirNotFound`) e, com `require_env_file`, o `.env` ausente
    (`StackEnvMissing`; o arquivo nunca é lido nem criado). Com `stack`, devolve-o; senão, um
    `ComposeStack` com o `runner` e o `env`."""
    if profile.kind != "local":
        raise RemoteProfileRefused()
    check_docker_target(env)
    stack_dir = locate_stack_dir(profile, env, cwd)
    env_file_present = (stack_dir / STACK_ENV_FILE).exists()
    if require_env_file and not env_file_present:
        raise StackEnvMissing()
    if stack is not None:
        return stack, env_file_present
    return ComposeStack(profile.compose_project, stack_dir, runner, env=dict(env)), env_file_present


def health_probe(api: ApiClient) -> Callable[[], dict | None]:
    """O `system.health` da espera do `up`: o corpo (também o do 503), ou `None` sem resposta útil."""

    def probe() -> dict | None:
        try:
            return api.health()
        except (ApiError, ApiUnreachable):
            return None

    return probe


def profile_view(profile: EffectiveProfile) -> dict[str, Any]:
    """O `ProfileView`: sem o token, com o endereço de exibição; o projeto Compose
    (e a origem dele) só com o perfil local."""
    local = profile.kind == "local"
    origins: dict[str, Any] = {key: profile.origins.get(key) for key in _VIEW_ORIGIN_KEYS}
    if not local:
        origins["compose_project"] = None
    return {"name": profile.name, "kind": profile.kind, "api_url": profile.display_url,
            "token_set": bool(profile.token), "compose_project": profile.compose_project if local else None,
            "origins": origins}


def service_view(state: ServiceState) -> dict[str, str]:
    """`{"service", "state", "health"}` de um serviço."""
    return {"service": state.service, "state": state.state, "health": state.health}


def status_body(report: StatusReport) -> dict[str, Any]:
    """O `status.output`."""
    snapshot = report.snapshot
    return {"profile": profile_view(report.profile),
            "services": [service_view(s) for s in snapshot.services] if snapshot is not None else None,
            "api_port": snapshot.api_port if snapshot is not None else None,
            "api": {"reachable": report.api_reachable, "health": report.health,
                    "owned_by_stack": report.owned_by_stack},
            "warnings": list(report.warnings)}


def up_body(summary: UpSummary) -> dict[str, Any]:
    """O `up.output`: `project`, `services`, `api_port`."""
    return {"project": summary.project, "services": [service_view(s) for s in summary.services],
            "api_port": summary.api_port}


def down_body(profile: EffectiveProfile) -> dict[str, Any]:
    """O `down.output`: `{"project": <projeto Compose do perfil>}`."""
    return {"project": profile.compose_project}
