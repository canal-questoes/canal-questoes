"""Passos de teste do CI, com o
executor do `pytest` injetado e JUnits de teste (nenhum `pytest` de verdade roda).

O script (`ops/ci/ci_suite.py`, carregado por `tests/helpers/ops.load_ops`):
- `load_known_nodes(path)`: só as entradas por nó (o texto antes do TAB); as `re:`, os comentários e as linhas vazias
  ignorados (o formato da lista local de instáveis conhecidos, `KNOWN_FLAKY_LOCAL`: `<nó><TAB><motivo>`, o nó podendo vir sem o
  parâmetro, valendo para todos os parâmetros);
- `failed_nodes(junit_paths)`: os nós com `failure` ou `error` nos JUnit (xunit2: `classname` com pontos e `name` com o
  parâmetro), normalizados para `arquivo::nome[parâmetro]`;
- `run(kind, *, workers, junit_dir, summary, pytest)`: o `fast` roda `-m "not system" -n <N> --dist worksteal
  --junitxml=<arquivo em junit_dir>`; o `full`, depois, `-m system` sem `-n`; se as falhas forem **todas** da lista,
  repete uma vez **só** esses nós (os argumentos de nó da repetição são exatamente os que falharam) e o resultado da
  repetição decide; falha fora da lista → falha sem repetição; o resumo lista a repetição;
- `main(argv, *, env, pytest)`: o resumo vai a `--summary` ou, sem ele, a `$GITHUB_STEP_SUMMARY` do `env` injetado.

**[I] Injeção das listas (minha; nada fixa onde o `run` acha a lista):** os testes trocam as constantes
`KNOWN_FLAKY_CI` e `KNOWN_FLAKY_LOCAL` do módulo por caminhos **absolutos** no `tmp_path` (`monkeypatch.setattr`) e
mudam o diretório atual para o `tmp_path`; a implementação as lê na chamada (não na importação). Um caminho absoluto
vence tanto a junção com a raiz do repositório (`Path` descarta a esquerda) quanto a leitura relativa ao diretório
atual. Em cada caso só uma das duas existe (no CI, a gerada pela exportação; na validação local da árvore de
trabalho, a do `docs/execucao`). **[I]** O executor falso grava o JUnit no caminho do `--junitxml=` de cada chamada
(também na repetição, quando houver) e devolve o código coerente com ele (0 sem falha, 1 com falha).

Os textos do resumo são os de `CI_MESSAGES` (reais no esqueleto; [I]). No esqueleto, toda função lança
`NotImplementedError`.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from tests.helpers.ops import load_ops

SCRIPT = "ops/ci/ci_suite.py"

FLAKY = "tests/integration/test_dispatch_closing.py::test_entries_after_time_with_dispatch_open_wait_for_transition"
FLAKY_BY_NAME = "tests/integration/test_engine_failures.py::test_second_loss_of_the_tree_creates_a_round_two_refetch"
FLAKY_PARAM = FLAKY_BY_NAME + "[r2]"
OTHER = "tests/unit/test_pacing.py::test_configured_bot_rate_is_honored"
PASSING = ("tests/unit/test_pacing.py::test_penalize_blocks_the_chat_for_retry_after",
           "tests/unit/test_decimal_comma.py::test_other_field_types_never_normalize[ 2,0 -integer]")

KNOWN_LIST = "\n".join([
    "# Instáveis conhecidos (lista de teste no formato da lista local de instáveis conhecidos).",
    "# <nó do pytest><TAB><motivo>; re:<expressão regular><TAB><motivo>",
    "",
    f"{FLAKY}\tcorrida do teste congelado",
    f"{FLAKY_BY_NAME}\to nó sem parâmetro vale para todos os parâmetros",
    "re:10048|Address already in use\tWindows: esgotamento de portas efêmeras",
    "re:tests/unit/test_pacing\\.py::.*\tuma expressão que casaria OTHER, ignorada",
    "",
]) + "\n"


def _module():
    return load_ops(SCRIPT)


def _junit(passed: Sequence[str], failed: Sequence[str] = (), errored: Sequence[str] = ()) -> str:
    def case(node: str, body: str) -> str:
        file_part, name = node.split("::", 1)
        classname = file_part.removesuffix(".py").replace("/", ".")
        return f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} time=\"0.010\">{body}</testcase>"

    cases = [case(n, "") for n in passed]
    cases += [case(n, '<failure message="assert False">AssertionError</failure>') for n in failed]
    cases += [case(n, '<error message="erro na fixture">RuntimeError</error>') for n in errored]
    total = len(passed) + len(failed) + len(errored)
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
            f'errors="{len(errored)}" failures="{len(failed)}" skipped="0" tests="{total}" time="1.0">'
            + "".join(cases) + "</testsuite></testsuites>")


class FakePytest:
    """Executor falso. `outcomes` diz, por chamada (`main`, `system` ou `rerun`), quais nós falham; os demais nós
    da chamada passam. A chamada é reconhecida pelos argumentos: com `-n` é a principal; com `-m system` (sem
    `not`), a de sistema; com argumentos de nó (`::`), a repetição."""

    def __init__(self, *, main: Sequence[str] = (), system: Sequence[str] = (), rerun: Sequence[str] = ()) -> None:
        self.outcomes = {"main": list(main), "system": list(system), "rerun": list(rerun)}
        self.calls: list[tuple[str, list[str]]] = []

    @staticmethod
    def kind_of(args: Sequence[str]) -> str:
        if any("::" in a for a in args):
            return "rerun"
        if "-n" in args:
            return "main"
        if "-m" in args and args[args.index("-m") + 1] == "system":
            return "system"
        raise AssertionError(f"chamada do pytest não reconhecida: {list(args)}")

    def __call__(self, args: Sequence[str]) -> int:
        args = list(args)
        kind = self.kind_of(args)
        self.calls.append((kind, args))
        failed = self.outcomes[kind]
        if kind == "rerun":
            nodes = [a for a in args if "::" in a]
            passed = [n for n in nodes if n not in failed]
            failed = [n for n in nodes if n in failed]
        else:
            passed = list(PASSING)
        for junit in (a.split("=", 1)[1] for a in args if a.startswith("--junitxml=")):
            Path(junit).parent.mkdir(parents=True, exist_ok=True)
            Path(junit).write_text(_junit(passed, failed), encoding="utf-8")
        return 1 if failed else 0

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def args(self, kind: str) -> list[str]:
        found = [args for k, args in self.calls if k == kind]
        assert len(found) == 1, f"chamadas {kind}: {len(found)}"
        return found[0]


@pytest.fixture
def lists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A lista do CI no `tmp_path` (caminho absoluto) e a local apontada para um arquivo inexistente."""
    module = _module()
    ci_list = tmp_path / "listas" / "known-flaky.txt"
    ci_list.parent.mkdir()
    ci_list.write_text(KNOWN_LIST, encoding="utf-8")
    monkeypatch.setattr(module, "KNOWN_FLAKY_CI", str(ci_list))
    monkeypatch.setattr(module, "KNOWN_FLAKY_LOCAL", str(tmp_path / "listas" / "inexistente.txt"))
    monkeypatch.chdir(tmp_path)
    return ci_list


