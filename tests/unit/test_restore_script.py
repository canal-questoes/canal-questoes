"""Testes da restauração `ops/backup/restore.py`.


Tudo com o `FakeSpawn` (nenhum processo real; o `subprocess.Popen` fica barrado nos testes do `main`). O
dump é a cifra falsa do `age` falso (`fake_age_ciphertext`), e o `age -d` falso devolve no stdout os bytes do
dump em claro (começam com `PGDMP`); o `pg_restore` falso os lê do stdin. Os ganchos das duas etapas varrem o
`tmp_path` inteiro: nenhum arquivo com `PGDMP` em momento nenhum.

A identidade é um arquivo do `tmp_path` com a forma de uma chave do age (falsa); os testes registram os
caminhos abertos (`open`, `io.open`, `os.open`) e varrem as saídas com `find_secrets` pelo conteúdo dela.

**[I]** Escolhas deste teste: o `main` chama `restore` do módulo com `Path` para `--dump` e
`--identity` (o teste de delegação troca `restore` por um registrador); as recusas, na ordem da implementação, saem
antes de qualquer processo, inclusive as de argumento e de projeto reservado.
"""

from __future__ import annotations

import builtins
import dataclasses
import io
import os
import subprocess
from pathlib import Path

import pytest

from tests.helpers.canary import find_secrets
from tests.helpers.ops import clean_env, load_ops, run_main
from tests.helpers.process_fake import (DEFAULT_DUMP, IGNORED_ERRORS_STDERR, PIPE, FakeSpawn, age_decrypt_argv,
                                        docker_ps_argv, docker_ps_project_argv, fake_age_ciphertext,
                                        pg_restore_argv, psql_tables_argv)

restore_mod = load_ops("ops/backup/restore.py")

PROJECT = "cq-restauracao"
CONTAINER = "7c6b5a4f3e2d"
OTHER_CONTAINER = "1a2b3c4d5e6f"
FAKE_SECRET_KEY = "AGE-SECRET-KEY-1" + "ZRY9X8GF2TVDW0S3JN54KHCE6MUA7LQQP" * 2
DUMP_NAME = "cq-20261029T033005Z.dump.age"
AGE_FAILURE_STDERR = b"age: error: no identity matched any of the recipients XYZ-SAIDA-AGE\n"
PG_RESTORE_FAILURE_STDERR = b"pg_restore: error: could not execute query XYZ-SAIDA-PG-RESTORE\n"

# Chave → (código, texto).
EXPECTED_MESSAGES = {
    "ok": (0, "Restauração concluída."),
    "invalid_arguments": (64, "Argumentos inválidos."),
    "decrypt_failed": (65, "A decifração do dump falhou."),
    "container_not_found": (69, "O contêiner do Postgres do projeto não foi encontrado em execução."),
    "restore_failed": (70, "A restauração do banco falhou; nada foi gravado."),
    "not_empty": (73, "O banco de destino não está vazio."),
    "stack_running": (75, "Outro serviço do projeto está no ar; deixe só o Postgres."),
    "reserved_project": (77, "Este projeto não pode receber restauração."),
    "internal": (1, "Erro interno da restauração."),
}


@dataclasses.dataclass
class Setup:
    root: Path
    dump: Path
    identity: Path
    spawn: FakeSpawn
    out: io.StringIO
    err: io.StringIO
    plain_seen: list[tuple[str, list[str]]]
    opened: list[str]

    def run(self, project: str = PROJECT, **kwargs: object) -> int:
        return restore_mod.restore(self.dump, self.identity, project, spawn=self.spawn, stdout=self.out,
                                   stderr=self.err, **kwargs)


def _plain_files(root: Path, identity: Path) -> list[str]:
    """Os arquivos com `PGDMP` sob `root` (a identidade fica de fora: o teste não a abre)."""
    return sorted(path.name for path in root.rglob("*")
                  if path.is_file() and path != identity and b"PGDMP" in path.read_bytes())


