"""Semente dos cenários do expurgo.

- `seed_purge_scenario(client, sql, *, bot=True, team=True, shared=True, contact="antigo→vigente",
  free_message_before_register=True, cross_offering=True, frozen_report=True, shared_contact=False)` monta um
  oferecimento **arquivado e sem trabalho pendente**, pronto para preparar e confirmar o expurgo, e devolve um
  `PurgeScenario`: os rótulos neutros das pessoas, as identidades canário (`tests/helpers/canary.py`,
  `CANARY_PURGE_PEOPLE`, prefixos `PURGE_PREFIXES`), as linhas gravadas e as contagens esperadas (`impact`, um
  `cq_server.purge.PurgeImpact`, e `outcome`, um `PurgeOutcome`).
- `make_pending(sql, kind, scenario=None)` cria, por SQL, **uma** unidade de uma classe de trabalho pendente do
  expurgo no oferecimento do cenário (`PENDING_KINDS`: o tipo → a classe que ele conta) e devolve a
  classe; sem `scenario`, usa o último semeado neste processo.
- `pending_counts(sql, offering_row)` conta as cinco classes de trabalho pendente por SQL **próprio** do ajudante
  (sem chamar `cq_server.purge`): `outbox`, `updates`, `tasks`, `freeze`, `due`.
- `snapshot_purge_tables(sql)`: as seis tabelas do expurgo e `offerings`, linha a linha (para "nada
  mudou").

**Montagem** (ordem; o relógio é o `clock` do teste, que não anda; as fixtures vêm do `request` guardado por
`tests.helpers.calls.SEED_REQUEST`, como nas sementes do disparo e dos relatórios):
1. pelas rotas (token inicial): o oferecimento `O` (código `expurgo-alvo-<n>`; com contato inicial, o antigo ou
   o vigente, conforme `contact`), com bot próprio na Bot API falsa (`bots.register`, `bots.activate`); com
   `shared` ou `cross_offering`, o oferecimento ativo `O2` (`expurgo-outro-<n>`, bot próprio); a importação da
   lista (`students.import`) das pessoas `aluno_a` e `aluno_b` em `O` e, com `shared`, de `aluno_b` também em
   `O2` (reaproveitada pelo RA); com `cross_offering`, uma pessoa neutra (`outra`) em `O2`; com `team`, o
   cadastro (`people.register`) de `equipe` e o link de equipe (`team.link`);
2. por SQL, o que no sistema real chega pelo Telegram e pelo worker (o `/start` com o aceite do termo, as
   respostas, as mensagens livres, as entregas e a fila do bot), no molde do `World` de
   `tests/integration/test_report_compute.py`: matrículas ativas (com o termo, no aluno), o convite de equipe
   usado, endereços em vários estados (`aluno_a`: um `inactive` da conta antiga e um `active`; `aluno_b` e
   `equipe`: `active`; `citada`: `blocked`), updates processados com texto, `chat_id` e `sender_id`, mensagens
   livres e as respostas fixas com o contato (`deliverytexts.answers_only_reply`) na fila do bot, `sent`;
3. o texto livre gravado antes do cadastro: as mensagens livres que citam o nome literal de `citada` (em `O`, com
   `free_message_before_register`; e em `O2`, com `cross_offering`, junto com uma resposta de texto que a cita)
   são gravadas **antes** da importação de `citada`;
4. com `frozen_report`, um disparo `closed` de `O` com a questão de duas partes (texto e número): `aluno_a`
   responde citando `aluno_b` (o nome no texto e o RA no número), `aluno_b` responde ("azul", "6.5"), `citada`
   fica `not_delivered` (`endpoint_blocked`); as duas respondidas já reveladas; a versão `closing` congelada
   por `reports.freeze_report` (sem o aviso à equipe); o disparo de `O2` (a resposta de texto citando
   `citada`) também fica fechado e congelado;
5. o contato trocado antes do arquivamento: uma resposta fixa com o e-mail antigo antes da troca
   (`offerings.update_settings`) e outra com o vigente depois; com `"antigo→removido"`, a troca é para nulo;
6. com `shared_contact`, o oferecimento ativo `O3` (`expurgo-contato-<n>`, bot próprio) com o mesmo e-mail
   vigente e uma resposta fixa com ele na fila do bot dele;
7. pelas rotas, `O` vai a `closed` (se ativo) e a `archived`; a semente **drena** o trabalho gerado: as tarefas
   `bot.sync_profile` (da ativação e do arquivamento; `offerings.py:767-769`, `bots.py:356`) dos bots do cenário
   rodam pelo `bots.profile_task_handler` com a Bot API falsa (posse por SQL, como as sementes do disparo e dos relatórios;
   `TaskQueue.complete`); nada mais fica vivo (as linhas da fila e os updates já nascem terminados, e nenhum
   aviso à equipe é registrado);
8. antes de devolver, afirma `pending_counts(sql, O) == {outbox: 0, updates: 0, tasks: 0, freeze: 0, due: 0}`.

**Contagens esperadas** (calculadas a partir do que a semente gravou, sem chamar `cq_server.purge`):
`identities_to_delete`/`identities_kept` pela regra de retenção escrita aqui (a matrícula em outro
oferecimento com `purged_at IS NULL`; o SQL desta consulta é do ajudante); `endpoints` e `updates` pela
contabilidade da semente (todas as linhas do bot de `O` têm `chat_id`); `contact_email_set` e
`contact_email_deleted` pelo modo do contato; `outbox_redacted` pelas respostas fixas com o contato na fila do
bot de `O` (a de e-mail antigo pelo contato igual, a de vigente pela citação literal e pelo contato igual);
`free_messages_redacted` pelas mensagens livres de **todo** o banco com a menção literal (nome completo, RA ou
e-mail, como a semente os escreve) de uma identidade cadastrada: num banco novo, as duas gravadas antes do cadastro; num
banco com outra semente não expurgada, também as dela. Valem no instante da semente: o teste que muda o banco
depois (outra semente, `make_pending`) recalcula o que mudou.

**Escolhas da semente** [I]: o que dá para gravar vai pelas folhas e rotas que já existem (e pelo console ou
pela Bot API falsa); o que só o Telegram e o worker produzem (vínculo, respostas, mensagens livres, entregas e
fila) é gravado por SQL, como o `World` dos relatórios faz, para que a semente rode sem worker e sem motor e cada número
esperado seja um literal do que ela gravou; o texto livre fica antes do cadastro pela ordem das gravações.
Sem bot (`bot=False`), as partes que dependem dele (equipe, endereços, updates, disparo, mensagens livres de `O`
e fila) não existem, quaisquer que sejam os outros argumentos (`scenario.seeded` diz o que foi semeado). Nada da
semente usa código do expurgo: no esqueleto, ela passa, e os testes ficam vermelhos na preparação ou na confirmação.
"""

from __future__ import annotations

import itertools
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from tests.helpers.api import (ADMIN_TOKEN, activate_bot, assert_matches_tolerant, auth, create_offering,
                               create_team_link, register_person)
from tests.helpers.canary import (CANARY_PURGE_PEOPLE, PURGE_A, PURGE_B, PURGE_C, PURGE_CONTACT_CURRENT,
                                  PURGE_CONTACT_OLD, PURGE_D, CanaryStudent)
from tests.helpers.contract import PENDING_KEYS, assert_matches
from tests.helpers.engine import FakeQuestion, build_variant, number_field, preset, text_field
from tests.helpers.secrets import remember_secret

# Os modos do contato do professor: o antigo trocado pelo vigente antes do arquivamento (padrão), só o
# vigente, o antigo removido (sem vigente) e sem contato.
CONTACT_MODES = ("antigo→vigente", "vigente", "antigo→removido", None)

# Os rótulos neutros das pessoas do cenário (nunca o nome): as quatro canário e a neutra do outro oferecimento.
LABELS = ("aluno_a", "aluno_b", "equipe", "citada")
NEUTRAL_LABEL = "outra"

