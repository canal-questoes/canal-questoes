// Comparação campo a campo entre o motor e o oráculo, e o relatório por questão, seed, caso e campo.
//
// - `compareVariant`: `snapshot.params` × `params` e `snapshot.correct_answers` × `true_answer`.
// - `compareGrade`, sobre a correção da MESMA resposta crua, pares motor × oráculo:
//   `valid` × `gradable`; `format_errors[k].html` × `format_errors[k]`; `format_errors[k].text` × o texto
//   de `format_errors[k]` pela regra de conversão de html em texto (`htmlToText`); `submitted_answers` ×
//   `submitted_answer`; `partial_scores`; `score`; `feedback`; `params`; `correct_answers` × `true_answer`.
//   E `warnings` × `[]`: as 5 questões de referência não imprimem no console (nem o `server.py` delas nem
//   os 5 elementos dessas questões têm `print`, conferido no PL a193f72), e `missing_media`/`media_error` vêm
//   de mídia, que a correção não produz. A suíte confere no oráculo, a cada execução, que
//   ele também não registrou saída no console nessas variantes (`conformance.test.ts`).
// - O JSON é normalizado com as chaves ordenadas, e `1` e `1.0` são iguais (ambos viram o mesmo número).
// - Antes dos campos, compara o desfecho (`outcome` do motor × variante/submissão quebrada no oráculo).
// - `formatReport` agrupa por questão, seed, caso e campo.
import type { OracleSubmission, OracleVariant } from './oracle.ts';

/** Onde a divergência aconteceu: questão, seed do motor e caso (ex.: `test:correct`, `mao:vazio`). */
export interface CaseKey {
  qid: string;
  seed: string;
  case: string;
}

export interface FieldDiff {
  /** Caminho do lado do motor (ex.: `score`, `format_errors.c_1.text`, `params.a`). */
  field: string;
  engine: unknown;
  oracle: unknown;
  /** O lado em que o campo não existe. */
  missing?: 'engine' | 'oracle';
}

export interface Divergence extends CaseKey, FieldDiff {
  /** Contexto curto (ex.: o motivo de uma variante quebrada). */
  detail?: string;
}

/** O que a comparação usa de um resultado do motor (um item de `results` da CLI). */
export interface EngineResultLike {
  status: number;
  body: unknown;
}

export type ComparableVariant = Pick<OracleVariant, 'params' | 'true_answer' | 'broken_at'>;
export type ComparableSubmission = Pick<
  OracleSubmission,
  | 'gradable'
  | 'broken'
  | 'format_errors'
  | 'submitted_answer'
  | 'partial_scores'
  | 'score'
  | 'feedback'
  | 'params'
  | 'true_answer'
>;

type JsonObject = Record<string, unknown>;

/** JSON normalizado: chaves ordenadas, `undefined` fora dos objetos, `-0` como `0`. */
export function normalize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => (item === undefined ? null : normalize(item)));
  if (isObject(value)) {
    const out: JsonObject = {};
    for (const key of Object.keys(value).sort()) {
      const item = value[key];
      if (item !== undefined) out[key] = normalize(item);
    }
    return out;
  }
  if (typeof value === 'number' && Object.is(value, -0)) return 0;
  return value;
}

/** Diferenças entre dois valores JSON, campo a campo, a partir de `path`. */
export function diffJson(engine: unknown, oracle: unknown, path: string): FieldDiff[] {
  return walk(normalize(engine), normalize(oracle), path);
}

function walk(a: unknown, b: unknown, path: string): FieldDiff[] {
  if (isObject(a) && isObject(b)) {
    const out: FieldDiff[] = [];
    for (const key of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) {
      const sub = path ? `${path}.${key}` : key;
      if (!(key in a)) out.push({ field: sub, engine: undefined, oracle: b[key], missing: 'engine' });
      else if (!(key in b)) out.push({ field: sub, engine: a[key], oracle: undefined, missing: 'oracle' });
      else out.push(...walk(a[key], b[key], sub));
    }
    return out;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    const out: FieldDiff[] = [];
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const sub = `${path}[${i}]`;
      if (i >= a.length) out.push({ field: sub, engine: undefined, oracle: b[i], missing: 'engine' });
      else if (i >= b.length) out.push({ field: sub, engine: a[i], oracle: undefined, missing: 'oracle' });
      else out.push(...walk(a[i], b[i], sub));
    }
    return out;
  }
  if (a === b) return [];
  const diff: FieldDiff = { field: path, engine: a, oracle: b };
  if (a === undefined) diff.missing = 'engine';
  else if (b === undefined) diff.missing = 'oracle';
  return [diff];
}

