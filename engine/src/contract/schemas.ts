// Validação contra os contratos publicados em contracts/engine/v1/, carregados do diretório
// ENGINE_CONTRACTS_DIR (config.contractsDir). Ajv 2020 do node_modules do PL, estrito (as mesmas
// opções do helper dos testes). O motor valida os pedidos (fora do esquema → 400 com details) e as
// próprias respostas (uma violação vira broken/ir_contract_violation). A correção recebe o
// snapshot pelo $defs.snapshot_input, tolerante, que o grade_request já referencia.
// Os details descrevem onde e por que o valor saiu do contrato, em português, sem copiar o valor:
// um pedido de correção traz a resposta crua do aluno (regra de privacidade).
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import { Ajv2020 } from 'ajv/dist/2020.js';
import type { ErrorObject, ValidateFunction } from 'ajv/dist/2020.js';

import {
  BROKEN_REASON_CODES,
  CONTRACT_PHASES,
  CONTRACT_VERSION,
  ERROR_CODES,
  IR_VERSION,
  UNSUPPORTED_REASON_CODES,
  WARNING_CODES,
} from './types.ts';
import type {
  CreateVariantRequest,
  CreateVariantResponse,
  ErrorResponse,
  GradeRequest,
  GradeResponse,
  HealthResponse,
  InspectRequest,
  InspectResponse,
} from './types.ts';

export const IR_SCHEMA_FILE = 'ir.schema.json';
export const API_SCHEMA_FILE = 'engine-api.schema.json';

export interface RequestTypes {
  inspect_request: InspectRequest;
  create_variant_request: CreateVariantRequest;
  grade_request: GradeRequest;
}

export interface ResponseTypes {
  health_response: HealthResponse;
  inspect_response: InspectResponse;
  create_variant_response: CreateVariantResponse;
  grade_response: GradeResponse;
  error_response: ErrorResponse;
}

export type RequestDef = keyof RequestTypes;
export type ResponseDef = keyof ResponseTypes;
/** Um $defs de engine-api.schema.json, ou "ir" para o ir.schema.json inteiro. */
export type SchemaName = RequestDef | ResponseDef | 'question_ref' | 'snapshot' | 'snapshot_input' | 'ir';

const SCHEMA_NAMES: readonly SchemaName[] = [
  'inspect_request',
  'create_variant_request',
  'grade_request',
  'health_response',
  'inspect_response',
  'create_variant_response',
  'grade_response',
  'error_response',
  'question_ref',
  'snapshot',
  'snapshot_input',
  'ir',
];

export type Validation = { valid: true; details: [] } | { valid: false; details: string[] };
export type Parsed<T> = { ok: true; value: T } | { ok: false; details: string[] };

export interface Contracts {
  readonly contractVersion: typeof CONTRACT_VERSION;
  readonly irVersion: typeof IR_VERSION;
  validate(name: SchemaName, value: unknown): Validation;
  /** Valida o corpo (não confiável) de um pedido e o devolve tipado. */
  parseRequest<D extends RequestDef>(def: D, body: unknown): Parsed<RequestTypes[D]>;
  validateResponse(def: ResponseDef, body: unknown): Validation;
}

/** Os esquemas não carregam ou não são os esperados: falha do motor, na subida. */
export class ContractLoadError extends Error {
  override name = 'ContractLoadError';
}

// Limite de details por resposta de erro; um oneOf com allErrors pode gerar dezenas.
const MAX_DETAILS = 20;

const cache = new Map<string, Contracts>();

/** Carrega e compila os dois esquemas do diretório (em cache pelo conteúdo dos arquivos). */
export function loadContracts(contractsDir: string): Contracts {
  const irText = readText(contractsDir, IR_SCHEMA_FILE);
  const apiText = readText(contractsDir, API_SCHEMA_FILE);
  const key = createHash('sha256').update(irText).update('\0').update(apiText).digest('hex');
  let contracts = cache.get(key);
  if (contracts === undefined) {
    contracts = compile(parseSchema(irText, IR_SCHEMA_FILE), parseSchema(apiText, API_SCHEMA_FILE));
    cache.set(key, contracts);
  }
  return contracts;
}

interface SchemaDocument {
  $id: string;
  $defs: Record<string, unknown>;
  [key: string]: unknown;
}

function readText(dir: string, file: string): string {
  try {
    return fs.readFileSync(path.join(dir, file), 'utf8');
  } catch {
    throw new ContractLoadError(`não foi possível ler o contrato ${file} no diretório ${dir}.`);
  }
}

