// A árvore de elementos que o PL percorre em cada fase. O traverse.py
// do PL (python/prairielearn/internal/traverse.py) lê o HTML com lxml.html.fragments_fromstring e
// visita todo element.iter() dos fragmentos. Na imagem a193f72 o lxml é o 6.1.1, com o libxml2
// 2.14.6: o tokenizador do libxml2 segue o HTML5, mas a construção da árvore não segue. Este módulo
// reproduz essa árvore:
// - fragments_fromstring (lxml/html/__init__.py): um HTML que não começa (depois de espaços, pela
//   regra \s do Python) por <html ou <!doctype é embrulhado em <html><body>…</body></html>; os
//   fragmentos são os filhos do único <body> filho da raiz (sem nenhum ou com mais de um, o lxml
//   falha e o PL não processa a questão);
// - o tokenizador é o do parse5 que o cheerio do PL usa (7.3.0, com o entities 6), com o texto cru
//   decidido só pelo nome da tag, como no libxml2, que não tem conteúdo estrangeiro (<svg>, <math>)
//   nem CDATA. O parse5 8.0.1 do PL não serve: o entities 8 decodifica, num atributo, uma referência
//   sem ";" seguida de letra quando ela é prefixo de um nome maior ("&notin" vira "¬in"; o HTML5 e o
//   libxml2 a deixam como está);
// - a construção da árvore segue o HTMLparser.c do libxml2: sem modos de inserção do HTML5 (nada de
//   <select>, <table> ou <noscript> especiais); a tag nova fecha só o nó corrente, pela tabela
//   htmlStartClose; html, head e body implícitos (htmlCheckImplied); html, head e body repetidos são
//   descartados; "/>" fecha qualquer elemento; a tag de fechamento fecha até o elemento aberto de
//   mesmo nome, se nenhum elemento acima dele tiver prioridade maior (htmlEndPriority); e o
//   analisador para de vez ao passar de 256 níveis.
// [F] As tabelas abaixo foram medidas no lxml da imagem: todos os pares <A><B> entre as tags que o
// libxml2 conhece, <A><B></A> para as prioridades, os modos de texto cru e os elementos vazios.
import { createRequire } from 'node:module';

import type { Token, TokenHandler } from 'parse5';

/** O parse5 do cheerio (o Tokenizer tem a mesma API do parse5 8, cujos tipos o motor usa). */
const parse5: typeof import('parse5') = createRequire(createRequire(import.meta.url).resolve('cheerio'))('parse5');
const { Tokenizer, TokenizerMode } = parse5;

export interface HtmlElementNode {
  /** Nome da tag como o libxml2 a guarda (só as letras ASCII em minúsculas). */
  name: string;
  /** Atributos na ordem do HTML; de um nome repetido, vale o primeiro. */
  attrs: Record<string, string>;
  parent: HtmlElementNode | null;
  children: HtmlElementNode[];
  /** Trecho [start, end) do elemento no HTML de entrada. */
  start: number;
  end: number;
}

type TokenizerState = (typeof TokenizerMode)[keyof typeof TokenizerMode];

/** Os elementos vazios do libxml2 (info->empty): fecham logo depois da tag de abertura. */
const EMPTY_ELEMENTS: ReadonlySet<string> = new Set([
  'area', 'base', 'basefont', 'br', 'col', 'frame', 'hr', 'img', 'input', 'isindex', 'link', 'meta', 'param',
]);

/** Os elementos de texto cru do libxml2 (info->dataMode), pelo nome, em qualquer contexto. */
const RAW_TEXT: ReadonlyMap<string, TokenizerState> = new Map<string, TokenizerState>([
  ['iframe', TokenizerMode.RAWTEXT],
  ['noembed', TokenizerMode.RAWTEXT],
  ['noframes', TokenizerMode.RAWTEXT],
  ['style', TokenizerMode.RAWTEXT],
  ['xmp', TokenizerMode.RAWTEXT],
  ['script', TokenizerMode.SCRIPT_DATA],
  ['textarea', TokenizerMode.RCDATA],
  ['title', TokenizerMode.RCDATA],
  ['plaintext', TokenizerMode.PLAINTEXT],
]);

const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'];

