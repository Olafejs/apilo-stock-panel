from app_channels import (
    build_channel_listing_rows,
    build_channel_matrix,
    build_listing_url,
    build_sales_channels,
)


def sample_products():
    return [
        {
            "id": 101,
            "name": "Testowy uchwyt na akcesoria",
            "sku": "T4-101",
            "ean": "5900000000101",
        },
        {
            "id": 102,
            "name": "Drugi produkt",
            "sku": "SKU-102",
            "ean": "5900000000102",
        },
    ]


def sample_platforms():
    return [
        {"id": 3, "name": "Allegro", "alias": "AL"},
        {"id": 6, "name": "Erli", "alias": "ER"},
        {"id": 9, "name": "PrestaShop", "alias": "PR"},
        {"id": 24, "name": "Zamówienie ręczne", "alias": "MA"},
        {"id": 36, "name": "Empik", "alias": "EM"},
        {"id": 39, "name": "Etsy", "alias": "ET"},
    ]


def auction(
    auction_id,
    platform_id,
    external_id,
    product_id,
    *,
    status=2,
    name="Produkt",
    price=39.99,
    quantity=7,
):
    return {
        "id": auction_id,
        "idExternal": external_id,
        "name": name,
        "status": status,
        "platformAccount": {"id": platform_id},
        "auctionProducts": [
            {
                "product": {"id": product_id},
                "priceWithTax": price,
                "quantitySelling": quantity,
            }
        ],
    }


def test_build_channel_rows_includes_all_real_sales_channels_and_excludes_manual_orders():
    channels, rows = build_channel_listing_rows(
        sample_products(),
        sample_platforms(),
        [
            auction(1, 3, "9001", 101),
            auction(2, 6, "23456789012", 101, name="Testowy uchwyt na akcesoria"),
            auction(3, 24, "manual-1", 101),
        ],
    )

    assert [channel["channel_key"] for channel in channels] == [
        "prestashop",
        "allegro",
        "erli",
        "empik",
        "etsy",
    ]
    assert {(row["channel_key"], row["external_id"]) for row in rows} == {
        ("allegro", "9001"),
        ("erli", "23456789012"),
    }
    assert rows[0]["offer_price"] == 39.99
    assert rows[0]["offer_quantity"] == 7


def test_duplicate_platform_alias_is_deduplicated_but_all_accounts_are_mapped():
    platforms = sample_platforms() + [
        {"id": 43, "name": "Allegro drugie konto", "alias": "AL"}
    ]
    channels, rows = build_channel_listing_rows(
        sample_products(),
        platforms,
        [
            auction(1, 3, "9001", 101),
            auction(2, 43, "9002", 102),
        ],
    )

    assert [channel["channel_key"] for channel in channels].count("allegro") == 1
    assert {(row["apilo_product_id"], row["external_id"]) for row in rows} == {
        (101, "9001"),
        (102, "9002"),
    }


def test_channel_matrix_marks_present_missing_and_duplicate_or_inactive_offers():
    channels = build_sales_channels(sample_platforms())
    listings = [
        {
            "apilo_product_id": 101,
            "channel_key": "allegro",
            "apilo_auction_id": 1,
            "external_id": "9001",
            "status": 2,
            "listing_name": "Uchwyt",
        },
        {
            "apilo_product_id": 101,
            "channel_key": "empik",
            "apilo_auction_id": 2,
            "external_id": "8001",
            "status": 2,
            "listing_name": "Uchwyt",
        },
        {
            "apilo_product_id": 101,
            "channel_key": "empik",
            "apilo_auction_id": 3,
            "external_id": "8002",
            "status": 2,
            "listing_name": "Uchwyt duplikat",
        },
        {
            "apilo_product_id": 102,
            "channel_key": "allegro",
            "apilo_auction_id": 4,
            "external_id": "9002",
            "status": 82,
            "listing_name": "Drugi produkt",
        },
    ]
    products = [{"apilo_id": p["id"], **p} for p in sample_products()]

    matrix = build_channel_matrix(products, channels, listings, limit=50)
    first = matrix["rows"][0]
    second = matrix["rows"][1]

    assert first["product"]["apilo_id"] == 102
    assert first["cells"]["allegro"]["status"] == "review"
    assert first["cells"]["erli"]["status"] == "missing"
    assert second["product"]["apilo_id"] == 101
    assert second["cells"]["allegro"]["status"] == "ok"
    assert second["cells"]["empik"]["status"] == "review"
    assert len(second["cells"]["empik"]["listings"]) == 2


def test_empik_api_data_verifies_active_offer_and_marks_inconsistencies():
    channels = build_sales_channels(sample_platforms())
    products = [{"apilo_id": p["id"], **p} for p in sample_products()]
    listings = [
        {
            "apilo_product_id": 101,
            "channel_key": "empik",
            "apilo_auction_id": 2,
            "external_id": "8001",
            "status": 2,
            "listing_name": "Uchwyt",
        }
    ]

    verified = build_channel_matrix(
        products,
        channels,
        listings,
        empik_offers=[
            {
                "offer_id": "8001",
                "shop_sku": "T4-101",
                "product_sku": "5900000000101",
                "active": 1,
                "state_code": "11",
                "quantity": 7,
                "price": 39.99,
            },
            {
                "offer_id": "8002",
                "shop_sku": "SKU-102",
                "product_sku": "5900000000102",
                "active": 1,
                "state_code": "11",
                "quantity": 2,
                "price": 19.99,
            },
        ],
        empik_api_enabled=True,
        limit=50,
    )

    by_id = {row["product"]["apilo_id"]: row for row in verified["rows"]}
    first_cell = by_id[101]["cells"]["empik"]
    second_cell = by_id[102]["cells"]["empik"]
    assert first_cell["status"] == "ok"
    assert first_cell["direct_match"] == "offer_id"
    assert first_cell["direct_offers"][0]["quantity"] == 7
    assert second_cell["status"] == "review"
    assert second_cell["direct_match"] == "shop_sku"

    inactive = build_channel_matrix(
        products[:1],
        channels,
        listings,
        empik_offers=[{"offer_id": "8001", "active": 0}],
        empik_api_enabled=True,
        limit=50,
    )
    assert inactive["rows"][0]["cells"]["empik"]["status"] == "review"


