"""Arquivos de implantação: os serviços e as variáveis do Compose, o banco, o Dockerfile e os segredos
fora do git.

Tudo fica numa **única** função, que começa exigindo o `compose.yaml`: no esqueleto (sem arquivos de
implantação) ela falha por falta desse arquivo, e nenhuma checagem que já valeria antes da implementação
(ex.: `.env` fora do git) fica sozinha numa função verde. Os problemas são reunidos e relatados
juntos, para que a implementação veja todos de uma vez.

Oferecimentos e bots, com as variáveis do Compose e as dependências novas:
api e worker com
`CQ_MASTER_KEY: ${CQ_MASTER_KEY:-}` e sem `CQ_TELEGRAM_API_URL` (nem outra variável dos bots); a
etapa final do `Dockerfile` com `PYTHONTZPATH=""`; e, na mesma função, as dependências (`httpx2`,
`httpcore2`, `cryptography` e `tzdata` no `cq-server` sem marcador, `tzdata` no `cq-client`, sem
`psycopg-pool`), no `pyproject.toml` e no `uv.lock`. `ENV_EXAMPLE_KEYS` não muda. No
esqueleto, o `compose.yaml` e o `Dockerfile` ainda não têm as mudanças de oferecimentos e bots, e a função falha por
isso.

Vínculo do aluno (lista e convites), com as variáveis do Compose:
o worker recebe as 7 variáveis do e-mail (`CQ_SMTP_HOST`,
`CQ_SMTP_PORT`, `CQ_SMTP_SECURITY`, `CQ_SMTP_USERNAME`, `CQ_SMTP_PASSWORD`, `CQ_SMTP_FROM` e
`CQ_SMTP_TIMEOUT_SECONDS`), todas `${…:-}` (sem configuração, o e-mail fica desligado e o Compose
sobe; a senha como segredo, só por interpolação); a api e os demais serviços, nenhuma `CQ_SMTP_*`.
`ENV_EXAMPLE_KEYS` não muda. No esqueleto, o `compose.yaml` não tem as variáveis do e-mail, e a
função falha por isso.
"""

from __future__ import annotations

import fnmatch
import pathlib
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Iterator
from typing import Any

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "compose.yaml"
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
ENV_EXAMPLE = REPO / ".env.example"
TEST_POSTGRES_SCRIPT = REPO / "scripts" / "test-postgres.sh"

REQUIRED_SERVICES = {"api", "worker", "postgres"}
OPTIONAL_SERVICES = {"engine"}  # previsto e comentado até o motor de questões ficar pronto
API_PORT = "127.0.0.1:${CQ_API_PORT:-8000}:8000"
ENV_EXAMPLE_KEYS = ["CQ_ADMIN_TOKEN", "CQ_API_PORT", "CQ_DB_PASSWORD"]

SECRET_NAME = re.compile(r"PASSWORD|TOKEN|SECRET|KEY", re.IGNORECASE)
# Interpolação que não carrega valor literal: `${X}`, `${X?msg}`, `${X:?msg}`, `${X-}`, `${X:-}`.
SAFE_INTERPOLATION = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?\?[^{}]*|:?-)?\}")
POSTGRES_PASSWORD = re.compile(r"\$\{CQ_DB_PASSWORD:\?[^}]*\}")
POSTGRES_IMAGE = re.compile(r"postgres:18\.6@sha256:[0-9a-f]{64}")
PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
URL_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://([^/\s@]*)@")

# Oferecimentos e bots: a chave-mestra na api e no
# worker, sem valor padrão; as demais variáveis de `BotSettings` não entram no Compose (vale o
# padrão, inclusive o `CQ_TELEGRAM_API_URL`).
MASTER_KEY_VALUE = "${CQ_MASTER_KEY:-}"
MASTER_KEY_SERVICES = ("api", "worker")
BOT_SETTINGS_VARS = ("CQ_TELEGRAM_API_URL", "CQ_BOT_LEASE_SECONDS", "CQ_BOT_RENEW_SECONDS",
                     "CQ_BOT_RECONCILE_SECONDS", "CQ_POLL_TIMEOUT_SECONDS", "CQ_TELEGRAM_TIMEOUT_SECONDS")
# Oferecimentos e bots: dependências novas, sem marcador de ambiente, e a retirada.
SERVER_PYPROJECT = REPO / "server" / "pyproject.toml"
CLIENT_PYPROJECT = REPO / "client" / "pyproject.toml"
UV_LOCK = REPO / "uv.lock"
REQUIRED_DEPENDENCIES = {
    "cq-server": ("httpx2", "httpcore2", "cryptography", "tzdata"),
    "cq-client": ("tzdata",),
}
REMOVED_DEPENDENCIES = ("psycopg-pool",)
# Vínculo do aluno: as 7 variáveis do e-mail, só no worker,
# cada uma como `${<nome>:-}` (sem valor padrão; vazio = ausente).
MAIL_VARS = ("CQ_SMTP_HOST", "CQ_SMTP_PORT", "CQ_SMTP_SECURITY", "CQ_SMTP_USERNAME", "CQ_SMTP_PASSWORD",
             "CQ_SMTP_FROM", "CQ_SMTP_TIMEOUT_SECONDS")
MAIL_SERVICE = "worker"
MAIL_PREFIX = "CQ_SMTP_"


# ---------------------------------------------------------------------------------------------
# Utilidades de leitura
# ---------------------------------------------------------------------------------------------


