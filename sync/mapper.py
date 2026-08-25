"""Maps a Mindware BSS invoice (+ its resolved accounts) into the Orion ERP JSON shape.

Target shape (2026-08-11): the human-readable "Cloud Invoice" field-name schema the
user showed from a real Orion invoice (partner TRANSCO TRADING, UAE, invoice
DNAE-26-006948), e.g. "Company Code", "Customer code", "Item Taxes" with
itedTedBasis/itedNetFcAmt/itedNetLcAmt. This replaced the earlier flat
USERID/APICODE/head/Item shape copied from the Autodesk SF Orderdesk sample,
which is no longer believed to be the right target for this integration.

Verified against the user's real numbers before trusting the shape: itedNetFcAmt
(0.22) = itedTaxableFcAmt (4.48) * itedTedRate (5.0) / 100, and itedNetLcAmt (0.06)
= itedNetFcAmt * the local->USD exchange rate (0.272294078 for AED). So Orion's
"Fc" (foreign currency) is the invoice's local transaction currency (AED/SAR/...)
and "Lc" (local currency) is actually Orion's USD group/ledger currency. Both
reconciled exactly, so this mapping is trusted, not guessed.

Important: invoice['account'] is the END CUSTOMER (the actual license holder/
subscriber, e.g. "Saudi Constructioneers Limited"). Mindware does not bill them
directly. invoice['billingTo'] is the PARTNER/reseller Mindware actually invoices,
and it's the partner's account that carries the Orion `code` (confirmed: real
billingTo accounts cross-matched against /api/accounts all have codes, e.g.
"Delta Line International -DLI" -> "CK1D0030"). So Customer code/ship/bill
addresses and account_config below are all keyed off the *billing* account, not
the end customer. "End User Details" needs the end customer's own account too
(for its country), fetched separately (see main.py).

There are 6 Mindware BSS tenants, each a fully separate database, so account ids,
payment-method ids, and Orion config can all differ per tenant. So none of the
lookup tables below are module-level constants; they're passed into
build_orion_payload() per call, sourced from that tenant's config file
(see tenants/_template.json and main.py).
"""

from datetime import datetime


def _parse(iso_datetime_str):
    return datetime.fromisoformat(iso_datetime_str.replace("Z", "+00:00"))


def format_date_slash(dt):
    """'Invoice date' / 'Delivery date' / 'Created date' style: 'DD/MM/YYYY'."""
    return dt.strftime("%d/%m/%Y")


def format_date_mon2y(dt):
    """'Billing Start/End Date' style, from the real sample: 'DD-MON-YY' (2-digit year)."""
    return dt.strftime("%d-%b-%y").upper()


def format_date_iso(dt):
    """Date embedded inside 'Item description', from the real sample: 'YYYY-MM-DD'."""
    return dt.strftime("%Y-%m-%d")


def _num(value):
    """JSON number, int when the value is whole (matches the real sample's '2', not '2.0')."""
    value = round(value, 4)
    return int(value) if value == int(value) else value


class MissingCustomerCodeError(Exception):
    pass


class MissingPaymentTermError(Exception):
    pass


class MissingAccountConfigError(Exception):
    pass


class MissingItemCodeError(Exception):
    pass


class UnhandledDiscountError(Exception):
    pass


