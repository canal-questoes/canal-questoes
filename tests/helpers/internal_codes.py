"""Detector de códigos internos, referências privadas e caminhos privados na árvore pública.

Código de teste, real desde o esqueleto. Quem o usa: o teste de contrato dos arquivos públicos, os testes do próprio
detector e as ferramentas da limpeza. O detector recebe o caminho e o texto, nunca lê o disco sozinho.

Quatro tipos de ocorrência (`OccurrenceKind`):
- `code`: os códigos do processo (decisões, pendências, requisitos, histórias, tarefas, fatias e rodadas), inclusive
  os requisitos e critérios com letra no fim, as revisões numeradas e as revisões por extenso a partir da segunda
  (inclusive a rodada da revisão);
- `private_ref`: as referências a itens de documentos privados sem caminho (a seção de um documento do processo, o
  nome de um desses documentos, um item da pesquisa, uma divergência numerada por letra e os rótulos do loop), e
  também os rótulos de achado de revisão, de análise e de verificação, as marcas dos motivos do vermelho, os rótulos
  das mudanças que não são só acréscimo e das reaberturas, os números de cenário e as clarificações citadas de uma
  especificação, os passos e o jargão do roteiro privado, as tabelas citadas por letra, as notas de história em
  itálico e os artigos citados do documento de princípios privado;
- `private_path`: os caminhos das árvores que não saem na exportação;
- `name`: o código de fatia num segmento de caminho ou num nome definido (Python: `def`, `async def`, `class`, as
  atribuições do módulo e da classe e os nomes importados; TypeScript: os títulos de `describe`, `it`, `test` e
  `suite`, que não contam como conteúdo).

Sem isenção de arquivo: os padrões são montados em tempo de execução a partir de pedaços, para este módulo não conter
nenhum dos textos que acusa e passar pela própria varredura. Na lista de códigos aceitos, só a coluna do texto aceito
fica fora da varredura (`find_in_text` a apaga antes de varrer).

**[I]** Escolhas deste módulo:
- o texto de uma ocorrência de conteúdo é o trecho casado (num caminho privado, sem o caractere que o antecede); o de
  uma ocorrência de nome é o nome ou o segmento inteiro; a linha de uma ocorrência de caminho é 0;
- nos nomes importados entram o módulo (em `from m import x` e em `import m`) e o nome ligado (`x` ou o `as`);
- as atribuições do módulo e da classe incluem as que ficam dentro de `if`, `try`, `with`, `for` e `while` desses
  níveis, nunca as de dentro de uma função;
- os títulos do TypeScript são o primeiro argumento literal (aspas simples, duplas ou crase) de `describe`, `it`,
  `test` e `suite`, com ou sem um prefixo `x.` (como `t.test`) e um modificador (`.only`, `.skip`, `.todo`), só nos
  arquivos da zona dos testes;
- `load_accepted` recusa, além da forma, um caminho com curinga, barra invertida, começo `/` ou segmento `.`/`..`, e
  um par caminho e texto repetido;
- ficam fora dos padrões o vocabulário do método de desenvolvimento, que é publicado (a revisão dupla, a convergência,
  a reabertura dos testes, a fatia, a rodada sem número), e as palavras que o produto usa (as rodadas de uma tarefa,
  mesmo por extenso, a reabertura de uma entrega, o plano de disparos, o contrato do motor, as fatias de espera do
  worker), além dos rótulos que só são dados de teste (os alunos neutros, com ou sem parênteses, as sementes, os
  nomes de mídia, as contas como "N" menos um e as regras do lint); por isso as famílias de palavras largas não
  entram, só as formas que citam um item privado.
"""

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

OccurrenceKind = Literal["code", "private_ref", "private_path", "name"]
Zone = Literal["tests", "rest"]

BINARY_SNIFF_BYTES = 8192

# O caminho da lista de códigos aceitos na árvore (a coluna do texto aceito fica fora da varredura).
ACCEPTED_CODES_PATH = "ops/export/" + "accepted-codes.txt"


@dataclass(frozen=True)
class Occurrence:
    path: str
    line: int
    kind: OccurrenceKind
    text: str


