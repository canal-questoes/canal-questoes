// Mapa de elementos, espelhando loadElements e loadExtensions de dist/question-servers/freeform.js
// (PL a193f72):
// - core: cada diretório de <APP_ROOT_PATH>/elements com info.json; cada nome vale também com "-"
//   trocado por "_", e valem os additionalNames (e as variantes deles com "_");
// - curso: todo diretório de <curso>/elements com info.json, só pelo nome exato do diretório.
// Na fase, o PL manda {...core, ...curso} ao Python: um elemento do curso tem precedência sobre um core
// de mesmo nome (resolveElement).
// Como no PL, o info.json de um elemento ou de uma extensão do curso é lido como no fs.readJson (UTF-8,
// sem o BOM do início) e conferido pelos esquemas do próprio PL (ElementCourseJsonSchema e
// ElementExtensionJsonSchema). Um info.json ilegível, que não é JSON ou que está fora do esquema, um
// diretório esperado que é arquivo, ou um subdiretório de elementExtensions/ que não existe (um link
// simbólico quebrado: o readdir interno do loadExtensions não trata erro nenhum), faz o loadElements ou o
// loadExtensions lançar, e o getContext do PL falha em toda fase de toda questão do curso. O info.json
// só é lido se for arquivo regular dentro do limite (text-file.ts); o PL leria sem fim um link
// para /dev/zero ou esperaria para sempre num FIFO, e o motor conta esses casos como ilegíveis. O motor
// guarda esses casos em CourseElements.problems, que o veredito de suporte trata (support.ts). Os mapas
// continuam montados com o que foi lido, para a correção, que não reavalia o suporte. O mapa do curso é
// relido a cada pedido; só o do core, que vem da imagem, fica guardado.
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { APP_ROOT_PATH } from './bindings.ts';
import { readTextFile, type TextFileRead } from './text-file.ts';

export type ElementType = 'core' | 'course';

export interface ElementInfo {
  /** Nome do diretório do elemento: o nome canônico. */
  name: string;
  directory: string;
  type: ElementType;
  controller?: unknown;
  additionalNames?: unknown;
  [key: string]: unknown;
}

export type ElementMap = Record<string, ElementInfo>;
export type ExtensionMap = Record<string, Record<string, Record<string, unknown>>>;

/** Um caminho do curso que faz o PL recusar os elementos do curso. */
export interface CourseElementProblem {
  /** Relativo à raiz do curso, com "/" (ex.: elements/meu-elemento/info.json). */
  path: string;
  /** O elemento do curso (diretório de elements/) cujo info.json falhou, se for o caso. */
  element?: string;
}

interface SafeParseSchema {
  safeParse(value: unknown): { success: true; data: Record<string, unknown> } | { success: false };
}

/** Os esquemas que o freeform.js importa de dist/schemas/index.js. */
const plSchemas = (await import(pathToFileURL(path.join(APP_ROOT_PATH, 'dist', 'schemas', 'index.js')).href)) as Record<string, unknown>;
const ElementCourseJsonSchema = plSchemas.ElementCourseJsonSchema as SafeParseSchema;
const ElementExtensionJsonSchema = plSchemas.ElementExtensionJsonSchema as SafeParseSchema;

function errorCode(err: unknown): string | undefined {
  return (err as NodeJS.ErrnoException | null)?.code;
}

