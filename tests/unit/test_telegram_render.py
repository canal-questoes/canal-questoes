"""Renderização IR → mensagem nos três dialetos.


Os formatadores de `cq_server.telegramrender` (núcleo, puros): `TelegramRichFormatter` (`rich`, o padrão
do Telegram), `TelegramHtmlFormatter` (`html`, o HTML simples do Telegram) e `PlainFormatter` (`plain`, o console).

**Saídas de referência versionadas** em `tests/fixtures/render/`: para cada construção
do subconjunto canônico (`paragrafo`, `listas`, `tabela`, `codigo` = `pre`/`code`, `formula-linha`,
`formula-bloco`, `midia` = `cq-media`, `campo` = `cq-field`), a entrada `<construção>.ir.html` e a saída
`<construção>.<modo>.txt` nos três modos. Cada arquivo termina com **uma** quebra de linha, que não faz
parte do conteúdo (os arquivos são LF, `.gitattributes`). As saídas seguem a tabela de conversão
(lista de permissão sobre o HTML canônico), com estas escolhas [I] que os arquivos fixam:
- blocos (`p`, listas, tabela, `pre`, fórmula em bloco) separados por uma linha em branco (`\\n\\n`) no
  HTML simples e no texto; no Rich, as tags de bloco lado a lado, sem espaço;
- listas em linhas com `• ` (não ordenada) ou `k. ` (ordenada), recuo de 2 espaços por nível;
- tabela: no Rich, `table/tr/th/td` com `colspan`/`rowspan` (sem `thead`/`tbody`, fora da lista do
  Rich); no HTML simples, `<pre>` com as células separadas por ` | ` e uma linha por `tr`; no texto, o
  mesmo sem o `pre`;
- `sup`/`sub` como `^(…)`/`_(…)` fora do Rich;
- **marcadores**: `media_labels` dá o rótulo de cada marcador da IR pela referência (`mN` da
  `cq-media`; o `name` do `cq-field`), em texto puro; a imagem vira `[<rótulo>]` seguido de um espaço e do
  `alt` (sem `alt`, só `[<rótulo>]`), e o campo vira `[<rótulo>]` em negrito (`<b>`) nos dois modos do
  Telegram; rótulo e `alt` escapados.

O que mais se confere: só as tags e entidades aceitas pelo modo (a saída reinjetada no validador da
`DispatchExtension`, `tests/helpers/bot_api_dispatch.parse_markup`); o resto (tags e atributos fora do subconjunto)
desembrulhado e escapado; as regras do HTML simples (`b/i/u/s` nunca contêm `code`/`pre`; `blockquote`
não se aninha); `escape`; a medição (texto simples e console em unidades UTF-16 depois do
parsing, ≤ 4096; Rich em bytes UTF-8 do texto sem as tags, com a fonte LaTeX, ≤ 32 768, e ≤ 500 blocos,
≤ 16 níveis, ≤ 20 colunas) com `fits=False` acima; `compose` e `plain`.

**Na mensagem da questão e no feedback**, pela Bot API falsa com a extensão do disparo (worker real, motor
falso, obtenção falsa):
- todo campo de `fields[]` apresentado mesmo sem marcador no enunciado, e as restrições mínima e máxima
  da múltipla quando a IR as traz;
- o eco da resposta digitada truncado em 200 caracteres com "…" **antes** do escape;
- o feedback que não cabe vira edição sem o bloco e um `notice` curto em resposta à original,
  no HTML simples, com o limite calibrado por um disparo de medição.

No esqueleto, os métodos dos formatadores lançam `NotImplementedError`, e o preparo de disparo não tem
rota (404): todo teste deste arquivo falha por um desses motivos.
"""

from __future__ import annotations

import dataclasses
import itertools
import pathlib
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest

from cq_server.adapters.engine import HttpQuestionEngine
from cq_server.config import DeliverySettings, EngineSettings
from cq_server.deliverytexts import VERDICT_INCORRECT
from cq_server.telegramrender import (
    CAPTION_MAX_LENGTH,
    ECHO_MAX_LENGTH,
    PlainFormatter,
    TelegramHtmlFormatter,
    TelegramRichFormatter,
)
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    confirm_dispatch,
    create_offering,
    get_dispatch,
    prepare_dispatch,
    register_bot,
    register_person,
)
from tests.helpers.bot_api import TelegramUser, _utf16_len, make_user
from tests.helpers.bot_api_dispatch import (
    HTML_TAGS,
    RICH_BLOCKS_MAX,
    RICH_COLUMNS_MAX,
    RICH_DEPTH_MAX,
    RICH_TAGS,
    RichMessage,
    parse_markup,
)
from tests.helpers.clock import FakeClock, iso
from tests.helpers.engine import (
    FakeOption,
    FakeQuestion,
    build_variant,
    multiple_choice_field,
    number_field,
    text_field,
)

