import base64
import os
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from db import (
    apply_product_snapshot,
    get_channel_description_checks,
    get_products,
    get_recent_audit_log,
    get_setting,
    record_login_attempt,
    replace_apilo_description_references,
    replace_channel_description_checks,
    save_sales_cache,
    save_sales_year_cache,
    set_setting,
    update_product_attributes,
    upsert_product_from_apilo,
)
from material_palette_checks import canonical_material_palette_text
from product_attributes import (
    description_primary_section_text,
    description_to_text,
    parse_material_color,
)


class DummyImageResponse:
    def __init__(self, body, content_type="image/jpeg"):
        self.body = body
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


VALID_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAFgwJ/l6k9WQAAAABJRU5ErkJggg=="
)


def test_login_success_records_audit_entry(app_module, client):
    set_setting(app_module.DB_PATH, "password_hash", generate_password_hash("haslo-testowe"))
    with client.session_transaction() as session:
        session["csrf_token"] = "login-csrf"

    response = client.post(
        "/login",
        data={"password": "haslo-testowe", "csrf_token": "login-csrf"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    with client.session_transaction() as session:
        assert session["logged_in"] is True
    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "login_success"
    assert audit_rows[0]["entity_type"] == "auth"


def test_login_rate_limit_blocks_after_limit(app_module, client):
    set_setting(app_module.DB_PATH, "password_hash", generate_password_hash("inne-haslo"))
    with client.session_transaction() as session:
        session["csrf_token"] = "rate-csrf"

    for _ in range(app_module.LOGIN_RATE_LIMIT_MAX_ATTEMPTS):
        record_login_attempt(app_module.DB_PATH, "127.0.0.1")

    response = client.post(
        "/login",
        data={"password": "zle-haslo", "csrf_token": "rate-csrf"},
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
    )

    assert response.status_code == 429
    assert "Za dużo nieudanych prób logowania" in response.get_data(as_text=True)


def test_post_without_csrf_is_rejected(client):
    response = client.post("/logout")

    assert response.status_code == 400
    assert response.get_data(as_text=True) == "Bad Request"


def test_setup_password_blocks_remote_request_without_setup_token(client):
    response = client.get(
        "/setup-password",
        environ_base={"REMOTE_ADDR": "192.168.1.50"},
    )

    assert response.status_code == 403
    assert "Pierwsze ustawienie hasła jest dozwolone tylko lokalnie" in response.get_data(
        as_text=True
    )


def test_setup_password_page_shows_project_name(client):
    response = client.get("/setup-password")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Apilo Panel Stanow Magazynowych" in html


def test_alert_settings_save_and_render(app_module, logged_in_client):
    response = logged_in_client.post(
        "/settings",
        data={
            "action": "alerts_settings",
            "alerts_low_stock_enabled": "1",
            "alerts_low_stock_interval_hours": "12",
            "csrf_token": "test-csrf-token",
        },
    )

    assert response.status_code == 302
    assert get_setting(app_module.DB_PATH, "alerts_low_stock_enabled") == "1"
    assert get_setting(app_module.DB_PATH, "alerts_low_stock_interval_hours") == "12"

    page = logged_in_client.get("/settings")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Włącz automatyczny alert niskich stanów" in html
    assert 'value="12"' in html

    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "low_stock_alert_settings_update"


def test_invalid_smtp_settings_are_rejected_without_partial_save(
    app_module, logged_in_client
):
    set_setting(app_module.DB_PATH, "smtp_host", "smtp.previous.example")

    response = logged_in_client.post(
        "/settings",
        data={
            "action": "email",
            "smtp_host": "smtp.new.example",
            "smtp_port": "587",
            "smtp_user": "panel@example.com",
            "smtp_use_tls": "1",
            "smtp_use_ssl": "1",
            "smtp_from": "panel@example.com",
            "smtp_to": "alerts@example.com",
            "csrf_token": "test-csrf-token",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "nie oba tryby" in response.get_data(as_text=True)
    assert get_setting(app_module.DB_PATH, "smtp_host") == "smtp.previous.example"


def test_manual_sync_pull_records_audit_entry(app_module, logged_in_client, monkeypatch):
    monkeypatch.setattr(app_module, "run_sync_pull_with_lock", lambda blocking=False: 7)

    response = logged_in_client.post(
        "/sync/pull",
        data={"csrf_token": "test-csrf-token"},
    )

    assert response.status_code == 302
    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "manual_sync_pull"
    assert audit_rows[0]["new_value"] == "7 produktów"


def test_perform_sync_pull_prefetches_main_thumbnails(app_module, monkeypatch):
    scheduled = []

    class FakeClient:
        def list_products(self):
            return [
                {
                    "id": 401,
                    "name": "Produkt testowy",
                    "sku": "SKU-401",
                    "status": 1,
                    "quantity": 2,
                }
            ]

        def get_product_media(self, batch, only_main=True):
            assert batch == [401]
            assert only_main is True
            return [
                {
                    "productId": 401,
                    "link": "https://example.com/thumb-401.png",
                }
            ]

        def list_sale_platforms(self):
            return []

        def list_auctions(self):
            return []

        def list_price_calculated(self, price_list_id):
            assert price_list_id is None
            return []

    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(app_module, "get_client", lambda: FakeClient())
    monkeypatch.setattr(app_module, "get_allegro_price_list_id", lambda: None)
    monkeypatch.setattr(
        app_module,
        "prefetch_thumbnail",
        lambda apilo_id, image_url, force=False: scheduled.append(
            (apilo_id, image_url, force)
        )
        or True,
    )

    count = app_module.perform_sync_pull()

    assert count == 1
    assert scheduled == [
        (401, "https://example.com/thumb-401.png", True)
    ]


def test_perform_sync_pull_forces_thumbnail_refresh_when_image_changes(
    app_module, monkeypatch
):
    scheduled = []

    class FakeClient:
        def list_products(self):
            return [
                {
                    "id": 402,
                    "name": "Produkt z nowa miniatura",
                    "sku": "SKU-402",
                    "status": 1,
                    "quantity": 2,
                }
            ]

        def get_product_media(self, batch, only_main=True):
            assert batch == [402]
            assert only_main is True
            return [
                {
                    "productId": 402,
                    "link": "https://example.com/thumb-402-new.png",
                }
            ]

        def list_sale_platforms(self):
            return []

        def list_auctions(self):
            return []

        def list_price_calculated(self, price_list_id):
            assert price_list_id is None
            return []

    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 402,
            "name": "Produkt z poprzednia miniatura",
            "sku": "SKU-402",
            "image_url": "https://example.com/thumb-402-old.png",
            "status": 1,
        },
    )
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(app_module, "get_client", lambda: FakeClient())
    monkeypatch.setattr(app_module, "get_allegro_price_list_id", lambda: None)
    monkeypatch.setattr(
        app_module,
        "prefetch_thumbnail",
        lambda apilo_id, image_url, force=False: scheduled.append(
            (apilo_id, image_url, force)
        )
        or True,
    )

    count = app_module.perform_sync_pull()

    assert count == 1
    assert scheduled == [
        (402, "https://example.com/thumb-402-new.png", True)
    ]


def test_suggested_stock_ignores_single_annual_sale(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 301,
            "sku": "SKU-ONE",
            "ean": "5900000000001",
            "name": "Produkt sprzedany raz",
            "quantity": 0,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 302,
            "sku": "SKU-TWO",
            "ean": "5900000000002",
            "name": "Produkt sprzedany dwa razy",
            "quantity": 0,
            "status": 1,
        },
    )
    save_sales_cache(
        app_module.DB_PATH,
        {"5900000000001": 1},
        {"5900000000001": []},
    )
    save_sales_year_cache(
        app_module.DB_PATH,
        {"5900000000001": 1, "5900000000002": 2},
        {"5900000000001": 1, "5900000000002": 2},
    )

    rows = get_products(
        app_module.DB_PATH,
        sort="name",
        limit=10,
        lead_time_days=7,
        safety_pct=50,
        suggest_days=365,
    )
    by_ean = {row["ean"]: row for row in rows}

    assert by_ean["5900000000001"]["quantity_30d"] == 1
    assert by_ean["5900000000001"]["quantity_year"] == 1
    assert by_ean["5900000000001"]["suggested_qty"] == 0
    assert by_ean["5900000000001"]["shortage_qty"] == 0
    assert by_ean["5900000000002"]["quantity_year"] == 2
    assert by_ean["5900000000002"]["suggested_qty"] == 1

    shortage_rows = get_products(
        app_module.DB_PATH,
        preset="shortage",
        sort="name",
        limit=10,
        lead_time_days=7,
        safety_pct=50,
        suggest_days=365,
    )
    assert [row["ean"] for row in shortage_rows] == ["5900000000002"]


def test_suggested_stock_removes_single_order_outlier_from_annual_calculation(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 303,
            "sku": "SKU-SINGLE-LARGE",
            "ean": "5900000000003",
            "name": "Produkt z pojedynczym dużym zamówieniem",
            "quantity": 0,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 304,
            "sku": "SKU-SINGLE-BULK",
            "ean": "5900000000004",
            "name": "Produkt z jednym dużym zamówieniem",
            "quantity": 0,
            "status": 1,
        },
    )
    totals = {"5900000000003": 54, "5900000000004": 16}
    details = {
        "5900000000003": [
            {"date": "2026-04-23", "qty": 2, "order_id": "AL260400104"},
            {"date": "2026-03-05", "qty": 50, "order_id": "PR260300017"},
            {"date": "2025-10-16", "qty": 1, "order_id": "AL251000058"},
            {"date": "2025-07-31", "qty": 1, "order_id": "AL250700143"},
        ],
        "5900000000004": [
            {"date": "2026-01-10", "qty": 16, "order_id": "AL260100001"},
        ],
    }
    save_sales_cache(app_module.DB_PATH, totals, details)
    save_sales_year_cache(
        app_module.DB_PATH,
        totals,
        {ean: len(items) for ean, items in details.items()},
        details,
    )

    rows = get_products(
        app_module.DB_PATH,
        sort="name",
        limit=10,
        lead_time_days=30,
        safety_pct=25,
        suggest_days=365,
    )
    by_ean = {row["ean"]: row for row in rows}

    assert by_ean["5900000000003"]["quantity_year"] == 54
    assert by_ean["5900000000003"]["quantity_year_adjusted"] == 4
    assert by_ean["5900000000003"]["outlier_order_qty"] == 50
    assert by_ean["5900000000003"]["suggested_qty"] == 1
    assert by_ean["5900000000004"]["quantity_year_adjusted"] == 0
    assert by_ean["5900000000004"]["outlier_order_qty"] == 16
    assert by_ean["5900000000004"]["suggested_qty"] == 0
    assert by_ean["5900000000004"]["shortage_qty"] == 0