# As chaves de assunto das tarefas pendentes do expurgo (o `question.refetch` fica de fora).
TASK_SUBJECT_KEYS = ("dispatch_row_id", "delivery_row_id", "submission_row_id", "update_row_id", "notice_row_id",
                     "batch_row_id", "preview_row_id", "digest_row_id", "offering_row_id", "invite_send_id", "bot_id")

# `make_pending`: o tipo → a classe de trabalho pendente que ele conta (exatamente 1 nela, 0 nas demais).
PENDING_KINDS: dict[str, str] = {
    "outbox_pending": "outbox",
    "outbox_leased": "outbox",
    "update_pending": "updates",
    "task_leased": "tasks",
    **{f"task:{key}": "tasks" for key in TASK_SUBJECT_KEYS},
    "freeze": "freeze",
    "reveal_failed": "due",
    "reveal_without_task": "due",
    "notice_pending": "due",
}

# O trabalho que não bloqueia: a `bot.sync_profile` `failed`, sem assunto devido.
FAILED_PROFILE_KIND = "task_failed_profile"

_WORKER = "seed-expurgo"
_counter = itertools.count(1)
_update_ids = itertools.count(700_000)
_LAST: list["PurgeScenario"] = []

# A questão de duas partes do disparo de `O` (texto e número) e a de texto do disparo de `O2`.
_REPO = "https://repos.cq.test/apoio/questoes-expurgo.git"
_COMMIT = "e8b3" * 10
_TWO_PARTS = FakeQuestion("Questão de apoio do expurgo", "Responda às duas partes.",
                          (text_field("texto", label="Cor:"), number_field("numero", label="Velocidade:")))


@dataclass(frozen=True)
class ScenarioPerson:
    """Uma pessoa do cenário: o rótulo neutro, a identidade canário (ou neutra), a linha e os pseudônimos."""

    label: str
    identity: CanaryStudent
    person_row: int
    pseudo_id: str
    alias: str
    deleted: bool  # a identidade sai no expurgo de `O`, no instante da semente


@dataclass(frozen=True)
class OtherOffering:
    """Um oferecimento ativo do cenário, fora do expurgo (`O2` ou `O3`)."""

    offering: dict
    code: str
    offering_row: int
    bot_row: int
    dispatch_id: str | None = None
    dispatch_row: int | None = None
    # As linhas com a pessoa apagada citada literalmente ou com o contato igual.
    update_rows: tuple[int, ...] = ()
    free_message_rows: tuple[int, ...] = ()
    submission_rows: tuple[int, ...] = ()
    outbox_rows: tuple[int, ...] = ()


@dataclass(frozen=True)
class PurgeScenario:
    offering: dict            # o `Offering` devolvido pelo arquivamento
    code: str                 # o `{ref}` (código)
    offering_id: str
    offering_row: int
    bot_row: int | None
    people: Mapping[str, ScenarioPerson]
    impact: Any               # cq_server.purge.PurgeImpact esperado
    outcome: Any              # cq_server.purge.PurgeOutcome esperado
    contact: str | None       # o modo do contato
    contact_old: str | None   # o e-mail antigo, se houve
    contact_current: str | None  # o e-mail vigente no arquivamento, se há
    seeded: Mapping[str, bool]   # o que foi de fato semeado (`bot`, `team`, `shared`, `cross_offering`, …)
    question_row: int | None = None
    dispatch_id: str | None = None
    dispatch_row: int | None = None
    deliveries: Mapping[str, int] = field(default_factory=dict)   # rótulo → linha da entrega
    submissions: Mapping[str, int] = field(default_factory=dict)  # rótulo → linha da submissão
    # As linhas gravadas por tabela no oferecimento `O` e no bot dele (`delivery_endpoints`, `telegram_updates`,
    # `free_messages`, `outbox_messages`), em ordem de gravação.
    rows: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    # As linhas de `outbox_messages` do bot de `O` com o contato (antigo e vigente), em ordem.
    contact_outbox_rows: tuple[int, ...] = ()
    # As mensagens livres gravadas antes do cadastro, com a menção literal (de `O` e de `O2`).
    free_text_limit_message_rows: tuple[int, ...] = ()
    other: OtherOffering | None = None           # `O2`
    shared_contact: OtherOffering | None = None  # `O3`

    def person(self, label: str) -> ScenarioPerson:
        return self.people[label]


# --- fixtures e linhas ---------------------------------------------------------------------------------


def _fixture(name: str) -> Any:
    from tests.helpers.calls import SEED_REQUEST

    request = SEED_REQUEST.get()
    assert request is not None, (
        "a semente do expurgo precisa do request do teste (fixture autouse _seed_request de tests/conftest.py)")
    return request.getfixturevalue(name)


def _code(prefix: str) -> str:
    return f"{prefix}-{next(_counter)}"


def _one(sql: psycopg.Connection, query: str, params: Any = None) -> Any:
    row = sql.execute(query, params).fetchone()
    assert row is not None, "a semente não achou a linha que acabou de gravar"
    return row[0]


def _offering_row(sql: psycopg.Connection, code: str) -> int:
    return int(_one(sql, "SELECT id FROM offerings WHERE code = %s", (code,)))


def _bot_row(sql: psycopg.Connection, offering_row: int) -> int:
    return int(_one(sql, "SELECT id FROM bots WHERE offering_id = %s", (offering_row,)))


def _post(client: Any, path: str, op_id: str, body: Any = None) -> dict:
    """Uma chamada de sucesso da semente (200), com o esquema congelado tolerando só a ausência das chaves do
    `Offering` acrescentadas depois (`assert_matches_tolerant`)."""
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    resp = client.post(path, headers=auth(ADMIN_TOKEN), **kwargs)
    assert resp.status_code == 200, f"{op_id} falhou na semente do expurgo: {resp.status_code} {resp.text[:300]}"
    result = resp.json()
    assert_matches_tolerant(op_id, 200, result)
    return result


_REGISTER_ATTEMPTS = 3


def _with_bot(client: Any, sql: psycopg.Connection, bot_api: Any, prefix: str, *, bot: bool = True,
              activate: bool = True, **fields: Any) -> tuple[dict, int, int | None]:
    """O oferecimento (código único e neutro) e, com `bot`, o bot próprio registrado e ativado. Tolera, como a
    semente dos relatórios, só o 503 `unavailable` transitório do `getMe` no registro."""
    offering = create_offering(client, ADMIN_TOKEN, strict_schema=False, code=_code(prefix),
                               course="Algoritmos e Programação", term="2031s1",
                               timezone="America/Sao_Paulo", **fields)
    row = _offering_row(sql, offering["code"])
    if not bot:
        return offering, row, None
    fake_bot = bot_api.add_bot()
    token = bot_api.token_of(fake_bot)
    remember_secret(token)
    for attempt in range(1, _REGISTER_ATTEMPTS + 1):
        resp = client.put(f"/v1/offerings/{offering['code']}/bot", json={"token": token},
                          headers=auth(ADMIN_TOKEN))
        transient = (resp.status_code == 503
                     and ((resp.json() or {}).get("error") or {}).get("code") == "unavailable")
        if not (transient and attempt < _REGISTER_ATTEMPTS):
            break
    assert resp.status_code == 200, f"registro do bot falhou: {resp.status_code} {resp.text[:300]}"
    assert_matches("bots.register", 200, resp.json())
    if activate:
        activate_bot(client, ADMIN_TOKEN, offering["code"])
    return offering, row, _bot_row(sql, row)


def _import(client: Any, code: str, rows: list[dict]) -> dict:
    resp = client.post(f"/v1/offerings/{code}/students", json={"rows": rows}, headers=auth(ADMIN_TOKEN))
    assert resp.status_code == 200, f"students.import falhou na semente: {resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert_matches("students.import", 200, body)
    return body


