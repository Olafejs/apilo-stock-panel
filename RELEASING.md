# Releasing

1. Zaktualizuj `VERSION`, sekcję wersji w `CHANGELOG.md`, Docker `APP_VERSION` i Compose build arg.
2. Uruchom:

   ```bash
   pytest -q
   ruff check .
   python3 -m py_compile *.py scripts/*.py tests/*.py
   bash -n *.sh
   python3 scripts/public_repo_guard.py
   python3 scripts/check_release.py
   ```

3. Zbuduj obraz kandydujący bez zatrzymywania live i uruchom testy w osobnym kontenerze z tymczasową bazą.
4. Przed wdrożeniem wykonaj backup SQLite i zapisz ID/PID/start/health kontenera oraz liczniki bazy.
5. Wdróż tylko po przejściu candidate gate. Zweryfikuj `/healthz`, bieżącą wersję z `VERSION`, login redirect, nginx LAN, SQLite `quick_check` i niezmienione liczniki danych.
6. Utwórz commit oraz odpowiadający mu tag, a potem uruchom `python3 scripts/check_release.py --release`.
