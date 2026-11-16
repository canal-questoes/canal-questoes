// AUTOTESTE dos contratos publicados (contracts/engine/v1/).
// Cobre: a IR e as respostas das operações validadas contra os esquemas publicados,
// a IR válida contra o esquema, o snapshot_input tolerante na correção (com saída estrita),
// os campos e as mídias da IR, o outcome broken/unsupported e a forma do snapshot e da IR.
// É verde já no esqueleto: não depende da implementação (testa o contrato, não o motor).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { describe, it } from 'node:test';

import { API_DEFS, CONTRACTS_DIR, assertValid, isValid, newAjv } from '../helpers/schemas.ts';

const irSchema = JSON.parse(fs.readFileSync(path.join(CONTRACTS_DIR, 'ir.schema.json'), 'utf8'));
const apiSchema = JSON.parse(fs.readFileSync(path.join(CONTRACTS_DIR, 'engine-api.schema.json'), 'utf8'));

const PL_COMMIT = 'a193f72048530a4499252c454acca963cdf98922';
const HASH_A = 'sha256:' + 'a'.repeat(64);
const HASH_B = 'sha256:' + 'b'.repeat(64);
const HASH_C = 'sha256:' + 'c'.repeat(64);
// PNG 1×1 (bytes quaisquer servem: o esquema só exige texto base64).
const PNG_B64 = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

function clone<T>(value: T): T {
  return structuredClone(value);
}

function questionRef(): any {
  return { repo: 'https://github.com/PrairieLearn/PrairieLearn', commit: PL_COMMIT, qid: 'element/numberInput' };
}

/** IR mínima com um campo de cada tipo, sem mídias nem explicação. */
function minimalIr(): any {
  return {
    ir_version: '1.0',
    statement: {
      html:
        '<p>Seja <cq-math display="inline">x = 3</cq-math>.</p>' +
        '<cq-field name="num"></cq-field><cq-field name="int"></cq-field><cq-field name="txt"></cq-field>' +
        '<cq-field name="mc"></cq-field><cq-field name="cb"></cq-field>',
    },
    fields: [
      {
        name: 'num',
        type: 'number',
        label_html: '<cq-math display="inline">y =</cq-math>',
        suffix_html: 'm/s',
        format: { kind: 'number', comparison: 'relabs', rtol: 0.01, atol: 1e-8, allow_fractions: true, allow_complex: false },
        hint: 'número decimal com ponto; ex.: 3.14',
        constraints: { allow_blank: false },
        weight: 1,
        options: null,
      },
      {
        name: 'int',
        type: 'integer',
        label_html: null,
        suffix_html: null,
        format: { kind: 'integer', base: 16 },
        hint: 'número inteiro na base 16',
        constraints: { allow_blank: false },
        weight: 1,
        options: null,
      },
      {
        name: 'txt',
        type: 'text',
        label_html: null,
        suffix_html: null,
        format: {
          kind: 'text',
          ignore_case: true,
          remove_spaces: false,
          remove_leading_trailing: false,
          normalize_to_ascii: false,
          multiline: false,
          regex: false,
        },
        hint: 'texto',
        constraints: { allow_blank: false },
        weight: 1,
        options: null,
      },
      {
        name: 'mc',
        type: 'single_choice',
        label_html: null,
        suffix_html: null,
        format: { kind: 'single_choice' },
        hint: 'escolha uma alternativa',
        constraints: { allow_blank: false },
        weight: 1,
        options: [
          { key: 'a', html: '<p>Primeira</p>', content_hash: HASH_A },
          { key: 'b', html: '<p>Segunda</p>', content_hash: HASH_B },
          { key: 'c', html: '<p>Terceira</p>', content_hash: HASH_C },
        ],
      },
      {
        name: 'cb',
        type: 'multiple_choice',
        label_html: null,
        suffix_html: null,
        format: { kind: 'multiple_choice', partial_credit: 'net-correct' },
        hint: 'marque de 1 a 3 alternativas',
        constraints: { allow_blank: false, min_select: 1, max_select: 3 },
        weight: 1,
        options: [
          { key: 'a', html: '<p>A</p>', content_hash: HASH_A },
          { key: 'b', html: '<p>B</p>', content_hash: HASH_B },
          { key: 'c', html: '<p>C</p>', content_hash: HASH_C },
        ],
      },
    ],
    answer_key: [
      { name: 'num', can_show: true, correct_value: 6.5, display_html: '6.5' },
      { name: 'int', can_show: true, correct_value: '1F', display_html: '1F' },
      { name: 'txt', can_show: true, correct_value: 'Brasil', display_html: 'Brasil' },
      { name: 'mc', can_show: true, correct_value: { key: 'b', html: 'Segunda' }, display_html: '<p>Segunda</p>' },
      { name: 'cb', can_show: false, correct_value: null, display_html: null },
    ],
    explanation: null,
    media: [],
  };
}

