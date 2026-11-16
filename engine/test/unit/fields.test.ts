// describeField: tipo, formato aceito, restrições, peso e dica de cada campo.
// Cobre o formato aceito (descritor estruturado derivado dos atributos; dica em português
// com no máximo 64 caracteres; restrições com as regras de precedência do PL; peso; base dos
// inteiros) e as regras dos campos da IR (o mapa de tipos, o formato e as restrições, as regras do
// multiple_choice). Padrões conferidos no código do PL (a193f72), no container pl-oracle:
// - elements/pl-number-input/pl-number-input.py: RTOL_DEFAULT 1e-2, ATOL_DEFAULT 1e-8, DIGITS_DEFAULT 2,
//   COMPARISON_DEFAULT relabs, ALLOW_FRACTIONS_DEFAULT True, ALLOW_COMPLEX_DEFAULT False, ALLOW_BLANK_DEFAULT False;
// - elements/pl-integer-input/pl-integer-input.py: BASE_DEFAULT 10, ALLOW_BLANK_DEFAULT False;
// - elements/pl-string-input/pl-string-input.py: IGNORE_CASE/REMOVE_SPACES/NORMALIZE_TO_ASCII/MULTILINE
//   False; remove-leading-trailing com padrão "multiline or False"; correct-answer-format exact|regex;
// - elements/pl-checkbox/pl-checkbox.py: PARTIAL_CREDIT_DEFAULT off; get_partial_credit_mode (legado
//   "true" + partial-credit-method PC|COV|EDC, padrão PC; "false" → off); get_min/max_options_to_select
//   (min-select; senão min-correct se detailed-help-text; senão 1 / nº de opções exibidas; cada limite
//   resolvido à parte, e min-select/max-select antes de min-correct/max-correct);
// - elements/pl-multiple-choice/pl-multiple-choice.py: ALLOW_BLANK_DEFAULT False;
// - python/prairielearn/html_utils.py: get_boolean_attrib aceita _PL_BOOLEAN_TRUE ("true", "True",
//   "1", …) e _PL_BOOLEAN_FALSE ("false", "False", "0", …).
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { describeField, type FieldDescription } from '../../src/ir/fields.ts';
import { assertPortuguese } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

/** Exige a dica do contrato: 1 a 64 caracteres, em português. */
function assertHint(result: FieldDescription, where: string): void {
  assert.equal(typeof result.hint, 'string', `hint deve ser texto (${where})`);
  const length = [...result.hint].length;
  assert.ok(length >= 1 && length <= 64, `hint com ${length} caracteres (${where}): ${result.hint}`);
  assertPortuguese(result.hint);
}

/** describeField + checagem da dica. */
function describeChecked(tag: string, attrs: Record<string, string>, optionCount: number | null): FieldDescription {
  const result = describeField(tag, attrs, optionCount);
  assertHint(result, `${tag} ${JSON.stringify(attrs)}`);
  return result;
}

describe('describeField: pl-number-input', () => {
  test('sem atributos → number relabs com os padrões do PL', () => {
    const r = describeChecked('pl-number-input', { 'answers-name': 'x' }, null);
    assert.equal(r.type, 'number');
    assert.deepEqual(r.format, {
      kind: 'number',
      comparison: 'relabs',
      rtol: 0.01,
      atol: 1e-8,
      allow_fractions: true,
      allow_complex: false,
    });
    assert.deepEqual(r.constraints, { allow_blank: false });
    assert.equal(r.weight, 1);
  });

  test('rtol e atol explícitos são repassados', () => {
    const r = describeChecked('pl-number-input', { 'answers-name': 'x', rtol: '0.05', atol: '0.1' }, null);
    assert.deepEqual(r.format, {
      kind: 'number',
      comparison: 'relabs',
      rtol: 0.05,
      atol: 0.1,
      allow_fractions: true,
      allow_complex: false,
    });
  });

  test('comparison="sigfig" digits="4" → digits 4, sem rtol nem atol', () => {
    const r = describeChecked('pl-number-input', { 'answers-name': 'x', comparison: 'sigfig', digits: '4' }, null);
    assert.deepEqual(r.format, {
      kind: 'number',
      comparison: 'sigfig',
      digits: 4,
      allow_fractions: true,
      allow_complex: false,
    });
  });

  test('comparison="decdig" sem digits → digits 2 (DIGITS_DEFAULT)', () => {
    const r = describeChecked('pl-number-input', { 'answers-name': 'x', comparison: 'decdig' }, null);
    assert.deepEqual(r.format, {
      kind: 'number',
      comparison: 'decdig',
      digits: 2,
      allow_fractions: true,
      allow_complex: false,
    });
  });

  test('allow-fractions="false" → allow_fractions false, com os demais padrões do PL', () => {
    // [F] pl-number-input.py: ALLOW_FRACTIONS_DEFAULT True, lido com get_boolean_attrib("allow-fractions").
    const r = describeChecked('pl-number-input', { 'answers-name': 'x', 'allow-fractions': 'false' }, null);
    assert.deepEqual(r.format, {
      kind: 'number',
      comparison: 'relabs',
      rtol: 0.01,
      atol: 1e-8,
      allow_fractions: false,
      allow_complex: false,
    });
    assert.deepEqual(r.constraints, { allow_blank: false });
  });
});

