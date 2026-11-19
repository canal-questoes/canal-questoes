// Teste de integração da correção, com snapshots criados no próprio teste.
// Cobre: a ordem do PL na correção (parâmetros alterados, parse do server.py, versões), o determinismo,
// o feedback, a vírgula decimal como erro de formato, a quebra no parse e no
// grade (dados ilegais, detalhe técnico sem os valores), os avisos de console no parse e no
// grade, a violação de contrato na correção, o score fora de [0, 1] e o
// 404 na correção.
// Fixtures: engine/test/fixtures/courses/cq-test-course/questions/{supported,broken}/* e as
// grade-* (asserção de valor em cada campo da saída da correção), entre elas
// broken/grade-parse-timeout (tempo esgotado no parse) e também
// broken/grade-parse-feedback (feedback gravado no parse), broken/grade-rewrite-submitted (resposta
// reescrita no grade) e broken/grade-print-parse-and-grade (saída no parse e no grade). As três ficam
// em broken/ porque têm um caminho que quebra no grade, e supported/ é a tabela SPECS do ir.test.ts.
// Também confere que o caminho absoluto do servidor fica fora de reason.message e de
// warnings[].message e que a mensagem do error_response vem em português.
// Valores esperados conferidos num espelho do freeform.js validado contra o oráculo.
// Os corpos das três fixtures de broken/ acima saíram das funções exportadas do próprio
// dist/question-servers/freeform.js do PL a193f72 (generate, prepare, parse, grade, com o pool de
// CodeCallerNative do PL), num container descartável da imagem fixada, com a gravação de
// lib/grading.js e models/grading-job.js aplicada por cima; o mesmo roteiro reproduz os corpos de
// grade-server-parse, grade-server-feedback e grade-print fixados abaixo.
// As notas parciais {score, weight} vêm de grade_answer_parameterized (python/prairielearn/
// grading_utils.py do PL a193f72): cria {"score": 0.0, "weight": weight}, grava o score e só acrescenta
// "feedback" quando o elemento dá um. pl-integer-input e pl-string-input não dão feedback. Conferido
// rodando o parse/grade desses elementos no container pl-oracle e nas submissões do oráculo.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { test } from 'node:test';

import * as cheerio from 'cheerio';

import type { Engine, EngineResult } from '../../src/engine.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, PL_COMMIT, TEST_COURSE, assertPortuguese, ref } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

const MULTI_PART = 'supported/multi-part';

/** version_mismatch de um snapshot criado pelo próprio motor. */
const SAME_VERSION = { engine: false, pl: false };

/** Marcador que faz o papel da resposta de um aluno (o mesmo de logging.test.ts). */
const RAW_MARKER = 'CANARIO-RESPOSTA-9f2c';

/** Raiz da área de repositórios do motor (o mesmo padrão de inspect.test.ts e ir.test.ts). */
const REPOS_ROOT = process.env.ENGINE_REPOS_ROOT ?? '/repos';

function sha256Hex(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

/**
 * Trechos de caminho absoluto do servidor, que são detalhe técnico em reason.message e em
 * warnings[].message. [F] No PL a193f72 (dist/question-servers/freeform.js,
 * processQuestionServer), as issues do server.py vêm prefixadas com path.join(question_dir,
 * 'server.py'): no motor, <ENGINE_REPOS_ROOT>/<sha256_hex(repo)>/<commit>/questions/<qid>/server.py
 * (test/Dockerfile). As questões do EXAMPLE resolvem para /PrairieLearn/exampleCourse/
 * (symlink do test/Dockerfile, pelo realpath), e o traceback do zygote cita /PrairieLearn/apps/. O
 * '/PrairieLearn/' solto fica de fora, porque casa com o identificador do repositório EXAMPLE.
 */
const SERVER_PATH_FRAGMENTS = [
  `${REPOS_ROOT}/`,
  '/repos/',
  sha256Hex(TEST_COURSE.repo),
  sha256Hex(EXAMPLE.repo),
  '/PrairieLearn/apps/',
  '/PrairieLearn/exampleCourse/',
];

/** nenhum caminho absoluto do servidor na mensagem funcional (reason.message ou warnings[].message). */
function assertNoServerPath(message: string, what: string): void {
  for (const fragment of SERVER_PATH_FRAGMENTS) {
    assert.ok(!message.includes(fragment), `${what}: caminho absoluto do servidor na mensagem ("${fragment}"): ${message}`);
  }
}

/**
 * [F] Mensagem do pl-integer-input (base 10) para resposta que não é inteiro, como o PL a193f72 a
 * produz: igual nas submissões de element/integerInput do oráculo (campo c_1) e no parse do elemento
 * rodado no container pl-oracle.
 */
const INTEGER_FORMAT_ERROR_HTML =
  "<p> Invalid format:\n            the submitted answer could not be interpreted as an integer.  No symbolic input, fractions, or scientific notation are allowed.\n    </p>\n    <p> Example <i>valid</i> inputs: <code class=\"me-2\">1</code> <code class=\"me-2\">-100</code> </p>\n    <p> Example <i>invalid</i> inputs: <code class=\"me-2\">6+9</code> <code class=\"me-2\">3.7</code> <code class=\"me-2\">3*pi</code> <code class=\"me-2\">6e19</code> <code class=\"me-2\">0x1a</code> <code class=\"me-2\">-0b0110</code> </p>\n    <hr>\n    <small>\n        <p> The submitted answer must be formatted as an integer. </p>\n    </small>";

/** O texto simples dessa mensagem pela regra de conversão de html em texto (tags fora, espaços colapsados, aparado). */
const INTEGER_FORMAT_ERROR_TEXT =
  'Invalid format: the submitted answer could not be interpreted as an integer. No symbolic input, fractions, ' +
  'or scientific notation are allowed. Example valid inputs: 1 -100 Example invalid inputs: 6+9 3.7 3*pi 6e19 ' +
  '0x1a -0b0110 The submitted answer must be formatted as an integer.';

/**
 * [F] Mensagem do pl-number-input de multi-part (sem atributos de formato) para '3,14', como o PL
 * a193f72 a produz: parse do elemento rodado no container pl-oracle.
 */
const NUMBER_FORMAT_ERROR_HTML =
  "<p>\n            Invalid format: The submitted answer could not be interpreted as a decimal number.\n        Do not write variables or other symbols.\n    </p>\n    <p>Example <i>valid</i> inputs: <code>5</code><code class=\"ms-3\">-17</code><code class=\"ms-3\">4.25e7</code><code class=\"ms-3\">3/7</code></p>\n    <p>Example <i>invalid</i> inputs: <code>6+9</code><code class=\"ms-3\">4*10^7</code><code class=\"ms-3\">3*pi</code><code class=\"ms-3\">3.6i</code></p>";

/** O texto simples dessa mensagem pela regra de conversão de html em texto. */
const NUMBER_FORMAT_ERROR_TEXT =
  'Invalid format: The submitted answer could not be interpreted as a decimal number. Do not write variables or ' +
  'other symbols. Example valid inputs: 5-174.25e73/7 Example invalid inputs: 6+94*10^73*pi3.6i';

/** Resposta crua toda correta de multi-part. */
const MULTI_PART_CORRECT: Record<string, unknown> = {
  num: '6.5',
  int: '1f',
  txt: 'brasil',
  mc: 'b',
  cb: ['a', 'c'],
};

// Os valores de multi-part abaixo saíram do próprio código do PL a193f72 (o
// question_phases do PL rodando generate/prepare/parse/grade dos elementos sobre a fixture, num
// container descartável da imagem fixada). Fontes [F]:
// - params e gabarito: pl-multiple-choice.py e pl-checkbox.py gravam em params[name] as opções
//   exibidas ({key, html, feedback[, score]}) e em correct_answers[name] a correta (mc) ou a lista das
//   corretas (cb), na ordem do documento (order="fixed"); num, int e txt vêm do server.py. As mesmas
//   constantes de create-variant.test.ts, copiadas porque aquele arquivo não as exporta. A correção
//   devolve params e gabarito como ficaram depois de parse/grade, e nem os
//   elementos nem o server.py de multi-part os alteram na correção.
// - submitted_answers: pl-number-input grava 6.5 (número) ou None quando não interpreta;
//   pl-integer-input com base="16" converte "1f" em 31, ou grava None; pl-string-input não reescreve o
//   texto por causa do ignore-case, e grava None no campo ausente; pl-multiple-choice e pl-checkbox não
//   reescrevem a resposta, nem com chave inválida.
// - partial_scores: grade_answer_parameterized (grading_utils.py) cria {score, weight} e só acrescenta
//   feedback quando ele é verdadeiro. pl-number-input dá "The correct answer used for grading was
//   6.5." quando acerta (show-correct-answer padrão true); pl-multiple-choice passa o feedback da
//   opção (null, então sem a chave); pl-checkbox grava {score, weight, feedback} com feedback =
//   {key: feedback da opção} para todas as opções dos params, e não só para as marcadas.
// - feedback {} e nenhum aviso: o server.py de multi-part só tem generate e não imprime, e os cinco
//   elementos não escrevem data["feedback"] nem imprimem.
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

/** submitted_answers de MULTI_PART_CORRECT, na forma que o PL a193f72 deixa depois do parse. */
const MULTI_PART_SUBMITTED = { num: 6.5, int: 31, txt: 'brasil', mc: 'b', cb: ['a', 'c'] };

/** O feedback por opção que o pl-checkbox grava na nota parcial de cb (as opções não têm feedback). */
const CB_OPTION_FEEDBACK = { a: null, b: null, c: null, d: null };

/** partial_scores de MULTI_PART_CORRECT, como o PL a193f72 os grava. */
const MULTI_PART_PARTIAL = {
  num: { score: 1, weight: 1, feedback: 'The correct answer used for grading was 6.5.' },
  int: { score: 1, weight: 1 },
  txt: { score: 1, weight: 1 },
  mc: { score: 1, weight: 1 },
  cb: { score: 1, weight: 1, feedback: CB_OPTION_FEEDBACK },
};

/**
 * Corpo inteiro da correção de multi-part: o da resposta toda correta, com os campos de
 * `overrides` trocados.
 */
function multiPartBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    outcome: 'ok',
    valid: true,
    format_errors: {},
    submitted_answers: MULTI_PART_SUBMITTED,
    partial_scores: MULTI_PART_PARTIAL,
    score: 1,
    feedback: {},
    params: MULTI_PART_PARAMS,
    correct_answers: MULTI_PART_CORRECT_ANSWERS,
    version_mismatch: SAME_VERSION,
    warnings: [],
    ...overrides,
  };
}