RENDER = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "render"
CONSTRUCTIONS = ("paragrafo", "listas", "tabela", "codigo", "formula-linha", "formula-bloco", "midia",
                 "campo")
MODES = ("rich", "html", "plain")
FORMATTERS: dict[str, Callable[[], Any]] = {
    "rich": TelegramRichFormatter, "html": TelegramHtmlFormatter, "plain": PlainFormatter}
# O dublê do Telegram lê o Rich como `rich` e o HTML simples como `html` (`parse_markup`).
VALIDATOR_MODE = {"rich": "rich", "html": "html"}
# Os rótulos dos marcadores das saídas de referência (texto puro; o formatador escapa).
MEDIA_LABELS = {"m1": "Figura 1", "m2": "Figura 2", "velocidade": "campo 1: Velocidade",
                "limite": "campo 2: x < 5 & y"}
# O formatador só produz estas entidades (válidas nos dois modos).
ENTITIES = {"lt", "gt", "amp", "quot"}
ENTITY = re.compile(r"&(#?[A-Za-z0-9]+);")


def _read(name: str) -> str:
    """O conteúdo de um arquivo de referência, sem a quebra de linha final (obrigatória)."""
    data = (RENDER / name).read_bytes()
    assert b"\r" not in data, f"{name}: fim de linha que não é LF"
    text = data.decode("utf-8")
    assert text.endswith("\n") and not text.endswith("\n\n"), f"{name}: sem a quebra de linha final"
    return text[:-1]


def _expected_length(mode: str, markup: str) -> int:
    """A medida do tamanho como o validador do dublê lê a saída: HTML simples e texto em unidades
    UTF-16 depois do parsing; Rich em bytes UTF-8 do texto sem as tags, com a fonte LaTeX."""
    if mode == "plain":
        return _utf16_len(markup)
    plain = parse_markup(markup, VALIDATOR_MODE[mode]).plain
    return len(plain.encode("utf-8")) if mode == "rich" else _utf16_len(plain)


def _assert_valid_markup(mode: str, markup: str) -> Any:
    """A saída reinjetada no validador da `DispatchExtension`: sem 400 `can't parse entities`, sem desvio
    de protocolo (atributo fora da lista, aninhamento que o Telegram só conserta), só as tags do modo e
    só as entidades `&lt; &gt; &amp; &quot;`."""
    parsed = parse_markup(markup, VALIDATOR_MODE[mode])
    assert parsed.error is None, f"{mode}: {parsed.error}"
    assert parsed.problems == [], f"{mode}: {parsed.problems}"
    tags = {m.group(1).lower() for m in re.finditer(r"</?([A-Za-z][A-Za-z0-9-]*)", markup)}
    assert tags <= (RICH_TAGS if mode == "rich" else HTML_TAGS)
    assert {m.group(1) for m in ENTITY.finditer(markup)} <= ENTITIES
    return parsed


# ---------------------------------------------------------------------------------------------
# Saídas de referência
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("construction", CONSTRUCTIONS)
def test_reference_output_for_each_construction_and_mode(construction: str, mode: str) -> None:
    """Para cada construção do subconjunto canônico
    e cada formatador, `from_ir(<construção>.ir.html, media_labels=…)` devolve exatamente a saída de
    referência versionada `<construção>.<modo>.txt`, no dialeto do formatador (`text_format` = o do
    formatador: `rich`, `html` ou `plain`), com `fits` verdadeiro e `length` medido como o canal conta;
    nos dois modos do Telegram, a saída passa pelo validador do dublê só com as tags e entidades do
    modo. O conjunto de arquivos cobre as 8 construções nos 3 modos, sem sobra."""
    expected_files = {f"{c}.ir.html" for c in CONSTRUCTIONS} | {
        f"{c}.{m}.txt" for c in CONSTRUCTIONS for m in MODES}
    assert {p.name for p in RENDER.iterdir()} == expected_files

    formatter = FORMATTERS[mode]()
    assert (formatter.text_format, formatter.max_length) == (
        mode, {"rich": 32768, "html": 4096, "plain": 4096}[mode])
    result = formatter.from_ir(_read(f"{construction}.ir.html"), media_labels=MEDIA_LABELS)

    expected = _read(f"{construction}.{mode}.txt")
    assert result.markup == expected
    assert (result.text_format, result.fits) == (mode, True)
    assert result.length == _expected_length(mode, expected)
    if mode != "plain":
        parsed = _assert_valid_markup(mode, result.markup)
        if mode == "rich":
            assert parsed.blocks <= RICH_BLOCKS_MAX and parsed.depth <= RICH_DEPTH_MAX
            assert parsed.columns <= RICH_COLUMNS_MAX


@pytest.mark.parametrize("construction", ["paragrafo", "listas", "tabela", "codigo", "formula-linha",
                                          "formula-bloco"])
