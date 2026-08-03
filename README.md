# LoyaltyLion → BigQuery Connector

Syncs loyalty program data from the LoyaltyLion v2 API into Google BigQuery with incremental refresh support.

## Tables

| Table | Partition | Cluster | Incremental |
|-------|-----------|---------|-------------|
| `ll_customers` | `updated_at` | `email` | `since_id` (new only — run full weekly for updates) |
| `ll_activities` | `created_at` | `customer_id, state` | `since_id` (immutable events) |
| `ll_transactions` | `created_at` | `customer_id, transaction_type` | `since_id` |
| `ll_orders` | `created_at` | `customer_id, state` | `since_id` (new only — run full weekly for status changes) |
| `ll_customer_balances` | `snapshot_date` | `merchant_id, snapshot_date` | Daily snapshot |

## Setup

1. Copy `loyaltylion_credentials.json.example` to `loyaltylion_credentials.json` and fill in:
   - `api_key` — LoyaltyLion private API key (starts with `pat_`)
   - `bigquery_project` — GCP project ID
   - `bigquery_dataset` — BigQuery dataset name (default: `loyaltylion`)
   - `bigquery_credentials_path` — Path to service account JSON key file

2. Install dependencies:
   ```bash
   pip install google-cloud-bigquery requests
   ```

## Usage

```bash
# Full sync (first run or weekly refresh)
python loyaltylion_to_bigquery.py full

# Incremental sync (daily, only new/updated records)
python loyaltylion_to_bigquery.py incremental
```

## Key Design Notes

- **LoyaltyLion API v2** uses cursor-based pagination (`cursor.next` in response body)
- **`since_id`** for incremental — only fetches NEW records, not updates to existing ones
- **Bearer token auth** (ProgramApiKey)
- Nested objects (customer in activity, rule in activity, tier in customer) are **flattened** into flat columns
- Complex objects (properties, metadata) are stored as **JSON strings** in `properties_json`, `metadata_json`
- `since_id` does NOT capture updates to existing records — periodic full syncs recommended for `customers` and `orders`
- LoyaltyLion IDs are numeric but stored as **STRING** in BigQuery for flexibility
- Order monetary fields (`total`, `total_tax`, etc.) come as decimal strings — converted with `safe_float()`
- `ll_customer_balances` takes a daily snapshot; if BigQuery's streaming buffer is active, it falls back to TRUNCATE instead of targeted DELETE

## Credentials

**Never commit `loyaltylion_credentials.json`** — it contains API keys. The `.gitignore` excludes it along with `loyaltylion_sync_state.json` (runtime state).