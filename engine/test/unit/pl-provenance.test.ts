// Proveniência das peças do PL que o motor reutiliza.
// Cobre: o código do PL sem alteração, na imagem oficial fixada, e a fidelidade ao PL (reuso de
// mustache, markdown.processQuestion e CodeCallerNative), este último pelo que o motor carrega
// e chama numa operação. As camadas da imagem são conferidas no host por
// engine/conformance/provenance.test.ts.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import { registerHooks } from 'node:module';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { EXAMPLE, ref } from '../helpers/fixtures.ts';

const APP = '/PrairieLearn/apps/prairielearn';
const ENGINE_DIR = path.join(APP, 'canal-questoes-engine');

/**
 * [F] sha256 destes arquivos na imagem prairielearn/prairielearn@sha256:c2b033eba27657a022ca82535cf48b4098bae02609e7f23114512bcf2d0ea4b6
 * (commit a193f72), medidos em 2026-09-24. Se a imagem base mudar, estes valores mudam.
 */
const PL_FILES: Record<string, string> = {
  'dist/lib/code-caller/code-caller-native.js': 'b222fbf4d38f4f54211f89e0a0a420e9af9b915e1a16209d4de2168cca306308',
  'dist/lib/markdown.js': 'f2fe8f2bb6aa4db0d265018a47d2558164efd902b0bacd3309cdb3f98e6f2c0e',
  'dist/lib/paths.js': '02fa2be90bcd848433baa87873d06c911cb327e7fb5ae3e47098f1e8010c523f',
  'python/zygote.py': '866781866a6a8cf078df7611bd5a720beb0a25b18866b5bc4a8d3b1efe1d2b7b',
  'python/prairielearn/internal/question_phases.py': 'eccc6a2aa3410d3844e25794e13dbc1344fac477f007a91afd0dc5506ef8ecb7',
};
const MUSTACHE_MJS = '/PrairieLearn/node_modules/.pnpm/mustache@4.2.0/node_modules/mustache/mustache.mjs';
const MUSTACHE_MJS_SHA256 = '0f6041f042ef7d23dc6b229dfd7b6f7af75aa192a72a458d82471cd2c0ac61f2';

function sha256(file: string): string {
  return createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function sourcesOf(dir: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const entry of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
    if (entry.isFile() && entry.name.endsWith('.ts')) {
      const full = path.join(entry.parentPath, entry.name);
      out.set(full, fs.readFileSync(full, 'utf8'));
    }
  }
  return out;
}

test('as peças do PL presentes na imagem são as do digest fixado', () => {
  for (const [rel, expected] of Object.entries(PL_FILES)) {
    assert.equal(sha256(path.join(APP, rel)), expected, `${rel} difere do PL a193f72 fixado`);
  }
  assert.equal(sha256(MUSTACHE_MJS), MUSTACHE_MJS_SHA256, 'o mustache 4.2.0 do PL difere do fixado');
});

/**
 * [F] Digest dos diretórios do PL que o motor executa, medido na imagem base fixada (2026-09-24),
 * pela mesma regra do hash de conteúdo (__pycache__ fora): [sha256, nº de arquivos].
 * Uma camada posterior que sobrescreva qualquer arquivo desses diretórios
 * (elementos suportados, biblioteca Python do PL, code caller, question servers) falha aqui.
 */
