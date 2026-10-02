# Shared helpers for VM deploy scripts. Source from the vm-ghcr directory.
# shellcheck shell=bash

nilo_env_value() {
  local key="$1"
  local file="${2:-credentials.env}"
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  printf '%s' "${line#"${key}"=}"
}

# Docker group applies only to new logins; re-exec this script with sg when needed.
nilo_ensure_docker_session() {
  local script="$1"
  shift

  if docker info >/dev/null 2>&1; then
    return 0
  fi
  if [[ ! -S /var/run/docker.sock ]]; then
    return 0
  fi
  if [[ "${NILO_DOCKER_REEXEC:-}" == 1 ]]; then
    echo "This shell still cannot use Docker. Log out and SSH in again, or ask an admin to fix the socket." >&2
    return 1
  fi
  if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
    echo "Adding ${USER} to the docker group..."
    sudo usermod -aG docker "$USER"
  fi
  if id -nG "$USER" | tr ' ' '\n' | grep -qx docker && command -v sg >/dev/null 2>&1; then
    echo "Applying the docker group for this run..."
    local quoted_args=""
    local arg
    for arg in "$@"; do
      quoted_args+=$(printf '%q ' "$arg")
    done
    exec sg docker -c "NILO_DOCKER_REEXEC=1 exec $(printf '%q' "$script") ${quoted_args}"
  fi
  return 1
}

nilo_ensure_ghcr_login() {
  local cred="${1:-credentials.env}"
  local user token
  if [[ ! -f "$cred" ]]; then
    return 0
  fi
  user="$(nilo_env_value GHCR_USER "$cred")"
  token="$(nilo_env_value GHCR_TOKEN "$cred")"
  if [[ -z "$user" || -z "$token" ]]; then
    return 0
  fi
  if echo "$token" | docker login ghcr.io -u "$user" --password-stdin >/dev/null 2>&1; then
    return 0
  fi
  echo "GHCR login failed for ${user}. In ./configure.sh set items 8 and 9 (token needs read:packages)." >&2
  return 1
}

nilo_explain_ghcr_unauthorized() {
  local image="$1"
  cat >&2 <<EOF
GHCR refused to pull ${image} (unauthorized).

Fix one of these, then run ./deploy.sh or ./update.sh again:

  1) ./configure.sh — set GitHub user (8) and token (9), save, then ./deploy.sh

  2) GitHub → Packages → nilo-datalake → Package settings → Public

  3) Confirm the publish workflow on main succeeded:
     https://github.com/NeoVisionsAI/NILO-datalake/actions
EOF
}
