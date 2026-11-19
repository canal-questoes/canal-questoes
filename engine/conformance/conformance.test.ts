// Suíte de conformidade do motor contra o oráculo (resultados iguais aos do PL, relatório, oráculo fora do ar, motor restrito).
// Roda no HOST, com o `pl-oracle` no ar:
//   node --test "engine/conformance/*.test.ts"
//
// - Antes: `checkOracle`, `loadFromDisk` e a checagem da imagem `canal-questoes/engine:test`.
// - Seeds: as 5 questões de referência × N seeds (`CQ_CONFORMANCE_SEEDS`, padrão 20), em base 36,
//   determinísticas (`seeds()` de `engine/test/helpers/fixtures.ts`).
// - Oráculo (primeiro): por questão × seed, o `test_once` do PL com o prefixo = seed (3 variantes
//   `<seed>-0..2` e 3 submissões corrigidas pelo PL), a variante da prévia com a seed, e os casos à mão de
//   `cases.ts` submetidos a essa variante, partindo da resposta `correct` do `test_once`.
// - Motor, restrito (`--network none`, não-root, somente leitura): UMA execução da CLI cria
//   as variantes (seed = prefixo, porque o `parseInt` do PL para no `-`), comparadas com as
//   4 variantes do oráculo; UMA execução corrige todos os `raw_submitted_answer` que o oráculo gravou.
//   A correção usa o snapshot do próprio motor, com `params` e `correct_answers` da variante que
//   o oráculo corrigiu: quando as variantes são iguais (conferido à parte), isso é o snapshot do motor, e
//   uma divergência de variante não contamina a comparação da correção.
// - Avisos da correção: `compareGrade` exige `warnings: []` em toda correção ok
//   do motor, porque as 5 questões não imprimem. A premissa é conferida no oráculo a cada execução: o PL
//   grava uma issue "output logged on console" na variante para cada chamada com saída no console, do
//   elemento ou do `server.py` (`question-servers/freeform.js`; `lib/grading.js` → `writeCourseIssues`)
//   [F], e nenhuma das variantes coletadas pode ter uma.
// - Vírgula: nos casos à mão `virgula_decimal` e `virgula_em_inteiro`, além da comparação, o
//   oráculo e o motor têm de dar erro de formato só no c_1, sem nota.
// - IR: validadas por `validate-schemas.ts` numa execução da imagem de teste.
// - Exigências: 0 divergências; p95 < 1000 ms em `create_variant` e em `grade`, excluída a primeira
//   chamada de cada execução da CLI (subida do zygote); duração total < 30 min.
// - Relatório: `engine/conformance/resultados/conformidade-<AAAAMMDD-HHMMSS>.json`.
// - Injeção (ponta a ponta): com `CQ_CONFORMANCE_INJECT=score`, o harness (e só ele) altera
//   o `score` de uma resposta do motor antes da comparação. A suíte então DEVE falhar em "correções
//   idênticas", e o caso de injeção exige que a divergência seja relatada com questão, seed, caso e campo.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { after, before, describe, it } from 'node:test';

import { EXAMPLE, seeds as makeSeeds } from '../test/helpers/fixtures.ts';
import { handmadeCases } from './cases.ts';
import { compareGrade, compareVariant, formatReport, type CaseKey, type Divergence } from './compare.ts';
import { checkImage, runEngine, runInTestImage, type CliRequest, type CliResult, type CliRun } from './engine-cli.ts';
import { checkOracle, Oracle, OracleHttpError, ORACLE_URL, type OracleSubmission, type OracleVariant } from './oracle.ts';

const QUESTIONS = [
  'element/multipleChoice',
  'element/checkbox',
  'element/numberInput',
  'element/integerInput',
  'element/stringInput',
] as const;

