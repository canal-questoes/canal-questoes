"""Máscara do texto do `/convite <e-mail>`: o reconhecimento do comando, a máscara e os resíduos
quando o Telegram não marca o comando.

`cq_server.students.invite_request_mask(text, command)` é uma função pura: devolve o texto que fica
guardado em `telegram_updates.text` depois do processamento de um `/convite`, ou `None` quando o texto
não muda. Regras:
- **prefixo:** o `command` gravado (o trecho coberto pela entidade `bot_command` no deslocamento 0, como
  foi escrito, ex.: `/convite`, `/convite@Bot`), cujo nome (antes do `@`) é `/convite` sem diferenciar
  maiúsculas; sem `command` (a entidade pode faltar), o trecho casado por
  `^/convite(@[A-Za-z0-9_]+)?(\\s|$)` sem diferenciar maiúsculas, como foi escrito
  e sem o espaço;
- **argumento:** `text[len(prefixo):].strip()` (o resto do texto, inclusive depois de quebra de linha);
- **máscara:** se o argumento não é vazio, `f"{prefixo} [e-mail]"`, em qualquer chat e para qualquer bot
  de destino (`/convite@outro_bot` também); senão, ou se o comando é outro, ou se não há texto,
  `None`.

O argumento nunca aparece na máscara (e-mail válido ou não, um ou vários). No esqueleto,
`invite_request_mask` lança `NotImplementedError`, e todo teste daqui passa por ela.
"""

from __future__ import annotations

import pytest

from cq_server.students import invite_request_mask

EMAIL = "a@b.example.org"

# --- com a entidade `bot_command` (o `command` gravado) --------------------------------------------

# (id, texto, comando, resultado esperado)
WITH_COMMAND: list[tuple[str, str | None, str | None, str | None]] = [
    ("convite", f"/convite {EMAIL}", "/convite", "/convite [e-mail]"),
    ("convite-arroba-bot", f"/convite@Bot {EMAIL}", "/convite@Bot", "/convite@Bot [e-mail]"),
    ("convite-maiuscula", f"/Convite {EMAIL}", "/Convite", "/Convite [e-mail]"),
    ("convite-tudo-maiusculo-arroba-bot", f"/CONVITE@Canal_Bot2 {EMAIL}", "/CONVITE@Canal_Bot2",
     "/CONVITE@Canal_Bot2 [e-mail]"),
    ("convite-quebra-de-linha", f"/convite\n{EMAIL}", "/convite", "/convite [e-mail]"),
    ("convite-espacos-nas-pontas", f"/convite   {EMAIL}  \n", "/convite", "/convite [e-mail]"),
    ("convite-dois-enderecos", f"/convite {EMAIL}\noutro@c.example.net", "/convite",
     "/convite [e-mail]"),
    ("convite-argumento-que-nao-e-email", "/convite quero entrar na turma", "/convite",
     "/convite [e-mail]"),
    ("convite-outro-bot", f"/convite@outro_bot {EMAIL}", "/convite@outro_bot",
     "/convite@outro_bot [e-mail]"),
    ("convite-sem-argumento", "/convite", "/convite", None),
    ("convite-so-espacos", "/convite   ", "/convite", None),
    ("convite-so-brancos-e-quebra", "/convite \n \t ", "/convite", None),
    ("convite-arroba-bot-sem-argumento", "/convite@Bot", "/convite@Bot", None),
    ("outro-comando-start", f"/start {EMAIL}", "/start", None),
    ("outro-comando-aceito", f"/aceito {EMAIL}", "/aceito", None),
    ("outro-comando-ajuda-arroba-bot", f"/ajuda@Bot {EMAIL}", "/ajuda@Bot", None),
    ("outro-comando-convites", f"/convites {EMAIL}", "/convites", None),
    ("texto-nulo-com-comando", None, "/convite", None),
    ("texto-nulo", None, None, None),
]


@pytest.mark.parametrize(("text", "command", "expected"),
                         [(t, c, e) for _, t, c, e in WITH_COMMAND],
                         ids=[i for i, *_ in WITH_COMMAND])
