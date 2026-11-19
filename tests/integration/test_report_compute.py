"""Cálculo do relatório de um disparo, com os cenários e as situações-limite do cálculo.



`reports.compute_report(conn, dispatch_row_id, *, now, identities, formatter) -> Report |
DispatchStateConflict` lê só `dispatches`, `question_refs`, `deliveries`, `delivery_contents.ir` e
`submissions`; nunca o motor, o canal, o `answer_key`, a `explanation`, `correct_answers`,
`submitted_answers` nem `free_messages`.

**Montagem.** Cada teste grava, por SQL, num banco novo (migrado até a versão corrente), o oferecimento com
bot, os alunos (pessoa, matrícula `student` ativa e endereço ativo), a questão (`question_refs`), o disparo
no estado pedido, as entregas em cada estado (com a variante do motor falso, `build_variant`, em
`delivery_contents`, e a mensagem principal enviada quando o estado a exige) e as submissões (com o update de
origem). Assim cada número do relatório esperado é um **literal calculado à mão** das submissões gravadas pelo
próprio teste, nunca recalculado pela fórmula do código; os quartis com n ≥ 2 são conferidos contra
`statistics.quantiles(..., n=4, method="inclusive")` e a mediana contra `statistics.median`, com n = 0
(`None`) e n = 1 (o próprio valor) literais. As frações são comparadas com tolerância de arredondamento de
ponto flutuante (`pytest.approx`, relativa 1e-12); as contagens, por igualdade.

**Escolhas fixadas por este teste** ([I], nada no produto as fixa de outro jeito):
- o texto de um item de lista truncado em 200 caracteres segue o eco do disparo (`deliveries.py:251`):
  os primeiros 200 pontos de código seguidos de "…";
- sem nenhuma IR gravada (alvo vazio), `distribution.fields` e `friction.fields` são vazios;
- os campos da distribuição (índice, tipo, rótulo) vêm da IR das entregas; nos testes, todas as entregas de
  um disparo têm o mesmo rótulo por campo.

No esqueleto, `compute_report` lança `NotImplementedError`: todo caso falha por esse motivo.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools
import secrets
import statistics
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from cq_server.dispatches import DispatchStateConflict
from cq_server.redaction import KnownIdentity, person_marker
from cq_server.reports import (
    ChoiceItem,
    DispatchHeader,
    Distribution,
    FieldDistribution,
    FieldFriction,
    Friction,
    NotDeliveredByReason,
    OfferingRef,
    Pair,
    QuestionFailedByStage,
    QuestionHeader,
    RatePair,
    Reach,
    Report,
    TopItem,
    TopList,
    compute_report,
)
from cq_server.telegramrender import PlainFormatter, TelegramHtmlFormatter, TelegramRichFormatter
from tests.helpers.engine import (
    ANSWER_SENTINEL,
    SENTINEL_KEY,
    FakeOption,
    FakeQuestion,
    build_variant,
    integer_field,
    multiple_choice_field,
    number_field,
    preset,
    single_choice_field,
)

UTC = timezone.utc
ZONE = "America/Sao_Paulo"
T0 = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
OPENS = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
CLOSES = OPENS + timedelta(hours=1)
SENT = OPENS + timedelta(seconds=5)
ON_TIME = OPENS + timedelta(minutes=10)
LATE = CLOSES + timedelta(minutes=10)
NOW = CLOSES + timedelta(minutes=30)
COMMIT = "a193f72048530a4499252c454acca963cdf98922"
# Um aluno do oferecimento cujo nome aparece em respostas, alternativas e títulos (redigido).
NAME = "Fulana Beltrana de Tal"
# O texto de uma mensagem livre do aluno: nunca sai no relatório.
FREE_TEXT = "mensagem livre do aluno RELATORIO-LIVRE-7C2E"
# Nomes de campo de gabarito proibidos em todo o relatório; `correct` também em `distribution`.
FORBIDDEN_NAMES = {"correct_answer", "answer_key", "is_correct", "correct_value", "explanation"}
ENDPOINT_REASONS = ("endpoint_blocked", "channel_refused", "engine_unavailable", "bot_no_access",
                    "channel_unavailable")
SENT_STATES = ("delivered", "answered", "dont_know", "no_answer", "answered_late")
ANSWERED_STATES = ("answered", "dont_know", "answered_late")


# --- montagem por SQL --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Student:
    person_row: int
    alias: str
    enrollment_row: int
    endpoint_row: int
    identity: KnownIdentity | None


@dataclass(frozen=True)
class Dispatch:
    row: int
    dispatch_id: uuid.UUID


@dataclass(frozen=True)
class Variant:
    snapshot: dict
    ir: dict


class World:
    """O oferecimento `active` com bot e o que os testes gravam nele, por SQL, no banco do teste."""

    def __init__(self, sql: psycopg.Connection, *, zone: str = ZONE) -> None:
        self.sql = sql
        self.zone = zone
        self._n = itertools.count(1)
        self.offering_id = uuid.uuid4()
        self.code = f"relatorio-{uuid.uuid4().hex[:8]}"
        self.offering_row = int(sql.execute(
            "INSERT INTO offerings (offering_id, code, course, term, timezone, response_window_minutes, state, "
            "created_at, activated_at) VALUES (%s, %s, %s, %s, %s, 60, 'active', %s, %s) RETURNING id",
            (self.offering_id, self.code, "Disciplina de Teste", "2026s2", zone, T0, T0)).fetchone()[0])
        self.bot_row = int(sql.execute(
            "INSERT INTO bots (bot_uuid, offering_id, telegram_bot_id, username, token_ciphertext, token_version, "
            "state, profile_state, registered_at) VALUES (%s, %s, %s, %s, %s, 1, 'registered', 'configured', %s) "
            "RETURNING id",
            (uuid.uuid4(), self.offering_row, 910_000_000 + self.offering_row, f"relatorio_{self.offering_row}_bot",
             b"\x01" + bytes(40), T0)).fetchone()[0])

    def next(self) -> int:
        return next(self._n)

    # -- pessoas ------------------------------------------------------------------------------------

    def student(self, full_name: str | None = None) -> Student:
        n = self.next()
        alias = uuid.uuid4().hex[:8]
        person = int(self.sql.execute(
            "INSERT INTO people (pseudo_id, alias, created_at) VALUES (%s, %s, %s) RETURNING id",
            (uuid.uuid4(), alias, T0)).fetchone()[0])
        identity = None
        if full_name is not None:
            self.sql.execute("INSERT INTO identities (person_id, full_name) VALUES (%s, %s)", (person, full_name))
            identity = KnownIdentity(alias=alias, full_name=full_name, ra=None, email=None)
        enrollment = int(self.sql.execute(
            "INSERT INTO enrollments (person_id, offering_id, role, state, created_at, activated_at, terms_version, "
            "terms_accepted_at) VALUES (%s, %s, 'student', 'active', %s, %s, 1, %s) RETURNING id",
            (person, self.offering_row, T0, T0, T0)).fetchone()[0])
        endpoint = int(self.sql.execute(
            "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, changed_at) "
            "VALUES (%s, %s, 'telegram', %s, 'active', %s, %s) RETURNING id",
            (person, self.bot_row, 6_000_000 + n, T0, T0)).fetchone()[0])
        return Student(person, alias, enrollment, endpoint, identity)

    # -- questão e disparo --------------------------------------------------------------------------

    def question(self, *, qid: str = "aula03/relatorio", title: str | None = "Questão do relatório",
                 commit: str = COMMIT) -> int:
        repo_hash = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
        return int(self.sql.execute(
            "INSERT INTO question_refs (ref_uuid, repo, repo_sha256, commit_sha, qid, supported, show_correct_answer, "
            "single_variant, partial_credit, title, answers_names, content_hash, inspected_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, true, true, false, true, %s, %s, %s, %s, %s) RETURNING id",
            (uuid.uuid4(), "https://git.exemplo.invalid/curso.git", repo_hash, commit, qid, title, ["resposta"],
             "sha256:" + "c" * 64, T0, T0)).fetchone()[0])

    def dispatch(self, question_row: int, *, state: str = "closed", origin: str = "cli") -> Dispatch:
        dispatch_id = uuid.uuid4()
        cols: dict[str, Any] = {"dispatch_uuid": dispatch_id, "offering_id": self.offering_row,
                                "question_ref_id": question_row, "origin": origin, "immediate": False,
                                "opens_at": OPENS, "closes_at": CLOSES, "state": state, "created_at": T0}
        if state in ("scheduled", "open", "closed"):
            cols.update(validated_at=T0, confirm_deadline=OPENS, preview=Jsonb({}))
        if state in ("open", "closed"):
            cols.update(confirmed_at=T0, opened_at=OPENS)
        if state == "closed":
            cols.update(closed_at=CLOSES)
        if state == "rejected":
            cols.update(rejected_at=T0, rejection="question_broken")
        if state == "cancelled":
            cols.update(cancelled_at=T0, cancel_reason="operator")
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        row = self.sql.execute(f"INSERT INTO dispatches ({names}) VALUES ({marks}) RETURNING id",
                               tuple(cols.values())).fetchone()
        return Dispatch(int(row[0]), dispatch_id)

    # -- entregas e submissões ----------------------------------------------------------------------

    def delivery(self, dispatch: Dispatch, *, state: str, variant: Variant | None = None,
                 student: Student | None = None, reason: str | None = None, failure: str | None = None,
                 parts: dict | None = None, answered_at: datetime = ON_TIME) -> int:
        """Uma entrega no estado pedido; com `variant`, a variante gravada em `delivery_contents`; nos
        estados que passam por `delivered` (e na falha de correção), a mensagem principal enviada."""
        who = student or self.student()
        needs_send = state in SENT_STATES or failure in ("grading_broken",)
        first_state = "pending" if needs_send else state
        row = int(self.sql.execute(
            "INSERT INTO deliveries (delivery_uuid, dispatch_id, enrollment_id, delivery_endpoint_id, seed, "
            "button_token, state, undelivered_reason, failure, created_at, variant_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (uuid.uuid4(), dispatch.row, who.enrollment_row, who.endpoint_row, secrets.randbelow(2**32),
             secrets.token_urlsafe(16), first_state, reason if not needs_send else None,
             failure if not needs_send else None, T0, OPENS if variant is not None else None)).fetchone()[0])
        if variant is not None:
            self.sql.execute(
                "INSERT INTO delivery_contents (delivery_id, snapshot, ir, media_refs, rendered, parts, selection) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (row, Jsonb(variant.snapshot), Jsonb(variant.ir), Jsonb([]), Jsonb({}), Jsonb(parts or {}),
                 Jsonb({})))
        if needs_send:
            assert variant is not None, "entrega enviada sem variante"
            message = int(self.sql.execute(
                "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, delivery_id, "
                "delivery_role, operation, state, attempts, available_at, created_at, sent_at, idempotency_key, "
                "channel_ref) VALUES (%s, %s, %s, %s, 'question', 'send', 'sent', 1, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (who.person_row, self.bot_row, who.endpoint_row, row, OPENS, OPENS, SENT, f"teste-relatorio:{row}:q",
                 str(row))).fetchone()[0])
            self.sql.execute(
                "UPDATE deliveries SET state = %s, failure = %s, sent_at = %s, principal_message_id = %s, "
                "answered_at = %s WHERE id = %s",
                (state, failure, SENT, message, answered_at if state in ANSWERED_STATES else None, row))
        return row

    def submission(self, delivery_row: int, *, outcome: str = "graded", valid: bool | None = True,
                   late: bool = False, score: float | None = None, raw_input: dict | None = None,
                   normalized: Sequence[str] = (), format_errors: dict | None = None,
                   part_derived: list | None = None, response_ms: int | None = None,
                   correct_answers: dict | None = None) -> int:
        received = LATE if late else ON_TIME
        update = int(self.sql.execute(
            "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, details, received_at, state, "
            "outcome, processed_at) VALUES (%s, 0, %s, 'message', %s, %s, 'processed', 'answer_recorded', %s) "
            "RETURNING id", (self.bot_row, 100_000 + self.next(), Jsonb({}), received, received)).fetchone()[0])
        graded = outcome in ("graded", "broken")
        return int(self.sql.execute(
            "INSERT INTO submissions (submission_uuid, delivery_id, telegram_update_id, outcome, valid, late, "
            "normalized_fields, raw_input, submitted_answers, format_errors, correct_answers, score, part_derived, "
            "response_ms, received_at, created_at, graded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (uuid.uuid4(), delivery_row, update, outcome, valid, late, list(normalized), Jsonb(raw_input or {}),
             Jsonb(raw_input or {}) if graded else None,
             Jsonb(format_errors) if format_errors is not None else (Jsonb({}) if outcome == "graded" else None),
             Jsonb(correct_answers) if correct_answers is not None else None, score,
             Jsonb(part_derived) if part_derived is not None else None, response_ms, received, received,
             received if outcome in ("graded", "broken") else None)).fetchone()[0])

    def answered(self, dispatch: Dispatch, variant: Variant, *, raw: dict, part_derived: list,
                 late: bool = False, score: float = 1.0, normalized: Sequence[str] = (),
                 response_ms: int | None = 60_000, student: Student | None = None) -> int:
        """Uma entrega respondida (no prazo: `answered`; atrasada: `answered_late`) com a submissão final
        `graded` válida; devolve o id da entrega."""
        row = self.delivery(dispatch, state="answered_late" if late else "answered", variant=variant,
                            student=student, answered_at=LATE if late else ON_TIME)
        self.submission(row, late=late, score=score, raw_input=raw, normalized=normalized,
                        part_derived=part_derived, response_ms=response_ms,
                        correct_answers=variant.snapshot["correct_answers"])
        return row

    def free_message(self, student: Student, text: str) -> None:
        update = int(self.sql.execute(
            "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, text, details, received_at, state, "
            "outcome, processed_at) VALUES (%s, 0, %s, 'message', %s, %s, %s, 'processed', 'free_message', %s) "
            "RETURNING id", (self.bot_row, 100_000 + self.next(), text, Jsonb({}), ON_TIME, ON_TIME)).fetchone()[0])
        self.sql.execute(
            "INSERT INTO free_messages (offering_id, delivery_endpoint_id, telegram_update_id, text, replied, "
            "ambiguous, received_at, created_at) VALUES (%s, %s, %s, %s, false, false, %s, %s)",
            (self.offering_row, student.endpoint_row, update, text, ON_TIME, ON_TIME))


def _variant(question: FakeQuestion, seed: str = "1a") -> Variant:
    snapshot, ir, _ = build_variant(question, seed)
    return Variant(snapshot, ir)


def _reordered(variant: Variant, order: Sequence[str], *, field: int = 0,
               html_overrides: dict[str, str] | None = None) -> Variant:
    """A mesma variante com as alternativas do campo embaralhadas (`order` = os HTML na nova ordem) e as
    chaves redistribuídas pela posição, como o PL faz; o `content_hash` acompanha o conteúdo. Com
    `html_overrides`, o HTML exibido de uma alternativa muda e o hash fica (para o teste da entrega de menor
    `id`)."""
    ir = copy.deepcopy(variant.ir)
    by_html = {o["html"]: o for o in ir["fields"][field]["options"]}
    ir["fields"][field]["options"] = [
        {"key": "abcdefghijklmnop"[i], "html": (html_overrides or {}).get(h, h),
         "content_hash": by_html[h]["content_hash"]}
        for i, h in enumerate(order)]
    return Variant(copy.deepcopy(variant.snapshot), ir)


def _hash(variant: Variant, html: str, *, field: int = 0) -> str:
    return next(o["content_hash"] for o in variant.ir["fields"][field]["options"] if o["html"] == html)


def _key(variant: Variant, html: str, *, field: int = 0) -> str:
    return next(o["key"] for o in variant.ir["fields"][field]["options"] if o["html"] == html)


def _part(index: int, *, choices: Iterable[str] = (), ratio: float | None = None,
          residual: float | None = None) -> dict:
    """Um item de `part_derived` como o `submission.grade` grava (`grading.py:_parts`)."""
    return {"index": index, "choices": list(choices), "ratio": ratio, "residual": residual}


def _format_error(name: str) -> dict:
    return {name: {"html": "Formato inválido.", "text": "Invalid format."}}


def _compute(world: World, dispatch: Dispatch, *, identities: Sequence[KnownIdentity] = (),
             formatter: Any = None, now: datetime = NOW) -> Any:
    return compute_report(world.sql, dispatch.row, now=now, identities=list(identities),
                          formatter=formatter if formatter is not None else PlainFormatter())


def _report(world: World, dispatch: Dispatch, **kwargs: Any) -> Report:
    result = _compute(world, dispatch, **kwargs)
    assert isinstance(result, Report), f"compute_report devolveu {result!r}"
    return result


def _approx(expected: float) -> Any:
    return pytest.approx(expected, rel=1e-12, abs=1e-12)


def _assert_rate(actual: RatePair, on_time: float | None, with_late: float | None, name: str) -> None:
    for side, got, want in (("on_time", actual.on_time, on_time), ("with_late", actual.with_late, with_late)):
        if want is None:
            assert got is None, f"{name}.{side}: {got!r} != None"
        else:
            assert got is not None and got == _approx(want), f"{name}.{side}: {got!r} != {want!r}"


def _assert_rates(report: Report, **expected: tuple[float | None, float | None]) -> None:
    for name, (on_time, with_late) in expected.items():
        _assert_rate(getattr(report.performance, name), on_time, with_late, name)


def _assert_pairs(report: Report, **expected: tuple[int, int]) -> None:
    for name, (on_time, with_late) in expected.items():
        assert getattr(report.performance, name) == Pair(on_time, with_late), name


def _field_names(obj: Any) -> set[str]:
    """Os nomes dos campos de todos os objetos do relatório (recursivo)."""
    names: set[str] = set()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            names.add(f.name)
            names |= _field_names(getattr(obj, f.name))
    elif isinstance(obj, (tuple, list)):
        for item in obj:
            names |= _field_names(item)
    return names


def _assert_no_answer_key(report: Report) -> None:
    """Nenhum campo de gabarito no relatório e nenhum `correct` na distribuição."""
    assert _field_names(report) & FORBIDDEN_NAMES == set()
    assert "correct" not in _field_names(report.distribution)
    assert ANSWER_SENTINEL not in repr(report)
    for item in report.distribution.fields:
        for choice in item.choices or ():
            assert [f.name for f in dataclasses.fields(choice)] == ["content_hash", "text", "count"]


ZERO_REASONS = NotDeliveredByReason(0, 0, 0, 0, 0)
ZERO_STAGES = QuestionFailedByStage(0, 0)


def _number_variant(**kw: Any) -> Variant:
    return _variant(preset("number", **kw))


# --- alcance e pares ---------------------------------------------------------------------------------


def test_reach_and_answered_pairs_on_closed_dispatch(sql: psycopg.Connection) -> None:
    """30 alvos, 28 entregues, 2 não entregues por `endpoint_blocked`, 18 respostas no prazo e 5
    atrasadas → alcance 30, 28, 2 (com o motivo) e 0 pendentes; respondidas 18 no prazo · 23 com atrasadas."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    for i in range(18):
        world.answered(dispatch, variant, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0, residual=0.0)],
                       response_ms=1000 * (i + 1))
    for _ in range(5):
        world.answered(dispatch, variant, late=True, raw={"resposta": "6.5"},
                       part_derived=[_part(0, ratio=1.0, residual=0.0)], response_ms=4_000_000)
    for _ in range(5):
        world.delivery(dispatch, state="no_answer", variant=variant)
    for _ in range(2):
        world.delivery(dispatch, state="not_delivered", reason="endpoint_blocked")
    report = _report(world, dispatch)
    assert report.reach == Reach(target=30, delivered=28, not_delivered=2,
                                 not_delivered_by_reason=NotDeliveredByReason(2, 0, 0, 0, 0), question_failed=0,
                                 question_failed_by_stage=ZERO_STAGES, pending=0)
    _assert_pairs(report, answered=(18, 23), correct=(18, 23), partially_correct=(0, 0), incorrect=(0, 0),
                  dont_know=(0, 0))
    _assert_rates(report, correct_rate=(18 / 28, 23 / 28), partially_correct_rate=(0.0, 0.0),
                  incorrect_rate=(0.0, 0.0), response_rate=(18 / 28, 23 / 28), mean_score=(1.0, 1.0))
    # Tempo até responder: só as 18 no prazo (1 000 a 18 000 ms), nenhum negativo.
    data = [1000 * (i + 1) for i in range(18)]
    q1, median, q3 = statistics.quantiles(data, n=4, method="inclusive")
    rt = report.response_time
    assert (rt.observations, rt.negative) == (18, 0)
    assert rt.median_ms == _approx(statistics.median(data))
    assert rt.median_ms == _approx(median)
    assert (rt.q1_ms, rt.q3_ms) == (_approx(q1), _approx(q3))
    assert report.distribution == Distribution(fields=(FieldDistribution(
        index=0, type="number", label="Velocidade:", unavailable=None, choices=None,
        values=TopList((TopItem("6.5", Pair(18, 23)),), Pair(0, 0)),
        ratios=TopList((TopItem("1", Pair(18, 23)),), Pair(0, 0)), unrated=Pair(0, 0), texts=None),))
    assert report.friction == Friction(fields=(FieldFriction(0, Pair(0, 0), Pair(0, 0)),),
                                       unmapped_format_errors=Pair(0, 0))


