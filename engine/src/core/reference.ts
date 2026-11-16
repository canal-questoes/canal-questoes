// Referência de questão e seed.
// A raiz do curso é <reposRoot>/<sha256_hex(utf8(repo))>/<commit>/, e a questão fica em
// <raiz>/questions/<qid>/, com info.json. A contenção compara realpaths dos dois lados, porque a
// própria raiz pode ser um link simbólico (a imagem de teste monta o exampleCourse assim). Como na
// varredura do PL, um diretório dentro de outra questão não é questão (escolha do motor).
// As mensagens são fixas: não ecoam o pedido nem caminhos do servidor.
import { createHash } from 'node:crypto';
import type { Stats } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';

import { InvalidRequestError, NotFoundError } from '../errors.ts';

export interface QuestionRef {
  repo: string;
  commit: string;
  qid: string;
}

// Os mesmos limites de question_ref e $defs.seed em engine-api.schema.json.
const REPO_MAX_LENGTH = 2048;
const COMMIT_PATTERN = /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/;
const QID_MAX_LENGTH = 512;
const QID_SEGMENT_PATTERN = /^[A-Za-z0-9_.-]+$/;
const SEED_PATTERN = /^[0-9a-z]{1,7}$/;
// O PL sorteia a seed em [0, 2^32), e o numpy.random.seed recusa valores maiores [F: question-variant.ts].
const SEED_LIMIT = 2 ** 32;

/** sha256 hex minúsculo de utf8(repo): o nome do diretório do repositório na área. */
export function repoKey(repo: string): string {
  return createHash('sha256').update(repo, 'utf8').digest('hex');
}

/** Converte a seed como o PL (parseInt em base 36); fora do formato ou ≥ 2^32 é pedido inválido. */
export function parseSeed(seed: string): number {
  if (typeof seed !== 'string' || !SEED_PATTERN.test(seed)) {
    throw new InvalidRequestError(
      'Seed inválida: use de 1 a 7 caracteres em base 36 minúscula (dígitos e letras de a a z).',
    );
  }
  const value = parseInt(seed, 36);
  if (value >= SEED_LIMIT) {
    throw new InvalidRequestError('Seed inválida: o valor em base 36 precisa ser menor que 2^32 (4294967296).');
  }
  return value;
}

/**
 * Resolve a referência para os caminhos reais do curso e da questão.
 * Lança InvalidRequestError (400) para repo, commit ou qid fora do formato, e NotFoundError (404)
 * para repositório, commit ou questão ausentes, questão sem info.json, fora de questions/ ou dentro do
 * diretório de outra questão.
 */
export async function resolveQuestionPaths(
  reposRoot: string,
  ref: QuestionRef,
): Promise<{ courseRoot: string; questionDir: string }> {
  validateRepo(ref.repo);
  validateCommit(ref.commit);
  const segments = qidSegments(ref.qid);

  // O diretório do repositório é conferido à parte só para a mensagem distinguir repo de commit.
  const repoDir = path.join(reposRoot, repoKey(ref.repo));
  if ((await realDirectory(repoDir)) === null) {
    throw new NotFoundError('Repositório não encontrado na área de repositórios do motor.');
  }
  const courseRoot = await realDirectory(path.join(repoDir, ref.commit));
  if (courseRoot === null) {
    throw new NotFoundError('Commit não encontrado para este repositório na área de repositórios do motor.');
  }

  // A questão precisa ficar estritamente dentro de realpath(raiz)/questions/, mesmo depois de
  // seguir os links do caminho: um link para fora dali conta como questão ausente.
  const questionsRoot = path.join(courseRoot, 'questions');
  const questionDir = await realDirectory(path.join(questionsRoot, ...segments));
  if (questionDir === null || !questionDir.startsWith(questionsRoot + path.sep)) {
    throw new NotFoundError('Questão não encontrada no curso.');
  }
  if (await insideAnotherQuestion(questionsRoot, segments)) {
    throw new NotFoundError('Questão não encontrada no curso: ela fica dentro do diretório de outra questão, e o PrairieLearn não a reconhece.');
  }
  if (!(await isFile(path.join(questionDir, 'info.json')))) {
    throw new NotFoundError('Questão não encontrada no curso: falta o arquivo info.json.');
  }
  return { courseRoot, questionDir };
}

