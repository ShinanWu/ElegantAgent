# Changelog

All notable changes are documented here. Versions follow Semantic Versioning.

## [1.1.1] - 2026-08-28

### Fixed

- Prevent the macOS WebView from showing an older bundled interface after an app upgrade.
- Version all frontend resource URLs and disable caching for local interface assets.

## [1.1.0] - 2026-08-24

### Added

- macOS Keychain storage and automatic migration for Cursor API keys.
- Cursor sandboxing by default; discussion Agents use a read-only tool allowlist.
- Regression verification for state synchronization, settings reloads, persistence recovery, WebSocket disconnects, and single-instance startup.
- GitHub Actions CI, release automation, privacy documentation, and a security policy.
- A single `VERSION` source for app, installer, API, and release metadata.

### Fixed

- Recover stale Cursor SDK sessions by rebuilding an Agent and restoring recent local conversation context.
- Prevent empty runs from leaving conversations disconnected or blank.
- Keep streaming trace cards informative before the first thinking/tool event arrives.
- Treat server snapshots as authoritative so reset conversations cannot reappear or remain stuck as running.
- Restart the active Manager after API key, default directory, or default model changes.
- Recover from malformed local JSON and write persistent data atomically.
- Prevent double-instance races and avoid terminating another live instance's bridge.
- Drop late background events after a WebSocket disconnect.
- Restore executable permissions for install and packaging scripts.

### Changed

- Bundle and installer identifier is now `com.shinanwu.yoya`.
- Direct runtime and build dependencies are pinned for reproducible releases.

## [1.0.5] - 2026-08-17

- Renamed the app and data directory to yoya.
- Sent original images to Cursor and persisted run errors in conversations.

[1.1.1]: https://github.com/ShinanWu/ElegantAgent/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ShinanWu/ElegantAgent/compare/v1.0.5...v1.1.0
[1.0.5]: https://github.com/ShinanWu/ElegantAgent/releases/tag/v1.0.5
