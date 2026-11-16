"""Distingue o repositório de origem da árvore exportada pelo `ops/export/export_public.py`.

Alguns testes congelados conferem arquivos privados que, por definição, não existem na árvore exportada (os
diretórios privados, a configuração local do agente). Para esses testes rodarem nas duas árvores sem perder asserção, eles perguntam
aqui em qual árvore estão.

O marcador é o `ops/ci/known-flaky.txt` **rastreado pelo git**: **[F]** o `export_public.py` grava esse arquivo no
destino (antes da varredura) e o commit da exportação o leva; na origem ele não é rastreado (`git ls-files` vazio).
A conferência é pelo `git ls-files`, não pela existência do arquivo no disco: uma cópia solta do arquivo na origem
não muda a resposta.
"""

from __future__ import annotations

import subprocess

from tests.helpers.ops import REPO

EXPORT_MARKER = "ops/ci/known-flaky.txt"


def tracked_files(*pathspecs: str) -> list[str]:
    """Os arquivos rastreados do repositório (`git ls-files -z`, só leitura), restritos aos `pathspecs` dados."""
    listed = subprocess.run(["git", "ls-files", "-z", "--", *pathspecs], cwd=REPO, capture_output=True, timeout=120,
                            check=False)
    assert listed.returncode == 0, "git ls-files falhou"
    return sorted(name for name in listed.stdout.decode("utf-8").split("\0") if name)


def is_exported_tree() -> bool:
    """Verdadeiro só na árvore exportada: o marcador `ops/ci/known-flaky.txt` é rastreado pelo git."""
    return tracked_files(EXPORT_MARKER) == [EXPORT_MARKER]
