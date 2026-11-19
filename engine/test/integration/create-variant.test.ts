// Teste de integração da criação de variante.
// Cobre: a criação de variante, o determinismo, os erros e os avisos;
// as bordas de seed, commit, qid, repositório e preferências.
// Também: os avisos da quebra por tempo esgotado e as
// variantes com imagens geradas pedidas ao mesmo tempo. E ainda: o
// caminho absoluto do servidor fora de reason.message e de warnings[].message, o snapshot
// completo das demais fixtures de supported/ e o eco no console do generate com marcador.
// Fixtures: engine/test/fixtures/courses/cq-test-course/questions/{supported,broken}/*, mais
// supported/variant-console-phases, broken/variant-long-output e variant-console-generate
// (esta última fica fora de supported/, cuja tabela por questão é a de ir.test.ts).
//
// Os valores de snapshot marcados "PL a193f72" saíram do próprio código do PL: as fases generate e
// prepare das fixtures rodadas pelo CodeCallerNative e pelo zygote.py do PL, num container
// descartável da imagem fixada, espelhando processQuestionHtml/processQuestionServer do freeform.js.
// O mesmo roteiro reproduz os valores do oráculo (element/numberInput seed abc: a = 9, b = 6; e os
// params de element/multipleChoice seed 17we19m, iguais aos da tabela variants do pl-oracle).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import type { Engine, EngineResult } from '../../src/engine.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, PL_COMMIT, TEST_COURSE, assertPortuguese, ref, seeds } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

type Base = { repo: string; commit: string };

// PL a193f72, supported/multi-part: pl-multiple-choice.py e pl-checkbox.py gravam em params[name] as
// opções exibidas ({key, html, feedback[, score]}) e em correct_answers[name] a correta (mc) ou a lista
// das corretas (cb); order="fixed" mantém a ordem do documento. num, int e txt vêm do server.py.
const MULTI_PART_PARAMS = {
  x: 3,
  mc: [
    { key: 'a', html: 'Opção um', feedback: null, score: 0 },
    { key: 'b', html: 'Opção dois', feedback: null, score: 1 },
    { key: 'c', html: 'Opção três', feedback: null, score: 0 },
  ],
  cb: [
    { key: 'a', html: 'Item um', feedback: null },
    { key: 'b', html: 'Item dois', feedback: null },
    { key: 'c', html: 'Item três', feedback: null },
    { key: 'd', html: 'Item quatro', feedback: null },
  ],
};

const MULTI_PART_CORRECT_ANSWERS = {
  num: 6.5,
  int: '1F',
  txt: 'Brasil',
  mc: { key: 'b', html: 'Opção dois', feedback: null, score: 1 },
  cb: [
    { key: 'a', html: 'Item um', feedback: null },
    { key: 'c', html: 'Item três', feedback: null },
  ],
};

// PL a193f72, supported/random-params: o zygote faz random.seed(variant_seed) antes do generate, e o
// server.py sorteia r = random.randint(1, 10**9). Um valor por seed de seeds(5).
const RANDOM_PARAMS_R: Record<string, number> = {
  '9ix': 447342178,
  '17we19m': 147104152,
  grnr17: 79590372,
  '1oo1irw': 263214772,
  xjb8jh: 399396944,
};

type SnapshotData = { params: Record<string, unknown>; correct_answers: Record<string, unknown> };

// PL a193f72, supported/element-hides-answer seed abc: builtin-grading="false" (pl-multiple-choice) e
// hide-answer-panel="true" (pl-checkbox) não impedem o prepare de gravar as opções e o gabarito; sem
// order, as duas listas saem na ordem sorteada com a seed da variante.
const HIDES_MC_SIM = { key: 'b', html: 'Sim', feedback: null, score: 1 };
const HIDES_CB_CERTA = { key: 'a', html: 'Certa', feedback: null };

// PL a193f72, supported/ir-course-media seed abc: order="fixed". O html da opção é o conteúdo do
// pl-answer serializado pelo pl.inner_html do PL (lxml, em ASCII), com o directory preservado e o "ã"
// do alt como referência numérica. É o dado do PL no snapshot, e não HTML canônico da IR (a regra das
// referências numéricas vale para a IR).
const COURSE_FIGURE_OPTION = {
  key: 'a',
  html: '<pl-figure file-name="x.png" directory="clientFilesCourse" alt="Curso"></pl-figure>',
  feedback: null,
  score: 1,
};

/**
 * params e correct_answers do snapshot, seed abc, das demais fixtures de supported/ e da variant-console-generate,
 * conferidos por valor. [F] PL a193f72: generate e prepare de cada fixture pelo CodeCallerNative e pelo zygote.py,
 * num container descartável da imagem fixada, pelo roteiro do cabeçalho. Sem
 * elemento de escolha, params fica {} e correct_answers é o que o server.py grava.
 */
