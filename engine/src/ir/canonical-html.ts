// Subconjunto canônico de HTML da IR.
//
// A entrada é HTML renderizado pelo PL (enunciado, opções, gabarito, explicação) ou um atributo (rótulo,
// sufixo). As etapas, nesta ordem, porque as de fórmula e de classe precisam da marcação original:
//  1. (só no gabarito) textarea → pre com o mesmo texto;
//  2. fórmulas (math.ts), com as classes mathjax_ignore/tex2jax_ignore e as fronteiras dos span ainda
//     presentes, como o MathJax as vê na página do PL;
//  3. remoção com o conteúdo: comentários, script, style, template, input, button, select, textarea,
//     noscript, iframe, noembed, noframes e o que o PL esconde por CSS (classes visually-hidden e
//     sr-only);
//  4. table.highlighttable do pl-code → só o conteúdo do td.code; o td.linenos sai [F] (pl-code.py,
//     linenos="table", escondido por CSS);
//  5. pre → texto literal (o realce de sintaxe sai; br vira quebra de linha); o marcador cq-field e o
//     img ficam no lugar em que aparecem, como no PL;
//  6. img → <img src="cq-media:mN" alt="…"> pelo resolveImage, ou o alt como texto (também no pre);
//  7. trocas (b→strong, i→em, ins→u, del/strike→s), desembrulho das tags fora do subconjunto e filtro
//     dos atributos;
//  8. p vazios saem; espaços consecutivos fora de pre e de cq-math viram um só;
//  9. serialização própria: só &amp;, &lt;, &gt; e, nos atributos, &quot;; demais caracteres
//     literais; elementos vazios sem barra; img sem texto alternativo sai sem alt.

import * as cheerio from 'cheerio';
import type { CheerioAPI } from 'cheerio';

import { isComment, isElement, isText, markMath, type DomElement, type DomNode, type DomParent } from './math.ts';

export interface CanonicalizeOptions {
  /** Id da mídia ("m1") para o src e o alt da imagem, ou null: a imagem sai, e o alt fica como texto. */
  resolveImage?: (src: string, alt: string | null) => string | null;
  /**
   * Gabarito (answer_key[].display_html): o textarea vira pre com o mesmo texto, porque no painel
   * de resposta ele guarda o próprio gabarito. Sem esta opção, o textarea sai com o conteúdo.
   */
  textareaAsPre?: boolean;
}

const WITHOUT_ATTRIBUTES = 'p br h1 h2 h3 h4 h5 h6 strong em u s sup sub blockquote ul ol li table thead tbody tr pre';

/** Tags permitidas e os atributos de cada uma. */
const ALLOWED_ATTRIBUTES = new Map<string, readonly string[]>([
  ...WITHOUT_ATTRIBUTES.split(' ').map((tag): [string, readonly string[]] => [tag, []]),
  ['th', ['colspan', 'rowspan']],
  ['td', ['colspan', 'rowspan']],
  ['code', ['class']],
  ['img', ['src', 'alt']],
  ['cq-math', ['display']],
  ['cq-field', ['name']],
]);

const RENAMES = new Map<string, string>([
  ['b', 'strong'],
  ['i', 'em'],
  ['ins', 'u'],
  ['del', 's'],
  ['strike', 's'],
]);

/**
 * Saem com o conteúdo (além dos comentários). O conteúdo das quatro últimas o aluno do PL nunca vê: a
 * página do PL exige JavaScript, e com ele o navegador esconde o noscript; o iframe mostra a página do
 * src, e não o que tem dentro; a folha de estilo padrão do HTML dá display: none a noembed e noframes.
 * O parse5 do cheerio, com scripting, lê o conteúdo das quatro como texto cru: desembrulhadas, elas
 * deixariam a marcação de dentro como texto literal (um campo no noscript viraria o texto
 * "<cq-field …>").
 */
const DROPPED_TAGS = new Set([
  'script',
  'style',
  'template',
  'input',
  'button',
  'select',
  'textarea',
  'noscript',
  'iframe',
  'noembed',
  'noframes',
]);

/** Classes com que o PL (Bootstrap) esconde conteúdo por CSS. */
const HIDDEN_CLASSES = ['visually-hidden', 'sr-only'];

const VOID_TAGS = new Set(['br', 'img']);

const LANGUAGE_CLASS = /^language-[A-Za-z0-9_+#.-]+$/;
const SPAN_NUMBER = /^[0-9]+$/;
/** Espaço do HTML (ASCII whitespace): o \s do JavaScript incluiria o espaço sem quebra. */
const HTML_SPACES = /[\t\n\f\r ]+/g;
const HTML_SPACES_ONLY = /^[\t\n\f\r ]*$/;

function classTokens(el: DomElement): string[] {
  return (el.attribs.class ?? '').split(/[\t\n\f\r ]+/).filter((token) => token !== '');
}

function hasClass(el: DomElement, names: readonly string[]): boolean {
  return classTokens(el).some((token) => names.includes(token));
}

/** Descendentes em ordem de documento (cópia: a árvore pode mudar depois). */
function descendants(root: DomParent): DomNode[] {
  const out: DomNode[] = [];
  const stack: DomNode[] = [...root.children].reverse();
  for (let node = stack.pop(); node !== undefined; node = stack.pop()) {
    out.push(node);
    if ('children' in node) {
      for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i]);
    }
  }
  return out;
}

