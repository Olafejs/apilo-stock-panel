from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from scripts import sync_allegro_attributes

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class ChunkResponse:
    headers = {"Content-Type": "image/png"}

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield b"too-large"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_thumbnail_failure_cleans_private_lifecycle(app_module, monkeypatch, tmp_path):
    thumb_dir = tmp_path / "thumbs"
    thumb_dir.mkdir()
    monkeypatch.setattr(app_module, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(app_module, "THUMB_MAX_DOWNLOAD_BYTES", 1)
    monkeypatch.setattr(app_module.requests, "get", lambda *args, **kwargs: ChunkResponse())

    with pytest.raises(ValueError, match="size limit"):
        app_module.download_thumbnail("https://example.com/image.png", str(thumb_dir / "1.png"))

    assert list(thumb_dir.iterdir()) == []


def test_thumbnail_rejects_destination_outside_cache_before_network(
    app_module, monkeypatch, tmp_path
):
    thumb_dir = tmp_path / "thumbs"
    outside = tmp_path / "outside"
    thumb_dir.mkdir()
    outside.mkdir()
    monkeypatch.setattr(app_module, "THUMB_DIR", str(thumb_dir))
    monkeypatch.setattr(
        app_module.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )

    with pytest.raises(ValueError, match="outside cache"):
        app_module.download_thumbnail(
            "https://example.com/image.png", str(outside / "1.png")
        )

    assert list(thumb_dir.iterdir()) == []
    assert list(outside.iterdir()) == []


def test_app_adds_baseline_browser_security_headers(client):
    response = client.get("/login")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_allegro_get_retries_retryable_http_error(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), timeout))
        if len(calls) == 1:
            headers = Message()
            headers["Retry-After"] = "0"
            raise urllib.error.HTTPError(
                request.full_url,
                503,
                "temporary",
                headers,
                None,
            )
        return FakeResponse({"id": "offer-1"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sync_allegro_attributes.time, "sleep", sleeps.append)

    result = sync_allegro_attributes.allegro_get_product_offer("1", "token-fixture")

    assert result == {"id": "offer-1"}
    assert calls == [("GET", 30), ("GET", 30)]
    assert sleeps == [0.0]


def test_release_deploy_syncs_channels_without_concurrent_live_worker():
    script = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")

    changed_at = script.index("\nLIVE_CHANGED=1\n")
    stop_at = script.index('docker stop "$LIVE_CONTAINER"', changed_at)
    one_shot_at = script.index("CHANNEL_SYNC_OK")
    promote_at = script.index('docker tag "$CANDIDATE_IMAGE" "$RELEASE_IMAGE"')
    restart_at = script.index(
        'docker compose up -d --no-build --force-recreate "$SERVICE"', promote_at
    )

    assert changed_at < stop_at < one_shot_at < promote_at < restart_at
    assert "--read-only" in script
    assert "--cap-drop ALL" in script
    assert "--security-opt no-new-privileges:true" in script
    assert "channels < 5 or listings < 1" in script
    assert "restore_database_backup" in script
    assert "source.backup(destination)" in script
    assert "os.replace(temporary_path, live_path)" in script
    assert "local restore_status=$?" in script
    assert '[[ "$restore_status" -eq 0 ]] || return "$restore_status"' in script
    assert "DATABASE_RESTORED=$BACKUP_PATH" in script
    live_changed_section = script.split("\nLIVE_CHANGED=1\n", 1)[1].split(
        "\nLIVE_CHANGED=0\n", 1
    )[0]
    assert "exit 1" not in live_changed_section
    assert "rollback 1" in live_changed_section


def test_release_embedded_restore_atomically_restores_verified_sqlite(tmp_path):
    deployment = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")
    restore_block = deployment.split("restore_database_backup() {", 1)[1].split(
        "\nrollback() {", 1
    )[0]
    python_source = restore_block.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    restore_script = tmp_path / "restore.py"
    restore_script.write_text(python_source, encoding="utf-8")
    backup_path = tmp_path / "backup.sqlite3"
    live_path = tmp_path / "live.sqlite3"

    for path, value in ((backup_path, "backup"), (live_path, "candidate")):
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker (value) VALUES (?)", (value,))
        connection.commit()
        connection.close()

    subprocess.run(
        [sys.executable, str(restore_script), str(backup_path), str(live_path)],
        check=True,
    )

    restored = sqlite3.connect(f"file:{live_path}?mode=ro", uri=True)
    assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert restored.execute("SELECT value FROM marker").fetchone()[0] == "backup"
    restored.close()
    assert not Path(str(live_path) + "-wal").exists()
    assert not Path(str(live_path) + "-shm").exists()


def test_allegro_get_does_not_retry_non_retryable_http_error(monkeypatch):
    calls = []
    sleeps = []

    def fake_urlopen(request, timeout):
        calls.append((request.get_method(), timeout))
        raise urllib.error.HTTPError(request.full_url, 404, "not found", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sync_allegro_attributes.time, "sleep", sleeps.append)

    with pytest.raises(urllib.error.HTTPError) as captured:
        sync_allegro_attributes.allegro_get_product_offer("missing", "token-fixture")

    assert captured.value.code == 404
    assert calls == [("GET", 30)]
    assert sleeps == []


def test_retry_helper_rejects_post_before_network(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called")),
    )
    request = urllib.request.Request(
        "https://example.com/token", data=b"grant_type=refresh_token", method="POST"
    )

    with pytest.raises(ValueError, match="GET requests only"):
        sync_allegro_attributes.urlopen_json_get_with_retry(request)


def test_allegro_attribute_sync_never_selects_manual_overrides(tmp_path):
    db_path = tmp_path / "attributes.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            allegro_auction_id TEXT,
            material TEXT,
            color TEXT,
            attributes_source TEXT,
            present_in_apilo INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO products
            (id, name, allegro_auction_id, material, color, attributes_source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "Ręczny", "100", "CARBON", "czarny", "manual_user_hint"),
            (2, "Automatyczny", "200", "", "", "allegro_description"),
        ],
    )

    regular = sync_allegro_attributes.select_products(connection, force=False)
    forced = sync_allegro_attributes.select_products(connection, force=True)
    connection.close()

    assert [row["id"] for row in regular] == [2]
    assert [row["id"] for row in forced] == [2]


