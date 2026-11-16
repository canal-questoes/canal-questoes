"""Disparos pela API: preparo, consulta, confirmação, cancelamento e lista.

- **Preparo e consulta**: o preparo
  válido devolve o `Dispatch` exato em `validating`, sem mensagem, sem chamada ao motor nem à obtenção
  pela API, com uma tarefa `dispatch.validate` e o `dispatch.prepared` exato com o ator; o imediato sem
  fechamento (a janela do oferecimento), com fechamento a 30 dias (aceito) e a 60 dias (`out_of_range`);
  o agendado até 90 dias; cada regra do preparo com o `reason` exato e sem eco; a origem pelo
  corpo ou pelo `X-CQ-Client`; a ordem das regras; `dispatches.get` com a prévia depois da validação,
  em UTC com `Z`, com as contagens e com 404 para disparo inexistente ou de outro oferecimento; repetir
  o preparo cria outro disparo; nenhuma resposta com identidade nem lista de pseudônimos.
- **Confirmação** (a janela efetiva e o alvo que mudou entre a prévia e a confirmação): a confirmação do agendado sem
  confirmação (`confirmed`, `confirmed_at`, o alvo **agora**; no imediato, `opens_at = agora` e
  `closes_at`), o `dispatch.confirmed` exato com o ator, a ordem das regras (404 → 404 → `invalid_state`
  → `bot_required` → `invalid_dispatch_state` → `window_expired`), `invalid_dispatch_state` em cada
  estado que não é "agendado sem confirmação" e com o prazo vencido, `window_expired` com a janela
  efetiva abaixo de 5 min ou o fechamento no passado, a segunda confirmação recusada, o alvo que mudou
  desde a prévia sem invalidar a confirmação; nada muda nas recusas.
- **Cancelamento e lista**: cancelar `validating` e `scheduled` (confirmado ou não)
  → `cancelled/operator` com o evento e o ator, e nada é enviado mesmo que a validação termine depois ou
  que a abertura agendada chegue; `open`, `closed`, `rejected` e `cancelled` → `invalid_dispatch_state`;
  `dispatches.list` com o `DispatchSummary` exato, paginação por `id` e só contagens.

Contratos: o contrato da API dos disparos (convenções, erros, ordem das
verificações, as 5 operações de disparo e o objeto `Dispatch`); o esquema do banco (referências, disparos,
chaves das tarefas, transições e eventos `dispatch.*`); as interfaces do servidor que os testes usam.

Convenções dos testes: corpo inteiro por `==` e `assert_matches` (pelos atalhos de
`tests/helpers/api.py`) em toda resposta de sucesso; erros por `assert_error` (status, `code`, `message`,
`fields` e `state` exatos) e `assert_no_echo`; horários pelo `FakeClock` e `iso(dt)`; ids de disparo, de
referência, internos, `seq` e pseudônimos nunca como literal (só as relações e as formas). O motor falso
estrito (`engine_fake`, com `protocol_errors() == []` exigido pela fixture) e a obtenção falsa
(`repo_fetcher`) atendem toda validação; nenhum teste chama `git`, Telegram ou motor reais.

Como os estados são montados (pela injeção de falhas dos testes; sem `UPDATE dispatches` por SQL):
- a validação, a abertura e o fechamento rodam pelos tratadores reais do núcleo, montados como o
  `run_worker` monta (o `HttpQuestionEngine` sobre o `engine_fake`, o `repo_fetcher` e o
  `TelegramChannel` com rota sobre a fixture `bots`), tomando as tarefas por SQL uma a uma, no molde da
  semente de `tests/helpers/calls.py`; a tarefa `dispatch.close` sai da varredura real
  (`scheduler.sweep_once` com `DISPATCH_RULES`), que é o único produtor dela;
- a rejeição pela obtenção falsa programada (`FetchNotFound("commit")` → `commit_not_found`);
- o cancelamento `validation_overran` pelo relógio (a validação de um agendado que começa com `now >=
  opens_at`) — os testes da confirmação não usam `dispatches.cancel`, que é testada à parte;
- o "nada é enviado depois do cancelamento" com o worker real (`bot_worker_thread` com o motor e a
  obtenção falsos), com controles positivos (um disparo irmão que valida e outro que abre);
- o aluno vinculado, pelo fluxo real do vínculo (importação, convite pelo SMTP falso, `/start` e `/aceito` pela
  Bot API falsa, com o worker), só onde o alvo importa;
- o bot sem acesso e o oferecimento sem bot, por SQL em `bots`, como nos testes de oferecimentos e do vínculo.

Leituras [I] deste arquivo (inferências, não fatos do contrato):
- `target_count` só é não nulo em `scheduled` (o alvo **agora**), `open` e `closed` (o alvo fixado); em
  `validating`, `rejected` e `cancelled`, é nulo (no agendado, o alvo é o de agora; depois da
  abertura, o alvo fixado);
- a chave da tarefa do preparo é `dispatch.validate:<dispatch_row_id>` e a da confirmação do imediato é
  `dispatch.open:<dispatch_row_id>` (a chave base; a varredura
  acrescenta `:r<k>`); a confirmação do agendado não enfileira tarefa (nenhuma tarefa
  com `available_at` futuro; a varredura abre no horário);
- a mensagem dos avisos da prévia é texto livre sem forma fixa: os testes conferem os `kind` exatos e a
  mensagem não vazia; os disparos de um mesmo teste usam commits distintos (outra referência), para que o
  aviso `repeated_question` não dependa da leitura de "já disparada"; os oferecimentos deste arquivo ficam
  no padrão (mensagens livres desligadas e sem contato do professor), e toda prévia validada traz, por
  último, o aviso `no_teacher_contact` (ele se acrescenta aos outros);
- o enunciado da prévia é o `plain` do formatador do canal com rota (`TelegramRichFormatter`, o modo
  padrão de `DeliverySettings()`) sobre o HTML do enunciado da variante de amostra que o motor falso
  emitiu, e começa pelo texto do enunciado do preset.

No esqueleto, as rotas `POST|GET
/v1/offerings/{ref}/dispatches`, `GET …/dispatches/{dispatch_id}`, `POST …/confirm` e `POST …/cancel` não
existem (404 no lugar do 200, 400 ou 409 esperado: rota ausente), e a migração `0004` não foi aplicada
(tabelas `dispatches` e `question_refs` ausentes). Cada função falha no primeiro pedido de disparo ou na
primeira leitura dessas tabelas. As asserções sobre o que o esqueleto já tem (401 sem token, o 404 de
oferecimento inexistente, que no esqueleto coincide com o da rota ausente) ficam na mesma função de um
caso que falha.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import itertools
import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest

from cq_server.ports.repos import FetchNotFound
from cq_server.questionrefs import repo_key
from tests.helpers.api import (
    ADMIN_TOKEN,
    activate_bot,
    auth,
    cancel_dispatch,
    confirm_dispatch,
    create_offering,
    create_token,
    get_dispatch,
    import_students,
    list_dispatches,
    list_events,
    prepare_dispatch,
    prepare_invites,
    register_bot,
    send_invites,
)
from tests.helpers.bot_api import make_user
from tests.helpers.clock import iso
from tests.helpers.contract import assert_error, assert_matches, assert_no_echo
from tests.helpers.engine import ANSWER_SENTINEL

ADMIN = auth(ADMIN_TOKEN)

# O oferecimento dos testes: fuso fora de São Paulo (o `timezone` da resposta vem dele) e janela padrão
# de 60 min (o fechamento do agendado sem `closes_at` e do imediato na confirmação).
TZ = "America/Recife"
WINDOW = timedelta(minutes=60)
# Todo oferecimento deste arquivo fica no padrão (mensagens livres
# desligadas e sem contato do professor), e a prévia de todo disparo validado traz este aviso, por último.
NO_TEACHER_CONTACT = "no_teacher_contact"

# A questão: um repositório no host de `TEST_REPO_SETTINGS` (`repos.cq.test`, a fixture `app`),
# um commit SHA-1 e o QID do preset `number` do motor falso, que existe na árvore `course` da obtenção falsa.
HOST = "repos.cq.test"
REPO = f"https://{HOST}/disciplina/questoes-disparo.git"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
COMMIT_SHA256 = "89abcdef" * 8
QID = "number"
# O preset `number` (tests/helpers/engine.py): título, enunciado e o campo (rótulo, sufixo e a dica do motor).
TITLE = "Velocidade média"
STATEMENT = "Um corpo percorre 13 m em 2 s. Qual é a velocidade média?"
FIELD = {"type": "number", "label": "Velocidade:", "suffix": "m/s", "hint": "Número; ex.: 4.25, -17 ou 3/7"}

OTHER_HOST_REPO = "https://git.outra.test/disciplina/questoes-disparo.git"

UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
MISSING_OFFERING_ID = "0e0e0e0e-0e0e-4e0e-8e0e-0e0e0e0e0e0e"
MISSING_DISPATCH_ID = "0d0d0d0d-0d0d-4d0d-8d0d-0d0d0d0d0d0d"

ZERO_DELIVERIES = {"pending": 0, "delivered": 0, "not_delivered": 0, "answered": 0, "dont_know": 0,
                   "no_answer": 0, "answered_late": 0, "question_failed": 0, "grading_pending": 0}
# a mensagem pareada de cada classe de rejeição.
COMMIT_NOT_FOUND = {"reason": "commit_not_found", "message": "O commit informado não existe no repositório.",
                    "details": []}

# Os tipos de evento do disparo, como literais (o esqueleto ainda não os tem).
DISPATCH_EVENT_TYPES = ("dispatch.prepared", "dispatch.validated", "dispatch.rejected", "dispatch.confirmed",
                        "dispatch.cancelled", "dispatch.opened", "dispatch.closed")
SUMMARY_DROPPED = ("offering", "timezone", "preview")

WORKER = "teste-disparos"
PROCESS_TIMEOUT = 15.0
# a posse por SQL só da tarefa disponível de menor `id` do tipo pedido (a forma do
# `TaskQueue.claim` e da semente de `tests/helpers/calls.py`).
_CLAIM_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, "
    "attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' "
    "            AND available_at <= %(now)s ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")

_counter = itertools.count(1)


# ---------------------------------------------------------------------------------------------
# Apoio
# ---------------------------------------------------------------------------------------------


@dataclasses.dataclass
class _H:
    """O que os testes deste arquivo usam: a API do teste, o banco, o relógio, o runtime dos bots, a Bot
    API falsa, o motor e a obtenção falsos, e o `request` (para as fixtures pedidas só por alguns casos)."""

    client: Any
    clock: Any
    sql: psycopg.Connection
    db_url: str
    settings: Any
    bots: Any
    bot_api: Any
    engine_fake: Any
    repo_fetcher: Any
    request: Any


@pytest.fixture
def h(client: Any, clock: Any, sql: psycopg.Connection, db_url: str, settings: Any, bots: Any, bot_api: Any,
      engine_fake: Any, repo_fetcher: Any, request: pytest.FixtureRequest) -> _H:
    # O preset `number` para o QID em qualquer repositório e commit (a árvore vem da obtenção falsa).
    engine_fake.program(QID, "number")
    return _H(client=client, clock=clock, sql=sql, db_url=db_url, settings=settings, bots=bots,
              bot_api=bot_api, engine_fake=engine_fake, repo_fetcher=repo_fetcher, request=request)


def _commit() -> str:
    """Um commit SHA-1 novo (outra referência, para o aviso `repeated_question` não entrar)."""
    return f"{next(_counter):040x}"


def _oref(offering: dict) -> dict:
    """O `OfferingRef` das respostas (o contrato da API)."""
    return {"offering_id": offering["offering_id"], "code": offering["code"]}


def _offering(h: _H, prefix: str, *, bot: bool = True, activate: bool = True) -> tuple[dict, int | None]:
    """Um oferecimento neutro (fuso `TZ`, janela de 60 min), com um bot novo da Bot API falsa registrado e,
    salvo `activate=False`, ativado; devolve o corpo do cadastro e o id do bot no Telegram."""
    offering = create_offering(h.client, ADMIN_TOKEN, code=f"{prefix}-{next(_counter)}", timezone=TZ,
                               response_window_minutes=int(WINDOW.total_seconds() // 60))
    if not bot:
        return offering, None
    bot_id = h.bot_api.add_bot()
    register_bot(h.client, ADMIN_TOKEN, offering["code"], h.bot_api.token_of(bot_id))
    if activate:
        activate_bot(h.client, ADMIN_TOKEN, offering["code"])
    return offering, bot_id


def _ok(resp: Any, op_id: str) -> dict:
    """200 com o corpo validado pelo esquema congelado da operação."""
    assert resp.status_code == 200, f"{op_id}: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches(op_id, 200, body)
    return body


def _post_close(h: _H, ref: str) -> None:
    assert _ok(h.client.post(f"/v1/offerings/{ref}/close", headers=ADMIN), "offerings.close")["state"] == "closed"


def _post_archive(h: _H, ref: str) -> None:
    prepared = _ok(h.client.post(f"/v1/offerings/{ref}/archive/prepare", headers=ADMIN), "offerings.prepare_archive")
    body = _ok(h.client.post(f"/v1/offerings/{ref}/archive", json={"confirmation_code": prepared["confirmation_code"]},
                             headers=ADMIN), "offerings.archive")
    assert body["state"] == "archived"


def _body(*, repo: str = REPO, commit: str = COMMIT, qid: str = QID, **extra: Any) -> dict:
    """O pedido de preparo; `extra` só com os campos não nulos."""
    return {"repo": repo, "commit": commit, "qid": qid, **{k: v for k, v in extra.items() if v is not None}}


def _post_prepare(h: _H, ref: str, body: Any = None, *, token: str | None = ADMIN_TOKEN,
                  client_kind: str | None = None, raw: bool = False) -> Any:
    """`POST /v1/offerings/{ref}/dispatches` direto (para os erros e para o `X-CQ-Client`)."""
    headers = auth(token, client_kind)
    if raw:
        return h.client.post(f"/v1/offerings/{ref}/dispatches", headers=headers)
    return h.client.post(f"/v1/offerings/{ref}/dispatches", json=_body() if body is None else body,
                         headers=headers)


def _prepare(h: _H, offering: dict, *, commit: str | None = None, opens_at: datetime | None = None,
             closes_at: datetime | None = None, token: str = ADMIN_TOKEN, by_id: bool = False) -> dict:
    """Prepara pela API (atalho de `tests/helpers/api.py`: 200 e esquema) e confere o `Dispatch` inteiro em
    `validating` (agendado sem fechamento: abertura + janela do oferecimento; imediato: o fechamento
    informado ou nulo); devolve o corpo."""
    commit = _commit() if commit is None else commit
    now = h.clock.now()
    body = prepare_dispatch(h.client, token, offering["offering_id"] if by_id else offering["code"], repo=REPO,
                            commit=commit, qid=QID, opens_at=None if opens_at is None else iso(opens_at),
                            closes_at=None if closes_at is None else iso(closes_at))
    expected_closes = closes_at if opens_at is None or closes_at is not None else opens_at + WINDOW
    assert body == _expected(offering, body, created_at=now, commit=commit, opens_at=opens_at,
                             closes_at=expected_closes)
    return body


def _t(value: datetime | None) -> str | None:
    return None if value is None else iso(value)


def _expected(offering: dict, prepared: dict, *, created_at: datetime, commit: str, state: str = "validating",
              origin: str = "api", opens_at: datetime | None = None, closes_at: datetime | None = None,
              repo: str = REPO, qid: str = QID) -> dict:
    """O `Dispatch` exato de um disparo recém-preparado: os ids lidos da resposta (só pela
    forma), todo o resto por valor."""
    assert UUID4.fullmatch(prepared["dispatch_id"]), "dispatch_id fora de UUID v4 canônico"
    assert UUID4.fullmatch(prepared["question"]["question_ref_id"]), "question_ref_id fora de UUID v4 canônico"
    return {"offering": _oref(offering), "timezone": TZ, "dispatch_id": prepared["dispatch_id"],
            "question": {"question_ref_id": prepared["question"]["question_ref_id"], "repo": repo,
                         "commit": commit, "qid": qid},
            "state": state, "confirmed": False, "origin": origin, "opens_at": _t(opens_at),
            "closes_at": _t(closes_at), "created_at": iso(created_at), "validated_at": None,
            "confirmed_at": None, "rejected_at": None, "opened_at": None, "closed_at": None,
            "cancelled_at": None, "rejection": None, "cancel_reason": None, "target_count": None,
            "deliveries": dict(ZERO_DELIVERIES), "preview": None}


def _summary(dispatch: dict) -> dict:
    """O `DispatchSummary`: o `Dispatch` sem `offering`, `timezone` e `preview`."""
    return {k: v for k, v in dispatch.items() if k not in SUMMARY_DROPPED}


def _statement(h: _H) -> str:
    """O enunciado esperado da prévia (ver a leitura [I] do docstring do módulo): o `plain` do
    formatador `rich` sobre o HTML do enunciado das variantes de amostra que o motor falso emitiu."""
    from cq_server.telegramrender import TelegramRichFormatter

    htmls = {v.ir["statement"]["html"] for v in h.engine_fake.issued_variants(QID)}
    assert len(htmls) == 1, f"variantes de amostra com enunciados distintos: {len(htmls)}"
    statement = TelegramRichFormatter().plain(htmls.pop())
    assert statement.startswith(STATEMENT), "o enunciado da prévia precisa começar pelo texto da questão"
    assert ANSWER_SENTINEL not in statement
    return statement


def _preview(h: _H, body: dict, *, target_count: int, kinds: tuple[str, ...]) -> dict:
    """A prévia esperada: título, enunciado, o campo, o alvo e os avisos com os
    `kind` exatos; a mensagem do aviso é texto livre (não vazio), lida da própria resposta."""
    preview = body["preview"]
    assert preview is not None, "disparo validado sem prévia"
    warnings = preview["warnings"]
    assert [w["kind"] for w in warnings] == list(kinds)
    assert all(w["message"].strip() for w in warnings), "aviso sem mensagem"
    return {"title": TITLE, "statement": _statement(h), "fields": [dict(FIELD)], "target_count": target_count,
            "warnings": warnings}


def _row_id(h: _H, dispatch_id: str) -> int:
    row = h.sql.execute("SELECT id FROM dispatches WHERE dispatch_uuid = %s", (uuid.UUID(dispatch_id),)).fetchone()
    assert row is not None, "disparo ausente do banco"
    return int(row[0])


def _dispatch_row(h: _H, dispatch_id: str) -> tuple:
    """(state, origin, immediate, opens_at, closes_at, window_minutes, preview IS NULL, rejection,
    cancel_reason, validated_at, confirm_deadline, confirmed_at, opened_at, closed_at, rejected_at,
    cancelled_at) da linha do disparo."""
    return h.sql.execute(
        "SELECT state, origin, immediate, opens_at, closes_at, window_minutes, preview IS NULL, rejection, "
        "cancel_reason, validated_at, confirm_deadline, confirmed_at, opened_at, closed_at, rejected_at, "
        "cancelled_at FROM dispatches WHERE dispatch_uuid = %s", (uuid.UUID(dispatch_id),)).fetchone()


def _new_tasks(h: _H, after_id: int) -> list[tuple]:
    """(task_type, payload, idempotency_key, state, attempts, available_at) das tarefas com `id > after_id`."""
    return h.sql.execute(
        "SELECT task_type, payload, idempotency_key, state, attempts, available_at FROM tasks WHERE id > %s "
        "ORDER BY id", (after_id,)).fetchall()


def _last_task(h: _H) -> int:
    return h.sql.execute("SELECT coalesce(max(id), 0) FROM tasks").fetchone()[0]


def _last_seq(h: _H) -> int:
    return h.sql.execute("SELECT coalesce(max(seq), 0) FROM events").fetchone()[0]


def _events(h: _H, cursor: int) -> list[dict]:
    """Os eventos `dispatch.*` depois de `cursor`, pela API, em ordem de `seq`, sem o `seq`."""
    items = list_events(h.client, ADMIN_TOKEN, *DISPATCH_EVENT_TYPES, after=cursor, limit=500)["items"]
    seqs = [e["seq"] for e in items]
    assert seqs == sorted(set(seqs)), f"seq fora de ordem ou repetido: {seqs}"
    return [{k: v for k, v in e.items() if k != "seq"} for e in items]


def _ev(event_type: str, at: datetime, actor: str | None, **data: Any) -> dict:
    return {"type": event_type, "occurred_at": iso(at), "actor_token_id": actor, "data": data}


def _fingerprint(h: _H) -> dict:
    """O que uma recusa não pode mudar: as linhas de `dispatches` e `question_refs`, as tarefas, as
    entregas, a fila de saída e os eventos."""
    return {
        "dispatches": h.sql.execute("SELECT to_jsonb(d) FROM dispatches d ORDER BY id").fetchall(),
        "question_refs": h.sql.execute("SELECT to_jsonb(q) FROM question_refs q ORDER BY id").fetchall(),
        "tasks": h.sql.execute("SELECT id, task_type, state, attempts, available_at, idempotency_key "
                               "FROM tasks ORDER BY id").fetchall(),
        "deliveries": h.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0],
        "outbox": h.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0],
        "events": h.sql.execute("SELECT seq, type FROM events ORDER BY seq").fetchall(),
    }


def _sends(h: _H) -> list[str]:
    """Os métodos de envio ou edição que a Bot API falsa recebeu (nenhum, nos disparos sem alvo)."""
    return [c.method for c in h.bot_api.calls() if c.method.startswith(("send", "edit"))]


def _no_identity(body: Any, people: list[tuple[str, str]]) -> None:
    """nenhuma resposta com identidade nem lista de pseudônimos."""
    text = json.dumps(body, ensure_ascii=False)
    for pseudo_id, alias in people:
        assert pseudo_id not in text and alias not in text, "pseudônimo de aluno numa resposta de disparo"


# --- os tratadores reais do núcleo, montados como o `run_worker` -------------------------------------


def _drain(h: _H, task_type: str, make: Callable[[contextlib.ExitStack], Any]) -> int:
    """Processa, uma a uma, as tarefas `task_type` disponíveis (posse por SQL, como a semente) com o
    tratador de `make` e as conclui pelo `TaskQueue.complete`; devolve quantas processou."""
    from cq_server.tasks import ClaimedTask, TaskQueue

    lease = timedelta(seconds=h.settings.lease_seconds)
    processed = 0
    with contextlib.ExitStack() as stack:
        queue = TaskQueue(h.db_url, clock=h.clock, settings=h.settings)
        stack.callback(queue.close)
        handler = make(stack)
        stack.callback(handler.close)
        conn = stack.enter_context(psycopg.connect(h.db_url, autocommit=True))
        while True:
            now = h.clock.now()
            row = conn.execute(_CLAIM_SQL, {"owner": WORKER, "until": now + lease, "type": task_type,
                                            "now": now}).fetchone()
            if row is None:
                return processed
            handler(ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3])))
            assert queue.complete(int(row[0]), WORKER), f"{task_type} não concluída (posse perdida)"
            processed += 1
            assert processed <= 60, f"mais de 60 tarefas {task_type}"


def _channel(h: _H, stack: contextlib.ExitStack) -> Any:
    """O canal com rota que o `run_worker` monta com bots, no modo padrão (`rich`)."""
    from cq_server.adapters.console import ConsoleChannel
    from cq_server.bots import TelegramChannel
    from cq_server.config import DeliverySettings

    channel = TelegramChannel(h.db_url, clock=h.clock, runtime=h.bots, default=ConsoleChannel(io.StringIO()),
                              delivery=DeliverySettings())
    stack.callback(channel.close)
    return channel


def _engine(h: _H) -> Any:
    from cq_server.adapters.engine import HttpQuestionEngine
    from cq_server.config import EngineSettings

    # Prazo dentro do invariante `timeout + 15 <= lease_seconds` (30 s nos testes).
    return HttpQuestionEngine(EngineSettings(engine_url=h.engine_fake.base_url, timeout_seconds=10.0))


def _validate(h: _H) -> int:
    """Roda a validação (`dispatch.validate`, com as continuações) de todos os disparos pendentes."""
    from cq_server.dispatches import VALIDATE_TASK_TYPE, validate_task_handler

    def make(stack: contextlib.ExitStack) -> Any:
        engine = _engine(h)
        stack.callback(engine.close)
        return validate_task_handler(h.db_url, clock=h.clock, settings=h.settings, engine=engine,
                                     fetcher=h.repo_fetcher, channel=_channel(h, stack))

    return _drain(h, VALIDATE_TASK_TYPE, make)


def _open(h: _H) -> int:
    """Roda as tarefas `dispatch.open` disponíveis (a do imediato sai da confirmação)."""
    from cq_server.dispatches import OPEN_TASK_TYPE, open_task_handler

    return _drain(h, OPEN_TASK_TYPE, lambda stack: open_task_handler(h.db_url, clock=h.clock, settings=h.settings))


def _close_due(h: _H) -> int:
    """A varredura real enfileira o `dispatch.close` dos abertos vencidos, e o tratador
    real fecha (sem worker, o bot não está sendo consultado: a barreira (b) do fechamento está satisfeita)."""
    from cq_server.dispatches import CLOSE_TASK_TYPE, close_task_handler
    from cq_server.scheduler import DISPATCH_RULES, sweep_once

    with psycopg.connect(h.db_url, autocommit=True) as conn:
        sweep_once(conn, now=h.clock.now(), retry_gap=float(h.settings.backoff_max_seconds), rules=DISPATCH_RULES)
    return _drain(h, CLOSE_TASK_TYPE, lambda stack: close_task_handler(
        h.db_url, clock=h.clock, settings=h.settings, channel=_channel(h, stack)))


def _wait(predicate: Callable[[], Any], message: str, timeout: float = PROCESS_TIMEOUT) -> Any:
    """Espera (real, com prazo; threads do worker) `predicate()` verdadeiro; devolve o valor."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, message
        time.sleep(0.02)