@dataclass(frozen=True)
class AcceptedEntry:
    path: str
    text: str
    reason: str


class ListInvalid(ValueError):
    """A lista de códigos aceitos fora da forma; a mensagem é fixa, sem a linha nem o valor."""

    def __init__(self) -> None:
        super().__init__("lista inválida")


# --------------------------------------------------------------------------------------------------
# Padrões, montados em tempo de execução
# --------------------------------------------------------------------------------------------------

_LEFT = "(?<![A-Za-z0-9_-])"
_RIGHT = "(?![A-Za-z0-9_])"
_DIGITS = "\\d"


def _code_pattern() -> re.Pattern[str]:
    letters = "[" + "D" + "P" + "M" + "S" + "]"
    ordinals = "|".join(("[Ss]egund", "[Tt]erceir", "[Qq]uart", "[Qq]uint", "[Ss]ext", "[Ss][ée]tim", "[Oo]itav",
                         "[Nn]on", "[Dd][ée]cim"))
    review = "(?:ão|ões|ao|oes)"
    alternatives = (
        letters + "-" + _DIGITS + "{1,4}",
        "F" + "R" + "-" + _DIGITS + "{3}" + "[a-z]?",
        "S" + "C" + "-" + _DIGITS + "{3}" + "[a-z]?",
        "U" + "S" + _DIGITS + "+(?:-" + _DIGITS + "+)?",
        "T" + _DIGITS + "{3}",
        "R" + "(?:1" + "0|" + _DIGITS + ")(?:-[A-Z]+-" + _DIGITS + "+)?",
        "[R" + "r]" + "odada" + "s? " + _DIGITS + "+",
        "[R" + "r]" + "evis" + review + " " + _DIGITS + "+",
        "(?:" + ordinals + ")[ao]s? (?:" + "revis" + review + "|rod" + "adas? d[aoe]s? " + "revis" + review + ")",
    )
    return re.compile(_LEFT + "(?:" + "|".join(alternatives) + ")" + _RIGHT)


