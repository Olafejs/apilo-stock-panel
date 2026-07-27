# Versioning Workflow

Projekt używa trzech warstw wersjonowania:

1. `git` dla dokładnego diffu,
2. `VERSION` dla wersji pokazywanej w aplikacji,
3. `CHANGELOG.md` dla opisu zmian per release.

## Standardowy flow

1. Zrób małą zmianę.
2. Zweryfikuj ją (`pytest`, smoke, ewentualnie Docker).
3. Jeśli zmiana jest user-facing albo operatorska, dopisz wpis do `CHANGELOG.md`.
4. Przy release zaktualizuj `VERSION`.
5. Zrób commit i tag zgodny z wersją.

## Release

Przykład:

```bash
git add VERSION CHANGELOG.md
git commit -m "chore: release 1.0.36"
git tag -a v1.0.36 -m "v1.0.36"
git push origin main
git push origin refs/tags/v1.0.36
```

Zasada:
- `VERSION` == wersja w UI,
- sekcja w `CHANGELOG.md` == opis release,
- tag Git == dokładny stan kodu dla tej wersji.
