import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from bss_client import BSSClient


def parse_args():
    parser = argparse.ArgumentParser(
        description="Write Orion's DocumentNo back onto one BSS invoice's custom field, "
                     "for cases where main.py's automatic write-back never ran (e.g. the "
                     "invoice was later seen as a duplicate, which skips the write-back)."
    )
    parser.add_argument("tenant", help="tenant config name, e.g. ksa_production")
    parser.add_argument("invoice_id", help="BSS invoice id (uuid)")
    parser.add_argument("document_no", help="Orion DocumentNo to write")
    return parser.parse_args()


def main():
    args = parse_args()
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    config = json.loads((Path(__file__).parent / "tenants" / f"{args.tenant}.json").read_text(encoding="utf-8"))
    bss = config["bss"]
    field_id = bss["orion_invoice_number_field_id"]

    client = BSSClient(
        base_url=bss["base_url"],
        token_url=bss["token_url"],
        username=bss["username"],
        password=bss["password"],
        client_id=bss.get("client_id"),
        client_secret=bss.get("client_secret"),
        api_version=bss.get("api_version", "3"),
    )
    client.set_invoice_custom_field(args.invoice_id, field_id, args.document_no)
    print(f"Wrote field {field_id} = {args.document_no!r} on invoice {args.invoice_id}")


if __name__ == "__main__":
    main()
