"""Teste de contrato dos arquivos públicos: julga a árvore que sai na exportação.

A árvore julgada é a fixture de sessão `public_root` (`tests/helpers/public_tree.py`): na árvore exportada, a própria
raiz; no repositório de origem, a exportação do último commit num destino temporário, exigindo o código 0. Toda
leitura passa pelo `public_root`, salvo a do documento privado no teste dos princípios, que só existe na origem.

Uma função por regra. O docstring de cada uma diz, em palavras, o vermelho esperado e até quando. Quatro delas são
verdes desde já: os controles dos auxiliares, a exportação limpa, a ausência de alteração pendente (com a árvore
commitada) e a necessidade de cada entrada da lista de códigos aceitos.

As três funções dos códigos internos mostram só o caminho, a linha e o tipo de cada ocorrência, nunca o texto. Os
códigos dos controles positivos são montados em tempo de execução, para este arquivo não conter nenhum.

As regras comuns dos guias de operação vêm dos auxiliares de `tests/contract/test_ops_docs.py`, importados sem
mudar aquele arquivo; a conferência de caminhos citados é reescrita aqui contra o `public_root`.

**[I]** Escolhas deste teste:
- o README: as seções são títulos `##`, reconhecidos por palavras-chave nesta ordem (o resumo em inglês, o que
  é, para quem, o que faz, o estado, como começar, a documentação, como contribuir, a segurança, a licença, como
  citar, os agradecimentos e o mantenedor); outras seções podem aparecer entre elas; os três badges (o CI, a
  cobertura e a licença) ficam entre o título do resumo e o de "o que é"; "o que faz" traz uma figura `.svg` de
  `docs/diagramas/`; o estado cita a versão 0.1.0 e a Unicamp; a licença cita a AGPL-3.0-or-later e o PrairieLearn; a
  frase obrigatória é conferida por fragmentos num mesmo parágrafo ("cobertura" e "mede"); nenhum dos termos do
  OpenSSF (`OPENSSF_TERMS`) aparece no README, sem diferenciar maiúsculas, nem nas URLs (reaberto em 2026-11-16: o
  projeto deixou de usar o Scorecard e o selo Best Practices, e os dois badges e a frase do Scorecard
  parcial saíram);
- a referência: cada `id` do catálogo, cada `cq <cli>`, cada `name` das ferramentas, cada `cq <folha local>` e cada
  elemento de resposta aparecem no texto de `docs/referencia/*.md`, sem diferenciar maiúsculas, sem as marcas `**` e
  as crases;
- os textos novos: todo `.md` rastreado da árvore, menos os do motor (`engine/`) e os guias de operação que já
  existiam; a lista fixa `NEW_TEXTS` tem de estar entre eles;
- os princípios: os títulos `###` da seção "Princípios fundamentais" do documento privado, sem o número romano e sem o
  parêntese final, e o título "Restrições técnicas", procurados no documento público sem diferenciar maiúsculas;
- um arquivo Python que não se deixa ler pela árvore sintática conta como problema no teste dos nomes.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree

import pytest

from tests.contract.test_ops_docs import (
    FORBIDDEN_COMMANDS,
    _cited_cq_commands,
    _cli_leaves,
    _has_paragraph,
    _known_variables,
)
from tests.helpers import git_fixture, internal_codes
from tests.helpers.engine import ELEMENT_OF
from tests.helpers.export_tree import EXPORT_MARKER, is_exported_tree
from tests.helpers.ops import REPO, load_ops
from tests.helpers.public_tree import (
    PRIVATE_PATHS,
    badge_urls,
    broken_links,
    contact_emails,
    exported_root,
    markdown_links,
    pending_exported_changes,
    repository_slugs,
    tracked,
)

# O documento privado dos princípios só existe na origem (lida pelo teste dos princípios; operandos constantes para a varredura
# estática do teste das listas a ver).
CONSTITUTION = REPO / ".specify/memory/constitution.md"

PENDING_MESSAGE = "há alterações não commitadas em arquivos exportados; o teste julga o último commit"

# Os arquivos públicos obrigatórios: a raiz, `.github/`, o índice e as quatro partes da documentação, os dois guias
# novos de operação e o guia do plugin.
REQUIRED_FILES: tuple[str, ...] = (
    "README.md", "LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md",
    "AGENTS.md", "CLAUDE.md", "ROADMAP.md", "CHANGELOG.md", "CITATION.cff",
    ".github/ISSUE_TEMPLATE/bug.yml", ".github/ISSUE_TEMPLATE/melhoria.yml", ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md", ".github/dependabot.yml",
    "docs/README.md",
    "docs/tutorial/primeiro-uso.md",
    "docs/referencia/README.md", "docs/referencia/cli.md", "docs/referencia/api.md", "docs/referencia/mcp.md",
    "docs/referencia/tipos-de-questao.md",
    "docs/explicacao/README.md", "docs/explicacao/arquitetura.md", "docs/explicacao/metodo.md",
    "docs/explicacao/principios.md", "docs/explicacao/decisoes.md",
    "docs/operacao/observabilidade.md", "docs/operacao/publicacao-de-versoes.md", "docs/operacao/plugin-e-agente.md",
)

# Os textos novos em Markdown (a lista fixa, para o teste das regras comuns não passar vazio).
NEW_TEXTS: tuple[str, ...] = tuple(path for path in REQUIRED_FILES if path.endswith(".md"))

# Os guias de operação que já existiam (escritos para o operador do piloto, com os testes deles).
OLD_OPERATION_GUIDES = frozenset({
    "docs/operacao/backup-e-restauracao.md", "docs/operacao/ci-e-exportacao.md", "docs/operacao/implantacao-vm.md",
    "docs/operacao/inicio-rapido.md", "docs/operacao/politica-de-retencao.md", "docs/operacao/piloto/diario.md",
    "docs/operacao/piloto/plano-de-disparos.md", "docs/operacao/piloto/questionario.md",
    "docs/operacao/piloto/roteiro-ensaio.md",
})

# As seções do README, na ordem: (nome, padrão do título, sem diferenciar maiúsculas).
README_SECTIONS: tuple[tuple[str, str], ...] = (
    ("o resumo em inglês", r"\bsummary\b|\bin english\b"),
    ("o que é", r"\bo que é\b"),
    ("para quem", r"\bpara quem\b"),
    ("o que faz", r"\bo que faz\b"),
    ("o estado", r"\bestado\b"),
    ("como começar", r"\bcomo começar\b|\bprimeiros passos\b"),
    ("a documentação", r"\bdocumentação\b"),
    ("como contribuir", r"\bcontribuir\b|\bcontribuição\b|\bcontribuições\b"),
    ("a segurança", r"\bsegurança\b"),
    ("a licença", r"\blicença\b"),
    ("como citar", r"\bcitar\b|\bcitação\b"),
    ("os agradecimentos", r"\bagradecimentos\b"),
    ("o mantenedor", r"\bmantenedor"),
)

# Os três badges: (nome, padrão da URL da imagem).
README_BADGES: tuple[tuple[str, str], ...] = (
    ("CI", r"/actions/workflows/[^/\s]+/badge\.svg|img\.shields\.io/github/actions/workflow/status/"),
    ("cobertura", r"codecov\.io/gh/"),
    ("licença", r"img\.shields\.io/(?:badge/licen[cs]e|github/license/)"),
)

# Os termos do OpenSSF que não aparecem no README, em minúsculas: o nome da fundação, o Scorecard (o badge
# em `api.scorecard.dev` e a frase da nota parcial) e o selo Best Practices (o badge em `bestpractices.dev`).
OPENSSF_TERMS: tuple[str, ...] = ("openssf", "scorecard", "bestpractices", "best practices")

# As folhas locais do `cq` (não estão no catálogo).
LOCAL_LEAVES: tuple[str, ...] = ("up", "down", "logs", "status", "mcp")

CATALOG_PATH = "contracts/api/v1/catalog.json"
TOOLS_PATH = "contracts/mcp/v1/tools.json"
PUBLIC_CONTACTS = "ops/export/public-contacts.txt"
PRINCIPLES_DOC = "docs/explicacao/principios.md"

_FENCE = re.compile(r"^\s*(```|~~~)")
_CITED_PATH = re.compile(r"(?<![\w./-])(?:ops|scripts)/[\w./-]*[\w]")
_ROMAN_TITLE = re.compile(r"^###\s+[IVXLC]+\.\s+(.+?)\s*$")

_SCANS: dict[str, tuple[list, list, list[str]]] = {}


@pytest.fixture(scope="session")
def public_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A árvore julgada (uma exportação por processo, no repositório de origem)."""
    return exported_root(tmp_path_factory)


