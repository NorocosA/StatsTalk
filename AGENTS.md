# AGENTS.md — SPSS Natural Language Assistant (SNLA)

> Status: P4 Testing & Release. 56 tests passing. `Plan.md` has full specs.

## Architecture (actual execution flow)

```
User → LLM (Intent + Method only) → Template Syntax → Validator → SPSS → Parser → Explainer → User
```

**Key deviation from Plan.md**: Syntax generation uses **pre-built templates** (`snla/syntax/templates.py`), not LLM.
LLM is only used for intent recognition and method recommendation. This was a deliberate P4 simplification
for reliability — template syntax can't hallucinate variable names.

## Commands

| Task | Command |
|------|---------|
| Setup | `python -m venv venv` then `pip install -r requirements.txt` |
| Dev run | `python launcher.py` (starts Flask + opens PyWebView window) |
| API only | `python snla/ui/server.py` (Flask on port 8501, no window) |
| Tests | `python -m pytest snla/tests/ -v` (56 tests, no SPSS/LLM needed) |
| Tests (CI-safe) | `python -m pytest snla/tests/ -v -m "not slow"` |
| Single test file | `python -m pytest snla/tests/test_validator.py -v` |
| Mock mode | Set `LLM_MOCK=true` in `.env` — no API key needed, returns canned responses |
| Package | `pyinstaller snla.spec --noconfirm` → `dist/SNLA.exe` |
| Clean P0 outputs | Delete `p0_output/` directory between runs |

## Environment

Copy `.env.example` → `.env`. Critical vars:

```
SPSS_PATH=           # stats.exe path (batch mode only)
SPSS_PYTHON_PATH=    # SPSS bundled Python3/python.exe (required for python mode)
SPSS_EXEC_MODE=python  # "python" (default, SPSS 26+) or "batch" (legacy)
LLM_MOCK=true        # Dev without API key
LLM_ENDPOINT=https://opencode.ai/zen/go/v1/chat/completions
```

**`LLM_MOCK=true`** returns deterministic canned JSON for all LLM calls. Essential for UI dev and tests.

## SPSS Execution Modes

| Mode | How | When to use |
|------|-----|-------------|
| **python** (default) | SPSS bundled Python → `spss.Submit()` | SPSS 26+. Most reliable. |
| **batch** (legacy) | `stats.exe -production silent` subprocess | SPSS <26 or when python mode fails |

**Batch mode hangs on SPSS 26+** — that's why python mode exists. If you get "SPSS execution failed" 
with batch mode, switch to python mode.

## Test Strategy

- **56 tests, all pass without real SPSS or LLM** — uses mock XML strings and mock LLM responses
- `conftest.py` provides shared fixtures: `sample_variables`, `mock_spss_output_ttest`, `analysis_result_ttest`, etc.
- Marker `@pytest.mark.slow` on tests requiring real SPSS — **always deselect in CI**: `-m "not slow"`
- Tests that need real SPSS: `scripts/verify_spss.py`, `scripts/verify_spss_v2.py` (manual only)

## Server (`snla/ui/server.py`) — critical design notes

**Single-user design** with global state guards:
```python
_executing: bool            # True while /api/analyze is running (blocks concurrent)
_active_executor            # SPSSExecutor handle for cancellation
_pending_greylist           # Greylist syntax awaiting user confirmation
_was_cancelled              # Set by /api/cancel
```

**API endpoints**: `/api/upload`, `/api/analyze`, `/api/cancel`, `/api/status`, `/api/export`, `/api/settings`

**Greylist flow**: COMPUTE/RECODE/SELECT IF → returns `requires_confirmation: true` → frontend shows dialog → re-send with `confirm_greylist: true`. Executed on **temporary data copy** (original file never touched).

## Constraints

1. **Windows-only** — SPSS automation requires SPSS bundled Python (`SPSS_PYTHON_PATH`)
2. **Syntax sandbox** — `snla/syntax/validator.py` blocks: SAVE, DELETE, ERASE, DATASET CLOSE, NEW FILE, BEGIN PROGRAM, AGGREGATE, ADD/MATCH FILES
3. **Privacy** — only variable names, types, labels, and aggregate stats go to cloud LLM. `snla/data/sanitizer.py` strips raw data values and sensitive variable names.
4. **PyWebView** requires Edge WebView2 runtime (bundled with Windows 10+). Falls back to browser.
5. **TLS workaround** — `snla/llm/client.py` uses a permissive TLS adapter (`CERT_NONE`, `SECLEVEL=1`) for the opencode.ai endpoint. Needed on some Windows/Python combos.

## Module map

| Module | Purpose |
|--------|---------|
| `snla/config.py` | All env-var config, `validate()` for startup checks |
| `snla/session.py` | In-memory `SessionState` dataclass (no DB) |
| `snla/data/reader.py` | `.sav` → pyreadstat, `.csv` → pandas |
| `snla/data/sanitizer.py` | Cloud-safe field filtering + sensitive var detection |
| `snla/llm/client.py` | OpenAI-compatible API wrapper with TLS adapter |
| `snla/llm/prompts/` | intent.py, method.py — prompt templates (LLM used here only) |
| `snla/syntax/validator.py` | Blacklist, greylist, variable existence, bracket pairing |
| `snla/syntax/templates.py` | Pre-built SPSS syntax for ~10 analysis types |
| `snla/executor/spss.py` | SPSS subprocess manager, OMS XML wrapper, temp copies for greylist |
| `snla/parser/output.py` | OMS XML + regex dual parser (OMS primary, regex fallback) |
| `snla/parser/schema.py` | `AnalysisResult`, `TableResult` dataclasses |
| `snla/explainer/naturalize.py` | Constraint layer (p-value rules) + LLM polish (rules first, LLM decorates) |
| `snla/explainer/export.py` | Word .docx export via python-docx |
| `snla/ui/server.py` | Flask REST API (856 lines — the orchestration hub) |
| `snla/ui/index.html` | Single-file frontend (561 lines HTML/CSS/JS) |
| `launcher.py` | Entry point: starts Flask thread → opens PyWebView window |

## File organization

```
snla/tests/          # 56 pytest tests (no SPSS needed)
scripts/             # Manual verification & demo scripts (need SPSS)
data/fixtures/       # test_data.sav, expected outputs, malicious_syntax.sps
docs/                # user_guide.md
.sisyphus/           # OpenCode plans (gitignored)
```

## Current status

- [x] P0–P3 complete
- [x] P4: Frontend migrated Streamlit → Flask + PyWebView
- [x] P4: Server rewrite — greylist flow, cancellation, settings persistence, template-based syntax
- [x] P4: PyInstaller packaging → `dist/SNLA.exe` (77.5 MB)
- [x] P4: E2E verification — 3/3 real SPSS pipelines pass (T-Test, Descriptives, Correlations)
- [x] P4: 50-case test checklist verification — 50/50 valid syntax, 28/50 method match
