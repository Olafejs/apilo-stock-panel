import pytest
from requests.adapters import HTTPAdapter

from apilo import ApiloClient, ApiloClientError, build_retry_session


def make_client(responses):
    client = object.__new__(ApiloClient)
    iterator = iter(responses)
    client._request = lambda *args, **kwargs: next(iterator)
    return client


def test_retry_session_never_retries_mutating_methods():
    adapter = build_retry_session().get_adapter("https://")
    assert isinstance(adapter, HTTPAdapter)
    retry = adapter.max_retries

    assert retry.allowed_methods == frozenset({"GET", "HEAD", "OPTIONS"})


def test_product_pagination_rejects_incomplete_empty_page():
    client = make_client(
        [
            {"products": [{"id": 1}, {"id": 2}], "totalCount": 3},
            {"products": [], "totalCount": 3},
        ]
    )

    with pytest.raises(ApiloClientError, match="incomplete product pagination"):
        client.list_products(limit=2)


def test_product_pagination_returns_exact_declared_snapshot():
    client = make_client(
        [
            {"products": [{"id": 1}, {"id": 2}], "totalCount": 3},
            {"products": [{"id": 3}], "totalCount": 3},
        ]
    )

    assert [product["id"] for product in client.list_products(limit=2)] == [1, 2, 3]