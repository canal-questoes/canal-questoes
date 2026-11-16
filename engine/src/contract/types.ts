// Tipos TypeScript do contrato v1. A fonte de verdade são os esquemas em contracts/engine/v1/
// (engine-api.schema.json e ir.schema.json): estes tipos os espelham, e schemas.ts confere na
// carga que as versões abaixo são as dos esquemas. As enumerações usadas em tempo de execução (ex.:
// pelos registros, log.ts) ficam em listas constantes, de onde os tipos são derivados.

export const CONTRACT_VERSION = '1.0';
export const IR_VERSION = '1.0';

/** Como no checkData do PL: qualquer valor não nulo com typeof 'object' (inclusive listas). */
export type JsonObject = Record<string, unknown> | unknown[];

// ---------------------------------------------------------------------------------------------
// engine-api.schema.json
// ---------------------------------------------------------------------------------------------

export interface QuestionRef {
  repo: string;
  commit: string;
  qid: string;
}

/** Fases que aparecem em avisos e quebras. */
export const CONTRACT_PHASES = ['generate', 'prepare', 'render', 'file', 'parse', 'grade'] as const;
export type ContractPhase = (typeof CONTRACT_PHASES)[number];

export const WARNING_CODES = ['console_output', 'missing_media', 'media_error'] as const;
export type WarningCode = (typeof WARNING_CODES)[number];

export interface Warning {
  code: WarningCode;
  /** Frase funcional em português, sem detalhe técnico. */
  message: string;
  phase?: ContractPhase;
}

/** Motivos de "não suportada". */
export const UNSUPPORTED_REASON_CODES = [
  'question_type_not_v3',
  'grading_method_not_internal',
  'external_grading',
  'workspace',
  'unsupported_elements',
  'no_answer_fields',
  'course_element_override',
  'custom_server_render',
  'invalid_answers_name',
  'invalid_question_metadata',
] as const;
export type UnsupportedReasonCode = (typeof UNSUPPORTED_REASON_CODES)[number];

export interface UnsupportedReason {
  code: UnsupportedReasonCode;
  message: string;
  elements?: string[];
}

export const BROKEN_REASON_CODES = ['question_code_error', 'timeout', 'invalid_data', 'ir_contract_violation'] as const;
export type BrokenReasonCode = (typeof BROKEN_REASON_CODES)[number];

export interface BrokenReason {
  code: BrokenReasonCode;
  message: string;
  phase: ContractPhase;
  /** Até 8000 caracteres; nunca os dados da chamada. */
  technical_detail?: string;
}

export interface QuestionMetadata {
  uuid: string | null;
  title: string | null;
  topic: string | null;
  tags: string[];
  type: string | null;
  grading_method: string;
  elements: string[];
  show_correct_answer: boolean;
  single_variant: boolean;
  partial_credit: boolean;
  answers_names: string[];
  content_hash: string;
}

/** Snapshot da saída da criação de variante ($defs.snapshot, estrito). */
export interface Snapshot {
  seed: string;
  params: JsonObject;
  correct_answers: JsonObject;
  preferences: JsonObject;
  engine_version: string;
  pl_commit: string;
  ir_version: typeof IR_VERSION;
}

/** Snapshot recebido na correção ($defs.snapshot_input): ir_version 1.x e chaves desconhecidas. */
export interface SnapshotInput {
  seed: string;
  params: JsonObject;
  correct_answers: JsonObject;
  preferences: JsonObject;
  engine_version: string;
  pl_commit: string;
  ir_version: string;
  [key: string]: unknown;
}

export interface FormatError {
  html: string;
  text: string;
}

export interface VersionMismatch {
  engine: boolean;
  pl: boolean;
}

export interface HealthResponse {
  status: 'ready';
  engine_version: string;
  contract_version: typeof CONTRACT_VERSION;
  ir_version: typeof IR_VERSION;
  pl_commit: string;
  workers: number;
}

export interface InspectRequest {
  question_ref: QuestionRef;
}

export interface InspectResponse {
  outcome: 'supported' | 'unsupported';
  question: QuestionMetadata;
  /** Vazia se suportada; pelo menos um motivo se não. */
  reasons: UnsupportedReason[];
}

export type PreferenceValue = string | number | boolean;

export interface CreateVariantRequest {
  question_ref: QuestionRef;
  seed: string;
  preferences?: Record<string, PreferenceValue>;
}

export interface CreateVariantOk {
  outcome: 'ok';
  snapshot: Snapshot;
  ir: Ir;
  warnings: Warning[];
}

export interface CreateVariantBroken {
  outcome: 'broken';
  reason: BrokenReason;
  warnings: Warning[];
}

export interface CreateVariantUnsupported {
  outcome: 'unsupported';
  reasons: UnsupportedReason[];
}

