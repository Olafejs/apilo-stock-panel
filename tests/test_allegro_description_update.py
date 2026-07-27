import copy
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from allegro_description_update import (
    AllegroDescriptionUnverifiedError,
    AllegroDescriptionUpdateError,
    AllegroDescriptionUpdater,
    AllegroFileCredentialStore,
    build_catalog_aligned_product_set,
    replace_material_palette_section,
    replace_primary_text_item,
    sanitize_allegro_text_html,
)
from material_palette_checks import (
    analyze_material_palette_block,
    canonical_material_palette_html,
)


REFERENCE_HTML = """
<p class="p1">Adapter <span class="s1"><strong>3M</strong></span> do filtra.</p></br>
<p class="p3"><strong>Cechy produktu</strong><strong></strong></p></br>
<ul></br><li></br><p><strong>Materiał:</strong> PETG.</p></br></li></br></ul>
"""
REFERENCE_TEXT = "adapter 3m do filtra cechy produktu materiał petg"
OLD_DESCRIPTION = {
    "sections": [
        {
            "items": [
                {"type": "TEXT", "content": "<p>Stary opis.</p>"},
                {"type": "IMAGE", "url": "https://a.allegroimg.com/original/example"},
            ]
        },
        {
            "items": [
                {"type": "TEXT", "content": "<p>Paleta kolorów pozostaje bez zmian.</p>"}
            ]
        },
    ]
}

LEGACY_PALETTE_DESCRIPTION = {
    "sections": [
        {
            "items": [
                {"type": "TEXT", "content": "<p>Opis główny PETG.</p>"},
                {"type": "IMAGE", "url": "https://example/main.jpg"},
            ]
        },
        {
            "items": [
                {"type": "IMAGE", "url": "https://example/palette.jpg"},
                {
                    "type": "TEXT",
                    "content": (
                        "<p><b>KOLORY PETG (Odporność do 85°C):</b></p>"
                        "<ol><li>Czarny</li><li>Biały</li></ol>"
                    ),
                },
            ]
        },
        {
            "items": [
                {
                    "type": "TEXT",
                    "content": "<p>Odkryj fascynujący świat druku 3D z nami!</p>",
                },
                {"type": "IMAGE", "url": "https://example/company.jpg"},
            ]
        },
    ]
}


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return copy.deepcopy(self._payload)


class FakeSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def offer_with_description(description):
    return {"id": "1234567890", "description": copy.deepcopy(description)}


def test_sanitize_apilo_html_for_allegro_without_changing_text():
    content = sanitize_allegro_text_html(REFERENCE_HTML)

    assert content == (
        "<p>Adapter <b>3M</b> do filtra.</p>"
        "<p><b>Cechy produktu</b></p>"
        "<ul><li><b>Materiał:</b> PETG.</li></ul>"
    )
    assert "class=" not in content
    assert "<span" not in content
    assert "strong" not in content
    assert "br" not in content


def test_replace_primary_text_preserves_images_and_later_sections():
    source = copy.deepcopy(OLD_DESCRIPTION)
    updated = replace_primary_text_item(source, "<p>Nowy opis.</p>")

    assert updated["sections"][0]["items"] == [
        {"type": "TEXT", "content": "<p>Nowy opis.</p>"},
        {"type": "IMAGE", "url": "https://a.allegroimg.com/original/example"},
    ]
    assert updated["sections"][1] == source["sections"][1]
    assert source["sections"][0]["items"][0]["content"] == "<p>Stary opis.</p>"


