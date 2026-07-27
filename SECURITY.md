# Security Policy

## Supported versions

Wspierana jest tylko najnowsza wersja z `main` i najnowszy tag release.

## Reporting a vulnerability

Nie publikuj od razu szczegółów podatności w publicznym issue.

Najbezpieczniej:
- zgłoś problem prywatnie właścicielowi repo,
- albo użyj prywatnego GitHub Security Advisory, jeśli jest włączone.

W zgłoszeniu podaj:
- wersję aplikacji,
- sposób odtworzenia,
- wpływ,
- czy problem dotyczy danych, sekretów, logowania, API Apilo albo SMTP.

## Notes

Projekt przechowuje wrażliwe dane konfiguracyjne i tokeny API. Po każdym incydencie bezpieczeństwa załóż:
- rotację hasła panelu,
- rotację `FLASK_SECRET_KEY`,
- rotację `SETTINGS_ENCRYPTION_KEY` lub wymianę `settings.key`,
- rotację sekretów Apilo i SMTP.