def _private_ref_patterns() -> tuple[re.Pattern[str], ...]:
    section = "§"
    documents = "|".join(("data-" + "model", "contr" + "acts", "des" + "ign", "dese" + "nho", "rote" + "iro",
                          "pl" + "an", "sp" + "ec", "rese" + "arch"))
    files = "|".join(("ta" + "sks", "pl" + "an", "rese" + "arch", "sp" + "ec", "data-" + "model",
                      "quick" + "start"))
    item = "R" + "-"
    # As letras dos rótulos de achado de revisão e de análise: só as que o processo usou, para "N-1" (uma conta) e
    # um rótulo de aluno com hífen (um dado de teste) não casarem.
    finding = "[" + "B" + "I" + "L" + "]"
    marks = "[" + "m" + "h" + "]" + _DIGITS + "+"
    return (
        re.compile("(?:" + documents + ")(?:\\.md)?\\s?" + section),
        re.compile("\\b(?:" + files + ")\\." + "md\\b"),
        re.compile("\\b" + "rese" + "arch " + item + _DIGITS + "+\\b"),
        re.compile(_LEFT + item + _DIGITS + "{2}(?:\\." + _DIGITS + "+)?" + _RIGHT),
        re.compile("diverg" + "[êe]" + "ncia \\([a-z]\\)"),
        re.compile("\\b" + "P" + "C" + _DIGITS + "\\b"),
        re.compile(_LEFT + "E" + _DIGITS + "-" + _DIGITS + _RIGHT),
        re.compile("\\b" + "grupo" + "s? " + "S" + _DIGITS + "\\b"),
        re.compile(section + "\\s?" + _DIGITS),
        # Os rótulos de achado de revisão, de análise e de verificação: letra, hífen e número, com um prefixo de
        # letra e dígito ou um nome no meio.
        re.compile(_LEFT + finding + "-" + _DIGITS + "{1,2}" + _RIGHT),
        re.compile(_LEFT + "(?!" + "R" + _DIGITS + ")[A-Z]" + _DIGITS + "-(?:[A-Z]{1,2}-)?" + _DIGITS + "{2,3}"
                   + _RIGHT),
        re.compile(_LEFT + "[A-Z]{1,3}-[A-Z]{2,8}-" + _DIGITS + "{2,3}" + _RIGHT),
        re.compile("\\b[Bb]loque" + "ios? [A-Z]" + _DIGITS + "+\\b"),
        # As marcas dos motivos do vermelho.
        re.compile("\\(\\(" + marks + "\\)|\\b(?:[Mm]otiv" + "os?|[Hh]igi" + "ene) \\(?" + marks + "\\b|\\("
                   + marks + ";|\\bpor \\(" + marks + "\\)"),
        # Os rótulos das mudanças que não são só acréscimo e das reaberturas.
        re.compile("\\b[Nn][ãa]o adit" + "ivas? N?" + _DIGITS + "|\\b[Rr]eabert" + "uras? (?:N" + _DIGITS + "|"
                   + _DIGITS + "+[a-z]?\\b)"),
        # Os números de cenário (com a parte decimal, um intervalo ou uma lista) e as clarificações citadas de uma
        # especificação.
        re.compile("(?i:\\bcen[áa]r" + "ios? " + _DIGITS + "+(?:\\." + _DIGITS + "+| (?:a|e) " + _DIGITS
                   + "+\\b|, " + _DIGITS + "))"),
        re.compile("\\b[Cc]larific" + "a[çc][ãa]o (?:Q" + _DIGITS + "|\")"),
        # Os passos e o jargão do roteiro privado, a extensão que só acrescenta e as tabelas por letra.
        re.compile("\\b[Pp]r[ée]-rev" + "is"),
        re.compile("\\bpelo mot" + "ivo certo\\b|\\b[Vv]erdes? por cons" + "tru[çc]|\\b[Pp]ar(?:es)? verm"
                   + "elhos?\\b|\\b[Vv]ermelho adm" + "itido\\b"),
        re.compile("\\b[Ii]ndependent [Tt]e" + "st\\b"),
        re.compile("\\b[Ee]xtens" + "(?:ão|ões) adit" + "ivas?\\b"),
        re.compile("\\b[Tt]abel" + "as? \\([A-Z]\\)"),
        # As notas de história em itálico (com a data ou o evento do processo) e os artigos citados do documento de
        # princípios privado.
        re.compile("\\*\\(At[ée] (?:20" + _DIGITS + "{2}-" + _DIGITS + "{2}-" + _DIGITS + "{2}|[ao] (?:reabert"
                   + "ura|correç|revis|mudança|rod" + "ada))"),
        re.compile("\\b[Cc]onstitui" + "[çc][ãa]o \\(?[IVX]+\\b"),
    )


def _private_path_pattern() -> re.Pattern[str]:
    slash = "/"
    docs = "do" + "cs" + slash + "(?:" + "|".join(("base-de-" + "conhecimento", "exec" + "ucao",
                                                     "mono" + "grafia", "arqui" + "tetura")) + ")" + slash
    trees = (docs, "spe" + "cs" + slash, "\\.spe" + "cify" + slash, "\\.cla" + "ude" + slash,
             "spi" + "kes" + slash)
    return re.compile("(?:^|[^A-Za-z0-9_.-])(" + "|".join(trees) + ")")


def _name_pattern() -> re.Pattern[str]:
    codes = "|".join((
        "r" + "(?:1" + "0|" + _DIGITS + ")",
        "u" + "s" + _DIGITS + "+",
        "t" + _DIGITS + "{3}",
        "f" + "r" + _DIGITS + "{3}",
        "s" + "c" + _DIGITS + "{3}",
        "d" + _DIGITS + "{2,3}",
        "p" + _DIGITS + "{2,3}",
        "s" + _DIGITS + "{2}",
        "m" + _DIGITS + "{2}",
    ))
    return re.compile("(?:^|[_/.-])(?:" + codes + ")(?=[_/.-]|$)", re.IGNORECASE)


