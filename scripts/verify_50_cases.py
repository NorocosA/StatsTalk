"""SNLA 50-Case Test Checklist Verification Script.

Runs each test case through the SNLA Phase 1 pipeline (intent → method →
syntax → validator) using mock LLM mode.  Optionally executes with real
SPSS when available.

Usage:
    python scripts/verify_50_cases.py [--execute] [--spss]

Options:
    --execute   Also run SPSS execution (requires SPSS installed)
    --spss      Same as --execute
"""
import csv
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Respect .env LLM_MOCK setting unless overridden by --mock flag
if "--mock" in sys.argv:
    os.environ["LLM_MOCK"] = "true"
# else: use whatever .env says (real LLM if configured)

from snla.data.reader import read_and_extract
from snla.syntax.validator import validate
from snla.ui.server import (
    session, _phase1_plan, _syntax_template, _make_executor, _execute_syntax, _parse_output
)


CHECKLIST_PATH = Path(PROJECT_ROOT, "data", "fixtures", "50_case_checklist.csv")
DATA_PATH = Path(PROJECT_ROOT, "data", "fixtures", "test_data.sav")
REPORT_PATH = Path(PROJECT_ROOT, "p0_output", "50_case_report.csv")
SPSS_MODE = "--execute" in sys.argv or "--spss" in sys.argv

# ── Load data ──────────────────────────────────────────────────────────────

meta = read_and_extract(str(DATA_PATH))
meta["file_path"] = str(DATA_PATH)
session.dataset_meta = meta
session.variables = meta.get("variables", [])

print(f"Data loaded: {meta['row_count']} rows, {meta['column_count']} vars")
for v in session.variables:
    vl = v.get("value_labels", {})
    vl_str = f" [{', '.join(f'{k}={v}' for k,v in vl.items())}]" if vl else ""
    print(f"  {v['name']:12s} {v.get('type','?'):8s} {v.get('label','')}{vl_str}")
print()

# ── Read checklist ─────────────────────────────────────────────────────────

with open(CHECKLIST_PATH, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    cases = list(reader)

results = []
passed = 0
failed = 0
skipped = 0
exec_pass = 0
exec_fail = 0

print(f"{'='*80}")
print(f"SNLA 50-Case Verification — {'SPSS EXECUTION' if SPSS_MODE else 'SYNTAX ONLY'}")
print(f"{'='*80}\n")

for case in cases:
    cid = case["编号"]
    query = case["NL描述"]
    expected = case["预期统计方法"].strip()
    expected_gvar = case.get("预期分组变量", "").strip() or None
    expected_tvar = case.get("预期检验变量", "").strip() or None

    print(f"[{cid}/50] {query}")
    print(f"         Expected: {expected} | gvar={expected_gvar} | tvar={expected_tvar}")

    # Phase 1: Planning
    try:
        method, plan, gvar, tvar = _phase1_plan(query)
    except Exception as e:
        print(f"  FAIL Phase 1: {e}")
        results.append({**case, "验收状态": "失败", "备注": f"Phase1 error: {e}"})
        failed += 1
        continue

    print(f"         Got:      {method} | gvar={gvar} | tvar={tvar} | plan={plan[:60]}")

    # Method check
    method_ok = method == expected
    if not method_ok:
        # Allow equivalent methods
        equivalents = {
            "pearson_correlation": ["correlations", "pearson_correlation"],
            "chi_square": ["chi_square", "crosstabs"],
        }
        for m, alts in equivalents.items():
            if expected in alts and method in alts:
                method_ok = True
                break

    # Syntax generation
    syntax = _syntax_template(method, grouping_var=gvar, test_var=tvar)
    if not syntax.strip():
        print(f"  FAIL Empty syntax")
        results.append({**case, "验收状态": "失败", "备注": "Empty syntax"})
        failed += 1
        continue

    # Validation
    validation = validate(syntax, [v["name"] for v in session.variables])
    if not validation["valid"]:
        errs = "; ".join(validation["errors"])
        print(f"  FAIL Validation: {errs}")
        results.append({**case, "验收状态": "失败", "备注": f"Validation: {errs}"})
        failed += 1
        continue

    print(f"  OK Syntax valid: {syntax[:80]}...")

    # SPSS Execution (optional)
    if SPSS_MODE:
        executor = _make_executor()
        try:
            exec_result = _execute_syntax(syntax, executor)
            if exec_result.get("success"):
                parsed = _parse_output(exec_result, method)
                if parsed and parsed.statistics:
                    print(f"  OK SPSS: {json.dumps(parsed.statistics, ensure_ascii=False)[:100]}")
                    exec_pass += 1
                else:
                    print(f"  WARN SPSS ran but no stat output")
                    exec_pass += 1
            else:
                err = exec_result.get("error", "unknown")
                print(f"  FAIL SPSS: {err}")
                exec_fail += 1
        except Exception as e:
            print(f"  FAIL SPSS ERROR: {e}")
            exec_fail += 1

    status = "通过" if method_ok else "部分通过"
    note = ""
    if not method_ok:
        note = f"方法不匹配: expected={expected}, got={method}"
    results.append({**case, "验收状态": status, "备注": note})
    if method_ok:
        passed += 1
    else:
        skipped += 1

    print()
    sys.stdout.flush()

# ── Write report ───────────────────────────────────────────────────────────

fieldnames = list(cases[0].keys())
with open(REPORT_PATH, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)

# ── Summary ────────────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"SUMMARY")
print(f"{'='*80}")
print(f"  Syntax: {passed} passed / {failed} failed / {skipped} partial (method mismatch)")
if SPSS_MODE:
    print(f"  SPSS:   {exec_pass} ok / {exec_fail} failed")
print(f"  Report: {REPORT_PATH}")
print(f"{'='*80}")
