// Quebras e avisos.
// reason.message e warnings[].message são frases funcionais em português, para o professor, sem
// detalhe técnico nem caminho do servidor: dizem a fase com o nome funcional e nunca
// citam a mensagem do PL, a exceção ou o que a questão imprimiu. O technical_detail traz a mensagem,
// o traceback e a saída do Python, truncados em 8000 caracteres, e nunca os dados da chamada
// (o lastCallData do CodeCallerNative, que traz as respostas cruas). A saída inclui a que a fase
// imprimiu antes da quebra e o PL registra como problema não fatal ao lado do fatal ("output logged on
// console"): a da chamada do server.py cujo retorno o checkData recusou, que de outro modo sumiria da
// resposta (uma quebra não leva avisos), e a da fase dos elementos antes de uma quebra do server.py.
// Nas fases parse e grade:
// - a recusa do checkData sai sem os valores que ela cita, o novo e o original de uma alteração
//   ilegal e o de tipo errado;
// - a saída e o texto da exceção do código da questão ficam como o PL os produz; é o registro que os
//   omite. No diagnóstico que o próprio motor monta a partir dos dados da chamada (violação do
//   contrato), a resposta crua é redigida como proteção extra, só nas chaves que o código da questão
//   gravou e nunca no texto do motor.
import type { BrokenReason, BrokenReasonCode, ContractPhase, Warning } from '../contract/types.ts';
import { TIMEOUT_MESSAGE } from '../pl/bindings.ts';
import type { CourseIssue } from '../pl/phases.ts';

export const TECHNICAL_DETAIL_MAX = 8000;

// O render monta o enunciado, as opções e o gabarito (ir/build.ts), e o file só gera as imagens da IR.
const PHASE_NAMES: Record<ContractPhase, string> = {
  generate: 'geração da variante',
  prepare: 'preparação da variante',
  render: 'montagem do enunciado e do gabarito',
  file: 'geração de imagens',
  parse: 'interpretação da resposta',
  grade: 'correção',
};

export function isTimeout(issue: CourseIssue): boolean {
  const cause = issue.cause;
  return cause instanceof Error && cause.message === TIMEOUT_MESSAGE;
}

export function firstFatal(issues: readonly CourseIssue[]): CourseIssue | undefined {
  return issues.find((i) => i.fatal);
}

function codeOf(issue: CourseIssue): BrokenReasonCode {
  if (issue.kind === 'invalid_state') return 'invalid_data';
  if (isTimeout(issue)) return 'timeout';
  return 'question_code_error';
}

export function brokenMessage(code: BrokenReasonCode, phase: ContractPhase, timeoutMs: number): string {
  const where = PHASE_NAMES[phase];
  switch (code) {
    case 'timeout':
      return `A questão está quebrada: o código da questão excedeu o tempo limite de ${formatSeconds(timeoutMs)} na fase de ${where}.`;
    case 'invalid_data':
      // O checkData recusa valor alterado, tipo errado e chave acrescentada (pl/check-data.ts).
      return `A questão está quebrada: o código da questão alterou dados de um modo que a fase de ${where} não permite.`;
    case 'ir_contract_violation':
      return `A questão está quebrada: o resultado da fase de ${where} não pode ser representado no formato do motor de questões.`;
    case 'question_code_error':
      // Também a exceção de um elemento, como a de um atributo inválido; no generate,
      // só o server.py roda (processQuestion do freeform.js).
      return `A questão está quebrada: o código da questão${phase === 'generate' ? '' : ' ou de um de seus elementos'} falhou na fase de ${where}.`;
  }
}

