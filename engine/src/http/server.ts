// Servidor HTTP interno do motor (as rotas de contracts/engine/v1), com node:http e
// sem framework. As rotas só transportam o corpo do núcleo (engine.ts): o status e o corpo de cada
// operação são os do Engine, que também registra o pedido. O servidor responde, sozinho, 413 (corpo
// acima do limite, descartando o restante), 404 route_not_found (inclusive num alvo que não se
// interpreta como URL, que é erro do cliente), 405 method_not_allowed, 400 para um corpo que não é
// JSON e 500 para uma falha fora do núcleo (ex.: a leitura do corpo). Nesses casos é
// ele que escreve a linha de pedido (uma por pedido), com a op da rota ("unknown" fora delas),
// o status e o código do erro; nunca o corpo, o caminho nem outro valor do pedido (regra de privacidade).
// Uma resposta que não se serializa (ex.: acima do tamanho máximo de cadeia do V8) vira 500
// internal_error, em vez de derrubar o processo com os pedidos em curso.
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import { errorResult, invalidJsonResult } from '../contract/error-response.ts';
import type { BatchOp } from '../contract/types.ts';
import type { Engine, EngineResult } from '../engine.ts';
import { jsonLineLogger, requestLogEntry, type LogFn } from '../log.ts';

/** Limite do corpo do pedido. */
export const MAX_BODY_BYTES = 2 * 1024 * 1024;

type Route =
  | { op: BatchOp; method: 'GET'; run: (engine: Engine) => Promise<EngineResult> }
  | { op: BatchOp; method: 'POST'; run: (engine: Engine, body: unknown) => Promise<EngineResult> };

const ROUTES: ReadonlyMap<string, Route> = new Map<string, Route>([
  ['/v1/health', { op: 'health', method: 'GET', run: (engine) => engine.health() }],
  ['/v1/inspect', { op: 'inspect', method: 'POST', run: (engine, body) => engine.inspect(body) }],
  ['/v1/variants', { op: 'create_variant', method: 'POST', run: (engine, body) => engine.createVariant(body) }],
  ['/v1/grade', { op: 'grade', method: 'POST', run: (engine, body) => engine.grade(body) }],
]);

/**
 * A rota do caminho do pedido, ou nenhuma. Um alvo que o parser HTTP do Node aceita mas que não se
 * interpreta como URL (ex.: `GET http://[ HTTP/1.1`) não é rota nenhuma: 404 route_not_found, pedido
 * inválido do cliente, e não falha do motor.
 */
function routeOf(url: string | undefined): Route | undefined {
  let pathname: string;
  try {
    pathname = new URL(url ?? '/', 'http://localhost').pathname;
  } catch {
    return undefined;
  }
  return ROUTES.get(pathname);
}

/** Registra e devolve uma resposta do próprio servidor (a op é a da rota, ou "unknown"). */
type OwnReply = (op: string, reply: Reply) => Reply;

interface Reply {
  status: number;
  body: unknown;
  /** O método aceito pela rota, no cabeçalho Allow do 405 (RFC 9110, seção 15.5.6). */
  allow?: string;
}

/** Mensagem do 500 de uma resposta que não pôde ser serializada (uma frase em português). */
const UNSERIALIZABLE_MESSAGE =
  'Falha interna do motor de questões: a resposta não pôde ser montada para envio, por exemplo por exceder o tamanho que o motor consegue enviar.';

/**
 * O corpo em JSON. [F] JSON.stringify lança RangeError acima do tamanho máximo de cadeia do V8 (uma IR
 * enorme) e TypeError num valor sem JSON (BigInt); lançado aqui, derrubaria o processo com todos os
 * pedidos em curso. Nesse caso, a resposta vira 500 internal_error, o único erro do contrato
 * para uma falha do motor, e o registro ganha um evento fixo, sem op: a linha do pedido já foi escrita
 * pelo Engine (uma por pedido).
 */
function serialize(reply: Reply, log: LogFn): { reply: Reply; text: string } {
  try {
    const text: unknown = JSON.stringify(reply.body);
    if (typeof text === 'string') return { reply, text };
  } catch {
    // abaixo
  }
  log({ level: 'error', event: 'response_serialization_error' });
  const fallback = errorResult('internal_error', { message: UNSERIALIZABLE_MESSAGE });
  return { reply: fallback, text: JSON.stringify(fallback.body) };
}

