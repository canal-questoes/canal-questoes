// Leitura dos arquivos de texto do repositório que o processo Node do motor abre: o info.json, o
// question.html e o server.py da questão e o info.json dos elementos e das extensões do curso. O PL lê
// esses arquivos com fs.readFile, seguindo os links simbólicos; o motor também segue os links, mas só
// lê arquivo regular, e só até o limite (escolha do motor):
// - o git guarda links simbólicos, e um link para /dev/zero, para um FIFO ou para um dispositivo faria
//   o motor ler sem fim (e abortar por falta de memória) ou esperar para sempre, derrubando junto os
//   outros pedidos;
// - um arquivo regular grande esgota a memória da varredura: a árvore do lxml (html-fragments.ts) custa
//   dezenas de vezes o tamanho do texto, e um question.html de 9 MiB já passa do heap do Node.
// O tipo é conferido antes de abrir (stat, para não abrir dispositivo nenhum) e de novo depois (fstat),
// com a abertura sem bloqueio (O_NONBLOCK), caso o arquivo mude entre as duas conferências. A leitura
// para no limite, sem confiar no tamanho informado (um arquivo de /proc informa 0).
import { constants, type Stats } from 'node:fs';
import fs from 'node:fs/promises';

/**
 * O teto de cada arquivo: 2 MiB, o mesmo do corpo de um pedido HTTP. O maior question.html do
 * exampleCourse e do testCourse do PL a193f72 tem 20 KB, e o maior server.py, 12 KB.
 */
export const TEXT_FILE_MAX_BYTES = 2 * 1024 * 1024;

export type TextFileRead =
  | { status: 'ok'; text: string }
  /** Existe, mas não é arquivo regular depois de seguir os links (diretório, dispositivo, FIFO, socket). */
  | { status: 'not_regular'; directory: boolean }
  /** Arquivo regular acima do limite. */
  | { status: 'too_large' };

/** Primeira leitura: o arquivo inteiro de uma vez nos casos comuns, sem passar do limite + 1. */
const INITIAL_CHUNK = 64 * 1024;

function notRegular(stats: Stats): TextFileRead {
  return { status: 'not_regular', directory: stats.isDirectory() };
}

/**
 * O texto do arquivo em UTF-8 (como fs.readFile(…, 'utf8')), se ele for regular e couber no limite. Os
 * erros do sistema de arquivos (ENOENT, ENOTDIR, ELOOP etc.) sobem como no fs.readFile, e quem chama
 * decide o que cada código significa.
 */
export async function readTextFile(file: string, maxBytes = TEXT_FILE_MAX_BYTES): Promise<TextFileRead> {
  const before = await fs.stat(file);
  if (!before.isFile()) return notRegular(before);
  if (before.size > maxBytes) return { status: 'too_large' };
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NONBLOCK);
  try {
    const stats = await handle.stat();
    if (!stats.isFile()) return notRegular(stats);
    if (stats.size > maxBytes) return { status: 'too_large' };
    let buffer = Buffer.allocUnsafe(Math.min(maxBytes + 1, Math.max(stats.size + 1, INITIAL_CHUNK)));
    let total = 0;
    for (;;) {
      if (total === buffer.length) {
        const bigger = Buffer.allocUnsafe(Math.min(maxBytes + 1, buffer.length * 2));
        buffer.copy(bigger, 0, 0, total);
        buffer = bigger;
      }
      const { bytesRead } = await handle.read(buffer, total, buffer.length - total, null);
      if (bytesRead === 0) break;
      total += bytesRead;
      // O arquivo cresceu depois do fstat (ou informa um tamanho menor que o real).
      if (total > maxBytes) return { status: 'too_large' };
    }
    return { status: 'ok', text: buffer.toString('utf8', 0, total) };
  } finally {
    await handle.close();
  }
}