def test_reach_by_reason_and_stage_and_internal_failure(sql: psycopg.Connection) -> None:
    """os 5 motivos de não entrega (objeto fechado) e as falhas de questão por estágio (preparação:
    `variant_broken`, `variant_unsupported`, `channel_limits_exceeded`, `engine_rejected`; correção:
    `grading_broken`), fora das entregues; as pendentes; a submissão com falha interna conta a entrega no
    alcance e fica fora do desempenho e da distribuição."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    for reason in ("endpoint_blocked", "endpoint_blocked", "channel_refused", "engine_unavailable",
                   "bot_no_access", "channel_unavailable"):
        world.delivery(dispatch, state="not_delivered", reason=reason)
    for failure in ("variant_broken", "variant_unsupported", "channel_limits_exceeded", "engine_rejected"):
        world.delivery(dispatch, state="question_failed", failure=failure)
    for _ in range(2):
        broken = world.delivery(dispatch, state="question_failed", failure="grading_broken", variant=variant)
        world.submission(broken, outcome="broken", valid=None, raw_input={"resposta": "8.25"})
    world.delivery(dispatch, state="pending", variant=variant)
    world.delivery(dispatch, state="pending")
    internal = world.delivery(dispatch, state="delivered", variant=variant)
    world.submission(internal, outcome="internal_failure", valid=None, raw_input={"resposta": "9.9"})
    world.answered(dispatch, variant, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0, residual=0.0)])
    report = _report(world, dispatch)
    assert report.reach == Reach(target=16, delivered=2, not_delivered=6,
                                 not_delivered_by_reason=NotDeliveredByReason(endpoint_blocked=2, channel_refused=1,
                                                                              engine_unavailable=1, bot_no_access=1,
                                                                              channel_unavailable=1),
                                 question_failed=6,
                                 question_failed_by_stage=QuestionFailedByStage(preparation=4, grading=2), pending=2)
    _assert_pairs(report, answered=(1, 1), correct=(1, 1), partially_correct=(0, 0), incorrect=(0, 0),
                  dont_know=(0, 0))
    _assert_rates(report, response_rate=(0.5, 0.5), correct_rate=(0.5, 0.5), mean_score=(1.0, 1.0))
    # Só a resposta válida entra na distribuição (com 1 ocorrência, abaixo do mínimo: "outras"); os valores
    # da submissão com falha interna ("9.9") e das quebradas na correção ("8.25") não.
    field = report.distribution.fields[0]
    assert field.values == TopList((), Pair(1, 1))
    assert field.ratios == TopList((), Pair(1, 1))
    assert field.unrated == Pair(0, 0)
    assert report.grading_pending == 0


# --- desempenho --------------------------------------------------------------------------------------


def test_score_bands_and_clamped_mean_score(sql: psycopg.Connection) -> None:
    """notas 1, 0,5 e 0 nas três faixas; 1,2 conta como correta e −0,1 como incorreta, e a nota média
    usa `least(greatest(score, 0), 1)`; as taxas sobre as entregues."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    for score in (1.0, 0.5, 0.0, 1.2, -0.1):
        world.answered(dispatch, variant, score=score, raw={"resposta": "6.5"},
                       part_derived=[_part(0, ratio=1.0, residual=0.0)])
    for score in (1.0, 1.0, 0.5):
        world.answered(dispatch, variant, late=True, score=score, raw={"resposta": "6.5"},
                       part_derived=[_part(0, ratio=1.0, residual=0.0)])
    for _ in range(2):
        world.delivery(dispatch, state="no_answer", variant=variant)
    report = _report(world, dispatch)
    assert report.reach.delivered == 10
    # Faixas: no prazo {1, 1,2} corretas, {0,5} parcial, {0, −0,1} incorretas; atrasadas {1, 1} e {0,5}.
    _assert_pairs(report, answered=(5, 8), correct=(2, 4), partially_correct=(1, 2), incorrect=(2, 2),
                  dont_know=(0, 0))
    # Média limitada: no prazo (1 + 0,5 + 0 + 1 + 0) / 5 = 0,5; com atrasadas (2,5 + 1 + 1 + 0,5) / 8 = 0,625.
    _assert_rates(report, correct_rate=(2 / 10, 4 / 10), partially_correct_rate=(1 / 10, 2 / 10),
                  incorrect_rate=(2 / 10, 2 / 10), response_rate=(5 / 10, 8 / 10), mean_score=(0.5, 0.625))


