"""Adaptador de obtenção das referências `bank:<nome>`: a porta de obtenção cumprida a partir do pacote
guardado no PostgreSQL, sem rede e sem `git`.

`BankRepoFetcher(settings, load)` implementa `ports.repos.RepoFetcher`. `ensure(repo, commit)`:
`<repos>/<repo_key>/<versão>/` já existe → `Fetched(reused=True)`, sem carga; senão carrega o pacote pelo
`load(nome, versão)` (`BankMissing` → `FetchNotFound`), lê o pacote com `banks.read_archive` e os tetos do
worker (`max_files`, `max_bytes`), escrevendo cada membro em `.staging/<uuid4>/tree` (arquivos 0644,
diretórios 0755, abertura exclusiva sem seguir links), confere a versão recalculada e faz o `rename` atômico
para o destino (o destino que já apareceu vale como reuso). A área de preparo sai sempre no fim, apagada sem
recursão (a árvore de um banco pode ter milhares de níveis).

Os desfechos usam só os conjuntos fechados da porta: o conteúdo acima do teto de bytes → `max_bytes`, arquivos
demais → `max_files`, qualquer outra recusa da leitura (o pacote corrompido no banco de dados ou uma exceção da
leitura que não é de E/S do volume, que a leitura converte em recusa) e a versão que não confere →
`path_invalid`; a falha do carregador ou do volume → falha temporária com o nome do tipo da exceção. A falha de
E/S na escrita de um membro (disco cheio, erro de E/S, área de preparo removida) chega aqui como recusa da
leitura, que converte toda exceção; o escritor a anota antes e ela vira falha temporária, não recusa do conteúdo.
A exceção é o nome longo demais para o volume (`ENAMETOOLONG` no `mkdir` ou no `open` de um membro: um caminho
aceito pela api que, somado ao prefixo `<raiz>/.staging/<uuid4>/tree/`, passa do limite do sistema): o escritor
não o anota, e ele fica como recusa do conteúdo, `path_invalid`, sem repetição. Nada do pacote (conteúdo ou nome
de arquivo) vai a registro, exceção ou desfecho: o adaptador não registra nada, e o texto das exceções nunca sai
daqui.

Sem `subprocess`.
"""

from __future__ import annotations

import errno
import os
import re
import uuid
from collections.abc import Callable
from typing import BinaryIO, cast

from cq_server.banks import BANK_NAME, BANK_REPO_PREFIX, BankArchiveError, BankMissing, bank_version, read_archive
from cq_server.config import RepoSettings
from cq_server.ports.repos import Fetched, FetchNotFound, FetchRefused, FetchTemporaryFailure
from cq_server.questionrefs import repo_key

# O diretório de trabalho dentro da raiz, o mesmo da obtenção pelo git (a limpeza da partida vale para os dois).
STAGING_DIR = ".staging"

# A versão de um banco: o SHA-256 hex minúsculo do manifesto (só ela entra num caminho).
_VERSION = re.compile(r"\A[0-9a-f]{64}\Z")

# Os modos publicados, fixados depois de criar (sem depender do `umask`).
_FILE_MODE = 0o644
_DIR_MODE = 0o755

# A criação exclusiva de cada arquivo, sem seguir link (onde existe) e em modo binário (no Windows).
_CREATE_FLAGS = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0))

# A recusa da leitura do pacote → a regra da porta; as demais viram `path_invalid`.
_REFUSED_RULE = {"too_large": "max_bytes", "too_many": "max_files"}


def _reused() -> Fetched:
    return Fetched(tree_oid=None, file_count=None, byte_count=None, reused=True)


