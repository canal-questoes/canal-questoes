// A função render própria do server.py. Na fase render, o PL chama
// qualquer objeto chamável de nome render no módulo (zygote.py e zygote_utils.get_module_function do
// PL a193f72: `fcn in mod and callable(mod[fcn])`), e o HTML dele substitui o da questão. Sem executar
// o server.py, a detecção é sintática e conservadora:
// - `def render(` em qualquer lugar do texto (a regra de sempre, sobre o texto cru);
// - `def render` (ou `async def render`) em qualquer lugar do código, inclusive dentro de def e class,
//   seguido de "(" ou de "[" (parâmetros de tipo, PEP 695: `def render[T](`), também com continuação
//   de linha (\) entre as palavras, como o Python 3.13 da imagem aceita;
// - no nível do módulo (fora de def e class, inclusive dentro de if, try, with, for e while), toda
//   ligação do nome render que pode dar um chamável: class render, atribuição (simples, múltipla, em
//   tupla ou lista, anotada com valor, aumentada), :=, alvo de for e de with, captura de case e
//   from … import … que liga render;
// - `global render` em qualquer lugar (uma função pode ligar o nome do módulo).
// Fora a regra do texto cru, comentários e textos entre aspas não contam, e o código é comparado na
// forma NFKC, como o Python normaliza os nomes (PEP 3131: `def ｒender(` define render). Ficam fora:
// import m as render (um módulo não é chamável), from m import * (os nomes dependem de m),
// except … as render (o nome some no fim do bloco) e as ligações dinâmicas (exec, globals(), setattr).

interface LogicalLine {
  /** Indentação da primeira linha física (tab até o próximo múltiplo de 8, como o tokenizador do Python). */
  indent: number;
  /**
   * O código, com cada texto entre aspas trocado por "" e sem comentários nem quebras de linha, na
   * forma NFKC (os nomes como o Python os lê).
   */
  code: string;
}

