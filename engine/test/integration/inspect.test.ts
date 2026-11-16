// Teste de integração da inspeção e do veredito de suporte.
// Cobre: a inspeção, o veredito de suporte e os metadados, e a checagem dinâmica
// na criação da variante. Todas as respostas são validadas contra os $defs.
// Fixtures: cq-test-course/questions/unsupported/* e cq-override-course/, mais as
// unsupported/inspect-* (nome de additionalNames, externalGradingOptions sozinho e
// tags fora do formato). Também: supported/inspect-underscore-alias (elemento de
// resposta suportado pelo nome alternativo com _) e unsupported/inspect-dynamic-* (answers-name que
// só fica inválido depois do template e único campo numa seção do template que não é exibida).
// E ainda: as supported/ir-* e broken/grade-parse-timeout entram na
// varredura; cq-override-course tem elements/pl-card (sobrescrita de um elemento que não é de
// resposta, cujo controlador lança), elements/pl-code sem info.json (não é elemento) e as questões
// inspect-* (pl-card do curso, alias com _ do core, pl-code core, comentários HTML e elementos do curso
// injetados pelo template); unsupported/inspect-uppercase-tag (tags em maiúsculas). As mensagens dos
// motivos não trazem caminho do servidor.
//
// Valores esperados de `elements` e `answers_names`: varredura estática do
// question.html com o mapa de elementos que o PL monta (freeform.ts loadElements: diretórios core,
// alias com _ e additionalNames; no curso, o diretório exato) e o parser do PL
// (lxml.html.fragments_fromstring, como em python/prairielearn/internal/traverse.py), rodada na
// imagem a193f72. `answers_names` só traz os 5 elementos de resposta do subconjunto suportado.
//
// Valores das variantes conferidas aqui: as fases generate, prepare e render do PL a193f72
// (CodeCallerNative, question_phases.py e os elementos core, com o mustache e o markdown do PL) rodadas
// sobre as fixtures, com as regras da IR e do HTML canônico.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';

import * as cheerio from 'cheerio';

import type { Engine, EngineResult } from '../../src/engine.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, OVERRIDE_COURSE, TEST_COURSE, assertPortuguese, ref } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

type Base = { repo: string; commit: string };

const REPOS_ROOT = process.env.ENGINE_REPOS_ROOT ?? '/repos';

const REFERENCE_QUESTIONS = [
  'element/multipleChoice',
  'element/checkbox',
  'element/numberInput',
  'element/integerInput',
  'element/stringInput',
  'element/figure',
  'element/markdown',
];

type StaticScan = { elements: string[]; answers_names: string[] };

/**
 * `elements` e `answers_names` das questões de referência, tirados do question.html do exampleCourse
 * (a193f72). Em element/markdown não há pl-code: os blocos de código do <markdown> só viram pl-code no
 * processamento do template, e a varredura estática olha as tags do question.html.
 */
const REFERENCE_SCAN: Record<string, StaticScan> = {
  'element/multipleChoice': {
    elements: ['pl-card', 'pl-multiple-choice', 'pl-question-panel'],
    // Não existe sky-color7 no question.html.
    answers_names: [
      'sky-color1', 'sky-color2', 'sky-color3', 'sky-color4', 'sky-color5', 'sky-color6', 'sky-color8',
      'sky-color9', 'sky-color10',
    ],
  },
  'element/checkbox': {
    elements: ['pl-card', 'pl-checkbox', 'pl-question-panel'],
    answers_names: [
      'group_ans_original', 'group_ans_restricted', 'group_ans_fixed', 'group_ans_v4', 'group_ans_v5',
      'group_ans_v6', 'group_ans_v7', 'group_ans_v8', 'group_ans_v9', 'group_ans_v10',
    ],
  },
  'element/numberInput': {
    elements: ['pl-card', 'pl-number-input', 'pl-question-panel'],
    answers_names: ['c_1', 'c_2', 'c_3', 'c_4', 'c_5', 'c_m', 'c_b', 'c_6', 'c_6_blank', 'c_7'],
  },
  'element/integerInput': {
    elements: ['pl-card', 'pl-integer-input', 'pl-question-panel'],
    answers_names: ['c_1', 'c_2', 'c_4', 'c_5', 'c_6', 'c_6_blank', 'c_7_b16', 'c_7_b2', 'c_large', 'c_np'],
  },
  'element/stringInput': {
    elements: ['pl-answer-panel', 'pl-card', 'pl-code', 'pl-hide-in-panel', 'pl-question-panel', 'pl-string-input'],
    answers_names: ['ans1', 'ans2', 'ans3', 'ans4', 'ans5', 'ans6', 'ans7', 'ans8', 'ans9', 'ans10'],
  },
  'element/figure': {
    elements: ['pl-card', 'pl-figure', 'pl-multiple-choice', 'pl-question-panel'],
    answers_names: ['booleans'],
  },
  'element/markdown': {
    elements: ['pl-multiple-choice', 'pl-question-panel'],
    answers_names: ['mc'],
  },
};

const II = ['pl-integer-input', 'pl-question-panel'];

/** Elementos das fixtures com um pl-string-input e o enunciado em pl-question-panel. */
const SI = ['pl-question-panel', 'pl-string-input'];

/** Elementos das fixtures com um pl-figure no enunciado e um pl-integer-input. */
const FII = ['pl-figure', ...II];

/**
 * `elements` e `answers_names` das fixtures com info.json válido, inclusive as
 * broken/*, que só quebram na execução. Os nomes de `answers_names` são o valor do
 * atributo, com as entidades decodificadas (a&lt;b → a<b), que é a chave que o PL usa. Elementos de
 * resposta fora do subconjunto suportado (pl-order-blocks, pl-symbolic-input, pl-matrix-input) e o pl-number-input
 * sobrescrito pelo curso não entram.
 */
