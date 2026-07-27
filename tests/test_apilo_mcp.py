import sqlite3

import pytest

from db import get_product_by_id, init_db
from apilo_mcp import (
    adjust_stock_quantity,
    apply_return_stock_corrections,
    count_products,
    find_product,
    get_product_inventory,
)


class FakeApiloClient:
    def __init__(self, remote_quantities=None):
        self.remote_quantities = remote_quantities or {}
        self.updates = []
        self.get_product_calls = []

    def update_quantities(self, updates):
        self.updates.append(updates)
        for update in updates:
            product_id = update.get("id")
            if product_id is not None:
                self.remote_quantities[int(product_id)] = int(update["quantity"])
        return {"changes": len(updates)}

    def get_product(self, product_id):
        self.get_product_calls.append(int(product_id))
        return {"id": int(product_id), "quantity": self.remote_quantities[int(product_id)]}

    def _request(self, method, path, params=None, json_body=None):
        assert method == "GET"
        assert path == "/rest/api/warehouse/product/"
        assert params == {"limit": 1, "offset": 0}
        return {"totalCount": 2, "products": [{"id": 1001}]}


def insert_product(db_path, **values):
    defaults = {
        "apilo_id": 1001,
        "original_code": "orig-1001",
        "sku": "SKU-1001",
        "ean": "5900000000001",
        "name": "Test product",
        "quantity": 5,
        "last_synced_quantity": 5,
        "dirty": 0,
        "allegro_auction_id": "1234567890",
    }
    defaults.update(values)
    columns = ", ".join(defaults.keys())
    placeholders = ", ".join("?" for _ in defaults)
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            f"INSERT INTO products ({columns}) VALUES ({placeholders})",
            tuple(defaults.values()),
        )
    conn.close()


def test_count_products_returns_remote_and_local_summary(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(db_path, apilo_id=1001, quantity=5, last_synced_quantity=5, dirty=0)
    insert_product(
        db_path,
        apilo_id=1002,
        original_code="orig-1002",
        sku="SKU-1002",
        ean="5900000000002",
        name="Second product",
        quantity=0,
        last_synced_quantity=0,
        dirty=1,
        allegro_auction_id="9876543210",
    )
    client = FakeApiloClient()

    result = count_products(db_path, client)

    assert result == {
        "remote_total_count": 2,
        "local_product_count": 2,
        "local_total_quantity": 5,
        "local_in_stock_count": 1,
        "local_out_of_stock_count": 1,
        "local_pending_sync_count": 1,
        "consistent": True,
    }


def test_get_product_inventory_returns_remote_quantity_not_only_local_cache(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(
        db_path,
        apilo_id=3003,
        quantity=5,
        last_synced_quantity=5,
        name="Zaślepka zamka do FAAC 740/741",
    )
    client = FakeApiloClient({3003: 8})

    result = get_product_inventory(db_path, client, name="FAAC")

    assert client.get_product_calls == [3003]
    assert result["remote_quantity"] == 8
    assert result["local_quantity"] == 5
    assert result["quantity_consistent"] is False
    assert result["name"] == "Zaślepka zamka do FAAC 740/741"


def test_find_product_matches_allegro_offer_id(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(db_path, allegro_auction_id="987654321")

    product = find_product(db_path, allegro_offer_id="987654321")

    assert product["apilo_id"] == 1001
    assert product["sku"] == "SKU-1001"
    assert product["quantity"] == 5


def test_adjust_stock_quantity_updates_remote_then_local_and_verifies(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(db_path, apilo_id=2002, quantity=7, last_synced_quantity=7)
    client = FakeApiloClient({2002: 10})

    result = adjust_stock_quantity(
        db_path,
        client,
        apilo_id=2002,
        delta=3,
        reason="zwrot Allegro N8WR/2026",
    )

    assert client.updates == [[{"id": 2002, "quantity": 10}]]
    assert result["before_quantity"] == 7
    assert result["after_quantity"] == 10
    assert result["verified"] is True
    product = get_product_by_id(db_path, result["local_id"])
    assert product["quantity"] == 10
    assert product["last_synced_quantity"] == 10
    assert product["dirty"] == 0


def test_return_stock_corrections_require_physical_receipt_confirmation(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(db_path)
    client = FakeApiloClient({1001: 6})

    with pytest.raises(ValueError, match="confirmed_received"):
        apply_return_stock_corrections(
            db_path,
            client,
            items=[{"allegro_offer_id": "1234567890", "quantity": 1}],
            confirmed_received=False,
        )

    assert client.updates == []


def test_return_stock_corrections_match_by_offer_and_increase_quantity(tmp_path):
    db_path = str(tmp_path / "apilo.sqlite3")
    init_db(db_path)
    insert_product(db_path, quantity=2, last_synced_quantity=2)
    client = FakeApiloClient({1001: 4})

    result = apply_return_stock_corrections(
        db_path,
        client,
        items=[{"allegro_offer_id": "1234567890", "quantity": 2}],
        confirmed_received=True,
        reference="N8WR/2026",
    )

    assert result["updated_count"] == 1
    assert result["updates"][0]["before_quantity"] == 2
    assert result["updates"][0]["after_quantity"] == 4
    assert client.updates == [[{"id": 1001, "quantity": 4}]]
