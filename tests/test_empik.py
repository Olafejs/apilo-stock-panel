import pytest

from empik import EmpikClient, EmpikClientError


class FakeResponse:
    def __init__(self, payload=None, *, status=200, headers=None, body=b""):
        self.payload = payload
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/json"}
        self.body = body

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_empik_client_lists_all_offer_pages_without_leaking_key():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "offers": [
                        {"offer_id": index, "shop_sku": f"SKU-{index}"}
                        for index in range(1, 101)
                    ],
                    "total_count": 101,
                }
            ),
            FakeResponse(
                {
                    "offers": [{"offer_id": 101, "shop_sku": "SKU-101"}],
                    "total_count": 101,
                }
            ),
        ]
    )
    client = EmpikClient("sekretny-klucz", shop_id=2811, session=session)

    offers = client.list_offers()

    assert len(offers) == 101
    assert session.calls[0][1]["params"] == {"max": 100, "offset": 0, "shop_id": 2811}
    assert session.calls[1][1]["params"] == {"max": 100, "offset": 100, "shop_id": 2811}
    assert session.calls[0][1]["headers"]["Authorization"] == "sekretny-klucz"
    assert "sekretny-klucz" not in session.calls[0][0]
    assert session.calls[0][1]["allow_redirects"] is False


def test_empik_client_rejects_duplicate_offer_page():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "offers": [{"offer_id": 1}, {"offer_id": 1}],
                    "total_count": 2,
                }
            )
        ]
    )

    with pytest.raises(EmpikClientError, match="zduplikowaną"):
        EmpikClient("key", session=session).list_offers()


def test_empik_client_retries_read_only_429_and_sanitizes_auth_errors():
    sleeps = []
    session = FakeSession(
        [
            FakeResponse({}, status=429, headers={"Retry-After": "0"}),
            FakeResponse({"offers": []}),
        ]
    )
    client = EmpikClient("key", session=session, sleep=sleeps.append)

    assert client.test_connection() is True
    assert sleeps == [0.0]

    unauthorized = FakeSession([FakeResponse({"secret": "never expose"}, status=401)])
    with pytest.raises(EmpikClientError, match="nieprawidłowy") as exc_info:
        EmpikClient("key", session=unauthorized).test_connection()
    assert "never expose" not in str(exc_info.value)


def test_empik_client_rejects_redirects_and_incomplete_declared_total():
    redirect = FakeSession([FakeResponse({}, status=302, headers={"Location": "http://127.0.0.1/"})])
    with pytest.raises(EmpikClientError, match="przekierowanie"):
        EmpikClient("key", session=redirect).test_connection()
    assert redirect.calls[0][1]["allow_redirects"] is False

    incomplete = FakeSession(
        [
            FakeResponse(
                {
                    "offers": [{"offer_id": 1, "shop_sku": "SKU-1"}],
                    "total_count": 2,
                }
            ),
            FakeResponse({"offers": [], "total_count": 2}),
        ]
    )
    with pytest.raises(EmpikClientError, match="niepełną"):
        EmpikClient("key", session=incomplete).list_offers()
    assert len(incomplete.calls) == 2

    missing_later_total = FakeSession(
        [
            FakeResponse(
                {
                    "offers": [{"offer_id": index} for index in range(100)],
                    "total_count": 150,
                }
            ),
            FakeResponse({"offers": [{"offer_id": 100}]}),
            FakeResponse({"offers": []}),
        ]
    )
    with pytest.raises(EmpikClientError, match="niepełną"):
        EmpikClient("key", session=missing_later_total).list_offers()
    assert len(missing_later_total.calls) == 3


def test_empik_client_lists_imports_with_page_tokens_and_downloads_bounded_report():
    session = FakeSession(
        [
            FakeResponse(
                {
                    "data": [{"import_id": 7, "date_created": "2026-07-25T10:00:00Z"}],
                    "next_page_token": "next-1",
                }
            ),
            FakeResponse(
                {
                    "data": [{"import_id": 8, "date_created": "2026-07-26T10:00:00Z"}],
                }
            ),
            FakeResponse(
                body=b"sku;errors\nSKU-1;invalid quantity\n",
                headers={"Content-Type": "text/csv; charset=utf-8"},
            ),
        ]
    )
    client = EmpikClient("key", session=session)

    imports = client.list_offer_imports(days=30)
    body, content_type = client.get_offer_import_error_report(8)

    assert [item["import_id"] for item in imports] == [8, 7]
    assert session.calls[1][1]["params"] == {"page_token": "next-1"}
    assert body.startswith(b"sku;errors")
    assert content_type == "text/csv"
