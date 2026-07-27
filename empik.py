import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests


EMPIK_BASE_URL = "https://marketplace.empik.com"
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
MAX_PAGE_COUNT = 100
MAX_ERROR_REPORT_BYTES = 10_000_000


class EmpikClientError(RuntimeError):
    pass


def _retry_delay(response, attempt):
    value = (response.headers.get("Retry-After") or "").strip()
    if value:
        try:
            return min(30.0, max(0.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(30.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, float(2**attempt))


class EmpikClient:
    def __init__(self, api_key, *, shop_id=None, session=None, timeout=(5, 30), sleep=time.sleep):
        api_key = str(api_key or "").strip()
        if not api_key:
            raise EmpikClientError("Brak klucza API EmpikPlace.")
        self.api_key = api_key
        self.shop_id = int(shop_id) if shop_id not in (None, "") else None
        if self.shop_id is not None and self.shop_id < 1:
            raise EmpikClientError("ID sklepu EmpikPlace musi być dodatnią liczbą.")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.sleep = sleep

    def _request(self, path, *, params=None, stream=False):
        request_params = dict(params or {})
        if self.shop_id is not None:
            request_params.setdefault("shop_id", self.shop_id)
        url = f"{EMPIK_BASE_URL}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": self.api_key,
            "User-Agent": "Apilo-Stock-Panel/Empik-read-only",
        }
        last_status = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    url,
                    params=request_params,
                    headers=headers,
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=False,
                )
            except requests.exceptions.Timeout as exc:
                raise EmpikClientError("Timeout połączenia z API EmpikPlace.") from exc
            except requests.exceptions.RequestException as exc:
                raise EmpikClientError("Błąd połączenia z API EmpikPlace.") from exc
            last_status = response.status_code
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 2:
                self.sleep(_retry_delay(response, attempt))
                continue
            if 300 <= response.status_code < 400:
                raise EmpikClientError(
                    "API EmpikPlace zwróciło niedozwolone przekierowanie."
                )
            if response.status_code == 401:
                raise EmpikClientError("Klucz API EmpikPlace jest nieprawidłowy lub wygasł.")
            if response.status_code == 403:
                raise EmpikClientError("Klucz API EmpikPlace nie ma dostępu do tego sklepu.")
            if response.status_code >= 400:
                raise EmpikClientError(f"API EmpikPlace zwróciło błąd HTTP {response.status_code}.")
            return response
        raise EmpikClientError(f"API EmpikPlace zwróciło błąd HTTP {last_status}.")

    def _get_json(self, path, *, params=None):
        response = self._request(path, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmpikClientError("API EmpikPlace zwróciło nieprawidłowy JSON.") from exc
        if not isinstance(payload, dict):
            raise EmpikClientError("API EmpikPlace zwróciło nieprawidłową strukturę danych.")
        return payload

    def test_connection(self):
        payload = self._get_json("/api/offers", params={"max": 1, "offset": 0})
        if not isinstance(payload.get("offers"), list):
            raise EmpikClientError("API EmpikPlace nie zwróciło listy ofert.")
        return True

    def list_offers(self):
        offers = []
        seen_offer_ids = set()
        expected_total = None
        offset = 0
        page_size = 100
        for _ in range(MAX_PAGE_COUNT):
            payload = self._get_json(
                "/api/offers",
                params={"max": page_size, "offset": offset},
            )
            page = payload.get("offers")
            if not isinstance(page, list):
                raise EmpikClientError("API EmpikPlace nie zwróciło listy ofert.")
            total_count = payload.get("total_count")
            if total_count is not None:
                try:
                    page_total = int(total_count)
                    if page_total < 0:
                        raise ValueError
                except (TypeError, ValueError) as exc:
                    raise EmpikClientError(
                        "API EmpikPlace zwróciło błędną liczbę ofert."
                    ) from exc
                if expected_total is None:
                    expected_total = page_total
                elif expected_total != page_total:
                    raise EmpikClientError(
                        "API EmpikPlace zmieniło liczbę ofert podczas stronicowania."
                    )
            if not page:
                if expected_total is not None and len(offers) < expected_total:
                    raise EmpikClientError(
                        "API EmpikPlace zwróciło niepełną listę ofert."
                    )
                break
            for offer in page:
                if not isinstance(offer, dict) or offer.get("offer_id") is None:
                    raise EmpikClientError("API EmpikPlace zwróciło ofertę bez identyfikatora.")
                offer_id = str(offer["offer_id"])
                if offer_id in seen_offer_ids:
                    raise EmpikClientError("API EmpikPlace zwróciło zduplikowaną ofertę.")
                seen_offer_ids.add(offer_id)
                offers.append(offer)
            if expected_total is not None:
                if len(offers) > expected_total:
                    raise EmpikClientError(
                        "API EmpikPlace zwróciło więcej ofert niż zadeklarowano."
                    )
                if len(offers) == expected_total:
                    break
            if expected_total is None and len(page) < page_size:
                break
            offset += len(page)
        else:
            raise EmpikClientError("API EmpikPlace przekroczyło limit stronicowania ofert.")
        return offers

    def list_offer_imports(self, *, days=30):
        start_date = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
        imports = []
        seen_import_ids = set()
        page_token = ""
        for _ in range(20):
            params = {"start_date": start_date}
            if page_token:
                params = {"page_token": page_token}
            payload = self._get_json("/api/offers/imports", params=params)
            page = payload.get("data")
            if not isinstance(page, list):
                raise EmpikClientError("API EmpikPlace nie zwróciło listy importów ofert.")
            for item in page:
                if not isinstance(item, dict) or item.get("import_id") is None:
                    continue
                import_id = str(item["import_id"])
                if import_id in seen_import_ids:
                    continue
                seen_import_ids.add(import_id)
                imports.append(item)
            next_token = str(payload.get("next_page_token") or "").strip()
            if not next_token:
                break
            if next_token == page_token:
                raise EmpikClientError("API EmpikPlace zwróciło zapętlony token strony importów.")
            page_token = next_token
        else:
            raise EmpikClientError("API EmpikPlace przekroczyło limit stronicowania importów.")
        imports.sort(key=lambda item: str(item.get("date_created") or ""), reverse=True)
        return imports

    def get_offer_import_error_report(self, import_id):
        try:
            import_id = int(import_id)
        except (TypeError, ValueError) as exc:
            raise EmpikClientError("Nieprawidłowy identyfikator importu EmpikPlace.") from exc
        if import_id < 1:
            raise EmpikClientError("Nieprawidłowy identyfikator importu EmpikPlace.")
        response = self._request(
            f"/api/offers/imports/{import_id}/error_report",
            stream=True,
        )
        chunks = []
        size = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_ERROR_REPORT_BYTES:
                raise EmpikClientError("Raport błędów EmpikPlace przekracza limit 10 MB.")
            chunks.append(chunk)
        content_type = (response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0]
        return b"".join(chunks), content_type
