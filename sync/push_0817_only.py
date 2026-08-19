"""One-off, targeted push for the 2026-08-17 KSA batch only.

Unlike main.py, this does NOT read or write state/ksa_production.json, so it
has no effect on the regular incremental sync's checkpoint. Run this once for
the 17/08 batch (the invoices deleted from Orion and being re-pushed with the
new "Cloud Invoice No" trailing "-" suffix), separately from any normal
nightly/manual main.py run.

Requires ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD in .env (real Orion
network access), so this only works run from ZaynabM_Super's machine.
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

DATE_FILTER = "invoiceDate ge datetime'2026-08-17T00:00:00' and invoiceDate lt datetime'2026-08-18T00:00:00'"


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

    with open(os.path.join(SYNC_DIR, "tenants", "ksa_production.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    bss = cfg["bss"]
    orion_config = cfg["orion"]
    payment_term_map = cfg["payment_term_map"]
    account_config = cfg["account_config"]
    invoice_prefix = orion_config.get("invoice_prefix")
    orion_invoice_number_field_id = bss.get("orion_invoice_number_field_id")

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
    log(f"[ksa_production] Pulled {len(invoices)} invoices dated 2026-08-17 from BSS.\n")

    account_cache = {}
    processed = skipped = duplicates = rejected = 0

    for invoice in invoices:
        invoice_id = invoice["id"]
        code = invoice.get("code") or ""

        if invoice_prefix and not code.startswith(invoice_prefix):
            log(f"[ksa_production] [SKIP] invoice {invoice_id} ({code}): prefix mismatch.")
            skipped += 1
            continue

        invoice_type = (invoice.get("type") or {}).get("type")
        if invoice_type != "Debit":
            log(f"[ksa_production] [SKIP] invoice {invoice_id} ({code}): type is {invoice_type!r}, not Debit.")
            skipped += 1
            continue

        invoice_status = (invoice.get("status") or {}).get("type")
        if invoice_status == "Canceled":
            log(f"[ksa_production] [SKIP] invoice {invoice_id} ({code}): status is Canceled.")
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
            log(f"[ksa_production] [SKIP] invoice {invoice_id} ({code}): {e}")
            skipped += 1
            continue

        try:
            result_push = push_payload(payload, invoice_id, "ksa_production", orion_client=orion_client)
        except DuplicateInvoiceError:
            log(f"[ksa_production] [DUPLICATE] invoice {invoice_id} ({code}) already exists in Orion, skipping.")
            duplicates += 1
            continue
        except InvoiceRejectedError as e:
            log(f"[ksa_production] [REJECTED] invoice {invoice_id} ({code}): {e}")
            rejected += 1
            continue

        log(f"[ksa_production] [OK] invoice {invoice_id} ({code}) -> {result_push}")

        if result_push.get("mode") == "posted":
            if not orion_invoice_number_field_id:
                log(f"[ksa_production] [FIELD-SKIP] invoice {invoice_id}: orion_invoice_number_field_id not configured.")
            else:
                document_no = (result_push.get("response") or {}).get("DocumentNo")
                if not document_no:
                    log(f"[ksa_production] [WARN] invoice {invoice_id}: no DocumentNo to write back.")
                else:
                    try:
                        client.set_invoice_custom_field(invoice_id, orion_invoice_number_field_id, str(document_no))
                    except Exception as e:
                        log(f"[ksa_production] [WARN] invoice {invoice_id}: could not write "
                            f"Orion Invoice Number back to BSS: {type(e).__name__}: {e}")
                    else:
                        log(f"[ksa_production] [FIELD] invoice {invoice_id}: wrote Orion Invoice "
                            f"Number {document_no!r} back to BSS field {orion_invoice_number_field_id}.")

        processed += 1

    log(f"\n[ksa_production] done. processed={processed} skipped={skipped} "
        f"duplicates={duplicates} rejected={rejected}")
    log("(state/ksa_production.json was NOT touched by this script.)")

    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(LOGS_DIR, f"push_0817_only_{ts}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\nLog saved to: {log_path}")


if __name__ == "__main__":
    main()
