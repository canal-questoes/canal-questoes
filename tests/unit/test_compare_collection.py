"""Comparação da coleta em série com a execução paralela,
com um executor de `pytest` falso (nenhum `pytest` de verdade roda).

O script (`scripts/compare_collection.py`, carregado por `tests/helpers/ops.load_ops`):
- `serial_ids(output)` lê a saída de `pytest --collect-only -q` e normaliza cada id para
  `arquivo::nome[parâmetro]`, ignorando a linha final de contagem (com ou sem desmarcados) e as linhas vazias;
- `junit_ids(path)` lê os `testcase` do JUnit do controlador e normaliza do mesmo jeito (`classname` com pontos →
  caminho do arquivo com `/` e `.py`; `name` com o parâmetro);
- `compare(serial, parallel, *, out)`: iguais → 0; um id a mais, a menos ou com nome diferente → código ≠ 0, com a
  contagem dos dois lados e **só** os ids divergentes em `out`;
- `main(argv, *, pytest)`: os argv do executor são `--collect-only -q -m "not system"` (coleta em série) e
  `-m "not system" -n <N> --dist worksteal --junitxml=<tmp>` (a execução paralela real; o `--collect-only` desliga
  o xdist).

**[F]** Os formatos, conferidos neste computador em 2026-10-29 com o pytest e o pytest-xdist do `uv.lock`
(`--collect-only -q` e `-n 2 --dist worksteal --junitxml` sobre `tests/unit/test_decimal_comma.py`): a coleta lista
um id por linha (`tests/unit/test_decimal_comma.py::test_other_field_types_never_normalize[ 2,0 -integer]`, com os
espaços do parâmetro), uma linha vazia e `66 tests collected in 0.15s`, ou `66/89 tests collected (23 deselected)
in 0.33s` com desmarcados; o JUnit (xunit2) traz `<testsuites><testsuite name="pytest" …><testcase
classname="tests.unit.test_decimal_comma" name="test_other_field_types_never_normalize[ 2,0 -integer]" …/>`, sem
`file`. **[I]** A suíte não tem classes de teste (`grep "^class Test" tests`, vazio), então a normalização com classe
não é conferida; o executor falso devolve `(código, stdout)`, a forma do docstring do esqueleto.

No esqueleto, toda função lança `NotImplementedError`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from tests.helpers.ops import load_ops

SCRIPT = "scripts/compare_collection.py"

IDS = (
    "tests/unit/test_decimal_comma.py::test_other_field_types_never_normalize[ 2,0 -integer]",
    "tests/unit/test_decimal_comma.py::test_number_field_normalizes_exactly_one_comma_and_no_point[simple]",
    "tests/unit/test_pacing.py::test_configured_bot_rate_is_honored",
    "tests/integration/test_reports_api.py::test_get_rejects_invalid_format_and_version_in_the_json_envelope[a.b-c]",
)


def _module():
    return load_ops(SCRIPT)


def _collect_output(ids: Sequence[str], *, deselected: int = 0) -> str:
    lines = list(ids) + [""]
    if deselected:
        lines.append(f"{len(ids)}/{len(ids) + deselected} tests collected ({deselected} deselected) in 0.33s")
    else:
        lines.append(f"{len(ids)} tests collected in 0.15s")
    return "\n".join(lines) + "\n"


def _junit(ids: Sequence[str], *, failed: Sequence[str] = ()) -> str:
    cases = []
    for node in ids:
        file_part, name = node.split("::", 1)
        classname = file_part.removesuffix(".py").replace("/", ".")
        body = '<failure message="falhou">AssertionError</failure>' if node in failed else ""
        cases.append(f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} time=\"0.010\">"
                     f"{body}</testcase>")
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
            f'errors="0" failures="{len(failed)}" skipped="0" tests="{len(ids)}" time="1.0">'
            + "".join(cases) + "</testsuite></testsuites>")


class FakePytest:
    """Executor falso: a coleta devolve `serial` como stdout; a execução grava o JUnit com `parallel` no caminho do
    `--junitxml=` e devolve `run_code`."""

    def __init__(self, serial: Sequence[str], parallel: Sequence[str], *, run_code: int = 0,
                 failed: Sequence[str] = ()) -> None:
        self.serial = serial
        self.parallel = parallel
        self.run_code = run_code
        self.failed = failed
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        args = list(args)
        self.calls.append(args)
        if "--collect-only" in args:
            return 0, _collect_output(self.serial, deselected=3)
        junit = [a.split("=", 1)[1] for a in args if a.startswith("--junitxml=")]
        assert len(junit) == 1, f"a execução paralela sem um --junitxml=: {args}"
        Path(junit[0]).write_text(_junit(self.parallel, failed=self.failed), encoding="utf-8")
        return self.run_code, ""


def _pair(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def test_serial_ids_reads_collect_only_q_and_skips_the_count_line() -> None:
    """um id por linha, normalizado; a linha de contagem (com e sem desmarcados) e as vazias ignoradas; os
    espaços do parâmetro preservados."""
    module = _module()
    assert module.serial_ids(_collect_output(IDS)) == list(IDS)
    assert module.serial_ids(_collect_output(IDS, deselected=23)) == list(IDS)
    assert module.serial_ids("\n" + _collect_output(IDS[:1])) == [IDS[0]]


def test_junit_ids_reads_the_controller_testcases_and_normalizes_like_the_collection(tmp_path: Path) -> None:
    """os `testcase` do JUnit (xunit2) viram `arquivo::nome[parâmetro]`, inclusive os que falharam; a mesma
    forma do `serial_ids`."""
    module = _module()
    path = tmp_path / "junit.xml"
    path.write_text(_junit(IDS, failed=[IDS[2]]), encoding="utf-8")
    assert sorted(module.junit_ids(path)) == sorted(IDS)


def test_compare_equal_sets_is_zero_and_prints_the_count() -> None:
    """Iguais (a ordem não importa) → 0, com a contagem na saída."""
    module = _module()
    out = io.StringIO()
    assert module.compare(list(IDS), list(reversed(IDS)), out=out) == 0
    assert str(len(IDS)) in out.getvalue()


@pytest.mark.parametrize("change", ["extra", "missing", "renamed"])
def test_compare_reports_only_the_divergent_ids_with_the_counts(change: str) -> None:
    """Um id a mais, a menos ou com nome diferente → código ≠ 0; a saída tem as duas contagens e só os ids
    divergentes (nenhum dos iguais)."""
    module = _module()
    serial = list(IDS)
    parallel = list(IDS)
    if change == "extra":
        parallel.append("tests/unit/test_pacing.py::test_extra_only_in_parallel")
        divergent = ["tests/unit/test_pacing.py::test_extra_only_in_parallel"]
    elif change == "missing":
        parallel.remove(IDS[1])
        divergent = [IDS[1]]
    else:
        parallel[3] = IDS[3].replace("[a.b-c]", "[a.b-d]")
        divergent = [IDS[3], parallel[3]]
    out = io.StringIO()
    code = module.compare(serial, parallel, out=out)
    text = out.getvalue()
    assert code != 0
    assert str(len(serial)) in text and str(len(parallel)) in text
    for node in divergent:
        assert node in text, f"falta o id divergente {node}"
    for node in set(serial) & set(parallel):
        assert node not in text, f"id igual nos dois lados apareceu na saída: {node}"


@pytest.mark.parametrize(("workers", "parallel", "expected_zero"), [
    (6, IDS, True),
    (4, IDS[:-1], False),
], ids=["iguais", "faltando-um"])
def test_main_runs_serial_collection_and_a_real_parallel_run_with_junit(
        workers: int, parallel: Sequence[str], expected_zero: bool, capsys: pytest.CaptureFixture[str]) -> None:
    """o `main` chama o executor duas vezes, a coleta em série
    (`--collect-only -q -m "not system"`, sem `-n`) e a execução real (`-m "not system" -n <N> --dist worksteal
    --junitxml=<tmp>`, sem `--collect-only`); compara os ids normalizados (0 se iguais, ≠ 0 se não); o temporário
    do JUnit não fica para trás."""
    module = _module()
    fake = FakePytest(IDS, parallel, run_code=0)
    code = module.main(["--workers", str(workers)], pytest=fake)
    assert (code == 0) is expected_zero, (code, capsys.readouterr())

    assert len(fake.calls) == 2, fake.calls
    serial_call = next(c for c in fake.calls if "--collect-only" in c)
    run_call = next(c for c in fake.calls if "--collect-only" not in c)
    assert "-q" in serial_call and _pair(serial_call, "-m") == "not system", serial_call
    assert "-n" not in serial_call and not any(a.startswith("--junitxml") for a in serial_call)
    assert _pair(run_call, "-m") == "not system", run_call
    assert _pair(run_call, "-n") == str(workers), run_call
    assert _pair(run_call, "--dist") == "worksteal", run_call
    junit = [a.split("=", 1)[1] for a in run_call if a.startswith("--junitxml=")]
    assert len(junit) == 1
    assert not Path(junit[0]).exists(), "o JUnit temporário ficou para trás"

