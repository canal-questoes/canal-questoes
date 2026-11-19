"""Fronteiras da implantação e do piloto (princípio "Portas e contratos";
onde vivem os scripts de operação; a sonda do e-mail).

Arquivo à parte: `tests/contract/test_boundaries.py`, `test_boundaries_cli.py` e `test_catalog.py` ficam
congelados e sem edição, e os verificadores deles são reusados aqui. Cobre, por AST:
- `cq_client.host_probe` sem `cq_server`, `click` e `subprocess` (também na sonda transitiva, para `click` e
  `cq_server`; a biblioteca padrão fica fora da transitiva, como no CLI);
- `cq_server.mail_probe` sem `smtplib` e sem `cq_server.adapters` (só a porta `MailServerProbe`), também
  na transitiva; o adaptador SMTP continua o único módulo do servidor com `smtplib`;
- nenhum script de `ops/` importa `cq_server`; só `ops/pilot/pilot_metrics.py` importa o `cq_client`, e só `api` e
  `profile`;
- os scripts de host (`cq_backup.py`, `restore.py`, `pull_backups.py`, `init_vm_env.py`, `make_bundle.py`,
  `repo_scan.py`, `export_public.py`, `check_question_refs.py`, `ci_suite.py`, `validate_local.py`,
  `scripts/fetch_age.py`, `scripts/compare_collection.py`) só com a biblioteca padrão (contra
  `sys.stdlib_module_names`) e com `main` atrás de `if __name__ == "__main__"` (nenhuma chamada de `main` fora
  dele); **[I]** o `repo_scan.py`, biblioteca sem `main`, só sem chamada solta no nível do módulo;
- nenhum `shell=True` nem `os.system` em `ops/` (**[I]** e nos dois scripts de `scripts/` listados acima);
- o `cq` sem folha a mais: a árvore do `cq_client.cli` tem só as folhas do catálogo, `LOCAL_COMMANDS` e
  `mcp`, e a folha `doctor` sem opção própria.

Cada verificador tem o seu controle positivo. **O que já passa no esqueleto:** as checagens estáticas já passam no
esqueleto, então ficam na mesma função de um controle positivo do comportamento:
`DockerHostProbe(ScriptedRunner(…), stack_dir, env=…).postgres_mounts("cq-x")` emite só os argv esperados
(`NotImplementedError` até a implementação).
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys

from tests.contract.test_boundaries import REPO, _imported_names, _module_files, _package_modules
from tests.contract.test_boundaries_cli import _click_imports, _client_sources, _loaded_by
from tests.contract.test_catalog import _cli_leaves, _imports_server

OPS_DIR = REPO / "ops"
# Os scripts que rodam no host com o `python3` do sistema.
HOST_SCRIPTS = (
    "ops/backup/cq_backup.py", "ops/backup/restore.py", "ops/backup/pull_backups.py", "ops/deploy/init_vm_env.py",
    "ops/deploy/make_bundle.py", "ops/export/repo_scan.py", "ops/export/export_public.py",
    "ops/pilot/check_question_refs.py", "ops/ci/ci_suite.py", "ops/ci/validate_local.py", "scripts/fetch_age.py",
    "scripts/compare_collection.py",
)
PILOT_METRICS = "ops/pilot/pilot_metrics.py"
# [I] O `repo_scan.py` é biblioteca (o `export_public.py` e o `make_bundle.py` o carregam por caminho) e não tem
# `main`: dele se exige só que importá-lo não rode nada (nenhuma chamada solta no nível do módulo).
LIBRARY_SCRIPTS = frozenset({"ops/export/repo_scan.py"})
NEW_SCRIPTS = ("scripts/fetch_age.py", "scripts/compare_collection.py")


def _ops_scripts() -> list[str]:
    return sorted(path.relative_to(REPO).as_posix() for path in OPS_DIR.rglob("*.py"))


def _source(relpath: str) -> str:
    return (REPO / relpath).read_text(encoding="utf-8")


def _top_imports(source: str) -> list[str]:
    """O módulo de topo de cada importação (inclusive a dinâmica por nome literal); a relativa vira `.<nome>`, que
    nunca é da biblioteca padrão (um script carregado por caminho não tem pacote)."""
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            found.append("." + (node.module or ""))
    for _, name in _imported_names(source, "script", False):
        if name.startswith("."):
            continue
        found.append(name.split(".")[0])
    return [name for name in found if name]


def _non_stdlib(source: str) -> list[str]:
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    return sorted({name for name in _top_imports(source) if name not in allowed})


def _client_imports(source: str) -> set[str]:
    """Os módulos do `cq_client` importados (`import cq_client.x` ou `from cq_client.x import …`)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names if alias.name.split(".")[0] == "cq_client"}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module \
                and node.module.split(".")[0] == "cq_client":
            found.add(node.module)
    return found