def test_process_low_stock_alert_skips_duplicate_auto_send(app_module, monkeypatch):
    sent_counts = []
    first_rows = [
        {"name": "Produkt A", "ean": "111", "quantity": 1, "suggested_qty": 5, "shortage_qty": 4},
        {"name": "Produkt B", "ean": "222", "quantity": 0, "suggested_qty": 3, "shortage_qty": 3},
    ]
    second_rows = [
        {"name": "Produkt A", "ean": "111", "quantity": 1, "suggested_qty": 6, "shortage_qty": 5},
    ]
    monkeypatch.setattr(app_module, "get_low_stock_rows", lambda limit=10: first_rows)
    monkeypatch.setattr(
        app_module,
        "send_low_stock_alert_email",
        lambda rows: sent_counts.append(len(rows)),
    )

    manual_result = app_module.process_low_stock_alert(mode="manual")
    auto_duplicate_result = app_module.process_low_stock_alert(mode="auto")
    monkeypatch.setattr(app_module, "get_low_stock_rows", lambda limit=10: second_rows)
    auto_sent_result = app_module.process_low_stock_alert(mode="auto")

    assert manual_result == {"status": "sent", "count": 2}
    assert auto_duplicate_result == {"status": "duplicate", "count": 2}
    assert auto_sent_result == {"status": "sent", "count": 1}
    assert sent_counts == [2, 1]
    assert get_setting(app_module.DB_PATH, "alerts_low_stock_last_result") == (
        "Wysłano alert automatycznie (1 pozycja)."
    )

    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=10)
    send_actions = [row for row in audit_rows if row["action"] == "low_stock_alert_send"]
    assert len(send_actions) == 2
    assert send_actions[0]["actor_ip"] == "system"


def test_sales_report_uses_realized_query_flag(app_module, logged_in_client, monkeypatch):
    calls = []

    def fake_get_sales_totals(days, realized_only=True):
        calls.append((days, realized_only))
        return (
            {"5901234123457": 3},
            {"orders_total": 5, "orders_used": 3, "realized_filter": realized_only},
            {},
        )

    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(app_module, "get_sales_totals", fake_get_sales_totals)
    monkeypatch.setattr(
        app_module,
        "build_sales_report_rows",
        lambda db_path, totals: [
            {"ean": "5901234123457", "name": "Produkt testowy", "quantity": 3}
        ],
    )

    response = logged_in_client.get("/sales-report?days=30&realized=0")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert calls == [(30, False)]
    assert "Produkt testowy" in html