def _services(compose: Any) -> dict:
    services = compose.get("services") if isinstance(compose, dict) else None
    if not isinstance(services, dict):
        raise AssertionError("compose.yaml sem o mapa `services`")
    return services


def _service(compose: Any, name: str) -> dict:
    svc = _services(compose).get(name)
    if not isinstance(svc, dict):
        raise AssertionError(f"serviço `{name}` ausente ou malformado")
    return svc


def _env(svc: dict) -> dict:
    env = svc.get("environment")
    return env if isinstance(env, dict) else {}


def _is_root(user: str) -> bool:
    return user.split(":")[0].strip().strip("\"'") in ("root", "0")


def _mask(text: str) -> tuple[str, list[str]]:
    """Troca cada interpolação `${…}` (com aninhamento) por um marcador `\\x00<i>\\x00`."""
    out: list[str] = []
    found: list[str] = []
    i = 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$$")
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
            out.append(f"\x00{len(found)}\x00")
            found.append(text[i:j])
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out), found


def _only_safe_interpolation(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    masked, found = _mask(value)
    return bool(found) and re.sub(r"\x00\d+\x00", "", masked) == "" and all(
        SAFE_INTERPOLATION.fullmatch(f) for f in found)


def _url_credential_problems(text: str) -> list[str]:
    """URL com senha (`esquema://usuário:senha@`) cuja senha não é só interpolação segura."""
    masked, found = _mask(text)
    problems = []
    for match in URL_USERINFO.finditer(masked):
        userinfo = match.group(1)
        if ":" not in userinfo:
            continue
        password = userinfo.split(":", 1)[1]
        refs = re.findall(r"\x00(\d+)\x00", password)
        if not re.fullmatch(r"(?:\x00\d+\x00)+", password) or not all(
                SAFE_INTERPOLATION.fullmatch(found[int(r)]) for r in refs):
            problems.append("URL com credencial que não vem só de ${…}")
    return problems


def _strings(node: Any, where: str) -> Iterator[tuple[str, str]]:
    if isinstance(node, str):
        yield where, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(value, f"{where}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _strings(item, f"{where}[{i}]")


def _declared_vars(svc: dict) -> Iterator[tuple[str, Any]]:
    """Variáveis do `environment` e dos `build.args` de um serviço."""
    yield from _env(svc).items()
    build = svc.get("build")
    args = build.get("args") if isinstance(build, dict) else None
    if isinstance(args, dict):
        yield from args.items()
    elif isinstance(args, list):
        for item in args:
            name, _, value = str(item).partition("=")
            yield name, value


def _mount(volume: Any) -> tuple[str | None, str]:
    if isinstance(volume, dict):
        return volume.get("source"), str(volume.get("target", "")).rstrip("/")
    parts = str(volume).split(":")
    if len(parts) >= 2:
        return parts[0], parts[1].rstrip("/")
    return None, parts[0].rstrip("/")


def _postgres_settings(command: Any) -> dict[str, str]:
    """Parâmetros `-c nome=valor` / `--nome=valor` do comando do Postgres (o último vale)."""
    if command is None:
        return {}
    tokens = shlex.split(command) if isinstance(command, str) else [str(t) for t in command]
    settings: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "-c" and i + 1 < len(tokens):
            pair, i = tokens[i + 1], i + 2
        elif token.startswith("-c") and len(token) > 2:
            pair, i = token[2:], i + 1
        elif token.startswith("--") and "=" in token:
            pair, i = token[2:], i + 1
        else:
            i += 1
            continue
        name, _, value = pair.partition("=")
        settings[name.replace("-", "_").lower()] = value
    return settings


def _healthcheck_text(svc: dict) -> str:
    test = (svc.get("healthcheck") or {}).get("test")
    if isinstance(test, list):
        return " ".join(str(t) for t in test if t not in ("CMD", "CMD-SHELL"))
    return str(test or "")


def _loopback_port(port: Any) -> bool:
    if isinstance(port, dict):
        return port.get("host_ip") == "127.0.0.1"
    return str(port).startswith("127.0.0.1:")


def _depends_on_healthy_postgres(name: str, svc: dict) -> list[str]:
    deps = svc.get("depends_on")
    pg = deps.get("postgres") if isinstance(deps, dict) else None
    if not isinstance(pg, dict) or pg.get("condition") != "service_healthy":
        return [f"{name}: depends_on.postgres.condition precisa ser service_healthy"]
    return []


def _database_url_problems(name: str, svc: dict) -> list[str]:
    url = _env(svc).get("CQ_DATABASE_URL")
    if not (isinstance(url, str) and "${CQ_DB_PASSWORD:?" in url):
        return [f"{name}: CQ_DATABASE_URL precisa conter ${{CQ_DB_PASSWORD:?…}}"]
    return []


# ---------------------------------------------------------------------------------------------
# Checagens
# ---------------------------------------------------------------------------------------------


def _check_services(compose: Any) -> list[str]:
    problems: list[str] = []
    services = _services(compose)
    names = set(services)
    if missing := REQUIRED_SERVICES - names:
        problems.append(f"serviços ausentes: {sorted(missing)}")
    if extra := names - REQUIRED_SERVICES - OPTIONAL_SERVICES:
        problems.append(f"serviços fora do previsto (só `engine` pode se somar): {sorted(extra)}")
    for name, svc in services.items():
        if not isinstance(svc, dict):
            problems.append(f"{name}: serviço malformado")
            continue
        env = svc.get("environment")
        if name in REQUIRED_SERVICES and env is None:
            problems.append(f"{name}: sem `environment`")
        elif env is not None and not isinstance(env, dict):
            problems.append(f"{name}: `environment` precisa ser um mapa")
        if "env_file" in svc:
            problems.append(f"{name}: `env_file` não é permitido (segredos só por ${{…}} no environment)")
        if svc.get("user") is not None and _is_root(str(svc["user"])):
            problems.append(f"{name}: `user` root")
        if name != "api":
            for port in svc.get("ports") or []:
                if not _loopback_port(port):
                    problems.append(f"{name}: porta publicada fora de 127.0.0.1")
        if name != "api" and "CQ_ADMIN_TOKEN" in yaml.safe_dump(svc):
            problems.append(f"{name}: menciona CQ_ADMIN_TOKEN (só a api recebe o token inicial)")
    return problems


def _check_postgres(compose: Any) -> list[str]:
    problems: list[str] = []
    pg = _service(compose, "postgres")
    image = str(pg.get("image", ""))
    if not POSTGRES_IMAGE.fullmatch(image):
        problems.append("postgres: imagem precisa ser postgres:18.6 fixada por @sha256:<64 hex>")
    script = TEST_POSTGRES_SCRIPT.read_text(encoding="utf-8") if TEST_POSTGRES_SCRIPT.is_file() else ""
    test_images = set(POSTGRES_IMAGE.findall(script))
    if len(test_images) != 1:
        problems.append("scripts/test-postgres.sh sem uma imagem postgres:18.6@sha256: única")
    elif image not in test_images:
        problems.append("postgres: digest diferente do Postgres de teste")

    mounts = [_mount(v) for v in pg.get("volumes") or []]
    if ("pgdata", "/var/lib/postgresql") not in mounts:
        problems.append("postgres: falta o volume pgdata:/var/lib/postgresql")
    if any(target == "/var/lib/postgresql/data" for _, target in mounts):
        problems.append("postgres: volume em /var/lib/postgresql/data derruba o Postgres 18")
    top_volumes = compose.get("volumes") or {}
    if not isinstance(top_volumes, dict) or "pgdata" not in top_volumes:
        problems.append("volume nomeado `pgdata` não declarado no nível de cima")

    password = _env(pg).get("POSTGRES_PASSWORD")
    if not (isinstance(password, str) and POSTGRES_PASSWORD.fullmatch(password)):
        problems.append("postgres: POSTGRES_PASSWORD precisa ser ${CQ_DB_PASSWORD:?…}")

    settings = _postgres_settings(pg.get("command"))
    if settings.get("log_error_verbosity", "").lower() != "terse":
        problems.append("postgres: command sem log_error_verbosity=terse")
    if settings.get("log_min_error_statement", "").lower() != "panic":
        problems.append("postgres: command sem log_min_error_statement=panic")

    health = _healthcheck_text(pg)
    if "pg_isready" not in health or not re.search(r"(^|\s)(-h\s*\S+|--host(=|\s+)\S+)", health):
        problems.append("postgres: healthcheck precisa de pg_isready por TCP (-h/--host)")
    return problems


def _check_api(compose: Any) -> list[str]:
    problems: list[str] = []
    api = _service(compose, "api")
    if api.get("ports") != [API_PORT]:
        problems.append(f"api: ports precisa ser exatamente [{API_PORT!r}]")
    problems += _database_url_problems("api", api)
    if _env(api).get("CQ_ADMIN_TOKEN") != "${CQ_ADMIN_TOKEN:-}":
        problems.append("api: CQ_ADMIN_TOKEN precisa ser ${CQ_ADMIN_TOKEN:-}")
    problems += _depends_on_healthy_postgres("api", api)
    return problems


def _check_worker(compose: Any) -> list[str]:
    problems: list[str] = []
    worker = _service(compose, "worker")
    env = _env(worker)
    output = env.get("CQ_CONSOLE_OUTPUT")
    if not (isinstance(output, str) and output.startswith("/") and not output.startswith(("/dev/", "/proc/"))):
        problems.append("worker: CQ_CONSOLE_OUTPUT precisa ser um arquivo no container, fora do stdout")
    if worker.get("restart") != "unless-stopped":
        problems.append("worker: restart precisa ser unless-stopped")
    if "CQ_ADMIN_TOKEN" in env:
        problems.append("worker: não pode receber CQ_ADMIN_TOKEN")
    problems += _database_url_problems("worker", worker)
    problems += _depends_on_healthy_postgres("worker", worker)
    return problems


def _check_compose_secrets(compose: Any) -> list[str]:
    problems: list[str] = []
    for name, svc in _services(compose).items():
        if not isinstance(svc, dict):
            continue
        for var, value in _declared_vars(svc):
            if SECRET_NAME.search(str(var)) and not _only_safe_interpolation(value):
                problems.append(f"{name}: {var} precisa vir só de ${{…}}, sem valor literal nem padrão")
    for where, text in _strings(compose, "compose"):
        problems += [f"{where}: {p}" for p in _url_credential_problems(text)]
    return problems


def _check_bots(compose: Any) -> list[str]:
    """`CQ_MASTER_KEY: ${CQ_MASTER_KEY:-}` na api e no worker, e em nenhum outro serviço;
    nenhuma outra variável de `BotSettings` no Compose."""
    problems: list[str] = []
    for name in MASTER_KEY_SERVICES:
        env = _env(_service(compose, name))
        if env.get("CQ_MASTER_KEY") != MASTER_KEY_VALUE:
            problems.append(f"{name}: CQ_MASTER_KEY precisa ser {MASTER_KEY_VALUE}")
        if "CQ_TELEGRAM_API_URL" in env:
            problems.append(f"{name}: CQ_TELEGRAM_API_URL não é definida no Compose (vale o padrão)")
    for name, svc in _services(compose).items():
        if not isinstance(svc, dict):
            continue
        declared = {str(var) for var, _ in _declared_vars(svc)}
        if extra := sorted(declared & set(BOT_SETTINGS_VARS)):
            problems.append(f"{name}: variáveis dos bots fora do previsto no Compose: {extra}")
        if name not in MASTER_KEY_SERVICES and "CQ_MASTER_KEY" in yaml.safe_dump(svc):
            problems.append(f"{name}: menciona CQ_MASTER_KEY (só a api e o worker recebem a chave-mestra)")
    return problems


def _check_mail(compose: Any) -> list[str]:
    """as 7 `CQ_SMTP_*` no worker, cada uma exatamente `${<nome>:-}`, e nenhuma outra
    `CQ_SMTP_*`; nenhum outro serviço declara ou menciona `CQ_SMTP_*`
    (a api não recebe o e-mail)."""
    problems: list[str] = []
    env = _env(_service(compose, MAIL_SERVICE))
    for var in MAIL_VARS:
        expected = f"${{{var}:-}}"
        if env.get(var) != expected:
            problems.append(f"{MAIL_SERVICE}: {var} precisa ser {expected}")
    for name, svc in _services(compose).items():
        if not isinstance(svc, dict):
            continue
        declared = sorted(str(var) for var, _ in _declared_vars(svc) if str(var).startswith(MAIL_PREFIX))
        if name == MAIL_SERVICE:
            if extra := sorted(set(declared) - set(MAIL_VARS)):
                problems.append(f"{name}: variáveis de e-mail fora do previsto no Compose: {extra}")
        elif declared:
            problems.append(f"{name}: variáveis de e-mail fora do worker: {declared}")
        elif MAIL_PREFIX in yaml.safe_dump(svc):
            problems.append(f"{name}: menciona {MAIL_PREFIX}* (só o worker recebe o e-mail)")
    return problems


def _check_env_example() -> list[str]:
    if not ENV_EXAMPLE.is_file():
        return [".env.example ausente"]
    problems: list[str] = []
    keys: list[str] = []
    for number, raw in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            problems.append(f".env.example:{number}: linha fora do formato CHAVE=valor")
            continue
        keys.append(match.group(1))
        if not re.fullmatch(r"|<[^<>]+>", match.group(2).strip()):
            problems.append(f".env.example: {match.group(1)} precisa ficar vazio ou com um marcador <…>")
    if sorted(keys) != ENV_EXAMPLE_KEYS:
        problems.append(f".env.example: chaves {sorted(keys)} != {ENV_EXAMPLE_KEYS}")
    return problems


def _dockerignored(path: str, patterns: list[str]) -> bool:
    """Semântica do `.dockerignore`: a última regra que casa decide; `!` reinclui."""
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = (pattern[1:] if negate else pattern).strip().lstrip("/").rstrip("/")
        if pat.startswith("./"):
            pat = pat[2:]
        candidates = [pat] + ([pat[3:]] if pat.startswith("**/") else [])
        if pat == "**" or any(fnmatch.fnmatchcase(path, c) for c in candidates):
            ignored = not negate
    return ignored


def _check_dockerignore() -> list[str]:
    if not DOCKERIGNORE.is_file():
        return [".dockerignore ausente"]
    patterns = [line.strip() for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]
    return [f".dockerignore não exclui {path}" for path in (".env", ".venv")
            if not _dockerignored(path, patterns)]


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUÇÃO, argumentos), juntando continuações e ignorando comentários e linhas vazias."""
    lines = text.splitlines()
    escape = "\\"
    for line in lines:
        directive = re.match(r"^#\s*([A-Za-z]+)\s*=\s*(\S+)\s*$", line)
        if not directive:
            break
        if directive.group(1).lower() == "escape":
            escape = directive.group(2)
    instructions: list[str] = []
    buffer = ""
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw.rstrip().endswith(escape):
            buffer += raw.rstrip()[: -len(escape)] + " "
            continue
        instructions.append((buffer + raw).strip())
        buffer = ""
    if buffer.strip():
        instructions.append(buffer.strip())
    result = []
    for instruction in instructions:
        parts = re.split(r"\s+", instruction, maxsplit=1)
        result.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
    return result


def _flags(arguments: str) -> list[str]:
    flags = []
    for token in arguments.split():
        if not token.startswith("--"):
            break
        flags.append(token)
    return flags


def _effective_user(stages: list[dict], index: int) -> str | None:
    stage = stages[index]
    if stage["users"]:
        return stage["users"][-1]
    if stage["base"] is not None:
        for j in range(index - 1, -1, -1):
            if stages[j]["name"] == stage["base"]:
                return _effective_user(stages, j)
    return None


def _check_dockerfile() -> list[str]:
    if not DOCKERFILE.is_file():
        return ["Dockerfile ausente"]
    return _dockerfile_problems(DOCKERFILE.read_text(encoding="utf-8"))


def _dockerfile_problems(text: str) -> list[str]:
    problems: list[str] = []
    stages: list[dict] = []

    def is_stage(ref: str) -> bool:
        names = {s["name"] for s in stages if s["name"]}
        return ref.lower() in names or (ref.isdigit() and int(ref) < len(stages))

    def pinned_or_stage(ref: str, what: str) -> None:
        if not is_stage(ref) and not PINNED.search(ref):
            problems.append(f"Dockerfile: {what} {ref} sem @sha256:")

    for head, args in _dockerfile_instructions(text):
        if head == "FROM":
            tokens = [t for t in args.split() if not t.startswith("--")]
            image = tokens[0] if tokens else ""
            name = tokens[2].lower() if len(tokens) >= 3 and tokens[1].lower() == "as" else None
            base = image.lower() if is_stage(image) else None
            if base is None and image.lower() != "scratch":
                pinned_or_stage(image, "FROM")
            stages.append({"name": name, "base": base, "users": []})
        elif head == "COPY":
            for flag in _flags(args):
                if flag.startswith("--from="):
                    pinned_or_stage(flag.split("=", 1)[1], "COPY --from=")
        elif head == "RUN":
            for flag in _flags(args):
                if flag.startswith("--mount="):
                    for option in flag[len("--mount="):].split(","):
                        key, _, value = option.partition("=")
                        if key == "from":
                            pinned_or_stage(value, "RUN --mount from=")
        elif head == "USER" and stages:
            stages[-1]["users"].append(args.split()[0] if args.split() else "")
        elif head in ("ENV", "ARG"):
            tokens = shlex.split(args)
            if head == "ENV" and tokens and "=" not in tokens[0]:
                pairs = [(tokens[0], " ".join(tokens[1:]))]
            else:
                pairs = [tuple(t.partition("=")[::2]) for t in tokens]
            for var, value in pairs:
                if SECRET_NAME.search(var) and value:
                    problems.append(f"Dockerfile: {head} {var} com valor (segredo na imagem)")
        problems += [f"Dockerfile: {p}" for p in _url_credential_problems(args)]

    if not stages:
        problems.append("Dockerfile sem FROM")
    else:
        user = _effective_user(stages, len(stages) - 1)
        if user is None:
            problems.append("Dockerfile: a etapa final roda como root (sem USER)")
        elif _is_root(user):
            problems.append("Dockerfile: a última USER da etapa final é root/0")
    return problems


def _final_stage_env(text: str) -> dict[str, str]:
    """as variáveis de `ENV` em vigor na etapa final, com as herdadas da etapa base (`FROM base`),
    como em `_effective_user`."""
    stages: list[dict] = []
    for head, args in _dockerfile_instructions(text):
        if head == "FROM":
            tokens = [t for t in args.split() if not t.startswith("--")]
            image = tokens[0].lower() if tokens else ""
            name = tokens[2].lower() if len(tokens) >= 3 and tokens[1].lower() == "as" else None
            base = next((i for i, s in enumerate(stages) if s["name"] == image), None)
            if base is None and image.isdigit() and int(image) < len(stages):
                base = int(image)
            stages.append({"name": name, "base": base, "env": {}})
        elif head == "ENV" and stages:
            tokens = shlex.split(args)
            if tokens and "=" not in tokens[0]:
                pairs = [(tokens[0], " ".join(tokens[1:]))]
            else:
                pairs = [tuple(t.partition("=")[::2]) for t in tokens]
            stages[-1]["env"].update(pairs)
    chain: list[dict[str, str]] = []
    index = len(stages) - 1 if stages else None
    while index is not None:
        chain.append(stages[index]["env"])
        index = stages[index]["base"]
    env: dict[str, str] = {}
    for layer in reversed(chain):
        env.update(layer)
    return env


def _check_dockerfile_timezone() -> list[str]:
    """A etapa final define `PYTHONTZPATH` vazio, para o `zoneinfo` usar só o `tzdata`
    do pacote (a mesma base de fusos no host e na imagem)."""
    if not DOCKERFILE.is_file():
        return []  # a ausência já é relatada por _check_dockerfile
    if _final_stage_env(DOCKERFILE.read_text(encoding="utf-8")).get("PYTHONTZPATH") != "":
        return ['Dockerfile: a etapa final precisa de ENV PYTHONTZPATH="" (a mesma base de fusos)']
    return []


def _requirement(text: str) -> tuple[str, bool]:
    """(nome normalizado, tem marcador de ambiente) de um requisito PEP 508."""
    match = re.match(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", text)
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower() if match else ""
    return name, ";" in text


def _check_dependencies() -> list[str]:
    """As dependências novas sem marcador, no
    `pyproject.toml` de cada pacote e no `uv.lock`, e sem `psycopg-pool`."""
    problems: list[str] = []
    for package, path in (("cq-server", SERVER_PYPROJECT), ("cq-client", CLIENT_PYPROJECT)):
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
        requirements = [_requirement(str(d)) for d in project.get("dependencies", [])]
        for dep in REQUIRED_DEPENDENCIES[package]:
            if [marker for name, marker in requirements if name == dep] != [False]:
                problems.append(f"{package}: {dep} precisa aparecer uma vez nas dependências, sem marcador")
        optional = [_requirement(str(d))[0] for extra in project.get("optional-dependencies", {}).values()
                    for d in extra]
        for dep in REMOVED_DEPENDENCIES:
            if dep in [name for name, _ in requirements] + optional:
                problems.append(f"{package}: {dep} não pode ser dependência (foi retirada)")
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock.get("package", [])}
    for package, deps in REQUIRED_DEPENDENCIES.items():
        locked = packages.get(package, {}).get("dependencies", [])
        for dep in deps:
            entries = [d for d in locked if d.get("name") == dep]
            if len(entries) != 1 or "marker" in entries[0]:
                problems.append(f"uv.lock: {package} precisa depender de {dep} uma vez, sem marcador")
    for dep in REMOVED_DEPENDENCIES:
        if dep in packages:
            problems.append(f"uv.lock ainda tem {dep} (foi retirada)")
    return problems


def _is_env_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def _check_git() -> list[str]:
    problems: list[str] = []
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, timeout=60)
    if listed.returncode != 0:
        return ["git ls-files falhou"]
    files = [f for f in listed.stdout.decode("utf-8", "replace").split("\0") if f]
    if tracked := [f for f in files if _is_env_file(pathlib.PurePosixPath(f).name)]:
        problems.append(f"arquivos .env no git: {tracked}")
    ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env"], cwd=REPO, timeout=60)
    if ignored.returncode != 0:
        problems.append(".env não é ignorado pelo git")
    example = subprocess.run(["git", "check-ignore", "-q", "--no-index", ".env.example"], cwd=REPO,
                             timeout=60)
    if example.returncode != 1:
        problems.append(".env.example está ignorado pelo git (o modelo precisa ser versionado)")
    return problems


_PIN = "@sha256:" + "a" * 64


def _self_check() -> list[str]:
    """Controles das próprias checagens (contra o vazio): cada detector acusa o caso ruim e aceita o
    bom. Ficam nesta função porque só fazem sentido junto com os arquivos que conferem."""
    cases: list[tuple[str, Any, Any]] = [
        ("segredo com :?", _only_safe_interpolation("${CQ_DB_PASSWORD:?defina a senha}"), True),
        ("segredo com :- vazio", _only_safe_interpolation("${CQ_ADMIN_TOKEN:-}"), True),
        ("segredo com padrão", _only_safe_interpolation("${CQ_DB_PASSWORD:-senha123}"), False),
        ("segredo literal", _only_safe_interpolation("senha-literal"), False),
        ("segredo sem chaves", _only_safe_interpolation("$CQ_DB_PASSWORD"), False),
        ("segredo com sufixo", _only_safe_interpolation("${CQ_DB_PASSWORD}x"), False),
        ("segredo nulo", _only_safe_interpolation(None), False),
        ("URL interpolada", _url_credential_problems(
            "postgresql://cq:${CQ_DB_PASSWORD:?defina a senha}@postgres:5432/cq"), []),
        ("URL literal", len(_url_credential_problems("postgresql://cq:hunter22@postgres:5432/cq")), 1),
        ("URL com padrão", len(_url_credential_problems("postgresql://cq:${P:-x}@postgres/cq")), 1),
        ("URL misturada", len(_url_credential_problems("postgresql://cq:a${P}@postgres/cq")), 1),
        ("URL sem senha", _url_credential_problems("http://127.0.0.1:8000/health"), []),
        (".env por .env*", _dockerignored(".env", [".env*", "!.env.example"]), True),
        (".env.example reincluído", _dockerignored(".env.example", [".env*", "!.env.example"]), False),
        (".venv por .venv/", _dockerignored(".venv", ["/.venv/"]), True),
        (".env não coberto", _dockerignored(".env", ["*.pyc", ".git"]), False),
        (".env reincluído", _dockerignored(".env", [".env", "!.env"]), False),
        ("postgres -c", _postgres_settings(
            "postgres -c log_error_verbosity=terse -c log_min_error_statement=panic"),
         {"log_error_verbosity": "terse", "log_min_error_statement": "panic"}),
        ("postgres lista, o último vale", _postgres_settings(
            ["postgres", "-c", "log_error_verbosity=terse", "--log-error-verbosity=verbose"]),
         {"log_error_verbosity": "verbose"}),
        ("instruções", _dockerfile_instructions(
            f"# escape=\\\nFROM a{_PIN} AS b\n\nRUN x \\\n  # comentário\n  y\nuser nonroot\n"),
         [("FROM", f"a{_PIN} AS b"), ("RUN", "x    y"), ("USER", "nonroot")]),
        ("Dockerfile bom", _dockerfile_problems(
            f"FROM python:3.12-slim{_PIN} AS builder\nCOPY --from=ghcr.io/astral-sh/uv:0.11.16{_PIN} /uv /bin/\n"
            f"FROM python:3.12-slim{_PIN}\nCOPY --from=builder /app /app\nUSER nonroot\n"), []),
        ("herda USER da etapa base", _dockerfile_problems(
            f"FROM python{_PIN} AS base\nUSER 999\nFROM base AS final\nCOPY --from=0 /a /a\n"), []),
        ("FROM sem digest", _dockerfile_problems("FROM python:3.12-slim\nUSER nonroot\n"),
         ["Dockerfile: FROM python:3.12-slim sem @sha256:"]),
        ("COPY --from sem digest", _dockerfile_problems(
            f"FROM python{_PIN}\nCOPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/\nUSER nonroot\n"),
         ["Dockerfile: COPY --from= ghcr.io/astral-sh/uv:0.11.16 sem @sha256:"]),
        ("RUN --mount from sem digest", _dockerfile_problems(
            f"FROM python{_PIN}\nRUN --mount=type=bind,from=uv:latest,target=/x true\nUSER nonroot\n"),
         ["Dockerfile: RUN --mount from= uv:latest sem @sha256:"]),
        ("USER root no fim", _dockerfile_problems(f"FROM python{_PIN}\nUSER nonroot\nUSER root\n"),
         ["Dockerfile: a última USER da etapa final é root/0"]),
        ("USER só no builder", _dockerfile_problems(
            f"FROM python{_PIN} AS builder\nUSER nonroot\nFROM python{_PIN}\n"),
         ["Dockerfile: a etapa final roda como root (sem USER)"]),
        ("USER 0:0", _dockerfile_problems(f"FROM python{_PIN}\nUSER 0:0\n"),
         ["Dockerfile: a última USER da etapa final é root/0"]),
        ("ENV com segredo", _dockerfile_problems(f"FROM python{_PIN}\nENV CQ_ADMIN_TOKEN=abc\nUSER nonroot\n"),
         ["Dockerfile: ENV CQ_ADMIN_TOKEN com valor (segredo na imagem)"]),
        # PYTHONTZPATH na etapa final, requisitos e variáveis dos bots.
        ("PYTHONTZPATH vazio na etapa final", _final_stage_env(
            f'FROM a{_PIN} AS b\nENV X=1\nFROM c{_PIN}\nENV A=1 \\\n    PYTHONTZPATH="" \\\n    B=2\n'),
         {"A": "1", "PYTHONTZPATH": "", "B": "2"}),
        ("PYTHONTZPATH só no builder", _final_stage_env(
            f'FROM a{_PIN} AS builder\nENV PYTHONTZPATH=""\nFROM c{_PIN}\nUSER nonroot\n'), {}),
        ("PYTHONTZPATH herdado da base", _final_stage_env(
            f'FROM a{_PIN} AS base\nENV PYTHONTZPATH ""\nFROM base AS final\nENV Y=2\n'),
         {"PYTHONTZPATH": "", "Y": "2"}),
        ("PYTHONTZPATH com caminho", _final_stage_env(
            f"FROM a{_PIN}\nENV PYTHONTZPATH=/usr/share/zoneinfo\n").get("PYTHONTZPATH"), "/usr/share/zoneinfo"),
        ("requisito sem marcador", _requirement("httpx2>=2.13,<3"), ("httpx2", False)),
        ("requisito com marcador", _requirement("tzdata>=2026.4; sys_platform == 'win32'"), ("tzdata", True)),
        ("requisito com extra", _requirement("psycopg[binary]>=3.3,<3.4"), ("psycopg", False)),
        ("nome normalizado", _requirement("Psycopg_Pool>=3.2"), ("psycopg-pool", False)),
        ("chave-mestra nos dois", _check_bots({"services": {
            "api": {"environment": {"CQ_MASTER_KEY": "${CQ_MASTER_KEY:-}"}},
            "worker": {"environment": {"CQ_MASTER_KEY": "${CQ_MASTER_KEY:-}"}},
            "postgres": {"environment": {}}}}), []),
        ("chave-mestra ausente e URL do Telegram", _check_bots({"services": {
            "api": {"environment": {"CQ_TELEGRAM_API_URL": "http://127.0.0.1:1"}},
            "worker": {"environment": {"CQ_MASTER_KEY": "${CQ_MASTER_KEY}", "CQ_BOT_LEASE_SECONDS": "60"}},
            "postgres": {"environment": {"CQ_MASTER_KEY": "${CQ_MASTER_KEY:-}"}}}}),
         ["api: CQ_MASTER_KEY precisa ser ${CQ_MASTER_KEY:-}",
          "api: CQ_TELEGRAM_API_URL não é definida no Compose (vale o padrão)",
          "worker: CQ_MASTER_KEY precisa ser ${CQ_MASTER_KEY:-}",
          "api: variáveis dos bots fora do previsto no Compose: ['CQ_TELEGRAM_API_URL']",
          "worker: variáveis dos bots fora do previsto no Compose: ['CQ_BOT_LEASE_SECONDS']",
          "postgres: menciona CQ_MASTER_KEY (só a api e o worker recebem a chave-mestra)"]),
        # as variáveis do e-mail só no worker, cada uma `${<nome>:-}`; a senha como segredo.
        ("e-mail no worker", _check_mail({"services": {
            "api": {"environment": {"CQ_MASTER_KEY": "${CQ_MASTER_KEY:-}"}},
            "worker": {"environment": {var: f"${{{var}:-}}" for var in MAIL_VARS}},
            "postgres": {"environment": {}}}}), []),
        ("e-mail fora do previsto", _check_mail({"services": {
            "api": {"environment": {"CQ_SMTP_HOST": "${CQ_SMTP_HOST:-}"}},
            "worker": {"environment": {**{var: f"${{{var}:-}}" for var in MAIL_VARS if var != "CQ_SMTP_FROM"},
                                       "CQ_SMTP_PASSWORD": "senha-literal",
                                       "CQ_SMTP_PORT": "${CQ_SMTP_PORT:-587}", "CQ_SMTP_DEBUG": "1"}},
            "postgres": {"command": "postgres -c x=${CQ_SMTP_HOST}", "environment": {}}}}),
         ["worker: CQ_SMTP_PORT precisa ser ${CQ_SMTP_PORT:-}",
          "worker: CQ_SMTP_PASSWORD precisa ser ${CQ_SMTP_PASSWORD:-}",
          "worker: CQ_SMTP_FROM precisa ser ${CQ_SMTP_FROM:-}",
          "api: variáveis de e-mail fora do worker: ['CQ_SMTP_HOST']",
          "worker: variáveis de e-mail fora do previsto no Compose: ['CQ_SMTP_DEBUG']",
          "postgres: menciona CQ_SMTP_* (só o worker recebe o e-mail)"]),
        ("senha do e-mail interpolada", _check_compose_secrets({"services": {
            "worker": {"environment": {"CQ_SMTP_PASSWORD": "${CQ_SMTP_PASSWORD:-}"}}}}), []),
        ("senha do e-mail literal", _check_compose_secrets({"services": {
            "worker": {"environment": {"CQ_SMTP_PASSWORD": "senha-literal"}}}}),
         ["worker: CQ_SMTP_PASSWORD precisa vir só de ${…}, sem valor literal nem padrão"]),
        ("senha do e-mail com padrão", _check_compose_secrets({"services": {
            "worker": {"environment": {"CQ_SMTP_PASSWORD": "${CQ_SMTP_PASSWORD:-senha}"}}}}),
         ["worker: CQ_SMTP_PASSWORD precisa vir só de ${…}, sem valor literal nem padrão"]),
    ]
    return [f"autoteste {label}: {actual!r} != {expected!r}"
            for label, actual, expected in cases if actual != expected]


def _run(problems: list[str], check: Callable[..., list[str]], *args: Any) -> None:
    try:
        problems.extend(check(*args))
    except Exception as exc:  # uma estrutura inesperada vira problema relatado, sem esconder os outros
        problems.append(f"{check.__name__}: {type(exc).__name__}: {exc}")


def test_deploy_files_match_plan() -> None:
    """Os serviços, o banco, a imagem e os segredos, como nas variáveis do Compose:
    - serviços api, worker e postgres (só `engine` pode se somar), `environment` em mapa;
    - postgres: imagem por digest (a mesma dos testes), volume `pgdata:/var/lib/postgresql`,
      `POSTGRES_PASSWORD` = `${CQ_DB_PASSWORD:?…}`, `log_error_verbosity=terse` e
      `log_min_error_statement=panic`;
    - api: porta só em 127.0.0.1, `CQ_DATABASE_URL` com `${CQ_DB_PASSWORD:?`,
      `CQ_ADMIN_TOKEN: ${CQ_ADMIN_TOKEN:-}`;
    - worker: console em arquivo, `restart: unless-stopped`, sem `CQ_ADMIN_TOKEN`;
    - api/worker com `service_healthy`;
    - variáveis de segredo e URLs com credencial só por `${…}`;
    - `.env.example` só com marcadores; `.dockerignore` com `.env` e `.venv`;
    - Dockerfile com imagens por digest e usuário final não-root;
    - nenhum `.env` no git.

    Oferecimentos e bots, com a chave-mestra dos bots,
    o fuso e as dependências:
    - api e worker com `CQ_MASTER_KEY: ${CQ_MASTER_KEY:-}` e sem `CQ_TELEGRAM_API_URL`; nenhuma
      outra variável de `BotSettings` no Compose; a chave-mestra em nenhum outro serviço;
    - etapa final do `Dockerfile` com `PYTHONTZPATH=""`;
    - `httpx2`, `httpcore2`, `cryptography` e `tzdata` no `cq-server` e `tzdata` no `cq-client`, sem
      marcador, no `pyproject.toml` e no `uv.lock`; sem `psycopg-pool`;
    - `.env.example` com as mesmas chaves da API base.

    Vínculo do aluno (lista e convites):
    - worker com as 7 variáveis do e-mail (`CQ_SMTP_HOST`, `CQ_SMTP_PORT`, `CQ_SMTP_SECURITY`,
      `CQ_SMTP_USERNAME`, `CQ_SMTP_PASSWORD`, `CQ_SMTP_FROM` e `CQ_SMTP_TIMEOUT_SECONDS`), cada uma
      `${<nome>:-}`, e nenhuma outra `CQ_SMTP_*`; a senha passa pela checagem de segredos (só por
      `${…}`);
    - api e demais serviços sem nenhuma `CQ_SMTP_*`;
    - `.env.example` sem mudança.
    Vermelho no esqueleto: o `compose.yaml` ainda não tem as variáveis do e-mail.
    """
    assert COMPOSE.is_file(), "compose.yaml ausente na raiz do repositório"
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    problems: list[str] = []
    _run(problems, _self_check)
    for check in (_check_services, _check_postgres, _check_api, _check_worker, _check_compose_secrets):
        _run(problems, check, compose)
    for check in (_check_env_example, _check_dockerignore, _check_dockerfile, _check_git):
        _run(problems, check)
    # Oferecimentos e bots.
    _run(problems, _check_bots, compose)
    for check in (_check_dockerfile_timezone, _check_dependencies):
        _run(problems, check)
    # Vínculo do aluno: as variáveis do e-mail.
    _run(problems, _check_mail, compose)
    assert not problems, "Problemas nos arquivos de implantação:\n- " + "\n- ".join(problems)
