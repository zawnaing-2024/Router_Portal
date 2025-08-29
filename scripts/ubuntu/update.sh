#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/router-portal"

echo "=== Router Portal Update Script ==="
echo "Starting update process..."

cd "$APP_DIR"

echo "[pull] Pulling latest changes from GitHub..."
if ! sudo -u routerportal git pull --ff-only; then
    echo "ERROR: Git pull failed. Please check repository status."
    exit 1
fi

echo "[backup] Creating backup of current database..."
sudo -u routerportal cp instance/network_tools.db instance/network_tools.db.backup.$(date +%Y%m%d_%H%M%S) || true

echo "[deps] Updating Python dependencies..."
if ! sudo -u routerportal bash -lc ". $APP_DIR/.venv/bin/activate && pip install -r requirements.txt"; then
    echo "ERROR: Failed to install dependencies."
    exit 1
fi

echo "[migrations] Running database migrations..."
if ! sudo -u routerportal bash -lc "
cd '$APP_DIR'
. .venv/bin/activate
python3 -c 'from app import create_app; app = create_app(); print(\"Migrations completed successfully\")'
"; then
    echo "WARNING: Database migration may have failed. Check logs."
fi

echo "[restart] Restarting Router Portal service..."
if ! systemctl restart router-portal; then
    echo "ERROR: Failed to restart service."
    exit 1
fi

echo "[status] Checking service status..."
sleep 3
if systemctl is-active --quiet router-portal; then
    echo "✅ Update completed successfully!"
    echo ""
    echo "Service Status:"
    systemctl status router-portal --no-pager --lines=5
    echo ""
    echo "Recent Logs:"
    journalctl -u router-portal -n 10 --no-pager
else
    echo "❌ Service failed to start properly."
    echo "Check logs: sudo journalctl -u router-portal -f"
    exit 1
fi

echo ""
echo "=== Update Complete ==="
echo "Web Interface: http://YOUR_SERVER_IP/"
echo "Admin Panel: Available for super admins only"


