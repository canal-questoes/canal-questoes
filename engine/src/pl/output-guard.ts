// Teto da saída do Python por chamada. [F] O CodeCallerNative do PL (a193f72,
// dist/lib/code-caller/code-caller-native.js) acumula sem limite, durante a chamada, a stdout e a stderr
// do processo (outputStdout, outputStderr e outputBoth), o canal de resultado (fd 3, outputData) e o de
// confirmação do restart (fd 4, outputRestart), e junta tudo numa cadeia só no fim da chamada ou no erro
// (_errorData). Uma questão que escreve sem parar esgota a memória do Node, ou estoura o tamanho máximo
// de cadeia do V8 dentro de um callback de timer, e o processo inteiro do motor cai com os pedidos em
// curso.
//
// Sem alterar o código do PL, o motor conta o que chega a esses streams durante cada chamada, com
// ouvintes próprios ao lado dos do PL. Acima do teto, pausa os streams (o PL não recebe mais nada),
// chama onExceeded (quem cria o executor mata o worker e o zygote) e a chamada falha com
// OutputLimitError. A fase registra o problema fatal como qualquer erro de chamada (questão quebrada,
// question_code_error), e o executor é descartado. É uma divergência do PL só acima do teto: lá, a
// questão que imprime 2 MiB e retorna sai normal, com o problema de console; aqui, sai quebrada.
//
// Os tetos (em caracteres, como os streams entregam o texto com setEncoding('utf8')):
// - console, 1 MiB por chamada (stdout e stderr somados): o PL guarda no máximo 10 000 caracteres de
//   cada texto do problema (lib/issues.js, recursivelyTruncateStrings) e o motor 8000 no
//   technical_detail, então o teto fica duas ordens de grandeza acima do que se aproveita; o PL
//   guarda a saída duas vezes (outputStdout/outputStderr e outputBoth) e ainda a junta, o que dá alguns
//   MiB por executor no pior caso, vezes N executores;
// - resultado, 16 MiB por chamada: os dados da fase, o HTML do render ou o arquivo do file() em base64
//   (uma figura de 12 MiB); bem acima de uma questão real (o snapshot de uma variante volta ao motor
//   num corpo de no máximo 2 MiB) e bem abaixo do limite de cadeia do V8 (~512 Mi caracteres) e da
//   memória de um container de 1 GiB. O fd 4 conta no mesmo teto: o worker fecha o fd 4 ao nascer
//   (zygote.py), e nenhuma confirmação de restart chega durante uma chamada.
// Perto do teto, o resultado depende de quanto da saída o Node leu antes do fim da chamada (a última
// leitura de cada pipe tem até 64 KiB); longe dele, não.
import type { ChildProcess } from 'node:child_process';
import type { Readable } from 'node:stream';

export type OutputChannel = 'console' | 'result';

export const OUTPUT_LIMITS: Readonly<Record<OutputChannel, number>> = {
  console: 1024 * 1024,
  result: 16 * 1024 * 1024,
};

/** A chamada passou do teto de saída; o executor foi encerrado pelo motor. */
export class OutputLimitError extends Error {
  override name = 'OutputLimitError';
  readonly channel: OutputChannel;
  /** Só a saída do console até o corte, como o err.data.outputBoth do PL (sem o lastCallData). */
  readonly data: { outputBoth: string };

  constructor(channel: OutputChannel, outputBoth: string) {
    const limit = OUTPUT_LIMITS[channel];
    super(
      channel === 'console'
        ? `a saída no console passou do limite de ${limit} caracteres por chamada; o motor encerrou o executor`
        : `o resultado da chamada passou do limite de ${limit} caracteres; o motor encerrou o executor`,
    );
    this.channel = channel;
    this.data = { outputBoth };
  }
}

/** err.data.outputBoth de um erro do CodeCallerNative, sem tocar no resto de err.data. */
function outputBothOf(err: unknown): string {
  const data = (err as { data?: unknown } | null)?.data;
  if (data === null || typeof data !== 'object') return '';
  const output = (data as { outputBoth?: unknown }).outputBoth;
  return typeof output === 'string' ? output : '';
}

/** Conta a saída do zygote em cada chamada e corta quando ela passa do teto. */
export class OutputGuard {
  readonly #streams: Readable[] = [];
  readonly #onExceeded: (channel: OutputChannel) => void;
  readonly #counts: Record<OutputChannel, number> = { console: 0, result: 0 };
  #active = false;
  #exceeded: OutputChannel | null = null;

  /** Os streams do zygote (child.stdio do CodeCallerNative): 1 e 2 são o console; 3 e 4, o resultado. */
  constructor(child: ChildProcess | null, onExceeded: (channel: OutputChannel) => void) {
    this.#onExceeded = onExceeded;
    const stdio = child?.stdio ?? [];
    for (const [fd, channel] of [
      [1, 'console'],
      [2, 'console'],
      [3, 'result'],
      [4, 'result'],
    ] as const) {
      const stream = stdio[fd] as Readable | null | undefined;
      if (stream === null || stream === undefined) continue;
      this.#streams.push(stream);
      stream.on('data', (chunk: string | Buffer) => this.#count(channel, chunk.length));
    }
  }

  /** O canal que passou do teto (o executor já foi encerrado), ou null. */
  get exceeded(): OutputChannel | null {
    return this.#exceeded;
  }

  /**
   * Uma chamada ao executor, contada do começo ao fim. Se a saída passar do teto, a chamada falha com
   * OutputLimitError, também quando o PL chegou a devolver o resultado.
   */
  async run<T extends { output: string }>(call: () => Promise<T>): Promise<T> {
    if (this.#exceeded !== null) throw new OutputLimitError(this.#exceeded, '');
    this.#counts.console = 0;
    this.#counts.result = 0;
    this.#active = true;
    let res: T;
    try {
      res = await call();
    } catch (err) {
      if (this.#exceeded !== null && !(err instanceof OutputLimitError)) throw new OutputLimitError(this.#exceeded, outputBothOf(err));
      throw err;
    } finally {
      this.#active = false;
    }
    if (this.#exceeded !== null) throw new OutputLimitError(this.#exceeded, res.output);
    return res;
  }

  #count(channel: OutputChannel, length: number): void {
    if (!this.#active || this.#exceeded !== null) return;
    this.#counts[channel] += length;
    if (this.#counts[channel] <= OUTPUT_LIMITS[channel]) return;
    this.#exceeded = channel;
    // Pausados, os streams não entregam mais nada ao PL; o _handleChildExit do PL os destrói na saída.
    for (const stream of this.#streams) stream.pause();
    try {
      this.#onExceeded(channel);
    } catch {
      // Estamos num ouvinte de stream: uma exceção aqui derrubaria o processo. O tempo esgotado do PL
      // ainda encerra a chamada, com os streams pausados.
    }
  }
}