/** Segundos com vírgula decimal e até três casas, sem zeros à direita (1234 → "1,234 s"; 2000 → "2 s"). */
function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(3).replace(/\.?0+$/, '').replace('.', ',')} s`;
}

// ---------------------------------------------------------------------------------------------
// technical_detail
// ---------------------------------------------------------------------------------------------

/** Corta no limite do contrato, guardando o começo e o fim, sem partir um par substituto. */
export function truncateDetail(text: string, max = TECHNICAL_DETAIL_MAX): string {
  if (text.length <= max) return text;
  const marker = `\n[… ${text.length - max} caracteres omitidos …]\n`;
  const room = max - marker.length - 8;
  let head = Math.floor(room / 3);
  let tailStart = text.length - (room - head);
  // Sem metade de um par substituto nas bordas.
  if (head > 0 && isHighSurrogate(text.charCodeAt(head - 1))) head -= 1;
  if (tailStart < text.length && isLowSurrogate(text.charCodeAt(tailStart))) tailStart += 1;
  return text.slice(0, head) + marker + text.slice(tailStart);
}

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}

const REDACTED = '[resposta redigida]';
// Valores curtos (um número, uma letra) só são redigidos entre aspas: fora delas, apagá-los estragaria
// o caminho do diagnóstico (índices de lista, nomes de campo) sem proteger um texto livre.
const MIN_BARE_REDACTION = 3;

/** As cadeias de uma resposta crua (valores de texto, em qualquer profundidade). */
function rawStrings(raw: unknown, out: Set<string> = new Set(), depth = 0): Set<string> {
  if (depth > 32) return out;
  if (typeof raw === 'string') {
    if (raw.length > 0) out.add(raw);
  } else if (Array.isArray(raw)) {
    for (const item of raw) rawStrings(item, out, depth + 1);
  } else if (raw !== null && typeof raw === 'object') {
    for (const value of Object.values(raw)) rawStrings(value, out, depth + 1);
  }
  return out;
}

/**
 * As formas em que o texto pode aparecer: cru, repr() e json.dumps() do Python e o segmento de
 * caminho do validador (JSON Pointer, com "~" → "~0" e "/" → "~1").
 */
function rawForms(raw: unknown): string[] {
  const forms = new Set<string>();
  for (const value of rawStrings(raw)) {
    const pyRepr = value.replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/\r/g, '\\r').replace(/\t/g, '\\t');
    const pointer = value.replace(/~/g, '~0').replace(/\//g, '~1');
    for (const form of [value, pyRepr, pyRepr.replace(/'/g, "\\'"), JSON.stringify(value).slice(1, -1), pointer]) forms.add(form);
  }
  return [...forms];
}

function isQuoted(text: string, start: number, end: number): boolean {
  const quote = text[start - 1];
  return (quote === '"' || quote === "'") && text[end] === quote;
}

/**
 * Redige no texto as formas da resposta crua numa passada só: os trechos sobrepostos ou vizinhos viram
 * um marcador só, e o marcador não é relido (a resposta "resposta" não o corrompe). Com cut, o texto é
 * um nome entre aspas que o validador cortou em 80 caracteres e "…" (contract/schemas.ts), e o começo
 * de uma forma no fim do corte também é redigido.
 */
function redactForms(text: string, forms: readonly string[], cut = false): string {
  const spans: Array<[number, number]> = [];
  for (const form of forms) {
    for (let at = text.indexOf(form); at !== -1; at = text.indexOf(form, at + 1)) {
      if (form.length >= MIN_BARE_REDACTION || isQuoted(text, at, at + form.length)) spans.push([at, at + form.length]);
    }
    if (cut) {
      // O corte fica antes do …" final; o nome começa depois da aspa inicial.
      const end = text.length - 2;
      for (let k = Math.min(form.length - 1, end - 1); k >= MIN_BARE_REDACTION; k -= 1) {
        if (text.startsWith(form.slice(0, k), end - k)) {
          spans.push([end - k, end]);
          break;
        }
      }
    }
  }
  if (spans.length === 0) return text;
  spans.sort((a, b) => a[0] - b[0]);
  let out = '';
  let done = 0;
  let [start, stop] = spans[0];
  for (const [s, e] of spans) {
    if (s <= stop) {
      stop = Math.max(stop, e);
      continue;
    }
    out += `${text.slice(done, start)}${REDACTED}`;
    done = stop;
    [start, stop] = [s, e];
  }
  return `${out}${text.slice(done, start)}${REDACTED}${text.slice(stop)}`;
}

// Um detail do validador é "<caminho>: <regra>" ou a contagem do que passou do limite
// (contract/schemas.ts). Na resposta da correção, o primeiro segmento do caminho é uma propriedade que
// o motor monta, e a regra e a contagem são texto do motor. Os segmentos seguintes e o nome entre aspas
// de uma propriedade não permitida são chaves que o código da questão gravou: só o format_errors tem
// estrutura no contrato, e um valor dele que não é texto passa como veio.
const HEAD = /^(?:\(corpo\)|\/[^/:]*)/;
const SEPARATOR = /: (?=falta |propriedade não permitida |deve |não |o valor )/;
const NOT_ALLOWED = /^propriedade não permitida "([\s\S]*)"$/;
const MORE_LINE = /^… e mais \d+ problema\(s\)\.$/;
// O nome cortado por quote() em contract/schemas.ts: 80 caracteres e "…".
const CUT_NAME_LENGTH = 81;

function redactDetail(detail: string, forms: readonly string[]): string {
  if (MORE_LINE.test(detail)) return detail;
  const head = HEAD.exec(detail)?.[0] ?? '';
  const parts = detail.split(SEPARATOR);
  // Fora do formato, ou com uma chave que traz o começo de uma regra (": deve "), não há como separar
  // a chave da regra: tudo depois do primeiro segmento é redigido.
  if (head === '' || parts.length !== 2) return `${head}${redactForms(detail.slice(head.length), forms)}`;
  const [pointer, rule] = parts;
  const name = NOT_ALLOWED.exec(rule)?.[1];
  const ruleText =
    name === undefined
      ? rule
      : `propriedade não permitida ${redactForms(`"${name}"`, forms, name.length === CUT_NAME_LENGTH && name.endsWith('…'))}`;
  return `${head}${redactForms(pointer.slice(head.length), forms)}: ${ruleText}`;
}

/**
 * Redige a resposta crua nos details de uma violação do contrato na correção, só nas chaves
 * que o código da questão gravou, para não apagar palavras do próprio diagnóstico ("deve ser texto" com
 * a resposta "texto", "/partial_scores" com a resposta "score").
 */
function redactDetails(details: readonly string[], raw: unknown): string[] {
  const forms = rawForms(raw);
  return forms.length === 0 ? [...details] : details.map((detail) => redactDetail(detail, forms));
}

/**
 * A recusa do checkData sem os valores que ela cita, que podem trazer a resposta crua: o novo e
 * o original de uma alteração ilegal e o de tipo errado ("data.feedback is not an object: <valor>").
 */
export function withoutCheckDataValues(message: string): string {
  return message
    .replace(/, new value: "[\s\S]*$/, '')
    .replace(/(data\.\w+ is not an? (?:integer|number|string|boolean|object)): [\s\S]*$/, '$1');
}

export interface DetailOptions {
  /** Fases parse e grade: sem os valores do checkData. */
  sensitive?: { raw: unknown };
}

/**
 * A mensagem do problema fatal e a saída da chamada que quebrou; depois, cada problema de saída no
 * console da mesma fase antes da quebra (consoleIssues, na ordem do PL), com a mensagem e a saída.
 */
export function technicalDetail(issue: CourseIssue, opts: DetailOptions = {}): string {
  let message = issue.message;
  if (opts.sensitive !== undefined && issue.kind === 'invalid_state') message = withoutCheckDataValues(message);
  let text = issue.outputBoth ? `${message}\n${issue.outputBoth}` : message;
  for (const printed of issue.consoleIssues ?? []) {
    text += `${text.endsWith('\n') ? '' : '\n'}${printed.message}`;
    if (printed.outputBoth) text += `\n${printed.outputBoth}`;
  }
  return truncateDetail(text);
}

export function brokenFromIssue(
  issue: CourseIssue,
  phase: ContractPhase,
  timeoutMs: number,
  opts: DetailOptions = {},
): BrokenReason {
  const code = codeOf(issue);
  const detail = technicalDetail(issue, opts);
  const reason: BrokenReason = { code, message: brokenMessage(code, phase, timeoutMs), phase };
  if (detail.length > 0) reason.technical_detail = detail;
  return reason;
}

export function contractViolation(
  phase: ContractPhase,
  details: readonly string[],
  timeoutMs: number,
  sensitive?: { raw: unknown },
): BrokenReason {
  const lines = sensitive === undefined ? details : redactDetails(details, sensitive.raw);
  const detail = truncateDetail(`A resposta montada não segue o contrato:\n${lines.join('\n')}`);
  return { code: 'ir_contract_violation', message: brokenMessage('ir_contract_violation', phase, timeoutMs), phase, technical_detail: detail };
}

// ---------------------------------------------------------------------------------------------
// Avisos
// ---------------------------------------------------------------------------------------------

export function consoleWarning(phase: ContractPhase): Warning {
  return {
    code: 'console_output',
    message: `O código da questão escreveu mensagens no console na fase de ${PHASE_NAMES[phase]}; a operação foi concluída normalmente.`,
    phase,
  };
}

/** Um aviso console_output por fase que imprimiu (os problemas não fatais de saída no console). */
export function consoleWarnings(issues: readonly CourseIssue[], phase: ContractPhase): Warning[] {
  return issues.some((i) => i.kind === 'console_output') ? [consoleWarning(phase)] : [];
}

/** Junta avisos, sem repetir o mesmo console_output de uma fase. */
export function mergeWarnings(target: Warning[], more: readonly Warning[]): void {
  for (const w of more) {
    if (w.code === 'console_output' && target.some((t) => t.code === 'console_output' && t.phase === w.phase)) continue;
    target.push(w);
  }
}
