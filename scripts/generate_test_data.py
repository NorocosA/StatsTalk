"""Generate extended test data for P5-3 backend comparison validation.

Creates:
  - data/fixtures/test_data_extended.sav  (~200 rows, 12 columns, all analysis types)
  - data/fixtures/test_data_boundary.sav  (10 rows, NaN values, boundary testing)

All random generation uses fixed seed (42) for reproducibility.
Python-only, no SPSS required.

Usage:
    python scripts/generate_test_data.py
"""

import os
import sys

import numpy as np
import pandas as pd
import pyreadstat

# ── Paths ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES_DIR = os.path.join(PROJECT_ROOT, "data", "fixtures")
ORIGINAL_SAV = os.path.join(FIXTURES_DIR, "test_data.sav")
EXTENDED_SAV = os.path.join(FIXTURES_DIR, "test_data_extended.sav")
BOUNDARY_SAV = os.path.join(FIXTURES_DIR, "test_data_boundary.sav")

# ── Constants ──────────────────────────────────────────────────────────────
TARGET_N = 200
BOUNDARY_N = 10
RNG = np.random.default_rng(42)
SEED = 42


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Load original
# ═══════════════════════════════════════════════════════════════════════════

def load_original(path):
    """Load existing test_data.sav, return (df, meta)."""
    df, meta = pyreadstat.read_sav(path)
    print(f"  Loaded {len(df)} rows, columns: {list(df.columns)}")
    return df, meta


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Extend rows (bootstrap with jitter)
# ═══════════════════════════════════════════════════════════════════════════

def extend_rows(df_original, n_rows):
    """Extend DataFrame to n_rows by sampling with replacement + small jitter.

    Preserves original distribution of gender, class, score, age while adding
    realistic noise to continuous variables.
    """
    n_orig = len(df_original)
    indices = RNG.choice(n_orig, size=n_rows, replace=True)
    df = df_original.iloc[indices].copy().reset_index(drop=True)

    # Add small jitter to numeric columns
    df["score"] = np.round(df["score"] + RNG.normal(0, 0.5, size=n_rows), 1)
    df["age"] = np.round(df["age"] + RNG.normal(0, 0.5, size=n_rows), 1)

    # Clip to reasonable ranges
    df["score"] = df["score"].clip(0, 100)
    df["age"] = df["age"].clip(10, 80)

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Add new columns
# ═══════════════════════════════════════════════════════════════════════════

def add_new_columns(df, n_rows):
    """Add 8 new columns for P5-3 backend comparison validation.

    Columns added:
      pre_score         — correlated with score (r~0.85), for paired t-test
      post_score        — score*1.1 + noise, simulated improvement
      income            — N(50000, 15000), for regression with age
      education         — ordinal {1,2,3,4}, for chi-square / categorical pred
      department        — string "理科"/"文科"/"工科", 3-group ANOVA
      treatment_group   — binary {0,1}, for chi-square tests
      stress_level      — gamma-skewed (mean~30), for non-parametric tests
      satisfaction      — ordinal 1-5 ~uniform, for Spearman correlation
    """
    # pre_score: score + small noise ⇒ r~0.85
    noise = RNG.normal(0, 4.5, size=n_rows)
    df["pre_score"] = np.round(df["score"] + noise, 1).clip(0, 100)

    # post_score: score * 1.1 + noise ⇒ simulates ~10% improvement
    noise = RNG.normal(0, 5.0, size=n_rows)
    df["post_score"] = np.round(df["score"] * 1.1 + noise, 1).clip(0, 100)

    # income: normally distributed, for regression with age
    df["income"] = np.round(RNG.normal(50000, 15000, size=n_rows)).astype(int)
    df["income"] = df["income"].clip(10000, 150000)

    # education: categorical with value labels
    df["education"] = RNG.choice([1, 2, 3, 4], size=n_rows, p=[0.15, 0.30, 0.35, 0.20])

    # department: string type
    df["department"] = RNG.choice(["理科", "文科", "工科"], size=n_rows, p=[0.35, 0.35, 0.30])

    # treatment_group: binary
    df["treatment_group"] = RNG.choice([0, 1], size=n_rows, p=[0.5, 0.5])

    # stress_level: gamma-distributed, right-skewed, mean ~30
    stress = RNG.gamma(4, 7.5, size=n_rows)
    df["stress_level"] = np.round(stress, 1).clip(0, 100)

    # satisfaction: ordinal 1-5, approximately uniform
    df["satisfaction"] = RNG.integers(1, 6, size=n_rows)

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 4: Add NaN values (boundary dataset only)
# ═══════════════════════════════════════════════════════════════════════════

def add_nan_values(df, n_nan=3):
    """Insert NaN into score and income for boundary testing."""
    idx_score = RNG.choice(len(df), size=min(n_nan, len(df)), replace=False)
    df.loc[idx_score, "score"] = np.nan

    idx_income = RNG.choice(len(df), size=min(n_nan, len(df)), replace=False)
    df.loc[idx_income, "income"] = np.nan

    return df


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Build metadata
# ═══════════════════════════════════════════════════════════════════════════