def test_plain_of_the_ir_is_the_console_text(construction: str) -> None:
    """`plain(canonical_html)` (o texto da prévia, dos rótulos,
    dos botões e das legendas) é o mesmo nos três formatadores e igual à saída de referência do
    console para as construções sem marcador; entidades decodificadas, nenhuma tag."""
    ir = _read(f"{construction}.ir.html")
    expected = _read(f"{construction}.plain.txt")
    for mode in MODES:
        assert FORMATTERS[mode]().plain(ir) == expected, mode
    samples = {
        "<strong>Velocidade</strong> (m/s)": "Velocidade (m/s)",
        "Área &lt; 3 &amp; <em>b</em>": "Área < 3 & b",
        "x<sup>2</sup>": "x^(2)",
        '<cq-math display="inline">x^2</cq-math>': "$x^2$",
    }
    for html, text in samples.items():
        assert [FORMATTERS[mode]().plain(html) for mode in MODES] == [text] * 3, html


# ---------------------------------------------------------------------------------------------
# Lista de permissão e regras de cada modo
# ---------------------------------------------------------------------------------------------


def test_tags_and_attributes_outside_the_subset_are_unwrapped_and_text_escaped() -> None:
    """Tag ou atributo de fora (qualquer outra tag ou atributo: desembrulhada; o texto fica,
    escapado): numa IR com tags e atributos fora do subconjunto (`span`, `a href`, `div`, `font`,
    `class` num `p`) e com `<`, `>` e `&` no texto, a saída dos dois modos do Telegram só tem as tags do
    modo, nenhum `href` nem atributo de fora, só as entidades `&lt; &gt; &amp; &quot;`, e o texto depois
    do parsing é o da IR; no texto puro, o mesmo texto, sem tags."""
    ir = ('<p class="lead">Veja <span class="x">isto</span> e <a href="https://exemplo.com/x">o link</a>: '
          '5 &gt; 3 &amp;&amp; &lt;b&gt;fim&lt;/b&gt;</p><div>bloco <font color="red">solto</font></div>')
    for mode in ("rich", "html"):
        result = FORMATTERS[mode]().from_ir(ir, media_labels={})
        parsed = _assert_valid_markup(mode, result.markup)
        for forbidden in ("href", "exemplo.com", "class=", "color=", "<span", "<a ", "<div", "<font"):
            assert forbidden not in result.markup, (mode, forbidden)
        assert "Veja isto e o link: 5 > 3 && <b>fim</b>" in parsed.plain
        assert "bloco solto" in parsed.plain
        assert result.fits is True
    plain = PlainFormatter().from_ir(ir, media_labels={}).markup
    assert "Veja isto e o link: 5 > 3 && <b>fim</b>" in plain and "bloco solto" in plain
    assert "href" not in plain and "<span" not in plain and "<div" not in plain


def test_simple_html_closes_marks_around_code_and_flattens_quotes() -> None:
    """As regras do HTML simples: no HTML simples,
    `b/i/u/s` nunca contêm `code` nem `pre` (as marcas fecham antes e reabrem depois, sem marca vazia),
    a fórmula em linha sai como `<code>` e `blockquote` não se aninha; o validador do dublê não acusa
    nada. No Rich, as mesmas construções ficam aninhadas como na IR."""
    html = TelegramHtmlFormatter()
    rich = TelegramRichFormatter()
    nested = "<p><em>a <strong>b <code>c</code> d</strong> e</em></p>"
    assert html.from_ir(nested, media_labels={}).markup == (
        "<i>a <b>b </b></i><code>c</code><i><b> d</b> e</i>")
    assert rich.from_ir(nested, media_labels={}).markup == "<p><i>a <b>b <code>c</code> d</b> e</i></p>"
    formula = '<p><strong>Área <cq-math display="inline">x^2</cq-math></strong></p>'
    assert html.from_ir(formula, media_labels={}).markup == "<b>Área </b><code>x^2</code>"
    assert rich.from_ir(formula, media_labels={}).markup == "<p><b>Área <tg-math>x^2</tg-math></b></p>"
    for ir in (nested, formula):
        _assert_valid_markup("html", html.from_ir(ir, media_labels={}).markup)
        _assert_valid_markup("rich", rich.from_ir(ir, media_labels={}).markup)

    quotes = "<blockquote><p>fora</p><blockquote><p>dentro</p></blockquote></blockquote>"
    flat = html.from_ir(quotes, media_labels={})
    parsed = _assert_valid_markup("html", flat.markup)
    assert "fora" in parsed.plain and "dentro" in parsed.plain
    assert flat.markup.count("<blockquote>") >= 1
    kept = rich.from_ir(quotes, media_labels={})
    assert kept.markup == "<blockquote><p>fora</p><blockquote><p>dentro</p></blockquote></blockquote>"


