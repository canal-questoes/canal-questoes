// Executores Python: um WorkerPool de CodeCallerNative do PL, com o mesmo
// withCodeCaller de dist/lib/code-caller/index.js (PL a193f72): cada fase pega um executor, prepara o
// curso, roda e devolve com restart(). O zygote semeia random e numpy.random com variant_seed na
// primeira chamada depois do restart (zygote.py), e é isso que torna as fases iguais às do PL.
// Se o restart pedir uma recriação (tempo esgotado, processo que saiu) ou falhar, o executor é
// descartado e recriado no próximo uso; como no PL, a falha do restart falha a operação, mesmo depois
// de uma fase bem-sucedida, e o motor responde 500 internal_error.
//
// Diferenças deliberadas de orquestração, que não mudam o resultado de uma questão sadia:
// - a saída do Python tem teto por chamada (output-guard.ts); acima dele, o motor mata o executor, a
//   chamada falha e a questão sai quebrada, sem o restart();
// - um executor ocioso que morreu (zygote ou worker mortos por sinal, pelo OOM killer) é trocado sem
//   virar questão quebrada: o pool confere o zygote antes de emprestá-lo (validate), o empréstimo
//   confere também o worker (isReady) antes da fase e de novo na primeira chamada; morto ali, a fase
//   roda outra vez num executor novo, porque nenhum código da questão chegou a rodar nele. Três
//   executores perdidos seguidos são falha do motor (500). Um executor que morre durante a chamada continua como no PL (questão quebrada): a
//   saída do processo não diz se foi o código da questão (exceção, os._exit, memória) ou um sinal;
// - um erro no stdin do zygote (EPIPE) é registrado em vez de derrubar o processo;
// - depois do done(), se o zygote não sai em 2 s, o motor mata o worker e o zygote com SIGKILL;
// - os processos que o código da questão deixa rodando são mortos depois de cada restart() e quando o
//   executor é descartado (leftover-processes.ts), como o zygote do PL faz com drop_privileges; se
//   algum sobra, a limpeza falha como um restart() que falha.
import { ChildProcess } from 'node:child_process';
import fs from 'node:fs';

import { codeCallerErrorLogger, type LogFn } from '../log.ts';
import { CodeCallerNative, type CallType, type CodeCaller, type CodeCallerResult } from './bindings.ts';
import { channelsOf, killLeftovers, type ExecutorChannels, type LeftoverSweep } from './leftover-processes.ts';
import { OutputGuard } from './output-guard.ts';
import { WorkerPool } from './pool.ts';

// O padrão do PL (config.workerPingTimeoutMilliseconds): a subida do zygote importa numpy, matplotlib etc.
const PING_TIMEOUT_MS = 60_000;

// Tentativas de uma fase quando o executor emprestado está morto antes da primeira chamada.
const LOST_EXECUTOR_ATTEMPTS = 3;

/** O processo do zygote (campo child do CodeCallerNative), se ainda existir. */
function childProcessOf(codeCaller: CodeCaller): ChildProcess | null {
  const child = (codeCaller as unknown as { child?: unknown }).child;
  return child instanceof ChildProcess ? child : null;
}

/** O estado interno do CodeCallerNative (um Symbol), como texto, só para o registro. */
function stateOf(codeCaller: CodeCaller): string {
  const state = (codeCaller as unknown as { state?: unknown }).state;
  return typeof state === 'symbol' ? (state.description ?? 'unknown') : 'unknown';
}

function hasExited(child: ChildProcess): boolean {
  return child.exitCode !== null || child.signalCode !== null;
}

let procAvailable: boolean | undefined;

function hasProc(): boolean {
  procAvailable ??= fs.existsSync('/proc/self/stat');
  return procAvailable;
}

/**
 * O processo já terminou, mesmo que o Node ainda não tenha recolhido a saída (estado Z ou X em
 * /proc/<pid>/stat), ou já não existe. Sem /proc, fica só o que o ChildProcess sabe.
 */
function hasTerminated(pid: number): boolean {
  if (!hasProc()) return false;
  let stat: string;
  try {
    stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
  } catch (err) {
    return (err as NodeJS.ErrnoException).code === 'ENOENT';
  }
  // "pid (comm) estado …": o comm pode ter espaços e parênteses; o estado vem depois do último ")".
  const state = stat.charAt(stat.lastIndexOf(')') + 2);
  return state === 'Z' || state === 'X';
}