def test_dont_know_on_time_and_late_distinguished_by_late(sql: psycopg.Connection) -> None:
    """O "Não sei" no prazo e o atrasado distinguidos pelo `late` da submissão; fora das
    respondidas, das faixas, da nota média e do numerador das taxas."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    for late in (False, False, True):
        row = world.delivery(dispatch, state="dont_know", variant=variant, answered_at=LATE if late else ON_TIME)
        world.submission(row, outcome="dont_know", valid=None, late=late, raw_input={})
    world.answered(dispatch, variant, score=0.5, raw={"resposta": "6.5"},
                   part_derived=[_part(0, ratio=1.0, residual=0.0)])
    world.delivery(dispatch, state="no_answer", variant=variant)
    report = _report(world, dispatch)
    assert report.reach.delivered == 5
    _assert_pairs(report, dont_know=(2, 3), answered=(1, 1), correct=(0, 0), partially_correct=(1, 1),
                  incorrect=(0, 0))
    _assert_rates(report, response_rate=(1 / 5, 1 / 5), correct_rate=(0.0, 0.0), partially_correct_rate=(1 / 5, 1 / 5),
                  incorrect_rate=(0.0, 0.0), mean_score=(0.5, 0.5))


# --- tempo até responder -----------------------------------------------------------------------------

RESPONSE_TIMES = {
    0: [],
    1: [4200],
    2: [-1500, 3000],
    3: [1000, 2000, 6000],
    4: [500, 1200, 2600, 9000],
    5: [-200, 800, 800, 4000, 12000],
}


@pytest.mark.parametrize("n", sorted(RESPONSE_TIMES))
def test_response_time_quartiles_only_valid_on_time(sql: psycopg.Connection, n: int) -> None:
    """a mediana e os quartis só das `graded` válidas no prazo (`percentile_cont`, inclusivo); o tempo
    negativo contado em `negative` e incluído; a atrasada, a inválida e o "Não sei" fora. n = 0 → `None` nos
    três; n = 1 → o próprio valor; n ≥ 2 → contra `statistics`."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    data = RESPONSE_TIMES[n]
    for value in data:
        world.answered(dispatch, variant, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0, residual=0.0)],
                       response_ms=value)
    world.answered(dispatch, variant, late=True, raw={"resposta": "6.5"},
                   part_derived=[_part(0, ratio=1.0, residual=0.0)], response_ms=777)
    invalid = world.delivery(dispatch, state="no_answer", variant=variant)
    world.submission(invalid, valid=False, score=0.0, raw_input={"resposta": "x"},
                     format_errors=_format_error("resposta"), response_ms=555)
    dont_know = world.delivery(dispatch, state="dont_know", variant=variant)
    world.submission(dont_know, outcome="dont_know", valid=None, raw_input={}, response_ms=333)
    rt = _report(world, dispatch).response_time
    assert rt.observations == n
    assert rt.negative == {0: 0, 1: 0, 2: 1, 3: 0, 4: 0, 5: 1}[n]
    if n == 0:
        assert (rt.median_ms, rt.q1_ms, rt.q3_ms) == (None, None, None)
    elif n == 1:
        assert (rt.median_ms, rt.q1_ms, rt.q3_ms) == (4200, 4200, 4200)
    else:
        q1, median, q3 = statistics.quantiles(data, n=4, method="inclusive")
        assert rt.median_ms == _approx(statistics.median(data))
        assert rt.median_ms == _approx(median)
        assert rt.q1_ms == _approx(q1)
        assert rt.q3_ms == _approx(q3)


