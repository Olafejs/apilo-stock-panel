from app_alerts import (
    build_low_stock_alert_history,
    build_low_stock_alert_signature,
    format_position_count,
    get_low_stock_alert_enabled,
    mark_low_stock_alert_error,
)
from db import get_recent_audit_log, get_setting, record_audit_log, set_setting


def test_low_stock_alert_signature_is_stable_for_row_order():
    rows_a = [
        {"ean": "2", "name": "B", "quantity": 1, "suggested_qty": 4, "shortage_qty": 3},
        {"ean": "1", "name": "A", "quantity": 0, "suggested_qty": 2, "shortage_qty": 2},
    ]
    rows_b = list(reversed(rows_a))

    assert build_low_stock_alert_signature(rows_a) == build_low_stock_alert_signature(rows_b)


def test_mark_low_stock_alert_error_and_enabled_flag_use_db_state(app_module):
    assert get_low_stock_alert_enabled(app_module.DB_PATH) is False
    set_setting(app_module.DB_PATH, "alerts_low_stock_enabled", "1")

    mark_low_stock_alert_error(app_module.DB_PATH, "Blad testowy", mode="manual")

    assert get_low_stock_alert_enabled(app_module.DB_PATH) is True
    assert get_setting(app_module.DB_PATH, "alerts_low_stock_last_result") == "Błąd ręcznego alertu."
    assert get_setting(app_module.DB_PATH, "alerts_low_stock_last_error") == "Blad testowy"


def test_build_low_stock_alert_history_formats_recent_entries(app_module):
    record_audit_log(
        app_module.DB_PATH,
        action="low_stock_alert_send",
        entity_type="email",
        entity_label="Alert niskich stanów",
        new_value="2 pozycje",
        details={"mode": "auto", "count": 2},
        actor_ip="system",
    )
    record_audit_log(
        app_module.DB_PATH,
        action="low_stock_alert_send",
        entity_type="email",
        entity_label="Alert niskich stanów",
        new_value="1 pozycja",
        details={"mode": "manual", "count": 1},
        actor_ip="127.0.0.1",
    )

    history = build_low_stock_alert_history(
        app_module.DB_PATH,
        limit=10,
        format_pull_time_fn=lambda value: value[:16],
        format_position_count_fn=format_position_count,
    )

    assert len(history) == 2
    assert {item["count_label"] for item in history} == {"1 pozycja", "2 pozycje"}
    assert {item["mode_label"] for item in history} == {"Auto", "Ręcznie"}
    assert get_recent_audit_log(app_module.DB_PATH, limit=2)
