// IR neutra de canal, de ponta a ponta sobre o exampleCourse e o curso de teste.
// Cobre: o enunciado, os campos, as opções, o gabarito, as mídias e os avisos
// e a IR válida contra o esquema. Usa as fixtures de mídias e painéis (supported/{latex,media,option-images,panels,no-show-correct-answer,
// single-variant,element-hides-answer,big-media}), as da correção (supported/multi-part e demais) e as próprias deste
// arquivo (supported/ir-*, cq-test-course/clientFilesCourse/x.png e
// cq-test-course/clientFilesCourse/sub/fig.png).
//
// Asserção de valor em cada campo de saída. Origem dos valores esperados:
// - question.html, info.json e server.py das fixtures e do exampleCourse (lidos no container pl-oracle,
//   PL a193f72), com as regras da IR e do HTML canônico;
// - padrões dos elementos do PL a193f72 (constantes *_DEFAULT de elements/*/*.py; pl-checkbox.py
//   prepare para o número de opções exibidas);
// - o oráculo (pl-oracle): params e true_answer da tabela variants, e o painel "Correct answer" de
//   GET /pl/course/1/question/<id>/preview?variant_id=<id> nas variantes com submissão das seeds de
//   seeds(3) (o preview só mostra o painel de resposta depois de uma submissão; lib/question-render.js).
//   O texto do painel foi canonicalizado à mão pelas regras do HTML canônico;
// - além disso: o painel "answer" de cada elemento renderizado com o próprio código do PL a193f72
//   (question_phases.process e elements/*/*.py, numa imagem descartável do PL, 2026-09-25), para os
//   campos que o preview do oráculo não mostra (can_show falso) e para fixtures do curso de teste;
// - por fim: o mesmo procedimento (generate, prepare, render dos painéis "question" e "answer" e o
//   file() de cada figura num executor novo) para outras fixtures do curso de teste e para o HTML serializado das
//   demais fixtures; o preview do oráculo (element/markdown, variante 377) e a tabela variants
//   (params e true_answer de element/figure e element/markdown, seed abc).
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';

import * as cheerio from 'cheerio';

import type { Engine } from '../../src/engine.ts';
import { assertCanonicalHtml } from '../helpers/canonical-html-checker.ts';
import { ok200, withEngine } from '../helpers/engine-harness.ts';
import { EXAMPLE, TEST_COURSE, assertPortuguese, ref, seeds } from '../helpers/fixtures.ts';
import { assertValid } from '../helpers/schemas.ts';

type Base = { repo: string; commit: string };

const SUPPORTED_DIR = path.join(
  import.meta.dirname, '..', 'fixtures', 'courses', 'cq-test-course', 'questions', 'supported',
);

const REPOS_ROOT = process.env.ENGINE_REPOS_ROOT ?? '/repos';

/** Diretório de uma questão do exampleCourse na área de repositórios, como o motor a lê. */
function exampleDir(qid: string): string {
  const repoHash = createHash('sha256').update(EXAMPLE.repo, 'utf8').digest('hex');
  return path.join(REPOS_ROOT, repoHash, EXAMPLE.commit, 'questions', qid);
}

/** As 5 questões de referência (um elemento de resposta cada). */
const REFERENCE = [
  'element/multipleChoice',
  'element/checkbox',
  'element/numberInput',
  'element/integerInput',
  'element/stringInput',
];

/** As questões suportadas do curso de teste. */
const EXPECTED_SUPPORTED = [
  'supported/multi-part',
  'supported/random-params',
  'supported/console-output',
  'supported/parse-changes-params',
  'supported/preferences',
  'supported/server-grade-score',
  'supported/server-grade-partial',
  'supported/server-grade-format-error',
  'supported/server-grade-score-out-of-range',
  'supported/parse-echo',
  'supported/latex',
  'supported/media',
  'supported/option-images',
  'supported/panels',
  'supported/no-show-correct-answer',
  'supported/single-variant',
  'supported/element-hides-answer',
  'supported/big-media',
  'supported/empty-answer-render',
];

/** Fixtures criadas para este arquivo. */
const IR_FIXTURES = [
  'supported/ir-submission-panel',
  'supported/ir-media-extra',
  'supported/ir-course-media',
  'supported/ir-media-escape',
  'supported/ir-media-types',
  'supported/ir-markup',
  'supported/ir-hidden-question-field',
  'supported/ir-media-timeout',
  'supported/ir-suffix-markup',
  'supported/ir-template-attrs',
  'supported/ir-console-echo',
  'supported/ir-multiline-escape',
  'supported/ir-answer-template',
  'supported/ir-media-subdir',
  'supported/ir-media-executor',
];

/** Raiz do curso de teste (a mídia de clientFilesCourse fica fora de questions/). */
const COURSE_DIR = path.join(import.meta.dirname, '..', 'fixtures', 'courses', 'cq-test-course');

/** Diretórios de supported/ do curso de teste, como "supported/<nome>", em ordem. */
function supportedFixtures(): string[] {
  return fs
    .readdirSync(SUPPORTED_DIR, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => `supported/${d.name}`)
    .sort();
}

/** Os mesmos bytes que o file() de supported/media devolve para "gerada.png" (server.py, GERADA_PNG). */
const GERADA_PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR42mMQqTgBAAH4AVX3uUk4AAAAAElFTkSuQmCC';

// ---------------------------------------------------------------------------------------------
// Utilitários locais
// ---------------------------------------------------------------------------------------------

/**
 * content_hash calculado de forma independente pela regra do hash das opções:
 * sha256 do html canônico da opção, com cada src="cq-media:mN" trocado por
 * src="cq-media:<origin>:<filename>" e os espaços colapsados e aparados.
 */
function expectedContentHash(html: string, ir: any): string {
  const stable = html.replace(/src="cq-media:(m[0-9]+)"/g, (_m: string, id: string) => {
    const media = ir.media.find((x: any) => x.id === id);
    assert.ok(media, `a opção referencia a mídia ${id}, ausente de ir.media`);
    return `src="cq-media:${media.origin}:${media.filename}"`;
  });
  const normalized = stable.replace(/\s+/g, ' ').trim();
  return 'sha256:' + createHash('sha256').update(normalized, 'utf8').digest('hex');
}

/** Todos os trechos de HTML canônico de uma IR, com a localização para as mensagens. */
function htmlParts(ir: any): Array<[string, string]> {
  const parts: Array<[string, string]> = [['statement.html', ir.statement.html]];
  for (const f of ir.fields) {
    if (typeof f.label_html === 'string') parts.push([`fields[${f.name}].label_html`, f.label_html]);
    if (typeof f.suffix_html === 'string') parts.push([`fields[${f.name}].suffix_html`, f.suffix_html]);
    for (const o of f.options ?? []) parts.push([`fields[${f.name}].options[${o.key}].html`, o.html]);
  }
  for (const a of ir.answer_key) {
    if (typeof a.display_html === 'string') parts.push([`answer_key[${a.name}].display_html`, a.display_html]);
  }
  if (ir.explanation !== null) parts.push(['explanation.html', ir.explanation.html]);
  return parts;
}

/** Ids das mídias referenciadas por um fragmento (src="cq-media:mN"), na ordem do documento. */
function mediaRefs(html: string): string[] {
  const $ = load(html);
  return $('img')
    .toArray()
    .map((el) => ($(el).attr('src') ?? '').replace(/^cq-media:/, ''));
}

/**
 * Ids na ordem de primeira aparição (numeração das mídias): enunciado, opções (na ordem dos
 * campos e das opções), gabarito e explicação.
 */
function firstAppearance(ir: any): string[] {
  const htmls: string[] = [ir.statement.html];
  for (const f of ir.fields) for (const o of f.options ?? []) htmls.push(o.html);
  for (const a of ir.answer_key) if (typeof a.display_html === 'string') htmls.push(a.display_html);
  if (ir.explanation !== null) htmls.push(ir.explanation.html);
  const seen: string[] = [];
  for (const html of htmls) {
    for (const id of mediaRefs(html)) if (!seen.includes(id)) seen.push(id);
  }
  return seen;
}

/**
 * Contrato e HTML canônico de toda IR obtida neste arquivo, mais as regras que valem para qualquer
 * questão: dica, marcadores de template, chaves e hash das opções, ordem e valor cru do
 * gabarito, numeração e tamanho das mídias e mensagem dos avisos.
 */
function checkIr(body: any, label: string): void {
  assertValid('create_variant_response', body);
  assertValid('ir', body.ir);
  const ir = body.ir;
  for (const [where, html] of htmlParts(ir)) {
    try {
      assertCanonicalHtml(html);
    } catch (err) {
      assert.fail(`${label} ${where}: ${(err as Error).message}\n${html.slice(0, 800)}`);
    }
    // HTML canônico: o texto sai com &amp;, &lt; e &gt;, e &quot; só nos atributos;
    // nenhuma outra entidade nomeada (&nbsp;, &eacute;, &apos;…).
    assert.doesNotMatch(
      html,
      /&(?!(?:amp|lt|gt|quot);)[A-Za-z][A-Za-z0-9]*;/,
      `${label} ${where}: entidade nomeada fora de &amp;, &lt;, &gt; e &quot; (HTML canônico): ${html.slice(0, 800)}`,
    );
    assert.ok(
      !html.replace(/<[^>]*>/g, '').includes('&quot;'),
      `${label} ${where}: &quot; fora de atributo; no texto, a aspa sai literal (HTML canônico): ${html.slice(0, 800)}`,
    );
    // HTML canônico: nenhuma referência numérica (&#39;, &#x27;, &#xE3;…), nem no
    // texto nem nos atributos: o render do PL produz &#x27; (html.escape) e o pl.inner_html produz
    // &#227; em atributo (lxml em ASCII); na IR, esses caracteres saem literais.
    assert.doesNotMatch(
      html,
      /&#(?:[0-9]+|[xX][0-9A-Fa-f]+);?/,
      `${label} ${where}: referência numérica de caractere; os caracteres saem literais (HTML canônico): ${html.slice(0, 800)}`,
    );
    // HTML canônico: fora de pre e de cq-math, os espaços consecutivos viram um só.
    for (const text of textNodesOutside(html, ['pre', 'cq-math'])) {
      assert.doesNotMatch(
        text,
        /[ \t\n\r\f]{2,}/,
        `${label} ${where}: espaços consecutivos fora de pre e de cq-math (HTML canônico): ${JSON.stringify(text)}`,
      );
    }
    // um <img> sem texto alternativo sai sem o atributo alt: nunca alt="".
    for (const [src, alt] of imagesOf(html)) {
      assert.ok(alt === undefined || alt.length > 0, `${label} ${where}: <img src="${src}"> com alt vazio; sem texto alternativo, o atributo sai`);
    }
  }
  for (const f of ir.fields) {
    assert.ok(
      typeof f.hint === 'string' && f.hint.length >= 1 && f.hint.length <= 64,
      `${label}: a dica do campo ${f.name} deve ter de 1 a 64 caracteres: ${JSON.stringify(f.hint)}`,
    );
    // a dica é em português; ela fica neutra e não menciona a vírgula (a vírgula decimal é do núcleo).
    try {
      assertPortuguese(f.hint);
    } catch (err) {
      assert.fail(`${label}: dica do campo ${f.name}: ${(err as Error).message}`);
    }
    assert.doesNotMatch(f.hint, /v[íi]rgula/i, `${label}: a dica do campo ${f.name} não pode mencionar a vírgula: ${f.hint}`);
  }

  // Nenhum marcador de template sobra no enunciado. "}}" não entra aqui: o LaTeX de
  // element/markdown tem "\frac{\sqrt{\pi}}{2}".
  assert.ok(!ir.statement.html.includes('{{'), `${label}: sobrou "{{" no enunciado: ${ir.statement.html.slice(0, 800)}`);
  // a explicação é o pl-answer-panel renderizado pelo PL, depois do mustache.
  if (ir.explanation !== null) {
    assert.ok(!ir.explanation.html.includes('{{'), `${label}: sobrou "{{" na explicação: ${ir.explanation.html.slice(0, 800)}`);
  }

  // Opções da IR: ordem e chaves de params[name]; hash pela regra, calculado aqui.
  for (const f of ir.fields) {
    if (!Array.isArray(f.options)) continue;
    const params = body.snapshot.params[f.name];
    assert.ok(Array.isArray(params), `${label}: params["${f.name}"] deveria ser a lista de opções exibidas`);
    assert.deepEqual(
      f.options.map((o: any) => o.key),
      params.map((p: any) => p.key),
      `${label}: as opções de ${f.name} têm as chaves de params["${f.name}"], na ordem de exibição`,
    );
    for (const o of f.options) {
      assert.equal(o.content_hash, expectedContentHash(o.html, ir), `${label}: content_hash de ${f.name}[${o.key}] (regra do hash das opções)`);
    }
  }

  // (ir.schema.json) um item de gabarito por campo, na ordem de fields; correct_value é
  // correct_answers[name], ou null se ausente.
  assert.deepEqual(
    ir.answer_key.map((a: any) => a.name),
    ir.fields.map((f: any) => f.name),
    `${label}: answer_key deveria ter um item por campo, na ordem de fields`,
  );
  for (const a of ir.answer_key) {
    assert.deepEqual(
      a.correct_value,
      body.snapshot.correct_answers[a.name] ?? null,
      `${label}: correct_value de ${a.name} = correct_answers["${a.name}"] (ou null)`,
    );
  }

  // Mídias da IR (ir.schema.json): m1, m2… na ordem de primeira aparição, todas
  // referenciadas pelo HTML; size_bytes é o tamanho do conteúdo.
  const ids = ir.media.map((_m: any, k: number) => `m${k + 1}`);
  assert.deepEqual(ir.media.map((m: any) => m.id), ids, `${label}: ids das mídias m1, m2… na ordem do array`);
  assert.deepEqual(firstAppearance(ir), ids, `${label}: numeração das mídias pela primeira aparição (enunciado, opções, gabarito, explicação)`);
  for (const m of ir.media) {
    if (m.status === 'ok') assert.equal(m.size_bytes, decoded(m).length, `${label}: size_bytes de ${m.filename}`);
  }

  // a mensagem de cada aviso é uma frase funcional em português; sem
  // caminho absoluto do servidor.
  for (const w of body.warnings) {
    try {
      assertPortuguese(w.message);
    } catch (err) {
      assert.fail(`${label}: mensagem do aviso ${w.code}: ${(err as Error).message}`);
    }
    assertNoServerPath(w.message, `${label}: mensagem do aviso ${w.code}`);
  }
}

/**
 * Trechos de caminho absoluto do servidor: a área de repositórios (ENGINE_REPOS_ROOT e o padrão
 * /repos/), os diretórios dos cursos nela (vetores sha256_hex(repo)) e o destino
 * do link simbólico do exampleCourse na imagem de teste (/PrairieLearn/exampleCourse). O "/PrairieLearn/"
 * solto fica de fora: ele casa com a URL do repositório do PL. Um caminho relativo, como
 * "clientFilesQuestion/ausente.png", continua permitido.
 */
const SERVER_PATH_MARKERS = [
  ...new Set([`${REPOS_ROOT}/`, '/repos/']),
  '70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7',
  '511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d',
  'c92033d9d541cdab2d8b535eb6711fda5deb07263a94b0c1e4be09ca1f74b892',
  '/PrairieLearn/exampleCourse',
  '/PrairieLearn/apps/',
];

function assertNoServerPath(message: string, where: string): void {
  for (const marker of SERVER_PATH_MARKERS) {
    assert.ok(!message.includes(marker), `${where}: caminho absoluto do servidor ("${marker}") é detalhe técnico: ${message}`);
  }
}

/** Textos dos nós de texto de um fragmento que não ficam dentro de nenhuma das tags dadas. */
function textNodesOutside(html: string, tags: string[]): string[] {
  const out: string[] = [];
  const walk = (nodes: any[], inside: boolean): void => {
    for (const node of nodes) {
      if (node.type === 'text') {
        if (!inside) out.push(node.data);
      } else if (Array.isArray(node.children)) {
        walk(node.children, inside || tags.includes(String(node.name).toLowerCase()));
      }
    }
  };
  walk((load(html).root().get(0) as any).children ?? [], false);
  return out;
}

async function createOk(engine: Engine, base: Base, qid: string, seed: string): Promise<any> {
  const body = ok200(await engine.createVariant({ question_ref: ref(base, qid), seed }));
  assert.equal(body.outcome, 'ok', `${qid} seed ${seed}: esperado outcome "ok": ${JSON.stringify(body).slice(0, 1500)}`);
  checkIr(body, `${qid} seed ${seed}`);
  return body;
}

function load(html: string): cheerio.CheerioAPI {
  return cheerio.load(html, null, false);
}

/** Texto decodificado de um fragmento de HTML. */
function textOf(html: string): string {
  return load(html).root().text();
}

function norm(s: string): string {
  return s.replace(/\s+/g, ' ').trim();
}

/** Fórmulas marcadas, na ordem do documento, com o LaTeX decodificado. */
function mathOf(html: string): Array<{ display: string; tex: string }> {
  const $ = load(html);
  return $('cq-math')
    .toArray()
    .map((el) => ({ display: $(el).attr('display') ?? '', tex: $(el).text() }));
}

/** Textos de todos os elementos com o seletor, na ordem do documento. */
function textsOf(html: string, selector: string): string[] {
  const $ = load(html);
  return $(selector)
    .toArray()
    .map((el) => $(el).text());
}

/** Nomes dos cq-field do enunciado, na ordem do documento. */
function markersOf(html: string): Array<string | undefined> {
  const $ = load(html);
  return $('cq-field')
    .toArray()
    .map((el) => $(el).attr('name'));
}

/** Pares [src, alt] dos <img> de um fragmento, na ordem do documento. */
function imagesOf(html: string): Array<[string | undefined, string | undefined]> {
  const $ = load(html);
  return $('img')
    .toArray()
    .map((el): [string | undefined, string | undefined] => [$(el).attr('src'), $(el).attr('alt')]);
}

/** Nomes das tags de um fragmento, na ordem do documento. */
function tagsOf(html: string): string[] {
  const $ = load(html);
  return $('*')
    .toArray()
    .map((el: any) => String(el.name).toLowerCase());
}

function field(ir: any, name: string): any {
  const f = ir.fields.find((x: any) => x.name === name);
  assert.ok(f, `campo "${name}" ausente da IR: ${JSON.stringify(ir.fields.map((x: any) => x.name))}`);
  return f;
}

function answerKey(ir: any, name: string): any {
  const a = ir.answer_key.find((x: any) => x.name === name);
  assert.ok(a, `gabarito do campo "${name}" ausente: ${JSON.stringify(ir.answer_key.map((x: any) => x.name))}`);
  return a;
}

/** Texto normalizado do display_html de um campo, que não pode ser nulo. */
function displayText(ir: any, name: string): string {
  const html = answerKey(ir, name).display_html;
  assert.equal(typeof html, 'string', `display_html de ${name} não deveria ser nulo`);
  return norm(textOf(html));
}

/** A única mídia com esse filename (uma mídia repetida reusa o mesmo id). */
function mediaByFilename(ir: any, filename: string): any {
  const list = ir.media.filter((m: any) => m.filename === filename);
  assert.equal(
    list.length,
    1,
    `esperada exatamente uma mídia com filename "${filename}": ${JSON.stringify(ir.media.map((m: any) => [m.id, m.origin, m.filename, m.status]))}`,
  );
  return list[0];
}

/** Resumo de cada mídia, para comparar com a lista esperada. */
function mediaSummary(ir: any): unknown[][] {
  return ir.media.map((m: any) => [m.id, m.origin, m.filename, m.content_type, m.alt, m.status]);
}

function decoded(media: any): Buffer {
  assert.equal(typeof media.data_base64, 'string', `mídia ${media.filename} sem data_base64`);
  return Buffer.from(media.data_base64, 'base64');
}

function assertAbsent(html: string, marker: string, where: string): void {
  assert.ok(!html.includes(marker), `${where} não deveria conter "${marker}": ${html.slice(0, 800)}`);
  assert.ok(!textOf(html).includes(marker), `${where} (texto) não deveria conter "${marker}"`);
}

// ---------------------------------------------------------------------------------------------
// Tabela por questão: campos, formato, restrições, peso, rótulo, sufixo, can_show,
// explicação e avisos. Valores tirados do question.html/info.json/server.py de cada questão, com os
// padrões dos elementos do PL a193f72 e as regras do PL para o can_show.
// ---------------------------------------------------------------------------------------------

type FieldType = 'number' | 'integer' | 'text' | 'single_choice' | 'multiple_choice';

interface FieldSpec {
  name: string;
  type: FieldType;
  format: Record<string, unknown>;
  /** allow-blank (padrão false em todos os elementos). */
  allowBlank?: boolean;
  /** [min_select, max_select], só em multiple_choice. */
  select?: [number, number];
  /** max_select vem do número de opções exibidas (sem max-select nem detailed-help-text). */
  maxIsShown?: boolean;
  /** weight (WEIGHT_DEFAULT = 1). */
  weight?: number;
  /**
   * Rótulo do atributo label: a fórmula (se houver), o texto normalizado e o HTML canônico, com os
   * espaços colapsados e aparados (o rótulo é só canonicalizado, como HTML canônico). Ausente = o
   * elemento não tem o atributo label, e label_html é null (o aria-label não vira rótulo).
   */
  label?: { tex?: string; text: string; html: string };
  /** Sufixo do atributo suffix, em texto. Ausente = sem o atributo, e suffix_html é null. */
  suffix?: string;
  /**
   * suffix_html exato, quando o texto não basta: o sufixo de linha única com "<", ">" ou "&" sai
   * escapado, e o do pl-string-input multiline sai como HTML canonicalizado.
   */
  suffixHtml?: string;
  /** answer_key[].can_show; ausente quando o teste não fixa o valor. */
  canShow?: boolean;
}

