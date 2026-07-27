from material_palette_checks import (
    analyze_material_palette_block,
    canonical_material_palette_html,
    canonical_material_palette_text,
)


def test_canonical_pla_block_is_recognized_from_marketplace_html():
    canonical = canonical_material_palette_text("PLA")
    html = (
        "<section><p><b>"
        + canonical.replace("Nasza bogata gama kolorów PLA:", "Nasza bogata gama kolorów <b>PLA</b>:</p><ol><li>")
        .replace(" Czarny Srebrny ", " Czarny</li><li>Srebrny ")
        .replace(" Uwaga:", "</li></ol><p><b>Uwaga:</b>")
        + "</p></section>"
    )

    result = analyze_material_palette_block(html)

    assert result["status"] == "match"
    assert result["material"] == "PLA"
    assert result["text"]
    assert result["block_hash"]


def test_palette_color_order_change_is_a_mismatch():
    changed = canonical_material_palette_text("PETG").replace(
        "Czarny Biały", "Biały Czarny", 1
    )

    result = analyze_material_palette_block(changed)

    assert result["status"] == "mismatch"
    assert result["material"] == "PETG"
    assert result["text"] == changed


def test_palette_punctuation_and_letter_case_are_strict():
    canonical = canonical_material_palette_text("PLA")

    assert analyze_material_palette_block(
        canonical.replace("Uwaga:", "UWAGA:", 1)
    )["status"] == "mismatch"
    assert analyze_material_palette_block(
        canonical.replace("realizację.", "realizację!", 1)
    )["status"] == "mismatch"


def test_palette_structure_requires_paragraphs_and_two_lists():
    structured = canonical_material_palette_html("PETG")

    assert analyze_material_palette_block(
        structured, require_structure=True
    )["status"] == "match"
    assert analyze_material_palette_block(
        canonical_material_palette_text("PETG"), require_structure=True
    )["status"] == "mismatch"
    assert analyze_material_palette_block(
        structured.replace("<ol>", "<div>").replace("</ol>", "</div>"),
        require_structure=True,
    )["status"] == "mismatch"


def test_incomplete_material_block_is_preserved_as_mismatch():
    incomplete = (
        "Początek. Nasze wydruki z materiału PLA łączą wyjątkową estetykę. "
        "Nasza bogata gama kolorów PLA: Czarny Biały. Odkryj fascynujący świat"
    )

    result = analyze_material_palette_block(incomplete)

    assert result["status"] == "mismatch"
    assert result["material"] == "PLA"
    assert "Nasze wydruki" in result["text"]


def test_description_without_palette_is_not_applicable():
    result = analyze_material_palette_block(
        "Opis produktu. Materiał: FLEX. Kolor: czarny. Zapraszam do zakupu!"
    )

    assert result == {
        "status": "absent",
        "material": "",
        "text": "",
        "block_hash": "",
        "expected_text": "",
    }


def test_only_supported_choice_palette_materials_have_canonical_templates():
    assert canonical_material_palette_text("PLA").startswith(
        "Nasze wydruki z materiału PLA"
    )
    assert canonical_material_palette_text("PET-G").startswith(
        "Nasze wydruki z materiału PETG"
    )
    assert canonical_material_palette_text("FLEX") == ""