# --------------------------------------------------------------------------------------------------
# Auxiliares deste módulo
# --------------------------------------------------------------------------------------------------


def _text(root: Path, rel: str) -> str:
    path = root.joinpath(*rel.split("/"))
    assert path.is_file(), f"arquivo ausente na árvore pública: {rel}"
    return path.read_text(encoding="utf-8")


def _norm(text: str) -> str:
    return " ".join(text.replace("**", "").replace("`", "").split()).lower()


def _sections(text: str) -> list[tuple[str, str]]:
    """Os títulos `##` (fora dos blocos cercados) com o corpo de cada um, até o próximo `##`."""
    sections: list[tuple[str, list[str]]] = []
    inside = False
    for line in text.splitlines():
        if _FENCE.match(line):
            inside = not inside
        if not inside and line.startswith("## "):
            sections.append((line[3:].strip(), []))
            continue
        if sections:
            sections[-1][1].append(line)
    return [(title, "\n".join(body)) for title, body in sections]


def _accepted(root: Path, *, required: bool = False) -> list:
    """As entradas da lista de códigos aceitos; ausente, vale como vazia, salvo no teste da necessidade."""
    path = root.joinpath(*internal_codes.ACCEPTED_CODES_PATH.split("/"))
    if not path.is_file():
        assert not required, "lista de códigos aceitos ausente na árvore pública"
        return []
    return internal_codes.load_accepted(path.read_bytes().decode("utf-8"))


