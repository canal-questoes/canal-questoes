// Teste da CLI em lote do motor (`node src/cli.ts`, stdin → stdout).
// Cobre: as respostas das quatro operações validadas contra os esquemas publicados, a CLI em lote
// usada pela suíte de conformidade (com o mesmo núcleo), o determinismo (o body de
// cada resultado é igual ao do motor em processo para o mesmo pedido), os erros do contrato (400
// para pedido inválido; 404 not_found; unsupported e broken com status 200 e outcome), os registros
// na stderr (uma linha JSON por pedido; a stdout traz só {results}) e o
// protocolo da entrada em lote do motor, inclusive a entrada
// que não é JSON (código 1 e "JSON inválido" na stderr), o item malformado do lote (400
// invalid_request só naquele item) e a saída grande (> 64 KiB) inteira, byte a byte. E também
// error.message em português, por assertPortuguese.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { describe, it } from 'node:test';

import * as cheerio from 'cheerio';

import type { Engine, EngineResult } from '../../src/engine.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, PL_COMMIT, TEST_COURSE, assertPortuguese, ref } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

// Raiz do engine na imagem (/PrairieLearn/apps/prairielearn/canal-questoes-engine): test/contract/../..
const ENGINE_ROOT = path.resolve(import.meta.dirname, '..', '..');
const CLI = path.join(ENGINE_ROOT, 'src', 'cli.ts');
const CLI_TIMEOUT_MS = 180_000;

interface CliRun {
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: Buffer;
  stderr: string;
}

/** Roda `node src/cli.ts` como subprocesso, escreve `input` na stdin e lê stdout e stderr inteiras. */
function runCli(input: string | Buffer): Promise<CliRun> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [CLI], {
      cwd: ENGINE_ROOT,
      env: { ...process.env, ENGINE_WORKERS: '2' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    const out: Buffer[] = [];
    const err: Buffer[] = [];
    child.stdout.on('data', (chunk: Buffer) => out.push(chunk));
    child.stderr.on('data', (chunk: Buffer) => err.push(chunk));
    const timer = setTimeout(() => child.kill('SIGKILL'), CLI_TIMEOUT_MS);
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal, stdout: Buffer.concat(out), stderr: Buffer.concat(err).toString('utf8') });
    });
    // A CLI pode sair sem ler a stdin inteira (EPIPE); o resultado é julgado pelas asserções.
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

/** Exige saída normal (código 0) e uma stdout que seja só o objeto {results}. */
function parseResults(run: CliRun): any[] {
  assert.equal(
    run.code,
    0,
    `a CLI deveria sair com código 0; saiu com ${run.code} (sinal ${run.signal}). stderr:\n${run.stderr.slice(0, 3000)}`,
  );
  const text = run.stdout.toString('utf8');
  let parsed: any;
  try {
    parsed = JSON.parse(text);
  } catch (e) {
    assert.fail(`a stdout deveria ser um único objeto JSON (${(e as Error).message}); início: ${text.slice(0, 500)}`);
  }
  assert.ok(
    parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed),
    'a stdout deveria ser um objeto JSON',
  );
  assert.deepEqual(Object.keys(parsed), ['results'], 'a stdout deveria trazer só o objeto {results}');
  assert.ok(Array.isArray(parsed.results), 'results deveria ser uma lista');
  return parsed.results as any[];
}

/** Linhas da stderr que são objetos JSON (as entradas de registro). */
function jsonLogLines(stderr: string): Record<string, unknown>[] {
  const entries: Record<string, unknown>[] = [];
  for (const line of stderr.split('\n')) {
    if (!line.trim()) continue;
    try {
      const value: unknown = JSON.parse(line);
      if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
        entries.push(value as Record<string, unknown>);
      }
    } catch {
      // linha que não é JSON: não é entrada de registro
    }
  }
  return entries;
}

function byId(results: any[]): Map<string, any> {
  const map = new Map<string, any>();
  for (const r of results) {
    assert.ok(r !== null && typeof r === 'object', 'cada resultado deveria ser um objeto');
    assert.equal(typeof r.id, 'string', 'cada resultado deveria ter id');
    assert.ok(!map.has(r.id), `id repetido nos resultados: ${r.id}`);
    map.set(r.id, r);
  }
  return map;
}

