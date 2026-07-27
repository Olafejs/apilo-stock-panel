# Apilo Stock Panel

[![CI](https://github.com/Olafejs/apilo-stock-panel/actions/workflows/ci.yml/badge.svg)](https://github.com/Olafejs/apilo-stock-panel/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/tag/Olafejs/apilo-stock-panel?label=release)](https://github.com/Olafejs/apilo-stock-panel/tags)
[![License](https://img.shields.io/github/license/Olafejs/apilo-stock-panel)](https://github.com/Olafejs/apilo-stock-panel/blob/main/LICENSE)

Lekki panel Flask/SQLite do podglądu stanów magazynowych i kontroli spójności ofert obsługiwanych przez Apilo.

## Funkcje

- synchronizacja produktów i stanów z Apilo,
- filtrowanie produktów oraz alerty niskich stanów,
- raport sprzedaży z eksportem CSV,
- macierz obecności ofert w PrestaShop, Allegro, ERLI, Empik i Etsy,
- porównywanie opisów kanałów z referencją Apilo,
- odczyt ofert i raportów błędów EmpikPlace,
- historia zmian i audyt operacji,
- szyfrowanie sekretów zapisanych w SQLite.

## Granica odpowiedzialności

Apilo pozostaje właścicielem zapisów cen, stanów i treści ofert. Bezpośrednie integracje marketplace są domyślnie używane do odczytu i diagnostyki. Nie konfiguruj dwóch niezależnych systemów zapisujących to samo pole.

Repozytorium nie zawiera danych runtime, kluczy API, tokenów, baz SQLite, logów, miniaturek ani konfiguracji konkretnego serwera.

## Szybki start

```bash
cp .env.example .env
docker compose up -d --build
```

Panel nasłuchuje domyślnie tylko na:

```text
http://127.0.0.1:5080
```

Jeżeli potrzebujesz dostępu z LAN lub internetu, użyj własnego reverse proxy z uwierzytelnieniem, ograniczeniem źródeł i TLS. Nie wystawiaj bezpośrednio procesu Gunicorn.

## Pierwsze uruchomienie

1. Ustaw `FLASK_SECRET_KEY` i opcjonalnie `APP_PASSWORD` w lokalnym `.env`.
2. Otwórz panel i przejdź do `Ustawienia`.
3. Wprowadź adres API Apilo, Client ID, Client Secret i kod autoryzacji.
4. Dla EmpikPlace zapisz klucz API w ustawieniach integracji.

Jeżeli `SETTINGS_ENCRYPTION_KEY` nie jest ustawiony, aplikacja utworzy lokalny plik `settings.key`. Przy backupie zachowaj razem bazę i odpowiadający jej klucz.

## PrestaShop

Ustaw publiczny adres sklepu bez końcowego ukośnika:

```ini
PRESTASHOP_PUBLIC_BASE_URL=https://shop.example.com
```

Host jest używany do budowania linków produktowych i ograniczenia kolektora opisów do skonfigurowanej domeny.

## Uruchomienie lokalne

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
python app.py
```

## Dane runtime

Lokalne dane są zapisywane poza Gitem:

- `data/db`,
- `data/logs`,
- `data/thumbs`,
- `settings.key`,
- `.env`.

## Testy i kontrola publikacji

```bash
ruff check .
pytest -q
python3 scripts/public_repo_guard.py
python3 scripts/check_release.py
```

CI dodatkowo uruchamia Gitleaks na każdym pushu i pull requeście.

## Bezpieczeństwo

Nie wysyłaj sekretów w issue ani pull requestach. Podatności zgłaszaj przez prywatny GitHub Security Advisory zgodnie z [SECURITY.md](SECURITY.md).

## Licencja

MIT. Projekt nie jest oficjalnym produktem ani rozszerzeniem firmy Apilo.
