import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from apilo import ApiloClient
from db import (
    get_setting,
    init_db,
    record_audit_log,
    update_product_quantity,
)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - runtime dependency guard
    FastMCP = None


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def normalize_base_url(value: str | None) -> str | None:
    if not value:
        return value
    base = value.rstrip("/")
    if base.endswith("/rest"):
        base = base[:-5]
    if base.endswith("/api"):
        base = base[:-4]
    return base


def resolve_db_path() -> str:
    configured = (os.getenv("APILO_DB_PATH") or "").strip()
    if configured:
        path = Path(configured)
        return str(path if path.is_absolute() else BASE_DIR / path)
    docker_mounted_db = BASE_DIR / "data" / "db" / "apilo.sqlite3"
    if docker_mounted_db.exists():
        return str(docker_mounted_db)
    return str(BASE_DIR / "apilo.sqlite3")


def get_config_value(db_path: str, env_key: str, setting_key: str, default: str | None = None):
    env_value = os.getenv(env_key)
    if env_value:
        return env_value
    setting_value = get_setting(db_path, setting_key)
    return setting_value if setting_value is not None else default


def build_client(db_path: str | None = None) -> ApiloClient:
    resolved_db_path = db_path or resolve_db_path()
    init_db(resolved_db_path)
    return ApiloClient(
        base_url=normalize_base_url(
            get_config_value(
                resolved_db_path,
                "APILO_BASE_URL",
                "apilo_base_url",
                "https://api.apilo.com",
            )
        ),
        client_id=get_config_value(resolved_db_path, "APILO_CLIENT_ID", "apilo_client_id"),
        client_secret=get_config_value(
            resolved_db_path,
            "APILO_CLIENT_SECRET",
            "apilo_client_secret",
        ),
        developer_id=os.getenv("APILO_DEVELOPER_ID") or None,
        db_path=resolved_db_path,
        grant_type=os.getenv("APILO_GRANT_TYPE"),
        auth_token=os.getenv("APILO_AUTH_TOKEN"),
    )


def _row_to_dict(row) -> dict[str, Any]:
    if row is None:
        raise ValueError("Pusty rekord produktu.")
    return dict(row)


def _query_products(db_path: str, where_sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM products WHERE present_in_apilo = 1 AND ({where_sql})", params
    ).fetchall()
    conn.close()
    return [_row_to_dict(row) for row in rows]


