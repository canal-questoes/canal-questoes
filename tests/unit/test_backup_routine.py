"""Testes da rotina de backup `ops/backup/cq_backup.py`, usada na implantação do piloto.


Tudo com o `FakeSpawn` de `tests/helpers/process_fake.py` (nenhum processo real; o `subprocess.Popen` fica
barrado nos testes do `main`) e com o relógio injetado (`now=`). O `age` falso grava em etapas
(cabeçalho, corpo) e chama um gancho entre elas: o teste examina o diretório dos dumps e o de estado nesse
instante, para provar que nenhum arquivo com `PGDMP` (o dump em claro) existe em nenhum momento.

**[I]** Escolhas deste teste:
- o temporário é `.tmp-<carimbo>-<pid>.age` no diretório dos dumps, com o `pid` do processo (os testes
  conferem o `pid` e o padrão, não o formato do carimbo);
- a falha depois de `renamed` e antes de `state_written` é provocada pelo `os.replace` do registro (a
  rotina grava o registro por temporário + `os.replace`); o código esperado é só "≠ 0", com uma
  linha de `BACKUP_MESSAGES` no stderr;
- o `main` monta um `BackupConfig` e chama `run` do módulo (o teste troca `run` por um registrador);
- o registro `finished_at` é o instante do `now()` injetado (o relógio do teste não anda).
"""

from __future__ import annotations

import builtins
import dataclasses
import io
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.helpers.canary import find_secrets
from tests.helpers.ops import clean_env, load_ops, run_main
from tests.helpers.process_fake import (AGE_HEADER, DEFAULT_DUMP, PIPE, FakeSpawn, age_encrypt_argv,
                                        docker_ps_argv, fake_age_ciphertext, pg_dump_argv, sha256_hex)

cq_backup = load_ops("ops/backup/cq_backup.py")

PROJECT = "cq-teste-bkp"
CONTAINER = "3f2a1b4c5d6e"
OTHER_CONTAINER = "9e8d7c6b5a4f"
# O destinatário de exemplo do README do age (público).
RECIPIENT = "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
OTHER_RECIPIENT = "age1lggyhqrw2nlhcxprm67z43rta597azn8gknawjehu9d9dl0jq3yqqvfafg"
# Uma identidade com a forma da do age (falsa): nunca pode aparecer em nada que a rotina escreve.
FAKE_SECRET_KEY = "AGE-SECRET-KEY-1" + "QQPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L" * 2

NOW = datetime(2026, 10, 29, 3, 30, 5, tzinfo=UTC)
NOW_NAME = "cq-20261029T033005Z.dump.age"
NOW_TEXT = "2026-10-29T03:30:05Z"

PG_DUMP_STDERR = b"pg_dump: error: saida-do-processo-pg-dump-XYZ\n"
AGE_STDERR = b"age: error: saida-do-processo-age-XYZ\n"