# --- distribuição: escolhas --------------------------------------------------------------------------


def test_single_choice_grouped_by_content_hash(sql: psycopg.Connection) -> None:
    """alternativas embaralhadas agrupadas pelo `content_hash`, com o texto da IR da entrega de menor
    `id` pelo `formatter.plain`; ordem pela contagem com atrasadas e, no empate, pelo hash; nenhum item
    marcado como correto."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    question = preset("single_choice", fields=(single_choice_field(label="<b>Cor</b> do céu &amp; do mar"),))
    first = _variant(question)  # Vermelho, Azul, Verde, Amarelo
    shuffled = _reordered(first, ["Verde", "Amarelo", "Azul", "Vermelho"])
    # A mesma alternativa (mesmo hash) exibida com outro HTML numa entrega posterior: o texto vem da de menor id.
    later = _reordered(first, ["Verde", "Amarelo", "Azul", "Vermelho"], html_overrides={"Azul": "Azul-celeste"})
    azul, verde, vermelho, amarelo = (_hash(first, h) for h in ("Azul", "Verde", "Vermelho", "Amarelo"))

    def choose(variant: Variant, html: str, *, late: bool = False, display: str | None = None) -> None:
        key = _key(variant, display or html)
        world.answered(dispatch, variant, late=late, score=1.0 if html == "Azul" else 0.0,
                       raw={"resposta": key}, part_derived=[_part(0, choices=[_hash(first, html)])])

    choose(first, "Azul")                                    # menor id: o texto de Azul vem daqui
    choose(shuffled, "Azul")
    choose(later, "Azul", late=True, display="Azul-celeste")
    choose(shuffled, "Verde")
    choose(first, "Vermelho", late=True)
    choose(first, "Amarelo")
    report = _report(world, dispatch)
    tied = sorted([(verde, "Verde", Pair(1, 1)), (vermelho, "Vermelho", Pair(0, 1)), (amarelo, "Amarelo", Pair(1, 1))])
    assert report.distribution.fields == (FieldDistribution(
        index=0, type="single_choice", label="Cor do céu & do mar", unavailable=None,
        choices=(ChoiceItem(azul, "Azul", Pair(2, 3)), *(ChoiceItem(h, t, c) for h, t, c in tied)),
        values=None, ratios=None, unrated=None, texts=None),)
    _assert_no_answer_key(report)


def test_choice_text_redacted_and_truncated(sql: psycopg.Connection) -> None:
    """O texto da alternativa é redigido (a marca da redação automática no lugar do nome de um aluno do oferecimento) e
    truncado em 200 caracteres com "…"."""
    world = World(sql)
    named = world.student(NAME)
    dispatch = world.dispatch(world.question())
    long_text = "Alternativa longa " + "x" * 250
    options = (FakeOption("a", f"Resposta de {NAME}"), FakeOption("b", long_text), FakeOption("c", "Curta"))
    variant = _variant(preset("single_choice", fields=(single_choice_field(options=options, correct="c"),)))
    for html in (f"Resposta de {NAME}", long_text):
        world.answered(dispatch, variant, score=0.0, raw={"resposta": _key(variant, html)},
                       part_derived=[_part(0, choices=[_hash(variant, html)])])
    report = _report(world, dispatch, identities=[named.identity])
    texts = {c.content_hash: c.text for c in report.distribution.fields[0].choices}
    assert texts == {_hash(variant, f"Resposta de {NAME}"): f"Resposta de {person_marker(named.alias)}",
                     _hash(variant, long_text): long_text[:200] + "…"}
    assert NAME not in repr(report)


def test_multiple_choice_counted_per_option(sql: psycopg.Connection) -> None:
    """A múltipla escolha é contada por alternativa marcada (cada `content_hash` da submissão)."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _variant(preset("multiple_choice"))  # Dois, Nove, Sete, Dez
    dois, sete, dez = (_hash(variant, h) for h in ("Dois", "Sete", "Dez"))
    world.answered(dispatch, variant, raw={"resposta": ["a", "c"]}, part_derived=[_part(0, choices=[dois, sete])])
    world.answered(dispatch, variant, score=0.5, raw={"resposta": ["a"]}, part_derived=[_part(0, choices=[dois])])
    world.answered(dispatch, variant, late=True, score=0.0, raw={"resposta": ["c", "d"]},
                   part_derived=[_part(0, choices=[sete, dez])])
    report = _report(world, dispatch)
    tied = sorted([(dois, "Dois", Pair(2, 2)), (sete, "Sete", Pair(1, 2))])
    assert report.distribution.fields[0].choices == (*(ChoiceItem(h, t, c) for h, t, c in tied),
                                                     ChoiceItem(dez, "Dez", Pair(0, 1)))
    _assert_no_answer_key(report)


