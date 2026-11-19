"""Passos de teste do CI.

Uso (nos workflows `.github/workflows/ci.yml`):

    uv run python ops/ci/ci_suite.py fast|full [--workers N] [--junit-dir D] [--summary <arquivo>]
                                               [--coverage-xml <arquivo>]

Roda `pytest -m "not system" -n N --dist worksteal --junitxml=…` (e, no `full`, depois, `-m system` em
série); lê os JUnit; se as falhas forem **todas** da lista por nó (`ops/ci/known-flaky.txt`, ou
a lista local de instáveis, filtrada, quando existir), repete **só** esses nós uma vez, e o
resultado da repetição decide; falha fora da lista falha sem repetição. O resumo (em
`$GITHUB_STEP_SUMMARY` ou `--summary`) lista as repetições. O executor do `pytest` é injetável.

Com `--coverage-xml <arquivo>`, a chamada principal mede a cobertura (`--cov --cov-report=xml:<arquivo>
--cov-report=`, com a configuração do `pyproject.toml`) e a repetição dos instáveis acrescenta à mesma medição
(`--cov --cov-append …`), para o XML final ter os dados das duas; a parte de sistema do `full` não mede. O código de
saída e o resumo são os mesmos de sem a opção: o resumo da cobertura é um passo à parte do workflow.

Só biblioteca padrão. Importável sem efeito colateral.

**[I]** Escolhas da implementação, não fixadas de antemão: a lista do CI vence a
local quando as duas existem; um caminho relativo das listas é lido a partir da raiz desta árvore (a deste
arquivo); no `full`, a parte de sistema roda mesmo com falha na principal, e a decisão vale para o conjunto das
duas; um código ≠ 0 sem falha legível no JUnit (coleta interrompida, JUnit ausente ou ilegível) falha sem
repetição; o resumo vai acrescentado ao `$GITHUB_STEP_SUMMARY` (como o GitHub espera) e sobrescreve o `--summary`.
"""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Literal, NoReturn, TextIO

SuiteKind = Literal["fast", "full"]

DEFAULT_WORKERS = 4

# As listas de instáveis por nó (a do CI é gerada na exportação).
KNOWN_FLAKY_CI = "ops/ci/known-flaky.txt"
KNOWN_FLAKY_LOCAL = "docs/execucao/instaveis-conhecidos.txt"
SUMMARY_ENV = "GITHUB_STEP_SUMMARY"

# [I] Os textos do resumo são da implementação (o que importa é que o resumo liste as repetições).
CI_MESSAGES: Mapping[str, str] = {
    "passed": "Suíte verde.",
    "failed": "Suíte vermelha: há falhas fora da lista de instáveis conhecidos.",
    "rerun_header": "Repetição única dos instáveis conhecidos:",
    "rerun_node": "- {node}",
    "rerun_passed": "A repetição passou.",
    "rerun_failed": "A repetição falhou.",
    "invalid_arguments": "Argumentos inválidos.",
}

# O executor do `pytest`: recebe os argumentos (sem o `pytest` na frente) e devolve o código de saída.
PytestRunner = Callable[[Sequence[str]], int]


def _tree_root() -> Path:
    """A raiz da árvore (a do repositório ou a da exportação): dois níveis acima de `ops/ci/`."""
    return Path(__file__).resolve().parents[2]


def load_known_nodes(path: Path) -> frozenset[str]:
    """As entradas por nó da lista (`arquivo::nome[parâmetro]`); as `re:` e os comentários ignorados."""
    nodes: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key = line.partition("\t")[0].strip()
        if not key or key.startswith("re:"):
            continue
        nodes.add(key)
    return frozenset(nodes)


def _known_list() -> frozenset[str]:
    """A lista em vigor, lida na chamada: a do CI (gerada pela exportação) e, sem ela, a local filtrada."""
    for name in (KNOWN_FLAKY_CI, KNOWN_FLAKY_LOCAL):
        path = _tree_root() / name  # um caminho absoluto descarta a raiz
        if path.is_file():
            return load_known_nodes(path)
    return frozenset()


def is_known(node: str, known: frozenset[str]) -> bool:
    """O nó está na lista: igual, igual sem o parâmetro, ou dentro de um arquivo (ou classe) listado."""
    base = node.split("[", 1)[0]
    if node in known or base in known:
        return True
    return any(base.startswith(key + "::") for key in known)


def _node_id(classname: str, name: str) -> str:
    """`classname` do xunit2 (com pontos) + `name` (com o parâmetro) → `arquivo::[Classe::]nome[parâmetro]`.

    O arquivo é o prefixo mais longo do `classname` que existe como `.py` no diretório atual ou na raiz da árvore;
    sem nenhum, o `classname` inteiro vira o arquivo (o caso dos testes de módulo, sem classe)."""
    if not classname:
        return name
    parts = classname.split(".")
    roots = (Path.cwd(), _tree_root())
    for i in range(len(parts), 0, -1):
        candidate = Path(*parts[:i]).with_suffix(".py")
        if any((root / candidate).is_file() for root in roots):
            return "::".join([candidate.as_posix(), *parts[i:], name])
    return f"{'/'.join(parts)}.py::{name}"


