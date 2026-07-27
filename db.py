import os
import json
import sqlite3
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_VALUE_PREFIX = "enc:v1:"
SECRET_SETTING_KEYS = {
    "apilo_client_secret",
    "empik_api_key",
    "flask_secret_key",
    "smtp_password",
}
SECRET_TOKEN_COLUMNS = ("access_token", "refresh_token")
SECRET_CIPHER_CACHE = {}


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _default_secret_key_path(db_path):
    override = (os.getenv("SETTINGS_ENCRYPTION_KEY_PATH") or "").strip()
    if override:
        return override
    db_dir = os.path.dirname(os.path.abspath(db_path))
    return os.path.join(db_dir, "settings.key")


def _load_secret_key_material(db_path):
    env_key = (os.getenv("SETTINGS_ENCRYPTION_KEY") or "").strip()
    if env_key:
        return env_key.encode("utf-8"), "env", ""

    key_path = _default_secret_key_path(db_path)
    key_dir = os.path.dirname(os.path.abspath(key_path))
    if key_dir:
        os.makedirs(key_dir, exist_ok=True)
    if not os.path.exists(key_path):
        key_bytes = Fernet.generate_key()
        with open(key_path, "wb") as handle:
            handle.write(key_bytes)
        os.chmod(key_path, 0o600)
        return key_bytes, "file", key_path
    with open(key_path, "rb") as handle:
        return handle.read().strip(), "file", key_path


def _get_secret_cipher_state(db_path):
    cache_key = (
        os.path.abspath(db_path),
        os.getenv("SETTINGS_ENCRYPTION_KEY", ""),
        os.getenv("SETTINGS_ENCRYPTION_KEY_PATH", ""),
    )
    cached = SECRET_CIPHER_CACHE.get(cache_key)
    if cached:
        return cached
    key_bytes, backend, key_path = _load_secret_key_material(db_path)
    try:
        cipher = Fernet(key_bytes)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Nieprawidłowy klucz szyfrowania settings.") from exc
    state = {
        "cipher": cipher,
        "backend": backend,
        "key_path": key_path,
    }
    SECRET_CIPHER_CACHE[cache_key] = state
    return state


def get_secret_storage_status(db_path):
    state = _get_secret_cipher_state(db_path)
    return {
        "enabled": True,
        "backend": state["backend"],
        "key_path": state["key_path"],
    }


def _encrypt_secret_value(db_path, value):
    if value is None or value == "":
        return value
    raw_value = str(value)
    if raw_value.startswith(ENCRYPTED_VALUE_PREFIX):
        return raw_value
    cipher = _get_secret_cipher_state(db_path)["cipher"]
    token = cipher.encrypt(raw_value.encode("utf-8")).decode("utf-8")
    return f"{ENCRYPTED_VALUE_PREFIX}{token}"


def _decrypt_secret_value(db_path, value, *, context):
    if value is None or value == "":
        return value
    raw_value = str(value)
    if not raw_value.startswith(ENCRYPTED_VALUE_PREFIX):
        return raw_value
    cipher = _get_secret_cipher_state(db_path)["cipher"]
    try:
        decrypted = cipher.decrypt(raw_value[len(ENCRYPTED_VALUE_PREFIX) :].encode("utf-8"))
    except InvalidToken as exc:
        raise RuntimeError(f"Nie można odszyfrować sekretu: {context}.") from exc
    return decrypted.decode("utf-8")


