// Driver do oráculo da suíte de conformidade.
//
// O oráculo é o PL local em modo de desenvolvimento (container `pl-oracle`, mesma versão fixada do
// motor). Ele é dirigido pelos endpoints web e lido de forma estruturada no Postgres interno, pelo
// `psql` do próprio container (`docker exec pl-oracle psql -U postgres -At`).
// Sem dependências: `fetch` e
// `node:child_process`.
//
// Fatos do PL a193f72 usados aqui [F]:
// - modo de desenvolvimento: o título das páginas começa com "[DEV]" (`dist/components/HeadContents.js`);
// - `GET /pl/loadFromDisk` só existe em modo dev e redireciona para `/pl/jobSequence/<id>`, que roda em
//   segundo plano (`dist/pages/instructorLoadFromDisk/instructorLoadFromDisk.js`);
// - `POST …/question/<id>/settings/test` com `__action=test_once` e `variant_seed_prefix` redireciona para
//   `…/jobSequence/<id>` e cria, em ordem, as variantes `<prefixo>-0`, `-1` e `-2` com os tipos de teste
//   `correct`, `incorrect` e `invalid` (`TEST_TYPES[iter % 3]`, `dist/lib/question-testing.js`);
// - `POST …/preview` com `__action=grade` grava a resposta com as chaves do formulário menos `__action`,
//   `__csrf_token` e `__variant_id` (`dist/lib/question-submission.js`; `urlencoded` com `extended: false`);
// - o 400 "Submission ID mismatch" vem de `selectSubmissionForGrading` (`dist/lib/grading.js`), que
//   escolhe a última submissão da variante por data; o relógio da VM do Docker recua de tempos em
//   tempos, e repetir resolve;
// - uma submissão está concluída quando `gradable` é falso (não passou no `parse`), quando `broken` é
//   verdadeiro ou quando `graded_at` está preenchido.
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

export const ORACLE_URL = 'http://127.0.0.1:3000';
export const ORACLE_CONTAINER = 'pl-oracle';
export const EXAMPLE_COURSE_PATH = '/PrairieLearn/exampleCourse';

/** Ordem do `lib/question-testing.js` do PL: a variante `<prefixo>-<i>` recebe `TEST_TYPES[i]`. */
export const TEST_TYPES = ['correct', 'incorrect', 'invalid'] as const;
export type TestType = (typeof TEST_TYPES)[number];

/** Esperas antes de cada nova tentativa do POST de correção que volta com 400 (até 3 novas tentativas). */
const SUBMIT_RETRY_DELAYS_MS = [2_000, 8_000, 25_000];
const HTTP_TIMEOUT_MS = 120_000;
const CHECK_TIMEOUT_MS = 10_000;
const JOB_TIMEOUT_MS = 300_000;
const JOB_POLL_MS = 500;

const VARIANT_COLUMNS = 'id, variant_seed, params, true_answer, options, preferences, broken_at';
const SUBMISSION_COLUMNS =
  'id, variant_id, raw_submitted_answer, submitted_answer, format_errors, partial_scores, score, ' +
  'gradable, broken, feedback, params, true_answer, graded_at';

/** O ambiente da suíte não está pronto (oráculo, banco, Docker ou imagem). Não é divergência do motor. */
export class EnvironmentError extends Error {
  override name = 'EnvironmentError';
}

/** Resposta HTTP de erro do oráculo. */
export class OracleHttpError extends Error {
  override name = 'OracleHttpError';
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** Linha de `variants` do oráculo. */
export interface OracleVariant {
  id: number;
  variant_seed: string;
  params: unknown;
  true_answer: unknown;
  options: unknown;
  preferences: unknown;
  broken_at: string | null;
}

/** Linha de `submissions` do oráculo. */
export interface OracleSubmission {
  id: number;
  variant_id: number;
  raw_submitted_answer: unknown;
  submitted_answer: unknown;
  format_errors: unknown;
  partial_scores: unknown;
  score: number | null;
  gradable: boolean;
  broken: boolean;
  feedback: unknown;
  params: unknown;
  true_answer: unknown;
  graded_at: string | null;
}

/** Um dos três casos do `test_once`: a variante `<prefixo>-<i>` e a submissão dela. */
export interface TestOnceCase {
  testType: TestType;
  variant: OracleVariant;
  submission: OracleSubmission;
}

export interface ProcessResult {
  code: number | null;
  stdout: string;
  stderr: string;
}

/**
 * Roda um processo sem shell (sem conversão de caminhos do MSYS), com a entrada opcional na stdin, e
 * devolve a saída inteira. Compartilhado com `engine-cli.ts`.
 */
export function runProcess(command: string, args: readonly string[], input?: string): Promise<ProcessResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      stdio: [input === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    child.stdout?.on('data', (chunk: Buffer) => stdout.push(chunk));
    child.stderr?.on('data', (chunk: Buffer) => stderr.push(chunk));
    child.on('error', (err: NodeJS.ErrnoException) => {
      if (err.code === 'ENOENT') {
        reject(new EnvironmentError(`comando "${command}" não encontrado no PATH; o Docker está instalado e em execução?`));
      } else {
        reject(err);
      }
    });
    child.on('close', (code) => {
      resolve({
        code,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8'),
      });
    });
    if (input !== undefined && child.stdin) {
      // O processo pode sair sem ler a entrada inteira (EPIPE); o código de saída conta a história.
      child.stdin.on('error', () => {});
      child.stdin.end(input, 'utf8');
    }
  });
}

