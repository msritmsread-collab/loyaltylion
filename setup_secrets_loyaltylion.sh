#!/bin/bash
# One-time setup: Create LoyaltyLion secrets in GCP Secret Manager
# Run this from your local machine with gcloud CLI authenticated
#
# Usage: bash setup_secrets.sh

set -e

PROJECT="msr-msia-sales-analysis"

echo "=========================================="
echo " GCP Secret Manager Setup - LoyaltyLion"
echo "=========================================="

# Enable Secret Manager API
echo ""
echo "Enabling Secret Manager API..."
gcloud services enable secretmanager.googleapis.com --project=$PROJECT

# Create LoyaltyLion API key secret
echo ""
echo "Creating LoyaltyLion API key secret..."
echo -n "PASTE_LOYALTYLION_API_KEY_HERE" | \
    gcloud secrets create connector-loyaltylion-api-key --data-file=- --project=$PROJECT

# BigQuery service account key (reuse if already exists from other connectors)
echo ""
echo "Checking BigQuery service account secret..."
if gcloud secrets describe connector-bq-service-account --project=$PROJECT &>/dev/null; then
    echo "  connector-bq-service-account already exists — skipping"
else
    echo "  Creating connector-bq-service-account secret..."
    echo "  Paste the BigQuery service account JSON key content, then press Ctrl+D:"
    cat | gcloud secrets create connector-bq-service-account --data-file=- --project=$PROJECT
fi

# VM service account — update this to match your VM's service account
VM_SA="loyaltylion@msr-msia-sales-analysis.iam.gserviceaccount.com"
echo ""
echo "Granting VM service account access to secrets..."
echo "(VM SA: $VM_SA)"
echo ""
read -p "Is this the correct VM service account email? (y/n) " -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Enter the correct VM service account email:"
    read VM_SA
fi

for secret in connector-loyaltylion-api-key connector-bq-service-account; do
    gcloud secrets add-iam-policy-binding $secret \
        --member="serviceAccount:$VM_SA" \
        --role="roles/secretmanager.secretAccessor" \
        --project=$PROJECT
    echo "  $secret -> OK"
done

echo ""
echo "=========================================="
echo " SECRET MANAGER SETUP COMPLETE"
echo "=========================================="
echo ""
echo "IMPORTANT: Update the LoyaltyLion API key with the real value:"
echo "  echo -n 'YOUR_ACTUAL_API_KEY' | gcloud secrets versions add connector-loyaltylion-api-key --data-file=- --project=$PROJECT"
echo ""
echo "Created secrets:"
gcloud secrets list --project=$PROJECT --filter="name:connector-loyaltylion OR name:connector-bq-service" --format="table(name,created)"