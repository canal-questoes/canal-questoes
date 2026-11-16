"""Testes da cópia `ops/backup/pull_backups.py`, que traz os backups da VM para um diretório local
pelo `sftp`.

O `pull` roda com o `LocalDirTransport` (um diretório local no lugar da VM) e o relógio injetado; o
`SftpTransport` roda com o `FakeSpawn` (`on_sftp`, o `sftp -b -` falso sobre um diretório local). O `main` não
recebe transporte nem `spawn`: os testes dele trocam o `SftpTransport` do módulo por uma fábrica que monta o
`LocalDirTransport` ou o `SftpTransport` real com o `FakeSpawn` (e o `subprocess.Popen` fica barrado). Os que
copiam dumps também param o relógio do `main` em `NOW` (`_freeze_clock`), porque ele lê a data real.

**[I]** Escolhas deste teste: o `main` monta `SftpTransport(host, user, identity,
remote_dir)` pelo nome do módulo, com `identity` como `Path`; a mensagem `ok` sai no stdout e as de falha, uma
linha no stderr; os nomes da cópia local (`.part`) ficam no próprio diretório local; a ordem de `PullResult.new`
não é conferida (só o conjunto).
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from tests.helpers.canary import find_secrets
from tests.helpers.ops import Result, clean_env, load_ops, run_main
from tests.helpers.process_fake import FakeSpawn, fake_age_ciphertext, sftp_argv

pull_mod = load_ops("ops/backup/pull_backups.py")
cq_backup = load_ops("ops/backup/cq_backup.py")

NOW = datetime(2026, 10, 29, 6, 0, 0, tzinfo=UTC)
HOST = "vm-piloto.example.org"
USER = "cqbackup"
REMOTE_DIR = "/var/backups/cq/dumps"

A = "cq-20261027T033000Z.dump.age"
B = "cq-20261028T033000Z.dump.age"
C = "cq-20261026T033000Z.dump.age"

EXPECTED_MESSAGES_FIXED = {
    "ok": (0, "Cópia atualizada: {n} arquivo(s) novo(s)."),
    "checksum_mismatch": (65, "Um arquivo copiado não confere com o resumo e foi descartado."),
    "unreachable": (69, "A VM não respondeu pela ligação SSH."),
    "invalid_arguments": (64, "Argumentos inválidos."),
}


def _content(name: str) -> bytes:
    return fake_age_ciphertext(f"conteudo de {name}".encode("ascii"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _origin(root: Path, names: list[str], *, sums_for: list[str] | None = None,
            override: dict[str, bytes] | None = None) -> Path:
    """O diretório "da VM": os dumps, o `SHA256SUMS` (dos nomes de `sums_for`, com o conteúdo original) e um
    `last-backup.json` (que não pode ser copiado)."""
    origin = root / "vm-dumps"
    origin.mkdir()
    for name in names:
        (origin / name).write_bytes((override or {}).get(name, _content(name)))
    listed = names if sums_for is None else sums_for
    lines = "".join(f"{_sha(_content(name))}  {name}\n" for name in sorted(listed))
    (origin / "SHA256SUMS").write_text(lines, encoding="utf-8")
    (origin / "last-backup.json").write_text('{"version": 1}', encoding="utf-8")
    return origin


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(directory.iterdir())}


def _dump_names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if re.fullmatch(cq_backup.DUMP_NAME_PATTERN, path.name))


class RecordingLocal:
    """Envolve o `LocalDirTransport` real e registra cada `fetch(nome, destino)`."""

    def __init__(self, path: Path) -> None:
        self.inner = pull_mod.LocalDirTransport(path)
        self.fetches: list[tuple[str, Path]] = []

    def list_names(self) -> list[str]:
        return self.inner.list_names()

    def fetch(self, name: str, dest: Path) -> None:
        self.fetches.append((name, Path(dest)))
        self.inner.fetch(name, dest)


@pytest.fixture
def no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("o teste da cópia não pode criar processo real")

    monkeypatch.setattr(subprocess, "Popen", refuse)


def _use_local(monkeypatch: pytest.MonkeyPatch, origin: Path) -> list[tuple[object, ...]]:
    """Troca o `SftpTransport` do módulo por um `LocalDirTransport` sobre `origin`; registra os argumentos."""
    built: list[tuple[object, ...]] = []

    def factory(host: str, user: str, identity: Path, remote_dir: str, **kwargs: object) -> object:
        built.append((host, user, identity, remote_dir))
        return pull_mod.LocalDirTransport(origin)

    monkeypatch.setattr(pull_mod, "SftpTransport", factory)
    return built


class _FrozenDatetime(datetime):
    """O `datetime` do módulo com o `now` parado em `NOW`: o `main` lê o relógio por `datetime.now(UTC)`, e os dumps
    de exemplo (`A` e `B`) não podem envelhecer com o calendário e sair da janela de dias que a cópia guarda."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
        assert tz is not None and tz.utcoffset(None) == timedelta(0), "o main lê o relógio em UTC"
        return NOW


