"""Redação do token de bot e textos fixos dos bots (perfil do bot e contagem de caracteres
do Telegram).

- **Redação**: além dos outros segredos redigidos (`cq_[A-Za-z0-9_-]{20,}` e os
  valores literais de `secrets`), a forma do token de bot
  `(?<![0-9A-Za-z_-])[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}(?![0-9A-Za-z_-])` vira `[segredo]`.
- **Textos** (`cq_server.botmessages`): as constantes de texto e `compose(name, *, course,
  term)`, só para `DESCRIPTION_ACTIVE`, `DESCRIPTION_ARCHIVED`, `HELP` e `WELCOME_TEAM` (outro nome
  → `KeyError`); substitui `{course}` e `{term}` sem `str.format` (chaves nos valores ficam
  literais); só nas duas descrições corta a disciplina para o texto caber em 512 unidades UTF-16,
  terminando em `…` (U+2026), no maior prefixo que cabe, e nunca corta o período.

Os textos esperados são montados aqui com os trechos literais esperados, e as contagens do
corte são literais.
"""

from __future__ import annotations

import pytest

from cq_server import botmessages
from cq_server.botmessages import compose
from cq_server.ports.telegram import BotCommand
from cq_server.redaction import KnownIdentity, redact
from tests.helpers.api import marker
from tests.helpers.canary import ZEFIRO

# --- redação ---------------------------------------------------------------------------------------

SECRET = "[segredo]"
BOT_ID = "6100000001"
SECRET_35 = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456-_"
SECRET_30 = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5"
SECRET_29 = SECRET_30[:29]
ID_16 = "1234567890123456"
BOT_TOKEN = f"{BOT_ID}:{SECRET_35}"
CORE_SECRET = "cq_" + "Rz9_-" * 5
LITERAL_SECRET = "SenhaLiteral.Qx9!"
assert (len(SECRET_35), len(SECRET_30), len(SECRET_29), len(ID_16)) == (35, 30, 29, 16)

ZEF = KnownIdentity(alias="a1b2c3", full_name=ZEFIRO.name, ra=ZEFIRO.ra, email=ZEFIRO.email)

# (id, texto, esperado): a forma do token com vizinhos que são limite.
REDACTED_CASES = [
    ("sozinho", BOT_TOKEN, SECRET),
    ("na-frase", f"o token {BOT_TOKEN} vazou", f"o token {SECRET} vazou"),
    ("entre-parenteses", f"({BOT_TOKEN})", f"({SECRET})"),
    ("atribuicao", f"token={BOT_TOKEN};", f"token={SECRET};"),
    ("json", '{"token": "' + BOT_TOKEN + '"}', '{"token": "' + SECRET + '"}'),
    ("entre-barras", f"api/{BOT_TOKEN}/getMe", f"api/{SECRET}/getMe"),
    ("dois-pontos-antes", f"bot:{BOT_TOKEN}", f"bot:{SECRET}"),
    ("arroba-antes", f"@{BOT_TOKEN}", f"@{SECRET}"),
    ("quebras-de-linha", f"linha\n{BOT_TOKEN}\nfim", f"linha\n{SECRET}\nfim"),
    ("id-de-1-digito", f"1:{SECRET_30}", SECRET),
    ("id-de-16-digitos", f"{ID_16}:{SECRET_30}", SECRET),
    ("segredo-de-30", f"x {BOT_ID}:{SECRET_30} y", f"x {SECRET} y"),
    ("segredo-longo", f"{BOT_ID}:{SECRET_30 * 4}.", f"{SECRET}."),
    ("vizinhos-da-direita-entram-no-segredo", f"{BOT_TOKEN}xyz-_ fim", f"{SECRET} fim"),
    ("dois-tokens", f"{BOT_TOKEN} e {ID_16}:{SECRET_30}", f"{SECRET} e {SECRET}"),
]

