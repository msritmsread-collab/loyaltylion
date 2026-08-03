"""LoyaltyLion API Client — v2 REST API

Supports Bearer token auth (ProgramApiKey), cursor-based pagination,
and rate-limited requests (20 req/s).

Authentication priority:
  1. GCP Secret Manager (on VM with GOOGLE_CLOUD=true)
  2. Local JSON file (loyaltylion_credentials.json)
"""

import json
import os
import time
import platform
import logging
import requests
from pathlib import Path
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path(__file__).parent / "loyaltylion_credentials.json"

# ── Secret Manager Auth ─────────────────────────────────────────────────────

SECRET_API_KEY = "connector-loyaltylion-api-key"
SECRET_BQ_SA = "connector-bq-service-account"


def _is_cloud():
    """Detect if running on GCP VM."""
    if os.environ.get("GOOGLE_CLOUD", "").lower() == "true":
        return True
    # Fallback: check metadata server
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/id",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


def _get_secret(secret_id):
    """Fetch a secret from GCP Secret Manager."""
    from google.cloud import secretmanager
    project = os.environ.get("CONNECTOR_PROJECT", "msr-msia-sales-analysis")
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("UTF-8")


def load_credentials():
    """Load credentials with Secret Manager fallback.

    Priority:
      1. GCP Secret Manager (on VM)
      2. Local JSON file (loyaltylion_credentials.json)
    """
    if _is_cloud():
        log.info("Cloud environment detected — using Secret Manager for credentials")
        try:
            api_key = _get_secret(SECRET_API_KEY)
            # Try Secret Manager for BQ SA key
            try:
                bq_sa_json = _get_secret(SECRET_BQ_SA)
                bq_info = json.loads(bq_sa_json)
                # Write to temp file for BigQuery client
                import tempfile
                tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
                tmp.write(bq_sa_json)
                tmp.close()
                bq_credentials_path = tmp.name
            except Exception as e:
                log.warning(f"Could not load BQ SA from Secret Manager: {e}")
                bq_credentials_path = str(CREDENTIALS_PATH.parent / "bigquery_credentials.json")

            return {
                "api_key": api_key,
                "rate_limit_seconds": 0.5,
                "bigquery_project": os.environ.get("CONNECTOR_PROJECT", "msr-msia-sales-analysis"),
                "bigquery_dataset": "loyaltylion",
                "bigquery_credentials_path": bq_credentials_path,
            }
        except Exception as e:
            log.warning(f"Secret Manager failed, falling back to local file: {e}")

    # Fallback: local JSON file
    log.info(f"Loading credentials from {CREDENTIALS_PATH}")
    with open(CREDENTIALS_PATH) as f:
        return json.load(f)


def safe_float(val):
    try:
        return float(val) if val else None
    except (ValueError, TypeError):
        return None


def safe_int(val):
    try:
        return int(val) if val else None
    except (ValueError, TypeError):
        return None