def _states(h: _H, ids: list[str]) -> list[str]:
    rows = dict(h.sql.execute("SELECT dispatch_uuid::text, state FROM dispatches WHERE dispatch_uuid = ANY(%s)",
                              ([uuid.UUID(i) for i in ids],)).fetchall())
    return [rows.get(i) for i in ids]


# --- alunos vinculados pelo fluxo real do vínculo ---------------------------------------------------


def _processed(h: _H, bot: int, update_id: int | None) -> str:
    """Espera o update gravado e processado; devolve o `outcome` da única linha dele."""
    assert update_id is not None, "o dublê descartou o update"

    def ready() -> list[tuple] | None:
        rows = h.sql.execute(
            "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
            "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (bot, update_id)).fetchall()
        return rows if rows and all(r[0] == "processed" for r in rows) else None

    rows = h.bot_api.wait_for(ready, timeout=PROCESS_TIMEOUT)
    assert len(rows) == 1, f"update gravado {len(rows)} vezes"
    return rows[0][1]


def _link_students(h: _H, offering: dict, bot: int, count: int) -> list[tuple[str, str]]:
    """Vincula `count` alunos neutros pelo fluxo real do vínculo (importação, lote de convites pelo SMTP falso,
    `/start <código>` e `/aceito` pela Bot API falsa, com o worker de bots); para o worker no fim e devolve
    `(pseudo_id, alias)` de cada um. Nenhum disparo pode estar em validação enquanto o worker roda."""
    smtp = h.request.getfixturevalue("smtp")
    mail_channel = h.request.getfixturevalue("mail_channel")
    start_worker = h.request.getfixturevalue("bot_worker_thread")
    code = offering["code"]
    students = []
    for _ in range(count):
        n = next(_counter)
        students.append({"name": f"Aluno Disparo {n}", "ra": f"664{n:07d}",
                         "email": f"aluno.disparo.{n}@turma.example.org"})
    imported = import_students(h.client, ADMIN_TOKEN, code, students)
    assert (imported["people_created"], imported["enrollments_created"]) == (count, count)
    prepared = prepare_invites(h.client, ADMIN_TOKEN, code)
    send_invites(h.client, ADMIN_TOKEN, code, prepared["confirmation_code"])
    start_worker(verification=mail_channel)
    links = h.bot_api.wait_for(lambda: (lambda found: found if len(found) == count else None)(smtp.invite_links()),
                               timeout=PROCESS_TIMEOUT)
    by_address = {link.rcpt_to[0]: link.code for link in links}
    for index, student in enumerate(students):
        sender = make_user(7600 + next(_counter))
        assert _processed(h, bot, h.bot_api.push_start(bot, sender, by_address[student["email"]])) == (
            "student_terms_presented")
        assert _processed(h, bot, h.bot_api.push_text(bot, sender, "/aceito", bot_command=True)) == "student_joined"
    _wait(lambda: h.sql.execute("SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased')"
                                ).fetchone()[0] == 0, "a fila de saída não esvaziou")
    start_worker.stop()
    people = []
    for student in students:
        row = h.sql.execute("SELECT p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                            "WHERE i.ra = %s", (student["ra"],)).fetchone()
        assert row is not None
        people.append((row[0], row[1]))
    return people


# =============================================================================================
# Preparo e consulta
# =============================================================================================


def _fmt(moment: datetime, offset_hours: int | None) -> str:
    """ISO 8601 com deslocamento: `Z` (UTC) ou `±HH:MM` (o mesmo instante em outro fuso)."""
    if offset_hours is None:
        return iso(moment)
    return moment.astimezone(timezone(timedelta(hours=offset_hours))).isoformat()


# (id, abertura em relação a agora, fechamento (agendado: em relação à abertura; imediato: a agora),
#  deslocamento dos horários do pedido, commit, repo, qid)
VALID_PREPARE = [
    pytest.param(None, None, None, COMMIT, REPO, QID, id="imediato-sem-fechamento"),
    pytest.param(None, timedelta(days=30), None, COMMIT, REPO, QID, id="imediato-fechamento-30-dias"),
    pytest.param(None, timedelta(minutes=5), 2, COMMIT, REPO, QID, id="imediato-fechamento-5-min"),
    pytest.param(timedelta(days=1), None, -3, COMMIT, REPO, QID, id="agendado-sem-fechamento-menos-3h"),
    pytest.param(timedelta(days=90), timedelta(days=30), None, COMMIT, REPO, QID, id="agendado-90-dias-janela-30"),
    pytest.param(timedelta(hours=2), timedelta(minutes=5), 5, COMMIT, REPO, QID, id="agendado-janela-5-min"),
    pytest.param(None, None, None, COMMIT_SHA256, REPO, QID, id="commit-sha256"),
    pytest.param(None, None, None, COMMIT, f"https://{HOST}/" + "r" * (2048 - len(f"https://{HOST}/")), QID,
                 id="repo-2048"),
    pytest.param(None, None, None, COMMIT, REPO, "capitulo-1/questao_2.v3", id="qid-em-pastas"),
    pytest.param(None, None, None, COMMIT, REPO, "q" * 512, id="qid-512"),
]


@pytest.mark.parametrize(("opens_in", "closes_in", "offset", "commit", "repo", "qid"), VALID_PREPARE)
def test_prepare_returns_validating_dispatch_and_one_validate_task_without_engine(
        h: _H, opens_in: timedelta | None, closes_in: timedelta | None, offset: int | None, commit: str,
        repo: str, qid: str) -> None:
    """O preparo (validando, a validação enfileirada, nada enviado, o motor nunca chamado pela
    API), os limites aceitos (abertura até 90 dias, janela de 5 min a 30 dias, o imediato
    com fechamento a 30 dias), a origem `api` sem `X-CQ-Client`, UTC com
    `Z` na saída e qualquer deslocamento na entrada:
    200 com o `Dispatch` exato (agendado: `opens_at` e `closes_at` = informado ou abertura +
    janela do oferecimento; imediato: `opens_at` nulo e `closes_at` informado ou nulo); a linha do disparo
    (`immediate`, `window_minutes` capturado só no imediato sem fechamento), a referência nova sem
    inspeção, exatamente uma tarefa `dispatch.validate` pendente para agora (`{"dispatch_row_id": n}`),
    um `dispatch.prepared` exato com o ator; nenhuma chamada ao motor falso nem à obtenção falsa, nenhuma
    mensagem na fila nem envio ao Telegram."""
    offering, _ = _offering(h, "preparo-valido")
    now = h.clock.advance(60)
    opens = None if opens_in is None else now + opens_in
    if opens is None:
        closes = None if closes_in is None else now + closes_in
    else:
        closes = opens + (WINDOW if closes_in is None else closes_in)
    body = {"repo": repo, "commit": commit, "qid": qid}
    if opens is not None:
        body["opens_at"] = _fmt(opens, offset)
    if closes_in is not None:
        body["closes_at"] = _fmt(closes, offset)
    cursor, last_task = _last_seq(h), _last_task(h)
    outbox_before = h.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0]

    prepared = _ok(_post_prepare(h, offering["code"], body), "dispatches.prepare")

    assert prepared == _expected(offering, prepared, created_at=now, commit=commit, repo=repo, qid=qid,
                                 opens_at=opens, closes_at=closes)
    dispatch_id, ref_id = prepared["dispatch_id"], prepared["question"]["question_ref_id"]
    row_id = _row_id(h, dispatch_id)
    window_minutes = 60 if opens is None and closes is None else None
    assert _dispatch_row(h, dispatch_id) == ("validating", "api", opens is None, opens, closes, window_minutes,
                                             True, None, None, None, None, None, None, None, None, None)
    assert h.sql.execute(
        "SELECT ref_uuid::text, repo, repo_sha256, commit_sha, qid, supported, title, inspected_at, created_at "
        "FROM question_refs").fetchall() == [(ref_id, repo, repo_key(repo), commit, qid, None, None, None, now)]
    assert _new_tasks(h, last_task) == [("dispatch.validate", {"dispatch_row_id": row_id},
                                         f"dispatch.validate:{row_id}", "pending", 0, now)]
    assert _events(h, cursor) == [_ev("dispatch.prepared", now, "bootstrap", offering_id=offering["offering_id"],
                                      dispatch_id=dispatch_id, question_ref_id=ref_id, origin="api")]
    assert h.engine_fake.calls() == []
    assert h.repo_fetcher.calls() == []
    assert h.sql.execute("SELECT count(*) FROM outbox_messages").fetchone()[0] == outbox_before
    assert _sends(h) == []


ORIGINS = [
    pytest.param(None, None, "api", id="sem-origem-sem-cabecalho"),
    pytest.param(None, "cli", "cli", id="cabecalho-cli"),
    pytest.param(None, "mcp", "mcp", id="cabecalho-mcp"),
    pytest.param(None, "other", "api", id="cabecalho-other"),
    pytest.param("integration", None, "integration", id="corpo-integration"),
    pytest.param("integration", "cli", "integration", id="corpo-vence-cabecalho"),
    pytest.param("mcp", "cli", "mcp", id="corpo-mcp-cabecalho-cli"),
    pytest.param("api", "mcp", "api", id="corpo-api-cabecalho-mcp"),
    pytest.param("cli", "other", "cli", id="corpo-cli"),
]


@pytest.mark.parametrize(("declared", "client_kind", "expected"), ORIGINS)
def test_prepare_origin_is_declared_by_body_or_client_header(h: _H, declared: str | None, client_kind: str | None,
                                                             expected: str) -> None:
    """A origem é declarada num conjunto fechado, com a API como padrão:
    com `origin` no corpo, vale ela; sem, vem do `X-CQ-Client` (`cli` → `cli`, `mcp` → `mcp`,
    `other` ou ausente → `api`); a mesma origem na resposta, na linha do disparo e no `dispatch.prepared`,
    e com um token criado o ator é o `token_id` dele."""
    offering, _ = _offering(h, "origem")
    secret, token = create_token(h.client, ADMIN_TOKEN, ["offerings:write", "offerings:read"], label="operador")
    now = h.clock.now()
    cursor = _last_seq(h)
    commit = _commit()

    prepared = _ok(_post_prepare(h, offering["code"], _body(commit=commit, origin=declared), token=secret,
                                 client_kind=client_kind), "dispatches.prepare")

    assert prepared == _expected(offering, prepared, created_at=now, commit=commit, origin=expected)
    assert _dispatch_row(h, prepared["dispatch_id"])[:2] == ("validating", expected)
    assert _events(h, cursor) == [_ev("dispatch.prepared", now, token["token_id"],
                                      offering_id=offering["offering_id"], dispatch_id=prepared["dispatch_id"],
                                      question_ref_id=prepared["question"]["question_ref_id"], origin=expected)]
    assert get_dispatch(h.client, secret, offering["code"], prepared["dispatch_id"]) == prepared


_DROP = object()
_REPO_PREFIX = f"https://{HOST}/"


def _invalid(t0: datetime, **fields: Any) -> dict:
    """Um pedido de preparo válido com os campos trocados (`_DROP` remove o campo)."""
    body: dict[str, Any] = {"repo": REPO, "commit": COMMIT, "qid": QID}
    for key, value in fields.items():
        if value is _DROP:
            body.pop(key, None)
        else:
            body[key] = value
    return body


_T1 = timedelta(days=1)
# (id, construtor do pedido a partir de agora, `fields` esperado, valores que não podem ecoar)
INVALID_PREPARE: list[Any] = [
    # repo
    pytest.param(lambda t: _invalid(t, repo=_DROP), [("repo", "missing")], (), id="repo-ausente"),
    pytest.param(lambda t: _invalid(t, repo=None), [("repo", "missing")], (), id="repo-nulo"),
    pytest.param(lambda t: _invalid(t, repo=20312031), [("repo", "invalid_format")], (), id="repo-numero"),
    pytest.param(lambda t: _invalid(t, repo=""), [("repo", "too_short")], (), id="repo-vazio"),
    pytest.param(lambda t: _invalid(t, repo=_REPO_PREFIX + "r" * (2049 - len(_REPO_PREFIX))),
                 [("repo", "too_long")], (_REPO_PREFIX + "r" * (2049 - len(_REPO_PREFIX)),), id="repo-2049"),
    pytest.param(lambda t: _invalid(t, repo=f"http://{HOST}/disciplina/questoes-http.git"),
                 [("repo", "invalid_format")], (f"http://{HOST}/disciplina/questoes-http.git",), id="repo-http"),
    pytest.param(lambda t: _invalid(t, repo=f"https://usuario:senha-sentinela@{HOST}/q.git"),
                 [("repo", "invalid_format")], ("senha-sentinela", "usuario:senha"), id="repo-userinfo"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/q.git?ref=ramo-sentinela"),
                 [("repo", "invalid_format")], ("ramo-sentinela",), id="repo-consulta"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/q.git?private_token=glpat-Privado-Sentinela"),
                 [("repo", "invalid_format")], ("glpat-Privado-Sentinela", "private_token"), id="repo-token-privado"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/q.git#secao-sentinela"),
                 [("repo", "invalid_format")], ("secao-sentinela",), id="repo-fragmento"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/questoes%2Esentinela.git"),
                 [("repo", "invalid_format")], ("questoes%2Esentinela",), id="repo-percentual"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/disciplina\\questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela",), id="repo-barra-invertida"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/disciplina/../questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela",), id="repo-ponto-ponto"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}/./questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela",), id="repo-ponto"),
    pytest.param(lambda t: _invalid(t, repo=f"https://{HOST}:8443/questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela",), id="repo-porta"),
    pytest.param(lambda t: _invalid(t, repo="https://192.0.2.10/questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela", "192.0.2.10"), id="repo-ipv4"),
    pytest.param(lambda t: _invalid(t, repo="https://[2001:db8::1]/questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela", "2001:db8::1"), id="repo-ipv6"),
    pytest.param(lambda t: _invalid(t, repo="https://Repos.CQ.test/questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela",), id="repo-host-maiusculo"),
    pytest.param(lambda t: _invalid(t, repo="https://repositorio/questoes-sentinela.git"),
                 [("repo", "invalid_format")], ("questoes-sentinela", "repositorio"), id="repo-host-sem-ponto"),
    pytest.param(lambda t: _invalid(t, repo=OTHER_HOST_REPO), [("repo", "invalid_value")],
                 (OTHER_HOST_REPO, "git.outra.test"), id="repo-host-fora-da-lista"),
    # commit
    pytest.param(lambda t: _invalid(t, commit=_DROP), [("commit", "missing")], (), id="commit-ausente"),
    pytest.param(lambda t: _invalid(t, commit="ab12cd34e"), [("commit", "invalid_format")], ("ab12cd34e",),
                 id="commit-abreviado"),
    pytest.param(lambda t: _invalid(t, commit="ABCDEF01" * 5), [("commit", "invalid_format")], ("ABCDEF01" * 5,),
                 id="commit-maiusculo"),
    pytest.param(lambda t: _invalid(t, commit="refs/heads/ramo-principal"), [("commit", "invalid_format")],
                 ("ramo-principal",), id="commit-branch"),
    pytest.param(lambda t: _invalid(t, commit="a" * 41), [("commit", "invalid_format")], ("a" * 41,),
                 id="commit-41"),
    # qid
    pytest.param(lambda t: _invalid(t, qid=_DROP), [("qid", "missing")], (), id="qid-ausente"),
    pytest.param(lambda t: _invalid(t, qid="q" * 513), [("qid", "too_long")], ("q" * 513,), id="qid-513"),
    pytest.param(lambda t: _invalid(t, qid="../fora-da-arvore"), [("qid", "invalid_format")],
                 ("fora-da-arvore",), id="qid-ponto-ponto"),
    pytest.param(lambda t: _invalid(t, qid="questao com espaco"), [("qid", "invalid_format")],
                 ("questao com espaco",), id="qid-espaco"),
    # opens_at
    pytest.param(lambda t: _invalid(t, opens_at="2031-01-02T10:00:00"), [("opens_at", "invalid_format")],
                 ("2031-01-02T10:00:00",), id="abertura-sem-fuso"),
    pytest.param(lambda t: _invalid(t, opens_at="2031-01-02"), [("opens_at", "invalid_format")],
                 ("2031-01-02",), id="abertura-so-data"),
    pytest.param(lambda t: _invalid(t, opens_at="amanha-cedo-sentinela"), [("opens_at", "invalid_format")],
                 ("amanha-cedo-sentinela",), id="abertura-texto"),
    pytest.param(lambda t: _invalid(t, opens_at=iso(t - timedelta(seconds=1))), [("opens_at", "out_of_range")],
                 (), id="abertura-no-passado"),
    pytest.param(lambda t: _invalid(t, opens_at=iso(t + timedelta(days=90, seconds=1))),
                 [("opens_at", "out_of_range")], (), id="abertura-alem-de-90-dias"),
    # closes_at
    pytest.param(lambda t: _invalid(t, opens_at=iso(t + _T1), closes_at="2031-01-03T10:00:00"),
                 [("closes_at", "invalid_format")], ("2031-01-03T10:00:00",), id="fechamento-sem-fuso"),
    pytest.param(lambda t: _invalid(t, opens_at=iso(t + _T1), closes_at=iso(t + _T1 - timedelta(minutes=1))),
                 [("closes_at", "out_of_range")], (), id="fechamento-antes-da-abertura"),
    pytest.param(lambda t: _invalid(t, opens_at=iso(t + _T1), closes_at=iso(t + _T1 + timedelta(seconds=299))),
                 [("closes_at", "out_of_range")], (), id="janela-menor-que-5-min"),
    pytest.param(lambda t: _invalid(t, opens_at=iso(t + _T1), closes_at=iso(t + _T1 + timedelta(days=30, seconds=1))),
                 [("closes_at", "out_of_range")], (), id="janela-maior-que-30-dias"),
    pytest.param(lambda t: _invalid(t, closes_at=iso(t + timedelta(days=60))), [("closes_at", "out_of_range")],
                 (), id="imediato-fechamento-60-dias"),
    pytest.param(lambda t: _invalid(t, closes_at=iso(t + timedelta(minutes=4))), [("closes_at", "out_of_range")],
                 (), id="imediato-fechamento-4-min"),
    pytest.param(lambda t: _invalid(t, closes_at=iso(t - timedelta(minutes=1))), [("closes_at", "out_of_range")],
                 (), id="imediato-fechamento-no-passado"),
    # origin
    pytest.param(lambda t: _invalid(t, origin="robo-sentinela"), [("origin", "invalid_value")],
                 ("robo-sentinela",), id="origem-fora-do-conjunto"),
    pytest.param(lambda t: _invalid(t, origin="API"), [("origin", "invalid_value")], (), id="origem-maiuscula"),
    # corpo (as convenções da API base)
    pytest.param(lambda t: _invalid(t, campo_extra_sentinela=1), [("body", "unknown_field")],
                 ("campo_extra_sentinela",), id="campo-extra"),
    pytest.param(lambda t: _invalid(t, question={"repo": REPO}), [("body", "unknown_field")], (),
                 id="corpo-aninhado"),
    pytest.param(lambda t: {"repo": f"http://{HOST}/varios-sentinela.git", "commit": "ab12cd34e",
                            "qid": "../varios-sentinela", "origin": "robo-sentinela", "campo_extra_sentinela": "x"},
                 [("repo", "invalid_format"), ("commit", "invalid_format"), ("qid", "invalid_format"),
                  ("origin", "invalid_value"), ("body", "unknown_field")],
                 ("varios-sentinela", "ab12cd34e", "robo-sentinela", "campo_extra_sentinela"), id="varios-na-ordem"),
    pytest.param(lambda t: [REPO, COMMIT, QID], [("body", "invalid_format")], (), id="corpo-lista"),
    pytest.param(lambda t: None, [("body", "missing")], (), id="corpo-vazio"),
]


