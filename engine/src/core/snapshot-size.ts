// O tamanho do snapshot da variante.
// O snapshot é tudo o que o núcleo devolve na correção, dentro do corpo do POST /v1/grade, que o servidor
// HTTP limita a 2 MiB (413 payload_too_large acima disso). O PL não limita os params (ficam no
// banco), mas aqui eles vão e voltam: uma variante cujo snapshot não cabe num pedido de correção nunca
// seria corrigida. Por isso a criação a recusa como "questão quebrada" (ir_contract_violation, na última
// fase executada, a render), de forma determinística, em vez de devolver "ok".
// A folga é metade do corpo: o snapshot serializado (JSON compacto, UTF-8) vai até 1 MiB, e a outra
// metade fica para o question_ref, a resposta crua (raw_submitted_answers, sem teto no contrato) e a
// serialização do próprio núcleo, que pode ser maior que a compacta (o texto do jsonb, por exemplo, põe
// um espaço depois de cada ":" e ",").
import type { BrokenReason, Snapshot } from '../contract/types.ts';

/** O limite do corpo de um pedido ao servidor HTTP do motor (MAX_BODY_BYTES de http/server.ts). */
export const REQUEST_BODY_MAX_BYTES = 2 * 1024 * 1024;

/** O maior snapshot que a criação de variante devolve: metade do corpo do pedido de correção. */
export const SNAPSHOT_MAX_BYTES = REQUEST_BODY_MAX_BYTES / 2;

/** O tamanho do snapshot em JSON compacto (UTF-8), como ele sai na resposta. */
export function snapshotBytes(snapshot: Snapshot): number {
  return Buffer.byteLength(JSON.stringify(snapshot), 'utf8');
}

/** A quebra de um snapshot grande demais para a correção, ou undefined se ele cabe. */
export function oversizedSnapshot(snapshot: Snapshot): BrokenReason | undefined {
  const bytes = snapshotBytes(snapshot);
  if (bytes <= SNAPSHOT_MAX_BYTES) return undefined;
  return {
    code: 'ir_contract_violation',
    // A frase da quebra por contrato na fase render (outcome.ts) falaria da montagem do enunciado e do
    // gabarito; o que não cabe são os dados da variante, e a frase é própria.
    message: 'A questão está quebrada: os dados da variante que o código da questão gerou são grandes demais para que as respostas possam ser corrigidas.',
    phase: 'render',
    technical_detail:
      `O snapshot da variante tem ${bytes} bytes em JSON, acima do limite de ${SNAPSHOT_MAX_BYTES} bytes ` +
      `(metade dos ${REQUEST_BODY_MAX_BYTES} bytes do corpo do pedido de correção).`,
  };
}
