#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
LIVE = "apilo-panel"
CANARY = "apilo-panel-hardening-canary"
IMAGE = f"apilo-panel:{VERSION}-candidate"
PORT = "15080"


def inspect(name: str) -> dict:
    return json.loads(subprocess.check_output(["docker", "inspect", name], text=True, stderr=subprocess.DEVNULL, timeout=30))[0]


def health_with_retry() -> dict:
    """Bounded read-only retry with one-second backoff."""
    for attempt in range(45):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=4) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, json.JSONDecodeError):
            if attempt == 44:
                raise
            time.sleep(1)
    raise RuntimeError("canary unavailable")


def main() -> int:
    try:
        inspect(CANARY)
    except subprocess.CalledProcessError:
        pass
    else:
        raise RuntimeError("stale canary exists")
    live = inspect(LIVE)
    overrides = {
        "APP_HOST": "127.0.0.1",
        "APP_PORT": PORT,
        "APILO_DB_PATH": "/app/data/apilo.sqlite3",
        "BACKGROUND_REFRESH_ENABLED": "0",
    }
    environment = []
    seen = set()
    for item in live["Config"].get("Env") or []:
        key, value = item.split("=", 1)
        environment.append(f"{key}={overrides.get(key, value)}")
        seen.add(key)
    for key, value in overrides.items():
        if key not in seen:
            environment.append(f"{key}={value}")
    started = False
    with tempfile.TemporaryDirectory(prefix="apilo-panel-canary-", dir="/var/tmp") as temp_dir:
        root = Path(temp_dir)
        dirs = {name: root / name for name in ("db", "logs", "thumbs")}
        for path in dirs.values():
            path.mkdir(mode=0o700)
            os.chown(path, 1000, 1000)
        env_file = root / "container.env"
        env_file.write_text("\n".join(environment) + "\n")
        os.chmod(env_file, 0o600)
        command = [
            "docker", "run", "-d", "--name", CANARY, "--network", "host",
            "--user", "1000:1000", "--read-only", "--security-opt", "no-new-privileges:true",
            "--cap-drop", "ALL", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--memory", "512m", "--cpus", "1.00", "--pids-limit", "128", "--env-file", str(env_file),
            "--mount", f"type=bind,src={dirs['db']},dst=/app/data",
            "--mount", f"type=bind,src={dirs['logs']},dst=/app/logs",
            "--mount", f"type=bind,src={dirs['thumbs']},dst=/app/static/thumbs",
            IMAGE,
        ]
        try:
            subprocess.check_output(command, text=True, timeout=60)
            started = True
            health = health_with_retry()
            if health.get("status") != "ok" or health.get("version") != VERSION:
                raise RuntimeError("health/version mismatch")
            candidate = inspect(CANARY)
            host, config = candidate["HostConfig"], candidate["Config"]
            if config.get("User") != "1000:1000" or not host.get("ReadonlyRootfs"):
                raise RuntimeError("user/read-only mismatch")
            if not any("no-new-privileges" in item for item in (host.get("SecurityOpt") or [])):
                raise RuntimeError("no-new-privileges missing")
            if host.get("CapDrop") != ["ALL"] or host.get("Memory") != 536870912 or host.get("NanoCpus") != 1000000000 or host.get("PidsLimit") != 128:
                raise RuntimeError("runtime limits mismatch")
            with sqlite3.connect(f"file:{dirs['db'] / 'apilo.sqlite3'}?mode=ro&immutable=1", uri=True) as connection:
                if connection.execute("pragma quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite check failed")
            print("APILO_RUNTIME_CANARY=PASS uid=1000 read_only=true nnp=true cap_drop=ALL binds=3 data_isolated=true secrets_printed=false")
        finally:
            if started:
                subprocess.run(["docker", "rm", "-f", CANARY], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
