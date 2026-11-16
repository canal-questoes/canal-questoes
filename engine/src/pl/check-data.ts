// checkData: espelho literal da função homônima de
// /PrairieLearn/apps/prairielearn/dist/question-servers/freeform.js (PL a193f72), com a mesma tabela,
// a mesma ordem de verificação, as mesmas mensagens e o mesmo isEqual/difference do es-toolkit que o
// PL importa. Qualquer mudança aqui é divergência do PL.
import { difference, isEqual } from 'es-toolkit';

export type Phase = 'generate' | 'prepare' | 'render' | 'parse' | 'grade' | 'test' | 'file';

type PropType = 'integer' | 'number' | 'string' | 'boolean' | 'object';

const ALL_PHASES: readonly string[] = ['generate', 'prepare', 'render', 'parse', 'grade', 'test', 'file'];

/**
 * Confere as chaves de `data` para a fase: presença, tipo, alteração proibida em relação a `origData`
 * e chaves extras. Devolve a mensagem do PL ou null.
 */
export function checkData(
  data: Record<string, unknown>,
  origData: Record<string, unknown>,
  phase: Phase,
): string | null {
  const checked: string[] = [];
  const hasOwn = (obj: Record<string, unknown>, prop: string): boolean =>
    Object.prototype.hasOwnProperty.call(obj, prop);

  const checkProp = (
    prop: string,
    type: PropType,
    presentPhases: readonly string[],
    editPhases: readonly string[],
  ): string | null => {
    if (!presentPhases.includes(phase)) return null;
    if (!hasOwn(data, prop)) {
      return `"${prop}" is missing from "data"`;
    }
    const value = data[prop];
    switch (type) {
      case 'integer':
        if (!Number.isInteger(value)) {
          return `data.${prop} is not an integer: ${String(value)}`;
        }
        break;
      case 'number':
        if (!Number.isFinite(value)) {
          return `data.${prop} is not a number: ${String(value)}`;
        }
        break;
      case 'string':
        if (typeof value !== 'string') {
          return `data.${prop} is not a string: ${String(value)}`;
        }
        break;
      case 'boolean':
        if (value !== true && value !== false) {
          return `data.${prop} is not a boolean: ${String(value)}`;
        }
        break;
      case 'object':
        // Como no PL: listas passam (typeof [] === 'object'); null não.
        if (value == null || typeof value !== 'object') {
          return `data.${prop} is not an object: ${String(value)}`;
        }
        break;
      default:
        return `invalid type: ${String(type)}`;
    }
    if (!editPhases.includes(phase)) {
      if (!hasOwn(origData, prop)) {
        return `"${prop}" is missing from "origData"`;
      }
      if (!isEqual(value, origData[prop])) {
        return `data.${prop} has been illegally modified, new value: "${JSON.stringify(value)}", original value: "${JSON.stringify(origData[prop])}"`;
      }
    }
    checked.push(prop);
    return null;
  };

  if (!ALL_PHASES.includes(phase)) return `unknown phase: ${phase}`;

  // Tabela na mesma ordem do freeform.js (a primeira falha vence).
  // prettier-ignore
  const err =
       checkProp('params',                'object',  ALL_PHASES,                                    ['generate', 'prepare', 'parse', 'grade'])
    || checkProp('correct_answers',       'object',  ALL_PHASES,                                    ['generate', 'prepare', 'parse', 'grade'])
    || checkProp('variant_seed',          'integer', ALL_PHASES,                                    [])
    || checkProp('options',               'object',  ALL_PHASES,                                    [])
    || checkProp('preferences',           'object',  ALL_PHASES,                                    [])
    || checkProp('submitted_answers',     'object',  ['render', 'parse', 'grade', 'file'],          ['parse', 'grade'])
    || checkProp('format_errors',         'object',  ['render', 'parse', 'grade', 'test', 'file'],  ['parse', 'grade', 'test'])
    || checkProp('raw_submitted_answers', 'object',  ['render', 'parse', 'grade', 'test', 'file'],  ['test'])
    || checkProp('partial_scores',        'object',  ['render', 'grade', 'test', 'file'],           ['grade', 'test'])
    || checkProp('score',                 'number',  ['render', 'grade', 'test', 'file'],           ['grade', 'test'])
    || checkProp('feedback',              'object',  ['render', 'parse', 'grade', 'test', 'file'],  ['grade', 'parse', 'test'])
    || checkProp('editable',              'boolean', ['render'],                                    [])
    || checkProp('manual_grading',        'boolean', ['render'],                                    [])
    || checkProp('ai_grading',            'boolean', ['render'],                                    [])
    || checkProp('panel',                 'string',  ['render'],                                    [])
    || checkProp('correct_answer_shown',  'boolean', ['render'],                                    [])
    || checkProp('num_valid_submissions', 'integer', ['render', 'file'],                            [])
    || checkProp('gradable',              'boolean', ['parse', 'grade', 'test'],                    [])
    || checkProp('filename',              'string',  ['file'],                                      [])
    || checkProp('test_type',             'string',  ['test'],                                      [])
    || checkProp('answers_names',         'object',  ['prepare'],                                   ['prepare']);
  if (err) return err;

  const extraProps = difference(Object.keys(data), checked);
  if (extraProps.length > 0) return `"data" has invalid extra keys: ${extraProps.join(', ')}`;
  return null;
}
