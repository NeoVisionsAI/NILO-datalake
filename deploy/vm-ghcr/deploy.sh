#!/usr/bin/env bash
# Pull the published images and start the archive, the console, MongoDB, and MinIO.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f credentials.env ]]; then
  echo "credentials.env is missing. Run ./configure.sh or copy credentials.env.example." >&2
  exit 1
fi

env_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" credentials.env | tail -n 1 || true)"
  printf '%s' "${line#"${key}"=}"
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
      echo "$key contains @ : / ? or #. Use letters, digits, dot, underscore, or hyphen so the MongoDB URI stays valid." >&2
      exit 1
      ;;
  esac
}

compose() {
  docker compose --env-file credentials.env "$@"
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
      reject_placeholder MONGO_PASSWORD
      reject_placeholder MINIO_ROOT_PASSWORD
    fi
    compose pull
    exec docker compose --env-file credentials.env up --remove-orphans
    ;;
  "")
    ;;
  *)
    echo "usage: ./deploy.sh [--install-systemd | --foreground | --stop]" >&2
    exit 2
    ;;
esac

if [[ "${NILO_ALLOW_DEV_SECRETS:-}" != 1 ]]; then
  reject_placeholder NILO_CONSOLE_PASSWORD
  reject_placeholder NILO_INGEST_API_KEY
  reject_placeholder MONGO_PASSWORD
  reject_placeholder MINIO_ROOT_PASSWORD
fi

port="$(env_value HOST_PORT)"
port="${port:-8088}"

compose pull
compose up -d --remove-orphans --wait

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
  compose logs --tail 50 datalake >&2 || true
  exit 1
fi

echo
echo "Stack is up."
echo "  Console  http://127.0.0.1:${port}/console"
echo "  API      http://127.0.0.1:${port}/v1/health"
echo "  MinIO    http://127.0.0.1:9001"
echo "  MongoDB  127.0.0.1:27017"
echo
echo "The first start copies credentials.env into the datalake volume."
echo "Later edits belong in the web console. A new credentials.env is not copied over that file."
