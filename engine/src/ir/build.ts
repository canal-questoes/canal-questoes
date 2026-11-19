// Construção da IR.
// O que o aluno vê é o que o PL renderiza (fidelidade ao PL):
// 1. o question.html passa pelo template do PL com os dados do painel (mustache + markdown), e as tags
//    dos dois painéis passam pela mesma classificação da inspeção antes de qualquer render: um
//    elemento fora do subconjunto que só aparece com os dados do render (editable, panel,
//    correct_answer_shown) dá "unsupported" sem que o código dele rode. Contam também as tags que os
//    elementos suportados escrevem cru na saída do render, que o PL processa de novo (rótulo, sufixo,
//    header e footer do pl-card, o html e o feedback das opções; core/emitted-tags.ts), também nos
//    fragmentos que o motor renderiza à parte (passo 3);
// 2. os elementos de resposta suportados viram marcadores <cq-field name="…"> (o resto do texto fica
//    byte a byte) e o HTML passa pela fase render do PL com panel "question";
// 3. os fragmentos vão em lote, cada um num <cq-fragment id="…">, que o Python do PL repassa intacto:
//    com panel "question", as opções (params[name][*].html) e o rótulo e o sufixo cru de cada campo,
//    que o PL escreve cru ao lado do campo e renderiza de novo (core/emitted-tags.ts); com panel
//    "answer", o gabarito (a tag de cada elemento) e a explicação (os pl-answer-panel que o PL mostra
//    no painel de resposta);
// 4. as imagens geradas vêm da fase file do PL, uma chamada por arquivo, cada uma num executor
//    recém-reiniciado (como a rota de arquivos gerados do PL); antes da primeira, o template de cada
//    uma (com o filename nos dados) passa pela mesma classificação, porque a fase file roda o file() de
//    todo elemento do HTML;
// 5. tudo é canonicalizado (canonical-html.ts), com as fórmulas marcadas.
// Os renders rodam num único executor, como o render() do freeform.js, que renderiza os painéis da
// questão e da resposta no mesmo withCodeCaller. O painel de resposta vai em dois lotes: o que o PL
// renderiza (showCorrectAnswer verdadeiro, question-render.ts; campo sem ancestral que o esconda nesse
// painel) e o que só o motor renderiza (tudo, com showCorrectAnswer falso; a tag solitária de um campo
// escondido). Um problema fatal no primeiro quebra a variante, como no PL; no segundo, a variante
// degrada: os campos desse lote ficam sem forma exibível (display_html null; o can_show deles já é
// falso) e, se a explicação estava nele, ela sai null. O segundo lote é a última chamada do executor,
// porque um erro no Python encerra o processo dele.
import { createHash } from 'node:crypto';
import path from 'node:path';

import type { BrokenReason, ContractPhase, Ir, IrField, IrMedia, IrOption, UnsupportedReason, Warning } from '../contract/types.ts';
import { IR_VERSION } from '../contract/types.ts';
import { scanEmittedHtml, type EmittedScan } from '../core/emitted-tags.ts';
import { brokenFromIssue, consoleWarnings, firstFatal, mergeWarnings } from '../core/outcome.ts';
import { elementReasons, scanQuestionHtml, type FieldUse, type HtmlScan } from '../core/support.ts';
import type { Executors } from '../pl/executors.ts';
import { fragmentsFromString, iterElements } from '../pl/html-fragments.ts';
import {
  contextOptions,
  execTemplate,
  processElements,
  processQuestion,
  type CourseIssue,
  type PhaseData,
  type QuestionContext,
} from '../pl/phases.ts';
import { answerKeyEntry, explanationOf, hiddenInAnswerPanel } from './answer-key.ts';
import { shownAnswerPanels } from './answer-panels.ts';
import { canonicalizeHtml, type CanonicalizeOptions } from './canonical-html.ts';
import { describeField, getAttrib, getBooleanAttrib } from './fields.ts';
import { mediaLabel } from './media-label.ts';
import { irMedia, MEDIA_URLS, MediaRegistry, readStaticMedia, type MediaEntry } from './media.ts';

