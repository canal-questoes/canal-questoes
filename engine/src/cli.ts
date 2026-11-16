// Entrada em lote do motor (sem rede nem porta), usada pela suíte de
// conformidade para rodar o motor com --network none. Lê {requests: [{id, op, body}]} da stdin e
// escreve só {results: [{id, status, body, ms}]} na stdout, na ordem dos pedidos; os registros vão
// para a stderr. O núcleo é o mesmo do servidor HTTP (engine.ts).
// - Uma entrada que não é JSON (ou não é {requests: [...]}) sai com código 1 e "JSON inválido" na
//   stderr.
// - Um item que não é um pedido válido (não é objeto, sem id, op desconhecida, corpo que não é objeto)
//   dá um resultado 400 invalid_request só dele, e os demais seguem.
// Os pedidos são atendidos um de cada vez: o ms de cada resultado mede só aquele pedido.
import { loadConfig } from './config.ts';
import { errorResult } from './contract/error-response.ts';
import { BATCH_OPS, type BatchOp, type BatchResultItem } from './contract/types.ts';
import { createEngine, type Engine, type EngineResult } from './engine.ts';
import { jsonLineLogger, requestLogEntry, type LogFn } from './log.ts';

const OPS: ReadonlySet<string> = new Set(BATCH_OPS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString('utf8');
}

function fail(message: string): never {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

function run(engine: Engine, op: BatchOp, body: unknown): Promise<EngineResult> {
  switch (op) {
    case 'health':
      return engine.health();
    case 'inspect':
      return engine.inspect(body);
    case 'create_variant':
      return engine.createVariant(body);
    case 'grade':
      return engine.grade(body);
  }
}

const INVALID_ITEM_MESSAGE = 'Pedido inválido: cada item do lote precisa de "id" (texto), "op" (health, inspect, create_variant ou grade) e "body" (objeto).';

async function runItem(engine: Engine, item: unknown, log: LogFn): Promise<BatchResultItem> {
  const started = performance.now();
  const id = isRecord(item) && typeof item.id === 'string' ? item.id : null;
  const op = isRecord(item) && typeof item.op === 'string' && OPS.has(item.op) ? (item.op as BatchOp) : null;
  // Na consulta de saúde, que não tem corpo, o body pode faltar.
  const bodyOk = isRecord(item) && (isRecord(item.body) || (op === 'health' && item.body === undefined));
  if (id === null || op === null || !isRecord(item) || !bodyOk) {
    const result = errorResult('invalid_request', { message: INVALID_ITEM_MESSAGE, details: ['(item do lote): não é um pedido válido.'] });
    const ms = performance.now() - started;
    log(requestLogEntry({ op: op ?? 'unknown', request: undefined, status: result.status, response: result.body, ms }));
    return { id, status: result.status, body: result.body, ms: Math.round(ms) };
  }
  let result: EngineResult;
  try {
    result = await run(engine, op, item.body);
  } catch {
    result = errorResult('internal_error');
  }
  return { id, status: result.status, body: result.body, ms: Math.round(performance.now() - started) };
}

async function main(): Promise<void> {
  const text = await readStdin();
  let input: unknown;
  try {
    input = JSON.parse(text);
  } catch {
    fail('JSON inválido: a entrada da CLI precisa ser um objeto JSON {"requests": [...]}.');
  }
  if (!isRecord(input) || !Array.isArray(input.requests)) {
    fail('JSON inválido: a entrada da CLI precisa ser um objeto JSON {"requests": [...]}.');
  }

  const log = jsonLineLogger(process.stderr);
  let config;
  try {
    config = loadConfig(process.env);
  } catch (err) {
    fail(`Configuração inválida do motor: ${err instanceof Error ? err.message : String(err)}`);
  }
  config.log = log;
  const engine = await createEngine(config);
  const results: BatchResultItem[] = [];
  try {
    for (const item of input.requests) results.push(await runItem(engine, item, log));
  } finally {
    await engine.close();
  }
  // Sai só depois de a stdout esvaziar: um pipe com saída grande seria truncado por um exit imediato.
  process.stdout.write(JSON.stringify({ results }), () => process.exit(0));
}

main().catch(() => fail('Falha interna do motor na entrada em lote.'));