interface QuestionSpec {
  base: Base;
  fields: FieldSpec[];
  /**
   * Marcadores cq-field do enunciado, na ordem do documento: true = exatamente um por
   * campo, na ordem de fields; lista = os nomes exatos, quando um campo fica num trecho que não entra
   * no enunciado (pl-submission-panel; pl-hide-in-panel question="true").
   */
  markers: true | string[];
  /** explanation: null sem pl-answer-panel; objeto com ele. */
  explanation: 'null' | 'present';
  /**
   * Avisos: "none" = lista vazia; "console-generate" = ao menos um, todos
   * console_output na fase generate; "console-only" = só console_output, em qualquer fase (a lista
   * pode sair vazia); lista = pares "code/phase" exatos, ordenados (com a fase própria dos avisos de mídia).
   */
  warnings: 'none' | 'console-generate' | 'console-only' | string[];
}

/** Padrões dos elementos do PL a193f72 (formato e restrições dos campos da IR). */
const NUMBER = { kind: 'number', comparison: 'relabs', rtol: 0.01, atol: 1e-8, allow_fractions: true, allow_complex: false };
const INTEGER = { kind: 'integer', base: 10 };
const INTEGER_16 = { kind: 'integer', base: 16 };
const TEXT = {
  kind: 'text',
  ignore_case: false,
  remove_spaces: false,
  remove_leading_trailing: false,
  normalize_to_ascii: false,
  multiline: false,
  regex: false,
};
const SINGLE = { kind: 'single_choice' };
const MULTI_OFF = { kind: 'multiple_choice', partial_credit: 'off' };

function spec(name: string, type: FieldType, format: Record<string, unknown>, extra: Partial<FieldSpec> = {}): FieldSpec {
  return { name, type, format, canShow: true, ...extra };
}
const num = (name: string, extra: Partial<FieldSpec> = {}): FieldSpec => spec(name, 'number', NUMBER, extra);
const int = (name: string, extra: Partial<FieldSpec> = {}): FieldSpec => spec(name, 'integer', INTEGER, extra);
const txt = (name: string, extra: Partial<FieldSpec> = {}): FieldSpec => spec(name, 'text', TEXT, extra);
const single = (name: string, extra: Partial<FieldSpec> = {}): FieldSpec => spec(name, 'single_choice', SINGLE, extra);
const multi = (name: string, select: [number, number], extra: Partial<FieldSpec> = {}): FieldSpec =>
  spec(name, 'multiple_choice', MULTI_OFF, { select, ...extra });

/** label="$c = $": a fórmula guarda o espaço final (LaTeX byte a byte). */
const C_EQ = { tex: 'c = ', text: 'c =', html: '<cq-math display="inline">c = </cq-math>' };

const SPECS: Record<string, QuestionSpec> = {
  'element/multipleChoice': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      single('sky-color1'),
      single('sky-color2'),
      single('sky-color3', { weight: 3 }),
      single('sky-color4'),
      single('sky-color5'),
      single('sky-color6'),
      single('sky-color8'),
      single('sky-color9', { allowBlank: true }),
      single('sky-color10', { allowBlank: true }),
    ],
  },
  'element/checkbox': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      multi('group_ans_original', [1, 9], { maxIsShown: true }),
      // min-correct="2" max-correct="4" number-answers="5" detailed-help-text="true".
      multi('group_ans_restricted', [2, 4]),
      multi('group_ans_fixed', [1, 9], { maxIsShown: true, weight: 3 }),
      // number-answers="3", sem max-select: vale o número de opções exibidas (pl-checkbox.py, prepare).
      multi('group_ans_v4', [1, 3], { maxIsShown: true }),
      multi('group_ans_v5', [1, 9], { maxIsShown: true, format: { kind: 'multiple_choice', partial_credit: 'net-correct' } }),
      multi('group_ans_v6', [1, 9], { maxIsShown: true, format: { kind: 'multiple_choice', partial_credit: 'each-answer' } }),
      multi('group_ans_v7', [1, 9], { maxIsShown: true, format: { kind: 'multiple_choice', partial_credit: 'coverage' } }),
      multi('group_ans_v8', [1, 9], { maxIsShown: true }),
      multi('group_ans_v9', [3, 8]),
      multi('group_ans_v10', [3, 8]),
    ],
  },
  'element/numberInput': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      num('c_1'),
      num('c_2', { format: { ...NUMBER, rtol: 0.001, atol: 1e-10 }, label: C_EQ }),
      num('c_3', {
        format: { kind: 'number', comparison: 'sigfig', digits: 2, allow_fractions: true, allow_complex: false },
        label: C_EQ,
        suffix: 'units',
      }),
      num('c_4', {
        format: { kind: 'number', comparison: 'decdig', digits: 4, allow_fractions: true, allow_complex: true },
        weight: 2,
        label: C_EQ,
        suffix: 'units',
      }),
      num('c_5', { label: C_EQ, suffix: 'units' }),
      num('c_m'),
      num('c_b'),
      // correct-answer="": o painel de resposta mostra o rótulo (oráculo: "$c = $" e <samp></samp>).
      num('c_6', { allowBlank: true, label: C_EQ }),
      num('c_6_blank', { allowBlank: true, label: C_EQ }),
      num('c_7'),
    ],
  },
  'element/integerInput': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      int('c_1'),
      int('c_2', { label: C_EQ, suffix: 'units' }),
      int('c_4', { weight: 4 }),
      int('c_5'),
      int('c_6', { allowBlank: true }),
      // correct-answer="" (correct_answers = ""): o painel de resposta do PL é só
      // <span class="d-inline-block"><samp></samp></span>, vazio depois da canonicalização.
      int('c_6_blank', { allowBlank: true, canShow: false }),
      int('c_7_b16', { format: INTEGER_16 }),
      int('c_7_b2', { format: { kind: 'integer', base: 2 } }),
      int('c_large', { format: INTEGER_16 }),
      int('c_np'),
    ],
  },
  'element/stringInput': {
    base: EXAMPLE,
    markers: true,
    explanation: 'present',
    warnings: 'none',
    fields: [
      txt('ans1', { label: { text: 'a =', html: 'a =' } }),
      txt('ans2', { label: { text: 'b + c =', html: 'b + c =' } }),
      txt('ans3', { format: { ...TEXT, remove_leading_trailing: true } }),
      txt('ans4', { format: { ...TEXT, remove_spaces: true } }),
      txt('ans5', { allowBlank: true, label: { text: 'blank input box =', html: 'blank input box =' } }),
      txt('ans6', { format: { ...TEXT, ignore_case: true }, label: { text: 'Input box is case insensitive =', html: 'Input box is case insensitive =' } }),
      txt('ans7', { label: { text: 'Small box =', html: 'Small box =' } }),
      txt('ans8', { format: { ...TEXT, normalize_to_ascii: true }, label: { text: 'Weird text =', html: 'Weird text =' }, suffix: '?' }),
      // remove-leading-trailing tem padrão = multiline (pl-string-input.py, linha 126).
      txt('ans9', {
        format: { ...TEXT, multiline: true, remove_leading_trailing: true },
        label: { text: 'Program output', html: 'Program output' },
        suffix: "Don't worry about trailing newlines.",
      }),
      // Dentro de pl-hide-in-panel answer="true".
      txt('ans10', { format: { ...TEXT, regex: true, ignore_case: true, remove_leading_trailing: true }, canShow: false }),
    ],
  },
  'element/markdown': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [single('mc')],
  },
  // As 4 mídias saem "ok" (sem missing_media nem media_error); o matplotlib do file() de cos-x.png pode
  // imprimir ao montar o cache de fontes, então só console_output é tolerado.
  'element/figure': {
    base: EXAMPLE,
    markers: true,
    explanation: 'null',
    warnings: 'console-only',
    fields: [single('booleans')],
  },
  'supported/multi-part': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      num('num', { label: { tex: 'y =', text: 'y =', html: '<cq-math display="inline">y =</cq-math>' }, suffix: 'm/s' }),
      int('int', { format: INTEGER_16 }),
      txt('txt', { format: { ...TEXT, ignore_case: true } }),
      single('mc'),
      multi('cb', [1, 4], { maxIsShown: true, format: { kind: 'multiple_choice', partial_credit: 'net-correct' } }),
    ],
  },
  'supported/random-params': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/console-output': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'console-generate', fields: [int('v')] },
  'supported/parse-changes-params': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/preferences': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/server-grade-score': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/server-grade-partial': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/server-grade-format-error': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/server-grade-score-out-of-range': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  // O print é do parse, não do generate.
  'supported/parse-echo': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [txt('t')] },
  'supported/latex': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [num('z', { label: { tex: 'z =', text: 'z =', html: '<cq-math display="inline">z =</cq-math>' } })],
  },
  // Um aviso por mídia afetada: falha.png (file() lança; media_error na fase file) e ausente.png (imagem
  // comum sem arquivo; missing_media na fase render).
  'supported/media': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: ['media_error/file', 'missing_media/render'],
    fields: [single('mc')],
  },
  'supported/option-images': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [single('mc')] },
  'supported/panels': { base: TEST_COURSE, markers: true, explanation: 'present', warnings: 'none', fields: [int('v')] },
  'supported/no-show-correct-answer': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'present',
    warnings: 'none',
    fields: [int('v', { canShow: false })],
  },
  'supported/single-variant': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/element-hides-answer': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      int('dentro', { canShow: false }),
      num('num', { canShow: false }),
      multi('cb', [1, 2], { maxIsShown: true, canShow: false }),
      txt('txt', { canShow: false }),
      single('mc', { canShow: false }),
      int('visivel'),
    ],
  },
  'supported/big-media': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/empty-answer-render': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [int('sem', { canShow: false }), int('v')],
  },
  'supported/no-partial-credit': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('a'), int('b')] },
  'supported/grade-changes-params': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  // Fixtures criadas para grade.test.ts e create-variant.test.ts: um campo "v" sem
  // atributos. grade-print só imprime no grade(), então a variante não tem aviso; variant-console-phases
  // imprime no prepare e no file() (um console_output por fase, como em create-variant.test.ts).
  'supported/grade-print': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/grade-server-feedback': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/grade-server-parse': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [txt('v')] },
  'supported/variant-console-phases': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: ['console_output/file', 'console_output/prepare'],
    fields: [int('v')],
  },
  // "sub" fica dentro de pl-submission-panel, que não entra no enunciado: sem cq-field.
  'supported/ir-submission-panel': {
    base: TEST_COURSE,
    markers: ['v'],
    explanation: 'null',
    warnings: 'none',
    fields: [int('sub', { canShow: false }), int('v')],
  },
  // Um único aviso: o file() de vazia.png devolve vazio (media_error na fase file).
  'supported/ir-media-extra': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'present',
    warnings: ['media_error/file'],
    fields: [int('v')],
  },
  'supported/ir-course-media': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [single('mc')] },
  // O caminho do <img> escapa de clientFilesQuestion: mídia ausente, missing_media na fase render.
  'supported/ir-media-escape': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: ['missing_media/render'],
    fields: [int('v')],
  },
  'supported/ir-media-types': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  // label="&lt;b&gt;Área&lt;/b&gt; $A$ =": a marcação vira strong e a fórmula vira cq-math.
  'supported/ir-markup': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [num('x', { label: { tex: 'A', text: 'Área A =', html: '<strong>Área</strong> <cq-math display="inline">A</cq-math> =' } })],
  },
  // "h" fica dentro de pl-hide-in-panel question="true", que não entra no enunciado: sem cq-field; o
  // painel de resposta mostra "h" (answer="false", o padrão), então can_show é true.
  'supported/ir-hidden-question-field': {
    base: TEST_COURSE,
    markers: ['v'],
    explanation: 'null',
    warnings: 'none',
    fields: [int('h'), int('v')],
  },
  // O file() de lenta.png não termina: a mídia fica ausente, com media_error na fase file.
  'supported/ir-media-timeout': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: ['media_error/file'],
    fields: [int('v')],
  },
  // Fixture de inspect.test.ts: <pl_integer_input answers-name="v" base="16">,
  // o nome alternativo com "_" do pl-integer-input (o tipo sai do nome canônico).
  'supported/inspect-underscore-alias': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [int('v', { format: INTEGER_16 })],
  },
  // Sufixos com "<", ">" e "&" depois da decodificação do atributo: o de
  // linha única sai escapado, como no PL ({{suffix}}), e o do multiline sai como HTML canonicalizado
  // ({{{suffix}}}; <b> → strong, <i> → em).
  'supported/ir-suffix-markup': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [
      num('n', { suffix: 'm<sup>2</sup> & s', suffixHtml: 'm&lt;sup&gt;2&lt;/sup&gt; &amp; s' }),
      int('i', { suffix: '<b>un</b>', suffixHtml: '&lt;b&gt;un&lt;/b&gt;' }),
      txt('t', { suffix: '<i>x</i>', suffixHtml: '&lt;i&gt;x&lt;/i&gt;' }),
      txt('m', {
        format: { ...TEXT, multiline: true, remove_leading_trailing: true },
        suffix: 'fim aqui',
        suffixHtml: '<strong>fim</strong> <em>aqui</em>',
      }),
    ],
  },
  // answers-name="{{params.nome}}" base="{{params.base}}": o PL vê o HTML depois do mustache (n1, 16).
  'supported/ir-template-attrs': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [int('n1', { format: INTEGER_16 })],
  },
  // O generate imprime "depuração ECO-GENERATE-3b7".
  'supported/ir-console-echo': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'console-generate', fields: [int('v')] },
  'supported/ir-multiline-escape': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'null',
    warnings: 'none',
    fields: [txt('t', { format: { ...TEXT, multiline: true, remove_leading_trailing: true } })],
  },
  // label="Valor {{params.r}}:", com o r da seed abc (server.py: random.randint(1, 10**9) = 763509902).
  'supported/ir-answer-template': {
    base: TEST_COURSE,
    markers: true,
    explanation: 'present',
    warnings: 'none',
    fields: [int('v', { label: { text: 'Valor 763509902:', html: 'Valor 763509902:' } })],
  },
  'supported/ir-media-subdir': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
  'supported/ir-media-executor': { base: TEST_COURSE, markers: true, explanation: 'null', warnings: 'none', fields: [int('v')] },
};

/** Confere uma IR contra a tabela SPECS[qid]. */
function checkSpec(qid: string, body: any, label: string): void {
  const expected = SPECS[qid];
  assert.ok(expected, `${qid}: sem valores esperados na tabela SPECS`);
  const ir = body.ir;
  assert.deepEqual(
    ir.fields.map((f: any) => [f.name, f.type]),
    expected.fields.map((s) => [s.name, s.type]),
    `${label}: campos (nome e tipo) na ordem do documento (mapa de tipos da IR)`,
  );
  assert.deepEqual(
    markersOf(ir.statement.html),
    expected.markers === true ? expected.fields.map((s) => s.name) : expected.markers,
    `${label}: cq-field no enunciado, um por campo que o painel da questão mostra, na ordem do documento`,
  );
  for (const s of expected.fields) {
    const f = field(ir, s.name);
    const where = `${label} campo ${s.name}`;
    assert.deepEqual(f.format, s.format, `${where}: format`);
    const allowBlank = s.allowBlank ?? false;
    if (s.type === 'multiple_choice') {
      assert.ok(s.select, `${where}: tabela sem select`);
      assert.deepEqual(
        f.constraints,
        { allow_blank: allowBlank, min_select: s.select[0], max_select: s.select[1] },
        `${where}: constraints (precedência de pl-checkbox.py)`,
      );
      if (s.maxIsShown) {
        assert.equal(
          f.constraints.max_select,
          body.snapshot.params[s.name].length,
          `${where}: max_select = número de opções exibidas (len(params["${s.name}"]))`,
        );
      }
    } else {
      assert.deepEqual(f.constraints, { allow_blank: allowBlank }, `${where}: constraints`);
    }
    assert.equal(f.weight, s.weight ?? 1, `${where}: weight`);
    if (s.label !== undefined) {
      assert.equal(typeof f.label_html, 'string', `${where}: label_html não deveria ser nulo`);
      assert.deepEqual(
        mathOf(f.label_html),
        s.label.tex === undefined ? [] : [{ display: 'inline', tex: s.label.tex }],
        `${where}: fórmula do rótulo: ${f.label_html}`,
      );
      assert.equal(norm(textOf(f.label_html)), s.label.text, `${where}: texto do rótulo: ${f.label_html}`);
      assert.equal(norm(f.label_html), s.label.html, `${where}: label_html é o atributo label só canonicalizado, sem envoltório`);
    } else {
      assert.equal(f.label_html, null, `${where}: sem o atributo label, label_html é null; o aria-label não vira rótulo`);
    }
    if (s.suffix !== undefined) {
      assert.equal(typeof f.suffix_html, 'string', `${where}: suffix_html não deveria ser nulo`);
      assert.equal(norm(textOf(f.suffix_html)), s.suffix, `${where}: texto do sufixo: ${f.suffix_html}`);
      if (s.suffixHtml !== undefined) {
        assert.equal(f.suffix_html, s.suffixHtml, `${where}: suffix_html`);
      } else if (!/[<>&]/.test(s.suffix)) {
        // Sem "<", ">" nem "&", o sufixo canonicalizado é o próprio texto: apóstrofo e aspas saem
        // literais no HTML canônico, como em "Don't worry about trailing newlines.".
        assert.equal(f.suffix_html, s.suffix, `${where}: sufixo sem marcação é só canonicalizado`);
      }
    } else {
      assert.equal(f.suffix_html, null, `${where}: sem o atributo suffix, suffix_html é null`);
    }
    if (s.canShow !== undefined) {
      assert.equal(answerKey(ir, s.name).can_show, s.canShow, `${where}: can_show`);
    }
  }
  if (expected.explanation === 'null') {
    assert.equal(ir.explanation, null, `${label}: sem pl-answer-panel, explanation é null`);
  } else {
    assert.notEqual(ir.explanation, null, `${label}: o pl-answer-panel deveria virar explanation`);
  }
  const warnings: any[] = body.warnings;
  if (expected.warnings === 'none') {
    assert.deepEqual(warnings, [], `${label}: nenhuma condição de aviso: ${JSON.stringify(warnings)}`);
  } else if (expected.warnings === 'console-generate') {
    assert.ok(warnings.length >= 1, `${label}: faltou o aviso de saída no console`);
    for (const w of warnings) {
      assert.deepEqual([w.code, w.phase], ['console_output', 'generate'], `${label}: só o generate imprime: ${JSON.stringify(warnings)}`);
    }
  } else if (expected.warnings === 'console-only') {
    for (const w of warnings) {
      assert.equal(w.code, 'console_output', `${label}: só a saída no console pode gerar aviso aqui: ${JSON.stringify(warnings)}`);
    }
  } else {
    assert.deepEqual(
      warningPairs(body),
      expected.warnings,
      `${label}: avisos exatos, com código e fase: ${JSON.stringify(warnings)}`,
    );
  }
}

/** Pares "code/phase" dos avisos, ordenados. */
function warningPairs(body: any): string[] {
  return body.warnings.map((w: any) => `${w.code}/${w.phase}`).sort();
}

// ---------------------------------------------------------------------------------------------
// IR válida e HTML canônico
// ---------------------------------------------------------------------------------------------

test('IR válida e HTML canônico: 5 referências × 3 seeds, element/markdown e todas as supported/*', async () => {
  const supported = supportedFixtures();
  for (const qid of [...EXPECTED_SUPPORTED, ...IR_FIXTURES]) {
    assert.ok(supported.includes(qid), `fixture ausente no curso de teste: ${qid}`);
  }
  await withEngine({}, async (engine) => {
    for (const qid of REFERENCE) {
      for (const seed of seeds(3)) await createOk(engine, EXAMPLE, qid, seed);
    }
    await createOk(engine, EXAMPLE, 'element/markdown', 'abc');
    for (const qid of supported) await createOk(engine, TEST_COURSE, qid, 'abc');
  });
});

test('tabela por questão: campos e tipos na ordem do documento, um cq-field por campo, formato, restrições, peso, rótulo, sufixo, can_show, explicação e avisos', async () => {
  const supported = supportedFixtures();
  await withEngine({}, async (engine) => {
    for (const qid of REFERENCE) {
      for (const seed of seeds(3)) checkSpec(qid, await createOk(engine, EXAMPLE, qid, seed), `${qid} seed ${seed}`);
    }
    checkSpec('element/markdown', await createOk(engine, EXAMPLE, 'element/markdown', 'abc'), 'element/markdown seed abc');
    // Toda fixture de supported/ passa pela tabela: uma fixture nova sem valores esperados falha aqui.
    for (const qid of supported) {
      assert.ok(qid in SPECS, `${qid}: fixture de supported/ sem valores esperados na tabela SPECS`);
      checkSpec(qid, await createOk(engine, TEST_COURSE, qid, 'abc'), `${qid} seed abc`);
    }
  });
});

/**
 * [F] Texto do painel da questão de cada fixture (question.html), sem os elementos de resposta, que
 * viram cq-field, e sem o conteúdo de pl-answer-panel, pl-submission-panel e pl-hide-in-panel
 * question="true". cq-field e img não têm texto; params de multi-part
 * (x = 3) e o padrão de preferences (n = 2, cor = azul) vêm do server.py e do info.json.
 */