/** Corpo inteiro de uma correção inválida de multi-part: sem notas, com os erros e a resposta dados. */
function multiPartInvalidBody(formatErrors: Record<string, unknown>, submitted: Record<string, unknown>): Record<string, unknown> {
  return multiPartBody({ valid: false, format_errors: formatErrors, submitted_answers: submitted, partial_scores: null, score: null });
}

/** Cria a variante, exige outcome "ok" e devolve o snapshot. */
async function createSnapshot(engine: Engine, qid: string, seed = 'abc'): Promise<any> {
  const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, qid), seed }));
  assertValid('create_variant_response', body);
  assert.equal(body.outcome, 'ok', `a variante de ${qid} deve sair "ok": ${JSON.stringify(body).slice(0, 800)}`);
  return body.snapshot;
}

function gradeRequest(qid: string, snapshot: unknown, raw: Record<string, unknown>): Record<string, unknown> {
  return { question_ref: ref(TEST_COURSE, qid), snapshot, raw_submitted_answers: raw };
}

/** Corrige, exige 200 e um corpo válido contra grade_response. */
async function grade(engine: Engine, qid: string, snapshot: unknown, raw: Record<string, unknown>): Promise<any> {
  const body = ok200(await engine.grade(gradeRequest(qid, snapshot, raw)));
  assertValid('grade_response', body);
  return body;
}

/** Corrige e exige outcome "ok". */
async function gradeOk(engine: Engine, qid: string, snapshot: unknown, raw: Record<string, unknown>): Promise<any> {
  const body = await grade(engine, qid, snapshot, raw);
  assert.equal(body.outcome, 'ok', `a correção de ${qid} deve sair "ok": ${JSON.stringify(body).slice(0, 800)}`);
  return body;
}

/** Conversão definida do HTML do erro para texto simples: texto do HTML, espaços colapsados, aparado. */
function htmlToText(html: string): string {
  return cheerio.load(html, null, false).root().text().replace(/\s+/g, ' ').trim();
}

/**
 * Exige o status e o código de erro, com corpo válido contra error_response e error.message em
 * português (o mesmo critério de
 * create-variant.test.ts e inspect.test.ts).
 */
function assertError(result: EngineResult, status: number, code: string, what: string): void {
  assert.equal(result.status, status, `${what}: esperado ${status}, veio ${result.status}: ${JSON.stringify(result.body).slice(0, 800)}`);
  assertValid('error_response', result.body);
  assert.equal((result.body as any).error.code, code, `${what}: código de erro inesperado`);
  assertPortuguese((result.body as any).error.message);
}

/** Código e fase de cada aviso, na ordem (a mensagem é conferida à parte). */
function warningKinds(body: any): Array<{ code: unknown; phase: unknown }> {
  return body.warnings.map((w: any) => ({ code: w.code, phase: w.phase }));
}

/** O corpo sem warnings, para comparar o resto por igualdade quando a mensagem do aviso não é fixada. */
function withoutWarnings(body: any): Record<string, unknown> {
  const { warnings: _warnings, ...rest } = body;
  return rest;
}

/**
 * A mensagem do aviso é frase funcional em português, sem detalhe técnico. A
 * saída do Python é detalhe técnico (é conteúdo do technical_detail), então o texto impresso pelo
 * código da questão não aparece nela, nem o caminho do server.py que o PL põe na issue.
 */
function assertWarningMessages(body: any, printed: string, what: string): void {
  for (const w of body.warnings) {
    assertPortuguese(w.message);
    assert.ok(!w.message.includes(printed), `${what}: a mensagem do aviso traz a saída do console: ${w.message}`);
    assertNoServerPath(w.message, `${what}: aviso ${w.code}`);
  }
}

/**
 * Motivo de uma quebra da correção: frase funcional em português (engine-api.schema.json,
 * broken_reason.message), sem caminho do servidor e sem nenhum dos trechos técnicos dados.
 */
function assertReasonMessage(body: any, leaked: string[], what: string): void {
  assertPortuguese(body.reason.message);
  assertNoServerPath(body.reason.message, `${what}: reason.message`);
  for (const fragment of leaked) {
    assert.ok(!body.reason.message.includes(fragment), `${what}: a mensagem funcional traz "${fragment}": ${body.reason.message}`);
  }
}

test('multi-part todo correto: válida, nota 1 e as cinco notas parciais 1', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const body = await gradeOk(engine, MULTI_PART, snapshot, MULTI_PART_CORRECT);
    assert.equal(body.valid, true, 'a resposta toda correta deve ser válida');
    assert.equal(body.score, 1, 'nota final');
    assert.deepEqual(Object.keys(body.partial_scores).sort(), ['cb', 'int', 'mc', 'num', 'txt'], 'uma nota parcial por campo');
    for (const [name, partial] of Object.entries<any>(body.partial_scores)) {
      assert.equal(partial.score, 1, `nota parcial de ${name}`);
    }
    // Corpo inteiro: resposta interpretada ("1f" na base 16 vira 31), notas parciais com o weight e o
    // feedback que os elementos gravam, params e gabarito do snapshot, sem feedback nem avisos.
    assert.deepEqual(body, multiPartBody(), 'corpo da correção toda correta de multi-part');
  });
});

test('num "3,14": inválida, com erro de formato só em num e sem nota', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const body = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, num: '3,14' });
    assert.equal(body.valid, false, 'vírgula decimal é erro de formato, como no PL');
    assert.deepEqual(Object.keys(body.format_errors), ['num'], 'o erro de formato deve aparecer só em num');
    const error = body.format_errors.num;
    // Mensagem do PL para pl-number-input (conferida no oráculo): "Invalid format: The
    // submitted answer could not be interpreted as a decimal number."
    assert.match(error.html, /Invalid format/, `format_errors.num.html deve ser a mensagem do PL: ${error.html}`);
    assert.match(error.html, /could not be interpreted as a decimal number/, `format_errors.num.html: ${error.html}`);
    assert.equal(error.text, htmlToText(error.html), 'format_errors.num.text deve ser o texto do HTML do PL');
    assert.match(error.text, /^Invalid format: The submitted answer could not be interpreted as a decimal number\./, `text: ${error.text}`);
    assert.ok(!error.text.includes('<'), `format_errors.num.text deve ser texto simples: ${error.text}`);
    // Valor exato: html como o PL a193f72 o produz (parse do pl-number-input da fixture, com '3,14',
    // rodado no container pl-oracle) e text pela regra de conversão de html em texto, aplicada ao pé da letra (os
    // <code> adjacentes ficam colados, porque o PL não põe espaço entre eles).
    assert.equal(htmlToText(NUMBER_FORMAT_ERROR_HTML), NUMBER_FORMAT_ERROR_TEXT, 'autoconferência: o texto esperado segue a regra');
    assert.deepEqual(
      body.format_errors,
      { num: { html: NUMBER_FORMAT_ERROR_HTML, text: NUMBER_FORMAT_ERROR_TEXT } },
      'format_errors exatamente com a mensagem do pl-number-input, em html e em text',
    );
    // [F] pl-number-input.py: o parse grava None no campo que não interpreta.
    assert.equal(body.submitted_answers.num, null, 'num fica null em submitted_answers, como o PL deixa');
    assert.equal(body.score, null, 'sem nota quando a resposta é inválida');
    assert.equal(body.partial_scores, null, 'sem notas parciais quando a resposta é inválida');
    // Corpo inteiro: só num fica null; os outros campos são interpretados assim mesmo (o parse roda em
    // todos os elementos), e params e gabarito voltam como estavam no snapshot.
    assert.deepEqual(
      body,
      multiPartInvalidBody(
        { num: { html: NUMBER_FORMAT_ERROR_HTML, text: NUMBER_FORMAT_ERROR_TEXT } },
        { ...MULTI_PART_SUBMITTED, num: null },
      ),
      'corpo da correção de multi-part com num "3,14"',
    );
  });
});

test('resposta crua com campo desconhecido: tratada como no PL (válida, nota 1, campo preservado)', async () => {
  // Campos desconhecidos ou faltando são tratados como o PL trata. No PL, o
  // submitted_answer é o corpo inteiro (question-submission.js), e os elementos só olham os próprios
  // answers-name: a chave desconhecida fica em submitted_answers e não altera a nota.
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const body = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, campo_desconhecido: 'x' });
    assert.equal(body.valid, true, 'um campo desconhecido não invalida a resposta');
    assert.equal(body.score, 1, 'nota final');
    assert.deepEqual(body.format_errors, {}, 'sem erros de formato');
    assert.equal(body.submitted_answers.campo_desconhecido, 'x', 'o campo desconhecido é preservado, como no PL');
    assert.ok(!('campo_desconhecido' in body.partial_scores), 'o campo desconhecido não recebe nota parcial');
    assert.deepEqual(
      body,
      multiPartBody({ submitted_answers: { ...MULTI_PART_SUBMITTED, campo_desconhecido: 'x' } }),
      'corpo da correção com o campo desconhecido: igual ao da toda correta, com o campo em submitted_answers',
    );
  });
});

test('mc errado com o resto correto: nota parcial 0 em mc e nota final 0.8', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const body = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, mc: 'a' });
    assert.equal(body.valid, true, 'a resposta deve ser válida');
    assert.equal(body.partial_scores.mc.score, 0, 'nota parcial de mc');
    assert.equal(body.score, 0.8, 'nota final ponderada (4 de 5 partes)');
    // A opção "a" tem score 0 e feedback null nos params: nota parcial {score: 0, weight: 1}, sem feedback.
    assert.deepEqual(
      body,
      multiPartBody({
        submitted_answers: { ...MULTI_PART_SUBMITTED, mc: 'a' },
        partial_scores: { ...MULTI_PART_PARTIAL, mc: { score: 0, weight: 1 } },
        score: 0.8,
      }),
      'corpo da correção com mc "a"',
    );
  });
});

