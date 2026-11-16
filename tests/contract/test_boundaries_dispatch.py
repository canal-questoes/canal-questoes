"""Fronteiras do núcleo no disparo e na resposta (princípio "Portas e contratos"; a recepção, a
fila e o worker; a leitura de identidades só pelos leitores
autorizados, a entrada simulada do console, o canal passado aos tratadores, o formatador simples fora
do núcleo e a configuração de entrega até os tratadores).

Arquivo à parte: `tests/contract/test_boundaries.py` fica congelado e sem edição, e as checagens dele
são reusadas aqui (`_module_files`, `_violations`, `_imported_names`, `_is_forbidden`,
`TRANSITIVE_FORBIDDEN`, `_identities_sql`). Cobre:
- os 12 módulos do núcleo do disparo e da resposta (`questionrefs`, `dispatches`, `deliveries`, `answers`, `grading`,
  `formaterrors`, `freemessages`, `scheduler`, `telegramrender`, `buttons`, `pacing`, `deliverytexts`) e as
  portas (inclusive `ports/engine.py` e `ports/repos.py`) não importam HTTP, `subprocess`, `socket` nem
  adaptadores, pela AST (direto) e num processo novo (transitivo); controle positivo: `adapters/engine.py`
  importa `httpx2` e `adapters/git.py` importa `subprocess`, e a checagem os acusa, inclusive se entrassem
  no núcleo;
- o domínio não importa `cq_server.telegramrender` (só `bots`, que o expõe como `formatter`);
- por AST, os módulos de domínio do disparo (`dispatches`, `deliveries`, `answers`, `grading`, `freemessages`,
  `scheduler`, `questionrefs`) não citam `TelegramChannel` nem `ConsoleChannel` e não testam o tipo do
  canal (`isinstance`, `issubclass`, `type(...)`), com controle positivo sintético;
- nenhum dos 12 módulos, nem as portas e os adaptadores do motor e da obtenção, cita `identities` em SQL;
- por AST, `cq_server.__main__`, `cq_server.api` e `cq_client` não citam `ConsoleInbox` nem
  `store_console_updates`, o `__main__` (e nele o `_cmd_worker`) não passa `console_routes`, e só
  `cq_server.adapters.git` cita `GitTestOptions` (o `build_repo_fetcher` não tem caminho para ele).

**Além da ausência do proibido, a fronteira de verdade** (para que
nenhum teste passe no esqueleto): a raiz de composição e o worker, em execução:
- `cq_server.__main__.build_engine` e `build_repo_fetcher` devolvem os adaptadores reais
  (`HttpQuestionEngine`, `GitRepoFetcher`, sem `GitTestOptions`), e só eles carregam os módulos dos
  adaptadores (importar o núcleo e o worker não os carrega); o `_cmd_worker` passa ao `run_worker` o que
  eles devolvem, sem `console_routes`;
- o `run_worker` registra os tratadores das 8 tarefas do disparo e da resposta (`dispatch.validate`, `dispatch.expire`,
  `dispatch.open`, `dispatch.close`, `delivery.prepare`, `delivery.reveal`, `submission.grade`,
  `question.refetch`) no caminho `console_routes=True` só pelas portas: com um motor e uma obtenção que só
  cumprem as portas, ou sem eles (`unconfigured`), sem montar adaptador nenhum, e com um canal que só
  cumpre a porta `DeliveryChannel`, sem ler o `formatter` na subida; sem `bots` e sem
  `console_routes`, não registra nenhum deles;
- o `TelegramChannel` de `bots` expõe os formatadores de `telegramrender` (`formatter`, `formatter_for`).

Falha esperada no esqueleto: `build_engine` e `build_repo_fetcher` lançam
`NotImplementedError`, o `run_worker` ainda não registra os tratadores do disparo (contrato não
cumprido até as tarefas que os registram) e `TelegramChannel.formatter` lança
`NotImplementedError`. As checagens estruturais (que já valem no esqueleto) ficam na mesma função
de um desses casos.
"""

from __future__ import annotations

import ast
import io
import json
import os
import pathlib
import subprocess
import sys
import threading
from collections.abc import Mapping
from typing import Any

import pytest

from tests.contract.test_boundaries import (
    CORE_MODULES,
    FORBIDDEN,
    REPO,
    TRANSITIVE_FORBIDDEN,
    _identities_sql,
    _imported_names,
    _is_forbidden,
    _module_files,
    _package_dir,
    _violations,
)

