import logging

from app_admin import (
    build_recent_audit_entries,
    build_secret_storage_payload,
    get_email_settings_snapshot,
    summarize_inventory_values_snapshot,
    write_audit_event,
)
from db import set_setting


def test_build_secret_storage_payload_supports_env_and_file_modes():
    assert build_secret_storage_payload({"backend": "env", "key_path": ""}) == {
        "mode_label": "Klucz z .env",
        "location": "SETTINGS_ENCRYPTION_KEY",
    }
    assert build_secret_storage_payload({"backend": "file", "key_path": "/tmp/settings.key"}) == {
        "mode_label": "Plik klucza",
        "location": "/tmp/settings.key",
    }


def test_email_settings_snapshot_reads_current_values(app_module):
    set_setting(app_module.DB_PATH, "smtp_host", "smtp.example.com")
    set_setting(app_module.DB_PATH, "smtp_port", "587")
    set_setting(app_module.DB_PATH, "smtp_user", "user@example.com")
    set_setting(app_module.DB_PATH, "smtp_password", "tajne")

    snapshot = get_email_settings_snapshot(app_module.DB_PATH)

    assert snapshot["smtp_host"] == "smtp.example.com"
    assert snapshot["smtp_port"] == "587"
    assert snapshot["smtp_user"] == "user@example.com"
    assert snapshot["has_password"] is True


def test_build_recent_audit_entries_formats_labels_and_changes(app_module):
    write_audit_event(
        app_module.DB_PATH,
        logging.getLogger("test"),
        action="inventory_value_refresh",
        entity_type="settings",
        entity_label="Wartosc magazynu",
        old_value=summarize_inventory_values_snapshot("10.00", "12.00"),
        new_value=summarize_inventory_values_snapshot("15.00", "18.00"),
        actor_ip="127.0.0.1",
    )

    entries = build_recent_audit_entries(app_module.DB_PATH, limit=5)

    assert entries[0]["action"] == "Przeliczenie wartości"
    assert entries[0]["entity_type"] == "Ustawienia"
    assert "sklep=10.00 allegro=12.00 -> sklep=15.00 allegro=18.00" == entries[0]["change"]
    assert entries[0]["actor_ip"] == "127.0.0.1"