/** PIDs dos filhos diretos de um processo (Linux: /proc/<pid>/stat, campo ppid). */
function childPids(parent: number): number[] {
  const out: number[] = [];
  let entries: string[] = [];
  try {
    entries = fs.readdirSync('/proc');
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (!/^[0-9]+$/.test(entry)) continue;
    try {
      const stat = fs.readFileSync(`/proc/${entry}/stat`, 'utf8');
      // "pid (comm) estado ppid …": o comm pode ter espaços e parênteses; o ppid vem depois do último ")".
      const ppid = Number(stat.slice(stat.lastIndexOf(')') + 2).split(' ')[1]);
      if (ppid === parent) out.push(Number(entry));
    } catch {
      // o processo saiu durante a varredura
    }
  }
  return out;
}

/** O zygote do executor está vivo, no estado WAITING do PL (pronto para uma chamada). */
function isAlive(codeCaller: CodeCaller): boolean {
  if (stateOf(codeCaller) !== 'WAITING') return false;
  const child = childProcessOf(codeCaller);
  return child !== null && child.pid !== undefined && !hasExited(child) && !hasTerminated(child.pid);
}

/**
 * O zygote vivo e o worker que vai ler a chamada (filho do zygote que não é zumbi). [F] Um worker morto
 * com o executor ocioso não é visto pelo CodeCallerNative: o zygote recolhe o worker, lança
 * RuntimeError e só sai depois de finalizar o Python (zygote.py), o que levou de 0,7 s a mais de 1 s
 * nas sondas. Uma chamada nessa janela iria para um processo que não a lê e sairia como questão
 * quebrada. Sem /proc, vale só o zygote.
 */
function isReady(codeCaller: CodeCaller): boolean {
  if (!isAlive(codeCaller)) return false;
  if (!hasProc()) return true;
  const zygote = childProcessOf(codeCaller)!.pid!;
  return childPids(zygote).some((pid) => !hasTerminated(pid));
}

// Espera pelo worker de um zygote vivo: o zygote confirma o restart no fd 4 antes do fork do worker
// seguinte (zygote.py), e logo depois do restart o worker novo pode ainda não existir.
const WORKER_WAIT_MS = 1_000;
const WORKER_POLL_MS = 2;

/** isReady, esperando até WORKER_WAIT_MS pelo fork do worker; false se o executor não serve. */
async function waitReady(codeCaller: CodeCaller): Promise<boolean> {
  const deadline = performance.now() + WORKER_WAIT_MS;
  for (;;) {
    if (isReady(codeCaller)) return true;
    if (!isAlive(codeCaller) || performance.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, WORKER_POLL_MS));
  }
}

/** Mata com SIGKILL o worker (filho do zygote) e depois o zygote. */
function killExecutor(child: ChildProcess): void {
  if (hasExited(child)) return;
  if (child.pid !== undefined) {
    for (const pid of childPids(child.pid)) {
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        // já saiu
      }
    }
  }
  child.kill('SIGKILL');
}

// Tempo para o zygote sair depois do SIGTERM antes do SIGKILL.
const EXIT_GRACE_MS = 2_000;

/**
 * Garante que o zygote saia. O SIGTERM do CodeCallerNative (done() ou tempo esgotado) é tratado pelo
 * zygote.py, que mata o worker e sai; mas o sinal pode cair numa thread nativa do processo (numpy e
 * afins criam threads), e aí o laço principal continua bloqueado no waitpid, com o worker vivo. Nesse
 * caso, depois de um prazo, o motor mata o worker e o zygote com SIGKILL.
 */
function ensureExit(child: ChildProcess, onForced: () => void): void {
  if (hasExited(child)) return;
  if (!child.killed) child.kill('SIGTERM');
  const timer = setTimeout(() => {
    if (hasExited(child)) return;
    onForced();
    killExecutor(child);
  }, EXIT_GRACE_MS);
  timer.unref();
  child.once('exit', () => clearTimeout(timer));
}

/** Um executor do pool: o CodeCallerNative do PL, o teto de saída do motor e os canais do zygote. */
interface Executor {
  codeCaller: CodeCaller;
  guard: OutputGuard;
  /** Os canais do zygote, para achar os processos deixados pelo código da questão; null sem /proc. */
  channels: ExecutorChannels | null;
}

/** O executor saiu antes da primeira chamada do empréstimo (nenhum código da questão rodou nele). */
class ExecutorLostError extends Error {
  override name = 'ExecutorLostError';
  constructor() {
    super('o executor Python saiu antes da chamada');
  }
}

/** Processos deixados pelo código da questão sobreviveram à limpeza depois do restart(). */
class LeftoverProcessesError extends Error {
  override name = 'LeftoverProcessesError';
  constructor(remaining: number) {
    super(`${remaining} processo(s) deixado(s) pelo código da questão continuaram vivos depois do restart()`);
  }
}

