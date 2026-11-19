// Referências e utilitários comuns dos testes.
import assert from 'node:assert/strict';

export const EXAMPLE = {
  repo: 'https://github.com/PrairieLearn/PrairieLearn',
  commit: 'a193f72048530a4499252c454acca963cdf98922',
} as const;

export const TEST_COURSE = {
  repo: 'fixtures/cq-test-course',
  commit: '1111111111111111111111111111111111111111',
} as const;

export const OVERRIDE_COURSE = {
  repo: 'fixtures/cq-override-course',
  commit: '1111111111111111111111111111111111111111',
} as const;

export const PL_COMMIT = 'a193f72048530a4499252c454acca963cdf98922';

export function ref(base: { repo: string; commit: string }, qid: string): { repo: string; commit: string; qid: string } {
  return { repo: base.repo, commit: base.commit, qid };
}

/** Seeds determinísticas no formato do PL (base 36, valor em [0, 2^32)). */
export function seeds(n: number, offset = 0): string[] {
  const out: string[] = [];
  for (let i = 0; i < n; i++) {
    const value = Number((BigInt(i + offset) * 2654435761n + 12345n) % 4294967296n);
    out.push(value.toString(36));
  }
  return out;
}

// As duas últimas linhas cobrem as mensagens de error_response que também
// passam por assertPortuguese: 413, 500, rota e método. Ficam de fora os trechos que
// casam com uma mensagem em inglês e anulariam a checagem: 'erro' (de "error"), 'interna' (de
// "internal"), 'limite' (de "limited") e 'motor' (palavra inglesa).
const PT_WORDS = [
  'questão', 'questao', 'elemento', 'correção', 'correcao', 'suportad', 'não', 'código', 'campo',
  'resposta', 'número', 'numero', 'inteiro', 'texto', 'alternativa', 'marque', 'escolha',
  'formato', 'arquivo', 'tempo', 'encontrad', 'inválid', 'pedido', 'preparação', 'figura',
  'imagem', 'dados', 'metadados', 'nome', 'exemplo', 'ex.:', 'digite', 'selecione',
  'opç', 'esgotad', 'quebrad', 'curso', 'painel', 'externa', 'marcar', 'valor',
  'fração', 'notação', 'maiúscul', 'espaço', 'linha', 'gerad', 'ausente', 'permitid', 'versão',
  'falha', 'interno', 'método', 'tamanho', 'acima', 'corpo', 'excedid', 'desconhecid', 'inexistente',
  'encerrad', 'indisponív', 'serviço',
];

const TECHNICAL = ['traceback', 'exception', 'error:', 'valueerror', 'typeerror', 'runtimeerror', 'keyerror'];

/** Mensagem funcional em português, sem nomes de exceção técnica. */
export function assertPortuguese(msg: unknown): void {
  assert.equal(typeof msg, 'string', 'mensagem deve ser texto');
  const text = (msg as string).toLowerCase();
  assert.ok(text.trim().length > 0, 'mensagem vazia');
  assert.ok(PT_WORDS.some((w) => text.includes(w)), `mensagem não parece português: ${msg}`);
  for (const bad of TECHNICAL) {
    assert.ok(!text.includes(bad), `mensagem com detalhe técnico (${bad}): ${msg}`);
  }
}
