// checkData: cópia da tabela do freeform.js do PL (a193f72).
// Cobre a fidelidade ao PL (nenhuma divergência), a alteração proibida de dados (→
// questão quebrada, via invalid_data) e o detalhe técnico. As mensagens esperadas são as LITERAIS de
// /PrairieLearn/apps/prairielearn/dist/question-servers/freeform.js (função checkData, linhas 275–359),
// conferidas no container pl-oracle.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { checkData, type Phase } from '../../src/pl/check-data.ts';

type Data = Record<string, unknown>;

/** Chaves presentes em todas as fases (params, correct_answers, variant_seed, options, preferences). */
function baseData(): Data {
  return {
    params: { x: 3, lista: [1, 2] },
    correct_answers: { v: 6.5 },
    variant_seed: 13368,
    options: { client_files_question_url: '/cq/clientFilesQuestion' },
    preferences: {},
  };
}

/** Dados com exatamente as chaves que o PL exige em cada fase (presentPhases da tabela). */
function dataFor(phase: 'generate' | 'prepare' | 'parse' | 'grade' | 'test'): Data {
  const d = baseData();
  switch (phase) {
    case 'generate':
      return d;
    case 'prepare':
      return { ...d, answers_names: {} };
    case 'parse':
      return {
        ...d,
        submitted_answers: { v: '1' },
        format_errors: {},
        raw_submitted_answers: { v: '1' },
        feedback: {},
        gradable: true,
      };
    case 'grade':
      return {
        ...d,
        submitted_answers: { v: '1' },
        format_errors: {},
        raw_submitted_answers: { v: '1' },
        partial_scores: {},
        score: 0,
        feedback: {},
        gradable: true,
      };
    case 'test':
      return {
        ...d,
        format_errors: {},
        raw_submitted_answers: { v: '1' },
        partial_scores: {},
        score: 0,
        feedback: {},
        gradable: true,
        test_type: 'correct',
      };
  }
}

/** Mensagem literal do PL para alteração proibida. */
function illegal(prop: string, newValue: unknown, origValue: unknown): string {
  return `data.${prop} has been illegally modified, new value: "${JSON.stringify(newValue)}", original value: "${JSON.stringify(origValue)}"`;
}

/** Controle: os dados montados pelo teste são aceitos sem mudança na fase. */
function assertBaselineAccepted(phase: 'generate' | 'prepare' | 'parse' | 'grade' | 'test'): void {
  const orig = dataFor(phase);
  assert.equal(checkData(structuredClone(orig), orig, phase), null, `dados de controle recusados na fase ${phase}`);
}

describe('checkData na fase generate', () => {
  test('aceita mudar params e correct_answers', () => {
    assertBaselineAccepted('generate');
    const orig = dataFor('generate');
    const data = { ...structuredClone(orig), params: { x: 4, novo: 'a' }, correct_answers: { v: 7, w: 'b' } };
    assert.equal(checkData(data, orig, 'generate'), null);
  });

  test('acusa variant_seed alterado', () => {
    const orig = dataFor('generate');
    const data = { ...structuredClone(orig), variant_seed: 13369 };
    assert.equal(checkData(data, orig, 'generate'), illegal('variant_seed', 13369, 13368));
    assert.equal(
      checkData(data, orig, 'generate'),
      'data.variant_seed has been illegally modified, new value: "13369", original value: "13368"',
    );
  });

  test('acusa chave extra', () => {
    const orig = dataFor('generate');
    const data = { ...structuredClone(orig), foo: 1 };
    assert.equal(checkData(data, orig, 'generate'), '"data" has invalid extra keys: foo');
  });

  test('acusa answers_names fora da fase prepare como chave extra', () => {
    const orig = dataFor('generate');
    const data = { ...structuredClone(orig), answers_names: {} };
    assert.equal(checkData(data, orig, 'generate'), '"data" has invalid extra keys: answers_names');
  });

  test('acusa chave faltando', () => {
    const orig = dataFor('generate');
    const data = structuredClone(orig);
    delete data.options;
    assert.equal(checkData(data, orig, 'generate'), '"options" is missing from "data"');
  });

  test('acusa params que não é objeto (null e texto)', () => {
    const orig = dataFor('generate');
    assert.equal(
      checkData({ ...structuredClone(orig), params: null }, orig, 'generate'),
      'data.params is not an object: null',
    );
    assert.equal(
      checkData({ ...structuredClone(orig), params: 'x' }, orig, 'generate'),
      'data.params is not an object: x',
    );
  });

  test('aceita params: [] (no PL, lista é object)', () => {
    const orig = dataFor('generate');
    assert.equal(checkData({ ...structuredClone(orig), params: [] }, orig, 'generate'), null);
  });

  test('acusa variant_seed não inteiro', () => {
    const orig = dataFor('generate');
    assert.equal(
      checkData({ ...structuredClone(orig), variant_seed: 1.5 }, orig, 'generate'),
      'data.variant_seed is not an integer: 1.5',
    );
  });
});