def _make_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, project: str = PROJECT,
                postgres: tuple[str, ...] = (CONTAINER,), project_ids: tuple[str, ...] | None = None,
                tables: int = 0, age_code: int = 0, restore_code: int = 0, restore_stderr: bytes = b"") -> Setup:
    root = tmp_path
    backups = root / "copias"
    keys = root / "chaves"
    workdir = root / "trabalho"
    for directory in (backups, keys, workdir):
        directory.mkdir()
    dump = backups / DUMP_NAME
    dump.write_bytes(fake_age_ciphertext(DEFAULT_DUMP))
    identity = keys / "identidade-backup.txt"
    identity.write_text(f"# created: 2026-10-29T00:00:00Z\n# public key: age1exemplo\n{FAKE_SECRET_KEY}\n",
                        encoding="ascii")
    monkeypatch.chdir(workdir)
    plain_seen: list[tuple[str, list[str]]] = []

    def hook(stage: str) -> None:
        plain_seen.append((stage, _plain_files(root, identity)))

    spawn = FakeSpawn()
    spawn.on_docker_ps(list(postgres), project=project)
    spawn.on_docker_ps_project(list(postgres if project_ids is None else project_ids), project=project)
    spawn.on_psql_tables(tables)
    spawn.on_age(returncode=age_code, stderr=AGE_FAILURE_STDERR if age_code else b"", hook=hook)
    spawn.on_pg_restore(returncode=restore_code,
                        stderr=restore_stderr or (PG_RESTORE_FAILURE_STDERR if restore_code else b""), hook=hook)
    opened: list[str] = []
    real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

    def _name(file: object) -> str:
        return os.fsdecode(os.fspath(file)) if isinstance(file, (str, bytes, os.PathLike)) else repr(file)

    def tracking_open(file: object, *args: object, **kwargs: object) -> object:
        opened.append(_name(file))
        return real_open(file, *args, **kwargs)

    def tracking_io_open(file: object, *args: object, **kwargs: object) -> object:
        opened.append(_name(file))
        return real_io_open(file, *args, **kwargs)

    def tracking_os_open(path: object, *args: object, **kwargs: object) -> int:
        opened.append(_name(path))
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    monkeypatch.setattr(io, "open", tracking_io_open)
    monkeypatch.setattr(os, "open", tracking_os_open)
    return Setup(root=root, dump=dump, identity=identity, spawn=spawn, out=io.StringIO(), err=io.StringIO(),
                 plain_seen=plain_seen, opened=opened)


def _identity_untouched(setup: Setup) -> None:
    """A identidade nunca aberta pelo processo do teste, nem copiada para outro arquivo do `tmp_path`, nem
    impressa."""
    assert not [path for path in setup.opened if Path(path) == setup.identity]
    for path in setup.root.rglob("*"):
        if path.is_file() and path != setup.identity:
            assert FAKE_SECRET_KEY.encode("ascii") not in path.read_bytes(), f"identidade copiada: {path.name}"
    assert find_secrets(setup.out.getvalue() + setup.err.getvalue(), [FAKE_SECRET_KEY]) == []


def _assert_exit(setup: Setup, code: int, key: str, project: str = PROJECT) -> None:
    """O código e o texto exatos de `key`: `ok` no stdout; as demais, uma linha no stderr,
    sem caminho, nome de projeto, conteúdo da identidade nem saída de processo."""
    expected_code, text = EXPECTED_MESSAGES[key]
    assert code == expected_code
    if key == "ok":
        assert (setup.out.getvalue(), setup.err.getvalue()) == (text + "\n", "")
    else:
        assert (setup.out.getvalue(), setup.err.getvalue()) == ("", text + "\n")
    leaked = find_secrets(setup.out.getvalue() + setup.err.getvalue(),
                          [str(setup.root), setup.root.as_posix(), project, CONTAINER, DUMP_NAME,
                           AGE_FAILURE_STDERR.decode(), PG_RESTORE_FAILURE_STDERR.decode(),
                           IGNORED_ERRORS_STDERR.decode(), restore_mod.IGNORED_ERRORS_MARKER])
    assert leaked == []
    _identity_untouched(setup)


def _expected_argvs(setup: Setup, project: str = PROJECT) -> list[list[str]]:
    return [docker_ps_argv(project), docker_ps_project_argv(project), psql_tables_argv(CONTAINER),
            age_decrypt_argv(setup.identity, setup.dump), pg_restore_argv(CONTAINER)]


@pytest.fixture
def no_real_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        pytest.fail("o teste do main não pode criar processo real")

    monkeypatch.setattr(subprocess, "Popen", refuse)


# --------------------------------------------------------------------------------------------------
# Sucesso
# --------------------------------------------------------------------------------------------------


