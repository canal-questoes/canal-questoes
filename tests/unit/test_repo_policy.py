"""Política de repositório (a validação da referência, a configuração das variáveis do repositório e
a porta e o adaptador de obtenção).


O que se confere aqui, sem rede e sem `git`:
- `questionrefs.validate_repo(repo, allowed_hosts)`: um controle positivo por regra (o par recusado e o
  aceito diferem só no ponto da regra), com o código exato (`repo_length` · `repo_syntax` ·
  `repo_dot_segment` · `host_not_allowed`), e a lista vazia recusando tudo; `repo_key` com os vetores
  fixos do motor (`engine/test/unit/reference.test.ts`), na mesma função de um caso de `validate_repo`
  (`repo_key` já é real no esqueleto);
- `RepoSettings.from_env(env)`: os padrões (vazio vale como ausente), a leitura das 5 variáveis e as
  recusas com mensagem fixa **sem o valor** (nem no `repr`, nos `args` ou no traceback);
  `check_fetch_timeout` no limite (`prazo + 15 == posse`) e acima;
- a conferência de endereços do `GitRepoFetcher` com um resolvedor injetado (a forma do
  `socket.getaddrinfo`, que é o padrão do adaptador): todo endereço não global (loopback, 10/8,
  172.16/12, 192.168/16, 169.254/16, 100.64/10, `fd00::/8`, `fe80::/10`, `::ffff:127.0.0.1`, NAT64 e 6to4,
  multicast) → `FetchRefused("address_not_global")` sem processo nenhum e sem conexão; um endereço global
  (`8.8.8.8`) segue até o `git`, com o DNS fixado em `http.curloptResolve` e o ambiente sem `CQ_*`;
- a classificação do `stderr` do `git`, um caso por padrão. O `git` nunca roda: o
  `subprocess.Popen` (que o `subprocess.run` também usa) é trocado por um processo falso que devolve o
  código 128 e o `stderr` do caso na chamada `fetch` (e sucesso nas demais), e o `git_binary` aponta para
  um caminho que não existe. O texto do `stderr` (com uma sentinela) não aparece no resultado, nos logs
  nem em fd 1 e 2. [I] O `curl 28` (a transferência abortada pelo `http.lowSpeedLimit/Time`, que é parte
  do teto de tempo da obtenção) conta como `timeout`; o prazo total com o grupo de processos morto fica
  para o teste de sistema, porque o `os.killpg` e o `prlimit` não existem no Windows;
- `cq_server.__main__.build_repo_fetcher(env, settings)` em processo, com `capture_logs` e `capfd`: sem
  raiz, raiz vazia, raiz que é arquivo, inexistente ou sem escrita (`os.access` negado por `monkeypatch`
  só naquela raiz) → `None` e um único `worker.repos_disabled` (WARNING, `cq_server.worker`); raiz
  relativa → `ConfigError`; configurado → o `GitRepoFetcher`, sem registro;
- dois subprocessos: `python -m cq_server worker` com `CQ_REPOS_ROOT` relativo e `python -m cq_server
  serve` com `CQ_REPO_ALLOWED_HOSTS` inválido → código 2 e `{logger: "cq_server.main", event:
  "config.invalid", detail: <mensagem exata>}`, sem subir (nenhum dos dois afirma a contagem total de
  registros; o `serve` lê a configuração antes do `_migrate`, então o banco vazio continua vazio).

As mensagens de `ConfigError` seguem o molde do `config.py` (as de `_positive_int`/`_positive_float` e a da
invariante do SMTP); a regra é só que as mensagens são fixas, sem o valor, e este teste as fixa.

No esqueleto, `validate_repo`, `RepoSettings.from_env`, `check_fetch_timeout`, `GitRepoFetcher.ensure` e
`build_repo_fetcher` lançam `NotImplementedError`, e o `serve`/`worker` ainda não leem as variáveis:
todo caso falha. Os casos do `GitRepoFetcher` passam pelo adaptador
(a conferência de endereços e a
classificação do `stderr` pelo caminho do `ensure` até o `git fetch`, que o processo falso intercepta:
nenhum `git` roda).
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from cq_server.adapters.git import GitRepoFetcher
from cq_server.config import ConfigError, RepoSettings, Settings, check_fetch_timeout
from cq_server.ports.repos import FetchNotFound, FetchRefused, FetchTemporaryFailure
from cq_server.questionrefs import repo_key, validate_repo
from tests.helpers.logs import capture_logs
from tests.helpers.processes import WorkerProcess, spawn_worker, worker_env

# --- mensagens exatas (fixas, sem o valor) -------------------------------------------------------

MSG_HOSTS = ("CQ_REPO_ALLOWED_HOSTS deve ser uma lista, separada por vírgulas, de nomes DNS em minúsculas "
             "com ponto, sem porta nem endereço IP.")
MSG_ROOT = "CQ_REPOS_ROOT deve ser um caminho absoluto."
MSG_TIMEOUT = "CQ_REPO_FETCH_TIMEOUT_SECONDS deve ser um número finito maior que zero."
MSG_MAX_BYTES = "CQ_REPO_MAX_BYTES deve ser um número inteiro maior que zero."
MSG_MAX_FILES = "CQ_REPO_MAX_FILES deve ser um número inteiro maior que zero."
MSG_LEASE = "CQ_REPO_FETCH_TIMEOUT_SECONDS mais 15 deve caber em CQ_LEASE_SECONDS."

# --- valores ---------------------------------------------------------------------------------------

HOST = "repos.cq.test"
ALLOWED = frozenset({HOST})
REPO = f"https://{HOST}/prairielearn/example-course"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
# Vetores fixos do motor (engine/test/unit/reference.test.ts:20-34).
ENGINE_KEY_VECTORS = {
    "https://github.com/PrairieLearn/PrairieLearn":
        "70cd87d835005aa983a0ab4a076a50b801266e9fce00829051d0f5451e0d1cd7",
    "fixtures/cq-test-course": "511bd4373e5ce17ffaf75f5c484e80afcbaa28b65bd633c30242ffc5ed512f8d",
    "fixtures/cq-override-course": "c92033d9d541cdab2d8b535eb6711fda5deb07263a94b0c1e4be09ca1f74b892",
}
# Os padrões de `RepoSettings`.
DEFAULTS = {"allowed_hosts": frozenset(), "repos_root": None, "fetch_timeout_seconds": 40,
            "max_bytes": 104_857_600, "max_files": 20_000}
REPO_VARS = ("CQ_REPO_ALLOWED_HOSTS", "CQ_REPOS_ROOT", "CQ_REPO_FETCH_TIMEOUT_SECONDS", "CQ_REPO_MAX_BYTES",
             "CQ_REPO_MAX_FILES")
# Sentinela do `stderr` do git: nunca pode sair no resultado, no log nem em fd 1 e 2.
STDERR_SENTINEL = "SENTINELA-STDERR-9c41"
SUBPROCESS_TIMEOUT = 60.0  # subida de um processo Python novo com o pacote inteiro (como no vínculo do aluno)


def _settings(lease_seconds: float = 60) -> Settings:
    """O `Settings` da API base sem banco de verdade (a posse é a única coisa lida aqui)."""
    return Settings(database_url="postgresql://cq@127.0.0.1:9/cq", lease_seconds=lease_seconds)


def _assert_config_error(call: Callable[[], object], message: str, *, hidden: tuple[str, ...] = ()) -> None:
    """`call()` lança `ConfigError` com `message` exata; nem a mensagem, nem o `repr`, nem os `args`
    contêm os textos de `hidden`, e o traceback formatado (com as exceções encadeadas) não contém os que
    trazem a marca `sentinela` (os demais, como `localhost`, podem aparecer legitimamente no código-fonte
    citado pelo traceback)."""
    with pytest.raises(ConfigError) as excinfo:
        call()
    exc = excinfo.value
    assert str(exc) == message
    assert exc.args == (message,)
    for text in (str(exc), repr(exc), *map(str, exc.args)):
        for value in hidden:
            assert value not in text, f"o valor {value!r} apareceu no erro"
    formatted = "".join(traceback.format_exception(exc))
    for value in hidden:
        if "sentinela" in value.lower():
            assert value not in formatted, f"o valor {value!r} apareceu no traceback"


# ---------------------------------------------------------------------------------------------
# validate_repo e repo_key
# ---------------------------------------------------------------------------------------------

VALID_REPOS = [
    REPO,
    f"https://{HOST}/prairielearn/example-course.git",
    f"https://{HOST}/prairielearn/example-course/",
    f"https://{HOST}/Dono_1/Curso~2.v3-final",
    f"https://{HOST}/a/.oculto/b..c/...",
]


def test_validate_repo_accepts_valid_references_and_repo_key_matches_the_engine() -> None:
    """A referência válida é aceita como informada, sem normalizar caixa nem `.git`; a regex
    fechada: só `https`, host DNS em minúsculas com ponto, caminho com
    `[A-Za-z0-9._~-]`; segmentos como `.oculto`, `b..c` e `...` não são `.`/`..`; `repo_key`
    (real no esqueleto) é o SHA-256 hex do UTF-8, igual aos vetores fixos do motor, afirmado na mesma
    função de um caso de `validate_repo`."""
    for repo in VALID_REPOS:
        assert validate_repo(repo, ALLOWED) is None, repo
    # A lista com mais de um host: basta o do `repo` estar nela (comparação exata do host).
    assert validate_repo(REPO, frozenset({"git.example.org", HOST})) is None

    for repo, key in ENGINE_KEY_VECTORS.items():
        assert repo_key(repo) == key
    assert repo_key("repositório/ç") == hashlib.sha256("repositório/ç".encode("utf-8")).hexdigest()
    assert repo_key(REPO) != repo_key(REPO + ".git")  # duas grafias, duas chaves


LONG_PREFIX = f"https://{HOST}/"
URL_2048 = LONG_PREFIX + "a" * (2048 - len(LONG_PREFIX))
URL_2049 = URL_2048 + "b"

# (id, recusado, aceito que difere só na regra, lista permitida, código). Nos casos de forma, a lista
# também contém o host "recusado" (quando ele é um texto de host), para que o motivo não seja a lista.
RULE_CASES = [
    pytest.param(f"https://usuario@{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="userinfo"),
    pytest.param(f"https://usuario:senha@{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax",
                 id="userinfo-com-senha"),
    pytest.param(f"https://{HOST}/x?ref=main", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="consulta"),
    pytest.param(f"https://{HOST}/x#main", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="fragmento"),
    pytest.param(f"https://{HOST}/x%2e%2e/y", f"https://{HOST}/x2e2e/y", ALLOWED, "repo_syntax",
                 id="percent"),
    pytest.param(f"https://{HOST}/a\\b", f"https://{HOST}/a/b", ALLOWED, "repo_syntax", id="barra-invertida"),
    pytest.param(f"https://{HOST}\\@outro.example.org/x", f"https://{HOST}/x", ALLOWED, "repo_syntax",
                 id="barra-invertida-no-host"),
    pytest.param(f"https://{HOST}:443/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="porta"),
    pytest.param("https://127.0.0.1/x", f"https://{HOST}/x", frozenset({"127.0.0.1", HOST}), "repo_syntax",
                 id="ip-v4"),
    pytest.param("https://[::1]/x", f"https://{HOST}/x", frozenset({"[::1]", "::1", HOST}), "repo_syntax",
                 id="ip-v6"),
    pytest.param("https://Repos.cq.test/x", f"https://{HOST}/x", frozenset({"Repos.cq.test", HOST}),
                 "repo_syntax", id="maiusculas-no-host"),
    pytest.param("https://engine/x", f"https://{HOST}/x", frozenset({"engine", HOST}), "repo_syntax",
                 id="host-sem-ponto"),
    pytest.param("https://repós.cq.test/x", f"https://{HOST}/x", frozenset({"repós.cq.test", HOST}),
                 "repo_syntax", id="host-nao-ascii"),
    pytest.param(f"https://{HOST}/a b", f"https://{HOST}/a-b", ALLOWED, "repo_syntax", id="espaco"),
    pytest.param(f"https://{HOST}", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="sem-caminho"),
    pytest.param(f"http://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="http"),
    pytest.param(f"git://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="git"),
    pytest.param(f"file://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="file"),
    pytest.param(f"ssh://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="ssh"),
    pytest.param(f"HTTPS://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="esquema-maiusculo"),
    pytest.param(f" https://{HOST}/x", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="espaco-inicial"),
    pytest.param(f"https://{HOST}/x\n", f"https://{HOST}/x", ALLOWED, "repo_syntax", id="quebra-final"),
    pytest.param(f"https://{HOST}/a/../b", f"https://{HOST}/a/..b/b", ALLOWED, "repo_dot_segment",
                 id="ponto-ponto"),
    pytest.param(f"https://{HOST}/./b", f"https://{HOST}/.b/b", ALLOWED, "repo_dot_segment", id="ponto"),
    pytest.param(f"https://{HOST}/a/..", f"https://{HOST}/a/...", ALLOWED, "repo_dot_segment",
                 id="ponto-ponto-final"),
    pytest.param(f"https://{HOST}/a/./", f"https://{HOST}/a/.x/", ALLOWED, "repo_dot_segment",
                 id="ponto-antes-da-barra-final"),
    pytest.param(URL_2049, URL_2048, ALLOWED, "repo_length", id="comprimento-2049"),
    pytest.param("https://git.example.org/x", f"https://{HOST}/x", ALLOWED, "host_not_allowed",
                 id="host-fora-da-lista"),
    pytest.param(f"https://sub.{HOST}/x", f"https://{HOST}/x", ALLOWED, "host_not_allowed",
                 id="subdominio-fora-da-lista"),
    pytest.param(f"https://{HOST}.evil.example/x", f"https://{HOST}/x", ALLOWED, "host_not_allowed",
                 id="sufixo-fora-da-lista"),
]


@pytest.mark.parametrize(("bad", "good", "allowed", "code"), RULE_CASES)
def test_validate_repo_rule_with_positive_control(bad: str, good: str, allowed: frozenset[str],
                                                   code: str) -> None:
    """`repo` sem userinfo, consulta, fragmento, fora de `https` ou com segmento `.`/`..` →
    recusado sem eco; host exato da lista; a regex fechada também recusa
    `%`, `\\`, porta, IP, maiúsculas no host e host sem ponto, e o comprimento fora de 1 a 2048:
    cada regra devolve o seu código, e o controle positivo, que só muda o ponto da regra, devolve
    `None`."""
    assert validate_repo(bad, allowed) == code
    assert validate_repo(good, allowed) is None


def test_validate_repo_empty_allowlist_refuses_everything() -> None:
    """`CQ_REPO_ALLOWED_HOSTS` vazia recusa tudo: com a lista vazia, toda
    referência de forma válida → `host_not_allowed`; com o host na lista, a mesma referência passa
    (controle positivo)."""
    for repo in VALID_REPOS:
        assert validate_repo(repo, frozenset()) == "host_not_allowed", repo
        assert validate_repo(repo, ALLOWED) is None, repo


# ---------------------------------------------------------------------------------------------
# RepoSettings
# ---------------------------------------------------------------------------------------------


def test_repo_settings_defaults_from_empty_or_blank_env() -> None:
    """Padrões: lista vazia, raiz ausente, 40 s, 100 MiB, 20 000 arquivos; e
    cada variável vazia vale como ausente, porque o Compose as repassa por `${VAR:-}`. Os
    padrões da dataclass (reais no esqueleto) afirmados na mesma função de um `from_env`."""
    assert dataclasses.asdict(RepoSettings.from_env({})) == DEFAULTS
    blank = RepoSettings.from_env({name: "" for name in REPO_VARS})
    assert dataclasses.asdict(blank) == DEFAULTS
    assert blank == RepoSettings()
    assert dataclasses.asdict(RepoSettings()) == DEFAULTS
    # Variáveis de outros grupos não mudam nada.
    assert RepoSettings.from_env({"CQ_ENGINE_URL": "http://engine:8080", "CQ_SMTP_HOST": "x"}) == RepoSettings()


def test_repo_settings_reads_every_variable(tmp_path: pathlib.Path) -> None:
    """as 5 variáveis lidas de `env` (a lista separada por vírgula,
    a raiz absoluta, o prazo como número finito, os tetos inteiros), cada uma no seu campo; o prazo no
    limite da posse padrão (`45 + 15 == 60`) é aceito por `check_fetch_timeout`."""
    root = str(tmp_path / "repos")
    env = {"CQ_REPO_ALLOWED_HOSTS": "repos.cq.test,git.example.org,gitlab.cursos.example.edu.br",
           "CQ_REPOS_ROOT": root, "CQ_REPO_FETCH_TIMEOUT_SECONDS": "12.5", "CQ_REPO_MAX_BYTES": "1000",
           "CQ_REPO_MAX_FILES": "10"}
    settings = RepoSettings.from_env(env)
    assert dataclasses.asdict(settings) == {
        "allowed_hosts": frozenset({"repos.cq.test", "git.example.org", "gitlab.cursos.example.edu.br"}),
        "repos_root": root, "fetch_timeout_seconds": 12.5, "max_bytes": 1000, "max_files": 10}
    assert type(settings.allowed_hosts) is frozenset
    assert type(settings.max_bytes) is int and type(settings.max_files) is int
    single = RepoSettings.from_env({"CQ_REPO_ALLOWED_HOSTS": HOST, "CQ_REPO_FETCH_TIMEOUT_SECONDS": "45"})
    assert single.allowed_hosts == ALLOWED
    assert single.fetch_timeout_seconds == 45
    assert check_fetch_timeout(single, _settings(60)) is None


INVALID_ENVS = [
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "localhost"}, MSG_HOSTS, ("localhost",), id="hosts-localhost"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "127.0.0.1"}, MSG_HOSTS, ("127.0.0.1",), id="hosts-ipv4-loopback"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "8.8.8.8"}, MSG_HOSTS, ("8.8.8.8",), id="hosts-ipv4-global"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "[::1]"}, MSG_HOSTS, ("[::1]",), id="hosts-ipv6-colchetes"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "::1"}, MSG_HOSTS, (), id="hosts-ipv6"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "sentinela-sem-ponto"}, MSG_HOSTS, ("sentinela-sem-ponto",),
                 id="hosts-sem-ponto"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "repos.sentinela.test:443"}, MSG_HOSTS,
                 ("repos.sentinela.test",), id="hosts-com-porta"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "Repos.Sentinela.test"}, MSG_HOSTS, ("Repos.Sentinela.test",),
                 id="hosts-maiusculas"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "https://repos.sentinela.test"}, MSG_HOSTS,
                 ("repos.sentinela.test",), id="hosts-com-esquema"),
    pytest.param({"CQ_REPO_ALLOWED_HOSTS": "repos.cq.test,localhost"}, MSG_HOSTS, ("localhost",),
                 id="hosts-um-invalido-na-lista"),
    pytest.param({"CQ_REPOS_ROOT": "raiz-relativa-sentinela"}, MSG_ROOT, ("raiz-relativa-sentinela",),
                 id="raiz-relativa"),
    pytest.param({"CQ_REPOS_ROOT": "./sentinela/repos"}, MSG_ROOT, ("sentinela",), id="raiz-ponto-barra"),
    pytest.param({"CQ_REPO_FETCH_TIMEOUT_SECONDS": "0"}, MSG_TIMEOUT, (), id="prazo-0"),
    pytest.param({"CQ_REPO_FETCH_TIMEOUT_SECONDS": "-1"}, MSG_TIMEOUT, (), id="prazo-negativo"),
    pytest.param({"CQ_REPO_FETCH_TIMEOUT_SECONDS": "nan"}, MSG_TIMEOUT, ("nan",), id="prazo-nan"),
    pytest.param({"CQ_REPO_FETCH_TIMEOUT_SECONDS": "inf"}, MSG_TIMEOUT, (), id="prazo-inf"),
    pytest.param({"CQ_REPO_FETCH_TIMEOUT_SECONDS": "quarenta-sentinela"}, MSG_TIMEOUT,
                 ("quarenta-sentinela",), id="prazo-texto"),
    pytest.param({"CQ_REPO_MAX_BYTES": "sentinela-bytes"}, MSG_MAX_BYTES, ("sentinela-bytes",),
                 id="bytes-texto"),
    pytest.param({"CQ_REPO_MAX_BYTES": "1.5"}, MSG_MAX_BYTES, ("1.5",), id="bytes-fracao"),
    pytest.param({"CQ_REPO_MAX_BYTES": "0"}, MSG_MAX_BYTES, (), id="bytes-0"),
    pytest.param({"CQ_REPO_MAX_BYTES": "-1"}, MSG_MAX_BYTES, (), id="bytes-negativo"),
    pytest.param({"CQ_REPO_MAX_FILES": "sentinela-arquivos"}, MSG_MAX_FILES, ("sentinela-arquivos",),
                 id="arquivos-texto"),
    pytest.param({"CQ_REPO_MAX_FILES": "2.5"}, MSG_MAX_FILES, ("2.5",), id="arquivos-fracao"),
    pytest.param({"CQ_REPO_MAX_FILES": "0"}, MSG_MAX_FILES, (), id="arquivos-0"),
    pytest.param({"CQ_REPO_MAX_FILES": "-3"}, MSG_MAX_FILES, (), id="arquivos-negativo"),
]


@pytest.mark.parametrize(("env", "message", "hidden"), INVALID_ENVS)
def test_repo_settings_rejects_invalid_values_without_echo(env: dict[str, str], message: str,
                                                           hidden: tuple[str, ...]) -> None:
    """A lista só com nomes DNS em minúsculas **com ponto**, sem porta, IP nem
    `localhost` (a recusa de produção já na configuração); a raiz absoluta; o prazo finito
    > 0; os tetos inteiros > 0; mensagens fixas, sem o valor: cada valor inválido
    → `ConfigError` com a mensagem exata, e o valor não aparece na mensagem, no `repr`, nos `args` nem
    no traceback."""
    _assert_config_error(lambda: RepoSettings.from_env(env), message, hidden=hidden)


@pytest.mark.parametrize(("timeout", "lease", "accepted"), [
    pytest.param(40, 55, True, id="padrao-40-no-limite-55"),
    pytest.param(40, 54.9, False, id="padrao-40-acima-de-54.9"),
    pytest.param(45, 60, True, id="45-no-limite-da-posse-padrao"),
    pytest.param(45.5, 60, False, id="45.5-acima-da-posse-padrao"),
    pytest.param(15, 30, True, id="15-no-limite-da-posse-dos-testes"),
    pytest.param(16, 30, False, id="16-acima-da-posse-dos-testes"),
])
def test_check_fetch_timeout_limit(timeout: float, lease: float, accepted: bool) -> None:
    """Invariante `CQ_REPO_FETCH_TIMEOUT_SECONDS + 15 <=
    CQ_LEASE_SECONDS`, como a do SMTP: no limite, aceito (`None`); acima, `ConfigError` com a
    mensagem exata, sem os valores."""
    repo = RepoSettings(fetch_timeout_seconds=timeout)
    if accepted:
        assert check_fetch_timeout(repo, _settings(lease)) is None
    else:
        _assert_config_error(lambda: check_fetch_timeout(repo, _settings(lease)), MSG_LEASE)


# ---------------------------------------------------------------------------------------------
# GitRepoFetcher: endereços e classificação do git
# ---------------------------------------------------------------------------------------------


class _Resolver:
    """Resolvedor injetado com a forma do `socket.getaddrinfo(host, port, …)`: devolve os endereços
    do caso (ou levanta `error`) e anota os hosts pedidos. Nenhum DNS de verdade."""

    def __init__(self, *addresses: str, error: BaseException | None = None) -> None:
        self.addresses = addresses
        self.error = error
        self.hosts: list[str] = []

    def __call__(self, host: Any, port: Any, *args: Any, **kwargs: Any) -> list[tuple]:
        self.hosts.append(host.decode("ascii") if isinstance(host, bytes) else str(host))
        if self.error is not None:
            raise self.error
        infos = []
        for address in self.addresses:
            if ":" in address:
                infos.append((socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                              (address, 443, 0, 0)))
            else:
                infos.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)))
        return infos


class _FakeProcess:
    """Um processo falso com a interface de `subprocess.Popen` que o adaptador usa (diretamente ou por
    `subprocess.run`): `communicate`, `wait`, `poll`, os canais e o código de saída."""

    def __init__(self, args: Any, kwargs: dict[str, Any], returncode: int, stderr_text: str) -> None:
        self.args = args
        self.pid = 2 ** 22 + 4242  # acima do `pid_max` usual: um `killpg` acidental não acha ninguém
        self.returncode: int | None = None
        self._final = returncode
        text = bool(kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")
                    or kwargs.get("errors"))
        err_bytes = stderr_text.encode("utf-8")
        self.stdin = io.BytesIO() if kwargs.get("stdin") == subprocess.PIPE else None
        out_target = kwargs.get("stdout")
        err_target = kwargs.get("stderr")
        stdout_data = b""
        stderr_data: bytes | None = None
        if err_target == subprocess.STDOUT:
            stdout_data += err_bytes
        elif err_target == subprocess.PIPE:
            stderr_data = err_bytes
        elif hasattr(err_target, "write"):
            try:
                err_target.write(err_bytes)
            except TypeError:
                err_target.write(stderr_text)
        elif isinstance(err_target, int) and err_target >= 0:
            os.write(err_target, err_bytes)

        def wrap(data: bytes | None) -> Any:
            if data is None:
                return None
            return io.StringIO(data.decode("utf-8")) if text else io.BytesIO(data)

        self._stdout_data = stdout_data if out_target == subprocess.PIPE else None
        self._stderr_data = stderr_data
        self._text = text
        self.stdout = wrap(self._stdout_data)
        self.stderr = wrap(self._stderr_data)

    def _decode(self, data: bytes | None) -> Any:
        if data is None:
            return None
        return data.decode("utf-8") if self._text else data

    def communicate(self, input: Any = None, timeout: float | None = None) -> tuple[Any, Any]:
        self.returncode = self._final
        return self._decode(self._stdout_data), self._decode(self._stderr_data)

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = self._final
        return self._final

    def poll(self) -> int:
        self.returncode = self._final
        return self._final

    def kill(self) -> None:
        if self.returncode is None:
            self.returncode = -9

    terminate = kill

    def send_signal(self, sig: int) -> None:
        self.kill()

    def __enter__(self) -> _FakeProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        for stream in (self.stdout, self.stderr, self.stdin):
            if stream is not None:
                stream.close()
        self.wait()


class _FakeGit:
    """Substitui o `subprocess.Popen`: anota cada chamada (`argv` e `env`) e devolve um processo falso;
    a chamada `fetch` sai com `fetch_code` e `fetch_stderr`, as demais com sucesso e sem saída."""

    def __init__(self, fetch_stderr: str = "", fetch_code: int = 128) -> None:
        self.fetch_stderr = fetch_stderr
        self.fetch_code = fetch_code
        self.calls: list[dict[str, Any]] = []

    def __call__(self, args: Any, *positional: Any, **kwargs: Any) -> _FakeProcess:
        argv = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a)
                for a in ([args] if isinstance(args, (str, bytes, os.PathLike)) else args)]
        self.calls.append({"argv": argv, "env": kwargs.get("env")})
        if "fetch" in argv:
            return _FakeProcess(args, kwargs, self.fetch_code, self.fetch_stderr)
        return _FakeProcess(args, kwargs, 0, "")

    def fetch_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if "fetch" in call["argv"]]


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> _FakeGit:
    """O `git` falso: nenhum processo de verdade sai deste teste."""
    fake = _FakeGit()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return fake


def _fetcher(root: pathlib.Path, resolver: _Resolver) -> GitRepoFetcher:
    """O adaptador de produção (sem `test_options`), com o resolvedor injetado e um binário do git que
    não existe (se o `Popen` falso fosse contornado, nada rodaria)."""
    settings = RepoSettings(allowed_hosts=ALLOWED, repos_root=str(root))
    return GitRepoFetcher(settings, git_binary=str(root.parent / "git-ausente-cq"), resolver=resolver)


def _assert_nothing_published(root: pathlib.Path) -> None:
    """Nem a árvore final nem sobra de staging (`rmtree` do staging no `finally`)."""
    assert not (root / repo_key(REPO) / COMMIT).exists()
    staging = root / ".staging"
    if staging.exists():
        assert list(staging.iterdir()) == []


NON_GLOBAL = [
    pytest.param(("127.0.0.1",), id="loopback-v4"),
    pytest.param(("::1",), id="loopback-v6"),
    pytest.param(("10.1.2.3",), id="10-8"),
    pytest.param(("172.16.5.4",), id="172-16-12"),
    pytest.param(("172.31.255.254",), id="172-16-12-fim"),
    pytest.param(("192.168.1.1",), id="192-168-16"),
    pytest.param(("169.254.1.1",), id="169-254-16"),
    pytest.param(("100.64.0.1",), id="100-64-10"),
    pytest.param(("0.0.0.0",), id="nao-especificado"),
    pytest.param(("192.0.2.1",), id="documentacao"),
    pytest.param(("fd00::1",), id="fd00-8"),
    pytest.param(("fe80::1",), id="fe80-10"),
    pytest.param(("::ffff:127.0.0.1",), id="ipv4-mapeado"),
    pytest.param(("64:ff9b::7f00:1",), id="nat64"),
    pytest.param(("64:ff9b:1::a00:1",), id="nat64-local"),
    pytest.param(("2002:7f00:1::1",), id="6to4"),
    pytest.param(("224.0.0.1",), id="multicast-v4"),
    pytest.param(("ff02::1",), id="multicast-v6"),
    pytest.param(("8.8.8.8", "10.0.0.1"), id="um-global-e-um-privado"),
    pytest.param(("2001:4860:4860::8888", "fe80::1"), id="um-global-v6-e-um-link-local"),
]


@pytest.mark.parametrize("addresses", NON_GLOBAL)
def test_git_fetcher_refuses_non_global_addresses_without_git(
        addresses: tuple[str, ...], tmp_path: pathlib.Path, fake_git: _FakeGit, network_guard: Any,
        capfd: Any) -> None:
    """Endereço de loopback, faixa privada ou link-local recusado (nenhum acesso a serviço
    interno); conferir **todos** os endereços: só `is_global` e não multicast, e fora
    também `64:ff9b::/96`, `64:ff9b:1::/48` e `2002::/16` (com um resolvedor
    injetado): o host permitido que resolve para algum endereço não global →
    `FetchRefused("address_not_global")`, depois de resolver o host exato, sem nenhum processo (o `git`
    nunca roda), sem conexão, sem árvore nem staging, e sem nada em fd 1 e 2."""
    root = tmp_path / "repos"
    root.mkdir()
    resolver = _Resolver(*addresses)
    capfd.readouterr()
    result = _fetcher(root, resolver).ensure(REPO, COMMIT)
    assert result == FetchRefused("address_not_global")
    assert resolver.hosts and set(resolver.hosts) == {HOST}
    assert fake_git.calls == []
    assert network_guard.attempts() == []
    _assert_nothing_published(root)
    assert capfd.readouterr() == ("", "")


def test_git_fetcher_global_address_reaches_git_with_pinned_dns_and_minimal_env(
        tmp_path: pathlib.Path, fake_git: _FakeGit, network_guard: Any, monkeypatch: pytest.MonkeyPatch,
        capfd: Any) -> None:
    """Um endereço global passa (o `git` recebe `http.curloptResolve=<host>:443:<a1>,<a2>`
    com o IPv6 entre colchetes, para não resolver de novo; o ambiente do
    `git` é construído do zero, sem `CQ_*` nem o que o processo tinha; o texto aceito é o texto
    enviado): `8.8.8.8` e `2001:4860:4860::8888` seguem até o `git fetch` com o `repo` e o `commit`
    exatos; o `not our ref` do servidor vira `FetchNotFound("commit")`; nenhuma conexão sai deste
    processo."""
    monkeypatch.setenv("CQ_SENTINELA_AMBIENTE", "valor-sentinela-do-ambiente")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    fake_git.fetch_stderr = f"fatal: remote error: upload-pack: not our ref {COMMIT}\n"
    root = tmp_path / "repos"
    root.mkdir()
    resolver = _Resolver("8.8.8.8", "2001:4860:4860::8888")
    capfd.readouterr()
    result = _fetcher(root, resolver).ensure(REPO, COMMIT)
    assert result == FetchNotFound("commit")
    assert set(resolver.hosts) == {HOST}
    [fetch] = fake_git.fetch_calls()
    assert REPO in fetch["argv"]
    assert COMMIT in fetch["argv"]
    pins = [token.split("http.curloptResolve=", 1)[1] for token in fetch["argv"]
            if "http.curloptResolve=" in token]
    assert len(pins) == 1, fetch["argv"]
    host, port, addresses = pins[0].removeprefix("+").split(":", 2)
    assert (host, port) == (HOST, "443")
    assert set(addresses.split(",")) == {"8.8.8.8", "[2001:4860:4860::8888]"}
    for call in fake_git.calls:
        env = call["env"]
        assert isinstance(env, dict), "o git tem de rodar com o ambiente mínimo explícito"
        assert [key for key in env if key.upper().startswith("CQ_")] == []
        assert "GIT_SSL_NO_VERIFY" not in env
        assert "valor-sentinela-do-ambiente" not in "\n".join(map(str, env.values()))
    assert network_guard.attempts() == []
    _assert_nothing_published(root)
    assert capfd.readouterr() == ("", "")


def test_git_fetcher_resolution_failure_is_temporary_dns(tmp_path: pathlib.Path, fake_git: _FakeGit,
                                                         network_guard: Any) -> None:
    """A falha de resolução (→ `FetchTemporaryFailure("dns")`,
    repetível): o resolvedor que levanta `socket.gaierror` não chega ao `git`."""
    root = tmp_path / "repos"
    root.mkdir()
    resolver = _Resolver(error=socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
    assert _fetcher(root, resolver).ensure(REPO, COMMIT) == FetchTemporaryFailure("dns")
    assert set(resolver.hosts) == {HOST}
    assert fake_git.calls == []
    assert network_guard.attempts() == []
    _assert_nothing_published(root)


URL_IN_ERR = f"'https://{HOST}/{STDERR_SENTINEL}/'"
STDERR_CASES = [
    pytest.param(f"fatal: remote error: upload-pack: not our ref {COMMIT}\n", 128, FetchNotFound("commit"),
                 id="not-our-ref"),
    pytest.param(f"fatal: couldn't find remote ref {COMMIT}{STDERR_SENTINEL}\n", 128, FetchNotFound("commit"),
                 id="couldnt-find-remote-ref"),
    pytest.param(f"fatal: could not read Username for 'https://{HOST}': terminal prompts disabled\n"
                 f"{STDERR_SENTINEL}\n", 128, FetchNotFound("repository"), id="terminal-prompts-disabled"),
    pytest.param(f"remote: Invalid username or password.\nfatal: Authentication failed for {URL_IN_ERR}\n", 128,
                 FetchNotFound("repository"), id="authentication-failed"),
    pytest.param(f"remote: Repository not found.\nfatal: repository {URL_IN_ERR} not found\n", 128,
                 FetchNotFound("repository"), id="repository-not-found"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: SSL certificate problem: self-signed certificate in "
                 "certificate chain\n", 128, FetchNotFound("repository"), id="ssl-certificate-problem"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 301\n", 128,
                 FetchRefused("redirect"), id="redirect-301"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 302\n", 128,
                 FetchRefused("redirect"), id="redirect-302"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 307\n", 128,
                 FetchRefused("redirect"), id="redirect-307"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 308\n", 128,
                 FetchRefused("redirect"), id="redirect-308"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: Could not resolve host: {HOST}\n", 128,
                 FetchTemporaryFailure("dns"), id="could-not-resolve-host"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: Failed to connect to {HOST} port 443 after 3 ms: "
                 "Couldn't connect to server\n", 128, FetchTemporaryFailure("connect"), id="failed-to-connect"),
    pytest.param("error: RPC failed; curl 28 Operation too slow. Less than 1024 bytes/sec transferred the last "
                 f"20 seconds\nfatal: expected flush after ref listing {STDERR_SENTINEL}\n", 128,
                 FetchTemporaryFailure("timeout"), id="curl-28-lento"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: Operation timed out after 20001 milliseconds with 0 "
                 "bytes received\n", 128, FetchTemporaryFailure("timeout"), id="curl-28-timed-out"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 500\n", 128,
                 FetchTemporaryFailure("http_5xx"), id="http-500"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 502\n", 128,
                 FetchTemporaryFailure("http_5xx"), id="http-502"),
    pytest.param(f"fatal: unable to access {URL_IN_ERR}: The requested URL returned error: 503\n", 128,
                 FetchTemporaryFailure("http_5xx"), id="http-503"),
    pytest.param(f"fatal: something unexpected happened {STDERR_SENTINEL}\n", 128,
                 FetchTemporaryFailure("git_error"), id="desconhecido-128"),
    pytest.param("", 1, FetchTemporaryFailure("git_error"), id="saida-1-sem-stderr"),
]


@pytest.mark.parametrize(("stderr", "code", "expected"), STDERR_CASES)
def test_git_stderr_is_classified_without_leaking_its_text(
        stderr: str, code: int, expected: object, tmp_path: pathlib.Path, fake_git: _FakeGit,
        network_guard: Any, capfd: Any) -> None:
    """Classificação (o `stderr` em `LC_ALL=C` é lido só para classificar e nunca vai a log, evento nem
    API; um padrão por teste; o desconhecido cai em `git_error`, temporário, o lado seguro de
    não perder resposta), com as classes fechadas: cada padrão do `git fetch` →
    o desfecho exato (`FetchNotFound("commit" | "repository")`, `FetchRefused("redirect")`,
    `FetchTemporaryFailure("dns" | "connect" | "timeout" | "http_5xx" | "git_error")`); o texto do
    `stderr` (com a sentinela) não aparece no resultado, nos logs nem em fd 1 e 2 (controle positivo:
    a sentinela está no `stderr` do caso, quando há texto); nada publicado e nenhum staging sobra."""
    fake_git.fetch_stderr = stderr
    fake_git.fetch_code = code
    root = tmp_path / "repos"
    root.mkdir()
    capfd.readouterr()
    with capture_logs() as cap:
        result = _fetcher(root, _Resolver("8.8.8.8")).ensure(REPO, COMMIT)
    out, err = capfd.readouterr()
    assert result == expected
    assert type(result) is type(expected)
    assert len(fake_git.fetch_calls()) == 1
    rendered = "\n".join([repr(result), str(result), *map(str, dataclasses.astuple(result))])
    for surface, text in (("resultado", rendered), ("logs", cap.text), ("stdout", out), ("stderr", err)):
        assert STDERR_SENTINEL not in text, f"texto do stderr do git em {surface}"
        assert "unable to access" not in text, f"texto do stderr do git em {surface}"
        assert HOST not in text, f"host do stderr do git em {surface}"
    assert network_guard.attempts() == []
    _assert_nothing_published(root)


# ---------------------------------------------------------------------------------------------
# build_repo_fetcher em processo
# ---------------------------------------------------------------------------------------------


def _build(env: dict[str, str], settings: Settings | None = None) -> Any:
    from cq_server.__main__ import build_repo_fetcher

    return build_repo_fetcher(env, settings or _settings(60))


def _deny_write(monkeypatch: pytest.MonkeyPatch, root: pathlib.Path) -> None:
    """`os.access(…, W_OK)` nega a escrita em `root` e abaixo dela (determinístico no Windows e como
    root); o resto vai ao `os.access` real."""
    real_access = os.access
    denied = os.path.normcase(os.path.realpath(root))

    def fake_access(path: Any, mode: int, *args: Any, **kwargs: Any) -> bool:
        if not isinstance(path, int) and mode & os.W_OK:
            target = os.path.normcase(os.path.realpath(os.fspath(path)))
            if target == denied or target.startswith(denied + os.sep):
                return False
        return real_access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake_access)


DISABLED_ROOTS = ["ausente", "vazia", "arquivo", "inexistente", "sem-escrita"]


@pytest.mark.parametrize("kind", DISABLED_ROOTS)
def test_build_repo_fetcher_without_usable_root_returns_none_and_warns_once(
        kind: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, network_guard: Any,
        capfd: Any) -> None:
    """`build_repo_fetcher` devolve `None` com um único `worker.repos_disabled`
    sem `CQ_REPOS_ROOT`, ou com a raiz inexistente, que não é diretório ou sem escrita; as validações
    recebem então `FetchTemporaryFailure("repos_unavailable")`. Vazia vale como ausente: `None`, um
    único registro WARNING `worker.repos_disabled` no logger `cq_server.worker`, nada em fd 1 e 2 e
    nenhuma conexão. A lista de hosts válida vem junto (não liga nada sem a raiz)."""
    env = {"CQ_REPO_ALLOWED_HOSTS": HOST}
    if kind == "vazia":
        env["CQ_REPOS_ROOT"] = ""
    elif kind == "arquivo":
        target = tmp_path / "repos-arquivo"
        target.write_text("não é diretório", encoding="utf-8")
        env["CQ_REPOS_ROOT"] = str(target)
    elif kind == "inexistente":
        env["CQ_REPOS_ROOT"] = str(tmp_path / "repos-inexistente")
    elif kind == "sem-escrita":
        target = tmp_path / "repos-sem-escrita"
        target.mkdir()
        _deny_write(monkeypatch, target)
        env["CQ_REPOS_ROOT"] = str(target)
    capfd.readouterr()
    with capture_logs() as cap:
        fetcher = _build(env)
    assert fetcher is None
    assert len(cap.records) == 1, cap.records
    expected = {"level": "WARNING", "logger": "cq_server.worker", "event": "worker.repos_disabled"}
    assert expected.items() <= cap.records[0].items(), cap.records[0]
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []
    if kind == "inexistente":
        assert not (tmp_path / "repos-inexistente").exists(), "a raiz inexistente não é criada"


@pytest.mark.parametrize(("env", "message", "hidden"), [
    pytest.param({"CQ_REPOS_ROOT": "raiz-relativa-sentinela"}, MSG_ROOT, ("raiz-relativa-sentinela",),
                 id="raiz-relativa"),
    pytest.param({"CQ_REPOS_ROOT": "@ROOT@", "CQ_REPO_ALLOWED_HOSTS": "localhost"}, MSG_HOSTS, ("localhost",),
                 id="hosts-invalidos"),
    pytest.param({"CQ_REPOS_ROOT": "@ROOT@", "CQ_REPO_FETCH_TIMEOUT_SECONDS": "45.5"}, MSG_LEASE, ("45.5",),
                 id="prazo-acima-da-posse"),
])
def test_build_repo_fetcher_rejects_invalid_configuration(
        env: dict[str, str], message: str, hidden: tuple[str, ...], tmp_path: pathlib.Path,
        network_guard: Any, capfd: Any) -> None:
    """A configuração (`build_repo_fetcher` lê `RepoSettings.from_env(env)` e confere
    `check_fetch_timeout`; `ConfigError` com a mensagem exata, sem o valor; a saída 2 e o
    `config.invalid` são do processo, testados no subprocesso): raiz relativa, lista inválida e prazo
    que passa da posse de 60 s → `ConfigError`, sem registro, sem nada em fd 1 e 2 e sem conexão."""
    root = tmp_path / "repos"
    root.mkdir()
    env = {key: (str(root) if value == "@ROOT@" else value) for key, value in env.items()}
    capfd.readouterr()
    with capture_logs() as cap:
        _assert_config_error(lambda: _build(env), message, hidden=hidden)
    assert cap.records == []
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []


def test_build_repo_fetcher_configured_returns_git_fetcher_silently(
        tmp_path: pathlib.Path, network_guard: Any, capfd: Any) -> None:
    """Configurado → o `GitRepoFetcher`, sem registro (`__main__.build_repo_fetcher` não tem
    caminho para `GitTestOptions`): com a raiz absoluta, gravável e a lista, devolve o adaptador de
    produção, sem nenhum registro, nada em fd 1 e 2 e sem conexão; a raiz continua lá."""
    root = tmp_path / "repos"
    root.mkdir()
    env = {"CQ_REPOS_ROOT": str(root), "CQ_REPO_ALLOWED_HOSTS": HOST}
    capfd.readouterr()
    with capture_logs() as cap:
        fetcher = _build(env)
    assert type(fetcher) is GitRepoFetcher
    assert cap.records == []
    assert capfd.readouterr() == ("", "")
    assert network_guard.attempts() == []
    assert root.is_dir()


# ---------------------------------------------------------------------------------------------
# Os processos `cq-server worker` e `cq-server serve`
# ---------------------------------------------------------------------------------------------


def _json_records(out: str) -> list[dict]:
    """As linhas JSON da saída padrão do processo (tudo sai em JSON)."""
    records: list[dict] = []
    for line in out.splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _wait(predicate: Callable[[], Any], timeout: float = SUBPROCESS_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.1)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_worker_process_with_relative_repos_root_exits_2_with_config_invalid(
        db_url: str, bot_api: Any) -> None:
    """A ordem no worker (o `_cmd_worker` chama `build_repo_fetcher` depois da verificação do
    e-mail; `ConfigError` → `config.invalid` e código 2, como no e-mail): `python -m cq_server worker` com
    `CQ_REPOS_ROOT` relativo sai com código 2 sem subir o laço, com um registro `{level: ERROR, logger:
    cq_server.main, event: config.invalid, detail: <mensagem exata>}`; o `worker.mail_disabled`
    (sem `CQ_SMTP_HOST`) sai antes e é contado à parte; o valor da raiz não aparece em fd 1 e 2. Não se
    afirma a contagem total de registros."""
    proc = spawn_worker({"CQ_DATABASE_URL": db_url, "CQ_WORKER_ID": "worker-raiz-relativa",
                         "CQ_REPOS_ROOT": "raiz-relativa-sentinela"})
    try:
        _wait(lambda: proc.poll() is not None or '"worker.started"' in proc.output()[0])
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o cq-server worker não saiu com código 2 (saiu: {code}; registros: {events})"
    assert "worker.started" not in events, events
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    expected = {"level": "ERROR", "logger": "cq_server.main", "event": "config.invalid", "detail": MSG_ROOT}
    assert expected.items() <= invalid[0].items(), invalid[0]
    disabled = [r for r in records if r.get("event") == "worker.mail_disabled"]
    assert len(disabled) == 1, events
    assert events.index("worker.mail_disabled") < events.index("config.invalid"), events
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert "raiz-relativa-sentinela" not in text, f"valor da raiz na {surface}"


def test_serve_process_with_invalid_allowed_hosts_exits_2_before_migrating(
        empty_db_url: str, bot_api: Any) -> None:
    """A ordem no serve (o `_cmd_serve` lê `RepoSettings.from_env(os.environ)`
    **antes** do `_migrate`; `ConfigError` → `config.invalid` e código 2):
    `python -m cq_server serve` com `CQ_REPO_ALLOWED_HOSTS` inválido sai com código 2 sem subir o
    servidor (a porta nunca escuta, nenhum registro do uvicorn) e sem migrar (o banco vazio continua sem
    tabelas), com `{level: ERROR, logger: cq_server.main, event: config.invalid, detail: <mensagem
    exata>}`; o valor não aparece em fd 1 e 2. Não se afirma a contagem total de registros."""
    port = _free_port()
    env = worker_env({"CQ_DATABASE_URL": empty_db_url, "CQ_REPO_ALLOWED_HOSTS": "sentinela-sem-ponto"})
    proc = WorkerProcess([sys.executable, "-m", "cq_server", "serve", "--host", "127.0.0.1", "--port",
                          str(port)], env)
    listened = False
    try:
        def settled() -> bool:
            nonlocal listened
            if proc.poll() is not None:
                return True
            listened = listened or _listening(port)
            return listened

        _wait(settled)
        code = proc.poll()
    finally:
        proc.stop()
    out, err = proc.output()
    records = _json_records(out)
    events = [r.get("event") for r in records]
    assert code == 2, f"o cq-server serve não saiu com código 2 (saiu: {code}; escutou: {listened}; " \
                      f"registros: {events})"
    assert not listened
    assert [r for r in records if str(r.get("logger", "")).startswith("uvicorn")] == []
    invalid = [r for r in records if r.get("event") == "config.invalid"]
    assert len(invalid) == 1, events
    expected = {"level": "ERROR", "logger": "cq_server.main", "event": "config.invalid", "detail": MSG_HOSTS}
    assert expected.items() <= invalid[0].items(), invalid[0]
    for surface, text in (("saída padrão", out), ("saída de erro", err)):
        assert "sentinela-sem-ponto" not in text, f"valor da lista na {surface}"
    with psycopg.connect(empty_db_url) as conn:
        tables = conn.execute("SELECT count(*) FROM information_schema.tables "
                              "WHERE table_schema = 'public'").fetchone()[0]
    assert tables == 0, "o serve migrou antes de ler a configuração"