const STATEMENT_TEXT: Record<string, string> = {
  'supported/console-output': 'Digite 1.',
  'supported/grade-changes-params': 'Digite 1.',
  'supported/grade-print': 'Digite 1.',
  'supported/variant-console-phases': 'Digite 1.',
  'supported/parse-changes-params': 'Digite um inteiro.',
  'supported/server-grade-score': 'Digite 7.',
  'supported/server-grade-partial': 'Digite 7.',
  'supported/server-grade-format-error': 'Digite 7.',
  'supported/server-grade-score-out-of-range': 'Digite 7.',
  'supported/grade-server-feedback': 'Digite 7.',
  'supported/parse-echo': 'Digite um texto.',
  'supported/grade-server-parse': 'Digite a letra A.',
  'supported/no-show-correct-answer': 'Questão que não mostra a resposta correta.',
  'supported/single-variant': 'Questão de variante única.',
  'supported/empty-answer-render': 'Digite dois inteiros.',
  'supported/no-partial-credit': 'Digite 1 e 2.',
  'supported/big-media': 'Questão com uma figura gerada grande.',
  'supported/multi-part': 'x = 3',
  'supported/panels': 'Enunciado da questão com painéis.',
  'supported/option-images': 'Qual é a figura verde?',
  'supported/media': 'Figuras da questão.',
  'supported/element-hides-answer': 'Campos cujo gabarito o PL não mostra.',
  'supported/preferences': 'n = 2, cor = azul',
  'supported/ir-submission-panel': 'Um campo fica dentro do painel de submissão.',
  'supported/ir-media-extra': 'Mídias com nomes que precisam de decodificação.',
  'supported/ir-course-media': 'Mesmo nome de arquivo em dois diretórios.',
  'supported/ir-media-escape': 'Imagem com caminho que sai de clientFilesQuestion.',
  'supported/ir-media-types': 'Mídias com outras extensões.',
  'supported/ir-markup': 'Rótulo com marcação.',
  'supported/ir-hidden-question-field': 'Um campo fica oculto no painel da questão.',
  'supported/ir-suffix-markup': 'Sufixos com marcação.',
  'supported/ir-template-attrs': 'Digite 31 em base 16.',
  'supported/ir-console-echo': 'Digite 1.',
  'supported/ir-multiline-escape': 'Texto com vários caracteres especiais.',
  'supported/ir-answer-template': 'Digite o valor.',
  'supported/ir-media-subdir': 'Figuras em subdiretórios.',
  'supported/ir-media-executor': 'Três figuras geradas.',
};

test('texto exato do enunciado das fixtures: só o painel da questão, sem o texto dos widgets nem dos painéis de resposta e de submissão', async () => {
  await withEngine({}, async (engine) => {
    const bodies: Record<string, any> = {};
    for (const [qid, expected] of Object.entries(STATEMENT_TEXT)) {
      const body = await createOk(engine, TEST_COURSE, qid, 'abc');
      bodies[qid] = body;
      assert.equal(norm(textOf(body.ir.statement.html)), expected, `${qid}: texto do enunciado: ${body.ir.statement.html}`);
    }
    // <img src="cq-media:mN" alt="…"> com o alt do pl-figure (question.html).
    assert.deepEqual(
      imagesOf(bodies['supported/variant-console-phases'].ir.statement.html),
      [['cq-media:m1', 'Figura gerada']],
      'variant-console-phases: imagem do enunciado',
    );
    // "Digite o número {{params.r}}." com o r sorteado da variante.
    const random = await createOk(engine, TEST_COURSE, 'supported/random-params', 'abc');
    assert.equal(norm(textOf(random.ir.statement.html)), `Digite o número ${random.snapshot.params.r}.`, 'random-params: texto do enunciado');
    // multi-part: "x = {{params.x}}" não tem fórmula; o rótulo "$y =$" e o sufixo "m/s" do pl-number-input
    // ficam no campo, não no enunciado.
    const multiPart = await createOk(engine, TEST_COURSE, 'supported/multi-part', 'abc');
    assert.deepEqual(mathOf(multiPart.ir.statement.html), [], `multi-part: o enunciado não tem fórmula: ${multiPart.ir.statement.html}`);
  });
});

// ---------------------------------------------------------------------------------------------
// Enunciado
// ---------------------------------------------------------------------------------------------

test('element/numberInput seed abc: valores sorteados no enunciado, sem template nem tags do PL, um cq-field por campo', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, EXAMPLE, 'element/numberInput', 'abc');
    const html: string = body.ir.statement.html;
    assert.ok(
      html.includes('<cq-math display="inline">c^2 = 9^2 + 6^2</cq-math>'),
      `enunciado sem a fórmula com os parâmetros sorteados: ${html.slice(0, 1500)}`,
    );
    assert.ok(!html.includes('{{') && !html.includes('}}'), 'sobrou marcador de template no enunciado');
    // O texto "pl-number-input" dentro de <code> é legítimo; o que não pode sobrar é uma tag.
    assert.doesNotMatch(html, /<\/?pl-/, 'sobrou tag de elemento do PL no enunciado');

    const $ = load(html);
    const markers = $('cq-field').toArray().map((el) => $(el).attr('name'));
    const names = body.ir.fields.map((f: any) => f.name);
    assert.deepEqual(markers, names, 'o enunciado deve ter exatamente um cq-field por campo, na ordem de fields');

    const insp = ok200(await engine.inspect({ question_ref: ref(EXAMPLE, 'element/numberInput') }));
    assert.deepEqual(names, insp.question.answers_names, 'fields[*].name deve ser igual aos answers_names da inspeção');
  });
});

test('valores sorteados de cada variante no enunciado: referências nas seeds de seeds(3), random-params, preferences e multi-part', async () => {
  await withEngine({}, async (engine) => {
    for (const seed of seeds(3)) {
      // element/numberInput: "$c^2 = {{params.a}}^2 + {{params.b}}^2$" em 6 partes e
      // "<samp>{{correct_answers.c_1}}</samp>" no painel inicial (question.html).
      const ni = await createOk(engine, EXAMPLE, 'element/numberInput', seed);
      const niParams = ni.snapshot.params;
      const niHtml: string = ni.ir.statement.html;
      assert.equal(
        mathOf(niHtml).filter((m) => m.display === 'inline' && m.tex === `c^2 = ${niParams.a}^2 + ${niParams.b}^2`).length,
        6,
        `numberInput seed ${seed}: a fórmula com a e b sorteados deveria aparecer nas 6 partes: ${JSON.stringify(mathOf(niHtml))}`,
      );
      assert.ok(
        norm(textOf(niHtml)).includes(`The correct value of c in all questions below is ${String(ni.snapshot.correct_answers.c_1)}.`),
        `numberInput seed ${seed}: faltou o gabarito de c_1 no texto do enunciado`,
      );

      // element/integerInput: "$c = {{params.a}} + {{params.b}}$" em 4 partes, a soma em base 16,
      // <samp>{{correct_answers.c_1}}</samp>, <code>{{correct_answers.c_large}}</code> e <code>{{params.d}}</code>.
      const ii = await createOk(engine, EXAMPLE, 'element/integerInput', seed);
      const iiParams = ii.snapshot.params;
      const iiAnswers = ii.snapshot.correct_answers;
      const iiHtml: string = ii.ir.statement.html;
      const iiMath = mathOf(iiHtml);
      assert.equal(
        iiMath.filter((m) => m.display === 'inline' && m.tex === `c = ${iiParams.a} + ${iiParams.b}`).length,
        4,
        `integerInput seed ${seed}: fórmula c = a + b nas 4 partes: ${JSON.stringify(iiMath)}`,
      );
      assert.equal(
        iiMath.filter((m) => m.display === 'inline' && m.tex === `c = ${iiParams.a16}_{16} + ${iiParams.b16}_{16}`).length,
        1,
        `integerInput seed ${seed}: fórmula em base 16: ${JSON.stringify(iiMath)}`,
      );
      assert.ok(
        norm(textOf(iiHtml)).includes(`The correct value of c in all questions below is ${String(iiAnswers.c_1)}.`),
        `integerInput seed ${seed}: faltou o gabarito de c_1 no texto do enunciado`,
      );
      const iiCodes = textsOf(iiHtml, 'code');
      assert.ok(iiCodes.includes(iiAnswers.c_large), `integerInput seed ${seed}: <code> com c_large ${iiAnswers.c_large}: ${JSON.stringify(iiCodes)}`);
      assert.ok(iiCodes.includes(String(iiParams.d)), `integerInput seed ${seed}: <code> com params.d: ${JSON.stringify(iiCodes)}`);

      // element/stringInput: os pl-code com os parâmetros (texto do <pre>; oráculo: 'a = "love"*3\n').
      const si = await createOk(engine, EXAMPLE, 'element/stringInput', seed);
      const siParams = si.snapshot.params;
      const pres = textsOf(si.ir.statement.html, 'pre');
      const hasPre = (snippet: string): boolean => pres.some((t) => t.includes(snippet));
      for (const snippet of [
        `a = "${siParams.stringname}"*${siParams.a}`,
        `b = "${siParams.b}"`,
        `c = "${siParams.c}"`,
        `"${siParams.d}"`,
        `"${siParams.e}"`,
        `"${siParams.f}"`,
      ]) {
        assert.ok(hasPre(snippet), `stringInput seed ${seed}: nenhum <pre> contém ${JSON.stringify(snippet)}: ${JSON.stringify(pres)}`);
      }
    }

    for (const seed of seeds(5)) {
      const body = await createOk(engine, TEST_COURSE, 'supported/random-params', seed);
      assert.ok(
        norm(textOf(body.ir.statement.html)).includes(`Digite o número ${body.snapshot.params.r}.`),
        `random-params seed ${seed}: o enunciado deveria trazer o r sorteado: ${body.ir.statement.html}`,
      );
    }

    const defaults = await createOk(engine, TEST_COURSE, 'supported/preferences', 'abc');
    assert.ok(norm(textOf(defaults.ir.statement.html)).includes('n = 2, cor = azul'), `preferences padrão: ${defaults.ir.statement.html}`);
    const asked = ok200(
      await engine.createVariant({ question_ref: ref(TEST_COURSE, 'supported/preferences'), seed: 'abc', preferences: { n: 5, cor: 'verde' } }),
    );
    assert.equal(asked.outcome, 'ok', 'preferences com valores pedidos deve sair "ok"');
    checkIr(asked, 'supported/preferences {n: 5, cor: "verde"}');
    assert.ok(norm(textOf(asked.ir.statement.html)).includes('n = 5, cor = verde'), `preferences pedidas: ${asked.ir.statement.html}`);

    const multiPart = await createOk(engine, TEST_COURSE, 'supported/multi-part', 'abc');
    assert.ok(norm(textOf(multiPart.ir.statement.html)).includes('x = 3'), `multi-part: faltou "x = 3": ${multiPart.ir.statement.html}`);
  });
});

test('o enunciado preserva o texto do painel da questão e não traz o conteúdo dos elementos de resposta', async () => {
  await withEngine({}, async (engine) => {
    const statement = async (base: Base, qid: string): Promise<string> => (await createOk(engine, base, qid, 'abc')).ir.statement.html;
    const includesAll = (html: string, snippets: string[], qid: string): void => {
      const text = norm(textOf(html));
      for (const s of snippets) assert.ok(text.includes(s), `${qid}: o texto do enunciado deveria conter ${JSON.stringify(s)}: ${text.slice(0, 1500)}`);
    };
    const includesNone = (html: string, snippets: string[], qid: string): void => {
      for (const s of snippets) assertAbsent(html, s, `${qid} statement.html (o elemento de resposta vira cq-field)`);
    };

    includesAll(await statement(TEST_COURSE, 'supported/panels'), ['Enunciado da questão com painéis.'], 'panels');
    includesAll(await statement(TEST_COURSE, 'supported/option-images'), ['Qual é a figura verde?'], 'option-images');
    includesAll(await statement(TEST_COURSE, 'supported/media'), ['Figuras da questão.'], 'media');
    includesAll(
      await statement(TEST_COURSE, 'supported/latex'),
      ['Seja x^2 e y.', 'Integral:', 'Soma:', 'Bloco com quebra:', 'Preço: $5', 'Código: $literal$', 'Ignorado: $ignorado$'],
      'latex',
    );

    const multiPart = await statement(TEST_COURSE, 'supported/multi-part');
    includesNone(multiPart, ['Opção um', 'Opção dois', 'Opção três', 'Item um', 'Item dois', 'Item três', 'Item quatro'], 'multi-part');
    includesNone(await statement(TEST_COURSE, 'supported/element-hides-answer'), ['Certa', 'Errada'], 'element-hides-answer');

    const checkbox = await statement(EXAMPLE, 'element/checkbox');
    includesAll(checkbox, ['Group work provides which of the following benefits:'], 'element/checkbox');
    includesNone(checkbox, ['Friendship.'], 'element/checkbox');

    // pl-card header="Part N" vira o texto do cabeçalho (pl-card.mustache: div.card-header desembrulhado);
    // o <code>pl-checkbox</code> de dentro de um <a> fica depois do desembrulho do link.
    const multipleChoice = await statement(EXAMPLE, 'element/multipleChoice');
    includesAll(
      multipleChoice,
      ['What is the color of the sky?', 'Part 1', 'Part 2', 'Part 3', 'Part 4', 'Part 5', 'Part 6', 'Part 7', 'Part 8', 'Part 9'],
      'element/multipleChoice',
    );
    assert.ok(textsOf(multipleChoice, 'code').includes('pl-checkbox'), 'element/multipleChoice: <code>pl-checkbox</code> do link desembrulhado');
    includesNone(multipleChoice, ['Pink', 'Purple'], 'element/multipleChoice');
    // (ênfase; no HTML canônico, b → strong) [F] question.html (pl-oracle): os únicos <b> do painel
    // da questão são "single option" (introdução) e "4" (parte 2); o de element/numberInput é "real or
    // complex values".
    assert.deepEqual(textsOf(multipleChoice, 'strong'), ['single option', '4'], 'element/multipleChoice: <b> vira <strong>');
    assert.ok(multipleChoice.includes('<strong>single option</strong>'), 'element/multipleChoice: <strong>single option</strong>');
    const numberInput = await statement(EXAMPLE, 'element/numberInput');
    assert.deepEqual(textsOf(numberInput, 'strong'), ['real or complex values'], 'element/numberInput: <b> vira <strong>');

    // [F] Oráculo, element/markdown seed abc (variante 377): um só <h1>; os "<h1>…</h1>" dos blocos
    // cercados são texto de <pre>; "Is markdown cool?" fica fora do pl-question-panel, no painel da questão.
    const markdown = await statement(EXAMPLE, 'element/markdown');
    assert.deepEqual(textsOf(markdown, 'h1').map(norm), ['Hello, world!'], 'element/markdown: um só <h1>');
    includesAll(markdown, ['Is markdown cool?', 'this Markdown link!'], 'element/markdown');
    const markdownPres = textsOf(markdown, 'pre');
    for (const snippet of [
      'Inline `code` has `back-ticks around` it.',
      "<h1>This code doesn't have syntax highlighting</h1>",
      'printf("Hello, world!");',
      '<h1>Hello, world!</h1>',
      'std::cout << "Hello, " << name << std::endl;',
    ]) {
      assert.ok(
        markdownPres.some((t) => t.includes(snippet)),
        `element/markdown: nenhum <pre> contém ${JSON.stringify(snippet)}: ${JSON.stringify(markdownPres)}`,
      );
    }

    // [F] Oráculo, element/stringInput: o pl-code guarda a barra e o n literais.
    const stringPres = textsOf(await statement(EXAMPLE, 'element/stringInput'), 'pre');
    assert.ok(
      stringPres.some((t) => t.includes('print("I\'m\\nsorry\\nDave.")')),
      `element/stringInput: faltou o <pre> com print("I'm\\nsorry\\nDave."): ${JSON.stringify(stringPres)}`,
    );
  });
});

test('os marcadores cq-field ficam na posição do elemento, no meio do texto', async () => {
  // Entre duas partes do enunciado, só cabe um espaço (HTML canônico, "Espaços": os consecutivos viram
  // um só); "\s?" admite também a ausência dele onde o fonte só tem espaço entre blocos.
  await withEngine({}, async (engine) => {
    // [F] Oráculo (painel da questão de element/numberInput seed abc): "$f = $<c_m> $ x \, +$ <c_b>".
    const niBody = await createOk(engine, EXAMPLE, 'element/numberInput', 'abc');
    const numberInput: string = niBody.ir.statement.html;
    assert.match(
      numberInput,
      /<cq-math display="inline">f = <\/cq-math>\s?<cq-field name="c_m"><\/cq-field>\s?<cq-math display="inline"> x \\, \+<\/cq-math>\s?<cq-field name="c_b"><\/cq-field>/,
      `element/numberInput: c_m e c_b entre as fórmulas de f: ${numberInput.slice(0, 4000)}`,
    );
    // [F] question.html (pl-oracle): em cada pl-card, o pl-number-input vem logo depois do último
    // parágrafo do pl-question-panel ("<p> Solve for the value of $c$ given $c^2 = {{params.a}}^2 +
    // {{params.b}}^2$ </p>" e, em c_6 e c_6_blank, a pergunta própria). O pl-card é desembrulhado.
    const { a, b } = niBody.snapshot.params;
    const solve = `<p>\\s?Solve for the value of <cq-math display="inline">c</cq-math> given <cq-math display="inline">c\\^2 = ${a}\\^2 \\+ ${b}\\^2</cq-math>\\s?</p>`;
    const beforeField: Record<string, string> = {
      c_1: solve,
      c_2: solve,
      c_3: solve,
      c_4: solve,
      c_5: solve,
      c_6: '<p>One of the roots of <cq-math display="inline">c\\^2 = 0</cq-math> is <cq-math display="inline">c=0</cq-math>\\. Is there another root\\? Keep blank if there is no other root\\.</p>',
      c_6_blank: '<p>What is the greatest common divisor of 7 and 9\\? If the answer is 1, you can keep it blank\\.</p>',
      c_7: solve,
    };
    for (const [name, before] of Object.entries(beforeField)) {
      assert.match(
        numberInput,
        new RegExp(`${before}\\s?<cq-field name="${name}"></cq-field>`),
        `element/numberInput: ${name} logo depois do parágrafo da própria parte: ${numberInput.slice(0, 6000)}`,
      );
    }
    // [F] Oráculo (element/multipleChoice): "The sky is <select sky-color10>. This is because…". Aqui o
    // espaço é do próprio texto ("The sky is\n    <pl-multiple-choice…>.\n    This is because").
    const multipleChoice: string = (await createOk(engine, EXAMPLE, 'element/multipleChoice', 'abc')).ir.statement.html;
    assert.match(
      multipleChoice,
      /The sky is\s<cq-field name="sky-color10"><\/cq-field>\.\sThis is because/,
      'element/multipleChoice: sky-color10 no meio da frase',
    );
    // [F] question.html: nas partes 1 a 8, o pl-multiple-choice vem logo depois de "<p>What is the color
    // of the sky?</p>" (na parte 8, "<p> What is the color of the sky? </p>").
    for (const name of ['sky-color1', 'sky-color2', 'sky-color3', 'sky-color4', 'sky-color5', 'sky-color6', 'sky-color8', 'sky-color9']) {
      assert.match(
        multipleChoice,
        new RegExp(`<p>\\s?What is the color of the sky\\?\\s?</p>\\s?<cq-field name="${name}"></cq-field>`),
        `element/multipleChoice: ${name} logo depois da pergunta da própria parte`,
      );
    }
    const hides: string = (await createOk(engine, TEST_COURSE, 'supported/element-hides-answer', 'abc')).ir.statement.html;
    assert.match(
      hides,
      /Campos cujo gabarito o PL não mostra\.<\/p>\s?<cq-field name="dentro"><\/cq-field>/,
      `element-hides-answer: "dentro" logo depois do parágrafo do pl-question-panel: ${hides}`,
    );
    // [F] question.html de multi-part: "<pl-question-panel>x = {{params.x}}</pl-question-panel>" e os
    // cinco elementos em seguida, separados só por quebras de linha. O enunciado inteiro é o texto e os
    // marcadores, nessa ordem.
    const multiPart: string = (await createOk(engine, TEST_COURSE, 'supported/multi-part', 'abc')).ir.statement.html;
    assert.match(
      multiPart,
      /^\s?x = 3\s?<cq-field name="num"><\/cq-field>\s?<cq-field name="int"><\/cq-field>\s?<cq-field name="txt"><\/cq-field>\s?<cq-field name="mc"><\/cq-field>\s?<cq-field name="cb"><\/cq-field>\s?$/,
      `multi-part: enunciado inteiro: ${multiPart}`,
    );
    // o pl-submission-panel não entra no enunciado: sobra o parágrafo e o marcador de v.
    const submission: string = (await createOk(engine, TEST_COURSE, 'supported/ir-submission-panel', 'abc')).ir.statement.html;
    assert.match(
      submission,
      /^\s?<p>Um campo fica dentro do painel de submissão\.<\/p>\s?<cq-field name="v"><\/cq-field>\s?$/,
      `ir-submission-panel: enunciado inteiro: ${submission}`,
    );
  });
});

