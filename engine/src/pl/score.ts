// Score da questão a partir de partial_scores, espelhando processQuestionHtml e getPartialScoreValues
// de /PrairieLearn/apps/prairielearn/dist/question-servers/freeform.js (PL a193f72). O PL calcula o
// score logo depois do grade dos elementos, antes do grade do server.py.

/** Como getPartialScoreValues: score não numérico conta 0; weight não numérico conta 1. */
function partialScoreValues(value: unknown): { score: number; weight: number } {
  const obj = (typeof value === 'object' && value != null ? value : {}) as Record<string, unknown>;
  return {
    score: typeof obj.score === 'number' ? obj.score : 0,
    weight: typeof obj.weight === 'number' ? obj.weight : 1,
  };
}

/**
 * Com crédito parcial: média das notas ponderada pelos pesos (divisor 1 se o peso total é 0).
 * Sem crédito parcial: 1 se há pelo menos uma parte e todas têm score ≥ 1; senão 0.
 */
export function computeScore(partialScores: Record<string, unknown> | undefined, partialCredit: boolean): number {
  const values = Object.values(partialScores ?? {});
  if (partialCredit) {
    let totalWeight = 0;
    let totalWeightScore = 0;
    for (const value of values) {
      const { score, weight } = partialScoreValues(value);
      totalWeight += weight;
      totalWeightScore += weight * score;
    }
    return totalWeightScore / (totalWeight === 0 ? 1 : totalWeight);
  }
  return values.length > 0 && values.every((value) => partialScoreValues(value).score >= 1) ? 1 : 0;
}