/** Falhas de arquivo que vêm do conteúdo do repositório (o PL falha do mesmo jeito), e não da área. */
function isContentError(err: unknown): boolean {
  const code = errorCode(err);
  return code === 'ENOTDIR' || code === 'EISDIR' || code === 'ELOOP';
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Um info.json lido como no fs.readJson do PL: missing (um dos códigos que o PL pula), unreadable (o PL
 * lança ao ler, ou o motor não lê: não é arquivo regular ou passa do limite), invalid (não é JSON) ou ok.
 */
type JsonRead = { status: 'missing' } | { status: 'unreadable' } | { status: 'invalid' } | { status: 'ok'; value: unknown };

async function readJson(file: string, missing: readonly string[]): Promise<JsonRead> {
  let read: TextFileRead;
  try {
    read = await readTextFile(file);
  } catch (err) {
    const code = errorCode(err);
    if (code !== undefined && missing.includes(code)) return { status: 'missing' };
    if (isContentError(err)) return { status: 'unreadable' };
    throw err;
  }
  // Um diretório no lugar do info.json (o EISDIR do PL), um dispositivo, um FIFO ou um acima do limite.
  if (read.status !== 'ok') return { status: 'unreadable' };
  const text = read.text;
  try {
    return { status: 'ok', value: JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text) };
  } catch {
    return { status: 'invalid' };
  }
}

/** A informação lida, para os mapas da correção: o objeto, {} se não for objeto JSON, ou null se ilegível. */
function readInfo(read: JsonRead): Record<string, unknown> | null {
  if (read.status === 'ok') return isRecord(read.value) ? read.value : {};
  return read.status === 'invalid' ? {} : null;
}

/**
 * readdir ordenado. Um arquivo no lugar do diretório é problema do curso. Um diretório que não existe dá
 * [] onde o PL tolera o ENOENT (a raiz de elements/ e a de elementExtensions/) e é problema do curso onde
 * o PL não o trata (missingIsProblem: cada entrada de elementExtensions/).
 */
async function readDirNames(
  dir: string,
  rel: string,
  problems: CourseElementProblem[] | null,
  missingIsProblem = false,
): Promise<string[]> {
  try {
    return (await fs.readdir(dir)).sort();
  } catch (err) {
    const missing = errorCode(err) === 'ENOENT';
    if (problems !== null && (isContentError(err) || (missing && missingIsProblem))) {
      problems.push({ path: rel });
      return [];
    }
    if (missing) return [];
    throw err;
  }
}

async function isDirectory(file: string): Promise<boolean> {
  try {
    return (await fs.lstat(file)).isDirectory();
  } catch (err) {
    const code = errorCode(err);
    if (code === 'ENOENT' || code === 'ENOTDIR') return false;
    throw err;
  }
}

/** loadElements do freeform.js para os elementos core (a imagem do PL: qualquer falha é do motor). */
async function loadCoreElements(sourceDir: string): Promise<ElementMap> {
  const elements: ElementMap = {};
  for (const elementName of await readDirNames(sourceDir, '', null)) {
    if (!(await isDirectory(path.join(sourceDir, elementName)))) continue;
    const read = await readJson(path.join(sourceDir, elementName, 'info.json'), ['ENOENT']);
    if (read.status === 'missing') continue;
    if (read.status !== 'ok') throw new Error(`info.json ilegível no elemento core ${elementName}`);
    const rawInfo = isRecord(read.value) ? read.value : {};
    const info: ElementInfo = { ...rawInfo, name: elementName, directory: path.join(sourceDir, elementName), type: 'core' };
    elements[elementName] = info;
    elements[elementName.replaceAll('-', '_')] = info;
    const additionalNames = Array.isArray(rawInfo.additionalNames) ? rawInfo.additionalNames : [];
    for (const name of additionalNames) {
      if (typeof name !== 'string') continue;
      elements[name] = info;
      elements[name.replaceAll('-', '_')] = info;
    }
  }
  return elements;
}

/** loadElements do freeform.js para os elementos do curso, com os problemas que o fariam lançar. */
async function loadCourseElements(sourceDir: string, problems: CourseElementProblem[]): Promise<ElementMap> {
  const elements: ElementMap = {};
  for (const elementName of await readDirNames(sourceDir, 'elements', problems)) {
    const directory = path.join(sourceDir, elementName);
    if (!(await isDirectory(directory))) continue;
    const read = await readJson(path.join(directory, 'info.json'), ['ENOENT']);
    if (read.status === 'missing') continue;
    const parsed = read.status === 'ok' ? ElementCourseJsonSchema.safeParse(read.value) : null;
    if (parsed?.success === true) {
      elements[elementName] = { name: elementName, directory, type: 'course', ...parsed.data };
      continue;
    }
    problems.push({ path: `elements/${elementName}/info.json`, element: elementName });
    const rawInfo = readInfo(read);
    if (rawInfo !== null) elements[elementName] = { ...rawInfo, name: elementName, directory, type: 'course' };
  }
  return elements;
}

