"""Sigilo do token de bot, da chave-mestra e dos códigos de convite: o token nunca aparece em
texto claro.


Cada teste exercita um conjunto de caminhos e depois varre **todas** as superfícies que viu:
- as respostas da API: corpo, cabeçalhos e a URL do pedido (`_Exchange`);
- as saídas do CLI (saída padrão, de erro e combinada);
- os logs do sistema (`capture_logs`, com o `JsonFormatter` real) e os do transporte HTTP
  (`capture_transport_logs`, em DEBUG nos loggers `httpx2` e `httpcore2`: a URL da Bot API contém o
  token);
- a saída padrão e a de erro do processo (fd 1 e 2, `capfd`) e, no teste do subprocesso, as do
  `cq-server worker` de verdade;
- os eventos (pela API) e os parâmetros enviados à Bot API falsa;
- **todas** as colunas de **todas** as tabelas do banco (`scan_all_tables`, com `bytea` em hex,
  `escape` e base64), procurando também as formas hex, base64 e base64url dos tokens e da chave.

Os segredos são os de `all_secrets()` (tokens da Bot API falsa, inteiros e a parte secreta; links e
códigos de convite; segredos de token da API) e os de `bot_secrets()` (as chaves-mestras de teste),
com o SHA-256 hex de cada um (`find_secrets`). As **únicas** exceções, cada uma com igualdade exata e
controle positivo: o corpo do 201 de `team.link` e a saída do `cq team link` num terminal (só o próprio
link e o seu código), o 201 de `tokens.create` (só o próprio segredo, como na API base), o texto literal do
`/start <código>` em `telegram_updates.text` e as colunas de resumo (`invites.code_sha256`,
`api_tokens.secret_sha256`). O código de confirmação do arquivamento não é segredo.
Os controles positivos provam que cada varredura acharia um vazamento: a coluna cifrada decifra de
forma independente para o token, uma tabela temporária com o token é acusada, um `httpx2.Client` comum
registra o token na captura do transporte, uma escrita no fd 2 é achada, e as varreduras dos logs
(sistema e transporte) e dos parâmetros ao Telegram acusam um registro e um parâmetro sintéticos com o
token. As checagens contra o vazio provam que as varreduras de log não passaram vazias: a captura do
transporte tem os registros DEBUG do `httpcore2` do próprio adaptador (a conexão com a porta da Bot
API falsa), e a dos logs do sistema tem os registros dos caminhos exercitados (`request` do
`bots.register`; `bot.lease`, `bot.poll_problem`, `telegram.update` e `task.processed` do worker).

Com as mensagens livres opcionais e o contato do oferecimento, o
`Offering` traz `free_messages` e `contact_email_set`; o corpo exato esperado do oferecimento cadastrado sem
esses campos traz os dois, falsos (o padrão).
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import time
import uuid
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import click
import httpx2
import psycopg
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

import cq_client.cli as cq_cli
from cq_server.adapters.console import ConsoleChannel
from cq_server.botmessages import (ALREADY_LINKED, COMMANDS, DESCRIPTION_ACTIVE, DESCRIPTION_ARCHIVED,
                                   HELP, LINK_INVALID, WELCOME_TEAM)
from cq_server.events import EventType
from tests.helpers.api import (ADMIN_TOKEN, BOT_TOKEN_SHAPE, TEAM_LINK_SHAPE, activate_bot, all_events,
                               auth, create_offering, create_team_link, create_token, link_code,
                               register_bot, register_person)
from tests.helpers.bot_api import make_user
from tests.helpers.canary import find_secrets
from tests.helpers.clock import iso
from tests.helpers.contract import MESSAGES, assert_error, assert_matches, load_catalog
from tests.helpers.db import scan_all_tables
from tests.helpers.logs import Captured, capture_logs, capture_transport_logs
from tests.helpers.processes import spawn_worker
from tests.helpers.secrets import MASTER_KEY, OTHER_MASTER_KEY, all_secrets, bot_secrets, remember_secret

ADMIN = auth(ADMIN_TOKEN)
EVENT_TYPES = [t.value for t in EventType]
# Relatórios e acompanhamento: os 3 tipos
# de evento dos relatórios, em literal (o teste os exclui da lista exata de eventos e confere o excluído).
REPORT_EVENT_TYPES = ("report.frozen", "notice.recorded", "offering.digest_changed")
TIMEZONE = "America/Sao_Paulo"
PROMPT_LABEL = "Token do bot"  # cli.prompt_secret("Token do bot")
TEAM_LINK_VALIDITY = timedelta(hours=168)  # padrão de `valid_hours`
ARCHIVE_CODE_VALIDITY = timedelta(minutes=10)  # validade do código de confirmação do arquivamento
# Avanço do FakeClock que libera qualquer espera do sistema nestes testes: a espera crescente da
# consulta e as esperas das filas (tarefas e saída) chegam no máximo a 60 s (conftest: backoff máximo 60).
RELEASE = 61

WORKER_ID = "w-segredo-bots"
OTHER_KEY_WORKER_ID = "w-segredo-outra-chave"
PROCESS_WORKER_ID = "w-segredo-processo"
PROFILE_WORKER_ID = "w-segredo-perfil"
PROFILE_OFFERING = "segredo-perfil"
PROFILE_TASK = "bot.sync_profile"  # a tarefa que sincroniza o perfil do bot
PROFILE_METHODS = ("setMyDescription", "setMyCommands")
# Do erro de um tratador, a fila guarda só o nome do tipo (`TaskQueue.work_once`).
TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REGISTER_OFFERING = "segredo-registro"
OTHER_OFFERING = "segredo-registro-outro"
ARCHIVED_OFFERING = "segredo-arquivado"
WORKER_OFFERING = "segredo-worker"
OTHER_KEY_OFFERING = "segredo-outra-chave"
CLI_OFFERING = "segredo-cli"
PROCESS_OFFERING = "segredo-processo"
REDACTION_OFFERING = "segredo-redacao"
INVALID_CODE = "CodigoQueNaoExiste0000"  # 22 caracteres do alfabeto do código, sem convite
UNKNOWN_BOT_TOKEN = "1234568:" + "Zz9_-" * 7  # formato local válido, desconhecido da Bot API falsa
DEFAULT_COURSE, DEFAULT_TERM = "Algoritmos e Programação", "2031s1"  # os de `create_offering`

OFFERING_BOT_OPERATIONS = ("offerings.create", "offerings.list", "offerings.get", "offerings.close",
                 "offerings.prepare_archive", "offerings.archive", "bots.register", "bots.activate",
                 "bots.get", "team.link")
OFFERING_BOT_EVENT_TYPES = (
    "offering.created", "offering.state_changed", "offering.archive_prepared", "bot.registered",
    "bot.token_replaced", "bot.profile_updated", "bot.lease_changed", "bot.access_changed",
    "team.link_created", "team.link_revoked", "team.joined", "team.join_refused", "endpoint.blocked",
    "outbox.reply_failed")
OFFERING_BOT_CLI_LEAVES = frozenset({
    "offerings create", "offerings list", "offerings show", "offerings close",
    "offerings archive prepare", "offerings archive confirm", "bots register", "bots activate",
    "bots show", "team link"})
SUCCESS_STATUS = {"offerings.create": 201, "team.link": 201}
UUID4_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
assert re.fullmatch(r"[A-Za-z0-9_-]{22}", INVALID_CODE)
assert BOT_TOKEN_SHAPE.fullmatch(UNKNOWN_BOT_TOKEN)


# ---------------------------------------------------------------------------------------------
# Superfícies gravadas
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Exchange:
    """Uma resposta da API vista pelo cliente do teste, com a URL do pedido."""

    method: str
    url: str
    path: str
    status: int
    headers: str
    body: str

    @property
    def is_team_link_creation(self) -> bool:
        return (self.method == "POST" and self.status == 201
                and re.fullmatch(r"/v1/offerings/[^/]+/team-links", self.path) is not None)

    @property
    def is_token_creation(self) -> bool:
        return self.method == "POST" and self.path == "/v1/tokens" and self.status == 201

    def own_secrets(self) -> set[str]:
        """Os segredos que esta resposta pode conter (a exceção exata): o link e o código do 201 de
        `team.link` e o segredo do 201 de `tokens.create`."""
        try:
            body = json.loads(self.body)
        except ValueError:
            return set()
        if self.is_team_link_creation:
            return _link_secrets(body.get("link") if isinstance(body, dict) else None)
        if self.is_token_creation and isinstance(body, dict) and isinstance(body.get("secret"), str):
            return {body["secret"]}
        return set()


def _link_secrets(link: Any) -> set[str]:
    match = TEAM_LINK_SHAPE.fullmatch(link) if isinstance(link, str) else None
    return set() if match is None else {link, match.group(2)}


def _record(http: Any, sink: list[_Exchange]) -> None:
    """Grava toda resposta que passa pelo cliente HTTP (API, helpers e CLI em processo); o link e o
    código de todo 201 de `team.link` entram no registro de segredos."""

    def hook(response: Any) -> None:
        response.read()
        request = response.request
        exchange = _Exchange(method=request.method, url=str(request.url), path=request.url.path,
                             status=response.status_code,
                             headers="\n".join(f"{k}: {v}" for k, v in response.headers.multi_items()),
                             body=response.text)
        sink.append(exchange)
        if exchange.is_team_link_creation or exchange.is_token_creation:
            for secret in sorted(exchange.own_secrets()):
                remember_secret(secret)

    hooks = http.event_hooks
    http.event_hooks = {"request": list(hooks["request"]), "response": [*hooks["response"], hook]}


@dataclass(frozen=True)
class _CliRun:
    leaf: str
    args: tuple[str, ...]
    json: bool
    terminal: bool
    exit_code: int
    stdout: str
    stderr: str
    output: str

    @property
    def texts(self) -> tuple[str, str, str]:
        return (self.stdout, self.stderr, self.output)


def _leaf_of(args: list[str]) -> str | None:
    command: click.Command = cq_cli.cli
    path: list[str] = []
    for arg in args:
        if not isinstance(command, click.Group):
            break
        sub = command.get_command(click.Context(command), arg)
        if sub is None:
            return None
        path.append(arg)
        command = sub
    if not path or isinstance(command, click.Group):
        return None
    return " ".join(path)


class _Cli:
    """O CLI em processo (fixture `cli_invoke`), com o terminal simulado por execução:
    `cli.stdout_is_terminal`, `cli.stdin_is_terminal` e `cli.prompt_secret` (este grava o rótulo e
    devolve `prompt_answer`; sem resposta roteirizada, falha)."""

    def __init__(self, cli_invoke: Any, monkeypatch: Any) -> None:
        self._invoke = cli_invoke
        self._monkeypatch = monkeypatch
        self.runs: list[_CliRun] = []
        self.prompts: list[str] = []

    def run(self, args: list[str], exit_code: int, *, json_mode: bool = False, stdout_terminal: bool = False,
            stdin_terminal: bool = False, prompt_answer: str | None = None, **kwargs: Any) -> _CliRun:
        def prompt(label: str) -> str:
            self.prompts.append(label)
            assert prompt_answer is not None, f"prompt_secret({label!r}) chamado sem resposta roteirizada"
            return prompt_answer

        self._monkeypatch.setattr(cq_cli, "stdout_is_terminal", lambda: stdout_terminal)
        self._monkeypatch.setattr(cq_cli, "stdin_is_terminal", lambda: stdin_terminal)
        self._monkeypatch.setattr(cq_cli, "prompt_secret", prompt)
        leaf = _leaf_of(args)
        assert leaf is not None, f"{args} não é um comando folha do cli"
        result = self._invoke(list(args), json=json_mode, **kwargs)
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"{args}: o CLI levantou {type(result.exception).__name__}")
        assert result.exit_code == exit_code, (
            f"{args} (json={json_mode}): código {result.exit_code} != {exit_code}; stderr: {result.stderr[:300]}")
        for text in (result.stdout, result.stderr):
            assert "Traceback" not in text, f"{args}: rastreamento de pilha na saída"
        if exit_code != 0:
            assert result.stdout == "", f"{args}: erro com saída padrão não vazia"
        run = _CliRun(leaf, tuple(args), json_mode, stdout_terminal, result.exit_code, result.stdout,
                      result.stderr, result.output)
        self.runs.append(run)
        return run


@dataclass
class _Surfaces:
    exchanges: list[_Exchange]
    logs: Captured
    transport: Captured
    caught: list[warnings.WarningMessage]


@contextlib.contextmanager
def _sweep(*clients: Any) -> Iterator[_Surfaces]:
    """Grava as respostas dos clientes dados e captura os logs do sistema, os do transporte e os
    avisos durante o bloco."""
    exchanges: list[_Exchange] = []
    for http in clients:
        _record(http, exchanges)
    with capture_transport_logs() as transport, capture_logs() as logs, \
            warnings.catch_warnings(record=True) as caught:
        yield _Surfaces(exchanges, logs, transport, caught)


# ---------------------------------------------------------------------------------------------
# Segredos e varreduras
# ---------------------------------------------------------------------------------------------


def _secrets(bot_api: Any) -> list[str]:
    """Todos os segredos conhecidos, mais as formas que um vazamento no banco poderia ter: hex,
    base64 e base64url de cada token de bot e da chave-mestra decodificada."""
    base = all_secrets() + bot_secrets()
    tokens = [s for s in base if BOT_TOKEN_SHAPE.fullmatch(s)] + bot_api.all_tokens()
    derived: list[str] = []
    for token in dict.fromkeys(tokens):
        raw = token.encode("ascii")
        derived += [raw.hex(), base64.b64encode(raw).decode().rstrip("="),
                    base64.urlsafe_b64encode(raw).decode().rstrip("=")]
    for key in bot_secrets():
        raw = base64.urlsafe_b64decode(key + "=")
        derived += [raw.hex(), base64.b64encode(raw).decode().rstrip("=")]
    return list(dict.fromkeys(base + derived))


def _assert_exchanges_clean(exchanges: list[_Exchange], secrets: list[str]) -> None:
    """Nenhum segredo em corpo, cabeçalho ou URL; no corpo do 201 de `team.link` e de `tokens.create`,
    só o próprio segredo fica de fora (e é achado: controle positivo)."""
    leaks = []
    for ex in exchanges:
        own = ex.own_secrets()
        body_secrets = [s for s in secrets if s not in own]
        hits = sorted(set(find_secrets(ex.headers, secrets)) | set(find_secrets(ex.body, body_secrets))
                      | set(find_secrets(ex.url, secrets)))
        if hits:
            leaks.append((ex.method, ex.path, ex.status, hits))
        if own:
            assert find_secrets(ex.body, sorted(own)), f"o detector não achou o segredo no 201 de {ex.path}"
    assert leaks == [], f"segredo em respostas da API (corpo, cabeçalhos ou URL): {leaks}"
    assert exchanges and {ex.url.split("/")[2] for ex in exchanges} == {"testserver"}, "URL não gravada"


def _assert_cli_clean(runs: list[_CliRun], secrets: list[str], allowed: dict[int, set[str]] | None = None) -> None:
    """Nenhum segredo nas saídas do CLI; `allowed` (índice da execução → segredos) é a exceção exata da
    saída do `cq team link` num terminal (o próprio link e o seu código)."""
    allowed = allowed or {}
    leaks = []
    for i, run in enumerate(runs):
        scan = [s for s in secrets if s not in allowed.get(i, set())]
        hits = sorted({h for text in run.texts for h in find_secrets(text, scan)})
        if hits:
            leaks.append((run.args, run.json, hits))
    assert leaks == [], f"segredo na saída do CLI: {leaks}"
    for i, own in allowed.items():
        assert runs[i].leaf == "team link" and runs[i].terminal and runs[i].exit_code == 0, runs[i]
        assert find_secrets(runs[i].stdout, sorted(own)), "o detector não achou o link na saída do terminal"


def _assert_logs_clean(surfaces: _Surfaces, secrets: list[str]) -> None:
    """Logs do sistema (JSON) e do transporte HTTP sem segredo."""
    assert all("_raw" not in record for record in surfaces.logs.records), "linha de log que não é JSON"
    system = [(i, hits) for i, line in enumerate(surfaces.logs.lines) if (hits := find_secrets(line, secrets))]
    assert system == [], f"segredo nos logs do sistema: {system}"
    transport = [(i, hits) for i, line in enumerate(surfaces.transport.lines)
                 if (hits := find_secrets(line, secrets))]
    assert transport == [], f"segredo nos logs do transporte (httpx2/httpcore2): {transport}"


def _check_fd(capfd: Any, caught: list[warnings.WarningMessage], secrets: list[str], probe: str) -> None:
    """Saída padrão e de erro do processo (fd 1 e 2, inclusive das threads) e avisos sem segredo;
    controle positivo: `probe` escrito direto no fd 2 é achado. Os avisos voltam ao resumo do pytest."""
    out, err = capfd.readouterr()
    for channel, text in (("saída padrão", out), ("saída de erro", err)):
        hits = find_secrets(text, secrets)
        assert hits == [], f"segredo na {channel} do processo: {hits}"
    warned = [(w.category.__name__, find_secrets(str(w.message), secrets)) for w in caught]
    assert [w for w in warned if w[1]] == [], f"segredo em avisos: {warned}"
    os.write(2, f"sonda sintetica {probe}\n".encode())
    _, err = capfd.readouterr()
    assert find_secrets(err, [probe]), "a captura do fd 2 não pegou a escrita sintética"
    for w in caught:
        warnings.warn_explicit(w.message, w.category, w.filename, w.lineno, source=w.source)


def _assert_telegram_params_clean(bot_api: Any, secrets: list[str]) -> None:
    """Nenhum segredo nos parâmetros enviados à Bot API falsa nem nos textos entregues."""
    params = [(c.method, c.seq) for c in bot_api.calls()
              if find_secrets(json.dumps(c.params, ensure_ascii=False), secrets)]
    assert params == [], f"segredo nos parâmetros enviados ao Telegram: {params}"
    texts = [m.call_seq for m in bot_api.sent_messages() if find_secrets(m.text, secrets)]
    assert texts == [], f"segredo nos textos enviados ao Telegram: {texts}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _db_secret_hits(conninfo: Any, secrets: list[str]) -> dict[tuple[str, str], list[tuple[str, list[str]]]]:
    """`(tabela, coluna)` → [(texto, rótulos)] das células com algum segredo (ou o seu resumo)."""
    found: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for table, column, text in scan_all_tables(conninfo):
        hits = find_secrets(text, secrets)
        if hits:
            found.setdefault((table, column), []).append((text, hits))
    return found


def _assert_database_clean(db_url: str, secrets: list[str], *, invite_codes: list[str],
                           start_texts: list[str], api_secrets: list[str]) -> None:
    """Varredura genérica do banco (todas as colunas de todas as tabelas). Exceções exatas, com
    controle positivo: os resumos de `invites.code_sha256` (os códigos dados) e de
    `api_tokens.secret_sha256` (os segredos dados) e o texto literal do `/start <código>` em
    `telegram_updates.text`. Nada mais pode ter segredo."""
    hits = _db_secret_hits(db_url, secrets)
    invites = hits.pop(("invites", "code_sha256"), [])
    assert sorted(text for text, _ in invites) == sorted(_digest(code) for code in invite_codes), invites
    assert all(labels == [f"resumo:{text[:8]}…"] for text, labels in invites), invites
    tokens = hits.pop(("api_tokens", "secret_sha256"), [])
    assert sorted(text for text, _ in tokens) == sorted(_digest(secret) for secret in api_secrets), tokens
    assert all(labels == [f"resumo:{text[:8]}…"] for text, labels in tokens), tokens
    starts = hits.pop(("telegram_updates", "text"), [])
    assert sorted(text for text, _ in starts) == sorted(start_texts), starts
    for text, labels in starts:
        code = text.split(" ", 1)[1]
        assert labels == [f"segredo:{code[:6]}…"], (text, labels)
    assert hits == {}, f"segredo no banco fora das exceções: {sorted(hits)}"


def _decrypt_independently(blob: bytes, master_key: str, bot_uuid: uuid.UUID, telegram_bot_id: int) -> str:
    """Decifra o blob v1 com os primitivos da `cryptography`, sem `secretbox`."""
    ikm = base64.urlsafe_b64decode(master_key + "=")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"cq/bot-token/aes-256-gcm/v1").derive(ikm)
    key_id = HKDF(algorithm=hashes.SHA256(), length=4, salt=None, info=b"cq/master-key-id/v1").derive(ikm)
    assert blob[0] == 1 and blob[1:5] == key_id, "o blob não é v1 desta chave-mestra"
    aad = (b"cq/bot-token/v1\x00" + blob[0:5] + bot_uuid.bytes
           + telegram_bot_id.to_bytes(8, "big", signed=True))
    return AESGCM(key).decrypt(blob[5:17], blob[17:], aad).decode("ascii")


def _assert_ciphertexts(sql: psycopg.Connection, expected: dict[int, str]) -> None:
    """Controle positivo da varredura do banco: a coluna cifrada de cada bot decifra, de forma
    independente e com a chave-mestra de teste, exatamente para o token atual dele."""
    rows = sql.execute("SELECT telegram_bot_id, bot_uuid, token_ciphertext FROM bots ORDER BY id").fetchall()
    assert {tid: _decrypt_independently(bytes(blob), MASTER_KEY, bot_uuid, tid) for tid, bot_uuid, blob in rows} == (
        expected)


def _assert_scan_catches_temp_table(db_url: str, token: str) -> None:
    """Controle positivo: `scan_all_tables` acha o token numa tabela temporária, em texto e em
    `bytea`."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("CREATE TEMP TABLE cq_sonda_segredo (texto text, bruto bytea)")
        conn.execute("INSERT INTO cq_sonda_segredo (texto, bruto) VALUES (%s, %s)",
                     (f"token vazado {token}", token.encode("ascii")))
        found = {key for key in _db_secret_hits(conn, [token]) if key[0] == "cq_sonda_segredo"}
    assert found == {("cq_sonda_segredo", "texto"), ("cq_sonda_segredo", "bruto")}, found