describe('describeField: pl-integer-input', () => {
  test('base="16" → {kind: "integer", base: 16}', () => {
    const r = describeChecked('pl-integer-input', { 'answers-name': 'i', base: '16' }, null);
    assert.equal(r.type, 'integer');
    assert.deepEqual(r.format, { kind: 'integer', base: 16 });
    assert.deepEqual(r.constraints, { allow_blank: false });
    assert.equal(r.weight, 1);
  });

  test('sem base → base 10', () => {
    const r = describeChecked('pl-integer-input', { 'answers-name': 'i' }, null);
    assert.deepEqual(r.format, { kind: 'integer', base: 10 });
  });

  test('base="0" → base 0, e não 10 (aceita os prefixos 0x, 0b e 0o)', () => {
    // [F] pl-integer-input.py: base = get_integer_attrib("base", 10); base 0 é o modo com prefixo (zero_base).
    const r = describeChecked('pl-integer-input', { 'answers-name': 'i', base: '0' }, null);
    assert.deepEqual(r.format, { kind: 'integer', base: 0 });
  });
});

describe('describeField: pl-string-input', () => {
  test('ignore-case="true" multiline="true" → remove_leading_trailing segue multiline', () => {
    const r = describeChecked('pl-string-input', { 'answers-name': 't', 'ignore-case': 'true', multiline: 'true' }, null);
    assert.equal(r.type, 'text');
    assert.deepEqual(r.format, {
      kind: 'text',
      ignore_case: true,
      multiline: true,
      remove_leading_trailing: true,
      remove_spaces: false,
      normalize_to_ascii: false,
      regex: false,
    });
    assert.deepEqual(r.constraints, { allow_blank: false });
  });

  test('sem atributos → todos false', () => {
    const r = describeChecked('pl-string-input', { 'answers-name': 't' }, null);
    assert.deepEqual(r.format, {
      kind: 'text',
      ignore_case: false,
      multiline: false,
      remove_leading_trailing: false,
      remove_spaces: false,
      normalize_to_ascii: false,
      regex: false,
    });
  });

  test('multiline="true" remove-leading-trailing="false" → remove_leading_trailing false', () => {
    const r = describeChecked(
      'pl-string-input',
      { 'answers-name': 't', multiline: 'true', 'remove-leading-trailing': 'false' },
      null,
    );
    assert.equal(r.format.multiline, true);
    assert.equal(r.format.remove_leading_trailing, false);
  });

  test('correct-answer-format="regex" → regex true', () => {
    const r = describeChecked('pl-string-input', { 'answers-name': 't', 'correct-answer-format': 'regex' }, null);
    assert.equal(r.format.regex, true);
  });
});

describe('describeField: pl-multiple-choice', () => {
  test('allow-blank="true" → single_choice com allow_blank true', () => {
    const r = describeChecked('pl-multiple-choice', { 'answers-name': 'mc', 'allow-blank': 'true' }, 3);
    assert.equal(r.type, 'single_choice');
    assert.deepEqual(r.format, { kind: 'single_choice' });
    assert.deepEqual(r.constraints, { allow_blank: true });
  });

  test('sem allow-blank → allow_blank false', () => {
    const r = describeChecked('pl-multiple-choice', { 'answers-name': 'mc' }, 3);
    assert.deepEqual(r.constraints, { allow_blank: false });
  });
});