const SNAPSHOT_DATA: Record<string, SnapshotData> = {
  'supported/element-hides-answer': {
    params: {
      cb: [HIDES_CB_CERTA, { key: 'b', html: 'Errada', feedback: null }],
      mc: [{ key: 'a', html: 'Não', feedback: null, score: 0 }, HIDES_MC_SIM],
    },
    correct_answers: { dentro: 1, num: 1, txt: 'a', visivel: 1, cb: [HIDES_CB_CERTA], mc: HIDES_MC_SIM },
  },
  'supported/ir-course-media': {
    params: {
      mc: [
        COURSE_FIGURE_OPTION,
        { key: 'b', html: '<pl-figure file-name="x.png" alt="Quest&#227;o"></pl-figure>', feedback: null, score: 0 },
      ],
    },
    correct_answers: { mc: COURSE_FIGURE_OPTION },
  },
  'supported/inspect-underscore-alias': { params: {}, correct_answers: { v: 31 } },
  'supported/ir-submission-panel': { params: {}, correct_answers: { sub: 1, v: 1 } },
  'supported/ir-hidden-question-field': { params: {}, correct_answers: { h: 1, v: 1 } },
  'supported/ir-markup': { params: {}, correct_answers: { x: 2 } },
  'supported/latex': { params: {}, correct_answers: { z: 1 } },
  // "sem" não tem gabarito (server.py): a chave não existe em correct_answers.
  'supported/empty-answer-render': { params: {}, correct_answers: { v: 1 } },
  ...Object.fromEntries(
    [
      'supported/console-output',
      'supported/panels',
      'supported/big-media',
      'supported/single-variant',
      'supported/no-show-correct-answer',
      'supported/ir-media-extra',
      'supported/ir-media-escape',
      'supported/ir-media-types',
      'supported/ir-media-timeout',
      'supported/variant-console-phases',
      'variant-console-generate',
    ].map((qid): [string, SnapshotData] => [qid, { params: {}, correct_answers: { v: 1 } }]),
  ),
};

/** O PNG 1x1 que o file() de supported/variant-console-phases devolve para "x.png" (server.py, X_PNG). */
const X_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mMQqTgBAAH4AVX3uUk4AAAAAElFTkSuQmCC';

/**
 * Última linha do traceback de broken/generate-exception, com os códigos ANSI do rich.
 * [F] PL a193f72: a fixture rodada pelo CodeCallerNative num container descartável da imagem fixada
 * dá um outputBoth de 415 caracteres, que termina com esta linha; o excepthook
 * do zygote usa Console(force_terminal=True) e export_text(styles=True)
 * (python/prairielearn/internal/traceback.py). O ANSI fica como o PL o produz.
 */
const GENERATE_EXCEPTION_LAST_LINE = '\u001b[1;91mValueError: \u001b[0mfalha proposital';

/**
 * Nome do diretório de fixtures/cq-test-course na área de repositórios: sha256_hex do repo (vetores
 * de teste). Só aparece num caminho interno do motor, que é detalhe técnico.
 */
const TEST_COURSE_REPO_DIR = '511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d';

/** O mesmo, para https://github.com/PrairieLearn/PrairieLearn (vetores de teste). */
const EXAMPLE_REPO_DIR = '70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7';

/** Área de repositórios do motor, com o mesmo padrão de ir.test.ts e inspect.test.ts. */
const REPOS_ROOT = process.env.ENGINE_REPOS_ROOT ?? '/repos';

/**
 * Trechos de caminho absoluto do servidor. Em reason.message e em
 * warnings[].message, um caminho como /repos/… é detalhe técnico. [F] No PL a193f72 (freeform.js,
 * processQuestionServer), as mensagens de quebra do server.py vêm prefixadas com o caminho do
 * server.py; o do exampleCourse, pelo link simbólico da imagem de teste, tem o realpath
 * /PrairieLearn/exampleCourse/…. O "/PrairieLearn/" solto fica de fora, porque é parte do repo do
 * EXAMPLE (https://github.com/PrairieLearn/PrairieLearn), que uma mensagem pode citar.
 */
const SERVER_PATH_FRAGMENTS = [
  ...new Set([`${REPOS_ROOT}/`, '/repos/', TEST_COURSE_REPO_DIR, EXAMPLE_REPO_DIR, '/PrairieLearn/apps/', '/PrairieLearn/exampleCourse/']),
];

/** A mensagem (reason.message ou warnings[].message) sem caminho absoluto do servidor. */
function assertNoServerPath(message: string, what: string): void {
  for (const fragment of SERVER_PATH_FRAGMENTS) {
    assert.ok(
      !message.includes(fragment),
      `${what}: a mensagem traz o caminho do servidor "${fragment}", que é detalhe técnico: ${message}`,
    );
  }
}

function variantRequest(base: Base, qid: string, seed: string, preferences?: Record<string, unknown>): Record<string, unknown> {
  const body: Record<string, unknown> = { question_ref: ref(base, qid), seed };
  if (preferences !== undefined) body.preferences = preferences;
  return body;
}

/** Cria a variante, exige 200 e um corpo válido contra create_variant_response. */
async function createVariant(
  engine: Engine,
  base: Base,
  qid: string,
  seed: string,
  preferences?: Record<string, unknown>,
): Promise<any> {
  const body = ok200(await engine.createVariant(variantRequest(base, qid, seed, preferences)));
  assertValid('create_variant_response', body);
  return body;
}

/**
 * Exige o status e o código de erro, com corpo válido contra error_response e error.message em
 * português.
 */