// Espera pela saída do zygote de um executor descartado antes da limpeza final: o prazo do ensureExit
// e uma folga para o SIGKILL.
const EXIT_WAIT_MS = 2 * EXIT_GRACE_MS;

/**
 * O CodeCaller entregue às fases num empréstimo: cada chamada passa pelo teto de saída, e a primeira
 * confere de novo se o executor está pronto (ele pode morrer enquanto a fase monta o template).
 * restart() e done() ficam com o withCodeCaller.
 */
class Loan implements CodeCaller {
  readonly uuid: string;
  /** O executor estava morto na primeira chamada: a fase é refeita noutro executor. */
  lost = false;
  readonly #executor: Executor;
  #calls = 0;

  constructor(executor: Executor) {
    this.#executor = executor;
    this.uuid = executor.codeCaller.uuid;
  }

  prepareForCourse(opts: { coursePath: string; forbiddenModules: string[] }): Promise<void> {
    return this.#executor.codeCaller.prepareForCourse(opts);
  }

  call(type: CallType, directory: string | null, file: string | null, fcn: string, args: unknown[]): Promise<CodeCallerResult> {
    const { codeCaller, guard } = this.#executor;
    this.#calls += 1;
    if (this.#calls === 1 && !isReady(codeCaller)) this.lost = true;
    // Perdido, o empréstimo não chama mais o PL: o resultado da fase é descartado e ela roda de novo.
    if (this.lost) return Promise.reject(new ExecutorLostError());
    return guard.run(() => codeCaller.call(type, directory, file, fcn, args));
  }

  restart(): Promise<boolean> {
    return Promise.reject(new Error('restart() é feito pelo withCodeCaller'));
  }

  done(): void {
    throw new Error('done() é feito pelo pool de executores');
  }
}

type LoanOutcome<R> = { lost: false; value: R } | { lost: true };

export interface ExecutorOptions {
  size: number;
  questionTimeoutMs: number;
  log: LogFn;
}

export class Executors {
  readonly size: number;
  readonly #pool: WorkerPool<Executor>;
  readonly #log: LogFn;
  /** As limpezas dos executores descartados ainda em curso (close() espera por elas). */
  readonly #sweeps = new Set<Promise<void>>();