def test_updater_refreshes_token_writes_once_and_verifies_readback(monkeypatch):
    sanitized = sanitize_allegro_text_html(REFERENCE_HTML)
    updated_description = replace_primary_text_item(OLD_DESCRIPTION, sanitized)
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                },
            ),
            FakeResponse(200, offer_with_description(OLD_DESCRIPTION)),
            FakeResponse(200, {}),
            FakeResponse(200, offer_with_description(updated_description)),
        ]
    )
    saved = []
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        save_token_payload=saved.append,
        session=session,
    )
    monkeypatch.setattr(
        "allegro_description_update.description_matches",
        lambda expected, actual: "adapter 3m do filtra" in actual.casefold(),
    )

    result = updater.update_primary_description(
        "1234567890", REFERENCE_HTML, REFERENCE_TEXT
    )

    assert result["outcome"] == "updated"
    assert result["access_token"] == "new-access-token"
    assert len(saved) == 1
    assert saved[0]["access_token"] == "new-access-token"
    assert saved[0]["refresh_token"] == "new-refresh-token"
    assert [call[0] for call in session.calls] == ["POST", "GET", "PATCH", "GET"]
    patch_call = session.calls[2]
    assert patch_call[2]["json"]["description"] == updated_description
    assert patch_call[2]["allow_redirects"] is False
    assert "client-secret" not in repr(session.calls)


def test_updater_does_not_patch_when_offer_already_matches(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "token", "refresh_token": "refresh"}),
            FakeResponse(200, offer_with_description(OLD_DESCRIPTION)),
        ]
    )
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        session=session,
    )
    monkeypatch.setattr(
        "allegro_description_update.description_matches", lambda expected, actual: True
    )

    result = updater.update_primary_description(
        "1234567890", REFERENCE_HTML, REFERENCE_TEXT
    )

    assert result["outcome"] == "unchanged"
    assert [call[0] for call in session.calls] == ["POST", "GET"]


def test_timeout_after_patch_is_verified_without_retry(monkeypatch):
    sanitized = sanitize_allegro_text_html(REFERENCE_HTML)
    updated_description = replace_primary_text_item(OLD_DESCRIPTION, sanitized)
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "token", "refresh_token": "refresh"}),
            FakeResponse(200, offer_with_description(OLD_DESCRIPTION)),
            requests.Timeout("ambiguous"),
            FakeResponse(200, offer_with_description(updated_description)),
        ]
    )
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        session=session,
    )
    matches = iter([True, False, True])
    monkeypatch.setattr(
        "allegro_description_update.description_matches",
        lambda expected, actual: next(matches),
    )

    result = updater.update_primary_description(
        "1234567890", REFERENCE_HTML, REFERENCE_TEXT
    )

    assert result["outcome"] == "verified_after_error"
    assert [call[0] for call in session.calls].count("PATCH") == 1


def test_timeout_with_unconfirmed_readback_fails_without_retry(monkeypatch):
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "token", "refresh_token": "refresh"}),
            FakeResponse(200, offer_with_description(OLD_DESCRIPTION)),
            requests.Timeout("ambiguous"),
            FakeResponse(200, offer_with_description(OLD_DESCRIPTION)),
        ]
    )
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        session=session,
    )
    matches = iter([True, False, False])
    monkeypatch.setattr(
        "allegro_description_update.description_matches",
        lambda expected, actual: next(matches),
    )

    with pytest.raises(AllegroDescriptionUnverifiedError):
        updater.update_primary_description(
            "1234567890", REFERENCE_HTML, REFERENCE_TEXT
        )

    assert [call[0] for call in session.calls].count("PATCH") == 1


def test_file_store_atomically_updates_the_shared_token_store(tmp_path):
    os.chmod(tmp_path, 0o700)
    env_path = tmp_path / "allegro.env"
    token_path = tmp_path / "tokens.json"
    env_path.write_text(
        "ALLEGRO_CLIENT_ID=client-id\nALLEGRO_CLIENT_SECRET=client-secret\n"
    )
    token_path.write_text(
        json.dumps(
            {
                "refresh_token": "old-refresh",
                "scope": "scope",
                "extra": {"refresh_token": "stale-extra-secret"},
            }
        )
    )
    os.chmod(env_path, 0o600)
    os.chmod(token_path, 0o600)
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                },
            )
        ]
    )
    store = AllegroFileCredentialStore(
        env_path=env_path, token_path=token_path, session=session
    )

    assert store.is_configured() is True
    with store.locked_updater() as updater:
        assert updater.refresh_access_token() == "new-access"

    saved = json.loads(token_path.read_text())
    assert saved["access_token"] == "new-access"
    assert saved["refresh_token"] == "new-refresh"
    assert saved["expires_at"] > 0
    assert "extra" not in saved
    assert "old-refresh" not in token_path.read_text()
    assert "stale-extra-secret" not in token_path.read_text()
    assert token_path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".tokens-*.tmp"))


