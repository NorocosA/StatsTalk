"""SNLA Combined 50+15 Case Verification — Real LLM + SPSS, 30s cooldown.

Tests test_data.sav (50 cases) then airline.sav (15 cases) with real LLM,
30-second delay between each case to avoid API rate limiting.

Usage:
    python scripts/verify_combined.py [--skip-spss] [--dataset airline|testdata|both]
"""

import csv
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ["LLM_MOCK"] = "false"
os.environ["LLM_CALL_LOG"] = "false"

from snla.data.reader import read_and_extract
from snla.syntax.validator import validate
from snla.ui.server import (
    session,
    _phase1_plan,
    _syntax_template,
    _make_executor,
    _execute_syntax,
    _parse_output,
)

COOLDOWN = 30  # seconds between cases
SKIP_SPSS = "--skip-spss" in sys.argv
DATASET = "both"
for a in sys.argv:
    if a.startswith("--dataset="):
        DATASET = a.split("=", 1)[1]

TESTDATA_CSV = Path(PROJECT_ROOT, "data", "fixtures", "50_case_checklist.csv")
TESTDATA_SAV = Path(PROJECT_ROOT, "data", "fixtures", "test_data.sav")
AIRLINE_CSV = Path(PROJECT_ROOT, "data", "fixtures", "airline_checklist.csv")
AIRLINE_SAV = Path(PROJECT_ROOT, "data", "fixtures", "airline.sav")
REPORT_DIR = Path(PROJECT_ROOT, "p0_output")
REPORT_DIR.mkdir(exist_ok=True)

DATASETS = []
if DATASET in ("testdata", "both"):
    DATASETS.append(("testdata", str(TESTDATA_SAV), str(TESTDATA_CSV)))
if DATASET in ("airline", "both"):
    DATASETS.append(("airline", str(AIRLINE_SAV), str(AIRLINE_CSV)))

total_passed = total_failed = total_exec_ok = total_exec_fail = 0
all_results = []

for ds_name, sav_path, csv_path in DATASETS:
    # ── Load data ──
    meta = read_and_extract(sav_path)
    meta["file_path"] = sav_path
    session.dataset_meta = meta
    session.variables = meta.get("variables", [])
    session.history = []

    print(f"\n{'=' * 80}")
    print(f"Dataset: {ds_name} — {meta['row_count']} rows, {meta['column_count']} vars")
    print(f"{'=' * 80}")
    for v in session.variables[:6]:
        vl = v.get("value_labels", {}) or {}
        vl_s = f" [{', '.join(f'{k}={v}' for k, v in list(vl.items())[:3])}]" if vl else ""
        print(f"  {v['name']:25s} {v.get('type', '?'):8s} {v.get('label', '')}{vl_s}")
    if len(session.variables) > 6:
        print(f"  ... +{len(session.variables) - 6} more variables")

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        cases = list(csv.DictReader(f))

    ds_passed = ds_failed = ds_exec_ok = ds_exec_fail = 0

    for idx, case in enumerate(cases):
        cid = case.get("编号", str(idx + 1))
        query = case["NL描述"].strip()
        expected = case.get("预期统计方法", "").strip()

        print(f"\n[{cid}] {query}")
        print(f"     Expected: {expected}")

        # Phase 1: LLM Planning
        t0 = time.time()
        try:
            method, plan, gvar, tvar = _phase1_plan(query)
        except Exception as e:
            print(f"     FAIL Phase1: {e}")
            ds_failed += 1
            all_results.append({**case, "验收状态": "失败", "备注": f"LLM error: {e}"})
            continue
        llm_time = time.time() - t0
        plan_short = (plan or "(empty)")[:80]
        print(f"     LLM: {method} | g={gvar} t={tvar} | {llm_time:.1f}s")
        print(f"     Plan: {plan_short}")

        # Syntax from template
        syntax = _syntax_template(method, grouping_var=gvar, test_var=tvar)
        validation = validate(syntax, [v["name"] for v in session.variables])
        if not validation["valid"]:
            errs = "; ".join(validation["errors"])[:100]
            print(f"     FAIL Valid: {errs}")
            ds_failed += 1
            all_results.append({**case, "验收状态": "失败", "备注": f"Validation: {errs}"})
            continue
        print(f"     OK Syntax: {syntax[:70]}...")

        # SPSS Execution
        if not SKIP_SPSS:
            exec_result = _execute_syntax(syntax, _make_executor())
            if exec_result.get("success"):
                parsed = _parse_output(exec_result, method)
                if parsed and parsed.statistics:
                    s = json.dumps(parsed.statistics, ensure_ascii=False)[:100]
                    print(f"     OK SPSS: {s}")
                    ds_exec_ok += 1
                else:
                    print(f"     WARN No stat output")
                    ds_exec_ok += 1
            else:
                print(f"     FAIL SPSS: {exec_result.get('error', '?')}")
                ds_exec_fail += 1

        method_ok = method == expected
        if method_ok:
            ds_passed += 1
            status = "通过"
        else:
            status = "方法偏差"
        all_results.append(
            {
                **case,
                "验收状态": status,
                "实际方法": method,
                "分组变量": gvar or "",
                "检验变量": tvar or "",
                "LLM耗时s": f"{llm_time:.1f}",
            }
        )

        # ── Cooldown ──
        if idx < len(cases) - 1:
            print(f"     ... cooling {COOLDOWN}s ...", end="", flush=True)
            time.sleep(COOLDOWN)
            print(" done")

    print(
        f"\n--- {ds_name} Summary: {ds_passed} passed, {ds_failed} failed, "
        f"SPSS {ds_exec_ok} ok / {ds_exec_fail} fail ---"
    )
    total_passed += ds_passed
    total_failed += ds_failed
    total_exec_ok += ds_exec_ok
    total_exec_fail += ds_exec_fail

# ── Write report ──
report_path = REPORT_DIR / "combined_verification_report.csv"
fieldnames = list(all_results[0].keys()) if all_results else []
with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_results)

print(f"\n{'=' * 80}")
print(
    f"FINAL: {total_passed} method-match, {total_failed} issues, "
    f"SPSS {total_exec_ok} ok / {total_exec_fail} fail"
)
print(f"Report: {report_path}")
