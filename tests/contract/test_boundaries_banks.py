"""Fronteiras do banco de questões local (princípio "Portas e contratos": o núcleo sem HTTP, sem `git`, sem
processos e sem os adaptadores; o único `subprocess` do cliente fica no executor da pilha).

Arquivo à parte: `tests/contract/test_boundaries.py` e `tests/contract/test_boundaries_cli.py` ficam congelados e
sem edição; os verificadores deles são reusados aqui. Cobre:
- `cq_server.banks` (o núcleo) sem `subprocess`, `socket`, HTTP, biblioteca de `git` (`git`, `pygit2`,
  `dulwich`), `tarfile` nem `cq_server.adapters`, por AST, com os controles positivos do verificador; e, num
  processo novo, sem nada do `TRANSITIVE_FORBIDDEN` congelado;
- `cq_server.adapters.banks` sem `subprocess` (a árvore vem do pacote guardado, nunca de um `git`);
- `cq_client.banks` sem `subprocess`, sem `click` e sem `cq_server`;
- `cq_client.stack_compose` continua o único módulo do cliente que importa `subprocess`;
- no `GitRunner`, por AST: nenhum `shutil.which` (nem outro `which`), nenhum `shell=True`, nenhum `os.system` nem
  `os.popen`.

O caso vermelho, na mesma função das checagens por AST: o `GitRunner` real, com um `PATH` montado no `tmp_path`
(uma entrada relativa com um `git` sentinela, uma entrada absoluta dentro da pasta do banco com outro sentinela e
uma entrada absoluta com o `git` dublê), monta o argv `[<git dublê absoluto>, "-C", <pasta>, *args]`, sem shell,
com o ambiente sem nenhuma `GIT_*` herdada mais `GIT_NO_LAZY_FETCH=1`, `GIT_NO_REPLACE_OBJECTS=1` e
`GIT_TERMINAL_PROMPT=0`, e com o diretório de trabalho do processo no diretório do dublê (fora da pasta do banco).
O processo é interceptado no `subprocess.Popen` (o `subprocess.run` passa por ele): nada roda de verdade, e o
`shutil.which` lança se for chamado.

Vermelho no esqueleto: `GitRunner.run` lança `NotImplementedError`.

**[I]** Escolhas deste arquivo:
- no Windows, os sentinelas e o dublê se chamam `git.exe` (os sentinelas também `git.bat` e `git.cmd`); fora dele,
  `git`, com o modo de execução;
- a entrada do processo (`input`) é conferida pelo `communicate` ou pela escrita no `stdin`, o que a implementação
  usar; a saída devolvida é a do dublê.
"""

from __future__ import annotations

import ast
import io
import os
import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from tests.contract.test_boundaries import (
    FORBIDDEN,
    TRANSITIVE_FORBIDDEN,
    _imported_names,
    _is_forbidden,
    _module_files,
)
from tests.contract.test_boundaries_cli import CLIENT_DIR, _click_imports, _client_sources, _loaded_by
from tests.contract.test_catalog import _imports_server

# O `FORBIDDEN` congelado mais `socket`, o `tarfile` e as bibliotecas de `git` (o núcleo lê o pacote sozinho).
BANKS_CORE_FORBIDDEN = FORBIDDEN + ("socket", "tarfile", "git", "pygit2", "dulwich")
GIT_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"}
# As variáveis `GIT_*` herdadas que trocariam o repositório lido (nenhuma pode chegar ao processo).
INHERITED_GIT_ENV = {"GIT_DIR": "outro-repositorio", "GIT_WORK_TREE": "outra-arvore",
                     "GIT_OBJECT_DIRECTORY": "outros-objetos", "GIT_CONFIG_COUNT": "1",
                     "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "sentinela",
                     "GIT_TERMINAL_PROMPT": "1"}
