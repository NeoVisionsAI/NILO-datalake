#!/usr/bin/env bash
# After each push: refresh scripts from GitHub and redeploy (same as bootstrap + deploy).
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

case "${1:-all}" in
  all)
    ./bootstrap.sh
    ./deploy.sh
    ;;
  image)
    ./deploy.sh
    ;;
  scripts)
    ./bootstrap.sh
    ;;
  *)
    echo "usage: ./update.sh [image|scripts]" >&2
    exit 2
    ;;
esac
