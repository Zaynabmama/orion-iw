import time
import requests


class BSSClient:
    """OAuth2 (password grant) client for the Mindware BSS / Infiterra Billing API v3."""

    def __init__(self, base_url, token_url, username, password, client_id, client_secret=None, api_version="3"):
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version
        self._access_token = None
        self._expires_at = 0

    def _fetch_token(self):
        data = {
            "grant_type": "password",
            "username": self.username,
            "password": self.password,
        }
        # This server rejects client_id/client_secret as body fields (invalid_client);
        # it expects them as an HTTP Basic Auth header instead (RFC 6749 2.3.1).
        auth = (self.client_id, self.client_secret) if self.client_id else None
        resp = requests.post(self.token_url, data=data, auth=auth, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + payload.get("expires_in", 3600) - 60

    def _token(self):
        if not self._access_token or time.time() >= self._expires_at:
            self._fetch_token()
        return self._access_token

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        # The API rejects requests with no version indicator (404 "InvalidApiVersionError");
        # it must be sent as this header, not a path segment or query param.
        headers = {"Authorization": f"Bearer {self._token()}", "X-Api-Version": self.api_version}
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 401:
            self._fetch_token()
            headers = {"Authorization": f"Bearer {self._token()}", "X-Api-Version": self.api_version}
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def iter_invoices(self, updated_since=None, page_size=100):
        """Yields invoice dicts (with items + customFields expanded), paginating through all results.

        updated_since: ISO datetime string, e.g. '2026-07-21T00:00:00'. When set, only
        invoices updated after this timestamp are returned (for incremental syncs).
        """
        page_index = 1
        while True:
            params = {
                "pageIndex": page_index,
                "pageSize": page_size,
                "include": "items,customFields",
                "$orderBy": "updatedAt asc",
            }
            if updated_since:
                params["$filter"] = f"updatedAt gt datetime'{updated_since}'"
            result = self._get("/api/invoices", params=params)
            invoices = result.get("data", [])
            if not invoices:
                break
            yield from invoices
            paging = result.get("paging") or {}
            if page_index >= paging.get("totalPages", page_index):
                break
            page_index += 1

    def get_account(self, account_id):
        return self._get(f"/api/accounts/{account_id}")
