# File Inventory and Dependencies

## Runtime flow

`app.py` skleja aplikację Flask i korzysta z modułów pomocniczych:
- `app_auth.py`
- `app_admin.py`
- `app_alerts.py`
- `app_reporting.py`
- `app_sync.py`
- `app_config.py`
- `app_utils.py`
- `apilo.py`
- `db.py`

## Główne pliki

| File | Purpose |
|---|---|
| `app.py` | entrypoint Flask, routing, renderowanie widoków |
| `app_config.py` | env, ścieżki, wersja, stałe runtime |
| `app_utils.py` | wspólne helpery parsowania i formatowania |
| `app_auth.py` | auth, CSRF, rate limit, helpery bezpieczeństwa |
| `app_admin.py` | audit log i snapshoty ustawień |
| `app_sync.py` | stan syncu, harmonogram, background thread |
| `app_reporting.py` | raport sprzedaży i eksport CSV |
| `app_alerts.py` | alerty niskich stanów |
| `apilo.py` | klient API Apilo i tokeny OAuth |
| `db.py` | SQLite, migracje, cache i settings |
| `templates/` | widoki Jinja |
| `static/` | CSS i cache miniaturek |
| `tests/` | testy pytest |

## Pliki operatorskie

| File | Purpose |
|---|---|
| `README.md` | szybki start i konfiguracja |
| `CONTRIBUTING.md` | zasady zmian w repo |
| `SECURITY.md` | zgłaszanie podatności |
| `.github/ISSUE_TEMPLATE/` | szablony issue dla publicznego repo |
| `.github/release.yml` | kategorie automatycznych release notes na GitHubie |
| `CHANGELOG.md` | historia release |
| `VERSION` | wersja pokazywana w UI |
| `docs/GITHUB_PUBLIC_RELEASE.md` | gotowy opis repo, topics i release notes do GitHub UI |
| `docker-compose.yml` | lokalny i serwerowy deploy Docker |
| `Dockerfile` | obraz produkcyjny |
| `gunicorn.conf.py` | runtime `gunicorn` |
| `apilo-panel.service` | przykładowy unit systemd |

## Ścieżki runtime

- `data/db`
- `data/logs`
- `data/thumbs`

Te dane nie powinny trafiać do repo.
