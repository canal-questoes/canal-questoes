// Fases do PL, espelhando processQuestionPhase, processQuestionHtml, processQuestionServer,
// execPythonServer, execTemplate e processQuestion de dist/question-servers/freeform.js (PL a193f72).
// O Python (question_phases.py, elementos e server.py) roda sem alteração
// pelo CodeCallerNative do PL; o template passa pelo mustache e pelo markdown.processQuestion do PL.
//
// Diferenças deliberadas, que não mudam o resultado das fases:
// - os problemas (courseIssues) guardam o tipo e o erro original, para o motor classificar a quebra
//   (question_code_error, timeout, invalid_data) sem ler texto;
// - um retorno do server.py que não é objeto vira problema fatal, em vez da exceção que o checkData
//   do PL lançaria;
// - processElements roda a fase dos elementos sobre um HTML dado (a IR troca os elementos de resposta
//   por marcadores antes do render). O template é processado por quem chama;
// - o primeiro problema fatal de processQuestion leva os problemas de saída no console registrados
//   antes dele na mesma fase (consoleIssues). O PL grava todos os problemas da fase
//   (writeCourseIssues), e o professor vê a saída ao lado da quebra; o motor só leva à resposta o
//   primeiro fatal, e é por ele que essa saída chega ao technical_detail (core/outcome.ts);
// - o execTemplate lê o question.html pela leitura segura de text-file.ts: o que não é arquivo
//   regular ou passa do limite falha como um erro do fs.readFile do PL, com um erro de template.
//
// processQuestion e processElements recebem os dados como quem chama os monta e os passam adiante como
// o PL os relê do banco na fase (fromDatabase, jsonb.ts): entre as fases, o PL grava a variante e a
// submissão em colunas jsonb e as relê de lá, com as chaves na ordem do jsonb.
import fs from 'node:fs/promises';
import path from 'node:path';

import mustache from 'mustache';

import { FunctionMissingError, processQuestionMarkdown, type CodeCaller } from './bindings.ts';
import { checkData, type Phase } from './check-data.ts';
import { phaseElements, type ElementMap, type ExtensionMap } from './elements.ts';
import { fromDatabase } from './jsonb.ts';
import { computeScore } from './score.ts';
import { readTextFile, TEXT_FILE_MAX_BYTES, type TextFileRead } from './text-file.ts';

export type { Phase } from './check-data.ts';

export type PhaseData = Record<string, unknown>;

export type IssueKind = 'call_error' | 'invalid_state' | 'template_error' | 'console_output' | 'file_error';

/** Um problema de uma fase (CourseIssueError do freeform.js). */
export interface CourseIssue {
  /** A mensagem, como o PL a monta. */
  message: string;
  fatal: boolean;
  kind: IssueKind;
  /** A saída do Python (err.data.outputBoth, ou a saída de uma chamada que concluiu). */
  outputBoth?: string;
  /** O erro original da chamada ao executor (nunca serializado: err.data traz o lastCallData). */
  cause?: unknown;
  /** Num problema fatal: os problemas de saída no console da mesma fase antes dele, na ordem do PL. */
  consoleIssues?: readonly CourseIssue[];
}

/** O contexto da questão (getContext do freeform.js, no modo native). */
export interface QuestionContext {
  /** question.directory: o qid. */
  qid: string;
  /** course_dir = course_dir_host (modo native). */
  courseDir: string;
  /** question_dir = question_dir_host = <courseDir>/questions/<qid>. */
  questionDir: string;
  coreElements: ElementMap;
  courseElements: ElementMap;
  courseElementExtensions: ExtensionMap;
  /** question.partial_credit (sync/fromDisk/questions.js: partialCredit ?? type === "v3"). */
  partialCredit: boolean;
}

export interface PhaseResult {
  courseIssues: CourseIssue[];
  data: PhaseData;
  html: string;
  fileData: Buffer;
}