def _remove_tree(path: str) -> None:
    """Apaga a árvore em `path`, de baixo para cima e sem recursão, ignorando as falhas.

    O `shutil.rmtree` do Python 3.12 é recursivo e levanta `RecursionError` numa árvore mais funda que o limite de
    recursão, e a área de preparo pode ter milhares de níveis. O `os.walk` de baixo para cima usa uma pilha e não
    entra em link. Um link para diretório, que ele lista entre os diretórios, é apagado como link (no Linux, o
    `rmdir` falha e o `unlink` o remove), sem tocar no alvo. A remoção é por caminho, não por descritor: a área é
    do próprio worker (0700, com um nome novo), e o conteúdo do pacote não cria links."""
    for current, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(current, name))
            except OSError:
                pass
        for name in dirs:
            entry = os.path.join(current, name)
            try:
                os.rmdir(entry)
            except OSError:
                try:
                    os.unlink(entry)
                except OSError:
                    pass
    try:
        os.rmdir(path)
    except OSError:
        pass


class _MemberFile:
    """O arquivo de um membro que anota no escritor a falha de E/S da escrita e do fechamento (e a repassa)."""

    def __init__(self, raw: BinaryIO, writer: _TreeWriter) -> None:
        self._raw = raw
        self._writer = writer

    def write(self, data: bytes) -> int:
        try:
            return self._raw.write(data)
        except OSError as exc:
            self._writer.note_io_error(exc)
            raise

    def close(self) -> None:
        try:
            self._raw.close()
        except OSError as exc:
            self._writer.note_io_error(exc)
            raise


class _TreeWriter:
    """A escrita dos membros do pacote numa árvore nova: cria os diretórios pais (0755) e abre cada arquivo com
    criação exclusiva (0644). Os caminhos já vêm conferidos pela leitura do pacote (sem `.`, `..`, barra invertida
    nem conflito entre arquivo e diretório).

    `io_error` guarda o nome do tipo da primeira falha de E/S do volume (na criação, na escrita ou no
    fechamento), que a leitura do pacote converte em recusa; só o nome do tipo, nunca o texto. O nome longo demais
    para o volume (`ENAMETOOLONG`) não é anotado: é recusa do conteúdo."""

    def __init__(self, tree: str) -> None:
        self.tree = tree
        # Os diretórios já criados, numa árvore de dicionários (um nível por componente do caminho).
        self.created: dict[str, dict] = {}
        self.io_error: str | None = None

    def note_io_error(self, exc: OSError) -> None:
        """Anota a falha de E/S do volume. O nome longo demais (`ENAMETOOLONG`) fica de fora: é o caminho do
        conteúdo que não cabe no volume, e a leitura do pacote o converte em recusa, como qualquer outra exceção."""
        if exc.errno == errno.ENAMETOOLONG:
            return
        if self.io_error is None:
            self.io_error = type(exc).__name__

    def _inside(self, parts: list[str]) -> str:
        """O caminho de `parts` dentro da árvore. No Windows, um componente com `:` (unidade ou fluxo alternativo do
        NTFS) faria a junção sair da árvore ou escrever num fluxo escondido: é recusado como conteúdo inválido (não
        como falha do volume). A junção é conferida dentro da árvore em todo sistema."""
        if os.name == "nt" and any(":" in part for part in parts):
            raise ValueError("componente fora da forma deste sistema")
        full = os.path.join(self.tree, *parts)
        root = os.path.normpath(self.tree)
        if os.path.commonpath([root, os.path.normpath(full)]) != root:
            raise ValueError("caminho fora da árvore")
        return full

    def _parents(self, parts: list[str]) -> None:
        """Cria, do mais raso ao mais fundo, os diretórios pais de `parts` que ainda não existem (0755). A procura
        nos já criados desce a árvore deles um componente por nível, sem montar de novo o prefixo; só o diretório
        novo monta o caminho dele, para a conferência `_inside` e o `mkdir`."""
        node = self.created
        for index, part in enumerate(parts[:-1], start=1):
            child = node.get(part)
            if child is None:
                full = self._inside(parts[:index])
                os.mkdir(full, 0o700)
                os.chmod(full, _DIR_MODE)
                child = node[part] = {}
            node = child

    def open_member(self, path: str) -> BinaryIO:
        try:
            raw = self._open(path)
        except OSError as exc:
            self.note_io_error(exc)
            raise
        return cast(BinaryIO, _MemberFile(raw, self))

    def _open(self, path: str) -> BinaryIO:
        parts = path.split("/")
        target = self._inside(parts)
        self._parents(parts)
        fd = os.open(target, _CREATE_FLAGS, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, _FILE_MODE)
            else:  # pragma: no cover - só onde não há `fchmod`
                os.chmod(target, _FILE_MODE)
            return os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise


