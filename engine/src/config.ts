// Configuração do motor, lida das variáveis de ambiente por loadConfig (que os testes também
// usam). Um valor fora do formato lança ConfigError na subida, em vez de virar um motor que falha
// em cada pedido. As versões vêm do package.json (engine_version) e da ENV PL_COMMIT do Dockerfile de
// execução.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import type { LogFn } from './log.ts';

export interface EngineConfig {
  reposRoot: string;
  workers: number;
  questionTimeoutMs: number;
  contractsDir: string;
  plCommit: string;
  engineVersion: string;
  /** Endereço do servidor HTTP (main.ts); o Engine não usa. */
  host: string;
  port: number;
  /** Registro de uma linha JSON por pedido. Ausente, vale o padrão: a stdout. */
  log?: LogFn;
}

/** Configuração recusada (variável ausente ou fora do formato). */
export class ConfigError extends Error {
  override name = 'ConfigError';
}

// src/config.ts → raiz do engine (na imagem, /PrairieLearn/apps/prairielearn/canal-questoes-engine).
const ENGINE_ROOT = path.resolve(import.meta.dirname, '..');
const CONTRACT_FILES = ['ir.schema.json', 'engine-api.schema.json'];

const DEFAULTS = {
  reposRoot: '/repos',
  // O PL dá 10 s a cada chamada do código da questão.
  questionTimeoutMs: 10_000,
  contractsDir: path.join(ENGINE_ROOT, 'contracts', 'engine', 'v1'),
  host: '0.0.0.0',
  port: 8080,
};

// Os mesmos formatos de pl_commit e engine_version em engine-api.schema.json ($defs.snapshot).
const PL_COMMIT_PATTERN = /^[0-9a-f]{40}$/;
const VERSION_PATTERN = /^[0-9]+\.[0-9]+\.[0-9]+$/;
const POSITIVE_INTEGER_PATTERN = /^[0-9]+$/;

export function loadConfig(env: Record<string, string | undefined> = process.env): EngineConfig {
  const plCommit = read(env, 'PL_COMMIT');
  if (plCommit === undefined) {
    throw new ConfigError('PL_COMMIT ausente: defina o commit do PrairieLearn da imagem (40 hexadecimais).');
  }
  if (!PL_COMMIT_PATTERN.test(plCommit)) {
    throw new ConfigError(`PL_COMMIT inválido (${JSON.stringify(plCommit)}): use o commit completo, com 40 hexadecimais minúsculos.`);
  }

  const reposRoot = read(env, 'ENGINE_REPOS_ROOT') ?? DEFAULTS.reposRoot;
  requireDirectory('ENGINE_REPOS_ROOT', reposRoot);

  const contractsDir = read(env, 'ENGINE_CONTRACTS_DIR') ?? DEFAULTS.contractsDir;
  requireDirectory('ENGINE_CONTRACTS_DIR', contractsDir);
  for (const file of CONTRACT_FILES) {
    if (!fs.existsSync(path.join(contractsDir, file))) {
      throw new ConfigError(`ENGINE_CONTRACTS_DIR inválido: o arquivo de contrato ${file} não existe no diretório ${contractsDir}.`);
    }
  }

  return {
    reposRoot,
    workers: readInteger(env, 'ENGINE_WORKERS', os.availableParallelism(), 1, 1024),
    questionTimeoutMs: readInteger(env, 'ENGINE_QUESTION_TIMEOUT_MS', DEFAULTS.questionTimeoutMs, 1, 3_600_000),
    contractsDir,
    plCommit,
    engineVersion: engineVersion(),
    host: read(env, 'ENGINE_HOST') ?? DEFAULTS.host,
    port: readInteger(env, 'ENGINE_PORT', DEFAULTS.port, 0, 65_535),
  };
}

/** O valor da variável, aparado; vazio conta como ausente (convenção de ENV em containers). */
function read(env: Record<string, string | undefined>, name: string): string | undefined {
  const value = env[name]?.trim();
  return value === undefined || value === '' ? undefined : value;
}

function readInteger(
  env: Record<string, string | undefined>,
  name: string,
  fallback: number,
  min: number,
  max: number,
): number {
  const text = read(env, name);
  if (text === undefined) return fallback;
  const value = POSITIVE_INTEGER_PATTERN.test(text) ? Number(text) : Number.NaN;
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new ConfigError(`${name} inválido (${JSON.stringify(text)}): use um número inteiro entre ${min} e ${max}.`);
  }
  return value;
}

function requireDirectory(name: string, dir: string): void {
  if (!path.isAbsolute(dir)) {
    throw new ConfigError(`${name} inválido (${JSON.stringify(dir)}): use um caminho absoluto.`);
  }
  let isDirectory = false;
  try {
    isDirectory = fs.statSync(dir).isDirectory();
  } catch {
    // inexistente ou ilegível: mesma mensagem abaixo
  }
  if (!isDirectory) {
    throw new ConfigError(`${name} inválido: o diretório ${dir} não existe ou não pode ser lido.`);
  }
}

let cachedEngineVersion: string | undefined;

/** engine_version = a versão do package.json do engine. */
function engineVersion(): string {
  if (cachedEngineVersion === undefined) {
    const file = path.join(ENGINE_ROOT, 'package.json');
    let version: unknown;
    try {
      version = (JSON.parse(fs.readFileSync(file, 'utf8')) as { version?: unknown }).version;
    } catch {
      throw new ConfigError(`não foi possível ler a versão do motor em ${file}.`);
    }
    if (typeof version !== 'string' || !VERSION_PATTERN.test(version)) {
      throw new ConfigError(`versão do motor inválida em ${file}: use o formato MAIOR.MENOR.CORREÇÃO.`);
    }
    cachedEngineVersion = version;
  }
  return cachedEngineVersion;
}