function assertError(result: EngineResult, status: number, code: string, what: string): void {
  assert.equal(result.status, status, `${what}: esperado ${status}, veio ${result.status}: ${JSON.stringify(result.body).slice(0, 800)}`);
  assertValid('error_response', result.body);
  assert.equal((result.body as any).error.code, code, `${what}: código de erro inesperado`);
  assertPortuguese((result.body as any).error.message);
}

/**
 * Exige outcome "broken" com o código e a fase dados, e warnings vazio: nenhuma fixture broken/* deste
 * arquivo tem imagem comum ausente nem file(), e console_output só vale quando a operação concluiu;
 * numa quebra, a saída vai para o technical_detail. O motivo funcional fica sem o
 * caminho absoluto do servidor, que o PL põe nas mensagens de quebra.
 */
function assertBroken(body: any, code: string, phase: string, what: string): void {
  assert.equal(body.outcome, 'broken', `${what}: esperado outcome "broken", veio ${JSON.stringify(body).slice(0, 800)}`);
  assert.equal(body.reason.code, code, `${what}: reason.code inesperado`);
  assert.equal(body.reason.phase, phase, `${what}: reason.phase inesperado`);
  assertPortuguese(body.reason.message);
  assertNoServerPath(body.reason.message, `${what}: reason.message`);
  assert.deepEqual(body.warnings, [], `${what}: uma variante quebrada destas fixtures não tem avisos`);
}

/**
 * O motivo funcional não traz o detalhe técnico (engine-api.schema.json, broken_reason.message:
 * "Frase funcional em português, sem detalhe técnico"): a mensagem da exceção e a do checkData ficam no
 * technical_detail.
 */
function assertMessageWithoutDetail(body: any, fragments: string[], what: string): void {
  for (const fragment of fragments) {
    assert.ok(
      !body.reason.message.includes(fragment),
      `${what}: reason.message não pode trazer o detalhe técnico "${fragment}": ${body.reason.message}`,
    );
  }
}

/** Pares "code/phase" distintos dos avisos, em ordem alfabética (a ordem dos avisos não é fixada). */
function warningPairs(body: any): string[] {
  return [...new Set<string>(body.warnings.map((w: any) => `${w.code}/${w.phase}`))].sort();
}

/** technical_detail presente e com cada trecho dado (a mensagem, o traceback e a saída). */
function assertTechnicalDetailIncludes(body: any, fragments: string[], what: string): void {
  assert.equal(typeof body.reason.technical_detail, 'string', `${what}: technical_detail deve existir`);
  for (const fragment of fragments) {
    assert.ok(
      body.reason.technical_detail.includes(fragment),
      `${what}: technical_detail deveria conter "${fragment}": ${JSON.stringify(body.reason.technical_detail).slice(0, 1500)}`,
    );
  }
}

test('element/numberInput com seed abc: snapshot igual ao do oráculo e versões corretas', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, EXAMPLE, 'element/numberInput', 'abc');
    assert.equal(body.outcome, 'ok', 'a variante de referência deve sair "ok"');
    // Valores conferidos no oráculo: a = 9, b = 6, c = sqrt(117).
    assert.deepEqual(body.snapshot.params, { a: 9, b: 6 }, 'params divergentes do oráculo');
    assert.equal(body.snapshot.correct_answers.c_1, 10.816653826391969, 'correct_answers.c_1 divergente do oráculo');
    assert.equal(body.snapshot.seed, 'abc', 'o snapshot deve guardar a seed pedida');
    assert.equal(body.snapshot.engine_version, '1.0.0', 'engine_version do snapshot');
    assert.equal(body.snapshot.pl_commit, PL_COMMIT, 'pl_commit do snapshot');
    assert.equal(body.snapshot.ir_version, '1.0', 'ir_version do snapshot');
  });
});

test('o mesmo pedido duas vezes dá corpos idênticos', async () => {
  await withEngine({}, async (engine) => {
    const first = await createVariant(engine, EXAMPLE, 'element/numberInput', 'abc');
    const second = await createVariant(engine, EXAMPLE, 'element/numberInput', 'abc');
    assert.deepStrictEqual(second, first, 'a mesma entrada deve produzir a mesma saída');
  });
});

test('random-params: seeds diferentes sorteiam valores diferentes, e a mesma seed repete o valor', async () => {
  await withEngine({}, async (engine) => {
    const values: number[] = [];
    for (const seed of seeds(5)) {
      const body = await createVariant(engine, TEST_COURSE, 'supported/random-params', seed);
      assert.equal(body.outcome, 'ok', `random-params com seed ${seed} deve sair "ok"`);
      assert.equal(typeof body.snapshot.params.r, 'number', 'params.r deve ser numérico');
      values.push(body.snapshot.params.r);
    }
    assert.ok(new Set(values).size >= 4, `esperados pelo menos 4 valores distintos de r em 5 seeds, vieram ${JSON.stringify(values)}`);

    const seed = seeds(1)[0];
    const again = await createVariant(engine, TEST_COURSE, 'supported/random-params', seed);
    assert.equal(again.snapshot.params.r, values[0], 'a mesma seed deve sortear o mesmo r');
  });
});