def test_healthz_returns_ok_payload(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["version"]


def test_index_csv_export_uses_current_filters(app_module, logged_in_client, monkeypatch):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 101,
            "originalCode": "KOD-1",
            "sku": "SKU-ALFA",
            "ean": "5901111111111",
            "name": "Produkt Alfa",
            "priceWithTax": 12.5,
            "priceWithoutTax": 10.16,
            "quantity": 7,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 102,
            "originalCode": "KOD-2",
            "sku": "SKU-BETA",
            "ean": "5902222222222",
            "name": "Produkt Beta",
            "priceWithTax": 22.5,
            "priceWithoutTax": 18.29,
            "quantity": 3,
            "status": 1,
        },
    )

    response = logged_in_client.get(
        "/?search=Alfa&sort=name&order=asc&export=1"
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment; filename=produkty_all_" in response.headers["Content-Disposition"]
    assert "Produkt Alfa" in body
    assert "Produkt Beta" not in body
    assert "Apilo ID;SKU;Kod oryginalny;Nazwa;EAN;Stan;" in body

    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "products_export_csv"


def test_index_csv_export_is_not_limited_to_page_size(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    for product_id in range(1, 31):
        upsert_product_from_apilo(
            app_module.DB_PATH,
            {
                "id": 1000 + product_id,
                "sku": f"SKU-{product_id:02d}",
                "name": f"Produkt {product_id:02d}",
                "quantity": product_id,
                "status": 1,
            },
        )

    response = logged_in_client.get("/?limit=25&sort=name&order=asc&export=1")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Produkt 01" in body
    assert "Produkt 30" in body
    assert len(body.splitlines()) == 31


def test_quantity_update_rejects_external_next_redirect(
    app_module, logged_in_client, monkeypatch
):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 301,
            "sku": "SKU-REDIRECT",
            "name": "Produkt redirect",
            "quantity": 2,
            "status": 1,
        },
    )
    product = get_products(app_module.DB_PATH, search="SKU-REDIRECT", limit=1)[0]

    class DummyClient:
        def update_quantities(self, payload):
            assert payload == [{"quantity": 3, "id": 301}]

    monkeypatch.setattr(app_module, "get_client", lambda: DummyClient())

    response = logged_in_client.post(
        f"/products/{product['id']}/quantity",
        data={
            "quantity": "3",
            "next": "https://example.invalid/phishing",
            "csrf_token": "test-csrf-token",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_quantity_update_stops_when_remote_state_changed(
    app_module, logged_in_client, monkeypatch
):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 302, "name": "Produkt konflikt", "quantity": 2, "status": 1},
    )
    product = get_products(app_module.DB_PATH, search="Produkt konflikt", limit=1)[0]

    class DummyClient:
        def get_product(self, product_id):
            assert product_id == 302
            return {"id": 302, "quantity": 4}

        def update_quantities(self, payload):
            raise AssertionError(f"Nie wolno nadpisać nowszego stanu: {payload}")

    monkeypatch.setattr(app_module, "get_client", lambda: DummyClient())

    response = logged_in_client.post(
        f"/products/{product['id']}/quantity",
        data={
            "quantity": "3",
            "expected_quantity": "2",
            "next": "/",
            "csrf_token": "test-csrf-token",
        },
        follow_redirects=True,
    )

    assert "Stan w Apilo zmienił się" in response.get_data(as_text=True)
    refreshed = get_products(app_module.DB_PATH, search="Produkt konflikt", limit=1)[0]
    assert refreshed["quantity"] == 4


def test_quantity_timeout_is_accepted_only_after_remote_verification(
    app_module, logged_in_client, monkeypatch
):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 303, "name": "Produkt timeout", "quantity": 2, "status": 1},
    )
    product = get_products(app_module.DB_PATH, search="Produkt timeout", limit=1)[0]

    class DummyClient:
        reads = 0

        def get_product(self, product_id):
            self.reads += 1
            return {"id": product_id, "quantity": 2 if self.reads == 1 else 3}

        def update_quantities(self, payload):
            raise app_module.ApiloClientError("Apilo API error: connection error.")

    monkeypatch.setattr(app_module, "get_client", lambda: DummyClient())

    response = logged_in_client.post(
        f"/products/{product['id']}/quantity",
        data={
            "quantity": "3",
            "expected_quantity": "2",
            "next": "/",
            "csrf_token": "test-csrf-token",
        },
        follow_redirects=True,
    )

    assert "potwierdzony po błędzie połączenia" in response.get_data(as_text=True)
    refreshed = get_products(app_module.DB_PATH, search="Produkt timeout", limit=1)[0]
    assert refreshed["quantity"] == 3


def test_quantity_change_is_marked_unverified_when_remote_check_also_fails(
    app_module, logged_in_client, monkeypatch
):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 304, "name": "Produkt niejednoznaczny", "quantity": 2, "status": 1},
    )
    product = get_products(app_module.DB_PATH, search="Produkt niejednoznaczny", limit=1)[0]

    class AmbiguousClient:
        def __init__(self):
            self.reads = 0

        def get_product(self, product_id):
            self.reads += 1
            if self.reads == 1:
                return {"id": product_id, "quantity": 2}
            raise app_module.ApiloClientError("Apilo API error: connection error.")

        def update_quantities(self, updates):
            raise app_module.ApiloClientError("Apilo API error: connection error.")

    ambiguous_client = AmbiguousClient()
    monkeypatch.setattr(app_module, "get_client", lambda: ambiguous_client)

    response = logged_in_client.post(
        f"/products/{product['id']}/quantity",
        data={
            "quantity": "3",
            "expected_quantity": "2",
            "next": "/",
            "csrf_token": "test-csrf-token",
        },
        follow_redirects=True,
    )

    assert "oznaczony do ponownej synchronizacji" in response.get_data(as_text=True)
    refreshed = get_products(app_module.DB_PATH, search="Produkt niejednoznaczny", limit=1)[0]
    assert refreshed["quantity"] == 2
    assert refreshed["dirty"] == 1


def test_inventory_sync_does_not_commit_partial_snapshot_when_enrichment_fails(
    app_module, monkeypatch
):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 501, "name": "Poprzedni snapshot", "quantity": 2, "status": 1},
    )

    class FailingClient:
        def list_products(self):
            return [{"id": 502, "name": "Nowy snapshot", "quantity": 3, "status": 1}]

        def get_product_media(self, product_ids, only_main=True):
            return []

        def list_sale_platforms(self):
            return []

        def list_auctions(self):
            return []

        def list_price_calculated(self, price_id):
            raise app_module.ApiloClientError("Apilo API error: connection error.")

    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(app_module, "get_client", lambda: FailingClient())

    with pytest.raises(app_module.ApiloClientError):
        app_module.perform_sync_pull()

    rows = get_products(app_module.DB_PATH, limit=None)
    assert [(row["apilo_id"], row["name"]) for row in rows] == [
        (501, "Poprzedni snapshot")
    ]
    assert get_setting(app_module.DB_PATH, "last_pull_at") is None


def test_inventory_sync_commits_complete_snapshot_once(app_module, monkeypatch):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 601, "name": "Stary produkt", "quantity": 1, "status": 1},
    )

    class CompleteClient:
        def list_products(self):
            return [
                {
                    "id": 602,
                    "sku": "SKU-602",
                    "ean": "5900000000602",
                    "name": "Pełny snapshot",
                    "priceWithTax": 10.0,
                    "quantity": 5,
                    "status": 1,
                }
            ]

        def get_product_media(self, product_ids, only_main=True):
            assert product_ids == [602]
            return [{"productId": 602, "link": "https://example.com/602.jpg"}]

        def list_sale_platforms(self):
            return [{"id": 7, "name": "Allegro"}]

        def list_auctions(self):
            return []

        def list_price_calculated(self, price_id):
            return [{"product": 602, "customPriceWithTax": 15.0}]

    prefetched = []
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(app_module, "get_client", lambda: CompleteClient())
    monkeypatch.setattr(
        app_module,
        "prefetch_thumbnail",
        lambda product_id, url, force=False: prefetched.append((product_id, url, force)),
    )

    assert app_module.perform_sync_pull() == 1

    rows = get_products(app_module.DB_PATH, limit=None)
    assert [(row["apilo_id"], row["quantity"]) for row in rows] == [(602, 5)]
    assert rows[0]["image_url"] == "https://example.com/602.jpg"
    assert rows[0]["allegro_price_with_tax"] == 15.0
    assert get_setting(app_module.DB_PATH, "last_pull_at")
    assert prefetched == [(602, "https://example.com/602.jpg", True)]


