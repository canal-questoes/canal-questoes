"""Fronteiras do núcleo ("Portas e contratos"; estrutura do
projeto).

O núcleo (`people`, `tokens`, `events`, `redaction`, `tasks`, `outbox`, `notices`, `migrate`,
`config`, `clock` e `ports/*`) não depende de HTTP, de e-mail, de processos externos, da camada de
API nem dos adaptadores. A checagem é por AST, sobre o código-fonte, e inclui importações dentro de
funções, sob `TYPE_CHECKING`, relativas e dinâmicas (`importlib.import_module`, `__import__`).

Verde desde o início: é uma propriedade estrutural
negativa, que já vale no esqueleto. Os controles positivos mostram que a checagem acusa as violações.

Oferecimentos e bots (o núcleo sem HTTP; "Portas e contratos"): o núcleo inclui
`offerings`, `bots`, `reception`,
`team`, `secretbox`, `timezones` e `botmessages` (`ports/telegram.py` já entra por `ports`); só
`cq_server.adapters.telegram` importa `httpx2`/`httpcore2`. Caso negativo: `cq_server.bots` não
importa o adaptador do Telegram (o `BotRuntime` recebe a fábrica de quem monta). Controle positivo:
o adaptador real importa `httpx2` e seria acusado se entrasse no núcleo. Verde desde o
início.

Vínculo do aluno (lista e convites; o convite por e-mail e a leitura de
identidades; "Portas e contratos"): o núcleo inclui `students`
(`ports/verification.py` já entra por `ports`); `smtplib` é proibido também na checagem
transitiva (só `cq_server.adapters.smtp` o importa). Caso negativo: `cq_server.students` não importa
`cq_server.adapters.smtp` (nem `smtplib`); controle positivo: o adaptador real importa `smtplib` e
seria acusado se entrasse no núcleo. Caso estrutural da leitura de `identities`: SQL
que cita a tabela `identities` (`FROM`, `JOIN`, `INTO` ou `UPDATE`) só em `cq_server.events` e
`cq_server.people` (os leitores do núcleo de dados) e em `cq_server.students`, com controle positivo sobre um texto
sintético. Verde desde o início.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

CORE_MODULES = ("people", "tokens", "events", "redaction", "tasks", "outbox", "notices", "migrate",
                "config", "clock", "ports",
                # Oferecimentos e bots (núcleo sem HTTP); `ports/telegram.py` já entra por `ports`.
                "offerings", "bots", "reception", "team", "secretbox", "timezones", "botmessages")
# Vínculo do aluno; `ports/verification.py` já entra por `ports`.
CORE_MODULES += ("students",)

# Proibidos por nome exato ou como prefixo de pacote (`starlette.requests`, `cq_server.api.routes`).
FORBIDDEN = ("fastapi", "starlette", "uvicorn", "requests", "aiohttp", "urllib.request",
             "http.client", "smtplib", "subprocess", "cq_server.api", "cq_server.adapters")
# o transporte do `httpx2` também é HTTP e fica só no adaptador.
FORBIDDEN += ("httpcore", "httpcore2")
# `httpx*`: qualquer pacote de topo cujo nome começa com `httpx` (httpx, httpx2, …).
FORBIDDEN_TOP_PREFIX = "httpx"
# Na checagem transitiva, a biblioteca padrão fica de fora: dependências legítimas do núcleo
# (psycopg, pydantic) carregam `subprocess` por conta própria. A AST cobre o uso direto.
TRANSITIVE_FORBIDDEN = ("fastapi", "starlette", "uvicorn", "requests", "aiohttp", "cq_server.api",
                        "cq_server.adapters")
TRANSITIVE_FORBIDDEN += ("httpcore", "httpcore2")  # oferecimentos e bots
# Vínculo do aluno: o protocolo de e-mail fica só no adaptador; nenhuma dependência do
# núcleo carrega `smtplib` (a exceção à regra da biblioteca padrão acima é deliberada).
TRANSITIVE_FORBIDDEN += ("smtplib",)

DYNAMIC_IMPORTERS = {"importlib.import_module", "import_module", "__import__", "importlib.__import__",
                     "builtins.__import__"}


def _is_forbidden(module: str, forbidden: tuple[str, ...] = FORBIDDEN) -> bool:
    if module.split(".")[0].startswith(FORBIDDEN_TOP_PREFIX):
        return True
    return any(module == f or module.startswith(f + ".") for f in forbidden)


def _package_dir() -> pathlib.Path:
    spec = importlib.util.find_spec("cq_server")
    assert spec is not None and spec.submodule_search_locations, "pacote cq_server não encontrado"
    return pathlib.Path(list(spec.submodule_search_locations)[0])


def _module_files(name: str) -> list[tuple[str, pathlib.Path, bool]]:
    """(nome qualificado, arquivo, é pacote) do módulo `cq_server.<name>`, como arquivo ou pacote."""
    pkg = _package_dir()
    found: list[tuple[str, pathlib.Path, bool]] = []
    single = pkg / f"{name}.py"
    if single.is_file():
        found.append((f"cq_server.{name}", single, False))
    folder = pkg / name
    if folder.is_dir():
        for path in sorted(folder.rglob("*.py")):
            parts = ["cq_server", *path.relative_to(pkg).with_suffix("").parts]
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            found.append((".".join(parts), path, is_pkg))
    return found


def _resolve(module: str | None, level: int, package: str) -> str:
    if level == 0:
        return module or ""
    parts = package.split(".")
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    base = ".".join(parts)
    return f"{base}.{module}" if module else base


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        inner = _call_name(func.value)
        return f"{inner}.{func.attr}" if inner else func.attr
    return ""


def _imported_names(source: str, modname: str, is_pkg: bool) -> Iterator[tuple[int, str]]:
    """Todo nome de módulo importado por `source`, com a linha, resolvendo importações relativas."""
    package = modname if is_pkg else modname.rpartition(".")[0]
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module, node.level, package)
            yield node.lineno, base
            for alias in node.names:
                if alias.name != "*":
                    yield node.lineno, f"{base}.{alias.name}"
        elif isinstance(node, ast.Call) and _call_name(node.func) in DYNAMIC_IMPORTERS:
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                target = node.args[0].value
                stripped = target.lstrip(".")
                level = len(target) - len(stripped)
                if level:
                    anchor = package
                    for kw in node.keywords:
                        if kw.arg == "package" and isinstance(kw.value, ast.Constant):
                            anchor = str(kw.value.value)
                    target = _resolve(stripped or None, level, anchor)
                yield node.lineno, target


def _violations(source: str, modname: str, is_pkg: bool = False) -> list[tuple[int, str]]:
    return sorted({(line, name) for line, name in _imported_names(source, modname, is_pkg)
                   if _is_forbidden(name)})


@pytest.mark.parametrize("module", CORE_MODULES)
def test_core_module_does_not_import_http_or_adapters(module: str) -> None:
    """Portas e contratos: o módulo do núcleo não importa HTTP, e-mail, processos, API nem
    adaptadores (checagem por AST)."""
    files = _module_files(module)
    assert files, f"módulo do núcleo cq_server.{module} não encontrado (a estrutura do repositório)"
    problems = []
    for modname, path, is_pkg in files:
        for line, name in _violations(path.read_text(encoding="utf-8"), modname, is_pkg):
            problems.append(f"{path.relative_to(_package_dir().parent)}:{line} importa {name}")
    assert problems == []


def test_core_modules_do_not_load_http_or_adapters_transitively() -> None:
    """Portas e contratos: importar o núcleo não carrega frameworks HTTP, a API nem os adaptadores,
    nem por dependência indireta (num processo novo)."""
    modules = [modname for name in CORE_MODULES for modname, _, _ in _module_files(name)]
    assert len(modules) >= len(CORE_MODULES)
    code = (
        "import importlib, json, sys\n"
        "for m in json.loads(sys.argv[1]):\n"
        "    importlib.import_module(m)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"}
    result = subprocess.run([sys.executable, "-c", code, json.dumps(modules)], cwd=REPO, env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert set(modules) <= set(loaded)
    assert [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)] == []


FORBIDDEN_SNIPPETS = [
    ("import fastapi", "cq_server.people", False, "fastapi"),
    ("from fastapi import FastAPI", "cq_server.people", False, "fastapi"),
    ("import starlette.requests", "cq_server.tokens", False, "starlette.requests"),
    ("from uvicorn import run", "cq_server.events", False, "uvicorn"),
    ("import httpx", "cq_server.redaction", False, "httpx"),
    ("import httpx2 as h", "cq_server.tasks", False, "httpx2"),
    ("import httpx_sse", "cq_server.tasks", False, "httpx_sse"),
    ("from requests import get", "cq_server.outbox", False, "requests"),
    ("import aiohttp", "cq_server.notices", False, "aiohttp"),
    ("import urllib.request", "cq_server.migrate", False, "urllib.request"),
    ("from urllib import request", "cq_server.migrate", False, "urllib.request"),
    ("from urllib.request import urlopen", "cq_server.config", False, "urllib.request"),
    ("import http.client", "cq_server.clock", False, "http.client"),
    ("from http import client", "cq_server.clock", False, "http.client"),
    ("import smtplib", "cq_server.outbox", False, "smtplib"),
    ("from subprocess import run", "cq_server.migrate", False, "subprocess"),
    ("from cq_server import api", "cq_server.people", False, "cq_server.api"),
    ("from cq_server.api import create_app", "cq_server.people", False, "cq_server.api"),
    ("from . import api", "cq_server.people", False, "cq_server.api"),
    ("from .adapters.console import ConsoleChannel", "cq_server.outbox", False, "cq_server.adapters.console"),
    ("from .. import adapters", "cq_server.ports.delivery", False, "cq_server.adapters"),
    ("from ..api import schemas", "cq_server.ports", True, "cq_server.api"),
    ("import importlib\nimportlib.import_module('fastapi')", "cq_server.tasks", False, "fastapi"),
    ("from importlib import import_module\nimport_module('.adapters', package='cq_server')",
     "cq_server.tasks", False, "cq_server.adapters"),
    ("__import__('subprocess')", "cq_server.tasks", False, "subprocess"),
    ("def f():\n    import httpx2\n    return httpx2", "cq_server.people", False, "httpx2"),
    ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from starlette.requests import Request",
     "cq_server.events", False, "starlette.requests"),
    # o adaptador do Telegram e o transporte HTTP dentro dos módulos do núcleo.
    ("from .adapters.telegram import http_factory", "cq_server.bots", False, "cq_server.adapters.telegram"),
    ("from cq_server.adapters import telegram", "cq_server.team", False, "cq_server.adapters.telegram"),
    ("from ..adapters.telegram import HttpTelegramBotApi", "cq_server.ports.telegram", False,
     "cq_server.adapters.telegram"),
    ("import importlib\nimportlib.import_module('cq_server.adapters.telegram')", "cq_server.reception", False,
     "cq_server.adapters.telegram"),
    ("import httpx2", "cq_server.offerings", False, "httpx2"),
    ("import httpcore2", "cq_server.reception", False, "httpcore2"),
    ("from httpcore2 import ConnectionPool", "cq_server.bots", False, "httpcore2"),
    ("import httpcore", "cq_server.secretbox", False, "httpcore"),
]


@pytest.mark.parametrize("source, modname, is_pkg, expected", FORBIDDEN_SNIPPETS,
                         ids=[s[0].splitlines()[-1] for s in FORBIDDEN_SNIPPETS])
def test_scanner_detects_forbidden_imports(source: str, modname: str, is_pkg: bool, expected: str) -> None:
    """Controle positivo: a checagem por AST acusa cada forma de importação proibida."""
    names = {name for _, name in _violations(source, modname, is_pkg)}
    assert expected in names


@pytest.mark.parametrize("source, modname", [
    ("import json\nimport urllib.parse\nfrom http import HTTPStatus", "cq_server.people"),
    ("import psycopg\nfrom cq_server.clock import Clock", "cq_server.tasks"),
    ("from . import clock\nfrom .ports.delivery import Sent", "cq_server.outbox"),
    ("from .delivery import Sent", "cq_server.ports.other"),
    ("import email_validator\nimport uuid", "cq_server.people"),
    # a porta, a cifra e a base de fusos são permitidas ao núcleo.
    ("from cq_server.ports.telegram import TelegramBotApiFactory\nfrom .secretbox import TokenCipher",
     "cq_server.bots"),
    ("from cryptography.hazmat.primitives.ciphers.aead import AESGCM", "cq_server.secretbox"),
    ("import tzdata\nimport zoneinfo", "cq_server.timezones"),
    ("from .telegram import BotCommand", "cq_server.ports.other"),
])
def test_scanner_accepts_allowed_imports(source: str, modname: str) -> None:
    """Controle negativo: importações permitidas ao núcleo não são acusadas."""
    assert _violations(source, modname) == []


def test_scanner_flags_the_real_api_package() -> None:
    """Controle positivo sobre o código real: a camada de API importa o framework HTTP, e a mesma
    checagem aplicada a ela acusa isso."""
    files = _module_files("api")
    assert files, "pacote cq_server.api não encontrado"
    names = {name for modname, path, is_pkg in files
             for _, name in _violations(path.read_text(encoding="utf-8"), modname, is_pkg)}
    assert any(n.split(".")[0] in ("fastapi", "starlette") for n in names), names


def _is_telegram_transport(module: str) -> bool:
    return (module == "cq_server.adapters" or module.startswith("cq_server.adapters.")
            or module.split(".")[0] in ("httpx2", "httpcore2"))


def test_bots_does_not_import_the_telegram_adapter() -> None:
    """Caso negativo (oferecimentos e bots: o `api_factory` do `BotRuntime` é
    obrigatório porque o núcleo não importa o adaptador; "Portas e contratos"): `cq_server.bots` não
    importa `cq_server.adapters.telegram` (nem outro adaptador, nem `httpx2`/`httpcore2`), pela AST, e
    importar `cq_server.bots` num processo novo não carrega nenhum deles."""
    files = _module_files("bots")
    assert [modname for modname, _, _ in files] == ["cq_server.bots"]
    modname, path, is_pkg = files[0]
    names = {name for _, name in _imported_names(path.read_text(encoding="utf-8"), modname, is_pkg)}
    assert "cq_server.ports.telegram" in names, "cq_server.bots não usa a porta (checagem no vazio)"
    assert sorted(n for n in names if _is_telegram_transport(n)) == []

    code = "import json, sys\nimport cq_server.bots\nprint(json.dumps(sorted(sys.modules)))\n"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"}
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert {"cq_server.bots", "cq_server.ports.telegram"} <= set(loaded)
    assert [m for m in loaded if _is_telegram_transport(m)] == []


def test_scanner_flags_the_real_telegram_adapter() -> None:
    """Controle positivo sobre o código real (oferecimentos e bots: o adaptador do
    Telegram é quem fala HTTP): `cq_server.adapters.telegram` importa `httpx2`
    e `httpcore2`, e a checagem do núcleo o acusa, com o nome dele e se entrasse no núcleo (lido
    como `cq_server.bots`)."""
    files = [f for f in _module_files("adapters") if f[0] == "cq_server.adapters.telegram"]
    assert len(files) == 1, "cq_server.adapters.telegram não encontrado (a estrutura do repositório)"
    modname, path, is_pkg = files[0]
    source = path.read_text(encoding="utf-8")
    names = {name for _, name in _violations(source, modname, is_pkg)}
    assert {"httpx2", "httpcore2"} <= names, names
    as_core = {name for _, name in _violations(source, "cq_server.bots")}
    assert {"httpx2", "httpcore2"} <= as_core, as_core


# ---------------------------------------------------------------------------------------------
# o adaptador SMTP fora do núcleo (vínculo do aluno)
# ---------------------------------------------------------------------------------------------


def _is_smtp_transport(module: str) -> bool:
    return (module == "cq_server.adapters" or module.startswith("cq_server.adapters.")
            or module.split(".")[0] == "smtplib")


def _modules_loaded_by(module: str) -> list[str]:
    """`sys.modules` depois de importar só `module` num processo novo."""
    code = f"import json, sys\nimport {module}\nprint(json.dumps(sorted(sys.modules)))\n"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP"}
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_students_does_not_import_the_smtp_adapter() -> None:
    """Caso negativo (vínculo do aluno: o tratador `invite.email`
    recebe o canal de quem monta, e o núcleo fala só com a porta `ports.verification`;
    "Portas e contratos"): `cq_server.students` não importa `cq_server.adapters.smtp` (nem outro adaptador, nem
    `smtplib`), pela AST, e importar `cq_server.students` num processo novo não carrega nenhum deles.
    Controle positivo: a checagem por AST acusa cada forma de trazer o adaptador ou o `smtplib` para
    `students` ou para a porta."""
    files = _module_files("students")
    assert [modname for modname, _, _ in files] == ["cq_server.students"]
    modname, path, is_pkg = files[0]
    names = {name for _, name in _imported_names(path.read_text(encoding="utf-8"), modname, is_pkg)}
    assert "cq_server.ports.verification" in names, "cq_server.students não usa a porta (checagem no vazio)"
    assert sorted(n for n in names if _is_smtp_transport(n)) == []

    adapter = "cq_server.adapters.smtp"
    for source, target, expected in (
        ("from .adapters.smtp import SmtpVerificationChannel", "cq_server.students", adapter),
        ("from cq_server.adapters import smtp", "cq_server.students", adapter),
        ("import cq_server.adapters.smtp", "cq_server.students", adapter),
        ("import smtplib", "cq_server.students", "smtplib"),
        ("from smtplib import SMTP", "cq_server.students", "smtplib"),
        ("import importlib\nimportlib.import_module('smtplib')", "cq_server.students", "smtplib"),
        ("def f():\n    from .adapters.smtp import SmtpDeadline\n    return SmtpDeadline", "cq_server.students",
         adapter),
        ("from ..adapters.smtp import SmtpDeadline", "cq_server.ports.verification", adapter),
    ):
        assert expected in {name for _, name in _violations(source, target)}, source

    loaded = _modules_loaded_by("cq_server.students")
    assert {"cq_server.students", "cq_server.ports.verification"} <= set(loaded)
    assert [m for m in loaded if _is_smtp_transport(m)] == []


def test_scanner_flags_the_real_smtp_adapter() -> None:
    """Controle positivo sobre o código real (vínculo do aluno: o
    adaptador SMTP é o único módulo que fala com o servidor de e-mail): `cq_server.adapters.smtp`
    importa `smtplib`, e a checagem do núcleo o acusa, com o nome dele e se entrasse no núcleo (lido
    como `cq_server.students`); importá-lo num processo novo carrega `smtplib`, que a checagem
    transitiva (`TRANSITIVE_FORBIDDEN`) acusa."""
    files = [f for f in _module_files("adapters") if f[0] == "cq_server.adapters.smtp"]
    assert len(files) == 1, "cq_server.adapters.smtp não encontrado (a estrutura do repositório)"
    modname, path, is_pkg = files[0]
    source = path.read_text(encoding="utf-8")
    assert "smtplib" in {name for _, name in _violations(source, modname, is_pkg)}
    assert "smtplib" in {name for _, name in _violations(source, "cq_server.students")}

    loaded = _modules_loaded_by("cq_server.adapters.smtp")
    assert "smtplib" in [m for m in loaded if _is_forbidden(m, TRANSITIVE_FORBIDDEN)]


# ---------------------------------------------------------------------------------------------
# leitura interna de `identities`
# ---------------------------------------------------------------------------------------------

# SQL que cita a tabela `identities`. Sem diferenciar maiúsculas, como o próprio SQL.
IDENTITIES_SQL = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+identities\b", re.IGNORECASE)
# Os leitores do núcleo de dados (`events`, que carrega as identidades para a redação, e
# `people`, no cadastro e na revelação) e o módulo de alunos (tratador `invite.email`, `/convite`
# e importação).
IDENTITIES_READERS = frozenset({"cq_server.events", "cq_server.people", "cq_server.students"})


def _package_modules() -> list[tuple[str, pathlib.Path]]:
    """(nome qualificado, arquivo) de todo módulo `.py` do pacote `cq_server`, inclusive os
    subpacotes (`api`, `adapters`, `ports`, `migrations`)."""
    pkg = _package_dir()
    found: list[tuple[str, pathlib.Path]] = []
    for path in sorted(pkg.rglob("*.py")):
        parts = ["cq_server", *path.relative_to(pkg).with_suffix("").parts]
        if parts[-1] == "__init__":
            parts = parts[:-1]
        found.append((".".join(parts), path))
    return found


def _identities_sql(source: str) -> list[str]:
    """Cada citação de `identities` em SQL (ex.: `FROM identities`), com os espaços normalizados.

    Olha o código-fonte inteiro e cada literal de texto da AST: o parser junta os literais adjacentes
    (inclusive as partes fixas de uma f-string), e assim um SQL partido em várias linhas do código
    (`"… FROM "` + `"identities …"`) também é achado."""
    texts = [source] + [node.value for node in ast.walk(ast.parse(source))
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    return sorted({" ".join(m.group(0).split()) for text in texts for m in IDENTITIES_SQL.finditer(text)})


def _identities_offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    """Módulos fora de `IDENTITIES_READERS` cujo código cita `identities` em SQL."""
    return {modname: hits for modname, source in sorted(sources.items())
            if modname not in IDENTITIES_READERS and (hits := _identities_sql(source))}


def test_identities_sql_only_in_base_readers_and_students() -> None:
    """Caso estrutural (leitura interna de
    `identities`; verde desde o início): em todo módulo de `cq_server` (inclusive `api`,
    `adapters`, `ports`, `reception`, `team` e `worker`), SQL que cita a tabela `identities` com
    `FROM`, `JOIN`, `INTO` ou `UPDATE` só aparece em `cq_server.events` e `cq_server.people` (os
    leitores do núcleo de dados) e em `cq_server.students`; a consulta do `/convite` pelo e-mail fica em
    `students`, nunca na `reception`. Contra o vazio: a busca acha os dois leitores do núcleo de dados."""
    sources = {modname: path.read_text(encoding="utf-8") for modname, path in _package_modules()}
    assert {"cq_server.reception", "cq_server.team", "cq_server.worker", "cq_server.students",
            "cq_server.api.routes", "cq_server.adapters.smtp", "cq_server.ports.verification"} <= set(sources)
    assert _identities_offenders(sources) == {}
    readers = {modname for modname, source in sources.items() if _identities_sql(source)}
    assert {"cq_server.events", "cq_server.people"} <= readers <= IDENTITIES_READERS, readers


_IDENTITIES_SQL_CASES = [
    ('cur.execute("SELECT email FROM identities WHERE person_id = %s", (person_id,))', ["FROM identities"]),
    ('"SELECT p.id FROM people p JOIN identities i ON i.person_id = p.id"', ["JOIN identities"]),
    ('"INSERT INTO identities (person_id, ra) VALUES (%s, %s)"', ["INTO identities"]),
    ('"UPDATE identities SET email = %s WHERE person_id = %s"', ["UPDATE identities"]),
    ('"select email from identities where email_normalized = %s"', ["from identities"]),
    ('"""SELECT email\n    FROM\n        identities\n    WHERE ra = %s"""', ["FROM identities"]),
    ('query = ("SELECT email FROM "\n         "identities WHERE ra = %s")', ["FROM identities"]),
    ('query = f"SELECT email FROM identities WHERE ra = {ra!r}"', ["FROM identities"]),
    ("known = load_known_identities(conn)", []),
    ('"SELECT id FROM people WHERE id = %s"', []),
    ('"SELECT 1 FROM identities_archive"', []),
    ("# as identities ficam na área restrita", []),
]


@pytest.mark.parametrize("source, expected", _IDENTITIES_SQL_CASES,
                         ids=[f"caso{i}" for i in range(len(_IDENTITIES_SQL_CASES))])
def test_identities_scan_detects_sql_forms(source: str, expected: list[str]) -> None:
    """Controle positivo da busca: ela acusa cada forma de SQL que cita a tabela
    `identities` (`FROM`, `JOIN`, `INTO`, `UPDATE`, em minúsculas, com quebra de linha, partido em
    literais adjacentes e em f-string) e não acusa nomes de função, outras tabelas nem comentários sem
    SQL."""
    assert _identities_sql(source) == expected


def test_identities_scan_flags_a_synthetic_reader_outside_students() -> None:
    """Controle positivo por módulo: um texto sintético com `FROM identities` é
    acusado quando está num módulo fora dos leitores (lido como `cq_server.reception` ou
    `cq_server.worker`) e aceito em `cq_server.events`, `cq_server.people` e `cq_server.students`."""
    sql = 'cur.execute("SELECT person_id FROM identities WHERE email_normalized = %s", (email,))'
    sources = {modname: sql for modname in ("cq_server.reception", "cq_server.worker", "cq_server.events",
                                            "cq_server.people", "cq_server.students")}
    assert _identities_offenders(sources) == {"cq_server.reception": ["FROM identities"],
                                              "cq_server.worker": ["FROM identities"]}