def _scan(root: Path) -> tuple[list, list, list[str]]:
    """As ocorrências de conteúdo, as de caminho e nome, e os arquivos Python ilegíveis (uma vez por árvore)."""
    key = str(root)
    if key not in _SCANS:
        content: list = []
        names: list = []
        unreadable: list[str] = []
        for rel in tracked(root):
            names.extend(internal_codes.find_in_path(rel))
            data = root.joinpath(*rel.split("/")).read_bytes()
            if internal_codes.is_binary(data):
                continue
            text = data.decode("utf-8")
            content.extend(internal_codes.find_in_text(rel, text))
            if rel.endswith((".py", ".ts")):
                try:
                    names.extend(internal_codes.find_in_names(rel, text))
                except SyntaxError:
                    unreadable.append(rel)
        _SCANS[key] = (content, names, unreadable)
    return _SCANS[key]


def _report(occurrences: Iterable) -> list[str]:
    """Só o caminho, a linha e o tipo, nunca o texto."""
    return sorted(f"{found.path}:{found.line}: {found.kind}" for found in occurrences)


def _assert_none(lines: list[str], what: str) -> None:
    shown = "\n".join(lines[:200])
    assert not lines, f"{len(lines)} {what}:\n{shown}"


# --------------------------------------------------------------------------------------------------
# Verdes desde já
# --------------------------------------------------------------------------------------------------