/** Consulta o Postgres do oráculo e devolve as linhas não vazias da saída (`psql -At`). */
export async function psql(sql: string, container: string = ORACLE_CONTAINER): Promise<string[]> {
  const result = await runProcess('docker', [
    'exec',
    '-e',
    'PGCLIENTENCODING=UTF8',
    container,
    'psql',
    '-U',
    'postgres',
    '-At',
    '-c',
    sql,
  ]);
  if (result.code !== 0) {
    const message = result.stderr.trim();
    if (/no such container|is not running|cannot connect to the docker daemon|error during connect/i.test(message)) {
      throw new EnvironmentError(`o container do oráculo (${container}) não está disponível: ${message}`);
    }
    throw new Error(`o psql do oráculo falhou (${message}) na consulta: ${sql.slice(0, 300)}`);
  }
  return result.stdout.split(/\r?\n/).filter((line) => line.trim() !== '');
}

/**
 * Confere que o oráculo responde em `baseUrl`, em modo de desenvolvimento, e que o banco dele responde
 * pelo `psql` do container. Qualquer falha é `EnvironmentError`, com mensagem em português.
 */
export async function checkOracle(baseUrl: string, container: string = ORACLE_CONTAINER): Promise<void> {
  const url = new URL('/pl', baseUrl);
  let status: number;
  let html: string;
  try {
    const res = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(CHECK_TIMEOUT_MS) });
    status = res.status;
    html = await res.text();
  } catch (err) {
    throw new EnvironmentError(
      `o oráculo não responde em ${baseUrl} (${describeError(err)}). Suba o PL local em modo de ` +
        `desenvolvimento (container ${container}; ver o guia de início rápido do motor) antes da suíte.`,
    );
  }
  if (status !== 200) {
    throw new EnvironmentError(
      `o oráculo em ${baseUrl} respondeu HTTP ${status} em /pl; em modo de desenvolvimento a resposta é 200 sem login`,
    );
  }
  if (!/<title>\s*\[DEV\]/.test(html)) {
    throw new EnvironmentError(
      `o oráculo em ${baseUrl} não está em modo de desenvolvimento (o título da página não começa com "[DEV]")`,
    );
  }
  try {
    await psql('select 1', container);
  } catch (err) {
    if (err instanceof EnvironmentError) throw err;
    throw new EnvironmentError(`o banco do oráculo não responde pelo psql do container ${container}: ${describeError(err)}`);
  }
}

export class Oracle {
  readonly baseUrl: string;
  readonly container: string;
  /** Novas tentativas do POST de correção por causa de um 400 (relógio da VM não monotônico). */
  retries = 0;
  #cookies = new Map<string, string>();
  #courseId: number | null = null;
  #questionIds = new Map<string, number>();

  constructor(baseUrl: string = ORACLE_URL, container: string = ORACLE_CONTAINER) {
    this.baseUrl = baseUrl;
    this.container = container;
  }

  psql(sql: string): Promise<string[]> {
    return psql(sql, this.container);
  }

