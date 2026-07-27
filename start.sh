#!/bin/bash

set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"
APP_URL="http://127.0.0.1:5000"

find_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL" >/dev/null 2>&1 || true
    return 0
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" >/dev/null 2>&1 || true
    return 0
  fi
  return 1
}

PYTHON_BIN="$(find_python)"
if [ -z "${PYTHON_BIN:-}" ]; then
  echo "Nie znaleziono Pythona 3."
  exit 1
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Wymagany jest Python 3.10 lub nowszy."
  echo "Aktualnie: $("$PYTHON_BIN" --version 2>&1)"
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
fi

.venv/bin/python -m pip install -r requirements.txt
( sleep 2; open_browser ) &
echo "Uruchamiam panel: $APP_URL"
.venv/bin/python app.py
