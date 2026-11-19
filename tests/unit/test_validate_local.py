"""Validação local do CI num container, só pelo plano de comandos (`plan_commands`): nenhum Docker roda.


O que se confere no plano (`plan_commands(export_dir, *, run_id, repo_root) -> list[list[str]]`):
- todo recurso Docker criado ou removido (`--name`, `--network`, `network create|rm`, `volume create|rm`, `rm`) com
  o prefixo `cq-ci-local-<id>`; pelo menos uma rede própria e um container;
- a imagem `python:3.12-slim-trixie` com o **mesmo digest** do `Dockerfile` da raiz;
- o container do Python com `--user` sem privilégio (nem `0`, nem `root`);
- a pasta exportada montada (`-v`, `--volume` ou `--mount`);
- dentro do container, o passo do age Linux (`scripts/fetch_age.py --platform linux-amd64`, com o `CQ_TEST_AGE_DIR`)
  **antes** do `ops/ci/ci_suite.py fast`;
- nenhuma montagem de `/var/run/docker.sock` nem `//./pipe/docker_engine` (`FORBIDDEN_MOUNTS`), em lugar nenhum;
- a remoção só do que criou (nada de `prune`, `rmi` nem `image rm`), depois dos passos, e tudo o que foi
  criado sem `--rm` é removido;
- o `--export-dir` fora do repositório: dentro dele (ou a própria raiz) → `ValueError` **[I]** (a regra só diz
  fora do repositório; a recusa antes de qualquer comando é a forma que um teste sem Docker alcança).

**[I]** A leitura do plano como texto (cada argv unido por espaços, os comandos em ordem) para a ordem dos passos de
dentro do container é deste teste: nada fixa se eles vão num `sh -c` só ou em `docker exec` separados.

No esqueleto, `plan_commands` lança `NotImplementedError`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.helpers.ops import REPO, load_ops

SCRIPT = "ops/ci/validate_local.py"
RUN_ID = "t085a1"
PREFIX = f"cq-ci-local-{RUN_ID}"
FORBIDDEN = ("/var/run/docker.sock", "//./pipe/docker_engine")
_DIGEST = re.compile(r"^FROM python:3\.12-slim-trixie@sha256:([0-9a-f]{64})\b", re.MULTILINE)


def _module():
    return load_ops(SCRIPT)


def _python_image() -> str:
    found = _DIGEST.findall((REPO / "Dockerfile").read_text(encoding="utf-8"))
    assert found and len(set(found)) == 1, "o Dockerfile da raiz sem um digest único do python:3.12-slim-trixie"
    return f"python:3.12-slim-trixie@sha256:{found[0]}"


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    path = tmp_path / "exportado"
    path.mkdir()
    (path / "pyproject.toml").write_text("[tool.uv.workspace]\nmembers = []\n", encoding="utf-8")
    return path


def _plan(export_dir: Path) -> list[list[str]]:
    """O plano da execução `RUN_ID` (chamado no corpo de cada teste: no esqueleto, o `NotImplementedError` é uma
    falha do caso, não um erro de preparação)."""
    commands = _module().plan_commands(export_dir, run_id=RUN_ID, repo_root=REPO)
    assert commands and all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in commands)
    return commands


def _docker_args(command: list[str]) -> list[str] | None:
    """Os argumentos depois do `docker` (o executável pode vir com caminho), ou `None` se não é `docker`."""
    if Path(command[0]).name.lower().removesuffix(".exe") != "docker":
        return None
    return command[1:]


def _created(plan: list[list[str]]) -> tuple[set[str], set[str]]:
    """(recursos criados, containers com `--rm`)."""
    created: set[str] = set()
    auto_removed: set[str] = set()
    for command in plan:
        args = _docker_args(command)
        if args is None:
            continue
        if args[:2] in (["network", "create"], ["volume", "create"]):
            created.add(args[-1])
        if args and args[0] in ("run", "create"):
            for i, arg in enumerate(args):
                if arg == "--name":
                    created.add(args[i + 1])
                    if "--rm" in args:
                        auto_removed.add(args[i + 1])
                elif arg.startswith("--name="):
                    created.add(arg.split("=", 1)[1])
                    if "--rm" in args:
                        auto_removed.add(arg.split("=", 1)[1])
    return created, auto_removed


def _removed(plan: list[list[str]]) -> tuple[set[str], int]:
    """(recursos removidos, índice do primeiro comando de remoção)."""
    removed: set[str] = set()
    first = len(plan)
    for index, command in enumerate(plan):
        args = _docker_args(command)
        if args is None:
            continue
        if args[:2] in (["network", "rm"], ["volume", "rm"]):
            targets = [a for a in args[2:] if not a.startswith("-")]
        elif args and args[0] in ("rm", "stop", "kill") or args[:2] == ["container", "rm"]:
            start = 2 if args[0] == "container" else 1
            targets = [a for a in args[start:] if not a.startswith("-")]
        else:
            continue
        removed.update(targets)
        first = min(first, index)
    return removed, first


def test_every_docker_resource_has_the_run_prefix(export_dir: Path) -> None:
    """todo nome criado, usado como rede ou removido começa por `cq-ci-local-<id>`; há uma rede
    própria e pelo menos um container."""
    plan = _plan(export_dir)
    created, _ = _created(plan)
    removed, _ = _removed(plan)
    networks = []
    for command in plan:
        args = _docker_args(command) or []
        for i, arg in enumerate(args):
            if arg == "--network":
                networks.append(args[i + 1])
            elif arg.startswith("--network="):
                networks.append(arg.split("=", 1)[1])
    names = created | removed | set(networks)
    assert names, "o plano não cria nenhum recurso Docker"
    for name in names:
        assert name.startswith(PREFIX), f"recurso fora do prefixo {PREFIX}: {name}"
    assert any((_docker_args(c) or [])[:2] == ["network", "create"] for c in plan), "sem rede própria"
    assert any((_docker_args(c) or [])[:1] == ["run"] for c in plan), "sem container"


def test_python_container_uses_the_dockerfile_digest_an_unprivileged_user_and_mounts_the_export(
        export_dir: Path) -> None:
    """o container do Python com a imagem do `Dockerfile` (mesmo digest), `--user` sem privilégio e a pasta
    exportada montada."""
    plan = _plan(export_dir)
    image = _python_image()
    runs = [(_docker_args(c) or []) for c in plan if (_docker_args(c) or [])[:1] == ["run"] and image in c]
    assert len(runs) == 1, f"containers com {image}: {len(runs)}"
    args = runs[0]
    users = [args[i + 1] for i, a in enumerate(args) if a in ("--user", "-u")]
    users += [a.split("=", 1)[1] for a in args if a.startswith("--user=")]
    assert len(users) == 1, f"--user: {users}"
    uid = users[0].split(":", 1)[0]
    assert uid not in ("", "0", "root"), f"usuário com privilégio: {users[0]}"
    mounts = [args[i + 1] for i, a in enumerate(args) if a in ("-v", "--volume", "--mount")]
    mounts += [a.split("=", 1)[1] for a in args if a.startswith(("--volume=", "--mount="))]
    exported = {str(export_dir), export_dir.as_posix(), str(export_dir.resolve()), export_dir.resolve().as_posix()}
    assert any(any(form in mount for form in exported) for mount in mounts), f"a pasta exportada não montada: {mounts}"


def test_the_linux_age_step_runs_before_the_fast_suite_inside_the_container(export_dir: Path) -> None:
    """o mesmo passo do workflow, `scripts/fetch_age.py --platform linux-amd64`, com o `CQ_TEST_AGE_DIR`,
    antes do `ops/ci/ci_suite.py fast`."""
    plan = _plan(export_dir)
    text = "\n".join(" ".join(command) for command in plan)
    age = text.find("scripts/fetch_age.py")
    suite = text.find("ops/ci/ci_suite.py fast")
    assert age >= 0, "sem o passo do age"
    assert suite >= 0, "sem o ci_suite.py fast"
    assert age < suite, "o age vem antes da suíte"
    assert "--platform linux-amd64" in text
    assert "CQ_TEST_AGE_DIR" in text[:suite], "o CQ_TEST_AGE_DIR definido antes da suíte"


def test_the_docker_socket_is_never_mounted(export_dir: Path) -> None:
    """nenhum argumento do plano cita o socket do Docker (`FORBIDDEN_MOUNTS`), com qualquer barra."""
    plan = _plan(export_dir)
    module = _module()
    assert tuple(module.FORBIDDEN_MOUNTS) == FORBIDDEN
    for command in plan:
        for arg in command:
            normalized = arg.replace("\\", "/")
            for forbidden in FORBIDDEN:
                assert forbidden not in normalized, f"montagem proibida no plano: {command}"


def test_cleanup_removes_only_what_it_created_after_the_steps(export_dir: Path) -> None:
    """os comandos de remoção vêm depois de todo `run`, só citam recursos criados pelo plano, e todo recurso
    criado sem `--rm` é removido; nenhum `prune`, `rmi` nem `image rm`."""
    plan = _plan(export_dir)
    created, auto_removed = _created(plan)
    removed, first_removal = _removed(plan)
    assert removed <= created, f"remove o que não criou: {sorted(removed - created)}"
    assert created - auto_removed <= removed, f"criado e não removido: {sorted(created - auto_removed - removed)}"
    last_run = max(i for i, c in enumerate(plan) if (_docker_args(c) or [])[:1] == ["run"])
    assert first_removal > last_run, "a remoção vem depois dos passos"
    for command in plan:
        args = _docker_args(command) or []
        assert "prune" not in args and "rmi" not in args and args[:2] != ["image", "rm"], command


def test_two_runs_differ_only_in_the_resource_names(export_dir: Path) -> None:
    """Cada execução tem o seu id: trocar o id troca só os nomes dos recursos."""
    module = _module()
    first = module.plan_commands(export_dir, run_id=RUN_ID, repo_root=REPO)
    second = module.plan_commands(export_dir, run_id="t085b2", repo_root=REPO)
    renamed = [[arg.replace("cq-ci-local-t085b2", PREFIX) for arg in command] for command in second]
    assert renamed == first


@pytest.mark.parametrize("inside", ["raiz", "subpasta"])
def test_export_dir_inside_the_repository_is_refused(inside: str) -> None:
    """o `--export-dir` tem de estar fora do repositório; a raiz ou uma pasta dentro dela → `ValueError`,
    antes de qualquer comando."""
    path = REPO if inside == "raiz" else REPO / "ops"
    with pytest.raises(ValueError):
        _module().plan_commands(path, run_id=RUN_ID, repo_root=REPO)
