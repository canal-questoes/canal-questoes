// Leitura de uma questão da área de repositórios (só leitura) e o contexto das fases do PL.
// O info.json é conferido como na sincronização do PL (sync/course-db.js): pelo esquema do próprio PL
// (QuestionJsonSchema) e pelas regras de validateQuestion (pl/question-validation.ts).
// JSON inválido, tipos fora do formato ou um erro dessas regras dão metadados
// inválidos, e nunca 500. Os arquivos só são lidos se forem regulares e couberem no limite
// (pl/text-file.ts). Os elementos do curso são relidos a cada pedido, com os problemas
// que fariam o PL recusar o curso (pl/elements.ts).
import path from 'node:path';

import { QuestionJsonSchema } from '../pl/bindings.ts';
import { coreElements, courseElements, type CourseElementProblem, type ElementMap, type ExtensionMap } from '../pl/elements.ts';
import type { QuestionContext } from '../pl/phases.ts';
import { validateQuestion } from '../pl/question-validation.ts';
import { readTextFile, type TextFileRead } from '../pl/text-file.ts';
import { resolveQuestionPaths, type QuestionRef } from './reference.ts';

/**
 * valid: o info.json passa na regra do PL. data: o objeto lido (mesmo inválido pelo esquema), ou null se
 * não é um objeto JSON. Os metadados da inspeção só usam data com valid.
 */
export interface QuestionInfo {
  valid: boolean;
  data: Record<string, unknown> | null;
}

/** Os arquivos da questão que o motor lê no processo Node. */
export type QuestionFile = 'info.json' | 'question.html' | 'server.py';

export interface LoadedQuestion {
  ref: QuestionRef;
  /** Raiz real do curso (realpath). */
  courseRoot: string;
  /** Diretório real da questão (realpath), para leituras. */
  questionDir: string;
  info: QuestionInfo;
  /** question.html cru; "" se não existe ou não é lido. */
  html: string;
  /** server.py cru, ou null se não existe ou não é lido. */
  serverPy: string | null;
  /**
   * Os arquivos da questão que existem mas o motor não lê, na ordem de QuestionFile: não são arquivo
   * regular depois de seguir os links (dispositivo, FIFO, socket) ou passam do limite.
   */
  unreadable: QuestionFile[];
  core: ElementMap;
  course: ElementMap;
  extensions: ExtensionMap;
  /** O que faria o PL recusar os elementos do curso (loadElements, loadExtensions); vazio se o curso carrega. */
  courseProblems: CourseElementProblem[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

type QuestionFileRead = { status: 'ok'; text: string } | { status: 'absent' } | { status: 'unreadable' };

async function readQuestionFile(file: string): Promise<QuestionFileRead> {
  let read: TextFileRead;
  try {
    read = await readTextFile(file);
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    // ELOOP: um link simbólico em ciclo no repositório, conteúdo da questão como os demais.
    if (code === 'ENOENT' || code === 'ENOTDIR' || code === 'EISDIR' || code === 'ELOOP') return { status: 'absent' };
    throw err;
  }
  if (read.status === 'ok') return read;
  // Um diretório no lugar do arquivo conta como ausente, como o EISDIR do fs.readFile.
  if (read.status === 'not_regular' && read.directory) return { status: 'absent' };
  return { status: 'unreadable' };
}

function textOf(read: QuestionFileRead): string | null {
  return read.status === 'ok' ? read.text : null;
}

/**
 * O info.json pela regra do PL: JSON, objeto, QuestionJsonSchema (que também exige o uuid) e, sobre a
 * saída do esquema (com os padrões preenchidos), as regras de validateQuestion.
 */
export function parseQuestionInfo(text: string | null): QuestionInfo {
  if (text === null) return { valid: false, data: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { valid: false, data: null };
  }
  if (!isRecord(parsed)) return { valid: false, data: null };
  let accepted = false;
  try {
    const result = QuestionJsonSchema.safeParse(parsed) as { success: boolean; data?: unknown };
    accepted = result.success && validateQuestion(result.data).length === 0;
  } catch {
    accepted = false;
  }
  return { valid: accepted, data: parsed };
}

/** Resolve a referência e lê os arquivos da questão e os elementos (core e do curso). */
export async function loadQuestion(reposRoot: string, ref: QuestionRef): Promise<LoadedQuestion> {
  const { courseRoot, questionDir } = await resolveQuestionPaths(reposRoot, ref);
  const [infoRead, htmlRead, serverRead, core, courseMaps] = await Promise.all([
    readQuestionFile(path.join(questionDir, 'info.json')),
    readQuestionFile(path.join(questionDir, 'question.html')),
    readQuestionFile(path.join(questionDir, 'server.py')),
    coreElements(),
    courseElements(courseRoot),
  ]);
  const reads: Array<[QuestionFile, QuestionFileRead]> = [
    ['info.json', infoRead],
    ['question.html', htmlRead],
    ['server.py', serverRead],
  ];
  return {
    ref,
    courseRoot,
    questionDir,
    // Um info.json que não é lido é ilegível: metadados inválidos.
    info: parseQuestionInfo(textOf(infoRead)),
    html: textOf(htmlRead) ?? '',
    serverPy: textOf(serverRead),
    unreadable: reads.filter(([, read]) => read.status === 'unreadable').map(([file]) => file),
    core,
    course: courseMaps.elements,
    extensions: courseMaps.extensions,
    courseProblems: courseMaps.problems,
  };
}

/**
 * partialCredit ?? type === "v3" (sync/fromDisk/questions.js do PL). Na correção, que não reavalia o
 * suporte, vale o objeto lido mesmo fora do esquema.
 */
export function partialCreditOf(info: QuestionInfo): boolean {
  const data = info.data;
  if (data === null) return false;
  return typeof data.partialCredit === 'boolean' ? data.partialCredit : data.type === 'v3';
}

/** O contexto das fases (getContext do freeform.js, modo native). */
export function questionContext(question: LoadedQuestion): QuestionContext {
  return {
    qid: question.ref.qid,
    courseDir: question.courseRoot,
    questionDir: path.join(question.courseRoot, 'questions', question.ref.qid),
    coreElements: question.core,
    courseElements: question.course,
    courseElementExtensions: question.extensions,
    partialCredit: partialCreditOf(question.info),
  };
}