def test_controls_detect_planted_codes_links_and_owners(tmp_path: Path) -> None:
    """Verde desde já: os controles positivos dos auxiliares numa amostra montada em tempo de execução. O detector
    acha um código, um caminho privado e um código de fatia num nome, e a lista de códigos aceitos tira a ocorrência
    e acusa a entrada sem uso; os links relativos são lidos e os quebrados, achados; o dono do repositório sai dos
    badges e do CITATION; os e-mails saem dos arquivos de contato; a alteração pendente só conta num arquivo que a
    exportação leva."""
    # O detector e a lista de códigos aceitos.
    code = "P-" + "123"
    occurrences = internal_codes.find_in_text("docs/guia.md", f"veja {code} aqui\n")
    assert [(found.line, found.kind, found.text) for found in occurrences] == [(1, "code", code)]
    private = "spec" + "s/"
    assert [found.kind for found in internal_codes.find_in_text("README.md", f"leia {private}x.md\n")] == \
        ["private_path"]
    slice_file = "tests/test_" + "r" + "9" + "_amostra.py"
    assert [found.kind for found in internal_codes.find_in_path(slice_file)] == ["name"]
    entries = internal_codes.load_accepted(f"docs/guia.md\t{code}\tmotivo de teste\n"
                                           f"docs/outro.md\t{code}\toutro motivo de teste\n")
    remaining, unused = internal_codes.apply_accepted(occurrences, entries)
    assert remaining == []
    assert [entry.path for entry in unused] == ["docs/outro.md"]

    # Os links.
    sample = "\n".join([
        "[a](guia.md) e [b](guia.md#secao)", "![c](img/figura.svg)", "[d](https://example.org/x)", "[e](#so-ancora)",
        "[f](mailto:alguem@example.org)", "`[g](em-crase.md)`", "```", "[h](no-bloco.md)", "```",
        "[i](<com espaco.md>)", "[j](faltando.md)", "[k](pasta)", "[l](../../fora.md)", "[m](Guia.md)",
        "[![n](img/figura.svg)](guia.md \"título\")", "[o](/docs/guia.md)",
    ]) + "\n"
    assert markdown_links(sample) == ["guia.md", "guia.md#secao", "img/figura.svg", "com espaco.md", "faltando.md",
                                      "pasta", "../../fora.md", "Guia.md", "img/figura.svg", "guia.md",
                                      "/docs/guia.md"]
    links_repo = git_fixture.make_repo(tmp_path, {
        "docs/README.md": sample.encode("utf-8"),
        "docs/guia.md": b"# Guia\n",
        "docs/com espaco.md": b"# Com espaco\n",
        "docs/img/figura.svg": b"<svg xmlns=\"http://www.w3.org/2000/svg\"/>\n",
        "docs/pasta/README.md": b"# Pasta\n",
    }, name="links")
    assert broken_links(links_repo) == [("docs/README.md", "faltando.md"), ("docs/README.md", "../../fora.md"),
                                        ("docs/README.md", "Guia.md")]

    # O dono do repositório (os badges e o CITATION; o link do texto a outro repositório não conta).
    owner, name = "dono-de-teste", "repositorio-de-teste"
    readme = "\n".join([
        "# Amostra", "", "## Summary", "",
        f"[![CI](https://github.com/{owner}/{name}/actions/workflows/ci.yml/badge.svg)]"
        f"(https://github.com/{owner}/{name}/actions/workflows/ci.yml)",
        f"![cobertura](https://codecov.io/gh/{owner}/{name}/graph/badge.svg)",
        f"![Scorecard](https://api.scorecard.dev/projects/github.com/{owner}/{name}/badge)",
        f"![licença](https://img.shields.io/github/license/{owner}/{name})",
        "", "Veja [o PrairieLearn](https://github.com/PrairieLearn/PrairieLearn).",
    ]) + "\n"
    owner_tree = tmp_path / "dono"
    owner_tree.mkdir()
    (owner_tree / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    (owner_tree / "CITATION.cff").write_text(f"cff-version: 1.2.0\nrepository-code: \"https://github.com/{owner}/"
                                             f"{name}.git\"\n", encoding="utf-8", newline="\n")
    assert len(badge_urls(readme)) == 5
    assert repository_slugs(owner_tree) == {(owner, name)}
    (owner_tree / "README.md").write_text(
        readme + f"![CI](https://img.shields.io/github/actions/workflow/status/outro-dono/{name}/ci.yml)\n",
        encoding="utf-8", newline="\n")
    assert repository_slugs(owner_tree) == {(owner, name), ("outro-dono", name)}

    # Os e-mails dos arquivos de contato.
    reserved = "canal-questoes@example.org"
    real = "pessoa" + "@" + "exemplo-real.com.br"
    contacts_tree = tmp_path / "contatos"
    contacts_tree.mkdir()
    (contacts_tree / "README.md").write_text("# Sem contato\n", encoding="utf-8", newline="\n")
    (contacts_tree / "SECURITY.md").write_text(f"Escreva para {reserved} ou {real}.\n", encoding="utf-8",
                                               newline="\n")
    assert contact_emails(contacts_tree) == {"README.md": [], "SECURITY.md": [reserved, real]}

    # A alteração pendente: só num arquivo que a lista positiva leva ou na pasta da sobreposição.
    pending_repo = git_fixture.make_repo(tmp_path, {
        "ops/export/allowlist.txt": b"# lista de teste\nsrc/**\n",
        "ops/export/publico/AGENTS.md": b"# Agentes\n",
        "src/a.py": b"x = 1\n",
        "privado/b.txt": b"b\n",
    }, name="pendente")
    assert pending_exported_changes(pending_repo) is False
    git_fixture.modify(pending_repo, "privado/b.txt", b"c\n")
    git_fixture.add_untracked(pending_repo, "privado/c.txt", b"c\n")
    assert pending_exported_changes(pending_repo) is False
    git_fixture.add_untracked(pending_repo, "src/novo.py", b"y = 2\n")
    assert pending_exported_changes(pending_repo) is True
    git_fixture.commit_all(pending_repo)
    git_fixture.modify(pending_repo, "ops/export/publico/AGENTS.md", b"# Outro\n")
    assert pending_exported_changes(pending_repo) is True
    git_fixture.commit_all(pending_repo)
    (pending_repo / "src" / "a.py").unlink()
    assert pending_exported_changes(pending_repo) is True


def test_export_of_head_exits_clean(public_root: Path) -> None:
    """Verde desde já: a exportação do último commit sai com o código 0 (a fixture o exige; um achado da varredura
    é o vermelho), e a árvore tem arquivos rastreados, com a lista de instáveis derivada."""
    files = tracked(public_root)
    assert files, "a árvore pública não tem arquivos rastreados"
    assert EXPORT_MARKER in files, "a árvore pública sem a lista de instáveis derivada"


def test_no_pending_changes_in_exported_files() -> None:
    """Verde com a árvore commitada: no repositório de origem, nenhum arquivo que a exportação leva tem alteração
    não commitada. Na árvore exportada, o teste julga o próprio disco, e a regra não se aplica."""
    if not is_exported_tree():
        assert not pending_exported_changes(REPO), PENDING_MESSAGE


def test_accepted_codes_entries_are_needed(public_root: Path) -> None:
    """Verde desde já: cada entrada da lista de códigos aceitos aponta para um arquivo da árvore pública e casa pelo
    menos uma ocorrência dele (no conteúdo, no caminho ou num nome)."""
    entries = _accepted(public_root, required=True)
    files = set(tracked(public_root))
    content, names, _ = _scan(public_root)
    _, unused = internal_codes.apply_accepted([*content, *names], entries)
    absent = sorted({entry.path for entry in entries if entry.path not in files})
    assert absent == [], f"entradas que apontam para arquivo ausente: {absent}"
    positions = {id(entry): number for number, entry in enumerate(entries, start=1)}
    assert [f"entrada {positions[id(entry)]}: {entry.path}" for entry in unused] == []


# --------------------------------------------------------------------------------------------------
# A sobreposição e os arquivos obrigatórios
# --------------------------------------------------------------------------------------------------


def test_overlay_at_root_and_no_private_variants(public_root: Path) -> None:
    """Vermelho até a exportação pôr a sobreposição pública na raiz: os três arquivos da raiz são os da
    sobreposição, byte a byte, e não as versões privadas; a pasta da sobreposição não sai; nenhum caminho privado
    sai; o CLAUDE.md começa por `@AGENTS.md`; não há `.mcp.json` nem `llms.txt` na raiz."""
    export_public = load_ops("ops/export/export_public.py")
    files = tracked(public_root)
    file_set = set(files)
    assert [name for name in export_public.OVERLAY_FILES if name not in file_set] == [], \
        "arquivo da sobreposição ausente na raiz"
    if not is_exported_tree():
        for name in export_public.OVERLAY_FILES:
            exported = (public_root / name).read_bytes()
            overlay = _head_blob(export_public.OVERLAY_DIR + name)
            assert overlay is not None, f"sobreposição sem {name} no último commit"
            assert exported == overlay, f"{name} da raiz diferente do da sobreposição"
            private = _head_blob(name)
            assert private is None or exported != private, f"{name} da raiz igual à versão privada"
    assert [path for path in files if path.startswith(export_public.OVERLAY_DIR)] == [], \
        "a pasta da sobreposição saiu no próprio caminho"
    assert sorted(path for path in files if path.startswith(PRIVATE_PATHS)) == []
    first = (public_root / "CLAUDE.md").read_text(encoding="utf-8").splitlines()[:1]
    assert first == ["@AGENTS.md"], "o CLAUDE.md não começa por @AGENTS.md"
    assert [name for name in (".mcp.json", "llms.txt") if name in file_set or (public_root / name).exists()] == []


def _head_blob(rel: str) -> bytes | None:
    """O conteúdo de `rel` no último commit da origem, ou `None` se o caminho não está lá."""
    listed = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), "ls-tree", "-z", "HEAD", "--", rel],
                            capture_output=True, timeout=120, check=False)
    assert listed.returncode == 0, "git ls-tree falhou"
    if not listed.stdout:
        return None
    shown = subprocess.run(["git", "--no-optional-locks", "-C", str(REPO), "cat-file", "blob", f"HEAD:{rel}"],
                           capture_output=True, timeout=120, check=False)
    assert shown.returncode == 0, "git cat-file falhou"
    return shown.stdout


