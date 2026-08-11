# Sentry Privacy Review

This checklist is a release gate. It must be completed against the configured
StatsTalk Sentry project by a human reviewer before publishing a release.

- [ ] Consent was left unchecked and no event arrived before explicit opt-in.
- [ ] A test crash contains only exception type, function names and line numbers,
      application version, Windows version, effective backend, and random install ID.
- [ ] Exception messages, source context, local variables, breadcrumbs, request and
      response data, environment variables, paths, user data, traces, and profiles are absent.
- [ ] The in-app preview matches the allowlisted values visible in Sentry.
- [ ] Clearing queued reports discards the local preview and pending SDK events.
- [ ] Withdrawing consent stops new events and deletes the local installation ID.

Review date:

Reviewer:

Sentry project:

Release version:

Result: PENDING
