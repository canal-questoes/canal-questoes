"""Workflows do GitHub Actions (implantação e piloto: o CI fica em dois arquivos, o rápido e o do motor;
o `age` no CI), lidos como YAML (`yaml.safe_load`) e como texto.

`.github/workflows/ci.yml` e `.github/workflows/engine.yml`:
- gatilhos: o `on` do ci com `push` (todas as branches: sem filtro), `pull_request` e `workflow_dispatch`; o do engine
  com `workflow_dispatch` e `push.paths` com `engine/**`, `contracts/engine/**` e um padrão que cobre o arquivo de
  fixação do PL;
- `permissions` do topo exatamente `{contents: read}`; `concurrency` com
  `group: ${{ github.workflow }}-${{ github.ref }}` e `cancel-in-progress: true`;
- os jobs `fast` e `full` (ci) e `engine` (engine); o `full.if` exato
  (`github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/main'`, com ou sem `${{ }}`);
- toda ação `dono/nome@<40 hex>` com o comentário `# vX.Y.Z` na mesma linha;
- nenhum `secrets.` nem `github.token` em lugar nenhum; nenhuma ocorrência de `api.telegram.org`, `smtp.`,
  `CQ_SMTP_PASSWORD` ou `CQ_TELEGRAM_API_URL`; nenhum `env` com nome de segredo;
- `timeout-minutes` 45 (`fast`), 120 (`full`) e 120 (`engine`);
- no `fast` e no `full`: o Postgres de serviço com a imagem (e o digest) de `scripts/test-postgres.sh`; o passo do age
  (`uv run python scripts/fetch_age.py --platform linux-amd64 --dest "$RUNNER_TEMP/age"`) antes do passo do
  `ci_suite.py` e o `CQ_TEST_AGE_DIR` exportado para os passos seguintes (pelo `$GITHUB_ENV` num passo anterior, ou
  no `env` do job ou do passo da suíte); o `fast` chama `uv run python ops/ci/ci_suite.py fast` e o `full`,
  `… ci_suite.py full`;
- o `engine` cria o `pl-oracle` com a imagem do PL fixada (a do `FROM` do `engine/Dockerfile`, commit e digest) antes
  de chamar `scripts/run-engine-suite.sh`.

**[F]** O PL é fixado no `engine/Dockerfile` (linha 5: `FROM prairielearn/prairielearn:<commit>@sha256:<digest>`;
lido em 2026-10-29); nenhum outro arquivo do repositório o fixa, então **[I]** o arquivo de fixação do PL do
CI é o `engine/Dockerfile`, e o teste pede que algum padrão de `push.paths` o cubra (o `engine/**` já
cobre). **[F]** O PyYAML lê a chave `on` como o booleano `True` (YAML 1.1); o teste aceita as duas chaves.

**Vermelho no esqueleto**: arquivo ausente até a implementação; cada função falha na primeira asserção, a de
que o workflow existe.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
ENGINE = WORKFLOWS / "engine.yml"

FULL_IF = "github.event_name == 'workflow_dispatch' || github.ref == 'refs/heads/main'"
CONCURRENCY_GROUP = "${{ github.workflow }}-${{ github.ref }}"
TIMEOUTS = {"fast": 45, "full": 120, "engine": 120}
# A regra "nenhum segredo": além de `secrets.`, o `github.token` (o token do workflow por outro caminho), de que
# o CI não precisa para nada.
FORBIDDEN_TEXT = ("secrets.", "github.token", "api.telegram.org", "smtp.", "CQ_SMTP_PASSWORD", "CQ_TELEGRAM_API_URL")
SECRET_NAME = re.compile(r"(?i)(password|passwd|secret|token|master_key|api_key|private_key)")
AGE_STEP = 'uv run python scripts/fetch_age.py --platform linux-amd64 --dest "$RUNNER_TEMP/age"'
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>[^\s#]+)\s*(?P<comment>#.*)?$")
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^#\s*v[0-9]+\.[0-9]+\.[0-9]+\s*$")
PL_PIN_FILE = "engine/Dockerfile"


def _text(path: Path) -> str:
    assert path.is_file(), f"workflow ausente: {path.relative_to(REPO).as_posix()}"
    return path.read_text(encoding="utf-8")


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(_text(path))
    assert isinstance(data, dict), f"{path.name} não é um mapeamento YAML"
    return data


def _on(data: dict[str, Any]) -> Any:
    # PyYAML (YAML 1.1) lê `on` como `True`.
    assert ("on" in data) != (True in data), "o gatilho `on` ausente ou repetido"
    return data["on"] if "on" in data else data[True]


def _strip_expression(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"\$\{\{\s*(.*?)\s*\}\}", value)
    return match.group(1) if match else value


def _postgres_image() -> str:
    match = re.search(r"^IMAGE=(\S+)$", (REPO / "scripts" / "test-postgres.sh").read_text(encoding="utf-8"),
                      re.MULTILINE)
    assert match, "scripts/test-postgres.sh sem IMAGE="
    return match.group(1)


def _pl_image() -> str:
    match = re.search(r"^FROM (prairielearn/prairielearn:[0-9a-f]{40}@sha256:[0-9a-f]{64})\s*$",
                      (REPO / PL_PIN_FILE).read_text(encoding="utf-8"), re.MULTILINE)
    assert match, "engine/Dockerfile sem a imagem do PL fixada"
    return match.group(1)


def _envs(node: Any) -> list[dict[str, Any]]:
    """Todos os mapeamentos `env` do workflow (topo, jobs, passos e serviços)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                found.append(value)
            found.extend(_envs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_envs(item))
    return found


