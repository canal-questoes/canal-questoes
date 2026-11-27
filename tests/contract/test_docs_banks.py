"""Testes dos textos públicos do banco de questões local: o guia de operação novo, as referências da API, do CLI e
do MCP, a skill de disparo, a política de retenção, o tutorial e o registro de mudanças.

Os documentos são **lidos**, nunca escritos; nenhum comando deles é executado. Onde cada um é lido:
- pela raiz do repositório, só o que a lista de leituras dos testes já cobre: o guia
  `docs/operacao/banco-local.md`, a política de retenção e a skill de disparo do plugin;
- pela árvore exportada do último commit (a fixture `public_root`, como em `tests/contract/test_public_tree.py`):
  as referências (`docs/referencia/`), o tutorial (`docs/tutorial/primeiro-uso.md`) e o `CHANGELOG.md`.

O que cada função confere:
- o guia: as seções das duas origens, do nome, da versão, dos limites, do perfil remoto e do que fica guardado (e
  por quanto tempo), com o conteúdo mínimo de cada uma; os avisos de LFS, de caixa e de NFC/NFD; a linha pronta do
  preparo do disparo com a referência `bank:`; as regras comuns dos documentos de operação
  (`tests/contract/test_ops_docs.py`: comandos proibidos, caminhos citados, comandos `cq` da árvore, variáveis
  `CQ_*` conhecidas e a varredura dos detectores) e nenhum código interno;
- as referências: a da API cita as duas operações, o método e o caminho, e os 5 motivos novos; a do CLI, os dois
  comandos e as opções; a do MCP, as duas ferramentas e o `git_rev`;
- a skill de disparo: no procedimento, o envio do banco local (`upload_question_bank`, com a referência `bank:`)
  vem antes do preparo (`prepare_dispatch`);
- a política de retenção: os mesmos títulos de antes (o texto novo fica dentro das seções existentes) e um
  parágrafo que diz que o banco enviado fica guardado;
- o tutorial: os mesmos títulos de antes, só uma nota na seção do disparo, com o link para o guia, e o resto do
  tutorial sem o banco local; o disparo continua pelo repositório de demonstração;
- o `CHANGELOG.md`: a seção `[Unreleased]` existe, e uma entrada na categoria Adicionado dela ou de uma seção de
  versão cita os comandos e as ferramentas (na publicação, a entrada passa do `[Unreleased]` à seção da versão).

Vermelho no esqueleto: o guia não existe, e as referências, a skill, a política, o tutorial e o registro de
mudanças ainda não têm o texto do banco local (até a escrita dos textos públicos e o commit deles).

**[I]** Escolhas deste arquivo:
- uma seção do guia é achada pelo título `##` que contém a palavra da tabela `GUIDE_SECTIONS` (sem diferenciar
  maiúsculas), não pelo título exato; o conteúdo mínimo é conferido por fragmentos, sem as marcas `**` e as crases;
- nos números do guia, o separador de milhar (ponto ou espaço) é ignorado (`20.000` e `20 000` valem `20000`);
- a referência de cada superfície é conferida no arquivo dela (`api.md`, `cli.md`, `mcp.md`), não na união;
- a linha pronta do preparo é um trecho de código (crase ou bloco cercado) com `cq dispatches prepare`, `--repo
  bank:` e `--commit`.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

import pytest

from tests.contract.test_ops_docs import (
    RETENTION_POLICY,
    RETENTION_TITLES,
    _check_common,
    _code_texts,
    _doc,
    _has_paragraph,
    _norm,
    _sections,
    _titles,
)
from tests.helpers import internal_codes
from tests.helpers.public_tree import exported_root, markdown_links

GUIDE = "docs/operacao/banco-local.md"
DISPATCH_SKILL = "plugins/canal-questoes/skills/disparo/SKILL.md"
API_REFERENCE = "docs/referencia/api.md"
CLI_REFERENCE = "docs/referencia/cli.md"
MCP_REFERENCE = "docs/referencia/mcp.md"
TUTORIAL = "docs/tutorial/primeiro-uso.md"
CHANGELOG = "CHANGELOG.md"

# As seções do guia: a palavra do título → os fragmentos que o corpo precisa ter.
GUIDE_SECTIONS: dict[str, tuple[str, ...]] = {
    "orige": ("cq banks upload", "--git", "pasta"),
    "nome": ("bank:", "3 a 40"),
    "versão": ("sha-256", "conteúdo"),
    "limite": ("20000", "100 mib", "128 mib"),
    "perfil remoto": ("túnel",),
    "guardad": ("imutável", "enquanto o sistema existir"),
}
# Os avisos do guia, em qualquer lugar do texto: o LFS (o ponteiro vai no lugar do arquivo), a caixa e o NFC/NFD.
GUIDE_WARNINGS = (("lfs", "ponteiro"), ("caixa",), ("nfc", "nfd"))

NEW_REASONS = ("too_large", "path_not_allowed", "entry_not_allowed", "not_a_course", "mismatch")
REFERENCE_NEEDS: dict[str, tuple[str, ...]] = {
    API_REFERENCE: ("banks.upload", "banks.list", "post /v1/question-banks", "get /v1/question-banks", *NEW_REASONS),
    CLI_REFERENCE: ("cq banks upload", "cq banks list", "--git", "--name"),
    MCP_REFERENCE: ("upload_question_bank", "list_question_banks", "git_rev"),
}

# Os títulos `##` do tutorial, que não mudam (a nota entra dentro da seção do disparo).
TUTORIAL_TITLES = [
    "## Antes de começar", "## 1. Obter o código e instalar o `cq`", "## 2. Criar o arquivo de ambiente",
    "## 3. Criar o perfil do `cq`", "## 4. Subir a pilha e conferir", "## 5. Criar o oferecimento de teste",
    "## 6. Criar o bot no Telegram e ligá-lo ao oferecimento", "## 7. Importar a turma de um aluno só",
    "## 8. Enviar o convite e entrar pelo link", "## 9. Disparar uma questão", "## 10. Responder pelo Telegram",
    "## 11. Ver o relatório", "## 12. Limpar tudo", "## Quando algo dá errado", "## Próximos passos",
]
TUTORIAL_DISPATCH = "## 9. Disparar uma questão"
# O preparo do tutorial continua pelo repositório HTTPS (o de demonstração).
TUTORIAL_HTTPS_PREPARE = "cq dispatches prepare tutorial --repo <endereço https do repositório>"
# Os sinais do banco local que não podem aparecer fora da seção do disparo do tutorial.
BANK_SIGNS = ("banco-local.md", "cq banks", "bank:", "upload_question_bank")

CHANGELOG_NEEDS = ("cq banks upload", "cq banks list", "upload_question_bank", "list_question_banks")
# As seções do registro de mudanças onde a entrada vale: o `[Unreleased]` e as seções de versão, no formato que o
# guia de publicação confere (`## [X.Y.Z] - ` e a data em ano-mês-dia).
CHANGELOG_SECTION = re.compile(r"## \[Unreleased\]|## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}")
CHANGELOG_ADDED = "### Adicionado"

_THOUSANDS = re.compile(r"(?<=\d)[.   ](?=\d{3}(?!\d))")


@pytest.fixture(scope="session")
def public_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A árvore julgada (uma exportação por processo, no repositório de origem)."""
    return exported_root(tmp_path_factory)


