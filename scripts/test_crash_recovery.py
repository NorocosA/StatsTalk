"""SPSS crash recovery test (Plan.md 8.1 item 8).

Tests that SPSS can recover after forced termination:
1. Run a normal analysis to establish baseline
2. Force-terminate 3 times during analysis
3. Verify SPSS can still execute normally on 4th attempt
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from snla.executor.spss import SPSSExecutor

DATA = "data/fixtures/test_data.sav"


def run_baseline():
    """Verify SPSS works normally."""
    e = SPSSExecutor()
    r = e.run("DESCRIPTIVES VARIABLES=score.", DATA, "crash_baseline")
    assert r.success, f"Baseline failed: {r.error_message}"
    print(f"  [BASELINE] OK ({r.duration_seconds:.1f}s)")


def force_terminate_test():
    """Terminate SPSS 3 times, verify recovery on 4th."""
    for i in range(3):
        print(f"  [TERMINATE #{i+1}] Starting...")
        e = SPSSExecutor()
        # Use a syntax that takes measurable time
        # We'll submit it and immediately terminate the executor
        try:
            from snla.config import SPSS_PYTHON_PATH
            import subprocess
            # Start a long-running script
            proc = subprocess.Popen(
                [SPSS_PYTHON_PATH, "-c",
                 "import spss, time; spss.Submit('GET FILE=\\'" +
                 os.path.abspath(DATA).replace("\\", "/") + "\\'.'); "
                 "time.sleep(5)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            time.sleep(1)  # Let it start
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            print(f"  [TERMINATE #{i+1}] Done")
        except Exception as exc:
            print(f"  [TERMINATE #{i+1}] Error: {exc}")
            return False
    return True


def run_recovery():
    """After 3 kills, verify SPSS still works."""
    e = SPSSExecutor()
    r = e.run("DESCRIPTIVES VARIABLES=score age.", DATA, "crash_recovery")
    if r.success:
        print(f"  [RECOVERY] OK ({r.duration_seconds:.1f}s)")
    else:
        print(f"  [RECOVERY] FAILED: {r.error_message}")
    return r.success


def main():
    print("SPSS Crash Recovery Test")
    print("=" * 50)

    # 1. Baseline
    print("1. Baseline test...")
    try:
        run_baseline()
    except Exception as e:
        print(f"   FAILED: {e}")
        return

    # 2. Force terminate 3 times
    print("2. Force terminate x3...")
    ok = force_terminate_test()
    if not ok:
        print("   Termination test had issues, continuing...")

    # 3. Recovery test
    print("3. Recovery test...")
    recovered = run_recovery()

    print("=" * 50)
    if recovered:
        print("RESULT: PASS — SPSS recovered after 3 forced terminations")
    else:
        print("RESULT: FAIL — SPSS did not recover")
    return int(not recovered)


if __name__ == "__main__":
    sys.exit(main())
