"""Roteiro do teste de sistema do adaptador git (os casos do adaptador:
gitlink → `FetchRefused("submodule")`, sem submódulos; prazo esgotado →
`FetchTemporaryFailure("timeout")`, que o núcleo rejeita como `repository_refused` na última tentativa,
pelos tetos padrão da obtenção).

Roda **dentro** de um container da imagem do servidor com `--network none` (nada sai do loopback por
construção), montado como somente leitura, só com a biblioteca padrão, o `cq_server` do `.venv` da imagem
e os programas da imagem (`git`, `git-http-backend`, `openssl`, `setpriv`). Nunca fala com a internet.

Modos:
- `main` (como root): confere o ambiente da imagem; gera na hora uma CA, o certificado de
  `repos.cq.test` (e `outro.cq.test`) e uma CA errada; cria os repositórios de teste; sobe o
  `git http-backend` atrás de um servidor **HTTPS** em 127.0.0.1 (com um caminho que redireciona com 301
  para `outro.cq.test` e um que nunca responde); roda cada caso num processo `run` com o uid 999 do
  worker (`setpriv`), medindo os pedidos que o servidor recebeu; confere a leitura da árvore pelo uid
  65534 do motor e os processos `git` que sobraram; imprime **uma** linha JSON com o relatório bruto
  (as asserções ficam no pytest, `tests/system/test_git_fetcher.py`).
- `run <config JSON>` (uid 999, `umask 077`): monta o `GitRepoFetcher` (com
  `GitTestOptions(ca_file, allow_loopback=True, allow_port=True)`, ou sem ele no caso de produção) e um
  resolvedor que mapeia `repos.cq.test` e `outro.cq.test` para 127.0.0.1, chama `ensure` (uma vez, ou
  duas em paralelo com um vigia da árvore final) e imprime o desfecho e a árvore publicada.

O processo `run` recebe no ambiente valores com a marca `sentinela` (`CQ_*`, `GIT_SSL_NO_VERIFY`,
`GIT_TRACE`, `SSLKEYLOGFILE`, um `https_proxy`) que nunca podem chegar ao processo `git` (o
ambiente do git é construído do zero).

Configuração hostil (a regra: sem ajudante de credenciais e sem as configurações de git do sistema e do
usuário, sem submódulos, LFS nem hooks, cada regra com controle positivo): depois dos
casos comuns, o `main` planta os arquivos **padrão** de configuração do git nos dois níveis, o
`/etc/gitconfig` do sistema e o `~/.gitconfig` de um HOME do worker (`home-hostil`), com
- um `credential.helper` em cada nível que grava um marcador (o do usuário devolve usuário e senha);
- um `url.https://outro.cq.test:<porta>/.insteadOf` para o host do teste (e o `http.curloptResolve`
  que leva `outro.cq.test` ao mesmo servidor, para que um git que honre a troca chegue lá de fato);
- um `core.hooksPath` com hooks que gravam marcador e um `core.fsmonitor` com script que grava marcador;
- os `filter.lfs.smudge`/`clean` que gravam marcador (o repositório `atributos` tem `filter=lfs`).
Com isso, roda dois casos do `ensure` com o HOME hostil: `hostile_config` (o `atributos`, que tem de dar
certo sem marcador nem pedido a `outro.cq.test`) e `hostile_config_auth` (o caminho `/protegido/`, que
responde 401 sem `Authorization`: o ajudante não pode rodar, e nenhum pedido leva credencial). O
controle positivo (`hostile_control`) roda um `git` puro no mesmo HOME e com o mesmo `/etc/gitconfig`
(uid 999): um `clone` do `atributos` (a troca leva os pedidos a `outro.cq.test`; hooks e filtro rodam),
um `status` no clone (o fsmonitor roda) e um `clone` do `/protegido/` (os dois ajudantes rodam e o
pedido leva `Authorization`). O `/etc/gitconfig` original volta no fim.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
import traceback

SCRIPT = os.path.abspath(__file__)
WORK = "/work"
SRV = f"{WORK}/srv"
TLS = f"{WORK}/tls"
ROOTS = f"{WORK}/roots"
LOG = f"{WORK}/log"
BIN = f"{WORK}/bin"
BUILD = f"{WORK}/build"
HOST = "repos.cq.test"
OTHER_HOST = "outro.cq.test"
WORKER_UID = 999
ENGINE_UID = 65534
GIT_ENV_LOG = f"{LOG}/git-env.log"
RECORDING_GIT = f"{BIN}/git-registra"
DEFAULT_TIMEOUT = 30.0
DEADLINE_TIMEOUT = 5.0
DEFAULT_MAX_BYTES = 104_857_600
DEFAULT_MAX_FILES = 20_000
MISSING_COMMIT = "f" * 40
# O ambiente do processo `run`: tudo o que tem `sentinela` não pode chegar ao git.
RUNNER_ENV = {
    "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin",
    "HOME": f"{WORK}/home-worker",
    "LC_ALL": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "CQ_DATABASE_URL": "postgresql://cq:senha-sentinela-git@postgres:5432/cq",
    "CQ_MASTER_KEY": "chave-sentinela-git",
    "CQ_SMTP_PASSWORD": "smtp-sentinela-git",
    "GIT_SSL_NO_VERIFY": "1-sentinela",
    "GIT_TRACE": "1",
    "GIT_CURL_VERBOSE": "1",
    "SSLKEYLOGFILE": f"{LOG}/chaves-sentinela.log",
    "https_proxy": "http://proxy-sentinela.invalid:3128",
    "HTTPS_PROXY": "http://proxy-sentinela.invalid:3128",
}
LEAK_KEYS = ("GIT_SSL_NO_VERIFY", "GIT_TRACE", "GIT_CURL_VERBOSE", "SSLKEYLOGFILE")
# Configuração hostil nos arquivos padrão do git.
HOSTILE = f"{WORK}/hostil"
HOSTILE_HOME = f"{WORK}/home-hostil"
MARKS = f"{HOSTILE}/marcas"
SYSTEM_GITCONFIG = "/etc/gitconfig"
PROTECTED_PREFIX = "/protegido"
HOOK_NAMES = ("reference-transaction", "post-checkout", "post-merge", "post-index-change", "post-rewrite",
              "pre-auto-gc", "pre-commit", "post-commit", "push-to-checkout", "fsmonitor-watchman")
# O ambiente do `run` nos casos hostis: o mesmo, com o HOME que tem o `.gitconfig` hostil.
HOSTILE_RUNNER_ENV = {**RUNNER_ENV, "HOME": HOSTILE_HOME}
# O ambiente do `git` puro do controle positivo: o mesmo HOME, sem os valores `sentinela` (o proxy morto
# impediria o controle de chegar ao servidor) e sem terminal.
CONTROL_ENV = {"PATH": "/usr/bin:/bin", "HOME": HOSTILE_HOME, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
# Um ponteiro do Git LFS: com `filter=lfs`, os bytes publicados têm de ser os do ponteiro (o blob).
LFS_POINTER = ("version https://git-lfs.github.com/spec/v1\n"
               "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
               "size 12345\n")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------------------------
# Comum
# ---------------------------------------------------------------------------------------------


def repo_key(repo: str) -> str:
    return hashlib.sha256(repo.encode("utf-8")).hexdigest()


def snapshot(path: str) -> dict | None:
    """A árvore em `path` sem seguir links: `{caminho: {type, mode|sha256|target}}` (`.` é a raiz)."""
    if not os.path.lexists(path):
        return None
    root_stat = os.lstat(path)
    if not stat.S_ISDIR(root_stat.st_mode):
        return {".": {"type": "other"}}
    out: dict[str, dict] = {".": {"type": "dir", "mode": oct(stat.S_IMODE(root_stat.st_mode))}}
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = {"type": "link", "target": os.readlink(full)}
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = {"type": "dir", "mode": oct(stat.S_IMODE(st.st_mode))}
            elif stat.S_ISREG(st.st_mode):
                with open(full, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
                out[rel] = {"type": "file", "mode": oct(stat.S_IMODE(st.st_mode)), "sha256": digest}
            else:
                out[rel] = {"type": "other"}
    return out


def leaf_paths(path: str) -> list[str]:
    """Os arquivos e links de `path` (sem os diretórios), relativos e ordenados."""
    found = []
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [*filenames, *[d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]]:
            found.append(os.path.relpath(os.path.join(dirpath, name), path).replace(os.sep, "/"))
    return sorted(found)


def listing(path: str) -> list[str] | None:
    return sorted(os.listdir(path)) if os.path.isdir(path) else None


# ---------------------------------------------------------------------------------------------
# Modo `run` (uid 999)
# ---------------------------------------------------------------------------------------------


def _resolver(host: object, port: object, *args: object, **kwargs: object) -> list[tuple]:
    """A forma do `socket.getaddrinfo`: `repos.cq.test` e `outro.cq.test` → 127.0.0.1; o resto falha."""
    name = host.decode("ascii") if isinstance(host, bytes) else str(host)
    if name in (HOST, OTHER_HOST):
        number = port if isinstance(port, int) else 443
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", number))]
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _describe(result: object) -> dict:
    from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure

    if type(result) is Fetched:
        return {"type": "Fetched", "tree_oid": result.tree_oid, "file_count": result.file_count,
                "byte_count": result.byte_count, "reused": result.reused}
    if type(result) is FetchRefused:
        return {"type": "FetchRefused", "rule": result.rule}
    if type(result) is FetchNotFound:
        return {"type": "FetchNotFound", "what": result.what}
    if type(result) is FetchTemporaryFailure:
        return {"type": "FetchTemporaryFailure", "error_type": result.error_type}
    return {"type": "unexpected", "name": type(result).__name__}


def run_case(cfg: dict) -> dict:
    os.umask(0o077)  # os modos da árvore não podem depender do umask
    from cq_server.adapters.git import GitRepoFetcher, GitTestOptions
    from cq_server.config import RepoSettings

    settings = RepoSettings(allowed_hosts=frozenset({HOST}), repos_root=cfg["root"],
                            fetch_timeout_seconds=cfg["timeout"], max_bytes=cfg["max_bytes"],
                            max_files=cfg["max_files"])
    kwargs: dict = {"resolver": _resolver}
    if cfg.get("git_binary"):
        kwargs["git_binary"] = cfg["git_binary"]
    if not cfg.get("production"):
        kwargs["test_options"] = GitTestOptions(ca_file=cfg["ca_file"], allow_loopback=True, allow_port=True)

    def call() -> dict:
        try:
            return _describe(GitRepoFetcher(settings, **kwargs).ensure(cfg["repo"], cfg["commit"]))
        except BaseException as exc:  # noqa: BLE001 - o relatório leva só o tipo
            return {"type": "exception", "name": type(exc).__name__}

    key_dir = os.path.join(cfg["root"], repo_key(cfg["repo"]))
    final = os.path.join(key_dir, cfg["commit"])
    watch = {"observations": 0, "partial": [], "foreign_names": []}
    expected_paths = cfg.get("expected_paths")
    done = threading.Event()

    def watcher() -> None:
        while not done.is_set():
            names = listing(key_dir)
            if names is not None:
                foreign = sorted(set(names) - {cfg["commit"]})
                if foreign and foreign not in watch["foreign_names"]:
                    watch["foreign_names"].append(foreign)
            if os.path.isdir(final):
                try:
                    seen = leaf_paths(final)
                except OSError:
                    seen = ["<erro ao listar>"]
                watch["observations"] += 1
                if expected_paths is not None and seen != expected_paths:
                    watch["partial"].append(len(seen))
            time.sleep(0.001)

    watch_thread = threading.Thread(target=watcher, name="cq-fetch-watch", daemon=True)
    watch_thread.start()
    start = time.monotonic()
    if cfg.get("concurrent"):
        barrier = threading.Barrier(2)
        outcomes: list[dict | None] = [None, None]

        def worker(index: int) -> None:
            barrier.wait()
            outcomes[index] = call()

        threads = [threading.Thread(target=worker, args=(i,), name=f"cq-fetch-{i}") for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    else:
        outcomes = [call()]
    elapsed = time.monotonic() - start
    time.sleep(0.05)  # o vigia olha ao menos mais uma vez depois do fim das chamadas
    done.set()
    watch_thread.join(5)
    return {"outcomes": outcomes, "elapsed": elapsed, "tree": snapshot(final), "key_dir": listing(key_dir),
            "staging": listing(os.path.join(cfg["root"], ".staging")), "watch": watch}


# ---------------------------------------------------------------------------------------------
# Modo `main` (root): ambiente, certificados, repositórios e servidor
# ---------------------------------------------------------------------------------------------


def environment() -> dict:
    git = shutil.which("git")
    backend = None
    if git:
        exec_path = subprocess.run([git, "--exec-path"], capture_output=True, text=True).stdout.strip()
        candidate = os.path.join(exec_path, "git-http-backend")
        backend = candidate if os.path.isfile(candidate) else None
    repos = None
    if os.path.lexists("/repos"):
        st = os.lstat("/repos")
        repos = {"is_dir": stat.S_ISDIR(st.st_mode), "uid": st.st_uid, "gid": st.st_gid,
                 "mode": oct(stat.S_IMODE(st.st_mode))}
    return {"git": git, "http_backend": backend, "openssl": shutil.which("openssl"),
            "setpriv": shutil.which("setpriv"), "prlimit": shutil.which("prlimit"), "repos": repos,
            "python": sys.version.split()[0], "uid": os.getuid()}


def _run(args: list[str], *, cwd: str | None = None, env: dict | None = None, data: bytes | None = None) -> bytes:
    proc = subprocess.run(args, cwd=cwd, env=env, input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{args[:3]} falhou ({proc.returncode}): {proc.stderr.decode(errors='replace')[-800:]}")
    return proc.stdout


def make_tls() -> dict:
    os.makedirs(TLS, mode=0o755, exist_ok=True)
    ossl = ["openssl"]
    for name in ("ca", "ca-errada"):
        _run([*ossl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{TLS}/{name}.key", "-out",
              f"{TLS}/{name}.pem", "-days", "2", "-subj", f"/CN=cq-test-fetch {name}", "-addext",
              "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"], cwd=TLS)
    with open(f"{TLS}/servidor.ext", "w", encoding="ascii") as fh:
        fh.write(f"subjectAltName=DNS:{HOST},DNS:{OTHER_HOST}\nbasicConstraints=CA:FALSE\n"
                 "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n")
    _run([*ossl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{TLS}/servidor.key", "-out",
          f"{TLS}/servidor.csr", "-subj", f"/CN={HOST}"], cwd=TLS)
    _run([*ossl, "x509", "-req", "-in", f"{TLS}/servidor.csr", "-CA", f"{TLS}/ca.pem", "-CAkey", f"{TLS}/ca.key",
          "-CAcreateserial", "-out", f"{TLS}/servidor.pem", "-days", "2", "-extfile", f"{TLS}/servidor.ext"],
         cwd=TLS)
    for name in ("ca.pem", "ca-errada.pem"):
        os.chmod(f"{TLS}/{name}", 0o644)
    return {"ca": f"{TLS}/ca.pem", "wrong_ca": f"{TLS}/ca-errada.pem", "cert": f"{TLS}/servidor.pem",
            "key": f"{TLS}/servidor.key"}


GIT_BUILD_ENV = {
    "PATH": "/usr/bin:/bin", "HOME": f"{WORK}/home-build", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "cq teste", "GIT_AUTHOR_EMAIL": "teste@cq.test", "GIT_COMMITTER_NAME": "cq teste",
    "GIT_COMMITTER_EMAIL": "teste@cq.test", "GIT_AUTHOR_DATE": "2026-09-01T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-09-01T12:00:00+00:00",
}


def _git(*args: str, cwd: str) -> bytes:
    return _run(["git", "-c", "core.autocrlf=false", "-c", "core.symlinks=true", *args], cwd=cwd,
                env=GIT_BUILD_ENV)


def _write(base: str, rel: str, content: bytes | str, mode: int = 0o644) -> None:
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content.encode("utf-8") if isinstance(content, str) else content)
    os.chmod(path, mode)


def _new_repo(name: str, *, sha256: bool = False) -> str:
    work = os.path.join(BUILD, name)
    os.makedirs(work)
    args = ["init", "-q", "-b", "main"]
    if sha256:
        args.append("--object-format=sha256")
    _git(*args, work, cwd=BUILD)
    return work


def _commit(work: str, message: str) -> str:
    _git("add", "-A", cwd=work)
    _git("commit", "-q", "--allow-empty", "-m", message, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).decode().strip()


def _publish(work: str, name: str) -> str:
    bare = os.path.join(SRV, f"{name}.git")
    _git("clone", "-q", "--bare", "--no-local", work, bare, cwd=BUILD)
    return bare


def expected_tree(bare: str, commit: str) -> dict:
    """A árvore que o `ensure` tem de escrever, tirada do próprio git: os bytes de cada blob (sem
    `.gitattributes`, sem checkout), 0644/0755 pelos modos do git e os diretórios 0755; e as contagens
    das entradas `100644`/`100755`/`120000`."""
    tree: dict[str, dict] = {".": {"type": "dir", "mode": "0o755"}}
    files = 0
    total = 0
    raw = _git("ls-tree", "-r", "-z", "-l", "--full-tree", commit, cwd=bare)
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, path_bytes = record.split(b"\t", 1)
        mode, _kind, oid, size = meta.split()
        path = path_bytes.decode("utf-8")
        parts = path.split("/")
        for i in range(1, len(parts)):
            tree["/".join(parts[:i])] = {"type": "dir", "mode": "0o755"}
        if mode not in (b"100644", b"100755", b"120000"):
            continue
        blob = _git("cat-file", "blob", oid.decode(), cwd=bare)
        files += 1
        total += int(size)
        if mode == b"120000":
            tree[path] = {"type": "link", "target": blob.decode("utf-8")}
        else:
            tree[path] = {"type": "file", "mode": "0o755" if mode == b"100755" else "0o644",
                          "sha256": hashlib.sha256(blob).hexdigest()}
    tree_oid = _git("rev-parse", f"{commit}^{{tree}}", cwd=bare).decode().strip()
    return {"tree": tree, "tree_oid": tree_oid, "file_count": files, "byte_count": total}


def make_repos() -> dict:
    """Os repositórios servidos em `SRV/<nome>.git`; devolve `{nome: {bare, commits...}}`."""
    for path in (SRV, BUILD, GIT_BUILD_ENV["HOME"]):
        os.makedirs(path, mode=0o755, exist_ok=True)
    repos: dict[str, dict] = {}

    work = _new_repo("curso")
    _write(work, "infoCourse.json", '{"uuid": "5f0b1d7e-0000-4000-8000-000000000001", "name": "CQ"}\n')
    _write(work, "questions/q1/info.json", '{"title": "Soma", "type": "v3"}\n')
    _write(work, "questions/q1/question.html", "<pl-question-panel>1 + 1?</pl-question-panel>\n")
    _write(work, "scripts/run.sh", "#!/bin/sh\necho ok\n", 0o755)
    _write(work, "README.md", "primeira versão\n")
    first = _commit(work, "primeira")
    _write(work, "README.md", "segunda versão\n")
    _write(work, "questions/q2/info.json", '{"title": "Produto", "type": "v3"}\n')
    tip = _commit(work, "segunda")
    repos["curso"] = {"bare": _publish(work, "curso"), "first": first, "tip": tip}

    work = _new_repo("curso256", sha256=True)
    _write(work, "infoCourse.json", '{"name": "CQ sha256"}\n')
    _write(work, "questions/q1/info.json", '{"title": "Sha256"}\n')
    repos["curso256"] = {"commit": _commit(work, "sha256")}
    repos["curso256"]["bare"] = _publish(work, "curso256")

    work = _new_repo("link-absoluto")
    _write(work, "questions/q1/info.json", "{}\n")
    os.symlink("/etc/passwd", os.path.join(work, "questions/q1/senhas"))
    repos["link-absoluto"] = {"commit": _commit(work, "link absoluto")}
    repos["link-absoluto"]["bare"] = _publish(work, "link-absoluto")

    work = _new_repo("link-cadeia")
    _write(work, "questions/q1/info.json", "{}\n")
    os.makedirs(os.path.join(work, "a"))
    os.symlink("..", os.path.join(work, "a/l1"))  # lexicalmente interno: aponta para a raiz
    os.symlink("a/l1/..", os.path.join(work, "l2"))  # também interno à letra, mas sai da árvore
    repos["link-cadeia"] = {"commit": _commit(work, "link em cadeia")}
    repos["link-cadeia"]["bare"] = _publish(work, "link-cadeia")

    work = _new_repo("gitlink")
    _write(work, "questions/q1/info.json", "{}\n")
    _git("add", "-A", cwd=work)
    _git("update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},sub", cwd=work)
    _git("commit", "-q", "-m", "gitlink", cwd=work)
    repos["gitlink"] = {"commit": _git("rev-parse", "HEAD", cwd=work).decode().strip()}
    repos["gitlink"]["bare"] = _publish(work, "gitlink")

    work = _new_repo("atributos")
    _write(work, ".gitattributes", "* text eol=crlf\nignorado.txt export-ignore\n*.txt ident\n"
                                   "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    _write(work, "linhas.txt", "a\nb\n$Id$\n")
    _write(work, "ignorado.txt", "fica\n")
    _write(work, "modelo.bin", LFS_POINTER)
    repos["atributos"] = {"commit": _commit(work, "atributos")}
    repos["atributos"]["bare"] = _publish(work, "atributos")

    work = _new_repo("grande")
    _write(work, "dados.bin", hashlib.shake_256(b"cq-test-fetch-grande").digest(3_000_000))
    repos["grande"] = {"commit": _commit(work, "grande")}
    repos["grande"]["bare"] = _publish(work, "grande")

    work = _new_repo("muitos")
    for i in range(30):
        _write(work, f"arquivos/f{i:02d}.txt", f"arquivo {i}\n")
    repos["muitos"] = {"commit": _commit(work, "muitos")}
    repos["muitos"]["bare"] = _publish(work, "muitos")

    work = _new_repo("paralelo")
    for i in range(300):
        _write(work, f"d{i % 10}/f{i:03d}.txt", f"conteúdo {i}\n" * 20)
    repos["paralelo"] = {"commit": _commit(work, "paralelo")}
    repos["paralelo"]["bare"] = _publish(work, "paralelo")
    return repos


class _State:
    lock = threading.Lock()
    requests: list[dict] = []
    stop = threading.Event()
    port = 0
    backend = ""


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - assinatura da base
        return

    def handle(self) -> None:
        # O aperto de mão TLS acontece aqui, na thread da conexão (o socket de escuta não o faz): uma
        # CA errada no cliente derruba só esta conexão, antes de qualquer pedido HTTP.
        try:
            self.request.do_handshake()
        except (ssl.SSLError, OSError):
            return
        super().handle()

    def do_GET(self) -> None:  # noqa: N802 - nome da base
        self._handle()

    def do_POST(self) -> None:  # noqa: N802 - nome da base
        self._handle()

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _handle(self) -> None:
        path, _, query = self.path.partition("?")
        with _State.lock:
            _State.requests.append({"method": self.command, "path": path, "host": self.headers.get("Host"),
                                    "auth": self.headers.get("Authorization") is not None})
        if path.startswith(f"{PROTECTED_PREFIX}/"):
            # Exige credencial: sem `Authorization`, 401 (o git consultaria o ajudante de credenciais).
            if self.headers.get("Authorization") is None:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="cq-test-fetch"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = path.removeprefix(PROTECTED_PREFIX)
        if path.startswith("/redirecionar/"):
            target = f"https://{OTHER_HOST}:{_State.port}{path.removeprefix('/redirecionar')}"
            if query:
                target += f"?{query}"
            self.send_response(301)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/lento/"):
            _State.stop.wait(120)  # nunca responde: o cliente tem de desistir pelo prazo
            return
        body = self._body()
        env = {"PATH": "/usr/bin:/bin", "HOME": f"{WORK}/home-servidor", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_PROJECT_ROOT": SRV, "GIT_HTTP_EXPORT_ALL": "1", "PATH_INFO": path, "QUERY_STRING": query,
               "REQUEST_METHOD": self.command, "CONTENT_TYPE": self.headers.get("Content-Type", ""),
               "CONTENT_LENGTH": str(len(body)), "REMOTE_ADDR": "127.0.0.1", "SERVER_PROTOCOL": "HTTP/1.0"}
        if self.headers.get("Git-Protocol"):
            env["HTTP_GIT_PROTOCOL"] = self.headers["Git-Protocol"]
        if self.headers.get("Content-Encoding"):
            env["HTTP_CONTENT_ENCODING"] = self.headers["Content-Encoding"]
        proc = subprocess.run([_State.backend], input=body, env=env, capture_output=True, timeout=60)
        output = proc.stdout
        sep = output.find(b"\r\n\r\n")
        head, payload = (output[:sep], output[sep + 4:]) if sep >= 0 else output.partition(b"\n\n")[::2]
        status = 200
        headers = []
        for line in head.split(b"\n"):
            line = line.strip(b"\r")
            if not line:
                continue
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"status":
                status = int(value.split()[0])
            else:
                headers.append((name.strip().decode("latin-1"), value.strip().decode("latin-1")))
        self.send_response(status)
        for name, value in headers:
            if name.lower() != "content-length":
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # Conexões derrubadas pelo cliente (o prazo mata o git; a CA errada desiste no aperto de mão)
        # são esperadas; o relatório não precisa do traceback.
        return


def start_server(tls: dict, backend: str) -> http.server.ThreadingHTTPServer:
    _State.backend = backend
    for path in (f"{WORK}/home-servidor",):
        os.makedirs(path, mode=0o755, exist_ok=True)
    httpd = _QuietServer(("127.0.0.1", 0), _Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tls["cert"], tls["key"])
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    _State.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, name="cq-fetch-https", daemon=True).start()
    return httpd


def _prepare_dirs() -> None:
    os.makedirs(WORK, mode=0o755, exist_ok=True)
    os.chmod(WORK, 0o755)
    for path in (ROOTS, BIN):
        os.makedirs(path, mode=0o755, exist_ok=True)
    for path in (LOG, f"{WORK}/home-worker"):
        os.makedirs(path, mode=0o755, exist_ok=True)
        os.chown(path, WORKER_UID, WORKER_UID)
    with open(RECORDING_GIT, "w", encoding="ascii") as fh:
        fh.write("#!/bin/sh\n"
                 f"{{ echo '--cq-inicio--'; env; echo '--cq-args--'; for a in \"$@\"; do echo \"$a\"; done; "
                 f"echo '--cq-fim--'; }} >> {GIT_ENV_LOG}\n"
                 "exec /usr/bin/git \"$@\"\n")
    os.chmod(RECORDING_GIT, 0o755)


def _case_root(name: str) -> str:
    root = os.path.join(ROOTS, name)
    os.makedirs(root, mode=0o755, exist_ok=True)
    os.chmod(root, 0o755)
    os.chown(root, WORKER_UID, WORKER_UID)
    return root


def _run_as_worker(cfg: dict, env: dict | None = None) -> dict:
    args = ["setpriv", f"--reuid={WORKER_UID}", f"--regid={WORKER_UID}", "--clear-groups", "--",
            sys.executable, SCRIPT, "run", json.dumps(cfg)]
    proc = subprocess.run(args, env=env or RUNNER_ENV, capture_output=True, text=True, timeout=240, cwd="/")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    try:
        return json.loads(lines[-1])
    except (IndexError, ValueError):
        return {"runner_error": f"o processo do caso saiu com {proc.returncode} sem JSON",
                "stderr": proc.stderr[-2000:]}


def _read_as_engine(path: str) -> dict:
    """Lê a árvore publicada como o uid 65534 do motor: `{caminho: sha256}` dos arquivos e os erros."""
    code = ("import hashlib, json, os, sys\n"
            "root = sys.argv[1]; out = {}; errors = []\n"
            "for d, dirs, files in os.walk(root):\n"
            "    for n in files:\n"
            "        p = os.path.join(d, n); r = os.path.relpath(p, root)\n"
            "        if os.path.islink(p):\n"
            "            continue\n"
            "        try:\n"
            "            out[r] = hashlib.sha256(open(p, 'rb').read()).hexdigest()\n"
            "        except OSError as e:\n"
            "            errors.append([r, type(e).__name__])\n"
            "print(json.dumps({'files': out, 'errors': errors}))\n")
    args = ["setpriv", f"--reuid={ENGINE_UID}", f"--regid={ENGINE_UID}", "--clear-groups", "--",
            sys.executable, "-c", code, path]
    proc = subprocess.run(args, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}, capture_output=True,
                          text=True, timeout=60, cwd="/")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"files": {}, "errors": [["<processo>", f"saiu com {proc.returncode}"]]}


def _git_processes(uid: int) -> list[str]:
    """As linhas de comando dos processos com `git` que ainda existem com o `uid` dado."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/status", encoding="ascii", errors="replace") as fh:
                uid_line = next(line for line in fh if line.startswith("Uid:"))
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except (OSError, StopIteration):
            continue
        if int(uid_line.split()[1]) == uid and "git" in cmdline:
            found.append(cmdline)
    return found


