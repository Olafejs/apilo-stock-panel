#!/usr/bin/env python3
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app_channels import PRESTASHOP_PUBLIC_BASE_URL, build_listing_url  # noqa: E402
from db import (  # noqa: E402
    get_apilo_description_references,
    get_db,
    init_db,
    replace_channel_description_checks,
)
from description_checks import description_matches, description_preview  # noqa: E402
from material_palette_checks import analyze_material_palette_block  # noqa: E402
from product_attributes import (  # noqa: E402
    description_primary_section_text,
    description_to_text,
)
from scripts.sync_allegro_attributes import (  # noqa: E402
    allegro_get_product_offer,
    get_access_token,
    load_env_file,
)

PUBLIC_CHANNELS = {"prestashop", "erli"}
UNAVAILABLE_CHANNELS = {"empik", "etsy"}
PRESTASHOP_PUBLIC_HOST = urlparse(PRESTASHOP_PUBLIC_BASE_URL).hostname or ""
ALLOWED_PUBLIC_HOSTS = {"erli.pl", "www.erli.pl"}
if PRESTASHOP_PUBLIC_HOST:
    ALLOWED_PUBLIC_HOSTS.add(PRESTASHOP_PUBLIC_HOST)
MAX_PAGE_BYTES = 2_500_000
PUBLIC_GET_ATTEMPTS = 4
RETRYABLE_PUBLIC_STATUS = {403, 429, 502, 503, 504}
PUBLIC_REQUEST_SEMAPHORE = threading.BoundedSemaphore(3)
PUBLIC_RATE_LOCK = threading.Lock()
PUBLIC_NEXT_REQUEST_AT = {}
PUBLIC_BLOCK_UNTIL = {}
PUBLIC_REQUEST_INTERVAL = {"erli.pl": 0.6}
if PRESTASHOP_PUBLIC_HOST:
    PUBLIC_REQUEST_INTERVAL[PRESTASHOP_PUBLIC_HOST] = 0.08
MAX_PUBLIC_REDIRECTS = 5
MAX_STORED_DESCRIPTION_CHARS = 65_535


class _JsonLdCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._active = False
        self._parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and "ld+json" in str(
            attributes.get("type") or ""
        ).lower():
            self._active = True
            self._parts = []

    def handle_endtag(self, tag):
        if tag == "script" and self._active:
            self._active = False
            self.scripts.append("".join(self._parts))

    def handle_data(self, data):
        if self._active:
            self._parts.append(data)


class _PublicDescriptionExtractor(HTMLParser):
    BLOCK_TAGS = {
        "article",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ol",
        "p",
        "section",
        "table",
        "tr",
        "ul",
    }
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, channel):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.depth = 0
        self.parts = []
        self.done = False
        self.skip_depth = 0

    def _matches_root(self, tag, attributes):
        classes = set(str(attributes.get("class") or "").split())
        if self.channel == "prestashop":
            return attributes.get("id") == "description"
        return (
            self.channel == "erli"
            and tag == "section"
            and (
                attributes.get("id") == "product-description"
                or "product-description" in classes
            )
        )

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        attributes = dict(attrs)
        if not self.depth:
            if self._matches_root(tag, attributes):
                self.depth = 1
            return
        if tag in {"script", "style", "template"}:
            self.skip_depth += 1
        if not self.skip_depth:
            if tag == "br":
                self.parts.append("\n")
            elif tag == "li":
                self.parts.append("\n• ")
            elif tag in self.BLOCK_TAGS:
                self.parts.append("\n")
        if tag not in self.VOID_TAGS:
            self.depth += 1

    def handle_endtag(self, tag):
        if not self.depth or self.done:
            return
        if tag in {"script", "style", "template"} and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and (tag == "li" or tag in self.BLOCK_TAGS):
            self.parts.append("\n")
        self.depth -= 1
        if self.depth == 0:
            self.done = True

    def handle_data(self, data):
        if self.depth and not self.done and not self.skip_depth:
            self.parts.append(data)


def _walk_json_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk_json_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in value:
                yield from _walk_json_nodes(value[key])


