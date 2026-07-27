import sqlite3

import pytest

from db import (
    apply_product_snapshot,
    get_channel_listings,
    get_empik_offer_import,
    get_empik_offer_imports,
    get_empik_offers,
    get_product_by_id,
    get_products,
    get_sales_channels,
    get_setting,
    get_tokens,
    migrate_secret_storage,
    replace_empik_snapshot,
    save_tokens,
    set_setting,
    update_product_attributes,
    update_product_attributes_manual,
    upsert_product_from_apilo,
)


def test_secret_settings_are_encrypted_at_rest(app_module):
    set_setting(app_module.DB_PATH, "smtp_password", "smtp-tajne")

    conn = sqlite3.connect(app_module.DB_PATH)
    raw_value = conn.execute(
        "SELECT value FROM settings WHERE key = 'smtp_password'"
    ).fetchone()[0]
    conn.close()

    assert raw_value.startswith("enc:v1:")
    assert get_setting(app_module.DB_PATH, "smtp_password") == "smtp-tajne"


def test_tokens_are_encrypted_and_legacy_plaintext_can_be_migrated(app_module):
    save_tokens(
        app_module.DB_PATH,
        {
            "access_token": "access-secret",
            "access_token_expires_at": "2026-03-12T00:00:00+00:00",
            "refresh_token": "refresh-secret",
            "refresh_token_expires_at": "2026-03-13T00:00:00+00:00",
        },
    )

    conn = sqlite3.connect(app_module.DB_PATH)
    encrypted_tokens = conn.execute(
        "SELECT access_token, refresh_token FROM tokens WHERE id = 1"
    ).fetchone()
    assert encrypted_tokens[0].startswith("enc:v1:")
    assert encrypted_tokens[1].startswith("enc:v1:")

    conn.execute("DELETE FROM tokens")
    conn.execute(
        """
        INSERT INTO tokens (
            id,
            access_token,
            access_token_expires_at,
            refresh_token,
            refresh_token_expires_at,
            updated_at
        ) VALUES (1, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-access",
            "2026-03-12T00:00:00+00:00",
            "legacy-refresh",
            "2026-03-13T00:00:00+00:00",
            "2026-03-11T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    migrated = migrate_secret_storage(app_module.DB_PATH)
    tokens = get_tokens(app_module.DB_PATH)
    conn = sqlite3.connect(app_module.DB_PATH)
    migrated_tokens = conn.execute(
        "SELECT access_token, refresh_token FROM tokens WHERE id = 1"
    ).fetchone()
    conn.close()

    assert migrated["tokens"] == 2
    assert migrated_tokens[0].startswith("enc:v1:")
    assert migrated_tokens[1].startswith("enc:v1:")
    assert tokens["access_token"] == "legacy-access"
    assert tokens["refresh_token"] == "legacy-refresh"


def test_get_products_without_limit_returns_all_filtered_rows(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 201,
            "originalCode": "ALFA-1",
            "sku": "SKU-ALFA",
            "ean": "5900000000201",
            "name": "Produkt Alfa",
            "priceWithTax": 10.0,
            "priceWithoutTax": 8.13,
            "quantity": 4,
            "status": 1,
        },
    )
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 202,
            "originalCode": "BETA-2",
            "sku": "SKU-BETA",
            "ean": "5900000000202",
            "name": "Produkt Beta",
            "priceWithTax": 20.0,
            "priceWithoutTax": 16.26,
            "quantity": 2,
            "status": 1,
        },
    )

    rows = get_products(
        app_module.DB_PATH,
        search="Produkt",
        sort="name",
        order="asc",
        limit=None,
        offset=0,
    )

    assert [row["name"] for row in rows] == ["Produkt Alfa", "Produkt Beta"]


def test_product_snapshot_is_atomic_and_hides_missing_remote_products(app_module):
    for product_id in (301, 302):
        upsert_product_from_apilo(
            app_module.DB_PATH,
            {
                "id": product_id,
                "sku": f"SKU-{product_id}",
                "name": f"Stary {product_id}",
                "quantity": 1,
                "status": 1,
            },
        )

    result = apply_product_snapshot(
        app_module.DB_PATH,
        [
            {
                "id": 301,
                "sku": "SKU-301",
                "name": "Nowa nazwa",
                "quantity": 7,
                "status": 1,
                "priceWithTax": 12.5,
            },
            {
                "id": 303,
                "sku": "SKU-303",
                "name": "Nowy produkt",
                "quantity": 4,
                "status": 1,
                "priceWithTax": 20.0,
            },
        ],
        image_map={301: "https://example.com/301.jpg"},
        auction_map={301: "123456789"},
        attributes_map={
            301: {"material": "PLA", "color": "czarny", "source": "test"}
        },
        price_map={301: "14.99", 303: "23.99"},
        replace_auction_data=True,
        synced_at="2026-07-18T20:00:00+00:00",
    )

    rows = get_products(app_module.DB_PATH, sort="name", limit=None)
    assert {row["apilo_id"] for row in rows} == {301, 303}
    row_301 = next(row for row in rows if row["apilo_id"] == 301)
    assert row_301["name"] == "Nowa nazwa"
    assert row_301["image_url"] == "https://example.com/301.jpg"
    assert row_301["allegro_auction_id"] == "123456789"
    assert row_301["material"] == "PLA"
    assert row_301["allegro_price_with_tax"] == 14.99
    assert get_setting(app_module.DB_PATH, "last_pull_at") == "2026-07-18T20:00:00+00:00"
    assert result["active_count"] == 2
    assert result["deactivated_count"] == 1

    connection = sqlite3.connect(app_module.DB_PATH)
    stale = connection.execute(
        "SELECT present_in_apilo FROM products WHERE apilo_id = 302"
    ).fetchone()
    connection.close()
    assert stale == (0,)


def test_invalid_product_snapshot_leaves_previous_state_unchanged(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 401, "name": "Produkt bazowy", "quantity": 2, "status": 1},
    )

    with pytest.raises(ValueError, match="identyfikator"):
        apply_product_snapshot(
            app_module.DB_PATH,
            [
                {"id": 402, "name": "Nie zapisuj", "quantity": 3, "status": 1},
                {"id": None, "name": "Błędny", "quantity": 4, "status": 1},
            ],
            image_map={},
            auction_map={},
            attributes_map={},
            price_map={},
            replace_auction_data=True,
        )

    rows = get_products(app_module.DB_PATH, limit=None)
    assert [(row["apilo_id"], row["name"]) for row in rows] == [
        (401, "Produkt bazowy")
    ]


def test_empty_remote_snapshot_cannot_hide_existing_inventory(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 501, "name": "Nie ukrywaj", "quantity": 2, "status": 1},
    )

    with pytest.raises(ValueError, match="Pusty snapshot"):
        apply_product_snapshot(
            app_module.DB_PATH,
            [],
            image_map={},
            auction_map={},
            attributes_map={},
            price_map={},
            replace_auction_data=True,
        )

    assert get_products(app_module.DB_PATH, limit=None)[0]["apilo_id"] == 501


def test_manual_product_attributes_survive_automatic_refresh_and_snapshot(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {"id": 601, "name": "Produkt CARBON", "quantity": 2, "status": 1},
    )
    product = get_products(app_module.DB_PATH, search="Produkt CARBON", limit=1)[0]

    update_product_attributes_manual(
        app_module.DB_PATH,
        product["id"],
        material="CARBON",
        color="czarny",
    )
    update_product_attributes(
        app_module.DB_PATH,
        {601: {"material": "PLA", "color": "biały", "source": "allegro_description"}},
    )
    apply_product_snapshot(
        app_module.DB_PATH,
        [{"id": 601, "name": "Produkt CARBON", "quantity": 2, "status": 1}],
        image_map={},
        auction_map={},
        attributes_map={
            601: {"material": "PETG", "color": "szary", "source": "apilo_auction_description"}
        },
        price_map={},
        replace_auction_data=True,
    )

    refreshed = get_product_by_id(app_module.DB_PATH, product["id"])
    assert refreshed["material"] == "CARBON"
    assert refreshed["color"] == "czarny"
    assert refreshed["attributes_source"] == "manual_user_hint"
    assert refreshed["attributes_updated_at"]


def test_product_snapshot_replaces_sales_channel_matrix_atomically(app_module):
    apply_product_snapshot(
        app_module.DB_PATH,
        [{"id": 701, "name": "Produkt kanałowy", "quantity": 2, "status": 1}],
        image_map={},
        auction_map={701: "9001"},
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
                "apilo_product_id": 701,
                "channel_key": "allegro",
                "apilo_auction_id": 81,
                "external_id": "9001",
                "status": 2,
                "listing_name": "Produkt kanałowy",
            },
            {
                "apilo_product_id": 999,
                "channel_key": "erli",
                "apilo_auction_id": 82,
                "external_id": "8001",
                "status": 2,
                "listing_name": "Nieaktywny produkt",
            },
        ],
        synced_at="2026-07-26T12:00:00+00:00",
    )

    assert [channel["channel_key"] for channel in get_sales_channels(app_module.DB_PATH)] == [
        "allegro",
        "erli",
    ]
    listings = get_channel_listings(app_module.DB_PATH)
    assert len(listings) == 1
    assert listings[0]["apilo_product_id"] == 701
    assert listings[0]["external_id"] == "9001"


def test_empik_api_key_is_encrypted_at_rest(app_module):
    set_setting(app_module.DB_PATH, "empik_api_key", "empik-tajny-klucz")

    conn = sqlite3.connect(app_module.DB_PATH)
    raw_value = conn.execute(
        "SELECT value FROM settings WHERE key = 'empik_api_key'"
    ).fetchone()[0]
    conn.close()

    assert raw_value.startswith("enc:v1:")
    assert "empik-tajny-klucz" not in raw_value
    assert get_setting(app_module.DB_PATH, "empik_api_key") == "empik-tajny-klucz"


def test_empik_snapshot_is_atomic_and_exposes_offer_and_import_details(app_module):
    result = replace_empik_snapshot(
        app_module.DB_PATH,
        [
            {
                "offer_id": 901,
                "shop_sku": "SKU-901",
                "product_sku": "5900000000901",
                "active": True,
                "state_code": "11",
                "quantity": 8,
                "price": "29.99",
                "product": {"title": "Produkt Empik"},
            }
        ],
        [
            {
                "import_id": 701,
                "date_created": "2026-07-26T12:00:00Z",
                "status": "COMPLETE",
                "has_error_report": True,
                "lines_read": 2,
                "lines_in_success": 1,
                "lines_in_error": 1,
                "offer_updated": 1,
            }
        ],
        synced_at="2026-07-26T12:05:00+00:00",
    )

    assert result == {
        "offers": 1,
        "imports": 1,
        "synced_at": "2026-07-26T12:05:00+00:00",
    }
    offers = get_empik_offers(app_module.DB_PATH)
    assert offers[0]["offer_id"] == "901"
    assert offers[0]["active"] == 1
    assert offers[0]["price"] == 29.99
    imports = get_empik_offer_imports(app_module.DB_PATH)
    assert imports[0]["lines_in_error"] == 1
    saved_import = get_empik_offer_import(app_module.DB_PATH, 701)
    assert saved_import is not None
    assert saved_import["has_error_report"] == 1
    assert get_setting(app_module.DB_PATH, "empik_last_sync_at") == "2026-07-26T12:05:00+00:00"

    with pytest.raises(ValueError, match="zduplikowany"):
        replace_empik_snapshot(
            app_module.DB_PATH,
            [{"offer_id": 902}, {"offer_id": 902}],
            [],
        )

    assert [row["offer_id"] for row in get_empik_offers(app_module.DB_PATH)] == ["901"]
