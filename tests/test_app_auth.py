import requests

from app_auth import (
    get_client_ip,
    is_local_setup_request,
    is_safe_redirect_target,
    public_error_message,
    validate_csrf,
)


def test_is_safe_redirect_target_accepts_only_local_paths():
    assert is_safe_redirect_target("/panel?channel=allegro") is True
    assert is_safe_redirect_target("//evil.example") is False
    assert is_safe_redirect_target("https://evil.example") is False
    assert is_safe_redirect_target("/\\evil.example") is False
    assert is_safe_redirect_target("/\r\nLocation: https://evil.example") is False
    assert is_safe_redirect_target("/\x00bad") is False


def test_public_error_message_maps_known_error_types():
    assert public_error_message(requests.exceptions.Timeout()) == "Timeout połączenia z API Apilo."
    assert public_error_message(requests.exceptions.RequestException()) == "Błąd połączenia z API Apilo."
    assert public_error_message(RuntimeError("Apilo padlo")) == "Apilo padlo"


def test_request_helpers_support_forwarded_ip_and_csrf(app_module):
    with app_module.app.test_request_context(
        "/login",
        method="POST",
        data={"csrf_token": "test-token"},
        headers={"X-Forwarded-For": "198.51.100.5, 10.0.0.1"},
        environ_base={"REMOTE_ADDR": "10.0.0.1"},
    ):
        from flask import session

        session["csrf_token"] = "test-token"

        assert get_client_ip(trust_x_forwarded_for=True) == "198.51.100.5"
        assert validate_csrf() is True


def test_is_local_setup_request_accepts_only_expected_ips():
    assert is_local_setup_request("127.0.0.1") is True
    assert is_local_setup_request("192.168.1.1") is True
    assert is_local_setup_request("192.168.1.55") is False