@pytest.mark.parametrize(("make", "fields", "echo"), INVALID_PREPARE)
def test_prepare_rejects_invalid_request_without_echo_and_creates_nothing(
        h: _H, make: Callable[[datetime], Any], fields: list[tuple[str, str]], echo: tuple[str, ...]) -> None:
    """Referência mal formada: repositório fora de `https`, com userinfo, consulta
    (inclusive `?private_token=`), fragmento, `%`, `\\`, segmento `.`/`..`, porta, IP, host fora
    das minúsculas ou sem ponto, host fora da lista; commit abreviado, maiúsculo, branch; QID inválido e
    com 513), horários (sem fuso; abertura no passado e além de 90 dias; fechamento antes da
    abertura; janela < 5 min e > 30 dias; o imediato com fechamento a 60 dias, a 4 min e no passado;
    origem fora do conjunto; as convenções da API base (um motivo por campo, na ordem
    do esquema, o desconhecido no fim, corpo vazio e corpo que não é objeto): 400 exato, sem eco dos
    valores enviados; nenhum disparo, referência, tarefa, mensagem nem evento é criado."""
    offering, _ = _offering(h, "preparo-invalido")
    before = _fingerprint(h)
    request = make(h.clock.now())

    if request is None:
        resp = _post_prepare(h, offering["code"], raw=True)
    else:
        resp = _post_prepare(h, offering["code"], request)

    assert_error(resp, 400, "invalid_request", fields=fields)
    for value in echo:
        assert_no_echo(resp, value)
    assert _fingerprint(h) == before
    assert h.engine_fake.calls() == [] and h.repo_fetcher.calls() == []


