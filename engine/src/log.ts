// Registros do motor: uma linha JSON por entrada. A stdout no servidor; a stderr na CLI, cuja
// stdout traz só o objeto {results}.
// Regra de privacidade: a resposta crua do aluno (texto livre) nunca vai para o registro. Por isso a
// entrada de pedido é montada por lista de campos permitidos (requestLogEntry), que só copia valores
// de enumerações do contrato, o qid e números; nada de technical_detail, saída do console, mensagens
// ou corpos. O errorLogger do CodeCallerNative registra só um evento fixo (codeCallerErrorLogger).
import {
  BATCH_OPS,
  BROKEN_REASON_CODES,
  CONTRACT_PHASES,
  ERROR_CODES,
  UNSUPPORTED_REASON_CODES,
  WARNING_CODES,
} from './contract/types.ts';

export type LogFn = (entry: Record<string, unknown>) => void;

/** Escreve cada entrada como uma linha JSON, com a hora; nunca lança (o registro não derruba o pedido). */
export function jsonLineLogger(stream: NodeJS.WritableStream): LogFn {
  return (entry) => {
    const time = new Date().toISOString();
    let line: string;
    try {
      line = JSON.stringify({ time, ...entry });
    } catch {
      // BigInt ou referência circular: registra o fato, sem o conteúdo.
      line = JSON.stringify({ time, level: 'error', event: 'log_serialization_error' });
    }
    try {
      stream.write(line + '\n');
    } catch {
      // stream fechado: o registro se perde, o pedido segue
    }
  };
}

/**
 * errorLogger para o CodeCallerNative do PL. [F] code-caller-native.js (a193f72), _logError: repassa
 * uma mensagem que pode trazer a saída do processo filho e o errData com o lastCallData (os argumentos
 * da chamada, com as respostas cruas). Os argumentos são ignorados de propósito.
 */
export function codeCallerErrorLogger(log: LogFn): (...args: unknown[]) => void {
  return () => log({ level: 'error', event: 'code_caller_error' });
}

/**
 * Quem escreve a entrada de pedido: o Engine (engine.ts, operation), e, para as respostas que não
 * chegam ao núcleo, a CLI (item inválido do lote) e o servidor HTTP (http/server.ts), estes sem corpo.
 */
export interface RequestLogInput {
  /** Operação: health, inspect, create_variant ou grade; outro texto vira "unknown". */
  op: string;
  /** Corpo do pedido (não confiável): só question_ref.qid é lido. undefined quando não há corpo. */
  request: unknown;
  status: number;
  /** Corpo da resposta: só enumerações do contrato são lidas. */
  response: unknown;
  ms: number;
}

// Só valores das enumerações de engine-api.schema.json são copiados da resposta.
const OUTCOMES: ReadonlySet<string> = new Set(['ok', 'broken', 'unsupported', 'supported']);
const ERROR_CODE_SET: ReadonlySet<string> = new Set(ERROR_CODES);
const BROKEN_CODE_SET: ReadonlySet<string> = new Set(BROKEN_REASON_CODES);
const UNSUPPORTED_CODE_SET: ReadonlySet<string> = new Set(UNSUPPORTED_REASON_CODES);
const WARNING_CODE_SET: ReadonlySet<string> = new Set(WARNING_CODES);
const PHASE_SET: ReadonlySet<string> = new Set(CONTRACT_PHASES);
const OP_SET: ReadonlySet<string> = new Set(BATCH_OPS);
// O formato de question_ref.qid (caracteres e tamanho); outro texto não é copiado.
const QID_PATTERN = /^[A-Za-z0-9_./-]{1,512}$/;

/**
 * A entrada de registro de um pedido: op, qid, status, outcome e ms, mais os códigos do motivo, a
 * fase da quebra e os códigos dos avisos. qid é "" quando o pedido não traz um qid no formato.
 */
export function requestLogEntry(input: RequestLogInput): Record<string, unknown> {
  const response = asRecord(input.response);
  const entry: Record<string, unknown> = {
    level: input.status >= 500 ? 'error' : 'info',
    event: 'request',
    op: OP_SET.has(input.op) ? input.op : 'unknown',
    qid: qidOf(input.request),
    status: input.status,
    outcome: outcomeOf(response),
    ms: Number.isFinite(input.ms) && input.ms > 0 ? Math.round(input.ms) : 0,
  };

  const reason = asRecord(response?.reason);
  if (reason) {
    const code = known(reason.code, BROKEN_CODE_SET);
    if (code) entry.reason_code = code;
    const phase = known(reason.phase, PHASE_SET);
    if (phase) entry.phase = phase;
  }
  const reasons = response?.reasons;
  if (Array.isArray(reasons)) {
    entry.reason_codes = reasons.flatMap((r: unknown) => known(asRecord(r)?.code, UNSUPPORTED_CODE_SET) ?? []);
  }
  const warnings = response?.warnings;
  if (Array.isArray(warnings)) {
    entry.warning_codes = warnings.flatMap((w: unknown) => known(asRecord(w)?.code, WARNING_CODE_SET) ?? []);
  }
  const valid = response?.valid;
  if (typeof valid === 'boolean') entry.valid = valid;
  return entry;
}

function outcomeOf(response: Record<string, unknown> | undefined): string {
  const outcome = known(response?.outcome, OUTCOMES);
  if (outcome) return outcome;
  const error = known(asRecord(response?.error)?.code, ERROR_CODE_SET);
  if (error) return error;
  if (response?.status === 'ready') return 'ready';
  return 'unknown';
}

function qidOf(request: unknown): string {
  const qid = asRecord(asRecord(request)?.question_ref)?.qid;
  return typeof qid === 'string' && QID_PATTERN.test(qid) ? qid : '';
}

function known(value: unknown, allowed: ReadonlySet<string>): string | undefined {
  return typeof value === 'string' && allowed.has(value) ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
}
