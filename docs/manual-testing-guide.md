# Manual Testing Guide

Environment: Windows 10 or later, Python 3.10+, project root `D:\Projects\StatsTalk`.

## Start

```powershell
cd D:\Projects\StatsTalk
venv\Scripts\activate
python launcher.py
```

API-only alternative:

```powershell
python snla/ui/server.py
```

The desktop launcher opens the secure local page automatically on a random loopback
port. API-only mode prints a one-use bootstrap URL for that launch; do not reuse a URL
from an earlier process.

## Demo Data Flow

1. Click the demo data button, or upload `data/fixtures/test_data.sav`.
2. Confirm variables appear: `gender`, `score`, `class`, `age`.
3. Ask `比较男女成绩差异`.
4. Confirm the result includes a method, statistics, and a plain-language explanation.
5. Ask `显示成绩的描述性统计`.
6. Export a Word report and open the downloaded file.

## Airline Data Flow

Dataset: `data/fixtures/airline.sav`.

1. Upload the file.
2. Confirm variables and row count are shown.
3. Run these scenarios:

| Scenario | Input | Expected method family |
| --- | --- | --- |
| Descriptives | `飞行距离的平均值和标准差是多少` | descriptives |
| Frequencies | `统计各舱位等级的人数` | frequencies |
| Group comparison | `比较男性和女性的满意度是否有差异` | t-test |
| ANOVA | `不同出行类型的飞行距离是否有差异` | ANOVA |
| Crosstabs | `舱位等级和满意度之间是否有关联` | chi-square/crosstabs |
| Correlation | `在线值机便利性和满意度的关系` | correlation |
| Regression | `飞行距离能预测满意度吗` | regression |
| Non-parametric | `不同舱位等级的满意度，用非参数检验` | Kruskal-Wallis |

## Edge Cases

| Check | Expected result |
| --- | --- |
| Submit empty input | 400 error |
| Submit before uploading data | 400 error |
| Rapidly submit more than 10 analyses in one minute | 429 error |
| Start two analyses concurrently | 409 error |
| Upload `.txt` | rejected |
| Upload file over 500 MB | rejected |
| Cancel while SPSS is running | process stops and UI recovers |

## MCP Smoke Test

```powershell
python scripts/mcp_integration_test.py
```

Expected tools:

- `snla_status`
- `snla_upload`
- `snla_variables`
- `snla_analyze`
- `snla_confirm`
- `snla_cancel`
- `snla_export`

## Record

For each run, record:

- Date and commit
- Backend: `python` or `spss`
- LLM mode: mock or real
- Dataset
- Scenario input
- Success/failure
- Error message, if any