const PL_DIRS: Record<string, [string, number]> = {
  'elements/pl-multiple-choice': ['89f215f62f11da77f9fb6a7c6be18bb40df04af181719f4b7a8d42b033e0e4f4', 9],
  'elements/pl-checkbox': ['58d320d02d59f44334cc682969b00790dfbb472a8e4e313a1a85ae601123e2ac', 8],
  'elements/pl-number-input': ['f09a9484a73d542dedda1107d03094eb3c052d7b62d689be0c63bbd666da9749', 7],
  'elements/pl-integer-input': ['89800cb527ba2c319c0293b5ca02bef99de76ba065d096a87c2316e7555cc5f2', 6],
  'elements/pl-string-input': ['f1440e17406f5f75d5e7a0457ff663ad4f30ca3c0997fcb2f39fc395c8dcd5a9', 6],
  'elements/pl-question-panel': ['c7fed78674fd9ae433a3bd7460bd869ad81a4f75af9ebc48412d11ff493a1b21', 2],
  'elements/pl-answer-panel': ['ec745df9a527cbe162fe72a352d684934e8bd2a5ec718661ace6e7d98ffa3a6b', 2],
  'elements/pl-submission-panel': ['bdc055cb7c301ff0f9c478712c5024be7638043f5ddcf9b8601bac06b2dddb8c', 2],
  'elements/pl-hide-in-panel': ['d6cb9555dccde74ff793abbe40a654d09416bf0473926c24fcac89c881cb257b', 2],
  'elements/pl-card': ['580de6fe27d33cc055a429d53939de80d416172ea9d835fe2f654de74c0450bb', 3],
  'elements/pl-code': ['580f0d912d2c3984ac93d5479580eb483f8d81de0307ab566d4248b7cba97662', 5],
  'elements/pl-figure': ['04d083a9ccdd92793bc6b800af10af535c5bab6f7564fb16e3b1b7ec5a858008', 3],
  'python/prairielearn': ['5bd0970a7b6b201f71a008a7d6550f0d6eddeabe71068ba81b9261021b3cf99f', 18],
  'dist/lib/code-caller': ['baa6826ae808b978c506fd0596b0865ee85a3ad5b26162733df1aaca8d531f45', 11],
  'dist/question-servers': ['df431c48fcca4750fbf3454b61cd85e6fe89e3e345becba6f1145a13ae2cad72', 14],
};

function dirDigest(dir: string): [string, number] {
  const files: string[] = [];
  const walk = (rel: string): void => {
    for (const e of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
      const child = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) {
        if (e.name !== '__pycache__') walk(child);
      } else if (e.isFile()) files.push(child);
    }
  };
  walk('');
  files.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const h = createHash('sha256');
  for (const rel of files) {
    const bytes = fs.readFileSync(path.join(dir, rel));
    h.update(rel);
    h.update('\0');
    h.update(String(bytes.length));
    h.update('\0');
    h.update(bytes);
    h.update('\0');
  }
  return [h.digest('hex'), files.length];
}

test('os diretórios do PL executados pelo motor são os do digest fixado, sem sobrescrita', () => {
  for (const [rel, expected] of Object.entries(PL_DIRS)) {
    assert.deepEqual(dirDigest(path.join(APP, rel)), expected, `${rel} difere do PL a193f72 fixado`);
  }
});

test('o mustache resolvido a partir do motor é o do node_modules do PL', () => {
  // [F] No Node 24, import.meta.resolve só respeita o 2º argumento (parent) com a flag
  // --experimental-import-meta-resolve; sem ela, o parent é ignorado e a resolução sairia da pasta
  // deste teste. Por isso a resolução a partir de src/ roda num processo filho com a flag.
  const resolved = execFileSync(
    process.execPath,
    [
      '--experimental-import-meta-resolve',
      '--input-type=module',
      '-e',
      'process.stdout.write(import.meta.resolve("mustache", process.argv[1]))',
      `file://${ENGINE_DIR}/src/`,
    ],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  );
  const file = fs.realpathSync(new URL(resolved));
  assert.equal(file, MUSTACHE_MJS, `o motor resolveria outro mustache: ${file}`);
});

// O reuso é conferido pelo que o motor CARREGA e CHAMA numa operação, e não por
// texto no código-fonte (um comentário ou um import sem uso não basta).
// - Um hook de módulos do Node (module.registerHooks, no mesmo thread) é registrado ANTES de o motor
//   ser importado neste processo e anota o caminho real de cada módulo carregado.
// - No load hook, e só na memória deste processo de teste, cada peça do PL recebe ao fim do módulo
//   um invólucro que conta a chamada e repassa argumentos, this e retorno sem mudança. O arquivo no
//   disco não muda: o sha256 dele é conferido no primeiro teste deste arquivo.
const CODE_CALLER_NATIVE_JS = path.join(APP, 'dist/lib/code-caller/code-caller-native.js');
const MARKDOWN_JS = path.join(APP, 'dist/lib/markdown.js');
const PROBE_KEY = 'canal-questoes.test.pl-reuse-probe';

