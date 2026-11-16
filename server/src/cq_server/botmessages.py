"""Textos fixos dos bots.

`{course}` e `{term}` recebem a disciplina e o período já redigidos: `compose`
não redige, substitui os dois marcadores sem `str.format` (outras chaves no texto ficam literais) e
só nas duas descrições corta a disciplina para o texto caber em `DESCRIPTION_MAX_UTF16` unidades
UTF-16, terminando em `…` (nunca o período). `LINK_INVALID`, `ALREADY_LINKED` e `OTHER_PERSON` são
usados sem `compose`.

Termo de uso e textos do aluno: `TERMS`, `WELCOME_STUDENT`,
`TERMS_REFUSED` e `INVITE_REQUESTED` passam por `compose`, com a disciplina cortada para o texto caber em
`TEXT_MAX_UTF16` unidades UTF-16; o `TERMS` tem também `{version}`, preenchido com o `TERMS_VERSION`
lido na hora da chamada, e `{course}`, `{term}` e `{version}` são trocados numa passada só.
`compose_invite_email` monta o assunto e o corpo do e-mail do convite, com a disciplina e o período
sem corte; no assunto, normalizados (`Cc`, `Zl` e `Zp` viram espaço, os espaços repetidos viram um
só e as pontas são aparadas); o corpo leva os valores como vieram.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable

from cq_server.ports.telegram import BotCommand

# Limite da descrição do bot, em unidades UTF-16.
DESCRIPTION_MAX_UTF16 = 512

DESCRIPTION_ACTIVE = (
    "Bot do oferecimento {course} ({term}). Para participar, toque no link de convite que você "
    "recebeu da coordenação."
)
DESCRIPTION_ARCHIVED = (
    "O oferecimento {course} ({term}) foi encerrado. Este bot não recebe mais mensagens."
)
COMMANDS = (
    BotCommand(command="start", description="Começar ou usar um link de convite"),
    BotCommand(command="ajuda", description="Ver como usar este bot"),
)
HELP = (
    "Este é o bot do oferecimento {course} ({term}). Para participar, toque no link de convite que "
    "você recebeu da coordenação. Se o link não funcionar, peça um novo à coordenação."
)
WELCOME_TEAM = (
    "Pronto! Você faz parte da equipe do oferecimento {course} ({term}). Por este chat você vai "
    "receber os avisos do oferecimento."
)
LINK_INVALID = "Este link não é válido ou já venceu. Peça um novo link à coordenação."
ALREADY_LINKED = "Você já está vinculado a este oferecimento."
OTHER_PERSON = (
    "Esta conta do Telegram já está vinculada a outra pessoa neste oferecimento. Fale com a "
    "coordenação."
)

# --- termo de uso e textos do aluno ------------------------------

# Versão do termo de uso; lida na hora do `compose` (os testes a trocam com `monkeypatch`).
TERMS_VERSION = 1

# Limite de uma mensagem do Telegram, em unidades UTF-16.
TEXT_MAX_UTF16 = 4096

TERMS = (
    "Termo de uso do bot do oferecimento {course} ({term}), versão {version}\n"
    "\n"
    "Leia até o fim: as opções para aceitar ou recusar estão no final.\n"
    "\n"
    "Para que serve. Por este bot você vai receber questões da disciplina e responder a elas. As "
    "respostas são registradas para o ensino na disciplina e para o piloto de um Trabalho de "
    "Conclusão de Curso (TCC) de Ciência da Computação da Unicamp, que avalia este sistema.\n"
    "\n"
    "Quais dados são guardados:\n"
    "• o seu nome, RA e e-mail, como estão na lista da turma enviada pela coordenação;\n"
    "• o número que identifica a sua conta do Telegram (o seu nome de usuário, o nome do perfil e "
    "o telefone não são guardados);\n"
    "• as suas respostas e as mensagens que você mandar a este bot.\n"
    "\n"
    "Quem vê. A equipe do oferecimento acompanha as respostas sem ver o seu nome: cada aluno "
    "aparece por um código. A ligação entre o código e o seu nome só é consultada quando "
    "necessário, e cada consulta fica registrada. No TCC, os resultados aparecem só de forma "
    "agregada, sem identificar ninguém.\n"
    "\n"
    "Por quanto tempo. Os dados ficam guardados pelo prazo da política de retenção deste "
    "oferecimento, definida pela coordenação. Depois desse prazo, os seus dados de identificação "
    "(nome, RA, e-mail e conta do Telegram) são apagados, e as respostas continuam guardadas sem "
    "eles.\n"
    "\n"
    "Se você recusar, nada é vinculado e você não recebe questões por este bot. Neste piloto, "
    "estas questões só são oferecidas por aqui. Se mudar de ideia, o link do e-mail continua "
    "valendo até vencer.\n"
    "\n"
    "Para aceitar, toque em /aceito\n"
    "Para recusar, toque em /recuso"
)
WELCOME_STUDENT = (
    "Pronto! Você aceitou o termo de uso e está vinculado ao oferecimento {course} ({term}). As "
    "questões da disciplina vão chegar por este chat. Se precisar de ajuda, fale com a coordenação."
)
TERMS_REFUSED = (
    "Tudo bem: você recusou o termo de uso. Nada foi vinculado, e você não vai receber questões do "
    "oferecimento {course} ({term}) por este chat.\n"
    "\n"
    "Se mudar de ideia, toque de novo no link do e-mail, nesta mesma conta do Telegram, enquanto "
    "ele valer. Se o link vencer, mande /convite seguido de um espaço e do seu e-mail "
    "institucional (o mesmo da lista da turma) para receber um novo link por e-mail."
)
INVITE_REQUESTED = (
    "Pedido recebido. Se o e-mail informado for de um aluno da lista do oferecimento {course} "
    "({term}) que ainda não se vinculou, um novo link de convite vai para esse e-mail, e o link "
    "anterior deixa de valer. O e-mail pode levar alguns minutos para chegar.\n"
    "\n"
    "Para pedir, mande /convite seguido de um espaço e do seu e-mail institucional, o mesmo da "
    "lista da turma. Há um limite de pedidos por dia. Se o link não chegar, fale com a coordenação."
)

# Os textos com `{course}` e `{term}` que `compose` monta, e os que ele corta (as descrições).
_TEMPLATES = {
    "DESCRIPTION_ACTIVE": DESCRIPTION_ACTIVE,
    "DESCRIPTION_ARCHIVED": DESCRIPTION_ARCHIVED,
    "HELP": HELP,
    "WELCOME_TEAM": WELCOME_TEAM,
}
_CUT_TO_FIT = frozenset({"DESCRIPTION_ACTIVE", "DESCRIPTION_ARCHIVED"})

# Os textos do aluno, todos cortados para caber numa mensagem do Telegram.
_STUDENT_TEMPLATES = {
    "TERMS": TERMS,
    "WELCOME_STUDENT": WELCOME_STUDENT,
    "TERMS_REFUSED": TERMS_REFUSED,
    "INVITE_REQUESTED": INVITE_REQUESTED,
}

_PLACEHOLDER = re.compile(r"\{(course|term)\}")
# `{version}` só no `TERMS`, trocado na mesma passada de `{course}` e `{term}`.
_TERMS_PLACEHOLDER = re.compile(r"\{(course|term|version)\}")
ELLIPSIS = "…"

# E-mail do convite.
INVITE_EMAIL_SUBJECT = "Seu link para o bot do oferecimento {course} ({term})"
INVITE_EMAIL_BODY = (
    "Olá!\n"
    "\n"
    "Você está na lista da turma do oferecimento {course} ({term}). As questões da disciplina "
    "chegam por um bot do Telegram, e este é o seu link pessoal para começar:\n"
    "\n"
    "{link}\n"
    "\n"
    "Abra o link no celular ou no computador em que você usa o Telegram e toque em \"Iniciar\". O "
    "bot vai mostrar o termo de uso, que você pode aceitar ou recusar.\n"
    "\n"
    "O link vale até {validity}. Não o repasse: ele é só seu.\n"
    "\n"
    "Se o link vencer ou se perder, mande ao bot a mensagem /convite seguida de um espaço e do seu "
    "e-mail institucional (o mesmo da lista da turma) para receber um novo link.\n"
    "\n"
    "Esta mensagem foi enviada automaticamente; não é preciso respondê-la."
)
_EMAIL_PLACEHOLDER = re.compile(r"\{(course|term|link|validity)\}")
# Categorias Unicode trocadas por espaço no assunto: controles, separadores de linha e de parágrafo.
_SUBJECT_BREAK_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})
_SPACES = re.compile(" {2,}")


def utf16_units(text: str) -> int:
    """O tamanho do texto em unidades UTF-16 (como o Telegram conta)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _fill(template: str, course: str, term: str) -> str:
    """Troca `{course}` e `{term}` numa passada só: o que vem nos valores fica literal (inclusive
    marcadores e chaves), e nenhuma outra chave do texto é interpretada."""
    values = {"course": course, "term": term}
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def _fill_terms(template: str, course: str, term: str, version: str) -> str:
    """Troca `{course}`, `{term}` e `{version}` numa passada só (o molde de `_fill`)."""
    values = {"course": course, "term": term, "version": version}
    return _TERMS_PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def _cut_course(fill: Callable[[str], str], course: str, limit: int) -> str:
    """O texto `fill(course)` se ele cabe em `limit` unidades UTF-16; senão, `fill` com o maior
    prefixo da disciplina (em caracteres, sem partir um par substituto) que, seguido de `…`, faz o
    texto caber. O resto do texto (inclusive o período) nunca é cortado."""
    text = fill(course)
    if utf16_units(text) <= limit:
        return text
    # Unidades do texto sem a disciplina, com o `…` no lugar dela.
    budget = limit - utf16_units(fill(ELLIPSIS))
    used = 0
    cut = 0
    for ch in course:
        size = 2 if ord(ch) > 0xFFFF else 1
        if used + size > budget:
            break
        used += size
        cut += 1
    return fill(course[:cut] + ELLIPSIS)