def test_prepare_host_allowlist_comes_from_the_injected_repo_settings(h: _H, settings: Any, clock: Any,
                                                                      bots: Any) -> None:
    """Host na lista de hosts permitidos da configuração do administrador (a lista
    vazia recusa tudo; `invalid_value`, sem eco; o
    `create_app(…, repo_settings=…)` não lê o ambiente; os testes injetam a lista): no app da
    fixture (só `repos.cq.test`), o host `git.outra.test` → `repo invalid_value`; num app próprio com só
    `git.outra.test`, o `repos.cq.test` → `invalid_value` e o `git.outra.test` → 200 (controle positivo);
    num app com `RepoSettings()` (lista vazia), os dois → `invalid_value`. Só o preparo aceito cria o
    disparo."""
    from fastapi.testclient import TestClient

    from cq_server.api import create_app
    from cq_server.config import RepoSettings

    offering, _ = _offering(h, "lista-hosts")
    ref = offering["code"]
    other_commit = _commit()
    resp = _post_prepare(h, ref, _body(repo=OTHER_HOST_REPO, commit=other_commit))
    assert_error(resp, 400, "invalid_request", fields=[("repo", "invalid_value")])
    assert_no_echo(resp, OTHER_HOST_REPO, "git.outra.test")

    own_settings = RepoSettings(allowed_hosts=frozenset({"git.outra.test"}))
    with TestClient(create_app(settings, clock=clock, bots=bots, repo_settings=own_settings),
                    raise_server_exceptions=False) as own:
        refused = own.post(f"/v1/offerings/{ref}/dispatches", json=_body(), headers=ADMIN)
        assert_error(refused, 400, "invalid_request", fields=[("repo", "invalid_value")])
        assert_no_echo(refused, REPO, HOST)
        now = clock.now()
        accepted = _ok(own.post(f"/v1/offerings/{ref}/dispatches",
                                json=_body(repo=OTHER_HOST_REPO, commit=other_commit), headers=ADMIN),
                       "dispatches.prepare")
        assert accepted == _expected(offering, accepted, created_at=now, commit=other_commit, repo=OTHER_HOST_REPO)

    with TestClient(create_app(settings, clock=clock, bots=bots, repo_settings=RepoSettings()),
                    raise_server_exceptions=False) as empty:
        for repo in (REPO, OTHER_HOST_REPO):
            resp = empty.post(f"/v1/offerings/{ref}/dispatches", json=_body(repo=repo, commit=_commit()),
                              headers=ADMIN)
            assert_error(resp, 400, "invalid_request", fields=[("repo", "invalid_value")])
            assert_no_echo(resp, repo)

    assert h.sql.execute("SELECT dispatch_uuid::text FROM dispatches").fetchall() == [(accepted["dispatch_id"],)]