# (id, texto que não muda, controle positivo com o vizinho trocado por um limite, esperado do
# controle).
NOT_A_BOUNDARY_CASES = [
    ("letra-antes", f"x{BOT_TOKEN}", f"x {BOT_TOKEN}", f"x {SECRET}"),
    ("sublinhado-antes", f"_{BOT_TOKEN}", f"_ {BOT_TOKEN}", f"_ {SECRET}"),
    ("hifen-antes", f"-{BOT_TOKEN}", f"- {BOT_TOKEN}", f"- {SECRET}"),
    ("digito-antes-17-digitos", f"9{ID_16}:{SECRET_30}", f"9 {ID_16}:{SECRET_30}", f"9 {SECRET}"),
    ("zero-a-esquerda", f"0{BOT_TOKEN}", f"0 {BOT_TOKEN}", f"0 {SECRET}"),
    ("id-zero", f"0:{SECRET_30}", f"10:{SECRET_30}", SECRET),
    ("segredo-de-29", f"{BOT_ID}:{SECRET_29}", f"{BOT_ID}:{SECRET_30}", SECRET),
    ("hifen-no-lugar-dos-dois-pontos", f"{BOT_ID}-{SECRET_35}", BOT_TOKEN, SECRET),
    ("espaco-depois-dos-dois-pontos", f"{BOT_ID}: {SECRET_35}", BOT_TOKEN, SECRET),
]


@pytest.mark.parametrize(("text", "expected"), [(t, e) for _, t, e in REDACTED_CASES],
                         ids=[i for i, _, _ in REDACTED_CASES])
def test_bot_token_shape_becomes_secret_marker(text: str, expected: str) -> None:
    """A redação troca por marca de segredo qualquer texto com a forma de um token de bot:
    a forma do token, com vizinhos que são limite (início e fim do texto, espaço,
    pontuação, aspas, barra, `:`, `@`, quebra de linha), vira `[segredo]`; id de 1 a 16 dígitos;
    parte secreta de 30 caracteres ou mais (os caracteres do alfabeto à direita entram nela)."""
    assert redact(text, []) == expected


@pytest.mark.parametrize(("text", "control", "control_expected"),
                         [(t, c, e) for _, t, c, e in NOT_A_BOUNDARY_CASES],
                         ids=[i for i, *_ in NOT_A_BOUNDARY_CASES])
def test_bot_token_shape_with_non_boundary_neighbors_is_kept(text: str, control: str,
                                                            control_expected: str) -> None:
    """As fronteiras `(?<![0-9A-Za-z_-])` e `[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}`, com vizinhos
    que não são limite, não viram segredo: letra, `_`, `-` ou dígito colado
    antes do id (inclusive um id de 17 dígitos e o zero à esquerda), id `0`, parte secreta de 29
    caracteres e separador diferente de `:` não mudam o texto; controle positivo: o mesmo token com
    um vizinho que é limite vira `[segredo]`."""
    assert redact(control, []) == control_expected
    assert redact(text, []) == text


def test_core_secrets_are_still_redacted_next_to_bot_tokens() -> None:
    """Os outros segredos redigidos: o padrão `cq_…` e os valores literais de
    `secrets` continuam virando `[segredo]`, sozinhos e no mesmo texto que um token de bot."""
    assert redact(f"a {CORE_SECRET} b", []) == f"a {SECRET} b"
    assert redact(f"a {LITERAL_SECRET} b", [], secrets=[LITERAL_SECRET]) == f"a {SECRET} b"
    text = f"{CORE_SECRET} e {BOT_TOKEN}; inicial {LITERAL_SECRET}."
    assert redact(text, [], secrets=[LITERAL_SECRET]) == f"{SECRET} e {SECRET}; inicial {SECRET}."