def test_messages_table_and_successful_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Códigos e textos fixos e as constantes, na mesma função de uma
    restauração bem-sucedida (código 0, `Restauração concluída.` no stdout)."""
    assert restore_mod.RESTORE_MESSAGES == EXPECTED_MESSAGES
    assert restore_mod.RESERVED == frozenset({"cq", "cq-demo"})
    assert restore_mod.PRODUCTION_PROJECT == "cq"
    assert restore_mod.IGNORED_ERRORS_MARKER == "errors ignored on restore"
    setup = _make_setup(tmp_path, monkeypatch)

    code = setup.run()

    _assert_exit(setup, code, "ok")


def test_success_runs_exact_argv_with_streaming_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os cinco argv exatos, nesta ordem; o `age -d` com `stdout=PIPE` e o `pg_restore`
    lendo o stdout dele (os dois encadeados, os bytes do dump em claro só no pipe); nenhum arquivo com `PGDMP`
    em disco em nenhum momento (os ganchos das duas etapas)."""
    setup = _make_setup(tmp_path, monkeypatch)

    code = setup.run()

    assert code == 0
    assert setup.spawn.argvs == _expected_argvs(setup)
    calls = setup.spawn.calls
    assert all(call.env is not None for call in calls)
    assert calls[3].stdout == PIPE
    assert calls[4].stdin is calls[3].process.stdout
    assert calls[4].stdin_data == DEFAULT_DUMP
    stages = [stage for stage, _ in setup.plain_seen]
    assert "run" in stages
    for stage, plain in setup.plain_seen:
        assert plain == [], f"dump em claro em disco na etapa {stage}"
    assert _plain_files(setup.root, setup.identity) == []
    _identity_untouched(setup)


