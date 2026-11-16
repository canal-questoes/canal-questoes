// AUTOTESTE de integridade da suíte congelada.
// Cobre: a suíte congelada (a lista exata de arquivos *.test.ts presentes na imagem de teste, os helpers e a área
// /repos de teste fazem parte do oráculo congelado; a implementação não pode afrouxar a suíte
// removendo ou acrescentando arquivos de teste), a montagem de /repos e os helpers.
// Não depende da implementação: verifica só o conteúdo da imagem de teste.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { describe, it } from 'node:test';

import { OVERRIDE_COURSE, TEST_COURSE } from '../helpers/fixtures.ts';

// Raiz do engine na imagem (/PrairieLearn/apps/prairielearn/canal-questoes-engine): test/contract/../..
const ENGINE_ROOT = path.resolve(import.meta.dirname, '..', '..');
const REPOS_ROOT = '/repos';

/** A lista fixa da suíte congelada. Qualquer arquivo a mais ou a menos falha. */
const EXPECTED_TEST_FILES: string[] = [
  ...['schemas', 'cli', 'http-api', 'suite-integrity'].map((n) => `test/contract/${n}.test.ts`),
  ...[
    'reference',
    'check-data',
    'score',
    'content-hash',
    'pool',
    'typecheck',
    'config',
    'math',
    'canonical-html',
    'fields',
    'pl-provenance',
  ].map((n) => `test/unit/${n}.test.ts`),
  ...['create-variant', 'grade', 'inspect', 'ir', 'robustness', 'logging'].map(
    (n) => `test/integration/${n}.test.ts`,
  ),
].sort();

const EXPECTED_HELPERS: string[] = ['schemas', 'fixtures', 'engine-harness', 'canonical-html-checker'].map(
  (n) => `test/helpers/${n}.ts`,
);

// Todos os arquivos *.test.ts sob test/, em qualquer profundidade, como casa o glob do CMD da imagem
// de teste ("test/**/*.test.ts").
function listTestFiles(): string[] {
  const testDir = path.join(ENGINE_ROOT, 'test');
  const entries = fs.readdirSync(testDir, { recursive: true, encoding: 'utf8' });
  return entries
    .map((rel) => rel.split(path.sep).join('/'))
    .filter((rel) => rel.endsWith('.test.ts'))
    .map((rel) => `test/${rel}`)
    .sort();
}

function sha256Hex(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

describe('integridade da suíte de testes', () => {
  it('a lista de arquivos *.test.ts presentes é exatamente a lista congelada', () => {
    const present = listTestFiles();
    const missing = EXPECTED_TEST_FILES.filter((f) => !present.includes(f));
    const extra = present.filter((f) => !EXPECTED_TEST_FILES.includes(f));
    assert.deepEqual(
      { faltando: missing, sobrando: extra },
      { faltando: [], sobrando: [] },
      `a suíte de testes da imagem difere da lista congelada (raiz ${ENGINE_ROOT})`,
    );
    assert.deepEqual(present, EXPECTED_TEST_FILES);
  });

  it('os helpers de teste existem', () => {
    for (const rel of EXPECTED_HELPERS) {
      const abs = path.join(ENGINE_ROOT, rel);
      assert.ok(fs.existsSync(abs) && fs.statSync(abs).isFile(), `helper ausente: ${rel}`);
    }
  });

  for (const [name, base] of [
    ['cq-test-course', TEST_COURSE],
    ['cq-override-course', OVERRIDE_COURSE],
  ] as const) {
    it(`o curso de teste ${name} está montado em /repos/<sha256(repo)>/<commit>/`, () => {
      const courseRoot = path.join(REPOS_ROOT, sha256Hex(base.repo), base.commit);
      assert.ok(
        fs.existsSync(courseRoot) && fs.statSync(courseRoot).isDirectory(),
        `diretório do curso ausente: ${courseRoot}`,
      );
      const info = path.join(courseRoot, 'infoCourse.json');
      assert.ok(fs.existsSync(info) && fs.statSync(info).isFile(), `infoCourse.json ausente em ${courseRoot}`);
      const questions = path.join(courseRoot, 'questions');
      assert.ok(
        fs.existsSync(questions) && fs.statSync(questions).isDirectory(),
        `diretório questions/ ausente em ${courseRoot}`,
      );
      // O curso montado é o das fixtures, e não outro.
      const fixtureInfo = path.join(ENGINE_ROOT, 'test', 'fixtures', 'courses', name, 'infoCourse.json');
      assert.equal(
        fs.readFileSync(info, 'utf8'),
        fs.readFileSync(fixtureInfo, 'utf8'),
        `o infoCourse.json montado em /repos difere de test/fixtures/courses/${name}/`,
      );
    });
  }
});