test('cb com uma marcação: lista ["a"] vale 0.5, e o valor único "a" corrige igual e preserva a forma', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const asList = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, cb: ['a'] });
    assert.equal(asList.score, 0.9, 'nota final com cb 0.5 e as outras quatro partes 1: (4 + 0.5) / 5');
    assert.equal(asList.valid, true, 'cb ["a"] deve ser válida');
    assert.equal(asList.partial_scores.cb.score, 0.5, 'net-correct com 1 de 2 corretas');

    const asString = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, cb: 'a' });
    assert.equal(asString.valid, asList.valid, 'valid de "a" igual ao de ["a"]');
    assert.equal(asString.score, asList.score, 'score de "a" igual ao de ["a"]');
    assert.deepEqual(asString.partial_scores.cb, asList.partial_scores.cb, 'partial_scores.cb de "a" igual ao de ["a"]');
    assert.equal(asString.submitted_answers.cb, 'a', 'submitted_answers.cb preserva a forma recebida, como no PL');
    // [F] pl-checkbox.py (grade): set("a") e set(["a"]) dão {"a"}, então a nota é a mesma, e o feedback
    // por opção cobre as quatro opções dos params.
    const halfCb = { score: 0.5, weight: 1, feedback: CB_OPTION_FEEDBACK };
    assert.deepEqual(
      asList,
      multiPartBody({
        submitted_answers: { ...MULTI_PART_SUBMITTED, cb: ['a'] },
        partial_scores: { ...MULTI_PART_PARTIAL, cb: halfCb },
        score: 0.9,
      }),
      'corpo da correção com cb ["a"]',
    );
    assert.deepEqual(
      asString,
      multiPartBody({
        submitted_answers: { ...MULTI_PART_SUBMITTED, cb: 'a' },
        partial_scores: { ...MULTI_PART_PARTIAL, cb: halfCb },
        score: 0.9,
      }),
      'corpo da correção com cb "a"',
    );
  });
});

test('sem o campo txt: inválida, com format_errors.txt', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const { txt: _omitted, ...withoutTxt } = MULTI_PART_CORRECT;
    const body = await gradeOk(engine, MULTI_PART, snapshot, withoutTxt);
    assert.equal(body.valid, false, 'campo faltando gera o erro de formato do elemento');
    assert.deepEqual(Object.keys(body.format_errors), ['txt'], 'só o campo faltando tem erro de formato');
    // [F] pl-string-input.py, linha 265: "No submitted answer."
    assert.match(body.format_errors.txt.html, /No submitted answer\./, 'mensagem do próprio elemento para resposta ausente');
    // html sem alteração e text pela regra de conversão de html em texto (sem tags nem entidades, fica igual).
    assert.deepEqual(
      body.format_errors,
      { txt: { html: 'No submitted answer.', text: 'No submitted answer.' } },
      'format_errors exatamente com a mensagem do elemento, em html e em text',
    );
    // [F] pl-string-input.py, linha 266: o parse grava None no campo ausente.
    assert.equal(body.submitted_answers.txt, null, 'o campo ausente fica null em submitted_answers, como o PL deixa');
    assert.equal(body.score, null, 'sem nota');
    assert.equal(body.partial_scores, null, 'sem notas parciais');
    assert.deepEqual(
      body,
      multiPartInvalidBody(
        { txt: { html: 'No submitted answer.', text: 'No submitted answer.' } },
        { num: 6.5, int: 31, mc: 'b', cb: ['a', 'c'], txt: null },
      ),
      'corpo da correção de multi-part sem txt',
    );
  });
});

test('cb com opção inexistente "&<z>": o html do erro vem escapado como o PL o produz, e o text decodifica as entidades', async () => {
  // [F] pl-checkbox.py, linhas 736-740 (PL a193f72): f"You selected an invalid option: {html.escape(one_bad_key)}",
  // antes das checagens de mínimo e máximo. Com uma só chave inválida, a mensagem não depende da
  // ordem do set. Conferido rodando o parse do elemento no container pl-oracle.
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const body = await gradeOk(engine, MULTI_PART, snapshot, { ...MULTI_PART_CORRECT, cb: ['&<z>'] });
    assert.equal(body.valid, false, 'opção inexistente é erro de formato');
    assert.deepEqual(
      body.format_errors,
      { cb: { html: 'You selected an invalid option: &amp;&lt;z&gt;', text: 'You selected an invalid option: &<z>' } },
      'html sem alteração e text com as entidades decodificadas',
    );
    assert.equal(body.score, null, 'sem nota');
    assert.equal(body.partial_scores, null, 'sem notas parciais');
    // O pl-checkbox não anula a lista inválida: cb fica como veio em submitted_answers.
    assert.deepEqual(
      body,
      multiPartInvalidBody(
        { cb: { html: 'You selected an invalid option: &amp;&lt;z&gt;', text: 'You selected an invalid option: &<z>' } },
        { ...MULTI_PART_SUBMITTED, cb: ['&<z>'] },
      ),
      'corpo da correção de multi-part com cb ["&<z>"]',
    );
  });
});

test('grade-changes-params: parâmetros e gabarito alterados no grade do código da questão voltam no resultado', async () => {
  // O grade altera parâmetros e gabarito, mas a nota é calculada antes
  // do grade do server.py: com v = 1 e o gabarito ainda 1, a nota é 1.
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, 'supported/grade-changes-params');
    const body = await gradeOk(engine, 'supported/grade-changes-params', snapshot, { v: '1' });
    assert.equal(body.valid, true, 'resposta válida');
    assert.equal(body.params.graded, true, 'params alterado no grade');
    assert.equal(body.correct_answers.v, 3, 'gabarito alterado no grade');
    assert.equal(body.score, 1, 'nota calculada antes do grade do server.py');
    // Corpo inteiro: o generate não grava params (snapshot com params {}), e o pl-integer-input grava o
    // inteiro interpretado em submitted_answers (pl-integer-input.py, linha 280).
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 1 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: { graded: true },
        correct_answers: { v: 3 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de grade-changes-params',
    );
  });
});

test('no-partial-credit: sem crédito parcial, uma parte errada zera a nota', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, 'supported/no-partial-credit');
    const half = await gradeOk(engine, 'supported/no-partial-credit', snapshot, { a: '1', b: '0' });
    assert.equal(half.valid, true);
    assert.equal(half.partial_scores.a.score, 1, 'nota parcial de a');
    assert.equal(half.partial_scores.b.score, 0, 'nota parcial de b');
    assert.equal(half.score, 0, 'tudo ou nada: com crédito parcial seria 0.5');
    assert.deepEqual(
      half.partial_scores,
      { a: { score: 1, weight: 1 }, b: { score: 0, weight: 1 } },
      'notas parciais completas, como grade_answer_parameterized as grava',
    );
    const full = await gradeOk(engine, 'supported/no-partial-credit', snapshot, { a: '1', b: '2' });
    assert.equal(full.score, 1, 'as duas partes certas dão 1');
    assert.deepEqual(
      full.partial_scores,
      { a: { score: 1, weight: 1 }, b: { score: 1, weight: 1 } },
      'notas parciais completas com as duas partes certas',
    );
    // Corpos inteiros. [F] PL a193f72: o pl-integer-input grava o inteiro interpretado; o server.py só
    // tem generate (sem params, sem print), e o gabarito volta como está no snapshot.
    const npcBody = (submitted: Record<string, unknown>, partial: Record<string, unknown>, score: number) => ({
      outcome: 'ok',
      valid: true,
      format_errors: {},
      submitted_answers: submitted,
      partial_scores: partial,
      score,
      feedback: {},
      params: {},
      correct_answers: { a: 1, b: 2 },
      version_mismatch: SAME_VERSION,
      warnings: [],
    });
    assert.deepEqual(
      half,
      npcBody({ a: 1, b: 0 }, { a: { score: 1, weight: 1 }, b: { score: 0, weight: 1 } }, 0),
      'corpo da correção de no-partial-credit com b errado',
    );
    assert.deepEqual(
      full,
      npcBody({ a: 1, b: 2 }, { a: { score: 1, weight: 1 }, b: { score: 1, weight: 1 } }, 1),
      'corpo da correção de no-partial-credit toda correta',
    );
  });
});

test('grade não roda quando o parse acusa erro de formato', async () => {
  // grade-echo lança exceção no grade: se o grade rodasse, o resultado seria "broken".
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, 'broken/grade-echo');
    const body = await gradeOk(engine, 'broken/grade-echo', snapshot, {});
    assert.equal(body.valid, false, 'campo ausente é erro de formato');
    assert.deepEqual(Object.keys(body.format_errors), ['v']);
    assert.equal(body.score, null);
    // Corpo inteiro. [F] pl-string-input.py, linhas 265-266: "No submitted answer." e None no campo.
    // warnings vazio: o grade, que imprime, não roda, e nem o parse nem o generate imprimem.
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'No submitted answer.', text: 'No submitted answer.' } },
        submitted_answers: { v: null },
        partial_scores: null,
        score: null,
        feedback: {},
        params: {},
        correct_answers: { v: 'a' },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de grade-echo com {}',
    );
  });
});

test('a correção não reavalia o suporte: server-render com snapshot corrige normalmente', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = {
      seed: 'abc',
      params: {},
      correct_answers: { v: 1 },
      preferences: {},
      engine_version: '1.0.0',
      pl_commit: PL_COMMIT,
      ir_version: '1.0',
    };
    const body = await gradeOk(engine, 'unsupported/server-render', snapshot, { v: '1' });
    assert.equal(body.valid, true);
    assert.equal(body.score, 1, 'a correção não recusa questão que a inspeção daria como não suportada');
    // Corpo inteiro. [F] PL a193f72: o render do server.py não roda na correção; o pl-integer-input
    // grava 1 e a nota parcial {score, weight} (grade_answer_parameterized, sem feedback). engine_version
    // 1.0.0 é a do motor (engine/package.json), então sem divergência de versão.
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 1 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 1 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de server-render',
    );
  });
});

test('a correção usa e devolve params e gabarito do snapshot recebido, sem regenerá-los pela seed', async () => {
  // O generate de random-params sorteia r = random.randint(1, 10**9) e grava o gabarito v = r. O
  // snapshot abaixo traz r = 1000000001, fora desse intervalo: nenhuma seed o reproduz, então um
  // motor que regenerasse params ou gabarito na correção devolveria outro r e daria nota 0.
  // [F] PL a193f72: o parse roda com variant.params e variant.true_answer, e o grade com os da submissão
  // que saiu do parse (dist/question-servers/freeform.js), sem novo generate; conferido rodando o
  // parse/grade do pl-integer-input sobre esse snapshot na imagem fixada.
  const snapshot = {
    seed: 'abc',
    params: { r: 1000000001 },
    correct_answers: { v: 1000000001 },
    preferences: {},
    engine_version: '1.0.0',
    pl_commit: PL_COMMIT,
    ir_version: '1.0',
  };
  await withEngine({}, async (engine) => {
    const body = await gradeOk(engine, 'supported/random-params', snapshot, { v: '1000000001' });
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 1000000001 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: { r: 1000000001 },
        correct_answers: { v: 1000000001 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção com o snapshot montado no teste',
    );
  });
});