def build_variable_labels(original_labels_dict):
    """Combine original and new variable labels into a single dict."""
    labels = dict(original_labels_dict)
    labels.update({
        "pre_score": "Pre-test Score",
        "post_score": "Post-test Score",
        "income": "Annual Income",
        "education": "Education Level",
        "department": "Department",
        "treatment_group": "Treatment Group",
        "stress_level": "Stress Level",
        "satisfaction": "Satisfaction Score",
    })
    return labels


def build_value_labels(original_value_labels):
    """Combine original and new variable value labels into a single dict."""
    vlabels = dict(original_value_labels)

    # Update gender labels to Chinese (spec requirement for extended dataset)
    vlabels["gender"] = {1: "男", 2: "女"}

    # New variable value labels
    vlabels["education"] = {1: "小学及以下", 2: "初中", 3: "高中", 4: "大学及以上"}
    vlabels["treatment_group"] = {0: "对照组", 1: "实验组"}

    return vlabels


def build_measure_map():
    """Return variable_measure dict for all columns."""
    return {
        "gender": "nominal",
        "score": "scale",
        "class": "nominal",
        "age": "scale",
        "pre_score": "scale",
        "post_score": "scale",
        "income": "scale",
        "education": "ordinal",
        "department": "nominal",
        "treatment_group": "nominal",
        "stress_level": "scale",
        "satisfaction": "ordinal",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

def main():
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    print("=" * 60)
    print("P5-3 Test Data Generator")
    print("=" * 60)

    # ── Load original ──────────────────────────────────────────────────
    print("\n[1/4] Loading original test_data.sav ...")
    df_orig, meta = load_original(ORIGINAL_SAV)

    # ── Build extended dataset ─────────────────────────────────────────
    print(f"\n[2/4] Generating extended dataset ({TARGET_N} rows) ...")
    df_ext = extend_rows(df_orig, TARGET_N)
    df_ext = add_new_columns(df_ext, TARGET_N)

    var_labels = build_variable_labels(meta.column_names_to_labels)
    var_val_labels = build_value_labels(meta.variable_value_labels)
    var_measure = build_measure_map()

    print(f"  Writing {EXTENDED_SAV} ...")
    pyreadstat.write_sav(
        df_ext,
        EXTENDED_SAV,
        column_labels=var_labels,
        variable_value_labels=var_val_labels,
        variable_measure=var_measure,
    )
    print(f"  OK Extended dataset written ({len(df_ext)} rows, {len(df_ext.columns)} cols)")

    # ── Build boundary dataset ─────────────────────────────────────────
    print(f"\n[3/4] Generating boundary dataset ({BOUNDARY_N} rows with NaN) ...")
    df_bnd = extend_rows(df_orig, BOUNDARY_N)
    df_bnd = add_new_columns(df_bnd, BOUNDARY_N)
    df_bnd = add_nan_values(df_bnd, n_nan=3)

    print(f"  Writing {BOUNDARY_SAV} ...")
    pyreadstat.write_sav(
        df_bnd,
        BOUNDARY_SAV,
        column_labels=var_labels,
        variable_value_labels=var_val_labels,
        variable_measure=var_measure,
    )
    print(f"  OK Boundary dataset written ({len(df_bnd)} rows, {len(df_bnd.columns)} cols)")

    # ── Verification ───────────────────────────────────────────────────
    print("\n[4/4] Verification ...")
    for path, name in [(EXTENDED_SAV, "Extended"), (BOUNDARY_SAV, "Boundary")]:
        df_check, meta_check = pyreadstat.read_sav(path)
        nas = df_check.isna().sum()
        cols_with_na = {k: int(v) for k, v in nas.items() if v > 0}

        print(f"\n  ── {name} Dataset ──")
        print(f"     Rows: {len(df_check)}")
        print(f"     Columns ({len(df_check.columns)}): {list(df_check.columns)}")
        print(f"     Value labels: {list(meta_check.variable_value_labels.keys())}")
        print(f"     Column labels: {list(meta_check.column_names_to_labels.keys())}")
        if cols_with_na:
            print(f"     Missing values: {cols_with_na}")
        else:
            print(f"     Missing values: (none)")

        # Quick sanity checks
        if "pre_score" in df_check.columns:
            corr = df_check[["score", "pre_score"]].dropna().corr().iloc[0, 1]
            print(f"     Correlation score vs pre_score: r={corr:.3f}")
        if "income" in df_check.columns:
            print(f"     Income range: {df_check['income'].min():.0f} - {df_check['income'].max():.0f}")
        if "stress_level" in df_check.columns:
            print(f"     Stress level range: {df_check['stress_level'].min():.1f} - {df_check['stress_level'].max():.1f}")
            print(f"     Stress level mean: {df_check['stress_level'].mean():.1f} (target: ~30, right-skewed)")

    print("\n" + "=" * 60)
    print("Done! Both datasets ready for P5-3 validation.")
    print("=" * 60)


if __name__ == "__main__":
    main()