def test_production_flag_restores_into_cq(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--production --project cq` (a recuperação numa VM nova) segue o mesmo caminho dos argv."""
    setup = _make_setup(tmp_path, monkeypatch, project="cq")

    code = setup.run("cq", production=True)

    _assert_exit(setup, code, "ok", project="cq")
    assert setup.spawn.argvs == _expected_argvs(setup, "cq")


def test_custom_binaries_are_used_in_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--age-bin` e `--docker-bin` trocam só o primeiro elemento dos argv."""
    setup = _make_setup(tmp_path, monkeypatch)
    age_bin, docker_bin = "C:/ferramentas/age/age.exe", "/usr/local/bin/docker"
    spawn = FakeSpawn()
    spawn.on_docker_ps([CONTAINER], project=PROJECT, docker=docker_bin)
    spawn.on_docker_ps_project([CONTAINER], project=PROJECT, docker=docker_bin)
    spawn.on_psql_tables(0, docker=docker_bin)
    spawn.on_age(age=age_bin)
    spawn.on_pg_restore(docker=docker_bin)
    setup.spawn = spawn

    code = setup.run(age_bin=age_bin, docker_bin=docker_bin)

    assert code == 0
    assert spawn.argvs == [docker_ps_argv(PROJECT, docker=docker_bin),
                           docker_ps_project_argv(PROJECT, docker=docker_bin),
                           psql_tables_argv(CONTAINER, docker=docker_bin),
                           age_decrypt_argv(setup.identity, setup.dump, age=age_bin),
                           pg_restore_argv(CONTAINER, docker=docker_bin)]


# --------------------------------------------------------------------------------------------------
# Falhas do pipe
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("age_code", "restore_code", "restore_stderr", "key"), [
    pytest.param(1, 0, b"", "decrypt_failed", id="age-falha-pg-restore-ok"),
    pytest.param(0, 1, b"", "restore_failed", id="pg-restore-falha-age-ok"),
    pytest.param(0, 0, IGNORED_ERRORS_STDERR, "restore_failed", id="erros-ignorados-com-codigo-0"),
])
def test_pipe_checks_both_codes_and_ignored_errors(age_code: int, restore_code: int, restore_stderr: bytes,
                                                   key: str, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """os dois códigos do pipe; `errors ignored on restore` no stderr do `pg_restore` com código 0
    também é falha; o stderr do `pg_restore` não é repetido; nada em claro em disco."""
    setup = _make_setup(tmp_path, monkeypatch, age_code=age_code, restore_code=restore_code,
                        restore_stderr=restore_stderr)

    code = setup.run()

    _assert_exit(setup, code, key)
    assert setup.spawn.argvs == _expected_argvs(setup)
    for stage, plain in setup.plain_seen:
        assert plain == [], f"dump em claro em disco na etapa {stage}"
    assert _plain_files(setup.root, setup.identity) == []


# --------------------------------------------------------------------------------------------------
# Recusas
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("project", "production", "key"), [
    pytest.param("cq", False, "reserved_project", id="cq-sem-production"),
    pytest.param("cq-demo", False, "reserved_project", id="cq-demo"),
    pytest.param("cq-demo", True, "reserved_project", id="cq-demo-com-production"),
    pytest.param(PROJECT, True, "invalid_arguments", id="production-com-outro-projeto"),
])
def test_reserved_projects_and_production_rule(project: str, production: bool, key: str, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """`cq` sem `--production` e `cq-demo` sempre → `reserved_project` (77); `--production` com outro
    projeto → `invalid_arguments` (64); nenhum processo, e a identidade nem é aberta."""
    setup = _make_setup(tmp_path, monkeypatch, project=project)

    code = setup.run(project, production=production)

    _assert_exit(setup, code, key, project=project)
    assert setup.spawn.calls == []


@pytest.mark.parametrize("postgres", [
    pytest.param((), id="nenhum"),
    pytest.param((CONTAINER, OTHER_CONTAINER), id="dois"),
])
def test_postgres_container_must_be_exactly_one(postgres: tuple[str, ...], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """0 ou 2 contêineres do `postgres` em execução → `container_not_found` (69); só o
    primeiro `docker ps`."""
    setup = _make_setup(tmp_path, monkeypatch, postgres=postgres)

    code = setup.run()

    _assert_exit(setup, code, "container_not_found")
    assert setup.spawn.argvs == [docker_ps_argv(PROJECT)]


def test_other_service_running_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """outro contêiner do projeto no ar (além do `postgres`) → `stack_running` (75), antes da
    contagem e do `age`."""
    setup = _make_setup(tmp_path, monkeypatch, project_ids=(CONTAINER, OTHER_CONTAINER))

    code = setup.run()

    _assert_exit(setup, code, "stack_running")
    assert setup.spawn.argvs == [docker_ps_argv(PROJECT), docker_ps_project_argv(PROJECT)]


@pytest.mark.parametrize("tables", [1, 42])
def test_non_empty_database_is_refused(tables: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """banco de destino com tabelas em `public` → `not_empty` (73), antes do `age`."""
    setup = _make_setup(tmp_path, monkeypatch, tables=tables)

    code = setup.run()

    _assert_exit(setup, code, "not_empty")
    assert setup.spawn.argvs == [docker_ps_argv(PROJECT), docker_ps_project_argv(PROJECT),
                                 psql_tables_argv(CONTAINER)]


# --------------------------------------------------------------------------------------------------
# Linha de comando
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [
    pytest.param([], id="vazio"),
    pytest.param(["--identity", "x.txt", "--project", PROJECT], id="sem-dump"),
    pytest.param(["--dump", "x.age", "--project", PROJECT], id="sem-identidade"),
    pytest.param(["--dump", "x.age", "--identity", "x.txt"], id="sem-projeto"),
    pytest.param(["--dump", "x.age", "--identity", "x.txt", "--project", PROJECT, "--forca"], id="opcao-desconhecida"),
])
def test_main_rejects_malformed_command_line(argv: list[str], no_real_process: None) -> None:
    """linha de comando inválida → `invalid_arguments` (64), uma linha fixa, sem o `usage` do
    `argparse` nem eco."""
    result = run_main(restore_mod, argv, env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (64, "", "Argumentos inválidos.\n")


def test_main_refuses_cq_without_production_before_any_process(tmp_path: Path, no_real_process: None) -> None:
    """pelo `main`, `--project cq` sem `--production` → 77, sem processo."""
    dump, identity = tmp_path / DUMP_NAME, tmp_path / "id.txt"
    dump.write_bytes(fake_age_ciphertext(DEFAULT_DUMP))
    identity.write_text(f"{FAKE_SECRET_KEY}\n", encoding="ascii")
    result = run_main(restore_mod, ["--dump", str(dump), "--identity", str(identity), "--project", "cq"],
                      env=clean_env())

    assert (result.code, result.stdout, result.stderr) == (77, "", "Este projeto não pode receber restauração.\n")


def test_main_delegates_to_restore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_real_process: None) -> None:
    """o `main` passa `--dump`, `--identity`, `--project`, `--production`, `--age-bin` e
    `--docker-bin` (padrões `age` e `docker`) ao `restore` e devolve o código dele."""
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_restore(*args: object, **kwargs: object) -> int:
        seen.append((args, kwargs))
        return 0

    monkeypatch.setattr(restore_mod, "restore", fake_restore)
    dump, identity = tmp_path / DUMP_NAME, tmp_path / "id.txt"

    plain = run_main(restore_mod, ["--dump", str(dump), "--identity", str(identity), "--project", PROJECT],
                     env=clean_env())
    production = run_main(restore_mod, ["--dump", str(dump), "--identity", str(identity), "--project", "cq",
                                        "--production", "--age-bin", "/opt/age/age", "--docker-bin", "podman"],
                          env=clean_env())

    assert (plain.code, production.code) == (0, 0)

    def normalized(args: tuple[object, ...], kwargs: dict[str, object]) -> dict[str, object]:
        names = ("dump", "identity", "project")
        values = dict(zip(names, args))
        values.update(kwargs)
        return {key: values.get(key) for key in ("dump", "identity", "project", "production", "age_bin",
                                                  "docker_bin")}

    assert [normalized(*call) for call in seen] == [
        {"dump": dump, "identity": identity, "project": PROJECT, "production": False, "age_bin": "age",
         "docker_bin": "docker"},
        {"dump": dump, "identity": identity, "project": "cq", "production": True, "age_bin": "/opt/age/age",
         "docker_bin": "podman"},
    ]
