#!/usr/bin/env bash
# One-command deployment script for Router Portal
# Usage: curl -fsSL https://raw.githubusercontent.com/zawnaing-2024/Router_Portal/main/scripts/ubuntu/deploy.sh | sudo bash

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
REPO_URL="https://github.com/zawnaing-2024/Router_Portal.git"
APP_DIR="/opt/router-portal"
APP_USER="routerportal"

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root (sudo)"
    exit 1
fi

log_info "Starting Router Portal deployment..."
log_info "Repository: $REPO_URL"
log_info "Install Directory: $APP_DIR"

# Check Ubuntu version
if ! grep -q "Ubuntu 22" /etc/os-release; then
    log_warning "This script is optimized for Ubuntu 22.04. Continuing anyway..."
fi

# Install dependencies
log_info "Installing system dependencies..."
apt update
apt install -y python3.10-venv python3-pip git nginx curl wget

# Create application user
log_info "Creating application user: $APP_USER"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    adduser --system --group "$APP_USER" --no-create-home --shell /bin/bash
    log_success "User $APP_USER created"
else
    log_info "User $APP_USER already exists"
fi

# Create application directory
log_info "Creating application directory..."
mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

# Clone or update repository
log_info "Setting up application repository..."
sudo -u "$APP_USER" bash -c "
cd '$APP_DIR'
if [ ! -d .git ]; then
    git clone '$REPO_URL' .
    log_success 'Repository cloned'
else
    git fetch --all --prune
    git reset --hard origin/main
    log_success 'Repository updated'
fi
"

# Setup Python virtual environment
log_info "Setting up Python virtual environment..."
sudo -u "$APP_USER" bash -c "
cd '$APP_DIR'
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip
pip install gunicorn
pip install -r requirements.txt
"

# Create .env file if missing
if [ ! -f "$APP_DIR/.env" ]; then
    log_info "Creating .env file..."
    SECRET_KEY=\$(openssl rand -hex 32)
    echo "FLASK_SECRET_KEY=\$SECRET_KEY" > "$APP_DIR/.env"
    chown "$APP_USER":"$APP_USER" "$APP_DIR/.env"
    log_success ".env file created"
fi

# Create WSGI file
log_info "Creating WSGI entry point..."
cat > "$APP_DIR/wsgi.py" << 'WSGI_EOF'
from app import create_app
app = create_app()
WSGI_EOF
chown "$APP_USER":"$APP_USER" "$APP_DIR/wsgi.py"

# Create systemd service
log_info "Creating systemd service..."
cat > /etc/systemd/system/router-portal.service << SERVICE_EOF
[Unit]
Description=Router Portal (Flask + Gunicorn)
After=network.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=-$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/gunicorn --chdir $APP_DIR wsgi:app --bind 127.0.0.1:8000 --workers 2 --threads 4 --timeout 120
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Reload systemd and enable service
systemctl daemon-reload
systemctl enable router-portal

# Create Nginx configuration
log_info "Configuring Nginx..."
cat > /etc/nginx/sites-available/router-portal << NGINX_EOF
server {
    listen 80;
    server_name _;

    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
    }

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;
}
NGINX_EOF

# Enable site and disable default
ln -sf /etc/nginx/sites-available/router-portal /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Test Nginx configuration
nginx -t
if [ $? -eq 0 ]; then
    systemctl reload nginx
    log_success "Nginx configured"
else
    log_error "Nginx configuration test failed"
    exit 1
fi

# Run database migrations
log_info "Running database migrations..."
sudo -u "$APP_USER" bash -c "
cd '$APP_DIR'
. .venv/bin/activate
python3 -c 'from app import create_app; app = create_app(); print(\"Database migrations completed\")'
"

# Start service
log_info "Starting Router Portal service..."
systemctl restart router-portal

# Wait a moment and check status
sleep 5
if systemctl is-active --quiet router-portal; then
    log_success "Router Portal service started successfully!"
else
    log_error "Service failed to start. Check logs:"
    echo "sudo journalctl -u router-portal -f"
    exit 1
fi

# Get server IP
SERVER_IP=$(curl -s ifconfig.me || hostname -I | awk '{print $1}' || echo "YOUR_SERVER_IP")

# Success message
echo ""
echo "========================================"
echo -e "${GREEN}🎉 Router Portal Deployed Successfully!${NC}"
echo "========================================"
echo ""
echo -e "${BLUE}🌐 Web Interface:${NC} http://$SERVER_IP/"
echo -e "${BLUE}👤 Initial Setup:${NC} http://$SERVER_IP/init"
echo ""
echo -e "${YELLOW}📋 Next Steps:${NC}"
echo "1. Open http://$SERVER_IP/ in your browser"
echo "2. Go to /init to create your first super admin user"
echo "3. Log in and access Admin → Manage Companies"
echo "4. Configure Telegram settings for each company"
echo ""
echo -e "${BLUE}🔧 Useful Commands:${NC}"
echo "• Status:  sudo systemctl status router-portal"
echo "• Logs:    sudo journalctl -u router-portal -f"
echo "• Restart: sudo systemctl restart router-portal"
echo "• Update:  curl -fsSL https://raw.githubusercontent.com/zawnaing-2024/Router_Portal/main/scripts/ubuntu/update.sh | sudo bash"
echo ""
echo -e "${GREEN}📚 Documentation:${NC} https://github.com/zawnaing-2024/Router_Portal/blob/main/scripts/ubuntu/README.md"
echo ""
echo "========================================"
