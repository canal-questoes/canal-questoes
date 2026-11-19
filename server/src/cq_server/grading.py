"""Correção das submissões e nova obtenção do conteúdo.
O motor corrige fora de transação; o desfecho é aplicado depois, com as travas, se a submissão ainda
está em `grading`.

- `submission.grade`: lê sem trava a submissão (fora de `grading` → sem efeito, sem chamar o motor);
  chama `engine.grade(ref, snapshot, answers.raw_for_engine(...))` **fora de transação**; depois
  trava disparo (`FOR SHARE`) → entrega (`FOR NO KEY UPDATE`) → submissão (`FOR UPDATE … outcome = 'grading'`;
  senão descarta: outra execução venceu) e aplica o desfecho:
  `Graded(valid)` → `answered`/`answered_late`, a revelação na mesma transação (também na atrasada) e o
  `submission.graded`; `Graded(invalid)` → a explicação traduzida (`formaterrors`), as partes com erro
  limpas (todas, com o erro fora de `fields[]`) e o novo pedido do campo; `Broken` →
  `question_failed/grading_broken` e o `delivery.question_failed`; `EngineRequestRejected` →
  `internal_failure`, a entrega reaberta, o `ANSWER_KEPT` uma vez por entrega e o
  `submission.internal_failure`; `EngineUnavailable`/`EngineNotFound` → a submissão continua
  `grading`, o aviso `GRADING_DELAYED` uma vez (`delay_notice_sent`) e `RetryTaskLater` (a espera da fila,
  até `backoff_max_seconds`; esgotada, a varredura cria a rodada seguinte: tentativas sem
  limite); no `EngineNotFound`, a `question.refetch` (uma viva por referência).
- `question.refetch`: `RepoFetcher.ensure` depois de um `EngineNotFound`; a submissão continua `grading`
  qualquer que seja o resultado.

Importa `deliveries` (pela renderização em `answers`), `answers` (`raw_for_engine` e os auxiliares da
entrega, para que a correção mexa na entrega como a resposta) e `formaterrors`. Nenhum texto do aluno sai
em evento, log ou `last_error`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server import answers, deliveries, deliverytexts, formaterrors, outbox  # noqa: F401 - dependências
from cq_server._leasing import LazyConnection, utc
from cq_server.clock import Clock
from cq_server.config import Settings
from cq_server.events import EventType, append_event
from cq_server.outbox import Outbox
from cq_server.ports.delivery import DeliveryChannel
from cq_server.ports.engine import (
    Broken,
    EngineNotFound,
    EngineRequestRejected,
    EngineUnavailable,
    Graded,
    QuestionEngine,
    QuestionRef,
)
from cq_server.ports.repos import FetchNotFound, FetchRefused, FetchTemporaryFailure, RepoFetcher
from cq_server.tasks import (
    ClaimedTask,
    PermanentTaskError,
    RetryTaskLater,
    enqueue_in_transaction,
    finished_rounds,
    has_live_task,
)

# Tipos de tarefa (payloads `{"submission_row_id": n}` e
# `{"question_ref_row_id": n}`).
GRADE_TASK_TYPE = "submission.grade"
REFETCH_TASK_TYPE = "question.refetch"

# A mensagem fixa da tarefa que precisa renderizar sem canal com rota.
NO_ROUTE_ERROR = "canal sem rota"


def _payload_id(task: ClaimedTask, key: str) -> int:
    payload = task.payload
    value = payload.get(key) if isinstance(payload, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PermanentTaskError(f"payload sem {key}")
    return value


def _json(value: Any) -> Any:
    """O valor devolvido pelo motor em JSON puro (as `Mapping` viram `dict`, as tuplas, listas)."""
    if isinstance(value, Mapping):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(v) for v in value]
    return value


def _jsonb(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(_json(value))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------------------------------
# Derivados por parte (`part_derived` e `submission.graded.parts`)
# --------------------------------------------------------------------------------------------------


def _part_score(partial: Any, name: str) -> float | None:
    """A nota da parte como o motor devolveu (`partial_scores[name].score`)."""
    item: Any = None
    if isinstance(partial, Mapping):
        item = partial.get(name)
    elif isinstance(partial, (list, tuple)):
        item = next((p for p in partial if isinstance(p, Mapping) and p.get("name") == name), None)
    if isinstance(item, Mapping):
        item = item.get("score")
    return _number(item)


def _choice_hashes(field_ir: Mapping[str, Any], value: Any) -> list[str]:
    """O `content_hash` das alternativas escolhidas, na ordem das opções (a múltipla digitada vale
    caractere a caractere, como o `set(submitted_key)` do `pl-checkbox`)."""
    if field_ir.get("type") not in ("single_choice", "multiple_choice"):
        return []
    if isinstance(value, str):
        chosen = {value} if field_ir.get("type") == "single_choice" else set(value)
    elif isinstance(value, (list, tuple)):
        chosen = {v for v in value if isinstance(v, str)}
    else:
        return []
    return [str(option["content_hash"]) for option in (field_ir.get("options") or ())
            if option.get("key") in chosen and option.get("content_hash") is not None]


def _numeric(field_ir: Mapping[str, Any], submitted: Any, correct: Any) -> tuple[float | None, float | None]:
    """(razão, resíduo) numéricos da parte: `submetido / gabarito` e `submetido − gabarito`, só nos
    campos `number` e `integer` com os dois valores numéricos finitos."""
    if field_ir.get("type") not in ("number", "integer"):
        return None, None
    s, c = _number(submitted), _number(correct)
    if s is None or c is None:
        return None, None
    residual = s - c
    ratio = s / c if c != 0 else None
    return (ratio if ratio is None or math.isfinite(ratio) else None,
            residual if math.isfinite(residual) else None)


def _parts(fields: Sequence[Mapping[str, Any]], raw_input: Mapping[str, Any], graded: Graded) -> list[dict]:
    """As partes do `submission.graded`, por índice do campo: nota, alternativas, razão e resíduo."""
    submitted = graded.submitted_answers if isinstance(graded.submitted_answers, Mapping) else {}
    correct = graded.correct_answers if isinstance(graded.correct_answers, Mapping) else {}
    out = []
    for index, field_ir in enumerate(fields):
        name = field_ir.get("name")
        ratio, residual = _numeric(field_ir, submitted.get(name), correct.get(name))
        out.append({"index": index, "score": _part_score(graded.partial_scores, name),
                    "choices": _choice_hashes(field_ir, raw_input.get(name)), "ratio": ratio,
                    "residual": residual})
    return out


def _explanation(fields: Sequence[Mapping[str, Any]], format_errors: Mapping[str, Any]) -> str:
    """A explicação traduzida dos erros de formato: uma frase por erro, na
    ordem dos campos (os de fora de `fields[]` por último), sem repetir; nada do texto do PL."""
    by_name = {f.get("name"): f for f in fields}
    names = [f.get("name") for f in fields if f.get("name") in format_errors]
    names += [name for name in format_errors if name not in by_name]
    sentences: list[str] = []
    for name in names:
        error = format_errors[name]
        field_ir = by_name.get(name)
        text = getattr(error, "text", "")
        sentence = formaterrors.translate(None if field_ir is None else field_ir.get("type"), text,
                                          hint=None if field_ir is None else field_ir.get("hint"))
        if sentence not in sentences:
            sentences.append(sentence)
    return " ".join(sentences) if sentences else formaterrors.OUTSIDE_FIELDS


# --------------------------------------------------------------------------------------------------
# `submission.grade`
# --------------------------------------------------------------------------------------------------


class GradeTaskHandler:
    """O tratador de `submission.grade` (chamável, com `close()`; conexão aberta no primeiro uso)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                 channel: DeliveryChannel | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._engine = engine
        self._channel = channel
        self._outbox = Outbox(conninfo, clock=clock, settings=settings)

    def __call__(self, task: ClaimedTask) -> None:
        submission_row = _payload_id(task, "submission_row_id")
        conn = self._db.get()
        row = conn.execute(
            "SELECT s.outcome, s.delivery_id, s.raw_input, s.normalized_fields, q.id, q.repo, q.commit_sha, "
            "q.qid, c.snapshot, c.ir FROM submissions s JOIN deliveries d ON d.id = s.delivery_id "
            "JOIN dispatches x ON x.id = d.dispatch_id JOIN question_refs q ON q.id = x.question_ref_id "
            "JOIN delivery_contents c ON c.delivery_id = d.id WHERE s.id = %s",
            (submission_row,),
        ).fetchone()
        if row is None or row[0] != answers.SubmissionOutcome.GRADING:
            return  # reexecução depois do commit, ou submissão inexistente: sem efeito
        channel = self._channel
        if channel is None:
            raise PermanentTaskError(NO_ROUTE_ERROR)
        delivery_row = int(row[1])
        ref_row = int(row[4])
        ref = QuestionRef(repo=row[5], commit=row[6], qid=row[7])
        raw = answers.raw_for_engine(dict(row[9] or {}), dict(row[2] or {}), list(row[3] or []))
        try:
            if self._engine is None:
                raise EngineUnavailable("unconfigured")
            result = self._engine.grade(ref, dict(row[8] or {}), raw)
        except (EngineUnavailable, EngineNotFound) as exc:
            self._delayed(conn, delivery_row, submission_row, ref_row=ref_row,
                          not_found=isinstance(exc, EngineNotFound))
            raise RetryTaskLater(0) from None
        except EngineRequestRejected as exc:
            self._record(conn, delivery_row, submission_row, channel=channel, rejected=exc.kind)
            return
        self._record(conn, delivery_row, submission_row, channel=channel, result=result)

    # --- correção pendente -----------------------------------------------------------------------

    def _delayed(self, conn: psycopg.Connection, delivery_row: int, submission_row: int, *, ref_row: int,
                 not_found: bool) -> None:
        """O motor não corrigiu agora: o aviso `GRADING_DELAYED` uma vez e, no `not_found`, a
        `question.refetch` se não há outra viva para a referência."""
        now = self._clock.now()
        with conn.transaction():
            view = answers.load_delivery(conn, delivery_row, lock=True)
            sub = conn.execute("SELECT delay_notice_sent FROM submissions WHERE id = %s AND outcome = 'grading' "
                               "FOR UPDATE", (submission_row,)).fetchone()
            if view is None or sub is None:
                return
            if not sub[0]:
                conn.execute("UPDATE submissions SET delay_notice_sent = true WHERE id = %s", (submission_row,))
                answers.notify(conn, self._outbox, view, deliverytexts.GRADING_DELAYED,
                               key=f"grading.delayed:{submission_row}")
            if not_found:
                subject = {"question_ref_row_id": ref_row}
                if not has_live_task(conn, REFETCH_TASK_TYPE, subject):
                    done, _last = finished_rounds(conn, REFETCH_TASK_TYPE, subject)
                    enqueue_in_transaction(conn, REFETCH_TASK_TYPE, subject, now=now,
                                           idempotency_key=f"{REFETCH_TASK_TYPE}:{ref_row}:r{done + 1}")

    # --- desfecho --------------------------------------------------------------------------------

    def _record(self, conn: psycopg.Connection, delivery_row: int, submission_row: int, *,
                channel: DeliveryChannel, result: Graded | Broken | None = None,
                rejected: str | None = None) -> None:
        now = self._clock.now()
        with conn.transaction():
            view = answers.load_delivery(conn, delivery_row, lock=True)
            sub = conn.execute(
                "SELECT submission_uuid, received_at, raw_input FROM submissions "
                "WHERE id = %s AND outcome = 'grading' FOR UPDATE",
                (submission_row,),
            ).fetchone()
            if view is None or sub is None:
                return  # outra execução venceu
            contents = answers.load_contents(conn, delivery_row)
            if contents is None:  # pragma: no cover - a submissão só nasce depois da variante
                return
            submission_uuid, received_at, raw_input = sub[0], utc(sub[1]), dict(sub[2] or {})
            if rejected is not None:
                self._rejected(conn, view, contents, submission_row, submission_uuid, rejected, now=now,
                               channel=channel)
            elif isinstance(result, Broken):
                self._broken(conn, view, contents, submission_row, result, now=now, channel=channel)
            elif isinstance(result, Graded) and result.valid:
                self._graded(conn, view, contents, submission_row, submission_uuid, result, received_at,
                             raw_input, now=now, channel=channel)
            elif isinstance(result, Graded):
                self._invalid(conn, view, contents, submission_row, result, received_at, now=now,
                              channel=channel)
            else:  # pragma: no cover - a porta só devolve `Graded` ou `Broken`
                raise PermanentTaskError("desfecho do motor desconhecido")

    @staticmethod
    def _response_ms(view: answers.DeliveryView, received_at: datetime) -> int | None:
        """`received_at − sent_at` em milissegundos, com sinal (negativo se a resposta chegou antes do `sent_at`)."""
        if view.sent_at is None:
            return None
        return int((received_at - view.sent_at) / timedelta(milliseconds=1))

    def _store_graded(self, conn: psycopg.Connection, submission_row: int, result: Graded, *, view:
                      answers.DeliveryView, received_at: datetime, now: datetime,
                      part_derived: list[dict] | None) -> None:
        """A submissão `graded` com tudo como o motor devolveu."""
        format_errors = {name: {"html": error.html, "text": error.text}
                         for name, error in result.format_errors.items()}
        mismatch = result.version_mismatch
        conn.execute(
            "UPDATE submissions SET outcome = 'graded', valid = %s, score = %s, partial_scores = %s, "
            "format_errors = %s, submitted_answers = %s, feedback = %s, params = %s, correct_answers = %s, "
            "part_derived = %s, engine_mismatch = %s, pl_mismatch = %s, response_ms = %s, graded_at = %s "
            "WHERE id = %s",
            (result.valid, result.score, _jsonb(result.partial_scores), Jsonb(format_errors),
             _jsonb(result.submitted_answers), _jsonb(result.feedback), _jsonb(result.params),
             _jsonb(result.correct_answers), _jsonb(part_derived),
             None if mismatch is None else mismatch.engine, None if mismatch is None else mismatch.pl,
             self._response_ms(view, received_at), now, submission_row),
        )

    def _graded(self, conn: psycopg.Connection, view: answers.DeliveryView, contents: answers.Contents,
                submission_row: int, submission_uuid: Any, result: Graded, received_at: datetime,
                raw_input: Mapping[str, Any], *, now: datetime, channel: DeliveryChannel) -> None:
        """`Graded(valid)`: `answered` (ou `answered_late` a partir de `no_answer`), a revelação na mesma
        transação, no desfecho de correção pendente no fechamento e na atrasada, a edição com
        o feedback (e, com o disparo fechado, o gabarito permitido na mesma edição) e o
        `submission.graded`."""
        fields = contents.fields
        parts = _parts(fields, raw_input, result)
        derived = [{k: p[k] for k in ("index", "choices", "ratio", "residual")} for p in parts]
        self._store_graded(conn, submission_row, result, view=view, received_at=received_at, now=now,
                           part_derived=derived)
        normalized = conn.execute("SELECT normalized_fields FROM submissions WHERE id = %s",
                                  (submission_row,)).fetchone()[0]
        late = received_at > view.closes_at  # o gravado até o horário conta no prazo
        if view.state in ("delivered", "no_answer"):
            new_state = "answered_late" if view.state == "no_answer" else "answered"
            row = conn.execute(
                "UPDATE deliveries SET state = %s, answered_at = %s, current_field = NULL, "
                "controls_version = controls_version + 1 WHERE id = %s RETURNING controls_version",
                (new_state, received_at, view.id),
            ).fetchone()
            view.state, view.current_field = new_state, None
            view.controls_version = int(row[0])
            answers.reveal_now(conn, view, now)
            outbox.cancel_delivery_messages(conn, delivery_row_id=view.id, role="field_prompt")
            answers.refresh(conn, view, contents, channel=channel, now=now, notice_key=f"s{submission_row}")
        append_event(conn, EventType.SUBMISSION_GRADED,
                     {"offering_id": view.offering_uuid, "dispatch_id": view.dispatch_uuid,
                      "delivery_id": view.delivery_uuid, "submission_id": submission_uuid,
                      "question_ref_id": view.question_ref_uuid, "person": view.pseudo_id, "valid": True,
                      "late": late, "normalized": bool(normalized), "score": result.score, "parts": parts},
                     occurred_at=now, actor_token_id=None)

    def _close_if_needed(self, conn: psycopg.Connection, view: answers.DeliveryView, now: datetime) -> None:
        """Com o disparo já fechado, a entrega `delivered` sem desfecho vira `no_answer` sem revelação,
        com os controles e o "Não sei"; ela aceita a atrasada e o
        "Não sei" atrasado. A edição sai pelo `move_to_field` e pelo `refresh` de quem a chama."""
        del now
        if view.dispatch_state == "closed" and view.state == "delivered":
            conn.execute("UPDATE deliveries SET state = 'no_answer' WHERE id = %s", (view.id,))
            view.state = "no_answer"

    def _invalid(self, conn: psycopg.Connection, view: answers.DeliveryView, contents: answers.Contents,
                 submission_row: int, result: Graded, received_at: datetime, *, now: datetime,
                 channel: DeliveryChannel) -> None:
        """`Graded(invalid)`: o erro não conta tentativa; limpa só as partes com erro e
        volta à primeira delas, ou, sem nenhuma chave de campo, todas as partes e o primeiro campo
        (erro fora de `fields[]`); a explicação traduzida, o novo pedido e a edição com os controles novos."""
        self._store_graded(conn, submission_row, result, view=view, received_at=received_at, now=now,
                           part_derived=None)
        fields = contents.fields
        errored = [i for i, f in enumerate(fields) if f.get("name") in result.format_errors]
        if errored:
            parts = {k: v for k, v in contents.parts.items()
                     if k not in {fields[i].get("name") for i in errored}}
            target = errored[0]
        else:
            parts, target = {}, 0
        answers.notify(conn, self._outbox, view,
                       deliverytexts.FORMAT_ERROR.format(explanation=_explanation(fields, result.format_errors)),
                       key=f"grading.format_error:{submission_row}")
        self._close_if_needed(conn, view, now)
        answers.move_to_field(conn, view, contents, target, parts=parts, channel=channel,
                              prompt_key=f"s{submission_row}", now=now)
        answers.refresh(conn, view, contents, channel=channel, now=now)

    def _broken(self, conn: psycopg.Connection, view: answers.DeliveryView, contents: answers.Contents,
                submission_row: int, result: Broken, *, now: datetime, channel: DeliveryChannel) -> None:
        """`Broken`: `question_failed/grading_broken`, sem revelação (a `no_answer` não tem
        `revealed_at`), a edição sem controles e o `delivery.question_failed`."""
        mismatch = result.version_mismatch
        conn.execute(
            "UPDATE submissions SET outcome = 'broken', failure_code = %s, failure_phase = %s, "
            "engine_mismatch = %s, pl_mismatch = %s, graded_at = %s WHERE id = %s",
            (result.code, result.phase, None if mismatch is None else mismatch.engine,
             None if mismatch is None else mismatch.pl, now, submission_row),
        )
        if view.state not in ("delivered", "no_answer"):
            return
        row = conn.execute(
            "UPDATE deliveries SET state = 'question_failed', failure = 'grading_broken', current_field = NULL, "
            "controls_version = controls_version + 1 WHERE id = %s RETURNING controls_version",
            (view.id,),
        ).fetchone()
        view.state, view.current_field = "question_failed", None
        view.controls_version = int(row[0])
        outbox.cancel_delivery_messages(conn, delivery_row_id=view.id, role="field_prompt")
        answers.refresh(conn, view, contents, channel=channel, now=now)
        append_event(conn, EventType.DELIVERY_QUESTION_FAILED,
                     {"offering_id": view.offering_uuid, "dispatch_id": view.dispatch_uuid,
                      "delivery_id": view.delivery_uuid, "person": view.pseudo_id, "failure": "grading_broken"},
                     occurred_at=now, actor_token_id=None)

    def _rejected(self, conn: psycopg.Connection, view: answers.DeliveryView, contents: answers.Contents,
                  submission_row: int, submission_uuid: Any, kind: str, *, now: datetime,
                  channel: DeliveryChannel) -> None:
        """`EngineRequestRejected` (400/413): `internal_failure`, a entrega reaberta no
        primeiro campo (partes e seleção limpas), o `ANSWER_KEPT` uma vez por entrega e o
        `submission.internal_failure`; com o disparo já fechado, `no_answer`
        sem a revelação."""
        kept_before = conn.execute(
            "SELECT 1 FROM submissions WHERE delivery_id = %s AND outcome = 'internal_failure' LIMIT 1",
            (view.id,),
        ).fetchone() is not None
        conn.execute("UPDATE submissions SET outcome = 'internal_failure', failure_code = %s, graded_at = %s "
                     "WHERE id = %s", (kind, now, submission_row))
        if not kept_before:
            answers.notify(conn, self._outbox, view, deliverytexts.ANSWER_KEPT,
                           key=f"grading.answer_kept:{view.id}")
        if view.state in ("delivered", "no_answer"):
            self._close_if_needed(conn, view, now)
            answers.move_to_field(conn, view, contents, 0, parts={}, channel=channel,
                                  prompt_key=f"s{submission_row}", now=now)
            answers.refresh(conn, view, contents, channel=channel, now=now)
        append_event(conn, EventType.SUBMISSION_INTERNAL_FAILURE,
                     {"offering_id": view.offering_uuid, "dispatch_id": view.dispatch_uuid,
                      "delivery_id": view.delivery_uuid, "submission_id": submission_uuid,
                      "person": view.pseudo_id, "engine_error": kind},
                     occurred_at=now, actor_token_id=None)

    def close(self) -> None:
        """Fecha as conexões do tratador (idempotente)."""
        self._db.close()
        self._outbox.close()


