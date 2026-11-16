"""CLI `cq`.

Um comando por operação do catálogo, com saída para pessoas ou, com a opção global `--json`, o corpo
JSON da resposta da API. O tratamento de erros e os códigos de saída ficam no próprio grupo `cli`
(`_RootGroup.main`), e não só em `main()`:

| Situação | Código |
| --- | --- |
| sucesso | 0 |
| a API recusou (4xx/5xx) ou `health` com `status = error` | 1 |
| uso incorreto | 2 |
| API inalcançável | 3 |

Com código diferente de 0, a saída padrão fica vazia (exceto o corpo do `health` com erro em
`--json`) e toda mensagem vai para o stderr, sem rastreamento de pilha. As mensagens de uso incorreto
nunca ecoam um valor digitado nem uma opção desconhecida (as mensagens padrão do Click ecoam).

O token vem só do perfil (`profile.resolve_profile`); nenhuma opção ou argumento o recebe. O segredo de
`tokens create` só aparece num terminal; fora dele, exige `--secret-file`.

O token do bot de `bots register` também não passa por opção nem argumento:
vem do arquivo de `--token-file` ou, com a entrada padrão num terminal, de um prompt mascarado. O link
de `team link` é uma credencial, com a mesma regra do segredo de `tokens create` (terminal ou
`--secret-file`). No 409 `invalid_state`, o texto para pessoas traz também a linha `Estado atual:`.

`students import` lê o arquivo da lista de `--file` com `cq_client.roster`,
depois do perfil e antes de chamar a API; arquivo fora do formato é uso incorreto (código 2), sem
nenhum pedido, sem caminho nem conteúdo. Nos erros da importação, cada item `rows[i]` sai no texto como
`  - linha <N>: <reason>`, com N a linha do arquivo em que o registro começa; com `--json`, o corpo da
API sai intacto.

Disparos e mensagens livres: os grupos `dispatches` (`prepare`, `show`, `list`, `confirm`, `cancel`) e
`messages` (`list`). O CLI não manda `origin` (vale o `X-CQ-Client: cli`) e passa os horários como
digitados (só com deslocamento; a API recusa os sem fuso). Todo texto de terceiros ou de aluno impresso
(prévia, motivos, `repo`, `qid`, mensagens livres) passa por `_terminal_safe`: os controles C0/C1
(inclusive o ESC das sequências ANSI) e as quebras de linha saem como escapes visíveis; o `--json` sai
como a API.

Perfis: a opção global `--profile NOME` (antes do
subcomando, como o `--json`; o valor nunca é ecoado) escolhe um perfil nomeado, resolvido por
`profile.resolve_profile` uma vez por invocação; o `ApiClient` usa o `as_profile()` dele. Erro de perfil
→ 2, `Uso incorreto: <motivo fixo>`. O aviso de `http://` fora do loopback (só perfil nomeado remoto) sai
no stderr, sem mudar o stdout nem o código. Sem perfil escolhido, vale o perfil implícito.

Os comandos locais `up`, `down`, `status` e `logs`, fora do catálogo,
sobre as rotinas de `cq_client.local_commands` e o `Stack` do `ctx.obj` (ou o `ComposeStack`); o `status` com
o código 1 imprime o relatório no stdout; o Ctrl+C sai com 130 só neles, no `doctor`, nas folhas do expurgo e na
pergunta de subir a pilha. Na API inalcançável de uma folha do catálogo, com o perfil local, sem `CQ_API_URL` e sem
`--json`, a dica de subir a pilha e, num terminal, a pergunta `[s/N]` (`_report_unreachable`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
import typing
import zoneinfo
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn

import click

from cq_client import doctor, local_commands, local_ops
from cq_client.api import ApiClient, ApiError, ApiUnreachable
from cq_client.hint import local_stack_hint
from cq_client.host_probe import DockerHostProbe, HostProbe
from cq_client.local_commands import (
    ASK_START_TEMPLATE,
    DOWN_DONE_TEMPLATE,
    EVENTS_TYPE_OR_PERSON,
    FOREIGN_API_TEMPLATE,
    INTERRUPTED_MESSAGE,
    LOGS_FOLLOW_JSON_REFUSED,
    LOGS_SERVICE_REFUSED,
    LOGS_TAIL,
    LOGS_TAIL_RANGE,
    PORT_MISMATCH_MESSAGE,
    RERUN_MESSAGE,
    STATUS_PROBLEM_SUMMARY,
    UP_DONE_TEMPLATE,
    UP_TIMEOUT_RANGE,
    UP_TIMEOUT_SECONDS,
    PortMismatch,
    StatusReport,
    UpSummary,
    UpTimeout,
    health_ok,
    service_line,
    up_timeout_message,
)
from cq_client.profile import (
    EffectiveProfile,
    Profile,
    ProfileError,
    insecure_transport_warning,
    resolve_profile,
)
from cq_client.roster import Roster, RosterFileError, read_roster, row_line
from cq_client.stack import (
    LOG_SERVICES,
    STACK_ENV_FILE,
    DockerUnavailable,
    Stack,
    StackEnvMissing,
    StackError,
    check_docker_target,
    locate_stack_dir,
)
from cq_client.stack_compose import ComposeStack, SubprocessRunner

_T = typing.TypeVar("_T")

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_INTERRUPTED = 130  # só nos comandos locais, no `doctor`, no expurgo e na pergunta de subir a pilha

HTTP_LOGGERS = ("httpx", "httpx2", "httpcore", "httpcore2")

# O nome da folha do servidor MCP (`mcp_command`, com o import preguiçoso de
# `cq_client.mcp_server`).
MCP_COMMAND = "mcp"
# Se o servidor MCP não carregar (SDK ausente ou quebrado), a folha escreve a
# linha fixa de falha interna do servidor (`mcp_server.MCP_STDERR_MESSAGES["internal"]`, repetida aqui porque o
# módulo que a define é o que falhou ao carregar; um teste confere a igualdade) e sai com 1.
MCP_LOAD_FAILURE_LINE = "Falha interna numa ferramenta; detalhes omitidos."

SECRET_NOT_SAVED_CODE = "secret_not_saved"
SECRET_NOT_SAVED_MESSAGE = "O segredo não pôde ser gravado; o token foi revogado."
UNREACHABLE_CODE = "api_unreachable"

# Token do bot, link da equipe e estado do oferecimento
BOT_TOKEN_PROMPT = "Token do bot"
LINK_NOT_SAVED_CODE = "link_not_saved"
LINK_NOT_SAVED_MESSAGE = "O link não pôde ser gravado; gere outro."
INVALID_STATE_CODE = "invalid_state"
PURGE_PENDING_CODE = "purge_pending"  # expurgo com pendências
ZONE_UNAVAILABLE = "fuso indisponível"


# --------------------------------------------------------------------------------------------------
# Pontos que os testes substituem


def stdout_is_terminal() -> bool:
    """Diz se a saída padrão é um terminal (os testes a substituem)."""
    stream = sys.stdout
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def stdin_is_terminal() -> bool:
    """Diz se a entrada padrão é um terminal (token do bot; os testes a substituem)."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def stderr_is_terminal() -> bool:
    """Diz se a saída de erro é um terminal (pergunta de subir a pilha local; os testes a
    substituem)."""
    stream = sys.stderr
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def ask_start_local_stack(project: str) -> bool:
    """Pergunta no stderr se deve subir a pilha local do projeto `project` agora, com "não" como
    padrão (os testes a substituem).

    Lê uma linha da entrada padrão: `s` ou `sim` (sem distinção de caixa) = sim; vazio, outra resposta ou
    fim da entrada = não; sem tempo-limite. O Ctrl+C (`KeyboardInterrupt`) sobe a quem chamou (código
    130). O `click.confirm` não serve: só aceita y/n e converte o fim da entrada e o Ctrl+C no mesmo
    `Abort` (código 1)."""
    click.echo(ASK_START_TEMPLATE.format(project=project), err=True, nl=False)
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stderr.flush()
    stream = sys.stdin
    try:
        line = stream.readline() if stream is not None else ""
    except (EOFError, ValueError, OSError):
        line = ""
    try:
        echoed = bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        echoed = False
    if not line or not echoed:
        # Sem o eco do terminal (fim da entrada, ou entrada que não é um terminal), a linha da pergunta
        # termina aqui, para que a próxima mensagem do stderr comece numa linha própria.
        click.echo("", err=True)
    return line.strip().lower() in ("s", "sim")


def prompt_secret(label: str) -> str:
    """Pede um segredo no terminal, mascarado e sem eco (token do bot; os testes a substituem).

    É o prompt oculto do Click: o texto digitado não aparece na tela nem em nenhuma saída.
    """
    return click.prompt(label, hide_input=True)


def write_secret_file(file: typing.TextIO, secret: str) -> None:
    """Escreve o segredo no arquivo do --secret-file (os testes a substituem).

    O arquivo já foi criado (exclusivo, `0600`) e aberto com `newline="\\n"`: grava `segredo + "\\n"`
    e força a gravação em disco.
    """
    file.write(secret + "\n")
    file.flush()
    os.fsync(file.fileno())


# --------------------------------------------------------------------------------------------------
# Saída e erros