def _assert_transport_capture_catches(bot_api: Any, token: str) -> None:
    """Controle positivo da captura do transporte: um `httpx2.Client` comum registra a URL com o
    token; a mesma captura e o mesmo detector o acham."""
    with capture_transport_logs() as probe, httpx2.Client(timeout=5, trust_env=False) as http:
        resp = http.post(f"{bot_api.base_url}/bot{token}/getMe", json={})
    assert resp.status_code in (200, 401), resp.status_code
    assert find_secrets(probe.text, [token]), "a captura do transporte não acharia o token"


def _assert_transport_exercised(transport: Captured, bot_api: Any) -> None:
    """Contra o vazio da varredura do transporte (com o transporte direto, o
    `httpcore2` registra em DEBUG, sem o token): a captura tem os registros do próprio adaptador, a
    conexão com a porta da Bot API falsa e o envio de pedidos. Nada mais no processo usa o `httpcore2`
    durante a captura (o cliente do teste e o CLI em processo vão pelo `TestClient`)."""
    records = transport.records
    assert transport.lines and all("_raw" not in r for r in records), "captura do transporte vazia ou fora de JSON"
    connects = [r for r in records if r.get("logger") == "httpcore2.connection"
                and str(r.get("message", "")).startswith("connect_tcp.started")
                and f"port={bot_api.port}" in str(r.get("message", ""))]
    sends = [r for r in records if str(r.get("logger", "")).startswith("httpcore2.")
             and str(r.get("message", "")).startswith("send_request_headers.started")]
    assert connects and sends, f"sem registros do httpcore2 do adaptador: {transport.lines[:5]}"


