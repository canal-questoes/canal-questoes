// Corpo de erro do contrato ($defs.error_response) e o status HTTP de cada código.
// As mensagens são frases em português para quem opera o núcleo, sem pilha, detalhe interno
// nem caminho do servidor, e chamam o serviço de "motor de questões". Só o 500 é
// passível de nova tentativa pelo núcleo, e as mensagens dele dizem isso; uma falha do motor
// nunca expõe a mensagem da exceção.
import { InvalidRequestError, NotFoundError } from '../errors.ts';
import type { ErrorCode, ErrorResponse } from './types.ts';

export const ERROR_STATUS = {
  invalid_request: 400,
  not_found: 404,
  payload_too_large: 413,
  route_not_found: 404,
  method_not_allowed: 405,
  internal_error: 500,
} as const satisfies Record<ErrorCode, number>;

export type ErrorStatus = (typeof ERROR_STATUS)[ErrorCode];

export const ERROR_MESSAGES: Readonly<Record<ErrorCode, string>> = {
  invalid_request: 'Pedido inválido: o corpo não segue o contrato da API do motor de questões.',
  not_found: 'Questão não encontrada: o repositório, o commit ou o QID não existe na área de repositórios do motor.',
  payload_too_large: 'O corpo do pedido excede o tamanho máximo de 2 MiB.',
  route_not_found: 'Rota desconhecida: as rotas da API são GET /v1/health e POST /v1/inspect, /v1/variants e /v1/grade.',
  method_not_allowed: 'Método não permitido nesta rota: /v1/health aceita só GET, e /v1/inspect, /v1/variants e /v1/grade, só POST.',
  internal_error: 'Falha interna do motor de questões; o pedido pode ser repetido.',
};

/** Mensagens de situações específicas, com o código correspondente. */
export const SPECIFIC_MESSAGES = {
  invalidJson: 'Pedido inválido: o corpo não é JSON válido.',
  engineClosed: 'O motor de questões foi encerrado e não atende mais pedidos; o pedido pode ser repetido depois que ele for reiniciado.',
} as const;

export interface ErrorResult<S extends ErrorStatus = ErrorStatus> {
  status: S;
  body: ErrorResponse;
}

export function errorResponse(code: ErrorCode, message: string = ERROR_MESSAGES[code], details?: readonly string[]): ErrorResponse {
  const error: ErrorResponse['error'] = { code, message: message.trim() || ERROR_MESSAGES[code] };
  if (details !== undefined && details.length > 0) error.details = [...details];
  return { error };
}

export function errorResult<C extends ErrorCode>(
  code: C,
  opts: { message?: string; details?: readonly string[] } = {},
): ErrorResult<(typeof ERROR_STATUS)[C]> {
  return { status: ERROR_STATUS[code], body: errorResponse(code, opts.message, opts.details) };
}

/** 400 de um corpo fora do esquema, com os details da validação (schemas.ts). */
export function invalidRequestResult(details: readonly string[], message?: string): ErrorResult<400> {
  return errorResult('invalid_request', { message, details });
}

/** 400 de um corpo que não é JSON (servidor HTTP e itens da CLI). */
export function invalidJsonResult(): ErrorResult<400> {
  return errorResult('invalid_request', {
    message: SPECIFIC_MESSAGES.invalidJson,
    details: ['(corpo): o texto recebido não pôde ser interpretado como JSON.'],
  });
}

/** 500 depois de engine.close(). */
export function engineClosedResult(): ErrorResult<500> {
  return errorResult('internal_error', { message: SPECIFIC_MESSAGES.engineClosed });
}

/**
 * Mapeia uma exceção para o erro do contrato: InvalidRequestError → 400, NotFoundError → 404 (com a
 * mensagem do próprio erro, que o motor escreve em português) e qualquer outra → 500 com a mensagem
 * fixa, sem a mensagem nem a pilha da exceção.
 */
export function errorResultFromException(err: unknown): ErrorResult<400 | 404 | 500> {
  if (err instanceof InvalidRequestError) {
    return errorResult('invalid_request', { message: err.message, details: detailsOf(err) });
  }
  if (err instanceof NotFoundError) {
    return errorResult('not_found', { message: err.message });
  }
  return errorResult('internal_error');
}

/** details opcionais anexados a um InvalidRequestError (lista de textos). */
function detailsOf(err: Error): string[] | undefined {
  const details: unknown = (err as { details?: unknown }).details;
  return Array.isArray(details) && details.every((d) => typeof d === 'string') ? details : undefined;
}