def _parse_git_env_log() -> list[dict]:
    if not os.path.exists(GIT_ENV_LOG):
        return []
    with open(GIT_ENV_LOG, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    calls = []
    for block in text.split("--cq-inicio--\n")[1:]:
        env_part, _, rest = block.partition("--cq-args--\n")
        args_part = rest.partition("--cq-fim--")[0]
        env = {}
        for line in env_part.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                env[key] = value
        calls.append({"env": env, "args": args_part.splitlines()})
    return calls


# ---------------------------------------------------------------------------------------------
# Configuração hostil do git
# ---------------------------------------------------------------------------------------------


def _write_script(path: str, text: str) -> None:
    # UTF-8: os comentários dos scripts têm acentos.
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


def plant_hostile_config(port: int) -> str | None:
    """Planta a configuração hostil nos arquivos padrão do git (sistema e usuário) e devolve o conteúdo
    que o `/etc/gitconfig` tinha antes (`None` se não existia), para o `main` restaurar."""
    for path in (HOSTILE, f"{HOSTILE}/hooks"):
        os.makedirs(path, mode=0o755, exist_ok=True)
    for path in (MARKS, HOSTILE_HOME, f"{HOSTILE}/controle"):
        os.makedirs(path, mode=0o755, exist_ok=True)
        os.chown(path, WORKER_UID, WORKER_UID)
    hook = ("#!/bin/sh\n"
            '[ "$(basename "$0")" = reference-transaction ] && cat >/dev/null\n'
            f'echo "$*" >> {MARKS}/hook-$(basename "$0")\n'
            "exit 0\n")
    for name in HOOK_NAMES:
        _write_script(f"{HOSTILE}/hooks/{name}", hook)
    _write_script(f"{HOSTILE}/fsmonitor.sh", f'#!/bin/sh\necho "$*" >> {MARKS}/fsmonitor\nexit 1\n')
    _write_script(f"{HOSTILE}/filtro.sh", f'#!/bin/sh\necho "$1" >> {MARKS}/filtro-$1\nexec cat\n')
    _write_script(f"{HOSTILE}/credencial.sh",
                  "#!/bin/sh\n"
                  "# $1: o nível (sistema | usuario); $2: a ação (get | store | erase)\n"
                  "cat >/dev/null\n"
                  f'echo "$2" >> {MARKS}/credencial-$1\n'
                  'if [ "$1" = usuario ] && [ "$2" = get ]; then\n'
                  "  printf 'username=cq-hostil\\npassword=senha-hostil\\n'\n"
                  "fi\n"
                  "exit 0\n")
    previous = None
    if os.path.lexists(SYSTEM_GITCONFIG):
        with open(SYSTEM_GITCONFIG, encoding="utf-8", errors="surrogateescape") as fh:
            previous = fh.read()
    with open(SYSTEM_GITCONFIG, "w", encoding="ascii") as fh:
        fh.write("[credential]\n"
                 f'\thelper = "!{HOSTILE}/credencial.sh sistema"\n'
                 "[core]\n"
                 f"\thooksPath = {HOSTILE}/hooks\n"
                 f"\tfsmonitor = {HOSTILE}/fsmonitor.sh\n"
                 '[filter "lfs"]\n'
                 f"\tsmudge = {HOSTILE}/filtro.sh smudge\n"
                 f"\tclean = {HOSTILE}/filtro.sh clean\n")
    os.chmod(SYSTEM_GITCONFIG, 0o644)
    user_config = f"{HOSTILE_HOME}/.gitconfig"
    with open(user_config, "w", encoding="ascii") as fh:
        fh.write("[credential]\n"
                 f'\thelper = "!{HOSTILE}/credencial.sh usuario"\n'
                 f'[url "https://{OTHER_HOST}:{port}/"]\n'
                 f"\tinsteadOf = https://{HOST}:{port}/\n"
                 "[http]\n"
                 f"\tcurloptResolve = {OTHER_HOST}:{port}:127.0.0.1\n")
    os.chown(user_config, WORKER_UID, WORKER_UID)
    os.chmod(user_config, 0o644)
    return previous


def restore_system_config(previous: str | None) -> None:
    if previous is None:
        if os.path.lexists(SYSTEM_GITCONFIG):
            os.remove(SYSTEM_GITCONFIG)
        return
    with open(SYSTEM_GITCONFIG, "w", encoding="utf-8", errors="surrogateescape") as fh:
        fh.write(previous)


def clear_marks() -> None:
    for name in os.listdir(MARKS):
        os.remove(os.path.join(MARKS, name))


def _as_worker(args: list[str], *, cwd: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(["setpriv", f"--reuid={WORKER_UID}", f"--regid={WORKER_UID}", "--clear-groups", "--",
                           *args], env=CONTROL_ENV, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=timeout)


def hostile_control(ca: str) -> dict:
    """O controle positivo: um `git` puro (uid 999) no mesmo HOME e com o mesmo `/etc/gitconfig`; só fixa
    o endereço e a CA do servidor de teste por `-c`. Devolve, por passo, a saída, o fim do `stderr` e os
    pedidos que o servidor recebeu; e os marcadores gravados."""
    work = f"{HOSTILE}/controle"
    base = f"https://{HOST}:{_State.port}"
    pin = ["git", "-c", f"http.curloptResolve={HOST}:{_State.port}:127.0.0.1", "-c", f"http.sslCAInfo={ca}"]
    steps = [
        ("clone", [*pin, "clone", "-q", f"{base}/atributos.git", "publico"], work),
        ("status", ["git", "status", "--porcelain"], f"{work}/publico"),
        ("status_again", ["git", "status", "--porcelain"], f"{work}/publico"),
        ("clone_protected", [*pin, "clone", "-q", f"{base}{PROTECTED_PREFIX}/atributos.git", "protegido"], work),
    ]
    clear_marks()
    out: dict = {"steps": {}}
    for name, args, cwd in steps:
        if not os.path.isdir(cwd):
            out["steps"][name] = {"exit": None, "stderr": f"sem o diretório {cwd}", "requests": []}
            continue
        with _State.lock:
            before = len(_State.requests)
        proc = _as_worker(args, cwd=cwd, timeout=120)
        time.sleep(0.2)
        with _State.lock:
            requests = _State.requests[before:]
        out["steps"][name] = {"exit": proc.returncode, "stderr": proc.stderr[-1500:], "requests": requests}
    out["markers"] = listing(MARKS)
    out["config_origins"] = _as_worker(["git", "config", "--show-origin", "--list"], cwd=work,
                                       timeout=60).stdout.splitlines()
    return out


def main() -> dict:
    report: dict = {"environment": environment(), "setup_error": None, "cases": {}}
    env = report["environment"]
    missing = [name for name in ("git", "http_backend", "openssl", "setpriv") if not env[name]]
    if missing:
        report["setup_error"] = f"ferramentas ausentes na imagem: {', '.join(missing)}"
        return report
    try:
        _prepare_dirs()
        tls = make_tls()
        repos = make_repos()
        httpd = start_server(tls, env["http_backend"])
    except Exception:  # noqa: BLE001 - vai inteiro para o relatório
        report["setup_error"] = traceback.format_exc()[-3000:]
        return report
    base = f"https://{HOST}:{_State.port}"
    curso = repos["curso"]
    not_tip = expected_tree(curso["bare"], curso["first"])
    tip = expected_tree(curso["bare"], curso["tip"])
    sha256 = expected_tree(repos["curso256"]["bare"], repos["curso256"]["commit"])
    attrs = expected_tree(repos["atributos"]["bare"], repos["atributos"]["commit"])
    parallel = expected_tree(repos["paralelo"]["bare"], repos["paralelo"]["commit"])

    def cfg(name: str, repo: str, commit: str, **extra: object) -> dict:
        config = {"name": name, "repo": repo, "commit": commit, "root": _case_root(extra.pop("root_name", name)),
                  "timeout": DEFAULT_TIMEOUT, "max_bytes": DEFAULT_MAX_BYTES, "max_files": DEFAULT_MAX_FILES,
                  "ca_file": tls["ca"], "production": False, "git_binary": None, "concurrent": False,
                  "expected_paths": None}
        config.update(extra)
        return config

    plan = [
        (cfg("not_tip", f"{base}/curso.git", curso["first"]), not_tip),
        (cfg("reuse", f"{base}/curso.git", curso["first"], root_name="not_tip"), not_tip),
        (cfg("sha256", f"{base}/curso256.git", repos["curso256"]["commit"]), sha256),
        (cfg("missing_commit", f"{base}/curso.git", MISSING_COMMIT), None),
        (cfg("missing_repository", f"{base}/nao-existe.git", curso["tip"]), None),
        (cfg("redirect", f"{base}/redirecionar/curso.git", curso["tip"]), None),
        (cfg("absolute_symlink", f"{base}/link-absoluto.git", repos["link-absoluto"]["commit"]), None),
        (cfg("escaping_symlink_chain", f"{base}/link-cadeia.git", repos["link-cadeia"]["commit"]), None),
        (cfg("gitlink", f"{base}/gitlink.git", repos["gitlink"]["commit"]), None),
        (cfg("gitattributes", f"{base}/atributos.git", repos["atributos"]["commit"]), attrs),
        (cfg("max_bytes", f"{base}/grande.git", repos["grande"]["commit"], max_bytes=1_000_000), None),
        (cfg("max_files", f"{base}/muitos.git", repos["muitos"]["commit"], max_files=10), None),
        (cfg("deadline", f"{base}/lento/curso.git", curso["tip"], timeout=DEADLINE_TIMEOUT), None),
        (cfg("concurrent", f"{base}/paralelo.git", repos["paralelo"]["commit"], concurrent=True,
             expected_paths=sorted(p for p, v in parallel["tree"].items() if v["type"] != "dir" and p != ".")),
         parallel),
        (cfg("environment", f"{base}/curso.git", curso["tip"], git_binary=RECORDING_GIT), tip),
        (cfg("wrong_ca", f"{base}/curso.git", curso["tip"], ca_file=tls["wrong_ca"]), None),
        (cfg("production", f"https://{HOST}/curso.git", curso["tip"], production=True), None),
    ]

    def run_entry(config: dict, expected: dict | None, env: dict | None = None) -> dict:
        with _State.lock:
            before = len(_State.requests)
        _log(f"caso {config['name']}")
        run = _run_as_worker(config, env)
        time.sleep(0.2)
        with _State.lock:
            requests = _State.requests[before:]
        return {"config": {k: v for k, v in config.items() if k != "expected_paths"}, "expected": expected,
                "run": run, "requests": requests,
                "final_path": os.path.join(config["root"], repo_key(config["repo"]), config["commit"])}

    for config, expected in plan:
        entry = run_entry(config, expected)
        if config["name"] == "not_tip":
            entry["engine_read"] = _read_as_engine(entry["final_path"])
        if config["name"] == "deadline":
            time.sleep(1.0)
            entry["leftover_git_processes"] = _git_processes(WORKER_UID)
        if config["name"] == "environment":
            entry["git_calls"] = _parse_git_env_log()
        report["cases"][config["name"]] = entry

    # Configuração hostil nos arquivos padrão do git: os casos do `ensure` e o controle positivo.
    report["hostile"] = {"error": None, "control": None}
    previous: str | None = None
    planted = False
    try:
        previous = plant_hostile_config(_State.port)
        planted = True
        hostile_plan = [
            (cfg("hostile_config", f"{base}/atributos.git", repos["atributos"]["commit"], hostile=True), attrs),
            (cfg("hostile_config_auth", f"{base}{PROTECTED_PREFIX}/atributos.git", repos["atributos"]["commit"],
                 hostile=True), None),
        ]
        for config, expected in hostile_plan:
            clear_marks()
            entry = run_entry(config, expected, HOSTILE_RUNNER_ENV)
            entry["markers"] = listing(MARKS)
            report["cases"][config["name"]] = entry
        report["hostile"]["control"] = hostile_control(tls["ca"])
    except Exception:  # noqa: BLE001 - vai inteiro para o relatório
        report["hostile"]["error"] = traceback.format_exc()[-3000:]
    finally:
        if planted:
            restore_system_config(previous)
    report["server_port"] = _State.port
    _State.stop.set()
    httpd.shutdown()
    return report


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "main"
    if mode == "run":
        print(json.dumps(run_case(json.loads(sys.argv[2]))), flush=True)
    else:
        try:
            result = main()
        except Exception:  # noqa: BLE001 - o relatório leva o erro
            result = {"environment": None, "setup_error": traceback.format_exc()[-3000:], "cases": {}}
        print(json.dumps(result), flush=True)