test('multi-part: as opções de mc têm as chaves a, b e c, e o gabarito é b', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/multi-part', 'abc');
    assert.equal(body.outcome, 'ok', 'multi-part deve sair "ok"');
    const mc = body.snapshot.params.mc;
    assert.ok(Array.isArray(mc), 'params.mc deve ser a lista de opções exibidas');
    assert.deepEqual(
      mc.map((option: any) => option.key),
      ['a', 'b', 'c'],
      'chaves das opções de mc',
    );
    assert.equal(body.snapshot.correct_answers.mc.key, 'b', 'gabarito de mc');
  });
});

test('multi-part seed abc: snapshot.params e snapshot.correct_answers completos, iguais aos do PL', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/multi-part', 'abc');
    assert.equal(body.outcome, 'ok', 'multi-part deve sair "ok"');
    assert.deepEqual(body.snapshot.params, MULTI_PART_PARAMS, 'params de multi-part divergentes do PL');
    assert.deepEqual(
      body.snapshot.correct_answers,
      MULTI_PART_CORRECT_ANSWERS,
      'correct_answers de multi-part divergentes do PL (int fica a string "1F" do server.py)',
    );
    assert.deepEqual(body.snapshot.preferences, {}, 'multi-part não tem esquema de preferências');
  });
});

test('random-params: params e correct_answers de cada seed iguais aos do PL, com v = r', async () => {
  await withEngine({}, async (engine) => {
    for (const seed of seeds(5)) {
      const body = await createVariant(engine, TEST_COURSE, 'supported/random-params', seed);
      assert.equal(body.outcome, 'ok', `random-params com seed ${seed} deve sair "ok"`);
      const r = RANDOM_PARAMS_R[seed];
      assert.equal(typeof r, 'number', `seed ${seed} sem valor de referência do PL`);
      assert.deepEqual(body.snapshot.params, { r }, `params de random-params com seed ${seed}`);
      assert.deepEqual(
        body.snapshot.correct_answers,
        { v: body.snapshot.params.r },
        `correct_answers.v deve ser o r sorteado (server.py), seed ${seed}`,
      );
      assert.equal(body.snapshot.seed, seed, 'o snapshot deve guardar a seed pedida');
    }
  });
});

test('media e option-images seed abc: opções com figura no snapshot, iguais às do PL', async () => {
  await withEngine({}, async (engine) => {
    // PL a193f72, supported/media: order="fixed", a primeira opção é a correta; o html da opção é o
    // conteúdo do pl-answer, com o pl-figure ainda por renderizar. r0 = random.random() logo depois de
    // random.seed(variant_seed).
    const media = await createVariant(engine, TEST_COURSE, 'supported/media', 'abc');
    assert.equal(media.outcome, 'ok', 'media deve sair "ok"');
    const withFigure = { key: 'a', html: 'Com figura <pl-figure file-name="figura.png"></pl-figure>', feedback: null, score: 1 };
    assert.deepEqual(
      media.snapshot.params,
      { r0: 0.7110740091675601, mc: [withFigure, { key: 'b', html: 'Sem figura', feedback: null, score: 0 }] },
      'params de media divergentes do PL',
    );
    assert.deepEqual(media.snapshot.correct_answers, { mc: withFigure }, 'correct_answers de media divergentes do PL');

    // PL a193f72, supported/option-images: sem order, o pl-multiple-choice embaralha com a seed da
    // variante; com seed abc, a correta (a.png) fica em "a", e c.png vem antes de b.png.
    const images = await createVariant(engine, TEST_COURSE, 'supported/option-images', 'abc');
    assert.equal(images.outcome, 'ok', 'option-images deve sair "ok"');
    const figureA = { key: 'a', html: '<pl-figure file-name="a.png" alt="Figura A"></pl-figure>', feedback: null, score: 1 };
    assert.deepEqual(
      images.snapshot.params,
      {
        mc: [
          figureA,
          { key: 'b', html: '<pl-figure file-name="c.png" alt="Figura C"></pl-figure>', feedback: null, score: 0 },
          { key: 'c', html: '<pl-figure file-name="b.png" alt="Figura B"></pl-figure>', feedback: null, score: 0 },
        ],
      },
      'params de option-images divergentes do PL',
    );
    assert.deepEqual(images.snapshot.correct_answers, { mc: figureA }, 'correct_answers de option-images divergentes do PL');
  });
});

/** O snapshot inteiro da seed abc: params e correct_answers do PL, sem chaves a mais. */
function assertSnapshotData(body: any, qid: string, expected: SnapshotData): void {
  assert.equal(body.outcome, 'ok', `${qid} deve sair "ok": ${JSON.stringify(body).slice(0, 800)}`);
  assert.deepEqual(body.snapshot.params, expected.params, `${qid}: snapshot.params divergente do PL`);
  assert.deepEqual(body.snapshot.correct_answers, expected.correct_answers, `${qid}: snapshot.correct_answers divergente do PL`);
  // $defs.snapshot é estrito. Nenhuma destas fixtures tem esquema de preferências no info.json, e
  // extractDefaultPreferences(undefined) devolve {} (PL a193f72, lib/question-preferences.js).
  assert.deepEqual(
    body.snapshot,
    {
      seed: 'abc',
      params: expected.params,
      correct_answers: expected.correct_answers,
      preferences: {},
      engine_version: '1.0.0',
      pl_commit: PL_COMMIT,
      ir_version: '1.0',
    },
    `${qid}: snapshot completo`,
  );
}