class _Usage(click.UsageError):
    """Uso incorreto com motivo já em português, sem dados do usuário."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Done(Exception):
    """Encerra o comando com `code`; as mensagens já foram escritas."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _utf8_streams() -> None:
    """Põe stdout e stderr em UTF-8 quando não estão.

    No Windows, uma saída redirecionada (um agente rodando o `cq` pelo shell) usa a página de código
    do sistema, como a cp1252, e um caractere fora dela derrubaria o comando depois de a API já ter
    agido (uma revelação auditada sem saída). Um terminal e o `CliRunner` já estão em UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def _echo(text: str) -> None:
    click.echo(text)


def _echo_err(text: str) -> None:
    click.echo(text, err=True)


def _dumps(body: Any) -> str:
    return json.dumps(body, ensure_ascii=False)


def _declared_option(ctx: click.Context | None, name: str) -> click.Option | None:
    """A opção declarada no comando do contexto com esse nome longo, ou `None` (nunca confia no
    texto digitado: só cita um nome que o próprio comando declara)."""
    if ctx is None or not name.startswith("--"):
        return None
    for param in ctx.command.get_params(ctx):
        if isinstance(param, click.Option) and name in (*param.opts, *param.secondary_opts):
            return param
    return None


def _option_name(param: click.Parameter | None) -> str | None:
    """O nome longo declarado de uma opção, ou `None` (argumentos não são citados)."""
    if not isinstance(param, click.Option):
        return None
    return next((opt for opt in param.opts if opt.startswith("--")), None)


def _usage_reason(exc: click.UsageError) -> str:
    """Motivo em português, que cita no máximo o nome de uma opção declarada."""
    if isinstance(exc, _Usage):
        return exc.reason
    if isinstance(exc, click.NoSuchOption):
        return "opção desconhecida."
    if isinstance(exc, click.NoSuchCommand):
        return "comando desconhecido."
    if isinstance(exc, click.exceptions.NoArgsIsHelpError):
        return "falta o comando (veja --help)."
    if isinstance(exc, click.BadOptionUsage):
        option = _declared_option(exc.ctx, exc.option_name)
        if option is None:
            return "uso inválido de uma opção."
        name = _option_name(option)
        if option.is_flag:
            return f"a opção {name} não aceita valor."
        return f"falta o valor da opção {name}."
    if isinstance(exc, click.MissingParameter):
        name = _option_name(exc.param)
        if name is not None:
            return f"falta a opção {name}."
        return "falta um argumento obrigatório."
    if isinstance(exc, click.BadParameter):
        name = _option_name(exc.param)
        if name is not None:
            return f"valor inválido na opção {name}."
        return "valor inválido num argumento."
    if isinstance(exc, click.BadArgumentUsage):
        return "argumentos inválidos."
    return "comando ou argumentos inválidos."


def _quiet_http_loggers() -> None:
    """Põe os loggers do transporte em WARNING ou acima."""
    for name in HTTP_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


# --------------------------------------------------------------------------------------------------
# Classes do Click: erros de uso sem eco e códigos de saída no próprio grupo


@contextlib.contextmanager
def _usage_context(ctx: click.Context) -> typing.Iterator[None]:
    """Liga ao erro de uso o contexto do comando (o analisador do Click não liga), para que o motivo
    possa conferir se uma opção citada é mesmo declarada."""
    try:
        yield
    except click.UsageError as exc:
        if exc.ctx is None:
            exc.ctx = ctx
        raise


class _Command(click.Command):
    """Comando folha: argumento a mais é uso incorreto sem eco."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        ctx.allow_extra_args = True
        with _usage_context(ctx):
            rest = super().parse_args(ctx, args)
        if rest:
            raise _Usage("há argumentos a mais.")
        return rest


class _Group(click.Group):
    """Grupo: subcomando desconhecido é uso incorreto sem eco."""

    command_class = _Command
    group_class = type

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        with _usage_context(ctx):
            return super().parse_args(ctx, args)

    def resolve_command(self, ctx: click.Context,
                        args: list[str]) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.NoSuchCommand:
            raise _Usage("comando desconhecido.") from None


class _RootGroup(_Group):
    """Grupo raiz: converte toda saída em código e mensagem (os códigos `EXIT_*` acima)."""

    group_class = _Group

    def main(self, args: Any = None, prog_name: str | None = None, complete_var: str | None = None,
             standalone_mode: bool = True, **extra: Any) -> Any:
        _quiet_http_loggers()
        _utf8_streams()
        # Nomes, rótulos e motivos são texto livre: nada de expandir curingas no Windows.
        extra.setdefault("windows_expand_args", False)
        try:
            result = super().main(args=args, prog_name=prog_name, complete_var=complete_var,
                                  standalone_mode=False, **extra)
            code = result if isinstance(result, int) and not isinstance(result, bool) else EXIT_OK
        except _Done as done:
            code = done.code
        except click.UsageError as exc:
            _echo_err(f"Uso incorreto: {_usage_reason(exc)}")
            code = EXIT_USAGE
        except click.Abort:
            _echo_err("Erro: operação interrompida.")
            code = EXIT_API_ERROR
        except click.ClickException as exc:
            _echo_err(f"Erro: falha no CLI ({type(exc).__name__}).")
            code = exc.exit_code
        except Exception as exc:  # noqa: BLE001 - nenhuma saída tem rastreamento de pilha
            _echo_err(f"Erro: falha inesperada no CLI ({type(exc).__name__}).")
            code = EXIT_API_ERROR
        if standalone_mode:
            sys.exit(code)
        return code


# --------------------------------------------------------------------------------------------------
# Estado da invocação


@dataclass
class _Options:
    as_json: bool
    http: Any | None
    # O nome dado em `--profile` (nunca ecoado) e o perfil efetivo, resolvido uma vez por invocação.
    profile_name: str | None = None
    effective: EffectiveProfile | None = None
    # Os pontos injetáveis dos comandos locais, vindos do `ctx.obj`:
    # o `Stack` (nível 1), o `CommandRunner` do `ComposeStack` (nível 2), o relógio e o `sleep` da espera
    # do `up` e o diretório atual da descoberta da pilha. `None` = o real.
    stack: Stack | None = None
    runner: Any | None = None
    clock: Callable[[], float] | None = None
    sleep: Callable[[float], None] | None = None
    cwd: Path | None = None
    # A porta de leitura do host do `cq doctor` (`postgres.volume` e `disk.space`).
    host_probe: HostProbe | None = None


def _options() -> _Options:
    ctx = click.get_current_context()
    found = ctx.find_object(_Options)
    if found is None:  # pragma: no cover - o grupo raiz sempre cria
        return _Options(as_json=False, http=None)
    return found


def _effective_profile() -> EffectiveProfile:
    """O perfil efetivo: `--profile` > `CQ_PROFILE` > `default_profile` > implícito.

    Erro de perfil → uso incorreto (2) com o motivo fixo, sem eco. Na primeira resolução, o aviso de
    `http://` fora do loopback vai ao stderr, sem mudar o stdout nem o código; o nome do
    perfil vem do arquivo e passa por `_terminal_safe`."""
    options = _options()
    if options.effective is None:
        try:
            effective = resolve_profile(os.environ, options.profile_name)
        except ProfileError as exc:
            raise _Usage(exc.reason) from None
        warning = insecure_transport_warning(effective)
        if warning is not None:
            _echo_err(_terminal_safe(warning))
        options.effective = effective
    return options.effective


def _profile() -> Profile:
    """O perfil do `ApiClient` (endereço e token), pelo perfil efetivo, explícito ou implícito."""
    return _effective_profile().as_profile()


@contextlib.contextmanager
def _api(profile: Profile) -> typing.Iterator[ApiClient]:
    with ApiClient(profile.api_url, profile.token, client_kind="cli", http=_options().http) as api:
        yield api


def _field_line(item: dict, lines: Sequence[int] | None) -> str:
    """Um item de `fields` no texto: `  - <field>: <reason>` ou, na importação da lista (com as
    linhas do arquivo), `  - linha <N>: <reason>` para um `rows[i]`."""
    line = row_line(item.get("field"), lines) if lines is not None else None
    if line is not None:
        return f"  - linha {line}: {item.get('reason')}"
    return f"  - {item.get('field')}: {item.get('reason')}"


def _pending_line(pending: dict) -> str:
    """A linha do `purge_pending` no texto: só as 5 contagens do envelope."""
    return (f"  pendente: {pending.get('outbox')} na fila de saída, {pending.get('updates')} updates, "
            f"{pending.get('tasks')} tarefas, {pending.get('freeze')} relatórios por congelar, "
            f"{pending.get('due')} envios devidos (revelações e avisos).")


def _report_api_error(exc: ApiError, lines: Sequence[int] | None = None) -> NoReturn:
    """Escreve o erro da API no stderr e encerra com código 1. `lines` são as linhas do arquivo da
    lista (só em `students import`); com `--json`, o corpo sai intacto, sem tradução."""
    if _options().as_json:
        _echo_err(_dumps(exc.body))
    else:
        error = exc.body.get("error") if isinstance(exc.body, dict) else None
        error = error if isinstance(error, dict) else {}
        message = error.get("message")
        _echo_err(f"Erro: {message if isinstance(message, str) else 'a API recusou o pedido.'}")
        state = error.get("state")
        if error.get("code") == INVALID_STATE_CODE and isinstance(state, str):
            _echo_err(f"Estado atual: {state}")
        pending = exc.pending
        if error.get("code") == PURGE_PENDING_CODE and pending is not None:
            _echo_err(_pending_line(pending))  # só no expurgo com pendências
        permission = error.get("permission")
        if isinstance(permission, str):
            _echo_err(f"Permissão necessária: {permission}")
        fields = error.get("fields")
        if isinstance(fields, list):
            for item in fields:
                if isinstance(item, dict):
                    _echo_err(_field_line(item, lines))
    raise _Done(EXIT_API_ERROR)


def _local_hint() -> tuple[EffectiveProfile, str] | None:
    """A dica de subir a pilha local com o perfil efetivo já resolvido, ou `None` quando ela não
    se aplica (perfil remoto, `CQ_API_URL` definida, perfil ainda não resolvido). O `--json` fica com
    quem chama."""
    profile = _options().effective
    if profile is None:
        return None
    hint = local_stack_hint(profile, os.environ)
    return None if hint is None else (profile, hint)


def _report_unreachable(exc: ApiUnreachable) -> NoReturn:
    """A API inalcançável numa folha do catálogo: a mensagem fixa e o código 3.

    Com o perfil `local`, sem `CQ_API_URL` e sem
    `--json`, depois da mensagem, a linha da dica; e, com o stdin e o stderr num terminal, a pergunta
    `[s/N]`. "Sim" roda a mesma rotina do `cq up` (resumo no stderr) e pede para repetir o comando, que
    nunca é repetido aqui: código 3, ou o do `up` se ele falhar (1, 2, 130)."""
    message = f"Não foi possível conectar à API em {exc.url}."
    if _options().as_json:
        _echo_err(_dumps({"error": {"code": UNREACHABLE_CODE, "message": message}}))
        raise _Done(EXIT_UNREACHABLE)
    _echo_err(f"Erro: {message}")
    found = _local_hint()
    if found is not None:
        profile, hint = found
        _echo_err(hint)
        if stdin_is_terminal() and stderr_is_terminal():
            _offer_local_up(profile)
    raise _Done(EXIT_UNREACHABLE)


def _offer_local_up(profile: EffectiveProfile) -> None:
    """A pergunta de subir a pilha local e, no "sim", a subida. Volta só no "não"."""
    try:
        start = ask_start_local_stack(profile.compose_project)
    except KeyboardInterrupt:
        _echo_err(INTERRUPTED_MESSAGE)
        raise _Done(EXIT_INTERRUPTED) from None
    if not start:
        return
    summary = _local_up(profile, timeout=UP_TIMEOUT_SECONDS, rebuild=False)
    for line in _up_lines(summary):
        _echo_err(line)
    _echo_err(RERUN_MESSAGE)
    raise _Done(EXIT_UNREACHABLE)


def _request(call: Callable[[ApiClient], _T], profile: Profile | None = None, *,
             lines: Sequence[int] | None = None) -> _T:
    """Carrega o perfil (se não veio pronto), chama a API e trata os erros (códigos 1, 2 e 3).

    `lines` são as linhas do arquivo da lista, para traduzir os erros `rows[i]`.
    """
    if profile is None:
        profile = _profile()
    with _api(profile) as api:
        try:
            return call(api)
        except ApiError as exc:
            _report_api_error(exc, lines)
        except ApiUnreachable as exc:
            _report_unreachable(exc)


