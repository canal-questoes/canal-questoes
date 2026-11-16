// Servidor HTTP do motor: configuração pelo ambiente (config.ts), o núcleo
// (engine.ts) e as rotas /v1 (http/server.ts). Os registros vão para a stdout, uma linha JSON por
// entrada. SIGTERM e SIGINT encerram o servidor e os executores Python.
import { ConfigError, loadConfig, type EngineConfig } from './config.ts';
import { createEngine } from './engine.ts';
import { startServer } from './http/server.ts';
import { jsonLineLogger } from './log.ts';

const log = jsonLineLogger(process.stdout);

let config: EngineConfig;
try {
  config = loadConfig(process.env);
} catch (err) {
  process.stderr.write(`${err instanceof ConfigError ? err.message : 'Configuração inválida do motor.'}\n`);
  process.exit(1);
}
config.log = log;

const engine = await createEngine(config);
let server: Awaited<ReturnType<typeof startServer>>;
try {
  server = await startServer(engine, { host: config.host, port: config.port });
} catch (err) {
  // Ex.: porta em uso. Só o código do erro vai para o registro, sem mensagem nem pilha.
  const code = (err as NodeJS.ErrnoException | null)?.code;
  log({ level: 'error', event: 'listen_failed', host: config.host, port: config.port, code: typeof code === 'string' ? code : 'unknown' });
  await engine.close();
  process.exit(1);
}
log({
  level: 'info',
  event: 'started',
  url: server.url,
  workers: config.workers,
  engine_version: config.engineVersion,
  pl_commit: config.plCommit,
});

let stopping = false;
async function stop(signal: string): Promise<void> {
  if (stopping) return;
  stopping = true;
  log({ level: 'info', event: 'stopping', signal });
  try {
    await server.close();
  } finally {
    await engine.close();
    process.exit(0);
  }
}

process.on('SIGTERM', () => void stop('SIGTERM'));
process.on('SIGINT', () => void stop('SIGINT'));
