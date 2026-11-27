"""Canário do servidor MCP.

Arquivo à parte do canário congelado (`tests/privacy/test_canary.py`); a semente
(`tests/helpers/calls.py`, `tests/helpers/purge.py`) e os detectores (`tests/helpers/canary.py`,
`tests/helpers/secrets.py`) são reusados.

Semente (no banco do teste, pelas rotas e pelos ajudantes das fases anteriores):
- `O4`, um oferecimento cuja disciplina (texto de operador) cita `equipe`, cadastrado antes da semente do expurgo (a
  redação da gravação ainda não a conhece; só a saída a redige), como `tests/privacy/test_purge_privacy.py`;
- `seed_state` (os alvos de toda operação do catálogo, inclusive os oferecimentos preparados para o expurgo);
- as pessoas canário da API base (`CANARY_PEOPLE`, as da revelação) e o aluno canário `STUDENT_A` importado (a sentinela
  de entrada do motivo da revelação e do nome do arquivo da lista: cadastrada, a redação a conhece);
- a semente do expurgo (`seed_purge_scenario`) e uma segunda resposta de texto em `O2` que cita `citada` (o mínimo de
  ocorrências da distribuição `texts`);
- o perfil `local` (o endereço de teste `TEST_API_URL`, o diretório da pilha com o `.env` de teste e o token do
  perfil, um token novo com todas as permissões) e o perfil `remoto`; a API dos testes por `http=`.

Em cada era (`MODES`), um servidor por superfície, todos em processo:
- `principal`: as 38 ferramentas (o conjunto calculado, catálogo exposto ∪ `LOCAL_TOOL_BINDINGS`) com sucesso, as
  locais pelo `FakeStack` (nível 1) com as amostras de `tests/helpers/log_samples.py`; o expurgo de `O` pelas
  ferramentas (preparo e confirmação); erros de execução (código errado, alvo inexistente, arquivo da lista
  relativo e fora do formato, código do `down` usado, nada a parar); erros de protocolo com o nome canário na
  chave e no valor (toda ferramenta) e a ferramenta desconhecida; o banco de questões local: o envio de um banco
  válido do `tmp_path` com um nome neutro, a listagem, e as recusas locais (a pasta relativa e a pasta sem
  `infoCourse.json`), só com a mensagem fixa do motivo;
- `nivel2`: as 5 locais pelo `FakeRunner` (nível 2), com as mesmas amostras de registros no `docker compose logs`;
- `sem-permissao`: toda ferramenta da API com um token só de `people:read` (403 com a linha da permissão, salvo as
  de `people:read`; a revelação negada é o controle negativo);
- `remoto`: as 5 locais com o perfil remoto (`stack.RemoteProfileRefused`, sem tocar a pilha).

Varredura (`find_canary` com os prefixos das fases anteriores e `find_secrets`, pelo `scan` de `tests/helpers/mcp_client.py`):
o estruturado, o texto, o `raw`, os erros de execução e de protocolo, o `tools/list` e o `deps.stderr` de cada
servidor (só linhas de `MCP_STDERR_MESSAGES`, nenhuma de falha interna). Zero identidade fora do sucesso da
`reveal_identities` com pessoas (controle positivo: exatamente os alvos das pessoas reveladas, no estruturado e no
texto; o 403 e o só `not_found` sem nenhum), salvo as exceções do expurgo, cada uma com controle positivo (antes
do expurgo, a mesma consulta sai redigida) e contagem exata (por era: a disciplina de `O4` no `get_offering` e no
`list_offerings`; o `raw_input` de `O2` no `get_dispatch_report` no momento). Zero segredo: o token do perfil, o
token inicial, o somente leitura, os tokens de bot, as chaves-mestras, os sentinelas dos registros, do `.env` e do
Docker falso. Controle contra o vazio: cada ferramenta com ao menos um sucesso e um erro em cada era.

`test_mcp_canary_subprocess_stderr_has_no_identity_or_secret`: o `cq mcp` num subprocesso, com a API inalcançável
(`http://127.0.0.1:9`), roda uma chamada de cada ferramenta da API (o erro fixo `api_unreachable`; o envio do banco
lê um banco válido do `tmp_path` antes do pedido) e erros de
protocolo com o nome canário; o stderr do filho tem só linhas de `MCP_STDERR_MESSAGES`, sem identidade nem segredo.

No esqueleto, `build_server` lança `NotImplementedError` (o primeiro teste, depois da semente); a folha
`mcp` está ausente da árvore do `cq` (o segundo, conferido antes de abrir o subprocesso).
"""

from __future__ import annotations

import io
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from cq_client import roster
from cq_client.mcp_server import (
    ERROR_TEXT_TEMPLATE,
    INVALID_PARAMS_CODE,
    LOCAL_TOOL_BINDINGS,
    LOGS_TAIL_MAX,
    MCP_MESSAGES,
    MCP_STDERR_MESSAGES,
    PERMISSION_LINE_TEMPLATE,
    PROTOCOL_MESSAGES,
)
from tests.helpers import log_samples, mcp_client
from tests.helpers import banks as bank_fixtures  # banco de questões local: o banco válido do `tmp_path`
from cq_client.banks import LOCAL_MESSAGES  # banco de questões local: as mensagens fixas das recusas locais
from tests.helpers.api import ADMIN_TOKEN, create_offering, create_token, import_students, register_person
from tests.helpers.calls import SEED_COMMIT, SEED_QID, SEED_REPO, seed_state
from tests.helpers.canary import (
    CANARY_PEOPLE,
    OLIMPIO,
    PURGE_C,
    PURGE_D,
    STUDENT_A,
    STUDENT_B,
    ZEFIRO,
    find_canary,
    fold,
)
from tests.helpers.cli_runner import ENV_SENTINEL, ENV_SENTINEL_TOKEN, UNREACHABLE_API_URL, make_stack_dir, write_profile
from tests.helpers.clock import FakeClock
from tests.helpers.contract import load_catalog
from tests.helpers.docker_fake import SENTINEL_SECRET, FakeRunner
from tests.helpers.secrets import all_secrets, bot_secrets
from tests.helpers.stack_fake import TEST_API_URL, FakeSleep, FakeStack

