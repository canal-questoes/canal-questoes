"""Textos fixos dos avisos à equipe, dos resumos e da prévia.
Os campos entram por `str.format`, com o fixo e cada valor escapados à parte.

Módulo próprio, à parte dos textos de `botmessages` e `deliverytexts`; nada daqui entra no menu dos bots.
Todos em português, funcionais, sem detalhe técnico (traceback, caminho, código de erro, URL, inglês).

- **Modelos**: os textos com valores são modelos de `str.format` (`{title}`, `{code}`, …), compostos por
  `render(template, formatter, **values)`, que escapa o fixo e cada valor separadamente no dialeto do
  formatador; os valores de texto (título, disciplina) chegam já redigidos e são truncados por
  quem compõe até caber; os números nunca são cortados.
- **Linhas**: cada modelo é uma sequência de linhas (`\\n`). As linhas opcionais têm constante própria
  (`OPENED_PENDING`, `GRADING_PENDING_LINE`, `PREVIEW_PARTS`, os avisos da prévia) e entram **antes da
  última linha** do modelo (a do comando), quando a condição vale. Exceção: a última linha de
  `ACCESS_RESTORED` (a orientação para os resumos perdidos) só sai com algum resumo de fechamento
  perdido no episódio.
- **Pares e taxas**: `pair(on_time, with_late)` = `"{a} no prazo · {b} com atrasadas"` (sempre os dois
  lados); `rate(x)` = `"{p}%"` ou `EMPTY`; `duration(ms)` = a duração legível ou `EMPTY`.
- **Comandos**: os comandos citados pelos textos são folhas do catálogo, listadas em `COMMAND_LEAVES`.

- **Formas**: `rate` com uma casa e vírgula (`"50,0%"`) e `duration` em milissegundos arredondados
  (`"10000 ms"`), as mesmas formas do texto de `cq reports show` (tests/cli/test_cli_reports.py).
"""

from __future__ import annotations

import string

from cq_server.ports.delivery import Formatted, MessageFormatter

# --------------------------------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------------------------------

UNTITLED = "questão sem título"
EMPTY = "—"

# As folhas do catálogo citadas pelos textos (o teste confere que cada uma existe no catálogo).
COMMAND_LEAVES = ("reports show", "reports resend", "dispatches show", "dispatches confirm", "events list",
                  "invites resend")

# --------------------------------------------------------------------------------------------------
# Prévia
# --------------------------------------------------------------------------------------------------

# O cabeçalho do disparo agendado (com abertura e fechamento no fuso do oferecimento).
PREVIEW_HEADER = (
    "Prévia do disparo: {title}\n"
    "Abertura: {opens_at}\n"
    "Fechamento: {closes_at}\n"
    "Alunos-alvo: {target}\n"
    "As mensagens a seguir mostram a questão como os alunos vão recebê-la; os botões não registram resposta.\n"
    "Para confirmar o disparo: cq dispatches confirm {code} {dispatch_id}"
)
# O cabeçalho do imediato sem fechamento informado: abre na confirmação.
PREVIEW_IMMEDIATE = (
    "Prévia do disparo: {title}\n"
    "Abre na confirmação e fecha {minutes} minutos depois.\n"
    "Alunos-alvo: {target}\n"
    "As mensagens a seguir mostram a questão como os alunos vão recebê-la; os botões não registram resposta.\n"
    "Para confirmar o disparo: cq dispatches confirm {code} {dispatch_id}"
)
# Só com 2 ou mais partes.
PREVIEW_PARTS = "A questão tem {parts} partes; a prévia mostra a primeira."
# Os avisos da prévia vindos do motor, pelo tipo, com texto fixo: a mensagem do motor nunca
# sai. Os do núcleo usam `dispatches.CORE_WARNING_MESSAGES`.
PREVIEW_ENGINE_WARNINGS: dict[str, str] = {
    "console_output": "A questão produziu mensagens de diagnóstico ao gerar a variante.",
    "missing_media": "A questão cita uma figura que não foi encontrada.",
    "media_error": "Uma figura da questão não pôde ser carregada.",
}
# A linha acrescentada ao pedido de campo da prévia, com a dica que o aluno vê no campo.
PREVIEW_FIELD_HINT = "Dica do campo para o aluno: {hint}"
# A confirmação do toque num botão da prévia (no máximo 200 caracteres).
PREVIEW_TAP = "Esta é uma prévia: o toque não registra resposta."
# A resposta a uma mensagem da prévia por "responder".
PREVIEW_REPLY = "Esta é uma prévia: a sua resposta não foi registrada."