describe('checkData na fase prepare', () => {
  test('exige answers_names', () => {
    assertBaselineAccepted('prepare');
    const orig = dataFor('prepare');
    const data = structuredClone(orig);
    delete data.answers_names;
    assert.equal(checkData(data, orig, 'prepare'), '"answers_names" is missing from "data"');
  });
});

describe('checkData na fase parse', () => {
  test('aceita mudar submitted_answers, format_errors e feedback', () => {
    assertBaselineAccepted('parse');
    const orig = dataFor('parse');
    const data = {
      ...structuredClone(orig),
      submitted_answers: { v: 1 },
      format_errors: { v: 'Formato inválido' },
      feedback: { v: 'ok' },
    };
    assert.equal(checkData(data, orig, 'parse'), null);
  });

  test('aceita mudar params e correct_answers (parse que altera parâmetros)', () => {
    const orig = dataFor('parse');
    const data = { ...structuredClone(orig), params: { alterado: true }, correct_answers: { v: 3 } };
    assert.equal(checkData(data, orig, 'parse'), null);
  });

  test('rejeita mudança em raw_submitted_answers', () => {
    const orig = dataFor('parse');
    const data = { ...structuredClone(orig), raw_submitted_answers: { v: '2' } };
    assert.equal(checkData(data, orig, 'parse'), illegal('raw_submitted_answers', { v: '2' }, { v: '1' }));
    assert.equal(
      checkData(data, orig, 'parse'),
      'data.raw_submitted_answers has been illegally modified, new value: "{"v":"2"}", original value: "{"v":"1"}"',
    );
  });
});

describe('checkData na fase grade', () => {
  test('aceita mudar partial_scores e score', () => {
    assertBaselineAccepted('grade');
    const orig = dataFor('grade');
    const data = { ...structuredClone(orig), partial_scores: { v: { score: 1, weight: 1 } }, score: 1 };
    assert.equal(checkData(data, orig, 'grade'), null);
  });

  test('aceita mudar params e correct_answers (grade que altera parâmetros)', () => {
    const orig = dataFor('grade');
    const data = { ...structuredClone(orig), params: { graded: true }, correct_answers: { v: 3 } };
    assert.equal(checkData(data, orig, 'grade'), null);
  });

  test('rejeita score: Infinity', () => {
    const orig = dataFor('grade');
    assert.equal(
      checkData({ ...structuredClone(orig), score: Infinity }, orig, 'grade'),
      'data.score is not a number: Infinity',
    );
  });
});

describe('checkData na fase test', () => {
  const ALLOWED = new Set(['raw_submitted_answers', 'format_errors', 'partial_scores', 'score', 'feedback']);

  /** Novo valor do mesmo tipo, para que só a regra de edição decida. */
  function mutate(value: unknown): unknown {
    if (typeof value === 'number') return value + 1;
    if (typeof value === 'string') return `${value}-mudado`;
    if (typeof value === 'boolean') return !value;
    return { ...(value as Record<string, unknown>), mudado: true };
  }

  test('só aceita mudar raw_submitted_answers, format_errors, partial_scores, score e feedback', () => {
    assertBaselineAccepted('test');
    const orig = dataFor('test');
    for (const key of Object.keys(orig)) {
      const newValue = mutate(orig[key]);
      const data = { ...structuredClone(orig), [key]: newValue };
      const got = checkData(data, orig, 'test');
      if (ALLOWED.has(key)) {
        assert.equal(got, null, `a fase test deveria aceitar mudar ${key}`);
      } else {
        assert.equal(got, illegal(key, newValue, orig[key]), `a fase test deveria recusar mudar ${key}`);
      }
    }
  });
});

describe('checkData: fase desconhecida e igualdade estrutural', () => {
  test('fase desconhecida → unknown phase: x', () => {
    const d = dataFor('generate');
    assert.equal(checkData(d, structuredClone(d), 'x' as string as Phase), 'unknown phase: x');
  });

  test('a igualdade é estrutural (cópia profunda, outra ordem de chaves)', () => {
    const orig = { ...dataFor('test'), params: { a: { b: [1, { c: 2 }] }, z: 1 }, correct_answers: { k: [true, null] } };
    const equal = {
      ...structuredClone(orig),
      params: { z: 1, a: { b: [1, { c: 2 }] } },
      correct_answers: { k: [true, null] },
    };
    assert.notEqual(equal.params, orig.params, 'controle: objetos distintos');
    assert.equal(checkData(equal, orig, 'test'), null, 'estruturas iguais não são alteração');

    const changed = { ...structuredClone(orig), params: { a: { b: [1, { c: 3 }] }, z: 1 } };
    assert.equal(checkData(changed, orig, 'test'), illegal('params', changed.params, orig.params));
  });
});
