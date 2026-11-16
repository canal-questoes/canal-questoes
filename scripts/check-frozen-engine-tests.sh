#!/usr/bin/env bash
# Confere o congelamento dos testes do motor.
# Uso: scripts/check-frozen-engine-tests.sh <dir com testes.sha256>
# Falha se: um hash não confere; a lista de arquivos rastreados mudou; há arquivo novo, alterado ou
# não rastreado nos caminhos congelados. Aceita nomes com espaço (fixtures como "figura dois.png").
set -euo pipefail

SPEC_DIR="${1:?informe o diretório com o testes.sha256}"
HASHES="${SPEC_DIR}/testes.sha256"
PATHSPECS=('engine/test/*' 'engine/conformance/*.ts' engine/tsconfig.json .dockerignore 'contracts/engine/v1/*.json')

sha256sum -c --quiet "${HASHES}"

# Cada linha é "<64 hex> *<caminho>" (ou "<64 hex>  <caminho>"): o caminho começa na coluna 67.
expected="$(cut -c67- "${HASHES}" | sort)"
actual="$(git ls-files -- "${PATHSPECS[@]}" | sort)"
if [ "${expected}" != "${actual}" ]; then
  echo "a lista de arquivos congelados mudou:" >&2
  diff <(echo "${expected}") <(echo "${actual}") >&2 || true
  exit 1
fi

dirty="$(git status --porcelain --untracked-files=all -- engine/test 'engine/conformance/*.ts' engine/tsconfig.json .dockerignore contracts/engine/v1 \
  | grep -v '__pycache__' || true)"
if [ -n "${dirty}" ]; then
  echo "há mudanças nos caminhos congelados:" >&2
  echo "${dirty}" >&2
  exit 1
fi
echo "congelamento ok ($(wc -l < "${HASHES}") arquivos)"