def _is_main_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"
            and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
            and len(node.test.comparators) == 1 and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__")


def _main_problems(source: str) -> list[str]:
    """`main` definido no módulo, chamado dentro de um `if __name__ == "__main__"` e em nenhum outro lugar do nível
    do módulo (importar o script não roda nada)."""
    tree = ast.parse(source)
    problems: list[str] = []
    if not any(isinstance(n, ast.FunctionDef) and n.name == "main" for n in tree.body):
        problems.append("sem main")
    guards = [n for n in tree.body if _is_main_guard(n)]
    if not any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "main"
               for g in guards for c in ast.walk(g)):
        problems.append("sem if __name__ == '__main__' chamando main")
    for node in tree.body:
        if _is_main_guard(node) or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "main"
               for c in ast.walk(node)):
            problems.append(f"main chamado fora do guarda na linha {node.lineno}")
    return problems


def _loose_calls(source: str) -> list[int]:
    """As linhas de chamadas soltas no nível do módulo (um `Expr` com `Call`), que rodariam ao importar."""
    return [node.lineno for node in ast.parse(source).body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)]


def _shell_calls(source: str) -> list[int]:
    """As linhas com `shell=True` em qualquer chamada ou com `os.system(...)`."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if any(k.arg == "shell" and not (isinstance(k.value, ast.Constant) and k.value.value is False)
               for k in node.keywords):
            lines.append(node.lineno)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "system" and isinstance(func.value, ast.Name) \
                and func.value.id == "os":
            lines.append(node.lineno)
        if isinstance(func, ast.Name) and func.id == "system":
            lines.append(node.lineno)
    return sorted(set(lines))


def _server_source(module: str) -> tuple[str, str]:
    files = _module_files(module)
    assert [m for m, _, _ in files] == [f"cq_server.{module}"], f"cq_server.{module} não é um arquivo único"
    modname, path, _ = files[0]
    return modname, path.read_text(encoding="utf-8")


def _imports_of(source: str, modname: str) -> set[str]:
    return {name for _, name in _imported_names(source, modname, False)}


def _touches(names: set[str], prefix: str) -> bool:
    return any(n == prefix or n.startswith(prefix + ".") for n in names)


def test_deploy_modules_and_ops_scripts_respect_boundaries_and_host_probe_emits_only_expected_argv(
        tmp_path: pathlib.Path) -> None:
    """Portas e contratos. As checagens estáticas (já valem no esqueleto) e, na mesma função, o controle
    positivo do `DockerHostProbe` (vermelho por `NotImplementedError` até a implementação)."""
    # --- cq_client.host_probe: sem cq_server, click e subprocess ------------------------------------------------
    sources = _client_sources()
    host_probe = sources["cq_client.host_probe"]
    assert _imports_server(ast.parse(host_probe)) == [], "cq_client.host_probe importa cq_server"
    assert _click_imports(host_probe) == [], "cq_client.host_probe importa click"
    assert not _touches(_imports_of(host_probe, "cq_client.host_probe"), "subprocess"), (
        "cq_client.host_probe importa subprocess (o executor é o CommandRunner do CLI)")
    assert _touches(_imports_of("import subprocess\n", "cq_client.host_probe"), "subprocess"), "controle positivo"
    loaded = _loaded_by(["cq_client.host_probe"])
    assert "cq_client.host_probe" in loaded and "click" not in loaded
    assert [m for m in loaded if m == "cq_server" or m.startswith("cq_server.")] == []

    # --- cq_server.mail_probe: sem smtplib e sem cq_server.adapters ---------------------------------------
    modname, mail_probe = _server_source("mail_probe")
    names = _imports_of(mail_probe, modname)
    assert not _touches(names, "smtplib"), "cq_server.mail_probe importa smtplib"
    assert not _touches(names, "cq_server.adapters"), "cq_server.mail_probe importa um adaptador"
    assert _touches(names, "cq_server.ports.verification"), "cq_server.mail_probe sem a porta MailServerProbe"
    assert _touches(_imports_of("from .adapters.smtp import X\n", modname), "cq_server.adapters"), "controle positivo"
    loaded = _loaded_by([modname])
    assert modname in loaded
    assert [m for m in loaded if m == "smtplib" or m.startswith("cq_server.adapters")] == [], (
        "importar cq_server.mail_probe carregou smtplib ou um adaptador")
    with_smtplib = sorted(m for m, path in _package_modules()
                          if _touches(_imports_of(path.read_text(encoding="utf-8"), m), "smtplib"))
    assert with_smtplib == ["cq_server.adapters.smtp"], with_smtplib

    # --- ops/: nenhum cq_server; só pilot_metrics com o cq_client (api e profile) --------------------------------
    scripts = _ops_scripts()
    assert set(HOST_SCRIPTS) - set(NEW_SCRIPTS) | {PILOT_METRICS} <= set(scripts), "script de ops/ ausente"
    for relpath in scripts:
        assert _imports_server(ast.parse(_source(relpath))) == [], f"{relpath} importa cq_server"
    with_client = {relpath: _client_imports(_source(relpath)) for relpath in scripts}
    assert {r for r, mods in with_client.items() if mods} == {PILOT_METRICS}, with_client
    assert with_client[PILOT_METRICS] == {"cq_client.api", "cq_client.profile"}
    assert _client_imports("from cq_client.doctor import Check\n") == {"cq_client.doctor"}, "controle positivo"

    # --- scripts de host: só a biblioteca padrão; main atrás do guarda -----------------------------------------
    for relpath in HOST_SCRIPTS:
        source = _source(relpath)
        assert _non_stdlib(source) == [], f"{relpath} importa fora da biblioteca padrão: {_non_stdlib(source)}"
        if relpath in LIBRARY_SCRIPTS:
            assert _loose_calls(source) == [], f"{relpath}: chamada solta no nível do módulo"
        else:
            assert _main_problems(source) == [], f"{relpath}: {_main_problems(source)}"
    assert _loose_calls('"""doc."""\nimport re\nX = re.compile("a")\nprint(X)\n') == [4], "controle positivo"
    assert _non_stdlib("import httpx2\nfrom cq_client import api\nfrom . import x\nimport json\n") == [
        ".", "cq_client", "httpx2"], "controle positivo do verificador da biblioteca padrão"
    assert _main_problems("def main():\n    return 0\nmain()\n") == [
        "sem if __name__ == '__main__' chamando main", "main chamado fora do guarda na linha 3"]
    assert _main_problems('def main():\n    return 0\nif __name__ == "__main__":\n    raise SystemExit(main())\n') == []

    # --- nenhum shell=True nem os.system ------------------------------------------------------------------------
    for relpath in [*scripts, *NEW_SCRIPTS]:
        assert _shell_calls(_source(relpath)) == [], f"{relpath}: shell=True ou os.system"
    assert _shell_calls("import os, subprocess\nsubprocess.run('x', shell=True)\nos.system('y')\n") == [2, 3]
    assert _shell_calls("import subprocess\nsubprocess.run(['x'], shell=False)\n") == []

    # --- o cq sem folha a mais ------------------------------------------------------------------
    from cq_client.cli import MCP_COMMAND, cli
    from cq_client.local_commands import LOCAL_COMMANDS
    from tests.helpers.contract import load_catalog

    leaves = _cli_leaves(cli)
    assert set(leaves) == {op["cli"] for op in load_catalog()["operations"]} | LOCAL_COMMANDS | {MCP_COMMAND}
    assert leaves["doctor"] == list(cli.params), "a folha doctor ganhou opção própria"

    # --- controle positivo: o DockerHostProbe emite só os argv esperados ----------------------------------
    from cq_client.host_probe import DockerHostProbe, Mount
    from cq_client.stack import compose_prefix

    from tests.helpers.host_probe_fake import ScriptedRunner, result

    stack_dir = tmp_path / "pilha"
    ps = (*compose_prefix("cq-x", stack_dir), "ps", "-q", "postgres")
    inspect = ("docker", "inspect", "--format", "{{json .Mounts}}", "0123456789ab")
    mounts = [{"Type": "volume", "Name": "cq-x_pgdata", "Source": "/x", "Destination": "/var/lib/postgresql"}]
    runner = ScriptedRunner({ps: result("0123456789ab\n"), inspect: result(json.dumps(mounts))})
    probe = DockerHostProbe(runner, stack_dir, env={"PATH": "/usr/bin"})
    assert probe.postgres_mounts("cq-x") == (Mount("volume", "cq-x_pgdata", "/var/lib/postgresql"),)
    assert runner.argvs == [list(ps), list(inspect)]