def _public_text(root: Path, rel: str) -> str:
    path = root.joinpath(*rel.split("/"))
    assert path.is_file(), f"arquivo ausente na árvore pública: {rel}"
    return path.read_text(encoding="utf-8")


def _plain(text: str) -> str:
    """O texto normalizado (sem `**`, sem crases, espaços colapsados, minúsculas), sem o separador de milhar."""
    return _THOUSANDS.sub("", _norm(text))


def _section_with(text: str, word: str) -> tuple[str, str]:
    """O primeiro título `##` que contém `word` (sem diferenciar maiúsculas) e o corpo dele."""
    for title, body in _sections(text).items():
        if word in title.lower():
            return title, body
    raise AssertionError(f"seção ausente: nenhum título ## com {word!r}")


def _missing(text: str, needles: tuple[str, ...]) -> list[str]:
    plain = _plain(text)
    return [needle for needle in needles if needle not in plain]


# --------------------------------------------------------------------------------------------------
# O guia novo
# --------------------------------------------------------------------------------------------------


def test_bank_guide_has_required_sections_warnings_and_ready_line() -> None:
    """O guia `docs/operacao/banco-local.md`: as seções das duas origens (a pasta e o `--git`, pelo `cq banks
    upload`), do nome (de 3 a 40, e a referência `bank:`), da versão (o SHA-256 do conteúdo), dos limites (20000
    arquivos, 100 MiB de conteúdo e 128 MiB de pacote), do perfil remoto (pelo túnel) e do que fica guardado (o
    banco imutável, enquanto o sistema existir); os avisos de LFS (o ponteiro), de caixa e de NFC/NFD; a linha
    pronta `cq dispatches prepare ... --repo bank:<nome> --commit <versão>`; as regras comuns dos documentos de
    operação; nenhum código interno."""
    text = _doc(GUIDE)
    assert text.startswith("# "), "o guia não começa por um título #"
    problems: dict[str, list[str]] = {}
    for word, needles in GUIDE_SECTIONS.items():
        title, body = _section_with(text, word)
        missing = _missing(body, needles)
        if missing:
            problems[title] = missing
    assert problems == {}
    for warning in GUIDE_WARNINGS:
        assert _missing(text, warning) == [], f"aviso ausente no guia: {warning}"
    ready = [code for code in _code_texts(text)
             if "cq dispatches prepare" in code and "--repo bank:" in code and "--commit" in code]
    assert ready, "o guia não tem a linha pronta do preparo com a referência bank:"
    _check_common(GUIDE)
    assert internal_codes.find_in_text(GUIDE, text) == []


# --------------------------------------------------------------------------------------------------
# As referências, a skill e a política
# --------------------------------------------------------------------------------------------------


