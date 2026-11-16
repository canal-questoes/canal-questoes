"""Esquemas Pydantic da API v1.

- Todo modelo herda de `StrictModel` (`extra="forbid"`: `additionalProperties: false` no OpenAPI e
  nenhum campo fora do esquema na resposta; `hide_input_in_errors`: um erro de validação não mostra
  o valor).
- As marcas de contrato vêm **por tipo**, de um marcador próprio que acrescenta a chave ao
  esquema gerado sem substituí-lo: `x-identity` (`IdentityName`, `IdentityRa`,
  `IdentityEmail`, os mesmos tipos no pedido de cadastro, na resposta da revelação e em cada
  linha do pedido de `students.import`), `x-secret`
  (`SecretText` na criação de token; `BotTokenText` no pedido de `bots.register` e `TeamLinkText`
  na resposta de `team.link`) e `x-free-text` (`FreeText`: rótulo, justificativa,
  erro; `CourseText`, a disciplina e o período do oferecimento).
- Os modelos de **pedido** documentam o corpo no OpenAPI; a validação da entrada é feita à mão
  (`cq_server.api.validation`), para seguir a precedência do envelope de erro sem ecoar valores.
- Os de **resposta** são os DTOs que as rotas constroem explicitamente; o FastAPI os valida de novo
  contra o `response_model` (falha fechada).
- Horários saem como texto RFC 3339 em UTC com `Z`, na forma canônica (`timestamp`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, GetJsonSchemaHandler, TypeAdapter, model_validator
from pydantic.json_schema import JsonSchemaValue, SkipJsonSchema
from pydantic_core import CoreSchema

from cq_server.doctor import BACKUP_AGE_TEMPLATE, DOCTOR_REASON_MESSAGES, ServerCheckId, ServerCheckResult
from cq_server.permissions import Permission


@dataclass(frozen=True)
class SchemaMark:
    """Acrescenta `key: value` ao esquema JSON do tipo marcado."""

    key: str
    value: Any

    def __get_pydantic_json_schema__(self, core_schema: CoreSchema,
                                     handler: GetJsonSchemaHandler) -> JsonSchemaValue:
        schema = handler(core_schema)
        schema[self.key] = self.value
        return schema


# --- formatos ------------------------------------------------------------------------------------

_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_ALIAS = r"(?=[0-9a-f]*[a-f])[0-9a-f]{4,32}"

PseudoId = Annotated[str, Field(pattern=rf"^{_UUID4}$")]
Alias = Annotated[str, Field(pattern=rf"^{_ALIAS}$")]
Pseudonym = Annotated[str, Field(pattern=rf"^(?:{_ALIAS}|{_UUID4})$")]
TokenId = Annotated[str, Field(pattern=rf"^{_UUID4}$")]
ActorTokenId = Annotated[str, Field(pattern=rf"^(?:bootstrap|{_UUID4})$")]
Timestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")]
SchemaVersion = Annotated[str, Field(pattern=r"^[0-9]{4}$")]
Cursor = Annotated[int, Field(ge=0)]
ClientKind = Literal["cli", "mcp", "other"]

# --- marcas --------------------------------------------------------------------------------------

IdentityName = Annotated[str, SchemaMark("x-identity", "name")]
IdentityRa = Annotated[str, SchemaMark("x-identity", "ra")]
IdentityEmail = Annotated[str, SchemaMark("x-identity", "email")]
SecretText = Annotated[str, Field(pattern=r"^cq_[A-Za-z0-9_-]{43}$"), SchemaMark("x-secret", True)]
FreeText = Annotated[str, SchemaMark("x-free-text", True)]

# --- oferecimentos, bots e equipe ---------------------------------------------------------------

OfferingId = Annotated[str, Field(pattern=rf"^{_UUID4}$")]
OfferingCode = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")]
OfferingState = Literal["draft", "active", "closed", "archived"]
BotState = Literal["registered", "no_access"]
ProfileState = Literal["pending", "configured", "failed"]
Username = Annotated[str, Field(pattern=r"^[A-Za-z0-9_]{5,32}$")]
TelegramBotId = Annotated[int, Field(ge=1)]
TimezoneName = Annotated[str, Field(min_length=1, max_length=64)]
ResponseWindow = Annotated[int, Field(ge=5, le=43200)]
# Disciplina e período na resposta: texto livre redigido, que pode passar dos limites da entrada.
CourseText = Annotated[str, Field(min_length=1), SchemaMark("x-free-text", True)]
ConfirmationCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{22}$")]
BotTokenText = Annotated[str, Field(min_length=1, max_length=80,
                                    pattern=r"^[1-9][0-9]{0,15}:[A-Za-z0-9_-]{30,}$"),
                         SchemaMark("x-secret", True)]
TeamLinkText = Annotated[str, Field(pattern=r"^https://t\.me/[A-Za-z0-9_]{5,32}\?start=[A-Za-z0-9_-]{22}$"),
                         SchemaMark("x-secret", True)]

# --- alunos e convites ---------------------------------------------------------------------------

InviteOrigin = Literal["batch", "coordination", "self_service"]
Count = Annotated[int, Field(ge=0)]
ValidHours = Annotated[int, Field(ge=1, le=720)]


def timestamp(value: datetime) -> str:
    """Horário RFC 3339 em UTC com `Z`: sem fração quando os microssegundos são zero."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("horário sem fuso")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, regex_engine="python-re")


# --- saúde ---------------------------------------------------------------------------------------


class HealthStatus(StrictModel):
    """Saúde do sistema: 200 com `status: ok`; 503 com `status: error`."""

    status: Literal["ok", "error"]
    version: str
    database: Literal["ok", "unreachable"]
    schema_version: SchemaVersion | None
    expected_schema_version: SchemaVersion


# --- pessoas -------------------------------------------------------------------------------------


class PersonRegistration(StrictModel):
    """Pedido de `people.register`."""

    name: IdentityName = Field(description="Nome completo; espaços colapsados; 1 a 200 caracteres.")
    ra: IdentityRa | None = Field(default=None, description="Só dígitos, de 1 a 10.")
    email: IdentityEmail | None = Field(default=None, description="E-mail, até 254 caracteres.")


class Person(StrictModel):
    """Pessoa por pseudônimo (sem identidade)."""

    pseudo_id: PseudoId
    alias: Alias
    created_at: Timestamp


class PeoplePage(StrictModel):
    items: list[Person]
    next_cursor: Cursor


class RevealRequest(StrictModel):
    """Pedido de `identity.reveal`."""

    pseudonyms: list[Pseudonym] = Field(min_length=1, max_length=100)
    reason: str = Field(description="Justificativa; sem espaços nas pontas, 1 a 500 caracteres.")