def _pair(args: list[str], flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None


def _run(kind: str, fake: FakePytest, tmp_path: Path, *, workers: int = 4) -> tuple[int, str]:
    summary = io.StringIO()
    code = _module().run(kind, workers=workers, junit_dir=tmp_path / "junit", summary=summary, pytest=fake)
    return code, summary.getvalue()


# --- leitura das listas e dos JUnit -----------------------------------------------------------------------------


def test_load_known_nodes_keeps_only_node_entries(tmp_path: Path) -> None:
    """só as entradas por nó, sem o motivo; comentários, linhas vazias e `re:` ignorados."""
    path = tmp_path / "instaveis.txt"
    path.write_text(KNOWN_LIST, encoding="utf-8")
    assert _module().load_known_nodes(path) == frozenset({FLAKY, FLAKY_BY_NAME})


def test_failed_nodes_reads_failures_and_errors_from_every_junit(tmp_path: Path) -> None:
    """Falhas e erros (de vários arquivos) normalizados para `arquivo::nome[parâmetro]`; os que passaram, fora."""
    first = tmp_path / "a.xml"
    second = tmp_path / "b.xml"
    first.write_text(_junit(PASSING, failed=[FLAKY]), encoding="utf-8")
    second.write_text(_junit([], errored=[FLAKY_PARAM]), encoding="utf-8")
    assert _module().failed_nodes([first, second]) == {FLAKY, FLAKY_PARAM}


# --- o passo do CI -------------------------------------------------------------------------------------------------


def test_fast_green_runs_once_in_parallel_without_rerun(lists: Path, tmp_path: Path) -> None:
    """o `fast` roda uma vez `-m "not system" -n <N> --dist worksteal --junitxml=<em junit_dir>`;
    verde → 0, sem repetição, sem a parte de sistema; o resumo diz verde."""
    fake = FakePytest()
    code, summary = _run("fast", fake, tmp_path, workers=3)
    assert code == 0
    assert fake.kinds() == ["main"]
    args = fake.args("main")
    assert _pair(args, "-m") == "not system" and _pair(args, "-n") == "3" and _pair(args, "--dist") == "worksteal"
    junit = [a.split("=", 1)[1] for a in args if a.startswith("--junitxml=")]
    assert len(junit) == 1 and Path(junit[0]).resolve().is_relative_to((tmp_path / "junit").resolve()), junit
    module = _module()
    assert module.CI_MESSAGES["passed"] in summary
    assert module.CI_MESSAGES["rerun_header"] not in summary


@pytest.mark.parametrize("failed", [[FLAKY], [FLAKY, FLAKY_PARAM]], ids=["um-no", "no-e-parametro"])
def test_failure_only_of_known_nodes_reruns_exactly_those_once_and_the_rerun_decides(
        lists: Path, tmp_path: Path, failed: list[str]) -> None:
    """falha só de nós da lista (inclusive um parâmetro de um nó listado sem parâmetro) →
    uma repetição só, com exatamente esses nós como argumentos de nó, sem `-n`; a repetição passa → 0; o resumo
    cita a repetição e cada nó repetido."""
    fake = FakePytest(main=failed)
    code, summary = _run("fast", fake, tmp_path)
    assert code == 0, summary
    assert fake.kinds() == ["main", "rerun"]
    rerun = fake.args("rerun")
    assert sorted(a for a in rerun if "::" in a) == sorted(failed), rerun
    assert "-n" not in rerun
    module = _module()
    assert module.CI_MESSAGES["rerun_header"] in summary
    for node in failed:
        assert module.CI_MESSAGES["rerun_node"].format(node=node) in summary
    assert module.CI_MESSAGES["rerun_passed"] in summary


def test_known_node_that_fails_again_fails_the_step(lists: Path, tmp_path: Path) -> None:
    """A repetição falha de novo → código ≠ 0; o resumo cita a repetição e o fracasso; nenhuma segunda
    repetição."""
    fake = FakePytest(main=[FLAKY], rerun=[FLAKY])
    code, summary = _run("fast", fake, tmp_path)
    assert code != 0
    assert fake.kinds() == ["main", "rerun"]
    module = _module()
    assert module.CI_MESSAGES["rerun_header"] in summary and module.CI_MESSAGES["rerun_failed"] in summary


@pytest.mark.parametrize("failed", [[OTHER], [OTHER, FLAKY]], ids=["fora-da-lista", "misturada"])
def test_failure_outside_the_list_fails_without_rerun(lists: Path, tmp_path: Path, failed: list[str]) -> None:
    """Qualquer falha fora da lista (sozinha ou junto de uma da lista; a `re:` que casaria o nome não conta) →
    código ≠ 0, sem repetição; o resumo diz vermelho e não tem o cabeçalho da repetição."""
    fake = FakePytest(main=failed)
    code, summary = _run("fast", fake, tmp_path)
    assert code != 0
    assert fake.kinds() == ["main"]
    module = _module()
    assert module.CI_MESSAGES["failed"] in summary
    assert module.CI_MESSAGES["rerun_header"] not in summary


def test_the_local_list_is_used_when_the_ci_list_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """sem `ops/ci/known-flaky.txt`, vale a lista local de instáveis conhecidos (`KNOWN_FLAKY_LOCAL`)
    filtrada (só as entradas por nó)."""
    module = _module()
    local = tmp_path / "instaveis-conhecidos.txt"
    local.write_text(KNOWN_LIST, encoding="utf-8")
    monkeypatch.setattr(module, "KNOWN_FLAKY_CI", str(tmp_path / "inexistente.txt"))
    monkeypatch.setattr(module, "KNOWN_FLAKY_LOCAL", str(local))
    monkeypatch.chdir(tmp_path)
    fake = FakePytest(main=[FLAKY])
    code, _ = _run("fast", fake, tmp_path)
    assert code == 0
    assert fake.kinds() == ["main", "rerun"]


def test_full_runs_the_system_part_after_in_series(lists: Path, tmp_path: Path) -> None:
    """o `full` roda a principal (com `-n`) e, depois, `-m system` sem `-n` nem `--dist`; os dois
    verdes → 0."""
    fake = FakePytest()
    code, _ = _run("full", fake, tmp_path, workers=2)
    assert code == 0
    assert fake.kinds() == ["main", "system"]
    system = fake.args("system")
    assert _pair(system, "-m") == "system"
    assert "-n" not in system and not any(a.startswith("--dist") for a in system), system


def test_full_fails_when_the_system_part_fails_outside_the_list(lists: Path, tmp_path: Path) -> None:
    """Uma falha da parte de sistema fora da lista → código ≠ 0, sem repetição."""
    fake = FakePytest(system=["tests/system/test_compose.py::test_stack_comes_up"])
    code, summary = _run("full", fake, tmp_path)
    assert code != 0
    assert "rerun" not in fake.kinds()
    assert _module().CI_MESSAGES["failed"] in summary


# --- linha de comando ------------------------------------------------------------------------------------------------


def test_main_writes_the_summary_to_github_step_summary_from_the_injected_env(lists: Path, tmp_path: Path) -> None:
    """sem `--summary`, o resumo vai ao arquivo de `$GITHUB_STEP_SUMMARY` do `env` injetado; o código
    é o do passo; `--workers` chega ao `-n`."""
    summary_file = tmp_path / "step-summary.md"
    fake = FakePytest(main=[FLAKY])
    code = _module().main(["fast", "--workers", "5", "--junit-dir", str(tmp_path / "junit")],
                          env={"GITHUB_STEP_SUMMARY": str(summary_file)}, pytest=fake)
    assert code == 0
    assert _pair(fake.args("main"), "-n") == "5"
    module = _module()
    text = summary_file.read_text(encoding="utf-8")
    assert module.CI_MESSAGES["rerun_header"] in text
    assert module.CI_MESSAGES["rerun_node"].format(node=FLAKY) in text


def test_main_summary_option_wins_over_the_environment(lists: Path, tmp_path: Path) -> None:
    """`--summary <arquivo>` recebe o resumo, e o arquivo de `$GITHUB_STEP_SUMMARY` não é criado."""
    chosen = tmp_path / "resumo.md"
    ignored = tmp_path / "nao-usar.md"
    fake = FakePytest(main=[OTHER])
    code = _module().main(["fast", "--junit-dir", str(tmp_path / "junit"), "--summary", str(chosen)],
                          env={"GITHUB_STEP_SUMMARY": str(ignored)}, pytest=fake)
    assert code != 0
    assert _module().CI_MESSAGES["failed"] in chosen.read_text(encoding="utf-8")
    assert not ignored.exists()
