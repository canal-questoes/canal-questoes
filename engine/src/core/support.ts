// Veredito de suporte.
// A varredura olha as tags que a questão USA: cada elemento que o PL processaria, isto é, cada tag da
// árvore que o lxml do PL monta (lxml.html.fragments_fromstring, percorrida por element.iter() no
// traverse.py) presente no mapa de elementos do PL (core, com os nomes alternativos, e todo elemento do
// curso). A árvore é a do libxml2 da imagem (pl/html-fragments.ts), inclusive dentro de <noscript>,
// <select>, <table> e <svg>, e com "/>" fechando qualquer elemento. A mesma varredura serve à inspeção
// (question.html cru) e à checagem dinâmica da criação da variante (HTML depois do template).
import type { UnsupportedReason } from '../contract/types.ts';
import { getAttrib } from '../ir/fields.ts';
import { resolveElement, type CourseElementProblem, type ElementMap, type ElementType } from '../pl/elements.ts';
import { fragmentsFromString, iterElements, type HtmlElementNode } from '../pl/html-fragments.ts';
import { bindsRender } from './server-render.ts';

/** Elementos de resposta suportados. */
export const ANSWER_ELEMENTS = [
  'pl-multiple-choice',
  'pl-checkbox',
  'pl-number-input',
  'pl-integer-input',
  'pl-string-input',
] as const;
export type AnswerElement = (typeof ANSWER_ELEMENTS)[number];

/**
 * O conjunto suportado: os elementos de resposta, o auxiliar pl-answer, os de painel e os de
 * exibição. O pl-answer não é elemento core no PL a193f72 (não há elements/pl-answer; pl-multiple-choice
 * e pl-checkbox leem as tags filhas), e por isso só conta para um elemento do curso com esse nome, que
 * é sobrescrita (course_element_override), e não elemento fora do subconjunto.
 */
export const SUPPORTED_ELEMENTS: ReadonlySet<string> = new Set<string>([
  ...ANSWER_ELEMENTS,
  'pl-answer',
  'pl-question-panel',
  'pl-answer-panel',
  'pl-submission-panel',
  'pl-hide-in-panel',
  'pl-card',
  'pl-code',
  'pl-figure',
]);

const ANSWER_SET: ReadonlySet<string> = new Set(ANSWER_ELEMENTS);