class BankRepoFetcher:
    """A publicação da árvore de um banco enviado no volume de repositórios."""

    def __init__(self, settings: RepoSettings, load: Callable[[str, str], bytes | BankMissing]) -> None:
        self.settings = settings
        self.load = load

    def ensure(self, repo: str, commit: str) -> Fetched | FetchRefused | FetchNotFound | FetchTemporaryFailure:
        root = self.settings.repos_root
        if root is None:
            return FetchTemporaryFailure("repos_unavailable")
        # A forma da referência: só um nome válido e uma versão hexadecimal entram num caminho.
        if not (isinstance(repo, str) and isinstance(commit, str) and repo.startswith(BANK_REPO_PREFIX)
                and BANK_NAME.fullmatch(repo[len(BANK_REPO_PREFIX):]) and _VERSION.fullmatch(commit)):
            return FetchRefused("repo_syntax")
        name = repo[len(BANK_REPO_PREFIX):]
        key_dir = os.path.join(root, repo_key(repo))
        final = os.path.join(key_dir, commit)
        # 1. O reuso, antes de carregar o pacote.
        if os.path.isdir(final):
            return _reused()
        # 2. A carga do pacote guardado; o texto da exceção não sai daqui.
        try:
            loaded = self.load(name, commit)
        except Exception as exc:  # noqa: BLE001 - só o nome do tipo vai ao desfecho
            return FetchTemporaryFailure(type(exc).__name__)
        if isinstance(loaded, BankMissing):
            return FetchNotFound(loaded.what)
        # 3. A escrita na área de preparo, a conferência e a publicação; a área sai sempre no fim.
        staging = os.path.join(root, STAGING_DIR, str(uuid.uuid4()))
        try:
            return self._publish(bytes(loaded), commit, staging=staging, key_dir=key_dir, final=final)
        except OSError as exc:
            return FetchTemporaryFailure(type(exc).__name__)
        finally:
            _remove_tree(staging)

    def _publish(self, archive: bytes, commit: str, *, staging: str, key_dir: str,
                 final: str) -> Fetched | FetchRefused | FetchTemporaryFailure:
        os.makedirs(os.path.dirname(staging), mode=0o700, exist_ok=True)
        os.mkdir(staging, 0o700)
        tree = os.path.join(staging, "tree")
        os.mkdir(tree, 0o700)
        writer = _TreeWriter(tree)
        try:
            summary = read_archive(archive, max_files=self.settings.max_files, max_bytes=self.settings.max_bytes,
                                   open_member=writer.open_member)
        except BankArchiveError as refusal:
            # A recusa causada pela falha do volume na escrita é falha temporária, não pacote corrompido.
            if writer.io_error is not None:
                return FetchTemporaryFailure(writer.io_error)
            return FetchRefused(_REFUSED_RULE.get(refusal.reason, "path_invalid"))
        if bank_version(summary.entries) != commit:
            return FetchRefused("path_invalid")
        # A publicação atômica: modos explícitos, `rename` e o destino que já apareceu como reuso.
        os.chmod(tree, _DIR_MODE)
        os.makedirs(key_dir, exist_ok=True)
        try:
            os.chmod(key_dir, _DIR_MODE)
        except OSError:
            pass
        try:
            os.rename(tree, final)
        except OSError as exc:
            if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                return _reused()
            raise
        return Fetched(tree_oid=None, file_count=summary.file_count, byte_count=summary.byte_count, reused=False)
