#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/router-portal"

cd "$APP_DIR"
echo "[pull]"; sudo -u routerportal git pull --ff-only
echo "[deps]"; sudo -u routerportal bash -lc ". $APP_DIR/.venv/bin/activate && pip install -r requirements.txt"
echo "[restart]"; systemctl restart router-portal
echo "OK"


