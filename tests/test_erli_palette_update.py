import copy

import pytest
import requests

from erli_palette_update import (
    ErliPaletteUpdateError,
    ErliPaletteUpdater,
    ErliPaletteUnverifiedError,
    erli_write_configured,
    updater_from_env,
)
from material_palette_checks import canonical_material_palette_html


LEGACY_DESCRIPTION = {
    "sections": [
        {
            "items": [
                {"type": "TEXT", "content": "<p>Opis główny. Materiał: PLA</p>"},
                {"type": "IMAGE", "url": "https://example.com/main.jpg"},
            ]
        },
        {
            "items": [
                {"type": "IMAGE", "url": "https://example.com/palette.jpg"},
                {
                    "type": "TEXT",
                    "content": (
                        "<p>Nasze wydruki z materiału PLA łączą wyjątkową estetykę "
                        "z ekologicznym podejściem i odpornością na ścieranie.</p>"
                        "<p>Dlaczego warto wybrać PLA?</p>"
                        "<p>Nasza bogata gama kolorów PLA:</p>"
                        "<ul><li>Czarny</li><li>Biały</li></ul>"
                    ),
                },
            ]
        },
        {
            "items": [
                {"type": "TEXT", "content": "<p>Odkryj fascynujący świat druku 3D.</p>"},
                {"type": "IMAGE", "url": "https://example.com/company.jpg"},
            ]
        },
    ]
}


def product(description, *, frozen=False):
    return {
        "externalId": "56789012",
        "marketplaceId": 23456789013,
        "ean": "5900000001111",
        "name": "Testowa podstawka na gry",
        "description": copy.deepcopy(description),
        "frozen": {"description": frozen},
    }


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return copy.deepcopy(self._payload)


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def updater(session, **kwargs):
    return ErliPaletteUpdater(
        api_token="secret-token",
        api_base_url="https://erli.pl/svc/shop-api",
        session=session,
        poll_attempts=kwargs.get("poll_attempts", 20),
        poll_interval=0,
        sleep=lambda _seconds: None,
    )


def test_updates_only_description_once_and_polls_until_exact_readback():
    expected = copy.deepcopy(LEGACY_DESCRIPTION)
    expected["sections"][1]["items"][1]["content"] = canonical_material_palette_html(
        "PLA"
    )
    session = FakeSession(
        [
            FakeResponse(200, [product(LEGACY_DESCRIPTION)]),
            FakeResponse(202, {"updatedFields": ["description"]}),
            FakeResponse(200, product(LEGACY_DESCRIPTION)),
            FakeResponse(200, product(expected)),
        ]
    )

    result = updater(session).update_material_palette(
        23456789013, "PLA", expected_ean="5900000001111"
    )

    assert result["outcome"] == "replaced"
    assert result["external_id"] == "56789012"
    assert result["marketplace_id"] == 23456789013
    assert result["verified_after_error"] is False
    assert [call[0] for call in session.calls] == ["POST", "PATCH", "GET", "GET"]
    patch_call = session.calls[1]
    assert patch_call[1].endswith("/products/56789012")
    assert patch_call[2]["json"] == {"description": expected}
    assert patch_call[2]["allow_redirects"] is False


def test_timeout_is_not_retried_and_matching_readback_confirms_success():
    expected = copy.deepcopy(LEGACY_DESCRIPTION)
    expected["sections"][1]["items"][1]["content"] = canonical_material_palette_html(
        "PLA"
    )
    session = FakeSession(
        [
            FakeResponse(200, [product(LEGACY_DESCRIPTION)]),
            requests.Timeout("timeout"),
            FakeResponse(200, product(expected)),
        ]
    )

    result = updater(session).update_material_palette(
        23456789013, "PLA", expected_ean="5900000001111"
    )

    assert result["verified_after_error"] is True
    assert [call[0] for call in session.calls].count("PATCH") == 1


def test_server_error_is_not_retried_and_matching_readback_confirms_success():
    expected = copy.deepcopy(LEGACY_DESCRIPTION)
    expected["sections"][1]["items"][1]["content"] = canonical_material_palette_html(
        "PLA"
    )
    session = FakeSession(
        [
            FakeResponse(200, [product(LEGACY_DESCRIPTION)]),
            FakeResponse(503, {"error": "temporary"}),
            FakeResponse(200, product(expected)),
        ]
    )

    result = updater(session).update_material_palette(
        23456789013, "PLA", expected_ean="5900000001111"
    )

    assert result["verified_after_error"] is True
    assert [call[0] for call in session.calls].count("PATCH") == 1


