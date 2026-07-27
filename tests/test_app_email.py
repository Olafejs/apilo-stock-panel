import pytest

from app_email import SmtpValidationError, normalize_smtp_settings


def valid_settings(**changes):
    values = {
        "smtp_host": "smtp.example.com",
        "smtp_port": "587",
        "smtp_user": "panel@example.com",
        "smtp_use_tls": "1",
        "smtp_use_ssl": "0",
        "smtp_from": "panel@example.com",
        "smtp_to": "alerts@example.com",
    }
    values.update(changes)
    return values


def test_smtp_settings_are_normalized():
    result = normalize_smtp_settings(valid_settings(smtp_port=" 0587 "))

    assert result["smtp_host"] == "smtp.example.com"
    assert result["smtp_port"] == "587"
    assert result["smtp_use_tls"] == "1"


def test_smtp_rejects_conflicting_transport_modes():
    with pytest.raises(SmtpValidationError, match="nie oba"):
        normalize_smtp_settings(valid_settings(smtp_use_ssl="1"))


def test_smtp_rejects_invalid_port_and_header_injection():
    with pytest.raises(SmtpValidationError, match="zakres"):
        normalize_smtp_settings(valid_settings(smtp_port="70000"))

    with pytest.raises(SmtpValidationError, match="Nieprawidłowy adres"):
        normalize_smtp_settings(
            valid_settings(smtp_to="alerts@example.com\nBcc: attacker@example.com")
        )


def test_smtp_allows_fully_empty_disabled_configuration():
    result = normalize_smtp_settings({}, allow_empty=True)

    assert result == {
        "smtp_host": "",
        "smtp_port": "",
        "smtp_user": "",
        "smtp_use_tls": "0",
        "smtp_use_ssl": "0",
        "smtp_from": "",
        "smtp_to": "",
    }
