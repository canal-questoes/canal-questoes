"""Respostas fixas do modo desligado das mensagens livres.


`cq_server.deliverytexts`, as composições do modo desligado:
- `answers_only_reply(email, *, buttons_open=False)`: `ANSWERS_ONLY_REPLY`; com `buttons_open`, mais
  `" " + USE_BUTTONS_HOWTO` (a pessoa só tem aberta escolha com botões);
  com contato, mais `" " + CONTACT_HOWTO.format(email=email)`;
- `use_reply_masked(email)`: no ambíguo, `USE_REPLY` seguido de `" " + CONTACT_HOWTO.format(email=email)`
  com contato, e só o `USE_REPLY` sem.

O e-mail entra **literal**, sem `formatter.escape`: a resposta fixa sai como texto puro, sem `parse_mode`
(`CONTACT_HOWTO`; `bots.py`). Por isso o e-mail do caso tem "&", que o escape de HTML
trocaria por "&amp;". Cada resposta cabe no limite de texto do canal (`botmessages.TEXT_MAX_UTF16`).

Os textos esperados são literais escritos aqui, e não as constantes do módulo. As funções não
existem no esqueleto: o caso falha com `AttributeError` em `deliverytexts`.
"""

from __future__ import annotations

from cq_server import botmessages, deliverytexts

# Textos esperados, literais.
ANSWERS_ONLY_REPLY = "Este canal recebe só respostas às questões."
USE_BUTTONS_HOWTO = 'Para responder, use os botões da questão ou toque em "Responder" na mensagem dela.'
USE_REPLY = ('Há mais de uma questão aberta esperando texto. Toque em "Responder" na mensagem da questão '
             'certa e digite a resposta.')
EMAIL = "a&b@x.org"
CONTACT = "Para falar com o professor, escreva para a&b@x.org."


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_answers_only_reply_with_and_without_contact() -> None:
    """Resposta fora do canal, com e sem contato
    (`use_reply_masked`; `buttons_open`):
    - `answers_only_reply("a&b@x.org")` == "Este canal recebe só respostas às questões. Para falar com o
      professor, escreva para a&b@x.org." (literal, sem escape); `answers_only_reply(None)` == a 1ª frase;
    - com `buttons_open=True`, a frase `USE_BUTTONS_HOWTO` entre as duas, e só as duas primeiras sem
      contato;
    - `use_reply_masked("a&b@x.org")` == `USE_REPLY` + " " + a frase do contato; `use_reply_masked(None)`
      == `USE_REPLY`;
    - nenhuma com "&amp;", e todas cabem no limite de texto do canal."""
    assert deliverytexts.answers_only_reply(EMAIL) == f"{ANSWERS_ONLY_REPLY} {CONTACT}"
    assert deliverytexts.answers_only_reply(None) == ANSWERS_ONLY_REPLY
    assert deliverytexts.answers_only_reply(EMAIL, buttons_open=True) == (
        f"{ANSWERS_ONLY_REPLY} {USE_BUTTONS_HOWTO} {CONTACT}")
    assert deliverytexts.answers_only_reply(None, buttons_open=True) == (
        f"{ANSWERS_ONLY_REPLY} {USE_BUTTONS_HOWTO}")
    assert deliverytexts.answers_only_reply(EMAIL, buttons_open=False) == f"{ANSWERS_ONLY_REPLY} {CONTACT}"
    assert deliverytexts.use_reply_masked(EMAIL) == f"{USE_REPLY} {CONTACT}"
    assert deliverytexts.use_reply_masked(None) == USE_REPLY
    assert deliverytexts.USE_BUTTONS_HOWTO == USE_BUTTONS_HOWTO

    texts = [deliverytexts.answers_only_reply(EMAIL), deliverytexts.answers_only_reply(None),
             deliverytexts.answers_only_reply(EMAIL, buttons_open=True),
             deliverytexts.answers_only_reply(None, buttons_open=True),
             deliverytexts.use_reply_masked(EMAIL), deliverytexts.use_reply_masked(None)]
    for text in texts:
        assert "&amp;" not in text, text
        assert 0 < _utf16(text) <= botmessages.TEXT_MAX_UTF16, text