/** Compara a variante do motor (`create_variant`) com uma variante do oráculo. */
export function compareVariant(key: CaseKey, engine: EngineResultLike | undefined, oracle: ComparableVariant): Divergence[] {
  const outcome = checkOutcome(key, engine, oracle.broken_at == null ? 'ok' : 'broken');
  if (!outcome.bothOk) return outcome.divergences;
  const snapshot = isObject(outcome.body.snapshot) ? outcome.body.snapshot : {};
  return [
    ...diffJson(snapshot.params, oracle.params, 'params'),
    ...diffJson(snapshot.correct_answers, oracle.true_answer, 'correct_answers'),
  ].map((d) => ({ ...key, ...d }));
}

/** Compara a correção do motor (`grade`) com a submissão que o oráculo gravou para a mesma resposta crua. */
export function compareGrade(key: CaseKey, engine: EngineResultLike | undefined, oracle: ComparableSubmission): Divergence[] {
  const outcome = checkOutcome(key, engine, oracle.broken ? 'broken' : 'ok');
  if (!outcome.bothOk) return outcome.divergences;
  const body = outcome.body;
  return [
    ...diffJson(body.valid, oracle.gradable, 'valid'),
    ...compareFormatErrors(body.format_errors, oracle.format_errors),
    ...diffJson(body.submitted_answers, oracle.submitted_answer, 'submitted_answers'),
    ...diffJson(body.partial_scores, oracle.partial_scores, 'partial_scores'),
    ...diffJson(body.score, oracle.score, 'score'),
    ...diffJson(body.feedback, oracle.feedback, 'feedback'),
    ...diffJson(body.params, oracle.params, 'params'),
    ...diffJson(body.correct_answers, oracle.true_answer, 'correct_answers'),
    ...diffJson(body.warnings, EXPECTED_GRADE_WARNINGS, 'warnings').map((d) => ({ ...d, detail: WARNINGS_DETAIL })),
  ].map((d) => ({ ...key, ...d }));
}

/** Avisos esperados numa correção `ok` das questões de referência: nenhum (ver o cabeçalho). */
const EXPECTED_GRADE_WARNINGS: readonly unknown[] = [];
const WARNINGS_DETAIL =
  'as questões de referência não imprimem no console e a correção não produz mídia: nenhum aviso esperado';

type FieldDiffWithDetail = FieldDiff & { detail?: string };

/**
 * `format_errors` do motor (`{k: {html, text}}`) × o do oráculo (`{k: html}`), chave a chave:
 * - chave de um lado só: `format_errors.<k>`, com o lado ausente;
 * - `format_errors.<k>.html` × o html do oráculo, sem alteração;
 * - `format_errors.<k>.text` × `htmlToText(html do oráculo)`. Um item que não é objeto,
 *   ou sem `text`, diverge nos dois campos que faltam.
 */
function compareFormatErrors(engine: unknown, oracle: unknown): FieldDiffWithDetail[] {
  if (!isObject(engine) || !isObject(oracle)) return diffJson(engine, oracle, 'format_errors');
  const out: FieldDiffWithDetail[] = [];
  for (const key of [...new Set([...Object.keys(engine), ...Object.keys(oracle)])].sort()) {
    const field = `format_errors.${key}`;
    const item = engine[key];
    const html = oracle[key];
    if (item === undefined) {
      out.push({ field, engine: undefined, oracle: html, missing: 'engine' });
      continue;
    }
    if (html === undefined) {
      out.push({ field, engine: item, oracle: undefined, missing: 'oracle' });
      continue;
    }
    const entry = isObject(item) ? item : {};
    const shape = isObject(item) ? undefined : `o item do motor não é um objeto {html, text}: ${short(item)}`;
    out.push(...diffJson(entry.html, html, `${field}.html`).map((d) => (shape ? { ...d, detail: shape } : d)));
    // Sem html de texto no oráculo não há texto esperado; o html já foi comparado acima.
    if (typeof html !== 'string') continue;
    const expected = htmlToText(html);
    if (expected === null) {
      out.push({
        field: `${field}.text`,
        engine: entry.text,
        oracle: undefined,
        detail: `o comparador não sabe converter em texto o html do oráculo (${short(html, 120)}); confira à mão`,
      });
      continue;
    }
    if (entry.text !== expected) {
      const diff: FieldDiffWithDetail = { field: `${field}.text`, engine: entry.text, oracle: expected };
      if (entry.text === undefined) diff.missing = 'engine';
      if (shape) diff.detail = shape;
      out.push(diff);
    }
  }
  return out;
}