def test_thumb_uses_private_browser_cache_for_fresh_local_file(
    app_module, logged_in_client, monkeypatch, tmp_path
):
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    monkeypatch.setattr(app_module, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(app_module, "THUMB_TTL_SECONDS", 3600)
    monkeypatch.setattr(
        app_module.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Fresh thumbnail should not trigger download.")
        ),
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 201,
            "name": "Produkt z miniatura",
            "image_url": "https://example.com/thumb.jpg",
            "status": 1,
        },
    )
    thumb_path = Path(app_module.THUMB_DIR) / "201.jpg"
    thumb_path.write_bytes(b"fresh-thumb")

    response = logged_in_client.get("/thumb/201")

    assert response.status_code == 200
    assert response.get_data() == b"fresh-thumb"
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    assert "Cookie" in response.headers["Vary"]


def test_thumb_serves_stale_local_file_before_background_refresh(
    app_module, logged_in_client, monkeypatch, tmp_path
):
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    monkeypatch.setattr(app_module, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(app_module, "THUMB_TTL_SECONDS", 3600)
    monkeypatch.setattr(
        app_module.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Stale thumbnail should refresh in background.")
        ),
    )
    scheduled = []
    monkeypatch.setattr(
        app_module,
        "schedule_thumbnail_refresh",
        lambda apilo_id, url, local_path: scheduled.append((apilo_id, url, local_path)) or True,
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 202,
            "name": "Produkt ze stara miniatura",
            "image_url": "https://example.com/stale.jpg",
            "status": 1,
        },
    )
    thumb_path = Path(app_module.THUMB_DIR) / "202.jpg"
    thumb_path.write_bytes(b"stale-thumb")
    os.utime(thumb_path, (1, 1))

    response = logged_in_client.get("/thumb/202")

    assert response.status_code == 200
    assert response.get_data() == b"stale-thumb"
    assert response.headers["Cache-Control"] == "private, no-cache"
    assert scheduled == [
        (202, "https://example.com/stale.jpg", str(thumb_path))
    ]


def test_thumb_downloads_missing_file_and_sets_private_cache(
    app_module, logged_in_client, monkeypatch, tmp_path
):
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    monkeypatch.setattr(app_module, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(app_module, "THUMB_TTL_SECONDS", 3600)
    monkeypatch.setattr(
        app_module.requests,
        "get",
        lambda *args, **kwargs: DummyImageResponse(
            VALID_PNG_BYTES,
            content_type="image/png",
        ),
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 203,
            "name": "Produkt do pobrania miniatury",
            "image_url": "https://example.com/download.png",
            "status": 1,
        },
    )

    response = logged_in_client.get("/thumb/203")

    assert response.status_code == 200
    assert response.get_data()
    assert response.headers["Cache-Control"] == "private, max-age=3600"
    assert (thumb_dir / "203.png").read_bytes()
    assert len((thumb_dir / "203.png").read_bytes()) < len(VALID_PNG_BYTES) * 3


def test_index_renders_thumb_cache_buster_from_image_url(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    image_url = "https://example.com/product-thumb.jpg"
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 301,
            "originalCode": "KOD-301",
            "sku": "SKU-301",
            "ean": "5903333333333",
            "name": "Produkt z cache-busterem",
            "image_url": image_url,
            "quantity": 5,
            "status": 1,
        },
    )

    response = logged_in_client.get("/")
    html = response.get_data(as_text=True)
    version = app_module.build_thumb_version(image_url)

    assert response.status_code == 200
    assert f"/thumb/301?v={version}" in html


def test_parse_material_color_from_allegro_description_html():
    payload = {
        "sections": [
            {
                "items": [
                    {
                        "type": "TEXT",
                        "content": "<p><b>Materiał:</b> PET-G</p><p><b>Kolor:</b> szary</p>",
                    }
                ]
            }
        ]
    }

    attrs = parse_material_color(description_to_text(payload))

    assert attrs == {"material": "PETG", "color": "szary"}


def test_product_attributes_render_search_and_export(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 701,
            "originalCode": "KOD-PLA",
            "sku": "SKU-PLA",
            "ean": "5907777777777",
            "name": "Uchwyt testowy",
            "quantity": 4,
            "status": 1,
        },
    )
    update_product_attributes(
        app_module.DB_PATH,
        {
            701: {
                "material": "PLA",
                "color": "czarny",
                "source": "allegro_description",
            }
        },
    )

    response = logged_in_client.get("/?search=czarny")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Materiał: <strong>PLA</strong>" in html
    assert "Kolor: <strong>czarny</strong>" in html
    assert "Kopiuj ścieżkę" not in html
    assert "Folder" not in html

    csv_response = logged_in_client.get("/?search=PLA&export=1")
    csv_body = csv_response.get_data(as_text=True)
    assert "Materiał;Kolor;URL zdjecia" in csv_body
    assert "PLA;czarny;" in csv_body


def test_manual_product_attribute_editor_updates_local_metadata(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 751,
            "sku": "SKU-CARBON",
            "name": "Produkt testowy Carbon",
            "quantity": 4,
            "status": 1,
        },
    )
    product = get_products(app_module.DB_PATH, search="SKU-CARBON", limit=1)[0]

    response = logged_in_client.post(
        f"/products/{product['id']}/attributes",
        data={
            "material": "CARBON",
            "color": "",
            "next": "https://example.invalid/phishing",
            "csrf_token": "test-csrf-token",
        },
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    refreshed = get_products(app_module.DB_PATH, search="SKU-CARBON", limit=1)[0]
    assert refreshed["material"] == "CARBON"
    assert refreshed["color"] == "czarny"
    assert refreshed["attributes_source"] == "manual_user_hint"
    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "product_attributes_update"

    html = logged_in_client.get("/?search=SKU-CARBON").get_data(as_text=True)
    assert f'action="/products/{product["id"]}/attributes"' in html
    assert "Edytuj dane" in html
    assert 'option value="CARBON" selected' in html


def test_exact_material_search_and_attribute_filters(app_module, logged_in_client, monkeypatch):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 801,
            "sku": "SKU-PLA",
            "ean": "5908888888881",
            "name": "Produkt PLA",
            "quantity": 1,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 802,
            "sku": "SKU-PETG",
            "ean": "5908888888882",
            "name": "Produkt PETG z tekstem PLA w nazwie",
            "quantity": 1,
            "status": 1,
        },
    )
    update_product_attributes(
        app_module.DB_PATH,
        {
            801: {"material": "PLA", "color": "biały", "source": "test"},
            802: {"material": "PETG", "color": "szary", "source": "test"},
        },
    )

    search_response = logged_in_client.get("/?search=PLA")
    search_html = search_response.get_data(as_text=True)
    assert "Produkt PLA" in search_html
    assert "Produkt PETG z tekstem PLA w nazwie" not in search_html

    filter_response = logged_in_client.get("/?material=PETG&color=szary")
    filter_html = filter_response.get_data(as_text=True)
    assert "Produkt PETG z tekstem PLA w nazwie" in filter_html
    assert "Produkt PLA" not in filter_html
    assert "style=\"--badge-bg:#9ca3af;" in filter_html


