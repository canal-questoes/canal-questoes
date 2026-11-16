// AUTOTESTE, sem oráculo: o comparador e o driver da suíte de conformidade.
// Cobre a comparação e o relatório: o relatório aponta questão, seed, caso e campo; e o oráculo
// fora do ar → erro de ambiente, e não um falso "tudo igual". Roda no host:
//   node --test "engine/conformance/*.test.ts"
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  compareGrade,
  compareVariant,
  formatReport,
  htmlToText,
  type ComparableSubmission,
  type Divergence,
} from './compare.ts';
import { checkOracle, EnvironmentError } from './oracle.ts';

const KEY = { qid: 'element/numberInput', seed: '9ix', case: 'test:correct' };

/**
 * Mensagens de erro de formato como o oráculo as gravou (`submissions.format_errors` do `pl-oracle`, PL
 * a193f72), uma por elemento das 5 questões de referência, e o texto de cada uma pela regra de conversão de
 * html em texto (conferido com o `.text()` do cheerio da imagem do PL, a mesma conversão de `grade.test.ts`).
 */
const ORACLE_MESSAGES: ReadonlyArray<{ element: string; html: string; text: string }> = [
  {
    element: 'pl-multiple-choice (sky-color1 = "zz")',
    html: 'Invalid choice: <code class="user-output-invalid">zz</code>',
    text: 'Invalid choice: zz',
  },
  {
    element: 'pl-checkbox (group_ans_original)',
    html: 'You must select between <b>1</b> and <b>9</b> options.',
    text: 'You must select between 1 and 9 options.',
  },
  {
    element: 'pl-integer-input (template mustache)',
    html:
      '<p> Invalid format:\n            the submitted answer could not be interpreted as an integer.  No symbolic input, ' +
      'fractions, or scientific notation are allowed.\n    </p>\n    <p> Example <i>valid</i> inputs: ' +
      '<code class="me-2">1</code> <code class="me-2">-100</code> </p>\n    <p> Example <i>invalid</i> inputs: ' +
      '<code class="me-2">6+9</code> <code class="me-2">3.7</code> <code class="me-2">3*pi</code> ' +
      '<code class="me-2">6e19</code> <code class="me-2">0x1a</code> <code class="me-2">-0b0110</code> </p>\n    ' +
      '<hr>\n    <small>\n        <p> The submitted answer must be formatted as an integer. </p>\n    </small>',
    text:
      'Invalid format: the submitted answer could not be interpreted as an integer. No symbolic input, fractions, or ' +
      'scientific notation are allowed. Example valid inputs: 1 -100 Example invalid inputs: 6+9 3.7 3*pi 6e19 0x1a ' +
      '-0b0110 The submitted answer must be formatted as an integer.',
  },
  {
    element: 'pl-number-input (c_1)',
    html:
      '<p>\n            Invalid format: The submitted answer could not be interpreted as a decimal number.\n        ' +
      'Do not write variables or other symbols.\n    </p>\n    <p>Example <i>valid</i> inputs: <code>5</code>' +
      '<code class="ms-3">-17</code><code class="ms-3">4.25e7</code><code class="ms-3">3/7</code></p>\n    ' +
      '<p>Example <i>invalid</i> inputs: <code>6+9</code><code class="ms-3">4*10^7</code><code class="ms-3">3*pi</code>' +
      '<code class="ms-3">3.6i</code></p>',
    // Os <code> adjacentes, sem espaço entre eles no html, ficam colados no texto (regra ao pé da letra).
    text:
      'Invalid format: The submitted answer could not be interpreted as a decimal number. Do not write variables or ' +
      'other symbols. Example valid inputs: 5-174.25e73/7 Example invalid inputs: 6+94*10^73*pi3.6i',
  },
  {
    element: 'pl-string-input (ans1 em branco)',
    html: 'Invalid format. The submitted answer was left blank.',
    text: 'Invalid format. The submitted answer was left blank.',
  },
];

function message(index: number): { html: string; text: string } {
  const m = ORACLE_MESSAGES[index];
  assert.ok(m, `mensagem ${index} ausente da tabela`);
  return m;
}

