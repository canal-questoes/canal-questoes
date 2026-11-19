// WorkerPool: pool de executores com fábrica falsa.
// Cobre os pedidos simultâneos (número configurável de executores; os excedentes esperam
// em fila, sem recusa), a recuperação (depois de um erro que exige descarte, o executor é recriado e o
// motor segue atendendo) e a disciplina dos executores. O momento de criação dos executores não
// é fixado, e nenhum teste o exige.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';

import { WorkerPool } from '../../src/pl/pool.ts';

interface FakeWorker {
  id: number;
}

function fakeFactory(): {
  created: FakeWorker[];
  destroyed: FakeWorker[];
  create: () => Promise<FakeWorker>;
  destroy: (w: FakeWorker) => Promise<void>;
} {
  const created: FakeWorker[] = [];
  const destroyed: FakeWorker[] = [];
  return {
    created,
    destroyed,
    create: async () => {
      const w = { id: created.length + 1 };
      created.push(w);
      return w;
    },
    destroy: async (w) => {
      destroyed.push(w);
    },
  };
}

const T = { timeout: 10_000 };

describe('WorkerPool', () => {
  test('com size 2 e 5 usos simultâneos, nunca há mais de 2 em uso e todos terminam', T, async () => {
    const f = fakeFactory();
    const pool = new WorkerPool<FakeWorker>({ size: 2, create: f.create, destroy: f.destroy });
    assert.equal(pool.size, 2);
    let inUse = 0;
    let maxInUse = 0;
    const busy = new Set<FakeWorker>();
    const results = await Promise.all(
      [0, 1, 2, 3, 4].map((i) =>
        pool.use(async (w) => {
          assert.ok(!busy.has(w), `executor ${w.id} entregue a dois usos ao mesmo tempo`);
          busy.add(w);
          inUse += 1;
          maxInUse = Math.max(maxInUse, inUse);
          await delay(25);
          inUse -= 1;
          busy.delete(w);
          return i * 10;
        }),
      ),
    );
    assert.deepEqual(results, [0, 10, 20, 30, 40], 'todos os 5 usos terminam, cada um com o próprio resultado');
    assert.ok(maxInUse <= 2, `mais de 2 executores em uso ao mesmo tempo: ${maxInUse}`);
    assert.equal(maxInUse, 2, 'com 5 pedidos simultâneos, os 2 executores deveriam ser usados em paralelo');
    assert.ok(f.created.length <= 2, `o pool criou ${f.created.length} executores com size 2`);
    await pool.close();
  });

  test('discardOn que devolve true destrói o executor e cria outro no uso seguinte', T, async () => {
    const f = fakeFactory();
    const pool = new WorkerPool<FakeWorker>({ size: 1, create: f.create, destroy: f.destroy });
    const boom = new Error('executor corrompido');
    let first: FakeWorker | undefined;
    let seen: unknown;
    await assert.rejects(
      pool.use(
        async (w) => {
          first = w;
          throw boom;
        },
        {
          discardOn: (err) => {
            seen = err;
            return true;
          },
        },
      ),
      (err) => err === boom,
    );
    assert.equal(seen, boom, 'discardOn recebe o erro lançado por fn');
    const second = await pool.use(async (w) => w);
    assert.ok(first, 'fn recebeu um executor');
    assert.notEqual(second, first, 'o uso seguinte deveria receber um executor novo');
    assert.ok(f.destroyed.includes(first), 'o executor descartado deveria ser destruído');
    assert.equal(f.created.length, 2);
    await pool.close();
  });

  test('discard(w) marca o executor para recriação', T, async () => {
    const f = fakeFactory();
    const pool = new WorkerPool<FakeWorker>({ size: 1, create: f.create, destroy: f.destroy });
    let first: FakeWorker | undefined;
    const result = await pool.use(async (w) => {
      first = w;
      pool.discard(w);
      return 'ok';
    });
    assert.equal(result, 'ok', 'discard não altera o resultado do uso');
    const second = await pool.use(async (w) => w);
    assert.ok(first, 'fn recebeu um executor');
    assert.notEqual(second, first, 'o uso seguinte deveria receber um executor novo');
    assert.ok(f.destroyed.includes(first), 'o executor marcado deveria ser destruído');
    assert.equal(f.created.length, 2);
    await pool.close();
  });

  test('um erro de fn se propaga e o executor volta ao pool', T, async () => {
    const f = fakeFactory();
    const pool = new WorkerPool<FakeWorker>({ size: 1, create: f.create, destroy: f.destroy });
    const err1 = new Error('falha da questão');
    let first: FakeWorker | undefined;
    await assert.rejects(
      pool.use(async (w) => {
        first = w;
        throw err1;
      }),
      (err) => err === err1,
    );
    const err2 = new Error('outra falha');
    await assert.rejects(
      pool.use(
        async () => {
          throw err2;
        },
        { discardOn: () => false },
      ),
      (err) => err === err2,
    );
    const again = await pool.use(async (w) => w);
    assert.ok(first, 'fn recebeu um executor');
    assert.equal(again, first, 'depois de um erro sem descarte, o mesmo executor volta ao pool');
    assert.equal(f.created.length, 1);
    assert.equal(f.destroyed.length, 0);
    await pool.close();
  });

  test('close() destrói todos os executores', T, async () => {
    const f = fakeFactory();
    const pool = new WorkerPool<FakeWorker>({ size: 2, create: f.create, destroy: f.destroy });
    await Promise.all([
      pool.use(async () => {
        await delay(20);
      }),
      pool.use(async () => {
        await delay(20);
      }),
    ]);
    assert.ok(f.created.length >= 1);
    await pool.close();
    assert.equal(f.destroyed.length, f.created.length, 'todo executor criado deveria ser destruído');
    for (const w of f.created) assert.ok(f.destroyed.includes(w), `executor ${w.id} não foi destruído`);
  });
});
