// Processos que o código da questão deixa rodando (uma questão ruim não
// contamina os pedidos seguintes). [F] No PL a193f72, o zygote.py só limpa esses processos com
// drop_privileges: quando o worker sai, mata tudo o que roda com o usuário executor (pkill -u executor)
// e, se ainda sobrar algum, recusa a confirmação do restart, que falha. É o modo do executor em
// container (executor.js, dropPrivileges: true); o modo native (dist/lib/code-caller/index.js) não
// limpa nada, e o motor roda como ele (dropPrivileges: false), com o Node e o Python no mesmo
// usuário. Sem a limpeza, um processo em segundo plano (subprocess.Popen sem wait, os.system com &)
// sobrevive ao restart() com a stdout, a stderr e às vezes o canal de resultado (fd 3) do zygote, e o
// que ele escreve cai na chamada de outro pedido: um aviso console_output espúrio, a saída de outra
// questão (com a resposta crua de um aluno) num technical_detail, ou um restart que falha com 500.
//
// Sem um usuário próprio, o motor reconhece os processos do executor pelos canais. O Node cria para o
// zygote um socket por canal (stdin, stdout, stderr, fd 3 e fd 4; o libuv usa socketpair), e a ponta
// do zygote só passa adiante por herança: quem a segura descende do zygote, mesmo depois de
// reparentado ao init. Ficam de fora o zygote e o worker atual (o zygote só cria workers); os demais
// são mortos com SIGKILL, em rodadas, até ninguém mais segurar os canais. Como no PL, se sobrar algum
// depois do prazo, a limpeza falha.
// Limite conhecido: um processo que fecha ou troca todos os canais herdados (uma nova sessão com a
// stdin, a stdout e a stderr em /dev/null) não é encontrado; ele também não escreve em canal nenhum do
// executor, e por isso não contamina os pedidos seguintes.
import fs from 'node:fs';

/** Os canais de um executor, lidos logo depois de o zygote nascer. */
export interface ExecutorChannels {
  zygote: number;
  /** O início do zygote (campo 22 de /proc/<pid>/stat, em tiques desde o boot). */
  startTime: number;
  /** Os alvos de /proc/<zygote>/fd/0 a 4 ("socket:[inode]"). */
  targets: ReadonlySet<string>;
  /** A linha de comando do zygote, que é também a dos workers (um fork sem exec). */
  cmdline: string;
}

export interface LeftoverSweep {
  /** Processos mortos pela limpeza. */
  killed: number;
  /** Processos que ainda seguravam os canais no fim do prazo (0: a limpeza concluiu). */
  remaining: number;
}

// Prazo da limpeza e intervalo entre as rodadas: um processo morto com SIGKILL só larga os canais quando
// termina de sair, e um que criou filhos antes de morrer deixa-os para a rodada seguinte.
const SWEEP_DEADLINE_MS = 1_000;
const SWEEP_POLL_MS = 5;

interface ProcStat {
  state: string;
  ppid: number;
  startTime: number;
}

function readStat(pid: string): ProcStat | null {
  let stat: string;
  try {
    stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
  } catch {
    return null;
  }
  // "pid (comm) estado ppid …": o comm pode ter espaços e parênteses; os campos seguem o último ")".
  const fields = stat.slice(stat.lastIndexOf(')') + 2).split(' ');
  return { state: fields[0] ?? '', ppid: Number(fields[1]), startTime: Number(fields[19]) };
}

function readCmdline(pid: string): string | null {
  try {
    return fs.readFileSync(`/proc/${pid}/cmdline`, 'latin1');
  } catch {
    return null;
  }
}

/**
 * Os canais do zygote, ou null sem /proc (a limpeza fica desligada). Só entram sockets e pipes: um alvo
 * como /dev/null é de todos os processos.
 */
