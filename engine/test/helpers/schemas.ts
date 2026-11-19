// Validação contra os contratos publicados, independente de src/.
// Usa o Ajv 2020 do node_modules do PL, em modo estrito.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

const requirePl = createRequire('/PrairieLearn/apps/prairielearn/');
const Ajv2020 = requirePl('ajv/dist/2020').default;

export const CONTRACTS_DIR =
  process.env.ENGINE_CONTRACTS_DIR ?? '/PrairieLearn/apps/prairielearn/canal-questoes-engine/contracts/engine/v1';

const irSchema = JSON.parse(fs.readFileSync(path.join(CONTRACTS_DIR, 'ir.schema.json'), 'utf8'));
const apiSchema = JSON.parse(fs.readFileSync(path.join(CONTRACTS_DIR, 'engine-api.schema.json'), 'utf8'));

export function newAjv(): any {
  const ajv = new Ajv2020({ strict: true, allowUnionTypes: true, allErrors: true });
  ajv.addSchema(irSchema);
  ajv.addSchema(apiSchema);
  return ajv;
}

const ajv = newAjv();

export const API_DEFS: string[] = Object.keys(apiSchema.$defs);

function validator(name: string): any {
  const id = name === 'ir' ? irSchema.$id : `${apiSchema.$id}#/$defs/${name}`;
  const v = ajv.getSchema(id);
  if (!v) throw new Error(`esquema desconhecido: ${name}`);
  return v;
}

export function isValid(name: string, value: unknown): boolean {
  return validator(name)(value) as boolean;
}

export function assertValid(name: string, value: unknown): void {
  const v = validator(name);
  if (!v(value)) {
    const errors = (v.errors ?? [])
      .map((e: any) => `${e.instancePath || '(raiz)'} ${e.message}${e.params ? ' ' + JSON.stringify(e.params) : ''}`)
      .join('\n');
    assert.fail(`documento inválido contra "${name}":\n${errors}\n${JSON.stringify(value).slice(0, 2000)}`);
  }
}