def test_references_cite_bank_operations_commands_tools_and_reasons(public_root: Path) -> None:
    """As referências pela árvore exportada: a da API cita `banks.upload` e `banks.list`, o `POST` e o `GET` de
    `/v1/question-banks` e os 5 motivos novos de `fields[].reason`; a do CLI, `cq banks upload` e `cq banks list`,
    com o `--name` e o `--git`; a do MCP, `upload_question_bank`, `list_question_banks` e o `git_rev`."""
    problems = {rel: _missing(_public_text(public_root, rel), needles) for rel, needles in REFERENCE_NEEDS.items()}
    assert {rel: missing for rel, missing in problems.items() if missing} == {}


def test_dispatch_skill_uploads_bank_before_prepare() -> None:
    """A skill de disparo do plugin: no `## Procedimento`, o envio do banco local (`upload_question_bank`) aparece
    antes do preparo (`prepare_dispatch`), e o trecho do envio diz que o preparo usa a referência `bank:<nome>` com
    a versão devolvida."""
    text = _doc(DISPATCH_SKILL)
    procedure = _sections(text).get("## Procedimento")
    assert procedure is not None, "seção ausente: ## Procedimento"
    upload = procedure.find("`upload_question_bank`")
    prepare = procedure.find("`prepare_dispatch`")
    assert prepare >= 0, "o procedimento não cita o prepare_dispatch"
    assert upload >= 0, "o procedimento não cita o envio do banco local (upload_question_bank)"
    assert upload < prepare, "o envio do banco local vem depois do preparo"
    assert _has_paragraph(procedure, "upload_question_bank", "bank:"), (
        "o trecho do envio não cita a referência bank:")


def test_retention_policy_says_uploaded_bank_stays_stored() -> None:
    """A política de retenção: os mesmos títulos, na ordem (o texto novo fica dentro das seções existentes), e um
    parágrafo que diz que o banco enviado fica guardado."""
    text = _doc(RETENTION_POLICY)
    assert _titles(text) == RETENTION_TITLES
    assert _has_paragraph(text, "banco", "enviado", "fica guardado"), (
        "a política não diz que o banco enviado fica guardado")


# --------------------------------------------------------------------------------------------------
# O tutorial e o registro de mudanças
# --------------------------------------------------------------------------------------------------


def test_tutorial_has_only_a_note_in_dispatch_section(public_root: Path) -> None:
    """O tutorial pela árvore exportada: os mesmos títulos `##`; a seção do disparo continua com o preparo pelo
    repositório HTTPS e ganha uma nota com o link para o guia do banco local (`../operacao/banco-local.md`); fora
    dela, nenhuma menção ao banco local."""
    text = _public_text(public_root, TUTORIAL)
    assert _titles(text) == TUTORIAL_TITLES
    sections = _sections(text)
    dispatch = sections[TUTORIAL_DISPATCH]
    assert TUTORIAL_HTTPS_PREPARE in _norm(dispatch), "o preparo do tutorial deixou o repositório HTTPS"
    outside = {title: [sign for sign in BANK_SIGNS if sign in body.lower()]
               for title, body in sections.items() if title != TUTORIAL_DISPATCH}
    assert {title: signs for title, signs in outside.items() if signs} == {}
    targets = {posixpath.normpath(posixpath.join(posixpath.dirname(TUTORIAL), target.split("#", 1)[0]))
               for target in markdown_links(dispatch)}
    assert GUIDE in targets, "a seção do disparo do tutorial não aponta o guia do banco local"


def test_changelog_has_unreleased_entry_for_local_bank(public_root: Path) -> None:
    """O `CHANGELOG.md` pela árvore exportada: a seção `## [Unreleased]` existe e, na categoria `### Adicionado`
    dela ou de uma seção de versão (`## [X.Y.Z] - <data>`), uma entrada cita os comandos `cq banks upload` e
    `cq banks list` e as ferramentas `upload_question_bank` e `list_question_banks`. A entrada nasce no
    `[Unreleased]` e, na publicação da versão, passa à seção dela, com o `[Unreleased]` vazio acima; os quatro
    nomes têm de estar na mesma categoria, de uma seção só."""
    text = _public_text(public_root, CHANGELOG)
    lines = text.splitlines()
    assert "## [Unreleased]" in lines, "seção ausente: ## [Unreleased]"
    missing_by_section: dict[str, list[str]] = {}
    for index, title in enumerate(lines):
        if not CHANGELOG_SECTION.fullmatch(title):
            continue
        end = next((i for i in range(index + 1, len(lines)) if lines[i].startswith("## [")), len(lines))
        body = lines[index + 1:end]
        if CHANGELOG_ADDED not in body:
            continue
        start = body.index(CHANGELOG_ADDED) + 1
        stop = next((i for i in range(start, len(body)) if body[i].startswith("### ")), len(body))
        missing_by_section[title] = _missing("\n".join(body[start:stop]), CHANGELOG_NEEDS)
    assert missing_by_section, "nenhuma seção [Unreleased] ou de versão com a categoria Adicionado"
    assert any(missing == [] for missing in missing_by_section.values()), (
        f"nenhuma categoria Adicionado cita os quatro nomes; o que falta em cada seção: {missing_by_section}")
