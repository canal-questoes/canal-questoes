"""Comandos locais da pilha, sem Click.


As rotinas de `up`, `down` e `status` sobre o `Protocol` `Stack`, sem Click, para que o CLI e o servidor
MCP as reaproveitem. `LOCAL_COMMANDS` é o conjunto das folhas do CLI fora do catálogo
(disjunto das operações). As mensagens fixas ficam aqui; o CLI só as imprime.

Tipos, constantes, mensagens e rotinas ficam todos neste módulo.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_client.profile import LOOPBACK_HOSTS, EffectiveProfile, insecure_transport_warning
from cq_client.stack import (
    LOG_SERVICES,
    SERVICES,
    DockerUnavailable,
    ServiceState,
    Stack,
    StackEnvMissing,
    StackError,
    StackFailure,
    StackSnapshot,
)

# As folhas locais do CLI.
LOCAL_COMMANDS = frozenset({"up", "down", "status", "logs"})
UP_TIMEOUT_SECONDS = 180
UP_POLL_SECONDS = 1.0
LOGS_TAIL = 200

# Limites das opções (fora deles, uso incorreto, código 2).
UP_TIMEOUT_RANGE = (1, 3600)
LOGS_TAIL_RANGE = (1, 10000)

# Os avisos do `status`.
STATUS_WARNINGS = ("foreign_api", "port_mismatch", "docker_unavailable", "insecure_transport",
                   "env_file_missing")

# Mensagens fixas (stdout no sucesso; stderr no progresso e nos erros).
UP_DONE_TEMPLATE = "Pilha local (projeto {project}) no ar."
DOWN_DONE_TEMPLATE = "Pilha local (projeto {project}) parada; os dados foram mantidos."
SERVICE_LINE_TEMPLATE = "  {service}: {state}, {health}"
# No texto para pessoas do `up` e do `status`, o estado e a saúde de cada
# serviço saem com rótulos em português; o `--json` mantém o enum.
SERVICE_STATE_LABELS = {"running": "rodando", "stopped": "parado", "absent": "ausente"}
SERVICE_HEALTH_LABELS = {"healthy": "saudável", "unhealthy": "com falha", "starting": "iniciando",
                         "none": "sem verificação"}
# O serviço "não verificado" (`unknown`) sai numa linha só, com o motivo pelo erro da pilha que o `status`
# absorveu no relatório. **[I]** o do Docker indisponível é o caso de referência; os do `.env` ausente
# e da falha genérica do Compose seguem o mesmo formato, com as palavras das mensagens fixas da pilha.
SERVICE_UNVERIFIED_TEMPLATE = "  {service}: não verificado ({reason})"
UNVERIFIED_DOCKER_UNAVAILABLE = "Docker indisponível"
UNVERIFIED_ENV_FILE_MISSING = "arquivo de ambiente ausente"
UNVERIFIED_STACK_FAILURE = "falha do Docker Compose"
UP_PROGRESS_STARTING = "Subindo os serviços do projeto {project}…"
UP_PROGRESS_FIRST_BUILD = "A primeira subida constrói as imagens e pode demorar."
UP_PROGRESS_WAITING = "Esperando a API ficar saudável…"
UP_TIMEOUT_TEMPLATE = 'A pilha não ficou saudável em {seconds} s: {services}. Veja "cq logs" e "cq doctor".'
PORT_MISMATCH_MESSAGE = ("A API do projeto publica outra porta que a do perfil local; ajuste api_url no "
                         "perfil.")
FOREIGN_API_TEMPLATE = "A API no endereço do perfil não pertence à pilha local (projeto {project})."
INTERRUPTED_MESSAGE = "Interrompido."
RERUN_MESSAGE = "Rode o comando de novo."
ASK_START_TEMPLATE = 'Subir a pilha local (projeto "{project}") agora? [s/N] '
LOGS_SERVICE_REFUSED = ("Serviço não aceito: os registros aceitos são os da api, do worker e do postgres; "
                        "os do engine são sensíveis e ficam fora das saídas do sistema.")
LOGS_FOLLOW_JSON_REFUSED = "--follow não combina com --json."
EVENTS_TYPE_OR_PERSON = "Informe --type ou --person."
# A linha fixa de resumo do `status` com o código 1 no stderr, quando
# não há um erro da pilha com mensagem própria (Docker indisponível, sem `.env`, falha do Compose). **[I]** só a
# forma (uma linha fixa) é exigida; o texto exato é escolha da implementação.
STATUS_PROBLEM_SUMMARY = "O status encontrou problemas; veja o relatório na saída padrão."


# --------------------------------------------------------------------------------------------------
# Tipos
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class UpSummary:
    """O `up` bem-sucedido: os quatro serviços "ok" e a porta conferida."""

    project: str
    services: tuple[ServiceState, ...]
    api_port: int


class UpTimeout(Exception):
    """A espera do `up` esgotou; `not_ok` são os serviços que não ficaram "ok" (código 1). A mensagem
    sai de `UP_TIMEOUT_TEMPLATE` com o tempo da espera."""

    exit_code = 1

    def __init__(self, not_ok: tuple[str, ...]) -> None:
        super().__init__("a pilha não ficou saudável a tempo")
        self.not_ok = tuple(not_ok)


class PortMismatch(Exception):
    """A porta publicada pela API do projeto difere da do perfil local (código 1)."""

    exit_code = 1
    message = PORT_MISMATCH_MESSAGE

    def __init__(self) -> None:
        super().__init__(self.message)


@dataclass(frozen=True)
class StatusReport:
    """O relatório do `status` (a forma de `status.output`): `snapshot` é `None` com perfil remoto;
    `health` é o corpo de `system.health` (ou `None` com a API inalcançável); `warnings` ⊆
    `STATUS_WARNINGS`; `exit_code` 0 ou 1. O `health` só guarda um corpo válido no esquema
    `system.health.response`."""

    profile: EffectiveProfile
    snapshot: StackSnapshot | None
    api_reachable: bool
    health: dict[str, Any] | None
    owned_by_stack: bool | None
    warnings: tuple[str, ...] = field(default=())
    exit_code: int = 0
    # Campo com padrão (opcional para quem constrói o relatório): o erro da pilha que o
    # `status` absorveu no relatório (`DockerUnavailable`, `StackEnvMissing` ou `StackFailure`), para o CLI
    # escrever a linha fixa de resumo no stderr. `None` sem erro da pilha.
    stack_error: StackError | None = None


# --------------------------------------------------------------------------------------------------
# Apoio (puro)
# --------------------------------------------------------------------------------------------------

# Os serviços sem verificação de saúde que contam como "ok" rodando (só o worker).
_HEALTH_OPTIONAL = frozenset({"worker"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def service_ok(state: ServiceState) -> bool:
    """Serviço "ok": `running` e `healthy`, ou `running` sem verificação (só o worker)."""
    if state.state != "running":
        return False
    return state.health == "healthy" or (state.health == "none" and state.service in _HEALTH_OPTIONAL)


def not_ok_services(snapshot: StackSnapshot) -> tuple[str, ...]:
    """Os serviços de `SERVICES` que não estão "ok", na ordem fixa (um serviço sem linha conta como fora)."""
    by_name = {s.service: s for s in snapshot.services}
    return tuple(name for name in SERVICES if name not in by_name or not service_ok(by_name[name]))


def profile_port(profile: EffectiveProfile) -> int | None:
    """A porta do `api_url` do perfil (a explícita, ou a padrão do esquema); `None` se ilegível."""
    try:
        parts = urllib.parse.urlsplit(profile.api_url)
        port = parts.port
    except ValueError:
        return None
    return port if port is not None else _DEFAULT_PORTS.get(parts.scheme)


def _profile_on_loopback(profile: EffectiveProfile) -> bool:
    try:
        host = urllib.parse.urlsplit(profile.api_url).hostname
    except ValueError:
        return False
    return host is not None and host in LOOPBACK_HOSTS


def api_owned(snapshot: StackSnapshot, profile: EffectiveProfile) -> bool:
    """A API do endereço do perfil pertence à pilha local (pertencimento da API).

    Vale o `api_owned` do instantâneo quando a implementação da pilha o decide; senão (o `ComposeStack` o
    deixa `None`), a regra: o contêiner `api` do projeto `running` e `healthy`, o host do perfil de loopback
    e a porta publicada (`port api 8000`) igual à do perfil."""
    if snapshot.api_owned is not None:
        return snapshot.api_owned
    api = next((s for s in snapshot.services if s.service == "api"), None)
    if api is None or api.state != "running" or api.health != "healthy":
        return False
    port = profile_port(profile)
    return _profile_on_loopback(profile) and port is not None and snapshot.api_port == port


def port_mismatch(snapshot: StackSnapshot, profile: EffectiveProfile) -> bool:
    """A api do projeto publica uma porta, e ela difere da do perfil."""
    return snapshot.api_port is not None and snapshot.api_port != profile_port(profile)


def health_ok(health: dict | None) -> bool:
    """O corpo de `system.health` saudável (200, `status: ok`; o 503 traz `status: error`)."""
    return isinstance(health, dict) and health.get("status") == "ok"


# `contracts/api/v1/schemas/system.health.response.schema.json`, à mão como o `doctor._server_checks` (o pacote
# cliente não depende de um validador de JSON Schema). `fullmatch`: o `$` do `re` aceitaria um `\n` no fim.
_HEALTH_KEYS = frozenset({"status", "version", "database", "schema_version", "expected_schema_version"})
_HEALTH_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_HEALTH_SCHEMA_VERSION = re.compile(r"[0-9]{4}")


def valid_health_body(body: object) -> bool:
    """O corpo cabe no esquema `system.health.response`: exatamente as cinco
    chaves, `status` e `database` no enum, `version` em `X.Y.Z` e as versões do esquema com 4 dígitos (a atual
    também nula)."""
    if not isinstance(body, dict) or body.keys() != _HEALTH_KEYS:
        return False

    def matches(pattern: re.Pattern[str], value: object) -> bool:
        return isinstance(value, str) and pattern.fullmatch(value) is not None

    return (body["status"] in ("ok", "error") and body["database"] in ("ok", "unreachable")
            and matches(_HEALTH_VERSION, body["version"])
            and (body["schema_version"] is None or matches(_HEALTH_SCHEMA_VERSION, body["schema_version"]))
            and matches(_HEALTH_SCHEMA_VERSION, body["expected_schema_version"]))


def service_line(state: ServiceState, stack_error: StackError | None = None) -> str:
    """A linha de um serviço no texto para pessoas do `up` e do `status`: o estado e a
    saúde com os rótulos em português; o serviço `unknown`, `não verificado (<motivo>)` pelo `stack_error`."""
    if state.state == "unknown" or state.health == "unknown":
        if isinstance(stack_error, DockerUnavailable):
            reason = UNVERIFIED_DOCKER_UNAVAILABLE
        elif isinstance(stack_error, StackEnvMissing):
            reason = UNVERIFIED_ENV_FILE_MISSING
        else:
            reason = UNVERIFIED_STACK_FAILURE
        return SERVICE_UNVERIFIED_TEMPLATE.format(service=state.service, reason=reason)
    return SERVICE_LINE_TEMPLATE.format(service=state.service,
                                        state=SERVICE_STATE_LABELS.get(state.state, state.state),
                                        health=SERVICE_HEALTH_LABELS.get(state.health, state.health))


def unverified_snapshot() -> StackSnapshot:
    """Os quatro serviços "não verificado" (`unknown`): Docker indisponível ou sem `.env`."""
    return StackSnapshot(services=tuple(ServiceState(s, "unknown", "unknown") for s in SERVICES),
                         api_port=None, api_owned=None)


def up_timeout_message(error: UpTimeout, timeout: float) -> str:
    """A linha fixa da espera esgotada, com o tempo em segundos inteiros quando exato."""
    seconds: float | int = int(timeout) if float(timeout).is_integer() else timeout
    return UP_TIMEOUT_TEMPLATE.format(seconds=seconds, services=", ".join(error.not_ok))


# --------------------------------------------------------------------------------------------------
# Rotinas
# --------------------------------------------------------------------------------------------------


def run_up(stack: Stack, profile: EffectiveProfile, api_health: Callable[[], dict | None], *,
           timeout: float, rebuild: bool, clock: Callable[[], float], sleep: Callable[[float], None],
           progress: Callable[[str], None]) -> UpSummary:
    """Sobe a pilha e espera os quatro serviços "ok" e a porta conferida. Levanta `StackError`,
    `UpTimeout` ou `PortMismatch`.

    Uma leitura antes (só para avisar da primeira construção), uma subida
    (`up(rebuild=...)`, idempotente: o Compose não recria o que roda) e, depois que o Compose termina, a
    espera no `clock` e no `sleep` injetados, uma leitura a cada `UP_POLL_SECONDS` até `timeout`. O sucesso
    exige os quatro serviços "ok", a API do perfil pertencendo à pilha (nunca só a resposta HTTP
    no endereço) e o `system.health` saudável; a porta publicada diferente da do perfil, com os quatro
    "ok", é `PortMismatch` (não muda esperando). Nada é derrubado em nenhum caminho. O progresso sai só
    pelas linhas fixas; o `KeyboardInterrupt` sobe ao CLI."""
    project = profile.compose_project
    progress(UP_PROGRESS_STARTING.format(project=project))
    before = stack.snapshot()
    if any(s.state == "absent" for s in before.services):
        progress(UP_PROGRESS_FIRST_BUILD)
    stack.up(rebuild=rebuild)
    progress(UP_PROGRESS_WAITING)
    start = clock()
    while True:
        snapshot = stack.snapshot()
        missing = not_ok_services(snapshot)
        if not missing:
            if port_mismatch(snapshot, profile):
                raise PortMismatch()
            if (snapshot.api_port is not None and api_owned(snapshot, profile)
                    and health_ok(api_health())):
                return UpSummary(project=project, services=snapshot.services, api_port=snapshot.api_port)
            # Os contêineres "ok", mas a API do perfil não é a desta pilha ou ainda não responde.
            missing = ("api",)
        elapsed = clock() - start
        if elapsed >= timeout:
            raise UpTimeout(missing)
        sleep(min(UP_POLL_SECONDS, timeout - elapsed))


def run_down(stack: Stack) -> None:
    """Para e remove os contêineres e a rede do projeto, mantendo os dados. Com a pilha já
    parada ou ausente, o Compose não falha (código 0)."""
    stack.down()


def run_logs(stack: Stack, services: Sequence[str] = (), *, tail: int = LOGS_TAIL,
             follow: bool = False) -> Iterator[str]:
    """Os registros limpos dos `services` (vazio = os três de `LOG_SERVICES`, na ordem fixa).

    A conferência fica aqui também (defesa em profundidade): serviço fora de `LOG_SERVICES`, comparado
    exatamente, ou `tail` fora de `LOGS_TAIL_RANGE` → `ValueError` com a mensagem fixa, antes de qualquer
    chamada à pilha; o CLI valida antes e imprime `Uso incorreto: …`."""
    chosen = tuple(dict.fromkeys(services)) if services else LOG_SERVICES
    if any(s not in LOG_SERVICES for s in chosen):
        raise ValueError(LOGS_SERVICE_REFUSED)
    low, high = LOGS_TAIL_RANGE
    if isinstance(tail, bool) or not isinstance(tail, int) or not low <= tail <= high:
        raise ValueError("--tail fora do intervalo aceito.")
    return stack.logs(chosen, tail=tail, follow=follow)


def status_report(profile: EffectiveProfile, stack: Stack | None, api: ApiClient, *,
                  env_file_present: bool = True) -> StatusReport:
    """O relatório do `status`; `stack = None` com perfil remoto.

    `env_file_present` (opcional, com padrão): `False` quando o CLI já viu que o `.env` falta no
    diretório da pilha; o relatório sai então sem chamar a pilha, como quando a própria pilha
    levanta `StackEnvMissing`. Com o perfil remoto, a pilha nunca é chamada.

    - Docker indisponível (`DockerUnavailable`), sem `.env` ou outra falha do Compose (`StackFailure`) ao
      ler o estado: os quatro serviços "não verificado", o aviso correspondente (`docker_unavailable`,
      `env_file_missing`; a falha genérica não tem aviso próprio) e o erro em `stack_error`, código
      1. Os demais erros da pilha (variável ausente, código 2) sobem ao CLI, sem relatório.
    - A API: `system.health` (o corpo também no 503); inalcançável → `reachable = False`; outra resposta
      (`ApiError`) ou um corpo fora do esquema `system.health.response` → alcançável sem corpo.
    - Avisos, na ordem de `STATUS_WARNINGS`: `foreign_api` (a API responde, mas não é da pilha),
      `port_mismatch`, `docker_unavailable`, `insecure_transport` (só informativo: não muda o código),
      `env_file_missing`.
    - Código 0 só com tudo rodando e saudável e a API desta pilha (local) ou com a API saudável (remoto)."""
    warnings: set[str] = set()
    if insecure_transport_warning(profile) is not None:
        warnings.add("insecure_transport")

    snapshot: StackSnapshot | None = None
    stack_error: StackError | None = None
    verified = False
    if profile.kind == "local" and stack is not None:
        if not env_file_present:
            stack_error = StackEnvMissing()
        else:
            try:
                snapshot = stack.snapshot()
                verified = True
            except (DockerUnavailable, StackEnvMissing, StackFailure) as error:
                stack_error = error
        if stack_error is not None:
            snapshot = unverified_snapshot()
            if isinstance(stack_error, DockerUnavailable):
                warnings.add("docker_unavailable")
            elif isinstance(stack_error, StackEnvMissing):
                warnings.add("env_file_missing")

    try:
        health: dict[str, Any] | None = api.health()
        reachable = True
    except ApiUnreachable:
        health, reachable = None, False
    except ApiError:
        health, reachable = None, True
    if not valid_health_body(health):
        # Um corpo fora de `system.health.response` não é repassado; a API
        # respondeu (alcançável), mas sem saúde confirmada, como no `ApiError`.
        health = None

    owned: bool | None = None
    if verified and snapshot is not None:
        owned = api_owned(snapshot, profile)
        if reachable and not owned:
            warnings.add("foreign_api")
        if port_mismatch(snapshot, profile):
            warnings.add("port_mismatch")

    api_fine = reachable and health_ok(health)
    if profile.kind == "local" and stack is not None:
        healthy = (verified and snapshot is not None and api_fine and owned is True
                   and not not_ok_services(snapshot) and not (warnings - {"insecure_transport"}))
    else:
        healthy = api_fine
    return StatusReport(profile=profile, snapshot=snapshot, api_reachable=reachable, health=health,
                        owned_by_stack=owned,
                        warnings=tuple(w for w in STATUS_WARNINGS if w in warnings),
                        exit_code=0 if healthy else 1, stack_error=stack_error)
