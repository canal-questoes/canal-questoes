// loadConfig e o AUTOTESTE "ambiente da imagem".
// Cobre o tempo máximo configurável (padrão 10 s como o PL), o número configurável de
// executores (padrão = CPUs disponíveis), as versões (engine_version do package.json, pl_commit da
// ENV) e o determinismo (ambiente herdado do Dockerfile de execução: PYTHONHASHSEED=0, PL_COMMIT e
// NODE_OPTIONS ausente).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, test } from 'node:test';

import { loadConfig } from '../../src/config.ts';
import { NotImplementedError } from '../../src/errors.ts';

const PL_COMMIT = 'a193f72048530a4499252c454acca963cdf98922';
const ENGINE_ROOT = path.resolve(import.meta.dirname, '..', '..');

/** Erro de validação da configuração (e não a ausência de implementação). */
function isConfigError(err: unknown): boolean {
  return err instanceof Error && !(err instanceof NotImplementedError) && err.message.length > 0;
}

describe('loadConfig', () => {
  test('aplica os valores padrão', () => {
    const config = loadConfig({ PL_COMMIT });
    assert.equal(config.reposRoot, '/repos');
    assert.ok(Number.isInteger(config.workers) && config.workers >= 1, `workers inválido: ${config.workers}`);
    assert.equal(config.workers, os.availableParallelism(), 'padrão de workers = os.availableParallelism()');
    assert.equal(config.questionTimeoutMs, 10000, 'padrão do PL: 10 s por chamada');
    assert.ok(path.isAbsolute(config.contractsDir), `contractsDir deve ser absoluto: ${config.contractsDir}`);
    assert.ok(
      path.resolve(config.contractsDir).endsWith(path.join('contracts', 'engine', 'v1')),
      `contractsDir deve terminar em contracts/engine/v1: ${config.contractsDir}`,
    );
    assert.ok(
      fs.existsSync(path.join(config.contractsDir, 'engine-api.schema.json')),
      `contractsDir padrão deve conter os esquemas: ${config.contractsDir}`,
    );
    assert.equal(config.plCommit, PL_COMMIT);
    assert.equal(config.engineVersion, '1.0.0');
  });

  test('as variáveis de ambiente sobrepõem os padrões', () => {
    // Diretórios que existem de fato, para que só a leitura das variáveis seja testada.
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cq-config-'));
    try {
      const reposRoot = path.join(tmp, 'outra-area');
      const contractsDir = path.join(tmp, 'contratos');
      fs.mkdirSync(reposRoot);
      fs.mkdirSync(contractsDir);
      for (const name of ['ir.schema.json', 'engine-api.schema.json']) {
        fs.copyFileSync(path.join(ENGINE_ROOT, 'contracts', 'engine', 'v1', name), path.join(contractsDir, name));
      }
      const config = loadConfig({
        PL_COMMIT,
        ENGINE_REPOS_ROOT: reposRoot,
        ENGINE_WORKERS: '3',
        ENGINE_QUESTION_TIMEOUT_MS: '5000',
        ENGINE_CONTRACTS_DIR: contractsDir,
      });
      assert.equal(config.reposRoot, reposRoot);
      assert.equal(config.workers, 3);
      assert.equal(config.questionTimeoutMs, 5000);
      assert.equal(config.contractsDir, contractsDir);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  test('ENGINE_WORKERS "0" ou "abc" lança erro', () => {
    // Controle: o mesmo ambiente com um valor válido carrega.
    assert.equal(loadConfig({ PL_COMMIT, ENGINE_WORKERS: '2' }).workers, 2);
    for (const workers of ['0', 'abc']) {
      assert.throws(
        () => loadConfig({ PL_COMMIT, ENGINE_WORKERS: workers }),
        isConfigError,
        `ENGINE_WORKERS=${JSON.stringify(workers)} deveria ser recusado`,
      );
    }
  });

  test('sem PL_COMMIT lança erro', () => {
    // Controle: com PL_COMMIT, carrega.
    assert.equal(loadConfig({ PL_COMMIT }).plCommit, PL_COMMIT);
    assert.throws(() => loadConfig({}), isConfigError, 'sem PL_COMMIT a configuração deveria ser recusada');
  });

  test('engineVersion = 1.0.0, lido do package.json', () => {
    const pkg = JSON.parse(fs.readFileSync(path.join(ENGINE_ROOT, 'package.json'), 'utf8'));
    assert.equal(pkg.version, '1.0.0', 'package.json do engine deve estar na versão 1.0.0');
    assert.equal(loadConfig({ PL_COMMIT }).engineVersion, pkg.version);
  });
});

// AUTOTESTE (verde já no esqueleto): o ambiente herdado do Dockerfile de execução.
test('ambiente da imagem: PYTHONHASHSEED=0, PL_COMMIT fixado e NODE_OPTIONS ausente', () => {
  assert.equal(process.env.PYTHONHASHSEED, '0', 'PYTHONHASHSEED deve ser 0 (determinismo)');
  assert.equal(process.env.PL_COMMIT, PL_COMMIT, 'PL_COMMIT deve ser o commit fixado do PL');
  assert.ok(
    process.env.NODE_OPTIONS === undefined || process.env.NODE_OPTIONS === '',
    `NODE_OPTIONS deve estar ausente ou vazio: ${JSON.stringify(process.env.NODE_OPTIONS)}`,
  );
});