def _person_by_ra(sql: psycopg.Connection, ra: str) -> tuple[int, str, str]:
    row = sql.execute("SELECT p.id, p.pseudo_id::text, p.alias FROM identities i JOIN people p ON p.id = i.person_id "
                      "WHERE i.ra = %s", (ra,)).fetchone()
    assert row is not None, "a semente não achou a pessoa importada"
    return int(row[0]), str(row[1]), str(row[2])


def _activate_student(sql: psycopg.Connection, person_row: int, offering_row: int, at: datetime) -> int:
    """O aceite do termo (o `/aceito` do vínculo do aluno): a matrícula `student` ativa, com a versão 1 do termo."""
    return int(_one(sql, "UPDATE enrollments SET state = 'active', activated_at = %s, terms_version = 1, "
                         "terms_accepted_at = %s WHERE person_id = %s AND offering_id = %s RETURNING id",
                    (at, at, person_row, offering_row)))


def _endpoint(sql: psycopg.Connection, person_row: int, bot_row: int, chat_id: int, state: str, at: datetime) -> int:
    return int(_one(sql, "INSERT INTO delivery_endpoints (person_id, bot_id, channel, chat_id, state, created_at, "
                         "changed_at) VALUES (%s, %s, 'telegram', %s, %s, %s, %s) RETURNING id",
                    (person_row, bot_row, chat_id, state, at, at)))


def _update(sql: psycopg.Connection, bot_row: int, *, chat_id: int, text: str | None, outcome: str,
            at: datetime, state: str = "processed") -> int:
    processed = state == "processed"
    return int(_one(sql, "INSERT INTO telegram_updates (bot_id, update_epoch, update_id, kind, chat_id, chat_type, "
                         "sender_id, text, details, received_at, state, outcome, processed_at) "
                         "VALUES (%s, 0, %s, 'message', %s, 'private', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (bot_row, next(_update_ids), chat_id, chat_id, text, Jsonb({}), at, state,
                     outcome if processed else None, at if processed else None)))


def _free_message(sql: psycopg.Connection, offering_row: int, endpoint_row: int, update_row: int, text: str,
                  at: datetime, *, replied: bool) -> int:
    return int(_one(sql, "INSERT INTO free_messages (offering_id, delivery_endpoint_id, telegram_update_id, text, "
                         "replied, ambiguous, received_at, created_at) VALUES (%s, %s, %s, %s, %s, false, %s, %s) "
                         "RETURNING id", (offering_row, endpoint_row, update_row, text, replied, at, at)))


def _fixed_reply(sql: psycopg.Connection, bot_row: int, update_row: int, email: str | None, at: datetime,
                 state: str = "sent") -> int:
    """A resposta fixa ao chat de um update (a forma (c) da fila; `deliverytexts.answers_only_reply`), com o
    contato literal; `sent` (a fila do bot terminada)."""
    from cq_server import deliverytexts

    sent = state == "sent"
    return int(_one(sql, "INSERT INTO outbox_messages (recipient_person_id, text, idempotency_key, state, attempts, "
                         "available_at, lease_owner, lease_until, created_at, sent_at, channel_ref, bot_id, "
                         "reply_to_update_id, priority) VALUES (NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                         "%s, 1) RETURNING id",
                    (deliverytexts.answers_only_reply(email), f"semente-expurgo:resposta:{uuid.uuid4().hex}", state,
                     1 if sent else 0, at, _WORKER if state == "leased" else None,
                     at + timedelta(minutes=5) if state == "leased" else None, at, at if sent else None,
                     str(next(_update_ids)) if sent else None, bot_row, update_row)))


def _question(sql: psycopg.Connection, qid: str, at: datetime, names: tuple[str, ...] = ("resposta",)) -> int:
    return int(_one(sql, "INSERT INTO question_refs (ref_uuid, repo, repo_sha256, commit_sha, qid, supported, "
                         "show_correct_answer, single_variant, partial_credit, title, answers_names, content_hash, "
                         "inspected_at, created_at) VALUES (%s, %s, %s, %s, %s, true, true, false, true, %s, %s, %s, "
                         "%s, %s) RETURNING id",
                    (uuid.uuid4(), _REPO, uuid.uuid4().hex * 2, _COMMIT, qid, "Questão de apoio do expurgo",
                     list(names), "sha256:" + "e" * 64, at, at)))


def _dispatch(sql: psycopg.Connection, offering_row: int, question_row: int, *, state: str,
              opens: datetime, closes: datetime) -> tuple[int, str]:
    """Um disparo no estado pedido, no molde do `World.dispatch` dos relatórios (`closed` ou `cancelled` por operador)."""
    dispatch_id = uuid.uuid4()
    cols: dict[str, Any] = {"dispatch_uuid": dispatch_id, "offering_id": offering_row,
                            "question_ref_id": question_row, "origin": "cli", "immediate": False,
                            "opens_at": opens, "closes_at": closes, "state": state,
                            "created_at": opens - timedelta(hours=1)}
    if state == "closed":
        cols.update(validated_at=cols["created_at"], confirm_deadline=opens, preview=Jsonb({}),
                    confirmed_at=cols["created_at"], opened_at=opens, closed_at=closes)
    elif state == "cancelled":
        cols.update(cancelled_at=cols["created_at"], cancel_reason="operator")
    else:  # pragma: no cover - só os dois estados acima
        raise ValueError(state)
    names = ", ".join(cols)
    marks = ", ".join(["%s"] * len(cols))
    row = int(_one(sql, f"INSERT INTO dispatches ({names}) VALUES ({marks}) RETURNING id", tuple(cols.values())))
    return row, str(dispatch_id)


