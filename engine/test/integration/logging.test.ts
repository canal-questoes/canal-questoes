// Registros sem dados sensíveis (identidade só pela revelação).
// Cobre: os registros (uma linha por pedido, sem respostas cruas, sem technical_detail nem saída do console
// nas fases parse/grade, errorLogger do CodeCallerNative só com o evento fixo); o aviso de console (o eco no
// console vira aviso, cuja mensagem não traz o texto ecoado); as quebras
// por exceção e por tempo esgotado no grade; o technical_detail, que nunca inclui
// os dados da chamada, o lastCallData).
// O marcador CANARIO-RESPOSTA-9f2c faz o papel da resposta de um aluno: não pode aparecer em nenhum
// registro, nem no motor em processo (captureLog) nem na stderr da CLI. Nas quebras em que o código
// da questão não ecoa a resposta (grade-timeout e parse-exception), também não pode aparecer no corpo
// da resposta, porque só chegaria lá pelo lastCallData. Em grade-echo, que ecoa de propósito, o
// marcador tem de estar no technical_detail (a saída do Python), o que prova que o eco chegou ao motor.
// Os avisos de cada caso são conferidos por valor: console_output só quando a operação concluiu.
// Também: reason.message e warnings[].message funcionais, sem o marcador ecoado e sem o
// caminho absoluto do servidor, também pela CLI.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { test } from 'node:test';
import util from 'node:util';

import type { Engine } from '../../src/engine.ts';
import { captureLog, ok200, withEngine, type LogEntry } from '../helpers/engine-harness.ts';
import { TEST_COURSE, assertPortuguese, ref } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

const MARKER = 'CANARIO-RESPOSTA-9f2c';
const APP_DIR = path.resolve(import.meta.dirname, '..', '..');

/** Área de repositórios do motor, com o mesmo padrão de ir.test.ts e inspect.test.ts. */
const REPOS_ROOT = process.env.ENGINE_REPOS_ROOT ?? '/repos';

/**
 * Trechos de caminho absoluto do servidor. Em reason.message e em
 * warnings[].message, um caminho como /repos/… é detalhe técnico. O diretório de fixtures/cq-test-course
 * na área é o sha256_hex do repo (vetores de teste). [F] No PL a193f72 (freeform.js,
 * processQuestionServer), as mensagens de quebra do server.py vêm prefixadas com o caminho do server.py.
 * O "/PrairieLearn/" solto fica de fora, porque é parte do repo do EXAMPLE, que uma mensagem pode citar.
 * Mesma lista de create-variant.test.ts.
 */
const SERVER_PATH_FRAGMENTS = [
  ...new Set([
    `${REPOS_ROOT}/`,
    '/repos/',
    '511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d',
    '70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7',
    '/PrairieLearn/apps/',
    '/PrairieLearn/exampleCourse/',
  ]),
];

function assertNoServerPath(message: string, where: string): void {
  for (const fragment of SERVER_PATH_FRAGMENTS) {
    assert.ok(
      !message.includes(fragment),
      `${where}: a mensagem traz o caminho do servidor "${fragment}", que é detalhe técnico: ${message}`,
    );
  }
}

/**
 * O motivo funcional de uma quebra: em português, sem o marcador e sem caminho do servidor
 * (frase funcional, sem detalhe técnico). Em grade-echo, a exceção traz a resposta crua (o marcador), que é
 * detalhe técnico e só pode ir para o technical_detail.
 */
function assertFunctionalReason(body: any, where: string): void {
  assertPortuguese(body.reason.message);
  assert.ok(!String(body.reason.message).includes(MARKER), `o marcador vazou para reason.message (${where}): ${body.reason.message}`);
  assertNoServerPath(body.reason.message, `${where}: reason.message`);
}

type Expected = { qid: string; outcome: string };

/** Serializa a entrada de todas as formas relevantes (inclusive objetos Error, que o JSON esconde). */
function render(entry: unknown): string {
  let json = '';
  try {
    json = JSON.stringify(entry) ?? '';
  } catch {
    json = '';
  }
  return json + '\n' + util.inspect(entry, { depth: null, maxArrayLength: null, maxStringLength: null, breakLength: Infinity });
}

function assertNoMarker(entries: unknown[], where: string): void {
  entries.forEach((entry, i) => {
    assert.ok(!render(entry).includes(MARKER), `o marcador vazou para o registro (${where}, entrada ${i}): ${render(entry).slice(0, 1500)}`);
  });
}

