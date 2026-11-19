// Erros do motor. InvalidRequestError → 400, NotFoundError → 404 (conforme o contrato).

export class InvalidRequestError extends Error {
  override name = 'InvalidRequestError';
  /** Onde e por que o pedido saiu do contrato, sem copiar valores (vão para error.details). */
  readonly details: string[] | undefined;

  constructor(message: string, opts: { details?: readonly string[] } = {}) {
    super(message);
    this.details = opts.details === undefined ? undefined : [...opts.details];
  }
}

export class NotFoundError extends Error {
  override name = 'NotFoundError';
}

export class NotImplementedError extends Error {
  override name = 'NotImplementedError';
  constructor(what: string) {
    super(`não implementado: ${what}`);
  }
}