test('parse-echo: aviso console_output na fase parse', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, 'supported/parse-echo');
    const body = await gradeOk(engine, 'supported/parse-echo', snapshot, { t: 'a' });
    assert.ok(
      body.warnings.some((w: any) => w.code === 'console_output' && w.phase === 'parse'),
      `esperado aviso {code: "console_output", phase: "parse"}: ${JSON.stringify(body.warnings)}`,
    );
    // Só o parse do server.py imprime (o pl-string-input não imprime, e não há grade no server.py):
    // exatamente um aviso.
    assert.deepEqual(warningKinds(body), [{ code: 'console_output', phase: 'parse' }], 'avisos da correção');
    assertWarningMessages(body, "{'t': 'a'}", 'parse-echo');
    assert.deepEqual(
      withoutWarnings(body),
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { t: 'a' },
        partial_scores: { t: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { t: 'a' },
        version_mismatch: SAME_VERSION,
      },
      'a saída no console não altera a correção',
    );

    // Com o marcador, a saída do console é a resposta crua ecoada; ela não vai para a mensagem do aviso.
    const echoed = await gradeOk(engine, 'supported/parse-echo', snapshot, { t: RAW_MARKER });
    assert.deepEqual(warningKinds(echoed), [{ code: 'console_output', phase: 'parse' }], 'avisos da correção com o marcador');
    assertWarningMessages(echoed, RAW_MARKER, 'parse-echo com o marcador');
  });
});

test('parse-echo com resposta ausente: inválida, e a saída do parse vira aviso console_output assim mesmo', async () => {
  // [F] freeform.js (PL a193f72), processQuestion: o erro de formato do pl-string-input não é fatal, e
  // o parse do server.py roda depois dele e imprime "{}" (a resposta crua). A operação conclui com
  // outcome "ok", então vale o console_output. pl-string-input.py: "No submitted
  // answer." e None no campo ausente. Conferido rodando o parse sobre a fixture na imagem fixada.
  await withEngine({}, async (engine) => {
    const qid = 'supported/parse-echo';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, {});
    assert.deepEqual(warningKinds(body), [{ code: 'console_output', phase: 'parse' }], 'avisos da correção inválida');
    assertWarningMessages(body, '{}', 'parse-echo com {}');
    assert.deepEqual(
      withoutWarnings(body),
      {
        outcome: 'ok',
        valid: false,
        format_errors: { t: { html: 'No submitted answer.', text: 'No submitted answer.' } },
        submitted_answers: { t: null },
        partial_scores: null,
        score: null,
        feedback: {},
        params: {},
        correct_answers: { t: 'a' },
        version_mismatch: SAME_VERSION,
      },
      'corpo da correção de parse-echo com {}',
    );
  });
});

test('server-grade-score: o grade do server.py sobrescreve a nota', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/server-grade-score';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '7' });
    assert.equal(body.valid, true, 'a resposta deve ser válida');
    assert.equal(body.score, 0.25, 'a nota definida pelo server.py prevalece');
    // O server.py só mexe no score: a nota parcial é a do elemento (7 = 7).
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 7 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 0.25,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de server-grade-score',
    );
  });
});

test('server-grade-score-out-of-range: a nota 1.5 do server.py é repassada como no PL', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/server-grade-score-out-of-range';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '7' });
    assert.equal(body.valid, true, 'a resposta deve ser válida');
    assert.equal(body.score, 1.5, 'o motor não limita a nota a [0, 1]');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 7 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1.5,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de server-grade-score-out-of-range',
    );
  });
});

test('server-grade-partial: a nota parcial muda no server.py, e a final é a calculada antes dele', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/server-grade-partial';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '7' });
    assert.equal(body.valid, true, 'a resposta deve ser válida');
    assert.equal(body.partial_scores.v.score, 0, 'a nota parcial alterada pelo server.py');
    assert.equal(body.score, 1, 'a nota final não é recalculada depois do grade do server.py');
    // O server.py troca só o score da parte: o weight gravado pelo elemento continua, sem chaves a mais.
    assert.deepEqual(body.partial_scores, { v: { score: 0, weight: 1 } }, 'partial_scores completo');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 7 },
        partial_scores: { v: { score: 0, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de server-grade-partial',
    );
  });
});

test('server-grade-format-error: erro de formato acrescentado no grade torna a resposta inválida', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/server-grade-format-error';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '13' });
    assert.equal(body.valid, false, 'erro de formato no grade deixa a resposta inválida');
    assert.equal(body.format_errors.v.html, 'Resposta proibida', 'a mensagem do server.py, como veio');
    assert.equal(body.score, null, 'sem nota quando a resposta é inválida');
    assert.equal(body.partial_scores, null, 'sem notas parciais quando a resposta é inválida');
    // Corpo inteiro: só a chave v tem erro, com o text igual ao html (texto sem tags nem entidades).
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'Resposta proibida', text: 'Resposta proibida' } },
        submitted_answers: { v: 13 },
        partial_scores: null,
        score: null,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de server-grade-format-error',
    );
  });
});

test('parse-changes-params: parâmetros e gabarito alterados no parse voltam no resultado', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/parse-changes-params';
    const snapshot = await createSnapshot(engine, qid);
    assert.equal(snapshot.correct_answers.v, 1, 'o gabarito da variante vem do generate');
    const body = await gradeOk(engine, qid, snapshot, { v: '2' });
    assert.equal(body.params.parsed, true, 'params.parsed gravado pelo parse');
    assert.equal(body.correct_answers.v, 2, 'correct_answers.v alterado pelo parse');
    // O grade roda com os dados do parse: o gabarito 2 corrige a resposta 2.
    assert.equal(body.valid, true, 'a resposta deve ser válida');
    assert.equal(body.score, 1, 'o grade usa o gabarito alterado pelo parse');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 2 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: { parsed: true },
        correct_answers: { v: 2 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção de parse-changes-params',
    );
  });
});

test('parse-changes-params com "abc": resposta inválida, mas params e gabarito alterados pelo parse do server.py voltam assim mesmo', async () => {
  // [F] freeform.js (PL a193f72), processQuestion: o erro de formato do elemento não é fatal, então o
  // parse do server.py roda depois dele; parse() devolve params e true_answer do fim do parse, e
  // grading.js grava ...data mesmo com gradable false. O pl-integer-input recusa "abc" na base 10 e
  // grava None em submitted_answers (pl-integer-input.py, linhas 264-274).
  await withEngine({}, async (engine) => {
    const qid = 'supported/parse-changes-params';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: 'abc' });
    assert.equal(body.valid, false, 'o pl-integer-input recusa "abc"');
    assert.deepEqual(Object.keys(body.format_errors), ['v'], 'o erro de formato aparece só em v');
    assert.equal(body.format_errors.v.html, INTEGER_FORMAT_ERROR_HTML, 'a mensagem do pl-integer-input, sem alteração');
    assert.equal(htmlToText(INTEGER_FORMAT_ERROR_HTML), INTEGER_FORMAT_ERROR_TEXT, 'autoconferência: o texto esperado segue a regra');
    assert.equal(body.format_errors.v.text, INTEGER_FORMAT_ERROR_TEXT, 'o text pela regra de conversão de html em texto');
    assert.deepEqual(body.params, { parsed: true }, 'params alterado pelo parse do server.py, mesmo com a resposta inválida');
    assert.deepEqual(body.correct_answers, { v: 2 }, 'gabarito alterado pelo parse do server.py, mesmo com a resposta inválida');
    assert.deepEqual(body.submitted_answers, { v: null }, 'resposta interpretada como o PL deixa');
    assert.equal(body.score, null, 'sem nota');
    assert.equal(body.partial_scores, null, 'sem notas parciais');
    assert.deepEqual(body.feedback, {}, 'sem feedback');
    assert.deepEqual(body.version_mismatch, SAME_VERSION, 'sem divergência de versão');
    assert.deepEqual(body.warnings, [], 'sem avisos');
  });
});

test('versões: o snapshot de outra versão é corrigido e a divergência é marcada', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);

    const same = await gradeOk(engine, MULTI_PART, snapshot, MULTI_PART_CORRECT);
    assert.deepEqual(same.version_mismatch, { engine: false, pl: false }, 'sem divergência de versão');
    assert.deepEqual(same, multiPartBody(), 'corpo da correção com o snapshot do próprio motor');

    // A divergência só marca version_mismatch: o resto do corpo é o mesmo da correção sem divergência.
    const oldEngine = await gradeOk(engine, MULTI_PART, { ...snapshot, engine_version: '0.9.0' }, MULTI_PART_CORRECT);
    assert.deepEqual(oldEngine.version_mismatch, { engine: true, pl: false }, 'snapshot de outra versão do motor');
    assert.equal(oldEngine.score, 1, 'a correção é feita mesmo com versão divergente');
    assert.deepEqual(oldEngine, multiPartBody({ version_mismatch: { engine: true, pl: false } }), 'corpo com engine_version 0.9.0');

    const otherPl = 'b'.repeat(40);
    assert.notEqual(otherPl, PL_COMMIT);
    const oldPl = await gradeOk(engine, MULTI_PART, { ...snapshot, pl_commit: otherPl }, MULTI_PART_CORRECT);
    assert.deepEqual(oldPl.version_mismatch, { engine: false, pl: true }, 'snapshot de outra versão do PL');
    assert.equal(oldPl.score, 1, 'a correção é feita mesmo com versão divergente');
    assert.deepEqual(oldPl, multiPartBody({ version_mismatch: { engine: false, pl: true } }), 'corpo com outro pl_commit');

    // As duas divergências ao mesmo tempo: cada uma é comparada à parte.
    const oldBoth = await gradeOk(engine, MULTI_PART, { ...snapshot, engine_version: '0.9.0', pl_commit: otherPl }, MULTI_PART_CORRECT);
    assert.deepEqual(oldBoth, multiPartBody({ version_mismatch: { engine: true, pl: true } }), 'corpo com as duas versões divergentes');

    const newerIr = { ...snapshot, ir_version: '1.1', chave_desconhecida: { futuro: true } };
    const result = await engine.grade(gradeRequest(MULTI_PART, newerIr, MULTI_PART_CORRECT));
    const tolerant = ok200(result);
    assertValid('grade_response', tolerant);
    assert.equal(tolerant.outcome, 'ok', 'snapshot com ir_version 1.1 e chave desconhecida deve ser corrigido');
    // A correção é a mesma: ir_version e chaves desconhecidas não entram em nenhuma regra da correção
    // e a checagem de versão compara só engine_version e pl_commit.
    assert.equal(tolerant.valid, true, 'snapshot tolerante: resposta válida');
    assert.equal(tolerant.score, 1, 'snapshot tolerante: nota 1');
    assert.deepEqual(Object.keys(tolerant.partial_scores).sort(), ['cb', 'int', 'mc', 'num', 'txt'], 'snapshot tolerante: uma nota parcial por campo');
    for (const [name, partial] of Object.entries<any>(tolerant.partial_scores)) {
      assert.equal(partial.score, 1, `snapshot tolerante: nota parcial de ${name}`);
    }
    assert.deepEqual(tolerant.version_mismatch, SAME_VERSION, 'ir_version e chave desconhecida não marcam divergência de versão');
    assert.deepEqual(tolerant, same, 'o corpo com o snapshot tolerante é igual ao do snapshot original (determinismo)');
    assert.deepEqual(tolerant, multiPartBody(), 'corpo da correção com o snapshot tolerante');
  });
});

