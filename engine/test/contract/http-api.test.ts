// Teste de contrato da API HTTP /v1 (startServer em 127.0.0.1, porta 0).
// Cobre: a saúde (com versões do motor, do contrato, da IR e do PL), as
// três rotas POST com corpos válidos contra os esquemas publicados, o determinismo
// (o corpo de cada rota é igual ao do motor em processo para o mesmo pedido), os erros do contrato
// (unsupported e broken com HTTP 200 e outcome; 400 invalid_request com details, inclusive para
// JSON que não é objeto; 404 not_found nas três rotas, 404 route_not_found, 405 method_not_allowed,
// 413 payload_too_large, 500 internal_error sem pilha depois de close()), as mensagens em português
// (todo error.message, por assertPortuguese), o corpo fixado da correção
// de random-params pela rota, o limite de 2 MiB com Content-Type application/json e o contrato de
// rotas do motor.
import assert from 'node:assert/strict';
import http from 'node:http';
import { test } from 'node:test';

import { loadConfig } from '../../src/config.ts';
import { createEngine, type EngineResult } from '../../src/engine.ts';
import { startServer } from '../../src/http/server.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, PL_COMMIT, TEST_COURSE, assertPortuguese, ref } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

const HOST = '127.0.0.1';
const REQUEST_TIMEOUT_MS = 120_000;
const NUMBER_INPUT = ref(EXAMPLE, 'element/numberInput');

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

/** Snapshot válido pelo $defs.snapshot_input, para pedidos de correção que não chegam a corrigir. */
const ANY_SNAPSHOT = {
  seed: 'abc',
  params: {},
  correct_answers: {},
  preferences: {},
  engine_version: '1.0.0',
  pl_commit: PL_COMMIT,
  ir_version: '1.0',
};

/**
 * Snapshot de TEST_COURSE supported/random-params (pl-integer-input answers-name="v", correct_answers.v
 * = params.r), o mesmo GRADE_SNAPSHOT de cli.test.ts. O motor não tem estado: corrige o snapshot
 * recebido.
 */
const RANDOM_PARAMS_SNAPSHOT = {
  seed: 'abc',
  params: { r: 42 },
  correct_answers: { v: 42 },
  preferences: {},
  engine_version: '1.0.0',
  pl_commit: PL_COMMIT,
  ir_version: '1.0',
};

/**
 * [F] Corpo esperado da correção de random-params com {v: "42"} e RANDOM_PARAMS_SNAPSHOT, o mesmo
 * EXPECTED_GRADE de cli.test.ts. Fonte: as fases parse e grade do PL a193f72
 * (python/prairielearn/internal/question_phases.py com o pl-integer-input e o server.py da fixture,
 * sem grade próprio), rodadas na imagem do PL, e as regras da correção do motor. Fixado aqui também
 * porque a igualdade com o motor em processo não pega um erro que a rota e o núcleo cometam juntos.
 */
const EXPECTED_RANDOM_PARAMS_GRADE = {
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
 * Corpo do mesmo pedido feito ao motor em processo, depois de passar por JSON, como passaria pela
 * rota. Determinismo: a mesma entrada produz a mesma saída (o corpo não tem campo de medição de tempo), e
 * pelo contrato do motor, a rota só transporta o corpo do núcleo. Assim, uma camada HTTP que altere ou
 * perca qualquer valor do corpo, inclusive a ordem de uma lista, falha na comparação.
 */
async function inProcessBody(pending: Promise<EngineResult>): Promise<any> {
  return JSON.parse(JSON.stringify(ok200(await pending)));
}

interface HttpResult {
  status: number;
  headers: http.IncomingHttpHeaders;
  text: string;
  body: any;
}

function parseJson(text: string): any {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** Um pedido HTTP simples, sem keep-alive, que lê a resposta inteira. */
function request(base: string, method: string, pathname: string, body?: string): Promise<HttpResult> {
  return new Promise((resolve, reject) => {
    const headers: Record<string, string> = { connection: 'close' };
    if (body !== undefined) {
      headers['content-type'] = 'application/json';
      headers['content-length'] = String(Buffer.byteLength(body));
    }
    const req = http.request(new URL(pathname, base), { method, headers, agent: false }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (chunk: Buffer) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8');
        resolve({ status: res.statusCode ?? 0, headers: res.headers, text, body: parseJson(text) });
      });
      res.on('error', reject);
    });
    req.on('error', reject);
    req.setTimeout(REQUEST_TIMEOUT_MS, () => req.destroy(new Error(`tempo esgotado esperando ${method} ${pathname}`)));
    req.end(body);
  });
}

