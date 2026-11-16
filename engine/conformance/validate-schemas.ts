// Validação das IR da conformidade contra os esquemas publicados.
//
// Roda DENTRO da imagem de teste, onde o Ajv do PL e os contratos existem:
//   docker run --rm -i --network none --user 65534:65534 --read-only --tmpfs /tmp \
//     canal-questoes/engine:test node conformance/validate-schemas.ts < corpos.json
// Lê da stdin uma lista JSON de corpos de `create_variant` (respostas 200 da CLI) e valida cada um
// contra `create_variant_response` e, quando `outcome` é `ok`, a IR contra `ir.schema.json`, com o
// helper independente `engine/test/helpers/schemas.ts`. Escreve na stdout
// `{"validated": <n>, "errors": [{"index": <i>, "message": "..."}]}` e sai com código 1 se algum falhar.
import { assertValid } from '../test/helpers/schemas.ts';

interface ValidationError {
  index: number;
  message: string;
}

const chunks: Buffer[] = [];
for await (const chunk of process.stdin) chunks.push(chunk as Buffer);

let bodies: unknown;
try {
  bodies = JSON.parse(Buffer.concat(chunks).toString('utf8'));
} catch (err) {
  process.stderr.write(`JSON inválido na entrada de validate-schemas.ts: ${(err as Error).message}\n`);
  process.exit(1);
}
if (!Array.isArray(bodies)) {
  process.stderr.write('a entrada de validate-schemas.ts precisa ser uma lista de corpos de create_variant\n');
  process.exit(1);
}

const errors: ValidationError[] = [];
bodies.forEach((body: unknown, index: number) => {
  try {
    assertValid('create_variant_response', body);
    if ((body as { outcome?: unknown }).outcome === 'ok') assertValid('ir', (body as { ir?: unknown }).ir);
  } catch (err) {
    errors.push({ index, message: err instanceof Error ? err.message : String(err) });
  }
});

process.stdout.write(JSON.stringify({ validated: bodies.length, errors }));
process.exitCode = errors.length > 0 ? 1 : 0;