def test_prepare_refusals_follow_the_contract_order(h: _H) -> None:
    """Oferecimento ativo com bot registrado e com acesso; senão conflito, sem criar
    disparo; a ordem do contrato (401 → 400 → 403 → 404 do oferecimento → `invalid_state` →
    `bot_required`): sem token → 401; pedido inválido num oferecimento inexistente → 400 (o pedido vem
    antes); token sem `offerings:write` num inexistente → 403; inexistente por código e por identificador
    → 404; `ref` fora do formato → 400 `ref`; rascunho com e sem bot, encerrado e arquivado → 409
    `invalid_state` com o estado (o estado vem antes do bot); ativo com o bot `no_access` e sem bot (a
    linha do bot apagada, como nos testes do vínculo) → 409 `bot_required`. Nada é criado nas recusas; o controle
    positivo (um ativo com bot) aceita."""
    draft, _ = _offering(h, "preparo-rascunho", activate=False)
    draft_no_bot, _ = _offering(h, "preparo-rascunho-sem-bot", bot=False)
    closed, _ = _offering(h, "preparo-encerrado")
    _post_close(h, closed["code"])
    archived, _ = _offering(h, "preparo-arquivado", activate=False)
    _post_archive(h, archived["code"])
    no_access, no_access_bot = _offering(h, "preparo-sem-acesso")
    h.sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                  "WHERE telegram_bot_id = %s", (no_access_bot,))
    no_bot, _ = _offering(h, "preparo-sem-bot")
    h.sql.execute("DELETE FROM bots WHERE offering_id = (SELECT id FROM offerings WHERE code = %s)",
                  (no_bot["code"],))
    active, _ = _offering(h, "preparo-ativo")
    reader, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    before = _fingerprint(h)

    assert_error(_post_prepare(h, active["code"], token=None), 401, "unauthenticated")
    invalid = _post_prepare(h, "mc999-inexistente", _body(commit="ab12cd34e"))
    assert_error(invalid, 400, "invalid_request", fields=[("commit", "invalid_format")])
    assert_error(_post_prepare(h, "mc999-inexistente", token=reader), 403, "forbidden", permission="offerings:write")
    assert_error(_post_prepare(h, active["code"], token=reader), 403, "forbidden", permission="offerings:write")
    for ref in ("mc999-inexistente", MISSING_OFFERING_ID):
        assert_error(_post_prepare(h, ref), 404, "not_found")
    bad_ref = _post_prepare(h, "Oferta_Formato_Invalido")
    assert_error(bad_ref, 400, "invalid_request", fields=[("ref", "invalid_format")])
    assert_no_echo(bad_ref, "Oferta_Formato_Invalido")
    for offering, state in ((draft, "draft"), (draft_no_bot, "draft"), (closed, "closed"), (archived, "archived")):
        for ref in (offering["code"], offering["offering_id"]):
            assert_error(_post_prepare(h, ref), 409, "invalid_state", state=state)
    for offering in (no_access, no_bot):
        assert_error(_post_prepare(h, offering["code"]), 409, "bot_required")
    assert _fingerprint(h) == before

    now = h.clock.now()
    accepted = _prepare(h, active, commit=COMMIT, by_id=True)
    assert accepted == _expected(active, accepted, created_at=now, commit=COMMIT)


def test_get_returns_preview_after_validation_in_utc_without_identity(h: _H) -> None:
    """A consulta traz a prévia depois da validação (título, tipo, rótulo,
    sufixo e dica do campo, o enunciado em texto, o alvo no momento, sem gabarito), as contagens de
    entregas por estado (nenhuma identidade nem lista de pseudônimos) e UTC com `Z`:
    com dois alunos vinculados pelo fluxo real do vínculo, um agendado preparado com horários em
    `-03:00`; antes da validação a consulta é igual ao preparo (`validating`, sem prévia); depois dela,
    `scheduled`, sem confirmação, `validated_at`, os horários em UTC, `target_count` 2 (o alvo agora), a
    prévia exata só com o aviso `no_teacher_contact` do oferecimento no padrão e as 9
    contagens zeradas; o gabarito sentinela do motor falso ausente; nenhum
    `pseudo_id` nem apelido dos alunos em nenhuma resposta; o leitor com `offerings:read` consulta."""
    offering, bot = _offering(h, "consulta-previa")
    assert bot is not None
    people = _link_students(h, offering, bot, 2)
    sends_after_link = _sends(h)
    created = h.clock.advance(60)
    opens = created + timedelta(days=2)
    closes = opens + timedelta(hours=3)
    reader, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    body = {"repo": REPO, "commit": COMMIT, "qid": QID, "opens_at": _fmt(opens, -3), "closes_at": _fmt(closes, -3)}
    prepared = _ok(_post_prepare(h, offering["code"], body), "dispatches.prepare")
    expected = _expected(offering, prepared, created_at=created, commit=COMMIT, opens_at=opens, closes_at=closes)
    assert prepared == expected
    assert expected["opens_at"].endswith("Z") and expected["closes_at"].endswith("Z")
    assert get_dispatch(h.client, reader, offering["code"], prepared["dispatch_id"]) == expected
    _no_identity(prepared, people)

    validated = h.clock.advance(90)
    assert _validate(h) >= 1
    shown = get_dispatch(h.client, reader, offering["code"], prepared["dispatch_id"])
    assert shown == {**expected, "state": "scheduled", "validated_at": iso(validated), "target_count": 2,
                     "preview": _preview(h, shown, target_count=2, kinds=(NO_TEACHER_CONTACT,))}
    assert get_dispatch(h.client, ADMIN_TOKEN, offering["offering_id"], prepared["dispatch_id"]) == shown
    assert ANSWER_SENTINEL not in json.dumps(shown, ensure_ascii=False)
    _no_identity(shown, people)
    assert _sends(h) == sends_after_link, "o preparo e a validação não mandam nada aos alunos"


def test_get_refusals_follow_the_contract_order(h: _H) -> None:
    """A ordem do contrato em `dispatches.get` (404 do oferecimento → 404 do disparo,
    inexistente ou de outro oferecimento): sem token → 401; token sem `offerings:read` → 403; oferecimento
    inexistente → 404; disparo inexistente → 404; o disparo de outro oferecimento, pelo código e pelo
    identificador deste → 404 (e 200 pelo dono, o controle positivo); `dispatch_id` fora do UUID v4
    canônico → 400 `dispatch_id invalid_format`, sem eco; `ref` fora do formato → 400 `ref`."""
    mine, _ = _offering(h, "consulta-minha")
    other, _ = _offering(h, "consulta-outra")
    no_read, _ = create_token(h.client, ADMIN_TOKEN, ["people:read"], label="sem leitura")
    now = h.clock.now()
    prepared = _prepare(h, other, commit=COMMIT)
    dispatch_id = prepared["dispatch_id"]
    assert get_dispatch(h.client, ADMIN_TOKEN, other["code"], dispatch_id) == _expected(
        other, prepared, created_at=now, commit=COMMIT)

    def get(ref: str, did: str, token: str | None = ADMIN_TOKEN) -> Any:
        return h.client.get(f"/v1/offerings/{ref}/dispatches/{did}", headers=auth(token))

    assert_error(get(other["code"], dispatch_id, None), 401, "unauthenticated")
    assert_error(get(other["code"], dispatch_id, no_read), 403, "forbidden", permission="offerings:read")
    assert_error(get("mc999-inexistente", dispatch_id), 404, "not_found")
    assert_error(get(MISSING_OFFERING_ID, dispatch_id), 404, "not_found")
    assert_error(get(other["code"], MISSING_DISPATCH_ID), 404, "not_found")
    for ref in (mine["code"], mine["offering_id"]):
        assert_error(get(ref, dispatch_id), 404, "not_found")
    for bad in ("disparo-inexistente-sentinela", dispatch_id.upper(), dispatch_id.replace("-", "")):
        resp = get(other["code"], bad)
        assert_error(resp, 400, "invalid_request", fields=[("dispatch_id", "invalid_format")])
        assert_no_echo(resp, bad)
    bad_ref = get("Oferta_Formato_Invalido", dispatch_id)
    assert_error(bad_ref, 400, "invalid_request", fields=[("ref", "invalid_format")])


def test_repeated_prepare_creates_another_dispatch_with_the_same_reference(h: _H) -> None:
    """Não idempotente: repetir o pedido cria outro disparo; a referência é uma
    por `(repositório, commit, questão)`, compartilhada: dois
    preparos iguais → dois disparos com `dispatch_id` distintos e o mesmo `question_ref_id`, uma só linha
    em `question_refs`, duas tarefas `dispatch.validate` (uma por disparo) e dois `dispatch.prepared`; a
    mesma referência noutro oferecimento também reusa a linha."""
    offering, _ = _offering(h, "preparo-repetido")
    other, _ = _offering(h, "preparo-repetido-outro")
    now = h.clock.now()
    cursor, last_task = _last_seq(h), _last_task(h)

    first = _prepare(h, offering, commit=COMMIT)
    second = _prepare(h, offering, commit=COMMIT)
    third = _prepare(h, other, commit=COMMIT)

    assert len({first["dispatch_id"], second["dispatch_id"], third["dispatch_id"]}) == 3
    ref_id = first["question"]["question_ref_id"]
    assert first == _expected(offering, first, created_at=now, commit=COMMIT)
    assert second == _expected(offering, second, created_at=now, commit=COMMIT)
    assert third == _expected(other, third, created_at=now, commit=COMMIT)
    assert second["question"]["question_ref_id"] == third["question"]["question_ref_id"] == ref_id
    assert h.sql.execute("SELECT ref_uuid::text FROM question_refs").fetchall() == [(ref_id,)]
    rows = [_row_id(h, d["dispatch_id"]) for d in (first, second, third)]
    assert _new_tasks(h, last_task) == [("dispatch.validate", {"dispatch_row_id": r}, f"dispatch.validate:{r}",
                                         "pending", 0, now) for r in rows]
    assert _events(h, cursor) == [
        _ev("dispatch.prepared", now, "bootstrap", offering_id=o["offering_id"], dispatch_id=d["dispatch_id"],
            question_ref_id=ref_id, origin="api")
        for o, d in ((offering, first), (offering, second), (other, third))]


def test_two_spellings_of_the_same_repository_are_distinct_references(h: _H) -> None:
    """O mesmo repositório escrito de formas diferentes (a
    referência é usada exatamente como foi informada, depois de validada; duas grafias são repositórios
    diferentes); no banco, o `repo` é literal, com
    `repo_sha256 = repo_key(repo)`, e normalizar maiúsculas e `.git` contrariaria essa regra:
    a mesma questão (mesmo commit e QID) preparada com três grafias válidas do mesmo repositório
    (com `.git`, sem `.git` e sem `.git` com a barra final) → três disparos com `question_ref_id`
    distintos, cada um com o `repo` devolvido exatamente como informado; três linhas em `question_refs`
    (o `repo` literal e a chave de cada grafia); três `dispatch.prepared`, um por referência; na
    validação, uma obtenção nova por par `(grafia, commit)` pela obtenção falsa (as demais execuções da
    mesma grafia reusam a árvore dela), cada uma com árvore própria
    (nenhum reuso entre grafias), o motor chamado com cada grafia literal, e três prévias independentes
    (sem `repeated_question`: nenhuma referência se repete)."""
    offering, _ = _offering(h, "duas-grafias")
    commit = _commit()
    bare = REPO.removesuffix(".git")
    spellings = [REPO, bare, bare + "/"]
    assert len(set(spellings)) == 3 and len({repo_key(r) for r in spellings}) == 3
    now = h.clock.now()
    cursor, last_task = _last_seq(h), _last_task(h)

    prepared = [prepare_dispatch(h.client, ADMIN_TOKEN, offering["code"], repo=r, commit=commit, qid=QID)
                for r in spellings]

    for repo, body in zip(spellings, prepared):
        assert body == _expected(offering, body, created_at=now, commit=commit, repo=repo)
        assert body["question"]["repo"] == repo, "o repo precisa voltar exatamente como foi informado"
    ref_ids = [d["question"]["question_ref_id"] for d in prepared]
    assert len(set(ref_ids)) == 3, "duas grafias do mesmo repositório viraram a mesma referência"
    assert h.sql.execute(
        "SELECT ref_uuid::text, repo, repo_sha256, commit_sha, qid FROM question_refs WHERE commit_sha = %s "
        "ORDER BY id", (commit,)).fetchall() == [
        (ref_id, repo, repo_key(repo), commit, QID) for ref_id, repo in zip(ref_ids, spellings)]
    rows = [_row_id(h, d["dispatch_id"]) for d in prepared]
    assert _new_tasks(h, last_task) == [("dispatch.validate", {"dispatch_row_id": r}, f"dispatch.validate:{r}",
                                         "pending", 0, now) for r in rows]
    assert _events(h, cursor) == [
        _ev("dispatch.prepared", now, "bootstrap", offering_id=offering["offering_id"], dispatch_id=d["dispatch_id"],
            question_ref_id=d["question"]["question_ref_id"], origin="api") for d in prepared]
    assert h.repo_fetcher.calls(commit=commit) == [], "o preparo não obtém o repositório"

    validated = h.clock.advance(30)
    assert _validate(h) >= 3
    # Toda execução da validação começa pela obtenção, e a árvore já obtida
    # volta como `Fetched(reused=True)`, como no `test_dispatch_validation.py`; conta só as
    # obtenções novas, uma por grafia; as demais chamadas da mesma grafia são reusos da árvore dela.
    for repo in spellings:
        calls = h.repo_fetcher.calls(repo, commit)
        assert [(c.outcome, c.reused) for c in calls if not c.reused] == [("Fetched", False)], (
            f"uma obtenção nova por grafia, sem reuso da árvore de outra grafia: {calls}")
        assert all(c.outcome == "Fetched" and c.reused for c in calls[1:]), (
            f"depois da obtenção nova, só reusos da árvore da própria grafia: {calls}")
        assert h.repo_fetcher.tree_path(repo, commit).is_dir()
    assert len([c for c in h.repo_fetcher.calls(commit=commit) if not c.reused]) == 3
    assert {v.repo for v in h.engine_fake.issued_variants(QID) if v.commit == commit} == set(spellings), (
        "o motor precisa receber cada grafia literal")
    shown = [_scheduled(h, offering, d, validated_at=validated) for d in prepared]
    for repo, body in zip(spellings, shown):
        assert body["question"]["repo"] == repo
    assert len({s["dispatch_id"] for s in shown}) == 3
    assert h.repo_fetcher.protocol_errors() == []