def _answered(sql: psycopg.Connection, *, dispatch_row: int, enrollment_row: int, person_row: int, bot_row: int,
              endpoint_row: int, chat_id: int, question: FakeQuestion, raw: dict, text: str, score: float,
              part_derived: list, opens: datetime, answered: datetime, revealed: datetime) -> tuple[int, int, int]:
    """Uma entrega respondida no prazo e já revelada (a variante, a mensagem principal `sent`, a submissão final
    `graded` válida e o update de origem com o texto); devolve (entrega, submissão, update)."""
    snapshot, ir, _ = build_variant(question, "1a")
    delivery = int(_one(sql, "INSERT INTO deliveries (delivery_uuid, dispatch_id, enrollment_id, delivery_endpoint_id, "
                             "seed, button_token, state, created_at, variant_at) VALUES (%s, %s, %s, %s, %s, %s, "
                             "'pending', %s, %s) RETURNING id",
                        (uuid.uuid4(), dispatch_row, enrollment_row, endpoint_row, secrets.randbelow(2**32),
                         secrets.token_urlsafe(16), opens, opens)))
    sql.execute("INSERT INTO delivery_contents (delivery_id, snapshot, ir, media_refs, rendered, parts, selection) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (delivery, Jsonb(snapshot), Jsonb(ir), Jsonb([]), Jsonb({}), Jsonb({}), Jsonb({})))
    sent = opens + timedelta(seconds=5)
    message = int(_one(sql, "INSERT INTO outbox_messages (recipient_person_id, bot_id, delivery_endpoint_id, "
                            "delivery_id, delivery_role, operation, state, attempts, available_at, created_at, "
                            "sent_at, idempotency_key, channel_ref) VALUES (%s, %s, %s, %s, 'question', 'send', "
                            "'sent', 1, %s, %s, %s, %s, %s) RETURNING id",
                       (person_row, bot_row, endpoint_row, delivery, opens, opens, sent,
                        f"semente-expurgo:{delivery}:q", str(delivery))))
    sql.execute("UPDATE deliveries SET state = 'answered', sent_at = %s, principal_message_id = %s, answered_at = %s, "
                "revealed_at = %s WHERE id = %s", (sent, message, answered, revealed, delivery))
    update = _update(sql, bot_row, chat_id=chat_id, text=text, outcome="answer_recorded", at=answered)
    submission = int(_one(sql, "INSERT INTO submissions (submission_uuid, delivery_id, telegram_update_id, outcome, "
                               "valid, late, normalized_fields, raw_input, submitted_answers, format_errors, "
                               "correct_answers, score, part_derived, response_ms, received_at, created_at, "
                               "graded_at) VALUES (%s, %s, %s, 'graded', true, false, %s, %s, %s, %s, %s, %s, %s, "
                               "%s, %s, %s, %s) RETURNING id",
                          (uuid.uuid4(), delivery, update, [], Jsonb(raw), Jsonb(raw), Jsonb({}),
                           Jsonb(snapshot["correct_answers"]), score, Jsonb(part_derived),
                           int((answered - sent).total_seconds() * 1000), answered, answered, answered)))
    return delivery, submission, update


def _not_delivered(sql: psycopg.Connection, *, dispatch_row: int, enrollment_row: int, endpoint_row: int,
                   at: datetime) -> int:
    return int(_one(sql, "INSERT INTO deliveries (delivery_uuid, dispatch_id, enrollment_id, delivery_endpoint_id, "
                         "seed, button_token, state, undelivered_reason, created_at) VALUES (%s, %s, %s, %s, %s, %s, "
                         "'not_delivered', 'endpoint_blocked', %s) RETURNING id",
                    (uuid.uuid4(), dispatch_row, enrollment_row, endpoint_row, secrets.randbelow(2**32),
                     secrets.token_urlsafe(16), at)))


def _part(index: int, *, ratio: float | None = None, residual: float | None = None) -> dict:
    """Um item de `part_derived` como o `submission.grade` da resposta grava (`grading.py:_parts`)."""
    return {"index": index, "choices": [], "ratio": ratio, "residual": residual}


def _freeze(sql: psycopg.Connection, dispatch_row: int, now: datetime) -> None:
    """A versão `closing` pelo `reports.freeze_report` dos relatórios (na transação, com o disparo travado e as identidades
    de `load_write_identities`, a ordem de travas dos relatórios), sem o aviso à equipe."""
    from cq_server.config import redaction_secrets
    from cq_server.events import load_write_identities
    from cq_server.reports import ReportTrigger, freeze_report
    from cq_server.telegramrender import PlainFormatter

    settings = _fixture("settings")
    with sql.transaction():
        sql.execute("SELECT id FROM dispatches WHERE id = %s FOR NO KEY UPDATE", (dispatch_row,))
        frozen = freeze_report(sql, dispatch_row, trigger=ReportTrigger.CLOSING, now=now,
                               identities=load_write_identities(sql), formatter=PlainFormatter(),
                               secrets=redaction_secrets(settings))
    assert frozen is not None, "a semente do expurgo não congelou a versão closing"


# --- drenagem e pendências -----------------------------------------------------------------------------

_CLAIM_PROFILE_SQL = (
    "UPDATE tasks SET state = 'leased', lease_owner = %(owner)s, lease_until = %(until)s, attempts = attempts + 1 "
    "WHERE id = (SELECT id FROM tasks WHERE task_type = %(type)s AND state = 'pending' AND available_at <= %(now)s "
    "            AND payload->>'bot_id' = ANY(%(subjects)s) ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, task_type, payload, attempts")


def _drain_profiles(sql: psycopg.Connection, bot_rows: list[int]) -> int:
    """Roda as `bot.sync_profile` vivas dos bots dados pelo tratador dos bots com a Bot API falsa (posse por SQL,
    como as sementes do disparo e dos relatórios, sem `work_once`); devolve quantas rodaram."""
    from cq_server.bots import profile_task_handler
    from cq_server.offerings import PROFILE_TASK_TYPE
    from cq_server.tasks import ClaimedTask, TaskQueue

    if not bot_rows:
        return 0
    settings = _fixture("settings")
    clock = _fixture("clock")
    runtime = _fixture("bots")
    handler = profile_task_handler(settings.database_url, clock=clock, runtime=runtime, settings=settings)
    queue = TaskQueue(settings.database_url, clock=clock, settings=settings)
    processed = 0
    try:
        while True:
            now = clock.now()
            until = now + timedelta(seconds=settings.lease_seconds)
            row = sql.execute(_CLAIM_PROFILE_SQL, {"owner": _WORKER, "until": until, "type": PROFILE_TASK_TYPE,
                                                   "now": now, "subjects": [str(b) for b in bot_rows]}).fetchone()
            if row is None:
                return processed
            task = ClaimedTask(id=int(row[0]), task_type=row[1], payload=row[2], attempts=int(row[3]))
            try:
                handler(task)
            except Exception as exc:  # noqa: BLE001 - a semente só relata o tipo da tarefa e da exceção
                raise AssertionError(
                    f"a semente do expurgo falhou ao processar {task.task_type}: {type(exc).__name__}") from None
            assert queue.complete(task.id, _WORKER), f"a semente não concluiu {task.task_type} (posse perdida)"
            processed += 1
            assert processed <= 4 * len(bot_rows), "a semente processou bot.sync_profile demais"
    finally:
        queue.close()


def _guarded(column: str) -> str:
    """O id de assunto do `payload` como `bigint`, só quando é número (nunca um cast que falhe)."""
    return f"(CASE WHEN jsonb_typeof(t.payload->'{column}') = 'number' THEN (t.payload->>'{column}')::bigint END)"


# As tarefas vivas cujo assunto é do oferecimento, por chave do `payload`.
_TASK_SUBJECT_SQL = {
    "dispatch_row_id": "EXISTS (SELECT 1 FROM dispatches s WHERE s.id = {v} AND s.offering_id = %(o)s)",
    "delivery_row_id": ("EXISTS (SELECT 1 FROM deliveries d JOIN dispatches s ON s.id = d.dispatch_id "
                        "WHERE d.id = {v} AND s.offering_id = %(o)s)"),
    "submission_row_id": ("EXISTS (SELECT 1 FROM submissions g JOIN deliveries d ON d.id = g.delivery_id "
                          "JOIN dispatches s ON s.id = d.dispatch_id WHERE g.id = {v} AND s.offering_id = %(o)s)"),
    "update_row_id": ("EXISTS (SELECT 1 FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
                      "WHERE u.id = {v} AND b.offering_id = %(o)s)"),
    "notice_row_id": "EXISTS (SELECT 1 FROM team_notices n WHERE n.id = {v} AND n.offering_id = %(o)s)",
    "batch_row_id": "EXISTS (SELECT 1 FROM invite_batches x WHERE x.id = {v} AND x.offering_id = %(o)s)",
    "preview_row_id": ("EXISTS (SELECT 1 FROM dispatch_previews v JOIN dispatches s ON s.id = v.dispatch_id "
                       "WHERE v.id = {v} AND s.offering_id = %(o)s)"),
    "digest_row_id": "EXISTS (SELECT 1 FROM offering_digests g WHERE g.id = {v} AND g.offering_id = %(o)s)",
    "offering_row_id": "{v} = %(o)s",
    "invite_send_id": ("EXISTS (SELECT 1 FROM invite_sends x JOIN enrollments e ON e.id = x.enrollment_id "
                       "WHERE x.id = {v} AND e.offering_id = %(o)s)"),
    "bot_id": "EXISTS (SELECT 1 FROM bots b WHERE b.id = {v} AND b.offering_id = %(o)s)",
}

# A revelação devida (a condição literal da regra `delivery.reveal` de `DISPATCH_RULES`, `scheduler.py:237-240`).
_REVEAL_DUE_LITERAL = (
    "p.state = 'closed' AND s.revealed_at IS NULL AND s.state IN ('answered', 'dont_know', 'answered_late') "
    "AND NOT EXISTS (SELECT 1 FROM submissions g WHERE g.delivery_id = s.id AND g.outcome = 'grading')")


def _without_deadline(condition: str) -> str:
    """"Sem o prazo": `now = 'infinity'` no lugar do parâmetro da condição do varredor."""
    return condition.replace("%(now)s", "'infinity'::timestamptz")


def pending_counts(sql: psycopg.Connection, offering_row: int) -> dict[str, int]:
    """As cinco classes de trabalho pendente para o oferecimento `offering_row`, por SQL do ajudante (nunca pelo
    `cq_server.purge`): `outbox` e `updates` do bot dele, `tasks` vivas com assunto dele (fora o
    `question.refetch`), `freeze` pela `_FREEZE_DUE` e `due` pela revelação devida (condição literal), pelos
    avisos `pending` e pelas condições dos avisos dos relatórios que valem num arquivado, todas sem o prazo."""
    from cq_server import scheduler

    subjects = " OR ".join(f"({template.format(v=_guarded(key))})" for key, template in _TASK_SUBJECT_SQL.items())
    due_sources = (
        ("dispatches s", "s.offering_id", scheduler._NOTICE_REJECTED_DUE),
        ("dispatches s", "s.offering_id", scheduler._NOTICE_CANCELLED_DUE),
        ("dispatches s", "s.offering_id", scheduler._NOTICE_QUESTION_FAILED_DUE),
        ("offerings s", "s.id", scheduler._NOTICE_ACCESS_EPISODE_DUE),
        ("invite_batches s", "s.offering_id", scheduler._NOTICE_INVITE_BATCH_DUE),
        ("offerings s", "s.id", scheduler._NOTICE_INVITE_WINDOW_DUE),
    )
    due_parts = [
        "(SELECT count(*) FROM deliveries s JOIN dispatches p ON p.id = s.dispatch_id "
        f"WHERE p.offering_id = %(o)s AND {_REVEAL_DUE_LITERAL})",
        "(SELECT count(*) FROM team_notices s WHERE s.offering_id = %(o)s AND s.state = 'pending')",
        *(f"(SELECT count(*) FROM {source} WHERE {column} = %(o)s AND ({_without_deadline(condition)}))"
          for source, column, condition in due_sources),
    ]
    query = (
        "SELECT "
        "(SELECT count(*) FROM outbox_messages m JOIN bots b ON b.id = m.bot_id "
        " WHERE b.offering_id = %(o)s AND m.state IN ('pending', 'leased')), "
        "(SELECT count(*) FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        " WHERE b.offering_id = %(o)s AND u.state = 'pending'), "
        "(SELECT count(*) FROM tasks t WHERE t.state IN ('pending', 'leased') AND t.task_type <> 'question.refetch' "
        f" AND ({subjects})), "
        "(SELECT count(*) FROM dispatches s WHERE s.offering_id = %(o)s "
        f" AND ({_without_deadline(scheduler._FREEZE_DUE)})), "
        f"({' + '.join(due_parts)})"
    )
    row = sql.execute(query, {"o": offering_row}).fetchone()
    assert row is not None
    return dict(zip(PENDING_KEYS, (int(value) for value in row), strict=True))


