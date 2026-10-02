#!/usr/bin/env bash
# Pull the published images and start the archive, the console, and MinIO.
set -euo pipefail

cd "$(dirname "$0")"
_vm_lib="$(dirname "$0")/_lib.sh"
if [[ ! -f "$_vm_lib" ]]; then
  _ref="${NILO_DEPLOY_REF:-main}"
  _url="https://raw.githubusercontent.com/NeoVisionsAI/NILO-datalake/${_ref}/deploy/vm-ghcr/_lib.sh"
  echo "Missing _lib.sh — downloading (or run ./bootstrap.sh to refresh all scripts)..."
  curl -fsSL "$_url" -o "${_vm_lib}.partial"
  mv "${_vm_lib}.partial" "$_vm_lib"
  chmod 644 "$_vm_lib"
fi
# shellcheck source=_lib.sh
source "$_vm_lib"
nilo_ensure_docker_session "$0" "$@" || exit 1

if [[ ! -f credentials.env ]]; then
  echo "credentials.env is missing. Run ./configure.sh or copy credentials.env.example." >&2
  exit 1
fi

env_value() {
  nilo_env_value "$1" credentials.env
}

reject_placeholder() {
  local key="$1"
  local value
  value="$(env_value "$key")"
  case "$value" in
    ""|CHANGE_ME|nilo-dev-key|nilo-secret|change-me)
      echo "$key is still a placeholder. Run ./configure.sh and set a real value." >&2
      echo "Labs can override this check with NILO_ALLOW_DEV_SECRETS=1." >&2
      exit 1
      ;;
  esac
  case "$value" in
    *[@:/?#]*)
      echo "$key contains @ : / ? or #. Use letters, digits, dot, underscore, or hyphen." >&2
      exit 1
      ;;
  esac
}

compose() {
  docker compose --env-file credentials.env "$@"
}

pull_datalake_image() {
  local image log
  image="$(env_value SERVICE_IMAGE)"
  if [[ -z "$image" ]]; then
    echo "SERVICE_IMAGE is empty in credentials.env." >&2
    return 1
  fi
  nilo_ensure_ghcr_login credentials.env || return 1
  log="$(mktemp)"
  if compose pull datalake >"$log" 2>&1; then
    rm -f "$log"
    return 0
  fi
  cat "$log" >&2
  if grep -qiE 'unauthorized|denied|permission' "$log"; then
    nilo_explain_ghcr_unauthorized "$image"
  fi
  rm -f "$log"
  return 1
}

minio_is_up() {
  curl -fsS --max-time 3 http://127.0.0.1:9000/minio/health/live >/dev/null 2>&1
}

ensure_docker() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  echo "Docker is not usable from this user. Installing docker.io and the Compose plugin..."
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl docker.io docker-compose-v2 \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl docker.io docker-compose-plugin
  sudo systemctl enable --now docker || true
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  echo "Docker is installed, but this shell cannot talk to the socket." >&2
  echo "Run ./deploy.sh again (the script applies the docker group automatically)." >&2
  return 1
}

ensure_minio() {
  if minio_is_up; then
    echo "MinIO is already running at http://127.0.0.1:9000."
    return 0
  fi
  if [[ "${NILO_ALLOW_DEV_SECRETS:-}" != 1 ]]; then
    reject_placeholder MINIO_ROOT_PASSWORD
  fi
  ensure_docker
  echo "MinIO is not running. Pulling the image and starting it..."
  compose pull minio minio-init
  compose up -d minio
  local i
  for i in $(seq 1 40); do
    if minio_is_up; then
      compose run --rm --no-deps minio-init
      echo "MinIO is up."
      echo "  API      http://127.0.0.1:9000"
      echo "  Console  http://127.0.0.1:9001"
      echo "  Buckets  nilo-media (sessions) and nilo-backups (database dumps)"
      return 0
    fi
    sleep 3
  done
  echo "MinIO did not become ready." >&2
  compose logs --tail 40 minio >&2 || true
  return 1
}

