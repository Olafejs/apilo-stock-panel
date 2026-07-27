import base64
import copy
import html
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from html.parser import HTMLParser

import fcntl

import requests

from description_checks import description_matches
from material_palette_checks import (
    analyze_material_palette_block,
    canonical_material_palette_html,
    normalize_palette_material,
)
from product_attributes import description_primary_section_text


ALLEGRO_API_BASE_URL = "https://api.allegro.pl"
ALLEGRO_OAUTH_BASE_URL = "https://allegro.pl"
ALLEGRO_ACCEPT = "application/vnd.allegro.public.v1+json"
MAX_ALLEGRO_RESPONSE_BYTES = 2_000_000
MAX_ALLEGRO_DESCRIPTION_HTML_CHARS = 40_000
OFFER_ID_PATTERN = re.compile(r"^[0-9]{5,30}$")
LEGACY_PALETTE_PATTERN = re.compile(
    r"\bKOLORY\s+(?:PLA\s*\+?|PET\s*-?\s*G)\b", re.I
)
COMPANY_SECTION_PATTERN = re.compile(
    r"Odkryj\s+fascynujący\s+świat\s+druku\s+3D", re.I
)


class AllegroDescriptionUpdateError(RuntimeError):
    pass


class AllegroDescriptionUnverifiedError(AllegroDescriptionUpdateError):
    pass