class LoyaltyLionClient:
    """Handles authentication and API calls to LoyaltyLion v2."""

    def __init__(self, credentials=None):
        self.creds = credentials or load_credentials()
        self.api_key = self.creds["api_key"]
        self.base_url = "https://api.loyaltylion.com/v2"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })
        self._request_count = 0
        self._last_request_time = 0
        self.rate_limit_seconds = self.creds.get("rate_limit_seconds", 0.5)

    def _rate_limit(self):
        """Throttle requests to stay within API rate limits."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)
        self._last_request_time = time.time()
        self._request_count += 1

    def _get(self, path, params=None):
        """GET request with retry on 429 and 5xx errors."""
        if params is None:
            params = {}
        self._rate_limit()
        url = f"{self.base_url}/{path}"

        for attempt in range(4):
            resp = self.session.get(url, params=params)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                log.warning(f"Rate limited. Waiting {retry_after}s (attempt {attempt + 1})")
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500:
                log.warning(f"Server error {resp.status_code}, retrying (attempt {attempt + 1})...")
                time.sleep(2 ** attempt)
                continue

            log.error(f"API error {resp.status_code}: {resp.text[:500]}")
            return {"error": resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text}

        return {"error": {"message": "Max retries exceeded"}}

    def _paginate_cursor(self, path, params=None, max_pages=None):
        """Paginate through LoyaltyLion cursor-based results."""
        if params is None:
            params = {}
        params["limit"] = params.get("limit", 500)

        # Map API path to response data key
        data_key_map = {
            "customers": "customers",
            "activities": "activities",
            "transactions": "transactions",
            "orders": "orders",
        }
        data_key = data_key_map.get(path, path)

        results = []
        page = 0

        while True:
            data = self._get(path, params)

            if isinstance(data, dict) and "error" in data:
                log.error(f"Paginate error on {path}: {data['error']}")
                break

            items = data.get(data_key, [])
            results.extend(items)

            cursor = data.get("cursor", {})
            next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
            if not next_cursor:
                break

            params["cursor"] = next_cursor
            page += 1
            if max_pages and page >= max_pages:
                log.info(f"Reached max_pages limit ({max_pages}) on {path}")
                break

        return results

    # ── Entity Fetch Methods ──────────────────────────────────────────

    def get_customers(self, since_id=None, updated_at_min=None):
        params = {}
        if since_id is not None:
            params["since_id"] = since_id
        if updated_at_min:
            params["updated_at_min"] = updated_at_min
        return self._paginate_cursor("customers", params)

    def get_activities(self, since_id=None, created_at_min=None, created_at_max=None):
        params = {}
        if since_id is not None:
            params["since_id"] = since_id
        if created_at_min:
            params["created_at_min"] = created_at_min
        if created_at_max:
            params["created_at_max"] = created_at_max
        return self._paginate_cursor("activities", params)

    def get_transactions(self, since_id=None, created_at_min=None):
        params = {}
        if since_id is not None:
            params["since_id"] = since_id
        if created_at_min:
            params["created_at_min"] = created_at_min
        return self._paginate_cursor("transactions", params)

    def get_orders(self, since_id=None, updated_at_min=None):
        params = {}
        if since_id is not None:
            params["since_id"] = since_id
        if updated_at_min:
            params["updated_at_min"] = updated_at_min
        return self._paginate_cursor("orders", params)


# ── Flatten Functions ────────────────────────────────────────────────────

def flatten_customer(c):
    tier_membership = c.get("loyalty_tier_membership") or {}
    if not isinstance(tier_membership, dict):
        tier_membership = {}
    tier = tier_membership.get("loyalty_tier") or {} if isinstance(tier_membership, dict) else {}

    tier_eligibility = c.get("tier_eligibility") or {}
    if not isinstance(tier_eligibility, dict):
        tier_eligibility = {}

    referred_by = c.get("referred_by")
    if isinstance(referred_by, dict):
        referred_by = json.dumps(referred_by)

    properties = c.get("properties") or {}
    metadata = c.get("metadata") or {}
    linked_ids = c.get("linked_merchant_ids") or []

    return {
        "id": str(c.get("id", "")),
        "merchant_id": c.get("merchant_id"),
        "email": c.get("email"),
        "points_approved": safe_int(c.get("points_approved")),
        "points_pending": safe_int(c.get("points_pending")),
        "points_spent": safe_int(c.get("points_spent")),
        "rewards_claimed": safe_int(c.get("rewards_claimed")),
        "rewards_used": safe_int(c.get("rewards_used")),
        "blocked": c.get("blocked"),
        "guest": c.get("guest"),
        "enrolled": c.get("enrolled"),
        "enrolled_at": c.get("enrolled_at"),
        "referral_id": c.get("referral_id"),
        "referred_by": referred_by,
        "loyalty_tier_id": safe_int(tier_membership.get("id")) if isinstance(tier_membership, dict) else None,
        "loyalty_tier_name": tier.get("name") if isinstance(tier, dict) else None,
        "insights_segment": c.get("insights_segment"),
        "birthday": c.get("birthday"),
        "referral_url": c.get("referral_url"),
        "loyalty_pass_url": c.get("loyalty_pass_url"),
        "properties_json": json.dumps(properties) if properties else None,
        "metadata_json": json.dumps(metadata) if metadata else None,
        "linked_merchant_ids_json": json.dumps(linked_ids) if linked_ids else None,
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }


def flatten_activity(a):
    customer = a.get("customer") or {}
    if not isinstance(customer, dict):
        customer = {}
    rule = a.get("rule") or {}
    if not isinstance(rule, dict):
        rule = {}
    flow = a.get("flow") or {}
    if not isinstance(flow, dict):
        flow = {}

    return {
        "id": str(a.get("id", "")),
        "merchant_id": a.get("merchant_id"),
        "value": safe_int(a.get("value")),
        "state": a.get("state"),
        "customer_id": str(customer.get("id", "")) if customer.get("id") else None,
        "customer_merchant_id": customer.get("merchant_id"),
        "customer_email": customer.get("email"),
        "customer_points_pending": safe_int(customer.get("points_pending")),
        "customer_points_approved": safe_int(customer.get("points_approved")),
        "customer_points_spent": safe_int(customer.get("points_spent")),
        "rule_id": safe_int(rule.get("id")) if rule.get("id") else None,
        "rule_name": rule.get("name"),
        "rule_title": rule.get("title"),
        "flow_id": safe_int(flow.get("id")) if flow.get("id") else None,
        "flow_journey_id": safe_int(flow.get("journey_id")) if flow.get("journey_id") else None,
        "flow_name": flow.get("name"),
        "flow_block_id": flow.get("block_id"),
        "created_at": a.get("created_at"),
    }


def flatten_transaction(t):
    customer = t.get("customer") or {}
    if not isinstance(customer, dict):
        customer = {}

    # Handle resource discriminator (activity, adjustment, expiry, claimed_reward)
    resource_type = t.get("resource")
    resource_obj = t.get(resource_type) if isinstance(resource_type, str) else None
    resource_id = None
    if isinstance(resource_obj, dict):
        resource_id = resource_obj.get("id")

    return {
        "id": str(t.get("id", "")),
        "merchant_id": t.get("merchant_id"),
        "value": safe_int(t.get("value")) or safe_int(t.get("points_approved")),
        "points": safe_int(t.get("points")) or safe_int(t.get("points_approved")),
        "description": t.get("description"),
        "transaction_type": t.get("transaction_type") or t.get("type") or resource_type,
        "state": t.get("state"),
        "customer_id": str(customer.get("id", "")) if customer.get("id") else None,
        "customer_email": customer.get("email"),
        "resource_type": resource_type,
        "resource_id": str(resource_id) if resource_id else None,
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
    }


def flatten_order(o):
    customer = o.get("customer") or {}
    if not isinstance(customer, dict):
        customer = {}

    return {
        "id": str(o.get("id", "")),
        "merchant_id": o.get("merchant_id"),
        "order_number": o.get("merchant_number") or o.get("order_number"),
        "total": safe_float(o.get("total")),
        "total_tax": safe_float(o.get("total_tax")),
        "total_shipping": safe_float(o.get("total_shipping")),
        "total_discounts": safe_float(o.get("total_discounts")),
        "total_paid": safe_float(o.get("total_paid")),
        "total_refunded": safe_float(o.get("total_refunded")),
        "payment_status": o.get("payment_status"),
        "fulfillment_status": o.get("fulfillment_status"),
        "refund_status": o.get("refund_status"),
        "cancellation_status": o.get("cancellation_status"),
        "customer_id": str(customer.get("id", "")) if customer.get("id") else None,
        "customer_email": customer.get("email") or o.get("customer_email"),
        "state": o.get("state"),
        "created_at": o.get("created_at"),
        "updated_at": o.get("updated_at"),
    }


def flatten_customer_balance(c, snapshot_date):
    """Flatten customer for the daily balance snapshot (point balance history)."""
    tier_membership = c.get("loyalty_tier_membership") or {}
    if not isinstance(tier_membership, dict):
        tier_membership = {}
    tier = tier_membership.get("loyalty_tier") or {} if isinstance(tier_membership, dict) else {}

    return {
        "id": str(c.get("id", "")),
        "merchant_id": c.get("merchant_id"),
        "email": c.get("email"),
        "points_approved": safe_int(c.get("points_approved")),
        "points_pending": safe_int(c.get("points_pending")),
        "points_spent": safe_int(c.get("points_spent")),
        "rewards_claimed": safe_int(c.get("rewards_claimed")),
        "rewards_used": safe_int(c.get("rewards_used")),
        "enrolled": c.get("enrolled"),
        "loyalty_tier_id": safe_int(tier_membership.get("id")) if isinstance(tier_membership, dict) else None,
        "loyalty_tier_name": tier.get("name") if isinstance(tier, dict) else None,
        "insights_segment": c.get("insights_segment"),
        "snapshot_date": snapshot_date,
        "_pulled_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    client = LoyaltyLionClient()
    print("Testing LoyaltyLion connection...")
    customers = client.get_customers()
    print(f"Customers: {len(customers)} records")
    activities = client.get_activities()
    print(f"Activities: {len(activities)} records")
    orders = client.get_orders()
    print(f"Orders: {len(orders)} records")
    print("LoyaltyLion connection successful!")