/**
 * O corpo inteiro da resposta sem o marcador. Só vale para quebras em que o código da questão não ecoa
 * a resposta (nem na exceção nem no console): aí o marcador só chegaria ao corpo pelo lastCallData, que
 * o technical_detail nunca inclui.
 */
function assertNoMarkerInBody(body: unknown, where: string): void {
  const text = JSON.stringify(body) ?? '';
  assert.ok(!text.includes(MARKER), `o marcador vazou para o corpo da resposta (${where}): ${text.slice(0, 1500)}`);
}

/**
 * Os avisos sem o marcador ecoado no console: warnings[].message é funcional, em português e sem
 * detalhe técnico, e a saída do Python é detalhe técnico. O corpo inteiro não
 * serve aqui, porque submitted_answers traz a resposta do aluno de propósito.
 */
function assertWarningsWithoutMarker(body: any, where: string): void {
  for (const w of body.warnings) {
    assertPortuguese(w.message);
    assert.ok(!String(w.message).includes(MARKER), `o marcador ecoado vazou para a mensagem do aviso (${where}): ${w.message}`);
    assertNoServerPath(w.message, `${where}: warnings[].message`);
  }
}

/** Pares "code/phase" distintos dos avisos, em ordem alfabética (a ordem dos avisos não é fixada). */
function warningPairs(body: any): string[] {
  return [...new Set<string>(body.warnings.map((w: any) => `${w.code}/${w.phase}`))].sort();
}

/**
 * Exatamente uma entrada de pedido (com op, qid, outcome e ms) por pedido. A comparação não depende
 * da ordem das entradas (a CLI pode atender os pedidos de um lote em paralelo).
 */
function assertRequestEntries(entries: LogEntry[], expected: Expected[], where: string): void {
  const requests = entries.filter((e) => e !== null && typeof e === 'object' && 'op' in e);
  assert.equal(
    requests.length,
    expected.length,
    `${where}: esperadas ${expected.length} entradas de pedido, vieram ${requests.length}: ${JSON.stringify(entries).slice(0, 1500)}`,
  );
  requests.forEach((entry, i) => {
    assert.ok(typeof entry.op === 'string' && entry.op.length > 0, `${where}: entrada de pedido ${i} sem op`);
    assert.ok(typeof entry.qid === 'string', `${where}: entrada de pedido ${i} sem qid`);
    assert.ok(typeof entry.outcome === 'string', `${where}: entrada de pedido ${i} sem outcome`);
    assert.ok(
      typeof entry.ms === 'number' && Number.isFinite(entry.ms) && entry.ms >= 0,
      `${where}: entrada de pedido ${i} sem ms numérico`,
    );
  });
  const key = (e: Record<string, unknown>): string => `${String(e.qid)} → ${String(e.outcome)}`;
  assert.deepEqual(
    requests.map(key).sort(),
    expected.map(key).sort(),
    `${where}: qid e outcome das entradas de pedido`,
  );
}

async function snapshotOf(engine: Engine, qid: string): Promise<any> {
  const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, qid), seed: 'abc' }));
  assert.equal(body.outcome, 'ok', `${qid}: a variante deveria dar ok: ${JSON.stringify(body).slice(0, 1000)}`);
  return body.snapshot;
}

async function gradeWith(engine: Engine, qid: string, raw: Record<string, unknown>): Promise<any> {
  const snapshot = await snapshotOf(engine, qid);
  const body = ok200(await engine.grade({ question_ref: ref(TEST_COURSE, qid), snapshot, raw_submitted_answers: raw }));
  assertValid('grade_response', body);
  return body;
}

// ---------------------------------------------------------------------------------------------
// Motor em processo, com captureLog()
// ---------------------------------------------------------------------------------------------

test('(a) correção normal de multi-part com o marcador em txt: nada vaza para o registro', async () => {
  const { log, entries } = captureLog();
  await withEngine(
    {},
    async (engine) => {
      const body = await gradeWith(engine, 'supported/multi-part', {
        num: '6.5', int: '1f', txt: MARKER, mc: 'b', cb: ['a', 'c'],
      });
      assert.equal(body.outcome, 'ok', `esperado ok: ${JSON.stringify(body).slice(0, 1000)}`);
      // multi-part não imprime nem tem mídia (server.py e question.html): nenhum aviso.
      assert.deepEqual(body.warnings, [], `avisos inesperados: ${JSON.stringify(body.warnings)}`);
    },
    { log },
  );
  assertNoMarker(entries, '(a)');
  assertRequestEntries(entries, [
    { qid: 'supported/multi-part', outcome: 'ok' },
    { qid: 'supported/multi-part', outcome: 'ok' },
  ], '(a)');
});

