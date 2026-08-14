"""Orchestrates one sync run across all configured Mindware BSS tenants: pull
new/updated invoices from each, map each to the Orion JSON shape, and push (or,
for now, save locally).

Run manually with `python main.py`, or wire up to Task Scheduler / cron for a
recurring sync. Each tenant is a fully separate Mindware BSS database (own
accounts, own payment methods) with its own config file in tenants/*.json (see
tenants/_template.json) and its own incremental sync state in state/<tenant>.json.
"""

import glob
import json
import os

from dotenv import load_dotenv

from bss_client import BSSClient
from mapper import (
    build_orion_payload,
    MissingAccountConfigError,
    MissingCustomerCodeError,
    MissingItemCodeError,
    MissingPaymentTermError,
    UnhandledDiscountError,
)
from orion_client import OrionClient, push_payload

SYNC_DIR = os.path.dirname(__file__)
TENANTS_DIR = os.path.join(SYNC_DIR, "tenants")
STATE_DIR = os.path.join(SYNC_DIR, "state")


def load_tenant_configs():
    configs = []
    for path in sorted(glob.glob(os.path.join(TENANTS_DIR, "*.json"))):
        if os.path.basename(path).startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            configs.append(json.load(f))
    return configs


def load_state(tenant_name):
    path = os.path.join(STATE_DIR, f"{tenant_name}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated_at": None}


def save_state(tenant_name, state):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"{tenant_name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def sync_tenant(tenant_config, orion_client):
    tenant_name = tenant_config["tenant_name"]
    bss = tenant_config["bss"]
    orion_config = tenant_config["orion"]
    payment_term_map = tenant_config["payment_term_map"]
    account_config = tenant_config["account_config"]

    client = BSSClient(
        base_url=bss["base_url"],
        token_url=bss["token_url"],
        username=bss["username"],
        password=bss["password"],
        client_id=bss.get("client_id"),
        client_secret=bss.get("client_secret"),
        api_version=bss.get("api_version", "3"),
    )

    state = load_state(tenant_name)
    account_cache = {}
    processed = 0
    skipped = 0
    latest_updated_at = state["last_updated_at"]

    invoice_prefix = orion_config.get("invoice_prefix")

    for invoice in client.iter_invoices(updated_since=state["last_updated_at"]):
        invoice_id = invoice["id"]

        # Each tenant's invoice codes carry a fixed prefix (DNSA, DNKW, ...), and all
        # the per-tenant Orion constants (locations, tax, currency) hang off it. A
        # mismatch means this tenant's config file and its BSS credentials don't
        # belong together, so skip rather than book with the wrong constants.
        code = invoice.get("code") or ""
        if invoice_prefix and not code.startswith(invoice_prefix):
            print(f"[{tenant_name}] [SKIP] invoice {invoice_id}: code {code!r} does not "
                  f"start with this tenant's prefix {invoice_prefix!r} -- check config.")
            skipped += 1
            continue

        # Mindware bills the partner (billingTo), not the end customer (account) --
        # that's the account whose `code` becomes Orion's "Customer code" field.
        billing_id = (invoice.get("billingTo") or invoice["account"])["id"]
        end_customer_id = invoice["account"]["id"]

        if billing_id not in account_cache:
            account_cache[billing_id] = client.get_account(billing_id)
        # The end customer's own account is only needed for its country (for
        # "End User Details"); fetched separately since it's a different account
        # than the billing party whenever billingTo is set.
        if end_customer_id not in account_cache:
            account_cache[end_customer_id] = client.get_account(end_customer_id)

        try:
            payload = build_orion_payload(
                invoice, account_cache[billing_id], account_cache[end_customer_id],
                orion_config, payment_term_map, account_config
            )
        except (MissingAccountConfigError, MissingCustomerCodeError,
                MissingItemCodeError, MissingPaymentTermError, UnhandledDiscountError) as e:
            print(f"[{tenant_name}] [SKIP] invoice {invoice_id}: {e}")
            skipped += 1
            continue

        result = push_payload(payload, invoice_id, tenant_name, orion_client=orion_client)
        print(f"[{tenant_name}] [OK] invoice {invoice_id} -> {result}")
        processed += 1
        latest_updated_at = invoice["updatedAt"]

    state["last_updated_at"] = latest_updated_at
    save_state(tenant_name, state)
    print(f"[{tenant_name}] done. processed={processed} skipped={skipped} last_updated_at={latest_updated_at}")
    return processed, skipped


def main():
    load_dotenv(os.path.join(SYNC_DIR, ".env"))
    orion_base_url = os.environ.get("ORION_BASE_URL") or None
    orion_username = os.environ.get("ORION_USERNAME") or None
    orion_password = os.environ.get("ORION_PASSWORD") or None
    orion_client = (
        OrionClient(orion_base_url, orion_username, orion_password)
        if orion_base_url and orion_username and orion_password
        else None
    )
    if orion_client is None:
        print("[INFO] ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD not fully set in .env "
              "-- payloads will be saved to outbox/ instead of posted to Orion.")

    tenant_configs = load_tenant_configs()
    if not tenant_configs:
        print(f"No tenant config files found in {TENANTS_DIR}. Copy tenants/_template.json and fill it in.")
        return

    total_processed = 0
    total_skipped = 0
    failed_tenants = []
    for tenant_config in tenant_configs:
        tenant_name = tenant_config["tenant_name"]
        if not tenant_config["bss"]["username"]:
            print(f"[{tenant_name}] [SKIP TENANT] no credentials configured yet -- fill in tenants/{tenant_name}.json")
            failed_tenants.append(tenant_name)
            continue
        try:
            processed, skipped = sync_tenant(tenant_config, orion_client)
            total_processed += processed
            total_skipped += skipped
        except Exception as e:
            # One tenant's failure (bad credentials, network issue, etc.) must not
            # stop the others from syncing.
            print(f"[{tenant_name}] [TENANT FAILED] {type(e).__name__}: {e}")
            failed_tenants.append(tenant_name)

    print(f"\nAll tenants done. total_processed={total_processed} total_skipped={total_skipped} "
          f"failed_tenants={failed_tenants}")


if __name__ == "__main__":
    main()