ESCAPE_CASES = [
    pytest.param("<b>negrito</b>", "&lt;b&gt;negrito&lt;/b&gt;", id="tag"),
    pytest.param("a & b", "a &amp; b", id="e-comercial"),
    pytest.param("</code>", "&lt;/code&gt;", id="fecha-code"),
    pytest.param("https://exemplo.com/?a=1&b=2", "https://exemplo.com/?a=1&amp;b=2", id="url"),
    pytest.param("/comando", "/comando", id="comando"),
    pytest.param("&amp; &lt;", "&amp;amp; &amp;lt;", id="entidade-literal"),
    pytest.param("😀 x > 3", "😀 x &gt; 3", id="nao-bmp"),
]


@pytest.mark.parametrize("text, escaped", ESCAPE_CASES)
def test_escape_makes_any_text_literal_in_each_mode(text: str, escaped: str) -> None:
    """`escape(text)` escreve o texto de fora da IR (eco, textos fixos,
    rótulos, sufixos) como literal: nos dois modos do Telegram, `<`, `>` e `&` viram entidade, nenhuma
    tag nem link sai, e o texto depois do parsing é exatamente o original; `/comando` e URLs ficam
    como texto (a detecção é desligada pelo `skip_entity_detection` do Rich; no HTML simples ela não se
    desliga, e esse risco fica aceito); no texto puro, o original sem mudança. `length` e `fits` pela
    medida do modo."""
    for mode in ("rich", "html"):
        result = FORMATTERS[mode]().escape(text)
        assert result.markup == escaped, mode
        parsed = _assert_valid_markup(mode, result.markup)
        assert parsed.plain == text
        assert "<" not in result.markup
        assert (result.text_format, result.fits) == (mode, True)
        assert result.length == _expected_length(mode, result.markup)
    plain = PlainFormatter().escape(text)
    assert (plain.markup, plain.text_format, plain.fits, plain.length) == (
        text, "plain", True, _utf16_len(text))


# ---------------------------------------------------------------------------------------------
# Medição dos limites
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["html", "plain"])
def test_simple_text_limit_is_4096_utf16_units_after_parsing(mode: str) -> None:
    """no HTML simples e no texto puro, o limite é 4096 unidades
    UTF-16 do texto depois do parsing (tags fora, entidades decodificadas): 2048 emojis (4096 unidades)
    cabem e um caractere a mais não (`fits=False`, `length` 4097); 4096 `&` cabem no HTML simples mesmo
    com a marcação de 20 480 caracteres. O `max_length` é 4096."""
    formatter = FORMATTERS[mode]()
    assert formatter.max_length == 4096
    at_limit = formatter.escape("😀" * 2048)
    assert (at_limit.length, at_limit.fits) == (4096, True)
    over = formatter.escape("😀" * 2048 + "a")
    assert (over.length, over.fits) == (4097, False)
    amps = formatter.escape("&" * 4096)
    assert (amps.length, amps.fits) == (4096, True)
    assert amps.markup == ("&amp;" * 4096 if mode == "html" else "&" * 4096)
    assert formatter.escape("&" * 4097).fits is False
    big = formatter.from_ir("<p>" + "k" * 4096 + "</p>", media_labels={})
    assert (big.length, big.fits) == (4096, True)
    bigger = formatter.from_ir("<p>" + "k" * 4097 + "</p>", media_labels={})
    assert (bigger.length, bigger.fits) == (4097, False)


def test_rich_limit_counts_utf8_bytes_and_the_latex_source() -> None:
    """no Rich, o texto é medido em **bytes UTF-8** do texto sem
    as tags (entidades decodificadas), **com** a fonte LaTeX das fórmulas, até 32 768: 16 384 `é`
    (32 768 bytes) cabem, 16 385 não (32 770 bytes, embora sejam só 16 385 unidades UTF-16); 32 760
    letras mais uma fórmula de 8 caracteres cabem (32 768), e com 9 não. O `max_length` é 32 768; a
    legenda tem teto de 1024 e o eco, de 200 (`CAPTION_MAX_LENGTH`, `ECHO_MAX_LENGTH`)."""
    rich = TelegramRichFormatter()
    assert rich.max_length == 32768
    assert (CAPTION_MAX_LENGTH, ECHO_MAX_LENGTH) == (1024, 200)
    at_limit = rich.escape("é" * 16384)
    assert (at_limit.length, at_limit.fits) == (32768, True)
    over = rich.escape("é" * 16385)
    assert (over.length, over.fits) == (32770, False)
    emoji = rich.escape("😀" * 8192)
    assert (emoji.length, emoji.fits) == (32768, True)
    with_formula = rich.from_ir('<p>' + "a" * 32760 + '<cq-math display="inline">abcdefgh</cq-math></p>',
                                media_labels={})
    assert (with_formula.length, with_formula.fits) == (32768, True)
    assert with_formula.markup == "<p>" + "a" * 32760 + "<tg-math>abcdefgh</tg-math></p>"
    longer = rich.from_ir('<p>' + "a" * 32760 + '<cq-math display="inline">abcdefghi</cq-math></p>',
                          media_labels={})
    assert (longer.length, longer.fits) == (32769, False)