function elementsNamed(root: DomParent, name: string): DomElement[] {
  return descendants(root).filter((node): node is DomElement => isElement(node) && node.name === name);
}

/** Texto literal de um trecho: o dos nós de texto, com br como quebra de linha. */
function literalText(node: DomParent): string {
  let out = '';
  for (const child of node.children) {
    if (isText(child)) out += child.data;
    else if (isElement(child)) out += child.name === 'br' ? '\n' : literalText(child);
  }
  // O parser do HTML troca CR e CRLF por LF; um CR que veio de &#13; mudaria na releitura.
  return out.replace(/\r\n?/g, '\n');
}

/** Troca o conteúdo do elemento por um único nó de texto (ou nada, se o texto for vazio). */
function setText($: CheerioAPI, el: DomElement, text: string): void {
  if (text === '') $(el).empty();
  else $(el).text(text);
}

function unwrap($: CheerioAPI, el: DomElement): void {
  $(el).replaceWith($(el).contents());
}

// ---------------------------------------------------------------------------------------------
// Etapas
// ---------------------------------------------------------------------------------------------

function textareasToPre($: CheerioAPI, root: DomParent): void {
  for (const textarea of elementsNamed(root, 'textarea')) {
    const text = literalText(textarea);
    textarea.name = 'pre';
    textarea.attribs = {};
    setText($, textarea, text);
  }
}

function removeDropped($: CheerioAPI, root: DomParent): void {
  const stack: DomNode[] = [...root.children];
  for (let node = stack.pop(); node !== undefined; node = stack.pop()) {
    if (isComment(node) || (isElement(node) && (DROPPED_TAGS.has(node.name) || hasClass(node, HIDDEN_CLASSES)))) {
      $(node).remove();
    } else if ('children' in node) {
      stack.push(...node.children);
    }
  }
}

function flattenHighlightTables($: CheerioAPI, root: DomParent): void {
  // De dentro para fora: uma tabela aninhada é resolvida antes da que a contém.
  const tables = elementsNamed(root, 'table').filter((table) => hasClass(table, ['highlighttable']));
  for (const table of tables.reverse()) {
    const cells = descendants(table).filter((node): node is DomElement => isElement(node) && node.name === 'td');
    const code = cells.find((cell) => hasClass(cell, ['code']));
    if (code !== undefined) {
      $(table).replaceWith($(code).contents());
    } else {
      for (const cell of cells) if (hasClass(cell, ['linenos'])) $(cell).remove();
    }
  }
}

/**
 * Elementos que ficam no pre, no ponto em que aparecem: o marcador do campo e a imagem. O PL deixa o
 * elemento renderizado e o img dentro do pre, e o navegador os mostra ali [F] (traverse.py).
 */
function keptInPre(node: DomNode): node is DomElement {
  return isElement(node) && (node.name === 'img' || (node.name === 'cq-field' && node.attribs.name !== undefined));
}

/**
 * O conteúdo de um pre em ordem: trechos de texto literal (como em literalText) e os elementos de
 * keptInPre. O conteúdo acidental de um cq-field fica logo depois dele, como em normalizeElements.
 */
function preContents(node: DomParent, out: Array<string | DomElement> = []): Array<string | DomElement> {
  for (const child of node.children) {
    let text: string | null = null;
    if (isText(child)) text = child.data;
    else if (isElement(child) && child.name === 'br') text = '\n';
    else if (isElement(child)) {
      if (keptInPre(child)) out.push(child);
      preContents(child, out);
    }
    if (text === null) continue;
    const last = out[out.length - 1];
    if (typeof last === 'string') out[out.length - 1] = last + text;
    else out.push(text);
  }
  return out;
}

function flattenPre($: CheerioAPI, root: DomParent): void {
  for (const pre of elementsNamed(root, 'pre').reverse()) {
    const pieces = preContents(pre);
    $(pre).empty();
    for (const piece of pieces) {
      if (typeof piece !== 'string') {
        $(piece).empty();
        $(pre).append(piece);
        continue;
      }
      // Como em literalText: o parser do HTML troca CR e CRLF por LF.
      const text = piece.replace(/\r\n?/g, '\n');
      if (text !== '') $(pre).append($('<cq-text></cq-text>').text(text).contents());
    }
  }
}

function resolveImages($: CheerioAPI, root: DomParent, resolveImage: CanonicalizeOptions['resolveImage']): void {
  for (const img of elementsNamed(root, 'img')) {
    const src = img.attribs.src ?? '';
    // Sem texto alternativo (ausente ou vazio), a imagem não tem alt.
    const alt = img.attribs.alt === undefined || img.attribs.alt === '' ? null : img.attribs.alt;
    const id = src !== '' && resolveImage !== undefined ? resolveImage(src, alt) : null;
    if (id !== null) {
      img.attribs = alt === null ? { src: `cq-media:${id}` } : { src: `cq-media:${id}`, alt };
    } else if (alt !== null) {
      const holder = $('<cq-text></cq-text>').text(alt);
      $(img).replaceWith(holder.contents());
    } else {
      $(img).remove();
    }
  }
}