test('(b) parse-echo ecoa o marcador no console: vira aviso console_output e não vai para o registro', async () => {
  const { log, entries } = captureLog();
  await withEngine(
    {},
    async (engine) => {
      const body = await gradeWith(engine, 'supported/parse-echo', { t: MARKER });
      assert.equal(body.outcome, 'ok', `esperado ok: ${JSON.stringify(body).slice(0, 1000)}`);
      assert.ok(
        body.warnings.some((w: any) => w.code === 'console_output'),
        `o eco no console deveria gerar o aviso console_output: ${JSON.stringify(body.warnings)}`,
      );
      // Só o parse do server.py imprime (aviso console_output na fase parse).
      assert.deepEqual(warningPairs(body), ['console_output/parse'], `avisos inesperados: ${JSON.stringify(body.warnings)}`);
      assertWarningsWithoutMarker(body, '(b)');
    },
    { log },
  );
  assertNoMarker(entries, '(b)');
  assertRequestEntries(entries, [
    { qid: 'supported/parse-echo', outcome: 'ok' },
    { qid: 'supported/parse-echo', outcome: 'ok' },
  ], '(b)');
});

test('(c) grade-echo ecoa o marcador e lança exceção: broken no grade, sem o marcador no registro', async () => {
  const { log, entries } = captureLog();
  await withEngine(
    {},
    async (engine) => {
      const body = await gradeWith(engine, 'broken/grade-echo', { v: MARKER });
      assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
      assert.equal(body.reason.phase, 'grade', 'o grade deveria ter rodado e quebrado');
      assert.equal(body.reason.code, 'question_code_error', 'exceção no grade do server.py');
      assertFunctionalReason(body, '(c)');
      // [F] PL a193f72 (CodeCallerNative, fixture rodada num container descartável): o print e a
      // exceção do grade põem o marcador na saída do Python (outputBoth), e o technical_detail traz essa
      // saída. O eco chega ao motor; o que não pode é ir para o registro.
      assert.equal(typeof body.reason.technical_detail, 'string', 'technical_detail deve existir');
      assert.ok(
        body.reason.technical_detail.includes(MARKER),
        `o technical_detail deveria trazer a saída ecoada: ${JSON.stringify(body.reason.technical_detail).slice(0, 1500)}`,
      );
      // console_output só vale quando a operação concluiu: numa quebra, a saída vai
      // para o technical_detail, e não há aviso.
      assert.deepEqual(body.warnings, [], `uma correção quebrada não tem avisos: ${JSON.stringify(body.warnings)}`);
    },
    { log },
  );
  assertNoMarker(entries, '(c)');
  assertRequestEntries(entries, [
    { qid: 'broken/grade-echo', outcome: 'ok' },
    { qid: 'broken/grade-echo', outcome: 'broken' },
  ], '(c)');
});

test('(d) grade-timeout com o marcador e ENGINE_QUESTION_TIMEOUT_MS=2000: broken/timeout, sem o marcador no registro', async () => {
  const { log, entries } = captureLog();
  await withEngine(
    { ENGINE_QUESTION_TIMEOUT_MS: '2000' },
    async (engine) => {
      const body = await gradeWith(engine, 'broken/grade-timeout', { v: MARKER });
      assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
      assert.equal(body.reason.code, 'timeout', 'reason.code');
      assert.equal(body.reason.phase, 'grade', 'o tempo esgota no grade do server.py');
      assertFunctionalReason(body, '(d)');
      // O grade de grade-timeout não imprime nem lança: o marcador só chegaria ao corpo pelo lastCallData.
      assertNoMarkerInBody(body, '(d)');
      assert.deepEqual(body.warnings, [], `uma correção quebrada não tem avisos: ${JSON.stringify(body.warnings)}`);
    },
    { log },
  );
  assertNoMarker(entries, '(d)');
  assertRequestEntries(entries, [
    { qid: 'broken/grade-timeout', outcome: 'ok' },
    { qid: 'broken/grade-timeout', outcome: 'broken' },
  ], '(d)');
});

