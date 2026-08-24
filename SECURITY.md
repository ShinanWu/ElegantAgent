# Security Policy

## Supported versions

Only the latest GitHub Release receives security fixes.

## Reporting a vulnerability

Please do not open a public issue for an unpatched vulnerability. Use GitHub's **Report a vulnerability** private reporting flow for this repository:

`Security` → `Advisories` → `Report a vulnerability`

Include affected versions, reproduction steps, impact, and any proposed mitigation. You should receive an initial response within 7 days. Please allow time for a coordinated fix before public disclosure.

## Security model

- Cursor API keys are stored in the macOS Keychain when available.
- Local agents run with Cursor sandboxing enabled by default.
- Discussion agents receive a read-only built-in tool allowlist.
- The local HTTP server binds to `127.0.0.1` only.
- Release packages should be verified against the SHA-256 digest published with each release.

Users should still back up working directories and review high-impact Agent instructions. yoya is a local automation client and cannot guarantee that generated commands are safe or correct.
