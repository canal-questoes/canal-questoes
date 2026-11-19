#!/usr/bin/env bash
# Postgres descartável para os testes: 127.0.0.1:55432, sem senha, sem volume.
# Uso: scripts/test-postgres.sh up | down | status
# Só mexe no container cq-test-pg; nunca remove outros recursos.
set -euo pipefail

NAME=cq-test-pg
IMAGE=postgres:18.6@sha256:86c951e05bf56c93d95d397747fb8820ac76cc3bedb78f43abd83eedbe3666ae
PORT=55432
export MSYS_NO_PATHCONV=1

status() {
  docker ps --filter "name=^${NAME}$" --format '{{.Names}} {{.Status}}'
}

up() {
  if [ -n "$(docker ps -q --filter "name=^${NAME}$")" ]; then
    echo "${NAME} já está no ar"
  else
    if [ -n "$(docker ps -aq --filter "name=^${NAME}$")" ]; then
      docker rm -f "${NAME}" >/dev/null
    fi
    docker run -d --name "${NAME}" \
      -e POSTGRES_HOST_AUTH_METHOD=trust \
      -p "127.0.0.1:${PORT}:5432" \
      --tmpfs /var/lib/postgresql \
      --shm-size=256m \
      "${IMAGE}" \
      -c max_connections=300 >/dev/null
  fi
  for _ in $(seq 1 60); do
    if docker exec "${NAME}" pg_isready -h 127.0.0.1 -p 5432 -U postgres >/dev/null 2>&1; then
      echo "${NAME} pronto em 127.0.0.1:${PORT}"
      return 0
    fi
    sleep 1
  done
  echo "${NAME} não ficou pronto em 60 s" >&2
  return 1
}

down() {
  if [ -n "$(docker ps -aq --filter "name=^${NAME}$")" ]; then
    docker rm -f "${NAME}" >/dev/null
    echo "${NAME} removido"
  else
    echo "${NAME} não existe"
  fi
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "uso: $0 up|down|status" >&2; exit 2 ;;
esac