const DEFAULT_SEEDS = 20;
const P95_LIMIT_MS = 1000;
const TOTAL_LIMIT_MS = 30 * 60 * 1000;
const RESULTS_DIR = path.join(import.meta.dirname, 'resultados');
/** Divergências listadas nas mensagens de falha; a lista completa fica no relatório JSON. */
const MESSAGE_LIMIT = 40;

interface VariantCase extends CaseKey {
  oracleVariant: OracleVariant;
}

interface GradeCase extends CaseKey {
  oracleVariant: OracleVariant;
  oracleSubmission: OracleSubmission;
}

interface OracleFailure extends CaseKey {
  error: string;
}

interface Injection extends CaseKey {
  requestId: string;
  original: unknown;
  injected: number;
}

interface SuiteState {
  startedAt: Date;
  reportFile: string;
  seeds: string[];
  inject: 'score' | null;
  variantCases: VariantCase[];
  gradeCases: GradeCase[];
  oracleFailures: OracleFailure[];
  oracleRetries: number;
  /** Issues de saída no console que o oráculo gravou nas variantes coletadas (`<variant_id>: <mensagem>`). */
  oracleConsoleIssues: string[];
  variantRun: CliRun | null;
  gradeRun: CliRun | null;
  firstIds: { variant: string | null; grade: string | null };
  variantDivergences: Divergence[];
  gradeDivergences: Divergence[];
  injection: Injection | null;
  irValidated: number;
  irExpected: number;
  irErrors: Array<{ id: string; message: string }>;
  elapsedMs: number | null;
  error: string | null;
}

let state: SuiteState | null = null;

/**
 * O estado da coleta. Se a coleta falhou (oráculo fora do ar, imagem ausente, CLI do motor com erro),
 * cada teste falha com essa causa, e não com um falso "tudo igual".
 */
function current(): SuiteState {
  assert.ok(state, 'a coleta da conformidade não rodou');
  if (state.error !== null) assert.fail(`a coleta da conformidade falhou: ${state.error}`);
  return state;
}

function variantRequestId(qid: string, seed: string): string {
  return `${qid}|${seed}`;
}

/** Só a chave do caso, sem os dados do oráculo (é o que vai para cada divergência). */
function keyOf(c: CaseKey): CaseKey {
  return { qid: c.qid, seed: c.seed, case: c.case };
}

function gradeRequestId(key: CaseKey): string {
  return `${key.qid}|${key.seed}|${key.case}`;
}

const MIN_SEEDS = 20;

function parseSeedCount(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === '') return DEFAULT_SEEDS;
  const n = Number(raw);
  // O mínimo é 20 seeds por questão: valores menores são recusados, para que a
  // suíte nunca passe com uma amostra menor que a exigida.
  if (!Number.isInteger(n) || n < MIN_SEEDS) {
    throw new Error(`CQ_CONFORMANCE_SEEDS inválido: ${JSON.stringify(raw)} (use um inteiro ≥ ${MIN_SEEDS})`);
  }
  return n;
}

function parseInject(raw: string | undefined): 'score' | null {
  if (raw === undefined || raw.trim() === '') return null;
  if (raw === 'score') return 'score';
  throw new Error(`CQ_CONFORMANCE_INJECT desconhecido: ${JSON.stringify(raw)} (o único valor aceito é "score")`);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

/** p95 pelo método do posto mais próximo. */
function p95(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil(0.95 * sorted.length) - 1] ?? null;
}

/** Tempos de uma execução da CLI, sem a primeira chamada (subida do zygote). */
function timings(run: CliRun | null, firstId: string | null): number[] {
  if (!run) return [];
  return run.results.filter((r) => r.id !== firstId).map((r) => r.ms);
}

