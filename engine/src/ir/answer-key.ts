// Gabarito e explicação da IR (com a regra do can_show).
// display_html é a renderização da própria tag do elemento no painel de resposta do PL, canonicalizada
// (o textarea do gabarito vira pre), também quando o gabarito não pode ser mostrado; vazia
// depois da canonicalização, não há forma exibível. Quando o PL não faria esse render
// (showCorrectAnswer falso ou ancestral que esconde o campo no painel de resposta) e ele falha, não há
// forma exibível, e a variante segue (ir/build.ts). A regra de variante única do PL depende da nota do
// aluno e fica com o núcleo.
import type { AnswerKeyEntry, Explanation } from '../contract/types.ts';
import type { ElementUse, FieldUse } from '../core/support.ts';
import { canonicalizeHtml, type CanonicalizeOptions } from './canonical-html.ts';
import { getBooleanAttrib } from './fields.ts';

/** O elemento de resposta não mostra a própria resposta correta: o render do painel de resposta é "". */
export function hidesOwnAnswer(element: string, attrs: Record<string, string>): boolean {
  switch (element) {
    case 'pl-number-input':
      return !getBooleanAttrib(attrs, 'show-correct-answer', true);
    case 'pl-checkbox':
      return getBooleanAttrib(attrs, 'hide-answer-panel', false);
    case 'pl-multiple-choice':
      return !getBooleanAttrib(attrs, 'builtin-grading', true);
    default:
      return false;
  }
}

/** O elemento não mostra a própria resposta correta. */
export function elementHidesAnswer(field: FieldUse): boolean {
  return hidesOwnAnswer(field.element, field.attrs);
}

/**
 * Um elemento que o PL não renderiza no painel de resposta, nem os filhos dele: pl-question-panel e
 * pl-submission-panel (vazios fora do próprio painel) e pl-hide-in-panel com answer="true".
 */
export function hidesInAnswerPanel(use: ElementUse): boolean {
  return (
    use.type === 'core' &&
    (use.name === 'pl-question-panel' ||
      use.name === 'pl-submission-panel' ||
      (use.name === 'pl-hide-in-panel' && getBooleanAttrib(use.attrs, 'answer', false)))
  );
}

/** O campo tem um ancestral que o PL não renderiza no painel de resposta. */
export function hiddenInAnswerPanel(field: FieldUse): boolean {
  return field.ancestors.some(hidesInAnswerPanel);
}

export interface AnswerKeyInput {
  field: FieldUse & { name: string };
  /**
   * O HTML do elemento renderizado no painel de resposta, ou undefined se não houve render (ou se ele
   * falhou onde o PL não o faria).
   */
  rendered: string | undefined;
  /** correct_answers[name], ou undefined se ausente. */
  correctValue: unknown;
  showCorrectAnswer: boolean;
  resolveImage: CanonicalizeOptions['resolveImage'];
}

export function answerKeyEntry(input: AnswerKeyInput): AnswerKeyEntry {
  const canonical =
    input.rendered === undefined ? '' : canonicalizeHtml(input.rendered, { resolveImage: input.resolveImage, textareaAsPre: true });
  const display = canonical.trim() === '' ? null : canonical;
  return {
    name: input.field.name,
    can_show: input.showCorrectAnswer && !elementHidesAnswer(input.field) && !hiddenInAnswerPanel(input.field) && display !== null,
    correct_value: input.correctValue === undefined ? null : input.correctValue,
    display_html: display,
  };
}

/**
 * A explicação: o conteúdo dos pl-answer-panel renderizados, sujeito a showCorrectAnswer; null sem
 * painel que o PL mostre no painel de resposta (answer-panels.ts).
 */
export function explanationOf(
  panels: ReadonlyArray<string | undefined>,
  showCorrectAnswer: boolean,
  resolveImage: CanonicalizeOptions['resolveImage'],
): Explanation | null {
  if (panels.length === 0) return null;
  const parts = panels.map((html) => canonicalizeHtml(html ?? '', { resolveImage }));
  return { html: parts.filter((p) => p.trim() !== '').join(' '), can_show: showCorrectAnswer };
}