GIT_ARGS = ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"]
# As opções globais que toda chamada passa (a configuração do operador não roda programa) e a lista fechada delas.
GIT_REQUIRED_CONFIG = ("core.fsmonitor=false", "core.hooksPath=")
GIT_INPUT = b"objeto-de-entrada\n"
FAKE_STDOUT = b"saida-do-duble\n"
IS_WINDOWS = sys.platform == "win32"
SENTINEL_NAMES = ("git.exe", "git.bat", "git.cmd") if IS_WINDOWS else ("git",)
DOUBLE_NAME = "git.exe" if IS_WINDOWS else "git"


def _violations(source: str, modname: str, forbidden: tuple[str, ...]) -> list[tuple[int, str]]:
    return sorted({(line, name) for line, name in _imported_names(source, modname, False)
                   if _is_forbidden(name, forbidden)})


def _server_source(module: str) -> tuple[str, str]:
    files = _module_files(module)
    assert [modname for modname, _, _ in files] == [f"cq_server.{module}"], (
        f"cq_server.{module} não encontrado como arquivo único")
    modname, path, _ = files[0]
    return modname, path.read_text(encoding="utf-8")


def _imports_subprocess(source: str, modname: str) -> bool:
    names = _imported_names(source, modname, modname == "cq_client")
    return any(name.split(".")[0] == "subprocess" for _, name in names)


def _class_node(source: str, name: str) -> ast.ClassDef:
    found = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ClassDef) and node.name == name]
    assert len(found) == 1, f"classe {name} ausente ou repetida"
    return found[0]


def _runner_problems(node: ast.AST) -> list[str]:
    """`which`, `shell=True`, `os.system` e `os.popen` dentro do nó."""
    problems: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr in ("which", "system", "popen"):
            problems.append(f"atributo {child.attr}")
        elif isinstance(child, ast.Name) and child.id == "which":
            problems.append("nome which")
        elif isinstance(child, ast.keyword) and child.arg == "shell":
            if not (isinstance(child.value, ast.Constant) and child.value.value is False):
                problems.append("shell diferente de False")
    return problems


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def _executable(folder: pathlib.Path, name: str) -> pathlib.Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"sentinela: nunca roda\n")
    path.chmod(0o755)
    return path


class _KeptBytes(io.BytesIO):
    """Um `stdin` que guarda o que foi escrito mesmo depois de fechado."""

    def __init__(self) -> None:
        super().__init__()
        self.kept = b""

    def write(self, data: Any) -> int:  # type: ignore[override]
        self.kept += bytes(data)
        return len(data)

    def close(self) -> None:
        pass


