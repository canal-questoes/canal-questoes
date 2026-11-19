// A ida e volta pelo banco que o PL faz entre as fases (por fidelidade ao PL). O PL a193f72
// grava a variante e a submissão em colunas jsonb do PostgreSQL (variants.params, true_answer,
// preferences; submissions.params, true_answer, submitted_answer, raw_submitted_answer, format_errors,
// feedback, partial_scores) e as relê antes das fases seguintes. O caminho é o do pg: JSON.stringify na
// gravação, o jsonb e o JSON.parse do pg-types na leitura, e depois o z.record do db-types.js:
// - o jsonb guarda as chaves de cada objeto, em qualquer profundidade, pelo tamanho em bytes UTF-8 e,
//   no empate, pela comparação dos bytes; o JSON.parse as insere nessa ordem, e o objeto do JS põe na
//   frente as chaves que parecem índices (dos dois lados);
// - os valores não mudam: o número volta do numeric como o mesmo double, e o que o JSON.stringify não
//   representa (undefined, -0) já sai dele como no PL;
// - o z.record(z.string(), …) de cada coluna descarta uma chave __proto__ no primeiro nível (zod 4).
//
// O que cada fase relê do banco; as demais chaves de data o PL monta em memória:
// - generate e prepare: preferences, montadas a partir de questions.preferences_schema
//   (extractDefaultPreferences); params e correct_answers passam do generate ao prepare em memória
//   (makeVariant, lib/question-variant.js);
// - render e file: a variante gravada e a submissão, se houver (extractVariantSubmissionData);
// - parse: só a variante; a resposta crua vem do formulário, em memória (saveSubmission, lib/grading.js);
// - grade: a submissão gravada depois do parse e as preferences da variante (gradeVariant, com
//   selectSubmissionForGrading);
// - test: a variante.
import type { Phase } from './check-data.ts';

type Stored = Record<string, unknown>;

const VARIANT_FIELDS = ['params', 'correct_answers', 'preferences'] as const;
const SUBMISSION_FIELDS = ['submitted_answers', 'format_errors', 'partial_scores', 'feedback', 'raw_submitted_answers'] as const;

const FROM_DATABASE: Readonly<Record<Phase, readonly string[]>> = {
  generate: ['preferences'],
  prepare: ['preferences'],
  render: [...VARIANT_FIELDS, ...SUBMISSION_FIELDS],
  file: [...VARIANT_FIELDS, ...SUBMISSION_FIELDS],
  parse: VARIANT_FIELDS,
  grade: [...VARIANT_FIELDS, ...SUBMISSION_FIELDS],
  test: VARIANT_FIELDS,
};

function isRecord(value: unknown): value is Stored {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** A ordem das chaves de um objeto jsonb: as mais curtas (em bytes UTF-8) primeiro e, no empate, pelos bytes. */
export function compareJsonbKeys(a: string, b: string): number {
  const x = Buffer.from(a, 'utf8');
  const y = Buffer.from(b, 'utf8');
  return x.length - y.length || Buffer.compare(x, y);
}

/** Uma cópia com as chaves na ordem do jsonb (Object.fromEntries cria até a chave __proto__ como dado). */
function jsonbKeyOrder(record: Stored): Stored {
  return Object.fromEntries(Object.keys(record).sort(compareJsonbKeys).map((key) => [key, record[key]]));
}

/** O valor como volta de uma coluna jsonb: JSON.stringify do pg, o jsonb e o JSON.parse do pg-types. */
export function jsonbRoundTrip<T>(value: T): T {
  const text = JSON.stringify(value, (_key, v: unknown) => (isRecord(v) ? jsonbKeyOrder(v) : v));
  // undefined (e o que não é JSON) fica como está: o pg grava null, e quem lê cai no "?? {}".
  return text === undefined ? value : (JSON.parse(text) as T);
}

/** O valor como o PL o relê do banco: o jsonb e o z.record da coluna (sem __proto__ no primeiro nível). */
export function readStored<T>(value: T): T {
  const out = jsonbRoundTrip(value);
  if (isRecord(out) && Object.hasOwn(out, '__proto__')) delete out['__proto__'];
  return out;
}

/** Uma cópia dos dados da fase, com as chaves que o PL relê do banco nessa fase como ele as relê. */
export function fromDatabase(phase: Phase, data: Stored): Stored {
  const out: Stored = { ...data };
  for (const field of FROM_DATABASE[phase]) {
    if (Object.hasOwn(data, field)) out[field] = readStored(data[field]);
  }
  return out;
}
