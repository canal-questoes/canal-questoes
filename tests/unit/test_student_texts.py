"""Textos do aluno, e-mail do convite e validade no fuso.


- **Textos do bot** (`cq_server.botmessages`): `TERMS`, `WELCOME_STUDENT`,
  `TERMS_REFUSED` e `INVITE_REQUESTED` passam por `compose(name, *, course, term)`;
  o `TERMS` tem `{version}` na primeira linha, preenchido com o `TERMS_VERSION` lido na
  hora da chamada; `{course}`, `{term}` e `{version}` são trocados numa passada só, e as chaves
  que vêm nos valores ficam literais; nos quatro, a disciplina é cortada (terminando em `…`)
  para o texto caber em 4.096 unidades UTF-16, no maior prefixo que cabe, sem partir um par substituto,
  e o período nunca é cortado. Os nomes que `tests/unit/test_bot_texts.py` exige como `KeyError` continuam dando
  `KeyError`.
- **E-mail do convite** (`compose_invite_email`): assunto e corpo exatos, sem nome de
  aluno (a função não recebe nome); no assunto, a disciplina e o período têm os caracteres `Cc`, `Zl` e
  `Zp` trocados por espaço, as sequências de espaço reduzidas a um e as pontas aparadas; o corpo não
  passa por essa troca e nunca corta a disciplina.
- **Validade no fuso** (`timezones.format_local`): `AAAA-MM-DD HH:MM <abreviação>
  (<fuso>)`, o formato do CLI (`strftime('%Y-%m-%d %H:%M %Z')`) com o fuso entre parênteses.

Os textos esperados são montados aqui com os trechos literais esperados (e não com as
constantes do módulo), e as contagens de unidades UTF-16 do corte são literais.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timezone

import pytest

from cq_server import botmessages
from cq_server.botmessages import compose, compose_invite_email
from cq_server.timezones import format_local

UTC = timezone.utc
ELLIPSIS = "…"
TEXT_LIMIT = 4096

# Os quatro nomes dos textos do bot testados aqui.
NAMES = ["TERMS", "WELCOME_STUDENT", "TERMS_REFUSED", "INVITE_REQUESTED"]

# --- trechos literais dos textos do bot ----------------------------------------------------------

_TERMS_HEAD = "Termo de uso do bot do oferecimento "
# O `TERMS` depois de `, versão {version}` (cada item é uma linha; as linhas vazias separam os
# parágrafos).
_TERMS_REST = "\n".join((
    "",
    "",
    "Leia até o fim: as opções para aceitar ou recusar estão no final.",
    "",
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a elas. As "
    "respostas são registradas para o ensino na disciplina e para o piloto de um Trabalho de "
    "Conclusão de Curso (TCC) de Ciência da Computação da Unicamp, que avalia este sistema.",
    "",
    "Quais dados são guardados:",
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;",
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do perfil e o "
    "telefone não são guardados);",
    "• as suas respostas e as mensagens que você mandar a este bot.",
    "",
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno aparece "
    "por um código. A ligação entre o código e o seu nome só é consultada quando necessário, e cada "
    "consulta fica registrada. No TCC, os resultados aparecem só de forma agregada, sem identificar "
    "ninguém.",
    "",
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste "
    "oferecimento, definida pela coordenação. Depois desse prazo, os seus dados de identificação "
    "(nome, RA, e-mail e conta do Telegram) são apagados, e as respostas continuam guardadas sem "
    "eles.",
    "",
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste piloto, estas "
    "questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail continua valendo até "
    "vencer.",
    "",
    "Para aceitar, toque em /aceito",
    "Para recusar, toque em /recuso",
))
_TERMS_TAIL = "Para aceitar, toque em /aceito\nPara recusar, toque em /recuso"

# (antes da disciplina, depois do período fechado com `)`) dos outros três textos.
_PARTS = {
    "WELCOME_STUDENT": (
        "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento ",
        ". As questões da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a "
        "coordenação.",
    ),
    "TERMS_REFUSED": (
        "Tudo bem: você recusou o termo de uso. Nada foi vinculado, e você não vai receber questões "
        "do oferecimento ",
        " por este chat.\n"
        "\n"
        "Se mudar de ideia, toque de novo no link do e-mail, nesta mesma conta do Telegram, enquanto "
        "ele valer. Se o link vencer, mande /convite seguido de um espaço e do seu e-mail "
        "institucional (o mesmo da lista da turma) para receber um novo link por e-mail.",
    ),
    "INVITE_REQUESTED": (
        "Pedido recebido. Se o e-mail informado for de um aluno da lista do oferecimento ",
        " que ainda não se vinculou, um novo link de convite vai para esse e-mail, e o link anterior "
        "deixa de valer. O e-mail pode levar alguns minutos para chegar.\n"
        "\n"
        "Para pedir, mande /convite seguido de um espaço e do seu e-mail institucional, o mesmo da "
        "lista da turma. Há um limite de pedidos por dia. Se o link não chegar, fale com a "
        "coordenação.",
    ),
}


def _expected(name: str, course: str, term: str, version: int | str = 1) -> str:
    """O texto do bot com os valores nos lugares de `{course}`, `{term}` e (só no
    `TERMS`) `{version}`, por concatenação dos trechos literais."""
    if name == "TERMS":
        return _TERMS_HEAD + course + " (" + term + "), versão " + str(version) + _TERMS_REST
    before, after = _PARTS[name]
    return before + course + " (" + term + ")" + after


def _template(name: str) -> str:
    """A constante do texto do bot, com os marcadores."""
    return _expected(name, "{course}", "{term}", "{version}")


# --- trechos literais do e-mail do convite --------------------------------------------------------

_SUBJECT_HEAD = "Seu link para o bot do oferecimento "


def _expected_subject(course: str, term: str) -> str:
    return _SUBJECT_HEAD + course + " (" + term + ")"


def _expected_body(course: str, term: str, link: str, validity: str) -> str:
    return "\n".join((
        "Olá!",
        "",
        "Você está na lista da turma do oferecimento " + course + " (" + term + "). As questões da "
        "disciplina chegam por um bot do Telegram, e este é o seu link pessoal para começar:",
        "",
        link,
        "",
        "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O "
        "bot vai mostrar o termo de uso, que você pode aceitar ou recusar.",
        "",
        "O link vale até " + validity + ". Não o repasse: ele é só seu.",
        "",
        "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
        "e-mail institucional (o mesmo da lista da turma) para receber um novo link.",
        "",
        "Esta mensagem foi enviada automaticamente; não é preciso respondê-la.",
    ))


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


COURSE = "MC102 — Algoritmos e Programação de Computadores"
TERM = "2026, 2º semestre"

# Disciplinas fora do BMP (2 unidades UTF-16 cada: 𝐀 a 𝐙, em ciclo) e período de 40 caracteres com
# acentos e um caractere fora do BMP (41 unidades), como no teste dos textos do bot.
COURSE_ASTRAL_200 = "".join(chr(0x1D400 + (i % 26)) for i in range(200))
COURSE_ASTRAL_2500 = "".join(chr(0x1D400 + (i % 26)) for i in range(2500))
TERM_40 = "Segundo semestre de 2026 — turma única \U0001D419"
# Disciplina só no BMP, com acentos (1 unidade cada), e período ASCII de 40 caracteres.
COURSE_BMP = ("ÁlgebraNuméricaçãoÍÓÚ" * 300)[:5000]
TERM_BMP_40 = "Segundo semestre de 2026 - turma unica A"
assert (len(COURSE_ASTRAL_200), _utf16_units(COURSE_ASTRAL_200)) == (200, 400)
assert (len(COURSE_ASTRAL_2500), _utf16_units(COURSE_ASTRAL_2500)) == (2500, 5000)
assert (len(TERM_40), _utf16_units(TERM_40), len(TERM_BMP_40)) == (40, 41, 40)
assert (len(COURSE_BMP), _utf16_units(COURSE_BMP)) == (5000, 5000)

# Os nomes que os testes dos textos do bot exigem como `KeyError` (tests/unit/test_bot_texts.py).
BOT_KEYERROR_NAMES = ["LINK_INVALID", "ALREADY_LINKED", "OTHER_PERSON", "COMMANDS",
                     "description_active", "Help", "HELP ", "WELCOME", "", "compose",
                     "DESCRIPTION_MAX_UTF16", "{course}"]


# --- já verdes no esqueleto ----------------------------------------------------------------


def test_terms_version_is_1() -> None:
    """O termo com a versão vigente (`TERMS_VERSION = 1`;
    `TERMS_VERSION: int = 1`). Já passa no esqueleto: a constante é real no
    esqueleto."""
    assert type(botmessages.TERMS_VERSION) is int
    assert botmessages.TERMS_VERSION == 1


@pytest.mark.parametrize("name", BOT_KEYERROR_NAMES,
                         ids=["link-invalid", "already-linked", "other-person", "commands",
                              "minusculas", "outra-caixa", "espaco-final", "parcial", "vazio",
                              "funcao", "outra-constante", "marcador"])
def test_bot_keyerror_names_unchanged(name: str) -> None:
    """Os quatro nomes dos textos do aluno
    não colidem com os que `tests/unit/test_bot_texts.py` exige como `KeyError` (ex.: `WELCOME`): cada
    um desses nomes continua dando `KeyError`. Já passa no esqueleto."""
    with pytest.raises(KeyError):
        compose(name, course=COURSE, term=TERM)


# --- textos do bot ---------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_compose_student_text_exact(name: str) -> None:
    """Textos fixos, em português, com a disciplina e o período:
    a constante tem o texto exato (o `TERMS` com `{version}` na
    primeira linha); `compose` devolve o texto com a disciplina e o período curtos nos lugares
    dos marcadores (e a versão 1 no `TERMS`), só com quebras `\\n`; outras grafias do nome (caixa,
    espaço, prefixo) → `KeyError`."""
    assert getattr(botmessages, name) == _template(name)
    assert compose(name, course=COURSE, term=TERM) == _expected(name, COURSE, TERM)
    for variant in (name.lower(), name.title(), name + " ", " " + name, name[:-1]):
        with pytest.raises(KeyError):
            compose(variant, course=COURSE, term=TERM)


def test_terms_version_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """O termo com a versão vigente na primeira linha (`TERMS_VERSION`
    lido na hora da chamada; com `TERMS_VERSION = 2` (por `monkeypatch`), a primeira linha diz
    'versão 2'; a assinatura `compose(name, *, course, term)` não muda): com a
    versão 1, a primeira linha termina em `versão 1`; trocada para 2 e para 12 depois da importação,
    o `compose` seguinte já mostra a versão nova; os outros três textos não têm versão e não mudam."""
    params = inspect.signature(compose).parameters.values()
    assert [(p.name, p.kind) for p in params] == [
        ("name", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("course", inspect.Parameter.KEYWORD_ONLY),
        ("term", inspect.Parameter.KEYWORD_ONLY),
    ]
    first = compose("TERMS", course=COURSE, term=TERM)
    assert first == _expected("TERMS", COURSE, TERM, 1)
    assert first.split("\n", 1)[0] == (
        "Termo de uso do bot do oferecimento MC102 — Algoritmos e Programação de Computadores "
        "(2026, 2º semestre), versão 1")

    monkeypatch.setattr(botmessages, "TERMS_VERSION", 2)
    second = compose("TERMS", course=COURSE, term=TERM)
    assert second == _expected("TERMS", COURSE, TERM, 2)
    assert second.split("\n", 1)[0].endswith("(2026, 2º semestre), versão 2")

    monkeypatch.setattr(botmessages, "TERMS_VERSION", 12)
    assert compose("TERMS", course=COURSE, term=TERM) == _expected("TERMS", COURSE, TERM, 12)
    for name in NAMES[1:]:
        assert compose(name, course=COURSE, term=TERM) == _expected(name, COURSE, TERM)


def test_terms_braces_from_course_and_term_stay_literal() -> None:
    """Uma chave que vem na disciplina ou no período (ex.: `{version}`, `{course}`) fica
    literal (a troca numa passada só, `\\{(course|term|version)\\}`); com o
    `TERMS` com a disciplina `A {version} {term}` e o período `{course}`, esses valores aparecem
    literais na primeira linha, e só o `{version}` do texto recebe a versão."""
    text = compose("TERMS", course="A {version} {term}", term="{course}")
    assert text.split("\n", 1)[0] == (
        "Termo de uso do bot do oferecimento A {version} {term} ({course}), versão 1")
    assert text == _expected("TERMS", "A {version} {term}", "{course}", 1)


BRACE_CASES = [
    ("termo-marcadores-no-periodo", "TERMS", "MC102", "{version} {course} {0} {{x}}"),
    ("termo-chaves-soltas", "TERMS", "Álgebra {0} {x} {{version}} } {", "{term}"),
    ("boas-vindas-com-version", "WELCOME_STUDENT", "Curso {version}", "{term}"),
    ("recusa-trocados", "TERMS_REFUSED", "{term}", "{course}"),
    ("pedido-chaves-soltas", "INVITE_REQUESTED", "Álgebra {0} {x} {{course}} } {", "{version}"),
]


@pytest.mark.parametrize(("name", "course", "term"), [(n, c, t) for _, n, c, t in BRACE_CASES],
                         ids=[i for i, *_ in BRACE_CASES])
def test_braces_in_course_and_term_stay_literal(name: str, course: str, term: str) -> None:
    """Os marcadores trocados numa passada só (`{version}` só no
    `TERMS`): marcadores e chaves que vêm na disciplina ou no período saem exatamente como vieram, em
    cada um dos quatro textos."""
    assert compose(name, course=course, term=term) == _expected(name, course, term)


# (nome, unidades UTF-16 do texto) com a disciplina de 200 caracteres fora do BMP e o período de 40.
UNCUT_ASTRAL_UNITS = [("TERMS", 1954), ("WELCOME_STUDENT", 612), ("TERMS_REFUSED", 820),
                      ("INVITE_REQUESTED", 865)]


@pytest.mark.parametrize(("name", "units"), UNCUT_ASTRAL_UNITS)
def test_course_of_200_astral_characters_is_not_cut(name: str, units: int) -> None:
    """Corte para 4.096 unidades UTF-16, caso sem corte: com disciplina de 200
    caracteres fora do BMP (400 unidades) e período de 40 caracteres com um fora do BMP (41 unidades),
    o texto cabe e sai **sem** corte, com o valor exato e sem `…`."""
    result = compose(name, course=COURSE_ASTRAL_200, term=TERM_40)
    assert result == _expected(name, COURSE_ASTRAL_200, TERM_40)
    assert _utf16_units(result) == units
    assert ELLIPSIS not in result


# (id, nome, disciplina, caracteres da disciplina mantidos, unidades do texto sem corte, unidades do
# texto cortado): disciplina sintética longa passada direto ao `compose`.
CUT_CASES = [
    ("termo-bmp-3000", "TERMS", COURSE_BMP[:3000], 2541, 4554, 4096),
    ("termo-astral-2500", "TERMS", COURSE_ASTRAL_2500, 1270, 6554, 4095),
    ("boas-vindas-astral-2500", "WELCOME_STUDENT", COURSE_ASTRAL_2500, 1941, 5212, 4095),
    ("recusa-astral-2500", "TERMS_REFUSED", COURSE_ASTRAL_2500, 1837, 5420, 4095),
    ("pedido-astral-2500", "INVITE_REQUESTED", COURSE_ASTRAL_2500, 1815, 5465, 4096),
]


@pytest.mark.parametrize(("name", "course", "kept", "uncut_units", "units"),
                         [(n, c, k, uu, u) for _, n, c, k, uu, u in CUT_CASES],
                         ids=[i for i, *_ in CUT_CASES])
def test_long_course_is_cut_to_fit_4096_utf16_units(name: str, course: str, kept: int,
                                                   uncut_units: int, units: int) -> None:
    """Corte para 4.096 unidades UTF-16 (nos quatro, a disciplina é cortada, terminando em `…`,
    para o texto caber em 4.096 unidades UTF-16; o período nunca é cortado): com uma disciplina
    sintética longa (3.000 unidades no BMP no `TERMS`; 5.000 unidades fora do BMP nos quatro), a
    disciplina vira o maior prefixo que, seguido de `…`, faz o texto caber, sem partir um par
    substituto (por isso 4.095 unidades quando sobra uma só), e o período sai inteiro."""
    assert _utf16_units(_expected(name, course, TERM_40)) == uncut_units
    result = compose(name, course=course, term=TERM_40)
    assert result == _expected(name, course[:kept] + ELLIPSIS, TERM_40)
    assert _utf16_units(result) == units


# (nome, caracteres da disciplina no BMP com que o texto tem exatamente 4.096 unidades, com o período
# ASCII de 40).
BOUNDARY_CASES = [("TERMS", 2543), ("WELCOME_STUDENT", 3885), ("TERMS_REFUSED", 3677),
                  ("INVITE_REQUESTED", 3632)]


@pytest.mark.parametrize(("name", "fits"), BOUNDARY_CASES)
def test_text_boundary_at_4096_utf16_units(name: str, fits: int) -> None:
    """Corte para 4.096 unidades UTF-16: com disciplina no BMP (1
    unidade por caractere), o texto de exatamente 4.096 unidades sai inteiro; com um caractere a mais
    (4.097), a disciplina perde dois caracteres e ganha `…` (4.096 unidades), e o período sai
    inteiro."""
    exact = compose(name, course=COURSE_BMP[:fits], term=TERM_BMP_40)
    assert exact == _expected(name, COURSE_BMP[:fits], TERM_BMP_40)
    assert _utf16_units(exact) == TEXT_LIMIT
    over = compose(name, course=COURSE_BMP[:fits + 1], term=TERM_BMP_40)
    assert over == _expected(name, COURSE_BMP[:fits - 1] + ELLIPSIS, TERM_BMP_40)
    assert _utf16_units(over) == TEXT_LIMIT


def _is_word(char: str) -> bool:
    return re.fullmatch(r"\w", char) is not None


@pytest.mark.parametrize("course", [COURSE, COURSE_ASTRAL_2500], ids=["curta", "cortada"])
def test_terms_commands_are_surrounded_by_non_word_characters(course: str) -> None:
    """O aluno aceita com `/aceito` e recusa com `/recuso` (o
    `TERMS` termina nas duas linhas das opções; `/aceito` e `/recuso` no termo
    cercados por caracteres que não são de palavra): cada comando aparece uma vez, com um caractere
    que não é de palavra antes e o fim do texto ou um caractere que não é de palavra depois; com a
    disciplina cortada, as duas linhas finais continuam inteiras."""
    text = compose("TERMS", course=course, term=TERM_40)
    assert text.endswith("\n\n" + _TERMS_TAIL)
    for command in ("/aceito", "/recuso"):
        starts = [match.start() for match in re.finditer(re.escape(command), text)]
        assert len(starts) == 1, (command, starts)
        start, end = starts[0], starts[0] + len(command)
        assert start > 0 and not _is_word(text[start - 1]), command
        assert end == len(text) or not _is_word(text[end]), command


# --- e-mail do convite -----------------------------------------------------------

LINK = "https://t.me/cq_exemplo_bot?start=AbCdEfGhIjKlMnOpQrStUv"
VALIDITY = "2026-10-03 14:00 -03 (America/Sao_Paulo)"


def test_invite_email_subject_and_body_exact() -> None:
    """O e-mail (assunto e corpo fixos em português, com a disciplina e o período, o link
    `https://t.me/<bot>?start=<código>`, a validade no fuso e a instrução do `/convite`; sem o nome nem
    o RA do aluno; `compose_invite_email(*, course, term,
    link, validity) -> tuple[str, str]`): a função só recebe esses quatro argumentos nomeados (nenhum
    nome de aluno) e devolve a tupla (assunto, corpo) exata."""
    params = inspect.signature(compose_invite_email).parameters.values()
    assert [(p.name, p.kind) for p in params] == [
        ("course", inspect.Parameter.KEYWORD_ONLY),
        ("term", inspect.Parameter.KEYWORD_ONLY),
        ("link", inspect.Parameter.KEYWORD_ONLY),
        ("validity", inspect.Parameter.KEYWORD_ONLY),
    ]
    result = compose_invite_email(course=COURSE, term=TERM, link=LINK, validity=VALIDITY)
    assert type(result) is tuple
    assert result == (
        "Seu link para o bot do oferecimento MC102 — Algoritmos e Programação de Computadores "
        "(2026, 2º semestre)",
        _expected_body(COURSE, TERM, LINK, VALIDITY),
    )
    assert result[1].split("\n")[4] == LINK


# (id, disciplina, período, disciplina no assunto, período no assunto).
SUBJECT_CASES = [
    ("crlf-e-tab", "A\r\nB\tC", "2026s2", "A B C", "2026s2"),
    ("pontas", "\n MC102 \t", "\r2026s2\r\n", "MC102", "2026s2"),
    ("separadores-unicode", "Linha\u2028Parágrafo\u2029Fim", "2026\x0bs2", "Linha Parágrafo Fim",
     "2026 s2"),
    ("controles-c0-c1", "A\x00B\x1fC\x7fD\x85E", "T\x0cU", "A B C D E", "T U"),
    ("espacos-repetidos", "MC102  -   Turma", "2026   s2", "MC102 - Turma", "2026 s2"),
    ("sequencia-mista", "A \r\n \t B", "x\u2028\u2029y", "A B", "x y"),
]


@pytest.mark.parametrize(("course", "term", "subject_course", "subject_term"),
                         [(c, t, sc, st) for _, c, t, sc, st in SUBJECT_CASES],
                         ids=[i for i, *_ in SUBJECT_CASES])
def test_invite_email_subject_normalizes_controls_and_body_does_not(
        course: str, term: str, subject_course: str, subject_term: str) -> None:
    """O assunto (no assunto, os caracteres de controle e as quebras de linha da disciplina e do período
    (categorias Unicode `Cc`, `Zl` e `Zp`) viram espaço, os espaços repetidos viram um só e as pontas
    são aparadas; o corpo não muda; `"A\\r\\nB\\tC"` → `A B C`): CR, LF, TAB,
    VT, FF, NUL, US, DEL, NEL, U+2028 e U+2029 viram espaço no assunto, com as sequências de espaço
    reduzidas a um e as pontas aparadas, na disciplina e no período; o corpo leva os valores
    recebidos, sem a troca."""
    subject, body = compose_invite_email(course=course, term=term, link=LINK, validity=VALIDITY)
    assert subject == _expected_subject(subject_course, subject_term)
    assert body == _expected_body(course, term, LINK, VALIDITY)


def test_invite_email_never_cuts_the_course_and_keeps_braces_literal() -> None:
    """Disciplina e período redigidos (sem corte: o e-mail não tem limite de
    4.096); o `_fill` dos textos do bot (o que vem nos valores fica literal): com uma disciplina de 5.000
    unidades UTF-16, o assunto e o corpo a levam inteira, sem `…`; marcadores e chaves na disciplina e
    no período (`{term}`, `{link}`, `{course}`, `{validity}`, `{0}`) saem exatamente como vieram."""
    subject, body = compose_invite_email(course=COURSE_BMP, term=TERM_BMP_40, link=LINK,
                                         validity=VALIDITY)
    assert subject == _expected_subject(COURSE_BMP, TERM_BMP_40)
    assert body == _expected_body(COURSE_BMP, TERM_BMP_40, LINK, VALIDITY)
    assert ELLIPSIS not in subject + body

    course, term = "Álgebra {term} {link} {0}", "{course} {validity}"
    assert compose_invite_email(course=course, term=term, link=LINK, validity=VALIDITY) == (
        "Seu link para o bot do oferecimento Álgebra {term} {link} {0} ({course} {validity})",
        _expected_body(course, term, LINK, VALIDITY),
    )


# --- validade no fuso ------------------------------------------------------------

# (id, instante em UTC, fuso, texto esperado).
FORMAT_CASES = [
    ("sao-paulo", datetime(2026, 10, 3, 17, 0, tzinfo=UTC), "America/Sao_Paulo",
     "2026-10-03 14:00 -03 (America/Sao_Paulo)"),
    ("sao-paulo-dia-anterior", datetime(2026, 10, 3, 2, 0, tzinfo=UTC), "America/Sao_Paulo",
     "2026-10-02 23:00 -03 (America/Sao_Paulo)"),
    ("sao-paulo-horario-de-verao-2018", datetime(2018, 11, 10, 12, 0, tzinfo=UTC),
     "America/Sao_Paulo", "2018-11-10 10:00 -02 (America/Sao_Paulo)"),
    ("sao-paulo-segundos-truncados", datetime(2026, 10, 3, 17, 0, 59, 999999, tzinfo=UTC),
     "America/Sao_Paulo", "2026-10-03 14:00 -03 (America/Sao_Paulo)"),
    ("kolkata-meia-hora", datetime(2026, 10, 3, 17, 0, tzinfo=UTC), "Asia/Kolkata",
     "2026-10-03 22:30 IST (Asia/Kolkata)"),
    ("kolkata-ano-seguinte", datetime(2026, 12, 31, 20, 45, tzinfo=UTC), "Asia/Kolkata",
     "2027-01-01 02:15 IST (Asia/Kolkata)"),
    ("utc", datetime(2026, 10, 3, 17, 0, tzinfo=UTC), "UTC", "2026-10-03 17:00 UTC (UTC)"),
]


@pytest.mark.parametrize(("moment", "zone", "expected"),
                         [(m, z, e) for _, m, z, e in FORMAT_CASES],
                         ids=[i for i, *_ in FORMAT_CASES])
def test_format_local_in_offering_timezone(moment: datetime, zone: str, expected: str) -> None:
    """O e-mail mostra a validade no fuso do oferecimento (`AAAA-MM-DD HH:MM
    <abreviação> (<fuso>)`, ex.: `2026-10-03 14:00 -03 (America/Sao_Paulo)`, o mesmo formato do CLI
    com o fuso entre parênteses, que trunca os segundos; `format_local(moment,
    zone)`): `America/Sao_Paulo` (inclusive a troca de dia e o horário de verão de 2018, `-02`),
    `Asia/Kolkata` (meia hora, troca de ano) e `UTC`."""
    assert format_local(moment, zone) == expected
