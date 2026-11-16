// computeScore: score calculado a partir de partial_scores, como o PL.
// Cobre a nota final (calculada a partir das notas por parte, na ordem do PL) e a fidelidade ao PL,
// com o score como na correção do PL. Regra conferida em
// /PrairieLearn/apps/prairielearn/dist/question-servers/freeform.js (getPartialScoreValues e
// processQuestionHtml, linhas 409–463): score não numérico conta 0, weight não numérico conta 1.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { computeScore } from '../../src/pl/score.ts';

describe('computeScore com crédito parcial', () => {
  test('média ponderada por weight (padrão 1)', () => {
    assert.equal(computeScore({ a: { score: 1, weight: 3 }, b: { score: 0 } }, true), 0.75);
    assert.equal(computeScore({ a: { score: 1 }, b: { score: 0.5 } }, true), 0.75);
  });

  test('valor não numérico conta como score 0 e weight 1', () => {
    assert.equal(computeScore({ a: { score: '1' }, b: { score: 1 } }, true), 0.5, 'score em texto conta 0');
    assert.equal(computeScore({ a: { score: 1, weight: '3' }, b: { score: 0 } }, true), 0.5, 'weight em texto conta 1');
    assert.equal(computeScore({ a: null, b: { score: 1 } }, true), 0.5, 'parte null conta 0/1');
    assert.equal(computeScore({ a: 'x', b: { score: 1 } }, true), 0.5, 'parte que não é objeto conta 0/1');
  });

  test('{} → 0', () => {
    assert.equal(computeScore({}, true), 0);
  });

  test('partial_scores ausente → 0 (como o "?? {}" do PL)', () => {
    assert.equal(computeScore(undefined, true), 0);
  });
});

describe('computeScore sem crédito parcial', () => {
  test('1 só com pelo menos uma parte e todas ≥ 1', () => {
    assert.equal(computeScore({ a: { score: 1 }, b: { score: 1 } }, false), 1);
    assert.equal(computeScore({ a: { score: 1.5 } }, false), 1, 'score acima de 1 também conta como ≥ 1');
  });

  test('{} → 0', () => {
    assert.equal(computeScore({}, false), 0);
    assert.equal(computeScore(undefined, false), 0);
  });

  test('uma parte abaixo de 1 → 0', () => {
    assert.equal(computeScore({ a: { score: 1 }, b: { score: 0.99 } }, false), 0);
  });

  test('score não numérico conta como 0', () => {
    assert.equal(computeScore({ a: { score: 1 }, b: { score: '1' } }, false), 0);
  });
});
