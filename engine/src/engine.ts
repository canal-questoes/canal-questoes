// Fachada do motor (a interface que o servidor HTTP, a CLI e os testes usam).
// Cada operação recebe o corpo JSON (não confiável), valida o pedido contra o contrato (400 com
// details), executa e confere a própria resposta: uma resposta que violaria o contrato por causa da
// questão vira broken/ir_contract_violation (determinístico, sem nova tentativa), e nunca 500. Só uma
// falha do próprio motor dá 500 internal_error, sem a mensagem nem a pilha da exceção.
// Cada pedido gera uma linha de registro (log.ts), sem respostas cruas nem conteúdo gerado.
// A saúde só responde 200 ready quando um executor Python pode ser obtido (pl/readiness.ts); senão,
// 500 internal_error, porque o contrato fixa status "ready" no 200.
import type { EngineConfig } from './config.ts';
import { engineClosedResult, errorResultFromException, errorResult, invalidRequestResult } from './contract/error-response.ts';
import { loadContracts, type Contracts, type RequestDef, type RequestTypes, type ResponseDef } from './contract/schemas.ts';
import type { BatchOp, CreateVariantResponse, GradeResponse, HealthResponse } from './contract/types.ts';
import { CONTRACT_VERSION, IR_VERSION } from './contract/types.ts';
import { createVariant } from './core/create-variant.ts';
import { gradeSubmission } from './core/grade.ts';
import { inspectQuestion } from './core/inspect.ts';
import { contractViolation } from './core/outcome.ts';
import { loadQuestion } from './core/question.ts';
import { jsonLineLogger, requestLogEntry, type LogFn } from './log.ts';
import { NOT_READY_MESSAGE, TrackedExecutors } from './pl/readiness.ts';

export interface EngineResult {
  status: 200 | 400 | 404 | 500;
  body: unknown;
}

export interface Engine {
  health(): Promise<EngineResult>;
  inspect(body: unknown): Promise<EngineResult>;
  createVariant(body: unknown): Promise<EngineResult>;
  grade(body: unknown): Promise<EngineResult>;
  close(): Promise<void>;
}

export async function createEngine(config: EngineConfig): Promise<Engine> {
  const contracts: Contracts = loadContracts(config.contractsDir);
  const log: LogFn = config.log ?? jsonLineLogger(process.stdout);
  const executors = new TrackedExecutors({ size: config.workers, questionTimeoutMs: config.questionTimeoutMs, log });
  const deps = {
    reposRoot: config.reposRoot,
    executors,
    questionTimeoutMs: config.questionTimeoutMs,
    engineVersion: config.engineVersion,
    plCommit: config.plCommit,
  };
  let closed = false;

  /** Uma operação: registro, 500 depois de close() e mapeamento das exceções (errors.ts). */
  const operation = async (op: BatchOp, request: unknown, run: () => Promise<EngineResult>): Promise<EngineResult> => {
    const started = performance.now();
    let result: EngineResult;
    if (closed) {
      result = engineClosedResult();
    } else {
      try {
        result = await run();
      } catch (err) {
        result = closed ? engineClosedResult() : errorResultFromException(err);
      }
    }
    log(requestLogEntry({ op, request, status: result.status, response: result.body, ms: performance.now() - started }));
    return result;
  };

  /** Valida o pedido e roda a operação; fora do contrato, 400 com details. */
  const withRequest = async <D extends RequestDef>(
    def: D,
    body: unknown,
    run: (request: RequestTypes[D]) => Promise<EngineResult>,
  ): Promise<EngineResult> => {
    const parsed = contracts.parseRequest(def, body);
    return parsed.ok ? run(parsed.value) : invalidRequestResult(parsed.details);
  };

  /** Uma resposta 200 conferida contra o contrato; fora dele, falha do motor (500). */
  const checked = (def: ResponseDef, body: unknown): EngineResult => {
    const validation = contracts.validateResponse(def, body);
    return validation.valid ? { status: 200, body } : errorResult('internal_error');
  };

  return {
    health: () =>
      operation('health', undefined, async () => {
        // Sem executor, nenhuma operação com código da questão roda: o motor não está pronto.
        if (!(await executors.ready(config.reposRoot))) {
          return closed ? engineClosedResult() : errorResult('internal_error', { message: NOT_READY_MESSAGE });
        }
        const body: HealthResponse = {
          status: 'ready',
          engine_version: config.engineVersion,
          contract_version: CONTRACT_VERSION,
          ir_version: IR_VERSION,
          pl_commit: config.plCommit,
          workers: config.workers,
        };
        return checked('health_response', body);
      }),

    inspect: (body) =>
      operation('inspect', body, () =>
        withRequest('inspect_request', body, async (request) => {
          const question = await loadQuestion(config.reposRoot, request.question_ref);
          return checked('inspect_response', await inspectQuestion(question));
        }),
      ),

    createVariant: (body) =>
      operation('create_variant', body, () =>
        withRequest('create_variant_request', body, async (request) => {
          const outcome = await createVariant(deps, request);
          const validation = contracts.validateResponse('create_variant_response', outcome.response);
          if (validation.valid) return { status: 200, body: outcome.response };
          // A resposta violaria o contrato por causa da questão.
          const broken: CreateVariantResponse = {
            outcome: 'broken',
            reason: contractViolation(outcome.phase, validation.details, config.questionTimeoutMs),
            warnings: [],
          };
          return checked('create_variant_response', broken);
        }),
      ),

    grade: (body) =>
      operation('grade', body, () =>
        withRequest('grade_request', body, async (request) => {
          const outcome = await gradeSubmission(deps, request);
          const validation = contracts.validateResponse('grade_response', outcome.response);
          if (validation.valid) return { status: 200, body: outcome.response };
          // Os details não copiam valores; a redação da resposta crua é uma garantia a mais.
          const broken: GradeResponse = {
            outcome: 'broken',
            reason: contractViolation(outcome.phase, validation.details, config.questionTimeoutMs, { raw: outcome.raw }),
            version_mismatch: outcome.response.version_mismatch,
            warnings: [],
          };
          return checked('grade_response', broken);
        }),
      ),

    close: async () => {
      if (closed) return;
      closed = true;
      await executors.close();
    },
  };
}
