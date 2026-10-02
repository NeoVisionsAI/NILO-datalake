#!/usr/bin/env bash
# After each push: refresh scripts from GitHub and redeploy (same as bootstrap + deploy).
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck source=_lib.sh
source "$(dirname "$0")/_lib.sh"
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