def test_required_public_files_exist(public_root: Path) -> None:
    """Vermelho até os textos públicos e a configuração do CI: os arquivos da raiz, os formulários e o template de
    `.github/`, o arquivo do Dependabot, o índice e as quatro partes da documentação, os dois guias novos de
    operação e o guia do plugin estão na árvore pública."""
    file_set = set(tracked(public_root))
    assert [path for path in REQUIRED_FILES if path not in file_set] == []


# --------------------------------------------------------------------------------------------------
# Os textos públicos
# --------------------------------------------------------------------------------------------------


def test_readme_order_badges_and_notes(public_root: Path) -> None:
    """Vermelho até os textos públicos: as seções do README na ordem fixada neste arquivo; os três badges (CI, cobertura
    e licença) depois do resumo em inglês; a frase do que a cobertura mede; nenhum termo do OpenSSF no README inteiro,
    no texto nem nas URLs. Vermelho, depois da reabertura, enquanto o README tiver os badges ou a frase do
    Scorecard e do selo Best Practices."""
    text = _text(public_root, "README.md")
    sections = _sections(text)
    titles = [title.lower() for title, _ in sections]
    found: dict[str, int] = {}
    start = 0
    for name, pattern in README_SECTIONS:
        index = next((i for i in range(start, len(titles)) if re.search(pattern, titles[i])), None)
        if index is None:
            break
        found[name] = index
        start = index + 1
    assert [name for name, _ in README_SECTIONS if name not in found] == [], "seções do README ausentes ou fora de ordem"

    top = "\n".join(body for _, body in sections[found["o resumo em inglês"]:found["o que é"]])
    images = badge_urls(top)
    assert [name for name, pattern in README_BADGES if not any(re.search(pattern, url) for url in images)] == [], \
        "badges ausentes entre o resumo e a primeira seção"

    body = {name: sections[index][1] for name, index in found.items()}
    assert any(re.search(r"docs/diagramas/[^\s)]+\.svg", url) for url in badge_urls(body["o que faz"])), \
        "o que faz: sem o diagrama de contexto"
    assert "0.1.0" in body["o estado"] and "unicamp" in _norm(body["o estado"]), "o estado: sem a versão e o piloto"
    license_text = _norm(body["a licença"])
    assert "agpl-3.0-or-later" in license_text and "prairielearn" in license_text, \
        "a licença: sem a AGPL-3.0-or-later ou sem a nota do PrairieLearn"
    assert _has_paragraph(text, "cobertura", "mede"), "sem a frase do que a cobertura mede"
    readme = _norm(text)
    assert [term for term in OPENSSF_TERMS if term in readme] == [], "o README cita o OpenSSF, o Scorecard ou o selo"