function send(res: http.ServerResponse, reply: Reply, log: LogFn): void {
  if (res.headersSent) return;
  const { reply: sent, text } = serialize(reply, log);
  const headers: http.OutgoingHttpHeaders = {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(text),
  };
  if (sent.allow !== undefined) headers.allow = sent.allow;
  res.writeHead(sent.status, headers);
  res.end(text);
}

type BodyRead = { ok: true; text: string } | { ok: false; tooLarge: true };

/** Lê o corpo até o limite; acima dele, para de acumular e descarta o restante até o fim do envio. */
function readBody(req: http.IncomingMessage, maxBytes: number): Promise<BodyRead> {
  return new Promise((resolve, reject) => {
    const declared = Number(req.headers['content-length']);
    let tooLarge = Number.isFinite(declared) && declared > maxBytes;
    const chunks: Buffer[] = [];
    let size = 0;
    req.on('data', (chunk: Buffer) => {
      if (tooLarge) return;
      size += chunk.length;
      if (size > maxBytes) {
        tooLarge = true;
        chunks.length = 0;
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(tooLarge ? { ok: false, tooLarge: true } : { ok: true, text: Buffer.concat(chunks).toString('utf8') }));
    req.on('error', reject);
  });
}

/**
 * Atende um pedido. As respostas do próprio servidor passam por `own`, que as registra; as do núcleo
 * (route.run) já saem registradas pelo Engine, e registrá-las aqui daria duas linhas por pedido.
 */
async function handle(engine: Engine, req: http.IncomingMessage, maxBytes: number, own: OwnReply): Promise<Reply> {
  const route = routeOf(req.url);
  if (route === undefined) {
    req.resume();
    return own('unknown', errorResult('route_not_found'));
  }
  if (req.method !== route.method) {
    req.resume();
    return own(route.op, { ...errorResult('method_not_allowed'), allow: route.method });
  }
  if (route.method === 'GET') {
    req.resume();
    return route.run(engine);
  }
  const read = await readBody(req, maxBytes);
  if (!read.ok) return own(route.op, errorResult('payload_too_large'));
  let body: unknown;
  try {
    body = JSON.parse(read.text);
  } catch {
    return own(route.op, invalidJsonResult());
  }
  return route.run(engine, body);
}

export async function startServer(
  engine: Engine,
  opts: {
    host: string;
    port: number;
    maxBodyBytes?: number;
    /** Destino das linhas de pedido das respostas do próprio servidor; por padrão a stdout, como o do Engine. */
    log?: LogFn;
  },
): Promise<{ url: string; close(): Promise<void> }> {
  const maxBytes = opts.maxBodyBytes ?? MAX_BODY_BYTES;
  const log = opts.log ?? jsonLineLogger(process.stdout);
  const server = http.createServer((req, res) => {
    const started = performance.now();
    // Sem o corpo do pedido (request: undefined), a entrada sai com qid "" e só os códigos da resposta.
    const own: OwnReply = (op, reply) => {
      log(requestLogEntry({ op, request: undefined, status: reply.status, response: reply.body, ms: performance.now() - started }));
      return reply;
    };
    handle(engine, req, maxBytes, own)
      // Uma falha fora do núcleo (ex.: a conexão caiu no meio da leitura) é falha do motor.
      .catch(() => own(routeOf(req.url)?.op ?? 'unknown', errorResult('internal_error')))
      .then((reply) => send(res, reply, log))
      // Última defesa: nada do envio vira rejeição sem tratamento, que derrubaria o processo.
      .catch(() => res.destroy());
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(opts.port, opts.host, () => {
      server.off('error', reject);
      resolve();
    });
  });
  const address = server.address() as AddressInfo;
  const host = address.family === 'IPv6' ? `[${address.address}]` : address.address;
  return {
    url: `http://${host}:${address.port}`,
    close: () =>
      new Promise<void>((resolve, reject) => {
        server.close((err) => (err ? reject(err) : resolve()));
        server.closeAllConnections();
      }),
  };
}
