"""Isolamento do motor no Compose (disparo e resposta): a rede interna, os limites, os segredos e as
montagens do serviço `engine`.

O motor roda o código Python dos autores das questões. Este teste lê o `compose.yaml` **real**
com `yaml.safe_load`, no molde de `test_deploy_files.py` (congelado), **sem** `docker compose config`,
que imprime os segredos interpolados (`compose.yaml`, cabeçalho). A configuração efetiva do daemon
(`docker inspect`) fica no teste de sistema `tests/system/test_engine_system.py`.

As funções puras normalizam as formas curtas e longas do Compose:
- `_engine_networks(compose)`: as redes do serviço `engine` (lista ou mapa; `default` quando a chave
  falta), com a definição do topo de cada uma;
- `_mounts(svc)`: as montagens (`src:dst[:opções]` e a forma longa com `read_only` e `volume.nocopy`);
- `_interpolated_names(node)`: os nomes de variável de toda interpolação `${…}` (com aninhamento);
- `engine_isolation_problems(compose)`: a checagem inteira, com uma mensagem estável por regra, sempre
  com o prefixo `engine:`.

As 18 regras:
1. o serviço `engine` existe;
2. só as chaves de `ENGINE_ALLOWED_KEYS` (qualquer outra, como `ports`, `expose`, `network_mode`,
   `privileged`, `cap_add`, `pid`, `ipc`, `devices`, `env_file`, `extra_hosts`, `dns`, `sysctls`,
   `userns_mode`, `volumes_from`, `secrets`, `configs`, `deploy`, `links`, é problema);
3. `build == {"context": ".", "dockerfile": "engine/Dockerfile"}` sem `args`, ou `image` com
   `@sha256:<64 hex>`;
4. `networks` explícito, não vazio e sem `default`; cada rede no topo com `internal: true`, sem
   `external`, com `driver` ausente ou `bridge`, com `com.docker.network.bridge.inhibit_ipv4: "true"`
   e sem `enable_ipv6: true` (sem o `inhibit_ipv4`, o gateway da rede interna alcança os
   serviços do host);
5. `read_only: true`;
6. `tmpfs` com exatamente `/tmp`, com `size=`;
7. `user: "65534:65534"`;
8. `cap_drop: [ALL]`;
9. `security_opt: ["no-new-privileges:true"]` (nada de `seccomp`/`apparmor` `unconfined`);
10. `pids_limit`, `mem_limit` e `cpus` presentes, cada um literal ou `${CQ_ENGINE_…:-<padrão>}`, com o
    `pids_limit` diferente de `-1` e de `0`;
11. exatamente um volume: `repos` em `/repos`, `read_only` e `nocopy` (nenhum bind, nenhum
    `/var/run/docker.sock`, nenhum `pgdata`), e o volume nomeado `repos` declarado no topo, sem opções;
12. `environment` só com chaves `ENGINE_*` que não sejam nome de segredo, e toda interpolação do
    serviço só com nomes `CQ_ENGINE_*` (nenhum segredo chega ao motor);
13. log `json-file` (ou `local`) com `max-size` de no máximo 10 MiB e `max-file` de no máximo 3 (o log
    do motor é sensível e tem rotação curta);
14. `restart: unless-stopped` (a queda do motor pelo código da questão reinicia o contêiner);
15. o `healthcheck.test` no loopback (`127.0.0.1`) e em `/v1/health`;
16. nos outros serviços, só o `worker` cita uma rede do motor, e cita exatamente `default` e as redes
    do motor; a `api` e o `postgres` não citam nenhuma (nem por `network_mode: service:engine`);
17. o `worker` monta `repos` em `/repos` com escrita; a `api` e o `postgres` não montam `repos`;
18. o `worker` tem `CQ_ENGINE_URL` com o host `engine`; a `api` (e o `postgres`) não têm `CQ_ENGINE_URL`
    (a API nunca chama o motor).

**Controle positivo por mutação**: `MUTATIONS` (rótulo, mutação sobre uma cópia
profunda do `compose.yaml` real, fragmento esperado na mensagem). O arquivo real tem **zero**
problemas, e cada mutação, aplicada **sozinha**, precisa produzir o problema esperado (100% das
mutações acusadas). Mais o autoteste das funções puras com as formas curtas e longas.

**Uma única função de teste**, `test_engine_isolation_in_compose`, que começa exigindo o serviço
`engine`: no esqueleto, o `compose.yaml` ainda não tem o
serviço, a rede nem o volume (a implementação vem depois), e a função falha por isso (serviço ausente).
"""

from __future__ import annotations

import copy
import pathlib
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"

ENGINE = "engine"
ENGINE_NETWORK = "engine"  # a rede interna do motor tem o mesmo nome do serviço
OTHER_SERVICES = ("api", "worker", "postgres")
ENGINE_ALLOWED_KEYS = frozenset({
    "build", "image", "environment", "networks", "read_only", "tmpfs", "user", "cap_drop", "security_opt",
    "pids_limit", "mem_limit", "cpus", "volumes", "logging", "healthcheck", "restart"})
