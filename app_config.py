import os
import secrets

from dotenv import load_dotenv

from app_utils import parse_bool_value, parse_int_value, read_version
from db import get_setting, init_db, set_setting


BASE_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_PATH = os.getenv("APILO_DB_PATH", "apilo.sqlite3")
if not os.path.isabs(DB_PATH):
    DB_PATH = os.path.join(BASE_DIR, DB_PATH)
init_db(DB_PATH)

THUMB_DIR = os.path.join(BASE_DIR, "static", "thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

THUMB_TTL_SECONDS = parse_int_value(os.getenv("THUMB_TTL_SECONDS"), 86400, min_value=0)
THUMB_DOWNLOAD_TIMEOUT_SECONDS = parse_int_value(
    os.getenv("THUMB_DOWNLOAD_TIMEOUT_SECONDS"), 10, min_value=1, max_value=60
)
THUMB_MAX_DOWNLOAD_BYTES = parse_int_value(
    os.getenv("THUMB_MAX_DOWNLOAD_BYTES"), 8_000_000, min_value=65_536, max_value=20_000_000
)
REFRESH_INTERVAL_SECONDS = parse_int_value(
    os.getenv("REFRESH_INTERVAL_SECONDS"), 600, min_value=10
)
SALES_CACHE_REFRESH_INTERVAL_SECONDS = parse_int_value(
    os.getenv("SALES_CACHE_REFRESH_INTERVAL_SECONDS"), 1800, min_value=60
)
SALES_YEAR_REFRESH_INTERVAL_SECONDS = parse_int_value(
    os.getenv("SALES_YEAR_REFRESH_INTERVAL_SECONDS"), 21600, min_value=300
)
SESSION_LIFETIME_MINUTES = parse_int_value(
    os.getenv("SESSION_LIFETIME_MINUTES"), 480, min_value=5, max_value=43200
)
LOGIN_RATE_LIMIT_WINDOW_SECONDS = parse_int_value(
    os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS"), 600, min_value=60, max_value=86400
)
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = parse_int_value(
    os.getenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS"), 5, min_value=1, max_value=100
)
SESSION_COOKIE_SECURE = parse_bool_value(os.getenv("SESSION_COOKIE_SECURE"), default=False)
APP_SETUP_TOKEN = (os.getenv("APP_SETUP_TOKEN") or "").strip()
TRUST_X_FORWARDED_FOR = parse_bool_value(
    os.getenv("TRUST_X_FORWARDED_FOR"), default=False
)
APP_PASSWORD = os.getenv("APP_PASSWORD")
APP_HOST = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT = parse_int_value(os.getenv("APP_PORT"), 5000, min_value=1, max_value=65535)
DEBUG_MODE = os.getenv("FLASK_DEBUG") == "1"
APP_VERSION = read_version(BASE_DIR)
PROJECT_NAME = "Apilo Panel Stanow Magazynowych"
PROJECT_DESCRIPTION = "Panel WWW do pracy na stanach magazynowych z Apilo."


def resolve_flask_secret_key():
    env_key = os.getenv("FLASK_SECRET_KEY")
    if env_key:
        return env_key, "env"
    stored_key = get_setting(DB_PATH, "flask_secret_key")
    if stored_key:
        return stored_key, "db"
    generated_key = secrets.token_urlsafe(64)
    set_setting(DB_PATH, "flask_secret_key", generated_key)
    return generated_key, "generated"


FLASK_SECRET_KEY, FLASK_SECRET_KEY_SOURCE = resolve_flask_secret_key()