def _common(path: Path) -> dict[str, Any]:
    """As regras dos dois arquivos: permissões, concorrência, ações fixadas, segredos e rede."""
    text = _text(path)
    data = _load(path)
    assert data.get("permissions") == {"contents": "read"}, data.get("permissions")
    # um job não pode trocar as permissões do topo por outras com escrita (`write-all`, `contents: write`…);
    # [I] o job pode omitir `permissions` (herda o topo) ou repetir exatamente `{contents: read}`.
    jobs = data.get("jobs")
    assert isinstance(jobs, dict) and jobs, f"{path.name} sem jobs"
    for job_id, job in jobs.items():
        assert isinstance(job, dict), (path.name, job_id)
        if "permissions" in job:
            # Só o `fast` do ci pode pedir também o `id-token: write`, o do envio da cobertura pela identidade do
            # workflow; os outros jobs, exatamente `{contents: read}`.
            allowed = [{"contents": "read"}]
            if (path.name, job_id) == ("ci.yml", "fast"):
                allowed.append({"contents": "read", "id-token": "write"})
            assert job["permissions"] in allowed, (
                f"{path.name}: o job {job_id} troca as permissões só de leitura: {job['permissions']!r}")
    concurrency = data.get("concurrency")
    assert isinstance(concurrency, dict), concurrency
    assert concurrency.get("group") == CONCURRENCY_GROUP, concurrency
    assert concurrency.get("cancel-in-progress") is True, concurrency
    uses = [USES_LINE.match(line) for line in text.splitlines() if re.match(r"^\s*(?:-\s*)?uses:", line)]
    for match in uses:
        assert match is not None, "linha de `uses:` fora da forma `uses: dono/nome@<sha> # vX.Y.Z`"
        assert PINNED_ACTION.fullmatch(match.group("ref")), f"ação sem SHA de 40 hex: {match.group('ref')}"
        assert match.group("comment") and VERSION_COMMENT.match(match.group("comment")), (
            f"ação sem o comentário # vX.Y.Z: {match.group(0).strip()}")
    for forbidden in FORBIDDEN_TEXT:
        assert forbidden not in text, f"{path.name} contém {forbidden!r}"
    for env in _envs(data):
        for name in env:
            assert not SECRET_NAME.search(str(name)), f"env com nome de segredo em {path.name}: {name}"
    return data


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, "job sem passos"
    return steps


def _run_index(steps: list[dict[str, Any]], needle: str) -> int:
    found = [i for i, step in enumerate(steps) if needle in str(step.get("run", ""))]
    assert len(found) == 1, f"passos com {needle!r}: {len(found)}"
    return found[0]


