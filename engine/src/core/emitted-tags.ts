// As tags que os elementos suportados escrevem cru no render.
// No render, o PL não para na árvore do question.html: o traverse_and_replace (internal/traverse.py)
// passa cada texto que um elemento devolve pelo lxml.html.fragments_fromstring e processa os
// elementos dessa saída como os do HTML. As outras fases (traverse_and_execute) só percorrem a árvore.
// Os valores que um elemento suportado escreve cru ({{{…}}} no template dele, PL a193f72) entram,
// assim, na árvore do render:
// - o atributo label do pl-number-input, do pl-integer-input e do pl-string-input (todos os painéis);
// - o atributo suffix do pl-string-input multiline (o de linha única sai escapado, {{suffix}});
// - os atributos header e footer do pl-card (title e subtitle saem escapados);
// - o html das opções do pl-multiple-choice e do pl-checkbox, lido de params[answers-name] nos painéis
//   da questão e da submissão (vindo do prepare, do external-json ou do server.py), e o de
//   correct_answers[answers-name] no painel de resposta; o feedback das opções, que o grade copia
//   para partial_scores e os painéis da questão e da submissão mostram depois de uma resposta.
// No painel de resposta, o elemento que não mostra a própria resposta devolve "" e não escreve
// nada. O conteúdo do pl-card e dos elementos de painel é o inner_html deles: as tags já estão na
// árvore. O pl-code escapa o código (pygments), e o pl-figure só escreve o img. Cada painel poda a
// árvore como o PL: um pl-question-panel fora do painel da questão devolve "", e os filhos dele nunca
// são visitados (o mesmo vale para pl-answer-panel, pl-submission-panel e pl-hide-in-panel). A saída
// de um elemento é percorrida de novo, com a mesma regra, até não haver saída nova. As tags achadas
// assim são classificadas pela regra da varredura (support.ts): fora do subconjunto ou sobrescritas
// pelo curso.
// Limitações [I]: cada valor é lido sozinho, e não no meio do template do elemento; só um valor que
// termina dentro de uma tag por fechar (ex.: label="<x a=") muda de árvore com o texto que o segue. O
// painel da submissão usa o template do painel da questão: os dados de uma submissão (e o que o
// server.py põe no feedback ao corrigir) não existem na criação da variante.
import { hidesOwnAnswer } from '../ir/answer-key.ts';
import { getAttrib, getBooleanAttrib } from '../ir/fields.ts';
import { resolveElement, type ElementMap } from '../pl/elements.ts';
import { fragmentsFromString, type HtmlElementNode } from '../pl/html-fragments.ts';
import { SUPPORTED_ELEMENTS } from './support.ts';

/** Os painéis em que o PL renderiza o HTML da questão (renderPanel do freeform.js). */
export type RenderPanel = 'question' | 'submission' | 'answer';

export const ALL_PANELS: readonly RenderPanel[] = ['question', 'submission', 'answer'];

/** Os dados da variante que os elementos de escolha leem no render. */
export interface EmittedData {
  params: unknown;
  correctAnswers: unknown;
}

/**
 * Um HTML que o motor renderiza à parte, fora do percurso do PL, num painel: as opções e a tag de
 * cada campo da IR, também quando um ancestral o esconderia no PL. O código de um
 * elemento fora do subconjunto não pode rodar nem aí.
 */
export interface EngineRender {
  html: string;
  panel: RenderPanel;
}

export interface EmittedScan {
  /** Nomes canônicos de elementos fora do subconjunto na saída dos elementos, em ordem alfabética. */
  unsupported: string[];
  /** Nomes canônicos de elementos do curso com o nome de um elemento suportado, idem. */
  overridden: string[];
}

const TEXT_INPUTS: ReadonlySet<string> = new Set(['pl-number-input', 'pl-integer-input', 'pl-string-input']);
const CHOICES: ReadonlySet<string> = new Set(['pl-multiple-choice', 'pl-checkbox']);

function entryOf(container: unknown, key: string): unknown {
  if (container !== null && typeof container === 'object' && !Array.isArray(container) && Object.hasOwn(container, key)) {
    return (container as Record<string, unknown>)[key];
  }
  return undefined;
}

/** Os textos das chaves dadas de uma opção ou de uma lista de opções (o que não é texto não tem tags). */
function optionTexts(value: unknown, keys: readonly string[]): string[] {
  const list = Array.isArray(value) ? value : [value];
  return list.flatMap((option: unknown) =>
    keys.flatMap((key) => {
      const text = entryOf(option, key);
      return typeof text === 'string' ? [text] : [];
    }),
  );
}

