// Referência de questão: repoKey, parseSeed e resolveQuestionPaths.
// Cobre a referência de questão (commit completo; branch/tag/abreviação recusadas; área de repositórios só leitura),
// a área de repositórios (vetores de teste, resolução, contenção por realpath dos dois lados) e a seed.
// A área de teste é montada pelo próprio teste em /tmp.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';

import { parseSeed, repoKey, resolveQuestionPaths } from '../../src/core/reference.ts';
import { InvalidRequestError, NotFoundError } from '../../src/errors.ts';

/** sha256 hex minúsculo de utf8(texto), calculado no próprio teste (independente de src/). */
function sha256hex(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex');
}

describe('repoKey', () => {
  test('bate com os três vetores de teste do contrato', () => {
    assert.equal(
      repoKey('https://github.com/PrairieLearn/PrairieLearn'),
      '70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7',
    );
    assert.equal(
      repoKey('fixtures/cq-test-course'),
      '511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d',
    );
    assert.equal(
      repoKey('fixtures/cq-override-course'),
      'c92033d9d541cdab2d8b535eb6711fda5deb07263a94b0c1e4be09ca1f74b892',
    );
  });

  test('é o sha256 hex de utf8(repo), igual ao usado para montar a área de teste', () => {
    assert.equal(repoKey('r'), sha256hex('r'));
    assert.equal(repoKey('repositório/ç'), sha256hex('repositório/ç'));
  });
});

describe('parseSeed', () => {
  test('converte seeds válidas em base 36, como o parseInt do PL', () => {
    assert.equal(parseSeed('abc'), 13368);
    assert.equal(parseSeed('0'), 0);
    assert.equal(parseSeed('1z141z3'), 4294967295, '"1z141z3" é 2^32 - 1, o maior valor aceito');
  });

  test('recusa seeds fora de ^[0-9a-z]{1,7}$ ou com valor ≥ 2^32', () => {
    for (const seed of ['1z141z4', 'ABC', '', '12345678', 'a-b']) {
      assert.throws(
        () => parseSeed(seed),
        InvalidRequestError,
        `a seed ${JSON.stringify(seed)} deveria ser recusada com InvalidRequestError`,
      );
    }
  });
});

