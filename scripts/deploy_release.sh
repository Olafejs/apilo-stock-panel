#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Uruchom: sudo $ROOT/scripts/deploy_release.sh" >&2
  exit 1
fi

OWNER_USER="${SUDO_USER:-$(id -un)}"
VERSION="$(tr -d '[:space:]' < VERSION)"
SERVICE="apilo-panel"
LIVE_CONTAINER="apilo-panel"
CANDIDATE_IMAGE="apilo-panel:${VERSION}-candidate"
RELEASE_IMAGE="apilo-panel:${VERSION}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ROLLBACK_IMAGE="apilo-panel:rollback-${STAMP}"
BACKUP_DIR="$ROOT/data/backups"
BACKUP_PATH="$BACKUP_DIR/apilo-pre-v${VERSION}-${STAMP}.sqlite3"
REPORT_PATH="$BACKUP_DIR/deploy-v${VERSION}-${STAMP}.txt"
LIVE_CHANGED=0
OLD_IMAGE_ID=""

wait_for_health() {
  local expected_version="${1:-}"
  python3 - "$expected_version" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

expected = sys.argv[1]
last_error = None
for _ in range(60):
    try:
        with urllib.request.urlopen("http://127.0.0.1:5080/healthz", timeout=4) as response:
            payload = json.load(response)
        if payload.get("status") != "ok":
            raise RuntimeError(f"health status={payload.get('status')}")
        if expected and payload.get("version") != expected:
            raise RuntimeError(
                f"version={payload.get('version')} expected={expected}"
            )
        print(f"HEALTH_OK version={payload.get('version')}")
        raise SystemExit(0)
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        last_error = exc
        time.sleep(1)
raise RuntimeError(f"health timeout: {last_error}")
PY
}

