"""As listas versionadas da exportação e do pacote: o que entra no repositório exportado, o que os testes
precisam, as exceções fictícias e os binários.

Lê a árvore real **só para leitura** (`git ls-files -z`, os arquivos de `ops/export/` e os testes); nunca exporta nem
empacota. Duas partes:

**Higiene (verde no esqueleto):** as listas já existem no esqueleto, então
estas funções passam já no esqueleto:
- `allowlist.txt` sem padrão inválido (pela regra própria do teste, `_pattern_problem`);
- a allowlist não casa `docs/execucao`, `docs/base-de-conhecimento`, `docs/monografia`, `.claude`, `.specify` nem
  `.env` (nenhum arquivo rastreado sob eles e nenhum caminho de prova);
- `test-needs.txt` ⊂ allowlist;
- a varredura estática dos testes (os caminhos montados a partir de `REPO` em `tests/**/*.py`) ⊂ `test-needs.txt`.

**Falha esperada no esqueleto (`NotImplementedError` até a implementação), numa função só:** a `Allowlist` e o `select` do
`repo_scan` iguais ao oráculo do teste; a varredura (`repo_scan.scan`) sobre a árvore exportável real sem achado;
`fictitious.txt` só necessária nas árvores de teste e na documentação de operação; `binary-allowlist.txt` só com
arquivos rastreados e binários; os 6 detectores de segredo sobre **toda** a árvore versionada (inclusive
`docs/execucao`, `docs/base-de-conhecimento`, `docs/monografia`, `specs/`, `.claude` e `.specify`), sem achado fora
de `fictitious.txt` (os de dado pessoal ficam de fora, porque a base de conhecimento cita pessoas reais);
e as demais asserções sobre as listas (nenhum caminho de `specs/` sai na exportação nem é lido pelos testes exportados, os
scripts internos de fora, cada entrada de `test-needs.txt` existente, a carga recusando padrão inválido).

**[I]** Escolhas deste teste:
- o oráculo da allowlist é `fnmatch.fnmatchcase` de cada padrão (o `*` cruza `/`); padrão inválido = vazio,
  espaço nas pontas, começo `/`, `!` ou `./`, barra invertida, segmento vazio, `.` ou `..`, ou `/` no fim; a carga
  (`Allowlist.load`) recusa padrão inválido com `ValueError`;
- uma entrada de `test-needs.txt` que termina em `/` é um diretório lido inteiro: está na allowlist só quando um
  padrão `X/**` o leva inteiro, e cobre tudo o que fica abaixo dele;
- a varredura estática segue, em cada arquivo, as constantes de módulo derivadas de `Path(__file__)` (`.resolve()`,
  `.parent`, `.parents[N]`), as importadas de `tests.*` e as montadas com `/`; registra cada cadeia `base / "a" /
  "b"` com pelo menos um segmento constante (até o primeiro não constante) e cada `load_ops("<caminho>")`; os
  caminhos sob `tests/` ficam de fora (`tests/**` vai inteiro);
- fica de fora da varredura estática, declarado em `EXCLUDED_READS`, o `.claude/settings.json` lido por
  `tests/contract/test_plugin.py` (congelado): a lista positiva nunca leva `.claude/`; no
  repositório exportado, esse teste confere que `.claude/` não está lá, em vez de ler o arquivo;
- "valores que não aparecem fora de `tests/` e `docs/operacao/`" é conferido pela varredura **sem** as exceções
  fictícias sobre a árvore exportável fora das árvores de teste (`tests/`, `engine/test/`) e de `docs/operacao/`:
  nenhum achado (um valor fictício curto, como os dígitos de um RA, pode aparecer como texto em outro lugar sem ser
  um achado); a própria `fictitious.txt` fica de fora dessa conta.

**Exportação pública:**
- as pastas privadas incluem a arquitetura, as especificações das fatias e os experimentos, e nenhum caminho dessas
  pastas é exportável (nem mesmo os caminhos exatos de `test-needs.txt`);
- a lista positiva não leva as versões privadas de `AGENTS.md`, `CLAUDE.md` e `ROADMAP.md` da raiz (a exportação põe
  na raiz as versões públicas da sobreposição);
- a varredura da árvore exportável usa a lista de contatos públicos: a função falha no esqueleto pela carga dessa
  lista (`parse_public_contacts`, `NotImplementedError` no esqueleto) até a implementação;
- higiene, verde desde o esqueleto: as regras acima, a forma da lista de códigos aceitos e da lista de
  contatos públicos e a ausência de código interno nas listas (pelo detector de `tests/helpers/internal_codes.py`).
"""