/** Entidades nomeadas que `htmlToText` decodifica; qualquer outra faz a conversão desistir (`null`). */
const NAMED_ENTITIES: Readonly<Record<string, string>> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
};

/**
 * Elementos cujo conteúdo o HTML não lê como marcação comum (RAWTEXT, RCDATA, PLAINTEXT), que o parser guarda
 * à parte (`template`) ou que podem reordenar o texto (as tags de tabela, pelo "foster parenting"): fora do
 * alcance de `htmlToText`.
 */
const UNSUPPORTED_ELEMENTS = new Set([
  'caption',
  'col',
  'colgroup',
  'iframe',
  'noembed',
  'noframes',
  'noscript',
  'plaintext',
  'script',
  'style',
  'table',
  'tbody',
  'td',
  'template',
  'textarea',
  'tfoot',
  'th',
  'thead',
  'title',
  'tr',
  'xmp',
]);

const START_TAG = /<([A-Za-z][^\s/>]*)(?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?|\s*\/)*\s*>/y;
const END_TAG = /<\/([A-Za-z][^\s/>]*)\s*>/y;
const COMMENT = /<!--([\s\S]*?)-->/y;

/**
 * Texto simples de um html de erro de formato, pela regra de conversão de html em texto: o conteúdo textual, sem as
 * tags nem os comentários, com as entidades decodificadas, os espaços consecutivos (`\s+`) colapsados num
 * só e aparado. É a mesma regra do `htmlToText` de `engine/test/integration/grade.test.ts` (o `.text()` do
 * cheerio), escrita aqui sem dependências porque este arquivo roda no host, sem `node_modules`.
 *
 * Cobre o que as mensagens do PL usam: tags com atributos entre aspas, comentários, entidades numéricas e
 * `&amp; &lt; &gt; &quot; &apos; &nbsp;`. Devolve `null` para o resto (outra entidade nomeada ou sem `;`,
 * `<!DOCTYPE>`, CDATA, `<?…>`, os elementos de `UNSUPPORTED_ELEMENTS`, tag malformada, NUL), em vez de
 * adivinhar o que o parser do HTML faria; o comparador acusa esse caso para conferência à mão.
 */
export function htmlToText(html: string): string | null {
  if (html.includes('\u0000')) return null;
  let text = '';
  let i = 0;
  while (i < html.length) {
    const lt = html.indexOf('<', i);
    const chunk = decodeEntities(html.slice(i, lt === -1 ? html.length : lt));
    if (chunk === null) return null;
    text += chunk;
    if (lt === -1) break;
    const next = html[lt + 1] ?? '';
    if (/[A-Za-z]/.test(next)) {
      START_TAG.lastIndex = lt;
      const match = START_TAG.exec(html);
      if (!match || UNSUPPORTED_ELEMENTS.has((match[1] ?? '').toLowerCase())) return null;
      i = START_TAG.lastIndex;
    } else if (next === '/') {
      END_TAG.lastIndex = lt;
      const match = END_TAG.exec(html);
      if (!match) return null;
      i = END_TAG.lastIndex;
    } else if (next === '!') {
      COMMENT.lastIndex = lt;
      const match = COMMENT.exec(html);
      // `<!-->`, `<!--->` e `--!>` fecham o comentário de outro jeito no HTML: fora do alcance.
      const body = match?.[1];
      if (body === undefined || body.startsWith('>') || body.startsWith('->') || body.includes('--!>')) return null;
      i = COMMENT.lastIndex;
    } else if (next === '?') {
      return null;
    } else {
      // `<` sem tag depois (ex.: `1 < 2`) é texto.
      text += '<';
      i = lt + 1;
    }
  }
  return text.replace(/\s+/g, ' ').trim();
}