def test_parse_material_color_falls_back_after_generic_material_sentence():
    attrs = parse_material_color(
        "Ten zaawansowany materiał zapewnia wyjątkową wytrzymałość. "
        "Wykonany z materiału włókno węglowe (CARBON)."
    )

    assert attrs == {"material": "CARBON", "color": "czarny"}


def test_parse_material_color_handles_flex_and_multicolor():
    attrs = parse_material_color(
        "Materiał: elastyczna guma. Kolor: biały."
    )
    assert attrs == {"material": "FLEX", "color": "czarny"}

    attrs = parse_material_color(
        "Materiał: PLA. Kolor: czerwony, czarny i biały."
    )
    assert attrs == {"material": "PLA", "color": "wielokolorowy"}

    attrs = parse_material_color(
        "Materiał: PLA. Kolor: domyślny kolor to Ciemnoczerwony w połączeniu z bielą."
    )
    assert attrs == {"material": "PLA", "color": "wielokolorowy"}

    attrs = parse_material_color(
        "Materiał: PLA. Kolor: domyślny kolor to Ciemnozielony."
    )
    assert attrs == {"material": "PLA", "color": "zielony"}

    attrs = parse_material_color(
        "Materiał: PLA. Kolor: domyślny kolor to Ciemnozielony."
    )
    assert attrs == {"material": "PLA", "color": "zielony"}

    attrs = parse_material_color(
        "Materiał: PLA. Kolor: domyślny kolor to Beżowy."
    )
    assert attrs == {"material": "PLA", "color": "beżowy"}

    attrs = parse_material_color(
        "Materiał: PETG. Kolor: domyślny kolor to Niebieski Przezroczysty."
    )
    assert attrs == {"material": "PETG", "color": "niebieski"}

    attrs = parse_material_color(
        "Materiał: PETG. Kolor: domyślny kolor to Zielony Przezroczysty."
    )
    assert attrs == {"material": "PETG", "color": "zielony"}

    attrs = parse_material_color(
        "Przedmiotem aukcji jest wielokolorowy produkt testowy. Materiał: PLA."
    )
    assert attrs == {"material": "PLA", "color": "wielokolorowy"}


def test_parse_material_color_forces_carbon_to_black():
    attrs = parse_material_color(
        "Materiał: CARBON. Kolor: czerwony."
    )
    assert attrs == {"material": "CARBON", "color": "czarny"}

    attrs = parse_material_color(
        "Materiał: włókno węglowe."
    )
    assert attrs == {"material": "CARBON", "color": "czarny"}


def test_parse_material_color_prefers_default_color_over_palette():
    attrs = parse_material_color(
        "Materiał: Wydruk wykonany z materiału PLA. "
        "Kolor: Domyślny kolor to Pomarańczowy. "
        "Możliwość wykonania w innym kolorze z palety poniżej. "
        "Nasza bogata gama kolorów PLA: Czarny Srebrny Szary Biały Czerwony."
    )
    assert attrs == {"material": "PLA", "color": "pomarańczowy"}

    attrs = parse_material_color(
        "Materiał: PETG. Personalizacja: Możliwość wyboru dowolnego koloru. "
        "Kolor: Domyślny kolor to niebieski. "
        "Nasza bogata gama kolorów PETG: Czarny Srebrny Szary Biały Czerwony."
    )
    assert attrs == {"material": "PETG", "color": "niebieski"}


def test_parse_material_color_uses_material_line_not_generic_flex_words():
    attrs = parse_material_color(
        "Przedmiotem aukcji jest uchwyt do modułu sieciowego Flex Mini. "
        "Materiał: wydruk wykonany z bezpiecznego i wytrzymałego materiału PETG, "
        "gwarantującego trwałość Personalizacja: możliwość edycji. "
        "Kolor: domyślny kolor to Czarny."
    )
    assert attrs == {"material": "PETG", "color": "czarny"}

    attrs = parse_material_color(
        "Funkcjonalność: umożliwia elastyczne projektowanie instalacji. "
        "Materiał: wydruk wykonany z bezpiecznego i wytrzymałego materiału PLA, "
        "gwarantującego trwałość Personalizacja: możliwość edycji. "
        "Kolor: domyślny kolor to czarny."
    )
    assert attrs == {"material": "PLA", "color": "czarny"}


def test_description_primary_section_ignores_palette_and_company_sections():
    payload = {
        "sections": [
            {
                "items": [
                    {
                        "type": "TEXT",
                        "content": "<p>Materiał: CARBON</p><p>Kolor: czarny</p>",
                    }
                ]
            },
            {
                "items": [
                    {
                        "type": "TEXT",
                        "content": "<p>Nasza bogata gama kolorów: biały czerwony niebieski</p>",
                    }
                ]
            },
            {
                "items": [
                    {"type": "TEXT", "content": "<p>O firmie Example Company</p>"}
                ]
            },
        ]
    }

    full_text = description_to_text(payload)
    primary_attrs = parse_material_color(description_primary_section_text(payload))

    assert "Nasza bogata gama kolorów" in full_text
    assert "Nasza bogata gama kolorów" not in description_primary_section_text(payload)
    assert primary_attrs == {"material": "CARBON", "color": "czarny"}


