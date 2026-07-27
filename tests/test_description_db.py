import sqlite3

import pytest

from db import (
    get_apilo_description_reference,
    get_apilo_description_reference_status,
    get_apilo_description_references,
    get_channel_description_checks,
    init_db,
    replace_apilo_description_references,
    replace_channel_description_checks,
    upsert_channel_description_check,
    upsert_product_from_apilo,
)


def _reference(product_id, text_hash):
    return {
        "apilo_product_id": product_id,
        "ean": f"5900000000{product_id}",
        "sku": f"SKU-{product_id}",
        "description_html": f"<p>Opis produktu {product_id}</p>",
        "description_preview": f"Opis produktu {product_id}",
        "description_text": f"opis produktu {product_id}",
        "description_hash": text_hash,
        "export_price": 39.99,
        "export_quantity": 7,
    }


def _product(product_id):
    return {
        "id": product_id,
        "sku": f"SKU-{product_id}",
        "ean": f"5900000000{product_id}",
        "name": f"Produkt {product_id}",
        "quantity": 7,
        "status": 1,
    }


def test_description_reference_snapshot_and_checks_are_atomic(app_module):
    upsert_product_from_apilo(
        app_module.DB_PATH,
        {
            "id": 101,
            "sku": "SKU-101",
            "ean": "5900000000101",
            "name": "Uchwyt",
            "quantity": 7,
            "status": 1,
        },
    )
    imported = replace_apilo_description_references(
        app_module.DB_PATH,
        [_reference(101, "hash-101")],
        source_name="export.xlsx",
        imported_at="2026-07-26T18:38:52+00:00",
    )
    replace_channel_description_checks(
        app_module.DB_PATH,
        [
            {
                "apilo_product_id": 101,
                "channel_key": "allegro",
                "external_id": "9001",
                "reference_hash": "hash-101",
                "status": "match",
                "source": "allegro_api",
                "actual_description_text": "Opis odczytany z Allegro",
                "palette_status": "match",
                "palette_material": "PLA",
                "palette_block_text": "Wzorcowy blok PLA",
                "palette_block_hash": "palette-hash-pla",
            }
        ],
        checked_at="2026-07-26T19:00:00+00:00",
    )

    assert imported == 1
    assert get_apilo_description_reference_status(app_module.DB_PATH)["count"] == 1
    assert get_apilo_description_references(app_module.DB_PATH)[0]["export_quantity"] == 7
    reference = get_apilo_description_reference(app_module.DB_PATH, 101)
    assert reference is not None
    assert reference["description_html"] == "<p>Opis produktu 101</p>"
    assert reference["description_preview"] == "Opis produktu 101"
    stored_check = get_channel_description_checks(app_module.DB_PATH)[0]
    assert stored_check["status"] == "match"
    assert stored_check["actual_description_text"] == "Opis odczytany z Allegro"
    assert stored_check["palette_status"] == "match"
    assert stored_check["palette_material"] == "PLA"
    assert stored_check["palette_block_text"] == "Wzorcowy blok PLA"
    assert stored_check["palette_block_hash"] == "palette-hash-pla"
    assert get_channel_description_checks(
        app_module.DB_PATH, apilo_product_id=101
    ) == [stored_check]
    assert get_channel_description_checks(
        app_module.DB_PATH, apilo_product_id=999
    ) == []

    replace_apilo_description_references(
        app_module.DB_PATH,
        [_reference(101, "hash-new")],
        source_name="export-new.xlsx",
    )
    assert get_channel_description_checks(app_module.DB_PATH) == []


def test_partial_reference_export_is_rejected_and_preserves_snapshot(app_module):
    upsert_product_from_apilo(app_module.DB_PATH, _product(201))
    upsert_product_from_apilo(app_module.DB_PATH, _product(202))
    replace_apilo_description_references(
        app_module.DB_PATH,
        [_reference(201, "hash-201"), _reference(202, "hash-202")],
        source_name="complete.xlsx",
    )

    with pytest.raises(ValueError, match="dokładnie wszystkie"):
        replace_apilo_description_references(
            app_module.DB_PATH,
            [_reference(201, "hash-new")],
            source_name="partial.xlsx",
        )

    references = get_apilo_description_references(app_module.DB_PATH)
    assert [item["description_hash"] for item in references] == ["hash-201", "hash-202"]


def test_empty_description_check_does_not_delete_previous_results(app_module):
    upsert_product_from_apilo(app_module.DB_PATH, _product(301))
    replace_apilo_description_references(
        app_module.DB_PATH,
        [_reference(301, "hash-301")],
        source_name="complete.xlsx",
    )
    existing = {
        "apilo_product_id": 301,
        "channel_key": "allegro",
        "external_id": "9301",
        "reference_hash": "hash-301",
        "status": "match",
        "source": "allegro_api",
    }
    replace_channel_description_checks(app_module.DB_PATH, [existing])

    with pytest.raises(ValueError, match="żadnych ofert"):
        replace_channel_description_checks(app_module.DB_PATH, [])

    assert get_channel_description_checks(app_module.DB_PATH)[0]["status"] == "match"


def test_single_description_check_can_be_rechecked_without_replacing_snapshot(app_module):
    upsert_product_from_apilo(app_module.DB_PATH, _product(401))
    replace_apilo_description_references(
        app_module.DB_PATH,
        [_reference(401, "hash-401")],
        source_name="complete.xlsx",
    )
    original = {
        "apilo_product_id": 401,
        "channel_key": "erli",
        "external_id": "9401",
        "reference_hash": "hash-401",
        "status": "mismatch",
        "source": "public_page",
        "actual_description_text": "Stary opis ERLI",
    }
    replace_channel_description_checks(app_module.DB_PATH, [original])

    upsert_channel_description_check(
        app_module.DB_PATH,
        {
            **original,
            "status": "match",
            "actual_description_text": "Pełny poprawny opis ERLI",
            "palette_status": "mismatch",
            "palette_material": "PETG",
            "palette_block_text": "Niepoprawny blok PETG",
            "palette_block_hash": "palette-hash-wrong",
        },
        checked_at="2026-07-26T20:00:00+00:00",
    )

    result = get_channel_description_checks(app_module.DB_PATH)
    assert result[0]["status"] == "match"
    assert result[0]["actual_description_text"] == "Pełny poprawny opis ERLI"
    assert result[0]["palette_status"] == "mismatch"
    assert result[0]["palette_material"] == "PETG"
    assert result[0]["checked_at"] == "2026-07-26T20:00:00+00:00"


def test_description_check_migration_preserves_existing_rows(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE channel_description_checks (
            apilo_product_id INTEGER NOT NULL,
            channel_key TEXT NOT NULL,
            external_id TEXT NOT NULL,
            reference_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            source TEXT NOT NULL,
            detail TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (apilo_product_id, channel_key, external_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO channel_description_checks
        VALUES (501, 'erli', '9501', 'hash-501', 'mismatch',
                'public_page', '', '2026-07-26T20:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()

    init_db(db_path)

    result = get_channel_description_checks(db_path)
    assert len(result) == 1
    assert result[0]["status"] == "mismatch"
    assert result[0]["actual_description_text"] == ""
    assert result[0]["palette_status"] == "unverified"
    assert result[0]["palette_material"] == ""
    assert result[0]["palette_block_text"] == ""
    assert result[0]["palette_block_hash"] == ""
