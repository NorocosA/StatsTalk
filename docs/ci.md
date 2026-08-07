# Python 3.12 CI and dependency policy

## Reproducible environments

`requirements.in` is the human-maintained abstract dependency declaration.
`requirements.lock` is compiled for Python 3.12 across supported platforms and
pins every package with package-index hashes. Development, CI, and release builds
must install this lock with hash verification:

```powershell
uv venv --python 3.12 .venv
uv pip sync --python .venv\Scripts\python.exe --require-hashes requirements.lock
```

Regenerate the lock only in a dependency-update PR:

```powershell
uv pip compile --python-version 3.12 --universal --generate-hashes requirements.in -o requirements.lock
```

The workflow pins uv 0.10.10 and uses Python 3.12 x64 on both runner families.

## Automated gates

The Ubuntu job runs Python backend, Flask API, seven-tool MCP, loopback and LLM
transport security, statistical decision, airline fixture, lint, and overall
coverage checks. `LLM_MOCK=false` is the baseline; tests opt into mocks locally.
`STATS_BACKEND=python` guarantees CI never starts a real SPSS process.

Overall line coverage covers the CI-executable product surface and must remain at
or above 70%. The following modules are excluded for explicit reasons:

- `snla/executor/spss.py` and `snla/executor/adapter.py`: exercised by the signed
  manual SPSS comparison gate; the adapter is not called by application entry points.
- `snla/rag/`: optional, not integrated into the beta dependency set.
- `snla/explainer/charts.py`: not wired into a beta product workflow.
- `snla/tests/`: test code is not production coverage.

Loopback and LLM transport decisions, plus statistical capability and method
decisions, each have a separate 100% branch-coverage gate. The syntax sandbox
has focused regression cases in the security step without changing its runtime code.

The Windows job builds `StatsTalk.exe` with PyInstaller from the same lock, checks
its size, and executes `StatsTalk.exe --version` as a non-GUI startup sanity probe.

## Master branch protection

The required checks for `master` are recorded in `.github/required-checks.json`:

- `CI / Ubuntu quality gates`
- `CI / Windows package`

Branch protection must require both checks, require the branch to be up to date,
and require pull requests before merging. If workflow or job names change, update
the workflow and the required-check record in the same pull request.

## SPSS boundary

GitHub Actions must not install, detect, license-check, or start IBM SPSS. Release
candidates continue to use the separate signed Windows/SPSS 26 manual validation
defined in the release roadmap.