def _emit(body: dict, lines: Callable[[dict], list[str]]) -> None:
    """Saída de sucesso: o corpo (com `--json`) ou as linhas para pessoas."""
    if _options().as_json:
        _echo(_dumps(body))
        return
    text = lines(body)
    if text:  # uma só escrita: nada de saída parcial se algo falhar no meio
        _echo("\n".join(text))


def _person_line(person: dict) -> str:
    return f"{person['alias']}  {person['pseudo_id']}  {person['created_at']}"


def _dash(value: Any) -> str:
    return "-" if value is None else str(value)


# --------------------------------------------------------------------------------------------------
# Árvore de comandos


@click.group("cq", cls=_RootGroup)
@click.option("--json", "as_json", is_flag=True,
              help="Imprime o corpo JSON da resposta da API (antes do subcomando).")
@click.option("--profile", "profile_name", default=None, metavar="NOME",
              help="Usa o perfil nomeado NOME do arquivo de perfis (antes do subcomando).")
@click.pass_context
def cli(ctx: click.Context, as_json: bool, profile_name: str | None) -> None:
    """canal-questoes: operação pela API e da pilha local.

    O endereço e o token vêm de um perfil do arquivo TOML de perfis (CQ_CONFIG, ou o arquivo padrão do
    cq). O arquivo pode ter perfis nomeados, locais ou remotos, em [profiles.NOME]; escolha um com
    --profile NOME (antes do subcomando), com CQ_PROFILE ou com default_profile no arquivo, nesta ordem.
    Sem perfil escolhido, vale o perfil implícito: as chaves api_url e token do topo do arquivo.
    CQ_API_URL e CQ_TOKEN vencem o campo correspondente do perfil. Nenhuma opção recebe o token.

    \b
    Variáveis de ambiente:
      CQ_PROFILE    perfil nomeado escolhido (a opção --profile vence)
      CQ_API_URL    endereço da API (vence o do perfil)
      CQ_TOKEN      token da API (vence o do perfil)
      CQ_CONFIG     caminho do arquivo de perfis
      CQ_STACK_DIR  diretório da pilha local, se o perfil local não tiver stack_dir

    \b
    Comandos locais (operam a pilha Docker deste computador pelo perfil local):
      cq up      sobe a pilha e espera a API ficar saudável
      cq down    para a pilha, mantendo os dados
      cq status  mostra o perfil, a pilha (no perfil local) e a saúde da API
      cq logs    mostra os registros da api, do worker e do postgres

    O comando cq doctor confere o perfil, a API e a pilha local.
    """
    _quiet_http_loggers()
    given = ctx.obj if isinstance(ctx.obj, dict) else {}
    cwd = given.get("cwd")
    ctx.obj = _Options(as_json=as_json, http=given.get("http"), profile_name=profile_name,
                       stack=given.get("stack"), runner=given.get("runner"), clock=given.get("clock"),
                       sleep=given.get("sleep"), cwd=Path(cwd) if cwd is not None else None,
                       host_probe=given.get("host_probe"))


@cli.command("health")
def health_command() -> None:
    """Saúde da API e do banco (system.health)."""
    body = _request(lambda api: api.health())
    if body.get("status") == "ok":
        _emit(body, lambda b: [f"API no ar: versão {b.get('version')}, esquema {b.get('schema_version')}."])
        return
    if _options().as_json:
        _echo(_dumps(body))
    else:
        schema = body.get("schema_version")
        _echo_err(f"Erro: API com problema (banco: {body.get('database')}, "
                  f"esquema: {'nenhum' if schema is None else schema}).")
    raise _Done(EXIT_API_ERROR)


# --- comandos locais da pilha -------------------
#
# As folhas `up`, `down`, `status` e `logs` ficam fora do catálogo (`local_commands.LOCAL_COMMANDS`):
# as rotinas e as mensagens fixas vêm de `cq_client.local_commands`, e o CLI só valida as opções (antes de
# qualquer pedido, código 2), escolhe a pilha (o `Stack` do `ctx.obj`, o `ComposeStack` com o `runner` do
# `ctx.obj` ou o `SubprocessRunner`) e imprime. O texto do Docker nunca é repassado. O Ctrl+C sai com
# 130 e `Interrompido.` só aqui, no `doctor` e nas folhas do expurgo (`_interruptible`; nas demais, o `Abort` → 1).

# Nomes das origens dos campos na linha `Origem:`.
_ORIGIN_LABELS = {
    "name": {"option": "opção --profile", "env": "CQ_PROFILE", "file": "default_profile", "default": "implícito"},
    "kind": {"profile": "declarado", "inferred": "inferido do endereço"},
    "api_url": {"env": "CQ_API_URL", "profile": "perfil", "file": "topo do arquivo", "default": "padrão"},
    "token": {"env": "CQ_TOKEN", "profile": "perfil", "file": "topo do arquivo", "default": "ausente"},
}
_KIND_LABELS = {"local": "local", "remote": "remoto"}


def _profile_lines(profile: EffectiveProfile) -> list[str]:
    """As linhas `Perfil:` e `Origem:` do `status` e do `doctor`: o endereço sem
    credenciais e nunca o token; o nome vindo do arquivo passa por `_terminal_safe`."""
    name = _terminal_safe(profile.name) if profile.name is not None else "implícito"
    token = "token definido" if profile.token else "sem token"
    origins = profile.origins

    def label(key: str) -> str:
        return _ORIGIN_LABELS[key].get(origins.get(key, ""), "-")

    return [
        f"Perfil: {name} ({_KIND_LABELS.get(profile.kind, profile.kind)}), "
        f"API em {_terminal_safe(profile.display_url)}, {token}.",
        f"Origem: nome {label('name')}, tipo {label('kind')}, endereço {label('api_url')}, "
        f"token {label('token')}.",
    ]


def _up_lines(summary: UpSummary) -> list[str]:
    return [UP_DONE_TEMPLATE.format(project=summary.project), *(service_line(s) for s in summary.services)]


def _check_range(value: int, bounds: tuple[int, int], option: str) -> None:
    """Uso incorreto fora do intervalo, sem eco do valor nem dos limites."""
    low, high = bounds
    if not low <= value <= high:
        raise _Usage(f"valor fora do intervalo aceito na opção {option}.")


@contextlib.contextmanager
def _interruptible() -> Iterator[None]:
    """O Ctrl+C (`KeyboardInterrupt`) dos comandos locais, do `doctor` e do expurgo: `Interrompido.` no
    stderr, em texto puro também com `--json`, e o código 130. Só os comandos locais, o `doctor` e as folhas do
    expurgo passam por aqui; nas demais folhas, vale o `click.Abort` → 1 do grupo raiz."""
    try:
        yield
    except KeyboardInterrupt:
        _echo_err(INTERRUPTED_MESSAGE)
        raise _Done(EXIT_INTERRUPTED) from None


@contextlib.contextmanager
def _local_errors(timeout: float | None = None) -> Iterator[None]:
    """Converte as falhas dos comandos locais na linha fixa do stderr e no código."""
    try:
        with _interruptible():
            yield
    except StackError as exc:
        _echo_err(exc.message)
        raise _Done(exc.exit_code) from None
    except UpTimeout as exc:
        _echo_err(up_timeout_message(exc, timeout if timeout is not None else UP_TIMEOUT_SECONDS))
        raise _Done(exc.exit_code) from None
    except PortMismatch as exc:
        _echo_err(exc.message)
        raise _Done(exc.exit_code) from None


def _local_stack(profile: EffectiveProfile, *, require_env_file: bool = True) -> tuple[Stack, bool]:
    """`local_ops.local_stack` com o ambiente do processo e o `cwd`, o `Stack` e o `runner`
    do `ctx.obj`: as recusas na ordem fixa, sem nenhuma chamada ao Docker."""
    options = _options()
    return local_ops.local_stack(profile, os.environ, cwd=options.cwd if options.cwd is not None else Path.cwd(),
                                 stack=options.stack, runner=options.runner, require_env_file=require_env_file)


def _local_up(profile: EffectiveProfile, *, timeout: int, rebuild: bool) -> UpSummary:
    """A rotina do `cq up` (também a do "sim" da pergunta de subir a pilha): as recusas, a subida e a espera, com o
    progresso fixo no stderr; as falhas saem pela linha fixa e o código (`_Done`)."""
    options = _options()
    with _local_errors(timeout):
        stack, _ = _local_stack(profile)
        with _api(profile.as_profile()) as api:
            return local_commands.run_up(stack, profile, local_ops.health_probe(api), timeout=timeout,
                                         rebuild=rebuild, clock=options.clock or time.monotonic,
                                         sleep=options.sleep or time.sleep, progress=_echo_err)
    raise AssertionError("inalcançável")  # pragma: no cover - `_local_errors` sempre levanta


@cli.command("up")
@click.option("--timeout", "timeout", type=int, default=UP_TIMEOUT_SECONDS, metavar="SEGUNDOS",
              help="Espera máxima pela pilha saudável, em segundos (1 a 3600; padrão 180).")
@click.option("--rebuild", is_flag=True, default=False, help="Reconstrói as imagens da api e do worker antes.")
def up_command(timeout: int, rebuild: bool) -> None:
    """Sobe a pilha local e espera a API ficar saudável (comando local)."""
    _check_range(timeout, UP_TIMEOUT_RANGE, "--timeout")
    summary = _local_up(_effective_profile(), timeout=timeout, rebuild=rebuild)
    if _options().as_json:
        _echo(_dumps(local_ops.up_body(summary)))
        return
    _echo("\n".join(_up_lines(summary)))


@cli.command("down")
def down_command() -> None:
    """Para a pilha local, mantendo os dados (comando local)."""
    profile = _effective_profile()
    with _local_errors():
        stack, _ = _local_stack(profile)
        local_commands.run_down(stack)
    if _options().as_json:
        _echo(_dumps(local_ops.down_body(profile)))
        return
    _echo(DOWN_DONE_TEMPLATE.format(project=profile.compose_project))


def _status_api_line(report: StatusReport) -> str:
    """A linha `API:` do `status`."""
    if not report.api_reachable:
        return "API: inalcançável."
    health = report.health
    if health is None:
        return "API: com falha."
    state = "saudável" if health_ok(health) else "com falha"

    def field(key: str) -> str:
        return _terminal_safe(_dash(health.get(key)))

    return (f"API: {state}, versão {field('version')}, esquema {field('schema_version')} "
            f"(esperado {field('expected_schema_version')}).")