def test_async_result_that_never_matches_is_unverified():
    session = FakeSession(
        [
            FakeResponse(200, [product(LEGACY_DESCRIPTION)]),
            FakeResponse(202, {"updatedFields": ["description"]}),
            FakeResponse(200, product(LEGACY_DESCRIPTION)),
            FakeResponse(200, product(LEGACY_DESCRIPTION)),
        ]
    )

    with pytest.raises(ErliPaletteUnverifiedError):
        updater(session, poll_attempts=2).update_material_palette(
            23456789013, "PLA", expected_ean="5900000001111"
        )

    assert [call[0] for call in session.calls].count("PATCH") == 1


def test_delayed_async_success_after_ten_readbacks_is_confirmed():
    expected = copy.deepcopy(LEGACY_DESCRIPTION)
    expected["sections"][1]["items"][1]["content"] = canonical_material_palette_html(
        "PLA"
    )
    session = FakeSession(
        [
            FakeResponse(200, [product(LEGACY_DESCRIPTION)]),
            FakeResponse(202, {"updatedFields": ["description"]}),
            *[FakeResponse(200, product(LEGACY_DESCRIPTION)) for _ in range(11)],
            FakeResponse(200, product(expected)),
        ]
    )

    result = updater(session, poll_attempts=12).update_material_palette(
        23456789013, "PLA", expected_ean="5900000001111"
    )

    assert result["outcome"] == "replaced"
    assert [call[0] for call in session.calls].count("PATCH") == 1
    assert [call[0] for call in session.calls].count("GET") == 12


def test_frozen_description_is_rejected_before_patch():
    session = FakeSession([FakeResponse(200, [product(LEGACY_DESCRIPTION, frozen=True)])])

    with pytest.raises(ErliPaletteUpdateError, match="zamrożony"):
        updater(session).update_material_palette(
            23456789013, "PLA", expected_ean="5900000001111"
        )

    assert [call[0] for call in session.calls] == ["POST"]


def test_duplicate_or_wrong_product_relation_is_rejected():
    duplicate = product(LEGACY_DESCRIPTION)
    duplicate["externalId"] = "other"
    session = FakeSession([FakeResponse(200, [product(LEGACY_DESCRIPTION), duplicate])])

    with pytest.raises(ErliPaletteUpdateError, match="jednoznacznego"):
        updater(session).update_material_palette(
            23456789013, "PLA", expected_ean="5900000001111"
        )

    wrong_ean = product(LEGACY_DESCRIPTION)
    wrong_ean["ean"] = "5900000000000"
    session = FakeSession([FakeResponse(200, [wrong_ean])])
    with pytest.raises(ErliPaletteUpdateError, match="EAN"):
        updater(session).update_material_palette(
            23456789013, "PLA", expected_ean="5900000001111"
        )


def test_private_env_file_is_required_and_foreign_base_is_rejected(tmp_path):
    env_file = tmp_path / "erli.env"
    env_file.write_text("ERLI_API_TOKEN=test-token\n")
    env_file.chmod(0o600)

    assert erli_write_configured(env_file) is True
    configured = updater_from_env(env_file, session=FakeSession([]))
    assert configured.api_base_url == "https://erli.pl/svc/shop-api"

    env_file.chmod(0o644)
    assert erli_write_configured(env_file) is False
    with pytest.raises(ErliPaletteUpdateError, match="uprawnienia"):
        updater_from_env(env_file)

    env_file.chmod(0o600)
    env_file.write_text(
        "ERLI_API_TOKEN=test-token\n"
        "ERLI_API_BASE=https://example.com/svc/shop-api\n"
    )
    with pytest.raises(ErliPaletteUpdateError, match="Adres"):
        updater_from_env(env_file)


def test_symlink_and_api_redirect_are_rejected(tmp_path):
    source = tmp_path / "source.env"
    source.write_text("ERLI_API_TOKEN=test-token\n")
    source.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(source)
    assert erli_write_configured(link) is False

    session = FakeSession([FakeResponse(302, {})])
    with pytest.raises(ErliPaletteUpdateError, match="przekierowanie"):
        updater(session).update_material_palette(
            23456789013, "PLA", expected_ean="5900000001111"
        )
    assert len(session.calls) == 1
    assert session.calls[0][2]["allow_redirects"] is False
