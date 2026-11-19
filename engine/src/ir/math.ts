// Fórmulas da IR: acha o LaTeX nos nós de
// texto como o MathJax da página do PL e troca cada fórmula por <cq-math display="inline|block">, com o
// LaTeX exato (o texto decodificado, sem colapso de espaços).
//
// [F] O PL carrega o MathJax 4.1.3 (apps/prairielearn/package.json) com ignoreHtmlClass
// "mathjax_ignore|tex2jax_ignore", processHtmlClass "mathjax_process" e inlineMath $…$ e \(…\)
// (src/lib/client/mathjax.ts). O resto fica no padrão do MathJax 4.1.3: displayMath $$…$$ e \[…\],
// processEscapes, processEnvironments e processRefs (FindTeX.OPTIONS em mathjax/input/tex.js).
// Sem dependência nova, este módulo espelha as duas peças do MathJax que decidem o que é fórmula:
// - HTMLDomStrings (mathjax/core.js): quais nós de texto entram, onde as cadeias se partem (em toda
//   fronteira de elemento, menos br, wbr e comentários) e as tags e classes ignoradas;
// - FindTeX (mathjax/input/tex.js): os delimitadores, com a contagem de chaves e os escapes.
// Além dos delimitadores acima, o FindTeX também trata \begin{…}…\end{…} (bloco), \ref{…} e
// \eqref{…} (inline) e o escape \\ (vira \); ficam aqui por fidelidade ao PL.
//
// A saída não é idempotente: "\$1 e \$2" vira o texto "$1 e $2", que numa segunda passada seria uma
// fórmula. Por isso o enunciado passa por aqui uma única vez (canonicalizeHtml já inclui esta etapa).

import * as cheerio from 'cheerio';
import type { CheerioAPI } from 'cheerio';

// ---------------------------------------------------------------------------------------------
// Nós do DOM do cheerio (domhandler 5), com os tipos tirados da API pública do cheerio: o pacote
// domhandler que resolve a partir de apps/prairielearn é outra versão (6.x), e não o do cheerio.
// ---------------------------------------------------------------------------------------------

export type DomNode = Parameters<typeof cheerio.contains>[0];
export type DomElement = Extract<DomNode, { attribs: Record<string, string> }>;
export type DomText = Extract<DomNode, { nodeType: 3 }>;
export type DomParent = Extract<DomNode, { children: unknown[] }>;

export function isText(node: DomNode): node is DomText {
  return node.nodeType === 3;
}

export function isComment(node: DomNode): boolean {
  return node.nodeType === 8;
}

export function isElement(node: DomNode): node is DomElement {
  return node.nodeType === 1 && 'attribs' in node;
}

// ---------------------------------------------------------------------------------------------
// FindTeX (MathJax 4.1.3), com as opções do PL
// ---------------------------------------------------------------------------------------------

/** Uma fórmula (ou um escape) achada numa cadeia de texto. */
export interface FoundMath {
  /** Posição do delimitador de abertura na cadeia. */
  start: number;
  /** Posição logo depois do delimitador de fechamento. */
  end: number;
  /** O LaTeX sem os delimitadores; num escape, o caractere literal. */
  math: string;
  /** true = bloco; false = inline; null = escape (\$ ou \\), que vira texto. */
  display: boolean | null;
}

interface EndDelimiter {
  close: string;
  display: boolean;
  pattern: RegExp;
}

const INLINE_MATH: ReadonlyArray<readonly [string, string]> = [
  ['$', '$'],
  ['\\(', '\\)'],
];
const DISPLAY_MATH: ReadonlyArray<readonly [string, string]> = [
  ['$$', '$$'],
  ['\\[', '\\]'],
];

/** quotePattern do MathJax (util/string). */
function quotePattern(text: string): string {
  return text.replace(/([\^$(){}.+*?\-|[\]:\\])/g, '\\$1');
}

/** sortLength do MathJax: mais longo primeiro; empate em ordem lexicográfica. */
function sortLength(a: string, b: string): number {
  if (a.length !== b.length) return b.length - a.length;
  return a === b ? 0 : a < b ? -1 : 1;
}

/** endPattern do FindTeX: o fechamento, um comando ou caractere escapado, ou uma chave. */
function endPattern(closePattern: string): RegExp {
  return new RegExp(`${closePattern}|\\\\(?:[a-zA-Z]|.)|[{}]`, 'g');
}