test('demais fixtures de supported/ seed abc: snapshot.params e snapshot.correct_answers iguais aos do PL, sem chaves do motor', async () => {
  const slow = 'supported/ir-media-timeout';
  await withEngine({}, async (engine) => {
    for (const [qid, expected] of Object.entries(SNAPSHOT_DATA)) {
      if (qid === slow) continue;
      assertSnapshotData(await createVariant(engine, TEST_COURSE, qid, 'abc'), qid, expected);
    }
  });
  // O file() de lenta.png não termina: com o tempo curto, como em ir.test.ts, a mídia fica ausente e a
  // variante sai "ok" sem esperar o tempo padrão.
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    assertSnapshotData(await createVariant(engine, TEST_COURSE, slow, 'abc'), slow, SNAPSHOT_DATA[slow]);
  });
});

test('console-output: a variante sai "ok" com aviso console_output na fase generate', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/console-output', 'abc');
    assert.equal(body.outcome, 'ok', 'saída no console não pode quebrar a variante');
    assert.ok(
      body.warnings.some((w: any) => w.code === 'console_output' && w.phase === 'generate'),
      `esperado aviso {code: "console_output", phase: "generate"}, vieram ${JSON.stringify(body.warnings)}`,
    );
    // Só o generate imprime, e a questão não tem mídia: nenhum outro aviso.
    assert.deepEqual(warningPairs(body), ['console_output/generate'], `avisos inesperados: ${JSON.stringify(body.warnings)}`);
    for (const w of body.warnings) {
      assertPortuguese(w.message);
      assertNoServerPath(w.message, 'console-output: warnings[].message');
    }
  });
});

test('variant-console-generate: o texto impresso no generate fica fora da mensagem do aviso console_output', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'variant-console-generate', 'abc');
    assert.equal(body.outcome, 'ok', 'saída no console no generate não pode quebrar a variante');
    // [F] PL a193f72 (fixture rodada pelo CodeCallerNative num container descartável, pelo roteiro do
    // cabeçalho): o output do generate é "depuração ECO-GENERATE-3b7\n", e o prepare não imprime. A
    // questão não tem mídia: só o aviso do generate.
    assert.deepEqual(warningPairs(body), ['console_output/generate'], `avisos inesperados: ${JSON.stringify(body.warnings)}`);
    // a mensagem do aviso é funcional e sem detalhe técnico; o texto impresso é a saída do Python.
    // O marcador ECO-GENERATE-3b7, ao contrário de "depuração", não aparece numa frase funcional.
    for (const w of body.warnings) {
      assertPortuguese(w.message);
      assert.ok(!w.message.includes('ECO-GENERATE-3b7'), `a mensagem do aviso não pode trazer o texto impresso: ${w.message}`);
      assertNoServerPath(w.message, 'variant-console-generate: warnings[].message');
    }
  });
});

test('variant-console-phases: saída no console no prepare e no file vira console_output em cada fase, sem quebrar a variante nem a mídia', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/variant-console-phases', 'abc');
    assert.equal(body.outcome, 'ok', 'saída no console no prepare e no file não pode quebrar a variante');
    // server.py da fixture: o generate não imprime; o prepare e o file() de x.png imprimem. No PL a193f72,
    // processQuestionServer registra "output logged on console" em qualquer fase que conclui
    // (freeform.js); a fase do aviso segue o precedente do generate e do parse.
    assert.deepEqual(
      warningPairs(body),
      ['console_output/file', 'console_output/prepare'],
      `avisos esperados: console_output no prepare e no file, e nada mais; vieram ${JSON.stringify(body.warnings)}`,
    );
    // A mensagem do aviso é funcional, em português e sem detalhe técnico; o
    // texto impresso é a saída do Python, que é detalhe técnico.
    for (const w of body.warnings) {
      assertPortuguese(w.message);
      for (const printed of ['ECO-PREPARE-3b7', 'ECO-FILE-3b7']) {
        assert.ok(!w.message.includes(printed), `a mensagem do aviso não pode trazer o texto impresso "${printed}": ${w.message}`);
      }
      assertNoServerPath(w.message, 'variant-console-phases: warnings[].message');
    }
    // A única mídia é x.png (question.html): m1 por ser a primeira a aparecer, content_type pela
    // extensão, alt do pl-figure e os bytes que o file() devolve.
    assert.deepEqual(
      body.ir.media.map((m: any) => [m.id, m.origin, m.filename, m.content_type, m.alt, m.status]),
      [['m1', 'dynamic', 'x.png', 'image/png', 'Figura gerada', 'ok']],
      'a saída no console não torna a mídia ausente nem muda seus metadados',
    );
    const media = body.ir.media[0];
    assert.ok(
      Buffer.from(media.data_base64, 'base64').equals(Buffer.from(X_PNG_BASE64, 'base64')),
      'bytes de x.png diferentes do PNG devolvido pelo file()',
    );
    assert.equal(media.size_bytes, Buffer.from(X_PNG_BASE64, 'base64').length, 'size_bytes de x.png = tamanho do PNG');
  });
});

