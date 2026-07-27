import base64
import json
import threading
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import get_tokens, save_tokens


ACCESS_TOKEN_MARGIN_SECONDS = 30
REFRESH_TOKEN_MARGIN_SECONDS = 60
DEFAULT_RETRY_TOTAL = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.5
DEFAULT_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)


def build_retry_session():
    retry = Retry(
        total=DEFAULT_RETRY_TOTAL,
        connect=DEFAULT_RETRY_TOTAL,
        read=DEFAULT_RETRY_TOTAL,
        status=DEFAULT_RETRY_TOTAL,
        backoff_factor=DEFAULT_RETRY_BACKOFF_SECONDS,
        status_forcelist=DEFAULT_RETRY_STATUS_CODES,
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "apilo-panel/1.0"})
    return session


class ApiloClientError(RuntimeError):
    pass


def parse_iso(value):
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_expired(iso_value, margin_seconds=0):
    dt = parse_iso(iso_value)
    if not dt:
        return True
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if margin_seconds:
        now = now + timedelta(seconds=margin_seconds)
    return dt <= now


class ApiloClient:
    _shared_session = None
    _shared_session_lock = threading.Lock()

    def __init__(
        self,
        base_url,
        client_id,
        client_secret,
        developer_id,
        db_path,
        grant_type=None,
        auth_token=None,
        timeout=30,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.developer_id = developer_id
        self.db_path = db_path
        self.grant_type = grant_type
        self.auth_token = auth_token
        self.timeout = timeout
        self._token_lock = threading.Lock()
        self.session = self._get_shared_session()

    @classmethod
    def _get_shared_session(cls):
        if cls._shared_session is None:
            with cls._shared_session_lock:
                if cls._shared_session is None:
                    cls._shared_session = build_retry_session()
        return cls._shared_session

    def _basic_auth_header(self):
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _ensure_tokens(self):
        with self._token_lock:
            tokens = get_tokens(self.db_path)
            if not tokens:
                if not self.grant_type or not self.auth_token:
                    raise ApiloClientError(
                        "Missing APILO_GRANT_TYPE/APILO_AUTH_TOKEN and no stored tokens."
                    )
                return self._fetch_tokens(self.grant_type, self.auth_token)

            if tokens.get("access_token") and not is_expired(
                tokens.get("access_token_expires_at"),
                margin_seconds=ACCESS_TOKEN_MARGIN_SECONDS,
            ):
                return tokens

            refresh_token = tokens.get("refresh_token")
            if not refresh_token or is_expired(
                tokens.get("refresh_token_expires_at"),
                margin_seconds=REFRESH_TOKEN_MARGIN_SECONDS,
            ):
                if not self.grant_type or not self.auth_token:
                    raise ApiloClientError("Refresh token missing or expired.")
                return self._fetch_tokens(self.grant_type, self.auth_token)

            return self._fetch_tokens("refresh_token", refresh_token)

    def _force_refresh_tokens(self):
        with self._token_lock:
            tokens = get_tokens(self.db_path) or {}
            refresh_token = tokens.get("refresh_token")
            refresh_expire_at = tokens.get("refresh_token_expires_at")
            if refresh_token and not is_expired(
                refresh_expire_at,
                margin_seconds=REFRESH_TOKEN_MARGIN_SECONDS,
            ):
                return self._fetch_tokens("refresh_token", refresh_token)
            if self.grant_type and self.auth_token:
                return self._fetch_tokens(self.grant_type, self.auth_token)
            raise ApiloClientError("Refresh token missing or expired.")

    def _build_tokens_payload(self, data, grant_type, original_token):
        existing = get_tokens(self.db_path) or {}
        refresh_token = data.get("refreshToken") or existing.get("refresh_token")
        refresh_expire_at = data.get("refreshTokenExpireAt") or existing.get(
            "refresh_token_expires_at"
        )
        if grant_type == "refresh_token":
            refresh_token = refresh_token or original_token
        tokens = {
            "access_token": data.get("accessToken"),
            "access_token_expires_at": data.get("accessTokenExpireAt"),
            "refresh_token": refresh_token,
            "refresh_token_expires_at": refresh_expire_at,
        }
        required = (
            "access_token",
            "access_token_expires_at",
            "refresh_token",
            "refresh_token_expires_at",
        )
        missing = [name for name in required if not tokens.get(name)]
        if missing:
            raise ApiloClientError(
                "Token request failed: missing token fields "
                + ", ".join(sorted(missing))
                + "."
            )
        return tokens

    def _fetch_tokens(self, grant_type, token):
        url = f"{self.base_url}/rest/auth/token/"
        payload = {"grantType": grant_type, "token": token}
        if self.developer_id:
            payload["developerId"] = self.developer_id
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {self._basic_auth_header()}",
        }
        try:
            response = self.session.post(
                url, headers=headers, data=json.dumps(payload), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ApiloClientError("Token request failed: connection error.") from exc
        if response.status_code not in (200, 201):
            raise ApiloClientError(f"Token request failed: {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiloClientError("Token request failed: invalid JSON response.") from exc
        if not isinstance(data, dict):
            raise ApiloClientError("Token request failed: invalid response format.")
        tokens = self._build_tokens_payload(data, grant_type, token)
        save_tokens(self.db_path, tokens)
        saved = get_tokens(self.db_path)
        if not saved:
            raise ApiloClientError("Token request failed: could not save tokens.")
        return saved

    def _send_request(self, method, url, headers, params, json_body):
        try:
            return self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiloClientError("Apilo API error: connection error.") from exc

    def _request(self, method, path, params=None, json_body=None):
        url = f"{self.base_url}{path}"
        tokens = self._ensure_tokens()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tokens['access_token']}",
        }
        response = self._send_request(method, url, headers, params, json_body)
        if response.status_code == 401:
            tokens = self._force_refresh_tokens()
            headers["Authorization"] = f"Bearer {tokens['access_token']}"
            response = self._send_request(method, url, headers, params, json_body)
        if response.status_code >= 400:
            raise ApiloClientError(f"Apilo API error: {response.status_code}")
        if not response.text:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiloClientError("Apilo API error: invalid JSON response.") from exc

    def list_products(self, limit=2000):
        offset = 0
        all_products = []
        while True:
            data = self._request(
                "GET",
                "/rest/api/warehouse/product/",
                params={"limit": limit, "offset": offset},
            )
            if not isinstance(data, dict):
                raise ApiloClientError("Apilo API error: invalid product list response.")
            products = data.get("products", [])
            if not isinstance(products, list):
                raise ApiloClientError("Apilo API error: invalid product page.")
            all_products.extend(products)
            total = data.get("totalCount")
            if total is not None:
                try:
                    total = int(total)
                except (TypeError, ValueError) as exc:
                    raise ApiloClientError("Apilo API error: invalid product count.") from exc
                if total < 0:
                    raise ApiloClientError("Apilo API error: invalid product count.")
            if total is None:
                if len(products) < limit:
                    break
            else:
                if len(all_products) >= total:
                    break
                if not products:
                    raise ApiloClientError(
                        "Apilo API error: incomplete product pagination response."
                    )
            offset += limit
        if total is not None and len(all_products) != total:
            raise ApiloClientError("Apilo API error: incomplete product snapshot.")
        return all_products

    def update_quantities(self, updates):
        if not updates:
            return {"changes": 0}
        return self._request(
            "PATCH",
            "/rest/api/warehouse/product/",
            json_body=updates,
        )

    def test_connection(self):
        return self._request("GET", "/rest/api/")

    def get_product(self, product_id):
        return self._request("GET", f"/rest/api/warehouse/product/{product_id}/")

    def get_product_media(self, product_ids, only_main=True):
        if not product_ids:
            return []
        params = {
            "onlyMain": "1" if only_main else "0",
        }
        for pid in product_ids:
            params.setdefault("productIds[]", []).append(pid)
        data = self._request("GET", "/rest/api/warehouse/product/media/", params=params)
        return data.get("media", [])

    def list_orders(
        self,
        updated_after=None,
        updated_before=None,
        created_after=None,
        created_before=None,
        ordered_after=None,
        ordered_before=None,
        payment_status=None,
        limit=512,
    ):
        offset = 0
        all_orders = []
        while True:
            params = {"limit": limit, "offset": offset}
            if updated_after:
                params["updatedAfter"] = updated_after
            if updated_before:
                params["updatedBefore"] = updated_before
            if created_after:
                params["createdAfter"] = created_after
            if created_before:
                params["createdBefore"] = created_before
            if ordered_after:
                params["orderedAfter"] = ordered_after
            if ordered_before:
                params["orderedBefore"] = ordered_before
            if payment_status is not None:
                params["paymentStatus"] = payment_status
            data = self._request("GET", "/rest/api/orders/", params=params)
            orders = data.get("orders", [])
            all_orders.extend(orders)
            total = data.get("totalCount")
            if total is None:
                if len(orders) < limit:
                    break
            else:
                if len(all_orders) >= total:
                    break
            offset += limit
        return all_orders

    def list_price_calculated(self, price_id, limit=512):
        offset = 0
        all_prices = []
        while True:
            params = {"price": price_id, "limit": limit, "offset": offset}
            data = self._request("GET", "/rest/api/warehouse/price-calculated/", params=params)
            items = data.get("list", [])
            all_prices.extend(items)
            total = data.get("totalCount")
            if total is None:
                if len(items) < limit:
                    break
            else:
                if len(all_prices) >= total:
                    break
            offset += limit
        return all_prices

    def list_auctions(self, limit=512):
        offset = 0
        all_auctions = []
        while True:
            params = {"limit": limit, "offset": offset}
            data = self._request("GET", "/rest/api/sale/auction/", params=params)
            if isinstance(data, dict):
                auctions = data.get("auctions", [])
                total = data.get("totalCount")
            else:
                auctions = data or []
                total = None
            all_auctions.extend(auctions)
            if total is None:
                if len(auctions) < limit:
                    break
            else:
                if len(all_auctions) >= total:
                    break
            offset += limit
        return all_auctions

    def list_sale_platforms(self):
        data = self._request("GET", "/rest/api/sale/")
        return data.get("platforms", []) if isinstance(data, dict) else []

    def get_order_status_map(self):
        return self._request("GET", "/rest/api/orders/status/map/") or []

    def get_shipment_status_map(self):
        return self._request("GET", "/rest/api/orders/shipment/status/map/") or []