def test_bot_token_next_to_a_person_and_with_an_ra_inside() -> None:
    """A forma do token entra nos segredos do algoritmo de redação: um nome ao
    lado do token vira a marca da pessoa, e o token vira `[segredo]`; um token cujo id contém o RA
    de uma pessoa cadastrada vira `[segredo]` inteiro (o intervalo unido contém um segredo), sem
    marca de pessoa e sem sobrar a parte secreta."""
    text = f"{ZEFIRO.name} mandou {BOT_TOKEN}."
    assert redact(text, [ZEF]) == f"{marker(ZEF.alias)} mandou {SECRET}."
    with_ra = f"token {ZEFIRO.ra}:{SECRET_30} fim"
    assert redact(with_ra, [ZEF]) == f"token {SECRET} fim"


def test_redaction_with_bot_tokens_is_idempotent() -> None:
    """A redação roda na gravação e de novo na saída: com token de bot,
    segredo `cq_…` e pessoa no mesmo texto, `redact(redact(x)) == redact(x)`, com saída exata."""
    text = f"{ZEFIRO.name} usou {BOT_TOKEN} e {CORE_SECRET} (RA {ZEFIRO.ra})."
    once = redact(text, [ZEF])
    assert once == f"{marker(ZEF.alias)} usou {SECRET} e {SECRET} (RA {marker(ZEF.alias)})."
    assert redact(once, [ZEF]) == once


# --- textos fixos ----------------------------------------------------------------

TEMPLATES = {
    "DESCRIPTION_ACTIVE": ("Bot do oferecimento {course} ({term}). Para participar, toque no link de "
                           "convite que você recebeu da coordenação."),
    "DESCRIPTION_ARCHIVED": ("O oferecimento {course} ({term}) foi encerrado. Este bot não recebe mais "
                             "mensagens."),
    "HELP": ("Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de "
             "convite que você recebeu da coordenação. Se o link não funcionar, peça um novo à "
             "coordenação."),
    "WELCOME_TEAM": ("Pronto! Você faz parte da equipe do oferecimento {course} ({term}). Por este "
                     "chat você vai receber os avisos do oferecimento."),
}
FIXED_TEXTS = {
    "LINK_INVALID": "Este link não é válido ou já venceu. Peça um novo link à coordenação.",
    "ALREADY_LINKED": "Você já está vinculado a este oferecimento.",
    "OTHER_PERSON": ("Esta conta do Telegram já está vinculada a outra pessoa neste oferecimento. "
                     "Fale com a coordenação."),
}
COMMANDS = [
    BotCommand(command="start", description="Começar ou usar um link de convite"),
    BotCommand(command="ajuda", description="Ver como usar este bot"),
]
ELLIPSIS = "…"


def _expected(name: str, course: str, term: str) -> str:
    """O texto esperado com `course` e `term` no lugar dos marcadores (trechos literais)."""
    if name == "DESCRIPTION_ACTIVE":
        return ("Bot do oferecimento " + course + " (" + term + "). Para participar, toque no link "
                "de convite que você recebeu da coordenação.")
    if name == "DESCRIPTION_ARCHIVED":
        return ("O oferecimento " + course + " (" + term + ") foi encerrado. Este bot não recebe "
                "mais mensagens.")
    if name == "HELP":
        return ("Este é o bot do oferecimento " + course + " (" + term + "). Para participar, toque "
                "no link de convite que você recebeu da coordenação. Se o link não funcionar, peça "
                "um novo à coordenação.")
    assert name == "WELCOME_TEAM"
    return ("Pronto! Você faz parte da equipe do oferecimento " + course + " (" + term + "). Por "
            "este chat você vai receber os avisos do oferecimento.")


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


COURSE = "MC102 — Algoritmos e Programação de Computadores"
TERM = "2026, 2º semestre"

