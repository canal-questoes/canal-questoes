// Prontidão do motor para a saúde (GET /v1/health). O motor está pronto quando consegue
// um executor Python: é isso que toda operação com código da questão precisa.
// [F] No PL a193f72 (dist/lib/code-caller/index.js, init), o pool nasce com min = número de workers,
// o servidor espera pool.ready() antes de atender e cada falha de criação é registrada
// (factoryCreateError, "Error creating Python worker"). Aqui o pool cria sob demanda (pool.ts), e a
// saúde guarda o resultado do último empréstimo:
// - um executor chegou à fase (foi criado ou estava livre e vivo): pronto;
// - o empréstimo falhou antes da fase (a criação falhou, ou três executores morreram seguidos): não
//   pronto, com o evento fixo executor_unavailable no registro, sem a mensagem do erro;
// - nada ainda, ou não pronto: a saúde pede um executor ao mesmo pool, sem rodar código nenhum. Esse
//   executor aquece o pool para a operação seguinte, e o pool nunca passa de `size` executores.
// O empréstimo da saúde é o de qualquer fase (prepareForCourse, confere o worker, restart()), e por
// isso o executor volta ao pool no mesmo estado de um executor que atendeu uma fase.
import type { CodeCaller } from './bindings.ts';
import { Executors, type ExecutorOptions } from './executors.ts';
import { PoolClosedError } from './pool.ts';

/** Mensagem do 500 da saúde quando nenhum executor pôde ser obtido (uma frase em português). */
export const NOT_READY_MESSAGE =
  'O motor de questões não está pronto: nenhum executor Python pôde ser iniciado; o pedido pode ser repetido.';

type Readiness = 'unknown' | 'ready' | 'unavailable';

export class TrackedExecutors extends Executors {
  #state: Readiness = 'unknown';
  /** O empréstimo da saúde em curso; um só, para qualquer número de consultas simultâneas. */
  #probe: Promise<void> | null = null;
  /** Consultas da saúde à espera do próximo resultado de empréstimo, de qualquer origem. */
  readonly #waiting: (() => void)[] = [];
  readonly #logEntry: ExecutorOptions['log'];

  constructor(opts: ExecutorOptions) {
    super(opts);
    this.#logEntry = opts.log;
  }

  /** O withCodeCaller de Executors, guardando se o empréstimo chegou à fase. */
  override async withCodeCaller<R>(coursePath: string, fn: (codeCaller: CodeCaller) => Promise<R>): Promise<R> {
    let reached = false;
    try {
      return await super.withCodeCaller(coursePath, (codeCaller) => {
        if (!reached) {
          reached = true;
          this.#settle('ready');
        }
        return fn(codeCaller);
      });
    } catch (err) {
      // Depois de close(), a recusa do pool não diz nada do ambiente de execução.
      if (!reached && !(err instanceof PoolClosedError)) {
        this.#logEntry({ level: 'error', event: 'executor_unavailable' });
        this.#settle('unavailable');
      }
      throw err;
    }
  }

  /**
   * Um executor pode ser obtido? Pronto, responde na hora. Senão, pede um executor ao pool (ou espera
   * o pedido em curso) e responde com o primeiro resultado de empréstimo que chegar, o dele ou o de
   * uma operação. `coursePath` só vai para o prepareForCourse, que no CodeCallerNative só o guarda:
   * nenhuma chamada é feita.
   */
  async ready(coursePath: string): Promise<boolean> {
    if (this.#isReady()) return true;
    const settled = new Promise<void>((resolve) => this.#waiting.push(resolve));
    let probe = this.#probe;
    if (probe === null) {
      probe = this.withCodeCaller(coursePath, async () => undefined).then(
        () => undefined,
        () => undefined, // o resultado já ficou em #state
      );
      this.#probe = probe;
      void probe.finally(() => {
        if (this.#probe === probe) this.#probe = null;
      });
    }
    await Promise.race([probe, settled]);
    return this.#isReady();
  }

  /** Lido de novo depois de cada espera: o estado muda em qualquer empréstimo. */
  #isReady(): boolean {
    return this.#state === 'ready';
  }

  #settle(state: Exclude<Readiness, 'unknown'>): void {
    this.#state = state;
    for (const resolve of this.#waiting.splice(0)) resolve();
  }
}
