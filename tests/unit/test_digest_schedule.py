"""Agenda do resumo diário.


As duas funções puras de `cq_server.digests`, com `zoneinfo`:
- `next_occurrence(after, local_time, zone, *, strictly_after=False) -> datetime`: a primeira ocorrência,
  em UTC, do horário local `local_time` no fuso `zone` **em ou depois** de `after` (estritamente depois com
  `strictly_after`: ligar depois do horário de hoje → amanhã); o horário que não existe (o salto do
  horário de verão) ou que se repete (a volta) é lido com `fold = 0`: no salto, com o deslocamento
  de antes da transição; na volta, a primeira passagem, e o horário repetido ocorre **uma vez** no dia;
- `local_day_bounds(day, zone) -> (início, fim)`: os limites `[início, fim)` em UTC da data local `day`,
  com 23 ou 25 horas nos dias de transição.

Os valores esperados são literais calculados à mão (os deslocamentos de cada fuso na data), nunca pela
mesma fórmula do código: `America/Sao_Paulo` sem horário de verão desde 2019 (UTC−3) e com ele no
histórico (2018-11-04, o salto à meia-noite; 2019-02-16/17, a volta à meia-noite); `America/New_York` em
2026 (salto em 8 de março às 02:00, volta em 1º de novembro às 02:00). Todo horário devolvido é `datetime`
com fuso, deslocamento zero (todo horário é `datetime` com fuso UTC).

**No esqueleto:** as duas funções lançam `NotImplementedError`. As constantes reais do
esqueleto (`DEFAULT_LOCAL_TIME`, a regex `LOCAL_TIME`) são afirmadas na mesma função de um caso de
`next_occurrence`, que falha antes delas.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cq_server import digests

UTC = timezone.utc
SAO_PAULO = "America/Sao_Paulo"
NEW_YORK = "America/New_York"


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def _assert_utc(moment: datetime) -> None:
    assert isinstance(moment, datetime) and moment.tzinfo is not None, f"horário sem fuso: {moment!r}"
    assert moment.utcoffset() == timedelta(0), f"horário fora de UTC: {moment!r}"


# ---------------------------------------------------------------------------------------------
# next_occurrence
# ---------------------------------------------------------------------------------------------


def test_next_occurrence_before_the_time_is_today_and_default_constants() -> None:
    """Às 09:00 de São Paulo (12:00 UTC), o horário 20:00 é hoje, 23:00 UTC; o
    `DEFAULT_LOCAL_TIME` é `"20:00"` e a regex `LOCAL_TIME` aceita só `HH:MM` de 00:00 a 23:59 (as
    constantes reais do esqueleto ficam na mesma função deste caso)."""
    got = digests.next_occurrence(_utc(2026, 10, 21, 12, 0), "20:00", SAO_PAULO)
    assert got == _utc(2026, 10, 21, 23, 0)
    _assert_utc(got)

    assert digests.DEFAULT_LOCAL_TIME == "20:00"
    for good in ("00:00", "07:05", "19:59", "20:00", "23:59"):
        assert digests.LOCAL_TIME.fullmatch(good), good
    for bad in ("24:00", "7h", "7:00", "20:0", "20:60", "2000", " 20:00", "20:00 ", "20:00\n", "", "２０:００"):
        assert not digests.LOCAL_TIME.fullmatch(bad), bad


def test_next_occurrence_after_the_time_is_tomorrow() -> None:
    """Às 20:30 de São Paulo (23:30 UTC), o horário 20:00 já passou: a próxima
    ocorrência é amanhã, 2026-10-22 23:00 UTC; o mesmo vale um minuto depois do horário."""
    assert digests.next_occurrence(_utc(2026, 10, 21, 23, 30), "20:00", SAO_PAULO) == _utc(2026, 10, 22, 23, 0)
    assert digests.next_occurrence(_utc(2026, 10, 21, 23, 1), "20:00", SAO_PAULO) == _utc(2026, 10, 22, 23, 0)


def test_next_occurrence_at_the_exact_time_and_strictly_after() -> None:
    """No instante exato do horário, a ocorrência é o próprio instante (a
    primeira ≥ `after`); com `strictly_after`, a de amanhã (o avanço depois do resumo do dia). Antes do
    horário, `strictly_after` não muda nada."""
    exact = _utc(2026, 10, 21, 23, 0)
    assert digests.next_occurrence(exact, "20:00", SAO_PAULO) == exact
    assert digests.next_occurrence(exact, "20:00", SAO_PAULO, strictly_after=True) == _utc(2026, 10, 22, 23, 0)
    assert digests.next_occurrence(_utc(2026, 10, 21, 22, 59), "20:00", SAO_PAULO,
                                   strictly_after=True) == exact


def test_next_occurrence_across_the_date_turn() -> None:
    """A data local e a data UTC diferem. Às 22:00 de São Paulo, a ocorrência cai no dia
    seguinte em UTC (01:00); às 00:30, logo depois da meia-noite local (que é 03:00 UTC); e às 23:50 locais
    de 21/10 (02:50 UTC de 22/10), o 00:30 seguinte é o de 22/10 local, 03:30 UTC de 22/10."""
    assert digests.next_occurrence(_utc(2026, 10, 21, 12, 0), "22:00", SAO_PAULO) == _utc(2026, 10, 22, 1, 0)
    assert digests.next_occurrence(_utc(2026, 10, 21, 3, 0), "00:30", SAO_PAULO) == _utc(2026, 10, 21, 3, 30)
    assert digests.next_occurrence(_utc(2026, 10, 22, 2, 50), "00:30", SAO_PAULO) == _utc(2026, 10, 22, 3, 30)
    # A meia-noite local de 31/12 para 1º/01 (virada de ano), em UTC 03:00 de 1º/01.
    assert digests.next_occurrence(_utc(2026, 12, 31, 23, 0), "00:00", SAO_PAULO) == _utc(2027, 1, 1, 3, 0)


@pytest.mark.parametrize(("after", "local_time", "zone", "expected"), [
    # Nova York, salto de 2026-03-08 às 02:00 (EST, UTC−5) para 03:00 (EDT, UTC−4): 02:30 não existe; com
    # `fold = 0`, vale o deslocamento de antes da transição (UTC−5): 07:30 UTC.
    (_utc(2026, 3, 8, 5, 0), "02:30", NEW_YORK, _utc(2026, 3, 8, 7, 30)),
    # São Paulo histórico, salto de 2018-11-04 à meia-noite (UTC−3) para 01:00 (UTC−2): 00:30 não existe;
    # com `fold = 0`, UTC−3: 03:30 UTC.
    (_utc(2018, 11, 3, 12, 0), "00:30", SAO_PAULO, _utc(2018, 11, 4, 3, 30)),
], ids=["new-york-2026-03-08", "sao-paulo-2018-11-04"])
def test_next_occurrence_nonexistent_local_time_uses_fold_zero(after: datetime, local_time: str, zone: str,
                                                              expected: datetime) -> None:
    """O horário que não existe no dia do salto do horário de verão sai uma vez, com o
    deslocamento de antes da transição (`fold = 0`), sem erro e sem pular o dia."""
    got = digests.next_occurrence(after, local_time, zone)
    assert got == expected
    _assert_utc(got)


@pytest.mark.parametrize(("after", "local_time", "zone", "first", "next_day"), [
    # Nova York, volta de 2026-11-01 às 02:00 (EDT, UTC−4) para 01:00 (EST, UTC−5): 01:30 ocorre duas
    # vezes (05:30 e 06:30 UTC); com `fold = 0`, a primeira (05:30 UTC). O dia seguinte já é EST: 06:30 UTC.
    (_utc(2026, 11, 1, 4, 0), "01:30", NEW_YORK, _utc(2026, 11, 1, 5, 30), _utc(2026, 11, 2, 6, 30)),
    # São Paulo histórico, volta de 2019-02-17 à meia-noite (UTC−2) para 23:00 de 16/02 (UTC−3): 23:30 de
    # 16/02 ocorre duas vezes (01:30 e 02:30 UTC de 17/02); com `fold = 0`, a primeira. O dia seguinte
    # (17/02, UTC−3): 02:30 UTC de 18/02.
    (_utc(2019, 2, 16, 12, 0), "23:30", SAO_PAULO, _utc(2019, 2, 17, 1, 30), _utc(2019, 2, 18, 2, 30)),
], ids=["new-york-2026-11-01", "sao-paulo-2019-02-16"])
def test_next_occurrence_ambiguous_local_time_happens_once(after: datetime, local_time: str, zone: str,
                                                           first: datetime, next_day: datetime) -> None:
    """O horário repetido na volta do horário de verão é a primeira passagem (`fold = 0`) e
    ocorre **uma vez** no dia: estritamente depois dela, ou entre as duas passagens, a próxima ocorrência é
    a do dia seguinte, não a segunda passagem (no máximo um resumo por data local)."""
    got = digests.next_occurrence(after, local_time, zone)
    assert got == first
    _assert_utc(got)
    assert digests.next_occurrence(first, local_time, zone, strictly_after=True) == next_day
    # Entre as duas passagens (45 min depois da primeira, ainda antes da segunda, 1 h depois).
    between = first + timedelta(minutes=45)
    assert digests.next_occurrence(between, local_time, zone) == next_day


def test_next_occurrence_ignores_the_zone_of_after() -> None:
    """`after` com outro fuso (o mesmo instante) dá o mesmo resultado, em UTC."""
    from zoneinfo import ZoneInfo

    moment = _utc(2026, 10, 21, 12, 0).astimezone(ZoneInfo("Asia/Tokyo"))
    got = digests.next_occurrence(moment, "20:00", SAO_PAULO)
    assert got == _utc(2026, 10, 21, 23, 0)
    _assert_utc(got)


# ---------------------------------------------------------------------------------------------
# local_day_bounds
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("day", "zone", "start", "end"), [
    # São Paulo sem horário de verão (UTC−3): 24 h, de 03:00 UTC a 03:00 UTC.
    (date(2026, 10, 21), SAO_PAULO, _utc(2026, 10, 21, 3, 0), _utc(2026, 10, 22, 3, 0)),
    # Nova York num dia comum de verão (EDT, UTC−4).
    (date(2026, 7, 1), NEW_YORK, _utc(2026, 7, 1, 4, 0), _utc(2026, 7, 2, 4, 0)),
    # Nova York no dia do salto: começa em EST (05:00 UTC) e termina em EDT (04:00 UTC): 23 h.
    (date(2026, 3, 8), NEW_YORK, _utc(2026, 3, 8, 5, 0), _utc(2026, 3, 9, 4, 0)),
    # Nova York no dia da volta: começa em EDT (04:00 UTC) e termina em EST (05:00 UTC): 25 h.
    (date(2026, 11, 1), NEW_YORK, _utc(2026, 11, 1, 4, 0), _utc(2026, 11, 2, 5, 0)),
    # São Paulo histórico, a volta à meia-noite de 17/02: o dia 16/02 começa em UTC−2 e termina em UTC−3.
    (date(2019, 2, 16), SAO_PAULO, _utc(2019, 2, 16, 2, 0), _utc(2019, 2, 17, 3, 0)),
    # São Paulo histórico, o salto à meia-noite de 04/11: a meia-noite não existe; o dia começa às 01:00
    # locais (03:00 UTC) e termina à meia-noite seguinte em UTC−2 (02:00 UTC): 23 h.
    (date(2018, 11, 4), SAO_PAULO, _utc(2018, 11, 4, 3, 0), _utc(2018, 11, 5, 2, 0)),
    # UTC: o próprio dia.
    (date(2026, 10, 21), "UTC", _utc(2026, 10, 21, 0, 0), _utc(2026, 10, 22, 0, 0)),
], ids=["sao-paulo", "new-york-summer", "new-york-spring-forward", "new-york-fall-back",
        "sao-paulo-2019-fall-back", "sao-paulo-2018-spring-forward", "utc"])
def test_local_day_bounds_in_utc(day: date, zone: str, start: datetime, end: datetime) -> None:
    """A atividade do dia pela data local, com os limites em UTC calculados
    em Python: `[início, fim)` em UTC, com 23 h ou 25 h nos dias de transição."""
    got = digests.local_day_bounds(day, zone)
    assert got == (start, end)
    assert isinstance(got, tuple) and len(got) == 2
    for moment in got:
        _assert_utc(moment)


def test_local_day_bounds_are_contiguous_across_days() -> None:
    """O fim de um dia é o início do seguinte (nenhum instante fica sem data local nem
    em duas), inclusive nos dias de transição."""
    for zone, first in ((NEW_YORK, date(2026, 3, 6)), (NEW_YORK, date(2026, 10, 30)),
                        (SAO_PAULO, date(2019, 2, 14)), (SAO_PAULO, date(2018, 11, 2))):
        days = [first + timedelta(days=k) for k in range(5)]
        bounds = [digests.local_day_bounds(day, zone) for day in days]
        for (_, end), (start, _) in zip(bounds, bounds[1:], strict=False):
            assert end == start, (zone, bounds)