# Os módulos do núcleo do disparo e da resposta.
DISPATCH_CORE_MODULES = ("questionrefs", "dispatches", "deliveries", "answers", "grading", "formaterrors",
                   "freemessages", "scheduler", "telegramrender", "buttons", "pacing", "deliverytexts")
# Os módulos de domínio do disparo: falam com o canal só pela porta `DeliveryChannel`.
DISPATCH_DOMAIN_MODULES = ("dispatches", "deliveries", "answers", "grading", "freemessages", "scheduler",
                     "questionrefs")
# Direto (AST): além do congelado, `socket` (a rede só nos adaptadores:
# `cq_server.adapters` é o único lugar com `httpx2`, `subprocess`, `socket` e `git`).
DISPATCH_FORBIDDEN = FORBIDDEN + ("socket",)
TELEGRAMRENDER = "cq_server.telegramrender"
CONCRETE_CHANNELS = {"TelegramChannel", "ConsoleChannel"}
CONSOLE_INPUT = {"ConsoleInbox", "store_console_updates"}
NEW_ADAPTERS = ("cq_server.adapters.engine", "cq_server.adapters.git")
# Configuração mínima da raiz de composição: o motor e a obtenção configurados.
ENGINE_URL = "http://motor.cq.test:8080"
ALLOWED_HOSTS = "repos.cq.test"


def _dispatch_task_types() -> set[str]:
    """As 8 tarefas do disparo e da resposta que o `run_worker` registra."""
    from cq_server import deliveries, dispatches, grading

    return {dispatches.VALIDATE_TASK_TYPE, dispatches.EXPIRE_TASK_TYPE, dispatches.OPEN_TASK_TYPE,
            dispatches.CLOSE_TASK_TYPE, deliveries.PREPARE_TASK_TYPE, deliveries.REVEAL_TASK_TYPE,
            grading.GRADE_TASK_TYPE, grading.REFETCH_TASK_TYPE}


def _dispatch_violations(source: str, modname: str, is_pkg: bool = False) -> list[tuple[int, str]]:
    return sorted({(line, name) for line, name in _imported_names(source, modname, is_pkg)
                   if _is_forbidden(name, DISPATCH_FORBIDDEN)})