/** htmlStartClose: o nó corrente (chave) é fechado por uma destas tags novas. */
const START_CLOSE: ReadonlyMap<string, ReadonlySet<string>> = new Map(
  Object.entries({
    a: ['a', 'fieldset', 'table', 'td', 'th'],
    address: ['dd', 'dl', 'dt', 'form', 'li', 'ul'],
    b: ['center', 'p', 'td', 'th'],
    big: ['p'],
    caption: ['col', 'colgroup', 'tbody', 'tfoot', 'thead', 'tr'],
    colgroup: ['colgroup', 'tbody', 'tfoot', 'thead', 'tr'],
    dd: ['dt'],
    dir: ['dd', 'dl', 'dt', 'form', 'ul'],
    dl: ['form', 'li'],
    dt: ['dd', 'dl'],
    font: ['center', 'td', 'th'],
    form: ['form'],
    ...Object.fromEntries(HEADINGS.map((h) => [h, ['fieldset', 'form', 'li', 'p', 'table']])),
    head: [
      'a', 'abbr', 'acronym', 'address', 'b', 'bdo', 'big', 'blockquote', 'body', 'br', 'center', 'cite', 'code', 'dd', 'dfn',
      'dir', 'div', 'dl', 'dt', 'em', 'fieldset', 'font', 'form', 'frameset', ...HEADINGS, 'hr', 'i', 'iframe', 'img',
      'kbd', 'li', 'listing', 'map', 'menu', 'ol', 'p', 'pre', 'q', 's', 'samp', 'small', 'span', 'strike', 'strong',
      'sub', 'sup', 'table', 'tt', 'u', 'ul', 'var', 'xmp',
    ],
    i: ['center', 'p', 'td', 'th'],
    legend: ['fieldset'],
    li: ['li'],
    listing: ['dd', 'dl', 'dt', 'fieldset', 'form', 'li', 'table', 'ul'],
    menu: ['dd', 'dl', 'dt', 'form', 'ul'],
    ol: ['form'],
    option: ['optgroup', 'option'],
    p: [
      'address', 'blockquote', 'body', 'caption', 'center', 'col', 'colgroup', 'dd', 'dir', 'div', 'dl', 'dt', 'fieldset',
      'form', 'frameset', ...HEADINGS, 'head', 'hr', 'li', 'listing', 'menu', 'ol', 'p', 'pre', 'table', 'tbody', 'td',
      'tfoot', 'th', 'title', 'tr', 'ul', 'xmp',
    ],
    pre: ['dd', 'dl', 'dt', 'fieldset', 'form', 'li', 'table', 'ul'],
    s: ['p'],
    small: ['p'],
    span: ['td', 'th'],
    strike: ['p'],
    tbody: ['tbody', 'tfoot'],
    td: ['tbody', 'td', 'tfoot', 'th', 'tr'],
    tfoot: ['tbody'],
    th: ['tbody', 'td', 'tfoot', 'th', 'tr'],
    thead: ['tbody', 'tfoot'],
    tr: ['tbody', 'tfoot', 'tr'],
    tt: ['p'],
    u: ['p', 'td', 'th'],
    ul: ['address', 'form', 'menu', 'pre'],
  }).map(([current, closers]) => [current, new Set(closers)]),
);

/** htmlEndPriority: uma tag de fechamento não fecha elemento de prioridade maior que a dela. */
const END_PRIORITY: ReadonlyMap<string, number> = new Map(
  Object.entries({ div: 150, td: 160, th: 160, tr: 170, thead: 180, tbody: 180, tfoot: 180, table: 190, head: 200, body: 200, html: 220 }),
);
const DEFAULT_END_PRIORITY = 100;

/** As tags que, logo abaixo do html, implicam um head (htmlCheckImplied). */
const HEAD_TAGS: ReadonlySet<string> = new Set(['script', 'style', 'meta', 'link', 'title', 'base']);

/** A profundidade máxima do libxml2 sem XML_PARSE_HUGE: um elemento a mais para o analisador. */
const MAX_DEPTH = 256;

/** ^\s*<(?:html|!doctype) com re.I, como o _looks_like_full_html_unicode do lxml (\s do Python). */
const FULL_HTML = /^[\t\n\v\f\r\x1c-\x1f \x85\xa0\u{1680}\u{2000}-\u{200a}\u{2028}\u{2029}\u{202f}\u{205f}\u{3000}]*<(?:html|!doctype)/iu;

const WRAP_START = '<html><body>';
const WRAP_END = '</body></html>';