/** Correção inválida do motor com os `format_errors` dados. */
function invalidEngineGrade(formatErrors: unknown): { status: number; body: Record<string, unknown> } {
  return engineGrade({ valid: false, score: null, partial_scores: null, format_errors: formatErrors });
}

/** Submissão inválida do oráculo com os `format_errors` dados. */
function invalidOracleSubmission(formatErrors: Record<string, unknown>): ComparableSubmission {
  return oracleSubmission({ gradable: false, score: null, partial_scores: null, format_errors: formatErrors });
}

/** O essencial de cada divergência, para comparar por valor. */
function essentials(divergences: readonly Divergence[]): Array<Record<string, unknown>> {
  return divergences.map((d) => {
    const out: Record<string, unknown> = { field: d.field, engine: d.engine, oracle: d.oracle };
    if (d.missing !== undefined) out.missing = d.missing;
    return out;
  });
}

function oracleSubmission(overrides: Partial<ComparableSubmission> = {}): ComparableSubmission {
  return {
    gradable: true,
    broken: false,
    format_errors: {},
    submitted_answer: { c_1: 10.816653826391969, c_2: 3 },
    partial_scores: { c_1: { score: 1, weight: 1 }, c_2: { score: 1, weight: 1 } },
    score: 1,
    feedback: {},
    params: { a: 9, b: 6 },
    true_answer: { c_1: 10.816653826391969, c_2: 3 },
    ...overrides,
  };
}

function engineGrade(overrides: Record<string, unknown> = {}): { status: number; body: Record<string, unknown> } {
  return {
    status: 200,
    body: {
      outcome: 'ok',
      valid: true,
      format_errors: {},
      submitted_answers: { c_1: 10.816653826391969, c_2: 3 },
      partial_scores: { c_1: { score: 1, weight: 1 }, c_2: { score: 1, weight: 1 } },
      score: 1,
      feedback: {},
      params: { a: 9, b: 6 },
      correct_answers: { c_1: 10.816653826391969, c_2: 3 },
      version_mismatch: { engine: false, pl: false },
      warnings: [],
      ...overrides,
    },
  };
}

function engineVariant(params: unknown, correctAnswers: unknown): { status: number; body: unknown } {
  return {
    status: 200,
    body: {
      outcome: 'ok',
      snapshot: {
        seed: '9ix',
        params,
        correct_answers: correctAnswers,
        preferences: {},
        engine_version: '1.0.0',
        pl_commit: 'a193f72048530a4499252c454acca963cdf98922',
        ir_version: '1.0',
      },
      ir: {},
      warnings: [],
    },
  };
}

function fields(divergences: readonly Divergence[]): string[] {
  return divergences.map((d) => d.field);
}

