#!/bin/bash
# LoyaltyLion → BigQuery Connector - VM Deployment Script
# Run on the GCP VM (loyaltylion) as the msrit_msread user
#
# Usage:
#   bash deploy_loyaltylion.sh
#
# This script:
#   1. Clones the repo to ~/loyaltylion
#   2. Creates symlink at /opt/msread-loyaltylion
#   3. Sets up Python venv and installs dependencies
#   4. Installs cron for scheduled syncs (8am + 2pm MYT)
#   5. Creates systemd services/timers as alternative scheduling

set -e

REPO_URL="https://github.com/msritmsread-collab/loyaltylion.git"
APP_DIR="/opt/msread-loyaltylion"
GIT_DIR="$HOME/loyaltylion"
LOG_DIR="$GIT_DIR/logs"

echo "=========================================="
echo " LoyaltyLion → BigQuery Connector - VM Deployment"
echo "=========================================="

# ---- Step 1: Clone / update repo ----
echo ""
echo "[1/7] Setting up git repo..."
if [ -d "$GIT_DIR" ]; then
    echo "Repo exists, pulling latest..."
    cd "$GIT_DIR" && git pull
else
    echo "Cloning repo..."
    git clone "$REPO_URL" "$GIT_DIR"
fi

# ---- Step 2: Symlink ----
echo ""
echo "[2/7] Setting up symlink..."
sudo rm -f "$APP_DIR"
sudo ln -s "$GIT_DIR" "$APP_DIR"
echo "Symlink: $APP_DIR -> $GIT_DIR"

# ---- Step 3: Copy credentials (if in /tmp) ----
echo ""
echo "[3/7] Checking credentials..."
for f in loyaltylion_credentials.json bigquery_credentials.json loyaltylion_sync_state.json; do
    if [ -f "/tmp/$f" ] && [ ! -f "$GIT_DIR/$f" ]; then
        cp "/tmp/$f" "$GIT_DIR/$f"
        echo "Copied $f from /tmp"
    fi
done

# Update bigquery_credentials_path to match new location
if [ -f "$GIT_DIR/loyaltylion_credentials.json" ]; then
    sed -i 's|/opt/msread-loyaltylion/bigquery_credentials.json|/home/msrit_msread/loyaltylion/bigquery_credentials.json|' \
        "$GIT_DIR/loyaltylion_credentials.json"
    echo "Updated bigquery_credentials_path in loyaltylion_credentials.json"
fi

# ---- Step 4: Python virtual environment ----
echo ""
echo "[4/7] Setting up Python environment..."
python3 -m venv "$GIT_DIR/venv"
"$GIT_DIR/venv/bin/pip" install --upgrade pip
"$GIT_DIR/venv/bin/pip" install -r "$GIT_DIR/requirements.txt"

echo "Installed packages:"
"$GIT_DIR/venv/bin/pip" list --format=columns | grep -E "google|requests" || true

# ---- Step 5: Log directory ----
echo ""
echo "[5/7] Creating log directory..."
mkdir -p "$LOG_DIR"

# ---- Step 6: Crontab (8am + 2pm MYT = 00:00 + 06:00 UTC) ----
echo ""
echo "[6/7] Setting up crontab..."
PYTHON="$GIT_DIR/venv/bin/python3"
SCRIPT="$GIT_DIR/loyaltylion_to_bigquery.py"

crontab - <<CRONEOF
# LoyaltyLion → BigQuery incremental sync
# 8:00 AM MYT (00:00 UTC)
0 0 * * * $PYTHON $SCRIPT incremental >> $LOG_DIR/sync_8am.log 2>&1
# 2:00 PM MYT (06:00 UTC)
0 6 * * * $PYTHON $SCRIPT incremental >> $LOG_DIR/sync_2pm.log 2>&1
CRONEOF

echo "Crontab installed:"
crontab -l

# ---- Step 7: Systemd services (optional, alternative to cron) ----
echo ""
echo "[7/7] Setting up systemd services and timers (optional)..."

APP_USER="msread"

# Incremental sync service
sudo tee /etc/systemd/system/loyaltylion-incremental.service > /dev/null << SVCEOF
[Unit]
Description=LoyaltyLion Incremental Sync to BigQuery
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=GOOGLE_CLOUD=true
Environment=CONNECTOR_PROJECT=msr-msia-sales-analysis
ExecStart=$GIT_DIR/venv/bin/python3 $GIT_DIR/loyaltylion_to_bigquery.py incremental
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF

# Incremental timer (daily 00:00 UTC = 8:00 AM MYT)
sudo tee /etc/systemd/system/loyaltylion-incremental.timer > /dev/null << TMREOF
[Unit]
Description=Run LoyaltyLion incremental sync daily at 8:00 AM MYT

[Timer]
OnCalendar=*-*-* 00:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
TMREOF

# Full sync service
sudo tee /etc/systemd/system/loyaltylion-full.service > /dev/null << SVCEOF2
[Unit]
Description=LoyaltyLion Full Sync to BigQuery
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=GOOGLE_CLOUD=true
Environment=CONNECTOR_PROJECT=msr-msia-sales-analysis
ExecStart=$GIT_DIR/venv/bin/python3 $GIT_DIR/loyaltylion_to_bigquery.py full
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SVCEOF2

# Full sync timer (weekly Sunday 21:00 UTC = Monday 05:00 MYT)
sudo tee /etc/systemd/system/loyaltylion-full.timer > /dev/null << TMREOF2
[Unit]
Description=Run LoyaltyLion full sync weekly (Monday 05:00 MYT)

[Timer]
OnCalendar=Mon *-*-* 21:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
TMREOF2

sudo systemctl daemon-reload

echo ""
echo "=========================================="
echo " DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "Scheduled syncs (cron):"
echo "  8:00 AM MYT (00:00 UTC) — incremental"
echo "  2:00 PM MYT (06:00 UTC) — incremental"
echo ""
echo "Manual run:"
echo "  Incremental: $GIT_DIR/venv/bin/python3 $GIT_DIR/loyaltylion_to_bigquery.py incremental"
echo "  Full:         $GIT_DIR/venv/bin/python3 $GIT_DIR/loyaltylion_to_bigquery.py full"
echo ""
echo "Update code:"
echo "  cd $GIT_DIR && git pull"
echo ""
echo "Logs:"
echo "  Incremental 8am: $LOG_DIR/sync_8am.log"
echo "  Incremental 2pm: $LOG_DIR/sync_2pm.log"
echo ""
echo "Optional systemd timers (if preferred over cron):"
echo "  sudo systemctl enable --now loyaltylion-incremental.timer"
echo "  sudo systemctl enable --now loyaltylion-full.timer"
echo ""
echo "To enable systemd timers (and disable cron if using systemd instead):"
echo "  sudo systemctl enable --now loyaltylion-incremental.timer"
echo "  sudo systemctl enable --now loyaltylion-full.timer"
echo "  crontab -r  # remove cron entries if using systemd"
echo ""