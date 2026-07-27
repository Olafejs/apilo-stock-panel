from app_utils import format_pull_time
from db import get_recent_audit_log, get_setting, record_audit_log


AUDIT_ACTION_LABELS = {
    "login_success": "Logowanie",
    "password_setup": "Pierwsze hasło",
    "password_change": "Zmiana hasła",
    "email_settings_update": "Ustawienia SMTP",
    "email_test_send": "Email testowy",
    "api_settings_update": "Ustawienia API",
    "api_connection_test": "Test API",
    "empik_settings_update": "Ustawienia Empik API",
    "empik_connection_test": "Test Empik API",
    "empik_api_sync": "Synchronizacja Empik API",
    "empik_error_report_download": "Raport błędów Empik",
    "allegro_settings_update": "Cennik Allegro",
    "suggestions_settings_update": "Ustawienia sugestii",
    "suggestions_refresh": "Przeliczenie sugestii",
    "inventory_value_refresh": "Przeliczenie wartości",
    "products_export_csv": "Eksport CSV",
    "low_stock_alert_settings_update": "Auto alert stanów",
    "low_stock_alert_send": "Alert niskich stanów",
    "product_quantity_update": "Zmiana stanu",
    "product_quantity_conflict": "Konflikt stanu",
    "product_quantity_unverified": "Niepotwierdzony stan",
    "product_attributes_update": "Zmiana danych produktu",
    "manual_sync_pull": "Pobranie z Apilo",
    "channel_description_recheck": "Ponowna kontrola opisu",
    "allegro_description_update": "Aktualizacja opisu Allegro",
    "channel_visibility_settings_update": "Widoczność kanałów",
}

AUDIT_ENTITY_LABELS = {
    "auth": "Logowanie",
    "security": "Bezpieczeństwo",
    "email": "Email",
    "api": "API",
    "settings": "Ustawienia",
    "product": "Produkt",
    "sync": "Synchronizacja",
}


def build_secret_storage_payload(secret_storage_status):
    backend = secret_storage_status.get("backend") or "file"
    if backend == "env":
        return {
            "mode_label": "Klucz z .env",
            "location": "SETTINGS_ENCRYPTION_KEY",
        }
    return {
        "mode_label": "Plik klucza",
        "location": secret_storage_status.get("key_path") or "settings.key",
    }


def get_email_settings_snapshot(db_path):
    return {
        "smtp_host": get_setting(db_path, "smtp_host") or "",
        "smtp_port": get_setting(db_path, "smtp_port") or "",
        "smtp_user": get_setting(db_path, "smtp_user") or "",
        "smtp_use_tls": get_setting(db_path, "smtp_use_tls") or "0",
        "smtp_use_ssl": get_setting(db_path, "smtp_use_ssl") or "0",
        "smtp_from": get_setting(db_path, "smtp_from") or "",
        "smtp_to": get_setting(db_path, "smtp_to") or "",
        "has_password": bool(get_setting(db_path, "smtp_password")),
    }


def get_api_settings_snapshot(db_path):
    return {
        "apilo_base_url": get_setting(db_path, "apilo_base_url") or "",
        "apilo_client_id": get_setting(db_path, "apilo_client_id") or "",
        "has_client_secret": bool(get_setting(db_path, "apilo_client_secret")),
    }


def get_empik_settings_snapshot(db_path):
    return {
        "shop_id": get_setting(db_path, "empik_shop_id") or "",
        "has_api_key": bool(get_setting(db_path, "empik_api_key")),
    }


def snapshot_value_text(value):
    if value is None or value == "":
        return "-"
    return str(value)


def summarize_email_settings_snapshot(values):
    return (
        f"host={values.get('smtp_host') or '-'} "
        f"port={values.get('smtp_port') or '-'} "
        f"user={values.get('smtp_user') or '-'} "
        f"tls={values.get('smtp_use_tls') or '0'} "
        f"ssl={values.get('smtp_use_ssl') or '0'} "
        f"from={values.get('smtp_from') or '-'} "
        f"to={values.get('smtp_to') or '-'} "
        f"haslo={'set' if values.get('has_password') else 'empty'}"
    )


def summarize_api_settings_snapshot(base_url, client_id, has_secret):
    return (
        f"base={base_url or '-'} "
        f"client_id={client_id or '-'} "
        f"secret={'set' if has_secret else 'empty'}"
    )


def summarize_empik_settings_snapshot(shop_id, has_api_key):
    return f"shop_id={shop_id or '-'} api_key={'set' if has_api_key else 'empty'}"


def summarize_suggestions_settings_snapshot(lead_time_days, safety_pct, suggest_days):
    return (
        f"lead={snapshot_value_text(lead_time_days)} "
        f"days={snapshot_value_text(suggest_days)} "
        f"safety={snapshot_value_text(safety_pct)}%"
    )


def summarize_inventory_values_snapshot(store_value, allegro_value):
    return (
        f"sklep={snapshot_value_text(store_value)} "
        f"allegro={snapshot_value_text(allegro_value)}"
    )


def write_audit_event(
    db_path,
    logger,
    *,
    action,
    entity_type,
    entity_id=None,
    entity_label=None,
    old_value=None,
    new_value=None,
    details=None,
    actor_ip="",
):
    try:
        record_audit_log(
            db_path,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=entity_label,
            old_value=old_value,
            new_value=new_value,
            details=details,
            actor_ip=actor_ip,
        )
    except Exception:
        logger.exception("Audit log write failed for action=%s", action)


def build_recent_audit_entries(db_path, limit=40):
    entries = []
    for row in get_recent_audit_log(db_path, limit=limit):
        old_value = (row["old_value"] or "").strip()
        new_value = (row["new_value"] or "").strip()
        if old_value and new_value and old_value != new_value:
            change_text = f"{old_value} -> {new_value}"
        elif new_value:
            change_text = new_value
        elif old_value:
            change_text = old_value
        else:
            change_text = "-"
        entries.append(
            {
                "created_at": format_pull_time(row["created_at"]),
                "action": AUDIT_ACTION_LABELS.get(row["action"], row["action"]),
                "entity_type": AUDIT_ENTITY_LABELS.get(row["entity_type"], row["entity_type"]),
                "entity_label": row["entity_label"]
                or AUDIT_ENTITY_LABELS.get(row["entity_type"], row["entity_type"]),
                "change": change_text,
                "actor_ip": row["actor_ip"] or "-",
            }
        )
    return entries