def _extract_public_description(channel, page):
    candidates = []
    subtree = _PublicDescriptionExtractor(channel)
    subtree.feed(page)
    subtree.close()
    subtree_text = description_preview("".join(subtree.parts))
    if subtree_text:
        if channel == "erli":
            palette_start = subtree_text.casefold().find(
                "nasze wydruki z materiału"
            )
            if palette_start >= 0:
                subtree_text = subtree_text[:palette_start].rstrip()
        return subtree_text[:MAX_STORED_DESCRIPTION_CHARS]

    collector = _JsonLdCollector()
    collector.feed(page)
    collector.close()
    for script in collector.scripts:
        try:
            value = json.loads(script)
        except (TypeError, ValueError):
            continue
        for node in _walk_json_nodes(value):
            node_type = node.get("@type")
            node_types = [node_type] if isinstance(node_type, str) else node_type or []
            description = node.get("description")
            if "Product" in node_types and isinstance(description, str):
                candidate = description_preview(description)
                if candidate:
                    candidates.append(candidate)
    if not candidates:
        return ""
    return max(candidates, key=len)[:MAX_STORED_DESCRIPTION_CHARS]


def _load_targets(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT cl.apilo_product_id, cl.channel_key, cl.external_id,
               cl.listing_name, cl.status, p.name, p.ean, p.sku
        FROM channel_listings cl
        JOIN products p ON p.apilo_id = cl.apilo_product_id
        WHERE p.present_in_apilo = 1 AND COALESCE(cl.external_id, '') != ''
        ORDER BY cl.apilo_product_id, cl.channel_key, cl.external_id
        """
    ).fetchall()
    conn.close()
    targets = []
    seen = set()
    for row in rows:
        target = dict(row)
        key = (
            int(target["apilo_product_id"]),
            str(target["channel_key"]),
            str(target["external_id"]),
        )
        if key in seen:
            continue
        seen.add(key)
        targets.append(target)
    return targets


def _throttle_public_host(host):
    interval = PUBLIC_REQUEST_INTERVAL.get(host, 0.2)
    with PUBLIC_RATE_LOCK:
        now = time.monotonic()
        allowed_at = max(
            now,
            PUBLIC_NEXT_REQUEST_AT.get(host, now),
            PUBLIC_BLOCK_UNTIL.get(host, now),
        )
        PUBLIC_NEXT_REQUEST_AT[host] = allowed_at + interval
    if allowed_at > now:
        time.sleep(allowed_at - now)


def _penalize_public_host(host, seconds=15.0):
    with PUBLIC_RATE_LOCK:
        PUBLIC_BLOCK_UNTIL[host] = max(
            PUBLIC_BLOCK_UNTIL.get(host, 0.0),
            time.monotonic() + seconds,
        )


def _validate_public_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in ALLOWED_PUBLIC_HOSTS
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("Niedozwolony adres strony oferty.")
    return host


def _request_public_url(url):
    host = _validate_public_url(url)
    response = None
    for attempt in range(PUBLIC_GET_ATTEMPTS):
        try:
            _throttle_public_host(host)
            with PUBLIC_REQUEST_SEMAPHORE:
                response = requests.get(
                    url,
                    headers={"User-Agent": "Apilo-Stock-Panel/description-read-only"},
                    timeout=(5, 30),
                    stream=True,
                    allow_redirects=False,
                )
            if response.status_code not in RETRYABLE_PUBLIC_STATUS:
                break
            if response.status_code in {403, 429}:
                _penalize_public_host(host)
            response.close()
            if attempt + 1 >= PUBLIC_GET_ATTEMPTS:
                break
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else float(2**attempt)
            except ValueError:
                delay = float(2**attempt)
            time.sleep(min(8.0, max(0.5, delay)))
        except requests.RequestException:
            if attempt + 1 >= PUBLIC_GET_ATTEMPTS:
                raise
            time.sleep(float(2**attempt))
    if response is None:
        raise RuntimeError("Nie pobrano strony oferty.")
    return response


def _bounded_public_page(url):
    current_url = url
    response = None
    for _ in range(MAX_PUBLIC_REDIRECTS + 1):
        response = _request_public_url(current_url)
        if not 300 <= response.status_code < 400:
            break
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise RuntimeError("Przekierowanie strony oferty nie zawiera adresu.")
        current_url = urljoin(current_url, location)
        _validate_public_url(current_url)
    else:
        raise RuntimeError("Strona oferty przekroczyła limit przekierowań.")
    if response is None:
        raise RuntimeError("Nie pobrano strony oferty.")
    response.raise_for_status()
    chunks = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_PAGE_BYTES:
            raise RuntimeError("Strona oferty przekracza limit 2,5 MB.")
        chunks.append(chunk)
    encoding = response.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


def _result(
    target,
    reference,
    status,
    source,
    detail="",
    actual_description_text="",
    palette=None,
):
    palette = palette or {
        "status": "unverified",
        "material": "",
        "text": "",
        "block_hash": "",
    }
    return {
        "apilo_product_id": target["apilo_product_id"],
        "channel_key": target["channel_key"],
        "external_id": target["external_id"],
        "reference_hash": reference["description_hash"],
        "status": status,
        "source": source,
        "detail": detail,
        "actual_description_text": str(actual_description_text or "")[:MAX_STORED_DESCRIPTION_CHARS],
        "palette_status": str(palette.get("status") or "unverified")[:30],
        "palette_material": str(palette.get("material") or "")[:30],
        "palette_block_text": str(palette.get("text") or "")[:MAX_STORED_DESCRIPTION_CHARS],
        "palette_block_hash": str(palette.get("block_hash") or "")[:64],
    }


def build_structured_description_result(target, reference, description, source):
    full_marketplace_text = description_to_text(description)
    marketplace_text = description_primary_section_text(description)
    palette_html = " ".join(
        str(item.get("content") or "")
        for section in (description or {}).get("sections") or []
        for item in section.get("items") or []
        if str(item.get("type") or "").upper() == "TEXT"
    )
    palette = analyze_material_palette_block(
        palette_html or full_marketplace_text,
        require_structure=True,
    )
    matches = description_matches(reference["description_text"], marketplace_text)
    return _result(
        target,
        reference,
        "match" if matches else "mismatch",
        source,
        actual_description_text=marketplace_text,
        palette=palette,
    )


def _check_target(target, reference, allegro_access_token):
    channel = target["channel_key"]
    try:
        if channel == "allegro":
            if not allegro_access_token:
                return _result(
                    target,
                    reference,
                    "unavailable",
                    "allegro_api",
                    "brak_autoryzacji",
                    palette={"status": "unavailable"},
                )
            offer = allegro_get_product_offer(target["external_id"], allegro_access_token)
            marketplace_description = offer.get("description")
            return build_structured_description_result(
                target,
                reference,
                marketplace_description,
                "allegro_api",
            )
        if channel in PUBLIC_CHANNELS:
            url = build_listing_url(channel, target, target)
            page = _bounded_public_page(url)
            marketplace_text = _extract_public_description(channel, page)
            matches = description_matches(reference["description_text"], marketplace_text)
            palette = (
                analyze_material_palette_block(page, require_structure=True)
                if channel == "erli"
                else {"status": "not_applicable"}
            )
            return _result(
                target,
                reference,
                "match" if matches else "mismatch",
                "public_page",
                actual_description_text=marketplace_text,
                palette=palette,
            )
        if channel in UNAVAILABLE_CHANNELS:
            return _result(
                target,
                reference,
                "unavailable",
                "marketplace_api",
                "opis_niedostepny",
                palette={"status": "unavailable"},
            )
        return _result(
            target,
            reference,
            "unavailable",
            "unknown_channel",
            "brak_konektora",
        )
    except Exception as exc:
        return _result(
            target,
            reference,
            "error",
            "description_checker",
            type(exc).__name__,
        )


def run(db_path, *, env_file, workers=6):
    init_db(db_path)
    references = {
        int(item["apilo_product_id"]): item
        for item in get_apilo_description_references(db_path)
    }
    if not references:
        raise RuntimeError("Brak opisów referencyjnych Apilo.")
    targets = _load_targets(db_path)
    targets = [item for item in targets if int(item["apilo_product_id"]) in references]

    if env_file:
        load_env_file(env_file)
    allegro_access_token = None
    if any(item["channel_key"] == "allegro" for item in targets):
        try:
            allegro_access_token = get_access_token()
        except Exception:
            allegro_access_token = None

    checks = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 12))) as executor:
        futures = {
            executor.submit(
                _check_target,
                target,
                references[int(target["apilo_product_id"])],
                allegro_access_token,
            ): target
            for target in targets
        }
        for future in as_completed(futures):
            checks.append(future.result())
    checks.sort(
        key=lambda item: (
            item["apilo_product_id"],
            item["channel_key"],
            item["external_id"],
        )
    )
    replace_channel_description_checks(db_path, checks)
    return {
        "checked": len(checks),
        "statuses": dict(sorted(Counter(item["status"] for item in checks).items())),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Porównuje opisy kanałów z wzorcowymi opisami Apilo."
    )
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "db" / "apilo.sqlite3"))
    parser.add_argument(
        "--env-file",
        default=os.environ.get("ALLEGRO_ENV_FILE", ""),
    )
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    result = run(
        args.db,
        env_file=args.env_file,
        workers=args.workers,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