def _single_product(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    if not rows:
        raise ValueError(f"Nie znalazłam produktu w lokalnej bazie Apilo dla: {label}.")
    if len(rows) > 1:
        names = ", ".join(
            f"#{row['id']} {row['name'] or row['sku'] or row['apilo_id']}" for row in rows[:5]
        )
        raise ValueError(f"Znalazłam kilka produktów dla {label}: {names}. Doprecyzuj identyfikator.")
    return rows[0]


def find_product(
    db_path: str,
    *,
    local_id: int | None = None,
    apilo_id: int | None = None,
    allegro_offer_id: str | None = None,
    sku: str | None = None,
    ean: str | None = None,
    name: str | None = None,
):
    init_db(db_path)
    if local_id is not None:
        return _single_product(_query_products(db_path, "id = ?", (local_id,)), f"local_id={local_id}")
    if apilo_id is not None:
        return _single_product(
            _query_products(db_path, "apilo_id = ?", (apilo_id,)),
            f"apilo_id={apilo_id}",
        )
    if allegro_offer_id:
        return _single_product(
            _query_products(db_path, "allegro_auction_id = ?", (str(allegro_offer_id),)),
            f"allegro_offer_id={allegro_offer_id}",
        )
    if sku:
        return _single_product(_query_products(db_path, "sku = ?", (sku,)), f"sku={sku}")
    if ean:
        return _single_product(_query_products(db_path, "ean = ?", (ean,)), f"ean={ean}")
    if name:
        return _single_product(
            _query_products(db_path, "name LIKE ?", (f"%{name}%",)),
            f"name={name}",
        )
    raise ValueError("Podaj local_id, apilo_id, allegro_offer_id, sku, ean albo name.")


def _update_payload(product: dict[str, Any], quantity: int):
    if product.get("apilo_id") is not None:
        return {"id": int(product["apilo_id"]), "quantity": quantity}
    if product.get("original_code"):
        return {"originalCode": product["original_code"], "quantity": quantity}
    raise ValueError("Produkt nie ma identyfikatora Apilo ani originalCode.")


def _remote_quantity(client, product: dict[str, Any]) -> int:
    if product.get("apilo_id") is not None and hasattr(client, "get_product"):
        remote = client.get_product(int(product["apilo_id"]))
    elif product.get("apilo_id") is not None and hasattr(client, "_request"):
        remote = client._request("GET", f"/rest/api/warehouse/product/{product['apilo_id']}/")
    else:
        raise ValueError("Nie mogę zweryfikować zdalnego stanu bez apilo_id.")
    if not isinstance(remote, dict) or "quantity" not in remote:
        raise RuntimeError("Apilo nie zwróciło pola quantity przy weryfikacji produktu.")
    return int(remote["quantity"])


def count_products(db_path: str, client) -> dict[str, Any]:
    init_db(db_path)
    data = client._request(
        "GET",
        "/rest/api/warehouse/product/",
        params={"limit": 1, "offset": 0},
    )
    if not isinstance(data, dict) or "totalCount" not in data:
        raise RuntimeError("Apilo nie zwróciło totalCount dla listy produktów.")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS product_count,
                COALESCE(SUM(quantity), 0) AS total_quantity,
                SUM(CASE WHEN quantity > 0 THEN 1 ELSE 0 END) AS in_stock_count,
                SUM(CASE WHEN quantity = 0 THEN 1 ELSE 0 END) AS out_of_stock_count,
                SUM(CASE WHEN dirty = 1 THEN 1 ELSE 0 END) AS pending_sync_count
            FROM products
            WHERE present_in_apilo = 1 AND apilo_id IS NOT NULL
            """
        ).fetchone()
    finally:
        conn.close()
    local_product_count = int(row[0] or 0)
    remote_total_count = int(data["totalCount"])
    return {
        "remote_total_count": remote_total_count,
        "local_product_count": local_product_count,
        "local_total_quantity": int(row[1] or 0),
        "local_in_stock_count": int(row[2] or 0),
        "local_out_of_stock_count": int(row[3] or 0),
        "local_pending_sync_count": int(row[4] or 0),
        "consistent": remote_total_count == local_product_count,
    }


def get_product_inventory(
    db_path: str,
    client,
    *,
    local_id: int | None = None,
    apilo_id: int | None = None,
    allegro_offer_id: str | None = None,
    sku: str | None = None,
    ean: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    product = find_product(
        db_path,
        local_id=local_id,
        apilo_id=apilo_id,
        allegro_offer_id=allegro_offer_id,
        sku=sku,
        ean=ean,
        name=name,
    )
    remote_quantity = _remote_quantity(client, product)
    local_quantity = int(product["quantity"] or 0)
    return {
        "local_id": int(product["id"]),
        "apilo_id": product.get("apilo_id"),
        "sku": product.get("sku"),
        "ean": product.get("ean"),
        "name": product.get("name"),
        "allegro_offer_id": product.get("allegro_auction_id"),
        "remote_quantity": remote_quantity,
        "local_quantity": local_quantity,
        "last_synced_quantity": int(product["last_synced_quantity"] or 0),
        "pending_sync": bool(product.get("dirty")),
        "quantity_consistent": remote_quantity == local_quantity,
        "source": "remote_apilo_api",
    }


def adjust_stock_quantity(
    db_path: str,
    client,
    *,
    local_id: int | None = None,
    apilo_id: int | None = None,
    allegro_offer_id: str | None = None,
    sku: str | None = None,
    ean: str | None = None,
    name: str | None = None,
    quantity: int | None = None,
    delta: int | None = None,
    reason: str = "",
):
    if (quantity is None) == (delta is None):
        raise ValueError("Podaj dokładnie jedno: quantity albo delta.")
    product = find_product(
        db_path,
        local_id=local_id,
        apilo_id=apilo_id,
        allegro_offer_id=allegro_offer_id,
        sku=sku,
        ean=ean,
        name=name,
    )
    before = int(product["quantity"] or 0)
    after = int(quantity if quantity is not None else before + int(delta))
    if after < 0:
        raise ValueError("Stan magazynowy nie może być ujemny.")

    payload = _update_payload(product, after)
    client.update_quantities([payload])
    verified_quantity = _remote_quantity(client, product)
    if verified_quantity != after:
        raise RuntimeError(
            f"Weryfikacja Apilo nie przeszła: oczekiwano {after}, zdalnie jest {verified_quantity}."
        )

    update_product_quantity(db_path, int(product["id"]), after)
    record_audit_log(
        db_path,
        "mcp_product_quantity_update",
        "product",
        entity_id=product["id"],
        entity_label=product.get("name") or product.get("sku") or str(product.get("apilo_id")),
        old_value=str(before),
        new_value=str(after),
        details={
            "reason": reason,
            "delta": after - before,
            "apilo_id": product.get("apilo_id"),
            "sku": product.get("sku") or "",
            "ean": product.get("ean") or "",
            "allegro_offer_id": product.get("allegro_auction_id") or "",
        },
        actor_ip="mcp",
    )
    return {
        "local_id": int(product["id"]),
        "apilo_id": product.get("apilo_id"),
        "sku": product.get("sku"),
        "ean": product.get("ean"),
        "name": product.get("name"),
        "allegro_offer_id": product.get("allegro_auction_id"),
        "before_quantity": before,
        "after_quantity": after,
        "delta": after - before,
        "verified": True,
        "reason": reason,
    }


def apply_return_stock_corrections(
    db_path: str,
    client,
    *,
    items: list[dict[str, Any]],
    confirmed_received: bool,
    reference: str = "",
):
    if not confirmed_received:
        raise ValueError("confirmed_received musi być true przed przyjęciem zwrotu na stan.")
    updates = []
    for item in items:
        returned_quantity = int(item.get("quantity") or item.get("returned_quantity") or 0)
        if returned_quantity <= 0:
            raise ValueError("Każda pozycja zwrotu musi mieć quantity większe od 0.")
        reason = item.get("reason") or f"zwrot {reference}".strip()
        updates.append(
            adjust_stock_quantity(
                db_path,
                client,
                local_id=item.get("local_id"),
                apilo_id=item.get("apilo_id"),
                allegro_offer_id=item.get("allegro_offer_id") or item.get("offer_id"),
                sku=item.get("sku"),
                ean=item.get("ean"),
                name=item.get("name"),
                delta=returned_quantity,
                reason=reason,
            )
        )
    return {"updated_count": len(updates), "updates": updates, "reference": reference}


def _json_result(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


if FastMCP is not None:
    mcp = FastMCP("apilo")

    @mcp.tool()
    def apilo_health() -> str:
        """Sprawdza konfigurację lokalnej bazy i połączenie z API Apilo bez ujawniania sekretów."""
        db_path = resolve_db_path()
        init_db(db_path)
        client = build_client(db_path)
        client.test_connection()
        return _json_result({"ok": True, "db_path": db_path})

    @mcp.tool()
    def apilo_count_products() -> str:
        """Liczy produkty w Apilo przez API i porównuje z lokalnym cache, bez wykonywania zmian stanów."""
        db_path = resolve_db_path()
        return _json_result(count_products(db_path, build_client(db_path)))

    @mcp.tool()
    def apilo_find_product(
        local_id: int | None = None,
        apilo_id: int | None = None,
        allegro_offer_id: str | None = None,
        sku: str | None = None,
        ean: str | None = None,
        name: str | None = None,
    ) -> str:
        """Znajduje jeden produkt w lokalnym cache Apilo po ID Apilo, ofercie Allegro, SKU, EAN albo nazwie."""
        return _json_result(
            find_product(
                resolve_db_path(),
                local_id=local_id,
                apilo_id=apilo_id,
                allegro_offer_id=allegro_offer_id,
                sku=sku,
                ean=ean,
                name=name,
            )
        )

    @mcp.tool()
    def apilo_get_product_inventory(
        local_id: int | None = None,
        apilo_id: int | None = None,
        allegro_offer_id: str | None = None,
        sku: str | None = None,
        ean: str | None = None,
        name: str | None = None,
    ) -> str:
        """Zwraca aktualny zdalny stan produktu z API Apilo; lokalny cache służy tylko do dopasowania produktu."""
        db_path = resolve_db_path()
        return _json_result(
            get_product_inventory(
                db_path,
                build_client(db_path),
                local_id=local_id,
                apilo_id=apilo_id,
                allegro_offer_id=allegro_offer_id,
                sku=sku,
                ean=ean,
                name=name,
            )
        )

    @mcp.tool()
    def apilo_set_stock_quantity(
        quantity: int,
        local_id: int | None = None,
        apilo_id: int | None = None,
        allegro_offer_id: str | None = None,
        sku: str | None = None,
        ean: str | None = None,
        name: str | None = None,
        reason: str = "manualna korekta MCP",
    ) -> str:
        """Ustawia konkretny stan produktu w Apilo, aktualizuje lokalny cache i weryfikuje zdalny wynik."""
        db_path = resolve_db_path()
        return _json_result(
            adjust_stock_quantity(
                db_path,
                build_client(db_path),
                local_id=local_id,
                apilo_id=apilo_id,
                allegro_offer_id=allegro_offer_id,
                sku=sku,
                ean=ean,
                name=name,
                quantity=quantity,
                reason=reason,
            )
        )

    @mcp.tool()
    def apilo_adjust_stock_quantity(
        delta: int,
        local_id: int | None = None,
        apilo_id: int | None = None,
        allegro_offer_id: str | None = None,
        sku: str | None = None,
        ean: str | None = None,
        name: str | None = None,
        reason: str = "korekta MCP",
    ) -> str:
        """Zmienia stan produktu o delta, np. +1 po zwrocie albo -1 po korekcie, z weryfikacją w Apilo."""
        db_path = resolve_db_path()
        return _json_result(
            adjust_stock_quantity(
                db_path,
                build_client(db_path),
                local_id=local_id,
                apilo_id=apilo_id,
                allegro_offer_id=allegro_offer_id,
                sku=sku,
                ean=ean,
                name=name,
                delta=delta,
                reason=reason,
            )
        )

    @mcp.tool()
    def apilo_apply_return_stock_corrections(
        items: list[dict[str, Any]],
        confirmed_received: bool,
        reference: str = "",
    ) -> str:
        """Przyjmuje fizycznie otrzymany zwrot na stan: dopasowuje po ofercie/SKU/EAN/ID, zwiększa ilości i weryfikuje w Apilo."""
        db_path = resolve_db_path()
        return _json_result(
            apply_return_stock_corrections(
                db_path,
                build_client(db_path),
                items=items,
                confirmed_received=confirmed_received,
                reference=reference,
            )
        )
else:  # pragma: no cover
    mcp = None


def main():
    if mcp is None:
        raise RuntimeError("Brak pakietu mcp. Zainstaluj zależność: pip install mcp")
    mcp.run()


if __name__ == "__main__":
    main()
