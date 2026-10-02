#!/usr/bin/env bash
# Download the VM bundle from GitHub. Does not clone the application.
# credentials.env is left in place when it already exists.
set -euo pipefail

OWNER="NeoVisionsAI"
REPO="NILO-datalake"
REF="${NILO_DEPLOY_REF:-main}"
BASE="https://raw.githubusercontent.com/${OWNER}/${REPO}/${REF}/deploy/vm-ghcr"

if [[ -n "${NILO_DEPLOY_DIR:-}" ]]; then
  DIR="$NILO_DEPLOY_DIR"
elif [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  DIR="$PWD"
fi

mkdir -p "$DIR"
cd "$DIR"

fetch() {
  local name="$1"
  curl -fsSL "${BASE}/${name}" -o "${name}.partial"
  mv "${name}.partial" "$name"
}

FILES=(
  _lib.sh
  compose.yaml
  credentials.env.example
  configure.sh
  deploy.sh
  update.sh
  sync.sh
  run.sh
  README.md
  nilo-datalake-vm.service
)

for name in "${FILES[@]}"; do
  echo "fetch ${name}"
  fetch "$name"
done

if [[ ! -f credentials.env ]]; then
  cp credentials.env.example credentials.env
  chmod 600 credentials.env
  echo "Created credentials.env from the example. Run ./configure.sh before ./update.sh."
else
  echo "Kept existing credentials.env."
fi

fetch_bootstrap() {
  curl -fsSL "${BASE}/bootstrap.sh" -o bootstrap.sh.next
  bash -n bootstrap.sh.next
  mv bootstrap.sh.next bootstrap.sh
}

fetch_bootstrap
chmod 755 bootstrap.sh configure.sh deploy.sh update.sh sync.sh run.sh
chmod 644 _lib.sh 2>/dev/null || true

minio_password="$(grep -E '^MINIO_ROOT_PASSWORD=' credentials.env | tail -n 1 | cut -d= -f2- || true)"
case "$minio_password" in
  ""|CHANGE_ME|nilo-dev-key|nilo-secret|change-me)
    echo "MinIO is not installed yet. Run ./configure.sh, set secrets, save (s), then m or w."
    ;;
  *)
    ./deploy.sh --ensure-minio
    ;;
esac
cat <<EOF

Scripts updated in ${DIR}.

  ./configure.sh  — passwords; 8–9 only if the GHCR package is private
  ./deploy.sh     — start or refresh MinIO + archive + console
  ./update.sh     — bootstrap + deploy (use after each push to main)

EOF