function validateRepo(repo: unknown): void {
  // maxLength do JSON Schema conta pontos de código, não unidades UTF-16.
  if (typeof repo !== 'string' || repo.length === 0 || [...repo].length > REPO_MAX_LENGTH) {
    throw new InvalidRequestError(`Repositório inválido: informe um identificador com 1 a ${REPO_MAX_LENGTH} caracteres.`);
  }
}

function validateCommit(commit: unknown): void {
  if (typeof commit !== 'string' || !COMMIT_PATTERN.test(commit)) {
    throw new InvalidRequestError(
      'Commit inválido: use o hash completo, com 40 ou 64 hexadecimais minúsculos; branch, tag e abreviação não são aceitas.',
    );
  }
}

/** Segmentos do qid; recusa caminho absoluto, segmento vazio, "." e "..". */
function qidSegments(qid: unknown): string[] {
  const segments = typeof qid === 'string' && qid.length <= QID_MAX_LENGTH ? qid.split('/') : [];
  const valid =
    segments.length > 0 && segments.every((s) => QID_SEGMENT_PATTERN.test(s) && s !== '.' && s !== '..');
  if (!valid) {
    throw new InvalidRequestError(
      'Identificador de questão inválido: use um caminho relativo a questions/, com segmentos de letras, dígitos, "_", "." ou "-", sem "." nem "..".',
    );
  }
  return segments;
}

/**
 * Se algum diretório acima da questão, dentro de questions/, é questão para o PL. A varredura do PL
 * (loadInfoForDirectory em sync/course-db.js, com recursive) lê <dir>/info.json e só desce em <dir>
 * quando a leitura falha com ENOENT ou ENOTDIR; qualquer outro resultado (o arquivo lido, mesmo
 * inválido, ou um erro como EISDIR ou ELOOP) registra <dir> como questão, e nada abaixo dele é
 * questão. O stat dá os mesmos códigos sem ler o arquivo; os demais erros são da área (500).
 */
async function insideAnotherQuestion(questionsRoot: string, segments: string[]): Promise<boolean> {
  for (let depth = 1; depth < segments.length; depth++) {
    try {
      await fs.stat(path.join(questionsRoot, ...segments.slice(0, depth), 'info.json'));
      return true;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException | null)?.code;
      if (code === 'ENOENT' || code === 'ENOTDIR') continue;
      if (code === 'ELOOP' || code === 'ENAMETOOLONG') return true;
      throw err;
    }
  }
  return false;
}

/** realpath do caminho, se ele existir e for um diretório; null se não existir. */
async function realDirectory(p: string): Promise<string | null> {
  let real: string;
  try {
    real = await fs.realpath(p);
  } catch (err) {
    if (isMissing(err)) return null;
    throw err;
  }
  return (await statOrNull(real))?.isDirectory() ? real : null;
}

async function isFile(p: string): Promise<boolean> {
  return (await statOrNull(p))?.isFile() ?? false;
}

async function statOrNull(p: string): Promise<Stats | null> {
  try {
    return await fs.stat(p);
  } catch (err) {
    if (isMissing(err)) return null;
    throw err;
  }
}

/** Erros de sistema de arquivos que significam "não existe" (inclui laço de links). Os demais sobem (500). */
function isMissing(err: unknown): boolean {
  const code = (err as NodeJS.ErrnoException | null)?.code;
  return code === 'ENOENT' || code === 'ENOTDIR' || code === 'ELOOP' || code === 'ENAMETOOLONG';
}
