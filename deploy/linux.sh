#!/usr/bin/env bash
# Installs/updates this project only. Never copies or starts a local trading book.
set -Eeuo pipefail
umask 077
REPOSITORY="https://github.com/Roco211/GATE-CFD.git"
INSTALL_DIR="${GRID_INSTALL_DIR:-/opt/gate-cfd}"
PUBLIC_HOST=""
LOCAL_ONLY=0
while (($#)); do
  case "$1" in
    --host) PUBLIC_HOST="${2:?--host requires a public IPv4 address or domain}"; shift 2 ;;
    --local) LOCAL_ONLY=1; shift ;;
    --help) echo 'sudo bash deploy/linux.sh [--host PUBLIC_IP_OR_DOMAIN | --local]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done
[[ $EUID == 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
[[ "$INSTALL_DIR" == /opt/* && "$INSTALL_DIR" != *..* && "$INSTALL_DIR" != *$'\n'* ]] || { echo 'GRID_INSTALL_DIR must be a directory below /opt.' >&2; exit 1; }
if [[ -n "$PUBLIC_HOST" ]]; then
  [[ "$PUBLIC_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ && "$PUBLIC_HOST" != *..* ]] || { echo 'Invalid public host.' >&2; exit 1; }
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  . /etc/os-release
  [[ "$ID" == ubuntu || "$ID" == debian ]] || { echo 'Automatic Docker installation supports Ubuntu/Debian. Install Docker Compose first on other distributions.' >&2; exit 1; }
  apt-get update
  apt-get install -y ca-certificates curl git
  install -m 0755 -d /etc/apt/keyrings
  curl --fail --silent --show-error --location "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/%s %s stable\n' "$(dpkg --print-architecture)" "$ID" "${UBUNTU_CODENAME:-$VERSION_CODENAME}" > /etc/apt/sources.list.d/docker.list
  apt-get update
  if command -v docker >/dev/null; then
    apt-get install -y docker-compose-plugin
  else
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
fi
command -v git >/dev/null || { apt-get update; apt-get install -y git; }
systemctl enable --now docker
umask 022
if [[ -d "$INSTALL_DIR/.git" ]]; then
  remote=$(git -C "$INSTALL_DIR" remote get-url origin)
  [[ "$remote" == "$REPOSITORY" || "$remote" == git@github.com:Roco211/GATE-CFD.git ]] || { echo 'Existing directory belongs to another repository.' >&2; exit 1; }
  [[ -z "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)" ]] || { echo 'Tracked files have local changes; commit or resolve them before upgrading.' >&2; exit 1; }
  git -C "$INSTALL_DIR" pull --ff-only
else
  [[ ! -e "$INSTALL_DIR" ]] || { echo 'Install directory exists and is not this repository.' >&2; exit 1; }
  git clone "$REPOSITORY" "$INSTALL_DIR"
fi
umask 077
cd "$INSTALL_DIR"
if [[ -z "$PUBLIC_HOST" && $LOCAL_ONLY == 0 && ! -f .env.deploy ]]; then
  read -r -p 'Public IPv4/domain for HTTPS (Enter for SSH-tunnel-only access): ' PUBLIC_HOST </dev/tty
  [[ -z "$PUBLIC_HOST" || "$PUBLIC_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}$ ]] || { echo 'Invalid public host.' >&2; exit 1; }
fi
if [[ -n "$PUBLIC_HOST" ]]; then
  printf 'GRID_PUBLIC_HOST=%s\nCOMPOSE_PROFILES=https\n' "$PUBLIC_HOST" > .env.deploy
elif [[ ! -f .env.deploy || $LOCAL_ONLY == 1 ]]; then
  printf 'GRID_PUBLIC_HOST=localhost\nCOMPOSE_PROFILES=\n' > .env.deploy
fi
docker compose --env-file .env.deploy build grid
docker compose --env-file .env.deploy up -d --remove-orphans --wait --wait-timeout 120
if grep -q '^COMPOSE_PROFILES=https$' .env.deploy; then
  PUBLIC_HOST=$(sed -n 's/^GRID_PUBLIC_HOST=//p' .env.deploy)
  # Git may replace the bind-mounted Caddyfile inode. Recreate only the proxy
  # so its configuration is refreshed even when the Compose definition is unchanged.
  docker compose --env-file .env.deploy up -d --no-deps --force-recreate https
  docker compose --env-file .env.deploy exec -T grid python deploy/check_https.py "$PUBLIC_HOST"
  echo 'Grid Studio installed. Persistent data and encryption keys are stored in Docker volumes.'
  echo "Open https://$PUBLIC_HOST/ and enter your configured access token."
  echo 'Certificate issuance requires inbound TCP 80 and 443. Do not bypass certificate errors; inspect: docker compose --env-file .env.deploy logs https'
else
  echo 'Grid Studio installed. Persistent data and encryption keys are stored in Docker volumes.'
  echo "Only 127.0.0.1:${GRID_PORT:-18473} is exposed. Forward it through SSH to access the console."
fi
echo 'Configure Gate API credentials after login. No trading credentials or previous strategies were imported.'
