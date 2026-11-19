// Operação isolada, sem estado e resistente.
// Cobre: o autoteste "ambiente restrito"; o tempo esgotado e a
// recuperação; os pedidos simultâneos; p95 < 1 s com o motor aquecido.
// O motor restrito com os mesmos resultados do PL sem restrição é coberto pela suíte de conformidade,
// que roda o motor restrito contra o oráculo.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import { test } from 'node:test';

import type { Engine } from '../../src/engine.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, TEST_COURSE, assertPortuguese, ref, seeds } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

const HOW_TO_RUN = 'execute a imagem de teste com --network none --read-only --user 65534:65534 --tmpfs /tmp';

/** Parâmetros de element/numberInput na seed "abc" (valores conferidos no oráculo). */
const NUMBER_INPUT_ABC_PARAMS = { a: 9, b: 6 };

function tryConnect(host: string, port: number, timeoutMs: number): Promise<'connected' | 'failed' | 'timeout'> {
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const timer = setTimeout(() => {
      socket.destroy();
      resolve('timeout');
    }, timeoutMs);
    socket.once('connect', () => {
      clearTimeout(timer);
      socket.destroy();
      resolve('connected');
    });
    socket.once('error', () => {
      clearTimeout(timer);
      socket.destroy();
      resolve('failed');
    });
  });
}

function numberInputRequest(seed: string): Record<string, unknown> {
  return { question_ref: ref(EXAMPLE, 'element/numberInput'), seed };
}

/** Aquece os dois executores do pool (a primeira chamada de cada um sobe o zygote). */
async function warmUp(engine: Engine): Promise<void> {
  const results = await Promise.all([
    engine.createVariant(numberInputRequest('abc')),
    engine.createVariant(numberInputRequest('abc')),
  ]);
  for (const r of results) assert.equal(ok200(r).outcome, 'ok', 'aquecimento: element/numberInput deveria dar ok');
}

/** Depois de uma quebra, 5 pedidos válidos seguidos são atendidos corretamente. */
async function assertFiveNumberInputOk(engine: Engine, after: string): Promise<void> {
  for (let i = 0; i < 5; i++) {
    const body = ok200(await engine.createVariant(numberInputRequest('abc')));
    assert.equal(body.outcome, 'ok', `pedido ${i + 1} depois de ${after}: ${JSON.stringify(body).slice(0, 1000)}`);
    assertValid('create_variant_response', body);
    assert.deepEqual(body.snapshot.params, NUMBER_INPUT_ABC_PARAMS, `params do pedido ${i + 1} depois de ${after}`);
  }
}

function p95(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.ceil(0.95 * sorted.length) - 1];
}

// ---------------------------------------------------------------------------------------------
// Autoteste (verde já no esqueleto): as restrições estavam ativas nesta execução.
// ---------------------------------------------------------------------------------------------

test('autoteste "ambiente restrito": uid 65534, sistema de arquivos somente leitura e sem rede', async () => {
  const uid = process.getuid?.();
  assert.equal(uid, 65534, `o processo roda com uid ${uid}; ${HOW_TO_RUN}`);

  for (const target of ['/PrairieLearn/apps/prairielearn/canal-questoes-engine/x', '/repos/x']) {
    let code: string | undefined;
    try {
      fs.writeFileSync(target, 'x');
      fs.rmSync(target, { force: true });
    } catch (err) {
      code = (err as NodeJS.ErrnoException).code;
    }
    assert.ok(
      code === 'EROFS' || code === 'EACCES',
      `escrever em ${target} deveria falhar com EROFS ou EACCES (resultado: ${code ?? 'escrita aceita'}); ${HOW_TO_RUN}`,
    );
  }

  // /var/tmp é gravável por qualquer usuário (drwxrwxrwt): só o --read-only faz a escrita falhar.
  // Isso distingue "sistema de arquivos somente leitura" de "diretório de outro dono".
  let varTmpCode: string | undefined;
  try {
    fs.writeFileSync('/var/tmp/cq-escrita', 'x');
    fs.rmSync('/var/tmp/cq-escrita', { force: true });
  } catch (err) {
    varTmpCode = (err as NodeJS.ErrnoException).code;
  }
  assert.equal(varTmpCode, 'EROFS', `escrever em /var/tmp deveria falhar com EROFS (resultado: ${varTmpCode ?? 'escrita aceita'}); ${HOW_TO_RUN}`);

  // /tmp precisa ser um tmpfs gravável (a única área de escrita do motor).
  const mounts = fs.readFileSync('/proc/mounts', 'utf8');
  assert.ok(/^\S+ \/tmp tmpfs /m.test(mounts), `/tmp não é um tmpfs; ${HOW_TO_RUN}`);
  fs.writeFileSync('/tmp/cq-escrita', 'x');
  fs.rmSync('/tmp/cq-escrita', { force: true });

  const outcome = await tryConnect('1.1.1.1', 53, 2000);
  assert.notEqual(outcome, 'connected', `a conexão TCP de saída para 1.1.1.1:53 foi aceita; ${HOW_TO_RUN}`);
});