/** O formato de answers_name da IR (ir.schema.json, $defs.answers_name). */
const ANSWERS_NAME_PATTERN = /^[^\s"<>&]+$/;
const ANSWERS_NAME_MAX_LENGTH = 200;

/** Um elemento do mapa usado pelo HTML. */
export interface ElementUse {
  /** Nome canônico: o diretório do elemento. */
  name: string;
  type: ElementType;
  attrs: Record<string, string>;
}

/** Um elemento de resposta suportado (core), na ordem do documento. */
export interface FieldUse {
  element: AnswerElement;
  /** O answers-name (decodificado), ou undefined se o elemento não o tem. */
  name: string | undefined;
  attrs: Record<string, string>;
  /** Os elementos do mapa que o contêm, do mais externo para o mais interno. */
  ancestors: ElementUse[];
  /** Trecho [start, end) do elemento no HTML varrido. */
  start: number;
  end: number;
  /** Se está dentro de outro elemento de resposta suportado. */
  nested: boolean;
}

export interface HtmlScan {
  /** Nomes canônicos dos elementos usados, sem repetição, em ordem alfabética. */
  elements: string[];
  /** Elementos de resposta suportados, na ordem do documento. */
  fields: FieldUse[];
  /** Nomes canônicos de elementos fora do subconjunto (core não suportado ou do curso com outro nome). */
  unsupported: string[];
  /** Nomes canônicos de elementos do curso com o nome de um elemento suportado. */
  overridden: string[];
  /** Trechos [start, end) dos pl-answer-panel mais externos, na ordem do documento. */
  answerPanels: Array<{ start: number; end: number }>;
}

function uniqueSorted(values: Iterable<string>): string[] {
  return [...new Set(values)].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** Varre o HTML e classifica as tags pelo mapa de elementos (core + curso). */
export function scanQuestionHtml(html: string, core: ElementMap, course: ElementMap): HtmlScan {
  // null: o lxml falharia (documento sem body ou com mais de um), e o PL não processaria elemento algum.
  const fragments = fragmentsFromString(html) ?? [];
  const used: string[] = [];
  const unsupported: string[] = [];
  const overridden: string[] = [];
  const fields: FieldUse[] = [];
  const answerPanels: Array<{ start: number; end: number }> = [];
  const uses = new Map<HtmlElementNode, ElementUse>();

  const ancestorsOf = (el: HtmlElementNode): ElementUse[] => {
    const out: ElementUse[] = [];
    for (let node = el.parent; node !== null; node = node.parent) {
      const use = uses.get(node);
      if (use !== undefined) out.unshift(use);
    }
    return out;
  };

  // element.iter() percorre em pré-ordem: os ancestrais já estão em `uses`.
  for (const node of iterElements(fragments)) {
    const info = resolveElement(node.name, core, course);
    if (info === undefined) continue;
    const use: ElementUse = { name: info.name, type: info.type, attrs: { ...node.attrs } };
    uses.set(node, use);
    used.push(info.name);
    if (info.type === 'course') {
      (SUPPORTED_ELEMENTS.has(info.name) ? overridden : unsupported).push(info.name);
      continue;
    }
    if (!SUPPORTED_ELEMENTS.has(info.name)) {
      unsupported.push(info.name);
      continue;
    }
    const ancestors = ancestorsOf(node);
    if (ANSWER_SET.has(info.name)) {
      fields.push({
        element: info.name as AnswerElement,
        name: getAttrib(use.attrs, 'answers-name'),
        attrs: use.attrs,
        ancestors,
        start: node.start,
        end: node.end,
        nested: ancestors.some((a) => a.type === 'core' && ANSWER_SET.has(a.name)),
      });
    } else if (info.name === 'pl-answer-panel' && !ancestors.some((a) => a.name === 'pl-answer-panel')) {
      answerPanels.push({ start: node.start, end: node.end });
    }
  }

  return {
    elements: uniqueSorted(used),
    fields,
    unsupported: uniqueSorted(unsupported),
    overridden: uniqueSorted(overridden),
    answerPanels,
  };
}

/** answers-name que a IR representa (ir.schema.json, $defs.answers_name). */
export function isRepresentableAnswersName(name: string): boolean {
  return name.length >= 1 && [...name].length <= ANSWERS_NAME_MAX_LENGTH && ANSWERS_NAME_PATTERN.test(name);
}

/** Os answers-name dos elementos de resposta suportados, na ordem do documento. */
export function answersNames(scan: HtmlScan): string[] {
  return scan.fields.flatMap((f) => (f.name === undefined ? [] : [f.name]));
}

function list(names: readonly string[]): string {
  return names.join(', ');
}

/** Os motivos que dependem das tags usadas (mesma regra na inspeção e na checagem dinâmica). */
export function elementReasons(scan: HtmlScan): UnsupportedReason[] {
  const reasons: UnsupportedReason[] = [];
  if (scan.unsupported.length > 0) {
    const plural = scan.unsupported.length > 1;
    reasons.push({
      code: 'unsupported_elements',
      message: `A questão usa ${plural ? 'os elementos' : 'o elemento'} ${list(scan.unsupported)}, fora do subconjunto suportado.`,
      elements: [...scan.unsupported],
    });
  }
  if (scan.overridden.length > 0) {
    const plural = scan.overridden.length > 1;
    reasons.push({
      code: 'course_element_override',
      message: `O curso redefine ${plural ? 'os elementos' : 'o elemento'} ${list(scan.overridden)} usado${plural ? 's' : ''} pela questão; só as versões do PrairieLearn são suportadas.`,
      elements: [...scan.overridden],
    });
  }
  if (scan.fields.length === 0) {
    reasons.push({
      code: 'no_answer_fields',
      message: 'A questão não tem nenhum campo de resposta suportado (múltipla escolha, caixas de marcar, número, inteiro ou texto).',
    });
  }
  const invalid = answersNames(scan).filter((name) => !isRepresentableAnswersName(name));
  if (invalid.length > 0) {
    reasons.push({
      code: 'invalid_answers_name',
      message: `A questão tem ${invalid.length > 1 ? 'nomes de campo' : 'um nome de campo'} (answers-name) com espaço, aspas, <, > ou &, que a representação da questão não aceita: ${invalid.map((n) => JSON.stringify(n)).join(', ')}.`,
    });
  }
  return reasons;
}

/**
 * A questão define render no server.py, pela regra de core/server-render.ts:
 * - `def render(` no texto cru, em qualquer lugar;
 * - `def render(` ou `def render[` (PEP 695), com async ou não, em qualquer lugar do código, também com
 *   continuação de linha;
 * - no nível do módulo, uma ligação do nome render por class, atribuição, :=, alvo de for ou de with,
 *   captura de case ou from … import;
 * - `global render` em qualquer lugar.
 */
export function definesCustomRender(serverPy: string | null): boolean {
  return serverPy !== null && (/\bdef\s+render\s*\(/.test(serverPy) || bindsRender(serverPy));
}

/**
 * Os elementos do curso não carregam no PL (loadElements ou loadExtensions lançam), e nenhuma questão
 * do curso roda nele. `elements` traz os elementos do curso cujo info.json o PL recusa.
 */
export function courseElementsReason(problems: readonly CourseElementProblem[]): UnsupportedReason {
  const paths = problems.map((p) => p.path);
  const elements = uniqueSorted(problems.flatMap((p) => (p.element === undefined ? [] : [p.element])));
  return {
    code: 'unsupported_elements',
    message: `Os elementos do curso não carregam no PrairieLearn, e nenhuma questão do curso roda nele: ${list(paths)} ${paths.length > 1 ? 'estão' : 'está'} fora do formato do PrairieLearn (info.json que não é JSON ou fora do esquema, ou arquivo no lugar de um diretório).`,
    ...(elements.length > 0 ? { elements } : {}),
  };
}

/** Motivos que vêm do info.json válido. */
export function metadataReasons(info: Record<string, unknown>): UnsupportedReason[] {
  const reasons: UnsupportedReason[] = [];
  if (info.type !== 'v3') {
    reasons.push({
      code: 'question_type_not_v3',
      message: `A questão é do tipo ${JSON.stringify(info.type)}; só questões do tipo "v3" são suportadas.`,
    });
  }
  if (info.gradingMethod === 'External' || info.gradingMethod === 'Manual') {
    reasons.push({
      code: 'grading_method_not_internal',
      message: `A questão usa correção ${info.gradingMethod === 'External' ? 'externa' : 'manual'}; só a correção interna do PrairieLearn é suportada.`,
    });
  }
  if (info.externalGradingOptions !== undefined) {
    reasons.push({
      code: 'external_grading',
      message: 'A questão tem opções de correção externa (externalGradingOptions), fora do subconjunto suportado.',
    });
  }
  if (info.workspaceOptions !== undefined) {
    reasons.push({ code: 'workspace', message: 'A questão usa um workspace, fora do subconjunto suportado.' });
  }
  return reasons;
}

export const INVALID_METADATA_REASON: UnsupportedReason = {
  code: 'invalid_question_metadata',
  message: 'Os metadados da questão (info.json) não são JSON válido ou estão fora do formato do PrairieLearn.',
};

export const CUSTOM_RENDER_REASON: UnsupportedReason = {
  code: 'custom_server_render',
  message:
    'O server.py da questão define uma função render própria (ou liga o nome render a outro objeto, por class, atribuição, import ou global), que substituiria o HTML da questão.',
};
