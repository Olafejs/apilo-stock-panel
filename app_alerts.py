import hashlib
import json
import secrets
from datetime import datetime, timezone

from app_utils import parse_bool_value, parse_datetime_value, parse_int_value, utc_now_iso
from db import get_products, get_recent_audit_log, get_setting, set_setting


def get_low_stock_rows(db_path, lead_time_days, safety_pct, suggest_days, limit=10):
    rows = get_products(
        db_path,
        preset="shortage",
        sort="shortage",
        order="desc",
        limit=limit,
        offset=0,
        lead_time_days=lead_time_days,
        safety_pct=safety_pct,
        suggest_days=suggest_days,
    )
    result = []
    for row in rows:
        current_qty = row["quantity"] if row["quantity"] is not None else 0
        suggested_qty = row["suggested_qty"] if row["suggested_qty"] is not None else 0
        shortage_qty = row["shortage_qty"] if row["shortage_qty"] is not None else 0
        result.append(
            {
                "name": row["name"] or "-",
                "ean": row["ean"] or "",
                "quantity": current_qty,
                "suggested_qty": suggested_qty,
                "shortage_qty": shortage_qty,
            }
        )
    return result


def get_low_stock_alert_enabled(db_path):
    return parse_bool_value(get_setting(db_path, "alerts_low_stock_enabled"), default=False)


def get_low_stock_alert_interval_hours(db_path):
    return parse_int_value(
        get_setting(db_path, "alerts_low_stock_interval_hours"),
        24,
        min_value=1,
        max_value=720,
    )


def summarize_low_stock_alert_settings_snapshot(enabled, interval_hours):
    return f"auto={'1' if enabled else '0'} interval={interval_hours}h"


def format_item_count(count, singular, paucal, plural):
    count = int(count or 0)
    mod10 = count % 10
    mod100 = count % 100
    if count == 1:
        word = singular
    elif 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        word = paucal
    else:
        word = plural
    return f"{count} {word}"


def format_position_count(count):
    return format_item_count(count, "pozycja", "pozycje", "pozycji")


def get_low_stock_alert_next_check_iso(db_path, compute_next_run_at):
    if not get_low_stock_alert_enabled(db_path):
        return ""
    return compute_next_run_at(
        get_setting(db_path, "alerts_low_stock_last_check_at"),
        get_low_stock_alert_interval_hours(db_path) * 3600,
    )


def is_low_stock_alert_due(db_path, compute_next_run_at, now=None):
    if not get_low_stock_alert_enabled(db_path):
        return False
    scheduled_at = parse_datetime_value(
        get_low_stock_alert_next_check_iso(db_path, compute_next_run_at)
    )
    if not scheduled_at:
        return True
    now = now or datetime.now(timezone.utc)
    return scheduled_at <= now


def build_low_stock_alert_signature(rows):
    normalized_rows = []
    for row in rows:
        normalized_rows.append(
            {
                "ean": row.get("ean") or "",
                "name": row.get("name") or "",
                "quantity": int(row.get("quantity") or 0),
                "suggested_qty": int(row.get("suggested_qty") or 0),
                "shortage_qty": int(row.get("shortage_qty") or 0),
            }
        )
    normalized_rows.sort(key=lambda item: (item["ean"], item["name"]))
    payload = json.dumps(normalized_rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def update_low_stock_alert_state(
    db_path,
    *,
    checked_at=None,
    result_message=None,
    error_message=None,
    signature=None,
    sent_count=None,
    sent_at=None,
):
    checked_at = checked_at or utc_now_iso()
    set_setting(db_path, "alerts_low_stock_last_check_at", checked_at)
    if result_message is not None:
        set_setting(db_path, "alerts_low_stock_last_result", result_message)
    if error_message:
        set_setting(db_path, "alerts_low_stock_last_error", error_message)
        set_setting(db_path, "alerts_low_stock_last_error_at", checked_at)
    else:
        set_setting(db_path, "alerts_low_stock_last_error", "")
        set_setting(db_path, "alerts_low_stock_last_error_at", "")
    if signature is not None:
        set_setting(db_path, "alerts_low_stock_last_hash", signature)
    if sent_at is not None:
        set_setting(db_path, "alerts_low_stock_sent_at", sent_at)
    if sent_count is not None:
        set_setting(db_path, "alerts_low_stock_sent_count", str(sent_count))


def mark_low_stock_alert_error(db_path, message, *, mode="auto"):
    result_prefix = "Błąd automatycznego alertu." if mode == "auto" else "Błąd ręcznego alertu."
    update_low_stock_alert_state(
        db_path,
        checked_at=utc_now_iso(),
        result_message=result_prefix,
        error_message=message,
    )


def process_low_stock_alert(
    db_path,
    *,
    mode,
    low_stock_row_limit,
    get_low_stock_rows_fn,
    send_low_stock_alert_email_fn,
    record_audit_event_fn,
    format_position_count_fn,
):
    checked_at = utc_now_iso()
    alert_rows = get_low_stock_rows_fn(limit=low_stock_row_limit)
    if not alert_rows:
        update_low_stock_alert_state(
            db_path,
            checked_at=checked_at,
            result_message="Brak pozycji do alertu.",
            signature="",
        )
        return {"status": "empty", "count": 0}

    signature = build_low_stock_alert_signature(alert_rows)
    previous_signature = get_setting(db_path, "alerts_low_stock_last_hash") or ""
    if mode == "auto" and previous_signature and secrets.compare_digest(previous_signature, signature):
        update_low_stock_alert_state(
            db_path,
            checked_at=checked_at,
            result_message="Brak zmian od ostatniej wysyłki.",
            signature=signature,
        )
        return {"status": "duplicate", "count": len(alert_rows)}

    send_low_stock_alert_email_fn(alert_rows)
    result_message = (
        f"Wysłano alert automatycznie ({format_position_count_fn(len(alert_rows))})."
        if mode == "auto"
        else f"Wysłano alert ręcznie ({format_position_count_fn(len(alert_rows))})."
    )
    update_low_stock_alert_state(
        db_path,
        checked_at=checked_at,
        result_message=result_message,
        signature=signature,
        sent_count=len(alert_rows),
        sent_at=checked_at,
    )
    record_audit_event_fn(
        "low_stock_alert_send",
        "email",
        entity_label="Alert niskich stanów",
        new_value=format_position_count_fn(len(alert_rows)),
        details={
            "mode": mode,
            "count": len(alert_rows),
            "signature": signature[:16],
        },
        actor_ip="system" if mode == "auto" else None,
    )
    return {"status": "sent", "count": len(alert_rows)}


def build_low_stock_alert_history(db_path, *, limit=10, format_pull_time_fn, format_position_count_fn):
    history = []
    scan_limit = max(limit * 8, 40)
    for row in get_recent_audit_log(db_path, limit=scan_limit):
        if row["action"] != "low_stock_alert_send":
            continue
        details = {}
        details_json = row["details_json"]
        if details_json:
            try:
                details = json.loads(details_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
        mode = details.get("mode") or "manual"
        history.append(
            {
                "created_at": format_pull_time_fn(row["created_at"]),
                "mode_label": "Auto" if mode == "auto" else "Ręcznie",
                "count_label": (
                    format_position_count_fn(details["count"])
                    if details.get("count") is not None
                    else row["new_value"] or "-"
                ),
                "actor_ip": row["actor_ip"] or ("system" if mode == "auto" else "-"),
            }
        )
        if len(history) >= limit:
            break
    return history