test('element/markdown: markdown processado, pl-code sem números de linha, $ literal e fórmulas', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, EXAMPLE, 'element/markdown', 'abc');
    const html: string = body.ir.statement.html;
    const $ = load(html);
    assert.equal($('markdown').length, 0, 'a tag markdown não pode sobrar no enunciado');
    assert.doesNotMatch(html, /<\/?markdown/, 'a tag markdown não pode sobrar no enunciado');
    for (const tag of ['<h1>', '<strong>', '<pre>']) {
      assert.ok(html.includes(tag), `o enunciado deveria conter ${tag}: ${html.slice(0, 1500)}`);
    }
    for (const el of $('pre').toArray()) {
      const text = $(el).text();
      assert.doesNotMatch(text, /^[\d\s]+$/, `<pre> só com números de linha (coluna do pl-code vazou): ${JSON.stringify(text)}`);
    }
    assert.ok(
      $('code').toArray().some((el) => $(el).text() === '$A$1'),
      'o texto $A$1 deveria estar dentro de um code',
    );
    assert.ok(textOf(html).includes('$1.50'), '"$1.50" deveria aparecer como texto');
    const maths = mathOf(html);
    assert.ok(!maths.some((m) => m.tex.includes('1.50')), `"$1.50" não pode virar fórmula: ${JSON.stringify(maths)}`);
    assert.ok(
      maths.some((m) => m.display === 'inline' && m.tex === 'c^2 = a^2 + b^2'),
      `faltou a fórmula inline c^2 = a^2 + b^2: ${JSON.stringify(maths)}`,
    );
    assert.ok(
      maths.some((m) => m.display === 'block' && m.tex.includes('\\begin{cases}')),
      `faltou o bloco com \\begin{cases}: ${JSON.stringify(maths)}`,
    );

    // (fórmulas, com o LaTeX byte a byte) [F] Oráculo, element/markdown seed abc (variante 377): o painel da
    // questão tem exatamente 2 fórmulas inline e 3 em bloco fora de code/pre/mathjax_ignore, com o LaTeX
    // byte a byte (inclusive o espaço depois de "a_{n+1} =" e as quebras de linha do bloco cases).
    assert.deepEqual(
      maths.filter((m) => m.display === 'inline').map((m) => m.tex),
      ['c^2 = a^2 + b^2', '\\sin(\\theta)=\\tan(\\theta)\\cos(\\theta)'],
      `fórmulas inline de element/markdown: ${JSON.stringify(maths)}`,
    );
    assert.deepEqual(
      maths.filter((m) => m.display === 'block').map((m) => m.tex),
      [
        '\\int_0^\\infty e^{-x^2} dx = \\frac{\\sqrt{\\pi}}{2}',
        '\\phi = \\frac{1+\\sqrt{5}}{2}',
        '\na_{n+1} = \n\\begin{cases}\n\\dfrac{a_n}{2} & \\text{if } a_n \\text{ is even} \\\\\n3a_n + 1 & \\text{if } a_n \\text{ is odd}\n\\end{cases}\n',
      ],
      `fórmulas em bloco de element/markdown: ${JSON.stringify(maths)}`,
    );
    // Os "$" em code e em span.mathjax_ignore ficam como texto (oráculo: "<span class="mathjax_ignore">$</span>1.50").
    const codes = textsOf(html, 'code');
    for (const literal of ['$formula$', '\\(formula\\)', '$$formula$$', '\\[formula\\]', '$A$1', 'B$5', '$C9', 'D2', '\\$']) {
      assert.ok(codes.includes(literal), `element/markdown: faltou o <code> ${JSON.stringify(literal)}: ${JSON.stringify(codes)}`);
    }
    assert.ok(
      norm(textOf(html)).includes('a coffee and a bagel cost $1.50 and $2.00 respectively.'),
      'element/markdown: os "$" de mathjax_ignore ficam no texto',
    );
    // (ênfase no HTML canônico) [F] Oráculo, variante 377: "*In your question?*" vira <em>.
    assert.ok(
      textsOf(html, 'em').map(norm).includes('In your question?'),
      `element/markdown: faltou o <em> de "In your question?": ${JSON.stringify(textsOf(html, 'em'))}`,
    );
  });
});

test('HTML serializado: &, < e > escapados, aspas e apóstrofo literais no texto', async () => {
  await withEngine({}, async (engine) => {
    // [F] question.html de element/stringInput (pl-oracle): suffix="Don't worry about trailing newlines."
    // em ans9 e o pl-code com print("I'm\nsorry\nDave.") (barra e n literais). O oráculo serializa o
    // apóstrofo como &#x27;; a IR o deixa literal.
    const si = await createOk(engine, EXAMPLE, 'element/stringInput', 'abc');
    assert.equal(field(si.ir, 'ans9').suffix_html, "Don't worry about trailing newlines.", 'suffix_html de ans9, com o apóstrofo literal');
    assert.ok(
      si.ir.statement.html.includes('print("I\'m\\nsorry\\nDave.")'),
      `element/stringInput: o HTML do enunciado deveria conter print("I'm\\nsorry\\nDave.") com aspas e apóstrofo literais: ${si.ir.statement.html.slice(0, 4000)}`,
    );

    // [F] question.html de element/markdown: o bloco cases tem "&" dentro de \[…\], a alternativa
    // correta é `std::cout << "yes!" << std::endl;`, e "***It's more likely than you might think!***"
    // (oráculo, variante 377: "It&#x27;s").
    const md = await createOk(engine, EXAMPLE, 'element/markdown', 'abc');
    const html: string = md.ir.statement.html;
    assert.ok(
      html.includes('\\dfrac{a_n}{2} &amp; \\text{if }'),
      `element/markdown: o "&" do cq-math sai como &amp;: ${html.slice(0, 4000)}`,
    );
    assert.ok(html.includes("It's more likely than you might think!"), `element/markdown: apóstrofo literal no texto: ${html.slice(0, 4000)}`);
    const yes = field(md.ir, 'mc').options.filter((o: any) => textsOf(o.html, 'code').includes('std::cout << "yes!" << std::endl;'));
    assert.equal(yes.length, 1, 'element/markdown: uma opção com o code de "yes!"');
    assert.ok(
      yes[0].html.includes('std::cout &lt;&lt; "yes!" &lt;&lt; std::endl;'),
      `element/markdown: "<" escapado e aspas literais no html da opção: ${yes[0].html}`,
    );

    // [F] Oráculo, element/markdown seed abc (variante 377): o PL serializa o apóstrofo como
    // &#x27; ("Let&#x27;s check out some math!", "don&#x27;t want code formatting", "doesn&#x27;t have
    // syntax highlighting"), e o pl-code escreve "#include" e "&lt;iostream&gt;" em spans separados; na
    // IR, o apóstrofo sai literal, e "<" e ">" do texto saem como &lt; e &gt;.
    for (const snippet of [
      "Let's check out some math!",
      "if you don't want code formatting",
      '<code>&lt;markdown&gt;</code>',
      "&lt;h1&gt;This code doesn't have syntax highlighting&lt;/h1&gt;",
      '#include &lt;iostream&gt;',
    ]) {
      assert.ok(html.includes(snippet), `element/markdown: o HTML do enunciado deveria conter ${JSON.stringify(snippet)}: ${html.slice(0, 6000)}`);
    }
    // [F] question.html de element/numberInput (pl-oracle): "we've opted to add a suffix or a unit
    // classification to the input via <code>suffix="units"</code>" (oráculo: "we&#x27;ve").
    const ni: string = (await createOk(engine, EXAMPLE, 'element/numberInput', 'abc')).ir.statement.html;
    assert.ok(
      ni.includes('we\'ve opted to add a suffix or a unit classification to the input via <code>suffix="units"</code>'),
      `element/numberInput: apóstrofo e aspas literais no texto: ${ni.slice(0, 6000)}`,
    );
  });
});

test('panels: o enunciado não traz submissão, conteúdo oculto nem explicação; a explicação vem à parte', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/panels', 'abc');
    const html: string = body.ir.statement.html;
    for (const marker of ['MARCADOR-SUBMISSAO', 'MARCADOR-OCULTO', 'Explicação']) {
      assertAbsent(html, marker, 'statement.html');
    }
    assert.notEqual(body.ir.explanation, null, 'a explicação (pl-answer-panel) deveria estar na IR');
    const expl: string = body.ir.explanation.html;
    assert.ok(textOf(expl).includes('Explicação'), `explanation.html deveria conter "Explicação": ${expl}`);
    assert.equal(norm(textOf(expl)), 'Explicação: a=1', `texto de explanation.html (<p>Explicação: $a=1$</p>): ${expl}`);
    // HTML serializado: o "ç" e o "ã" saem literais, e o $a=1$ vira cq-math.
    assert.equal(norm(expl), '<p>Explicação: <cq-math display="inline">a=1</cq-math></p>', `explanation.html de panels: ${expl}`);
    assert.ok(
      mathOf(expl).some((m) => m.tex === 'a=1'),
      `explanation.html deveria conter o cq-math de "$a=1$": ${expl}`,
    );
    assert.equal(body.ir.explanation.can_show, true, 'explanation.can_show deveria ser true');
  });
});

test('element/stringInput: "Any of" (pl-answer-panel) fica fora do enunciado e vai para a explicação, que pode ser mostrada', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, EXAMPLE, 'element/stringInput', 'abc');
    assertAbsent(body.ir.statement.html, 'Any of', 'statement.html');
    assert.notEqual(body.ir.explanation, null, 'a explicação deveria estar na IR');
    assert.ok(
      textOf(body.ir.explanation.html).includes('Any of'),
      `explanation.html deveria conter "Any of": ${body.ir.explanation.html}`,
    );
    assert.equal(
      norm(textOf(body.ir.explanation.html)),
      'Any of nitrogen, phosphorus, or potassium, written either as the full word or as the chemical symbol (N, P, K), in any combination of upper- and lowercase.',
      'texto de explanation.html (pl-answer-panel do question.html)',
    );
    // info.json sem showCorrectAnswer: o padrão é true.
    assert.equal(body.ir.explanation.can_show, true, 'explanation.can_show de element/stringInput');
  });
});

// ---------------------------------------------------------------------------------------------
// campos e gabarito de multi-part
// ---------------------------------------------------------------------------------------------

test('multi-part: tipos, rótulo, sufixo, opções e gabarito', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/multi-part', 'abc');
    const ir = body.ir;
    assert.deepEqual(
      ir.fields.map((f: any) => f.type),
      ['number', 'integer', 'text', 'single_choice', 'multiple_choice'],
      'tipos dos campos fora da ordem do documento',
    );
    const num = field(ir, 'num');
    assert.ok(
      typeof num.label_html === 'string' && num.label_html.includes('<cq-math display="inline">y =</cq-math>'),
      `label_html de num deveria conter a fórmula "y =": ${num.label_html}`,
    );
    assert.deepEqual(mathOf(num.label_html), [{ display: 'inline', tex: 'y =' }], `label_html de num tem só a fórmula "y =": ${num.label_html}`);
    assert.equal(norm(textOf(num.label_html)), 'y =', `label_html de num sem outro texto: ${num.label_html}`);
    assert.equal(num.suffix_html, 'm/s', 'suffix_html de num');

    // Campos de ponta a ponta: formato aceito, restrições e peso dos campos de multi-part.
    assert.deepEqual(num.format, { kind: 'number', comparison: 'relabs', rtol: 0.01, atol: 1e-8, allow_fractions: true, allow_complex: false }, 'format de num');
    assert.deepEqual(field(ir, 'int').format, { kind: 'integer', base: 16 }, 'format de int (a base faz parte do formato)');
    assert.deepEqual(
      field(ir, 'txt').format,
      { kind: 'text', ignore_case: true, remove_spaces: false, remove_leading_trailing: false, normalize_to_ascii: false, multiline: false, regex: false },
      'format de txt',
    );
    assert.deepEqual(field(ir, 'cb').format, { kind: 'multiple_choice', partial_credit: 'net-correct' }, 'format de cb');
    assert.deepEqual(field(ir, 'cb').constraints, { allow_blank: false, min_select: 1, max_select: 4 }, 'constraints de cb (4 opções exibidas)');
    for (const name of ['num', 'int', 'txt', 'mc']) {
      assert.deepEqual(field(ir, name).constraints, { allow_blank: false }, `constraints de ${name}`);
    }
    for (const f of ir.fields) assert.equal(f.weight, 1, `weight de ${f.name}`);
    // Sem pl-answer-panel, não há explicação.
    assert.equal(ir.explanation, null, 'multi-part não tem pl-answer-panel');

    const mc = field(ir, 'mc');
    const params = body.snapshot.params.mc;
    assert.deepEqual(mc.options.map((o: any) => o.key), ['a', 'b', 'c'], 'chaves das opções de mc');
    assert.deepEqual(
      mc.options.map((o: any) => norm(textOf(o.html))),
      params.map((p: any) => norm(textOf(p.html))),
      'as opções de mc deveriam trazer os textos das alternativas',
    );
    assert.deepEqual(mc.options.map((o: any) => norm(textOf(o.html))), ['Opção um', 'Opção dois', 'Opção três'], 'textos de mc (order="fixed")');
    // Opções da IR e HTML canônico. O html é o conteúdo do pl-answer canonicalizado: texto puro,
    // sem envoltório e com "ç" e "ã" literais.
    assert.deepEqual(mc.options.map((o: any) => norm(o.html)), ['Opção um', 'Opção dois', 'Opção três'], 'html das opções de mc');
    for (const o of mc.options) {
      assert.equal(o.content_hash, expectedContentHash(o.html, ir), `content_hash da opção ${o.key} de mc (regra do hash das opções)`);
    }
    // cb com order="fixed": as 4 opções, na ordem do documento.
    const cb = field(ir, 'cb');
    assert.deepEqual(cb.options.map((o: any) => o.key), ['a', 'b', 'c', 'd'], 'chaves das opções de cb');
    assert.deepEqual(
      cb.options.map((o: any) => norm(textOf(o.html))),
      ['Item um', 'Item dois', 'Item três', 'Item quatro'],
      'textos das opções de cb (order="fixed")',
    );
    assert.deepEqual(cb.options.map((o: any) => norm(o.html)), ['Item um', 'Item dois', 'Item três', 'Item quatro'], 'html das opções de cb');

    assert.deepEqual(
      ir.answer_key.map((a: any) => a.name),
      ir.fields.map((f: any) => f.name),
      'answer_key deveria ter um item por campo, na ordem de fields',
    );
    for (const a of ir.answer_key) {
      assert.equal(a.can_show, true, `can_show de ${a.name}`);
      assert.equal(typeof a.display_html, 'string', `display_html de ${a.name} não deveria ser nulo`);
      assert.deepEqual(a.correct_value, body.snapshot.correct_answers[a.name], `correct_value de ${a.name}`);
    }

    // (gabarito exibível) Conteúdo de display_html: o que o painel de resposta do PL mostra para
    // cada elemento, canonicalizado. [F] Render do painel "answer" da fixture com o código do PL
    // (conferido no oráculo): "$y =$ 6.5 m/s | 1F |
    // Brasil | (b) Opção dois | (a) Item um (c) Item três". pl-number-input: label cru, <samp> com
    // format_true_ans (".12g") e o sufixo; pl-integer-input: <samp>1F</samp>; pl-string-input:
    // <code>Brasil</code> (o botão do popover sai com o conteúdo); escolhas: "<li>(chave) html</li>".
    const display = (name: string): string => {
      const entry = ir.answer_key.find((a: any) => a.name === name);
      assert.ok(entry, `answer_key sem ${name}`);
      assertCanonicalHtml(entry.display_html);
      return norm(textOf(entry.display_html));
    };
    assert.equal(display('num'), 'y = 6.5 m/s', 'display_html de num: rótulo, gabarito e sufixo');
    assert.deepEqual(mathOf(answerKey(ir, 'num').display_html), [{ display: 'inline', tex: 'y =' }], 'display_html de num: o rótulo "$y =$" vira cq-math');
    assert.equal(display('int'), '1F', 'display_html de int (gabarito "1F" em base 16)');
    assert.equal(display('txt'), 'Brasil', 'display_html de txt');
    assert.deepEqual(textsOf(answerKey(ir, 'txt').display_html, 'code'), ['Brasil'], 'display_html de txt: o gabarito em <code>');
    assert.equal(display('mc'), '(b) Opção dois', 'display_html de mc: só a alternativa correta, com a chave');
    assert.deepEqual(
      textsOf(answerKey(ir, 'cb').display_html, 'li').map(norm),
      ['(a) Item um', '(c) Item três'],
      'display_html de cb: só as corretas, com a chave, na ordem',
    );
    // HTML serializado do gabarito. [F] Painel "answer" do PL a193f72 (2026-09-25):
    // pl-number-input "<span>$y =$</span>\n<samp>6.5</samp>\n<span>m/s</span>"; pl-integer-input
    // "<samp>1F</samp>"; pl-string-input "<code>Brasil</code>" e o botão do popover, que sai com o
    // conteúdo; as escolhas em "<ul><li>(chave) html</li></ul>", com "ç" e "ã" literais.
    assert.equal(norm(answerKey(ir, 'num').display_html), '<cq-math display="inline">y =</cq-math> 6.5 m/s', 'display_html de num (HTML)');
    assert.equal(norm(answerKey(ir, 'int').display_html), '1F', 'display_html de int (HTML)');
    assert.equal(norm(answerKey(ir, 'txt').display_html), '<code>Brasil</code>', 'display_html de txt (HTML)');
    assert.ok(answerKey(ir, 'mc').display_html.includes('(b) Opção dois'), `display_html de mc (HTML): ${answerKey(ir, 'mc').display_html}`);
    for (const item of ['(a) Item um', '(c) Item três']) {
      assert.ok(answerKey(ir, 'cb').display_html.includes(item), `display_html de cb (HTML) com ${item}: ${answerKey(ir, 'cb').display_html}`);
    }
  });
});

test('sem os atributos label e suffix, label_html e suffix_html são null; o aria-label não vira rótulo', async () => {
  await withEngine({}, async (engine) => {
    // [F] question.html (pl-oracle): estes campos só têm aria-label ("c", "x", "b", "c in base 16",
    // "c in base 2", "String", "Plant nutrient"), sem label nem suffix.
    const cases: Array<[string, string[]]> = [
      ['element/numberInput', ['c_1', 'c_m', 'c_b', 'c_7']],
      ['element/integerInput', ['c_1', 'c_4', 'c_5', 'c_6', 'c_6_blank', 'c_7_b16', 'c_7_b2', 'c_large', 'c_np']],
      ['element/stringInput', ['ans3', 'ans4', 'ans10']],
    ];
    for (const [qid, names] of cases) {
      const body = await createOk(engine, EXAMPLE, qid, 'abc');
      for (const name of names) {
        assert.equal(field(body.ir, name).label_html, null, `${qid} ${name}: label_html (só aria-label)`);
        assert.equal(field(body.ir, name).suffix_html, null, `${qid} ${name}: suffix_html (sem suffix)`);
      }
    }
    // Escolhas não têm label nem suffix no PL.
    const mc = await createOk(engine, EXAMPLE, 'element/multipleChoice', 'abc');
    for (const f of mc.ir.fields) {
      assert.deepEqual([f.label_html, f.suffix_html], [null, null], `element/multipleChoice ${f.name}: rótulo e sufixo`);
    }
  });
});

// ---------------------------------------------------------------------------------------------
// Ordem de exibição, conteúdo e hash estável das opções
// ---------------------------------------------------------------------------------------------

/** Cria variantes percorrendo seeds até achar duas cujas ordens de exibição (orderOf) diferem. */
async function twoOrders(
  engine: Engine,
  base: Base,
  qid: string,
  orderOf: (body: any) => string[],
): Promise<[any, any]> {
  let first: any = null;
  for (const seed of seeds(30)) {
    const body = await createOk(engine, base, qid, seed);
    if (first === null) {
      first = body;
    } else if (JSON.stringify(orderOf(body)) !== JSON.stringify(orderOf(first))) {
      return [first, body];
    }
  }
  assert.fail(`${qid}: nenhuma seed entre 30 deu uma ordem de opções diferente da primeira`);
}

test('element/multipleChoice sky-color1: ordem da variante e hash estável entre seeds', async () => {
  await withEngine({}, async (engine) => {
    const orderOf = (body: any): string[] => body.snapshot.params['sky-color1'].map((p: any) => norm(textOf(p.html)));
    const variants = await twoOrders(engine, EXAMPLE, 'element/multipleChoice', orderOf);
    const hashSets: string[][] = [];
    const blueHashes: string[] = [];
    for (const body of variants) {
      const params = body.snapshot.params['sky-color1'];
      const options = field(body.ir, 'sky-color1').options;
      assert.deepEqual(options.map((o: any) => o.key), params.map((p: any) => p.key), 'as chaves seguem params["sky-color1"]');
      assert.deepEqual(
        options.map((o: any) => norm(textOf(o.html))),
        params.map((p: any) => norm(textOf(p.html))),
        'as opções seguem a ordem de exibição da variante',
      );
      const hashes = options.map((o: any) => o.content_hash);
      assert.equal(new Set(hashes).size, 7, `esperados 7 content_hash distintos: ${JSON.stringify(hashes)}`);
      hashSets.push([...hashes].sort());
      const blue = params.find((p: any) => norm(textOf(p.html)) === 'Blue');
      assert.ok(blue, 'alternativa "Blue" ausente de params');
      blueHashes.push(options.find((o: any) => o.key === blue.key).content_hash);
    }
    assert.deepEqual(hashSets[0], hashSets[1], 'o conjunto dos content_hash deveria ser igual nas duas variantes');
    assert.equal(blueHashes[0], blueHashes[1], 'o hash da opção "Blue" deveria ser igual nas duas variantes');
  });
});

test('element/checkbox group_ans_original: o hash de "Friendship." é igual em variantes de ordens diferentes', async () => {
  await withEngine({}, async (engine) => {
    const orderOf = (body: any): string[] => body.snapshot.params.group_ans_original.map((p: any) => norm(textOf(p.html)));
    const variants = await twoOrders(engine, EXAMPLE, 'element/checkbox', orderOf);
    const friendship: string[] = [];
    const hashSets: string[][] = [];
    for (const body of variants) {
      const options = field(body.ir, 'group_ans_original').options;
      const matches = options.filter((o: any) => norm(textOf(o.html)) === 'Friendship.');
      assert.equal(matches.length, 1, 'exatamente uma opção "Friendship." (as 9 alternativas são exibidas)');
      friendship.push(matches[0].content_hash);
      hashSets.push(options.map((o: any) => o.content_hash).sort());
    }
    assert.equal(friendship[0], friendship[1], 'o hash de "Friendship." deveria ser igual nas duas variantes');
    assert.deepEqual(hashSets[0], hashSets[1], 'o conjunto dos 9 content_hash deveria ser igual nas duas variantes');
  });
});