def _status_warning_line(warning: str, profile: EffectiveProfile) -> str | None:
    """O aviso fixo do `status` no texto."""
    if warning == "foreign_api":
        return FOREIGN_API_TEMPLATE.format(project=profile.compose_project)
    if warning == "port_mismatch":
        return PORT_MISMATCH_MESSAGE
    if warning == "docker_unavailable":
        return DockerUnavailable.message
    if warning == "env_file_missing":
        return StackEnvMissing.message
    if warning == "insecure_transport":
        text = insecure_transport_warning(profile)
        return _terminal_safe(text) if text is not None else None
    return None


def _status_lines(report: StatusReport) -> list[str]:
    profile = report.profile
    lines = _profile_lines(profile)
    if report.snapshot is not None:
        lines.append(f"Pilha local (projeto {profile.compose_project}):")
        lines.extend(service_line(s, report.stack_error) for s in report.snapshot.services)
    lines.append(_status_api_line(report))
    for warning in report.warnings:
        line = _status_warning_line(warning, profile)
        if line is not None:
            lines.append(line)
    return lines


@cli.command("status")
def status_command() -> None:
    """Mostra o perfil, a pilha local (no perfil local) e a saúde da API (comando local)."""
    profile = _effective_profile()
    with _local_errors():
        stack: Stack | None = None
        env_file_present = True
        if profile.kind == "local":
            stack, env_file_present = _local_stack(profile, require_env_file=False)
        with _api(profile.as_profile()) as api:
            report = local_commands.status_report(profile, stack, api, env_file_present=env_file_present)
    # O relatório no stdout também no código 1, com a linha fixa de resumo no stderr.
    if _options().as_json:
        _echo(_dumps(local_ops.status_body(report)))
    else:
        _echo("\n".join(_status_lines(report)))
    if report.exit_code != EXIT_OK:
        error = report.stack_error
        _echo_err(error.message if error is not None else STATUS_PROBLEM_SUMMARY)
        raise _Done(report.exit_code)


@cli.command("logs")
@click.option("--service", "services", multiple=True, metavar="SERVIÇO",
              help="Serviço: api, worker ou postgres (repetível; padrão: os três).")
@click.option("--tail", "tail", type=int, default=LOGS_TAIL, metavar="N",
              help="Linhas finais de cada serviço (1 a 10000; padrão 200).")
@click.option("--follow", is_flag=True, default=False, help="Segue os registros novos (não combina com --json).")
def logs_command(services: tuple[str, ...], tail: int, follow: bool) -> None:
    """Mostra os registros da api, do worker e do postgres, já limpos (comando local)."""
    # Comparação exata: os registros do engine são sensíveis e ficam fora; o valor não é ecoado.
    if any(service not in LOG_SERVICES for service in services):
        raise _Usage(LOGS_SERVICE_REFUSED)
    _check_range(tail, LOGS_TAIL_RANGE, "--tail")
    as_json = _options().as_json
    if follow and as_json:
        raise _Usage(LOGS_FOLLOW_JSON_REFUSED)
    profile = _effective_profile()
    with _local_errors():
        stack, _ = _local_stack(profile)
        lines = local_commands.run_logs(stack, services, tail=tail, follow=follow)
        try:
            if follow:
                # Linha a linha, enquanto chegam; o Ctrl+C encerra (130).
                for line in lines:
                    _echo(line)
                return
            # Sem seguir, tudo antes de imprimir: numa falha do Compose, o stdout fica vazio.
            collected = list(lines)
        finally:
            close = getattr(lines, "close", None)
            if close is not None:
                close()
    if as_json:
        _echo(_dumps({"lines": collected}))
    elif collected:
        _echo("\n".join(collected))


# --- doctor ---------------------------
#
# A folha do `system.doctor`, composta no cliente: a parte da API (`doctor.collect_api_checks`: `health`, o
# código HTTP da operação e as do servidor), a parte local (só com o perfil local), as 2 do host logo depois das
# locais e o arquivo de perfil (com qualquer perfil).
# O relatório sai no stdout também com o código 1, com a linha fixa de resumo
# no stderr; nunca o código 3. Com a API inalcançável, o perfil local, sem `CQ_API_URL` e sem `--json`, a
# linha da dica de subir a pilha no stderr, **sem** a pergunta.


def _doctor_local_checks(profile: EffectiveProfile, *, api_reachable: bool,
                         settings: doctor.LocalDoctorSettings) -> list[doctor.Check]:
    """A parte local por `doctor.collect_local_checks`, seguida das 2 do host por
    `doctor.collect_host_checks`: "não se aplica" com o perfil remoto, sem tocar a pilha nem o
    host; com o perfil local, o `Stack` do `ctx.obj` ou o `ComposeStack`.

    Diferente dos comandos locais, nada aqui recusa com o código 2: o diretório da pilha não encontrado vira
    `stack.directory` em falha; o `.env` ausente, `stack.env_file` em falha (só a existência, sem ler);
    e, **[I]**, o alvo do Docker não local, `docker.reachable` em falha, sem nenhuma chamada ao
    Docker. O diretório é procurado antes do alvo, para que `stack.directory` saia verificado. Nessas três
    saídas antecipadas, volume e disco saem "não verificado" com o motivo delas, sem nenhum probe.

    O `HostProbe`: o do `ctx.obj`; senão, o `DockerHostProbe` real (com o ambiente do processo) só quando
    o `ctx.obj` não traz `stack` nem `runner`; com um dublê injetado e sem `host_probe`, volume e disco saem "não
    verificado" `no_host_probe`, sem chamar nada (nenhum argv fora da lista fechada)."""
    if profile.kind != "local":
        return [*doctor.collect_local_checks(None, profile, stack_dir_error=None, env_file_present=None,
                                             api_reachable=api_reachable),
                *doctor.collect_host_checks(profile, host_probe=None, settings=settings)]
    options = _options()
    try:
        stack_dir = locate_stack_dir(profile, os.environ, options.cwd if options.cwd is not None else Path.cwd())
    except StackError as error:
        return [*doctor.collect_local_checks(None, profile, stack_dir_error=error, env_file_present=None,
                                             api_reachable=api_reachable),
                *doctor.collect_host_checks(profile, host_probe=None, settings=settings, blocked="stack_directory")]
    env_file_present = (stack_dir / STACK_ENV_FILE).exists()
    try:
        check_docker_target(os.environ)
    except StackError as error:
        return [*doctor.collect_local_checks(None, profile, stack_dir_error=error, env_file_present=env_file_present,
                                             api_reachable=api_reachable),
                *doctor.collect_host_checks(profile, host_probe=None, settings=settings, blocked="docker_target")]
    stack: Stack = (options.stack if options.stack is not None
                    else ComposeStack(profile.compose_project, stack_dir, options.runner, env=dict(os.environ)))
    local_checks = doctor.collect_local_checks(stack, profile, stack_dir_error=None,
                                               env_file_present=env_file_present, api_reachable=api_reachable)
    if not env_file_present:
        return [*local_checks,
                *doctor.collect_host_checks(profile, host_probe=None, settings=settings, blocked="env_file")]
    host_probe = options.host_probe
    if host_probe is None and options.stack is None and options.runner is None:
        host_probe = DockerHostProbe(SubprocessRunner(), stack_dir, env=dict(os.environ))
    return [*local_checks, *doctor.collect_host_checks(profile, host_probe=host_probe, settings=settings)]


def _doctor_line(check: doctor.Check) -> str:
    """`[<resultado>] <id>[ (<código>)]: <mensagem>[ (<count> | <detail>)]`; o que veio da
    API passa por `_terminal_safe`."""
    offering = f" ({_terminal_safe(check.offering)})" if check.offering is not None else ""
    if check.count is not None:
        extra = f" ({check.count})"
    elif check.detail is not None:
        extra = f" ({_terminal_safe(check.detail)})"
    else:
        extra = ""
    return doctor.CHECK_LINE_TEMPLATE.format(label=doctor.RESULT_LABELS[check.result], id=check.id,
                                             offering=offering, message=_terminal_safe(check.message),
                                             extra=extra)


def _doctor_lines(profile: EffectiveProfile, checks: list[doctor.Check]) -> list[str]:
    failures = sum(1 for check in checks if check.result == "failure")
    warnings = sum(1 for check in checks if check.result == "warning")
    return [*_profile_lines(profile), *(_doctor_line(check) for check in checks),
            doctor.SUMMARY_TEMPLATE.format(failures=failures, warnings=warnings)]


def _doctor_body(profile: EffectiveProfile, checks: list[doctor.Check]) -> dict[str, Any]:
    """O `doctor.output`: o `ProfileView` e as verificações, sem token nem caminho."""
    return {"profile": local_ops.profile_view(profile),
            "checks": [{"id": c.id, "part": c.part, "result": c.result, "message": c.message,
                        "offering": c.offering, "count": c.count, "detail": c.detail} for c in checks]}


@cli.command("doctor")
def doctor_command() -> None:
    """Confere o perfil, a API e a pilha local (system.doctor e as verificações do cliente)."""
    options = _options()
    # Os limites do disco lidos na partida, antes de qualquer chamada; inválido → uso
    # incorreto (2) com a mensagem fixa de `LocalDoctorSettings.from_env`, sem o valor.
    try:
        settings = doctor.LocalDoctorSettings.from_env(os.environ)
    except ValueError as exc:
        raise _Usage(str(exc)) from None
    profile = _effective_profile()
    with _local_errors():
        with _api(profile.as_profile()) as api:
            api_checks = doctor.collect_api_checks(api, profile)
        api_reachable = not any(c.id == "api.reachable" and c.result == "failure" for c in api_checks)
        checks = [*api_checks,
                  *_doctor_local_checks(profile, api_reachable=api_reachable, settings=settings),
                  *doctor.check_profile_file(profile.config_path)]
    if options.as_json:
        _echo(_dumps(_doctor_body(profile, checks)))
    else:
        _echo("\n".join(_doctor_lines(profile, checks)))
    code = doctor.exit_code(checks)
    if code == EXIT_OK:
        return
    if not api_reachable and not options.as_json:
        # Só a linha da dica, nunca a pergunta interativa.
        found = _local_hint()
        if found is not None:
            _echo_err(found[1])
    _echo_err(doctor.DOCTOR_PROBLEM_SUMMARY)
    raise _Done(code)


# --- servidor MCP ------------------------------------------------------------------------------------


@cli.command(MCP_COMMAND)
def mcp_command() -> None:
    """Inicia o servidor MCP (stdio) para agentes, com o perfil escolhido."""
    # O SDK `mcp` só é carregado aqui: os demais comandos não pagam o import. O servidor é chamado pelo
    # atributo do módulo, e o perfil (`--profile`) é resolvido por ele a cada chamada de ferramenta.
    try:
        from cq_client import mcp_server
    except Exception:  # noqa: BLE001 - só a linha fixa, sem detalhe
        click.echo(MCP_LOAD_FAILURE_LINE, err=True)
        raise _Done(1) from None

    raise _Done(mcp_server.serve(_options().profile_name))


