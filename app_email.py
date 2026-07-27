import re
from email.utils import parseaddr


class SmtpValidationError(ValueError):
    pass


def _validate_email(value, label):
    if not value:
        raise SmtpValidationError(f"Brak pola: {label}.")
    if "\r" in value or "\n" in value:
        raise SmtpValidationError(f"Nieprawidłowy adres email w polu: {label}.")
    for item in value.split(","):
        candidate = item.strip()
        _display_name, address = parseaddr(candidate)
        if not address or "@" not in address:
            raise SmtpValidationError(f"Nieprawidłowy adres email w polu: {label}.")
        local, domain = address.rsplit("@", 1)
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise SmtpValidationError(f"Nieprawidłowy adres email w polu: {label}.")


def normalize_smtp_settings(values, *, allow_empty=True):
    normalized = {
        "smtp_host": str(values.get("smtp_host") or "").strip(),
        "smtp_port": str(values.get("smtp_port") or "").strip(),
        "smtp_user": str(values.get("smtp_user") or "").strip(),
        "smtp_use_tls": "1" if str(values.get("smtp_use_tls") or "0") == "1" else "0",
        "smtp_use_ssl": "1" if str(values.get("smtp_use_ssl") or "0") == "1" else "0",
        "smtp_from": str(values.get("smtp_from") or "").strip(),
        "smtp_to": str(values.get("smtp_to") or "").strip(),
    }
    has_any_value = any(
        normalized[key]
        for key in ("smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_to")
    )
    if not has_any_value and allow_empty:
        normalized["smtp_use_tls"] = "0"
        normalized["smtp_use_ssl"] = "0"
        return normalized

    host = normalized["smtp_host"]
    if not host:
        raise SmtpValidationError("Brak hosta SMTP.")
    if (
        len(host) > 253
        or "://" in host
        or "/" in host
        or "\\" in host
        or not re.fullmatch(r"[A-Za-z0-9.:-]+", host)
    ):
        raise SmtpValidationError("Nieprawidłowy host SMTP.")

    try:
        port = int(normalized["smtp_port"])
    except ValueError as exc:
        raise SmtpValidationError("Nieprawidłowy port SMTP.") from exc
    if not 1 <= port <= 65535:
        raise SmtpValidationError("Port SMTP musi mieścić się w zakresie 1–65535.")
    normalized["smtp_port"] = str(port)

    if normalized["smtp_use_tls"] == "1" and normalized["smtp_use_ssl"] == "1":
        raise SmtpValidationError("Wybierz STARTTLS albo SSL, nie oba tryby jednocześnie.")

    sender = normalized["smtp_from"] or normalized["smtp_user"]
    recipient = normalized["smtp_to"] or normalized["smtp_user"]
    _validate_email(sender, "Nadawca")
    _validate_email(recipient, "Odbiorca")
    return normalized
