"""One-off, targeted push for a specific date range, across multiple tenants.

Unlike main.py, this does NOT read or write state/<tenant>.json, so it has no
effect on the regular incremental sync's checkpoint for any tenant. Originally
built for the 2026-08-17 KSA batch only (the invoices deleted from Orion and
re-pushed); extended 2026-08-31 to cover a date range and loop over multiple
tenants (kuwait/freezone/uae, once those got real BSS credentials configured
-- qatar/oman still don't, so they're excluded).

Requires ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD in .env (real Orion
network access), so this only works run from ZaynabM_Super's machine.

Env vars:
  PUSH_DATE       start date, YYYY-MM-DD (default 2026-08-17)
  PUSH_DATE_END   optional inclusive end date, YYYY-MM-DD (single day if unset)
  PUSH_TENANTS    optional comma-separated tenant list (default: ksa_production,kuwait,freezone,uae)
  PUSH_ONLY_CODE  optional: restrict to one invoice code (any tenant)
  PUSH_CLOUD_INVOICE_NO  optional: override Cloud Invoice No for PUSH_ONLY_CODE
  PUSH_SUPPLEMENTARY=1   optional: ksa_production-only supplementary-items mode (see below)
"""

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
from orion_client import DuplicateInvoiceError, InvoiceRejectedError, OrionClient, push_payload

SYNC_DIR = os.path.dirname(__file__)
LOGS_DIR = os.path.join(SYNC_DIR, "logs")

DEFAULT_TENANTS = ["ksa_production", "kuwait", "freezone", "uae"]

TARGET_CODE = os.environ.get("PUSH_ONLY_CODE")
OVERRIDE_CLOUD_INVOICE_NO = os.environ.get("PUSH_CLOUD_INVOICE_NO")
SUPPLEMENTARY_MODE = os.environ.get("PUSH_SUPPLEMENTARY") == "1"
# ksa_production-only: re-push just specific missing line items from specific
# invoices as a separate supplementary invoice (Cloud Invoice No suffixed "-01").
SUPPLEMENTARY_ITEMS = {
    "DNSA-26-003899": [1],
    "DNSA-26-003904": [1, 2, 3],
    "DNSA-26-003887": [1, 2],
    "DNSA-26-003886": [1],
    "DNSA-26-003906": [1],
}


def _date_filter():
    from datetime import datetime as _dt, timedelta as _td
    date_str = os.environ.get("PUSH_DATE", "2026-08-17")
    start = _dt.strptime(date_str, "%Y-%m-%d")
    end_date_str = os.environ.get("PUSH_DATE_END")
    if end_date_str:
        # Inclusive end date: PUSH_DATE=2026-08-26 PUSH_DATE_END=2026-08-31
        # covers invoices dated 26th through 31st.
        end = _dt.strptime(end_date_str, "%Y-%m-%d") + _td(days=1)
    else:
        end = start + _td(days=1)
    return (f"invoiceDate ge datetime'{start:%Y-%m-%d}T00:00:00' "
            f"and invoiceDate lt datetime'{end:%Y-%m-%d}T00:00:00'")


DATE_FILTER = _date_filter()


