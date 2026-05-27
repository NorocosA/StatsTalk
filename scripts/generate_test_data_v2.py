"""Generate test_data_v2.sav — extended dataset for 50-case verification.

Adds variables that the original test_data.sav was missing:
  - class_id  (Numeric, 1=A 2=B 3=C) — enables ONEWAY ANOVA
  - income    (Numeric) — for correlation/regression
  - education (Numeric, 1=HS 2=BA 3=MA 4=PhD) — for multi-group ANOVA
  - anxiety   (Numeric) — for correlation
  - exam1/exam2 (Numeric) — for paired t-test
  - group     (Numeric, 1=Control 2=Treatment) — for t-test

Also includes all original variables from test_data.sav.
"""

import os
import random
import sys

import numpy as np
import pandas as pd
import pyreadstat

random.seed(42)
np.random.seed(42)

n = 30

# ── Original variables ─────────────────────────────────────────────────
genders = [1, 2] * 15
random.shuffle(genders)

scores, ages = [], []
for g in genders:
    if g == 1:
        scores.append(round(random.gauss(78, 8), 1))
        ages.append(random.randint(18, 24))
    else:
        scores.append(round(random.gauss(84, 7), 1))
        ages.append(random.randint(18, 25))
scores = [max(60, min(100, s)) for s in scores]

# classes as strings (original)
classes_str = ["A", "B", "C"] * 10
random.shuffle(classes_str)

# ── NEW: class_id as numeric (enables ONEWAY) ──────────────────────────
class_id_map = {"A": 1, "B": 2, "C": 3}
class_ids = [class_id_map[c] for c in classes_str]

# ── NEW: income (correlated with age and education) ────────────────────
education_levels = [1] * 7 + [2] * 10 + [3] * 8 + [4] * 5  # 1=HS, 2=BA, 3=MA, 4=PhD
random.shuffle(education_levels)
income = [
    round(3000 + edu * 2000 + age * 200 + random.gauss(0, 1500), 0)
    for edu, age in zip(education_levels, ages)
]
income = [max(2000, min(20000, i)) for i in income]

# ── NEW: anxiety (negatively correlated with score) ────────────────────
anxiety = [round(50 - (s - 70) * 0.3 + random.gauss(0, 5), 1) for s in scores]
anxiety = [max(20, min(80, a)) for a in anxiety]

# ── NEW: exam1 / exam2 (paired, exam2 slightly higher) ─────────────────
exam1 = [round(random.gauss(72, 10), 1) for _ in range(n)]
exam2 = [round(e1 + random.gauss(3, 5), 1) for e1 in exam1]
exam1 = [max(50, min(100, e)) for e in exam1]
exam2 = [max(50, min(100, e)) for e in exam2]

# ── NEW: group (1=Control, 2=Treatment) ────────────────────────────────
groups = [1] * 15 + [2] * 15
random.shuffle(groups)

# ── Build DataFrame ─────────────────────────────────────────────────────
df = pd.DataFrame(
    {
        "gender": genders,
        "score": scores,
        "class": classes_str,
        "class_id": class_ids,
        "age": ages,
        "income": income,
        "education": education_levels,
        "anxiety": anxiety,
        "exam1": exam1,
        "exam2": exam2,
        "group": groups,
    }
)

# ── Write .sav ──────────────────────────────────────────────────────────
outdir = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "fixtures",
)
os.makedirs(outdir, exist_ok=True)
outpath = os.path.join(outdir, "test_data_v2.sav")

pyreadstat.write_sav(
    df,
    outpath,
    column_labels=[
        "Gender",
        "Test Score",
        "Class Name",
        "Class ID",
        "Age",
        "Monthly Income",
        "Education Level",
        "Anxiety Score",
        "Exam 1 Score",
        "Exam 2 Score",
        "Treatment Group",
    ],
    variable_value_labels={
        "gender": {1: "Male", 2: "Female"},
        "class_id": {1: "Class A", 2: "Class B", 3: "Class C"},
        "education": {1: "High School", 2: "Bachelor", 3: "Master", 4: "PhD"},
        "group": {1: "Control", 2: "Treatment"},
    },
    variable_measure={
        "gender": "nominal",
        "score": "scale",
        "class": "nominal",
        "class_id": "nominal",
        "age": "scale",
        "income": "scale",
        "education": "ordinal",
        "anxiety": "scale",
        "exam1": "scale",
        "exam2": "scale",
        "group": "nominal",
    },
)

print(f"Created: {outpath}")
print(f"  Cases: {len(df)}, Variables: {len(df.columns)}")
for col in df.columns:
    print(f"  {col:12s}: {str(df[col].dtype):8s}  range=[{df[col].min()}, {df[col].max()}]")
