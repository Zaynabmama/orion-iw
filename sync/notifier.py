"""Sends run-summary alert emails via Microsoft Graph (app-only client-credentials
auth, since this sender mailbox -- Donotreply@mindware.net -- isn't a real user
logging in interactively). Used by main.py to alert on skipped/failed invoices
once the nightly sync runs unattended, since a skip that isn't caught can be
silently lost forever (see mapper.py/main.py state-tracking notes).
"""

import os

import requests


def _get_graph_token(tenant_id, client_id, client_secret):
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_alert(subject, body_text, recipients):
    """recipients: list of email address strings. Reads GRAPH_TENANT_ID/
    GRAPH_CLIENT_ID/GRAPH_CLIENT_SECRET/GRAPH_SENDER from the environment
    (see .env) -- raises if any are missing rather than failing silently,
    since a broken alert path defeats the whole point of alerting."""
    tenant_id = os.environ["GRAPH_TENANT_ID"]
    client_id = os.environ["GRAPH_CLIENT_ID"]
    client_secret = os.environ["GRAPH_CLIENT_SECRET"]
    sender = os.environ["GRAPH_SENDER"]

    token = _get_graph_token(tenant_id, client_id, client_secret)
    url = f"https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
        },
        "saveToSentItems": "false",
    }
    resp = requests.post(
        url, json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=30
    )
    resp.raise_for_status()