/** IR com explicação e com uma mídia "ok" e outra "missing". */
function irWithMedia(): any {
  const ir = minimalIr();
  ir.statement.html = '<p><img src="cq-media:m1" alt="Figura"></p>' + ir.statement.html;
  ir.explanation = { html: '<p>Explicação: <cq-math display="inline">a=1</cq-math></p>', can_show: true };
  ir.media = [
    {
      id: 'm1',
      origin: 'static',
      filename: 'clientFilesQuestion/figura.png',
      content_type: 'image/png',
      alt: 'Figura',
      status: 'ok',
      data_base64: PNG_B64,
      size_bytes: 68,
    },
    {
      id: 'm2',
      origin: 'dynamic',
      filename: 'falha.png',
      content_type: 'image/png',
      alt: null,
      status: 'missing',
    },
  ];
  return ir;
}

function snapshot(): any {
  return {
    seed: 'abc',
    params: { a: 9, b: 6 },
    correct_answers: { c_1: 10.816653826391969 },
    preferences: {},
    engine_version: '1.0.0',
    pl_commit: PL_COMMIT,
    ir_version: '1.0',
  };
}

function variantOk(): any {
  return {
    outcome: 'ok',
    snapshot: snapshot(),
    ir: minimalIr(),
    warnings: [{ code: 'console_output', message: 'O código da questão imprimiu no console.', phase: 'generate' }],
  };
}

function variantBroken(): any {
  return {
    outcome: 'broken',
    reason: {
      code: 'question_code_error',
      message: 'O código da questão falhou na preparação da variante.',
      phase: 'generate',
      technical_detail: 'Traceback (most recent call last): ValueError: falha proposital',
    },
    warnings: [],
  };
}

function variantUnsupported(): any {
  return {
    outcome: 'unsupported',
    reasons: [{ code: 'unsupported_elements', message: 'A questão usa elementos não suportados.', elements: ['pl-order-blocks'] }],
  };
}

function gradeValid(): any {
  return {
    outcome: 'ok',
    valid: true,
    format_errors: {},
    submitted_answers: { v: 7 },
    partial_scores: { v: { score: 1, weight: 1 } },
    score: 1,
    feedback: {},
    params: {},
    correct_answers: { v: 7 },
    version_mismatch: { engine: false, pl: false },
    warnings: [],
  };
}

function gradeInvalid(): any {
  return {
    outcome: 'ok',
    valid: false,
    format_errors: { num: { html: 'Invalid format: <code>3,14</code>', text: 'Invalid format: 3,14' } },
    submitted_answers: { num: null },
    partial_scores: null,
    score: null,
    feedback: {},
    params: {},
    correct_answers: { num: 3.14 },
    version_mismatch: { engine: false, pl: false },
    warnings: [],
  };
}

function gradeBroken(): any {
  return {
    outcome: 'broken',
    reason: { code: 'timeout', message: 'O código da questão excedeu o tempo limite na correção.', phase: 'grade' },
    version_mismatch: { engine: true, pl: false },
    warnings: [],
  };
}

function inspectSupported(): any {
  return {
    outcome: 'supported',
    question: {
      uuid: '4f1d2e3c-0000-4000-8000-000000000001',
      title: 'Exemplo',
      topic: 'Elementos',
      tags: [],
      type: 'v3',
      grading_method: 'Internal',
      elements: ['pl-card', 'pl-number-input', 'pl-question-panel'],
      show_correct_answer: true,
      single_variant: false,
      partial_credit: true,
      answers_names: ['c_1'],
      content_hash: HASH_A,
    },
    reasons: [],
  };
}