# --- a semente -----------------------------------------------------------------------------------------


def _classify(sql: psycopg.Connection, offering_row: int) -> dict[int, bool]:
    """A regra de retenção, escrita aqui: pessoa matriculada em `O` com identidade → mantida (`True`) se
    tem matrícula em outro oferecimento com `purged_at IS NULL`. Sem a coluna `purged_at` (esqueleto, antes da
    migração `0006`), todo oferecimento conta como não expurgado."""
    has_purged_at = sql.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'offerings' "
        "AND column_name = 'purged_at')").fetchone()[0]
    not_purged = "o2.purged_at IS NULL" if has_purged_at else "true"
    rows = sql.execute(
        "SELECT e.person_id, EXISTS (SELECT 1 FROM enrollments e2 JOIN offerings o2 ON o2.id = e2.offering_id "
        f"WHERE e2.person_id = e.person_id AND e2.offering_id <> %(o)s AND {not_purged}) "
        "FROM enrollments e JOIN identities i ON i.person_id = e.person_id WHERE e.offering_id = %(o)s",
        {"o": offering_row}).fetchall()
    return {int(person): bool(kept) for person, kept in rows}


def _literal_mentions(sql: psycopg.Connection) -> int:
    """As mensagens livres de todo o banco com a menção literal (nome completo, RA ou e-mail, como a semente os
    escreve) de alguma identidade cadastrada: as que o passo de redação do expurgo muda."""
    known = [value for row in sql.execute("SELECT full_name, ra, email FROM identities").fetchall()
             for value in row if value]
    texts = [row[0] for row in sql.execute("SELECT text FROM free_messages").fetchall()]
    return sum(1 for text in texts if any(value in text for value in known))


