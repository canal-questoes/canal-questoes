// Módulo de ligação com as peças do PL reutilizadas sem alteração (fidelidade ao PL).
// O dist do PL não tem .d.ts: os módulos são importados dinamicamente pelo caminho absoluto, e os
// tipos abaixo declaram só o que o motor usa. O mustache vem do node_modules do PL por import comum
// (em pl/phases.ts), porque o pacote tem tipos.
//
// Peças usadas:
// - CodeCallerNative e FunctionMissingError (dist/lib/code-caller): o executor Python (zygote);
// - markdown.processQuestion (dist/lib/markdown.js): os blocos <markdown> do question.html;
// - APP_ROOT_PATH (dist/lib/paths.js): a raiz de apps/prairielearn (elements/ e python/);
// - QuestionJsonSchema (dist/schemas/infoQuestion.js): o formato do info.json da questão, o mesmo que
//   a sincronização do PL confere (sync/course-db.js).
import path from 'node:path';
import { pathToFileURL } from 'node:url';

/** Raiz de apps/prairielearn: o motor fica em <raiz>/canal-questoes-engine/src/pl/. */
const PL_APP_DIR = path.resolve(import.meta.dirname, '..', '..', '..');

function distModule(rel: string): Promise<Record<string, unknown>> {
  return import(pathToFileURL(path.join(PL_APP_DIR, 'dist', rel)).href) as Promise<Record<string, unknown>>;
}

export type CallType = 'question' | 'course-element' | 'core-element' | 'restart' | 'ping';

/** Dados que o CodeCallerNative anexa ao erro de uma chamada (err.data). Só outputBoth é lido. */
export interface CodeCallerErrorData {
  outputBoth?: string;
  [key: string]: unknown;
}

export interface CodeCallerResult {
  result: unknown;
  output: string;
}

/** A parte da interface do CodeCallerNative (code-caller-native.js, PL a193f72) que o motor usa. */
export interface CodeCaller {
  readonly uuid: string;
  prepareForCourse(opts: { coursePath: string; forbiddenModules: string[] }): Promise<void>;
  call(type: CallType, directory: string | null, file: string | null, fcn: string, args: unknown[]): Promise<CodeCallerResult>;
  /** true: o executor voltou ao zygote; false: precisa ser recriado. Pode lançar. */
  restart(): Promise<boolean>;
  done(): void;
}

export interface CodeCallerOptions {
  dropPrivileges: boolean;
  questionTimeoutMilliseconds: number;
  pingTimeoutMilliseconds: number;
  errorLogger: (...args: unknown[]) => void;
}

interface CodeCallerNativeClass {
  create(options: CodeCallerOptions): Promise<CodeCaller>;
}

interface SafeParseSchema {
  safeParse(value: unknown): { success: boolean };
}

const [codeCallerModule, sharedModule, markdownModule, pathsModule, infoQuestionModule] = await Promise.all([
  distModule('lib/code-caller/code-caller-native.js'),
  distModule('lib/code-caller/code-caller-shared.js'),
  distModule('lib/markdown.js'),
  distModule('lib/paths.js'),
  distModule('schemas/infoQuestion.js'),
]);

export const CodeCallerNative = codeCallerModule.CodeCallerNative as CodeCallerNativeClass;

export const FunctionMissingError = sharedModule.FunctionMissingError as new (message: string) => Error;

/** APP_ROOT_PATH do PL (dist/lib/paths.js). */
export const APP_ROOT_PATH = pathsModule.APP_ROOT_PATH as string;

/**
 * markdown.processQuestion do PL. A chamada passa pelo namespace do módulo a cada vez, para usar
 * sempre a ligação exportada pelo PL.
 */
export function processQuestionMarkdown(html: string): string {
  return (markdownModule.processQuestion as (html: string) => string)(html);
}

/** O esquema zod do info.json de questão do PL (QuestionJsonSchema). */
export const QuestionJsonSchema = infoQuestionModule.QuestionJsonSchema as SafeParseSchema;

/** A mensagem de tempo esgotado do CodeCallerNative (_timeout). */
export const TIMEOUT_MESSAGE = 'timeout exceeded, killing CodeCallerNative child';
