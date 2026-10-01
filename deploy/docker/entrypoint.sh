#!/bin/sh
# The image starts as root so the data volume can be chowned, then drops to nilo.
set -eu

mkdir -p /data/catalog /data/traces /data/archive /data/inbox /data/staging /data/quarantine /data/config
chown -R nilo:nilo /data
exec gosu nilo "$@"