def _assert_sweeps_catch(token: str) -> None:
    """Controles positivos das varreduras dos logs e dos parâmetros ao Telegram, com as mesmas
    funções: uma linha JSON do sistema com o token (na forma dos logs do sistema, só ASCII), uma
    linha do transporte com o token, um parâmetro e um texto enviados com o token são acusados."""
    line = json.dumps({"level": "INFO", "logger": "cq_server.worker", "event": f"sintetico /bot{token}/getUpdates"},
                      ensure_ascii=True)
    system = Captured()
    system.stream.write(line + "\n")
    with pytest.raises(AssertionError, match="segredo nos logs do sistema"):
        _assert_logs_clean(_Surfaces([], system, Captured(), []), [token])
    transport = Captured()
    transport.stream.write(json.dumps({"logger": "httpcore2.http11", "message": f"sintetico {token}"}) + "\n")
    with pytest.raises(AssertionError, match="segredo nos logs do transporte"):
        _assert_logs_clean(_Surfaces([], Captured(), transport, []), [token])
    sent_call = SimpleNamespace(method="sendMessage", seq=1, params={"chat_id": 1, "text": f"sintetico {token}"})
    with pytest.raises(AssertionError, match="segredo nos parâmetros enviados ao Telegram"):
        _assert_telegram_params_clean(SimpleNamespace(calls=lambda: [sent_call], sent_messages=lambda: []), [token])
    sent_text = SimpleNamespace(call_seq=1, text=f"sintetico {token}")
    with pytest.raises(AssertionError, match="segredo nos textos enviados ao Telegram"):
        _assert_telegram_params_clean(SimpleNamespace(calls=lambda: [], sent_messages=lambda: [sent_text]), [token])


def _worker_records(surfaces: _Surfaces) -> list[dict]:
    return [r for r in surfaces.logs.records if r.get("logger") == "cq_server.worker"]


def _last_seq(bot_api: Any) -> int:
    return max((c.seq for c in bot_api.calls()), default=0)


