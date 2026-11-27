"""Catálogo único (princípio "Portas e contratos").

`test_catalog_matches_contract_table` e `test_client_does_not_import_server` conferem o arquivo
congelado e uma propriedade estrutural negativa.
Os demais comparam o catálogo com a API (`create_app`) e com a árvore do CLI (`cq_client.cli.cli`).

Oferecimentos e bots (a tabela de operações e a forma das folhas do CLI):
o catálogo tem as 10 operações de oferecimentos, bots e equipe
(depois das 9 gerais), `confirm: true` em `identity.reveal` e `offerings.archive`, e as 8 permissões.
Como compara com `Permission`, que só ganha as 3 permissões novas na implementação,
`test_catalog_matches_contract_table` fica vermelho no esqueleto por asserção de contrato;
`test_client_does_not_import_server` é verde desde o início.

Vínculo do aluno (a tabela de operações e a forma das folhas do CLI):
o catálogo tem as 5 operações de alunos e convites (`ids[19:24]`),
`confirm: true` também em `invites.send`, e nenhuma
permissão a mais. Essa parte de `test_catalog_matches_contract_table` passa já no esqueleto (confere o
`catalog.json`, que já tem as 5). As folhas
`students import` e `invites resend` têm a forma exata do contrato; no esqueleto, o CLI não
tem os comandos novos, e as comparações do catálogo com o OpenAPI, as rotas e as folhas do CLI ficam
vermelhas por isso.

Disparo e resposta (a tabela de operações e a forma das folhas do CLI):
o catálogo tem as 6 operações de disparos e mensagens livres
(`ids[24:30]`), `confirm: true` também em
`dispatches.confirm`, `messages.list` fora do MCP e nenhuma permissão a mais.
`test_catalog_matches_contract_table`, além de conferir o `catalog.json`, confere
que a implementação bate com ele (para que este teste não
passe no esqueleto): o registro de operações (`cq_server.api.operations.OPERATIONS`) tem todas
na ordem do catálogo, com o método, o caminho e a permissão de cada uma, e o OpenAPI publicado
(`create_app(...).openapi()`, sem banco) dá a cada (método, caminho) o `operationId` do catálogo;
vermelho no esqueleto porque as 6 operações novas não estão no registro nem no OpenAPI.
As 6 folhas do disparo têm a forma exata do contrato; no esqueleto, as rotas e o CLI não
têm as operações novas, e as comparações do catálogo com o OpenAPI, as rotas e as folhas do CLI ficam
vermelhas por isso.

Mensagens livres opcionais por oferecimento, desligadas por padrão (a tabela de operações e a forma das
folhas do CLI): o catálogo tem `offerings.update_settings`
(`PATCH /v1/offerings/{ref}/settings`, `offerings:write`, `offerings settings`, exposta ao MCP como
`update_offering_settings`, sem `confirm`, não destrutiva) em `ids[30:31]`, sem permissão a mais; as 6 do disparo
ficam em `ids[24:30]`. A folha `offerings settings` e as duas opções de mensagens livres de `offerings create`
(`--free-messages`, escolha `on`/`off`, e `--contact-email`, opcionais, com valor) têm a forma exata
do contrato. Vermelho no esqueleto: a operação nova não está no registro nem no OpenAPI, e o CLI
não tem a folha nem as opções novas.

Relatórios e acompanhamento (a tabela de operações e a forma das folhas do CLI):
o catálogo tem as 5 operações de relatórios e do resumo diário (`reports.get`,
`reports.list_versions`, `reports.resend_summary`, `offerings.get_digest`, `offerings.update_digest`)
em `ids[31:36]`, expostas ao MCP sem `confirm`, não destrutivas e sem permissão a mais; a operação
das mensagens livres fica em `ids[30:31]`. As 5 folhas (`reports show`, `reports versions`, `reports resend`, `offerings
digest show`, `offerings digest set`) têm a forma exata do contrato, no formato de `_param_shape`.
Vermelho no esqueleto: as 5 operações não estão no registro nem no OpenAPI publicado, as rotas não existem
e o CLI não tem as folhas novas.

CLI (a tabela de operações e a forma das folhas do CLI; operações locais no catálogo, sem endpoint):
o catálogo tem `system.doctor`, `offerings.prepare_purge` e `offerings.purge`, por último
(`ids[36:39]`), expostas ao MCP, sem permissão a mais; `offerings.purge` é destrutiva, com `confirm: true`; as 5 dos
relatórios ficam em `ids[31:36]`. A árvore do CLI tem as folhas do catálogo e os comandos locais
(`cq_client.local_commands.LOCAL_COMMANDS`: `up`, `down`, `status`, `logs`; conjuntos disjuntos), com a opção
global `--profile` na cadeia de toda folha e nenhum parâmetro de token; as folhas das 3 operações têm a forma exata do
contrato (`doctor` sem parâmetros; `offerings purge prepare` = `REF`; `offerings purge confirm` =
`REF` e `--confirmation`, obrigatória, com valor), e `events list` tem `--person` (opcional, com valor), com o
`--type` opcional. Vermelho no esqueleto: as 3 operações não estão no registro nem no
OpenAPI publicado, as rotas não existem e o CLI não tem as folhas novas, os comandos locais nem
o `--profile`.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from typing import Any

import click

from tests.helpers.contract import REPO, load_catalog

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# A tabela de operações, linha a linha (os motivos MCP são os textos do catalog.json congelado).
EXPECTED_OPERATIONS: dict[str, dict] = {
    "system.health": {
        "method": "GET", "path": "/health", "permission": None, "cli": "health",
        "mcp": {"exposed": False,
                "reason": ("Operação de diagnóstico; no MCP, o diagnóstico fica com a run_doctor, e a "
                           "stack_status mostra a saúde da API da pilha local.")},
        "destructive": False,
    },
    "people.register": {
        "method": "POST", "path": "/v1/people", "permission": "people:write", "cli": "people add",
        "mcp": {"exposed": False, "reason": "A identidade passaria pelo modelo."},
        "destructive": False,
    },
    "people.list": {
        "method": "GET", "path": "/v1/people", "permission": "people:read", "cli": "people list",
        "mcp": {"exposed": True, "tool": "list_people", "confirm": False},
        "destructive": False,
    },
    "people.get": {
        "method": "GET", "path": "/v1/people/{ref}", "permission": "people:read", "cli": "people show",
        "mcp": {"exposed": True, "tool": "get_person", "confirm": False},
        "destructive": False,
    },
    "identity.reveal": {
        "method": "POST", "path": "/v1/identity/reveal", "permission": "identity:reveal",
        "cli": "identity reveal",
        "mcp": {"exposed": True, "tool": "reveal_identities", "confirm": True},
        "destructive": False,
    },
    "tokens.create": {
        "method": "POST", "path": "/v1/tokens", "permission": "tokens:manage", "cli": "tokens create",
        "mcp": {"exposed": False, "reason": "O segredo passaria pelo modelo."},
        "destructive": False,
    },
    "tokens.list": {
        "method": "GET", "path": "/v1/tokens", "permission": "tokens:manage", "cli": "tokens list",
        "mcp": {"exposed": False, "reason": "Gestão de tokens de API só pelo CLI."},
        "destructive": False,
    },
    "tokens.revoke": {
        "method": "POST", "path": "/v1/tokens/{token_id}/revoke", "permission": "tokens:manage",
        "cli": "tokens revoke",
        "mcp": {"exposed": False, "reason": "Gestão de tokens de API só pelo CLI."},
        "destructive": False,
    },
    "events.list": {
        "method": "GET", "path": "/v1/events", "permission": "events:read", "cli": "events list",
        "mcp": {"exposed": True, "tool": "list_events", "confirm": False},
        "destructive": False,
    },
    # oferecimentos e bots, linha a linha (os motivos MCP são os textos do catalog.json congelado).
    "offerings.create": {
        "method": "POST", "path": "/v1/offerings", "permission": "offerings:write",
        "cli": "offerings create",
        "mcp": {"exposed": True, "tool": "create_offering", "confirm": False},
        "destructive": False,
    },
    "offerings.list": {
        "method": "GET", "path": "/v1/offerings", "permission": "offerings:read",
        "cli": "offerings list",
        "mcp": {"exposed": True, "tool": "list_offerings", "confirm": False},
        "destructive": False,
    },
    "offerings.get": {
        "method": "GET", "path": "/v1/offerings/{ref}", "permission": "offerings:read",
        "cli": "offerings show",
        "mcp": {"exposed": True, "tool": "get_offering", "confirm": False},
        "destructive": False,
    },
    "offerings.close": {
        "method": "POST", "path": "/v1/offerings/{ref}/close", "permission": "offerings:write",
        "cli": "offerings close",
        "mcp": {"exposed": True, "tool": "close_offering", "confirm": False},
        "destructive": False,
    },
    "offerings.prepare_archive": {
        "method": "POST", "path": "/v1/offerings/{ref}/archive/prepare", "permission": "offerings:write",
        "cli": "offerings archive prepare",
        "mcp": {"exposed": True, "tool": "prepare_archive_offering", "confirm": False},
        "destructive": False,
    },
    "offerings.archive": {
        "method": "POST", "path": "/v1/offerings/{ref}/archive", "permission": "offerings:write",
        "cli": "offerings archive confirm",
        "mcp": {"exposed": True, "tool": "archive_offering", "confirm": True},
        "destructive": True,
    },
    "bots.register": {
        "method": "PUT", "path": "/v1/offerings/{ref}/bot", "permission": "bots:manage",
        "cli": "bots register",
        "mcp": {"exposed": False, "reason": "O token do bot é segredo e não passa pelo modelo."},
        "destructive": False,
    },
    "bots.activate": {
        "method": "POST", "path": "/v1/offerings/{ref}/bot/activate", "permission": "bots:manage",
        "cli": "bots activate",
        "mcp": {"exposed": True, "tool": "activate_bot", "confirm": False},
        "destructive": False,
    },
    "bots.get": {
        "method": "GET", "path": "/v1/offerings/{ref}/bot", "permission": "offerings:read",
        "cli": "bots show",
        "mcp": {"exposed": True, "tool": "get_bot", "confirm": False},
        "destructive": False,
    },
    "team.link": {
        "method": "POST", "path": "/v1/offerings/{ref}/team-links", "permission": "offerings:write",
        "cli": "team link",
        "mcp": {"exposed": False,
                "reason": "O link de equipe é uma credencial e não passa pelo modelo."},
        "destructive": False,
    },
    # vínculo do aluno, linha a linha; `invites.send` é a única destrutiva do vínculo,
    # com `confirm: true`; o reenvio não é destrutivo.
    "students.import": {
        "method": "POST", "path": "/v1/offerings/{ref}/students", "permission": "people:write",
        "cli": "students import",
        "mcp": {"exposed": True, "tool": "import_students", "confirm": False},
        "destructive": False,
    },
    "students.summary": {
        "method": "GET", "path": "/v1/offerings/{ref}/students/summary", "permission": "offerings:read",
        "cli": "students summary",
        "mcp": {"exposed": True, "tool": "get_students_summary", "confirm": False},
        "destructive": False,
    },
    "invites.prepare_send": {
        "method": "POST", "path": "/v1/offerings/{ref}/invites/prepare", "permission": "offerings:write",
        "cli": "invites send prepare",
        "mcp": {"exposed": True, "tool": "prepare_send_invites", "confirm": False},
        "destructive": False,
    },
    "invites.send": {
        "method": "POST", "path": "/v1/offerings/{ref}/invites", "permission": "offerings:write",
        "cli": "invites send confirm",
        "mcp": {"exposed": True, "tool": "send_invites", "confirm": True},
        "destructive": True,
    },
    "invites.resend": {
        "method": "POST", "path": "/v1/offerings/{ref}/invites/resend", "permission": "offerings:write",
        "cli": "invites resend",
        "mcp": {"exposed": True, "tool": "resend_invites", "confirm": False},
        "destructive": False,
    },
    # disparo e resposta, linha a linha (nenhuma permissão a mais);
    # `dispatches.confirm` é a única destrutiva do disparo (envio em massa à turma), com `confirm: true`;
    # `messages.list` não é exposta ao MCP (o motivo é o texto do catalog.json congelado).
    "dispatches.prepare": {
        "method": "POST", "path": "/v1/offerings/{ref}/dispatches", "permission": "offerings:write",
        "cli": "dispatches prepare",
        "mcp": {"exposed": True, "tool": "prepare_dispatch", "confirm": False},
        "destructive": False,
    },
    "dispatches.get": {
        "method": "GET", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}",
        "permission": "offerings:read", "cli": "dispatches show",
        "mcp": {"exposed": True, "tool": "get_dispatch", "confirm": False},
        "destructive": False,
    },
    "dispatches.list": {
        "method": "GET", "path": "/v1/offerings/{ref}/dispatches", "permission": "offerings:read",
        "cli": "dispatches list",
        "mcp": {"exposed": True, "tool": "list_dispatches", "confirm": False},
        "destructive": False,
    },
    "dispatches.confirm": {
        "method": "POST", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}/confirm",
        "permission": "offerings:write", "cli": "dispatches confirm",
        "mcp": {"exposed": True, "tool": "confirm_dispatch", "confirm": True},
        "destructive": True,
    },
    "dispatches.cancel": {
        "method": "POST", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}/cancel",
        "permission": "offerings:write", "cli": "dispatches cancel",
        "mcp": {"exposed": True, "tool": "cancel_dispatch", "confirm": False},
        "destructive": False,
    },
    "messages.list": {
        "method": "GET", "path": "/v1/offerings/{ref}/messages", "permission": "offerings:read",
        "cli": "messages list",
        "mcp": {"exposed": False,
                "reason": "Texto livre de aluno; fica fora do MCP nesta versão."},
        "destructive": False,
    },
    # mensagens livres opcionais, a última operação do disparo: a configuração das mensagens livres do
    # oferecimento; exposta, sem confirmação e não destrutiva (o e-mail do contato não passa pelo
    # modelo, mas o catálogo não descreve os parâmetros das ferramentas).
    "offerings.update_settings": {
        "method": "PATCH", "path": "/v1/offerings/{ref}/settings", "permission": "offerings:write",
        "cli": "offerings settings",
        "mcp": {"exposed": True, "tool": "update_offering_settings", "confirm": False},
        "destructive": False,
    },
    # relatórios e acompanhamento, linha a linha (`ids[31:36]`; nenhuma permissão
    # a mais); as 5 expostas, sem confirmação e não destrutivas (o reenvio do resumo só vai à equipe).
    "reports.get": {
        "method": "GET", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}/report",
        "permission": "offerings:read", "cli": "reports show",
        "mcp": {"exposed": True, "tool": "get_dispatch_report", "confirm": False},
        "destructive": False,
    },
    "reports.list_versions": {
        "method": "GET", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}/report/versions",
        "permission": "offerings:read", "cli": "reports versions",
        "mcp": {"exposed": True, "tool": "list_report_versions", "confirm": False},
        "destructive": False,
    },
    "reports.resend_summary": {
        "method": "POST", "path": "/v1/offerings/{ref}/dispatches/{dispatch_id}/report/resend",
        "permission": "offerings:write", "cli": "reports resend",
        "mcp": {"exposed": True, "tool": "resend_report_summary", "confirm": False},
        "destructive": False,
    },
    "offerings.get_digest": {
        "method": "GET", "path": "/v1/offerings/{ref}/digest", "permission": "offerings:read",
        "cli": "offerings digest show",
        "mcp": {"exposed": True, "tool": "get_offering_digest", "confirm": False},
        "destructive": False,
    },
    "offerings.update_digest": {
        "method": "PATCH", "path": "/v1/offerings/{ref}/digest", "permission": "offerings:write",
        "cli": "offerings digest set",
        "mcp": {"exposed": True, "tool": "update_offering_digest", "confirm": False},
        "destructive": False,
    },
    # CLI, linha a linha, por último (`ids[36:39]`; nenhuma permissão a mais); as 3
    # expostas; só a confirmação do expurgo é destrutiva, com `confirm: true`.
    "system.doctor": {
        "method": "GET", "path": "/v1/system/doctor", "permission": "offerings:read",
        "cli": "doctor",
        "mcp": {"exposed": True, "tool": "run_doctor", "confirm": False},
        "destructive": False,
    },
    "offerings.prepare_purge": {
        "method": "POST", "path": "/v1/offerings/{ref}/purge/prepare", "permission": "people:write",
        "cli": "offerings purge prepare",
        "mcp": {"exposed": True, "tool": "prepare_purge_offering", "confirm": False},
        "destructive": False,
    },
    "offerings.purge": {
        "method": "POST", "path": "/v1/offerings/{ref}/purge", "permission": "people:write",
        "cli": "offerings purge confirm",
        "mcp": {"exposed": True, "tool": "purge_offering", "confirm": True},
        "destructive": True,
    },
    # o banco de questões local: o envio da pasta e a listagem das versões, no fim do catálogo.
    "banks.upload": {
        "method": "POST", "path": "/v1/question-banks", "permission": "offerings:write",
        "cli": "banks upload",
        "mcp": {"exposed": True, "tool": "upload_question_bank", "confirm": False},
        "destructive": False,
    },
    "banks.list": {
        "method": "GET", "path": "/v1/question-banks", "permission": "offerings:read",
        "cli": "banks list",
        "mcp": {"exposed": True, "tool": "list_question_banks", "confirm": False},
        "destructive": False,
    },
}

FORBIDDEN_PARAM_NAMES = {"token", "secret", "bot_token"}
FORBIDDEN_OPTS = {"--token", "--secret", "--bot-token"}


def _catalog_pairs() -> set[tuple[str, str]]:
    return {(op["method"], op["path"]) for op in load_catalog()["operations"]}


def _normalize_route_path(path: str) -> str:
    """Tira o conversor do Starlette (`{ref:str}` → `{ref}`), como no OpenAPI."""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*):[^}]*\}", r"{\1}", path)


def _route_pairs(routes: Any, prefix: str = "") -> set[tuple[str, str]]:
    """(MÉTODO, caminho) de todas as rotas registradas, entrando em `Mount`/`Router`."""
    pairs: set[tuple[str, str]] = set()
    for route in routes:
        path = _normalize_route_path(prefix + getattr(route, "path", ""))
        sub = getattr(route, "routes", None)
        methods = getattr(route, "methods", None)
        if sub and methods is None:
            pairs |= _route_pairs(sub, path)
        elif methods is None:
            pairs.add(("ANY", path))
        else:
            upper = {m.upper() for m in methods}
            if "GET" in upper:
                upper.discard("HEAD")  # o Starlette acrescenta HEAD a toda rota GET
            pairs |= {(m, path) for m in upper}
    return pairs


def _cli_leaves(group: click.Group) -> dict[str, list[click.Parameter]]:
    """Folhas da árvore do `cli` → parâmetros de toda a cadeia (grupos e folha)."""
    leaves: dict[str, list[click.Parameter]] = {}

    def walk(cmd: click.Command, names: list[str], params: list[click.Parameter]) -> None:
        chain = params + list(cmd.params)
        if isinstance(cmd, click.Group):
            ctx = click.Context(cmd)
            for name in cmd.list_commands(ctx):
                child = cmd.get_command(ctx, name)
                assert child is not None, f"subcomando {name!r} listado mas ausente"
                walk(child, names + [name], chain)
        else:
            leaves[" ".join(names)] = chain

    walk(group, [], [])
    leaves.pop("", None)
    return leaves


def _cli_command(group: click.Group, path: str) -> click.Command:
    """O comando da árvore do `cli` no caminho `path` (ex.: `"bots register"`)."""
    cmd: click.Command = group
    for name in path.split():
        assert isinstance(cmd, click.Group), f"{path!r}: {name!r} depois de uma folha"
        child = cmd.get_command(click.Context(cmd), name)
        assert child is not None, f"comando {path!r} ausente da árvore do cli"
        cmd = child
    return cmd


def _param_shape(param: click.Parameter) -> tuple:
    """Forma de um parâmetro do click, sem depender do nome interno da variável Python."""
    if isinstance(param, click.Argument):
        return ("argument", param.human_readable_name, param.required, param.nargs)
    assert isinstance(param, click.Option), f"parâmetro de tipo inesperado: {param!r}"
    return ("option", tuple(param.opts), tuple(param.secondary_opts), param.required, param.is_flag,
            param.multiple, param.nargs)


def _imports_server(tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.split(".")[0] == "cq_server"]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == "cq_server":
                found.append(node.module)
        elif isinstance(node, ast.Call):
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.split(".")[0] == "cq_server":
                    found.append(f"chamada com {arg.value!r}")
    return found


# --- tabela do catálogo e cliente sem o servidor --------------------------------------------------

def test_catalog_matches_contract_table() -> None:
    """as operações com os valores exatos da tabela; ids únicos;
    `mcp.confirm` verdadeiro só na revelação e nas 4 destrutivas; as permissões exigidas.

    Oferecimentos e bots: depois das 9 operações gerais, as 10 de
    oferecimentos, bots e equipe; `confirm: true` em `identity.reveal` e
    `offerings.archive`; as 8 permissões no catálogo e em `Permission` (vermelho no esqueleto:
    `Permission` só ganha `offerings:read`, `offerings:write` e `bots:manage` na implementação).

    Vínculo do aluno: as 5 operações
    de alunos e convites, em `ids[19:24]`; `confirm: true` também em
    `invites.send`; as mesmas 8 permissões. Verde já
    no esqueleto.

    Disparo e resposta: as 6 operações de
    disparos e mensagens livres, em `ids[24:30]`, na ordem da tabela;
    `confirm: true` também em
    `dispatches.confirm`; as mesmas 8 permissões. A implementação bate
    com o catálogo: o registro de operações da API tem todas na ordem do catálogo, com o mesmo método,
    caminho e permissão, e o OpenAPI publicado dá a cada (método, caminho) o `operationId` do catálogo.
    Vermelho no esqueleto: as 6 operações novas não estão no registro nem no OpenAPI
    até a implementação.

    Mensagens livres opcionais: depois das 6 do disparo em
    `ids[24:30]`, `ids[30:31] == ["offerings.update_settings"]`, sem `confirm` e sem permissão a mais.
    Vermelho: `offerings.update_settings` não está no registro nem no OpenAPI publicado.

    Relatórios e acompanhamento: depois da operação das mensagens livres (`ids[30:31]`), as 5
    dos relatórios em `ids[31:36]`, na ordem da tabela, sem `confirm` e sem permissão a mais.
    Vermelho: as 5 operações não estão no registro nem no OpenAPI publicado.

    CLI: as 5 dos relatórios em `ids[31:36]` e as 3 do CLI em
    `ids[36:39]`, na ordem da tabela; `confirm: true` em exatamente `identity.reveal`,
    `offerings.archive`, `invites.send`, `dispatches.confirm` e `offerings.purge`; as mesmas 8 permissões.
    Vermelho: as 3 operações não estão no registro nem no OpenAPI publicado.

    Banco de questões local: 41 operações; as 2 do banco em `ids[39:]` (o envio e a listagem), sem
    `confirm` e sem permissão a mais. Verde no esqueleto: o registro e o OpenAPI já têm as 2."""
    catalog = load_catalog()
    assert catalog["version"] == "1"
    ids = [op["id"] for op in catalog["operations"]]
    assert len(ids) == len(set(ids)) == 41
    assert {op["id"]: {k: v for k, v in op.items() if k != "id"} for op in catalog["operations"]} \
        == EXPECTED_OPERATIONS
    assert [op["id"] for op in catalog["operations"] if op["mcp"].get("confirm") is True] == [
        "identity.reveal", "offerings.archive", "invites.send", "dispatches.confirm", "offerings.purge"]
    # as 5 operações do vínculo do aluno, na ordem delas.
    assert ids[19:24] == ["students.import", "students.summary", "invites.prepare_send", "invites.send",
                          "invites.resend"]
    # as 6 operações do disparo, na ordem delas.
    # o recorte das 6 do disparo é `ids[24:30]`; a das mensagens livres vem logo depois.
    assert ids[24:30] == ["dispatches.prepare", "dispatches.get", "dispatches.list", "dispatches.confirm",
                          "dispatches.cancel", "messages.list"]
    # a das mensagens livres em `ids[30:31]`; depois, as 5 dos relatórios.
    assert ids[30:31] == ["offerings.update_settings"]
    # as 5 dos relatórios em `ids[31:36]`; por último, as 3 do CLI.
    assert ids[31:36] == ["reports.get", "reports.list_versions", "reports.resend_summary", "offerings.get_digest",
                          "offerings.update_digest"]
    assert ids[36:39] == ["system.doctor", "offerings.prepare_purge", "offerings.purge"]
    # as 2 do banco de questões local, no fim.
    assert ids[39:] == ["banks.upload", "banks.list"]
    from cq_server.permissions import Permission

    permissions = {op["permission"] for op in catalog["operations"]} - {None}
    assert permissions == {p.value for p in Permission} == {
        "tokens:manage", "people:write", "people:read", "events:read", "identity:reveal",
        "offerings:read", "offerings:write", "bots:manage"}
    clis = [op["cli"] for op in catalog["operations"]]
    assert len(clis) == len(set(clis))

    # a implementação bate com o catálogo (registro de operações e OpenAPI publicado).
    from cq_server.api import create_app
    from cq_server.api.operations import OPERATIONS
    from cq_server.config import Settings

    expected = [(op["id"], op["method"], op["path"], op["permission"]) for op in catalog["operations"]]
    registry = [(op.id, op.method, op.path, op.permission.value if op.permission else None)
                for op in OPERATIONS]
    assert [row[0] for row in expected if row not in registry] == [], "operações do catálogo fora do registro"
    assert registry == expected
    doc = create_app(Settings(database_url="postgresql://cq-contrato.invalid/cq")).openapi()
    published = {(method.upper(), path): item[method].get("operationId")
                 for path, item in doc["paths"].items() for method in HTTP_METHODS if method in item}
    assert {op_id: published.get((method, path)) for op_id, method, path, _ in expected} == {
        op_id: op_id for op_id, _, _, _ in expected}


def test_client_does_not_import_server() -> None:
    """Portas e contratos; o CLI consome a API (verde desde o início): o pacote cliente não importa nem depende
    de `cq_server` (código, `pyproject.toml`, `uv.lock` e importação real)."""
    sources = sorted((REPO / "client" / "src" / "cq_client").rglob("*.py"))
    assert sources, "pacote cliente sem módulos"
    offenders = {str(p.relative_to(REPO)): hits for p in sources
                 if (hits := _imports_server(ast.parse(p.read_text(encoding="utf-8"))))}
    assert offenders == {}

    project = tomllib.loads((REPO / "client" / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = [re.split(r"[\s\[<>=!~;]", d, maxsplit=1)[0].lower().replace("_", "-")
            for d in project.get("dependencies", [])]
    assert "cq-server" not in deps, deps
    for extra in project.get("optional-dependencies", {}).values():
        assert all("cq-server" not in d.lower().replace("_", "-") for d in extra)

    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    client_pkgs = [p for p in lock["package"] if p["name"] == "cq-client"]
    assert len(client_pkgs) == 1
    assert "cq-server" not in {d["name"] for d in client_pkgs[0].get("dependencies", [])}

    probe = ("import sys, cq_client, cq_client.api, cq_client.profile, cq_client.cli; "
             "print(sorted(m for m in sys.modules if m == 'cq_server' or m.startswith('cq_server.')))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60,
                         cwd=str(REPO))
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]"


# --- catálogo × API e catálogo × CLI -----------------------------------------------------------

def test_openapi_paths_and_methods_match_catalog(app: Any) -> None:
    """`paths` × métodos de `app.openapi()`, tirando
    `/openapi.json`, é igual ao conjunto do catálogo."""
    doc = app.openapi()
    pairs = {(m.upper(), path) for path, item in doc["paths"].items() for m in HTTP_METHODS if m in item}
    assert pairs - {("GET", "/openapi.json")} == _catalog_pairs()


def test_registered_routes_match_catalog(app: Any) -> None:
    """'Toda rota da API está no catálogo': as rotas registradas no app,
    inclusive as fora do OpenAPI, tirando `/openapi.json`, são exatamente as do catálogo."""
    pairs = {(m, p) for m, p in _route_pairs(app.routes) if p != "/openapi.json"}
    assert pairs == _catalog_pairs()


def test_cli_leaves_match_catalog_and_take_no_token() -> None:
    """as folhas da árvore do `cli` são exatamente os `cli`
    do catálogo; nenhum parâmetro da cadeia (grupos e folha) se chama `token` ou `secret`.

    Oferecimentos e bots (o token do bot; nenhum segredo no modelo): as 10 folhas
    dessa área; nenhum parâmetro se chama `bot_token` nem tem a opção `--bot-token`; a folha
    `bots register` só tem o argumento `REF` (obrigatório) e a opção `--token-file` (opcional, com
    valor): nenhuma opção ou argumento recebe o token.

    Vínculo do aluno: as 5 folhas (`students import`,
    `students summary`, `invites send prepare`, `invites send confirm`, `invites resend`), também sem
    parâmetro `token`/`secret`; forma exata, no formato de `_param_shape`, comparada como lista ordenada
    por `repr` (como a de `bots register`): `students import` = argumento `REF` (obrigatório) e
    `--file` (obrigatório, com valor, texto simples); `invites resend` = argumento `REF`, argumento
    variádico obrigatório `PSEUDONYM...` (`nargs=-1`, como o `REF...` do `identity reveal`) e
    `--valid-hours` (opcional, com valor). Vermelho no esqueleto: o CLI não tem os comandos novos
    na árvore.

    Disparo e resposta: as 6 folhas (`dispatches prepare`, `dispatches
    show`, `dispatches list`, `dispatches confirm`, `dispatches cancel`, `messages list`), também sem
    parâmetro `token`/`secret`; forma exata, no formato de `_param_shape`, comparada como lista ordenada
    por `repr`: `dispatches prepare` = `REF` e as opções obrigatórias `--repo`, `--commit` e `--qid` e as
    opcionais `--opens-at` e `--closes-at`, todas com valor (o CLI não manda `origin`); `dispatches
    show`/`confirm`/`cancel` = `REF` e `DISPATCH_ID`; `dispatches list` e `messages list` = `REF`,
    `--after` e `--limit` (opcionais, com valor). Vermelho no esqueleto: o CLI não tem os comandos novos
    na árvore.

    Mensagens livres opcionais por oferecimento (forma das folhas): a folha
    `offerings settings` = `REF`, `--free-messages` (escolha `on`/`off`, com valor), `--contact-email`
    (com valor) e `--clear-contact-email` (flag), todas opcionais; `offerings create` = `CODE`, as opções
    obrigatórias `--course`, `--term` e `--timezone`, e as opcionais `--response-window`,
    `--free-messages` (escolha `on`/`off`) e `--contact-email`, com valor. Vermelho: o CLI não tem a folha
    nem as opções novas.

    Relatórios e acompanhamento (forma das folhas): as 5 folhas, também sem parâmetro
    `token`/`secret`; forma exata, no formato de `_param_shape`, comparada como lista ordenada por `repr`:
    `reports show` = `REF`, `DISPATCH_ID`, `--version` (opcional, com valor) e `--csv` (flag); `reports
    versions` = `REF`, `DISPATCH_ID`, `--after` e `--limit` (como `dispatches list`); `reports resend` =
    `REF` e `DISPATCH_ID`; `offerings digest show` = `REF`; `offerings digest set` = `REF`, `--enabled`
    (escolha `on`/`off`, com valor) e `--at` (com valor), ambas opcionais. Vermelho: o CLI não tem as folhas
    novas.

    CLI (operações locais no catálogo, sem endpoint): as folhas da árvore são exatamente a união
    dos `cli` do catálogo com `LOCAL_COMMANDS` (`up`, `down`, `status`, `logs`), conjuntos disjuntos; a opção
    global `--profile` (opcional, com valor) está na cadeia de toda folha, e nenhum parâmetro da cadeia,
    inclusive ela, recebe token ou segredo; forma exata dessas folhas, no formato de `_param_shape`:
    `doctor` sem parâmetros; `offerings purge prepare` = `REF`; `offerings purge confirm` = `REF` e
    `--confirmation` (obrigatória, com valor, como `offerings archive confirm`); `events list` = `--type`
    (opcional, repetível, com valor), `--person` (opcional, com valor), `--after` e `--limit`. Vermelho: o CLI
    não tem as folhas novas, os comandos locais nem o `--profile`.

    Operação por agente (a folha `cq mcp`): a árvore
    é a união dos `cli` do catálogo, de `LOCAL_COMMANDS` e da folha `MCP_COMMAND` (`mcp`, de
    `cq_client.cli`), os três conjuntos disjuntos. A folha `mcp` não tem parâmetro
    próprio: a cadeia dela é exatamente a dos parâmetros da raiz, com o `--profile` global, e a proibição de token e
    segredo vale para ela como para toda folha. Na mesma função, o caso que falha no esqueleto: `cq mcp` em processo
    pelo `CliRunner`, com o stdin vazio (EOF imediato) e o perfil num arquivo de teste inexistente, sai com 0 e
    não escreve nada no stdout (o canal do protocolo). Vermelho no esqueleto: a folha `mcp` está ausente
    da árvore do `cq` até a implementação."""
    from cq_client.cli import MCP_COMMAND, cli
    from cq_client.local_commands import LOCAL_COMMANDS

    leaves = _cli_leaves(cli)
    catalog_leaves = {op["cli"] for op in load_catalog()["operations"]}
    # A árvore é a união do catálogo com os comandos locais, disjuntos.
    assert LOCAL_COMMANDS == frozenset({"up", "down", "status", "logs"})
    assert catalog_leaves.isdisjoint(LOCAL_COMMANDS)
    # A folha `mcp`, fora do catálogo e dos comandos locais.
    assert MCP_COMMAND not in catalog_leaves and MCP_COMMAND not in LOCAL_COMMANDS
    assert sorted((catalog_leaves | LOCAL_COMMANDS | {MCP_COMMAND}) - set(leaves)) == [], (
        "folhas ausentes da árvore do cli")
    assert set(leaves) == catalog_leaves | LOCAL_COMMANDS | {MCP_COMMAND}
    offending = sorted(
        f"{leaf}: {param.name}" for leaf, params in leaves.items() for param in params
        if param.name in FORBIDDEN_PARAM_NAMES
        or FORBIDDEN_OPTS & {*getattr(param, "opts", []), *getattr(param, "secondary_opts", [])})
    assert offending == []

    register = _cli_command(cli, "bots register")
    assert sorted((_param_shape(p) for p in register.params), key=repr) == [
        ("argument", "REF", True, 1),
        ("option", ("--token-file",), (), False, False, False, 1),
    ]

    # Vínculo do aluno: forma exata das folhas `students import` e `invites resend`.
    students_import = _cli_command(cli, "students import")
    assert sorted((_param_shape(p) for p in students_import.params), key=repr) == [
        ("argument", "REF", True, 1),
        ("option", ("--file",), (), True, False, False, 1),
    ]
    invites_resend = _cli_command(cli, "invites resend")
    assert sorted((_param_shape(p) for p in invites_resend.params), key=repr) == [
        ("argument", "PSEUDONYM...", True, -1),
        ("argument", "REF", True, 1),
        ("option", ("--valid-hours",), (), False, False, False, 1),
    ]

    # Disparo e resposta: forma exata das 6 folhas do disparo.
    prepare = _cli_command(cli, "dispatches prepare")
    assert sorted((_param_shape(p) for p in prepare.params), key=repr) == sorted([
        ("argument", "REF", True, 1),
        ("option", ("--repo",), (), True, False, False, 1),
        ("option", ("--commit",), (), True, False, False, 1),
        ("option", ("--qid",), (), True, False, False, 1),
        ("option", ("--opens-at",), (), False, False, False, 1),
        ("option", ("--closes-at",), (), False, False, False, 1),
    ], key=repr)
    for leaf in ("dispatches show", "dispatches confirm", "dispatches cancel"):
        command = _cli_command(cli, leaf)
        assert sorted((_param_shape(p) for p in command.params), key=repr) == [
            ("argument", "DISPATCH_ID", True, 1),
            ("argument", "REF", True, 1),
        ], leaf
    for leaf in ("dispatches list", "messages list"):
        command = _cli_command(cli, leaf)
        assert sorted((_param_shape(p) for p in command.params), key=repr) == [
            ("argument", "REF", True, 1),
            ("option", ("--after",), (), False, False, False, 1),
            ("option", ("--limit",), (), False, False, False, 1),
        ], leaf

    # Mensagens livres: forma exata de `offerings settings` e de `offerings create` com as opções de mensagens livres.
    settings = _cli_command(cli, "offerings settings")
    assert sorted((_param_shape(p) for p in settings.params), key=repr) == sorted([
        ("argument", "REF", True, 1),
        ("option", ("--free-messages",), (), False, False, False, 1),
        ("option", ("--contact-email",), (), False, False, False, 1),
        ("option", ("--clear-contact-email",), (), False, True, False, 1),
    ], key=repr)
    free_messages = next(p for p in settings.params if "--free-messages" in getattr(p, "opts", ()))
    assert isinstance(free_messages.type, click.Choice)
    assert list(free_messages.type.choices) == ["on", "off"]
    create = _cli_command(cli, "offerings create")
    assert sorted((_param_shape(p) for p in create.params), key=repr) == sorted([
        ("argument", "CODE", True, 1),
        ("option", ("--course",), (), True, False, False, 1),
        ("option", ("--term",), (), True, False, False, 1),
        ("option", ("--timezone",), (), True, False, False, 1),
        ("option", ("--response-window",), (), False, False, False, 1),
        ("option", ("--free-messages",), (), False, False, False, 1),
        ("option", ("--contact-email",), (), False, False, False, 1),
    ], key=repr)
    create_free = next(p for p in create.params if "--free-messages" in getattr(p, "opts", ()))
    assert isinstance(create_free.type, click.Choice)
    assert list(create_free.type.choices) == ["on", "off"]

    # Relatórios: forma exata das 5 folhas dos relatórios.
    show = _cli_command(cli, "reports show")
    assert sorted((_param_shape(p) for p in show.params), key=repr) == sorted([
        ("argument", "REF", True, 1),
        ("argument", "DISPATCH_ID", True, 1),
        ("option", ("--version",), (), False, False, False, 1),
        ("option", ("--csv",), (), False, True, False, 1),
    ], key=repr)
    versions = _cli_command(cli, "reports versions")
    assert sorted((_param_shape(p) for p in versions.params), key=repr) == sorted([
        ("argument", "REF", True, 1),
        ("argument", "DISPATCH_ID", True, 1),
        ("option", ("--after",), (), False, False, False, 1),
        ("option", ("--limit",), (), False, False, False, 1),
    ], key=repr)
    resend = _cli_command(cli, "reports resend")
    assert sorted((_param_shape(p) for p in resend.params), key=repr) == [
        ("argument", "DISPATCH_ID", True, 1),
        ("argument", "REF", True, 1),
    ]
    digest_show = _cli_command(cli, "offerings digest show")
    assert [_param_shape(p) for p in digest_show.params] == [("argument", "REF", True, 1)]
    digest_set = _cli_command(cli, "offerings digest set")
    assert sorted((_param_shape(p) for p in digest_set.params), key=repr) == sorted([
        ("argument", "REF", True, 1),
        ("option", ("--enabled",), (), False, False, False, 1),
        ("option", ("--at",), (), False, False, False, 1),
    ], key=repr)
    enabled = next(p for p in digest_set.params if "--enabled" in getattr(p, "opts", ()))
    assert isinstance(enabled.type, click.Choice)
    assert list(enabled.type.choices) == ["on", "off"]

    # CLI: a opção global `--profile` na raiz (nunca ecoada; não recebe token) e, por
    # isso, na cadeia de toda folha; forma exata das folhas do expurgo e do `doctor` e de `events list` com `--person`.
    profile_options = [p for p in cli.params if "--profile" in getattr(p, "opts", ())]
    assert [_param_shape(p) for p in profile_options] == [("option", ("--profile",), (), False, False, False, 1)]
    assert profile_options[0].name not in FORBIDDEN_PARAM_NAMES
    assert sorted(leaf for leaf, params in leaves.items() if profile_options[0] not in params) == []
    assert [_param_shape(p) for p in _cli_command(cli, "doctor").params] == []
    purge_prepare = _cli_command(cli, "offerings purge prepare")
    assert [_param_shape(p) for p in purge_prepare.params] == [("argument", "REF", True, 1)]
    purge_confirm = _cli_command(cli, "offerings purge confirm")
    assert sorted((_param_shape(p) for p in purge_confirm.params), key=repr) == [
        ("argument", "REF", True, 1),
        ("option", ("--confirmation",), (), True, False, False, 1),
    ]
    events_list = _cli_command(cli, "events list")
    assert sorted((_param_shape(p) for p in events_list.params), key=repr) == sorted([
        ("option", ("--type",), (), False, False, True, 1),
        ("option", ("--person",), (), False, False, False, 1),
        ("option", ("--after",), (), False, False, False, 1),
        ("option", ("--limit",), (), False, False, False, 1),
    ], key=repr)

    # Operação por agente: a folha `mcp` sem parâmetros próprios, filha direta da raiz; a cadeia dela é
    # a da raiz (com o `--profile` global), sem token nem segredo.
    mcp_leaf = _cli_command(cli, MCP_COMMAND)
    assert [_param_shape(p) for p in mcp_leaf.params] == []
    assert leaves[MCP_COMMAND] == list(cli.params)
    assert profile_options[0] in leaves[MCP_COMMAND]
    assert [p.name for p in leaves[MCP_COMMAND]
            if p.name in FORBIDDEN_PARAM_NAMES
            or FORBIDDEN_OPTS & {*getattr(p, "opts", []), *getattr(p, "secondary_opts", [])}] == []

    # Caso que falha no esqueleto e passa depois: `cq mcp` com o stdin vazio sai com 0 e o stdout (o canal do protocolo) vazio.
    from tests.helpers.cli_runner import invoke_cli

    result = invoke_cli([MCP_COMMAND])
    assert result.exit_code == 0, (result.exit_code, result.stderr[-500:])
    assert result.stdout == ""