# --------------------------------------------------------------------------------------------------
# Avisos de disparo
# --------------------------------------------------------------------------------------------------

# `reason` = `dispatches.REJECTION_MESSAGES[classe]`; `reference` = `qid@commit_short`.
REJECTED = (
    "Disparo rejeitado: {reference} — {title}\n"
    "Motivo: {reason}\n"
    "Para ver os detalhes: cq dispatches show {code} {dispatch_id}"
)
OPENED = (
    "Disparo aberto: {title}\n"
    "Alunos-alvo: {target}\n"
    "Entregues: {delivered}\n"
    "Não entregues: {not_delivered}\n"
    "Em falha de questão: {question_failed}\n"
    "Pendentes: {pending}\n"
    "Fechamento: {closes_at}\n"
    "Para acompanhar: cq dispatches show {code} {dispatch_id}"
)
# Só com pendentes.
OPENED_PENDING = "As tentativas de entrega às pendentes continuam até o fechamento."
CANCELLED = (
    "Disparo cancelado: {title}\n"
    "Motivo: {reason}\n"
    "Para ver os detalhes: cq dispatches show {code} {dispatch_id}"
)
# Só os cancelamentos sem ação do operador.
CANCEL_REASONS: dict[str, str] = {
    "expired": "o prazo de confirmação venceu sem que o disparo fosse confirmado.",
    "window_missed": "a abertura atrasou além da tolerância e a janela do disparo foi perdida.",
    "validation_overran": "a validação não terminou antes do horário de abertura.",
}
QUESTION_FAILED = (
    "Falha de questão na correção: {title}\n"
    "A questão falhou ao corrigir a resposta de um aluno. A resposta foi guardada e aparece no relatório.\n"
    "Para ver o relatório: cq reports show {code} {dispatch_id}"
)

# --------------------------------------------------------------------------------------------------
# Resumos de fechamento e de reenvio
# --------------------------------------------------------------------------------------------------

CLOSED_SUMMARY = (
    "Disparo fechado: {title}\n"
    "Janela: de {opens_at} a {closes_at}\n"
    "Alcance: {target} alunos-alvo, {delivered} entregues, {not_delivered} não entregues, "
    "{question_failed} em falha de questão\n"
    "Respostas: {answered}\n"
    "Corretas: {correct}\n"
    "Parcialmente corretas: {partially_correct}\n"
    "Não sei: {dont_know}\n"
    "Taxa de resposta: {response_rate}\n"
    "Tempo até responder, no prazo: mediana {median}, primeiro quartil {q1}, terceiro quartil {q3}\n"
    "Relatório completo: cq reports show {code} {dispatch_id}"
)
RESENT_SUMMARY = (
    "Resumo reenviado (versão {version}): {title}\n"
    "Janela: de {opens_at} a {closes_at}\n"
    "Alcance: {target} alunos-alvo, {delivered} entregues, {not_delivered} não entregues, "
    "{question_failed} em falha de questão\n"
    "Respostas: {answered}\n"
    "Corretas: {correct}\n"
    "Parcialmente corretas: {partially_correct}\n"
    "Não sei: {dont_know}\n"
    "Taxa de resposta: {response_rate}\n"
    "Tempo até responder, no prazo: mediana {median}, primeiro quartil {q1}, terceiro quartil {q3}\n"
    "Relatório completo: cq reports show {code} {dispatch_id}"
)
# Só com correções pendentes.
GRADING_PENDING_LINE = "Correções pendentes: {grading_pending}"

# --------------------------------------------------------------------------------------------------
# Bot sem acesso, no retorno
# --------------------------------------------------------------------------------------------------

ACCESS_RESTORED = (
    "O bot do oferecimento {course} voltou a funcionar.\n"
    "Período sem acesso: de {started_at} a {ended_at}\n"
    "Entregas não feitas por falta de acesso, nos disparos fechados no período: {not_delivered}\n"
    "Entregas ainda pendentes: {pending}\n"
    "Avisos à equipe perdidos no período: {lost_notices}\n"
    "Resumos de fechamento perdidos no período: {lost_summaries}\n"
    "Para reenviar um resumo perdido: cq reports resend {code} <disparo>; os disparos estão em "
    "cq events list --type notice.recorded"
)