  /** `GET /pl/loadFromDisk` e espera o job terminar com sucesso. */
  async loadFromDisk(): Promise<void> {
    const res = await this.#request('GET', '/pl/loadFromDisk');
    if (res.status !== 302 || !res.location) {
      throw new EnvironmentError(
        `o "Load from disk" do oráculo respondeu HTTP ${res.status} sem redirecionar para o job; o oráculo está em modo de desenvolvimento?`,
      );
    }
    const jobId = jobSequenceId(res.location);
    const status = await this.#waitJob(jobId);
    if (status !== 'Success') {
      throw new EnvironmentError(`o "Load from disk" do oráculo terminou com status ${status} (veja /pl/jobSequence/${jobId})`);
    }
    this.#courseId = null;
    this.#questionIds.clear();
  }

  /** Id numérico da questão do exampleCourse no banco do oráculo. */
  async questionId(qid: string): Promise<number> {
    const cached = this.#questionIds.get(qid);
    if (cached !== undefined) return cached;
    const courseId = await this.#exampleCourseId();
    const [line] = await this.psql(
      `select id from questions where course_id = ${courseId} and qid = ${sqlText(qid)} and deleted_at is null`,
    );
    if (line === undefined) {
      throw new EnvironmentError(`a questão ${qid} não existe no exampleCourse do oráculo; rode o "Load from disk"`);
    }
    const id = parseId(line, `id da questão ${qid}`);
    this.#questionIds.set(qid, id);
    return id;
  }

  /** Cria a variante pela prévia do instrutor com a seed dada e a lê da tabela `variants`. */
  async createVariant(qid: string, seed: string): Promise<OracleVariant> {
    const preview = await this.#previewPath(qid);
    const page = await this.#request('GET', `${preview}?variant_seed=${encodeURIComponent(seed)}`);
    if (page.status !== 200) {
      throw new OracleHttpError(page.status, `a prévia de ${qid} com a seed ${seed} respondeu HTTP ${page.status}: ${errorSummary(page.text)}`);
    }
    const variantId = parseId(inputValue(formHtml(page.text, 'question-form'), '__variant_id'), '__variant_id');
    const variant = await this.#variant(variantId);
    if (variant.variant_seed !== seed) {
      throw new Error(`a variante ${variantId} do oráculo tem a seed ${variant.variant_seed}, e não ${seed}`);
    }
    return variant;
  }

  /**
   * Submete uma resposta crua à variante, pelo POST `__action=grade` da prévia, e lê a submissão gravada.
   * Um 400 é repetido até 3 vezes (relógio da VM não monotônico).
   */
  async submit(qid: string, variantId: number, raw: Record<string, unknown>): Promise<OracleSubmission> {
    for (let attempt = 0; ; attempt++) {
      try {
        return await this.#submitOnce(qid, variantId, raw);
      } catch (err) {
        const delay = SUBMIT_RETRY_DELAYS_MS[attempt];
        if (!(err instanceof OracleHttpError) || err.status !== 400 || delay === undefined) throw err;
        this.retries++;
        await sleep(delay);
      }
    }
  }

  /**
   * Aciona o `test_once` do PL com o prefixo de seed e devolve as três variantes `<prefixo>-0..2` com a
   * submissão de cada uma e o tipo de teste.
   */
  async testOnce(qid: string, prefix: string): Promise<TestOnceCase[]> {
    const courseId = await this.#exampleCourseId();
    const questionId = await this.questionId(qid);
    const settings = `/pl/course/${courseId}/question/${questionId}/settings`;
    const page = await this.#request('GET', settings);
    if (page.status !== 200) {
      throw new OracleHttpError(page.status, `a página de configurações de ${qid} respondeu HTTP ${page.status}: ${errorSummary(page.text)}`);
    }
    const csrf = inputValue(formHtml(page.text, 'question-tests-form'), '__csrf_token');
    const [beforeLine] = await this.psql('select coalesce(max(id), 0) from variants');
    const before = parseId(beforeLine ?? '', 'maior id de variants');
    const res = await this.#request(
      'POST',
      `${settings}/test`,
      new URLSearchParams([
        ['__csrf_token', csrf],
        ['__action', 'test_once'],
        ['variant_seed_prefix', prefix],
      ]),
    );
    if (res.status !== 302 || !res.location) {
      throw new OracleHttpError(res.status, `o test_once de ${qid} respondeu HTTP ${res.status}: ${errorSummary(res.text)}`);
    }
    const jobId = jobSequenceId(res.location);
    const jobStatus = await this.#waitJob(jobId);