def push_tenant(tenant_name, orion_client, log):
    with open(os.path.join(SYNC_DIR, "tenants", f"{tenant_name}.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    bss = cfg["bss"]
    orion_config = cfg["orion"]
    payment_term_map = cfg["payment_term_map"]
    account_config = cfg["account_config"]
    invoice_prefix = orion_config.get("invoice_prefix")
    orion_invoice_number_field_id = bss.get("orion_invoice_number_field_id")

    if not bss.get("username"):
        log(f"[{tenant_name}] [SKIP TENANT] no credentials configured yet.")
        return 0, 0, 0, 0

    client = BSSClient(
        base_url=bss["base_url"],
        token_url=bss["token_url"],
        username=bss["username"],
        password=bss["password"],
        client_id=bss.get("client_id"),
        client_secret=bss.get("client_secret"),
        api_version=bss.get("api_version", "3"),
    )

    result = client._get("/api/invoices", params={
        "pageIndex": 1,
        "pageSize": 200,
        "include": "items,customFields",
        "$orderBy": "invoiceDate asc",
        "$filter": DATE_FILTER,
    })
    invoices = result.get("data", [])
    log(f"[{tenant_name}] Pulled {len(invoices)} invoices from BSS.\n")

    account_cache = {}
    processed = skipped = duplicates = rejected = 0

    for invoice in invoices:
        invoice_id = invoice["id"]
        code = invoice.get("code") or ""

        if SUPPLEMENTARY_MODE and (tenant_name != "ksa_production" or code not in SUPPLEMENTARY_ITEMS):
            continue
        if TARGET_CODE and code != TARGET_CODE:
            continue

        if invoice_prefix and not code.startswith(invoice_prefix):
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): prefix mismatch.")
            skipped += 1
            continue

        invoice_type = (invoice.get("type") or {}).get("type")
        if invoice_type != "Debit":
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): type is {invoice_type!r}, not Debit.")
            skipped += 1
            continue

        invoice_status = (invoice.get("status") or {}).get("type")
        if invoice_status == "Canceled":
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): status is Canceled.")
            skipped += 1
            continue

        skip_product_keywords = orion_config.get("skip_product_keywords", [])
        invoice_product_names = [
            (item.get("product") or {}).get("name") or item.get("description") or ""
            for item in invoice.get("items") or []
        ]
        matched_skip_keyword = next(
            (keyword for keyword in skip_product_keywords
             if any(keyword.lower() in name.lower() for name in invoice_product_names)),
            None,
        )
        if matched_skip_keyword:
            log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): product matches "
                f"configured skip keyword {matched_skip_keyword!r}. Not synced.")
            skipped += 1
            continue

        billing_id = (invoice.get("billingTo") or invoice["account"])["id"]
        end_customer_id = invoice["account"]["id"]
        if billing_id not in account_cache:
            account_cache[billing_id] = client.get_account(billing_id)
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

        if SUPPLEMENTARY_MODE and tenant_name == "ksa_production":
            selected_indexes = SUPPLEMENTARY_ITEMS[code]
            if max(selected_indexes, default=-1) >= len(payload["Items"]):
                log(f"[{tenant_name}] [SKIP] invoice {invoice_id} ({code}): "
                    f"expected missing item indexes {selected_indexes}, but payload has "
                    f"{len(payload['Items'])} items.")
                skipped += 1
                continue
            payload["Items"] = [payload["Items"][index] for index in selected_indexes]
            payload["Cloud Invoice No"] = f"{code}-01"
            log(f"[{tenant_name}] [SUPPLEMENTARY] invoice {invoice_id} ({code}): "
                f"selected item indexes {selected_indexes}; Cloud Invoice No -> "
                f"{payload['Cloud Invoice No']}")

        if OVERRIDE_CLOUD_INVOICE_NO and code == TARGET_CODE:
            payload["Cloud Invoice No"] = OVERRIDE_CLOUD_INVOICE_NO
            log(f"[{tenant_name}] [OVERRIDE] invoice {invoice_id} ({code}): "
                f"Cloud Invoice No -> {OVERRIDE_CLOUD_INVOICE_NO}")

        try:
            result_push = push_payload(payload, invoice_id, tenant_name, orion_client=orion_client)
        except DuplicateInvoiceError:
            log(f"[{tenant_name}] [DUPLICATE] invoice {invoice_id} ({code}) already exists in Orion, skipping.")
            duplicates += 1
            continue
        except InvoiceRejectedError as e:
            log(f"[{tenant_name}] [REJECTED] invoice {invoice_id} ({code}): {e}")
            rejected += 1
            continue

        log(f"[{tenant_name}] [OK] invoice {invoice_id} ({code}) -> {result_push}")

        if result_push.get("mode") == "posted":
            if not orion_invoice_number_field_id:
                log(f"[{tenant_name}] [FIELD-SKIP] invoice {invoice_id}: orion_invoice_number_field_id not configured.")
            else:
                document_no = (result_push.get("response") or {}).get("DocumentNo")
                if not document_no:
                    log(f"[{tenant_name}] [WARN] invoice {invoice_id}: no DocumentNo to write back.")
                else:
                    try:
                        client.set_invoice_custom_field(invoice_id, orion_invoice_number_field_id, str(document_no))
                    except Exception as e:
                        log(f"[{tenant_name}] [WARN] invoice {invoice_id}: could not write "
                            f"Orion Invoice Number back to BSS: {type(e).__name__}: {e}")
                    else:
                        log(f"[{tenant_name}] [FIELD] invoice {invoice_id}: wrote Orion Invoice "
                            f"Number {document_no!r} back to BSS field {orion_invoice_number_field_id}.")

        processed += 1

    log(f"\n[{tenant_name}] done. processed={processed} skipped={skipped} "
        f"duplicates={duplicates} rejected={rejected}")
    log(f"(state/{tenant_name}.json was NOT touched by this script.)\n")
    return processed, skipped, duplicates, rejected


def main():
    load_dotenv(os.path.join(SYNC_DIR, ".env"))

    log_lines = []

    def log(msg):
        print(msg)
        log_lines.append(msg)

    orion_base_url = os.environ.get("ORION_BASE_URL")
    orion_username = os.environ.get("ORION_USERNAME")
    orion_password = os.environ.get("ORION_PASSWORD")
    if not (orion_base_url and orion_username and orion_password):
        log("[ERROR] ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD not set in .env. Aborting, nothing pushed.")
        return
    orion_client = OrionClient(orion_base_url, orion_username, orion_password)

    tenants = [t.strip() for t in os.environ.get("PUSH_TENANTS", ",".join(DEFAULT_TENANTS)).split(",") if t.strip()]
    log(f"Tenants: {tenants}   Date filter: {DATE_FILTER}\n")

    totals = {"processed": 0, "skipped": 0, "duplicates": 0, "rejected": 0}
    for tenant_name in tenants:
        processed, skipped, duplicates, rejected = push_tenant(tenant_name, orion_client, log)
        totals["processed"] += processed
        totals["skipped"] += skipped
        totals["duplicates"] += duplicates
        totals["rejected"] += rejected

    log(f"All tenants done. processed={totals['processed']} skipped={totals['skipped']} "
        f"duplicates={totals['duplicates']} rejected={totals['rejected']}")

    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(LOGS_DIR, f"push_0817_only_{ts}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\nLog saved to: {log_path}")


if __name__ == "__main__":
    main()