def _wait_until(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    """Espera real por outra thread ou processo, com prazo (o resultado vem do sistema)."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        assert time.monotonic() < deadline, f"prazo de {timeout} s esgotado: {what}"
        time.sleep(0.05)


def _advance_until(clock: Any, seconds: float, predicate: Callable[[], Any], what: str, *,
                   rounds: int = 5, wait: float = 5.0) -> None:
    """Libera uma espera do sistema avançando o `FakeClock` e espera, com
    prazo real, o efeito. Se a thread do sistema ainda não tinha marcado o prazo quando o relógio andou
    (corrida entre o teste e a thread), o prazo ficou à frente: o relógio anda de novo, até `rounds`."""
    for _ in range(rounds):
        clock.advance(seconds)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
    raise AssertionError(f"{what}: nada depois de {rounds} avanços de {seconds} s no relógio")


def _compose(template: str, course: str, term: str) -> str:
    return template.replace("{course}", course).replace("{term}", term)


def _has_record(records: list[dict], expected: dict) -> bool:
    return any(expected.items() <= record.items() for record in records)


def _requests_per_operation(records: list[dict]) -> dict[str, bool]:
    """Para cada operação de `OFFERING_BOT_OPERATIONS`: existe um registro `request` do sucesso dela?"""
    requests = [r for r in records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]
    ops = {op["id"]: op for op in load_catalog()["operations"]}
    return {op_id: _has_record(requests, {"method": ops[op_id]["method"], "route": ops[op_id]["path"],
                                          "status": SUCCESS_STATUS.get(op_id, 200)})
            for op_id in OFFERING_BOT_OPERATIONS}


def _offering_events(events: list[dict], offering_id: str, event_type: str) -> list[dict]:
    return [e["data"] for e in sorted(events, key=lambda e: e["seq"])
            if e["type"] == event_type and e["data"].get("offering_id") == offering_id]


def _update_state(sql: psycopg.Connection, bot_id: int, update_id: int) -> tuple | None:
    return sql.execute(
        "SELECT u.state, u.outcome FROM telegram_updates u JOIN bots b ON b.id = u.bot_id "
        "WHERE b.telegram_bot_id = %s AND u.update_id = %s", (bot_id, update_id)).fetchone()


def _bot_row(sql: psycopg.Connection, columns: str, bot_id: int) -> tuple | None:
    return sql.execute(f"SELECT {columns} FROM bots WHERE telegram_bot_id = %s", (bot_id,)).fetchone()


def _quiet(sql: psycopg.Connection) -> bool:
    """Nenhuma tarefa nem mensagem pendente ou em posse."""
    (busy,) = sql.execute(
        "SELECT (SELECT count(*) FROM tasks WHERE state IN ('pending', 'leased')) "
        "+ (SELECT count(*) FROM outbox_messages WHERE state IN ('pending', 'leased'))").fetchone()
    return busy == 0


def _profile_task(sql: psycopg.Connection, internal: int, token_version: int) -> tuple | None:
    """(estado, tentativas, `available_at`, `last_error`) da tarefa do perfil da versão do token dada,
    pela chave de idempotência (`bot.sync_profile:<id>:<token_version>:active`)."""
    row = sql.execute("SELECT state, attempts, available_at, last_error FROM tasks WHERE idempotency_key = %s",
                      (f"{PROFILE_TASK}:{internal}:{token_version}:active",)).fetchone()
    return None if row is None else tuple(row)


def _profile_attempted(sql: psycopg.Connection, internal: int, token_version: int, state: str,
                       attempts: int) -> Callable[[], bool]:
    def check() -> bool:
        row = _profile_task(sql, internal, token_version)
        return row is not None and row[:2] == (state, attempts)
    return check


def _is_profile_call(call: Any) -> bool:
    return call.method in PROFILE_METHODS


# ---------------------------------------------------------------------------------------------
# Testes
# ---------------------------------------------------------------------------------------------


def test_bot_register_failures_never_expose_the_token(
        client: Any, app_factory: Any, bot_settings: Any, bot_api: Any, clock: Any, db_url: str,
        sql: psycopg.Connection, cli_invoke: Any, monkeypatch: Any, tmp_path: Any, capfd: Any) -> None:
    """`bots.register` sob cada falha prevista do `getMe` e cada recusa local: o token nunca sai.

    Varreduras (respostas, cabeçalhos, URL, logs do sistema e do transporte, fd 1 e 2, eventos,
    CLI, parâmetros ao Telegram e banco); validação do token (401 e 421, 400, `NotABot` → 400
    `[token, invalid_value]`; 429, 5xx, HTML, conexão cortada, resposta presa, 409 e 403 → 503
    `unavailable`; nada gravado); bot repetido (`bot_conflict` com `duplicate` e `invalid_value`);
    recusas locais (formato, permissão, chave-mestra ausente → 503 `not_configured`, oferecimento
    inexistente e arquivado: nenhuma chamada ao Telegram); o registro do bot com token recusado, Telegram fora, troca de token e bot de outro oferecimento; a
    chave-mestra trocada (a coluna cifrada decifra só com a chave de teste); controles positivos
    (decifração independente, tabela temporária, captura do transporte, fd 2); contra o vazio:
    a captura do transporte tem os registros do `httpcore2` do adaptador, e
    os logs do sistema têm o registro `request` do `bots.register` para cada status visto (como na
    API base).
    """
    cli = _Cli(cli_invoke, monkeypatch)
    started = clock.now()
    with _sweep(client) as surfaces:
        target = create_offering(client, ADMIN_TOKEN, code=REGISTER_OFFERING)
        create_offering(client, ADMIN_TOKEN, code=OTHER_OFFERING)

        def put(token: Any, ref: str = REGISTER_OFFERING, headers: dict | None = None, http: Any = client) -> Any:
            return http.put(f"/v1/offerings/{ref}/bot", json={"token": token}, headers=headers or ADMIN)

        def fresh(program: Callable[[int], None] | None = None, **kwargs: Any) -> int:
            bot = bot_api.add_bot(**kwargs)
            if program is not None:
                program(bot)
            return bot

        # 1. Cada desfecho do getMe: (rótulo, bot, erro esperado, (token_state, outcome, status) da chamada).
        cases = [
            ("401 (revogado)", fresh(bot_api.revoke), "invalid_value", ("revoked", "error", 401)),
            ("421", fresh(lambda b: bot_api.fail("getMe", error="forbidden_token", bot_id=b)), "invalid_value",
             ("valid", "error", 421)),
            ("400", fresh(lambda b: bot_api.fail("getMe", status=400, bot_id=b)), "invalid_value",
             ("valid", "error", 400)),
            ("não é bot", fresh(is_bot=False), "invalid_value", ("valid", "ok", 200)),
            ("sem username", fresh(username=None), "invalid_value", ("valid", "ok", 200)),
            ("429", fresh(lambda b: bot_api.fail("getMe", retry_after=7, bot_id=b)), "unavailable",
             ("valid", "error", 429)),
            ("500", fresh(lambda b: bot_api.fail("getMe", status=500, bot_id=b)), "unavailable",
             ("valid", "error", 500)),
            ("502 HTML", fresh(lambda b: bot_api.fail_network("getMe", kind="not_json", bot_id=b)), "unavailable",
             ("valid", "error", 502)),
            ("conexão cortada", fresh(lambda b: bot_api.fail_network("getMe", kind="reset", bot_id=b)),
             "unavailable", ("valid", "reset", None)),
            ("resposta presa", fresh(lambda b: bot_api.fail_network("getMe", kind="hang", bot_id=b)),
             "unavailable", ("valid", "hang", None)),
            ("409 webhook", fresh(lambda b: bot_api.fail("getMe", error="conflict_webhook", bot_id=b)),
             "unavailable", ("valid", "error", 409)),
            ("409 consumidor", fresh(lambda b: bot_api.fail("getMe", error="conflict_poll", bot_id=b)),
             "unavailable", ("valid", "error", 409)),
            ("403", fresh(lambda b: bot_api.fail("getMe", error="blocked", bot_id=b)), "unavailable",
             ("valid", "error", 403)),
        ]
        for label, bot, expected, outcome in cases:
            resp = put(bot_api.token_of(bot))
            if expected == "invalid_value":
                assert_error(resp, 400, "invalid_request", fields=[("token", "invalid_value")])
            else:
                assert_error(resp, 503, "unavailable")
            finished = bot_api.wait_for(
                lambda bot=bot: [c for c in bot_api.calls(bot_id=bot) if c.outcome != "pending"], timeout=10)
            assert [(c.method, c.token_state, c.outcome, c.status) for c in finished] == [("getMe", *outcome)], label

        # 2. Registro aceito e os dois conflitos (o getMe roda antes da conferência).
        ok_bot = bot_api.add_bot()
        ok_user = bot_api.state(ok_bot).username
        resp = put(bot_api.token_of(ok_bot))
        assert resp.status_code == 200, resp.text[:300]
        assert_matches("bots.register", 200, resp.json())
        assert resp.json() == {
            "offering": {"offering_id": target["offering_id"], "code": REGISTER_OFFERING}, "username": ok_user,
            "telegram_bot_id": ok_bot, "state": "registered", "profile_state": "pending",
            "registered_at": iso(started), "token_replaced_at": None, "lease": None, "last_poll_ok_at": None,
            "problem": None}
        assert_error(put(bot_api.token_of(ok_bot), ref=OTHER_OFFERING), 409, "bot_conflict",
                     fields=[("token", "duplicate")])
        stranger = bot_api.add_bot()
        assert_error(put(bot_api.token_of(stranger)), 409, "bot_conflict", fields=[("token", "invalid_value")])

        # 3. Recusas antes de qualquer chamada ao Telegram.
        before = _last_seq(bot_api)
        too_long, short_secret, leading_zero = "1:" + "a" * 79, "123:" + "b" * 29, "0123:" + "c" * 35
        for shaped in (too_long, short_secret, leading_zero):
            remember_secret(shaped)
        assert_error(put(too_long), 400, "invalid_request", fields=[("token", "too_long")])
        assert_error(put(short_secret), 400, "invalid_request", fields=[("token", "invalid_format")])
        assert_error(put(leading_zero), 400, "invalid_request", fields=[("token", "invalid_format")])
        assert_error(put(""), 400, "invalid_request", fields=[("token", "too_short")])
        assert_error(put([bot_api.token_of(stranger)]), 400, "invalid_request", fields=[("token", "invalid_format")])
        writer_secret, _ = create_token(client, ADMIN_TOKEN, ["offerings:write"], label="sem bots manage")
        assert_error(put(bot_api.token_of(stranger), headers=auth(writer_secret)), 403, "forbidden",
                     permission="bots:manage")
        assert_error(put(bot_api.token_of(stranger), ref="segredo-inexistente"), 404, "not_found")
        create_offering(client, ADMIN_TOKEN, code=ARCHIVED_OFFERING)
        prepared = client.post(f"/v1/offerings/{ARCHIVED_OFFERING}/archive/prepare", headers=ADMIN)
        assert prepared.status_code == 200, prepared.text[:300]
        archived = client.post(f"/v1/offerings/{ARCHIVED_OFFERING}/archive", headers=ADMIN,
                               json={"confirmation_code": prepared.json()["confirmation_code"]})
        assert archived.status_code == 200, archived.text[:300]
        assert_error(put(bot_api.token_of(stranger), ref=ARCHIVED_OFFERING), 409, "invalid_state", state="archived")
        no_key_app = app_factory(bot_settings=dataclasses.replace(bot_settings, master_key=None))
        with TestClient(no_key_app, raise_server_exceptions=False) as no_key:
            _record(no_key, surfaces.exchanges)
            assert_error(put(bot_api.token_of(stranger), http=no_key), 503, "not_configured")
        assert [c.method for c in bot_api.calls() if c.seq > before] == [], "chamada ao Telegram antes da hora"

        # 4. O CLI com um token de outro oferecimento: erro sem o token (arquivo, fora de terminal).
        duplicated = tmp_path / "token-duplicado.txt"
        duplicated.write_text(bot_api.token_of(ok_bot) + "\n", encoding="utf-8")
        run = cli.run(["bots", "register", OTHER_OFFERING, "--token-file", str(duplicated)], 1)
        assert run.stderr.splitlines() == [f"Erro: {MESSAGES['bot_conflict']}", "  - token: duplicate"]
        run = cli.run(["bots", "register", OTHER_OFFERING, "--token-file", str(duplicated)], 1, json_mode=True)
        assert json.loads(run.stderr) == {"error": {"code": "bot_conflict", "message": MESSAGES["bot_conflict"],
                                                    "fields": [{"field": "token", "reason": "duplicate"}]}}
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    secrets = _secrets(bot_api)
    assert set(bot_api.all_tokens()) <= set(secrets) and MASTER_KEY in secrets
    _assert_exchanges_clean(surfaces.exchanges, secrets)
    _assert_cli_clean(cli.runs, secrets)
    _assert_logs_clean(surfaces, secrets)
    _check_fd(capfd, surfaces.caught, secrets, bot_api.token_of(stranger))
    _assert_telegram_params_clean(bot_api, secrets)
    # Só o registro aceito gerou evento de bot, e só ele ficou no banco (cifrado).
    assert [(e["type"], e["data"]) for e in events if e["type"].startswith("bot.")] == [
        ("bot.registered", {"offering_id": target["offering_id"], "telegram_bot_id": ok_bot, "username": ok_user})]
    assert sql.execute("SELECT telegram_bot_id, token_version, state FROM bots").fetchall() == [
        (ok_bot, 1, "registered")]
    _assert_ciphertexts(sql, {ok_bot: bot_api.token_of(ok_bot)})
    _assert_database_clean(db_url, secrets, invite_codes=[], start_texts=[], api_secrets=[writer_secret])
    _assert_scan_catches_temp_table(db_url, bot_api.token_of(ok_bot))
    _assert_transport_capture_catches(bot_api, bot_api.token_of(ok_bot))
    # Contra o vazio: as varreduras de log leram os registros dos caminhos exercitados.
    _assert_transport_exercised(surfaces.transport, bot_api)
    register_op = {op["id"]: op for op in load_catalog()["operations"]}["bots.register"]
    requests = [r for r in surfaces.logs.records if r.get("logger") == "cq_server.api" and r.get("event") == "request"]
    assert {status: _has_record(requests, {"method": register_op["method"], "route": register_op["path"],
                                           "status": status})
            for status in (200, 400, 403, 404, 409, 503)} == dict.fromkeys((200, 400, 403, 404, 409, 503), True)


def test_cli_bot_token_and_team_link_leave_only_where_allowed(
        client: Any, bot_api: Any, clock: Any, db_url: str, sql: psycopg.Connection, cli_invoke: Any,
        monkeypatch: Any, tmp_path: Any, capfd: Any) -> None:
    """O token do bot entra pelo CLI só por arquivo ou prompt mascarado, e o link de equipe sai só num
    terminal (ou num arquivo); as 10 folhas do CLI de oferecimentos e bots rodam sem expor segredo.

    Token do bot (pelo CLI: `--token-file` com espaços e quebras nas pontas tirados, prompt
    `cli.prompt_secret("Token do bot")` num terminal, código 2 sem os dois); link de equipe (o link é
    credencial: no terminal, a saída exata `Link de equipe para …` e o link, ou o corpo com `--json`;
    fora dele, só com `--secret-file`); validade no fuso do oferecimento; sigilo (a
    saída do `cq team link` num terminal e o 201 de `team.link` são as únicas exceções, exatas; os
    códigos ficam só como resumo no banco); controles contra o vazio: cada uma dessas folhas invocada
    com sucesso e um registro de pedido por operação (`OFFERING_BOT_OPERATIONS`).
    """
    cli = _Cli(cli_invoke, monkeypatch)
    person = register_person(client, ADMIN_TOKEN, name="Professora Segredo Terminal", ra="5500000021")
    alias, pseudo = person["alias"], person["pseudo_id"]
    started = clock.now()
    expires = started + TEAM_LINK_VALIDITY
    local = expires.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")
    with _sweep(client) as surfaces:
        run = cli.run(["offerings", "create", CLI_OFFERING, "--course", "Laboratório de Segredos", "--term", "2031s1",
                       "--timezone", TIMEZONE], 0, json_mode=True)
        created = json.loads(run.stdout)
        assert_matches("offerings.create", 201, created)
        assert re.fullmatch(UUID4_RE, created["offering_id"]), created
        assert created == {
            "offering_id": created["offering_id"], "code": CLI_OFFERING, "course": "Laboratório de Segredos",
            "term": "2031s1", "timezone": TIMEZONE, "response_window_minutes": 1440, "state": "draft",
            "created_at": iso(started), "activated_at": None, "closed_at": None, "archived_at": None, "bot": None,
            "free_messages": False, "contact_email_set": False,
            # O `Offering` traz `purged_at`, nulo sem expurgo.
            "purged_at": None}
        link_offering = {"offering_id": created["offering_id"], "code": CLI_OFFERING, "timezone": TIMEZONE}

        # Token por arquivo, fora de terminal (espaços e quebras de linha nas pontas).
        bot = bot_api.add_bot()
        username = bot_api.state(bot).username
        token_file = tmp_path / "token.txt"
        token_file.write_text(f"\n  {bot_api.token_of(bot)}  \n\n", encoding="utf-8")
        run = cli.run(["bots", "register", CLI_OFFERING, "--token-file", str(token_file)], 0)
        assert run.stdout.splitlines() == [
            f"Bot registrado: @{username} no oferecimento {CLI_OFFERING} (perfil: pending)."]
        # Token novo do mesmo bot pelo prompt mascarado, num terminal (troca de token).
        rotated = bot_api.rotate_token(bot)
        run = cli.run(["bots", "register", CLI_OFFERING], 0, json_mode=True, stdin_terminal=True, prompt_answer=rotated)
        assert cli.prompts == [PROMPT_LABEL]
        replaced = json.loads(run.stdout)
        assert_matches("bots.register", 200, replaced)
        assert replaced == {
            "offering": {"offering_id": created["offering_id"], "code": CLI_OFFERING}, "username": username,
            "telegram_bot_id": bot, "state": "registered", "profile_state": "pending",
            "registered_at": iso(started), "token_replaced_at": iso(started), "lease": None,
            "last_poll_ok_at": None, "problem": None}
        # Token recusado pelo Telegram: erro sem o token; sem terminal e sem arquivo: código 2.
        remember_secret(UNKNOWN_BOT_TOKEN)
        unknown_file = tmp_path / "token-desconhecido.txt"
        unknown_file.write_text(UNKNOWN_BOT_TOKEN, encoding="utf-8")
        run = cli.run(["bots", "register", CLI_OFFERING, "--token-file", str(unknown_file)], 1)
        assert run.stderr.splitlines() == [f"Erro: {MESSAGES['invalid_request']}", "  - token: invalid_value"]
        run = cli.run(["bots", "register", CLI_OFFERING], 2)
        assert run.stderr.splitlines() == ["Uso incorreto: fora de um terminal, informe --token-file."]
        assert cli.prompts == [PROMPT_LABEL], "o prompt só roda num terminal"

        run = cli.run(["bots", "activate", CLI_OFFERING], 0, json_mode=True)
        active = {**created, "state": "active", "activated_at": iso(started),
                  "bot": {"username": username, "telegram_bot_id": bot, "state": "registered",
                          "profile_state": "pending"}}
        assert json.loads(run.stdout) == active
        run = cli.run(["bots", "show", CLI_OFFERING], 0)
        assert run.stdout.splitlines() == [
            f"@{username}  registered  perfil: pending  posse: nenhuma  última consulta: -"]

        # Link de equipe num terminal: a única saída do CLI com o link (texto e --json).
        terminal_text = cli.run(["team", "link", CLI_OFFERING, "--person", alias], 0, stdout_terminal=True)
        lines = terminal_text.stdout.splitlines()
        assert len(lines) == 2 and lines[0] == (
            f"Link de equipe para {alias} (válido até {iso(expires)}; {local} {TIMEZONE}):"), lines
        text_match = TEAM_LINK_SHAPE.fullmatch(lines[1])
        assert text_match is not None and text_match.group(1) == username, lines[1]
        terminal_json = cli.run(["team", "link", CLI_OFFERING, "--person", alias], 0, json_mode=True,
                                stdout_terminal=True)
        json_body = json.loads(terminal_json.stdout)
        assert_matches("team.link", 201, json_body)
        assert json_body == {"link": json_body["link"], "expires_at": iso(expires),
                             "person": {"pseudo_id": pseudo, "alias": alias}, "offering": link_offering}
        json_match = TEAM_LINK_SHAPE.fullmatch(json_body["link"])
        assert json_match is not None and json_match.group(1) == username
        # Fora de terminal: só com --secret-file (o link vai para o arquivo, nunca para a saída).
        run = cli.run(["team", "link", CLI_OFFERING, "--person", alias], 2)
        assert run.stderr.splitlines() == ["Uso incorreto: fora de um terminal, informe --secret-file."]
        link_file = tmp_path / "link.txt"
        run = cli.run(["team", "link", CLI_OFFERING, "--person", alias, "--secret-file", str(link_file)], 0,
                      json_mode=True)
        assert json.loads(run.stdout) == {"expires_at": iso(expires), "person": {"pseudo_id": pseudo, "alias": alias},
                                          "offering": link_offering, "secret_file": str(link_file)}
        content = link_file.read_text(encoding="utf-8")
        assert content.endswith("\n") and content.count("\n") == 1
        file_match = TEAM_LINK_SHAPE.fullmatch(content[:-1])
        assert file_match is not None and file_match.group(1) == username
        codes = [text_match.group(2), json_match.group(2), file_match.group(2)]
        assert len(set(codes)) == 3 and all(code in all_secrets() for code in codes), "códigos fora do registro"

        # As demais folhas, com --json igual à API.
        run = cli.run(["offerings", "list"], 0, json_mode=True)
        assert json.loads(run.stdout) == client.get("/v1/offerings", headers=ADMIN).json()
        run = cli.run(["offerings", "show", created["offering_id"]], 0, json_mode=True)
        assert json.loads(run.stdout) == active
        run = cli.run(["offerings", "close", CLI_OFFERING], 0, json_mode=True)
        closed = {**active, "state": "closed", "closed_at": iso(started)}
        assert json.loads(run.stdout) == closed
        run = cli.run(["offerings", "archive", "prepare", CLI_OFFERING], 0, json_mode=True)
        prepared = json.loads(run.stdout)
        assert_matches("offerings.prepare_archive", 200, prepared)
        assert re.fullmatch(r"[A-Za-z0-9_-]{22}", prepared["confirmation_code"]), prepared
        assert prepared == {"offering": closed, "team_members_active": 0, "pending_links": 1,
                            "confirmation_code": prepared["confirmation_code"],
                            "expires_at": iso(started + ARCHIVE_CODE_VALIDITY)}
        run = cli.run(["offerings", "archive", "confirm", CLI_OFFERING, "--confirmation",
                       prepared["confirmation_code"]], 0, json_mode=True)
        assert json.loads(run.stdout) == {**closed, "state": "archived", "archived_at": iso(started)}
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    secrets = _secrets(bot_api)
    assert rotated in secrets and {lines[1], json_body["link"], content[:-1]} <= set(secrets)
    terminal_runs = {cli.runs.index(terminal_text): _link_secrets(lines[1]),
                     cli.runs.index(terminal_json): _link_secrets(json_body["link"])}
    _assert_cli_clean(cli.runs, secrets, terminal_runs)
    _assert_exchanges_clean(surfaces.exchanges, secrets)
    assert len([ex for ex in surfaces.exchanges if ex.is_team_link_creation]) == 3
    _assert_logs_clean(surfaces, secrets)
    _check_fd(capfd, surfaces.caught, secrets, rotated)
    _assert_telegram_params_clean(bot_api, secrets)
    # Os três links da mesma matrícula: os dois primeiros substituídos, o último revogado no arquivamento.
    assert _offering_events(events, created["offering_id"], "team.link_revoked") == [
        {"offering_id": created["offering_id"], "person": pseudo, "revoke_reason": reason}
        for reason in ("replaced", "replaced", "archived")]
    _assert_ciphertexts(sql, {bot: rotated})
    _assert_database_clean(db_url, secrets, invite_codes=codes, start_texts=[], api_secrets=[])
    # Contra o vazio: as 10 folhas com sucesso e um `request` de sucesso por operação.
    assert {r.leaf for r in cli.runs if r.exit_code == 0} == OFFERING_BOT_CLI_LEAVES
    assert _requests_per_operation(surfaces.logs.records) == {op_id: True for op_id in OFFERING_BOT_OPERATIONS}


def test_worker_telegram_failures_never_expose_the_token(
        client: Any, bot_api: Any, bot_settings: Any, bot_worker_thread: Any, settings: Any, clock: Any,
        db_url: str, sql: psycopg.Connection, capfd: Any) -> None:
    """O worker com bots sob todas as falhas previstas do Telegram, na consulta, na entrega e no
    perfil, com troca de token, arquivamento e chave-mestra trocada: o token e a chave nunca saem.

    Sigilo (exercitado por todas as falhas previstas do Telegram): na consulta, 500,
    conexão cortada, 502 HTML, resposta presa, 400, 409 de webhook, 409 de outro consumidor e 429; na
    entrega, 429, 500, 400, 403 de bloqueio e 401; no perfil, 429;
    revogação (sem acesso, um evento por episódio, recuperação pela troca de token);
    troca de token com o mesmo dono; arquivamento (posse limpa,
    convite pendente revogado, descrição arquivada); a chave-mestra trocada (nenhuma
    chamada ao Telegram, `token_unreadable`); código de convite (o `/start <código>` gravado é a única exceção no
    banco, com os resumos dos convites); eventos (os 14 tipos de `OFFERING_BOT_EVENT_TYPES` saem, com os dados exatos
    do ciclo do bot); controles positivos (decifração independente, tabela temporária, transporte, fd 2).
    As falhas do tratador do perfil além do 429 (`TelegramUnavailable`, `TokenRejected`,
    `RequestRejected`) estão em `test_worker_profile_failures_never_expose_the_token`. Contra o vazio:
    a captura do transporte tem os registros do `httpcore2` do adaptador, e
    os logs do sistema têm os registros do worker (por inclusão): `bot.lease` da posse
    assumida, `bot.poll_problem` de cada problema da consulta exercitado e `telegram.update` de cada
    desfecho.
    """
    started = clock.now()
    teacher = register_person(client, ADMIN_TOKEN, name="Professora Segredo Worker", ra="5500000011")
    assistant = register_person(client, ADMIN_TOKEN, name="Monitor Segredo Worker", ra="5500000012")
    u1, u2, u3 = make_user(11), make_user(12), make_user(13)
    with _sweep(client) as surfaces:
        offering = create_offering(client, ADMIN_TOKEN, code=WORKER_OFFERING)
        oid = offering["offering_id"]
        bot = bot_api.add_bot()
        register_bot(client, ADMIN_TOKEN, WORKER_OFFERING, bot_api.token_of(bot))
        activate_bot(client, ADMIN_TOKEN, WORKER_OFFERING)
        code1 = link_code(create_team_link(client, ADMIN_TOKEN, WORKER_OFFERING, teacher["alias"])["link"])
        code2 = link_code(create_team_link(client, ADMIN_TOKEN, WORKER_OFFERING, assistant["alias"])["link"])

        def processed(update_id: int | None, outcome: str) -> int:
            assert update_id is not None, "a Bot API falsa descartou o update"
            _wait_until(lambda: _update_state(sql, bot, update_id) == ("processed", outcome), 15,
                        f"update {update_id} não processado como {outcome}")
            return update_id

        def delivered(user: Any, count: int) -> Callable[[], bool]:
            return lambda: len(bot_api.sent_messages(bot, chat_id=user.user_id)) >= count

        def programmed(method: str, after: int) -> Any:
            return bot_api.wait_for(lambda: [c for c in bot_api.calls(method, bot_id=bot)
                                             if c.programmed and c.seq > after], timeout=15)[0]

        def polled_after(seq: int) -> Callable[[], Any]:
            return lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.seq > seq and c.outcome == "ok"]

        # 1. Perfil com 429 no setMyDescription (a tarefa espera e repete).
        bot_api.fail("setMyDescription", retry_after=2, bot_id=bot)
        worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=WORKER_ID),
                                   channel=ConsoleChannel(io.StringIO()), handlers={})
        programmed("setMyDescription", 0)
        _advance_until(clock, RELEASE, lambda: _bot_row(sql, "profile_state", bot) == ("configured",),
                       "o perfil não foi configurado depois do 429")
        bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=15)

        # 2. Cada falha prevista da consulta, uma de cada vez, liberando a espera no relógio.
        poll_failures: list[tuple[str, Callable[[], None]]] = [
            ("500", lambda: bot_api.fail("getUpdates", status=500, bot_id=bot)),
            ("conexão cortada", lambda: bot_api.fail_network("getUpdates", kind="reset", bot_id=bot)),
            ("502 HTML", lambda: bot_api.fail_network("getUpdates", kind="not_json", bot_id=bot)),
            ("resposta presa", lambda: bot_api.fail_network("getUpdates", kind="hang", bot_id=bot)),
            ("400", lambda: bot_api.fail("getUpdates", status=400, bot_id=bot)),
            ("409 webhook", lambda: bot_api.fail("getUpdates", error="conflict_webhook", bot_id=bot)),
            ("409 consumidor", lambda: bot_api.fail("getUpdates", error="conflict_poll", bot_id=bot)),
            ("429", lambda: bot_api.fail("getUpdates", retry_after=5, bot_id=bot)),
        ]
        exercised: dict[str, tuple[str, int | None]] = {}
        for label, program in poll_failures:
            before = _last_seq(bot_api)
            program()
            call = programmed("getUpdates", before)
            exercised[label] = (call.outcome, call.status)
            _advance_until(clock, RELEASE, polled_after(call.seq), f"a consulta não voltou depois de {label}")
        assert exercised == {"500": ("error", 500), "conexão cortada": ("reset", None), "502 HTML": ("error", 502),
                             "resposta presa": ("hang", None), "400": ("error", 400), "409 webhook": ("error", 409),
                             "409 consumidor": ("error", 409), "429": ("error", 429)}

        # 3. Entrega: 429 nas boas-vindas, 400 numa resposta, 500 na recusa do link, 403 de bloqueio.
        before = _last_seq(bot_api)
        bot_api.fail("sendMessage", retry_after=3, bot_id=bot)
        processed(bot_api.push_start(bot, u1, code1), "team_joined")
        programmed("sendMessage", before)
        _advance_until(clock, RELEASE, delivered(u1, 1), "as boas-vindas não saíram depois do 429")
        bot_api.fail("sendMessage", status=400, bot_id=bot)
        rejected = processed(bot_api.push_text(bot, u2, "/ajuda", bot_command=True), "help")
        _wait_until(lambda: sql.execute(
            "SELECT o.state, o.last_error FROM outbox_messages o JOIN telegram_updates u "
            "ON u.id = o.reply_to_update_id WHERE u.update_id = %s", (rejected,)).fetchall()
            == [("failed", "request_rejected")], 15, "a resposta recusada (400) não falhou de vez")
        before = _last_seq(bot_api)
        bot_api.fail("sendMessage", status=500, bot_id=bot)
        processed(bot_api.push_start(bot, u3, INVALID_CODE), "team_refused_invalid")
        programmed("sendMessage", before)
        _advance_until(clock, RELEASE, delivered(u3, 1), "a recusa do link não saiu depois do 500")
        bot_api.block_chat(bot, u1)
        processed(bot_api.push_start(bot, u1, code1), "team_already_linked")
        _wait_until(lambda: sql.execute("SELECT state FROM delivery_endpoints WHERE chat_id = %s",
                                        (u1.user_id,)).fetchall() == [("blocked",)], 15, "o endereço não ficou blocked")
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente antes do 401")

        # 4. 401 na entrega: sem acesso; recuperação pela troca de token (API); a resposta sai depois.
        bot_api.fail("sendMessage", error="unauthorized", bot_id=bot)
        processed(bot_api.push_text(bot, u3, "/ajuda", bot_command=True), "help")
        _wait_until(lambda: _bot_row(sql, "state, access_problem", bot) == ("no_access", "token_rejected"), 15,
                    "o 401 na entrega não deixou o bot sem acesso")
        second = bot_api.rotate_token(bot)
        restored = register_bot(client, ADMIN_TOKEN, WORKER_OFFERING, second)
        assert (restored["state"], restored["problem"]) == ("registered", None)
        # O relógio só anda depois de concluída a tarefa do perfil da versão 2, que sai sem espera: um
        # avanço durante a execução venceria a posse da tarefa (30 s) e a repetiria.
        internal = _bot_row(sql, "id", bot)[0]
        _wait_until(lambda: (_profile_task(sql, internal, 2) or ("",))[0] == "done", 15,
                    "a tarefa do perfil da versão 2 não terminou")
        _advance_until(clock, RELEASE, delivered(u3, 2), "a resposta pendente não saiu com o token novo")
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente antes da revogação")

        # 5. Revogação na consulta: sem acesso; recuperação pela troca de token.
        bot_api.revoke(bot)
        _wait_until(lambda: _bot_row(sql, "state, access_problem", bot) == ("no_access", "token_rejected"), 15,
                    "a revogação não deixou o bot sem acesso")
        third = bot_api.rotate_token(bot)
        register_bot(client, ADMIN_TOKEN, WORKER_OFFERING, third)
        bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot)
                                  if c.token_version == 3 and c.outcome == "ok"], timeout=15)
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente antes do arquivamento")

        # 6. Encerramento e arquivamento em duas fases: descrição arquivada com o token atual.
        resp = client.post(f"/v1/offerings/{WORKER_OFFERING}/close", headers=ADMIN)
        assert resp.status_code == 200, resp.text[:300]
        resp = client.post(f"/v1/offerings/{WORKER_OFFERING}/archive/prepare", headers=ADMIN)
        assert resp.status_code == 200, resp.text[:300]
        assert (resp.json()["team_members_active"], resp.json()["pending_links"]) == (1, 1)
        resp = client.post(f"/v1/offerings/{WORKER_OFFERING}/archive", headers=ADMIN,
                           json={"confirmation_code": resp.json()["confirmation_code"]})
        assert resp.status_code == 200, resp.text[:300]
        archived_text = _compose(DESCRIPTION_ARCHIVED, DEFAULT_COURSE, DEFAULT_TERM)
        bot_api.wait_for(lambda: [c for c in bot_api.calls("setMyDescription", bot_id=bot)
                                  if c.params == {"description": archived_text}], timeout=15)
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente depois do arquivamento")
        assert bot_worker_thread.stop_one(worker) < 10, "run_worker com bots não parou em 10 s"
        assert worker.error is None and worker.returned

        # 7. Worker com outra chave-mestra válida: token ilegível, nenhuma chamada ao Telegram.
        create_offering(client, ADMIN_TOKEN, code=OTHER_KEY_OFFERING)
        other_bot = bot_api.add_bot()
        other = register_bot(client, ADMIN_TOKEN, OTHER_KEY_OFFERING, bot_api.token_of(other_bot))
        activate_bot(client, ADMIN_TOKEN, OTHER_KEY_OFFERING)
        before = _last_seq(bot_api)
        wrong_key = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=OTHER_KEY_WORKER_ID),
                                      bot_settings=dataclasses.replace(bot_settings, master_key=OTHER_MASTER_KEY),
                                      channel=ConsoleChannel(io.StringIO()), handlers={})
        _wait_until(lambda: _bot_row(sql, "state, access_problem", other_bot) == ("no_access", "token_unreadable"),
                    15, "a chave trocada não deixou o bot sem acesso")
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente com a chave trocada")
        assert bot_worker_thread.stop_one(wrong_key) < 10, "run_worker com a chave trocada não parou em 10 s"
        assert wrong_key.error is None and wrong_key.returned
        assert [c.method for c in bot_api.calls() if c.seq > before] == [], "a chave trocada chamou o Telegram"
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    # --- O ciclo exercitado (contra o vazio) ---
    help_text = _compose(HELP, DEFAULT_COURSE, DEFAULT_TERM)
    welcome = _compose(WELCOME_TEAM, DEFAULT_COURSE, DEFAULT_TERM)
    assert [(c.params, c.outcome, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ({"chat_id": u1.user_id, "text": welcome}, "error", 429),
        ({"chat_id": u1.user_id, "text": welcome}, "ok", 200),
        ({"chat_id": u2.user_id, "text": help_text}, "error", 400),
        ({"chat_id": u3.user_id, "text": LINK_INVALID}, "error", 500),
        ({"chat_id": u3.user_id, "text": LINK_INVALID}, "ok", 200),
        ({"chat_id": u1.user_id, "text": ALREADY_LINKED}, "error", 403),
        ({"chat_id": u3.user_id, "text": help_text}, "error", 401),
        ({"chat_id": u3.user_id, "text": help_text}, "ok", 200)]
    assert [c.token_version for c in bot_api.calls("sendMessage", bot_id=bot)] == [1, 1, 1, 1, 1, 1, 1, 2]
    assert [c.params for c in bot_api.calls("setMyDescription", bot_id=bot)] == [
        {"description": _compose(DESCRIPTION_ACTIVE, DEFAULT_COURSE, DEFAULT_TERM)}] * 4 + [
        {"description": archived_text}]
    assert _offering_events(events, oid, "bot.access_changed") == [
        {"offering_id": oid, "access": "lost", "problem": "token_rejected"},
        {"offering_id": oid, "access": "restored", "problem": None},
        {"offering_id": oid, "access": "lost", "problem": "token_rejected"},
        {"offering_id": oid, "access": "restored", "problem": None}]
    assert _offering_events(events, oid, "bot.token_replaced") == [
        {"offering_id": oid, "telegram_bot_id": bot, "token_version": 2},
        {"offering_id": oid, "telegram_bot_id": bot, "token_version": 3}]
    assert _offering_events(events, oid, "bot.lease_changed") == [
        {"offering_id": oid, "worker_id": WORKER_ID, "change": change, "reason": reason}
        for change, reason in (("acquired", "acquired"), ("released", "no_access"), ("acquired", "acquired"),
                               ("released", "no_access"), ("acquired", "acquired"), ("released", "archived"))]
    assert _offering_events(events, oid, "bot.profile_updated") == [
        {"offering_id": oid, "profile_state": "configured", "purpose": purpose}
        for purpose in ("active", "active", "active", "archived")]
    assert _offering_events(events, oid, "offering.state_changed") == [
        {"offering_id": oid, "from_state": a, "to_state": b}
        for a, b in (("draft", "active"), ("active", "closed"), ("closed", "archived"))]
    assert _offering_events(events, oid, "team.link_revoked") == [
        {"offering_id": oid, "person": assistant["pseudo_id"], "revoke_reason": "archived"}]
    assert _offering_events(events, other["offering"]["offering_id"], "bot.access_changed") == [
        {"offering_id": other["offering"]["offering_id"], "access": "lost", "problem": "token_unreadable"}]
    reply_row, reply_message = sql.execute(
        "SELECT u.id, o.id FROM outbox_messages o JOIN telegram_updates u ON u.id = o.reply_to_update_id "
        "WHERE u.update_id = %s", (rejected,)).fetchone()
    assert [e["data"] for e in events if e["type"] == "outbox.reply_failed"] == [
        {"message_id": reply_message, "reply_to": reply_row, "attempts": 1, "error": "request_rejected"}]
    assert set(OFFERING_BOT_EVENT_TYPES) <= set(EVENT_TYPES)
    assert set(OFFERING_BOT_EVENT_TYPES) <= {e["type"] for e in events}, "tipo de evento que não saiu"
    assert _offering_events(events, oid, "team.joined") == [
        {"offering_id": oid, "person": teacher["pseudo_id"], "endpoint_replaced": False}]
    assert _offering_events(events, oid, "endpoint.blocked") == [
        {"offering_id": oid, "person": teacher["pseudo_id"], "channel": "telegram"}]
    assert _offering_events(events, oid, "team.join_refused") == [{"offering_id": oid, "refusal": "invalid"}]
    assert _offering_events(events, oid, "team.link_created") == [
        {"offering_id": oid, "person": person["pseudo_id"], "expires_at": iso(started + TEAM_LINK_VALIDITY)}
        for person in (teacher, assistant)]

    # --- Varreduras ---
    secrets = _secrets(bot_api)
    assert {bot_api.token_of(bot), second, third, OTHER_MASTER_KEY} <= set(secrets)
    _assert_exchanges_clean(surfaces.exchanges, secrets)
    _assert_logs_clean(surfaces, secrets)
    _check_fd(capfd, surfaces.caught, secrets, third)
    _assert_telegram_params_clean(bot_api, secrets)
    _assert_ciphertexts(sql, {bot: third, other_bot: bot_api.token_of(other_bot)})
    _assert_database_clean(db_url, secrets, invite_codes=[code1, code2],
                           start_texts=[f"/start {code1}", f"/start {code1}"], api_secrets=[])
    _assert_scan_catches_temp_table(db_url, third)
    _assert_transport_capture_catches(bot_api, third)
    # Contra o vazio: as varreduras de log leram os registros do adaptador e do worker.
    _assert_transport_exercised(surfaces.transport, bot_api)
    records = _worker_records(surfaces)
    assert _has_record(records, {"event": "bot.lease", "offering_id": oid, "change": "acquired",
                                 "reason": "acquired"}), "sem registro bot.lease da posse assumida"
    problems = {r.get("problem") for r in records
                if r.get("event") == "bot.poll_problem" and r.get("offering_id") == oid}
    assert {"telegram_unavailable", "webhook_active", "concurrent_consumer", "rate_limited"} <= problems, sorted(
        map(str, problems))
    outcomes = {r.get("outcome") for r in records if r.get("event") == "telegram.update"}
    assert {"team_joined", "help", "team_refused_invalid", "team_already_linked"} <= outcomes, sorted(
        map(str, outcomes))


def test_worker_profile_failures_never_expose_the_token(
        client: Any, bot_api: Any, bot_worker_thread: Any, settings: Any, clock: Any, db_url: str,
        sql: psycopg.Connection, capfd: Any) -> None:
    """O tratador `bot.sync_profile` sob uma falha de cada classe do perfil, com o bot em consulta e
    uma troca de token no meio: o token e a chave nunca saem.

    Sigilo (nas falhas previstas do perfil do bot): o
    caminho de falha do perfil (decifra o token, chama `setMyDescription`/`setMyCommands` e, na falha,
    grava o perfil, marca `no_access` e registra `task.failed`) sob as mesmas varreduras (respostas,
    logs do sistema e do transporte, fd 1 e 2, parâmetros ao Telegram, eventos e banco). Perfil do bot
    (o resultado de cada classe de falha), uma chamada do perfil por tentativa:
    - `TelegramUnavailable` (500 e, depois, conexão cortada): falha temporária, sem mudança nem
      evento, nova tentativa depois da espera da fila de tarefas (2 s no `FakeClock`, `last_error`
      `TelegramUnavailable`);
    - `TokenRejected` (401): bot `no_access` com os eventos da perda de acesso (posse limpa com
      `bot.lease_changed {released, no_access}` e depois `bot.access_changed {lost, token_rejected}`;
      a revogação), perfil `failed`, `bot.profile_updated {failed}` e `task.failed` por último;
    - `RequestRejected` (400): perfil `failed`, `bot.profile_updated {failed}` e `task.failed`; o bot
      segue com acesso.
    Troca de token (recuperação: `bot.token_replaced`,
    `bot.access_changed {restored}`, tarefa nova com a chave da versão 2, posse assumida de novo e
    consulta com o token novo); parada ordenada (`released`/`shutdown`); eventos (todos os eventos,
    exatos e na ordem); contra o vazio: registros do `httpcore2` do adaptador, `bot.lease` e
    `task.processed` (logs da API base, por inclusão); controles positivos (decifração
    independente, tabela temporária, transporte, fd 2, logs e parâmetros ao Telegram).

    A troca de token acontece com o bot já `no_access`: uma consulta antiga que receba 401 depois da
    troca não abre episódio, porque a perda de acesso só vale para `state = registered` com a mesma
    `token_version`.

    Relatórios e avisos: os tratadores dessa parte são registrados no
    worker com bots mesmo com `handlers={}`, e o episódio `lost` → `restored` cria a tarefa
    `notice.access_episode`, que registra o aviso `bot_access_restored` sem destinatário (`no_recipient`) com
    o evento `notice.recorded`. Antes de parar o worker, o teste espera
    essa tarefa `done` (o `_quiet` sozinho corre contra a varredura). As listas exatas de tarefas e de eventos
    valem sem as tarefas `notice.%` e sem os 3 tipos dos relatórios (`REPORT_EVENT_TYPES`); contrapartida exata na
    mesma função: as tarefas excluídas são exatamente a `notice.access_episode` `done` com o payload no id do
    oferecimento, e os eventos excluídos, exatamente o `notice.recorded` do episódio (`episode_seq` = o `seq`
    do `bot.access_changed` `lost`, `recipient_count` 0, os campos fora do tipo nulos; o horário não é
    fixado). No esqueleto, a espera da tarefa estoura (nenhuma tarefa de aviso é criada).
    """
    started = clock.now()
    after_first, after_second = started + timedelta(seconds=2), started + timedelta(seconds=4)
    with _sweep(client) as surfaces:
        offering = create_offering(client, ADMIN_TOKEN, code=PROFILE_OFFERING)
        oid = offering["offering_id"]
        bot = bot_api.add_bot()
        username = bot_api.state(bot).username
        first = bot_api.token_of(bot)
        register_bot(client, ADMIN_TOKEN, PROFILE_OFFERING, first)
        activate_bot(client, ADMIN_TOKEN, PROFILE_OFFERING)
        (internal,) = sql.execute("SELECT id FROM bots WHERE telegram_bot_id = %s", (bot,)).fetchone()

        # 1. TelegramUnavailable (500) na 1ª tentativa: falha temporária, sem evento; a posse é assumida.
        bot_api.fail("*", status=500, bot_id=bot, when=_is_profile_call)
        worker = bot_worker_thread(settings=dataclasses.replace(settings, worker_id=PROFILE_WORKER_ID),
                                   channel=ConsoleChannel(io.StringIO()), handlers={})
        _wait_until(_profile_attempted(sql, internal, 1, "pending", 1), 15, "a 1ª tentativa do perfil não falhou")
        assert _profile_task(sql, internal, 1) == ("pending", 1, after_first, "TelegramUnavailable")
        bot_api.wait_for(lambda: bot_api.pending_polls(bot), timeout=15)

        # 2. TokenRejected (401) na 2ª tentativa: sem acesso, posse limpa, perfil failed, task.failed.
        bot_api.fail("*", error="unauthorized", bot_id=bot, when=_is_profile_call)
        clock.advance(2)
        _wait_until(_profile_attempted(sql, internal, 1, "failed", 2), 15, "o 401 no perfil não falhou a tarefa")
        _wait_until(lambda: _bot_row(sql, "state, access_problem, lease_owner", bot) == (
            "no_access", "token_rejected", None), 15, "o 401 no perfil não deixou o bot sem acesso")

        # 3. Recuperação pela troca de token; conexão cortada (TelegramUnavailable) na 1ª tentativa nova.
        second = bot_api.rotate_token(bot)
        bot_api.fail_network("*", kind="reset", bot_id=bot, when=_is_profile_call)
        restored = register_bot(client, ADMIN_TOKEN, PROFILE_OFFERING, second)
        assert (restored["state"], restored["profile_state"], restored["problem"]) == ("registered", "pending", None)
        _wait_until(_profile_attempted(sql, internal, 2, "pending", 1), 15, "a 1ª tentativa nova do perfil não falhou")
        assert _profile_task(sql, internal, 2) == ("pending", 1, after_second, "TelegramUnavailable")
        bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot)
                                  if c.token_version == 2 and c.outcome == "ok"], timeout=15)

        # 4. RequestRejected (400) na 2ª tentativa nova: perfil failed e task.failed; o bot segue com acesso.
        bot_api.fail("*", status=400, bot_id=bot, when=_is_profile_call)
        clock.advance(2)
        _wait_until(_profile_attempted(sql, internal, 2, "failed", 2), 15, "o 400 no perfil não falhou a tarefa")
        # A tarefa do aviso do episódio, concluída antes de parar o worker.
        _wait_until(lambda: sql.execute("SELECT state FROM tasks WHERE task_type = 'notice.access_episode'")
                    .fetchall() == [("done",)], 15, "a tarefa notice.access_episode do episódio não concluiu")
        _wait_until(lambda: _quiet(sql), 15, "trabalho pendente depois das falhas do perfil")
        assert bot_worker_thread.stop_one(worker) < 10, "run_worker com bots não parou em 10 s"
        assert worker.error is None and worker.returned
        events = all_events(client, ADMIN_TOKEN, *EVENT_TYPES)

    # --- O ciclo exercitado (contra o vazio) ---
    profile_calls = [c for c in bot_api.calls(bot_id=bot) if c.method in PROFILE_METHODS]
    assert [(c.token_version, c.token_state, c.outcome, c.status, c.programmed, c.clock_at)
            for c in profile_calls] == [
        (1, "valid", "error", 500, True, started),
        (1, "valid", "error", 401, True, after_first),
        (2, "valid", "reset", None, True, after_first),
        (2, "valid", "error", 400, True, after_second)]
    active = {"setMyDescription": {"description": _compose(DESCRIPTION_ACTIVE, DEFAULT_COURSE, DEFAULT_TERM)},
              "setMyCommands": {"commands": [{"command": c.command, "description": c.description}
                                             for c in COMMANDS]}}
    assert [c.params for c in profile_calls] == [active[c.method] for c in profile_calls]
    assert (bot_api.state(bot).description, bot_api.state(bot).commands) == (None, None)
    # As tarefas dos avisos (`notice.%`) saem do recorte;
    # contrapartida exata: só a `notice.access_episode` do episódio, concluída, com o payload no id do oferecimento
    # (o id interno da linha do assunto, como único valor; o nome da chave, o da regra, não
    # é fixado aqui).
    (offering_row,) = sql.execute("SELECT id FROM offerings WHERE offering_id = %s::uuid", (oid,)).fetchone()
    r6_tasks = [tuple(row) for row in sql.execute(
        "SELECT task_type, payload, state FROM tasks WHERE task_type LIKE 'notice.%' ORDER BY id")]
    assert [(task_type, list(payload.values()), state) for task_type, payload, state in r6_tasks] == [
        ("notice.access_episode", [offering_row], "done")], r6_tasks
    tasks = [tuple(row) for row in sql.execute(
        "SELECT id, task_type, payload, idempotency_key, state, attempts, last_error FROM tasks "
        "WHERE task_type NOT LIKE 'notice.%' ORDER BY id")]
    assert [row[1:6] for row in tasks] == [
        (PROFILE_TASK, {"bot_id": internal}, f"{PROFILE_TASK}:{internal}:1:active", "failed", 2),
        (PROFILE_TASK, {"bot_id": internal}, f"{PROFILE_TASK}:{internal}:2:active", "failed", 2)]
    (rejected_task, *_, rejected_error), (request_task, *_, request_error) = tasks
    assert all(isinstance(e, str) and TYPE_NAME.fullmatch(e) for e in (rejected_error, request_error)), (
        rejected_error, request_error)
    assert _bot_row(sql, "state, access_problem, profile_state, token_version, lease_owner, lease_until", bot) == (
        "registered", None, "failed", 2, None, None)

    def lease(change: str, reason: str) -> dict:
        return {"offering_id": oid, "worker_id": PROFILE_WORKER_ID, "change": change, "reason": reason}

    failed_profile = {"offering_id": oid, "profile_state": "failed", "purpose": "active"}
    # Os eventos dos avisos saem da lista exata;
    # contrapartida exata: só o `notice.recorded` do aviso do episódio, sem destinatário (horário não fixado).
    (lost_seq,) = [e["seq"] for e in events if e["type"] == "bot.access_changed" and e["data"]["access"] == "lost"]
    assert [(e["type"], e["data"]) for e in sorted(events, key=lambda e: e["seq"]) if e["type"] in REPORT_EVENT_TYPES] == [
        ("notice.recorded", {"offering_id": oid, "notice_type": "bot_access_restored", "dispatch_id": None,
                             "report_version": None, "episode_seq": lost_seq, "batch_id": None,
                             "window_started_at": None, "local_date": None, "state": "no_recipient",
                             "recipient_count": 0})]
    assert [(e["type"], e["occurred_at"], e["data"]) for e in sorted(events, key=lambda e: e["seq"])
            if e["type"] not in REPORT_EVENT_TYPES] == [
        ("offering.created", iso(started), {"offering_id": oid, "code": PROFILE_OFFERING}),
        ("bot.registered", iso(started), {"offering_id": oid, "telegram_bot_id": bot, "username": username}),
        ("offering.state_changed", iso(started), {"offering_id": oid, "from_state": "draft", "to_state": "active"}),
        ("bot.lease_changed", iso(started), lease("acquired", "acquired")),
        ("bot.lease_changed", iso(after_first), lease("released", "no_access")),
        ("bot.access_changed", iso(after_first), {"offering_id": oid, "access": "lost", "problem": "token_rejected"}),
        ("bot.profile_updated", iso(after_first), failed_profile),
        ("task.failed", iso(after_first), {"task_id": rejected_task, "task_type": PROFILE_TASK, "attempts": 2,
                                           "error": rejected_error}),
        ("bot.token_replaced", iso(after_first), {"offering_id": oid, "telegram_bot_id": bot, "token_version": 2}),
        ("bot.access_changed", iso(after_first), {"offering_id": oid, "access": "restored", "problem": None}),
        ("bot.lease_changed", iso(after_first), lease("acquired", "acquired")),
        ("bot.profile_updated", iso(after_second), failed_profile),
        ("task.failed", iso(after_second), {"task_id": request_task, "task_type": PROFILE_TASK, "attempts": 2,
                                            "error": request_error}),
        ("bot.lease_changed", iso(after_second), lease("released", "shutdown"))]
    # Contra o vazio: as varreduras de log leram os registros do adaptador e do worker.
    _assert_transport_exercised(surfaces.transport, bot_api)
    records = _worker_records(surfaces)
    for change, reason in (("acquired", "acquired"), ("released", "shutdown")):
        assert _has_record(records, {"event": "bot.lease", "offering_id": oid, "change": change,
                                     "reason": reason}), f"sem registro bot.lease {change}/{reason}"
    assert {tid: [r.get("state") for r in records if r.get("event") == "task.processed" and r.get("id") == tid]
            for tid in (rejected_task, request_task)} == {rejected_task: ["pending", "failed"],
                                                          request_task: ["pending", "failed"]}

    # --- Varreduras ---
    secrets = _secrets(bot_api)
    assert {first, second, MASTER_KEY, OTHER_MASTER_KEY} <= set(secrets)
    _assert_exchanges_clean(surfaces.exchanges, secrets)
    _assert_logs_clean(surfaces, secrets)
    _check_fd(capfd, surfaces.caught, secrets, second)
    _assert_telegram_params_clean(bot_api, secrets)
    _assert_ciphertexts(sql, {bot: second})
    _assert_database_clean(db_url, secrets, invite_codes=[], start_texts=[], api_secrets=[])
    _assert_scan_catches_temp_table(db_url, second)
    _assert_transport_capture_catches(bot_api, second)
    _assert_sweeps_catch(second)


def test_worker_process_output_has_no_token_or_master_key(
        client: Any, bot_api: Any, clock: Any, db_url: str, sql: psycopg.Connection, capfd: Any) -> None:
    """A saída padrão e a de erro (fd 1 e 2) do `cq-server worker` de verdade, num subprocesso com a
    chave-mestra no ambiente, não têm token, chave-mestra, link nem código de convite.

    Sigilo (a saída padrão e a de erro dos processos); a chave-mestra vem do ambiente e nunca sai;
    subprocesso apontado para a Bot API falsa (`SystemClock`); vínculo e resposta (o subprocesso
    consulta, grava, vincula e responde, com uma falha de servidor na consulta e outra na entrega);
    revogação; controle positivo: a saída tem os registros JSON do worker (`worker.started`,
    `telegram.update`) e o detector acharia o token nela.
    """
    person = register_person(client, ADMIN_TOKEN, name="Professora Segredo Processo", ra="5500000091")
    create_offering(client, ADMIN_TOKEN, code=PROCESS_OFFERING)
    bot = bot_api.add_bot()
    register_bot(client, ADMIN_TOKEN, PROCESS_OFFERING, bot_api.token_of(bot))
    activate_bot(client, ADMIN_TOKEN, PROCESS_OFFERING)
    code = link_code(create_team_link(client, ADMIN_TOKEN, PROCESS_OFFERING, person["alias"])["link"])
    user = make_user(91)
    bot_api.fail("getUpdates", status=500, bot_id=bot)
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": PROCESS_WORKER_ID})
    try:
        bot_api.wait_for(lambda: [c for c in bot_api.calls("getUpdates", bot_id=bot) if c.outcome == "ok"], timeout=30)
        bot_api.push_start(bot, user, code)
        bot_api.wait_for(lambda: bot_api.sent_messages(bot, chat_id=user.user_id), timeout=30)
        bot_api.fail("sendMessage", status=500, bot_id=bot)
        bot_api.push_text(bot, user, "/ajuda", bot_command=True)
        bot_api.wait_for(lambda: len(bot_api.sent_messages(bot, chat_id=user.user_id)) >= 2, timeout=30)
        bot_api.revoke(bot)
        _wait_until(lambda: _bot_row(sql, "state, access_problem", bot) == ("no_access", "token_rejected"), 30,
                    "o subprocesso não deixou o bot revogado sem acesso")
    finally:
        proc.stop()
    out, err = proc.output()

    secrets = _secrets(bot_api)
    assert {bot_api.token_of(bot), MASTER_KEY, code} <= set(secrets)
    for channel, text in (("saída padrão", out), ("saída de erro", err)):
        hits = find_secrets(text, secrets)
        assert hits == [], f"segredo na {channel} do cq-server worker: {hits}"
    # Controle positivo: a saída capturada é o log JSON do worker, com o trabalho feito.
    records: list[dict] = []
    not_json = 0
    for line in out.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            not_json += 1
    assert not_json == 0, f"{not_json} linhas da saída padrão do worker fora de JSON"
    assert _has_record(records, {"event": "worker.started", "worker_id": PROCESS_WORKER_ID})
    assert _has_record(records, {"event": "telegram.update", "outcome": "team_joined"})
    assert _has_record(records, {"event": "telegram.update", "outcome": "help"})
    assert [(c.outcome, c.status) for c in bot_api.calls("getUpdates", bot_id=bot) if c.programmed] == [("error", 500)]
    assert [(c.outcome, c.status) for c in bot_api.calls("sendMessage", bot_id=bot)] == [
        ("ok", 200), ("error", 500), ("ok", 200)]
    assert find_secrets(out + f"\n{bot_api.token_of(bot)}\n", secrets), "o detector não acharia o token na saída"
    _check_fd(capfd, [], secrets, bot_api.token_of(bot))
    _assert_telegram_params_clean(bot_api, secrets)


def test_bot_token_shape_is_redacted_in_free_text(
        client: Any, bot_api: Any, clock: Any, db_url: str, sql: psycopg.Connection, capfd: Any) -> None:
    """A redação troca por `[segredo]` o token de bot (conhecido ou só com a forma) no texto livre.

    Forma do token (qualquer texto com a forma de um token de bot); redação do texto livre
    (disciplina redigida na gravação e na saída; rótulo de token da API também); texto livre (disciplina
    marcada como texto livre); sigilo (o token não fica no banco nem sai em resposta, log ou evento).
    """
    started = clock.now()
    bot = bot_api.add_bot()
    known = bot_api.token_of(bot)
    shaped = "987654321:" + "Qq7-_" * 7  # a forma do token, de um bot que o sistema nunca viu
    remember_secret(shaped)
    with _sweep(client) as surfaces:
        created = create_offering(client, ADMIN_TOKEN, code=REDACTION_OFFERING, course=f"Oficina {known} e {shaped}")
        assert created == {
            "offering_id": created["offering_id"], "code": REDACTION_OFFERING,
            "course": "Oficina [segredo] e [segredo]", "term": DEFAULT_TERM, "timezone": TIMEZONE,
            "response_window_minutes": 1440, "state": "draft", "created_at": iso(started), "activated_at": None,
            "closed_at": None, "archived_at": None, "bot": None, "free_messages": False, "contact_email_set": False,
            # O `Offering` traz `purged_at`, nulo sem expurgo.
            "purged_at": None}
        resp = client.get(f"/v1/offerings/{REDACTION_OFFERING}", headers=ADMIN)
        assert resp.status_code == 200 and resp.json() == created
        _, token_body = create_token(client, ADMIN_TOKEN, ["offerings:read"], label=f"rotulo {known}")
        assert token_body["label"] == "rotulo [segredo]"
        events = all_events(client, ADMIN_TOKEN, "token.created", "offering.created")
    assert sql.execute("SELECT course FROM offerings WHERE code = %s", (REDACTION_OFFERING,)).fetchall() == [
        ("Oficina [segredo] e [segredo]",)]
    assert [(e["type"], e["data"]) for e in events] == [
        ("offering.created", {"offering_id": created["offering_id"], "code": REDACTION_OFFERING}),
        ("token.created", {"token_id": token_body["token_id"], "label": "rotulo [segredo]",
                           "permissions": ["offerings:read"]})]
    secrets = _secrets(bot_api)
    assert {known, shaped} <= set(secrets)
    _assert_exchanges_clean(surfaces.exchanges, secrets)
    _assert_logs_clean(surfaces, secrets)
    _check_fd(capfd, surfaces.caught, secrets, known)
    api_secrets = [s for s in all_secrets() if s.startswith("cq_") and s != ADMIN_TOKEN]
    _assert_database_clean(db_url, secrets, invite_codes=[], start_texts=[], api_secrets=api_secrets)
    assert bot_api.calls() == [], "o texto livre com a forma do token não chama o Telegram"