def get_db(db_path):
    if db_path != ":memory:":
        db_dir = os.path.dirname(os.path.abspath(db_path))
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db(db_path):
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apilo_id INTEGER UNIQUE,
                original_code TEXT,
                sku TEXT,
                ean TEXT,
                image_url TEXT,
                name TEXT,
                price_with_tax TEXT,
                price_without_tax TEXT,
                allegro_price_with_tax TEXT,
                allegro_auction_id TEXT,
                material TEXT,
                color TEXT,
                attributes_source TEXT,
                attributes_updated_at TEXT,
                quantity INTEGER,
                status INTEGER,
                last_synced_quantity INTEGER,
                dirty INTEGER DEFAULT 0,
                present_in_apilo INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT,
                last_synced_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                access_token TEXT,
                access_token_expires_at TEXT,
                refresh_token TEXT,
                refresh_token_expires_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_cache (
                ean TEXT PRIMARY KEY,
                quantity_30d INTEGER,
                daily_json TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_cache_year (
                ean TEXT PRIMARY KEY,
                quantity_year INTEGER,
                quantity_year_adjusted INTEGER,
                orders_year INTEGER,
                outlier_order_qty INTEGER,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                entity_label TEXT,
                old_value TEXT,
                new_value TEXT,
                details_json TEXT,
                actor_ip TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_channels (
                channel_key TEXT PRIMARY KEY,
                channel_name TEXT NOT NULL,
                platform_id INTEGER,
                alias TEXT,
                sort_order INTEGER NOT NULL DEFAULT 100,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apilo_product_id INTEGER NOT NULL,
                channel_key TEXT NOT NULL,
                apilo_auction_id INTEGER,
                external_id TEXT,
                status INTEGER,
                listing_name TEXT,
                offer_price REAL,
                offer_quantity INTEGER,
                updated_at TEXT NOT NULL,
                UNIQUE(apilo_product_id, channel_key, apilo_auction_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS apilo_description_references (
                apilo_product_id INTEGER PRIMARY KEY,
                ean TEXT,
                sku TEXT,
                description_html TEXT NOT NULL DEFAULT '',
                description_preview TEXT NOT NULL DEFAULT '',
                description_text TEXT NOT NULL,
                description_hash TEXT NOT NULL,
                export_price REAL,
                export_quantity INTEGER,
                source_name TEXT NOT NULL,
                imported_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_description_checks (
                apilo_product_id INTEGER NOT NULL,
                channel_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                reference_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                detail TEXT,
                actual_description_text TEXT NOT NULL DEFAULT '',
                palette_status TEXT NOT NULL DEFAULT 'unverified',
                palette_material TEXT NOT NULL DEFAULT '',
                palette_block_text TEXT NOT NULL DEFAULT '',
                palette_block_hash TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL,
                PRIMARY KEY (apilo_product_id, channel_key, external_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS empik_offers (
                offer_id TEXT PRIMARY KEY,
                shop_sku TEXT,
                product_sku TEXT,
                active INTEGER NOT NULL DEFAULT 0,
                state_code TEXT,
                quantity INTEGER,
                price REAL,
                product_title TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS empik_offer_imports (
                import_id TEXT PRIMARY KEY,
                date_created TEXT,
                status TEXT,
                reason_status TEXT,
                has_error_report INTEGER NOT NULL DEFAULT 0,
                lines_read INTEGER NOT NULL DEFAULT 0,
                lines_in_success INTEGER NOT NULL DEFAULT 0,
                lines_in_error INTEGER NOT NULL DEFAULT 0,
                lines_in_pending INTEGER NOT NULL DEFAULT 0,
                offer_inserted INTEGER NOT NULL DEFAULT 0,
                offer_updated INTEGER NOT NULL DEFAULT 0,
                offer_deleted INTEGER NOT NULL DEFAULT 0,
                origin TEXT,
                mode TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_apilo_id ON products(apilo_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_original_code ON products(original_code)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_created ON login_attempts(ip_address, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_listings_product ON channel_listings(apilo_product_id, channel_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel_listings_status ON channel_listings(channel_key, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_empik_offers_shop_sku ON empik_offers(shop_sku)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_empik_offers_product_sku ON empik_offers(product_sku)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_empik_imports_date ON empik_offer_imports(date_created DESC)"
        )
    _ensure_column(db_path, "products", "ean", "TEXT")
    _ensure_column(db_path, "products", "image_url", "TEXT")
    _ensure_column(db_path, "products", "price_with_tax", "TEXT")
    _ensure_column(db_path, "products", "price_without_tax", "TEXT")
    _ensure_column(db_path, "products", "allegro_price_with_tax", "TEXT")
    _ensure_column(db_path, "products", "allegro_auction_id", "TEXT")
    _ensure_column(db_path, "products", "material", "TEXT")
    _ensure_column(db_path, "products", "color", "TEXT")
    _ensure_column(db_path, "products", "attributes_source", "TEXT")
    _ensure_column(db_path, "products", "attributes_updated_at", "TEXT")
    _ensure_column(db_path, "products", "present_in_apilo", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(db_path, "channel_listings", "offer_price", "REAL")
    _ensure_column(db_path, "channel_listings", "offer_quantity", "INTEGER")
    _ensure_column(
        db_path,
        "apilo_description_references",
        "description_html",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        db_path,
        "apilo_description_references",
        "description_preview",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        db_path,
        "channel_description_checks",
        "actual_description_text",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        db_path,
        "channel_description_checks",
        "palette_status",
        "TEXT NOT NULL DEFAULT 'unverified'",
    )
    _ensure_column(
        db_path,
        "channel_description_checks",
        "palette_material",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        db_path,
        "channel_description_checks",
        "palette_block_text",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        db_path,
        "channel_description_checks",
        "palette_block_hash",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(db_path, "sales_cache", "daily_json", "TEXT")
    _ensure_column(db_path, "sales_cache_year", "quantity_year_adjusted", "INTEGER")
    _ensure_column(db_path, "sales_cache_year", "outlier_order_qty", "INTEGER")
    _ensure_price_columns_real(db_path)
    with conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_present_apilo ON products(present_in_apilo, apilo_id)"
        )
    conn.close()


def get_tokens(db_path):
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM tokens WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    for column in SECRET_TOKEN_COLUMNS:
        result[column] = _decrypt_secret_value(db_path, result.get(column), context=f"tokens.{column}")
    return result


def save_tokens(db_path, tokens):
    now = utc_now_iso()
    access_token = _encrypt_secret_value(db_path, tokens.get("access_token"))
    refresh_token = _encrypt_secret_value(db_path, tokens.get("refresh_token"))
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO tokens (
                id,
                access_token,
                access_token_expires_at,
                refresh_token,
                refresh_token_expires_at,
                updated_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                access_token = excluded.access_token,
                access_token_expires_at = excluded.access_token_expires_at,
                refresh_token = excluded.refresh_token,
                refresh_token_expires_at = excluded.refresh_token_expires_at,
                updated_at = excluded.updated_at
            """,
            (
                access_token,
                tokens.get("access_token_expires_at"),
                refresh_token,
                tokens.get("refresh_token_expires_at"),
                now,
            ),
        )
    conn.close()


def _ensure_column(db_path, table, column, column_def):
    conn = get_db(db_path)
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not any(row["name"] == column for row in columns):
        with conn:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")
    conn.close()


def _ensure_price_columns_real(db_path):
    conn = get_db(db_path)
    columns = conn.execute("PRAGMA table_info(products)").fetchall()
    types = {row["name"]: (row["type"] or "").upper() for row in columns}
    targets = ("price_with_tax", "price_without_tax", "allegro_price_with_tax")
    if all(types.get(name) == "REAL" for name in targets):
        conn.close()
        return
    existing = {row["name"] for row in columns}

    def col_expr(name):
        if name not in existing:
            return "NULL"
        if name in targets:
            return f"CAST({name} AS REAL)"
        return name

    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apilo_id INTEGER UNIQUE,
                original_code TEXT,
                sku TEXT,
                ean TEXT,
                image_url TEXT,
                name TEXT,
                price_with_tax REAL,
                price_without_tax REAL,
                allegro_price_with_tax REAL,
                allegro_auction_id TEXT,
                material TEXT,
                color TEXT,
                attributes_source TEXT,
                attributes_updated_at TEXT,
                quantity INTEGER,
                status INTEGER,
                last_synced_quantity INTEGER,
                dirty INTEGER DEFAULT 0,
                present_in_apilo INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT,
                last_synced_at TEXT
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO products_new (
                id,
                apilo_id,
                original_code,
                sku,
                ean,
                image_url,
                name,
                price_with_tax,
                price_without_tax,
                allegro_price_with_tax,
                allegro_auction_id,
                material,
                color,
                attributes_source,
                attributes_updated_at,
                quantity,
                status,
                last_synced_quantity,
                dirty,
                present_in_apilo,
                updated_at,
                last_synced_at
            )
            SELECT
                {col_expr("id")},
                {col_expr("apilo_id")},
                {col_expr("original_code")},
                {col_expr("sku")},
                {col_expr("ean")},
                {col_expr("image_url")},
                {col_expr("name")},
                {col_expr("price_with_tax")},
                {col_expr("price_without_tax")},
                {col_expr("allegro_price_with_tax")},
                {col_expr("allegro_auction_id")},
                {col_expr("material")},
                {col_expr("color")},
                {col_expr("attributes_source")},
                {col_expr("attributes_updated_at")},
                {col_expr("quantity")},
                {col_expr("status")},
                {col_expr("last_synced_quantity")},
                {col_expr("dirty")},
                {col_expr("present_in_apilo")},
                {col_expr("updated_at")},
                {col_expr("last_synced_at")}
            FROM products
            """
        )
        conn.execute("DROP TABLE products")
        conn.execute("ALTER TABLE products_new RENAME TO products")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_name ON products(name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_ean ON products(ean)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_apilo_id ON products(apilo_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_present_apilo ON products(present_in_apilo, apilo_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_original_code ON products(original_code)"
        )
    conn.close()


def upsert_product_from_apilo(db_path, product):
    now = utc_now_iso()
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO products (
                apilo_id,
                original_code,
                sku,
                ean,
                image_url,
                name,
                price_with_tax,
                price_without_tax,
                allegro_price_with_tax,
                allegro_auction_id,
                quantity,
                status,
                last_synced_quantity,
                dirty,
                present_in_apilo,
                updated_at,
                last_synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apilo_id) DO UPDATE SET
                sku = excluded.sku,
                ean = excluded.ean,
                name = excluded.name,
                original_code = excluded.original_code,
                price_with_tax = excluded.price_with_tax,
                price_without_tax = excluded.price_without_tax,
                status = excluded.status,
                quantity = excluded.quantity,
                last_synced_quantity = excluded.last_synced_quantity,
                dirty = 0,
                present_in_apilo = 1,
                updated_at = excluded.updated_at,
                last_synced_at = excluded.last_synced_at
            """,
            (
                product.get("id"),
                product.get("originalCode"),
                product.get("sku"),
                product.get("ean"),
                product.get("image_url"),
                product.get("name"),
                product.get("priceWithTax"),
                product.get("priceWithoutTax"),
                None,
                None,
                product.get("quantity"),
                product.get("status"),
                product.get("quantity"),
                0,
                1,
                now,
                now,
            ),
        )
    conn.close()


def _validated_product_snapshot(products):
    if not isinstance(products, list):
        raise ValueError("Snapshot produktów musi być listą.")
    normalized = []
    seen_ids = set()
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("Nieprawidłowy rekord produktu w snapshotcie.")
        raw_product_id = product.get("id")
        if raw_product_id is None:
            raise ValueError("Produkt w snapshotcie nie ma poprawnego identyfikatora Apilo.")
        try:
            product_id = int(raw_product_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Produkt w snapshotcie nie ma poprawnego identyfikatora Apilo."
            ) from exc
        if product_id <= 0:
            raise ValueError("Produkt w snapshotcie nie ma poprawnego identyfikatora Apilo.")
        if product_id in seen_ids:
            raise ValueError(f"Powielony identyfikator Apilo w snapshotcie: {product_id}.")
        seen_ids.add(product_id)
        normalized.append({**product, "id": product_id})
    return normalized


def apply_product_snapshot(
    db_path,
    products,
    *,
    image_map,
    auction_map,
    attributes_map,
    price_map,
    replace_auction_data,
    channel_listings=None,
    sales_channels=None,
    synced_at=None,
):
    """Atomowo zastępuje aktywny snapshot po pełnym odczycie danych z Apilo."""
    normalized = _validated_product_snapshot(products)
    remote_ids = {product["id"] for product in normalized}
    synced_at = synced_at or utc_now_iso()
    conn = get_db(db_path)
    try:
        previous_images = {
            int(row["apilo_id"]): row["image_url"]
            for row in conn.execute(
                """
                SELECT apilo_id, image_url
                FROM products
                WHERE present_in_apilo = 1 AND apilo_id IS NOT NULL
                """
            ).fetchall()
        }
        previous_active_ids = set(previous_images)
        if previous_active_ids and not remote_ids:
            raise ValueError(
                "Pusty snapshot Apilo został odrzucony, aby nie ukryć całego magazynu."
            )
        with conn:
            conn.execute("UPDATE products SET present_in_apilo = 0 WHERE present_in_apilo = 1")
            conn.executemany(
                """
                INSERT INTO products (
                    apilo_id, original_code, sku, ean, name,
                    price_with_tax, price_without_tax, quantity, status,
                    last_synced_quantity, dirty, present_in_apilo,
                    updated_at, last_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                ON CONFLICT(apilo_id) DO UPDATE SET
                    original_code = excluded.original_code,
                    sku = excluded.sku,
                    ean = excluded.ean,
                    name = excluded.name,
                    price_with_tax = excluded.price_with_tax,
                    price_without_tax = excluded.price_without_tax,
                    quantity = excluded.quantity,
                    status = excluded.status,
                    last_synced_quantity = excluded.last_synced_quantity,
                    dirty = 0,
                    present_in_apilo = 1,
                    updated_at = excluded.updated_at,
                    last_synced_at = excluded.last_synced_at
                """,
                [
                    (
                        product["id"],
                        product.get("originalCode"),
                        product.get("sku"),
                        product.get("ean"),
                        product.get("name"),
                        product.get("priceWithTax"),
                        product.get("priceWithoutTax"),
                        product.get("quantity"),
                        product.get("status"),
                        product.get("quantity"),
                        synced_at,
                        synced_at,
                    )
                    for product in normalized
                ],
            )

            conn.execute("UPDATE products SET image_url = NULL WHERE present_in_apilo = 1")
            conn.executemany(
                """
                UPDATE products SET image_url = ?
                WHERE apilo_id = ? AND present_in_apilo = 1
                """,
                [
                    (url, int(product_id))
                    for product_id, url in image_map.items()
                    if int(product_id) in remote_ids
                ],
            )

            conn.execute(
                "UPDATE products SET allegro_price_with_tax = NULL WHERE present_in_apilo = 1"
            )
            conn.executemany(
                """
                UPDATE products SET allegro_price_with_tax = ?
                WHERE apilo_id = ? AND present_in_apilo = 1
                """,
                [
                    (value, int(product_id))
                    for product_id, value in price_map.items()
                    if int(product_id) in remote_ids
                ],
            )

            if replace_auction_data:
                conn.execute(
                    "UPDATE products SET allegro_auction_id = NULL WHERE present_in_apilo = 1"
                )
                conn.executemany(
                    """
                    UPDATE products SET allegro_auction_id = ?
                    WHERE apilo_id = ? AND present_in_apilo = 1
                    """,
                    [
                        (value, int(product_id))
                        for product_id, value in auction_map.items()
                        if int(product_id) in remote_ids
                    ],
                )
                conn.executemany(
                    """
                    UPDATE products
                    SET material = ?, color = ?, attributes_source = ?, attributes_updated_at = ?
                    WHERE apilo_id = ?
                      AND present_in_apilo = 1
                      AND COALESCE(attributes_source, '') != 'manual_user_hint'
                    """,
                    [
                        (
                            (values.get("material") or "").strip(),
                            (values.get("color") or "").strip(),
                            (values.get("source") or "").strip(),
                            synced_at,
                            int(product_id),
                        )
                        for product_id, values in attributes_map.items()
                        if int(product_id) in remote_ids
                        and (values.get("material") or values.get("color"))
                    ],
                )

                if channel_listings is not None and sales_channels is not None:
                    conn.execute("DELETE FROM channel_listings")
                    conn.execute("DELETE FROM sales_channels")
                    conn.executemany(
                        """
                        INSERT INTO sales_channels (
                            channel_key, channel_name, platform_id, alias, sort_order, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                channel["channel_key"],
                                channel["channel_name"],
                                channel.get("platform_id"),
                                channel.get("alias"),
                                channel.get("sort_order", 100),
                                synced_at,
                            )
                            for channel in sales_channels
                        ],
                    )
                    conn.executemany(
                        """
                        INSERT INTO channel_listings (
                            apilo_product_id, channel_key, apilo_auction_id,
                            external_id, status, listing_name, offer_price,
                            offer_quantity, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                int(listing["apilo_product_id"]),
                                listing["channel_key"],
                                listing.get("apilo_auction_id"),
                                listing.get("external_id"),
                                listing.get("status"),
                                listing.get("listing_name"),
                                listing.get("offer_price"),
                                listing.get("offer_quantity"),
                                synced_at,
                            )
                            for listing in channel_listings
                            if int(listing["apilo_product_id"]) in remote_ids
                        ],
                    )

            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES ('last_pull_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (synced_at, synced_at),
            )
    finally:
        conn.close()

    changed_image_ids = sorted(
        int(product_id)
        for product_id, image_url in image_map.items()
        if int(product_id) in remote_ids
        and previous_images.get(int(product_id)) != image_url
    )
    return {
        "active_count": len(remote_ids),
        "deactivated_count": len(previous_active_ids - remote_ids),
        "changed_image_ids": changed_image_ids,
    }


def get_sales_channels(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT channel_key, channel_name, platform_id, alias, sort_order, updated_at
        FROM sales_channels
        ORDER BY sort_order ASC, channel_name COLLATE NOCASE ASC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_channel_listings(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT apilo_product_id, channel_key, apilo_auction_id,
               external_id, status, listing_name, offer_price,
               offer_quantity, updated_at
        FROM channel_listings
        ORDER BY apilo_product_id ASC, channel_key ASC, apilo_auction_id ASC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def replace_apilo_description_references(
    db_path, records, *, source_name, imported_at=None
):
    imported_at = imported_at or utc_now_iso()
    normalized = []
    seen = set()
    for record in records:
        product_id = int(record["apilo_product_id"])
        description_text = str(record.get("description_text") or "").strip()
        description_html = str(record.get("description_html") or description_text).strip()
        description_preview = str(record.get("description_preview") or description_text).strip()
        description_hash = str(record.get("description_hash") or "").strip()
        if (
            product_id < 1
            or product_id in seen
            or not description_text
            or not description_preview
            or not description_hash
            or len(description_html) > 65_535
        ):
            raise ValueError("Nieprawidłowy rekord opisu referencyjnego Apilo.")
        seen.add(product_id)
        normalized.append(
            (
                product_id,
                str(record.get("ean") or "").strip(),
                str(record.get("sku") or "").strip(),
                description_html,
                description_preview,
                description_text,
                description_hash,
                record.get("export_price"),
                record.get("export_quantity"),
                str(source_name or "eksport-apilo.xlsx")[:200],
                imported_at,
            )
        )
    if not normalized:
        raise ValueError("Eksport Apilo nie zawiera opisów referencyjnych.")
    conn = get_db(db_path)
    try:
        active_ids = {
            int(row["apilo_id"])
            for row in conn.execute(
                "SELECT apilo_id FROM products WHERE present_in_apilo = 1"
            ).fetchall()
            if row["apilo_id"] is not None
        }
        if seen != active_ids:
            raise ValueError(
                "Eksport musi zawierać dokładnie wszystkie aktywne produkty Apilo."
            )
        with conn:
            conn.execute("DELETE FROM apilo_description_references")
            conn.executemany(
                """
                INSERT INTO apilo_description_references (
                    apilo_product_id, ean, sku, description_html,
                    description_preview, description_text,
                    description_hash, export_price, export_quantity,
                    source_name, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
            conn.execute("DELETE FROM channel_description_checks")
    finally:
        conn.close()
    return len(normalized)


def get_apilo_description_references(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT apilo_product_id, ean, sku, description_text, description_hash,
               export_price, export_quantity, source_name, imported_at
        FROM apilo_description_references
        ORDER BY apilo_product_id
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_apilo_description_reference(db_path, apilo_product_id):
    conn = get_db(db_path)
    row = conn.execute(
        """
        SELECT apilo_product_id, ean, sku, description_html, description_preview,
               description_text, description_hash, source_name, imported_at
        FROM apilo_description_references
        WHERE apilo_product_id = ?
        """,
        (int(apilo_product_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_apilo_description_reference_status(db_path):
    conn = get_db(db_path)
    row = conn.execute(
        """
        SELECT COUNT(*) AS count, MAX(imported_at) AS imported_at,
               MAX(source_name) AS source_name
        FROM apilo_description_references
        """
    ).fetchone()
    conn.close()
    return dict(row)


def replace_channel_description_checks(db_path, checks, *, checked_at=None):
    checked_at = checked_at or utc_now_iso()
    allowed_statuses = {"match", "mismatch", "unavailable", "error"}
    allowed_palette_statuses = {
        "match",
        "mismatch",
        "absent",
        "unavailable",
        "not_applicable",
        "unverified",
    }
    normalized = []
    seen = set()
    for check in checks:
        key = (
            int(check["apilo_product_id"]),
            str(check["channel_key"]),
            str(check.get("external_id") or ""),
        )
        status = str(check.get("status") or "")
        palette_status = str(check.get("palette_status") or "unverified")
        if (
            key in seen
            or key[0] < 1
            or not key[1]
            or not key[2]
            or status not in allowed_statuses
            or palette_status not in allowed_palette_statuses
        ):
            raise ValueError("Nieprawidłowy rekord kontroli opisu kanału.")
        seen.add(key)
        normalized.append(
            (
                *key,
                str(check.get("reference_hash") or ""),
                status,
                str(check.get("source") or "")[:100],
                str(check.get("detail") or "")[:300],
                str(check.get("actual_description_text") or "")[:65_535],
                palette_status,
                str(check.get("palette_material") or "")[:30],
                str(check.get("palette_block_text") or "")[:65_535],
                str(check.get("palette_block_hash") or "")[:64],
                checked_at,
            )
        )
    if not normalized:
        raise ValueError("Kontrola opisów nie zwróciła żadnych ofert.")
    conn = get_db(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM channel_description_checks")
            conn.executemany(
                """
                INSERT INTO channel_description_checks (
                    apilo_product_id, channel_key, external_id,
                    reference_hash, status, source, detail,
                    actual_description_text, palette_status, palette_material,
                    palette_block_text, palette_block_hash, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                normalized,
            )
    finally:
        conn.close()
    return len(normalized)


def get_channel_description_checks(db_path, *, apilo_product_id=None):
    conn = get_db(db_path)
    if apilo_product_id is None:
        rows = conn.execute(
            """
            SELECT apilo_product_id, channel_key, external_id, reference_hash,
                   status, source, detail, actual_description_text,
                   palette_status, palette_material, palette_block_text,
                   palette_block_hash, checked_at
            FROM channel_description_checks
            ORDER BY apilo_product_id, channel_key, external_id
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT apilo_product_id, channel_key, external_id, reference_hash,
                   status, source, detail, actual_description_text,
                   palette_status, palette_material, palette_block_text,
                   palette_block_hash, checked_at
            FROM channel_description_checks
            WHERE apilo_product_id = ?
            ORDER BY channel_key, external_id
            """,
            (int(apilo_product_id),),
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def upsert_channel_description_check(db_path, check, *, checked_at=None):
    checked_at = checked_at or utc_now_iso()
    product_id = int(check["apilo_product_id"])
    channel_key = str(check.get("channel_key") or "").strip()
    external_id = str(check.get("external_id") or "").strip()
    status = str(check.get("status") or "").strip()
    palette_status = str(check.get("palette_status") or "unverified").strip()
    if (
        product_id < 1
        or not channel_key
        or not external_id
        or status not in {"match", "mismatch", "unavailable", "error"}
        or palette_status
        not in {
            "match",
            "mismatch",
            "absent",
            "unavailable",
            "not_applicable",
            "unverified",
        }
    ):
        raise ValueError("Nieprawidłowy rekord kontroli opisu kanału.")
    conn = get_db(db_path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO channel_description_checks (
                    apilo_product_id, channel_key, external_id,
                    reference_hash, status, source, detail,
                    actual_description_text, palette_status, palette_material,
                    palette_block_text, palette_block_hash, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(apilo_product_id, channel_key, external_id) DO UPDATE SET
                    reference_hash = excluded.reference_hash,
                    status = excluded.status,
                    source = excluded.source,
                    detail = excluded.detail,
                    actual_description_text = excluded.actual_description_text,
                    palette_status = excluded.palette_status,
                    palette_material = excluded.palette_material,
                    palette_block_text = excluded.palette_block_text,
                    palette_block_hash = excluded.palette_block_hash,
                    checked_at = excluded.checked_at
                """,
                (
                    product_id,
                    channel_key,
                    external_id,
                    str(check.get("reference_hash") or ""),
                    status,
                    str(check.get("source") or "")[:100],
                    str(check.get("detail") or "")[:300],
                    str(check.get("actual_description_text") or "")[:65_535],
                    palette_status,
                    str(check.get("palette_material") or "")[:30],
                    str(check.get("palette_block_text") or "")[:65_535],
                    str(check.get("palette_block_hash") or "")[:64],
                    checked_at,
                ),
            )
    finally:
        conn.close()


def get_channel_listings_for_product(db_path, apilo_product_id):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT apilo_product_id, channel_key, apilo_auction_id,
               external_id, status, listing_name, offer_price,
               offer_quantity, updated_at
        FROM channel_listings
        WHERE apilo_product_id = ?
        ORDER BY channel_key, apilo_auction_id
        """,
        (int(apilo_product_id),),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def count_channel_listings(db_path, channel_key):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM channel_listings WHERE channel_key = ?",
        (str(channel_key),),
    ).fetchone()
    conn.close()
    return int(row["count"] or 0)


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _safe_bool_int(value):
    if isinstance(value, str):
        return 1 if value.strip().lower() in {"1", "true", "yes"} else 0
    return 1 if bool(value) else 0


def replace_empik_snapshot(db_path, offers, imports, *, synced_at=None):
    now = synced_at or utc_now_iso()
    offer_rows = []
    seen_offer_ids = set()
    for offer in offers:
        offer_id = str(offer.get("offer_id") or "").strip()
        if not offer_id or offer_id in seen_offer_ids:
            raise ValueError("Nieprawidłowy lub zduplikowany identyfikator oferty EmpikPlace.")
        seen_offer_ids.add(offer_id)
        product = offer.get("product") if isinstance(offer.get("product"), dict) else {}
        pricing = (
            offer.get("applicable_pricing")
            if isinstance(offer.get("applicable_pricing"), dict)
            else {}
        )
        offer_rows.append(
            (
                offer_id,
                str(offer.get("shop_sku") or "").strip(),
                str(offer.get("product_sku") or "").strip(),
                _safe_bool_int(offer.get("active")),
                str(offer.get("state_code") or "").strip(),
                _safe_int(offer.get("quantity"), 0),
                _safe_float(
                    offer.get("price")
                    if offer.get("price") not in (None, "")
                    else pricing.get("price")
                ),
                str(product.get("title") or offer.get("product_title") or "").strip(),
                now,
            )
        )

    import_rows = []
    seen_import_ids = set()
    for item in imports:
        import_id = str(item.get("import_id") or "").strip()
        if not import_id or import_id in seen_import_ids:
            continue
        seen_import_ids.add(import_id)
        import_rows.append(
            (
                import_id,
                str(item.get("date_created") or "").strip(),
                str(item.get("status") or "").strip(),
                str(item.get("reason_status") or "").strip(),
                _safe_bool_int(item.get("has_error_report")),
                _safe_int(item.get("lines_read"), 0),
                _safe_int(item.get("lines_in_success"), 0),
                _safe_int(item.get("lines_in_error"), 0),
                _safe_int(item.get("lines_in_pending"), 0),
                _safe_int(item.get("offer_inserted"), 0),
                _safe_int(item.get("offer_updated"), 0),
                _safe_int(item.get("offer_deleted"), 0),
                str(item.get("origin") or "").strip(),
                str(item.get("mode") or "").strip(),
                now,
            )
        )

    conn = get_db(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM empik_offers")
            conn.executemany(
                """
                INSERT INTO empik_offers (
                    offer_id, shop_sku, product_sku, active, state_code,
                    quantity, price, product_title, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                offer_rows,
            )
            conn.execute("DELETE FROM empik_offer_imports")
            conn.executemany(
                """
                INSERT INTO empik_offer_imports (
                    import_id, date_created, status, reason_status,
                    has_error_report, lines_read, lines_in_success,
                    lines_in_error, lines_in_pending, offer_inserted,
                    offer_updated, offer_deleted, origin, mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                import_rows,
            )
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at)
                VALUES ('empik_last_sync_at', ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (now, now),
            )
            conn.execute(
                "DELETE FROM settings WHERE key IN ('empik_last_sync_error', 'empik_last_sync_error_at')"
            )
    finally:
        conn.close()
    return {"offers": len(offer_rows), "imports": len(import_rows), "synced_at": now}


def get_empik_offers(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT offer_id, shop_sku, product_sku, active, state_code,
               quantity, price, product_title, updated_at
        FROM empik_offers
        ORDER BY product_title COLLATE NOCASE ASC, offer_id ASC
        """
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_empik_offer_imports(db_path, *, limit=20):
    limit = max(1, min(int(limit), 100))
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT import_id, date_created, status, reason_status,
               has_error_report, lines_read, lines_in_success,
               lines_in_error, lines_in_pending, offer_inserted,
               offer_updated, offer_deleted, origin, mode, updated_at
        FROM empik_offer_imports
        ORDER BY date_created DESC, import_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_empik_offer_import(db_path, import_id):
    conn = get_db(db_path)
    row = conn.execute(
        """
        SELECT import_id, date_created, status, reason_status,
               has_error_report, lines_in_error
        FROM empik_offer_imports
        WHERE import_id = ?
        """,
        (str(import_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_products(
    db_path,
    search=None,
    material_filter=None,
    color_filter=None,
    preset="all",
    sort="name",
    order="asc",
    limit: int | None = 50,
    offset: int = 0,
    lead_time_days=14,
    safety_pct=20,
    suggest_days=30,
):
    allowed = {
        "name": "name",
        "quantity": "quantity",
        "suggested": "suggested_qty",
        "shortage": "shortage_qty",
        "stock_value": "stock_value",
        "sales_year": "quantity_year",
        "updated": "updated_at",
    }
    sort_col = allowed.get(sort, "name")
    order_dir = "DESC" if order == "desc" else "ASC"
    if sort_col == "name":
        order_clause = f"{sort_col} COLLATE NOCASE {order_dir}"
    else:
        order_clause = f"{sort_col} {order_dir}"
    try:
        suggest_days = int(suggest_days)
    except (TypeError, ValueError):
        suggest_days = 30
    if suggest_days < 1:
        suggest_days = 30
    query_base, params = _build_products_scope(
        search=search,
        material_filter=material_filter,
        color_filter=color_filter,
        preset=preset,
        lead_time_days=lead_time_days,
        safety_pct=safety_pct,
        suggest_days=suggest_days,
    )
    query = f"""
        SELECT *
        FROM ({query_base}) AS computed
        ORDER BY {order_clause}
    """
    if limit is not None:
        query += "\n        LIMIT ? OFFSET ?"
        params.extend([limit, offset])

    conn = get_db(db_path)
    rows = conn.execute(query, tuple(params)).fetchall()
    conn.close()
    return rows


def _build_products_scope(
    search=None,
    material_filter=None,
    color_filter=None,
    preset="all",
    lead_time_days=14,
    safety_pct=20,
    suggest_days=30,
):
    base_query = """
        SELECT
            base.*,
            CASE
                WHEN base.suggested_qty IS NULL THEN NULL
                ELSE base.suggested_qty - COALESCE(base.quantity, 0)
            END AS shortage_qty,
            ROUND(
                COALESCE(base.quantity, 0) * COALESCE(base.allegro_price_with_tax, base.price_with_tax, 0),
                2
            ) AS stock_value
        FROM (
            SELECT
                p.*,
                sc.quantity_30d,
                scy.quantity_year,
                scy.quantity_year_adjusted,
                scy.orders_year,
                scy.outlier_order_qty,
                CASE
                    WHEN COALESCE(scy.quantity_year_adjusted, scy.quantity_year, 0) = 1 THEN 0
                    WHEN sc.quantity_30d IS NULL
                        AND COALESCE(scy.quantity_year_adjusted, scy.quantity_year, 0) = 0
                        AND COALESCE(scy.outlier_order_qty, 0) = 0
                        THEN NULL
                    ELSE CAST(
                        (
                            CASE
                                WHEN CAST(? AS INTEGER) = 365
                                    THEN COALESCE(scy.quantity_year_adjusted, scy.quantity_year, 0) / 365.0
                                WHEN COALESCE(sc.quantity_30d, 0) = 0
                                    AND COALESCE(scy.quantity_year_adjusted, scy.quantity_year, 0) > 0
                                    THEN COALESCE(scy.quantity_year_adjusted, scy.quantity_year, 0) / 365.0
                                ELSE COALESCE(sc.quantity_30d, 0) / CAST(? AS REAL)
                            END
                        ) * ? * (1 + ? / 100.0) + 0.9999
                        AS INTEGER
                    )
                END AS suggested_qty
            FROM products p
            LEFT JOIN sales_cache sc ON sc.ean = p.ean
            LEFT JOIN sales_cache_year scy ON scy.ean = p.ean
            WHERE COALESCE(p.present_in_apilo, 1) = 1
        ) AS base
    """
    params = [suggest_days, suggest_days, lead_time_days, safety_pct]
    where_clauses = []
    if search:
        search_value = str(search).strip()
        normalized_search = search_value.lower()
        material_aliases = {
            "pla": ("PLA", "PLA+"),
            "pla+": ("PLA+",),
            "petg": ("PETG",),
            "pet-g": ("PETG",),
            "flex": ("FLEX", "Flex/guma"),
            "guma": ("FLEX", "Flex/guma"),
            "flex/guma": ("FLEX", "Flex/guma"),
            "tpu": ("FLEX", "Flex/guma"),
            "carbon": ("CARBON",),
        }
        color_aliases = {
            "wielokolor": "wielokolorowy",
            "wielokolorowy": "wielokolorowy",
        }
        if normalized_search in material_aliases:
            values = material_aliases[normalized_search]
            where_clauses.append("material IN (" + ",".join("?" for _ in values) + ")")
            params.extend(values)
        elif normalized_search in color_aliases:
            where_clauses.append("color = ?")
            params.append(color_aliases[normalized_search])
        else:
            like = f"%{search_value}%"
            where_clauses.append(
                "(sku LIKE ? OR name LIKE ? OR original_code LIKE ? OR ean LIKE ? OR material LIKE ? OR color LIKE ?)"
            )
            params.extend([like, like, like, like, like, like])
    if material_filter:
        if material_filter == "PLA":
            values = ("PLA", "PLA+")
        elif material_filter == "FLEX":
            values = ("FLEX", "Flex/guma")
        else:
            values = (material_filter,)
        where_clauses.append("material IN (" + ",".join("?" for _ in values) + ")")
        params.extend(values)
    if color_filter:
        where_clauses.append("color = ?")
        params.append(color_filter)
    if preset == "shortage":
        where_clauses.append("COALESCE(shortage_qty, 0) > 0")
    elif preset == "out_of_stock":
        where_clauses.append("COALESCE(quantity, 0) = 0")
    elif preset == "no_ean":
        where_clauses.append("(ean IS NULL OR ean = '')")
    elif preset == "no_image":
        where_clauses.append("(image_url IS NULL OR image_url = '')")
    elif preset == "no_sales":
        where_clauses.append("COALESCE(quantity_year, 0) = 0")
    elif preset == "high_value":
        where_clauses.append("COALESCE(stock_value, 0) > 0")
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    return f"SELECT * FROM ({base_query}) AS computed {where_sql}", params


def get_products_count(
    db_path,
    search=None,
    material_filter=None,
    color_filter=None,
    preset="all",
    lead_time_days=14,
    safety_pct=20,
    suggest_days=30,
):
    query_base, params = _build_products_scope(
        search=search,
        material_filter=material_filter,
        color_filter=color_filter,
        preset=preset,
        lead_time_days=lead_time_days,
        safety_pct=safety_pct,
        suggest_days=suggest_days,
    )
    conn = get_db(db_path)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM ({query_base}) AS computed",
        tuple(params),
    ).fetchone()
    conn.close()
    return row["count"] if row else 0



def get_attribute_filter_counts(db_path):
    conn = get_db(db_path)
    material_rows = conn.execute(
        """
        SELECT
            CASE
                WHEN material IN ('PLA', 'PLA+') THEN 'PLA'
                WHEN material = 'PETG' THEN 'PETG'
                WHEN material IN ('FLEX', 'Flex/guma') THEN 'FLEX'
                WHEN material = 'CARBON' THEN 'CARBON'
                ELSE material
            END AS value,
            COUNT(*) AS count
        FROM products
        WHERE present_in_apilo = 1 AND COALESCE(material, '') != ''
        GROUP BY value
        ORDER BY count DESC, value COLLATE NOCASE ASC
        """
    ).fetchall()
    color_rows = conn.execute(
        """
        SELECT color AS value, COUNT(*) AS count
        FROM products
        WHERE present_in_apilo = 1 AND COALESCE(color, '') != ''
        GROUP BY color
        ORDER BY count DESC, color COLLATE NOCASE ASC
        """
    ).fetchall()
    conn.close()
    return {
        "materials": [dict(row) for row in material_rows if row["value"]],
        "colors": [dict(row) for row in color_rows if row["value"]],
    }

def get_dashboard_metrics(db_path, lead_time_days=14, safety_pct=20, suggest_days=30):
    query_base, params = _build_products_scope(
        search=None,
        preset="all",
        lead_time_days=lead_time_days,
        safety_pct=safety_pct,
        suggest_days=suggest_days,
    )
    conn = get_db(db_path)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_products,
            SUM(CASE WHEN COALESCE(shortage_qty, 0) > 0 THEN 1 ELSE 0 END) AS shortage_count,
            SUM(CASE WHEN COALESCE(shortage_qty, 0) > 0 THEN shortage_qty ELSE 0 END) AS shortage_units,
            SUM(CASE WHEN COALESCE(quantity, 0) = 0 THEN 1 ELSE 0 END) AS out_of_stock_count,
            SUM(CASE WHEN ean IS NULL OR ean = '' THEN 1 ELSE 0 END) AS missing_ean_count,
            SUM(CASE WHEN image_url IS NULL OR image_url = '' THEN 1 ELSE 0 END) AS missing_image_count,
            SUM(CASE WHEN COALESCE(quantity_year, 0) = 0 THEN 1 ELSE 0 END) AS no_sales_count,
            SUM(CASE WHEN COALESCE(stock_value, 0) > 0 THEN 1 ELSE 0 END) AS high_value_count,
            ROUND(COALESCE(SUM(stock_value), 0), 2) AS inventory_value
        FROM ({query_base}) AS computed
        """,
        tuple(params),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_product_by_id(db_path, product_id):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM products WHERE id = ? AND present_in_apilo = 1",
        (product_id,),
    ).fetchone()
    conn.close()
    return row


def get_product_by_apilo_id(db_path, apilo_id):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM products WHERE apilo_id = ? AND present_in_apilo = 1",
        (apilo_id,),
    ).fetchone()
    conn.close()
    return row


def get_ean_name_map(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT ean, name FROM products WHERE present_in_apilo = 1 AND ean IS NOT NULL AND ean != ''"
    ).fetchall()
    conn.close()
    return {row["ean"]: row["name"] for row in rows}


def get_product_maps(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT apilo_id, ean, original_code, sku
        FROM products
        WHERE present_in_apilo = 1 AND ean IS NOT NULL AND ean != ''
        """
    ).fetchall()
    conn.close()
    by_apilo_id = {}
    by_original_code = {}
    by_sku = {}
    for row in rows:
        ean = row["ean"]
        if row["apilo_id"]:
            by_apilo_id[str(row["apilo_id"])] = ean
        if row["original_code"]:
            by_original_code[row["original_code"]] = ean
        if row["sku"]:
            by_sku[row["sku"]] = ean
    return by_apilo_id, by_original_code, by_sku


def get_product_id_maps(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT apilo_id, ean, sku FROM products WHERE present_in_apilo = 1 AND apilo_id IS NOT NULL"
    ).fetchall()
    conn.close()
    by_sku = {}
    by_ean = {}
    by_apilo_id = {}
    for row in rows:
        apilo_id = row["apilo_id"]
        if apilo_id is None:
            continue
        by_apilo_id[str(apilo_id)] = apilo_id
        if row["sku"]:
            by_sku[row["sku"]] = apilo_id
        if row["ean"]:
            by_ean[row["ean"]] = apilo_id
    return by_apilo_id, by_sku, by_ean


def update_allegro_prices(db_path, price_map):
    if not price_map:
        return
    conn = get_db(db_path)
    with conn:
        conn.executemany(
            "UPDATE products SET allegro_price_with_tax = ? WHERE apilo_id = ?",
            [(value, key) for key, value in price_map.items()],
        )
    conn.close()


def get_base_price_map(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT apilo_id, price_with_tax FROM products WHERE present_in_apilo = 1 AND apilo_id IS NOT NULL"
    ).fetchall()
    conn.close()
    result = {}
    for row in rows:
        if row["price_with_tax"] is not None:
            result[row["apilo_id"]] = row["price_with_tax"]
    return result


def update_allegro_auction_ids(db_path, auction_map):
    conn = get_db(db_path)
    try:
        with conn:
            conn.execute("UPDATE products SET allegro_auction_id = NULL")
            if auction_map:
                conn.executemany(
                    "UPDATE products SET allegro_auction_id = ? WHERE apilo_id = ?",
                    [(value, key) for key, value in auction_map.items()],
                )
    finally:
        conn.close()


def update_product_attributes(db_path, attributes_map):
    conn = get_db(db_path)
    now = utc_now_iso()
    try:
        with conn:
            for product_id, values in attributes_map.items():
                material = (values.get("material") or "").strip()
                color = (values.get("color") or "").strip()
                source = (values.get("source") or "").strip()
                if not material and not color:
                    continue
                conn.execute(
                    """
                    UPDATE products
                    SET material = ?, color = ?, attributes_source = ?, attributes_updated_at = ?
                    WHERE apilo_id = ?
                      AND COALESCE(attributes_source, '') != 'manual_user_hint'
                    """,
                    (material, color, source, now, product_id),
                )
    finally:
        conn.close()


def update_product_attributes_manual(db_path, product_id, *, material, color):
    conn = get_db(db_path)
    now = utc_now_iso()
    try:
        with conn:
            cursor = conn.execute(
                """
                UPDATE products
                SET material = ?, color = ?, attributes_source = 'manual_user_hint',
                    attributes_updated_at = ?
                WHERE id = ? AND present_in_apilo = 1
                """,
                ((material or "").strip(), (color or "").strip(), now, int(product_id)),
            )
            if cursor.rowcount != 1:
                raise ValueError("Produkt nie istnieje albo nie jest już aktywny w Apilo.")
    finally:
        conn.close()


def get_inventory_value_totals(db_path):
    conn = get_db(db_path)
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN price_with_tax IS NOT NULL THEN quantity * price_with_tax ELSE 0 END) AS store_total,
            SUM(CASE WHEN allegro_price_with_tax IS NOT NULL THEN quantity * allegro_price_with_tax ELSE 0 END) AS allegro_total
        FROM products
        WHERE present_in_apilo = 1
        """
    ).fetchone()
    conn.close()
    store_total = row["store_total"] if row and row["store_total"] is not None else 0
    allegro_total = row["allegro_total"] if row and row["allegro_total"] is not None else 0
    return float(store_total), float(allegro_total)


def save_sales_cache(db_path, totals, details_map):
    now = utc_now_iso()
    conn = get_db(db_path)
    with conn:
        conn.execute("DELETE FROM sales_cache")
        if totals:
            import json
            conn.executemany(
                """
                INSERT INTO sales_cache (ean, quantity_30d, daily_json, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (ean, qty, json.dumps(details_map.get(ean, [])), now)
                    for ean, qty in totals.items()
                ],
            )
    conn.close()


def get_sales_cache_map(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT ean, quantity_30d FROM sales_cache"
    ).fetchall()
    conn.close()
    return {row["ean"]: row["quantity_30d"] for row in rows}


def get_sales_cache_details_map(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT ean, daily_json FROM sales_cache WHERE daily_json IS NOT NULL"
    ).fetchall()
    conn.close()
    import json
    result = {}
    for row in rows:
        try:
            result[row["ean"]] = json.loads(row["daily_json"])
        except (TypeError, json.JSONDecodeError):
            continue
    return result


def calculate_adjusted_year_sales(quantity_year, order_details=None):
    try:
        total_qty = int(quantity_year or 0)
    except (TypeError, ValueError):
        total_qty = 0
    if not order_details:
        return total_qty, 0

    order_quantities = []
    for item in order_details:
        try:
            qty = int(item.get("qty") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if qty > 0:
            order_quantities.append(qty)
    if not order_quantities:
        return total_qty, 0

    order_quantities.sort(reverse=True)
    largest_order_qty = order_quantities[0]
    if len(order_quantities) == 1:
        if largest_order_qty >= 3:
            return 0, largest_order_qty
        return total_qty, 0

    rest_qty = sum(order_quantities[1:])
    if rest_qty <= 0:
        return total_qty, 0
    rest_average = rest_qty / (len(order_quantities) - 1)
    if (
        largest_order_qty >= 5
        and largest_order_qty >= rest_average * 5
        and largest_order_qty >= rest_qty * 3
    ):
        return max(total_qty - largest_order_qty, 0), largest_order_qty
    return total_qty, 0


def save_sales_year_cache(db_path, totals, order_counts, details_map=None):
    now = utc_now_iso()
    details_map = details_map or {}
    conn = get_db(db_path)
    with conn:
        conn.execute("DELETE FROM sales_cache_year")
        rows = []
        for ean, qty in totals.items():
            adjusted_qty, outlier_qty = calculate_adjusted_year_sales(
                qty,
                details_map.get(ean),
            )
            rows.append((ean, qty, adjusted_qty, order_counts.get(ean, 0), outlier_qty, now))
        conn.executemany(
            """
            INSERT INTO sales_cache_year (
                ean,
                quantity_year,
                quantity_year_adjusted,
                orders_year,
                outlier_order_qty,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.close()


def get_sales_year_map(db_path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT ean, quantity_year, quantity_year_adjusted, orders_year, outlier_order_qty
        FROM sales_cache_year
        """
    ).fetchall()
    conn.close()
    return {
        row["ean"]: {
            "quantity": row["quantity_year"],
            "quantity_adjusted": row["quantity_year_adjusted"],
            "orders": row["orders_year"],
            "outlier_order_qty": row["outlier_order_qty"],
        }
        for row in rows
    }


def update_product_image(db_path, apilo_id, image_url):
    now = utc_now_iso()
    conn = get_db(db_path)
    previous_row = conn.execute(
        "SELECT image_url FROM products WHERE apilo_id = ?",
        (apilo_id,),
    ).fetchone()
    previous_url = previous_row["image_url"] if previous_row else None
    with conn:
        conn.execute(
            """
            UPDATE products
            SET image_url = ?,
                updated_at = ?
            WHERE apilo_id = ?
            """,
            (image_url, now, apilo_id),
        )
    conn.close()
    return bool(previous_url) and previous_url != image_url




def update_product_quantity(db_path, product_id, quantity):
    now = utc_now_iso()
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            UPDATE products
            SET quantity = ?,
                dirty = 0,
                last_synced_quantity = ?,
                last_synced_at = ?,
                updated_at = ?
            WHERE id = ? AND present_in_apilo = 1
            """,
            (quantity, quantity, now, now, product_id),
        )
    conn.close()


def mark_product_quantity_unverified(db_path, product_id):
    now = utc_now_iso()
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            UPDATE products
            SET dirty = 1,
                updated_at = ?
            WHERE id = ? AND present_in_apilo = 1
            """,
            (now, product_id),
        )
    conn.close()


def record_audit_log(
    db_path,
    action,
    entity_type,
    entity_id=None,
    entity_label=None,
    old_value=None,
    new_value=None,
    details=None,
    actor_ip=None,
):
    details_json = json.dumps(details, ensure_ascii=False) if details is not None else None
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO audit_log (
                action,
                entity_type,
                entity_id,
                entity_label,
                old_value,
                new_value,
                details_json,
                actor_ip,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                entity_type,
                str(entity_id) if entity_id is not None else None,
                entity_label,
                old_value,
                new_value,
                details_json,
                actor_ip,
                utc_now_iso(),
            ),
        )
    conn.close()


def get_recent_audit_log(db_path, limit=50):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT *
        FROM audit_log
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def prune_login_attempts(db_path, before_iso):
    conn = get_db(db_path)
    with conn:
        conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (before_iso,))
    conn.close()


def count_recent_login_attempts(db_path, ip_address, since_iso):
    conn = get_db(db_path)
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM login_attempts
        WHERE ip_address = ? AND created_at >= ?
        """,
        (ip_address, since_iso),
    ).fetchone()
    conn.close()
    return row["count"] if row else 0


def record_login_attempt(db_path, ip_address):
    conn = get_db(db_path)
    with conn:
        conn.execute(
            "INSERT INTO login_attempts (ip_address, created_at) VALUES (?, ?)",
            (ip_address, utc_now_iso()),
        )
    conn.close()


def clear_login_attempts(db_path, ip_address):
    conn = get_db(db_path)
    with conn:
        conn.execute("DELETE FROM login_attempts WHERE ip_address = ?", (ip_address,))
    conn.close()


def get_setting(db_path, key):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?",
        (key,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    value = row["value"]
    if key in SECRET_SETTING_KEYS:
        return _decrypt_secret_value(db_path, value, context=f"settings.{key}")
    return value


def set_setting(db_path, key, value):
    now = utc_now_iso()
    stored_value = _encrypt_secret_value(db_path, value) if key in SECRET_SETTING_KEYS else value
    conn = get_db(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, stored_value, now),
        )
    conn.close()


def migrate_secret_storage(db_path):
    migrated = {"settings": 0, "tokens": 0}
    now = utc_now_iso()
    conn = get_db(db_path)
    settings_rows = conn.execute(
        """
        SELECT key, value
        FROM settings
        WHERE key IN ({placeholders})
        """.format(placeholders=", ".join("?" for _ in SECRET_SETTING_KEYS)),
        tuple(sorted(SECRET_SETTING_KEYS)),
    ).fetchall()
    with conn:
        for row in settings_rows:
            raw_value = row["value"]
            if not raw_value or str(raw_value).startswith(ENCRYPTED_VALUE_PREFIX):
                continue
            conn.execute(
                """
                UPDATE settings
                SET value = ?, updated_at = ?
                WHERE key = ?
                """,
                (_encrypt_secret_value(db_path, raw_value), now, row["key"]),
            )
            migrated["settings"] += 1
        token_row = conn.execute("SELECT * FROM tokens WHERE id = 1").fetchone()
        if token_row:
            token_updates = {}
            for column in SECRET_TOKEN_COLUMNS:
                raw_value = token_row[column]
                if raw_value and not str(raw_value).startswith(ENCRYPTED_VALUE_PREFIX):
                    token_updates[column] = _encrypt_secret_value(db_path, raw_value)
            if token_updates:
                conn.execute(
                    """
                    UPDATE tokens
                    SET access_token = COALESCE(?, access_token),
                        refresh_token = COALESCE(?, refresh_token),
                        updated_at = ?
                    WHERE id = 1
                    """,
                    (
                        token_updates.get("access_token"),
                        token_updates.get("refresh_token"),
                        now,
                    ),
                )
                migrated["tokens"] = len(token_updates)
    conn.close()
    return migrated