function parseSchema(text: string, file: string): SchemaDocument {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    throw new ContractLoadError(`o contrato ${file} não é JSON válido.`);
  }
  const record = asRecord(doc);
  if (!record || typeof record.$id !== 'string' || !asRecord(record.$defs)) {
    throw new ContractLoadError(`o contrato ${file} não tem $id e $defs.`);
  }
  return record as SchemaDocument;
}

function compile(irSchema: SchemaDocument, apiSchema: SchemaDocument): Contracts {
  checkAgreement(irSchema, apiSchema);

  const ajv = new Ajv2020({ strict: true, allowUnionTypes: true, allErrors: true });
  ajv.addSchema(irSchema);
  ajv.addSchema(apiSchema);
  const pointer = (name: string): string => `${apiSchema.$id}#/$defs/${name}`;

  const validators = new Map<SchemaName, ValidateFunction>();
  for (const name of SCHEMA_NAMES) {
    const validator = ajv.getSchema(name === 'ir' ? irSchema.$id : pointer(name));
    if (!validator) throw new ContractLoadError(`o contrato não define o esquema ${name}.`);
    validators.set(name, validator);
  }

  // Nas respostas com oneOf por outcome, os details vêm do ramo do outcome do valor: sem isso, uma
  // IR inválida viria misturada com "falta reason" e "falta reasons" dos outros ramos.
  const branches = new Map<SchemaName, Map<string, ValidateFunction>>();
  for (const name of SCHEMA_NAMES) {
    const oneOf = asRecord(apiSchema.$defs[name])?.oneOf;
    if (!Array.isArray(oneOf)) continue;
    const byOutcome = new Map<string, ValidateFunction>();
    oneOf.forEach((branch: unknown, i: number) => {
      const outcome = asRecord(asRecord(asRecord(branch)?.properties)?.outcome)?.const;
      const validator = ajv.getSchema(`${pointer(name)}/oneOf/${i}`);
      if (typeof outcome === 'string' && validator) byOutcome.set(outcome, validator);
    });
    if (byOutcome.size > 0) branches.set(name, byOutcome);
  }

  const validate = (name: SchemaName, value: unknown): Validation => {
    const validator = validators.get(name);
    if (!validator) throw new ContractLoadError(`esquema desconhecido: ${name}`);
    if (validator(value)) return { valid: true, details: [] };
    let errors: ErrorObject[] = [...(validator.errors ?? [])];
    const outcome = asRecord(value)?.outcome;
    const branch = typeof outcome === 'string' ? branches.get(name)?.get(outcome) : undefined;
    if (branch && !branch(value)) errors = [...(branch.errors ?? errors)];
    return { valid: false, details: formatErrors(errors) };
  };

  return {
    contractVersion: CONTRACT_VERSION,
    irVersion: IR_VERSION,
    validate,
    parseRequest<D extends RequestDef>(def: D, body: unknown): Parsed<RequestTypes[D]> {
      const result = validate(def, body);
      return result.valid ? { ok: true, value: body as RequestTypes[D] } : { ok: false, details: result.details };
    },
    validateResponse: (def, body) => validate(def, body),
  };
}

/**
 * As versões e as enumerações fixadas no código (types.ts) têm de ser as dos esquemas carregados:
 * um contrato trocado sem o código acompanhar falha na subida, e não num pedido.
 */
function checkAgreement(irSchema: SchemaDocument, apiSchema: SchemaDocument): void {
  const at = (schema: unknown, ...keys: string[]): unknown =>
    keys.reduce<unknown>((node, key) => asRecord(node)?.[key], schema);
  const defs = apiSchema.$defs;
  const expected: Array<[string, unknown, unknown]> = [
    ['ir_version', at(irSchema, 'properties', 'ir_version', 'const'), IR_VERSION],
    ['contract_version', at(defs.health_response, 'properties', 'contract_version', 'const'), CONTRACT_VERSION],
    ['warning.code', at(defs.warning, 'properties', 'code', 'enum'), WARNING_CODES],
    ['warning.phase', at(defs.warning, 'properties', 'phase', 'enum'), CONTRACT_PHASES],
    ['reason.code', at(defs.reason, 'properties', 'code', 'enum'), UNSUPPORTED_REASON_CODES],
    ['broken_reason.code', at(defs.broken_reason, 'properties', 'code', 'enum'), BROKEN_REASON_CODES],
    ['broken_reason.phase', at(defs.broken_reason, 'properties', 'phase', 'enum'), CONTRACT_PHASES],
    ['error_response.error.code', at(defs.error_response, 'properties', 'error', 'properties', 'code', 'enum'), ERROR_CODES],
  ];
  for (const [what, inSchema, inCode] of expected) {
    const same = Array.isArray(inCode)
      ? Array.isArray(inSchema) && inSchema.length === inCode.length && inCode.every((v) => inSchema.includes(v))
      : inSchema === inCode;
    if (!same) {
      throw new ContractLoadError(
        `contrato divergente do código em ${what}: o esquema traz ${JSON.stringify(inSchema)}, o motor espera ${JSON.stringify(inCode)}.`,
      );
    }
  }
}

