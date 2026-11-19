"""Adaptador de obtenção de `repo@commit` pelo `git`: a porta de obtenção cumprida pelo cliente `git`,
com a política de endereços descrita abaixo.

`GitRepoFetcher` implementa `ports.repos.RepoFetcher` e é o **único** módulo que chama o `git` (por
`subprocess`). `ensure(repo, commit)`: `/repos/<repo_key>/<commit>/` já existe → `Fetched(reused=True)`
sem rede; senão revalida a URL e o host permitido, resolve e confere todos os endereços (só globais),
fixa o DNS, busca um único commit por SHA num `.staging/<uuid4>/` com ambiente mínimo (sem `CQ_*`), só
`https`, TLS verificado, sem prompt, credencial, configuração do sistema, hooks nem redirecionamento, e
tetos; extrai com `ls-tree` + `cat-file` (sem checkout), recusa caminho inválido, link que sai da árvore
e gitlink, e faz o `rename` atômico para o destino. `sweep_staging(now)`, na partida do worker, remove
os `.staging/<uuid4>` com mais de 1 hora.

`GitTestOptions` (a CA dos testes, loopback e porta) existe só para os testes de sistema;
`__main__.build_repo_fetcher` não tem caminho para ele.

Partes:
- o reuso antes de qualquer validação e a revalidação pela política do núcleo
  (`questionrefs.validate_repo`), com o código mapeado para `REFUSED_RULES`;
- a resolução pelo resolvedor injetado (a forma do `socket.getaddrinfo`, o padrão) e a conferência de
  **todos** os endereços: só `is_global`, sem multicast, sem as faixas IPv6 que embutem
  IPv4 (NAT64 e 6to4) e com o IPv4 mapeado conferido pelo endereço IPv4;
- o DNS fixado (`http.curloptResolve`), o ambiente mínimo construído do zero e a configuração por `-c`,
  o repositório descartável e o `git fetch` de um único commit;
- a classificação do código de saída e do `stderr` do `git`. O `stderr` é lido só
  para classificar: nunca vai a log, evento, API nem ao resultado;
- a extração: `cat-file -t` confere que o objeto é um commit, `ls-tree -r -z -l` lista
  a árvore, as conferências (caminho, gitlink, modos, tetos de arquivos e de bytes) vêm antes de escrever,
  os blobs saem de um único `cat-file --batch` lido em fluxo, e os links são conferidos por `realpath`
  depois de tudo escrito;
- a publicação atômica: modos explícitos 0644/0755 (sem depender do `umask`), `rename`
  de `.staging/<uuid4>/tree` para `<repo_key>/<commit>`, e `ENOTEMPTY`/`EEXIST` como `reused`;
- `sweep_staging`, que remove só os nomes no formato UUID com mais de `STAGING_MAX_AGE_SECONDS`.

Com `GitTestOptions` (só os testes de sistema): a CA própria (`http.sslCAInfo`), o loopback aceito na
conferência de endereços, a porta explícita na URL (validada sem ela pela mesma política, e usada na
resolução e no DNS fixado) e, no ambiente do `git`, um proxy inalcançável para todo host que não seja o
do `repo`.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cq_server.config import RepoSettings
from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure
from cq_server.questionrefs import COMMIT, REPO_URL, repo_key, validate_repo

# Idade mínima de um diretório de staging abandonado.
STAGING_MAX_AGE_SECONDS = 3600

# O diretório de trabalho dentro da raiz (nunca coincide com uma chave, que tem 64
# caracteres hexadecimais).
STAGING_DIR = ".staging"
HTTPS_PORT = 443
# Só os nomes de `str(uuid.uuid4())` são sobras de staging (nada mais é apagado).
_STAGING_NAME = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
# O proxy inalcançável do ambiente do `git` nos testes de sistema.
_TEST_DEAD_PROXY = "http://127.0.0.1:9"
# Só nos testes (`GitTestOptions.allow_port`): a URL com porta explícita.
_URL_WITH_PORT = re.compile(r"\Ahttps://(?P<host>[a-z0-9.-]+):(?P<port>[1-9][0-9]{0,4})(?P<path>/\S*)\Z")
# Modos aceitos na árvore (arquivo, executável, link) e o do gitlink.
_FILE_MODES = {b"100644": 0o644, b"100755": 0o755}
_LINK_MODE = b"120000"
_GITLINK_MODE = b"160000"
_MAX_NAME_BYTES = 255
_CHUNK = 1 << 20

FetchResult = Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure

# Faixas IPv6 que embutem um IPv4 e que o `is_global` do Python aceita.
_EMBEDDED_IPV4_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64 (RFC 6052)
    ipaddress.ip_network("64:ff9b:1::/48"),  # NAT64 local (RFC 8215)
    ipaddress.ip_network("2002::/16"),  # 6to4 (RFC 3056)
)

# A configuração de toda chamada do `git` (`-c chave=valor`).
_GIT_CONFIG = (
    "protocol.allow=never",
    "protocol.https.allow=always",
    "http.followRedirects=false",
    "http.sslVerify=true",
    "credential.helper=",
    "core.askPass=",
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "transfer.fsckObjects=true",
    "fetch.recurseSubmodules=false",
    "submodule.recurse=false",
    "maintenance.auto=false",
    "gc.auto=0",
    "fetch.writeCommitGraph=false",
    "http.lowSpeedLimit=1024",
    "http.lowSpeedTime=20",
)

# Os padrões do `stderr` do `git` em `LC_ALL=C`, na ordem em que são conferidos.
_SIGXFSZ = getattr(signal, "SIGXFSZ", 25)
_REDIRECT = re.compile(r"returned error: 30[1-8]\b")
_HTTP_5XX = re.compile(r"returned error: 5[0-9][0-9]\b")
_COMMIT_NOT_FOUND = ("not our ref", "couldn't find remote ref")
# `server verification failed` (libcurl 8 com GnuTLS; `server certificate verification failed` nas
# versões anteriores) é o texto do `git` do Debian, que usa o `libcurl3t64-gnutls`, para
# a falha que o OpenSSL chama de `SSL certificate problem` [F: `git fetch` com a CA errada na imagem do
# servidor: "server verification failed: certificate signer not trusted. (CAfile: … CRLfile: none)"].
_REPOSITORY_NOT_FOUND = re.compile(
    r"terminal prompts disabled|Authentication failed|repository '[^\n]*' not found|SSL certificate problem"
    r"|server (?:certificate )?verification failed")
_DNS_FAILURE = ("Could not resolve host",)
_CONNECT_FAILURE = ("Failed to connect", "Couldn't connect to server")
_TIMEOUT = re.compile(r"\bcurl 28\b|Operation timed out|Operation too slow")
_FILE_SIZE_EXCEEDED = re.compile(rf"died of signal {_SIGXFSZ}\b|File size limit exceeded")


@dataclass(frozen=True)
class GitTestOptions:
    """Opções só dos testes de sistema: CA própria, loopback e porta explícita permitidos."""

    ca_file: str | None
    allow_loopback: bool
    allow_port: bool


class _Timeout(Exception):
    """O prazo total da obtenção (`fetch_timeout_seconds`) acabou durante um processo `git`."""


# ---------------------------------------------------------------------------------------------
# Partes puras
# ---------------------------------------------------------------------------------------------


def refusal_for(repo: str, commit: str, allowed_hosts: frozenset[str]) -> FetchRefused | None:
    """A revalidação do adaptador: o código de `questionrefs.validate_repo`
    mapeado para `REFUSED_RULES` (`host_not_allowed` fica; `repo_syntax`, `repo_dot_segment` e
    `repo_length` viram `repo_syntax`) e o `commit` fora de `COMMIT` como `repo_syntax` [I]."""
    code = validate_repo(repo, allowed_hosts)
    if code == "host_not_allowed":
        return FetchRefused("host_not_allowed")
    if code is not None:
        return FetchRefused("repo_syntax")
    if not isinstance(commit, str) or COMMIT.fullmatch(commit) is None:
        return FetchRefused("repo_syntax")
    return None


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """O endereço de um `sockaddr[0]` (sem o `%escopo` do IPv6), ou `None` se não for um endereço."""
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def address_is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address, *,
                      allow_loopback: bool = False) -> bool:
    """Vale só `is_global and not is_multicast`, fora `64:ff9b::/96`,
    `64:ff9b:1::/48` e `2002::/16`; o IPv4 mapeado em IPv6 é conferido pelo próprio IPv4 (nem toda
    versão do Python 3.12 faz isso no `is_global`). `allow_loopback` só nos testes de sistema."""
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return address_is_public(mapped, allow_loopback=allow_loopback)
        if any(address in network for network in _EMBEDDED_IPV4_NETWORKS):
            return False
    if allow_loopback and address.is_loopback:
        return True
    return address.is_global and not address.is_multicast


def checked_addresses(infos: Sequence[Any], *, allow_loopback: bool = False) -> list[str] | None:
    """Os endereços distintos (em ordem) do resultado de um `getaddrinfo`, se **todos** forem públicos;
    `None` se algum não for (ou não for um endereço). Uma lista vazia volta vazia."""
    addresses: list[str] = []
    for info in infos:
        try:
            text = str(info[4][0])
        except (IndexError, TypeError):
            return None
        address = _ip(text)
        if address is None or not address_is_public(address, allow_loopback=allow_loopback):
            return None
        shown = str(address)
        if shown not in addresses:
            addresses.append(shown)
    return addresses


def curl_resolve_pin(host: str, port: int, addresses: Sequence[str]) -> str:
    """O valor de `http.curloptResolve`: `<host>:<porta>:<a1>,<a2>,…`, com o IPv6
    entre colchetes, para o curl usar os endereços conferidos sem resolver de novo."""
    shown = [f"[{a}]" if ":" in a else a for a in addresses]
    return f"{host}:{port}:{','.join(shown)}"


def git_environment(home: str) -> dict[str, str]:
    """O ambiente do `git`, construído do zero: nada de `os.environ` (nem `CQ_*`,
    proxies, `GIT_SSL_NO_VERIFY`, `GIT_TRACE*` ou `SSLKEYLOGFILE`)."""
    return {
        "PATH": "/usr/bin:/bin", "HOME": home, "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "GIT_ALLOW_PROTOCOL": "https", "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
    }


def git_config_args(*, pin: str | None = None, ca_file: str | None = None) -> list[str]:
    """Os `-c chave=valor` de toda chamada, mais o DNS fixado e a CA dos testes."""
    items = list(_GIT_CONFIG)
    if ca_file is not None:
        items.append(f"http.sslCAInfo={ca_file}")
    if pin is not None:
        items.append(f"http.curloptResolve={pin}")
    args: list[str] = []
    for item in items:
        args += ["-c", item]
    return args


def object_format(commit: str) -> str:
    """O formato do repositório descartável pelo tamanho do hash: 40 → `sha1`,
    64 → `sha256`."""
    return "sha256" if len(commit) == 64 else "sha1"


def classify_git_failure(returncode: int, stderr: bytes | str) -> FetchResult:
    """O desfecho de um `git fetch` que não saiu com 0, pela ordem: o sinal do
    teto de bytes, depois o `stderr` em `LC_ALL=C`; o desconhecido é `git_error` (temporário, o lado
    seguro: nenhuma resposta se perde). O texto do `stderr` não sai daqui."""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr or "")
    if returncode == -_SIGXFSZ or _FILE_SIZE_EXCEEDED.search(text):
        return FetchRefused("max_bytes")
    if _REDIRECT.search(text):
        return FetchRefused("redirect")
    if any(pattern in text for pattern in _COMMIT_NOT_FOUND):
        return FetchNotFound("commit")
    if _REPOSITORY_NOT_FOUND.search(text):
        return FetchNotFound("repository")
    if any(pattern in text for pattern in _DNS_FAILURE):
        return FetchTemporaryFailure("dns")
    if any(pattern in text for pattern in _CONNECT_FAILURE):
        return FetchTemporaryFailure("connect")
    if _TIMEOUT.search(text):
        return FetchTemporaryFailure("timeout")
    if _HTTP_5XX.search(text):
        return FetchTemporaryFailure("http_5xx")
    return FetchTemporaryFailure("git_error")


def split_test_port(repo: str) -> tuple[str, int] | None:
    """Só com `GitTestOptions.allow_port`: `https://<host>:<porta>/<caminho>` → (a URL sem a porta, que
    passa pela mesma política, e a porta); `None` se `repo` não tiver essa forma."""
    match = _URL_WITH_PORT.fullmatch(repo) if isinstance(repo, str) else None
    if match is None:
        return None
    port = int(match.group("port"))
    if not 0 < port < 65536:
        return None
    return f"https://{match.group('host')}{match.group('path')}", port


def path_is_valid(path: bytes) -> bool:
    """Cada componente do caminho, em bytes, não é vazio, `.`, `..` nem `.git` (em
    qualquer caixa), e tem no máximo 255 bytes."""
    for part in path.split(b"/"):
        if part in (b"", b".", b"..") or part.lower() == b".git" or len(part) > _MAX_NAME_BYTES:
            return False
        if b"\0" in part:
            return False
    return True


@dataclass(frozen=True)
class TreeEntry:
    """Uma entrada de `ls-tree -r -z -l` que vai para a árvore publicada (arquivo ou link)."""

    mode: bytes
    oid: bytes
    size: int
    path: bytes


def parse_ls_tree(raw: bytes, *, max_files: int, max_bytes: int) -> list[TreeEntry] | FetchRefused:
    """Os registros `modo tipo oid tamanho<TAB>caminho<NUL>` de `ls-tree -r -z -l`,
    conferidos **antes** de escrever qualquer coisa: gitlink (`160000`) → `submodule`;
    caminho inválido ou outro modo → `path_invalid`; mais de `max_files` entradas de arquivo e link →
    `max_files`; soma dos tamanhos acima de `max_bytes` → `max_bytes`."""
    entries: list[TreeEntry] = []
    total = 0
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, sep, path = record.partition(b"\t")
        fields = meta.split()
        if not sep or len(fields) != 4:
            return FetchRefused("path_invalid")
        mode, kind, oid, size_text = fields
        if mode == _GITLINK_MODE:
            return FetchRefused("submodule")
        if (mode not in _FILE_MODES and mode != _LINK_MODE) or kind != b"blob" or not size_text.isdigit():
            return FetchRefused("path_invalid")
        if not path_is_valid(path):
            return FetchRefused("path_invalid")
        entries.append(TreeEntry(mode, oid, int(size_text), path))
        if len(entries) > max_files:
            return FetchRefused("max_files")
        total += int(size_text)
    if total > max_bytes:
        return FetchRefused("max_bytes")
    return entries


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class _InvalidTree(Exception):
    """A árvore não pode ser escrita como veio (caminho repetido, link vazio): `path_invalid`."""


class _BatchFailure(Exception):
    """O `cat-file --batch` não entregou o que a listagem prometeu (objeto ausente ou tamanho diferente)."""


# ---------------------------------------------------------------------------------------------
# O adaptador
# ---------------------------------------------------------------------------------------------


class GitRepoFetcher:
    """Obtenção de `repo@commit` pelo `git` (ver o docstring do módulo)."""

    def __init__(self, settings: RepoSettings, *, git_binary: str = "git",
                 resolver: Callable[..., Any] | None = None,
                 test_options: GitTestOptions | None = None) -> None:
        self._settings = settings
        self._git_binary = git_binary
        self._resolver = resolver
        self._test_options = test_options

    def ensure(self, repo: str, commit: str,
               ) -> Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure:
        root = self._settings.repos_root
        if root is None:
            return FetchTemporaryFailure("repos_unavailable")
        # 1. O reuso, antes de qualquer validação, resolução ou rede. O `commit` só
        #    entra no caminho se tiver a forma de um hash (nada de `..` montando outro caminho).
        if (isinstance(repo, str) and isinstance(commit, str) and COMMIT.fullmatch(commit)
                and os.path.isdir(os.path.join(root, repo_key(repo), commit))):
            return Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
        # 2. A revalidação pela política do núcleo: nenhuma recusa cria nada na área. Só nos testes, a
        #    porta explícita sai da URL para a mesma política (e vai à resolução e ao DNS fixado).
        options = self._test_options
        validated, port = repo, HTTPS_PORT
        if options is not None and options.allow_port:
            split = split_test_port(repo)
            if split is not None:
                validated, port = split
        refused = refusal_for(validated, commit, self._settings.allowed_hosts)
        if refused is not None:
            return refused
        match = REPO_URL.fullmatch(validated)
        assert match is not None  # garantido pela revalidação
        host = match.group("host")
        # 3. Resolver e conferir todos os endereços.
        allow_loopback = bool(options is not None and options.allow_loopback)
        resolver = self._resolver if self._resolver is not None else socket.getaddrinfo
        try:
            infos = resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return FetchTemporaryFailure("dns")
        addresses = checked_addresses(list(infos or ()), allow_loopback=allow_loopback)
        if addresses is None:
            return FetchRefused("address_not_global")
        if not addresses:
            return FetchTemporaryFailure("dns")
        pin = curl_resolve_pin(host, port, addresses)
        # 4. O repositório descartável e a busca, com o prazo total.
        deadline = time.monotonic() + float(self._settings.fetch_timeout_seconds)
        staging_root = os.path.join(root, STAGING_DIR)
        staging = os.path.join(staging_root, str(uuid.uuid4()))
        try:
            os.makedirs(staging_root, mode=0o700, exist_ok=True)
            os.mkdir(staging, 0o700)
            git_dir = os.path.join(staging, "git")
            home = os.path.join(staging, "home")
            os.mkdir(home, 0o700)
            env = git_environment(home)
            if options is not None:
                # Nos testes, todo host fora do `repo` cai num proxy morto.
                for name in ("https_proxy", "HTTPS_PROXY"):
                    env[name] = _TEST_DEAD_PROXY
                for name in ("no_proxy", "NO_PROXY"):
                    env[name] = host
            failed = self._fetch(repo, commit, git_dir=git_dir, env=env, pin=pin, deadline=deadline)
            if failed is not None:
                return failed
            return self._extract(repo, commit, root=root, git_dir=git_dir, env=env, deadline=deadline,
                                 staging=staging)
        except _Timeout:
            return FetchTemporaryFailure("timeout")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def sweep_staging(self, now: datetime) -> int:
        """Na partida do worker, remove os `.staging/<uuid4>/` com mais de
        `STAGING_MAX_AGE_SECONDS` (sobras de um worker morto no meio de uma obtenção). Só nomes no
        formato UUID e só diretórios de verdade (nunca segue link). Devolve quantos removeu."""
        root = self._settings.repos_root
        if root is None:
            return 0
        staging_root = os.path.join(root, STAGING_DIR)
        try:
            names = os.listdir(staging_root)
        except OSError:
            return 0
        limit = now.timestamp() - STAGING_MAX_AGE_SECONDS
        removed = 0
        for name in names:
            if _STAGING_NAME.fullmatch(name) is None:
                continue
            path = os.path.join(staging_root, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= limit:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.lexists(path):
                removed += 1
        return removed

    # --- o processo git --------------------------------------------------------------------------

    def _config(self, *, pin: str | None = None) -> list[str]:
        options = self._test_options
        return git_config_args(pin=pin, ca_file=options.ca_file if options is not None else None)

    def _fetch(self, repo: str, commit: str, *, git_dir: str, env: dict[str, str], pin: str,
               deadline: float) -> FetchResult | None:
        """`git init --bare` e `git fetch` de um único commit; `None` quando
        o commit chegou."""
        code, _out, err = self._git(
            [*self._config(), "init", "--bare", "--quiet", "--template=",
             f"--object-format={object_format(commit)}", git_dir],
            env=env, deadline=deadline)
        if code != 0:
            return classify_git_failure(code, err)
        code, _out, err = self._git(
            [f"--git-dir={git_dir}", *self._config(pin=pin), "fetch", "--quiet",
             "--depth=1", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head", repo, commit],
            env=env, deadline=deadline)
        if code != 0:
            return classify_git_failure(code, err)
        return None

    def _extract(self, repo: str, commit: str, *, root: str, git_dir: str, env: dict[str, str],
                 deadline: float, staging: str) -> FetchResult:
        """Extração com `ls-tree` + `cat-file`, sem checkout, e publicação
        atômica."""
        base = [f"--git-dir={git_dir}", *self._config()]
        # O objeto pedido tem de ser um commit.
        code, out, _err = self._git([*base, "cat-file", "-t", commit], env=env, deadline=deadline)
        if code != 0 or out.strip() != b"commit":
            return FetchNotFound("commit")
        code, out, _err = self._git([*base, "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}"],
                                    env=env, deadline=deadline)
        tree_oid = out.decode("ascii", "replace").strip()
        if code != 0 or COMMIT.fullmatch(tree_oid) is None:
            return FetchTemporaryFailure("git_error")
        code, out, _err = self._git([*base, "ls-tree", "-r", "-z", "-l", "--full-tree", tree_oid],
                                    env=env, deadline=deadline)
        if code != 0:
            return FetchTemporaryFailure("git_error")
        entries = parse_ls_tree(out, max_files=int(self._settings.max_files),
                                max_bytes=int(self._settings.max_bytes))
        if isinstance(entries, FetchRefused):
            return entries
        tree = os.path.join(staging, "tree")
        os.mkdir(tree, 0o700)
        try:
            links = self._write_tree(os.fsencode(tree), entries, base=base, env=env, deadline=deadline)
        except _InvalidTree:
            return FetchRefused("path_invalid")
        except _BatchFailure:
            return FetchTemporaryFailure("git_error")
        # Os links, depois de tudo escrito: o alvo absoluto e, pelo `realpath`, o
        # que sai da árvore (pega as cadeias de links, que a conferência lexical deixa passar).
        if any(target.startswith(b"/") for _path, target in links):
            return FetchRefused("symlink_absolute")
        real_tree = os.path.realpath(tree)
        for path, _target in links:
            if not _within(os.path.realpath(os.fsdecode(path)), real_tree):
                return FetchRefused("symlink_escape")
        # Publicação atômica: modos explícitos, `rename` e a corrida como reuso.
        os.chmod(tree, 0o755)
        key_dir = os.path.join(root, repo_key(repo))
        os.makedirs(key_dir, exist_ok=True)
        try:
            os.chmod(key_dir, 0o755)
        except OSError:
            pass
        final = os.path.join(key_dir, commit)
        try:
            os.rename(tree, final)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)
            raise
        return Fetched(tree_oid=tree_oid, file_count=len(entries), byte_count=sum(e.size for e in entries),
                       reused=False)

    def _write_tree(self, tree: bytes, entries: Sequence[TreeEntry], *, base: list[str], env: dict[str, str],
                    deadline: float) -> list[tuple[bytes, bytes]]:
        """Escreve cada entrada em `tree` com os bytes do blob (um único `cat-file --batch`, lido em
        fluxo): arquivos 0644/0755 com `O_EXCL|O_NOFOLLOW`, diretórios 0755 e links com o alvo do blob,
        sempre com o modo fixado depois de criar (sem depender do `umask`). Devolve os links escritos,
        `(caminho, alvo)`, para a conferência depois da escrita."""
        created: set[bytes] = set()
        links: list[tuple[bytes, bytes]] = []
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))

        def parent_dirs(path: bytes) -> None:
            parts = path.split(b"/")[:-1]
            for index in range(1, len(parts) + 1):
                directory = b"/".join(parts[:index])
                if directory in created:
                    continue
                full = os.path.join(tree, directory)
                try:
                    os.mkdir(full, 0o700)
                except FileExistsError:
                    raise _InvalidTree from None
                os.chmod(full, 0o755)
                created.add(directory)

        def write(entry: TreeEntry, chunks: Callable[[], bytes | None]) -> None:
            parent_dirs(entry.path)
            full = os.path.join(tree, entry.path)
            if entry.mode == _LINK_MODE:
                target = b""
                while (chunk := chunks()) is not None:
                    target += chunk
                if not target or b"\0" in target:
                    raise _InvalidTree
                try:
                    os.symlink(target, full)
                except FileExistsError:
                    raise _InvalidTree from None
                links.append((full, target))
                return
            try:
                fd = os.open(full, flags, 0o600)
            except FileExistsError:
                raise _InvalidTree from None
            try:
                while (chunk := chunks()) is not None:
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(fd, view):]
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, _FILE_MODES[entry.mode])
            finally:
                os.close(fd)
            if not hasattr(os, "fchmod"):
                os.chmod(full, _FILE_MODES[entry.mode])

        self._cat_file_batch(entries, write, base=base, env=env, deadline=deadline)
        return links

    def _cat_file_batch(self, entries: Sequence[TreeEntry], write: Callable[..., None], *, base: list[str],
                        env: dict[str, str], deadline: float) -> None:
        """Um único `git cat-file --batch`: pede os blobs na ordem da listagem e entrega o conteúdo de
        cada um a `write` em pedaços, conferindo o tipo e o tamanho contra a listagem. O prazo total mata
        o grupo de processos (`_Timeout`)."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Timeout
        kwargs: dict[str, Any] = {"env": env, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.DEVNULL}
        if os.name == "posix":
            kwargs["process_group"] = 0
        proc = subprocess.Popen(self._command([*base, "cat-file", "--batch"]), **kwargs)
        expired = threading.Event()

        def on_deadline() -> None:
            expired.set()
            self._kill(proc)

        timer = threading.Timer(remaining, on_deadline)
        timer.daemon = True
        timer.start()

        def feed() -> None:
            try:
                for entry in entries:
                    proc.stdin.write(entry.oid + b"\n")
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        writer = threading.Thread(target=feed, name="cq-git-cat-file", daemon=True)
        writer.start()
        stdout = proc.stdout
        try:
            for entry in entries:
                fields = stdout.readline().split()
                if (len(fields) != 3 or fields[0] != entry.oid or fields[1] != b"blob"
                        or not fields[2].isdigit() or int(fields[2]) != entry.size):
                    raise _BatchFailure
                left = entry.size

                def chunks() -> bytes | None:
                    nonlocal left
                    if left == 0:
                        return None
                    data = stdout.read(min(left, _CHUNK))
                    if not data:
                        raise _BatchFailure
                    left -= len(data)
                    return data

                write(entry, chunks)
                if left or stdout.read(1) != b"\n":
                    raise _BatchFailure
        except (_BatchFailure, OSError, ValueError):
            if expired.is_set():
                raise _Timeout from None
            raise
        finally:
            timer.cancel()
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()) + 1)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                proc.wait()
            writer.join(1)
            stdout.close()
        if expired.is_set():
            raise _Timeout
        if proc.returncode != 0:
            raise _BatchFailure

    def _command(self, args: Sequence[str]) -> list[str]:
        """O `argv` do `git`, sob o teto de bytes (`prlimit --fsize`) no Linux."""
        argv = [self._git_binary, *args]
        if sys.platform.startswith("linux"):
            argv = ["prlimit", f"--fsize={int(self._settings.max_bytes)}", "--", *argv]
        return argv

    def _git(self, args: Sequence[str], *, env: dict[str, str], deadline: float,
             ) -> tuple[int, bytes, bytes]:
        """Roda o `git` com o ambiente mínimo e o prazo restante; o grupo de processos inteiro morre no
        fim do prazo (`_Timeout`). Devolve o código de saída, a saída e o `stderr` (só para classificar)."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Timeout
        kwargs: dict[str, Any] = {"env": env, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                                  "stderr": subprocess.PIPE}
        if os.name == "posix":
            kwargs["process_group"] = 0
        proc = subprocess.Popen(self._command(args), **kwargs)
        try:
            out, err = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            proc.communicate()
            raise _Timeout from None
        except BaseException:
            self._kill(proc)
            proc.communicate()
            raise
        return int(proc.returncode), out or b"", err or b""

    @staticmethod
    def _kill(proc: Any) -> None:
        """Mata o grupo de processos do `git` (o `git-remote-https` e o `index-pack` inclusive)."""
        killpg = getattr(os, "killpg", None)
        if killpg is not None and os.name == "posix":
            try:
                killpg(proc.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.kill()
        except OSError:
            pass
