# SNLA — SPSS Natural Language Assistant

> 用说话的方式完成统计分析。零代码、零 SPSS 操作。

## MCP Configuration

```json
{
  "mcpServers": {
    "snla": {
      "command": "python",
      "args": ["snla/mcp_server.py", "--transport", "stdio"],
      "env": {
        "SPSS_PATH": "${SPSS_PATH}",
        "SPSS_PYTHON_PATH": "${SPSS_PYTHON_PATH}",
        "SPSS_EXEC_MODE": "python",
        "LLM_ENDPOINT": "${LLM_ENDPOINT}",
        "LLM_API_KEY": "${LLM_API_KEY}",
        "LLM_MOCK": "false",
        "STATS_BACKEND": "spss"
      }
    }
  }
}
```

For OpenClaw:
```bash
openclaw mcp set snla --command python --args "snla/mcp_server.py" --env SPSS_PATH="..." --env LLM_API_KEY="..."
```

## Available Tools

| Tool | Purpose | Key Args |
|------|---------|----------|
| `snla_status` | Server health, trusted methods, SPSS availability | — |
| `snla_upload` | Upload data file (.sav/.csv) | `file_path` (server path) |
| `snla_variables` | List variable metadata | — |
| `snla_analyze` | Plan + execute statistical analysis | `query` (natural language) |
| `snla_confirm` | Confirm pending greylist operation | — |
| `snla_cancel` | Cancel running analysis | — |
| `snla_export` | Export last result as .docx | — |

## Workflow

### 1. Standard Analysis (no data modification)

```
User: "比较男女成绩差异"
  → LLM checks snla_status() for available methods
  → LLM calls snla_analyze(query="比较男女成绩差异")
  → Returns {ok, method, explanation, markdown, result}
  → LLM presents markdown table + explanation to user
```

### 2. Greylist Confirmation (COMPUTE / RECODE / SELECT IF)

```
User: "将成绩标准化为 Z 分数，然后比较男女差异"
  → snla_analyze(query) → {ok: false, requires_confirmation: true, message: "..."}
  → LLM displays the confirmation message to user
  → User: "确认"
  → LLM calls snla_confirm()
  → Returns analysis results (same format as standard)
```

### 3. Export

```
User: "导出报告"
  → LLM calls snla_export()
  → Returns {ok, content_base64, filename}
```

## Constraints (MUST FOLLOW)

1. **Call snla_status() first** — before routing any analysis request, check which methods are trusted and whether SPSS is available. Never assume a method is available.

2. **Transparent errors** — when a tool returns `{ok: false, error: {...}}`, always relay `error.user_message` verbatim to the user. Do NOT paraphrase, soften, or omit error details. The `error.suggestion` field, if non-null, should be offered as a follow-up suggestion.

3. **Statistical modesty** — never claim "significant" for p > 0.05. The tool's explanation already follows statistical conventions; do not amplify or exaggerate findings.

4. **Greylist loop** — when snla_analyze returns `requires_confirmation: true`, you MUST present the `message` field to the user and WAIT for explicit confirmation before calling snla_confirm(). Do NOT auto-confirm.

5. **ENGINE_BUSY** — when you receive `error.code == "ENGINE_BUSY"`, relay the message and suggest retrying in 15 seconds. Do NOT invent alternative actions.

6. **METHOD_UNAVAILABLE** — when a method is rejected (e.g., simple_regression without SPSS), present the `error.suggestion` as the recommended alternative action.

7. **No data → no analysis** — if snla_status() returns `has_data: false`, prompt the user to upload a file before proceeding.

## Multi-turn Context

The servers tracks per-session state. After the first analysis:
- `snla_status()` returns the current dataset info
- Follow-up queries like "那换成班级差异呢？" will work via `snla_analyze`
- Export uses the last analysis result

## Supported Methods (Runtime)

Method availability is returned dynamically by `snla_status()`.trusted_methods.
Consult it before routing user intent to a specific method.
