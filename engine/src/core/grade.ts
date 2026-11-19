// Correção, espelhando parse() e
// grade() do freeform.js e a gravação de lib/grading.js e models/grading-job.js do PL:
// 1. parse (elementos e server.py) com submitted_answers e raw_submitted_answers iguais à resposta
//    crua; com format_errors, a resposta é inválida e o grade não roda;
// 2. grade com os dados do parse: o grade dos elementos, o score calculado nesse ponto
//    (processQuestionHtml) e o grade do server.py, que pode sobrescrevê-lo ou acrescentar
//    format_errors (e então a resposta fica inválida, com score e partial_scores null).
// Entre as fases, os dados passam pelo banco como no PL (jsonb.ts): o parse relê do banco só a variante
// (params, correct_answers e preferences do snapshot), e a resposta crua vem na ordem recebida, como o
// formulário; o grade relê a submissão gravada depois do parse; e a resposta traz a submissão como o PL
// a grava e a relê. processQuestion faz a leitura de cada fase, e readStored, a da resposta.
// A correção não reavalia o suporte, e um snapshot de outra versão é corrigido e marcado.
import * as cheerio from 'cheerio';

import type {
  ContractPhase,
  FormatError,
  GradeRequest,
  GradeResponse,
  JsonObject,
  VersionMismatch,
  Warning,
} from '../contract/types.ts';
import { readStored } from '../pl/jsonb.ts';
import { contextOptions, processQuestion, type PhaseData } from '../pl/phases.ts';
import type { OperationDeps } from './deps.ts';
import { brokenFromIssue, consoleWarnings, firstFatal, mergeWarnings } from './outcome.ts';
import { loadQuestion, questionContext } from './question.ts';
import { parseSeed } from './reference.ts';

export interface GradeOutcome {
  response: GradeResponse;
  /** A última fase executada: a de uma violação do contrato. */
  phase: ContractPhase;
  /** A resposta crua, para redigir o diagnóstico de uma violação do contrato. */
  raw: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function hasKeys(value: unknown): boolean {
  return value !== null && typeof value === 'object' && Object.keys(value).length > 0;
}

/** O texto do HTML do erro: conteúdo textual, entidades decodificadas, espaços colapsados, aparado. */
export function formatErrorText(html: string): string {
  return cheerio.load(html, null, false).root().text().replace(/\s+/g, ' ').trim();
}

/**
 * format_errors do contrato: cada mensagem do PL em html (sem alteração) e em text. Um valor que não é
 * texto fica como veio, e a conferência do contrato o recusa (broken/ir_contract_violation).
 */
function formatErrors(value: unknown): Record<string, FormatError> {
  if (!isRecord(value)) return value as Record<string, FormatError>;
  const out: Record<string, unknown> = {};
  for (const [name, message] of Object.entries(value)) {
    out[name] = typeof message === 'string' ? { html: message, text: formatErrorText(message) } : message;
  }
  return out as Record<string, FormatError>;
}

export async function gradeSubmission(deps: OperationDeps, request: GradeRequest): Promise<GradeOutcome> {
  const snapshot = request.snapshot;
  const raw = request.raw_submitted_answers;
  const variantSeed = parseSeed(snapshot.seed);
  const question = await loadQuestion(deps.reposRoot, request.question_ref);
  const context = questionContext(question);
  const timeoutMs = deps.questionTimeoutMs;
  const sensitive = { sensitive: { raw } };
  const version_mismatch: VersionMismatch = {
    engine: snapshot.engine_version !== deps.engineVersion,
    pl: snapshot.pl_commit !== deps.plCommit,
  };
  const options = (): PhaseData => ({ ...contextOptions(context), user: null, group: null });
  const warnings: Warning[] = [];

  // 1. parse (freeform.js parse(); a submissão nova tem feedback e format_errors vazios e gradable true;
  //    a variante vem do banco, e a resposta crua, do formulário).
  const parseData: PhaseData = {
    params: structuredClone(snapshot.params ?? {}),
    correct_answers: structuredClone(snapshot.correct_answers ?? {}),
    submitted_answers: structuredClone(raw),
    feedback: {},
    format_errors: {},
    variant_seed: variantSeed,
    options: options(),
    preferences: structuredClone(snapshot.preferences),
    raw_submitted_answers: structuredClone(raw),
    gradable: true,
  };
  const parsed = await deps.executors.withCodeCaller(context.courseDir, (cc) => processQuestion('parse', cc, parseData, context));
  const parseFatal = firstFatal(parsed.courseIssues);
  if (parseFatal !== undefined) {
    return {
      response: { outcome: 'broken', reason: brokenFromIssue(parseFatal, 'parse', timeoutMs, sensitive), version_mismatch, warnings: [] },
      phase: 'parse',
      raw,
    };
  }
  mergeWarnings(warnings, consoleWarnings(parsed.courseIssues, 'parse'));
  const afterParse = parsed.data;
  if (hasKeys(afterParse.format_errors)) {
    // gradable false: a submissão fica gravada sem correção (lib/grading.js).
    return {
      response: {
        outcome: 'ok',
        valid: false,
        format_errors: formatErrors(readStored(afterParse.format_errors)),
        submitted_answers: readStored(afterParse.submitted_answers) as JsonObject,
        partial_scores: null,
        score: null,
        feedback: readStored(afterParse.feedback) as JsonObject,
        params: readStored(afterParse.params) as JsonObject,
        correct_answers: readStored(afterParse.correct_answers) as JsonObject,
        version_mismatch,
        warnings,
      },
      phase: 'parse',
      raw,
    };
  }

  // 2. grade (freeform.js grade(), com params e gabarito da submissão, que o parse pode ter mudado; a
  //    submissão e a variante vêm do banco).
  const gradeData: PhaseData = {
    params: afterParse.params ?? {},
    correct_answers: afterParse.correct_answers ?? {},
    submitted_answers: afterParse.submitted_answers ?? {},
    format_errors: afterParse.format_errors ?? {},
    partial_scores: {},
    score: 0,
    feedback: afterParse.feedback ?? {},
    variant_seed: variantSeed,
    options: options(),
    preferences: structuredClone(snapshot.preferences),
    raw_submitted_answers: afterParse.raw_submitted_answers ?? {},
    gradable: true,
  };
  const graded = await deps.executors.withCodeCaller(context.courseDir, (cc) => processQuestion('grade', cc, gradeData, context));
  const gradeFatal = firstFatal(graded.courseIssues);
  if (gradeFatal !== undefined) {
    return {
      response: { outcome: 'broken', reason: brokenFromIssue(gradeFatal, 'grade', timeoutMs, sensitive), version_mismatch, warnings: [] },
      phase: 'grade',
      raw,
    };
  }
  mergeWarnings(warnings, consoleWarnings(graded.courseIssues, 'grade'));
  const result = graded.data;
  // Com format_errors no fim do grade, score e partial_scores viram null (models/grading-job.js).
  const valid = !hasKeys(result.format_errors);
  return {
    response: {
      outcome: 'ok',
      valid,
      format_errors: formatErrors(readStored(result.format_errors)),
      submitted_answers: readStored(result.submitted_answers) as JsonObject,
      partial_scores: valid ? (readStored(result.partial_scores) as JsonObject) : null,
      score: valid ? (result.score as number) : null,
      feedback: readStored(result.feedback) as JsonObject,
      params: readStored(result.params) as JsonObject,
      correct_answers: readStored(result.correct_answers) as JsonObject,
      version_mismatch,
      warnings,
    },
    phase: 'grade',
    raw,
  };
}