from __future__ import annotations

import ast
import fnmatch
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

import pytest

from tests.helpers.export_tree import is_exported_tree
from tests.helpers.internal_codes import find_in_text, load_accepted
from tests.helpers.ops import REPO, load_ops

EXPORT_DIR = REPO / "ops" / "export"
ALLOWLIST = EXPORT_DIR / "allowlist.txt"
TEST_NEEDS = EXPORT_DIR / "test-needs.txt"
FICTITIOUS = EXPORT_DIR / "fictitious.txt"
BINARY_ALLOWLIST = EXPORT_DIR / "binary-allowlist.txt"
FICTITIOUS_REL = "ops/export/fictitious.txt"
ACCEPTED_CODES = EXPORT_DIR / "accepted-codes.txt"
PUBLIC_CONTACTS = EXPORT_DIR / "public-contacts.txt"

# o que a allowlist nunca leva.
NEVER_EXPORTED = ("docs/execucao/", "docs/base-de-conhecimento/", "docs/monografia/", "docs/arquitetura/", ".claude/",
                  ".specify/", "specs/", "spikes/")
NEVER_EXPORTED_PROBES = (
    "docs/execucao/estado-loop.md",
    "docs/execucao/instaveis-conhecidos.txt",
    "docs/base-de-conhecimento/registro-de-decisoes.md",
    "docs/monografia/estrutura.md",
    ".claude/settings.json",
    ".specify/memory/constitution.md",
    ".env",
    ".env.local",
    ".env.producao",
)
# os scripts internos do desenvolvimento (revisão e custo) ficam de fora por omissão.
LOOP_SCRIPTS = (
    "scripts/run-codex-review.sh",
    "scripts/run-codex-verify.sh",
    "scripts/run-codex-qualquer.sh",
    "scripts/make-review-prompt.py",
    "scripts/loop-cost.py",
)
# [I] Leituras dos testes que a lista positiva não leva, cada uma com o motivo (ver o docstring).
EXCLUDED_READS: Mapping[str, str] = {
    ".claude/settings.json": "teste do plugin: configuração do agente, nunca exportada",
    ".specify/memory/constitution.md": "teste dos princípios públicos: a constituição, lida só no repositório privado",
}
# Controle positivo da varredura estática: leituras conhecidas dos testes do plugin e da implantação.
KNOWN_READS = ("docs/operacao/plugin-e-agente.md", "compose.yaml", "uv.lock")
# [I] As árvores de teste e a documentação de operação, onde os valores fictícios podem aparecer.
FICTITIOUS_HOMES = ("tests/", "engine/test/", "docs/operacao/")
# os detectores de segredo que varrem a árvore versionada inteira.
SECRET_KINDS = frozenset({"age_secret_key", "private_key_block", "telegram_bot_token", "api_token", "app_password",
                          "env_assignment"})
BINARY_SNIFF_BYTES = 8192


# --------------------------------------------------------------------------------------------------
# Leitura das listas e da árvore (oráculo do teste, sem o `repo_scan`)
# --------------------------------------------------------------------------------------------------


def _raw_lines(path: Path) -> list[str]:
    assert path.is_file(), f"lista ausente: {path.relative_to(REPO).as_posix()}"
    data = path.read_bytes()
    assert b"\r" not in data, f"{path.name}: fim de linha CRLF"
    return data.decode("utf-8").split("\n")


def _entries(path: Path) -> list[str]:
    return [line for line in _raw_lines(path) if line.strip() and not line.startswith("#")]


def _pattern_problem(pattern: str) -> str | None:
    if pattern != pattern.strip():
        return "espaço nas pontas"
    if pattern.startswith(("/", "!", "./")):
        return "começo inválido"
    if "\\" in pattern:
        return "barra invertida"
    if pattern.endswith("/"):
        return "barra no fim"
    if any(part in ("", ".", "..") for part in pattern.split("/")):
        return "segmento vazio, . ou .."
    return None


def _patterns() -> list[str]:
    return _entries(ALLOWLIST)


