# Airline Dataset Test Plan

Dataset: `data/fixtures/airline.sav`

Purpose: verify StatsTalk on a larger, realistic dataset with categorical variables, rating scales, delays, and numeric travel metrics.

## Startup

```powershell
cd D:\Projects\StatsTalk
venv\Scripts\activate
python launcher.py
```

Recommended settings:

```ini
STATS_BACKEND=python
LLM_MOCK=false
```

Use `STATS_BACKEND=spss` for SPSS-specific verification.

## Variable Areas

| Area | Example variables |
| --- | --- |
| Demographics | `Gender`, `Age` |
| Travel | `FlightDistance`, `TypeofTravel`, `Class`, `CustomerType` |
| Service ratings | `Inflightwifiservice`, `Foodanddrink`, `Seatcomfort`, `Onlineboarding` |
| Operations | `DepartureDelayinMinutes`, `ArrivalDelayinMinutes` |
| Outcome | `satisfaction` |

## Test Scenarios

| ID | Input | Expected method |
| --- | --- | --- |
| A1 | `飞行距离的平均值和标准差是多少` | descriptives |
| A2 | `统计各舱位等级的人数` | frequencies |
| A3 | `比较男性和女性的满意度是否有差异` | independent_t_test |
| A4 | `不同出行类型的飞行距离是否有显著差异` | oneway_anova or t-test depending on groups |
| A5 | `舱位等级和满意度之间是否有关联` | chi_square/crosstabs |
| A6 | `在线值机便利性和满意度的关系` | correlation |
| A7 | `飞行距离能预测满意度吗` | simple_regression |
| A8 | `不同舱位等级的满意度，用非参数检验` | kruskal_wallis |
| A9 | `男性和女性的行李服务评价是否有差异，用非参数检验` | mann_whitney_u |

## Acceptance Checks

- Upload finishes and variables are visible.
- Planner chooses a plausible method and variables.
- Result includes statistics, table data, and explanation.
- Python backend does not require SPSS.
- SPSS backend, when enabled, produces parseable OMS XML or a clear error.
- Word export works after at least one successful analysis.

## Notes

The airline dataset contains many ordinal service ratings. For strict statistical interpretation, some rating comparisons may be better treated as non-parametric analyses. The test goal is to verify routing and output quality, not to claim every default method is the only valid statistical choice.
