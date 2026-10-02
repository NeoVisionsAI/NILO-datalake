#!/usr/bin/env bash
# First boot and later checks for the NILO datalake stack.
# MongoDB and MinIO are containers: current releases are not in Debian apt.

if [[ "$(id -u)" -ne 0 ]]; then
  exec sudo -E bash "$0" "$@"
fi

set -u

ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$ROOT"

GREEN=$'\033[32m'
RED=$'\033[31m'
YELLOW=$'\033[33m'
BOLD=$'\033[1m'
DIM=$'\033[2m'
RESET=$'\033[0m'

OKS=0
FAILS=0

section() {
  printf "\n${BOLD}%s${RESET}\n" "$1"
}

ok() {
  OKS=$((OKS + 1))
  printf "  ${GREEN}✓${RESET} %s\n" "$1"
}

warn() {
  printf "  ${YELLOW}!${RESET} %s\n" "$1"
}

fail() {
  local title="$1"
  local detail="${2:-}"
  FAILS=$((FAILS + 1))
  printf "  ${RED}✗${RESET} %s\n" "$title"
  if [[ -n "$detail" ]]; then
    printf "${RED}"
    while IFS= read -r line; do
      printf "      %s\n" "$line"
    done <<<"$detail"
    printf "${RESET}"
  fi
}

run_quiet() {
  local title="$1"
  shift
  local err
  err="$(mktemp)"
  if "$@" >"$err" 2>&1; then
    ok "$title"
    rm -f "$err"
    return 0
  fi
  fail "$title" "$(cat "$err")"
  rm -f "$err"
  return 1
}

printf "\n${BOLD}NILO datalake${RESET}\n"
printf "${DIM}%s${RESET}\n" "$(date -u +"%Y-%m-%d %H:%M:%S UTC")"

section "Host packages"

missing=()
for pkg in ca-certificates curl rsync openssh-client; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    ok "$pkg"
  else
    missing+=("$pkg")
  fi
done

if command -v docker >/dev/null 2>&1; then
  ok "docker $(docker --version 2>/dev/null | awk '{print $3}' | tr -d ',')"
else
  missing+=(docker.io)
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "docker compose"
elif ! dpkg -s docker-compose-v2 >/dev/null 2>&1; then
  missing+=(docker-compose-v2)
fi

if ((${#missing[@]} > 0)); then
  warn "installing: ${missing[*]}"
  apt_log="$(mktemp)"
  if apt-get update >"$apt_log" 2>&1 && apt-get install -y "${missing[@]}" >>"$apt_log" 2>&1; then
    ok "apt install ${missing[*]}"
  else
    fail "apt install ${missing[*]}" "$(tail -n 40 "$apt_log")"
  fi
  rm -f "$apt_log"
fi

section "Docker"

if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet docker; then
    ok "docker daemon"
  elif systemctl enable --now docker >/tmp/nilo-docker-start.err 2>&1; then
    ok "docker daemon started"
  else
    fail "docker daemon" "$(cat /tmp/nilo-docker-start.err 2>/dev/null || true)"
  fi
elif docker info >/dev/null 2>&1; then
  ok "docker daemon"
else
  fail "docker daemon" "systemctl is not available and docker info failed"
fi

if ! docker compose version >/dev/null 2>&1; then
  fail "docker compose" "install docker-compose-v2 and run this script again"
fi

section "Configuration"

if [[ -f "$ROOT/.env" ]]; then
  ok ".env present"
else
  cp "$ROOT/config/docker.env.example" "$ROOT/.env"
  ok "wrote .env from config/docker.env.example"
fi

# shellcheck disable=SC1091
set -a
source "$ROOT/.env"
set +a

if [[ "${NILO_INGEST_API_KEY:-nilo-dev-key}" == "nilo-dev-key" ]]; then
  warn "NILO_INGEST_API_KEY is still the development default. Change .env before a real site."
else
  ok "ingest API key is set"
fi

section "Containers"

if docker compose version >/dev/null 2>&1; then
  up_log="$(mktemp)"
  if docker compose up -d --build --wait >"$up_log" 2>&1; then
    ok "docker compose up"
  else
    fail "docker compose up" "$(tail -n 60 "$up_log")"
    printf "\n${DIM}Last container logs:${RESET}\n"
    docker compose logs --tail 40 2>/dev/null || true
  fi
  rm -f "$up_log"
fi

wait_ready() {
  local title="$1"
  local service="$2"
  shift 2
  local err i
  err="$(mktemp)"
  for i in $(seq 1 40); do
    if "$@" >"$err" 2>&1; then
      ok "$title"
      rm -f "$err"
      return 0
    fi
    sleep 3
  done
  {
    echo "last check:"
    cat "$err"
    echo
    echo "container logs:"
    docker compose logs --tail 50 "$service" 2>&1 || true
  } >"${err}.full"
  fail "$title" "$(cat "${err}.full")"
  rm -f "$err" "${err}.full"
  return 1
}

if docker compose version >/dev/null 2>&1; then
  wait_ready "datalake http://127.0.0.1:8088/v1/health" datalake \
    curl -fsS --max-time 3 http://127.0.0.1:8088/v1/health
  wait_ready "MinIO http://127.0.0.1:9000/minio/health/live" minio \
    curl -fsS --max-time 3 http://127.0.0.1:9000/minio/health/live
fi

section "Summary"

printf "  ${GREEN}%s ok${RESET}   ${RED}%s failed${RESET}\n" "$OKS" "$FAILS"
if ((FAILS > 0)); then
  printf "\n${RED}Deploy failed.${RESET} The lines above are the check that broke and its trace.\n"
  printf "More logs: ${BOLD}docker compose logs -f${RESET}\n\n"
  exit 1
fi

printf "\n${GREEN}Stack is up.${RESET}\n"
printf "  Console http://127.0.0.1:8088/console\n"
printf "          user and password are NILO_CONSOLE_USERNAME and NILO_CONSOLE_PASSWORD in .env\n"
printf "          after the first start, change them in the console; a later .env edit is not copied\n"
printf "  API     http://127.0.0.1:8088/v1/health\n"
printf "  MinIO   http://127.0.0.1:9001   (user %s)\n" "${MINIO_ROOT_USER:-nilo}"
printf "          buckets nilo-media (sessions) and nilo-backups (database dumps)\n"
printf "  Traces  docker compose exec datalake nilo-datalake traces\n"
printf "          also /data/traces/failures.jsonl inside the datalake volume\n\n"
