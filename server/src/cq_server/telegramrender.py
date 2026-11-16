"""Formatadores de texto dos canais: o HTML do Rich, o HTML simples do Telegram e o texto puro do
console, cada um com os seus limites de texto e de legenda.

Os três formatadores ficam no núcleo, puros (sem E/S), e implementam `ports.delivery.MessageFormatter`:
- `TelegramRichFormatter` (`rich`, o padrão do Telegram): HTML do Rich, limite de 32 768 bytes
  UTF-8 do texto sem as tags mais a fonte LaTeX, 500 blocos, 16 níveis e 20 colunas;
- `TelegramHtmlFormatter` (`html`, a alternativa ao Rich): HTML simples do Telegram, 4 096 unidades UTF-16 do
  texto depois do parsing;
- `PlainFormatter` (`plain`): texto puro, o do console, reexportado por
  `cq_server.adapters.console`; 4 096 unidades UTF-16.

O núcleo decide **o que** mostrar pelas capacidades do canal; o formatador decide **como** escrever
no dialeto. Só o `TelegramChannel` (em `bots`) e o `ConsoleChannel` expõem estes formatadores; o
domínio nunca importa este módulo para escolher um canal.

**Conversão IR → mensagem**: o HTML canônico da IR é lido com `html.parser` por uma
**lista de permissão**. As tags fora do subconjunto canônico (e todos os atributos, salvo `colspan`/
`rowspan` das células no Rich e `class="language-*"` do `code` dentro de `pre`) são desembrulhadas: o
texto fica, escapado. As escolhas [I] que as saídas de referência de `tests/fixtures/render/` fixam:
- no HTML simples e no texto, os blocos (`p`, listas, tabela, `pre`, fórmula em bloco, citação) saem
  separados por uma linha em branco (`\\n\\n`); o título (`h1`–`h6`) sai em negrito no HTML simples,
  numa linha própria, seguido de uma quebra só; no Rich, as tags de bloco ficam lado a lado;
- listas em linhas com `• ` (não ordenada) ou `k. ` (ordenada), recuo de 2 espaços por nível;
- tabela: no Rich, `table/tr/th/td` (sem `thead`/`tbody`); com mais de 20 colunas, `<pre>`; no HTML
  simples, `<pre>` com as células separadas por ` | `, uma linha por `tr`; no texto, o mesmo sem `pre`;
- `sup`/`sub` como `^(…)`/`_(…)` fora do Rich;
- marcadores: `media_labels` dá o rótulo de cada marcador pela referência (`mN` da `cq-media`; o
  `name` do `cq-field`); sem rótulo, a própria referência. A imagem vira `[<rótulo>] <alt>` (sem `alt`,
  só `[<rótulo>]`); o campo vira `[<rótulo>]`, em negrito (`<b>`) nos dois modos do Telegram;
- HTML simples (regra do Telegram): `b/i/u/s` nunca contêm `code` nem `pre` (as marcas fecham antes e reabrem depois,
  sem marca vazia); dentro de `pre`, só texto; `blockquote` aninhado é achatado no de fora.

O formatador só produz as entidades `&lt; &gt; &amp;` (válidas nos dois modos). `escape` escreve um
texto de fora da IR como literal: no Rich e no HTML simples, `&`, `<` e `>` viram entidade;
no texto puro, nada muda. `compose` junta blocos já escritos, na ordem e sem mudar nenhum: `\\n\\n`
entre eles no HTML simples e no texto; no Rich, lado a lado, com o bloco sem nenhuma tag de bloco (um
`escape`, por exemplo) dentro de um `<p>`. Toda medida é refeita sobre a marcação final, como o canal
conta.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser

from cq_server.botmessages import utf16_units
from cq_server.ports.delivery import Formatted

# Limites de texto por modo.
PLAIN_MAX_LENGTH = 4096
HTML_MAX_LENGTH = 4096
RICH_MAX_LENGTH = 32768
# Os outros limites do Rich.
RICH_MAX_BLOCKS = 500
RICH_MAX_DEPTH = 16
RICH_MAX_COLUMNS = 20
# A legenda de mídia; o eco é truncado antes do escape.
CAPTION_MAX_LENGTH = 1024
ECHO_MAX_LENGTH = 200

MEDIA_SCHEME = "cq-media:"

# O subconjunto canônico da IR: o resto é desembrulhado.
_KNOWN_TAGS = frozenset({
    "p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "u", "s", "sup", "sub", "blockquote",
    "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td", "pre", "code", "img", "cq-math",
    "cq-field"})
_VOID_TAGS = frozenset({"br", "img"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Os blocos da IR (fora de contexto, `li`, `tr`, `th`, `td`, `thead` e `tbody` também são blocos).
_BLOCK_TAGS = frozenset({"p", "blockquote", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
                         "pre"}) | _HEADINGS
# As marcas da IR e as do Telegram (os dois modos).
_MARKS = {"strong": "b", "em": "i", "u": "u", "s": "s"}
_LANGUAGE = re.compile(r"language-[A-Za-z0-9_+#-]{1,32}")
_SPAN = re.compile(r"[1-9][0-9]{0,3}")

# Medição sobre a marcação produzida (só as tags e entidades que este módulo escreve).
_TOKEN = re.compile(r"<(/?)([a-z][a-z0-9-]*)([^>]*)>|&(lt|gt|amp|quot);")
_ENTITY_TEXT = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}
_COLSPAN = re.compile(r'\bcolspan="([0-9]+)"')
# Blocos do Rich (os que contam no limite de blocos), entre as tags que o formatador escreve.
_RICH_BLOCKS = frozenset({"p", "pre", "ul", "ol", "li", "blockquote", "table", "tr", "tg-math-block",
                          "img", "hr"}) | _HEADINGS
_RICH_VOID = frozenset({"br", "hr", "img", "input"})
_RICH_BLOCK_OPEN = re.compile(r"<(?:p|h[1-6]|pre|ul|ol|li|blockquote|table|tr|tg-math-block)[\s>]")


def _escape(text: str) -> str:
    """Texto literal no HTML dos dois modos: só `&`, `<` e `>` (as aspas ficam)."""
    return html.escape(text, quote=False)


# --------------------------------------------------------------------------------------------------
# Leitura do HTML canônico
# --------------------------------------------------------------------------------------------------


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Monta a árvore só com as tags do subconjunto canônico; as outras somem (o texto fica)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("")
        self._stack: list[_Node] = [self.root]

    def _append_text(self, data: str) -> None:
        children = self._stack[-1].children
        if children and isinstance(children[-1], str):
            children[-1] += data
        else:
            children.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _KNOWN_TAGS:
            return
        node = _Node(tag, {name: value or "" for name, value in reversed(attrs)})
        self._stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _KNOWN_TAGS:
            self._stack[-1].children.append(_Node(tag, {name: value or "" for name, value in reversed(attrs)}))

    def handle_endtag(self, tag: str) -> None:
        if tag not in _KNOWN_TAGS or tag in _VOID_TAGS:
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self._append_text(data)


def _parse(canonical_html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(canonical_html)
    builder.close()
    return builder.root


def _text_of(node: _Node | str) -> str:
    """O texto cru de um nó (o de `pre`, `code` e `cq-math`); `br` vira quebra de linha."""
    if isinstance(node, str):
        return node
    if node.tag == "br":
        return "\n"
    return "".join(_text_of(child) for child in node.children)


def _is_block(node: _Node | str) -> bool:
    if isinstance(node, str):
        return False
    return node.tag in _BLOCK_TAGS or (node.tag == "cq-math" and node.attrs.get("display") == "block")


def _blank(node: _Node | str) -> bool:
    return isinstance(node, str) and not node.strip()


def _marker_text(node: _Node, labels: Mapping[str, str]) -> str:
    """O texto de um marcador da IR: `[<rótulo>] <alt>` na imagem, `[<rótulo>]` no campo."""
    if node.tag == "cq-field":
        name = node.attrs.get("name", "")
        return f"[{labels.get(name, name)}]"
    src = node.attrs.get("src", "")
    alt = node.attrs.get("alt", "")
    if not src.startswith(MEDIA_SCHEME):
        return alt
    ref = src[len(MEDIA_SCHEME):]
    label = f"[{labels.get(ref, ref)}]"
    return f"{label} {alt}" if alt else label


def _number_fields(root: _Node) -> None:
    """Troca o `name` de cada `cq-field` por `campo k` (k a partir de 1, na ordem do documento)."""
    count = 0
    stack: list[_Node | str] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            continue
        if node.tag == "cq-field":
            count += 1
            node.attrs["name"] = f"campo {count}"
        stack.extend(reversed(node.children))


def _language(node: _Node) -> str | None:
    """A classe `language-*` do `code` dentro do `pre`, se válida."""
    for child in node.children:
        if isinstance(child, _Node) and child.tag == "code":
            value = child.attrs.get("class", "")
            return value if _LANGUAGE.fullmatch(value) else None
    return None


def _table_rows(table: _Node) -> list[list[_Node]]:
    """As linhas da tabela (as de `thead`/`tbody` inclusive), cada uma com as suas células."""
    rows: list[_Node] = []
    for child in table.children:
        if isinstance(child, _Node):
            if child.tag == "tr":
                rows.append(child)
            elif child.tag in ("thead", "tbody"):
                rows.extend(c for c in child.children if isinstance(c, _Node) and c.tag == "tr")
    return [[c for c in row.children if isinstance(c, _Node) and c.tag in ("th", "td")] for row in rows]


def _span(cell: _Node, name: str) -> str | None:
    value = cell.attrs.get(name)
    return value if value is not None and _SPAN.fullmatch(value) else None


def _columns(rows: list[list[_Node]]) -> int:
    return max((sum(int(_span(c, "colspan") or 1) for c in row) for row in rows), default=0)


# --------------------------------------------------------------------------------------------------
# Rich (`rich`)
# --------------------------------------------------------------------------------------------------


class _RichWriter:
    """A IR no HTML do Rich: as tags da IR com os nomes do Telegram, na mesma estrutura."""

    def __init__(self, labels: Mapping[str, str]) -> None:
        self._labels = labels

    def children(self, node: _Node) -> str:
        items = node.children
        if node.tag in ("ul", "ol", "table", "thead", "tbody", "tr"):
            items = [c for c in items if not _blank(c)]
        elif node.tag in ("", "blockquote", "li"):
            items = [c for i, c in enumerate(items) if not (_blank(c) and _between_blocks(items, i))]
        return "".join(self.node(child, node.tag) for child in items)

    def node(self, node: _Node | str, parent: str) -> str:
        if isinstance(node, str):
            return _escape(node)
        tag = node.tag
        if tag == "br":
            return "<br>"
        if tag in _MARKS:
            mark = _MARKS[tag]
            return f"<{mark}>{self.children(node)}</{mark}>"
        if tag in ("p", "sup", "sub", "blockquote") or tag in _HEADINGS:
            return f"<{tag}>{self.children(node)}</{tag}>"
        if tag in ("ul", "ol"):
            inner = "".join(self.node(c, tag) if isinstance(c, _Node) and c.tag == "li"
                            else f"<li>{self.node(c, 'li')}</li>"
                            for c in node.children if not _blank(c))
            return f"<{tag}>{inner}</{tag}>"
        if tag == "li" and parent in ("ul", "ol"):
            return f"<li>{self.children(node)}</li>"
        if tag == "table":
            return self._table(node)
        if tag == "pre":
            language = _language(node)
            text = _escape(_text_of(node))
            return f'<pre><code class="{language}">{text}</code></pre>' if language else f"<pre>{text}</pre>"
        if tag == "code":
            return f"<code>{_escape(_text_of(node))}</code>"
        if tag == "cq-math":
            name = "tg-math-block" if node.attrs.get("display") == "block" else "tg-math"
            return f"<{name}>{_escape(_text_of(node))}</{name}>"
        if tag == "img":
            return _escape(_marker_text(node, self._labels))
        if tag == "cq-field":
            return f"<b>{_escape(_marker_text(node, self._labels))}</b>"
        # `li`, `tr`, `th`, `td`, `thead` e `tbody` fora do contexto: desembrulhados.
        return self.children(node)

    def _table(self, table: _Node) -> str:
        rows = _table_rows(table)
        if _columns(rows) > RICH_MAX_COLUMNS:
            # Limite do Rich: mais de 20 colunas não cabem numa tabela do Rich; o texto vai num `pre`.
            lines = (" | ".join(_cell_text(cell, self._labels) for cell in row) for row in rows)
            return f"<pre>{_escape(chr(10).join(lines))}</pre>"
        out = ["<table>"]
        for row in rows:
            out.append("<tr>")
            for cell in row:
                attrs = "".join(f' {name}="{value}"' for name in ("colspan", "rowspan")
                                if (value := _span(cell, name)) is not None)
                out.append(f"<{cell.tag}{attrs}>{self.children(cell)}</{cell.tag}>")
            out.append("</tr>")
        out.append("</table>")
        return "".join(out)


def _between_blocks(items: Sequence[_Node | str], index: int) -> bool:
    """O texto em branco na posição `index` fica entre blocos (ou entre um bloco e a borda)."""
    before = items[index - 1] if index > 0 else None
    after = items[index + 1] if index + 1 < len(items) else None
    return (before is None or _is_block(before)) and (after is None or _is_block(after))


# --------------------------------------------------------------------------------------------------
# HTML simples (`html`) e texto (`plain`)
# --------------------------------------------------------------------------------------------------


class _Inline:
    """Um trecho de linha. No HTML simples, as marcas abrem só antes de um texto (nenhuma marca
    vazia) e fecham antes de `code`/`pre` (no Telegram, `b/i/u/s` nunca os contêm), reabrindo depois."""

    def __init__(self, markup: bool) -> None:
        self._markup = markup
        self._parts: list[str] = []
        self._wanted: list[str] = []
        self._open: list[str] = []

    def push(self, mark: str) -> None:
        self._wanted.append(mark)

    def pop(self) -> None:
        self._wanted.pop()

    def _sync(self, target: list[str]) -> None:
        common = 0
        while (common < len(self._open) and common < len(target)
               and self._open[common] == target[common]):
            common += 1
        self._parts.extend(f"</{mark}>" for mark in reversed(self._open[common:]))
        self._parts.extend(f"<{mark}>" for mark in target[common:])
        self._open = list(target)

    def text(self, text: str) -> None:
        if not text:
            return
        if self._markup:
            self._sync(self._wanted)
            self._parts.append(_escape(text))
        else:
            self._parts.append(text)

    def newline(self) -> None:
        self._parts.append("\n")

    def atomic(self, markup: str) -> None:
        """Um trecho já escrito que nenhuma marca pode conter (`code`, `pre`)."""
        if self._markup:
            self._sync([])
        self._parts.append(markup)

    def finish(self) -> str:
        if self._markup:
            self._sync([])
        return "".join(self._parts)


@dataclass(frozen=True)
class _Block:
    text: str
    separator: str = "\n\n"  # o que vem depois dele, antes do próximo bloco


def _join(blocks: Sequence[_Block]) -> str:
    out: list[str] = []
    for index, block in enumerate(blocks):
        if index:
            out.append(blocks[index - 1].separator)
        out.append(block.text)
    return "".join(out)


class _FlatWriter:
    """A IR no HTML simples (`markup=True`) ou no texto puro (`markup=False`): uma sequência de blocos."""

    def __init__(self, labels: Mapping[str, str], *, markup: bool) -> None:
        self._labels = labels
        self._markup = markup

    def render(self, root: _Node) -> str:
        return _join(self.blocks(root.children, in_quote=False))

    def blocks(self, nodes: Sequence[_Node | str], *, in_quote: bool) -> list[_Block]:
        result: list[_Block] = []
        run: list[_Node | str] = []

        def flush() -> None:
            text = self._run_text(run)
            if text.strip():
                result.append(_Block(text))
            run.clear()

        for node in nodes:
            if _is_block(node):
                flush()
                result.extend(self.block(node, in_quote=in_quote))  # type: ignore[arg-type]
            else:
                run.append(node)
        flush()
        return result

    def inline_text(self, nodes: Sequence[_Node | str], *, bold: bool = False) -> str:
        writer = _Inline(self._markup)
        if bold and self._markup:
            writer.push("b")
        self.inline(nodes, writer)
        return writer.finish()

    def block(self, node: _Node, *, in_quote: bool) -> list[_Block]:
        tag = node.tag
        if tag in _HEADINGS:
            text = self.inline_text(node.children, bold=True)
            return [_Block(text, "\n")] if text.strip() else []
        if tag == "blockquote":
            inner = self.blocks(node.children, in_quote=True)
            if not inner:
                return []
            if self._markup:
                # No Telegram, `blockquote` não se aninha; o de dentro é achatado no de fora.
                return inner if in_quote else [_Block(f"<blockquote>{_join(inner)}</blockquote>")]
            return [_Block("\n".join(f"> {line}" if line else ">" for line in _join(inner).split("\n")))]
        if tag in ("ul", "ol"):
            lines = self.list_lines(node, 0)
            return [_Block("\n".join(lines))] if lines else []
        if tag == "table":
            text = "\n".join(" | ".join(_cell_text(cell, self._labels) for cell in row)
                             for row in _table_rows(node))
            if not text:
                return []
            return [_Block(f"<pre>{_escape(text)}</pre>" if self._markup else text)]
        if tag == "pre":
            text = _text_of(node)
            if not self._markup:
                return [_Block(text)]
            language = _language(node)
            if language:
                return [_Block(f'<pre><code class="{language}">{_escape(text)}</code></pre>')]
            return [_Block(f"<pre>{_escape(text)}</pre>")]
        if tag == "cq-math":
            latex = _text_of(node)
            return [_Block(f"<pre>{_escape(latex)}</pre>" if self._markup else f"$${latex}$$")]
        # `p` e os blocos fora de contexto (`li`, `tr`, `th`, `td`, `thead`, `tbody`): o conteúdo.
        return self.blocks(node.children, in_quote=in_quote)

    def list_lines(self, node: _Node, level: int) -> list[str]:
        """As linhas de uma lista: `• ` ou `k. ` na primeira linha do item, recuo de 2 espaços por nível
        (a sublista vem com o seu)."""
        ordered = node.tag == "ol"
        indent = "  " * level
        lines: list[str] = []
        number = 0
        for item in node.children:
            if _blank(item):
                continue
            number += 1
            marker = f"{number}. " if ordered else "• "
            children = item.children if isinstance(item, _Node) and item.tag == "li" else [item]
            started = False
            for part in self._item_parts(children, level):
                if isinstance(part, list):
                    if not started:
                        lines.append(indent + marker.rstrip())
                        started = True
                    lines.extend(part)
                    continue
                for line in part.split("\n"):
                    lines.append(indent + ("  " if started else marker) + line)
                    started = True
            if not started:
                lines.append(indent + marker.rstrip())
        return lines

    def _item_parts(self, children: Sequence[_Node | str], level: int) -> list[str | list[str]]:
        """O conteúdo de um item: trechos de texto e sublistas (já em linhas), na ordem."""
        parts: list[str | list[str]] = []
        run: list[_Node | str] = []
        for child in children:
            if isinstance(child, _Node) and child.tag in ("ul", "ol"):
                parts.append(self._run_text(run))
                run = []
                parts.append(self.list_lines(child, level + 1))
            elif _is_block(child):
                parts.append(self._run_text(run))
                run = []
                parts.append(_join(self.block(child, in_quote=False)))  # type: ignore[arg-type]
            else:
                run.append(child)
        parts.append(self._run_text(run))
        return [part for part in parts if isinstance(part, list) or part.strip()]

    def _run_text(self, run: Sequence[_Node | str]) -> str:
        """Um trecho de linha; vazio se só tem espaços."""
        if not run or all(_blank(node) for node in run):
            return ""
        return self.inline_text(run)

    def inline(self, nodes: Sequence[_Node | str], writer: _Inline) -> None:
        for node in nodes:
            if isinstance(node, str):
                writer.text(node)
                continue
            tag = node.tag
            if tag in _MARKS:
                if self._markup:
                    writer.push(_MARKS[tag])
                    self.inline(node.children, writer)
                    writer.pop()
                else:
                    self.inline(node.children, writer)
            elif tag in ("sup", "sub"):
                writer.text("^(" if tag == "sup" else "_(")
                self.inline(node.children, writer)
                writer.text(")")
            elif tag == "br":
                writer.newline()
            elif tag in ("code", "pre"):
                text = _text_of(node)
                if self._markup:
                    writer.atomic(f"<code>{_escape(text)}</code>")
                else:
                    writer.text(text)
            elif tag == "cq-math":
                latex = _text_of(node)
                if self._markup:
                    writer.atomic(f"<code>{_escape(latex)}</code>")
                else:
                    fence = "$$" if node.attrs.get("display") == "block" else "$"
                    writer.text(f"{fence}{latex}{fence}")
            elif tag == "img":
                writer.text(_marker_text(node, self._labels))
            elif tag == "cq-field":
                if self._markup:
                    writer.push("b")
                    writer.text(_marker_text(node, self._labels))
                    writer.pop()
                else:
                    writer.text(_marker_text(node, self._labels))
            elif tag == "table":
                writer.text(" ".join(" | ".join(_cell_text(c, self._labels) for c in row)
                                     for row in _table_rows(node)))
            else:
                # Blocos dentro de uma linha (fora do HTML canônico): só o conteúdo.
                self.inline(node.children, writer)


def _cell_text(cell: _Node, labels: Mapping[str, str]) -> str:
    """O texto puro de uma célula, numa linha só."""
    return _FlatWriter(labels, markup=False).inline_text(cell.children).replace("\n", " ")


# --------------------------------------------------------------------------------------------------
# Medição
# --------------------------------------------------------------------------------------------------


def _markup_plain(markup: str) -> str:
    """O texto depois do parsing (tags fora, entidades decodificadas, `<br>` como quebra)."""
    def replace(match: re.Match[str]) -> str:
        if match.group(4) is not None:
            return _ENTITY_TEXT[match.group(4)]
        return "\n" if match.group(2) == "br" and not match.group(1) else ""

    return _TOKEN.sub(replace, markup)


@dataclass(frozen=True)
class _RichMeasure:
    length: int
    blocks: int
    depth: int
    columns: int


def _measure_rich(markup: str) -> _RichMeasure:
    """As medidas do Rich como o Telegram conta: bytes UTF-8 do texto sem as tags, com a fonte
    LaTeX; blocos (as tags de bloco e cada trecho de texto solto no nível de cima); níveis; colunas."""
    plain: list[str] = []
    stack: list[str] = []
    rows: list[int] = []
    blocks = depth = columns = 0
    top_run = False
    position = 0

    def top_inline() -> None:
        nonlocal blocks, top_run
        if not stack and not top_run:
            blocks += 1
            top_run = True

    def text(chunk: str) -> None:
        if chunk:
            plain.append(chunk)
            if chunk.strip():
                top_inline()

    for match in _TOKEN.finditer(markup):
        text(markup[position:match.start()])
        position = match.end()
        closing, name, attrs, entity = match.groups()
        if entity is not None:
            plain.append(_ENTITY_TEXT[entity])
            top_inline()
            continue
        if closing:
            if stack:
                stack.pop()
            if name == "tr" and rows:
                columns = max(columns, rows.pop())
            continue
        if name in ("td", "th") and rows:
            span = _COLSPAN.search(attrs)
            rows[-1] += int(span.group(1)) if span else 1
        if name == "tr":
            rows.append(0)
        if name in _RICH_BLOCKS:
            blocks += 1
            if not stack:
                top_run = False
        else:
            top_inline()
        depth = max(depth, len(stack) + 1)
        if name == "br":
            plain.append("\n")
        if name not in _RICH_VOID:
            stack.append(name)
    text(markup[position:])
    return _RichMeasure(len("".join(plain).encode("utf-8")), blocks, depth, columns)


# --------------------------------------------------------------------------------------------------
# Os formatadores
# --------------------------------------------------------------------------------------------------


class _Formatter:
    """Base dos três formatadores (os métodos de `MessageFormatter`)."""

    text_format: str
    max_length: int

    def from_ir(self, canonical_html: str, *, media_labels: Mapping[str, str]) -> Formatted:
        """O HTML canônico da IR no dialeto do formatador, medido."""
        return self._formatted(self._render(_parse(canonical_html), media_labels))

    def escape(self, text: str) -> Formatted:
        """Um texto de fora da IR como literal, medido."""
        return self._formatted(self._escape(text))

    def compose(self, blocks: Sequence[Formatted]) -> Formatted:
        """Os blocos já escritos neste dialeto, na ordem e sem mudar nenhum, medidos juntos."""
        return self._formatted(self._join([block.markup for block in blocks if block.markup]))

    def plain(self, canonical_html: str) -> str:
        """O texto puro do HTML canônico (prévia, rótulos, botões, legendas): o mesmo nos três.

        Sem rótulos, o campo vira `[campo k]`, com `k` a ordem do marcador no HTML: o nome do campo do PL
        nunca sai.
        """
        root = _parse(canonical_html)
        _number_fields(root)
        return _FlatWriter({}, markup=False).render(root)

    # -- por dialeto --------------------------------------------------------------------------

    def _render(self, root: _Node, labels: Mapping[str, str]) -> str:
        raise NotImplementedError

    def _escape(self, text: str) -> str:
        return _escape(text)

    def _join(self, markups: list[str]) -> str:
        return "\n\n".join(markups)

    def _formatted(self, markup: str) -> Formatted:
        length = utf16_units(_markup_plain(markup))
        return Formatted(markup=markup, text_format=self.text_format, length=length,
                         fits=length <= self.max_length)


class TelegramRichFormatter(_Formatter):
    """O modo `rich` do Telegram (`sendRichMessage`, `skip_entity_detection: true`)."""

    text_format = "rich"
    max_length = RICH_MAX_LENGTH

    def _render(self, root: _Node, labels: Mapping[str, str]) -> str:
        return _RichWriter(labels).children(root)

    def _join(self, markups: list[str]) -> str:
        return "".join(m if _RICH_BLOCK_OPEN.search(m) else f"<p>{m}</p>" for m in markups)

    def _formatted(self, markup: str) -> Formatted:
        measure = _measure_rich(markup)
        fits = (measure.length <= self.max_length and measure.blocks <= RICH_MAX_BLOCKS
                and measure.depth <= RICH_MAX_DEPTH and measure.columns <= RICH_MAX_COLUMNS)
        return Formatted(markup=markup, text_format=self.text_format, length=measure.length, fits=fits)


class TelegramHtmlFormatter(_Formatter):
    """O modo `html` do Telegram (`parse_mode=HTML`, `link_preview_options.is_disabled`)."""

    text_format = "html"
    max_length = HTML_MAX_LENGTH

    def _render(self, root: _Node, labels: Mapping[str, str]) -> str:
        return _FlatWriter(labels, markup=True).render(root)


class PlainFormatter(_Formatter):
    """Texto puro (o console): nada é escapado."""

    text_format = "plain"
    max_length = PLAIN_MAX_LENGTH

    def _render(self, root: _Node, labels: Mapping[str, str]) -> str:
        return _FlatWriter(labels, markup=False).render(root)

    def _escape(self, text: str) -> str:
        return text

    def _formatted(self, markup: str) -> Formatted:
        length = utf16_units(markup)
        return Formatted(markup=markup, text_format=self.text_format, length=length,
                         fits=length <= self.max_length)