def grade_task_handler(conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                       channel: DeliveryChannel | None) -> GradeTaskHandler:
    """O tratador de `submission.grade`, para `run_worker`. Não lê o canal na
    construção; sem motor, toda correção fica pendente (`EngineUnavailable("unconfigured")`)."""
    return GradeTaskHandler(conninfo, clock=clock, settings=settings, engine=engine, channel=channel)


# --------------------------------------------------------------------------------------------------
# `question.refetch`
# --------------------------------------------------------------------------------------------------


class RefetchTaskHandler:
    """O tratador de `question.refetch` (chamável, com `close()`; conexão aberta no primeiro uso)."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, fetcher: RepoFetcher | None) -> None:
        del clock, settings
        self._db = LazyConnection(conninfo)
        self._fetcher = fetcher

    def __call__(self, task: ClaimedTask) -> None:
        ref_row = _payload_id(task, "question_ref_row_id")
        row = self._db.get().execute("SELECT repo, commit_sha FROM question_refs WHERE id = %s",
                                     (ref_row,)).fetchone()
        if row is None:
            raise PermanentTaskError("referência inexistente")
        if self._fetcher is None:
            raise RetryTaskLater(0)  # obtenção desligada: `repos_unavailable`, repetível
        result = self._fetcher.ensure(row[0], row[1])
        if isinstance(result, FetchTemporaryFailure):
            raise RetryTaskLater(0)
        if isinstance(result, FetchNotFound):
            # A submissão continua `grading`; a correção repete sem limite.
            raise PermanentTaskError("conteúdo não encontrado na origem")
        if isinstance(result, FetchRefused):
            raise PermanentTaskError("obtenção recusada pela política")

    def close(self) -> None:
        """Fecha a conexão do tratador (idempotente)."""
        self._db.close()


def refetch_task_handler(conninfo: str, *, clock: Clock, settings: Settings,
                         fetcher: RepoFetcher | None) -> RefetchTaskHandler:
    """O tratador de `question.refetch`, para `run_worker`."""
    return RefetchTaskHandler(conninfo, clock=clock, settings=settings, fetcher=fetcher)
