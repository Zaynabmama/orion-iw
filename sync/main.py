"""Orchestrates one sync run across all configured Mindware BSS tenants: pull
new/updated invoices from each, map each to the Orion JSON shape, and push (or,
for now, save locally).

Run manually with `python main.py`, or via a scheduled task for a recurring
sync (see nightly Task Scheduler setup, 2026-08-14). Each tenant is a fully
separate Mindware BSS database (own accounts, own payment methods) with its own
config file in tenants/*.json (see tenants/_template.json) and its own
incremental sync state in state/<tenant>.json.

Every run writes a full log to logs/<timestamp>.log and, if anything was
skipped or a tenant failed, emails a summary via notifier.send_alert() -- this
is the safety net for the known gap where a skipped invoice can permanently
drop out of future runs once a later invoice's success advances the tenant's
sync checkpoint past it (see mapper.py "Invoice date" / account_config notes
and the 2026-08-14 conversation). The user chose alerting over fixing that
checkpoint gap outright, so skips MUST stay visible here.
"""

import glob
import json
import os
from datetime import datetime

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
import notifier

SYNC_DIR = os.path.dirname(__file__)
TENANTS_DIR = os.path.join(SYNC_DIR, "tenants")
STATE_DIR = os.path.join(SYNC_DIR, "state")
LOGS_DIR = os.path.join(SYNC_DIR, "logs")


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


def sync_tenant(tenant_config, orion_client, log):
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
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id}: code {code!r} does not "
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
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): {e}")
            skipped += 1
            continue

        result = push_payload(payload, invoice_id, tenant_name, orion_client=orion_client)
        log(f"[{tenant_name}] [OK] invoice {invoice_id} -> {result}")
        processed += 1
        latest_updated_at = invoice["updatedAt"]

    state["last_updated_at"] = latest_updated_at
    save_state(tenant_name, state)
    log(f"[{tenant_name}] done. processed={processed} skipped={skipped} last_updated_at={latest_updated_at}")
    return processed, skipped


def main():
    load_dotenv(os.path.join(SYNC_DIR, ".env"))

    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    orion_base_url = os.environ.get("ORION_BASE_URL") or None
    orion_username = os.environ.get("ORION_USERNAME") or None
    orion_password = os.environ.get("ORION_PASSWORD") or None
    orion_client = (
        OrionClient(orion_base_url, orion_username, orion_password)
        if orion_base_url and orion_username and orion_password
        else None
    )
    if orion_client is None:
        log("[INFO] ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD not fully set in .env "
            "-- payloads will be saved to outbox/ instead of posted to Orion.")

    tenant_configs = load_tenant_configs()
    if not tenant_configs:
        log(f"No tenant config files found in {TENANTS_DIR}. Copy tenants/_template.json and fill it in.")
        _write_log(log_lines)
        return

    total_processed = 0
    total_skipped = 0
    failed_tenants = []
    for tenant_config in tenant_configs:
        tenant_name = tenant_config["tenant_name"]
        if not tenant_config["bss"]["username"]:
            log(f"[{tenant_name}] [SKIP TENANT] no credentials configured yet -- fill in tenants/{tenant_name}.json")
            failed_tenants.append(tenant_name)
            continue
        try:
            processed, skipped = sync_tenant(tenant_config, orion_client, log)
            total_processed += processed
            total_skipped += skipped
        except Exception as e:
            # One tenant's failure (bad credentials, network issue, etc.) must not
            # stop the others from syncing.
            log(f"[{tenant_name}] [TENANT FAILED] {type(e).__name__}: {e}")
            failed_tenants.append(tenant_name)

    log(f"\nAll tenants done. total_processed={total_processed} total_skipped={total_skipped} "
        f"failed_tenants={failed_tenants}")

    log_path = _write_log(log_lines)

    if total_skipped or failed_tenants:
        _send_alert(total_processed, total_skipped, failed_tenants, log_lines, log_path)


def _write_log(log_lines):
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(LOGS_DIR, f"{ts}.log")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    return path


def _send_alert(total_processed, total_skipped, failed_tenants, log_lines, log_path):
    recipients = [addr.strip() for addr in os.environ.get("ALERT_RECIPIENTS", "").split(",") if addr.strip()]
    if not recipients:
        print("[WARN] Skipped/failed invoices this run, but ALERT_RECIPIENTS isn't set in .env -- no email sent.")
        return

    subject = f"Orion sync: {total_skipped} skipped" + (f", {len(failed_tenants)} tenant(s) failed" if failed_tenants else "")
    body = (
        f"processed={total_processed} skipped={total_skipped} failed_tenants={failed_tenants}\n"
        f"Full log saved at: {log_path}\n\n"
        + "\n".join(log_lines)
    )
    try:
        notifier.send_alert(subject, body, recipients)
    except Exception as e:
        # Don't let a broken alert path crash the run itself -- the log file on
        # disk is still the fallback record even if the email never went out.
        print(f"[WARN] Failed to send alert email: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
