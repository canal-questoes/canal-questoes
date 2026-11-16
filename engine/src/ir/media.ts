// Mídias da IR (estáticas e geradas).
// No render da IR, as URLs de arquivos de data.options são marcadores (urn:cq:…), e cada <img> que
// aponta para um deles vira uma mídia: estática (bytes lidos do curso, com contenção de caminho) ou
// gerada (a fase file do PL, uma chamada por arquivo; quem chama é build.ts). O src é uma URL, como na
// página do PL: o nome do arquivo é o caminho antes do primeiro "?" ou "#" (a rota do PL lê o
// req.params[0] do Express, sem a query, e o navegador nunca envia o fragmento), decodificado. Uma
// <img> com src de outra origem (URL externa, data:, outro caminho), que o aluno do PL vê e o motor não
// obtém, vira mídia ausente, com aviso (build.ts). A numeração m1, m2… segue a primeira aparição (as de
// outra origem também contam), e a mesma origem com o mesmo filename reusa o id. Uma mídia acima dos
// tetos de MEDIA_LIMITS fica ausente, com o mesmo id.
import { constants } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

import type { IrMedia } from '../contract/types.ts';

/**
 * Tetos das mídias da IR (escolha do motor, como o limite de text-file.ts). O PL serve um arquivo
 * estático por fluxo, sem limite (sendCourseFile, res.sendFile), mas a IR leva os bytes em base64
 * numa única resposta JSON. Sem teto, um arquivo de 2 GiB dá 500 (ERR_FS_FILE_TOO_LARGE) a cada
 * tentativa, e mídias somando centenas de MiB estouram o JSON.stringify da resposta.
 * - fileBytes, 12 MiB por mídia: o maior arquivo que uma mídia gerada consegue ter, porque o file()
 *   volta em base64 no resultado da chamada, com teto de 16 MiB (output-guard.ts); as duas origens têm
 *   assim o mesmo teto. É mais de 17 vezes a maior imagem do exampleCourse e do testCourse do PL
 *   a193f72 (682 200 bytes).
 * - totalBytes, 32 MiB por IR (cerca de 43 MiB em base64): a resposta da criação (as mídias, o HTML e
 *   o snapshot, que tem de caber no corpo de 2 MiB de um pedido de correção) fica uma ordem de grandeza
 *   abaixo do limite de cadeia do V8 (~512 Mi caracteres), e a memória de um pedido (bytes, base64,
 *   JSON e o buffer de escrita, cerca de 4 vezes o total) fica bem abaixo de um container de 1 GiB. O
 *   maior clientFilesQuestion do exampleCourse tem 2,9 MB.
 * O total conta as mídias na ordem da numeração: a que não cabe no que resta fica ausente, e uma
 * seguinte menor ainda entra.
 */
export const MEDIA_LIMITS = {
  fileBytes: 12 * 1024 * 1024,
  totalBytes: 32 * 1024 * 1024,
} as const;

/** Os valores de client_files_question_url, client_files_course_url e client_files_question_dynamic_url. */
export const MEDIA_URLS = {
  clientFilesQuestion: 'urn:cq:clientFilesQuestion',
  clientFilesCourse: 'urn:cq:clientFilesCourse',
  generatedFilesQuestion: 'urn:cq:generatedFilesQuestion',
} as const;

/** O prefixo dos marcadores de URL do render da IR (os de MEDIA_URLS e os dos arquivos de elementos). */
const MARKER_PREFIX = 'urn:cq:';

/** O tamanho máximo, em caracteres, do filename de uma mídia de outra origem. */
const OTHER_FILENAME_MAX = 200;

const CONTENT_TYPES: Readonly<Record<string, string>> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
  '.bmp': 'image/bmp',
  '.ico': 'image/x-icon',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.avif': 'image/avif',
  '.pdf': 'application/pdf',
};

/** content_type pela extensão. */
export function contentTypeOf(filename: string): string {
  return CONTENT_TYPES[path.posix.extname(filename).toLowerCase()] ?? 'application/octet-stream';
}

