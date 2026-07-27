#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
FORBIDDEN_DIRS = {"data", "logs", "backups", "secrets", "exports", "uploads"}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".log",
    ".csv",
    ".xlsx",
    ".jsonl",
}
FORBIDDEN_TEXT = {
    "private home path": "/home/" + "olafejs",
    "private host": "192.168." + "1.131",
    "private network": "192.168." + "1.0/24",
    "private brand": "wee" + "ball",
    "private shop domain": "sklep." + "wee" + "ball" + ".pl",
    "private example email domain": "poczta." + "op" + ".pl",
    "private token path": ".allegro" + "-mcp",
    "private credential filename": "allegro" + "_mcp.env",
    "private credential filename (ERLI)": "erli" + "_mcp.env",
}
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b", re.I)
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "example.invalid"}


def files_to_scan():
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
        )
        for raw_relative in result.stdout.split(b"\0"):
            if not raw_relative:
                continue
            relative = Path(raw_relative.decode("utf-8"))
            path = ROOT / relative
            if path.is_file():
                yield path, relative
        return
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_file():
            yield path, relative


def main() -> int:
    errors = []
    for path, relative in files_to_scan():
        lower_parts = {part.lower() for part in relative.parts[:-1]}
        if lower_parts & FORBIDDEN_DIRS:
            errors.append(f"forbidden runtime directory: {relative}")
        name = relative.name.lower()
        if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
            errors.append(f"forbidden environment file: {relative}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden sensitive/generated file: {relative}")
        if any(marker in name for marker in (".before-", ".backup", ".dump")):
            errors.append(f"forbidden backup filename: {relative}")
        if path.stat().st_size > 5_000_000:
            errors.append(f"oversized file requires review: {relative}")
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        if relative == Path("scripts/public_repo_guard.py"):
            continue
        folded = text.casefold()
        for label, value in FORBIDDEN_TEXT.items():
            if value.casefold() in folded:
                errors.append(f"{label}: {relative}")
        if JWT.search(text):
            errors.append(f"JWT-like value: {relative}")
        for match in EMAIL.finditer(text):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                errors.append(f"non-example email address: {relative}")
                break

    if errors:
        for error in sorted(set(errors)):
            print(f"PUBLIC_REPO_GUARD=FAIL reason={error}", file=sys.stderr)
        return 1
    print("PUBLIC_REPO_GUARD=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
