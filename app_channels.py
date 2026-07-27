import math
import os
import re
import unicodedata
from collections import defaultdict
from urllib.parse import quote, quote_plus


CHANNEL_ORDER = {
    "prestashop": 10,
    "allegro": 20,
    "erli": 30,
    "empik": 40,
    "etsy": 50,
}
CHANNEL_ALIASES = {
    "AL": "allegro",
    "ER": "erli",
    "PR": "prestashop",
    "EM": "empik",
    "ET": "etsy",
}
EXCLUDED_CHANNEL_ALIASES = {"MA"}
ACTIVE_AUCTION_STATUS = 2
PRESTASHOP_PUBLIC_BASE_URL = os.getenv(
    "PRESTASHOP_PUBLIC_BASE_URL", "https://shop.example.com"
).rstrip("/")


def _channel_key(platform):
    alias = str(platform.get("alias") or "").strip().upper()
    if alias in CHANNEL_ALIASES:
        return CHANNEL_ALIASES[alias]
    name = str(platform.get("name") or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def build_sales_channels(platforms):
    channels_by_key = {}
    for fallback_order, platform in enumerate(platforms, start=100):
        alias = str(platform.get("alias") or "").strip().upper()
        if alias in EXCLUDED_CHANNEL_ALIASES:
            continue
        key = _channel_key(platform)
        if not key or key in channels_by_key:
            continue
        channels_by_key[key] = {
            "channel_key": key,
            "channel_name": str(platform.get("name") or key).strip(),
            "platform_id": platform.get("id"),
            "alias": alias,
            "sort_order": CHANNEL_ORDER.get(key, fallback_order),
        }
    return sorted(
        channels_by_key.values(),
        key=lambda item: (item["sort_order"], item["channel_name"]),
    )


def _product_lookup_maps(products):
    by_apilo_id = {}
    by_sku = {}
    by_ean = {}
    for product in products:
        product_id = product.get("id")
        if product_id is None:
            continue
        product_id = int(product_id)
        by_apilo_id[str(product_id)] = product_id
        if product.get("sku"):
            by_sku[str(product["sku"])] = product_id
        if product.get("ean"):
            by_ean[str(product["ean"])] = product_id
    return by_apilo_id, by_sku, by_ean


def _matched_product_rows(auction, by_apilo_id, by_sku, by_ean):
    matched = {}
    for auction_product in auction.get("auctionProducts") or []:
        product_info = auction_product.get("product") or {}
        product_id = product_info.get("id") or auction_product.get("productId")
        mapped_id = by_apilo_id.get(str(product_id)) if product_id is not None else None
        if mapped_id is None:
            sku = auction_product.get("sku") or product_info.get("sku")
            mapped_id = by_sku.get(str(sku)) if sku else None
        if mapped_id is None:
            ean = auction_product.get("ean") or product_info.get("ean")
            mapped_id = by_ean.get(str(ean)) if ean else None
        if mapped_id is not None:
            matched.setdefault(mapped_id, auction_product)
    return matched.items()


def build_channel_listing_rows(products, platforms, auctions):
    channels = build_sales_channels(platforms)
    channel_by_key = {item["channel_key"]: item for item in channels}
    channel_by_platform_id = {}
    for platform in platforms:
        alias = str(platform.get("alias") or "").strip().upper()
        if alias in EXCLUDED_CHANNEL_ALIASES:
            continue
        channel = channel_by_key.get(_channel_key(platform))
        if channel and platform.get("id") is not None:
            channel_by_platform_id[platform["id"]] = channel
    by_apilo_id, by_sku, by_ean = _product_lookup_maps(products)
    rows = []
    seen = set()
    for auction in auctions:
        platform_account = auction.get("platformAccount") or {}
        channel = channel_by_platform_id.get(platform_account.get("id"))
        if not channel:
            continue
        auction_id = auction.get("id")
        external_id = str(auction.get("idExternal") or "").strip()
        for product_id, auction_product in _matched_product_rows(
            auction, by_apilo_id, by_sku, by_ean
        ):
            identity = (product_id, channel["channel_key"], str(auction_id))
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(
                {
                    "apilo_product_id": product_id,
                    "channel_key": channel["channel_key"],
                    "apilo_auction_id": auction_id,
                    "external_id": external_id,
                    "status": auction.get("status"),
                    "listing_name": str(auction.get("name") or "").strip(),
                    "offer_price": auction_product.get("priceWithTax"),
                    "offer_quantity": auction_product.get("quantitySelling"),
                }
            )
    return channels, rows


def slugify_listing_name(value):
    normalized = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()


def build_listing_url(channel_key, listing, product):
    external_id = str(listing.get("external_id") or "").strip()
    if channel_key == "allegro" and external_id:
        return f"https://allegro.pl/oferta/{quote(external_id, safe='')}"
    if channel_key == "erli" and external_id:
        slug = slugify_listing_name(listing.get("listing_name") or product.get("name")) or "produkt"
        return f"https://erli.pl/produkt/{slug},{quote(external_id, safe='')}"
    if channel_key == "prestashop" and external_id:
        return (
            f"{PRESTASHOP_PUBLIC_BASE_URL}/index.php?id_product="
            f"{quote(external_id, safe='')}&controller=product"
        )
    if channel_key == "empik":
        query = product.get("ean") or listing.get("listing_name") or product.get("name")
        if query:
            return f"https://www.empik.com/szukaj/produkt?q={quote_plus(str(query))}"
    if channel_key == "etsy" and external_id:
        return f"https://www.etsy.com/listing/{quote(external_id, safe='')}"
    return ""


def classify_channel_cell(listings):
    if not listings:
        return "missing"
    if len(listings) == 1 and listings[0].get("status") == ACTIVE_AUCTION_STATUS:
        return "ok"
    return "review"


def _product_matches_search(product, search):
    if not search:
        return True
    needle = search.casefold()
    values = (
        product.get("name"),
        product.get("sku"),
        product.get("ean"),
        product.get("original_code"),
        product.get("apilo_id"),
    )
    return any(needle in str(value).casefold() for value in values if value is not None)


def _empik_offer_indexes(offers):
    indexes = {
        "offer_id": defaultdict(list),
        "shop_sku": defaultdict(list),
        "product_sku": defaultdict(list),
    }
    for raw_offer in offers or []:
        offer = dict(raw_offer)
        for field in indexes:
            value = str(offer.get(field) or "").strip()
            if value:
                indexes[field][value].append(offer)
    return indexes


def _match_empik_offers(product, listings, indexes):
    direct_matches = []
    seen_ids = set()
    for listing in listings:
        external_id = str(listing.get("external_id") or "").strip()
        for offer in indexes["offer_id"].get(external_id, []):
            offer_id = str(offer.get("offer_id"))
            if offer_id not in seen_ids:
                direct_matches.append(offer)
                seen_ids.add(offer_id)
    if direct_matches:
        return direct_matches, "offer_id"

    candidates = (
        ("shop_sku", product.get("sku")),
        ("product_sku", product.get("ean")),
    )
    for field, raw_value in candidates:
        value = str(raw_value or "").strip()
        matches = indexes[field].get(value, []) if value else []
        if len(matches) == 1:
            return [matches[0]], field
    return [], "none"


def build_channel_matrix(
    products,
    channels,
    listings,
    *,
    search="",
    channel_filter="",
    status_filter="",
    page=1,
    limit=50,
    empik_offers=None,
    empik_api_enabled=False,
    description_references=None,
    description_checks=None,
    description_filter="",
    palette_filter="",
):
    listing_map = defaultdict(list)
    for listing in listings:
        listing_map[(listing["apilo_product_id"], listing["channel_key"])].append(
            dict(listing)
        )
    empik_indexes = _empik_offer_indexes(empik_offers)
    reference_map = {
        int(reference["apilo_product_id"]): dict(reference)
        for reference in description_references or []
    }
    check_map = {
        (
            int(check["apilo_product_id"]),
            str(check["channel_key"]),
            str(check.get("external_id") or ""),
        ): dict(check)
        for check in description_checks or []
    }
    palette_materials_by_product = defaultdict(set)
    palette_required_products = set()
    for (product_id, channel_key, _external_id), check in check_map.items():
        reference = reference_map.get(product_id)
        if (
            channel_key not in {"allegro", "erli"}
            or not reference
            or check.get("reference_hash") != reference.get("description_hash")
            or check.get("palette_status") not in {"match", "mismatch"}
        ):
            continue
        palette_required_products.add(product_id)
        material = str(check.get("palette_material") or "").strip()
        if material:
            palette_materials_by_product[product_id].add(material)

    rows = []
    totals = {"ok": 0, "missing": 0, "review": 0}
    description_totals = {
        "match": 0,
        "mismatch": 0,
        "unavailable": 0,
        "unverified": 0,
        "no_reference": 0,
    }
    palette_totals = {
        "match": 0,
        "mismatch": 0,
        "unavailable": 0,
        "unverified": 0,
    }
    for raw_product in products:
        product = dict(raw_product)
        if not _product_matches_search(product, search):
            continue
        reference = reference_map.get(int(product["apilo_id"]))
        product_id = int(product["apilo_id"])
        product_palette_required = product_id in palette_required_products
        product_palette_materials = palette_materials_by_product.get(product_id, set())
        product_palette_material = (
            next(iter(product_palette_materials))
            if len(product_palette_materials) == 1
            else ""
        )
        product_palette_conflict = len(product_palette_materials) > 1
        product["has_apilo_description"] = bool(reference)
        cells = {}
        for channel in channels:
            key = channel["channel_key"]
            cell_listings = listing_map.get((product["apilo_id"], key), [])
            cell_listings.sort(
                key=lambda item: (
                    item.get("status") != ACTIVE_AUCTION_STATUS,
                    item.get("external_id") or "",
                )
            )
            cell_status = classify_channel_cell(cell_listings)
            direct_offers = []
            direct_match = "none"
            if key == "empik" and empik_api_enabled:
                direct_offers, direct_match = _match_empik_offers(
                    product,
                    cell_listings,
                    empik_indexes,
                )
                direct_active = bool(direct_offers) and all(
                    bool(offer.get("active")) for offer in direct_offers
                )
                if (
                    len(cell_listings) != 1
                    or len(direct_offers) != 1
                    or not direct_active
                    or cell_listings[0].get("status") != ACTIVE_AUCTION_STATUS
                ):
                    cell_status = "review" if cell_listings or direct_offers else "missing"
                else:
                    cell_status = "ok"
            enriched_listings = []
            check_statuses = []
            listing_palette_statuses = []
            listing_palette_materials = []
            for listing in cell_listings:
                check = check_map.get(
                    (
                        int(product["apilo_id"]),
                        key,
                        str(listing.get("external_id") or ""),
                    )
                )
                if check and reference and check.get("reference_hash") != reference.get(
                    "description_hash"
                ):
                    check = None
                if check:
                    check_statuses.append(check["status"])
                    raw_palette_status = str(
                        check.get("palette_status") or "unverified"
                    )
                    if (
                        key in {"allegro", "erli"}
                        and raw_palette_status == "absent"
                        and product_palette_required
                    ):
                        raw_palette_status = "missing"
                    listing_palette_statuses.append(raw_palette_status)
                    if check.get("palette_material"):
                        listing_palette_materials.append(
                            str(check["palette_material"])
                        )
                elif key in {"allegro", "erli", "empik"}:
                    listing_palette_statuses.append("unverified")
                enriched_listings.append(
                    {
                        **listing,
                        "url": build_listing_url(key, listing, product),
                        "link_label": "Znajdź" if key == "empik" else "Otwórz",
                        "description_check": check,
                    }
                )
            if not cell_listings:
                description_status = "not_applicable"
            elif not reference:
                description_status = "no_reference"
            elif "mismatch" in check_statuses:
                description_status = "mismatch"
            elif len(check_statuses) == len(cell_listings) and all(
                status == "match" for status in check_statuses
            ):
                description_status = "match"
            elif "unavailable" in check_statuses:
                description_status = "unavailable"
            else:
                description_status = "unverified"
            if not cell_listings or key not in {"allegro", "erli", "empik"}:
                palette_status = "not_applicable"
            elif key == "empik":
                palette_status = "unavailable"
            elif product_palette_conflict or "mismatch" in listing_palette_statuses:
                palette_status = "mismatch"
            elif "missing" in listing_palette_statuses:
                palette_status = "missing"
            elif (
                listing_palette_statuses
                and len(listing_palette_statuses) == len(cell_listings)
                and all(status == "match" for status in listing_palette_statuses)
            ):
                palette_status = "match"
            elif "unavailable" in listing_palette_statuses:
                palette_status = "unavailable"
            elif not product_palette_required and listing_palette_statuses and all(
                status in {"absent", "not_applicable"}
                for status in listing_palette_statuses
            ):
                palette_status = "not_applicable"
            else:
                palette_status = "unverified"
            palette_material = product_palette_material
            if not palette_material and len(set(listing_palette_materials)) == 1:
                palette_material = listing_palette_materials[0]
            cells[key] = {
                "status": cell_status,
                "description_status": description_status,
                "palette_status": palette_status,
                "palette_material": palette_material,
                "listings": enriched_listings,
                "direct_offers": direct_offers,
                "direct_match": direct_match,
            }
        if channel_filter and channel_filter in cells:
            statuses = [cells[channel_filter]["status"]]
        else:
            statuses = [cell["status"] for cell in cells.values()]
        for cell_status in statuses:
            totals[cell_status] += 1
        if channel_filter and channel_filter in cells:
            row_description_statuses = [cells[channel_filter]["description_status"]]
        else:
            row_description_statuses = [
                cell["description_status"]
                for cell in cells.values()
                if cell["description_status"] != "not_applicable"
            ]
        for description_status in row_description_statuses:
            if description_status in description_totals:
                description_totals[description_status] += 1
        if channel_filter and channel_filter in cells:
            row_palette_statuses = [cells[channel_filter]["palette_status"]]
        else:
            row_palette_statuses = [
                cell["palette_status"]
                for cell in cells.values()
                if cell["palette_status"] != "not_applicable"
            ]
        for palette_status in row_palette_statuses:
            total_status = "mismatch" if palette_status == "missing" else palette_status
            if total_status in palette_totals:
                palette_totals[total_status] += 1
        if status_filter and status_filter not in statuses:
            continue
        if description_filter and description_filter not in row_description_statuses:
            continue
        if palette_filter:
            filter_statuses = [
                "mismatch" if status == "missing" else status
                for status in row_palette_statuses
            ]
            if palette_filter not in filter_statuses:
                continue
        rows.append({"product": product, "cells": cells})

    rows.sort(
        key=lambda row: (
            str(row["product"].get("name") or "").casefold(),
            row["product"].get("apilo_id") or 0,
        )
    )
    total_count = len(rows)
    total_pages = max(1, math.ceil(total_count / limit))
    page = max(1, min(int(page), total_pages))
    offset = (page - 1) * limit
    return {
        "rows": rows[offset : offset + limit],
        "totals": totals,
        "description_totals": description_totals,
        "palette_totals": palette_totals,
        "total_count": total_count,
        "total_pages": total_pages,
        "page": page,
        "limit": limit,
    }
