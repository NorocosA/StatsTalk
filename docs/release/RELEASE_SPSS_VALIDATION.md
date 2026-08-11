# StatsTalk SPSS 26 Release Validation

Complete this report on a Windows 10 or Windows 11 x64 machine with IBM SPSS
Statistics 26 before approving a release. Do not add unrelated product or
account information.

## Environment

- Release version:
- Validation date:
- Reviewer:
- Windows version and build:
- SPSS version: 26
- Python version: 3.12.x
- Release commit:

## Commands

```powershell
python scripts/compare_backends.py --methods all
python scripts/e2e_backend_smoke.py --backend both
```

## Comparison Matrix

- `backend_comparison.json` SHA-256:
- `method_trust.json` SHA-256:
- Canonical methods present: 11 / 11
- Cases passed:
- Cases failed:
- Schema failures:
- Numeric tolerance failures:
- Conclusion conflicts:

## Technical Review

- [ ] Every canonical method has at least one successful comparison case.
- [ ] Every declared comparison field is present for both backends.
- [ ] Every numeric difference is within its documented tolerance.
- [ ] Significance conclusions agree at the configured alpha threshold.
- [ ] Missing-data and small-sample cases were reviewed.
- [ ] OMS XML parsing produced the expected method-specific statistics.
- [ ] Capability registry changes are expected and evidence-backed.
- [ ] `e2e_smoke_report.json` contains all five expected methods and no failures.

OMS parsing outcome:

Matrix summary:

Known deviations and disposition:

## Release Decision

- [ ] Approve release
- [ ] Reject release

Reviewer signature:

Decision date:
