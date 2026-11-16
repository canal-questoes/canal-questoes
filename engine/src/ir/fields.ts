// Descrição de um campo de resposta da IR: tipo, formato aceito, restrições, peso e dica
// de cada elemento de resposta suportado, a partir dos atributos da tag, como o PL os lê.
//
// Os padrões e as regras de leitura dos atributos vêm do código do PL fixado (a193f72) [F]:
// - python/prairielearn/html_utils.py: _get_attrib (aceita o nome antigo com "_"), get_boolean_attrib
//   (_PL_BOOLEAN_TRUE/_PL_BOOLEAN_FALSE), get_integer_attrib (int()), get_float_attrib (float()) e
//   get_enum_attrib (valor em minúsculas, entre os nomes do enum);
// - elements/pl-{number,integer,string}-input, pl-multiple-choice e pl-checkbox: constantes *_DEFAULT,
//   get_partial_credit_mode, get_min_options_to_select e get_max_options_to_select.
//
// Valores que o PL recusaria (ValueError) não chegam aqui: o prepare de cada elemento valida os
// atributos contra o schema do elemento (pl.validate_element, formatos boolean/integer/number e enums)
// antes de qualquer render. Por isso, um valor inválido cai no padrão do elemento, e a função é total.

export type FieldType = 'number' | 'integer' | 'text' | 'single_choice' | 'multiple_choice';

export interface FieldDescription {
  type: FieldType;
  format: Record<string, unknown>;
  constraints: Record<string, unknown>;
  weight: number;
  hint: string;
}

type Attrs = Record<string, string>;

// ---------------------------------------------------------------------------------------------
// Leitura de atributos como no prairielearn/html_utils.py
// ---------------------------------------------------------------------------------------------

const PL_BOOLEAN_TRUE: ReadonlySet<string> = new Set(['true', 't', '1', 'True', 'T', 'TRUE', 'yes', 'y', 'Yes', 'Y', 'YES']);
const PL_BOOLEAN_FALSE: ReadonlySet<string> = new Set(['false', 'f', '0', 'False', 'F', 'FALSE', 'no', 'n', 'No', 'N', 'NO']);

/** _get_attrib: o nome com hífen; se ausente, o nome antigo com "_" no lugar de cada "-". */
export function getAttrib(attrs: Attrs, name: string): string | undefined {
  if (Object.hasOwn(attrs, name)) return attrs[name];
  const legacyName = name.replaceAll('-', '_');
  if (Object.hasOwn(attrs, legacyName)) return attrs[legacyName];
  return undefined;
}

/** has_attrib. */
export function hasAttrib(attrs: Attrs, name: string): boolean {
  return getAttrib(attrs, name) !== undefined;
}

/** get_boolean_attrib com padrão: só os valores exatos dos conjuntos do PL. */
export function getBooleanAttrib(attrs: Attrs, name: string, fallback: boolean): boolean {
  const value = getAttrib(attrs, name);
  if (value === undefined) return fallback;
  if (PL_BOOLEAN_TRUE.has(value)) return true;
  if (PL_BOOLEAN_FALSE.has(value)) return false;
  return fallback;
}

// int() e float() do Python: espaços nas pontas, sinal e "_" entre dígitos (PEP 515).
const PY_DIGITS = String.raw`\d(?:_?\d)*`;
const PY_INT = new RegExp(`^[+-]?${PY_DIGITS}$`);
const PY_FLOAT = new RegExp(`^[+-]?(?:(?:${PY_DIGITS})?\\.${PY_DIGITS}|${PY_DIGITS}\\.?)(?:[eE][+-]?${PY_DIGITS})?$`);

function parsePyInt(text: string): number | null {
  const trimmed = text.trim();
  if (!PY_INT.test(trimmed)) return null;
  const value = Number(trimmed.replaceAll('_', ''));
  if (!Number.isSafeInteger(value)) return null;
  return value === 0 ? 0 : value; // sem -0
}

function parsePyFloat(text: string): number | null {
  const trimmed = text.trim();
  if (!PY_FLOAT.test(trimmed)) return null;
  // inf e nan (aceitos pelo float() do Python) não têm representação no JSON da IR.
  const value = Number(trimmed.replaceAll('_', ''));
  return Number.isFinite(value) ? value : null;
}

/** get_integer_attrib com padrão. */
export function getIntegerAttrib(attrs: Attrs, name: string, fallback: number): number {
  const value = getAttrib(attrs, name);
  if (value === undefined) return fallback;
  return parsePyInt(value) ?? fallback;
}

/** get_float_attrib com padrão. */
export function getFloatAttrib(attrs: Attrs, name: string, fallback: number): number {
  const value = getAttrib(attrs, name);
  if (value === undefined) return fallback;
  return parsePyFloat(value) ?? fallback;
}

/** get_enum_attrib com padrão; `allowed` traz os valores aceitos, já em minúsculas. */
function getEnumAttrib<T extends string>(attrs: Attrs, name: string, allowed: readonly T[], fallback: T): T {
  const value = getAttrib(attrs, name);
  if (value === undefined) return fallback;
  return (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

// ---------------------------------------------------------------------------------------------
// Padrões dos elementos (a193f72)
// ---------------------------------------------------------------------------------------------

const WEIGHT_DEFAULT = 1; // WEIGHT_DEFAULT em todos os cinco elementos
const ALLOW_BLANK_DEFAULT = false; // ALLOW_BLANK_DEFAULT em todos os cinco elementos

// pl-number-input.py
const COMPARISONS = ['relabs', 'sigfig', 'decdig'] as const;
type Comparison = (typeof COMPARISONS)[number];
const COMPARISON_DEFAULT: Comparison = 'relabs';
const RTOL_DEFAULT = 1e-2;
const ATOL_DEFAULT = 1e-8;
const DIGITS_DEFAULT = 2;
const ALLOW_FRACTIONS_DEFAULT = true;
const ALLOW_COMPLEX_DEFAULT = false;

// pl-integer-input.py
const BASE_DEFAULT = 10;

// pl-string-input.py
const IGNORE_CASE_DEFAULT = false;
const REMOVE_SPACES_DEFAULT = false;
const NORMALIZE_TO_ASCII_DEFAULT = false;
const MULTILINE_DEFAULT = false;
const REMOVE_LEADING_TRAILING_DEFAULT = false;
const CORRECT_ANSWER_FORMATS = ['exact', 'regex'] as const;

// pl-checkbox.py
const PARTIAL_CREDIT_MODES = ['off', 'net-correct', 'coverage', 'each-answer'] as const;
type PartialCredit = (typeof PARTIAL_CREDIT_MODES)[number];
const PARTIAL_CREDIT_DEFAULT: PartialCredit = 'off';
const LEGACY_PARTIAL_CREDIT_METHOD_DEFAULT = 'PC';
const LEGACY_PARTIAL_CREDIT: ReadonlyMap<string, PartialCredit> = new Map([
  ['PC', 'net-correct'],
  ['COV', 'coverage'],
  ['EDC', 'each-answer'],
]);
const DETAILED_HELP_TEXT_DEFAULT = false;
const MIN_SELECT_DEFAULT = 1;

// ---------------------------------------------------------------------------------------------
// Regras do pl-checkbox
// ---------------------------------------------------------------------------------------------

/**
 * get_partial_credit_mode: primeiro o estilo antigo (partial-credit booleano + partial-credit-method
 * PC|COV|EDC, padrão PC; "false" → off); senão, o estilo novo (off|net-correct|coverage|each-answer).
 */
function partialCreditMode(attrs: Attrs): PartialCredit {
  const value = getAttrib(attrs, 'partial-credit');
  if (value === undefined) return PARTIAL_CREDIT_DEFAULT;
  if (PL_BOOLEAN_FALSE.has(value)) return 'off';
  if (PL_BOOLEAN_TRUE.has(value)) {
    const method = getAttrib(attrs, 'partial-credit-method') ?? LEGACY_PARTIAL_CREDIT_METHOD_DEFAULT;
    // Método inválido: o PL tenta o estilo novo, que recusa partial-credit-method (ValueError).
    return LEGACY_PARTIAL_CREDIT.get(method) ?? PARTIAL_CREDIT_DEFAULT;
  }
  // Estilo novo com partial-credit-method: o PL recusa (ValueError).
  if (hasAttrib(attrs, 'partial-credit-method')) return PARTIAL_CREDIT_DEFAULT;
  return getEnumAttrib(attrs, 'partial-credit', PARTIAL_CREDIT_MODES, PARTIAL_CREDIT_DEFAULT);
}

/**
 * get_min_options_to_select / get_max_options_to_select: 1. min-select/max-select; 2. min-correct/
 * max-correct, só com detailed-help-text; 3. o padrão. Cada limite é resolvido à parte.
 */
function optionsToSelect(attrs: Attrs, bound: 'min' | 'max', fallback: number): number {
  const detailedHelpText = getBooleanAttrib(attrs, 'detailed-help-text', DETAILED_HELP_TEXT_DEFAULT);
  if (hasAttrib(attrs, `${bound}-select`)) return getIntegerAttrib(attrs, `${bound}-select`, fallback);
  if (hasAttrib(attrs, `${bound}-correct`) && detailedHelpText) {
    return getIntegerAttrib(attrs, `${bound}-correct`, fallback);
  }
  return fallback;
}

// ---------------------------------------------------------------------------------------------
// Dica (≤ 64 caracteres, em português, neutra quanto à vírgula decimal, que é tratada no núcleo)
// ---------------------------------------------------------------------------------------------

/** Limite do input_field_placeholder do Telegram. */
const HINT_MAX_LENGTH = 64;

/** A primeira candidata que cabe no limite; a última é sempre curta. */
function fitHint(candidates: readonly string[]): string {
  const fitting = candidates.find((c) => [...c].length <= HINT_MAX_LENGTH);
  return fitting ?? [...(candidates.at(-1) ?? 'Resposta')].slice(0, HINT_MAX_LENGTH).join('');
}

/** "a", "a ou b", "a, b ou c". */
function joinAlternatives(items: readonly string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} ou ${items.at(-1)}`;
}

function plural(count: number, singular: string, pluralForm: string): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function numberHint(comparison: Comparison, digits: number, allowFractions: boolean, allowComplex: boolean): string {
  const subject = allowComplex ? 'Número real ou complexo' : 'Número';
  let precision = '';
  if (comparison === 'sigfig') precision = ` com ${plural(digits, 'algarismo significativo', 'algarismos significativos')}`;
  if (comparison === 'decdig') precision = ` com ${plural(digits, 'casa decimal', 'casas decimais')}`;
  const noFractions = allowFractions ? '' : precision ? ', sem frações' : ' sem frações';
  const examples = ['4.25', '-17'];
  if (allowFractions) examples.push('3/7');
  if (allowComplex) examples.push('1+2j');
  if (examples.length > 3) examples.splice(1, 1);
  const head = `${subject}${precision}${noFractions}`;
  return fitHint([
    `${head}; ex.: ${joinAlternatives(examples)}`,
    head,
    `${subject}${precision}`,
    `${subject}${allowFractions ? '' : ' sem frações'}`,
    subject,
  ]);
}

function integerHint(base: number): string {
  if (base === BASE_DEFAULT) return fitHint(['Número inteiro; ex.: 42 ou -17', 'Número inteiro']);
  if (base === 0) {
    // Base 0: decimal ou os prefixos 0x, 0b e 0o (pl-integer-input.mustache, zero_base).
    return fitHint(['Número inteiro; ex.: 42, 0x2A, 0b101010 ou 0o52', 'Número inteiro']);
  }
  // Os dígitos em maiúsculas, como o np.base_repr do PL.
  const repr = (n: number): string => n.toString(base).toUpperCase();
  return fitHint([`Número inteiro na base ${base}; ex.: ${repr(42)} ou -${repr(17)}`, `Número inteiro na base ${base}`, 'Número inteiro']);
}

function textHint(ignoreCase: boolean, removeSpaces: boolean, normalizeToAscii: boolean, multiline: boolean): string {
  const subject = multiline ? 'Texto (pode ter várias linhas)' : 'Texto';
  const notes: string[] = [];
  if (ignoreCase) notes.push('não diferencia maiúsculas');
  const ignored = [removeSpaces ? 'espaços' : '', normalizeToAscii ? 'acentos' : ''].filter(Boolean);
  if (ignored.length > 0) notes.push(`${ignored.join(' e ')} ignorados`);
  // Sem caber tudo, as notas saem do fim para o começo.
  const candidates: string[] = [];
  for (let n = notes.length; n >= 0; n--) candidates.push([subject, ...notes.slice(0, n)].join('; '));
  return fitHint(candidates);
}

function multipleChoiceHint(minSelect: number, maxSelect: number): string {
  if (minSelect === maxSelect) {
    return fitHint([`Marque exatamente ${plural(minSelect, 'alternativa', 'alternativas')}`, 'Marque as alternativas']);
  }
  if (minSelect <= 0) {
    return fitHint([`Marque até ${plural(maxSelect, 'alternativa', 'alternativas')}`, 'Marque as alternativas']);
  }
  return fitHint([`Marque de ${minSelect} a ${maxSelect} alternativas`, 'Marque as alternativas']);
}

// ---------------------------------------------------------------------------------------------
// describeField
// ---------------------------------------------------------------------------------------------

/**
 * Descreve o campo de um elemento de resposta suportado. `attrs` são os atributos do elemento no
 * question.html (depois do mustache) e `optionCount` é o número de opções exibidas na variante
 * (`len(params[name])`), obrigatório no pl-checkbox e ignorado nos demais.
 */
export function describeField(tag: string, attrs: Record<string, string>, optionCount: number | null): FieldDescription {
  // O PL registra cada elemento também com "_" no lugar de "-" (freeform.js).
  const element = tag.toLowerCase().replaceAll('_', '-');
  // O peso como o PL o lê. O PL não recusa peso negativo, mas a IR exige peso ≥ 0: a conferência do
  // contrato transforma esse caso em broken/ir_contract_violation.
  const weight = getIntegerAttrib(attrs, 'weight', WEIGHT_DEFAULT);
  const allowBlank = getBooleanAttrib(attrs, 'allow-blank', ALLOW_BLANK_DEFAULT);

  switch (element) {
    case 'pl-number-input': {
      const comparison = getEnumAttrib(attrs, 'comparison', COMPARISONS, COMPARISON_DEFAULT);
      const allowFractions = getBooleanAttrib(attrs, 'allow-fractions', ALLOW_FRACTIONS_DEFAULT);
      const allowComplex = getBooleanAttrib(attrs, 'allow-complex', ALLOW_COMPLEX_DEFAULT);
      const format: Record<string, unknown> = { kind: 'number', comparison };
      // rtol/atol só valem em relabs; digits só em sigfig e decdig (grade do pl-number-input).
      let digits = DIGITS_DEFAULT;
      if (comparison === 'relabs') {
        format['rtol'] = getFloatAttrib(attrs, 'rtol', RTOL_DEFAULT);
        format['atol'] = getFloatAttrib(attrs, 'atol', ATOL_DEFAULT);
      } else {
        digits = getIntegerAttrib(attrs, 'digits', DIGITS_DEFAULT);
        format['digits'] = digits;
      }
      format['allow_fractions'] = allowFractions;
      format['allow_complex'] = allowComplex;
      return {
        type: 'number',
        format,
        constraints: { allow_blank: allowBlank },
        weight,
        hint: numberHint(comparison, digits, allowFractions, allowComplex),
      };
    }

    case 'pl-integer-input': {
      let base = getIntegerAttrib(attrs, 'base', BASE_DEFAULT);
      // O prepare recusa base fora de {0} ∪ [2, 36].
      if (base !== 0 && (base < 2 || base > 36)) base = BASE_DEFAULT;
      return {
        type: 'integer',
        format: { kind: 'integer', base },
        constraints: { allow_blank: allowBlank },
        weight,
        hint: integerHint(base),
      };
    }

    case 'pl-string-input': {
      const ignoreCase = getBooleanAttrib(attrs, 'ignore-case', IGNORE_CASE_DEFAULT);
      const removeSpaces = getBooleanAttrib(attrs, 'remove-spaces', REMOVE_SPACES_DEFAULT);
      const normalizeToAscii = getBooleanAttrib(attrs, 'normalize-to-ascii', NORMALIZE_TO_ASCII_DEFAULT);
      const multiline = getBooleanAttrib(attrs, 'multiline', MULTILINE_DEFAULT);
      // O padrão de remove-leading-trailing é "multiline or REMOVE_LEADING_TRAILING_DEFAULT".
      const removeLeadingTrailing = getBooleanAttrib(
        attrs,
        'remove-leading-trailing',
        multiline || REMOVE_LEADING_TRAILING_DEFAULT,
      );
      const regex = getEnumAttrib(attrs, 'correct-answer-format', CORRECT_ANSWER_FORMATS, 'exact') === 'regex';
      return {
        type: 'text',
        format: {
          kind: 'text',
          ignore_case: ignoreCase,
          remove_spaces: removeSpaces,
          remove_leading_trailing: removeLeadingTrailing,
          normalize_to_ascii: normalizeToAscii,
          multiline,
          regex,
        },
        constraints: { allow_blank: allowBlank },
        weight,
        hint: textHint(ignoreCase, removeSpaces, normalizeToAscii, multiline),
      };
    }

    case 'pl-multiple-choice':
      return {
        type: 'single_choice',
        format: { kind: 'single_choice' },
        constraints: { allow_blank: allowBlank },
        weight,
        hint: 'Escolha uma alternativa',
      };

    case 'pl-checkbox': {
      if (optionCount === null) {
        throw new TypeError('describeField: o pl-checkbox precisa do número de opções exibidas');
      }
      // allow-blank não muda os limites: o mínimo padrão é MIN_SELECT_DEFAULT, e o máximo, o nº de opções.
      const minSelect = optionsToSelect(attrs, 'min', MIN_SELECT_DEFAULT);
      const maxSelect = optionsToSelect(attrs, 'max', optionCount);
      return {
        type: 'multiple_choice',
        format: { kind: 'multiple_choice', partial_credit: partialCreditMode(attrs) },
        constraints: { allow_blank: allowBlank, min_select: minSelect, max_select: maxSelect },
        weight,
        hint: multipleChoiceHint(minSelect, maxSelect),
      };
    }

    default:
      throw new TypeError(`describeField: elemento sem campo de resposta suportado: ${tag}`);
  }
}