test('parse-exception quebra na fase parse, e grade-echo quebra na fase grade', async () => {
  await withEngine({}, async (engine) => {
    const parseQid = 'broken/parse-exception';
    const parseBody = await grade(engine, parseQid, await createSnapshot(engine, parseQid), { v: '1' });
    assert.equal(parseBody.outcome, 'broken', `parse-exception deve sair "broken": ${JSON.stringify(parseBody).slice(0, 800)}`);
    assert.equal(parseBody.reason.code, 'question_code_error', 'exceção no parse');
    assert.equal(parseBody.reason.phase, 'parse', 'fase da quebra');
    assertPortuguese(parseBody.reason.message);
    // broken_reason.message é "frase funcional em português, sem detalhe técnico" (engine-api.schema.json),
    // e a mensagem da exceção é detalhe técnico: ela fica só no technical_detail.
    assert.ok(
      !parseBody.reason.message.includes('falha proposital'),
      `a mensagem funcional não pode trazer o texto da exceção: ${parseBody.reason.message}`,
    );
    // a issue do PL começa por <questão>/server.py, com o caminho absoluto da área.
    assertNoServerPath(parseBody.reason.message, 'parse-exception: reason.message');
    // Nem o parse-exception nem o pl-integer-input imprimem: sem avisos.
    assert.deepEqual(parseBody.warnings, [], 'parse-exception: sem avisos');
    assert.deepEqual(parseBody.version_mismatch, SAME_VERSION, 'parse-exception: sem divergência de versão');

    const gradeQid = 'broken/grade-echo';
    const gradeBody = await grade(engine, gradeQid, await createSnapshot(engine, gradeQid), { v: 'qualquer texto' });
    assert.equal(gradeBody.outcome, 'broken', `grade-echo deve sair "broken": ${JSON.stringify(gradeBody).slice(0, 800)}`);
    assert.equal(gradeBody.reason.code, 'question_code_error', 'exceção no grade');
    assert.equal(gradeBody.reason.phase, 'grade', 'fase da quebra');
    assertPortuguese(gradeBody.reason.message);
    // O texto da exceção do grade-echo é str(raw_submitted_answers) = "{'v': 'qualquer texto'}".
    assert.ok(
      !gradeBody.reason.message.includes('qualquer texto'),
      `a mensagem funcional não pode trazer o texto da exceção (a resposta crua): ${gradeBody.reason.message}`,
    );
    assertNoServerPath(gradeBody.reason.message, 'grade-echo: reason.message');
    // O technical_detail traz o traceback. [F] PL a193f72: o outputBoth desta chamada
    // (freeform.js grade() rodado sobre a fixture na imagem fixada) tem menos de mil caracteres, bem
    // abaixo do corte de 8000, e termina com '\u001b[1;91mValueError: \u001b[0m…': o nome da exceção
    // sai inteiro, sem código ANSI no meio. Não se exige aqui o texto ecoado (a resposta crua): o
    // technical_detail fica como o PL o produz, e é o registro que o omite (logging.test.ts).
    assert.equal(typeof gradeBody.reason.technical_detail, 'string', 'grade-echo: technical_detail deve existir');
    assert.ok(
      gradeBody.reason.technical_detail.includes('ValueError'),
      `grade-echo: technical_detail deve conter o traceback da exceção: ${gradeBody.reason.technical_detail.slice(0, 800)}`,
    );
    // A saída do print não vira aviso numa correção quebrada (console_output exige que
    // a operação conclua).
    assert.deepEqual(gradeBody.warnings, [], 'grade-echo: sem avisos numa correção quebrada');
    assert.deepEqual(gradeBody.version_mismatch, SAME_VERSION, 'grade-echo: sem divergência de versão');
  });
});

test('parse-exception: o technical_detail traz a mensagem da exceção e não traz a resposta crua', async () => {
  // O parse do parse-exception lança ValueError("falha proposital no parse") sem ecoar a resposta.
  // Com o marcador, o pl-integer-input grava erro de formato (não fatal), e o server.py roda e lança
  // assim mesmo (freeform.js, processQuestion). O marcador só chegaria ao detalhe pelos dados da
  // chamada (lastCallData), que o technical_detail nunca inclui.
  await withEngine({}, async (engine) => {
    const qid = 'broken/parse-exception';
    const body = await grade(engine, qid, await createSnapshot(engine, qid), { v: RAW_MARKER });
    assert.equal(body.outcome, 'broken', `parse-exception deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.reason.code, 'question_code_error', 'exceção no parse');
    assert.equal(body.reason.phase, 'parse', 'fase da quebra');
    assert.equal(typeof body.reason.technical_detail, 'string', 'technical_detail deve existir');
    assert.ok(
      body.reason.technical_detail.includes('falha proposital no parse'),
      `technical_detail deve conter a mensagem da exceção: ${body.reason.technical_detail.slice(0, 800)}`,
    );
    assert.ok(
      !body.reason.technical_detail.includes(RAW_MARKER),
      `a resposta crua vazou para o technical_detail: ${body.reason.technical_detail.slice(0, 800)}`,
    );
    assert.ok(!body.reason.message.includes(RAW_MARKER), `a resposta crua vazou para a mensagem: ${body.reason.message}`);
    assertReasonMessage(body, ['falha proposital'], 'parse-exception com o marcador');
    assert.deepEqual(body.warnings, [], 'sem avisos');
  });
});

test('grade-echo com saída longa: o technical_detail da quebra na correção sai truncado em 8000 caracteres, sem 500 nem ir_contract_violation', async () => {
  // O grade do grade-echo imprime raw_submitted_answers e lança ValueError(str(raw_submitted_answers)):
  // com 20000 caracteres na resposta, a saída e o traceback passam de 40000 caracteres, acima do
  // maxLength 8000 de broken_reason.technical_detail. O pl-string-input não limita o tamanho no parse
  // (PL a193f72). O corte não fixa qual parte fica.
  const long = 'x'.repeat(20000);
  await withEngine({}, async (engine) => {
    const qid = 'broken/grade-echo';
    const result = await engine.grade(gradeRequest(qid, await createSnapshot(engine, qid), { v: long }));
    assert.notEqual(result.status, 500, 'o technical_detail longo é da questão, não falha interna');
    const body = ok200(result);
    assertValid('grade_response', body);
    assert.equal(body.outcome, 'broken', `grade-echo deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.reason.code, 'question_code_error', 'exceção no grade, e não violação de contrato');
    assert.equal(body.reason.phase, 'grade', 'fase da quebra');
    const detail = body.reason.technical_detail;
    assert.equal(typeof detail, 'string', 'technical_detail deve existir');
    assert.ok(detail.length > 0 && detail.length <= 8000, `technical_detail truncado em 8000 caracteres, veio ${detail.length}`);
    assertPortuguese(body.reason.message);
    assert.ok(!body.reason.message.includes('xxxxxxxxxx'), `a mensagem funcional não pode trazer a saída: ${body.reason.message.slice(0, 800)}`);
    assertNoServerPath(body.reason.message, 'grade-echo com saída longa: reason.message');
    // Numa quebra, a saída do console vai para o technical_detail, e não vira aviso.
    assert.deepEqual(body.warnings, [], 'sem avisos numa correção quebrada');
    assert.deepEqual(body.version_mismatch, SAME_VERSION, 'sem divergência de versão');
  });
});

test('quebra na correção com snapshot de outra versão: a divergência é marcada também no "broken"', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'broken/parse-exception';
    const snapshot = await createSnapshot(engine, qid);

    const oldEngine = await grade(engine, qid, { ...snapshot, engine_version: '0.9.0' }, { v: '1' });
    assert.equal(oldEngine.outcome, 'broken', `esperado broken: ${JSON.stringify(oldEngine).slice(0, 800)}`);
    assert.equal(oldEngine.reason.phase, 'parse', 'fase da quebra');
    assert.deepEqual(oldEngine.version_mismatch, { engine: true, pl: false }, 'snapshot de outra versão do motor');

    const otherPl = 'b'.repeat(40);
    assert.notEqual(otherPl, PL_COMMIT);
    const oldPl = await grade(engine, qid, { ...snapshot, pl_commit: otherPl }, { v: '1' });
    assert.equal(oldPl.outcome, 'broken', `esperado broken: ${JSON.stringify(oldPl).slice(0, 800)}`);
    assert.equal(oldPl.reason.phase, 'parse', 'fase da quebra');
    assert.deepEqual(oldPl.version_mismatch, { engine: false, pl: true }, 'snapshot de outra versão do PL');

    // A divergência de versão não muda a quebra: exceção no parse, mensagem funcional e nenhum aviso
    // (nem o parse-exception nem o pl-integer-input imprimem).
    for (const [what, body] of [['engine_version 0.9.0', oldEngine], ['outro pl_commit', oldPl]] as const) {
      assert.equal(body.reason.code, 'question_code_error', `${what}: exceção no parse`);
      assertPortuguese(body.reason.message);
      assert.ok(
        !body.reason.message.includes('falha proposital'),
        `${what}: a mensagem funcional não pode trazer o texto da exceção: ${body.reason.message}`,
      );
      assertNoServerPath(body.reason.message, `${what}: reason.message`);
      assert.deepEqual(body.warnings, [], `${what}: sem avisos`);
    }
  });
});

