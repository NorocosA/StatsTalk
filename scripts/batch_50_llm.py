"""Batch run all 50 test cases with real LLM and collect metrics."""
import csv
import json
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.e2e_demo import run_pipeline


def norm_method(m: str) -> str:
    """Normalize method name for comparison."""
    return m.lower().replace(" ", "_").replace("-", "_")


def main():
    csv_path = Path(__file__).resolve().parent.parent / "data" / "test_cases_50.csv"
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        cases = list(csv.DictReader(f))

    results = []
    start_all = time.perf_counter()

    for i, row in enumerate(cases):
        cid = row["编号"]
        query = row["NL描述"]
        expected = norm_method(row.get("预期统计方法", ""))
        category = row.get("分类", "")

        print(f"\n{'='*60}")
        print(f"[{i+1}/50] {cid} ({category})")
        print(f"  Query: {query[:60]}")
        if expected:
            print(f"  Expected: {expected}")

        t0 = time.perf_counter()
        try:
            report = run_pipeline(
                str(Path("data/fixtures/test_data_v2.sav")),
                query,
                "./p0_output",
            )
            elapsed = time.perf_counter() - t0

            intent = report.get("intent", "?")
            method = norm_method(report.get("method", "?"))
            syntax_ok = report.get("validation", {}).get("valid", False)
            exec_ok = report.get("execution", {}).get("success", False)
            stats = report.get("analysis", {}).get("statistics", {})
            p_val = stats.get("p_value", None)
            explanation = report.get("analysis", {}).get("explanation", "")[:80]

            method_match = method.startswith(expected[:5]) if expected else True

            result = {
                "id": cid, "category": category, "query": query[:60],
                "expected_method": expected, "actual_method": method,
                "intent": intent, "method_correct": method_match,
                "syntax_valid": syntax_ok, "exec_ok": exec_ok,
                "p_value": p_val, "elapsed_s": round(elapsed, 1),
                "explanation": explanation, "error": None,
            }

            icon = "✅" if (exec_ok and method_match) else ("⚠️" if exec_ok else "❌")
            print(f"  {icon} intent={intent} method={method} "
                  f"exec={'OK' if exec_ok else 'FAIL'} "
                  f"match={'OK' if method_match else 'WRONG'} "
                  f"({elapsed:.1f}s)")

        except Exception as e:
            elapsed = time.perf_counter() - t0
            result = {
                "id": cid, "category": category, "query": query[:60],
                "expected_method": expected, "actual_method": "ERROR",
                "intent": "?", "method_correct": False,
                "syntax_valid": False, "exec_ok": False,
                "p_value": None, "elapsed_s": round(elapsed, 1),
                "explanation": "", "error": str(e)[:200],
            }
            print(f"  ❌ ERROR: {str(e)[:100]}")

        results.append(result)

        # Save incremental results
        out_path = Path("p0_output/llm_50_results.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)

    # ── Summary ──────────────────────────────────────────────────────────
    total = len(results)
    exec_ok = sum(1 for r in results if r["exec_ok"])
    syntax_ok = sum(1 for r in results if r["syntax_valid"])
    method_ok = sum(1 for r in results if r["method_correct"])
    errors = sum(1 for r in results if r["error"])

    total_time = time.perf_counter() - start_all
    avg_time = sum(r["elapsed_s"] for r in results) / total if total else 0

    summary = {
        "total": total,
        "exec_success": exec_ok,
        "exec_rate": f"{exec_ok/total*100:.0f}%",
        "syntax_valid": syntax_ok,
        "method_correct": method_ok,
        "method_rate": f"{method_ok/total*100:.0f}%",
        "errors": errors,
        "total_time_s": round(total_time, 1),
        "avg_time_s": round(avg_time, 1),
    }

    print(f"\n{'='*60}")
    print(f"RESULTS: {exec_ok}/{total} executed ({exec_ok/total*100:.0f}%)")
    print(f"  Method correct: {method_ok}/{total} ({method_ok/total*100:.0f}%)")
    print(f"  Syntax valid: {syntax_ok}/{total}")
    print(f"  Errors: {errors}")
    print(f"  Total time: {total_time/60:.1f} min ({avg_time:.1f}s avg)")
    print(f"{'='*60}")

    # Save summary
    with open("p0_output/llm_50_summary.json", "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, indent=2,
                  ensure_ascii=False, default=str)

    print(f"\nReports saved to p0_output/llm_50_*.json")


if __name__ == "__main__":
    main()
