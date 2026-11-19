"""Os workflows do GitHub Actions e o Dependabot da árvore pública, lidos como YAML (`yaml.safe_load`) e como texto.

A árvore julgada é a fixture `public_root` (`tests/helpers/public_tree.py`): na árvore exportada, a própria raiz; no
repositório de origem, a exportação do último commit num destino temporário. Toda leitura passa por ela.

Quatro regras, uma função cada:
- as regras de todo `.github/workflows/*.yml`: cada linha `uses:` como `dono/nome[/caminho]@<40 hex> # vX.Y.Z`; as
  `permissions` do topo presentes e só de leitura (`read` em todo valor, ou `read-all`); escrita só nos jobs da
  tabela (`ci.yml`, job `fast`: `id-token`); nenhum `secrets.`, nenhum `github.token`, nenhum nome de `env` com cara
  de segredo, nenhuma URL do Telegram nem do SMTP;
- o job `fast` do `ci.yml`: as permissões exatas `{contents: read, id-token: write}` e a lista fechada dos passos, na
  ordem (os de base, reconhecidos pelo `uses:` ou pelo começo do `run:`, mais o lint, o resumo da cobertura e o envio
  ao Codecov), para nenhum passo novo herdar a permissão de escrita sem reabrir este teste;
- nenhum workflow do Scorecard do OpenSSF: nem um arquivo com `scorecard` no nome, nem a ação `ossf/scorecard-action`
  em outro workflow;
- o `.github/dependabot.yml`: as seis entradas (cinco ecossistemas), semanais, com os diretórios existentes na árvore
  pública, a imagem do PrairieLearn no `ignore` e nada para as imagens de teste.

Os auxiliares de leitura e as regras de texto do teste do CI (`tests/contract/test_ci_workflow.py`) são importados sem
mudar aquele arquivo.

**Vermelho no esqueleto:** todas as funções ficavam vermelhas enquanto o workflow do Scorecard e o arquivo do
Dependabot não existiam (arquivo ausente); depois, a do `fast`, até o `ci.yml` ganhar o lint, o resumo e o envio da
cobertura. As regras que os workflows do esqueleto já cumpriam ficam nas mesmas funções.

**Reaberto em 2026-11-16:** o projeto deixou de usar o Scorecard. O `scorecard.yml` saiu do conjunto
obrigatório e da tabela da escrita, e a função dele virou a da ausência, vermelha enquanto o `scorecard.yml` existir.

**[I]** Escolhas deste teste:
- o conjunto dos workflows tem de conter `ci.yml` e `engine.yml` (pode ter outros, que seguem as regras comuns e não
  podem ser do Scorecard); as regras valem também para um `.yaml`;
- um job sem `permissions` herda o topo; num job, o valor em texto só pode ser `read-all`, e os valores de um
  mapeamento só `read`, `write` ou `none`; a função das regras comuns confere que a escrita fica dentro da tabela, e a
  do `fast` confere as permissões exatas;
- no `fast`, o lint pode ficar em qualquer lugar entre o `uv sync --locked` e a suíte; os passos de base são
  reconhecidos pelo nome da ação (sem o SHA, que a regra comum já confere) ou pelo começo do `run:`; o resumo é o
  passo cujo `run:` tem o `coverage report` em Markdown, e as duas linhas fixas do escopo e da ressalva são conferidas
  pelas frases inteiras;
- no `fast`, o lint só bloqueia se o passo não tiver `continue-on-error` nem `if`: as chaves do passo do lint ficam
  contidas em `{name, run}`, e o job `fast` não tem `continue-on-error`;
- na ausência do Scorecard, o nome do arquivo é conferido sem diferenciar maiúsculas, e a ação, pelo texto do
  workflow, também sem diferenciar maiúsculas;
- no Dependabot, um `directory` vale pela pasta na árvore pública (`/` é a raiz); a chave `directories` não é aceita.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.contract.test_ci_workflow import (
    FORBIDDEN_TEXT,
    PINNED_ACTION,
    SECRET_NAME,
    USES_LINE,
    VERSION_COMMENT,
    _envs,
    _strip_expression,
)
from tests.helpers.public_tree import exported_root

WORKFLOWS_DIR = ".github/workflows"
DEPENDABOT = ".github/dependabot.yml"
REQUIRED_WORKFLOWS = frozenset({"ci.yml", "engine.yml"})

# A escrita permitida por job: (arquivo, job) -> as permissões com `write`; nenhum outro job escreve.
WRITE_TABLE: dict[tuple[str, str], frozenset[str]] = {
    ("ci.yml", "fast"): frozenset({"id-token"}),
}
JOB_PERMISSION_VALUES = frozenset({"read", "write", "none"})

# O job `fast`.
FAST_PERMISSIONS = {"contents": "read", "id-token": "write"}
LINT_RUN = "uv run ruff check server/src client/src ops scripts"
SUITE_RUN = "uv run python ops/ci/ci_suite.py fast --coverage-xml coverage.xml"
SUMMARY_IF = "!cancelled() && hashFiles('coverage.xml') != ''"
SUMMARY_REPORT = 'uv run coverage report --format=markdown >> "$GITHUB_STEP_SUMMARY"'
SUMMARY_LINES = (
    "servidor e cliente, medidos no processo do pytest e nos subprocessos que herdam o ambiente",
    "subestimada: os subprocessos com ambiente explícito ficam fora",
)
CODECOV_REF = "codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5"
CODECOV_VERSION = "v7.1.1"
CODECOV_IF = ("github.event_name != 'pull_request' || "
              "github.event.pull_request.head.repo.full_name == github.repository")
CODECOV_WITH = {"use_oidc": True, "files": "coverage.xml", "disable_search": True, "fail_ci_if_error": False}
# A lista fechada dos passos do `fast`, na ordem, sem o lint (que fica entre a sincronização e a suíte).
FAST_STEPS = ("checkout", "setup-uv", "uv-cache", "sync", "warm-cache", "max-connections", "age", "suite", "summary",
              "codecov")

# Sem o Scorecard do OpenSSF: o trecho do nome do arquivo e a ação, sem diferenciar maiúsculas.
SCORECARD_NAME = "scorecard"
SCORECARD_ACTION = "ossf/scorecard-action"

# O Dependabot: (ecossistema, diretório), seis entradas.
DEPENDABOT_ENTRIES = Counter({
    ("uv", "/"): 1,
    ("npm", "/engine"): 1,
    ("github-actions", "/"): 1,
    ("docker", "/"): 1,
    ("docker", "/engine"): 1,
    ("docker-compose", "/"): 1,
})
PL_IGNORE = [{"dependency-name": "prairielearn/prairielearn"}]
TEST_IMAGE_DIRS = ("/engine/test", "/tests/system")


@pytest.fixture(scope="session")
def public_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A árvore julgada (uma exportação por processo, no repositório de origem)."""
    return exported_root(tmp_path_factory)