/** Decodifica as entidades de um trecho de texto; `null` se houver uma que `htmlToText` não cobre. */
function decodeEntities(chunk: string): string | null {
  let out = '';
  let i = 0;
  while (i < chunk.length) {
    const amp = chunk.indexOf('&', i);
    if (amp === -1) {
      out += chunk.slice(i);
      break;
    }
    out += chunk.slice(i, amp);
    const rest = chunk.slice(amp);
    const numeric = /^&#(?:[xX]([0-9A-Fa-f]+)|([0-9]+));?/.exec(rest);
    if (numeric) {
      const code = numeric[1] !== undefined ? parseInt(numeric[1], 16) : parseInt(numeric[2] ?? '', 10);
      // O HTML troca ou remapeia estes valores (U+FFFD, tabela do windows-1252): fora do alcance.
      if (!(code > 0 && code <= 0x10ffff) || (code >= 0xd800 && code <= 0xdfff) || (code >= 0x80 && code <= 0x9f)) {
        return null;
      }
      out += String.fromCodePoint(code);
      i = amp + numeric[0].length;
      continue;
    }
    const named = /^&([A-Za-z][A-Za-z0-9]*);/.exec(rest);
    if (named) {
      const name = named[1] ?? '';
      const value = Object.hasOwn(NAMED_ENTITIES, name) ? NAMED_ENTITIES[name] : undefined;
      if (value === undefined) return null;
      out += value;
      i = amp + named[0].length;
      continue;
    }
    // `&nome` sem `;` pode ser uma entidade legada no HTML (ex.: `&copy`): fora do alcance.
    if (/^&[A-Za-z]/.test(rest)) return null;
    out += '&';
    i = amp + 1;
  }
  return out;
}

/**
 * Compara o desfecho. `bothOk` diz se os dois lados deram `ok` (e então os campos são comparados);
 * senão, `divergences` traz a divergência de desfecho, ou nada se os dois quebraram.
 */
function checkOutcome(
  key: CaseKey,
  engine: EngineResultLike | undefined,
  oracleOutcome: 'ok' | 'broken',
): { bothOk: boolean; body: JsonObject; divergences: Divergence[] } {
  if (engine === undefined) {
    const divergence: Divergence = { ...key, field: 'outcome', engine: undefined, oracle: oracleOutcome, missing: 'engine' };
    return { bothOk: false, body: {}, divergences: [divergence] };
  }
  const body = isObject(engine.body) ? engine.body : {};
  if (engine.status !== 200) {
    const divergence: Divergence = {
      ...key,
      field: 'outcome',
      engine: `HTTP ${engine.status}`,
      oracle: oracleOutcome,
      detail: short(engine.body),
    };
    return { bothOk: false, body, divergences: [divergence] };
  }
  if (body.outcome !== oracleOutcome) {
    const divergence: Divergence = { ...key, field: 'outcome', engine: body.outcome, oracle: oracleOutcome };
    const reason = body.reason ?? body.reasons;
    if (reason !== undefined) divergence.detail = short(reason);
    return { bothOk: false, body, divergences: [divergence] };
  }
  return { bothOk: oracleOutcome === 'ok', body, divergences: [] };
}

/**
 * Relatório legível, agrupado por questão, seed, caso e campo. Com `limit`, lista
 * só as primeiras divergências e diz quantas ficaram de fora.
 */
export function formatReport(divergences: readonly Divergence[], opts: { limit?: number } = {}): string {
  if (divergences.length === 0) return 'nenhuma divergência entre o motor e o oráculo';
  const shown = opts.limit === undefined ? divergences : divergences.slice(0, opts.limit);
  const byQid = new Map<string, Map<string, Map<string, Divergence[]>>>();
  for (const d of shown) {
    const bySeed = byQid.get(d.qid) ?? new Map<string, Map<string, Divergence[]>>();
    byQid.set(d.qid, bySeed);
    const byCase = bySeed.get(d.seed) ?? new Map<string, Divergence[]>();
    bySeed.set(d.seed, byCase);
    const list = byCase.get(d.case) ?? [];
    byCase.set(d.case, list);
    list.push(d);
  }
  const lines = [`${divergences.length} divergência(s) entre o motor e o oráculo:`];
  for (const [qid, bySeed] of byQid) {
    lines.push(`questão ${qid}`);
    for (const [seed, byCase] of bySeed) {
      lines.push(`  seed ${seed}`);
      for (const [name, list] of byCase) {
        lines.push(`    caso ${name}`);
        for (const d of list) {
          const engine = d.missing === 'engine' ? '(ausente)' : short(d.engine);
          const oracle = d.missing === 'oracle' ? '(ausente)' : short(d.oracle);
          lines.push(`      campo ${d.field}: motor=${engine} oráculo=${oracle}${d.detail ? ` (${d.detail})` : ''}`);
        }
      }
    }
  }
  if (shown.length < divergences.length) {
    lines.push(`… e mais ${divergences.length - shown.length} divergência(s) fora desta listagem`);
  }
  return lines.join('\n');
}

function short(value: unknown, max = 200): string {
  const text = value === undefined ? 'undefined' : (JSON.stringify(value) ?? String(value));
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function isObject(value: unknown): value is JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