test('preferences: sem pedir, valem os padrões do info.json', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/preferences', 'abc');
    assert.equal(body.outcome, 'ok', 'preferences deve sair "ok"');
    assert.deepEqual(body.snapshot.params, { n: 2, cor: 'azul' }, 'params a partir das preferências padrão');
    assert.deepEqual(body.snapshot.preferences, { n: 2, cor: 'azul' }, 'snapshot.preferences com os padrões');
  });
});

test('preferences: as preferências pedidas sobrepõem os padrões', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'supported/preferences', 'abc', { n: 5, cor: 'verde' });
    assert.equal(body.outcome, 'ok', 'preferences com valores pedidos deve sair "ok"');
    assert.deepEqual(body.snapshot.params, { n: 5, cor: 'verde' }, 'params devem refletir as preferências pedidas');
    assert.deepEqual(body.snapshot.preferences, { n: 5, cor: 'verde' }, 'snapshot.preferences com os valores pedidos');
  });
});

test('preferences: um pedido parcial sobrepõe só as chaves pedidas, e as outras ficam com o padrão', async () => {
  // as padrão do info.json, sobrepostas pelas pedidas. [F] PL a193f72,
  // lib/question-variant.js: {...extractDefaultPreferences(schema), ...pedidas}.
  await withEngine({}, async (engine) => {
    const onlyN = await createVariant(engine, TEST_COURSE, 'supported/preferences', 'abc', { n: 5 });
    assert.equal(onlyN.outcome, 'ok', 'preferences com {n: 5} deve sair "ok"');
    assert.deepEqual(onlyN.snapshot.preferences, { n: 5, cor: 'azul' }, 'snapshot.preferences com n pedido e cor padrão');
    assert.deepEqual(onlyN.snapshot.params, { n: 5, cor: 'azul' }, 'params com n pedido e cor padrão');

    const onlyCor = await createVariant(engine, TEST_COURSE, 'supported/preferences', 'abc', { cor: 'verde' });
    assert.equal(onlyCor.outcome, 'ok', 'preferences com {cor: "verde"} deve sair "ok"');
    assert.deepEqual(onlyCor.snapshot.preferences, { n: 2, cor: 'verde' }, 'snapshot.preferences com cor pedida e n padrão');
    assert.deepEqual(onlyCor.snapshot.params, { n: 2, cor: 'verde' }, 'params com cor pedida e n padrão');
  });
});

test('preferences: questão sem esquema de preferências dá snapshot.preferences vazio', async () => {
  // [F] O info.json de element/numberInput no exampleCourse do PL a193f72 não tem "preferences", e
  // extractDefaultPreferences(undefined) devolve {} (lib/question-preferences.js).
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, EXAMPLE, 'element/numberInput', 'abc');
    assert.equal(body.outcome, 'ok', 'element/numberInput deve sair "ok"');
    assert.deepEqual(body.snapshot.preferences, {}, 'sem esquema, não há preferências padrão');
  });
});

test('preferences fora do esquema da questão são pedido inválido (400)', async () => {
  await withEngine({}, async (engine) => {
    const cases: Array<[string, Record<string, unknown>]> = [
      ['chave desconhecida', { x: 1 }],
      ['tipo errado', { n: 'cinco' }],
      ['fora do enum', { cor: 'roxo' }],
    ];
    for (const [what, preferences] of cases) {
      const result = await engine.createVariant(variantRequest(TEST_COURSE, 'supported/preferences', 'abc', preferences));
      assertError(result, 400, 'invalid_request', `preferências com ${what}`);
    }
  });
});

test('generate-exception: questão quebrada no generate, com motivo em português e detalhe técnico', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'broken/generate-exception', 'abc');
    assertBroken(body, 'question_code_error', 'generate', 'generate-exception');
    assert.equal(typeof body.reason.technical_detail, 'string', 'technical_detail deve existir');
    assert.ok(
      body.reason.technical_detail.includes('falha proposital'),
      'technical_detail deve conter a mensagem da exceção do código da questão',
    );
    // server.py: raise ValueError("falha proposital"). O texto da exceção é detalhe técnico.
    assertMessageWithoutDetail(body, ['falha proposital'], 'generate-exception');
    // Os códigos ANSI do rich ficam como o PL os produz. O outputBoth do PL tem
    // 415 caracteres, abaixo do corte de 8000, então a última linha do traceback chega inteira.
    assert.ok(
      body.reason.technical_detail.includes('\u001b['),
      `o technical_detail mantém os códigos ANSI do traceback: ${JSON.stringify(body.reason.technical_detail).slice(0, 1500)}`,
    );
    assert.ok(
      body.reason.technical_detail.includes(GENERATE_EXCEPTION_LAST_LINE),
      `a última linha do traceback deveria ser ${JSON.stringify(GENERATE_EXCEPTION_LAST_LINE)}, como no PL: ${JSON.stringify(body.reason.technical_detail).slice(0, 1500)}`,
    );
  });
});