install_systemd() {
  local unit="/etc/systemd/system/nilo-datalake-vm.service"
  if [[ ! -f nilo-datalake-vm.service ]]; then
    echo "nilo-datalake-vm.service is missing. Run ./bootstrap.sh." >&2
    exit 1
  fi
  sed "s|@DIR@|$(pwd)|g" nilo-datalake-vm.service | sudo tee "$unit" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now nilo-datalake-vm.service
  echo "systemd unit enabled: $unit"
}

case "${1:-}" in
  --ensure-minio)
    ensure_minio
    exit 0
    ;;
  --install-systemd)
    install_systemd
    exit 0
    ;;
  --stop)
    compose down
    exit 0
    ;;
  --foreground)
    if [[ "${NILO_ALLOW_DEV_SECRETS:-}" != 1 ]]; then
      reject_placeholder NILO_CONSOLE_PASSWORD
      reject_placeholder NILO_INGEST_API_KEY
      reject_placeholder MINIO_ROOT_PASSWORD
    fi
    ensure_minio
    compose run --rm --no-deps minio-init || true
    pull_datalake_image
    exec docker compose --env-file credentials.env up --remove-orphans
    ;;
  "")
    ;;
  *)
    echo "usage: ./deploy.sh [--ensure-minio | --install-systemd | --foreground | --stop]" >&2
    exit 2
    ;;
esac

if [[ "${NILO_ALLOW_DEV_SECRETS:-}" != 1 ]]; then
  reject_placeholder NILO_CONSOLE_PASSWORD
  reject_placeholder NILO_INGEST_API_KEY
  reject_placeholder MINIO_ROOT_PASSWORD
fi

port="$(env_value HOST_PORT)"
port="${port:-8088}"

if ! ensure_minio; then
  echo "MinIO did not start. The console was not started." >&2
  exit 1
fi
if ! compose run --rm --no-deps minio-init; then
  echo "MinIO is up, but the buckets were not created. Starting the console anyway." >&2
  compose logs --tail 30 minio-init >&2 || true
fi
if ! pull_datalake_image; then
  echo "The datalake image was not pulled. The console was not started." >&2
  exit 1
fi
compose up -d --pull always --force-recreate --remove-orphans datalake 2>/dev/null \
  || compose up -d --force-recreate --remove-orphans datalake

echo "Waiting for the console and the archive API on port ${port}..."
ok=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${port}/v1/health" >/dev/null \
    && curl -fsS --max-time 3 -o /dev/null "http://127.0.0.1:${port}/console" \
    && curl -fsS --max-time 3 -o /dev/null "http://127.0.0.1:${port}/console/static/app.js"; then
    ok=1
    break
  fi
  sleep 3
done

if [[ "$ok" != 1 ]]; then
  echo "The datalake container did not answer on port ${port}." >&2
  compose ps >&2 || true
  compose logs --tail 80 datalake >&2 || true
  exit 1
fi

console_build="$(curl -fsS --max-time 3 "http://127.0.0.1:${port}/console/api/version" 2>/dev/null | sed -n 's/.*"build"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)"
if curl -fsS --max-time 5 "http://127.0.0.1:${port}/console/static/app.js" 2>/dev/null | grep -q '"overview"'; then
  echo "Console UI: current (Overview dashboard)."
elif [[ -n "$console_build" && "$console_build" != "unknown" ]]; then
  echo "Console build ${console_build} — if the page looks old, hard-refresh the browser (Ctrl+Shift+R)." >&2
else
  echo "Console static files look outdated. Run ./deploy.sh again or hard-refresh (Ctrl+Shift+R)." >&2
fi

echo
echo "Stack is up."
echo "  Console  http://127.0.0.1:${port}/console"
echo "  API      http://127.0.0.1:${port}/v1/health"
echo "  MinIO    http://127.0.0.1:9001"
echo "           nilo-media = sessions, nilo-backups = database dump files"
echo
echo "The first start copies credentials.env into the datalake volume."
echo "Later edits belong in the web console. A new credentials.env is not copied over that file."
