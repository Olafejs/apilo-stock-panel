import csv
import hashlib
import io
import logging
import os
import secrets
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import (
    Flask,
    abort,
    flash,
    has_request_context,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
from PIL import Image, ImageOps
from werkzeug.security import check_password_hash, generate_password_hash

from allegro_description_update import (
    AllegroDescriptionUnverifiedError,
    AllegroDescriptionUpdateError,
    AllegroFileCredentialStore,
)
from erli_palette_update import (
    ErliPaletteUnverifiedError,
    ErliPaletteUpdateError,
    erli_write_configured,
    updater_from_env as erli_updater_from_env,
)

from app_alerts import (
    build_low_stock_alert_history as build_runtime_low_stock_alert_history,
    format_item_count as format_alert_item_count,
    format_position_count as format_alert_position_count,
    get_low_stock_alert_enabled as get_runtime_low_stock_alert_enabled,
    get_low_stock_alert_interval_hours as get_runtime_low_stock_alert_interval_hours,
    get_low_stock_alert_next_check_iso as get_runtime_low_stock_alert_next_check_iso,
    get_low_stock_rows as get_runtime_low_stock_rows,
    is_low_stock_alert_due as is_runtime_low_stock_alert_due,
    mark_low_stock_alert_error as mark_runtime_low_stock_alert_error,
    process_low_stock_alert as process_runtime_low_stock_alert,
    summarize_low_stock_alert_settings_snapshot as summarize_runtime_low_stock_alert_settings_snapshot,
    update_low_stock_alert_state as update_runtime_low_stock_alert_state,
)
from app_auth import (
    get_csrf_token,
    is_safe_redirect_target,
    login_required,
    public_error_message,
    render_setup_password as render_auth_setup_password,
    validate_csrf,
)
from apilo import ApiloClient, ApiloClientError
from app_admin import (
    build_recent_audit_entries,
    build_secret_storage_payload,
    get_api_settings_snapshot,
    get_email_settings_snapshot,
    get_empik_settings_snapshot,
    summarize_api_settings_snapshot,
    summarize_email_settings_snapshot,
    summarize_empik_settings_snapshot,
    summarize_inventory_values_snapshot,
    summarize_suggestions_settings_snapshot,
    write_audit_event,
)
from app_email import SmtpValidationError, normalize_smtp_settings
from description_diffs import build_description_diff
from material_palette_checks import (
    canonical_material_palette_text,
    normalize_palette_material,
)
from product_attributes import parse_material_color
from empik import EmpikClient, EmpikClientError
from app_auth import (
    get_client_ip as resolve_client_ip,
    is_local_setup_request as is_local_setup_request_for_ip,
    is_login_rate_limited as check_login_rate_limited,
    login_window_start_iso as build_login_window_start_iso,
    password_missing as auth_password_missing,
    setup_token_required as auth_setup_token_required,
    tokens_missing as auth_tokens_missing,
)
from app_config import (
    APP_HOST,
    APP_PASSWORD,
    APP_PORT,
    APP_SETUP_TOKEN,
    APP_VERSION,
    DB_PATH,
    DEBUG_MODE,
    FLASK_SECRET_KEY,
    FLASK_SECRET_KEY_SOURCE,
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
    LOGIN_RATE_LIMIT_WINDOW_SECONDS,
    LOG_DIR,
    PROJECT_DESCRIPTION,
    PROJECT_NAME,
    REFRESH_INTERVAL_SECONDS,
    SALES_CACHE_REFRESH_INTERVAL_SECONDS,
    SALES_YEAR_REFRESH_INTERVAL_SECONDS,
    SESSION_COOKIE_SECURE,
    SESSION_LIFETIME_MINUTES,
    THUMB_DIR,
    THUMB_DOWNLOAD_TIMEOUT_SECONDS,
    THUMB_MAX_DOWNLOAD_BYTES,
    THUMB_TTL_SECONDS,
    TRUST_X_FORWARDED_FOR,
)
from app_reporting import (
    build_sales_report_csv,
    build_sales_report_rows,
    get_sales_totals as build_sales_totals,
    normalize_sales_report_days,
)
from app_sync import (
    build_sync_status_payload as build_runtime_sync_status_payload,
    compute_next_run_at,
    ensure_sync_schedule as ensure_runtime_sync_schedule,
    get_sync_job_label as get_runtime_sync_job_label,
    get_sync_status_snapshot,
    is_schedule_due,
    mark_sync_failed as mark_runtime_sync_failed,
    mark_sync_finished,
    mark_sync_started,
    schedule_inventory_sync as schedule_runtime_inventory_sync,
    schedule_sales_refresh as schedule_runtime_sales_refresh,
    should_refresh_year_sales_cache as should_refresh_runtime_year_sales_cache,
    start_background_refresh as start_runtime_background_refresh,
    update_sync_status as update_runtime_sync_status,
)
from app_inventory_sync import build_allegro_price_map, build_auction_metadata
from app_channels import build_channel_listing_rows, build_channel_matrix, build_listing_url
from app_utils import (
    format_date_pl,
    format_pln,
    format_pull_time,
    parse_float_value,
    parse_int_value,
    utc_now_iso,
)
from db import (
    apply_product_snapshot,
    clear_login_attempts,
    count_channel_listings,
    get_dashboard_metrics,
    get_attribute_filter_counts,
    get_apilo_description_reference,
    get_apilo_description_references,
    get_apilo_description_reference_status,
    get_channel_description_checks,
    get_channel_listings,
    get_channel_listings_for_product,
    get_empik_offer_import,
    get_empik_offer_imports,
    get_empik_offers,
    get_secret_storage_status,
    get_products,
    get_products_count,
    get_product_by_id,
    get_product_by_apilo_id,
    get_sales_channels,
    get_sales_cache_details_map,
    get_sales_year_map,
    get_setting,
    migrate_secret_storage,
    replace_empik_snapshot,
    set_setting,
    save_sales_cache,
    save_sales_year_cache,
    record_login_attempt,
    get_inventory_value_totals,
    mark_product_quantity_unverified,
    update_product_attributes_manual,
    update_product_quantity,
    upsert_channel_description_check,
)


app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=SESSION_LIFETIME_MINUTES),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    SESSION_COOKIE_NAME="apilo_session",
)
SYNC_LOCK = threading.Lock()
EMPIK_SYNC_LOCK = threading.Lock()
ALLEGRO_DESCRIPTION_WRITE_LOCK = threading.Lock()
ERLI_PALETTE_WRITE_LOCK = threading.Lock()
THUMB_REFRESH_LOCK = threading.Lock()
THUMB_REFRESH_IN_PROGRESS = set()
THUMB_REFRESH_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="thumb-refresh",
)
THUMB_RENDER_MAX_EDGE_PX = 256
PRODUCT_PRESET_LABELS = {
    "all": "Wszystkie",
    "shortage": "Braki",
    "out_of_stock": "Zero stanu",
    "no_ean": "Bez EAN",
    "no_image": "Bez zdjęcia",
    "no_sales": "Bez sprzedaży",
    "high_value": "Najwyższa wartość",
}
PRODUCT_PAGE_LIMITS = (25, 50, 100, 200)
PUBLIC_DESCRIPTION_RECHECK_CHANNELS = {"erli", "prestashop"}
DESCRIPTION_STATUS_LABELS = {
    "match": "Opis zgodny",
    "mismatch": "Opis różny",
    "unavailable": "Brak odczytu opisu",
    "error": "Opis niesprawdzony",
    "unverified": "Opis niesprawdzony",
}
PALETTE_STATUS_LABELS = {
    "match": "Blok kolorów zgodny",
    "mismatch": "Blok kolorów różny",
    "missing": "Brak bloku kolorów",
    "absent": "Bez bloku kolorów",
    "unavailable": "Brak odczytu bloku kolorów",
    "not_applicable": "Blok kolorów nie dotyczy",
    "unverified": "Blok kolorów niesprawdzony",
}
PRODUCT_MATERIAL_OPTIONS = ("PLA", "PLA+", "PETG", "FLEX", "CARBON")
PRODUCT_EXPORT_COLUMNS = (
    "Apilo ID",
    "SKU",
    "Kod oryginalny",
    "Nazwa",
    "EAN",
    "Stan",
    "Sug. stan",
    "Brak",
    "Sprzedaz 30d",
    "Sprzedaz 365d",
    "Zamowienia 365d",
    "Cena sklepowa brutto",
    "Cena Allegro brutto",
    "Wartosc stanu",
    "Allegro ID",
    "Materiał",
    "Kolor",
    "URL zdjecia",
    "Aktualizacja",
)
LOW_STOCK_ALERT_ROW_LIMIT = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "app.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
app.logger.setLevel(logging.INFO)
app.logger.propagate = True
if FLASK_SECRET_KEY_SOURCE == "generated":
    logging.getLogger(__name__).warning(
        "FLASK_SECRET_KEY nie ustawiony. Wygenerowano klucz i zapisano w settings (flask_secret_key)."
    )
SECRET_MIGRATION_RESULT = migrate_secret_storage(DB_PATH)
SECRET_STORAGE_STATUS = get_secret_storage_status(DB_PATH)
if SECRET_MIGRATION_RESULT["settings"] or SECRET_MIGRATION_RESULT["tokens"]:
    logging.getLogger(__name__).info(
        "Migrated encrypted secrets settings=%s tokens=%s",
        SECRET_MIGRATION_RESULT["settings"],
        SECRET_MIGRATION_RESULT["tokens"],
    )


def get_config_value(env_key, setting_key, default=None):
    env_value = os.getenv(env_key)
    if env_value:
        return env_value
    setting_value = get_setting(DB_PATH, setting_key)
    return setting_value if setting_value is not None else default


def normalize_base_url(value):
    if not value:
        return value
    base = value.rstrip("/")
    if base.endswith("/rest"):
        base = base[:-5]
    if base.endswith("/api"):
        base = base[:-4]
    return base