def test_matrix_filters_by_selected_channel_and_status():
    channels = build_sales_channels(sample_platforms())
    products = [{"apilo_id": p["id"], **p} for p in sample_products()]
    listings = [
        {
            "apilo_product_id": 101,
            "channel_key": "allegro",
            "apilo_auction_id": 1,
            "external_id": "9001",
            "status": 2,
            "listing_name": "Uchwyt",
        }
    ]

    matrix = build_channel_matrix(
        products,
        channels,
        listings,
        channel_filter="allegro",
        status_filter="missing",
        limit=50,
    )

    assert [row["product"]["apilo_id"] for row in matrix["rows"]] == [102]


def test_matrix_compares_description_checks_with_current_apilo_reference():
    channels = build_sales_channels(sample_platforms())
    products = [{"apilo_id": p["id"], **p} for p in sample_products()]
    listings = [
        {
            "apilo_product_id": 101,
            "channel_key": "allegro",
            "apilo_auction_id": 1,
            "external_id": "9001",
            "status": 2,
            "listing_name": "Uchwyt",
            "offer_price": 39.99,
            "offer_quantity": 7,
        },
        {
            "apilo_product_id": 102,
            "channel_key": "allegro",
            "apilo_auction_id": 2,
            "external_id": "9002",
            "status": 2,
            "listing_name": "Drugi produkt",
            "offer_price": 19.99,
            "offer_quantity": 2,
        },
    ]
    references = [
        {"apilo_product_id": 101, "description_hash": "hash-101"},
        {"apilo_product_id": 102, "description_hash": "hash-102"},
    ]
    checks = [
        {
            "apilo_product_id": 101,
            "channel_key": "allegro",
            "external_id": "9001",
            "reference_hash": "hash-101",
            "status": "match",
        },
        {
            "apilo_product_id": 102,
            "channel_key": "allegro",
            "external_id": "9002",
            "reference_hash": "hash-102",
            "status": "mismatch",
        },
    ]

    matrix = build_channel_matrix(
        products,
        channels,
        listings,
        description_references=references,
        description_checks=checks,
        description_filter="mismatch",
        channel_filter="allegro",
        limit=50,
    )

    assert [row["product"]["apilo_id"] for row in matrix["rows"]] == [102]
    assert matrix["description_totals"]["match"] == 1
    assert matrix["description_totals"]["mismatch"] == 1
    listing = matrix["rows"][0]["cells"]["allegro"]["listings"][0]
    assert listing["offer_price"] == 19.99
    assert listing["offer_quantity"] == 2


def test_matrix_checks_palette_blocks_separately_across_allegro_and_erli():
    channels = build_sales_channels(sample_platforms())
    products = [{"apilo_id": 101, **sample_products()[0]}]
    listings = [
        {
            "apilo_product_id": 101,
            "channel_key": channel_key,
            "apilo_auction_id": index,
            "external_id": external_id,
            "status": 2,
            "listing_name": "Uchwyt",
        }
        for index, (channel_key, external_id) in enumerate(
            (("allegro", "9001"), ("erli", "7001")), start=1
        )
    ]
    references = [{"apilo_product_id": 101, "description_hash": "hash-101"}]
    checks = [
        {
            "apilo_product_id": 101,
            "channel_key": "allegro",
            "external_id": "9001",
            "reference_hash": "hash-101",
            "status": "match",
            "palette_status": "match",
            "palette_material": "PLA",
        },
        {
            "apilo_product_id": 101,
            "channel_key": "erli",
            "external_id": "7001",
            "reference_hash": "hash-101",
            "status": "match",
            "palette_status": "absent",
            "palette_material": "",
        },
    ]

    matrix = build_channel_matrix(
        products,
        channels,
        listings,
        description_references=references,
        description_checks=checks,
        palette_filter="mismatch",
        limit=50,
    )

    row = matrix["rows"][0]
    assert row["cells"]["allegro"]["palette_status"] == "match"
    assert row["cells"]["allegro"]["palette_material"] == "PLA"
    assert row["cells"]["erli"]["palette_status"] == "missing"
    assert matrix["palette_totals"]["match"] == 1
    assert matrix["palette_totals"]["mismatch"] == 1


def test_public_listing_links_use_direct_urls_and_empik_ean_search_fallback():
    product = {"name": "Uchwyt", "ean": "5900000000101"}
    listing = {"external_id": "23456789012", "listing_name": "Testowy uchwyt na akcesoria"}

    assert build_listing_url("allegro", {"external_id": "12345678901"}, product) == (
        "https://allegro.pl/oferta/12345678901"
    )
    assert build_listing_url("erli", listing, product) == (
        "https://erli.pl/produkt/testowy-uchwyt-na-akcesoria,23456789012"
    )
    assert build_listing_url("prestashop", {"external_id": "212"}, product).endswith(
        "id_product=212&controller=product"
    )
    assert build_listing_url("empik", {"external_id": "34567890123"}, product) == (
        "https://www.empik.com/szukaj/produkt?q=5900000000101"
    )
    assert build_listing_url("etsy", {"external_id": "45678901234"}, product) == (
        "https://www.etsy.com/listing/45678901234"
    )