describe('contratos compilam no Ajv2020 estrito', () => {
  it('os dois esquemas e todos os $defs compilam', () => {
    const ajv = newAjv();
    assert.ok(ajv.getSchema(irSchema.$id), 'ir.schema.json não compilou');
    assert.ok(ajv.getSchema(apiSchema.$id), 'engine-api.schema.json não compilou');
    assert.ok(API_DEFS.length > 0, 'engine-api.schema.json sem $defs');
    for (const name of API_DEFS) {
      assert.ok(ajv.getSchema(`${apiSchema.$id}#/$defs/${name}`), `$defs/${name} não compilou`);
    }
    for (const name of Object.keys(irSchema.$defs)) {
      assert.ok(ajv.getSchema(`${irSchema.$id}#/$defs/${name}`), `ir $defs/${name} não compilou`);
    }
  });

  it('o Ajv do helper está de fato no modo estrito', () => {
    const ajv = newAjv();
    assert.throws(
      () => ajv.compile({ type: 'object', palavraChaveDesconhecida: true }),
      /strict mode/,
      'o modo estrito deveria recusar palavra-chave desconhecida',
    );
  });

  it('os esquemas declaram o dialeto 2020-12', () => {
    assert.equal(irSchema.$schema, 'https://json-schema.org/draft/2020-12/schema');
    assert.equal(apiSchema.$schema, 'https://json-schema.org/draft/2020-12/schema');
  });
});

describe('documentos válidos passam', () => {
  it('IR mínima com um campo de cada tipo', () => {
    const ir = minimalIr();
    assert.deepEqual(
      ir.fields.map((f: any) => f.type),
      ['number', 'integer', 'text', 'single_choice', 'multiple_choice'],
    );
    assertValid('ir', ir);
  });

  it('IR com explicação e mídias ok e missing', () => {
    assertValid('ir', irWithMedia());
  });

  it('hint com exatamente 64 caracteres', () => {
    const ir = minimalIr();
    ir.fields[0].hint = 'a'.repeat(64);
    assertValid('ir', ir);
  });

  it('snapshot', () => {
    assertValid('snapshot', snapshot());
  });

  it('variante ok, broken e unsupported', () => {
    assertValid('create_variant_response', variantOk());
    assertValid('create_variant_response', variantBroken());
    assertValid('create_variant_response', variantUnsupported());
  });

  it('correção ok com valid true, com valid false, e correção broken', () => {
    assertValid('grade_response', gradeValid());
    assertValid('grade_response', gradeInvalid());
    assertValid('grade_response', gradeBroken());
  });

  it('grade_request com snapshot.ir_version "1.1" e uma chave desconhecida no snapshot', () => {
    const req = {
      question_ref: questionRef(),
      snapshot: { ...snapshot(), ir_version: '1.1', chave_de_motor_novo: { x: 1 } },
      raw_submitted_answers: { c_1: '10.8' },
    };
    assertValid('grade_request', req);
  });

  it('pedidos, saúde, inspeção e erro (bases dos casos inválidos)', () => {
    assertValid('question_ref', questionRef());
    assertValid('inspect_request', { question_ref: questionRef() });
    assertValid('create_variant_request', { question_ref: questionRef(), seed: 'abc' });
    assertValid('create_variant_request', { question_ref: questionRef(), seed: '1z141z3', preferences: { n: 5, cor: 'verde' } });
    assertValid('inspect_response', inspectSupported());
    assertValid('inspect_response', {
      ...inspectSupported(),
      outcome: 'unsupported',
      reasons: [{ code: 'no_answer_fields', message: 'A questão não tem campo de resposta suportado.' }],
    });
    assertValid('health_response', {
      status: 'ready',
      engine_version: '1.0.0',
      contract_version: '1.0',
      ir_version: '1.0',
      pl_commit: PL_COMMIT,
      workers: 2,
    });
    assertValid('error_response', { error: { code: 'invalid_request', message: 'Pedido inválido.', details: ['/seed deve casar o padrão'] } });
  });
});

interface InvalidCase {
  title: string;
  def: string;
  base: () => any;
  /** Recebe uma cópia da base e devolve o documento inválido. */
  mutate: (doc: any) => any;
}