/** [F] Textos de element/checkbox, na ordem do documento (question.html; group_ans_fixed usa order="fixed"). */
const CHECKBOX_TEXTS = [
  'Exposure to new viewpoints from different fields and life experiences.',
  'Improved creativity and overall work quality.',
  'The ability to work on a project by yourself.',
  'Having only one person make all of the decisions.',
  'Constructive dialog and increased internal group motivation.',
  'Personal accountability to the team to work on a project.',
  'Friendship.',
  'The resources to work on a small project instead of a large project.',
  'Prepares you for a team-environment in the workplace or a research group.',
];

/** [F] sky-color10 (question.html, parte 9): a fórmula de cada cor. */
const SKY_COLOR10_NM: Record<string, string> = {
  Blue: '470nm',
  Purple: '400nm',
  Orange: '600nm',
  Yellow: '580nm',
  Green: '520nm',
  Red: '700nm',
};

test('opções: conteúdo canônico na ordem de exibição, com fórmulas, markdown, pl-code e textos das escolhas múltiplas', async () => {
  await withEngine({}, async (engine) => {
    for (const seed of seeds(3)) {
      // element/checkbox: textos iguais aos de params, na ordem; 5 opções em restricted
      // (number-answers="5"), 3 em v4 (number-answers="3") e 9 nos demais (pl-checkbox.py, prepare).
      const cbBody = await createOk(engine, EXAMPLE, 'element/checkbox', seed);
      const counts: Record<string, number> = { group_ans_restricted: 5, group_ans_v4: 3 };
      for (const s of SPECS['element/checkbox'].fields) {
        const options = field(cbBody.ir, s.name).options;
        const params = cbBody.snapshot.params[s.name];
        assert.equal(options.length, counts[s.name] ?? 9, `checkbox seed ${seed}: número de opções de ${s.name}`);
        assert.deepEqual(
          options.map((o: any) => norm(textOf(o.html))),
          params.map((p: any) => norm(textOf(p.html))),
          `checkbox seed ${seed}: textos das opções de ${s.name} na ordem de params`,
        );
        // Opções da IR e HTML canônico. [F] Oráculo (variants da question 108 nessas seeds): o html
        // de cada alternativa em params é texto puro, sem "<", ">", "&" nem aspas; o html canônico da
        // opção é esse texto, com os espaços colapsados, sem envoltório.
        assert.deepEqual(
          options.map((o: any) => norm(o.html)),
          params.map((p: any) => norm(p.html)),
          `checkbox seed ${seed}: html das opções de ${s.name}`,
        );
      }
      assert.deepEqual(
        field(cbBody.ir, 'group_ans_fixed').options.map((o: any) => [o.key, norm(textOf(o.html))]),
        CHECKBOX_TEXTS.map((text, k) => [String.fromCharCode(97 + k), text]),
        `checkbox seed ${seed}: group_ans_fixed na ordem do documento`,
      );
      assert.deepEqual(
        field(cbBody.ir, 'group_ans_fixed').options.map((o: any) => [o.key, norm(o.html)]),
        CHECKBOX_TEXTS.map((text, k) => [String.fromCharCode(97 + k), text]),
        `checkbox seed ${seed}: html das opções de group_ans_fixed`,
      );

      // element/multipleChoice.
      const mcBody = await createOk(engine, EXAMPLE, 'element/multipleChoice', seed);
      assert.deepEqual(
        field(mcBody.ir, 'sky-color3').options.map((o: any) => [o.key, norm(textOf(o.html))]),
        [['a', 'Blue'], ['b', 'Pink'], ['c', 'Purple'], ['d', 'Orange'], ['e', 'Yellow'], ['f', 'Brown'], ['g', 'Red']],
        `multipleChoice seed ${seed}: sky-color3 com order="fixed"`,
      );
      for (const s of SPECS['element/multipleChoice'].fields) {
        const options = field(mcBody.ir, s.name).options;
        const params = mcBody.snapshot.params[s.name];
        // Em sky-color10, o "$…$" de cada alternativa vira cq-math, cujo texto é o LaTeX sem os "$".
        const paramText = (p: any): string => (s.name === 'sky-color10' ? norm(textOf(p.html).replace(/\$/g, '')) : norm(textOf(p.html)));
        assert.deepEqual(
          options.map((o: any) => norm(textOf(o.html))),
          params.map(paramText),
          `multipleChoice seed ${seed}: textos das opções de ${s.name} na ordem de params`,
        );
        // Opções da IR e HTML canônico. [F] Oráculo (variants da question 127 nessas seeds):
        // alternativas em texto puro ("All of the above" e "None of the above" inclusive); em sky-color10,
        // "Cor ($Nnm$)", cujo "$…$" vira cq-math.
        const paramHtml = (p: any): string => {
          if (s.name !== 'sky-color10') return norm(p.html);
          const color = norm(textOf(p.html)).split(' ')[0];
          return `${color} (<cq-math display="inline">${SKY_COLOR10_NM[color]}</cq-math>)`;
        };
        assert.deepEqual(options.map((o: any) => norm(o.html)), params.map(paramHtml), `multipleChoice seed ${seed}: html das opções de ${s.name}`);
      }
      assert.equal(field(mcBody.ir, 'sky-color2').options.length, 4, `multipleChoice seed ${seed}: sky-color2 com number-answers="4"`);
      for (const o of field(mcBody.ir, 'sky-color10').options) {
        const color = norm(textOf(o.html)).split(' ')[0];
        assert.ok(color in SKY_COLOR10_NM, `sky-color10 seed ${seed}: cor inesperada: ${o.html}`);
        assert.deepEqual(mathOf(o.html), [{ display: 'inline', tex: SKY_COLOR10_NM[color] }], `sky-color10 seed ${seed}: fórmula da opção ${color}`);
      }
    }

    // [F] Oráculo, element/markdown seed abc: params.mc = [yes! (inline code), "Definitely not"
    // (bloco cercado → pl-code), console.log('no')].
    const md = await createOk(engine, EXAMPLE, 'element/markdown', 'abc');
    const mdOptions = field(md.ir, 'mc').options;
    assert.equal(mdOptions.length, 3, 'element/markdown: 3 opções');
    const withCode = (text: string): any[] => mdOptions.filter((o: any) => JSON.stringify(textsOf(o.html, 'code')) === JSON.stringify([text]));
    const yes = withCode('std::cout << "yes!" << std::endl;');
    assert.equal(yes.length, 1, `element/markdown: opção com o code de "yes!": ${JSON.stringify(mdOptions)}`);
    assert.equal(yes[0].key, md.snapshot.correct_answers.mc.key, 'element/markdown: "yes!" é a correta');
    // [F] Oráculo, variante 377: true_answer.mc = {key: "a", html: "<p><code>std::cout &lt;&lt;
    // "yes!" &lt;&lt; std::endl;</code></p>"}; o painel de resposta do pl-multiple-choice (radio) é
    // "<li>(chave) html</li>" (pl-multiple-choice.mustache). O markdown do PL já foi processado: sem crases.
    const mdDisplay: string = answerKey(md.ir, 'mc').display_html;
    assert.equal(typeof mdDisplay, 'string', 'element/markdown: display_html de mc');
    assert.equal(
      norm(textOf(mdDisplay)),
      `(${md.snapshot.correct_answers.mc.key}) std::cout << "yes!" << std::endl;`,
      `element/markdown: display_html de mc: ${mdDisplay}`,
    );
    assert.deepEqual(textsOf(mdDisplay, 'code'), ['std::cout << "yes!" << std::endl;'], 'element/markdown: o gabarito de mc em <code>');
    assert.equal(withCode("console.log('no')").length, 1, `element/markdown: opção com console.log('no'): ${JSON.stringify(mdOptions)}`);
    const definitelyNot = mdOptions.filter((o: any) => textsOf(o.html, 'pre').some((t) => t.includes('std::cout << "Definitely not" << std::endl;')));
    assert.equal(definitelyNot.length, 1, `element/markdown: opção com o <pre> de "Definitely not": ${JSON.stringify(mdOptions)}`);
    assert.deepEqual(textsOf(definitelyNot[0].html, 'code'), [], 'o pl-code vira só o <pre>, sem <code>');

    // (variante determinística) [F] Oráculo (pl-oracle, PL a193f72): params e true_answer das
    // variantes 377 e 378 (element/markdown, question 123, seed abc), idênticas; o "\n" final dos dois
    // <p> é o que o markdown do PL grava.
    const mdA = { key: 'a', html: '<p><code>std::cout &lt;&lt; "yes!" &lt;&lt; std::endl;</code></p>\n', score: 1, feedback: null };
    assert.deepEqual(
      md.snapshot.params,
      {
        mc: [
          mdA,
          { key: 'b', html: '<pl-code language="cpp">std::cout &lt;&lt; "Definitely not" &lt;&lt; std::endl;</pl-code>', score: 0, feedback: null },
          { key: 'c', html: "<p><code>console.log('no')</code></p>\n", score: 0, feedback: null },
        ],
      },
      'element/markdown seed abc: snapshot.params (oráculo)',
    );
    assert.deepEqual(md.snapshot.correct_answers, { mc: mdA }, 'element/markdown seed abc: snapshot.correct_answers (oráculo)');
    // html canônico de cada opção, na ordem de params: o <p> e o <code> do
    // markdown ficam, com "<" escapado e o apóstrofo literal (o oráculo mostra "console.log(&#x27;no&#x27;)",
    // sem referência numérica); o pl-code (table.highlighttable do PL) vira só o <pre> do td.code, sem os spans do realce
    // e com o "\n" final que o pygments escreve (oráculo, variante 377).
    assert.deepEqual(mdOptions.map((o: any) => o.key), ['a', 'b', 'c'], 'element/markdown: chaves das opções (params.mc)');
    assert.equal(norm(mdOptions[0].html), '<p><code>std::cout &lt;&lt; "yes!" &lt;&lt; std::endl;</code></p>', `element/markdown: html da opção a: ${mdOptions[0].html}`);
    assert.deepEqual(textsOf(mdOptions[1].html, 'pre'), ['std::cout << "Definitely not" << std::endl;\n'], `element/markdown: <pre> da opção b: ${mdOptions[1].html}`);
    assert.equal(mdOptions[1].html.trim(), '<pre>std::cout &lt;&lt; "Definitely not" &lt;&lt; std::endl;\n</pre>', `element/markdown: html da opção b: ${mdOptions[1].html}`);
    assert.equal(norm(mdOptions[2].html), "<p><code>console.log('no')</code></p>", `element/markdown: html da opção c: ${mdOptions[2].html}`);

    // element-hides-answer: as alternativas de mc e cb (ordem aleatória, o padrão).
    const hides = await createOk(engine, TEST_COURSE, 'supported/element-hides-answer', 'abc');
    for (const [name, texts] of [['mc', ['Não', 'Sim']], ['cb', ['Certa', 'Errada']]] as Array<[string, string[]]>) {
      const got = field(hides.ir, name).options.map((o: any) => norm(textOf(o.html)));
      assert.deepEqual(got, hides.snapshot.params[name].map((p: any) => norm(textOf(p.html))), `element-hides-answer: textos de ${name} na ordem de params`);
      assert.deepEqual([...got].sort(), texts, `element-hides-answer: alternativas de ${name}`);
      // html canônico: o texto do pl-answer, sem envoltório, com o "ã" literal.
      const html = field(hides.ir, name).options.map((o: any) => norm(o.html));
      assert.deepEqual(html, got, `element-hides-answer: html das opções de ${name}`);
    }
  });
});

test('option-images: a opção com a.png tem o mesmo content_hash em variantes de ordens diferentes', async () => {
  await withEngine({}, async (engine) => {
    const orderOf = (body: any): string[] =>
      body.snapshot.params.mc.map((p: any) => /file-name="([^"]+)"/.exec(p.html)?.[1] ?? '?');
    const variants = await twoOrders(engine, TEST_COURSE, 'supported/option-images', orderOf);
    const hashes: string[] = [];
    for (const body of variants) {
      const media = mediaByFilename(body.ir, 'clientFilesQuestion/a.png');
      const options = field(body.ir, 'mc').options;
      const withA = options.filter((o: any) => o.html.includes(`src="cq-media:${media.id}"`));
      assert.equal(withA.length, 1, `exatamente uma opção deveria referenciar a mídia de a.png (${media.id})`);
      const paramA = body.snapshot.params.mc.find((p: any) => p.html.includes('file-name="a.png"'));
      assert.equal(withA[0].key, paramA.key, 'a opção com a.png deveria ter a chave exibida de params');
      assert.equal(
        withA[0].content_hash,
        expectedContentHash(withA[0].html, body.ir),
        'content_hash da opção com a.png pela regra do hash das opções (identidade estável da mídia)',
      );
      hashes.push(withA[0].content_hash);
    }
    // O id da mídia pode diferir entre as variantes; o hash não.
    assert.equal(hashes[0], hashes[1], 'o content_hash da opção com a.png deveria ser igual nas duas variantes');
  });
});

// ---------------------------------------------------------------------------------------------
// restrições do checkbox
// ---------------------------------------------------------------------------------------------

test('element/checkbox group_ans_restricted: min/max pela precedência do PL', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, EXAMPLE, 'element/checkbox', 'abc');
    assert.deepEqual(field(body.ir, 'group_ans_restricted').constraints, {
      allow_blank: false,
      min_select: 2,
      max_select: 4,
    });
  });
});

// ---------------------------------------------------------------------------------------------
// Fórmulas
// ---------------------------------------------------------------------------------------------

test('latex: fórmulas inline e block com o LaTeX exato; $ escapado, code e mathjax_ignore intactos', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/latex', 'abc');
    const html: string = body.ir.statement.html;
    const maths = mathOf(html);
    assert.deepEqual(
      maths.filter((m) => m.display === 'inline').map((m) => m.tex),
      ['x^2', 'y'],
      `fórmulas inline: ${JSON.stringify(maths)}`,
    );
    assert.deepEqual(
      maths.filter((m) => m.display === 'block').map((m) => m.tex),
      ['\\int_0^1 f', 'a+b', 'a %c\n+b'],
      `fórmulas em bloco: ${JSON.stringify(maths)}`,
    );
    const text = textOf(html);
    assert.ok(text.includes('$5'), `"$5" deveria aparecer como texto: ${text}`);
    assert.ok(!text.includes('\\$5'), 'o escape \\$ deveria virar $');
    assert.ok(!maths.some((m) => m.tex.includes('5')), '"$5" não pode virar fórmula');
    const $ = load(html);
    assert.ok(
      $('code').toArray().some((el) => $(el).text() === '$literal$'),
      '$literal$ deveria ficar dentro de um code',
    );
    assert.ok(text.includes('$ignorado$'), '$ignorado$ deveria ficar como texto');
    assert.ok(!maths.some((m) => m.tex.includes('ignorado') || m.tex.includes('literal')), 'code e mathjax_ignore não viram fórmula');
  });
});

// ---------------------------------------------------------------------------------------------
// Mídias
// ---------------------------------------------------------------------------------------------

test('media: estática, geradas, semeadura do file(), falha e ausente, id reusado na opção', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/media', 'abc');
    const ir = body.ir;
    const warnings: any[] = body.warnings;

    const figura = mediaByFilename(ir, 'clientFilesQuestion/figura.png');
    assert.equal(figura.origin, 'static');
    assert.equal(figura.content_type, 'image/png');
    assert.equal(figura.status, 'ok');
    const fileBytes = fs.readFileSync(path.join(SUPPORTED_DIR, 'media', 'clientFilesQuestion', 'figura.png'));
    assert.ok(decoded(figura).equals(fileBytes), 'bytes de figura.png diferentes do arquivo');
    assert.equal(figura.size_bytes, fileBytes.length, 'size_bytes de figura.png = tamanho do arquivo');

    const gerada = mediaByFilename(ir, 'gerada.png');
    assert.equal(gerada.origin, 'dynamic');
    assert.equal(gerada.content_type, 'image/png', 'content_type de gerada.png');
    assert.equal(gerada.status, 'ok');
    assert.ok(decoded(gerada).equals(Buffer.from(GERADA_PNG_BASE64, 'base64')), 'bytes de gerada.png diferentes do PNG fixo');
    assert.equal(gerada.size_bytes, Buffer.from(GERADA_PNG_BASE64, 'base64').length, 'size_bytes de gerada.png');

    const aleatorio = mediaByFilename(ir, 'aleatorio.png');
    assert.equal(aleatorio.origin, 'dynamic');
    assert.equal(aleatorio.content_type, 'image/png', 'content_type pela extensão');
    assert.equal(aleatorio.status, 'ok');
    const drawn = Number(decoded(aleatorio).toString('utf8'));
    assert.equal(
      drawn,
      body.snapshot.params.r0,
      'o file() deveria ser semeado como o generate (mesmo primeiro sorteio num executor recém-reiniciado)',
    );

    const falha = mediaByFilename(ir, 'falha.png');
    assert.equal(falha.origin, 'dynamic');
    assert.equal(falha.content_type, 'image/png', 'content_type pela extensão, mesmo ausente');
    assert.equal(falha.status, 'missing');
    assert.ok(warnings.some((w) => w.code === 'media_error'), `faltou o aviso media_error: ${JSON.stringify(warnings)}`);

    const ausente = mediaByFilename(ir, 'clientFilesQuestion/ausente.png');
    assert.equal(ausente.origin, 'static');
    assert.equal(ausente.content_type, 'image/png', 'content_type pela extensão, mesmo ausente');
    assert.equal(ausente.status, 'missing');
    assert.ok(warnings.some((w) => w.code === 'missing_media'), `faltou o aviso missing_media: ${JSON.stringify(warnings)}`);
    // um aviso por mídia afetada, sem saída de console (o erro do file() não é print);
    // media_error na fase file e missing_media na fase render.
    assert.deepEqual(warningPairs(body), ['media_error/file', 'missing_media/render'], `avisos exatos, com a fase: ${JSON.stringify(warnings)}`);
    // a mensagem é funcional, sem detalhe técnico: o texto da exceção do file()
    // (server.py: RuntimeError("falha proposital no file()")) não entra.
    for (const w of warnings) {
      assert.ok(!w.message.includes('falha proposital'), `mensagem do aviso ${w.code} com o texto da exceção do file(): ${w.message}`);
    }

    const src = `src="cq-media:${figura.id}"`;
    assert.ok(ir.statement.html.includes(src), `o enunciado deveria referenciar figura.png (${figura.id})`);
    // O enunciado referencia também as imagens geradas.
    assert.ok(ir.statement.html.includes(`src="cq-media:${gerada.id}"`), `o enunciado deveria referenciar gerada.png (${gerada.id})`);
    assert.ok(ir.statement.html.includes(`src="cq-media:${aleatorio.id}"`), `o enunciado deveria referenciar aleatorio.png (${aleatorio.id})`);
    const paramWithFig = body.snapshot.params.mc.find((p: any) => p.html.includes('figura.png'));
    assert.ok(paramWithFig, 'opção com figura.png ausente de params');
    const option = field(ir, 'mc').options.find((o: any) => o.key === paramWithFig.key);
    assert.ok(option.html.includes(src), `a opção deveria reusar o id ${figura.id} de figura.png: ${option.html}`);
    // o pl-figure da alternativa não tem alt: o <img> da opção sai sem o atributo, mesmo com a
    // mídia m1 tendo alt "Figura estática" no enunciado. A div do pl-figure é desembrulhada: a opção é o
    // texto e o <img> (PL a193f72: 'Com figura <div class="container-fluid mb-3"><img src="…"
    // class="img-fluid mx-auto d-block"></div>').
    assert.deepEqual(imagesOf(option.html), [['cq-media:m1', undefined]], `<img> da opção "Com figura", sem alt: ${option.html}`);
    assert.deepEqual(tagsOf(option.html), ['img'], `a opção "Com figura" só tem o <img> como tag: ${option.html}`);
    assert.equal(norm(field(ir, 'mc').options[1].html), 'Sem figura', 'html da opção "Sem figura"');
    assert.equal(option.content_hash, expectedContentHash(option.html, ir), 'content_hash da opção com figura.png');
    assert.ok(
      option.content_hash ===
        'sha256:' +
          createHash('sha256')
            .update(option.html.replace(src, 'src="cq-media:static:clientFilesQuestion/figura.png"').replace(/\s+/g, ' ').trim(), 'utf8')
            .digest('hex'),
      'o hash usa a identidade cq-media:static:clientFilesQuestion/figura.png',
    );

    // Textos alternativos preservados; figura sem alt → alt null.
    assert.equal(figura.alt, 'Figura estática', 'alt de figura.png');
    assert.equal(gerada.alt, 'Figura gerada', 'alt de gerada.png');
    assert.equal(aleatorio.alt, null, 'aleatorio.png não tem alt no question.html');
    assert.equal(falha.alt, null, 'falha.png não tem alt no question.html');
    assert.equal(ausente.alt, 'Ausente', 'alt da imagem comum ausente.png');

    // Numeração das mídias: m1… na ordem do enunciado; a opção e o gabarito reusam m1.
    assert.deepEqual(
      ir.media.map((m: any) => [m.id, m.filename]),
      [
        ['m1', 'clientFilesQuestion/figura.png'],
        ['m2', 'gerada.png'],
        ['m3', 'aleatorio.png'],
        ['m4', 'falha.png'],
        ['m5', 'clientFilesQuestion/ausente.png'],
      ],
      'numeração das mídias de media',
    );
    // (ir.schema.json) as mídias ausentes também são referenciadas pelo enunciado; alt no <img>.
    // aleatorio.png e falha.png não têm alt no question.html: o <img> sai sem o atributo, como no
    // pl-figure do PL ({{#alt}}alt="{{alt}}"{{/alt}}).
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [
        ['cq-media:m1', 'Figura estática'],
        ['cq-media:m2', 'Figura gerada'],
        ['cq-media:m3', undefined],
        ['cq-media:m4', undefined],
        ['cq-media:m5', 'Ausente'],
      ],
      'src e alt das imagens do enunciado, na ordem do documento',
    );

    // order="fixed": a opção com figura é a primeira.
    assert.deepEqual(
      field(ir, 'mc').options.map((o: any) => [o.key, norm(textOf(o.html))]),
      [['a', 'Com figura'], ['b', 'Sem figura']],
      'opções de mc (order="fixed")',
    );
    assert.deepEqual(mediaRefs(field(ir, 'mc').options[1].html), [], '"Sem figura" não tem imagem');
    // Snapshot: a correta é a primeira opção, com a figura (question.html; pl-multiple-choice.py prepare).
    assert.equal(body.snapshot.correct_answers.mc.key, 'a', 'correct_answers.mc.key');
    assert.ok(
      body.snapshot.correct_answers.mc.html.includes('Com figura') && body.snapshot.correct_answers.mc.html.includes('file-name="figura.png"'),
      `correct_answers.mc.html: ${body.snapshot.correct_answers.mc.html}`,
    );

    // o gabarito exibível é a opção correta, com a mesma mídia do enunciado.
    const display: string = answerKey(ir, 'mc').display_html;
    assert.equal(typeof display, 'string', 'display_html de mc');
    assert.deepEqual(mediaRefs(display), ['m1'], `display_html de mc reusa m1 (figura.png): ${display}`);
    assert.ok(textOf(display).includes('Com figura'), `display_html de mc mostra "Com figura": ${display}`);
    assert.ok(!textOf(display).includes('Sem figura'), `display_html de mc não mostra "Sem figura": ${display}`);
    // Painel de resposta do pl-multiple-choice: "<li>(a) Com figura <img></li>" (a figura sem alt).
    assert.equal(norm(textOf(display)), '(a) Com figura', `texto do display_html de mc: ${display}`);
    // [F] Painel "answer" do PL a193f72: '<li>(a) Com figura <div class="container-fluid mb-3"><img
    // src="/cfq/figura.png" class="img-fluid mx-auto d-block"></div></li>', sem alt. O alt da mídia m1
    // ("Figura estática", do enunciado) não passa para o <img> do gabarito.
    assert.deepEqual(imagesOf(display), [['cq-media:m1', undefined]], `<img> do gabarito de mc, sem alt: ${display}`);
    assert.doesNotMatch(display, /\salt=/, `o gabarito de mc não tem atributo alt: ${display}`);
  });
});

