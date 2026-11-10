# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.13] - 2026-11-09

### Added

- Public HACS repository layout (`hacs.json`, documentation, issue templates, CI)
- Lovelace `custom:claber-card` shipped inside the integration and auto-registered as a frontend module (version cache-bust)
- Card i18n: English default, Italian when Home Assistant language is `it`

### Changed

- Manifest documentation / issue tracker / codeowners point at this repository
- Service and config-flow copy clarified for public release

### Fixed

- Monotonic `irrigation_ends_at` (same remaining minutes no longer jumps forward)

## [0.1.12] - 2026-11

### Added

- Cmd 0C "start program A/B now" buttons

## [0.1.11] - 2026-11

### Changed

- Normalize MAC `unique_id` on setup; discovery no longer re-prompts configured devices

## [0.1.10] - 2026-11

### Added

- Programs A/B entities, Leggi programmi, Auto switch
- Rain `last_seen`
- Entity registry migration for hyphen unique IDs

## [0.1.9] - 2026-10

### Changed

- Rain uses the same cache reconcile as Hydro
- Hydro lines are `valve` with device class water; remaining time is a timestamp

[0.1.13]: https://github.com/bordeo/ha-claber-bt/releases/tag/v0.1.13