ENGINE_BUILD = {"context": ".", "dockerfile": "engine/Dockerfile"}
INHIBIT_IPV4 = "com.docker.network.bridge.inhibit_ipv4"
ENGINE_USER = "65534:65534"
NO_NEW_PRIVILEGES = ["no-new-privileges:true"]
LOG_DRIVERS = frozenset({"json-file", "local"})
MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_LOG_FILES = 3
REPOS_VOLUME = "repos"
REPOS_TARGET = "/repos"
FORBIDDEN_SOURCES = ("/var/run/docker.sock", "pgdata")
ENGINE_VAR_PREFIX = "CQ_ENGINE_"
ENGINE_ENV_PREFIX = "ENGINE_"

# O mesmo detector de nome de segredo de `test_deploy_files.py` (congelado).
SECRET_NAME = re.compile(r"PASSWORD|TOKEN|SECRET|KEY", re.IGNORECASE)
PINNED_IMAGE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
ENGINE_DEFAULT = re.compile(r"\$\{CQ_ENGINE_[A-Z0-9_]+:-([^{}$]+)\}")
SIZE = re.compile(r"(\d+)([bkmg]?)", re.IGNORECASE)
SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

# Mensagens estáveis, uma por regra (os fragmentos das mutações vêm daqui).
MSG_MISSING = "engine: serviço `engine` ausente"
MSG_KEY = "engine: chave não permitida: {key}"
MSG_BUILD = ("engine: build precisa ser {context: ., dockerfile: engine/Dockerfile} sem args, "
             "ou image fixada por @sha256")
MSG_NETWORKS = "engine: `networks` precisa ser explícito, não vazio e sem `default`"
MSG_NET_UNDECLARED = "engine: rede `{net}` não declarada no topo"
MSG_NET_INTERNAL = "engine: rede `{net}` sem internal: true"
MSG_NET_EXTERNAL = "engine: rede `{net}` external"
MSG_NET_DRIVER = "engine: rede `{net}` com driver diferente de bridge"
MSG_NET_INHIBIT = "engine: rede `{net}` sem inhibit_ipv4 \"true\""
MSG_NET_IPV6 = "engine: rede `{net}` com enable_ipv6"
MSG_READ_ONLY = "engine: read_only precisa ser true"
MSG_TMPFS = "engine: tmpfs precisa ser exatamente /tmp com size="
MSG_USER = "engine: user precisa ser \"65534:65534\""
MSG_CAP_DROP = "engine: cap_drop precisa ser [ALL]"
MSG_SECURITY_OPT = "engine: security_opt precisa ser [\"no-new-privileges:true\"]"
MSG_LIMIT = "engine: {key} ausente ou inválido"
MSG_VOLUMES = "engine: volumes precisa ser só repos em /repos, read_only e nocopy"
MSG_VOLUME_FORBIDDEN = "engine: montagem proibida: {source}"
MSG_REPOS_TOP = "engine: volume nomeado `repos` precisa estar no topo, sem opções"
MSG_ENV_KEY = "engine: variável de ambiente fora do previsto: {key}"
MSG_INTERPOLATION = "engine: interpolação fora de CQ_ENGINE_*: {name}"
MSG_LOGGING = "engine: logging precisa ser json-file ou local com max-size <= 10m e max-file <= 3"
MSG_RESTART = "engine: restart precisa ser unless-stopped"
MSG_HEALTHCHECK = "engine: healthcheck precisa consultar 127.0.0.1 em /v1/health"
MSG_OTHER_NETWORK = "engine: {service} cita a rede do motor"
MSG_WORKER_NETWORKS = "engine: worker precisa estar exatamente em default e nas redes do motor"
MSG_WORKER_REPOS = "engine: worker precisa montar repos em /repos com escrita"
MSG_OTHER_REPOS = "engine: {service} monta o volume repos"
MSG_WORKER_URL = "engine: worker precisa de CQ_ENGINE_URL com o host engine"
MSG_OTHER_URL = "engine: {service} menciona CQ_ENGINE_URL"


# ---------------------------------------------------------------------------------------------
# Funções puras de leitura
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Mount:
    """Uma montagem normalizada (forma curta ou longa)."""

    type: str
    source: str | None
    target: str
    read_only: bool
    nocopy: bool = False


def _services(compose: Any) -> dict:
    services = compose.get("services") if isinstance(compose, dict) else None
    return services if isinstance(services, dict) else {}


def _service_networks(svc: Any) -> list[str] | None:
    """Os nomes das redes de um serviço (lista ou mapa); `None` quando a chave falta."""
    if not isinstance(svc, dict) or "networks" not in svc:
        return None
    nets = svc["networks"]
    if isinstance(nets, dict):
        return [str(n) for n in nets]
    if isinstance(nets, list):
        return [str(n) for n in nets]
    return []


def _engine_networks(compose: Any) -> dict[str, dict | None]:
    """As redes do serviço `engine`, cada uma com a definição do topo (`None` se não declarada; `{}`
    se declarada sem opções). Sem a chave `networks`, o Compose põe o serviço na `default`."""
    svc = _services(compose).get(ENGINE)
    names = _service_networks(svc)
    if names is None:
        names = ["default"]
    top = compose.get("networks") if isinstance(compose, dict) else None
    top = top if isinstance(top, dict) else {}
    result: dict[str, dict | None] = {}
    for name in names:
        if name in top:
            definition = top[name]
            result[name] = definition if isinstance(definition, dict) else {}
        else:
            result[name] = None
    return result


