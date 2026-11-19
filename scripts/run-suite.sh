#!/usr/bin/env bash
# Suíte Python inteira, rodada por diretório: no Windows, a suíte numa só execução esgota as
# portas efêmeras. Os testes de integração vão em duas metades.
# As partes sem sistema rodam em paralelo pelo pytest-xdist:
# -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal. A parte -m system segue em série, sem -n. O paralelismo entra só
# pela linha de comando (nem o pytest.ini nem o tests/conftest.py o configuram). A suíte do motor não roda aqui.
# Uso: scripts/run-suite.sh [rótulo]
#   FROZEN=<dir com testes.sha256>  confere o congelamento no fim
#   SYSTEM=0                        pula o teste de sistema (Compose)
#   CQ_SUITE_WORKERS=<N>            processos do xdist nas partes sem sistema (padrão: 4, medido em
#                                   2026-11-03: com 6, o WinError 10048 e o 503 do Telegram falso apareciam em
#                                   7 a 11 casos por execução; com 4, só os instáveis conhecidos, em 19 a 21 min;
#                                   se aparecer "too many clients" ou o WinError 10048, baixe este número primeiro)
#   CQ_SUITE_OUT=<dir>              onde gravar as saídas (padrão: ${TMPDIR:-/tmp}/cq-suite)
# Sai com 1 se alguma parte falhar ou se o congelamento não conferir.
set -u
cd "$(git rev-parse --show-toplevel)"
export PYTHONUTF8=1
TAG="${1:-full}"
OUT="${CQ_SUITE_OUT:-${TMPDIR:-/tmp}/cq-suite}/${TAG}"
mkdir -p "${OUT}"
INT=( $(ls tests/integration/test_*.py | sort) )
HALF=$(( ${#INT[@]} / 2 ))
STATUS=0
run() {
  local name="$1"; shift
  timeout 3000 uv run pytest -q -p no:cacheprovider --junitxml="${OUT}/${name}.xml" "$@" > "${OUT}/${name}.txt" 2>&1 || STATUS=1
  echo "${name}: $(tail -1 "${OUT}/${name}.txt")"
}
run unit tests/unit -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
run contract tests/contract -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
run cli tests/cli -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
run intA "${INT[@]:0:$HALF}" -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
run intB "${INT[@]:$HALF}" -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
run privacy tests/privacy -m "not system" -n "${CQ_SUITE_WORKERS:-4}" --dist worksteal
if [ "${SYSTEM:-1}" = "1" ]; then
  run system tests/system -m system
fi
if [ -n "${FROZEN:-}" ]; then
  bash scripts/check-frozen-tests.sh "${FROZEN}" || STATUS=1
fi
echo "saídas em ${OUT}"
exit "${STATUS}"