def _loaded_by(modules: list[str]) -> list[str]:
    """`sys.modules` depois de importar `modules` num processo novo."""
    code = ("import importlib, json, sys\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    importlib.import_module(m)\n"
            "print(json.dumps(sorted(sys.modules)))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"}
    result = subprocess.run([sys.executable, "-c", code, json.dumps(modules)], cwd=REPO, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def _identifiers(source: str) -> set[str]:
    """Todo identificador citado pelo código (nomes, atributos, importações, parâmetros, argumentos
    nomeados, definições) e todo literal de texto que é um identificador (ex.: `getattr(m, "X")` ou a
    chave `"x"` de um `**{"x": …}`); docstrings e comentários com espaço não contam."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, (ast.keyword, ast.arg)) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            names.add(node.value)
    return names


def _expr_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _channel_offenses(source: str) -> list[str]:
    """Citações de um canal concreto e testes do tipo do canal (princípio "Portas e contratos": o núcleo decide pelas
    capacidades e escreve pelo `formatter`, nunca pelo tipo do canal)."""
    found: list[str] = []
    tree = ast.parse(source)
    for name in sorted(_identifiers(source) & CONCRETE_CHANNELS):
        found.append(f"cita {name}")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func.id if isinstance(node.func, ast.Name) else (
            node.func.attr if isinstance(node.func, ast.Attribute) else "")
        if func in ("isinstance", "issubclass") and node.args:
            subject = _expr_names(node.args[0])
            kinds = _expr_names(node.args[1]) if len(node.args) > 1 else set()
            if any("channel" in n.lower() for n in subject) or any(n.endswith("Channel") for n in kinds):
                found.append(f"{func} sobre o canal (linha {node.lineno})")
        elif func == "type" and len(node.args) == 1:
            if any("channel" in n.lower() for n in _expr_names(node.args[0])):
                found.append(f"type() do canal (linha {node.lineno})")
    return found


def _package_sources(package_dir: pathlib.Path, prefix: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(package_dir.rglob("*.py")):
        parts = [prefix, *path.relative_to(package_dir).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path.read_text(encoding="utf-8")
    return out


# --- a raiz de composição e o worker em execução ------------------------------------------------


class _PortEngine:
    """Um motor que só cumpre a porta `QuestionEngine` (nenhum adaptador); nada o chama na subida."""

    def inspect(self, ref: Any) -> Any:
        raise AssertionError("o worker chamou o motor na subida")

    def create_variant(self, ref: Any, seed: str) -> Any:
        raise AssertionError("o worker chamou o motor na subida")

    def grade(self, ref: Any, snapshot: Any, raw_answers: Any) -> Any:
        raise AssertionError("o worker chamou o motor na subida")

    def close(self) -> None:
        return None


class _PortFetcher:
    """Uma obtenção que só cumpre a porta `RepoFetcher`; nada a chama na subida."""

    def ensure(self, repo: str, commit: str) -> Any:
        raise AssertionError("o worker obteve um repositório na subida")


class _PortChannel:
    """Um canal que só cumpre a porta `DeliveryChannel` (nem Telegram nem console) e registra quem lê o
    formatador (o `run_worker` não o lê na subida)."""

    def __init__(self) -> None:
        from cq_server.adapters.console import CONSOLE_CAPABILITIES

        self.capabilities = CONSOLE_CAPABILITIES
        self.reads: list[str] = []

    @property
    def formatter(self) -> Any:
        self.reads.append("formatter")
        raise AssertionError("o run_worker leu o formatter na subida")

    def formatter_for(self, text_format: str) -> Any:
        self.reads.append(f"formatter_for({text_format})")
        raise AssertionError("o run_worker leu o formatter_for na subida")

    def send(self, message: Any) -> Any:
        raise AssertionError("nenhuma mensagem sai na subida")


def _forbid_adapter_construction(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Troca os construtores dos adaptadores do motor e da obtenção por um que registra a montagem: o worker e o domínio
    só recebem o motor e a obtenção prontos (a raiz de composição é o `__main__`)."""
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.adapters.git import GitRepoFetcher

    built: list[str] = []

    def forbidden(self: Any, *args: Any, **kwargs: Any) -> None:
        built.append(type(self).__name__)
        raise AssertionError("adaptador montado fora da raiz de composição")

    monkeypatch.setattr(HttpQuestionEngine, "__init__", forbidden)
    monkeypatch.setattr(GitRepoFetcher, "__init__", forbidden)
    return built


def _registered_handlers(monkeypatch: pytest.MonkeyPatch, settings: Any, **kwargs: Any) -> dict[str, Any]:
    """Os tratadores que o `run_worker(settings, …, **kwargs)` passa à fila de tarefas: a primeira
    `TaskQueue.work_once` os captura e pede a parada (sem executar tarefa nenhuma); a espera do esquema
    é dispensada (o que se confere aqui é a montagem, não o banco)."""
    from cq_server import worker
    from cq_server.tasks import TaskQueue

    captured: list[dict[str, Any]] = []
    stop = threading.Event()

    def work_once(self: Any, worker_id: str, handlers: Mapping[str, Any]) -> None:
        captured.append(dict(handlers))
        stop.set()
        return None

    monkeypatch.setattr(worker, "_wait_for_schema", lambda settings, stop: True)
    monkeypatch.setattr(TaskQueue, "work_once", work_once)
    failure: list[BaseException] = []

    def target() -> None:
        try:
            worker.run_worker(settings, stop=stop, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - relançada na thread do teste
            failure.append(exc)

    thread = threading.Thread(target=target, name="cq-test-boundaries-worker")
    thread.start()
    thread.join(30)
    stop.set()
    thread.join(10)
    assert not thread.is_alive(), "o run_worker não parou"
    if failure:
        raise failure[0]
    assert captured, "o run_worker não chegou à fila de tarefas"
    return captured[0]


def _build_env(repos_root: pathlib.Path) -> dict[str, str]:
    return {"CQ_ENGINE_URL": ENGINE_URL, "CQ_REPOS_ROOT": str(repos_root),
            "CQ_REPO_ALLOWED_HOSTS": ALLOWED_HOSTS}


# ---------------------------------------------------------------------------------------------
# Núcleo sem HTTP, processos, rede nem adaptadores; motor e obtenção só pelas portas
# ---------------------------------------------------------------------------------------------


def test_dispatch_core_modules_import_nothing_forbidden_and_get_engine_and_fetcher_only_by_ports(
        monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    """Portas e contratos; leitura de identidades só pelos leitores autorizados; estrutura do disparo e
    da resposta: a recepção, a fila e o worker.

    Estrutura: cada módulo do núcleo do disparo (e as portas, com `ports/engine.py` e `ports/repos.py`)
    não importa HTTP, `subprocess`, `socket`, a API nem os adaptadores (checagem por AST, com as formas
    de importação do congelado); nenhum desses módulos, das portas do motor e da obtenção nem dos adaptadores deles
    cita a tabela `identities` em SQL (`FROM`, `JOIN`, `INTO`, `UPDATE`; a redação usa
    `events.load_write_identities`/`load_known_identities`), com controle positivo sobre um texto
    sintético.

    Em execução: o `run_worker(…, console_routes=True)` registra os tratadores das 8 tarefas do disparo (e o
    `telegram.update`) recebendo um motor e uma obtenção que só cumprem as portas, e também sem eles
    (motor ou obtenção ausentes → `unconfigured`/`repos_unavailable` na execução), sem montar
    `HttpQuestionEngine` nem `GitRepoFetcher` e sem chamar o motor nem a obtenção na subida; sem `bots`
    e sem `console_routes`, só o `invite.email` do vínculo do aluno (nenhum tratador do disparo)."""
    problems: dict[str, list[str]] = {}
    for module in (*DISPATCH_CORE_MODULES, "ports"):
        files = _module_files(module)
        assert files, f"módulo do núcleo cq_server.{module} não encontrado (a estrutura do repositório)"
        if module == "ports":
            assert {"cq_server.ports.engine", "cq_server.ports.repos"} <= {m for m, _, _ in files}
        found = [f"{path.relative_to(_package_dir().parent)}:{line} importa {name}"
                 for modname, path, is_pkg in files
                 for line, name in _dispatch_violations(path.read_text(encoding="utf-8"), modname, is_pkg)]
        if found:
            problems[module] = found
    assert problems == {}

    sources: dict[str, str] = {}
    for name in DISPATCH_CORE_MODULES:
        for modname, path, _ in _module_files(name):
            sources[modname] = path.read_text(encoding="utf-8")
    for modname, path, _ in (*_module_files("ports"), *_module_files("adapters")):
        if modname in ("cq_server.ports.engine", "cq_server.ports.repos", *NEW_ADAPTERS):
            sources[modname] = path.read_text(encoding="utf-8")
    assert len(sources) == len(DISPATCH_CORE_MODULES) + 4, sorted(sources)
    assert {modname: hits for modname, source in sources.items() if (hits := _identities_sql(source))} == {}
    assert _identities_sql('cur.execute("SELECT full_name FROM identities WHERE person_id = %s")') == [
        "FROM identities"]

    from cq_server import reception, students
    from cq_server.adapters.console import ConsoleChannel

    built = _forbid_adapter_construction(monkeypatch)
    expected = _dispatch_task_types()
    r4_only = _registered_handlers(monkeypatch, settings, channel=ConsoleChannel(io.StringIO()),
                                   engine=_PortEngine(), fetcher=_PortFetcher())
    assert set(r4_only) == {students.INVITE_EMAIL_TASK_TYPE}
    with_ports = _registered_handlers(monkeypatch, settings, channel=ConsoleChannel(io.StringIO()),
                                      engine=_PortEngine(), fetcher=_PortFetcher(), console_routes=True)
    assert sorted(expected - set(with_ports)) == [], "tratadores novos ausentes do run_worker"
    assert reception.UPDATE_TASK_TYPE in with_ports
    without = _registered_handlers(monkeypatch, settings, channel=ConsoleChannel(io.StringIO()),
                                   console_routes=True)
    assert sorted(expected - set(without)) == [], "tratadores novos ausentes do run_worker"
    assert built == []


def test_dispatch_core_modules_do_not_load_http_or_adapters_transitively(tmp_path: pathlib.Path) -> None:
    """Portas e contratos: importar os módulos do núcleo do disparo num processo novo não carrega frameworks
    HTTP (`httpx*`, `httpcore*`), a API, `smtplib` nem os adaptadores (inclusive `cq_server.adapters.engine`
    e `cq_server.adapters.git`), nem por dependência indireta. Raiz de composição (a recepção, a
    fila e o worker): num processo novo, o worker (`cq_server.worker`) e o `cq_server.__main__`
    também não carregam os adaptadores do motor e da obtenção; só `build_engine` e `build_repo_fetcher` os carregam, e
    devolvem os objetos deles."""
    modules = [modname for name in (*DISPATCH_CORE_MODULES, "ports") for modname, _, _ in _module_files(name)]
    assert len(modules) >= len(DISPATCH_CORE_MODULES) + 2
    loaded = _loaded_by(modules)
    assert set(modules) <= set(loaded)
    assert [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)] == []

    code = ("import json, sys\n"
            "import cq_server.worker, cq_server.__main__ as main\n"
            "from cq_server.config import Settings\n"
            f"adapters = {list(NEW_ADAPTERS)!r}\n"
            "before = sorted(m for m in adapters if m in sys.modules)\n"
            "settings = Settings(database_url='postgresql://cq-fronteiras.invalid/cq')\n"
            "env = json.loads(sys.argv[1])\n"
            "engine = main.build_engine(env, settings)\n"
            "fetcher = main.build_repo_fetcher(env, settings)\n"
            "after = sorted(m for m in adapters if m in sys.modules)\n"
            "kinds = [f'{type(o).__module__}.{type(o).__name__}' for o in (engine, fetcher)]\n"
            "print(json.dumps({'before': before, 'after': after, 'kinds': kinds}))\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP" and not k.startswith("CQ_")}
    result = subprocess.run([sys.executable, "-c", code, json.dumps(_build_env(tmp_path))], cwd=REPO,
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"before": [], "after": sorted(NEW_ADAPTERS),
                      "kinds": ["cq_server.adapters.engine.HttpQuestionEngine",
                                "cq_server.adapters.git.GitRepoFetcher"]}


def test_scanner_flags_the_real_engine_and_git_adapters(tmp_path: pathlib.Path) -> None:
    """Controle positivo sobre o código real (o adaptador do motor
    fala HTTP por `httpx2` e o da obtenção roda o `git` por `subprocess`): `cq_server.adapters.engine`
    importa `httpx2` e `cq_server.adapters.git` importa `subprocess`, e a checagem os acusa com o nome
    deles e se entrassem no núcleo (lidos como `cq_server.dispatches` e `cq_server.grading`); importá-los
    num processo novo carrega módulos que a checagem transitiva acusa. Controle positivo sintético: cada
    forma de trazer esses adaptadores, `subprocess` ou `socket` para um módulo do núcleo é acusada.

    Raiz de composição: os adaptadores acusados são exatamente os
    objetos que `cq_server.__main__.build_engine` e `build_repo_fetcher` devolvem com o motor e a obtenção
    configurados (sem ler `os.environ`)."""
    adapters = {modname: path for modname, path, _ in _module_files("adapters")}
    engine_source = adapters["cq_server.adapters.engine"].read_text(encoding="utf-8")
    git_source = adapters["cq_server.adapters.git"].read_text(encoding="utf-8")
    assert "httpx2" in {name for _, name in _dispatch_violations(engine_source, "cq_server.adapters.engine")}
    assert "httpx2" in {name for _, name in _dispatch_violations(engine_source, "cq_server.dispatches")}
    assert "subprocess" in {name for _, name in _dispatch_violations(git_source, "cq_server.adapters.git")}
    assert "subprocess" in {name for _, name in _dispatch_violations(git_source, "cq_server.grading")}
    loaded = _loaded_by(["cq_server.adapters.engine", "cq_server.adapters.git"])
    flagged = {m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)}
    assert {"httpx2", "cq_server.adapters.engine", "cq_server.adapters.git"} <= flagged, sorted(flagged)

    for source, modname, expected in (
        ("from .adapters.engine import HttpQuestionEngine", "cq_server.dispatches", "cq_server.adapters.engine"),
        ("from cq_server.adapters import git", "cq_server.grading", "cq_server.adapters.git"),
        ("import cq_server.adapters.git", "cq_server.questionrefs", "cq_server.adapters.git"),
        ("from ..adapters.engine import HttpQuestionEngine", "cq_server.ports.engine", "cq_server.adapters.engine"),
        ("import subprocess", "cq_server.questionrefs", "subprocess"),
        ("from subprocess import run", "cq_server.grading", "subprocess"),
        ("import socket", "cq_server.pacing", "socket"),
        ("from socket import getaddrinfo", "cq_server.questionrefs", "socket"),
        ("import importlib\nimportlib.import_module('socket')", "cq_server.scheduler", "socket"),
        ("def f():\n    import httpx2\n    return httpx2", "cq_server.deliveries", "httpx2"),
        ("import httpcore2", "cq_server.answers", "httpcore2"),
    ):
        assert expected in {name for _, name in _dispatch_violations(source, modname)}, source
    for source, modname in (
        ("from cq_server.ports.engine import QuestionEngine\nfrom cq_server.ports.repos import RepoFetcher",
         "cq_server.dispatches"),
        ("import hashlib\nimport secrets\nimport time", "cq_server.pacing"),
        ("from .ports.delivery import Formatted", "cq_server.telegramrender"),
    ):
        assert _dispatch_violations(source, modname) == [], source

    from cq_server import __main__ as main
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.adapters.git import GitRepoFetcher
    from cq_server.config import Settings

    settings = Settings(database_url="postgresql://cq-fronteiras.invalid/cq")
    env = _build_env(tmp_path)
    engine = main.build_engine(env, settings)
    fetcher = main.build_repo_fetcher(env, settings)
    try:
        assert type(engine) is HttpQuestionEngine
        assert type(fetcher) is GitRepoFetcher
        assert {type(engine).__module__, type(fetcher).__module__} == set(NEW_ADAPTERS)
    finally:
        if engine is not None:
            engine.close()


# ---------------------------------------------------------------------------------------------
# O domínio não importa o renderizador nem conhece o canal concreto
# ---------------------------------------------------------------------------------------------


def _imports_telegramrender(source: str, modname: str, is_pkg: bool = False) -> list[str]:
    return sorted({name for _, name in _imported_names(source, modname, is_pkg)
                   if name == TELEGRAMRENDER or name.startswith(TELEGRAMRENDER + ".")})


def test_domain_does_not_import_telegramrender() -> None:
    """Portas e contratos; formatador fora do domínio (`telegramrender` só é usado pelo
    `TelegramChannel`, em `bots`, que o expõe como `formatter`; o domínio nunca o importa): nenhum módulo
    do núcleo, fora `bots` e o próprio `telegramrender`, importa `cq_server.telegramrender`.
    Controle positivo: cada forma de importá-lo num módulo de domínio é acusada.
    Em execução: o `TelegramChannel` expõe os formatadores de `telegramrender` pelo `formatter`
    (`rich` → `TelegramRichFormatter`, `html` → `TelegramHtmlFormatter`) e pelo `formatter_for` (`plain`
    → `PlainFormatter`; outro dialeto → `ValueError`), sem tocar o banco."""
    modules = [m for m in (*CORE_MODULES, *DISPATCH_CORE_MODULES) if m not in ("bots", "telegramrender")]
    problems = {modname: hits for name in modules for modname, path, is_pkg in _module_files(name)
                if (hits := _imports_telegramrender(path.read_text(encoding="utf-8"), modname, is_pkg))}
    assert problems == {}
    for source, modname in (
        ("from cq_server.telegramrender import PlainFormatter", "cq_server.deliveries"),
        ("from . import telegramrender", "cq_server.answers"),
        ("from .telegramrender import TelegramRichFormatter", "cq_server.dispatches"),
        ("import cq_server.telegramrender as render", "cq_server.grading"),
        ("def f():\n    from .telegramrender import TelegramHtmlFormatter\n    return 1", "cq_server.scheduler"),
        ("from ..telegramrender import PlainFormatter", "cq_server.ports.delivery"),
    ):
        assert _imports_telegramrender(source, modname) != [], source

    from cq_server import telegramrender
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.bots import TelegramChannel
    from cq_server.clock import SystemClock
    from cq_server.config import DeliverySettings

    def channel(telegram_format: str) -> TelegramChannel:
        return TelegramChannel("postgresql://cq-fronteiras.invalid/cq", clock=SystemClock(), runtime=None,
                               default=ConsoleChannel(io.StringIO()),
                               delivery=DeliverySettings(telegram_format=telegram_format))

    rich, html = channel("rich"), channel("html")
    assert type(rich.formatter) is telegramrender.TelegramRichFormatter
    assert type(html.formatter) is telegramrender.TelegramHtmlFormatter
    assert [type(rich.formatter_for(f)) for f in ("rich", "html", "plain")] == [
        telegramrender.TelegramRichFormatter, telegramrender.TelegramHtmlFormatter,
        telegramrender.PlainFormatter]
    with pytest.raises(ValueError):
        rich.formatter_for("markdown")


_CHANNEL_SCAN_CASES: list[tuple[str, list[str]]] = [
    ("from cq_server.bots import TelegramChannel", ["cita TelegramChannel"]),
    ("from cq_server.adapters.console import ConsoleChannel as C", ["cita ConsoleChannel"]),
    ("def f(channel):\n    return isinstance(channel, object)", ["isinstance sobre o canal (linha 2)"]),
    ("def f(ch, DeliveryChannel):\n    return isinstance(ch, DeliveryChannel)",
     ["isinstance sobre o canal (linha 2)"]),
    ("def f(runtime):\n    return isinstance(runtime.channel, tuple)", ["isinstance sobre o canal (linha 2)"]),
    ("def f(channel):\n    return type(channel).__name__", ["type() do canal (linha 2)"]),
    ("def f(cls, BaseChannel):\n    return issubclass(cls, BaseChannel)", ["issubclass sobre o canal (linha 2)"]),
    ("def f(m):\n    return getattr(m, 'TelegramChannel')", ["cita TelegramChannel"]),
    ("def f(channel):\n    return channel.capabilities.max_buttons > 0", []),
    ("def f(channel, text_format):\n    return channel.formatter_for(text_format)", []),
    ('"""O TelegramChannel é do bots; aqui só a porta."""\nx = 1', []),
    ("def f(value):\n    return isinstance(value, dict)", []),
]


def test_domain_modules_do_not_name_or_test_the_concrete_channel(monkeypatch: pytest.MonkeyPatch,
                                                                 settings: Any) -> None:
    """Portas e contratos (o núcleo nunca testa qual é o canal); o canal passado aos tratadores.

    Estrutura: cada módulo de domínio do disparo não cita `TelegramChannel` nem `ConsoleChannel` e não
    faz `isinstance`, `issubclass` nem `type(...)` sobre o canal (checagem por AST); controle positivo da
    checagem: ela acusa a citação de `TelegramChannel` e `ConsoleChannel` (importação, apelido, `getattr`)
    e o teste do tipo do canal, e não acusa o uso da porta (capacidades e `formatter_for`), docstrings
    nem `isinstance` sobre outra coisa.

    Em execução: com um canal que só cumpre a porta `DeliveryChannel` (nem Telegram nem console), o
    `run_worker(…, console_routes=True)` registra os tratadores das 8 tarefas do disparo e não lê o
    `formatter` nem o `formatter_for` na subida."""
    problems: dict[str, list[str]] = {}
    for module in DISPATCH_DOMAIN_MODULES:
        files = _module_files(module)
        assert files, f"módulo de domínio cq_server.{module} não encontrado"
        problems |= {modname: hits for modname, path, _ in files
                     if (hits := _channel_offenses(path.read_text(encoding="utf-8")))}
    assert problems == {}
    for source, expected in _CHANNEL_SCAN_CASES:
        assert _channel_offenses(source) == expected, source

    channel = _PortChannel()
    handlers = _registered_handlers(monkeypatch, settings, channel=channel, engine=_PortEngine(),
                                    fetcher=_PortFetcher(), console_routes=True)
    assert sorted(_dispatch_task_types() - set(handlers)) == [], "tratadores novos ausentes do run_worker"
    assert channel.reads == []


# ---------------------------------------------------------------------------------------------
# A entrada simulada do console e as opções de teste da obtenção fora da produção
# ---------------------------------------------------------------------------------------------


def test_console_input_and_git_test_options_are_not_wired_in_production(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """Entrada simulada do console só nos testes (a
    `ConsoleInbox` e o `run_worker(…, console_routes=True)` existem só para a ponta a ponta pelo console;
    `GitTestOptions` só pelos testes, e `__main__.build_repo_fetcher` não tem caminho para ele): por AST,
    `cq_server.__main__`, todo módulo de `cq_server.api` e todo módulo de `cq_client` não citam
    `ConsoleInbox` nem `store_console_updates`; o `__main__` (onde fica o `_cmd_worker`) não cita
    `console_routes`; e, em todo o pacote `cq_server`, só `cq_server.adapters.git` cita `GitTestOptions`
    ou `test_options`. Contra o vazio: `_cmd_worker` e `build_repo_fetcher` existem no `__main__`, a
    `ConsoleInbox` existe em `cq_server.adapters.console` e `GitTestOptions` em `cq_server.adapters.git`.

    Em execução (a recepção, a fila e o worker): com o motor e a obtenção
    configurados no ambiente, o `_cmd_worker` passa ao `run_worker` o `HttpQuestionEngine` e o
    `GitRepoFetcher` que a raiz de composição monta, o `GitRepoFetcher` sem nenhum `GitTestOptions` em
    atributo nenhum, e não passa `console_routes`."""
    package = _package_dir()
    server = _package_sources(package, "cq_server")
    client_dir = REPO / "client" / "src" / "cq_client"
    client = _package_sources(client_dir, "cq_client")
    assert client and "cq_server.__main__" in server and "cq_server.api.routes" in server

    main_tree = ast.parse(server["cq_server.__main__"])
    functions = {node.name for node in ast.walk(main_tree) if isinstance(node, ast.FunctionDef)}
    assert {"_cmd_worker", "build_repo_fetcher", "build_engine"} <= functions
    assert "ConsoleInbox" in _identifiers(server["cq_server.adapters.console"])
    assert "GitTestOptions" in _identifiers(server["cq_server.adapters.git"])

    production = {name: source for name, source in {**server, **client}.items()
                  if name == "cq_server.__main__" or name.startswith("cq_server.api")
                  or name.startswith("cq_client")}
    assert {name: sorted(_identifiers(source) & CONSOLE_INPUT) for name, source in production.items()
            if _identifiers(source) & CONSOLE_INPUT} == {}
    assert "console_routes" not in _identifiers(server["cq_server.__main__"])
    test_options = {"GitTestOptions", "test_options"}
    assert {name: sorted(_identifiers(source) & test_options) for name, source in server.items()
            if _identifiers(source) & test_options and name != "cq_server.adapters.git"} == {}

    for source, expected in (
        ("from cq_server.adapters.console import ConsoleInbox", {"ConsoleInbox"}),
        ("from cq_server import reception\nreception.store_console_updates(conn)", {"store_console_updates"}),
        ("inbox = getattr(console, 'ConsoleInbox')", {"ConsoleInbox"}),
    ):
        assert _identifiers(source) & CONSOLE_INPUT == expected, source
    for source in (
        "def _cmd_worker(settings, bots):\n    return run_worker(settings, console_routes=True)",
        "def _cmd_worker(settings, bots):\n    return run_worker(settings, **{'console_routes': True})",
        "console_routes = True",
    ):
        assert "console_routes" in _identifiers(source), source
    for source in (
        "def build_repo_fetcher(env, settings):\n    return GitRepoFetcher(settings, test_options=None)",
        "from cq_server.adapters.git import GitTestOptions",
        "options = git.GitTestOptions(ca_file='x', allow_loopback=True, allow_port=True)",
    ):
        assert _identifiers(source) & test_options, source
    assert _identifiers('"""Sem ConsoleInbox na produção; nada de console_routes aqui."""') & (
        CONSOLE_INPUT | {"console_routes"}) == set()

    import signal

    from cq_server import __main__ as main
    from cq_server import worker
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.adapters.git import GitRepoFetcher, GitTestOptions
    from cq_server.config import Settings

    for name in [k for k in os.environ if k.startswith("CQ_")]:
        monkeypatch.delenv(name)
    for name, value in _build_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: None)  # o teste não instala sinais
    passed: list[dict[str, Any]] = []

    def run_worker(settings: Any, **kwargs: Any) -> None:
        passed.append(kwargs)

    monkeypatch.setattr(worker, "run_worker", run_worker)
    settings = Settings(database_url="postgresql://cq-fronteiras.invalid/cq")
    assert main._cmd_worker(settings, object()) == main.EXIT_OK  # type: ignore[arg-type]
    [kwargs] = passed
    engine, fetcher = kwargs.get("engine"), kwargs.get("fetcher")
    try:
        assert type(engine) is HttpQuestionEngine and type(fetcher) is GitRepoFetcher, (
            "o _cmd_worker não passou ao run_worker o que a raiz de composição monta")
        assert not any(isinstance(value, GitTestOptions) for value in vars(fetcher).values())
        assert kwargs.get("console_routes", False) is False
    finally:
        if isinstance(engine, HttpQuestionEngine):
            engine.close()
