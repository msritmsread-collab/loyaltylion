# LoyaltyLion → BigQuery Connector

Syncs loyalty program data from the LoyaltyLion v2 API into Google BigQuery with incremental refresh support.

## Tables

| Table | Partition | Cluster | Incremental |
|-------|-----------|---------|-------------|
| `ll_customers` | `updated_at` | `email` | `updated_at_min` (catches point balance changes) |
| `ll_activities` | `created_at` | `customer_id, state` | `since_id` (immutable events) |
| `ll_transactions` | `created_at` | `customer_id, transaction_type` | `since_id` |
| `ll_orders` | `created_at` | `customer_id, state` | `since_id` |
| `ll_customer_balances` | `snapshot_date` | `merchant_id, snapshot_date` | Daily snapshot |

## VM Deployment (loyaltylion VM)

The connector runs on a dedicated GCP VM (`loyaltylion`, IP: `34.124.194.116`).

### Directory Layout

```
/opt/msread-loyaltylion → /home/msrit_msread/loyaltylion (symlink)
~/loyaltylion/                          # Git clone of this repo
  ├── loyaltylion_connect.py            # API client
  ├── loyaltylion_to_bigquery.py        # Pipeline orchestrator
  ├── loyaltylion_credentials.json      # API key + BigQuery config (not in git)
  ├── bigquery_credentials.json         # Service account key (not in git)
  ├── loyaltylion_sync_state.json       # Runtime state (not in git)
  ├── venv/                             # Python virtual environment
  └── logs/                             # Cron log output
```

### Setup (one-time)

```bash
# Clone the repo
git clone https://github.com/msritmsread-collab/loyaltylion.git ~/loyaltylion

# Create symlink
sudo ln -s /home/msrit_msread/loyaltylion /opt/msread-loyaltylion

# Copy credentials (not in git)
cp /tmp/loyaltylion_credentials.json ~/loyaltylion/
cp /tmp/loyaltylion_sync_state.json ~/loyaltylion/
# Upload bigquery_credentials.json separately (SCP from local machine)

# Update credentials path to match new location
sed -i 's|/opt/msread-loyaltylion/bigquery_credentials.json|/home/msrit_msread/loyaltylion/bigquery_credentials.json|' ~/loyaltylion/loyaltylion_credentials.json

# Create venv and install deps
python3 -m venv ~/loyaltylion/venv
~/loyaltylion/venv/bin/pip install -r ~/loyaltylion/requirements.txt

# Create log directory
mkdir -p ~/loyaltylion/logs
```

### Scheduled Sync (Crontab)

Runs twice daily — 8:00 AM and 2:00 PM Malaysia time (UTC+8):

```bash
crontab -l
# 0 0 * * * /home/msrit_msread/loyaltylion/venv/bin/python3 /home/msrit_msread/loyaltylion/loyaltylion_to_bigquery.py incremental >> /home/msrit_msread/loyaltylion/logs/sync_8am.log 2>&1
# 0 6 * * * /home/msrit_msread/loyaltylion/venv/bin/python3 /home/msrit_msread/loyaltylion/loyaltylion_to_bigquery.py incremental >> /home/msrit_msread/loyaltylion/logs/sync_2pm.log 2>&1
```

### Manual Run

```bash
# Incremental (daily)
~/loyaltylion/venv/bin/python3 ~/loyaltylion/loyaltylion_to_bigquery.py incremental

# Full sync (re-fetches everything, truncates all tables first)
~/loyaltylion/venv/bin/python3 ~/loyaltylion/loyaltylion_to_bigquery.py full
```

### Update Code

```bash
cd ~/loyaltylion && git pull
```

## Authentication

Priority order for BigQuery credentials:
1. **Service account key file** (`bigquery_credentials_path` in `loyaltylion_credentials.json`) — used first
2. **VM default credentials** — fallback if key file is missing

For LoyaltyLion API:
1. **GCP Secret Manager** (on VM with `GOOGLE_CLOUD=true`) — tried first
2. **`loyaltylion_credentials.json`** — local fallback

**Never commit** `loyaltylion_credentials.json`, `bigquery_credentials.json`, or `loyaltylion_sync_state.json` — they contain secrets. The `.gitignore` excludes them.

## Key Design Notes

- **LoyaltyLion API v2** uses cursor-based pagination (`cursor.next` in response body)
- **Customers** use `updated_at_min` for incremental sync to catch point balance changes, with DELETE+re-insert for updated rows
- **Activities/transactions/orders** use `since_id` (immutable events, new records only)
- **`ll_customer_balances`** takes a daily snapshot; if BigQuery's streaming buffer is active, it falls back to TRUNCATE
- **Full sync** truncates all tables first and resets sync state to prevent duplicates
- **Streaming buffer handling**: both `delete_updated_since` and `delete_for_date` catch streaming buffer errors and fall back to TRUNCATE
- Nested objects (customer in activity, rule in activity, tier in customer) are **flattened** into flat columns
- Complex objects (properties, metadata) are stored as **JSON strings**
- LoyaltyLion IDs are numeric but stored as **STRING** in BigQuery for flexibility
- Order monetary fields (`total`, `total_tax`, etc.) come as decimal strings — converted with `safe_float()`