# =============================================================================================
# Confirmação
# =============================================================================================


def _scheduled(h: _H, offering: dict, prepared: dict, *, validated_at: datetime,
               kinds: tuple[str, ...] = ("empty_target", NO_TEACHER_CONTACT), target_count: int = 0) -> dict:
    """O `Dispatch` validado (agendado sem confirmação), lido pela consulta e conferido por inteiro: o do
    preparo (já conferido por `_prepare`) com o estado, `validated_at`, o alvo agora e a prévia."""
    shown = get_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"])
    expected = {**prepared, "state": "scheduled", "validated_at": iso(validated_at), "target_count": target_count}
    assert shown == {**expected, "preview": _preview(h, shown, target_count=target_count, kinds=kinds)}
    return shown


def test_confirm_scheduled_marks_confirmation_and_keeps_it_scheduled(h: _H) -> None:
    """O agendado confirmado continua agendado, com a marca; nada é enviado antes do horário;
    a resposta traz o alvo naquele momento:
    200 com o `Dispatch` exato (`confirmed = true`, `confirmed_at` = agora, `state = scheduled`, os
    horários e a prévia sem mudança, `target_count` 0 sem alunos); a consulta devolve o mesmo; um
    `dispatch.confirmed` exato com o ator (o `token_id` do token criado) e os horários; nenhuma tarefa
    nova (a abertura é da varredura, no horário), nenhuma mensagem, nenhuma chamada nova ao motor."""
    offering, _ = _offering(h, "confirma-agendado")
    secret, token = create_token(h.client, ADMIN_TOKEN, ["offerings:write", "offerings:read"], label="operador")
    opens = h.clock.now() + timedelta(days=1)
    commit = _commit()
    prepared = _prepare(h, offering, commit=commit, opens_at=opens)
    validated = h.clock.advance(30)
    _validate(h)
    before = _scheduled(h, offering, prepared, validated_at=validated)
    confirmed_at = h.clock.advance(600)
    cursor, last_task = _last_seq(h), _last_task(h)
    engine_calls = len(h.engine_fake.calls())

    body = confirm_dispatch(h.client, secret, offering["code"], prepared["dispatch_id"])

    assert body == {**before, "confirmed": True, "confirmed_at": iso(confirmed_at)}
    assert get_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"]) == body
    assert _events(h, cursor) == [_ev("dispatch.confirmed", confirmed_at, token["token_id"],
                                      offering_id=offering["offering_id"], dispatch_id=prepared["dispatch_id"],
                                      target_count=0, opens_at=iso(opens), closes_at=iso(opens + WINDOW))]
    assert _new_tasks(h, last_task) == []
    assert _dispatch_row(h, prepared["dispatch_id"])[0] == "scheduled"
    assert len(h.engine_fake.calls()) == engine_calls
    assert _sends(h) == []


@pytest.mark.parametrize("closes_in", [pytest.param(None, id="fechamento-pela-janela"),
                                       pytest.param(timedelta(days=2), id="fechamento-informado")])
def test_confirm_immediate_opens_now_with_the_effective_window(h: _H, closes_in: timedelta | None) -> None:
    """O imediato abre na confirmação; no imediato, a abertura é o momento da
    confirmação e, sem fechamento informado, o fechamento é a abertura mais a janela do oferecimento:
    com o relógio adiante da validação, 200 com `opens_at` =
    agora e `closes_at` = agora + 60 min (ou o informado no preparo), `confirmed = true`, ainda
    `scheduled`; a tarefa `dispatch.open` (`{"dispatch_row_id": n}`, chave `dispatch.open:<n>`) pendente
    para agora; o `dispatch.confirmed` exato com o ator e os horários efetivos."""
    offering, _ = _offering(h, "confirma-imediato")
    prepared_at = h.clock.now()
    closes = None if closes_in is None else prepared_at + closes_in
    commit = _commit()
    prepared = _prepare(h, offering, commit=commit, closes_at=closes)
    validated = h.clock.advance(45)
    _validate(h)
    before = _scheduled(h, offering, prepared, validated_at=validated)
    now = h.clock.advance(3600)
    cursor, last_task = _last_seq(h), _last_task(h)
    effective_closes = now + WINDOW if closes is None else closes

    body = confirm_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"])

    assert body == {**before, "confirmed": True, "confirmed_at": iso(now), "opens_at": iso(now),
                    "closes_at": iso(effective_closes)}
    assert get_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"]) == body
    row_id = _row_id(h, prepared["dispatch_id"])
    assert _new_tasks(h, last_task) == [("dispatch.open", {"dispatch_row_id": row_id}, f"dispatch.open:{row_id}",
                                         "pending", 0, now)]
    assert _events(h, cursor) == [_ev("dispatch.confirmed", now, "bootstrap", offering_id=offering["offering_id"],
                                      dispatch_id=prepared["dispatch_id"], target_count=0, opens_at=iso(now),
                                      closes_at=iso(effective_closes))]


def test_confirm_reports_the_current_target_even_if_it_changed_since_the_preview(h: _H) -> None:
    """A resposta traz o número de alunos-alvo naquele momento, sem invalidar a confirmação
    se ele mudou desde a prévia; o alvo é fixado na abertura: validado sem
    aluno (prévia com `target_count` 0 e os avisos `empty_target` e `no_teacher_contact`), um aluno vinculado depois pelo fluxo do
    vínculo; a confirmação é aceita, com `target_count` 1 na resposta e no `dispatch.confirmed`, e a prévia
    continua a da validação (0); nenhuma identidade na resposta."""
    offering, bot = _offering(h, "confirma-alvo")
    assert bot is not None
    opens = h.clock.now() + timedelta(days=3)
    commit = _commit()
    prepared = _prepare(h, offering, commit=commit, opens_at=opens)
    validated = h.clock.now()
    _validate(h)
    before = _scheduled(h, offering, prepared, validated_at=validated)
    people = _link_students(h, offering, bot, 1)
    now = h.clock.advance(120)
    cursor = _last_seq(h)

    body = confirm_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"])

    assert body == {**before, "confirmed": True, "confirmed_at": iso(now), "target_count": 1}
    assert body["preview"] == before["preview"] and body["preview"]["target_count"] == 0
    assert _events(h, cursor) == [_ev("dispatch.confirmed", now, "bootstrap", offering_id=offering["offering_id"],
                                      dispatch_id=prepared["dispatch_id"], target_count=1, opens_at=iso(opens),
                                      closes_at=iso(opens + WINDOW))]
    _no_identity(body, people)


def _post_confirm(h: _H, ref: str, dispatch_id: str, token: str | None = ADMIN_TOKEN) -> Any:
    return h.client.post(f"/v1/offerings/{ref}/dispatches/{dispatch_id}/confirm", headers=auth(token))


def test_confirm_refusals_follow_the_contract_order(h: _H) -> None:
    """A ordem do contrato em `dispatches.confirm` (404 → 404 → `invalid_state` →
    `bot_required` → `invalid_dispatch_state` → `window_expired`): sem token → 401; token só de leitura →
    403 `offerings:write`; oferecimento inexistente → 404; disparo inexistente e de outro oferecimento →
    404; `dispatch_id` fora do formato → 400 sem eco; oferecimento encerrado ou arquivado depois da
    validação → `invalid_state` com o estado (antes de olhar o disparo); o bot `no_access` com um disparo
    ainda em validação → `bot_required` (antes do `invalid_dispatch_state`). Nada muda nas recusas, e o
    disparo validado do oferecimento com acesso confirma (controle positivo)."""
    active, _ = _offering(h, "confirma-ordem")
    other, _ = _offering(h, "confirma-outro")
    closing, _ = _offering(h, "confirma-encerra")
    archiving, _ = _offering(h, "confirma-arquiva")
    no_access, no_access_bot = _offering(h, "confirma-sem-acesso")
    reader, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    opens = h.clock.now() + timedelta(days=1)
    mine = _prepare(h, active, opens_at=opens)
    foreign = _prepare(h, other, opens_at=opens)
    at_closed = _prepare(h, closing, opens_at=opens)
    at_archived = _prepare(h, archiving, opens_at=opens)
    _validate(h)
    _post_close(h, closing["code"])
    _post_close(h, archiving["code"])
    _post_archive(h, archiving["code"])
    blocked = _prepare(h, no_access, opens_at=opens)  # ainda em validação
    h.sql.execute("UPDATE bots SET state = 'no_access', access_problem = 'token_rejected' "
                  "WHERE telegram_bot_id = %s", (no_access_bot,))
    before = _fingerprint(h)

    assert_error(_post_confirm(h, active["code"], mine["dispatch_id"], None), 401, "unauthenticated")
    assert_error(_post_confirm(h, active["code"], mine["dispatch_id"], reader), 403, "forbidden",
                 permission="offerings:write")
    for ref in ("mc999-inexistente", MISSING_OFFERING_ID):
        assert_error(_post_confirm(h, ref, mine["dispatch_id"]), 404, "not_found")
    assert_error(_post_confirm(h, active["code"], MISSING_DISPATCH_ID), 404, "not_found")
    assert_error(_post_confirm(h, active["code"], foreign["dispatch_id"]), 404, "not_found")
    for bad in ("disparo-inexistente-sentinela", mine["dispatch_id"].upper()):
        resp = _post_confirm(h, active["code"], bad)
        assert_error(resp, 400, "invalid_request", fields=[("dispatch_id", "invalid_format")])
        assert_no_echo(resp, bad)
    assert_error(_post_confirm(h, closing["code"], at_closed["dispatch_id"]), 409, "invalid_state", state="closed")
    assert_error(_post_confirm(h, archiving["offering_id"], at_archived["dispatch_id"]), 409, "invalid_state",
                 state="archived")
    assert_error(_post_confirm(h, no_access["code"], blocked["dispatch_id"]), 409, "bot_required")
    assert _fingerprint(h) == before

    now = h.clock.now()
    confirmed = confirm_dispatch(h.client, ADMIN_TOKEN, active["code"], mine["dispatch_id"])
    assert (confirmed["state"], confirmed["confirmed"], confirmed["confirmed_at"]) == ("scheduled", True, iso(now))


