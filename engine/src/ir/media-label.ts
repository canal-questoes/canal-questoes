// O nome de uma mídia nas mensagens de aviso missing_media e media_error.
// O filename da IR não tem teto (o caminho de uma estática e o nome passado ao file() vêm do conteúdo da
// questão, e o de outra origem tem até 200 caracteres; media.ts). Na mensagem, que é uma frase para o
// professor, o nome fica numa linha só e encurtado: sem controles nem quebras de linha, e, acima de
// LABEL_MAX caracteres, só o fim, que traz o nome do arquivo e a extensão, depois de "…". O filename da
// mídia na IR continua inteiro.

/** O tamanho máximo, em caracteres (pontos de código), do nome de uma mídia numa mensagem. */
export const MEDIA_LABEL_MAX = 80;

/** O nome da mídia para uma mensagem de aviso. */
export function mediaLabel(filename: string): string {
  const flat = filename.replace(/[\p{Cc}\p{Zl}\p{Zp}\s]+/gu, ' ').trim();
  const chars = [...flat];
  if (chars.length <= MEDIA_LABEL_MAX) return flat;
  return `…${chars.slice(chars.length - (MEDIA_LABEL_MAX - 1)).join('')}`;
}