def _check_suite_job(job: dict[str, Any], kind: str) -> None:
    """O `fast` ou o `full`: o Postgres de serviço, o age antes da suíte, o `CQ_TEST_AGE_DIR` e a chamada certa."""
    assert job.get("timeout-minutes") == TIMEOUTS[kind], (kind, job.get("timeout-minutes"))
    services = job.get("services")
    assert isinstance(services, dict) and services, f"{kind} sem serviço Postgres"
    images = [svc.get("image") for svc in services.values() if isinstance(svc, dict)]
    assert _postgres_image() in images, f"{kind}: o Postgres de serviço não é {_postgres_image()}: {images}"
    steps = _steps(job)
    age = _run_index(steps, AGE_STEP)
    suite = _run_index(steps, f"uv run python ops/ci/ci_suite.py {kind}")
    assert age < suite, f"{kind}: o age vem antes da suíte"
    exported = any("CQ_TEST_AGE_DIR" in str(step.get("run", "")) and "GITHUB_ENV" in str(step.get("run", ""))
                   for step in steps[age:suite])
    exported = exported or "CQ_TEST_AGE_DIR" in (job.get("env") or {})
    exported = exported or "CQ_TEST_AGE_DIR" in (steps[suite].get("env") or {})
    assert exported, f"{kind}: o CQ_TEST_AGE_DIR não chega ao passo da suíte"
    other = "full" if kind == "fast" else "fast"
    assert not any(f"ci_suite.py {other}" in str(step.get("run", "")) for step in steps), kind


def test_ci_workflow_triggers_permissions_concurrency_and_jobs() -> None:
    """o `on` do ci com `push` sem filtro, `pull_request` e `workflow_dispatch`; as regras
    comuns; os jobs `fast` e `full`, com o `full.if` exato e os tempos 45 e 120."""
    data = _common(CI)
    on = _on(data)
    assert isinstance(on, dict) and set(on) == {"push", "pull_request", "workflow_dispatch"}, on
    push = on["push"] or {}
    assert not {"branches", "branches-ignore", "paths", "paths-ignore", "tags"} & set(push), (
        f"o push do ci vale para todas as branches: {push}")
    jobs = data.get("jobs")
    assert isinstance(jobs, dict) and set(jobs) == {"fast", "full"}, jobs
    assert "if" not in jobs["fast"], "o fast roda em todo gatilho"
    assert _strip_expression(str(jobs["full"].get("if", ""))) == FULL_IF, jobs["full"].get("if")


def test_ci_fast_and_full_get_postgres_age_and_the_ci_suite_step() -> None:
    """o Postgres de serviço com o digest do `scripts/test-postgres.sh`; o passo do age
    Linux antes da suíte, com o `CQ_TEST_AGE_DIR`; `ci_suite.py fast` no `fast` e `ci_suite.py full` no `full`."""
    jobs = _load(CI)["jobs"]
    _check_suite_job(jobs["fast"], "fast")
    _check_suite_job(jobs["full"], "full")


def test_engine_workflow_triggers_and_oracle_before_the_engine_suite() -> None:
    """o `on` do engine com `workflow_dispatch` e `push.paths` (`engine/**`, `contracts/engine/**` e um
    padrão que cobre o `engine/Dockerfile`); as regras comuns; um job só, `engine`, de 120 min, que cria o
    `pl-oracle` com a imagem fixada do PL e depois chama `scripts/run-engine-suite.sh`."""
    data = _common(ENGINE)
    on = _on(data)
    assert isinstance(on, dict) and set(on) == {"workflow_dispatch", "push"}, on
    paths = (on["push"] or {}).get("paths")
    assert isinstance(paths, list), on["push"]
    assert "engine/**" in paths and "contracts/engine/**" in paths, paths
    assert any(fnmatch.fnmatch(PL_PIN_FILE, pattern) for pattern in paths), paths
    jobs = data.get("jobs")
    assert isinstance(jobs, dict) and set(jobs) == {"engine"}, jobs
    job = jobs["engine"]
    assert job.get("timeout-minutes") == TIMEOUTS["engine"]
    steps = _steps(job)
    oracle = [i for i, step in enumerate(steps)
              if "pl-oracle" in str(step.get("run", "")) and _pl_image() in str(step.get("run", ""))]
    assert len(oracle) == 1, f"passos que criam o pl-oracle com {_pl_image()}: {len(oracle)}"
    suite = _run_index(steps, "scripts/run-engine-suite.sh")
    assert oracle[0] < suite, "o oráculo vem antes da suíte do motor"
