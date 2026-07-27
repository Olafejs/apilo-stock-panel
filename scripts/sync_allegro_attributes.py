#!/usr/bin/env python3
import argparse
import base64
import fcntl
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from product_attributes import description_primary_section_text, parse_material_color  # noqa: E402

DEFAULT_ENV_FILE = os.environ.get("ALLEGRO_ENV_FILE", "")
DEFAULT_DB_PATH = ROOT_DIR / "data" / "db" / "apilo.sqlite3"
ACCEPT = "application/vnd.allegro.public.v1+json"
GET_MAX_ATTEMPTS = 3
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY_SECONDS = 10.0


def load_env_file(path):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_tokens(token_store_path):
    path = Path(token_store_path).expanduser()
    if not path.exists():
        raise RuntimeError(f"Brak pliku tokenów Allegro: {path}")
    return json.loads(path.read_text()), path


def save_tokens(path, tokens):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_schema_version": 1, **tokens}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def refresh_access_token(tokens, path):
    client_id = os.environ.get("ALLEGRO_CLIENT_ID")
    client_secret = os.environ.get("ALLEGRO_CLIENT_SECRET")
    refresh_token = tokens.get("refresh_token")
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("Brak ALLEGRO_CLIENT_ID/SECRET albo refresh_token.")
    oauth_base = os.environ.get("ALLEGRO_OAUTH_BASE_URL") or "https://allegro.pl"
    body = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh_token}
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        oauth_base.rstrip("/") + "/auth/oauth/token",
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    refreshed = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or refresh_token,
        "token_type": data.get("token_type", "bearer"),
        "scope": data.get("scope") or tokens.get("scope"),
        "expires_at": time.time() + int(data.get("expires_in", 3600)) - 60,
        "extra": data,
    }
    save_tokens(path, refreshed)
    return refreshed


def get_access_token():
    token_store_path = os.environ.get("ALLEGRO_TOKEN_STORE_PATH") or str(
        ROOT_DIR / "secrets" / "allegro_tokens.json"
    )
    path = Path(token_store_path).expanduser()
    lock_path = path.parent / ".tokens.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        tokens, path = load_tokens(path)
        if float(tokens.get("expires_at") or 0) <= time.time() + 120:
            tokens = refresh_access_token(tokens, path)
        return tokens["access_token"]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def retry_delay_seconds(attempt, headers=None):
    retry_after = (headers or {}).get("Retry-After")
    if retry_after:
        try:
            return min(MAX_RETRY_DELAY_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(MAX_RETRY_DELAY_SECONDS, 0.5 * (2**attempt))


def urlopen_json_get_with_retry(request, attempts=GET_MAX_ATTEMPTS):
    if request.get_method() != "GET":
        raise ValueError("Retry helper accepts GET requests only.")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUS or attempt + 1 >= attempts:
                raise
            time.sleep(retry_delay_seconds(attempt, exc.headers))
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 >= attempts:
                raise
            time.sleep(retry_delay_seconds(attempt))
    raise RuntimeError("GET retry loop exhausted.")


def allegro_get_product_offer(offer_id, access_token):
    api_base = os.environ.get("ALLEGRO_API_BASE_URL") or "https://api.allegro.pl"
    request = urllib.request.Request(
        api_base.rstrip("/") + f"/sale/product-offers/{offer_id}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": ACCEPT,
            "Accept-Language": os.environ.get("ALLEGRO_ACCEPT_LANGUAGE", "pl-PL"),
        },
        method="GET",
    )
    return urlopen_json_get_with_retry(request)


def ensure_attribute_columns(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    required = {
        "material": "TEXT",
        "color": "TEXT",
        "attributes_source": "TEXT",
        "attributes_updated_at": "TEXT",
        "present_in_apilo": "INTEGER NOT NULL DEFAULT 1",
    }
    for column, definition in required.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE products ADD COLUMN {column} {definition}")
    conn.commit()


def select_products(conn, force=False, limit=None):
    where = (
        "present_in_apilo = 1 "
        "AND allegro_auction_id IS NOT NULL "
        "AND allegro_auction_id != '' "
        "AND COALESCE(attributes_source, '') != 'manual_user_hint'"
    )
    if not force:
        where += " AND (material IS NULL OR material = '' OR color IS NULL OR color = '')"
    query = f"""
        SELECT id, name, allegro_auction_id, material, color, attributes_source
        FROM products
        WHERE {where}
        ORDER BY id
    """
    if limit:
        query += " LIMIT ?"
        return conn.execute(query, (limit,)).fetchall()
    return conn.execute(query).fetchall()


def sync(db_path, force=False, limit=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_attribute_columns(conn)
    rows = select_products(conn, force=force, limit=limit)
    access_token = get_access_token()
    checked = updated = skipped = errors = 0
    try:
        for row in rows:
            checked += 1
            offer_id = str(row["allegro_auction_id"])
            try:
                offer = allegro_get_product_offer(offer_id, access_token)
                text = description_primary_section_text(offer.get("description"))
                attrs = parse_material_color(text)
                if force:
                    material = attrs["material"]
                    color = attrs["color"]
                else:
                    material = attrs["material"] or row["material"] or ""
                    color = attrs["color"] or row["color"] or ""
                if force or material or color:
                    cursor = conn.execute(
                        """
                        UPDATE products
                        SET material = ?, color = ?, attributes_source = ?, attributes_updated_at = ?
                        WHERE id = ?
                          AND COALESCE(attributes_source, '') != 'manual_user_hint'
                        """,
                        (
                            material,
                            color,
                            "allegro_description",
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            row["id"],
                        ),
                    )
                    if cursor.rowcount == 1:
                        updated += 1
                    else:
                        skipped += 1
                else:
                    skipped += 1
            except urllib.error.HTTPError as exc:
                errors += 1
                print(f"ERROR offer={offer_id} http={exc.code}", file=sys.stderr)
            except Exception as exc:
                errors += 1
                print(f"ERROR offer={offer_id} {type(exc).__name__}: {exc}", file=sys.stderr)
        conn.commit()
    finally:
        conn.close()
    return {"checked": checked, "updated": updated, "skipped": skipped, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Pobiera materiał i kolor z opisów aukcji Allegro do Apilo Panelu.")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--force", action="store_true", help="Odśwież także produkty z już zapisanym materiałem/kolorem.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    result = sync(args.db, force=args.force, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
