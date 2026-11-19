"""Verificação do arquivo de perfil no `doctor`.


`cq_client.doctor.permission_result(st_mode, platform)` e `check_profile_file(path, *, platform, mode)`
(o `doctor` no cliente):
- `profile.permissions` pelo modo dado, com modos **sintéticos** (o modo é injetado), em qualquer sistema, sem
  `chmod` e sem `skip`: 0o600 → ok; 0o640 e 0o604 (legível pelo grupo ou por outros) → aviso; `platform="nt"` →
  "não verificado" (o modo não reflete as ACLs). **[F]** No Windows, `os.chmod` só ajusta o somente-leitura e
  `os.stat` devolve 0o666 ou 0o444 (documentação do Python, `os.chmod`): por isso o modo nunca vem de um `chmod`;
- `profile.git` é aviso quando algum ancestral do caminho resolvido tem `.git` (diretório ou arquivo), sem rodar
  `git`; a cópia de trabalho é simulada **dentro do `tmp_path` do caso** (`tmp_path/repo/.git`, com o perfil em
  `tmp_path/repo/sub/config.toml`); **[I]** o pai do `tmp_path` é o diretório-base da sessão do pytest,
  compartilhado por todos os testes, e um `.git` ali daria `profile.git = warning` nos
  casos seguintes;
- sem arquivo de perfil, as duas "não se aplica";
- nunca `chmod`, nunca o caminho na mensagem, nenhuma escrita fora do `tmp_path` do caso.

As mensagens esperadas são as fixas do cliente (`CLIENT_MESSAGES` e `NOT_APPLICABLE_NO_PROFILE_FILE`, reais no
esqueleto); as asserções sobre elas ficam nas mesmas funções dos casos vermelhos. Os resultados sempre na
ordem `profile.permissions`, `profile.git`, com `part = "profile"` e sem `offering`, `count` nem `detail`.
"""

from __future__ import annotations

import os
import pathlib
import stat
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from cq_client import doctor
from cq_client.doctor import (
    CHECK_IDS,
    CHECK_PARTS,
    CLIENT_MESSAGES,
    NOT_APPLICABLE_NO_PROFILE_FILE,
    PROFILE_CHECK_IDS,
    Check,
)

REGULAR = stat.S_IFREG

# Os modos sintéticos e o resultado esperado em sistemas POSIX.
POSIX_MODES = (
    (0o600, "ok"),
    (0o400, "ok"),
    (0o640, "warning"),
    (0o604, "warning"),
    (0o644, "warning"),
    (0o660, "warning"),
    (0o606, "warning"),
    (0o666, "warning"),
)


def _permissions(result: str) -> Check:
    return Check("profile.permissions", "profile", result, CLIENT_MESSAGES[("profile.permissions", result)])


def _git(result: str) -> Check:
    return Check("profile.git", "profile", result, CLIENT_MESSAGES[("profile.git", result)])


def _not_applicable() -> list[Check]:
    return [Check(check_id, "profile", "not_applicable", NOT_APPLICABLE_NO_PROFILE_FILE)
            for check_id in PROFILE_CHECK_IDS]


def _mode(value: int) -> Callable[[pathlib.Path], int]:
    """O modo injetado: o arquivo regular com as permissões dadas, sem tocar o disco."""
    def read(path: pathlib.Path) -> int:
        assert isinstance(path, pathlib.Path)
        return REGULAR | value
    return read


