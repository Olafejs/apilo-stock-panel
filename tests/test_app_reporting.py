from app_reporting import (
    build_sales_report_csv,
    build_sales_report_rows,
    get_sales_totals,
    normalize_sales_report_days,
    pick_external_order_id,
)
from db import upsert_product_from_apilo


class FakeReportingClient:
    def __init__(self, orders, status_map):
        self._orders = orders
        self._status_map = status_map

    def list_orders(self, ordered_after=None, payment_status=None):
        assert ordered_after
        assert payment_status == 2
        return list(self._orders)

    def get_order_status_map(self):
        return list(self._status_map)


def test_normalize_sales_report_days_clamps_invalid_values():
    assert normalize_sales_report_days("7") == 7
    assert normalize_sales_report_days("0") == 30
    assert normalize_sales_report_days("999") == 30
    assert normalize_sales_report_days("abc") == 30


def test_pick_external_order_id_handles_nested_values():
    assert pick_external_order_id({"externalOrderId": {"id": "A-123"}}) == "A-123"
    assert pick_external_order_id({"marketplaceOrderId": {"value": "B-456"}}) == "B-456"
    assert pick_external_order_id({"id": 789}) == "789"


def test_get_sales_totals_and_rows_use_realized_orders_and_product_mappings(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 301,
            "originalCode": "KOD-301",
            "sku": "SKU-301",
            "ean": "5900000000301",
            "name": "Produkt Gamma",
            "priceWithTax": 15.0,
            "priceWithoutTax": 12.2,
            "quantity": 5,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 302,
            "originalCode": "KOD-302",
            "sku": "SKU-302",
            "ean": "5900000000302",
            "name": "Produkt Delta",
            "priceWithTax": 25.0,
            "priceWithoutTax": 20.33,
            "quantity": 7,
            "status": 1,
        },
    )

    client = FakeReportingClient(
        orders=[
            {
                "id": 10,
                "status": 27,
                "orderedAt": "2026-03-11T10:00:00Z",
                "externalOrderId": {"id": "AL-10"},
                "orderItems": [
                    {"productId": 301, "quantity": 2},
                    {"originalCode": "KOD-302", "quantity": 1},
                ],
            },
            {
                "id": 11,
                "status": 5,
                "orderedAt": "2026-03-10T10:00:00Z",
                "orderItems": [
                    {"sku": "SKU-301", "quantity": 9},
                ],
            },
        ],
        status_map=[
            {"id": 27, "name": "Zrealizowane"},
            {"id": 5, "name": "Nowe"},
        ],
    )

    totals, meta, details = get_sales_totals(
        app_module.DB_PATH,
        client,
        30,
        realized_only=True,
    )
    rows = build_sales_report_rows(app_module.DB_PATH, totals)
    csv_body = build_sales_report_csv(rows)

    assert meta == {
        "orders_total": 2,
        "orders_used": 1,
        "realized_filter": True,
    }
    assert totals == {
        "5900000000301": 2,
        "5900000000302": 1,
    }
    assert details["5900000000301"][0]["allegro_id"] == "AL-10"
    assert rows[0]["name"] == "Produkt Gamma"
    assert rows[0]["quantity"] == 2
    assert "5900000000301;Produkt Gamma;2" in csv_body
