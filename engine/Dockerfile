# Imagem de execução do engine. Contexto de build: raiz do repositório.
#   docker build -f engine/Dockerfile -t canal-questoes/engine:dev .
# Roda dentro da imagem oficial do PrairieLearn, fixada por commit e digest,
# para reutilizar o código Python do PL e as peças de orquestração do dist sem alteração.
FROM prairielearn/prairielearn:a193f72048530a4499252c454acca963cdf98922@sha256:c2b033eba27657a022ca82535cf48b4098bae02609e7f23114512bcf2d0ea4b6

ENV PL_COMMIT=a193f72048530a4499252c454acca963cdf98922 \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp \
    PYTHONHASHSEED=0 \
    ENGINE_REPOS_ROOT=/repos

# Dentro de apps/prairielearn, para os imports resolverem para as mesmas versões do PL.
WORKDIR /PrairieLearn/apps/prairielearn/canal-questoes-engine

COPY engine/package.json engine/tsconfig.json ./
COPY engine/src ./src
COPY engine/docker/init.py ./docker/init.py
COPY contracts/engine/v1 ./contracts/engine/v1
RUN mkdir -p /repos

USER 65534:65534
EXPOSE 8080

# A saúde é a do motor (GET /v1/health), e não a do PL: a imagem base herda um HEALTHCHECK
# para a porta 3000 do servidor do PL, que aqui não roda e deixaria o container "unhealthy" para
# sempre. A consulta vai ao loopback, que existe mesmo com --network none, na porta do ENGINE_PORT;
# com ENGINE_PORT=0 ou com ENGINE_HOST num endereço que não atende no loopback, quem roda o container
# declara o próprio healthcheck (`healthcheck:` no compose) ou o desliga (`docker run --no-healthcheck`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl --fail --silent --show-error --max-time 4 --output /dev/null "http://127.0.0.1:${ENGINE_PORT:-8080}/v1/health"

# PID 1 que repassa os sinais e recolhe os processos órfãos (docker/init.py): a imagem do PL não
# traz tini, e o node como PID 1 deixaria zumbi cada worker Python morto num tempo esgotado. Com
# `docker run --init` (ou `init: true` no compose), o init do Docker fica acima deste, sem conflito.
# O CMD, ou o comando do `docker run`, roda como filho: `docker run <imagem> node src/cli.ts`.
ENTRYPOINT ["/PrairieLearn/.venv/bin/python3.13", "-I", "-S", "/PrairieLearn/apps/prairielearn/canal-questoes-engine/docker/init.py", "--"]
CMD ["node", "src/main.ts"]
