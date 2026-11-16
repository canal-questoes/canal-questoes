"""Testes da opção de cobertura dos passos de teste do CI, `ops/ci/ci_suite.py --coverage-xml <arquivo>`, com o
executor falso do `pytest` de `tests/unit/test_ci_suite.py` (importado sem mudar aquele arquivo; nenhum `pytest` de
verdade roda).

O que se confere:
- com a opção, a chamada principal ganha `--cov --cov-report=xml:<arquivo> --cov-report=` e a repetição dos
  instáveis, `--cov --cov-append --cov-report=xml:<arquivo> --cov-report=`; os outros argumentos de cada chamada ficam
  os mesmos, na mesma ordem;
- a parte de sistema do `full` não mede (nenhum argumento `--cov`);
- o código de saída e o resumo são os mesmos de uma execução sem a opção (o resumo não fala da cobertura);
- sem a opção, nenhuma chamada leva `--cov`.

**No esqueleto:** com a opção, o `run` e o `main` lançam `NotImplementedError`.
O comportamento sem a opção, já real, é conferido na mesma função de cada caso com a opção, como a referência da
comparação.

**[I]** Escolhas deste teste:
- o caminho de `xml:` é comparado pelo caminho resolvido (relativo ao diretório atual), não pelo texto;
- os três (ou quatro) argumentos da cobertura são conferidos na ordem do contrato, entre os argumentos da chamada,
  em qualquer posição;
- as listas de instáveis são injetadas como em `tests/unit/test_ci_suite.py`: as constantes do módulo trocadas por
  caminhos absolutos no `tmp_path`, com o diretório atual no `tmp_path`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.helpers.ops import load_ops
from tests.unit.test_ci_suite import FLAKY, FLAKY_PARAM, KNOWN_LIST, OTHER, FakePytest

SCRIPT = "ops/ci/ci_suite.py"
COVERAGE_PREFIX = "--cov"
XML_PREFIX = "--cov-report=xml:"
SYSTEM_NODE = "tests/system/test_compose.py::test_stack_comes_up"


def _module():
    return load_ops(SCRIPT)


@pytest.fixture
def flaky_lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A lista do CI no `tmp_path` (caminho absoluto), a local apontada para um arquivo inexistente e o diretório
    atual no `tmp_path`."""
    module = _module()
    ci_list = tmp_path / "listas" / "known-flaky.txt"
    ci_list.parent.mkdir()
    ci_list.write_text(KNOWN_LIST, encoding="utf-8")
    monkeypatch.setattr(module, "KNOWN_FLAKY_CI", str(ci_list))
    monkeypatch.setattr(module, "KNOWN_FLAKY_LOCAL", str(tmp_path / "listas" / "inexistente.txt"))
    monkeypatch.chdir(tmp_path)
    return ci_list


def _make_fake(outcomes: dict[str, Sequence[str]]) -> FakePytest:
    return FakePytest(main=outcomes.get("main", ()), system=outcomes.get("system", ()),
                      rerun=outcomes.get("rerun", ()))


def _run(kind: str, fake: FakePytest, tmp_path: Path, coverage_xml: Path | None = None) -> tuple[int, str]:
    summary = io.StringIO()
    kwargs = {} if coverage_xml is None else {"coverage_xml": coverage_xml}
    code = _module().run(kind, workers=4, junit_dir=tmp_path / "junit", summary=summary, pytest=fake, **kwargs)
    return code, summary.getvalue()


def _coverage_args(args: Sequence[str]) -> list[str]:
    return [arg for arg in args if arg.startswith(COVERAGE_PREFIX)]


def _other_args(args: Sequence[str]) -> list[str]:
    return [arg for arg in args if not arg.startswith(COVERAGE_PREFIX)]


def _assert_measured(args: Sequence[str], target: Path, *, append: bool) -> None:
    """Os argumentos da cobertura da chamada, na ordem do contrato, com o `xml:` no arquivo dado."""
    found = _coverage_args(args)
    expected_flags = ["--cov", "--cov-append"] if append else ["--cov"]
    assert len(found) == len(expected_flags) + 2, found
    assert found[:len(expected_flags)] == expected_flags, found
    xml, empty = found[len(expected_flags):]
    assert xml.startswith(XML_PREFIX), found
    assert Path(xml[len(XML_PREFIX):]).resolve() == target.resolve(), found
    assert empty == "--cov-report=", found


def _assert_no_coverage_text(summary: str) -> None:
    lowered = summary.casefold()
    assert "--cov" not in lowered and "coverage" not in lowered and "cobertura" not in lowered, summary


def _baseline(kind: str, outcomes: dict[str, Sequence[str]], tmp_path: Path) -> tuple[FakePytest, int, str]:
    """A execução sem a opção: nenhuma chamada com `--cov`."""
    fake = _make_fake(outcomes)
    code, summary = _run(kind, fake, tmp_path)
    for kind_of_call, args in fake.calls:
        assert _coverage_args(args) == [], (kind_of_call, args)
    _assert_no_coverage_text(summary)
    return fake, code, summary


