"""O `compose.yaml` da implantação e do piloto (o congelado `tests/contract/test_deploy_files.py`
continua verde).

O arquivo é lido como YAML, **sem interpolar** (nunca `docker compose config`, que imprime os segredos). O que se
confere:
- a `api` continua com `ports` exatamente `["127.0.0.1:${CQ_API_PORT:-8000}:8000"]`, e nenhum outro serviço publica
  porta;
- na `api`, as 8 variáveis novas, cada uma `${VAR:-}` e **só** nesse serviço; no `worker`, só
  `CQ_MAIL_PROBE_INTERVAL_SECONDS: ${CQ_MAIL_PROBE_INTERVAL_SECONDS:-}`; nenhuma variável nova começa com
  `CQ_SMTP_`;
- a única montagem nova é a da `api`, `${CQ_BACKUP_STATUS_DIR:-./ops/backup/status-unconfigured}:/run/cq-backup:ro`,
  cujo padrão é um diretório versionado que contém **só** `README.md`; nenhum caminho de montagem cita `dumps`;
- `api`, `worker` e `postgres` com `logging` `json-file`, `max-size` ≤ `10m` e `max-file` ≤ `3`; o `engine` igual ao
  anterior (o bloco congelado pelo `test_engine_isolation.py`); o `postgres` com `stop_grace_period: 60s`;
- o cabeçalho (os comentários antes de `services:`) cita cada variável nova e as do host e do cliente
  (`CQ_BACKUP_STATUS_DIR`, `CQ_BACKUP_DIR`, `CQ_DOCTOR_DISK_WARNING_PERCENT`, `CQ_DOCTOR_DISK_FAILURE_PERCENT`);
- nenhum `env_file`, nenhum `secrets:`.

No esqueleto, falha: o `compose.yaml` ainda é o anterior (sem as mudanças da implementação; uma asserção de contrato
ainda não cumprida). **Ao lado delas:** o `README.md` do diretório padrão (já no esqueleto) e as
regressões (as portas, o bloco do motor, nenhum `env_file`) ficam nas mesmas funções das asserções novas.

**[I]** Escolhas deste teste: o conjunto das variáveis anteriores (`PREVIOUS_ENV_NAMES`) e o bloco do motor
(`PREVIOUS_ENGINE`) estão copiados aqui do `compose.yaml` de 2026-10-29,
em vez de lidos do histórico do git, para o teste valer no repositório exportado (um commit só);
as montagens do `postgres` e do `worker` anteriores ficam iguais (só a da `api` é nova).
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from typing import Any

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"
STATUS_UNCONFIGURED = REPO / "ops" / "backup" / "status-unconfigured"
STATUS_UNCONFIGURED_REL = "ops/backup/status-unconfigured"

API_PORTS = ["127.0.0.1:${CQ_API_PORT:-8000}:8000"]

# as variáveis novas da `api` (as da implantação e as do `doctor` do CLI, que vêm do Compose).
API_NEW_VARS = (
    "CQ_BACKUP_STATUS_FILE",
    "CQ_RETENTION_DAYS",
    "CQ_DOCTOR_BACKUP_WARNING_HOURS",
    "CQ_DOCTOR_BACKUP_FAILURE_HOURS",
    "CQ_DOCTOR_MAIL_STALE_HOURS",
    "CQ_DOCTOR_BOT_POLL_SECONDS",
    "CQ_DOCTOR_TASK_OVERDUE_SECONDS",
    "CQ_DOCTOR_CLOCK_SKEW_SECONDS",
)
WORKER_NEW_VARS = ("CQ_MAIL_PROBE_INTERVAL_SECONDS",)
# as variáveis do host e do cliente que o cabeçalho também cita.
HOST_CLIENT_VARS = (
    "CQ_BACKUP_STATUS_DIR",
    "CQ_BACKUP_DIR",
    "CQ_DOCTOR_DISK_WARNING_PERCENT",
    "CQ_DOCTOR_DISK_FAILURE_PERCENT",
)

API_STATUS_MOUNT = "${CQ_BACKUP_STATUS_DIR:-./ops/backup/status-unconfigured}:/run/cq-backup:ro"
MOUNT_DEFAULT = re.compile(r"^\$\{CQ_BACKUP_STATUS_DIR:-(?P<default>[^}]+)\}:(?P<target>[^:]+):ro$")

LOGGED_SERVICES = ("api", "worker", "postgres")
MAX_LOG_BYTES = 10 * 1024 * 1024
MAX_LOG_FILES = 3
_SIZE = re.compile(r"^(?P<n>\d+)(?P<unit>[kmg]?)$")
_UNITS = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}

# **[F]** As variáveis de ambiente dos serviços no `compose.yaml` anterior (2026-10-29).
PREVIOUS_ENV_NAMES = frozenset({
    "CQ_ADMIN_TOKEN", "CQ_CONSOLE_OUTPUT", "CQ_DATABASE_URL", "CQ_ENGINE_TIMEOUT_SECONDS", "CQ_ENGINE_URL",
    "CQ_FREE_MESSAGE_REPLY_SECONDS", "CQ_MASTER_KEY", "CQ_REPOS_ROOT", "CQ_REPO_ALLOWED_HOSTS",
    "CQ_REPO_FETCH_TIMEOUT_SECONDS", "CQ_REPO_MAX_BYTES", "CQ_REPO_MAX_FILES", "CQ_SEND_BOT_PER_SECOND",
    "CQ_SEND_CHAT_INTERVAL_SECONDS", "CQ_SMTP_FROM", "CQ_SMTP_HOST", "CQ_SMTP_PASSWORD", "CQ_SMTP_PORT",
    "CQ_SMTP_SECURITY", "CQ_SMTP_TIMEOUT_SECONDS", "CQ_SMTP_USERNAME", "CQ_TELEGRAM_FORMAT",
    "CQ_TELEGRAM_MAX_BUTTONS", "CQ_TELEGRAM_PHOTO_MAX_BYTES", "ENGINE_WORKERS", "POSTGRES_DB", "POSTGRES_PASSWORD",
    "POSTGRES_USER",
})
# **[F]** As montagens anteriores que não mudam (2026-10-29).
PREVIOUS_VOLUMES = {"postgres": ["pgdata:/var/lib/postgresql"], "worker": ["repos:/repos"]}
# **[F]** O bloco do motor no `compose.yaml` anterior (2026-10-29), como o `yaml.safe_load` o lê.
PREVIOUS_ENGINE: dict[str, Any] = {
    "build": {"context": ".", "dockerfile": "engine/Dockerfile"},
    "environment": {"ENGINE_WORKERS": "${CQ_ENGINE_WORKERS:-2}"},
    "networks": ["engine"],
    "read_only": True,
    "tmpfs": ["/tmp:size=${CQ_ENGINE_TMP_SIZE:-256m},mode=1777"],
    "user": "65534:65534",
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "pids_limit": "${CQ_ENGINE_PIDS_LIMIT:-256}",
    "mem_limit": "${CQ_ENGINE_MEM_LIMIT:-2g}",
    "cpus": "${CQ_ENGINE_CPUS:-2}",
    "volumes": [{"type": "volume", "source": "repos", "target": "/repos", "read_only": True,
                 "volume": {"nocopy": True}}],
    "logging": {"driver": "json-file", "options": {"max-size": "1m", "max-file": "2"}},
    "healthcheck": {
        "test": ["CMD", "curl", "--fail", "--silent", "--show-error", "--max-time", "4", "--output", "/dev/null",
                 "http://127.0.0.1:8080/v1/health"],
        "interval": "30s",
        "timeout": "5s",
        "retries": 3,
        "start_period": "60s",
        "start_interval": "1s",
    },
    "restart": "unless-stopped",
}


def _compose() -> dict[str, Any]:
    assert COMPOSE.is_file(), "compose.yaml ausente"
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("services"), dict), "compose.yaml sem `services`"
    return data


def _env(service: dict[str, Any]) -> dict[str, Any]:
    """O `environment` do serviço como mapa (aceita a forma de lista `NOME=valor`)."""
    env = service.get("environment") or {}
    if isinstance(env, list):
        pairs = (str(item).split("=", 1) for item in env)
        return {pair[0]: (pair[1] if len(pair) > 1 else None) for pair in pairs}
    assert isinstance(env, dict), "environment em forma desconhecida"
    return dict(env)


def _volume_texts(service: dict[str, Any]) -> list[str]:
    """Cada montagem do serviço como texto (a forma longa vira o seu YAML), para procurar `dumps`."""
    return [item if isinstance(item, str) else yaml.safe_dump(item) for item in (service.get("volumes") or [])]


def _size_bytes(value: Any) -> int | None:
    match = _SIZE.match(str(value).strip().lower())
    if match is None:
        return None
    return int(match["n"]) * _UNITS[match["unit"]]


def _tracked(prefix: str) -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z", "--", prefix], cwd=REPO, capture_output=True, timeout=120,
                            check=False)
    assert listed.returncode == 0, "git ls-files falhou"
    return sorted(name for name in listed.stdout.decode("utf-8").split("\0") if name)


def _header(text: str) -> str:
    """Os comentários do topo do arquivo, antes da primeira chave (`services:`)."""
    lines = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
            continue
        break
    return "\n".join(lines)


def test_api_ports_new_variables_and_backup_status_mount() -> None:
    """as portas (regressão), as variáveis novas da `api` e do `worker`, cada uma só no seu serviço, a
    montagem só leitura do estado do backup e o diretório padrão versionado só com `README.md`."""
    compose = _compose()
    services = compose["services"]
    api = services["api"]

    # Regressão: a api publica só no loopback, e nenhum outro serviço publica porta.
    assert api.get("ports") == API_PORTS
    assert [name for name, svc in services.items() if name != "api" and "ports" in svc] == []

    # As variáveis novas da api, cada uma `${VAR:-}` e só na api.
    api_env = _env(api)
    for name in API_NEW_VARS:
        assert api_env.get(name) == f"${{{name}:-}}", f"api: {name} deve ser ${{{name}:-}}"
    worker_env = _env(services["worker"])
    for name in WORKER_NEW_VARS:
        assert worker_env.get(name) == f"${{{name}:-}}", f"worker: {name} deve ser ${{{name}:-}}"
    owners = {"api": API_NEW_VARS, "worker": WORKER_NEW_VARS}
    for service_name, svc in services.items():
        env = _env(svc)
        for owner, names in owners.items():
            if owner == service_name:
                continue
            assert [name for name in names if name in env] == [], f"{service_name} recebe variável de {owner}"
    new_names = {name for svc in services.values() for name in _env(svc)} - PREVIOUS_ENV_NAMES
    assert sorted(new_names) == sorted(API_NEW_VARS + WORKER_NEW_VARS)
    assert [name for name in new_names if name.startswith("CQ_SMTP_")] == []

    # A única montagem nova: a da api, só leitura, em /run/cq-backup.
    assert api.get("volumes") == [API_STATUS_MOUNT]
    for name, volumes in PREVIOUS_VOLUMES.items():
        assert services[name].get("volumes") == volumes, f"{name}: montagens anteriores mudaram"
    match = MOUNT_DEFAULT.match(API_STATUS_MOUNT)
    assert match is not None and match["target"] == "/run/cq-backup"
    default = match["default"]
    assert default == "./" + STATUS_UNCONFIGURED_REL
    assert (REPO / default).resolve() == STATUS_UNCONFIGURED.resolve()
    for name, svc in services.items():
        assert [text for text in _volume_texts(svc) if "dumps" in text] == [], f"{name}: montagem cita dumps"

    # O diretório padrão (versionado no esqueleto): só o README.md, no disco e no git.
    assert STATUS_UNCONFIGURED.is_dir()
    assert sorted(path.name for path in STATUS_UNCONFIGURED.iterdir()) == ["README.md"]
    assert _tracked(STATUS_UNCONFIGURED_REL) == [STATUS_UNCONFIGURED_REL + "/README.md"]
    assert (STATUS_UNCONFIGURED / "README.md").read_text(encoding="utf-8").strip()


def test_log_rotation_engine_block_and_postgres_grace() -> None:
    """`logging` json-file ≤ 10m × 3 em `api`, `worker` e `postgres` (e o motor, anterior, já dentro do
    limite); o bloco do motor igual ao anterior; `stop_grace_period: 60s` no `postgres`; nenhum `env_file` nem
    `secrets:`."""
    compose = _compose()
    services = compose["services"]

    for name in (*LOGGED_SERVICES, "engine"):
        logging = services[name].get("logging")
        assert isinstance(logging, dict) and logging.get("driver") == "json-file", f"{name}: logging json-file"
        options = logging.get("options") or {}
        size = _size_bytes(options.get("max-size"))
        assert size is not None and 0 < size <= MAX_LOG_BYTES, f"{name}: max-size acima de 10m"
        files = str(options.get("max-file", "")).strip()
        assert files.isdigit() and 1 <= int(files) <= MAX_LOG_FILES, f"{name}: max-file acima de 3"

    assert services["postgres"].get("stop_grace_period") == "60s"

    # Regressões: o motor anterior (o bloco congelado) e nenhum `env_file` nem `secrets`.
    assert services["engine"] == PREVIOUS_ENGINE
    assert "secrets" not in compose
    for name, svc in services.items():
        assert "env_file" not in svc, f"{name}: env_file"
        assert "secrets" not in svc, f"{name}: secrets"


def test_header_documents_new_host_and_client_variables() -> None:
    """o cabeçalho do `compose.yaml` cita cada variável nova e as do host e do cliente."""
    text = COMPOSE.read_text(encoding="utf-8")
    header = _header(text)
    assert header.strip(), "compose.yaml sem cabeçalho"
    missing = [name for name in (*API_NEW_VARS, *WORKER_NEW_VARS, *HOST_CLIENT_VARS)
               if not re.search(rf"\b{name}\b", header)]
    assert missing == [], f"o cabeçalho não cita: {missing}"
    # Regressão: o aviso da API base sobre `docker compose config` continua no cabeçalho.
    assert "docker compose config" in header