def seed_purge_scenario(client: Any, sql: psycopg.Connection, *, bot: bool = True, team: bool = True,
                        shared: bool = True, contact: str | None = "antigo→vigente",
                        free_message_before_register: bool = True, cross_offering: bool = True,
                        frozen_report: bool = True, shared_contact: bool = False) -> PurgeScenario:
    """Monta o cenário do expurgo (ver o comentário do módulo) e devolve o `PurgeScenario`."""
    from cq_server.purge import PurgeImpact, PurgeOutcome

    if contact not in CONTACT_MODES:
        raise ValueError(f"contact deve ser um de {CONTACT_MODES}")
    if shared_contact and contact not in ("antigo→vigente", "vigente"):
        raise ValueError("shared_contact exige um contato vigente")
    clock = _fixture("clock")
    bot_api = _fixture("bot_api")
    now = clock.now()
    early = now - timedelta(hours=3)
    opens, closes = now - timedelta(hours=2), now - timedelta(hours=1)
    answered_at, revealed_at = opens + timedelta(minutes=30), closes + timedelta(minutes=1)
    old = PURGE_CONTACT_OLD if contact in ("antigo→vigente", "antigo→removido") else None
    current = PURGE_CONTACT_CURRENT if contact in ("antigo→vigente", "vigente") else None
    with_team = team and bot
    with_dispatch = frozen_report and bot
    with_p50_here = free_message_before_register and bot
    rows: dict[str, list[int]] = {"delivery_endpoints": [], "telegram_updates": [], "free_messages": [],
                                  "outbox_messages": []}

    # 1. Os oferecimentos e a lista.
    initial = old if old is not None else current
    create_fields = {} if initial is None else {"contact_email": initial}
    offering, o_row, o_bot = _with_bot(client, sql, bot_api, "expurgo-alvo", bot=bot, **create_fields)
    code = offering["code"]
    other_needed = shared or cross_offering
    other = other_row = other_bot = None
    if other_needed:
        other, other_row, other_bot = _with_bot(client, sql, bot_api, "expurgo-outro")
    _import(client, code, [PURGE_A.row(), PURGE_B.row()])
    a_row, a_pseudo, a_alias = _person_by_ra(sql, PURGE_A.ra)
    b_row, b_pseudo, b_alias = _person_by_ra(sql, PURGE_B.ra)
    neutral: CanaryStudent | None = None
    if other is not None:
        other_rows = [PURGE_B.row()] if shared else []
        if cross_offering:
            n = next(_counter)
            neutral = CanaryStudent("Pessoa Neutra Outra Turma", f"44{n:08d}", f"pessoa.neutra.{n}@turma.example.org",
                                    ())
            other_rows.append(neutral.row())
        if other_rows:
            _import(client, other["code"], other_rows)

    # 2. O vínculo pelo Telegram (por SQL): matrículas ativas e endereços.
    endpoints: dict[str, int] = {}
    enrollments: dict[str, int] = {}
    if bot:
        enrollments["aluno_a"] = _activate_student(sql, a_row, o_row, early)
        enrollments["aluno_b"] = _activate_student(sql, b_row, o_row, early)
        old_a = _endpoint(sql, a_row, o_bot, PURGE_A.accounts[1].user_id, "inactive", early)
        endpoints["aluno_a"] = _endpoint(sql, a_row, o_bot, PURGE_A.accounts[0].user_id, "active", early)
        endpoints["aluno_b"] = _endpoint(sql, b_row, o_bot, PURGE_B.accounts[0].user_id, "active", early)
        rows["delivery_endpoints"] += [old_a, endpoints["aluno_a"], endpoints["aluno_b"]]
    other_endpoints: dict[str, int] = {}
    other_enrollments: dict[str, int] = {}
    neutral_chat = 9_200_000_000 + next(_counter)
    if other is not None:
        if shared:
            other_enrollments["aluno_b"] = _activate_student(sql, b_row, other_row, early)
            other_endpoints["aluno_b"] = _endpoint(sql, b_row, other_bot, PURGE_B.accounts[0].user_id, "active",
                                                   early)
        if neutral is not None:
            e_row, _, _ = _person_by_ra(sql, neutral.ra)
            other_enrollments[NEUTRAL_LABEL] = _activate_student(sql, e_row, other_row, early)
            other_endpoints[NEUTRAL_LABEL] = _endpoint(sql, e_row, other_bot, neutral_chat, "active", early)
    people_rows: dict[str, tuple[CanaryStudent, int, str, str]] = {
        "aluno_a": (PURGE_A, a_row, a_pseudo, a_alias), "aluno_b": (PURGE_B, b_row, b_pseudo, b_alias)}
    if with_team:
        # Uma segunda semente no mesmo banco reaproveita a pessoa da equipe ainda cadastrada (o cadastro repetido
        # daria 409 `conflict`); depois de um expurgo que a apagou, o cadastro cria outra pessoa.
        if sql.execute("SELECT 1 FROM identities WHERE ra = %s", (PURGE_C.ra,)).fetchone() is None:
            register_person(client, ADMIN_TOKEN, name=PURGE_C.name, ra=PURGE_C.ra, email=PURGE_C.email)
        c_row, c_pseudo, c_alias = _person_by_ra(sql, PURGE_C.ra)
        create_team_link(client, ADMIN_TOKEN, code, c_alias)
        enrollments["equipe"] = int(_one(sql, "UPDATE enrollments SET state = 'active', activated_at = %s "
                                              "WHERE person_id = %s AND offering_id = %s RETURNING id",
                                         (early, c_row, o_row)))
        sql.execute("UPDATE invites SET state = 'used', used_at = %s WHERE enrollment_id = %s AND state = 'pending'",
                    (early, enrollments["equipe"]))
        endpoints["equipe"] = _endpoint(sql, c_row, o_bot, PURGE_C.accounts[0].user_id, "active", early)
        rows["delivery_endpoints"].append(endpoints["equipe"])
        people_rows["equipe"] = (PURGE_C, c_row, c_pseudo, c_alias)

    # 3. A era do contato antigo: uma mensagem livre neutra e a resposta fixa com o e-mail antigo.
    contact_outbox: list[int] = []
    if bot:
        update = _update(sql, o_bot, chat_id=PURGE_B.accounts[0].user_id, text="Quando sai a próxima questão?",
                         outcome="free_message", at=early + timedelta(minutes=5))
        rows["telegram_updates"].append(update)
        rows["free_messages"].append(_free_message(sql, o_row, endpoints["aluno_b"], update,
                                                   "Quando sai a próxima questão?", early + timedelta(minutes=5),
                                                   replied=initial is not None))
        if initial is not None:
            contact_outbox.append(_fixed_reply(sql, o_bot, update, initial, early + timedelta(minutes=5)))

    # 4. O texto livre gravado antes do cadastro: as mensagens livres (e a resposta de texto de `O2`) que citam `citada`
    # literalmente, gravadas antes do cadastro dela.
    p50_rows: list[int] = []
    cited_text = f"Alguém sabe se {PURGE_D.name} vai entregar o trabalho?"
    if with_p50_here:
        update = _update(sql, o_bot, chat_id=PURGE_A.accounts[0].user_id, text=cited_text, outcome="free_message",
                         at=early + timedelta(minutes=10))
        rows["telegram_updates"].append(update)
        fm = _free_message(sql, o_row, endpoints["aluno_a"], update, cited_text, early + timedelta(minutes=10),
                           replied=False)
        rows["free_messages"].append(fm)
        p50_rows.append(fm)
    other_info: OtherOffering | None = None
    if other is not None:
        cross_updates: list[int] = []
        cross_free: list[int] = []
        cross_submissions: list[int] = []
        other_dispatch_row = other_dispatch_id = None
        if cross_offering:
            update = _update(sql, other_bot, chat_id=neutral_chat, text=cited_text, outcome="free_message",
                             at=early + timedelta(minutes=10))
            cross_updates.append(update)
            fm = _free_message(sql, other_row, other_endpoints[NEUTRAL_LABEL], update, cited_text,
                               early + timedelta(minutes=10), replied=False)
            cross_free.append(fm)
            p50_rows.append(fm)
            other_question = _question(sql, "text", early)
            other_dispatch_row, other_dispatch_id = _dispatch(sql, other_row, other_question, state="closed",
                                                              opens=opens, closes=closes)
            answer = f"igual à de {PURGE_D.name}"
            _, submission, update = _answered(
                sql, dispatch_row=other_dispatch_row, enrollment_row=other_enrollments[NEUTRAL_LABEL],
                person_row=_person_by_ra(sql, neutral.ra)[0], bot_row=other_bot,
                endpoint_row=other_endpoints[NEUTRAL_LABEL], chat_id=neutral_chat, question=preset("text"),
                raw={"resposta": answer}, text=answer, score=0.0, part_derived=[_part(0)], opens=opens,
                answered=answered_at, revealed=revealed_at)
            cross_updates.append(update)
            cross_submissions.append(submission)
        other_info = OtherOffering(offering=other, code=other["code"], offering_row=other_row, bot_row=other_bot,
                                   dispatch_id=other_dispatch_id, dispatch_row=other_dispatch_row,
                                   update_rows=tuple(cross_updates), free_message_rows=tuple(cross_free),
                                   submission_rows=tuple(cross_submissions))

    # 5. O cadastro de `citada` (depois das mensagens que a citam): importada só em `O`, com o endereço bloqueado.
    _import(client, code, [PURGE_D.row()])
    d_row, d_pseudo, d_alias = _person_by_ra(sql, PURGE_D.ra)
    people_rows["citada"] = (PURGE_D, d_row, d_pseudo, d_alias)
    if bot:
        enrollments["citada"] = _activate_student(sql, d_row, o_row, early + timedelta(minutes=20))
        endpoints["citada"] = _endpoint(sql, d_row, o_bot, PURGE_D.accounts[0].user_id, "blocked",
                                        early + timedelta(minutes=20))
        rows["delivery_endpoints"].append(endpoints["citada"])

    # 6. O disparo fechado de `O` (texto e número citando `aluno_b`) com a versão congelada.
    question_row = dispatch_row = None
    dispatch_id = None
    deliveries: dict[str, int] = {}
    submissions: dict[str, int] = {}
    if with_dispatch:
        question_row = _question(sql, "duas-partes", early, ("texto", "numero"))
        dispatch_row, dispatch_id = _dispatch(sql, o_row, question_row, state="closed", opens=opens, closes=closes)
        a_text = f"a mesma de {PURGE_B.name}"
        deliveries["aluno_a"], submissions["aluno_a"], update = _answered(
            sql, dispatch_row=dispatch_row, enrollment_row=enrollments["aluno_a"], person_row=a_row, bot_row=o_bot,
            endpoint_row=endpoints["aluno_a"], chat_id=PURGE_A.accounts[0].user_id, question=_TWO_PARTS,
            raw={"texto": a_text, "numero": PURGE_B.ra}, text=a_text, score=0.0,
            part_derived=[_part(0), _part(1, ratio=float(PURGE_B.ra) / 6.5, residual=float(PURGE_B.ra) - 6.5)],
            opens=opens, answered=answered_at, revealed=revealed_at)
        rows["telegram_updates"].append(update)
        deliveries["aluno_b"], submissions["aluno_b"], update = _answered(
            sql, dispatch_row=dispatch_row, enrollment_row=enrollments["aluno_b"], person_row=b_row, bot_row=o_bot,
            endpoint_row=endpoints["aluno_b"], chat_id=PURGE_B.accounts[0].user_id, question=_TWO_PARTS,
            raw={"texto": "azul", "numero": "6.5"}, text="azul", score=1.0,
            part_derived=[_part(0), _part(1, ratio=1.0, residual=0.0)], opens=opens, answered=answered_at,
            revealed=revealed_at)
        rows["telegram_updates"].append(update)
        deliveries["citada"] = _not_delivered(sql, dispatch_row=dispatch_row, enrollment_row=enrollments["citada"],
                                              endpoint_row=endpoints["citada"], at=opens)
        rows["outbox_messages"] += [int(r[0]) for r in sql.execute(
            "SELECT principal_message_id FROM deliveries WHERE id = ANY(%s) AND principal_message_id IS NOT NULL "
            "ORDER BY id", ([deliveries["aluno_a"], deliveries["aluno_b"]],)).fetchall()]

    # 7. A troca do contato antes do arquivamento e a resposta fixa com o vigente.
    if contact == "antigo→vigente" or contact == "antigo→removido":
        resp = client.patch(f"/v1/offerings/{code}/settings", json={"contact_email": current},
                            headers=auth(ADMIN_TOKEN))
        assert resp.status_code == 200, f"troca do contato falhou: {resp.status_code} {resp.text[:300]}"
        assert_matches_tolerant("offerings.update_settings", 200, resp.json())
    if bot and current is not None and contact == "antigo→vigente":
        update = _update(sql, o_bot, chat_id=PURGE_A.accounts[0].user_id, text="Posso entregar depois do prazo?",
                         outcome="free_message", at=now - timedelta(minutes=30))
        rows["telegram_updates"].append(update)
        rows["free_messages"].append(_free_message(sql, o_row, endpoints["aluno_a"], update,
                                                   "Posso entregar depois do prazo?", now - timedelta(minutes=30),
                                                   replied=True))
        contact_outbox.append(_fixed_reply(sql, o_bot, update, current, now - timedelta(minutes=30)))
    rows["outbox_messages"] += contact_outbox

    # 8. As versões congeladas (com todas as identidades já cadastradas, inclusive `citada`).
    if dispatch_row is not None:
        _freeze(sql, dispatch_row, now)
    if other_info is not None and other_info.dispatch_row is not None:
        _freeze(sql, other_info.dispatch_row, now)

    # 9. O oferecimento ativo com o mesmo contato vigente (a regra do contato igual).
    shared_info: OtherOffering | None = None
    if shared_contact:
        o3, o3_row, o3_bot = _with_bot(client, sql, bot_api, "expurgo-contato", contact_email=current)
        update = _update(sql, o3_bot, chat_id=9_300_000_000 + next(_counter), text="Oi, tem alguém aí?",
                         outcome="not_linked", at=now - timedelta(minutes=20))
        reply = _fixed_reply(sql, o3_bot, update, current, now - timedelta(minutes=20))
        shared_info = OtherOffering(offering=o3, code=o3["code"], offering_row=o3_row, bot_row=o3_bot,
                                    update_rows=(update,), outbox_rows=(reply,))

    # 10. Encerramento e arquivamento pelas rotas; a drenagem das tarefas que eles (e as ativações) geram.
    if bot:
        _post(client, f"/v1/offerings/{code}/close", "offerings.close")
    prepared = _post(client, f"/v1/offerings/{code}/archive/prepare", "offerings.prepare_archive")
    archived = _post(client, f"/v1/offerings/{code}/archive", "offerings.archive",
                     {"confirmation_code": prepared["confirmation_code"]})
    assert archived["state"] == "archived", f"a semente não arquivou o oferecimento: {archived['state']}"
    scenario_bots = [b for b in (o_bot, other_bot, shared_info.bot_row if shared_info else None) if b is not None]
    _drain_profiles(sql, scenario_bots)
    pending = pending_counts(sql, o_row)
    assert pending == dict.fromkeys(PENDING_KEYS, 0), f"a semente do expurgo deixou trabalho pendente: {pending}"

    # 11. As contagens esperadas.
    kept = _classify(sql, o_row)
    people: dict[str, ScenarioPerson] = {}
    for label, (identity, row, pseudo, alias) in people_rows.items():
        people[label] = ScenarioPerson(label, identity, row, pseudo, alias, deleted=not kept.get(row, False))
    impact = PurgeImpact(identities_to_delete=sum(1 for value in kept.values() if not value),
                         identities_kept=sum(1 for value in kept.values() if value),
                         endpoints=len(rows["delivery_endpoints"]), updates=len(rows["telegram_updates"]),
                         contact_email_set=current is not None)
    outcome = PurgeOutcome(identities_deleted=impact.identities_to_delete, identities_kept=impact.identities_kept,
                           endpoints=impact.endpoints, updates=impact.updates,
                           free_messages_redacted=_literal_mentions(sql), outbox_redacted=len(contact_outbox),
                           contact_email_deleted=current is not None)
    scenario = PurgeScenario(
        offering=archived, code=code, offering_id=archived["offering_id"], offering_row=o_row, bot_row=o_bot,
        people=people, impact=impact, outcome=outcome, contact=contact, contact_old=old, contact_current=current,
        seeded={"bot": bot, "team": with_team, "shared": shared, "cross_offering": cross_offering,
                "frozen_report": with_dispatch, "free_message_before_register": with_p50_here,
                "shared_contact": shared_contact},
        question_row=question_row, dispatch_id=dispatch_id, dispatch_row=dispatch_row, deliveries=deliveries,
        submissions=submissions, rows={table: tuple(ids) for table, ids in rows.items()},
        contact_outbox_rows=tuple(contact_outbox), free_text_limit_message_rows=tuple(p50_rows), other=other_info,
        shared_contact=shared_info)
    _LAST[:] = [scenario]
    return scenario


