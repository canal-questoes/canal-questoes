// Proveniência das imagens do motor, no host.
// Cobre: a reutilização do PL fixado e a fidelidade ao PL (a imagem do motor deriva da imagem oficial do PL no digest
// fixado). As imagens canal-questoes/engine:dev e :test precisam começar exatamente pelas camadas da
// imagem base. Os arquivos do PL reutilizados são conferidos dentro da imagem por
// engine/test/unit/pl-provenance.test.ts.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

const PL_IMAGE = 'prairielearn/prairielearn@sha256:c2b033eba27657a022ca82535cf48b4098bae02609e7f23114512bcf2d0ea4b6';

function layers(image: string): string[] {
  let out: string;
  try {
    out = execFileSync('docker', ['image', 'inspect', image, '--format', '{{json .RootFS.Layers}}'], { encoding: 'utf8' });
  } catch (err) {
    throw new Error(`imagem ${image} indisponível no Docker local (construa com os comandos do engine/README.md): ${String(err)}`);
  }
  return JSON.parse(out) as string[];
}

test('as imagens do motor derivam da imagem oficial do PL no digest fixado', () => {
  const base = layers(PL_IMAGE);
  assert.ok(base.length > 0, 'a imagem base não tem camadas');
  for (const image of ['canal-questoes/engine:dev', 'canal-questoes/engine:test']) {
    const own = layers(image);
    assert.ok(own.length > base.length, `${image} deveria acrescentar camadas à imagem do PL`);
    assert.deepEqual(own.slice(0, base.length), base, `${image} não parte de ${PL_IMAGE}`);
  }
});