def test_file_store_rejects_world_readable_token_file(tmp_path):
    os.chmod(tmp_path, 0o700)
    env_path = tmp_path / "allegro.env"
    token_path = tmp_path / "tokens.json"
    env_path.write_text(
        "ALLEGRO_CLIENT_ID=client-id\nALLEGRO_CLIENT_SECRET=client-secret\n"
    )
    token_path.write_text(json.dumps({"refresh_token": "refresh"}))
    os.chmod(env_path, 0o600)
    os.chmod(token_path, 0o644)
    store = AllegroFileCredentialStore(env_path=env_path, token_path=token_path)

    assert store.is_configured() is False
    with pytest.raises(AllegroDescriptionUpdateError):
        with store.locked_updater():
            pass


def test_updater_does_not_follow_cross_host_redirects():
    sink_hits = []

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            sink_hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            return

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{sink.server_port}/internal"
            )
            self.end_headers()

        def log_message(self, format, *args):
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (sink, source)
    ]
    for thread in threads:
        thread.start()
    try:
        updater = AllegroDescriptionUpdater(
            client_id="client-id",
            client_secret="client-secret",
            refresh_token="refresh-token",
            api_base_url=f"http://127.0.0.1:{source.server_port}",
        )
        with pytest.raises(AllegroDescriptionUpdateError):
            updater.get_offer("1234567890", "access-token")
        assert sink_hits == []
    finally:
        source.shutdown()
        sink.shutdown()
        source.server_close()
        sink.server_close()


def test_replace_legacy_palette_preserves_primary_images_and_company_section():
    source = copy.deepcopy(LEGACY_PALETTE_DESCRIPTION)

    updated, outcome = replace_material_palette_section(source, "PETG")

    assert outcome == "replaced"
    assert updated["sections"][0] == source["sections"][0]
    assert updated["sections"][2] == source["sections"][2]
    assert updated["sections"][1]["items"][0] == {
        "type": "IMAGE",
        "url": "https://example/palette.jpg",
    }
    palette_html = updated["sections"][1]["items"][1]["content"]
    assert palette_html == canonical_material_palette_html("PETG")
    assert analyze_material_palette_block(
        palette_html, require_structure=True
    )["status"] == "match"
    assert source == LEGACY_PALETTE_DESCRIPTION


def test_insert_palette_before_company_when_it_is_missing():
    source = {
        "sections": [
            copy.deepcopy(LEGACY_PALETTE_DESCRIPTION["sections"][0]),
            copy.deepcopy(LEGACY_PALETTE_DESCRIPTION["sections"][2]),
        ]
    }

    updated, outcome = replace_material_palette_section(source, "PLA")

    assert outcome == "inserted"
    assert len(updated["sections"]) == 3
    assert updated["sections"][0] == source["sections"][0]
    assert updated["sections"][2] == source["sections"][1]
    palette_html = updated["sections"][1]["items"][0]["content"]
    analysis = analyze_material_palette_block(palette_html, require_structure=True)
    assert analysis["status"] == "match"
    assert analysis["material"] == "PLA"


def test_reject_multiple_palette_sections_in_one_offer():
    source = copy.deepcopy(LEGACY_PALETTE_DESCRIPTION)
    source["sections"].insert(2, copy.deepcopy(source["sections"][1]))

    with pytest.raises(AllegroDescriptionUpdateError):
        replace_material_palette_section(source, "PETG")


