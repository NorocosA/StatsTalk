# Evaluation And Testing Guide

This guide describes how to verify StatsTalk across unit tests, API tests, backend checks, manual UI testing, and MCP integration.

## Test Levels

| Level | Command | Notes |
| --- | --- | --- |
| CI-safe tests | `python -m pytest snla/tests/ -v -m "not slow"` | No SPSS or live LLM required |
| API tests | `python -m pytest snla/tests/test_server.py -v` | Flask test client with mocks |
| Python backend | `python -m pytest snla/tests/test_python_backend.py -v` | pandas/pingouin result shape |
| Parser tests | `python -m pytest snla/tests/test_parser.py -v` | OMS XML and LST parsing |
| Export tests | `python -m pytest snla/tests/test_export.py -v` | Word report generation |
| MCP integration | `python scripts/mcp_integration_test.py` | FastMCP tool contract |

## Mock Mode

Use mock mode for local development without an API key:

```ini
LLM_MOCK=true
STATS_BACKEND=python
```

Then run:

```powershell
python launcher.py
```

## Real LLM Verification

Use this only when a valid API key is configured:

```ini
LLM_MOCK=false
LLM_API_KEY_STORAGE=dpapi
STATS_BACKEND=python
```

Enter the API key through the StatsTalk settings panel before running this
check. Do not place plaintext API keys in `.env`.

Then run:

```powershell
python scripts/verify_combined.py
```

## SPSS Verification

SPSS checks require Windows and an installed IBM SPSS Statistics distribution:

```ini
STATS_BACKEND=spss
SPSS_PATH=C:\Program Files\IBM\SPSS\Statistics\29\stats.exe
SPSS_PYTHON_PATH=C:\Program Files\IBM\SPSS\Statistics\29\Python3\python.exe
```

Useful scripts:

```powershell
python scripts/verify_spss.py
python scripts/e2e_backend_smoke.py
```

## Acceptance Criteria

- Upload accepts `.sav` and `.csv`, rejects unsupported extensions and oversize files.
- `/api/analyze` handles empty input, missing data, success, cancellation, and concurrent requests.
- Python backend returns `AnalysisResult` objects for supported methods.
- SPSS backend writes OMS XML or falls back to LST parsing when available.
- Export produces a non-empty `.docx`.
- MCP exposes status, upload, variables, analyze, confirm, cancel, and export tools.
- Privacy filtering strips raw data and `value_labels` before LLM calls.

## Known Manual Checks

- Load `data/fixtures/test_data.sav`.
- Ask: `比较男女成绩差异`.
- Ask: `显示成绩的描述性统计`.
- Export Word report and open it.
- Trigger a greylist operation and confirm that it runs on a temporary copy.