class RevealedPerson(StrictModel):
    pseudo_id: PseudoId
    alias: Alias
    name: IdentityName
    ra: IdentityRa | None
    email: IdentityEmail | None


class Revelation(StrictModel):
    people: list[RevealedPerson]
    not_found: list[Pseudonym]


# --- tokens --------------------------------------------------------------------------------------


class TokenCreation(StrictModel):
    """Pedido de `tokens.create`."""

    label: str = Field(description="Rótulo; sem espaços nas pontas, 1 a 100 caracteres.")
    permissions: list[Permission] = Field(min_length=1, max_length=len(Permission))


class Token(StrictModel):
    token_id: TokenId
    label: FreeText
    permissions: list[Permission]
    created_at: Timestamp
    revoked_at: Timestamp | None


class CreatedToken(StrictModel):
    token_id: TokenId
    label: FreeText
    permissions: list[Permission]
    created_at: Timestamp
    revoked_at: None
    secret: SecretText


class TokenList(StrictModel):
    items: list[Token]


# --- oferecimentos ------------------------------------------------------------------------------


class OfferingCreation(StrictModel):
    """Pedido de `offerings.create`."""

    code: Annotated[str, Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]{2,39}$",
                               description="Código escolhido pelo operador; sem o formato de UUID e "
                                           "sem identidade de pessoa cadastrada.")]
    course: Annotated[str, Field(min_length=1, max_length=200,
                                 description="Disciplina; sem espaços nas pontas.")]
    term: Annotated[str, Field(min_length=1, max_length=40, description="Período; sem espaços nas pontas.")]
    timezone: Annotated[str, Field(min_length=1, max_length=64, description="Fuso da base IANA.")]
    response_window_minutes: ResponseWindow = Field(default=1440,
                                                    description="Janela de resposta, em minutos.")
    # A configuração de mensagens livres e o contato do professor.
    free_messages: bool = Field(default=False, description="Mensagens livres ligadas; ausente: desligadas.")
    contact_email: IdentityEmail | None = Field(
        default=None, description="E-mail do contato do professor, até 254 caracteres; ausente ou `null`: "
                                  "sem contato. Nunca é devolvido.")


