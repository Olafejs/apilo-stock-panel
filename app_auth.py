import ipaddress
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from flask import redirect, render_template, request, session, url_for

from db import count_recent_login_attempts, get_setting, get_tokens, prune_login_attempts


def is_safe_redirect_target(target):
    if not target:
        return False
    from urllib.parse import urlparse

    if "\\" in target or any(ord(char) < 32 or ord(char) == 127 for char in target):
        return False
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    return parsed.path.startswith("/") and not parsed.path.startswith("//")


def public_error_message(exc, default="Wystąpił błąd."):
    if isinstance(exc, requests.exceptions.Timeout):
        return "Timeout połączenia z API Apilo."
    if isinstance(exc, requests.exceptions.RequestException):
        return "Błąd połączenia z API Apilo."

    message = str(exc).strip()
    if not message:
        return default
    if message.startswith("Apilo API error:") or message.startswith("Token request failed:"):
        return "Błąd komunikacji z API Apilo."
    if isinstance(exc, RuntimeError):
        return message.splitlines()[0][:200]
    return default


def get_client_ip(trust_x_forwarded_for=False):
    forwarded = request.headers.get("X-Forwarded-For", "")
    if trust_x_forwarded_for and forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    return request.remote_addr or "unknown"


def is_local_setup_request(client_ip):
    if client_ip in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}:
        return True
    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    if isinstance(parsed_ip, ipaddress.IPv4Address):
        private_gateway_ranges = (
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        return any(parsed_ip in network for network in private_gateway_ranges) and client_ip.endswith(
            ".1"
        )
    return False


def login_window_start_iso(window_seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()


def is_login_rate_limited(db_path, client_ip, window_seconds, max_attempts):
    if not client_ip or client_ip == "unknown":
        return False
    window_start = login_window_start_iso(window_seconds)
    prune_login_attempts(db_path, window_start)
    attempts = count_recent_login_attempts(db_path, client_ip, window_start)
    return attempts >= max_attempts


def setup_token_required(app_setup_token, is_local_request):
    return bool(app_setup_token) and not is_local_request


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf():
    token = session.get("csrf_token")
    provided = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(token and provided and secrets.compare_digest(token, provided))


def tokens_missing(db_path):
    tokens = get_tokens(db_path) or {}
    return not (tokens.get("access_token") or tokens.get("refresh_token"))


def password_missing(db_path, app_password):
    if app_password:
        return False
    return get_setting(db_path, "password_hash") is None


def render_setup_password(*, require_setup_token, remote_setup_blocked, status_code=200):
    return (
        render_template(
            "setup_password.html",
            require_setup_token=require_setup_token,
            remote_setup_blocked=remote_setup_blocked,
        ),
        status_code,
    )
