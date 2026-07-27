from description_diffs import build_description_diff


def test_description_diff_marks_missing_and_added_words():
    result = build_description_diff(
        "Uchwyt czerwony wykonany z PLA.",
        "Uchwyt niebieski wykonany z PET-G.",
    )

    assert result["available"] is True
    assert result["changed"] is True
    assert result["missing_words"] == 2
    assert result["added_words"] == 2
    assert 0 < result["similarity_percent"] < 100
    missing = "".join(
        item["text"] for item in result["segments"] if item["kind"] == "missing"
    )
    added = "".join(
        item["text"] for item in result["segments"] if item["kind"] == "added"
    )
    assert "czerwony" in missing
    assert "PLA" in missing
    assert "niebieski" in added
    assert "PET-G" in added


def test_description_diff_ignores_case_and_whitespace_only_changes():
    result = build_description_diff(
        "Opis produktu.\nWysokość 10 cm.",
        "opis   produktu. Wysokość 10 cm.",
    )

    assert result["available"] is True
    assert result["changed"] is False
    assert result["similarity_percent"] == 100
    assert result["missing_words"] == 0
    assert result["added_words"] == 0


def test_description_diff_reports_missing_channel_text_without_fabricating_segments():
    result = build_description_diff("Pełny opis Apilo", "")

    assert result == {
        "available": False,
        "changed": False,
        "similarity_percent": None,
        "missing_words": 0,
        "added_words": 0,
        "truncated": False,
        "segments": [],
    }


def test_description_diff_bounds_very_long_values():
    result = build_description_diff("a " * 12_000, "b " * 12_000)

    assert result["available"] is True
    assert result["truncated"] is True
    assert len(result["segments"]) <= 8
