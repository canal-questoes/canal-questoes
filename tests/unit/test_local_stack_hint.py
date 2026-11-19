"""A dica da pilha local como biblioteca.


`local_stack_hint(profile, env)` devolve a linha fixa da dica, com o projeto Compose do perfil, só com o
perfil `local` e sem `CQ_API_URL` (vazia = ausente); `None` com perfil remoto ou com `CQ_API_URL`. O módulo
`cq_client.hint` não carrega o `click` (o servidor MCP o reaproveita), nem transitivamente pelo
`cq_client.profile` (o `import click` dele é preguiçoso).

**O que já passa:** a sonda de `sys.modules` já passa no esqueleto, então fica na mesma
função do caso `local_stack_hint` com perfil `local` sem `CQ_API_URL` (vermelho por `NotImplementedError` até a
implementação).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from cq_client.hint import HINT_TEMPLATE, local_stack_hint
from cq_client.profile import EffectiveProfile, resolve_profile

REPO = pathlib.Path(__file__).resolve().parents[2]
LOCAL_URL = "http://127.0.0.1:18765"
HINT_CQ = 'Dica: a pilha local (projeto Compose "cq") parece parada; suba-a com "cq up".'


def _profile(tmp_path: pathlib.Path, *, kind: str = "local", project: str = "cq", named: bool = True,
             api_url: str = LOCAL_URL) -> EffectiveProfile:
    """Um perfil efetivo (o tipo real do esqueleto)."""
    return EffectiveProfile(
        name="p" if named else None, kind=kind, api_url=api_url, token=None, stack_dir=None,  # type: ignore[arg-type]
        compose_project=project,
        origins={"name": "option" if named else "default", "kind": "profile" if named else "inferred",
                 "api_url": "profile" if named else "file", "token": "default",
                 "compose_project": "profile" if project != "cq" else "default"},
        named=named, config_path=tmp_path / "cq.toml")


def _modules_after_import(module: str) -> list[str]:
    """`sys.modules` depois de importar só `module` num processo novo (como tests/contract/test_boundaries.py)."""
    code = f"import json, sys\nimport {module}\nprint(json.dumps(sorted(sys.modules)))\n"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONSTARTUP" and not k.upper().startswith("CQ_")}
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_hint_for_local_profile_and_module_without_click(tmp_path) -> None:
    """com o perfil `local` e sem `CQ_API_URL` (ausente ou vazia), a linha fixa
    da dica com o projeto do perfil. Na mesma função: `cq_client.hint` importável sem o `click`,
    num processo novo, com `cq_client.profile` carregado por ele."""
    assert local_stack_hint(_profile(tmp_path), {}) == HINT_CQ
    assert local_stack_hint(_profile(tmp_path), {"CQ_API_URL": ""}) == HINT_CQ
    assert local_stack_hint(_profile(tmp_path, project="cq-local"), {}) == HINT_TEMPLATE.format(project="cq-local")
    assert local_stack_hint(_profile(tmp_path, named=False), {"CQ_TOKEN": "x"}) == HINT_CQ
    # A sonda e a constante real do esqueleto.
    assert HINT_TEMPLATE.format(project="cq") == HINT_CQ
    loaded = _modules_after_import("cq_client.hint")
    assert "cq_client.hint" in loaded
    assert "cq_client.profile" in loaded
    assert "click" not in loaded
    assert not any(m.startswith("click.") for m in loaded)
    assert not any(m == "cq_server" or m.startswith("cq_server.") for m in loaded)


@pytest.mark.parametrize(
    ("kind", "env"),
    [("remote", {}), ("remote", {"CQ_API_URL": ""}), ("local", {"CQ_API_URL": LOCAL_URL}),
     ("local", {"CQ_API_URL": "https://vm.cq.test"})],
    ids=["remoto", "remoto_variavel_vazia", "local_com_cq_api_url", "local_com_cq_api_url_remota"])
def test_no_hint_with_remote_profile_or_cq_api_url(tmp_path, kind, env) -> None:
    """sem dica com o perfil remoto ou com `CQ_API_URL` não vazia."""
    assert local_stack_hint(_profile(tmp_path, kind=kind), env) is None


def test_hint_for_legacy_demo_profile_names_project_cq(tmp_path) -> None:
    """o perfil legado da demonstração (só `api_url` de loopback no topo do arquivo, sem perfis
    nomeados) é local por inferência e nomeia o projeto `cq`."""
    config = tmp_path / "cq.toml"
    config.write_text(f'api_url = "{LOCAL_URL}"\ntoken = "tok-da-demo-Xr5"\n', encoding="utf-8")
    profile = resolve_profile({"CQ_CONFIG": str(config)})
    assert (profile.name, profile.kind, profile.compose_project) == (None, "local", "cq")
    assert local_stack_hint(profile, {"CQ_CONFIG": str(config)}) == HINT_CQ
    assert "tok-da-demo-Xr5" not in local_stack_hint(profile, {"CQ_CONFIG": str(config)})
