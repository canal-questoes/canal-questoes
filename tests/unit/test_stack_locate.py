"""Descoberta do diretório da pilha e do alvo do Docker.


- `locate_stack_dir(profile, env, cwd)`: o `stack_dir` do perfil > `CQ_STACK_DIR` (vazia = ausente) > o `cwd`
  ou o ancestral mais próximo com os 4 marcadores (`STACK_MARKERS`, só a existência); senão
  `StackDirNotFound` (2, mensagem fixa, sem caminho).
- `check_docker_target(env)`: `DOCKER_HOST` vazio, `unix://…` e `npipe://…` aceitos; qualquer outro esquema
  (`tcp://`, `ssh://`…) e `DOCKER_CONTEXT` não vazia → `StackTargetRefused` (2, sem eco).

Os diretórios são do `tmp_path` (`make_stack_dir`); nenhum Docker, nenhum subprocesso.
"""

from __future__ import annotations

import pathlib

import pytest

from cq_client.profile import EffectiveProfile
from cq_client.stack import (
    STACK_MARKERS,
    StackDirNotFound,
    StackError,
    StackTargetRefused,
    check_docker_target,
    locate_stack_dir,
)

from tests.helpers.cli_runner import make_stack_dir

LOCAL_URL = "http://127.0.0.1:18765"

STACK_DIR_NOT_FOUND = ("Diretório da pilha não encontrado: defina stack_dir no perfil local ou CQ_STACK_DIR "
                       "(veja docs/operacao/inicio-rapido.md).")
TARGET_REFUSED = ("O Docker aponta para um alvo que não é local (DOCKER_HOST ou DOCKER_CONTEXT); o CLI só opera a "
                  "pilha deste computador.")


def _profile(tmp_path: pathlib.Path, stack_dir: pathlib.Path | None = None) -> EffectiveProfile:
    """Um perfil local efetivo (o tipo real do esqueleto), com o `stack_dir` dado."""
    return EffectiveProfile(
        name="local" if stack_dir is not None else None, kind="local", api_url=LOCAL_URL, token=None,
        stack_dir=stack_dir, compose_project="cq",
        origins={"name": "option" if stack_dir is not None else "default", "kind": "inferred",
                 "api_url": "profile" if stack_dir is not None else "file", "token": "default",
                 "compose_project": "default"},
        named=stack_dir is not None, config_path=tmp_path / "cq.toml")


def _same(found: pathlib.Path, expected: pathlib.Path) -> bool:
    return pathlib.Path(found).resolve() == expected.resolve()


def _assert_not_found(exc: StackDirNotFound, *paths: pathlib.Path) -> None:
    assert isinstance(exc, StackError)
    assert exc.exit_code == 2
    assert exc.message == STACK_DIR_NOT_FOUND == str(exc)
    for path in paths:
        for text in (str(exc), repr(exc), exc.message):
            assert str(path) not in text and path.name not in text and path.as_posix() not in text


# --------------------------------------------------------------------------------------------------
# locate_stack_dir


def test_profile_stack_dir_wins_over_env_and_cwd(tmp_path) -> None:
    """o `stack_dir` do perfil vence `CQ_STACK_DIR` e o diretório atual (mesmo com os marcadores)."""
    from_profile = make_stack_dir(tmp_path, name="do-perfil")
    from_env = make_stack_dir(tmp_path, name="da-variavel")
    from_cwd = make_stack_dir(tmp_path, name="do-cwd")
    found = locate_stack_dir(_profile(tmp_path, from_profile), {"CQ_STACK_DIR": str(from_env)}, from_cwd)
    assert _same(found, from_profile)
    # os marcadores reais do esqueleto.
    assert STACK_MARKERS == ("compose.yaml", "engine/Dockerfile", "server/pyproject.toml", "client/pyproject.toml")


def test_env_stack_dir_wins_over_cwd(tmp_path) -> None:
    """sem `stack_dir` no perfil, `CQ_STACK_DIR` vence o diretório atual."""
    from_env = make_stack_dir(tmp_path, name="da-variavel")
    from_cwd = make_stack_dir(tmp_path, name="do-cwd")
    assert _same(locate_stack_dir(_profile(tmp_path), {"CQ_STACK_DIR": str(from_env)}, from_cwd), from_env)


def test_empty_env_stack_dir_is_absent(tmp_path) -> None:
    """`CQ_STACK_DIR` vazia vale como ausente (o diretório atual com os marcadores serve)."""
    from_cwd = make_stack_dir(tmp_path, name="do-cwd")
    assert _same(locate_stack_dir(_profile(tmp_path), {"CQ_STACK_DIR": ""}, from_cwd), from_cwd)


def test_cwd_or_nearest_ancestor_with_markers(tmp_path) -> None:
    """sem perfil nem variável, o diretório atual com os 4 marcadores, ou o ancestral **mais
    próximo** que os tenha (uma pilha dentro de outra: vale a de dentro)."""
    outer = make_stack_dir(tmp_path, name="externa")
    deep = outer / "server" / "src" / "pacote"
    deep.mkdir(parents=True)
    assert _same(locate_stack_dir(_profile(tmp_path), {}, outer), outer)
    assert _same(locate_stack_dir(_profile(tmp_path), {}, deep), outer)
    inner = make_stack_dir(outer, name="interna")
    inner_deep = inner / "docs" / "operacao"
    inner_deep.mkdir(parents=True)
    assert _same(locate_stack_dir(_profile(tmp_path), {}, inner_deep), inner)


