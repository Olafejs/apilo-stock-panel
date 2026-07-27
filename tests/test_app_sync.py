from datetime import datetime, timedelta, timezone

from app_sync import (
    build_sync_status_payload,
    ensure_sync_schedule,
    get_sync_status_snapshot,
    mark_sync_failed,
    mark_sync_finished,
    mark_sync_started,
    should_refresh_year_sales_cache,
    update_sync_status,
)


def reset_sync_state():
    update_sync_status(
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


def test_ensure_sync_schedule_populates_missing_next_runs():
    reset_sync_state()

    ensure_sync_schedule(
        last_pull_at="",
        sales_cache_at="",
        refresh_interval_seconds=600,
        sales_cache_refresh_interval_seconds=1800,
    )

    snapshot = get_sync_status_snapshot()
    assert snapshot["next_inventory_sync_at"]
    assert snapshot["next_sales_refresh_at"]


def test_mark_sync_status_updates_runtime_state():
    reset_sync_state()

    mark_sync_started("inventory")
    started = get_sync_status_snapshot()
    assert started["running"] is True
    assert started["job"] == "inventory"

    mark_sync_finished("inventory")
    finished = get_sync_status_snapshot()
    assert finished["running"] is False
    assert finished["last_success_job"] == "inventory"

    mark_sync_failed("Blad testowy")
    failed = get_sync_status_snapshot()
    assert failed["running"] is False
    assert failed["last_error"] == "Blad testowy"


def test_build_sync_status_payload_exposes_human_labels():
    reset_sync_state()
    mark_sync_started("sales_cache")

    payload = build_sync_status_payload(
        last_pull_at="2026-03-11T10:00:00+00:00",
        sales_cache_at="2026-03-11T11:00:00+00:00",
        sales_year_cache_at="2026-03-11T12:00:00+00:00",
    )

    assert payload["running"] is True
    assert payload["job_label"] == "odświeżanie sugestii sprzedaży"
    assert payload["state_label"] == "Trwa odświeżanie sugestii sprzedaży"


def test_should_refresh_year_sales_cache_respects_age_and_suggest_days():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(hours=8)).isoformat()

    assert should_refresh_year_sales_cache("", 30, 21600) is True
    assert should_refresh_year_sales_cache(recent, 30, 21600) is False
    assert should_refresh_year_sales_cache(stale, 30, 21600) is True
    assert should_refresh_year_sales_cache(recent, 365, 21600) is True
