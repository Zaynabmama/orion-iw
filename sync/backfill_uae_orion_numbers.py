import argparse
import json
import re
from pathlib import Path

from bss_client import BSSClient


SUCCESS_LINE = re.compile(
    r"\[uae\] \[OK\] invoice (?P<invoice_id>[0-9a-f-]+) -> .*?"
    r"'DocumentNo': '(?P<document_no>[^']+)'"
)


def parse_successes(log_path):
    mappings = {}
    for line in log_path.read_text(encoding="utf-8").splitlines():
        match = SUCCESS_LINE.search(line)
        if match:
            mappings[match.group("invoice_id")] = match.group("document_no")
    return mappings


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill UAE BSS Orion Invoice Number values from a saved sync log."
    )
    parser.add_argument("log_path", type=Path, help="Saved server output containing [uae] [OK] lines")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write values to BSS; without this flag, only preview the mappings",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    mappings = parse_successes(args.log_path)
    print(f"Found {len(mappings)} successful UAE invoice mappings.")
    if not mappings:
        return

    config_path = Path(__file__).parent / "tenants" / "uae.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bss_config = config["bss"]
    field_id = bss_config["orion_invoice_number_field_id"]

    if not args.apply:
        print("Preview only. Add --apply to update BSS.")
        for invoice_id, document_no in mappings.items():
            print(f"{invoice_id} -> {document_no}")
        return

    client = BSSClient(
        base_url=bss_config["base_url"],
        token_url=bss_config["token_url"],
        username=bss_config["username"],
        password=bss_config["password"],
        client_id=bss_config.get("client_id"),
        client_secret=bss_config.get("client_secret"),
        api_version=bss_config.get("api_version", "3"),
    )
    for invoice_id, document_no in mappings.items():
        client.set_invoice_custom_field(invoice_id, field_id, document_no)
        print(f"Updated {invoice_id} -> {document_no}")


if __name__ == "__main__":
    main()
