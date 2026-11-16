// Criação de variante.
// Espelha generate() e prepare() do freeform.js, cada fase num executor recém-reiniciado:
// 1. veredito estático: "não suportada" responde sem executar nenhum código da questão;
// 2. generate (só o server.py);
// 3. checagem dinâmica: o template (mustache + markdown do PL) com os dados do generate, classificado
//    pela mesma regra, antes do prepare, que rodaria o código de todo elemento do HTML;
// 4. prepare (elementos e server.py);
// 5. a mesma checagem com os dados que o prepare devolveu: o prepare do server.py roda depois do dos
//    elementos e pode mudar params, e o template das fases seguintes (render, parse, grade, file) passa
//    a ter as tags que ele injetar; o PL rodaria o código delas a partir do render;
// 6. IR (render, fragmentos e mídias; ir/build.ts), que antes de qualquer render classifica também os
//    templates dos painéis, com os dados do render, e as tags que os elementos suportados escrevem cru
//    na saída do render (core/emitted-tags.ts), que o PL processa de novo; e, antes da primeira chamada
//    da fase file, o template de cada mídia gerada, com o filename nos dados;
// 7. o tamanho do snapshot: um que não caberia num pedido de correção (core/snapshot-size.ts) dá
//    "questão quebrada" (ir_contract_violation, fase render), e não "ok".
// A saída dos elementos só conta aqui, com os dados do render: é o render que a processa, e o prepare
// percorre só a árvore do HTML.
import path from 'node:path';

import type {
  BrokenReason,
  ContractPhase,
  CreateVariantRequest,
  CreateVariantResponse,
  PreferenceValue,
  Snapshot,
  UnsupportedReason,
  Warning,
} from '../contract/types.ts';
import { IR_VERSION } from '../contract/types.ts';
import { InvalidRequestError } from '../errors.ts';
import { buildIr } from '../ir/build.ts';
import { contextOptions, execTemplate, processQuestion, type PhaseData, type QuestionContext } from '../pl/phases.ts';
import type { OperationDeps } from './deps.ts';
import { staticVerdict } from './inspect.ts';
import { brokenFromIssue, consoleWarnings, firstFatal, mergeWarnings } from './outcome.ts';
import { loadQuestion, questionContext, type LoadedQuestion } from './question.ts';
import { parseSeed } from './reference.ts';
import { oversizedSnapshot } from './snapshot-size.ts';
import { elementReasons, scanQuestionHtml } from './support.ts';

/** Um CreateVariantResponse ainda sem a conferência do contrato, com a última fase executada. */
export interface VariantOutcome {
  response: CreateVariantResponse;
  /** A fase em que uma violação do contrato é atribuída (a última executada). */
  phase: ContractPhase;
}

type PreferenceSchema = Record<string, { type?: unknown; default?: unknown; enum?: unknown }>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function preferenceSchema(question: LoadedQuestion): PreferenceSchema {
  const schema = question.info.data?.preferences;
  return isRecord(schema) ? (schema as PreferenceSchema) : {};
}

/**
 * As preferências da variante: as padrão do info.json (extractDefaultPreferences do PL), sobrepostas
 * pelas pedidas (lib/question-variant.js). Uma chave fora do esquema, um valor do tipo errado ou fora
 * do enum é pedido inválido.
 */
export function resolvePreferences(
  question: LoadedQuestion,
  requested: Record<string, PreferenceValue> | undefined,
): Record<string, unknown> {
  const schema = preferenceSchema(question);
  const defaults: Record<string, unknown> = {};
  for (const [key, spec] of Object.entries(schema)) defaults[key] = isRecord(spec) ? spec.default : undefined;
  const details: string[] = [];
  for (const [key, value] of Object.entries(requested ?? {})) {
    const where = `/preferences/${key.length > 80 ? `${key.slice(0, 80)}…` : key}`;
    if (!Object.hasOwn(schema, key) || !isRecord(schema[key])) {
      details.push(`${where}: a preferência não existe no esquema de preferências da questão`);
      continue;
    }
    const spec = schema[key];
    if (typeof value !== spec.type) {
      details.push(`${where}: deve ser do tipo ${String(spec.type)}`);
      continue;
    }
    if (Array.isArray(spec.enum) && !spec.enum.includes(value)) {
      details.push(`${where}: deve ser um destes valores: ${spec.enum.map((v) => JSON.stringify(v)).join(', ')}`);
    }
  }
  if (details.length > 0) {
    throw new InvalidRequestError('Pedido inválido: as preferências pedidas não seguem o esquema de preferências da questão.', { details });
  }
  return { ...defaults, ...(requested ?? {}) };
}

function broken(reason: BrokenReason, phase: ContractPhase): VariantOutcome {
  // Numa quebra não há aviso: console_output só vale quando a operação conclui.
  return { response: { outcome: 'broken', reason, warnings: [] }, phase };
}

function unsupported(reasons: UnsupportedReason[], phase: ContractPhase): VariantOutcome {
  return { response: { outcome: 'unsupported', reasons }, phase };
}

