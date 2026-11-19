"""Contagem de controles das escolhas e o limiar de degradação.


A escolha única também tem o botão "Enviar", como a múltipla: os controles de uma escolha são
as alternativas mais 2 ("Enviar" e "Não sei") **nas duas escolhas** (ver
`deliveries.choice_has_buttons`). Uma escolha cujos controles passam de `max_buttons` do canal é
degradada: sem botões e digitável pela chave. A mesma conta vale na abertura (`deliveries.build_rendered`,
os derivados `buttons` e `typeable` de cada campo) e na recepção (`answers.field_typeable`, a ligação do
texto digitado).

Puro: a IR vem do motor falso (`tests/helpers/engine.py`, `build_variant`), a mesma que o motor falso
devolve na criação da variante, e o formatador é o de texto puro (o conteúdo dos blocos não importa aqui).

Vermelho esperado sem o "Enviar" na única: com `max_buttons = 5`, a única de 4 alternativas tem
botões (4 + 0 + 1 = 5 ≤ 5) e não é digitável. Os
controles (`max_buttons = 6` na única; a múltipla de 4 com `max_buttons = 5`) passam sempre
e confirmam que a conta da múltipla é a mesma e que a única com espaço continua com botões.
"""

from __future__ import annotations

from typing import Any

from cq_server.answers import field_typeable
from cq_server.deliveries import build_rendered
from cq_server.ports.delivery import ChannelCapabilities
from cq_server.telegramrender import PlainFormatter
from tests.helpers.engine import build_variant, preset

SEED = "1abc"


def _caps(max_buttons: int) -> ChannelCapabilities:
    return ChannelCapabilities(can_edit=True, max_buttons=max_buttons, supports_math=False,
                               supports_rich_text=True)


def _ir(name: str) -> dict[str, Any]:
    _snapshot, ir, _warnings = build_variant(preset(name), SEED)
    assert len(ir["fields"]) == 1 and len(ir["fields"][0]["options"]) == 4
    return ir


def _field(ir: dict[str, Any], max_buttons: int) -> dict[str, Any]:
    rendered = build_rendered(ir, formatter=PlainFormatter(), capabilities=_caps(max_buttons))
    [field] = rendered["fields"]
    return field


def test_choice_controls_are_alternatives_plus_submit_and_dont_know_in_both_choices() -> None:
    """A escolha única de 4 alternativas precisa de 6 controles (4 alternativas,
    "Enviar" e "Não sei"). Com `max_buttons = 5` ela é degradada (sem botões, digitável) na abertura e na
    recepção; com `max_buttons = 6`, tem botões e não é digitável (controle). Na mesma função, o controle
    da múltipla de 4, que conta o "Enviar" (4 + 1 + 1 = 6): com `max_buttons = 5` continua
    degradada, e com 6 continua com botões. Uma função só, para que os controles, que já passam, fiquem junto do
    caso que falha sem o "Enviar" na única."""
    single = _ir("single_choice")
    assert single["fields"][0]["type"] == "single_choice"
    degraded = _field(single, 5)
    assert (degraded["buttons"], degraded["typeable"]) == (False, True), (
        "a única de 4 alternativas com max_buttons = 5 devia ser degradada (4 + Enviar + Não sei = 6)")
    assert field_typeable(single["fields"][0], _caps(5)) is True
    with_buttons = _field(single, 6)
    assert (with_buttons["buttons"], with_buttons["typeable"]) == (True, False)
    assert field_typeable(single["fields"][0], _caps(6)) is False

    multiple = _ir("multiple_choice")
    assert multiple["fields"][0]["type"] == "multiple_choice"
    degraded = _field(multiple, 5)
    assert (degraded["buttons"], degraded["typeable"]) == (False, True)
    assert field_typeable(multiple["fields"][0], _caps(5)) is True
    with_buttons = _field(multiple, 6)
    assert (with_buttons["buttons"], with_buttons["typeable"]) == (True, False)
    assert field_typeable(multiple["fields"][0], _caps(6)) is False
