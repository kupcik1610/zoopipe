#!/usr/bin/env bash
# GCE startup script: stands up zoopipe behind Caddy (automatic HTTPS) as a
# systemd service. Runs as root on every boot. Written to be idempotent AND
# fault-tolerant: each section is best-effort, so a failure in one (e.g. a git
# fetch when the branch has moved) can never stop the others (e.g. Caddy) from
# coming up. The app code already lives on the disk, so git is a nice-to-have.
set -ux   # NOTE: deliberately no -e / -o pipefail -- see above.

APP_USER=zoopipe
APP_HOME=/opt/zoopipe
REPO=https://github.com/kupcik1610/zoopipe.git
BRANCH=main

META="http://metadata.google.internal/computeMetadata/v1"
metadata() { curl -s -H "Metadata-Flavor: Google" "$META/$1"; }

APP_KEY=$(metadata instance/attributes/app-key)
DOMAIN=$(metadata instance/attributes/domain)

id -u "$APP_USER" &>/dev/null || useradd -r -m -d "$APP_HOME" -s /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_HOME"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y || true
apt-get install -y git curl ca-certificates gnupg apt-transport-https debian-keyring debian-archive-keyring || true

# --- app code (best-effort; disk copy is the source of truth) ----------------
git config --global --add safe.directory "$APP_HOME/app" || true
if [ -d "$APP_HOME/app/.git" ]; then
  git -C "$APP_HOME/app" fetch --depth 1 origin "$BRANCH" && \
    git -C "$APP_HOME/app" reset --hard "origin/$BRANCH" || \
    echo "WARN: git update failed, keeping on-disk code"
else
  git clone --depth 1 -b "$BRANCH" "$REPO" "$APP_HOME/app" || \
    echo "WARN: git clone failed"
fi

# --- private python + deps via uv (into the app folder) ----------------------
export HOME="$APP_HOME" UV_INSTALL_DIR="$APP_HOME/bin" UV_CACHE_DIR="$APP_HOME/cache"
UV="$APP_HOME/bin/uv"
[ -x "$UV" ] || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d "$APP_HOME/venv" ] || "$UV" venv "$APP_HOME/venv" --python 3.12
"$UV" pip install --python "$APP_HOME/venv/bin/python" -r "$APP_HOME/app/requirements.txt" || true

mkdir -p "$APP_HOME/models"
chown -R "$APP_USER:$APP_USER" "$APP_HOME"

# --- systemd service ---------------------------------------------------------
cat >/etc/systemd/system/zoopipe.service <<SVCEOF
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
SVCEOF
systemctl daemon-reload
systemctl enable --now zoopipe
systemctl restart zoopipe

# --- Caddy: automatic HTTPS reverse proxy (always ensured) -------------------
if ! command -v caddy &>/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

mkdir -p /etc/caddy
cat >/etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
    reverse_proxy 127.0.0.1:5001
}
CADDYEOF
systemctl enable caddy
systemctl restart caddy

# --- rclone: one-way sync of out/ -> Google Drive ----------------------------
# The Drive OAuth token lives on the disk at /root/.config/rclone/rclone.conf
# (created once, persists across reboots). We (re)install the rclone binary and
# ensure the sync timer; if the config is missing the timer just no-ops.
if ! command -v rclone &>/dev/null; then
  curl -sL https://downloads.rclone.org/rclone-current-linux-amd64.zip -o /tmp/rclone.zip
  apt-get install -y unzip || true
  unzip -o -q /tmp/rclone.zip -d /tmp/rclone-dl
  cp /tmp/rclone-dl/rclone-*-linux-amd64/rclone /usr/local/bin/
  chmod +x /usr/local/bin/rclone
fi

cat >/etc/systemd/system/zoopipe-sync.service <<'SYNCEOF'
[Unit]
Description=Sync zoopipe out/ to Google Drive
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rclone copy /opt/zoopipe/app/out gdrive:zoopipe-fish --create-empty-src-dirs --transfers=8 --checkers=16
SYNCEOF

cat >/etc/systemd/system/zoopipe-sync.timer <<'SYNCTEOF'
[Unit]
Description=Run zoopipe Drive sync every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60
Unit=zoopipe-sync.service

[Install]
WantedBy=timers.target
SYNCTEOF
systemctl daemon-reload
systemctl enable --now zoopipe-sync.timer
