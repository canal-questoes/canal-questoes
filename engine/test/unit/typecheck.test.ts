// AUTOTESTE (verde já no esqueleto): checagem de tipos do engine.
// Cobre o TypeScript executado direto pelo Node (sintaxe apagável, checagem com o TypeScript 6 do
// node_modules do PL) e o rigor do tsconfig.json, que faz parte do oráculo e fica congelado.
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { test } from 'node:test';

const ENGINE_ROOT = path.resolve(import.meta.dirname, '..', '..');

function tscPath(): string {
  const requirePl = createRequire('/PrairieLearn/apps/prairielearn/');
  const dir = path.dirname(requirePl.resolve('typescript/package.json'));
  return path.join(dir, 'lib', 'tsc.js');
}

test('tsconfig.json exige strict, noEmit, erasableSyntaxOnly e inclui src, test e conformance', () => {
  const tsconfig = JSON.parse(fs.readFileSync(path.join(ENGINE_ROOT, 'tsconfig.json'), 'utf8'));
  const opts = tsconfig.compilerOptions ?? {};
  assert.equal(opts.strict, true, 'compilerOptions.strict deve ser true');
  assert.equal(opts.noEmit, true, 'compilerOptions.noEmit deve ser true');
  assert.equal(opts.erasableSyntaxOnly, true, 'compilerOptions.erasableSyntaxOnly deve ser true');
  const include: unknown = tsconfig.include;
  assert.ok(Array.isArray(include), 'include deve ser uma lista');
  for (const dir of ['src', 'test', 'conformance']) {
    assert.ok(
      include.some((p: unknown) => typeof p === 'string' && (p === dir || p.startsWith(`${dir}/`))),
      `include deve conter ${dir}: ${JSON.stringify(include)}`,
    );
  }
});

test('o TypeScript 6 do PL aceita o projeto (tsc -p tsconfig.json sai com 0)', { timeout: 300_000 }, () => {
  const tsc = tscPath();
  const version = spawnSync(process.execPath, [tsc, '--version'], { cwd: ENGINE_ROOT, encoding: 'utf8' });
  assert.equal(version.status, 0, `tsc --version falhou: ${version.stderr}`);
  assert.ok(version.stdout.startsWith('Version 6.'), `esperado TypeScript 6, veio: ${version.stdout.trim()}`);

  const check = spawnSync(process.execPath, [tsc, '-p', 'tsconfig.json'], {
    cwd: ENGINE_ROOT,
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  assert.equal(
    check.status,
    0,
    `a checagem de tipos falhou (código ${check.status}):\n${check.stdout}\n${check.stderr}`,
  );
});