function priority(name: string): number {
  return END_PRIORITY.get(name) ?? DEFAULT_END_PRIORITY;
}

function closesCurrent(current: string, tag: string): boolean {
  return START_CLOSE.get(current)?.has(tag) === true;
}

/** A construção da árvore do libxml2 sobre os tokens (offsets no HTML embrulhado). */
class TreeBuilder {
  readonly roots: HtmlElementNode[] = [];
  readonly #stack: HtmlElementNode[] = [];
  /** ctxt->html: 3 depois de um head, 10 depois de um body. */
  #seen = 0;
  /** ctxt->depth: html, head e body descartados, cujas tags de fechamento são ignoradas. */
  #discarded = 0;
  halted = false;

  #current(): HtmlElementNode | undefined {
    return this.#stack.at(-1);
  }

  #push(name: string, attrs: Record<string, string>, start: number): HtmlElementNode {
    const parent = this.#current() ?? null;
    const node: HtmlElementNode = { name, attrs, parent, children: [], start, end: start };
    // Sem nó aberto, o elemento é uma raiz; depois da primeira, fica fora do documento.
    if (parent !== null) parent.children.push(node);
    else if (this.roots.length === 0) this.roots.push(node);
    if (name === 'head' && this.#seen < 3) this.#seen = 3;
    if (name === 'body' && this.#seen < 10) this.#seen = 10;
    this.#stack.push(node);
    return node;
  }

  #pop(end: number): void {
    const node = this.#stack.pop();
    if (node !== undefined) node.end = end;
  }

  /** htmlAutoClose: fecha o nó corrente enquanto a tag nova o fechar. */
  #autoClose(tag: string, at: number): void {
    for (let current = this.#current(); current !== undefined && closesCurrent(current.name, tag); current = this.#current()) {
      this.#pop(at);
    }
  }

  /** htmlCheckImplied: html, head e body implícitos. */
  #checkImplied(tag: string, at: number): void {
    if (tag === 'html') return;
    if (this.#stack.length === 0) this.#push('html', {}, at);
    if (tag === 'body' || tag === 'head') return;
    if (this.#stack.length <= 1 && HEAD_TAGS.has(tag)) {
      if (this.#seen >= 3) return;
      this.#push('head', {}, at);
    } else if (tag !== 'noframes' && tag !== 'frame' && tag !== 'frameset') {
      if (this.#seen >= 10) return;
      if (this.#stack.some((n) => n.name === 'body' || n.name === 'head')) return;
      this.#push('body', {}, at);
    }
  }

  /** Uma tag de abertura; devolve o modo de texto cru em que o tokenizador deve seguir, se houver. */
  startTag(token: Token.TagToken, start: number, end: number): TokenizerState | undefined {
    if (this.halted) return undefined;
    const name = token.tagName;
    this.unfinishedStartTag(name, start);
    const discard =
      (name === 'html' && this.#stack.length > 0) ||
      (name === 'head' && this.#stack.length !== 1) ||
      (name === 'body' && this.#stack.some((n) => n.name === 'body'));
    if (discard) {
      this.#discarded += 1;
      // Como no htmlParseElementInternal: o "/>" de uma tag descartada fecha o nó corrente.
      if (token.selfClosing) this.#pop(end);
      return undefined;
    }
    if (this.#stack.length >= MAX_DEPTH) {
      this.halt(start);
      return undefined;
    }
    const attrs: Record<string, string> = {};
    for (const attr of token.attrs) attrs[attr.name] = attr.value;
    this.#push(name, attrs, start);
    if (token.selfClosing || EMPTY_ELEMENTS.has(name)) {
      this.#pop(end);
      return undefined;
    }
    return RAW_TEXT.get(name);
  }

  /**
   * O que o htmlParseStartTag faz logo depois de ler o nome, antes dos atributos: também numa tag que
   * o HTML deixa por terminar, e que então não vira elemento.
   */
  unfinishedStartTag(name: string, at: number): void {
    if (this.halted) return;
    this.#autoClose(name, at);
    this.#checkImplied(name, at);
  }

  /** htmlParseEndTag e htmlAutoCloseOnClose. */
  endTag(token: Token.TagToken, start: number, end: number): void {
    if (this.halted) return;
    const name = token.tagName;
    if (this.#discarded > 0 && (name === 'html' || name === 'body' || name === 'head')) {
      this.#discarded -= 1;
      return;
    }
    let index = this.#stack.length - 1;
    while (index >= 0 && this.#stack[index].name !== name) index -= 1;
    if (index < 0) return;
    const limit = priority(name);
    for (let i = this.#stack.length - 1; i > index; i -= 1) {
      if (priority(this.#stack[i].name) > limit) return;
    }
    while (this.#stack.length - 1 > index) this.#pop(start);
    this.#pop(end);
  }

  /** Texto fora de espaço em branco direto no html ou no head (htmlStartCharData). */
  text(at: number): void {
    if (this.halted) return;
    const current = this.#current();
    if (current !== undefined && current.name !== 'html' && current.name !== 'head') return;
    if (current?.name === 'head') this.#autoClose('p', at);
    this.#checkImplied('p', at);
  }

  /** Fecha todos os elementos abertos (fim do HTML ou parada do analisador). */
  halt(at: number): void {
    while (this.#stack.length > 0) this.#pop(at);
    this.halted = true;
  }
}

function location(token: { location: Token.Location | null }): Token.Location {
  const loc = token.location;
  if (loc === null) throw new Error('o tokenizador do parse5 não devolveu a posição do token');
  return loc;
}

/** O tokenizador, com acesso à tag de abertura em que o HTML terminou (eof-in-tag, não emitida). */
class FragmentTokenizer extends Tokenizer {
  unfinishedStartTag(lastEmitted: Token.TagToken | null): Token.TagToken | null {
    const token = this.currentToken;
    if (token === null || token === lastEmitted || token.type !== parse5.Token.TokenType.START_TAG) return null;
    return token;
  }
}

/**
 * lxml.html.fragments_fromstring (lxml 6.1.1 e libxml2 2.14.6, na imagem do PL): os elementos de
 * primeiro nível, com os descendentes, e os trechos no HTML de entrada. null quando o lxml falharia
 * (o documento sem nenhum body ou com mais de um).
 */
export function fragmentsFromString(html: string): HtmlElementNode[] | null {
  const wrapped = !FULL_HTML.test(html);
  const source = wrapped ? `${WRAP_START}${html}${WRAP_END}` : html;
  const shift = wrapped ? WRAP_START.length : 0;
  const clamp = (offset: number): number => Math.min(Math.max(offset - shift, 0), html.length);
  const builder = new TreeBuilder();
  let tokenizer: FragmentTokenizer | null = null;
  let lastStartTag: Token.TagToken | null = null;
  const handler: TokenHandler = {
    onStartTag(token) {
      lastStartTag = token;
      const loc = location(token);
      const raw = builder.startTag(token, loc.startOffset, loc.endOffset);
      if (raw !== undefined && tokenizer !== null) tokenizer.state = raw;
    },
    onEndTag(token) {
      const loc = location(token);
      builder.endTag(token, loc.startOffset, loc.endOffset);
    },
    onCharacter(token) {
      builder.text(location(token).startOffset);
    },
    onNullCharacter(token) {
      builder.text(location(token).startOffset);
    },
    onWhitespaceCharacter() {},
    onComment() {},
    onDoctype() {},
    onEof() {
      const unfinished = tokenizer?.unfinishedStartTag(lastStartTag) ?? null;
      if (unfinished !== null) builder.unfinishedStartTag(unfinished.tagName, location(unfinished).startOffset);
      builder.halt(source.length);
    },
    onParseError: null,
  };
  tokenizer = new FragmentTokenizer({ sourceCodeLocationInfo: true }, handler);
  tokenizer.write(source, true);

  const root = builder.roots[0];
  if (root === undefined || root.name !== 'html') return null;
  const bodies = root.children.filter((n) => n.name === 'body');
  if (bodies.length !== 1) return null;
  const fragments = bodies[0].children;
  for (const node of iterElements(fragments)) {
    node.start = clamp(node.start);
    node.end = clamp(node.end);
  }
  return fragments;
}

/** element.iter() de cada fragmento: todos os elementos, em pré-ordem (a ordem das tags no HTML). */
export function* iterElements(fragments: readonly HtmlElementNode[]): Generator<HtmlElementNode> {
  const stack = [...fragments].reverse();
  for (let node = stack.pop(); node !== undefined; node = stack.pop()) {
    yield node;
    for (let i = node.children.length - 1; i >= 0; i -= 1) stack.push(node.children[i]);
  }
}
