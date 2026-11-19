# Imagem única do servidor (api e worker), pelo guia "uv in Docker"
# (https://docs.astral.sh/uv/guides/integration/docker/).
#
# - Duas etapas: a primeira instala as dependências e depois o pacote cq-server (não editável) num
#   .venv; a final só copia esse .venv para uma imagem limpa, sem o uv e sem o código-fonte.
# - Toda imagem é fixada por digest.
# - Roda como o usuário nonroot (uid 999). Nenhum segredo entra na imagem: tudo vem do ambiente do
#   container (compose.yaml).

# --- Etapa de construção -----------------------------------------------------------------------
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d /uv /uvx /bin/

# Bytecode compilado na construção (a imagem final não escreve .pyc); cópia em vez de links, porque
# o cache fica numa montagem; o Python é sempre o da imagem base.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# 1) Só as dependências (camada que muda pouco). `--frozen`, e não `--locked`: os pyproject.toml dos
#    membros do workspace ainda não estão aqui.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-workspace --package cq-server --no-dev

# 2) O workspace (só o necessário) e o pacote cq-server, instalado como não editável. O cliente só
#    entra com o pyproject.toml, para o uv conferir o lock do workspace inteiro (`--locked`).
COPY pyproject.toml uv.lock /app/
COPY client/pyproject.toml /app/client/pyproject.toml
COPY server /app/server
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --package cq-server --no-dev

# --- Etapa final -------------------------------------------------------------------------------
FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot

# O `git` do Debian para a obtenção de
# `repo@commit` (só o adaptador `cq_server/adapters/git.py` o chama; sem `git-lfs`, sem as
# recomendações) e o ponto de montagem do volume `repos`, 0755 com o dono 999 do worker. O motor lê
# como 65534 pelos bits de "outros" e monta o volume com `nocopy`, então é este diretório que dá o dono
# ao volume novo. O `.staging` (0700, o motor nem lista) vem junto na cópia inicial para o volume nascer
# não vazio: enquanto está vazio, qualquer montagem sem `nocopy` de outra imagem refaz a cópia e troca o
# dono. A versão do pacote não é fixada: o digest da base fixa o
# trixie, e o `apt` traz as correções de segurança.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o 999 -g 999 -m 0755 /repos \
 && install -d -o 999 -g 999 -m 0700 /repos/.staging

# O .venv fica com o dono root: o processo lê, mas não altera o próprio código.
COPY --from=builder /app/.venv /app/.venv

# PYTHONTZPATH vazio: o `zoneinfo` usa só o pacote `tzdata` do .venv, a mesma base de fusos do host
# de desenvolvimento.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONTZPATH=""

WORKDIR /app
EXPOSE 8000

USER nonroot

CMD ["cq-server", "serve"]
