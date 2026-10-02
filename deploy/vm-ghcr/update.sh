#!/usr/bin/env bash
# One command after each push: refresh scripts and pull the GHCR image.
set -euo pipefail

cd "$(dirname "$0")"

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
