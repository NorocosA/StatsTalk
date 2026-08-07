# StatsTalk 0.9 Beta Roadmap

## Status

- Target: public Windows beta (`0.9.0`)
- Distribution: GitHub Pre-release
- Platform: Windows 10/11 x64
- Runtime: Python 3.12 x64
- Schedule: quality-gated, no fixed release date
- Development model: short-lived branches and reviewed PRs into protected `master`

## Product Goal

StatsTalk 0.9 beta is a local-first desktop statistics tool for users who want to run
common analyses without learning command syntax. The release must remain useful without
an API key or SPSS installation, while making every cloud interaction, backend fallback,
and statistical decision visible and auditable.

The release is a beta, not a claim of production, clinical, regulatory, or research
certification.

## Release Scope

### Supported entry points

- PyWebView desktop UI as the primary entry point.
- Token-protected browser fallback.
- Local Flask API used by the desktop and browser clients.
- Experimental MCP server, disabled by default and enabled explicitly by the user.

### Supported data formats

- SPSS `.sav`.
- CSV `.csv` with encoding diagnostics.
- Excel `.xlsx` with explicit worksheet selection.

The default upload limit is 100 MB. Excel imports are limited to 5 million effective
cells. The application stops with a clear error instead of attempting an unsafe load.
Excel macros are not executed, formulas are read as cached values, worksheets are not
merged automatically, and cross-sheet analysis is out of scope.

### Public statistical capabilities

The release exposes 11 independent capabilities. `correlations` is an alias of Pearson
correlation, not a separate method.

1. Descriptives
2. Frequencies
3. Independent-samples t-test
4. Paired-samples t-test
5. One-way ANOVA
6. Pearson correlation
7. Spearman correlation
8. Chi-square/crosstabs
9. Mann-Whitney U
10. Kruskal-Wallis
11. Simple linear regression

Wilcoxon, multiple regression, and logistic regression are hidden from the public UI,
API capability response, and MCP tools for 0.9. Logistic regression remains a placeholder
and must not be advertised as implemented.

## Explicit Non-Goals

- macOS or Linux desktop packages.
- Multi-user or multi-tenant operation.
- Automatic software updates.
- Portable API-key backups.
- Data-changing SPSS operations such as `COMPUTE`, `RECODE`, or `SELECT IF`.
- PDF or Excel report export.
- Automatic worksheet merging or cross-sheet analysis.
- SPSS license inspection, validation, or judgement.
- Code signing for 0.9; signing is a 1.0 release gate.

## Architecture Direction

### Shared analysis service

Desktop, HTTP API, and MCP entry points must use one `AnalysisService`. The service owns:

- analysis planning;
- method capability lookup;
- method and variable applicability validation;
- preferred and effective backend selection;
- execution and parsing;
- local explanation and optional cloud polish;
- audit metadata and structured errors.

Protocol adapters only translate requests and responses. The existing executors,
templates, parsers, and result schema remain reusable implementation components.

### Capability registry

A typed, reviewed registry is the release source of truth. Each capability records:

- canonical method and aliases;
- Python and SPSS support;
- validation status per backend;
- required variable roles and parameters;
- minimum sample or group constraints;
- whether SPSS-to-Python fallback is allowed;
- comparison fields and method-specific tolerances.

Generated comparison reports are evidence. They must never overwrite the production
registry automatically.

## Security Contract

### Local service access

- Bind only to a random loopback port.
- Generate a new launch secret every startup.
- Use a one-time bootstrap token to obtain a session token.
- Remove bootstrap secrets from the address bar immediately.
- Keep session tokens in memory or `sessionStorage`, never durable browser storage.
- Require authentication for settings, upload, analysis, export, model, and MCP controls.
- Restrict CORS to the active local origin; wildcard CORS is forbidden.
- Never return a complete API key from an endpoint.
- Invalidate all tokens when the application exits or restarts.

### LLM transport

- Public endpoints must use HTTPS with normal certificate and hostname verification.
- Plain HTTP is permitted only for `localhost` and `127.0.0.1` local models.
- Certificate-verification bypasses are not configurable and must be removed.
- Arbitrary OpenAI-compatible providers are allowed within these transport rules.

### API-key storage

- Encrypt API keys with Windows DPAPI, bound to the current user.
- Store ciphertext separately at `%APPDATA%\StatsTalk\secure_key.bin`.
- Store only `LLM_API_KEY_STORAGE=dpapi` in ordinary configuration.
- Never silently fall back to plaintext after encryption or decryption failure.
- On DPAPI failure, disable cloud features and ask the user to enter a new key.
- Do not provide portable encrypted-key backup in 0.9.
- Migrate legacy plaintext keys only after explicit consent and transactional encrypt,
  persist, and decrypt verification.