def test_rich_limits_blocks_depth_and_columns() -> None:
    """No Rich, 500 blocos cabem e 501 não; 16 níveis
    de aninhamento cabem e 17 não; uma tabela de 20 colunas sai como `<table>` e uma de 21 sai como
    `<pre>` com as células separadas por ` | ` (e cabe). A contagem é a do validador do dublê. No HTML
    simples, 501 parágrafos curtos cabem (não há teto de blocos)."""
    rich = TelegramRichFormatter()
    five_hundred = rich.from_ir("<p>x</p>" * 500, media_labels={})
    assert five_hundred.fits is True
    assert parse_markup(five_hundred.markup, "rich").blocks == 500
    assert rich.from_ir("<p>x</p>" * 501, media_labels={}).fits is False
    assert TelegramHtmlFormatter().from_ir("<p>x</p>" * 501, media_labels={}).fits is True

    def quotes(levels: int) -> str:
        return "<blockquote>" * levels + "<p>x</p>" + "</blockquote>" * levels

    sixteen = rich.from_ir(quotes(15), media_labels={})
    assert (sixteen.fits, parse_markup(sixteen.markup, "rich").depth) == (True, 16)
    seventeen = rich.from_ir(quotes(16), media_labels={})
    assert (seventeen.fits, parse_markup(seventeen.markup, "rich").depth) == (False, 17)

    def table(columns: int) -> str:
        return "<table><tr>" + "".join(f"<td>c{i}</td>" for i in range(1, columns + 1)) + "</tr></table>"

    twenty = rich.from_ir(table(20), media_labels={})
    assert twenty.markup == table(20) and twenty.fits is True
    wide = rich.from_ir(table(21), media_labels={})
    assert wide.markup == "<pre>" + " | ".join(f"c{i}" for i in range(1, 22)) + "</pre>"
    assert wide.fits is True
    _assert_valid_markup("rich", wide.markup)


@pytest.mark.parametrize("mode", MODES)
def test_compose_keeps_the_blocks_in_order_and_measures_the_whole(mode: str) -> None:
    """`compose(blocks)` junta blocos já escritos no dialeto, na ordem, sem
    mudar nenhum, e mede o todo como o canal conta; um bloco só volta com a mesma medida. É a conta
    que decide o feedback que não cabe: o enunciado que coube na abertura mais um feedback com
    eco de 200 caracteres passa do limite (`fits=False`), e o mesmo enunciado com o veredito curto
    cabe."""
    formatter = FORMATTERS[mode]()
    first = formatter.escape("Primeira parte")
    second = formatter.escape("Segunda <parte> & fim")
    whole = formatter.compose([first, second])
    assert whole.text_format == mode and whole.fits is True
    assert first.markup in whole.markup and second.markup in whole.markup
    assert whole.markup.index(first.markup) < whole.markup.index(second.markup)
    assert whole.length >= first.length + second.length
    assert whole.length == _expected_length(mode, whole.markup)
    if mode != "plain":
        _assert_valid_markup(mode, whole.markup)
    single = formatter.compose([first])
    assert first.markup in single.markup and single.length == first.length

    limit = formatter.max_length
    statement = formatter.from_ir("<p>" + "k" * (limit - 96) + "</p>", media_labels={})
    assert statement.fits is True
    feedback = formatter.escape("Sua resposta: " + "y" * ECHO_MAX_LENGTH + "… · " + VERDICT_INCORRECT)
    assert feedback.fits is True
    too_long = formatter.compose([statement, feedback])
    assert too_long.fits is False and too_long.length > limit
    notice = formatter.compose([statement, formatter.escape(VERDICT_INCORRECT)])
    assert notice.fits is True


# ---------------------------------------------------------------------------------------------
# A mensagem da questão e o feedback pela Bot API falsa
# ---------------------------------------------------------------------------------------------

REPO = "https://repos.cq.test/render/questoes.git"
WAIT = 30.0
_codes = itertools.count(1)
_commits = itertools.count(1)
_students = itertools.count(1)


def _commit() -> str:
    """Um commit SHA-1 novo (cada questão programada tem o seu par `(repo, commit)`)."""
    return f"{0xC0DE0000 + next(_commits):040x}"


@dataclasses.dataclass(frozen=True)
class _Class:
    """Um oferecimento ativo com bot na Bot API falsa e uma aluna vinculada (matrícula `student`
    `active` e endereço `active` no bot)."""

    code: str
    bot: int
    user: TelegramUser

    @property
    def chat(self) -> int:
        return self.user.user_id