def test_relative_links_resolve(public_root: Path) -> None:
    """Vermelho até os textos públicos: todo link relativo de todo
    Markdown da árvore pública resolve para um arquivo ou um diretório rastreado dela, com caixa."""
    assert broken_links(public_root) == []


def test_contacts_and_single_repository_owner(public_root: Path) -> None:
    """Vermelho até os textos públicos: os e-mails dos arquivos de contato são de domínio reservado ou da lista de
    contatos públicos, com pelo menos um no código de conduta e na política de segurança; cada endereço da lista
    aparece num arquivo de contato; os badges do README e o CITATION usam um só dono e um só repositório."""
    repo_scan = load_ops("ops/export/repo_scan.py")
    emails = contact_emails(public_root)
    contacts_file = public_root.joinpath(*PUBLIC_CONTACTS.split("/"))
    contacts_text = contacts_file.read_bytes().decode("utf-8") if contacts_file.is_file() else ""
    contacts = {address.lower() for address in repo_scan.parse_public_contacts(contacts_text)}

    def reserved(address: str) -> bool:
        domain = address.rsplit("@", 1)[-1].lower()
        return any(domain.endswith(entry) if entry.startswith(".") else domain == entry or
                   domain.endswith("." + entry) for entry in repo_scan.RESERVED_EMAIL_DOMAINS)

    outside = sorted({rel for rel, found in emails.items() for address in found
                      if not reserved(address) and address.lower() not in contacts})
    assert outside == [], f"e-mail fora dos reservados e da lista de contatos em {outside}"
    assert [rel for rel in ("CODE_OF_CONDUCT.md", "SECURITY.md") if not emails.get(rel)] == [], \
        "arquivo de contato sem e-mail"
    cited = {address.lower() for found in emails.values() for address in found}
    assert len(contacts - cited) == 0, "endereço da lista de contatos sem citação nos arquivos de contato"
    slugs = repository_slugs(public_root)
    assert len(slugs) == 1, f"{len(slugs)} pares de dono e repositório nos badges e no CITATION"