class _AllegroHtmlSanitizer(HTMLParser):
    ALLOWED_TAGS = {"p", "h1", "h2", "ul", "ol", "li", "b"}
    TAG_ALIASES = {"strong": "b"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.list_item_depth = 0

    def handle_starttag(self, tag, attrs):
        normalized = self.TAG_ALIASES.get(tag.casefold(), tag.casefold())
        if normalized == "li":
            self.list_item_depth += 1
        if normalized == "p" and self.list_item_depth:
            return
        if normalized in self.ALLOWED_TAGS:
            self.parts.append(f"<{normalized}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        normalized = self.TAG_ALIASES.get(tag.casefold(), tag.casefold())
        if normalized == "p" and self.list_item_depth:
            return
        if normalized in self.ALLOWED_TAGS:
            self.parts.append(f"</{normalized}>")
        if normalized == "li" and self.list_item_depth:
            self.list_item_depth -= 1

    def handle_data(self, data):
        if not data or not data.strip():
            return
        value = re.sub(r"\s+", " ", data)
        self.parts.append(html.escape(value, quote=False))

    def content(self):
        value = "".join(self.parts).strip()
        previous = None
        while value != previous:
            previous = value
            value = re.sub(r"<(b|p|li)>\s*</\1>", "", value)
        return value


def sanitize_allegro_text_html(source_html):
    parser = _AllegroHtmlSanitizer()
    parser.feed(str(source_html or ""))
    parser.close()
    content = parser.content()
    if not content:
        raise AllegroDescriptionUpdateError("Brak treści opisu do zapisania.")
    if len(content) > MAX_ALLEGRO_DESCRIPTION_HTML_CHARS:
        raise AllegroDescriptionUpdateError("Opis przekracza limit Allegro.")
    return content


def replace_primary_text_item(description, content):
    if not isinstance(description, dict):
        raise AllegroDescriptionUpdateError("Oferta Allegro nie ma prawidłowego opisu.")
    updated = copy.deepcopy(description)
    sections = updated.get("sections")
    if not isinstance(sections, list) or not sections:
        sections = [{"items": []}]
        updated["sections"] = sections
    first_section = sections[0]
    if not isinstance(first_section, dict):
        raise AllegroDescriptionUpdateError("Pierwsza sekcja opisu jest nieprawidłowa.")
    items = first_section.get("items")
    if not isinstance(items, list):
        items = []
    replacement = {"type": "TEXT", "content": content}
    new_items = []
    inserted = False
    for item in items:
        if isinstance(item, dict) and str(item.get("type") or "").upper() == "TEXT":
            if not inserted:
                new_items.append(replacement)
                inserted = True
            continue
        new_items.append(copy.deepcopy(item))
    if not inserted:
        new_items.insert(0, replacement)
    first_section["items"] = new_items
    return updated


def _palette_candidates(description):
    sections = description.get("sections") if isinstance(description, dict) else None
    if not isinstance(sections, list) or not sections:
        raise AllegroDescriptionUpdateError("Oferta Allegro nie ma prawidłowych sekcji opisu.")
    candidates = []
    for section_index, section in enumerate(sections[1:], start=1):
        items = section.get("items") if isinstance(section, dict) else None
        if not isinstance(items, list):
            continue
        for item_index, item in enumerate(items):
            if not isinstance(item, dict) or str(item.get("type") or "").upper() != "TEXT":
                continue
            content = str(item.get("content") or "")
            analysis = analyze_material_palette_block(content, require_structure=False)
            if analysis["status"] != "absent" or LEGACY_PALETTE_PATTERN.search(content):
                if COMPANY_SECTION_PATTERN.search(content):
                    raise AllegroDescriptionUpdateError(
                        "Blok kolorów jest połączony z inną sekcją oferty."
                    )
                candidates.append((section_index, item_index, content, analysis))
    return candidates


def _template_palette_material(material):
    normalized = normalize_palette_material(material)
    return "PLA" if normalized == "PLA+" else normalized


def material_palette_section_matches(description, material):
    expected_material = _template_palette_material(material)
    if not expected_material:
        return False
    candidates = _palette_candidates(description)
    if len(candidates) != 1:
        return False
    analysis = analyze_material_palette_block(candidates[0][2], require_structure=True)
    return analysis["status"] == "match" and analysis["material"] == expected_material


def replace_material_palette_section(description, material):
    expected_material = _template_palette_material(material)
    canonical_html = canonical_material_palette_html(expected_material)
    if not expected_material or not canonical_html:
        raise AllegroDescriptionUpdateError(
            "Materiał produktu nie ma obsługiwanego wzorca kolorów."
        )
    if len(canonical_html) > MAX_ALLEGRO_DESCRIPTION_HTML_CHARS:
        raise AllegroDescriptionUpdateError("Wzorzec kolorów jest zbyt długi.")
    updated = copy.deepcopy(description)
    sections = updated.get("sections") if isinstance(updated, dict) else None
    if not isinstance(sections, list) or not sections:
        raise AllegroDescriptionUpdateError("Oferta Allegro nie ma prawidłowych sekcji opisu.")

    candidates = _palette_candidates(updated)
    if len(candidates) > 1:
        raise AllegroDescriptionUpdateError(
            "Oferta zawiera więcej niż jeden blok materiału i kolorów."
        )
    if candidates:
        if material_palette_section_matches(updated, expected_material):
            return updated, "unchanged"
        section_index, item_index, _content, _analysis = candidates[0]
        replacement = copy.deepcopy(sections[section_index]["items"][item_index])
        replacement["type"] = "TEXT"
        replacement["content"] = canonical_html
        sections[section_index]["items"][item_index] = replacement
        return updated, "replaced"

    insert_at = len(sections)
    for section_index, section in enumerate(sections[1:], start=1):
        items = (section.get("items") or []) if isinstance(section, dict) else []
        section_text = " ".join(
            str(item.get("content") or "")
            for item in items
            if isinstance(item, dict)
            and str(item.get("type") or "").upper() == "TEXT"
        )
        if COMPANY_SECTION_PATTERN.search(section_text):
            insert_at = section_index
            break
    sections.insert(
        insert_at,
        {"items": [{"type": "TEXT", "content": canonical_html}]},
    )
    return updated, "inserted"


def _description_image_urls(description):
    sections = (description.get("sections") or []) if isinstance(description, dict) else []
    return [
        str(item.get("url") or "")
        for section in sections
        if isinstance(section, dict)
        for item in section.get("items", [])
        if isinstance(item, dict) and str(item.get("type") or "").upper() == "IMAGE"
    ]


def _parameter_signature(parameter):
    if parameter.get("valuesIds"):
        return ("ids", tuple(str(value) for value in parameter["valuesIds"]))
    if parameter.get("values"):
        return ("values", tuple(str(value) for value in parameter["values"]))
    if parameter.get("rangeValue") is not None:
        value = parameter["rangeValue"]
        return ("range", json.dumps(value, ensure_ascii=False, sort_keys=True))
    return ("empty", "")


def _catalog_parameter_payload(parameter):
    payload: dict[str, object] = {"id": str(parameter.get("id") or "")}
    if not payload["id"]:
        raise AllegroDescriptionUpdateError(
            "Produkt katalogowy Allegro ma nieprawidłowy parametr."
        )
    if parameter.get("valuesIds"):
        payload["valuesIds"] = list(parameter["valuesIds"])
    elif parameter.get("values"):
        payload["values"] = list(parameter["values"])
    elif parameter.get("rangeValue") is not None:
        payload["rangeValue"] = copy.deepcopy(parameter["rangeValue"])
    else:
        raise AllegroDescriptionUpdateError(
            "Produkt katalogowy Allegro ma pusty parametr."
        )
    return payload


def build_catalog_aligned_product_set(offer, catalog_product):
    product_set = offer.get("productSet") if isinstance(offer, dict) else None
    if not product_set:
        return None, []
    if not isinstance(product_set, list) or len(product_set) != 1:
        raise AllegroDescriptionUpdateError(
            "Aktualizacja bloków kolorów nie obsługuje ofert zestawowych."
        )
    source = product_set[0]
    product = source.get("product") if isinstance(source, dict) else None
    if not isinstance(product, dict):
        raise AllegroDescriptionUpdateError(
            "Oferta Allegro nie ma przypisanego produktu katalogowego."
        )
    product_id = str((product or {}).get("id") or "")
    catalog_id = str((catalog_product or {}).get("id") or product_id)
    if not product_id or catalog_id != product_id:
        raise AllegroDescriptionUpdateError("Allegro zwróciło inny produkt katalogowy.")
    offer_parameters = {
        str(parameter.get("id") or ""): parameter
        for parameter in (product.get("parameters") or [])
        if isinstance(parameter, dict) and parameter.get("id")
    }
    catalog_parameters = {
        str(parameter.get("id") or ""): parameter
        for parameter in (catalog_product.get("parameters") or [])
        if isinstance(parameter, dict) and parameter.get("id")
    }
    changed_ids = sorted(
        parameter_id
        for parameter_id in set(offer_parameters) | set(catalog_parameters)
        if parameter_id not in offer_parameters
        or parameter_id not in catalog_parameters
        or _parameter_signature(offer_parameters[parameter_id])
        != _parameter_signature(catalog_parameters[parameter_id])
    )
    if not changed_ids:
        return None, []
    aligned = {
        "product": {
            "id": product_id,
            "parameters": [
                _catalog_parameter_payload(parameter)
                for parameter in catalog_parameters.values()
            ],
        },
        "quantity": copy.deepcopy(source.get("quantity") or {"value": 1}),
    }
    for key in (
        "responsiblePerson",
        "responsibleProducer",
        "safetyInformation",
        "marketedBeforeGPSRObligation",
        "deposits",
    ):
        if source.get(key) is not None:
            aligned[key] = copy.deepcopy(source[key])
    return [aligned], changed_ids


class AllegroDescriptionUpdater:
    def __init__(
        self,
        *,
        client_id,
        client_secret,
        refresh_token,
        save_token_payload=None,
        session=None,
        timeout_seconds=30,
        api_base_url=ALLEGRO_API_BASE_URL,
        oauth_base_url=ALLEGRO_OAUTH_BASE_URL,
    ):
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise AllegroDescriptionUpdateError("Brak konfiguracji zapisu Allegro.")
        self.save_token_payload = save_token_payload
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.api_base_url = api_base_url.rstrip("/")
        self.oauth_base_url = oauth_base_url.rstrip("/")

    @staticmethod
    def _validate_offer_id(offer_id):
        value = str(offer_id or "").strip()
        if not OFFER_ID_PATTERN.fullmatch(value):
            raise AllegroDescriptionUpdateError("Nieprawidłowy identyfikator oferty Allegro.")
        return value

    @staticmethod
    def _json_response(response, expected_statuses):
        if response.status_code not in expected_statuses:
            if response.status_code >= 500 or response.status_code in {408, 425, 429}:
                raise AllegroDescriptionUnverifiedError(
                    "Allegro nie potwierdziło wyniku operacji."
                )
            raise AllegroDescriptionUpdateError(
                f"Allegro odrzuciło operację (HTTP {response.status_code})."
            )
        if len(response.content or b"") > MAX_ALLEGRO_RESPONSE_BYTES:
            raise AllegroDescriptionUpdateError("Odpowiedź Allegro jest zbyt duża.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AllegroDescriptionUpdateError(
                "Allegro zwróciło nieprawidłową odpowiedź."
            ) from exc
        if not isinstance(payload, dict):
            raise AllegroDescriptionUpdateError("Allegro zwróciło nieprawidłowe dane.")
        return payload

    def _request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout_seconds)
        kwargs.setdefault("allow_redirects", False)
        return self.session.request(method, url, **kwargs)

    def refresh_access_token(self):
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        try:
            response = self._request(
                "POST",
                self.oauth_base_url + "/auth/oauth/token",
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                },
            )
        except requests.RequestException as exc:
            raise AllegroDescriptionUpdateError(
                "Nie udało się odświeżyć dostępu do Allegro."
            ) from exc
        payload = self._json_response(response, {200})
        access_token = str(payload.get("access_token") or "").strip()
        new_refresh_token = str(payload.get("refresh_token") or self.refresh_token).strip()
        if not access_token or not new_refresh_token:
            raise AllegroDescriptionUpdateError("Allegro nie zwróciło kompletu tokenów.")
        if self.save_token_payload:
            self.save_token_payload(
                {
                    "access_token": access_token,
                    "refresh_token": new_refresh_token,
                    "expires_in": int(payload.get("expires_in") or 3600),
                    "token_type": str(payload.get("token_type") or "bearer"),
                    "scope": payload.get("scope"),
                }
            )
        self.refresh_token = new_refresh_token
        return access_token

    def get_offer(self, offer_id, access_token):
        offer_id = self._validate_offer_id(offer_id)
        try:
            response = self._request(
                "GET",
                self.api_base_url + f"/sale/product-offers/{offer_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": ALLEGRO_ACCEPT,
                    "Accept-Language": "pl-PL",
                },
            )
        except requests.RequestException as exc:
            raise AllegroDescriptionUnverifiedError(
                "Nie udało się odczytać oferty Allegro."
            ) from exc
        offer = self._json_response(response, {200})
        if str(offer.get("id") or "") != offer_id:
            raise AllegroDescriptionUpdateError("Allegro zwróciło inną ofertę.")
        return offer

    def get_catalog_product(self, product_id, access_token):
        product_id = str(product_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{10,100}", product_id):
            raise AllegroDescriptionUpdateError(
                "Nieprawidłowy identyfikator produktu katalogowego Allegro."
            )
        try:
            response = self._request(
                "GET",
                self.api_base_url + f"/sale/products/{product_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": ALLEGRO_ACCEPT,
                    "Accept-Language": "pl-PL",
                },
            )
        except requests.RequestException as exc:
            raise AllegroDescriptionUnverifiedError(
                "Nie udało się odczytać produktu katalogowego Allegro."
            ) from exc
        product = self._json_response(response, {200})
        if str(product.get("id") or "") != product_id:
            raise AllegroDescriptionUpdateError(
                "Allegro zwróciło inny produkt katalogowy."
            )
        return product

    def patch_description(
        self, offer_id, access_token, description, *, product_set=None
    ):
        offer_id = self._validate_offer_id(offer_id)
        payload = {"description": description}
        if product_set is not None:
            payload["productSet"] = product_set
        try:
            response = self._request(
                "PATCH",
                self.api_base_url + f"/sale/product-offers/{offer_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": ALLEGRO_ACCEPT,
                    "Content-Type": ALLEGRO_ACCEPT,
                    "Accept-Language": "pl-PL",
                },
                json=payload,
            )
        except requests.RequestException as exc:
            raise AllegroDescriptionUnverifiedError(
                "Nie udało się potwierdzić odpowiedzi po zapisie Allegro."
            ) from exc
        self._json_response(response, {200, 201, 202})

    def update_primary_description(self, offer_id, reference_html, reference_text):
        offer_id = self._validate_offer_id(offer_id)
        content = sanitize_allegro_text_html(reference_html)
        sanitized_description = {"sections": [{"items": [{"type": "TEXT", "content": content}]}]}
        sanitized_text = description_primary_section_text(sanitized_description)
        if not description_matches(reference_text, sanitized_text):
            raise AllegroDescriptionUpdateError(
                "Konwersja opisu zmieniłaby treść względem wzorca Apilo."
            )

        access_token = self.refresh_access_token()
        current_offer = self.get_offer(offer_id, access_token)
        current_text = description_primary_section_text(current_offer.get("description"))
        if description_matches(reference_text, current_text):
            return {
                "outcome": "unchanged",
                "offer": current_offer,
                "access_token": access_token,
            }

        updated_description = replace_primary_text_item(
            current_offer.get("description"), content
        )
        patch_was_ambiguous = False
        try:
            self.patch_description(offer_id, access_token, updated_description)
        except AllegroDescriptionUnverifiedError:
            patch_was_ambiguous = True

        try:
            verified_offer = self.get_offer(offer_id, access_token)
        except AllegroDescriptionUpdateError as exc:
            raise AllegroDescriptionUnverifiedError(
                "Nie udało się potwierdzić opisu po zapisie."
            ) from exc
        verified_text = description_primary_section_text(verified_offer.get("description"))
        if not description_matches(reference_text, verified_text):
            raise AllegroDescriptionUnverifiedError(
                "Allegro nie potwierdziło zgodności opisu po zapisie."
            )
        return {
            "outcome": "verified_after_error" if patch_was_ambiguous else "updated",
            "offer": verified_offer,
            "access_token": access_token,
        }

    def update_material_palette(self, offer_id, material):
        offer_id = self._validate_offer_id(offer_id)
        expected_material = _template_palette_material(material)
        if not expected_material:
            raise AllegroDescriptionUpdateError(
                "Materiał produktu nie ma obsługiwanego wzorca kolorów."
            )

        access_token = self.refresh_access_token()
        current_offer = self.get_offer(offer_id, access_token)
        current_description = current_offer.get("description")
        updated_description, outcome = replace_material_palette_section(
            current_description, expected_material
        )
        if outcome == "unchanged":
            return {
                "outcome": outcome,
                "verified_after_error": False,
                "catalog_parameter_ids": [],
                "offer": current_offer,
                "access_token": access_token,
            }

        catalog_product = None
        aligned_product_set = None
        catalog_parameter_ids = []
        current_product_set = current_offer.get("productSet") or []
        if current_product_set:
            if not isinstance(current_product_set, list) or len(current_product_set) != 1:
                raise AllegroDescriptionUpdateError(
                    "Aktualizacja bloków kolorów nie obsługuje ofert zestawowych."
                )
            current_product = current_product_set[0].get("product") or {}
            product_id = str(current_product.get("id") or "")
            catalog_product = self.get_catalog_product(product_id, access_token)
            aligned_product_set, catalog_parameter_ids = (
                build_catalog_aligned_product_set(current_offer, catalog_product)
            )

        current_primary_text = description_primary_section_text(current_description)
        current_images = _description_image_urls(current_description)
        patch_was_ambiguous = False
        try:
            self.patch_description(
                offer_id,
                access_token,
                updated_description,
                product_set=aligned_product_set,
            )
        except AllegroDescriptionUnverifiedError:
            patch_was_ambiguous = True

        try:
            verified_offer = self.get_offer(offer_id, access_token)
        except AllegroDescriptionUpdateError as exc:
            raise AllegroDescriptionUnverifiedError(
                "Nie udało się potwierdzić bloku materiału i kolorów po zapisie."
            ) from exc
        verified_description = verified_offer.get("description")
        verified_primary_text = description_primary_section_text(verified_description)
        if not description_matches(current_primary_text, verified_primary_text):
            raise AllegroDescriptionUnverifiedError(
                "Główny opis oferty zmienił się podczas aktualizacji kolorów."
            )
        if _description_image_urls(verified_description) != current_images:
            raise AllegroDescriptionUnverifiedError(
                "Zdjęcia oferty zmieniły się podczas aktualizacji kolorów."
            )
        if not material_palette_section_matches(verified_description, expected_material):
            raise AllegroDescriptionUnverifiedError(
                "Allegro nie potwierdziło poprawnego bloku materiału i kolorów."
            )
        if catalog_parameter_ids:
            remaining_alignment, remaining_ids = build_catalog_aligned_product_set(
                verified_offer, catalog_product
            )
            if remaining_alignment is not None or remaining_ids:
                raise AllegroDescriptionUnverifiedError(
                    "Allegro nie potwierdziło parametrów produktu katalogowego."
                )
        return {
            "outcome": outcome,
            "verified_after_error": patch_was_ambiguous,
            "catalog_parameter_ids": catalog_parameter_ids,
            "offer": verified_offer,
            "access_token": access_token,
        }