def _class_with_student(client: Any, sql: psycopg.Connection, rich: Any, clock: FakeClock) -> _Class:
    """O oferecimento e o bot pela API dos oferecimentos; a pessoa pela API base; a matrícula e o endereço por SQL
    (o vínculo do aluno, já testado em outro lugar: matrícula `active` com o termo aceito e endereço `active`). O chat
    fica conhecido do dublê por um `/ajuda` (essa resposta sai em texto puro)."""
    offering = create_offering(client, ADMIN_TOKEN, code=f"render-{next(_codes)}")
    code = offering["code"]
    bot = rich.add_bot()
    register_bot(client, ADMIN_TOKEN, code, rich.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, code)
    index = next(_students)
    person = register_person(client, ADMIN_TOKEN, name=f"Aluna Neutra {index}")
    user = make_user(700 + index)
    now = clock.now()
    person_row = sql.execute("SELECT id FROM people WHERE pseudo_id = %s",
                             (uuid.UUID(person["pseudo_id"]),)).fetchone()[0]
    offering_row = sql.execute("SELECT id FROM offerings WHERE code = %s", (code,)).fetchone()[0]
    bot_row = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()[0]
    sql.execute(
        "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, "
        "terms_version, terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s)",
        (person_row, offering_row, now, now, now))
    sql.execute(
        "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
        "changed_at) VALUES (%s, %s, 'telegram', %s, 'active', %s, %s)",
        (person_row, bot_row, user.user_id, now, now))
    rich.push_text(bot, user, "/ajuda", bot_command=True)
    return _Class(code=code, bot=bot, user=user)


def _start_worker(bot_worker_thread: Any, engine_fake: Any, repo_fetcher: Any,
                  delivery: DeliverySettings) -> HttpQuestionEngine:
    """O worker real com bots, o motor HTTP apontado para o motor falso e a obtenção falsa."""
    engine = HttpQuestionEngine(EngineSettings(engine_url=engine_fake.base_url, timeout_seconds=10.0))
    bot_worker_thread(engine=engine, fetcher=repo_fetcher, delivery=delivery)
    return engine


def _validated_and_confirmed(client: Any, rich: Any, klass: _Class, dispatch_id: str) -> None:
    """Espera a validação terminar (`scheduled`, sem confirmação) e confirma (abre agora)."""
    def settled() -> dict | None:
        body = get_dispatch(client, ADMIN_TOKEN, klass.code, dispatch_id)
        return body if body["state"] != "validating" else None

    body = rich.wait_for(settled, timeout=WAIT)
    assert (body["state"], body["confirmed"]) == ("scheduled", False), body.get("rejection")
    confirm_dispatch(client, ADMIN_TOKEN, klass.code, dispatch_id)


def _marker(rich: Any) -> int:
    """O `seq` da última chamada recebida pelo dublê (as mensagens depois dele são novas)."""
    return max((c.seq for c in rich.calls()), default=0)


def _question(rich: Any, klass: _Class, after: int) -> RichMessage:
    """A mensagem da questão: formatada (Rich ou HTML simples), sem pedido de campo e sem ser resposta
    (os pedidos de campo, avisos e mensagens de reserva respondem à questão)."""
    def find() -> RichMessage | None:
        for message in rich.messages(klass.bot, klass.chat):
            if (message.call_seq > after and message.text_format in ("rich_html", "html")
                    and not message.force_reply and message.reply_to_message_id is None):
                return message
        return None

    return rich.wait_for(find, timeout=WAIT)


def _prompt(rich: Any, klass: _Class, question: RichMessage) -> RichMessage:
    """O pedido do campo digitável (`ForceReply`), em resposta à questão."""
    def find() -> RichMessage | None:
        for message in rich.messages(klass.bot, klass.chat):
            if message.force_reply and message.reply_to_message_id == question.message_id:
                return message
        return None

    return rich.wait_for(find, timeout=WAIT)


def _edit_after(rich: Any, klass: _Class, message_id: int, after: int,
                predicate: Callable[[RichMessage], bool]) -> Any:
    def find() -> Any:
        for edit in rich.edits(klass.bot, klass.chat, message_id):
            if edit.call_seq > after and predicate(edit.after):
                return edit
        return None

    return rich.wait_for(find, timeout=WAIT)


SEVEN_OPTIONS = (FakeOption("a", "Dois"), FakeOption("b", "Nove"), FakeOption("c", "Sete"),
                 FakeOption("d", "Oito"), FakeOption("e", "Onze"), FakeOption("f", "Quinze"),
                 FakeOption("g", "Treze"))


