import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from allegro_description_update import (
    AllegroDescriptionUpdateError,
    material_palette_section_matches,
    replace_material_palette_section,
)


DEFAULT_ERLI_API_BASE = "https://erli.pl/svc/shop-api"
ERLI_USER_AGENT = "Apilo-Panel/1.14"
MARKETPLACE_ID_RE = re.compile(r"^[1-9][0-9]{0,18}$")
MAX_ENV_BYTES = 65_536


class ErliPaletteUpdateError(RuntimeError):
    pass


class ErliPaletteUnverifiedError(ErliPaletteUpdateError):
    pass


def _validate_api_base(value):
    base = str(value or DEFAULT_ERLI_API_BASE).strip().rstrip("/")
    parsed = urlparse(base)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "erli.pl"
        or parsed.port not in {None, 443}
        or parsed.path != "/svc/shop-api"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ErliPaletteUpdateError("Adres API ERLI jest nieprawidłowy.")
    return base


def _load_private_env(path):
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ErliPaletteUpdateError("Brak bezpiecznej konfiguracji ERLI.") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ErliPaletteUpdateError(
                "Plik konfiguracji ERLI jest nieprawidłowy."
            )
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ErliPaletteUpdateError(
                "Plik konfiguracji ERLI ma niebezpieczne uprawnienia."
            )
        if info.st_size > MAX_ENV_BYTES:
            raise ErliPaletteUpdateError("Plik konfiguracji ERLI jest zbyt duży.")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = None
            content = stream.read(MAX_ENV_BYTES + 1)
    except OSError as exc:
        raise ErliPaletteUpdateError(
            "Nie udało się odczytać konfiguracji ERLI."
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    values = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    token = values.get("ERLI_API_TOKEN") or values.get("ERLI_TOKEN")
    if not token or len(token) > 16_384:
        raise ErliPaletteUpdateError("Brak tokenu API ERLI.")
    return {
        "api_token": token,
        "api_base_url": _validate_api_base(values.get("ERLI_API_BASE")),
    }


def erli_write_configured(env_path):
    try:
        _load_private_env(env_path)
        return True
    except ErliPaletteUpdateError:
        return False


def updater_from_env(env_path, **kwargs):
    config = _load_private_env(env_path)
    return ErliPaletteUpdater(**config, **kwargs)


class ErliPaletteUpdater:
    def __init__(
        self,
        *,
        api_token,
        api_base_url=DEFAULT_ERLI_API_BASE,
        session=None,
        timeout=60,
        poll_attempts=20,
        poll_interval=2,
        sleep=None,
    ):
        if not str(api_token or "").strip():
            raise ErliPaletteUpdateError("Brak tokenu API ERLI.")
        self.api_token = str(api_token).strip()
        self.api_base_url = _validate_api_base(api_base_url)
        self.session = session or requests.Session()
        self.timeout = max(1, min(int(timeout), 120))
        self.poll_attempts = max(1, min(int(poll_attempts), 30))
        self.poll_interval = max(0, min(float(poll_interval), 10))
        self.sleep = sleep or time.sleep

    def _request(self, method, path, *, json_body=None, ambiguous_write=False):
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": ERLI_USER_AGENT,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self.session.request(
                method,
                self.api_base_url + path,
                headers=headers,
                json=json_body,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            error_class = (
                ErliPaletteUnverifiedError
                if ambiguous_write
                else ErliPaletteUpdateError
            )
            raise error_class("Nie udało się połączyć z API ERLI.") from exc

    @staticmethod
    def _json_response(response, expected_statuses):
        if response.status_code not in expected_statuses:
            if 300 <= response.status_code < 400:
                raise ErliPaletteUpdateError(
                    "API ERLI zwróciło niedozwolone przekierowanie."
                )
            raise ErliPaletteUpdateError(
                f"API ERLI odrzuciło żądanie (HTTP {response.status_code})."
            )
        try:
            return response.json()
        except (TypeError, ValueError) as exc:
            raise ErliPaletteUpdateError("API ERLI zwróciło nieprawidłowe dane.") from exc

    @staticmethod
    def _marketplace_id(value):
        text = str(value or "").strip()
        if not MARKETPLACE_ID_RE.fullmatch(text):
            raise ErliPaletteUpdateError("Nieprawidłowy identyfikator oferty ERLI.")
        return int(text)

    @staticmethod
    def _external_id(value):
        external_id = str(value or "").strip()
        if not external_id or len(external_id) > 1023:
            raise ErliPaletteUpdateError(
                "Nieprawidłowy identyfikator produktu ERLI."
            )
        return external_id

    def find_product(self, marketplace_id):
        marketplace_id = self._marketplace_id(marketplace_id)
        body = {
            "pagination": {
                "sortField": "marketplaceId",
                "order": "ASC",
                "limit": 2,
            },
            "filter": {
                "field": "marketplaceId",
                "operator": "=",
                "value": marketplace_id,
            },
            "fields": [
                "externalId",
                "marketplaceId",
                "name",
                "ean",
                "description",
                "frozen",
                "updated",
            ],
        }
        response = self._request("POST", "/products/_search", json_body=body)
        products = self._json_response(response, {200})
        if not isinstance(products, list) or len(products) != 1:
            raise ErliPaletteUpdateError(
                "Nie znaleziono jednoznacznego produktu ERLI."
            )
        product = products[0]
        try:
            returned_marketplace_id = int(product.get("marketplaceId") or 0)
            self._external_id(product.get("externalId"))
        except (AttributeError, TypeError, ValueError, ErliPaletteUpdateError) as exc:
            raise ErliPaletteUpdateError("API ERLI zwróciło inny produkt.") from exc
        if not isinstance(product, dict) or returned_marketplace_id != marketplace_id:
            raise ErliPaletteUpdateError("API ERLI zwróciło inny produkt.")
        return product

    def get_product(self, external_id):
        external_id = self._external_id(external_id)
        response = self._request("GET", "/products/" + quote(external_id, safe=""))
        product = self._json_response(response, {200})
        if not isinstance(product, dict) or str(product.get("externalId") or "") != external_id:
            raise ErliPaletteUpdateError("API ERLI zwróciło inny produkt.")
        return product

    def patch_description(self, external_id, description):
        external_id = self._external_id(external_id)
        response = self._request(
            "PATCH",
            "/products/" + quote(external_id, safe=""),
            json_body={"description": description},
            ambiguous_write=True,
        )
        if response.status_code >= 500:
            raise ErliPaletteUnverifiedError(
                "ERLI zwróciło błąd po wysłaniu opisu."
            )
        result = self._json_response(response, {202})
        if not isinstance(result, dict) or "description" not in (
            result.get("updatedFields") or []
        ):
            raise ErliPaletteUnverifiedError(
                "ERLI nie potwierdziło przyjęcia opisu do aktualizacji."
            )
        return result

    def update_material_palette(self, marketplace_id, material, *, expected_ean=""):
        marketplace_id = self._marketplace_id(marketplace_id)
        current_product = self.find_product(marketplace_id)
        external_id = self._external_id(current_product["externalId"])
        if expected_ean and str(current_product.get("ean") or "").strip() != str(
            expected_ean
        ).strip():
            raise ErliPaletteUpdateError("EAN produktu ERLI nie zgadza się z Apilo.")
        if bool((current_product.get("frozen") or {}).get("description")):
            raise ErliPaletteUpdateError(
                "Opis produktu ERLI jest zamrożony w panelu sprzedawcy."
            )
        current_description = current_product.get("description")
        try:
            updated_description, outcome = replace_material_palette_section(
                current_description, material
            )
        except AllegroDescriptionUpdateError as exc:
            message = str(exc).replace("Oferta Allegro", "Produkt ERLI")
            raise ErliPaletteUpdateError(message) from exc
        if outcome == "unchanged":
            return {
                "outcome": outcome,
                "verified_after_error": False,
                "marketplace_id": marketplace_id,
                "external_id": external_id,
                "product": current_product,
            }

        patch_was_ambiguous = False
        try:
            self.patch_description(external_id, updated_description)
        except ErliPaletteUnverifiedError:
            patch_was_ambiguous = True

        last_error = None
        for attempt in range(self.poll_attempts):
            if attempt:
                self.sleep(self.poll_interval)
            try:
                verified_product = self.get_product(external_id)
            except ErliPaletteUpdateError as exc:
                last_error = exc
                continue
            verified_description = verified_product.get("description")
            if (
                verified_description == updated_description
                and material_palette_section_matches(verified_description, material)
            ):
                return {
                    "outcome": outcome,
                    "verified_after_error": patch_was_ambiguous,
                    "marketplace_id": marketplace_id,
                    "external_id": external_id,
                    "product": verified_product,
                }
        raise ErliPaletteUnverifiedError(
            "ERLI nie potwierdziło bloku materiału i kolorów po zapisie."
        ) from last_error