def build_apilo_product_url(apilo_product_id):
    base = normalize_base_url(
        get_config_value("APILO_BASE_URL", "apilo_base_url", "https://api.apilo.com")
    )
    parsed = urlparse(base or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "https://apilo.com/pl/logowanie/"
    return f"{base}/warehouse/product/detail/{int(apilo_product_id)}/"


def get_order_url_template():
    base = normalize_base_url(
        get_config_value("APILO_BASE_URL", "apilo_base_url", "https://api.apilo.com")
    )
    return f"{base}/order/order/detail/{{id}}/"


def build_order_url(order_id):
    template = get_order_url_template()
    base = normalize_base_url(
        get_config_value("APILO_BASE_URL", "apilo_base_url", "https://api.apilo.com")
    )
    return template.replace("{id}", str(order_id or "")).replace("{base}", base)


def get_client():
    return ApiloClient(
        base_url=normalize_base_url(
            get_config_value("APILO_BASE_URL", "apilo_base_url", "https://api.apilo.com")
        ),
        client_id=get_config_value("APILO_CLIENT_ID", "apilo_client_id"),
        client_secret=get_config_value("APILO_CLIENT_SECRET", "apilo_client_secret"),
        developer_id=None,
        db_path=DB_PATH,
        grant_type=os.getenv("APILO_GRANT_TYPE"),
        auth_token=os.getenv("APILO_AUTH_TOKEN"),
    )


def get_empik_client():
    shop_id = get_setting(DB_PATH, "empik_shop_id") or ""
    return EmpikClient(
        get_config_value("EMPIK_API_KEY", "empik_api_key"),
        shop_id=shop_id or None,
    )


def perform_empik_sync():
    client = get_empik_client()
    previous_offer_count = len(get_empik_offers(DB_PATH))
    previous_import_count = len(get_empik_offer_imports(DB_PATH, limit=1))
    offers = client.list_offers()
    apilo_offer_count = count_channel_listings(DB_PATH, "empik")
    if not offers and (
        apilo_offer_count or previous_offer_count or previous_import_count
    ):
        raise EmpikClientError(
            "API EmpikPlace zwróciło pustą listę mimo istniejącego snapshotu Empik. "
            "Poprzednie dane zostały zachowane."
        )
    imports = client.list_offer_imports(days=30)
    return replace_empik_snapshot(DB_PATH, offers, imports)


def run_empik_sync_with_lock(*, blocking=False):
    acquired = EMPIK_SYNC_LOCK.acquire(blocking=blocking)
    if not acquired:
        return None
    try:
        return perform_empik_sync()
    finally:
        EMPIK_SYNC_LOCK.release()


def get_sync_job_label(job):
    return get_runtime_sync_job_label(job)


def normalize_product_preset(value):
    return value if value in PRODUCT_PRESET_LABELS else "all"


def normalize_material_filter(value):
    allowed = set(PRODUCT_MATERIAL_OPTIONS)
    return value if value in allowed else ""


def normalize_manual_material(value):
    normalized = (value or "").strip().upper()
    normalized = normalized.replace("PET-G", "PETG").replace("PET G", "PETG")
    normalized = {"FLEX/GUMA": "FLEX", "GUMA": "FLEX"}.get(normalized, normalized)
    if normalized and normalized not in PRODUCT_MATERIAL_OPTIONS:
        raise ValueError("Wybierz materiał z dostępnej listy.")
    return normalized


def normalize_color_filter(value):
    return " ".join((value or "").split())


def default_sort_for_preset(preset):
    if preset == "high_value":
        return "stock_value", "desc"
    if preset == "no_sales":
        return "sales_year", "asc"
    if preset in {"out_of_stock", "no_ean", "no_image"}:
        return "name", "asc"
    return "shortage", "desc"


def normalize_sort_order(value, default):
    return value if value in {"asc", "desc"} else default


def build_product_list_state(args):
    search = args.get("search")
    material_filter = normalize_material_filter(args.get("material") or "")
    color_filter = normalize_color_filter(args.get("color") or "")
    preset = normalize_product_preset(args.get("preset") or "all")
    default_sort, default_order = default_sort_for_preset(preset)
    sort = args.get("sort") or default_sort
    order = normalize_sort_order(args.get("order") or default_order, default_order)
    page = parse_int_value(args.get("page"), 1, min_value=1)
    limit = parse_int_value(args.get("limit"), 50, min_value=1)
    if limit not in PRODUCT_PAGE_LIMITS:
        limit = 50
    lead_time_days = get_suggest_lead_time_days()
    safety_pct = get_suggest_safety_pct()
    suggest_days = get_suggest_days()
    return {
        "export": args.get("export") == "1",
        "search": search,
        "material_filter": material_filter,
        "color_filter": color_filter,
        "preset": preset,
        "sort": sort,
        "order": order,
        "page": page,
        "limit": limit,
        "offset": (page - 1) * limit,
        "lead_time_days": lead_time_days,
        "safety_pct": safety_pct,
        "suggest_days": suggest_days,
    }


def fetch_product_rows(list_state, *, limit=None, offset=None, unbounded=False):
    effective_limit = None if unbounded else (list_state["limit"] if limit is None else limit)
    effective_offset = list_state["offset"] if offset is None else offset
    return get_products(
        DB_PATH,
        search=list_state["search"],
        material_filter=list_state["material_filter"],
        color_filter=list_state["color_filter"],
        preset=list_state["preset"],
        sort=list_state["sort"],
        order=list_state["order"],
        limit=effective_limit,
        offset=effective_offset,
        lead_time_days=list_state["lead_time_days"],
        safety_pct=list_state["safety_pct"],
        suggest_days=list_state["suggest_days"],
    )


def serialize_product_export_row(product):
    return [
        product["apilo_id"] or "",
        product["sku"] or "",
        product["original_code"] or "",
        product["name"] or "",
        product["ean"] or "",
        product["quantity"] if product["quantity"] is not None else "",
        product["suggested_qty"] if product["suggested_qty"] is not None else "",
        product["shortage_qty"] if product["shortage_qty"] is not None else "",
        product["quantity_30d"] if product["quantity_30d"] is not None else "",
        product["quantity_year"] if product["quantity_year"] is not None else "",
        product["orders_year"] if product["orders_year"] is not None else "",
        product["price_with_tax"] if product["price_with_tax"] is not None else "",
        product["allegro_price_with_tax"] if product["allegro_price_with_tax"] is not None else "",
        product["stock_value"] if product["stock_value"] is not None else "",
        product["allegro_auction_id"] or "",
        product["material"] or "",
        product["color"] or "",
        product["image_url"] or "",
        format_pull_time(product["updated_at"] or ""),
    ]


def build_products_csv_response(list_state):
    export_rows = fetch_product_rows(list_state, offset=0, unbounded=True)
    record_audit_event(
        "products_export_csv",
        "settings",
        entity_label="Eksport produktów CSV",
        new_value=format_position_count(len(export_rows)),
        details={
            "search": list_state["search"] or "",
            "material": list_state["material_filter"],
            "color": list_state["color_filter"],
            "preset": list_state["preset"],
            "sort": list_state["sort"],
            "order": list_state["order"],
        },
    )
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(PRODUCT_EXPORT_COLUMNS)
    for product in export_rows:
        writer.writerow(serialize_product_export_row(product))
    filename = (
        f"produkty_{list_state['preset']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    response = app.response_class(
        output.getvalue(),
        mimetype="text/csv",
    )
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


def ensure_sync_schedule():
    ensure_runtime_sync_schedule(
        last_pull_at=get_setting(DB_PATH, "last_pull_at"),
        sales_cache_at=get_setting(DB_PATH, "sales_cache_at"),
        refresh_interval_seconds=REFRESH_INTERVAL_SECONDS,
        sales_cache_refresh_interval_seconds=SALES_CACHE_REFRESH_INTERVAL_SECONDS,
    )


def update_sync_status(**changes):
    update_runtime_sync_status(**changes)


def schedule_inventory_sync(reference_time=None, retry=False):
    schedule_runtime_inventory_sync(
        REFRESH_INTERVAL_SECONDS,
        reference_time=reference_time,
        retry=retry,
    )


def schedule_sales_refresh(reference_time=None, retry=False):
    schedule_runtime_sales_refresh(
        SALES_CACHE_REFRESH_INTERVAL_SECONDS,
        reference_time=reference_time,
        retry=retry,
    )


def mark_sync_failed(job, exc):
    del job
    mark_runtime_sync_failed(
        public_error_message(exc, default="Synchronizacja nie powiodła się.")
    )


def should_refresh_year_sales_cache(force=False):
    return should_refresh_runtime_year_sales_cache(
        get_setting(DB_PATH, "sales_year_cache_at"),
        get_suggest_days(),
        SALES_YEAR_REFRESH_INTERVAL_SECONDS,
        force=force,
    )


def build_sync_status_payload():
    ensure_sync_schedule()
    return build_runtime_sync_status_payload(
        last_pull_at=get_setting(DB_PATH, "last_pull_at"),
        sales_cache_at=get_setting(DB_PATH, "sales_cache_at"),
        sales_year_cache_at=get_setting(DB_PATH, "sales_year_cache_at"),
    )


def get_suggest_lead_time_days():
    return parse_int_value(get_setting(DB_PATH, "suggest_lead_time_days"), 1, min_value=1)


def get_suggest_safety_pct():
    return parse_float_value(get_setting(DB_PATH, "suggest_safety_pct"), 20.0, min_value=0.0)


def get_suggest_days():
    parsed = parse_int_value(get_setting(DB_PATH, "suggest_days"), 30, min_value=1)
    return parsed if parsed in (30, 60, 120, 180, 365) else 30


def get_allegro_price_list_id():
    return parse_int_value(get_setting(DB_PATH, "allegro_price_list_id"), 20, min_value=1)


def get_low_stock_rows(limit=10):
    return get_runtime_low_stock_rows(
        DB_PATH,
        get_suggest_lead_time_days(),
        get_suggest_safety_pct(),
        get_suggest_days(),
        limit=limit,
    )


def get_low_stock_alert_enabled():
    return get_runtime_low_stock_alert_enabled(DB_PATH)


def get_low_stock_alert_interval_hours():
    return get_runtime_low_stock_alert_interval_hours(DB_PATH)


def summarize_low_stock_alert_settings_snapshot(enabled, interval_hours):
    return summarize_runtime_low_stock_alert_settings_snapshot(enabled, interval_hours)


def format_item_count(count, singular, paucal, plural):
    return format_alert_item_count(count, singular, paucal, plural)


def format_position_count(count):
    return format_alert_position_count(count)


def get_low_stock_alert_next_check_iso():
    return get_runtime_low_stock_alert_next_check_iso(DB_PATH, compute_next_run_at)


def is_low_stock_alert_due(now=None):
    return is_runtime_low_stock_alert_due(
        DB_PATH,
        compute_next_run_at,
        now=now,
    )


def build_low_stock_alert_signature(rows):
    from app_alerts import build_low_stock_alert_signature as build_runtime_low_stock_alert_signature

    return build_runtime_low_stock_alert_signature(rows)


def update_low_stock_alert_state(
    *,
    checked_at=None,
    result_message=None,
    error_message=None,
    signature=None,
    sent_count=None,
    sent_at=None,
):
    update_runtime_low_stock_alert_state(
        DB_PATH,
        checked_at=checked_at,
        result_message=result_message,
        error_message=error_message,
        signature=signature,
        sent_count=sent_count,
        sent_at=sent_at,
    )


def mark_low_stock_alert_error(message, *, mode="auto"):
    mark_runtime_low_stock_alert_error(
        DB_PATH,
        message,
        mode=mode,
    )


def process_low_stock_alert(mode="manual"):
    return process_runtime_low_stock_alert(
        DB_PATH,
        mode=mode,
        low_stock_row_limit=LOW_STOCK_ALERT_ROW_LIMIT,
        get_low_stock_rows_fn=get_low_stock_rows,
        send_low_stock_alert_email_fn=send_low_stock_alert_email,
        record_audit_event_fn=record_audit_event,
        format_position_count_fn=format_position_count,
    )


def run_low_stock_alert_with_lock(blocking):
    acquired = SYNC_LOCK.acquire(blocking=blocking)
    if not acquired:
        return None
    try:
        return process_low_stock_alert(mode="auto")
    finally:
        SYNC_LOCK.release()


def build_low_stock_alert_history(limit=10):
    return build_runtime_low_stock_alert_history(
        DB_PATH,
        limit=limit,
        format_pull_time_fn=format_pull_time,
        format_position_count_fn=format_position_count,
    )


def record_audit_event(
    action,
    entity_type,
    entity_id=None,
    entity_label=None,
    old_value=None,
    new_value=None,
    details=None,
    actor_ip=None,
):
    resolved_ip = actor_ip
    if resolved_ip is None:
        resolved_ip = get_client_ip() if has_request_context() else ""
    write_audit_event(
        DB_PATH,
        app.logger,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        old_value=old_value,
        new_value=new_value,
        details=details,
        actor_ip=resolved_ip,
    )


def get_client_ip():
    return resolve_client_ip(TRUST_X_FORWARDED_FOR)


def is_local_setup_request():
    return is_local_setup_request_for_ip(get_client_ip())


def login_window_start_iso():
    return build_login_window_start_iso(LOGIN_RATE_LIMIT_WINDOW_SECONDS)


def is_login_rate_limited(client_ip):
    return check_login_rate_limited(
        DB_PATH,
        client_ip,
        LOGIN_RATE_LIMIT_WINDOW_SECONDS,
        LOGIN_RATE_LIMIT_MAX_ATTEMPTS,
    )


def setup_token_required():
    return auth_setup_token_required(APP_SETUP_TOKEN, is_local_setup_request())


def run_sync_pull_with_lock(blocking):
    acquired = SYNC_LOCK.acquire(blocking=blocking)
    if not acquired:
        return None
    mark_sync_started("inventory")
    try:
        count = perform_sync_pull()
        mark_sync_finished("inventory")
        schedule_inventory_sync()
        return count
    except Exception as exc:
        mark_sync_failed("inventory", exc)
        schedule_inventory_sync(retry=True)
        raise
    finally:
        SYNC_LOCK.release()


def run_suggestions_refresh_with_lock(blocking, force_year=False):
    acquired = SYNC_LOCK.acquire(blocking=blocking)
    if not acquired:
        return False
    mark_sync_started("sales_cache")
    try:
        refresh_suggestions_cache(force_year=force_year)
        mark_sync_finished("sales_cache")
        schedule_sales_refresh()
        return True
    except Exception as exc:
        mark_sync_failed("sales_cache", exc)
        schedule_sales_refresh(retry=True)
        raise
    finally:
        SYNC_LOCK.release()


@app.before_request
def require_csrf():
    if request.method == "POST":
        if not validate_csrf():
            return ("Bad Request", 400)


def tokens_missing():
    return auth_tokens_missing(DB_PATH)


def password_missing():
    return auth_password_missing(DB_PATH, APP_PASSWORD)


def render_setup_password(status_code=200):
    return render_auth_setup_password(
        require_setup_token=setup_token_required(),
        remote_setup_blocked=(not APP_SETUP_TOKEN and not is_local_setup_request()),
        status_code=status_code,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if password_missing():
        return redirect(url_for("setup_password"))
    if request.method == "POST":
        client_ip = get_client_ip()
        if is_login_rate_limited(client_ip):
            app.logger.warning("Blocked login by rate limit ip=%s path=%s", client_ip, request.path)
            flash(
                "Za dużo nieudanych prób logowania. Odczekaj kilka minut i spróbuj ponownie.",
                "error",
            )
            return render_template("login.html"), 429
        password = request.form.get("password")
        if APP_PASSWORD:
            valid = password and password == APP_PASSWORD
        else:
            password_hash = get_setting(DB_PATH, "password_hash")
            valid = password and password_hash and check_password_hash(
                password_hash, password
            )
        if valid:
            if client_ip and client_ip != "unknown":
                clear_login_attempts(DB_PATH, client_ip)
            session.clear()
            session.permanent = True
            session["logged_in"] = True
            session["logged_in_at"] = utc_now_iso()
            dest = request.args.get("next")
            if not is_safe_redirect_target(dest):
                dest = url_for("index")
            record_audit_event(
                "login_success",
                "auth",
                entity_label="Panel",
                new_value="ok",
                details={"next": dest},
            )
            return redirect(dest)
        if client_ip and client_ip != "unknown":
            record_login_attempt(DB_PATH, client_ip)
        app.logger.warning("Failed login attempt ip=%s path=%s", client_ip, request.path)
        flash("Nieprawidłowe hasło.", "error")
    return render_template("login.html")


@app.route("/setup-password", methods=["GET", "POST"])
def setup_password():
    if not password_missing():
        return redirect(url_for("login"))
    if request.method == "POST":
        client_ip = get_client_ip()
        if setup_token_required():
            provided_setup_token = request.form.get("setup_token") or ""
            if not secrets.compare_digest(provided_setup_token, APP_SETUP_TOKEN):
                app.logger.warning("Blocked password setup with invalid token ip=%s", client_ip)
                flash("Nieprawidłowy token konfiguracji.", "error")
                return render_setup_password()
        elif not is_local_setup_request():
            app.logger.warning("Blocked remote password setup ip=%s", client_ip)
            flash(
                "Pierwsze ustawienie hasła jest dozwolone tylko lokalnie lub z tokenem konfiguracji.",
                "error",
            )
            return render_setup_password(status_code=403)
        password = request.form.get("password")
        confirm = request.form.get("confirm")
        if not password or len(password) < 8:
            flash("Hasło musi mieć minimum 8 znaków.", "error")
            return render_setup_password()
        if password != confirm:
            flash("Hasła nie są zgodne.", "error")
            return render_setup_password()
        set_setting(DB_PATH, "password_hash", generate_password_hash(password))
        record_audit_event(
            "password_setup",
            "security",
            entity_label="Hasło panelu",
            old_value="brak",
            new_value="ustawione",
            details={"setup_token_required": setup_token_required()},
        )
        flash("Hasło ustawione. Zaloguj się.", "success")
        return redirect(url_for("login"))
    if not APP_SETUP_TOKEN and not is_local_setup_request():
        app.logger.warning("Blocked remote password setup form ip=%s", get_client_ip())
        flash(
            "Pierwsze ustawienie hasła jest dozwolone tylko lokalnie lub z tokenem konfiguracji.",
            "error",
        )
        return render_setup_password(status_code=403)
    return render_setup_password()


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def build_color_badge_style(color):
    styles = {
        "czarny": "--badge-bg:#111;--badge-border:#111;--badge-fg:#fff;",
        "biały": "--badge-bg:#fff;--badge-border:#cfcfcf;--badge-fg:#222;",
        "szary": "--badge-bg:#9ca3af;--badge-border:#6b7280;--badge-fg:#111;",
        "grafitowy": "--badge-bg:#374151;--badge-border:#1f2937;--badge-fg:#fff;",
        "czerwony": "--badge-bg:#b91c1c;--badge-border:#7f1d1d;--badge-fg:#fff;",
        "zielony": "--badge-bg:#22c55e;--badge-border:#15803d;--badge-fg:#052e16;",
        "niebieski": "--badge-bg:#1d4ed8;--badge-border:#1e3a8a;--badge-fg:#fff;",
        "żółty": "--badge-bg:#fde047;--badge-border:#eab308;--badge-fg:#422006;",
        "pomarańczowy": "--badge-bg:#fb923c;--badge-border:#ea580c;--badge-fg:#431407;",
        "fioletowy": "--badge-bg:#7e22ce;--badge-border:#581c87;--badge-fg:#fff;",
        "różowy": "--badge-bg:#f9a8d4;--badge-border:#db2777;--badge-fg:#500724;",
        "brązowy": "--badge-bg:#92400e;--badge-border:#78350f;--badge-fg:#fff;",
        "beżowy": "--badge-bg:#ead8b7;--badge-border:#c6a86b;--badge-fg:#3f2f16;",
        "srebrny": "--badge-bg:#d1d5db;--badge-border:#9ca3af;--badge-fg:#111827;",
        "złoty": "--badge-bg:#facc15;--badge-border:#ca8a04;--badge-fg:#422006;",
        "transparentny": "--badge-bg:rgba(255,255,255,0.55);--badge-border:#9ca3af;--badge-fg:#111827;",
        "naturalny": "--badge-bg:#f5f5dc;--badge-border:#d6d3a3;--badge-fg:#2f2f19;",
        "wielokolorowy": "--badge-bg:#f3f4f6;--badge-border:#7c3aed;--badge-fg:#111827;",
    }
    return styles.get(color or "", "")


@app.route("/")
@login_required
def index():
    if tokens_missing():
        return redirect(url_for("settings"))
    list_state = build_product_list_state(request.args)
    if list_state["export"]:
        return build_products_csv_response(list_state)
    products = fetch_product_rows(list_state)
    total_count = get_products_count(
        DB_PATH,
        search=list_state["search"],
        material_filter=list_state["material_filter"],
        color_filter=list_state["color_filter"],
        preset=list_state["preset"],
        lead_time_days=list_state["lead_time_days"],
        safety_pct=list_state["safety_pct"],
        suggest_days=list_state["suggest_days"],
    )
    total_pages = max(1, (total_count + list_state["limit"] - 1) // list_state["limit"])
    if list_state["page"] > total_pages:
        list_state["page"] = total_pages
        list_state["offset"] = (list_state["page"] - 1) * list_state["limit"]
        products = fetch_product_rows(list_state)
    dashboard = get_dashboard_metrics(
        DB_PATH,
        lead_time_days=list_state["lead_time_days"],
        safety_pct=list_state["safety_pct"],
        suggest_days=list_state["suggest_days"],
    )
    preset_counts = {
        "all": dashboard.get("total_products", 0) or 0,
        "shortage": dashboard.get("shortage_count", 0) or 0,
        "out_of_stock": dashboard.get("out_of_stock_count", 0) or 0,
        "no_ean": dashboard.get("missing_ean_count", 0) or 0,
        "no_image": dashboard.get("missing_image_count", 0) or 0,
        "no_sales": dashboard.get("no_sales_count", 0) or 0,
        "high_value": dashboard.get("high_value_count", 0) or 0,
    }
    attribute_filter_counts = get_attribute_filter_counts(DB_PATH)
    preset_options = [
        {
            "id": key,
            "label": label,
            "count": preset_counts.get(key, 0),
            "sort": default_sort_for_preset(key)[0],
            "order": default_sort_for_preset(key)[1],
        }
        for key, label in PRODUCT_PRESET_LABELS.items()
    ]
    details_cache = get_sales_cache_details_map(DB_PATH)
    year_summary = get_sales_year_map(DB_PATH)
    suggestions = {}
    suggest_details = {}
    for product in products:
        ean = product["ean"]
        if not ean:
            continue
        suggested = product["suggested_qty"]
        if suggested is not None:
            suggestions[ean] = max(int(suggested), 0)
        details = details_cache.get(ean)
        if details:
            suggest_details[ean] = [
                {
                    "date": item.get("date"),
                    "qty": item.get("qty"),
                    "order_id": item.get("order_id"),
                    "url": build_order_url(item.get("order_id", "")),
                }
                for item in details
                if item.get("order_id")
            ]
    return render_template(
        "index.html",
        products=products,
        search=list_state["search"] or "",
        material_filter=list_state["material_filter"],
        color_filter=list_state["color_filter"],
        attribute_filter_counts=attribute_filter_counts,
        preset=list_state["preset"],
        preset_label=PRODUCT_PRESET_LABELS.get(
            list_state["preset"],
            PRODUCT_PRESET_LABELS["all"],
        ),
        preset_options=preset_options,
        sort=list_state["sort"],
        order=list_state["order"],
        page=list_state["page"],
        total_pages=total_pages,
        total_count=total_count,
        limit=list_state["limit"],
        dashboard=dashboard,
        suggestions=suggestions,
        suggest_details=suggest_details,
        year_summary=year_summary,
        suggest_lead_time_days=list_state["lead_time_days"],
        suggest_safety_pct=list_state["safety_pct"],
        sync_status=build_sync_status_payload(),
        product_detail_base_url=normalize_base_url(
            get_config_value("APILO_BASE_URL", "apilo_base_url", "https://api.apilo.com")
        ),
        color_badge_style=build_color_badge_style,
    )


@app.get("/sales-channels")
@login_required
def sales_channels():
    if tokens_missing():
        return redirect(url_for("settings"))
    all_channels = get_sales_channels(DB_PATH)
    show_etsy = get_setting(DB_PATH, "sales_channels_show_etsy") == "1"
    channels = [
        channel
        for channel in all_channels
        if show_etsy or channel["channel_key"] != "etsy"
    ]
    listings = get_channel_listings(DB_PATH)
    products = get_products(DB_PATH, sort="name", order="asc", limit=None)
    search = (request.args.get("search") or "").strip()
    channel_filter = (request.args.get("channel") or "").strip()
    valid_channels = {channel["channel_key"] for channel in channels}
    if channel_filter not in valid_channels:
        channel_filter = ""
    status_filter = (request.args.get("status") or "").strip()
    if status_filter not in {"ok", "missing", "review"}:
        status_filter = ""
    description_filter = (request.args.get("description") or "").strip()
    if description_filter not in {
        "match",
        "mismatch",
        "unavailable",
        "unverified",
        "no_reference",
    }:
        description_filter = ""
    palette_filter = (request.args.get("palette") or "").strip()
    if palette_filter not in {"match", "mismatch", "unavailable", "unverified"}:
        palette_filter = ""
    limit = parse_int_value(request.args.get("limit"), 50, min_value=25, max_value=200)
    if limit not in PRODUCT_PAGE_LIMITS:
        limit = 50
    page = parse_int_value(request.args.get("page"), 1, min_value=1)
    empik_api_key_set = bool(get_config_value("EMPIK_API_KEY", "empik_api_key"))
    empik_last_sync_raw = get_setting(DB_PATH, "empik_last_sync_at") or ""
    empik_api_enabled = empik_api_key_set and bool(empik_last_sync_raw)
    empik_offers = get_empik_offers(DB_PATH) if empik_api_enabled else []
    description_references = get_apilo_description_references(DB_PATH)
    description_checks = get_channel_description_checks(DB_PATH)
    description_reference_status = get_apilo_description_reference_status(DB_PATH)
    description_last_checked_raw = max(
        (str(item.get("checked_at") or "") for item in description_checks),
        default="",
    )
    matrix = build_channel_matrix(
        products,
        channels,
        listings,
        search=search,
        channel_filter=channel_filter,
        status_filter=status_filter,
        page=page,
        limit=limit,
        empik_offers=empik_offers,
        empik_api_enabled=empik_api_enabled,
        description_references=description_references,
        description_checks=description_checks,
        description_filter=description_filter,
        palette_filter=palette_filter,
    )
    last_updated = format_pull_time(all_channels[0]["updated_at"]) if all_channels else ""
    empik_status = {
        "verified": empik_api_enabled,
    }
    return render_template(
        "sales_channels.html",
        channels=channels,
        matrix=matrix,
        search=search,
        channel_filter=channel_filter,
        status_filter=status_filter,
        description_filter=description_filter,
        palette_filter=palette_filter,
        limit=limit,
        last_updated=last_updated,
        description_last_checked=format_pull_time(description_last_checked_raw),
        description_reference_status=description_reference_status,
        empik_status=empik_status,
    )


def recheck_public_channel_description(target, reference):
    from scripts.check_channel_descriptions import _check_target

    return _check_target(target, reference, None)


def allegro_description_write_configured():
    return get_allegro_credential_store().is_configured()


def get_allegro_credential_store():
    return AllegroFileCredentialStore(
        env_path=os.getenv(
            "ALLEGRO_WRITE_ENV_FILE", "/run/secrets/allegro.env"
        ),
        token_path=os.getenv(
            "ALLEGRO_WRITE_TOKEN_FILE", "/run/secrets/allegro/tokens.json"
        ),
    )


def get_allegro_description_updater():
    return get_allegro_credential_store().locked_updater()


def get_erli_write_env_path():
    return os.getenv("ERLI_WRITE_ENV_FILE", "/run/secrets/erli.env")


def erli_palette_write_configured():
    return erli_write_configured(get_erli_write_env_path())


def get_erli_palette_updater():
    return erli_updater_from_env(get_erli_write_env_path())


def update_allegro_offer_description(target, reference):
    from scripts.check_channel_descriptions import _check_target

    with get_allegro_description_updater() as updater:
        update = updater.update_primary_description(
            target["external_id"],
            reference["description_html"],
            reference["description_text"],
        )
    result = _check_target(target, reference, update["access_token"])
    if result["status"] != "match":
        raise AllegroDescriptionUnverifiedError(
            "Nie udało się potwierdzić zgodności opisu po zapisie."
        )
    return update["outcome"], result


def _reference_palette_material(reference):
    parsed = parse_material_color(reference["description_text"] or "")
    normalized = normalize_palette_material(parsed.get("material"))
    return "PLA" if normalized == "PLA+" else normalized


def update_allegro_offer_palette(target, reference, expected_material):
    from scripts.check_channel_descriptions import _check_target

    with get_allegro_description_updater() as updater:
        update = updater.update_material_palette(
            target["external_id"], expected_material
        )
    result = _check_target(target, reference, update["access_token"])
    if (
        result.get("palette_status") != "match"
        or result.get("palette_material") != expected_material
    ):
        raise AllegroDescriptionUnverifiedError(
            "Nie udało się potwierdzić bloku materiału i kolorów po zapisie."
        )
    return (
        update["outcome"],
        result,
        list(update.get("catalog_parameter_ids") or []),
    )


def update_erli_offer_palette(target, reference, expected_material):
    from scripts.check_channel_descriptions import build_structured_description_result

    updater = get_erli_palette_updater()
    update = updater.update_material_palette(
        target["external_id"],
        expected_material,
        expected_ean=target.get("ean") or "",
    )
    result = build_structured_description_result(
        target,
        reference,
        update["product"]["description"],
        "erli_api",
    )
    if (
        result.get("palette_status") != "match"
        or result.get("palette_material") != expected_material
    ):
        raise ErliPaletteUnverifiedError(
            "Nie udało się potwierdzić bloku materiału i kolorów ERLI."
        )
    return update["outcome"], result, bool(update["verified_after_error"])


def _palette_context(checks):
    relevant = [
        check
        for check in checks
        if check.get("channel_key") in {"allegro", "erli"}
        and check.get("palette_status") in {"match", "mismatch"}
    ]
    materials = {
        str(check.get("palette_material") or "").strip()
        for check in relevant
        if check.get("palette_material")
    }
    return {
        "required": bool(relevant),
        "material": next(iter(materials)) if len(materials) == 1 else "",
        "conflict": len(materials) > 1,
    }


def _resolved_palette_status(channel_key, check, context):
    if channel_key == "empik":
        return "unavailable"
    if channel_key not in {"allegro", "erli"}:
        return "not_applicable"
    if not check:
        return "unverified"
    status = str(check.get("palette_status") or "unverified")
    if context["conflict"] and status in {"match", "mismatch"}:
        return "mismatch"
    if status == "absent" and context["required"]:
        return "missing"
    return status


def _palette_status_label(status, material=""):
    if status == "match" and material:
        return f"Kolory {material} zgodne"
    if status == "missing" and material:
        return f"Brak bloku kolorów {material}"
    return PALETTE_STATUS_LABELS.get(status, "Blok kolorów niesprawdzony")


@app.get("/sales-channels/apilo-description/<int:apilo_product_id>")
@login_required
def sales_channel_apilo_description(apilo_product_id):
    reference = get_apilo_description_reference(DB_PATH, apilo_product_id)
    product_row = get_product_by_apilo_id(DB_PATH, apilo_product_id)
    if not reference or not product_row:
        return abort(404)
    product = dict(product_row)
    check_rows = [
        item
        for item in get_channel_description_checks(
            DB_PATH, apilo_product_id=apilo_product_id
        )
        if item.get("reference_hash") == reference["description_hash"]
    ]
    checks = {
        (str(item["channel_key"]), str(item["external_id"])): item
        for item in check_rows
    }
    palette_context = _palette_context(check_rows)
    reference_palette_material = _reference_palette_material(reference)
    stored_palette_material = normalize_palette_material(product.get("material"))
    if stored_palette_material == "PLA+":
        stored_palette_material = "PLA"
    palette_material_consistent = (
        not stored_palette_material
        or stored_palette_material == reference_palette_material
    )
    channel_names = {
        str(item["channel_key"]): str(item["channel_name"])
        for item in get_sales_channels(DB_PATH)
    }
    allegro_write_ready = allegro_description_write_configured()
    erli_write_ready = erli_palette_write_configured()
    repairs = []
    for listing in get_channel_listings_for_product(DB_PATH, apilo_product_id):
        external_id = str(listing.get("external_id") or "").strip()
        if not external_id:
            continue
        channel_key = str(listing["channel_key"])
        check = checks.get((channel_key, external_id))
        status = str((check or {}).get("status") or "unverified")
        description_diff = None
        if status == "mismatch" and check:
            candidate_diff = build_description_diff(
                reference["description_preview"] or reference["description_text"],
                check.get("actual_description_text") or "",
            )
            if candidate_diff["available"]:
                description_diff = candidate_diff
        palette_status = _resolved_palette_status(
            channel_key, check, palette_context
        )
        palette_material = str(
            (check or {}).get("palette_material")
            or palette_context["material"]
            or reference_palette_material
            or ""
        )
        palette_diff = None
        if palette_status == "mismatch" and check:
            expected_palette = canonical_material_palette_text(palette_material)
            candidate_palette_diff = build_description_diff(
                expected_palette,
                check.get("palette_block_text") or "",
            )
            if candidate_palette_diff["available"]:
                palette_diff = candidate_palette_diff
        repairs.append(
            {
                "channel_key": channel_key,
                "channel_name": channel_names.get(channel_key, channel_key),
                "external_id": external_id,
                "status": status,
                "status_label": DESCRIPTION_STATUS_LABELS.get(status, "Opis niesprawdzony"),
                "palette_status": palette_status,
                "palette_status_label": _palette_status_label(
                    palette_status, palette_material
                ),
                "palette_material": palette_material,
                "needs_repair": (
                    status in {"mismatch", "error", "unverified"}
                    or palette_status in {"mismatch", "missing", "unverified"}
                ),
                "public_url": build_listing_url(channel_key, listing, product),
                "recheck_supported": channel_key in PUBLIC_DESCRIPTION_RECHECK_CHANNELS,
                "write_supported": (
                    channel_key == "allegro"
                    and allegro_write_ready
                    and status in {"mismatch", "error", "unverified"}
                ),
                "palette_write_supported": (
                    (
                        (channel_key == "allegro" and allegro_write_ready)
                        or (channel_key == "erli" and erli_write_ready)
                    )
                    and palette_status in {"mismatch", "missing"}
                    and bool(reference_palette_material)
                    and palette_material_consistent
                    and not palette_context["conflict"]
                ),
                "expected_palette_material": reference_palette_material,
                "diff": description_diff,
                "palette_diff": palette_diff,
            }
        )
    response = jsonify(
        {
            "apilo_product_id": apilo_product_id,
            "name": product.get("name") or f"Produkt {apilo_product_id}",
            "ean": product.get("ean") or "",
            "description_preview": reference["description_preview"]
            or reference["description_text"],
            "description_html": reference["description_html"]
            or reference["description_preview"]
            or reference["description_text"],
            "imported_at": reference["imported_at"],
            "reference_hash": reference["description_hash"],
            "reference_palette_material": reference_palette_material,
            "apilo_url": build_apilo_product_url(apilo_product_id),
            "repairs": repairs,
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.post("/sales-channels/update-allegro-description")
@login_required
def sales_channel_update_allegro_description():
    apilo_product_id = parse_int_value(
        request.form.get("apilo_product_id"), 0, min_value=1
    )
    external_id = str(request.form.get("external_id") or "").strip()
    submitted_hash = str(request.form.get("reference_hash") or "").strip()
    reference = get_apilo_description_reference(DB_PATH, apilo_product_id)
    product_row = get_product_by_apilo_id(DB_PATH, apilo_product_id)
    listing = next(
        (
            item
            for item in get_channel_listings_for_product(DB_PATH, apilo_product_id)
            if item["channel_key"] == "allegro"
            and str(item.get("external_id") or "") == external_id
        ),
        None,
    )
    if not reference or not product_row or not listing:
        return jsonify({"error": "Nie znaleziono powiązanej oferty Allegro."}), 404
    if not submitted_hash or submitted_hash != reference["description_hash"]:
        return (
            jsonify(
                {
                    "error": "Wzorzec Apilo zmienił się. Otwórz podgląd opisu ponownie."
                }
            ),
            409,
        )
    if not allegro_description_write_configured():
        return jsonify({"error": "Zapis Allegro nie jest skonfigurowany."}), 503
    if not ALLEGRO_DESCRIPTION_WRITE_LOCK.acquire(blocking=False):
        return jsonify({"error": "Inna aktualizacja Allegro jest już w toku."}), 409

    target = {**dict(product_row), **dict(listing)}
    try:
        outcome, result = update_allegro_offer_description(target, reference)
        upsert_channel_description_check(DB_PATH, result)
        current_checks = [
            item
            for item in get_channel_description_checks(
                DB_PATH, apilo_product_id=apilo_product_id
            )
            if item.get("reference_hash") == reference["description_hash"]
        ]
        palette_context = _palette_context(current_checks)
        palette_status = _resolved_palette_status(
            "allegro", result, palette_context
        )
        palette_material = str(
            result.get("palette_material") or palette_context["material"] or ""
        )
        palette_diff = None
        if palette_status == "mismatch":
            candidate_diff = build_description_diff(
                canonical_material_palette_text(palette_material),
                result.get("palette_block_text") or "",
            )
            if candidate_diff["available"]:
                palette_diff = candidate_diff
        is_match = palette_status in {"match", "absent", "not_applicable"}
        record_audit_event(
            "allegro_description_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value=f"allegro: match ({outcome})",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "outcome": outcome,
                "reference_hash": reference["description_hash"],
            },
        )
        response = jsonify(
            {
                "status": "match",
                "status_label": DESCRIPTION_STATUS_LABELS["match"],
                "description_updated": True,
                "is_match": is_match,
                "needs_repair": not is_match,
                "diff": None,
                "palette_status": palette_status,
                "palette_status_label": _palette_status_label(
                    palette_status, palette_material
                ),
                "palette_diff": palette_diff,
                "outcome": outcome,
                "message": "Opis Allegro jest zgodny ze wzorcem Apilo.",
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except AllegroDescriptionUnverifiedError as exc:
        record_audit_event(
            "allegro_description_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: niepotwierdzony",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except AllegroDescriptionUpdateError as exc:
        record_audit_event(
            "allegro_description_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: błąd",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        app.logger.error(
            "Allegro description update failed: %s", type(exc).__name__
        )
        record_audit_event(
            "allegro_description_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: błąd",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": "Nie udało się zaktualizować opisu Allegro."}), 502
    finally:
        ALLEGRO_DESCRIPTION_WRITE_LOCK.release()


@app.post("/sales-channels/update-allegro-palette")
@login_required
def sales_channel_update_allegro_palette():
    apilo_product_id = parse_int_value(
        request.form.get("apilo_product_id"), 0, min_value=1
    )
    external_id = str(request.form.get("external_id") or "").strip()
    submitted_hash = str(request.form.get("reference_hash") or "").strip()
    reference = get_apilo_description_reference(DB_PATH, apilo_product_id)
    product_row = get_product_by_apilo_id(DB_PATH, apilo_product_id)
    listing = next(
        (
            item
            for item in get_channel_listings_for_product(DB_PATH, apilo_product_id)
            if item["channel_key"] == "allegro"
            and str(item.get("external_id") or "") == external_id
        ),
        None,
    )
    if not reference or not product_row or not listing:
        return jsonify({"error": "Nie znaleziono powiązanej oferty Allegro."}), 404
    if not submitted_hash or submitted_hash != reference["description_hash"]:
        return (
            jsonify(
                {
                    "error": "Wzorzec Apilo zmienił się. Otwórz podgląd opisu ponownie."
                }
            ),
            409,
        )
    expected_material = _reference_palette_material(reference)
    stored_material = normalize_palette_material(product_row["material"])
    if stored_material == "PLA+":
        stored_material = "PLA"
    if not expected_material:
        return jsonify({"error": "Nie rozpoznano materiału PLA lub PETG."}), 409
    if stored_material and stored_material != expected_material:
        return jsonify({"error": "Materiał produktu wymaga ręcznej weryfikacji."}), 409
    if not allegro_description_write_configured():
        return jsonify({"error": "Zapis Allegro nie jest skonfigurowany."}), 503
    if not ALLEGRO_DESCRIPTION_WRITE_LOCK.acquire(blocking=False):
        return jsonify({"error": "Inna aktualizacja Allegro jest już w toku."}), 409

    target = {**dict(product_row), **dict(listing)}
    try:
        outcome, result, catalog_parameter_ids = update_allegro_offer_palette(
            target, reference, expected_material
        )
        upsert_channel_description_check(DB_PATH, result)
        description_status = str(result.get("status") or "unverified")
        palette_status = str(result.get("palette_status") or "unverified")
        is_match = description_status == "match" and palette_status == "match"
        message = f"Blok materiału i kolorów {expected_material} jest zgodny."
        if catalog_parameter_ids:
            message += (
                f" Wyrównano też {len(catalog_parameter_ids)} "
                "parametry produktu do katalogu Allegro."
            )
        record_audit_event(
            "allegro_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value=f"allegro: kolory {expected_material} zgodne ({outcome})",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "outcome": outcome,
                "reference_hash": reference["description_hash"],
                "material": expected_material,
                "catalog_parameter_ids": catalog_parameter_ids,
            },
        )
        response = jsonify(
            {
                "status": description_status,
                "status_label": DESCRIPTION_STATUS_LABELS.get(
                    description_status, "Opis niesprawdzony"
                ),
                "description_updated": False,
                "palette_updated": True,
                "is_match": is_match,
                "needs_repair": not is_match,
                "diff": None,
                "palette_status": palette_status,
                "palette_status_label": _palette_status_label(
                    palette_status, expected_material
                ),
                "palette_material": expected_material,
                "palette_diff": None,
                "outcome": outcome,
                "catalog_parameter_count": len(catalog_parameter_ids),
                "message": message,
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except AllegroDescriptionUnverifiedError as exc:
        record_audit_event(
            "allegro_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: kolory niepotwierdzone",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except AllegroDescriptionUpdateError as exc:
        record_audit_event(
            "allegro_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: błąd kolorów",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        app.logger.error("Allegro palette update failed: %s", type(exc).__name__)
        record_audit_event(
            "allegro_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="allegro: błąd kolorów",
            details={
                "channel": "allegro",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return (
            jsonify({"error": "Nie udało się zaktualizować kolorów Allegro."}),
            502,
        )
    finally:
        ALLEGRO_DESCRIPTION_WRITE_LOCK.release()


@app.post("/sales-channels/update-erli-palette")
@login_required
def sales_channel_update_erli_palette():
    apilo_product_id = parse_int_value(
        request.form.get("apilo_product_id"), 0, min_value=1
    )
    external_id = str(request.form.get("external_id") or "").strip()
    submitted_hash = str(request.form.get("reference_hash") or "").strip()
    reference = get_apilo_description_reference(DB_PATH, apilo_product_id)
    product_row = get_product_by_apilo_id(DB_PATH, apilo_product_id)
    listing = next(
        (
            item
            for item in get_channel_listings_for_product(DB_PATH, apilo_product_id)
            if item["channel_key"] == "erli"
            and str(item.get("external_id") or "") == external_id
        ),
        None,
    )
    if not reference or not product_row or not listing:
        return jsonify({"error": "Nie znaleziono powiązanego produktu ERLI."}), 404
    if not submitted_hash or submitted_hash != reference["description_hash"]:
        return (
            jsonify(
                {
                    "error": "Wzorzec Apilo zmienił się. Otwórz podgląd opisu ponownie."
                }
            ),
            409,
        )
    expected_material = _reference_palette_material(reference)
    stored_material = normalize_palette_material(product_row["material"])
    if stored_material == "PLA+":
        stored_material = "PLA"
    current_checks = [
        item
        for item in get_channel_description_checks(
            DB_PATH, apilo_product_id=apilo_product_id
        )
        if item.get("reference_hash") == reference["description_hash"]
    ]
    if _palette_context(current_checks)["conflict"]:
        return jsonify({"error": "Materiał kanałów wymaga ręcznej weryfikacji."}), 409
    if not expected_material:
        return jsonify({"error": "Nie rozpoznano materiału PLA lub PETG."}), 409
    if stored_material and stored_material != expected_material:
        return jsonify({"error": "Materiał produktu wymaga ręcznej weryfikacji."}), 409
    if not erli_palette_write_configured():
        return jsonify({"error": "Zapis ERLI nie jest skonfigurowany."}), 503
    if not ERLI_PALETTE_WRITE_LOCK.acquire(blocking=False):
        return jsonify({"error": "Inna aktualizacja ERLI jest już w toku."}), 409

    target = {**dict(product_row), **dict(listing)}
    try:
        outcome, result, verified_after_error = update_erli_offer_palette(
            target, reference, expected_material
        )
        upsert_channel_description_check(DB_PATH, result)
        description_status = str(result.get("status") or "unverified")
        palette_status = str(result.get("palette_status") or "unverified")
        is_match = description_status == "match" and palette_status == "match"
        message = f"Blok materiału i kolorów {expected_material} w ERLI jest zgodny."
        record_audit_event(
            "erli_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value=f"erli: kolory {expected_material} zgodne ({outcome})",
            details={
                "channel": "erli",
                "external_id": external_id,
                "outcome": outcome,
                "reference_hash": reference["description_hash"],
                "material": expected_material,
                "verified_after_error": verified_after_error,
            },
        )
        response = jsonify(
            {
                "status": description_status,
                "status_label": DESCRIPTION_STATUS_LABELS.get(
                    description_status, "Opis niesprawdzony"
                ),
                "description_updated": False,
                "palette_updated": True,
                "is_match": is_match,
                "needs_repair": not is_match,
                "diff": None,
                "palette_status": palette_status,
                "palette_status_label": _palette_status_label(
                    palette_status, expected_material
                ),
                "palette_material": expected_material,
                "palette_diff": None,
                "outcome": outcome,
                "verified_after_error": verified_after_error,
                "message": message,
            }
        )
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except ErliPaletteUnverifiedError as exc:
        record_audit_event(
            "erli_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="erli: kolory niepotwierdzone",
            details={
                "channel": "erli",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except ErliPaletteUpdateError as exc:
        record_audit_event(
            "erli_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="erli: błąd kolorów",
            details={
                "channel": "erli",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return jsonify({"error": str(exc)}), 502
    except Exception as exc:
        app.logger.error("ERLI palette update failed: %s", type(exc).__name__)
        record_audit_event(
            "erli_palette_update",
            "product",
            entity_id=apilo_product_id,
            entity_label=product_row["name"] or str(apilo_product_id),
            new_value="erli: błąd kolorów",
            details={
                "channel": "erli",
                "external_id": external_id,
                "error": type(exc).__name__,
            },
        )
        return (
            jsonify({"error": "Nie udało się zaktualizować kolorów ERLI."}),
            502,
        )
    finally:
        ERLI_PALETTE_WRITE_LOCK.release()


@app.post("/sales-channels/recheck-description")
@login_required
def sales_channel_recheck_description():
    apilo_product_id = parse_int_value(request.form.get("apilo_product_id"), 0, min_value=1)
    channel_key = str(request.form.get("channel_key") or "").strip()
    external_id = str(request.form.get("external_id") or "").strip()
    if channel_key not in PUBLIC_DESCRIPTION_RECHECK_CHANNELS or not external_id:
        abort(400)
    reference = get_apilo_description_reference(DB_PATH, apilo_product_id)
    product_row = get_product_by_apilo_id(DB_PATH, apilo_product_id)
    listings = get_channel_listings_for_product(DB_PATH, apilo_product_id)
    listing = next(
        (
            item
            for item in listings
            if item["channel_key"] == channel_key
            and str(item.get("external_id") or "") == external_id
        ),
        None,
    )
    if not reference or not product_row or not listing:
        return abort(404)
    target = {**dict(product_row), **dict(listing)}
    result = recheck_public_channel_description(target, reference)
    upsert_channel_description_check(DB_PATH, result)
    current_checks = [
        item
        for item in get_channel_description_checks(
            DB_PATH, apilo_product_id=apilo_product_id
        )
        if item.get("reference_hash") == reference["description_hash"]
    ]
    palette_context = _palette_context(current_checks)
    palette_status = _resolved_palette_status(
        channel_key, result, palette_context
    )
    palette_material = str(
        result.get("palette_material") or palette_context["material"] or ""
    )
    palette_diff = None
    if palette_status == "mismatch":
        candidate_palette_diff = build_description_diff(
            canonical_material_palette_text(palette_material),
            result.get("palette_block_text") or "",
        )
        if candidate_palette_diff["available"]:
            palette_diff = candidate_palette_diff
    description_diff = None
    if result["status"] == "mismatch":
        candidate_diff = build_description_diff(
            reference["description_preview"] or reference["description_text"],
            result.get("actual_description_text") or "",
        )
        if candidate_diff["available"]:
            description_diff = candidate_diff
    record_audit_event(
        "channel_description_recheck",
        "product",
        entity_id=apilo_product_id,
        entity_label=product_row["name"] or product_row["sku"] or str(apilo_product_id),
        new_value=f"{channel_key}: {result['status']}",
        details={"channel": channel_key, "external_id": external_id},
    )
    return jsonify(
        {
            "status": result["status"],
            "status_label": DESCRIPTION_STATUS_LABELS.get(
                result["status"], "Opis niesprawdzony"
            ),
            "is_match": (
                result["status"] == "match"
                and palette_status in {"match", "absent", "not_applicable"}
            ),
            "needs_repair": (
                result["status"] in {"mismatch", "error"}
                or palette_status in {"mismatch", "missing", "unverified"}
            ),
            "diff": description_diff,
            "palette_status": palette_status,
            "palette_status_label": _palette_status_label(
                palette_status, palette_material
            ),
            "palette_diff": palette_diff,
        }
    )


@app.post("/sales-channels/empik-sync")
@login_required
def empik_sync():
    next_url = request.form.get("next") or url_for("sales_channels", channel="empik")
    if not is_safe_redirect_target(next_url):
        next_url = url_for("sales_channels", channel="empik")
    try:
        result = run_empik_sync_with_lock(blocking=False)
        if result is None:
            flash("Synchronizacja Empik API jest już w toku.", "info")
            return redirect(next_url)
        record_audit_event(
            "empik_api_sync",
            "sync",
            entity_label="Empik API",
            new_value=f"{result['offers']} ofert, {result['imports']} importów",
        )
        flash(f"Empik API: sprawdzono {result['offers']} ofert.", "success")
    except Exception as exc:
        app.logger.exception("Empik API sync failed")
        message = public_error_message(exc)
        set_setting(DB_PATH, "empik_last_sync_error", message)
        set_setting(DB_PATH, "empik_last_sync_error_at", utc_now_iso())
        record_audit_event(
            "empik_api_sync",
            "sync",
            entity_label="Empik API",
            new_value="błąd",
            details={"message": message},
        )
        flash(message, "error")
    return redirect(next_url)


@app.get("/sales-channels/empik-imports/<int:import_id>/error-report")
@login_required
def empik_error_report(import_id):
    import_row = get_empik_offer_import(DB_PATH, import_id)
    if not import_row or not import_row.get("has_error_report"):
        abort(404)
    try:
        body, content_type = get_empik_client().get_offer_import_error_report(import_id)
    except Exception as exc:
        app.logger.exception("Empik error report download failed")
        flash(public_error_message(exc), "error")
        return redirect(url_for("sales_channels", channel="empik"))
    extensions = {
        "text/csv": "csv",
        "application/csv": "csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/xml": "xml",
        "text/xml": "xml",
    }
    extension = extensions.get(content_type, "bin")
    record_audit_event(
        "empik_error_report_download",
        "sync",
        entity_id=str(import_id),
        entity_label="Raport błędów Empik OF03",
        new_value="pobrano",
    )
    response = send_file(
        io.BytesIO(body),
        mimetype=content_type,
        as_attachment=True,
        download_name=f"empik-of03-{import_id}.{extension}",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "channel_visibility":
            current_show_etsy = get_setting(DB_PATH, "sales_channels_show_etsy") == "1"
            show_etsy = request.form.get("sales_channels_show_etsy") == "1"
            set_setting(DB_PATH, "sales_channels_show_etsy", "1" if show_etsy else "0")
            record_audit_event(
                "channel_visibility_settings_update",
                "settings",
                entity_label="Widoczność kanałów",
                old_value=f"etsy={'widoczny' if current_show_etsy else 'ukryty'}",
                new_value=f"etsy={'widoczny' if show_etsy else 'ukryty'}",
            )
            flash("Widok kanałów zapisany.", "success")
        elif action == "email":
            current_email_settings = get_email_settings_snapshot(DB_PATH)
            smtp_password = request.form.get("smtp_password")
            clear_smtp_password = request.form.get("smtp_password_clear") == "1"
            try:
                validated_email_settings = normalize_smtp_settings(
                    {
                        "smtp_host": request.form.get("smtp_host"),
                        "smtp_port": request.form.get("smtp_port"),
                        "smtp_user": request.form.get("smtp_user"),
                        "smtp_use_tls": request.form.get("smtp_use_tls"),
                        "smtp_use_ssl": request.form.get("smtp_use_ssl"),
                        "smtp_from": request.form.get("smtp_from"),
                        "smtp_to": request.form.get("smtp_to"),
                    }
                )
            except SmtpValidationError as exc:
                flash(str(exc), "error")
                return redirect(url_for("settings"))
            new_email_settings = {
                **validated_email_settings,
                "has_password": (
                    False
                    if clear_smtp_password
                    else bool(smtp_password) or current_email_settings["has_password"]
                ),
            }
            for key, value in validated_email_settings.items():
                set_setting(DB_PATH, key, value)
            if clear_smtp_password:
                set_setting(DB_PATH, "smtp_password", "")
            elif smtp_password:
                set_setting(DB_PATH, "smtp_password", smtp_password)
            record_audit_event(
                "email_settings_update",
                "email",
                entity_label="Ustawienia SMTP",
                old_value=summarize_email_settings_snapshot(current_email_settings),
                new_value=summarize_email_settings_snapshot(new_email_settings),
            )
            flash("Ustawienia email zapisane.", "success")
        elif action == "api":
            current_api_settings = get_api_settings_snapshot(DB_PATH)
            api_client_secret = request.form.get("apilo_client_secret")
            clear_api_client_secret = request.form.get("apilo_client_secret_clear") == "1"
            new_api_settings = {
                "apilo_base_url": request.form.get("apilo_base_url") or "",
                "apilo_client_id": request.form.get("apilo_client_id") or "",
                "has_client_secret": (
                    False
                    if clear_api_client_secret
                    else bool(api_client_secret) or current_api_settings["has_client_secret"]
                ),
            }
            set_setting(DB_PATH, "apilo_base_url", request.form.get("apilo_base_url") or "")
            set_setting(DB_PATH, "apilo_client_id", request.form.get("apilo_client_id") or "")
            if clear_api_client_secret:
                set_setting(DB_PATH, "apilo_client_secret", "")
            elif api_client_secret:
                set_setting(DB_PATH, "apilo_client_secret", api_client_secret)
            auth_code = request.form.get("apilo_auth_code") or ""
            token_fetch_status = "skipped"
            if auth_code:
                try:
                    client = get_client()
                    client._fetch_tokens("authorization_code", auth_code)
                    token_fetch_status = "ok"
                    flash("Dane API zapisane i tokeny pobrane.", "success")
                except Exception as exc:
                    app.logger.exception("API token fetch failed")
                    token_fetch_status = "error"
                    flash(public_error_message(exc), "error")
            else:
                flash("Ustawienia API Apilo zapisane.", "success")
            record_audit_event(
                "api_settings_update",
                "api",
                entity_label="Ustawienia API Apilo",
                old_value=summarize_api_settings_snapshot(
                    current_api_settings["apilo_base_url"],
                    current_api_settings["apilo_client_id"],
                    current_api_settings["has_client_secret"],
                ),
                new_value=summarize_api_settings_snapshot(
                    new_api_settings["apilo_base_url"],
                    new_api_settings["apilo_client_id"],
                    new_api_settings["has_client_secret"],
                ),
                details={
                    "auth_code_used": bool(auth_code),
                    "token_fetch_status": token_fetch_status,
                },
            )
        elif action == "api_test":
            previous_test_status = get_setting(DB_PATH, "api_test_status") or ""
            try:
                client = get_client()
                client.timeout = 10
                client.test_connection()
                set_setting(DB_PATH, "api_test_status", "ok")
                set_setting(DB_PATH, "api_test_message", "Połączenie działa.")
                set_setting(DB_PATH, "api_test_at", utc_now_iso())
                record_audit_event(
                    "api_connection_test",
                    "api",
                    entity_label="Test połączenia API",
                    old_value=previous_test_status or None,
                    new_value="ok",
                    details={"message": "Połączenie działa."},
                )
                flash("Połączenie działa.", "success")
            except requests.exceptions.Timeout:
                set_setting(DB_PATH, "api_test_status", "error")
                set_setting(DB_PATH, "api_test_message", "Timeout połączenia z API.")
                set_setting(DB_PATH, "api_test_at", utc_now_iso())
                record_audit_event(
                    "api_connection_test",
                    "api",
                    entity_label="Test połączenia API",
                    old_value=previous_test_status or None,
                    new_value="error",
                    details={"message": "Timeout połączenia z API."},
                )
                flash("Timeout połączenia z API.", "error")
            except Exception as exc:
                app.logger.exception("API connection test failed")
                message = public_error_message(exc)
                set_setting(DB_PATH, "api_test_status", "error")
                set_setting(DB_PATH, "api_test_message", message)
                set_setting(DB_PATH, "api_test_at", utc_now_iso())
                record_audit_event(
                    "api_connection_test",
                    "api",
                    entity_label="Test połączenia API",
                    old_value=previous_test_status or None,
                    new_value="error",
                    details={"message": message},
                )
                flash(message, "error")
        elif action == "empik":
            current_empik = get_empik_settings_snapshot(DB_PATH)
            api_key = (request.form.get("empik_api_key") or "").strip()
            clear_api_key = request.form.get("empik_api_key_clear") == "1"
            shop_id_raw = (request.form.get("empik_shop_id") or "").strip()
            if shop_id_raw:
                try:
                    shop_id = int(shop_id_raw)
                except ValueError:
                    flash("ID sklepu EmpikPlace musi być dodatnią liczbą.", "error")
                    return redirect(url_for("settings"))
                if shop_id < 1:
                    flash("ID sklepu EmpikPlace musi być dodatnią liczbą.", "error")
                    return redirect(url_for("settings"))
                shop_id_value = str(shop_id)
            else:
                shop_id_value = ""
            new_empik = {
                "shop_id": shop_id_value,
                "has_api_key": (
                    False
                    if clear_api_key
                    else bool(api_key) or current_empik["has_api_key"]
                ),
            }
            set_setting(DB_PATH, "empik_shop_id", shop_id_value)
            if clear_api_key:
                set_setting(DB_PATH, "empik_api_key", "")
            elif api_key:
                set_setting(DB_PATH, "empik_api_key", api_key)
            credentials_changed = (
                shop_id_value != current_empik["shop_id"] or bool(api_key) or clear_api_key
            )
            if credentials_changed:
                set_setting(DB_PATH, "empik_last_sync_at", "")
                set_setting(DB_PATH, "empik_test_status", "")
                set_setting(DB_PATH, "empik_test_message", "")
            record_audit_event(
                "empik_settings_update",
                "api",
                entity_label="Ustawienia API EmpikPlace",
                old_value=summarize_empik_settings_snapshot(
                    current_empik["shop_id"],
                    current_empik["has_api_key"],
                ),
                new_value=summarize_empik_settings_snapshot(
                    new_empik["shop_id"],
                    new_empik["has_api_key"],
                ),
            )
            flash("Ustawienia API EmpikPlace zapisane.", "success")
        elif action == "empik_test":
            previous_test_status = get_setting(DB_PATH, "empik_test_status") or ""
            try:
                get_empik_client().test_connection()
                status = "ok"
                message = "Połączenie z EmpikPlace działa."
                flash(message, "success")
            except Exception as exc:
                app.logger.exception("Empik API connection test failed")
                status = "error"
                message = public_error_message(exc)
                flash(message, "error")
            set_setting(DB_PATH, "empik_test_status", status)
            set_setting(DB_PATH, "empik_test_message", message)
            set_setting(DB_PATH, "empik_test_at", utc_now_iso())
            record_audit_event(
                "empik_connection_test",
                "api",
                entity_label="Test API EmpikPlace",
                old_value=previous_test_status or None,
                new_value=status,
                details={"message": message},
            )
        elif action == "allegro":
            current_price_list_id = get_setting(DB_PATH, "allegro_price_list_id") or ""
            allegro_price_list_id = parse_int_value(
                request.form.get("allegro_price_list_id"), 20, min_value=1
            )
            set_setting(DB_PATH, "allegro_price_list_id", str(allegro_price_list_id))
            record_audit_event(
                "allegro_settings_update",
                "settings",
                entity_label="Cennik Allegro",
                old_value=current_price_list_id or None,
                new_value=str(allegro_price_list_id),
            )
            flash("Ustawienia Allegro zapisane.", "success")
        elif action == "email_test":
            try:
                send_test_email()
                record_audit_event(
                    "email_test_send",
                    "email",
                    entity_label="Email testowy",
                    new_value=get_setting(DB_PATH, "smtp_to")
                    or get_setting(DB_PATH, "smtp_user")
                    or "-",
                )
                flash("Wysłano testowy email.", "success")
            except Exception as exc:
                app.logger.exception("Email test failed")
                flash(public_error_message(exc), "error")
        elif action == "alerts_settings":
            current_enabled = get_low_stock_alert_enabled()
            current_interval = get_low_stock_alert_interval_hours()
            enabled = request.form.get("alerts_low_stock_enabled") == "1"
            interval_hours = parse_int_value(
                request.form.get("alerts_low_stock_interval_hours"),
                24,
                min_value=1,
                max_value=720,
            )
            set_setting(DB_PATH, "alerts_low_stock_enabled", "1" if enabled else "0")
            set_setting(DB_PATH, "alerts_low_stock_interval_hours", str(interval_hours))
            if enabled and not current_enabled:
                set_setting(DB_PATH, "alerts_low_stock_last_check_at", "")
                set_setting(DB_PATH, "alerts_low_stock_last_result", "Auto alert włączony.")
            elif not enabled:
                set_setting(DB_PATH, "alerts_low_stock_last_result", "Auto alert wyłączony.")
                set_setting(DB_PATH, "alerts_low_stock_last_error", "")
                set_setting(DB_PATH, "alerts_low_stock_last_error_at", "")
            record_audit_event(
                "low_stock_alert_settings_update",
                "settings",
                entity_label="Auto alert niskich stanów",
                old_value=summarize_low_stock_alert_settings_snapshot(
                    current_enabled,
                    current_interval,
                ),
                new_value=summarize_low_stock_alert_settings_snapshot(
                    enabled,
                    interval_hours,
                ),
            )
            flash("Ustawienia auto alertu zapisane.", "success")
        elif action == "alerts_email":
            try:
                result = process_low_stock_alert(mode="manual")
                if result["status"] == "empty":
                    flash("Brak pozycji do alertu niskich stanów.", "info")
                else:
                    flash(
                        f"Wysłano alert niskich stanów ({format_position_count(result['count'])}).",
                        "success",
                    )
            except Exception as exc:
                app.logger.exception("Low stock alert email failed")
                message = public_error_message(exc)
                mark_low_stock_alert_error(message, mode="manual")
                flash(message, "error")
        elif action == "password":
            password = request.form.get("password")
            confirm = request.form.get("confirm")
            if not password or len(password) < 8:
                flash("Hasło musi mieć minimum 8 znaków.", "error")
            elif password != confirm:
                flash("Hasła nie są zgodne.", "error")
            else:
                set_setting(DB_PATH, "password_hash", generate_password_hash(password))
                record_audit_event(
                    "password_change",
                    "security",
                    entity_label="Hasło panelu",
                    old_value="ustawione",
                    new_value="zmienione",
                )
                flash("Hasło zostało zmienione.", "success")
        elif action == "suggestions":
            current_lead_time = get_suggest_lead_time_days()
            current_safety_pct = get_suggest_safety_pct()
            current_suggest_days = get_suggest_days()
            lead_time = parse_int_value(request.form.get("lead_time_days"), 1, min_value=1)
            safety_pct = parse_float_value(request.form.get("safety_pct"), 20.0, min_value=0.0)
            suggest_days = parse_int_value(request.form.get("suggest_days"), 30, min_value=1)
            if suggest_days not in (30, 60, 120, 180, 365):
                suggest_days = 30
            set_setting(DB_PATH, "suggest_lead_time_days", str(lead_time))
            set_setting(DB_PATH, "suggest_safety_pct", str(safety_pct))
            set_setting(DB_PATH, "suggest_days", str(suggest_days))
            record_audit_event(
                "suggestions_settings_update",
                "settings",
                entity_label="Sugestie stanów",
                old_value=summarize_suggestions_settings_snapshot(
                    current_lead_time,
                    current_safety_pct,
                    current_suggest_days,
                ),
                new_value=summarize_suggestions_settings_snapshot(
                    lead_time,
                    safety_pct,
                    suggest_days,
                ),
            )
            flash("Ustawienia sugestii zapisane.", "success")
        elif action == "suggestions_refresh":
            try:
                refreshed = run_suggestions_refresh_with_lock(blocking=False, force_year=True)
                if refreshed:
                    record_audit_event(
                        "suggestions_refresh",
                        "settings",
                        entity_label="Sugestie stanów",
                        new_value="odświeżone",
                        details={"force_year": True},
                    )
                    flash("Sugestie stanów odświeżone.", "success")
                else:
                    flash("Synchronizacja jest już w toku. Spróbuj ponownie za chwilę.", "info")
            except Exception as exc:
                app.logger.exception("Suggestions refresh failed")
                flash(public_error_message(exc), "error")
        elif action == "inventory_value":
            previous_store_total = get_setting(DB_PATH, "inventory_value_store") or ""
            previous_allegro_total = get_setting(DB_PATH, "inventory_value_allegro") or ""
            try:
                store_total, allegro_total = get_inventory_value_totals(DB_PATH)
                set_setting(DB_PATH, "inventory_value_store", f"{store_total:.2f}")
                set_setting(DB_PATH, "inventory_value_allegro", f"{allegro_total:.2f}")
                set_setting(DB_PATH, "inventory_value_at", utc_now_iso())
                record_audit_event(
                    "inventory_value_refresh",
                    "settings",
                    entity_label="Wartość magazynu",
                    old_value=summarize_inventory_values_snapshot(
                        previous_store_total,
                        previous_allegro_total,
                    ),
                    new_value=summarize_inventory_values_snapshot(
                        f"{store_total:.2f}",
                        f"{allegro_total:.2f}",
                    ),
                )
                flash("Przeliczono wartość magazynu.", "success")
            except Exception as exc:
                app.logger.exception("Inventory value calculation failed")
                flash(public_error_message(exc), "error")
        return redirect(url_for("settings"))

    email_snapshot = get_email_settings_snapshot(DB_PATH)
    email_settings = {
        "smtp_host": email_snapshot["smtp_host"],
        "smtp_port": email_snapshot["smtp_port"],
        "smtp_user": email_snapshot["smtp_user"],
        "has_smtp_password": email_snapshot["has_password"],
        "smtp_use_tls": email_snapshot["smtp_use_tls"],
        "smtp_use_ssl": email_snapshot["smtp_use_ssl"],
        "smtp_from": email_snapshot["smtp_from"],
        "smtp_to": email_snapshot["smtp_to"],
    }
    api_settings = {
        "apilo_base_url": get_setting(DB_PATH, "apilo_base_url") or "",
        "apilo_client_id": get_setting(DB_PATH, "apilo_client_id") or "",
        "has_client_secret": bool(get_config_value("APILO_CLIENT_SECRET", "apilo_client_secret")),
    }
    empik_settings = {
        "shop_id": get_setting(DB_PATH, "empik_shop_id") or "",
        "has_api_key": bool(get_config_value("EMPIK_API_KEY", "empik_api_key")),
        "test_status": get_setting(DB_PATH, "empik_test_status") or "",
        "test_message": get_setting(DB_PATH, "empik_test_message") or "",
        "test_at": format_pull_time(get_setting(DB_PATH, "empik_test_at") or ""),
    }
    empik_imports = (
        get_empik_offer_imports(DB_PATH, limit=20)
        if empik_settings["has_api_key"]
        else []
    )
    empik_connection = {
        "last_sync_at": format_pull_time(
            get_setting(DB_PATH, "empik_last_sync_at") or ""
        ),
        "last_error": get_setting(DB_PATH, "empik_last_sync_error") or "",
        "error_lines": sum(
            int(item.get("lines_in_error") or 0) for item in empik_imports
        ),
    }
    allegro_settings = {
        "allegro_price_list_id": str(get_allegro_price_list_id()),
    }
    api_status = {
        "config_ok": bool(
            get_config_value("APILO_BASE_URL", "apilo_base_url", "")
            and get_config_value("APILO_CLIENT_ID", "apilo_client_id", "")
            and get_config_value("APILO_CLIENT_SECRET", "apilo_client_secret", "")
        ),
        "tokens_ok": not tokens_missing(),
        "test_status": get_setting(DB_PATH, "api_test_status") or "",
        "test_message": get_setting(DB_PATH, "api_test_message") or "",
        "test_at": format_pull_time(get_setting(DB_PATH, "api_test_at") or ""),
    }
    api_locked = api_status["config_ok"] and api_status["tokens_ok"]
    api_edit_mode = request.args.get("edit_api") == "1"
    show_api_form = (not api_locked) or api_edit_mode
    inventory_values = {
        "store": get_setting(DB_PATH, "inventory_value_store") or "",
        "allegro": get_setting(DB_PATH, "inventory_value_allegro") or "",
        "updated_at": format_pull_time(get_setting(DB_PATH, "inventory_value_at") or ""),
    }
    low_stock_dashboard = get_dashboard_metrics(
        DB_PATH,
        lead_time_days=get_suggest_lead_time_days(),
        safety_pct=get_suggest_safety_pct(),
        suggest_days=get_suggest_days(),
    )
    last_sent_count = parse_int_value(get_setting(DB_PATH, "alerts_low_stock_sent_count"), 0, min_value=0)
    low_stock_alerts = {
        "rows": get_low_stock_rows(limit=10),
        "count": low_stock_dashboard.get("shortage_count", 0) or 0,
        "units": low_stock_dashboard.get("shortage_units", 0) or 0,
        "enabled": get_low_stock_alert_enabled(),
        "interval_hours": get_low_stock_alert_interval_hours(),
        "last_sent_at": format_pull_time(get_setting(DB_PATH, "alerts_low_stock_sent_at") or ""),
        "last_sent_count": last_sent_count,
        "last_sent_count_label": format_position_count(last_sent_count),
        "last_check_at": format_pull_time(
            get_setting(DB_PATH, "alerts_low_stock_last_check_at") or ""
        ),
        "next_check_at": format_pull_time(get_low_stock_alert_next_check_iso()),
        "last_result": get_setting(DB_PATH, "alerts_low_stock_last_result") or "",
        "last_error": get_setting(DB_PATH, "alerts_low_stock_last_error") or "",
        "last_error_at": format_pull_time(
            get_setting(DB_PATH, "alerts_low_stock_last_error_at") or ""
        ),
        "history": build_low_stock_alert_history(limit=10),
    }
    return render_template(
        "settings.html",
        required=tokens_missing(),
        email=email_settings,
        api=api_settings,
        empik=empik_settings,
        empik_connection=empik_connection,
        empik_imports=empik_imports,
        allegro=allegro_settings,
        api_status=api_status,
        api_locked=api_locked,
        api_edit_mode=api_edit_mode,
        show_api_form=show_api_form,
        secret_storage=build_secret_storage_payload(SECRET_STORAGE_STATUS),
        inventory_values=inventory_values,
        low_stock_alerts=low_stock_alerts,
        audit_entries=build_recent_audit_entries(DB_PATH, limit=40),
        channel_visibility={
            "show_etsy": get_setting(DB_PATH, "sales_channels_show_etsy") == "1",
        },
        suggest_lead_time_days=get_suggest_lead_time_days(),
        suggest_safety_pct=get_suggest_safety_pct(),
        suggest_days=get_suggest_days(),
    )


@app.post("/products/<int:product_id>/attributes")
@login_required
def update_product_attributes_route(product_id):
    next_url = request.form.get("next") or request.referrer
    if not is_safe_redirect_target(next_url):
        next_url = url_for("index")
    try:
        target = get_product_by_id(DB_PATH, product_id)
        if not target:
            flash("Produkt nie znaleziony.", "error")
            return redirect(next_url)

        material = normalize_manual_material(request.form.get("material"))
        color = " ".join((request.form.get("color") or "").split())
        if len(color) > 50:
            raise ValueError("Nazwa koloru może mieć maksymalnie 50 znaków.")
        if material in {"FLEX", "CARBON"}:
            color = "czarny"

        old_value = f"materiał={target['material'] or 'brak'}, kolor={target['color'] or 'brak'}"
        update_product_attributes_manual(
            DB_PATH,
            product_id,
            material=material,
            color=color,
        )
        new_value = f"materiał={material or 'brak'}, kolor={color or 'brak'}"
        record_audit_event(
            "product_attributes_update",
            "product",
            entity_id=product_id,
            entity_label=target["name"] or target["sku"] or f"Produkt {product_id}",
            old_value=old_value,
            new_value=new_value,
            details={"source": "manual_user_hint"},
        )
        flash("Materiał i kolor zapisane ręcznie.", "success")
    except Exception as exc:
        app.logger.exception("Product attributes update failed")
        flash(public_error_message(exc), "error")
    return redirect(next_url)


@app.post("/products/<int:product_id>/quantity")
@login_required
def update_quantity(product_id):
    quantity_raw = request.form.get("quantity")
    expected_raw = request.form.get("expected_quantity")
    next_url = request.form.get("next") or request.referrer
    if not is_safe_redirect_target(next_url):
        next_url = url_for("index")
    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        flash("Nieprawidłowa wartość stanu magazynowego.", "error")
        return redirect(next_url)
    if quantity < 0:
        flash("Stan magazynowy nie może być ujemny.", "error")
        return redirect(next_url)

    try:
        target = get_product_by_id(DB_PATH, product_id)
        if not target:
            flash("Produkt nie znaleziony.", "error")
            return redirect(next_url)
        if target["apilo_id"] is None:
            flash("Brak identyfikatora Apilo potrzebnego do bezpiecznej weryfikacji.", "error")
            return redirect(next_url)
        expected_quantity = int(
            expected_raw if expected_raw is not None else (target["quantity"] or 0)
        )
        client = get_client()
        remote_before = client.get_product(int(target["apilo_id"]))
        if not isinstance(remote_before, dict) or "quantity" not in remote_before:
            raise RuntimeError("Apilo nie zwróciło stanu produktu do weryfikacji.")
        remote_before_quantity = int(remote_before["quantity"])
        if remote_before_quantity != expected_quantity:
            update_product_quantity(DB_PATH, product_id, remote_before_quantity)
            record_audit_event(
                "product_quantity_conflict",
                "product",
                entity_id=product_id,
                entity_label=target["name"] or target["sku"] or f"Produkt {product_id}",
                old_value=str(expected_quantity),
                new_value=str(remote_before_quantity),
                details={"result": "remote_changed_before_write"},
            )
            flash(
                "Stan w Apilo zmienił się od otwarcia strony. Odświeżyłam wartość bez jej nadpisywania.",
                "info",
            )
            return redirect(next_url)

        patch_error = None
        try:
            client.update_quantities([{"id": int(target["apilo_id"]), "quantity": quantity}])
        except ApiloClientError as exc:
            patch_error = exc

        try:
            remote_after = client.get_product(int(target["apilo_id"]))
        except Exception as exc:
            mark_product_quantity_unverified(DB_PATH, product_id)
            record_audit_event(
                "product_quantity_unverified",
                "product",
                entity_id=product_id,
                entity_label=target["name"] or target["sku"] or f"Produkt {product_id}",
                old_value=str(expected_quantity),
                new_value="nieznany",
                details={"result": "verification_failed_after_write"},
            )
            raise RuntimeError(
                "Nie udało się potwierdzić wyniku w Apilo. Produkt został oznaczony do ponownej synchronizacji."
            ) from exc
        if not isinstance(remote_after, dict) or "quantity" not in remote_after:
            mark_product_quantity_unverified(DB_PATH, product_id)
            raise RuntimeError(
                "Nie udało się potwierdzić wyniku w Apilo. Produkt został oznaczony do ponownej synchronizacji."
            )
        verified_quantity = int(remote_after["quantity"])
        if verified_quantity != quantity:
            update_product_quantity(DB_PATH, product_id, verified_quantity)
            if patch_error:
                raise patch_error
            raise RuntimeError("Apilo nie potwierdziło zadanego stanu magazynowego.")

        previous_quantity = target["quantity"]
        update_product_quantity(DB_PATH, product_id, verified_quantity)
        record_audit_event(
            "product_quantity_update",
            "product",
            entity_id=product_id,
            entity_label=target["name"] or target["sku"] or f"Produkt {product_id}",
            old_value=str(previous_quantity) if previous_quantity is not None else "brak",
            new_value=str(verified_quantity),
            details={
                "sku": target["sku"] or "",
                "ean": target["ean"] or "",
                "verified_after_error": bool(patch_error),
            },
        )
        if patch_error:
            flash(
                "Stan został potwierdzony po błędzie połączenia i zapisany lokalnie.",
                "success",
            )
        else:
            flash("Stan zaktualizowany i potwierdzony w Apilo.", "success")
    except Exception as exc:
        app.logger.exception("Quantity update failed")
        flash(public_error_message(exc), "error")
    return redirect(next_url)


@app.post("/sync/pull")
@login_required
def sync_pull():
    try:
        count = run_sync_pull_with_lock(blocking=False)
        if count is None:
            flash("Synchronizacja jest już w toku. Spróbuj ponownie za chwilę.", "info")
        else:
            record_audit_event(
                "manual_sync_pull",
                "sync",
                entity_label="Pobranie produktów",
                new_value=f"{count} produktów",
            )
            flash(f"Pobrano {count} produktów z Apilo.", "success")
    except Exception as exc:
        app.logger.exception("Manual sync pull failed")
        flash(public_error_message(exc), "error")
    next_url = request.form.get("next") or url_for("index")
    if not is_safe_redirect_target(next_url):
        next_url = url_for("index")
    return redirect(next_url)


@app.post("/sync/push")
@login_required
def sync_push():
    flash("Zmiany są wysyłane od razu do Apilo.", "info")
    return redirect(url_for("index"))


@app.get("/sync/status")
@login_required
def sync_status():
    return jsonify(build_sync_status_payload())


@app.route("/sales-report")
@login_required
def sales_report():
    if tokens_missing():
        return redirect(url_for("settings"))
    days = normalize_sales_report_days(request.args.get("days"), default=30)
    export = request.args.get("export") == "1"
    realized_only = request.args.get("realized", "1") != "0"
    now = datetime.now(timezone.utc)
    updated_after = (now - timedelta(days=days)).isoformat()
    try:
        totals, meta, _daily_map = get_sales_totals(days, realized_only=realized_only)
    except Exception as exc:
        app.logger.exception("Sales report generation failed")
        flash(public_error_message(exc), "error")
        return redirect(url_for("index"))
    rows = build_sales_report_rows(DB_PATH, totals)
    if export:
        response = app.response_class(
            build_sales_report_csv(rows),
            mimetype="text/csv",
        )
        response.headers["Content-Disposition"] = "attachment; filename=raport_sprzedazy.csv"
        return response
    return render_template(
        "sales_report.html",
        rows=rows,
        days=days,
        realized_only=realized_only,
        orders_total=meta["orders_total"],
        orders_used=meta["orders_used"],
        realized_filter=meta["realized_filter"],
        updated_after=format_pull_time(updated_after),
    )


def get_sales_totals(days, realized_only=True):
    return build_sales_totals(
        DB_PATH,
        get_client(),
        days,
        realized_only=realized_only,
    )


def perform_sync_pull():
    if tokens_missing():
        raise RuntimeError("Brak tokenów Apilo.")
    client = get_client()
    products = client.list_products()
    product_ids = [int(product["id"]) for product in products if product.get("id") is not None]

    image_map = {}
    batch_size = 50
    for idx in range(0, len(product_ids), batch_size):
        media = client.get_product_media(product_ids[idx : idx + batch_size], only_main=True)
        for item in media:
            product_id = item.get("productId")
            if product_id is not None and item.get("link"):
                image_map[int(product_id)] = item["link"]

    auction_map = {}
    attributes_map = {}
    sales_channels = None
    channel_listings = None
    replace_auction_data = False
    try:
        platforms = client.list_sale_platforms()
        auctions = client.list_auctions()
        auction_map, attributes_map = build_auction_metadata(products, platforms, auctions)
        sales_channels, channel_listings = build_channel_listing_rows(
            products, platforms, auctions
        )
        replace_auction_data = True
    except Exception:
        app.logger.exception(
            "Pobieranie ofert kanałów sprzedaży nie powiodło się; zachowuję poprzednie metadane."
        )

    allegro_price_id = get_allegro_price_list_id()
    prices = client.list_price_calculated(allegro_price_id)
    price_map = build_allegro_price_map(products, prices, compute_allegro_price)

    sync_result = apply_product_snapshot(
        DB_PATH,
        products,
        image_map=image_map,
        auction_map=auction_map,
        attributes_map=attributes_map,
        price_map=price_map,
        replace_auction_data=replace_auction_data,
        sales_channels=sales_channels,
        channel_listings=channel_listings,
    )
    changed_image_ids = set(sync_result["changed_image_ids"])
    for product_id, image_url in image_map.items():
        prefetch_thumbnail(product_id, image_url, force=product_id in changed_image_ids)
    if sync_result["deactivated_count"]:
        app.logger.info(
            "Ukryto %s produktów nieobecnych w pełnym snapshotcie Apilo.",
            sync_result["deactivated_count"],
        )
    return sync_result["active_count"]


def refresh_suggestions_cache(force_year=False):
    suggest_days = get_suggest_days()
    totals, _, details_map = get_sales_totals(suggest_days)
    save_sales_cache(DB_PATH, totals, details_map)
    set_setting(DB_PATH, "sales_cache_at", utc_now_iso())
    if suggest_days == 365:
        year_totals, year_details = totals, details_map
        force_year = True
    elif force_year or should_refresh_year_sales_cache(force=force_year):
        year_totals, _, year_details = get_sales_totals(365)
    else:
        return
    year_order_counts = {ean: len(items) for ean, items in year_details.items()}
    save_sales_year_cache(DB_PATH, year_totals, year_order_counts, year_details)
    set_setting(DB_PATH, "sales_year_cache_at", utc_now_iso())


def background_refresh_loop():
    ensure_sync_schedule()
    while True:
        ran_job = False
        now = datetime.now(timezone.utc)
        try:
            if not tokens_missing() and is_schedule_due(
                get_sync_status_snapshot().get("next_inventory_sync_at"), now
            ):
                count = run_sync_pull_with_lock(blocking=False)
                ran_job = True
                if count is None:
                    schedule_inventory_sync(reference_time=now, retry=True)
                    app.logger.info("Background inventory sync skipped, sync already in progress.")
                else:
                    app.logger.info("Background inventory sync completed, pulled %s products.", count)
        except Exception:
            app.logger.exception("Background inventory sync failed")
        now = datetime.now(timezone.utc)
        try:
            if not tokens_missing() and is_schedule_due(
                get_sync_status_snapshot().get("next_sales_refresh_at"), now
            ):
                force_year_refresh = should_refresh_year_sales_cache()
                refreshed = run_suggestions_refresh_with_lock(
                    blocking=False,
                    force_year=force_year_refresh,
                )
                ran_job = True
                if refreshed:
                    app.logger.info("Background sales cache refresh completed.")
                else:
                    schedule_sales_refresh(reference_time=now, retry=True)
                    app.logger.info("Background sales cache refresh skipped, sync already in progress.")
        except Exception:
            app.logger.exception("Background sales cache refresh failed")
        now = datetime.now(timezone.utc)
        try:
            if is_low_stock_alert_due(now):
                alert_result = run_low_stock_alert_with_lock(blocking=False)
                if alert_result is None:
                    pass
                elif alert_result["status"] == "sent":
                    ran_job = True
                    app.logger.info(
                        "Background low-stock alert sent for %s products.",
                        alert_result["count"],
                    )
                elif alert_result["status"] == "duplicate":
                    ran_job = True
                    app.logger.info("Background low-stock alert skipped, no changes detected.")
                else:
                    ran_job = True
                    app.logger.info("Background low-stock alert skipped, no shortages found.")
        except Exception as exc:
            message = public_error_message(exc)
            mark_low_stock_alert_error(message, mode="auto")
            app.logger.exception("Background low-stock alert failed")
        time.sleep(5 if not ran_job else 1)


def compute_allegro_price(item, base_value, markup_pct=0.0):
    custom_price = item.get("customPriceWithTax")
    if custom_price is not None:
        return f"{float(custom_price):.2f}"
    mode = item.get("customMode")
    modify = item.get("customPriceModify")
    if modify is None and mode is None:
        if base_value is None:
            return None
        try:
            base_val = float(base_value)
        except (TypeError, ValueError):
            return None
        if markup_pct:
            base_val *= 1 + markup_pct / 100.0
        return f"{base_val:.2f}"
    if modify is None:
        return None
    try:
        modify_val = float(modify)
    except (TypeError, ValueError):
        return None
    try:
        base_val = float(base_value) if base_value is not None else None
    except (TypeError, ValueError):
        base_val = None
    if mode == 3:
        return f"{modify_val:.2f}"
    if base_val is None:
        return None
    if mode == 5:
        return f"{base_val * (1 + modify_val / 100.0):.2f}"
    if mode == 7:
        return f"{base_val + modify_val:.2f}"
    if mode == 6:
        if modify_val >= 100:
            return None
        return f"{base_val / (1 - modify_val / 100.0):.2f}"
    return f"{base_val:.2f}"


def start_background_refresh(debug_mode):
    return start_runtime_background_refresh(debug_mode, background_refresh_loop)


def build_thumb_version(image_url):
    if not image_url:
        return ""
    return hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]


def get_thumb_filename(apilo_id, image_url):
    parsed = urlparse(image_url or "")
    if parsed.scheme not in ("http", "https"):
        return None
    ext = os.path.splitext(parsed.path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    return f"{apilo_id}{ext}"


def get_thumb_local_path(apilo_id, image_url):
    filename = get_thumb_filename(apilo_id, image_url)
    if not filename:
        return None, None
    return filename, os.path.join(THUMB_DIR, filename)


def is_thumb_fresh(local_path):
    if not local_path or not os.path.exists(local_path) or THUMB_TTL_SECONDS == 0:
        return False
    age = time.time() - os.path.getmtime(local_path)
    return age < THUMB_TTL_SECONDS


def send_thumb_file(filename, max_age=None):
    response = send_from_directory(
        THUMB_DIR,
        filename,
        max_age=max_age if max_age and max_age > 0 else None,
    )
    response.vary.add("Cookie")
    if max_age and max_age > 0:
        response.headers["Cache-Control"] = f"private, max-age={max_age}"
    else:
        response.headers["Cache-Control"] = "private, no-cache"
        response.headers.pop("Expires", None)
    return response


def render_thumbnail_image(source_path, dest_path, filename):
    ext = os.path.splitext(filename)[1].lower()
    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail(
            (THUMB_RENDER_MAX_EDGE_PX, THUMB_RENDER_MAX_EDGE_PX),
            Image.Resampling.LANCZOS,
        )
        if ext in (".jpg", ".jpeg"):
            image = image.convert("RGB")
            image.save(
                dest_path,
                format="JPEG",
                quality=82,
                optimize=True,
                progressive=True,
            )
        else:
            image.save(dest_path, format="PNG", optimize=True)


def download_thumbnail(url, local_path):
    os.makedirs(THUMB_DIR, exist_ok=True)
    thumb_root = os.path.realpath(THUMB_DIR)
    destination = os.path.abspath(local_path)
    filename = os.path.basename(destination)
    if os.path.realpath(os.path.dirname(destination)) != thumb_root:
        raise ValueError("Thumbnail destination outside cache directory.")
    if not filename or os.path.islink(destination):
        raise ValueError("Unsafe thumbnail destination.")
    with tempfile.TemporaryDirectory(prefix=".thumb-download-", dir=thumb_root) as temp_dir:
        download_path = os.path.join(temp_dir, "source.download")
        rendered_path = os.path.join(temp_dir, "rendered.tmp")
        with requests.get(
            url,
            timeout=THUMB_DOWNLOAD_TIMEOUT_SECONDS,
            stream=True,
        ) as response:
            response.raise_for_status()
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                raise ValueError("Unsupported thumbnail content type.")
            total_size = 0
            with open(download_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total_size += len(chunk)
                    if total_size > THUMB_MAX_DOWNLOAD_BYTES:
                        raise ValueError("Thumbnail exceeds size limit.")
                    handle.write(chunk)
        render_thumbnail_image(download_path, rendered_path, filename)
        os.replace(rendered_path, destination)


def refresh_thumbnail_background(apilo_id, url, local_path):
    try:
        download_thumbnail(url, local_path)
    except Exception:
        app.logger.warning(
            "Background thumbnail refresh failed for apilo_id=%s",
            apilo_id,
            exc_info=True,
        )
    finally:
        with THUMB_REFRESH_LOCK:
            THUMB_REFRESH_IN_PROGRESS.discard(apilo_id)


def schedule_thumbnail_refresh(apilo_id, url, local_path):
    with THUMB_REFRESH_LOCK:
        if apilo_id in THUMB_REFRESH_IN_PROGRESS:
            return False
        THUMB_REFRESH_IN_PROGRESS.add(apilo_id)
    try:
        THUMB_REFRESH_EXECUTOR.submit(
            refresh_thumbnail_background,
            apilo_id,
            url,
            local_path,
        )
        return True
    except Exception:
        with THUMB_REFRESH_LOCK:
            THUMB_REFRESH_IN_PROGRESS.discard(apilo_id)
        app.logger.warning(
            "Thumbnail refresh scheduling failed for apilo_id=%s",
            apilo_id,
            exc_info=True,
        )
        return False


def prefetch_thumbnail(apilo_id, image_url, force=False):
    filename, local_path = get_thumb_local_path(apilo_id, image_url)
    if not filename:
        return False
    if not force and is_thumb_fresh(local_path):
        return False
    return schedule_thumbnail_refresh(apilo_id, image_url, local_path)


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=()",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'",
    )
    if request.endpoint in {"login", "setup_password"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.context_processor
def inject_now():
    now = datetime.now().astimezone()
    return {
        "now_human": now.strftime("%Y-%m-%d %H:%M:%S"),
        "csrf_token": get_csrf_token,
        "app_version": APP_VERSION,
        "project_name": PROJECT_NAME,
        "project_description": PROJECT_DESCRIPTION,
        "thumb_version": build_thumb_version,
    }


@app.get("/healthz")
def healthz():
    try:
        get_setting(DB_PATH, "last_pull_at")
        return jsonify(
            {
                "status": "ok",
                "version": APP_VERSION,
                "sync_running": get_sync_status_snapshot().get("running", False),
            }
        )
    except Exception:
        app.logger.exception("Healthcheck failed")
        return (
            jsonify(
                {
                    "status": "error",
                    "version": APP_VERSION,
                }
            ),
            503,
        )

app.template_filter("date_pl")(format_date_pl)
app.template_filter("pln")(format_pln)


def send_email_message(subject, body):
    import smtplib
    from email.message import EmailMessage

    try:
        smtp_settings = normalize_smtp_settings(
            {
                "smtp_host": get_setting(DB_PATH, "smtp_host"),
                "smtp_port": get_setting(DB_PATH, "smtp_port"),
                "smtp_user": get_setting(DB_PATH, "smtp_user"),
                "smtp_use_tls": get_setting(DB_PATH, "smtp_use_tls"),
                "smtp_use_ssl": get_setting(DB_PATH, "smtp_use_ssl"),
                "smtp_from": get_setting(DB_PATH, "smtp_from"),
                "smtp_to": get_setting(DB_PATH, "smtp_to"),
            },
            allow_empty=False,
        )
    except SmtpValidationError as exc:
        raise RuntimeError(str(exc)) from exc
    host = smtp_settings["smtp_host"]
    port = int(smtp_settings["smtp_port"])
    user = smtp_settings["smtp_user"]
    password = get_setting(DB_PATH, "smtp_password") or ""
    use_tls = smtp_settings["smtp_use_tls"] == "1"
    use_ssl = smtp_settings["smtp_use_ssl"] == "1"
    sender = smtp_settings["smtp_from"] or user
    recipient = smtp_settings["smtp_to"] or user

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)

    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=30) as server:
        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()
        if user and password:
            server.login(user, password)
        server.send_message(msg)


def send_test_email():
    send_email_message(
        f"{PROJECT_NAME} - test email",
        f"Test konfiguracji SMTP z projektu {PROJECT_NAME}.",
    )


def send_low_stock_alert_email(rows):
    lines = [
        f"Alert niskich stanow - {PROJECT_NAME}",
        "",
        f"Liczba pozycji: {len(rows)}",
        "",
    ]
    for row in rows:
        lines.append(
            "- {name} | EAN: {ean} | stan: {qty} | sugerowany: {suggested} | brak: {shortage}".format(
                name=row["name"],
                ean=row["ean"] or "-",
                qty=row["quantity"],
                suggested=row["suggested_qty"],
                shortage=row["shortage_qty"],
            )
        )
    send_email_message(f"{PROJECT_NAME} - alert niskich stanow", "\n".join(lines))


@app.get("/thumb/<int:apilo_id>")
@login_required
def thumb(apilo_id):
    product = get_product_by_apilo_id(DB_PATH, apilo_id)
    if not product or not product["image_url"]:
        abort(404)
    url = product["image_url"]
    filename, local_path = get_thumb_local_path(apilo_id, url)
    if not filename:
        abort(404)
    has_local_thumb = os.path.exists(local_path)
    if has_local_thumb and THUMB_TTL_SECONDS > 0:
        if is_thumb_fresh(local_path):
            return send_thumb_file(filename, max_age=THUMB_TTL_SECONDS)
        schedule_thumbnail_refresh(apilo_id, url, local_path)
        return send_thumb_file(filename)
    try:
        download_thumbnail(url, local_path)
    except Exception:
        if has_local_thumb:
            return send_thumb_file(filename)
        return redirect(url)
    max_age = THUMB_TTL_SECONDS if THUMB_TTL_SECONDS > 0 else None
    return send_thumb_file(filename, max_age=max_age)


if __name__ == "__main__":
    start_background_refresh(DEBUG_MODE)
    app.run(host=APP_HOST, port=APP_PORT, debug=DEBUG_MODE, use_reloader=DEBUG_MODE)