// ---------------------------------------------------------------------------------------------
// (e) Os casos (b)–(d) pela CLI: a stderr inteira não contém o marcador
// ---------------------------------------------------------------------------------------------

type CliRun = { code: number | null; stdout: string; stderr: string };

function runCli(input: unknown, env: Record<string, string>): Promise<CliRun> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['src/cli.ts'], {
      cwd: APP_DIR,
      env: { ...process.env, ENGINE_WORKERS: '2', ...env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    const timer = setTimeout(() => child.kill('SIGKILL'), 180_000);
    child.stdout.on('data', (chunk: Buffer) => out.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => err.push(chunk));
    child.on('error', reject);
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout: Buffer.concat(out).toString('utf8'), stderr: Buffer.concat(err).toString('utf8') });
    });
    // A CLI pode sair antes de ler a entrada inteira; o EPIPE não é o que este teste avalia.
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(input));
  });
}

function cliResults(run: CliRun, where: string): any[] {
  assert.equal(run.code, 0, `${where}: a CLI saiu com código ${run.code}; stderr: ${run.stderr.slice(0, 1500)}`);
  let parsed: any;
  try {
    parsed = JSON.parse(run.stdout);
  } catch {
    assert.fail(`${where}: a stdout da CLI não é JSON: ${run.stdout.slice(0, 500)}`);
  }
  assert.ok(Array.isArray(parsed?.results), `${where}: a stdout da CLI não traz results`);
  return parsed.results;
}

/** Entradas JSON da stderr da CLI (uma linha por registro). */
function stderrEntries(stderr: string): LogEntry[] {
  const entries: LogEntry[] = [];
  for (const line of stderr.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('{')) continue;
    try {
      entries.push(JSON.parse(trimmed));
    } catch {
      // linha que não é JSON: continua valendo para a checagem do marcador na stderr inteira
    }
  }
  return entries;
}

