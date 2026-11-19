"""A dica de subir a pilha local quando ela parece parada.


Biblioteca sem Click e sem `up`, para que o servidor MCP a reaproveite: `local_stack_hint`
devolve a linha fixa da dica (com o projeto Compose do perfil) ou `None` quando ela não se aplica
(perfil remoto ou `CQ_API_URL` definida). A pergunta interativa e o `--json` ficam com o CLI.

A regra: a dica só vale com o perfil `local` e sem `CQ_API_URL` (uma
variável vazia conta como ausente, como na resolução do perfil). O texto é fixo e só leva o nome do
projeto Compose do perfil (nunca o token nem o endereço).
"""

from __future__ import annotations

from collections.abc import Mapping

from cq_client.profile import EffectiveProfile

# A linha da dica, depois da mensagem de API inalcançável (stderr).
HINT_TEMPLATE = 'Dica: a pilha local (projeto Compose "{project}") parece parada; suba-a com "cq up".'


def local_stack_hint(profile: EffectiveProfile, env: Mapping[str, str]) -> str | None:
    """A linha da dica com o projeto de `profile`, ou `None` com perfil remoto ou `CQ_API_URL` não
    vazia em `env`."""
    if profile.kind != "local":
        return None
    if env.get("CQ_API_URL"):
        # Uma `CQ_API_URL` não vazia aponta a outro lugar: a pilha local não é a suspeita.
        return None
    return HINT_TEMPLATE.format(project=profile.compose_project)
