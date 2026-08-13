# Changelog

All notable changes to StatsTalk are documented here. The project follows Semantic
Versioning; beta builds may still change user-facing workflows before 1.0.

## [0.9.0-beta] - Unreleased

### Added

- Eleven reviewed public statistical capabilities with local applicability validation.
- Complete no-API-key local analysis, deterministic explanation, and Word/JSON exports.
- Optional SPSS execution with transparent, recorded Python fallback where qualified.
- Safe `.sav`, `.csv`, and explicit single-worksheet `.xlsx` import.
- Per-user DPAPI API-key storage and password-protected portable `.stkb` backups.
- Session-only dataset workspaces with opt-in encrypted original-file restore references.
- Consent-based, allowlist-only Sentry crash reporting with preview and withdrawal.
- Experimental opt-in MCP tools that reuse the desktop security and privacy boundaries.
- Per-user Windows installer, portable ZIP policy, release manifest, and SHA-256 checksums.

### Privacy

- Raw datasets remain local. Cloud planning receives only allowlisted, desensitized
  variable structure; optional AI polish receives aggregate result fields only.
- Crash reporting and AI features are disabled until the user explicitly opts in.
- The portable package stores application state beside the executable in `Data`.

### Migration Notes

- Legacy plaintext API keys are not silently loaded. StatsTalk asks for explicit DPAPI
  migration consent or a new key.
- DPAPI ciphertext cannot be moved between Windows accounts. Export a password-protected
  `.stkb` backup before reinstalling Windows, changing accounts, or moving portable data.
- Existing legacy session shadow files are removed; dataset history is session-only.

### Known Limits

- Desktop distribution and SPSS automation support Windows 10/11 x64 only.
- Real SPSS 26 parity evidence and clean Windows 10/11 distribution signoff are release gates.
- Code signing is deferred and explicitly required for the 1.0 release.
- The application is single-user and does not automatically download or install updates.
- MCP remains experimental and disabled by default.