test('grade-timeout: tempo esgotado no grade, sem avisos', async () => {
  // O grade do grade-timeout não imprime (while True: pass), e o processo é descartado no tempo esgotado.
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    const qid = 'broken/grade-timeout';
    const body = await grade(engine, qid, await createSnapshot(engine, qid), { v: 'qualquer texto' });
    assert.equal(body.outcome, 'broken', `grade-timeout deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.reason.code, 'timeout', 'tempo esgotado');
    assert.equal(body.reason.phase, 'grade', 'fase da quebra');
    assertPortuguese(body.reason.message);
    assertNoServerPath(body.reason.message, 'grade-timeout: reason.message');
    assert.deepEqual(body.warnings, [], 'sem avisos');
    assert.deepEqual(body.version_mismatch, SAME_VERSION, 'sem divergência de versão');
  });
});

test('grade-parse-timeout: tempo esgotado no parse do server.py é quebra timeout na fase parse, sem avisos e sem a resposta crua no corpo', async () => {
  // Fixture broken/grade-parse-timeout: o generate grava o gabarito, e o parse do server.py não termina
  // (while True: pass) nem imprime. O limite vale para cada chamada do código da questão, e
  // phase é a fase executada. O marcador vai num campo desconhecido, que o PL preserva
  // sem erro de formato: ele só chegaria ao corpo pelos dados da chamada.
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    const qid = 'broken/grade-parse-timeout';
    const body = await grade(engine, qid, await createSnapshot(engine, qid), { v: '1', nota: RAW_MARKER });
    assert.equal(body.outcome, 'broken', `grade-parse-timeout deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.reason.code, 'timeout', 'tempo esgotado');
    assert.equal(body.reason.phase, 'parse', 'fase da quebra');
    assertPortuguese(body.reason.message);
    assertNoServerPath(body.reason.message, 'grade-parse-timeout: reason.message');
    assert.deepEqual(body.warnings, [], 'sem avisos');
    assert.deepEqual(body.version_mismatch, SAME_VERSION, 'sem divergência de versão');
    assert.ok(!JSON.stringify(body).includes(RAW_MARKER), `a resposta crua vazou para o corpo: ${JSON.stringify(body).slice(0, 800)}`);
  });
});

test('determinismo com ENGINE_WORKERS=2: seis correções de cb inválido, em paralelo de 3 em 3, dão o mesmo erro', async () => {
  await withEngine({ ENGINE_WORKERS: '2' }, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const raw = { ...MULTI_PART_CORRECT, cb: ['x', 'y', 'z'] };
    const bodies: any[] = [];
    for (let round = 0; round < 2; round++) {
      bodies.push(...(await Promise.all([0, 1, 2].map(() => gradeOk(engine, MULTI_PART, snapshot, raw)))));
    }
    assert.equal(bodies.length, 6);
    for (const body of bodies) {
      assert.equal(body.valid, false, 'opções inexistentes são erro de formato');
      assert.ok('cb' in body.format_errors, `esperado format_errors.cb: ${JSON.stringify(body.format_errors)}`);
      assert.deepStrictEqual(body.format_errors, bodies[0].format_errors, 'format_errors devem ser idênticos em todas as correções');
      // [F] pl-checkbox.py, linhas 736-740: uma das chaves inválidas, escolhida por set.pop(). Qual delas
      // depende da semente de hash, por isso o regex; x, y e z não têm o que escapar.
      assert.deepEqual(Object.keys(body.format_errors), ['cb'], 'o erro de formato aparece só em cb');
      assert.match(body.format_errors.cb.html, /^You selected an invalid option: [xyz]$/, `format_errors.cb.html: ${body.format_errors.cb.html}`);
      assert.equal(body.format_errors.cb.text, body.format_errors.cb.html, 'sem tags nem entidades, o text é igual ao html');
      // Corpo inteiro: o pl-checkbox não anula a lista inválida, sem notas, e o resto como na toda correta.
      // A mensagem de cb, já presa acima a uma das três chaves, é a desta correção.
      const cbError = body.format_errors.cb.html;
      assert.deepEqual(
        body,
        multiPartInvalidBody({ cb: { html: cbError, text: cbError } }, { ...MULTI_PART_SUBMITTED, cb: ['x', 'y', 'z'] }),
        'corpo da correção de multi-part com cb ["x", "y", "z"]',
      );
    }
  });
});

test('correção sem raw_submitted_answers é pedido inválido (400)', async () => {
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    assertError(
      await engine.grade({ question_ref: ref(TEST_COURSE, MULTI_PART), snapshot }),
      400,
      'invalid_request',
      'correção sem raw_submitted_answers',
    );
  });
});

test('correção com snapshot ou referência fora do contrato dá 400 invalid_request', async () => {
  // snapshot_input aceita só ir_version 1.x (^1\.[0-9]+$) e seed em base 36 minúscula (^[0-9a-z]{1,7}$);
  // question_ref.commit precisa de 40 ou 64 hexadecimais, e a abreviação é recusada.
  // A tolerância do snapshot_input cobre só ir_version 1.x e chaves desconhecidas:
  // pl_commit fora de ^[0-9a-f]{40}$ e engine_version fora de ^[0-9]+\.[0-9]+\.[0-9]+$ são pedido fora
  // do contrato (400), e não "outra versão" com version_mismatch (engine-api.schema.json, snapshot_input).
  await withEngine({}, async (engine) => {
    const snapshot = await createSnapshot(engine, MULTI_PART);
    const refCase = (what: string, questionRef: Record<string, unknown>): [string, Record<string, unknown>] => [
      what,
      { question_ref: questionRef, snapshot, raw_submitted_answers: MULTI_PART_CORRECT },
    ];
    const cases: Array<[string, Record<string, unknown>]> = [
      ['snapshot com ir_version 2.0', gradeRequest(MULTI_PART, { ...snapshot, ir_version: '2.0' }, MULTI_PART_CORRECT)],
      ['snapshot com seed "ABC"', gradeRequest(MULTI_PART, { ...snapshot, seed: 'ABC' }, MULTI_PART_CORRECT)],
      ['snapshot com pl_commit abreviado', gradeRequest(MULTI_PART, { ...snapshot, pl_commit: PL_COMMIT.slice(0, 7) }, MULTI_PART_CORRECT)],
      ['snapshot com engine_version "1.0"', gradeRequest(MULTI_PART, { ...snapshot, engine_version: '1.0' }, MULTI_PART_CORRECT)],
      [
        'commit abreviado',
        {
          question_ref: ref({ repo: TEST_COURSE.repo, commit: TEST_COURSE.commit.slice(0, 7) }, MULTI_PART),
          snapshot,
          raw_submitted_answers: MULTI_PART_CORRECT,
        },
      ],
      // $defs.question_ref: commit ^([0-9a-f]{40}|[0-9a-f]{64})$ (sem nome de branch, tag nem maiúsculas)
      // e qid sem segmento "." ou "..". Os mesmos casos da variante e da inspeção, aqui pela correção; o
      // commit do EXAMPLE em maiúsculas existe na área em minúsculas, então só o esquema o recusa.
      refCase('commit "main"', ref({ repo: TEST_COURSE.repo, commit: 'main' }, MULTI_PART)),
      refCase('commit "v1.0"', ref({ repo: TEST_COURSE.repo, commit: 'v1.0' }, MULTI_PART)),
      refCase('commit em maiúsculas', ref({ repo: EXAMPLE.repo, commit: EXAMPLE.commit.toUpperCase() }, 'element/numberInput')),
      refCase('qid "a/../b"', ref(TEST_COURSE, 'a/../b')),
      refCase('qid "./x"', ref(TEST_COURSE, './x')),
    ];
    for (const [what, request] of cases) {
      assertError(await engine.grade(request), 400, 'invalid_request', `correção com ${what}`);
    }
  });
});

test('correção com repositório, commit ou qid inexistentes, ou questão sem info.json, dá 404 not_found', async () => {
  // Snapshot válido contra snapshot_input: o 404 vem só da referência.
  const snapshot = {
    seed: 'abc',
    params: {},
    correct_answers: { v: 1 },
    preferences: {},
    engine_version: '1.0.0',
    pl_commit: PL_COMMIT,
    ir_version: '1.0',
  };
  const refs: Array<[string, { repo: string; commit: string; qid: string }]> = [
    ['repositório inexistente', ref({ repo: 'https://example.invalid/nao-existe', commit: EXAMPLE.commit }, 'element/numberInput')],
    ['commit inexistente', ref({ repo: EXAMPLE.repo, commit: 'c'.repeat(40) }, 'element/numberInput')],
    ['qid inexistente', ref(EXAMPLE, 'element/naoExiste')],
    ['questão sem info.json', ref(TEST_COURSE, 'incomplete')],
  ];
  await withEngine({}, async (engine) => {
    for (const [what, questionRef] of refs) {
      const result = await engine.grade({ question_ref: questionRef, snapshot, raw_submitted_answers: { v: '1' } });
      assertError(result, 404, 'not_found', `correção com ${what}`);
      assertPortuguese((result.body as any).error.message);
    }
  });
});

test('grade-server-parse: erro de formato gravado pelo parse do server.py deixa a resposta inválida, sem grade', async () => {
  // [F] freeform.js (PL a193f72): processQuestion roda o parse do server.py depois do dos elementos, e
  // parse() marca gradable false quando há format_errors; grading.js grava a submissão sem correção.
  await withEngine({}, async (engine) => {
    const qid = 'supported/grade-server-parse';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: 'proibido' });
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'Recusada no parse', text: 'Recusada no parse' } },
        submitted_answers: { v: 'proibido' },
        partial_scores: null,
        score: null,
        feedback: {},
        params: {},
        correct_answers: { v: 'A' },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção recusada no parse do server.py',
    );
  });
});

test('grade-server-parse: a resposta interpretada pelo parse do server.py é a que volta e a que o grade corrige', async () => {
  // O parse do server.py troca "a" por "A". Sem ignore-case, o pl-string-input só dá nota 1 se o grade
  // comparar o valor interpretado ("A") com o gabarito "A".
  await withEngine({}, async (engine) => {
    const qid = 'supported/grade-server-parse';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: 'a' });
    assert.deepEqual(body.submitted_answers, { v: 'A' }, 'submitted_answers depois do parse do server.py');
    assert.equal(body.score, 1, 'o grade usa a resposta interpretada');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 'A' },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 'A' },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção com a resposta reinterpretada',
    );
  });
});

test('grade-server-feedback: o feedback do grade do server.py volta no resultado válido', async () => {
  // A nota é calculada antes do grade do server.py: 7 contra o gabarito 7 dá 1, e o
  // gabarito 8 gravado depois não muda a nota.
  await withEngine({}, async (engine) => {
    const qid = 'supported/grade-server-feedback';
    const snapshot = await createSnapshot(engine, qid);
    assert.deepEqual(snapshot.correct_answers, { v: 7 }, 'o gabarito da variante vem do generate');
    const body = await gradeOk(engine, qid, snapshot, { v: '7' });
    assert.deepEqual(body.feedback, { v: 'Muito bem' }, 'feedback gravado pelo grade do server.py');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 7 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: { v: 'Muito bem' },
        params: { graded: true },
        correct_answers: { v: 8 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção válida de grade-server-feedback',
    );
  });
});