def test_common_rules_in_new_texts(public_root: Path) -> None:
    """Vermelho até os textos públicos: nos textos novos, nenhum comando proibido; todo `cq ...` em código começa por
    uma folha da árvore de comandos; toda variável `CQ_*` existe; todo `ops/...` e `scripts/...` citado existe na
    árvore pública."""
    files = tracked(public_root)
    file_set = set(files)
    dirs = {"/".join(path.split("/")[:end]) for path in files for end in range(1, path.count("/") + 1)}
    texts = [path for path in files if path.lower().endswith(".md") and not path.startswith("engine/")
             and path not in OLD_OPERATION_GUIDES]
    problems: dict[str, list[str]] = {}
    missing = [path for path in NEW_TEXTS if path not in texts]
    if missing:
        problems["textos novos ausentes"] = missing
    leaves = _cli_leaves()
    known = _known_variables()
    for rel in texts:
        text = _text(public_root, rel)
        found: list[str] = []
        lowered = text.lower()
        found.extend(f"comando proibido: {command}" for command in FORBIDDEN_COMMANDS if command in lowered)
        cited = {match.rstrip(".,;:)") for match in _CITED_PATH.findall(text)}
        found.extend(f"caminho citado inexistente: {path}" for path in sorted(cited)
                     if path not in file_set and path not in dirs)
        found.extend(f"cq fora da árvore: {' '.join(words)}" for words in _cited_cq_commands(text)
                     if not any(" ".join(words[:n]) in leaves for n in range(1, len(words) + 1)))
        for variable in sorted(set(re.findall(r"CQ_[A-Z0-9_]+", text))):
            if variable.endswith("_"):
                if not any(name.startswith(variable) for name in known):
                    found.append(f"variável desconhecida: {variable}")
            elif variable not in known:
                found.append(f"variável desconhecida: {variable}")
        if found:
            problems[rel] = found
    assert problems == {}


def test_reference_covers_catalog_tools_and_question_types(public_root: Path) -> None:
    """Vermelho até os textos públicos: a referência cita cada operação do catálogo (o identificador e o comando
    `cq`), cada ferramenta do MCP, cada comando local e cada elemento de resposta suportado."""
    catalog = json.loads(_text(public_root, CATALOG_PATH))
    tools = json.loads(_text(public_root, TOOLS_PATH))
    reference = sorted(path for path in tracked(public_root)
                       if path.startswith("docs/referencia/") and path.endswith(".md"))
    assert reference, "referência ausente"
    text = _norm("\n".join(_text(public_root, path) for path in reference))
    needed: list[str] = []
    for operation in catalog["operations"]:
        needed.append(operation["id"])
        if operation.get("cli"):
            needed.append("cq " + operation["cli"])
    needed.extend(tool["name"] for tool in tools["tools"])
    needed.extend("cq " + leaf for leaf in LOCAL_LEAVES)
    needed.extend(sorted(set(ELEMENT_OF.values())))
    assert sorted({item for item in needed if item.lower() not in text}) == []


