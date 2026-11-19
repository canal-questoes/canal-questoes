"""Teste de sistema: a pilha sobe a partir do pacote de implantação
(a implantação pelo pacote, de ponta a ponta).

`build_bundle(REPO, out, source="worktree")` gera o pacote num diretório temporário (só lê a árvore real; nada é
escrito nela), que é descompactado noutro; um `.env` de teste com segredos gerados (nunca o `.env` real) fica ao lado
do `compose.yaml` extraído; a pilha sobe com `docker compose -p cq-systest-deploy -f <dir>/compose.yaml --env-file
<dir>/.env up -d --build`, com porta livre; `/health` responde 200; `GET /v1/system/doctor` traz `backup.age` em
`warning` com a mensagem de "não configurado" (a montagem padrão aponta para o `ops/backup/status-unconfigured` do
pacote, e `CQ_BACKUP_STATUS_FILE` fica vazia). O `finally` faz `down -v` **só** desse projeto. Prazo explícito em cada
comando. Nunca roda `docker compose config` (imprime os segredos), `prune`, a `cq-demo` nem o projeto `cq`.

O caso depende também do `doctor`, além do pacote e do `compose.yaml`.
No esqueleto, `build_bundle` lança `NotImplementedError`.

**[I]** Escolhas deste teste: os membros do pacote ficam na raiz do arquivo (o `compose.yaml` em
`<dir>/compose.yaml`); a extração recusa membro fora do diretório; o `.env` de teste tem só as variáveis
obrigatórias e a chave-mestra (as demais ficam vazias, o padrão do servidor); os segredos gerados saem das mensagens
de erro do teste (`_scrub`).
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import tarfile
import time
from pathlib import Path

import httpx2
import pytest

from tests.helpers.api import auth
from tests.helpers.ops import REPO, clean_env, load_ops

pytestmark = pytest.mark.system

PROJECT = "cq-systest-deploy"
BUILD_TIMEOUT = 1800
COMMAND_TIMEOUT = 300
HEALTH_DEADLINE_SECONDS = 180
BACKUP_NOT_CONFIGURED = ("backup.age", "warning", "not_configured")


def _require_docker() -> None:
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"Docker indisponível para o teste de sistema ({type(exc).__name__}). "
                    "Instale/ligue o Docker ou rode `pytest -m 'not system'`.")
    if info.returncode != 0:
        pytest.fail("Docker indisponível para o teste de sistema (`docker info` falhou). "
                    "Ligue o Docker ou rode `pytest -m 'not system'`.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _extract(archive_path: Path, target: Path) -> None:
    target.mkdir()
    root = target.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            destination = (target / member.name).resolve()
            assert destination == root or root in destination.parents, "membro do pacote fora do diretório"
            assert member.isfile() or member.isdir(), "membro do pacote que não é arquivo nem diretório"
        archive.extractall(target, filter="data")


class _Stack:
    """O `docker compose` do projeto descartável, a partir do diretório extraído."""

    def __init__(self, app: Path, env_file: Path, hidden: list[str]) -> None:
        self.app = app
        self.env_file = env_file
        self.hidden = hidden
        self.env = clean_env()

    def scrub(self, text: str) -> str:
        for value in self.hidden:
            text = text.replace(value, "[segredo]")
        return text

    def run(self, *args: str, timeout: float = COMMAND_TIMEOUT, check: bool = True) -> subprocess.CompletedProcess:
        assert "config" not in args, "o teste nunca roda `docker compose config` (imprime segredos)"
        cmd = ["docker", "compose", "-p", PROJECT, "-f", str(self.app / "compose.yaml"), "--env-file",
               str(self.env_file), *args]
        result = subprocess.run(cmd, cwd=self.app, env=self.env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        if check:
            assert result.returncode == 0, (
                f"`docker compose {' '.join(args)}` falhou ({result.returncode}):\n"
                f"{self.scrub(result.stderr[-3000:])}")
        return result


def _wait_health(http: httpx2.Client, started: float, stack: _Stack) -> None:
    deadline = started + HEALTH_DEADLINE_SECONDS
    last = "sem resposta"
    while time.monotonic() < deadline:
        try:
            resp = http.get("/health")
        except Exception as exc:  # conexão recusada enquanto os serviços sobem
            last = type(exc).__name__
        else:
            last = f"{resp.status_code} {stack.scrub(resp.text[:300])}"
            if resp.status_code == 200:
                return
        time.sleep(1)
    pytest.fail(f"/health não ficou ok em {HEALTH_DEADLINE_SECONDS} s depois do `up` (último: {last})")


def test_stack_boots_from_bundle_and_reports_backup_not_configured(tmp_path: Path) -> None:
    """A implantação pelo pacote: o pacote da árvore de trabalho sobe num projeto descartável, `/health` 200 e
    `backup.age` em `warning` "não configurado" pelo `doctor` (a montagem padrão do pacote)."""
    from cq_server.doctor import DOCTOR_REASON_MESSAGES

    make_bundle = load_ops("ops/deploy/make_bundle.py")
    out_dir = tmp_path / "pacote"
    out_dir.mkdir()
    out = out_dir / "cq-bundle-sistema.tar.gz"

    code = make_bundle.build_bundle(REPO, out, source="worktree")

    assert code == 0, "o pacote da árvore de trabalho falhou"
    app = tmp_path / "app"
    _extract(out, app)
    assert (app / "compose.yaml").is_file()
    assert sorted(path.name for path in (app / "ops" / "backup" / "status-unconfigured").iterdir()) == ["README.md"]
    assert not (app / ".env").exists() and not (app / ".git").exists()

    _require_docker()
    db_password = secrets.token_urlsafe(24)
    admin_token = secrets.token_urlsafe(32)
    master_key = secrets.token_urlsafe(32)
    port = _free_port()
    env_file = app / ".env"
    values = {"CQ_DB_PASSWORD": db_password, "CQ_ADMIN_TOKEN": admin_token, "CQ_MASTER_KEY": master_key,
              "CQ_API_PORT": str(port)}
    env_file.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    if os.name == "posix":
        env_file.chmod(0o600)
    stack = _Stack(app, env_file, [db_password, admin_token, master_key])
    try:
        stack.run("down", "-v", "--remove-orphans")  # ambiente limpo: só o projeto do próprio teste
        stack.run("up", "-d", "--build", timeout=BUILD_TIMEOUT)
        started = time.monotonic()
        with httpx2.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
            _wait_health(http, started, stack)
            resp = http.get("/v1/system/doctor", headers=auth(admin_token))
            assert resp.status_code == 200, f"doctor: {resp.status_code} {stack.scrub(resp.text[:500])}"
            body = resp.json()
            assert stack.scrub(resp.text) == resp.text, "o doctor devolveu um segredo gerado"
        backup = [check for check in body["checks"] if check["id"] == "backup.age"]
        assert len(backup) == 1, "o doctor sem a verificação backup.age"
        assert backup[0]["result"] == "warning"
        assert backup[0]["message"] == DOCTOR_REASON_MESSAGES[BACKUP_NOT_CONFIGURED]
    finally:
        stack.run("down", "-v", "--remove-orphans", check=False)
