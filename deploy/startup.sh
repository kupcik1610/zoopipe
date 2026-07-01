#!/usr/bin/env bash
# GCE startup script: stands up zoopipe behind Caddy (automatic HTTPS) as a
# systemd service. Runs as root on every boot; written to be idempotent so a
# reboot just fast-forwards to the latest code + deps.
set -euxo pipefail

APP_USER=zoopipe
APP_HOME=/opt/zoopipe
REPO=https://github.com/kupcik1610/zoopipe.git
BRANCH=deploy

META="http://metadata.google.internal/computeMetadata/v1"
metadata() { curl -s -H "Metadata-Flavor: Google" "$META/$1"; }

APP_KEY=$(metadata instance/attributes/app-key)
DOMAIN=$(metadata instance/attributes/domain)

id -u "$APP_USER" &>/dev/null || useradd -r -m -d "$APP_HOME" -s /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_HOME"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl ca-certificates gnupg apt-transport-https debian-keyring debian-archive-keyring

# --- app code ---------------------------------------------------------------
if [ -d "$APP_HOME/app/.git" ]; then
  git -C "$APP_HOME/app" fetch --depth 1 origin "$BRANCH"
  git -C "$APP_HOME/app" reset --hard "origin/$BRANCH"
else
  git clone --depth 1 -b "$BRANCH" "$REPO" "$APP_HOME/app"
fi

# --- private python + deps via uv (into the app folder, like the desktop app) -
export HOME="$APP_HOME" UV_INSTALL_DIR="$APP_HOME/bin" UV_CACHE_DIR="$APP_HOME/cache"
UV="$APP_HOME/bin/uv"
[ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d "$APP_HOME/venv" ] || "$UV" venv "$APP_HOME/venv" --python 3.12
"$UV" pip install --python "$APP_HOME/venv/bin/python" -r "$APP_HOME/app/requirements.txt"

mkdir -p "$APP_HOME/models"
chown -R "$APP_USER:$APP_USER" "$APP_HOME"

# --- systemd service --------------------------------------------------------
cat >/etc/systemd/system/zoopipe.service <<EOF
[Unit]
Description=zoopipe fish-catalogue app
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_HOME/app
Environment=HOST=127.0.0.1
Environment=PORT=5001
Environment=OPEN_BROWSER=0
Environment=U2NET_HOME=$APP_HOME/models
Environment=APP_KEY=$APP_KEY
ExecStart=$APP_HOME/venv/bin/python $APP_HOME/app/run_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now zoopipe
systemctl restart zoopipe

# --- Caddy: automatic HTTPS reverse proxy -----------------------------------
if ! command -v caddy &>/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

cat >/etc/caddy/Caddyfile <<EOF
$DOMAIN {
    reverse_proxy 127.0.0.1:5001
}
EOF
systemctl restart caddy