test('grade-server-feedback: com erro de formato no grade, feedback, params e gabarito alterados são preservados, sem nota', async () => {
  // [F] models/grading-job.js (PL a193f72), updateGradingJobAfterGrading: com gradable false, só score e
  // partial_scores viram null; feedback, params e true_answer são gravados como vieram do grade.
  await withEngine({}, async (engine) => {
    const qid = 'supported/grade-server-feedback';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '13' });
    assert.equal(body.valid, false, 'erro de formato no grade deixa a resposta inválida');
    assert.deepEqual(body.feedback, { v: 'Muito bem' }, 'feedback preservado no resultado inválido');
    assert.deepEqual(body.params, { graded: true }, 'params preservado no resultado inválido');
    assert.deepEqual(body.correct_answers, { v: 8 }, 'gabarito alterado preservado no resultado inválido');
    assert.deepEqual(
      body,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'Resposta proibida', text: 'Resposta proibida' } },
        submitted_answers: { v: 13 },
        partial_scores: null,
        score: null,
        feedback: { v: 'Muito bem' },
        params: { graded: true },
        correct_answers: { v: 8 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção inválida de grade-server-feedback',
    );
  });
});

test('grade-print: saída no console durante o grade vira aviso console_output na fase grade, e a correção conclui', async () => {
  const printed = 'MARCADOR-SAIDA-GRADE-7c3e';
  await withEngine({}, async (engine) => {
    const qid = 'supported/grade-print';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '1' });
    // Só o grade do server.py imprime: exatamente um aviso, na fase grade.
    assert.deepEqual(warningKinds(body), [{ code: 'console_output', phase: 'grade' }], 'avisos da correção');
    assertWarningMessages(body, printed, 'grade-print');
    assert.deepEqual(
      withoutWarnings(body),
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 1 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 1 },
        version_mismatch: SAME_VERSION,
      },
      'a saída no console não altera a correção',
    );
  });
});

test('grade-parse-feedback: o feedback gravado no parse do server.py volta na resposta recusada no parse e chega ao grade na resposta válida', async () => {
  // [F] PL a193f72: o checkData do freeform.js deixa o parse mudar feedback; parse() devolve feedback:
  // resultData.feedback, que lib/grading.js grava na submissão (insertSubmission), e grade() começa com
  // feedback: submission.feedback. Nem o pl-string-input nem o grade do server.py (que só quebra com
  // "QUEBRA") escrevem data["feedback"]. Com "proibido", o grade não roda (primeiro passo da correção).
  await withEngine({}, async (engine) => {
    const qid = 'broken/grade-parse-feedback';
    const snapshot = await createSnapshot(engine, qid);
    assert.deepEqual(snapshot.correct_answers, { v: 'A' }, 'o gabarito da variante vem do generate');

    const refused = await gradeOk(engine, qid, snapshot, { v: 'proibido' });
    assert.deepEqual(refused.feedback, { v: 'Lido no parse' }, 'feedback do parse na resposta recusada no parse');
    assert.deepEqual(
      refused,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'Recusada no parse', text: 'Recusada no parse' } },
        submitted_answers: { v: 'proibido' },
        partial_scores: null,
        score: null,
        feedback: { v: 'Lido no parse' },
        params: {},
        correct_answers: { v: 'A' },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção recusada no parse do server.py, com o feedback gravado no parse',
    );

    // O parse troca "a" por "A": o pl-string-input dá nota 1, e o feedback do parse atravessa o grade.
    const accepted = await gradeOk(engine, qid, snapshot, { v: 'a' });
    assert.deepEqual(accepted.feedback, { v: 'Lido no parse' }, 'feedback do parse no resultado válido');
    assert.deepEqual(
      accepted,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 'A' },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: { v: 'Lido no parse' },
        params: {},
        correct_answers: { v: 'A' },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção válida de grade-parse-feedback',
    );
  });
});

test('grade-rewrite-submitted: a resposta interpretada reescrita no grade do server.py é a que volta, na resposta válida e na inválida', async () => {
  // [F] PL a193f72: o checkData do freeform.js deixa o grade mudar submitted_answers; grade() devolve
  // submitted_answer: resultData.submitted_answers, e models/grading-job.js grava esse valor também sem
  // gradable (updateGradingJobAfterGrading só troca score e partial_scores por null; o SQL grava
  // COALESCE($submitted_answer, submitted_answer)). A nota é calculada antes do grade do server.py
  // : 7 contra o gabarito 7 dá 1, e a reescrita para 70 não a muda.
  await withEngine({}, async (engine) => {
    const qid = 'broken/grade-rewrite-submitted';
    const snapshot = await createSnapshot(engine, qid);
    assert.deepEqual(snapshot.correct_answers, { v: 7 }, 'o gabarito da variante vem do generate');

    const valid = await gradeOk(engine, qid, snapshot, { v: '7' });
    assert.deepEqual(valid.submitted_answers, { v: 70 }, 'submitted_answers como o grade do server.py deixou');
    assert.deepEqual(
      valid,
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 70 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção válida de grade-rewrite-submitted',
    );

    const invalid = await gradeOk(engine, qid, snapshot, { v: '13' });
    assert.deepEqual(invalid.submitted_answers, { v: 130 }, 'submitted_answers reescrito no grade, mesmo com erro de formato');
    assert.deepEqual(
      invalid,
      {
        outcome: 'ok',
        valid: false,
        format_errors: { v: { html: 'Resposta proibida', text: 'Resposta proibida' } },
        submitted_answers: { v: 130 },
        partial_scores: null,
        score: null,
        feedback: {},
        params: {},
        correct_answers: { v: 7 },
        version_mismatch: SAME_VERSION,
        warnings: [],
      },
      'corpo da correção inválida de grade-rewrite-submitted',
    );
  });
});

test('grade-print-parse-and-grade: saída no console no parse e no grade vira um aviso console_output por fase, e a correção conclui', async () => {
  // [F] PL a193f72: processQuestionServer (freeform.js) registra uma issue "output logged on console"
  // em cada chamada do server.py que imprime e conclui; rodando a fixture, o parse e o grade dão uma
  // cada. O precedente deste arquivo é um aviso por fase que imprime (parse-echo e grade-print); a
  // ordem entre os dois avisos não é fixada, por isso a comparação ordenada.
  const printedParse = 'MARCADOR-SAIDA-PARSE-4e2a';
  const printedGrade = 'MARCADOR-SAIDA-GRADE-4e2a';
  await withEngine({}, async (engine) => {
    const qid = 'broken/grade-print-parse-and-grade';
    const snapshot = await createSnapshot(engine, qid);
    const body = await gradeOk(engine, qid, snapshot, { v: '1' });
    assert.equal(body.warnings.length, 2, `um aviso por fase que imprimiu: ${JSON.stringify(body.warnings)}`);
    assert.deepEqual(
      body.warnings.map((w: any) => `${w.code}/${w.phase}`).sort(),
      ['console_output/grade', 'console_output/parse'],
      'avisos da correção com saída no parse e no grade',
    );
    assertWarningMessages(body, printedParse, 'grade-print-parse-and-grade');
    assertWarningMessages(body, printedGrade, 'grade-print-parse-and-grade');
    assert.deepEqual(
      withoutWarnings(body),
      {
        outcome: 'ok',
        valid: true,
        format_errors: {},
        submitted_answers: { v: 1 },
        partial_scores: { v: { score: 1, weight: 1 } },
        score: 1,
        feedback: {},
        params: {},
        correct_answers: { v: 1 },
        version_mismatch: SAME_VERSION,
      },
      'a saída no console não altera a correção',
    );
  });
});

test('grade-print-parse-and-grade com 13: o parse imprime e conclui, o grade imprime e quebra, e a correção quebrada não leva avisos', async () => {
  // console_output exige que a operação conclua: a saída do parse, que concluiu, não
  // vira aviso quando o grade quebra. [F] PL a193f72: rodando a fixture com 13, o outputBoth do grade
  // (menos de mil caracteres, bem abaixo do corte de 8000) começa com a linha impressa e termina com
  // '\u001b[1;91mValueError: \u001b[0mfalha proposital no grade'.
  await withEngine({}, async (engine) => {
    const qid = 'broken/grade-print-parse-and-grade';
    const body = await grade(engine, qid, await createSnapshot(engine, qid), { v: '13' });
    assert.equal(body.outcome, 'broken', `${qid} com 13 deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assert.equal(body.reason.code, 'question_code_error', 'exceção no grade do server.py');
    assert.equal(body.reason.phase, 'grade', 'fase da quebra');
    assertReasonMessage(body, ['falha proposital', 'MARCADOR-SAIDA-PARSE-4e2a', 'MARCADOR-SAIDA-GRADE-4e2a'], `${qid} com 13`);
    assert.deepEqual(body.warnings, [], 'sem avisos numa correção quebrada, nem os da fase parse, que concluiu');
    assert.deepEqual(body.version_mismatch, SAME_VERSION, 'sem divergência de versão');
    // O technical_detail traz a mensagem da exceção e a saída do Python da chamada que quebrou.
    assert.equal(typeof body.reason.technical_detail, 'string', 'technical_detail deve existir');
    for (const fragment of ['falha proposital no grade', 'MARCADOR-SAIDA-GRADE-4e2a']) {
      assert.ok(
        body.reason.technical_detail.includes(fragment),
        `technical_detail deve conter "${fragment}": ${body.reason.technical_detail.slice(0, 800)}`,
      );
    }
  });
});

test('grade-parse-feedback com "quebra" e grade-rewrite-submitted com 99: exceção no grade do server.py é quebra na fase grade', async () => {
  // As duas fixtures ficam em broken/ por este caminho: o grade do server.py lança
  // ValueError("falha proposital no grade") (com "quebra", o parse já trocou a resposta por "QUEBRA").
  // Nenhuma das duas imprime. [F] PL a193f72: o outputBoth de cada uma termina com
  // '\u001b[1;91mValueError: \u001b[0mfalha proposital no grade', sem ANSI no meio da mensagem.
  await withEngine({}, async (engine) => {
    for (const [qid, raw] of [
      ['broken/grade-parse-feedback', { v: 'quebra' }],
      ['broken/grade-rewrite-submitted', { v: '99' }],
    ] as const) {
      const body = await grade(engine, qid, await createSnapshot(engine, qid), raw);
      assert.equal(body.outcome, 'broken', `${qid} deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
      assert.equal(body.reason.code, 'question_code_error', `${qid}: exceção no grade do server.py`);
      assert.equal(body.reason.phase, 'grade', `${qid}: fase da quebra`);
      assertReasonMessage(body, ['falha proposital'], qid);
      assert.equal(typeof body.reason.technical_detail, 'string', `${qid}: technical_detail deve existir`);
      assert.ok(
        body.reason.technical_detail.includes('falha proposital no grade'),
        `${qid}: technical_detail deve conter a mensagem da exceção: ${body.reason.technical_detail.slice(0, 800)}`,
      );
      assert.deepEqual(body.warnings, [], `${qid}: sem avisos`);
      assert.deepEqual(body.version_mismatch, SAME_VERSION, `${qid}: sem divergência de versão`);
    }
  });
});