def _mounts(svc: Any) -> list[Mount]:
    """As montagens do serviço: `src:dst[:opções]` (bind quando a origem é um caminho) e a forma
    longa (`type`, `source`, `target`, `read_only`, `volume.nocopy`)."""
    if not isinstance(svc, dict):
        return []
    mounts: list[Mount] = []
    for item in svc.get("volumes") or []:
        if isinstance(item, dict):
            mtype = str(item.get("type", "volume"))
            volume_opts = item.get("volume") if isinstance(item.get("volume"), dict) else {}
            source = item.get("source")
            mounts.append(Mount(mtype, None if source is None else str(source),
                                str(item.get("target", "")).rstrip("/") or "/",
                                item.get("read_only") is True, volume_opts.get("nocopy") is True))
            continue
        parts = str(item).split(":")
        if len(parts) == 1:
            mounts.append(Mount("volume", None, parts[0].rstrip("/") or "/", False))
            continue
        source, target = parts[0], parts[1]
        options = parts[2].split(",") if len(parts) > 2 else []
        mtype = "bind" if source.startswith(("/", ".", "~")) else "volume"
        mounts.append(Mount(mtype, source, target.rstrip("/") or "/", "ro" in options, "nocopy" in options))
    return mounts


def _mask(text: str) -> list[str]:
    """As interpolações `${…}` de `text`, com aninhamento (a lógica de máscara de
    `test_deploy_files.py`); `$$` é o cifrão literal."""
    found: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            i += 2
            continue
        if text.startswith("${", i):
            depth, j = 0, i
            while j < len(text):
                if text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if text[j] == "}":
                    depth -= 1
                    j += 1
                    if depth == 0:
                        break
                    continue
                j += 1
            found.append(text[i:j])
            i = j
            continue
        i += 1
    return found


def _interpolated_names(node: Any) -> set[str]:
    """Os nomes de variável de toda interpolação `${…}` em `node` (chaves e valores, recursivo),
    inclusive as aninhadas no padrão (`${A:-${B}}` → `{A, B}`)."""
    names: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            names |= _interpolated_names(key)
            names |= _interpolated_names(value)
    elif isinstance(node, list):
        for item in node:
            names |= _interpolated_names(item)
    elif isinstance(node, str):
        for expr in _mask(node):
            inner = expr[2:-1]
            match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", inner)
            if match:
                names.add(match.group(0))
            names |= _interpolated_names(inner[match.end() if match else 0:])
    return names