/**
 * Checagem dinâmica: o question.html pelo template do PL com os dados da fase, classificado pela
 * mesma regra da inspeção. Um erro de template não dá motivo aqui: ele quebra a fase seguinte (prepare
 * ou render), como no PL (processQuestionHtml), antes de qualquer Python.
 */
async function templateReasons(context: QuestionContext, data: PhaseData): Promise<UnsupportedReason[]> {
  let templated: string;
  try {
    templated = await execTemplate(path.join(context.questionDir, 'question.html'), data);
  } catch {
    return [];
  }
  return elementReasons(scanQuestionHtml(templated, context.coreElements, context.courseElements));
}

export async function createVariant(deps: OperationDeps, request: CreateVariantRequest): Promise<VariantOutcome> {
  const variantSeed = parseSeed(request.seed);
  const question = await loadQuestion(deps.reposRoot, request.question_ref);

  // 1. Veredito estático: nada da questão é executado.
  const verdict = staticVerdict(question);
  if (verdict.reasons.length > 0) return { response: { outcome: 'unsupported', reasons: verdict.reasons }, phase: 'generate' };

  const preferences = resolvePreferences(question, request.preferences);
  const context = questionContext(question);
  const timeoutMs = deps.questionTimeoutMs;
  const warnings: Warning[] = [];

  // 2. generate (freeform.js generate()).
  const generateData: PhaseData = {
    params: {},
    correct_answers: {},
    variant_seed: variantSeed,
    options: { ...contextOptions(context), user: null, group: null },
    preferences,
  };
  const generated = await deps.executors.withCodeCaller(context.courseDir, (cc) => processQuestion('generate', cc, generateData, context));
  const generateFatal = firstFatal(generated.courseIssues);
  if (generateFatal !== undefined) return broken(brokenFromIssue(generateFatal, 'generate', timeoutMs), 'generate');
  mergeWarnings(warnings, consoleWarnings(generated.courseIssues, 'generate'));
  const params = generated.data.params;
  const correctAnswers = generated.data.correct_answers;

  // 3. Checagem dinâmica, com os dados que o prepare recebe (freeform.js prepare()).
  const prepareData = (phaseParams: unknown, phaseCorrectAnswers: unknown): PhaseData => ({
    params: structuredClone(phaseParams ?? {}),
    correct_answers: structuredClone(phaseCorrectAnswers ?? {}),
    variant_seed: variantSeed,
    options: { ...contextOptions(context), user: null, group: null },
    preferences,
    answers_names: {},
  });
  const generatedReasons = await templateReasons(context, prepareData(params, correctAnswers));
  if (generatedReasons.length > 0) return unsupported(generatedReasons, 'generate');

  // 4. prepare.
  const prepared = await deps.executors.withCodeCaller(context.courseDir, (cc) =>
    processQuestion('prepare', cc, prepareData(params, correctAnswers), context),
  );
  const prepareFatal = firstFatal(prepared.courseIssues);
  if (prepareFatal !== undefined) return broken(brokenFromIssue(prepareFatal, 'prepare', timeoutMs), 'prepare');
  mergeWarnings(warnings, consoleWarnings(prepared.courseIssues, 'prepare'));

  // 5. A mesma checagem com os dados que o prepare devolveu, antes de qualquer render.
  const preparedReasons = await templateReasons(context, prepareData(prepared.data.params, prepared.data.correct_answers));
  if (preparedReasons.length > 0) return unsupported(preparedReasons, 'prepare');

  const snapshot: Snapshot = {
    seed: request.seed,
    params: prepared.data.params as Snapshot['params'],
    correct_answers: prepared.data.correct_answers as Snapshot['correct_answers'],
    preferences: preferences as Snapshot['preferences'],
    engine_version: deps.engineVersion,
    pl_commit: deps.plCommit,
    ir_version: IR_VERSION,
  };

  // 6. IR.
  const showCorrectAnswer = question.info.data?.showCorrectAnswer !== false;
  const built = await buildIr({
    executors: deps.executors,
    context,
    questionDir: question.questionDir,
    courseRoot: question.courseRoot,
    showCorrectAnswer,
    variant: { variantSeed, params: snapshot.params, correctAnswers: snapshot.correct_answers, preferences },
    questionTimeoutMs: timeoutMs,
  });
  if (!built.ok) {
    // Fora do subconjunto pelos templates do render ou pela saída dos elementos (nenhum render rodou, e a
    // última fase é o prepare), ou pelos templates da fase file (depois do render).
    if ('unsupported' in built) return unsupported(built.unsupported, built.phase);
    return broken(built.reason, built.reason.phase);
  }

  // 7. Um snapshot que o núcleo não conseguiria devolver na correção: a variante nunca seria corrigida.
  const oversized = oversizedSnapshot(snapshot);
  if (oversized !== undefined) return broken(oversized, 'render');
  mergeWarnings(warnings, built.warnings);

  return { response: { outcome: 'ok', snapshot, ir: built.ir, warnings }, phase: 'render' };
}