# --- pessoas ---------------------------------------------------------------------------------------


@cli.group("people")
def people_group() -> None:
    """Pessoas (pseudônimos)."""


@people_group.command("add")
@click.option("--name", required=True, help="Nome completo.")
@click.option("--ra", default=None, help="RA (opcional).")
@click.option("--email", default=None, help="E-mail (opcional).")
def people_add(name: str, ra: str | None, email: str | None) -> None:
    """Cadastra uma pessoa (people.register)."""
    body = _request(lambda api: api.register_person(name=name, ra=ra, email=email))
    _emit(body, lambda b: [f"Pessoa cadastrada: {b['alias']} ({b['pseudo_id']})."])


@people_group.command("list")
@click.option("--after", type=int, default=None, help="Cursor: só pessoas depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
def people_list(after: int | None, limit: int | None) -> None:
    """Lista as pessoas em ordem de cadastro (people.list)."""
    body = _request(lambda api: api.list_people(after=after, limit=limit))
    _emit(body, lambda b: [*(_person_line(p) for p in b["items"]), f"Próximo cursor: {b['next_cursor']}"])


@people_group.command("show")
@click.argument("ref")
def people_show(ref: str) -> None:
    """Mostra uma pessoa pelo apelido ou identificador (people.get)."""
    body = _request(lambda api: api.get_person(ref))
    _emit(body, lambda b: [_person_line(b)])


# --- identidade ------------------------------------------------------------------------------------


@cli.group("identity")
def identity_group() -> None:
    """Identidades (revelação auditada)."""


@identity_group.command("reveal")
@click.argument("refs", nargs=-1, required=True, metavar="REF...")
@click.option("--reason", required=True, help="Justificativa da revelação (auditada).")
def identity_reveal(refs: tuple[str, ...], reason: str) -> None:
    """Revela a identidade de pseudônimos (identity.reveal)."""
    body = _request(lambda api: api.reveal(list(refs), reason=reason))

    def lines(b: dict) -> list[str]:
        out = [f"{p['alias']}  {p['name']}  RA: {_dash(p.get('ra'))}  e-mail: {_dash(p.get('email'))}"
               for p in b["people"]]
        out += [f"Não encontrado: {item}" for item in b["not_found"]]
        return out

    _emit(body, lines)


# --- tokens ----------------------------------------------------------------------------------------


@cli.group("tokens")
def tokens_group() -> None:
    """Tokens da API."""


def _create_secret_file(path: str) -> typing.TextIO:
    """Cria o arquivo de --secret-file com criação exclusiva e permissão 0600 (onde o sistema permitir)."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0))  # LF também no Windows
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise _Usage("o arquivo de --secret-file já existe.") from None
    except (OSError, ValueError):
        raise _Usage("não foi possível criar o arquivo de --secret-file.") from None
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return open(fd, "w", encoding="ascii", newline="\n")  # noqa: SIM115 - fechado pelo chamador
    except Exception:
        os.close(fd)
        _remove_quietly(path)
        raise _Usage("não foi possível criar o arquivo de --secret-file.") from None


def _remove_quietly(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _close_quietly(file: typing.TextIO) -> None:
    with contextlib.suppress(Exception):
        file.close()


def _secret_not_saved() -> NoReturn:
    if _options().as_json:
        _echo_err(_dumps({"error": {"code": SECRET_NOT_SAVED_CODE, "message": SECRET_NOT_SAVED_MESSAGE}}))
    else:
        _echo_err("Erro: o segredo não pôde ser gravado; o token foi revogado.")
    raise _Done(EXIT_API_ERROR)


@tokens_group.command("create")
@click.option("--label", required=True, help="Rótulo do token.")
@click.option("--permission", "permissions", multiple=True, required=True,
              help="Permissão concedida (repita para várias).")
@click.option("--secret-file", "secret_file", default=None,
              help="Arquivo novo onde gravar o segredo (obrigatório fora de um terminal).")
def tokens_create(label: str, permissions: tuple[str, ...], secret_file: str | None) -> None:
    """Cria um token (tokens.create). O segredo só é mostrado num terminal."""
    profile = _profile()
    if secret_file is None and not stdout_is_terminal():
        raise _Usage("fora de um terminal, informe --secret-file.")
    file = _create_secret_file(secret_file) if secret_file is not None else None
    with _api(profile) as api:
        try:
            body = api.create_token(label=label, permissions=list(permissions))
        except (ApiError, ApiUnreachable) as exc:
            if file is not None and secret_file is not None:
                _close_quietly(file)
                _remove_quietly(secret_file)
            if isinstance(exc, ApiError):
                _report_api_error(exc)
            _report_unreachable(exc)
        except BaseException:
            if file is not None and secret_file is not None:
                _close_quietly(file)
                _remove_quietly(secret_file)
            raise

        public = {key: value for key, value in body.items() if key != "secret"}
        first_line = f"Token criado: {body['token_id']} ({', '.join(body['permissions'])})."
        if file is None:
            _emit(body, lambda b: [first_line, f"Segredo (mostrado só agora): {b['secret']}"])
            return

        try:
            write_secret_file(file, body["secret"])
            file.close()
        except Exception:  # noqa: BLE001 - qualquer falha de gravação revoga o token
            _close_quietly(file)
            _remove_quietly(secret_file)  # type: ignore[arg-type]
            with contextlib.suppress(Exception):
                api.revoke_token(body["token_id"])
            _secret_not_saved()

    _emit({**public, "secret_file": secret_file},
          lambda _b: [first_line, f"Segredo gravado em {secret_file}."])


@tokens_group.command("list")
def tokens_list() -> None:
    """Lista os tokens em ordem de criação (tokens.list)."""
    body = _request(lambda api: api.list_tokens())
    _emit(body, lambda b: [
        f"{t['token_id']}  {t['label']}  {','.join(t['permissions'])}  {t['created_at']}  "
        f"{'ativo' if t.get('revoked_at') is None else t['revoked_at']}"
        for t in b["items"]])


@tokens_group.command("revoke")
@click.argument("token_id")
def tokens_revoke(token_id: str) -> None:
    """Revoga um token (tokens.revoke); revogar de novo não muda nada."""
    body = _request(lambda api: api.revoke_token(token_id))
    _emit(body, lambda b: [f"Token revogado: {b['token_id']} em {b['revoked_at']}."])


# --- eventos ---------------------------------------------------------------------------------------


@cli.group("events")
def events_group() -> None:
    """Log de eventos."""


@events_group.command("list")
@click.option("--type", "types", multiple=True,
              help="Tipo de evento (repita para vários; obrigatório sem --person).")
@click.option("--after", type=int, default=None, help="Cursor: só eventos depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
@click.option("--person", default=None, metavar="REF",
              help="Só os eventos que citam a pessoa (apelido ou pseudônimo).")
def events_list(types: tuple[str, ...], after: int | None, limit: int | None, person: str | None) -> None:
    """Lista eventos em ordem de sequência (events.list)."""
    # A validação vem antes de qualquer pedido, sem eco.
    if not types and person is None:
        raise _Usage(EVENTS_TYPE_OR_PERSON)
    body = _request(lambda api: api.list_events(list(types), after=after, limit=limit, person=person))
    _emit(body, lambda b: [*(f"{e['seq']}  {e['occurred_at']}  {e['type']}" for e in b["items"]),
                           f"Próximo cursor: {b['next_cursor']}"])


# --- oferecimentos ---------------------------------------------------------------------------------


def _offering_line(offering: dict) -> str:
    """`<code>  <state>  <course>  <term>  bot: <username ou "-">`."""
    bot = offering.get("bot")
    username = bot.get("username") if isinstance(bot, dict) else None
    return (f"{offering['code']}  {offering['state']}  {offering['course']}  {offering['term']}  "
            f"bot: {_dash(username)}")


@cli.group("offerings")
def offerings_group() -> None:
    """Oferecimentos de disciplina."""


@offerings_group.command("create")
@click.argument("code")
@click.option("--course", required=True, help="Nome da disciplina.")
@click.option("--term", required=True, help="Período (ex.: 2031s1).")
@click.option("--timezone", "zone", required=True, help="Fuso IANA do oferecimento (ex.: America/Sao_Paulo).")
@click.option("--response-window", "response_window", type=int, default=None,
              help="Janela de resposta em minutos (5 a 43200; padrão 1440).")
@click.option("--free-messages", "free_messages", type=click.Choice(["on", "off"]), default=None,
              help="Mensagens livres ligadas (on) ou desligadas (off; o padrão).")
@click.option("--contact-email", "contact_email", default=None,
              help="E-mail do contato do professor, indicado aos alunos com as mensagens livres desligadas.")
def offerings_create(code: str, course: str, term: str, zone: str, response_window: int | None,
                     free_messages: str | None, contact_email: str | None) -> None:
    """Cadastra um oferecimento, em rascunho (offerings.create)."""
    body = _request(lambda api: api.create_offering(code=code, course=course, term=term, timezone=zone,
                                                    response_window_minutes=response_window,
                                                    free_messages=_on_off(free_messages),
                                                    contact_email=contact_email))
    _emit(body, lambda b: [f"Oferecimento cadastrado: {b['code']} ({b['offering_id']}), em rascunho."])


def _on_off(value: str | None) -> bool | None:
    """A escolha `on`/`off` como booleano (`None` quando a opção não foi dada)."""
    return None if value is None else value == "on"


@offerings_group.command("settings")
@click.argument("ref")
@click.option("--free-messages", "free_messages", type=click.Choice(["on", "off"]), default=None,
              help="Liga (on) ou desliga (off) as mensagens livres.")
@click.option("--contact-email", "contact_email", default=None,
              help="Define ou troca o e-mail do contato do professor.")
@click.option("--clear-contact-email", "clear_contact_email", is_flag=True, default=False,
              help="Remove o contato do professor.")
def offerings_settings(ref: str, free_messages: str | None, contact_email: str | None,
                       clear_contact_email: bool) -> None:
    """Altera a configuração de mensagens livres do oferecimento (offerings.update_settings).

    O e-mail do contato nunca é impresso: a saída diz só se há contato."""
    if free_messages is None and contact_email is None and not clear_contact_email:
        raise _Usage("informe --free-messages, --contact-email ou --clear-contact-email.")
    if contact_email is not None and clear_contact_email:
        raise _Usage("as opções --contact-email e --clear-contact-email não combinam.")
    body = _request(lambda api: api.update_offering_settings(
        ref, free_messages=_on_off(free_messages), contact_email=contact_email,
        clear_contact_email=clear_contact_email))
    _emit(body, lambda b: [
        f"Configuração de {b['code']}: mensagens livres "
        f"{'ligadas' if b.get('free_messages') else 'desligadas'}; contato do professor "
        f"{'definido' if b.get('contact_email_set') else 'não definido'}."])


@offerings_group.command("list")
@click.option("--after", type=int, default=None, help="Cursor: só oferecimentos depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
def offerings_list(after: int | None, limit: int | None) -> None:
    """Lista os oferecimentos em ordem de cadastro (offerings.list)."""
    body = _request(lambda api: api.list_offerings(after=after, limit=limit))
    _emit(body, lambda b: [*(f"{o['code']}  {o['state']}  {o['course']}  {o['term']}" for o in b["items"]),
                           f"Próximo cursor: {b['next_cursor']}"])


@offerings_group.command("show")
@click.argument("ref")
def offerings_show(ref: str) -> None:
    """Mostra um oferecimento pelo código ou identificador (offerings.get)."""
    body = _request(lambda api: api.get_offering(ref))
    _emit(body, _offering_show_lines)


def _offering_show_lines(offering: dict) -> list[str]:
    """A linha do oferecimento e, só se expurgado, `Expurgado em <purged_at>.`."""
    lines = [_offering_line(offering)]
    purged_at = offering.get("purged_at")
    if purged_at is not None:
        lines.append(f"Expurgado em {purged_at}.")
    return lines


@offerings_group.command("close")
@click.argument("ref")
def offerings_close(ref: str) -> None:
    """Encerra um oferecimento ativo (offerings.close); encerrar de novo não muda nada."""
    body = _request(lambda api: api.close_offering(ref))
    _emit(body, lambda b: [_offering_line(b)])


@offerings_group.group("archive")
def offerings_archive_group() -> None:
    """Arquivamento em duas fases: preparar e confirmar."""


@offerings_archive_group.command("prepare")
@click.argument("ref")
def offerings_archive_prepare(ref: str) -> None:
    """Prepara o arquivamento e mostra o código de confirmação (offerings.prepare_archive)."""
    body = _request(lambda api: api.prepare_archive(ref))
    _emit(body, lambda b: [
        f"Arquivar {b['offering']['code']}: {b['team_members_active']} na equipe, "
        f"{b['pending_links']} links pendentes.",
        f"Código de confirmação (válido até {b['expires_at']}): {b['confirmation_code']}"])


@offerings_archive_group.command("confirm")
@click.argument("ref")
@click.option("--confirmation", required=True, help="Código de confirmação dado por `archive prepare`.")
def offerings_archive_confirm(ref: str, confirmation: str) -> None:
    """Arquiva o oferecimento com o código de confirmação (offerings.archive)."""
    body = _request(lambda api: api.archive_offering(ref, confirmation_code=confirmation))
    _emit(body, lambda b: [f"Oferecimento arquivado: {b['code']} em {b['archived_at']}."])


# --- expurgo ---------------------------------------------------------------------------------------
#
# Folhas do expurgo, no catálogo: o Ctrl+C na chamada à API sai com 130 e `Interrompido.`, como nos comandos locais
# (`_interruptible`); as demais folhas seguem o mapeamento comum.


@offerings_group.group("purge")
def offerings_purge_group() -> None:
    """Expurgo das identidades de um oferecimento arquivado, em duas fases: preparar e confirmar."""


@offerings_purge_group.command("prepare")
@click.argument("ref")
def offerings_purge_prepare(ref: str) -> None:
    """Prepara o expurgo, mostra o impacto e o código de confirmação (offerings.prepare_purge)."""
    with _interruptible():
        body = _request(lambda api: api.prepare_purge(ref))

    def lines(b: dict) -> list[str]:
        impact = b["impact"]
        return [
            f"Expurgar {b['offering']['code']}: {impact['identities_to_delete']} identidades a apagar, "
            f"{impact['identities_kept']} mantidas por outro oferecimento, {impact['endpoints']} endereços de "
            f"entrega, {impact['updates']} updates, e-mail de contato "
            f"{'sim' if impact['contact_email_set'] else 'não'}.",
            f"Código de confirmação (válido até {b['expires_at']}): {b['confirmation_code']}"]

    _emit(body, lines)


@offerings_purge_group.command("confirm")
@click.argument("ref")
@click.option("--confirmation", required=True, help="Código de confirmação dado por `purge prepare`.")
def offerings_purge_confirm(ref: str, confirmation: str) -> None:
    """Expurga as identidades do oferecimento com o código de confirmação (offerings.purge); repetida num
    oferecimento já expurgado, mostra o resultado gravado."""
    with _interruptible():
        body = _request(lambda api: api.purge_offering(ref, confirmation_code=confirmation))
    _emit(body, lambda b: [
        f"Oferecimento expurgado: {b['offering']['code']} em {b['purged_at']}; "
        f"{b['result']['identities_deleted']} identidades apagadas, {b['result']['identities_kept']} mantidas."])


# --- resumo diário --------------------------------------------------------------------------------


def _digest_lines(body: dict[str, Any]) -> list[str]:
    """`Resumo diário de <code>: <ligado|desligado>, às <HH:MM> (<timezone>).`"""
    return [f"Resumo diário de {body['offering']['code']}: {'ligado' if body.get('enabled') else 'desligado'}, "
            f"às {body['local_time']} ({body['timezone']})."]


@offerings_group.group("digest")
def offerings_digest_group() -> None:
    """Resumo diário à equipe: consultar e alterar a configuração."""


@offerings_digest_group.command("show")
@click.argument("ref")
def offerings_digest_show(ref: str) -> None:
    """Mostra a configuração do resumo diário do oferecimento (offerings.get_digest)."""
    body = _request(lambda api: api.get_offering_digest(ref))
    _emit(body, _digest_lines)


@offerings_digest_group.command("set")
@click.argument("ref")
@click.option("--enabled", "enabled", type=click.Choice(["on", "off"]), default=None,
              help="Liga (on) ou desliga (off) o resumo diário.")
@click.option("--at", "local_time", default=None, help="Horário local HH:MM, no fuso do oferecimento.")
def offerings_digest_set(ref: str, enabled: str | None, local_time: str | None) -> None:
    """Altera a configuração do resumo diário do oferecimento (offerings.update_digest).

    O --at vai como digitado (quem valida é a API)."""
    if enabled is None and local_time is None:
        raise _Usage("informe --enabled ou --at.")
    body = _request(lambda api: api.update_offering_digest(ref, enabled=_on_off(enabled), local_time=local_time))
    _emit(body, _digest_lines)


# --- bots -----------------------------------------------------------------------------------------


@cli.group("bots")
def bots_group() -> None:
    """Bots do Telegram dos oferecimentos."""


def _read_token_file(path: str) -> str:
    """O token do arquivo de --token-file, em UTF-8, sem os espaços e as quebras de linha das pontas.

    Arquivo inexistente ou ilegível é uso incorreto, sem citar o caminho nem o conteúdo.
    """
    try:
        with open(path, "rb") as file:
            raw = file.read()
        return raw.decode("utf-8").strip()
    except (OSError, ValueError):  # UnicodeDecodeError é ValueError
        raise _Usage("não foi possível ler o arquivo de --token-file.") from None


def _bot_token(token_file: str | None) -> str:
    """O token do bot: do arquivo, se dado; senão, do prompt
    mascarado, só com a entrada padrão num terminal. Nenhuma opção ou argumento recebe o token."""
    if token_file is not None:
        return _read_token_file(token_file)
    if not stdin_is_terminal():
        raise _Usage("fora de um terminal, informe --token-file.")
    return prompt_secret(BOT_TOKEN_PROMPT)


@bots_group.command("register")
@click.argument("ref")
@click.option("--token-file", "token_file", default=None,
              help="Arquivo com o token do bot (obrigatório fora de um terminal; sem ele, o token é "
                   "pedido num prompt mascarado).")
def bots_register(ref: str, token_file: str | None) -> None:
    """Registra ou troca o bot do oferecimento (bots.register). O token nunca vai na linha de comando."""
    profile = _profile()
    bot_token = _bot_token(token_file)
    body = _request(lambda api: api.register_bot(ref, token=bot_token), profile)
    _emit(body, lambda b: [f"Bot registrado: @{b['username']} no oferecimento {b['offering']['code']} "
                           f"(perfil: {b['profile_state']})."])


@bots_group.command("activate")
@click.argument("ref")
def bots_activate(ref: str) -> None:
    """Ativa o oferecimento com o bot registrado (bots.activate)."""
    body = _request(lambda api: api.activate_bot(ref))
    _emit(body, lambda b: [_offering_line(b)])


def _bot_lines(bot: dict) -> list[str]:
    """Texto de `bots show`; horários como a API os devolve."""
    lease = bot.get("lease")
    if isinstance(lease, dict):
        holder = f"posse: {lease['worker_id']} até {lease['until']}"
    else:
        holder = "posse: nenhuma"
    lines = [f"@{bot['username']}  {bot['state']}  perfil: {bot['profile_state']}  {holder}  "
             f"última consulta: {_dash(bot.get('last_poll_ok_at'))}"]
    problem = bot.get("problem")
    if isinstance(problem, dict):
        lines.append(f"Problema: {problem['message']}")
    return lines


@bots_group.command("show")
@click.argument("ref")
def bots_show(ref: str) -> None:
    """Mostra o bot do oferecimento: estado, perfil, posse e problema (bots.get)."""
    body = _request(lambda api: api.get_bot(ref))
    _emit(body, _bot_lines)


# --- equipe ---------------------------------------------------------------------------------------


@cli.group("team")
def team_group() -> None:
    """Equipe dos oferecimentos."""


def _local_validity(expires_at: Any, zone: Any) -> str:
    """A validade no fuso do oferecimento, `AAAA-MM-DD HH:MM <tzname> <fuso>`, pelo `zoneinfo` sobre o
    `tzdata` do cliente; `fuso indisponível` se o cliente não carrega o fuso."""
    try:
        moment = datetime.fromisoformat(expires_at)
        if moment.tzinfo is None:
            return ZONE_UNAVAILABLE
        local = moment.astimezone(zoneinfo.ZoneInfo(zone))
        return f"{local.strftime('%Y-%m-%d %H:%M %Z')} {zone}"
    except Exception:  # noqa: BLE001 - o link já existe: nenhuma falha aqui pode perdê-lo
        return ZONE_UNAVAILABLE


def _team_link_heading(body: dict) -> str:
    """`Link de equipe para <alias> (válido até <expires_at>; <validade no fuso>):`."""
    expires_at = body["expires_at"]
    local = _local_validity(expires_at, body["offering"]["timezone"])
    return f"Link de equipe para {body['person']['alias']} (válido até {expires_at}; {local}):"


def _link_not_saved() -> NoReturn:
    if _options().as_json:
        _echo_err(_dumps({"error": {"code": LINK_NOT_SAVED_CODE, "message": LINK_NOT_SAVED_MESSAGE}}))
    else:
        _echo_err("Erro: o link não pôde ser gravado; gere outro.")
    raise _Done(EXIT_API_ERROR)


@team_group.command("link")
@click.argument("ref")
@click.option("--person", required=True, help="Pseudônimo da pessoa (apelido ou identificador).")
@click.option("--valid-hours", "valid_hours", type=int, default=None,
              help="Validade do link em horas (1 a 720; padrão 168).")
@click.option("--secret-file", "secret_file", default=None,
              help="Arquivo novo onde gravar o link (obrigatório fora de um terminal).")
def team_link(ref: str, person: str, valid_hours: int | None, secret_file: str | None) -> None:
    """Gera o link de equipe de uma pessoa (team.link). O link só é mostrado num terminal."""
    profile = _profile()
    if secret_file is None and not stdout_is_terminal():
        raise _Usage("fora de um terminal, informe --secret-file.")
    file = _create_secret_file(secret_file) if secret_file is not None else None
    with _api(profile) as api:
        try:
            body = api.create_team_link(ref, person=person, valid_hours=valid_hours)
        except (ApiError, ApiUnreachable) as exc:
            if file is not None and secret_file is not None:
                _close_quietly(file)
                _remove_quietly(secret_file)
            if isinstance(exc, ApiError):
                _report_api_error(exc)
            _report_unreachable(exc)
        except BaseException:
            if file is not None and secret_file is not None:
                _close_quietly(file)
                _remove_quietly(secret_file)
            raise

    if file is None:
        _emit(body, lambda b: [_team_link_heading(b), b["link"]])
        return

    try:
        write_secret_file(file, body["link"])
        file.close()
    except Exception:  # noqa: BLE001 - qualquer falha de gravação: o link fica pendente até vencer
        _close_quietly(file)
        _remove_quietly(secret_file)  # type: ignore[arg-type]
        _link_not_saved()

    public = {key: value for key, value in body.items() if key != "link"}
    _emit({**public, "secret_file": secret_file},
          lambda _b: [_team_link_heading(body), f"Link gravado em {secret_file}."])


# --- alunos ---------------------------------------------------------------------------------------


@cli.group("students")
def students_group() -> None:
    """Alunos dos oferecimentos."""


def _read_roster(path: str) -> Roster:
    """O arquivo da lista (`cq_client.roster`); fora do formato, uso incorreto com o motivo fixo da
    leitura da lista, sem o caminho nem o conteúdo."""
    try:
        return read_roster(path)
    except RosterFileError as exc:
        raise _Usage(exc.reason) from None


@students_group.command("import")
@click.argument("ref")
@click.option("--file", "file_path", required=True,
              help="Arquivo CSV da lista (UTF-8; colunas ra, nome e email na primeira linha, separadas por "
                   "vírgula ou por ponto e vírgula).")
def students_import(ref: str, file_path: str) -> None:
    """Importa a lista da turma, tudo ou nada (students.import). Só as contagens saem."""
    profile = _profile()
    roster = _read_roster(file_path)
    body = _request(lambda api: api.import_students(ref, rows=roster.rows), profile, lines=roster.lines)
    _emit(body, lambda b: [
        f"Lista importada em {b['offering']['code']}: {b['received']} linhas; {b['people_created']} pessoas "
        f"novas, {b['people_reused']} já cadastradas; {b['enrollments_created']} matrículas novas, "
        f"{b['already_enrolled']} já matriculados."])


# Estados da adesão, na ordem e com o rótulo do texto de `students summary`.
_ADHESION_LABELS = (
    ("no_invite", "sem convite"),
    ("sending", "envio em andamento"),
    ("invite_valid", "convite válido"),
    ("terms_presented", "termo apresentado"),
    ("invite_lapsed", "convite vencido ou revogado"),
    ("send_failed", "falha no envio"),
    ("linked", "vinculados"),
)


@students_group.command("summary")
@click.argument("ref")
def students_summary(ref: str) -> None:
    """Mostra a adesão dos alunos: só contagens por estado (students.summary)."""
    body = _request(lambda api: api.get_students_summary(ref))
    _emit(body, lambda b: [
        f"Adesão de {b['offering']['code']}: {b['enrolled']} alunos matriculados.",
        *(f"  {label}: {b[state]}" for state, label in _ADHESION_LABELS)])


# --- convites -------------------------------------------------------------------------------------


@cli.group("invites")
def invites_group() -> None:
    """Convites dos alunos por e-mail."""


@invites_group.group("send")
def invites_send_group() -> None:
    """Envio dos convites à turma em duas fases: preparar e confirmar."""


@invites_send_group.command("prepare")
@click.argument("ref")
@click.option("--valid-hours", "valid_hours", type=int, default=None,
              help="Validade dos convites em horas, a partir do envio (1 a 720; padrão 168).")
def invites_send_prepare(ref: str, valid_hours: int | None) -> None:
    """Prepara o envio e mostra o código de confirmação (invites.prepare_send)."""
    body = _request(lambda api: api.prepare_send_invites(ref, valid_hours=valid_hours))
    _emit(body, lambda b: [
        f"Enviar convites de {b['offering']['code']}: {b['eligible']} alunos, "
        f"validade de {b['valid_hours']} h a partir do envio.",
        f"Código de confirmação (válido até {b['expires_at']}): {b['confirmation_code']}"])


@invites_send_group.command("confirm")
@click.argument("ref")
@click.option("--confirmation", required=True, help="Código de confirmação dado por `invites send prepare`.")
def invites_send_confirm(ref: str, confirmation: str) -> None:
    """Enfileira os convites da turma com o código de confirmação (invites.send)."""
    body = _request(lambda api: api.send_invites(ref, confirmation_code=confirmation))
    _emit(body, lambda b: [f"Convites enfileirados em {b['offering']['code']}: {b['queued']}."])


def _resend_lines(body: dict) -> list[str]:
    """Texto de `invites resend`: os itens sem pessoa só são contados."""
    lines = [f"Reenvios enfileirados em {body['offering']['code']}: {body['queued']}."]
    lines += [f"Não é aluno do oferecimento: {person['alias']}" for person in body["not_students"]]
    if body["not_found_count"] > 0:
        lines.append(f"Sem pessoa: {body['not_found_count']}")
    return lines


@invites_group.command("resend")
@click.argument("ref")
@click.argument("pseudonyms", nargs=-1, required=True, metavar="PSEUDONYM...")
@click.option("--valid-hours", "valid_hours", type=int, default=None,
              help="Validade dos convites em horas, a partir do envio (1 a 720; padrão 168).")
def invites_resend(ref: str, pseudonyms: tuple[str, ...], valid_hours: int | None) -> None:
    """Reenvia o convite a alunos, pelo apelido ou identificador (invites.resend). Cada pedido cria
    envios novos: só o último link vale."""
    body = _request(lambda api: api.resend_invites(ref, pseudonyms=list(pseudonyms), valid_hours=valid_hours))
    _emit(body, _resend_lines)


# --- disparos e mensagens livres ------------------------------------------------------------------

_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _terminal_safe(text: Any) -> str:
    """O texto sem controle cru: quebras de linha, tabulação e os
    controles C0/C1 (inclusive ESC e DEL) viram os escapes visíveis do `repr` do Python."""
    out: list[str] = []
    for ch in str(text):
        code = ord(ch)
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


_DELIVERY_COUNT_KEYS = ("pending", "delivered", "not_delivered", "answered", "dont_know", "no_answer",
                        "answered_late", "question_failed", "grading_pending")


def _moment_line(label: str, moment: Any, zone: str) -> str:
    """`  <rótulo>: <UTC da API> (<AAAA-MM-DD HH:MM> <sigla> <fuso>)` (o formato do `team link`)."""
    return f"  {label}: {moment} ({_local_validity(moment, zone)})"


def _dispatch_lines(body: dict) -> list[str]:
    """Texto de `dispatches show`: estado e confirmação; a questão; abertura e
    fechamento em UTC e no fuso do oferecimento; a origem declarada; o motivo; as contagens; a prévia
    recuada. Todo texto de terceiros pelo `_terminal_safe`."""
    zone = body.get("timezone") or "UTC"
    lines = [f"Disparo {body['dispatch_id']}: {body['state']}"]
    if body.get("confirmed"):
        lines.append(f"  confirmação: confirmado em {body.get('confirmed_at')}")
    else:
        lines.append("  confirmação: não confirmado")
    question = body.get("question") or {}
    lines += [f"  repositório: {_terminal_safe(question.get('repo'))}",
              f"  commit: {_terminal_safe(question.get('commit'))}",
              f"  questão: {_terminal_safe(question.get('qid'))}"]
    if body.get("opens_at") is not None:
        lines.append(_moment_line("abertura", body["opens_at"], zone))
    if body.get("closes_at") is not None:
        lines.append(_moment_line("fechamento", body["closes_at"], zone))
    lines.append(f"  origem: {body.get('origin')} (declarada)")
    rejection = body.get("rejection")
    if isinstance(rejection, dict):
        lines.append(f"  motivo: {rejection.get('message')}")
        lines += [f"    - {_terminal_safe(detail)}" for detail in rejection.get("details") or []]
    if body.get("cancel_reason") is not None:
        lines.append(f"  cancelamento: {body['cancel_reason']}")
    if body.get("target_count") is not None:
        lines.append(f"  alunos-alvo: {body['target_count']}")
    deliveries = body.get("deliveries") or {}
    lines.append("  entregas:")
    lines += [f"    {key}: {deliveries.get(key, 0)}" for key in _DELIVERY_COUNT_KEYS]
    preview = body.get("preview")
    if isinstance(preview, dict):
        lines.append("  prévia:")
        if preview.get("title") is not None:
            lines.append(f"    título: {_terminal_safe(preview['title'])}")
        lines.append(f"    enunciado: {_terminal_safe(preview.get('statement', ''))}")
        for index, item in enumerate(preview.get("fields") or [], start=1):
            lines.append(f"    campo {index}: {item.get('type')}")
            for key, label in (("label", "rótulo"), ("suffix", "sufixo"), ("hint", "dica")):
                if item.get(key) is not None:
                    lines.append(f"      {label}: {_terminal_safe(item[key])}")
        lines.append(f"    alunos-alvo na validação: {preview.get('target_count')}")
        for warning in preview.get("warnings") or []:
            lines.append(f"    aviso {warning.get('kind')}: {_terminal_safe(warning.get('message', ''))}")
    return lines


def _dispatch_list_line(item: dict) -> str:
    """`<dispatch_id>  <state>  <opens_at>  <closes_at>  <qid>`."""
    return (f"{item['dispatch_id']}  {item['state']}  {_dash(item.get('opens_at'))}  "
            f"{_dash(item.get('closes_at'))}  {_terminal_safe(item['question']['qid'])}")


def _confirm_line(body: dict) -> str:
    """`Disparo confirmado: <id>; abre <em <opens_at> | agora>, fecha em <closes_at>; <n> alunos-alvo
    agora.`: no imediato, a abertura é o próprio momento da confirmação."""
    opens = "agora" if body.get("opens_at") == body.get("confirmed_at") else f"em {body.get('opens_at')}"
    return (f"Disparo confirmado: {body['dispatch_id']}; abre {opens}, fecha em {body.get('closes_at')}; "
            f"{body.get('target_count')} alunos-alvo agora.")


@cli.group("dispatches")
def dispatches_group() -> None:
    """Disparos de questão aos alunos do oferecimento."""


@dispatches_group.command("prepare")
@click.argument("ref")
@click.option("--repo", required=True, help="URL https do repositório da questão.")
@click.option("--commit", required=True, help="SHA do commit (40 ou 64 dígitos hexadecimais).")
@click.option("--qid", required=True, help="Caminho da questão no repositório.")
@click.option("--opens-at", "opens_at", default=None,
              help="Abertura, ISO 8601 com deslocamento (ex.: 2031-01-02T09:00:00-03:00); sem ela, imediato.")
@click.option("--closes-at", "closes_at", default=None,
              help="Fechamento, ISO 8601 com deslocamento; sem ele, a janela do oferecimento.")
def dispatches_prepare(ref: str, repo: str, commit: str, qid: str, opens_at: str | None,
                       closes_at: str | None) -> None:
    """Prepara um disparo; a validação roda no worker (dispatches.prepare)."""
    body = _request(lambda api: api.prepare_dispatch(ref, repo=repo, commit=commit, qid=qid, opens_at=opens_at,
                                                     closes_at=closes_at))
    _emit(body, lambda b: [
        f"Disparo preparado em {b['offering']['code']}: {b['dispatch_id']} (validando).",
        f"Consulte com: cq dispatches show {b['offering']['code']} {b['dispatch_id']}"])


@dispatches_group.command("show")
@click.argument("ref")
@click.argument("dispatch_id")
def dispatches_show(ref: str, dispatch_id: str) -> None:
    """Mostra um disparo, com a prévia depois da validação (dispatches.get)."""
    body = _request(lambda api: api.get_dispatch(ref, dispatch_id))
    _emit(body, _dispatch_lines)


@dispatches_group.command("list")
@click.argument("ref")
@click.option("--after", type=int, default=None, help="Cursor: só disparos depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
def dispatches_list(ref: str, after: int | None, limit: int | None) -> None:
    """Lista os disparos do oferecimento em ordem de preparo (dispatches.list)."""
    body = _request(lambda api: api.list_dispatches(ref, after=after, limit=limit))
    _emit(body, lambda b: [*(_dispatch_list_line(item) for item in b["items"]),
                           f"Próximo cursor: {b['next_cursor']}"])


@dispatches_group.command("confirm")
@click.argument("ref")
@click.argument("dispatch_id")
def dispatches_confirm(ref: str, dispatch_id: str) -> None:
    """Confirma um disparo validado: envia a questão à turma no horário (dispatches.confirm)."""
    body = _request(lambda api: api.confirm_dispatch(ref, dispatch_id))
    _emit(body, lambda b: [_confirm_line(b)])


@dispatches_group.command("cancel")
@click.argument("ref")
@click.argument("dispatch_id")
def dispatches_cancel(ref: str, dispatch_id: str) -> None:
    """Cancela um disparo que ainda não abriu (dispatches.cancel)."""
    body = _request(lambda api: api.cancel_dispatch(ref, dispatch_id))
    _emit(body, lambda b: [f"Disparo cancelado: {b['dispatch_id']}."])


@cli.group("messages")
def messages_group() -> None:
    """Mensagens livres dos alunos."""


@messages_group.command("list")
@click.argument("ref")
@click.option("--after", type=int, default=None, help="Cursor: só mensagens depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
def messages_list(ref: str, after: int | None, limit: int | None) -> None:
    """Lista as mensagens livres dos alunos, com o texto redigido (messages.list)."""
    body = _request(lambda api: api.list_free_messages(ref, after=after, limit=limit))
    _emit(body, lambda b: [*(f"{item['created_at']}  {item['alias']}  {_terminal_safe(item['text'])}"
                             for item in b["items"]),
                           f"Próximo cursor: {b['next_cursor']}"])


# --- relatórios do disparo ----------------------------------------------------------------------

_EMPTY = "—"
REPORT_LAST_LINE = "Distribuição e fricção: use --json ou --csv."


def _report_local(moment: Any, zone: str) -> str:
    """`AAAA-MM-DD HH:MM <fuso>` (a hora local do relatório); `fuso indisponível` sem o fuso no cliente."""
    try:
        local = datetime.fromisoformat(str(moment).replace("Z", "+00:00")).astimezone(zoneinfo.ZoneInfo(zone))
        return f"{local.strftime('%Y-%m-%d %H:%M')} {zone}"
    except Exception:  # noqa: BLE001 - a linha sai mesmo sem o fuso
        return ZONE_UNAVAILABLE


def _count_pair(count: dict) -> str:
    return f"{count['on_time']} no prazo · {count['with_late']} com atrasadas"


def _percent(value: Any) -> str:
    return _EMPTY if value is None else f"{value * 100:.1f}".replace(".", ",") + "%"


def _rate_pair(rate: dict) -> str:
    return f"{_percent(rate['on_time'])} no prazo · {_percent(rate['with_late'])} com atrasadas"


def _duration(ms: Any) -> str:
    return _EMPTY if ms is None else f"{ms:.0f} ms"


def _report_lines(body: dict) -> list[str]:
    """Texto de `reports show`: cabeçalho, alcance, desempenho e tempo até
    responder, cada par como `<a> no prazo · <b> com atrasadas` e `—` no vazio; a distribuição e a
    fricção só no `--json` ou no `--csv`. O texto de terceiros pelo `_terminal_safe`."""
    dispatch, reach, perf, times = body["dispatch"], body["reach"], body["performance"], body["response_time"]
    zone = body.get("timezone") or "UTC"
    frozen = body.get("frozen")
    question = dispatch["question"]
    title = dispatch.get("title")
    state = f"Estado: {dispatch['state']}" + (" (parcial)" if dispatch.get("partial") else "")
    version = ("calculado agora" if frozen is None
               else f"versão {frozen['version']} ({frozen['trigger']}, {frozen['frozen_at']})")
    by_reason = ", ".join(f"{key} {value}" for key, value in reach["not_delivered_by_reason"].items())
    by_stage = ", ".join(f"{key} {value}" for key, value in reach["question_failed_by_stage"].items())
    return [
        f"Relatório do disparo {dispatch['dispatch_id']} em {body['offering']['code']}",
        f"Questão: {_terminal_safe(question['qid'])} @ {question['commit_short']}",
        f"Título: {_EMPTY if title is None else _terminal_safe(title)}",
        f"Abertura: {dispatch['opens_at']} ({_report_local(dispatch['opens_at'], zone)})",
        f"Fechamento: {dispatch['closes_at']} ({_report_local(dispatch['closes_at'], zone)})",
        state,
        f"origem: {dispatch['origin']} (declarada)",
        version,
        (f"Alcance: {reach['target']} alunos-alvo, {reach['delivered']} entregues, {reach['not_delivered']} não "
         f"entregues, {reach['question_failed']} em falha de questão, {reach['pending']} pendentes"),
        f"Não entregues por motivo: {by_reason}",
        f"Falha de questão por estágio: {by_stage}",
        f"Respondidas: {_count_pair(perf['answered'])}",
        f"Corretas: {_count_pair(perf['correct'])}",
        f"Parcialmente corretas: {_count_pair(perf['partially_correct'])}",
        f"Incorretas: {_count_pair(perf['incorrect'])}",
        f"Não sei: {_count_pair(perf['dont_know'])}",
        f"Taxa de resposta: {_rate_pair(perf['response_rate'])}",
        f"Taxa de corretas: {_rate_pair(perf['correct_rate'])}",
        f"Taxa de parcialmente corretas: {_rate_pair(perf['partially_correct_rate'])}",
        f"Taxa de incorretas: {_rate_pair(perf['incorrect_rate'])}",
        f"Nota média: {_rate_pair(perf['mean_score'])}",
        (f"Tempo até responder, no prazo: mediana {_duration(times['median_ms'])}, primeiro quartil "
         f"{_duration(times['q1_ms'])}, terceiro quartil {_duration(times['q3_ms'])}; {times['observations']} "
         f"observações, {times['negative']} negativas"),
        f"Correções pendentes: {body['grading_pending']}",
        REPORT_LAST_LINE,
    ]


@cli.group("reports")
def reports_group() -> None:
    """Relatórios dos disparos."""


@reports_group.command("show")
@click.argument("ref")
@click.argument("dispatch_id")
@click.option("--version", "version", default=None, help="Versão congelada (número ou latest); sem ela, agora.")
@click.option("--csv", "as_csv", is_flag=True, help="Imprime o CSV da API como veio (não combina com --json).")
def reports_show(ref: str, dispatch_id: str, version: str | None, as_csv: bool) -> None:
    """Mostra o relatório de um disparo, calculado agora ou de uma versão congelada (reports.get)."""
    if as_csv:
        if _options().as_json:
            raise _Usage("--csv não combina com --json.")
        text = _request(lambda api: api.get_report_csv(ref, dispatch_id, version=version))
        # Os bytes como vieram (BOM e CRLF), sem `_terminal_safe` nem quebra de linha a mais.
        click.echo(text.encode("utf-8"), nl=False)
        return
    body = _request(lambda api: api.get_report(ref, dispatch_id, version=version))
    _emit(body, _report_lines)


@reports_group.command("versions")
@click.argument("ref")
@click.argument("dispatch_id")
@click.option("--after", type=int, default=None, help="Cursor: só versões depois dele.")
@click.option("--limit", type=int, default=None, help="Máximo de itens (1 a 500).")
def reports_versions(ref: str, dispatch_id: str, after: int | None, limit: int | None) -> None:
    """Lista as versões congeladas do relatório de um disparo (reports.list_versions)."""
    body = _request(lambda api: api.list_report_versions(ref, dispatch_id, after=after, limit=limit))
    _emit(body, lambda b: [*(f"{item['version']}  {item['trigger']}  {item['frozen_at']}" for item in b["items"]),
                           f"Próximo cursor: {b['next_cursor']}"])


@reports_group.command("resend")
@click.argument("ref")
@click.argument("dispatch_id")
def reports_resend(ref: str, dispatch_id: str) -> None:
    """Congela a versão seguinte do relatório de um disparo fechado e reenvia o resumo à equipe
    (reports.resend_summary)."""
    body = _request(lambda api: api.resend_report_summary(ref, dispatch_id))
    _emit(body, lambda b: [f"Resumo reenviado: {b['dispatch_id']}, versão {b['frozen']['version']}."])


def main() -> None:
    """Ponto de entrada do script `cq`."""
    cli()
