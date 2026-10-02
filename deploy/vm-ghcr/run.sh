#!/usr/bin/env bash
# Alias of ./deploy.sh.
set -euo pipefail
cd "$(dirname "$0")"
exec ./deploy.sh "$@"
