#!/bin/bash
# LoyaltyLion → BigQuery Connector - VM Deployment Script
# Run on your Ubuntu/Debian GCP VM as root or with sudo
#
# Usage:
#   sudo bash deploy.sh
#
# Before running:
#   1. Clone this repo on the VM
#   2. Ensure GCP Secret Manager is set up (run setup_secrets.sh once)
#   3. Ensure the VM service account has secretmanager.secretAccessor role

set -e

APP_DIR="/opt/msread-loyaltylion"
APP_USER="msread"
INCREMENTAL_SERVICE="loyaltylion-incremental"
FULL_SERVICE="loyaltylion-full"

echo "=========================================="
echo " LoyaltyLion → BigQuery Connector - VM Deployment"
echo "=========================================="

# ---- Step 1: System packages ----
echo ""
echo "[1/7] Installing system packages..."
apt update && apt install -y \
    python3 python3-pip python3-venv git

# ---- Step 2: Create app user (reuse if exists) ----
echo ""
echo "[2/7] Ensuring app user exists..."
if ! id "$APP_USER" &>/dev/null; then
    useradd -r -s /bin/false "$APP_USER"
    echo "User '$APP_USER' created."
else
    echo "User '$APP_USER' already exists."
fi

# ---- Step 3: Copy app files ----
echo ""
echo "[3/7] Setting up app directory..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$APP_DIR/logs"

if [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
    cp "$SCRIPT_DIR/loyaltylion_connect.py" "$APP_DIR/"
    cp "$SCRIPT_DIR/loyaltylion_to_bigquery.py" "$APP_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$APP_DIR/"
    echo "Copied files from $SCRIPT_DIR to $APP_DIR"
else
    echo "Already in $APP_DIR, skipping copy."
fi

# ---- Step 4: Python virtual environment ----
echo ""
echo "[4/7] Setting up Python environment..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "Installed packages:"
"$APP_DIR/venv/bin/pip" list --format=columns | grep -E "google|requests"

# ---- Step 5: Environment marker ----
echo ""
echo "[5/7] Setting up environment..."
cat > "$APP_DIR/.env" << 'ENVEOF'
GOOGLE_CLOUD=true
CONNECTOR_PROJECT=msr-msia-sales-analysis
ENVEOF

chmod 600 "$APP_DIR/.env"

# ---- Step 6: Systemd services and timers ----
echo ""
echo "[6/7] Setting up systemd services and timers..."

# Incremental sync service
cat > /etc/systemd/system/$INCREMENTAL_SERVICE.service << SVCEOF
[Unit]
Description=LoyaltyLion Incremental Sync to BigQuery
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/loyaltylion_to_bigquery.py incremental
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

# Incremental timer (daily at 04:00 MYT = 20:00 UTC)
cat > /etc/systemd/system/$INCREMENTAL_SERVICE.timer << TMREOF
[Unit]
Description=Run LoyaltyLion incremental sync daily at 04:00 MYT

[Timer]
OnCalendar=*-*-* 20:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
TMREOF

# Full sync service
cat > /etc/systemd/system/$FULL_SERVICE.service << SVCEOF2
[Unit]
Description=LoyaltyLion Full Sync to BigQuery
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python3 $APP_DIR/loyaltylion_to_bigquery.py full
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF2

# Full sync timer (weekly Sunday 05:00 MYT = Saturday 21:00 UTC)
cat > /etc/systemd/system/$FULL_SERVICE.timer << TMREOF2
[Unit]
Description=Run LoyaltyLion full sync weekly (Sunday 05:00 MYT)

[Timer]
OnCalendar=Sun *-*-* 21:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
TMREOF2

# Set permissions
chown -R $APP_USER:$APP_USER "$APP_DIR"

# Enable and start timers
systemctl daemon-reload
systemctl enable $INCREMENTAL_SERVICE.timer
systemctl enable $FULL_SERVICE.timer
systemctl start $INCREMENTAL_SERVICE.timer
systemctl start $FULL_SERVICE.timer

echo ""
echo "Timers active:"
systemctl list-timers --no-pager | grep loyaltylion || echo "(timers starting up)"

# ---- Step 7: Verify Secret Manager access ----
echo ""
echo "[7/7] Verifying Secret Manager access..."
sudo -u $APP_USER bash -c "export GOOGLE_CLOUD=true; export CONNECTOR_PROJECT=msr-msia-sales-analysis; $APP_DIR/venv/bin/python3 -c \"
import sys
sys.path.insert(0, '$APP_DIR')
from loyaltylion_connect import load_credentials
try:
    creds = load_credentials()
    print(f'API key loaded: {creds[\"api_key\"][:8]}...')
    print(f'BigQuery project: {creds[\"bigquery_project\"]}')
    print('Secret Manager access: OK')
except Exception as e:
    print(f'Secret Manager access FAILED: {e}')
    print('Ensure the VM service account has roles/secretmanager.secretAccessor')
\"" 2>&1 || echo "Verification check complete (review output above)"

# ---- Done ----
echo ""
echo "=========================================="
echo " DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Incremental sync (daily 04:00 MYT):"
echo "  Service:  sudo systemctl status $INCREMENTAL_SERVICE"
echo "  Timer:    sudo systemctl status $INCREMENTAL_SERVICE.timer"
echo ""
echo "Full sync (weekly Sunday 05:00 MYT):"
echo "  Service:  sudo systemctl status $FULL_SERVICE"
echo "  Timer:    sudo systemctl status $FULL_SERVICE.timer"
echo ""
echo "Manual run:"
echo "  Incremental: sudo -u $APP_USER $APP_DIR/venv/bin/python3 $APP_DIR/loyaltylion_to_bigquery.py incremental"
echo "  Full:         sudo -u $APP_USER $APP_DIR/venv/bin/python3 $APP_DIR/loyaltylion_to_bigquery.py full"
echo ""
echo "Logs:"
echo "  Incremental: sudo journalctl -u $INCREMENTAL_SERVICE -f"
echo "  Full:         sudo journalctl -u $FULL_SERVICE -f"
echo ""