# --- o run ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("outcomes", [
    pytest.param({}, id="verde"),
    pytest.param({"main": [OTHER]}, id="falha-fora-da-lista"),
    pytest.param({"main": [OTHER, FLAKY]}, id="falha-misturada"),
])
def test_fast_measures_only_the_main_call(flaky_lists: Path, tmp_path: Path,
                                          outcomes: dict[str, Sequence[str]]) -> None:
    """Sem repetição: com a opção, a chamada principal leva `--cov --cov-report=xml:<arquivo> --cov-report=` e os
    mesmos outros argumentos; as chamadas, o código e o resumo são os mesmos de sem a opção. No esqueleto,
    o `run` com a opção lança `NotImplementedError`."""
    base_fake, base_code, base_summary = _baseline("fast", outcomes, tmp_path)
    assert base_fake.kinds() == ["main"]

    target = tmp_path / "coverage.xml"
    fake = _make_fake(outcomes)
    code, summary = _run("fast", fake, tmp_path, target)

    assert fake.kinds() == base_fake.kinds()
    main = fake.args("main")
    _assert_measured(main, target, append=False)
    assert _other_args(main) == base_fake.args("main")
    assert (code, summary) == (base_code, base_summary)
    _assert_no_coverage_text(summary)


@pytest.mark.parametrize("outcomes", [
    pytest.param({"main": [FLAKY]}, id="repeticao-passa"),
    pytest.param({"main": [FLAKY, FLAKY_PARAM]}, id="no-e-parametro"),
    pytest.param({"main": [FLAKY], "rerun": [FLAKY]}, id="repeticao-falha"),
])
def test_rerun_appends_to_the_same_coverage_file(flaky_lists: Path, tmp_path: Path,
                                                 outcomes: dict[str, Sequence[str]]) -> None:
    """Com a repetição dos instáveis: a principal mede, e a repetição leva `--cov --cov-append
    --cov-report=xml:<arquivo> --cov-report=`, com os mesmos nós e outros argumentos; o código e o resumo são os
    mesmos de sem a opção. No esqueleto, o `run` com a opção lança
    `NotImplementedError`."""
    base_fake, base_code, base_summary = _baseline("fast", outcomes, tmp_path)
    assert base_fake.kinds() == ["main", "rerun"]

    target = tmp_path / "saida" / "coverage.xml"
    fake = _make_fake(outcomes)
    code, summary = _run("fast", fake, tmp_path, target)

    assert fake.kinds() == ["main", "rerun"]
    _assert_measured(fake.args("main"), target, append=False)
    rerun = fake.args("rerun")
    _assert_measured(rerun, target, append=True)
    assert _other_args(fake.args("main")) == base_fake.args("main")
    assert _other_args(rerun) == base_fake.args("rerun")
    assert (code, summary) == (base_code, base_summary)
    _assert_no_coverage_text(summary)


@pytest.mark.parametrize("outcomes", [
    pytest.param({}, id="verde"),
    pytest.param({"system": [SYSTEM_NODE]}, id="sistema-falha"),
    pytest.param({"main": [FLAKY]}, id="principal-repete"),
])
def test_full_does_not_measure_the_system_part(flaky_lists: Path, tmp_path: Path,
                                               outcomes: dict[str, Sequence[str]]) -> None:
    """No `full`, com a opção, só a principal (e a repetição, quando houver) mede; a chamada de sistema fica igual à
    de sem a opção, sem nenhum `--cov`; o código e o resumo são os mesmos. No esqueleto,
    o `run` com a opção lança `NotImplementedError`."""
    base_fake, base_code, base_summary = _baseline("full", outcomes, tmp_path)
    assert "system" in base_fake.kinds()

    target = tmp_path / "coverage.xml"
    fake = _make_fake(outcomes)
    code, summary = _run("full", fake, tmp_path, target)

    assert fake.kinds() == base_fake.kinds()
    _assert_measured(fake.args("main"), target, append=False)
    system = fake.args("system")
    assert _coverage_args(system) == [], system
    assert system == base_fake.args("system")
    if "rerun" in fake.kinds():
        _assert_measured(fake.args("rerun"), target, append=True)
    assert (code, summary) == (base_code, base_summary)
    _assert_no_coverage_text(summary)


# --- a linha de comando --------------------------------------------------------------------------------------------


def test_main_accepts_the_coverage_option(flaky_lists: Path, tmp_path: Path) -> None:
    """`main` com `--coverage-xml coverage.xml` (relativo ao diretório atual) passa o arquivo ao passo: a principal
    mede nele; o código e o resumo em `--summary` são os de sem a opção. Sem a opção, nenhum `--cov`; a opção sem
    valor continua `invalid_arguments` (2). No esqueleto, o `main` com a opção lança
    `NotImplementedError` depois de ler os argumentos."""
    module = _module()
    junit = str(tmp_path / "junit")

    base_fake = FakePytest(main=[FLAKY])
    base_summary = tmp_path / "resumo-sem.md"
    base_code = module.main(["fast", "--junit-dir", junit, "--summary", str(base_summary)], env={}, pytest=base_fake)
    assert base_fake.kinds() == ["main", "rerun"]
    assert all(_coverage_args(args) == [] for _, args in base_fake.calls)
    assert module.main(["fast", "--coverage-xml"], env={}, pytest=FakePytest()) == 2

    fake = FakePytest(main=[FLAKY])
    summary = tmp_path / "resumo-com.md"
    code = module.main(["fast", "--junit-dir", junit, "--summary", str(summary), "--coverage-xml", "coverage.xml"],
                       env={}, pytest=fake)

    assert code == base_code
    assert fake.kinds() == ["main", "rerun"]
    _assert_measured(fake.args("main"), tmp_path / "coverage.xml", append=False)
    _assert_measured(fake.args("rerun"), tmp_path / "coverage.xml", append=True)
    assert _other_args(fake.args("main")) == base_fake.args("main")
    assert summary.read_text(encoding="utf-8") == base_summary.read_text(encoding="utf-8")