const STRING_START = /^(?:[rRbBuUfFtT]{1,2})?(?:'''|"""|'|")/;
const IDENTIFIER_CHAR = /[\p{L}\p{N}_]/u;

/** O início de um texto entre aspas em `i` (prefixo e aspas), ou null. Um prefixo não continua um nome. */
function stringStart(source: string, i: number): { prefix: string; quote: string } | null {
  const c = source[i];
  if (c !== '"' && c !== "'" && IDENTIFIER_CHAR.test(source[i - 1] ?? '')) return null;
  const match = STRING_START.exec(source.slice(i, i + 5));
  if (match === null) return null;
  const prefix = match[0].replace(/['"]+$/, '');
  return { prefix, quote: match[0].slice(prefix.length) };
}

/** O limite de f-strings aninhadas do Python (MAXFSTRINGLEVEL); além dele o arquivo nem compila. */
const MAX_FSTRING_NESTING = 150;

/** O fim de um texto entre aspas que começa em `start` (depois do prefixo e das aspas de abertura). */
function stringEnd(source: string, start: number, quote: string, formatted: boolean, nesting = 0): number {
  if (nesting > MAX_FSTRING_NESTING) return source.length;
  let braces = 0;
  let i = start;
  while (i < source.length) {
    const c = source[i];
    if (c === '\\') {
      i += 2;
      continue;
    }
    if (braces === 0 && source.startsWith(quote, i)) return i + quote.length;
    if (quote.length === 1 && braces === 0 && (c === '\n' || c === '\r')) return i;
    if (formatted && c === '{') {
      if (braces === 0 && source[i + 1] === '{') {
        i += 2;
        continue;
      }
      braces += 1;
    } else if (formatted && c === '}' && braces > 0) {
      braces -= 1;
    } else if (formatted && braces > 0) {
      // Dentro de {…} de uma f-string, outro texto entre aspas (inclusive com as mesmas aspas, Python 3.12).
      const nested = stringStart(source, i);
      if (nested !== null) {
        const formattedNested = /[fFtT]/.test(nested.prefix);
        i = stringEnd(source, i + nested.prefix.length + nested.quote.length, nested.quote, formattedNested, nesting + 1);
        continue;
      }
    }
    i += 1;
  }
  return source.length;
}

/** As linhas lógicas do código Python: junta continuações e o que está entre parênteses. */
function logicalLines(source: string): LogicalLine[] {
  const lines: LogicalLine[] = [];
  let code = '';
  let indent = 0;
  let depth = 0;
  let lineStart = true;
  let i = 0;
  const flush = (): void => {
    if (code.trim() !== '') lines.push({ indent, code: code.normalize('NFKC') });
    code = '';
    depth = 0;
  };
  while (i < source.length) {
    if (lineStart) {
      // Indentação de uma nova linha lógica; linhas em branco e só de comentário não contam.
      let column = 0;
      while (i < source.length && ' \t\f'.includes(source[i])) {
        column = source[i] === '\t' ? column - (column % 8) + 8 : source[i] === '\f' ? 0 : column + 1;
        i += 1;
      }
      if (i < source.length && source[i] === '#') while (i < source.length && source[i] !== '\n' && source[i] !== '\r') i += 1;
      if (i < source.length && (source[i] === '\n' || source[i] === '\r')) {
        i += 1;
        continue;
      }
      indent = column;
      lineStart = false;
      continue;
    }
    const c = source[i];
    if (c === '#') {
      while (i < source.length && source[i] !== '\n' && source[i] !== '\r') i += 1;
      continue;
    }
    if (c === '\\' && (source[i + 1] === '\n' || source[i + 1] === '\r')) {
      i += source[i + 1] === '\r' && source[i + 2] === '\n' ? 3 : 2;
      code += ' ';
      continue;
    }
    if (c === '\n' || c === '\r') {
      i += c === '\r' && source[i + 1] === '\n' ? 2 : 1;
      if (depth > 0) {
        code += ' ';
      } else {
        flush();
        lineStart = true;
      }
      continue;
    }
    const str = stringStart(source, i);
    if (str !== null) {
      i = stringEnd(source, i + str.prefix.length + str.quote.length, str.quote, /[fFtT]/.test(str.prefix));
      code += '""';
      continue;
    }
    if ('([{'.includes(c)) depth += 1;
    else if (')]}'.includes(c) && depth > 0) depth -= 1;
    code += c;
    i += 1;
  }
  flush();
  return lines;
}

/** Divide `text` nas posições de `separator` fora de parênteses, colchetes e chaves. */
function splitTopLevel(text: string, isSeparator: (text: string, i: number) => number): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if ('([{'.includes(c)) depth += 1;
    else if (')]}'.includes(c)) depth = Math.max(0, depth - 1);
    else if (depth === 0) {
      const width = isSeparator(text, i);
      if (width > 0) {
        parts.push(text.slice(start, i));
        start = i + width;
        i += width - 1;
      }
    }
  }
  parts.push(text.slice(start));
  return parts;
}

/**
 * A largura de um "=" de atribuição em `i`: o simples e o último de uma aumentada (+=, <<= …); 0 se for
 * ==, <=, >=, != ou :=, que não atribuem.
 */
function assignmentAt(text: string, i: number): number {
  if (text[i] !== '=') return 0;
  if (text[i + 1] === '=') return 0;
  const before = text[i - 1] ?? '';
  if ('=!:'.includes(before) && before !== '') return 0;
  if ((before === '<' || before === '>') && text[i - 2] !== before) return 0;
  return 1;
}

/** O nome render ligado por um alvo de atribuição (nome solto, em tupla, lista ou parênteses). */
function targetBindsRender(target: string): boolean {
  const grouping: boolean[] = [];
  let previous = '';
  for (let i = 0; i < target.length; i += 1) {
    const c = target[i];
    if ('([{'.includes(c)) {
      // Depois de um nome ou de um fechamento, é chamada ou subscrito: o que está dentro não é alvo.
      grouping.push(c !== '{' && !/[\p{L}\p{N}_)\]}"]/u.test(previous));
      previous = c;
      continue;
    }
    if (')]}'.includes(c)) {
      grouping.pop();
      previous = c;
      continue;
    }
    if (IDENTIFIER_CHAR.test(c) && !IDENTIFIER_CHAR.test(target[i - 1] ?? '')) {
      let end = i;
      while (end < target.length && IDENTIFIER_CHAR.test(target[end])) end += 1;
      let after = end;
      while (after < target.length && target[after].trim() === '') after += 1;
      const next = target[after] ?? ' ';
      if (target.slice(i, end) === 'render' && previous !== '.' && !'.[('.includes(next) && grouping.every(Boolean)) {
        return true;
      }
      previous = target[end - 1];
      i = end - 1;
      continue;
    }
    if (c.trim() !== '') previous = c;
  }
  return false;
}

/** Uma instrução simples do nível do módulo que liga render. */
function statementBindsRender(statement: string): boolean {
  const text = statement.trim();
  if (/(?<![\p{L}\p{N}_.])render\s*:=/u.test(text)) return true;
  if (/^from\s/.test(text)) {
    const names = /^from\s+[^\s]+\s+import\s+(.*)$/s.exec(text)?.[1] ?? '';
    return names
      .replace(/[()]/g, ' ')
      .split(',')
      .some((item) => {
        const [name, alias] = item.trim().split(/\s+as\s+/);
        return (alias ?? name)?.trim() === 'render';
      });
  }
  if (/^(import|del|pass|return|raise|assert|break|continue|nonlocal)\b/.test(text)) return false;
  // type render = … (Python 3.12) liga um TypeAliasType, que não é chamável.
  if (/^type\s+[\p{L}_][\p{L}\p{N}_]*\s*[[=]/u.test(text)) return false;
  // Os alvos são as partes antes do último "=" (numa aumentada, o operador fica no fim do alvo). Um
  // alvo nunca tem lambda: dali em diante é o valor, e o "=" de um padrão de lambda não atribui.
  const parts = splitTopLevel(text, assignmentAt);
  const lambdaAt = parts.findIndex((part) => /(?<![\p{L}\p{N}_])lambda(?![\p{L}\p{N}_])/u.test(part));
  const targets = parts.slice(0, lambdaAt >= 0 ? Math.min(lambdaAt, parts.length - 1) : -1);
  return targets.some((target, index) => {
    // Anotada (render: T = valor): o alvo é o que vem antes do ":".
    const annotated = index === 0 ? splitTopLevel(target, (t, i) => (t[i] === ':' ? 1 : 0)) : [target];
    return targetBindsRender(annotated[0]);
  });
}

/** O cabeçalho de uma instrução composta (o texto antes do ":") liga render? */
function headerBindsRender(header: string): boolean {
  const text = header.trim();
  if (/^class\s+render\b/.test(text)) return true;
  if (/(?<![\p{L}\p{N}_.])render\s*:=/u.test(text)) return true;
  const loop = /^(?:async\s+)?for\s/.exec(text);
  if (loop !== null) {
    const targets = text.slice(loop[0].length);
    const inAt = /\sin(?![\p{L}\p{N}_])/u.exec(targets);
    return targetBindsRender(inAt === null ? targets : targets.slice(0, inAt.index));
  }
  if (/^(?:async\s+)?with\b/.test(text)) {
    // Os itens "expr as alvo", separados por vírgula, também na forma com parênteses do Python 3.10.
    let items = text.replace(/^(?:async\s+)?with\b/, '').trim();
    if (items.startsWith('(') && items.endsWith(')')) items = items.slice(1, -1);
    return splitTopLevel(items, (t, i) => (t[i] === ',' ? 1 : 0)).some((item) => {
      const as = /\bas\b/.exec(item);
      return as !== null && targetBindsRender(item.slice(as.index + 2));
    });
  }
  if (/^case\b/.test(text)) {
    const pattern = text.replace(/^case\b/, '').split(/\bif\b/)[0];
    return /(?<![\p{L}\p{N}_.])render(?!\s*[(.=]|[\p{L}\p{N}_])/u.test(pattern);
  }
  return false;
}

const COMPOUND = /^(?:if|elif|else|for|while|try|except|finally|with|def|class|async|match|case)\b/;

/** Separa "cabeçalho: corpo" de uma instrução composta; null se a linha não é composta. */
function splitCompound(code: string): { header: string; body: string } | null {
  const text = code.trim();
  if (!COMPOUND.test(text)) return null;
  const parts = splitTopLevel(text, (t, i) => (t[i] === ':' && t[i + 1] !== '=' ? 1 : 0));
  if (parts.length === 1) return null;
  return { header: parts[0], body: parts.slice(1).join(':') };
}

function bodyBindsRender(body: string): boolean {
  return splitTopLevel(body, (t, i) => (t[i] === ';' ? 1 : 0)).some(statementBindsRender);
}

function declaresGlobalRender(code: string): boolean {
  return splitTopLevel(code, (t, i) => (t[i] === ';' ? 1 : 0)).some((statement) => {
    const names = /^\s*global\s+(.*)$/s.exec(statement)?.[1];
    return names !== undefined && names.split(',').some((name) => name.trim() === 'render');
  });
}

/**
 * `def render` seguido de "(" ou de "[" numa linha lógica, em que a continuação com \ já virou espaço.
 * Um nome como renderer não conta: depois de render só vêm espaços e o parêntese ou o colchete.
 */
const DEF_RENDER = /(?<![\p{L}\p{N}_])def\s+render\s*[([]/u;

/** O server.py define ou liga render como a regra acima descreve (sem contar o `def render(` do texto cru). */
export function bindsRender(source: string): boolean {
  // Os blocos abertos: a indentação do cabeçalho e se o corpo é do módulo ou de def/class.
  const blocks: Array<{ indent: number; module: boolean }> = [];
  for (const line of logicalLines(source)) {
    if (DEF_RENDER.test(line.code)) return true;
    while (blocks.length > 0 && blocks[blocks.length - 1].indent >= line.indent) blocks.pop();
    const inModule = blocks.length === 0 || blocks[blocks.length - 1].module;
    if (declaresGlobalRender(line.code)) return true;
    const compound = splitCompound(line.code);
    if (compound === null) {
      if (inModule && bodyBindsRender(line.code)) return true;
      continue;
    }
    const opensScope = /^(?:async\s+)?(?:def|class)\b/.test(compound.header.trim());
    if (inModule && headerBindsRender(compound.header)) return true;
    const bodyInModule = inModule && !opensScope;
    if (compound.body.trim() === '') blocks.push({ indent: line.indent, module: bodyInModule });
    else if ((bodyInModule && bodyBindsRender(compound.body)) || declaresGlobalRender(compound.body)) return true;
  }
  return false;
}
