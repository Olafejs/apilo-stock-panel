import importlib
import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT_DIR))
    monkeypatch.setenv("APILO_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv(
        "SETTINGS_ENCRYPTION_KEY",
        "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_SETUP_TOKEN", raising=False)
    monkeypatch.delenv("TRUST_X_FORWARDED_FOR", raising=False)

    sys.modules.pop("app", None)
    sys.modules.pop("app_admin", None)
    sys.modules.pop("app_alerts", None)
    sys.modules.pop("app_auth", None)
    sys.modules.pop("app_config", None)
    sys.modules.pop("app_reporting", None)
    sys.modules.pop("app_sync", None)
    sys.modules.pop("app_utils", None)
    sys.modules.pop("db", None)
    app_module = importlib.import_module("app")
    app_module.app.config.update(TESTING=True)
    app_module.update_sync_status(
        running=False,
        job="",
        started_at="",
        finished_at="",
        last_success_job="",
        last_success_at="",
        last_error="",
        last_error_at="",
        next_inventory_sync_at="",
        next_sales_refresh_at="",
    )
    return app_module


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


@pytest.fixture
def logged_in_client(app_module):
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True
        session["logged_in_at"] = "2026-03-11T00:00:00+00:00"
        session["csrf_token"] = "test-csrf-token"
    return client