def _title_pattern() -> re.Pattern[str]:
    callee = "(?:" + "|".join(("des" + "cribe", "i" + "t", "te" + "st", "su" + "ite")) + ")"
    literal = ("(?:'(?:[^'\\\\\\n]|\\\\.)*'" + "|\"(?:[^\"\\\\\\n]|\\\\.)*\"" + "|`(?:[^`\\\\]|\\\\.)*`)")
    return re.compile("(?<![\\w$.])(?:[A-Za-z_$][\\w$]*\\.)?" + callee + "(?:\\.(?:only|skip|todo))?\\s*\\(\\s*("
                      + literal + ")")


CODE_PATTERN = _code_pattern()
PRIVATE_REF_PATTERNS = _private_ref_patterns()
PRIVATE_PATH_PATTERN = _private_path_pattern()
NAME_PATTERN = _name_pattern()
TITLE_PATTERN = _title_pattern()


# --------------------------------------------------------------------------------------------------
# Zonas e binários
# --------------------------------------------------------------------------------------------------


def zone_of(path: str) -> Zone:
    """`tests` para `tests/`, `engine/test/` e os `.ts` de `engine/conformance/`; `rest` para o resto."""
    if path.startswith(("tests/", "engine/test/")):
        return "tests"
    if path.startswith("engine/conformance/") and path.endswith(".ts"):
        return "tests"
    return "rest"


def is_binary(data: bytes) -> bool:
    """A regra da varredura da exportação: NUL nos primeiros 8 KiB ou UTF-8 inválido."""
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


# --------------------------------------------------------------------------------------------------
# Conteúdo
# --------------------------------------------------------------------------------------------------


def _blank(text: str, start: int, end: int) -> str:
    """O texto com o trecho `[start, end)` trocado por espaços, mantendo as quebras de linha."""
    middle = "".join(char if char == "\n" else " " for char in text[start:end])
    return text[:start] + middle + text[end:]


def _titles(path: str, text: str) -> list[tuple[int, int]]:
    """Os trechos (início, fim) dos títulos de teste de um `.ts` da zona dos testes."""
    if not path.endswith(".ts") or zone_of(path) != "tests":
        return []
    return [match.span(1) for match in TITLE_PATTERN.finditer(text)]


def _without_accepted_column(text: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 2:
            fields[1] = " " * len(fields[1])
            lines[index] = "\t".join(fields)
    return "\n".join(lines)


def _line_occurrences(path: str, number: int, line: str) -> list[Occurrence]:
    found: list[tuple[int, Occurrence]] = []
    for match in CODE_PATTERN.finditer(line):
        found.append((match.start(), Occurrence(path, number, "code", match.group(0))))
    for pattern in PRIVATE_REF_PATTERNS:
        for match in pattern.finditer(line):
            found.append((match.start(), Occurrence(path, number, "private_ref", match.group(0))))
    for match in PRIVATE_PATH_PATTERN.finditer(line):
        found.append((match.start(1), Occurrence(path, number, "private_path", match.group(1))))
    found.sort(key=lambda item: item[0])
    return [occurrence for _, occurrence in found]


def find_in_text(path: str, text: str) -> list[Occurrence]:
    """As ocorrências de código, referência e caminho privados no conteúdo, por linha (a primeira é 1).

    Nos `.ts` da zona dos testes, os títulos de teste ficam fora (são nomes, ver `find_in_names`); na lista de
    códigos aceitos, a coluna do texto aceito fica fora."""
    for start, end in reversed(_titles(path, text)):
        text = _blank(text, start, end)
    if path == ACCEPTED_CODES_PATH:
        text = _without_accepted_column(text)
    occurrences: list[Occurrence] = []
    for number, line in enumerate(text.split("\n"), start=1):
        occurrences.extend(_line_occurrences(path, number, line.rstrip("\r")))
    return occurrences


# --------------------------------------------------------------------------------------------------
# Caminhos e nomes
# --------------------------------------------------------------------------------------------------


def _has_slice_code(name: str) -> bool:
    return NAME_PATTERN.search(name) is not None


def find_in_path(path: str) -> list[Occurrence]:
    """Os segmentos do caminho com código de fatia (tipo `name`, linha 0, o segmento como texto)."""
    return [Occurrence(path, 0, "name", segment) for segment in path.split("/") if segment and
            _has_slice_code(segment)]


def _assigned_names(target: ast.AST) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assigned_names(element)
    elif isinstance(target, ast.Starred):
        yield from _assigned_names(target.value)


def _level_assignments(body: Sequence[ast.stmt]) -> Iterable[tuple[int, str]]:
    """As atribuições de um nível (módulo ou classe), entrando nos blocos compostos que não abrem escopo."""
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _assigned_names(target):
                    yield stmt.lineno, name
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            for name in _assigned_names(stmt.target):
                yield stmt.lineno, name
        elif isinstance(stmt, ast.TypeAlias):
            yield from ((stmt.lineno, name) for name in _assigned_names(stmt.name))
        elif isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith)):
            yield from _level_assignments(stmt.body)
            yield from _level_assignments(getattr(stmt, "orelse", []))
        elif isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            yield from _level_assignments(stmt.body)
            for handler in stmt.handlers:  # type: ignore[attr-defined]
                yield from _level_assignments(handler.body)
            yield from _level_assignments(stmt.orelse)  # type: ignore[attr-defined]
            yield from _level_assignments(stmt.finalbody)  # type: ignore[attr-defined]