@pytest.mark.parametrize("missing", STACK_MARKERS)
def test_three_of_four_markers_do_not_count(tmp_path, missing) -> None:
    """um diretório com só 3 dos 4 marcadores não serve; o ancestral com os 4 serve; sem nenhum,
    `StackDirNotFound`."""
    partial = make_stack_dir(tmp_path, name="parcial", markers=[m for m in STACK_MARKERS if m != missing])
    with pytest.raises(StackDirNotFound) as exc:
        locate_stack_dir(_profile(tmp_path), {}, partial)
    _assert_not_found(exc.value, partial)
    outer = make_stack_dir(tmp_path, name="externa")
    nested_partial = make_stack_dir(outer, name="parcial", markers=[m for m in STACK_MARKERS if m != missing])
    assert _same(locate_stack_dir(_profile(tmp_path), {}, nested_partial), outer)


def test_marker_must_be_where_expected(tmp_path) -> None:
    """os marcadores são relativos ao diretório candidato (`engine/Dockerfile`, e não `Dockerfile` na
    raiz)."""
    flat = make_stack_dir(tmp_path, name="plana", markers=["compose.yaml", "Dockerfile", "server/pyproject.toml",
                                                           "client/pyproject.toml"])
    with pytest.raises(StackDirNotFound):
        locate_stack_dir(_profile(tmp_path), {}, flat)


def test_not_found_without_profile_env_or_markers(tmp_path) -> None:
    """sem `stack_dir`, sem `CQ_STACK_DIR` e sem marcadores no diretório atual nem nos ancestrais →
    `StackDirNotFound` (2, a mensagem fixa, sem caminho)."""
    empty = tmp_path / "vazio" / "mais-fundo"
    empty.mkdir(parents=True)
    with pytest.raises(StackDirNotFound) as exc:
        locate_stack_dir(_profile(tmp_path), {}, empty)
    _assert_not_found(exc.value, empty)


def test_missing_profile_stack_dir_is_not_found(tmp_path) -> None:
    """o `stack_dir` do perfil que não existe → `StackDirNotFound`, sem cair para
    `CQ_STACK_DIR` nem para o diretório atual, e sem o caminho na mensagem."""
    missing = tmp_path / "pilha-sumida-Xk2"
    from_env = make_stack_dir(tmp_path, name="da-variavel")
    from_cwd = make_stack_dir(tmp_path, name="do-cwd")
    with pytest.raises(StackDirNotFound) as exc:
        locate_stack_dir(_profile(tmp_path, missing), {"CQ_STACK_DIR": str(from_env)}, from_cwd)
    _assert_not_found(exc.value, missing)


# --------------------------------------------------------------------------------------------------
# check_docker_target


@pytest.mark.parametrize(
    "env",
    [{}, {"DOCKER_HOST": ""}, {"DOCKER_HOST": "unix:///var/run/docker.sock"},
     {"DOCKER_HOST": "unix:///home/operador/.docker/run/docker.sock"},
     {"DOCKER_HOST": "npipe:////./pipe/docker_engine"},
     {"DOCKER_HOST": "npipe:////./pipe/dockerDesktopLinuxEngine"},
     {"DOCKER_CONTEXT": ""}, {"DOCKER_HOST": "", "DOCKER_CONTEXT": ""}],
    ids=["nada", "host_vazio", "unix", "unix_rootless", "npipe", "npipe_desktop", "contexto_vazio", "os_dois_vazios"])
def test_local_docker_target_accepted(env) -> None:
    """sem `DOCKER_HOST` (ou vazio), `unix://…` e `npipe://…` são o Docker deste computador;
    `DOCKER_CONTEXT` vazia vale como ausente."""
    assert check_docker_target(env) is None


@pytest.mark.parametrize(
    ("env", "needle"),
    [({"DOCKER_HOST": "tcp://10.0.0.5:2376"}, "10.0.0.5"),
     ({"DOCKER_HOST": "tcp://127.0.0.1:2375"}, "2375"),
     ({"DOCKER_HOST": "ssh://operador@vm.cq.test"}, "vm.cq.test"),
     ({"DOCKER_HOST": "https://docker.cq.test:2376"}, "docker.cq.test"),
     ({"DOCKER_HOST": "fd://"}, "fd://"),
     ({"DOCKER_CONTEXT": "contexto-remoto-Qw7"}, "contexto-remoto-Qw7"),
     ({"DOCKER_HOST": "unix:///var/run/docker.sock", "DOCKER_CONTEXT": "desktop-linux"}, "desktop-linux")],
    ids=["tcp", "tcp_loopback", "ssh", "outro_esquema", "fd", "contexto", "contexto_com_host_local"])
def test_non_local_docker_target_refused(env, needle) -> None:
    """`DOCKER_HOST` com outro esquema (`tcp://`, `ssh://`, …) ou `DOCKER_CONTEXT` não vazia →
    `StackTargetRefused` (2), com a mensagem fixa, sem eco do valor."""
    with pytest.raises(StackTargetRefused) as exc:
        check_docker_target(env)
    error = exc.value
    assert isinstance(error, StackError)
    assert error.exit_code == 2
    assert error.message == TARGET_REFUSED == str(error)
    for text in (str(error), repr(error)):
        assert needle not in text
