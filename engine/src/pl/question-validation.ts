// As regras de validateQuestion (dist/sync/course-db.js do PL a193f72, linhas 807-869), que a
// sincronização do PL aplica ao info.json de cada questão depois do esquema (loadAndValidateJson, sobre
// a saída do QuestionJsonSchema, com os padrões preenchidos). Uma questão com algum desses erros fica só
// com sync_errors no banco (sync_questions.sql: o "first pass" só grava tipo, preferências e o resto das
// questões sem erro), e o PL não a executa. No motor, ela dá invalid_question_metadata, como o info.json
// fora do esquema.
// A função do PL não é exportada: as regras estão reproduzidas aqui na ordem e com as mensagens do PL,
// reaproveitando do PL fixado o validatePreferencesSchema (dist/lib/question-settings/validation.js),
// os esquemas de options (dist/schemas) e o zod. Ficam de fora, por escolha deliberada:
// - o compartilhamento (sharingSets, sharePublicly, shareSourcePublicly), que só dá erro com
//   config.checkSharingOnSync, false por padrão no PL;
// - o timeout de externalGradingOptions acima do máximo, que só dá aviso;
// - a existência do originCourse dos autores (checkAuthorOriginCourses, em loadQuestions), que consulta
//   o banco do PL.
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { z } from 'zod';

import { APP_ROOT_PATH } from './bindings.ts';

function distModule(rel: string): Promise<Record<string, unknown>> {
  return import(pathToFileURL(path.join(APP_ROOT_PATH, 'dist', rel)).href) as Promise<Record<string, unknown>>;
}

const [validationModule, schemasModule] = await Promise.all([
  distModule('lib/question-settings/validation.js'),
  distModule('schemas/index.js'),
]);

const validatePreferencesSchema = validationModule.validatePreferencesSchema as (preferences: unknown) => string[];

interface ParseSchema {
  parse(value: unknown): unknown;
}

/** A parte da saída do QuestionJsonSchema que as regras leem (o esquema garante os tipos e os padrões). */
interface ParsedQuestion {
  type: string;
  options?: Record<string, unknown>;
  gradingMethod: string;
  externalGradingOptions?: unknown;
  preferences?: Record<string, unknown>;
  authors: Array<{ email?: string; orcid?: string; originCourse?: string }>;
}

/** isValidORCID do course-db.js: o dígito verificador do ORCID (ISO 7064 11,2). */
function isValidORCID(orcid: string): boolean {
  const digits = orcid.replaceAll('-', '');
  if (!/^\d{15}[\dX]$/.test(digits)) return false;
  let total = 0;
  for (let i = 0; i < 15; i++) {
    total = (total + Number.parseInt(digits[i])) * 2;
  }
  const remainder = total % 11;
  const result = (12 - remainder) % 11;
  const checkDigit = result === 10 ? 'X' : String(result);
  return digits[15] === checkDigit;
}

/**
 * Os erros de validateQuestion para a saída do QuestionJsonSchema do PL, com as mensagens do PL, na
 * mesma ordem; vazio se a questão sincroniza.
 */
export function validateQuestion(parsed: unknown): string[] {
  const question = parsed as ParsedQuestion;
  const errors: string[] = [];
  if (question.options) {
    if (question.type === 'v3') {
      errors.push('"options" is not supported for v3 questions.');
    } else {
      try {
        const schema = schemasModule[`QuestionOptions${question.type}JsonSchema`] as ParseSchema;
        schema.parse(question.options);
      } catch (err) {
        errors.push(`Error validating question options: ${(err as Error).message}`);
      }
    }
  }
  if (question.gradingMethod === 'External' && !question.externalGradingOptions) {
    errors.push('"externalGradingOptions" is required when "gradingMethod" is "External"');
  }
  if (question.preferences) {
    errors.push(...validatePreferencesSchema(question.preferences));
  }
  if (question.authors.length > 0) {
    for (const author of question.authors) {
      if (!author.email && !author.orcid && !author.originCourse) {
        errors.push('At least one of "email", "orcid", or "originCourse" is required for each author');
      }
      if (author.orcid && !isValidORCID(author.orcid)) {
        errors.push(
          `The author ORCID identifier "${author.orcid}" has an invalid checksum. See the official website (https://orcid.org) for info on how to create or look up an identifier`,
        );
      }
      if (author.email && !z.email().safeParse(author.email).success) {
        errors.push(`The author email address "${author.email}" is invalid`);
      }
    }
  }
  return errors;
}