function stamp(date: Date): string {
  const pad = (n: number): string => String(n).padStart(2, '0');
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-` +
    `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

/** Oráculo, para uma questão: test_once, variante da prévia e casos à mão, seed a seed. */
async function collectOracle(oracle: Oracle, qid: string, seeds: readonly string[], s: SuiteState): Promise<void> {
  for (const seed of seeds) {
    const tests = await oracle.testOnce(qid, seed);
    for (const t of tests) {
      s.variantCases.push({ qid, seed, case: `variante:test:${t.testType}`, oracleVariant: t.variant });
      s.gradeCases.push({ qid, seed, case: `test:${t.testType}`, oracleVariant: t.variant, oracleSubmission: t.submission });
    }
    const preview = await oracle.createVariant(qid, seed);
    s.variantCases.push({ qid, seed, case: 'variante:preview', oracleVariant: preview });
    const correct = tests.find((t) => t.testType === 'correct');
    assert.ok(correct, `o test_once de ${qid} (${seed}) não trouxe o caso correct`);
    for (const hc of handmadeCases(qid, asRecord(preview.true_answer), asRecord(correct.submission.raw_submitted_answer))) {
      const key = { qid, seed, case: `mao:${hc.name}` };
      try {
        const submission = await oracle.submit(qid, preview.id, hc.raw);
        s.gradeCases.push({ ...key, oracleVariant: preview, oracleSubmission: submission });
      } catch (err) {
        if (!(err instanceof OracleHttpError)) throw err;
        s.oracleFailures.push({ ...key, error: err.message });
      }
    }
  }
  console.log(`oráculo: ${qid} × ${seeds.length} seeds coletado`);
}

async function collect(s: SuiteState): Promise<void> {
  const t0 = performance.now();
  await checkOracle(ORACLE_URL);
  const oracle = new Oracle(ORACLE_URL);
  await oracle.loadFromDisk();
  await checkImage();

  // 1. Oráculo. As questões são independentes (variantes distintas) e rodam em paralelo.
  await Promise.all(QUESTIONS.map((qid) => collectOracle(oracle, qid, s.seeds, s)));
  s.oracleRetries = oracle.retries;
  // Ordem determinística no relatório, qualquer que tenha sido a ordem de chegada.
  const qids: readonly string[] = QUESTIONS;
  const order = (a: CaseKey, b: CaseKey): number =>
    qids.indexOf(a.qid) - qids.indexOf(b.qid) || s.seeds.indexOf(a.seed) - s.seeds.indexOf(b.seed) || a.case.localeCompare(b.case);
  s.variantCases.sort(order);
  s.gradeCases.sort(order);
  // Premissa do `warnings: []` esperado nas correções (compare.ts): o oráculo não registrou saída no console.
  s.oracleConsoleIssues = await oracleConsoleIssues(oracle, [...s.variantCases, ...s.gradeCases]);

  // 2. Motor: uma execução da CLI para todas as variantes.
  const variantRequests: CliRequest[] = [];
  for (const qid of QUESTIONS) {
    for (const seed of s.seeds) {
      variantRequests.push({
        id: variantRequestId(qid, seed),
        op: 'create_variant',
        body: { question_ref: { repo: EXAMPLE.repo, commit: EXAMPLE.commit, qid }, seed },
      });
    }
  }
  s.firstIds.variant = variantRequests[0]?.id ?? null;
  s.variantRun = await runEngine(variantRequests);
  const variantRun = s.variantRun;
  for (const vc of s.variantCases) {
    s.variantDivergences.push(
      ...compareVariant(keyOf(vc), variantRun.byId.get(variantRequestId(vc.qid, vc.seed)), vc.oracleVariant),
    );
  }

  // 3. Motor: uma execução da CLI para todas as correções, com a resposta crua que o oráculo gravou.
  const gradeRequests: CliRequest[] = [];
  const graded: GradeCase[] = [];
  for (const gc of s.gradeCases) {
    const created = variantRun.byId.get(variantRequestId(gc.qid, gc.seed));
    const createdBody = asRecord(created?.body);
    if (created?.status !== 200 || createdBody.outcome !== 'ok') {
      // Sem variante do motor não há snapshot para corrigir; a divergência da variante já foi acusada.
      s.gradeDivergences.push({
        ...keyOf(gc),
        field: 'snapshot',
        engine: `sem variante ok do motor (HTTP ${created?.status}, outcome ${String(createdBody.outcome)})`,
        oracle: `variante ${gc.oracleVariant.id}`,
      });
      continue;
    }
    gradeRequests.push({
      id: gradeRequestId(gc),
      op: 'grade',
      body: {
        question_ref: { repo: EXAMPLE.repo, commit: EXAMPLE.commit, qid: gc.qid },
        snapshot: {
          ...asRecord(createdBody.snapshot),
          params: gc.oracleVariant.params,
          correct_answers: gc.oracleVariant.true_answer,
        },
        raw_submitted_answers: gc.oracleSubmission.raw_submitted_answer ?? {},
      },
    });
    graded.push(gc);
  }
  if (gradeRequests.length > 0) {
    s.firstIds.grade = gradeRequests[0]?.id ?? null;
    s.gradeRun = await runEngine(gradeRequests);
    const gradeRun = s.gradeRun;
    if (s.inject === 'score') s.injection = injectScore(gradeRun, graded);
    for (const gc of graded) {
      s.gradeDivergences.push(...compareGrade(keyOf(gc), gradeRun.byId.get(gradeRequestId(gc)), gc.oracleSubmission));
    }
  }

  // 4. IR: validadas dentro da imagem de teste, com o helper independente de esquemas.
  const okBodies = variantRun.results.filter((r) => r.status === 200);
  s.irExpected = okBodies.length;
  if (okBodies.length > 0) {
    const run = await runInTestImage(['node', 'conformance/validate-schemas.ts'], JSON.stringify(okBodies.map((r) => r.body)));
    let parsed: { validated?: unknown; errors?: unknown };
    try {
      parsed = JSON.parse(run.stdout) as { validated?: unknown; errors?: unknown };
    } catch {
      throw new Error(`validate-schemas.ts saiu com código ${run.code} sem relatório JSON; stderr: ${run.stderr.slice(-2000)}`);
    }
    s.irValidated = typeof parsed.validated === 'number' ? parsed.validated : 0;
    const errors = Array.isArray(parsed.errors) ? (parsed.errors as Array<{ index: number; message: string }>) : [];
    s.irErrors = errors.map((e) => ({ id: okBodies[e.index]?.id ?? `#${e.index}`, message: e.message }));
    if (run.code !== 0 && errors.length === 0) {
      throw new Error(`validate-schemas.ts saiu com código ${run.code} sem listar erros; stderr: ${run.stderr.slice(-2000)}`);
    }
  }
  s.elapsedMs = performance.now() - t0;
}

/**
 * Issues "output logged on console" que o oráculo gravou nas variantes dos casos (o PL as grava por variante,
 * uma por chamada com saída; `lib/issues.js`, `instructor_message` = `err.toString()`) [F].
 */
async function oracleConsoleIssues(
  oracle: Oracle,
  cases: ReadonlyArray<{ oracleVariant: OracleVariant }>,
): Promise<string[]> {
  const ids = [...new Set(cases.map((c) => c.oracleVariant.id))].sort((a, b) => a - b);
  if (ids.length === 0) return [];
  return oracle.psql(
    `select variant_id || ': ' || instructor_message from issues where variant_id in (${ids.join(', ')}) ` +
      `and instructor_message like '%output logged on console%' order by id`,
  );
}

/** Altera, só no harness, o score da primeira correção `ok` do motor (a divergência injetada). */
function injectScore(run: CliRun, graded: readonly GradeCase[]): Injection | null {
  for (const gc of graded) {
    const requestId = gradeRequestId(gc);
    const result: CliResult | undefined = run.byId.get(requestId);
    if (!result || result.status !== 200 || asRecord(result.body).outcome !== 'ok') continue;
    const body = asRecord(result.body);
    const original = body.score;
    const injected = typeof original === 'number' ? original + 0.5 : 0.5;
    result.body = { ...body, score: injected };
    return { qid: gc.qid, seed: gc.seed, case: gc.case, requestId, original, injected };
  }
  return null;
}

/** Mensagem de falha: as primeiras divergências e onde está o relatório completo. */
function failureReport(s: SuiteState, divergences: readonly Divergence[]): string {
  return `${formatReport(divergences, { limit: MESSAGE_LIMIT })}\nrelatório completo: ${s.reportFile}`;
}

function writeReport(s: SuiteState): string {
  const variantP95 = p95(timings(s.variantRun, s.firstIds.variant));
  const gradeP95 = p95(timings(s.gradeRun, s.firstIds.grade));
  const byCase = (list: readonly Divergence[], key: CaseKey): Divergence[] =>
    list.filter((d) => d.qid === key.qid && d.seed === key.seed && d.case === key.case);
  const report = {
    started_at: s.startedAt.toISOString(),
    elapsed_ms: s.elapsedMs,
    questions: QUESTIONS,
    seeds: s.seeds,
    inject: s.inject,
    injection: s.injection,
    error: s.error,
    summary: {
      variants: { cases: s.variantCases.length, divergences: s.variantDivergences.length },
      submissions: {
        cases: s.gradeCases.length,
        divergences: s.gradeDivergences.length,
        oracle_failures: s.oracleFailures.length,
      },
      ir: { expected: s.irExpected, validated: s.irValidated, errors: s.irErrors.length },
      p95_ms: { create_variant: variantP95, grade: gradeP95 },
      oracle_retries: s.oracleRetries,
      oracle_console_issues: s.oracleConsoleIssues.length,
      cli_wall_ms: { create_variant: s.variantRun?.wallMs ?? null, grade: s.gradeRun?.wallMs ?? null },
    },
    variants: s.variantCases.map((vc) => ({
      qid: vc.qid,
      seed: vc.seed,
      case: vc.case,
      oracle_variant_id: vc.oracleVariant.id,
      oracle_variant_seed: vc.oracleVariant.variant_seed,
      engine_ms: s.variantRun?.byId.get(variantRequestId(vc.qid, vc.seed))?.ms ?? null,
      divergences: byCase(s.variantDivergences, vc),
    })),
    submissions: s.gradeCases.map((gc) => ({
      qid: gc.qid,
      seed: gc.seed,
      case: gc.case,
      oracle_variant_id: gc.oracleVariant.id,
      oracle_submission_id: gc.oracleSubmission.id,
      engine_ms: s.gradeRun?.byId.get(gradeRequestId(gc))?.ms ?? null,
      divergences: byCase(s.gradeDivergences, gc),
    })),
    oracle_failures: s.oracleFailures,
    oracle_console_issues: s.oracleConsoleIssues,
    ir_errors: s.irErrors,
    engine_stderr_tail: {
      create_variant: s.variantRun?.stderr.slice(-4000) ?? null,
      grade: s.gradeRun?.stderr.slice(-4000) ?? null,
    },
  };
  fs.mkdirSync(RESULTS_DIR, { recursive: true });
  fs.writeFileSync(s.reportFile, JSON.stringify(report, null, 1) + '\n', 'utf8');
  return s.reportFile;
}

describe('conformidade do motor contra o oráculo (5 questões de referência)', () => {
  before(async () => {
    const startedAt = new Date();
    const s: SuiteState = {
      startedAt,
      reportFile: path.join(RESULTS_DIR, `conformidade-${stamp(startedAt)}.json`),
      seeds: makeSeeds(parseSeedCount(process.env.CQ_CONFORMANCE_SEEDS)),
      inject: parseInject(process.env.CQ_CONFORMANCE_INJECT),
      variantCases: [],
      gradeCases: [],
      oracleFailures: [],
      oracleRetries: 0,
      oracleConsoleIssues: [],
      variantRun: null,
      gradeRun: null,
      firstIds: { variant: null, grade: null },
      variantDivergences: [],
      gradeDivergences: [],
      injection: null,
      irValidated: 0,
      irExpected: 0,
      irErrors: [],
      elapsedMs: null,
      error: null,
    };
    state = s;
    try {
      await collect(s);
    } catch (err) {
      // Não relança: cada teste abaixo falha com esta causa (ver current()).
      s.error = err instanceof Error ? `${err.name}: ${err.message}` : String(err);
    }
  });

  after(() => {
    if (state) console.log(`relatório da conformidade: ${writeReport(state)}`);
  });

  it('variantes idênticas às do oráculo: preview e as 3 do test_once, por questão × seed', () => {
    const s = current();
    assert.equal(
      s.variantCases.length,
      QUESTIONS.length * s.seeds.length * 4,
      'a coleta não cobriu as 4 variantes do oráculo por questão × seed',
    );
    assert.equal(s.variantDivergences.length, 0, failureReport(s, s.variantDivergences));
  });

  it('correções idênticas às do oráculo para a mesma resposta crua: test_once e casos à mão', () => {
    const s = current();
    for (const qid of QUESTIONS) {
      for (const seed of s.seeds) {
        const cases = s.gradeCases.filter((gc) => gc.qid === qid && gc.seed === seed).map((gc) => gc.case);
        for (const expected of ['test:correct', 'test:incorrect', 'test:invalid', 'mao:vazio']) {
          assert.ok(cases.includes(expected), `falta o caso ${expected} de ${qid} (${seed}) na coleta`);
        }
      }
    }
    assert.deepEqual(s.oracleFailures, [], 'o oráculo não corrigiu alguns casos (nada foi comparado para eles)');
    assert.deepEqual(
      s.oracleConsoleIssues,
      [],
      'o oráculo registrou saída no console nestas variantes: o esperado "warnings: []" das correções (compare.ts) ' +
        'deixa de valer e precisa ser revisto (avisos de saída no console)',
    );
    const note = s.injection ? '\n(inclui a divergência injetada por CQ_CONFORMANCE_INJECT=score)' : '';
    assert.equal(s.gradeDivergences.length, 0, failureReport(s, s.gradeDivergences) + note);
  });

  it('vírgula num campo numérico é erro de formato do próprio campo, no oráculo e no motor', () => {
    const s = current();
    // Casos de cases.ts: o gabarito do c_1 escrito com vírgula e os demais campos com a resposta correta.
    const commaCases = [
      { qid: 'element/numberInput', name: 'mao:virgula_decimal' },
      { qid: 'element/integerInput', name: 'mao:virgula_em_inteiro' },
    ];
    for (const { qid, name } of commaCases) {
      for (const seed of s.seeds) {
        const where = `${qid} (${seed}, ${name})`;
        const gc = s.gradeCases.find((c) => c.qid === qid && c.seed === seed && c.case === name);
        assert.ok(gc, `falta o caso ${where} na coleta`);
        // Premissa (o caso testa o que pretende): no PL a193f72, só o c_1 dá erro de formato, sem nota
        // (conferido no pl-oracle com as seeds 1oo1irw, xjb8jh, 6ekyb2 e 1eayq1r).
        const sub = gc.oracleSubmission;
        assert.deepEqual(
          {
            gradable: sub.gradable,
            broken: sub.broken,
            format_errors: Object.keys(asRecord(sub.format_errors)),
            c_1: asRecord(sub.submitted_answer).c_1,
            score: sub.score,
            partial_scores: sub.partial_scores,
          },
          { gradable: false, broken: false, format_errors: ['c_1'], c_1: null, score: null, partial_scores: null },
          `${where}: o oráculo não tratou a vírgula como erro de formato do c_1, e o caso deixou de testar a vírgula decimal`,
        );
        // Motor: o mesmo desfecho, sem normalizar a vírgula (`score` e `partial_scores` null
        // quando `valid` é falso). O html e o texto da mensagem são comparados com os do oráculo pela
        // `compareGrade`, no caso "correções idênticas".
        const requestId = gradeRequestId(gc);
        if (s.injection?.requestId === requestId) continue;
        const result = s.gradeRun?.byId.get(requestId);
        assert.equal(result?.status, 200, `${where}: status da correção no motor`);
        const body = asRecord(result?.body);
        assert.deepEqual(
          {
            outcome: body.outcome,
            valid: body.valid,
            format_errors: Object.keys(asRecord(body.format_errors)),
            c_1: asRecord(body.submitted_answers).c_1,
            score: body.score,
            partial_scores: body.partial_scores,
          },
          { outcome: 'ok', valid: false, format_errors: ['c_1'], c_1: null, score: null, partial_scores: null },
          `${where}: o motor precisa tratar a vírgula como erro de formato do c_1, como o PL`,
        );
      }
    }
  });

  it('toda IR gerada valida contra ir.schema.json, dentro da imagem de teste', () => {
    const s = current();
    assert.ok(s.irExpected > 0, 'nenhuma variante do motor para validar');
    assert.equal(s.irValidated, s.irExpected, 'validate-schemas.ts não validou todos os corpos');
    const shown = s.irErrors.slice(0, 5).map((e) => `${e.id}: ${e.message.slice(0, 1500)}`);
    assert.equal(
      s.irErrors.length,
      0,
      `${s.irErrors.length} IR fora do esquema:\n${shown.join('\n')}\nrelatório completo: ${s.reportFile}`,
    );
  });

  it('p95 < 1000 ms em create_variant e em grade, sem a primeira chamada de cada execução da CLI', () => {
    const s = current();
    const variantMs = timings(s.variantRun, s.firstIds.variant);
    const gradeMs = timings(s.gradeRun, s.firstIds.grade);
    assert.ok(variantMs.length > 0 && gradeMs.length > 0, 'sem tempos suficientes para o p95');
    const variantP95 = p95(variantMs) ?? Infinity;
    const gradeP95 = p95(gradeMs) ?? Infinity;
    assert.ok(variantP95 < P95_LIMIT_MS, `p95 de create_variant = ${variantP95} ms (limite ${P95_LIMIT_MS} ms)`);
    assert.ok(gradeP95 < P95_LIMIT_MS, `p95 de grade = ${gradeP95} ms (limite ${P95_LIMIT_MS} ms)`);
  });

  it('a suíte completa roda em menos de 30 min', () => {
    const s = current();
    assert.ok(s.elapsedMs !== null, 'a coleta não terminou');
    assert.ok(s.elapsedMs < TOTAL_LIMIT_MS, `a suíte levou ${Math.round(s.elapsedMs / 1000)} s (limite 1800 s)`);
  });

  it(
    'uma divergência injetada no score é acusada com questão, seed, caso e campo (ponta a ponta)',
    { skip: process.env.CQ_CONFORMANCE_INJECT === 'score' ? false : 'rode com CQ_CONFORMANCE_INJECT=score para este caso' },
    () => {
      const s = current();
      const injection = s.injection;
      assert.ok(injection, 'nenhuma correção ok do motor para receber a divergência injetada');
      const hits = s.gradeDivergences.filter(
        (d) => d.qid === injection.qid && d.seed === injection.seed && d.case === injection.case && d.field === 'score',
      );
      assert.equal(hits.length, 1, `a divergência injetada não foi acusada no campo score:\n${failureReport(s, s.gradeDivergences)}`);
      assert.equal(hits[0]?.engine, injection.injected, 'a divergência acusada não traz o valor injetado');
      const report = formatReport(s.gradeDivergences);
      for (const text of [injection.qid, injection.seed, injection.case, 'score']) {
        assert.ok(report.includes(text), `o relatório não cita "${text}":\n${report}`);
      }
    },
  );
});