MODES = mcp_client.MODES
SCAN_PREFIXES = mcp_client.SCAN_PREFIXES

# As exceções nomeadas declaradas do expurgo (os textos são os de `tests/privacy/test_purge_privacy.py`).
OPERATOR_COURSE = f"Monitoria com {PURGE_C.name}"  # a disciplina de `O4` cita `equipe` (apagada)
CITED_ANSWER = f"igual à de {PURGE_D.name}"  # a resposta de texto de `O2` cita `citada` (apagada)
OPERATOR_TEXT_BEFORE_PURGE = "texto de operador anterior ao expurgo"
OTHER_OFFERING_RAW_INPUT = "raw_input de outro oferecimento"
REVEAL = "revelação com pessoas"

# As sentinelas de entrada (o aluno canário cadastrado): no motivo da revelação e no nome do arquivo da lista.
REVEAL_REASON = f"conferência pedida por {STUDENT_A.name}"
LIST_FILE_PREFIX = f"lista de {STUDENT_A.name}"

# O nome canário como chave desconhecida (os esquemas de entrada têm `additionalProperties: false`).
CANARY_KEY = fold(ZEFIRO.name).replace(" ", "_")
# Um código de confirmação de formato aceito pela API, nunca emitido, com o nome canário dobrado.
WRONG_CODE = fold(ZEFIRO.name).replace(" ", "-")
# Um oferecimento de formato válido que não existe.
MISSING_OFFERING = "mcp-canario-inexistente"
REMOTE_URL = "https://cq.example.org"

TOOLS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "mcp" / "v1"


# ---------------------------------------------------------------------------------------------
# Conjuntos e textos esperados
# ---------------------------------------------------------------------------------------------


def _catalog_tools() -> dict[str, dict]:
    """As ferramentas do catálogo exposto: nome → operação."""
    return {op["mcp"]["tool"]: op for op in load_catalog()["operations"] if op["mcp"]["exposed"]}


def _local_tools() -> set[str]:
    return {binding.name for bindings in LOCAL_TOOL_BINDINGS.values() for binding in bindings}


def _all_tools() -> set[str]:
    """O conjunto calculado (sem total): catálogo exposto ∪ `LOCAL_TOOL_BINDINGS`."""
    return set(_catalog_tools()) | _local_tools()


