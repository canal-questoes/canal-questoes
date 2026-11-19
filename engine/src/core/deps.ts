// O que as operações do núcleo recebem do motor (engine.ts): a área de repositórios, os executores
// Python e as versões que vão no snapshot e na comparação da correção.
import type { Executors } from '../pl/executors.ts';

export interface OperationDeps {
  reposRoot: string;
  executors: Executors;
  /** Limite de cada chamada do código da questão, citado nas mensagens de tempo esgotado. */
  questionTimeoutMs: number;
  engineVersion: string;
  plCommit: string;
}