describe('resolveQuestionPaths', () => {
  const COMMIT = '0123456789abcdef0123456789abcdef01234567';
  const COMMIT64 = 'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210';
  const COMMIT_UPPER = '0123456789ABCDEF0123456789ABCDEF01234567';
  const COMMIT_SHORT = 'a193f72';
  const COMMIT_MISSING = '2222222222222222222222222222222222222222';

  let base = '';
  let reposRoot = '';
  let courseRoot = '';
  let externalDir = '';
  let linkedCourseTarget = '';

  async function writeQuestion(dir: string, withInfo = true): Promise<void> {
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, 'question.html'), '<pl-integer-input answers-name="v"></pl-integer-input>\n');
    if (withInfo) {
      await fs.writeFile(
        path.join(dir, 'info.json'),
        JSON.stringify({ uuid: '00000000-0000-4000-8000-000000000001', title: 'Q', topic: 'T', type: 'v3' }),
      );
    }
  }

  before(async () => {
    // Área temporária própria: /tmp/cq-ref-XXXX/{repos, externo, curso-ligado}.
    base = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), 'cq-ref-')));
    reposRoot = path.join(base, 'repos');

    // Repo "r", commit de 40 hex: questões q, grupo/sub, sem-info e o link fora.
    courseRoot = path.join(reposRoot, sha256hex('r'), COMMIT);
    await writeQuestion(path.join(courseRoot, 'questions', 'q'));
    await writeQuestion(path.join(courseRoot, 'questions', 'grupo', 'sub'));
    await writeQuestion(path.join(courseRoot, 'questions', 'sem-info'), false);

    // Diretórios que tornariam os qids inválidos "encontráveis" se o caminho fosse só normalizado:
    // assim, só a validação do qid explica o 400.
    await writeQuestion(path.join(courseRoot, 'questions', 'b')); // a/../b
    await fs.mkdir(path.join(courseRoot, 'questions', 'a'), { recursive: true });
    await writeQuestion(path.join(courseRoot, 'questions', 'x')); // ./x
    await writeQuestion(path.join(courseRoot, 'questions', 'abs')); // /abs
    await writeQuestion(path.join(courseRoot, 'questions', 'a', 'b')); // a//b

    // Diretório FORA da área, com info.json, e o link questions/fora → ele.
    externalDir = path.join(base, 'externo');
    await writeQuestion(externalDir);
    await fs.symlink(externalDir, path.join(courseRoot, 'questions', 'fora'));

    // Commit de 64 hex, outro diretório montado.
    await writeQuestion(path.join(reposRoot, sha256hex('r'), COMMIT64, 'questions', 'q'));

    // Commits inválidos com diretório presente: só a validação do commit explica o 400.
    await writeQuestion(path.join(reposRoot, sha256hex('r'), COMMIT_UPPER, 'questions', 'q'));
    await writeQuestion(path.join(reposRoot, sha256hex('r'), COMMIT_SHORT, 'questions', 'q'));
    // Branch e tag também existem como diretórios, para que só a validação explique a recusa.
    await writeQuestion(path.join(reposRoot, sha256hex('r'), 'main', 'questions', 'q'));
    await writeQuestion(path.join(reposRoot, sha256hex('r'), 'v1.0', 'questions', 'q'));

    // Repo "s": o diretório do commit é um link simbólico para um curso na mesma área temporária
    // (como a imagem de teste faz com o exampleCourse).
    linkedCourseTarget = path.join(base, 'curso-ligado');
    await writeQuestion(path.join(linkedCourseTarget, 'questions', 'q'));
    await fs.mkdir(path.join(reposRoot, sha256hex('s')), { recursive: true });
    await fs.symlink(linkedCourseTarget, path.join(reposRoot, sha256hex('s'), COMMIT));

    // Conferência da própria área (erro aqui é da fixture, não do motor).
    assert.ok((await fs.lstat(path.join(courseRoot, 'questions', 'fora'))).isSymbolicLink(), 'fixture: fora deve ser link');
    assert.ok((await fs.lstat(path.join(reposRoot, sha256hex('s'), COMMIT))).isSymbolicLink(), 'fixture: commit de s deve ser link');
    await fs.access(path.join(externalDir, 'info.json'));
  });

  after(async () => {
    if (base) await fs.rm(base, { recursive: true, force: true });
  });

  test('resolve q para {courseRoot, questionDir} com os caminhos esperados', async () => {
    const got = await resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT, qid: 'q' });
    assert.equal(path.resolve(got.courseRoot), courseRoot);
    assert.equal(path.resolve(got.questionDir), path.join(courseRoot, 'questions', 'q'));
  });

  test('resolve qid com mais de um segmento', async () => {
    const got = await resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT, qid: 'grupo/sub' });
    assert.equal(path.resolve(got.courseRoot), courseRoot);
    assert.equal(path.resolve(got.questionDir), path.join(courseRoot, 'questions', 'grupo', 'sub'));
  });

  test('aceita um commit de 64 hex', async () => {
    const got = await resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT64, qid: 'q' });
    const expectedRoot = path.join(reposRoot, sha256hex('r'), COMMIT64);
    assert.equal(path.resolve(got.courseRoot), expectedRoot);
    assert.equal(path.resolve(got.questionDir), path.join(expectedRoot, 'questions', 'q'));
  });

  test('resolve normalmente quando o diretório do commit é um link simbólico', async () => {
    const got = await resolveQuestionPaths(reposRoot, { repo: 's', commit: COMMIT, qid: 'q' });
    assert.equal(await fs.realpath(got.courseRoot), linkedCourseTarget);
    assert.equal(await fs.realpath(got.questionDir), path.join(linkedCourseTarget, 'questions', 'q'));
  });

  test('lança InvalidRequestError para commit abreviado, com maiúsculas, nome de branch ou tag', async () => {
    for (const commit of [COMMIT_SHORT, COMMIT_UPPER, 'main', 'v1.0']) {
      await assert.rejects(
        resolveQuestionPaths(reposRoot, { repo: 'r', commit, qid: 'q' }),
        InvalidRequestError,
        `o commit ${commit} deveria ser recusado como pedido inválido`,
      );
    }
  });

  test('lança InvalidRequestError para qid com ".", "..", absoluto ou segmento vazio', async () => {
    for (const qid of ['a/../b', './x', '/abs', 'a//b']) {
      await assert.rejects(
        resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT, qid }),
        InvalidRequestError,
        `o qid ${JSON.stringify(qid)} deveria ser recusado como pedido inválido`,
      );
    }
  });

  test('lança NotFoundError para repo e commit inexistentes', async () => {
    await assert.rejects(
      resolveQuestionPaths(reposRoot, { repo: 'nao-existe', commit: COMMIT, qid: 'q' }),
      NotFoundError,
      'repositório ausente deveria dar NotFoundError',
    );
    await assert.rejects(
      resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT_MISSING, qid: 'q' }),
      NotFoundError,
      'commit ausente deveria dar NotFoundError',
    );
  });

  test('lança NotFoundError para questão sem info.json', async () => {
    await assert.rejects(
      resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT, qid: 'sem-info' }),
      NotFoundError,
      'questão sem info.json deveria dar NotFoundError',
    );
  });

  test('lança NotFoundError para link que sai de questions/ (contenção por realpath)', async () => {
    // O alvo existe e tem info.json: só a contenção explica o 404.
    await assert.rejects(
      resolveQuestionPaths(reposRoot, { repo: 'r', commit: COMMIT, qid: 'fora' }),
      NotFoundError,
      'link para fora de questions/ deveria dar NotFoundError',
    );
  });
});