function postJson(base: string, pathname: string, value: unknown): Promise<HttpResult> {
  return request(base, 'POST', pathname, JSON.stringify(value));
}

/**
 * Envia um corpo grande com Content-Length, em pedaços e respeitando a contrapressão. Aceita a
 * rejeição antecipada da conexão só depois de a resposta chegar; sem resposta, falha.
 */
function sendLarge(base: string, pathname: string, payload: Buffer): Promise<HttpResult> {
  return new Promise((resolve, reject) => {
    let gotResponse = false;
    let settled = false;
    const settle = (fn: () => void): void => {
      if (!settled) {
        settled = true;
        fn();
      }
    };
    const req = http.request(
      new URL(pathname, base),
      {
        method: 'POST',
        agent: false,
        headers: {
          connection: 'close',
          'content-type': 'application/json',
          'content-length': String(payload.length),
        },
      },
      (res) => {
        gotResponse = true;
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('end', () => {
          const text = Buffer.concat(chunks).toString('utf8');
          settle(() => resolve({ status: res.statusCode ?? 0, headers: res.headers, text, body: parseJson(text) }));
          req.destroy();
        });
        res.on('error', (err) => settle(() => reject(new Error(`a resposta ao corpo grande foi interrompida: ${err.message}`))));
      },
    );
    req.on('error', (err: NodeJS.ErrnoException) => {
      // Rejeição antecipada da conexão depois da resposta: aceita (a resposta é julgada no 'end').
      if (gotResponse) return;
      settle(() =>
        reject(new Error(`a conexão foi encerrada antes de qualquer resposta HTTP (${err.code ?? err.message}); esperado 413`)),
      );
    });
    req.setTimeout(REQUEST_TIMEOUT_MS, () => req.destroy(new Error('tempo esgotado no envio do corpo grande')));
    const CHUNK = 64 * 1024;
    let offset = 0;
    const pump = (): void => {
      while (offset < payload.length) {
        if (req.destroyed) return;
        const end = Math.min(offset + CHUNK, payload.length);
        const flushed = req.write(payload.subarray(offset, end));
        offset = end;
        if (!flushed) {
          req.once('drain', pump);
          return;
        }
      }
      req.end();
    };
    pump();
  });
}

/** Status esperado, Content-Type JSON e corpo JSON. */
function expectJson(r: HttpResult, status: number, what: string): any {
  assert.equal(r.status, status, `${what}: esperado HTTP ${status}, veio ${r.status}: ${r.text.slice(0, 1500)}`);
  const contentType = String(r.headers['content-type'] ?? '');
  assert.ok(contentType.startsWith('application/json'), `${what}: Content-Type deveria começar com application/json; veio "${contentType}"`);
  assert.notEqual(r.body, undefined, `${what}: o corpo deveria ser JSON: ${r.text.slice(0, 500)}`);
  return r.body;
}

/**
 * Erro do contrato: status, Content-Type, error_response válido, o código esperado e a mensagem em
 * português. O error_response sai com mensagens em português, como toda
 * mensagem para pessoas; o critério é o assertPortuguese, o
 * mesmo dos 400 e 404 de create-variant.test.ts e inspect.test.ts.
 */
function expectError(r: HttpResult, status: number, code: string, what: string): any {
  const body = expectJson(r, status, what);
  assertValid('error_response', body);
  assert.equal(body.error.code, code, `${what}: esperado error.code ${code}`);
  try {
    assertPortuguese(body.error.message);
  } catch (err) {
    assert.fail(`${what}: error.message deveria ser frase em português: ${(err as Error).message}`);
  }
  return body;
}

/** Nenhum texto do erro traz pilha de execução ou caminho de código-fonte. */
function assertNoStack(body: any, what: string): void {
  const texts: string[] = [body.error.message, ...(body.error.details ?? [])];
  for (const t of texts) {
    assert.doesNotMatch(t, /(^|\n)\s*at\s+\S+/, `${what}: o erro traz pilha de execução: ${t}`);
    assert.doesNotMatch(t, /\.(ts|js|mjs|cjs):\d+/, `${what}: o erro traz caminho de código-fonte: ${t}`);
    assert.doesNotMatch(t, /node:internal/, `${what}: o erro traz pilha interna do Node: ${t}`);
  }
}