    const seeds = TEST_TYPES.map((_, i) => `${prefix}-${i}`);
    const rows = await this.#json<{ variant: OracleVariant; submissions: OracleSubmission[] }>(
      `select json_build_object('variant', row_to_json(v), 'submissions', ` +
        `(select coalesce(json_agg(row_to_json(s) order by s.id), '[]'::json) ` +
        `from (select ${SUBMISSION_COLUMNS} from submissions where variant_id = v.id) s))::text ` +
        `from (select ${VARIANT_COLUMNS} from variants where question_id = ${questionId} and id > ${before} ` +
        `and variant_seed in (${seeds.map(sqlText).join(', ')})) v order by v.id`,
    );
    const cases: TestOnceCase[] = [];
    const problems: string[] = [];
    TEST_TYPES.forEach((testType, i) => {
      const matching = rows.filter((row) => row.variant.variant_seed === seeds[i]);
      const only = matching[0];
      if (matching.length !== 1 || only === undefined) {
        problems.push(`${matching.length} variantes com a seed ${seeds[i]}`);
        return;
      }
      const submission = only.submissions[0];
      if (only.submissions.length !== 1 || submission === undefined) {
        problems.push(`${only.submissions.length} submissões na variante ${only.variant.id} (${seeds[i]})`);
        return;
      }
      if (!isFinished(submission)) {
        problems.push(`a submissão ${submission.id} (${seeds[i]}) não foi corrigida`);
        return;
      }
      cases.push({ testType, variant: only.variant, submission });
    });
    if (problems.length > 0) {
      throw new Error(
        `o test_once de ${qid} com o prefixo ${prefix} (job ${jobId}, status ${jobStatus}) não produziu as três ` +
          `variantes e submissões esperadas: ${problems.join('; ')}`,
      );
    }
    return cases;
  }

  async #submitOnce(qid: string, variantId: number, raw: Record<string, unknown>): Promise<OracleSubmission> {
    const preview = await this.#previewPath(qid);
    const page = await this.#request('GET', `${preview}?variant_id=${variantId}`);
    if (page.status !== 200) {
      throw new OracleHttpError(page.status, `a prévia da variante ${variantId} de ${qid} respondeu HTTP ${page.status}: ${errorSummary(page.text)}`);
    }
    const csrf = inputValue(formHtml(page.text, 'question-form'), '__csrf_token');
    const [beforeLine] = await this.psql(`select coalesce(max(id), 0) from submissions where variant_id = ${variantId}`);
    const before = parseId(beforeLine ?? '', 'maior id de submissions');
    const body = new URLSearchParams([
      ['__action', 'grade'],
      ['__variant_id', String(variantId)],
      ['__csrf_token', csrf],
    ]);
    for (const [key, value] of Object.entries(raw)) {
      if (value === null || value === undefined) continue;
      // Listas viram chaves repetidas, como o formulário do PL envia a escolha múltipla.
      for (const item of Array.isArray(value) ? value : [value]) body.append(key, formValue(item));
    }
    const res = await this.#request('POST', preview, body);
    if (res.status >= 400) {
      throw new OracleHttpError(res.status, `a correção no oráculo (${qid}, variante ${variantId}) respondeu HTTP ${res.status}: ${errorSummary(res.text)}`);
    }
    const [submission] = await this.#json<OracleSubmission>(
      `select row_to_json(s)::text from (select ${SUBMISSION_COLUMNS} from submissions ` +
        `where variant_id = ${variantId} and id > ${before} order by id desc limit 1) s`,
    );
    if (submission === undefined) {
      throw new Error(`o oráculo não gravou a submissão na variante ${variantId} de ${qid} (HTTP ${res.status})`);
    }
    if (!isFinished(submission)) {
      throw new Error(`a submissão ${submission.id} do oráculo (${qid}, variante ${variantId}) não foi corrigida`);
    }
    return submission;
  }

  async #variant(variantId: number): Promise<OracleVariant> {
    const [variant] = await this.#json<OracleVariant>(
      `select row_to_json(v)::text from (select ${VARIANT_COLUMNS} from variants where id = ${variantId}) v`,
    );
    if (variant === undefined) throw new Error(`a variante ${variantId} não está na tabela variants do oráculo`);
    return variant;
  }

  async #previewPath(qid: string): Promise<string> {
    const courseId = await this.#exampleCourseId();
    return `/pl/course/${courseId}/question/${await this.questionId(qid)}/preview`;
  }

  async #exampleCourseId(): Promise<number> {
    if (this.#courseId !== null) return this.#courseId;
    const [line] = await this.psql(
      `select id from courses where path = ${sqlText(EXAMPLE_COURSE_PATH)} and deleted_at is null order by id limit 1`,
    );
    if (line === undefined) {
      throw new EnvironmentError(`o exampleCourse (${EXAMPLE_COURSE_PATH}) não está carregado no oráculo; rode o "Load from disk"`);
    }
    this.#courseId = parseId(line, 'id do exampleCourse');
    return this.#courseId;
  }

  async #waitJob(jobId: number): Promise<string> {
    const deadline = Date.now() + JOB_TIMEOUT_MS;
    for (;;) {
      const [status] = await this.psql(`select status from job_sequences where id = ${jobId}`);
      if (status !== undefined && status !== 'Running' && status !== 'Stopping') return status;
      if (Date.now() > deadline) {
        throw new Error(`o job ${jobId} do oráculo não terminou em ${JOB_TIMEOUT_MS / 1000} s (status ${status ?? 'ausente'})`);
      }
      await sleep(JOB_POLL_MS);
    }
  }

  async #json<T>(sql: string): Promise<T[]> {
    return (await this.psql(sql)).map((line) => JSON.parse(line) as T);
  }

  async #request(
    method: 'GET' | 'POST',
    pathAndQuery: string,
    form?: URLSearchParams,
  ): Promise<{ status: number; location: string | null; text: string }> {
    const headers: Record<string, string> = {};
    if (this.#cookies.size > 0) {
      headers.cookie = [...this.#cookies].map(([name, value]) => `${name}=${value}`).join('; ');
    }
    if (form) headers['content-type'] = 'application/x-www-form-urlencoded';
    let res: Response;
    try {
      res = await fetch(new URL(pathAndQuery, this.baseUrl), {
        method,
        headers,
        body: form?.toString(),
        redirect: 'manual',
        signal: AbortSignal.timeout(HTTP_TIMEOUT_MS),
      });
    } catch (err) {
      throw new EnvironmentError(`o oráculo parou de responder (${method} ${pathAndQuery}): ${describeError(err)}`);
    }
    for (const cookie of res.headers.getSetCookie()) {
      const pair = cookie.split(';', 1)[0] ?? '';
      const eq = pair.indexOf('=');
      if (eq > 0) this.#cookies.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim());
    }
    return { status: res.status, location: res.headers.get('location'), text: await res.text() };
  }
}