## Privacy Contract

### Cloud boundary

Cloud planning may receive only:

- the user's analysis question;
- desensitized variable names and labels;
- variable types;
- row and column counts.

It must not receive raw rows, value labels, original sensitive names, file paths, or
unapproved statistical output. Sensitive names and labels are replaced locally and
mapped back only after the cloud response is received.

### Local-first explanation

The deterministic local explainer is the default and is sufficient for a complete
analysis. AI polish is a separate, default-off option. Before enabling it, the UI lists
the aggregate fields that would be transmitted. Statistical conclusions must never
depend on AI polish.

### Local data lifecycle

- Uploaded working copies and history are session-only by default.
- Normal exit removes session data; the next startup removes stale crash remnants.
- Optional session restore stores only an encrypted file reference and minimal metadata.
- Restore metadata uses a separate DPAPI purpose and file from API-key storage.
- The original dataset is reopened after user confirmation; StatsTalk does not persist a
  private dataset copy for restore.

## Statistical Decision Policy

- Read-only analyses that pass rule validation may execute immediately.
- Method corrections stop the flow, explain the reason, and require user confirmation.
- The rule engine has authority to reject an LLM recommendation.
- Syntax validity is not treated as evidence that a statistical method is appropriate.
- Public methods must have schema, numeric, significance-decision, missing-data, and
  boundary-sample tests.
- Cross-backend comparison uses method-specific fields and tolerances, not exact floating
  point equality and not a p-value-only global rule.

### Backend fallback

The application distinguishes:

- `preferred_backend`: the user's saved choice;
- `effective_backend`: the backend used for the current analysis;
- `fallback_reason`: a structured explanation when they differ.

If SPSS is preferred but unavailable, StatsTalk may transparently use Python only when
the selected method is validated for Python. The saved preference is not changed. The UI
shows the fallback once per session, every result and export records the effective
backend, and the user can permanently switch to Python. Unsupported methods stop with a
clear explanation. The application never changes the statistical method during fallback.

SPSS detection checks only the necessary configured or conventional executable paths.
StatsTalk does not call license APIs, inspect license identifiers, or make statements
about the legitimacy of an SPSS installation.

## User Experience

- Chinese UI and explanations.
- Statistical terms include their English name on first use.
- Logs, internal error codes, and developer diagnostics remain English.
- Default results show the conclusion, key statistics, method warnings, and fallback
  state.
- Full tables, syntax, backend metadata, and diagnostics appear under an advanced section.
- Warnings that affect interpretation are never hidden in the advanced section.
- Word reports support writing workflows.
- JSON analysis records support reproduction and diagnostics without including secrets,
  raw paths, access tokens, or raw data.

## Crash Reporting

Sentry crash reporting is offered on first run with an unchecked consent box. No event is
sent before consent. Users can withdraw consent, clear queued events, reset the random
installation identifier, and preview the fields in the latest sanitized report.

The client reconstructs events from an allowlist instead of deleting fields from a raw
Sentry event. Allowed data is limited to:

- exception type without an untrusted exception message;
- sanitized stack function names and line numbers;
- application version;
- Windows version;
- effective backend type;
- a random installation UUID.

Breadcrumbs, request bodies, response bodies, locals, environment variables, source
context, performance traces, profiles, usernames, hostnames, IP fields, paths, queries,
variable metadata, syntax, and results are excluded. The UUID is generated only after
consent and deleted when consent is withdrawn, reset, or removed during uninstall.

## Build, CI, and Release Policy

### Reproducible dependencies

- Maintain abstract dependency declarations and an exact, hashed lock file.
- Development, CI, and release builds install from the same lock file.
- Dependency upgrades arrive as isolated, fully tested PRs.
- Python 3.12 x64 is the sole release runtime.

### Automated CI

Every commit and PR runs:

| Layer | Coverage | Environment |
| --- | --- | --- |
| Python backend | Routing, parameters, statistical outputs, failure behavior | Ubuntu |
| API regression | Flask routes and analysis journeys | Ubuntu |
| MCP integration | All seven tools and state transitions | Ubuntu |
| Security | TLS, CORS, tokens, key masking, privacy boundaries | Ubuntu |
| Real fixture regression | Airline fixture analysis does not regress | Ubuntu |
| Package build | PyInstaller build and executable sanity checks | Windows |

The initial line coverage gate is 70%. Security, privacy, capability, method-validation,
and backend-selection decision branches require complete targeted coverage. The two stale
chi-square `xfail` markers must be removed so regressions fail CI.

### SPSS release validation

CI does not launch SPSS. Before each release, a developer runs the following on Windows
10/11 with IBM SPSS Statistics 26 installed:

1. `scripts/compare_backends.py --methods all`
2. `scripts/e2e_backend_smoke.py`
3. Manual review of conclusion conflicts, OMS parsing, and registry changes

Required artifacts:

- `backend_comparison.json`;
- `method_trust.json` as evidence only;
- `e2e_smoke_report.json`;
- signed `RELEASE_SPSS_VALIDATION.md` containing the date, reviewer, OS version, SPSS
  version, matrix summary, OMS result, and release decision.

The smoke script must assert expected methods and produce JSON rather than treating any
successful HTTP response as statistical correctness.

### Distribution

- Inno Setup per-user installer without an administrator requirement.
- Portable ZIP.
- SHA-256 checksums for every artifact.
- WebView2 detection and actionable startup guidance.
- Uninstall prompt for configuration and encrypted session-data cleanup.
- No automatic updater. The app may check the latest version and link to GitHub Releases,
  sending only the app version and operating system.
- `0.9.0` is published as a GitHub Pre-release with beta status and known limits.

## Delivery Slices

| # | Title | Type | Blocked by |
| --- | --- | --- | --- |
| 1 | [Define the 0.9 capability registry](https://github.com/NorocosA/StatsTalk/issues/1) | AFK | None |
| 2 | [Secure loopback access with launch tokens](https://github.com/NorocosA/StatsTalk/issues/2) | AFK | None |
| 3 | [Enforce verified LLM transport](https://github.com/NorocosA/StatsTalk/issues/3) | AFK | None |
| 4 | [Protect secrets with DPAPI](https://github.com/NorocosA/StatsTalk/issues/4) | AFK | None |
| 5 | [Route all analysis through `AnalysisService`](https://github.com/NorocosA/StatsTalk/issues/5) | AFK | 1 |
| 6 | [Validate method applicability before execution](https://github.com/NorocosA/StatsTalk/issues/6) | AFK | 1, 5 |
| 7 | [Deliver the no-Key local analysis journey](https://github.com/NorocosA/StatsTalk/issues/7) | AFK | 5, 6 |
| 8 | [Enforce cloud privacy and opt-in AI polish](https://github.com/NorocosA/StatsTalk/issues/8) | AFK | 5, 7 |
| 9 | [Implement transparent SPSS fallback](https://github.com/NorocosA/StatsTalk/issues/9) | AFK | 1, 5, 6 |
| 10 | [Make data ephemeral with opt-in restore](https://github.com/NorocosA/StatsTalk/issues/10) | AFK | 4, 5 |
| 11 | [Add safe Excel import](https://github.com/NorocosA/StatsTalk/issues/11) | AFK | 5, 10 |
| 12 | [Layer results and add reproducible exports](https://github.com/NorocosA/StatsTalk/issues/12) | AFK | 5, 6, 9 |
| 13 | [Ship MCP as an opt-in experimental surface](https://github.com/NorocosA/StatsTalk/issues/13) | AFK | 2, 5, 7, 8 |
| 14 | [Add consent-based sanitized Sentry reports](https://github.com/NorocosA/StatsTalk/issues/14) | HITL | 2, 4 |
| 15 | [Establish reproducible Python 3.12 CI](https://github.com/NorocosA/StatsTalk/issues/15) | AFK | None |
| 16 | [Qualify the 11 public statistical methods](https://github.com/NorocosA/StatsTalk/issues/16) | HITL | 1, 6, 15 |
| 17 | [Build Windows installer and portable package](https://github.com/NorocosA/StatsTalk/issues/17) | HITL | 2, 4, 10, 11, 13, 14, 15 |
| 18 | [Publish the 0.9 beta pre-release](https://github.com/NorocosA/StatsTalk/issues/18) | HITL | 12, 16, 17 |

## Release Gates

`0.9.0` may be published only when all of the following are true:

- All 18 delivery slices are complete.
- `master` is protected and all required CI checks pass.
- No endpoint exposes plaintext API keys or unauthenticated sensitive actions.
- TLS verification cannot be disabled for public endpoints.
- Production cloud paths pass end-to-end desensitization tests.
- The no-Key local journey completes upload, validation, analysis, explanation, and export.
- Exactly the reviewed 11 capabilities are publicly exposed.
- Python and SPSS validation evidence contains no unresolved conclusion conflict.
- Word and JSON exports record the effective backend and validation warnings.
- Installer and portable package pass clean Windows 10/11 startup, analysis, export, and
  uninstall tests.
- Sentry consent, withdrawal, event preview, and field allowlist are verified.
- Release artifacts, checksums, changelog, known limits, and SPSS validation report are
  attached to the public GitHub Pre-release.