class AllegroFileCredentialStore:
    MAX_ENV_BYTES = 65_536
    MAX_TOKEN_BYTES = 1_000_000

    def __init__(self, *, env_path, token_path, session=None):
        self.env_path = Path(env_path)
        self.token_path = Path(token_path)
        self.session = session

    @staticmethod
    def _validate_private_file(path, max_bytes):
        try:
            info = path.lstat()
        except OSError as exc:
            raise AllegroDescriptionUpdateError(
                "Brak bezpiecznej konfiguracji Allegro."
            ) from exc
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise AllegroDescriptionUpdateError(
                "Plik konfiguracji Allegro jest nieprawidłowy."
            )
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AllegroDescriptionUpdateError(
                "Plik konfiguracji Allegro ma niebezpieczne uprawnienia."
            )
        if info.st_size > max_bytes:
            raise AllegroDescriptionUpdateError(
                "Plik konfiguracji Allegro jest zbyt duży."
            )

    def _load_env(self):
        self._validate_private_file(self.env_path, self.MAX_ENV_BYTES)
        values = {}
        for raw in self.env_path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def _validate_token_directory(self):
        parent = self.token_path.parent
        try:
            info = parent.lstat()
        except OSError as exc:
            raise AllegroDescriptionUpdateError(
                "Brak bezpiecznego katalogu tokenów Allegro."
            ) from exc
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not os.access(parent, os.W_OK)
        ):
            raise AllegroDescriptionUpdateError(
                "Katalog tokenów Allegro ma niebezpieczne uprawnienia."
            )

    def _load_tokens(self):
        self._validate_private_file(self.token_path, self.MAX_TOKEN_BYTES)
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AllegroDescriptionUpdateError(
                "Magazyn tokenów Allegro jest nieprawidłowy."
            ) from exc
        if not isinstance(payload, dict):
            raise AllegroDescriptionUpdateError(
                "Magazyn tokenów Allegro jest nieprawidłowy."
            )
        return payload

    def _load_configuration(self):
        self._validate_token_directory()
        env = self._load_env()
        tokens = self._load_tokens()
        values = {
            "client_id": str(env.get("ALLEGRO_CLIENT_ID") or "").strip(),
            "client_secret": str(env.get("ALLEGRO_CLIENT_SECRET") or "").strip(),
            "refresh_token": str(tokens.get("refresh_token") or "").strip(),
        }
        if not all(values.values()):
            raise AllegroDescriptionUpdateError("Brak konfiguracji zapisu Allegro.")
        return values

    def is_configured(self):
        try:
            self._load_configuration()
            return True
        except (AllegroDescriptionUpdateError, OSError):
            return False

    def _save_token_payload(self, refreshed):
        current = self._load_tokens()
        current.update(
            {
                "_schema_version": 1,
                "access_token": refreshed["access_token"],
                "refresh_token": refreshed["refresh_token"],
                "token_type": refreshed.get("token_type") or "bearer",
                "expires_at": time.time()
                + int(refreshed.get("expires_in") or 3600)
                - 60,
            }
        )
        current.pop("extra", None)
        if refreshed.get("scope"):
            current["scope"] = refreshed["scope"]
        parent = self.token_path.parent
        self._validate_token_directory()
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=".tokens-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.chmod(temporary_path, 0o600)
                json.dump(current, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.token_path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

    @contextmanager
    def locked_updater(self):
        parent = self.token_path.parent
        lock_path = parent / ".tokens.lock"
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            values = self._load_configuration()
            yield AllegroDescriptionUpdater(
                client_id=values["client_id"],
                client_secret=values["client_secret"],
                refresh_token=values["refresh_token"],
                save_token_payload=self._save_token_payload,
                session=self.session,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