// ---------------------------------------------------------------------------------------------
// Tempo esgotado e recuperação
// ---------------------------------------------------------------------------------------------

test('generate-timeout dá broken/timeout em menos de 6 s, e os 5 pedidos seguintes são atendidos', async () => {
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    // Os executores já sobem aquecidos, para que o tempo medido seja o do limite configurado.
    await warmUp(engine);
    const t0 = performance.now();
    const body = ok200(
      await engine.createVariant({ question_ref: ref(TEST_COURSE, 'broken/generate-timeout'), seed: 'abc' }),
    );
    const elapsed = performance.now() - t0;
    assertValid('create_variant_response', body);
    assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
    assert.equal(body.reason.code, 'timeout', 'reason.code');
    assertPortuguese(body.reason.message);
    assert.equal(body.reason.phase, 'generate', 'reason.phase');
    assert.ok(elapsed < 6000, `o tempo esgotado deveria sair em menos de 6 s (levou ${Math.round(elapsed)} ms)`);
    await assertFiveNumberInputOk(engine, 'generate-timeout');
  });
});

test('depois de generate-exception, os 5 pedidos seguintes são atendidos', async () => {
  await withEngine({}, async (engine) => {
    const body = ok200(
      await engine.createVariant({ question_ref: ref(TEST_COURSE, 'broken/generate-exception'), seed: 'abc' }),
    );
    assertValid('create_variant_response', body);
    assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
    assert.equal(body.reason.code, 'question_code_error', 'reason.code');
    await assertFiveNumberInputOk(engine, 'generate-exception');
  });
});

// Recuperação também depois de quebra no prepare e de alteração proibida de dados.
for (const [qid, phase, code] of [
  ['broken/static-figure-missing', 'prepare', 'question_code_error'],
  ['broken/illegal-data', 'generate', 'invalid_data'],
] as const) {
  test(`depois de ${qid} (${phase}: ${code}), os 5 pedidos seguintes são atendidos`, async () => {
    await withEngine({}, async (engine) => {
      const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, qid), seed: 'abc' }));
      assertValid('create_variant_response', body);
      assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 1000)}`);
      assert.equal(body.reason.phase, phase, 'reason.phase');
      assert.equal(body.reason.code, code, 'reason.code');
      await assertFiveNumberInputOk(engine, qid);
    });
  });
}

/** Resposta toda correta de TEST_COURSE supported/multi-part. */
const MULTI_PART_CORRECT = { num: '6.5', int: '1f', txt: 'brasil', mc: 'b', cb: ['a', 'c'] };

async function snapshotOf(engine: Engine, qid: string): Promise<any> {
  const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, qid), seed: 'abc' }));
  assert.equal(body.outcome, 'ok', `a variante de ${qid} deveria sair ok: ${JSON.stringify(body).slice(0, 800)}`);
  return body.snapshot;
}

/** Depois de uma quebra na correção, 5 correções válidas seguidas de multi-part dão nota 1. */
async function assertFiveGradesOk(engine: Engine, after: string): Promise<void> {
  const snapshot = await snapshotOf(engine, 'supported/multi-part');
  for (let i = 0; i < 5; i++) {
    const body = ok200(
      await engine.grade({
        question_ref: ref(TEST_COURSE, 'supported/multi-part'),
        snapshot,
        raw_submitted_answers: MULTI_PART_CORRECT,
      }),
    );
    assertValid('grade_response', body);
    assert.equal(body.outcome, 'ok', `correção ${i + 1} depois de ${after}: ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.valid, true, `correção ${i + 1} depois de ${after}: válida`);
    assert.equal(body.score, 1, `correção ${i + 1} depois de ${after}: nota 1`);
  }
}