def test_choice_with_more_options_than_buttons_groups_with_buttons(sql: psycopg.Connection) -> None:
    """Situações-limite (**[F]** a degradação é decidida por entrega na abertura,
    `deliveries.choice_has_buttons`): a mesma alternativa escolhida numa entrega com botões (toque e
    "Enviar") e noutra com o campo degradado (mais alternativas que botões; a chave digitada) → um único
    item com contagem 2; o mesmo na múltipla (marcada por toque numa e digitada na outra)."""
    world = World(sql)
    many = tuple(FakeOption(k, f"Opção {k.upper()}") for k in "abcdefghijkl") + (FakeOption("m", "Azul"),)
    # Escolha única: com botões (4 alternativas) e degradada (13 alternativas, "Azul" na chave `m`).
    single = world.dispatch(world.question(qid="aula03/unica"))
    buttons = _variant(preset("single_choice"))
    degraded_q = preset("single_choice", fields=(single_choice_field(options=many, correct="m"),))
    degraded = _variant(degraded_q)
    azul = _hash(buttons, "Azul")
    assert _hash(degraded, "Azul") == azul
    world.answered(single, buttons, raw={"resposta": "b"}, part_derived=[_part(0, choices=[azul])])
    world.answered(single, degraded, raw={"resposta": "m"}, part_derived=[_part(0, choices=[azul])])
    single_report = _report(world, single)
    assert single_report.distribution.fields[0].choices == (ChoiceItem(azul, "Azul", Pair(2, 2)),)
    # Múltipla: "Sete" marcada por toque numa entrega e digitada (com "a") noutra, degradada.
    multi = world.dispatch(world.question(qid="aula03/multipla"))
    primes = _variant(preset("multiple_choice"))
    sete = _hash(primes, "Sete")
    many_primes = tuple(FakeOption(k, f"Número {k.upper()}") for k in "abcdefghijkl") + (FakeOption("m", "Sete"),)
    degraded_multi = _variant(preset("multiple_choice", fields=(multiple_choice_field(options=many_primes,
                                                                                      correct=("m",)),)))
    other = _hash(degraded_multi, "Número A")
    assert _hash(degraded_multi, "Sete") == sete
    world.answered(multi, primes, raw={"resposta": ["c"]}, part_derived=[_part(0, choices=[sete])])
    world.answered(multi, degraded_multi, score=0.5, raw={"resposta": "am"},
                   part_derived=[_part(0, choices=[other, sete])])
    choices = {c.content_hash: c for c in _report(world, multi).distribution.fields[0].choices}
    assert choices[sete] == ChoiceItem(sete, "Sete", Pair(2, 2))
    assert choices[other] == ChoiceItem(other, "Número A", Pair(1, 1))
    assert set(choices) == {sete, other}


