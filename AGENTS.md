# AGENTS.md - StatsTalk

> Status: production-oriented desktop/API/MCP app. Core tests are CI-safe without SPSS or a real LLM when mock mode is enabled.

## Architecture

```text
User natural-language request
  -> Planner: intent, method, variables
  -> Backend router
      -> Python backend: pandas/pingouin
      -> SPSS backend: SPSS Python Submit or batch mode
  -> Template syntax
  -> Validator: blacklist and greylist sandbox
  -> Executor
  -> Parser: OMS XML, LST fallback, or Python AnalysisResult
  -> Explainer: statistical constraints and optional LLM polish
  -> UI, Word export, or MCP response
```

## Commands

| Task | Command |
| --- | --- |
| Setup | `uv venv --python 3.12 .venv && uv pip sync --python .venv\Scripts\python.exe --require-hashes requirements.lock` |
| Desktop run | `python launcher.py` |
| API only | `python snla/ui/server.py` |
| MCP server | `python snla/mcp_server.py` |
| CI-safe tests | `python -m pytest snla/tests/ -v -m "not slow"` |
| API tests | `python -m pytest snla/tests/test_server.py -v` |
| Python backend tests | `python -m pytest snla/tests/test_python_backend.py -v` |
| Export tests | `python -m pytest snla/tests/test_export.py -v` |
| Lint/format | `python -m ruff check snla/ && python -m ruff format snla/` |
| Package | `pyinstaller snla.spec --noconfirm` |

## Environment

Copy `.env.example` to `.env`.

```ini
LLM_ENDPOINT=https://opencode.ai/zen/go/v1/chat/completions
LLM_API_KEY=your-key-here
LLM_MODEL=deepseek-v4-flash
STATS_BACKEND=python        # python | spss
SPSS_PATH=...
SPSS_PYTHON_PATH=...
LLM_MOCK=true
```

## Server Notes

- `snla/ui/server.py`: Flask routes and global single-user state.
- `snla/ui/_helpers.py`: executor creation, LLM availability, rate limiting, dataframe loading.
- `snla/ui/_pipeline.py`: analysis pipeline helpers.
- Single-user guard: concurrent `/api/analyze` returns 409.
- Rate limit: 10 `/api/analyze` requests per 60 seconds.
- Upload limit: 500 MB, `.sav` and `.csv` only.
- Dataset working copies and history are session-only by default.
- Optional restore stores only a purpose-separated DPAPI-encrypted original-file reference.
- Config hot reload: `/api/reload-config`.

## API Endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/` | Frontend HTML |
| GET | `/api/status` | Health and session state |
| POST | `/api/upload` | Upload `.sav` or `.csv` |
| POST | `/api/suggest` | Network-free local method suggestion |
| POST | `/api/analyze` | Main analysis pipeline |
| POST | `/api/cancel` | Cancel running analysis |
| POST | `/api/confirm` | Execute pending greylist operation |
| GET | `/api/variables` | Cloud-safe variable list |
| GET/POST | `/api/settings` | Read/update config |
| POST | `/api/open-local-dataset` | Open a path selected by the desktop file dialog |
| POST | `/api/restore-dataset` | Reopen an encrypted reference after consent |
| GET/DELETE | `/api/local-data` | Inspect/clear retained dataset artifacts |
| POST | `/api/api-key-backup/export` | Download a password-protected key backup |
| POST | `/api/api-key-backup/import` | Restore and rebind a key backup to DPAPI |
| POST | `/api/reload-config` | Reload `.env` |
| POST | `/api/models` | List LLM models |
| GET | `/api/detect-spss` | Detect SPSS installations |
| GET | `/api/export` | Download Word report |

## Module Map

| Module | Purpose |
| --- | --- |
| `snla/config.py` | Env config, validation, hot reload |
| `snla/session.py` | Single-user session state |
| `snla/secrets.py` | DPAPI storage and portable password-protected key backups |
| `snla/trust.py` | Trusted method whitelist |
| `snla/data/reader.py` | `.sav`/`.csv` readers |
| `snla/data/sanitizer.py` | Privacy filtering and variable desensitization |
| `snla/data/retention.py` | Ephemeral workspaces and encrypted restore references |
| `snla/data/persistence.py` | Legacy no-op/cleanup compatibility |
| `snla/data/range_expander.py` | `Q1-Q10` style expansion |
| `snla/llm/client.py` | LLM API wrapper and retry logic |
| `snla/llm/prompts/` | Planner/syntax prompt builders |
| `snla/syntax/templates.py` | SPSS syntax templates |
| `snla/syntax/validator.py` | Syntax sandbox |
| `snla/executor/spss.py` | SPSS subprocess manager |
| `snla/executor/python.py` | Python statistical executor |
| `snla/executor/adapter.py` | Unified backend adapter |
| `snla/parser/` | OMS XML, LST, and schema layers |
| `snla/explainer/naturalize.py` | Statistical explanation layer |
| `snla/explainer/export.py` | Word report export |
| `snla/explainer/charts.py` | Chart generation |
| `snla/orchestrator/` | Planner and greylist state machine |
| `snla/rag/` | SPSS documentation retrieval support |
| `snla/mcp_server.py` | FastMCP tools |

## Constraints

1. SPSS automation is Windows-focused.
2. Python backend is the no-SPSS path.
3. The app is single-user, not multi-tenant.
4. Only variable structure should be sent to LLMs; raw data stays local.
5. Greylisted mutating operations must require confirmation and run on a temporary copy.
6. Keep the package name `snla`; StatsTalk is the user-facing brand.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `NorocosA/StatsTalk`.
See `docs/agents/issue-tracker.md`.

### Triage labels

Use the canonical triage labels defined in `docs/agents/triage-labels.md`.

### Domain docs

StatsTalk uses a single domain context rooted at `CONTEXT.md`, with
architectural decisions under `docs/adr/`. See `docs/agents/domain.md`.