def _no_git_ancestor(path: pathlib.Path) -> bool:
    return not any((parent / ".git").exists() for parent in path.resolve().parents)


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Any:
    """Nenhum `chmod` e nenhum processo durante o caso; no fim, nada novo no diretório-base da sessão (o pai do
    `tmp_path`), além do próprio `tmp_path` do caso."""
    calls: list[str] = []

    def forbid(name: str) -> Callable[..., Any]:
        def blocked(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            raise AssertionError(f"o doctor não pode chamar {name}")
        return blocked

    monkeypatch.setattr(os, "chmod", forbid("os.chmod"))
    monkeypatch.setattr(pathlib.Path, "chmod", forbid("Path.chmod"))
    monkeypatch.setattr(pathlib.Path, "lchmod", forbid("Path.lchmod"), raising=False)
    monkeypatch.setattr(subprocess, "run", forbid("subprocess.run"))
    monkeypatch.setattr(subprocess, "Popen", forbid("subprocess.Popen"))
    monkeypatch.setattr(subprocess, "check_output", forbid("subprocess.check_output"))
    monkeypatch.setattr(os, "system", forbid("os.system"))
    base = tmp_path.parent
    before = sorted(p.name for p in base.iterdir())
    yield calls
    after = sorted(p.name for p in base.iterdir())
    assert after == before, f"escrita fora do tmp_path do caso: {sorted(set(after) - set(before))}"
    assert calls == []


@pytest.mark.parametrize(("value", "expected"), POSIX_MODES)
def test_permission_result_posix_synthetic_modes(value: int, expected: str) -> None:
    """0o600 (e só leitura do dono) → ok; qualquer bit de grupo ou de outros → aviso; com qualquer sistema rodando
    os testes (o modo é dado; nenhum `chmod`, nenhum `skip`). Os ids, a parte e as mensagens fixas reais ficam na
    mesma função."""
    assert PROFILE_CHECK_IDS == ("profile.permissions", "profile.git")
    assert CHECK_IDS[-2:] == PROFILE_CHECK_IDS
    assert {CHECK_PARTS[i] for i in PROFILE_CHECK_IDS} == {"profile"}
    assert permission_result_of(value, "posix") == _permissions(expected)


def permission_result_of(value: int, platform: str) -> Check:
    return doctor.permission_result(REGULAR | value, platform)


@pytest.mark.parametrize("value", [0o600, 0o640, 0o604, 0o666, 0o444])
def test_permission_result_on_windows_is_not_verified(value: int) -> None:
    """No Windows (`platform="nt"`), o modo não reflete as ACLs: "não verificado", qualquer que seja o modo."""
    assert CLIENT_MESSAGES[("profile.permissions", "not_verified")].startswith("No Windows")
    assert permission_result_of(value, "nt") == _permissions("not_verified")


def test_check_profile_file_outside_git_ok(tmp_path: pathlib.Path, guarded: Any) -> None:
    """Arquivo fora de cópia git, modo 0o600 → as duas "ok"; nenhum caminho na mensagem."""
    folder = tmp_path / "perfil-sem-git"
    folder.mkdir()
    profile = folder / "config.toml"
    profile.write_bytes(b'api_url = "http://127.0.0.1:9"\n')
    assert _no_git_ancestor(profile), "ambiente de teste com .git num ancestral do tmp_path"
    checks = doctor.check_profile_file(profile, platform="posix", mode=_mode(0o600))
    assert checks == [_permissions("ok"), _git("ok")]
    _assert_no_path(checks, profile)


@pytest.mark.parametrize("git_kind", ["directory", "file"])
def test_check_profile_file_inside_git_copy_is_warning(tmp_path: pathlib.Path, guarded: Any,
                                                         git_kind: str) -> None:
    """`.git` (diretório, ou arquivo como numa worktree ou submódulo) num ancestral do caminho do perfil →
    `profile.git` aviso, sem rodar `git`; o `.git` fica **dentro** do `tmp_path` do caso."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    if git_kind == "directory":
        (repo / ".git").mkdir()
    else:
        (repo / ".git").write_bytes(b"gitdir: ../outro/.git/worktrees/repo\n")
    profile = repo / "sub" / "config.toml"
    profile.write_bytes(b'api_url = "http://127.0.0.1:9"\n')
    assert not (tmp_path / ".git").exists() and _no_git_ancestor(tmp_path)
    checks = doctor.check_profile_file(profile, platform="posix", mode=_mode(0o600))
    assert checks == [_permissions("ok"), _git("warning")]
    _assert_no_path(checks, profile)
    # O mesmo arquivo com modo legível por outros: as duas verificações mudam, cada uma pela sua condição.
    assert doctor.check_profile_file(profile, platform="posix", mode=_mode(0o644)) == [
        _permissions("warning"), _git("warning")]


def test_check_profile_file_git_in_direct_parent(tmp_path: pathlib.Path, guarded: Any) -> None:
    """O `.git` no próprio diretório do perfil também conta (o ancestral mais próximo)."""
    repo = tmp_path / "repo-direto"
    (repo / ".git").mkdir(parents=True)
    profile = repo / "config.toml"
    profile.write_bytes(b"")
    assert doctor.check_profile_file(profile, platform="posix", mode=_mode(0o600)) == [
        _permissions("ok"), _git("warning")]


def test_check_profile_file_windows_permissions_not_verified(tmp_path: pathlib.Path, guarded: Any) -> None:
    """No Windows, `profile.permissions` "não verificado" e `profile.git` verificado normalmente."""
    profile = tmp_path / "config.toml"
    profile.write_bytes(b"")
    assert _no_git_ancestor(profile)
    assert doctor.check_profile_file(profile, platform="nt", mode=_mode(0o666)) == [
        _permissions("not_verified"), _git("ok")]


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_check_profile_file_missing_is_not_applicable(tmp_path: pathlib.Path, guarded: Any, platform: str) -> None:
    """Sem arquivo de perfil, as duas "não se aplica", sem ler o modo; nada é criado."""
    profile = tmp_path / "nao-existe" / "config.toml"
    read: list[pathlib.Path] = []

    def mode(path: pathlib.Path) -> int:
        read.append(path)
        raise AssertionError("o modo de um arquivo inexistente não pode ser lido")

    assert NOT_APPLICABLE_NO_PROFILE_FILE == "Não se aplica: não há arquivo de perfil."
    assert doctor.check_profile_file(profile, platform=platform, mode=mode) == _not_applicable()
    assert read == []
    assert not profile.parent.exists(), "o doctor criou o diretório do perfil"


def test_check_profile_file_default_mode_reads_stat_without_chmod(tmp_path: pathlib.Path, guarded: Any) -> None:
    """Com o `mode` padrão (o `stat` real), o resultado de `profile.permissions` segue `permission_result` sobre o
    modo lido, em qualquer sistema; o arquivo não muda de modo."""
    profile = tmp_path / "config.toml"
    profile.write_bytes(b"")
    real_mode = profile.stat().st_mode
    checks = doctor.check_profile_file(profile, platform=os.name)
    assert checks[0] == doctor.permission_result(real_mode, os.name)
    assert checks[1] == _git("ok")
    assert profile.stat().st_mode == real_mode


def _assert_no_path(checks: list[Check], profile: pathlib.Path) -> None:
    """Nem o caminho, nem o diretório, nem o nome do arquivo aparecem nas mensagens."""
    for check in checks:
        texts = [check.message, check.detail or ""]
        for text in texts:
            assert str(profile) not in text and str(profile.parent) not in text
            assert profile.name not in text and profile.parent.name not in text, text
