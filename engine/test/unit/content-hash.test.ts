// contentHash: hash do conteúdo do diretório da questão.
// Cobre o hash do conteúdo da questão na inspeção, pela regra: sha256 sobre, para cada
// arquivo em ordem lexicográfica do caminho relativo (separador "/"),
// caminho + "\0" + tamanho em decimal + "\0" + bytes + "\0"; diretórios vazios não contam.
// O valor esperado é calculado NO PRÓPRIO TESTE, independente de src/.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';

import { contentHash } from '../../src/core/content-hash.ts';

type Files = Record<string, Buffer>;

/** Regra do hash de conteúdo, implementada no teste. */
function expectedHash(files: Files): string {
  const names = Object.keys(files).sort((a, b) => Buffer.compare(Buffer.from(a, 'utf8'), Buffer.from(b, 'utf8')));
  const h = createHash('sha256');
  for (const name of names) {
    const bytes = files[name];
    h.update(Buffer.from(name, 'utf8'));
    h.update(Buffer.from([0]));
    h.update(Buffer.from(String(bytes.length), 'utf8'));
    h.update(Buffer.from([0]));
    h.update(bytes);
    h.update(Buffer.from([0]));
  }
  return `sha256:${h.digest('hex')}`;
}

async function writeTree(dir: string, files: Files, order: string[] = Object.keys(files)): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
  for (const name of order) {
    const full = path.join(dir, ...name.split('/'));
    await fs.mkdir(path.dirname(full), { recursive: true });
    await fs.writeFile(full, files[name]);
  }
}

function baseFiles(): Files {
  // Bytes 0..255 (inclui \0) e texto UTF-8 com acentos (tamanho em bytes ≠ número de caracteres).
  const binary = Buffer.alloc(256);
  for (let i = 0; i < 256; i++) binary[i] = i;
  return {
    'info.json': Buffer.from('{"uuid":"00000000-0000-4000-8000-000000000001","title":"Questão de teste","type":"v3"}\n', 'utf8'),
    'question.html': Buffer.from('<pl-question-panel>x = {{params.x}}</pl-question-panel>\n', 'utf8'),
    'server.py': Buffer.from('def generate(data):\n    data["params"]["x"] = 3\n', 'utf8'),
    'clientFilesQuestion/figura.png': binary,
    'tests/sub/dados.txt': Buffer.from('ação', 'utf8'),
  };
}

describe('contentHash', () => {
  let base = '';
  let counter = 0;

  /** Monta um diretório novo em /tmp com os arquivos dados. */
  async function tree(files: Files, order?: string[]): Promise<string> {
    counter += 1;
    const dir = path.join(base, `q${counter}`);
    await writeTree(dir, files, order);
    return dir;
  }

  before(async () => {
    base = await fs.mkdtemp(path.join(os.tmpdir(), 'cq-hash-'));
  });

  after(async () => {
    if (base) await fs.rm(base, { recursive: true, force: true });
  });

  test('bate com o valor calculado pela regra do hash de conteúdo', async () => {
    const files = baseFiles();
    const dir = await tree(files);
    const got = await contentHash(dir);
    assert.match(got, /^sha256:[0-9a-f]{64}$/);
    assert.equal(got, expectedHash(files));
  });

  test('mudar um byte muda o hash', async () => {
    const files = baseFiles();
    const original = await contentHash(await tree(files));
    const changed = baseFiles();
    changed['clientFilesQuestion/figura.png'][255] = 0;
    const got = await contentHash(await tree(changed));
    assert.notEqual(got, original);
    assert.equal(got, expectedHash(changed));
  });

  test('renomear um arquivo muda o hash', async () => {
    const files = baseFiles();
    const original = await contentHash(await tree(files));
    const renamed = baseFiles();
    renamed['server2.py'] = renamed['server.py'];
    delete renamed['server.py'];
    const got = await contentHash(await tree(renamed));
    assert.notEqual(got, original);
    assert.equal(got, expectedHash(renamed));
  });

  test('acrescentar um arquivo muda o hash', async () => {
    const files = baseFiles();
    const original = await contentHash(await tree(files));
    const added = { ...baseFiles(), 'clientFilesQuestion/nova.txt': Buffer.from('', 'utf8') };
    const got = await contentHash(await tree(added));
    assert.notEqual(got, original);
    assert.equal(got, expectedHash(added));
  });

  test('a ordem de criação dos arquivos não muda o hash', async () => {
    const files = baseFiles();
    const forward = await contentHash(await tree(files));
    const reversed = await contentHash(await tree(files, Object.keys(files).reverse()));
    assert.equal(reversed, forward);
  });

  test('diretórios vazios não mudam o hash', async () => {
    const files = baseFiles();
    const without = await contentHash(await tree(files));
    const dir = await tree(files);
    await fs.mkdir(path.join(dir, 'vazio'));
    await fs.mkdir(path.join(dir, 'tests', 'vazio', 'mais-fundo'), { recursive: true });
    const withEmpty = await contentHash(dir);
    assert.equal(withEmpty, without);
    assert.equal(withEmpty, expectedHash(files));
  });
});
