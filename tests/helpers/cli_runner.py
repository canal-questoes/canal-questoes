"""Invocação do CLI e ajudantes de perfil, pilha e relógio, com ambiente limpo e terminal simulado.


- `invoke_cli(args, *, json=False, obj=None, env=None, terminal=False, answer=None, config=None,
  tmp_path=None)` roda o `cq` em processo pelo `CliRunner`:
  - `obj` vira o `ctx.obj` e só aceita as chaves `http`, `stack` (nível 1), `runner` (nível 2), `clock`,
    `sleep` e `cwd`;
  - o ambiente sai **limpo**: toda `CQ_*` e `COMPOSE_*` do processo, `DOCKER_HOST` e `DOCKER_CONTEXT`
    removidas (inclusive `CQ_PROFILE`, `CQ_STACK_DIR`, `CQ_API_URL` e `CQ_TOKEN`), e `CQ_CONFIG`
    **sempre** num arquivo de teste: o `config` dado (ex.: o da `write_profile`), o `CQ_CONFIG` de `env`,
    ou um arquivo inexistente no `tmp_path` (ou, sem ele, num diretório temporário da sessão). Nunca o
    padrão do desenvolvedor, para que o perfil implícito nunca caia no `DEFAULT_API_URL` da porta 8000.
    `env` é aplicado por último (`None` remove a variável), menos `CQ_CONFIG = None`, recusado;
  - `terminal=True` substitui só `cli.stdin_is_terminal` e `cli.stderr_is_terminal` (por `True`) durante a
    invocação; o `ask_start_local_stack` roda **real**;
  - `answer` vira o `input` do `CliRunner`, o stdin simulado que a pergunta de subir a pilha local lê (`None` = EOF).
- `write_profile(tmp_path, text) -> Path`: o arquivo TOML de perfil de teste (nunca o do desenvolvedor).
- `make_stack_dir(tmp_path, *, env_file=True, markers=STACK_MARKERS, env_text=None, name="pilha") -> Path`:
  um diretório de pilha com os marcadores vazios e, se pedido, um `.env` com conteúdo-sentinela
  (`ENV_SENTINEL`, também no `CQ_ADMIN_TOKEN` dele) que nenhuma saída pode mostrar.
- `align_clock_to_db(clock, sql)`: `clock.set(<clock_timestamp() do banco de teste>)`, chamado
  imediatamente antes de cada `system.doctor` nos testes que esperam `clock.skew = ok`.

Endereços: `UNREACHABLE_API_URL` (`http://127.0.0.1:9`) é a API inalcançável; os casos de sucesso com
perfil local usam `TEST_API_URL` de `tests/helpers/stack_fake.py` e a API dos testes por `http=`.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from cq_client.stack import STACK_MARKERS

from tests.helpers.clock import FakeClock

UNREACHABLE_API_URL = "http://127.0.0.1:9"
PROFILE_FILE = "cq-config-teste.toml"
MISSING_PROFILE_FILE = "cq-config-inexistente.toml"

# Conteúdo-sentinela do `.env` de teste (o CLI nunca o lê, cria nem altera).
ENV_SENTINEL = "sentinela-env-pilha-Q8w3Zt5Kd1"
ENV_SENTINEL_TOKEN = f"cq_{ENV_SENTINEL}_" + "x" * 24
ENV_TEXT = (
    "# .env de teste do CLI: nenhum comando do CLI pode ler nem mostrar este conteúdo\n"
    f"CQ_DB_PASSWORD={ENV_SENTINEL}\n"
    f"CQ_ADMIN_TOKEN={ENV_SENTINEL_TOKEN}\n"
    f"CQ_MASTER_KEY={ENV_SENTINEL}-chave\n"
)

OBJ_KEYS = frozenset({"http", "stack", "runner", "clock", "sleep", "cwd"})

_CLEARED_NAMES = ("CQ_PROFILE", "CQ_STACK_DIR", "CQ_API_URL", "CQ_TOKEN", "CQ_CONFIG", "DOCKER_HOST",
                  "DOCKER_CONTEXT")
_CLEARED_PREFIXES = ("CQ_", "COMPOSE_")

_session_dir: Path | None = None


def _missing_config(tmp_path: Path | None) -> Path:
    global _session_dir
    if tmp_path is not None:
        base = Path(tmp_path)
    else:
        if _session_dir is None:
            _session_dir = Path(tempfile.mkdtemp(prefix="cq-cli-"))
        base = _session_dir
    path = base / MISSING_PROFILE_FILE
    assert not path.exists(), "o arquivo de perfil 'inexistente' existe"
    return path


def clean_env(config: Path, env: Mapping[str, str | None] | None = None) -> dict[str, str | None]:
    """O ambiente do `CliRunner` (`None` remove a variável): as convenções limpas, `CQ_CONFIG = config`
    e, por cima, `env`."""
    full: dict[str, str | None] = {k: None for k in os.environ if k.upper().startswith(_CLEARED_PREFIXES)}
    full.update({name: None for name in _CLEARED_NAMES})
    full["CQ_CONFIG"] = str(config)
    full.update(env or {})
    if full.get("CQ_CONFIG") is None:
        raise ValueError("invoke_cli sempre define CQ_CONFIG num arquivo de teste")
    return full


def invoke_cli(args: Sequence[str], *, json: bool = False, obj: Mapping[str, Any] | None = None,
              env: Mapping[str, str | None] | None = None, terminal: bool = False,
              answer: str | None = None, config: Path | None = None,
              tmp_path: Path | None = None) -> Any:
    """Roda o `cq` com o `CliRunner` (ver o docstring do módulo); devolve o `click.testing.Result`."""
    from click.testing import CliRunner

    from cq_client import cli as cq_cli

    given = dict(obj or {})
    unknown = set(given) - OBJ_KEYS
    if unknown:
        raise ValueError(f"chaves de obj fora das aceitas pelo CLI: {sorted(unknown)}")
    full_env = clean_env(config if config is not None else _missing_config(tmp_path), env)
    argv = (["--json"] if json else []) + list(args)
    patches = []
    if terminal:
        patches = [mock.patch.object(cq_cli, "stdin_is_terminal", lambda: True),
                   mock.patch.object(cq_cli, "stderr_is_terminal", lambda: True)]
    for patch in patches:
        patch.start()
    try:
        return CliRunner().invoke(cq_cli.cli, argv, env=full_env, obj=given if obj is not None else None,
                                  input=answer)
    finally:
        for patch in reversed(patches):
            patch.stop()


def write_profile(tmp_path: Path, text: str, *, name: str = PROFILE_FILE) -> Path:
    """Grava o arquivo TOML de perfil de teste em `tmp_path` (bytes exatos de `text`, UTF-8) e devolve o
    caminho, para `invoke_cli(..., config=<caminho>)` ou `CQ_CONFIG`."""
    path = Path(tmp_path) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def make_stack_dir(tmp_path: Path, *, env_file: bool = True, markers: Sequence[str] = STACK_MARKERS,
                   env_text: str | None = None, name: str = "pilha") -> Path:
    """Cria `tmp_path/<name>` com os marcadores vazios (`STACK_MARKERS` por padrão; outro subconjunto para
    os casos de "3 dos 4") e, com `env_file`, o `.env` de teste (`ENV_TEXT`, ou `env_text`)."""
    root = Path(tmp_path) / name
    root.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        path = root / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    if env_file:
        (root / ".env").write_bytes((env_text if env_text is not None else ENV_TEXT).encode("utf-8"))
    return root


def align_clock_to_db(clock: FakeClock, sql: Any) -> datetime:
    """põe o `FakeClock` no `clock_timestamp()` do banco de teste (pela conexão `sql`) e devolve o
    horário, em UTC."""
    row = sql.execute("SELECT clock_timestamp()").fetchone()
    now = row[0].astimezone(timezone.utc)
    clock.set(now)
    return now