restore_database_backup() {
  python3 - "$BACKUP_PATH" "$ROOT/data/db/apilo.sqlite3" <<'PY'
import os
import sqlite3
import sys

backup_path, live_path = sys.argv[1:]
temporary_path = f"{live_path}.restore-{os.getpid()}"
for candidate in (temporary_path, temporary_path + "-wal", temporary_path + "-shm"):
    try:
        os.unlink(candidate)
    except FileNotFoundError:
        pass
source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
    raise RuntimeError("rollback backup quick_check failed")
destination = sqlite3.connect(temporary_path)
source.backup(destination)
if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
    raise RuntimeError("restored database quick_check failed")
destination.close()
source.close()
os.chmod(temporary_path, 0o600)
for suffix in ("-wal", "-shm"):
    try:
        os.unlink(live_path + suffix)
    except FileNotFoundError:
        pass
with open(temporary_path, "rb") as handle:
    os.fsync(handle.fileno())
os.replace(temporary_path, live_path)
directory_fd = os.open(os.path.dirname(live_path), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  local restore_status=$?
  [[ "$restore_status" -eq 0 ]] || return "$restore_status"
  chown "$OWNER_USER":"$OWNER_USER" "$ROOT/data/db/apilo.sqlite3" || return 1
  chmod 600 "$ROOT/data/db/apilo.sqlite3" || return 1
}

rollback() {
  local trapped_exit=$?
  local exit_code="${1:-$trapped_exit}"
  trap - ERR
  set +e
  if [[ "$LIVE_CHANGED" == "1" && -n "$OLD_IMAGE_ID" ]]; then
    echo "Wdrożenie nie przeszło kontroli. Przywracam poprzedni obraz i bazę." >&2
    docker stop "$LIVE_CONTAINER" >/dev/null 2>&1 || true
    if [[ "$(docker inspect --format '{{.State.Running}}' "$LIVE_CONTAINER" 2>/dev/null)" == "true" ]]; then
      echo "Nie udało się zatrzymać kontenera; baza nie zostanie przywrócona podczas pracy procesu." >&2
      exit_code=97
    elif ! restore_database_backup; then
      echo "Nie udało się przywrócić zweryfikowanego backupu SQLite." >&2
      exit_code=98
    elif ! docker tag "$OLD_IMAGE_ID" "$RELEASE_IMAGE" \
      || ! docker compose up -d --no-build --force-recreate "$SERVICE" \
      || ! wait_for_health ""; then
      echo "Baza została przywrócona, ale poprzedni obraz nie wrócił poprawnie." >&2
      exit_code=99
    else
      {
        echo "DEPLOY=ROLLBACK"
        echo "ROLLBACK_RESULT=PASS"
        echo "DATABASE_RESTORED=$BACKUP_PATH"
        echo "VERSION=$VERSION"
        echo "ROLLBACK_IMAGE=$ROLLBACK_IMAGE"
        echo "TIME=$(date --iso-8601=seconds)"
      } > "$REPORT_PATH"
      chown "$OWNER_USER":"$OWNER_USER" "$REPORT_PATH"
      chmod 600 "$REPORT_PATH"
      exit "$exit_code"
    fi
    {
      echo "DEPLOY=ROLLBACK_FAILED"
      echo "ROLLBACK_RESULT=FAIL"
      echo "VERSION=$VERSION"
      echo "BACKUP=$BACKUP_PATH"
      echo "ROLLBACK_IMAGE=$ROLLBACK_IMAGE"
      echo "TIME=$(date --iso-8601=seconds)"
    } > "$REPORT_PATH"
    chown "$OWNER_USER":"$OWNER_USER" "$REPORT_PATH" 2>/dev/null || true
    chmod 600 "$REPORT_PATH" 2>/dev/null || true
  fi
  exit "$exit_code"
}
trap rollback ERR

command -v docker >/dev/null
command -v runuser >/dev/null
docker info >/dev/null
docker inspect "$LIVE_CONTAINER" >/dev/null

runuser -u "$OWNER_USER" -- python3 scripts/check_release.py --release

install -d -m 0700 -o "$OWNER_USER" -g "$OWNER_USER" "$BACKUP_DIR"
python3 - "$BACKUP_PATH" <<'PY'
import sqlite3
import sys

source = sqlite3.connect("file:data/db/apilo.sqlite3?mode=ro", uri=True)
destination = sqlite3.connect(sys.argv[1])
source.backup(destination)
if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
    raise RuntimeError("backup quick_check failed")
destination.close()
source.close()
PY
chown "$OWNER_USER":"$OWNER_USER" "$BACKUP_PATH"
chmod 600 "$BACKUP_PATH"
echo "BACKUP_OK path=$BACKUP_PATH"

OLD_IMAGE_ID="$(docker inspect --format '{{.Image}}' "$LIVE_CONTAINER")"
docker tag "$OLD_IMAGE_ID" "$ROLLBACK_IMAGE"

docker build --build-arg "APP_VERSION=$VERSION" -t "$CANDIDATE_IMAGE" .
python3 scripts/runtime_canary.py

LIVE_CHANGED=1
docker stop "$LIVE_CONTAINER" >/dev/null
docker run --rm \
  --name "apilo-panel-sync-${STAMP}" \
  --network host \
  --user 1000:1000 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 128 \
  --memory 512m \
  --cpus 1.0 \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
  -e TZ=Europe/Warsaw \
  -e APILO_DB_PATH=/app/data/apilo.sqlite3 \
  -v "$ROOT/data/db:/app/data" \
  -v "$ROOT/data/logs:/app/logs" \
  -v "$ROOT/data/thumbs:/app/static/thumbs" \
  "$CANDIDATE_IMAGE" \
  python -c 'import app; count = app.run_sync_pull_with_lock(blocking=True); print(f"CHANNEL_SYNC_OK products={count}")'

docker tag "$CANDIDATE_IMAGE" "$RELEASE_IMAGE"
docker compose up -d --no-build --force-recreate "$SERVICE"
wait_for_health "$VERSION"

python3 - <<'PY'
import sqlite3

connection = sqlite3.connect("file:data/db/apilo.sqlite3?mode=ro", uri=True)
result = connection.execute("PRAGMA quick_check").fetchone()[0]
active = connection.execute(
    "SELECT COUNT(*) FROM products WHERE present_in_apilo = 1"
).fetchone()[0]
channels = connection.execute("SELECT COUNT(*) FROM sales_channels").fetchone()[0]
listings = connection.execute("SELECT COUNT(*) FROM channel_listings").fetchone()[0]
connection.close()
if result != "ok":
    raise RuntimeError(f"live quick_check={result}")
if channels < 5 or listings < 1:
    raise RuntimeError(f"incomplete channel snapshot: channels={channels} listings={listings}")
print(
    f"LIVE_DB_OK active_products={active} sales_channels={channels} "
    f"channel_listings={listings}"
)
PY

container_user="$(docker inspect --format '{{.Config.User}}' "$LIVE_CONTAINER")"
readonly_root="$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$LIVE_CONTAINER")"
release_label="$(docker inspect --format '{{index .Config.Labels "com.apilo-stock-panel.app.version"}}' "$LIVE_CONTAINER")"
if [[ "$container_user" != "1000:1000" || "$readonly_root" != "true" || "$release_label" != "$VERSION" ]]; then
  echo "Kontrola hardeningu kontenera nie przeszła." >&2
  rollback 1
fi

running_container="$(docker ps --filter "name=^/${LIVE_CONTAINER}$" --filter "status=running" --format '{{.Names}}')"
[[ "$running_container" == "$LIVE_CONTAINER" ]]
LIVE_CHANGED=0
trap - ERR

{
  echo "DEPLOY=PASS"
  echo "VERSION=$VERSION"
  echo "BACKUP=$BACKUP_PATH"
  echo "ROLLBACK_IMAGE=$ROLLBACK_IMAGE"
  echo "CONTAINER_USER=$container_user"
  echo "READONLY_ROOT=$readonly_root"
  echo "TIME=$(date --iso-8601=seconds)"
} > "$REPORT_PATH"
chown "$OWNER_USER":"$OWNER_USER" "$REPORT_PATH"
chmod 600 "$REPORT_PATH"

echo "APILO_DEPLOY=PASS version=$VERSION rollback_image=$ROLLBACK_IMAGE"