def _input_properties() -> dict[str, set[str]]:
    """As propriedades do esquema de entrada de cada ferramenta (dos arquivos congelados de `contracts/mcp/v1/`)."""
    table = json.loads((TOOLS_DIR / "tools.json").read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for tool in table["tools"]:
        schema = json.loads((TOOLS_DIR / tool["input_schema"]).read_text(encoding="utf-8"))
        out[tool["name"]] = set(schema.get("properties", {}))
    return out


def _err(key: str) -> str:
    """O texto inteiro de um erro de execução sem linhas a mais."""
    code = key[len("api."):] if key.startswith("api.") else key
    return ERROR_TEXT_TEMPLATE.format(code=code, message=MCP_MESSAGES[key])


def _labels(*texts: str) -> set[str]:
    return set(find_canary(" | ".join(texts), prefixes=SCAN_PREFIXES))


def _secrets() -> list[str]:
    """Todos os segredos da varredura: os tokens (do perfil, inicial, somente leitura, de bot), as chaves-mestras e
    os sentinelas dos registros, do `.env` de teste e do Docker falso."""
    return [*all_secrets(), *bot_secrets(), *log_samples.SENTINELS, SENTINEL_SECRET, ENV_SENTINEL,
            ENV_SENTINEL_TOKEN]


def _absent_ref() -> str:
    """Um pseudônimo de formato válido (`PERSON_REF`) que não existe."""
    return "a" + uuid.uuid4().hex[:31]


def _compose_bytes(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------------------------
# Semente
# ---------------------------------------------------------------------------------------------


@dataclass
class _World:
    client: Any
    tmp_path: Path
    state: Any
    people: dict[str, dict]
    o4: str
    scenario: Any
    config: Path
    read_secret: str
    profile_secret: str
    purge_code: list[str] = field(default_factory=list)

    def env(self, profile: str = "local", **extra: str) -> dict[str, str]:
        return {"CQ_CONFIG": str(self.config), "CQ_PROFILE": profile, **extra}


def _second_cited_answer(client: Any, sql: psycopg.Connection, clock: Any, other: Any) -> None:
    """uma segunda resposta de texto em `O2`, igual à da semente (cita `citada`), de uma pessoa neutra, para o
    item passar do mínimo de ocorrências da distribuição `texts` do relatório no momento de `O2` (o molde de
    `_purge_second_cited_answer` do canário congelado)."""
    from tests.helpers.engine import preset
    from tests.helpers.purge import _activate_student, _answered, _endpoint, _import, _part, _person_by_ra

    neutral = {"name": "Pessoa Neutra Resposta Mcp", "ra": "4490000001",
               "email": "pessoa.resposta.mcp@turma.example.org"}
    _import(client, other.code, [neutral])
    person_row, _, _ = _person_by_ra(sql, neutral["ra"])
    now = clock.now()
    chat = 9_490_000_001
    enrollment = _activate_student(sql, person_row, other.offering_row, now - timedelta(hours=3))
    endpoint = _endpoint(sql, person_row, other.bot_row, chat, "active", now - timedelta(hours=3))
    opens = now - timedelta(hours=2)
    _answered(sql, dispatch_row=other.dispatch_row, enrollment_row=enrollment, person_row=person_row,
              bot_row=other.bot_row, endpoint_row=endpoint, chat_id=chat, question=preset("text"),
              raw={"resposta": CITED_ANSWER}, text=CITED_ANSWER, score=0.0, part_derived=[_part(0)], opens=opens,
              answered=opens + timedelta(minutes=30), revealed=now - timedelta(minutes=59))


def _world(client: Any, sql: psycopg.Connection, clock: Any, tmp_path: Path) -> _World:
    from tests.helpers.purge import seed_purge_scenario

    # Antes da semente do expurgo: a disciplina de `O4`, que a redação da gravação ainda não conhece.
    o4 = create_offering(client, ADMIN_TOKEN, course=OPERATOR_COURSE)["code"]
    state = seed_state(client, revoke_targets=1)
    people = {p.name: register_person(client, ADMIN_TOKEN, **p.payload()) for p in CANARY_PEOPLE}
    import_students(client, ADMIN_TOKEN, state.import_offering["code"], [STUDENT_A.row()])
    scenario = seed_purge_scenario(client, sql)
    assert scenario.other is not None and scenario.other.dispatch_id is not None
    _second_cited_answer(client, sql, clock, scenario.other)

    permissions = sorted({op["permission"] for op in load_catalog()["operations"] if op.get("permission")})
    profile_secret, _ = create_token(client, ADMIN_TOKEN, permissions, label="token do perfil do agente")
    read_secret, _ = create_token(client, ADMIN_TOKEN, ["people:read"], label="token somente leitura do agente")

    folder = tmp_path / "mcp-perfil"
    stack_dir = make_stack_dir(folder)
    config = write_profile(folder, "\n".join([
        f'api_url = "{UNREACHABLE_API_URL}"', "",
        "[profiles.local]", 'kind = "local"', f'api_url = "{TEST_API_URL}"', f"stack_dir = '{stack_dir}'",
        f'token = "{profile_secret}"', "",
        "[profiles.remoto]", 'kind = "remote"', f'api_url = "{REMOTE_URL}"', ""]))
    if os.name != "nt":
        os.chmod(config, 0o600)
    return _World(client=client, tmp_path=tmp_path, state=state, people=people, o4=o4, scenario=scenario,
                  config=config, read_secret=read_secret, profile_secret=profile_secret)


def _list_file(folder: Path, name: str, content: str) -> Path:
    path = folder / name
    path.write_bytes(content.encode("utf-8"))
    return path


def _neutral_list(index: int) -> str:
    return (f"ra,nome,email\n77{index:08d},Pessoa Neutra Lista {index},"
            f"pessoa.neutra.lista.{index}@turma.example.org\n")


# ---------------------------------------------------------------------------------------------
# Gravação das chamadas
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Record:
    mode: str
    surface: str
    tool: str | None  # `None`: a ferramenta desconhecida
    kind: str         # "ok", "error" ou "protocol"
    label: str        # "", `REVEAL`, `OPERATOR_TEXT_BEFORE_PURGE` ou `OTHER_OFFERING_RAW_INPUT`
    outcome: mcp_client.Outcome


class _Recorder:
    """Chama uma ferramenta, confere a forma do resultado na hora e grava a chamada para a varredura."""

    def __init__(self, records: list[_Record], mode: str, surface: str, client: Any) -> None:
        self.records, self.mode, self.surface, self.client = records, mode, surface, client

    def _where(self, tool: str | None) -> str:
        return f"{self.surface}/{self.mode} {tool}"

    async def ok(self, tool: str, args: dict, *, label: str = "") -> mcp_client.Outcome:
        out = await mcp_client.call(self.client, tool, args)
        assert out.protocol_code is None and out.is_error is False, (
            f"{self._where(tool)}: esperado sucesso; {out.protocol_message or out.text}")
        assert out.text == json.dumps(out.structured, ensure_ascii=False, separators=(",", ":")), self._where(tool)
        self.records.append(_Record(self.mode, self.surface, tool, "ok", label, out))
        return out

    async def error(self, tool: str, args: dict, *, text: str | None = None) -> mcp_client.Outcome:
        out = await mcp_client.call(self.client, tool, args)
        assert out.protocol_code is None and out.is_error is True, (
            f"{self._where(tool)}: esperado erro de execução; {out.protocol_message or out.text}")
        assert out.structured is None, f"{self._where(tool)}: erro de execução com structuredContent"
        if text is not None:
            assert out.text == text, f"{self._where(tool)}: {out.text!r} != {text!r}"
        self.records.append(_Record(self.mode, self.surface, tool, "error", "", out))
        return out

    async def protocol(self, tool: str, args: dict, *, message: str = "invalid_arguments") -> mcp_client.Outcome:
        out = await mcp_client.call(self.client, tool, args)
        assert out.protocol_code == INVALID_PARAMS_CODE, f"{self._where(tool)}: {out.protocol_code} {out.text}"
        assert out.protocol_message == PROTOCOL_MESSAGES[message], f"{self._where(tool)}: {out.protocol_message}"
        assert "data" not in out.raw, f"{self._where(tool)}: erro de protocolo com data"
        recorded = tool if message == "invalid_arguments" else None
        self.records.append(_Record(self.mode, self.surface, recorded, "protocol", "", out))
        return out


@dataclass
class _Run:
    records: list[_Record] = field(default_factory=list)
    listings: dict[str, list[dict]] = field(default_factory=dict)
    stderrs: dict[tuple[str, str], io.StringIO] = field(default_factory=dict)
    remote_stacks: list[FakeStack] = field(default_factory=list)
    logs_results: list[dict] = field(default_factory=list)


def _api_args(world: _World, i: int, list_path: Path) -> dict[str, dict]:
    """Argumentos de forma válida de cada ferramenta da API, para o servidor sem permissão (403 antes de qualquer
    efeito; nos alvos destrutivos, os alvos de sobra da semente)."""
    st = world.state
    draft, active = st.draft_offering["code"], st.active_offering["code"]
    imp, inv = st.import_offering["code"], st.invite_offering["code"]
    disp, rep = st.dispatch_offering["code"], st.report_offering["code"]
    zefiro = world.people[ZEFIRO.name]["pseudo_id"]
    spare_archive, spare_invite = st.archive_targets[4], st.invite_targets[4]
    return {
        "list_people": {},
        "get_person": {"person": zefiro},
        "reveal_identities": {"pseudonyms": [world.people[p.name]["pseudo_id"] for p in CANARY_PEOPLE],
                              "reason": REVEAL_REASON},
        "list_events": {"types": ["person.registered"]},
        "create_offering": {"code": f"mcp-proibido-{i}", "course": f"Monitoria de {ZEFIRO.name}", "term": "2031s1",
                            "timezone": "UTC"},
        "list_offerings": {},
        "get_offering": {"offering": draft},
        "close_offering": {"offering": active},
        "prepare_archive_offering": {"offering": spare_archive[0]},
        "archive_offering": {"offering": spare_archive[0], "confirmation_code": spare_archive[1]},
        "activate_bot": {"offering": draft},
        "get_bot": {"offering": draft},
        "import_students": {"offering": imp, "path": str(list_path)},
        "get_students_summary": {"offering": inv},
        "prepare_send_invites": {"offering": inv},
        "send_invites": {"offering": spare_invite[0], "confirmation_code": spare_invite[1]},
        "resend_invites": {"offering": inv, "pseudonyms": [st.resend_person["alias"]]},
        "prepare_dispatch": {"offering": disp, "repo": SEED_REPO, "commit": SEED_COMMIT, "qid": SEED_QID},
        "get_dispatch": {"offering": disp, "dispatch_id": st.dispatch_shown},
        "list_dispatches": {"offering": disp},
        "confirm_dispatch": {"offering": disp, "dispatch_id": str(uuid.uuid4())},
        "cancel_dispatch": {"offering": disp, "dispatch_id": str(uuid.uuid4())},
        "update_offering_settings": {"offering": draft, "free_messages": False},
        "get_dispatch_report": {"offering": rep, "dispatch_id": st.report_shown},
        "list_report_versions": {"offering": rep, "dispatch_id": st.report_shown},
        "resend_report_summary": {"offering": rep, "dispatch_id": st.report_shown},
        "get_offering_digest": {"offering": rep},
        "update_offering_digest": {"offering": rep, "enabled": False},
        "run_doctor": {},
        "prepare_purge_offering": {"offering": st.prepare_targets[0]},
        "purge_offering": {"offering": st.purge_targets[1][0], "confirmation_code": st.purge_targets[1][1]},
        # Banco de questões local: a leitura do banco válido passa, e a API recusa pela permissão
        "upload_question_bank": {"path": str(_bank(world.tmp_path, f"banco-sem-permissao-{i}")),
                                 "name": f"banco-proibido-{i}"},
        "list_question_banks": {},
    }


def _bank(root: Path, name: str) -> Path:
    """Banco de questões local: o curso de fixture copiado para `root/<name>` (uma vez), pelo caminho absoluto."""
    folder = root / name
    if not folder.exists():
        bank_fixtures.copy_course(folder)
    return folder.resolve()


def _bank_read(reason: str) -> str:
    """Banco de questões local: o texto inteiro de uma recusa local (a chave `bank.read`, só a mensagem fixa)."""
    return ERROR_TEXT_TEMPLATE.format(code="bank.read", message=LOCAL_MESSAGES[reason])


async def _api_success_and_errors(rec: _Recorder, world: _World, i: int, first: bool, folder: Path) -> None:
    """As ferramentas da API na superfície principal: sucesso, erros de execução e o expurgo de `O` com as exceções
    declaradas (os controles antes do expurgo só na primeira era)."""
    st, sc = world.state, world.scenario
    draft, active = st.draft_offering["code"], st.active_offering["code"]
    imp, inv = st.import_offering["code"], st.invite_offering["code"]
    disp, rep = st.dispatch_offering["code"], st.report_offering["code"]
    other_report = {"offering": sc.other.code, "dispatch_id": sc.other.dispatch_id}

    # --- o expurgo pelas ferramentas e as exceções declaradas ---
    if first:
        # Controles positivos: antes do expurgo, a mesma consulta sai redigida (varridas como as demais).
        await rec.ok("get_offering", {"offering": world.o4})
        await rec.ok("get_dispatch_report", other_report)
        prepared = await rec.ok("prepare_purge_offering", {"offering": sc.code})
        world.purge_code.append(prepared.structured["confirmation_code"])
    else:
        await rec.error("prepare_purge_offering", {"offering": sc.code})  # já expurgado
    await rec.ok("purge_offering", {"offering": sc.code, "confirmation_code": world.purge_code[0]})
    shown = await rec.ok("get_offering", {"offering": world.o4}, label=OPERATOR_TEXT_BEFORE_PURGE)
    assert shown.structured["course"] == OPERATOR_COURSE
    listed = await rec.ok("list_offerings", {"limit": 500}, label=OPERATOR_TEXT_BEFORE_PURGE)
    assert [o["course"] for o in listed.structured["items"] if o["code"] == world.o4] == [OPERATOR_COURSE]
    await rec.ok("get_dispatch_report", other_report, label=OTHER_OFFERING_RAW_INPUT)

    # --- pessoas e eventos ---
    zefiro = world.people[ZEFIRO.name]["pseudo_id"]
    await rec.ok("list_people", {"limit": 500})
    await rec.ok("get_person", {"person": zefiro})
    await rec.error("get_person", {"person": _absent_ref()})
    await rec.ok("list_events", {"types": ["person.registered", "identity.revealed", "offering.purged"],
                                 "limit": 500})
    await rec.ok("list_events", {"person": zefiro})

    # --- oferecimentos e bots ---
    # O código não leva a palavra "canario" (alvo `palavra:canario` de `ZEFIRO`); a
    # resposta da `create_offering` devolve o código, obrigatório e sem redação no contrato da API.
    await rec.ok("create_offering", {"code": f"mcp-oferta-{i}", "course": f"Monitoria de {ZEFIRO.name}",
                                     "term": "2031s1", "timezone": "UTC"})
    await rec.ok("get_offering", {"offering": draft})
    await rec.error("get_offering", {"offering": MISSING_OFFERING})
    await rec.ok("close_offering", {"offering": active})
    archive_ok, archive_err = st.archive_targets[2 * i][0], st.archive_targets[2 * i + 1][0]
    prepared = await rec.ok("prepare_archive_offering", {"offering": archive_ok})
    await rec.ok("archive_offering", {"offering": archive_ok,
                                      "confirmation_code": prepared.structured["confirmation_code"]})
    await rec.error("archive_offering", {"offering": archive_err, "confirmation_code": WRONG_CODE})
    await rec.error("prepare_archive_offering", {"offering": MISSING_OFFERING})
    await rec.ok("activate_bot", {"offering": draft})
    await rec.ok("get_bot", {"offering": draft})
    await rec.ok("update_offering_settings", {"offering": draft, "free_messages": True})

    # --- alunos e convites (a sentinela no nome do arquivo; o conteúdo fora do formato com o aluno canário) ---
    good = _list_file(folder, f"{LIST_FILE_PREFIX} {i}.csv", _neutral_list(i))
    bad = _list_file(folder, f"{LIST_FILE_PREFIX} {i} errada.csv", f"nome;{STUDENT_B.name}\n{STUDENT_B.ra}\n")
    await rec.ok("import_students", {"offering": imp, "path": str(good)})
    await rec.error("import_students", {"offering": imp, "path": good.name}, text=_err("file.not_absolute"))
    await rec.error("import_students", {"offering": imp, "path": str(bad)},
                    text=ERROR_TEXT_TEMPLATE.format(code="file.roster", message=roster.HEADER_REASON))
    await rec.ok("get_students_summary", {"offering": inv})
    await rec.ok("prepare_send_invites", {"offering": inv})
    send_ok, send_err = st.invite_targets[2 * i], st.invite_targets[2 * i + 1]
    await rec.ok("send_invites", {"offering": send_ok[0], "confirmation_code": send_ok[1]})
    await rec.error("send_invites", {"offering": send_err[0], "confirmation_code": WRONG_CODE})
    await rec.ok("resend_invites", {"offering": inv, "pseudonyms": [st.resend_person["alias"]]})

    # --- disparos ---
    await rec.ok("prepare_dispatch", {"offering": disp, "repo": SEED_REPO, "commit": SEED_COMMIT, "qid": SEED_QID})
    await rec.ok("get_dispatch", {"offering": disp, "dispatch_id": st.dispatch_shown})
    await rec.error("get_dispatch", {"offering": disp, "dispatch_id": str(uuid.uuid4())})
    await rec.ok("list_dispatches", {"offering": disp})
    await rec.ok("confirm_dispatch", {"offering": disp, "dispatch_id": st.confirm_targets[i]})
    await rec.error("confirm_dispatch", {"offering": disp, "dispatch_id": st.confirm_targets[i]})  # já confirmado
    await rec.ok("cancel_dispatch", {"offering": disp, "dispatch_id": st.cancel_targets[i]})

    # --- relatórios e resumo ---
    report = {"offering": rep, "dispatch_id": st.report_shown}
    await rec.ok("get_dispatch_report", report)
    await rec.ok("list_report_versions", report)
    await rec.ok("resend_report_summary", report)
    await rec.ok("get_offering_digest", {"offering": rep})
    await rec.ok("update_offering_digest", {"offering": rep, "local_time": "21:00"})

    # --- diagnóstico e expurgo (os alvos da semente) ---
    await rec.ok("run_doctor", {})
    await rec.ok("prepare_purge_offering", {"offering": st.prepare_targets[0]})
    await rec.error("prepare_purge_offering", {"offering": MISSING_OFFERING})
    await rec.error("purge_offering", {"offering": st.purge_targets[1][0], "confirmation_code": WRONG_CODE})

    # --- banco de questões local: o envio (um nome neutro por era), a listagem e as recusas locais ---
    bank = _bank(world.tmp_path, f"banco-principal-{i}")
    not_course = folder / f"banco-sem-curso-{i}"
    (not_course / "questions" / "q1").mkdir(parents=True)
    (not_course / "questions" / "q1" / "info.json").write_bytes(b"{}\n")
    uploaded = await rec.ok("upload_question_bank", {"path": str(bank), "name": f"banco-agente-{i}"})
    assert (uploaded.structured["name"], uploaded.structured["created"]) == (f"banco-agente-{i}", True)
    await rec.error("upload_question_bank", {"path": bank.name, "name": f"banco-agente-{i}"},
                    text=_bank_read("not_absolute"))
    await rec.error("upload_question_bank", {"path": str(not_course), "name": f"banco-agente-{i}"},
                    text=_bank_read("not_a_course"))
    listed = await rec.ok("list_question_banks", {"name": f"banco-agente-{i}"})
    assert [item["name"] for item in listed.structured["items"]] == [f"banco-agente-{i}"]

    # --- a revelação: com as pessoas (controle positivo) e só com pseudônimos inexistentes (controle negativo) ---
    await rec.ok("reveal_identities", {"pseudonyms": [world.people[p.name]["pseudo_id"] for p in CANARY_PEOPLE],
                                       "reason": REVEAL_REASON}, label=REVEAL)
    absent = await rec.ok("reveal_identities", {"pseudonyms": [_absent_ref()], "reason": REVEAL_REASON})
    assert absent.structured["people"] == [] and len(absent.structured["not_found"]) == 1, absent.structured


async def _local_calls(rec: _Recorder, *, nothing_to_stop: bool, run: _Run) -> None:
    """As 5 ferramentas locais com sucesso, e os erros do código usado e (no nível 1) de nada a parar."""
    await rec.ok("stack_status", {})
    logs = await rec.ok("stack_logs", {"tail": LOGS_TAIL_MAX})
    run.logs_results.append(logs.structured)
    await rec.ok("stack_up", {})
    prepared = await rec.ok("prepare_stack_down", {})
    code = prepared.structured["confirmation_code"]
    await rec.ok("stack_down", {"confirmation_code": code})
    await rec.error("stack_down", {"confirmation_code": code}, text=_err("down_code_invalid"))
    if nothing_to_stop:
        await rec.error("prepare_stack_down", {}, text=_err("down_nothing_to_stop"))


async def _era(world: _World, mode: str, i: int, run: _Run) -> None:
    """Uma era: os quatro servidores em processo (ver o docstring do módulo)."""
    first = i == 0
    folder = world.tmp_path / f"listas-{i}"
    folder.mkdir()
    sample_lines = log_samples.lines(log_samples.ALL_SAMPLES.values())
    properties = _input_properties()
    permissions = {tool: op["permission"] for tool, op in _catalog_tools().items()}

    def deps(surface: str, env: dict[str, str], **kwargs: Any) -> Any:
        sleep = FakeSleep(FakeClock())
        stderr = io.StringIO()
        run.stderrs[(mode, surface)] = stderr
        return mcp_client.make_deps(world.tmp_path, env=env, clock=sleep.monotonic, sleep=sleep, stderr=stderr,
                                    **kwargs)

    # --- principal: a API e as locais no nível 1 ---
    stack = FakeStack(["healthy"], logs=log_samples.by_service(log_samples.ALL_SAMPLES.values()))
    async with mcp_client.in_process(deps("principal", world.env(), http=world.client, stack=stack), mode) as c:
        run.listings[mode] = await mcp_client.list_tools(c)
        rec = _Recorder(run.records, mode, "principal", c)
        await _api_success_and_errors(rec, world, i, first, folder)
        await _local_calls(rec, nothing_to_stop=True, run=run)
        # Erros de protocolo com o nome canário na chave e no valor (toda ferramenta) e a ferramenta desconhecida.
        for tool in sorted(_all_tools()):
            await rec.protocol(tool, {CANARY_KEY: ZEFIRO.name})
            if "offering" in properties[tool]:
                await rec.protocol(tool, {"offering": ZEFIRO.name})
        await rec.protocol(f"ferramenta_{CANARY_KEY}", {"nome": OLIMPIO.email}, message="unknown_tool")

    # --- nível 2: as locais pelo `FakeRunner`, com as mesmas amostras no `docker compose logs` ---
    runner = FakeRunner(samples={"logs": _compose_bytes(sample_lines)})
    async with mcp_client.in_process(deps("nivel2", world.env(), http=world.client, runner=runner), mode) as c:
        await _local_calls(_Recorder(run.records, mode, "nivel2", c), nothing_to_stop=False, run=run)

    # --- sem permissão: toda ferramenta da API com o token só de `people:read` ---
    forbidden_list = _list_file(folder, f"{LIST_FILE_PREFIX} {i} sem permissao.csv", _neutral_list(100 + i))
    api_args = _api_args(world, i, forbidden_list)
    assert set(api_args) == set(_catalog_tools())
    async with mcp_client.in_process(deps("sem-permissao", world.env(CQ_TOKEN=world.read_secret),
                                          http=world.client), mode) as c:
        rec = _Recorder(run.records, mode, "sem-permissao", c)
        for tool, args in api_args.items():
            if permissions[tool] == "people:read":
                await rec.ok(tool, args)
                continue
            out = await rec.error(tool, args)
            lines = out.text.splitlines()
            assert lines[0] == _err("api.forbidden"), (tool, lines)
            assert PERMISSION_LINE_TEMPLATE.format(permission=permissions[tool]) in lines[1:], (tool, lines)

    # --- remoto: as locais recusadas antes da pilha ---
    remote_stack = FakeStack(["healthy"])
    run.remote_stacks.append(remote_stack)
    async with mcp_client.in_process(deps("remoto", world.env("remoto"), stack=remote_stack), mode) as c:
        rec = _Recorder(run.records, mode, "remoto", c)
        refused = _err("stack.RemoteProfileRefused")
        for tool in sorted(_local_tools()):
            args = {"confirmation_code": "A" * 22} if tool == "stack_down" else {}
            await rec.error(tool, args, text=refused)


# ---------------------------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------------------------


def test_mcp_canary_sweep_finds_no_identity_or_secret_outside_reveal(
        client: Any, sql: psycopg.Connection, clock: Any, tmp_path: Path, bot_api: Any) -> None:
    """Ver o docstring do módulo: as 38 ferramentas (o conjunto calculado) com sucesso e com
    erro nas duas eras, sobre a semente canário das fases anteriores (inclusive o expurgo); zero identidade fora do sucesso da
    `reveal_identities` com pessoas, salvo as exceções do expurgo com controle positivo e contagem exata; zero segredo;
    o `tools/list` e o `deps.stderr` de cada servidor varridos; controles positivos do detector e contra o vazio.

    No esqueleto, `build_server` lança `NotImplementedError` (a primeira era, depois da semente)."""
    world = _world(client, sql, clock, tmp_path)
    tools = _all_tools()
    run = _Run()
    for i, mode in enumerate(MODES):
        mcp_client.run(_era, world, mode, i, run)
    secrets = _secrets()

    # --- controles positivos do detector (contra a varredura vazia) ---
    assert f"nome:{ZEFIRO.name}" in mcp_client.scan(ZEFIRO.name, [])
    assert mcp_client.scan(f"Bearer {world.profile_secret}", secrets), "o detector não achou o token do perfil"
    assert f"aluno-nome:{STUDENT_A.name}" in mcp_client.scan(REVEAL_REASON, []), "o motivo não leva a sentinela"
    assert f"aluno-nome:{STUDENT_A.name}" in mcp_client.scan(LIST_FILE_PREFIX, []), "o arquivo sem a sentinela"
    sample_text = "\n".join(log_samples.lines(log_samples.ALL_SAMPLES.values()))
    sample_hits = mcp_client.scan(sample_text, secrets)
    assert any(h.startswith("nome:") for h in sample_hits) and any(h.startswith("segredo:") for h in sample_hits), (
        "as amostras de registros não levam identidade canário e segredo-sentinela")
    p714_labels, p731_labels = _labels(OPERATOR_COURSE), _labels(CITED_ANSWER)
    assert f"expurgo-nome:{PURGE_C.name}" in p714_labels and f"expurgo-nome:{PURGE_D.name}" in p731_labels
    revealed = set().union(*(_labels(*(v for v in (p.name, p.ra, p.email) if v)) for p in CANARY_PEOPLE))
    assert {f"nome:{p.name}" for p in CANARY_PEOPLE} <= revealed

    # --- contra o vazio: cada ferramenta com sucesso e com erro, em cada era ---
    for mode in MODES:
        mine = [r for r in run.records if r.mode == mode and r.tool is not None]
        assert {r.tool for r in mine if r.kind == "ok"} == tools, (mode, sorted(tools - {r.tool for r in mine
                                                                                          if r.kind == "ok"}))
        assert {r.tool for r in mine if r.kind != "ok"} == tools, (mode, sorted(tools - {r.tool for r in mine
                                                                                          if r.kind != "ok"}))
        assert {t["name"] for t in run.listings[mode]} == tools, mode
    assert len(run.logs_results) == 2 * len(MODES)
    for logs in run.logs_results:
        assert logs["returned"] > 0 and logs["redacted"] + logs["omitted"] > 0, logs  # a redação agiu

    # --- a varredura de toda chamada ---
    exceptions: list[tuple[str, str, str, frozenset[str]]] = []
    reveals = 0
    for r in run.records:
        hits = set(mcp_client.scan(r.outcome, secrets))
        where = (r.mode, r.surface, r.tool, r.kind)
        if r.label == REVEAL:
            # Controle positivo: exatamente os alvos das pessoas reveladas, no estruturado e no texto; nenhum
            # segredo e nenhuma sentinela de entrada (o aluno do motivo).
            reveals += 1
            assert hits == revealed, (where, sorted(hits ^ revealed))
            assert set(find_canary(r.outcome.text, prefixes=SCAN_PREFIXES)) == revealed, where
            structured = json.dumps(r.outcome.structured, ensure_ascii=False)
            assert set(find_canary(structured, prefixes=SCAN_PREFIXES)) == revealed, where
            continue
        if r.label in (OPERATOR_TEXT_BEFORE_PURGE, OTHER_OFFERING_RAW_INPUT):
            exceptions.append((r.mode, r.label, r.tool or "", frozenset(hits)))
            continue
        assert hits == set(), (where, sorted(hits), (r.outcome.text or "")[:300])
    assert reveals == len(MODES), f"revelações com pessoas: {reveals}"
    expected = [(mode, OPERATOR_TEXT_BEFORE_PURGE, "get_offering", frozenset(p714_labels)) for mode in MODES]
    expected += [(mode, OPERATOR_TEXT_BEFORE_PURGE, "list_offerings", frozenset(p714_labels)) for mode in MODES]
    expected += [(mode, OTHER_OFFERING_RAW_INPUT, "get_dispatch_report", frozenset(p731_labels)) for mode in MODES]
    assert Counter(exceptions) == Counter(expected), sorted(set(exceptions) ^ set(expected), key=str)

    # --- o `tools/list` e o stderr de cada servidor ---
    for mode, listing in run.listings.items():
        assert mcp_client.scan(listing, secrets) == [], mode
    assert set(run.stderrs) == {(mode, s) for mode in MODES for s in ("principal", "nivel2", "sem-permissao",
                                                                       "remoto")}
    for key, stream in run.stderrs.items():
        lines = stream.getvalue().splitlines()
        assert [line for line in lines if line not in MCP_STDERR_MESSAGES.values()] == [], (key, lines)
        assert MCP_STDERR_MESSAGES["internal"] not in lines, key
        assert mcp_client.scan(stream.getvalue(), secrets) == [], key
    # O perfil remoto foi recusado antes da pilha (nenhuma chamada ao dublê).
    assert [stack.calls for stack in run.remote_stacks] == [[] for _ in MODES]


@pytest.mark.parametrize("mode", MODES)
def test_mcp_canary_subprocess_stderr_has_no_identity_or_secret(tmp_path: Path, mode: str) -> None:
    """o `cq mcp` num subprocesso, com a API inalcançável (`http://127.0.0.1:9`) e o token inicial
    no ambiente, roda uma chamada de cada ferramenta da API (o erro fixo `api_unreachable`, sem identidade nem
    segredo) e erros de protocolo com o nome canário; o stderr do filho tem só linhas de `MCP_STDERR_MESSAGES`, sem a
    de falha interna, sem identidade e sem segredo.

    No esqueleto, a folha `mcp` está ausente da árvore do `cq` (conferida antes do subprocesso)."""
    import click

    from cq_client.cli import MCP_COMMAND, cli

    assert cli.get_command(click.Context(cli), MCP_COMMAND) is not None, "a folha `mcp` não está na árvore do cq"

    list_path = _list_file(tmp_path, f"{LIST_FILE_PREFIX}.csv", _neutral_list(200))
    ref, person, dispatch_id = "mcp-canario-sub", _absent_ref(), str(uuid.uuid4())
    args = {
        "list_people": {}, "get_person": {"person": person},
        "reveal_identities": {"pseudonyms": [person], "reason": REVEAL_REASON},
        "list_events": {"types": ["person.registered"]},
        "create_offering": {"code": ref, "course": f"Monitoria de {ZEFIRO.name}", "term": "2031s1",
                            "timezone": "UTC"},
        "list_offerings": {}, "get_offering": {"offering": ref}, "close_offering": {"offering": ref},
        "prepare_archive_offering": {"offering": ref},
        "archive_offering": {"offering": ref, "confirmation_code": WRONG_CODE},
        "activate_bot": {"offering": ref}, "get_bot": {"offering": ref},
        "import_students": {"offering": ref, "path": str(list_path)},
        "get_students_summary": {"offering": ref}, "prepare_send_invites": {"offering": ref},
        "send_invites": {"offering": ref, "confirmation_code": WRONG_CODE},
        "resend_invites": {"offering": ref, "pseudonyms": [person]},
        "prepare_dispatch": {"offering": ref, "repo": SEED_REPO, "commit": SEED_COMMIT, "qid": SEED_QID},
        "get_dispatch": {"offering": ref, "dispatch_id": dispatch_id},
        "list_dispatches": {"offering": ref},
        "confirm_dispatch": {"offering": ref, "dispatch_id": dispatch_id},
        "cancel_dispatch": {"offering": ref, "dispatch_id": dispatch_id},
        "update_offering_settings": {"offering": ref, "free_messages": True},
        "get_dispatch_report": {"offering": ref, "dispatch_id": dispatch_id},
        "list_report_versions": {"offering": ref, "dispatch_id": dispatch_id},
        "resend_report_summary": {"offering": ref, "dispatch_id": dispatch_id},
        "get_offering_digest": {"offering": ref},
        "update_offering_digest": {"offering": ref, "local_time": "21:00"},
        "run_doctor": {},
        "prepare_purge_offering": {"offering": ref},
        "purge_offering": {"offering": ref, "confirmation_code": WRONG_CODE},
        # Banco de questões local: a leitura do banco válido passa antes do pedido à API inalcançável
        "upload_question_bank": {"path": str(_bank(tmp_path, "banco-subprocesso")), "name": "banco-subprocesso"},
        "list_question_banks": {},
    }
    assert set(args) == set(_catalog_tools())
    env = {"CQ_CONFIG": str(tmp_path / mcp_client.MISSING_PROFILE_FILE), "CQ_API_URL": UNREACHABLE_API_URL,
           "CQ_TOKEN": ADMIN_TOKEN}
    errlog = tmp_path / "cq-mcp-stderr.txt"
    records: list[_Record] = []

    async def go() -> None:
        params = mcp_client.stdio_server_params(env, tmp_path)
        async with mcp_client.stdio_session(params, mode, errlog) as c:
            rec = _Recorder(records, mode, "subprocesso", c)
            for tool, tool_args in args.items():
                await rec.error(tool, tool_args, text=_err("api_unreachable"))
            await rec.protocol("get_offering", {"offering": ZEFIRO.name, CANARY_KEY: OLIMPIO.email})
            await rec.protocol(f"ferramenta_{CANARY_KEY}", {"nome": ZEFIRO.name}, message="unknown_tool")

    mcp_client.run(go)
    secrets = _secrets()
    assert {r.tool for r in records if r.kind == "error"} == set(_catalog_tools())
    for r in records:
        assert mcp_client.scan(r.outcome, secrets) == [], (r.tool, r.kind)
    text = errlog.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert [line for line in lines if line not in MCP_STDERR_MESSAGES.values()] == [], lines
    assert MCP_STDERR_MESSAGES["internal"] not in lines
    assert mcp_client.scan(text, secrets) == []
    # Controle positivo: o detector acharia o token inicial e o nome canário no stderr.
    assert mcp_client.scan(f"{text}\n{ADMIN_TOKEN} {ZEFIRO.name}", secrets)
