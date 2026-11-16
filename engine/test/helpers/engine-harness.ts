// Motor em processo para os testes.
import assert from 'node:assert/strict';

import { loadConfig } from '../../src/config.ts';
import { createEngine, type Engine, type EngineResult } from '../../src/engine.ts';

export type LogEntry = Record<string, unknown>;

export async function withEngine<T>(
  overrides: Record<string, string>,
  fn: (engine: Engine) => Promise<T>,
  opts: { log?: (entry: LogEntry) => void } = {},
): Promise<T> {
  const config = loadConfig({
    ...process.env,
    ENGINE_WORKERS: '2',
    ENGINE_QUESTION_TIMEOUT_MS: '5000',
    ...overrides,
  });
  if (opts.log) config.log = opts.log;
  const engine = await createEngine(config);
  try {
    return await fn(engine);
  } finally {
    await engine.close();
  }
}

/** Exige status 200 e devolve o corpo. */
export function ok200(result: EngineResult): any {
  assert.equal(
    result.status,
    200,
    `esperado 200, veio ${result.status}: ${JSON.stringify(result.body).slice(0, 1500)}`,
  );
  return result.body as any;
}

export function captureLog(): { log: (entry: LogEntry) => void; entries: LogEntry[] } {
  const entries: LogEntry[] = [];
  return { log: (entry) => entries.push(entry), entries };
}