const END_DELIMITERS = new Map<string, EndDelimiter>();
const START_PATTERN: RegExp = (() => {
  const delimiters: string[] = [];
  const add = ([open, close]: readonly [string, string], display: boolean): void => {
    delimiters.push(quotePattern(open));
    END_DELIMITERS.set(open, { close, display, pattern: endPattern(quotePattern(close)) });
  };
  for (const pair of INLINE_MATH) add(pair, false);
  for (const pair of DISPLAY_MATH) add(pair, true);
  // Grupo 1: nome do ambiente (processEnvironments). Grupo 2: escape (processEscapes) ou \ref/\eqref
  // (processRefs).
  const parts = [
    delimiters.sort(sortLength).join('|'),
    '\\\\begin\\s*\\{([^}]*)\\}',
    '(\\\\([\\\\$])|(\\\\(?:eq)?ref\\s*\\{[^}]*\\}))',
  ];
  return new RegExp(parts.join('|'), 'g');
})();
const ENV_GROUP = 1;
const SUB_GROUP = 2;

/** findEnd do FindTeX: o fechamento só vale fora de chaves; comandos e escapes são pulados. */
function findEnd(text: string, start: RegExpExecArray, end: EndDelimiter): FoundMath | null {
  const from = start.index + start[0].length;
  const pattern = end.pattern;
  pattern.lastIndex = from;
  let braces = 0;
  for (let match = pattern.exec(text); match !== null; match = pattern.exec(text)) {
    if ((match[1] || match[0]) === end.close && braces === 0) {
      return {
        start: start.index,
        end: match.index + match[0].length,
        math: text.substring(from, match.index),
        display: end.display,
      };
    }
    if (match[0] === '{') braces++;
    else if (match[0] === '}' && braces > 0) braces--;
  }
  return null;
}

/** findMathInString do FindTeX: as fórmulas e os escapes de uma cadeia, em ordem e sem sobreposição. */
export function findMath(text: string): FoundMath[] {
  const found: FoundMath[] = [];
  START_PATTERN.lastIndex = 0;
  for (let start = START_PATTERN.exec(text); start !== null; start = START_PATTERN.exec(text)) {
    let item: FoundMath | null;
    const env = start[ENV_GROUP];
    const sub = start[SUB_GROUP];
    if (env !== undefined) {
      const close = `{${env}}`;
      const pattern = endPattern(`\\\\end\\s*(\\{${quotePattern(env)}\\})`);
      item = findEnd(text, start, { close, display: true, pattern });
      // O ambiente inteiro (\begin{…} … \end{…}) é o LaTeX.
      if (item !== null) item.math = text.substring(item.start, item.end);
    } else if (sub !== undefined) {
      const end = start.index + sub.length;
      item =
        sub.length === 2
          ? { start: start.index, end, math: sub.substring(1), display: null }
          : { start: start.index, end, math: sub, display: false };
    } else {
      const delimiter = END_DELIMITERS.get(start[0]);
      item = delimiter === undefined ? null : findEnd(text, start, delimiter);
    }
    if (item !== null) {
      found.push(item);
      START_PATTERN.lastIndex = item.end;
    }
  }
  return found;
}

// ---------------------------------------------------------------------------------------------
// HTMLDomStrings (MathJax 4.1.3): as cadeias de texto do DOM
// ---------------------------------------------------------------------------------------------

// skipHtmlTags do MathJax, mais os marcadores da IR (cq-math já convertido; cq-field vazio).
const SKIP_TAGS =
  /^(?:script|noscript|style|textarea|pre|code|math|select|option|mjx-container|cq-math|cq-field)$/i;
const IGNORE_CLASS = /(?:^| )(?:mathjax_ignore|tex2jax_ignore)(?: |$)/;
const PROCESS_CLASS = /(?:^| )(?:mathjax_process)(?: |$)/;
// includeHtmlTags: entram na cadeia sem partir a fórmula (o comentário também, como "").
const INCLUDE_TAGS = new Map<string, string>([
  ['br', '\n'],
  ['wbr', ''],
]);

interface Piece {
  node: DomNode;
  length: number;
}

interface TextRun {
  text: string;
  pieces: Piece[];
}

/** HTMLDomStrings.find: as cadeias (irmãos consecutivos entre fronteiras de elemento) fora das regiões ignoradas. */
function collectRuns(root: DomParent): TextRun[] {
  const runs: TextRun[] = [];
  let text = '';
  let pieces: Piece[] = [];
  const pushRun = (): void => {
    if (/\S/.test(text)) runs.push({ text, pieces });
    text = '';
    pieces = [];
  };
  const extend = (node: DomNode, value: string): void => {
    pieces.push({ node, length: value.length });
    text += value;
  };

  const stack: Array<[DomNode, boolean]> = [];
  let node: DomNode | null = root.children[0] ?? null;
  let ignore = false;
  while (node !== null) {
    if (isText(node)) {
      if (!ignore) extend(node, node.data);
      node = node.next;
    } else if (isComment(node)) {
      if (!ignore) extend(node, '');
      node = node.next;
    } else if (isElement(node) && INCLUDE_TAGS.has(node.name)) {
      if (!ignore) extend(node, INCLUDE_TAGS.get(node.name) ?? '');
      node = node.next;
    } else if (isElement(node)) {
      pushRun();
      const cls = node.attribs.class ?? '';
      const process = PROCESS_CLASS.test(cls);
      const first: DomNode | undefined = node.children[0];
      if (first !== undefined && node.attribs['data-mjx'] === undefined && (process || !SKIP_TAGS.test(node.name))) {
        if (node.next !== null) stack.push([node.next, ignore]);
        node = first;
        ignore = (ignore || IGNORE_CLASS.test(cls)) && !process;
      } else {
        node = node.next;
      }
    } else {
      // handleOther: outros nós (ex.: o conteúdo de um template) partem a cadeia e não são percorridos.
      pushRun();
      node = node.next;
    }
    if (node === null) {
      const top = stack.pop();
      if (top !== undefined) {
        pushRun();
        [node, ignore] = top;
      }
    }
  }
  pushRun();
  return runs;
}

