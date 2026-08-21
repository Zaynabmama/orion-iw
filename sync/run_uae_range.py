import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

import main
from bss_client import BSSClient
from orion_client import OrionClient


class DateRangeBSSClient(BSSClient):
    def __init__(self, *args, start_date, end_date, **kwargs):
        super().__init__(*args, **kwargs)
        self.start_date = start_date
        self.end_date = end_date

    def iter_invoices(self, updated_since=None, page_size=100):
        page_index = 1
        while True:
            result = self._get("/api/invoices", params={
                "pageIndex": page_index,
                "pageSize": page_size,
                "include": "items,customFields",
                "$orderBy": "invoiceDate asc",
                "$filter": (
                    f"invoiceDate ge datetime'{self.start_date}T00:00:00' "
                    f"and invoiceDate lt datetime'{self.end_date}T00:00:00'"
                ),
            })
            invoices = result.get("data", [])
            if not invoices:
                break
            yield from invoices
            paging = result.get("paging") or {}
            if page_index >= paging.get("totalPages", page_index):
                break
            page_index += 1


def parse_args():
    parser = argparse.ArgumentParser(description="Sync UAE invoices for an invoice-date range.")
    parser.add_argument("--start", required=True, help="Inclusive invoice date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Exclusive invoice date, YYYY-MM-DD")
    return parser.parse_args()


def main_run():
    args = parse_args()
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
    config_path = Path(__file__).parent / "tenants" / "uae.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    bss = config["bss"]

    class ConfiguredDateRangeBSSClient(DateRangeBSSClient):
        def __init__(self, *client_args, **client_kwargs):
            super().__init__(*client_args, start_date=args.start, end_date=args.end, **client_kwargs)

    main.BSSClient = ConfiguredDateRangeBSSClient
    orion = OrionClient(
        os.environ["ORION_BASE_URL"],
        os.environ["ORION_USERNAME"],
        os.environ["ORION_PASSWORD"],
    )

    print(f"Running UAE invoices from {args.start} through the day before {args.end}")
    result = main.sync_tenant(config, orion, print)
    print("UAE RUN RESULT:", result)


if __name__ == "__main__":
    main_run()