def _python_names(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    names: list[tuple[int, str]] = list(_level_assignments(tree.body))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append((node.lineno, node.name))
            if isinstance(node, ast.ClassDef):
                names.extend(_level_assignments(node.body))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.append((node.lineno, alias.name))
                if alias.asname:
                    names.append((node.lineno, alias.asname))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append((node.lineno, node.module))
            for alias in node.names:
                names.append((node.lineno, alias.asname or alias.name))
    return sorted(set(names))


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def find_in_names(path: str, source: str) -> list[Occurrence]:
    """Os nomes definidos com código de fatia (Python) e os títulos de teste com código ou referência privada
    (os `.ts` da zona dos testes), todos do tipo `name`. Outros arquivos não têm nomes a conferir."""
    if path.endswith(".py"):
        return [Occurrence(path, line, "name", name) for line, name in _python_names(source)
                if _has_slice_code(name.replace(".", "/"))]
    occurrences: list[Occurrence] = []
    for start, end in _titles(path, source):
        title = source[start:end]
        if any(occurrence.kind in ("code", "private_ref")
               for occurrence in _line_occurrences(path, 0, title.replace("\n", " "))):
            occurrences.append(Occurrence(path, _line_of(source, start), "name", title))
    return occurrences


# --------------------------------------------------------------------------------------------------
# Lista de códigos aceitos
# --------------------------------------------------------------------------------------------------


def _path_ok(path: str) -> bool:
    if path != path.strip() or path.startswith("/") or "\\" in path or any(char in path for char in "*?["):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def load_accepted(text: str) -> list[AcceptedEntry]:
    """As entradas `<caminho>TAB<texto aceito>TAB<motivo>`; linhas vazias e as que começam com `#` ignoradas.

    Levanta `ListInvalid` (mensagem fixa): CRLF, linha fora da forma, campo vazio ou com espaço nas pontas,
    caminho que não é exato, linha ou par caminho e texto repetido."""
    if "\r" in text:
        raise ListInvalid()
    entries: list[AcceptedEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in text.split("\n"):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3 or any(not field or field != field.strip() for field in fields):
            raise ListInvalid()
        path, accepted, reason = fields
        if not _path_ok(path) or (path, accepted) in seen:
            raise ListInvalid()
        seen.add((path, accepted))
        entries.append(AcceptedEntry(path, accepted, reason))
    return entries


def apply_accepted(occurrences: Iterable[Occurrence],
                   entries: Sequence[AcceptedEntry]) -> tuple[list[Occurrence], list[AcceptedEntry]]:
    """O que sobra (as ocorrências sem entrada de mesmo caminho e mesmo texto) e as entradas sem uso, na ordem."""
    keys = {(entry.path, entry.text) for entry in entries}
    used: set[tuple[str, str]] = set()
    remaining: list[Occurrence] = []
    for occurrence in occurrences:
        key = (occurrence.path, occurrence.text)
        if key in keys:
            used.add(key)
        else:
            remaining.append(occurrence)
    return remaining, [entry for entry in entries if (entry.path, entry.text) not in used]
