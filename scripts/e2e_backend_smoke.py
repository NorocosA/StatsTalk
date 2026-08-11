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
import platform
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
        "grouping_variable": "gender",
        "test_variable": "score",
        "expects_p_value": True,
    },
    {
        "id": "smoke_anova",
        "text": "比较不同教育组的成绩差异",
        "expected_method": "oneway_anova",
        "grouping_variable": "education",
        "test_variable": "score",
        "expects_p_value": True,
    },
    {
        "id": "smoke_correlation",
        "text": "研究年龄和成绩之间的关系",
        "expected_method": "pearson_correlation",
        "grouping_variable": "age",
        "test_variable": "score",
        "expects_p_value": True,
    },
    {
        "id": "smoke_descriptives",
        "text": "统计成绩的平均分和标准差",
        "expected_method": "descriptives",
        "grouping_variable": None,
        "test_variable": "score",
        "expects_p_value": False,
    },
    {
        "id": "smoke_chi_square",
        "text": "分析性别和教育程度之间是否存在关联",
        "expected_method": "chi_square",
        "grouping_variable": "gender",
        "test_variable": "education",
        "expects_p_value": True,
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BASE_URL = ""
HTTP_SESSION = requests.Session()
TEST_DATA = PROJECT_ROOT / "data" / "fixtures" / "test_data_extended.sav"
REPORT_PATH = PROJECT_ROOT / "p0_output" / "e2e_smoke_report.json"


def start_server():
    """Start the Flask server in a background thread."""
    global BASE_URL

    os.chdir(PROJECT_ROOT)
    os.environ["LLM_MOCK"] = "true"

    from snla.ui.launch import prepare_loopback_server
    from snla.ui.server import app

    waitress_server, launch = prepare_loopback_server(app)
    BASE_URL = launch.origin

    def _run():
        waitress_server.run()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    time.sleep(1.0)

    bootstrap_token = parse_qs(urlsplit(launch.bootstrap_url).fragment)["bootstrap_token"][0]
    bootstrap = HTTP_SESSION.post(
        f"{BASE_URL}/api/bootstrap",
        json={"bootstrap_token": bootstrap_token},
        timeout=10,
    )
    bootstrap.raise_for_status()
    HTTP_SESSION.headers["Authorization"] = f"Bearer {bootstrap.json()['session_token']}"
    return t, waitress_server


def upload_file() -> bool:
    """Upload test_data.sav to the server."""
    with open(TEST_DATA, "rb") as f:
        resp = HTTP_SESSION.post(
            f"{BASE_URL}/api/upload",
            files={"file": (TEST_DATA.name, f, "application/octet-stream")},
        )
    data = resp.json()
    if not data.get("ok"):
        print(f"  [FAIL] Upload failed: {data.get('error')}")
        return False
    print(f"  [OK]   Uploaded: {data.get('filename')} ({data.get('row_count')} rows)")
    return True


def run_analysis(case: dict, backend: str) -> dict | None:
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
        resp = HTTP_SESSION.post(
            f"{BASE_URL}/api/analyze",
            json={
                "text": case["text"],
                "method": case["expected_method"],
                "grouping_variable": case["grouping_variable"],
                "test_variable": case["test_variable"],
                "selection_source": "user_selection",
            },
            timeout=120,
        )
        return resp.json()
    finally:
        cfg.STATS_BACKEND = original


def check_status() -> dict:
    """Check /api/status endpoint."""
    resp = HTTP_SESSION.get(f"{BASE_URL}/api/status")
    return resp.json()


def validate_outcome(result: dict | None, case: dict) -> tuple[bool, str]:
    """Require success and the exact method selected by the smoke case."""

    if not result:
        return False, "no response"
    if not result.get("ok"):
        error = result.get("error", {})
        return False, error.get("user_message", str(error))
    actual_method = result.get("method")
    if actual_method != case["expected_method"]:
        return False, f"expected {case['expected_method']}, got {actual_method}"
    return True, ""


def compare_results(spss_result: dict, py_result: dict, case: dict) -> dict:
    """Compare key statistics between backends."""
    case_id = case["id"]
    spss_stats = spss_result.get("result", {}).get("statistics", {})
    py_stats = py_result.get("result", {}).get("statistics", {})

    spss_p = spss_stats.get("p_value")
    py_p = py_stats.get("p_value")

    # Check for limited_mode (strategy C)
    if py_result.get("limited_mode"):
        print(f"  [INFO] {case_id}: Limited mode active — method not trusted for no-SPSS")
        print(f"         Warning: {py_result.get('warning', 'N/A')[:80]}...")
        return {"ok": False, "reason": "Python result unexpectedly used limited mode"}

    if spss_p is not None and py_p is not None:
        diff = abs(spss_p - py_p)
        conflict = (spss_p < 0.05) != (py_p < 0.05)
        status = "CONFLICT" if conflict else "OK"
        print(f"  [{status}] {case_id}: SPSS p={spss_p:.4f}, Python p={py_p:.4f} (diff={diff:.4f})")
        if diff > 0.01:
            print("         WARNING: p-value difference exceeds 0.01 threshold")
        return {
            "ok": not conflict,
            "spss_p": spss_p,
            "python_p": py_p,
            "p_difference": diff,
            "conclusion_conflict": conflict,
        }
    if not case["expects_p_value"]:
        print(f"  [OK] {case_id}: method does not require a p-value comparison")
        return {"ok": True, "reason": "p-value not required"}
    else:
        spss_str = f"p={spss_p:.4f}" if spss_p is not None else "N/A"
        py_str = f"p={py_p:.4f}" if py_p is not None else "N/A"
        print(f"  [FAIL] {case_id}: SPSS {spss_str}, Python {py_str} — missing p-value")
        return {"ok": False, "reason": "one backend is missing a required p-value"}


def save_report(report: dict) -> None:
    """Write the release-review artifact without user data or license details."""

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {REPORT_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="P5-3f E2E backend smoke test")
    parser.add_argument("--quick", action="store_true", help="Run only 3 cases")
    parser.add_argument(
        "--backend",
        choices=["spss", "python", "both"],
        default="both",
        help="Which backend(s) to test",
    )
    args = parser.parse_args()

    cases = SMOKE_CASES[:3] if args.quick else SMOKE_CASES

    print("=" * 60)
    print("P5-3f: E2E Backend Smoke Test (API-layer validation)")
    print("=" * 60)

    # 1. Start server
    print("\n[1/4] Starting Flask server...")
    server_thread, waitress_server = start_server()

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
        case_reports = []

        for case in cases:
            cid = case["id"]
            text = case["text"]
            print(f'\n  --- {cid}: "{text}" ---')
            case_report = {"id": cid, "expected_method": case["expected_method"]}
            spss_result = None
            py_result = None

            if args.backend in ("spss", "both"):
                print("  [SPSS backend]")
                spss_result = run_analysis(case, "spss")
                valid, error = validate_outcome(spss_result, case)
                if valid:
                    method = spss_result.get("method", "?")
                    explanation = spss_result.get("explanation", "")
                    has_explanation = bool(explanation and explanation.strip())
                    print(f"    [OK]   Method: {method}")
                    print(
                        f"    [OK]   Explanation: {'Yes' if has_explanation else 'No (limited mode)'}"
                    )
                    passed += 1
                else:
                    print(f"    [FAIL] {error}")
                    failed += 1
                case_report["spss"] = {
                    "ok": valid,
                    "method": spss_result.get("method") if spss_result else None,
                    "error": error or None,
                }
                time.sleep(1.0)

            if args.backend in ("python", "both"):
                print("  [Python backend]")
                py_result = run_analysis(case, "python")
                valid, error = validate_outcome(py_result, case)
                if valid:
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
                    print(f"    [FAIL] {error}")
                    failed += 1
                case_report["python"] = {
                    "ok": valid,
                    "method": py_result.get("method") if py_result else None,
                    "error": error or None,
                }

            # Compare if both backends ran
            if (
                args.backend == "both"
                and spss_result
                and py_result
                and spss_result.get("ok")
                and py_result.get("ok")
            ):
                comparison = compare_results(spss_result, py_result, case)
                case_report["comparison"] = comparison
                if not comparison["ok"]:
                    failed += 1
            case_reports.append(case_report)

    finally:
        waitress_server.close()

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    from snla import config

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {
            "os": platform.platform(),
            "spss_version": Path(config.SPSS_EXECUTABLE).parent.name,
        },
        "backend_filter": args.backend,
        "cases": case_reports,
        "summary": {"backend_checks_passed": passed, "failed_checks": failed},
    }
    save_report(report)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
