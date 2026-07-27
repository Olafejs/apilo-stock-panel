import os
import threading
from datetime import datetime, timedelta, timezone

from app_utils import format_pull_time, parse_datetime_value, utc_now_iso


SYNC_JOB_LABELS = {
    "inventory": "synchronizacja magazynu",
    "sales_cache": "odświeżanie sugestii sprzedaży",
}

SYNC_STATUS_LOCK = threading.Lock()
SYNC_STATUS = {
    "running": False,
    "job": "",
    "started_at": "",
    "finished_at": "",
    "last_success_job": "",
    "last_success_at": "",
    "last_error": "",
    "last_error_at": "",
    "next_inventory_sync_at": "",
    "next_sales_refresh_at": "",
}

BACKGROUND_REFRESH_LOCK = threading.Lock()
BACKGROUND_REFRESH_STARTED = False


def update_sync_status(**changes):
    with SYNC_STATUS_LOCK:
        SYNC_STATUS.update(changes)


def get_sync_status_snapshot():
    with SYNC_STATUS_LOCK:
        return dict(SYNC_STATUS)


def get_sync_job_label(job):
    return SYNC_JOB_LABELS.get(job, "synchronizacja")


def compute_next_run_at(last_value, interval_seconds):
    now = datetime.now(timezone.utc)
    last_dt = parse_datetime_value(last_value)
    if not last_dt:
        return now.isoformat()
    next_dt = last_dt + timedelta(seconds=interval_seconds)
    if next_dt < now:
        next_dt = now
    return next_dt.isoformat()


def ensure_sync_schedule(
    *,
    last_pull_at,
    sales_cache_at,
    refresh_interval_seconds,
    sales_cache_refresh_interval_seconds,
):
    changes = {}
    snapshot = get_sync_status_snapshot()
    if not snapshot.get("next_inventory_sync_at"):
        changes["next_inventory_sync_at"] = compute_next_run_at(
            last_pull_at, refresh_interval_seconds
        )
    if not snapshot.get("next_sales_refresh_at"):
        changes["next_sales_refresh_at"] = compute_next_run_at(
            sales_cache_at, sales_cache_refresh_interval_seconds
        )
    if changes:
        update_sync_status(**changes)


def schedule_inventory_sync(refresh_interval_seconds, reference_time=None, retry=False):
    reference_time = reference_time or datetime.now(timezone.utc)
    delay_seconds = min(60, refresh_interval_seconds) if retry else refresh_interval_seconds
    update_sync_status(
        next_inventory_sync_at=(reference_time + timedelta(seconds=delay_seconds)).isoformat()
    )


def schedule_sales_refresh(
    sales_cache_refresh_interval_seconds,
    reference_time=None,
    retry=False,
):
    reference_time = reference_time or datetime.now(timezone.utc)
    delay_seconds = (
        min(300, sales_cache_refresh_interval_seconds)
        if retry
        else sales_cache_refresh_interval_seconds
    )
    update_sync_status(
        next_sales_refresh_at=(reference_time + timedelta(seconds=delay_seconds)).isoformat()
    )


def mark_sync_started(job):
    now = utc_now_iso()
    update_sync_status(
        running=True,
        job=job,
        started_at=now,
        finished_at="",
    )


def mark_sync_finished(job):
    now = utc_now_iso()
    update_sync_status(
        running=False,
        job="",
        finished_at=now,
        last_success_job=job,
        last_success_at=now,
        last_error="",
        last_error_at="",
    )


def mark_sync_failed(error_message):
    now = utc_now_iso()
    update_sync_status(
        running=False,
        job="",
        finished_at=now,
        last_error=error_message,
        last_error_at=now,
    )


def is_schedule_due(value, now=None):
    scheduled_at = parse_datetime_value(value)
    if not scheduled_at:
        return True
    now = now or datetime.now(timezone.utc)
    return scheduled_at <= now


def should_refresh_year_sales_cache(last_year_refresh_at, suggest_days, interval_seconds, force=False):
    if force or suggest_days == 365:
        return True
    last_year_refresh = parse_datetime_value(last_year_refresh_at)
    if not last_year_refresh:
        return True
    return last_year_refresh + timedelta(seconds=interval_seconds) <= datetime.now(timezone.utc)


def build_sync_status_payload(
    *,
    last_pull_at,
    sales_cache_at,
    sales_year_cache_at,
):
    snapshot = get_sync_status_snapshot()
    running = snapshot.get("running", False)
    running_job = snapshot.get("job") or ""
    if running and running_job:
        state_label = f"Trwa {get_sync_job_label(running_job)}"
    elif snapshot.get("last_error"):
        state_label = f"Ostatni błąd: {snapshot['last_error']}"
    else:
        state_label = "Auto-sync aktywny"
    return {
        "running": running,
        "job": running_job,
        "job_label": get_sync_job_label(running_job) if running_job else "",
        "state_label": state_label,
        "last_inventory_sync_at": format_pull_time(last_pull_at or ""),
        "last_sales_refresh_at": format_pull_time(sales_cache_at or ""),
        "last_sales_year_refresh_at": format_pull_time(sales_year_cache_at or ""),
        "last_error": snapshot.get("last_error") or "",
        "last_error_at": format_pull_time(snapshot.get("last_error_at") or ""),
        "next_inventory_sync_at": snapshot.get("next_inventory_sync_at") or "",
        "next_sales_refresh_at": snapshot.get("next_sales_refresh_at") or "",
    }


def start_background_refresh(debug_mode, target):
    if debug_mode and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return False
    global BACKGROUND_REFRESH_STARTED
    with BACKGROUND_REFRESH_LOCK:
        if BACKGROUND_REFRESH_STARTED:
            return False
        thread = threading.Thread(
            target=target,
            name="background-refresh",
            daemon=True,
        )
        thread.start()
        BACKGROUND_REFRESH_STARTED = True
    return True