def _allowed(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _needs() -> tuple[frozenset[str], frozenset[str]]:
    entries = _entries(TEST_NEEDS)
    assert len(entries) == len(set(entries)), "test-needs.txt com entrada repetida"
    dirs = frozenset(entry for entry in entries if entry.endswith("/"))
    return frozenset(entries) - dirs, dirs


def _dir_allowed(directory: str, patterns: Iterable[str]) -> bool:
    return any(pattern.endswith("/**") and directory.startswith(pattern[:-2]) for pattern in patterns)


def _covered(path: str, files: frozenset[str], dirs: frozenset[str]) -> bool:
    return path in files or path + "/" in dirs or any(path.startswith(directory) for directory in dirs)


def _tracked() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, timeout=120, check=False)
    assert listed.returncode == 0, "git ls-files falhou"
    return sorted(name for name in listed.stdout.decode("utf-8").split("\0") if name)


def _is_binary(data: bytes) -> bool:
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


# --------------------------------------------------------------------------------------------------
# Varredura estática dos testes
# --------------------------------------------------------------------------------------------------


Parts = tuple[str, ...]


def _eval_path(node: ast.AST, table: Mapping[str, Parts], here: Parts) -> Parts | None:
    """O caminho (partes relativas à raiz) de uma expressão de caminho, ou `None` se não for derivável."""
    if isinstance(node, ast.Name):
        return table.get(node.id)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("resolve", "absolute") and not node.args:
            return _eval_path(func.value, table, here)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name in ("Path", "PurePath", "PosixPath", "PurePosixPath") and len(node.args) == 1:
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id == "__file__":
                return here
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _eval_path(node.value, table, here)
        return base[:-1] if base else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        base = _eval_path(node.value.value, table, here)
        index = node.slice
        if base is not None and isinstance(index, ast.Constant) and isinstance(index.value, int):
            drop = index.value + 1
            return base[:-drop] if drop <= len(base) else None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _eval_path(node.left, table, here)
        right = node.right
        if base is None or not (isinstance(right, ast.Constant) and isinstance(right.value, str)):
            return None
        return base + tuple(part for part in right.value.split("/") if part)
    return None