function issue(message: string, kind: IssueKind, fatal: boolean, extra: Partial<CourseIssue> = {}): CourseIssue {
  return { message, kind, fatal, ...extra };
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function errorString(err: unknown): string {
  return err instanceof Error ? err.toString() : String(err);
}

/** err.data.outputBoth do CodeCallerNative, sem tocar no resto de err.data (lastCallData). */
function outputBothOf(err: unknown): string | undefined {
  const data = (err as { data?: unknown } | null)?.data;
  if (data === null || typeof data !== 'object') return undefined;
  const output = (data as { outputBoth?: unknown }).outputBoth;
  return typeof output === 'string' && output.length > 0 ? output : undefined;
}

function isRecord(value: unknown): value is PhaseData {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Os caminhos que o PL põe em data.options em toda fase (getContextOptions do freeform.js). */
export function contextOptions(context: QuestionContext): PhaseData {
  return {
    question_path: context.questionDir,
    client_files_question_path: path.join(context.questionDir, 'clientFilesQuestion'),
    client_files_course_path: path.join(context.courseDir, 'clientFilesCourse'),
    server_files_course_path: path.join(context.courseDir, 'serverFilesCourse'),
    course_extensions_path: path.join(context.courseDir, 'elementExtensions'),
  };
}

/** O motor não lê o arquivo: não é arquivo regular depois de seguir os links, ou passa do limite. */
class UnreadableFileError extends Error {
  override name = 'UnreadableFileError';
  constructor(read: Exclude<TextFileRead, { status: 'ok' }>) {
    super(
      read.status === 'too_large'
        ? `o arquivo passa do limite de ${TEXT_FILE_MAX_BYTES} bytes, e o motor não o lê`
        : `o caminho não é um arquivo regular${read.directory ? ' (é um diretório)' : ''}, e o motor não o lê`,
    );
  }
}

/**
 * execTemplate do freeform.js: mustache do PL e depois o markdown.processQuestion do PL. O arquivo vem
 * da leitura segura (readTextFile), que decodifica como o fs.readFile(…, 'utf8') do PL; os erros do
 * sistema de arquivos (ENOENT etc.) sobem como no PL.
 */
export async function execTemplate(htmlFilename: string, data: PhaseData): Promise<string> {
  const read = await readTextFile(htmlFilename);
  if (read.status !== 'ok') throw new UnreadableFileError(read);
  const html = mustache.render(read.text, data);
  return processQuestionMarkdown(html);
}

/** processQuestionPhase do freeform.js: a fase dos elementos (question_phases.py) sobre o HTML dado. */
export async function processQuestionPhase(
  phase: Phase,
  codeCaller: CodeCaller,
  data: PhaseData,
  context: QuestionContext,
  html: string,
): Promise<PhaseResult> {
  const pythonContext = {
    html,
    // Os elementos do curso têm precedência sobre os core.
    elements: phaseElements(context.coreElements, context.courseElements),
    element_extensions: context.courseElementExtensions,
    course_path: context.courseDir,
  };
  const courseIssues: CourseIssue[] = [];
  let result: Record<string, unknown> | null = null;
  let output: string | null = null;
  try {
    const res = await codeCaller.call('question', context.qid, 'question.html', phase, [pythonContext, data]);
    result = isRecord(res.result) ? res.result : null;
    output = res.output;
  } catch (err) {
    courseIssues.push(issue(errorMessage(err), 'call_error', true, { outputBoth: outputBothOf(err), cause: err }));
  }
  if ((output?.length ?? 0) > 0) {
    courseIssues.push(issue(`output logged on console during ${phase}()`, 'console_output', false, { outputBoth: output ?? '' }));
  }
  const resultData = isRecord(result?.data) ? result.data : data;
  const resultHtml = typeof result?.html === 'string' ? result.html : '';
  const resultFile = typeof result?.file === 'string' ? result.file : '';
  return { courseIssues, data: resultData, html: resultHtml, fileData: Buffer.from(resultFile, 'base64') };
}

function emptyResult(courseIssues: CourseIssue[], data: PhaseData): PhaseResult {
  return { courseIssues, data, html: '', fileData: Buffer.from('') };
}

/**
 * processQuestionHtml do freeform.js: checkData antes da fase, o template do question.html, a fase dos
 * elementos e, no grade e no test, o score calculado logo depois do grade dos elementos (antes do
 * grade do server.py).
 */
export async function processQuestionHtml(
  phase: Phase,
  codeCaller: CodeCaller,
  data: PhaseData,
  context: QuestionContext,
): Promise<PhaseResult> {
  const checkErr = checkData(data, data, phase);
  if (checkErr) {
    return emptyResult([issue(`Invalid state before ${phase}(): ${checkErr}`, 'invalid_state', true)], data);
  }
  const htmlFilename = path.join(context.questionDir, 'question.html');
  let html: string;
  try {
    html = await execTemplate(htmlFilename, data);
  } catch (err) {
    return emptyResult([issue(`${htmlFilename}: ${errorString(err)}`, 'template_error', true, { cause: err })], data);
  }
  const result = await processQuestionPhase(phase, codeCaller, data, context, html);
  if (phase === 'grade' || phase === 'test') {
    // Como o "partial_scores ?? {}" do PL: computeScore aplica o mesmo Object.values.
    result.data.score = computeScore(result.data.partial_scores as Record<string, unknown> | undefined, context.partialCredit);
  }
  return result;
}

/**
 * A fase dos elementos sobre um HTML já processado pelo template: o checkData de processQuestionHtml
 * e processQuestionPhase, sem o score (só usada no render da IR). Os dados entram como o PL os
 * relê do banco no render (fromDatabase).
 */
export async function processElements(
  phase: 'render',
  codeCaller: CodeCaller,
  callerData: PhaseData,
  context: QuestionContext,
  html: string,
): Promise<PhaseResult> {
  const data = fromDatabase(phase, callerData);
  const checkErr = checkData(data, data, phase);
  if (checkErr) {
    return emptyResult([issue(`Invalid state before ${phase}(): ${checkErr}`, 'invalid_state', true)], data);
  }
  return processQuestionPhase(phase, codeCaller, data, context, html);
}

function defaultServerRet(phase: Phase, data: PhaseData, html: string): unknown {
  if (phase === 'render') return html;
  if (phase === 'file') return '';
  return data;
}

/** execPythonServer do freeform.js. */
async function execPythonServer(
  codeCaller: CodeCaller,
  phase: Phase,
  data: PhaseData,
  html: string,
  context: QuestionContext,
): Promise<{ result: unknown; output: string }> {
  const pythonArgs: unknown[] = [data];
  if (phase === 'render') pythonArgs.push(html);
  const fullFilename = path.join(context.questionDir, 'server.py');
  try {
    await fs.access(fullFilename, fs.constants.R_OK);
  } catch {
    return { result: defaultServerRet(phase, data, html), output: '' };
  }
  try {
    const { result, output } = await codeCaller.call('question', context.qid, 'server', phase, pythonArgs);
    return { result, output };
  } catch (err) {
    if (err instanceof FunctionMissingError) return { result: defaultServerRet(phase, data, html), output: '' };
    throw err;
  }
}

/** processQuestionServer do freeform.js: o server.py da fase, com checkData antes e depois. */
export async function processQuestionServer(
  phase: Phase,
  codeCaller: CodeCaller,
  data: PhaseData,
  html: string,
  fileData: Buffer,
  context: QuestionContext,
): Promise<PhaseResult> {
  const courseIssues: CourseIssue[] = [];
  const origData = structuredClone(data);
  const checkErrBefore = checkData(data, origData, phase);
  if (checkErrBefore) {
    courseIssues.push(issue(`Invalid state before calling server ${phase}(): ${checkErrBefore}`, 'invalid_state', true));
    return emptyResult(courseIssues, data);
  }
  const serverFile = path.join(context.questionDir, 'server.py');
  let result: unknown;
  let output: string;
  try {
    ({ result, output } = await execPythonServer(codeCaller, phase, data, html, context));
  } catch (err) {
    courseIssues.push(
      issue(`${serverFile}: Error calling ${phase}(): ${errorString(err)}`, 'call_error', true, { outputBoth: outputBothOf(err), cause: err }),
    );
    return { courseIssues, data, html, fileData };
  }
  if (typeof output === 'string' && output.length > 0) {
    courseIssues.push(issue(`${serverFile}: output logged on console`, 'console_output', false, { outputBoth: output }));
  }
  let newData = data;
  if (phase === 'render') {
    html = typeof result === 'string' ? result : String(result);
  } else if (phase === 'file') {
    // O retorno do file() vem em base64 (zygote.py); Buffer.from nunca falha com base64.
    const buf = Buffer.from(typeof result === 'string' ? result : '', 'base64');
    if (buf.length > 0) {
      if (fileData.length > 0) {
        courseIssues.push(issue(`${serverFile}: Error calling ${phase}(): attempting to overwrite non-empty fileData`, 'file_error', true));
        return { courseIssues, data, html, fileData };
      }
      fileData = Buffer.from(buf);
    }
  } else {
    if (!isRecord(result)) {
      courseIssues.push(issue(`${serverFile}: Invalid state after ${phase}(): "data" is not an object`, 'invalid_state', true));
      return { courseIssues, data: origData, html, fileData };
    }
    newData = result;
  }
  const checkErrAfter = checkData(newData, origData, phase);
  if (checkErrAfter) {
    courseIssues.push(issue(`${serverFile}: Invalid state after ${phase}(): ${checkErrAfter}`, 'invalid_state', true));
    // Os dados novos não passaram no checkData: volta o original, como no PL.
    return { courseIssues, data: origData, html, fileData };
  }
  return { courseIssues, data: newData, html, fileData };
}

/**
 * O primeiro problema fatal com os problemas de saída no console registrados antes dele na fase: a saída
 * da chamada do server.py cujo retorno o checkData recusou, ou a da fase dos elementos antes de uma
 * quebra do server.py. Os problemas continuam na lista, na ordem do PL.
 */
function withConsoleIssues(courseIssues: CourseIssue[]): CourseIssue[] {
  const index = courseIssues.findIndex((i) => i.fatal);
  if (index <= 0) return courseIssues;
  const printed = courseIssues.slice(0, index).filter((i) => i.kind === 'console_output');
  if (printed.length === 0) return courseIssues;
  const out = [...courseIssues];
  out[index] = { ...courseIssues[index], consoleIssues: printed };
  return out;
}

/**
 * processQuestion do freeform.js: o generate só no server.py; as demais fases, elementos e server.py.
 * Os dados entram como o PL os relê do banco na fase (fromDatabase).
 */
export async function processQuestion(
  phase: Phase,
  codeCaller: CodeCaller,
  callerData: PhaseData,
  context: QuestionContext,
): Promise<PhaseResult> {
  const data = fromDatabase(phase, callerData);
  if (phase === 'generate') {
    const result = await processQuestionServer(phase, codeCaller, data, '', Buffer.from(''), context);
    return { ...result, courseIssues: withConsoleIssues(result.courseIssues) };
  }
  const htmlResult = await processQuestionHtml(phase, codeCaller, data, context);
  if (htmlResult.courseIssues.some((i) => i.fatal)) {
    return { ...htmlResult, courseIssues: withConsoleIssues(htmlResult.courseIssues), data };
  }
  const serverResult = await processQuestionServer(phase, codeCaller, htmlResult.data, htmlResult.html, htmlResult.fileData, context);
  return {
    courseIssues: withConsoleIssues([...htmlResult.courseIssues, ...serverResult.courseIssues]),
    data: serverResult.data,
    html: serverResult.html,
    fileData: serverResult.fileData,
  };
}