export interface VariantState {
  variantSeed: number;
  params: unknown;
  correctAnswers: unknown;
  preferences: unknown;
}

export interface IrBuildInput {
  executors: Executors;
  context: QuestionContext;
  /** Diretório real da questão e raiz real do curso (leitura das mídias estáticas). */
  questionDir: string;
  courseRoot: string;
  showCorrectAnswer: boolean;
  variant: VariantState;
  questionTimeoutMs: number;
}

export type IrBuildResult =
  | { ok: true; ir: Ir; warnings: Warning[] }
  | { ok: false; reason: BrokenReason }
  /**
   * Fora do subconjunto, sem que o código das tags rode: pelos templates do render e pela saída
   * dos elementos, antes de qualquer render (phase "prepare"), ou pelos templates da fase file, depois
   * do render (phase "render").
   */
  | { ok: false; unsupported: UnsupportedReason[]; phase: ContractPhase };

type Panel = 'question' | 'answer';

/** Um campo da IR: um elemento de resposta com answers-name, fora de outro elemento de resposta. */
type NamedField = FieldUse & { name: string };

function entryOf(container: unknown, key: string): unknown {
  if (container !== null && typeof container === 'object' && Object.hasOwn(container, key)) {
    return (container as Record<string, unknown>)[key];
  }
  return undefined;
}

/** extractVariantSubmissionData do freeform.js, sem submissão (variante recém-criada: num_tries 0). */
function variantData(variant: VariantState): PhaseData {
  return {
    params: variant.params ?? {},
    correct_answers: variant.correctAnswers ?? {},
    submitted_answers: {},
    format_errors: {},
    partial_scores: {},
    score: 0,
    feedback: {},
    variant_seed: variant.variantSeed,
    preferences: variant.preferences,
    raw_submitted_answers: {},
    num_valid_submissions: 0,
  };
}

/** Os dados do renderPanel do freeform.js, com as URLs de arquivos trocadas pelos marcadores de mídia. */
function renderData(input: IrBuildInput, panel: Panel): PhaseData {
  return {
    ...variantData(input.variant),
    options: {
      client_files_question_url: MEDIA_URLS.clientFilesQuestion,
      client_files_course_url: MEDIA_URLS.clientFilesCourse,
      client_files_question_dynamic_url: MEDIA_URLS.generatedFilesQuestion,
      course_element_files_url: 'urn:cq:courseElements',
      course_element_extension_files_url: 'urn:cq:courseElementExtensions',
      submission_files_url: null,
      variant_id: 0,
      external_image_capture_url: null,
      base_url: '',
      workspace_url: null,
      ...contextOptions(input.context),
      user: null,
      group: null,
    },
    editable: true,
    manual_grading: false,
    ai_grading: false,
    panel,
    correct_answer_shown: panel === 'answer',
  };
}

/** Os dados do file() do freeform.js. */
function fileData(input: IrBuildInput, filename: string): PhaseData {
  return {
    ...variantData(input.variant),
    options: { ...contextOptions(input.context), user: null, group: null },
    filename,
  };
}

/** Troca trechos [start, end) do HTML (sem sobreposição) pelos textos dados. */
function splice(html: string, replacements: Array<{ start: number; end: number; text: string }>): string {
  let out = '';
  let cursor = 0;
  for (const r of [...replacements].sort((a, b) => a.start - b.start)) {
    if (r.start < cursor) continue;
    out += html.slice(cursor, r.start) + r.text;
    cursor = r.end;
  }
  return out + html.slice(cursor);
}

function fragmentOpen(id: string): string {
  return `<cq-fragment id="${id}">`;
}

const FRAGMENT_CLOSE = '</cq-fragment>';

function fragment(id: string, content: string): string {
  return `${fragmentOpen(id)}${content}${FRAGMENT_CLOSE}`;
}

/**
 * O conteúdo de cada <cq-fragment id> do HTML renderizado: o trecho do texto do PL entre as tags do
 * fragmento, achado na árvore do lxml (a do traverse do PL), sem reserializar. Assim a canonicalização
 * lê o conteúdo uma vez só, como lê o enunciado, e a quebra de linha inicial de pre, textarea e listing
 * fica (a reserialização do parse5 não a repõe, e a releitura descartaria mais uma). O PL escreve a tag
 * de abertura de um elemento desconhecido como get_source_definition: `<cq-fragment id="…">`.
 */