export function channelsOf(zygote: number): ExecutorChannels | null {
  const stat = readStat(String(zygote));
  if (stat === null || !Number.isFinite(stat.startTime)) return null;
  const cmdline = readCmdline(String(zygote));
  if (cmdline === null) return null;
  const targets = new Set<string>();
  for (let fd = 0; fd <= 4; fd += 1) {
    try {
      const target = fs.readlinkSync(`/proc/${zygote}/fd/${fd}`);
      if (target.startsWith('socket:[') || target.startsWith('pipe:[')) targets.add(target);
    } catch {
      // o zygote saiu, ou o canal não existe
    }
  }
  return targets.size > 0 ? { zygote, startTime: stat.startTime, targets, cmdline } : null;
}

function holdsChannel(pid: string, targets: ReadonlySet<string>): boolean {
  let fds: string[];
  try {
    fds = fs.readdirSync(`/proc/${pid}/fd`);
  } catch {
    return false;
  }
  for (const fd of fds) {
    try {
      if (targets.has(fs.readlinkSync(`/proc/${pid}/fd/${fd}`))) return true;
    } catch {
      // o descritor foi fechado durante a varredura
    }
  }
  return false;
}

/** Os processos que seguram algum canal do executor; com spareZygote, fora o zygote e seus filhos. */
function holders(channels: ExecutorChannels, spareZygote: boolean): number[] {
  const out: number[] = [];
  let entries: string[];
  try {
    entries = fs.readdirSync('/proc');
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (!/^[0-9]+$/.test(entry)) continue;
    const pid = Number(entry);
    if (pid === process.pid || (spareZygote && pid === channels.zygote)) continue;
    const stat = readStat(entry);
    // Um descendente do zygote nasceu depois dele, e um processo que já saiu (zumbi) não segura nada.
    if (stat === null || stat.startTime < channels.startTime || stat.state === 'Z' || stat.state === 'X') continue;
    if (spareZygote && stat.ppid === channels.zygote) continue;
    if (holdsChannel(entry, channels.targets)) out.push(pid);
  }
  return out;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function nextTurn(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

export interface SweepOptions {
  /** O executor continua em uso (depois do restart()): o zygote e o worker novo ficam. */
  spareZygote: boolean;
  /**
   * Descarte do executor: um worker que ainda segura os canais morre na limpeza, mas não conta como
   * processo deixado pelo código da questão. O zygote mata o worker ao sair (terminate_worker), mas o
   * SIGTERM que chega durante o fork do worker seguinte deixa o worker novo para trás: o zygote só o
   * conhece depois que o fork volta (worker_pid). Um worker é reconhecido pela linha de comando do
   * zygote; um fork sem exec do código da questão também, e morre do mesmo jeito.
   */
  workersUncounted?: boolean;
}

/**
 * Mata os processos que seguram os canais do executor. Com spareZygote, ficam o zygote e seus filhos;
 * sem ele (o zygote já saiu), não fica ninguém.
 */
export async function killLeftovers(channels: ExecutorChannels, opts: SweepOptions): Promise<LeftoverSweep> {
  const deadline = performance.now() + SWEEP_DEADLINE_MS;
  // Cada processo, pela primeira vez em que aparece: conta ou não (um morto ainda saindo reaparece na
  // rodada seguinte, já sem a linha de comando).
  const counted = new Map<number, boolean>();
  for (;;) {
    const found = holders(channels, opts.spareZygote);
    if (found.length === 0) break;
    if (performance.now() >= deadline) return { killed: countOf(counted), remaining: found.length };
    for (const pid of found) {
      if (!counted.has(pid)) counted.set(pid, opts.workersUncounted !== true || readCmdline(String(pid)) !== channels.cmdline);
      try {
        process.kill(pid, 'SIGKILL');
      } catch {
        // já saiu
      }
    }
    await delay(SWEEP_POLL_MS);
  }
  // O que um processo escreveu antes de sair pode estar no buffer do socket. Duas voltas do laço de
  // eventos garantem uma fase de poll (a mesma técnica do _restartTimeout do PL): o Node lê esses dados
  // agora, fora de qualquer chamada, quando o CodeCallerNative os descarta com um registro de erro, e
  // não na chamada seguinte.
  await nextTurn();
  await nextTurn();
  return { killed: countOf(counted), remaining: 0 };
}

function countOf(counted: ReadonlyMap<number, boolean>): number {
  let n = 0;
  for (const counts of counted.values()) if (counts) n += 1;
  return n;
}