/**
 * O mesmo valor de saúde de http-api.test.ts; workers reflete ENGINE_WORKERS=2,
 * passado por runCli.
 */
const EXPECTED_HEALTH = {
  status: 'ready',
  engine_version: '1.0.0',
  contract_version: '1.0',
  ir_version: '1.0',
  pl_commit: PL_COMMIT,
  workers: 2,
};

/**
 * error.message em português, conferida pelo assertPortuguese, o
 * mesmo dos 400 e 404 de create-variant.test.ts e inspect.test.ts.
 */
function assertPortugueseError(body: any, what: string): void {
  try {
    assertPortuguese(body?.error?.message);
  } catch (err) {
    assert.fail(`${what}: error.message deveria ser frase em português: ${(err as Error).message}`);
  }
}

// Snapshot do núcleo para a correção (o motor não tem estado: corrige o snapshot recebido).
// TEST_COURSE supported/random-params: pl-integer-input answers-name="v", correct_answers.v = params.r.
const GRADE_SNAPSHOT = {
  seed: 'abc',
  params: { r: 42 },
  correct_answers: { v: 42 },
  preferences: {},
  engine_version: '1.0.0',
  pl_commit: PL_COMMIT,
  ir_version: '1.0',
};

/**
 * [F] Corpo esperado da correção de random-params com {v: "42"} e GRADE_SNAPSHOT. Fonte: as fases
 * parse e grade do PL a193f72 (python/prairielearn/internal/question_phases.py com o pl-integer-input
 * e o server.py da fixture, sem grade próprio), rodadas na imagem do PL, e as regras da correção do
 * motor: submitted_answers com o inteiro interpretado; partial_scores do grade do elemento; score pela
 * média ponderada; version_mismatch falso porque o snapshot é desta versão do motor
 * (engine_version 1.0.0) e do PL; sem avisos, porque nada imprime.
 */
const EXPECTED_GRADE = {
  outcome: 'ok',
  valid: true,
  format_errors: {},
  submitted_answers: { v: 42 },
  partial_scores: { v: { score: 1, weight: 1 } },
  score: 1,
  feedback: {},
  params: { r: 42 },
  correct_answers: { v: 42 },
  version_mismatch: { engine: false, pl: false },
  warnings: [],
};

/**
 * [F] Metadados de element/numberInput sem o content_hash: info.json do exampleCourse (a193f72, lido no
 * container pl-oracle), com os valores padrão (gradingMethod, showCorrectAnswer,
 * singleVariant e partialCredit ausentes; type v3), e elements/answers_names do question.html, os
 * mesmos de inspect.test.ts (REFERENCE_SCAN).
 */
const NUMBER_INPUT_METADATA = {
  uuid: '933849d9-d7ee-4462-b743-b50e43b03b9e',
  title: 'Element pl-number-input: Input of real and complex numbers',
  topic: 'Element',
  tags: ['balamut2', 'Sp18'],
  type: 'v3',
  grading_method: 'Internal',
  elements: ['pl-card', 'pl-number-input', 'pl-question-panel'],
  show_correct_answer: true,
  single_variant: false,
  partial_credit: true,
  answers_names: ['c_1', 'c_2', 'c_3', 'c_4', 'c_5', 'c_m', 'c_b', 'c_6', 'c_6_blank', 'c_7'],
};

/** Nomes dos cq-field de um fragmento de HTML, na ordem do documento. */
function markersOf(html: string): Array<string | undefined> {
  const $ = cheerio.load(html, null, false);
  return $('cq-field')
    .toArray()
    .map((el) => $(el).attr('name'));
}

/**
 * Compara o body de cada resultado da CLI com o do mesmo pedido feito ao motor em processo, depois de
 * passar por JSON. Determinismo: a mesma entrada produz a mesma saída (o body não tem campo de medição de
 * tempo; o ms fica fora dele), e pelo protocolo da CLI em lote o body segue os mesmos $defs da rota, com o
 * mesmo núcleo. Uma CLI que altere ou perca qualquer valor do body, inclusive a ordem de uma
 * lista, falha aqui.
 */