def test_label_and_options_converted_by_injected_formatter(sql: psycopg.Connection) -> None:
    """o rótulo e as alternativas saem de `label_html`/`html` pelo `formatter.plain` injetado (um
    dublê que marca o texto prova que é o injetado), e o relatório é o mesmo com os três formatadores (o
    `plain` é o mesmo nos três)."""

    class MarkingFormatter:
        """Só `plain` é permitido ao cálculo; devolve o `plain` do formatador entre « »."""

        text_format = "plain"
        max_length = 4096

        def __init__(self) -> None:
            self.seen: list[str] = []

        def plain(self, canonical_html: str) -> str:
            self.seen.append(canonical_html)
            return "«" + PlainFormatter().plain(canonical_html) + "»"

        def from_ir(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("o cálculo só usa formatter.plain")

        escape = compose = from_ir

    world = World(sql)
    dispatch = world.dispatch(world.question())
    options = (FakeOption("a", "<i>Azul</i> &lt;claro&gt;"), FakeOption("b", "x<sup>2</sup>"))
    question = preset("single_choice", fields=(single_choice_field(label="<b>Cor</b> do céu &amp; do mar",
                                                                   options=options, correct="a"),))
    variant = _variant(question)
    first, second = _hash(variant, options[0].html), _hash(variant, options[1].html)
    world.answered(dispatch, variant, raw={"resposta": "a"}, part_derived=[_part(0, choices=[first])])
    world.answered(dispatch, variant, late=True, score=0.0, raw={"resposta": "b"},
                   part_derived=[_part(0, choices=[second])])
    marking = MarkingFormatter()
    field = _report(world, dispatch, formatter=marking).distribution.fields[0]
    assert field.label == "«Cor do céu & do mar»"
    tied = sorted([(first, "«Azul <claro>»", Pair(1, 1)), (second, "«x^(2)»", Pair(0, 1))],
                  key=lambda t: (-t[2].with_late, t[0]))
    assert field.choices == tuple(ChoiceItem(h, t, c) for h, t, c in tied)
    assert set(marking.seen) >= {"<b>Cor</b> do céu &amp; do mar", options[0].html, options[1].html}
    reports_by_formatter = [_report(world, dispatch, formatter=f()) for f in (PlainFormatter, TelegramHtmlFormatter,
                                                                             TelegramRichFormatter)]
    assert reports_by_formatter[0] == reports_by_formatter[1] == reports_by_formatter[2]
    assert reports_by_formatter[0].distribution.fields[0].label == "Cor do céu & do mar"


# --- distribuição: números e textos ------------------------------------------------------------------


def test_number_values_ratios_unrated_and_friction(sql: psycopg.Connection) -> None:
    """Número: `values` com o literal de `raw_for_engine` (a vírgula normalizada),
    `ratios` com 3 algarismos e `unrated` com as razões nulas; até 5 itens com pelo menos 2 no lado "com
    atrasadas", ordem pela contagem e pela cadeia; a fricção por índice (`format_errors` das inválidas,
    `decimal_comma` das válidas e inválidas) e a chave fora da IR em `unmapped_format_errors`."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant(fields=(number_field(label="Velocidade <i>v</i> (m/s)"),))
    r7, r3 = 7 / 6.5, -3 / 6.5
    on_time = [("6,5", ["resposta"], 1.0), ("6.5", [], 1.0), ("13/2", [], 1.0), ("7", [], r7), ("-3", [], r3),
               ("1e400", [], None)]
    late = [("7", [], r7), ("-3", [], r3), ("1e400", [], None)]
    rows = []
    for raw, normalized, ratio in on_time:
        rows.append(world.answered(dispatch, variant, raw={"resposta": raw}, normalized=normalized,
                                   part_derived=[_part(0, ratio=ratio)]))
    for raw, normalized, ratio in late:
        world.answered(dispatch, variant, late=True, raw={"resposta": raw}, normalized=normalized,
                       part_derived=[_part(0, ratio=ratio)])
    # Inválidas: no prazo com vírgula e erro no campo; atrasadas com erro no campo e com erro fora da IR.
    world.submission(rows[0], valid=False, score=0.0, raw_input={"resposta": "6,5x"}, normalized=["resposta"],
                     format_errors=_format_error("resposta"))
    spare = world.delivery(dispatch, state="no_answer", variant=variant)
    world.submission(spare, valid=False, late=True, score=0.0, raw_input={"resposta": "y"},
                     format_errors=_format_error("resposta"))
    world.submission(spare, valid=False, late=True, score=0.0, raw_input={"resposta": "6.5"},
                     format_errors=_format_error("verificacao"))
    report = _report(world, dispatch)
    assert report.distribution.fields == (FieldDistribution(
        index=0, type="number", label="Velocidade v (m/s)", unavailable=None, choices=None,
        # "6.5" (2, 2), "13/2" (1, 1: "outras"), "7" (1, 2), "-3" (1, 2), "1e400" (1, 2)
        values=TopList((TopItem("-3", Pair(1, 2)), TopItem("1e400", Pair(1, 2)), TopItem("6.5", Pair(2, 2)),
                        TopItem("7", Pair(1, 2))), Pair(1, 1)),
        # razão 1 (3, 3), 7/6,5 → "1.08" (1, 2), −3/6,5 → "-0.462" (1, 2); nulas (1, 2) em `unrated`
        ratios=TopList((TopItem("1", Pair(3, 3)), TopItem("-0.462", Pair(1, 2)), TopItem("1.08", Pair(1, 2))),
                       Pair(0, 0)),
        unrated=Pair(1, 2), texts=None),)
    assert report.friction == Friction(fields=(FieldFriction(0, format_errors=Pair(1, 2), decimal_comma=Pair(2, 2)),),
                                       unmapped_format_errors=Pair(0, 1))
    assert "verificacao" not in repr(report)
    _assert_pairs(report, answered=(6, 9))


def test_text_answers_grouped_normalized_and_redacted(sql: psycopg.Connection) -> None:
    """As 5 mais comuns com pelo menos 2 ocorrências, agrupadas por
    `normalize_text_answer` (`"abc\\n"`, `"\\tabc"` e o espaço não separável no grupo de `"abc"`; a caixa
    preservada), redigidas (a marca da redação automática no lugar do nome de um aluno do oferecimento) e as demais em
    "outras"; o nome está na submissão (controle positivo) e não no relatório."""
    world = World(sql)
    named = world.student(NAME)
    dispatch = world.dispatch(world.question())
    variant = _variant(preset("text"))
    on_time = ["abc\n", "\tabc", " abc", "abc", "Abc", "a  b", f"resposta de {NAME}", f"resposta de {NAME} ",
               "=1+1", "=1+1", "única"]
    late = ["Abc", "a b", "xyz", "xyz"]
    for text in on_time:
        world.answered(dispatch, variant, score=0.0, raw={"resposta": text}, part_derived=[_part(0)])
    for text in late:
        world.answered(dispatch, variant, late=True, score=0.0, raw={"resposta": text}, part_derived=[_part(0)])
    stored = [row[0] for row in sql.execute("SELECT raw_input::text FROM submissions ORDER BY id")]
    assert sum(NAME in text for text in stored) == 2
    report = _report(world, dispatch, identities=[named.identity])
    field = report.distribution.fields[0]
    assert (field.type, field.label, field.choices, field.values, field.ratios, field.unrated) == (
        "text", "Cor:", None, None, None, None)
    assert field.texts == TopList(
        items=(TopItem("abc", Pair(4, 4)), TopItem("=1+1", Pair(2, 2)), TopItem("Abc", Pair(1, 2)),
               TopItem("a b", Pair(1, 2)), TopItem(f"resposta de {person_marker(named.alias)}", Pair(2, 2))),
        others=Pair(1, 3))  # "xyz" (0, 2) e "única" (1, 1)
    assert NAME not in repr(report)


# --- várias partes -----------------------------------------------------------------------------------


def test_multipart_final_score_parts_and_partial_dont_know(sql: psycopg.Connection) -> None:
    """O desempenho pela nota final e a distribuição por parte; o "Não sei"
    depois de uma parte preenchida conta só no "não sei" (fora das respondidas, das faixas, da média, da
    taxa e da distribuição), com o erro de formato anterior na fricção; a entrega de duas partes com só a
    primeira preenchida, sem "Não sei" nem submissão, fica `no_answer`: conta nas entregues e o valor
    preenchido não sai na distribuição."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    question = FakeQuestion("Várias partes", "Responda às três partes.",
                            (number_field("num", label="Parte 1"), integer_field("int", correct=31, label="Parte 2"),
                             single_choice_field("mc", label="Parte 3")))
    variant = _variant(question)
    vermelho, azul = _hash(variant, "Vermelho", field=2), _hash(variant, "Azul", field=2)
    world.answered(dispatch, variant, score=1.0, raw={"num": "6.5", "int": "31", "mc": "b"},
                   part_derived=[_part(0, ratio=1.0, residual=0.0), _part(1, ratio=1.0, residual=0.0),
                                 _part(2, choices=[azul])])
    world.answered(dispatch, variant, score=0.5, raw={"num": "6.5", "int": "30", "mc": "a"},
                   part_derived=[_part(0, ratio=1.0, residual=0.0), _part(1, ratio=30 / 31, residual=-1.0),
                                 _part(2, choices=[vermelho])])
    world.answered(dispatch, variant, late=True, score=1.0, raw={"num": "6,5", "int": "31", "mc": "b"},
                   normalized=["num"],
                   part_derived=[_part(0, ratio=1.0, residual=0.0), _part(1, ratio=1.0, residual=0.0),
                                 _part(2, choices=[azul])])
    dont_know = world.delivery(dispatch, state="dont_know", variant=variant, parts={"num": "6.5"})
    world.submission(dont_know, valid=False, score=0.0, raw_input={"num": "6.5", "int": "x", "mc": "b"},
                     format_errors=_format_error("int"))
    world.submission(dont_know, outcome="dont_know", valid=None, raw_input={"num": "6.5"})
    world.delivery(dispatch, state="no_answer", variant=variant, parts={"num": "99"})
    report = _report(world, dispatch)
    assert report.reach.delivered == 5
    _assert_pairs(report, answered=(2, 3), correct=(1, 2), partially_correct=(1, 1), incorrect=(0, 0),
                  dont_know=(1, 1))
    # Média: no prazo (1 + 0,5) / 2 = 0,75; com atrasadas (1 + 0,5 + 1) / 3 = 2,5 / 3.
    _assert_rates(report, response_rate=(2 / 5, 3 / 5), correct_rate=(1 / 5, 2 / 5),
                  partially_correct_rate=(1 / 5, 1 / 5), incorrect_rate=(0.0, 0.0), mean_score=(0.75, 2.5 / 3))
    assert report.distribution.fields == (
        FieldDistribution(index=0, type="number", label="Parte 1", unavailable=None, choices=None,
                          values=TopList((TopItem("6.5", Pair(2, 3)),), Pair(0, 0)),
                          ratios=TopList((TopItem("1", Pair(2, 3)),), Pair(0, 0)), unrated=Pair(0, 0), texts=None),
        FieldDistribution(index=1, type="integer", label="Parte 2", unavailable=None, choices=None,
                          values=TopList((TopItem("31", Pair(1, 2)),), Pair(1, 1)),
                          ratios=TopList((TopItem("1", Pair(1, 2)),), Pair(1, 1)), unrated=Pair(0, 0), texts=None),
        FieldDistribution(index=2, type="single_choice", label="Parte 3", unavailable=None,
                          choices=(ChoiceItem(azul, "Azul", Pair(1, 2)), ChoiceItem(vermelho, "Vermelho", Pair(1, 1))),
                          values=None, ratios=None, unrated=None, texts=None),
    )
    assert report.friction == Friction(fields=(
        FieldFriction(0, format_errors=Pair(0, 0), decimal_comma=Pair(0, 1)),
        FieldFriction(1, format_errors=Pair(1, 1), decimal_comma=Pair(0, 0)),
        FieldFriction(2, format_errors=Pair(0, 0), decimal_comma=Pair(0, 0)),
    ), unmapped_format_errors=Pair(0, 0))