const FIXTURE_SCAN: Array<[Base, string, StaticScan]> = [
  // broken/*: a inspeção é estática, e nenhuma condição de não suporte se aplica.
  [TEST_COURSE, 'broken/generate-exception', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/generate-timeout', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-contract-violation', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-echo', { elements: SI, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-illegal-data', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-illegal-raw-in-grade', { elements: SI, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-parse-contract-violation', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-parse-feedback', { elements: SI, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-parse-illegal-data', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-parse-timeout', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-print-parse-and-grade', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-rewrite-submitted', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/grade-timeout', { elements: SI, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/illegal-data', { elements: II, answers_names: ['v'] }],
  // weight="-1" só quebra na montagem da IR; na varredura estática o campo conta.
  [TEST_COURSE, 'broken/negative-weight', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/parse-exception', { elements: II, answers_names: ['v'] }],
  // O arquivo do pl-figure só é conferido no prepare do elemento.
  [TEST_COURSE, 'broken/static-figure-missing', { elements: FII, answers_names: ['v'] }],
  [TEST_COURSE, 'broken/variant-long-output', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/big-media', { elements: FII, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/console-output', { elements: II, answers_names: ['v'] }],
  [
    TEST_COURSE,
    'supported/element-hides-answer',
    {
      elements: [
        'pl-checkbox', 'pl-hide-in-panel', 'pl-integer-input', 'pl-multiple-choice', 'pl-number-input',
        'pl-question-panel', 'pl-string-input',
      ],
      // Tipos misturados, na ordem do documento (um motor que agrupe por tipo falha).
      answers_names: ['dentro', 'num', 'cb', 'txt', 'mc', 'visivel'],
    },
  ],
  [TEST_COURSE, 'supported/empty-answer-render', { elements: II, answers_names: ['sem', 'v'] }],
  [TEST_COURSE, 'supported/grade-changes-params', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/grade-print', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/grade-server-feedback', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/grade-server-parse', { elements: SI, answers_names: ['v'] }],
  // pl_integer_input é o alias com _ do core pl-integer-input: nome canônico e campo suportado
  // (freeform.ts loadElements).
  [TEST_COURSE, 'supported/inspect-underscore-alias', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-answer-template', { elements: ['pl-answer-panel', ...II], answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-console-echo', { elements: II, answers_names: ['v'] }],
  // pl-figure de clientFilesQuestion e de clientFilesCourse, também dentro de pl-answer; o <img> comum
  // e o pl-answer (que não é diretório de elements/ do PL) não são elementos.
  [TEST_COURSE, 'supported/ir-course-media', { elements: ['pl-figure', 'pl-multiple-choice', 'pl-question-panel'], answers_names: ['mc'] }],
  // "h" fica dentro de pl-hide-in-panel question="true": não tem marcador no enunciado, mas é
  // campo suportado e entra em answers_names, na ordem do documento.
  [
    TEST_COURSE,
    'supported/ir-hidden-question-field',
    { elements: ['pl-hide-in-panel', 'pl-integer-input', 'pl-question-panel'], answers_names: ['h', 'v'] },
  ],
  // O <textarea> solto no enunciado é tag HTML comum, e não elemento.
  [TEST_COURSE, 'supported/ir-markup', { elements: ['pl-number-input', 'pl-question-panel'], answers_names: ['x'] }],
  // O <img> com "../" no src não é elemento; o fora.png na raiz da questão entra no content_hash.
  [TEST_COURSE, 'supported/ir-media-escape', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-media-executor', { elements: FII, answers_names: ['v'] }],
  // pl-figure no enunciado e no pl-answer-panel; o <img> comum não é elemento.
  [TEST_COURSE, 'supported/ir-media-extra', { elements: ['pl-answer-panel', 'pl-figure', 'pl-integer-input', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-media-subdir', { elements: FII, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-media-timeout', { elements: FII, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-media-types', { elements: FII, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/ir-multiline-escape', { elements: SI, answers_names: ['t'] }],
  // "sub" fica dentro de pl-submission-panel: não tem marcador no enunciado, mas é campo
  // suportado e entra em answers_names, na ordem do documento.
  [
    TEST_COURSE,
    'supported/ir-submission-panel',
    { elements: ['pl-integer-input', 'pl-question-panel', 'pl-submission-panel'], answers_names: ['sub', 'v'] },
  ],
  // Tipos misturados, na ordem do documento.
  [
    TEST_COURSE,
    'supported/ir-suffix-markup',
    { elements: ['pl-integer-input', 'pl-number-input', 'pl-question-panel', 'pl-string-input'], answers_names: ['n', 'i', 't', 'm'] },
  ],
  // Estática: o valor cru do atributo, antes do template ("{{params.nome}}" casa ^[^\s"<>&]+$).
  [TEST_COURSE, 'supported/ir-template-attrs', { elements: II, answers_names: ['{{params.nome}}'] }],
  [TEST_COURSE, 'supported/latex', { elements: ['pl-number-input', 'pl-question-panel'], answers_names: ['z'] }],
  [TEST_COURSE, 'supported/media', { elements: ['pl-figure', 'pl-multiple-choice', 'pl-question-panel'], answers_names: ['mc'] }],
  [
    TEST_COURSE,
    'supported/multi-part',
    {
      elements: ['pl-checkbox', 'pl-integer-input', 'pl-multiple-choice', 'pl-number-input', 'pl-question-panel', 'pl-string-input'],
      answers_names: ['num', 'int', 'txt', 'mc', 'cb'],
    },
  ],
  [TEST_COURSE, 'supported/no-partial-credit', { elements: II, answers_names: ['a', 'b'] }],
  [TEST_COURSE, 'supported/no-show-correct-answer', { elements: ['pl-answer-panel', ...II], answers_names: ['v'] }],
  [TEST_COURSE, 'supported/option-images', { elements: ['pl-figure', 'pl-multiple-choice', 'pl-question-panel'], answers_names: ['mc'] }],
  [
    TEST_COURSE,
    'supported/panels',
    {
      elements: ['pl-answer-panel', 'pl-hide-in-panel', 'pl-integer-input', 'pl-question-panel', 'pl-submission-panel'],
      answers_names: ['v'],
    },
  ],
  [TEST_COURSE, 'supported/parse-changes-params', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/parse-echo', { elements: ['pl-question-panel', 'pl-string-input'], answers_names: ['t'] }],
  [TEST_COURSE, 'supported/preferences', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/random-params', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/server-grade-format-error', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/server-grade-partial', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/server-grade-score', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/server-grade-score-out-of-range', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/single-variant', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'supported/variant-console-phases', { elements: FII, answers_names: ['v'] }],
  // Estática: o pl-order-blocks injetado por {{{params.extra}}} não está no question.html.
  [TEST_COURSE, 'unsupported/dynamic-element', { elements: II, answers_names: ['v'] }],
  // Estática: o valor cru do atributo, antes do template ("{{params.nome}}" casa ^[^\s"<>&]+$).
  [TEST_COURSE, 'unsupported/inspect-dynamic-invalid-answers-name', { elements: II, answers_names: ['v', '{{params.nome}}'] }],
  // Estática: o campo está no question.html, dentro da seção {{#params.mostrar}} do template.
  [TEST_COURSE, 'unsupported/inspect-dynamic-no-fields', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/element-in-answer-panel', { elements: ['pl-answer-panel', 'pl-integer-input', 'pl-matrix-latex', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/element-in-hidden-panel', { elements: ['pl-hide-in-panel', 'pl-integer-input', 'pl-matrix-latex', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/element-in-question-panel', { elements: ['pl-integer-input', 'pl-matrix-latex', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/element-in-submission-panel', { elements: ['pl-integer-input', 'pl-matrix-latex', 'pl-question-panel', 'pl-submission-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/external-grading', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/inspect-external-grading-options', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/invalid-answers-name', { elements: II, answers_names: ['v', 'a b'] }],
  [TEST_COURSE, 'unsupported/invalid-answers-name-quote', { elements: II, answers_names: ['v', 'a"b'] }],
  [TEST_COURSE, 'unsupported/invalid-answers-name-lt', { elements: II, answers_names: ['v', 'a<b'] }],
  [TEST_COURSE, 'unsupported/invalid-answers-name-gt', { elements: II, answers_names: ['v', 'a>b'] }],
  [TEST_COURSE, 'unsupported/invalid-answers-name-amp', { elements: II, answers_names: ['v', 'a&b'] }],
  [TEST_COURSE, 'unsupported/manual-grading', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/no-fields', { elements: ['pl-question-panel'], answers_names: [] }],
  [TEST_COURSE, 'unsupported/order-blocks', { elements: ['pl-integer-input', 'pl-order-blocks', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/server-render', { elements: II, answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/symbolic-input', { elements: ['pl-integer-input', 'pl-question-panel', 'pl-symbolic-input'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/type-v2', { elements: II, answers_names: ['v'] }],
  // Nome canônico, não o nome cru pl_order_blocks.
  [TEST_COURSE, 'unsupported/underscore-alias', { elements: ['pl-integer-input', 'pl-order-blocks', 'pl-question-panel'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/workspace', { elements: II, answers_names: ['v'] }],
  // pl-matrix-output é additionalNames de pl-variable-output; pl_graphviz_render, a variante com _ do
  // additionalNames de pl-graph (elements/<nome>/info.json do PL; freeform.ts, loadElements).
  [TEST_COURSE, 'unsupported/inspect-additional-name', { elements: ['pl-integer-input', 'pl-question-panel', 'pl-variable-output'], answers_names: ['v'] }],
  [TEST_COURSE, 'unsupported/inspect-additional-name-underscore', { elements: ['pl-graph', 'pl-integer-input', 'pl-question-panel'], answers_names: ['v'] }],
  // <PL-INTEGER-INPUT ANSWERS-NAME="v"> e <PL-ORDER-BLOCKS ANSWERS-NAME="ob">: o parser do PL (lxml)
  // devolve a tag e o atributo em minúsculas, e o nome canônico é o do diretório.
  [TEST_COURSE, 'unsupported/inspect-uppercase-tag', { elements: ['pl-integer-input', 'pl-order-blocks', 'pl-question-panel'], answers_names: ['v'] }],
  // O curso define custom-thing, pl-card e pl-number-input (elements/pl-code não tem info.json e não é
  // elemento); conta só o que a questão usa.
  [OVERRIDE_COURSE, 'uses-number', { elements: ['pl-number-input', 'pl-question-panel'], answers_names: [] }],
  [OVERRIDE_COURSE, 'uses-custom', { elements: ['custom-thing', 'pl-integer-input', 'pl-question-panel'], answers_names: ['v'] }],
  [OVERRIDE_COURSE, 'plain-integer', { elements: II, answers_names: ['v'] }],
  // pl-card do curso: nome canônico = diretório do curso; o campo core "v" entra em answers_names.
  [OVERRIDE_COURSE, 'inspect-uses-card', { elements: ['pl-card', 'pl-integer-input', 'pl-question-panel'], answers_names: ['v'] }],
  // O alias com _ vale só para elementos core (freeform.ts loadElements): <pl_number_input> resolve para
  // o pl-number-input core, e não para o do curso, que só tem o nome exato do diretório.
  [OVERRIDE_COURSE, 'inspect-number-alias', { elements: ['pl-number-input', 'pl-question-panel'], answers_names: ['x'] }],
  // elements/pl-code do curso não tem info.json: <pl-code> é o core.
  [OVERRIDE_COURSE, 'inspect-code-without-info', { elements: ['pl-code', 'pl-integer-input', 'pl-question-panel'], answers_names: ['v'] }],
  // Tags dentro de comentários HTML (no topo e dentro do pl-question-panel) não são elementos.
  [OVERRIDE_COURSE, 'inspect-html-comment', { elements: II, answers_names: ['v'] }],
  // Estática: os elementos do curso injetados por {{{params.extra}}} não estão no question.html.
  [OVERRIDE_COURSE, 'inspect-dynamic-override', { elements: II, answers_names: ['v'] }],
  // Três elementos fora do subconjunto; pl-variable (filho de pl-variable-output) não é elemento.
  [
    EXAMPLE,
    'demo/annotated/mathMatrixMultT',
    { elements: ['pl-matrix-input', 'pl-matrix-latex', 'pl-question-panel', 'pl-variable-output'], answers_names: [] },
  ],
];

/**
 * Questões com veredito estático "não suportada": os códigos de motivo exatos e,
 * quando há unsupported_elements, os nomes canônicos (comparados como conjunto: a ordem de
 * reason.elements não é fixada). A inspeção e a criação da variante dão a mesma classificação.
 */
const STATIC_UNSUPPORTED: Array<[Base, string, string[], string[] | null]> = [
  [TEST_COURSE, 'unsupported/type-v2', ['question_type_not_v3'], null],
  [TEST_COURSE, 'unsupported/external-grading', ['external_grading', 'grading_method_not_internal'], null],
  [TEST_COURSE, 'unsupported/inspect-external-grading-options', ['external_grading'], null],
  [TEST_COURSE, 'unsupported/manual-grading', ['grading_method_not_internal'], null],
  [TEST_COURSE, 'unsupported/workspace', ['workspace'], null],
  [TEST_COURSE, 'unsupported/no-fields', ['no_answer_fields'], null],
  [TEST_COURSE, 'unsupported/server-render', ['custom_server_render'], null],
  [TEST_COURSE, 'unsupported/invalid-answers-name', ['invalid_answers_name'], null],
  [TEST_COURSE, 'unsupported/invalid-answers-name-quote', ['invalid_answers_name'], null],
  [TEST_COURSE, 'unsupported/invalid-answers-name-lt', ['invalid_answers_name'], null],
  [TEST_COURSE, 'unsupported/invalid-answers-name-gt', ['invalid_answers_name'], null],
  [TEST_COURSE, 'unsupported/invalid-answers-name-amp', ['invalid_answers_name'], null],
  [TEST_COURSE, 'unsupported/malformed-info', ['invalid_question_metadata'], null],
  [TEST_COURSE, 'unsupported/invalid-json-info', ['invalid_question_metadata'], null],
  [TEST_COURSE, 'unsupported/inspect-invalid-tags', ['invalid_question_metadata'], null],
  [TEST_COURSE, 'unsupported/order-blocks', ['unsupported_elements'], ['pl-order-blocks']],
  [TEST_COURSE, 'unsupported/symbolic-input', ['unsupported_elements'], ['pl-symbolic-input']],
  [TEST_COURSE, 'unsupported/element-in-answer-panel', ['unsupported_elements'], ['pl-matrix-latex']],
  [TEST_COURSE, 'unsupported/element-in-question-panel', ['unsupported_elements'], ['pl-matrix-latex']],
  [TEST_COURSE, 'unsupported/element-in-submission-panel', ['unsupported_elements'], ['pl-matrix-latex']],
  [TEST_COURSE, 'unsupported/element-in-hidden-panel', ['unsupported_elements'], ['pl-matrix-latex']],
  [TEST_COURSE, 'unsupported/underscore-alias', ['unsupported_elements'], ['pl-order-blocks']],
  [TEST_COURSE, 'unsupported/inspect-additional-name', ['unsupported_elements'], ['pl-variable-output']],
  [TEST_COURSE, 'unsupported/inspect-additional-name-underscore', ['unsupported_elements'], ['pl-graph']],
  // O generate lança: "broken" na variante provaria que o código rodou.
  [TEST_COURSE, 'unsupported/inspect-uppercase-tag', ['unsupported_elements'], ['pl-order-blocks']],
  [OVERRIDE_COURSE, 'uses-custom', ['unsupported_elements'], ['custom-thing']],
  [OVERRIDE_COURSE, 'uses-number', ['course_element_override', 'no_answer_fields'], null],
  // pl-card tem nome do subconjunto suportado (sem unsupported_elements) e o campo é core (sem no_answer_fields). O
  // controlador do pl-card do curso lança no prepare e no render: "broken" provaria que ele rodou.
  [OVERRIDE_COURSE, 'inspect-uses-card', ['course_element_override'], null],
  [
    EXAMPLE,
    'demo/annotated/mathMatrixMultT',
    ['no_answer_fields', 'unsupported_elements'],
    ['pl-matrix-input', 'pl-matrix-latex', 'pl-variable-output'],
  ],
];

/** Diretório da questão na área de repositórios. */
function questionDir(base: Base, qid: string): string {
  const key = createHash('sha256').update(base.repo, 'utf8').digest('hex');
  return path.join(REPOS_ROOT, key, base.commit, 'questions', qid);
}

/** Lê o info.json da questão direto da área de repositórios, sem passar pelo motor. */
function readInfo(base: Base, qid: string): any {
  return JSON.parse(fs.readFileSync(path.join(questionDir(base, qid), 'info.json'), 'utf8'));
}

/**
 * Inspeciona, exige 200 e um corpo válido contra inspect_response. Em toda inspeção, o content_hash é
 * o da regra do hash de conteúdo, calculada aqui sobre os arquivos da questão.
 */
async function inspect(engine: Engine, base: Base, qid: string): Promise<any> {
  const body = ok200(await engine.inspect({ question_ref: ref(base, qid) }));
  assertValid('inspect_response', body);
  assert.equal(body.question.content_hash, contentHashOf(questionDir(base, qid)), `${qid}: content_hash pela regra do hash de conteúdo`);
  return body;
}

/**
 * Metadados que vêm do info.json, com os valores padrão. Vale para toda questão
 * com info.json válido, suportada ou não: o metadado não depende do veredito.
 */
function assertMetadataMatchesInfo(q: any, info: any, what: string): void {
  assert.equal(q.uuid, info.uuid, `${what}: uuid`);
  assert.equal(q.title, info.title, `${what}: title`);
  assert.equal(q.topic, info.topic, `${what}: topic`);
  assert.deepEqual(q.tags, info.tags ?? [], `${what}: tags na ordem do arquivo, [] se ausente`);
  assert.equal(q.type, info.type, `${what}: type`);
  assert.equal(q.grading_method, info.gradingMethod ?? 'Internal', `${what}: grading_method (Internal quando ausente)`);
  assert.equal(q.show_correct_answer, info.showCorrectAnswer ?? true, `${what}: show_correct_answer (padrão true)`);
  assert.equal(q.single_variant, info.singleVariant ?? false, `${what}: single_variant (padrão false)`);
  assert.equal(q.partial_credit, info.partialCredit ?? (info.type === 'v3'), `${what}: partial_credit (padrão true se v3)`);
}

/** `elements` (nomes canônicos, em ordem alfabética) e `answers_names` (ordem do documento). */
function assertStaticScan(q: any, expected: StaticScan, what: string): void {
  assert.deepEqual(q.elements, expected.elements, `${what}: elements`);
  assert.deepEqual(q.answers_names, expected.answers_names, `${what}: answers_names`);
}

function assertSupported(body: any, what: string): void {
  assert.equal(body.outcome, 'supported', `${what}: esperado "supported", veio ${JSON.stringify(body).slice(0, 800)}`);
  assert.deepEqual(body.reasons, [], `${what}: questão suportada não tem motivos`);
}

/**
 * Trechos de caminho absoluto do servidor: a área de repositórios, os diretórios de
 * cada repositório nela (sha256 do repo) e o caminho real do exampleCourse e do código do PL na imagem
 * (a raiz do EXAMPLE é um link para /PrairieLearn/exampleCourse). O "/PrairieLearn/" solto não entra:
 * ele casa com a URL do repositório do EXAMPLE, que não é caminho do servidor.
 */
const SERVER_PATH_FRAGMENTS: string[] = [
  `${REPOS_ROOT}/`,
  ...[EXAMPLE, TEST_COURSE, OVERRIDE_COURSE].map((base) => createHash('sha256').update(base.repo, 'utf8').digest('hex')),
  '/PrairieLearn/exampleCourse',
  '/PrairieLearn/apps/',
];

/** Um caminho absoluto do servidor na mensagem do motivo é detalhe técnico. */
function assertNoServerPath(message: string, what: string): void {
  for (const fragment of SERVER_PATH_FRAGMENTS) {
    assert.ok(!message.includes(fragment), `${what}: caminho do servidor ("${fragment}") na mensagem do motivo: ${message}`);
  }
}

/**
 * Exige "unsupported" com exatamente os códigos de motivo dados (como conjunto; um código a mais ou a
 * menos falha). Toda mensagem de motivo precisa estar em português, sem caminho do servidor.
 */
function assertUnsupported(body: any, expected: string[], what: string): void {
  assert.equal(body.outcome, 'unsupported', `${what}: esperado "unsupported", veio ${JSON.stringify(body).slice(0, 800)}`);
  const codes: string[] = body.reasons.map((r: any) => r.code);
  assert.deepEqual([...codes].sort(), [...expected].sort(), `${what}: códigos de motivo`);
  for (const reason of body.reasons) {
    assertPortuguese(reason.message);
    assertNoServerPath(reason.message, `${what} (${reason.code})`);
  }
}

/**
 * Exige o motivo unsupported_elements com exatamente estes elementos. A comparação é de conjunto
 * (ordenado dos dois lados), porque a ordem de reason.elements não é fixada; um nome repetido ou a
 * menos falha.
 */
function assertUnsupportedElements(reasons: any[], elements: string[], what: string): void {
  const reason = reasons.find((r: any) => r.code === 'unsupported_elements');
  assert.ok(reason, `${what}: esperado o motivo unsupported_elements, vieram ${JSON.stringify(reasons)}`);
  assert.ok(Array.isArray(reason.elements), `${what}: unsupported_elements sem a lista de elementos`);
  assert.deepEqual([...reason.elements].sort(), [...elements].sort(), `${what}: elementos fora do subconjunto`);
}

function assertNotFound(result: EngineResult, what: string): void {
  assert.equal(result.status, 404, `${what}: esperado 404, veio ${result.status}: ${JSON.stringify(result.body).slice(0, 800)}`);
  assertValid('error_response', result.body);
  const error = (result.body as any).error;
  assert.equal(error.code, 'not_found', `${what}: "não encontrada" é distinto de "não suportada"`);
  assertPortuguese(error.message);
}

function assertInvalidRequest(result: EngineResult, what: string): void {
  assert.equal(result.status, 400, `${what}: esperado 400, veio ${result.status}: ${JSON.stringify(result.body).slice(0, 800)}`);
  assertValid('error_response', result.body);
  const error = (result.body as any).error;
  assert.equal(error.code, 'invalid_request', `${what}: código de erro`);
  // error_response com mensagem em português, como no 404.
  assertPortuguese(error.message);
}

/**
 * Metadados de um info.json inválido: os tirados do info.json saem null, e os
 * que têm padrão levam o padrão. Com type null (não v3), o padrão de partial_credit é false.
 */
function assertInvalidInfoDefaults(q: any, what: string): void {
  assert.equal(q.uuid, null, `${what}: uuid null (info.json inválido)`);
  assert.equal(q.title, null, `${what}: title null (info.json inválido)`);
  assert.equal(q.topic, null, `${what}: topic null (info.json inválido)`);
  assert.equal(q.type, null, `${what}: type null (info.json inválido)`);
  assert.deepEqual(q.tags, [], `${what}: tags [] (padrão; info.json inválido)`);
  assert.equal(q.grading_method, 'Internal', `${what}: grading_method padrão (info.json inválido)`);
  assert.equal(q.show_correct_answer, true, `${what}: show_correct_answer padrão (info.json inválido)`);
  assert.equal(q.single_variant, false, `${what}: single_variant padrão (info.json inválido)`);
  assert.equal(q.partial_credit, false, `${what}: partial_credit padrão com type null (info.json inválido)`);
}

// Utilitários de IR para as variantes conferidas neste arquivo.

function loadHtml(html: string): cheerio.CheerioAPI {
  return cheerio.load(html, null, false);
}

/** Texto decodificado de um fragmento de HTML. */
function textOf(html: string): string {
  return loadHtml(html).root().text();
}

function norm(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

/** Nomes dos cq-field do enunciado, na ordem do documento. */
function markersOf(html: string): Array<string | undefined> {
  const $ = loadHtml(html);
  return $('cq-field')
    .toArray()
    .map((el) => $(el).attr('name'));
}

/** O campo sem a dica, cujo texto não é fixado (só o tamanho e o idioma, conferidos em ir.test.ts). */
function withoutHint(f: any): any {
  const { hint: _hint, ...rest } = f;
  return rest;
}

/** Cria a variante e exige 200, outcome "ok", corpo válido e IR válida. */
async function createVariantOk(engine: Engine, base: Base, qid: string, seed: string): Promise<any> {
  const body = ok200(await engine.createVariant({ question_ref: ref(base, qid), seed }));
  assertValid('create_variant_response', body);
  assert.equal(body.outcome, 'ok', `${qid} seed ${seed}: esperado outcome "ok": ${JSON.stringify(body).slice(0, 1500)}`);
  assertValid('ir', body.ir);
  return body;
}

test('questões de referência são suportadas, e os metadados batem com o info.json e o question.html', async () => {
  await withEngine({}, async (engine) => {
    for (const qid of REFERENCE_QUESTIONS) {
      const body = await inspect(engine, EXAMPLE, qid);
      assertSupported(body, qid);
      const info = readInfo(EXAMPLE, qid);
      const q = body.question;
      assertMetadataMatchesInfo(q, info, qid);
      assert.equal(q.type, 'v3', `${qid}: type`);
      assert.equal(q.grading_method, 'Internal', `${qid}: grading_method`);
      assert.equal(q.show_correct_answer, true, `${qid}: show_correct_answer`);
      assert.equal(q.single_variant, false, `${qid}: single_variant`);
      assert.equal(q.partial_credit, true, `${qid}: partial_credit`);
      // elements e answers_names em todas as de referência, com a ordem dos campos.
      assertStaticScan(q, REFERENCE_SCAN[qid], qid);
    }
  });
});

test('element/numberInput: answers_names na ordem do documento, elementos canônicos e content_hash estável', async () => {
  await withEngine({}, async (engine) => {
    const first = await inspect(engine, EXAMPLE, 'element/numberInput');
    assert.deepEqual(
      first.question.answers_names,
      ['c_1', 'c_2', 'c_3', 'c_4', 'c_5', 'c_m', 'c_b', 'c_6', 'c_6_blank', 'c_7'],
      'answers_names',
    );
    assert.deepEqual(first.question.elements, ['pl-card', 'pl-number-input', 'pl-question-panel'], 'elements');
    assert.match(first.question.content_hash, /^sha256:[0-9a-f]{64}$/, 'formato do content_hash');
    const second = await inspect(engine, EXAMPLE, 'element/numberInput');
    assert.equal(second.question.content_hash, first.question.content_hash, 'content_hash estável entre chamadas');

    // o valor é o da regra do hash de conteúdo, calculada aqui de forma independente.
    assert.equal(first.question.content_hash, contentHashOf(questionDir(EXAMPLE, 'element/numberInput')), 'content_hash pela regra do hash de conteúdo');
    const other = await inspect(engine, EXAMPLE, 'element/integerInput');
    assert.notEqual(other.question.content_hash, first.question.content_hash, 'questões diferentes têm hashes diferentes');
  });
});

/**
 * Regra do hash de conteúdo: para cada arquivo, em ordem lexicográfica do caminho relativo (com "/"),
 * caminho + NUL + tamanho decimal + NUL + bytes + NUL; diretórios vazios não contam.
 */
function contentHashOf(dir: string): string {
  const files: string[] = [];
  const walk = (rel: string): void => {
    for (const entry of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
      const child = rel ? `${rel}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(child);
      else if (entry.isFile()) files.push(child);
    }
  };
  walk('');
  files.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const h = createHash('sha256');
  for (const rel of files) {
    const bytes = fs.readFileSync(path.join(dir, rel));
    h.update(rel, 'utf8');
    h.update('\0');
    h.update(String(bytes.length), 'utf8');
    h.update('\0');
    h.update(bytes);
    h.update('\0');
  }
  return 'sha256:' + h.digest('hex');
}

test('toda questão inspecionada, suportada ou não, tem os metadados do info.json, elements, answers_names e content_hash pela regra', async () => {
  // Toda entrada da tabela aponta para uma questão presente na área de repositórios (uma fixture
  // removida ou renomeada falha aqui, antes do motor).
  for (const [base, qid] of FIXTURE_SCAN) {
    assert.ok(fs.existsSync(path.join(questionDir(base, qid), 'info.json')), `${qid}: entrada de FIXTURE_SCAN sem a questão na área de repositórios`);
  }
  await withEngine({}, async (engine) => {
    for (const [base, qid, expected] of FIXTURE_SCAN) {
      const body = await inspect(engine, base, qid);
      assertMetadataMatchesInfo(body.question, readInfo(base, qid), qid);
      assertStaticScan(body.question, expected, qid);
    }
  });
});

test('as questões suportadas do curso de teste e as que só quebram na execução (broken/*) dão "supported" na inspeção estática', async () => {
  // As broken/* têm info.json v3 sem gradingMethod, externalGradingOptions nem workspaceOptions, só
  // elementos do subconjunto suportado e nenhum "def render(": elas quebram no generate, no prepare, na
  // montagem da IR ou na correção, e a inspeção não executa código.
  const supported = FIXTURE_SCAN.filter(([base, qid]) => base === TEST_COURSE && /^(supported|broken)\//.test(qid));
  // toda questão de supported/ e de broken/ do curso de teste está na tabela, com os valores
  // esperados; uma fixture nova sem entrada falha aqui, antes do motor (como supportedFixtures() em
  // ir.test.ts).
  for (const group of ['supported', 'broken']) {
    for (const entry of fs.readdirSync(questionDir(TEST_COURSE, group), { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const qid = `${group}/${entry.name}`;
      assert.ok(supported.some(([, q]) => q === qid), `${qid}: fixture sem valores esperados na tabela FIXTURE_SCAN`);
    }
  }
  // As supported/ir-* e a broken/grade-parse-timeout exercitam
  // pl-figure de clientFilesCourse, campo em pl-hide-in-panel question="true", <textarea> e <img> com
  // "../" no enunciado, figura gerada .svg e parse que não termina.
  for (const qid of [
    'supported/panels',
    'supported/ir-submission-panel',
    'supported/ir-media-extra',
    'supported/inspect-underscore-alias',
    'supported/ir-course-media',
    'supported/ir-hidden-question-field',
    'supported/ir-markup',
    'supported/ir-media-escape',
    'supported/ir-media-timeout',
    'supported/ir-media-types',
    'broken/static-figure-missing',
    'broken/negative-weight',
    'broken/generate-timeout',
    'broken/grade-parse-timeout',
  ]) {
    assert.ok(supported.some(([, q]) => q === qid), `a tabela precisa ter ${qid}`);
  }
  await withEngine({}, async (engine) => {
    for (const [base, qid] of supported) {
      assertSupported(await inspect(engine, base, qid), qid);
    }
  });
});

test('metadados fora do padrão: single_variant, show_correct_answer e partial_credit', async () => {
  await withEngine({}, async (engine) => {
    const single = await inspect(engine, TEST_COURSE, 'supported/single-variant');
    assertSupported(single, 'single-variant');
    assert.equal(single.question.single_variant, true, 'single_variant informado ao núcleo');

    const noShow = await inspect(engine, TEST_COURSE, 'supported/no-show-correct-answer');
    assertSupported(noShow, 'no-show-correct-answer');
    assert.equal(noShow.question.show_correct_answer, false, 'show_correct_answer');

    const noPartial = await inspect(engine, TEST_COURSE, 'supported/no-partial-credit');
    assertSupported(noPartial, 'no-partial-credit');
    assert.equal(noPartial.question.partial_credit, false, 'partialCredit: false no info.json');

    // Tipo diferente de v3 sem partialCredit: o padrão do PL é false (sync/fromDisk/questions.js).
    const v2 = await inspect(engine, TEST_COURSE, 'unsupported/type-v2');
    assert.equal(v2.question.partial_credit, false, 'partial_credit padrão para tipo não v3');
    // O type vem do info.json mesmo quando não é v3.
    assert.equal(v2.question.type, 'Calculation', 'type do info.json de type-v2');
  });
});

test('correção externa, correção manual e workspace são não suportadas, com o motivo', async () => {
  await withEngine({}, async (engine) => {
    const external = await inspect(engine, TEST_COURSE, 'unsupported/external-grading');
    assertUnsupported(external, ['external_grading', 'grading_method_not_internal'], 'external-grading');
    assert.equal(external.question.grading_method, 'External', 'external-grading: gradingMethod do info.json');

    const manual = await inspect(engine, TEST_COURSE, 'unsupported/manual-grading');
    assertUnsupported(manual, ['grading_method_not_internal'], 'manual-grading');
    assert.equal(manual.question.grading_method, 'Manual', 'manual-grading: gradingMethod do info.json');

    assertUnsupported(await inspect(engine, TEST_COURSE, 'unsupported/workspace'), ['workspace'], 'workspace');

    // external_grading é a presença de externalGradingOptions, à parte do gradingMethod:
    // sem gradingMethod, o método é Internal, e grading_method_not_internal não se aplica.
    const optionsOnly = await inspect(engine, TEST_COURSE, 'unsupported/inspect-external-grading-options');
    assertUnsupported(optionsOnly, ['external_grading'], 'inspect-external-grading-options');
    assert.equal(optionsOnly.question.grading_method, 'Internal', 'inspect-external-grading-options: grading_method');
  });
});

test('elementos fora do subconjunto, em qualquer painel e por nome alternativo, são listados pelo nome canônico', async () => {
  await withEngine({}, async (engine) => {
    const cases: Array<[Base, string, string[]]> = [
      [TEST_COURSE, 'unsupported/order-blocks', ['pl-order-blocks']],
      [TEST_COURSE, 'unsupported/symbolic-input', ['pl-symbolic-input']],
      [TEST_COURSE, 'unsupported/element-in-answer-panel', ['pl-matrix-latex']],
      [TEST_COURSE, 'unsupported/element-in-question-panel', ['pl-matrix-latex']],
      [TEST_COURSE, 'unsupported/element-in-submission-panel', ['pl-matrix-latex']],
      [TEST_COURSE, 'unsupported/element-in-hidden-panel', ['pl-matrix-latex']],
      [TEST_COURSE, 'unsupported/underscore-alias', ['pl-order-blocks']],
      // Elementos core com os nomes alternativos: additionalNames do info.json do elemento.
      [TEST_COURSE, 'unsupported/inspect-additional-name', ['pl-variable-output']],
      [TEST_COURSE, 'unsupported/inspect-additional-name-underscore', ['pl-graph']],
      // Tag em maiúsculas: o nome canônico é o do diretório, em minúsculas.
      [TEST_COURSE, 'unsupported/inspect-uppercase-tag', ['pl-order-blocks']],
      [OVERRIDE_COURSE, 'uses-custom', ['custom-thing']],
    ];
    for (const [base, qid, elements] of cases) {
      const body = await inspect(engine, base, qid);
      assertUnsupported(body, ['unsupported_elements'], qid);
      assertUnsupportedElements(body.reasons, elements, qid);
    }
  });
});

test('com vários elementos fora do subconjunto, o motivo lista todos (demo/annotated/mathMatrixMultT)', async () => {
  await withEngine({}, async (engine) => {
    // question.html do exampleCourse (a193f72): pl-matrix-latex, pl-variable-output e o campo
    // pl-matrix-input "B", que está fora do subconjunto suportado e por isso não conta como campo suportado.
    const body = await inspect(engine, EXAMPLE, 'demo/annotated/mathMatrixMultT');
    assertUnsupported(body, ['no_answer_fields', 'unsupported_elements'], 'mathMatrixMultT');
    assertUnsupportedElements(body.reasons, ['pl-matrix-input', 'pl-matrix-latex', 'pl-variable-output'], 'mathMatrixMultT');
  });
});

test('tipo não v3, sem campos, render próprio e answers-name inválido são não suportadas', async () => {
  await withEngine({}, async (engine) => {
    const cases: Array<[string, string]> = [
      ['unsupported/type-v2', 'question_type_not_v3'],
      ['unsupported/no-fields', 'no_answer_fields'],
      ['unsupported/server-render', 'custom_server_render'],
      ['unsupported/invalid-answers-name', 'invalid_answers_name'],
      // Cada caractere que a IR não representa (aspas, <, > e &).
      ['unsupported/invalid-answers-name-quote', 'invalid_answers_name'],
      ['unsupported/invalid-answers-name-lt', 'invalid_answers_name'],
      ['unsupported/invalid-answers-name-gt', 'invalid_answers_name'],
      ['unsupported/invalid-answers-name-amp', 'invalid_answers_name'],
    ];
    for (const [qid, code] of cases) {
      assertUnsupported(await inspect(engine, TEST_COURSE, qid), [code], qid);
    }
  });
});

test('elemento do curso: a sobrescrita de um elemento suportado, de resposta ou não, não é suportada; o curso ter o elemento sem a questão usá-lo, o alias com _ do core e um diretório sem info.json não contam', async () => {
  await withEngine({}, async (engine) => {
    const usesNumber = await inspect(engine, OVERRIDE_COURSE, 'uses-number');
    // O campo sobrescrito pelo curso não conta como campo de resposta suportado. O conjunto é exato:
    // pl-number-input tem nome do subconjunto suportado (course_element_override, e não unsupported_elements), e
    // pl-question-panel é core suportado.
    assertUnsupported(usesNumber, ['course_element_override', 'no_answer_fields'], 'uses-number');

    assertSupported(await inspect(engine, OVERRIDE_COURSE, 'plain-integer'), 'plain-integer');

    // a sobrescrita de um elemento do subconjunto suportado que não é de resposta (pl-card) também dá
    // course_element_override, e sozinha: o campo "v" é o pl-integer-input core (sem no_answer_fields), e
    // pl-card tem nome de elemento suportado (sem unsupported_elements).
    assertUnsupported(await inspect(engine, OVERRIDE_COURSE, 'inspect-uses-card'), ['course_element_override'], 'inspect-uses-card');

    // [F] freeform.ts loadElements (PL a193f72): o alias com _ só é criado para elementos core, e o mapa
    // da fase é {...core, ...curso} por chave exata. <pl_number_input> resolve para o pl-number-input core,
    // que é campo suportado: nem course_element_override nem no_answer_fields.
    assertSupported(await inspect(engine, OVERRIDE_COURSE, 'inspect-number-alias'), 'inspect-number-alias');

    // [F] freeform.ts loadElements: um diretório de elements/ do curso sem info.json é ignorado
    // (só conta o diretório de elements/ do curso com info.json). <pl-code> é o core.
    assertSupported(await inspect(engine, OVERRIDE_COURSE, 'inspect-code-without-info'), 'inspect-code-without-info');
  });
});

test('plain-integer, num curso que sobrescreve pl-number-input, cria a variante "ok" com o campo core', async () => {
  await withEngine({}, async (engine) => {
    // A checagem dinâmica aplica a mesma classificação: o curso ter pl-number-input e custom-thing sem a
    // questão usá-los não conta. question.html: "<pl-question-panel>Digite 1.</pl-question-panel>" e o
    // pl-integer-input "v", sem server.py e sem correct-answer.
    const body = await createVariantOk(engine, OVERRIDE_COURSE, 'plain-integer', 'abc');
    const ir = body.ir;
    assert.deepEqual(
      ir.fields.map(withoutHint),
      [
        {
          name: 'v',
          type: 'integer',
          label_html: null,
          suffix_html: null,
          format: { kind: 'integer', base: 10 },
          constraints: { allow_blank: false },
          weight: 1,
          options: null,
        },
      ],
      'plain-integer: o campo do pl-integer-input core, com os padrões do elemento (rótulo e sufixo sem atributo)',
    );
    assert.deepEqual(markersOf(ir.statement.html), ['v'], `plain-integer: marcador do campo: ${ir.statement.html}`);
    assert.equal(norm(textOf(ir.statement.html)), 'Digite 1.', `plain-integer: texto do enunciado: ${ir.statement.html}`);
    // Sem gabarito: sem server.py e sem correct-answer, o prepare do PL a193f72 deixa correct_answers {}, e
    // o painel de resposta do pl-integer-input sai vazio (render devolve "" sem a_tru): can_show false e
    // display_html null.
    assert.deepEqual(
      ir.answer_key,
      [{ name: 'v', can_show: false, correct_value: null, display_html: null }],
      'plain-integer: gabarito ausente (renderização vazia do gabarito)',
    );
    assert.equal(ir.explanation, null, 'plain-integer: sem pl-answer-panel, explanation null');
    assert.deepEqual(ir.media, [], 'plain-integer: sem mídias');
    assert.deepEqual(body.snapshot.params, {}, 'plain-integer: params');
    assert.deepEqual(body.snapshot.correct_answers, {}, 'plain-integer: correct_answers');
    assert.deepEqual(body.warnings, [], 'plain-integer: nenhuma condição de aviso');
  });
});

test('tags de elemento dentro de comentários HTML não contam, na inspeção nem na checagem dinâmica, e o comentário sai do enunciado', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'inspect-html-comment';
    // question.html: "Digite 1." com um comentário dentro do pl-question-panel (<pl-order-blocks
    // answers-name="ob2">), um comentário no topo com pl-order-blocks, o pl-number-input e o
    // custom-thing do curso e um <pl-integer-input answers-name="a b">, e o pl-integer-input "v"; sem
    // server.py. [F] No parser do PL (lxml.html.fragments_fromstring, traverse.py e question_phases.py),
    // o comentário do topo fica fora dos HtmlElement, e o de dentro tem tag que não é texto: nenhum dos
    // dois é elemento. Uma varredura textual acusaria unsupported_elements, course_element_override e
    // invalid_answers_name.
    const inspected = await inspect(engine, OVERRIDE_COURSE, qid);
    assertSupported(inspected, qid);
    assertStaticScan(inspected.question, { elements: II, answers_names: ['v'] }, qid);

    // Variante: o mustache do PL a193f72 mantém os comentários no HTML pós-template, e a checagem
    // dinâmica aplica a mesma classificação. O render do PL a193f72 sobre a fixture repassa os
    // comentários no painel da questão; o HTML canônico os remove com o conteúdo. O resto é igual a plain-integer.
    const body = await createVariantOk(engine, OVERRIDE_COURSE, qid, 'abc');
    const ir = body.ir;
    assert.deepEqual(
      ir.fields.map(withoutHint),
      [
        {
          name: 'v',
          type: 'integer',
          label_html: null,
          suffix_html: null,
          format: { kind: 'integer', base: 10 },
          constraints: { allow_blank: false },
          weight: 1,
          options: null,
        },
      ],
      `${qid}: só o campo "v", com os padrões do pl-integer-input (rótulo e sufixo sem atributo)`,
    );
    assert.deepEqual(markersOf(ir.statement.html), ['v'], `${qid}: marcador do campo: ${ir.statement.html}`);
    assert.equal(norm(textOf(ir.statement.html)), 'Digite 1.', `${qid}: texto do enunciado: ${ir.statement.html}`);
    // O textOf ignora comentários; a remoção (HTML canônico, "comentários" com o conteúdo removido) é conferida no HTML.
    assert.doesNotMatch(ir.statement.html, /<!--|-->|pl-order-blocks|custom-thing|answers-name/, `${qid}: comentário no enunciado: ${ir.statement.html}`);
    // Sem server.py e sem correct-answer: o render do pl-integer-input no painel de resposta do PL a193f72
    // sai vazio (só o comentário do topo e espaços ficam no painel), então can_show false e display_html
    // null, como em plain-integer.
    assert.deepEqual(
      ir.answer_key,
      [{ name: 'v', can_show: false, correct_value: null, display_html: null }],
      `${qid}: gabarito ausente (renderização vazia do gabarito)`,
    );
    assert.equal(ir.explanation, null, `${qid}: sem pl-answer-panel, explanation null`);
    assert.deepEqual(ir.media, [], `${qid}: sem mídias`);
    assert.deepEqual(body.snapshot.params, {}, `${qid}: params`);
    assert.deepEqual(body.snapshot.correct_answers, {}, `${qid}: correct_answers`);
    assert.deepEqual(body.warnings, [], `${qid}: nenhuma condição de aviso`);
  });
});

test('elemento de resposta suportado pelo nome alternativo com _ dá "supported" e vira campo integer na variante', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'supported/inspect-underscore-alias';
    // question.html: <pl-question-panel><p>Digite 31 em base 16.</p></pl-question-panel> e
    // <pl_integer_input answers-name="v" base="16">; server.py: correct_answers["v"] = 31.
    const inspected = await inspect(engine, TEST_COURSE, qid);
    assertSupported(inspected, qid);
    assertStaticScan(inspected.question, { elements: II, answers_names: ['v'] }, qid);

    const body = await createVariantOk(engine, TEST_COURSE, qid, 'abc');
    const ir = body.ir;
    // Mapa de tipos pelo nome canônico (pl-integer-input), e não pelo nome cru da tag.
    assert.deepEqual(
      ir.fields.map(withoutHint),
      [
        {
          name: 'v',
          type: 'integer',
          label_html: null,
          suffix_html: null,
          format: { kind: 'integer', base: 16 },
          constraints: { allow_blank: false },
          weight: 1,
          options: null,
        },
      ],
      `${qid}: campo do alias, com o base do atributo`,
    );
    assert.deepEqual(markersOf(ir.statement.html), ['v'], `${qid}: marcador no lugar do widget: ${ir.statement.html}`);
    assert.equal(norm(textOf(ir.statement.html)), 'Digite 31 em base 16.', `${qid}: texto do enunciado: ${ir.statement.html}`);
    // Painel de resposta do PL a193f72 para o alias: <samp>1F</samp> (np.base_repr(31, 16)).
    assert.equal(ir.answer_key.length, 1, `${qid}: um item de gabarito`);
    const key = ir.answer_key[0];
    assert.equal(key.name, 'v', `${qid}: nome do gabarito`);
    assert.equal(key.can_show, true, `${qid}: can_show`);
    assert.equal(key.correct_value, 31, `${qid}: correct_value = correct_answers["v"]`);
    assert.equal(typeof key.display_html, 'string', `${qid}: display_html não deveria ser nulo`);
    assert.equal(norm(textOf(key.display_html)), '1F', `${qid}: gabarito exibido em base 16: ${key.display_html}`);
    assert.equal(ir.explanation, null, `${qid}: sem pl-answer-panel, explanation null`);
    assert.deepEqual(ir.media, [], `${qid}: sem mídias`);
    assert.deepEqual(body.snapshot.correct_answers, { v: 31 }, `${qid}: correct_answers`);
    assert.deepEqual(body.warnings, [], `${qid}: nenhuma condição de aviso`);
  });
});

test('info.json fora do formato ou que não é JSON dá invalid_question_metadata, e não 500', async () => {
  await withEngine({}, async (engine) => {
    // inspect-invalid-tags: "tags" é texto, e não lista de texto.
    for (const qid of ['unsupported/malformed-info', 'unsupported/invalid-json-info', 'unsupported/inspect-invalid-tags']) {
      const result = await engine.inspect({ question_ref: ref(TEST_COURSE, qid) });
      assert.notEqual(result.status, 500, `${qid}: metadados ilegíveis não são falha interna`);
      const body = ok200(result);
      assertValid('inspect_response', body);
      assertUnsupported(body, ['invalid_question_metadata'], qid);
      // O que não vem do info.json segue a regra mesmo com metadados inválidos: o hash de conteúdo e a
      // varredura do question.html (os três têm só o pl-integer-input "v").
      assert.equal(body.question.content_hash, contentHashOf(questionDir(TEST_COURSE, qid)), `${qid}: content_hash pela regra do hash de conteúdo`);
      assertStaticScan(body.question, { elements: II, answers_names: ['v'] }, qid);
      // Nenhum metadado copiado do info.json, nem o uuid e o topic, que são
      // válidos em malformed-info e inspect-invalid-tags; tags [] mesmo com o texto "teste".
      assertInvalidInfoDefaults(body.question, qid);
    }
  });
});

test('repositório, commit e qid inexistentes dão 404 not_found, com mensagem em português', async () => {
  await withEngine({}, async (engine) => {
    assertNotFound(
      await engine.inspect({ question_ref: ref({ repo: 'https://example.invalid/nao-existe', commit: EXAMPLE.commit }, 'element/numberInput') }),
      'repositório inexistente',
    );
    assertNotFound(
      await engine.inspect({ question_ref: ref({ repo: EXAMPLE.repo, commit: 'c'.repeat(40) }, 'element/numberInput') }),
      'commit inexistente',
    );
    assertNotFound(await engine.inspect({ question_ref: ref(EXAMPLE, 'element/naoExiste') }), 'qid inexistente');
    // Diretório de questão incompleto (sem info.json) é "não encontrada", não metadado inválido.
    assertNotFound(await engine.inspect({ question_ref: ref(TEST_COURSE, 'incomplete') }), 'questão sem info.json (inspect)');
    assertNotFound(
      await engine.createVariant({ question_ref: ref(TEST_COURSE, 'incomplete'), seed: 'abc' }),
      'questão sem info.json (variants)',
    );
  });
});

test('commit abreviado, branch, tag ou em maiúsculas e qid com "." ou ".." dão 400 invalid_request na inspeção', async () => {
  await withEngine({}, async (engine) => {
    for (const commit of ['a193f72', 'main', 'v1.0', EXAMPLE.commit.toUpperCase()]) {
      assertInvalidRequest(
        await engine.inspect({ question_ref: ref({ repo: EXAMPLE.repo, commit }, 'element/numberInput') }),
        `commit ${commit}`,
      );
    }
    for (const qid of ['a/../b', './x', 'element/./numberInput', '..']) {
      assertInvalidRequest(await engine.inspect({ question_ref: ref(EXAMPLE, qid) }), `qid ${qid}`);
    }
  });
});

test('POST variants em order-blocks dá "unsupported" sem executar o generate que lança', async () => {
  await withEngine({}, async (engine) => {
    const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, 'unsupported/order-blocks'), seed: 'abc' }));
    assertValid('create_variant_response', body);
    // "broken" provaria que o generate (que lança RuntimeError) rodou.
    assert.equal(body.outcome, 'unsupported', `esperado "unsupported", e não "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assertUnsupported(body, ['unsupported_elements'], 'order-blocks');
    assertUnsupportedElements(body.reasons, ['pl-order-blocks'], 'order-blocks');
  });
});

test('dynamic-element: a inspeção dá "supported", mas a variante dá "unsupported" antes do prepare', async () => {
  await withEngine({}, async (engine) => {
    assertSupported(await inspect(engine, TEST_COURSE, 'unsupported/dynamic-element'), 'inspeção de dynamic-element');

    const body = ok200(await engine.createVariant({ question_ref: ref(TEST_COURSE, 'unsupported/dynamic-element'), seed: 'abc' }));
    assertValid('create_variant_response', body);
    // "broken" provaria que o prepare do pl-order-blocks injetado (atributo inválido) rodou.
    assert.equal(body.outcome, 'unsupported', `esperado "unsupported", e não "broken": ${JSON.stringify(body).slice(0, 800)}`);
    // A mesma classificação sobre o HTML pós-template: o campo v e o answers-name "ob" são válidos, e o
    // único motivo é o elemento injetado.
    assertUnsupported(body, ['unsupported_elements'], 'dynamic-element');
    assertUnsupportedElements(body.reasons, ['pl-order-blocks'], 'dynamic-element');
  });
});

test('a checagem dinâmica aplica toda a tabela de motivos: answers-name inválido depois do template e nenhum campo depois do template dão "unsupported", e não "broken" nem 500', async () => {
  await withEngine({}, async (engine) => {
    // Template do PL (mustache) sobre os params do generate, conferido no PL a193f72:
    // - inspect-dynamic-invalid-answers-name: params.nome = "a b" → answers-name="a b" no segundo campo;
    //   o campo "v" continua válido, e o único motivo é invalid_answers_name;
    // - inspect-dynamic-no-fields: params.mostrar = False → a seção some, e não sobra campo.
    const cases: Array<[string, string[]]> = [
      ['unsupported/inspect-dynamic-invalid-answers-name', ['invalid_answers_name']],
      ['unsupported/inspect-dynamic-no-fields', ['no_answer_fields']],
    ];
    for (const [qid, codes] of cases) {
      // O question.html estático é válido: a inspeção não processa o template.
      assertSupported(await inspect(engine, TEST_COURSE, qid), `${qid} (inspeção)`);

      const result = await engine.createVariant({ question_ref: ref(TEST_COURSE, qid), seed: 'abc' });
      assert.notEqual(result.status, 500, `${qid}: a questão fora do subconjunto depois do template não é falha interna`);
      const body = ok200(result);
      assertValid('create_variant_response', body);
      // "broken" (ir_contract_violation) provaria que a IR foi montada com um nome com espaço ou sem campos.
      assert.equal(body.outcome, 'unsupported', `${qid}: esperado "unsupported": ${JSON.stringify(body).slice(0, 800)}`);
      assertUnsupported(body, codes, `${qid} (variante)`);
    }
  });
});

test('a checagem dinâmica usa o mapa core + curso: elementos do curso injetados pelo template dão course_element_override e unsupported_elements antes do prepare', async () => {
  await withEngine({}, async (engine) => {
    const qid = 'inspect-dynamic-override';
    // O question.html estático só tem pl-question-panel e o pl-integer-input "v": a inspeção não processa
    // o template.
    assertSupported(await inspect(engine, OVERRIDE_COURSE, qid), `${qid} (inspeção)`);

    const result = await engine.createVariant({ question_ref: ref(OVERRIDE_COURSE, qid), seed: 'abc' });
    assert.notEqual(result.status, 500, `${qid}: elemento do curso injetado pelo template não é falha interna`);
    const body = ok200(result);
    assertValid('create_variant_response', body);
    // [F] generate + mustache + markdown do PL a193f72 sobre a fixture: o HTML pós-template tem
    // <pl-number-input answers-name="x"></pl-number-input><custom-thing></custom-thing> entre o enunciado e
    // o campo "v". Com o mapa {...core, ...curso} de freeform.ts, pl-number-input resolve para o elemento do
    // curso (nome do subconjunto suportado: course_element_override) e custom-thing para o do curso de nome arbitrário
    // (unsupported_elements). O "v" core continua campo suportado (sem no_answer_fields). Um motor que use
    // só o mapa core na checagem dinâmica rodaria o prepare do pl-number-input do curso e daria "ok".
    assert.equal(body.outcome, 'unsupported', `${qid}: esperado "unsupported", e não "ok" nem "broken": ${JSON.stringify(body).slice(0, 800)}`);
    assertUnsupported(body, ['course_element_override', 'unsupported_elements'], `${qid} (variante)`);
    assertUnsupportedElements(body.reasons, ['custom-thing'], `${qid} (variante)`);
  });
});

test('POST variants numa questão com veredito estático "não suportada" dá "unsupported" com os mesmos motivos da inspeção, sem executar código', async () => {
  await withEngine({}, async (engine) => {
    for (const [base, qid, codes, elements] of STATIC_UNSUPPORTED) {
      const inspected = await inspect(engine, base, qid);
      assertUnsupported(inspected, codes, `${qid} (inspeção)`);
      if (elements) assertUnsupportedElements(inspected.reasons, elements, `${qid} (inspeção)`);

      const result = await engine.createVariant({ question_ref: ref(base, qid), seed: 'abc' });
      // Metadados ilegíveis também não são 404 nem 500 na variante.
      const body = ok200(result);
      assertValid('create_variant_response', body);
      // "ok" ou "broken" provaria que o código da questão rodou (o generate de server-render
      // funciona; o de order-blocks lança).
      assertUnsupported(body, codes, `${qid} (variante)`);
      if (elements) assertUnsupportedElements(body.reasons, elements, `${qid} (variante)`);
    }
  });
});