def failed_nodes(junit_paths: Iterable[Path]) -> set[str]:
    """Os nós com `failure` ou `error` nos JUnit dados, normalizados para `arquivo::nome[parâmetro]`."""
    failed: set[str] = set()
    for junit in junit_paths:
        root = ET.parse(junit).getroot()
        for case in root.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            failed.add(_node_id(case.get("classname", ""), case.get("name", "")))
    return failed


def _call(pytest: PytestRunner, args: list[str], junit: Path) -> tuple[int, set[str] | None]:
    """Roda uma chamada do `pytest` com o JUnit em `junit`; devolve o código e as falhas (`None`: JUnit ilegível)."""
    junit.unlink(missing_ok=True)
    code = pytest([*args, f"--junitxml={junit}"])
    try:
        failed = failed_nodes([junit])
    except (OSError, ET.ParseError):
        return code, None
    return code, failed


def _coverage_args(coverage_xml: Path | None, *, append: bool) -> list[str]:
    """Os argumentos do pytest-cov de uma chamada: nenhum sem o arquivo; com ele, a medição com o XML no arquivo e
    sem o relatório no terminal (`--cov-report=` vazio), e a repetição acrescenta à medição da principal."""
    if coverage_xml is None:
        return []
    return ["--cov", *(["--cov-append"] if append else []), f"--cov-report=xml:{coverage_xml}", "--cov-report="]


def run(kind: SuiteKind, *, workers: int, junit_dir: Path, summary: TextIO, pytest: PytestRunner,
        coverage_xml: Path | None = None) -> int:
    """Um passo do CI; devolve o código de saída. `coverage_xml`: o caminho do
    relatório de cobertura em XML, opcional; sem ele, o passo não mede cobertura."""
    junit_dir = Path(junit_dir)
    junit_dir.mkdir(parents=True, exist_ok=True)
    measure = _coverage_args(coverage_xml, append=False)
    calls = [(["-q", "-m", "not system", "-n", str(workers), "--dist", "worksteal", *measure],
              junit_dir / f"{kind}-main.xml")]
    if kind == "full":
        calls.append((["-q", "-m", "system"], junit_dir / f"{kind}-system.xml"))
    failed: set[str] = set()
    clean = True  # todo código ≠ 0 explicado por falhas legíveis no JUnit
    for args, junit in calls:
        code, nodes = _call(pytest, args, junit)
        if nodes is None or (code != 0 and not nodes):
            clean = False
        failed |= nodes or set()
    if clean and not failed:
        summary.write(CI_MESSAGES["passed"] + "\n")
        return 0
    known = _known_list()
    if not clean or not all(is_known(node, known) for node in failed):
        summary.write(CI_MESSAGES["failed"] + "\n")
        return 1
    rerun_nodes = sorted(failed)
    summary.write(CI_MESSAGES["rerun_header"] + "\n")
    for node in rerun_nodes:
        summary.write(CI_MESSAGES["rerun_node"].format(node=node) + "\n")
    # Os argumentos da cobertura antes dos nós: o `--cov` sem valor não pode ficar logo antes de um nó.
    code, nodes = _call(pytest, ["-q", *_coverage_args(coverage_xml, append=True), *rerun_nodes],
                        junit_dir / f"{kind}-rerun.xml")
    if code == 0 and nodes == set():
        summary.write(CI_MESSAGES["rerun_passed"] + "\n")
        summary.write(CI_MESSAGES["passed"] + "\n")
        return 0
    summary.write(CI_MESSAGES["rerun_failed"] + "\n")
    return 1


class _ArgumentError(Exception):
    """Argumentos inválidos (a mensagem do `argparse`, que ecoaria a entrada, não sai)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: ARG002 - mensagem fixa no lugar da do argparse
        raise _ArgumentError


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise ValueError
    return value


def _subprocess_pytest(args: Sequence[str]) -> int:
    """O executor real: `python -m pytest` com o interpretador atual (o do `uv run` no CI)."""
    return subprocess.run([sys.executable, "-m", "pytest", *args], check=False).returncode


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         pytest: PytestRunner | None = None) -> int:
    """Linha de comando; `env=None` → o ambiente do processo (lê só `GITHUB_STEP_SUMMARY`);
    `pytest=None` → o `pytest` num subprocesso."""
    parser = _Parser(prog="ci_suite.py", add_help=False)
    parser.add_argument("kind", choices=("fast", "full"))
    parser.add_argument("--workers", type=_positive, default=DEFAULT_WORKERS)
    parser.add_argument("--junit-dir", type=Path, default=Path("junit"))
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--coverage-xml", type=Path)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentError:
        print(CI_MESSAGES["invalid_arguments"], file=sys.stderr)
        return 2
    env = os.environ if env is None else env
    buffer = io.StringIO()
    try:
        return run(args.kind, workers=args.workers, junit_dir=args.junit_dir, summary=buffer,
                   pytest=pytest or _subprocess_pytest, coverage_xml=args.coverage_xml)
    finally:
        text = buffer.getvalue()
        if args.summary is not None:
            args.summary.write_text(text, encoding="utf-8", newline="\n")
        elif env.get(SUMMARY_ENV):
            with open(env[SUMMARY_ENV], "a", encoding="utf-8", newline="\n") as out:
                out.write(text)
        else:
            sys.stdout.write(text)


if __name__ == "__main__":
    sys.exit(main())
