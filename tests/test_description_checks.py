from openpyxl import Workbook
import pytest

from description_checks import (
    DescriptionExportError,
    description_preview,
    description_matches,
    load_apilo_description_export,
    normalize_description,
)


def _write_export(path, rows):
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Apilo.com"
    sheet.append(
        [
            "id",
            "name",
            "ean",
            "sku",
            "weight",
            "descriptionShort",
            "description",
            "price",
            "priceBuyingNetto",
            "quantity",
            "status",
            "category",
        ]
    )
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def test_apilo_xlsx_export_loads_reference_price_and_quantity(tmp_path):
    path = tmp_path / "export.xlsx"
    _write_export(
        path,
        [
            [
                101,
                "Uchwyt",
                "5900000000101",
                "SKU-101",
                0.2,
                "Krótki",
                "<p>Pełny opis produktu.</p>",
                39.99,
                20.0,
                7,
                1,
                "Akcesoria",
            ]
        ],
    )

    records = load_apilo_description_export(path)

    assert records[0]["apilo_product_id"] == 101
    assert records[0]["description_text"] == "pełny opis produktu"
    assert records[0]["description_html"] == "<p>Pełny opis produktu.</p>"
    assert records[0]["description_preview"] == "Pełny opis produktu."
    assert records[0]["export_price"] == 39.99
    assert records[0]["export_quantity"] == 7
    assert len(records[0]["description_hash"]) == 64


def test_apilo_xlsx_export_rejects_duplicate_product_ids(tmp_path):
    path = tmp_path / "export.xlsx"
    row = [101, "Uchwyt", "5901", "SKU", 0.2, "", "Opis", 1, 1, 1, 1, "A"]
    _write_export(path, [row, row])

    try:
        load_apilo_description_export(path)
    except DescriptionExportError as exc:
        assert "Powielone" in str(exc)
    else:
        raise AssertionError("Duplikat ID powinien zostać odrzucony")


def test_description_match_ignores_html_case_spacing_and_punctuation():
    reference = "<p>Pełny opis – produktu!</p>"
    marketplace = "Nawigacja PEŁNY   OPIS - produktu. Stopka"

    assert normalize_description(reference) == "pełny opis produktu"
    assert description_matches(reference, marketplace) is True
    assert description_matches(reference, "Inny opis") is False


def test_description_preview_preserves_readable_blocks_and_ignores_scripts():
    source = "<h2>Tytuł</h2><p>Pierwszy<br>drugi</p><ul><li>Raz</li><li>Dwa</li></ul><script>alert(1)</script>"

    preview = description_preview(source)

    assert preview == "Tytuł\n\nPierwszy\ndrugi\n\n• Raz\n\n• Dwa"
    assert "alert" not in preview


def test_apilo_xlsx_export_rejects_non_xlsx_payload(tmp_path):
    path = tmp_path / "fake.xlsx"
    path.write_bytes(b"not-an-xlsx")

    with pytest.raises(DescriptionExportError, match="Nieprawidłowy plik XLSX"):
        load_apilo_description_export(path)
