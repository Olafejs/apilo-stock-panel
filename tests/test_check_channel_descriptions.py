import pytest

from material_palette_checks import canonical_material_palette_html
from scripts import check_channel_descriptions as checker


TARGET = {
    "apilo_product_id": 101,
    "channel_key": "prestashop",
    "external_id": "212",
    "listing_name": "Uchwyt",
    "name": "Uchwyt",
    "ean": "5900000000101",
    "sku": "SKU-101",
}
REFERENCE = {
    "description_text": "pełny wzorcowy opis produktu",
    "description_hash": "hash-101",
}


def test_public_page_checker_matches_full_apilo_reference(monkeypatch):
    monkeypatch.setattr(
        checker,
        "_bounded_public_page",
        lambda url: (
            "<html><body><nav>Nawigacja</nav>"
            f"<div id='description' class='product-description'><p>{REFERENCE['description_text']}</p></div>"
            "<footer>Stopka</footer></body></html>"
        ),
    )

    result = checker._check_target(dict(TARGET), REFERENCE, None)

    assert result["status"] == "match"
    assert result["source"] == "public_page"


def test_allegro_checker_uses_official_description_payload(monkeypatch):
    target = {**TARGET, "channel_key": "allegro", "external_id": "9001"}
    palette = canonical_material_palette_html("PLA")
    monkeypatch.setattr(
        checker,
        "allegro_get_product_offer",
        lambda offer_id, token: {
            "description": {
                "sections": [
                    {"items": [{"type": "TEXT", "content": REFERENCE["description_text"]}]},
                    {"items": [{"type": "TEXT", "content": palette}]},
                    {"items": [{"type": "TEXT", "content": "O firmie Example Company"}]},
                ]
            }
        },
    )

    result = checker._check_target(target, REFERENCE, "token-testowy")

    assert result["status"] == "match"
    assert result["source"] == "allegro_api"
    assert result["actual_description_text"] == REFERENCE["description_text"]
    assert result["palette_status"] == "match"
    assert result["palette_material"] == "PLA"
    assert "Nasze wydruki z materiału PLA" in result["palette_block_text"]


def test_prestashop_checker_stores_product_description_for_diff(monkeypatch):
    monkeypatch.setattr(
        checker,
        "_bounded_public_page",
        lambda url: """
            <html><body>
              <div id="description" class="tab-pane product-description">
                <p>Pełny opis produktu w kolorze niebieskim.</p>
                <ul><li>Wysokość 12 cm</li></ul>
              </div>
            </body></html>
        """,
    )

    result = checker._check_target(dict(TARGET), REFERENCE, None)

    assert result["status"] == "mismatch"
    assert "kolorze niebieskim" in result["actual_description_text"]
    assert "Wysokość 12 cm" in result["actual_description_text"]


def test_erli_checker_prefers_visible_product_section_and_excludes_palette(monkeypatch):
    target = {**TARGET, "channel_key": "erli", "external_id": "erli-212"}
    palette = canonical_material_palette_html("PETG")
    monkeypatch.setattr(
        checker,
        "_bounded_public_page",
        lambda url: """
            <html><head>
              <script type="application/ld+json">
                {"@context":"https://schema.org","@type":"Product",
                 "description":"Błędny opis zapasowy JSON-LD."}
              </script>
            </head><body><section id="product-description">
              <p>""" + REFERENCE["description_text"] + """</p>
              """ + palette + """
              <p>O firmie Example Company</p>
            </section></body></html>
        """,
    )

    result = checker._check_target(target, REFERENCE, None)

    assert result["status"] == "match"
    assert result["actual_description_text"] == REFERENCE["description_text"]
    assert "Nasze wydruki" not in result["actual_description_text"]
    assert "O firmie" not in result["actual_description_text"]
    assert result["palette_status"] == "match"
    assert result["palette_material"] == "PETG"


def test_erli_checker_uses_json_ld_when_visible_section_is_unavailable(monkeypatch):
    target = {**TARGET, "channel_key": "erli", "external_id": "erli-212"}
    monkeypatch.setattr(
        checker,
        "_bounded_public_page",
        lambda url: """
            <html><head>
              <script type="application/ld+json">
                {"@context":"https://schema.org","@type":"Product",
                 "description":"Opis kanału ERLI z inną wysokością 15 cm."}
              </script>
            </head><body></body></html>
        """,
    )

    result = checker._check_target(target, REFERENCE, None)

    assert result["status"] == "mismatch"
    assert result["actual_description_text"] == "Opis kanału ERLI z inną wysokością 15 cm."
    assert result["palette_status"] == "absent"


def test_channel_without_description_api_is_explicitly_unavailable():
    target = {**TARGET, "channel_key": "empik", "external_id": "8001"}

    result = checker._check_target(target, REFERENCE, None)

    assert result["status"] == "unavailable"
    assert result["detail"] == "opis_niedostepny"
    assert result["actual_description_text"] == ""
    assert result["palette_status"] == "unavailable"


def test_public_page_redirect_to_untrusted_host_is_rejected_before_following(
    monkeypatch,
):
    calls = []

    class RedirectResponse:
        status_code = 302
        headers = {"Location": "http://127.0.0.1:8080/private"}
        url = "https://erli.pl/start"

        def close(self):
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return RedirectResponse()

    monkeypatch.setattr(checker.requests, "get", fake_get)
    monkeypatch.setattr(checker, "_throttle_public_host", lambda host: None)

    with pytest.raises(RuntimeError, match="Niedozwolony adres"):
        checker._bounded_public_page("https://erli.pl/start")

    assert len(calls) == 1
    assert calls[0][1]["allow_redirects"] is False