# Orion doesn't book marketplace lines against per-product SKUs. BSS product
# codes are Microsoft part numbers like "CFQ7TTC0LH18:0001", but Orion books
# against a small set of consolidated item codes (MS-CNS, MSAZ-CNS, ...), resolved by
# keyword match on the product name. Copied verbatim from the existing
# report-conversion script's KEYWORD_MAP (2026-08-10) so the two stay easy to
# diff; matching is case-insensitive substring, first hit wins, so order matters.
KEYWORD_MAP = {
    ("windows server", "window server", "Office LTSC Standard", "MSPER-CNS"): "MSPER-CNS",
    ("azure subscription", "MSAZ-CNS"): "MSAZ-CNS",
    ("azure prepayment", "MSAZ-CNS"): "MSAZ-CNS",
    ("google workspace", "GL-WSP-CNS"): "GL-WSP-CNS",
    ("m365", "microsoft 365", "office 365", "exchange online", "Microsoft Defender for Endpoint P1", "MS-CNS"): "MS-CNS",
    ("POWERPLATFORM - Power Apps Premium (New Commerce)", "powerapps premium", "power apps premium", "Power Apps Premium", "MS-CNS"): "MS-CNS",
    ("POWERPLATFORM - Power Automate per user plan (New Commerce)", "power automate per user", "Power Automate per user", "MS-CNS"): "MS-CNS",
    ("POWERPLATFORM - Power Automate unattended RPA add-on (New Commerce)", "MS-CNS"): "MS-CNS",
    ("Office LTSC Professional Plus 2024 (Commercial)", "MSPER-CNS"): "MSPER-CNS",
    ("Excel LTSC 2024", "excel ltsc", "MSPER-CNS"): "MSPER-CNS",
    ("Project Professional 2024 (Commercial) (Subs ID)", "project professional 2024", "MSPER-CNS"): "MSPER-CNS",
    ("SQL Server 2025 - 1 User CAL (Commercial)", "sql server 2025 - 1 user cal", "MSPER-CNS"): "MSPER-CNS",
    ("SQL Server 2025 Enterprise core - 2 core License Pack (Commercial)", "sql server 2025 enterprise core", "MSPER-CNS"): "MSPER-CNS",
    ("SQL Server 2025 Enterprise - 2 core License Pack - 3 year", "sql server 2025 enterprise - 2 core license pack - 3 year", "MSPER-CNS"): "MSPER-CNS",
    # Not in the source script's map either (a gap there too), seen on real KSA invoices
    # 2026-08-10; every other SQL Server 2025 product maps to MSPER-CNS.
    ("SQL Server 2025 Standard core - 2 core License Pack (Commercial)", "sql server 2025 standard core", "MSPER-CNS"): "MSPER-CNS",
    ("SQL Server 2025 Standard edition Perpetual 1 Server License (Commercial)", "sql server 2025 standard edition perpetual 1 server license", "MSPER-CNS"): "MSPER-CNS",
    ("Visual Studio Professional 2026 (Commercial)", "visual studio professional 2026", "MSPER-CNS"): "MSPER-CNS",
    ("Windows 11 Enterprise LTSC 2024 Upgrade (Commercial)", "windows 11 enterprise ltsc 2024 upgrade", "MSPER-CNS"): "MSPER-CNS",
    ("Windows 10 Enterprise LTSC 2021 Upgrade (Commercial)", "windows 10 enterprise ltsc 2021 upgrade", "MSPER-CNS"): "MSPER-CNS",
    ("Azure Plan Reserved Instances", "azure plan reserved instances", "MSRI-CNS"): "MSRI-CNS",
    ("acronis", "AS-CNS"): "AS-CNS",
    ("windows 11 pro", "MSPER-CNS"): "MSPER-CNS",
    ("windows 365", "MS-CNS"): "MS-CNS",
    ("windows 11 iot enterprise", "MSPER-CNS"): "MSPER-CNS",
    ("windows 10 iot enterprise", "MSPER-CNS"): "MSPER-CNS",
    ("power bi", "MS-CNS"): "MS-CNS",
    ("planner", "project plan", "MS-CNS"): "MS-CNS",
    ("power automate premium", "MS-CNS"): "MS-CNS",
    ("power automate process", "MS-CNS"): "MS-CNS",
    ("visio", "MS-CNS"): "MS-CNS",
    ("Microsoft Entra ID Governance (Education Faculty Pricing)", "Power Apps Premium (Non-Profit Pricing)", "MS-CNS"): "MS-CNS",
    ("MSRI-CNS",): "MSRI-CNS",
    ("dynamics 365", "MS-CNS"): "MS-CNS",
    ("AWS Account", "AWS"): "AWS-UTILITIES-CNS",
    ("minecraft education per user", "MS-CNS"): "MS-CNS",
}


def resolve_item_code(product_name):
    """BSS product name -> consolidated Orion item code, via KEYWORD_MAP."""
    name = (product_name or "").lower()
    for keywords, code in KEYWORD_MAP.items():
        for keyword in keywords:
            if keyword.lower() in name:
                return code
    raise MissingItemCodeError(
        f"No KEYWORD_MAP entry matches product name {product_name!r}. "
        f"Add a keyword for it in mapper.py."
    )