class _StaticScan:
    """Os caminhos que os testes montam a partir da raiz do repositório (ver o docstring do módulo)."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, Parts]] = {}

    def table(self, rel: str) -> dict[str, Parts]:
        if rel in self.tables:
            return self.tables[rel]
        self.tables[rel] = {}
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        here = PurePosixPath(rel).parts
        table: dict[str, Parts] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.ImportFrom) and stmt.module and stmt.module.startswith("tests."):
                module = stmt.module.replace(".", "/") + ".py"
                if (REPO / module).is_file():
                    other = self.table(module)
                    for alias in stmt.names:
                        if alias.name in other:
                            table[alias.asname or alias.name] = other[alias.name]
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                if len(targets) == 1 and isinstance(targets[0], ast.Name):
                    value = _eval_path(stmt.value, table, here)
                    if value is not None:
                        table[targets[0].id] = value
        self.tables[rel] = table
        return table

    def reads(self, rel: str) -> set[str]:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        table = self.table(rel)
        here = PurePosixPath(rel).parts
        inner = {id(node.left) for node in ast.walk(tree)
                 if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                 and isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Div)}
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if name == "load_ops" and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    found.add(PurePosixPath(node.args[0].value).as_posix())
                continue
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)) or id(node) in inner:
                continue
            operands: list[ast.AST] = []
            current: ast.AST = node
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                operands.append(current.right)
                current = current.left
            operands.reverse()
            base = _eval_path(current, table, here)
            if base is None:
                continue
            parts, added = base, 0
            for operand in operands:
                if not (isinstance(operand, ast.Constant) and isinstance(operand.value, str)):
                    break
                parts = parts + tuple(part for part in operand.value.split("/") if part)
                added += 1
            if added and parts:
                found.add("/".join(parts))
        return {path for path in found if path.split("/", 1)[0] != "tests"}


def _static_reads() -> dict[str, set[str]]:
    scan = _StaticScan()
    reads: dict[str, set[str]] = {}
    for path in sorted((REPO / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        for read in scan.reads(rel):
            reads.setdefault(read, set()).add(rel)
    return reads


# --------------------------------------------------------------------------------------------------
# Higiene: verdes no esqueleto
# --------------------------------------------------------------------------------------------------


def test_allowlist_has_only_valid_patterns() -> None:
    """`allowlist.txt` sem padrão inválido e sem repetição."""
    patterns = _patterns()
    assert patterns, "allowlist.txt sem padrões"
    problems = {pattern: problem for pattern in patterns if (problem := _pattern_problem(pattern)) is not None}
    assert problems == {}
    assert len(patterns) == len(set(patterns)), "allowlist.txt com padrão repetido"


def test_allowlist_never_takes_private_trees() -> None:
    """a allowlist não casa `docs/execucao`, `docs/base-de-conhecimento`, `docs/monografia`, `.claude`,
    `.specify` nem `.env`: nenhum arquivo rastreado sob eles e nenhum caminho de prova."""
    patterns = _patterns()
    private = [path for path in _tracked() if path.startswith(NEVER_EXPORTED) or path.split("/")[-1] == ".env"]
    # na árvore exportada os diretórios privados não existem por definição, então o controle da origem não
    # vale lá; em seu lugar, a conferência mais forte de que nenhum arquivo rastreado está neles.
    if is_exported_tree():
        assert private == [], "a árvore exportada tem arquivos rastreados nos diretórios privados"
    else:
        assert private, "controle: a árvore real tem arquivos nos diretórios privados"
    assert [path for path in (*private, *NEVER_EXPORTED_PROBES) if _allowed(path, patterns)] == []


def test_test_needs_within_allowlist() -> None:
    """`test-needs.txt` ⊂ allowlist (o diretório, só quando um padrão `X/**` o leva inteiro)."""
    patterns = _patterns()
    files, dirs = _needs()
    assert files and dirs
    assert sorted(path for path in files if not _allowed(path, patterns)) == []
    assert sorted(directory for directory in dirs if not _dir_allowed(directory, patterns)) == []


def test_static_test_reads_within_test_needs() -> None:
    """os caminhos que os testes montam a partir de `REPO` (e os de `load_ops`) ⊂ `test-needs.txt`, salvo as
    leituras declaradas em `EXCLUDED_READS`."""
    reads = _static_reads()
    for known in KNOWN_READS:
        assert known in reads, f"controle: a varredura estática não achou {known}"
    assert set(EXCLUDED_READS) <= set(reads), "EXCLUDED_READS com leitura que nenhum teste faz"
    files, dirs = _needs()
    missing = {path: sorted(sources) for path, sources in reads.items()
               if path not in EXCLUDED_READS and not _covered(path, files, dirs)}
    assert missing == {}


# --------------------------------------------------------------------------------------------------
# Falha esperada no esqueleto: a varredura real e as demais asserções sobre as listas
# --------------------------------------------------------------------------------------------------


def _fictitious(kinds: Iterable[str]) -> dict[str, frozenset[str]]:
    known = set(kinds)
    values: dict[str, set[str]] = {}
    for line in _entries(FICTITIOUS):
        kind, sep, value = line.partition("\t")
        assert sep and kind in known and value and value == value.strip() and "\t" not in value, \
            f"linha de fictitious.txt fora da forma <tipo>TAB<valor>: tipo {kind!r}"
        assert value not in values.get(kind, set()), f"valor repetido em fictitious.txt ({kind})"
        values.setdefault(kind, set()).add(value)
    return {kind: frozenset(found) for kind, found in values.items()}


def _report(findings: Iterable[object]) -> list[str]:
    return sorted(f"{finding.path}:{finding.line}: {finding.kind}" for finding in findings)  # type: ignore[attr-defined]


def test_exportable_tree_scan_and_lists_are_consistent(tmp_path: Path) -> None:
    """A lista carregada e a seleção do `repo_scan` iguais ao oráculo; a árvore
    exportável real sem achado; as exceções fictícias só necessárias nas árvores de teste e na documentação; a
    lista de binários só com rastreados binários; os detectores de segredo sobre toda a árvore versionada sem
    achado fora de `fictitious.txt`; e as demais regras das listas."""
    repo_scan = load_ops("ops/export/repo_scan.py")
    allowlist = repo_scan.Allowlist.load(ALLOWLIST)
    patterns = _patterns()
    tracked = _tracked()

    # A lista carregada e a seleção iguais ao oráculo do teste.
    assert list(allowlist.patterns) == patterns
    for path in (*tracked, *NEVER_EXPORTED_PROBES, *LOOP_SCRIPTS):
        assert allowlist.matches(path) == _allowed(path, patterns), path
    exportable = sorted(repo_scan.select(REPO, allowlist))
    assert exportable == [path for path in tracked if _allowed(path, patterns)]

    # A árvore exportável real sem achado, com as exceções das listas.
    fictitious = _fictitious(repo_scan.FINDING_KINDS)
    binary = frozenset(_entries(BINARY_ALLOWLIST))
    contacts = repo_scan.parse_public_contacts(PUBLIC_CONTACTS.read_text(encoding="utf-8"))
    assert _report(repo_scan.scan(REPO, exportable, fictitious=fictitious, binary_allow=binary,
                                  public_contacts=contacts)) == []

    # As exceções fictícias só são necessárias nas árvores de teste e na documentação de operação.
    outside = [path for path in exportable if not path.startswith(FICTITIOUS_HOMES) and path != FICTITIOUS_REL]
    assert outside
    assert _report(repo_scan.scan(REPO, outside, fictitious=None, binary_allow=binary,
                                  public_contacts=contacts)) == []

    # A lista de binários: só arquivos rastreados, binários e exportáveis.
    assert len(binary) == len(_entries(BINARY_ALLOWLIST)), "binary-allowlist.txt com entrada repetida"
    tracked_set = set(tracked)
    assert sorted(path for path in binary if path not in tracked_set) == []
    assert sorted(path for path in binary if not _is_binary((REPO / path).read_bytes())) == []
    assert sorted(path for path in binary if not _allowed(path, patterns)) == []

    # Os detectores de segredo sobre toda a árvore versionada (só leitura).
    assert {kind for kind, detector in repo_scan.DETECTORS.items() if detector.secret} >= SECRET_KINDS
    whole = repo_scan.scan(REPO, tracked, fictitious=fictitious, binary_allow=frozenset())
    assert _report(finding for finding in whole if finding.kind in SECRET_KINDS) == []

    # As demais regras das listas.
    files, dirs = _needs()
    assert [path for path in exportable if path.startswith("specs/")] == []
    assert [pattern for pattern in patterns if pattern.startswith("specs/")] == []
    assert sorted(path for path in files | dirs if path.startswith("specs/")) == []
    assert [path for path in LOOP_SCRIPTS if _allowed(path, patterns)] == []
    assert sorted(path for path in files if path not in tracked_set) == []
    assert sorted(directory for directory in dirs if not any(path.startswith(directory) for path in tracked)) == []

    # A carga recusa padrão inválido.
    for bad in ("/absoluto/**", "../fora/**", "!negado", "dir\\arquivo"):
        bad_list = tmp_path / "lista-invalida.txt"
        bad_list.write_text(f"# lista de teste\nsrc/**\n{bad}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            repo_scan.Allowlist.load(bad_list)


# --------------------------------------------------------------------------------------------------
# Higiene da exportação pública e das listas de códigos aceitos e de contatos, verdes desde o esqueleto
# --------------------------------------------------------------------------------------------------

# As versões privadas da raiz, que a lista positiva nunca leva (a exportação põe as públicas no lugar).
ROOT_AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "ROADMAP.md")
# Os arquivos públicos de contato, onde cada endereço da lista de contatos tem de aparecer.
CONTACT_FILES = ("README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md", "SUPPORT.md", "CITATION.cff")
MAX_PUBLIC_CONTACTS = 1


def test_allowlist_never_takes_root_agent_files() -> None:
    """Higiene: nenhum padrão da lista positiva casa `AGENTS.md`, `CLAUDE.md` nem `ROADMAP.md` da raiz, nas duas
    árvores (na exportada, eles são as versões públicas, que vêm da sobreposição, não da lista)."""
    patterns = _patterns()
    tracked = set(_tracked())
    assert set(ROOT_AGENT_FILES) <= tracked, "controle: os três arquivos da raiz são rastreados"
    assert [name for name in ROOT_AGENT_FILES if _allowed(name, patterns)] == []


def test_no_specs_path_is_exportable() -> None:
    """Higiene: nenhum padrão da lista positiva nem entrada de `test-needs.txt` fica nas especificações das fatias,
    nenhum padrão casa um caminho delas e nenhum arquivo rastreado delas é exportável."""
    patterns = _patterns()
    files, dirs = _needs()
    prefix = NEVER_EXPORTED[NEVER_EXPORTED.index("specs/")]
    probe = prefix + "001-qualquer/spec.md"
    assert [pattern for pattern in patterns if pattern.startswith(prefix) or fnmatch.fnmatchcase(probe, pattern)] == []
    assert sorted(path for path in files | dirs if path.startswith(prefix)) == []
    assert [path for path in _tracked() if path.startswith(prefix) and _allowed(path, patterns)] == []


def test_lists_have_no_internal_codes() -> None:
    """Higiene: as listas da exportação, com os comentários, sem código interno nem caminho privado no conteúdo,
    pelo detector (na lista de códigos aceitos, só a coluna do texto aceito fica fora); UTF-8, LF e sem linha
    repetida."""
    lists = (ALLOWLIST, TEST_NEEDS, FICTITIOUS, BINARY_ALLOWLIST, ACCEPTED_CODES, PUBLIC_CONTACTS)
    found: list[str] = []
    for path in lists:
        lines = _raw_lines(path)
        entries = [line for line in lines if line.strip() and not line.startswith("#")]
        assert len(entries) == len(set(entries)), f"{path.name}: linha repetida"
        rel = path.relative_to(REPO).as_posix()
        found.extend(f"{occurrence.path}:{occurrence.line}: {occurrence.kind}"
                     for occurrence in find_in_text(rel, "\n".join(lines)))
    assert found == []


def test_accepted_codes_list_shape() -> None:
    """Higiene: `accepted-codes.txt` na forma `<caminho>TAB<texto aceito>TAB<motivo>`, carregável pelo detector, sem
    linha repetida, com pelo menos uma entrada, o texto aceito sem espaço e cada caminho um arquivo existente que a
    lista positiva leva (a necessidade de cada entrada é conferida pelo teste da árvore pública, sobre a exportação)."""
    lines = _raw_lines(ACCEPTED_CODES)
    entries = load_accepted("\n".join(lines))
    assert entries, "accepted-codes.txt sem entradas"
    raw = [line for line in lines if line.strip() and not line.startswith("#")]
    assert len(raw) == len(entries) == len(set(raw))
    patterns = _patterns()
    assert sorted({entry.path for entry in entries if not (REPO / entry.path).is_file()}) == []
    assert sorted({entry.path for entry in entries if not _allowed(entry.path, patterns)}) == []
    assert [entry.path for entry in entries if any(char.isspace() for char in entry.text)] == [], \
        "texto aceito com espaço"


def test_public_contacts_list_shape() -> None:
    """Higiene: `public-contacts.txt` existe, com o cabeçalho comentado; cada linha um endereço ASCII na forma que o
    detector `email` da varredura reconhece, fora dos domínios reservados, sem repetição (sem caixa), no máximo uma
    entrada, e cada endereço presente num arquivo público de contato."""
    repo_scan = load_ops("ops/export/repo_scan.py")
    lines = _raw_lines(PUBLIC_CONTACTS)
    assert any(line.startswith("#") for line in lines), "public-contacts.txt sem o cabeçalho comentado"
    entries = [line for line in lines if line.strip() and not line.startswith("#")]
    assert len(entries) <= MAX_PUBLIC_CONTACTS
    lowered = [entry.lower() for entry in entries]
    assert len(lowered) == len(set(lowered)), "public-contacts.txt com endereço repetido"
    pattern = repo_scan.DETECTORS["email"].pattern
    texts = [(REPO / name).read_text(encoding="utf-8").lower() for name in CONTACT_FILES if (REPO / name).is_file()]
    for entry in entries:
        assert entry.isascii() and pattern.fullmatch(entry) is not None, "endereço fora da forma"
        domain = entry.rpartition("@")[2].lower()
        reserved = any(domain.endswith(item) if item.startswith(".") else domain == item or domain.endswith("." + item)
                       for item in repo_scan.RESERVED_EMAIL_DOMAINS)
        assert not reserved, "endereço de domínio reservado"
        assert any(entry.lower() in text for text in texts), "endereço que não aparece em nenhum arquivo de contato"
