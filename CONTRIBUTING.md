# Contributing

Projekt Apilo Stock Panel jest mały, więc zmiany powinny być ograniczone i konkretne.

## Zasady

- nie commituj lokalnych danych, sekretów i cache (`.env`, `data/`, logi, SQLite),
- nie dodawaj konfiguracji konkretnego serwera, konta sprzedawcy ani sklepu,
- trzymaj trasy Flask cienkie, a logikę przenoś do modułów pomocniczych,
- nie zmieniaj kompatybilności ustawień i migracji danych bez uzasadnienia,
- dodawaj testy dla zmian w logice i bezpieczeństwie.

## Setup developerski

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
pytest -q
python app.py
```

## Kontrola przed pushem

```bash
python3 scripts/public_repo_guard.py
ruff check .
python3 -m py_compile *.py scripts/*.py tests/*.py
pytest -q
python3 scripts/check_release.py
```

Zmiany funkcjonalne wymagają aktualizacji `VERSION` i `CHANGELOG.md`. Tag wydania powinien wskazywać commit z odpowiadającą mu wersją.