/** Valor de formulário enviado ao oráculo: texto puro; booleano; o resto em JSON. */
export function formValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return JSON.stringify(value) ?? 'null';
}

function isFinished(submission: OracleSubmission): boolean {
  return !submission.gradable || submission.broken || submission.graded_at !== null;
}

function jobSequenceId(location: string): number {
  const match = /\/jobSequence\/(\d+)/.exec(location);
  if (!match?.[1]) throw new Error(`redirecionamento inesperado do oráculo: ${location}`);
  return parseId(match[1], 'id do job');
}

function parseId(text: string, what: string): number {
  const trimmed = text.trim();
  if (!/^\d+$/.test(trimmed)) throw new Error(`${what} inválido vindo do oráculo: ${JSON.stringify(text)}`);
  return Number(trimmed);
}

function sqlText(value: string): string {
  return `'${value.replaceAll("'", "''")}'`;
}

function formHtml(html: string, name: string): string {
  const match = new RegExp(`<form\\b[^>]*\\bname="${name}"[^>]*>[\\s\\S]*?</form>`).exec(html);
  if (!match) throw new Error(`formulário "${name}" não encontrado na página do oráculo`);
  return match[0];
}

function inputValue(html: string, name: string): string {
  for (const [tag] of html.matchAll(/<input\b[^>]*>/g)) {
    if (!new RegExp(`\\bname="${name}"`).test(tag)) continue;
    const value = /\bvalue="([^"]*)"/.exec(tag);
    if (value?.[1] !== undefined) return decodeEntities(value[1]);
  }
  throw new Error(`campo "${name}" não encontrado no formulário do oráculo`);
}

function decodeEntities(text: string): string {
  return text
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&amp;', '&');
}

function errorSummary(html: string): string {
  const text = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  const match = /(Error|Erro)[^.]{0,300}/.exec(text);
  return match ? match[0] : text.slice(0, 300);
}

function describeError(err: unknown): string {
  if (err instanceof Error) {
    const cause = err.cause instanceof Error ? `: ${err.cause.message}` : '';
    return `${err.message}${cause}`;
  }
  return String(err);
}
