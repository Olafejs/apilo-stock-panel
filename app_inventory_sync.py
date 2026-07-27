from product_attributes import description_to_text, parse_material_color


def build_product_lookup_maps(products):
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


def build_auction_metadata(products, platforms, auctions):
    allegro_ids = {
        platform.get("id")
        for platform in platforms
        if (platform.get("name") or "").lower() == "allegro"
        or (platform.get("alias") or "").upper() == "AL"
    }
    by_apilo_id, by_sku, by_ean = build_product_lookup_maps(products)
    auction_map = {}
    attributes_map = {}
    for auction in auctions:
        platform_account = auction.get("platformAccount") or {}
        if allegro_ids and platform_account.get("id") not in allegro_ids:
            continue
        auction_id = auction.get("idExternal")
        if not auction_id or not str(auction_id).isdigit():
            continue
        description_text = description_to_text(
            auction.get("description")
            or auction.get("longDescription")
            or auction.get("shortDescription")
            or auction.get("opis")
        )
        parsed_attributes = parse_material_color(description_text)
        for auction_product in auction.get("auctionProducts", []):
            product_info = auction_product.get("product") or {}
            product_id = product_info.get("id") or auction_product.get("productId")
            mapped_id = by_apilo_id.get(str(product_id)) if product_id is not None else None
            if mapped_id is None:
                sku = auction_product.get("sku") or product_info.get("sku")
                mapped_id = by_sku.get(str(sku)) if sku else None
            if mapped_id is None:
                ean = auction_product.get("ean") or product_info.get("ean")
                mapped_id = by_ean.get(str(ean)) if ean else None
            if mapped_id is None:
                continue
            auction_map[mapped_id] = str(auction_id)
            if parsed_attributes["material"] or parsed_attributes["color"]:
                attributes_map[mapped_id] = {
                    "material": parsed_attributes["material"],
                    "color": parsed_attributes["color"],
                    "source": "apilo_auction_description",
                }
    return auction_map, attributes_map


def build_allegro_price_map(products, prices, compute_price):
    base_price_map = {
        int(product["id"]): product.get("priceWithTax")
        for product in products
        if product.get("id") is not None
    }
    price_map = {}
    for item in prices:
        product_id = item.get("product")
        if product_id is None:
            continue
        product_id = int(product_id)
        computed = compute_price(item, base_price_map.get(product_id), markup_pct=19.0)
        if computed is not None:
            price_map[product_id] = computed
    return price_map
