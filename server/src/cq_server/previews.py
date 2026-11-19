"""Prévia renderizada no chat da equipe: amostra, tarefa da prévia, carregador da posse, toque e resposta
à mensagem da prévia. Cada parte abaixo diz quem a chama, em que transação e o que grava; a prévia
reaproveita o renderizador da entrega das questões e a fila de saída dos avisos à equipe, sem caminho
próprio de envio.

- **Amostra** (`record_sample`): chamada pela validação do disparo dentro do `if updated:`, na transação da
  passagem a `scheduled`; grava a seed da validação (`int(seed, 36)`), a IR sem `data_base64` e um
  token de botões próprio.
- **Tarefa** `preview.send` (`preview_task_handler`): três passos, com o motor fora de transação
  (`create_variant(ref, _base36(seed))`, como o preparo da entrega) e a gravação final com o disparo
  `FOR SHARE`; o cabeçalho pela `outbox.enqueue_notice_text` e as mensagens da questão pela
  `outbox.enqueue_notice_content`.
- **Posse** (`NoticeContentLoader`): monta a `OutgoingMessage` das mensagens da forma 3 a partir do
  `rendered` gravado pela tarefa (sem canal), com `answerable = True`.
- **Botões inertes e resposta** (`is_preview_token`, `process_reply`): o toque recebe `PREVIEW_TAP`;
  a resposta por "responder" a uma mensagem da prévia recebe `PREVIEW_REPLY` pela fila (prioridade 2).

Importa `deliveries` (renderizador), `outbox`, `teamnotices` e `noticetexts`; nunca HTTP nem
`cq_server.adapters`. O canal (formatador e capacidades) só é lido na execução da tarefa.

**`rendered` da prévia**, gravado pela tarefa e só copiado pelo
carregador: `{"format": <dialeto da questão>, "question": {"text", "keyboard"}, "media": {"mN":
{"caption", "as_document", "filename", "content_type"}}, "field_prompt": {"<i>": {"text",
"text_format"}}}`.

**Escolhas [I] desta implementação** (detalhes de forma que a prévia não fixa):
- a chave do assunto no payload da tarefa é `preview_row_id` (o `dispatch_previews.id`);
- a chave do cabeçalho é `notice:<id>:p<pessoa>:header` (o cabeçalho não tem papel na fila);
- na gravação final, as identidades (o lock dos eventos) vêm **antes** do INSERT na fila, e não depois
  como na ordem de travas prevista para a prévia: o título do cabeçalho é redigido antes de enfileirado, como
  na fase 2 dos avisos (identidades → composição → INSERT na fila → eventos);
- o imediato com fechamento informado usa o `PREVIEW_HEADER` com a abertura `EMPTY` ("—");
- o pedido de campo sem dica no campo sai sem a linha `PREVIEW_FIELD_HINT`;
- a variante devolvida pela tarefa só fornece os bytes das mídias; a renderização é da IR gravada na
  amostra (`build_rendered(ir_gravada, …)`); a mídia `ok` sem bytes na variante
  faz a rodada falhar, sem mensagem.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import secrets
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

import psycopg
from psycopg.types.json import Jsonb

from cq_server import deliveries, deliverytexts, noticetexts, outbox, teamnotices
from cq_server._leasing import LazyConnection
from cq_server.clock import Clock
from cq_server.config import DeliverySettings, Settings, redaction_secrets
from cq_server.events import load_write_identities
from cq_server.ports.delivery import DeliveryChannel, MediaPayload, OutgoingMessage
from cq_server.ports.engine import (
    Broken,
    EngineNotFound,
    EngineRequestRejected,
    EngineUnavailable,
    QuestionEngine,
    QuestionRef,
    Unsupported,
    Variant,
)
from cq_server.redaction import redact
from cq_server.tasks import ClaimedTask, PermanentTaskError, enqueue_in_transaction, finished_rounds, has_live_task
from cq_server.timezones import format_local

if TYPE_CHECKING:
    from cq_server.answers import Inbound
    from cq_server.deliveries import Skip
    from cq_server.outbox import NoticeRow, Outbox

# A tarefa da prévia e o teto de rodadas da varredura dela.
PREVIEW_TASK_TYPE = "preview.send"
PREVIEW_MAX_ROUNDS = 5
# O nome da chave do assunto no payload da tarefa `preview.send`.
PREVIEW_SUBJECT_KEY = "preview_row_id"
# O tipo de aviso da prévia.
PREVIEW_KIND = teamnotices.NoticeKind.DISPATCH_PREVIEW
# A chave da `PREVIEW_REPLY` na fila.
REPLY_KEY_PREFIX = "preview-reply"

_NO_CHANNEL = "canal sem rota"
_REFETCH_TASK_TYPE = "question.refetch"


# --------------------------------------------------------------------------------------------------
# Amostra
# --------------------------------------------------------------------------------------------------


def _without_media_bytes(ir: Mapping[str, Any]) -> dict[str, Any]:
    """A IR como JSON simples, sem o `data_base64` das mídias."""
    plain = deliveries._plain_json(ir)
    media = []
    for item in plain.get("media") or []:
        item = dict(item) if isinstance(item, Mapping) else item
        if isinstance(item, dict):
            item.pop("data_base64", None)
        media.append(item)
    if "media" in plain:
        plain["media"] = media
    return plain


def _draw_token(conn: psycopg.Connection) -> str:
    """O token dos botões da prévia (`secrets.token_urlsafe(16)`, 22 caracteres), sorteado de novo se
    coincidir com o de uma entrega ou de outra prévia, até `team.MAX_CODE_DRAWS` vezes."""
    from cq_server.team import MAX_CODE_DRAWS  # só aqui: a constante dos sorteios dos códigos da equipe

    for _ in range(MAX_CODE_DRAWS):
        token = secrets.token_urlsafe(16)
        taken = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM deliveries WHERE button_token = %s) "
            "OR EXISTS (SELECT 1 FROM dispatch_previews WHERE button_token = %s)",
            (token, token),
        ).fetchone()
        if taken is not None and not taken[0]:
            return token
    raise RuntimeError("sorteios do token da prévia esgotados")


def record_sample(conn: psycopg.Connection, *, dispatch_row_id: int, seed: str, ir: Mapping[str, Any],
                  now: datetime) -> None:
    """Grava a amostra da validação: `seed` é a cadeia base 36 que foi ao motor.

    Na transação do chamador (a da passagem a `scheduled`, dentro do `if updated:`): `dispatch_previews`
    com `seed = int(seed, 36)`, o token sorteado e `created_at = now` (o `validated_at`), e
    `dispatch_preview_contents.ir` com a IR sem `data_base64` (com o gabarito e sem redação: a exceção
    prevista na regra de redação). Nenhuma outra coluna recebe a IR."""
    value = int(seed, 36)
    token = _draw_token(conn)
    row = conn.execute(
        "INSERT INTO dispatch_previews (dispatch_id, seed, button_token, created_at) VALUES (%s, %s, %s, %s) "
        "RETURNING id",
        (dispatch_row_id, value, token, now),
    ).fetchone()
    assert row is not None
    conn.execute("INSERT INTO dispatch_preview_contents (preview_id, ir) VALUES (%s, %s)",
                 (int(row[0]), Jsonb(_without_media_bytes(ir))))


# --------------------------------------------------------------------------------------------------
# Toque e resposta
# --------------------------------------------------------------------------------------------------


def is_preview_token(conn: psycopg.Connection, *, bot_id: int, token: str) -> bool:
    """Verdadeiro se `token` é o de uma prévia de disparo do oferecimento do bot.
    Leitura sem trava; não grava nada."""
    row = conn.execute(
        "SELECT 1 FROM dispatch_previews p JOIN dispatches s ON s.id = p.dispatch_id "
        "JOIN bots b ON b.offering_id = s.offering_id WHERE p.button_token = %s AND b.id = %s LIMIT 1",
        (token, bot_id),
    ).fetchone()
    return row is not None


def process_reply(conn: psycopg.Connection, inbound: Inbound, *, now: datetime, outbox: Outbox) -> bool:
    """Verdadeiro se o texto responde a uma mensagem da prévia no endereço vigente do chat; então
    enfileira `PREVIEW_REPLY`.

    Na transação do update: o texto com `reply_to_message_id` igual ao `channel_ref` de uma mensagem de
    um aviso `dispatch_preview` (cabeçalho, mídia, questão ou pedido) no endereço vigente (`active` ou
    `blocked`) do chat no bot (pelo índice `outbox_messages_notice_ref_idx`) → `PREVIEW_REPLY` ao chat
    do update, prioridade 2, chave `preview-reply:<update>`. Fora disso, falso, sem efeito (a resposta a
    outro aviso e o texto sem "responder" seguem o fluxo da equipe)."""
    del now  # a fila grava o horário do próprio relógio
    if (inbound.kind != "message" or inbound.text is None or inbound.reply_to_message_id is None
            or inbound.chat_id is None):
        return False
    row = conn.execute(
        "SELECT 1 FROM delivery_endpoints d "
        "JOIN outbox_messages m ON m.delivery_endpoint_id = d.id AND m.notice_id IS NOT NULL "
        "AND m.channel_ref = %s "
        "JOIN team_notices n ON n.id = m.notice_id AND n.kind = %s "
        "WHERE d.bot_id = %s AND d.chat_id = %s AND d.state IN ('active', 'blocked') LIMIT 1",
        (str(inbound.reply_to_message_id), PREVIEW_KIND.value, inbound.bot_id, inbound.chat_id),
    ).fetchone()
    if row is None:
        return False
    outbox.enqueue(conn, text=noticetexts.PREVIEW_REPLY, bot_id=inbound.bot_id,
                   reply_to_update_id=inbound.update_row_id, priority=teamnotices.NOTICE_PRIORITY,
                   idempotency_key=f"{REPLY_KEY_PREFIX}:{inbound.update_row_id}")
    return True


# --------------------------------------------------------------------------------------------------
# Carga na posse
# --------------------------------------------------------------------------------------------------


class NoticeContentLoader:
    """O `outbox.NoticeContent` da prévia: só copia o `rendered` gravado."""

    def __init__(self) -> None:
        pass

    def load(self, conn: psycopg.Connection, row: NoticeRow) -> OutgoingMessage | Skip:
        """A `OutgoingMessage` da mensagem da forma 3 (sem trava): a questão (texto, dialeto e teclado), a
        mídia (os bytes de `media_blobs`, a legenda e foto × documento gravados) ou o pedido de campo (o
        texto gravado, sem `reply_request`), todas com `answerable = True`; `Skip` sem o conteúdo."""
        from cq_server.deliveries import Skip

        found = conn.execute(
            "SELECT c.rendered, c.media_refs FROM team_notices n "
            "JOIN dispatch_previews p ON p.dispatch_id = n.dispatch_id "
            "JOIN dispatch_preview_contents c ON c.preview_id = p.id WHERE n.id = %s",
            (row.notice_row_id,),
        ).fetchone()
        if found is None or not isinstance(found[0], Mapping):
            return Skip("no_content")
        rendered: Mapping[str, Any] = found[0]
        refs: Mapping[str, Any] = found[1] if isinstance(found[1], Mapping) else {}
        base = OutgoingMessage(message_id=row.message_id, recipient=row.recipient, text="", answerable=True)
        if row.role == "question":
            question = rendered.get("question")
            if not isinstance(question, Mapping):
                return Skip("no_render")
            return deliveries._replace(base, text=str(question.get("text") or ""),
                                       text_format=str(rendered.get("format")),
                                       keyboard=deliveries._keyboard_from_json(question.get("keyboard")))
        if row.role == "media":
            entry = (rendered.get("media") or {}).get(str(row.media_id))
            sha = refs.get(str(row.media_id))
            if not isinstance(entry, Mapping) or sha is None:
                return Skip("media_missing")
            blob = conn.execute("SELECT content_type, data FROM media_blobs WHERE sha256 = %s", (sha,)).fetchone()
            if blob is None:
                return Skip("media_missing")
            caption = str(entry.get("caption") or "")
            media = MediaPayload(media_id=str(row.media_id), filename=str(entry.get("filename") or row.media_id),
                                 content_type=str(entry.get("content_type") or blob[0]),
                                 as_document=bool(entry.get("as_document")), caption=caption, data=bytes(blob[1]))
            return deliveries._replace(base, text=caption, media=media)
        if row.role == "field_prompt":
            entry = (rendered.get("field_prompt") or {}).get(str(row.field_index))
            if not isinstance(entry, Mapping):
                return Skip("field_not_current")
            return deliveries._replace(base, text=str(entry.get("text") or ""),
                                       text_format=str(entry.get("text_format")))
        return Skip("unknown_role")


# --------------------------------------------------------------------------------------------------
# Tarefa `preview.send`
# --------------------------------------------------------------------------------------------------


def _record_final(conn: psycopg.Connection, *, dispatch_row_id: int, offering_row_id: int,
                  state: teamnotices.NoticeState, now: datetime) -> None:
    """Registra o aviso já num estado final sem mensagem (`gave_up` ou `no_recipient`), com o
    `notice.recorded`, na transação do chamador; sem efeito se o aviso já existe."""
    notice_row_id = teamnotices.record(conn, offering_row_id=offering_row_id, kind=PREVIEW_KIND,
                                       subject_key=f"dispatch:{dispatch_row_id}", data={}, now=now,
                                       dispatch_row_id=dispatch_row_id)
    if notice_row_id is None:
        return
    notice = teamnotices._lock_notice(conn, notice_row_id)
    assert notice is not None
    teamnotices._finish(conn, notice, teamnotices._offering(conn, offering_row_id), state, 0, now)


def give_up(conn: psycopg.Connection, preview_row_id: int, now: datetime) -> None:
    """O `on_exhausted` da regra `preview.send`: esgotadas as
    `PREVIEW_MAX_ROUNDS` rodadas `failed`, o aviso `gave_up` com o evento, sem mensagem; na transação da
    varredura (a amostra travada)."""
    row = conn.execute(
        "SELECT p.dispatch_id, s.offering_id FROM dispatch_previews p JOIN dispatches s ON s.id = p.dispatch_id "
        "WHERE p.id = %s", (preview_row_id,)).fetchone()
    if row is None:
        return
    _record_final(conn, dispatch_row_id=int(row[0]), offering_row_id=int(row[1]),
                  state=teamnotices.NoticeState.GAVE_UP, now=now)


def _notice_exists(conn: psycopg.Connection, dispatch_row_id: int, offering_row_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM team_notices WHERE offering_id = %s AND kind = %s AND subject_key = %s",
        (offering_row_id, PREVIEW_KIND.value, f"dispatch:{dispatch_row_id}"),
    ).fetchone()
    return row is not None


def _escape_braces(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


class _TaskHandler:
    """Tratador da tarefa da prévia (chamável, com `close()`): `preview.send`."""

    def __init__(self, conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                 channel: DeliveryChannel | None, delivery: DeliverySettings | None) -> None:
        self._db = LazyConnection(conninfo)
        self._clock = clock
        self._settings = settings
        self._engine = engine
        self._channel = channel
        self._delivery = delivery if delivery is not None else DeliverySettings()
        # Os segredos configurados (o token inicial), redigidos no cabeçalho (nenhum segredo sai).
        self._secrets = redaction_secrets(settings)

    def close(self) -> None:
        self._db.close()

    def __call__(self, task: ClaimedTask) -> None:
        value = task.payload.get(PREVIEW_SUBJECT_KEY) if isinstance(task.payload, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise PermanentTaskError(f"payload sem {PREVIEW_SUBJECT_KEY}")
        conn = self._db.get()
        # 1. Pré-checagem sem trava.
        row = conn.execute(
            "SELECT p.dispatch_id, p.seed, s.state, s.offering_id, q.repo, q.commit_sha, q.qid, q.id, c.ir "
            "FROM dispatch_previews p JOIN dispatches s ON s.id = p.dispatch_id "
            "JOIN question_refs q ON q.id = s.question_ref_id "
            "JOIN dispatch_preview_contents c ON c.preview_id = p.id WHERE p.id = %s",
            (value,),
        ).fetchone()
        if row is None:
            raise PermanentTaskError("prévia inexistente")
        dispatch_row, seed, state, offering_row, repo, commit, qid, ref_row, ir = row
        dispatch_row, offering_row = int(dispatch_row), int(offering_row)
        if _notice_exists(conn, dispatch_row, offering_row):
            return
        unsent = state != "scheduled" or not teamnotices.team_recipients(conn, offering_row)
        if unsent and self._record_unsent(conn, dispatch_row):
            return
        # 2. O motor fora de transação: uma chamada externa por execução.
        channel = self._channel
        if channel is None:
            raise PermanentTaskError(_NO_CHANNEL)
        if self._engine is None:
            raise EngineUnavailable("unconfigured")
        ref = QuestionRef(repo=repo, commit=commit, qid=qid)
        try:
            result = self._engine.create_variant(ref, deliveries._base36(int(seed)))
        except EngineRequestRejected as exc:
            raise PermanentTaskError("engine_rejected") from exc
        except EngineNotFound:
            self._refetch(conn, int(ref_row))
            raise
        if isinstance(result, Unsupported):
            raise PermanentTaskError("variant_unsupported")
        if isinstance(result, Broken):
            raise PermanentTaskError("variant_broken")
        if not isinstance(result, Variant):
            raise TypeError("resultado do motor fora da porta")
        stored_ir: Mapping[str, Any] = ir if isinstance(ir, Mapping) else {}
        blobs = self._media_bytes(stored_ir, result)
        # 3. A gravação final.
        self._store(conn, dispatch_row, stored_ir, blobs, channel)

    # -- passos -------------------------------------------------------------------------------------

    def _record_unsent(self, conn: psycopg.Connection, dispatch_row_id: int) -> bool:
        """Numa transação curta, com o disparo `FOR SHARE` e a reconferência: fora de `scheduled` →
        `gave_up`; sem destinatário → `no_recipient`. Falso se a reconferência já não vale (segue)."""
        now = self._clock.now()
        with conn.transaction():
            locked = conn.execute("SELECT state, offering_id FROM dispatches WHERE id = %s FOR SHARE",
                                  (dispatch_row_id,)).fetchone()
            if locked is None:
                raise PermanentTaskError("disparo inexistente")
            state, offering_row = str(locked[0]), int(locked[1])
            if _notice_exists(conn, dispatch_row_id, offering_row):
                return True
            if state != "scheduled":
                final = teamnotices.NoticeState.GAVE_UP
            elif not teamnotices.team_recipients(conn, offering_row):
                final = teamnotices.NoticeState.NO_RECIPIENT
            else:
                return False
            _record_final(conn, dispatch_row_id=dispatch_row_id, offering_row_id=offering_row, state=final,
                          now=now)
            return True

    def _refetch(self, conn: psycopg.Connection, ref_row: int) -> None:
        """`EngineNotFound`: o `question.refetch` da entrega se não há tarefa viva para a referência
        (como `deliveries._PrepareHandler._refetch`); a rodada da prévia falha."""
        subject = {"question_ref_row_id": ref_row}
        with conn.transaction():
            if not has_live_task(conn, _REFETCH_TASK_TYPE, subject):
                done, _ = finished_rounds(conn, _REFETCH_TASK_TYPE, subject)
                enqueue_in_transaction(conn, _REFETCH_TASK_TYPE, subject, now=self._clock.now(),
                                       idempotency_key=f"{_REFETCH_TASK_TYPE}:{ref_row}:r{done + 1}")

    @staticmethod
    def _media_bytes(stored_ir: Mapping[str, Any], variant: Variant) -> dict[str, bytes]:
        """Os bytes de cada mídia `ok` da amostra, da variante reproduzida; a mídia `ok` sem bytes faz a
        rodada falhar, sem mensagem."""
        encoded: dict[str, Any] = {}
        for item in (variant.ir.get("media") or []):
            if isinstance(item, Mapping) and item.get("id") is not None:
                encoded[str(item.get("id"))] = item.get("data_base64")
        out: dict[str, bytes] = {}
        for item in stored_ir.get("media") or []:
            if not isinstance(item, Mapping) or item.get("status") != "ok":
                continue
            data = encoded.get(str(item.get("id")))
            if not isinstance(data, str):
                raise PermanentTaskError("media_without_bytes")
            out[str(item.get("id"))] = base64.b64decode(data)
        return out

    def _render(self, stored_ir: Mapping[str, Any], blobs: Mapping[str, bytes], channel: DeliveryChannel, *,
                token: str, closes_at: datetime | None, zone: str, single_variant: bool) -> dict[str, Any]:
        """O `rendered` da prévia: o `_render` de `deliveries` sobre o
        estado inicial com o `_View` aberto e o token da prévia; por mídia, a legenda, o tipo, o nome e
        foto × documento; o pedido do 1º campo digitável com o texto do aluno e a linha
        `PREVIEW_FIELD_HINT`."""
        formatter = channel.formatter
        built = deliveries.build_rendered(stored_ir, formatter=formatter, capabilities=channel.capabilities)
        view = deliveries._View(state="pending", current_field=0, controls_version=1, render_version=0,
                                button_token=token, revealed=False, dispatch_state="open", closes_at=closes_at,
                                timezone=zone, single_variant=single_variant, rendered=built)
        composed, keyboard, _fits = deliveries._render(view, formatter)
        media: dict[str, Any] = {}
        for index, item in enumerate(stored_ir.get("media") or [], start=1):
            media_id = str(item.get("id"))
            if media_id not in blobs:
                continue
            data = blobs[media_id]
            content_type = str(item.get("content_type") or "application/octet-stream")
            media[media_id] = {
                "caption": deliveries._caption(deliveries._figure_number(media_id, index), item.get("alt")),
                "as_document": content_type not in deliveries.PHOTO_TYPES or len(data) > self._delivery.photo_max_bytes,
                "filename": posixpath.basename(str(item.get("filename") or "")) or media_id,
                "content_type": content_type,
            }
        prompts: dict[str, Any] = {}
        fields = list(built.get("fields") or [])
        if fields and fields[0].get("typeable"):
            # O formatador do dialeto curto só aqui, na execução, como o `_field_prompt` de `deliveries`.
            short = channel.formatter_for(deliveries._short_dialect(built))
            spec = fields[0]
            label = short.escape(spec["label"]).markup if spec.get("label") else None
            suffix = short.escape(spec["suffix"]).markup if spec.get("suffix") else None
            markup = deliverytexts.field_request(1, len(fields), label, suffix)
            hint = spec.get("hint")
            if isinstance(hint, str) and hint:
                # A linha da dica na mesma quebra do pedido (um bloco só, como as linhas fixas da entrega).
                markup += "\n" + short.escape(noticetexts.PREVIEW_FIELD_HINT.format(hint=hint)).markup
            prompts["0"] = {"text": short.compose([deliveries._raw(short, markup)]).markup,
                            "text_format": short.text_format}
        return {"format": formatter.text_format,
                "question": {"text": composed.markup, "keyboard": deliveries._keyboard_json(keyboard)},
                "media": media, "field_prompt": prompts, "fields": len(fields)}

    def _header(self, conn: psycopg.Connection, channel: DeliveryChannel, *, dispatch: Mapping[str, Any],
                offering: Any, parts: int) -> str:
        """O cabeçalho: o título
        redigido (truncado só ele, para caber), abertura e fechamento no fuso (ou `PREVIEW_IMMEDIATE`), os
        alunos-alvo, `PREVIEW_PARTS` (N ≥ 2) e os avisos da prévia pelo tipo, antes da linha do comando. O
        título sai redigido com as identidades e os segredos configurados (regra de privacidade)."""
        from cq_server.dispatches import CORE_WARNING_MESSAGES  # só aqui (sem ciclo)

        identities = load_write_identities(conn)
        preview = dispatch["preview"] if isinstance(dispatch["preview"], Mapping) else {}
        raw_title = preview.get("title")
        title = (redact(raw_title, identities, secrets=self._secrets) if isinstance(raw_title, str) and raw_title
                 else noticetexts.UNTITLED)
        optional: list[str] = [noticetexts.PREVIEW_PARTS.format(parts=parts)] if parts >= 2 else []
        for warning in preview.get("warnings") or []:
            kind = warning.get("kind") if isinstance(warning, Mapping) else None
            if kind in CORE_WARNING_MESSAGES:
                optional.append(CORE_WARNING_MESSAGES[kind])
            elif kind in noticetexts.PREVIEW_ENGINE_WARNINGS:
                optional.append(noticetexts.PREVIEW_ENGINE_WARNINGS[kind])
        values: dict[str, Any] = {"title": title, "target": int(preview.get("target_count") or 0),
                                  "code": offering.code, "dispatch_id": str(dispatch["dispatch_uuid"])}
        opens_at, closes_at = dispatch["opens_at"], dispatch["closes_at"]
        if opens_at is None and closes_at is None:
            base = noticetexts.PREVIEW_IMMEDIATE
            values["minutes"] = int(dispatch["window_minutes"] or 0)
        else:
            base = noticetexts.PREVIEW_HEADER
            values["opens_at"] = noticetexts.EMPTY if opens_at is None else format_local(opens_at, offering.timezone)
            values["closes_at"] = noticetexts.EMPTY if closes_at is None else format_local(closes_at, offering.timezone)
        template = teamnotices._with_optional(base, before_last=[_escape_braces(line) for line in optional])
        return teamnotices._compose(template, channel.formatter, values, "title").markup

    def _store(self, conn: psycopg.Connection, dispatch_row_id: int, stored_ir: Mapping[str, Any],
               blobs: Mapping[str, bytes], channel: DeliveryChannel) -> None:
        """A gravação final numa transação: disparo `FOR SHARE` e a
        reconferência (fora de `scheduled` → `gave_up`; sem destinatário → `no_recipient`; aviso já
        registrado → sem efeito); o aviso; os blobs; `media_refs` e `rendered`; o cabeçalho e as mensagens
        de cada destinatário, nessa ordem; `queued` com o evento. Tudo ou nada (sem mensagem parcial)."""
        now = self._clock.now()
        with conn.transaction():
            found = conn.execute(
                "SELECT s.state, s.offering_id, s.dispatch_uuid, s.opens_at, s.closes_at, s.window_minutes, "
                "s.preview, o.timezone, coalesce(q.single_variant, false), p.id, p.button_token "
                "FROM dispatches s JOIN offerings o ON o.id = s.offering_id "
                "JOIN question_refs q ON q.id = s.question_ref_id "
                "JOIN dispatch_previews p ON p.dispatch_id = s.id WHERE s.id = %s FOR SHARE OF s",
                (dispatch_row_id,),
            ).fetchone()
            if found is None:
                raise PermanentTaskError("disparo inexistente")
            (state, offering_row, dispatch_uuid, opens_at, closes_at, window_minutes, preview, zone, single,
             preview_row, token) = found
            offering_row = int(offering_row)
            if _notice_exists(conn, dispatch_row_id, offering_row):
                return
            recipients = teamnotices.team_recipients(conn, offering_row)
            offering = teamnotices._offering(conn, offering_row)
            if state != "scheduled" or not recipients or offering.bot_row_id is None:
                final = (teamnotices.NoticeState.GAVE_UP if state != "scheduled"
                         else teamnotices.NoticeState.NO_RECIPIENT)
                _record_final(conn, dispatch_row_id=dispatch_row_id, offering_row_id=offering_row, state=final,
                              now=now)
                return
            notice_row_id = teamnotices.record(conn, offering_row_id=offering_row, kind=PREVIEW_KIND,
                                               subject_key=f"dispatch:{dispatch_row_id}", data={}, now=now,
                                               dispatch_row_id=dispatch_row_id)
            if notice_row_id is None:  # pragma: no cover - conferido acima, sob o disparo travado
                return
            refs: dict[str, str] = {}
            media_types = {str(m.get("id")): str(m.get("content_type") or "application/octet-stream")
                           for m in stored_ir.get("media") or [] if isinstance(m, Mapping)}
            rows = []
            for media_id, data in blobs.items():
                sha = hashlib.sha256(data).hexdigest()
                refs[media_id] = sha
                rows.append((sha, media_types.get(media_id, "application/octet-stream"), data))
            # Em ordem de sha256, como o preparo da entrega (sem espera em ordens opostas).
            for sha, content_type, data in sorted(rows, key=lambda blob: blob[0]):
                conn.execute(
                    "INSERT INTO media_blobs (sha256, content_type, size_bytes, data, created_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sha256) DO NOTHING",
                    (sha, content_type, len(data), data, now),
                )
            rendered = self._render(stored_ir, blobs, channel, token=str(token), closes_at=closes_at,
                                    zone=str(zone), single_variant=bool(single))
            conn.execute("UPDATE dispatch_preview_contents SET media_refs = %s, rendered = %s WHERE preview_id = %s",
                         (Jsonb(refs), Jsonb(rendered), int(preview_row)))
            header = self._header(conn, channel,
                                  dispatch={"preview": preview, "dispatch_uuid": dispatch_uuid, "opens_at": opens_at,
                                            "closes_at": closes_at, "window_minutes": window_minutes},
                                  offering=offering, parts=int(rendered["fields"]))
            media_ids = [str(m.get("id")) for m in stored_ir.get("media") or []
                         if isinstance(m, Mapping) and str(m.get("id")) in rendered["media"]]
            for recipient in recipients:
                base = f"notice:{notice_row_id}:p{recipient.person_row_id}"
                target = {"notice_row_id": notice_row_id, "recipient": recipient.pseudo_id,
                          "bot_id": offering.bot_row_id, "endpoint_row_id": recipient.endpoint_row_id, "now": now}
                outbox.enqueue_notice_text(conn, text=header, text_format=channel.formatter.text_format,
                                           idempotency_key=f"{base}:header", **target)
                for media_id in media_ids:
                    outbox.enqueue_notice_content(conn, role="media", media_id=media_id,
                                                  idempotency_key=f"{base}:media:{media_id}", **target)
                outbox.enqueue_notice_content(conn, role="question", idempotency_key=f"{base}:question", **target)
                if "0" in rendered["field_prompt"]:
                    outbox.enqueue_notice_content(conn, role="field_prompt", field_index=0,
                                                  idempotency_key=f"{base}:field_prompt:0", **target)
            notice = teamnotices._lock_notice(conn, notice_row_id)
            assert notice is not None
            teamnotices._finish(conn, notice, offering, teamnotices.NoticeState.QUEUED, len(recipients), now)


def preview_task_handler(conninfo: str, *, clock: Clock, settings: Settings, engine: QuestionEngine | None,
                         channel: DeliveryChannel | None,
                         delivery: DeliverySettings | None = None) -> _TaskHandler:
    """O tratador `preview.send`: `engine = None` → `EngineUnavailable("unconfigured")`;
    `delivery = None` → `DeliverySettings()` (o `photo_max_bytes` da escolha foto × documento).

    A conexão abre no primeiro uso; nada lê o canal aqui. `EngineUnavailable` sobe (falha
    temporária da tarefa); `EngineNotFound` enfileira o `question.refetch` e sobe; `EngineRequestRejected`,
    `Broken`, `Unsupported` e a mídia sem bytes → `PermanentTaskError` (a rodada falha, sem mensagem)."""
    return _TaskHandler(conninfo, clock=clock, settings=settings, engine=engine, channel=channel,
                        delivery=delivery)
