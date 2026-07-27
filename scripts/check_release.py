#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text().strip()
EXPECTED_TRACKED = {
    "apilo_mcp.py",
    "description_checks.py",
    "product_attributes.py",
    "scripts/check_channel_descriptions.py",
    "scripts/import_apilo_descriptions.py",
    "scripts/public_repo_guard.py",
    "scripts/sync_allegro_attributes.py",

    "tests/test_apilo_mcp.py",
    "tests/test_check_channel_descriptions.py",
    "tests/test_description_checks.py",
    "tests/test_description_db.py",
    "tests/test_security_hardening.py",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"RELEASE_CHECK=FAIL reason={message}")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    ).stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true")
    args = parser.parse_args()
    require(bool(re.fullmatch(r"\d+\.\d+\.\d+", VERSION)), "version-file")
    changelog = (ROOT / "CHANGELOG.md").read_text()
    require(
        bool(re.search(rf"^## \[{re.escape(VERSION)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE)),
        "changelog",
    )
    dockerfile = (ROOT / "Dockerfile").read_text()
    compose = (ROOT / "docker-compose.yml").read_text()
    require(f"ARG APP_VERSION={VERSION}" in dockerfile, "docker-arg")
    require('LABEL org.opencontainers.image.version="${APP_VERSION}"' in dockerfile, "docker-label")
    require(f'APP_VERSION: "{VERSION}"' in compose, "compose-build-arg")
    require(f'image: "apilo-panel:{VERSION}"' in compose, "compose-release-image")
    require("USER 1000:1000" in dockerfile, "docker-nonroot")
    require("APP_HOST=127.0.0.1" in dockerfile, "docker-loopback-default")
    gunicorn = (ROOT / "gunicorn.conf.py").read_text()
    require("os.getenv('APP_HOST', '127.0.0.1')" in gunicorn, "gunicorn-loopback-default")
    require("workers = 1" in gunicorn, "gunicorn-single-worker-sync-lock")
    require("BACKGROUND_REFRESH_ENABLED" in gunicorn, "gunicorn-canary-background-disable")
    for marker in ('user: "1000:1000"', "read_only: true", "no-new-privileges:true", "cap_drop:", "pids_limit: 128", "mem_limit: 512m", 'cpus: "1.00"', f"com.apilo-stock-panel.hardening.version={VERSION}"):
        require(marker in compose, f"runtime-hardening-{marker}")
    require((ROOT / "scripts/runtime_canary.py").exists(), "runtime-canary")
    require((ROOT / "scripts/deploy_release.sh").exists(), "release-deploy-script")
    require("set -Eeuo pipefail" in (ROOT / "start.sh").read_text(), "start-strict-mode")
    app = (ROOT / "app.py").read_text()
    require("tempfile.TemporaryDirectory" in app, "thumbnail-private-lifecycle")
    require("os.remove(" not in app, "thumbnail-manual-delete")
    sync = (ROOT / "scripts/sync_allegro_attributes.py").read_text()
    require("urlopen_json_get_with_retry" in sync, "get-retry")
    require('request.get_method() != "GET"' in sync, "retry-method-guard")
    require("GET_MAX_ATTEMPTS = 3" in sync, "retry-bound")
    tracked = set(git("ls-files").splitlines())
    require(EXPECTED_TRACKED <= tracked, "expected-files-untracked")
    if args.release:
        require(not git("status", "--porcelain"), "dirty-worktree")
        require(git("describe", "--tags", "--exact-match", "HEAD") == f"v{VERSION}", "tag")
    print(f"RELEASE_CHECK=PASS version={VERSION} release={str(args.release).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