# Disciplina de 200 caracteres fora do BMP (2 unidades UTF-16 cada: 𝐀 a 𝐙, em ciclo) e período de
# 40 caracteres, com acentos e um caractere fora do BMP (41 unidades).
COURSE_ASTRAL_200 = "".join(chr(0x1D400 + (i % 26)) for i in range(200))
COURSE_ASTRAL_250 = "".join(chr(0x1D400 + (i % 26)) for i in range(250))
TERM_40 = "Segundo semestre de 2026 — turma única \U0001D419"
# Disciplina só no BMP, com acentos (1 unidade cada), e período ASCII de 40 caracteres.
COURSE_BMP = ("ÁlgebraNuméricaçãoÍÓÚ" * 40)[:500]
TERM_BMP_40 = "Segundo semestre de 2026 - turma unica A"
assert (len(COURSE_ASTRAL_200), _utf16_units(COURSE_ASTRAL_200)) == (200, 400)
assert (len(TERM_40), _utf16_units(TERM_40), len(TERM_BMP_40)) == (40, 41, 40)


@pytest.mark.parametrize("name", list(TEMPLATES))
def test_compose_each_name_with_course_and_term(name: str) -> None:
    """Descrição com a disciplina e o período; texto fixo, em português; perfil do bot:
    a constante tem o texto exato esperado, e `compose` devolve o texto
    com a disciplina e o período nos lugares de `{course}` e `{term}`."""
    assert getattr(botmessages, name) == TEMPLATES[name]
    assert compose(name, course=COURSE, term=TERM) == _expected(name, COURSE, TERM)


@pytest.mark.parametrize("name", list(FIXED_TEXTS))
def test_texts_used_without_compose(name: str) -> None:
    """Textos fixos da recusa e do vínculo (`LINK_INVALID`,
    `ALREADY_LINKED` e `OTHER_PERSON` são usados sem `compose`): a constante tem o texto exato
    esperado, e `compose` com esse nome → `KeyError`."""
    assert getattr(botmessages, name) == FIXED_TEXTS[name]
    with pytest.raises(KeyError):
        compose(name, course=COURSE, term=TERM)


def test_commands_constant_and_compose_refuses_it() -> None:
    """Comandos `/start` e `/ajuda` com descrições em português: `COMMANDS` é
    exatamente `start` e `ajuda`, nessa ordem, com os textos esperados; `compose("COMMANDS")`
    → `KeyError`."""
    assert list(botmessages.COMMANDS) == COMMANDS
    with pytest.raises(KeyError):
        compose("COMMANDS", course=COURSE, term=TERM)


@pytest.mark.parametrize("name", ["description_active", "Help", "HELP ", "WELCOME", "", "compose",
                                  "DESCRIPTION_MAX_UTF16", "{course}"],
                         ids=["minusculas", "outra-caixa", "espaco-final", "parcial", "vazio",
                              "funcao", "outra-constante", "marcador"])
def test_compose_with_another_name_is_key_error(name: str) -> None:
    """Outro nome → `KeyError`: nomes em outra caixa, com espaço, parciais,
    vazios ou de outros atributos do módulo."""
    with pytest.raises(KeyError):
        compose(name, course=COURSE, term=TERM)


BRACE_CASES = [
    ("disciplina-com-term", "HELP", "Curso {term} e {course}", "2026s2"),
    ("periodo-com-marcadores", "WELCOME_TEAM", "MC102", "{course} {term} {0} {{x}}"),
    ("chaves-soltas", "DESCRIPTION_ACTIVE", "Álgebra {0} {x} {{course}} } {", "{term}"),
    ("trocados", "DESCRIPTION_ARCHIVED", "{term}", "{course}"),
]


@pytest.mark.parametrize(("name", "course", "term"), [(n, c, t) for _, n, c, t in BRACE_CASES],
                         ids=[i for i, *_ in BRACE_CASES])
def test_braces_in_course_and_term_stay_literal(name: str, course: str, term: str) -> None:
    """Substitui `{course}` e `{term}` sem `str.format`; chaves no texto ficam
    literais; disciplina contendo `{term}` fica literal): os marcadores e as chaves
    que vêm na disciplina ou no período saem exatamente como vieram."""
    assert compose(name, course=course, term=term) == _expected(name, course, term)


