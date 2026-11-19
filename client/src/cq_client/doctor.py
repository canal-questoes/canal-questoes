"""`doctor` no cliente.


Sem Click, para que o servidor MCP reaproveite a parte da API (`collect_api_checks`). Compõe:
- a **parte da API**: `api.reachable`, `database.*` e `api.access` pelo `health` e pelo código HTTP de
  `system.doctor`, mais as verificações devolvidas pelo servidor (as 6 globais e as `bot.*`);
- a **parte local** sobre o `Protocol` `Stack` (`collect_local_checks`; "não se aplica" com perfil
  remoto) e as 2 do host sobre o `HostProbe` (`collect_host_checks`);
- o **arquivo de perfil** (`check_profile_file`, com qualquer perfil; nunca muda permissões nem mostra o
  caminho).

As mensagens são fixas, em português, sem caminho, URL, identidade nem segredo; a resposta da
API nunca é repassada. Saída: 0 sem `failure`, 1 com ao menos uma.

Estrutura: `Check`, `CHECK_IDS`, os rótulos e as mensagens fixas. A
parte da API, o arquivo de perfil e o código de saída ficam aqui, assim como a parte local
(`collect_local_checks`).

Verificações do servidor e do host:
as 3 globais do servidor com motivo (`backup.age`, `smtp.server`, `retention.overdue`), conferidas por
`SERVER_REASON_MESSAGES` (a tabela `SERVER_MESSAGES` fica à parte; a falha de `bots.master_key`
também), e as 2 do host (`postgres.volume`,
`disk.space`, parte `local`) de `collect_host_checks` sobre o `HostProbe`. Os grupos têm tamanho fixo
(`SERVER_CHECK_IDS` com 9, `CHECK_IDS` com 27, `_SERVER_GLOBAL_IDS` com 6). O módulo implementa também
o validador do corpo com as regras das globais,
`collect_host_checks`, `postgres_volume_check`, `disk_space_check` e `LocalDoctorSettings.from_env`.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_client.host_probe import POSTGRES_DATA_DESTINATION, POSTGRES_VOLUME_SUFFIX, DiskUsage, HostProbe
from cq_client.local_commands import api_owned, profile_port, service_ok, valid_health_body
from cq_client.profile import EffectiveProfile
from cq_client.stack import (
    MIN_COMPOSE,
    SERVICES,
    DockerUnavailable,
    ServiceState,
    Stack,
    StackDirNotFound,
    StackEnvMissing,
    StackError,
    StackSnapshot,
)

CheckPart = Literal["api", "local", "profile"]
DoctorResult = Literal["ok", "warning", "failure", "not_verified", "not_applicable"]
DOCTOR_RESULTS = ("ok", "warning", "failure", "not_verified", "not_applicable")

# A lista fechada de ids, na ordem de saída. As de `bot.*` saem uma por
# oferecimento. As 3 globais com motivo ficam no fim de `SERVER_CHECK_IDS` (a ordem de
# `cq_server.doctor.SERVER_CHECK_IDS`); as 2 do host (`HOST_CHECK_IDS`, parte `local`) logo depois das 10 locais
# (`LOCAL_CHECK_IDS`).
API_CLIENT_CHECK_IDS = ("api.reachable", "database.reachable", "database.schema", "api.access")
SERVER_CHECK_IDS = ("bots.master_key", "bot.access", "bot.poll", "bot.lease", "tasks.overdue",
                    "clock.skew", "backup.age", "smtp.server", "retention.overdue")
LOCAL_CHECK_IDS = ("docker.reachable", "compose.version", "stack.directory", "stack.env_file",
                   "service.postgres", "service.api", "service.worker", "service.engine",
                   "engine.image", "api.owner")
# As 2 verificações do host, parte `local`, logo depois das 10 locais.
HOST_CHECK_IDS = ("postgres.volume", "disk.space")
PROFILE_CHECK_IDS = ("profile.permissions", "profile.git")
CHECK_IDS = API_CLIENT_CHECK_IDS + SERVER_CHECK_IDS + LOCAL_CHECK_IDS + HOST_CHECK_IDS + PROFILE_CHECK_IDS

CHECK_PARTS: Mapping[str, str] = {
    **{check_id: "api" for check_id in API_CLIENT_CHECK_IDS + SERVER_CHECK_IDS},
    **{check_id: "local" for check_id in LOCAL_CHECK_IDS + HOST_CHECK_IDS},
    **{check_id: "profile" for check_id in PROFILE_CHECK_IDS},
}

# O rótulo de cada resultado no texto, a linha de cada verificação e o resumo.
RESULT_LABELS: Mapping[str, str] = {
    "ok": "ok",
    "warning": "aviso",
    "failure": "falha",
    "not_verified": "não verificado",
    "not_applicable": "não se aplica",
}
CHECK_LINE_TEMPLATE = "[{label}] {id}{offering}: {message}{extra}"
SUMMARY_TEMPLATE = "Resultado: {failures} falha(s), {warnings} aviso(s)."

# Mensagens fixas do cliente por `(id, result)` (**[I]**: textos escolhidos pela implementação,
# sempre os mesmos em cada execução). As 6 do servidor vêm da API, conferidas contra `SERVER_MESSAGES`.
CLIENT_MESSAGES: Mapping[tuple[str, str], str] = {
    ("api.reachable", "ok"): "API alcançável.",
    ("api.reachable", "failure"): "API inalcançável no endereço do perfil.",
    ("database.reachable", "ok"): "Banco de dados alcançável pela API.",
    ("database.reachable", "failure"): "A API não alcança o banco de dados.",
    ("database.schema", "ok"): "Migrações do banco em dia.",
    ("database.schema", "failure"): "As migrações do banco não estão na versão esperada.",
    ("api.access", "ok"): "Acesso à parte da API do doctor.",
    ("docker.reachable", "ok"): "Docker acessível.",
    ("docker.reachable", "failure"): "O Docker não está disponível neste computador (instalado e em execução?).",
    ("compose.version", "ok"): "Versão do Docker Compose compatível.",
    ("compose.version", "warning"): "Docker Compose anterior à versão 2.27: atualize-o.",
    # [I] Com o Docker no ar, o Compose v2 que não responde (ausente) ou com a versão ilegível impede
    # toda operação da pilha; o "aviso" fica só para a versão abaixo da 2.27.
    ("compose.version", "failure"): "Docker Compose v2 ausente ou com versão ilegível: instale-o ou atualize-o.",
    ("stack.directory", "ok"): "Diretório da pilha encontrado.",
    ("stack.directory", "failure"):
        "Diretório da pilha não encontrado: defina stack_dir no perfil local ou CQ_STACK_DIR.",
    ("stack.env_file", "ok"): "Arquivo de ambiente presente no diretório da pilha.",
    ("stack.env_file", "failure"):
        "Arquivo de ambiente ausente no diretório da pilha: crie-o a partir do .env.example.",
    ("service.postgres", "ok"): "Serviço rodando e saudável.",
    ("service.postgres", "failure"): "Serviço parado, ausente ou com falha.",
    ("service.api", "ok"): "Serviço rodando e saudável.",
    ("service.api", "failure"): "Serviço parado, ausente ou com falha.",
    ("service.worker", "ok"): "Serviço rodando.",
    ("service.worker", "failure"): "Serviço parado, ausente ou com falha.",
    ("service.engine", "ok"): "Serviço rodando e saudável.",
    ("service.engine", "failure"): "Serviço parado, ausente ou com falha.",
    ("engine.image", "ok"): "Imagem do motor presente.",
    ("engine.image", "failure"): 'Imagem do motor ausente: suba a pilha com "cq up".',
    ("api.owner", "ok"): "A API do perfil pertence à pilha local.",
    ("api.owner", "failure"): "A API no endereço do perfil não pertence à pilha local.",
    ("profile.permissions", "ok"): "Arquivo de perfil legível só pelo dono.",
    ("profile.permissions", "warning"):
        "Arquivo de perfil legível por outros usuários: restrinja as permissões.",
    ("profile.permissions", "not_verified"):
        "No Windows, as permissões do arquivo de perfil dependem das ACLs e não são verificadas.",
    ("profile.git", "ok"): "Arquivo de perfil fora de cópia de trabalho git.",
    ("profile.git", "warning"):
        "Arquivo de perfil dentro de uma cópia de trabalho git: mova-o para fora dela.",
}

# As mensagens fixas do servidor por `(id, result)`, a mesma tabela de
# `cq_server.doctor.DOCTOR_MESSAGES` (o cliente não importa o servidor). Um corpo com outra mensagem foge
# do contrato e vira "erro da API", sem repassar nada dele.
SERVER_MESSAGES: Mapping[tuple[str, str], str] = {
    ("bots.master_key", "ok"): "Chave-mestra dos bots configurada.",
    ("bots.master_key", "warning"):
        "Chave-mestra dos bots ausente: os bots não podem ser registrados nem consultados.",
    ("bot.access", "ok"): "Bot com acesso ao Telegram.",
    ("bot.access", "failure"): 'Bot sem acesso ao Telegram: troque o token com "cq bots register".',
    ("bot.poll", "ok"): "Bot consultado recentemente.",
    ("bot.poll", "warning"): "Bot sem consulta recente: o worker pode estar parado ou sem rede.",
    ("bot.lease", "ok"): "Bot atendido por um worker.",
    ("bot.lease", "warning"): "Nenhum worker está atendendo o bot.",
    ("tasks.overdue", "ok"): "Nenhuma tarefa atrasada.",
    ("tasks.overdue", "warning"): "Há tarefas atrasadas: o worker pode estar parado.",
    ("clock.skew", "ok"): "Relógio da API em sincronia com o banco.",
    ("clock.skew", "warning"): "Relógio da API fora de sincronia com o banco.",
}

# `api.access = failure`, com o motivo de cada caso (a resposta da API nunca é repassada).
API_ACCESS_FAILURE_MESSAGES: Mapping[str, str] = {
    "no_token": "Sem acesso à parte da API: sem token.",
    "token_refused": "Sem acesso à parte da API: token recusado.",
    "forbidden": "Sem acesso à parte da API: sem permissão.",
    "api_error": "Sem acesso à parte da API: erro da API.",
}

# "Não verificado" por causa de outra verificação, e "não se aplica".
NOT_VERIFIED_MESSAGES: Mapping[str, str] = {
    "api_unreachable": "Não verificado: a API está inalcançável.",
    "database": "Não verificado: a API não confirmou o banco de dados.",
    "api_access": "Não verificado: sem acesso à parte da API.",
    "docker_unavailable": "Não verificado: o Docker não está disponível.",
    "stack_directory": "Não verificado: diretório da pilha não encontrado.",
    "env_file": "Não verificado: arquivo de ambiente ausente.",
    # [I] O alvo do Docker não local e a falha do Compose ao ler o estado da pilha (o texto do
    # Docker nunca é repassado).
    "docker_target": "Não verificado: o Docker aponta para um alvo que não é local.",
    "stack_state": "Não verificado: o Docker Compose falhou ao ler o estado da pilha.",
    # [I] O texto de `no_host_probe` é escolha da implementação (fixo é só o
    # motivo): com dublê injetado e sem `host_probe`, o CLI não monta o probe real.
    "no_host_probe": "Não verificado: a leitura do host não está disponível nesta execução.",
    "postgres_not_running": "Não verificado: o serviço do Postgres não está em execução.",
    "unreachable_path": "O disco dos dados do Docker não é acessível neste computador.",
}
NOT_APPLICABLE_REMOTE = "Não se aplica (perfil remoto)."
NOT_APPLICABLE_NO_PROFILE_FILE = "Não se aplica: não há arquivo de perfil."

# A linha fixa do stderr quando o relatório sai com o código 1 (o relatório
# continua no stdout). [I] O texto é escolha da implementação; segue o do `status`.
DOCTOR_PROBLEM_SUMMARY = "O doctor encontrou falhas; veja o relatório na saída padrão."

# --------------------------------------------------------------------------------------------------
# Tabelas das verificações do servidor e do host
# --------------------------------------------------------------------------------------------------

# As mensagens aceitas das 3 globais do servidor com motivo por `(id, result)`, os
# mesmos textos de `cq_server.doctor.DOCTOR_REASON_MESSAGES` (o cliente não importa o servidor). Nas de
# `backup.age`, o modelo `BACKUP_AGE_TEMPLATE` entra no conjunto como texto: o corpo só é aceito se a
# mensagem for igual a `BACKUP_AGE_TEMPLATE.format(hours=count)`. Entra também a falha
# de `bots.master_key`; o "ok" e o "aviso" dela ficam em `SERVER_MESSAGES`, e os pares `(id, result)` das duas
# tabelas não se repetem (o validador aceita o par de qualquer uma delas: `SERVER_MESSAGES` por igualdade,
# `SERVER_REASON_MESSAGES` por pertinência).
BACKUP_AGE_TEMPLATE = "Último backup há {hours} h."
SERVER_REASON_MESSAGES: Mapping[tuple[str, str], frozenset[str]] = {
    ("bots.master_key", "failure"): frozenset({
        "Chave-mestra dos bots não decifra os tokens gravados: use a chave com que os bots foram registrados.",
    }),
    ("backup.age", "ok"): frozenset({BACKUP_AGE_TEMPLATE}),
    ("backup.age", "warning"): frozenset({"Backup não configurado nesta pilha.", BACKUP_AGE_TEMPLATE}),
    ("backup.age", "failure"): frozenset({"Sem registro legível do último backup.", BACKUP_AGE_TEMPLATE}),
    ("smtp.server", "ok"): frozenset({"O servidor de e-mail aceitou a conexão e a autenticação."}),
    ("smtp.server", "warning"): frozenset({
        "O servidor de e-mail ainda não foi testado.",
        "E-mail não configurado no worker.",
        "O último teste do servidor de e-mail é antigo.",
    }),
    ("smtp.server", "failure"): frozenset({
        "Sem conexão com o servidor de e-mail.",
        "Falha na negociação TLS com o servidor de e-mail.",
        "O servidor de e-mail recusou a autenticação.",
    }),
    ("retention.overdue", "ok"): frozenset({"Nenhum oferecimento arquivado com prazo de retenção vencido."}),
    ("retention.overdue", "warning"):
        frozenset({"Há oferecimentos arquivados com prazo de retenção vencido sem expurgo."}),
}

# As mensagens de `postgres.volume` por motivo (`ok` = volume nomeado certo no destino certo).
POSTGRES_VOLUME_MESSAGES: Mapping[str, str] = {
    "ok": "O volume do Postgres está montado no lugar certo.",
    "anonymous": "O Postgres usa um volume anônimo.",
    # [I]: volume nomeado (nome sem 64 hex) diferente de `<projeto>_pgdata` no destino; sem o nome do volume.
    "other_volume": "O Postgres usa um volume com outro nome.",
    "bind": "O Postgres usa um diretório do host.",
    "wrong_path": "O volume do Postgres não está em /var/lib/postgresql.",
}

# A mensagem de `disk.space` (`{percent}` inteiro, piso; `{gib}` com uma casa decimal),
# montada no cliente pelo pior alvo.
DISK_SPACE_TEMPLATE = "Espaço em disco livre: {percent}% ({gib} GiB)."

# As 2 do host pelo perfil remoto (o túnel).
NOT_APPLICABLE_REMOTE_VM = "Não se aplica pelo túnel: rode o doctor na VM."

# As variáveis do `cq doctor` (vazia = ausente).
DOCTOR_ENV_DISK_WARNING = "CQ_DOCTOR_DISK_WARNING_PERCENT"
DOCTOR_ENV_DISK_FAILURE = "CQ_DOCTOR_DISK_FAILURE_PERCENT"
DOCTOR_ENV_BACKUP_DIR = "CQ_BACKUP_DIR"

# Mensagens fixas de `LocalDoctorSettings.from_env` (sem o valor recebido). [I] Os textos são escolha da
# implementação; uma mensagem por variável e por tipo de erro, sem eco da entrada.
LOCAL_SETTINGS_MESSAGES: Mapping[str, str] = {
    DOCTOR_ENV_DISK_WARNING: f"{DOCTOR_ENV_DISK_WARNING} deve ser um número maior que 0 e menor que 100.",
    DOCTOR_ENV_DISK_FAILURE: f"{DOCTOR_ENV_DISK_FAILURE} deve ser um número maior que 0 e menor que 100.",
    "order": f"{DOCTOR_ENV_DISK_FAILURE} deve ser menor que {DOCTOR_ENV_DISK_WARNING}.",
    DOCTOR_ENV_BACKUP_DIR: f"{DOCTOR_ENV_BACKUP_DIR} deve ser um caminho absoluto.",
}


@dataclass(frozen=True)
class LocalDoctorSettings:
    """Os limites do disco e o diretório extra a medir: percentuais em (0, 100) com
    `disk_failure_percent < disk_warning_percent`; `backup_dir` absoluto quando definido."""

    disk_warning_percent: float = 20
    disk_failure_percent: float = 10
    backup_dir: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LocalDoctorSettings:
        """Lê as variáveis de `env` (ausente ou vazia → padrão); inválido → `ValueError` com texto fixo,
        sem o valor: percentual fora de (0, 100), não numérico ou não finito; a falha não
        menor que o aviso (também contra o padrão da outra variável); `CQ_BACKUP_DIR` relativo."""
        defaults = cls()
        warning = _percent_from_env(env, DOCTOR_ENV_DISK_WARNING, defaults.disk_warning_percent)
        failure = _percent_from_env(env, DOCTOR_ENV_DISK_FAILURE, defaults.disk_failure_percent)
        if not failure < warning:
            raise ValueError(LOCAL_SETTINGS_MESSAGES["order"])
        backup_dir = env.get(DOCTOR_ENV_BACKUP_DIR) or None
        if backup_dir is not None and not _is_absolute(backup_dir):
            raise ValueError(LOCAL_SETTINGS_MESSAGES[DOCTOR_ENV_BACKUP_DIR])
        return cls(disk_warning_percent=warning, disk_failure_percent=failure, backup_dir=backup_dir)


def _percent_from_env(env: Mapping[str, str], name: str, default: float) -> float:
    """O percentual de `name` em `env` (ausente ou vazio → `default`); [I] aceita casas decimais (`12.5`)."""
    raw = env.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        # Sem a mensagem do `float`, que repete a entrada.
        raise ValueError(LOCAL_SETTINGS_MESSAGES[name]) from None
    if not math.isfinite(value) or not 0 < value < 100:
        raise ValueError(LOCAL_SETTINGS_MESSAGES[name])
    return value


def _is_absolute(path: str) -> bool:
    """[I] Absoluto no formato POSIX (o da VM) ou no do Windows (o computador do operador), qualquer que seja o
    sistema em que o `cq` roda."""
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


@dataclass(frozen=True)
class Check:
    """Uma verificação do `doctor` (a forma de `doctor.output`): `offering` só nas
    `bot.*` (o código); `count` só em `tasks.overdue`; `detail` é dado informativo de uma "ok" (ex.: a
    versão da imagem do motor)."""

    id: str
    part: str
    result: str
    message: str
    offering: str | None = None
    count: int | None = None
    detail: str | None = None


def collect_api_checks(api: ApiClient, profile: EffectiveProfile) -> list[Check]:
    """A parte da API: `health`, o código HTTP de `system.doctor` e as 6 do servidor.

    A ordem: as 4 do cliente e, depois, as do servidor **na ordem em que a API as
    devolve** (o cliente não reordena). Quando as do servidor não são verificadas, sai uma linha por id de
    `SERVER_CHECK_IDS`, com o motivo da primeira verificação que falhou. Nenhuma resposta da API é
    repassada: só as do servidor, validadas pelo contrato, levam o texto fixo que o servidor publica.
    """
    try:
        health = api.health()
    except (ApiUnreachable, ApiError):
        health = None
    if health is None or not valid_health_body(health):
        # [I] Um `health` que nem responde como a API (fora do 200/503, sem JSON ou com o corpo fora do esquema
        # `system.health.response`, como no `status`) também conta como API inalcançável no endereço do
        # perfil: nada nele confirma que ali está a API do sistema.
        return [_client_check("api.reachable", "failure"),
                *(_not_verified(check_id, "api_unreachable") for check_id in API_CLIENT_CHECK_IDS[1:]),
                *_server_not_verified("api_unreachable")]

    checks = [_client_check("api.reachable", "ok"), *_database_checks(health)]
    if any(check.result == "failure" for check in checks):
        # `health` 503: a autenticação também depende do banco; a operação nem é chamada.
        return [*checks, _not_verified("api.access", "database"), *_server_not_verified("database")]

    if not profile.token:
        # "sem token", sem chamar a operação (nunca o 401 que a API daria).
        return [*checks, _access_failure("no_token"), *_server_not_verified("api_access")]

    try:
        body = api.system_doctor()
    except ApiUnreachable:
        return [*checks, _access_failure("api_error"), *_server_not_verified("api_access")]
    except ApiError as exc:
        return [*checks, _access_failure(_access_failure_reason(exc.status)),
                *_server_not_verified("api_access")]

    server = _server_checks(body)
    if server is None:
        # Corpo fora do contrato: "erro da API", sem repassar nada dele.
        return [*checks, _access_failure("api_error"), *_server_not_verified("api_access")]
    return [*checks, _client_check("api.access", "ok"), *server]


def _client_check(check_id: str, result: str, *, detail: str | None = None) -> Check:
    return Check(check_id, CHECK_PARTS[check_id], result, CLIENT_MESSAGES[(check_id, result)], detail=detail)


def _not_verified(check_id: str, reason: str) -> Check:
    return Check(check_id, CHECK_PARTS[check_id], "not_verified", NOT_VERIFIED_MESSAGES[reason])


def _server_not_verified(reason: str) -> list[Check]:
    return [_not_verified(check_id, reason) for check_id in SERVER_CHECK_IDS]


def _access_failure(reason: str) -> Check:
    return Check("api.access", CHECK_PARTS["api.access"], "failure", API_ACCESS_FAILURE_MESSAGES[reason])


def _access_failure_reason(status: int) -> str:
    """401 → token recusado; 403 → sem permissão; o resto (5xx, 404, resposta inesperada) → erro da API."""
    if status == 401:
        return "token_refused"
    if status == 403:
        return "forbidden"
    return "api_error"


def _database_checks(health: Mapping[str, object]) -> list[Check]:
    """`database.reachable` e `database.schema` pelo corpo do `health` (200 ou 503): o banco fora deixa as
    migrações "não verificado"; com o banco no ar, a versão do esquema tem de ser a esperada."""
    if health.get("database") != "ok":
        return [_client_check("database.reachable", "failure"), _not_verified("database.schema", "database")]
    schema = health.get("schema_version")
    expected = health.get("expected_schema_version")
    schema_ok = isinstance(schema, str) and isinstance(expected, str) and schema == expected
    return [_client_check("database.reachable", "ok"),
            _client_check("database.schema", "ok" if schema_ok else "failure")]


_DOCTOR_BODY_KEYS = frozenset({"checked_at", "checks"})
_SERVER_CHECK_KEYS = frozenset({"id", "result", "message", "offering", "count"})
# As 6 globais do servidor, cada uma exatamente uma vez no corpo.
_SERVER_GLOBAL_IDS = ("bots.master_key", "tasks.overdue", "clock.skew", "backup.age", "smtp.server",
                      "retention.overdue")
_CHECKED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_OFFERING_CODE = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")


def _server_checks(body: object) -> list[Check] | None:
    """As verificações do servidor na ordem em que vieram, validadas pelo contrato do `system.doctor`;
    `None` se o corpo foge dele: chaves a mais ou a menos, `checked_at` fora do
    formato, par `(id, result)` fora das duas tabelas ou mensagem fora da aceita (`SERVER_MESSAGES` por igualdade,
    `SERVER_REASON_MESSAGES` por pertinência, o modelo da idade só formatado com o `count`), `offering` ou
    `count` fora do lugar (`_offering_and_count_ok`), cada global diferente de exatamente uma vez, ou uma `bot.*`
    repetida para o mesmo oferecimento. Não confere a ordem nem a coerência entre as `bot.*` de um oferecimento (os
    testes montam corpos sem ela)."""
    if not isinstance(body, Mapping) or body.keys() != _DOCTOR_BODY_KEYS:
        return None
    checked_at, items = body["checked_at"], body["checks"]
    if not isinstance(checked_at, str) or _CHECKED_AT.fullmatch(checked_at) is None or not isinstance(items, list):
        return None
    checks: list[Check] = []
    seen: set[tuple[str, str | None]] = set()
    for item in items:
        if not isinstance(item, Mapping) or item.keys() != _SERVER_CHECK_KEYS:
            return None
        check_id, result, message = item["id"], item["result"], item["message"]
        offering, count = item["offering"], item["count"]
        if not isinstance(check_id, str) or not isinstance(result, str) or not isinstance(message, str):
            return None
        templated = _accepted_message(check_id, result, message, count)
        if templated is None:
            return None
        if not _offering_and_count_ok(check_id, result, offering, count, templated=templated):
            return None
        # As globais contam por id (uma vez cada, mesmo com `offering`, como `retention.overdue`); as `bot.*`, por
        # oferecimento.
        key = (check_id, offering if check_id.startswith("bot.") else None)
        if key in seen:
            return None
        seen.add(key)
        checks.append(Check(check_id, CHECK_PARTS[check_id], result, message, offering=offering, count=count))
    if not all((check_id, None) in seen for check_id in _SERVER_GLOBAL_IDS):
        return None
    return checks


def _accepted_message(check_id: str, result: str, message: str, count: object) -> bool | None:
    """A mensagem é a aceita para `(id, result)`? `None` se não; `True` se casou com o modelo da idade (só igual a
    `BACKUP_AGE_TEMPLATE.format(hours=count)`, com `count` inteiro não negativo); `False` se casou com uma
    mensagem fixa. Os pares das duas tabelas não se repetem."""
    pair = (check_id, result)
    if pair in SERVER_MESSAGES:
        return False if SERVER_MESSAGES[pair] == message else None
    accepted = SERVER_REASON_MESSAGES.get(pair)
    if accepted is None:
        return None
    if message in accepted and "{hours}" not in message:
        return False
    if BACKUP_AGE_TEMPLATE in accepted and _non_negative_int(count) and message == BACKUP_AGE_TEMPLATE.format(
            hours=count):
        return True
    return None


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _offering_and_count_ok(check_id: str, result: str, offering: object, count: object, *,
                           templated: bool) -> bool:
    """`offering` e `count` no lugar:
    - `bot.*`: `offering` com o padrão do código, sem `count`;
    - `tasks.overdue`: sem `offering`; `count` 0 com "ok" e positivo com "warning";
    - `backup.age`: sem `offering`; `count` (as horas) só com o modelo da idade, `null` com as mensagens fixas;
    - `retention.overdue`: "ok" com `count` 0 e sem `offering`; "warning" com `count ≥ 1` e o código do mais antigo;
    - as demais: nem `offering` nem `count`."""
    if check_id.startswith("bot."):
        return _is_offering_code(offering) and count is None
    if check_id == "retention.overdue":
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            return False
        if result == "ok":
            return count == 0 and offering is None
        return count >= 1 and _is_offering_code(offering)
    if offering is not None:
        return False
    if check_id == "tasks.overdue":
        return _non_negative_int(count) and (count == 0) == (result == "ok")
    if check_id == "backup.age":
        return templated or count is None
    return count is None


def _is_offering_code(value: object) -> bool:
    return isinstance(value, str) and _OFFERING_CODE.fullmatch(value) is not None


def collect_local_checks(stack: Stack | None, profile: EffectiveProfile, *,
                         stack_dir_error: StackError | None, env_file_present: bool | None,
                         api_reachable: bool) -> list[Check]:
    """A parte local sobre o `Stack`; "não se aplica" com perfil remoto. Só lê a pilha
    (`docker_available`, `compose_version`, `snapshot`, `engine_image_id`), nunca a muda.

    Dependências, na ordem de `LOCAL_CHECK_IDS`:
    - perfil remoto → as dez "não se aplica", sem tocar a pilha;
    - sem diretório da pilha (`stack` é `None`, `stack_dir_error` = `StackDirNotFound`) → `stack.directory`
      falha e as demais "não verificado" (`stack_directory`);
    - [I] o alvo do Docker não local (`stack` é `None` com outro `StackError`, a `StackTargetRefused`):
      `docker.reachable` falha com a mensagem fixa do erro; o diretório e o `.env` verificados; o
      resto "não verificado" (`docker_target`);
    - Docker indisponível → `docker.reachable` falha; o que depende do Docker "não verificado"
      (`docker_unavailable`); o diretório e o `.env` (só existência) verificados;
    - sem `.env` → `stack.env_file` falha; os serviços e `api.owner` "não verificado" (`env_file`),
      sem nenhum `snapshot` (o prefixo do Compose sempre leva `--env-file`); a versão do Compose e a imagem
      do motor verificadas (não usam o prefixo);
    - `api.owner`: o instantâneo diz que a api é do projeto (`local_commands.api_owned`, que aplica a
      regra do loopback e da porta quando o `ComposeStack` deixa `api_owned` em `None`), o serviço `api`
      "ok" e a porta publicada igual à do `api_url` do perfil; a API inalcançável → "não verificado"
      (`api_unreachable`).
    """
    if profile.kind != "local":
        return [Check(check_id, CHECK_PARTS[check_id], "not_applicable", NOT_APPLICABLE_REMOTE)
                for check_id in LOCAL_CHECK_IDS]

    if stack is None:
        if stack_dir_error is None or isinstance(stack_dir_error, StackDirNotFound):
            return [_client_check(check_id, "failure") if check_id == "stack.directory"
                    else _not_verified(check_id, "stack_directory") for check_id in LOCAL_CHECK_IDS]
        # O diretório foi achado, mas o Docker aponta para fora deste computador: nenhuma chamada a ele.
        return [Check("docker.reachable", CHECK_PARTS["docker.reachable"], "failure", stack_dir_error.message),
                _not_verified("compose.version", "docker_target"),
                *_stack_files_checks(env_file_present),
                *(_not_verified(check_id, "docker_target") for check_id in LOCAL_CHECK_IDS[4:])]

    if not _docker_available(stack):
        return [_client_check("docker.reachable", "failure"),
                _not_verified("compose.version", "docker_unavailable"),
                *_stack_files_checks(env_file_present),
                *(_not_verified(check_id, "docker_unavailable") for check_id in LOCAL_CHECK_IDS[4:])]

    checks = [_client_check("docker.reachable", "ok"), _compose_version_check(stack),
              *_stack_files_checks(env_file_present)]

    snapshot: StackSnapshot | None = None
    reason = "stack_state"
    if env_file_present is False:
        reason = "env_file"
    else:
        try:
            snapshot = stack.snapshot()
        except DockerUnavailable:
            reason = "docker_unavailable"
        except StackEnvMissing:
            reason = "env_file"
        except StackError:
            # [I] Outra falha do Compose (variável ausente, saída ilegível…): sem estado e sem repassar o erro.
            reason = "stack_state"

    if snapshot is None:
        checks += [_not_verified(f"service.{service}", reason) for service in SERVICES]
    else:
        by_name = {state.service: state for state in snapshot.services}
        checks += [_client_check(f"service.{service}", "ok" if _service_fine(by_name.get(service)) else "failure")
                   for service in SERVICES]

    checks.append(_engine_image_check(stack))

    if snapshot is None:
        checks.append(_not_verified("api.owner", reason))
    elif not api_reachable:
        checks.append(_not_verified("api.owner", "api_unreachable"))
    else:
        checks.append(_client_check("api.owner", "ok" if _api_owner_ok(snapshot, profile) else "failure"))
    return checks


def _stack_files_checks(env_file_present: bool | None) -> list[Check]:
    """`stack.directory` (achado: há `Stack`) e `stack.env_file` (só a existência, sem ler)."""
    return [_client_check("stack.directory", "ok"),
            _client_check("stack.env_file", "failure" if env_file_present is False else "ok")]


def _docker_available(stack: Stack) -> bool:
    try:
        return bool(stack.docker_available())
    except StackError:
        return False


def _compose_version_check(stack: Stack) -> Check:
    """"ok" na 2.27 ou mais nova; "aviso" abaixo dela; "falha" sem versão legível."""
    try:
        version = stack.compose_version()
    except StackError:
        version = None
    if version is None:
        return _client_check("compose.version", "failure")
    return _client_check("compose.version", "ok" if tuple(version) >= MIN_COMPOSE else "warning")


# Os 12 primeiros hexadecimais depois de `sha256:`.
_IMAGE_ID_PATTERN = re.compile(r"(?:sha256:)?([0-9a-f]{12})[0-9a-f]*")


def _engine_image_check(stack: Stack) -> Check:
    """`engine.image`: ausente → falha; presente → ok com a versão no `detail` (só os 12 hexadecimais; um ID
    fora do formato não vai à saída)."""
    try:
        image_id = stack.engine_image_id()
    except StackError:
        image_id = None
    if not image_id:
        return _client_check("engine.image", "failure")
    match = _IMAGE_ID_PATTERN.fullmatch(image_id.strip().lower())
    return _client_check("engine.image", "ok", detail=match.group(1) if match is not None else None)


def _service_fine(state: ServiceState | None) -> bool:
    """Serviço "ok"; sem linha no instantâneo, fora."""
    return state is not None and service_ok(state)


def _api_owner_ok(snapshot: StackSnapshot, profile: EffectiveProfile) -> bool:
    """A api do projeto (`api_owned`), o serviço `api` "ok" e a porta publicada igual à do perfil."""
    api = next((state for state in snapshot.services if state.service == "api"), None)
    port = profile_port(profile)
    return (api_owned(snapshot, profile) and _service_fine(api) and port is not None
            and snapshot.api_port == port)


def check_profile_file(path: Path, *, platform: str = os.name,
                       mode: Callable[[Path], int] = lambda p: p.stat().st_mode) -> list[Check]:
    """`profile.permissions` e `profile.git` do arquivo de perfil (sem `chmod`, sem rodar `git`, sem o
    caminho na mensagem); sem arquivo, as duas "não se aplica"."""
    if not _is_file(path):
        return [Check(check_id, CHECK_PARTS[check_id], "not_applicable", NOT_APPLICABLE_NO_PROFILE_FILE)
                for check_id in PROFILE_CHECK_IDS]
    return [permission_result(mode(path), platform),
            _client_check("profile.git", "warning" if _inside_git_copy(path) else "ok")]


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _inside_git_copy(path: Path) -> bool:
    """Algum ancestral do caminho resolvido tem `.git` (diretório, ou arquivo como numa worktree ou num
    submódulo), sem rodar `git`."""
    try:
        parents = path.resolve().parents
    except OSError:
        parents = path.absolute().parents
    for parent in parents:
        try:
            if (parent / ".git").exists():
                return True
        except OSError:
            continue
    return False


# Bits de acesso do grupo e de outros (`stat.S_IRWXG | stat.S_IRWXO`). [I] Qualquer um deles dá o aviso: o
# arquivo guarda o token, e a escrita por outros também permitiria trocar o endereço da API.
_GROUP_OTHER_BITS = 0o077


def permission_result(st_mode: int, platform: str) -> Check:
    """`profile.permissions` pelo modo dado: 0o600 → ok; legível por grupo ou outros → aviso; `nt` →
    não verificado."""
    if platform == "nt":
        return _client_check("profile.permissions", "not_verified")
    return _client_check("profile.permissions", "warning" if st_mode & _GROUP_OTHER_BITS else "ok")


def exit_code(checks: list[Check]) -> int:
    """0 sem `failure`; 1 com ao menos uma."""
    return 1 if any(check.result == "failure" for check in checks) else 0


# --------------------------------------------------------------------------------------------------
# As 2 verificações do host
# --------------------------------------------------------------------------------------------------

# O nome de um volume anônimo do Docker: 64 hexadecimais.
_ANONYMOUS_VOLUME_NAME = re.compile(r"[0-9a-f]{64}")
# Os motivos da saída antecipada das locais que deixam as 2 do host "não verificado" sem chamar o probe.
HOST_BLOCKED_REASONS = ("stack_directory", "docker_target", "env_file")


def postgres_volume_check(host_probe: HostProbe, project: str) -> Check:
    """`postgres.volume` pelas montagens do Postgres do `project`: só a montagem no destino
    `/var/lib/postgresql` decide ([I] as outras montagens não mudam o resultado).
    - `postgres_mounts` `None` (serviço ausente ou parado) → "não verificado" `postgres_not_running`;
    - nada no destino (inclusive só em `/var/lib/postgresql/data`, ou a lista vazia) → "falha" `wrong_path`;
    - `volume` com o nome `<projeto>_pgdata` → "ok"; com nome de 64 hex → "falha" `anonymous`; com outro nome →
      "falha" `other_volume` (**[I]**);
    - `bind` → "falha" `bind`.
    [I] Outro tipo no destino (`tmpfs`, `npipe`, …) não é volume do Postgres no lugar certo: "falha" `wrong_path`; um
    `volume` sem nome conta como anônimo. Nenhum valor do probe (nome do volume, caminho) entra na mensagem."""
    mounts = host_probe.postgres_mounts(project)
    if mounts is None:
        return _not_verified("postgres.volume", "postgres_not_running")
    mount = next((m for m in mounts if m.destination == POSTGRES_DATA_DESTINATION), None)
    if mount is None:
        reason = "wrong_path"
    elif mount.type == "volume":
        if mount.name == f"{project}{POSTGRES_VOLUME_SUFFIX}":
            reason = "ok"
        elif mount.name is None or _ANONYMOUS_VOLUME_NAME.fullmatch(mount.name) is not None:
            reason = "anonymous"
        else:
            reason = "other_volume"
    elif mount.type == "bind":
        reason = "bind"
    else:
        reason = "wrong_path"
    return Check("postgres.volume", CHECK_PARTS["postgres.volume"], "ok" if reason == "ok" else "failure",
                 POSTGRES_VOLUME_MESSAGES[reason])


_GIB = 1024 ** 3


def disk_space_check(host_probe: HostProbe, settings: LocalDoctorSettings) -> Check:
    """`disk.space` pelo pior alvo (`docker_root_dir()` e `settings.backup_dir`):
    - `docker_root_dir` `None` (Docker inalcançável) → "não verificado" `docker_unavailable`;
    - algum alvo sem leitura neste host (`disk_usage` `None`, como o `DockerRootDir` do Docker Desktop) → "não
      verificado" `unreachable_path`;
    - senão, o alvo de menor fração livre (`free / total`): ≥ aviso → "ok"; ≥ falha → "aviso"; abaixo → "falha",
      com `DISK_SPACE_TEMPLATE` (`{percent}` inteiro pelo piso e `{gib}` livre com uma casa), sem caminho.
    As comparações usam a fração exata (`Fraction`), sem arredondar. [I] Um disco com `total` 0 conta como sem
    leitura (`unreachable_path`)."""
    root_dir = host_probe.docker_root_dir()
    if root_dir is None:
        return _not_verified("disk.space", "docker_unavailable")
    targets = [root_dir] if settings.backup_dir is None else [root_dir, settings.backup_dir]
    usages: list[DiskUsage] = []
    for target in targets:
        usage = host_probe.disk_usage(target)
        if usage is None or usage.total <= 0:
            return _not_verified("disk.space", "unreachable_path")
        usages.append(usage)
    worst = min(usages, key=lambda u: Fraction(max(u.free, 0), u.total))
    free = max(worst.free, 0)
    percent_free = Fraction(free * 100, worst.total)
    if percent_free >= Fraction(settings.disk_warning_percent):
        result = "ok"
    elif percent_free >= Fraction(settings.disk_failure_percent):
        result = "warning"
    else:
        result = "failure"
    message = DISK_SPACE_TEMPLATE.format(percent=math.floor(percent_free), gib=f"{free / _GIB:.1f}")
    return Check("disk.space", CHECK_PARTS["disk.space"], result, message)


def collect_host_checks(profile: EffectiveProfile, *, host_probe: HostProbe | None,
                        settings: LocalDoctorSettings, blocked: str | None = None) -> list[Check]:
    """As 2 do host, na ordem de `HOST_CHECK_IDS`: perfil remoto → "não se aplica"
    (`NOT_APPLICABLE_REMOTE_VM`); `blocked` (o motivo da saída antecipada das locais: `stack_directory`,
    `docker_target` ou `env_file`) → as duas "não verificado" com esse motivo, sem chamar o probe;
    `host_probe=None` → as duas "não verificado" (`no_host_probe`). Senão, `postgres_volume_check` pelo
    projeto do perfil e `disk_space_check`, independentes."""
    if profile.kind != "local":
        return [Check(check_id, CHECK_PARTS[check_id], "not_applicable", NOT_APPLICABLE_REMOTE_VM)
                for check_id in HOST_CHECK_IDS]
    if blocked is not None:
        if blocked not in HOST_BLOCKED_REASONS:
            raise ValueError("Motivo de bloqueio das verificações do host fora da lista.")
        return [_not_verified(check_id, blocked) for check_id in HOST_CHECK_IDS]
    if host_probe is None:
        return [_not_verified(check_id, "no_host_probe") for check_id in HOST_CHECK_IDS]
    return [postgres_volume_check(host_probe, profile.compose_project), disk_space_check(host_probe, settings)]
