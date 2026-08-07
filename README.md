# StatsTalk

StatsTalk lets users run common statistical analyses by describing the task in natural language. It supports a production SPSS backend and a no-SPSS Python backend powered by pandas/pingouin.

## Quick Start

```powershell
python -m pip install uv==0.10.10
uv venv --python 3.12 .venv
uv pip sync --python .venv\Scripts\python.exe --require-hashes requirements.lock
copy .env.example .env
.venv\Scripts\python.exe launcher.py
```

API-only mode:

```powershell
.venv\Scripts\python.exe snla/ui/server.py
```

MCP stdio server:

```powershell
.venv\Scripts\python.exe snla/mcp_server.py
```

## Configuration

Important `.env` keys:

```ini
LLM_ENDPOINT=https://opencode.ai/zen/go/v1/chat/completions
LLM_API_KEY=your-key-here
LLM_MODEL=deepseek-v4-flash
STATS_BACKEND=python        # python | spss
SPSS_PATH=...
SPSS_PYTHON_PATH=...
LLM_MOCK=true               # useful for local demo/testing
```

Public LLM endpoints must use HTTPS with a certificate trusted by the operating
system. Plain HTTP is accepted only for local services on `localhost` or
`127.0.0.1`; certificate verification cannot be disabled.

## Core Flow

```text
User request
  -> planner: intent, method, variables
  -> backend: Python or SPSS
  -> SPSS syntax template and validator
  -> executor and parser
  -> statistical constraint explainer
  -> UI, Word export, or MCP response
```

## Main Capabilities

- Natural-language analysis planning.
- SPSS execution through bundled SPSS Python or batch mode.
- Python backend for 15 methods.
- SPSS syntax templates with blacklist and greylist validation.
- OMS XML parser with LST fallback.
- Privacy filtering before LLM calls.
- SQLite shadow persistence for desktop sessions.
- Word `.docx` report export.
- Flask/PyWebView desktop UI and FastMCP server.

## Supported Methods

- Independent and paired t-tests
- One-way ANOVA
- Pearson and Spearman correlation
- Simple and multiple regression
- Chi-square/crosstabs
- Frequencies and descriptives
- Mann-Whitney U
- Kruskal-Wallis
- Wilcoxon
- Logistic regression placeholder/fallback

## Tests

```powershell
.venv\Scripts\python.exe -m pytest snla/tests/ -v -m "not slow"
.venv\Scripts\python.exe -m pytest snla/tests/test_server.py -v
.venv\Scripts\python.exe -m pytest snla/tests/test_python_backend.py -v
.venv\Scripts\python.exe -m pytest snla/tests/test_export.py -v
.venv\Scripts\python.exe -m pytest scripts/mcp_integration_test.py -v
```

Some SPSS-dependent checks are intended for local Windows machines with SPSS installed.

## Project Map

```text
snla/config.py              environment config and hot reload
snla/session.py             single-user session state
snla/data/                  readers, privacy filtering, persistence
snla/llm/                   LLM client and prompts
snla/syntax/                SPSS templates and syntax validator
snla/executor/              SPSS and Python backends
snla/parser/                OMS XML and LST parsers
snla/explainer/             result explanation, charts, Word export
snla/orchestrator/          planner and greylist state
snla/ui/                    Flask API and single-file frontend
snla/mcp_server.py          MCP tools
scripts/                   verification and demo scripts
docs/                      user and testing documentation
data/fixtures/             sample datasets and checklists
```

## Known Limits

- SPSS automation is Windows-focused.
- The Flask app is single-user; concurrent analyses return 409.
- SQLite persistence is a local shadow store, not multi-instance sync.
- Batch variable expansion such as `Q1-Q10` is currently a preprocessor, not multi-result aggregation.

## Packaging

```powershell
.venv\Scripts\python.exe -m PyInstaller snla.spec --noconfirm
```

Output: `dist/StatsTalk.exe`.

Development, CI, and packaging all use the exact hashes in `requirements.lock`.
See `docs/ci.md` for lock updates, coverage scope, and required checks.