def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Para o relógio do `main` em `NOW` (troca o `datetime` do módulo)."""
    monkeypatch.setattr(pull_mod, "datetime", _FrozenDatetime)


def _use_fake_sftp(monkeypatch: pytest.MonkeyPatch, spawn: FakeSpawn) -> None:
    """Troca o `SftpTransport` do módulo pelo real com o `FakeSpawn`."""
    real = pull_mod.SftpTransport

    def factory(host: str, user: str, identity: Path, remote_dir: str, **kwargs: object) -> object:
        return real(host, user, identity, remote_dir, spawn=spawn)

    monkeypatch.setattr(pull_mod, "SftpTransport", factory)


def _cli(identity: Path, local: Path, *extra: str) -> list[str]:
    return ["--host", HOST, "--identity", str(identity), "--remote-dir", REMOTE_DIR, "--local-dir", str(local),
            *extra]


def _assert_no_paths(result: Result, *paths: Path) -> None:
    combined = result.stdout + result.stderr
    for path in paths:
        assert str(path) not in combined and path.as_posix() not in combined


# --------------------------------------------------------------------------------------------------
# pull com o transporte local
# --------------------------------------------------------------------------------------------------


def test_messages_table_and_brings_only_new_dumps(tmp_path: Path) -> None:
    """a tabela fixa (`{n}` o único valor); na mesma função, um `pull`: origem com
    dois dumps novos e um já copiado → só os dois, cada um baixado para `.part` no diretório local, conferido
    e renomeado; o `last-backup.json` não vem; nada de `.part` sobra; nada muda na origem."""
    for key, value in EXPECTED_MESSAGES_FIXED.items():
        assert pull_mod.PULL_MESSAGES[key] == value
    assert pull_mod.PULL_MESSAGES["internal"][0] == 1
    assert set(pull_mod.PULL_MESSAGES) == {*EXPECTED_MESSAGES_FIXED, "internal"}
    assert re.findall(r"\{(\w*)\}", pull_mod.PULL_MESSAGES["ok"][1]) == ["n"]
    assert (pull_mod.DEFAULT_USER, pull_mod.DEFAULT_KEEP_DAYS) == ("cqbackup", 14)

    origin = _origin(tmp_path, [A, B, C])
    local = tmp_path / "copia"
    local.mkdir()
    (local / C).write_bytes(_content(C))
    before = _snapshot(origin)
    transport = RecordingLocal(origin)

    result = pull_mod.pull(transport, local, 14, NOW)

    assert sorted(result.new) == [A, B]
    assert result.discarded == [] and result.pruned == []
    assert _dump_names(local) == sorted([A, B, C])
    for name in (A, B, C):
        assert (local / name).read_bytes() == _content(name)
    assert not (local / "last-backup.json").exists()
    assert not [path.name for path in local.iterdir() if path.name.endswith(".part")]
    dump_fetches = [(name, dest) for name, dest in transport.fetches if name != "SHA256SUMS"]
    assert sorted(name for name, _ in dump_fetches) == [A, B]
    for name, dest in dump_fetches:
        assert dest.parent == local and dest.name.endswith(".part")
    assert "SHA256SUMS" in [name for name, _ in transport.fetches]
    assert "last-backup.json" not in [name for name, _ in transport.fetches]
    assert _snapshot(origin) == before


def test_mismatched_dump_is_discarded_and_valid_ones_kept(tmp_path: Path) -> None:
    """um dump cujo conteúdo não confere com o `SHA256SUMS` é descartado (o `.part` apagado), e
    os válidos ficam."""
    origin = _origin(tmp_path, [A, B], override={B: _content(B) + b"corrompido"})
    local = tmp_path / "copia"
    local.mkdir()

    result = pull_mod.pull(pull_mod.LocalDirTransport(origin), local, 14, NOW)

    assert result.new == [A]
    assert result.discarded == [B]
    assert _dump_names(local) == [A]
    assert not [path.name for path in local.iterdir() if path.name.endswith(".part")]


def test_dump_missing_from_sums_is_discarded(tmp_path: Path) -> None:
    """um dump fora do `SHA256SUMS` é descartado como divergente."""
    origin = _origin(tmp_path, [A, B], sums_for=[A])
    local = tmp_path / "copia"
    local.mkdir()

    result = pull_mod.pull(pull_mod.LocalDirTransport(origin), local, 14, NOW)

    assert result.new == [A]
    assert result.discarded == [B]
    assert _dump_names(local) == [A]


def test_local_prune_by_name_stamp(tmp_path: Path) -> None:
    """a cópia local é podada pelo carimbo do nome acima de `keep_days` (não pelo `mtime`); nomes fora
    do padrão ficam; nada muda na origem."""
    origin = _origin(tmp_path, [A])
    local = tmp_path / "copia"
    local.mkdir()
    old, recent = "cq-20261010T033000Z.dump.age", "cq-20261020T033000Z.dump.age"
    (local / old).write_bytes(_content(old))
    (local / recent).write_bytes(_content(recent))
    stamp_now, stamp_old = NOW.timestamp(), (NOW - timedelta(days=90)).timestamp()
    os.utime(local / old, (stamp_now, stamp_now))
    os.utime(local / recent, (stamp_old, stamp_old))
    outsiders = ["cq-20250101T000000Z.dump.age.bak", "notas.txt", "cq-antigo.dump.age"]
    for name in outsiders:
        (local / name).write_bytes(b"fica\n")
    before = _snapshot(origin)

    result = pull_mod.pull(pull_mod.LocalDirTransport(origin), local, 14, NOW)

    assert result.new == [A]
    assert result.pruned == [old]
    assert _dump_names(local) == sorted([A, recent])
    for name in outsiders:
        assert (local / name).read_bytes() == b"fica\n"
    assert _snapshot(origin) == before


# --------------------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------------------


def test_main_reports_new_files_with_only_n_interpolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         no_real_process: None) -> None:
    """pelo `main`, com o transporte trocado, código 0 e só `Cópia atualizada: 2 arquivo(s)
    novo(s).` no stdout; o `SftpTransport` montado com `--host`, `--user` padrão `cqbackup`, `--identity` e
    `--remote-dir`; nenhum caminho de chave nas saídas."""
    origin = _origin(tmp_path, [A, B])
    local = tmp_path / "copia"
    local.mkdir()
    identity = tmp_path / "chaves" / "id_cqbackup"
    built = _use_local(monkeypatch, origin)
    _freeze_clock(monkeypatch)

    result = run_main(pull_mod, _cli(identity, local), env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (0, "Cópia atualizada: 2 arquivo(s) novo(s).\n", "")
    assert built == [(HOST, USER, identity, REMOTE_DIR)]
    _assert_no_paths(result, identity, local, origin)


def test_main_mismatch_exits_65_with_fixed_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   no_real_process: None) -> None:
    """um dump divergente → código 65 e a mensagem fixa `checksum_mismatch` no stderr; o válido fica."""
    origin = _origin(tmp_path, [A, B], override={B: b"lixo"})
    local = tmp_path / "copia"
    local.mkdir()
    _use_local(monkeypatch, origin)
    _freeze_clock(monkeypatch)

    result = run_main(pull_mod, _cli(tmp_path / "id", local), env=clean_env())

    assert result.code == 65
    assert result.stderr == "Um arquivo copiado não confere com o resumo e foi descartado.\n"
    assert _dump_names(local) == [A]
    _assert_no_paths(result, tmp_path)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", ""])
def test_main_rejects_invalid_keep_days(value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                        no_real_process: None) -> None:
    """`--keep-days` inteiro ≥ 1 → senão `invalid_arguments` (64), sem eco e sem transporte."""
    built = _use_local(monkeypatch, tmp_path)
    local = tmp_path / "copia"

    result = run_main(pull_mod, _cli(tmp_path / "id", local, "--keep-days", value), env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (64, "", "Argumentos inválidos.\n")
    assert built == []
    assert not local.exists() or list(local.iterdir()) == []


def test_main_unreachable_vm_exits_69(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                      no_real_process: None) -> None:
    """o `sftp` com código ≠ 0 → `unreachable` (69), a linha fixa, sem a saída do `sftp` e sem o
    caminho da chave."""
    origin = _origin(tmp_path, [A])
    spawn = FakeSpawn().on_sftp(origin, remote_dir=REMOTE_DIR, returncode=255,
                                stderr=b"ssh: connect to host vm-piloto port 22: Connection refused\n")
    _use_fake_sftp(monkeypatch, spawn)
    identity = tmp_path / "chaves" / "id_cqbackup"
    local = tmp_path / "copia"
    local.mkdir()

    result = run_main(pull_mod, _cli(identity, local, "--user", "operador"), env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (69, "", "A VM não respondeu pela ligação SSH.\n")
    assert spawn.argvs and all(argv == sftp_argv(identity, "operador", HOST) for argv in spawn.argvs)
    assert _dump_names(local) == []
    _assert_no_paths(result, identity, tmp_path)
    assert find_secrets(result.stderr, ["Connection refused", str(identity)]) == []


# --------------------------------------------------------------------------------------------------
# SftpTransport
# --------------------------------------------------------------------------------------------------


def test_sftp_list_names_parses_ls_output(tmp_path: Path) -> None:
    """`list_names` roda o `sftp -b - -o BatchMode=yes -o IdentitiesOnly=yes -i <chave>
    <user>@<host>` exato com o lote **só** `ls -1 <dir>`; a saída (eco dos comandos, caminho prefixado, uma
    linha fora do padrão) vira os nomes-base que casam `DUMP_NAME_PATTERN`."""
    origin = _origin(tmp_path, [A, B])
    (origin / "cq-20261028T033000Z.dump.age.part").write_bytes(b"meio")
    spawn = FakeSpawn().on_sftp(origin, remote_dir=REMOTE_DIR, extra_ls_lines=["Can't ls: aviso qualquer"])
    identity = tmp_path / "id_cqbackup"
    transport = pull_mod.SftpTransport(HOST, USER, identity, REMOTE_DIR, spawn=spawn)

    names = transport.list_names()

    assert sorted(names) == [A, B]
    assert spawn.argvs == [sftp_argv(identity, USER, HOST)]
    batch = spawn.calls[0].batch
    assert [shlex.split(line) for line in batch] == [["ls", "-1", REMOTE_DIR]]
    assert spawn.calls[0].env is not None
    assert str(identity) not in repr(transport)


def test_sftp_fetch_uses_only_get(tmp_path: Path) -> None:
    """`fetch` roda o mesmo argv com o lote **só** `get <dir>/<nome> <local>` (na regra `shlex`
    POSIX do lote), e o arquivo chega igual."""
    origin = _origin(tmp_path, [A])
    spawn = FakeSpawn().on_sftp(origin, remote_dir=REMOTE_DIR)
    identity = tmp_path / "id_cqbackup"
    transport = pull_mod.SftpTransport(HOST, USER, identity, REMOTE_DIR, spawn=spawn)
    dest = tmp_path / "copia" / (A + ".part")
    dest.parent.mkdir()

    transport.fetch(A, dest)

    assert dest.read_bytes() == _content(A)
    assert spawn.argvs == [sftp_argv(identity, USER, HOST)]
    assert [shlex.split(line) for line in spawn.calls[0].batch] == [["get", f"{REMOTE_DIR}/{A}", str(dest)]]


def test_main_over_sftp_emits_only_ls_and_get(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              no_real_process: None) -> None:
    """uma cópia inteira pelo `SftpTransport` (com o `FakeSpawn`) só emite `ls` e `get`
    (nenhum `rm`, `put`, `rename`, `chmod`), todo argv é o exato, o `last-backup.json` não é pedido e a origem
    não muda."""
    origin = _origin(tmp_path, [A, B])
    spawn = FakeSpawn().on_sftp(origin, remote_dir=REMOTE_DIR)
    _use_fake_sftp(monkeypatch, spawn)
    _freeze_clock(monkeypatch)
    identity = tmp_path / "chaves" / "id_cqbackup"
    local = tmp_path / "copia"
    local.mkdir()
    before = _snapshot(origin)

    result = run_main(pull_mod, _cli(identity, local), env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (0, "Cópia atualizada: 2 arquivo(s) novo(s).\n", "")
    assert _dump_names(local) == [A, B]
    assert spawn.argvs and all(argv == sftp_argv(identity, USER, HOST) for argv in spawn.argvs)
    commands = [shlex.split(line) for call in spawn.calls for line in call.batch]
    assert commands and {words[0] for words in commands} <= {"ls", "get"}
    assert ["ls", "-1", REMOTE_DIR] in commands
    fetched = sorted(words[1] for words in commands if words[0] == "get")
    assert fetched == sorted([f"{REMOTE_DIR}/{A}", f"{REMOTE_DIR}/{B}", f"{REMOTE_DIR}/SHA256SUMS"])
    assert _snapshot(origin) == before
    _assert_no_paths(result, identity, local)