def test_sales_channels_page_renders_matrix_links_and_status_filters(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    monkeypatch.setattr(
        app_module, "allegro_description_write_configured", lambda: True
    )
    monkeypatch.setattr(app_module, "erli_palette_write_configured", lambda: True)
    apply_product_snapshot(
        app_module.DB_PATH,
        [
            {
                "id": 801,
                "sku": "SKU-801",
                "ean": "5900000000801",
                "name": "Produkt obecny",
                "quantity": 4,
                "status": 1,
            },
            {
                "id": 802,
                "sku": "SKU-802",
                "ean": "5900000000802",
                "name": "Produkt brakujący",
                "quantity": 2,
                "status": 1,
            },
        ],
        image_map={},
        auction_map={801: "12345678901"},
        attributes_map={},
        price_map={},
        replace_auction_data=True,
        sales_channels=[
            {
                "channel_key": "allegro",
                "channel_name": "Allegro",
                "platform_id": 3,
                "alias": "AL",
                "sort_order": 20,
            },
            {
                "channel_key": "erli",
                "channel_name": "Erli",
                "platform_id": 6,
                "alias": "ER",
                "sort_order": 30,
            },
        ],
        channel_listings=[
            {
                "apilo_product_id": 801,
                "channel_key": "allegro",
                "apilo_auction_id": 91,
                "external_id": "12345678901",
                "status": 2,
                "listing_name": "Produkt obecny",
                "offer_price": 49.99,
                "offer_quantity": 4,
            },
            {
                "apilo_product_id": 801,
                "channel_key": "erli",
                "apilo_auction_id": 92,
                "external_id": "23456789012",
                "status": 80,
                "listing_name": "Produkt obecny",
                "offer_price": 51.99,
                "offer_quantity": 3,
            },
        ],
    )
    replace_apilo_description_references(
        app_module.DB_PATH,
        [
            {
                "apilo_product_id": 801,
                "ean": "5900000000801",
                "sku": "SKU-801",
                "description_html": "<p>Wzorcowy <strong>opis</strong> produktu</p><ul><li>Cecha</li></ul>",
                "description_preview": "Wzorcowy opis produktu\n\n• Cecha",
                "description_text": "wzorcowy opis produktu. Materiał: PLA",
                "description_hash": "hash-801",
                "export_price": 49.99,
                "export_quantity": 4,
            },
            {
                "apilo_product_id": 802,
                "ean": "5900000000802",
                "sku": "SKU-802",
                "description_html": "<p>Wzorcowy opis brakującego produktu</p>",
                "description_preview": "Wzorcowy opis brakującego produktu",
                "description_text": "wzorcowy opis brakującego produktu",
                "description_hash": "hash-802",
                "export_price": 59.99,
                "export_quantity": 2,
            },
        ],
        source_name="export.xlsx",
    )
    replace_channel_description_checks(
        app_module.DB_PATH,
        [
            {
                "apilo_product_id": 801,
                "channel_key": "allegro",
                "external_id": "12345678901",
                "reference_hash": "hash-801",
                "status": "mismatch",
                "source": "allegro_api",
                "actual_description_text": "Stary opis produktu",
                "palette_status": "match",
                "palette_material": "PLA",
                "palette_block_text": canonical_material_palette_text("PLA"),
                "palette_block_hash": "palette-hash-pla",
            },
            {
                "apilo_product_id": 801,
                "channel_key": "erli",
                "external_id": "23456789012",
                "reference_hash": "hash-801",
                "status": "mismatch",
                "source": "public_page",
                "actual_description_text": "Wzorcowy opis kanału z inną właściwością",
                "palette_status": "mismatch",
                "palette_material": "PLA",
                "palette_block_text": "Niepoprawny blok kolorów PLA",
                "palette_block_hash": "palette-hash-wrong",
            },
        ],
    )
    response = logged_in_client.get("/sales-channels")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Kanały sprzedaży" in html
    assert "Produkt obecny" in html
    assert "Produkt brakujący" in html
    assert "EAN: 5900000000801" in html
    assert "SKU: SKU-801" not in html
    assert "Apilo ID: 801" not in html
    assert "<h2>EmpikPlace API</h2>" not in html
    assert "Kontrola opisów:" in html
    assert "https://allegro.pl/oferta/12345678901" in html
    assert "https://erli.pl/produkt/produkt-obecny,23456789012" in html
    assert "Jest" in html
    assert "Nie ma" in html
    assert "Wymaga poprawy" in html
    assert "Opis wzorcowy Apilo" in html
    assert "Sprawdź opis" in html
    assert "apilo-description-dialog" in html
    assert "Kopiuj HTML" in html
    assert "Aktualizuj na Allegro" in html
    assert "Aktualizuj materiał i kolory" in html
    assert "bez użycia LLM" in html
    assert "Pokaż różnice" in html
    assert "Wzorcowy opis kanału z inną właściwością" not in html
    assert ".innerHTML" not in html
    assert "Opis zgodny" in html
    assert "Opis różny" in html
    assert "Kolory PLA zgodne" in html
    assert "Blok kolorów różny" in html
    assert "Treść albo układ akapitów lub list różni się od sztywnego wzorca." in html
    assert "49,99 zł · stan 4" in html

    filtered = logged_in_client.get("/sales-channels?channel=allegro&status=missing")
    filtered_html = filtered.get_data(as_text=True)
    assert filtered.status_code == 200
    assert "Produkt brakujący" in filtered_html
    assert "Produkt obecny" not in filtered_html

    palette_filtered = logged_in_client.get("/sales-channels?palette=mismatch")
    assert palette_filtered.status_code == 200
    assert "Produkt obecny" in palette_filtered.get_data(as_text=True)

    preview_response = logged_in_client.get("/sales-channels/apilo-description/801")
    preview = preview_response.get_json()
    assert preview_response.status_code == 200
    assert preview_response.headers["Cache-Control"] == "private, no-store"
    assert preview["description_preview"] == "Wzorcowy opis produktu\n\n• Cecha"
    assert preview["description_html"].startswith("<p>Wzorcowy")
    assert preview["ean"] == "5900000000801"
    assert preview["reference_hash"] == "hash-801"
    assert preview["reference_palette_material"] == "PLA"
    assert preview["apilo_url"].endswith("/warehouse/product/detail/801/")
    allegro_repair = next(
        item for item in preview["repairs"] if item["channel_key"] == "allegro"
    )
    assert allegro_repair["needs_repair"] is True
    assert allegro_repair["write_supported"] is True
    assert allegro_repair["palette_write_supported"] is False
    assert allegro_repair["expected_palette_material"] == "PLA"
    assert allegro_repair["recheck_supported"] is False
    erli_repair = next(item for item in preview["repairs"] if item["channel_key"] == "erli")
    assert erli_repair["needs_repair"] is True
    assert erli_repair["palette_write_supported"] is True
    assert erli_repair["recheck_supported"] is True
    assert erli_repair["diff"]["available"] is True
    assert erli_repair["diff"]["changed"] is True
    assert erli_repair["diff"]["missing_words"] > 0
    assert erli_repair["diff"]["added_words"] > 0
    assert erli_repair["palette_status"] == "mismatch"
    assert erli_repair["palette_diff"]["available"] is True
    assert erli_repair["palette_diff"]["changed"] is True

    monkeypatch.setattr(
        app_module,
        "update_allegro_offer_description",
        lambda target, reference: (
            "updated",
            {
                "apilo_product_id": target["apilo_product_id"],
                "channel_key": "allegro",
                "external_id": target["external_id"],
                "reference_hash": reference["description_hash"],
                "status": "match",
                "source": "allegro_api",
                "detail": "",
                "actual_description_text": "Wzorcowy opis produktu Cecha",
                "palette_status": "match",
                "palette_material": "PLA",
                "palette_block_text": canonical_material_palette_text("PLA"),
                "palette_block_hash": "palette-hash-pla",
            },
        ),
    )
    update_response = logged_in_client.post(
        "/sales-channels/update-allegro-description",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "12345678901",
            "reference_hash": "hash-801",
        },
    )
    assert update_response.status_code == 200
    assert update_response.headers["Cache-Control"] == "private, no-store"
    assert update_response.get_json()["description_updated"] is True
    assert update_response.get_json()["status"] == "match"
    assert update_response.get_json()["is_match"] is True
    allegro_check = next(
        item
        for item in get_channel_description_checks(app_module.DB_PATH)
        if item["channel_key"] == "allegro"
    )
    assert allegro_check["status"] == "match"

    monkeypatch.setattr(
        app_module,
        "update_allegro_offer_palette",
        lambda target, reference, expected_material: (
            "replaced",
            {
                "apilo_product_id": target["apilo_product_id"],
                "channel_key": "allegro",
                "external_id": target["external_id"],
                "reference_hash": reference["description_hash"],
                "status": "match",
                "source": "allegro_api",
                "detail": "",
                "actual_description_text": "Wzorcowy opis produktu Cecha",
                "palette_status": "match",
                "palette_material": expected_material,
                "palette_block_text": canonical_material_palette_text(
                    expected_material
                ),
                "palette_block_hash": "palette-hash-pla",
            },
            ["224017", "237206"],
        ),
    )
    palette_update = logged_in_client.post(
        "/sales-channels/update-allegro-palette",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "12345678901",
            "reference_hash": "hash-801",
        },
    )
    assert palette_update.status_code == 200
    palette_result = palette_update.get_json()
    assert palette_result["palette_updated"] is True
    assert palette_result["palette_status"] == "match"
    assert palette_result["palette_material"] == "PLA"
    assert palette_result["catalog_parameter_count"] == 2
    assert "Wyrównano też 2" in palette_result["message"]

    monkeypatch.setattr(
        app_module,
        "update_erli_offer_palette",
        lambda target, reference, expected_material: (
            "replaced",
            {
                "apilo_product_id": target["apilo_product_id"],
                "channel_key": "erli",
                "external_id": target["external_id"],
                "reference_hash": reference["description_hash"],
                "status": "mismatch",
                "source": "erli_api",
                "detail": "",
                "actual_description_text": "Wzorcowy opis kanału z inną właściwością",
                "palette_status": "match",
                "palette_material": expected_material,
                "palette_block_text": canonical_material_palette_text(
                    expected_material
                ),
                "palette_block_hash": "palette-hash-pla",
            },
            False,
        ),
    )
    erli_palette_update = logged_in_client.post(
        "/sales-channels/update-erli-palette",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "23456789012",
            "reference_hash": "hash-801",
        },
    )
    assert erli_palette_update.status_code == 200
    erli_palette_result = erli_palette_update.get_json()
    assert erli_palette_result["palette_updated"] is True
    assert erli_palette_result["palette_status"] == "match"
    assert erli_palette_result["status"] == "mismatch"
    assert erli_palette_result["is_match"] is False
    erli_check = next(
        item
        for item in get_channel_description_checks(app_module.DB_PATH)
        if item["channel_key"] == "erli"
    )
    assert erli_check["source"] == "erli_api"
    assert erli_check["palette_status"] == "match"

    stale_erli_update = logged_in_client.post(
        "/sales-channels/update-erli-palette",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "23456789012",
            "reference_hash": "stale-hash",
        },
    )
    assert stale_erli_update.status_code == 409

    stale_update = logged_in_client.post(
        "/sales-channels/update-allegro-description",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "12345678901",
            "reference_hash": "stale-hash",
        },
    )
    assert stale_update.status_code == 409

    wrong_offer_update = logged_in_client.post(
        "/sales-channels/update-allegro-description",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "external_id": "99999999999",
            "reference_hash": "hash-801",
        },
    )
    assert wrong_offer_update.status_code == 404

    set_setting(app_module.DB_PATH, "apilo_base_url", "javascript:alert(1)")
    unsafe_base_preview = logged_in_client.get(
        "/sales-channels/apilo-description/801"
    ).get_json()
    assert unsafe_base_preview["apilo_url"] == "https://apilo.com/pl/logowanie/"

    monkeypatch.setattr(
        app_module,
        "recheck_public_channel_description",
        lambda target, reference: {
            "apilo_product_id": target["apilo_product_id"],
            "channel_key": target["channel_key"],
            "external_id": target["external_id"],
            "reference_hash": reference["description_hash"],
            "status": "match",
            "source": "public_page",
            "detail": "",
            "actual_description_text": "Wzorcowy opis produktu Cecha",
            "palette_status": "match",
            "palette_material": "PLA",
            "palette_block_text": canonical_material_palette_text("PLA"),
            "palette_block_hash": "palette-hash-pla",
        },
    )
    recheck = logged_in_client.post(
        "/sales-channels/recheck-description",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "channel_key": "erli",
            "external_id": "23456789012",
        },
    )
    assert recheck.status_code == 200
    assert recheck.get_json()["status"] == "match"
    assert recheck.get_json()["is_match"] is True
    assert recheck.get_json()["diff"] is None
    assert recheck.get_json()["palette_status"] == "match"
    assert recheck.get_json()["palette_diff"] is None
    erli_check = next(
        item
        for item in get_channel_description_checks(app_module.DB_PATH)
        if item["channel_key"] == "erli"
    )
    assert erli_check["status"] == "match"

    unsupported = logged_in_client.post(
        "/sales-channels/recheck-description",
        data={
            "csrf_token": "test-csrf-token",
            "apilo_product_id": "801",
            "channel_key": "allegro",
            "external_id": "12345678901",
        },
    )
    assert unsupported.status_code == 400