/** O valor que o atributo permitido guarda no subconjunto, ou null se ele sai. */
function canonicalValue(el: DomElement, attr: string, value: string): string | null {
  switch (attr) {
    case 'class': // só em code: o token language-<nome>
      return classTokens(el).find((token) => LANGUAGE_CLASS.test(token)) ?? null;
    case 'colspan':
    case 'rowspan':
      return SPAN_NUMBER.test(value.trim()) ? value.trim() : null;
    case 'alt': // sem texto alternativo, o atributo sai
      return value === '' ? null : value;
    default:
      return value;
  }
}

function canonicalAttributes(el: DomElement, name: string, allowed: readonly string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const attr of allowed) {
    const value = el.attribs[attr];
    const kept = value === undefined ? null : canonicalValue(el, attr, value);
    if (kept !== null) out[attr] = kept;
  }
  if (name === 'cq-math' && out.display !== 'inline' && out.display !== 'block') out.display = 'inline';
  return out;
}

function normalizeElements($: CheerioAPI, root: DomParent): void {
  for (const node of descendants(root)) {
    if (!isElement(node)) continue;
    const name = RENAMES.get(node.name) ?? node.name;
    const allowed = ALLOWED_ATTRIBUTES.get(name);
    if (allowed === undefined || (name === 'cq-field' && node.attribs.name === undefined)) {
      unwrap($, node);
      continue;
    }
    node.name = name;
    node.attribs = canonicalAttributes(node, name, allowed);
    if (name === 'cq-math') {
      // O conteúdo é só o LaTeX: o texto decodificado, sem marcação.
      const latex = literalText(node);
      const only = node.children.length === 1 ? node.children[0] : undefined;
      if (!(only !== undefined && isText(only) && only.data === latex)) setText($, node, latex);
    } else if (name === 'cq-field' && node.children.length > 0) {
      // O marcador é vazio; um conteúdo acidental fica depois dele, e não se perde.
      $(node).after($(node).contents());
    }
  }
}

function removeEmptyParagraphs($: CheerioAPI, root: DomParent): void {
  for (const p of elementsNamed(root, 'p').reverse()) {
    if (p.children.every((child) => isText(child) && HTML_SPACES_ONLY.test(child.data))) $(p).remove();
  }
}

/** Junta os nós de texto vizinhos e colapsa os espaços, fora de pre e de cq-math. */
function collapseWhitespace($: CheerioAPI, parent: DomParent): void {
  let previous: DomNode | null = null;
  for (const child of [...parent.children]) {
    if (isText(child)) {
      if (previous !== null && isText(previous)) {
        previous.data += child.data;
        $(child).remove();
        continue;
      }
    } else if (isElement(child) && child.name !== 'pre' && child.name !== 'cq-math') {
      collapseWhitespace($, child);
    }
    previous = child;
  }
  for (const child of [...parent.children]) {
    if (!isText(child)) continue;
    child.data = child.data.replace(HTML_SPACES, ' ');
    if (child.data === '') $(child).remove();
  }
}

// ---------------------------------------------------------------------------------------------
// Serialização
// ---------------------------------------------------------------------------------------------

function escapeText(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function escapeAttribute(value: string): string {
  return escapeText(value).replace(/"/g, '&quot;');
}

function serialize(nodes: readonly DomNode[]): string {
  let out = '';
  for (const node of nodes) {
    if (isText(node)) {
      out += escapeText(node.data);
      continue;
    }
    if (!isElement(node)) continue;
    out += `<${node.name}`;
    for (const [attr, value] of Object.entries(node.attribs)) out += ` ${attr}="${escapeAttribute(value)}"`;
    out += '>';
    if (VOID_TAGS.has(node.name)) continue;
    let inner = serialize(node.children);
    // O parser descarta a quebra de linha logo depois de <pre>; a primeira do texto precisa de outra.
    if (node.name === 'pre' && inner.startsWith('\n')) inner = `\n${inner}`;
    out += `${inner}</${node.name}>`;
  }
  return out;
}

/** Canonicaliza um trecho de HTML para o subconjunto da IR. */
export function canonicalizeHtml(html: string, opts: CanonicalizeOptions = {}): string {
  const $ = cheerio.load(html, null, false);
  const root = $.root()[0];
  if (opts.textareaAsPre === true) textareasToPre($, root);
  markMath($, root);
  removeDropped($, root);
  flattenHighlightTables($, root);
  flattenPre($, root);
  resolveImages($, root, opts.resolveImage);
  normalizeElements($, root);
  removeEmptyParagraphs($, root);
  collapseWhitespace($, root);
  return serialize(root.children);
}
