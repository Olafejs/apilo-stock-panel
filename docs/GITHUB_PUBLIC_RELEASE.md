# GitHub Project Metadata

## Display name

`Apilo Stock Panel`

## Description

Panel Flask/SQLite do podglądu magazynu i kontroli spójności ofert obsługiwanych przez Apilo.

## Topics

- `apilo`
- `flask`
- `inventory`
- `warehouse`
- `sqlite`
- `marketplace`
- `docker`
- `python`

## Public release checklist

1. Uruchom `python3 scripts/public_repo_guard.py`.
2. Uruchom Gitleaks na pełnej historii.
3. Uruchom `ruff check .` i `pytest -q`.
4. Sprawdź `python3 scripts/check_release.py --release` po utworzeniu tagu.
5. Publikuj wyłącznie z czystego drzewa roboczego.

Nie kopiuj do publicznego repo historii, baz, logów, eksportów ani konfiguracji pochodzącej z działającej instancji.