def _tmpfs_entries(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return ["<malformado>"]


def _size_bytes(value: Any) -> int | None:
    match = SIZE.fullmatch(str(value).strip()) if value is not None else None
    if not match:
        return None
    return int(match.group(1)) * SIZE_UNITS[match.group(2).lower()]


def _limit_ok(key: str, value: Any) -> bool:
    """`pids_limit`, `mem_limit` e `cpus`: literal positivo ou `${CQ_ENGINE_…:-<padrão>}`."""
    if value is None or isinstance(value, bool):
        return False
    text = str(value).strip()
    match = ENGINE_DEFAULT.fullmatch(text)
    if match:
        text = match.group(1).strip()
    elif "$" in text:
        return False
    if key == "pids_limit":
        return re.fullmatch(r"\d+", text) is not None and int(text) > 0
    if key == "cpus":
        return re.fullmatch(r"\d+(\.\d+)?", text) is not None and float(text) > 0
    return re.fullmatch(r"\d+(\.\d+)?[bkmg]?", text, re.IGNORECASE) is not None and float(
        re.sub(r"[bkmg]$", "", text, flags=re.IGNORECASE)) > 0


def _env(svc: Any) -> dict:
    env = svc.get("environment") if isinstance(svc, dict) else None
    if isinstance(env, dict):
        return env
    if isinstance(env, list):
        return {str(item).partition("=")[0]: str(item).partition("=")[2] for item in env}
    return {}


def _healthcheck_text(svc: dict) -> str:
    test = (svc.get("healthcheck") or {}).get("test") if isinstance(svc.get("healthcheck"), dict) else None
    if isinstance(test, list):
        return " ".join(str(t) for t in test)
    return str(test or "")


# ---------------------------------------------------------------------------------------------
# A checagem
# ---------------------------------------------------------------------------------------------


def _check_networks(compose: dict, svc: dict) -> list[str]:
    problems: list[str] = []
    names = _service_networks(svc)
    if not names or "default" in names:
        problems.append(MSG_NETWORKS)
    for net, definition in _engine_networks(compose).items():
        if net == "default" and "default" in (names or ["default"]):
            continue  # já relatado acima
        if definition is None:
            problems.append(MSG_NET_UNDECLARED.format(net=net))
            continue
        if definition.get("internal") is not True:
            problems.append(MSG_NET_INTERNAL.format(net=net))
        if definition.get("external"):
            problems.append(MSG_NET_EXTERNAL.format(net=net))
        if definition.get("driver", "bridge") != "bridge":
            problems.append(MSG_NET_DRIVER.format(net=net))
        opts = definition.get("driver_opts") if isinstance(definition.get("driver_opts"), dict) else {}
        if opts.get(INHIBIT_IPV4) != "true":
            problems.append(MSG_NET_INHIBIT.format(net=net))
        if definition.get("enable_ipv6") is True:
            problems.append(MSG_NET_IPV6.format(net=net))
    return problems


def _check_volumes(compose: dict, svc: dict) -> list[str]:
    problems: list[str] = []
    mounts = _mounts(svc)
    for mount in mounts:
        if mount.type == "bind":
            problems.append(MSG_VOLUME_FORBIDDEN.format(source=mount.source))
        elif mount.source is not None and any(mount.source == s or mount.source.startswith(s + "/")
                                              for s in FORBIDDEN_SOURCES):
            problems.append(MSG_VOLUME_FORBIDDEN.format(source=mount.source))
    if mounts != [Mount("volume", REPOS_VOLUME, REPOS_TARGET, True, True)]:
        problems.append(MSG_VOLUMES)
    top = compose.get("volumes")
    if not isinstance(top, dict) or REPOS_VOLUME not in top or top[REPOS_VOLUME] not in (None, {}):
        problems.append(MSG_REPOS_TOP)
    return problems


def _check_env(svc: dict) -> list[str]:
    problems: list[str] = []
    raw_env = svc.get("environment")
    if raw_env is not None and not isinstance(raw_env, (dict, list)):
        problems.append(MSG_ENV_KEY.format(key="<malformado>"))
    for key in _env(svc):
        if not str(key).startswith(ENGINE_ENV_PREFIX) or SECRET_NAME.search(str(key)):
            problems.append(MSG_ENV_KEY.format(key=key))
    for name in sorted(_interpolated_names(svc)):
        if not name.startswith(ENGINE_VAR_PREFIX):
            problems.append(MSG_INTERPOLATION.format(name=name))
    return problems


def _check_logging(svc: dict) -> list[str]:
    logging = svc.get("logging")
    if not isinstance(logging, dict) or logging.get("driver") not in LOG_DRIVERS:
        return [MSG_LOGGING]
    options = logging.get("options") if isinstance(logging.get("options"), dict) else {}
    size = _size_bytes(options.get("max-size"))
    files = str(options.get("max-file", "")).strip()
    if size is None or not 0 < size <= MAX_LOG_BYTES or not files.isdigit() or not 1 <= int(files) <= MAX_LOG_FILES:
        return [MSG_LOGGING]
    return []


def _check_other_services(compose: dict) -> list[str]:
    problems: list[str] = []
    services = _services(compose)
    engine_nets = {n for n in _engine_networks(compose) if n != "default"}
    for name in OTHER_SERVICES:
        svc = services.get(name)
        if not isinstance(svc, dict):
            continue
        nets = set(_service_networks(svc) or [])
        mode = str(svc.get("network_mode", ""))
        cites = bool(nets & (engine_nets | {ENGINE_NETWORK})) or mode in (f"service:{ENGINE}", f"container:{ENGINE}")
        mounts = _mounts(svc)
        if name == "worker":
            if not engine_nets or nets != {"default"} | engine_nets or "network_mode" in svc:
                problems.append(MSG_WORKER_NETWORKS)
            if Mount("volume", REPOS_VOLUME, REPOS_TARGET, False, False) not in [
                    Mount(m.type, m.source, m.target, m.read_only, False) for m in mounts]:
                problems.append(MSG_WORKER_REPOS)
            url = _env(svc).get("CQ_ENGINE_URL")
            host = urllib.parse.urlsplit(str(url)).hostname if isinstance(url, str) else None
            if host != ENGINE:
                problems.append(MSG_WORKER_URL)
            continue
        if cites:
            problems.append(MSG_OTHER_NETWORK.format(service=name))
        if any(m.source == REPOS_VOLUME for m in mounts):
            problems.append(MSG_OTHER_REPOS.format(service=name))
        if "CQ_ENGINE_URL" in yaml.safe_dump(svc):
            problems.append(MSG_OTHER_URL.format(service=name))
    return problems


def engine_isolation_problems(compose: Any) -> list[str]:
    """As 18 regras do motor no `compose.yaml`; lista vazia = isolado."""
    svc = _services(compose).get(ENGINE)
    if not isinstance(svc, dict):
        return [MSG_MISSING]
    problems: list[str] = []
    # 2. lista de chaves permitidas.
    problems += [MSG_KEY.format(key=key) for key in sorted(map(str, svc)) if key not in ENGINE_ALLOWED_KEYS]
    # 3. imagem construída de engine/Dockerfile ou fixada por digest.
    build_ok = "build" not in svc or svc["build"] == ENGINE_BUILD
    image_ok = "image" not in svc or PINNED_IMAGE.fullmatch(str(svc["image"])) is not None
    if not (build_ok and image_ok and ("build" in svc or "image" in svc)):
        problems.append(MSG_BUILD)
    # 4. redes internas, sem `default`.
    problems += _check_networks(compose, svc)
    # 5 a 9. sistema de arquivos, usuário e privilégios.
    if svc.get("read_only") is not True:
        problems.append(MSG_READ_ONLY)
    tmpfs = _tmpfs_entries(svc.get("tmpfs"))
    if len(tmpfs) != 1 or tmpfs[0].partition(":")[0].rstrip("/") != "/tmp" or not any(
            opt.startswith("size=") for opt in tmpfs[0].partition(":")[2].split(",")):
        problems.append(MSG_TMPFS)
    if svc.get("user") != ENGINE_USER:
        problems.append(MSG_USER)
    if svc.get("cap_drop") != ["ALL"]:
        problems.append(MSG_CAP_DROP)
    if svc.get("security_opt") != NO_NEW_PRIVILEGES:
        problems.append(MSG_SECURITY_OPT)
    # 10. limites.
    for key in ("pids_limit", "mem_limit", "cpus"):
        if not _limit_ok(key, svc.get(key)):
            problems.append(MSG_LIMIT.format(key=key))
    # 11. volumes.
    problems += _check_volumes(compose, svc)
    # 12. ambiente e interpolações.
    problems += _check_env(svc)
    # 13 a 15. log, reinício e saúde.
    problems += _check_logging(svc)
    if svc.get("restart") != "unless-stopped":
        problems.append(MSG_RESTART)
    health = _healthcheck_text(svc)
    if "127.0.0.1" not in health or "/v1/health" not in health:
        problems.append(MSG_HEALTHCHECK)
    # 16 a 18. os outros serviços.
    problems += _check_other_services(compose)
    return problems


# ---------------------------------------------------------------------------------------------
# Mutações (controle positivo)
# ---------------------------------------------------------------------------------------------


def _engine(c: dict) -> dict:
    return c["services"][ENGINE]


def _svc_set(service: str, key: str, value: Any) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        c["services"][service][key] = copy.deepcopy(value)
    return mutate


def _svc_del(service: str, key: str) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        c["services"][service].pop(key, None)
    return mutate


def _net_set(key: str, value: Any) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        c["networks"][ENGINE_NETWORK][key] = value
    return mutate


def _net_del(key: str) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        c["networks"][ENGINE_NETWORK].pop(key, None)
    return mutate


def _drop_inhibit(c: dict) -> None:
    opts = c["networks"][ENGINE_NETWORK].get("driver_opts")
    if isinstance(opts, dict):
        opts.pop(INHIBIT_IPV4, None)


def _add_engine_volume(volume: Any) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        _engine(c).setdefault("volumes", []).append(copy.deepcopy(volume))
    return mutate


def _add_security_opt(opt: str) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        _engine(c)["security_opt"] = list(_engine(c).get("security_opt") or []) + [opt]
    return mutate


def _set_log_option(key: str, value: Any) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        options = _engine(c)["logging"].setdefault("options", {})
        if value is None:
            options.pop(key, None)
        else:
            options[key] = value
    return mutate


def _add_env(service: str, key: str, value: str) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        env = c["services"][service].get("environment")
        if not isinstance(env, dict):
            env = {}
            c["services"][service]["environment"] = env
        env[key] = value
    return mutate


def _worker_repos_read_only(c: dict) -> None:
    worker = c["services"]["worker"]
    kept = [v for v in worker.get("volumes") or []
            if not any(m.source == REPOS_VOLUME for m in _mounts({"volumes": [v]}))]
    worker["volumes"] = kept + [f"{REPOS_VOLUME}:{REPOS_TARGET}:ro"]


def _worker_env_drop(key: str) -> Callable[[dict], None]:
    def mutate(c: dict) -> None:
        env = c["services"]["worker"].get("environment")
        if isinstance(env, dict):
            env.pop(key, None)
    return mutate


# (rótulo, mutação, fragmento esperado numa mensagem): a lista mínima e as extras.
MUTATIONS: list[tuple[str, Callable[[dict], None], str]] = [
    # Rede (rede não interna, modo de rede próprio, porta publicada).
    ("sem networks (rede padrão)", _svc_del(ENGINE, "networks"), MSG_NETWORKS),
    ("networks [engine, default]", _svc_set(ENGINE, "networks", [ENGINE_NETWORK, "default"]), MSG_NETWORKS),
    ("networks vazio", _svc_set(ENGINE, "networks", []), MSG_NETWORKS),
    ("rede com internal: false", _net_set("internal", False), MSG_NET_INTERNAL.format(net=ENGINE_NETWORK)),
    ("rede sem internal", _net_del("internal"), MSG_NET_INTERNAL.format(net=ENGINE_NETWORK)),
    ("rede external: true", _net_set("external", True), MSG_NET_EXTERNAL.format(net=ENGINE_NETWORK)),
    ("rede sem inhibit_ipv4", _drop_inhibit, MSG_NET_INHIBIT.format(net=ENGINE_NETWORK)),
    ("rede com driver overlay", _net_set("driver", "overlay"), MSG_NET_DRIVER.format(net=ENGINE_NETWORK)),
    ("rede com enable_ipv6", _net_set("enable_ipv6", True), MSG_NET_IPV6.format(net=ENGINE_NETWORK)),
    ("network_mode: bridge", _svc_set(ENGINE, "network_mode", "bridge"), MSG_KEY.format(key="network_mode")),
    ("network_mode: host", _svc_set(ENGINE, "network_mode", "host"), MSG_KEY.format(key="network_mode")),
    ("network_mode: none", _svc_set(ENGINE, "network_mode", "none"), MSG_KEY.format(key="network_mode")),
    ("ports no loopback", _svc_set(ENGINE, "ports", ["127.0.0.1:8081:8080"]), MSG_KEY.format(key="ports")),
    ("ports em mapa", _svc_set(ENGINE, "ports", [{"target": 8080, "published": "8081", "host_ip": "127.0.0.1"}]),
     MSG_KEY.format(key="ports")),
    ("expose", _svc_set(ENGINE, "expose", ["8080"]), MSG_KEY.format(key="expose")),
    ("extra_hosts host-gateway", _svc_set(ENGINE, "extra_hosts", ["host.docker.internal:host-gateway"]),
     MSG_KEY.format(key="extra_hosts")),
    ("dns externo", _svc_set(ENGINE, "dns", ["1.1.1.1"]), MSG_KEY.format(key="dns")),
    # Gravação, usuário, capacidades e privilégios.
    ("read_only: false", _svc_set(ENGINE, "read_only", False), MSG_READ_ONLY),
    ("read_only ausente", _svc_del(ENGINE, "read_only"), MSG_READ_ONLY),
    ("tmpfs removido", _svc_del(ENGINE, "tmpfs"), MSG_TMPFS),
    ("tmpfs sem size", _svc_set(ENGINE, "tmpfs", ["/tmp:mode=1777"]), MSG_TMPFS),
    ("tmpfs com outro caminho", _svc_set(ENGINE, "tmpfs", ["/tmp:size=256m", "/var/tmp:size=1m"]), MSG_TMPFS),
    ("user 0:0", _svc_set(ENGINE, "user", "0:0"), MSG_USER),
    ("user 999:999", _svc_set(ENGINE, "user", "999:999"), MSG_USER),
    ("user ausente", _svc_del(ENGINE, "user"), MSG_USER),
    ("cap_add NET_RAW", _svc_set(ENGINE, "cap_add", ["NET_RAW"]), MSG_KEY.format(key="cap_add")),
    ("cap_drop vazio", _svc_set(ENGINE, "cap_drop", []), MSG_CAP_DROP),
    ("privileged", _svc_set(ENGINE, "privileged", True), MSG_KEY.format(key="privileged")),
    ("pid do host", _svc_set(ENGINE, "pid", "host"), MSG_KEY.format(key="pid")),
    ("ipc do host", _svc_set(ENGINE, "ipc", "host"), MSG_KEY.format(key="ipc")),
    ("devices", _svc_set(ENGINE, "devices", ["/dev/kvm:/dev/kvm"]), MSG_KEY.format(key="devices")),
    ("sysctls", _svc_set(ENGINE, "sysctls", {"net.ipv4.ip_forward": "1"}), MSG_KEY.format(key="sysctls")),
    ("userns_mode host", _svc_set(ENGINE, "userns_mode", "host"), MSG_KEY.format(key="userns_mode")),
    ("security_opt vazio", _svc_set(ENGINE, "security_opt", []), MSG_SECURITY_OPT),
    ("no-new-privileges:false", _svc_set(ENGINE, "security_opt", ["no-new-privileges:false"]), MSG_SECURITY_OPT),
    ("seccomp=unconfined", _add_security_opt("seccomp=unconfined"), MSG_SECURITY_OPT),
    # Montagens: soquete do Docker, dados do banco, repos com escrita.
    ("soquete do Docker", _add_engine_volume("/var/run/docker.sock:/var/run/docker.sock"),
     MSG_VOLUME_FORBIDDEN.format(source="/var/run/docker.sock")),
    ("dados do banco", _add_engine_volume("pgdata:/var/lib/postgresql"),
     MSG_VOLUME_FORBIDDEN.format(source="pgdata")),
    ("repos com escrita", _svc_set(ENGINE, "volumes", [f"{REPOS_VOLUME}:{REPOS_TARGET}"]), MSG_VOLUMES),
    ("nocopy removido", _svc_set(ENGINE, "volumes", [
        {"type": "volume", "source": REPOS_VOLUME, "target": REPOS_TARGET, "read_only": True}]), MSG_VOLUMES),
    ("volumes_from", _svc_set(ENGINE, "volumes_from", ["worker"]), MSG_KEY.format(key="volumes_from")),
    # Segredos e ambiente.
    ("env_file .env", _svc_set(ENGINE, "env_file", ".env"), MSG_KEY.format(key="env_file")),
    ("secrets", _svc_set(ENGINE, "secrets", ["db_password"]), MSG_KEY.format(key="secrets")),
    ("senha do banco no ambiente", _add_env(ENGINE, "CQ_DB_PASSWORD", "${CQ_DB_PASSWORD:?x}"),
     MSG_ENV_KEY.format(key="CQ_DB_PASSWORD")),
    ("ENGINE_X com a chave-mestra", _add_env(ENGINE, "ENGINE_X", "${CQ_MASTER_KEY:-}"),
     MSG_INTERPOLATION.format(name="CQ_MASTER_KEY")),
    ("ENGINE_TOKEN", _add_env(ENGINE, "ENGINE_TOKEN", "x"), MSG_ENV_KEY.format(key="ENGINE_TOKEN")),
    # Imagem.
    ("build com args", _svc_set(ENGINE, "build", {**ENGINE_BUILD, "args": {"X": "1"}}), MSG_BUILD),
    ("image sem digest", lambda c: (_engine(c).pop("build", None), _engine(c).__setitem__(
        "image", "canal-questoes/engine:dev")), MSG_BUILD),
    # Outros serviços na rede do motor, repos e URL do motor.
    ("api em [default, engine]", _svc_set("api", "networks", ["default", ENGINE_NETWORK]),
     MSG_OTHER_NETWORK.format(service="api")),
    ("postgres em [engine]", _svc_set("postgres", "networks", [ENGINE_NETWORK]),
     MSG_OTHER_NETWORK.format(service="postgres")),
    ("api no netns do motor", _svc_set("api", "network_mode", f"service:{ENGINE}"),
     MSG_OTHER_NETWORK.format(service="api")),
    ("api com CQ_ENGINE_URL", _add_env("api", "CQ_ENGINE_URL", "http://engine:8080"),
     MSG_OTHER_URL.format(service="api")),
    ("api monta repos", lambda c: c["services"]["api"].setdefault("volumes", []).append("repos:/repos:ro"),
     MSG_OTHER_REPOS.format(service="api")),
    ("worker só na default", _svc_set("worker", "networks", ["default"]), MSG_WORKER_NETWORKS),
    ("worker sem networks", _svc_del("worker", "networks"), MSG_WORKER_NETWORKS),
    ("worker com repos só leitura", _worker_repos_read_only, MSG_WORKER_REPOS),
    ("worker sem CQ_ENGINE_URL", _worker_env_drop("CQ_ENGINE_URL"), MSG_WORKER_URL),
    # Limites e rotação.
    ("pids_limit removido", _svc_del(ENGINE, "pids_limit"), MSG_LIMIT.format(key="pids_limit")),
    ("mem_limit removido", _svc_del(ENGINE, "mem_limit"), MSG_LIMIT.format(key="mem_limit")),
    ("cpus removido", _svc_del(ENGINE, "cpus"), MSG_LIMIT.format(key="cpus")),
    ("pids_limit: -1", _svc_set(ENGINE, "pids_limit", -1), MSG_LIMIT.format(key="pids_limit")),
    ("pids_limit padrão 0", _svc_set(ENGINE, "pids_limit", "${CQ_ENGINE_PIDS_LIMIT:-0}"),
     MSG_LIMIT.format(key="pids_limit")),
    ("deploy.resources", _svc_set(ENGINE, "deploy", {"resources": {"limits": {"cpus": "2"}}}),
     MSG_KEY.format(key="deploy")),
    ("logging removido", _svc_del(ENGINE, "logging"), MSG_LOGGING),
    ("logging none", _svc_set(ENGINE, "logging", {"driver": "none"}), MSG_LOGGING),
    ("max-size 100m", _set_log_option("max-size", "100m"), MSG_LOGGING),
    ("max-file removido", _set_log_option("max-file", None), MSG_LOGGING),
    ("max-file 5", _set_log_option("max-file", "5"), MSG_LOGGING),
    ("restart removido", _svc_del(ENGINE, "restart"), MSG_RESTART),
    ("healthcheck fora do loopback", _svc_set(ENGINE, "healthcheck", {
        "test": ["CMD", "curl", "--fail", "http://engine:8080/v1/health"]}), MSG_HEALTHCHECK),
]


# ---------------------------------------------------------------------------------------------
# Autoteste das funções puras (formas curtas e longas)
# ---------------------------------------------------------------------------------------------


def _self_check() -> list[str]:
    net = {"internal": True, "driver": "bridge", "driver_opts": {INHIBIT_IPV4: "true"}}
    cases: list[tuple[str, Any, Any]] = [
        ("sem serviço engine", engine_isolation_problems({"services": {"api": {}}}), [MSG_MISSING]),
        ("compose vazio", engine_isolation_problems({}), [MSG_MISSING]),
        ("redes em lista", _engine_networks({"services": {"engine": {"networks": ["engine"]}},
                                             "networks": {"engine": net}}), {"engine": net}),
        ("redes em mapa", _engine_networks({"services": {"engine": {"networks": {"engine": {"aliases": ["m"]}}}},
                                            "networks": {"engine": net}}), {"engine": net}),
        ("sem networks vira default", _engine_networks({"services": {"engine": {}}}), {"default": None}),
        ("rede declarada sem opções", _engine_networks({"services": {"engine": {"networks": ["x"]}},
                                                        "networks": {"x": None}}), {"x": {}}),
        ("rede não declarada", _engine_networks({"services": {"engine": {"networks": ["y"]}}}), {"y": None}),
        ("volume curto ro", _mounts({"volumes": ["repos:/repos:ro"]}),
         [Mount("volume", "repos", "/repos", True, False)]),
        ("volume curto ro,nocopy", _mounts({"volumes": ["repos:/repos/:ro,nocopy"]}),
         [Mount("volume", "repos", "/repos", True, True)]),
        ("volume curto com escrita", _mounts({"volumes": ["repos:/repos"]}),
         [Mount("volume", "repos", "/repos", False, False)]),
        ("bind curto", _mounts({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}),
         [Mount("bind", "/var/run/docker.sock", "/var/run/docker.sock", False, False)]),
        ("bind relativo", _mounts({"volumes": ["./dados:/dados:rw"]}),
         [Mount("bind", "./dados", "/dados", False, False)]),
        ("volume anônimo", _mounts({"volumes": ["/cache"]}), [Mount("volume", None, "/cache", False, False)]),
        ("volume longo", _mounts({"volumes": [{"type": "volume", "source": "repos", "target": "/repos",
                                               "read_only": True, "volume": {"nocopy": True}}]}),
         [Mount("volume", "repos", "/repos", True, True)]),
        ("bind longo", _mounts({"volumes": [{"type": "bind", "source": "/srv", "target": "/srv"}]}),
         [Mount("bind", "/srv", "/srv", False, False)]),
        ("tmpfs longo", _mounts({"volumes": [{"type": "tmpfs", "target": "/tmp"}]}),
         [Mount("tmpfs", None, "/tmp", False, False)]),
        ("tmpfs em texto", _tmpfs_entries("/tmp:size=256m,mode=1777"), ["/tmp:size=256m,mode=1777"]),
        ("tmpfs em lista", _tmpfs_entries(["/tmp:size=${CQ_ENGINE_TMP_SIZE:-256m},mode=1777"]),
         ["/tmp:size=${CQ_ENGINE_TMP_SIZE:-256m},mode=1777"]),
        ("interpolação simples", _interpolated_names({"a": "${CQ_ENGINE_CPUS:-2}"}), {"CQ_ENGINE_CPUS"}),
        ("interpolação aninhada", _interpolated_names(["x ${A:-${B}} y", {"k": "${C?msg}"}]), {"A", "B", "C"}),
        ("cifrão literal", _interpolated_names("$${NAO_E}"), set()),
        ("interpolação em chave", _interpolated_names({"${K}": 1}), {"K"}),
        ("limite literal", _limit_ok("pids_limit", 256), True),
        ("limite interpolado", _limit_ok("mem_limit", "${CQ_ENGINE_MEM_LIMIT:-2g}"), True),
        ("limite com outra variável", _limit_ok("cpus", "${CQ_CPUS:-2}"), False),
        ("limite sem padrão", _limit_ok("cpus", "${CQ_ENGINE_CPUS}"), False),
        ("pids -1", _limit_ok("pids_limit", -1), False),
        ("pids 0", _limit_ok("pids_limit", 0), False),
        ("limite nulo", _limit_ok("mem_limit", None), False),
        ("tamanho 1m", _size_bytes("1m"), 1024**2),
        ("tamanho 100m", _size_bytes("100m"), 100 * 1024**2),
        ("tamanho ilegível", _size_bytes("x"), None),
        ("ambiente em lista", _env({"environment": ["ENGINE_WORKERS=2"]}), {"ENGINE_WORKERS": "2"}),
    ]
    return [f"autoteste {label}: {actual!r} != {expected!r}" for label, actual, expected in cases
            if actual != expected]


def test_engine_isolation_in_compose() -> None:
    """O serviço `engine` do `compose.yaml`
    real cumpre as 18 regras de isolamento (só a rede interna `engine` com `inhibit_ipv4`, sem porta,
    `read_only`, `tmpfs /tmp` com `size`, `user 65534:65534`, `cap_drop [ALL]`, `no-new-privileges`,
    limites de processos, memória e CPU, `repos` só leitura e `nocopy`, log curto, `restart:
    unless-stopped`, sem `env_file` nem segredo; só o `worker` na rede do motor; a api sem
    `CQ_ENGINE_URL`), e cada mutação de `MUTATIONS`, uma a uma, é acusada com a mensagem da regra.
    Vermelho no esqueleto: o `compose.yaml` ainda não tem o serviço `engine`."""
    assert COMPOSE.is_file(), "compose.yaml ausente na raiz do repositório"
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(_services(compose).get(ENGINE), dict), (
        "serviço `engine` ausente do compose.yaml")

    # Autoteste das funções puras e da mensagem do serviço ausente.
    assert _self_check() == []

    # O arquivo real: zero problemas.
    assert engine_isolation_problems(compose) == [], (
        "Problemas de isolamento do motor:\n- " + "\n- ".join(engine_isolation_problems(compose)))
    # Controle positivo das regras que dependem do topo: a rede e o volume declarados.
    assert _engine_networks(compose) == {ENGINE_NETWORK: compose["networks"][ENGINE_NETWORK]}
    assert _mounts(compose["services"][ENGINE]) == [Mount("volume", REPOS_VOLUME, REPOS_TARGET, True, True)]
    assert all(n.startswith(ENGINE_VAR_PREFIX) for n in _interpolated_names(compose["services"][ENGINE]))

    # Cada mutação, sozinha, sobre uma cópia profunda do arquivo real, é acusada (todas, 100%).
    labels = [label for label, _, _ in MUTATIONS]
    assert len(labels) == len(set(labels)) and len(MUTATIONS) >= 40
    missed: list[str] = []
    for label, mutate, expected in MUTATIONS:
        mutated = copy.deepcopy(compose)
        try:
            mutate(mutated)
            problems = engine_isolation_problems(mutated)
        except Exception as exc:  # uma estrutura inesperada no arquivo real vira falha relatada
            missed.append(f"{label}: a mutação falhou ({type(exc).__name__}: {exc})")
            continue
        if not any(expected in p for p in problems):
            missed.append(f"{label}: esperava {expected!r}, veio {problems}")
    assert missed == [], "Mutações não acusadas:\n- " + "\n- ".join(missed)
    # A cópia real não foi alterada pelas mutações.
    assert engine_isolation_problems(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))) == []