def test_confirm_refuses_every_dispatch_that_is_not_awaiting_confirmation(h: _H) -> None:
    """Validando, rejeitado, cancelado, agendado já confirmado ou já aberto → conflito de
    estado, e nada muda; uma segunda confirmação é conflito (`invalid_dispatch_state`,
    sem `fields`, `state` nem `permission`): um disparo em `validating`; um `rejected`
    (`commit_not_found`, pela obtenção falsa); um `cancelled/validation_overran` (a validação de um
    agendado que começa depois da abertura); um agendado já confirmado; um imediato confirmado e aberto
    pelo tratador `dispatch.open` → os cinco dão 409 `invalid_dispatch_state` e nada muda (linhas,
    tarefas, eventos)."""
    offering, _ = _offering(h, "confirma-estados")
    ref = offering["code"]
    t0 = h.clock.now()
    rejected_commit = _commit()
    h.repo_fetcher.program(REPO, rejected_commit, FetchNotFound("commit"))
    rejected = _prepare(h, offering, commit=rejected_commit)
    overran = _prepare(h, offering, opens_at=t0 + timedelta(minutes=10))
    confirmed = _prepare(h, offering, opens_at=t0 + timedelta(days=2))
    opened = _prepare(h, offering)
    validated = h.clock.set(t0 + timedelta(minutes=10))
    _validate(h)
    validating = _prepare(h, offering)
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, confirmed["dispatch_id"])
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, opened["dispatch_id"])
    assert _open(h) == 1

    shown = {name: get_dispatch(h.client, ADMIN_TOKEN, ref, d["dispatch_id"]) for name, d in (
        ("validating", validating), ("rejected", rejected), ("overran", overran), ("confirmed", confirmed),
        ("opened", opened))}
    assert (shown["validating"]["state"], shown["validating"]["validated_at"]) == ("validating", None)
    assert (shown["rejected"]["state"], shown["rejected"]["rejection"], shown["rejected"]["rejected_at"],
            shown["rejected"]["preview"]) == ("rejected", COMMIT_NOT_FOUND, iso(validated), None)
    assert (shown["overran"]["state"], shown["overran"]["cancel_reason"], shown["overran"]["cancelled_at"],
            shown["overran"]["validated_at"]) == ("cancelled", "validation_overran", iso(validated), None)
    assert (shown["confirmed"]["state"], shown["confirmed"]["confirmed"]) == ("scheduled", True)
    assert (shown["opened"]["state"], shown["opened"]["opened_at"], shown["opened"]["target_count"]) == (
        "open", iso(validated), 0)
    h.clock.advance(60)
    before = _fingerprint(h)

    for name, dispatch in shown.items():
        assert_error(_post_confirm(h, ref, dispatch["dispatch_id"]), 409, "invalid_dispatch_state")
        assert get_dispatch(h.client, ADMIN_TOKEN, ref, dispatch["dispatch_id"]) == dispatch, name
    assert _fingerprint(h) == before


def test_confirm_deadline_and_effective_window_with_boundaries(h: _H) -> None:
    """Agendado sem confirmação com a abertura já passada; janela efetiva abaixo de 5 min ou
    com o fechamento no passado → janela expirada; prazo de confirmação: a abertura no
    agendado; 24 h da validação no imediato; `invalid_dispatch_state` antes de
    `window_expired`, com os limites: todos validados em `t0`; em `abertura − 1 s` o
    agendado confirma e em `abertura` o outro recusa (`invalid_dispatch_state`); o imediato com fechamento
    em `t0 + 2 h` confirma com exatamente 5 min restantes, e recusa com 4 min 59 s e com o fechamento no
    passado (`window_expired`); o imediato sem fechamento confirma em `t0 + 24 h − 1 s` (abertura agora,
    fechamento agora + 60 min) e recusa em `t0 + 24 h`; com o prazo vencido **e** o fechamento no passado,
    vale o `invalid_dispatch_state`. Nada muda em nenhuma recusa."""
    offering, _ = _offering(h, "confirma-prazos")
    ref = offering["code"]
    t0 = h.clock.now()
    opens = t0 + timedelta(hours=2)
    scheduled_ok = _prepare(h, offering, opens_at=opens)
    scheduled_late = _prepare(h, offering, opens_at=opens)
    window_ok = _prepare(h, offering, closes_at=opens)
    window_short = _prepare(h, offering, closes_at=opens)
    window_past = _prepare(h, offering, closes_at=opens)
    both_late = _prepare(h, offering, closes_at=opens)
    immediate_ok = _prepare(h, offering)
    immediate_late = _prepare(h, offering)
    _validate(h)
    for dispatch in (scheduled_ok, scheduled_late, window_ok, window_short, window_past, both_late, immediate_ok,
                     immediate_late):
        _scheduled(h, offering, dispatch, validated_at=t0)

    def refused(dispatch: dict, code: str) -> None:
        before = _fingerprint(h)
        assert_error(_post_confirm(h, ref, dispatch["dispatch_id"]), 409, code)
        assert _fingerprint(h) == before

    def accepted(dispatch: dict, opens_at: datetime, closes_at: datetime) -> None:
        now = h.clock.now()
        before = get_dispatch(h.client, ADMIN_TOKEN, ref, dispatch["dispatch_id"])
        body = confirm_dispatch(h.client, ADMIN_TOKEN, ref, dispatch["dispatch_id"])
        assert body == {**before, "confirmed": True, "confirmed_at": iso(now), "opens_at": iso(opens_at),
                        "closes_at": iso(closes_at)}

    h.clock.set(opens - timedelta(minutes=5))
    accepted(window_ok, h.clock.now(), opens)
    h.clock.set(opens - timedelta(minutes=5) + timedelta(seconds=1))
    refused(window_short, "window_expired")
    h.clock.set(opens - timedelta(seconds=1))
    accepted(scheduled_ok, opens, opens + WINDOW)
    h.clock.set(opens)
    refused(scheduled_late, "invalid_dispatch_state")
    h.clock.set(opens + timedelta(seconds=1))
    refused(window_past, "window_expired")
    h.clock.set(t0 + timedelta(hours=24) - timedelta(seconds=1))
    accepted(immediate_ok, h.clock.now(), h.clock.now() + WINDOW)
    h.clock.set(t0 + timedelta(hours=24))
    refused(immediate_late, "invalid_dispatch_state")
    refused(both_late, "invalid_dispatch_state")


def test_second_confirmation_is_refused_and_changes_nothing(h: _H) -> None:
    """Uma segunda confirmação é conflito de estado: a primeira confirmação
    de um agendado → 200; a segunda, logo depois e com outro token de escrita → 409
    `invalid_dispatch_state`, sem novo `dispatch.confirmed`, e o `confirmed_at` continua o da primeira."""
    offering, _ = _offering(h, "confirma-duas-vezes")
    other_writer, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:write"], label="outro operador")
    prepared = _prepare(h, offering, opens_at=h.clock.now() + timedelta(days=1))
    _validate(h)
    first = confirm_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"])
    h.clock.advance(5)
    before = _fingerprint(h)

    assert_error(_post_confirm(h, offering["code"], prepared["dispatch_id"], other_writer), 409,
                 "invalid_dispatch_state")

    assert _fingerprint(h) == before
    assert get_dispatch(h.client, ADMIN_TOKEN, offering["code"], prepared["dispatch_id"]) == first


# =============================================================================================
# Cancelamento e lista
# =============================================================================================


def _post_cancel(h: _H, ref: str, dispatch_id: str, token: str | None = ADMIN_TOKEN) -> Any:
    return h.client.post(f"/v1/offerings/{ref}/dispatches/{dispatch_id}/cancel", headers=auth(token))


def _cancelled(before: dict, at: datetime) -> dict:
    """O `Dispatch` depois do cancelamento pelo operador: o estado, o
    motivo e o horário; o alvo deixa de valer (nulo); o resto, inclusive a prévia, como antes."""
    return {**before, "state": "cancelled", "cancel_reason": "operator", "cancelled_at": iso(at),
            "target_count": None}


@pytest.mark.parametrize("stage", ["validando", "agendado", "agendado-confirmado"])
def test_cancel_validating_or_scheduled_sets_operator_reason(h: _H, stage: str) -> None:
    """Cancelar um disparo validando ou agendado, confirmado ou não; o disparo fica
    cancelado, com o motivo e o evento; não destrutivo: 200 com o
    `Dispatch` exato em `cancelled` (`cancel_reason = operator`, `cancelled_at` = agora, o alvo nulo, o
    resto como antes); a consulta devolve o mesmo; um `dispatch.cancelled` exato com o ator (o `token_id`
    do token criado); nenhuma tarefa nova, nenhuma mensagem, nenhuma entrega."""
    offering, _ = _offering(h, "cancela")
    secret, token = create_token(h.client, ADMIN_TOKEN, ["offerings:write", "offerings:read"], label="operador")
    ref = offering["code"]
    opens = h.clock.now() + timedelta(days=1)
    prepared = _prepare(h, offering, opens_at=opens)
    if stage != "validando":
        _validate(h)
    if stage == "agendado-confirmado":
        confirm_dispatch(h.client, ADMIN_TOKEN, ref, prepared["dispatch_id"])
    before = get_dispatch(h.client, ADMIN_TOKEN, ref, prepared["dispatch_id"])
    assert (before["state"], before["confirmed"]) == (
        "validating" if stage == "validando" else "scheduled", stage == "agendado-confirmado")
    now = h.clock.advance(3600)
    cursor, last_task = _last_seq(h), _last_task(h)

    body = cancel_dispatch(h.client, secret, ref, prepared["dispatch_id"])

    assert body == _cancelled(before, now)
    assert get_dispatch(h.client, ADMIN_TOKEN, ref, prepared["dispatch_id"]) == body
    assert _dispatch_row(h, prepared["dispatch_id"])[0] == "cancelled"
    assert _events(h, cursor) == [_ev("dispatch.cancelled", now, token["token_id"],
                                      offering_id=offering["offering_id"], dispatch_id=prepared["dispatch_id"],
                                      cancel_reason="operator")]
    assert _new_tasks(h, last_task) == []
    assert h.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
    assert _sends(h) == []


def test_cancelled_dispatch_sends_nothing_even_if_validation_or_opening_come_later(h: _H,
                                                                                   bot_worker_thread: Any) -> None:
    """Nenhuma mensagem é enviada por ele, mesmo que a validação termine depois
    (uma validação que termina depois do cancelamento não muda o estado; nada é enviado); o caso
    do cancelamento um instante antes da abertura agendada; todo passo começa conferindo
    `validating`, e a varredura só abre o agendado confirmado: com o worker real (motor e obtenção
    falsos), (1) um disparo cancelado ainda em validação: a tarefa `dispatch.validate` dele termina
    `done` sem efeito, sem obtenção nem chamada ao motor para a referência dele, e ele continua igual,
    enquanto o irmão preparado junto chega a `scheduled` (controle positivo); (2) um agendado confirmado e
    cancelado 1 s antes da abertura: passado o horário, o irmão confirmado abre (controle positivo) e ele
    continua cancelado, sem tarefa `dispatch.open`, sem entrega, sem `dispatch.opened`; nenhum envio ao
    Telegram."""
    from cq_server.config import DeliverySettings

    offering, _ = _offering(h, "cancela-depois")
    ref = offering["code"]
    opens = h.clock.now() + timedelta(hours=1)
    lonely_commit = _commit()
    early = _prepare(h, offering, commit=lonely_commit, opens_at=opens)
    sibling = _prepare(h, offering, opens_at=opens)
    late = _prepare(h, offering, opens_at=opens)
    control = _prepare(h, offering, opens_at=opens)
    early_cancelled = cancel_dispatch(h.client, ADMIN_TOKEN, ref, early["dispatch_id"])
    early_row = _row_id(h, early["dispatch_id"])
    engine = _engine(h)
    try:
        bot_worker_thread(engine=engine, fetcher=h.repo_fetcher, delivery=DeliverySettings())
        ids = [sibling["dispatch_id"], late["dispatch_id"], control["dispatch_id"]]
        _wait(lambda: _states(h, ids) == ["scheduled"] * 3, "os irmãos não chegaram a scheduled")
        tasks = _wait(lambda: (lambda rows: rows if rows and all(r[0] in ("done", "failed") for r in rows) else None)(
            h.sql.execute("SELECT state FROM tasks WHERE task_type = 'dispatch.validate' AND payload = %s::jsonb",
                          (json.dumps({"dispatch_row_id": early_row}),)).fetchall()),
            "a validação do disparo cancelado não terminou")
        assert tasks == [("done",)]
        assert get_dispatch(h.client, ADMIN_TOKEN, ref, early["dispatch_id"]) == early_cancelled
        assert h.repo_fetcher.count(REPO, lonely_commit) == 0
        assert [c for c in h.engine_fake.calls() if isinstance(c.request, dict)
                and c.request.get("question_ref", {}).get("commit") == lonely_commit] == []

        confirm_dispatch(h.client, ADMIN_TOKEN, ref, late["dispatch_id"])
        confirm_dispatch(h.client, ADMIN_TOKEN, ref, control["dispatch_id"])
        h.clock.set(opens - timedelta(seconds=1))
        cursor = _last_seq(h)
        late_cancelled = cancel_dispatch(h.client, ADMIN_TOKEN, ref, late["dispatch_id"])
        assert late_cancelled["state"] == "cancelled"
        h.clock.set(opens + timedelta(seconds=1))
        _wait(lambda: _states(h, [control["dispatch_id"]]) == ["open"], "o controle não abriu no horário")
        assert get_dispatch(h.client, ADMIN_TOKEN, ref, late["dispatch_id"]) == late_cancelled
        late_row = _row_id(h, late["dispatch_id"])
        assert h.sql.execute("SELECT count(*) FROM tasks WHERE task_type = 'dispatch.open' AND payload = %s::jsonb",
                             (json.dumps({"dispatch_row_id": late_row}),)).fetchone()[0] == 0
        events = [(e["type"], e["data"]["dispatch_id"]) for e in _events(h, cursor)]
        assert ("dispatch.cancelled", late["dispatch_id"]) in events
        assert ("dispatch.opened", control["dispatch_id"]) in events
        assert [e for e in events if e[1] in (early["dispatch_id"], late["dispatch_id"])] == [
            ("dispatch.cancelled", late["dispatch_id"])]
    finally:
        bot_worker_thread.stop()
        engine.close()
    assert h.sql.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
    assert _sends(h) == []