# --- denominadores zero e alvo vazio -----------------------------------------------------------------


def test_zero_delivered_gives_null_rates(sql: psycopg.Connection) -> None:
    """nenhuma entregue → `None` em toda taxa e na média, nos dois lados."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    for reason in ("endpoint_blocked", "channel_refused", "channel_refused"):
        world.delivery(dispatch, state="not_delivered", reason=reason)
    report = _report(world, dispatch)
    assert report.reach == Reach(3, 0, 3, NotDeliveredByReason(1, 2, 0, 0, 0), 0, ZERO_STAGES, 0)
    _assert_pairs(report, answered=(0, 0), correct=(0, 0), partially_correct=(0, 0), incorrect=(0, 0),
                  dont_know=(0, 0))
    for name in ("correct_rate", "partially_correct_rate", "incorrect_rate", "response_rate", "mean_score"):
        assert getattr(report.performance, name) == RatePair(None, None), name
    rt = report.response_time
    assert (rt.observations, rt.negative, rt.median_ms, rt.q1_ms, rt.q3_ms) == (0, 0, None, None, None)


def test_zero_denominator_on_one_side_of_the_pair(sql: psycopg.Connection) -> None:
    """entregues sem resposta no prazo e uma atrasada → as taxas sobre as entregues valem 0 no prazo,
    e a nota média (sobre as respondidas) é `None` só no lado "no prazo"."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    world.answered(dispatch, variant, late=True, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0)])
    for _ in range(2):
        world.delivery(dispatch, state="no_answer", variant=variant)
    report = _report(world, dispatch)
    assert report.reach.delivered == 3
    _assert_pairs(report, answered=(0, 1), correct=(0, 1), partially_correct=(0, 0), incorrect=(0, 0))
    _assert_rates(report, correct_rate=(0.0, 1 / 3), partially_correct_rate=(0.0, 0.0), incorrect_rate=(0.0, 0.0),
                  response_rate=(0.0, 1 / 3), mean_score=(None, 1.0))