def test_palette_updater_writes_once_and_verifies_readback():
    updated_description, _ = replace_material_palette_section(
        LEGACY_PALETTE_DESCRIPTION, "PETG"
    )
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "token", "refresh_token": "refresh"}),
            FakeResponse(200, offer_with_description(LEGACY_PALETTE_DESCRIPTION)),
            FakeResponse(200, {}),
            FakeResponse(200, offer_with_description(updated_description)),
        ]
    )
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        session=session,
    )

    result = updater.update_material_palette("1234567890", "PETG")

    assert result["outcome"] == "replaced"
    assert [call[0] for call in session.calls] == ["POST", "GET", "PATCH", "GET"]
    assert session.calls[2][2]["json"]["description"] == updated_description


def test_catalog_alignment_uses_authoritative_product_parameters_only():
    offer = {
        "productSet": [
            {
                "product": {
                    "id": "ecd17145-1df8-445c-a005-9e5f81452c18",
                    "parameters": [
                        {"id": "224017", "values": ["stary kod"]},
                        {"id": "237206", "values": ["stary model"]},
                        {
                            "id": "248811",
                            "values": ["Example Company"],
                            "valuesIds": ["248811_1645181"],
                        },
                    ],
                },
                "quantity": {"value": 1},
                "responsibleProducer": {"id": "producer-id"},
            }
        ]
    }
    catalog = {
        "id": "ecd17145-1df8-445c-a005-9e5f81452c18",
        "parameters": [
            {"id": "224017", "values": ["5900000001096"]},
            {"id": "237206", "values": ["brak"]},
            {"id": "248811", "valuesIds": ["248811_1645181"]},
            {"id": "250792", "values": ["39269097"]},
        ],
    }

    aligned, changed_ids = build_catalog_aligned_product_set(offer, catalog)

    assert aligned is not None
    assert changed_ids == ["224017", "237206", "250792"]
    assert aligned[0]["product"]["parameters"] == [
        {"id": "224017", "values": ["5900000001096"]},
        {"id": "237206", "values": ["brak"]},
        {"id": "248811", "valuesIds": ["248811_1645181"]},
        {"id": "250792", "values": ["39269097"]},
    ]
    assert aligned[0]["responsibleProducer"] == {"id": "producer-id"}
    assert aligned[0]["quantity"] == {"value": 1}


def test_palette_updater_aligns_catalog_parameters_in_the_same_patch():
    product_id = "ecd17145-1df8-445c-a005-9e5f81452c18"
    before = offer_with_description(LEGACY_PALETTE_DESCRIPTION)
    before["productSet"] = [
        {
            "product": {
                "id": product_id,
                "parameters": [{"id": "237206", "values": ["stary model"]}],
            },
            "quantity": {"value": 1},
        }
    ]
    catalog = {
        "id": product_id,
        "parameters": [{"id": "237206", "values": ["brak"]}],
    }
    expected_description, _ = replace_material_palette_section(
        LEGACY_PALETTE_DESCRIPTION, "PETG"
    )
    after = offer_with_description(expected_description)
    after["productSet"] = [
        {
            "product": {
                "id": product_id,
                "parameters": [{"id": "237206", "values": ["brak"]}],
            },
            "quantity": {"value": 1},
        }
    ]
    session = FakeSession(
        [
            FakeResponse(200, {"access_token": "token", "refresh_token": "refresh"}),
            FakeResponse(200, before),
            FakeResponse(200, catalog),
            FakeResponse(200, {}),
            FakeResponse(200, after),
        ]
    )
    updater = AllegroDescriptionUpdater(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-token",
        session=session,
    )

    result = updater.update_material_palette("1234567890", "PETG")

    assert result["catalog_parameter_ids"] == ["237206"]
    assert [call[0] for call in session.calls] == ["POST", "GET", "GET", "PATCH", "GET"]
    patch_payload = session.calls[3][2]["json"]
    assert patch_payload["description"] == expected_description
    assert patch_payload["productSet"][0]["product"]["parameters"] == [
        {"id": "237206", "values": ["brak"]}
    ]