type ProbeName = 'CodeCallerNative.prototype.call' | 'markdown.processQuestion' | 'mustache Writer.prototype.render';

interface ReuseProbe {
  calls: Record<ProbeName, number>;
  instrumented: ProbeName[];
  instrumentErrors: string[];
  loaded: Set<string>;
}

/**
 * [F] Alvos no código do PL a193f72 (bytes fixados pelo sha256 acima):
 * - code-caller-native.js: `export class CodeCallerNative`, com o método `async call(type, directory, file, fcn, args)`;
 * - markdown.js: `export function processQuestion(html)` (declaração de função: a ligação é
 *   reatribuível dentro do módulo, e quem importa vê o invólucro);
 * - mustache.mjs: `mustache.render` delega a `defaultWriter.render`, isto é, a `Writer.prototype.render`,
 *   que também cobre um `new Mustache.Writer()`.
 */
const INSTRUMENTED: Record<string, { name: ProbeName; target: string }> = {
  [CODE_CALLER_NATIVE_JS]: { name: 'CodeCallerNative.prototype.call', target: 'CodeCallerNative.prototype.call' },
  [MARKDOWN_JS]: { name: 'markdown.processQuestion', target: 'processQuestion' },
  [MUSTACHE_MJS]: { name: 'mustache Writer.prototype.render', target: 'Writer.prototype.render' },
};

/** Código anexado ao fim do módulo: envolve `target` e conta cada chamada em probe.calls[name]. */
function counterSource(name: ProbeName, target: string): string {
  const key = JSON.stringify(PROBE_KEY);
  const label = JSON.stringify(name);
  return [
    '',
    ';{',
    `  const probe = globalThis[Symbol.for(${key})];`,
    '  if (probe) {',
    '    try {',
    `      const original = ${target};`,
    `      ${target} = function (...args) {`,
    `        probe.calls[${label}] += 1;`,
    '        return original.apply(this, args);',
    '      };',
    `      probe.instrumented.push(${label});`,
    '    } catch (err) {',
    `      probe.instrumentErrors.push(${label} + ': ' + String(err));`,
    '    }',
    '  }',
    '}',
    '',
  ].join('\n');
}

/** Caminho real (sem symlinks, sem query) de uma URL file:; null para node:, data: etc. */
function realFile(url: string): string | null {
  if (!url.startsWith('file:')) return null;
  const u = new URL(url);
  u.search = '';
  u.hash = '';
  const file = fileURLToPath(u);
  try {
    return fs.realpathSync(file);
  } catch {
    return file;
  }
}

function installReuseProbe(): { probe: ReuseProbe; remove(): void } {
  const probe: ReuseProbe = {
    calls: { 'CodeCallerNative.prototype.call': 0, 'markdown.processQuestion': 0, 'mustache Writer.prototype.render': 0 },
    instrumented: [],
    instrumentErrors: [],
    loaded: new Set(),
  };
  Reflect.set(globalThis, Symbol.for(PROBE_KEY), probe);
  const hooks = registerHooks({
    load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      const file = realFile(url);
      if (file === null) return result;
      probe.loaded.add(file);
      const spec = INSTRUMENTED[file];
      if (spec === undefined || result.source === undefined) return result;
      const text = typeof result.source === 'string' ? result.source : new TextDecoder().decode(result.source);
      return { ...result, source: text + counterSource(spec.name, spec.target) };
    },
  });
  return {
    probe,
    remove() {
      hooks.deregister();
      Reflect.deleteProperty(globalThis, Symbol.for(PROBE_KEY));
    },
  };
}

