// Hash do conteúdo da questão: sha256 sobre, para cada arquivo em ordem
// lexicográfica do caminho relativo (separador "/"), caminho + "\0" + tamanho decimal + "\0" + bytes
// + "\0". Diretórios vazios não contam.
// Os caminhos são tratados como bytes (Buffer), sem decodificar: a ordem é a dos bytes UTF-8 (igual à
// dos pontos de código) e um nome fora de UTF-8 não quebra a leitura. Só entram arquivos regulares:
// links simbólicos e arquivos especiais ficam de fora (nada fora da questão é lido nem seguido).
// Cada arquivo é lido em blocos, e só um bloco fica na memória: um arquivo grande da questão (um
// clientFilesQuestion de gigabytes) não esgota a memória do motor, derrubando os outros pedidos, nem
// passa do limite do fs.readFile, que daria 500. O tamanho entra no hash antes dos
// bytes, e por isso vem do fstat do arquivo aberto; os bytes lidos são conferidos contra ele. Um
// arquivo que muda durante a leitura (a área de repositórios é somente leitura) é falha interna, e o
// pedido pode ser repetido.
import { createHash, type Hash } from 'node:crypto';
import { constants } from 'node:fs';
import fs from 'node:fs/promises';

const SEPARATOR = Buffer.from('/', 'utf8');
const NUL = Buffer.from([0]);

/** O bloco de leitura, alocado uma vez por cálculo. */
const CHUNK_BYTES = 1024 * 1024;

/**
 * Sem seguir link (O_NOFOLLOW) e sem esperar num FIFO (O_NONBLOCK), caso a entrada listada como
 * arquivo regular tenha mudado antes da abertura.
 */
const OPEN_FLAGS = constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK;

export async function contentHash(dir: string): Promise<string> {
  const root = Buffer.from(dir, 'utf8');
  const files: Buffer[] = [];
  await collectFiles(root, null, files);
  files.sort(Buffer.compare);

  const hash = createHash('sha256');
  const chunk = Buffer.allocUnsafe(CHUNK_BYTES);
  for (const rel of files) await hashFile(hash, Buffer.concat([root, SEPARATOR, rel]), rel, chunk);
  return `sha256:${hash.digest('hex')}`;
}

/** Acrescenta ao hash a parte de um arquivo: caminho + "\0" + tamanho + "\0" + bytes + "\0". */
async function hashFile(hash: Hash, file: Buffer, rel: Buffer, chunk: Buffer): Promise<void> {
  const handle = await fs.open(file, OPEN_FLAGS);
  try {
    const stats = await handle.stat();
    if (!stats.isFile()) throw new Error('hash do conteúdo: um arquivo da questão deixou de ser arquivo regular durante a leitura');
    hash.update(rel);
    hash.update(NUL);
    hash.update(String(stats.size), 'utf8');
    hash.update(NUL);
    let total = 0;
    for (;;) {
      const { bytesRead } = await handle.read(chunk, 0, chunk.length, null);
      if (bytesRead === 0) break;
      total += bytesRead;
      // Mais bytes que o fstat informou: o arquivo cresceu, e o tamanho já entrou no hash.
      if (total > stats.size) break;
      hash.update(chunk.subarray(0, bytesRead));
    }
    if (total !== stats.size) throw new Error('hash do conteúdo: um arquivo da questão mudou de tamanho durante a leitura');
    hash.update(NUL);
  } finally {
    await handle.close();
  }
}

/** Acrescenta a `out` os caminhos relativos (com "/") dos arquivos regulares sob `rel`. */
async function collectFiles(root: Buffer, rel: Buffer | null, out: Buffer[]): Promise<void> {
  const current = rel === null ? root : Buffer.concat([root, SEPARATOR, rel]);
  const entries = await fs.readdir(current, { withFileTypes: true, encoding: 'buffer' });
  for (const entry of entries) {
    const child = rel === null ? entry.name : Buffer.concat([rel, SEPARATOR, entry.name]);
    if (entry.isDirectory()) await collectFiles(root, child, out);
    else if (entry.isFile()) out.push(child);
  }
}