interface Position {
  node: DomText;
  n: number;
}

/** findPosition do HTMLDocument: a posição na cadeia vira (nó de texto, deslocamento). */
function findPosition(run: TextRun, index: number): Position | null {
  let rest = index;
  for (const piece of run.pieces) {
    if (rest <= piece.length && isText(piece.node)) return { node: piece.node, n: Math.max(rest, 0) };
    rest -= piece.length;
  }
  return null;
}

/**
 * Insere um nó solto ao lado de ref, no mesmo pai. É feito à mão porque o before/after do cheerio 1.2
 * não fazem nada quando ref é um nó de texto (exigem hasChildren(ref)).
 */
function insertBeside(ref: DomNode, node: DomNode, after: boolean): void {
  const parent = ref.parent;
  if (parent === null || node === ref) return;
  if (node.parent !== null) {
    const old = node.parent.children;
    const oldIndex = old.indexOf(node);
    if (oldIndex !== -1) old.splice(oldIndex, 1);
    if (node.prev !== null) node.prev.next = node.next;
    if (node.next !== null) node.next.prev = node.prev;
  }
  const index = parent.children.indexOf(ref);
  if (index === -1) return;
  const prev = after ? ref : ref.prev;
  const next = after ? ref.next : ref;
  parent.children.splice(after ? index + 1 : index, 0, node);
  node.parent = parent;
  node.prev = prev;
  node.next = next;
  if (prev !== null) prev.next = node;
  if (next !== null) next.prev = node;
}

/** split do adaptador do MathJax: o nó fica com [0, offset), e o resto vai para um nó novo, logo depois. */
function splitText(node: DomText, offset: number): DomText {
  const rest = node.cloneNode();
  rest.data = node.data.slice(offset);
  node.data = node.data.slice(0, offset);
  insertBeside(node, rest, true);
  return rest;
}

/** HTMLMathItem.updateDocument: troca o trecho [start, end) da cadeia pelo nó novo. */
function replaceRange($: CheerioAPI, start: Position, end: Position, replacement: DomNode): void {
  if (start.node === end.node) {
    if (end.n > 0 && end.n < end.node.data.length) splitText(end.node, end.n);
    const target = start.n > 0 ? splitText(start.node, start.n) : start.node;
    $(target).replaceWith(replacement);
    return;
  }
  let current: DomNode | null = start.n > 0 ? splitText(start.node, start.n) : start.node;
  while (current !== null && current !== end.node) {
    const next: DomNode | null = current.next;
    $(current).remove();
    current = next;
  }
  insertBeside(end.node, replacement, false);
  if (end.n < end.node.data.length) splitText(end.node, end.n);
  $(end.node).remove();
}

function mathElement($: CheerioAPI, latex: string, display: boolean): DomNode {
  const node = $('<cq-math></cq-math>')
    .attr('display', display ? 'block' : 'inline')
    .text(latex)
    .get(0);
  if (node === undefined) throw new Error('cq-math não foi criado');
  return node;
}

/**
 * Converte as fórmulas da árvore no lugar. Cada escape vira texto; cada fórmula, um <cq-math>. Como no
 * MathJax, as trocas de uma cadeia são feitas de trás para a frente, para as posições valerem.
 */
export function markMath($: CheerioAPI, root: DomParent): void {
  for (const run of collectRuns(root)) {
    const items = findMath(run.text);
    const positions = items.map((item) => [findPosition(run, item.start), findPosition(run, item.end)] as const);
    for (let i = items.length - 1; i >= 0; i--) {
      const item = items[i];
      const [start, end] = positions[i];
      if (start === null || end === null) continue;
      let replacement: DomNode;
      if (item.display === null) {
        const literal = start.node.cloneNode();
        literal.data = item.math;
        replacement = literal;
      } else {
        replacement = mathElement($, item.math, item.display);
      }
      replaceRange($, start, end, replacement);
    }
  }
}

/** Delimitadores de fórmula → <cq-math display="…">. O resto do HTML fica como está. */
export function convertMath(html: string): string {
  const $ = cheerio.load(html, null, false);
  markMath($, $.root()[0]);
  return $.html();
}