def test_active_description_cuts_the_course_to_fit_512_utf16_units() -> None:
    """Descrição de até 512 caracteres, com a contagem em unidades UTF-16:
    com disciplina de 200 caracteres fora do BMP (400 unidades) e período de 40
    (41 unidades), a descrição ativa passaria de 512; a disciplina é cortada no maior prefixo que
    faz o texto caber (185 caracteres, sem partir um par substituto), terminada em `…`, o período sai
    inteiro, e o texto fica com 511 unidades."""
    result = compose("DESCRIPTION_ACTIVE", course=COURSE_ASTRAL_200, term=TERM_40)
    assert _utf16_units(_expected("DESCRIPTION_ACTIVE", COURSE_ASTRAL_200, TERM_40)) == 540
    assert result == _expected("DESCRIPTION_ACTIVE", COURSE_ASTRAL_200[:185] + ELLIPSIS, TERM_40)
    assert _utf16_units(result) == 511


def test_archived_description_is_cut_only_when_it_does_not_fit() -> None:
    """No arquivamento, a descrição de encerrado: com a mesma
    disciplina de 200 caracteres fora do BMP e o período de 40, a descrição de arquivado cabe (510
    unidades) e sai inteira, sem `…`; com 250 caracteres fora do BMP, a disciplina é cortada em 200
    caracteres e `…`, com o período inteiro (511 unidades)."""
    fits = compose("DESCRIPTION_ARCHIVED", course=COURSE_ASTRAL_200, term=TERM_40)
    assert fits == _expected("DESCRIPTION_ARCHIVED", COURSE_ASTRAL_200, TERM_40)
    assert _utf16_units(fits) == 510
    cut = compose("DESCRIPTION_ARCHIVED", course=COURSE_ASTRAL_250, term=TERM_40)
    assert cut == _expected("DESCRIPTION_ARCHIVED", COURSE_ASTRAL_250[:200] + ELLIPSIS, TERM_40)
    assert _utf16_units(cut) == 511


@pytest.mark.parametrize(("name", "fits", "cut_to"), [
    ("DESCRIPTION_ACTIVE", 373, 372),
    ("DESCRIPTION_ARCHIVED", 403, 402),
], ids=["ativa", "arquivada"])
def test_description_boundary_at_512_utf16_units(name: str, fits: int, cut_to: int) -> None:
    """Se a descrição passar de 512: com disciplina no BMP (com
    acentos, 1 unidade cada) e período ASCII de 40, o texto de exatamente 512 unidades sai inteiro;
    com um caractere a mais (513), a disciplina perde dois caracteres e ganha `…` (512 unidades)."""
    exact = compose(name, course=COURSE_BMP[:fits], term=TERM_BMP_40)
    assert exact == _expected(name, COURSE_BMP[:fits], TERM_BMP_40)
    assert _utf16_units(exact) == 512
    over = compose(name, course=COURSE_BMP[:fits + 1], term=TERM_BMP_40)
    assert over == _expected(name, COURSE_BMP[:cut_to] + ELLIPSIS, TERM_BMP_40)
    assert _utf16_units(over) == 512


@pytest.mark.parametrize(("name", "units"), [("HELP", 602), ("WELCOME_TEAM", 552)])
def test_help_and_welcome_are_never_cut(name: str, units: int) -> None:
    """Só nas duas descrições corta a disciplina: a ajuda e as boas-vindas saem
    inteiras mesmo passando de 512 unidades UTF-16."""
    result = compose(name, course=COURSE_ASTRAL_200, term=TERM_40)
    assert result == _expected(name, COURSE_ASTRAL_200, TERM_40)
    assert _utf16_units(result) == units
    assert ELLIPSIS not in result