test('option-images: numeração, origem, tipo, alt, bytes, imagem de cada opção e gabarito', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/option-images', 'abc');
    const ir = body.ir;
    const alt: Record<string, string> = { 'a.png': 'Figura A', 'b.png': 'Figura B', 'c.png': 'Figura C' };
    const files: string[] = body.snapshot.params.mc.map((p: any) => /file-name="([^"]+)"/.exec(p.html)?.[1] ?? '?');
    // O enunciado não tem imagem: m1…m3 seguem a ordem de exibição das opções.
    assert.deepEqual(
      mediaSummary(ir),
      files.map((f, k) => [`m${k + 1}`, 'static', `clientFilesQuestion/${f}`, 'image/png', alt[f], 'ok']),
      'mídias de option-images',
    );
    for (const f of ['a.png', 'b.png', 'c.png']) {
      const bytes = fs.readFileSync(path.join(SUPPORTED_DIR, 'option-images', 'clientFilesQuestion', f));
      const media = mediaByFilename(ir, `clientFilesQuestion/${f}`);
      assert.ok(decoded(media).equals(bytes), `bytes de ${f} diferentes do arquivo`);
      assert.equal(media.size_bytes, bytes.length, `size_bytes de ${f}`);
    }
    assert.deepEqual(mediaRefs(ir.statement.html), [], 'o enunciado de option-images não tem imagem');
    const options = field(ir, 'mc').options;
    for (const [k, o] of options.entries()) {
      assert.deepEqual(mediaRefs(o.html), [`m${k + 1}`], `a opção ${o.key} referencia só a própria figura`);
      // <img src="cq-media:mN" alt="…">: o alt do pl-figure da alternativa; a
      // opção é só a figura, sem texto.
      assert.deepEqual(imagesOf(o.html), [[`cq-media:m${k + 1}`, alt[files[k]]]], `src e alt da imagem da opção ${o.key}`);
      assert.equal(norm(textOf(o.html)), '', `a opção ${o.key} não tem texto: ${o.html}`);
    }
    // Gabarito: a opção com a.png (correct="true").
    const a = mediaByFilename(ir, 'clientFilesQuestion/a.png');
    assert.ok(body.snapshot.correct_answers.mc.html.includes('file-name="a.png"'), 'correct_answers.mc é a opção de a.png');
    assert.equal(
      body.snapshot.correct_answers.mc.key,
      body.snapshot.params.mc.find((p: any) => p.html.includes('file-name="a.png"')).key,
      'correct_answers.mc.key é a chave exibida de a.png',
    );
    const display: string = answerKey(ir, 'mc').display_html;
    assert.equal(typeof display, 'string', 'display_html de mc');
    assert.deepEqual(mediaRefs(display), [a.id], `display_html de mc mostra só a.png (${a.id}): ${display}`);
    // Painel de resposta do pl-multiple-choice: "<li>(chave) <img alt="Figura A"></li>".
    assert.equal(norm(textOf(display)), `(${body.snapshot.correct_answers.mc.key})`, `texto do display_html de mc: ${display}`);
    assert.deepEqual(imagesOf(display), [[`cq-media:${a.id}`, 'Figura A']], `imagem do display_html de mc: ${display}`);
  });
});

test('big-media: mídia gerada grande com os bytes do file()', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/big-media', 'abc');
    const ir = body.ir;
    assert.deepEqual(mediaSummary(ir), [['m1', 'dynamic', 'grande.png', 'image/png', 'Figura grande', 'ok']], 'mídias de big-media');
    // server.py: GRANDE = bytes(i % 251 for i in range(200000)).
    const expected = Buffer.from(Array.from({ length: 200000 }, (_v, k) => k % 251));
    assert.ok(decoded(ir.media[0]).equals(expected), 'bytes de grande.png diferentes dos do file()');
    assert.equal(ir.media[0].size_bytes, 200000, 'size_bytes de grande.png');
    assert.deepEqual(imagesOf(ir.statement.html), [['cq-media:m1', 'Figura grande']], 'imagem do enunciado de big-media');
  });
});

test('ir-media-extra: nomes decodificados, <img> gerada fora do pl-figure, file() vazio e figura só na explicação', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-extra', 'abc');
    const ir = body.ir;
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'dynamic', 'com espaço.png', 'image/png', 'Gerada com espaço', 'ok'],
        ['m2', 'static', 'clientFilesQuestion/figura dois.png', 'image/png', 'Estática com espaço', 'ok'],
        ['m3', 'dynamic', 'img-gerada.png', 'image/png', 'Imagem gerada', 'ok'],
        ['m4', 'dynamic', 'vazia.png', 'image/png', null, 'missing'],
        ['m5', 'static', 'clientFilesQuestion/explicacao.png', 'image/png', 'Figura da explicação', 'ok'],
      ],
      'mídias de ir-media-extra (nome decodificado; a da explicação depois das do enunciado)',
    );
    for (const m of ir.media) assert.ok(!m.filename.includes('%'), `filename sem codificação de URL: ${m.filename}`);
    // Bytes fixos do server.py da fixture e arquivos de clientFilesQuestion.
    assert.ok(decoded(ir.media[0]).equals(Buffer.from('CQ-IR-MIDIA-COM-ESPACO')), 'o file() recebeu o nome decodificado "com espaço.png"');
    assert.ok(decoded(ir.media[2]).equals(Buffer.from('CQ-IR-MIDIA-IMG-GERADA')), 'bytes de img-gerada.png');
    const dir = path.join(SUPPORTED_DIR, 'ir-media-extra', 'clientFilesQuestion');
    for (const [k, f] of [[1, 'figura dois.png'], [4, 'explicacao.png']] as Array<[number, string]>) {
      const bytes = fs.readFileSync(path.join(dir, f));
      assert.ok(decoded(ir.media[k]).equals(bytes), `bytes de ${f} diferentes do arquivo`);
      assert.equal(ir.media[k].size_bytes, bytes.length, `size_bytes de ${f}`);
    }
    // file() vazio: status missing, sem conteúdo.
    assert.ok(!('data_base64' in ir.media[3]), 'vazia.png não tem data_base64');
    // alt de cada <img> do enunciado (question.html); vazia.png não tem alt, e o <img> sai sem o atributo
    // (pl-figure.mustache do PL a193f72: {{#alt}}alt="{{alt}}"{{/alt}}).
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [['cq-media:m1', 'Gerada com espaço'], ['cq-media:m2', 'Estática com espaço'], ['cq-media:m3', 'Imagem gerada'], ['cq-media:m4', undefined]],
      'src e alt das imagens do enunciado',
    );
    assert.notEqual(ir.explanation, null, 'o pl-answer-panel deveria virar explanation');
    assert.deepEqual(mediaRefs(ir.explanation.html), ['m5'], `explanation.html referencia explicacao.png: ${ir.explanation.html}`);
    assert.deepEqual(imagesOf(ir.explanation.html), [['cq-media:m5', 'Figura da explicação']], 'src e alt da imagem da explicação');
    assert.equal(ir.explanation.can_show, true, 'explanation.can_show');
    assert.equal(norm(textOf(ir.explanation.html)), 'Explicação com figura.', `texto da explicação (pl-answer-panel): ${ir.explanation.html}`);
    // HTML serializado da explicação: "ç" e "ã" literais no texto e no alt.
    assert.ok(ir.explanation.html.includes('<p>Explicação com figura.</p>'), `explanation.html (HTML): ${ir.explanation.html}`);
    assert.ok(ir.explanation.html.includes('alt="Figura da explicação"'), `alt literal na explicação: ${ir.explanation.html}`);
    // um único aviso: o file() de vazia.png devolve vazio → media_error na fase file.
    assert.deepEqual(warningPairs(body), ['media_error/file'], `avisos de ir-media-extra: ${JSON.stringify(body.warnings)}`);
  });
});

test('EXAMPLE element/figure seed abc: mídias estáticas e geradas, referências no enunciado, nas opções e no gabarito', async () => {
  // Timeout maior e aquecimento: o matplotlib monta o cache de fontes em /tmp na primeira vez.
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '30000' }, async (engine) => {
    ok200(await engine.createVariant({ question_ref: ref(EXAMPLE, 'element/figure'), seed: 'abc' }));
    const body = await createOk(engine, EXAMPLE, 'element/figure', 'abc');
    const media: any[] = body.ir.media;
    assert.ok(media.some((m) => m.origin === 'static' && m.status === 'ok'), `faltou mídia estática ok: ${JSON.stringify(media.map((m) => [m.origin, m.filename, m.status]))}`);
    assert.ok(media.some((m) => m.origin === 'dynamic' && m.status === 'ok'), `faltou mídia gerada ok: ${JSON.stringify(media.map((m) => [m.origin, m.filename, m.status]))}`);
    checkSpec('element/figure', body, 'element/figure seed abc');
    // (variante determinística) [F] Oráculo (pl-oracle, PL a193f72): params e true_answer das
    // variantes 624 e 625 (element/figure, question 115, seed abc), idênticas.
    const selectThis = {
      key: 'a',
      html: 'Select this: <pl-figure file-name="true.png" display="inline" alt="Slider set to true"></pl-figure>',
      score: 1,
      feedback: null,
    };
    assert.deepEqual(
      body.snapshot.params,
      {
        booleans: [
          selectThis,
          { key: 'b', html: 'Not this: <pl-figure file-name="false.png" display="inline" alt="Slider set to false"></pl-figure>', score: 0, feedback: null },
        ],
      },
      'element/figure seed abc: snapshot.params (oráculo)',
    );
    assert.deepEqual(body.snapshot.correct_answers, { booleans: selectThis }, 'element/figure seed abc: snapshot.correct_answers (oráculo)');

    const ir = body.ir;
    // [F] question.html (pl-oracle): sin-x.png duas vezes, cos-x.png gerada, true.png e false.png em
    // linha no enunciado; as opções de booleans reusam true.png e false.png.
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'static', 'clientFilesQuestion/sin-x.png', 'image/png', 'Graph of a sine wave', 'ok'],
        ['m2', 'dynamic', 'cos-x.png', 'image/png', 'Graph of 1*cos(x) function', 'ok'],
        ['m3', 'static', 'clientFilesQuestion/true.png', 'image/png', 'Slider set to true', 'ok'],
        ['m4', 'static', 'clientFilesQuestion/false.png', 'image/png', 'Slider set to false', 'ok'],
      ],
      'mídias de element/figure',
    );
    for (const [k, f] of [[0, 'sin-x.png'], [2, 'true.png'], [3, 'false.png']] as Array<[number, string]>) {
      const bytes = fs.readFileSync(path.join(exampleDir('element/figure'), 'clientFilesQuestion', f));
      assert.ok(decoded(ir.media[k]).equals(bytes), `bytes de ${f} diferentes do arquivo do exampleCourse`);
      assert.equal(ir.media[k].size_bytes, bytes.length, `size_bytes de ${f}`);
    }
    // cos-x.png como o PL a produz com a mesma seed, num executor recém-reiniciado. [F]
    // Oráculo: element/figure é a question 115; as variantes 624 e 625 têm seed abc, e três downloads de
    // GET /pl/course/1/question/115/generatedFilesQuestion/variant/624/cos-x.png (2026-09-25) deram os
    // mesmos 31614 bytes, com este sha256 (o coeficiente de random.randint(1, 5) muda o gráfico).
    const cos = mediaByFilename(ir, 'cos-x.png');
    assert.equal(
      createHash('sha256').update(decoded(cos)).digest('hex'),
      '6381d8b9aedadc65767c9f02afdc2b50f8913b48c5ec498ed7ac455edb2c2e5f',
      'bytes de cos-x.png diferentes dos do oráculo na seed abc',
    );
    assert.equal(cos.size_bytes, 31614, 'size_bytes de cos-x.png (oráculo, seed abc)');
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [
        ['cq-media:m1', 'Graph of a sine wave'],
        ['cq-media:m1', 'Graph of a sine wave'],
        ['cq-media:m2', 'Graph of 1*cos(x) function'],
        ['cq-media:m3', 'Slider set to true'],
        ['cq-media:m4', 'Slider set to false'],
      ],
      'imagens do enunciado de element/figure (sin-x.png reusa m1)',
    );
    const options = field(ir, 'booleans').options;
    const byText = (text: string): any => {
      const list = options.filter((o: any) => norm(textOf(o.html)) === text);
      assert.equal(list.length, 1, `booleans: exatamente uma opção "${text}": ${JSON.stringify(options)}`);
      return list[0];
    };
    assert.deepEqual(mediaRefs(byText('Select this:').html), ['m3'], 'a opção "Select this:" reusa true.png (m3)');
    assert.deepEqual(mediaRefs(byText('Not this:').html), ['m4'], 'a opção "Not this:" reusa false.png (m4)');
    // o <img> da opção leva o alt do pl-figure (pl-figure.mustache repassa o alt).
    assert.deepEqual(imagesOf(byText('Select this:').html), [['cq-media:m3', 'Slider set to true']], 'src e alt da imagem de "Select this:"');
    assert.deepEqual(imagesOf(byText('Not this:').html), [['cq-media:m4', 'Slider set to false']], 'src e alt da imagem de "Not this:"');
    const display: string = answerKey(ir, 'booleans').display_html;
    assert.equal(typeof display, 'string', 'display_html de booleans');
    assert.deepEqual(mediaRefs(display), ['m3'], `o gabarito mostra true.png (m3): ${display}`);
    assert.ok(textOf(display).includes('Select this:'), `o gabarito mostra "Select this:": ${display}`);
    assert.ok(!textOf(display).includes('Not this:'), `o gabarito não mostra "Not this:": ${display}`);
    // Painel de resposta do pl-multiple-choice: "<li>(chave) Select this: <img></li>" (oráculo, variante
    // 624: a correta tem a chave "a" na seed abc).
    assert.equal(
      norm(textOf(display)),
      `(${body.snapshot.correct_answers.booleans.key}) Select this:`,
      `texto do display_html de booleans: ${display}`,
    );
    assert.deepEqual(imagesOf(display), [['cq-media:m3', 'Slider set to true']], `imagem do display_html de booleans: ${display}`);

    // [F] question.html (pl-oracle): o texto do painel da questão, com os cabeçalhos dos
    // pl-card; "Select this:" e "Not this:" ficam dentro do pl-multiple-choice, que vira cq-field; o
    // pl-figure em linha não tem texto ("Select <img> instead of <img> from below.").
    const text = norm(textOf(ir.statement.html));
    for (const snippet of [
      'The examples below showcase different features of pl-figure.',
      'Part 1',
      'Part 2',
      'Part 3',
      'Part 4',
      'Here is an example of a dynamic figure.',
      'Select instead of from below.',
      'Use display="inline" to embed a figure inline with the rest of the elements, like texts.',
    ]) {
      assert.ok(text.includes(snippet), `element/figure: o enunciado deveria conter ${JSON.stringify(snippet)}: ${text}`);
    }
    for (const absent of ['Select this:', 'Not this:']) {
      assertAbsent(ir.statement.html, absent, 'element/figure statement.html (as alternativas ficam no campo)');
    }
    // (quebra de linha) os 2 <br> do question.html: um no painel da parte 4 e outro depois do
    // pl-multiple-choice; pl-card.mustache e pl-figure.mustache não têm <br>.
    assert.equal(load(ir.statement.html)('br').length, 2, `element/figure: número de <br> no enunciado: ${ir.statement.html}`);
  });
});

test('empty-answer-render: resposta que o PL renderiza vazia não pode ser mostrada', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/empty-answer-render', 'abc');
    const sem = answerKey(body.ir, 'sem');
    assert.equal(sem.can_show, false, '"sem" não tem resposta correta: o painel de resposta do PL sai vazio');
    assert.equal(sem.display_html, null, '"sem" não tem forma exibível');
    assert.equal(sem.correct_value, null, 'correct_value de um campo sem gabarito é null');
    const v = answerKey(body.ir, 'v');
    assert.equal(v.can_show, true, '"v" tem resposta correta e pode ser mostrado');
    assert.equal(typeof v.display_html, 'string', '"v" tem forma exibível');
    assert.equal(displayText(body.ir, 'v'), '1', 'display_html de "v" mostra o gabarito 1');
  });
});

// ---------------------------------------------------------------------------------------------
// Gabarito exibível e can_show
// ---------------------------------------------------------------------------------------------

/**
 * [F] Painel "Correct answer" do oráculo para element/numberInput (question 128; variantes 6, 7 e 174),
 * canonicalizado: "$c = $" vira a fórmula "c = ", <samp> e <span> saem.
 */
const NUMBER_DISPLAY: Record<string, Record<string, string>> = {
  '9ix': {
    c_1: '8.24621125124', c_2: 'c = 8.24621125124', c_3: 'c = 8.2 units', c_4: 'c = 8.2462 units',
    c_5: 'c = 8.24621125124 units', c_m: '2', c_b: '5', c_6: 'c =', c_6_blank: 'c = 1', c_7: '+8.24621',
  },
  '17we19m': {
    c_1: '5', c_2: 'c = 5', c_3: 'c = 5.0 units', c_4: 'c = 5.0000 units',
    c_5: 'c = 5 units', c_m: '2', c_b: '5', c_6: 'c =', c_6_blank: 'c = 1', c_7: '+5.00000',
  },
  grnr17: {
    c_1: '9.48683298051', c_2: 'c = 9.48683298051', c_3: 'c = 9.5 units', c_4: 'c = 9.4868 units',
    c_5: 'c = 9.48683298051 units', c_m: '2', c_b: '5', c_6: 'c =', c_6_blank: 'c = 1', c_7: '+9.48683',
  },
};

/**
 * [F] Idem para element/integerInput (question 122; variantes 8, 9 e 194). c_6_blank fica de fora: a
 * renderização dele é vazia (display_html null, conferido à parte).
 */
const INTEGER_DISPLAY: Record<string, Record<string, string>> = {
  '9ix': {
    c_1: '10', c_2: 'c = 10 units', c_4: '10', c_5: '10', c_6: '0',
    c_7_b16: '28', c_7_b2: '101000', c_large: '4193ca54ee897110', c_np: '16',
  },
  '17we19m': {
    c_1: '7', c_2: 'c = 7 units', c_4: '7', c_5: '7', c_6: '0',
    c_7_b16: '28', c_7_b2: '101000', c_large: '5aa639589389f5b9', c_np: '16',
  },
  grnr17: {
    c_1: '12', c_2: 'c = 12 units', c_4: '12', c_5: '12', c_6: '0',
    c_7_b16: '2E', c_7_b2: '101110', c_large: '3fe63c782dfec421', c_np: '16',
  },
};

/**
 * [F] element/stringInput (question 142; variantes 10, 11 e 214): o painel mostra rótulo, o gabarito
 * em <code> e o sufixo; o botão do popover sai com o conteúdo.
 */
