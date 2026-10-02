#!/usr/bin/env bash
# Alias of ./update.sh.
set -euo pipefail
cd "$(dirname "$0")"
exec ./update.sh "$@"
