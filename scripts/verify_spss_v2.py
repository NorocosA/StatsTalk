"""P0 Verification: Run all 5 SPSS analysis types via Python executor."""

import sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import json, os, time
from datetime import datetime
from snla.executor.spss import SPSSExecutor

executor = SPSSExecutor()
data_path = r"D:\Projects\SPSS Natural Language Assistant(SNLA)\data\fixtures\test_data.sav"

# 5 analysis types with test syntax
tests = [
    ("FREQUENCIES", "FREQUENCIES VARIABLES=gender."),
    ("DESCRIPTIVES", "DESCRIPTIVES VARIABLES=score age."),
    ("T-TEST", "T-TEST GROUPS=gender(1 2) /VARIABLES=score."),
    ("CROSSTABS", "CROSSTABS TABLES=gender BY class."),
    ("REGRESSION", "REGRESSION /DEPENDENT=score /METHOD=ENTER age."),
]

results = []
for name, syntax in tests:
    print(f"Running {name}... ", end="", flush=True)
    t0 = time.time()
    result = executor.run(syntax=syntax, data_path=data_path, output_name=name.lower())
    elapsed = time.time() - t0

    status = "OK" if result.success else "FAIL"
    xml_size = os.path.getsize(result.xml_path) if result.xml_path else 0
    print(f"{status} ({elapsed:.1f}s, XML: {xml_size} bytes)")

    results.append(
        {
            "name": name,
            "success": result.success,
            "exit_code": result.exit_code,
            "duration_s": elapsed,
            "xml_path": result.xml_path,
            "xml_size": xml_size,
            "error": result.error_message,
        }
    )

# Summary
passed = sum(1 for r in results if r["success"])
total = len(results)
print(f"\n{'=' * 60}")
print(f"Result: {passed}/{total} passed")
print(f"Time: {datetime.now().isoformat()}")

# Save report
report = {
    "timestamp": datetime.now().isoformat(),
    "spss_python": executor.spss_python,
    "exec_mode": "python",
    "passed": passed,
    "total": total,
    "results": results,
}

outdir = r"D:\Projects\SPSS Natural Language Assistant(SNLA)\p0_output"
with open(os.path.join(outdir, "connectivity_report.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print(f"Report saved: {os.path.join(outdir, 'connectivity_report.json')}")