def build_orion_payload(invoice, billing_account, end_customer_account, orion_config,
                         payment_term_map, account_config, run_date=None):
    """invoice: a BSS invoice dict (from BSSClient.iter_invoices, with items/customFields included).
    billing_account: resolved BSS account (BSSClient.get_account) for the *billing*
        party: invoice['billingTo']['id'] if present, else invoice['account']['id'].
    end_customer_account: resolved BSS account for invoice['account'] (the end
        customer), needed only for its country, for "End User Details".
    orion_config: this tenant's dict of {company_code, txn_code, ship_mode,
        doc_location, sales_location, del_location, tax_code, tax_percent,
        mode_of_payment, payment_mode, ...}. Locations/tax are per-tenant, keyed
        off the tenant's invoice prefix (DNSA/DNKW/...) in the existing
        report-conversion script. ("Created by user ID" was dropped from the
        outgoing payload per the user, 2026-08-14; created_by_user_id in the
        tenant config files is now unused.)
    payment_term_map: this tenant's {BSS payment method id (str) -> Orion "Terms code"}.
    account_config: this tenant's {BSS billing account id (str) -> {salesman}}. An
        optional "_default" key is used for any billing account id with no entry
        of its own (for tenants where every partner shares one salesman code).
    run_date: datetime to stamp as "Created date" (defaults to now), for when the
        sync actually creates this record in Orion, not the BSS invoice date.

    Returns a dict shaped like the real Orion "Cloud Invoice" sample the user
    provided (2026-08-11), for one invoice -> one Orion invoice call.
    """
    end_customer = invoice["account"]
    billing_ref = invoice.get("billingTo") or end_customer
    billing_id = billing_ref["id"]

    config = account_config.get(str(billing_id)) or account_config.get("_default")
    if config is None:
        raise MissingAccountConfigError(
            f"No Orion account config for BSS billing account id {billing_id} "
            f"({billing_ref.get('name')}). Add it to this tenant's account_config, "
            f"or add a \"_default\" entry if every partner in this tenant uses the same salesman."
        )

    # .strip(): BSS account codes have been seen with stray leading/trailing
    # whitespace (e.g. "CK1E0177 "), which Orion's exact-match customer lookup
    # rejects as "Customer not found" even though the customer exists there.
    customer_code = ((billing_account or {}).get("code") or "").strip() or None
    if not customer_code:
        raise MissingCustomerCodeError(
            f"BSS billing account id {billing_id} ({billing_ref.get('name')}) has no "
            f"'code' set in Mindware BSS. It needs one there before this invoice can sync."
        )

    # "Invoice date"/"Delivery date" are stamped as the date this sync actually
    # posts to Orion, not the original BSS invoice date. Confirmed with the user
    # 2026-08-14 after Orion rejected a backdated invoice with "Invoice Date cannot
    # be less than today's date". Same value as "Created date" below.
    #
    # Deliberately local (naive), not UTC: fixed 2026-08-16 after the same "cannot
    # be less than today" rejection recurred at 2 AM Saudi time (UTC+3), where
    # datetime.now(timezone.utc) still reads as 23:00 the PREVIOUS day. Orion
    # evaluates "today" against local wall-clock time, so this must match that,
    # not UTC. Relies on the server's own clock being set to the correct local
    # timezone (confirmed true for the deployment machine).
    posting_dt = run_date or datetime.now()
    ship_address = f"{customer_code}-B"
    bill_address = ship_address  # same formula as ShipAddress in every real sample seen.

    # The partner's real purchase-order number lives in a BSS custom field named
    # "LPO number" (id 850), nested as groupFields[].values[0].value. Often empty,
    # since not every partner submits an LPO. NOTE: the real sample the user provided
    # has no field for this at all; unclear if this schema drops it or if it's
    # just absent on that particular invoice. Kept as a guess pending confirmation
    # (see mapper module docstring / conversation notes).
    lpo_number = ""
    for cf in invoice.get("customFields") or []:
        for gf in cf.get("groupFields") or []:
            if gf.get("name") == "LPO number":
                values = gf.get("values") or []
                if values and values[0].get("value"):
                    lpo_number = str(values[0]["value"])[:29]

    payment_method = invoice.get("paymentMethod") or {}
    terms_code = payment_term_map.get(payment_method.get("id"))
    if terms_code is None:
        raise MissingPaymentTermError(
            f"No payment_term_map entry for BSS payment method "
            f"{payment_method.get('id')} ({payment_method.get('name')}) on invoice {invoice['id']}."
        )

    # BSS API amounts (unitPrice/total/unitCost) are in invoice['currency'] (USD),
    # but the booked, customer-facing document is in invoice['transactionCurrency']
    # (e.g. AED) at invoice['exchangeRate'] (USD->local, e.g. 3.6725). Orion
    # invoices are entered in local currency (what the manual process types in
    # today). "Exchange rate" is emitted local->USD, matching both the existing
    # report script's EXCHANGE_RATE_MAP convention and the real sample (0.272294078
    # for AED).
    usd_to_local = invoice.get("exchangeRate") or 1
    local_currency = invoice.get("transactionCurrency") or invoice.get("currency", "")
    orion_rate = round(1 / usd_to_local, 10)

    # Every BSS line seen so far has a zero discount (unitPrice is already the net
    # partner price). The real sample DOES have a "Discount percentage" field, so
    # unlike the old flat schema this isn't a hard blocker; discount% is passed
    # through directly below. But FC actual value's discount formula is UNVERIFIED
    # (no real non-zero-discount invoice has been seen to check the math against).
    tax_percent = orion_config.get("tax_percent")
    tax_code = orion_config.get("tax_code", "")

    items = []
    for item in invoice.get("items", []):
        product = item.get("product") or {}
        # Some lines (seen 2026-08-14, e.g. DNSA-26-003827) carry no linked catalog
        # product at all. product.name is unavailable, but item['description']
        # has the same real product name ("M365 - Microsoft 365 Business Standard
        # (New Commerce)") that a normal line would've gotten from product.name.
        product_name = product.get("name") or item.get("description") or ""
        subscriptions = item.get("subscriptions") or []
        sub_id = subscriptions[0]["id"] if subscriptions else ""
        start_dt = _parse(item["startDate"]) if item.get("startDate") else None
        end_dt = _parse(item["endDate"]) if item.get("endDate") else None

        # "Item description" in the real sample embeds product name, subscription id,
        # BSS product/part code, and the billing period as "YYYY-MM-DD to YYYY-MM-DD"
        # all in one string: "M365 - ... (New Commerce) (<sub-id>) | <part-code> |
        # 2026-08-02 to 2026-08-06". Perpetual lines have no subscription/dates.
        desc = product_name
        if sub_id:
            desc += f" ({sub_id})"
        if product.get("code"):
            desc += f" | {product['code']}"
        if start_dt and end_dt:
            desc += f" | {format_date_iso(start_dt)} to {format_date_iso(end_dt)}"

        # Precise (unrounded) local-currency rate, used for the line total so a
        # rounded display Rate can't drift the total on high-quantity lines.
        # E.g. 0.17 USD x 3.75 rounded to 2dp (0.64) would overbill a 35,000-unit
        # line by 87.50 SAR if the total were derived from the rounded Rate instead
        # of computed directly like this.
        precise_rate = item["unitPrice"] * usd_to_local
        # Manual debit-note lines (no linked BSS product/subscription/asset, e.g.
        # DNSA-26-003908 "AZURE PREPAYMENT", DNSA-26-003904's SQL/Windows Server
        # lines) are typed in by the team without a separate cost entry, so BSS's
        # unitCost comes back 0 even though a real price was charged. Confirmed
        # with the user 2026-08-19: fall back to the sell price (unitPrice) as
        # the cost for these, rather than sending "0.00".
        unit_cost_usd = item.get("unitCost") or item["unitPrice"]
        quantity = item["quantity"]
        fc_value = round(precise_rate * quantity, 2)
        discount_pct = (item.get("discount") or {}).get("value") or 0
        fc_actual = round(fc_value * (1 - discount_pct / 100), 2)

        tax_percent_num = float(tax_percent) if tax_percent not in (None, "") else 0.0
        net_fc = round(fc_actual * tax_percent_num / 100, 2)
        net_lc = round(net_fc * orion_rate, 2)

        items.append({
            "Item code": resolve_item_code(product_name),
            "Item description": desc,
            "Grade code 1": "NA",
            "Grade code 2": "NA",
            # Confirmed with the Orion team (2026-08-11): always "NOS", regardless
            # of BSS's billing frequency (Annually/Monthly/License/...).
            "UOM": "NOS",
            "Location code": orion_config["sales_location"],
            "Delivery location code": orion_config["del_location"],
            "Invoiced quantity": _num(quantity),
            "Item Rate": round(precise_rate, 2),
            "Foreign currency value": fc_value,
            "Discount percentage": _num(discount_pct),
            "FC actual value": fc_actual,
            # Reuses posting_dt (computed once above) rather than a second
            # separate "now" call, so header and item dates can never disagree.
            "Created date": format_date_slash(posting_dt),
            # NB: "SubcriptionID" (missing the 's' in "Subscription") is the exact
            # spelling in the real sample, kept as-is since it's presumably the
            # literal field name Orion's API expects.
            # Confirmed with the user 2026-08-19: lines with no real BSS
            # subscription (perpetual/manual lines) send the literal string
            # "Subs ID" here instead of leaving it empty.
            "SubcriptionID": sub_id or "Subs ID",
            "Billing Start Date": format_date_mon2y(start_dt) if start_dt else "",
            "Billing End Date": format_date_mon2y(end_dt) if end_dt else "",
            # RESOLVED 2026-08-18, supersedes the 2026-08-11 answer: this is the
            # per-unit cost (BSS unitCost), matching the field's own name and the
            # same per-unit pattern as "Item Rate". Confirmed against a real
            # Triennial-installment invoice (DNSA-26-003802): unitCost (1614.18
            # USD) x exchangeRate (3.75) = 6053.18, which the user confirmed is
            # the correct value; the old totalCost-based formula gave 42372.23,
            # which was wrong. Sent as a string to match the real sample's
            # "Unit Cost Price": "2.28" (a string, unlike the numeric fields
            # around it).
            "Unit Cost Price": f"{unit_cost_usd * usd_to_local:.2f}",
            "Item Taxes": [
                {
                    # "R" is the only value seen so far (rate-based?); meaning unconfirmed.
                    "itedTedBasis": "R",
                    "itedTedRate": tax_percent_num,
                    "itedTedCurrCode": local_currency,
                    "itedNetFcAmt": net_fc,
                    "itedNetLcAmt": net_lc,
                    "TED code": tax_code,
                }
            ],
        })

    end_country = ((end_customer_account or {}).get("country") or {}).get("name", "")
    end_user_details = end_customer.get("name", "")
    if end_country:
        end_user_details += f" ; {end_country}"

    created_date = format_date_slash(posting_dt)

    return {
        "Company Code": orion_config["company_code"],
        "Transaction Code": orion_config["txn_code"],
        "Invoice date": created_date,
        "Document source location code": orion_config["doc_location"],
        "Customer code": customer_code,
        "Location code": orion_config["sales_location"],
        "Delivery location code": orion_config["del_location"],
        "Ship to address code": ship_address,
        "Bill to address code": bill_address,
        "Delivery date": created_date,
        "Document currency code": local_currency,
        "Exchange rate": orion_rate,
        "Salesman code": config["salesman"],
        "Terms code": terms_code,
        "Created date": created_date,
        "End User Details": end_user_details,
        "Inco Terms": orion_config["ship_mode"],
        # BSS invoice number, also the natural dedupe key (Orion should reject a
        # second POST carrying the same Cloud Invoice No). Confirmed with the user
        # 2026-08-19: append a trailing "-" (e.g. "DNSA-26-003934-").
        "Cloud Invoice No": invoice.get("code", "") + "-",
        # Mode Of Payment/Payment Mode: the real sample showed "OC"/"5" for that
        # invoice's own payment method. Orion started rejecting an empty
        # "Payment Mode" as a mandatory field 2026-08-20, so both are now sent
        # as this tenant's configured constants ("OC"/"5" for ksa_production).
        "Mode Of Payment": orion_config.get("mode_of_payment", ""),
        "Payment Mode": orion_config.get("payment_mode", ""),
        # Sent as fixed constants per the user, 2026-08-14: every invoice this
        # sync creates is new/unpaid at creation time.
        "Invoice status": 1,
        "Status": "Unpaid",
        # Field renamed "LPO Number" -> "Customer LPO" per the user, 2026-08-14.
        "Customer LPO": lpo_number,
        "Items": items,
    }
