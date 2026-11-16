#!/usr/bin/env bash
# Suíte do motor: constrói as imagens, roda os testes na imagem restrita, sobe o oráculo (container
# `pl-oracle`, 127.0.0.1:3000) e roda a conformidade; no fim, confere o congelamento dos testes do motor, só no
# repositório de origem (a árvore exportada não tem a lista do congelamento e escreve uma linha fixa no lugar).
# Rode sem outras suítes em paralelo: três testes do motor têm timeout de 2 s.
# Uso: scripts/run-engine-suite.sh [rótulo]
#   CQ_SUITE_OUT=<dir>  onde gravar as saídas (padrão: ${TMPDIR:-/tmp}/cq-engine-suite)
# Sai com 1 se alguma parte falhar ou se o congelamento não conferir (no repositório de origem, a lista ausente é erro).
set -u
cd "$(git rev-parse --show-toplevel)"
export MSYS_NO_PATHCONV=1
OUT="${CQ_SUITE_OUT:-${TMPDIR:-/tmp}/cq-engine-suite}/${1:-full}"
mkdir -p "${OUT}"
STATUS=0
step() { echo "== $1: $(date +%H:%M:%S)"; }
summary() { grep -E '^ℹ (tests|pass|fail|skipped)' "$1" | tr '\n' ' '; echo; }

step build-dev
docker build -f engine/Dockerfile -t canal-questoes/engine:dev . > "${OUT}/build-dev.txt" 2>&1 \
  || { echo "build-dev falhou"; tail -20 "${OUT}/build-dev.txt"; exit 1; }
step build-test
docker build -f engine/test/Dockerfile -t canal-questoes/engine:test . > "${OUT}/build-test.txt" 2>&1 \
  || { echo "build-test falhou"; tail -20 "${OUT}/build-test.txt"; exit 1; }

step image-suite
docker run --rm --network none --user 65534:65534 --read-only --tmpfs /tmp canal-questoes/engine:test \
  > "${OUT}/image-suite.txt" 2>&1 || STATUS=1
summary "${OUT}/image-suite.txt"

step oracle
docker start pl-oracle > /dev/null 2>&1 || echo "docker start pl-oracle falhou (o container existe?)"
for i in $(seq 1 60); do
  if curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/ | grep -qE '^(200|302)$'; then
    echo "oráculo no ar"; break
  fi
  sleep 5
done

step conformance
node --test "engine/conformance/*.test.ts" > "${OUT}/conformance.txt" 2>&1 || STATUS=1
summary "${OUT}/conformance.txt"

step frozen
# A árvore exportada é reconhecida pelo marcador `ops/ci/known-flaky.txt` rastreado pelo git (o mesmo critério dos
# testes); fora dela, a conferência roda e a lista ausente continua erro.
if [ "$(git ls-files -- ops/ci/known-flaky.txt)" != "" ]; then
  echo "congelamento: sem lista neste repositório"
else
  bash scripts/check-frozen-engine-tests.sh specs/001-motor-questoes || STATUS=1
fi
echo "saídas em ${OUT}"
exit "${STATUS}"