class OfferingSettingsUpdate(StrictModel):
    """Pedido de `offerings.update_settings`: fechado, com pelo menos um
    dos dois campos."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    free_messages: bool | SkipJsonSchema[None] = Field(
        default=None, description="Mensagens livres ligadas ou desligadas; ausente: não muda.")
    contact_email: IdentityEmail | None = Field(
        default=None, description="E-mail do contato do professor (define ou troca); `null`: remove; "
                                  "ausente: não muda. Nunca é devolvido.")


class BotSummary(StrictModel):
    """Resumo do bot no `Offering`."""

    username: Username
    telegram_bot_id: TelegramBotId
    state: BotState
    profile_state: ProfileState


class Offering(StrictModel):
    """`Offering`."""

    offering_id: OfferingId
    code: OfferingCode
    course: CourseText
    term: CourseText
    timezone: TimezoneName
    response_window_minutes: ResponseWindow
    state: OfferingState
    created_at: Timestamp
    activated_at: Timestamp | None
    closed_at: Timestamp | None
    archived_at: Timestamp | None
    bot: BotSummary | None
    # Só os booleanos; o e-mail do contato nunca sai.
    free_messages: bool
    contact_email_set: bool
    # O horário do expurgo; `null` = não expurgado.
    purged_at: Timestamp | None


class OfferingsPage(StrictModel):
    items: list[Offering]
    next_cursor: Cursor


class ArchivePreparation(StrictModel):
    """Resposta de `offerings.prepare_archive`."""

    offering: Offering
    team_members_active: Annotated[int, Field(ge=0)]
    pending_links: Annotated[int, Field(ge=0)]
    confirmation_code: ConfirmationCode
    expires_at: Timestamp


class ArchiveConfirmation(StrictModel):
    """Pedido de `offerings.archive`."""

    confirmation_code: Annotated[str, Field(min_length=1, max_length=64,
                                            description="Código devolvido pela preparação.")]


# --- bots ---------------------------------------------------------------------------------------


class BotRegistration(StrictModel):
    """Pedido de `bots.register`: o token só no corpo, nunca ecoado."""

    token: BotTokenText


class OfferingRef(StrictModel):
    offering_id: OfferingId
    code: OfferingCode


# --- doctor, parte da API ------------------------------------------------------------------------------
# O `id` e o `result` vêm dos tipos do núcleo: os 9 identificadores do servidor e os 3 resultados que ele
# devolve (`not_verified` e `not_applicable` são só do `doctor.output` do CLI). A mensagem é texto fixo do
# servidor, sem `x-free-text`: por `(id, result)`, de `DOCTOR_MESSAGES`; as de `backup.age`, `smtp.server`
# e `retention.overdue` e a da falha de `bots.master_key` vêm de `DOCTOR_REASON_MESSAGES`, por
# `(id, result, motivo)`.
# As regras por id do `offering` e do `count` ficam no
# validador do `DoctorCheckOut`, as mesmas do validador do cliente (`_offering_and_count_ok` do `cq_client.doctor`).

# As mensagens fixas de `backup.age` sem registro legível (motivos `not_configured` e `unreadable`),
# por `(result, message)`; só com elas o `count` desse id é nulo.
_BACKUP_AGE_FIXED_MESSAGES: frozenset[tuple[str, str]] = frozenset(
    (result, message) for (check_id, result, reason), message in DOCTOR_REASON_MESSAGES.items()
    if check_id == "backup.age" and reason != "age")
# Texto fixo do erro: nunca ecoa valor (nem o `id`, nem o código, nem a contagem).
_DOCTOR_CHECK_FIELDS_ERROR = "offering ou count fora das regras do id da verificação"


def _doctor_offering_and_count_ok(check_id: str, result: str, message: str, offering: str | None,
                                  count: int | None) -> bool:
    """`offering` e `count` no lugar:
    - `bot.access`, `bot.poll`, `bot.lease`: `offering` obrigatório (o código), `count` nulo;
    - `tasks.overdue`: `offering` nulo; `count` obrigatório, 0 com "ok" e positivo com os demais;
    - `backup.age`: `offering` nulo; `count` (as horas, piso) só com o registro legível, isto é, com a mensagem
      igual a `BACKUP_AGE_TEMPLATE.format(hours=count)`; nulo só com as mensagens fixas de "não configurado" e
      "ilegível" do mesmo resultado;
    - `retention.overdue`: "ok" com `count` 0 e `offering` nulo; os demais com `count ≥ 1` e o código do mais antigo;
    - `bots.master_key`, `clock.skew`, `smtp.server`: `offering` e `count` nulos.
    O padrão do código e o `count ≥ 0` já vêm dos tipos (`OfferingCode`, `Count`)."""
    if check_id.startswith("bot."):
        return offering is not None and count is None
    if check_id == "retention.overdue":
        if result == "ok":
            return count == 0 and offering is None
        return count is not None and count >= 1 and offering is not None
    if offering is not None:
        return False
    if check_id == "tasks.overdue":
        return count is not None and (count == 0) == (result == "ok")
    if check_id == "backup.age":
        if count is None:
            return (result, message) in _BACKUP_AGE_FIXED_MESSAGES
        return message == BACKUP_AGE_TEMPLATE.format(hours=count)
    return count is None


class DoctorCheckOut(StrictModel):
    """Uma verificação do servidor: `id` entre os 9 do `ServerCheckId` (as 6
    globais `bots.master_key`, `tasks.overdue`, `clock.skew`, `backup.age`, `smtp.server`, `retention.overdue` e as
    3 por oferecimento `bot.access`, `bot.poll`, `bot.lease`), `result` entre `ok`, `warning` e `failure`, a
    `message` fixa do servidor e, conforme o id, `offering` (o código; em `bot.*` e no "aviso" de
    `retention.overdue`) e `count` (em `tasks.overdue`, `backup.age` e `retention.overdue`).

    O validador aplica as regras por id (`_doctor_offering_and_count_ok`); um desvio levanta erro de validação com
    texto fixo, sem eco de valor (`hide_input_in_errors` do `StrictModel`). Não muda o esquema JSON."""

    id: ServerCheckId
    result: ServerCheckResult
    message: Annotated[str, Field(min_length=1)]
    offering: OfferingCode | None
    count: Count | None

    @model_validator(mode="after")
    def _offering_and_count_by_id(self) -> DoctorCheckOut:
        if not _doctor_offering_and_count_ok(self.id, self.result, self.message, self.offering, self.count):
            raise ValueError(_DOCTOR_CHECK_FIELDS_ERROR)
        return self


class SystemDoctor(StrictModel):
    """Resposta de `system.doctor`: o horário da API e as verificações na ordem fixa."""

    checked_at: Timestamp
    checks: list[DoctorCheckOut]


# --- expurgo ------------------------------------------------------------------------------------------
# Só contagens, estados e horários: nenhum pseudônimo, nome ou endereço.


class PurgeImpactOut(StrictModel):
    """O impacto mostrado na preparação do expurgo: só contagens."""

    identities_to_delete: Count
    identities_kept: Count
    endpoints: Count
    updates: Count
    contact_email_set: bool


class PurgePreparationOut(StrictModel):
    """Resposta de `offerings.prepare_purge`: o código com o padrão do arquivamento."""

    offering: OfferingRef
    impact: PurgeImpactOut
    confirmation_code: ConfirmationCode
    expires_at: Timestamp


# O pedido da confirmação do expurgo é o mesmo do arquivamento.
PurgeConfirmation = ArchiveConfirmation


class PurgeOutcomeOut(StrictModel):
    """O resultado gravado da confirmação do expurgo."""

    identities_deleted: Count
    identities_kept: Count
    endpoints: Count
    updates: Count
    free_messages_redacted: Count
    outbox_redacted: Count
    contact_email_deleted: bool


class PurgeResultOut(StrictModel):
    """Resposta de `offerings.purge`; igual na confirmação repetida."""

    offering: OfferingRef
    purged_at: Timestamp
    result: PurgeOutcomeOut


class Lease(StrictModel):
    worker_id: Annotated[str, Field(min_length=1)]
    until: Timestamp


class BotProblem(StrictModel):
    """Problema visível do bot, com a mensagem fixa do código."""

    code: Literal["token_rejected", "token_unreadable", "telegram_unavailable", "rate_limited",
                  "concurrent_consumer", "webhook_active"]
    message: Literal[
        "O Telegram recusou o token do bot. Gere um token novo no BotFather e registre-o.",
        "O token guardado não pode ser lido com a chave-mestra atual. Registre o token de novo.",
        "O Telegram não respondeu; o sistema vai tentar de novo.",
        "O Telegram pediu uma pausa; o sistema vai tentar de novo depois do tempo indicado.",
        "Outro programa está consultando este bot. Pare o outro programa.",
        "Havia um webhook configurado neste bot; ele foi removido.",
    ]


class Bot(StrictModel):
    """`Bot`."""

    offering: OfferingRef
    username: Username
    telegram_bot_id: TelegramBotId
    state: BotState
    profile_state: ProfileState
    registered_at: Timestamp
    token_replaced_at: Timestamp | None
    lease: Lease | None
    last_poll_ok_at: Timestamp | None
    problem: BotProblem | None


# --- equipe -------------------------------------------------------------------------------------


class TeamLinkRequest(StrictModel):
    """Pedido de `team.link`."""

    person: Pseudonym
    valid_hours: Annotated[int, Field(ge=1, le=720)] = Field(default=168,
                                                             description="Validade do link, em horas.")


class TeamLinkPerson(StrictModel):
    pseudo_id: PseudoId
    alias: Alias


class TeamLinkOffering(StrictModel):
    offering_id: OfferingId
    code: OfferingCode
    timezone: TimezoneName


class TeamLink(StrictModel):
    """Resposta de `team.link`: o link é credencial ao portador."""

    link: TeamLinkText
    expires_at: Timestamp
    person: TeamLinkPerson
    offering: TeamLinkOffering


# --- alunos e convites ---------------------------------------------------------------------------
# As respostas não têm texto livre nem marca: o oferecimento sai como `OfferingRef`;
# nenhuma liga linha da lista a pseudônimo.


class RosterItem(StrictModel):
    """Uma linha da lista no pedido de `students.import`: os
    três campos obrigatórios, com as regras e as marcas `x-identity` do cadastro de pessoas."""

    name: IdentityName = Field(description="Nome completo; espaços colapsados; 1 a 200 caracteres.")
    ra: IdentityRa = Field(description="Só dígitos, de 1 a 10.")
    email: IdentityEmail = Field(description="E-mail, até 254 caracteres.")


class StudentsImportRequest(StrictModel):
    """Pedido de `students.import`: as linhas lidas no cliente (a API nunca lê
    um caminho de arquivo); os erros por item saem em `rows[i]`, um motivo por item."""

    rows: list[RosterItem] = Field(min_length=1, max_length=1000)


class StudentsImport(StrictModel):
    """Resposta de `students.import`: só contagens."""

    offering: OfferingRef
    received: Count
    people_created: Count
    people_reused: Count
    enrollments_created: Count
    already_enrolled: Count


class StudentsSummary(StrictModel):
    """Resposta de `students.summary`: `enrolled` é a soma
    dos outros sete."""

    offering: OfferingRef
    enrolled: Count
    no_invite: Count
    sending: Count
    invite_valid: Count
    terms_presented: Count
    invite_lapsed: Count
    send_failed: Count
    linked: Count


class InvitesPrepareRequest(StrictModel):
    """Pedido de `invites.prepare_send`: objeto obrigatório, que pode ser `{}`."""

    valid_hours: ValidHours = Field(default=168, description="Validade dos convites do lote, em horas.")


class InvitesPreparation(StrictModel):
    """Resposta de `invites.prepare_send`: o código de confirmação não é
    segredo (nonce da confirmação em duas fases, como o do arquivamento)."""

    offering: OfferingRef
    eligible: Count
    valid_hours: ValidHours
    confirmation_code: ConfirmationCode
    expires_at: Timestamp


class InvitesSendRequest(StrictModel):
    """Pedido de `invites.send`, como o de `offerings.archive`."""

    confirmation_code: Annotated[str, Field(min_length=1, max_length=64,
                                            description="Código devolvido pelo preparo.")]


class InvitesQueued(StrictModel):
    """Resposta de `invites.send`."""

    offering: OfferingRef
    queued: Count
    valid_hours: ValidHours


class InvitesResendRequest(StrictModel):
    """Pedido de `invites.resend`, com as regras do `identity.reveal`:
    o limite vale para a lista enviada, com os repetidos."""

    pseudonyms: list[Pseudonym] = Field(min_length=1, max_length=100)
    valid_hours: ValidHours = Field(default=168, description="Validade dos convites, em horas.")


class NotStudent(StrictModel):
    """Pessoa citada no reenvio sem matrícula de aluno no oferecimento (pseudônimo canônico)."""

    pseudo_id: PseudoId
    alias: Alias


class InvitesResend(StrictModel):
    """Resposta de `invites.resend`: os itens sem pessoa só contados, nunca
    ecoados."""

    offering: OfferingRef
    queued: Count
    valid_hours: ValidHours
    not_students: list[NotStudent]
    not_found_count: Count


# --- eventos -------------------------------------------------------------------------------------


class PersonRegisteredData(StrictModel):
    pseudo_id: PseudoId
    alias: Alias


class TokenCreatedData(StrictModel):
    token_id: TokenId
    label: FreeText
    permissions: list[Permission]


class TokenRevokedData(StrictModel):
    token_id: TokenId


class IdentityRevealedData(StrictModel):
    client: ClientKind
    revealed: list[PseudoId]
    not_found_count: Annotated[int, Field(ge=0)]
    reason: FreeText


class IdentityRevealDeniedData(StrictModel):
    client: ClientKind
    requested: list[PseudoId]
    not_found_count: Annotated[int, Field(ge=0)]


class TaskFailedData(StrictModel):
    task_id: Annotated[int, Field(ge=1)]
    task_type: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.]{0,99}$")]
    attempts: Annotated[int, Field(ge=0)]
    error: FreeText


class OutboxFailedData(StrictModel):
    message_id: Annotated[int, Field(ge=1)]
    recipient: PseudoId
    attempts: Annotated[int, Field(ge=0)]
    error: FreeText


# Eventos de oferecimentos, bots e equipe


class OfferingCreatedData(StrictModel):
    offering_id: OfferingId
    code: OfferingCode


class OfferingStateChangedData(StrictModel):
    offering_id: OfferingId
    from_state: OfferingState
    to_state: OfferingState


class OfferingArchivePreparedData(StrictModel):
    offering_id: OfferingId
    expires_at: Timestamp


class BotRegisteredData(StrictModel):
    offering_id: OfferingId
    telegram_bot_id: TelegramBotId
    username: Username


class BotTokenReplacedData(StrictModel):
    offering_id: OfferingId
    telegram_bot_id: TelegramBotId
    token_version: Annotated[int, Field(ge=2)]


class BotProfileUpdatedData(StrictModel):
    offering_id: OfferingId
    profile_state: Literal["configured", "failed"]
    purpose: Literal["active", "archived"]


class BotLeaseChangedData(StrictModel):
    offering_id: OfferingId
    worker_id: Annotated[str, Field(min_length=1)]
    change: Literal["acquired", "released"]
    reason: Literal["acquired", "not_eligible", "shutdown", "archived", "no_access"]


class BotAccessChangedData(StrictModel):
    offering_id: OfferingId
    access: Literal["lost", "restored"]
    problem: Literal["token_rejected", "token_unreadable"] | None


class TeamLinkCreatedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    expires_at: Timestamp


class TeamLinkRevokedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    revoke_reason: Literal["replaced", "archived", "refused"]


class TeamJoinedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    endpoint_replaced: bool


class TeamJoinRefusedData(StrictModel):
    offering_id: OfferingId
    refusal: Literal["invalid", "other_person"]


class EndpointBlockedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    channel: Literal["telegram"]


class OutboxReplyFailedData(StrictModel):
    message_id: Annotated[int, Field(ge=1)]
    reply_to: Annotated[int, Field(ge=1)]
    attempts: Annotated[int, Field(ge=0)]
    error: FreeText


# Eventos da lista de alunos e dos convites: sem texto livre, identidade, código, chat
# nem e-mail; `person` é o pseudônimo.


class StudentListImportedData(StrictModel):
    offering_id: OfferingId
    received: Count
    people_created: Count
    people_reused: Count
    enrollments_created: Count
    already_enrolled: Count


class StudentInvitesPreparedData(StrictModel):
    offering_id: OfferingId
    eligible: Count
    valid_hours: ValidHours
    expires_at: Timestamp


class StudentInvitesQueuedData(StrictModel):
    offering_id: OfferingId
    origin: Literal["batch", "coordination"]
    queued: Count


class StudentInviteSentData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    origin: InviteOrigin
    expires_at: Timestamp


class StudentInviteFailedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    origin: InviteOrigin
    failure: Literal["recipient_refused", "auth_refused", "server_refused", "not_configured",
                     "attempts_exhausted"]


class StudentInviteCancelledData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    origin: InviteOrigin
    cancel_reason: Literal["offering_inactive", "already_linked", "superseded"]


class StudentInviteRevokedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    origin: InviteOrigin
    revoke_reason: Literal["replaced", "archived", "refused"]


class StudentJoinedData(StrictModel):
    offering_id: OfferingId
    person: PseudoId
    terms_version: Annotated[int, Field(ge=1)]
    endpoint_replaced: bool


# Eventos dos disparos: sem texto livre, identidade, texto digitado, URL
# do repositório nem detalhe técnico; `person` é o pseudônimo, e os `*_id` de domínio, os `uuid`.

EventUuid = Annotated[str, Field(pattern=rf"^{_UUID4}$")]
DispatchOrigin = Literal["cli", "api", "mcp", "integration"]
# `no_teacher_contact` por último.
DispatchWarning = Literal["console_output", "missing_media", "media_error", "empty_target",
                          "too_many_options", "repeated_question", "no_teacher_contact"]
RejectionReason = Literal["repository_unreachable", "repository_refused", "commit_not_found",
                          "question_not_found", "question_unsupported", "question_broken",
                          "engine_unavailable", "channel_limits_exceeded", "internal_failure"]
DispatchCancelReason = Literal["operator", "expired", "offering_closed", "window_missed",
                               "validation_overran"]
UndeliveredReason = Literal["endpoint_blocked", "channel_refused", "engine_unavailable",
                            "bot_no_access", "channel_unavailable"]
QuestionFailure = Literal["variant_broken", "variant_unsupported", "channel_limits_exceeded",
                          "grading_broken", "engine_rejected"]
ChoiceHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


class DispatchPreparedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    question_ref_id: EventUuid
    origin: DispatchOrigin


class DispatchValidatedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    target_count: Count
    warnings: list[DispatchWarning]


class DispatchRejectedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    rejection_reason: RejectionReason


class DispatchConfirmedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    target_count: Count
    opens_at: Timestamp
    closes_at: Timestamp


class DispatchCancelledData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    cancel_reason: DispatchCancelReason


class DispatchOpenedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    target_count: Count


class DeliveryCounts(StrictModel):
    """As entregas do disparo por estado, no fechamento (as 8 chaves)."""

    pending: Count
    delivered: Count
    not_delivered: Count
    answered: Count
    dont_know: Count
    no_answer: Count
    answered_late: Count
    question_failed: Count


class DispatchClosedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    trigger: Literal["window", "archive"]
    counts: DeliveryCounts


class DeliveryDeliveredData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    person: PseudoId


class DeliveryNotDeliveredData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    person: PseudoId
    undelivered_reason: UndeliveredReason


class DeliveryQuestionFailedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    person: PseudoId
    failure: QuestionFailure


class DeliveryDontKnowData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    submission_id: EventUuid
    person: PseudoId


class GradedPart(StrictModel):
    """Uma parte da correção, por índice do campo: as opções escolhidas pelo `content_hash`, sem o
    texto; `score` sem teto."""

    index: Annotated[int, Field(ge=0)]
    score: float
    choices: list[ChoiceHash]
    ratio: float | None
    residual: float | None


class SubmissionGradedData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    submission_id: EventUuid
    question_ref_id: EventUuid
    person: PseudoId
    valid: Literal[True]
    late: bool
    normalized: bool
    score: float
    parts: list[GradedPart]


class SubmissionInternalFailureData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    delivery_id: EventUuid
    submission_id: EventUuid
    person: PseudoId
    engine_error: Literal["invalid_request", "payload_too_large"]


# A configuração de mensagens livres alterada, sem o e-mail.
class OfferingSettingsChangedData(StrictModel):
    offering_id: OfferingId
    free_messages: bool
    contact_email_set: bool


# Eventos dos relatórios e avisos: sem pseudônimo, identidade nem texto livre;
# sem chaves internas (os `*_id` são os `uuid`); `recipient_count`, nunca `recipient`. O assunto do
# aviso vai nos campos próprios de cada tipo, nulos fora dele.

NoticeType = Literal["dispatch_preview", "dispatch_rejected", "dispatch_opened", "dispatch_closed",
                     "summary_resent", "dispatch_cancelled", "question_failed", "bot_access_restored",
                     "invite_failures", "daily_digest"]
# Os estados finais do aviso (o `pending` nunca vai a evento).
NoticeFinalState = Literal["queued", "no_recipient", "no_activity", "missed", "not_sent", "gave_up"]
EventLocalDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
EventLocalTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class ReportFrozenData(StrictModel):
    offering_id: OfferingId
    dispatch_id: EventUuid
    version: Annotated[int, Field(ge=1)]
    trigger: Literal["closing", "resend"]


class NoticeRecordedData(StrictModel):
    offering_id: OfferingId
    notice_type: NoticeType
    dispatch_id: EventUuid | None
    report_version: Annotated[int, Field(ge=1)] | None
    episode_seq: Annotated[int, Field(ge=1)] | None
    batch_id: EventUuid | None
    window_started_at: Timestamp | None
    local_date: EventLocalDate | None
    state: NoticeFinalState
    recipient_count: Count


class OfferingDigestChangedData(StrictModel):
    offering_id: OfferingId
    enabled: bool
    local_time: EventLocalTime


# Eventos do expurgo: só contagens e booleanos; sem pseudônimo, identidade
# nem o código de confirmação.
class OfferingPurgePreparedData(StrictModel):
    offering_id: OfferingId
    expires_at: Timestamp
    identities_to_delete: Count
    identities_kept: Count
    endpoints: Count
    updates: Count
    contact_email_set: bool


class OfferingPurgedData(StrictModel):
    offering_id: OfferingId
    identities_deleted: Count
    identities_kept: Count
    endpoints: Count
    updates: Count
    free_messages_redacted: Count
    outbox_redacted: Count
    contact_email_deleted: bool


class _EventBase(StrictModel):
    seq: Annotated[int, Field(ge=1)]
    occurred_at: Timestamp
    actor_token_id: ActorTokenId | None


class PersonRegisteredEvent(_EventBase):
    type: Literal["person.registered"]
    data: PersonRegisteredData


class TokenCreatedEvent(_EventBase):
    type: Literal["token.created"]
    data: TokenCreatedData


class TokenRevokedEvent(_EventBase):
    type: Literal["token.revoked"]
    data: TokenRevokedData


class IdentityRevealedEvent(_EventBase):
    type: Literal["identity.revealed"]
    data: IdentityRevealedData


class IdentityRevealDeniedEvent(_EventBase):
    type: Literal["identity.reveal_denied"]
    data: IdentityRevealDeniedData


class TaskFailedEvent(_EventBase):
    type: Literal["task.failed"]
    data: TaskFailedData


class OutboxFailedEvent(_EventBase):
    type: Literal["outbox.failed"]
    data: OutboxFailedData


class OfferingCreatedEvent(_EventBase):
    type: Literal["offering.created"]
    data: OfferingCreatedData


class OfferingStateChangedEvent(_EventBase):
    type: Literal["offering.state_changed"]
    data: OfferingStateChangedData


class OfferingArchivePreparedEvent(_EventBase):
    type: Literal["offering.archive_prepared"]
    data: OfferingArchivePreparedData


class BotRegisteredEvent(_EventBase):
    type: Literal["bot.registered"]
    data: BotRegisteredData


class BotTokenReplacedEvent(_EventBase):
    type: Literal["bot.token_replaced"]
    data: BotTokenReplacedData


class BotProfileUpdatedEvent(_EventBase):
    type: Literal["bot.profile_updated"]
    data: BotProfileUpdatedData


class BotLeaseChangedEvent(_EventBase):
    type: Literal["bot.lease_changed"]
    data: BotLeaseChangedData


class BotAccessChangedEvent(_EventBase):
    type: Literal["bot.access_changed"]
    data: BotAccessChangedData


class TeamLinkCreatedEvent(_EventBase):
    type: Literal["team.link_created"]
    data: TeamLinkCreatedData


class TeamLinkRevokedEvent(_EventBase):
    type: Literal["team.link_revoked"]
    data: TeamLinkRevokedData


class TeamJoinedEvent(_EventBase):
    type: Literal["team.joined"]
    data: TeamJoinedData


class TeamJoinRefusedEvent(_EventBase):
    type: Literal["team.join_refused"]
    data: TeamJoinRefusedData


class EndpointBlockedEvent(_EventBase):
    type: Literal["endpoint.blocked"]
    data: EndpointBlockedData


class OutboxReplyFailedEvent(_EventBase):
    type: Literal["outbox.reply_failed"]
    data: OutboxReplyFailedData


# Eventos da lista de alunos e dos convites


class StudentListImportedEvent(_EventBase):
    type: Literal["student.list_imported"]
    data: StudentListImportedData


class StudentInvitesPreparedEvent(_EventBase):
    type: Literal["student.invites_prepared"]
    data: StudentInvitesPreparedData


class StudentInvitesQueuedEvent(_EventBase):
    type: Literal["student.invites_queued"]
    data: StudentInvitesQueuedData


class StudentInviteSentEvent(_EventBase):
    type: Literal["student.invite_sent"]
    data: StudentInviteSentData


class StudentInviteFailedEvent(_EventBase):
    type: Literal["student.invite_failed"]
    data: StudentInviteFailedData


class StudentInviteCancelledEvent(_EventBase):
    type: Literal["student.invite_cancelled"]
    data: StudentInviteCancelledData


class StudentInviteRevokedEvent(_EventBase):
    type: Literal["student.invite_revoked"]
    data: StudentInviteRevokedData


class StudentJoinedEvent(_EventBase):
    type: Literal["student.joined"]
    data: StudentJoinedData


# Eventos dos disparos


class DispatchPreparedEvent(_EventBase):
    type: Literal["dispatch.prepared"]
    data: DispatchPreparedData


class DispatchValidatedEvent(_EventBase):
    type: Literal["dispatch.validated"]
    data: DispatchValidatedData


class DispatchRejectedEvent(_EventBase):
    type: Literal["dispatch.rejected"]
    data: DispatchRejectedData


class DispatchConfirmedEvent(_EventBase):
    type: Literal["dispatch.confirmed"]
    data: DispatchConfirmedData


class DispatchCancelledEvent(_EventBase):
    type: Literal["dispatch.cancelled"]
    data: DispatchCancelledData


class DispatchOpenedEvent(_EventBase):
    type: Literal["dispatch.opened"]
    data: DispatchOpenedData


class DispatchClosedEvent(_EventBase):
    type: Literal["dispatch.closed"]
    data: DispatchClosedData


class DeliveryDeliveredEvent(_EventBase):
    type: Literal["delivery.delivered"]
    data: DeliveryDeliveredData


class DeliveryNotDeliveredEvent(_EventBase):
    type: Literal["delivery.not_delivered"]
    data: DeliveryNotDeliveredData


class DeliveryQuestionFailedEvent(_EventBase):
    type: Literal["delivery.question_failed"]
    data: DeliveryQuestionFailedData


class DeliveryDontKnowEvent(_EventBase):
    type: Literal["delivery.dont_know"]
    data: DeliveryDontKnowData


class SubmissionGradedEvent(_EventBase):
    type: Literal["submission.graded"]
    data: SubmissionGradedData


class SubmissionInternalFailureEvent(_EventBase):
    type: Literal["submission.internal_failure"]
    data: SubmissionInternalFailureData


class OfferingSettingsChangedEvent(_EventBase):
    type: Literal["offering.settings_changed"]
    data: OfferingSettingsChangedData


# Eventos dos relatórios e avisos


class ReportFrozenEvent(_EventBase):
    type: Literal["report.frozen"]
    data: ReportFrozenData


class NoticeRecordedEvent(_EventBase):
    type: Literal["notice.recorded"]
    data: NoticeRecordedData


class OfferingDigestChangedEvent(_EventBase):
    type: Literal["offering.digest_changed"]
    data: OfferingDigestChangedData


class OfferingPurgePreparedEvent(_EventBase):
    type: Literal["offering.purge_prepared"]
    data: OfferingPurgePreparedData


class OfferingPurgedEvent(_EventBase):
    type: Literal["offering.purged"]
    data: OfferingPurgedData


Event = Annotated[
    Union[PersonRegisteredEvent, TokenCreatedEvent, TokenRevokedEvent, IdentityRevealedEvent,
          IdentityRevealDeniedEvent, TaskFailedEvent, OutboxFailedEvent,
          # Oferecimentos, bots e equipe
          OfferingCreatedEvent, OfferingStateChangedEvent, OfferingArchivePreparedEvent,
          BotRegisteredEvent, BotTokenReplacedEvent, BotProfileUpdatedEvent, BotLeaseChangedEvent,
          BotAccessChangedEvent, TeamLinkCreatedEvent, TeamLinkRevokedEvent, TeamJoinedEvent,
          TeamJoinRefusedEvent, EndpointBlockedEvent, OutboxReplyFailedEvent,
          # Lista de alunos e convites
          StudentListImportedEvent, StudentInvitesPreparedEvent, StudentInvitesQueuedEvent,
          StudentInviteSentEvent, StudentInviteFailedEvent, StudentInviteCancelledEvent,
          StudentInviteRevokedEvent, StudentJoinedEvent,
          # Disparos
          DispatchPreparedEvent, DispatchValidatedEvent, DispatchRejectedEvent,
          DispatchConfirmedEvent, DispatchCancelledEvent, DispatchOpenedEvent, DispatchClosedEvent,
          DeliveryDeliveredEvent, DeliveryNotDeliveredEvent, DeliveryQuestionFailedEvent,
          DeliveryDontKnowEvent, SubmissionGradedEvent, SubmissionInternalFailureEvent,
          # Configuração das mensagens livres
          OfferingSettingsChangedEvent,
          # Relatórios e avisos
          ReportFrozenEvent, NoticeRecordedEvent, OfferingDigestChangedEvent,
          # Expurgo
          OfferingPurgePreparedEvent, OfferingPurgedEvent],
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Event)


class EventsPage(StrictModel):
    items: list[Event]
    next_cursor: Cursor


# --- disparos e mensagens livres -----------------------------------------------------------------

# O conteúdo de terceiros e o texto do aluno são "não confiáveis"
# (`x-untrusted`, sempre com `x-free-text`); o `repo` e o `qid` são texto do operador (só `x-free-text`).
UntrustedText = Annotated[str, SchemaMark("x-free-text", True), SchemaMark("x-untrusted", True)]
OperatorText = Annotated[str, Field(min_length=1), SchemaMark("x-free-text", True)]
CommitSha = Annotated[str, Field(pattern=r"^([0-9a-f]{40}|[0-9a-f]{64})$")]
DispatchUuid = Annotated[str, Field(pattern=rf"^{_UUID4}$")]
DispatchState = Literal["validating", "rejected", "scheduled", "open", "closed", "cancelled"]
IrFieldType = Literal["number", "integer", "text", "single_choice", "multiple_choice"]
RejectionMessage = Literal[
    "Não foi possível obter o repositório da questão.",
    "O repositório foi recusado pela política de obtenção.",
    "O commit informado não existe no repositório.",
    "A questão não existe nesse commit.",
    "A questão usa recursos que o sistema não suporta.",
    "A questão falhou ao gerar uma variante.",
    "O motor de questões ficou indisponível durante a validação.",
    "A questão não cabe nos limites do canal.",
    "Falha interna na validação.",
]
# Horário do pedido: ISO 8601 com deslocamento explícito.
OffsetTimestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$")]


class DispatchPrepareRequest(StrictModel):
    """Pedido de `dispatches.prepare`: plano e fechado."""

    repo: Annotated[str, Field(min_length=1, max_length=2048,
                               description="URL `https` do repositório, num host permitido.")]
    commit: CommitSha = Field(description="SHA do commit (40 ou 64 dígitos hexadecimais minúsculos).")
    qid: Annotated[str, Field(min_length=1, max_length=512,
                              pattern=r"^(?!.*(^|/)\.{1,2}(/|$))[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$",
                              description="Caminho da questão no repositório.")]
    opens_at: OffsetTimestamp | SkipJsonSchema[None] = Field(
        default=None, description="Abertura (com deslocamento); ausente: imediato, na confirmação.")
    closes_at: OffsetTimestamp | SkipJsonSchema[None] = Field(
        default=None, description="Fechamento (com deslocamento); ausente: a janela do oferecimento.")
    origin: DispatchOrigin | SkipJsonSchema[None] = Field(
        default=None, description="Origem declarada; ausente: a do `X-CQ-Client`.")


class DispatchQuestion(StrictModel):
    """A questão do disparo (`repo` e `qid` redigidos na saída)."""

    question_ref_id: DispatchUuid
    repo: OperatorText
    commit: CommitSha
    qid: OperatorText


class DispatchRejection(StrictModel):
    """O motivo da rejeição: a classe, a mensagem pareada e os motivos do motor (redigidos)."""

    reason: RejectionReason
    message: RejectionMessage
    details: list[UntrustedText]


class DispatchDeliveries(StrictModel):
    """As contagens das entregas por estado e das correções pendentes (só contagens)."""

    pending: Count
    delivered: Count
    not_delivered: Count
    answered: Count
    dont_know: Count
    no_answer: Count
    answered_late: Count
    question_failed: Count
    grading_pending: Count


class PreviewField(StrictModel):
    type: IrFieldType
    label: UntrustedText | None
    suffix: UntrustedText | None
    hint: UntrustedText


class PreviewWarning(StrictModel):
    kind: DispatchWarning
    message: UntrustedText


class DispatchPreview(StrictModel):
    """A prévia: sem gabarito nem o nome do campo do PL."""

    title: UntrustedText | None
    statement: UntrustedText
    fields: list[PreviewField]
    target_count: Count
    warnings: list[PreviewWarning]


class DispatchSummary(StrictModel):
    """Um item de `dispatches.list`: o `Dispatch` sem `offering`, `timezone` e
    `preview`."""

    dispatch_id: DispatchUuid
    question: DispatchQuestion
    state: DispatchState
    confirmed: bool
    origin: DispatchOrigin
    opens_at: Timestamp | None
    closes_at: Timestamp | None
    created_at: Timestamp
    validated_at: Timestamp | None
    confirmed_at: Timestamp | None
    rejected_at: Timestamp | None
    opened_at: Timestamp | None
    closed_at: Timestamp | None
    cancelled_at: Timestamp | None
    rejection: DispatchRejection | None
    cancel_reason: DispatchCancelReason | None
    target_count: Count | None
    deliveries: DispatchDeliveries


class Dispatch(DispatchSummary):
    """O `Dispatch` completo."""

    offering: OfferingRef
    timezone: TimezoneName
    preview: DispatchPreview | None


class DispatchesPage(StrictModel):
    """Resposta de `dispatches.list`."""

    offering: OfferingRef
    timezone: TimezoneName
    items: list[DispatchSummary]
    next_cursor: Cursor


class FreeMessage(StrictModel):
    """Um item de `messages.list`: o texto redigido."""

    pseudo_id: PseudoId
    alias: Alias
    text: UntrustedText
    created_at: Timestamp


class FreeMessagesPage(StrictModel):
    """Resposta de `messages.list`."""

    offering: OfferingRef
    items: list[FreeMessage]
    next_cursor: Cursor


# --- relatório do disparo ------------------------------------------------------------------------

# Os tipos do relatório (todas as propriedades obrigatórias; anuláveis como `X | None`).
ContentHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
CommitShort = Annotated[str, Field(pattern=r"^[0-9a-f]{12}$")]
ReportTrigger = Literal["closing", "resend"]


class Pair(StrictModel):
    """Uma contagem em par: só no prazo e com as atrasadas."""

    on_time: Count
    with_late: Count


class RatePair(StrictModel):
    """Uma fração de 0 a 1 em par; `null` no denominador zero, em cada lado."""

    on_time: float | None
    with_late: float | None


class FrozenRef(StrictModel):
    """Uma versão congelada do relatório."""

    version: Annotated[int, Field(ge=1)]
    trigger: ReportTrigger
    frozen_at: Timestamp


class TopItem(StrictModel):
    value: UntrustedText
    count: Pair


class TopList(StrictModel):
    """Os mais comuns (no máximo 5, com pelo menos 2 ocorrências com as atrasadas) e as demais."""

    items: Annotated[list[TopItem], Field(max_length=5)]
    others: Pair


class ChoiceItem(StrictModel):
    """Uma alternativa escolhida (nenhuma marca de gabarito)."""

    content_hash: ContentHash
    text: UntrustedText
    count: Pair


class FieldDistribution(StrictModel):
    """A distribuição de um campo da IR."""

    index: Count
    type: IrFieldType
    label: UntrustedText | None
    unavailable: Literal["purged"] | None
    choices: list[ChoiceItem] | None
    values: TopList | None
    ratios: TopList | None
    unrated: Pair | None
    texts: TopList | None


class ReportDistribution(StrictModel):
    fields: list[FieldDistribution]


class FieldFriction(StrictModel):
    index: Count
    format_errors: Pair
    decimal_comma: Pair


class ReportFriction(StrictModel):
    fields: list[FieldFriction]
    unmapped_format_errors: Pair


class NotDeliveredByReason(StrictModel):
    endpoint_blocked: Count
    channel_refused: Count
    engine_unavailable: Count
    bot_no_access: Count
    channel_unavailable: Count


class QuestionFailedByStage(StrictModel):
    preparation: Count
    grading: Count


class ReportReach(StrictModel):
    """O alcance."""

    target: Count
    delivered: Count
    not_delivered: Count
    not_delivered_by_reason: NotDeliveredByReason
    question_failed: Count
    question_failed_by_stage: QuestionFailedByStage
    pending: Count


class ReportPerformance(StrictModel):
    """O desempenho."""

    answered: Pair
    correct: Pair
    partially_correct: Pair
    incorrect: Pair
    dont_know: Pair
    correct_rate: RatePair
    partially_correct_rate: RatePair
    incorrect_rate: RatePair
    response_rate: RatePair
    mean_score: RatePair


class ResponseTime(StrictModel):
    """O tempo até responder, só no prazo."""

    observations: Count
    negative: Count
    median_ms: float | None
    q1_ms: float | None
    q3_ms: float | None


class ReportQuestion(StrictModel):
    qid: OperatorText
    commit_short: CommitShort


class ReportDispatch(StrictModel):
    """`dispatch` do relatório: `partial` verdadeiro se e só se aberto."""

    dispatch_id: DispatchUuid
    question: ReportQuestion
    title: UntrustedText | None
    state: Literal["open", "closed"]
    partial: bool
    origin: DispatchOrigin
    opens_at: Timestamp
    closes_at: Timestamp


class Report(StrictModel):
    """O 200 `application/json` de `reports.get`, já redigido."""

    report_schema_version: Literal["1"]
    offering: OfferingRef
    timezone: TimezoneName
    dispatch: ReportDispatch
    computed_at: Timestamp
    frozen: FrozenRef | None
    grading_pending: Count
    reach: ReportReach
    performance: ReportPerformance
    response_time: ResponseTime
    distribution: ReportDistribution
    friction: ReportFriction


# O esquema da mídia `text/csv` do 200 de `reports.get` (marca declarativa).
REPORT_CSV_SCHEMA: dict[str, Any] = {"type": "string", "x-free-text": True, "x-untrusted": True}


class ReportVersionsPage(StrictModel):
    """Resposta de `reports.list_versions`: cursor = número da versão."""

    offering: OfferingRef
    dispatch_id: DispatchUuid
    items: list[FrozenRef]
    next_cursor: Cursor | None


class ReportResend(StrictModel):
    """Resposta de `reports.resend_summary`: a versão congelada agora, com
    `trigger = "resend"`."""

    offering: OfferingRef
    dispatch_id: DispatchUuid
    frozen: FrozenRef


# O horário local do resumo diário (`LocalTime`).
LocalTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class OfferingDigest(StrictModel):
    """Resposta de `offerings.get_digest` e `offerings.update_digest`: sem
    configuração gravada, desligado às 20:00."""

    offering: OfferingRef
    timezone: TimezoneName
    enabled: bool
    local_time: LocalTime


class OfferingDigestUpdate(StrictModel):
    """Pedido de `offerings.update_digest`: fechado, com pelo menos um dos dois
    campos."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    enabled: bool | SkipJsonSchema[None] = Field(default=None, description="Ligado ou desligado; ausente: não muda.")
    local_time: LocalTime | SkipJsonSchema[None] = Field(
        default=None, description="Horário local `HH:MM` (00:00 a 23:59) no fuso do oferecimento; ausente: não muda.")


