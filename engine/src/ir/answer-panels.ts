// Os pl-answer-panel que entram na explicação da IR.
// No PL, o render percorre o HTML de cima para baixo (traverse_and_replace do internal/traverse.py):
// quando um elemento devolve "", os filhos dele nunca são visitados. Por isso, um pl-answer-panel dentro
// de pl-question-panel ou pl-submission-panel (vazios fora do próprio painel) ou de pl-hide-in-panel com
// answer="true" nunca aparece no painel de resposta, e fica fora da explicação, como fica fora do
// gabarito o campo com um desses ancestrais (hiddenInAnswerPanel).
import type { HtmlScan } from '../core/support.ts';
import { resolveElement, type ElementMap } from '../pl/elements.ts';
import { fragmentsFromString, iterElements } from '../pl/html-fragments.ts';
import { hidesInAnswerPanel } from './answer-key.ts';

type Range = { start: number; end: number };

/** Trechos [start, end) dos elementos do mapa que o PL não renderiza no painel de resposta. */
function hidingRanges(html: string, core: ElementMap, course: ElementMap): Range[] {
  // A mesma árvore da varredura (support.ts), a do lxml do PL: os trechos são posições no mesmo
  // texto, e um trecho contém outro só se o elemento contém o outro nessa árvore.
  const out: Range[] = [];
  for (const node of iterElements(fragmentsFromString(html) ?? [])) {
    const info = resolveElement(node.name, core, course);
    if (info === undefined || !hidesInAnswerPanel({ name: info.name, type: info.type, attrs: { ...node.attrs } })) continue;
    out.push({ start: node.start, end: node.end });
  }
  return out;
}

/**
 * Os pl-answer-panel mais externos da varredura do painel de resposta que o PL mostra nesse painel: os
 * que não estão dentro de um elemento que o PL não renderiza nele. Na ordem do documento.
 */
export function shownAnswerPanels(scan: HtmlScan, html: string, core: ElementMap, course: ElementMap): Range[] {
  if (scan.answerPanels.length === 0) return [];
  const hiding = hidingRanges(html, core, course);
  return scan.answerPanels.filter((panel) => !hiding.some((h) => h.start <= panel.start && panel.end <= h.end));
}