function fragmentsOf(html: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const node of iterElements(fragmentsFromString(html) ?? [])) {
    const id = node.attrs.id;
    if (node.name !== 'cq-fragment' || id === undefined || out.has(id)) continue;
    const open = fragmentOpen(id);
    const start = html.startsWith(open, node.start) ? node.start + open.length : html.indexOf('>', node.start) + 1;
    const closed = node.end >= FRAGMENT_CLOSE.length && html.startsWith(FRAGMENT_CLOSE, node.end - FRAGMENT_CLOSE.length);
    const end = closed ? node.end - FRAGMENT_CLOSE.length : node.end;
    out.set(id, start > node.start && start <= end ? html.slice(start, end) : '');
  }
  return out;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function sha256(text: string): string {
  return `sha256:${createHash('sha256').update(text, 'utf8').digest('hex')}`;
}

/** content_hash da opção: identidade estável das mídias e espaços colapsados. */
function optionHash(html: string, media: MediaRegistry): string {
  const stable = html.replace(/src="cq-media:(m[0-9]+)"/g, (match, id: string) => {
    const identity = media.identity(id);
    return identity === undefined ? match : `src="cq-media:${identity}"`;
  });
  return sha256(stable.replace(/\s+/g, ' ').trim());
}

const TEXT_INPUTS = new Set(['pl-number-input', 'pl-integer-input', 'pl-string-input']);
const CHOICES = new Set(['pl-multiple-choice', 'pl-checkbox']);

type ResolveImage = CanonicalizeOptions['resolveImage'];

/** O atributo label de um campo de texto, que o PL escreve cru ({{{label}}}) ao lado do campo, ou null. */
function rawLabel(field: FieldUse): string | null {
  if (!TEXT_INPUTS.has(field.element)) return null;
  return getAttrib(field.attrs, 'label') ?? null;
}

/** O sufixo que o PL escreve cru ({{{suffix}}}): só o do pl-string-input multiline, ou null. */
function rawSuffix(field: FieldUse): string | null {
  if (field.element !== 'pl-string-input' || !getBooleanAttrib(field.attrs, 'multiline', false)) return null;
  return getAttrib(field.attrs, 'suffix') ?? null;
}

/**
 * O rótulo: o atributo label renderizado pelo PL no painel da questão, como o PL o mostra ao
 * lado do campo (um pl-figure ou um pl-code dele vira a imagem ou o bloco de código), e canonicalizado,
 * com as imagens resolvidas para as mídias. Sem o fragmento na saída do render, o atributo só
 * canonicalizado.
 */
function labelHtml(field: FieldUse, rendered: string | undefined, resolveImage: ResolveImage): string | null {
  const label = rawLabel(field);
  return label === null ? null : canonicalizeHtml(rendered ?? label, { resolveImage });
}

/**
 * O sufixo: o de linha única o PL escreve escapado ({{suffix}}), e ele sai como texto; o do
 * pl-string-input multiline, cru, sai renderizado como o rótulo.
 */
function suffixHtml(field: FieldUse, rendered: string | undefined, resolveImage: ResolveImage): string | null {
  if (!TEXT_INPUTS.has(field.element)) return null;
  const suffix = getAttrib(field.attrs, 'suffix');
  if (suffix === undefined) return null;
  const raw = rawSuffix(field);
  return canonicalizeHtml(raw === null ? escapeHtml(suffix) : (rendered ?? raw), { resolveImage });
}

interface RenderedPanels {
  statement: string;
  /** Os fragmentos do painel da questão: opções (o<i>-<j>), rótulos (l<i>) e sufixos crus (s<i>). */
  question: Map<string, string>;
  /** Os fragmentos do painel de resposta: gabarito (k<i>) e explicação (e<j>). */
  answers: Map<string, string>;
  /** O lote do painel de resposta que só o motor renderiza falhou: os fragmentos dele não existem. */
  soloDegraded: boolean;
  issues: CourseIssue[];
}

