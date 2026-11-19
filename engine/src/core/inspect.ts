// Inspeção: metadados, elementos, campos, hash e veredito, só com
// leitura de arquivos, sem executar código da questão.
import type { InspectResponse, QuestionMetadata, UnsupportedReason } from '../contract/types.ts';
import { TEXT_FILE_MAX_BYTES } from '../pl/text-file.ts';
import { contentHash } from './content-hash.ts';
import type { LoadedQuestion } from './question.ts';
import {
  answersNames,
  courseElementsReason,
  CUSTOM_RENDER_REASON,
  definesCustomRender,
  elementReasons,
  INVALID_METADATA_REASON,
  metadataReasons,
  scanQuestionHtml,
  type HtmlScan,
} from './support.ts';

export interface StaticVerdict {
  scan: HtmlScan;
  reasons: UnsupportedReason[];
}

const LIMIT_TEXT = `${TEXT_FILE_MAX_BYTES / (1024 * 1024)} MiB`;

/**
 * Os arquivos da questão que existem mas o motor não lê: não são arquivo regular depois de
 * seguir os links ou passam do limite. Sem ler um deles, o motor não confere a regra que depende dele,
 * e a questão fica não suportada pelo código dessa regra, sem executar nada.
 */
const UNREADABLE_INFO_REASON: UnsupportedReason = {
  code: 'invalid_question_metadata',
  message: `Os metadados da questão (info.json) não são lidos: o arquivo não é um arquivo regular ou passa de ${LIMIT_TEXT}.`,
};
const UNREADABLE_HTML_REASON: UnsupportedReason = {
  code: 'no_answer_fields',
  message: `O question.html da questão não é lido, porque não é um arquivo regular ou passa de ${LIMIT_TEXT}; sem ele, a questão não tem campo de resposta suportado.`,
};
const UNREADABLE_SERVER_REASON: UnsupportedReason = {
  code: 'custom_server_render',
  message: `O server.py da questão não é lido, porque não é um arquivo regular ou passa de ${LIMIT_TEXT}; sem lê-lo, não há como conferir se ele define uma função render própria.`,
};

/**
 * O veredito estático: metadados, tags usadas e server.py. Com o info.json inválido ou ilegível, só
 * invalid_question_metadata. Com elementos do curso que o PL recusa, só esse motivo: o PL não
 * carrega o curso (loadElements e loadExtensions lançam no getContext de toda fase), e nada da questão
 * chegaria a rodar.
 */
export function staticVerdict(question: LoadedQuestion): StaticVerdict {
  const scan = scanQuestionHtml(question.html, question.core, question.course);
  const unreadable = new Set(question.unreadable);
  if (unreadable.has('info.json')) return { scan, reasons: [UNREADABLE_INFO_REASON] };
  if (!question.info.valid || question.info.data === null) return { scan, reasons: [INVALID_METADATA_REASON] };
  if (question.courseProblems.length > 0) return { scan, reasons: [courseElementsReason(question.courseProblems)] };
  const reasons: UnsupportedReason[] = [
    ...metadataReasons(question.info.data),
    ...(unreadable.has('question.html') ? [UNREADABLE_HTML_REASON] : elementReasons(scan)),
  ];
  if (unreadable.has('server.py')) reasons.push(UNREADABLE_SERVER_REASON);
  else if (definesCustomRender(question.serverPy)) reasons.push(CUSTOM_RENDER_REASON);
  return { scan, reasons };
}

/** O formato de question.elements no contrato (engine-api.schema.json, $defs.question_metadata). */
const CONTRACT_ELEMENT_NAME = /^[a-z][a-z0-9_-]*$/;

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function booleanOr(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

/**
 * Os metadados da inspeção. Com info.json inválido, os tirados dele saem null, e os que têm
 * padrão levam o padrão; com type null, partial_credit é false. Um elemento do curso com nome
 * fora do formato do contrato (ex.: "x.y", que o lxml do PL lê como tag) fica fora de elements e só
 * aparece em reasons[].elements: a questão é "não suportada" e a resposta segue o contrato, sem 500.
 */
export function questionMetadata(question: LoadedQuestion, scan: HtmlScan, hash: string): QuestionMetadata {
  const info = question.info.valid && question.info.data !== null ? question.info.data : {};
  const type = stringOrNull(info.type);
  const tags = Array.isArray(info.tags) ? info.tags.filter((t): t is string => typeof t === 'string') : [];
  return {
    uuid: stringOrNull(info.uuid),
    title: stringOrNull(info.title),
    topic: stringOrNull(info.topic),
    tags,
    type,
    grading_method: typeof info.gradingMethod === 'string' ? info.gradingMethod : 'Internal',
    elements: scan.elements.filter((name) => CONTRACT_ELEMENT_NAME.test(name)),
    show_correct_answer: booleanOr(info.showCorrectAnswer, true),
    single_variant: booleanOr(info.singleVariant, false),
    partial_credit: booleanOr(info.partialCredit, type === 'v3'),
    answers_names: answersNames(scan),
    content_hash: hash,
  };
}

export async function inspectQuestion(question: LoadedQuestion): Promise<InspectResponse> {
  const verdict = staticVerdict(question);
  const hash = await contentHash(question.questionDir);
  return {
    outcome: verdict.reasons.length === 0 ? 'supported' : 'unsupported',
    question: questionMetadata(question, verdict.scan, hash),
    reasons: verdict.reasons,
  };
}