@pytest.mark.parametrize("mode", ["rich", "html"])
def test_question_message_presents_every_field_even_without_marker(
        client: Any, sql: psycopg.Connection, rich_bot_api: Any, clock: FakeClock, engine_fake: Any,
        repo_fetcher: Any, bot_worker_thread: Any, mode: str) -> None:
    """numa questão de duas partes cujo enunciado **não** tem
    nenhum `cq-field` (a IR da variante injetada no motor falso, como a de um campo dentro de um
    elemento removido na canonicalização), a mensagem da questão pelo Telegram, nos dois modos
    (Rich, o padrão, e HTML simples), traz o enunciado e **cada** campo de `fields[]`, na ordem: o
    rótulo e o sufixo do número e o rótulo da múltipla com as alternativas na ordem da variante; e
    mostra as restrições de seleção da múltipla que a IR traz (`min_select = 4`, `max_select = 6`) numa
    mesma linha. No HTML simples, as alternativas ficam em linhas (uma por linha)."""
    rich = rich_bot_api
    klass = _class_with_student(client, sql, rich, clock)
    commit = _commit()
    question = FakeQuestion(
        "Duas partes", "Responda às duas partes.",
        (number_field("velocidade", label="Velocidade média:", suffix="m/s"),
         multiple_choice_field("primos", correct=("a", "c", "e", "g"), options=SEVEN_OPTIONS,
                               min_select=4, max_select=6, label="Primos:")))
    engine_fake.program((REPO, commit, "multipart"), question)
    snapshot, ir, warnings = build_variant(question, "0")
    ir["statement"]["html"] = "<p>Responda às duas partes.</p>"
    assert [f["constraints"].get("min_select") for f in ir["fields"]] == [None, 4]
    assert [f["constraints"].get("max_select") for f in ir["fields"]] == [None, 6]
    engine_fake.inject("create_variant",
                       body={"outcome": "ok", "snapshot": snapshot, "ir": ir, "warnings": warnings},
                       times=None)

    dispatch = prepare_dispatch(client, ADMIN_TOKEN, klass.code, repo=REPO, commit=commit,
                                qid="multipart", closes_at=iso(clock.now() + timedelta(hours=2)))
    engine = _start_worker(bot_worker_thread, engine_fake, repo_fetcher,
                           DeliverySettings(telegram_format=mode, chat_interval_seconds=0.05))
    try:
        after = _marker(rich)
        _validated_and_confirmed(client, rich, klass, dispatch["dispatch_id"])
        message = _question(rich, klass, after)
    finally:
        bot_worker_thread.stop()
        engine.close()

    assert message.text_format == {"rich": "rich_html", "html": "html"}[mode]
    text = message.plain_text or ""
    assert "Responda às duas partes." in text
    labels = [text.find("Velocidade média:"), text.find("Primos:")]
    assert -1 not in labels and labels[0] < labels[1], text
    assert "m/s" in text[labels[0]:labels[1]]
    positions = [text.find(option.html, labels[1]) for option in SEVEN_OPTIONS]
    assert -1 not in positions and positions == sorted(positions), text
    # Os números sem outro dígito ao lado (no Rich, o texto depois do parsing junta os blocos).
    assert any(re.search(r"(?<![0-9])4(?![0-9])", line) and re.search(r"(?<![0-9])6(?![0-9])", line)
               for line in text.splitlines()), text
    if mode == "html":
        for option in SEVEN_OPTIONS:
            [line] = [line for line in text.splitlines() if option.html in line]
            assert [o.html for o in SEVEN_OPTIONS if o.html in line] == [option.html], line