async function assertSameAsInProcess(
  map: Map<string, any>,
  calls: Array<[string, (engine: Engine) => Promise<EngineResult>]>,
): Promise<void> {
  await withEngine({}, async (engine) => {
    for (const [id, call] of calls) {
      const expected = JSON.parse(JSON.stringify(ok200(await call(engine))));
      assert.deepStrictEqual(map.get(id).body, expected, `${id}: o body da CLI deveria ser igual ao do motor em processo (determinismo)`);
    }
  });
}

describe('CLI em lote: node src/cli.ts', () => {
  it('um lote com health, inspect, create_variant, grade, um pedido inválido e qids inexistentes', { timeout: CLI_TIMEOUT_MS + 130_000 }, async () => {
    const inspectBody = { question_ref: ref(EXAMPLE, 'element/numberInput') };
    const variantBody = { question_ref: ref(EXAMPLE, 'element/numberInput'), seed: 'abc' };
    const gradeBody = {
      question_ref: ref(TEST_COURSE, 'supported/random-params'),
      snapshot: GRADE_SNAPSHOT,
      raw_submitted_answers: { v: '42' },
    };
    const requests = [
      { id: 'saude', op: 'health', body: {} },
      { id: 'inspecao', op: 'inspect', body: inspectBody },
      { id: 'variante', op: 'create_variant', body: variantBody },
      { id: 'correcao', op: 'grade', body: gradeBody },
      // Sem seed: fora do esquema create_variant_request → 400.
      { id: 'invalido', op: 'create_variant', body: { question_ref: ref(EXAMPLE, 'element/numberInput') } },
      // Pedidos válidos pelo esquema, com qid ausente na área: not_found, com o HTTP equivalente 404
      // (o protocolo da CLI em lote e os erros da área de repositórios).
      { id: 'inexistente-inspecao', op: 'inspect', body: { question_ref: ref(EXAMPLE, 'element/nao-existe') } },
      {
        id: 'inexistente-variante',
        op: 'create_variant',
        body: { question_ref: ref(EXAMPLE, 'element/nao-existe'), seed: 'abc' },
      },
      {
        id: 'inexistente-correcao',
        op: 'grade',
        body: {
          question_ref: ref(TEST_COURSE, 'supported/nao-existe'),
          snapshot: GRADE_SNAPSHOT,
          raw_submitted_answers: { v: '42' },
        },
      },
    ];
    const run = await runCli(JSON.stringify({ requests }));
    const results = parseResults(run);

    assert.equal(results.length, requests.length, 'um resultado por pedido');
    const map = byId(results);
    assert.deepEqual([...map.keys()].sort(), requests.map((r) => r.id).sort(), 'os resultados trazem os mesmos id dos pedidos');

    for (const r of results) {
      assert.equal(typeof r.ms, 'number', `ms ausente no resultado ${r.id}`);
      assert.ok(Number.isFinite(r.ms) && r.ms >= 0, `ms inválido no resultado ${r.id}: ${r.ms}`);
    }

    const expected: [string, number, string][] = [
      ['saude', 200, 'health_response'],
      ['inspecao', 200, 'inspect_response'],
      ['variante', 200, 'create_variant_response'],
      ['correcao', 200, 'grade_response'],
      ['invalido', 400, 'error_response'],
      ['inexistente-inspecao', 404, 'error_response'],
      ['inexistente-variante', 404, 'error_response'],
      ['inexistente-correcao', 404, 'error_response'],
    ];
    for (const [id, status, def] of expected) {
      const r = map.get(id);
      assert.equal(r.status, status, `status de ${id}: esperado ${status}, veio ${r.status}: ${JSON.stringify(r.body).slice(0, 1500)}`);
      assertValid(def, r.body);
    }

    assert.deepEqual(map.get('saude').body, EXPECTED_HEALTH, 'saude: corpo da consulta de saúde');

    const inspected = map.get('inspecao').body;
    assert.equal(inspected.outcome, 'supported');
    assert.deepEqual(inspected.reasons, [], 'inspecao: questão suportada sem motivos');
    const { content_hash, ...metadata } = inspected.question;
    assert.deepEqual(metadata, NUMBER_INPUT_METADATA, 'inspecao: metadados de element/numberInput pela CLI');
    assert.match(content_hash, /^sha256:[0-9a-f]{64}$/, 'inspecao: formato do content_hash');

    const variant = map.get('variante').body;
    assert.equal(variant.outcome, 'ok');
    // Valores conferidos no oráculo, os mesmos de create-variant.test.ts: a = 9 e b = 6 na seed abc.
    assert.deepEqual(variant.snapshot.params, { a: 9, b: 6 }, 'variante: params divergentes do oráculo');
    assert.deepEqual(
      variant.ir.fields.map((f: any) => f.name),
      NUMBER_INPUT_METADATA.answers_names,
      'variante: um campo por answers-name, na ordem do documento',
    );
    assert.deepEqual(
      markersOf(variant.ir.statement.html),
      NUMBER_INPUT_METADATA.answers_names,
      'variante: um cq-field por campo no enunciado, na ordem do documento',
    );

    assert.deepEqual(map.get('correcao').body, EXPECTED_GRADE, 'correcao: corpo da correção de random-params');
    assert.equal(map.get('invalido').body.error.code, 'invalid_request');
    assertPortugueseError(map.get('invalido').body, 'invalido');
    for (const id of ['inexistente-inspecao', 'inexistente-variante', 'inexistente-correcao']) {
      assert.equal(map.get(id).body.error.code, 'not_found', `${id}: error.code`);
      assertPortugueseError(map.get(id).body, id);
    }

    // O restante de cada body (content_hash, a IR inteira, o snapshot e os avisos) é o do motor em
    // processo para o mesmo pedido.
    await assertSameAsInProcess(map, [
      ['inspecao', (engine) => engine.inspect(inspectBody)],
      ['variante', (engine) => engine.createVariant(variantBody)],
      ['correcao', (engine) => engine.grade(gradeBody)],
    ]);

    // Os registros vão para a stderr: uma linha JSON por pedido, com a operação.
    const logs = jsonLogLines(run.stderr).filter((e) => 'op' in e);
    assert.ok(
      logs.length >= requests.length,
      `esperada ao menos uma linha de registro JSON com "op" por pedido na stderr (${requests.length}); vieram ${logs.length}. stderr:\n${run.stderr.slice(0, 2000)}`,
    );
  });

  it('não suportada e quebrada saem com status 200 e outcome, e não com 4xx/5xx', { timeout: CLI_TIMEOUT_MS + 130_000 }, async () => {
    // A CLI usa o status HTTP equivalente (protocolo da CLI em lote), e o resultado semântico é 200.
    // Os valores esperados são os já fixados no motor em processo: inspect.test.ts (order-blocks:
    // só unsupported_elements, com ['pl-order-blocks']) e create-variant.test.ts (generate-exception:
    // question_code_error na fase generate).
    const orderBlocks = ref(TEST_COURSE, 'unsupported/order-blocks');
    const unsupportedVariantBody = { question_ref: orderBlocks, seed: 'abc' };
    const brokenVariantBody = { question_ref: ref(TEST_COURSE, 'broken/generate-exception'), seed: 'abc' };
    const requests = [
      { id: 'inspecao-nao-suportada', op: 'inspect', body: { question_ref: orderBlocks } },
      { id: 'variante-nao-suportada', op: 'create_variant', body: unsupportedVariantBody },
      { id: 'variante-quebrada', op: 'create_variant', body: brokenVariantBody },
    ];
    const results = parseResults(await runCli(JSON.stringify({ requests })));
    assert.equal(results.length, requests.length, 'um resultado por pedido');
    const map = byId(results);

    for (const [id, def] of [
      ['inspecao-nao-suportada', 'inspect_response'],
      ['variante-nao-suportada', 'create_variant_response'],
      ['variante-quebrada', 'create_variant_response'],
    ] as const) {
      const r = map.get(id);
      assert.ok(r, `faltou o resultado ${id}`);
      assert.equal(r.status, 200, `status de ${id}: esperado 200, veio ${r.status}: ${JSON.stringify(r.body).slice(0, 1500)}`);
      assertValid(def, r.body);
    }

    const inspected = map.get('inspecao-nao-suportada').body;
    assert.equal(inspected.outcome, 'unsupported');
    assert.deepEqual(inspected.reasons.map((x: any) => x.code), ['unsupported_elements'], 'inspect order-blocks: códigos de motivo');
    assert.deepEqual(inspected.reasons[0].elements, ['pl-order-blocks'], 'inspect order-blocks: elementos fora do subconjunto');

    const unsupported = map.get('variante-nao-suportada').body;
    assert.equal(unsupported.outcome, 'unsupported');
    // Os mesmos motivos da inspeção (inspect.test.ts, "POST variants em order-blocks").
    assert.deepEqual(unsupported.reasons.map((x: any) => x.code), ['unsupported_elements'], 'variante order-blocks: códigos de motivo');
    assert.deepEqual(unsupported.reasons[0].elements, ['pl-order-blocks'], 'variante order-blocks: elementos fora do subconjunto');

    const broken = map.get('variante-quebrada').body;
    assert.equal(broken.outcome, 'broken');
    assert.equal(broken.reason.code, 'question_code_error', 'variante generate-exception: reason.code');
    assert.equal(broken.reason.phase, 'generate', 'variante generate-exception: reason.phase');
    // create-variant.test.ts (assertBroken): uma variante quebrada destas fixtures não tem avisos.
    assert.deepEqual(broken.warnings, [], 'variante generate-exception: sem avisos');

    // O restante de cada body (metadados, textos dos motivos, message e technical_detail) é o do
    // motor em processo para o mesmo pedido.
    await assertSameAsInProcess(map, [
      ['inspecao-nao-suportada', (engine) => engine.inspect({ question_ref: orderBlocks })],
      ['variante-nao-suportada', (engine) => engine.createVariant(unsupportedVariantBody)],
      ['variante-quebrada', (engine) => engine.createVariant(brokenVariantBody)],
    ]);
  });

  it('item malformado do lote → 400 invalid_request só naquele item, e os demais seguem (entrada em lote)', { timeout: CLI_TIMEOUT_MS + 10_000 }, async () => {
    // um item do lote que não é um pedido válido (corpo que não é objeto, op desconhecida, id
    // ausente) gera um resultado só daquele item, com status 400 e invalid_request, e os demais
    // seguem. Um pedido válido antes e outro depois dos malformados mostram que o lote não parou.
    const requests: unknown[] = [
      { id: 'saude', op: 'health', body: {} },
      { id: 'op-desconhecida', op: 'apagar', body: {} },
      { id: 'corpo-lista', op: 'inspect', body: [] },
      { id: 'corpo-nulo', op: 'create_variant', body: null },
      { id: 'corpo-texto', op: 'grade', body: 'x' },
      { op: 'health', body: {} }, // id ausente
      7, // item que não é objeto: também não é um pedido válido
      { id: 'saude-final', op: 'health', body: {} },
    ];
    const results = parseResults(await runCli(JSON.stringify({ requests })));
    assert.equal(results.length, requests.length, 'um resultado por item do lote, inclusive os malformados');
    for (const r of results) {
      assert.ok(r !== null && typeof r === 'object' && !Array.isArray(r), `cada resultado deveria ser um objeto: ${JSON.stringify(r)}`);
    }

    // O id e a posição do resultado de um item sem id (ou que não é objeto) não são fixados.
    // Por isso o teste acha os resultados pelo id quando o item tem id, e os outros por exclusão, sem
    // byId() nem a ordem da lista.
    const named = ['saude', 'op-desconhecida', 'corpo-lista', 'corpo-nulo', 'corpo-texto', 'saude-final'];
    const withId = (id: string): any => {
      const found = results.filter((r) => r.id === id);
      assert.equal(found.length, 1, `${id}: esperado um único resultado com esse id; vieram ${found.length}`);
      return found[0];
    };
    const assertInvalidItem = (r: any, what: string): void => {
      assert.equal(r.status, 400, `${what}: esperado status 400, veio ${r.status}: ${JSON.stringify(r.body).slice(0, 1500)}`);
      assertValid('error_response', r.body);
      assert.equal(r.body.error.code, 'invalid_request', `${what}: error.code`);
      assertPortugueseError(r.body, what);
    };

    for (const id of ['saude', 'saude-final']) {
      const r = withId(id);
      assert.equal(r.status, 200, `${id}: esperado status 200, veio ${r.status}: ${JSON.stringify(r.body).slice(0, 1500)}`);
      assert.deepEqual(r.body, EXPECTED_HEALTH, `${id}: corpo da consulta de saúde`);
    }
    for (const id of ['op-desconhecida', 'corpo-lista', 'corpo-nulo', 'corpo-texto']) {
      assertInvalidItem(withId(id), id);
    }
    const unnamed = results.filter((r) => !named.includes(r.id));
    assert.equal(unnamed.length, 2, `o item sem id e o item 7 deveriam ter um resultado cada; vieram ${unnamed.length}`);
    unnamed.forEach((r, i) => assertInvalidItem(r, `item sem id ou que não é objeto (${i + 1}º)`));
  });

  it('a variante de TEST_COURSE supported/big-media sai inteira e parseável na stdout (> 64 KiB)', { timeout: CLI_TIMEOUT_MS + 10_000 }, async () => {
    const requests = [
      { id: 'grande', op: 'create_variant', body: { question_ref: ref(TEST_COURSE, 'supported/big-media'), seed: 'abc' } },
    ];
    const run = await runCli(JSON.stringify({ requests }));
    const results = parseResults(run);
    assert.ok(run.stdout.length > 64 * 1024, `a stdout deveria passar de 64 KiB; teve ${run.stdout.length} bytes`);

    assert.equal(results.length, 1);
    const r = results[0];
    assert.equal(r.id, 'grande');
    assert.equal(r.status, 200, `esperado 200: ${JSON.stringify(r.body).slice(0, 1500)}`);
    assertValid('create_variant_response', r.body);
    assert.equal(r.body.outcome, 'ok', `esperado outcome ok: ${JSON.stringify(r.body).slice(0, 1500)}`);

    // A mídia gerada de 200 000 bytes chega inteira (sem truncar a stdout).
    const media = (r.body.ir.media as any[]).find((m) => m.filename === 'grande.png');
    assert.ok(media, 'a mídia grande.png deveria estar na IR');
    assert.equal(media.status, 'ok');
    assert.equal(media.size_bytes, 200_000);
    const bytes = Buffer.from(media.data_base64, 'base64');
    assert.equal(bytes.length, 200_000, 'os bytes da mídia deveriam chegar inteiros');
    // [F] server.py de supported/big-media: GRANDE = bytes(i % 251 for i in range(200000)). Um defeito
    // na montagem da stdout grande que troque ou corrompa trechos sem mudar o tamanho falha aqui.
    const expectedBytes = Buffer.from(Array.from({ length: 200_000 }, (_v, k) => k % 251));
    assert.ok(bytes.equals(expectedBytes), 'os bytes de grande.png deveriam chegar intactos pela CLI');
  });

  it('uma entrada que não é JSON sai com código 1 e "JSON inválido" na stderr (entrada em lote)', { timeout: CLI_TIMEOUT_MS + 10_000 }, async () => {
    const run = await runCli('isto não é JSON {');
    assert.notEqual(run.code, null, `a CLI deveria sair sozinha, e não por sinal (${run.signal})`);
    // Protocolo da CLI em lote: uma entrada que não é JSON sai com código 1 e "JSON inválido" na stderr.
    assert.equal(run.code, 1, `uma entrada que não é JSON deveria sair com código 1; saiu com ${run.code}`);
    assert.ok(
      run.stderr.includes('JSON inválido'),
      `a stderr deveria conter "JSON inválido"; veio:\n${run.stderr.slice(0, 2000)}`,
    );
  });
});