# --- erros ---------------------------------------------------------------------------------------

ErrorCode = Literal["invalid_request", "unauthenticated", "forbidden", "not_found",
                    "method_not_allowed", "conflict", "internal",
                    # Oferecimentos, bots e equipe
                    "already_exists", "invalid_state", "bot_conflict", "bot_required",
                    "invalid_confirmation", "unavailable", "not_configured",
                    # Lista de alunos e papéis
                    "list_conflict", "role_conflict",
                    # Disparos
                    "invalid_dispatch_state", "window_expired", "grading_pending",
                    # Expurgo
                    "purge_pending", "purge_changed"]
Reason = Literal["missing", "unknown_field", "invalid_format", "too_short", "too_long", "too_few",
                 "too_many", "out_of_range", "invalid_value", "duplicate"]


class FieldError(StrictModel):
    field: Annotated[str, Field(pattern=r"^(body|query|X-CQ-Client|[a-z_]+(\[[0-9]+\])?)$")]
    reason: Reason


class PurgePendingOut(StrictModel):
    """O trabalho pendente que bloqueia o expurgo: só contagens."""

    outbox: Count
    updates: Count
    tasks: Count
    freeze: Count
    due: Count


class ErrorDetail(StrictModel):
    code: ErrorCode
    message: str
    permission: Permission | SkipJsonSchema[None] = Field(
        default=None, description="Só no 403: a permissão exigida.")
    state: OfferingState | SkipJsonSchema[None] = Field(
        default=None, description="Só no 409 invalid_state: o estado atual do oferecimento.")
    fields: list[FieldError] | SkipJsonSchema[None] = Field(
        default=None, min_length=1,
        description="Só em invalid_request, conflict, already_exists, bot_conflict, list_conflict e "
                    "role_conflict.")
    pending: PurgePendingOut | SkipJsonSchema[None] = Field(
        default=None, description="Só no 409 purge_pending: as contagens do trabalho pendente.")


class ErrorEnvelope(StrictModel):
    error: ErrorDetail


# Pedidos documentados no OpenAPI (validados à mão).
REQUEST_MODELS: tuple[type[StrictModel], ...] = (PersonRegistration, RevealRequest, TokenCreation,
                                                 # Oferecimentos, bots e equipe
                                                 OfferingCreation, ArchiveConfirmation,
                                                 BotRegistration, TeamLinkRequest,
                                                 # Lista de alunos e convites
                                                 StudentsImportRequest, InvitesPrepareRequest,
                                                 InvitesSendRequest, InvitesResendRequest,
                                                 # Disparos
                                                 DispatchPrepareRequest,
                                                 # Configuração das mensagens livres
                                                 OfferingSettingsUpdate,
                                                 # Resumo diário
                                                 OfferingDigestUpdate)