def test_cancel_refuses_open_closed_rejected_and_cancelled(h: _H) -> None:
    """Aberto, fechado, rejeitado ou cancelado → conflito de estado; a ordem do contrato
    (`dispatches.cancel`: 404 → 404 → `invalid_dispatch_state`, sem `fields`, `state` nem `permission`):
    um `rejected` (`commit_not_found`), um cancelado pelo operador, um imediato aberto pelo tratador
    `dispatch.open` e um fechado (a varredura e o tratador `dispatch.close` depois de `closes_at`) → 409
    `invalid_dispatch_state` e nada muda; antes das regras, sem token → 401, só leitura → 403, oferecimento
    inexistente → 404, disparo inexistente ou de outro oferecimento → 404, `dispatch_id` fora do formato
    → 400 sem eco."""
    offering, _ = _offering(h, "cancela-recusas")
    other, _ = _offering(h, "cancela-outro")
    ref = offering["code"]
    reader, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    t0 = h.clock.now()
    rejected_commit = _commit()
    h.repo_fetcher.program(REPO, rejected_commit, FetchNotFound("commit"))
    rejected = _prepare(h, offering, commit=rejected_commit)
    cancelled = _prepare(h, offering)
    opened = _prepare(h, offering)
    closed = _prepare(h, offering, closes_at=t0 + timedelta(minutes=30))
    foreign = _prepare(h, other)
    _validate(h)
    cancel_dispatch(h.client, ADMIN_TOKEN, ref, cancelled["dispatch_id"])
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, opened["dispatch_id"])
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, closed["dispatch_id"])
    assert _open(h) == 2
    h.clock.set(t0 + timedelta(minutes=30, seconds=1))
    assert _close_due(h) == 1
    shown = {d["dispatch_id"]: get_dispatch(h.client, ADMIN_TOKEN, ref, d["dispatch_id"])
             for d in (rejected, cancelled, opened, closed)}
    assert [shown[d["dispatch_id"]]["state"] for d in (rejected, cancelled, opened, closed)] == [
        "rejected", "cancelled", "open", "closed"]
    assert shown[closed["dispatch_id"]]["closed_at"] == iso(h.clock.now())
    before = _fingerprint(h)

    target = opened["dispatch_id"]
    assert_error(_post_cancel(h, ref, target, None), 401, "unauthenticated")
    assert_error(_post_cancel(h, ref, target, reader), 403, "forbidden", permission="offerings:write")
    for missing_ref in ("mc999-inexistente", MISSING_OFFERING_ID):
        assert_error(_post_cancel(h, missing_ref, target), 404, "not_found")
    assert_error(_post_cancel(h, ref, MISSING_DISPATCH_ID), 404, "not_found")
    assert_error(_post_cancel(h, ref, foreign["dispatch_id"]), 404, "not_found")
    resp = _post_cancel(h, ref, "disparo-inexistente-sentinela")
    assert_error(resp, 400, "invalid_request", fields=[("dispatch_id", "invalid_format")])
    assert_no_echo(resp, "disparo-inexistente-sentinela")
    for dispatch_id, dispatch in shown.items():
        assert_error(_post_cancel(h, ref, dispatch_id), 409, "invalid_dispatch_state")
        assert get_dispatch(h.client, ADMIN_TOKEN, ref, dispatch_id) == dispatch
    assert _fingerprint(h) == before
    assert get_dispatch(h.client, ADMIN_TOKEN, other["code"], foreign["dispatch_id"])["state"] == "scheduled"


def test_list_returns_exact_summaries_paginated_by_id_with_counts_only(h: _H) -> None:
    """Listar disparos em vários estados: estado, horários em UTC, motivo da rejeição, só
    contagens de entregas, sem identidade nem pseudônimos (a prévia só na consulta); paginação
    `after`/`limit` por `id`, com `next_cursor`: com seis disparos em
    `validating`, `rejected`, `scheduled`, agendado confirmado, `cancelled` e `open`, a lista devolve
    `{offering, timezone, items, next_cursor}` com os seis `DispatchSummary` exatos (a consulta de cada
    um sem `offering`, `timezone` e `preview`), em ordem de criação; encadear `limit=2` percorre tudo em
    páginas de 2, 2 e 2 com cursores crescentes, e a página vazia devolve `next_cursor == after`; `limit=1`
    e `after` além do fim; os disparos de outro oferecimento ficam fora; um rascunho sem disparos lista
    vazio (o 404 é só do oferecimento inexistente, em qualquer estado)."""
    offering, _ = _offering(h, "lista")
    other, _ = _offering(h, "lista-outra")
    draft, _ = _offering(h, "lista-rascunho", activate=False)
    ref = offering["code"]
    reader, _ = create_token(h.client, ADMIN_TOKEN, ["offerings:read"], label="leitura")
    t0 = h.clock.now()
    rejected_commit = _commit()
    h.repo_fetcher.program(REPO, rejected_commit, FetchNotFound("commit"))
    created = [
        _prepare(h, offering, commit=rejected_commit),
        _prepare(h, offering, opens_at=t0 + timedelta(days=1)),
        _prepare(h, offering, opens_at=t0 + timedelta(days=2), closes_at=t0 + timedelta(days=3)),
        _prepare(h, offering, opens_at=t0 + timedelta(days=4)),
        _prepare(h, offering),
    ]
    foreign = _prepare(h, other)
    h.clock.advance(30)
    _validate(h)
    rejected, scheduled, confirmed, cancelled, opened = created
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, confirmed["dispatch_id"])
    cancel_dispatch(h.client, ADMIN_TOKEN, ref, cancelled["dispatch_id"])
    confirm_dispatch(h.client, ADMIN_TOKEN, ref, opened["dispatch_id"])
    assert _open(h) == 1
    validating = _prepare(h, offering)
    order = [rejected, scheduled, confirmed, cancelled, opened, validating]
    shown = [get_dispatch(h.client, ADMIN_TOKEN, ref, d["dispatch_id"]) for d in order]
    assert [(s["state"], s["confirmed"], s["cancel_reason"]) for s in shown] == [
        ("rejected", False, None), ("scheduled", False, None), ("scheduled", True, None),
        ("cancelled", False, "operator"), ("open", True, None), ("validating", False, None)]
    assert shown[0]["rejection"] == COMMIT_NOT_FOUND
    assert all(s["deliveries"] == ZERO_DELIVERIES for s in shown)
    summaries = [_summary(s) for s in shown]

    page = list_dispatches(h.client, reader, ref)
    assert page == {"offering": _oref(offering), "timezone": TZ, "items": summaries,
                    "next_cursor": page["next_cursor"]}
    assert page["next_cursor"] > 0
    assert list_dispatches(h.client, ADMIN_TOKEN, offering["offering_id"]) == page

    seen: list[dict] = []
    sizes: list[int] = []
    after = 0
    while True:
        chunk = list_dispatches(h.client, ADMIN_TOKEN, ref, after=after, limit=2)
        if not chunk["items"]:
            assert chunk == {"offering": _oref(offering), "timezone": TZ, "items": [], "next_cursor": after}
            break
        assert chunk["next_cursor"] > after
        seen.extend(chunk["items"])
        sizes.append(len(chunk["items"]))
        after = chunk["next_cursor"]
        assert len(sizes) <= 3, "a paginação não terminou"
    assert sizes == [2, 2, 2]
    assert seen == summaries
    assert after == page["next_cursor"]
    one = list_dispatches(h.client, ADMIN_TOKEN, ref, limit=1)
    assert one["items"] == summaries[:1] and 0 < one["next_cursor"] < page["next_cursor"]
    beyond = page["next_cursor"] + 1000
    assert list_dispatches(h.client, ADMIN_TOKEN, ref, after=beyond) == {
        "offering": _oref(offering), "timezone": TZ, "items": [], "next_cursor": beyond}

    foreign_shown = get_dispatch(h.client, ADMIN_TOKEN, other["code"], foreign["dispatch_id"])
    other_page = list_dispatches(h.client, ADMIN_TOKEN, other["code"])
    assert other_page == {"offering": _oref(other), "timezone": TZ, "items": [_summary(foreign_shown)],
                          "next_cursor": other_page["next_cursor"]}
    assert list_dispatches(h.client, ADMIN_TOKEN, draft["code"]) == {
        "offering": _oref(draft), "timezone": TZ, "items": [], "next_cursor": 0}


INVALID_LIST_QUERY = [
    pytest.param({"limit": "0"}, [("limit", "out_of_range")], (), id="limit-0"),
    pytest.param({"limit": "501"}, [("limit", "out_of_range")], (), id="limit-501"),
    pytest.param({"limit": "limite-sentinela"}, [("limit", "invalid_format")], ("limite-sentinela",),
                 id="limit-texto"),
    pytest.param({"after": "cursor-sentinela"}, [("after", "invalid_format")], ("cursor-sentinela",),
                 id="after-texto"),
    pytest.param({"after": "-1"}, [("after", "out_of_range")], (), id="after-negativo"),
    pytest.param({"parametro_sentinela": "valor_sentinela"}, [("query", "unknown_field")],
                 ("parametro_sentinela", "valor_sentinela"), id="desconhecido"),
    pytest.param({"limit": "0", "parametro_sentinela": "1"}, [("limit", "out_of_range"), ("query", "unknown_field")],
                 ("parametro_sentinela",), id="desconhecido-no-fim"),
]


@pytest.mark.parametrize(("params", "fields", "echo"), INVALID_LIST_QUERY)
def test_list_rejects_invalid_query_and_follows_the_contract_order(h: _H, params: dict,
                                                                  fields: list[tuple[str, str]],
                                                                  echo: tuple[str, ...]) -> None:
    """`after` e `limit` (padrão 100, teto 500) e a ordem em `dispatches.list` (
    404 do oferecimento, em qualquer estado): consulta inválida → 400 exato, sem eco; sem token → 401;
    token sem `offerings:read` → 403; oferecimento inexistente → 404; e a mesma consulta válida no mesmo
    oferecimento lista o disparo preparado (controle positivo)."""
    offering, _ = _offering(h, "lista-invalida")
    ref = offering["code"]
    no_read, _ = create_token(h.client, ADMIN_TOKEN, ["people:read"], label="sem leitura")
    prepared = _prepare(h, offering)

    resp = h.client.get(f"/v1/offerings/{ref}/dispatches", params=params, headers=ADMIN)
    assert_error(resp, 400, "invalid_request", fields=fields)
    for value in echo:
        assert_no_echo(resp, value)
    assert_error(h.client.get(f"/v1/offerings/{ref}/dispatches", headers=auth(None)), 401, "unauthenticated")
    assert_error(h.client.get(f"/v1/offerings/{ref}/dispatches", headers=auth(no_read)), 403, "forbidden",
                 permission="offerings:read")
    for missing_ref in ("mc999-inexistente", MISSING_OFFERING_ID):
        assert_error(h.client.get(f"/v1/offerings/{missing_ref}/dispatches", headers=ADMIN), 404, "not_found")
    page = list_dispatches(h.client, ADMIN_TOKEN, ref)
    assert [i["dispatch_id"] for i in page["items"]] == [prepared["dispatch_id"]]