test('illegal-data: alteração proibida de data no generate é questão quebrada por invalid_data', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'broken/illegal-data', 'abc');
    assertBroken(body, 'invalid_data', 'generate', 'illegal-data');
    // [F] checkData do PL a193f72 (freeform.js), reproduzido em test/unit/check-data.test.ts: a chave
    // "foo" que o generate acrescenta dá '"data" has invalid extra keys: foo'. Essa é a mensagem da
    // quebra, que vai para o technical_detail e não para o motivo funcional.
    assertTechnicalDetailIncludes(body, ['"data" has invalid extra keys: foo'], 'illegal-data');
    assertMessageWithoutDetail(body, ['invalid extra keys'], 'illegal-data');
  });
});

test('static-figure-missing: pl-figure estático sem arquivo quebra na preparação, como no PL', async () => {
  await withEngine({}, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'broken/static-figure-missing', 'abc');
    assertBroken(body, 'question_code_error', 'prepare', 'static-figure-missing');
    // [F] PL a193f72, elements/pl-figure/pl-figure.py (prepare): raise FileNotFoundError(f'File
    // "{file_name}" not found in directory "{file_directory}".'), com DIRECTORY_DEFAULT
    // "clientFilesQuestion". O traceback do zygote sai com cores ANSI (rich, force_terminal), que
    // separam as aspas do nome do arquivo; por isso a checagem usa trechos que as cores não cortam.
    assertTechnicalDetailIncludes(body, ['nao-existe.png', 'not found in directory'], 'static-figure-missing');
    // A mensagem do PL traz o diretório absoluto da questão na área de repositórios do motor
    // (<ENGINE_REPOS_ROOT>/<sha256_hex(repo)>/<commit>/questions/...). Esse caminho interno é detalhe
    // técnico e fica fora do motivo funcional (broken_reason.message).
    assertMessageWithoutDetail(body, ['not found in directory', TEST_COURSE_REPO_DIR], 'static-figure-missing');
  });
});

test('variant-long-output: saída e traceback acima de 8000 caracteres saem truncados no technical_detail, sem virar ir_contract_violation', async () => {
  await withEngine({}, async (engine) => {
    // server.py: print("x" * 20000) e raise ValueError("falha longa"). No PL a193f72, a saída do
    // processo filho (outputBoth) passa de 20000 caracteres, acima do maxLength 8000 do esquema.
    const result = await engine.createVariant(variantRequest(TEST_COURSE, 'broken/variant-long-output', 'abc'));
    assert.notEqual(result.status, 500, 'o technical_detail longo é da questão, não falha interna');
    const body = ok200(result);
    assertValid('create_variant_response', body);
    assertBroken(body, 'question_code_error', 'generate', 'variant-long-output');
    const detail = body.reason.technical_detail;
    assert.equal(typeof detail, 'string', 'technical_detail deve existir');
    assert.ok(detail.length > 0 && detail.length <= 8000, `technical_detail truncado em 8000 caracteres, veio ${detail.length}`);
    // o technical_detail traz a saída do Python, e não só a mensagem e o traceback.
    // [F] PL a193f72 (fixture rodada pelo CodeCallerNative num container descartável): o outputBoth tem
    // 20597 caracteres, os 20000 "x" do print seguidos do traceback do rich (cerca de 600 caracteres,
    // cuja linha de código mostra "x" * 20000 com ANSI, e não uma sequência de x). Qualquer corte de até
    // 8000 caracteres (início, fim ou início e fim) guarda mais de 1000 "x" seguidos.
    assert.ok(
      detail.includes('x'.repeat(1000)),
      `a saída do print deveria chegar ao technical_detail truncado: ${JSON.stringify(detail).slice(0, 600)}`,
    );
    assertMessageWithoutDetail(body, ['falha longa', 'xxxxxxxxxx'], 'variant-long-output');
  });
});

test('negative-weight: resposta que violaria a IR vira broken/ir_contract_violation na fase render, e não 500', async () => {
  await withEngine({}, async (engine) => {
    const result = await engine.createVariant(variantRequest(TEST_COURSE, 'broken/negative-weight', 'abc'));
    assert.notEqual(result.status, 500, 'violação do contrato causada pela questão não é falha interna');
    const body = ok200(result);
    assertValid('create_variant_response', body);
    assertBroken(body, 'ir_contract_violation', 'render', 'negative-weight');
    // question.html: weight="-1"; ir.schema.json, $defs.field.weight, tem minimum 0. O erro do validador
    // (ex.: "/fields/0/weight must be >= 0") é detalhe técnico e fica fora do motivo funcional (
    // engine-api.schema.json, broken_reason.message: "Frase funcional em português, sem detalhe técnico").
    assert.doesNotMatch(
      body.reason.message,
      /must be|should be|>=|\/fields\//i,
      'negative-weight: o detalhe da violação do contrato fica fora do motivo funcional',
    );
  });
});

test('generate-timeout: tempo esgotado no generate é broken/timeout, sem avisos', async () => {
  // server.py: só "while True: pass", sem print; question.html sem mídia. console_output só vale quando a
  // operação concluiu, e missing_media e media_error só existem para mídias.
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    const body = await createVariant(engine, TEST_COURSE, 'broken/generate-timeout', 'abc');
    assertBroken(body, 'timeout', 'generate', 'generate-timeout');
  });
});