const INVALID_CASES: InvalidCase[] = [
  {
    title: 'hint com 65 caracteres',
    def: 'ir',
    base: minimalIr,
    mutate: (ir) => {
      ir.fields[0].hint = 'a'.repeat(65);
      return ir;
    },
  },
  {
    title: 'commit com 7 caracteres',
    def: 'question_ref',
    base: questionRef,
    mutate: (r) => {
      r.commit = 'a193f72';
      return r;
    },
  },
  {
    title: 'commit com 7 caracteres (dentro de inspect_request)',
    def: 'inspect_request',
    base: () => ({ question_ref: questionRef() }),
    mutate: (r) => {
      r.question_ref.commit = 'a193f72';
      return r;
    },
  },
  {
    title: 'qid com ".."',
    def: 'question_ref',
    base: questionRef,
    mutate: (r) => {
      r.qid = 'element/../numberInput';
      return r;
    },
  },
  {
    title: 'qid igual a ".."',
    def: 'question_ref',
    base: questionRef,
    mutate: (r) => {
      r.qid = '..';
      return r;
    },
  },
  {
    title: 'seed com maiúscula',
    def: 'seed',
    base: () => 'abc',
    mutate: () => 'ABC',
  },
  {
    title: 'seed com maiúscula (dentro de create_variant_request)',
    def: 'create_variant_request',
    base: () => ({ question_ref: questionRef(), seed: 'abc' }),
    mutate: (r) => {
      r.seed = 'ABC';
      return r;
    },
  },
  {
    title: 'mídia ok sem data_base64',
    def: 'ir',
    base: irWithMedia,
    mutate: (ir) => {
      delete ir.media[0].data_base64;
      return ir;
    },
  },
  {
    title: 'mídia missing com data_base64',
    def: 'ir',
    base: irWithMedia,
    mutate: (ir) => {
      ir.media[1].data_base64 = PNG_B64;
      return ir;
    },
  },
  {
    title: 'valid: false com score numérico',
    def: 'grade_response',
    base: gradeInvalid,
    mutate: (g) => {
      g.score = 0.5;
      return g;
    },
  },
  {
    title: 'valid: true com format_errors não vazio',
    def: 'grade_response',
    base: gradeValid,
    mutate: (g) => {
      g.format_errors = { v: { html: 'Invalid format', text: 'Invalid format' } };
      return g;
    },
  },
  {
    title: 'valid: true com partial_scores: null',
    def: 'grade_response',
    base: gradeValid,
    mutate: (g) => {
      g.partial_scores = null;
      return g;
    },
  },
  {
    title: 'inspect_response supported com reasons não vazio',
    def: 'inspect_response',
    base: inspectSupported,
    mutate: (r) => {
      r.reasons = [{ code: 'workspace', message: 'A questão usa área de trabalho.' }];
      return r;
    },
  },
  {
    title: 'options em campo number',
    def: 'ir',
    base: minimalIr,
    mutate: (ir) => {
      ir.fields[0].options = [{ key: 'a', html: '<p>A</p>', content_hash: HASH_A }];
      return ir;
    },
  },
  {
    title: 'multiple_choice sem min_select',
    def: 'ir',
    base: minimalIr,
    mutate: (ir) => {
      delete ir.fields[4].constraints.min_select;
      return ir;
    },
  },
  {
    title: 'min_select em campo text',
    def: 'ir',
    base: minimalIr,
    mutate: (ir) => {
      ir.fields[2].constraints.min_select = 1;
      return ir;
    },
  },
  {
    title: 'campo type "number" com format.kind "integer"',
    def: 'ir',
    base: minimalIr,
    mutate: (ir) => {
      ir.fields[0].format = { kind: 'integer', base: 10 };
      return ir;
    },
  },
  {
    title: 'create_variant com snapshot.ir_version "1.1" (a saída é estrita)',
    def: 'create_variant_response',
    base: variantOk,
    mutate: (v) => {
      v.snapshot.ir_version = '1.1';
      return v;
    },
  },
];

describe('documentos inválidos falham', () => {
  for (const c of INVALID_CASES) {
    it(c.title, () => {
      const base = c.base();
      // A base precisa ser válida, para que só a mutação explique a recusa.
      assertValid(c.def, base);
      const doc = c.mutate(clone(base));
      assert.equal(isValid(c.def, doc), false, `o esquema "${c.def}" deveria recusar: ${c.title}`);
    });
  }
});