# --------------------------------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------------------------------


def _workflows(root: Path) -> dict[str, tuple[str, dict[str, Any]]]:
    """Todo workflow da árvore pública: nome -> (texto, YAML). Exige os dois workflows do projeto."""
    folder = root.joinpath(*WORKFLOWS_DIR.split("/"))
    files = sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]) if folder.is_dir() else []
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        assert isinstance(data, dict), f"{path.name} não é um mapeamento YAML"
        found[path.name] = (text, data)
    missing = sorted(REQUIRED_WORKFLOWS - set(found))
    assert not missing, f"workflow ausente na árvore pública: {missing}"
    return found


def _jobs(name: str, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = data.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{name} sem jobs"
    for job_id, job in jobs.items():
        assert isinstance(job, dict), (name, job_id)
    return jobs


def _steps(name: str, job_id: str, job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, f"{name}: o job {job_id} sem passos"
    for step in steps:
        assert isinstance(step, dict), (name, job_id, step)
    return steps


def _has_uses_line(text: str, ref: str, version: str) -> bool:
    """Alguma linha `uses:` do texto fixa `ref` com o comentário `# <version>`."""
    for line in text.splitlines():
        match = USES_LINE.match(line)
        if match and match.group("ref") == ref and (match.group("comment") or "").strip() == f"# {version}":
            return True
    return False


def _fast_label(step: dict[str, Any]) -> str:
    """O papel de um passo do `fast`: pelo nome da ação ou pelo começo do `run:`."""
    uses = str(step.get("uses", ""))
    run = str(step.get("run", "")).lstrip()
    if uses:
        action = uses.split("@", 1)[0]
        return {"actions/checkout": "checkout", "astral-sh/setup-uv": "setup-uv",
                "codecov/codecov-action": "codecov"}.get(action, f"ação não prevista: {action}")
    if run.startswith('echo "UV_CACHE_DIR='):
        return "uv-cache"
    if run.strip() == "uv sync --locked":
        return "sync"
    if run.startswith("uv build --wheel"):
        return "warm-cache"
    if run.startswith('pg="${{ job.services.postgres.id }}"'):
        return "max-connections"
    if run.startswith("uv run python scripts/fetch_age.py"):
        return "age"
    if run.startswith("uv run ruff check"):
        return "lint"
    if run.startswith("uv run python ops/ci/ci_suite.py fast"):
        return "suite"
    if "coverage report --format=markdown" in run:
        return "summary"
    return f"passo não previsto: {step.get('name', run[:60])!r}"


# --------------------------------------------------------------------------------------------------
# As regras de todos os workflows
# --------------------------------------------------------------------------------------------------


def test_every_workflow_pins_actions_reads_by_default_and_has_no_secrets(public_root: Path) -> None:
    """Todo workflow da árvore pública: as ações fixadas pelo SHA de 40 hex com a versão em comentário, o topo só de
    leitura, a escrita só nos jobs da tabela, nenhum segredo, nenhum token do workflow, nenhum `env` com nome de
    segredo e nenhuma URL do Telegram nem do SMTP. Os workflows do esqueleto já cumpriam estas regras."""
    for name, (text, data) in sorted(_workflows(public_root).items()):
        for line in text.splitlines():
            if not re.match(r"^\s*(?:-\s*)?uses:", line):
                continue
            match = USES_LINE.match(line)
            assert match is not None, f"{name}: linha de `uses:` fora da forma `uses: dono/nome@<sha> # vX.Y.Z`"
            assert PINNED_ACTION.fullmatch(match.group("ref")), f"{name}: ação sem SHA de 40 hex: {match.group('ref')}"
            assert match.group("comment") and VERSION_COMMENT.match(match.group("comment")), (
                f"{name}: ação sem o comentário # vX.Y.Z: {match.group(0).strip()}")
        top = data.get("permissions")
        assert top is not None, f"{name}: sem `permissions` no topo"
        assert top == "read-all" or (isinstance(top, dict) and top and set(top.values()) == {"read"}), (
            f"{name}: o topo não é só de leitura: {top!r}")
        for job_id, job in _jobs(name, data).items():
            allowed = WRITE_TABLE.get((name, job_id), frozenset())
            permissions = job.get("permissions")
            if permissions is None:
                continue
            if isinstance(permissions, str):
                assert permissions == "read-all", f"{name}: o job {job_id} com `permissions: {permissions}`"
                continue
            assert isinstance(permissions, dict), (name, job_id, permissions)
            assert set(map(str, permissions.values())) <= JOB_PERMISSION_VALUES, (name, job_id, permissions)
            writes = {key for key, value in permissions.items() if value == "write"}
            assert writes <= allowed, f"{name}: o job {job_id} escreve fora da tabela: {sorted(writes - allowed)}"
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in text, f"{name} contém {forbidden!r}"
        for env in _envs(data):
            for variable in env:
                assert not SECRET_NAME.search(str(variable)), f"env com nome de segredo em {name}: {variable}"


# --------------------------------------------------------------------------------------------------
# O job `fast`
# --------------------------------------------------------------------------------------------------


def test_fast_job_lint_suite_summary_and_codecov_in_a_closed_step_list(public_root: Path) -> None:
    """O job `fast` do `ci.yml`: exatamente `{contents: read, id-token: write}`; a lista fechada dos passos, na ordem;
    o lint depois da sincronização e antes da suíte; a suíte com o XML da cobertura num passo só; o resumo com o `if`
    que tolera a falha da suíte e as duas linhas fixas; o envio ao Codecov depois do resumo, fixado pelo SHA, pela
    identidade do workflow, sem token e pulado nas PRs de fork. Vermelho até o `ci.yml` ganhar a permissão e os três
    passos novos; os passos de base já estavam na ordem."""
    text, data = _workflows(public_root)["ci.yml"]
    jobs = _jobs("ci.yml", data)
    assert set(jobs) == {"fast", "full"}, sorted(jobs)
    fast = jobs["fast"]
    assert fast.get("permissions") == FAST_PERMISSIONS, f"fast: permissões {fast.get('permissions')!r}"
    steps = _steps("ci.yml", "fast", fast)
    labels = [_fast_label(step) for step in steps]
    assert [label for label in labels if label != "lint"] == list(FAST_STEPS), labels
    assert labels.count("lint") == 1, labels
    lint = labels.index("lint")
    assert labels.index("sync") < lint < labels.index("suite"), f"fast: o lint fora do lugar: {labels}"
    assert str(steps[lint]["run"]).strip() == LINT_RUN, steps[lint]["run"]
    # O lint bloqueia: sem `continue-on-error` nem `if` no passo, e sem `continue-on-error` no job.
    assert set(steps[lint]) <= {"name", "run"}, f"fast: o passo do lint com chaves a mais: {sorted(steps[lint])}"
    assert "continue-on-error" not in fast, f"fast: o job com `continue-on-error`: {fast['continue-on-error']!r}"
    suite = steps[labels.index("suite")]
    assert str(suite["run"]).strip() == SUITE_RUN, suite["run"]
    summary = steps[labels.index("summary")]
    assert _strip_expression(str(summary.get("if", ""))) == SUMMARY_IF, summary.get("if")
    summary_run = str(summary["run"])
    assert SUMMARY_REPORT in summary_run, summary_run
    for line in SUMMARY_LINES:
        assert line in summary_run, f"fast: o resumo sem a linha fixa {line!r}"
    codecov = steps[labels.index("codecov")]
    assert codecov.get("uses") == CODECOV_REF, codecov.get("uses")
    assert _has_uses_line(text, CODECOV_REF, CODECOV_VERSION), f"o Codecov sem o comentário # {CODECOV_VERSION}"
    assert _strip_expression(str(codecov.get("if", ""))) == CODECOV_IF, codecov.get("if")
    assert codecov.get("with") == CODECOV_WITH, codecov.get("with")
    assert "env" not in codecov, codecov.get("env")
    for step in steps:
        assert "token" not in (step.get("with") or {}), f"fast: passo com `token`: {step.get('name')!r}"


# --------------------------------------------------------------------------------------------------
# Sem o Scorecard
# --------------------------------------------------------------------------------------------------


def test_no_openssf_scorecard_workflow(public_root: Path) -> None:
    """Nenhum workflow do Scorecard do OpenSSF na árvore pública: nenhum arquivo de `.github/workflows` com
    `scorecard` no nome e nenhum workflow com a ação `ossf/scorecard-action`, sem diferenciar maiúsculas. Vermelho
    enquanto o `scorecard.yml` existir."""
    workflows = _workflows(public_root)
    named = sorted(name for name in workflows if SCORECARD_NAME in name.lower())
    assert named == [], f"workflow do Scorecard na árvore pública: {named}"
    using = sorted(name for name, (text, _) in workflows.items() if SCORECARD_ACTION in text.lower())
    assert using == [], f"workflow com a ação do Scorecard: {using}"


# --------------------------------------------------------------------------------------------------
# O Dependabot
# --------------------------------------------------------------------------------------------------


def test_dependabot_covers_the_six_entries_with_existing_directories(public_root: Path) -> None:
    """O `.github/dependabot.yml`: a versão 2; as seis entradas (o uv, o npm do motor, as ações, o Docker na raiz e no
    motor e o Compose), todas semanais; cada diretório existe na árvore pública; a imagem do PrairieLearn no `ignore`
    da entrada do Docker do motor, que a fixa por hash; nenhuma entrada para as imagens de teste. Vermelho enquanto o
    arquivo do Dependabot não existe."""
    path = public_root.joinpath(*DEPENDABOT.split("/"))
    assert path.is_file(), f"arquivo ausente na árvore pública: {DEPENDABOT}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "dependabot.yml não é um mapeamento YAML"
    assert data.get("version") == 2, data.get("version")
    updates = data.get("updates")
    assert isinstance(updates, list), updates
    for entry in updates:
        assert isinstance(entry, dict), entry
        assert "directories" not in entry, entry
    pairs = Counter((str(entry.get("package-ecosystem")), str(entry.get("directory"))) for entry in updates)
    assert pairs == DEPENDABOT_ENTRIES, sorted(pairs.elements())
    for entry in updates:
        ecosystem, directory = entry["package-ecosystem"], entry["directory"]
        schedule = entry.get("schedule")
        assert isinstance(schedule, dict) and schedule.get("interval") == "weekly", (ecosystem, directory, schedule)
        folder = public_root.joinpath(*[part for part in directory.split("/") if part])
        assert folder.is_dir(), f"dependabot: o diretório {directory} não existe na árvore pública"
        assert not directory.rstrip("/").startswith(TEST_IMAGE_DIRS), (ecosystem, directory)
        if (ecosystem, directory) == ("docker", "/engine"):
            assert entry.get("ignore") == PL_IGNORE, entry.get("ignore")
