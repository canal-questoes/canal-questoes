"""Textos fixos dos avisos à equipe, dos resumos e da prévia (o resumo diário longo em várias mensagens,
os avisos do motor no cabeçalho da prévia e os avisos perdidos no episódio de bot sem acesso).

`cq_server.noticetexts`, módulo próprio (os textos de `botmessages` e `deliverytexts` e o menu de comandos
não mudam):
- o **conjunto fechado** de textos: exatamente as constantes esperadas (sem `DIGEST_MORE`)
  e `COMMAND_LEAVES`; `UNTITLED` e `EMPTY` literais; os motivos de `CANCEL_REASONS` (os 3
  cancelamentos sem ação do operador) e os tipos de `PREVIEW_ENGINE_WARNINGS` (os 3 avisos do motor,
  `dispatches.ENGINE_WARNING_KINDS`);
- cada texto em português, sem traceback, caminho, URL, código de erro nem inglês, fora dos comandos
  citados; `PREVIEW_TAP` com até 200 caracteres (o limite da confirmação do toque);
- os comandos citados nos textos são exatamente as 6 folhas de `COMMAND_LEAVES` (`reports show`, `reports
  resend`, `dispatches show`, `dispatches confirm`, `events list`, `invites resend`, inclusive a do aviso
  `bot_access_restored`), e cada folha existe no catálogo congelado; o `--type` citado é um tipo de
  evento do esquema congelado de `events.list`;
- `pair(18, 23) == "18 no prazo · 23 com atrasadas"` (sempre os dois lados); `rate(None) == "—"` e
  `rate(x)` em porcentagem; `duration(None) == "—"`;
- `render(template, formatter, **values)` escapa o fixo e cada valor separadamente nos três formatadores
  (`<`, `&`, tags, entidade já escrita, URL, `/comando` e chaves de `str.format` num título; números
  inteiros como valores), com o texto exato de `rich`, `html` e `plain`.

[I] Escolhas deste arquivo, sem regra escrita: o `Formatted` de `render` é o mesmo que
`formatter.escape` daria ao texto já composto (a marcação, o dialeto, a medida e o `fits`), como o código das entregas
compõe os textos de várias linhas (`deliveries.py:387`, `formatter.escape("\\n".join(lines))`): no `rich`,
sem `<p>` em volta e com as quebras de linha como vieram; a forma exata da porcentagem e da duração não é
fixada além do declarado (`rate(0.5)` é 50 seguido de `%`, com ou sem casas decimais).

As asserções sobre as constantes reais do esqueleto e sobre os textos dos outros módulos ficam na mesma função de
um caso de `pair` ou de `render`, que lança `NotImplementedError` no esqueleto; por isso, no
esqueleto, toda função deste arquivo fica vermelha por `NotImplementedError`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import pytest

from cq_server import botmessages, deliverytexts, dispatches, noticetexts
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter
from tests.helpers.contract import SCHEMAS, load_catalog, load_json

EMPTY = "—"
UNTITLED = "questão sem título"

# O conjunto fechado das constantes (e `COMMAND_LEAVES`).
NOTICE_CONSTANTS = {
    "PREVIEW_HEADER", "PREVIEW_PARTS", "PREVIEW_IMMEDIATE", "PREVIEW_ENGINE_WARNINGS", "PREVIEW_FIELD_HINT",
    "PREVIEW_TAP", "PREVIEW_REPLY",
    "REJECTED",
    "OPENED", "OPENED_PENDING",
    "CLOSED_SUMMARY", "RESENT_SUMMARY", "GRADING_PENDING_LINE",
    "CANCELLED", "CANCEL_REASONS",
    "QUESTION_FAILED",
    "ACCESS_RESTORED",
    "INVITE_FAILURES_BATCH", "INVITE_FAILURES_WINDOW",
    "DAILY_DIGEST", "DIGEST_DISPATCH_LINE",
    "UNTITLED", "EMPTY",
    "COMMAND_LEAVES",
}
# As 6 folhas do catálogo citadas pelos textos.
COMMAND_LEAVES = {"reports show", "reports resend", "dispatches show", "dispatches confirm", "events list",
                  "invites resend"}
# Os cancelamentos sem ação do operador.
CANCEL_REASON_KEYS = {"expired", "window_missed", "validation_overran"}

# Um comando citado: `cq <folha de duas palavras>` e os argumentos até o fim da linha ou um `;`.
_COMMAND = re.compile(r"\bcq ([a-z]+ [a-z]+)((?: [^\s;]+)*)")
_PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}")
# Detalhe técnico ou inglês que nenhum texto pode ter fora dos comandos.
_TECHNICAL = re.compile(
    r"traceback|exception|stack|https?://|www\.|\.py\b|\\|/|\b[a-z]+_[a-z_]+\b|\b[1-5][0-9]{2}\b|[{}<>]|"
    r"\b(HTTP|API|JSON|SQL|null|None|true|false|uuid|status|error|failed|failure|warning|report|notice|"
    r"preview|summary|digest|invite|pending|queued|sent|closed|opened|cancelled|rejected|retry|the|and|"
    r"of|dispatch|offering|engine|timeout|token)\b",
    re.IGNORECASE)

# Os textos das entregas (`deliverytexts`) e dos bots (o menu) que os relatórios não mudam.
QUESTION_FAILED_TEXT = ("Esta questão teve um problema e não conseguiu corrigir a sua resposta. A sua resposta "
                      "foi guardada.")
BOT_COMMANDS = [("start", "Começar ou usar um link de convite"), ("ajuda", "Ver como usar este bot")]

FORMATTERS: dict[str, Callable[[], Any]] = {
    "rich": TelegramRichFormatter, "html": TelegramHtmlFormatter, "plain": PlainFormatter}


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _texts() -> dict[str, str]:
    """Cada texto do módulo, pelo nome (os dicionários por chave, `NOME[chave]`)."""
    out: dict[str, str] = {}
    for name in sorted(NOTICE_CONSTANTS - {"COMMAND_LEAVES"}):
        value = getattr(noticetexts, name)
        if isinstance(value, dict):
            for key, text in value.items():
                out[f"{name}[{key}]"] = text
        else:
            out[name] = value
    return out


def _prose(text: str) -> str:
    """O texto sem os comandos citados e sem os campos variáveis."""
    return _PLACEHOLDER.sub("", _COMMAND.sub("", text))


def _event_types() -> set[str]:
    """Os tipos de evento do esquema congelado de `events.list` (o `const` de cada variante)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind = node.get("properties", {}).get("type")
            if isinstance(kind, dict) and "const" in kind:
                found.add(kind["const"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(load_json(str(SCHEMAS / "events.list.response.schema.json")))
    return found


def test_closed_set_portuguese_texts_and_cited_commands_with_pair() -> None:
    """Conjunto fechado e textos em português: as constantes
    públicas do módulo são exatamente as esperadas (com `COMMAND_LEAVES`, sem `DIGEST_MORE`);
    `UNTITLED` e `EMPTY` literais; `CANCEL_REASONS` com os 3 motivos automáticos e `PREVIEW_ENGINE_WARNINGS`
    com os 3 tipos do motor; cada texto não vazio, sem espaço nas pontas das linhas e, fora dos
    comandos, sem detalhe técnico nem inglês; `PREVIEW_TAP` com até 200 caracteres; os comandos citados
    são exatamente as 6 folhas de `COMMAND_LEAVES`, cada uma no catálogo congelado, e o `--type` citado é
    tipo de evento do esquema congelado; afirmadas junto de `pair` (sempre os dois lados)."""
    public = {name for name in vars(noticetexts) if name.isupper()}
    assert public == NOTICE_CONSTANTS, (sorted(public - NOTICE_CONSTANTS), sorted(NOTICE_CONSTANTS - public))
    assert not hasattr(noticetexts, "DIGEST_MORE")
    assert noticetexts.UNTITLED == UNTITLED
    assert noticetexts.EMPTY == EMPTY
    assert set(noticetexts.CANCEL_REASONS) == CANCEL_REASON_KEYS
    assert CANCEL_REASON_KEYS <= {reason.value for reason in dispatches.CancelReason}
    assert set(noticetexts.PREVIEW_ENGINE_WARNINGS) == set(dispatches.ENGINE_WARNING_KINDS)
    assert len(noticetexts.PREVIEW_ENGINE_WARNINGS) == 3

    texts = _texts()
    for name, text in texts.items():
        assert isinstance(text, str) and text.strip(), name
        assert all(line == line.strip() and line for line in text.split("\n")), name
        prose = _prose(text)
        assert not _TECHNICAL.search(prose), (name, _TECHNICAL.findall(prose))
    assert len(noticetexts.PREVIEW_TAP) <= 200 and _utf16(noticetexts.PREVIEW_TAP) <= 200

    assert set(noticetexts.COMMAND_LEAVES) == COMMAND_LEAVES and len(noticetexts.COMMAND_LEAVES) == 6
    catalog_leaves = {op["cli"] for op in load_catalog()["operations"]}
    assert COMMAND_LEAVES <= catalog_leaves, sorted(COMMAND_LEAVES - catalog_leaves)
    cited = {leaf for text in texts.values() for leaf, _ in _COMMAND.findall(text)}
    assert cited == COMMAND_LEAVES, (sorted(cited - COMMAND_LEAVES), sorted(COMMAND_LEAVES - cited))
    assert all(text.count("cq ") == len(_COMMAND.findall(text)) for text in texts.values())
    event_types = _event_types()
    filters = {match for text in texts.values() for match in re.findall(r"--type ([a-z_.]+)", text)}
    assert filters == {"notice.recorded", "student.invite_failed"}
    assert filters <= event_types, sorted(filters - event_types)

    assert noticetexts.pair(18, 23) == "18 no prazo · 23 com atrasadas"
    assert noticetexts.pair(0, 0) == "0 no prazo · 0 com atrasadas"
    assert noticetexts.pair(5, 5) == "5 no prazo · 5 com atrasadas"
    assert noticetexts.pair(1, 1024) == "1 no prazo · 1024 com atrasadas"


def test_rate_and_duration_use_the_empty_mark_without_value() -> None:
    """A taxa com denominador zero sai vazia (`rate(x)` =
    `"{p}%"` ou `EMPTY`; `duration(ms)` legível ou `EMPTY`): `rate(None)` e `duration(None)` são `"—"`;
    `rate(0.5)` é 50 seguido de `%` (com ou sem casas decimais), `rate(1.0)` 100 e `rate(0.0)` 0; uma
    duração com valor (inclusive 0) nunca é a marca vazia e traz o número."""
    assert noticetexts.rate(None) == EMPTY
    assert re.fullmatch(r"50(,0+)?%", noticetexts.rate(0.5)), noticetexts.rate(0.5)
    assert re.fullmatch(r"100(,0+)?%", noticetexts.rate(1.0)), noticetexts.rate(1.0)
    assert re.fullmatch(r"0(,0+)?%", noticetexts.rate(0.0)), noticetexts.rate(0.0)
    assert noticetexts.duration(None) == EMPTY
    for ms in (0, 1500, 90_000, 3_600_000):
        text = noticetexts.duration(ms)
        assert isinstance(text, str) and text and text != EMPTY, (ms, text)
        assert re.search(r"[0-9]", text) and "None" not in text, (ms, text)


# Um modelo de teste com `<`, `&` e uma entidade no fixo, um título hostil e um número.
TEMPLATE = "Fixo <i>&amp; {title}\nAlunos: {target}\nComando: cq reports show {code} {dispatch_id}"
TITLE = '<b>Soma</b> & "x" https://exemplo.invalid/a?b=1&c=2 /start {code} &lt;'
CODE = "mc102-2031s1"
DISPATCH_ID = "0d6c2f4e-1b7a-4c35-9e0a-2f1d3c4b5a69"
RENDERED_PLAIN = (
    'Fixo <i>&amp; <b>Soma</b> & "x" https://exemplo.invalid/a?b=1&c=2 /start {code} &lt;\n'
    "Alunos: 12\n"
    f"Comando: cq reports show {CODE} {DISPATCH_ID}")
RENDERED_MARKUP = (
    'Fixo &lt;i&gt;&amp;amp; &lt;b&gt;Soma&lt;/b&gt; &amp; "x" https://exemplo.invalid/a?b=1&amp;c=2 '
    "/start {code} &amp;lt;\n"
    "Alunos: 12\n"
    f"Comando: cq reports show {CODE} {DISPATCH_ID}")
EXPECTED_MARKUP = {"rich": RENDERED_MARKUP, "html": RENDERED_MARKUP, "plain": RENDERED_PLAIN}


@pytest.mark.parametrize("dialect", ["rich", "html", "plain"])
def test_render_escapes_the_fixed_text_and_each_value(dialect: str) -> None:
    """Os campos variáveis escapados antes da composição
    (`render(template, formatter, **values)` escapa o fixo e cada valor separadamente): no `rich` e no
    `html`, `<`, `>` e `&` do fixo e de cada valor viram entidade (inclusive a entidade já escrita, que não
    é mantida como marcação), as aspas, a URL e o `/comando` ficam literais, e as chaves de `str.format`
    dentro de um valor não são substituídas; no `plain`, nada é escapado; o número entra como texto; o
    `Formatted` é o mesmo que `formatter.escape` daria ao texto composto (dialeto, medida e `fits`)."""
    formatter = FORMATTERS[dialect]()
    result = noticetexts.render(TEMPLATE, formatter, title=TITLE, target=12, code=CODE,
                                dispatch_id=DISPATCH_ID)
    assert result.markup == EXPECTED_MARKUP[dialect]
    assert result.text_format == dialect
    assert result == formatter.escape(RENDERED_PLAIN)
    assert result.fits is True


@pytest.mark.parametrize("dialect", ["rich", "html", "plain"])
def test_render_real_template_keeps_bot_and_delivery_texts_untouched(dialect: str) -> None:
    """Os textos de `botmessages` e `deliverytexts` e o menu não mudam:
    o modelo real `INVITE_FAILURES_BATCH`, com a disciplina hostil, sai com o fixo escapado
    (`<apelidos>` no comando) e os números; na mesma função, nenhum nome destes avisos existe em `botmessages`
    nem em `deliverytexts` (salvo o `QUESTION_FAILED` das entregas, que continua o texto ao aluno, distinto do
    aviso à equipe), `botmessages.compose` não conhece os nomes destes avisos e o menu continua `start` e
    `ajuda`."""
    r6_names = NOTICE_CONSTANTS - {"QUESTION_FAILED"}
    for name in sorted(r6_names):
        assert not hasattr(botmessages, name), name
        assert not hasattr(deliverytexts, name), name
    for name in sorted(NOTICE_CONSTANTS):
        with pytest.raises(KeyError):
            botmessages.compose(name, course="Cálculo I", term="2031s1")
    assert deliverytexts.QUESTION_FAILED == QUESTION_FAILED_TEXT
    assert noticetexts.QUESTION_FAILED != deliverytexts.QUESTION_FAILED
    assert [(c.command, c.description) for c in botmessages.COMMANDS] == BOT_COMMANDS

    course = "Cálculo <I> & Cia"
    plain = ("Falhas no envio de convites do oferecimento Cálculo <I> & Cia\n"
             "No lote: 7 enviados e 2 com falha\n"
             "Para ver as falhas: cq events list --type student.invite_failed\n"
             f"Para reenviar: cq invites resend {CODE} <apelidos>")
    markup = ("Falhas no envio de convites do oferecimento Cálculo &lt;I&gt; &amp; Cia\n"
              "No lote: 7 enviados e 2 com falha\n"
              "Para ver as falhas: cq events list --type student.invite_failed\n"
              f"Para reenviar: cq invites resend {CODE} &lt;apelidos&gt;")
    formatter = FORMATTERS[dialect]()
    result = noticetexts.render(noticetexts.INVITE_FAILURES_BATCH, formatter, course=course, sent=7,
                                failed=2, code=CODE)
    assert result.markup == (plain if dialect == "plain" else markup)
    assert result == formatter.escape(plain)
