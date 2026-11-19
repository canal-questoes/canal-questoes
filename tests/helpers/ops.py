"""Carga dos scripts de `ops/` (e de `scripts/`) pelo caminho e execução do `main` com fluxos capturados
(scripts de `ops/`
carregados por caminho).

- `REPO`: a raiz do repositório.
- `load_ops(relpath) -> ModuleType`: carrega `REPO / relpath` por `importlib.util.spec_from_file_location`,
  com um nome de módulo único por arquivo (`module_name(relpath)`), **registrado em `sys.modules` antes do
  `exec_module`** e removido em caso de erro, como na receita "Importing a source file directly"
  (docs.python.org/3.12). **[F]** Sem o registro, um módulo com `from __future__ import annotations` e
  `@dataclass` levanta `AttributeError` no Python 3.12.3. O módulo já carregado do mesmo arquivo é
  reusado, como no `load_repo_scan` de `ops/export/export_public.py` e de `ops/deploy/make_bundle.py`.
- `module_name(relpath)`: `cq_ops_<caminho com _>`, sem o prefixo `ops/` e sem o `.py`. Assim
  `ops/export/repo_scan.py` vira `cq_ops_export_repo_scan`, **o mesmo nome** que o `load_repo_scan` da
  exportação e do pacote registra (`REPO_SCAN_MODULE`), para que as classes do `repo_scan` (ex.: `Finding`)
  não se dupliquem entre o teste e o script. Fora de `ops/` o caminho entra inteiro
  (`scripts/fetch_age.py` → `cq_ops_scripts_fetch_age`). **[I]** Só o prefixo é fixo; tirar o
  `ops/` é escolha minha, ditada pelo nome do esqueleto.
- `run_main(module, argv, *, env, stdin=b"") -> Result(code, stdout, stderr)`: roda `module.main(argv)`
  com `os.environ` trocado **inteiro** por `env` durante a chamada (os filhos herdam só ele) e com
  `sys.stdin`, `sys.stdout` e `sys.stderr` trocados por fluxos de texto em memória (UTF-8, com `.buffer`);
  `SystemExit` (do `argparse`, por exemplo) vira o código; qualquer outra exceção (inclusive o
  `NotImplementedError` do esqueleto) sobe para o teste. Os scripts resolvem `sys.stdout`/`sys.stderr` na
  chamada (docstrings do esqueleto), então a troca os alcança.
- `clean_env(**extra) -> dict[str, str]`: o ambiente do processo **sem** `CQ_*`, `COMPOSE_*`,
  `DOCKER_HOST` e `DOCKER_CONTEXT` do desenvolvedor (nomes comparados em maiúsculas: no Windows o ambiente
  não distingue caixa), com `extra` por cima (`None` remove a variável). O `CQ_TEST_AGE_DIR` também sai: só
  o `tests/helpers/age_bin.py` o lê.

Este módulo não importa `cq_server` nem `cq_client`.
"""

from __future__ import annotations

import importlib.util
import io
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from unittest import mock

REPO = Path(__file__).resolve().parents[2]

MODULE_PREFIX = "cq_ops_"
_OPS_DIR = "ops/"

_CLEARED_PREFIXES = ("CQ_", "COMPOSE_")
_CLEARED_NAMES = frozenset({"DOCKER_HOST", "DOCKER_CONTEXT"})


@dataclass(frozen=True)
class Result:
    """O resultado de um `run_main`: o código de saída e o texto capturado de cada fluxo."""

    code: int
    stdout: str
    stderr: str


def module_name(relpath: str | Path) -> str:
    """O nome do módulo de `relpath` (relativo à raiz do repositório; ver o docstring do módulo)."""
    rel = Path(relpath).as_posix()
    if rel.startswith("./"):
        rel = rel[2:]
    if rel.startswith(_OPS_DIR):
        rel = rel[len(_OPS_DIR):]
    if rel.endswith(".py"):
        rel = rel[:-3]
    return MODULE_PREFIX + re.sub(r"[^0-9A-Za-z_]", "_", rel)


def load_ops(relpath: str | Path) -> ModuleType:
    """Carrega `REPO / relpath` pelo caminho (ver o docstring do módulo)."""
    path = (REPO / Path(relpath)).resolve()
    if REPO not in path.parents:
        raise ValueError("load_ops só carrega arquivos de dentro do repositório")
    if not path.is_file():
        raise FileNotFoundError(f"script ausente no repositório: {Path(relpath).as_posix()}")
    name = module_name(relpath)
    existing = sys.modules.get(name)
    if existing is not None:
        existing_file = getattr(existing, "__file__", None)
        if existing_file is not None and Path(existing_file).resolve() == path:
            return existing
        raise ImportError(f"o nome {name} já está em sys.modules com outro arquivo")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def clean_env(**extra: str | None) -> dict[str, str]:
    """O ambiente do processo sem as variáveis do desenvolvedor (ver o docstring do módulo)."""
    env = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(_CLEARED_PREFIXES) and name.upper() not in _CLEARED_NAMES
    }
    for name, value in extra.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


def _text_stream(initial: bytes = b"") -> io.TextIOWrapper:
    # Sem tradução de fim de linha: o texto capturado é o que o script escreveu.
    return io.TextIOWrapper(io.BytesIO(initial), encoding="utf-8", newline="\n", write_through=True)


def _read_back(stream: io.TextIOWrapper) -> str:
    stream.flush()
    buffer = stream.buffer
    assert isinstance(buffer, io.BytesIO)
    return buffer.getvalue().decode("utf-8")


def _exit_code(exc: SystemExit, stderr: io.TextIOWrapper) -> int:
    code = exc.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    # `sys.exit("texto")`: o Python escreve o texto no stderr e sai com 1.
    stderr.write(f"{code}\n")
    return 1


def run_main(module: ModuleType, argv: Sequence[str], *, env: Mapping[str, str], stdin: bytes = b"") -> Result:
    """Roda `module.main(list(argv))` com o ambiente `env` e os fluxos capturados (ver o docstring do
    módulo)."""
    if any(value is None for value in env.values()):
        raise ValueError("run_main recebe o ambiente completo, sem valores None")
    fake_in = _text_stream(stdin)
    fake_out = _text_stream()
    fake_err = _text_stream()
    with mock.patch.dict(os.environ, dict(env), clear=True), \
            mock.patch.object(sys, "stdin", fake_in), \
            mock.patch.object(sys, "stdout", fake_out), \
            mock.patch.object(sys, "stderr", fake_err):
        try:
            code = module.main(list(argv))
        except SystemExit as exc:
            code = _exit_code(exc, fake_err)
    if not isinstance(code, int) or isinstance(code, bool):
        raise AssertionError(f"main devolveu {type(code).__name__}, não int")
    return Result(code=code, stdout=_read_back(fake_out), stderr=_read_back(fake_err))