/** loadExtensions do freeform.js (extensões de elementos do curso, em elementExtensions/). */
async function loadExtensions(sourceDir: string, runtimeDir: string, problems: CourseElementProblem[]): Promise<ExtensionMap> {
  const extensions: ExtensionMap = {};
  for (const element of await readDirNames(sourceDir, 'elementExtensions', problems)) {
    const extensionDirs = await readDirNames(path.join(sourceDir, element), `elementExtensions/${element}`, problems, true);
    // O PL cria a entrada do elemento para cada extensão encontrada, com info.json ou não.
    if (extensionDirs.length > 0) extensions[element] = {};
    for (const extensionDir of extensionDirs) {
      const read = await readJson(path.join(sourceDir, element, extensionDir, 'info.json'), ['ENOENT', 'ENOTDIR']);
      if (read.status === 'missing') continue;
      const directory = path.join(runtimeDir, element, extensionDir);
      const parsed = read.status === 'ok' ? ElementExtensionJsonSchema.safeParse(read.value) : null;
      if (parsed?.success === true) {
        extensions[element][extensionDir] = { name: extensionDir, directory, ...parsed.data };
        continue;
      }
      problems.push({ path: `elementExtensions/${element}/${extensionDir}/info.json` });
      const rawInfo = readInfo(read);
      if (rawInfo !== null) extensions[element][extensionDir] = { ...rawInfo, name: extensionDir, directory };
    }
  }
  return extensions;
}

let coreElementsPromise: Promise<ElementMap> | null = null;

/** Os elementos core do PL, carregados uma vez (como o init() do freeform.js). */
export function coreElements(): Promise<ElementMap> {
  coreElementsPromise ??= loadCoreElements(path.join(APP_ROOT_PATH, 'elements')).catch((err: unknown) => {
    coreElementsPromise = null;
    throw err;
  });
  return coreElementsPromise;
}

export interface CourseElements {
  elements: ElementMap;
  extensions: ExtensionMap;
  /** O que faria o PL recusar os elementos do curso, na ordem da leitura; vazio quando o curso carrega. */
  problems: CourseElementProblem[];
}

/** Elementos e extensões do curso (loadElementsForCourse e loadExtensionsForCourse do freeform.js). */
export async function courseElements(courseRoot: string): Promise<CourseElements> {
  const problems: CourseElementProblem[] = [];
  const elements = await loadCourseElements(path.join(courseRoot, 'elements'), problems);
  const extensionsDir = path.join(courseRoot, 'elementExtensions');
  const extensions = await loadExtensions(extensionsDir, extensionsDir, problems);
  return { elements, extensions, problems };
}

/** resolveElement do freeform.js: o do curso (nome exato) antes do core (com os nomes alternativos). */
export function resolveElement(tag: string, core: ElementMap, course: ElementMap): ElementInfo | undefined {
  if (Object.hasOwn(course, tag)) return course[tag];
  if (Object.hasOwn(core, tag)) return core[tag];
  return undefined;
}

/** Nome canônico da tag: o nome do diretório do elemento a que ela resolve. */
export function canonicalName(tag: string, core: ElementMap, course: ElementMap): string | undefined {
  return resolveElement(tag, core, course)?.name;
}

/** O mapa da fase: {...core, ...curso} (processQuestionPhase do freeform.js). */
export function phaseElements(core: ElementMap, course: ElementMap): ElementMap {
  return { ...core, ...course };
}