  constructor(opts: ExecutorOptions) {
    const errorLogger = codeCallerErrorLogger(opts.log);
    this.size = opts.size;
    this.#log = opts.log;
    this.#pool = new WorkerPool<Executor>({
      size: opts.size,
      create: async () => {
        const codeCaller = await CodeCallerNative.create({
          dropPrivileges: false,
          questionTimeoutMilliseconds: opts.questionTimeoutMs,
          pingTimeoutMilliseconds: PING_TIMEOUT_MS,
          errorLogger,
        });
        const child = childProcessOf(codeCaller);
        // [F] O CodeCallerNative não ouve o 'error' do stdin do zygote, e um 'error' sem ouvinte derruba
        // o processo do Node: a escrita de uma chamada no stdin de um zygote que acabou de morrer (EPIPE)
        // levaria o motor junto. O erro só é registrado; a saída do zygote, que vem em seguida, encerra
        // a chamada pelo próprio PL (_handleChildExit).
        child?.stdin?.on('error', () => opts.log({ level: 'warn', event: 'executor_stdin_error' }));
        const guard = new OutputGuard(child, (channel) => {
          opts.log({ level: 'warn', event: 'executor_output_limit', channel });
          if (child !== null) killExecutor(child);
        });
        // Lidos já, com o zygote vivo: depois que ele sai, o CodeCallerNative esquece o processo.
        const channels = child?.pid !== undefined ? channelsOf(child.pid) : null;
        return { codeCaller, guard, channels };
      },
      destroy: ({ codeCaller, channels }) => {
        const child = childProcessOf(codeCaller);
        const state = stateOf(codeCaller);
        try {
          codeCaller.done();
        } catch {
          // O estado do executor já não importa: ele sai do pool de qualquer jeito.
        }
        // O done() só manda sinal no estado WAITING; ensureExit cobre os demais e o zygote que não sai.
        if (child !== null) ensureExit(child, () => opts.log({ level: 'warn', event: 'executor_forced_stop', state }));
        if (channels !== null) this.#sweepAfterExit(channels, child);
      },
      validate: ({ codeCaller }) => {
        if (isAlive(codeCaller)) return true;
        opts.log({ level: 'warn', event: 'executor_lost', state: stateOf(codeCaller) });
        return false;
      },
    });
  }

  /** withCodeCaller do PL: um executor preparado para o curso, com restart() ao fim. */
  async withCodeCaller<R>(coursePath: string, fn: (codeCaller: CodeCaller) => Promise<R>): Promise<R> {
    for (let attempt = 1; ; attempt += 1) {
      const outcome = await this.#pool.use((executor) => this.#lend(executor, coursePath, fn));
      if (!outcome.lost) return outcome.value;
      this.#log({ level: 'warn', event: 'executor_lost', state: 'before_call' });
      // Executores que morrem um atrás do outro são falha do motor (500), não da questão.
      if (attempt >= LOST_EXECUTOR_ATTEMPTS) throw new ExecutorLostError();
    }
  }

  async #lend<R>(executor: Executor, coursePath: string, fn: (codeCaller: CodeCaller) => Promise<R>): Promise<LoanOutcome<R>> {
    const { codeCaller, guard } = executor;
    try {
      await codeCaller.prepareForCourse({ coursePath, forbiddenModules: [] });
    } catch (err) {
      this.#pool.discard(executor);
      throw err;
    }
    // Morto ocioso (zygote ou worker): trocado antes de qualquer código da questão.
    if (!(await waitReady(codeCaller))) {
      this.#pool.discard(executor);
      return { lost: true };
    }
    const loan = new Loan(executor);
    let fnResult: R | undefined;
    let fnError: unknown;
    let failed = false;
    try {
      fnResult = await fn(loan);
    } catch (err) {
      failed = true;
      fnError = err;
    }
    if (loan.lost) {
      this.#pool.discard(executor);
      return { lost: true };
    }
    if (guard.exceeded !== null) {
      // O motor já encerrou o executor (teto de saída): não há o que reiniciar.
      this.#pool.discard(executor);
      if (failed) throw fnError;
      return { lost: false, value: fnResult as R };
    }
    // Como o withCodeCaller do PL: restart() false pede recriação sem erro; restart() que lança também
    // descarta o executor, e o erro vale para a operação quando fn não falhou (fnErr ?? restartErr).
    let needsFullRestart = false;
    let restartFailed = false;
    let restartError: unknown;
    try {
      needsFullRestart = !(await codeCaller.restart());
    } catch (err) {
      restartFailed = true;
      restartError = err;
      needsFullRestart = true;
      this.#log({ level: 'error', event: 'executor_restart_failed' });
    }
    // Depois do restart(), o worker da fase já saiu (o zygote só confirma depois do waitpid) e o que ele
    // deixou rodando foi reparentado: é a hora em que o zygote do PL faz a limpeza (drop_privileges).
    // Um processo que sobra recusa o restart lá; aqui, o executor é descartado e a operação falha do
    // mesmo jeito. Um executor descartado é limpo no destroy.
    if (!needsFullRestart && executor.channels !== null) {
      const sweep = await killLeftovers(executor.channels, { spareZygote: true });
      this.#logSweep(sweep);
      if (sweep.remaining > 0) {
        restartFailed = true;
        restartError = new LeftoverProcessesError(sweep.remaining);
        needsFullRestart = true;
      }
    }
    if (needsFullRestart) this.#pool.discard(executor);
    if (failed) throw fnError;
    if (restartFailed) throw restartError;
    return { lost: false, value: fnResult as R };
  }

  /**
   * A limpeza de um executor descartado: espera o zygote sair (ensureExit garante a saída) e mata o que
   * ainda segura os canais dele. Os canais do Node já estão fechados então, e nada mais chega a uma
   * chamada; a limpeza encerra os processos, como o pkill do zygote do PL.
   */
  #sweepAfterExit(channels: ExecutorChannels, child: ChildProcess | null): void {
    const exited =
      child === null || hasExited(child)
        ? Promise.resolve()
        : new Promise<void>((resolve) => {
            const timer = setTimeout(resolve, EXIT_WAIT_MS);
            child.once('exit', () => {
              clearTimeout(timer);
              resolve();
            });
          });
    const sweep = exited.then(() => killLeftovers(channels, { spareZygote: false, workersUncounted: true })).then((result) => this.#logSweep(result));
    this.#sweeps.add(sweep);
    void sweep.finally(() => this.#sweeps.delete(sweep));
  }

  /** Só números no registro: quantos processos a limpeza matou e quantos sobraram. */
  #logSweep(sweep: LeftoverSweep): void {
    if (sweep.killed > 0) this.#log({ level: 'warn', event: 'executor_leftover_processes', killed: sweep.killed });
    if (sweep.remaining > 0) this.#log({ level: 'error', event: 'executor_cleanup_failed', remaining: sweep.remaining });
  }

  async close(): Promise<void> {
    await this.#pool.close();
    await Promise.all(this.#sweeps);
  }
}