test('pedidos inválidos dão 400 e referências inexistentes dão 404', async () => {
  await withEngine({}, async (engine) => {
    assertError(
      await engine.createVariant(variantRequest(EXAMPLE, 'element/numberInput', '1z141z4')),
      400,
      'invalid_request',
      'seed igual a 2^32',
    );
    assertError(
      await engine.createVariant(variantRequest({ repo: EXAMPLE.repo, commit: 'a193f72' }, 'element/numberInput', 'abc')),
      400,
      'invalid_request',
      'commit abreviado',
    );
    for (const commit of ['main', 'v1.0']) {
      assertError(
        await engine.createVariant(variantRequest({ repo: EXAMPLE.repo, commit }, 'element/numberInput', 'abc')),
        400,
        'invalid_request',
        `commit dado como branch ou tag (${commit})`,
      );
    }
    assertError(
      await engine.createVariant(variantRequest({ repo: EXAMPLE.repo, commit: 'ab'.repeat(32) }, 'element/numberInput', 'abc')),
      404,
      'not_found',
      'commit de 64 hexadecimais inexistente',
    );
    assertError(
      await engine.createVariant(variantRequest(EXAMPLE, 'element/naoExiste', 'abc')),
      404,
      'not_found',
      'qid inexistente',
    );
    assertError(
      await engine.createVariant({ question_ref: ref(EXAMPLE, 'element/numberInput') }),
      400,
      'invalid_request',
      'corpo sem seed',
    );
  });
});

test('seed, commit e qid fora do formato são pedido inválido (400), e repositório inexistente dá 404', async () => {
  // engine-api.schema.json: $defs.seed ^[0-9a-z]{1,7}$; question_ref.commit ^([0-9a-f]{40}|[0-9a-f]{64})$;
  // qid sem segmentos "." nem "..". Seed inválida (fora do formato de seed do PL):
  // pedido recusado como inválido, sem criar variante. Pedido fora do contrato → 400;
  // repositório ausente na área → 404.
  await withEngine({}, async (engine) => {
    for (const seed of ['ABC', '', '12345678', 'a-b']) {
      assertError(
        await engine.createVariant(variantRequest(EXAMPLE, 'element/numberInput', seed)),
        400,
        'invalid_request',
        `seed fora do formato (${JSON.stringify(seed)})`,
      );
    }
    assertError(
      await engine.createVariant(variantRequest({ repo: EXAMPLE.repo, commit: EXAMPLE.commit.toUpperCase() }, 'element/numberInput', 'abc')),
      400,
      'invalid_request',
      'commit com hexadecimais maiúsculos',
    );
    for (const qid of ['a/../b', './x']) {
      assertError(
        await engine.createVariant(variantRequest(EXAMPLE, qid, 'abc')),
        400,
        'invalid_request',
        `qid com segmento "." ou ".." (${qid})`,
      );
    }
    assertError(
      await engine.createVariant(variantRequest({ repo: 'https://example.invalid/nao-existe', commit: EXAMPLE.commit }, 'element/numberInput', 'abc')),
      404,
      'not_found',
      'repositório inexistente na área',
    );
  });
});

test('com 2 executores, variantes com imagens geradas pedidas ao mesmo tempo dão os mesmos corpos que em série', async () => {
  // Cada resultado simultâneo é idêntico ao do mesmo pedido feito sozinho. Estas questões
  // chamam o file() uma vez por imagem gerada, cada vez num executor recém-reiniciado, que é onde
  // sorteios e bytes de pedidos diferentes poderiam se misturar.
  const questions = ['supported/media', 'supported/ir-media-extra', 'supported/big-media'];
  const requests = questions.flatMap((qid) => seeds(4).map((seed) => ({ qid, seed, body: variantRequest(TEST_COURSE, qid, seed) })));
  assert.equal(requests.length, 12);
  await withEngine({ ENGINE_WORKERS: '2' }, async (engine) => {
    const serial: any[] = [];
    for (const req of requests) {
      const body = ok200(await engine.createVariant(req.body));
      assertValid('create_variant_response', body);
      assert.equal(body.outcome, 'ok', `${req.qid} seed ${req.seed} em série: ${JSON.stringify(body).slice(0, 800)}`);
      serial.push(body);
    }
    const parallel = (await Promise.all(requests.map((req) => engine.createVariant(req.body)))).map(ok200);
    for (let i = 0; i < requests.length; i++) {
      const label = `${requests[i].qid} seed ${requests[i].seed}`;
      assertValid('create_variant_response', parallel[i]);
      assert.deepStrictEqual(parallel[i], serial[i], `${label}: o corpo simultâneo difere do corpo em série`);
      if (requests[i].qid === 'supported/media') {
        // server.py de supported/media: o file() de aleatorio.png devolve str(random.random()), o primeiro
        // sorteio num executor recém-reiniciado com a seed da variante, igual ao r0 do generate.
        const aleatorio = parallel[i].ir.media.find((m: any) => m.filename === 'aleatorio.png');
        assert.ok(aleatorio, `${label}: aleatorio.png deveria estar nas mídias`);
        assert.equal(
          Number(Buffer.from(aleatorio.data_base64, 'base64').toString('utf8')),
          parallel[i].snapshot.params.r0,
          `${label}: o sorteio de aleatorio.png no pedido simultâneo deveria ser o r0 da própria variante`,
        );
      }
    }
  });
});