# --- trabalho pendente por SQL -------------------------------------------------------------------------


def _last(scenario: PurgeScenario | None) -> PurgeScenario:
    if scenario is not None:
        return scenario
    assert _LAST, "make_pending sem cenário: chame seed_purge_scenario antes"
    return _LAST[0]


def _needs(value: Any, what: str) -> Any:
    if value is None:
        raise ValueError(f"make_pending: o cenário não tem {what}")
    return value


def _task(sql: psycopg.Connection, task_type: str, payload: dict, *, state: str, at: datetime) -> int:
    leased = state == "leased"
    finished = state in ("done", "failed")
    return int(_one(sql, "INSERT INTO tasks (task_type, payload, idempotency_key, state, attempts, available_at, "
                         "lease_owner, lease_until, last_error, created_at, finished_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (task_type, Jsonb(payload), f"semente-pendente:{task_type}:{uuid.uuid4().hex}", state,
                     3 if finished else (1 if leased else 0), at, _WORKER if leased else None,
                     at + timedelta(minutes=5) if leased else None, "RuntimeError" if state == "failed" else None,
                     at, at if finished else None)))


def _spare_dispatch(sql: psycopg.Connection, scenario: PurgeScenario, at: datetime) -> int:
    """Um disparo `cancelled` por operador (não conta em nenhuma classe), para os assuntos que exigem disparo."""
    question = scenario.question_row if scenario.question_row is not None else _question(sql, "apoio", at)
    row, _ = _dispatch(sql, scenario.offering_row, question, state="cancelled", opens=at,
                       closes=at + timedelta(hours=1))
    return row