describe('describeField: pl-checkbox', () => {
  test('5 opções exibidas, sem limites → min_select 1, max_select 5 e partial_credit off', () => {
    const r = describeChecked('pl-checkbox', { 'answers-name': 'cb' }, 5);
    assert.equal(r.type, 'multiple_choice');
    assert.deepEqual(r.format, { kind: 'multiple_choice', partial_credit: 'off' });
    assert.deepEqual(r.constraints, { allow_blank: false, min_select: 1, max_select: 5 });
  });

  test('min-select="2" max-select="3" partial-credit="coverage" → 2, 3 e coverage', () => {
    const r = describeChecked(
      'pl-checkbox',
      { 'answers-name': 'cb', 'min-select': '2', 'max-select': '3', 'partial-credit': 'coverage' },
      5,
    );
    assert.equal(r.constraints.min_select, 2);
    assert.equal(r.constraints.max_select, 3);
    assert.equal(r.format.partial_credit, 'coverage');
  });

  test('detailed-help-text="true" min-correct="2" max-correct="4", 5 opções → 2 e 4', () => {
    const r = describeChecked(
      'pl-checkbox',
      { 'answers-name': 'cb', 'detailed-help-text': 'true', 'min-correct': '2', 'max-correct': '4' },
      5,
    );
    assert.equal(r.constraints.min_select, 2);
    assert.equal(r.constraints.max_select, 4);
  });

  test('min-correct="2" sem detailed-help-text → min_select 1', () => {
    const r = describeChecked('pl-checkbox', { 'answers-name': 'cb', 'min-correct': '2' }, 5);
    assert.equal(r.constraints.min_select, 1);
    assert.equal(r.constraints.max_select, 5);
  });

  // [F] pl-checkbox.py (a193f72), get_min_options_to_select e get_max_options_to_select, em ordem de
  // prioridade: 1. min-select/max-select; 2. min-correct/max-correct, só com detailed-help-text;
  // 3. o padrão (MIN_SELECT_DEFAULT 1; no máximo, o nº de opções exibidas). Cada limite é resolvido
  // à parte.
  test('min-select e max-select prevalecem sobre detailed-help-text com min-correct e max-correct', () => {
    const r = describeChecked(
      'pl-checkbox',
      {
        'answers-name': 'cb',
        'min-select': '2',
        'max-select': '3',
        'detailed-help-text': 'true',
        'min-correct': '1',
        'max-correct': '4',
      },
      5,
    );
    assert.deepEqual(
      r.constraints,
      { allow_blank: false, min_select: 2, max_select: 3 },
      'min-select e max-select vêm antes de min-correct e max-correct',
    );
  });

  test('detailed-help-text="true" com min-correct e sem max-correct → max_select volta ao nº de opções exibidas', () => {
    const r = describeChecked(
      'pl-checkbox',
      { 'answers-name': 'cb', 'detailed-help-text': 'true', 'min-correct': '2' },
      5,
    );
    assert.deepEqual(
      r.constraints,
      { allow_blank: false, min_select: 2, max_select: 5 },
      'sem max-select nem max-correct, o máximo é o nº de opções exibidas',
    );
  });

  test('partial-credit legado: "true" + partial-credit-method, e "true" sozinho', () => {
    const cov = describeChecked(
      'pl-checkbox',
      { 'answers-name': 'cb', 'partial-credit': 'true', 'partial-credit-method': 'COV' },
      5,
    );
    assert.equal(cov.format.partial_credit, 'coverage');
    const pc = describeChecked('pl-checkbox', { 'answers-name': 'cb', 'partial-credit': 'true' }, 5);
    assert.equal(pc.format.partial_credit, 'net-correct', 'o método legado padrão é PC → net-correct');
    const edc = describeChecked(
      'pl-checkbox',
      { 'answers-name': 'cb', 'partial-credit': 'true', 'partial-credit-method': 'EDC' },
      5,
    );
    assert.equal(edc.format.partial_credit, 'each-answer');
    const off = describeChecked('pl-checkbox', { 'answers-name': 'cb', 'partial-credit': 'false' }, 5);
    assert.equal(off.format.partial_credit, 'off');
  });

  test('partial-credit no estilo novo é repassado', () => {
    for (const mode of ['off', 'net-correct', 'coverage', 'each-answer']) {
      const r = describeChecked('pl-checkbox', { 'answers-name': 'cb', 'partial-credit': mode }, 5);
      assert.equal(r.format.partial_credit, mode);
    }
  });
});

describe('describeField: allow_blank em todos os tipos', () => {
  // [F] ALLOW_BLANK_DEFAULT False e get_boolean_attrib("allow-blank") em pl-number-input.py,
  // pl-string-input.py e pl-checkbox.py. No pl-checkbox o allow-blank não muda min/max_select
  // (get_min_options_to_select usa MIN_SELECT_DEFAULT 1; o máximo é o nº de opções exibidas).
  test('allow-blank="true" em pl-number-input → constraints {allow_blank: true}', () => {
    const r = describeChecked('pl-number-input', { 'answers-name': 'x', 'allow-blank': 'true' }, null);
    assert.deepEqual(r.constraints, { allow_blank: true });
  });

  test('allow-blank="true" em pl-string-input → constraints {allow_blank: true}', () => {
    const r = describeChecked('pl-string-input', { 'answers-name': 't', 'allow-blank': 'true' }, null);
    assert.deepEqual(r.constraints, { allow_blank: true });
  });

  test('allow-blank="true" em pl-checkbox com 3 opções → allow_blank true, min_select 1 e max_select 3', () => {
    const r = describeChecked('pl-checkbox', { 'answers-name': 'cb', 'allow-blank': 'true' }, 3);
    assert.deepEqual(r.constraints, { allow_blank: true, min_select: 1, max_select: 3 });
  });
});