/** Os painéis em que os filhos do elemento suportado são renderizados, dados os do próprio elemento. */
function childPanels(name: string, attrs: Record<string, string>, panels: readonly RenderPanel[]): RenderPanel[] {
  switch (name) {
    case 'pl-question-panel':
      return panels.filter((p) => p === 'question');
    case 'pl-answer-panel':
      return panels.filter((p) => p === 'answer');
    case 'pl-submission-panel':
      return panels.filter((p) => p === 'submission');
    case 'pl-hide-in-panel':
      return panels.filter((p) => !getBooleanAttrib(attrs, p, false));
    case 'pl-card':
      return [...panels];
    default:
      // Elementos de resposta, pl-code e pl-figure: o render lê os atributos e os dados, e os filhos
      // não voltam como elementos.
      return [];
  }
}

/** O HTML que o elemento suportado escreve cru no painel. */
function emittedIn(name: string, attrs: Record<string, string>, panel: RenderPanel, data: EmittedData): string[] {
  if (panel === 'answer' && hidesOwnAnswer(name, attrs)) return [];
  const out: string[] = [];
  if (TEXT_INPUTS.has(name)) {
    const label = getAttrib(attrs, 'label');
    if (label !== undefined) out.push(label);
    const suffix = getAttrib(attrs, 'suffix');
    if (name === 'pl-string-input' && suffix !== undefined && getBooleanAttrib(attrs, 'multiline', false)) out.push(suffix);
  } else if (name === 'pl-card') {
    for (const attr of ['header', 'footer']) {
      const value = getAttrib(attrs, attr);
      if (value !== undefined) out.push(value);
    }
  } else if (CHOICES.has(name)) {
    const answersName = getAttrib(attrs, 'answers-name');
    if (answersName === undefined) return out;
    if (panel === 'answer') out.push(...optionTexts(entryOf(data.correctAnswers, answersName), ['html']));
    else out.push(...optionTexts(entryOf(data.params, answersName), ['html', 'feedback']));
  }
  return out;
}

function uniqueSorted(values: Iterable<string>): string[] {
  return [...new Set(values)].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/**
 * Classifica as tags que o render do PL processaria na saída dos elementos suportados do HTML dado
 * (já passado pelo template), nos painéis dados, e as dos HTMLs que o motor renderiza à parte
 * (engineRenders), percorridos como saída de elemento. As tags da árvore do HTML ficam com
 * scanQuestionHtml.
 */
export function scanEmittedHtml(
  html: string,
  core: ElementMap,
  course: ElementMap,
  data: EmittedData,
  panels: readonly RenderPanel[] = ALL_PANELS,
  engineRenders: readonly EngineRender[] = [],
): EmittedScan {
  const unsupported: string[] = [];
  const overridden: string[] = [];
  // Cada saída é percorrida uma vez por painel: o html de uma opção pode conter o próprio elemento.
  const seen = new Set<string>();
  const pending: Array<{ nodes: HtmlElementNode[]; panels: readonly RenderPanel[]; emitted: boolean }> = [
    { nodes: fragmentsFromString(html) ?? [], panels, emitted: false },
    ...engineRenders.map((r) => ({ nodes: fragmentsFromString(r.html) ?? [], panels: [r.panel], emitted: true })),
  ];

  const emit = (name: string, attrs: Record<string, string>, nodePanels: readonly RenderPanel[]): void => {
    for (const panel of nodePanels) {
      for (const text of emittedIn(name, attrs, panel, data)) {
        const key = `${panel}\u0000${text}`;
        if (seen.has(key)) continue;
        seen.add(key);
        pending.push({ nodes: fragmentsFromString(text) ?? [], panels: [panel], emitted: true });
      }
    }
  };

  const visit = (node: HtmlElementNode, nodePanels: readonly RenderPanel[], emitted: boolean): void => {
    if (nodePanels.length === 0) return;
    const info = resolveElement(node.name, core, course);
    if (info === undefined) {
      pending.push({ nodes: node.children, panels: nodePanels, emitted });
      return;
    }
    const supported = info.type === 'core' && SUPPORTED_ELEMENTS.has(info.name);
    if (!supported) {
      // Na árvore do HTML, a varredura já o conta; na saída de um elemento, é tag nova. O que ele
      // renderiza não se sabe sem rodar o código dele.
      if (emitted) (info.type === 'course' && SUPPORTED_ELEMENTS.has(info.name) ? overridden : unsupported).push(info.name);
      return;
    }
    emit(info.name, node.attrs, nodePanels);
    pending.push({ nodes: node.children, panels: childPanels(info.name, node.attrs, nodePanels), emitted });
  };

  for (let item = pending.pop(); item !== undefined; item = pending.pop()) {
    for (const node of item.nodes) visit(node, item.panels, item.emitted);
  }
  return { unsupported: uniqueSorted(unsupported), overridden: uniqueSorted(overridden) };
}