test('API HTTP /v1 com startServer em 127.0.0.1:0', { timeout: 600_000 }, async (t) => {
  await withEngine({}, async (engine) => {
    const server = await startServer(engine, { host: HOST, port: 0 });
    const base = server.url;
    try {
      assert.match(base, /^http:\/\/127\.0\.0\.1:\d+/, `url do servidor inesperada: ${base}`);

      await t.test('GET /v1/health → 200 com as versões', async () => {
        const body = expectJson(await request(base, 'GET', '/v1/health'), 200, 'health');
        assertValid('health_response', body);
        const { workers, ...rest } = body;
        assert.deepEqual(rest, {
          status: 'ready',
          engine_version: '1.0.0',
          contract_version: '1.0',
          ir_version: '1.0',
          pl_commit: PL_COMMIT,
        });
        assert.ok(Number.isInteger(workers) && workers >= 1, `workers deveria ser inteiro ≥ 1: ${workers}`);
        // o motor usa o número configurado de executores (o harness configura ENGINE_WORKERS=2).
        assert.equal(workers, 2, 'workers deveria refletir ENGINE_WORKERS=2');
      });

      await t.test('POST /v1/inspect válido → 200 inspect_response com os metadados da questão', async () => {
        const body = expectJson(await postJson(base, '/v1/inspect', { question_ref: NUMBER_INPUT }), 200, 'inspect');
        assertValid('inspect_response', body);
        assert.equal(body.outcome, 'supported');
        assert.deepEqual(body.reasons, [], 'inspect: questão suportada sem motivos');
        const { content_hash, ...metadata } = body.question;
        assert.deepEqual(metadata, NUMBER_INPUT_METADATA, 'inspect: metadados de element/numberInput pela rota');
        assert.match(content_hash, /^sha256:[0-9a-f]{64}$/, 'inspect: formato do content_hash');
        // O content_hash pela regra do hash de conteúdo é conferido em inspect.test.ts; aqui, o corpo
        // inteiro (content_hash inclusive) é o do motor em processo.
        assert.deepStrictEqual(
          body,
          await inProcessBody(engine.inspect({ question_ref: NUMBER_INPUT })),
          'inspect: o corpo da rota deveria ser igual ao do motor em processo (determinismo)',
        );
      });

      await t.test('POST /v1/variants e POST /v1/grade válidos → 200 com os corpos do motor em processo', async () => {
        const variantRequest = { question_ref: NUMBER_INPUT, seed: 'abc' };
        const variant = expectJson(await postJson(base, '/v1/variants', variantRequest), 200, 'variants');
        assertValid('create_variant_response', variant);
        assert.equal(variant.outcome, 'ok', `a variante deveria sair ok: ${JSON.stringify(variant).slice(0, 1500)}`);
        // Valores conferidos no oráculo, os mesmos de create-variant.test.ts: a = 9 e b = 6 na seed abc.
        assert.deepEqual(variant.snapshot.params, { a: 9, b: 6 }, 'variants: params divergentes do oráculo');
        assert.deepEqual(
          variant.ir.fields.map((f: any) => f.name),
          NUMBER_INPUT_METADATA.answers_names,
          'variants: um campo por answers-name, na ordem do documento',
        );
        // A IR, o snapshot, os avisos e a ordem das listas chegam pela rota como o núcleo os produz.
        assert.deepStrictEqual(
          variant,
          await inProcessBody(engine.createVariant(variantRequest)),
          'variants: o corpo da rota deveria ser igual ao do motor em processo (determinismo)',
        );

        // Responde cada campo com o valor de correct_answers do próprio snapshot.
        const raw: Record<string, string> = {};
        for (const [name, value] of Object.entries(variant.snapshot.correct_answers as Record<string, unknown>)) {
          raw[name] = String(value);
        }
        const gradeRequest = { question_ref: NUMBER_INPUT, snapshot: variant.snapshot, raw_submitted_answers: raw };
        const graded = expectJson(await postJson(base, '/v1/grade', gradeRequest), 200, 'grade');
        assertValid('grade_response', graded);
        assert.equal(graded.outcome, 'ok', `a correção deveria sair ok: ${JSON.stringify(graded).slice(0, 1500)}`);
        // O snapshot é desta mesma versão do motor e do PL.
        assert.deepEqual(graded.version_mismatch, { engine: false, pl: false }, 'grade: sem divergência de versão');
        assert.deepStrictEqual(
          graded,
          await inProcessBody(engine.grade(gradeRequest)),
          'grade: o corpo da rota deveria ser igual ao do motor em processo (determinismo)',
        );
      });

      await t.test('POST /v1/grade de supported/random-params com a resposta certa → 200 com o corpo fixado', async () => {
        const gradeRequest = {
          question_ref: ref(TEST_COURSE, 'supported/random-params'),
          snapshot: RANDOM_PARAMS_SNAPSHOT,
          raw_submitted_answers: { v: '42' },
        };
        const graded = expectJson(await postJson(base, '/v1/grade', gradeRequest), 200, 'grade random-params');
        assertValid('grade_response', graded);
        assert.deepEqual(graded, EXPECTED_RANDOM_PARAMS_GRADE, 'grade random-params: corpo da correção pela rota');
      });

      await t.test('não suportada e quebrada voltam com HTTP 200 e outcome, e não com 4xx/5xx', async () => {
        // Só o 5xx gera nova tentativa, então um resultado semântico nunca vira erro HTTP.
        // Os valores esperados são os já fixados no motor em processo: inspect.test.ts (order-blocks:
        // só unsupported_elements, com ['pl-order-blocks']), create-variant.test.ts (generate-exception:
        // question_code_error na fase generate) e grade.test.ts (parse-exception: question_code_error
        // na fase parse).
        const orderBlocks = ref(TEST_COURSE, 'unsupported/order-blocks');
        const inspected = expectJson(
          await postJson(base, '/v1/inspect', { question_ref: orderBlocks }),
          200,
          'inspect order-blocks',
        );
        assertValid('inspect_response', inspected);
        assert.equal(inspected.outcome, 'unsupported', `inspect order-blocks: ${JSON.stringify(inspected).slice(0, 800)}`);
        assert.deepEqual(
          inspected.reasons.map((r: any) => r.code),
          ['unsupported_elements'],
          'inspect order-blocks: códigos de motivo',
        );
        assert.deepEqual(inspected.reasons[0].elements, ['pl-order-blocks'], 'inspect order-blocks: elementos fora do subconjunto');
        // o restante do corpo (metadados e o texto dos motivos) é o do motor em processo.
        assert.deepStrictEqual(
          inspected,
          await inProcessBody(engine.inspect({ question_ref: orderBlocks })),
          'inspect order-blocks: o corpo da rota deveria ser igual ao do motor em processo',
        );

        const unsupportedVariant = expectJson(
          await postJson(base, '/v1/variants', { question_ref: orderBlocks, seed: 'abc' }),
          200,
          'variants order-blocks',
        );
        assertValid('create_variant_response', unsupportedVariant);
        assert.equal(
          unsupportedVariant.outcome,
          'unsupported',
          `variants order-blocks: ${JSON.stringify(unsupportedVariant).slice(0, 800)}`,
        );
        // Os mesmos motivos da inspeção (inspect.test.ts, "POST variants em order-blocks").
        assert.deepEqual(
          unsupportedVariant.reasons.map((r: any) => r.code),
          ['unsupported_elements'],
          'variants order-blocks: códigos de motivo',
        );
        assert.deepEqual(unsupportedVariant.reasons[0].elements, ['pl-order-blocks'], 'variants order-blocks: elementos fora do subconjunto');
        assert.deepStrictEqual(
          unsupportedVariant,
          await inProcessBody(engine.createVariant({ question_ref: orderBlocks, seed: 'abc' })),
          'variants order-blocks: o corpo da rota deveria ser igual ao do motor em processo',
        );

        const generateException = { question_ref: ref(TEST_COURSE, 'broken/generate-exception'), seed: 'abc' };
        const brokenVariant = expectJson(
          await postJson(base, '/v1/variants', generateException),
          200,
          'variants generate-exception',
        );
        assertValid('create_variant_response', brokenVariant);
        assert.equal(brokenVariant.outcome, 'broken', `variants generate-exception: ${JSON.stringify(brokenVariant).slice(0, 800)}`);
        assert.equal(brokenVariant.reason.code, 'question_code_error', 'variants generate-exception: reason.code');
        assert.equal(brokenVariant.reason.phase, 'generate', 'variants generate-exception: reason.phase');
        // create-variant.test.ts (assertBroken): uma variante quebrada destas fixtures não tem avisos.
        assert.deepEqual(brokenVariant.warnings, [], 'variants generate-exception: sem avisos');
        // a mensagem e o technical_detail também chegam pela rota como o núcleo os produz.
        assert.deepStrictEqual(
          brokenVariant,
          await inProcessBody(engine.createVariant(generateException)),
          'variants generate-exception: o corpo da rota deveria ser igual ao do motor em processo',
        );

        // parse-exception: o generate não lança (correct_answers.v = 1); a quebra só vem no parse.
        const parseRef = ref(TEST_COURSE, 'broken/parse-exception');
        const parseVariant = expectJson(
          await postJson(base, '/v1/variants', { question_ref: parseRef, seed: 'abc' }),
          200,
          'variants parse-exception',
        );
        assertValid('create_variant_response', parseVariant);
        assert.equal(parseVariant.outcome, 'ok', `a variante de parse-exception deveria sair ok: ${JSON.stringify(parseVariant).slice(0, 800)}`);
        const parseGradeRequest = {
          question_ref: parseRef,
          snapshot: parseVariant.snapshot,
          raw_submitted_answers: { v: '1' },
        };
        const brokenGrade = expectJson(await postJson(base, '/v1/grade', parseGradeRequest), 200, 'grade parse-exception');
        assertValid('grade_response', brokenGrade);
        assert.equal(brokenGrade.outcome, 'broken', `grade parse-exception: ${JSON.stringify(brokenGrade).slice(0, 800)}`);
        assert.equal(brokenGrade.reason.code, 'question_code_error', 'grade parse-exception: reason.code');
        assert.equal(brokenGrade.reason.phase, 'parse', 'grade parse-exception: reason.phase');
        // grade.test.ts: nem o parse-exception nem o pl-integer-input imprimem, e o snapshot é desta
        // mesma versão do motor e do PL.
        assert.deepEqual(brokenGrade.warnings, [], 'grade parse-exception: sem avisos');
        assert.deepEqual(brokenGrade.version_mismatch, { engine: false, pl: false }, 'grade parse-exception: sem divergência de versão');
        assert.deepStrictEqual(
          brokenGrade,
          await inProcessBody(engine.grade(parseGradeRequest)),
          'grade parse-exception: o corpo da rota deveria ser igual ao do motor em processo',
        );
      });

      await t.test('JSON malformado → 400 invalid_request', async () => {
        for (const route of ['/v1/inspect', '/v1/variants', '/v1/grade']) {
          expectError(await request(base, 'POST', route, '{"question_ref": {'), 400, 'invalid_request', `${route} malformado`);
        }
      });

      await t.test('fora do esquema → 400 invalid_request com details', async () => {
        const cases: [string, unknown][] = [
          ['/v1/inspect', { question_ref: NUMBER_INPUT, chave_extra: true }],
          ['/v1/inspect', {}],
          ['/v1/variants', { question_ref: NUMBER_INPUT }],
          ['/v1/grade', { question_ref: NUMBER_INPUT, raw_submitted_answers: {} }],
        ];
        for (const [route, value] of cases) {
          const what = `${route} ${JSON.stringify(value)}`;
          const body = expectError(await postJson(base, route, value), 400, 'invalid_request', what);
          assert.ok(
            Array.isArray(body.error.details) && body.error.details.length > 0,
            `${what}: o 400 de esquema deveria trazer details não vazio`,
          );
        }
      });

      await t.test('JSON válido que não é objeto → 400 invalid_request com details, e não 500', async () => {
        // Os $defs inspect_request, create_variant_request e grade_request são "type": "object": um
        // corpo null, lista, texto ou número está fora do esquema (400), e não é falha do motor (500,
        // o único código que o núcleo repete).
        for (const route of ['/v1/inspect', '/v1/variants', '/v1/grade']) {
          for (const text of ['null', '[]', '"x"', '1']) {
            const what = `${route} com corpo ${text}`;
            const body = expectError(await request(base, 'POST', route, text), 400, 'invalid_request', what);
            assert.ok(
              Array.isArray(body.error.details) && body.error.details.length > 0,
              `${what}: o 400 de esquema deveria trazer details não vazio`,
            );
          }
        }
      });

      await t.test('qid inexistente → 404 not_found nas três rotas', async () => {
        const missing = ref(EXAMPLE, 'element/nao-existe');
        expectError(await postJson(base, '/v1/inspect', { question_ref: missing }), 404, 'not_found', 'inspect qid inexistente');
        expectError(
          await postJson(base, '/v1/variants', { question_ref: missing, seed: 'abc' }),
          404,
          'not_found',
          'variants qid inexistente',
        );
        // A correção também resolve a questão: um pedido válido pelo esquema, com qid ausente, dá 404.
        expectError(
          await postJson(base, '/v1/grade', { question_ref: missing, snapshot: ANY_SNAPSHOT, raw_submitted_answers: {} }),
          404,
          'not_found',
          'grade qid inexistente',
        );
      });

      await t.test('rota desconhecida → 404 route_not_found', async () => {
        for (const [method, route] of [
          ['GET', '/v1/desconhecida'],
          ['POST', '/v1/desconhecida'],
          ['GET', '/'],
        ] as const) {
          const r = method === 'POST' ? await postJson(base, route, {}) : await request(base, method, route);
          expectError(r, 404, 'route_not_found', `${method} ${route}`);
        }
      });

      await t.test('método errado numa rota conhecida → 405 method_not_allowed', async () => {
        expectError(await request(base, 'GET', '/v1/inspect'), 405, 'method_not_allowed', 'GET /v1/inspect');
        expectError(await request(base, 'GET', '/v1/variants'), 405, 'method_not_allowed', 'GET /v1/variants');
        expectError(await request(base, 'GET', '/v1/grade'), 405, 'method_not_allowed', 'GET /v1/grade');
        expectError(await postJson(base, '/v1/health', {}), 405, 'method_not_allowed', 'POST /v1/health');
      });

      await t.test('corpo de 3 MiB com Content-Length → 413 payload_too_large', async () => {
        const size = 3 * 1024 * 1024;
        const payload = Buffer.alloc(size, 0x61); // 'a'
        payload.write('{"pad":"', 0, 'utf8');
        payload.write('"}', size - 2, 'utf8');
        const r = await sendLarge(base, '/v1/inspect', payload);
        expectError(r, 413, 'payload_too_large', 'corpo de 3 MiB');
      });

      await t.test('corpo de 1,5 MiB (abaixo do limite de 2 MiB) não recebe 413', async () => {
        // O limite é de 2 MiB: um corpo de 1,5 MiB é lido inteiro e recusado pelo esquema
        // (chave "pad" desconhecida), com 400, e não com 413.
        const size = Math.floor(1.5 * 1024 * 1024);
        const payload = Buffer.alloc(size, 0x61); // 'a'
        payload.write('{"pad":"', 0, 'utf8');
        payload.write('"}', size - 2, 'utf8');
        const r = await sendLarge(base, '/v1/inspect', payload);
        expectError(r, 400, 'invalid_request', 'corpo de 1,5 MiB');
      });

      await t.test('o servidor continua atendendo depois dos erros', async () => {
        const body = expectJson(await request(base, 'GET', '/v1/health'), 200, 'health depois dos erros');
        assertValid('health_response', body);
      });
    } finally {
      await server.close();
    }
  });
});

test('depois de engine.close(), um pedido dá 500 internal_error sem pilha e com mensagem em português', { timeout: 300_000 }, async () => {
  // Motor criado aqui (e não por withEngine) porque o próprio teste o fecha antes do pedido.
  const engine = await createEngine(
    loadConfig({ ...process.env, ENGINE_WORKERS: '2', ENGINE_QUESTION_TIMEOUT_MS: '5000' }),
  );
  let engineClosed = false;
  let server: { url: string; close(): Promise<void> } | undefined;
  try {
    server = await startServer(engine, { host: HOST, port: 0 });
    await engine.close();
    engineClosed = true;

    const inspect = expectError(
      await postJson(server.url, '/v1/inspect', { question_ref: NUMBER_INPUT }),
      500,
      'internal_error',
      'inspect depois de close()',
    );
    assertNoStack(inspect, 'inspect depois de close()');

    const health = expectError(await request(server.url, 'GET', '/v1/health'), 500, 'internal_error', 'health depois de close()');
    assertNoStack(health, 'health depois de close()');
  } finally {
    if (server) await server.close();
    if (!engineClosed) await engine.close();
  }
});
