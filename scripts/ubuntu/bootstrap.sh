#!/usr/bin/env bash
set -euo pipefail

# One-time bootstrap for Ubuntu 22.04 to install and run Router Portal in production
# - Creates system user `routerportal`
# - Clones repo into /opt/router-portal
# - Creates Python venv, installs deps + gunicorn
# - Writes wsgi.py, systemd unit, and nginx site
# - Starts service behind nginx on http://SERVER_IP/

REPO_URL="https://github.com/zawnaing-2024/Router_Portal.git"
APP_USER="routerportal"
APP_DIR="/opt/router-portal"

echo "[packages] installing base packages"
apt update
apt install -y python3.10-venv python3-pip git nginx

echo "[user] ensuring app user and directory"
id -u "$APP_USER" >/dev/null 2>&1 || adduser --system --group "$APP_USER"
mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "[repo] cloning or updating repo"
sudo -u "$APP_USER" bash -lc "
set -e
cd '$APP_DIR'
if [ ! -d .git ]; then
  git clone '$REPO_URL' .
else
  git fetch --all --prune || true
  git reset --hard origin/HEAD || true
fi
"

echo "[venv] creating venv and installing dependencies"
sudo -u "$APP_USER" bash -lc "
cd '$APP_DIR'
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install gunicorn
pip install -r requirements.txt
"

echo "[env] creating minimal .env if missing (Telegram set in-portal)"
if [ ! -f "$APP_DIR/.env" ]; then
  openssl rand -hex 32 | awk '{print "FLASK_SECRET_KEY=" $1}' > "$APP_DIR/.env"
  chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
fi

echo "[wsgi] creating wsgi.py"
cat >"$APP_DIR/wsgi.py" <<'WSGI'
from app import create_app
app = create_app()
WSGI
chown "$APP_USER":"$APP_USER" "$APP_DIR/wsgi.py"

echo "[systemd] writing service unit"
cat >/etc/systemd/system/router-portal.service <<'UNIT'
[Unit]
Description=Router Portal (Flask + Gunicorn)
After=network.target

[Service]
User=routerportal
Group=routerportal
WorkingDirectory=/opt/router-portal
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=-/opt/router-portal/.env
ExecStart=/opt/router-portal/.venv/bin/gunicorn --chdir /opt/router-portal wsgi:app --bind 127.0.0.1:8000 --workers 1 --threads 4 --timeout 120
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable router-portal
systemctl restart router-portal

echo "[nginx] writing reverse proxy site"
cat >/etc/nginx/sites-available/router-portal <<'NGX'
server {
    listen 80;
    server_name _;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host               $host;
        proxy_set_header X-Real-IP          $remote_addr;
        proxy_set_header X-Forwarded-For    $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto  $scheme;
        proxy_read_timeout 300;
    }
}
NGX

ln -sf /etc/nginx/sites-available/router-portal /etc/nginx/sites-enabled/router-portal
rm -f /etc/nginx/sites-enabled/default || true
nginx -t
systemctl reload nginx

echo "[done] Open http://SERVER_IP/ and set Telegram in Settings."