def test_mask_with_bot_command_entity(text: str | None, command: str | None,
                                      expected: str | None) -> None:
    """Regras da máscara (nome do comando sem diferenciar maiúsculas só em `/aceito`, `/recuso` e
    `/convite`; máscara `f"{command} [e-mail]"` quando há resto, em qualquer chat e para
    qualquer bot de destino, com o `command` gravado mantido; argumento =
    `text[len(prefixo):].strip()`, inclusive depois de quebra de linha): `/convite a@b.example.org`,
    `/convite@Bot a@…`, `/Convite a@…`, `"/convite\\na@…"` e `/convite@outro_bot a@…` → a máscara exata;
    `/convite` sem argumento ou só com espaços, outro comando e `text=None` → `None`."""
    assert invite_request_mask(text, command) == expected


# --- sem a entidade (`command=None`) ---------------------------------------------------------------

# (id, texto, resultado esperado); o `command` é sempre `None`.
WITHOUT_COMMAND: list[tuple[str, str, str | None]] = [
    ("convite", f"/convite {EMAIL}", "/convite [e-mail]"),
    ("convite-maiusculas", f"/CONVITE {EMAIL}", "/CONVITE [e-mail]"),
    ("convite-caixa-mista", f"/Convite {EMAIL}", "/Convite [e-mail]"),
    ("convite-arroba-bot", f"/convite@Bot {EMAIL}", "/convite@Bot [e-mail]"),
    ("convite-maiuscula-arroba-bot", f"/Convite@Canal_Bot2 {EMAIL}", "/Convite@Canal_Bot2 [e-mail]"),
    ("convite-outro-bot", f"/convite@outro_bot {EMAIL}", "/convite@outro_bot [e-mail]"),
    ("convite-quebra-de-linha", f"/convite\n{EMAIL}", "/convite [e-mail]"),
    ("convite-tabulacao", f"/convite\t{EMAIL}", "/convite [e-mail]"),
    ("convite-arroba-bot-quebra-de-linha", f"/convite@Bot\n{EMAIL}", "/convite@Bot [e-mail]"),
    ("convite-argumento-que-nao-e-email", "/convite quero entrar", "/convite [e-mail]"),
    ("convite-sem-argumento", "/convite", None),
    ("convite-so-espacos", "/convite   ", None),
    ("convite-arroba-bot-sem-argumento", "/convite@Bot", None),
    ("convites-nao-casa", f"/convites {EMAIL}", None),
    ("convite-hifen-nao-casa", f"/convite-{EMAIL}", None),
    ("arroba-sem-bot-nao-casa", f"/convite@ {EMAIL}", None),
    ("espaco-antes-nao-casa", f" /convite {EMAIL}", None),
    ("texto-comum", f"quero um convite: {EMAIL}", None),
    ("so-o-email", EMAIL, None),
    ("outro-comando", f"/start {EMAIL}", None),
    ("texto-vazio", "", None),
]


@pytest.mark.parametrize(("text", "expected"), [(t, e) for _, t, e in WITHOUT_COMMAND],
                         ids=[i for i, *_ in WITHOUT_COMMAND])
def test_mask_without_bot_command_entity(text: str, expected: str | None) -> None:
    """Máscara também quando o Telegram não marca o comando (sem `command`, o texto que
    casa `^/convite(@[A-Za-z0-9_]+)?(\\s|$)` sem diferenciar maiúsculas recebe a mesma máscara, com o
    trecho casado como foi escrito, sem o espaço): `/convite a@…` e `/CONVITE a@…` →
    a máscara; `/convite` sem argumento e texto que não casa (ex.: `/convites a@…`, texto comum) →
    `None`."""
    assert invite_request_mask(text, None) == expected


def test_mask_never_carries_the_argument() -> None:
    """O e-mail digitado nunca fica no texto guardado: para o mesmo
    texto, com e sem a entidade, a máscara é a mesma e não contém nada do argumento (nem o e-mail, nem a
    parte local, nem o domínio)."""
    text = "/convite  Aluno.Teste+Convite@Dominio.example.org"
    with_entity = invite_request_mask(text, "/convite")
    without_entity = invite_request_mask(text, None)
    assert with_entity == "/convite [e-mail]"
    assert without_entity == with_entity
    for fragment in ("Aluno.Teste+Convite@Dominio.example.org", "Aluno.Teste", "Dominio.example.org", "@D"):
        assert fragment not in with_entity