test('(e) os casos (b)–(d) pela CLI (node src/cli.ts): a stderr inteira não contém o marcador', async () => {
  const env = { ENGINE_QUESTION_TIMEOUT_MS: '2000' };
  const qids = ['supported/parse-echo', 'broken/grade-echo', 'broken/grade-timeout'];
  const answerName: Record<string, string> = {
    'supported/parse-echo': 't',
    'broken/grade-echo': 'v',
    'broken/grade-timeout': 'v',
  };

  const variantsRun = await runCli(
    {
      requests: qids.map((qid, i) => ({
        id: `v${i}`,
        op: 'create_variant',
        body: { question_ref: ref(TEST_COURSE, qid), seed: 'abc' },
      })),
    },
    env,
  );
  const variants = cliResults(variantsRun, 'CLI (variantes)');
  assert.deepEqual(variants.map((r) => r.id), ['v0', 'v1', 'v2']);
  for (const r of variants) {
    assert.equal(r.status, 200, `variante ${r.id}: ${JSON.stringify(r.body).slice(0, 800)}`);
    assert.equal(r.body.outcome, 'ok', `variante ${r.id}`);
    // O generate dessas três fixtures não imprime, e elas não têm mídia (server.py e question.html).
    assert.deepEqual(r.body.warnings, [], `variante ${r.id}: avisos inesperados: ${JSON.stringify(r.body.warnings)}`);
  }
  assertRequestEntries(stderrEntries(variantsRun.stderr), qids.map((qid) => ({ qid, outcome: 'ok' })), 'CLI (variantes)');

  const gradesRun = await runCli(
    {
      requests: qids.map((qid, i) => ({
        id: `g${i}`,
        op: 'grade',
        body: {
          question_ref: ref(TEST_COURSE, qid),
          snapshot: variants[i].body.snapshot,
          raw_submitted_answers: { [answerName[qid]]: MARKER },
        },
      })),
    },
    env,
  );
  const grades = cliResults(gradesRun, 'CLI (correções)');
  assert.deepEqual(grades.map((r) => r.id), ['g0', 'g1', 'g2']);
  for (const r of grades) {
    assert.equal(r.status, 200, `correção ${r.id}`);
    assertValid('grade_response', r.body);
  }
  // (b)
  assert.equal(grades[0].body.outcome, 'ok', 'parse-echo pela CLI');
  assert.ok(grades[0].body.warnings.some((w: any) => w.code === 'console_output'), 'parse-echo pela CLI: aviso console_output');
  assert.deepEqual(warningPairs(grades[0].body), ['console_output/parse'], 'parse-echo pela CLI: avisos');
  assertWarningsWithoutMarker(grades[0].body, '(e) parse-echo pela CLI');
  // (c)
  assert.equal(grades[1].body.outcome, 'broken', 'grade-echo pela CLI');
  assert.equal(grades[1].body.reason.phase, 'grade', 'grade-echo pela CLI: reason.phase');
  assert.equal(grades[1].body.reason.code, 'question_code_error', 'grade-echo pela CLI: reason.code');
  assertFunctionalReason(grades[1].body, '(e) grade-echo pela CLI');
  assert.ok(
    String(grades[1].body.reason.technical_detail ?? '').includes(MARKER),
    'grade-echo pela CLI: o technical_detail deveria trazer a saída ecoada',
  );
  assert.deepEqual(grades[1].body.warnings, [], 'grade-echo pela CLI: sem avisos numa correção quebrada');
  // (d)
  assert.equal(grades[2].body.outcome, 'broken', 'grade-timeout pela CLI');
  assert.equal(grades[2].body.reason.code, 'timeout', 'grade-timeout pela CLI: reason.code');
  assert.equal(grades[2].body.reason.phase, 'grade', 'grade-timeout pela CLI: reason.phase');
  assertFunctionalReason(grades[2].body, '(e) grade-timeout pela CLI');
  assertNoMarkerInBody(grades[2].body, '(e) grade-timeout pela CLI');
  assert.deepEqual(grades[2].body.warnings, [], 'grade-timeout pela CLI: sem avisos numa correção quebrada');

  for (const [where, run] of [['variantes', variantsRun], ['correções', gradesRun]] as const) {
    assert.ok(!run.stderr.includes(MARKER), `o marcador vazou para a stderr da CLI (${where}): ${run.stderr.slice(0, 1500)}`);
  }
  assertRequestEntries(
    stderrEntries(gradesRun.stderr),
    [
      { qid: 'supported/parse-echo', outcome: 'ok' },
      { qid: 'broken/grade-echo', outcome: 'broken' },
      { qid: 'broken/grade-timeout', outcome: 'broken' },
    ],
    'CLI (correções)',
  );
});

// ---------------------------------------------------------------------------------------------
// (f) Quebra no parse sem eco: o marcador fica fora do registro e do corpo
// ---------------------------------------------------------------------------------------------

test('(f) parse-exception com o marcador num campo desconhecido: broken no parse, sem o marcador no registro nem no corpo', async () => {
  const { log, entries } = captureLog();
  await withEngine(
    {},
    async (engine) => {
      // O campo desconhecido "nota" chega ao parse do server.py dentro de raw_submitted_answers, e
      // portanto ao lastCallData do CodeCallerNative. A exceção do parse ("falha proposital no parse")
      // não ecoa nada, e o excepthook do PL a193f72 usa show_locals=False
      // (python/prairielearn/internal/traceback.py): o traceback não mostra data.
      const body = await gradeWith(engine, 'broken/parse-exception', { v: '1', nota: MARKER });
      assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
      assert.equal(body.reason.code, 'question_code_error', 'exceção no parse do server.py');
      assert.equal(body.reason.phase, 'parse', 'o parse deveria ter rodado e quebrado');
      assertFunctionalReason(body, '(f)');
      // O technical_detail existe e traz o traceback, então a checagem abaixo não é vazia.
      assert.equal(typeof body.reason.technical_detail, 'string', 'technical_detail deve existir');
      assert.ok(
        body.reason.technical_detail.includes('falha proposital no parse'),
        `technical_detail deveria conter a mensagem da exceção: ${JSON.stringify(body.reason.technical_detail).slice(0, 1500)}`,
      );
      assertNoMarkerInBody(body, '(f)');
      assert.deepEqual(body.warnings, [], `uma correção quebrada não tem avisos: ${JSON.stringify(body.warnings)}`);
    },
    { log },
  );
  assertNoMarker(entries, '(f)');
  assertRequestEntries(entries, [
    { qid: 'broken/parse-exception', outcome: 'ok' },
    { qid: 'broken/parse-exception', outcome: 'broken' },
  ], '(f)');
});