def compose(name: str, *, course: str, term: str) -> str:
    """O texto `name` com a disciplina e o período (ver o docstring do módulo); outro nome →
    `KeyError`.

    Nas descrições, se o texto passar de `DESCRIPTION_MAX_UTF16` unidades UTF-16, a disciplina é
    trocada pelo maior prefixo (em caracteres, sem partir um par substituto) que, seguido de `…`,
    faz o texto caber; o período nunca é cortado. Nos quatro textos do aluno, o mesmo corte para
    `TEXT_MAX_UTF16`; no `TERMS`, `{version}` recebe o `TERMS_VERSION` lido agora.
    """
    if name in _STUDENT_TEMPLATES:
        student_template = _STUDENT_TEMPLATES[name]
        if name == "TERMS":
            # Lida agora, e não na importação: o texto mostrado e a versão gravada coincidem.
            version = str(TERMS_VERSION)
            return _cut_course(lambda value: _fill_terms(student_template, value, term, version),
                               course, TEXT_MAX_UTF16)
        return _cut_course(lambda value: _fill(student_template, value, term), course,
                           TEXT_MAX_UTF16)
    template = _TEMPLATES[name]
    if name not in _CUT_TO_FIT:
        return _fill(template, course, term)
    return _cut_course(lambda value: _fill(template, value, term), course, DESCRIPTION_MAX_UTF16)


def _subject_value(value: str) -> str:
    """A disciplina ou o período para o assunto: cada caractere `Cc`, `Zl` ou `Zp` vira
    espaço, as sequências de espaços viram um só e as pontas são aparadas."""
    spaced = "".join(" " if unicodedata.category(ch) in _SUBJECT_BREAK_CATEGORIES else ch
                     for ch in value)
    return _SPACES.sub(" ", spaced).strip(" ")


def compose_invite_email(*, course: str, term: str, link: str, validity: str) -> tuple[str, str]:
    """O assunto e o corpo do e-mail do convite; `course` e `term` já redigidos.

    Sem corte da disciplina (o e-mail não tem o limite de uma mensagem do Telegram) e sem nome de
    aluno. Os marcadores são trocados numa passada só, e o que vem nos valores fica literal. Só o
    assunto passa pela normalização de `_subject_value`, e cada valor separadamente; o corpo leva os
    valores como vieram.
    """
    subject = _fill(INVITE_EMAIL_SUBJECT, _subject_value(course), _subject_value(term))
    values = {"course": course, "term": term, "link": link, "validity": validity}
    body = _EMAIL_PLACEHOLDER.sub(lambda match: values[match.group(1)], INVITE_EMAIL_BODY)
    return subject, body