describe('comparador da conformidade (compare.ts)', () => {
  it('respostas iguais não geram divergência (linha de base dos testes abaixo)', () => {
    assert.deepEqual(compareGrade(KEY, engineGrade(), oracleSubmission()), [], 'correção igual acusou divergência');
    const variant = { params: { a: 9, b: 6 }, true_answer: { c_1: 10.816653826391969 }, broken_at: null };
    assert.deepEqual(
      compareVariant(KEY, engineVariant({ a: 9, b: 6 }, { c_1: 10.816653826391969 }), variant),
      [],
      'variante igual acusou divergência',
    );
  });

  it('acusa divergência no score', () => {
    const divergences = compareGrade(KEY, engineGrade({ score: 0.5 }), oracleSubmission());
    assert.equal(divergences.length, 1, `esperada uma divergência, vieram: ${JSON.stringify(divergences)}`);
    const [d] = divergences;
    assert.deepEqual(
      { qid: d?.qid, seed: d?.seed, case: d?.case, field: d?.field, engine: d?.engine, oracle: d?.oracle },
      { ...KEY, field: 'score', engine: 0.5, oracle: 1 },
      'a divergência precisa citar questão, seed, caso, campo e os dois valores',
    );
  });

  it('acusa divergência em format_errors quando um lado tem uma chave a mais', () => {
    const invalid = { valid: false, score: null, partial_scores: null };
    const oracleInvalid = { gradable: false, score: null, partial_scores: null };

    const engineExtra = compareGrade(
      KEY,
      engineGrade({
        ...invalid,
        format_errors: { c_1: { html: 'Invalid format.', text: 'Invalid format.' }, c_2: { html: 'Blank.', text: 'Blank.' } },
      }),
      oracleSubmission({ ...oracleInvalid, format_errors: { c_1: 'Invalid format.' } }),
    );
    assert.deepEqual(fields(engineExtra), ['format_errors.c_2'], 'chave a mais no motor não foi acusada');
    assert.equal(engineExtra[0]?.missing, 'oracle', 'a chave a mais no motor falta no oráculo');

    const oracleExtra = compareGrade(
      KEY,
      engineGrade({ ...invalid, format_errors: { c_1: { html: 'Invalid format.', text: 'Invalid format.' } } }),
      oracleSubmission({ ...oracleInvalid, format_errors: { c_1: 'Invalid format.', c_3: 'Blank.' } }),
    );
    assert.deepEqual(fields(oracleExtra), ['format_errors.c_3'], 'chave a mais no oráculo não foi acusada');
    assert.equal(oracleExtra[0]?.missing, 'engine', 'a chave a mais no oráculo falta no motor');

    const sameKeys = compareGrade(
      KEY,
      engineGrade({ ...invalid, format_errors: { c_1: { html: 'Invalid format.', text: 'Invalid format.' } } }),
      oracleSubmission({ ...oracleInvalid, format_errors: { c_1: 'Invalid format.' } }),
    );
    assert.deepEqual(sameKeys, [], 'format_errors[k].html igual a format_errors[k] do oráculo não pode divergir');
  });

  it('htmlToText aplica a regra de conversão do html em texto às mensagens reais do oráculo', () => {
    for (const m of ORACLE_MESSAGES) {
      assert.equal(htmlToText(m.html), m.text, `texto da mensagem de ${m.element}`);
    }
  });

  it('htmlToText decodifica as entidades e tira tags e comentários', () => {
    const cases: Array<[string, string]> = [
      ['a &lt; b &amp;&amp; c &gt; d &quot;e&quot; &#39;f&#x27; g&nbsp;h &apos;i', 'a < b && c > d "e" \'f\' g h \'i'],
      ['<p>x<!-- comentário --></p> z', 'x z'],
      ['<a title="1 > 0">ok</a>', 'ok'],
      ['1 < 2 e 3 <= 4', '1 < 2 e 3 <= 4'],
      ['<br/>linha<br>outra<hr />fim', 'linhaoutrafim'],
      ['AT & T e &#; e &#x;', 'AT & T e &#; e &#x;'],
      ['&#128512; &#x1F600;', '\u{1F600} \u{1F600}'],
      ['<p>  muitos \n\t espaços  </p>', 'muitos espaços'],
    ];
    for (const [html, text] of cases) assert.equal(htmlToText(html), text, `texto de ${JSON.stringify(html)}`);
  });

  it('htmlToText desiste (null) do que não sabe converter, e compareGrade acusa em vez de aceitar', () => {
    for (const html of ['&copy; 2026', '&copy', '&constructor;', '<script>x</script>', '<![CDATA[x]]>', '<!DOCTYPE html>x', '<table><tr>x</tr></table>', '&#150;', '<p sem fim']) {
      assert.equal(htmlToText(html), null, `htmlToText(${JSON.stringify(html)}) deveria desistir`);
    }
    const divergences = compareGrade(
      KEY,
      invalidEngineGrade({ c_1: { html: '&copy; 2026', text: '© 2026' } }),
      invalidOracleSubmission({ c_1: '&copy; 2026' }),
    );
    assert.deepEqual(fields(divergences), ['format_errors.c_1.text'], 'um html que o comparador não converte não pode passar calado');
    assert.match(divergences[0]?.detail ?? '', /não sabe converter/, 'a divergência precisa dizer que o texto não foi conferido');
  });

  it('format_errors com o html do oráculo e o texto da regra não diverge', () => {
    const engineErrors: Record<string, unknown> = {};
    const oracleErrors: Record<string, unknown> = {};
    ORACLE_MESSAGES.forEach((m, i) => {
      engineErrors[`c_${i}`] = { html: m.html, text: m.text };
      oracleErrors[`c_${i}`] = m.html;
    });
    assert.deepEqual(compareGrade(KEY, invalidEngineGrade(engineErrors), invalidOracleSubmission(oracleErrors)), []);
  });

  it('acusa format_errors.<k>.text diferente do texto do html do oráculo', () => {
    const number = message(3);
    // Texto errado: o próprio html, sem conversão.
    const raw = compareGrade(KEY, invalidEngineGrade({ c_1: { html: number.html, text: number.html } }), invalidOracleSubmission({ c_1: number.html }));
    assert.deepEqual(essentials(raw), [{ field: 'format_errors.c_1.text', engine: number.html, oracle: number.text }]);
    assert.deepEqual([raw[0]?.qid, raw[0]?.seed, raw[0]?.case], [KEY.qid, KEY.seed, KEY.case], 'a divergência precisa citar questão, seed e caso');

    // Texto com os espaços do html, sem colapsar nem aparar.
    const integer = message(2);
    const spaced = integer.html.replace(/<[^>]+>/g, '');
    const notCollapsed = compareGrade(KEY, invalidEngineGrade({ c_1: { html: integer.html, text: spaced } }), invalidOracleSubmission({ c_1: integer.html }));
    assert.deepEqual(essentials(notCollapsed), [{ field: 'format_errors.c_1.text', engine: spaced, oracle: integer.text }]);
  });

  it('acusa item de format_errors sem text ou que não é objeto {html, text}', () => {
    const mc = message(0);
    const noText = compareGrade(KEY, invalidEngineGrade({ c_1: { html: mc.html } }), invalidOracleSubmission({ c_1: mc.html }));
    assert.deepEqual(essentials(noText), [{ field: 'format_errors.c_1.text', engine: undefined, oracle: mc.text, missing: 'engine' }]);

    // O item na forma do oráculo (só a string) não é a forma do contrato: faltam html e text.
    const plain = compareGrade(KEY, invalidEngineGrade({ c_1: mc.html }), invalidOracleSubmission({ c_1: mc.html }));
    assert.deepEqual(essentials(plain), [
      { field: 'format_errors.c_1.html', engine: undefined, oracle: mc.html, missing: 'engine' },
      { field: 'format_errors.c_1.text', engine: undefined, oracle: mc.text, missing: 'engine' },
    ]);
  });

  it('acusa format_errors.<k>.html diferente do html do oráculo, mesmo com o texto certo (sem alteração)', () => {
    const mc = message(0);
    const divergences = compareGrade(
      KEY,
      invalidEngineGrade({ c_1: { html: mc.text, text: mc.text } }),
      invalidOracleSubmission({ c_1: mc.html }),
    );
    assert.deepEqual(essentials(divergences), [{ field: 'format_errors.c_1.html', engine: mc.text, oracle: mc.html }]);
  });

  it('acusa aviso numa correção ok das questões de referência, e a falta da lista', () => {
    const warning = { code: 'console_output', phase: 'grade', message: 'O código da questão escreveu no console.' };
    const extra = compareGrade(KEY, engineGrade({ warnings: [warning] }), oracleSubmission());
    assert.deepEqual(essentials(extra), [{ field: 'warnings[0]', engine: warning, oracle: undefined, missing: 'oracle' }]);
    assert.match(extra[0]?.detail ?? '', /nenhum aviso esperado/, 'a divergência precisa dizer de onde vem o esperado');

    const absent = compareGrade(KEY, engineGrade({ warnings: undefined }), oracleSubmission());
    assert.deepEqual(essentials(absent), [{ field: 'warnings', engine: undefined, oracle: [], missing: 'engine' }]);
  });

  it('acusa divergência em params, na correção e na variante', () => {
    const grade = compareGrade(KEY, engineGrade({ params: { a: 9, b: 7 } }), oracleSubmission());
    assert.deepEqual(fields(grade), ['params.b'], 'params divergentes na correção não foram acusados');

    const variant = compareVariant(KEY, engineVariant({ a: 9, b: 7 }, { c_1: 10.816653826391969 }), {
      params: { a: 9, b: 6 },
      true_answer: { c_1: 10.816653826391969 },
      broken_at: null,
    });
    assert.deepEqual(fields(variant), ['params.b'], 'params divergentes na variante não foram acusados');
    assert.deepEqual([variant[0]?.engine, variant[0]?.oracle], [7, 6]);
  });

  it('ignora a ordem das chaves e trata 1 e 1.0 como iguais', () => {
    // Corpos vindos de JSON de verdade: o motor escreve "1.0" e outra ordem de chaves; o jsonb do oráculo, "1".
    const engineBody = JSON.parse(
      '{"warnings":[],"version_mismatch":{"pl":false,"engine":false},"correct_answers":{"c_2":3.0,"c_1":10.816653826391969},' +
        '"params":{"b":6.0,"a":9},"feedback":{},"score":1.0,"partial_scores":{"c_2":{"weight":1,"score":1.0},' +
        '"c_1":{"weight":1.0,"score":1}},"submitted_answers":{"c_2":3,"c_1":10.816653826391969},"format_errors":{},' +
        '"valid":true,"outcome":"ok"}',
    );
    const oracle = JSON.parse(
      '{"gradable":true,"broken":false,"format_errors":{},"submitted_answer":{"c_1":10.816653826391969,"c_2":3},' +
        '"partial_scores":{"c_1":{"score":1,"weight":1},"c_2":{"score":1,"weight":1}},"score":1,"feedback":{},' +
        '"params":{"a":9,"b":6},"true_answer":{"c_1":10.816653826391969,"c_2":3}}',
    );
    assert.deepEqual(compareGrade(KEY, { status: 200, body: engineBody }, oracle), [], 'ordem de chaves ou 1/1.0 geraram divergência');

    const variant = compareVariant(KEY, engineVariant(JSON.parse('{"b":6.0,"a":9}'), JSON.parse('{"c_2":3.0,"c_1":1}')), {
      params: JSON.parse('{"a":9,"b":6}'),
      true_answer: JSON.parse('{"c_1":1.0,"c_2":3}'),
      broken_at: null,
    });
    assert.deepEqual(variant, [], 'ordem de chaves ou 1/1.0 geraram divergência na variante');
  });

  it('formatReport agrupa e cita questão, seed, caso e campo', () => {
    const divergences = [
      ...compareGrade(KEY, engineGrade({ score: 0.5 }), oracleSubmission()),
      ...compareGrade({ ...KEY, case: 'mao:virgula_decimal' }, engineGrade({ params: { a: 9, b: 7 } }), oracleSubmission()),
      ...compareGrade({ qid: 'element/checkbox', seed: '1hq3', case: 'test:invalid' }, engineGrade({ feedback: { x: 1 } }), oracleSubmission()),
    ];
    assert.equal(divergences.length, 3);
    const report = formatReport(divergences);
    for (const text of [
      'element/numberInput',
      '9ix',
      'test:correct',
      'score',
      'mao:virgula_decimal',
      'params.b',
      'element/checkbox',
      '1hq3',
      'test:invalid',
      'feedback.x',
    ]) {
      assert.ok(report.includes(text), `o relatório não cita "${text}":\n${report}`);
    }
    const qidLines = report.split('\n').filter((line) => line.includes('element/numberInput'));
    assert.equal(qidLines.length, 1, `a questão deveria aparecer uma vez só, agrupando os casos:\n${report}`);
    assert.ok(report.split('\n').some((line) => /score/.test(line) && /0\.5/.test(line) && /\b1\b/.test(line)), `o relatório não mostra os dois valores do score:\n${report}`);
  });
});

describe('driver do oráculo (oracle.ts)', () => {
  it('checkOracle rejeita com EnvironmentError quando o oráculo não responde', async () => {
    // 127.0.0.1:3999: porta fechada comum (a porta 9 é bloqueada pelo próprio fetch do Node e não
    // exercitaria a conexão recusada).
    for (const url of ['http://127.0.0.1:3999', 'http://127.0.0.1:9']) {
      await assert.rejects(checkOracle(url), (err: unknown) => {
        assert.ok(err instanceof EnvironmentError, `esperado EnvironmentError em ${url}, veio ${String(err)}`);
        assert.match(err.message, /oráculo/, 'a mensagem de ambiente deve estar em português e citar o oráculo');
        return true;
      });
    }
  });
});
