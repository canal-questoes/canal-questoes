"""Configuração dos limites do `doctor`.

`DoctorSettings`: os padrões (5 min para a consulta do bot e
as tarefas atrasadas, 5 s para o desvio do relógio), `from_env(env)` lendo **só** o mapeamento recebido
(`CQ_DOCTOR_BOT_POLL_SECONDS`, `CQ_DOCTOR_TASK_OVERDUE_SECONDS`, `CQ_DOCTOR_CLOCK_SKEW_SECONDS`; números positivos
finitos; ausente → padrão) e o `ValueError` sem eco do valor no inválido. `create_app` não lê o ambiente
(a mesma restrição de `tests/unit/test_config.py`): o `__main__` passa `DoctorSettings.from_env(os.environ)`.

**O que já passa:** os padrões de `DoctorSettings()` e "`create_app` não lê o ambiente" já valem no
esqueleto; por isso ficam na **mesma função** de um caso de `DoctorSettings.from_env`, que falha por
`NotImplementedError` até a implementação.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from cq_server.config import Settings
from cq_server.doctor import (
    DOCTOR_ENV_BOT_POLL,
    DOCTOR_ENV_CLOCK_SKEW,
    DOCTOR_ENV_TASK_OVERDUE,
    DoctorSettings,
)

# As três variáveis e o campo de cada uma.
VARIABLES = (
    ("CQ_DOCTOR_BOT_POLL_SECONDS", "bot_poll_seconds"),
    ("CQ_DOCTOR_TASK_OVERDUE_SECONDS", "task_overdue_seconds"),
    ("CQ_DOCTOR_CLOCK_SKEW_SECONDS", "clock_skew_seconds"),
)
DEFAULTS = {"bot_poll_seconds": 300, "task_overdue_seconds": 300, "clock_skew_seconds": 5.0}

# Valores recusados: zero, negativo, não finito e texto ("números positivos finitos"). Cada um tem uma
# marca própria, para conferir que a mensagem não o repete.
INVALID_VALUES = ("0", "-1", "-0.5", "nan", "NaN", "inf", "-inf", "Infinity", "cinco-minutos-sentinela",
                  "12abc", "1e999")
# Só os valores distintivos entram na conferência de eco (um "0" ou um "inf" podem aparecer numa mensagem fixa
# legítima, como "maior que 0" ou "informe").
ECHO_CHECKED = frozenset({"-0.5", "NaN", "Infinity", "cinco-minutos-sentinela", "12abc", "1e999"})


def _fields(settings: DoctorSettings) -> dict[str, float]:
    return {"bot_poll_seconds": settings.bot_poll_seconds, "task_overdue_seconds": settings.task_overdue_seconds,
            "clock_skew_seconds": settings.clock_skew_seconds}


def test_defaults_and_from_env_without_variables() -> None:
    """Os padrões do esqueleto (300, 300, 5.0) e `from_env` sem nenhuma `CQ_DOCTOR_*` devolvendo os mesmos;
    os nomes das variáveis exportados pelo núcleo são os esperados; variáveis alheias não contam."""
    # Já vale no esqueleto: os padrões e os nomes.
    assert _fields(DoctorSettings()) == DEFAULTS
    assert (DOCTOR_ENV_BOT_POLL, DOCTOR_ENV_TASK_OVERDUE, DOCTOR_ENV_CLOCK_SKEW) == tuple(v for v, _ in VARIABLES)
    # O caso que falha no esqueleto, na mesma função.
    assert DoctorSettings.from_env({}) == DoctorSettings()
    assert DoctorSettings.from_env({"CQ_DATABASE_URL": "postgresql://x@127.0.0.1:9/x", "CQ_DOCTOR": "1",
                                    "CQ_DOCTOR_OUTRA_SECONDS": "-1"}) == DoctorSettings()


@pytest.mark.parametrize(("variable", "field"), VARIABLES)
@pytest.mark.parametrize(("raw", "value"), [("120", 120.0), ("2.5", 2.5), ("0.001", 0.001), ("86400", 86400.0)])
def test_each_valid_variable_sets_only_its_field(variable: str, field: str, raw: str, value: float) -> None:
    """Cada variável válida muda só o seu campo; os outros ficam no padrão."""
    settings = DoctorSettings.from_env({variable: raw})
    assert _fields(settings) == {**DEFAULTS, field: value}
    assert all(isinstance(v, (int, float)) and math.isfinite(v) for v in _fields(settings).values())


def test_all_three_variables_together() -> None:
    settings = DoctorSettings.from_env({"CQ_DOCTOR_BOT_POLL_SECONDS": "600",
                                        "CQ_DOCTOR_TASK_OVERDUE_SECONDS": "90",
                                        "CQ_DOCTOR_CLOCK_SKEW_SECONDS": "1.5"})
    assert _fields(settings) == {"bot_poll_seconds": 600.0, "task_overdue_seconds": 90.0,
                                 "clock_skew_seconds": 1.5}


@pytest.mark.parametrize(("variable", "field"), VARIABLES)
@pytest.mark.parametrize("raw", INVALID_VALUES)
def test_invalid_value_is_value_error_without_echo(variable: str, field: str, raw: str) -> None:
    """Zero, negativo, `nan`, `inf`, texto e um número que transborda para `inf` → `ValueError`, sem o valor
    na mensagem nem no `repr` da exceção (o `__main__` o transforma em erro de configuração, código 2)."""
    with pytest.raises(ValueError) as caught:
        DoctorSettings.from_env({variable: raw})
    if raw in ECHO_CHECKED:
        for text in (str(caught.value), repr(caught.value)):
            assert raw not in text, f"o valor recusado foi ecoado: {text!r}"


def test_from_env_reads_only_the_given_mapping_and_create_app_ignores_environment(
        monkeypatch: pytest.MonkeyPatch, unreachable_db_url: str) -> None:
    """`from_env` lê só o mapeamento recebido; `create_app` não lê o ambiente do processo (a mesma restrição de
    `test_config.py`): com `CQ_DOCTOR_*` inválidas (negativas) no processo, o app monta sem erro, e `from_env`
    sobre o mesmo ambiente recusa, sem eco."""
    import os

    from cq_server.api import create_app

    for variable, _ in VARIABLES:
        monkeypatch.setenv(variable, "-7.25")
    # Já vale no esqueleto: o `create_app` aceita o argumento e não lê o ambiente.
    app: Any = create_app(Settings(database_url=unreachable_db_url, admin_token=None))
    assert app is not None
    app_with_settings: Any = create_app(Settings(database_url=unreachable_db_url, admin_token=None),
                                        doctor_settings=DoctorSettings(bot_poll_seconds=60))
    assert app_with_settings is not None
    # O caso que falha no esqueleto, na mesma função: o mapeamento recebido manda, o ambiente do processo não.
    assert DoctorSettings.from_env({}) == DoctorSettings()
    with pytest.raises(ValueError) as caught:
        DoctorSettings.from_env(dict(os.environ))
    assert "-7.25" not in str(caught.value)
