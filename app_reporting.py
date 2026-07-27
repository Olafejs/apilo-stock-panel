from datetime import datetime, timedelta, timezone

from app_utils import parse_int_value
from db import get_ean_name_map, get_product_maps


def normalize_sales_report_days(value, default=30):
    try:
        days = int(value or default)
    except (TypeError, ValueError):
        days = default
    if days < 1 or days > 365:
        return default
    return days


def get_realized_order_status_ids(client):
    try:
        status_map = client.get_order_status_map()
    except Exception:
        return set()
    realized_ids = set()
    for item in status_map:
        haystack = " ".join(
            str(item.get(key) or "")
            for key in ("key", "name", "description")
        ).lower()
        if "zrealiz" in haystack or "complete" in haystack:
            status_id = item.get("id")
            if status_id is not None:
                realized_ids.add(status_id)
    return realized_ids


def extract_order_day(order):
    for key in ("orderedAt", "createdAt", "updatedAt"):
        value = order.get(key)
        if value:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def pick_external_order_id(order):
    candidates = (
        "externalId",
        "externalOrderId",
        "channelOrderId",
        "orderId",
        "marketplaceOrderId",
        "id",
    )
    for key in candidates:
        value = order.get(key)
        if isinstance(value, dict):
            value = value.get("id") or value.get("value")
        if value:
            return str(value)
    return ""


def get_sales_totals(db_path, client, days, realized_only=True):
    now = datetime.now(timezone.utc)
    ordered_after = (now - timedelta(days=days)).isoformat()
    orders = client.list_orders(ordered_after=ordered_after, payment_status=2)
    orders_total = len(orders)
    realized_filter = False
    if realized_only:
        realized_status_ids = get_realized_order_status_ids(client)
        if realized_status_ids:
            realized_filter = True
            orders = [
                order
                for order in orders
                if parse_int_value(order.get("status"), -1) in realized_status_ids
            ]
    by_apilo_id, by_original_code, by_sku = get_product_maps(db_path)
    totals = {}
    details_map = {}
    for order in orders:
        day_key = extract_order_day(order)
        order_id = order.get("id")
        external_order_id = pick_external_order_id(order)
        for item in order.get("orderItems", []):
            ean = item.get("ean")
            if not ean:
                product_id = item.get("productId")
                if product_id is not None:
                    ean = by_apilo_id.get(str(product_id))
            if not ean:
                original_code = item.get("originalCode")
                if original_code:
                    ean = by_original_code.get(original_code)
            if not ean:
                sku = item.get("sku")
                if sku:
                    ean = by_sku.get(sku)
            qty = item.get("quantity") or 0
            if not ean:
                continue
            totals[ean] = totals.get(ean, 0) + qty
            if day_key and order_id:
                details_map.setdefault(ean, {})
                entry = details_map[ean].setdefault(
                    order_id,
                    {
                        "date": day_key,
                        "qty": 0,
                        "order_id": order_id,
                        "allegro_id": external_order_id,
                    },
                )
                entry["qty"] += qty
    meta = {
        "orders_total": orders_total,
        "orders_used": len(orders),
        "realized_filter": realized_filter,
    }
    details_list = {}
    for ean, orders_map in details_map.items():
        items = list(orders_map.values())
        items.sort(key=lambda item: item["date"], reverse=True)
        details_list[ean] = items
    return totals, meta, details_list


def build_sales_report_rows(db_path, totals):
    ean_name_map = get_ean_name_map(db_path)
    rows = [
        {
            "ean": ean,
            "name": ean_name_map.get(ean, ""),
            "quantity": qty,
        }
        for ean, qty in totals.items()
    ]
    rows.sort(key=lambda row: row["quantity"], reverse=True)
    return rows


def build_sales_report_csv(rows):
    output = ["EAN;Nazwa;Sprzedane"]
    for row in rows:
        name = (row["name"] or "").replace(";", ",")
        output.append(f"{row['ean']};{name};{row['quantity']}")
    return "\n".join(output)
