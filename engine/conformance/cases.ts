// Casos de borda escritos à mão, por tipo de campo.
//
// Cada caso parte
// da resposta correta que o `test_once` do PL gravou e sobrescreve só os campos que interessam: no PL,
// um campo em branco vira erro de formato, e o caso deixaria de testar o que pretende. A exceção é o
// caso `vazio`, sem nenhum campo. A formatação dos números segue a do Python (`:.4f`, `:.6e`,
// `:_`, `:X`), mas isso não é crítico: o motor corrige a resposta crua que o oráculo gravou.
//
// Entre eles estão o caso `campo_desconhecido` nas 5 questões e o `virgula_em_inteiro`
// do `element/integerInput` (a vírgula decimal como erro de formato também num campo inteiro).

export interface HandmadeCase {
  /** Nome do caso, sem prefixo (ex.: `virgula_decimal`). */
  name: string;
  /** Resposta crua a submeter ao oráculo. */
  raw: Record<string, unknown>;
}

/**
 * Casos à mão para a questão `qid` (uma das 5 de referência), a partir do gabarito da variante
 * (`true_answer` do oráculo) e da resposta crua do caso `correct` do `test_once`.
 */
export function handmadeCases(
  qid: string,
  trueAnswer: Record<string, unknown>,
  correctRaw: Record<string, unknown>,
): HandmadeCase[] {
  const cases: HandmadeCase[] = [{ name: 'vazio', raw: {} }];
  const over = (name: string, fields: Record<string, unknown>): void => {
    cases.push({ name, raw: { ...correctRaw, ...fields } });
  };
  const ta = (name: string): unknown => trueAnswer[name];
  // Para as 5 questões: resposta correta com um campo que não existe na questão (campos
  // desconhecidos ou faltando são tratados como o PL trata). O oráculo grava o campo, e o motor
  // precisa corrigir igual.
  over('campo_desconhecido', { cq_campo_desconhecido: 'x' });

  if (qid === 'element/numberInput') {
    const c = toNumber(ta('c_1'));
    over('virgula_decimal', { c_1: c.toFixed(4).replace('.', ',') });
    over('fracao', { c_m: '4/2' });
    over('espacos_entre_digitos', { c_b: '5 .0' });
    over('menos_unicode_errado', { c_1: '−' + c.toFixed(4) });
    over('sigfig_limite', { c_3: c.toFixed(1) });
    over('notacao_cientifica', { c_1: pythonExponential(c, 6) });
  }
  if (qid === 'element/integerInput') {
    over('decimal_em_inteiro', { c_1: '3.5' });
    // A vírgula decimal num campo inteiro (c_1, base 10): o gabarito com ",0"
    // (ex.: "10,0"). O motor não pode ler a vírgula como separador (que daria 100) nem normalizá-la. O
    // esperado vem do oráculo: no PL a193f72, o `string_to_integer` do `pl-integer-input` falha com a
    // vírgula, e a submissão fica com `gradable` falso, `format_errors.c_1` (o template do elemento) e
    // `submitted_answer.c_1` nulo (conferido no pl-oracle com as seeds 9ix, 17we19m e grnr17).
    over('virgula_em_inteiro', { c_1: `${Math.trunc(toNumber(ta('c_1')))},0` });
    over('separador_underscore', { c_1: groupThousands(Math.trunc(toNumber(ta('c_1')))) });
    over('prefixo_hex_em_base16', { c_7_b16: '0x' + Math.trunc(toNumber(ta('c_7_b16'))).toString(16).toUpperCase() });
    over('digito_invalido_base2', { c_7_b2: '102' });
    over('hex_minusculo', { c_7_b16: Math.trunc(toNumber(ta('c_7_b16'))).toString(16) });
  }
  if (qid === 'element/stringInput') {
    over('espacos_nas_bordas', { ans1: '  ' + pythonStr(ta('ans1')) + ' ' });
    over('maiusculas_em_ignore_case', { ans6: pythonStr(ta('ans6')).toUpperCase() });
    over('remove_spaces', { ans4: Array.from(pythonStr(ta('ans4'))).join(' ') });
    over('ascii_normalizado', { ans8: '“Ee”' });
  }
  if (qid === 'element/multipleChoice') {
    over('chave_inexistente', { 'sky-color1': 'zz' });
    over('branco_com_allow_blank', { 'sky-color9': '' });
  }
  if (qid === 'element/checkbox') {
    over('uma_chave_so', { group_ans_original: 'a' });
    over('todas_as_chaves', { group_ans_original: ['a', 'b', 'c', 'd', 'e'] });
  }
  return cases;
}

function toNumber(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value);
  if (!Number.isFinite(n)) throw new Error(`gabarito numérico inválido para um caso à mão: ${JSON.stringify(value)}`);
  return n;
}

/** `str(dict.get(k, ""))` do Python, para os valores do gabarito. */
function pythonStr(value: unknown): string {
  if (value === undefined) return '';
  if (value === null) return 'None';
  if (typeof value === 'string') return value;
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') return String(value);
  return JSON.stringify(value) ?? '';
}

/** `f"{x:.<n>e}"` do Python: o expoente tem pelo menos dois dígitos (`1.081665e+01`). */
function pythonExponential(value: number, digits: number): string {
  return value.toExponential(digits).replace(/e([+-])(\d)$/, 'e$10$2');
}

/** `f"{n:_}"` do Python: milhares separados por `_`. */
function groupThousands(value: number): string {
  const digits = String(Math.abs(value));
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, '_');
  return value < 0 ? `-${grouped}` : grouped;
}
