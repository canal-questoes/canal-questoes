// Pool de executores. Faz o papel do generic-pool que o PL usa em
// dist/lib/code-caller/index.js: no máximo `size` executores vivos, criados sob demanda, e os pedidos
// excedentes esperam numa fila FIFO, sem recusa. O pool não conhece o CodeCaller: o restart() depois de
// cada fase e a decisão de descarte ficam com quem chama (withCodeCaller sobre use/discardOn/discard).
// Como a opção testOnBorrow do generic-pool (que o PL não liga), validate() confere um executor livre
// antes de emprestá-lo: o que não passa é destruído, e o pedido espera outro (um novo, se for preciso).
// Sem validate(), o comportamento é o de antes.

/** Erro de uso do pool depois de close(). */
export class PoolClosedError extends Error {
  override name = 'PoolClosedError';
  constructor() {
    super('pool de executores fechado');
  }
}

interface Waiter<T> {
  resolve: (w: T) => void;
  reject: (err: unknown) => void;
}

export class WorkerPool<T> {
  readonly size: number;

  readonly #create: () => Promise<T>;
  readonly #destroy: (w: T) => Promise<void> | void;
  readonly #validate: ((w: T) => boolean) | undefined;
  /** Executores vivos (livres ou em uso); um executor só sai daqui depois de destruído. */
  readonly #alive = new Set<T>();
  readonly #idle: T[] = [];
  readonly #busy = new Set<T>();
  /** Executores em uso marcados por discard(): são destruídos quando o uso termina. */
  readonly #marked = new Set<T>();
  readonly #waiters: Waiter<T>[] = [];
  #creating = 0;
  #closed = false;
  #closing: Promise<void> | null = null;
  #onDrained: (() => void) | null = null;

  constructor(opts: {
    size: number;
    create: () => Promise<T>;
    destroy: (w: T) => Promise<void> | void;
    /** Um executor livre ainda serve? Chamado antes de cada empréstimo de um executor livre. */
    validate?: (w: T) => boolean;
  }) {
    if (!Number.isInteger(opts.size) || opts.size < 1) {
      throw new RangeError(`tamanho do pool inválido: ${String(opts.size)}`);
    }
    this.size = opts.size;
    this.#create = opts.create;
    this.#destroy = opts.destroy;
    this.#validate = opts.validate;
  }

  /**
   * Empresta um executor a `fn` e o devolve ao fim. Se `fn` falhar e `discardOn(err)` for verdadeiro,
   * ou se o executor tiver sido marcado com discard(), ele é destruído, e o próximo uso cria outro.
   * O erro de `fn` sempre se propaga.
   */
  async use<R>(fn: (w: T) => Promise<R>, opts?: { discardOn?: (err: unknown) => boolean }): Promise<R> {
    const w = await this.#acquire();
    let shouldDiscard = false;
    try {
      return await fn(w);
    } catch (err) {
      if (opts?.discardOn) {
        try {
          shouldDiscard = opts.discardOn(err) === true;
        } catch {
          // Na dúvida, o executor não volta ao pool.
          shouldDiscard = true;
        }
      }
      throw err;
    } finally {
      await this.#release(w, shouldDiscard);
    }
  }

  /** Marca o executor em uso para recriação: ao fim do uso ele é destruído em vez de voltar ao pool. */
  discard(w: T): void {
    if (this.#busy.has(w)) {
      this.#marked.add(w);
      return;
    }
    const i = this.#idle.indexOf(w);
    if (i >= 0) {
      this.#idle.splice(i, 1);
      void this.#retire(w);
    }
  }

  /**
   * Fecha o pool: recusa os pedidos em fila e os novos, destrói os executores livres e espera os em uso
   * (e os que estão sendo criados) terminarem para destruí-los também. Idempotente.
   */
  close(): Promise<void> {
    this.#closing ??= this.#drain();
    return this.#closing;
  }

  async #drain(): Promise<void> {
    this.#closed = true;
    for (const waiter of this.#waiters.splice(0)) waiter.reject(new PoolClosedError());
    const drained = new Promise<void>((resolve) => {
      this.#onDrained = resolve;
    });
    await Promise.all(this.#idle.splice(0).map((w) => this.#retire(w)));
    this.#checkDrained();
    await drained;
  }

  #acquire(): Promise<T> {
    if (this.#closed) return Promise.reject(new PoolClosedError());
    return new Promise<T>((resolve, reject) => {
      this.#waiters.push({ resolve, reject });
      this.#pump();
    });
  }

  /** Atende a fila: primeiro com executores livres, depois criando novos até o limite. */
  #pump(): void {
    while (this.#waiters.length > 0 && !this.#closed) {
      const idle = this.#idle.shift();
      if (idle !== undefined) {
        if (!this.#isValid(idle)) {
          // Destruído, libera a vaga, e o #retire atende a fila de novo.
          void this.#retire(idle);
          continue;
        }
        this.#busy.add(idle);
        this.#waiters.shift()!.resolve(idle);
        continue;
      }
      if (this.#alive.size + this.#creating >= this.size) return;
      const waiter = this.#waiters.shift()!;
      this.#creating += 1;
      void this.#spawn(waiter);
    }
  }

  #isValid(w: T): boolean {
    if (this.#validate === undefined) return true;
    try {
      return this.#validate(w) === true;
    } catch {
      // Na dúvida, o executor não é emprestado.
      return false;
    }
  }

  async #spawn(waiter: Waiter<T>): Promise<void> {
    let w: T;
    try {
      w = await this.#create();
    } catch (err) {
      // A vaga volta para a fila; o próximo pedido tenta criar de novo.
      this.#creating -= 1;
      waiter.reject(err);
      this.#pump();
      this.#checkDrained();
      return;
    }
    this.#creating -= 1;
    if (this.#closed) {
      this.#alive.add(w);
      await this.#retire(w);
      waiter.reject(new PoolClosedError());
      return;
    }
    this.#alive.add(w);
    this.#busy.add(w);
    waiter.resolve(w);
  }

  async #release(w: T, discard: boolean): Promise<void> {
    this.#busy.delete(w);
    const marked = this.#marked.delete(w);
    if (discard || marked || this.#closed) {
      await this.#retire(w);
      return;
    }
    this.#idle.push(w);
    this.#pump();
  }

  /** Destrói o executor e só então libera a vaga, para nunca haver mais de `size` vivos. */
  async #retire(w: T): Promise<void> {
    try {
      await this.#destroy(w);
    } catch {
      // Falha ao destruir não impede a recriação; o pool não registra nada (sem dados da questão).
    }
    this.#alive.delete(w);
    this.#pump();
    this.#checkDrained();
  }

  #checkDrained(): void {
    if (this.#closed && this.#alive.size === 0 && this.#creating === 0 && this.#onDrained) {
      const done = this.#onDrained;
      this.#onDrained = null;
      done();
    }
  }
}
