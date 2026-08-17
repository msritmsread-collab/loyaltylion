"""LoyaltyLion → BigQuery Pipeline

Syncs loyalty program data from LoyaltyLion v2 API
into BigQuery with incremental refresh support.

Tables created:
  - ll_customers           (loyalty customer master, incremental with update tracking)
  - ll_activities          (points earned/spent events, incremental)
  - ll_transactions        (point transactions, incremental)
  - ll_orders              (order loyalty data, incremental)
  - ll_customer_balances   (daily point balance snapshot for trend tracking)
"""

import json
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone

from google.cloud import bigquery
from google.api_core.exceptions import NotFound

from loyaltylion_connect import (
    LoyaltyLionClient,
    load_credentials,
    flatten_customer,
    flatten_activity,
    flatten_transaction,
    flatten_order,
    flatten_customer_balance,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path(__file__).parent / "loyaltylion_credentials.json"
SYNC_STATE_PATH = Path(__file__).parent / "loyaltylion_sync_state.json"

# ── BigQuery Schema Definitions ──────────────────────────────────────────

SCHEMAS = {
    "ll_customers": [
        bigquery.SchemaField("id", "STRING"),
        bigquery.SchemaField("merchant_id", "STRING"),
        bigquery.SchemaField("email", "STRING"),
        bigquery.SchemaField("points_approved", "INTEGER"),
        bigquery.SchemaField("points_pending", "INTEGER"),
        bigquery.SchemaField("points_spent", "INTEGER"),
        bigquery.SchemaField("rewards_claimed", "INTEGER"),
        bigquery.SchemaField("rewards_used", "INTEGER"),
        bigquery.SchemaField("blocked", "BOOLEAN"),
        bigquery.SchemaField("guest", "BOOLEAN"),
        bigquery.SchemaField("enrolled", "BOOLEAN"),
        bigquery.SchemaField("enrolled_at", "TIMESTAMP"),
        bigquery.SchemaField("referral_id", "STRING"),
        bigquery.SchemaField("referred_by", "STRING"),
        bigquery.SchemaField("loyalty_tier_id", "STRING"),
        bigquery.SchemaField("loyalty_tier_name", "STRING"),
        bigquery.SchemaField("insights_segment", "STRING"),
        bigquery.SchemaField("birthday", "STRING"),
        bigquery.SchemaField("referral_url", "STRING"),
        bigquery.SchemaField("loyalty_pass_url", "STRING"),
        bigquery.SchemaField("properties_json", "STRING"),
        bigquery.SchemaField("metadata_json", "STRING"),
        bigquery.SchemaField("linked_merchant_ids_json", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
        bigquery.SchemaField("_pulled_at", "TIMESTAMP"),
    ],
    "ll_activities": [
        bigquery.SchemaField("id", "STRING"),
        bigquery.SchemaField("merchant_id", "STRING"),
        bigquery.SchemaField("value", "INTEGER"),
        bigquery.SchemaField("state", "STRING"),
        bigquery.SchemaField("customer_id", "STRING"),
        bigquery.SchemaField("customer_merchant_id", "STRING"),
        bigquery.SchemaField("customer_email", "STRING"),
        bigquery.SchemaField("customer_points_pending", "INTEGER"),
        bigquery.SchemaField("customer_points_approved", "INTEGER"),
        bigquery.SchemaField("customer_points_spent", "INTEGER"),
        bigquery.SchemaField("rule_id", "STRING"),
        bigquery.SchemaField("rule_name", "STRING"),
        bigquery.SchemaField("rule_title", "STRING"),
        bigquery.SchemaField("flow_id", "STRING"),
        bigquery.SchemaField("flow_journey_id", "STRING"),
        bigquery.SchemaField("flow_name", "STRING"),
        bigquery.SchemaField("flow_block_id", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP"),
        bigquery.SchemaField("_pulled_at", "TIMESTAMP"),
    ],
    "ll_transactions": [
        bigquery.SchemaField("id", "STRING"),
        bigquery.SchemaField("merchant_id", "STRING"),
        bigquery.SchemaField("value", "INTEGER"),
        bigquery.SchemaField("points", "INTEGER"),
        bigquery.SchemaField("description", "STRING"),
        bigquery.SchemaField("transaction_type", "STRING"),
        bigquery.SchemaField("state", "STRING"),
        bigquery.SchemaField("customer_id", "STRING"),
        bigquery.SchemaField("customer_email", "STRING"),
        bigquery.SchemaField("resource_type", "STRING"),
        bigquery.SchemaField("resource_id", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
        bigquery.SchemaField("_pulled_at", "TIMESTAMP"),
    ],
    "ll_orders": [
        bigquery.SchemaField("id", "STRING"),
        bigquery.SchemaField("merchant_id", "STRING"),
        bigquery.SchemaField("order_number", "STRING"),
        bigquery.SchemaField("total", "FLOAT"),
        bigquery.SchemaField("total_tax", "FLOAT"),
        bigquery.SchemaField("total_shipping", "FLOAT"),
        bigquery.SchemaField("total_discounts", "FLOAT"),
        bigquery.SchemaField("total_paid", "FLOAT"),
        bigquery.SchemaField("total_refunded", "FLOAT"),
        bigquery.SchemaField("payment_status", "STRING"),
        bigquery.SchemaField("fulfillment_status", "STRING"),
        bigquery.SchemaField("refund_status", "STRING"),
        bigquery.SchemaField("cancellation_status", "STRING"),
        bigquery.SchemaField("customer_id", "STRING"),
        bigquery.SchemaField("customer_email", "STRING"),
        bigquery.SchemaField("state", "STRING"),
        bigquery.SchemaField("created_at", "TIMESTAMP"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
        bigquery.SchemaField("_pulled_at", "TIMESTAMP"),
    ],
    "ll_customer_balances": [
        bigquery.SchemaField("id", "STRING"),
        bigquery.SchemaField("merchant_id", "STRING"),
        bigquery.SchemaField("email", "STRING"),
        bigquery.SchemaField("points_approved", "INTEGER"),
        bigquery.SchemaField("points_pending", "INTEGER"),
        bigquery.SchemaField("points_spent", "INTEGER"),
        bigquery.SchemaField("rewards_claimed", "INTEGER"),
        bigquery.SchemaField("rewards_used", "INTEGER"),
        bigquery.SchemaField("enrolled", "BOOLEAN"),
        bigquery.SchemaField("loyalty_tier_id", "STRING"),
        bigquery.SchemaField("loyalty_tier_name", "STRING"),
        bigquery.SchemaField("insights_segment", "STRING"),
        bigquery.SchemaField("snapshot_date", "DATE"),
        bigquery.SchemaField("_pulled_at", "TIMESTAMP"),
    ],
}

# Partitioning config: table -> partition field
PARTITION_FIELDS = {
    "ll_customers": "updated_at",
    "ll_activities": "created_at",
    "ll_transactions": "created_at",
    "ll_orders": "created_at",
    "ll_customer_balances": "snapshot_date",
}

# Clustering config per table
CLUSTER_FIELDS = {
    "ll_customers": ["email"],
    "ll_activities": ["customer_id", "state"],
    "ll_transactions": ["customer_id", "transaction_type"],
    "ll_orders": ["customer_id", "state"],
    "ll_customer_balances": ["merchant_id", "snapshot_date"],
}


# ── Sync State Management ─────────────────────────────────────────────────

def load_sync_state():
    """Load last sync state for incremental refresh."""
    if SYNC_STATE_PATH.exists():
        with open(SYNC_STATE_PATH) as f:
            return json.load(f)
    return {}


def save_sync_state(state):
    """Save sync state after successful run."""
    with open(SYNC_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ── BigQuery Client ───────────────────────────────────────────────────────

class BigQueryLoader:
    def __init__(self, credentials=None):
        creds = credentials or load_credentials()
        self.project_id = creds["bigquery_project"]
        self.dataset_id = creds["bigquery_dataset"]
        self.dataset_ref = f"{self.project_id}.{self.dataset_id}"

        # Try VM default credentials first (on GCP VM), then fall back to SA key file
        try:
            import google.auth
            credentials_obj, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/bigquery"]
            )
            self.client = bigquery.Client(
                project=self.project_id,
                credentials=credentials_obj,
            )
            log.info("Using VM default credentials for BigQuery")
        except Exception:
            log.info("VM default creds not available, using service account key file")
            self.client = bigquery.Client.from_service_account_json(
                creds["bigquery_credentials_path"]
            )

    def ensure_dataset(self):
        """Create dataset if it doesn't exist."""
        try:
            self.client.get_dataset(self.dataset_ref)
            log.info(f"Dataset {self.dataset_ref} exists")
        except NotFound:
            dataset = bigquery.Dataset(self.dataset_ref)
            dataset.location = "asia-southeast1"
            self.client.create_dataset(dataset)
            log.info(f"Created dataset {self.dataset_ref}")

    def ensure_table(self, table_name):
        """Create table with schema, partitioning, and clustering if it doesn't exist."""
        table_ref = f"{self.dataset_ref}.{table_name}"
        try:
            self.client.get_table(table_ref)
        except NotFound:
            schema = SCHEMAS[table_name]
            table = bigquery.Table(table_ref, schema=schema)
            # Add time partitioning
            if table_name in PARTITION_FIELDS:
                table.time_partitioning = bigquery.TimePartitioning(
                    type_=bigquery.TimePartitioningType.DAY,
                    field=PARTITION_FIELDS[table_name],
                )
            # Add clustering
            if table_name in CLUSTER_FIELDS:
                table.clustering_fields = CLUSTER_FIELDS[table_name]
            self.client.create_table(table)
            log.info(f"Created table {table_ref}")

    def delete_for_date(self, table_name, date_str):
        """Delete rows for a specific snapshot_date (for balance snapshots).

        Falls back to TRUNCATE if streaming buffer prevents targeted DELETE,
        which is safe for daily snapshots since we immediately re-insert all rows.
        """
        table_ref = f"{self.dataset_ref}.{table_name}"
        try:
            job = self.client.query(
                f"DELETE FROM `{table_ref}` WHERE snapshot_date = DATE('{date_str}')"
            )
            job.result()
            log.info(f"Deleted existing rows in {table_name} for date {date_str}")
        except Exception as e:
            if "streaming buffer" in str(e).lower():
                log.warning(f"Streaming buffer active on {table_name}, using TRUNCATE instead")
                truncate_job = self.client.query(f"TRUNCATE TABLE `{table_ref}`")
                truncate_job.result()
                log.info(f"Truncated {table_name} (streaming buffer workaround)")
            else:
                raise

    def delete_updated_since(self, table_name, since_timestamp):
        """Delete rows updated after a given timestamp for incremental upsert.

        Falls back to TRUNCATE if streaming buffer prevents targeted DELETE,
        since we immediately re-insert all updated rows anyway.
        """
        table_ref = f"{self.dataset_ref}.{table_name}"
        try:
            job = self.client.query(
                f"DELETE FROM `{table_ref}` "
                f"WHERE updated_at >= TIMESTAMP('{since_timestamp}')"
            )
            job.result()
            log.info(f"Deleted rows in {table_name} updated since {since_timestamp}")
        except Exception as e:
            if "streaming buffer" in str(e).lower():
                log.warning(f"Streaming buffer active on {table_name}, truncating instead")
                truncate_job = self.client.query(f"TRUNCATE TABLE `{table_ref}`")
                truncate_job.result()
                log.info(f"Truncated {table_name} (streaming buffer workaround)")
            else:
                raise

    BATCH_SIZE = 5000  # rows per batch to stay under BigQuery's 10MB limit

    def load_rows(self, table_name, rows):
        """Load rows into BigQuery table with schema filtering and batch fallback."""
        if not rows:
            log.warning(f"No rows to load for {table_name}")
            return

        table_ref = f"{self.dataset_ref}.{table_name}"
        schema = SCHEMAS[table_name]
        schema_field_names = {f.name for f in schema}

        clean_rows = []
        for row in rows:
            clean_row = {k: v for k, v in row.items() if k in schema_field_names}
            for field in schema_field_names:
                if field not in clean_row:
                    clean_row[field] = None
            clean_rows.append(clean_row)

        total = len(clean_rows)
        log.info(f"Loading {total} rows to {table_name}...")

        # Try streaming insert in batches first
        all_errors = []
        for i in range(0, total, self.BATCH_SIZE):
            batch = clean_rows[i : i + self.BATCH_SIZE]
            batch_num = i // self.BATCH_SIZE + 1
            total_batches = (total + self.BATCH_SIZE - 1) // self.BATCH_SIZE
            log.info(f"  Batch {batch_num}/{total_batches}: {len(batch)} rows")
            errors = self.client.insert_rows_json(table_ref, batch)
            if errors:
                all_errors.extend(errors)

        if not all_errors:
            log.info(f"Loaded {total} rows to {table_name} via streaming insert")
        else:
            log.warning(f"Streaming insert had {len(all_errors)} errors, falling back to load job")
            job = self.client.load_table_from_json(clean_rows, table_ref)
            job.result()
            log.info(f"Loaded {total} rows to {table_name} via load job")


# ── Sync Functions ────────────────────────────────────────────────────────

# Tables using since_id for incremental (new records only)
SINCE_ID_TABLES = [
    ("activities", "ll_activities", flatten_activity),
    ("transactions", "ll_transactions", flatten_transaction),
    ("orders", "ll_orders", flatten_order),
]


def sync_table(ll, bq, entity_name, table_name, flatten_fn, state, since_id=None):
    """Generic sync for a single LoyaltyLion entity → BigQuery table."""
    log.info(f"Syncing {entity_name} → {table_name}...")
    now = datetime.now(timezone.utc).isoformat()

    fetch_method = getattr(ll, f"get_{entity_name}")
    records = fetch_method(since_id=since_id)

    if isinstance(records, dict) and "error" in records:
        log.error(f"Error fetching {entity_name}: {records['error']}")
        return 0

    rows = [flatten_fn(r) for r in records]
    for row in rows:
        row["_pulled_at"] = now

    if rows:
        bq.load_rows(table_name, rows)

    # Track max id for incremental state
    max_id = max(
        (str(r.get("id", "")) for r in records if r.get("id")),
        default=None,
    )
    if max_id:
        state[f"{table_name}_since_id"] = max_id

    log.info(f"{entity_name} sync complete: {len(rows)} rows")
    return len(rows)


def sync_customers_updated(ll, bq, state, updated_at_min=None):
    """
    Sync customers using updated_at_min to catch point balance changes.
    Deletes existing records that were updated since the watermark, then re-inserts.
    This ensures ll_customers always has current data.
    """
    log.info("Syncing customers (updated) → ll_customers...")
    now = datetime.now(timezone.utc).isoformat()

    records = ll.get_customers(updated_at_min=updated_at_min)

    if isinstance(records, dict) and "error" in records:
        log.error(f"Error fetching customers: {records['error']}")
        return 0

    rows = [flatten_customer(r) for r in records]
    for row in rows:
        row["_pulled_at"] = now

    if updated_at_min and rows:
        bq.delete_updated_since("ll_customers", updated_at_min)

    if rows:
        bq.load_rows("ll_customers", rows)

    # Track max updated_at for next incremental run
    max_updated = max(
        (r.get("updated_at") for r in records if r.get("updated_at")),
        default=None,
    )
    if max_updated:
        state["ll_customers_updated_at"] = max_updated

    # Also track max id for since_id (used as fallback)
    max_id = max(
        (str(r.get("id", "")) for r in records if r.get("id")),
        default=None,
    )
    if max_id:
        state["ll_customers_since_id"] = max_id

    log.info(f"Customers sync complete: {len(rows)} rows")
    return len(rows)


def sync_customer_balances(ll, bq):
    """
    Snapshot all customer point balances into ll_customer_balances.
    Runs every sync to build a daily time-series of point balances.
    Deletes any existing rows for today's date first to avoid duplicates.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info(f"Snapshotting customer balances for {today} → ll_customer_balances...")

    # Delete any existing rows for today (in case of re-run)
    bq.delete_for_date("ll_customer_balances", today)

    records = ll.get_customers()

    if isinstance(records, dict) and "error" in records:
        log.error(f"Error fetching customers for balance snapshot: {records['error']}")
        return 0

    rows = [flatten_customer_balance(r, snapshot_date=today) for r in records]

    if rows:
        bq.load_rows("ll_customer_balances", rows)

    log.info(f"Customer balance snapshot complete: {len(rows)} rows")
    return len(rows)


# ── Main Pipeline ─────────────────────────────────────────────────────────

def run_pipeline(mode="full"):
    """
    Run the LoyaltyLion → BigQuery pipeline.

    mode:
        "full"         — fetch all records (first run or forced refresh)
        "incremental"  — only fetch new/updated records since last sync

    Point balance tracking:
        - ll_customers uses updated_at_min to catch point balance changes
        - ll_customer_balances always does a full snapshot for the current day
        - Activities/transactions/orders use since_id (immutable events)
    """
    creds = load_credentials()
    ll = LoyaltyLionClient(creds)
    bq = BigQueryLoader(creds)

    # Ensure BigQuery dataset and tables exist
    bq.ensure_dataset()
    for table_name in SCHEMAS:
        bq.ensure_table(table_name)

    state = load_sync_state()

    if mode == "incremental" and not state.get("last_run"):
        log.warning("No previous sync state found. Falling back to full sync.")
        mode = "full"

    log.info(f"Running pipeline in {mode} mode")

    total_rows = 0

    # 1. Customers — use updated_at_min to catch point balance changes
    if mode == "incremental" and state.get("ll_customers_updated_at"):
        updated_at_min = state["ll_customers_updated_at"]
        log.info(f"Incremental customers: fetching updated since {updated_at_min}")
    else:
        updated_at_min = None
    total_rows += sync_customers_updated(ll, bq, state, updated_at_min=updated_at_min)

    # 2. Activities, transactions, orders — use since_id (immutable events)
    for entity_name, table_name, flatten_fn in SINCE_ID_TABLES:
        since_id = state.get(f"{table_name}_since_id") if mode == "incremental" else None
        rows_synced = sync_table(ll, bq, entity_name, table_name, flatten_fn, state, since_id=since_id)
        total_rows += rows_synced

    # 3. Customer balance snapshot — always runs to build daily history
    total_rows += sync_customer_balances(ll, bq)

    # Save sync state
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state["last_run_mode"] = mode
    save_sync_state(state)
    log.info(f"Pipeline complete! Mode: {mode}, Total rows: {total_rows}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode not in ("full", "incremental"):
        print(f"Usage: python {sys.argv[0]} [full|incremental]")
        print("  full         — fetch all records (first run or forced refresh)")
        print("  incremental  — only fetch new/updated records since last sync")
        print()
        print("Point balances are tracked two ways:")
        print("  1. ll_customers is kept current (updated records replaced)")
        print("  2. ll_customer_balances snapshots daily for trend analysis")
        sys.exit(1)

    run_pipeline(mode=mode)