# --------------------------------------------------------------------------------------------------
# Convites
# --------------------------------------------------------------------------------------------------

INVITE_FAILURES_BATCH = (
    "Falhas no envio de convites do oferecimento {course}\n"
    "No lote: {sent} enviados e {failed} com falha\n"
    "Para ver as falhas: cq events list --type student.invite_failed\n"
    "Para reenviar: cq invites resend {code} <apelidos>"
)
INVITE_FAILURES_WINDOW = (
    "Falhas no envio de convites do oferecimento {course}\n"
    "{failed} envios com falha entre {window_start} e {window_end}\n"
    "Para ver as falhas: cq events list --type student.invite_failed\n"
    "Para reenviar: cq invites resend {code} <apelidos>"
)

# --------------------------------------------------------------------------------------------------
# Resumo diário
# --------------------------------------------------------------------------------------------------

DAILY_DIGEST = (
    "Resumo do dia {date} — {course}\n"
    "Adesão: {linked} vinculados, {sending} em envio, {terms_presented} com os termos apresentados, "
    "{invite_valid} com convite válido, {send_failed} com falha no envio, {invite_lapsed} com convite "
    "vencido, {no_invite} sem convite\n"
    "Vínculos novos no dia: {new_links}\n"
    "Respostas atrasadas recebidas no dia: {late}\n"
    "Disparos abertos ou fechados no dia: {dispatches}"
)
# Uma linha por disparo aberto ou fechado no dia, depois do `DAILY_DIGEST` (o resumo longo sai em partes,
# com linhas inteiras de disparo).
DIGEST_DISPATCH_LINE = (
    "• {title}: respostas {answered}; corretas {correct}; parcialmente corretas {partially_correct}; "
    "não sei {dont_know}"
)


# --------------------------------------------------------------------------------------------------
# Funções
# --------------------------------------------------------------------------------------------------


def pair(on_time: int, with_late: int) -> str:
    """`"{on_time} no prazo · {with_late} com atrasadas"` (sempre os dois lados)."""
    return f"{on_time} no prazo · {with_late} com atrasadas"


def rate(value: float | None) -> str:
    """A fração de 0 a 1 como porcentagem (`"{p}%"`); `EMPTY` com `None`.

    Uma casa decimal com vírgula (`0.5` → `"50,0%"`), como o `cq reports show`.
    """
    if value is None:
        return EMPTY
    return f"{value * 100:.1f}".replace(".", ",") + "%"


def duration(ms: float | None) -> str:
    """Uma duração em milissegundos, legível em português; `EMPTY` com `None`.

    Os milissegundos arredondados com a unidade (`10000` → `"10000 ms"`), como o `cq reports show`; o
    valor negativo (resposta antes do `sent_at` gravado) sai com o sinal.
    """
    if ms is None:
        return EMPTY
    return f"{ms:.0f} ms"


# Leitor de `str.format` (sem estado; minúsculo, não é um texto): separa o fixo dos campos sem interpretar os valores.
_template_reader = string.Formatter()


def render(template: str, formatter: MessageFormatter, **values: str | int) -> Formatted:
    """Compõe `template` no dialeto de `formatter`, escapando o fixo e cada valor separadamente.

    O modelo é lido uma vez pelas regras de `str.format` (`{{` e `}}` viram chaves literais); cada campo
    é trocado pelo seu valor como texto, **sem** nova leitura: as chaves, a marcação, as entidades e os
    `/comandos` de um valor (um título, uma disciplina) ficam literais. O escape do dialeto (`&`, `<` e
    `>` no `rich` e no `html`; nada no `plain`) vale caractere a caractere, então escapar o texto composto
    é o mesmo que escapar o fixo e cada valor separadamente e juntar; o `Formatted` sai medido pelo
    `formatter.escape`, como as entregas compõem os textos de várias linhas (`deliveries.py`). Um campo sem valor
    lança `KeyError`, como o `str.format`; os campos são só nomes (sem atributo nem índice).
    """
    parts: list[str] = []
    for literal, field, spec, conversion in _template_reader.parse(template):
        parts.append(literal)
        if field is None:
            continue
        if not field.isidentifier():
            raise ValueError(f"campo inválido no modelo: {field!r}")
        value: object = values[field]
        if conversion:
            value = _template_reader.convert_field(value, conversion)
        parts.append(format(value, spec) if spec else str(value))
    return formatter.escape("".join(parts))