const STRING_DISPLAY: Record<string, [string, string]> = {
  ans1: ['a =', ''],
  ans2: ['b + c =', ''],
  ans3: ['', ''],
  ans4: ['', ''],
  ans5: ['blank input box =', ''],
  ans6: ['Input box is case insensitive =', ''],
  ans7: ['Small box =', ''],
  ans8: ['Weird text =', '?'],
};

/** Escolhas com hide-letter-keys="true": o painel mostra a alternativa sem "(x)". */
const HIDE_LETTER_KEYS = new Set(['sky-color4', 'group_ans_v8', 'group_ans_v9', 'group_ans_v10']);

/**
 * [F] Painel de resposta de pl-multiple-choice e pl-checkbox no oráculo (questions 127 e 108, as 3
 * seeds; conferido contra true_answer): cada alternativa correta como "(chave) texto", ou só o texto
 * com hide-letter-keys, na ordem de correct_answers. O "$…$" vira cq-math, cujo texto não tem "$".
 */
function choiceItem(name: string, answer: any): string {
  const text = norm(textOf(answer.html).replace(/\$/g, ''));
  return HIDE_LETTER_KEYS.has(name) ? text : `(${answer.key}) ${text}`;
}

test('as 5 referências nas seeds de seeds(3): display_html de cada campo é o painel de resposta do PL (oráculo)', async () => {
  await withEngine({}, async (engine) => {
    for (const seed of seeds(3)) {
      const ni = await createOk(engine, EXAMPLE, 'element/numberInput', seed);
      for (const [name, expected] of Object.entries(NUMBER_DISPLAY[seed])) {
        assert.equal(displayText(ni.ir, name), expected, `numberInput seed ${seed}: display_html de ${name}`);
      }
      for (const name of ['c_2', 'c_3', 'c_4', 'c_5', 'c_6', 'c_6_blank']) {
        assert.deepEqual(mathOf(answerKey(ni.ir, name).display_html), [{ display: 'inline', tex: 'c = ' }], `numberInput seed ${seed}: rótulo de ${name} no gabarito`);
      }

      const ii = await createOk(engine, EXAMPLE, 'element/integerInput', seed);
      for (const [name, expected] of Object.entries(INTEGER_DISPLAY[seed])) {
        assert.equal(displayText(ii.ir, name), expected, `integerInput seed ${seed}: display_html de ${name}`);
      }
      assert.deepEqual(mathOf(answerKey(ii.ir, 'c_2').display_html), [{ display: 'inline', tex: 'c = ' }], `integerInput seed ${seed}: rótulo de c_2 no gabarito`);
      // c_6_blank: correct-answer="" grava correct_answers "" (pl-integer-input.py, prepare),
      // e o painel de resposta é <span class="d-inline-block"><samp></samp></span>, vazio depois da
      // canonicalização: sem forma exibível.
      const blank = answerKey(ii.ir, 'c_6_blank');
      assert.equal(blank.can_show, false, `integerInput seed ${seed}: can_show de c_6_blank (renderização vazia)`);
      assert.equal(blank.display_html, null, `integerInput seed ${seed}: display_html de c_6_blank`);
      assert.equal(blank.correct_value, '', `integerInput seed ${seed}: correct_value de c_6_blank (correct-answer="")`);

      const si = await createOk(engine, EXAMPLE, 'element/stringInput', seed);
      const siAnswers = si.snapshot.correct_answers;
      for (const [name, [label, suffix]] of Object.entries(STRING_DISPLAY)) {
        assert.equal(displayText(si.ir, name), norm(`${label} ${siAnswers[name]} ${suffix}`), `stringInput seed ${seed}: display_html de ${name}`);
        assert.deepEqual(textsOf(answerKey(si.ir, name).display_html, 'code'), [siAnswers[name]], `stringInput seed ${seed}: gabarito de ${name} em <code>`);
      }
      // ans9 (multiline): no painel de resposta, o gabarito fica num <textarea>, que no
      // display_html vira <pre> com o mesmo texto.
      const ans9: string = answerKey(si.ir, 'ans9').display_html;
      assert.equal(typeof ans9, 'string', `stringInput seed ${seed}: display_html de ans9`);
      assert.deepEqual(textsOf(ans9, 'pre'), [siAnswers.ans9], `stringInput seed ${seed}: o <textarea> de ans9 vira <pre> com o gabarito: ${ans9}`);
      const $ans9 = load(ans9);
      $ans9('pre').remove();
      assert.equal(norm($ans9.root().text()), "Program output Don't worry about trailing newlines.", `stringInput seed ${seed}: rótulo e sufixo de ans9`);
      // HTML serializado: o <pre> guarda as quebras de linha do gabarito ("I'm\nsorry\nDave.",
      // server.py) e o apóstrofo literal; o sufixo multiline (HTML canonicalizado) também sai com o
      // apóstrofo literal.
      assert.ok(ans9.includes(`<pre>${siAnswers.ans9}</pre>`), `stringInput seed ${seed}: <pre> de ans9 no HTML: ${ans9}`);
      assert.ok(ans9.includes("Don't worry about trailing newlines."), `stringInput seed ${seed}: sufixo de ans9 no HTML: ${ans9}`);
      // ans10 fica em pl-hide-in-panel answer="true" (can_show falso), mas o display_html é a
      // renderização da própria tag no painel de resposta. [F] PL a193f72, pl-string-input.py (render,
      // panel "answer"): com correct-answer-format="regex" e ignore-case="true", o gabarito ganha "(?i)" na
      // frente, e o template mostra 'Any answer matching the <a>regular expression</a> <code>…</code>'
      // (o <a> é desembrulhado no HTML canônico).
      const ans10Regex = '(?x) N(itrogen)? | P(hosphorus)? | K | potassium # (?x) verbose regex';
      assert.equal(siAnswers.ans10, ans10Regex, `stringInput seed ${seed}: correct-answer de ans10 (question.html)`);
      assert.equal(
        displayText(si.ir, 'ans10'),
        norm(`Any answer matching the regular expression (?i)${ans10Regex}`),
        `stringInput seed ${seed}: display_html de ans10`,
      );
      assert.deepEqual(textsOf(answerKey(si.ir, 'ans10').display_html, 'code'), [`(?i)${ans10Regex}`], `stringInput seed ${seed}: gabarito de ans10 em <code>`);

      const mcBody = await createOk(engine, EXAMPLE, 'element/multipleChoice', seed);
      for (const s of SPECS['element/multipleChoice'].fields) {
        assert.equal(
          displayText(mcBody.ir, s.name),
          choiceItem(s.name, mcBody.snapshot.correct_answers[s.name]),
          `multipleChoice seed ${seed}: display_html de ${s.name}`,
        );
      }

      const cbBody = await createOk(engine, EXAMPLE, 'element/checkbox', seed);
      for (const s of SPECS['element/checkbox'].fields) {
        const display: string = answerKey(cbBody.ir, s.name).display_html;
        assert.equal(typeof display, 'string', `checkbox seed ${seed}: display_html de ${s.name}`);
        assert.deepEqual(
          textsOf(display, 'li').map(norm),
          cbBody.snapshot.correct_answers[s.name].map((c: any) => choiceItem(s.name, c)),
          `checkbox seed ${seed}: display_html de ${s.name} lista só as corretas, na ordem`,
        );
      }
    }
  });
});

test('fixtures: display_html mostra o gabarito do painel de resposta do PL', async () => {
  await withEngine({}, async (engine) => {
    // pl-integer-input (oráculo: <samp>valor</samp>) com gabarito 1.
    for (const qid of [
      'supported/panels', 'supported/console-output', 'supported/single-variant', 'supported/big-media',
      'supported/preferences', 'supported/parse-changes-params', 'supported/grade-changes-params',
      'supported/ir-submission-panel', 'supported/ir-media-extra', 'supported/grade-print',
      'supported/variant-console-phases', 'supported/ir-media-escape', 'supported/ir-media-types',
      'supported/ir-hidden-question-field', 'supported/ir-console-echo', 'supported/ir-media-subdir',
      'supported/ir-media-executor',
    ]) {
      const body = await createOk(engine, TEST_COURSE, qid, 'abc');
      assert.equal(displayText(body.ir, 'v'), '1', `${qid}: display_html de v`);
    }
    for (const qid of [
      'supported/server-grade-score', 'supported/server-grade-partial', 'supported/server-grade-format-error',
      'supported/server-grade-score-out-of-range', 'supported/grade-server-feedback',
    ]) {
      const body = await createOk(engine, TEST_COURSE, qid, 'abc');
      assert.equal(displayText(body.ir, 'v'), '7', `${qid}: display_html de v`);
    }
    // pl-string-input sem rótulo, gabarito "A" (server.py): <code>A</code>; o botão do popover sai.
    const parse = await createOk(engine, TEST_COURSE, 'supported/grade-server-parse', 'abc');
    assert.equal(displayText(parse.ir, 'v'), 'A', 'grade-server-parse: display_html de v');
    assert.deepEqual(textsOf(answerKey(parse.ir, 'v').display_html, 'code'), ['A'], 'grade-server-parse: gabarito em <code>');
    const hides = await createOk(engine, TEST_COURSE, 'supported/element-hides-answer', 'abc');
    assert.equal(displayText(hides.ir, 'visivel'), '1', 'element-hides-answer: display_html de visivel');
    const noPartial = await createOk(engine, TEST_COURSE, 'supported/no-partial-credit', 'abc');
    assert.equal(displayText(noPartial.ir, 'a'), '1', 'no-partial-credit: display_html de a');
    assert.equal(displayText(noPartial.ir, 'b'), '2', 'no-partial-credit: display_html de b');
    // pl-string-input sem rótulo: o gabarito em <code>.
    const echo = await createOk(engine, TEST_COURSE, 'supported/parse-echo', 'abc');
    assert.equal(displayText(echo.ir, 't'), 'a', 'parse-echo: display_html de t');
    // pl-number-input com label="$z =$" e gabarito 1 (format_true_ans com ".12g").
    const latex = await createOk(engine, TEST_COURSE, 'supported/latex', 'abc');
    assert.equal(displayText(latex.ir, 'z'), 'z = 1', 'latex: display_html de z');
    assert.deepEqual(mathOf(answerKey(latex.ir, 'z').display_html), [{ display: 'inline', tex: 'z =' }], 'latex: rótulo de z no gabarito');
    const random = await createOk(engine, TEST_COURSE, 'supported/random-params', 'abc');
    assert.equal(displayText(random.ir, 'v'), String(random.snapshot.params.r), 'random-params: display_html de v = r sorteado');
  });
});

test('no-show-correct-answer: nenhum gabarito nem explicação pode ser mostrado', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/no-show-correct-answer', 'abc');
    assert.ok(body.ir.answer_key.length > 0, 'gabarito vazio');
    for (const a of body.ir.answer_key) assert.equal(a.can_show, false, `can_show de ${a.name}`);
    assert.notEqual(body.ir.explanation, null, 'a explicação deveria estar na IR');
    assert.equal(body.ir.explanation.can_show, false, 'explanation.can_show');
    // showCorrectAnswer muda o can_show, não o conteúdo.
    assert.equal(norm(textOf(body.ir.explanation.html)), 'Explicação', 'conteúdo do pl-answer-panel na explicação');
    // o "ç" e o "ã" saem literais no HTML serializado.
    assert.equal(norm(body.ir.explanation.html), '<p>Explicação</p>', `explanation.html de no-show-correct-answer: ${body.ir.explanation.html}`);
    assert.equal(answerKey(body.ir, 'v').correct_value, 1, 'correct_value de v (server.py)');
    // com can_show falso por showCorrectAnswer, o display_html é a renderização da tag no painel
    // de resposta: <samp>1</samp> (pl-integer-input.py; PL a193f72).
    assert.equal(displayText(body.ir, 'v'), '1', 'display_html de v (com can_show falso)');
  });
});

test('single-variant: can_show continua true (a regra da variante única é do núcleo)', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/single-variant', 'abc');
    assert.equal(answerKey(body.ir, 'v').can_show, true);
    assert.equal(answerKey(body.ir, 'v').correct_value, 1, 'correct_value de v (server.py)');
  });
});

test('element-hides-answer: can_show por elemento e por painel', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/element-hides-answer', 'abc');
    const ir = body.ir;
    assert.equal(answerKey(ir, 'num').can_show, false, 'pl-number-input show-correct-answer="false"');
    assert.equal(answerKey(ir, 'cb').can_show, false, 'pl-checkbox hide-answer-panel="true"');
    assert.equal(answerKey(ir, 'txt').can_show, false, 'pl-string-input dentro de pl-hide-in-panel answer="true"');
    const mc = answerKey(ir, 'mc');
    assert.equal(mc.can_show, false, 'pl-multiple-choice builtin-grading="false"');
    assert.equal(mc.display_html, null, 'pl-multiple-choice builtin-grading="false" não tem forma exibível');
    assert.equal(answerKey(ir, 'dentro').can_show, false, 'pl-integer-input dentro de pl-question-panel');
    assert.equal(answerKey(ir, 'visivel').can_show, true, 'pl-integer-input fora de painéis');

    // [F] PL a193f72: no painel "answer", pl-number-input.py devolve "" com show-correct-answer="false"
    // e pl-checkbox.py devolve "" com hide-answer-panel="true": sem forma exibível.
    assert.equal(answerKey(ir, 'num').display_html, null, 'display_html de num');
    assert.equal(answerKey(ir, 'cb').display_html, null, 'display_html de cb');
    // com can_show falso por um ancestral, o display_html é a renderização da própria tag no
    // painel de resposta [F] (PL a193f72): pl-integer-input → <samp>1</samp>; pl-string-input →
    // <code>a</code> (o botão do popover sai com o conteúdo).
    assert.equal(displayText(ir, 'dentro'), '1', 'display_html de dentro (em pl-question-panel; com can_show falso)');
    assert.equal(displayText(ir, 'txt'), 'a', 'display_html de txt (em pl-hide-in-panel answer="true"; com can_show falso)');
    assert.deepEqual(textsOf(answerKey(ir, 'txt').display_html, 'code'), ['a'], 'display_html de txt: o gabarito em <code>');

    // correct_value cru mesmo quando o gabarito não pode ser mostrado (server.py; correct="true").
    assert.equal(answerKey(ir, 'dentro').correct_value, 1, 'correct_value de dentro');
    assert.equal(answerKey(ir, 'num').correct_value, 1, 'correct_value de num');
    assert.equal(answerKey(ir, 'txt').correct_value, 'a', 'correct_value de txt');
    assert.equal(answerKey(ir, 'visivel').correct_value, 1, 'correct_value de visivel');
    const mcValue = mc.correct_value;
    assert.equal(norm(textOf(mcValue.html)), 'Sim', 'correct_value de mc é a alternativa "Sim"');
    assert.equal(
      mcValue.key,
      body.snapshot.params.mc.find((p: any) => norm(textOf(p.html)) === 'Sim').key,
      'correct_value.key de mc é a chave exibida de "Sim"',
    );
    assert.deepEqual(
      answerKey(ir, 'cb').correct_value.map((c: any) => norm(textOf(c.html))),
      ['Certa'],
      'correct_value de cb é a lista das corretas',
    );
  });
});

test('ir-submission-panel: campo dentro de pl-submission-panel não pode ser mostrado', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-submission-panel', 'abc');
    const ir = body.ir;
    assert.deepEqual(ir.fields.map((f: any) => f.name), ['sub', 'v'], 'campos na ordem do documento');
    assert.equal(answerKey(ir, 'sub').can_show, false, 'pl-integer-input dentro de pl-submission-panel');
    assert.equal(answerKey(ir, 'v').can_show, true, 'pl-integer-input fora de painéis');
    assert.equal(answerKey(ir, 'sub').correct_value, 1, 'correct_value de sub (server.py)');
    // o pl-submission-panel não entra no enunciado: "sub" não tem cq-field.
    assert.deepEqual(markersOf(ir.statement.html), ['v'], `cq-field do enunciado: só o de v: ${ir.statement.html}`);
    assert.equal(
      norm(textOf(ir.statement.html)),
      'Um campo fica dentro do painel de submissão.',
      `texto do enunciado (só o pl-question-panel): ${ir.statement.html}`,
    );
    // a renderização da própria tag de "sub" no painel de resposta: <samp>1</samp>.
    assert.equal(displayText(ir, 'sub'), '1', 'display_html de sub (com can_show falso)');
    // sem marcador no enunciado, "sub" continua campo e nome de resposta da inspeção.
    const insp = ok200(await engine.inspect({ question_ref: ref(TEST_COURSE, 'supported/ir-submission-panel') }));
    assert.deepEqual(insp.question.answers_names, ['sub', 'v'], 'answers_names da inspeção = fields[*].name');
  });
});

test('EXAMPLE element/stringInput: ans10 (em pl-hide-in-panel answer) não pode ser mostrado', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, EXAMPLE, 'element/stringInput', 'abc');
    assert.equal(answerKey(body.ir, 'ans10').can_show, false);
  });
});

// ---------------------------------------------------------------------------------------------
// Fixtures supported/ir-* (mídias, rótulo com marcação, campo oculto)
// ---------------------------------------------------------------------------------------------

/** content_hash de uma opção pela regra do hash das opções, com a identidade da mídia escrita à mão. */
function hashWithIdentity(html: string, identity: string): string {
  const stable = html.replace(/src="cq-media:m[0-9]+"/g, `src="cq-media:${identity}"`);
  return 'sha256:' + createHash('sha256').update(norm(stable), 'utf8').digest('hex');
}

test('ir-course-media: x.png de clientFilesQuestion e de clientFilesCourse não se confundem na mídia nem no hash', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-course-media', 'abc');
    const ir = body.ir;
    // [F] PL a193f72, pl-figure.py: directory="clientFilesCourse" lê de client_files_course_path e monta o
    // src com client_files_course_url; o <img> com options.client_files_course_url aponta para a mesma
    // mídia. filename = <diretório-base>/<caminho>.
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'static', 'clientFilesQuestion/x.png', 'image/png', 'Questão', 'ok'],
        ['m2', 'static', 'clientFilesCourse/x.png', 'image/png', 'Curso', 'ok'],
      ],
      'mídias de ir-course-media',
    );
    const questionBytes = fs.readFileSync(path.join(SUPPORTED_DIR, 'ir-course-media', 'clientFilesQuestion', 'x.png'));
    const courseBytes = fs.readFileSync(path.join(COURSE_DIR, 'clientFilesCourse', 'x.png'));
    assert.ok(!questionBytes.equals(courseBytes), 'as duas fixtures x.png precisam ter bytes diferentes');
    assert.ok(decoded(ir.media[0]).equals(questionBytes), 'bytes de clientFilesQuestion/x.png diferentes do arquivo da questão');
    assert.equal(ir.media[0].size_bytes, questionBytes.length, 'size_bytes de clientFilesQuestion/x.png');
    assert.ok(decoded(ir.media[1]).equals(courseBytes), 'bytes de clientFilesCourse/x.png diferentes do arquivo da raiz do curso');
    assert.equal(ir.media[1].size_bytes, courseBytes.length, 'size_bytes de clientFilesCourse/x.png');
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [['cq-media:m1', 'Questão'], ['cq-media:m2', 'Curso'], ['cq-media:m2', 'Curso']],
      'imagens do enunciado (o <img> de client_files_course_url reusa m2)',
    );

    // order="fixed": a opção "a" tem a figura do curso, e a "b", a da questão; o hash de cada
    // uma usa a identidade estável da própria mídia, escrita aqui sem passar por ir.media.
    const options = field(ir, 'mc').options;
    assert.deepEqual(
      options.map((o: any) => [o.key, imagesOf(o.html)]),
      [['a', [['cq-media:m2', 'Curso']]], ['b', [['cq-media:m1', 'Questão']]]],
      'opções de mc (order="fixed")',
    );
    assert.equal(options[0].content_hash, hashWithIdentity(options[0].html, 'static:clientFilesCourse/x.png'), 'content_hash da opção com a figura do curso');
    assert.equal(options[1].content_hash, hashWithIdentity(options[1].html, 'static:clientFilesQuestion/x.png'), 'content_hash da opção com a figura da questão');
    assert.notEqual(options[0].content_hash, options[1].content_hash, 'mídias de diretórios diferentes nunca se confundem');

    // Gabarito: a opção "a" (correct="true"); painel de resposta "<li>(a) <img alt="Curso"></li>".
    assert.equal(body.snapshot.correct_answers.mc.key, 'a', 'correct_answers.mc.key');
    const display: string = answerKey(ir, 'mc').display_html;
    assert.equal(typeof display, 'string', 'display_html de mc');
    assert.equal(norm(textOf(display)), '(a)', `texto do display_html de mc: ${display}`);
    assert.deepEqual(imagesOf(display), [['cq-media:m2', 'Curso']], `imagem do display_html de mc: ${display}`);
  });
});

test('ir-media-escape: imagem estática cujo caminho sai de clientFilesQuestion fica ausente, com aviso, e a variante sai "ok"', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-escape', 'abc');
    const ir = body.ir;
    // src = "<client_files_question_url>/../fora.png": o arquivo existe na raiz da questão, fora do
    // diretório-base. O filename da mídia não é fixado.
    assert.equal(ir.media.length, 1, `uma mídia: ${JSON.stringify(ir.media)}`);
    const m = ir.media[0];
    assert.deepEqual(
      [m.id, m.origin, m.content_type, m.alt, m.status],
      ['m1', 'static', 'image/png', 'Fora', 'missing'],
      'mídia cujo caminho escapa do diretório-base',
    );
    assert.ok(!('data_base64' in m), 'a mídia ausente não tem data_base64');
    assert.deepEqual(warningPairs(body), ['missing_media/render'], `avisos: ${JSON.stringify(body.warnings)}`);
    const fora = fs.readFileSync(path.join(SUPPORTED_DIR, 'ir-media-escape', 'fora.png'));
    assert.ok(!JSON.stringify(body).includes(fora.toString('base64')), 'os bytes de fora.png (fora do diretório-base) vazaram na resposta');
    assert.deepEqual(imagesOf(ir.statement.html), [['cq-media:m1', 'Fora']], 'imagem do enunciado');
  });
});