test('alteração proibida de dados no parse ou no grade é questão quebrada por invalid_data', async () => {
  // [F] checkData do freeform.js (PL a193f72): raw_submitted_answers só muda na fase test, e uma chave
  // extra em data dá '"data" has invalid extra keys: <chaves>'. processQuestionServer roda o checkData
  // depois de cada chamada do server.py e trata a recusa como erro fatal.
  await withEngine({}, async (engine) => {
    const parseQid = 'broken/grade-parse-illegal-data';
    const parseBody = await grade(engine, parseQid, await createSnapshot(engine, parseQid), { v: '1' });
    assert.equal(parseBody.outcome, 'broken', `${parseQid} deve sair "broken": ${JSON.stringify(parseBody).slice(0, 800)}`);
    assert.equal(parseBody.reason.code, 'invalid_data', 'o parse alterou raw_submitted_answers');
    assert.equal(parseBody.reason.phase, 'parse', 'fase da quebra');
    assertPortuguese(parseBody.reason.message);
    // A recusa do checkData é detalhe técnico: fica no technical_detail, não na mensagem funcional.
    assert.ok(
      !parseBody.reason.message.includes('illegally modified'),
      `${parseQid}: a mensagem funcional não pode trazer a recusa do checkData: ${parseBody.reason.message}`,
    );
    // [F] freeform.js: '<questão>/server.py: Invalid state after parse(): …', com o caminho absoluto.
    assertNoServerPath(parseBody.reason.message, `${parseQid}: reason.message`);
    assert.deepEqual(parseBody.version_mismatch, SAME_VERSION, `${parseQid}: sem divergência de versão`);
    assert.deepEqual(parseBody.warnings, [], `${parseQid}: sem avisos`);

    const gradeQid = 'broken/grade-illegal-data';
    const gradeBody = await grade(engine, gradeQid, await createSnapshot(engine, gradeQid), { v: '1' });
    assert.equal(gradeBody.outcome, 'broken', `${gradeQid} deve sair "broken": ${JSON.stringify(gradeBody).slice(0, 800)}`);
    assert.equal(gradeBody.reason.code, 'invalid_data', 'o grade acrescentou a chave foo em data');
    assert.equal(gradeBody.reason.phase, 'grade', 'fase da quebra');
    assertPortuguese(gradeBody.reason.message);
    assert.ok(
      !gradeBody.reason.message.includes('invalid extra keys'),
      `${gradeQid}: a mensagem funcional não pode trazer a recusa do checkData: ${gradeBody.reason.message}`,
    );
    assertNoServerPath(gradeBody.reason.message, `${gradeQid}: reason.message`);
    // O technical_detail traz a mensagem do checkData, literal.
    assert.equal(typeof gradeBody.reason.technical_detail, 'string', 'technical_detail deve existir');
    assert.ok(
      gradeBody.reason.technical_detail.includes('"data" has invalid extra keys: foo'),
      `technical_detail deve conter a recusa do checkData: ${gradeBody.reason.technical_detail.slice(0, 800)}`,
    );
    assert.deepEqual(gradeBody.version_mismatch, SAME_VERSION, `${gradeQid}: sem divergência de versão`);
    assert.deepEqual(gradeBody.warnings, [], `${gradeQid}: sem avisos`);
  });
});

test('raw_submitted_answers alterado no parse ou no grade: o technical_detail traz a recusa do checkData sem os valores novo e original', async () => {
  // [F] checkData do freeform.js (PL a193f72): 'data.raw_submitted_answers has been illegally modified,
  // new value: "…", original value: "…"'. O valor original é a resposta crua (aqui, o marcador), e o
  // novo é o que o server.py gravou (NOVO-VALOR-5d2a). Nas fases parse e grade, a mensagem sai sem os
  // dois valores. No parse, o marcador é erro de formato do pl-integer-input
  // (não fatal), e o parse do server.py roda assim mesmo; no grade, o pl-string-input aceita o marcador.
  const newValue = 'NOVO-VALOR-5d2a';
  await withEngine({}, async (engine) => {
    for (const [qid, phase] of [
      ['broken/grade-parse-illegal-data', 'parse'],
      ['broken/grade-illegal-raw-in-grade', 'grade'],
    ] as const) {
      const body = await grade(engine, qid, await createSnapshot(engine, qid), { v: RAW_MARKER });
      assert.equal(body.outcome, 'broken', `${qid} deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
      assert.equal(body.reason.code, 'invalid_data', `${qid}: o server.py alterou raw_submitted_answers`);
      assert.equal(body.reason.phase, phase, `${qid}: fase da quebra`);
      assertPortuguese(body.reason.message);
      assert.equal(typeof body.reason.technical_detail, 'string', `${qid}: technical_detail deve existir`);
      const detail: string = body.reason.technical_detail;
      assert.ok(
        detail.includes('data.raw_submitted_answers has been illegally modified'),
        `${qid}: technical_detail deve conter a recusa do checkData: ${detail.slice(0, 800)}`,
      );
      assert.ok(!detail.includes(RAW_MARKER), `${qid}: a resposta crua (valor original) vazou para o technical_detail: ${detail.slice(0, 800)}`);
      assert.ok(!detail.includes(newValue), `${qid}: o valor novo vazou para o technical_detail: ${detail.slice(0, 800)}`);
      for (const leaked of [RAW_MARKER, newValue, 'illegally modified']) {
        assert.ok(!body.reason.message.includes(leaked), `${qid}: a mensagem funcional traz "${leaked}": ${body.reason.message}`);
      }
      assertNoServerPath(body.reason.message, `${qid}: reason.message`);
      assert.deepEqual(body.version_mismatch, SAME_VERSION, `${qid}: sem divergência de versão`);
      assert.deepEqual(body.warnings, [], `${qid}: sem avisos`);
    }
  });
});

test('erro de formato que não é texto: a resposta violaria o contrato e vira "broken"/ir_contract_violation na última fase executada, sem 500', async () => {
  // [F] O checkData do PL só exige que format_errors seja objeto, e o PL aceita {"x": 1} como erro de
  // formato. O esquema do motor exige format_errors[k].html em texto, a mensagem do PL sem alteração,
  // e um objeto não vira texto sem inventar conteúdo. Com erro de formato no parse,
  // o grade não roda, e a última fase executada é o parse.
  await withEngine({}, async (engine) => {
    for (const [qid, phase] of [
      ['broken/grade-parse-contract-violation', 'parse'],
      ['broken/grade-contract-violation', 'grade'],
    ] as const) {
      const snapshot = await createSnapshot(engine, qid);
      const result = await engine.grade(gradeRequest(qid, snapshot, { v: '1' }));
      assert.notEqual(result.status, 500, `${qid}: violação de contrato causada pela questão não é falha interna`);
      const body = ok200(result);
      assertValid('grade_response', body);
      assert.equal(body.outcome, 'broken', `${qid} deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
      assert.equal(body.reason.code, 'ir_contract_violation', `${qid}: reason.code`);
      assert.equal(body.reason.phase, phase, `${qid}: a última fase executada`);
      assertPortuguese(body.reason.message);
      // O erro do validador do contrato (Ajv 2020 do node_modules do PL, por exemplo
      // "/format_errors/v must be string" ou "must have required property 'html'") é detalhe técnico e
      // fica fora do motivo funcional (broken_reason.message: "Frase funcional em português, sem
      // detalhe técnico"). O mesmo critério do negative-weight na variante (create-variant.test.ts); o
      // caminho /format_errors/v vem do server.py das duas fixtures (format_errors["v"] = {"x": 1}).
      assert.doesNotMatch(
        body.reason.message,
        /must be|should be|must have|must NOT|\/format_errors\b|format_errors\//i,
        `${qid}: o detalhe da violação do contrato fica fora do motivo funcional`,
      );
      assertNoServerPath(body.reason.message, `${qid}: reason.message`);
      assert.deepEqual(body.version_mismatch, SAME_VERSION, `${qid}: sem divergência de versão`);
      assert.deepEqual(body.warnings, [], `${qid}: sem avisos`);
    }
  });
});

test('violação de contrato na correção: nem a mensagem nem o technical_detail trazem a resposta crua', async () => {
  // Na violação de contrato, quem monta o diagnóstico é o motor, a partir dos dados devolvidos pelo
  // server.py, que levam raw_submitted_answers. O technical_detail nunca inclui os dados da chamada,
  // e nenhum dos dois server.py imprime ou ecoa a resposta: o corpo "broken" não tem
  // campo que carregue o marcador legitimamente.
  // - parse: o marcador em v é erro de formato do pl-integer-input (não fatal), e o parse do server.py
  //   o sobrescreve com {"x": 1};
  // - grade: em v o marcador daria erro de formato e o grade não rodaria, então ele vai num campo
  //   desconhecido, que o PL preserva em submitted_answers.
  await withEngine({}, async (engine) => {
    for (const [qid, phase, raw] of [
      ['broken/grade-parse-contract-violation', 'parse', { v: RAW_MARKER }],
      ['broken/grade-contract-violation', 'grade', { v: '1', nota: RAW_MARKER }],
    ] as const) {
      const result = await engine.grade(gradeRequest(qid, await createSnapshot(engine, qid), raw));
      assert.notEqual(result.status, 500, `${qid}: violação de contrato causada pela questão não é falha interna`);
      const body = ok200(result);
      assertValid('grade_response', body);
      assert.equal(body.outcome, 'broken', `${qid} deve sair "broken": ${JSON.stringify(body).slice(0, 800)}`);
      assert.equal(body.reason.code, 'ir_contract_violation', `${qid}: reason.code`);
      assert.equal(body.reason.phase, phase, `${qid}: a última fase executada`);
      assertPortuguese(body.reason.message);
      assert.doesNotMatch(
        body.reason.message,
        /must be|should be|must have|must NOT|\/format_errors\b|format_errors\//i,
        `${qid}: o detalhe da violação do contrato fica fora do motivo funcional`,
      );
      assertNoServerPath(body.reason.message, `${qid}: reason.message`);
      assert.deepEqual(body.version_mismatch, SAME_VERSION, `${qid}: sem divergência de versão`);
      assert.deepEqual(body.warnings, [], `${qid}: sem avisos`);
      assert.ok(!JSON.stringify(body).includes(RAW_MARKER), `${qid}: a resposta crua vazou para o corpo: ${JSON.stringify(body).slice(0, 800)}`);
    }
  });
});
