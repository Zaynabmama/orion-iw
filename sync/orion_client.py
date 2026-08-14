"""Push side. Real Orion endpoint + auth obtained from the user 2026-08-14 (was
the single biggest blocker on this project). Login is POST /api/auth/login with
a username/password -> short-lived (~24h) Bearer JWT; invoices are created via
POST /api/invoices/data with that token.

Until ORION_BASE_URL/ORION_USERNAME/ORION_PASSWORD are set in .env, push_payload()
still writes to the local outbox/ folder instead, so the rest of the pipeline
(pull + mapping) can keep being built and tested without touching the real API.
"""

import base64
import json
import os
import time
from datetime import datetime, timezone

import requests

OUTBOX_ROOT = os.path.join(os.path.dirname(__file__), "outbox")


def _decode_jwt_exp(token):
    """Reads the unverified 'exp' claim (unix seconds) out of a JWT so the client
    knows when to log in again, without adding a JWT library as a dependency."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("exp")
    except (IndexError, ValueError, TypeError):
        return None


class OrionClient:
    """Bearer-token client for the Orion ERP invoice API (POST /api/auth/login,
    GET/POST /api/invoices/...)."""

    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._token = None
        self._expires_at = 0

    def _login(self):
        resp = requests.post(
            f"{self.base_url}/api/auth/login",
            json={"username": self.username, "password": self.password},
            timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["token"]
        exp = _decode_jwt_exp(self._token)
        # 60s safety margin so a request doesn't start with a token that's about
        # to expire mid-flight; falls back to 1h if exp isn't readable.
        self._expires_at = (exp - 60) if exp else (time.time() + 3600)

    def _headers(self):
        if not self._token or time.time() >= self._expires_at:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    def get_invoice(self, txn_code, document_no):
        """GET /api/invoices/{txn_code}/{document_no}, e.g. get_invoice("INVC", "260312325")."""
        url = f"{self.base_url}/api/invoices/{txn_code}/{document_no}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code == 401:
            self._login()
            resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def create_invoice(self, payload):
        """POST /api/invoices/data. Returns the response dict, e.g.
        {"Success": "True", "SysID": ..., "DocumentNo": ..., ...}."""
        url = f"{self.base_url}/api/invoices/data"
        headers = {**self._headers(), "Content-Type": "application/json"}
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code == 401:
            self._login()
            headers = {**self._headers(), "Content-Type": "application/json"}
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if not resp.ok:
            # Orion returns this as a 400 when the same "Cloud Invoice No" was
            # already posted in an earlier run (e.g. one that died partway through
            # a batch, since the sync checkpoint only advances after a whole
            # tenant's run finishes). That's not a real failure, the invoice is
            # already in Orion, so it gets its own exception type: main.py treats
            # it as a benign skip and keeps going, instead of aborting the batch.
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if "duplicate" in str(body.get("Response", "")).lower():
                raise DuplicateInvoiceError(body.get("Response", ""), response=body)
            # raise_for_status()'s default message drops the response body, which is
            # exactly where Orion puts the actual validation error on a 400. Surface
            # it instead of just "400 Client Error".
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason} for {url}. Response body: {resp.text}",
                response=resp,
            )
        return resp.json()


class DuplicateInvoiceError(Exception):
    """Raised when Orion rejects a create because that Cloud Invoice No already
    exists there. response: Orion's parsed JSON body."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def push_payload(payload, invoice_id, tenant_name, orion_client=None):
    """Sends (or, if no OrionClient configured yet, saves) one Orion invoice payload.

    tenant_name: used to keep each tenant's saved-locally output separate
    (outbox/<tenant_name>/...); irrelevant once orion_client is real, since the
    Orion endpoint itself doesn't need to know which BSS tenant something came from.

    Returns a dict describing what happened, for logging by the caller.
    """
    if orion_client:
        result = orion_client.create_invoice(payload)
        # Orion reports failure as "Success": "False" in a 200 response rather
        # than an HTTP error status, so a non-raising call can still be a failure.
        if str(result.get("Success", "True")).lower() != "true":
            raise RuntimeError(f"Orion rejected invoice {invoice_id}: {result}")
        return {"mode": "posted", "response": result}

    tenant_outbox = os.path.join(OUTBOX_ROOT, tenant_name)
    os.makedirs(tenant_outbox, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = os.path.join(tenant_outbox, f"{ts}_{invoice_id}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return {"mode": "saved_local", "file": filename}