test('ir-media-types: foto.jpg estática e grafico.svg gerada, com o content_type pela extensão', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-types', 'abc');
    const ir = body.ir;
    // [F] O PL a193f72 serve os arquivos com o tipo pela extensão (express 4.22.3, res.attachment,
    // conferido no pl-oracle): .jpg → image/jpeg, .svg → image/svg+xml.
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'static', 'clientFilesQuestion/foto.jpg', 'image/jpeg', 'Foto', 'ok'],
        ['m2', 'dynamic', 'grafico.svg', 'image/svg+xml', 'Gráfico', 'ok'],
      ],
      'mídias de ir-media-types',
    );
    const jpg = fs.readFileSync(path.join(SUPPORTED_DIR, 'ir-media-types', 'clientFilesQuestion', 'foto.jpg'));
    assert.ok(decoded(ir.media[0]).equals(jpg), 'bytes de foto.jpg diferentes do arquivo');
    assert.equal(ir.media[0].size_bytes, jpg.length, 'size_bytes de foto.jpg');
    // Os mesmos bytes que o file() da fixture devolve (server.py, GRAFICO_SVG).
    const svg = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><rect width="4" height="4" fill="#2a7"/></svg>', 'utf8');
    assert.ok(decoded(ir.media[1]).equals(svg), 'bytes de grafico.svg diferentes dos do file()');
    assert.equal(ir.media[1].size_bytes, svg.length, 'size_bytes de grafico.svg');
    assert.deepEqual(imagesOf(ir.statement.html), [['cq-media:m1', 'Foto'], ['cq-media:m2', 'Gráfico']], 'imagens do enunciado');
  });
});

test('ir-markup: rótulo com marcação canonicalizado e conteúdo de <textarea> fora do enunciado', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-markup', 'abc');
    const ir = body.ir;
    // label="&lt;b&gt;Área&lt;/b&gt; $A$ =": o atributo decodificado é "<b>Área</b> $A$ =", que o PL
    // renderiza cru ({{{label}}}); canonicalizado, <b> vira <strong> e $A$ vira cq-math.
    const x = field(ir, 'x');
    assert.deepEqual(textsOf(x.label_html, 'strong'), ['Área'], `label_html de x: <b> vira <strong>: ${x.label_html}`);
    assert.deepEqual(mathOf(x.label_html), [{ display: 'inline', tex: 'A' }], `label_html de x: fórmula: ${x.label_html}`);
    assert.equal(norm(textOf(x.label_html)), 'Área A =', `label_html de x: texto: ${x.label_html}`);
    assert.ok(!x.label_html.includes('&lt;'), `a marcação do rótulo não pode sair escapada como texto: ${x.label_html}`);
    assert.equal(x.suffix_html, null, 'x não tem suffix');
    // [F] Painel "answer" do pl-number-input (PL a193f72): <span><b>Área</b> $A$ =</span> <samp>2</samp>.
    assert.equal(displayText(ir, 'x'), 'Área A = 2', 'display_html de x: rótulo e gabarito');
    assert.deepEqual(textsOf(answerKey(ir, 'x').display_html, 'strong'), ['Área'], 'display_html de x: <b> vira <strong>');
    assert.deepEqual(mathOf(answerKey(ir, 'x').display_html), [{ display: 'inline', tex: 'A' }], 'display_html de x: fórmula do rótulo');
    // HTML serializado do gabarito: span e samp desembrulhados, "Á" literal.
    assert.equal(
      norm(answerKey(ir, 'x').display_html),
      '<strong>Área</strong> <cq-math display="inline">A</cq-math> = 2',
      `display_html de x (HTML): ${answerKey(ir, 'x').display_html}`,
    );
    // no enunciado, o conteúdo de <textarea> continua removido.
    assertAbsent(ir.statement.html, 'MARCADOR-TEXTAREA', 'statement.html (conteúdo de textarea)');
    assert.equal(norm(textOf(ir.statement.html)), 'Rótulo com marcação.', `texto do enunciado: ${ir.statement.html}`);
    // O textarea do enunciado sai com o conteúdo, sem virar <pre> (a troca por pre é só no gabarito).
    assert.deepEqual(tagsOf(ir.statement.html), ['p', 'cq-field'], `tags do enunciado de ir-markup: ${ir.statement.html}`);
  });
});

test('ir-hidden-question-field: campo em pl-hide-in-panel question="true" fica sem cq-field, mas com gabarito exibível', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-hidden-question-field', 'abc');
    const ir = body.ir;
    assert.deepEqual(ir.fields.map((f: any) => f.name), ['h', 'v'], 'campos na ordem do documento');
    // O conteúdo de pl-hide-in-panel question="true" não entra no enunciado.
    assert.deepEqual(markersOf(ir.statement.html), ['v'], `cq-field do enunciado: só o de v: ${ir.statement.html}`);
    // [F] pl-hide-in-panel.py: com answer="false" (padrão), o painel de resposta mostra o conteúdo.
    assert.equal(answerKey(ir, 'h').can_show, true, 'can_show de h (nenhuma regra de esconder o gabarito se aplica)');
    assert.equal(displayText(ir, 'h'), '1', 'display_html de h: <samp>1</samp>');
    assert.equal(answerKey(ir, 'h').correct_value, 1, 'correct_value de h (server.py)');
  });
});

test('ir-media-timeout: file() que não termina deixa a mídia ausente, com aviso, sem quebrar a variante nem o motor', async () => {
  await withEngine({ ENGINE_QUESTION_TIMEOUT_MS: '2000' }, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-timeout', 'abc');
    const ir = body.ir;
    // No PL, o tempo esgotado do file() afeta só o pedido da imagem; a mídia que não pode ser obtida fica
    // ausente, com aviso, como o file() que falha: media_error na fase file.
    assert.deepEqual(mediaSummary(ir), [['m1', 'dynamic', 'lenta.png', 'image/png', 'Figura lenta', 'missing']], 'mídias de ir-media-timeout');
    assert.ok(!('data_base64' in ir.media[0]), 'lenta.png não tem data_base64');
    assert.deepEqual(warningPairs(body), ['media_error/file'], `avisos: ${JSON.stringify(body.warnings)}`);
    assert.equal(norm(textOf(ir.statement.html)), 'Figura que não termina de ser gerada.', 'texto do enunciado');
    // o tempo esgotado do file() não afeta o gabarito: o painel de resposta do pl-integer-input
    // é independente do file() e mostra <samp>1</samp> (server.py: correct_answers["v"] = 1).
    assert.equal(answerKey(ir, 'v').can_show, true, 'can_show de v');
    assert.equal(answerKey(ir, 'v').correct_value, 1, 'correct_value de v (server.py)');
    assert.equal(displayText(ir, 'v'), '1', 'display_html de v');
    // o motor continua atendendo: a variante seguinte sai "ok", com os params da seed abc
    // (oráculo, element/numberInput seed abc: {a: 9, b: 6}).
    const next = await createOk(engine, EXAMPLE, 'element/numberInput', 'abc');
    assert.deepEqual(next.snapshot.params, { a: 9, b: 6 }, 'params de element/numberInput seed abc depois do tempo esgotado');
  });
});

// ---------------------------------------------------------------------------------------------
// Sufixos com marcação, atributos e explicação com template, gabarito multiline,
// mídias em subdiretório e em executores recém-reiniciados, saída do generate nos avisos. Valores
// esperados das fixtures e do PL a193f72 (fases do PL numa imagem descartável, 2026-09-25: params,
// correct_answers, painéis "question" e "answer" e o file() de cada figura num executor novo).
// ---------------------------------------------------------------------------------------------

/** O campo sem a dica, que tem regra própria (checkIr). */
function withoutHint(f: any): any {
  const { hint: _hint, ...rest } = f;
  return rest;
}

test('ir-suffix-markup: sufixo de linha única escapado como texto, o do multiline como HTML canonicalizado, no campo e no gabarito', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-suffix-markup', 'abc');
    const ir = body.ir;
    // [F] suffix_html pela regra do sufixo com marcação (o mesmo que checkSpec confere), com os atributos decodificados do question.html.
    assert.deepEqual(
      ir.fields.map((f: any) => [f.name, f.suffix_html]),
      [
        ['n', 'm&lt;sup&gt;2&lt;/sup&gt; &amp; s'],
        ['i', '&lt;b&gt;un&lt;/b&gt;'],
        ['t', '&lt;i&gt;x&lt;/i&gt;'],
        ['m', '<strong>fim</strong> <em>aqui</em>'],
      ],
      'suffix_html de cada campo (sufixo com marcação)',
    );
    // [F] Painel "answer" do PL a193f72: pl-number-input "<samp>2</samp>\n<span>m&lt;sup&gt;2&lt;/sup&gt;
    // &amp; s</span>"; pl-integer-input "<samp>3</samp>\n<span>&lt;b&gt;un&lt;/b&gt;</span>";
    // pl-string-input "<code>x</code>\n<span>&lt;i&gt;x&lt;/i&gt;</span>" e o botão do popover; no
    // multiline, "<textarea>y</textarea>", o botão e '<div class="form-text"><b>fim</b> <i>aqui</i></div>'.
    assert.equal(norm(answerKey(ir, 'n').display_html), '2 m&lt;sup&gt;2&lt;/sup&gt; &amp; s', 'display_html de n');
    assert.equal(norm(answerKey(ir, 'i').display_html), '3 &lt;b&gt;un&lt;/b&gt;', 'display_html de i');
    assert.equal(norm(answerKey(ir, 't').display_html), '<code>x</code> &lt;i&gt;x&lt;/i&gt;', 'display_html de t');
    const m: string = answerKey(ir, 'm').display_html;
    assert.equal(typeof m, 'string', 'display_html de m');
    assert.deepEqual(textsOf(m, 'pre'), ['y'], `display_html de m: o <textarea> do gabarito vira <pre>: ${m}`);
    assert.ok(m.includes('<strong>fim</strong> <em>aqui</em>'), `display_html de m: o sufixo multiline como HTML canonicalizado: ${m}`);
    const $m = load(m);
    $m('pre').remove();
    assert.equal(norm($m.root().text()), 'fim aqui', `display_html de m: fora do <pre>, só o sufixo: ${m}`);
    for (const [name, value] of [['n', 2], ['i', 3], ['t', 'x'], ['m', 'y']] as Array<[string, unknown]>) {
      assert.equal(answerKey(ir, name).can_show, true, `can_show de ${name}`);
      assert.equal(answerKey(ir, name).correct_value, value, `correct_value de ${name} (server.py)`);
    }
    // Os sufixos ficam nos campos: o enunciado é o parágrafo e os quatro marcadores.
    assert.deepEqual(tagsOf(ir.statement.html), ['p', 'cq-field', 'cq-field', 'cq-field', 'cq-field'], `tags do enunciado: ${ir.statement.html}`);
  });
});

test('ir-template-attrs: answers-name e base vindos do template valem depois do mustache', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-template-attrs', 'abc');
    const ir = body.ir;
    // [F] PL a193f72: com params {nome: "n1", base: 16}, o painel da questão tem <input name="n1"> e
    // "integer in base 16"; o painel de resposta tem <samp>1F</samp> (31 em base 16).
    assert.deepEqual(body.snapshot.params, { nome: 'n1', base: 16 }, 'snapshot.params (server.py)');
    assert.deepEqual(body.snapshot.correct_answers, { n1: 31 }, 'snapshot.correct_answers (server.py)');
    assert.deepEqual(
      ir.fields.map(withoutHint),
      [
        {
          name: 'n1',
          type: 'integer',
          label_html: null,
          suffix_html: null,
          format: { kind: 'integer', base: 16 },
          constraints: { allow_blank: false },
          weight: 1,
          options: null,
        },
      ],
      'o campo tem o answers-name e a base do HTML depois do mustache',
    );
    assert.deepEqual(markersOf(ir.statement.html), ['n1'], `cq-field do enunciado: ${ir.statement.html}`);
    assert.equal(norm(textOf(ir.statement.html)), 'Digite 31 em base 16.', `texto do enunciado: ${ir.statement.html}`);
    assert.deepEqual(
      ir.answer_key.map((a: any) => [a.name, a.can_show, a.correct_value]),
      [['n1', true, 31]],
      'gabarito do campo n1',
    );
    assert.equal(displayText(ir, 'n1'), '1F', 'display_html de n1: 31 em base 16');
  });
});

test('ir-console-echo: o aviso do print no generate é uma frase funcional, sem o texto impresso', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-console-echo', 'abc');
    // [F] PL a193f72: a saída do generate é "depuração ECO-GENERATE-3b7\n".
    assert.ok(body.warnings.length >= 1, `faltou o aviso de saída no console: ${JSON.stringify(body.warnings)}`);
    for (const w of body.warnings) {
      assert.deepEqual([w.code, w.phase], ['console_output', 'generate'], `aviso do generate: ${JSON.stringify(w)}`);
      assert.ok(!w.message.includes('ECO-GENERATE-3b7'), `a mensagem do aviso repete o que a questão imprimiu: ${w.message}`);
    }
    // Nenhum campo da resposta carrega a saída do console.
    assert.ok(!JSON.stringify(body).includes('ECO-GENERATE-3b7'), 'a saída do console vazou na resposta');
    assert.equal(displayText(body.ir, 'v'), '1', 'display_html de v');
  });
});

test('ir-multiline-escape: o textarea do gabarito vira pre com o texto escapado, e a aspa literal', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-multiline-escape', 'abc');
    const key = answerKey(body.ir, 't');
    // [F] Painel "answer" do PL a193f72: um <script>, depois '<div><span class="input-group"><textarea …>a
    // &lt; b &amp;&amp; c &gt; &quot;d&quot;</textarea><button …>…</button></span></div>'. O script e o
    // botão saem com o conteúdo; o textarea vira <pre> com o mesmo texto; a aspa fica literal
    // no texto.
    const d: string = key.display_html;
    assert.equal(typeof d, 'string', 'display_html de t');
    assert.deepEqual(textsOf(d, 'pre'), ['a < b && c > "d"'], `texto do <pre> de t: ${d}`);
    assert.equal(norm(d), '<pre>a &lt; b &amp;&amp; c &gt; "d"</pre>', `display_html de t (HTML): ${d}`);
    assert.equal(key.can_show, true, 'can_show de t');
    assert.equal(key.correct_value, 'a < b && c > "d"', 'correct_value de t (server.py)');
    // No enunciado, o textarea do widget não vira pre: o campo é só o marcador.
    assert.deepEqual(tagsOf(body.ir.statement.html), ['p', 'cq-field'], `tags do enunciado: ${body.ir.statement.html}`);
  });
});

/**
 * [F] r = random.randint(1, 10**9) depois de random.seed(parseInt(seed, 36)) (zygote do PL a193f72),
 * conferido nas fases do PL; as seeds de seeds(3) repetem os valores de supported/random-params.
 */
const ANSWER_TEMPLATE_R: Record<string, number> = {
  abc: 763509902,
  '9ix': 447342178,
  '17we19m': 147104152,
  grnr17: 79590372,
};

test('ir-answer-template: explicação, rótulo e gabarito trazem o r sorteado da variante, depois do mustache', async () => {
  await withEngine({}, async (engine) => {
    for (const seed of ['abc', ...seeds(3)]) {
      const r = ANSWER_TEMPLATE_R[seed];
      assert.ok(r !== undefined, `seed ${seed} sem r esperado`);
      const body = await createOk(engine, TEST_COURSE, 'supported/ir-answer-template', seed);
      const ir = body.ir;
      assert.deepEqual(body.snapshot.params, { r }, `seed ${seed}: snapshot.params`);
      assert.deepEqual(body.snapshot.correct_answers, { v: r }, `seed ${seed}: snapshot.correct_answers`);
      // [F] Painel "answer" do PL a193f72: '<span>Valor <r>:</span>\n<samp><r></samp>' e, do
      // pl-answer-panel, '<p>A resposta é <r>.</p>'.
      assert.notEqual(ir.explanation, null, `seed ${seed}: o pl-answer-panel deveria virar explanation`);
      assert.equal(norm(ir.explanation.html), `<p>A resposta é ${r}.</p>`, `seed ${seed}: explanation.html`);
      assert.equal(ir.explanation.can_show, true, `seed ${seed}: explanation.can_show`);
      assert.equal(norm(field(ir, 'v').label_html), `Valor ${r}:`, `seed ${seed}: label_html de v (label="Valor {{params.r}}:")`);
      const display: string = answerKey(ir, 'v').display_html;
      assert.equal(typeof display, 'string', `seed ${seed}: display_html de v`);
      assert.equal(norm(display), `Valor ${r}: ${r}`, `seed ${seed}: display_html de v: rótulo e gabarito`);
      assert.ok(!display.includes('{{'), `seed ${seed}: sobrou "{{" no gabarito: ${display}`);
      assert.equal(answerKey(ir, 'v').correct_value, r, `seed ${seed}: correct_value de v`);
    }
  });
});

test('ir-media-subdir: o subdiretório entra no filename, e três fig.png de diretórios diferentes não se confundem', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-subdir', 'abc');
    const ir = body.ir;
    // [F] PL a193f72, pl-figure.py: o prepare aceita file-name="sub/fig.png" (os.path.isfile do caminho
    // no diretório-base) e o render monta o src com quote(file-name), que mantém a "/" ("/cfq/sub/fig.png",
    // "/cfq/fig.png" e "/cfc/sub/fig.png"). filename = <diretório-base>/<caminho decodificado>.
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'static', 'clientFilesQuestion/sub/fig.png', 'image/png', 'Sub', 'ok'],
        ['m2', 'static', 'clientFilesQuestion/fig.png', 'image/png', 'Raiz', 'ok'],
        ['m3', 'static', 'clientFilesCourse/sub/fig.png', 'image/png', 'Curso sub', 'ok'],
      ],
      'mídias de ir-media-subdir',
    );
    const files = [
      path.join(SUPPORTED_DIR, 'ir-media-subdir', 'clientFilesQuestion', 'sub', 'fig.png'),
      path.join(SUPPORTED_DIR, 'ir-media-subdir', 'clientFilesQuestion', 'fig.png'),
      path.join(COURSE_DIR, 'clientFilesCourse', 'sub', 'fig.png'),
    ].map((f) => fs.readFileSync(f));
    assert.equal(new Set(files.map((b) => b.toString('base64'))).size, 3, 'as três fixtures fig.png precisam ter bytes diferentes');
    for (const [k, bytes] of files.entries()) {
      assert.ok(decoded(ir.media[k]).equals(bytes), `bytes de ${ir.media[k].filename} diferentes do arquivo`);
      assert.equal(ir.media[k].size_bytes, bytes.length, `size_bytes de ${ir.media[k].filename}`);
    }
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [['cq-media:m1', 'Sub'], ['cq-media:m2', 'Raiz'], ['cq-media:m3', 'Curso sub']],
      'src e alt das imagens do enunciado',
    );
    assert.deepEqual(body.warnings, [], `sem avisos: ${JSON.stringify(body.warnings)}`);
  });
});

test('ir-media-executor: uma chamada do file() por arquivo, cada uma num executor recém-reiniciado e com os dados da variante', async () => {
  await withEngine({}, async (engine) => {
    const body = await createOk(engine, TEST_COURSE, 'supported/ir-media-executor', 'abc');
    const ir = body.ir;
    // [F] seed abc: random.seed(13368) e random.random() = 0.7110740091675601 (o mesmo r0 de
    // supported/media). Nas fases do PL a193f72, com uma chamada do file() por executor novo, r1.png e
    // r2.png devolveram "0.7110740091675601|1" e p.png, "7|1".
    assert.deepEqual(body.snapshot.params, { r0: 0.7110740091675601, k: 7 }, 'snapshot.params (server.py)');
    assert.deepEqual(body.snapshot.correct_answers, { v: 1 }, 'snapshot.correct_answers (server.py)');
    assert.deepEqual(
      mediaSummary(ir),
      [
        ['m1', 'dynamic', 'r1.png', 'image/png', 'R1', 'ok'],
        ['m2', 'dynamic', 'r2.png', 'image/png', 'R2', 'ok'],
        ['m3', 'dynamic', 'p.png', 'image/png', 'P', 'ok'],
      ],
      'mídias de ir-media-executor',
    );
    // O mesmo primeiro sorteio e a contagem 1 nas duas: o módulo recomeça e é semeado de novo a cada
    // arquivo; num executor compartilhado, r2.png teria outro sorteio e a contagem 2.
    assert.equal(decoded(mediaByFilename(ir, 'r1.png')).toString('utf8'), '0.7110740091675601|1', 'bytes de r1.png');
    assert.equal(decoded(mediaByFilename(ir, 'r2.png')).toString('utf8'), '0.7110740091675601|1', 'bytes de r2.png');
    // O file() recebe params e correct_answers da variante (extractVariantSubmissionData sem submissão).
    assert.equal(decoded(mediaByFilename(ir, 'p.png')).toString('utf8'), '7|1', 'bytes de p.png');
    assert.deepEqual(body.warnings, [], `sem avisos: ${JSON.stringify(body.warnings)}`);
    assert.deepEqual(
      imagesOf(ir.statement.html),
      [['cq-media:m1', 'R1'], ['cq-media:m2', 'R2'], ['cq-media:m3', 'P']],
      'src e alt das imagens do enunciado',
    );
  });
});