def _task_subject(sql: psycopg.Connection, key: str, scenario: PurgeScenario, at: datetime) -> int:
    """Uma linha do oferecimento do cenário para a chave `key` do `payload` (criada quando preciso, sem contar
    em nenhuma classe por si)."""
    o = scenario.offering_row
    if key == "dispatch_row_id":
        return _needs(scenario.dispatch_row, "disparo (frozen_report)")
    if key == "delivery_row_id":
        return _needs(scenario.deliveries.get("aluno_a"), "entrega (frozen_report)")
    if key == "submission_row_id":
        return _needs(scenario.submissions.get("aluno_a"), "submissão (frozen_report)")
    if key == "update_row_id":
        return _needs(scenario.rows.get("telegram_updates") or None, "update (bot)")[0]
    if key == "notice_row_id":
        dispatch = _spare_dispatch(sql, scenario, at)
        return int(_one(sql, "INSERT INTO team_notices (offering_id, kind, subject_key, state, dispatch_id, "
                             "created_at, finished_at) VALUES (%s, 'dispatch_preview', %s, 'no_recipient', %s, %s, "
                             "%s) RETURNING id",
                        (o, f"dispatch:{dispatch}", dispatch, at, at)))
    if key == "batch_row_id":
        return int(_one(sql, "INSERT INTO invite_batches (batch_uuid, offering_id, created_at) VALUES (%s, %s, %s) "
                             "RETURNING id", (uuid.uuid4(), o, at)))
    if key == "preview_row_id":
        dispatch = _spare_dispatch(sql, scenario, at)
        return int(_one(sql, "INSERT INTO dispatch_previews (dispatch_id, seed, button_token, created_at) "
                             "VALUES (%s, 0, %s, %s) RETURNING id", (dispatch, secrets.token_urlsafe(16), at)))
    if key == "digest_row_id":
        sql.execute("INSERT INTO offering_digests (offering_id, enabled, local_time, next_due_at, updated_at) "
                    "VALUES (%s, false, '21:00', NULL, %s) ON CONFLICT (offering_id) DO NOTHING", (o, at))
        return int(_one(sql, "SELECT id FROM offering_digests WHERE offering_id = %s", (o,)))
    if key == "offering_row_id":
        return o
    if key == "invite_send_id":
        enrollment = int(_one(sql, "SELECT e.id FROM enrollments e WHERE e.offering_id = %s AND e.role = 'student' "
                                   "ORDER BY e.id LIMIT 1", (o,)))
        return int(_one(sql, "INSERT INTO invite_sends (enrollment_id, origin, valid_hours, state, attempts, "
                             "created_at, finished_at) VALUES (%s, 'coordination', 168, 'sent', 1, %s, %s) "
                             "RETURNING id", (enrollment, at, at)))
    if key == "bot_id":
        return _needs(scenario.bot_row, "bot")
    raise ValueError(f"chave de assunto desconhecida: {key}")


# O tipo de tarefa de cada chave (qualquer um fora do `question.refetch`; cada um escolhido para facilitar a leitura).
_TASK_TYPE_OF = {
    "dispatch_row_id": "dispatch.close", "delivery_row_id": "delivery.prepare", "submission_row_id": "submission.grade",
    "update_row_id": "update.process", "notice_row_id": "notice.send", "batch_row_id": "notice.invite_batch",
    "preview_row_id": "preview.send", "digest_row_id": "digest.daily", "offering_row_id": "notice.access_episode",
    "invite_send_id": "invite.email", "bot_id": "bot.sync_profile",
}


def make_pending(sql: psycopg.Connection, kind: str, scenario: PurgeScenario | None = None) -> str:
    """Cria, por SQL, uma unidade de trabalho pendente do tipo `kind` (`PENDING_KINDS`) no oferecimento do
    cenário e devolve a classe de trabalho pendente que ela conta (exatamente 1 nela, 0 nas demais, partindo do
    cenário limpo). `FAILED_PROFILE_KIND` cria a `bot.sync_profile` `failed` (não bloqueia: devolve `""`).

    Tipos: `outbox_pending`/`outbox_leased` (resposta fixa na fila do bot); `update_pending`; `task:<chave>` (a
    tarefa `pending` com o assunto do oferecimento por cada chave de `TASK_SUBJECT_KEYS`) e `task_leased` (a
    `bot.sync_profile` em posse); `freeze` (um disparo `closed` sem versão `closing` e sem entregas);
    `reveal_failed` e `reveal_without_task` (a entrega respondida de `aluno_a` volta a "não revelada", com a
    tarefa `delivery.reveal` `failed` ou sem tarefa); `notice_pending` (um aviso `team_notices` `pending`)."""
    scenario = _last(scenario)
    at = _fixture("clock").now()
    o_bot = scenario.bot_row
    if kind in ("outbox_pending", "outbox_leased"):
        update = _needs(scenario.rows.get("telegram_updates") or None, "update (bot)")[0]
        _fixed_reply(sql, _needs(o_bot, "bot"), update, None, at, state=kind.removeprefix("outbox_"))
    elif kind == "update_pending":
        _update(sql, _needs(o_bot, "bot"), chat_id=PURGE_A.accounts[0].user_id, text="mensagem ainda na fila",
                outcome="", at=at, state="pending")
    elif kind == "task_leased":
        _task(sql, "bot.sync_profile", {"bot_id": _needs(o_bot, "bot")}, state="leased", at=at)
    elif kind == FAILED_PROFILE_KIND:
        _task(sql, "bot.sync_profile", {"bot_id": _needs(o_bot, "bot")}, state="failed", at=at)
        return ""
    elif kind.startswith("task:"):
        key = kind.removeprefix("task:")
        if key not in TASK_SUBJECT_KEYS:
            raise ValueError(f"tipo de pendência desconhecido: {kind}")
        _task(sql, _TASK_TYPE_OF[key], {key: _task_subject(sql, key, scenario, at)}, state="pending", at=at)
    elif kind == "freeze":
        question = scenario.question_row if scenario.question_row is not None else _question(sql, "apoio", at)
        _dispatch(sql, scenario.offering_row, question, state="closed", opens=at - timedelta(hours=2),
                  closes=at - timedelta(hours=1))
    elif kind in ("reveal_failed", "reveal_without_task"):
        delivery = _needs(scenario.deliveries.get("aluno_a"), "entrega respondida (frozen_report)")
        sql.execute("UPDATE deliveries SET revealed_at = NULL WHERE id = %s", (delivery,))
        if kind == "reveal_failed":
            _task(sql, "delivery.reveal", {"delivery_row_id": delivery}, state="failed", at=at)
    elif kind == "notice_pending":
        dispatch = _spare_dispatch(sql, scenario, at)
        sql.execute("INSERT INTO team_notices (offering_id, kind, subject_key, state, dispatch_id, created_at) "
                    "VALUES (%s, 'dispatch_opened', %s, 'pending', %s, %s)",
                    (scenario.offering_row, f"dispatch:{dispatch}", dispatch, at))
    else:
        raise ValueError(f"tipo de pendência desconhecido: {kind}")
    return PENDING_KINDS[kind]


# --- instantâneo ---------------------------------------------------------------------------------------

# As seis tabelas do expurgo e `offerings`, com a chave de ordem de cada uma.
PURGE_TABLES: dict[str, str] = {
    "delivery_endpoints": "id",
    "telegram_updates": "id",
    "free_messages": "id",
    "outbox_messages": "id",
    "identities": "person_id",
    "offering_contacts": "id",
    "offerings": "id",
}


def snapshot_purge_tables(sql: psycopg.Connection) -> dict[str, list[dict]]:
    """As linhas inteiras (todas as colunas, como objeto JSON) das tabelas de `PURGE_TABLES`, em ordem da chave:
    dois instantâneos iguais = nada mudou nelas."""
    return {table: [row[0] for row in sql.execute(f"SELECT to_jsonb(t) FROM {table} t ORDER BY t.{key}").fetchall()]
            for table, key in PURGE_TABLES.items()}