# o conjunto fechado das saídas.
EXPECTED_MESSAGES = {
    "ok": (0, "Backup concluído."),
    "busy": (75, "Outro backup está em andamento."),
    "recipients_invalid": (78, "O arquivo de destinatários do age está ausente ou inválido."),
    "container_not_found": (69, "O contêiner do Postgres do projeto não foi encontrado em execução."),
    "dump_failed": (70, "O dump do banco falhou."),
    "encrypt_failed": (70, "A cifra do dump falhou."),
    "write_failed": (73, "Não foi possível gravar o backup no disco."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "internal": (1, "Erro interno do backup."),
}

RECORD_KEYS = {"version", "finished_at", "file", "size_bytes", "sha256"}
TMP_PATTERN = re.compile(r"\.tmp-(?P<stamp>.+)-(?P<pid>\d+)\.age")


# --------------------------------------------------------------------------------------------------
# Montagem
# --------------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Setup:
    root: Path
    dumps: Path
    state: Path
    recipients: Path
    spawn: FakeSpawn
    out: io.StringIO
    err: io.StringIO
    stages: list[tuple[str, list[str], list[str]]]

    def config(self, **overrides: object) -> object:
        fields: dict[str, object] = {"project": PROJECT, "recipients": self.recipients, "dumps_dir": self.dumps,
                                     "state_dir": self.state}
        fields.update(overrides)
        return cq_backup.BackupConfig(**fields)

    def run(self, *, now: datetime = NOW, **overrides: object) -> int:
        return cq_backup.run(self.config(**overrides), spawn=self.spawn, now=lambda: now, stdout=self.out,
                             stderr=self.err)

    def reset_streams(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()


def _plain_files(*dirs: Path) -> list[str]:
    """Os arquivos (de qualquer nome) com `PGDMP` nos diretórios dados. A trava fica de fora: no Windows, o
    `msvcrt.locking` impede a leitura do arquivo enquanto a rotina a segura (e ela é vazia por contrato)."""
    hits = []
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.name == cq_backup.LOCK_FILE:
                continue
            if path.is_file() and b"PGDMP" in path.read_bytes():
                hits.append(path.name)
    return hits


def _names(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _snapshot(*dirs: Path) -> dict[str, bytes]:
    """Nome → bytes de cada arquivo (a trava fica de fora, como em `_plain_files`)."""
    snap = {}
    for directory in dirs:
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name != cq_backup.LOCK_FILE:
                snap[str(path)] = path.read_bytes()
    return snap


def _make_setup(tmp_path: Path, *, recipients: str | None = f"# chave do teste\n\n{RECIPIENT}\n",
                containers: tuple[str, ...] = (CONTAINER,), pg_dump_code: int = 0, age_code: int = 0,
                age_omit_header: bool = False) -> Setup:
    root = tmp_path / "vm"
    dumps = root / "dumps"
    state = root / "state"
    dumps.mkdir(parents=True)
    state.mkdir(parents=True)
    recipients_path = root / "recipients.txt"
    if recipients is not None:
        recipients_path.write_text(recipients, encoding="utf-8")
    stages: list[tuple[str, list[str], list[str]]] = []

    def hook(stage: str) -> None:
        stages.append((stage, _plain_files(dumps, state), _names(dumps)))

    spawn = FakeSpawn()
    spawn.on_docker_ps(list(containers), project=PROJECT)
    spawn.on_pg_dump(returncode=pg_dump_code, stderr=PG_DUMP_STDERR if pg_dump_code else b"", hook=hook)
    spawn.on_age(returncode=age_code, stderr=AGE_STDERR if age_code else b"", hook=hook,
                 omit_header=age_omit_header)
    return Setup(root=root, dumps=dumps, state=state, recipients=recipients_path, spawn=spawn, out=io.StringIO(),
                 err=io.StringIO(), stages=stages)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return _make_setup(tmp_path)


def _write_dump(dumps: Path, name: str, data: bytes | None = None, *, mtime: datetime | None = None) -> bytes:
    content = data if data is not None else fake_age_ciphertext(f"dump antigo {name}".encode("ascii"))
    path = dumps / name
    path.write_bytes(content)
    if mtime is not None:
        stamp = mtime.timestamp()
        os.utime(path, (stamp, stamp))
    return content


def _assert_failure(setup: Setup, code: int, key: str) -> None:
    """Uma falha: o código e a linha fixa de `key`, nada no stdout, sem caminho, projeto nem saída de processo."""
    assert code == EXPECTED_MESSAGES[key][0]
    assert setup.out.getvalue() == ""
    assert setup.err.getvalue() == EXPECTED_MESSAGES[key][1] + "\n"
    leaked = find_secrets(setup.err.getvalue(), [str(setup.root), setup.root.as_posix(), PROJECT, CONTAINER,
                                                  PG_DUMP_STDERR.decode(), AGE_STDERR.decode(), RECIPIENT])
    assert leaked == []


PREVIOUS_NAME = "cq-20261028T033000Z.dump.age"
PREVIOUS_TEXT = "2026-10-28T03:30:00Z"


@dataclasses.dataclass(frozen=True)
class Previous:
    """O estado de um backup bom anterior: o dump, o `SHA256SUMS` e o `last-backup.json` (bytes exatos)."""
    dumps: dict[str, bytes]
    sums: bytes
    record: bytes


def _seed_previous(setup: Setup) -> Previous:
    """Grava o resultado de um sucesso anterior: o dump `PREVIOUS_NAME`, o `SHA256SUMS` com uma
    linha por dump com nome do padrão já presente (ordem de nome) e um `last-backup.json` válido
    apontando para ele. Chamado depois de gravar os demais dumps antigos do caso."""
    dump = _write_dump(setup.dumps, PREVIOUS_NAME)
    dumps = {path.name: path.read_bytes() for path in sorted(setup.dumps.iterdir())
             if re.fullmatch(r"cq-\d{8}T\d{6}Z\.dump\.age", path.name)}
    sums = "".join(f"{sha256_hex(data)}  {name}\n" for name, data in dumps.items()).encode("utf-8")
    (setup.dumps / "SHA256SUMS").write_bytes(sums)
    record = json.dumps({"version": 1, "finished_at": PREVIOUS_TEXT, "file": PREVIOUS_NAME,
                         "size_bytes": len(dump), "sha256": sha256_hex(dump)}).encode("utf-8")
    (setup.state / "last-backup.json").write_bytes(record)
    return Previous(dumps=dumps, sums=sums, record=record)


def _previous_intact(setup: Setup, previous: Previous) -> None:
    """Regra de não tocar no registro: depois de uma falha, o `last-backup.json` e o `SHA256SUMS` do backup
    anterior ficam byte a byte iguais, os dumps antigos também, e nada mais surge no estado nem nos dumps (fora a
    trava)."""
    assert set(_names(setup.state)) <= {"last-backup.json", cq_backup.LOCK_FILE}
    record_path = setup.state / "last-backup.json"
    assert record_path.is_file(), "a falha apagou o registro do backup anterior"
    assert record_path.read_bytes() == previous.record, "a falha alterou o registro do backup anterior"
    sums_path = setup.dumps / "SHA256SUMS"
    assert sums_path.is_file(), "a falha apagou o SHA256SUMS anterior"
    assert sums_path.read_bytes() == previous.sums, "a falha alterou o SHA256SUMS anterior"
    assert _names(setup.dumps) == sorted([*previous.dumps, "SHA256SUMS"])
    for name, data in previous.dumps.items():
        assert (setup.dumps / name).read_bytes() == data, f"a falha alterou o dump antigo {name}"


def _tmp_argv_ok(argv: list[str], setup: Setup) -> Path:
    """O argv do `age` com o temporário certo; devolve o caminho do temporário."""
    assert argv[:4] == ["age", "-R", str(setup.recipients), "-o"] and len(argv) == 5
    output = Path(argv[4])
    assert output.parent == setup.dumps
    match = TMP_PATTERN.fullmatch(output.name)
    assert match is not None, "o temporário deve ser .tmp-<carimbo>-<pid>.age"
    assert int(match.group("pid")) == os.getpid()
    assert argv == age_encrypt_argv(setup.recipients, output)
    return output


@pytest.fixture
def replace_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    """Registra cada `os.replace` (delegando ao original)."""
    calls: list[tuple[Path, Path]] = []
    original = os.replace

    def recording(src: object, dst: object, *args: object, **kwargs: object) -> None:
        calls.append((Path(os.fspath(src)), Path(os.fspath(dst))))
        original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording)
    return calls


@pytest.fixture
def no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nos testes do `main`, nenhum processo real pode nascer."""

    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("o teste do main não pode criar processo real")

    monkeypatch.setattr(subprocess, "Popen", refuse)


# --------------------------------------------------------------------------------------------------
# Mensagens, nomes e destinatários
# --------------------------------------------------------------------------------------------------


def test_messages_table_and_successful_run(setup: Setup) -> None:
    """o conjunto fechado das saídas (chave, código e texto), na mesma função de uma
    execução bem-sucedida: código 0 e só `Backup concluído.` no stdout."""
    assert cq_backup.BACKUP_MESSAGES == EXPECTED_MESSAGES
    assert cq_backup.DUMP_NAME_PATTERN == r"^cq-\d{8}T\d{6}Z\.dump\.age$"
    assert cq_backup.AGE_HEADER == b"age-encryption.org/v1"
    assert (cq_backup.RECORD_FILE, cq_backup.SUMS_FILE, cq_backup.LOCK_FILE) == (
        "last-backup.json", "SHA256SUMS", "backup.lock")

    code = setup.run()

    assert code == 0
    assert setup.out.getvalue() == "Backup concluído.\n"
    assert setup.err.getvalue() == ""


@pytest.mark.parametrize(("at", "expected"), [
    pytest.param(NOW, NOW_NAME, id="utc"),
    pytest.param(datetime(2026, 10, 29, 0, 30, 5, tzinfo=timezone(timedelta(hours=-3))), NOW_NAME,
                 id="fuso-de-campinas-convertido"),
    pytest.param(datetime(2027, 1, 2, 23, 59, 59, tzinfo=UTC), "cq-20270102T235959Z.dump.age", id="virada"),
])
def test_dump_name_is_utc_stamp(at: datetime, expected: str) -> None:
    """`cq-<AAAAMMDDTHHMMSSZ>.dump.age` em UTC; o nome casa `DUMP_NAME_PATTERN` e volta
    ao mesmo instante por `parse_dump_name`."""
    name = cq_backup.dump_name(at)
    assert name == expected
    assert re.fullmatch(cq_backup.DUMP_NAME_PATTERN, name)
    parsed = cq_backup.parse_dump_name(name)
    assert parsed == at.astimezone(UTC).replace(microsecond=0)
    assert parsed is not None and parsed.utcoffset() == timedelta(0)


@pytest.mark.parametrize("name", [
    "cq-20261029T033005Z.dump.age.bak",
    "cq-20261029T033005.dump.age",
    "cq-20261029-033005Z.dump.age",
    "cq-20261329T033005Z.dump.age",
    "outro-20261029T033005Z.dump.age",
    ".tmp-20261029T033005Z-123.age",
    "SHA256SUMS",
    "last-backup.json",
    "notas.txt",
])
def test_parse_dump_name_rejects_other_names(name: str) -> None:
    """só o padrão exato (e uma data válida) tem carimbo; o resto é `None`."""
    assert cq_backup.parse_dump_name(name) is None


def test_parse_recipients_accepts_comments_and_blank_lines() -> None:
    """só `age1…`, `#…` e linhas vazias; os destinatários na ordem."""
    text = f"# destinatarios do backup\n\n{RECIPIENT}\n# outro\n\n{OTHER_RECIPIENT}\n"
    assert cq_backup.parse_recipients(text) == (RECIPIENT, OTHER_RECIPIENT)


@pytest.mark.parametrize("text", [
    pytest.param("", id="vazio"),
    pytest.param("# so comentario\n\n# outro\n", id="so-comentarios"),
    pytest.param(f"{RECIPIENT}\n{FAKE_SECRET_KEY}\n", id="chave-privada"),
    pytest.param(f"{RECIPIENT}\nisto nao e destinatario\n", id="lixo"),
    pytest.param("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n", id="ssh"),
])
def test_parse_recipients_rejects_invalid_text_without_echo(text: str) -> None:
    """nenhum destinatário ou linha fora das três formas → `RecipientsInvalid`, sem o
    conteúdo do arquivo na mensagem."""
    with pytest.raises(cq_backup.RecipientsInvalid) as caught:
        cq_backup.parse_recipients(text)
    shown = str(caught.value) + repr(caught.value)
    assert FAKE_SECRET_KEY not in shown
    assert "isto nao e destinatario" not in shown
    assert "AAAAC3NzaC1lZDI1NTE5" not in shown


@pytest.mark.parametrize("content", [
    pytest.param(None, id="ausente"),
    pytest.param("", id="vazio"),
    pytest.param("# so comentario\n\n", id="so-comentarios"),
    pytest.param(f"{RECIPIENT}\n{FAKE_SECRET_KEY}\n", id="chave-privada"),
    pytest.param(f"{RECIPIENT}\nlixo qualquer\n", id="lixo"),
])
def test_invalid_recipients_file_fails_before_any_process(content: str | None, tmp_path: Path) -> None:
    """destinatários ausente, vazio, só comentários, com chave privada ou com lixo →
    `recipients_invalid` (78) sem processo nenhum, sem dump novo e sem tocar no registro nem no `SHA256SUMS` do
    backup anterior; a chave privada não sai no stderr."""
    setup = _make_setup(tmp_path, recipients=content)
    previous = _seed_previous(setup)

    code = setup.run()

    _assert_failure(setup, code, "recipients_invalid")
    assert setup.spawn.calls == []
    _previous_intact(setup, previous)
    assert FAKE_SECRET_KEY not in setup.err.getvalue()


# --------------------------------------------------------------------------------------------------
# Sucesso: argv, pipe, nada em claro, arquivos
# --------------------------------------------------------------------------------------------------


def test_success_runs_exact_argv_chained_by_name(setup: Setup) -> None:
    """os três argv exatos, nesta ordem, chamados por nome (`env` explícito); o `pg_dump` com
    `stdout=PIPE` e o `age` lendo o stdout dele (o pipe, sem arquivo intermediário)."""
    code = setup.run()

    assert code == 0
    argvs = setup.spawn.argvs
    assert len(argvs) == 3
    assert argvs[0] == docker_ps_argv(PROJECT)
    assert argvs[1] == pg_dump_argv(CONTAINER)
    _tmp_argv_ok(argvs[2], setup)
    calls = setup.spawn.calls
    assert all(call.env is not None for call in calls)
    assert calls[1].stdout == PIPE
    assert calls[2].stdin is calls[1].process.stdout
    assert calls[2].stdin_data == DEFAULT_DUMP


def test_success_never_leaves_plain_dump_on_disk(setup: Setup) -> None:
    """em nenhum momento (o gancho do `age` falso entre o cabeçalho e o corpo) há arquivo com
    `PGDMP` no diretório dos dumps nem no de estado; o nome final só aparece depois das duas etapas."""
    code = setup.run()

    assert code == 0
    stages = [stage for stage, _, _ in setup.stages]
    assert stages.count("header") == 1 and stages.count("body") == 1
    for stage, plain, names in setup.stages:
        assert plain == [], f"dump em claro em disco na etapa {stage}"
        assert NOW_NAME not in names, f"nome final antes da hora (etapa {stage})"
    assert _plain_files(setup.root) == []


def test_success_writes_final_dump_sums_and_record(setup: Setup, replace_calls: list[tuple[Path, Path]]) -> None:
    """o dump final com o cabeçalho do age, sem temporário; o
    `SHA256SUMS` no formato do `sha256sum`; o `last-backup.json` com exatamente as chaves e os valores do
    arquivo, igual ao `read_record`; registro e resumo gravados por temporário + `os.replace` no mesmo
    diretório, depois do `os.replace` do dump."""
    code = setup.run()

    assert code == 0
    assert _names(setup.dumps) == sorted([NOW_NAME, "SHA256SUMS"])
    final = (setup.dumps / NOW_NAME).read_bytes()
    assert final.startswith(AGE_HEADER)
    assert final == fake_age_ciphertext(DEFAULT_DUMP)
    assert (setup.dumps / "SHA256SUMS").read_text(encoding="utf-8") == f"{sha256_hex(final)}  {NOW_NAME}\n"

    record_path = setup.state / "last-backup.json"
    raw = json.loads(record_path.read_text(encoding="utf-8"))
    assert set(raw) == RECORD_KEYS
    assert raw == {"version": 1, "finished_at": NOW_TEXT, "file": NOW_NAME, "size_bytes": len(final),
                   "sha256": sha256_hex(final)}
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", raw["finished_at"])
    assert re.fullmatch(r"cq-\d{8}T\d{6}Z\.dump\.age", raw["file"])
    assert re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) and raw["size_bytes"] > 0
    assert type(raw["version"]) is int and type(raw["size_bytes"]) is int
    assert cq_backup.read_record(record_path) == cq_backup.BackupRecord(
        version=1, finished_at=NOW_TEXT, file=NOW_NAME, size_bytes=len(final), sha256=sha256_hex(final))
    assert set(_names(setup.state)) <= {"last-backup.json", cq_backup.LOCK_FILE}

    by_target = {dst: src for src, dst in replace_calls}
    dump_dst, record_dst, sums_dst = setup.dumps / NOW_NAME, record_path, setup.dumps / "SHA256SUMS"
    for target in (dump_dst, record_dst, sums_dst):
        assert target in by_target, f"{target.name} não foi gravado por os.replace"
        assert by_target[target].parent == target.parent
    assert TMP_PATTERN.fullmatch(by_target[dump_dst].name)
    targets = [dst for _, dst in replace_calls]
    assert targets.index(dump_dst) < targets.index(record_dst)


def test_success_file_modes_on_posix(setup: Setup) -> None:
    """no POSIX, o dump e o `SHA256SUMS` em `0640` e o `last-backup.json` em `0644` (no Windows, a
    rotina roda e o caso só confere o sucesso)."""
    code = setup.run()

    assert code == 0
    if os.name == "posix":
        assert (setup.dumps / NOW_NAME).stat().st_mode & 0o777 == 0o640
        assert (setup.dumps / "SHA256SUMS").stat().st_mode & 0o777 == 0o640
        assert (setup.state / "last-backup.json").stat().st_mode & 0o777 == 0o644


def test_sums_list_every_retained_dump_in_name_order(setup: Setup) -> None:
    """`SHA256SUMS` com uma linha `<64 hex>  <nome>` por dump retido (os antigos dentro do
    prazo e o novo), em ordem de nome, igual ao SHA-256 real de cada arquivo; nomes fora do padrão ficam de
    fora."""
    older = _write_dump(setup.dumps, "cq-20261027T033000Z.dump.age")
    newer = _write_dump(setup.dumps, "cq-20261028T033000Z.dump.age")
    (setup.dumps / "notas.txt").write_bytes(b"anotacao do operador\n")

    code = setup.run()

    assert code == 0
    final = (setup.dumps / NOW_NAME).read_bytes()
    expected = [f"{sha256_hex(older)}  cq-20261027T033000Z.dump.age",
                f"{sha256_hex(newer)}  cq-20261028T033000Z.dump.age",
                f"{sha256_hex(final)}  {NOW_NAME}"]
    lines = (setup.dumps / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert lines == expected
    for line in lines:
        assert re.fullmatch(r"[0-9a-f]{64}  cq-\d{8}T\d{6}Z\.dump\.age", line)


def test_children_environment_drops_every_cq_variable(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """o ambiente de cada processo filho é o do processo **sem** nenhuma `CQ_*` (a rotina não lê
    nem passa nenhuma); as demais variáveis seguem."""
    monkeypatch.setenv("CQ_MASTER_KEY", "chave-mestra-que-nao-pode-ir-ao-filho")
    monkeypatch.setenv("CQ_DB_PASSWORD", "senha-que-nao-pode-ir-ao-filho")
    monkeypatch.setenv("CQ_BACKUP_PROJECT", PROJECT)
    monkeypatch.setenv("BKP_TESTE_MANTIDA", "segue-para-o-filho")
    expected = {name: value for name, value in os.environ.items() if not name.upper().startswith("CQ_")}

    code = setup.run()

    assert code == 0
    assert len(setup.spawn.calls) == 3
    for call in setup.spawn.calls:
        assert call.env is not None
        assert not [name for name in call.env if name.upper().startswith("CQ_")]
        assert call.env == expected


def test_env_file_is_never_opened(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """a rotina nunca abre um `.env` (nem o do diretório de trabalho): `open`, `io.open` e
    `os.open` registrados durante a execução."""
    workdir = setup.root / "trabalho"
    workdir.mkdir()
    (workdir / ".env").write_text("CQ_MASTER_KEY=nao-pode-ser-lida\n", encoding="utf-8")
    (setup.root / ".env").write_text("CQ_DB_PASSWORD=nao-pode-ser-lida\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def tracking_open(file: object, *args: object, **kwargs: object) -> object:
        opened.append(os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else repr(file))
        return real_open(file, *args, **kwargs)

    def tracking_io_open(file: object, *args: object, **kwargs: object) -> object:
        opened.append(os.fspath(file) if isinstance(file, (str, bytes, os.PathLike)) else repr(file))
        return real_io_open(file, *args, **kwargs)

    def tracking_os_open(path: object, *args: object, **kwargs: object) -> int:
        opened.append(os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else repr(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(io, "open", tracking_io_open)
    monkeypatch.setattr(os, "open", tracking_os_open)

    code = setup.run()

    monkeypatch.undo()
    assert code == 0
    assert opened, "o registro de aberturas não viu nada (o monkeypatch não pegou)"
    env_opens = [path for path in opened if os.path.basename(str(path)) in (".env", b".env")]
    assert env_opens == []


def test_orphan_temporaries_are_removed_under_the_lock(setup: Setup) -> None:
    """`.tmp-*` órfãos de uma execução interrompida são apagados (sob a trava); outros nomes
    ficam."""
    (setup.dumps / ".tmp-20261028T033000Z-4242.age").write_bytes(AGE_HEADER + b"\nresto de execucao\n")
    (setup.dumps / ".tmp-20261027T033000Z-17.age").write_bytes(b"")
    (setup.dumps / "notas.txt").write_bytes(b"fica\n")

    code = setup.run()

    assert code == 0
    assert _names(setup.dumps) == sorted([NOW_NAME, "SHA256SUMS", "notas.txt"])


# --------------------------------------------------------------------------------------------------
# Falhas
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("pg_dump_code", "age_code", "key"), [
    pytest.param(1, 0, "dump_failed", id="pg-dump-falha-age-ok"),
    pytest.param(0, 1, "encrypt_failed", id="age-falha-pg-dump-ok"),
])
def test_pipe_checks_both_exit_codes(pg_dump_code: int, age_code: int, key: str, tmp_path: Path) -> None:
    """os dois códigos do pipe; a falha de um com o outro em 0 → a chave dele, sem arquivo com
    nome final, sem temporário, sem tocar no registro nem no `SHA256SUMS` do backup anterior, nada em
    claro em momento nenhum, uma linha fixa no stderr."""
    setup = _make_setup(tmp_path, pg_dump_code=pg_dump_code, age_code=age_code)
    old = _write_dump(setup.dumps, "cq-20261020T033000Z.dump.age")
    previous = _seed_previous(setup)

    code = setup.run()

    _assert_failure(setup, code, key)
    assert _names(setup.dumps) == sorted(["cq-20261020T033000Z.dump.age", PREVIOUS_NAME, "SHA256SUMS"])
    assert (setup.dumps / "cq-20261020T033000Z.dump.age").read_bytes() == old  # nada podado numa falha
    _previous_intact(setup, previous)
    for stage, plain, _ in setup.stages:
        assert plain == [], f"dump em claro em disco na etapa {stage}"


def test_age_exit_zero_without_header_is_encrypt_failed(tmp_path: Path,
                                                         replace_calls: list[tuple[Path, Path]]) -> None:
    """O temporário só vira o nome final depois dos dois códigos 0 **e** do
    cabeçalho `age-encryption.org/v1` conferido. Com o `pg_dump` e o `age` em 0 mas o temporário sem o
    cabeçalho → `encrypt_failed` (70): nenhum `os.replace` para o nome final, o `SHA256SUMS` ou o registro;
    sem arquivo com nome final, sem temporário sobrando, o registro e o `SHA256SUMS` do backup anterior
    intactos, nada em claro em momento nenhum, uma linha fixa no stderr."""
    setup = _make_setup(tmp_path, age_omit_header=True)
    previous = _seed_previous(setup)

    code = setup.run()

    _assert_failure(setup, code, "encrypt_failed")
    # O caso não é vazio: os três processos rodaram, os dois do pipe saíram 0, e o `age` gravou o corpo sem
    # cabeçalho no temporário (o gancho viu o temporário no diretório dos dumps nesse instante).
    argvs = setup.spawn.argvs
    assert len(argvs) == 3
    assert argvs[:2] == [docker_ps_argv(PROJECT), pg_dump_argv(CONTAINER)]
    temporary = _tmp_argv_ok(argvs[2], setup)
    assert [call.process.wait() for call in setup.spawn.calls[1:]] == [0, 0]
    written = [names for stage, _, names in setup.stages if stage == "body"]
    assert len(written) == 1 and temporary.name in written[0]
    assert "header" not in [stage for stage, _, _ in setup.stages]
    for stage, plain, names in setup.stages:
        assert plain == [], f"dump em claro em disco na etapa {stage}"
        assert NOW_NAME not in names, f"nome final antes da hora (etapa {stage})"
    # Nada promovido: nem o dump, nem o resumo, nem o registro; o temporário foi removido.
    promoted = {dst.name for _, dst in replace_calls}
    assert promoted.isdisjoint({NOW_NAME, "SHA256SUMS", "last-backup.json"})
    assert not temporary.exists(), "o temporário sem cabeçalho sobreviveu à falha"
    assert not [name for name in _names(setup.dumps) if TMP_PATTERN.fullmatch(name)]
    assert NOW_NAME not in _names(setup.dumps)
    _previous_intact(setup, previous)


@pytest.mark.parametrize("containers", [
    pytest.param((), id="nenhum"),
    pytest.param((CONTAINER, OTHER_CONTAINER), id="dois"),
])
def test_container_must_be_exactly_one(containers: tuple[str, ...], tmp_path: Path) -> None:
    """0 ou 2 contêineres do `postgres` em execução → `container_not_found` (69), só o
    `docker ps` chamado, nada gravado e o registro e o `SHA256SUMS` anteriores intactos."""
    setup = _make_setup(tmp_path, containers=containers)
    previous = _seed_previous(setup)

    code = setup.run()

    _assert_failure(setup, code, "container_not_found")
    assert setup.spawn.argvs == [docker_ps_argv(PROJECT)]
    _previous_intact(setup, previous)


def test_unwritable_directory_is_write_failed(setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """o disco sem escrita (o `os.replace` levanta `OSError`) → `write_failed` (73); o temporário não
    sobrevive, nenhum nome final, e o registro e o `SHA256SUMS` do backup anterior ficam byte a byte iguais
    (nem apagados nem truncados)."""
    previous = _seed_previous(setup)

    def refuse(src: object, dst: object, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", refuse)

    code = setup.run()

    _assert_failure(setup, code, "write_failed")
    _previous_intact(setup, previous)


def test_failure_after_rename_leaves_unrecorded_dump_then_next_success_includes_it(
        setup: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    """uma falha depois de `renamed` e antes de `state_written` deixa o dump novo sem registro
    e sai ≠ 0 (uma linha fixa); o sucesso seguinte o inclui no `SHA256SUMS`."""
    original = os.replace
    failed: list[Path] = []

    def failing_record(src: object, dst: object, *args: object, **kwargs: object) -> None:
        target = Path(os.fspath(dst))
        if target.name == "last-backup.json" and not failed:
            failed.append(target)
            raise OSError(5, "Input/output error")
        original(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_record)

    first = setup.run()

    assert failed, "o registro não foi gravado por os.replace"
    assert first != 0
    assert setup.out.getvalue() == ""
    assert setup.err.getvalue() in {text + "\n" for _, text in EXPECTED_MESSAGES.values()}
    assert NOW_NAME in _names(setup.dumps)
    assert not (setup.state / "last-backup.json").exists()

    setup.reset_streams()
    later = NOW + timedelta(hours=1)
    later_name = "cq-20261029T043005Z.dump.age"
    second = setup.run(now=later)

    assert second == 0
    sums = (setup.dumps / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in sums] == [NOW_NAME, later_name]
    for line in sums:
        digest, name = line.split("  ", 1)
        assert digest == sha256_hex((setup.dumps / name).read_bytes())
    record = json.loads((setup.state / "last-backup.json").read_text(encoding="utf-8"))
    assert record["file"] == later_name


def test_busy_lock_exits_at_once_without_touching_anything(setup: Setup) -> None:
    """com a trava ocupada (uma `acquire_lock` no mesmo diretório de estado), a rotina sai
    `busy` (75) na hora: nenhum processo, e os dumps, o estado e até os órfãos ficam como estavam."""
    _write_dump(setup.dumps, "cq-20261020T033000Z.dump.age")
    (setup.dumps / ".tmp-20261028T033000Z-4242.age").write_bytes(b"orfao")
    (setup.state / "last-backup.json").write_text('{"anterior": true}', encoding="utf-8")
    with cq_backup.acquire_lock(setup.state) as held:
        assert held is True
        before = _snapshot(setup.dumps, setup.state)
        code = setup.run()
        after = _snapshot(setup.dumps, setup.state)

    _assert_failure(setup, code, "busy")
    assert setup.spawn.calls == []
    assert after == before


def test_acquire_lock_reports_busy_and_releases(tmp_path: Path) -> None:
    """a segunda `acquire_lock` no mesmo diretório dá `False` enquanto a primeira vale; depois
    de solta, a trava volta a ser obtida; o arquivo da trava fica vazio."""
    state = tmp_path / "state"
    state.mkdir()
    with cq_backup.acquire_lock(state) as first:
        assert first is True
        with cq_backup.acquire_lock(state) as second:
            assert second is False
    with cq_backup.acquire_lock(state) as again:
        assert again is True
    assert (state / "backup.lock").read_bytes() == b""


# --------------------------------------------------------------------------------------------------
# Poda
# --------------------------------------------------------------------------------------------------


def test_prune_after_success_uses_name_stamp_not_mtime(setup: Setup) -> None:
    """a poda só depois do sucesso, pelo carimbo do nome (não pelo `mtime`): o dump de 9 dias sai
    mesmo com `mtime` de agora; o de 4 dias fica mesmo com `mtime` antigo; nomes fora do padrão ficam."""
    old_name, recent_name = "cq-20261020T033000Z.dump.age", "cq-20261025T033000Z.dump.age"
    _write_dump(setup.dumps, old_name, mtime=NOW)
    _write_dump(setup.dumps, recent_name, mtime=NOW - timedelta(days=60))
    keep_out = ["cq-20261001T000000Z.dump.age.bak", "outro.dump.age", "cq-20261001.dump.age", "notas.txt"]
    for name in keep_out:
        (setup.dumps / name).write_bytes(b"fora do padrao\n")

    code = setup.run(keep_days=7)

    assert code == 0
    assert _names(setup.dumps) == sorted([recent_name, NOW_NAME, "SHA256SUMS", *keep_out])
    sums = (setup.dumps / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    assert [line.split("  ", 1)[1] for line in sums] == [recent_name, NOW_NAME]


def test_prune_never_removes_the_kept_dump(tmp_path: Path) -> None:
    """`prune` devolve os nomes apagados, poda pelo carimbo e nunca apaga `keep` (o recém
    gerado), mesmo com carimbo antigo; nomes fora do padrão ficam."""
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for name in ("cq-20261001T000000Z.dump.age", "cq-20261010T000000Z.dump.age",
                 "cq-20261028T000000Z.dump.age", "notas.txt", "cq-20261001T000000Z.dump.age.bak"):
        (dumps / name).write_bytes(b"x")

    pruned = cq_backup.prune(dumps, 7, NOW, "cq-20261001T000000Z.dump.age")

    assert sorted(pruned) == ["cq-20261010T000000Z.dump.age"]
    assert _names(dumps) == sorted(["cq-20261001T000000Z.dump.age", "cq-20261028T000000Z.dump.age",
                                    "notas.txt", "cq-20261001T000000Z.dump.age.bak"])


# --------------------------------------------------------------------------------------------------
# read_record
# --------------------------------------------------------------------------------------------------

GOOD_RECORD = {"version": 1, "finished_at": NOW_TEXT, "file": NOW_NAME, "size_bytes": 123,
               "sha256": "a" * 64}


def test_read_record_accepts_the_exact_shape(tmp_path: Path) -> None:
    """o objeto com exatamente as cinco chaves e os tipos certos vira `BackupRecord`."""
    path = tmp_path / "last-backup.json"
    path.write_text(json.dumps(GOOD_RECORD), encoding="utf-8")
    assert cq_backup.read_record(path) == cq_backup.BackupRecord(**GOOD_RECORD)


@pytest.mark.parametrize("content", [
    pytest.param(None, id="ausente"),
    pytest.param(b"", id="vazio"),
    pytest.param(json.dumps(GOOD_RECORD).encode() + b" " * 4097, id="maior-que-4096"),
    pytest.param(b"{nao e json", id="json-invalido"),
    pytest.param(json.dumps([GOOD_RECORD]).encode(), id="lista"),
    pytest.param(json.dumps({**GOOD_RECORD, "extra": 1}).encode(), id="chave-a-mais"),
    pytest.param(json.dumps({k: v for k, v in GOOD_RECORD.items() if k != "sha256"}).encode(), id="chave-a-menos"),
    pytest.param(json.dumps({**GOOD_RECORD, "version": 2}).encode(), id="versao-2"),
    pytest.param(json.dumps({**GOOD_RECORD, "version": "1"}).encode(), id="versao-texto"),
    pytest.param(json.dumps({**GOOD_RECORD, "version": True}).encode(), id="versao-booleana"),
    pytest.param(json.dumps({**GOOD_RECORD, "size_bytes": 0}).encode(), id="tamanho-zero"),
    pytest.param(json.dumps({**GOOD_RECORD, "size_bytes": "123"}).encode(), id="tamanho-texto"),
    pytest.param(json.dumps({**GOOD_RECORD, "size_bytes": 12.5}).encode(), id="tamanho-fracionario"),
    pytest.param(json.dumps({**GOOD_RECORD, "sha256": "A" * 64}).encode(), id="resumo-maiusculo"),
    pytest.param(json.dumps({**GOOD_RECORD, "sha256": "a" * 63}).encode(), id="resumo-curto"),
    pytest.param(json.dumps({**GOOD_RECORD, "finished_at": "2026-10-29T03:30:05+00:00"}).encode(),
                 id="instante-sem-z"),
    pytest.param(json.dumps({**GOOD_RECORD, "finished_at": 1730000000}).encode(), id="instante-numero"),
    pytest.param(json.dumps({**GOOD_RECORD, "file": "notas.txt"}).encode(), id="arquivo-fora-do-padrao"),
])
def test_read_record_rejects_every_deviation(content: bytes | None, tmp_path: Path) -> None:
    """ausente, vazio, maior que 4096 bytes, JSON inválido, chave a mais ou a menos, tipo ou
    forma errada → `None` (sem exceção)."""
    path = tmp_path / "last-backup.json"
    if content is not None:
        path.write_bytes(content)
    assert cq_backup.read_record(path) is None


# --------------------------------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------------------------------


def _cli_args(setup: Setup, *extra: str) -> list[str]:
    return ["run", "--project", PROJECT, "--recipients", str(setup.recipients), "--dumps-dir", str(setup.dumps),
            "--state-dir", str(setup.state), *extra]


@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5", ""])
def test_main_rejects_invalid_keep_days(value: str, setup: Setup, no_real_process: None) -> None:
    """`--keep-days` inteiro ≥ 1; `0`, `-1`, `abc`, `1.5` e vazio → `invalid_arguments` (64),
    uma linha fixa no stderr, sem eco do valor, sem processo, sem arquivo novo e o registro anterior intacto."""
    previous = _seed_previous(setup)

    result = run_main(cq_backup, _cli_args(setup, "--keep-days", value), env=clean_env())

    assert result.code == 64
    assert (result.stdout, result.stderr) == ("", "Argumentos inválidos.\n")
    _previous_intact(setup, previous)


@pytest.mark.parametrize("argv", [
    pytest.param([], id="sem-comando"),
    pytest.param(["backup"], id="comando-desconhecido"),
    pytest.param(["run", "--project", PROJECT], id="sem-diretorios"),
    pytest.param(["run", "--projeto", PROJECT], id="opcao-desconhecida"),
])
def test_main_rejects_malformed_command_line(argv: list[str], no_real_process: None) -> None:
    """linha de comando inválida → `invalid_arguments` (64) sem eco (nem o `usage` do
    `argparse`)."""
    result = run_main(cq_backup, argv, env=clean_env())

    assert result.code == 64
    assert (result.stdout, result.stderr) == ("", "Argumentos inválidos.\n")


def test_main_builds_config_and_delegates_to_run(setup: Setup, monkeypatch: pytest.MonkeyPatch,
                                                 no_real_process: None) -> None:
    """o `main` monta o `BackupConfig` com os parâmetros (padrões `--keep-days 7`,
    `--age-bin age`, `--docker-bin docker`) e devolve o código do `run`."""
    seen: list[object] = []

    def fake_run(*args: object, **kwargs: object) -> int:
        seen.append(args[0] if args else kwargs["config"])
        return 0

    monkeypatch.setattr(cq_backup, "run", fake_run)

    result = run_main(cq_backup, _cli_args(setup), env=clean_env())
    custom = run_main(cq_backup, _cli_args(setup, "--keep-days", "3", "--age-bin", "C:/age/age.exe",
                                           "--docker-bin", "/usr/local/bin/docker"), env=clean_env())

    assert (result.code, custom.code) == (0, 0)
    assert seen == [
        cq_backup.BackupConfig(project=PROJECT, recipients=setup.recipients, dumps_dir=setup.dumps,
                               state_dir=setup.state, keep_days=7, age_bin="age", docker_bin="docker"),
        cq_backup.BackupConfig(project=PROJECT, recipients=setup.recipients, dumps_dir=setup.dumps,
                               state_dir=setup.state, keep_days=3, age_bin="C:/age/age.exe",
                               docker_bin="/usr/local/bin/docker"),
    ]