def test_allegro_attribute_sync_cannot_race_with_manual_edit(tmp_path, monkeypatch):
    db_path = tmp_path / "attributes-race.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT,
            allegro_auction_id TEXT,
            material TEXT,
            color TEXT,
            attributes_source TEXT,
            attributes_updated_at TEXT,
            present_in_apilo INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    connection.execute(
        """
        INSERT INTO products
            (id, name, allegro_auction_id, material, color, attributes_source)
        VALUES (1, 'Produkt', '100', '', '', 'allegro_description')
        """
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(sync_allegro_attributes, "get_access_token", lambda: "token")

    def manual_edit_during_api_read(offer_id, token):
        assert offer_id == "100"
        assert token == "token"
        concurrent = sqlite3.connect(db_path)
        concurrent.execute(
            """
            UPDATE products
            SET material = 'CARBON', color = 'czarny', attributes_source = 'manual_user_hint'
            WHERE id = 1
            """
        )
        concurrent.commit()
        concurrent.close()
        return {"description": "Materiał: PLA. Kolor: biały."}

    monkeypatch.setattr(
        sync_allegro_attributes,
        "allegro_get_product_offer",
        manual_edit_during_api_read,
    )

    result = sync_allegro_attributes.sync(db_path)
    connection = sqlite3.connect(db_path)
    saved = connection.execute(
        "SELECT material, color, attributes_source FROM products WHERE id = 1"
    ).fetchone()
    connection.close()

    assert result == {"checked": 1, "updated": 0, "skipped": 1, "errors": 0}
    assert saved == ("CARBON", "czarny", "manual_user_hint")


def test_start_script_uses_full_strict_mode():
    lines = (ROOT / "start.sh").read_text().splitlines()
    assert "set -Eeuo pipefail" in lines[:5]


def test_container_and_gunicorn_default_to_loopback():
    assert "APP_HOST=127.0.0.1" in (ROOT / "Dockerfile").read_text()
    gunicorn = (ROOT / "gunicorn.conf.py").read_text()
    assert "os.getenv('APP_HOST', '127.0.0.1')" in gunicorn
    assert "workers = 1" in gunicorn
    assert "BACKGROUND_REFRESH_ENABLED" in gunicorn


def test_app_sync_job_label_wrapper_uses_runtime_module(app_module):
    assert app_module.get_sync_job_label("inventory") == "synchronizacja magazynu"
    assert app_module.get_sync_job_label("unknown") == "synchronizacja"