for (const [qid, raw, phase, code] of [
  ['broken/parse-exception', { v: '1' }, 'parse', 'question_code_error'],
  ['broken/grade-echo', { v: 'qualquer texto' }, 'grade', 'question_code_error'],
  ['broken/grade-timeout', { v: 'qualquer texto' }, 'grade', 'timeout'],
] as const) {
  test(`depois de ${qid} (quebra no ${phase}: ${code}), as 5 correções seguintes são atendidas`, async () => {
    await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
      const snapshot = await snapshotOf(engine, qid);
      const t0 = performance.now();
      const body = ok200(await engine.grade({ question_ref: ref(TEST_COURSE, qid), snapshot, raw_submitted_answers: raw }));
      const elapsed = performance.now() - t0;
      assertValid('grade_response', body);
      assert.equal(body.outcome, 'broken', `esperado broken: ${JSON.stringify(body).slice(0, 800)}`);
      assert.equal(body.reason.phase, phase, 'reason.phase');
      assert.equal(body.reason.code, code, 'reason.code');
      assertPortuguese(body.reason.message);
      assert.deepEqual(body.version_mismatch, { engine: false, pl: false }, 'version_mismatch presente também na quebra');
      if (code === 'timeout') assert.ok(elapsed < 6000, `o tempo esgotado deveria sair em menos de 6 s (levou ${Math.round(elapsed)} ms)`);
      await assertFiveGradesOk(engine, qid);
    });
  });
}

// ---------------------------------------------------------------------------------------------
// Pedidos simultâneos
// ---------------------------------------------------------------------------------------------

test('com 2 executores, 12 pedidos simultâneos dão os mesmos corpos que em série', async () => {
  const questions = ['element/numberInput', 'element/multipleChoice', 'element/checkbox'];
  const requests = questions.flatMap((qid) => seeds(4).map((seed) => ({ question_ref: ref(EXAMPLE, qid), seed })));
  assert.equal(requests.length, 12);
  await withEngine({ ENGINE_WORKERS: '2' }, async (engine) => {
    const serial: any[] = [];
    for (const req of requests) serial.push(ok200(await engine.createVariant(req)));
    const parallel = (await Promise.all(requests.map((req) => engine.createVariant(req)))).map(ok200);
    for (let i = 0; i < requests.length; i++) {
      const label = `${requests[i].question_ref.qid} seed ${requests[i].seed}`;
      assert.equal(serial[i].outcome, 'ok', `${label} em série`);
      assertValid('create_variant_response', parallel[i]);
      assert.deepStrictEqual(parallel[i], serial[i], `${label}: o corpo simultâneo difere do corpo em série`);
    }
  });
});

// ---------------------------------------------------------------------------------------------
// Desempenho com o motor aquecido
// ---------------------------------------------------------------------------------------------

test('aquecido, 40 create_variant e 40 grade de element/numberInput têm p95 < 1000 ms', async () => {
  await withEngine({}, async (engine) => {
    const qref = ref(EXAMPLE, 'element/numberInput');
    const rawFor = (snapshot: any): Record<string, string> => {
      const c = String(snapshot.correct_answers.c_1);
      return { c_1: c, c_2: c, c_3: c, c_4: c, c_5: c, c_m: '2', c_b: '5', c_6: '', c_6_blank: '', c_7: c };
    };

    await warmUp(engine);
    const warm = ok200(await engine.createVariant(numberInputRequest('abc')));
    assert.equal(
      ok200(await engine.grade({ question_ref: qref, snapshot: warm.snapshot, raw_submitted_answers: rawFor(warm.snapshot) })).outcome,
      'ok',
      'aquecimento da correção',
    );

    const createMs: number[] = [];
    const snapshots: any[] = [];
    for (const seed of seeds(40, 100)) {
      const t0 = performance.now();
      const body = ok200(await engine.createVariant(numberInputRequest(seed)));
      createMs.push(performance.now() - t0);
      assert.equal(body.outcome, 'ok', `create_variant seed ${seed}`);
      snapshots.push(body.snapshot);
    }
    const gradeMs: number[] = [];
    for (const snapshot of snapshots) {
      const t0 = performance.now();
      const body = ok200(await engine.grade({ question_ref: qref, snapshot, raw_submitted_answers: rawFor(snapshot) }));
      gradeMs.push(performance.now() - t0);
      assert.equal(body.outcome, 'ok', `grade seed ${snapshot.seed}`);
    }
    assert.ok(p95(createMs) < 1000, `p95 de create_variant = ${Math.round(p95(createMs))} ms (limite 1000 ms)`);
    assert.ok(p95(gradeMs) < 1000, `p95 de grade = ${Math.round(p95(gradeMs))} ms (limite 1000 ms)`);
  });
});