def test_sales_channels_etsy_visibility_is_controlled_from_settings(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    apply_product_snapshot(
        app_module.DB_PATH,
        [
            {
                "id": 901,
                "sku": "SKU-901",
                "ean": "5900000000901",
                "name": "Produkt Etsy",
                "quantity": 1,
                "status": 1,
            }
        ],
        image_map={},
        auction_map={},
        attributes_map={},
        price_map={},
        replace_auction_data=True,
        sales_channels=[
            {
                "channel_key": "etsy",
                "channel_name": "Etsy",
                "platform_id": 30,
                "alias": "ET",
                "sort_order": 50,
            }
        ],
        channel_listings=[
            {
                "apilo_product_id": 901,
                "channel_key": "etsy",
                "apilo_auction_id": 902,
                "external_id": "45678901235",
                "status": 2,
                "listing_name": "Produkt Etsy",
                "offer_price": 12.34,
                "offer_quantity": 1,
            }
        ],
    )

    hidden = logged_in_client.get("/sales-channels")
    hidden_html = hidden.get_data(as_text=True)
    assert hidden.status_code == 200
    assert "https://www.etsy.com/listing/45678901235" not in hidden_html
    assert "Brak aktywnych kanałów w widoku" in hidden_html

    settings_page = logged_in_client.get("/settings")
    assert "Pokaż Etsy w tabeli kanałów" in settings_page.get_data(as_text=True)

    enabled = logged_in_client.post(
        "/settings",
        data={
            "action": "channel_visibility",
            "sales_channels_show_etsy": "1",
            "csrf_token": "test-csrf-token",
        },
    )
    assert enabled.status_code == 302
    assert get_setting(app_module.DB_PATH, "sales_channels_show_etsy") == "1"

    visible = logged_in_client.get("/sales-channels")
    visible_html = visible.get_data(as_text=True)
    assert visible.status_code == 200
    assert "https://www.etsy.com/listing/45678901235" in visible_html
    assert "<th>Etsy</th>" in visible_html

    disabled = logged_in_client.post(
        "/settings",
        data={
            "action": "channel_visibility",
            "csrf_token": "test-csrf-token",
        },
    )
    assert disabled.status_code == 302
    assert get_setting(app_module.DB_PATH, "sales_channels_show_etsy") == "0"
    audit_rows = get_recent_audit_log(app_module.DB_PATH, limit=5)
    assert audit_rows[0]["action"] == "channel_visibility_settings_update"


def test_empik_settings_store_secret_without_rendering_it(
    app_module, logged_in_client
):
    response = logged_in_client.post(
        "/settings",
        data={
            "action": "empik",
            "empik_api_key": "empik-super-secret",
            "empik_shop_id": "2811",
            "csrf_token": "test-csrf-token",
        },
    )

    assert response.status_code == 302
    assert get_setting(app_module.DB_PATH, "empik_api_key") == "empik-super-secret"
    assert get_setting(app_module.DB_PATH, "empik_shop_id") == "2811"
    page = logged_in_client.get("/settings")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "EmpikPlace API" in html
    assert "Klucz ustawiony" in html
    assert "empik-super-secret" not in html


def test_empik_sync_enriches_channel_page_and_keeps_writes_in_apilo(
    app_module, logged_in_client, monkeypatch
):
    monkeypatch.setattr(app_module, "tokens_missing", lambda: False)
    set_setting(app_module.DB_PATH, "empik_api_key", "empik-test-key")
    apply_product_snapshot(
        app_module.DB_PATH,
        [
            {
                "id": 803,
                "sku": "SKU-803",
                "ean": "5900000000803",
                "name": "Produkt Empik API",
                "quantity": 4,
                "status": 1,
            }
        ],
        image_map={},
        auction_map={},
        attributes_map={},
        price_map={},
        replace_auction_data=True,
        sales_channels=[
            {
                "channel_key": "empik",
                "channel_name": "Empik",
                "platform_id": 36,
                "alias": "EM",
                "sort_order": 40,
            }
        ],
        channel_listings=[
            {
                "apilo_product_id": 803,
                "channel_key": "empik",
                "apilo_auction_id": 93,
                "external_id": "9803",
                "status": 2,
                "listing_name": "Produkt Empik API",
            }
        ],
    )

    class FakeEmpikClient:
        def list_offers(self):
            return [
                {
                    "offer_id": 9803,
                    "shop_sku": "SKU-803",
                    "product_sku": "5900000000803",
                    "active": True,
                    "state_code": "11",
                    "quantity": 6,
                    "price": 49.99,
                    "product": {"title": "Produkt Empik API"},
                }
            ]

        def list_offer_imports(self, days=30):
            assert days == 30
            return [
                {
                    "import_id": 7803,
                    "date_created": "2026-07-26T12:00:00Z",
                    "status": "COMPLETE",
                    "has_error_report": True,
                    "lines_read": 2,
                    "lines_in_success": 1,
                    "lines_in_error": 1,
                }
            ]

    monkeypatch.setattr(app_module, "get_empik_client", lambda: FakeEmpikClient())

    response = logged_in_client.post(
        "/sales-channels/empik-sync",
        data={"csrf_token": "test-csrf-token", "next": "/sales-channels?channel=empik"},
    )
    assert response.status_code == 302

    page = logged_in_client.get("/sales-channels?channel=empik")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "<h2>EmpikPlace API</h2>" not in html
    assert "API: aktywna · stan 6 · 49,99 zł" in html
    assert "kod stanu 11" in html
    assert "Pobierz raport OF03" not in html

    settings_page = logged_in_client.get("/settings")
    settings_html = settings_page.get_data(as_text=True)
    assert settings_page.status_code == 200
    assert "<h2>EmpikPlace API</h2>" in settings_html
    assert "Pobierz raport OF03" in settings_html
    assert "Ceny i stany nadal zapisuje Apilo" in settings_html


def test_empik_error_report_download_is_bounded_to_known_import(
    app_module, logged_in_client, monkeypatch
):
    set_setting(app_module.DB_PATH, "empik_api_key", "empik-test-key")
    app_module.replace_empik_snapshot(
        app_module.DB_PATH,
        [],
        [
            {
                "import_id": 9001,
                "date_created": "2026-07-26T12:00:00Z",
                "status": "COMPLETE",
                "has_error_report": True,
                "lines_in_error": 1,
            }
        ],
    )

    class FakeEmpikClient:
        def get_offer_import_error_report(self, import_id):
            assert import_id == 9001
            return b"sku;error\nSKU-1;invalid\n", "text/csv"

    monkeypatch.setattr(app_module, "get_empik_client", lambda: FakeEmpikClient())

    response = logged_in_client.get(
        "/sales-channels/empik-imports/9001/error-report"
    )
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.headers["Cache-Control"] == "no-store"
    assert b"SKU-1;invalid" in response.data

    missing = logged_in_client.get(
        "/sales-channels/empik-imports/9002/error-report"
    )
    assert missing.status_code == 404


def test_empik_empty_result_preserves_previous_snapshot_without_apilo_listings(
    app_module, monkeypatch
):
    app_module.replace_empik_snapshot(
        app_module.DB_PATH,
        [
            {
                "offer_id": 9101,
                "shop_sku": "SKU-9101",
                "active": True,
                "quantity": 3,
                "price": 19.99,
            }
        ],
        [],
    )

    class EmptyEmpikClient:
        def list_offers(self):
            return []

    monkeypatch.setattr(app_module, "get_empik_client", lambda: EmptyEmpikClient())

    with pytest.raises(app_module.EmpikClientError, match="Poprzednie dane"):
        app_module.perform_empik_sync()

    offers = app_module.get_empik_offers(app_module.DB_PATH)
    assert [offer["offer_id"] for offer in offers] == ["9101"]


def test_empik_empty_result_preserves_import_only_snapshot(
    app_module, monkeypatch
):
    app_module.replace_empik_snapshot(
        app_module.DB_PATH,
        [],
        [
            {
                "import_id": 9201,
                "date_created": "2026-07-26T12:00:00Z",
                "status": "COMPLETE",
                "has_error_report": True,
                "lines_in_error": 1,
            }
        ],
    )

    class EmptyEmpikClient:
        def list_offers(self):
            return []

    monkeypatch.setattr(app_module, "get_empik_client", lambda: EmptyEmpikClient())

    with pytest.raises(app_module.EmpikClientError, match="Poprzednie dane"):
        app_module.perform_empik_sync()

    imports = app_module.get_empik_offer_imports(app_module.DB_PATH)
    assert [item["import_id"] for item in imports] == ["9201"]
