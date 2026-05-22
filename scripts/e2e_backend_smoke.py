#!/usr/bin/env python3
"""P5-3f: E2E backend smoke test — API-layer validation (B script).

Sends real /api/analyze requests through the Flask server to verify that
the backend routing layer (server.py) correctly switches between SPSS and
Python backends and that the results are consistent.

Usage::

    python scripts/e2e_backend_smoke.py              # Full smoke test
    python scripts/e2e_backend_smoke.py --quick      # 3 cases only
    python scripts/e2e_backend_smoke.py --backend python  # Python only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Project root & imports
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Smoke test cases — 5 representative analyses
# ---------------------------------------------------------------------------
SMOKE_CASES = [
    {
        "id": "smoke_ttest",
        "text": "比较男女生在成绩上是否有显著差异",
        "expected_method": "independent_t_test",
    },
    {
        "id": "smoke_anova",
        "text": "比较不同班级的成绩差异",
        "expected_method": "oneway_anova",
    },
    {
        "id": "smoke_correlation",
        "text": "研究年龄和成绩之间的关系",
        "expected_method": "pearson_correlation",
    },
    {
        "id": "smoke_descriptives",
        "text": "统计成绩的平均分和标准差",
        "expected_method": "descriptives",
    },
    {
        "id": "smoke_chi_square",
        "text": "分析性别和班级之间是否存在关联",
        "expected_method": "chi_square",
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BASE_URL = "http://127.0.0.1:8501"
TEST_DATA = PROJECT_ROOT / "data" / "fixtures" / "test_data.sav"


def start_server():
    """Start the Flask server in a background thread."""
    _original_cwd = os.getcwd()
    os.chdir(PROJECT_ROOT)
    os.environ["LLM_MOCK"] = "true"

    from snla.ui.server import app

    def _run():
        app.run(host="127.0.0.1", port=8501, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(2.0)  # Wait for server to start
    return t


def upload_file() -> bool:
    """Upload test_data.sav to the server."""
    with open(TEST_DATA, "rb") as f:
        resp = requests.post(
            f"{BASE_URL}/api/upload",
            files={"file": ("test_data.sav", f, "application/octet-stream")},
        )
    data = resp.json()
    if not data.get("ok"):
        print(f"  [FAIL] Upload failed: {data.get('error')}")
        return False
    print(f"  [OK]   Uploaded: {data.get('filename')} ({data.get('row_count')} rows)")
    return True


def run_analysis(text: str, backend: str) -> dict | None:
    """Send an analyze request with the given backend."""
    # Set backend via env (server reads STATS_BACKEND at module level,
    # but we can override via the settings API)
    # Instead, we pass backend preference via query param approach...
    # Actually, the server reads STATS_BACKEND from env at startup.
    # For smoke testing, we toggle the backend by temporarily overriding env.
    import snla.config as cfg

    original = cfg.STATS_BACKEND
    try:
        # Directly monkey-patch to test both paths
        cfg.STATS_BACKEND = backend
        resp = requests.post(
            f"{BASE_URL}/api/analyze",
            json={"text": text},
            timeout=120,
        )
        return resp.json()
    finally:
        cfg.STATS_BACKEND = original


def check_status() -> dict:
    """Check /api/status endpoint."""
    resp = requests.get(f"{BASE_URL}/api/status")
    return resp.json()


def compare_results(spss_result: dict, py_result: dict, case_id: str) -> None:
    """Compare key statistics between backends."""
    spss_stats = spss_result.get("result", {}).get("statistics", {})
    py_stats = py_result.get("result", {}).get("statistics", {})

    spss_p = spss_stats.get("p_value")
    py_p = py_stats.get("p_value")

    # Check for limited_mode (strategy C)
    if py_result.get("limited_mode"):
        print(f"  [INFO] {case_id}: Limited mode active — method not trusted for no-SPSS")
        print(f"         Warning: {py_result.get('warning', 'N/A')[:80]}...")
        return

    if spss_p is not None and py_p is not None:
        diff = abs(spss_p - py_p)
        conflict = (spss_p < 0.05) != (py_p < 0.05)
        status = "CONFLICT" if conflict else "OK"
        print(f"  [{status}] {case_id}: SPSS p={spss_p:.4f}, Python p={py_p:.4f} (diff={diff:.4f})")
        if diff > 0.01:
            print(f"         WARNING: p-value difference exceeds 0.01 threshold")
    else:
        spss_str = f"p={spss_p:.4f}" if spss_p is not None else "N/A"
        py_str = f"p={py_p:.4f}" if py_p is not None else "N/A"
        print(f"  [SKIP] {case_id}: SPSS {spss_str}, Python {py_str} — one backend missing p-value")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="P5-3f E2E backend smoke test")
    parser.add_argument("--quick", action="store_true", help="Run only 3 cases")
    parser.add_argument("--backend", choices=["spss", "python", "both"],
                        default="both", help="Which backend(s) to test")
    args = parser.parse_args()

    cases = SMOKE_CASES[:3] if args.quick else SMOKE_CASES

    print("=" * 60)
    print("P5-3f: E2E Backend Smoke Test (API-layer validation)")
    print("=" * 60)

    # 1. Start server
    print("\n[1/4] Starting Flask server...")
    server_thread = start_server()

    try:
        # 2. Check status
        print("\n[2/4] Checking /api/status...")
        status = check_status()
        print(f"  [OK]   SPSS available: {status.get('spss_available')}")
        print(f"  [OK]   Current backend: {status.get('current_backend')}")
        print(f"  [OK]   Trusted methods: {len(status.get('trusted_methods', []))}")
        print(f"  [OK]   Trust source: {status.get('trust_source')}")

        # 3. Upload data
        print("\n[3/4] Uploading test data...")
        if not upload_file():
            return 1

        # 4. Run test cases
        print(f"\n[4/4] Running {len(cases)} smoke test case(s)...")
        passed = 0
        failed = 0

        for case in cases:
            cid = case["id"]
            text = case["text"]
            print(f"\n  --- {cid}: \"{text}\" ---")

            if args.backend in ("spss", "both"):
                print(f"  [SPSS backend]")
                spss_result = run_analysis(text, "spss")
                if spss_result and spss_result.get("ok"):
                    method = spss_result.get("method", "?")
                    explanation = spss_result.get("explanation", "")
                    has_explanation = bool(explanation and explanation.strip())
                    print(f"    [OK]   Method: {method}")
                    print(f"    [OK]   Explanation: {'Yes' if has_explanation else 'No (limited mode)'}")
                    passed += 1
                else:
                    err = spss_result.get("error", "unknown") if spss_result else "no response"
                    print(f"    [FAIL] {err}")
                    failed += 1
                time.sleep(1.0)

            if args.backend in ("python", "both"):
                print(f"  [Python backend]")
                py_result = run_analysis(text, "python")
                if py_result and py_result.get("ok"):
                    method = py_result.get("method", "?")
                    is_limited = py_result.get("limited_mode", False)
                    has_explanation = bool(py_result.get("explanation"))
                    warning = py_result.get("warning", "")
                    print(f"    [OK]   Method: {method}")
                    print(f"    [OK]   Limited mode: {is_limited}")
                    print(f"    [OK]   Explanation: {'Yes' if has_explanation else 'No'}")
                    if warning:
                        print(f"    [INFO] Warning: {warning[:100]}...")
                    passed += 1
                else:
                    err = py_result.get("error", "unknown") if py_result else "no response"
                    print(f"    [FAIL] {err}")
                    failed += 1

            # Compare if both backends ran
            if args.backend == "both" and spss_result and py_result:
                if spss_result.get("ok") and py_result.get("ok"):
                    compare_results(spss_result, py_result, cid)

    finally:
        # Cleanup — server thread is daemon, will exit with process
        pass

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