/**
 * Autoverificação do sensor, antes do motor: com um sensor próprio, importa instâncias separadas das
 * três peças do PL (a URL com a query abaixo é outra entrada no cache de módulos do Node; o motor
 * continua carregando as suas, pela URL sem query, e o load hook as vê de novo) e exige que cada
 * invólucro conte exatamente uma chamada e devolva o resultado do próprio PL. Assim, o vermelho do
 * teste abaixo vem do motor, e não de um sensor quebrado.
 */
async function selfCheckProbe(): Promise<void> {
  const SELF_CHECK_QUERY = '?cq-probe-selfcheck';
  const { probe, remove } = installReuseProbe();
  try {
    const mustache = (await import(pathToFileURL(MUSTACHE_MJS).href + SELF_CHECK_QUERY)).default;
    const markdown = await import(pathToFileURL(MARKDOWN_JS).href + SELF_CHECK_QUERY);
    const { CodeCallerNative } = await import(pathToFileURL(CODE_CALLER_NATIVE_JS).href + SELF_CHECK_QUERY);
    // [F] mustache 4.2.0: {{x}} escapa < e > (escapeHtml com entityMap).
    assert.equal(mustache.render('a {{x}}', { x: '<i>' }), 'a &lt;i&gt;', 'sensor: mustache.render do PL');
    // [F] markdown.js: processQuestion troca cada bloco <markdown> pelo HTML do marked.
    assert.equal(
      markdown.processQuestion('<markdown>**oi**</markdown>'),
      '<p><strong>oi</strong></p>\n',
      'sensor: markdown.processQuestion do PL',
    );
    // [F] code-caller-native.js: sem processo filho, o call do PL recusa o estado ("Invalid
    // CodeCallerNative state"); a mensagem prova que o original rodou por dentro do invólucro.
    const notWaiting = { state: 'sensor', debug() {}, _checkState: () => false };
    await assert.rejects(
      CodeCallerNative.prototype.call.call(notWaiting, 'question', 'q', 'server', 'generate', []),
      /Invalid CodeCallerNative state: sensor/,
      'sensor: CodeCallerNative.prototype.call do PL',
    );
  } finally {
    remove();
  }
  assert.deepEqual(probe.instrumentErrors, [], 'sensor: a instrumentação das peças do PL falhou');
  assert.deepEqual(
    [...probe.instrumented].sort(),
    Object.values(INSTRUMENTED)
      .map((s) => s.name)
      .sort(),
    'sensor: cada peça do PL deveria ser instrumentada uma vez',
  );
  assert.deepEqual(
    probe.calls,
    { 'CodeCallerNative.prototype.call': 1, 'markdown.processQuestion': 1, 'mustache Writer.prototype.render': 1 },
    'sensor: uma chamada contada por peça do PL',
  );
  for (const file of [CODE_CALLER_NATIVE_JS, MARKDOWN_JS, MUSTACHE_MJS]) {
    assert.ok(probe.loaded.has(file), `sensor: o load hook deveria anotar ${file}`);
  }
}

