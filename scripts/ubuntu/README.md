# Router Portal - Ubuntu Deployment

Complete multi-tenant network monitoring portal with enhanced alerting system.

## 🚀 Quick Start

### First Time Installation
```bash
# Download and run bootstrap script
curl -fsSL https://raw.githubusercontent.com/zawnaing-2024/Router_Portal/main/scripts/ubuntu/bootstrap.sh | sudo bash
```

### Update Existing Installation
```bash
# Run update script
curl -fsSL https://raw.githubusercontent.com/zawnaing-2024/Router_Portal/main/scripts/ubuntu/update.sh | sudo bash
```

## 📋 Features

### 🔐 Multi-Tenancy
- **Company Isolation**: Each company has separate data and alerts
- **User Management**: Role-based access (Super Admin, Company Admin, Editor, Viewer)
- **Company-Specific Settings**: Individual Telegram bots per company

### 🔔 Enhanced Alerting
- **Down Alerts**: Immediate notification when services go down
- **Restoration Alerts**: Automatic alerts when services come back up
- **Continuous Reminders**: 30-minute reminders while services remain down
- **Device Names**: All alerts include device identification
- **Duration Tracking**: Shows outage duration in restoration alerts

### 📊 Monitoring
- **Ping Monitoring**: Real-time latency monitoring with thresholds
- **Fiber Monitoring**: Optical power and link status monitoring
- **Resource Monitoring**: CPU, RAM, and storage usage tracking
- **Backup Management**: Automated router configuration backups

## 🛠️ Manual Installation

### Prerequisites
- Ubuntu 22.04 LTS
- Root or sudo access
- Internet connection

### Step 1: Bootstrap Installation
```bash
# Download bootstrap script
wget https://raw.githubusercontent.com/zawnaing-2024/Router_Portal/main/scripts/ubuntu/bootstrap.sh
chmod +x bootstrap.sh

# Run bootstrap (will prompt for sudo if needed)
sudo ./bootstrap.sh
```

### Step 2: Initial Setup
1. Open http://YOUR_SERVER_IP/ in your browser
2. Go to `/init` to create the first super admin user
3. Log in with the super admin account
4. Access Admin panel to create companies and users

### Step 3: Configure Telegram (Per Company)
1. Go to **Admin → Manage Companies**
2. Click **"Telegram Settings"** for each company
3. Configure bot token and chat ID
4. Test the configuration
5. Enable desired alert types

## 🔧 Configuration

### Environment Variables
Create `/opt/router-portal/.env`:
```bash
FLASK_SECRET_KEY=your_random_secret_key_here
```

### Telegram Setup (Per Company)
1. **Create Bot**: Message @BotFather on Telegram
2. **Get Token**: Copy the API token
3. **Create Group**: Create a Telegram group for alerts
4. **Add Bot**: Add your bot as administrator to the group
5. **Get Chat ID**: Use "Detect Chat ID" in the web interface or:
   ```bash
   curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates"
   ```

## 📊 Alert Types

| Alert Type | Trigger | Frequency | Device Name |
|------------|---------|-----------|-------------|
| **Ping Down** | 5+ consecutive failures | Immediate + 30min reminders | ✅ Yes |
| **Fiber Down** | Interface status down | Immediate + 30min reminders | ✅ Yes |
| **High Ping** | Exceeds threshold (default: 90ms) | Immediate | ✅ Yes |
| **Restoration** | Service comes back up | One-time | ✅ Yes |

## 🛠️ Management Commands

### Check Status
```bash
sudo systemctl status router-portal
sudo journalctl -u router-portal -f
```

### Restart Service
```bash
sudo systemctl restart router-portal
```

### View Logs
```bash
# Last 50 lines
sudo journalctl -u router-portal -n 50

# Follow logs in real-time
sudo journalctl -u router-portal -f
```

### Backup Database
```bash
cd /opt/router-portal
sudo -u routerportal cp instance/network_tools.db instance/backup_$(date +%Y%m%d_%H%M%S).db
```

## 🔍 Troubleshooting

### Service Won't Start
```bash
# Check service status
sudo systemctl status router-portal

# Check logs
sudo journalctl -u router-portal -n 100

# Check Python environment
sudo -u routerportal bash -c "cd /opt/router-portal && . .venv/bin/activate && python3 -c 'from app import create_app; print(\"OK\")'"
```

### Database Issues
```bash
# Backup current database
sudo -u routerportal cp /opt/router-portal/instance/network_tools.db /opt/router-portal/instance/backup.db

# Reset database (WARNING: This deletes all data)
sudo -u routerportal rm /opt/router-portal/instance/network_tools.db
sudo systemctl restart router-portal
```

### Permission Issues
```bash
# Fix ownership
sudo chown -R routerportal:routerportal /opt/router-portal

# Restart service
sudo systemctl restart router-portal
```

## 🔒 Security

- **Firewall**: Only port 80/443 open by default
- **User Isolation**: Company data completely separated
- **Admin Access**: Super admin only for system configuration
- **Session Security**: Secure session management with Flask-Login

## 📈 Performance

- **Memory**: ~200MB RAM usage
- **CPU**: Minimal background monitoring
- **Storage**: ~10MB for application + database size
- **Network**: Low bandwidth for monitoring

## 🆘 Support

### Common Issues

**"Permission denied" errors:**
```bash
sudo chown -R routerportal:routerportal /opt/router-portal
```

**"Module not found" errors:**
```bash
cd /opt/router-portal
sudo -u routerportal bash -c ". .venv/bin/activate && pip install -r requirements.txt"
```

**Database locked errors:**
```bash
sudo systemctl restart router-portal
```

### Logs Location
- **Application Logs**: `sudo journalctl -u router-portal -f`
- **Gunicorn Logs**: Included in systemd journal
- **Database**: `/opt/router-portal/instance/network_tools.db`

## 📝 Changelog

### Latest Features (v2.0)
- ✅ **Multi-tenant architecture** with company isolation
- ✅ **Company-specific Telegram alerting**
- ✅ **Enhanced restoration alerts** with duration tracking
- ✅ **Continuous down alerts** (30-minute reminders)
- ✅ **Device edit functionality** for all resources
- ✅ **Admin panel** for company and user management
- ✅ **Improved timezone handling** for consistent timestamps

---

**Router Portal** - Enterprise-grade network monitoring with intelligent alerting.
Developed by Zaw Naing Htun (Network Engineer)