def test_typed_echo_is_truncated_at_200_before_escaping(
        client: Any, sql: psycopg.Connection, rich_bot_api: Any, clock: FakeClock, engine_fake: Any,
        repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """O eco no `FEEDBACK`: a aluna responde ao pedido do
    campo com 253 caracteres que começam por `<b>`; a edição do feedback (Rich) mostra o eco truncado
    nos 200 primeiros caracteres seguidos de "…" (truncado **antes** do escape: o 200º caractere é o `<`
    do `</b>`, que aparece literal), nada além deles, e o veredito ❌; o eco sai escapado (`&lt;b&gt;`),
    nunca como tag."""
    rich = rich_bot_api
    klass = _class_with_student(client, sql, rich, clock)
    commit = _commit()
    engine_fake.program((REPO, commit, "text"),
                        FakeQuestion("Eco", "Qual é a cor do céu?", (text_field(label="Cor:"),)))
    answer = "<b>" + "a" * 196 + "</b>" + "z" * 50
    assert len(answer) == 253 and answer[199] == "<"

    dispatch = prepare_dispatch(client, ADMIN_TOKEN, klass.code, repo=REPO, commit=commit, qid="text",
                                closes_at=iso(clock.now() + timedelta(hours=2)))
    engine = _start_worker(bot_worker_thread, engine_fake, repo_fetcher,
                           DeliverySettings(chat_interval_seconds=0.05))
    try:
        after = _marker(rich)
        _validated_and_confirmed(client, rich, klass, dispatch["dispatch_id"])
        message = _question(rich, klass, after)
        prompt = _prompt(rich, klass, message)
        answered = _marker(rich)
        rich.push_text(klass.bot, klass.user, answer, reply_to_message_id=prompt.message_id)
        edit = _edit_after(rich, klass, message.message_id, answered,
                           lambda m: VERDICT_INCORRECT in (m.plain_text or ""))
    finally:
        bot_worker_thread.stop()
        engine.close()

    shown = edit.after
    assert shown.text_format == "rich_html"
    text = shown.plain_text or ""
    assert answer[:200] + "…" in text
    assert answer[:201] not in text and "z" * 5 not in text
    markup = shown.text or ""
    assert "&lt;b&gt;" + "a" * 196 + "&lt;" in markup
    assert "<b>" + "a" * 10 not in markup


def test_feedback_that_does_not_fit_becomes_an_edit_without_it_and_a_short_notice(
        client: Any, sql: psycopg.Connection, rich_bot_api: Any, clock: FakeClock, engine_fake: Any,
        repo_fetcher: Any, bot_worker_thread: Any) -> None:
    """O feedback que não cabe: no HTML simples (4096
    unidades UTF-16), um disparo de medição (enunciado de 1000 letras, resposta "x") dá o acréscimo da
    mensagem da questão e o da edição do feedback; o segundo disparo tem o enunciado do tamanho que
    deixa a questão 10 unidades abaixo do limite, e a resposta de 300 letras faz o estado com o feedback
    (eco truncado em 200 + "…") passar do limite. Então a edição da questão sai **sem** o bloco de
    feedback (o enunciado inteiro, nenhum eco, sem os controles encerrados, dentro do limite) e o
    feedback curto sai numa mensagem nova em resposta à questão (papel `notice`, no formato do feedback de nota parcial:
    o veredito ❌ com a nota, sem eco nem marcas por alternativa, curta); nenhuma mensagem
    nem edição mostra o eco."""
    rich = rich_bot_api
    klass = _class_with_student(client, sql, rich, clock)
    closes_at = iso(clock.now() + timedelta(hours=2))
    probe_size = 1000
    commit_a, commit_b = _commit(), _commit()
    engine_fake.program((REPO, commit_a, "text"),
                        FakeQuestion("Tamanho", "k" * probe_size, (text_field(label="Cor:"),)))
    html = DeliverySettings(telegram_format="html", chat_interval_seconds=0.05)

    first = prepare_dispatch(client, ADMIN_TOKEN, klass.code, repo=REPO, commit=commit_a, qid="text",
                             closes_at=closes_at)
    engine = _start_worker(bot_worker_thread, engine_fake, repo_fetcher, html)
    try:
        after = _marker(rich)
        _validated_and_confirmed(client, rich, klass, first["dispatch_id"])
        probe = _question(rich, klass, after)
        assert probe.text_format == "html"
        probe_text = probe.plain_text or ""
        assert probe_text.count("k" * probe_size) == 1
        open_overhead = _utf16_len(probe_text) - probe_size
        answered = _marker(rich)
        rich.push_text(klass.bot, klass.user, "x",
                       reply_to_message_id=_prompt(rich, klass, probe).message_id)
        graded = _edit_after(rich, klass, probe.message_id, answered,
                             lambda m: VERDICT_INCORRECT in (m.plain_text or ""))
        feedback_overhead = _utf16_len(graded.after.plain_text or "") - probe_size - 1

        size = 4096 - open_overhead - 10
        assert size > probe_size and feedback_overhead + 201 > open_overhead + 10, (
            "calibração: o feedback com eco de 200 tem de passar do limite", open_overhead,
            feedback_overhead)
        engine_fake.program((REPO, commit_b, "text"),
                            FakeQuestion("Tamanho", "k" * size, (text_field(label="Cor:"),)))
        second = prepare_dispatch(client, ADMIN_TOKEN, klass.code, repo=REPO, commit=commit_b,
                                  qid="text", closes_at=closes_at)
        after = _marker(rich)
        _validated_and_confirmed(client, rich, klass, second["dispatch_id"])
        message = _question(rich, klass, after)
        assert _utf16_len(message.plain_text or "") <= 4096
        answered = _marker(rich)
        rich.push_text(klass.bot, klass.user, "y" * 300,
                       reply_to_message_id=_prompt(rich, klass, message).message_id)

        def notice_and_edit() -> tuple[RichMessage, Any] | None:
            notices = [m for m in rich.messages(klass.bot, klass.chat)
                       if m.call_seq > answered and m.reply_to_message_id == message.message_id
                       and VERDICT_INCORRECT in (m.plain_text or "")]
            edits = [e for e in rich.edits(klass.bot, klass.chat, message.message_id)
                     if e.call_seq > answered and e.after.keyboard is None]
            return (notices[0], edits[-1]) if notices and edits else None

        notice, edit = rich.wait_for(notice_and_edit, timeout=WAIT)
    finally:
        bot_worker_thread.stop()
        engine.close()

    echo = "y" * 20
    shown = edit.after.plain_text or ""
    assert "k" * size in shown
    assert echo not in shown
    assert _utf16_len(shown) <= 4096
    notice_text = notice.plain_text or ""
    assert echo not in notice_text and _utf16_len(notice_text) <= 200
    assert notice.text_format in ("html", "plain")
    later = [m.plain_text or "" for m in rich.messages(klass.bot, klass.chat) if m.call_seq > answered]
    later += [e.after.plain_text or "" for e in rich.edits(klass.bot, klass.chat)
              if e.call_seq > answered]
    assert all(echo not in text for text in later)