test(
  'numa operação, o motor carrega e chama CodeCallerNative, markdown.processQuestion e mustache do PL, sem cópias',
  { timeout: 300_000 },
  async () => {
    // O sensor é conferido primeiro, e passa já no esqueleto. O teste fica vermelho depois dele:
    // loadConfig e createEngine lançam NotImplementedError.
    await selfCheckProbe();

    const { probe, remove } = installReuseProbe();
    const deltas = new Map<string, Record<ProbeName, number>>();
    try {
      // Importado só agora, depois do hook: nenhum módulo do motor foi carregado antes neste
      // processo. O teste só importou as instâncias próprias da autoverificação (URL com query).
      const { withEngine } = await import('../helpers/engine-harness.ts');
      await withEngine({}, async (engine) => {
        // [F] exampleCourse a193f72: element/numberInput tem {{params.a}} no question.html (exige o
        // mustache) e server.py; element/markdown tem blocos <markdown> (exige o processQuestion).
        // Toda variante executa Python (fases do PL e elementos), pelo CodeCallerNative.
        for (const qid of ['element/numberInput', 'element/markdown']) {
          const before = { ...probe.calls };
          const result = await engine.createVariant({ question_ref: ref(EXAMPLE, qid), seed: 'abc' });
          const body = result.body as { outcome?: unknown };
          assert.equal(result.status, 200, `${qid}: esperado 200, veio ${result.status}: ${JSON.stringify(body).slice(0, 800)}`);
          assert.equal(body.outcome, 'ok', `${qid}: a variante deveria sair ok: ${JSON.stringify(body).slice(0, 800)}`);
          const delta = { ...probe.calls };
          for (const name of Object.keys(delta) as ProbeName[]) delta[name] -= before[name];
          deltas.set(qid, delta);
        }
      });
    } finally {
      remove();
    }

    assert.deepEqual(probe.instrumentErrors, [], 'a instrumentação das peças do PL falhou');

    // Carregadas pelo motor: os arquivos do PL fixados, pelo caminho real.
    const reused = [CODE_CALLER_NATIVE_JS, MARKDOWN_JS, MUSTACHE_MJS];
    assert.deepEqual(
      reused.filter((file) => probe.loaded.has(file)),
      reused,
      'o motor deveria carregar code-caller-native.js e markdown.js do dist do PL e o mustache.mjs 4.2.0 do node_modules do PL',
    );
    assert.deepEqual(
      [...probe.instrumented].sort(),
      Object.values(INSTRUMENTED)
        .map((s) => s.name)
        .sort(),
      'cada peça do PL deveria ser carregada e instrumentada uma única vez',
    );

    // Chamadas durante cada operação (e não só na criação do motor).
    const numberInput = deltas.get('element/numberInput');
    const markdown = deltas.get('element/markdown');
    assert.ok(numberInput && markdown, 'as duas variantes deveriam ter rodado');
    for (const [qid, delta] of [
      ['element/numberInput', numberInput],
      ['element/markdown', markdown],
    ] as const) {
      assert.ok(
        delta['CodeCallerNative.prototype.call'] >= 1,
        `${qid}: o motor deveria executar o Python pelo CodeCallerNative.call do PL (chamadas: ${JSON.stringify(delta)})`,
      );
    }
    assert.ok(
      numberInput['mustache Writer.prototype.render'] >= 1,
      `element/numberInput: o motor deveria renderizar o question.html com o mustache do PL (chamadas: ${JSON.stringify(numberInput)})`,
    );
    assert.ok(
      markdown['markdown.processQuestion'] >= 1,
      `element/markdown: o motor deveria processar os blocos <markdown> com o processQuestion do PL (chamadas: ${JSON.stringify(markdown)})`,
    );

    // Sem cópias carregadas de outro lugar (ex.: um code-caller-native.js ou um mustache copiado).
    for (const file of probe.loaded) {
      const base = path.basename(file);
      if (base === 'code-caller-native.js') {
        assert.equal(file, CODE_CALLER_NATIVE_JS, `o motor carregou outra cópia do CodeCallerNative: ${file}`);
      }
      if (/^mustache(\.min)?\.(mjs|cjs|js)$/.test(base)) {
        assert.equal(file, MUSTACHE_MJS, `o motor carregou outra cópia do mustache: ${file}`);
      }
    }

    // Sem cópias no código do motor (conferido no texto dos arquivos de src/).
    const sources = sourcesOf(path.join(ENGINE_DIR, 'src'));
    for (const [file, text] of sources) {
      assert.doesNotMatch(path.basename(file), /mustache|zygote|code-caller-native/i, `cópia suspeita de peça do PL: ${file}`);
      assert.ok(!/^\s*(export\s+)?class\s+CodeCallerNative\b/m.test(text), `reimplementação do CodeCallerNative em ${file}`);
      assert.ok(!/entityMap\s*=\s*\{/.test(text), `reimplementação do escape do mustache em ${file}`);
    }
  },
);