type RenderOutcome =
  | {
      ok: true;
      rendered: RenderedPanels;
      scan: HtmlScan;
      fields: NamedField[];
      explanationPanels: number;
      /** A explicação foi no lote que só o motor renderiza (showCorrectAnswer falso). */
      explanationSolo: boolean;
    }
  | { ok: false; issue: CourseIssue }
  | { ok: false; unsupported: UnsupportedReason[] };

function uniqueSorted(values: Iterable<string>): string[] {
  return [...new Set(values)].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/**
 * A classificação da inspeção sobre os templates do render: as tags dos painéis da questão e da
 * resposta contam (o PL processa as duas), com as da saída dos elementos e as dos templates da fase
 * file, e os campos são os do painel da questão, os da IR.
 */
function renderReasons(question: HtmlScan, ...more: ReadonlyArray<HtmlScan | EmittedScan>): UnsupportedReason[] {
  return elementReasons({
    ...question,
    unsupported: uniqueSorted([...question.unsupported, ...more.flatMap((m) => m.unsupported)]),
    overridden: uniqueSorted([...question.overridden, ...more.flatMap((m) => m.overridden)]),
  });
}

/** O question.html pelo template do painel; um erro de template vira o problema fatal do PL. */
async function panelTemplate(input: IrBuildInput, data: PhaseData): Promise<{ html: string } | { issue: CourseIssue }> {
  const file = path.join(input.context.questionDir, 'question.html');
  try {
    return { html: await execTemplate(file, data) };
  } catch (err) {
    return { issue: { message: `${file}: ${err instanceof Error ? err.toString() : String(err)}`, fatal: true, kind: 'template_error', cause: err } };
  }
}

function choiceOptions(input: IrBuildInput, field: NamedField): Array<{ key: unknown; html: unknown }> {
  if (!CHOICES.has(field.element)) return [];
  const list = entryOf(input.variant.params, field.name);
  return Array.isArray(list) ? list.map((o: unknown) => ({ key: entryOf(o, 'key'), html: entryOf(o, 'html') })) : [];
}

async function renderPanels(input: IrBuildInput): Promise<RenderOutcome> {
  const questionData = renderData(input, 'question');
  const answerData = renderData(input, 'answer');
  const questionTemplate = await panelTemplate(input, questionData);
  if ('issue' in questionTemplate) return { ok: false, issue: questionTemplate.issue };
  const answerTemplate = await panelTemplate(input, answerData);
  if ('issue' in answerTemplate) return { ok: false, issue: answerTemplate.issue };

  const { context } = input;
  const { coreElements, courseElements } = context;
  const scan = scanQuestionHtml(questionTemplate.html, coreElements, courseElements);
  const answerScan = scanQuestionHtml(answerTemplate.html, coreElements, courseElements);
  const fields = scan.fields.filter((f): f is NamedField => !f.nested && f.name !== undefined);
  // O mesmo campo no HTML do painel de resposta: pela posição, ou pelo nome se o template mudou.
  const answerFields = answerScan.fields.filter((f) => !f.nested && f.name !== undefined);
  const answerMatches = fields.map((field, i) =>
    answerFields[i]?.name === field.name ? answerFields[i] : answerFields.find((f) => f.name === field.name),
  );
  // O marcador leva o nome como está: um nome com aspas, <, > ou & não chega ao render, porque a
  // checagem abaixo o recusa; um elemento sem answers-name não chega aqui, porque o prepare do PL o
  // recusa.
  const statementHtml = splice(
    questionTemplate.html,
    scan.fields
      .filter((f) => !f.nested)
      .map((f) => ({ start: f.start, end: f.end, text: f.name === undefined ? '' : `<cq-field name="${f.name}"></cq-field>` })),
  );

  // O lote do painel da questão: as opções e, campo a campo, o rótulo e o sufixo cru, que o PL escreve
  // ao lado do campo e renderiza de novo (a numeração das mídias segue a da canonicalização, em buildIr).
  let questionHtml = '';
  fields.forEach((field, i) => {
    choiceOptions(input, field).forEach((option, j) => {
      questionHtml += fragment(`o${i}-${j}`, typeof option.html === 'string' ? option.html : '');
    });
  });
  fields.forEach((field, i) => {
    const label = rawLabel(field);
    if (label !== null) questionHtml += fragment(`l${i}`, label);
    const suffix = rawSuffix(field);
    if (suffix !== null) questionHtml += fragment(`s${i}`, suffix);
  });

  // O painel de resposta em dois lotes: o que o PL renderiza (plHtml) e o que só o motor renderiza
  // (soloHtml): tudo, com showCorrectAnswer falso (o PL nunca renderiza o painel de resposta; o gabarito
  // sai pelo motor), e a tag de um campo que, no template do painel de resposta, tem um ancestral que o
  // esconde nesse painel.
  const answerSource = answerTemplate.html;
  let plHtml = '';
  let soloHtml = '';
  answerMatches.forEach((same, i) => {
    if (same === undefined) return;
    const html = fragment(`k${i}`, answerSource.slice(same.start, same.end));
    if (input.showCorrectAnswer && !hiddenInAnswerPanel(same)) plHtml += html;
    else soloHtml += html;
  });
  const explanationPanels = shownAnswerPanels(answerScan, answerSource, coreElements, courseElements);
  explanationPanels.forEach((panel, j) => {
    const html = fragment(`e${j}`, answerSource.slice(panel.start, panel.end));
    if (input.showCorrectAnswer) plHtml += html;
    else soloHtml += html;
  });

  // A saída dos elementos no render: o template da questão serve aos painéis da questão e da submissão
  // (o PL os renderiza com os mesmos params), e o da resposta, ao painel de resposta. Contam também os
  // HTMLs que o motor renderiza à parte: as opções, os rótulos e os sufixos no painel da questão; a tag
  // de cada campo e a explicação no de resposta, nos dois lotes.
  const emittedData = { params: input.variant.params, correctAnswers: input.variant.correctAnswers };
  const questionEmitted = scanEmittedHtml(questionTemplate.html, coreElements, courseElements, emittedData, ['question', 'submission'], [
    { html: questionHtml, panel: 'question' },
  ]);
  const answerEmitted = scanEmittedHtml(answerSource, coreElements, courseElements, emittedData, ['answer'], [
    { html: plHtml, panel: 'answer' },
    { html: soloHtml, panel: 'answer' },
  ]);
  // Antes de qualquer render: um elemento fora do subconjunto que só aparece com os dados do render, ou
  // na saída de um elemento suportado, não roda, e os nomes dos campos seguem a mesma regra da inspeção.
  const reasons = renderReasons(scan, answerScan, questionEmitted, answerEmitted);
  if (reasons.length > 0) return { ok: false, unsupported: reasons };

  const rendered = await input.executors.withCodeCaller(context.courseDir, async (codeCaller): Promise<RenderedPanels> => {
    const issues: CourseIssue[] = [];
    const out: RenderedPanels = { issues, statement: '', question: new Map(), answers: new Map(), soloDegraded: false };
    const statement = await processElements('render', codeCaller, questionData, context, statementHtml);
    issues.push(...statement.courseIssues);
    if (firstFatal(issues)) return out;
    if (questionHtml !== '') {
      const result = await processElements('render', codeCaller, questionData, context, questionHtml);
      issues.push(...result.courseIssues);
      if (firstFatal(issues)) return out;
      out.question = fragmentsOf(result.html);
    }
    if (plHtml !== '') {
      const result = await processElements('render', codeCaller, answerData, context, plHtml);
      issues.push(...result.courseIssues);
      if (firstFatal(issues)) return out;
      out.answers = fragmentsOf(result.html);
    }
    out.statement = statement.html;
    if (soloHtml !== '') {
      // A última chamada: o problema fatal (e a saída que veio com ele) fica de fora, porque o PL não
      // faria esse render; sem ele, a saída no console vira aviso, como nos outros lotes.
      const result = await processElements('render', codeCaller, answerData, context, soloHtml);
      if (firstFatal(result.courseIssues) === undefined) {
        issues.push(...result.courseIssues);
        for (const [id, html] of fragmentsOf(result.html)) out.answers.set(id, html);
      } else {
        out.soloDegraded = true;
      }
    }
    return out;
  });
  const fatal = firstFatal(rendered.issues);
  if (fatal !== undefined) return { ok: false, issue: fatal };
  return { ok: true, rendered, scan, fields, explanationPanels: explanationPanels.length, explanationSolo: !input.showCorrectAnswer };
}

/**
 * A classificação do template da fase file de cada mídia gerada, antes da primeira chamada: a
 * fase file do PL roda o file() de todo elemento do HTML (traverse_and_execute), e um elemento que só
 * aparece com o filename nos dados ({{#filename}}…{{/filename}}) rodaria. Um erro de template não dá
 * motivo aqui: ele quebra a fase file dessa mídia, como no PL.
 */
async function fileReasons(input: IrBuildInput, registry: MediaRegistry, scan: HtmlScan): Promise<UnsupportedReason[]> {
  const { context } = input;
  const scans: HtmlScan[] = [];
  for (const entry of registry.entries) {
    if (entry.source.kind !== 'dynamic') continue;
    let html: string;
    try {
      html = await execTemplate(path.join(context.questionDir, 'question.html'), fileData(input, entry.source.filename));
    } catch {
      continue;
    }
    scans.push(scanQuestionHtml(html, context.coreElements, context.courseElements));
  }
  return scans.length === 0 ? [] : renderReasons(scan, ...scans);
}

/**
 * O conteúdo de uma mídia gerada: a fase file do PL, num executor só para ela. A saída no console
 * vira aviso também quando a mídia fica ausente: a variante conclui, e o PL registra a saída da fase
 * file como problema não fatal mesmo com o retorno vazio (freeform.js, file()). Quando o file() lança,
 * a saída vai no erro, e não há aviso console_output.
 */
async function generatedMedia(input: IrBuildInput, entry: MediaEntry, warnings: Warning[]): Promise<Buffer | null> {
  if (entry.source.kind !== 'dynamic') return null;
  const filename = entry.source.filename;
  const result = await input.executors.withCodeCaller(input.context.courseDir, (codeCaller) =>
    processQuestion('file', codeCaller, fileData(input, filename), input.context),
  );
  mergeWarnings(warnings, consoleWarnings(result.courseIssues, 'file'));
  if (firstFatal(result.courseIssues) !== undefined || result.fileData.length === 0) {
    warnings.push({
      code: 'media_error',
      message: `Não foi possível gerar a imagem "${mediaLabel(entry.filename)}" da questão; a questão segue sem ela.`,
      phase: 'file',
    });
    return null;
  }
  return result.fileData;
}

async function loadMedia(input: IrBuildInput, registry: MediaRegistry, warnings: Warning[]): Promise<IrMedia[]> {
  const out: IrMedia[] = [];
  for (const entry of registry.entries) {
    let bytes: Buffer | null;
    if (entry.source.kind === 'other') {
      // Um src fora dos arquivos da questão e do curso (URL externa, data:, outro caminho): o aluno do PL
      // vê a imagem, e o motor, sem rede, não a obtém (a mídia fica ausente, com aviso).
      bytes = null;
      warnings.push({
        code: 'missing_media',
        message: `A imagem "${mediaLabel(entry.filename)}" da questão não vem dos arquivos da questão nem do curso e não pôde ser obtida; a questão segue sem ela.`,
        phase: 'render',
      });
    } else if (entry.source.kind === 'static') {
      bytes = await readStaticMedia(entry.source.baseDir, entry.source.relative);
      if (bytes === null) {
        warnings.push({
          code: 'missing_media',
          message: `A imagem "${mediaLabel(entry.filename)}" da questão não foi encontrada; a questão segue sem ela.`,
          phase: 'render',
        });
      }
    } else {
      bytes = await generatedMedia(input, entry, warnings);
    }
    const item = irMedia(entry, bytes);
    // Com conteúdo e ainda assim ausente: a mídia passou de um teto de tamanho da IR (media.ts) e fica
    // de fora, com aviso.
    if (bytes !== null && item.status === 'missing') {
      warnings.push({
        code: 'missing_media',
        message: `A imagem "${mediaLabel(entry.filename)}" da questão passa do tamanho que o motor entrega; a questão segue sem ela.`,
        phase: entry.origin === 'dynamic' ? 'file' : 'render',
      });
    }
    out.push(item);
  }
  return out;
}

/**
 * Monta a IR da variante. Uma tag fora do subconjunto nos templates do render ou na saída dos elementos
 * vira unsupported antes de qualquer render, e nos templates da fase file, antes de qualquer file(); uma
 * quebra no render que o PL faria vira broken (fase render), e uma no render que só o motor faz degrada
 * o gabarito e a explicação; mídias faltando, aviso.
 */
export async function buildIr(input: IrBuildInput): Promise<IrBuildResult> {
  const outcome = await renderPanels(input);
  if (!outcome.ok) {
    if ('unsupported' in outcome) return { ok: false, unsupported: outcome.unsupported, phase: 'prepare' };
    return { ok: false, reason: brokenFromIssue(outcome.issue, 'render', input.questionTimeoutMs) };
  }
  const { rendered, fields } = outcome;
  const warnings: Warning[] = [];
  mergeWarnings(warnings, consoleWarnings(rendered.issues, 'render'));

  // A numeração das mídias segue a ordem de canonicalização: enunciado, opções, gabarito, explicação e,
  // por último, rótulos e sufixos (campo a campo, o rótulo antes do sufixo). Uma imagem do rótulo que o
  // gabarito também mostra reusa o id que ganhou lá.
  const media = new MediaRegistry({ questionDir: input.questionDir, courseRoot: input.courseRoot });
  const statement = canonicalizeHtml(rendered.statement, { resolveImage: media.resolve });

  const irFields: IrField[] = fields.map((field, i) => {
    const choices = choiceOptions(input, field);
    const optionCount = CHOICES.has(field.element) ? choices.length : null;
    const description = describeField(field.element, field.attrs, optionCount);
    let options: IrOption[] | null = null;
    if (CHOICES.has(field.element)) {
      options = choices.map((choice, j) => {
        const html = canonicalizeHtml(rendered.question.get(`o${i}-${j}`) ?? '', { resolveImage: media.resolve });
        return { key: typeof choice.key === 'string' ? choice.key : String(choice.key), html, content_hash: optionHash(html, media) };
      });
    }
    return {
      name: field.name,
      type: description.type,
      // Preenchidos depois da explicação (numeração das mídias).
      label_html: null,
      suffix_html: null,
      format: description.format as unknown as IrField['format'],
      hint: description.hint,
      constraints: description.constraints as unknown as IrField['constraints'],
      weight: description.weight,
      options,
    };
  });

  const answerKey = fields.map((field, i) =>
    answerKeyEntry({
      field,
      rendered: rendered.answers.get(`k${i}`),
      correctValue: entryOf(input.variant.correctAnswers, field.name),
      showCorrectAnswer: input.showCorrectAnswer,
      resolveImage: media.resolve,
    }),
  );
  // No lote que só o motor renderiza e com ele degradado, o gabarito de cada campo já sai sem forma
  // exibível (sem fragmento), e a explicação, null.
  const explanation =
    outcome.explanationSolo && rendered.soloDegraded
      ? null
      : explanationOf(
          Array.from({ length: outcome.explanationPanels }, (_panel, j) => rendered.answers.get(`e${j}`)),
          input.showCorrectAnswer,
          media.resolve,
        );
  irFields.forEach((irField, i) => {
    irField.label_html = labelHtml(fields[i], rendered.question.get(`l${i}`), media.resolve);
    irField.suffix_html = suffixHtml(fields[i], rendered.question.get(`s${i}`), media.resolve);
  });

  // O render já rodou: a última fase executada é a render.
  const fileUnsupported = await fileReasons(input, media, outcome.scan);
  if (fileUnsupported.length > 0) return { ok: false, unsupported: fileUnsupported, phase: 'render' };
  const irMediaList = await loadMedia(input, media, warnings);
  const ir: Ir = {
    ir_version: IR_VERSION,
    statement: { html: statement },
    fields: irFields,
    answer_key: answerKey,
    explanation,
    media: irMediaList,
  };
  return { ok: true, ir, warnings };
}