export type CreateVariantResponse = CreateVariantOk | CreateVariantBroken | CreateVariantUnsupported;

export interface GradeRequest {
  question_ref: QuestionRef;
  snapshot: SnapshotInput;
  /** Resposta crua por answers-name, sem normalização. Nunca vai para os registros. */
  raw_submitted_answers: Record<string, unknown>;
}

export interface GradeOk {
  outcome: 'ok';
  valid: boolean;
  format_errors: Record<string, FormatError>;
  submitted_answers: JsonObject;
  /** null quando valid é false. */
  partial_scores: JsonObject | null;
  score: number | null;
  feedback: JsonObject;
  params: JsonObject;
  correct_answers: JsonObject;
  version_mismatch: VersionMismatch;
  warnings: Warning[];
}

export interface GradeBroken {
  outcome: 'broken';
  reason: BrokenReason;
  version_mismatch: VersionMismatch;
  warnings: Warning[];
}

export type GradeResponse = GradeOk | GradeBroken;

export const ERROR_CODES = [
  'invalid_request',
  'not_found',
  'payload_too_large',
  'route_not_found',
  'method_not_allowed',
  'internal_error',
] as const;
export type ErrorCode = (typeof ERROR_CODES)[number];

export interface ErrorResponse {
  error: {
    code: ErrorCode;
    /** Frase em português, sem pilha nem detalhe interno. */
    message: string;
    details?: string[];
  };
}

// ---------------------------------------------------------------------------------------------
// Entrada em lote da CLI
// ---------------------------------------------------------------------------------------------

export const BATCH_OPS = ['health', 'inspect', 'create_variant', 'grade'] as const;
export type BatchOp = (typeof BATCH_OPS)[number];

export interface BatchRequestItem {
  id: string;
  op: BatchOp;
  body: unknown;
}

export interface BatchResultItem {
  /** O id do pedido; null num item do lote sem id textual. */
  id: string | null;
  /** O status HTTP equivalente. */
  status: number;
  body: unknown;
  ms: number;
}

// ---------------------------------------------------------------------------------------------
// ir.schema.json
// ---------------------------------------------------------------------------------------------

export type FieldType = 'number' | 'integer' | 'text' | 'single_choice' | 'multiple_choice';

export interface NumberFormat {
  kind: 'number';
  comparison: 'relabs' | 'sigfig' | 'decdig';
  rtol?: number;
  atol?: number;
  digits?: number;
  allow_fractions: boolean;
  allow_complex: boolean;
}

export interface IntegerFormat {
  kind: 'integer';
  /** 0 aceita os prefixos 0x, 0b e 0o; senão, 2 a 36. */
  base: number;
}

export interface TextFormat {
  kind: 'text';
  ignore_case: boolean;
  remove_spaces: boolean;
  remove_leading_trailing: boolean;
  normalize_to_ascii: boolean;
  multiline: boolean;
  regex: boolean;
}

export interface SingleChoiceFormat {
  kind: 'single_choice';
}

export type MultipleChoicePartialCredit = 'off' | 'net-correct' | 'coverage' | 'each-answer';

export interface MultipleChoiceFormat {
  kind: 'multiple_choice';
  partial_credit: MultipleChoicePartialCredit;
}

/** format.kind é sempre igual ao type do campo. */
export type FieldFormat = NumberFormat | IntegerFormat | TextFormat | SingleChoiceFormat | MultipleChoiceFormat;

export interface FieldConstraints {
  allow_blank: boolean;
  /** Só em multiple_choice, onde os dois são obrigatórios. */
  min_select?: number;
  max_select?: number;
}

export interface IrOption {
  key: string;
  html: string;
  content_hash: string;
}

export interface IrField {
  name: string;
  type: FieldType;
  label_html: string | null;
  suffix_html: string | null;
  format: FieldFormat;
  /** 1 a 64 caracteres. */
  hint: string;
  constraints: FieldConstraints;
  /** ≥ 0. */
  weight: number;
  /** Lista nos tipos de escolha; null nos demais. */
  options: IrOption[] | null;
}

export interface AnswerKeyEntry {
  name: string;
  can_show: boolean;
  /** correct_answers[name] como o PL o guarda, ou null se ausente. */
  correct_value: unknown;
  display_html: string | null;
}

export interface Explanation {
  html: string;
  can_show: boolean;
}

export interface IrMedia {
  /** m1, m2… */
  id: string;
  origin: 'static' | 'dynamic';
  filename: string;
  content_type: string;
  alt: string | null;
  status: 'ok' | 'missing';
  /** Presentes só com status "ok". */
  data_base64?: string;
  size_bytes?: number;
}

export interface Ir {
  ir_version: typeof IR_VERSION;
  statement: { html: string };
  fields: IrField[];
  answer_key: AnswerKeyEntry[];
  explanation: Explanation | null;
  media: IrMedia[];
}