/** O caminho de uma URL: o texto antes do primeiro "?" (query) ou "#" (fragmento). */
export function urlPath(src: string): string {
  const cut = src.search(/[?#]/);
  return cut < 0 ? src : src.slice(0, cut);
}

/**
 * O src como o navegador lê a URL (WHATWG URL): sem os controles e espaços das pontas e sem tabulação
 * nem quebra de linha.
 */
function cleanSrc(src: string): string {
  return src.replace(/^[\x00-\x20]+|[\x00-\x20]+$/g, '').replace(/[\t\n\r]/g, '');
}

/**
 * O filename de uma mídia de outra origem, só informativo: o src (sem o prefixo dos marcadores), com
 * o conteúdo de um data: trocado por "…" (os bytes não vão no nome de uma mídia ausente), cortado em
 * OTHER_FILENAME_MAX caracteres.
 */
function otherFilename(src: string): string {
  let name = src.startsWith(MARKER_PREFIX) ? src.slice(MARKER_PREFIX.length) : src;
  const comma = name.indexOf(',');
  if (/^data:/i.test(name) && comma >= 0 && comma < name.length - 1) name = `${name.slice(0, comma + 1)}…`;
  const chars = [...name];
  return chars.length > OTHER_FILENAME_MAX ? `${chars.slice(0, OTHER_FILENAME_MAX).join('')}…` : name;
}

/**
 * urllib.parse.unquote do Python: cada sequência %XX vira o byte, e as sequências de bytes são lidas
 * em UTF-8 com substituição; o resto do texto fica como está.
 */
export function unquote(text: string): string {
  if (!text.includes('%')) return text;
  return text.replace(/(?:%[0-9A-Fa-f]{2})+/g, (run) => {
    const bytes = Buffer.from(run.replace(/%/g, ''), 'hex');
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  });
}

type Source =
  | { kind: 'static'; baseDir: string; relative: string }
  | { kind: 'dynamic'; filename: string }
  /** src (já limpo) fora das três origens, ou gerado sem nome: mídia ausente. */
  | { kind: 'other'; src: string };

export interface MediaEntry {
  id: string;
  origin: 'static' | 'dynamic';
  filename: string;
  alt: string | null;
  source: Source;
}

export interface MediaRoots {
  /** Diretório real da questão. */
  questionDir: string;
  /** Raiz real do curso. */
  courseRoot: string;
}

/** O registro que criou cada entrada: irMedia confere nele o teto agregado. */
const REGISTRY_OF = new WeakMap<MediaEntry, MediaRegistry>();

export class MediaRegistry {
  readonly entries: MediaEntry[] = [];
  readonly #byKey = new Map<string, MediaEntry>();
  readonly #roots: MediaRoots;
  /** A decisão dos tetos de cada mídia com conteúdo, tomada uma vez. */
  readonly #admitted = new Map<MediaEntry, boolean>();
  #usedBytes = 0;

  constructor(roots: MediaRoots) {
    this.#roots = roots;
  }

  /**
   * O resolveImage de canonicalizeHtml: o id da mídia do src, ou null se o src é vazio (a imagem não
   * tem origem, e fica o alt). Um src fora das três origens também vira mídia (ausente, origin
   * "static", filename de otherFilename), com a chave pelo src inteiro: nunca reusa o id de um arquivo
   * do curso, e dois src diferentes não se juntam, mesmo com o mesmo filename.
   */
  readonly resolve = (src: string, alt: string | null): string | null => {
    const source = this.#sourceOf(src);
    if (source === null) return null;
    const origin = source.kind === 'dynamic' ? 'dynamic' : 'static';
    let filename: string;
    let key: string;
    if (source.kind === 'other') {
      filename = otherFilename(source.src);
      key = `other:${source.src}`;
    } else {
      filename = source.kind === 'static' ? `${path.posix.basename(source.baseDir)}/${source.relative}` : source.filename;
      key = `${source.kind}:${filename}`;
    }
    let entry = this.#byKey.get(key);
    if (entry === undefined) {
      entry = { id: `m${this.entries.length + 1}`, origin, filename, alt, source };
      this.entries.push(entry);
      this.#byKey.set(key, entry);
      REGISTRY_OF.set(entry, this);
    }
    return entry.id;
  };

  /** O que ainda cabe no teto agregado (MEDIA_LIMITS.totalBytes). */
  get remainingBytes(): number {
    return MEDIA_LIMITS.totalBytes - this.#usedBytes;
  }

  /**
   * Se a mídia, com size bytes, entra na IR: cabe no teto por mídia e no que resta do agregado. A
   * decisão de cada entrada é tomada uma vez, e só a que entra conta no total.
   */
  admit(entry: MediaEntry, size: number): boolean {
    const known = this.#admitted.get(entry);
    if (known !== undefined) return known;
    const fits = size <= MEDIA_LIMITS.fileBytes && size <= this.remainingBytes;
    if (fits) this.#usedBytes += size;
    this.#admitted.set(entry, fits);
    return fits;
  }

  /**
   * A identidade estável da mídia (cq-media:<origin>:<filename>), para o hash das opções; a de outra
   * origem é o src inteiro, porque o filename dela é cortado.
   */
  identity(id: string): string | undefined {
    const entry = this.entries.find((e) => e.id === id);
    if (entry === undefined) return undefined;
    return entry.source.kind === 'other' ? `other:${entry.source.src}` : `${entry.origin}:${entry.filename}`;
  }

  #sourceOf(src: string): Source | null {
    const cleaned = cleanSrc(src);
    if (cleaned === '') return null;
    // O caminho (sem query nem fragmento) antes do unquote: um "%3F" no nome continua no nome.
    const location = urlPath(cleaned);
    const after = (base: string): string | null => (location.startsWith(`${base}/`) ? location.slice(base.length + 1) : null);
    // Um caminho vazio no diretório-base é uma estática ausente (readStaticMedia); na gerada, não há
    // nome a passar ao file().
    const question = after(MEDIA_URLS.clientFilesQuestion);
    if (question !== null) {
      return { kind: 'static', baseDir: path.join(this.#roots.questionDir, 'clientFilesQuestion'), relative: unquote(question) };
    }
    const course = after(MEDIA_URLS.clientFilesCourse);
    if (course !== null) {
      return { kind: 'static', baseDir: path.join(this.#roots.courseRoot, 'clientFilesCourse'), relative: unquote(course) };
    }
    const generated = after(MEDIA_URLS.generatedFilesQuestion);
    if (generated !== null && generated !== '') return { kind: 'dynamic', filename: unquote(generated) };
    return { kind: 'other', src: cleaned };
  }
}

function inside(dir: string, file: string): boolean {
  return file.startsWith(dir + path.sep);
}

export type StaticMediaRead =
  | { status: 'ok'; bytes: Buffer }
  /** Não existe, não é arquivo regular ou o caminho escapa do diretório-base. */
  | { status: 'missing' }
  /** Arquivo regular acima do teto; os bytes acima do teto não são lidos. */
  | { status: 'too_large' };

const MISSING: StaticMediaRead = { status: 'missing' };
const TOO_LARGE: StaticMediaRead = { status: 'too_large' };

/** Erros do sistema de arquivos que querem dizer "a mídia não existe" (os demais sobem). */
const MISSING_CODES: ReadonlySet<string> = new Set(['ENOENT', 'ENOTDIR', 'ELOOP', 'EISDIR', 'ENAMETOOLONG', 'EACCES']);

/**
 * Lê o arquivo regular até maxBytes, como readTextFile (text-file.ts): o tipo e o tamanho são
 * conferidos de novo no arquivo aberto (fstat), aberto sem bloqueio (O_NONBLOCK) e sem seguir link
 * (O_NOFOLLOW: o caminho já vem do realpath), caso ele mude depois do stat. A leitura para em
 * maxBytes + 1, sem confiar no tamanho informado.
 */
async function readBounded(file: string, maxBytes: number): Promise<StaticMediaRead> {
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NONBLOCK | constants.O_NOFOLLOW);
  try {
    const stats = await handle.stat();
    if (!stats.isFile()) return MISSING;
    if (stats.size > maxBytes) return TOO_LARGE;
    let buffer = Buffer.allocUnsafe(stats.size + 1);
    let total = 0;
    for (;;) {
      if (total === buffer.length) {
        // O arquivo cresceu depois do fstat.
        const bigger = Buffer.allocUnsafe(Math.min(maxBytes + 1, buffer.length * 2));
        buffer.copy(bigger, 0, 0, total);
        buffer = bigger;
      }
      const { bytesRead } = await handle.read(buffer, total, buffer.length - total, null);
      if (bytesRead === 0) break;
      total += bytesRead;
      if (total > maxBytes) return TOO_LARGE;
    }
    return { status: 'ok', bytes: buffer.subarray(0, total) };
  } finally {
    await handle.close();
  }
}

/**
 * Uma mídia estática: os bytes; "missing" se o arquivo não existe, não é arquivo regular ou o caminho
 * escapa do diretório-base (também depois de seguir os links); ou "too_large" se ele passa de
 * maxBytes (por padrão, o teto por mídia), conferido pelo tamanho antes de ler.
 */
export async function readStaticMediaFile(
  baseDir: string,
  relative: string,
  maxBytes: number = MEDIA_LIMITS.fileBytes,
): Promise<StaticMediaRead> {
  const target = path.resolve(baseDir, relative);
  if (!inside(baseDir, target)) return MISSING;
  try {
    const realBase = await fs.realpath(baseDir);
    const realTarget = await fs.realpath(target);
    if (!inside(realBase, realTarget)) return MISSING;
    const stats = await fs.stat(realTarget);
    if (!stats.isFile()) return MISSING;
    if (stats.size > maxBytes) return TOO_LARGE;
    return await readBounded(realTarget, maxBytes);
  } catch (err) {
    if (MISSING_CODES.has((err as NodeJS.ErrnoException).code ?? '')) return MISSING;
    throw err;
  }
}

/**
 * Os bytes de uma mídia estática, ou null se ela não existe, não é arquivo regular, escapa do
 * diretório-base ou passa do teto por mídia (readStaticMediaFile). Com null, a mídia fica ausente, e
 * quem monta a IR dá o aviso missing_media.
 */
export async function readStaticMedia(baseDir: string, relative: string): Promise<Buffer | null> {
  const read = await readStaticMediaFile(baseDir, relative);
  return read.status === 'ok' ? read.bytes : null;
}

/** Se o conteúdo cabe nos tetos: no por mídia e, numa entrada de um MediaRegistry, no agregado. */
function fitsLimits(entry: MediaEntry, size: number): boolean {
  const registry = REGISTRY_OF.get(entry);
  return registry === undefined ? size <= MEDIA_LIMITS.fileBytes : registry.admit(entry, size);
}

/**
 * A mídia da IR, com ou sem conteúdo. O conteúdo que não cabe nos tetos (MEDIA_LIMITS) sai, e a
 * mídia fica ausente, com o mesmo id; o aviso missing_media é de quem monta a IR (bytes não nulos e
 * status "missing").
 */
export function irMedia(entry: MediaEntry, bytes: Buffer | null): IrMedia {
  const kept = bytes !== null && fitsLimits(entry, bytes.length) ? bytes : null;
  const media: IrMedia = {
    id: entry.id,
    origin: entry.origin,
    filename: entry.filename,
    // De outra origem, o tipo vem do caminho da URL.
    content_type: contentTypeOf(entry.source.kind === 'other' ? urlPath(entry.source.src) : entry.filename),
    alt: entry.alt,
    status: kept === null ? 'missing' : 'ok',
  };
  if (kept !== null) {
    media.data_base64 = kept.toString('base64');
    media.size_bytes = kept.length;
  }
  return media;
}