class _Recorder:
    """O `subprocess.Popen` dublê: guarda o argv e os argumentos e devolve a saída fixa, sem rodar nada."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def factory(self) -> type:
        recorder = self

        class FakePopen:
            def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                self.args = args
                self.returncode: int | None = None
                self.pid = 4242
                self.stdin = _KeptBytes()
                self.stdout = io.BytesIO(FAKE_STDOUT)
                self.stderr = io.BytesIO(b"")
                self.call = {"args": args, "rest": rest, "kwargs": kwargs, "input": None}
                recorder.calls.append(self.call)

            def __enter__(self) -> FakePopen:
                return self

            def __exit__(self, *exc: Any) -> None:
                self.returncode = 0 if self.returncode is None else self.returncode

            def communicate(self, input: bytes | None = None, timeout: float | None = None) -> tuple[bytes, bytes]:
                self.call["input"] = input if input is not None else (self.stdin.kept or None)
                self.returncode = 0
                return self.stdout.read(), self.stderr.read()

            def wait(self, timeout: float | None = None) -> int:
                if self.call["input"] is None and self.stdin.kept:
                    self.call["input"] = self.stdin.kept
                self.returncode = 0
                return 0

            def poll(self) -> int | None:
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

            def terminate(self) -> None:
                self.returncode = -15

        return FakePopen


def _argv_problems(rest: list[str], bank: str) -> list[str]:
    """Os problemas do argv depois do executável: só pares `-c` da lista fechada e um `-C <pasta>`, antes do
    subcomando, que vem por último e igual ao pedido; cada par obrigatório aparece."""
    problems: list[str] = []
    configs: list[str] = []
    folders: list[str] = []
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        option = rest[index]
        if option in ("-c", "-C") and index + 1 < len(rest):
            (configs if option == "-c" else folders).append(rest[index + 1])
            index += 2
            continue
        problems.append(f"opção global fora da lista fechada: {option!r}")
        index += 1
    if rest[index:] != GIT_ARGS:
        problems.append(f"o subcomando não é o pedido: {rest[index:]!r}")
    if folders != [bank]:
        problems.append(f"o -C não é a pasta, uma vez: {folders!r}")
    problems.extend(f"configuração fora da lista fechada: {value!r}" for value in configs
                    if value not in GIT_REQUIRED_CONFIG)
    problems.extend(f"falta -c {value}" for value in GIT_REQUIRED_CONFIG if value not in configs)
    return problems


def _forbidden_which(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("o GitRunner chamou shutil.which")


@pytest.fixture
def git_path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, pathlib.Path]]:
    """A pasta do banco, os sentinelas e o dublê, com o `PATH` montado: relativa, dentro do banco, o dublê."""
    bank = tmp_path / "banco"
    (bank / "questions").mkdir(parents=True)
    (bank / "infoCourse.json").write_bytes(b'{"name": "SINT 001"}\n')
    for name in SENTINEL_NAMES:
        _executable(tmp_path / "relativo", name)
        _executable(bank / "ferramentas", name)
    double = _executable(tmp_path / "absoluto", DOUBLE_NAME)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["relativo", str(bank / "ferramentas"), str(double.parent)]))
    for key, value in INHERITED_GIT_ENV.items():
        monkeypatch.setenv(key, value)
    yield {"bank": bank, "double": double}


def test_banks_modules_respect_boundaries_and_git_runner_builds_isolated_argv(
        git_path: dict[str, pathlib.Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """As fronteiras dos módulos do banco local e, na mesma função, o `GitRunner` real com o `PATH` montado.

    Estático (já vale no esqueleto): o núcleo `cq_server.banks` sem o proibido (AST, com os controles positivos para
    `socket`, `tarfile`, `subprocess`, `git` e os adaptadores) e, num processo novo, sem nada do proibido
    transitivo; `cq_server.adapters.banks` sem `subprocess`; `cq_client.banks` sem `subprocess`, `click` e
    `cq_server`; só `cq_client.stack_compose` importa `subprocess` no pacote cliente; o `GitRunner` sem `which`,
    sem `shell=True`, sem `os.system` nem `os.popen`.

    O executor: `run(GIT_ARGS, repo=<pasta>, input=..., timeout=30)` chama o processo uma vez, com o argv `[<dublê
    absoluto>, *opções globais, *GIT_ARGS]` (nunca o sentinela relativo nem o de dentro da pasta), em que as opções
    globais, em qualquer ordem e todas antes do subcomando, são só `-C <pasta>` (uma vez) e os pares
    `-c core.fsmonitor=false` e `-c core.hooksPath=` (os dois obrigatórios; nada fora dessa lista), sem shell, com o
    ambiente explícito sem `GIT_*` herdada mais as três do executor, o diretório de trabalho no do dublê (fora da
    pasta) e a entrada dada; devolve a saída do dublê com o código 0.

    Vermelho no esqueleto: `GitRunner.run` lança `NotImplementedError`."""
    modname, source = _server_source("banks")
    assert _violations(source, modname, BANKS_CORE_FORBIDDEN) == [], f"{modname} importa o proibido"
    for snippet, expected in (("import socket", "socket"), ("import tarfile", "tarfile"),
                              ("from subprocess import run", "subprocess"), ("import git", "git"),
                              ("from cq_server.adapters.banks import BankRepoFetcher", "cq_server.adapters.banks"),
                              ("def f():\n    import httpx2\n    return httpx2", "httpx2")):
        assert expected in {name for _, name in _violations(snippet, modname, BANKS_CORE_FORBIDDEN)}, snippet
    loaded = _loaded_by([modname])
    assert modname in loaded
    assert [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)] == []

    files = _module_files("adapters")
    adapter = [(name, path) for name, path, _ in files if name == "cq_server.adapters.banks"]
    assert len(adapter) == 1, "cq_server.adapters.banks não encontrado"
    adapter_source = adapter[0][1].read_text(encoding="utf-8")
    assert not _imports_subprocess(adapter_source, "cq_server.adapters.banks")
    assert _imports_subprocess("import subprocess\n", "cq_server.adapters.banks"), "controle positivo"

    sources = _client_sources()
    client_banks = sources["cq_client.banks"]
    assert not _imports_subprocess(client_banks, "cq_client.banks")
    assert _click_imports(client_banks) == []
    assert _imports_server(ast.parse(client_banks)) == []
    with_subprocess = sorted(name for name, text in sources.items()
                             if _imports_subprocess(text, name))
    assert with_subprocess == ["cq_client.stack_compose"]
    assert (CLIENT_DIR / "stack_compose.py").is_file()

    runner_node = _class_node(sources["cq_client.stack_compose"], "GitRunner")
    assert _runner_problems(runner_node) == []
    assert _runner_problems(ast.parse("import shutil\nshutil.which('git')\n")), "controle positivo: which"
    assert _runner_problems(ast.parse("f(x, shell=True)\n")), "controle positivo: shell"

    folder = str(git_path["bank"])
    # Controles positivos: sem as opções de configuração, com uma fora da lista ou depois do subcomando.
    assert _argv_problems(["-C", folder, *GIT_ARGS], folder), "controle positivo: sem -c"
    assert _argv_problems(["-c", "core.fsmonitor=false", "-c", "core.hooksPath=", "-c", "core.pager=cat",
                           "-C", folder, *GIT_ARGS], folder), "controle positivo: -c fora da lista"
    assert _argv_problems(["-c", "core.fsmonitor=false", "-C", folder, *GIT_ARGS, "-c", "core.hooksPath="],
                          folder), "controle positivo: -c depois do subcomando"
    assert _argv_problems(["-C", folder, "-c", "core.hooksPath=", "-c", "core.fsmonitor=false", *GIT_ARGS],
                          folder) == [], "controle: a ordem das opções globais é livre"

    # O executor real, com o processo interceptado.
    from cq_client.stack import RunResult
    from cq_client.stack_compose import GitRunner

    bank, double = git_path["bank"], git_path["double"]
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder.factory())
    monkeypatch.setattr(shutil, "which", _forbidden_which)
    result = GitRunner().run(GIT_ARGS, repo=str(bank), input=GIT_INPUT, timeout=30.0)

    assert len(recorder.calls) == 1, f"o GitRunner criou {len(recorder.calls)} processos"
    call = recorder.calls[0]
    argv = call["args"]
    assert isinstance(argv, (list, tuple)), "o argv não é uma lista (shell?)"
    argv = [os.fspath(part) for part in argv]
    assert os.path.isabs(argv[0]), "o git do argv não é absoluto"
    assert _norm(argv[0]) == _norm(double), "o git escolhido não é o dublê da entrada absoluta fora do banco"
    assert _argv_problems(argv[1:], str(bank)) == []
    kwargs = call["kwargs"]
    assert not kwargs.get("shell", False), "o GitRunner usou shell"
    assert kwargs.get("executable") in (None, argv[0])

    env = kwargs.get("env")
    assert env is not None, "o GitRunner não passou um ambiente explícito"
    git_vars = {key.upper(): value for key, value in env.items() if key.upper().startswith("GIT_")}
    assert git_vars == GIT_ENV

    cwd = kwargs.get("cwd")
    assert cwd is not None, "o GitRunner não fixou o diretório de trabalho"
    assert _norm(cwd) == _norm(double.parent)
    inside = os.path.commonpath([_norm(cwd), _norm(bank)]) == _norm(bank)
    assert not inside, "o diretório de trabalho fica dentro da pasta do banco"

    assert call["input"] == GIT_INPUT
    assert isinstance(result, RunResult)
    assert (result.returncode, result.stdout) == (0, FAKE_STDOUT)
