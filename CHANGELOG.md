# Changelog

Wszystkie istotne zmiany publicznej wersji projektu są dokumentowane w tym pliku.

## [Unreleased]

### Changed

- Zaktualizowano i przypięto akcje CI korzystające z runtime Node.js 24.

## [1.14.1] - 2026-07-27

### Added

- Pierwszy publiczny snapshot aplikacji.
- Neutralna konfiguracja uruchomieniowa bez danych konkretnego serwera.
- Automatyczna kontrola prywatnych odwołań i sekretów przed publikacją.

### Security

- Usunięto dane runtime, przykładowe tokeny, identyfikatory API, prywatne adresy i ścieżki hosta.
- Bezpośrednie dane uwierzytelniające pozostają wyłącznie w lokalnym środowisku użytkownika.
