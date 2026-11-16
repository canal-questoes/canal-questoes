// O motor pela CLI em lote, num container restrito.
//
// `runEngine(requests)` roda
//   docker run --rm -i --network none --user 65534:65534 --read-only --tmpfs /tmp -e ENGINE_WORKERS=2 \
//     canal-questoes/engine:test node src/cli.ts
// escreve `{"requests": [...]}` na stdin e lê a stdout inteira, que traz só `{"results": [...]}`
// (o protocolo da CLI em lote). A stderr (registros do motor) é guardada à parte. Sem a imagem, ou sem o Docker,
// lança `EnvironmentError`. O processo é chamado sem shell, então os caminhos do container não sofrem a
// conversão do MSYS no Git Bash.
import { EnvironmentError, runProcess, type ProcessResult } from './oracle.ts';

export const ENGINE_TEST_IMAGE = 'canal-questoes/engine:test';

/** As mesmas restrições dos testes da imagem: sem rede, não-root, somente leitura. */
export const RESTRICTED_RUN_ARGS: readonly string[] = [
  'run',
  '--rm',
  '-i',
  '--network',
  'none',
  '--user',
  '65534:65534',
  '--read-only',
  '--tmpfs',
  '/tmp',
];

export type CliOp = 'health' | 'inspect' | 'create_variant' | 'grade';

export interface CliRequest {
  id: string;
  op: CliOp;
  body?: unknown;
}

export interface CliResult {
  id: string;
  status: number;
  body: any;
  ms: number;
}

export interface CliRun {
  /** Na ordem em que a CLI os escreveu. */
  results: CliResult[];
  byId: Map<string, CliResult>;
  /** Registros do motor (stderr), guardados à parte. */
  stderr: string;
  /** Duração da execução inteira do container, em ms. */
  wallMs: number;
}

/** A CLI do motor falhou ou violou o protocolo em lote (não é problema de ambiente). */
export class EngineCliError extends Error {
  override name = 'EngineCliError';
  readonly stderr: string;
  constructor(message: string, stderr: string) {
    super(message);
    this.stderr = stderr;
  }
}

/** Confere que a imagem de teste existe localmente. */
export async function checkImage(image: string = ENGINE_TEST_IMAGE): Promise<void> {
  const result = await runProcess('docker', ['image', 'inspect', '--format', '{{.Id}}', image]);
  if (result.code !== 0) {
    throw new EnvironmentError(
      `a imagem ${image} não está disponível (${firstLine(result.stderr) || `código ${result.code}`}). ` +
        `Construa com: docker build -f engine/Dockerfile -t canal-questoes/engine:dev . && ` +
        `docker build -f engine/test/Dockerfile -t ${image} .`,
    );
  }
}

/** Roda um comando na imagem de teste, com as restrições de isolamento (sem rede, sem root, somente leitura), e a entrada na stdin. */
export async function runInTestImage(
  command: readonly string[],
  input: string,
  dockerArgs: readonly string[] = [],
): Promise<ProcessResult> {
  await checkImage();
  return runProcess('docker', [...RESTRICTED_RUN_ARGS, ...dockerArgs, ENGINE_TEST_IMAGE, ...command], input);
}

/** Uma execução da CLI do motor com todos os pedidos. */
export async function runEngine(requests: readonly CliRequest[]): Promise<CliRun> {
  const started = performance.now();
  const result = await runInTestImage(['node', 'src/cli.ts'], JSON.stringify({ requests }), ['-e', 'ENGINE_WORKERS=2']);
  const wallMs = performance.now() - started;
  if (result.code !== 0) {
    throw new EngineCliError(
      `a CLI do motor (node src/cli.ts em ${ENGINE_TEST_IMAGE}) saiu com código ${result.code}; stderr: ${tail(result.stderr)}`,
      result.stderr,
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(result.stdout);
  } catch {
    throw new EngineCliError(
      `a stdout da CLI do motor não é um único objeto JSON (${result.stdout.length} caracteres; início: ` +
        `${JSON.stringify(result.stdout.slice(0, 200))})`,
      result.stderr,
    );
  }
  const results = checkResults(parsed, requests, result.stderr);
  return { results, byId: new Map(results.map((r) => [r.id, r])), stderr: result.stderr, wallMs };
}

function checkResults(parsed: unknown, requests: readonly CliRequest[], stderr: string): CliResult[] {
  const fail = (why: string): never => {
    throw new EngineCliError(`a saída da CLI do motor viola o protocolo em lote: ${why}`, stderr);
  };
  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) fail('a stdout não é um objeto');
  const results = (parsed as { results?: unknown }).results;
  if (!Array.isArray(results)) return fail('falta a lista "results"');
  const expected = new Set(requests.map((r) => r.id));
  const seen = new Set<string>();
  const out: CliResult[] = [];
  for (const item of results as unknown[]) {
    if (item === null || typeof item !== 'object') return fail('um resultado não é objeto');
    const { id, status, body, ms } = item as Record<string, unknown>;
    if (typeof id !== 'string') return fail(`resultado sem "id" de texto: ${JSON.stringify(item).slice(0, 200)}`);
    if (!expected.has(id)) return fail(`resultado com id desconhecido: ${id}`);
    if (seen.has(id)) return fail(`id repetido nos resultados: ${id}`);
    if (typeof status !== 'number' || !Number.isInteger(status)) return fail(`resultado ${id} sem "status" inteiro`);
    if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return fail(`resultado ${id} sem "ms" válido`);
    seen.add(id);
    out.push({ id, status, body, ms });
  }
  const missing = [...expected].filter((id) => !seen.has(id));
  if (missing.length > 0) fail(`faltam ${missing.length} resultados (ex.: ${missing.slice(0, 3).join(', ')})`);
  return out;
}

function firstLine(text: string): string {
  return text.trim().split(/\r?\n/, 1)[0] ?? '';
}

function tail(text: string, max = 2000): string {
  const trimmed = text.trim();
  return trimmed.length > max ? `…${trimmed.slice(-max)}` : trimmed || '(vazia)';
}
