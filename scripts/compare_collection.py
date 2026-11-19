"""Comparação da coleta serial com a paralela da suíte.


Uso:

    uv run python scripts/compare_collection.py [--workers N] [caminho ...]

Coleta os ids em série (`pytest --collect-only -q -m "not system"`) e os ids executados em paralelo (os
`testcase` do JUnit do controlador de `pytest -n <N> --dist worksteal --junitxml=<tmp>`), normaliza os dois
para `arquivo::nome[parâmetro]` e compara: iguais → 0; diferentes → código ≠ 0 com a contagem e só os ids
divergentes.

A lista paralela sai de uma execução **real**, porque o `--collect-only` desliga o xdist. Os
caminhos opcionais (por exemplo `tests/unit`) vão para as duas chamadas, para uma conferência parcial.

Códigos de saída:
- 0: os dois conjuntos são iguais;
- 1: os conjuntos diferem (a contagem e os ids divergentes vão para o stdout);
- 2: uma das chamadas do `pytest` não produziu uma lista utilizável (coleta com erro, execução interrompida ou
  JUnit ausente ou ilegível).

Uma execução paralela com casos que falham (código 1 do `pytest`) ainda serve para a comparação: o que importa
aqui é o conjunto executado, não o resultado; o script avisa no stderr.

Só biblioteca padrão. Importável sem efeito colateral. O executor do `pytest` é injetável.

Normalização (conferida no `src/_pytest/junitxml.py` do pytest 9.1.1): no xunit2 o `testcase`
traz só `classname` (o caminho do arquivo com `.` no lugar de `/` e sem o `.py`, seguido das classes) e `name` (o
último nome, com o parâmetro). A comparação é por conjunto: o `testcase` repetido do caso "falha na chamada e erro
na desmontagem" não conta como id a mais. Um `testcase` de coleta que falhou (`classname` vazio) fica com o `name`
cru e aparece como divergência, o que é desejado. **[I]** A suíte não tem classes de teste; com classe, o arquivo é
o prefixo mais longo do `classname` que existe como `.py` no repositório.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

# O executor do `pytest`: recebe os argumentos (sem o `pytest` na frente) e devolve o código e o stdout.
PytestRunner = Callable[[Sequence[str]], Any]

REPO = Path(__file__).resolve().parent.parent

# O padrão de processos do xdist no Windows (o mesmo de `scripts/run-suite.sh`).
DEFAULT_WORKERS = 6

MARKER = "not system"

# A linha final da coleta: `66 tests collected in 0.15s`, `66/89 tests collected (23 deselected) in 0.33s`,
# `1 test collected ...` ou `no tests collected ...`.
_COUNT_LINE = re.compile(r"^(\d+(/\d+)?|no) tests? collected\b")

# Códigos do `pytest` em que a execução foi até o fim (0: tudo passou; 1: algum caso falhou).
_RUN_COMPLETED = (0, 1)


def serial_ids(output: str) -> list[str]:
    """Os ids da saída de `pytest --collect-only -q`, normalizados."""
    ids: list[str] = []
    for raw in output.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        # Um cabeçalho de seção (`=== warnings summary ===`, `=== ERRORS ===`) ou a contagem encerram a lista.
        if line.startswith("=") or _COUNT_LINE.match(line):
            break
        ids.append(line)
    return ids


def _file_of(parts: Sequence[str]) -> tuple[str, list[str]]:
    """Separa o `classname` em caminho do arquivo e classes: o prefixo mais longo que existe como `.py`."""
    for k in range(len(parts), 0, -1):
        candidate = "/".join(parts[:k]) + ".py"
        if (REPO / candidate).is_file():
            return candidate, list(parts[k:])
    return "/".join(parts) + ".py", []


def _normalize_testcase(classname: str, name: str) -> str:
    if not classname:
        # `testcase` de coleta (erro ou `skip` de módulo): sem arquivo para reconstruir.
        return name
    file_part, classes = _file_of(classname.split("."))
    return "::".join([file_part, *classes, name])


def junit_ids(path: Path) -> list[str]:
    """Os ids dos `testcase` do JUnit do controlador, normalizados."""
    root = ET.parse(path).getroot()
    return [_normalize_testcase(case.get("classname", ""), case.get("name", "")) for case in root.iter("testcase")]


def compare(serial: Iterable[str], parallel: Iterable[str], *, out: TextIO) -> int:
    """0 se os dois conjuntos são iguais; senão ≠ 0, com a contagem e os ids divergentes em `out`."""
    serial_set = set(serial)
    parallel_set = set(parallel)
    print(f"Coleta em série: {len(serial_set)} casos.", file=out)
    print(f"Execução paralela: {len(parallel_set)} casos.", file=out)
    if serial_set == parallel_set:
        print(f"Iguais: os mesmos {len(serial_set)} casos nos dois lados.", file=out)
        return 0
    only_serial = sorted(serial_set - parallel_set)
    only_parallel = sorted(parallel_set - serial_set)
    print(f"Diferentes: {len(only_serial)} só na série e {len(only_parallel)} só na execução paralela.", file=out)
    if only_serial:
        print("Só na coleta em série:", file=out)
        for node in only_serial:
            print(f"  {node}", file=out)
    if only_parallel:
        print("Só na execução paralela:", file=out)
        for node in only_parallel:
            print(f"  {node}", file=out)
    return 1


def _subprocess_pytest(args: Sequence[str]) -> tuple[int, str]:
    """O `pytest` de verdade, no interpretador atual e na raiz do repositório; o stderr passa direto."""
    env = dict(os.environ, PYTHONUTF8="1")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode, completed.stdout or ""


def _unpack(result: Any) -> tuple[int, str]:
    """Aceita `(código, stdout)` ou um objeto com `returncode` e `stdout` (como o `CompletedProcess`)."""
    if hasattr(result, "returncode"):
        return int(result.returncode), str(getattr(result, "stdout", "") or "")
    code, stdout = result
    return int(code), str(stdout or "")


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("o número de processos tem de ser um inteiro positivo") from None
    if value < 1:
        raise argparse.ArgumentTypeError("o número de processos tem de ser um inteiro positivo")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compare_collection.py",
        description="Compara a coleta em série com os casos executados em paralelo pelo xdist.",
    )
    parser.add_argument("--workers", type=_positive_int, default=DEFAULT_WORKERS,
                        help=f"processos do xdist na execução paralela (padrão: {DEFAULT_WORKERS})")
    parser.add_argument("paths", nargs="*", help="caminhos de teste opcionais, passados às duas chamadas")
    return parser


def main(argv: Sequence[str] | None = None, *, pytest: PytestRunner | None = None) -> int:
    """Linha de comando; `pytest=None` → o `pytest` num subprocesso."""
    args = _parser().parse_args(list(argv) if argv is not None else None)
    runner: PytestRunner = pytest if pytest is not None else _subprocess_pytest
    paths = list(args.paths)

    print("Coletando os casos em série...", file=sys.stderr)
    code, stdout = _unpack(runner(["--collect-only", "-q", "-m", MARKER, *paths]))
    if code != 0:
        print(f"A coleta em série falhou (código {code} do pytest).", file=sys.stderr)
        return 2
    serial = serial_ids(stdout)

    workdir = Path(tempfile.mkdtemp(prefix="cq-compare-"))
    try:
        junit = workdir / "junit.xml"
        print(f"Executando a suíte em paralelo com {args.workers} processos...", file=sys.stderr)
        run_code, _ = _unpack(runner(["-m", MARKER, "-n", str(args.workers), "--dist", "worksteal",
                                      f"--junitxml={junit}", *paths]))
        if run_code not in _RUN_COMPLETED:
            print(f"A execução paralela não foi até o fim (código {run_code} do pytest).", file=sys.stderr)
            return 2
        if run_code == 1:
            print("Aviso: houve casos com falha na execução paralela; a comparação usa o conjunto executado.",
                  file=sys.stderr)
        try:
            parallel = junit_ids(junit)
        except (OSError, ET.ParseError):
            print("O JUnit da execução paralela está ausente ou ilegível.", file=sys.stderr)
            return 2
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return compare(serial, parallel, out=sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