// ---------------------------------------------------------------------------------------------
// details em português
// ---------------------------------------------------------------------------------------------

const TYPE_NAMES: Record<string, string> = {
  string: 'texto',
  number: 'número',
  integer: 'número inteiro',
  boolean: 'booleano',
  object: 'objeto',
  array: 'lista',
  null: 'null',
};

function formatErrors(errors: readonly ErrorObject[]): string[] {
  const details = new Set<string>();
  for (const error of errors) {
    // O "if" sempre vem junto do erro do then/else, que é o que diz o que falta.
    if (error.keyword === 'if') continue;
    details.add(`${error.instancePath === '' ? '(corpo)' : error.instancePath}: ${describe(error)}`);
  }
  if (details.size === 0) details.add('(corpo): o valor não segue o contrato.');
  const list = [...details];
  if (list.length <= MAX_DETAILS) return list;
  return [...list.slice(0, MAX_DETAILS), `… e mais ${list.length - MAX_DETAILS} problema(s).`];
}

/** Descreve a regra violada sem citar o valor recebido (só nomes de propriedade e o esperado). */
function describe(error: ErrorObject): string {
  const params = error.params as Record<string, unknown>;
  switch (error.keyword) {
    case 'required':
      return `falta a propriedade obrigatória ${quote(params.missingProperty)}`;
    case 'additionalProperties':
      return `propriedade não permitida ${quote(params.additionalProperty)}`;
    case 'type':
      return `deve ser ${typeList(params.type)}`;
    case 'const':
      return `deve ser ${json(params.allowedValue)}`;
    case 'enum':
      return `deve ser um destes valores: ${(Array.isArray(params.allowedValues) ? params.allowedValues : []).map(json).join(', ')}`;
    case 'pattern':
      return `não segue o formato ${String(params.pattern)}`;
    case 'minLength':
      return `deve ter pelo menos ${String(params.limit)} caractere(s)`;
    case 'maxLength':
      return `deve ter no máximo ${String(params.limit)} caractere(s)`;
    case 'minimum':
      return `deve ser maior ou igual a ${String(params.limit)}`;
    case 'maximum':
      return `deve ser menor ou igual a ${String(params.limit)}`;
    case 'exclusiveMinimum':
      return `deve ser maior que ${String(params.limit)}`;
    case 'exclusiveMaximum':
      return `deve ser menor que ${String(params.limit)}`;
    case 'minItems':
      return `deve ter pelo menos ${String(params.limit)} item(ns)`;
    case 'maxItems':
      return `deve ter no máximo ${String(params.limit)} item(ns)`;
    case 'minProperties':
      return `deve ter pelo menos ${String(params.limit)} propriedade(s)`;
    case 'maxProperties':
      return `deve ter no máximo ${String(params.limit)} propriedade(s)`;
    case 'oneOf':
      return 'deve seguir exatamente um dos formatos permitidos';
    case 'anyOf':
      return 'deve seguir pelo menos um dos formatos permitidos';
    case 'not':
      return 'não pode seguir este formato';
    case 'false schema':
      return 'não é permitido aqui';
    default:
      return `não atende à regra "${error.keyword}" do contrato`;
  }
}

function typeList(type: unknown): string {
  const types = Array.isArray(type) ? type.map(String) : String(type).split(',');
  return types.map((t) => TYPE_NAMES[t] ?? t).join(' ou ');
}

/** Nome de propriedade vindo do pedido, entre aspas e com tamanho limitado. */
function quote(value: unknown): string {
  const text = String(value);
  return `"${text.length > 80 ? `${text.slice(0, 80)}…` : text}"`;
}

function json(value: unknown): string {
  return JSON.stringify(value) ?? String(value);
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
}
