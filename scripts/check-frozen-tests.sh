#!/usr/bin/env bash
# Confere o congelamento dos testes de uma entrega.
# Uso: scripts/check-frozen-tests.sh <dir com testes.sha256>
# Falha se: um hash não confere; a lista de arquivos rastreados mudou; há arquivo novo, alterado ou
# não rastreado nos caminhos congelados.
set -euo pipefail

SPEC_DIR="${1:?informe o diretório com o testes.sha256}"
HASHES="${SPEC_DIR}/testes.sha256"
PATHS=(tests contracts/api/v1 pytest.ini)
GLOBS=('tests/*' 'contracts/api/v1/*' pytest.ini)

sha256sum -c --quiet "${HASHES}"

expected="$(awk '{f=$2; sub(/^*/, "", f); print f}' "${HASHES}" | sort)"
# Os contratos do CLI entram nos caminhos congelados só quando o arquivo de hashes cita
# algum arquivo sob contracts/cli/v1/; os demais caminhos continuam fixos.
if grep -q '^contracts/cli/v1/' <<< "${expected}"; then
  PATHS+=(contracts/cli/v1)
  GLOBS+=('contracts/cli/v1/*')
fi
# Do mesmo modo, os contratos do MCP entram só quando o arquivo de hashes cita algum
# arquivo sob contracts/mcp/v1/.
if grep -q '^contracts/mcp/v1/' <<< "${expected}"; then
  PATHS+=(contracts/mcp/v1)
  GLOBS+=('contracts/mcp/v1/*')
fi
actual="$(git ls-files "${GLOBS[@]}" | sort)"
if [ "${expected}" != "${actual}" ]; then
  echo "a lista de arquivos congelados mudou:" >&2
  diff <(echo "${expected}") <(echo "${actual}") >&2 || true
  exit 1
fi

dirty="$(git status --porcelain --untracked-files=all -- "${PATHS[@]}" | grep -v '__pycache__' || true)"
if [ -n "${dirty}" ]; then
  echo "há mudanças nos caminhos congelados:" >&2
  echo "${dirty}" >&2
  exit 1
fi
echo "congelamento ok ($(wc -l < "${HASHES}") arquivos)"