describe('describeField: peso e booleanos', () => {
  test('weight="3" → 3', () => {
    assert.equal(describeChecked('pl-integer-input', { 'answers-name': 'i', weight: '3' }, null).weight, 3);
    assert.equal(describeChecked('pl-checkbox', { 'answers-name': 'cb', weight: '3' }, 4).weight, 3);
  });

  test('booleanos como no pl.get_boolean_attrib: true/false, True, 1/0', () => {
    const cases: Array<[string, boolean]> = [
      ['true', true],
      ['false', false],
      ['True', true],
      ['1', true],
      ['0', false],
    ];
    for (const [value, expected] of cases) {
      const text = describeChecked('pl-string-input', { 'answers-name': 't', 'ignore-case': value }, null);
      assert.equal(text.format.ignore_case, expected, `ignore-case="${value}"`);
      const integer = describeChecked('pl-integer-input', { 'answers-name': 'i', 'allow-blank': value }, null);
      assert.equal(integer.constraints.allow_blank, expected, `allow-blank="${value}"`);
      const number = describeChecked('pl-number-input', { 'answers-name': 'x', 'allow-complex': value }, null);
      assert.equal(number.format.allow_complex, expected, `allow-complex="${value}"`);
      const checkbox = describeChecked(
        'pl-checkbox',
        { 'answers-name': 'cb', 'detailed-help-text': value, 'min-correct': '2', 'max-correct': '4' },
        5,
      );
      assert.equal(checkbox.constraints.min_select, expected ? 2 : 1, `detailed-help-text="${value}"`);
    }
  });
});

describe('describeField: dica', () => {
  const CASES: Array<[string, Record<string, string>, number | null]> = [
    ['pl-number-input', { 'answers-name': 'x' }, null],
    ['pl-number-input', { 'answers-name': 'x', comparison: 'sigfig', digits: '4' }, null],
    ['pl-number-input', { 'answers-name': 'x', 'allow-complex': 'true', 'allow-fractions': 'false' }, null],
    ['pl-integer-input', { 'answers-name': 'i' }, null],
    ['pl-integer-input', { 'answers-name': 'i', base: '16' }, null],
    ['pl-integer-input', { 'answers-name': 'i', base: '0' }, null],
    ['pl-string-input', { 'answers-name': 't' }, null],
    ['pl-string-input', { 'answers-name': 't', multiline: 'true', 'correct-answer-format': 'regex' }, null],
    ['pl-multiple-choice', { 'answers-name': 'mc' }, 4],
    ['pl-multiple-choice', { 'answers-name': 'mc', 'allow-blank': 'true' }, 4],
    ['pl-checkbox', { 'answers-name': 'cb' }, 5],
    ['pl-checkbox', { 'answers-name': 'cb', 'min-select': '2', 'max-select': '3' }, 5],
  ];

  test('toda hint tem de 1 a 64 caracteres e passa em assertPortuguese', () => {
    for (const [tag, attrs, count] of CASES) describeChecked(tag, attrs, count);
  });

  test('o resultado cabe no esquema de campo da IR', () => {
    for (const [tag, attrs, count] of CASES) {
      const r = describeChecked(tag, attrs, count);
      const isChoice = r.type === 'single_choice' || r.type === 'multiple_choice';
      const options = isChoice
        ? Array.from({ length: count ?? 1 }, (_, i) => ({
            key: String.fromCharCode(97 + i),
            html: `<p>opção ${i}</p>`,
            content_hash: `sha256:${'0'.repeat(64)}`,
          }))
        : null;
      // IR mínima em volta do campo, para validar contra ir.schema.json (o formato é oneOf estrito).
      assertValid('ir', {
        ir_version: '1.0',
        statement: { html: `<p><cq-field name="${attrs['answers-name']}"></cq-field></p>` },
        fields: [
          {
            name: attrs['answers-name'],
            type: r.type,
            label_html: null,
            suffix_html: null,
            format: r.format,
            hint: r.hint,
            constraints: r.constraints,
            weight: r.weight,
            options,
          },
        ],
        answer_key: [{ name: attrs['answers-name'], can_show: true, correct_value: null, display_html: null }],
        explanation: null,
        media: [],
      });
    }
  });
});