def test_diagrams_have_rendered_svg(public_root: Path) -> None:
    """Vermelho até os diagramas públicos: cada fonte D2 de `docs/diagramas/` tem o SVG ao lado, e o SVG é XML com a
    raiz `svg`."""
    files = tracked(public_root)
    file_set = set(files)
    sources = [path for path in files if re.fullmatch(r"docs/diagramas/[^/]+\.d2", path)]
    assert sources, "nenhuma fonte de diagrama na árvore pública"
    problems: list[str] = []
    for source in sources:
        svg = source[:-3] + ".svg"
        if svg not in file_set:
            problems.append(f"sem SVG: {source}")
            continue
        try:
            root = ElementTree.fromstring((public_root / svg).read_bytes())
        except ElementTree.ParseError:
            problems.append(f"SVG não é XML: {svg}")
            continue
        if root.tag.rsplit("}", 1)[-1] != "svg":
            problems.append(f"SVG sem a raiz svg: {svg}")
    assert problems == []


def test_public_principles_match_the_private_ones(public_root: Path) -> None:
    """Vermelho até os textos públicos: no repositório de origem, os sete títulos de princípio e o das restrições
    técnicas do documento privado aparecem no documento público dos princípios; na árvore exportada, o privado não
    está lá."""
    rel = CONSTITUTION.relative_to(REPO).as_posix()
    assert rel not in set(tracked(public_root)), "o documento privado dos princípios saiu na árvore pública"
    if is_exported_tree():
        assert not CONSTITUTION.exists(), "o documento privado dos princípios está na árvore exportada"
        return
    constitution = CONSTITUTION.read_text(encoding="utf-8")
    titles: list[str] = []
    section = None
    for line in constitution.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            if section == "Restrições técnicas":
                titles.append(section)
            continue
        match = _ROMAN_TITLE.match(line)
        if section == "Princípios fundamentais" and match:
            titles.append(re.sub(r"\s*\([^)]*\)\s*$", "", match.group(1)))
    assert len(titles) == 8, "controle: o documento privado deveria ter sete princípios e as restrições técnicas"
    public = _norm(_text(public_root, PRINCIPLES_DOC))
    assert [title for title in titles if _norm(title) not in public] == []


# --------------------------------------------------------------------------------------------------
# Os códigos internos
# --------------------------------------------------------------------------------------------------


def test_no_internal_codes_in_test_zone(public_root: Path) -> None:
    """Vermelho até a limpeza dos códigos internos no conteúdo dos testes e do código (alguns motivos dos testes só
    mudam junto com o catálogo): na zona dos testes, nenhuma ocorrência fora da lista de códigos aceitos. A mensagem
    mostra só o caminho, a linha e o tipo."""
    entries = _accepted(public_root)
    content, _, _ = _scan(public_root)
    remaining, _ = internal_codes.apply_accepted(
        [found for found in content if internal_codes.zone_of(found.path) == "tests"], entries)
    _assert_none(_report(remaining), "ocorrência(s) na zona dos testes")


def test_no_internal_codes_in_the_rest(public_root: Path) -> None:
    """Vermelho até a limpeza dos códigos internos no conteúdo do código, dos contratos, dos guias e das
    configurações: fora da zona dos testes, nenhuma ocorrência fora da lista de códigos aceitos. A mensagem mostra
    só o caminho, a linha e o tipo."""
    entries = _accepted(public_root)
    content, _, _ = _scan(public_root)
    remaining, _ = internal_codes.apply_accepted(
        [found for found in content if internal_codes.zone_of(found.path) == "rest"], entries)
    _assert_none(_report(remaining), "ocorrência(s) fora da zona dos testes")


def test_no_slice_codes_in_paths_and_names(public_root: Path) -> None:
    """Vermelho até a limpeza dos nomes de arquivos e de funções: nenhum caminho da árvore pública, nenhum nome
    definido em Python e nenhum título de teste em TypeScript tem código de fatia, fora da lista de códigos aceitos.
    A mensagem mostra só o caminho, a linha e o tipo."""
    entries = _accepted(public_root)
    _, names, unreadable = _scan(public_root)
    remaining, _ = internal_codes.apply_accepted(names, entries)
    _assert_none([*_report(remaining), *(f"{rel}: ilegível" for rel in unreadable)], "nome(s) com código de fatia")