def test_empty_target_is_all_zero_with_null_rates(sql: psycopg.Connection) -> None:
    """Alvo vazio: tudo zero, taxas `None`, nenhum campo (sem IR gravada) e fricção vazia."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    report = _report(world, dispatch)
    assert report.reach == Reach(0, 0, 0, ZERO_REASONS, 0, ZERO_STAGES, 0)
    _assert_pairs(report, answered=(0, 0), correct=(0, 0), partially_correct=(0, 0), incorrect=(0, 0),
                  dont_know=(0, 0))
    for name in ("correct_rate", "partially_correct_rate", "incorrect_rate", "response_rate", "mean_score"):
        assert getattr(report.performance, name) == RatePair(None, None), name
    rt = report.response_time
    assert (rt.observations, rt.negative, rt.median_ms, rt.q1_ms, rt.q3_ms) == (0, 0, None, None, None)
    assert report.distribution == Distribution(fields=())
    assert report.friction == Friction(fields=(), unmapped_format_errors=Pair(0, 0))
    assert report.grading_pending == 0


# --- gabarito ----------------------------------------------------------------------------------------


def test_single_variant_report_has_no_answer_key(sql: psycopg.Connection) -> None:
    """Na variante única, o valor mais comum é o correto, e o relatório não tem nenhum campo
    nem marca de gabarito; a sentinela do motor está em `correct_answers` da submissão e na IR (controle
    positivo) e não está no relatório; a explicação também não."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _variant(preset("single_variant"))  # correta: "b" (Azul)
    azul, verde = _hash(variant, "Azul"), _hash(variant, "Verde")
    for _ in range(3):
        world.answered(dispatch, variant, raw={"resposta": "b"}, part_derived=[_part(0, choices=[azul])])
    world.answered(dispatch, variant, score=0.0, raw={"resposta": "c"}, part_derived=[_part(0, choices=[verde])])
    assert variant.snapshot["correct_answers"][SENTINEL_KEY] == ANSWER_SENTINEL
    stored = sql.execute("SELECT count(*) FROM submissions WHERE correct_answers::text LIKE %s",
                         (f"%{ANSWER_SENTINEL}%",)).fetchone()[0]
    assert stored == 4
    in_ir = sql.execute("SELECT count(*) FROM delivery_contents WHERE ir::text LIKE %s",
                        (f"%{ANSWER_SENTINEL}%",)).fetchone()[0]
    assert in_ir == 4
    report = _report(world, dispatch)
    assert report.distribution.fields[0].choices == (ChoiceItem(azul, "Azul", Pair(3, 3)),
                                                     ChoiceItem(verde, "Verde", Pair(1, 1)))
    _assert_no_answer_key(report)
    assert "A resposta é a cor do céu" not in repr(report)


# --- cabeçalho e estados -----------------------------------------------------------------------------


def test_header_fields_and_grading_pending(sql: psycopg.Connection) -> None:
    """O cabeçalho: `report_schema_version = "1"`, o `OfferingRef`, o fuso, o QID, o
    `commit_short` de 12, o título redigido de novo (o nome de um aluno cadastrado depois da gravação vira a
    marca), a origem declarada, `partial` falso no fechado, os horários, `computed_at` = o `now` passado,
    `frozen = None`, `grading_pending` (só as `grading` no prazo) e `unavailable` sempre `None`; o texto das
    mensagens livres nunca sai."""
    world = World(sql)
    named = world.student(NAME)
    question = world.question(qid="aula03/velocidade", title=f"Questão de {NAME}")
    dispatch = world.dispatch(question, origin="api")
    variant = _number_variant()
    world.answered(dispatch, variant, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0)])
    for late in (False, False, True):
        row = world.delivery(dispatch, state="no_answer" if late else "delivered", variant=variant)
        world.submission(row, outcome="grading", valid=None, late=late, raw_input={"resposta": "6.5"})
    world.free_message(named, FREE_TEXT)
    report = _report(world, dispatch, identities=[named.identity])
    assert report.report_schema_version == "1"
    assert report.offering == OfferingRef(offering_id=world.offering_id, code=world.code)
    assert report.timezone == ZONE
    assert report.dispatch == DispatchHeader(
        dispatch_id=dispatch.dispatch_id, question=QuestionHeader(qid="aula03/velocidade", commit_short="a193f7204853"),
        title=f"Questão de {person_marker(named.alias)}", state="closed", partial=False, origin="api",
        opens_at=OPENS, closes_at=CLOSES)
    assert report.computed_at == NOW
    assert report.frozen is None
    assert report.grading_pending == 2
    assert all(item.unavailable is None for item in report.distribution.fields)
    assert NAME not in repr(report)
    assert FREE_TEXT not in repr(report)
    # A submissão em correção não conta nas respondidas (só a válida).
    _assert_pairs(report, answered=(1, 1))


def test_open_dispatch_is_partial(sql: psycopg.Connection) -> None:
    """o disparo aberto sai marcado como parcial, com o estado `open`; o título nulo fica `None`."""
    world = World(sql)
    dispatch = world.dispatch(world.question(title=None), state="open", origin="mcp")
    variant = _number_variant()
    world.answered(dispatch, variant, raw={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0)])
    world.delivery(dispatch, state="delivered", variant=variant)
    report = _report(world, dispatch)
    assert (report.dispatch.state, report.dispatch.partial, report.dispatch.title, report.dispatch.origin) == (
        "open", True, None, "mcp")
    assert report.reach.delivered == 2
    _assert_pairs(report, answered=(1, 1))
    _assert_rates(report, response_rate=(0.5, 0.5))


@pytest.mark.parametrize("state", ["validating", "rejected", "scheduled", "cancelled"])
def test_dispatch_states_without_report_are_refused(sql: psycopg.Connection, state: str) -> None:
    """Só `open` e `closed` têm relatório; os 4 demais estados → `DispatchStateConflict`."""
    world = World(sql)
    dispatch = world.dispatch(world.question(), state=state)
    assert _compute(world, dispatch) == DispatchStateConflict(state=state)


# --- volume, sem motor e sem canal -------------------------------------------------------------------


def test_large_dispatch_computed_without_engine_or_channel(sql: psycopg.Connection, engine_fake: Any,
                                                           bot_api: Any) -> None:
    """100 entregas e 300 tentativas (2 inválidas e 1 válida por entrega) calculadas sem chamar o motor nem
    o canal (o motor falso e a Bot API falsa sem nenhuma chamada)."""
    world = World(sql)
    dispatch = world.dispatch(world.question())
    variant = _number_variant()
    for _ in range(100):
        row = world.delivery(dispatch, state="answered", variant=variant)
        for _ in range(2):
            world.submission(row, valid=False, score=0.0, raw_input={"resposta": "x"},
                             format_errors=_format_error("resposta"))
        world.submission(row, score=1.0, raw_input={"resposta": "6.5"}, part_derived=[_part(0, ratio=1.0)],
                         response_ms=2000, correct_answers=variant.snapshot["correct_answers"])
    assert sql.execute("SELECT count(*) FROM submissions").fetchone()[0] == 300
    report = _report(world, dispatch)
    assert report.reach == Reach(100, 100, 0, ZERO_REASONS, 0, ZERO_STAGES, 0)
    _assert_pairs(report, answered=(100, 100), correct=(100, 100))
    assert report.friction == Friction(fields=(FieldFriction(0, Pair(200, 200), Pair(0, 0)),),
                                       unmapped_format_errors=Pair(0, 0))
    assert report.distribution.fields[0].values == TopList((TopItem("6.5", Pair(100, 100)),), Pair(0, 0))
    rt = report.response_time
    assert (rt.observations, rt.median_ms, rt.q1_ms, rt.q3_ms) == (100, 2000, 2000, 2000)
    assert engine_fake.calls() == []
    assert bot_api.calls() == []
