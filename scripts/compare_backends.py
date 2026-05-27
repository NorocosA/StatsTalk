#!/usr/bin/env python3
"""P5-3 Backend Comparison Engine — SPSS vs Python (pingouin) validation.

Validates statistical consistency between SPSS and Python backends across
all 12 analysis methods. Generates ``p0_output/backend_comparison.json``
with per-case comparison data and ``p0_output/method_trust.json`` with
per-method whitelist for P5-4 no-SPSS routing.

Usage::

    python scripts/compare_backends.py                          # All test cases
    python scripts/compare_backends.py --methods ttest,anova    # Specific methods
    python scripts/compare_backends.py --backend python         # Python-only
    python scripts/compare_backends.py --backend spss           # SPSS-only
    python scripts/compare_backends.py --list                   # List cases
    python scripts/compare_backends.py --alpha 0.01             # Custom alpha
    python scripts/compare_backends.py --ids chisquare_2x3_001  # Specific cases
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Windows console encoding fix (avoids UnicodeEncodeError on GBK terminals)
# ---------------------------------------------------------------------------

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# ---------------------------------------------------------------------------
# Project root & import setup
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Method name → filter short-names (for --methods flag)
# ---------------------------------------------------------------------------

METHOD_FILTER_MAP: dict[str, list[str]] = {
    "ttest": ["independent_t_test", "paired_t_test"],
    "independent_ttest": ["independent_t_test"],
    "paired_ttest": ["paired_t_test"],
    "anova": ["oneway_anova"],
    "regression": ["simple_regression"],
    "correlation": ["pearson_correlation", "spearman_correlation", "correlations"],
    "pearson": ["pearson_correlation", "correlations"],
    "spearman": ["spearman_correlation"],
    "chisquare": ["chi_square", "crosstabs"],
    "frequencies": ["frequencies"],
    "descriptives": ["descriptives"],
    "mannwhitney": ["mann_whitney_u"],
    "kruskalwallis": ["kruskal_wallis"],
}

DEFAULT_TIMEOUT = 120  # seconds per test case
DEFAULT_TRUST_THRESHOLD = 0.98
OUTPUT_DIR = PROJECT_ROOT / "p0_output"
YAML_PATH = PROJECT_ROOT / "data" / "fixtures" / "backend_test_cases.yaml"


# ===================================================================
# Helpers
# ===================================================================


def _resolve_data_path(relative: str) -> Path:
    """Resolve a YAML ``data_file`` relative to project root."""
    return (PROJECT_ROOT / relative).resolve()


def _safe_float(val: Any) -> float | None:
    """Convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _diff(a: float | None, b: float | None) -> float | None:
    """Absolute difference between two numbers."""
    if a is None or b is None:
        return None
    return abs(a - b)


def _ratio(a: float | None, b: float | None, fallback_near_zero: float = 1e-10) -> float | None:
    """Relative difference ``|a-b| / max(|a|,|b|, fallback_near_zero)``."""
    if a is None or b is None:
        return None
    denom = max(abs(a), abs(b), fallback_near_zero)
    return abs(a - b) / denom


def _pick_larger(a: float | None, b: float | None) -> float | None:
    """Return the larger of two values, or whichever is not None."""
    if a is None:
        return b
    if b is None:
        return a
    return a if abs(a) > abs(b) else b


# ===================================================================
# YAML loading
# ===================================================================


def load_test_cases(
    yaml_path: Path,
    methods_filter: list[str] | None = None,
    ids_filter: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Load and filter test cases from the YAML test matrix.

    Args:
        yaml_path: Path to ``backend_test_cases.yaml``.
        methods_filter: If set, only include cases whose ``method`` field
            appears in this list (after resolving via ``METHOD_FILTER_MAP``).
        ids_filter: If set, only include cases whose ``id`` appears in
            this list.

    Returns:
        List of test case dicts.

    Raises:
        SystemExit: If the YAML file cannot be loaded.
    """
    if not yaml_path.is_file():
        print(f"[ERROR] YAML test case file not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    try:
        import yaml
    except ImportError:
        print("[ERROR] PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    if not data or "test_cases" not in data:
        print("[ERROR] YAML file does not contain 'test_cases' key.", file=sys.stderr)
        sys.exit(1)

    cases: list[dict[str, Any]] = list(data["test_cases"])

    # Resolve method filter short-names
    if methods_filter:
        expanded: set[str] = set()
        for mf in methods_filter:
            mf_lower = mf.strip().lower()
            if mf_lower in METHOD_FILTER_MAP:
                expanded.update(METHOD_FILTER_MAP[mf_lower])
            else:
                expanded.add(mf_lower)
        cases = [c for c in cases if c.get("method", "") in expanded]

    if ids_filter:
        id_set = {i.strip() for i in ids_filter}
        cases = [c for c in cases if c.get("id", "") in id_set]

    return cases


# ===================================================================
# Backend setup
# ===================================================================


def _check_spss_available() -> bool:
    """Check if SPSS executable exists and env is configured."""
    from snla.config import SPSS_EXECUTABLE

    if not SPSS_EXECUTABLE or not os.path.isfile(SPSS_EXECUTABLE):
        return False
    return True


def _create_adapter(output_dir: str, exec_mode: str = "python") -> Any:
    """Create a ``BackendAdapter`` with the given SPSS exec mode.

    Args:
        output_dir: Directory for SPSS output artifacts.
        exec_mode: ``"python"`` (default) or ``"batch"``.

    Returns:
        Configured ``BackendAdapter`` instance.
    """
    from snla.executor.adapter import BackendAdapter
    from snla.executor.spss import SPSSExecutor
    from snla.executor.python import PythonStatsExecutor

    spss_exec = SPSSExecutor(output_dir=output_dir)
    # Override exec mode after construction (SSPSExecutor reads from env by default)
    spss_exec.exec_mode = exec_mode
    py_exec = PythonStatsExecutor()
    return BackendAdapter(spss_executor=spss_exec, python_executor=py_exec, output_dir=output_dir)


# ===================================================================
# Boundary data pre-processing
# ===================================================================


def _prepare_data_copy(
    original_path: Path,
    params: dict[str, Any],
    tmpdir: str,
) -> Path:
    """Create a modified copy of the data file for boundary test cases.

    Handles:
    - ``subset_size``: Take only the first N rows.
    - ``introduce_missing`` + ``missing_fraction``: Randomly set a fraction
      of values to NaN in all numeric columns.

    Args:
        original_path: Absolute path to the original ``.sav`` file.
        params: Test case ``params`` dict.
        tmpdir: Directory for temporary files.

    Returns:
        Path to the (possibly modified) data file.
    """
    if not original_path.suffix.lower() == ".sav":
        return original_path

    subset_size = params.get("subset_size")
    introduce_missing = params.get("introduce_missing", False)
    missing_fraction = params.get("missing_fraction", 0.0)

    if not subset_size and not introduce_missing:
        return original_path

    try:
        import pyreadstat

        df, meta = pyreadstat.read_sav(str(original_path))
    except Exception:
        return original_path  # fallback — execute as-is

    # Subset
    if subset_size:
        df = df.iloc[: int(subset_size)]

    # Introduce missing
    if introduce_missing and missing_fraction > 0:
        import numpy as np

        rng = np.random.default_rng(42)  # deterministic for reproducibility
        numeric_cols = df.select_dtypes(include="number").columns
        for col in numeric_cols:
            mask = rng.random(len(df)) < float(missing_fraction)
            df.loc[mask, col] = np.nan

    # Write temp .sav
    dest = Path(tmpdir) / f"boundary_{original_path.stem}.sav"
    try:
        import pyreadstat

        pyreadstat.write_sav(df, str(dest))
    except Exception:
        # pyreadstat write may fail with object columns; fall back to csv
        dest = Path(tmpdir) / f"boundary_{original_path.stem}.csv"
        df.to_csv(dest, index=False)

    return dest


# ===================================================================
# Execution with timeout
# ===================================================================


def _run_with_timeout(func, *args, timeout: int = DEFAULT_TIMEOUT, **kwargs):
    """Execute *func* in a thread pool with a hard timeout.

    Returns:
        Tuple of ``(result, error_string)``.  Exactly one is non-None.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(func, *args, **kwargs)
        try:
            result = future.result(timeout=timeout)
            return result, None
        except FutureTimeoutError:
            return None, f"Timed out after {timeout}s"
        except Exception as exc:
            return None, str(exc)


# ===================================================================
# Statistics comparison
# ===================================================================


def _compare_stats(
    spss_stats: dict[str, Any] | None,
    py_stats: dict[str, Any] | None,
    alpha: float,
) -> dict[str, Any]:
    """Compare normalised statistics from SPSS and Python backends.

    Detects conclusion conflicts (p-values crossing *alpha* threshold)
    and computes per-metric differences.

    Returns a dict of comparison fields ready for JSON output.
    """
    cmp: dict[str, Any] = {}

    spss_p = _safe_float(spss_stats.get("p_value")) if spss_stats else None
    py_p = _safe_float(py_stats.get("p_value")) if py_stats else None

    cmp["p_diff"] = _diff(spss_p, py_p)

    # Determine test statistic key
    test_key = None
    for candidate in ("t_value", "f_value", "chi_square", "r", "u_statistic", "h_statistic"):
        if spss_stats and candidate in spss_stats:
            test_key = candidate
            break
        if py_stats and candidate in py_stats:
            test_key = candidate
            break
    if test_key:
        spss_ts = _safe_float(spss_stats.get(test_key)) if spss_stats else None
        py_ts = _safe_float(py_stats.get(test_key)) if py_stats else None
        cmp["test_statistic_key"] = test_key
        cmp["test_statistic_diff_rel"] = _ratio(spss_ts, py_ts)

    # Degrees of freedom
    spss_df = _safe_float(spss_stats.get("df")) if spss_stats else None
    py_df = _safe_float(py_stats.get("df")) if py_stats else None
    cmp["df_diff"] = _diff(spss_df, py_df)

    # Effect size
    spss_es = _safe_float(spss_stats.get("effect_size")) if spss_stats else None
    py_es = _safe_float(py_stats.get("effect_size")) if py_stats else None
    cmp["effect_size_ratio"] = _ratio(spss_es, py_es)

    # n_valid
    spss_n = spss_stats.get("n_valid") if spss_stats else None
    py_n = py_stats.get("n_valid") if py_stats else None
    cmp["n_valid_spss"] = spss_n
    cmp["n_valid_py"] = py_n

    # Conclusion conflict detection
    if spss_p is not None and py_p is not None:
        spss_sig = spss_p < alpha
        py_sig = py_p < alpha
        cmp["conclusion_conflict"] = spss_sig != py_sig
    else:
        cmp["conclusion_conflict"] = False

    return cmp


# ===================================================================
# Single case runner
# ===================================================================


def _run_single_case(
    adapter: Any,
    test_case: dict[str, Any],
    tmpdir: str,
    alpha: float,
    backend_filter: str | None,
) -> dict[str, Any]:
    """Execute one test case on the requested backends and compare.

    Args:
        adapter: ``BackendAdapter`` instance.
        test_case: YAML test case dict.
        tmpdir: Directory for temporary boundary data files.
        alpha: Significance threshold.
        backend_filter: ``None``, ``"spss"``, or ``"python"``.

    Returns:
        Comparison result dict (one entry in the ``results`` array).
    """
    case_id = test_case.get("id", "unknown")
    method = test_case.get("method", "")
    data_file_rel = test_case.get("data_file", "")
    original_path = _resolve_data_path(data_file_rel)
    params = test_case.get("params", {}) or {}

    # ------------------------------------------------------------------
    # Prepare data (handle boundary params)
    # ------------------------------------------------------------------
    data_path = _prepare_data_copy(original_path, params, tmpdir)

    # ------------------------------------------------------------------
    # Build kwargs for BackendAdapter.run_spss / run_python
    # ------------------------------------------------------------------
    kwargs: dict[str, Any] = {}
    for key in ("grouping_var", "test_var", "dep_var", "indep_var", "var1", "var2", "groups"):
        if key in test_case and test_case[key] is not None:
            kwargs[key] = test_case[key]

    # Handle groups: YAML has list, adapter expects tuple
    if "groups" in kwargs and isinstance(kwargs["groups"], list):
        kwargs["groups"] = tuple(kwargs["groups"])

    # Pass params dict (minus boundary keys that _prepare_data_copy consumed)
    clean_params = {
        k: v
        for k, v in params.items()
        if k not in ("subset_size", "introduce_missing", "missing_fraction", "exec_mode")
    }
    if clean_params:
        kwargs["params"] = clean_params

    # ------------------------------------------------------------------
    # Determine which backends to run
    # ------------------------------------------------------------------
    run_spss = backend_filter is None or backend_filter == "spss"
    run_py = backend_filter is None or backend_filter == "python"

    # SPSS execution
    spss_result = None
    spss_success = False
    spss_stats = None
    spss_duration = 0.0
    spss_error = None

    if run_spss:
        # Check if we need a batch-mode adapter
        exec_mode = params.get("exec_mode", "python")
        if exec_mode != "python":
            batch_output_dir = os.path.join(tmpdir, "spss_batch_output")
            os.makedirs(batch_output_dir, exist_ok=True)
            batch_adapter = _create_adapter(batch_output_dir, exec_mode=exec_mode)
            exec_adapter = batch_adapter
        else:
            exec_adapter = adapter

        t0 = time.perf_counter()
        spss_result, spss_error = _run_with_timeout(
            exec_adapter.run_spss,
            method=method,
            data_path=str(data_path),
            **kwargs,
            timeout=DEFAULT_TIMEOUT,
        )
        spss_duration = round(time.perf_counter() - t0, 3)

        if spss_error:
            spss_success = False
        elif spss_result and spss_result.parser_used != "adapter_error":
            spss_success = True
            spss_stats = exec_adapter.extract_comparable_stats(spss_result)
        else:
            spss_success = False
            spss_error = "; ".join(spss_result.notes) if spss_result else "Unknown error"

    # Python execution
    py_result = None
    py_success = False
    py_stats = None
    py_duration = 0.0
    py_error = None

    if run_py:
        t0 = time.perf_counter()
        py_result, py_error = _run_with_timeout(
            adapter.run_python,
            method=method,
            data_path=str(data_path),
            **kwargs,
            timeout=DEFAULT_TIMEOUT,
        )
        py_duration = round(time.perf_counter() - t0, 3)

        if py_error:
            py_success = False
        elif py_result and py_result.parser_used != "adapter_error":
            py_success = True
            py_stats = adapter.extract_comparable_stats(py_result)
        else:
            py_success = False
            py_error = "; ".join(py_result.notes) if py_result else "Unknown error"

    # ------------------------------------------------------------------
    # Compare
    # ------------------------------------------------------------------
    both_ok = spss_success and py_success
    comparison = {}
    if both_ok:
        comparison = _compare_stats(spss_stats, py_stats, alpha)

    # Determine notes
    notes_parts: list[str] = []
    if spss_error:
        notes_parts.append(f"[SPSS] {spss_error}")
    if py_error:
        notes_parts.append(f"[Python] {py_error}")

    # Progress indicator (ASCII-safe for Windows consoles)
    status_spss = "OK" if spss_success else ("FAIL" if spss_error else "SKIP")
    status_py = "OK" if py_success else ("FAIL" if py_error else "SKIP")

    return {
        "id": case_id,
        "method": method,
        "description": test_case.get("description", ""),
        "data_file": data_file_rel,
        "spss_success": spss_success,
        "py_success": py_success,
        "spss_stats": spss_stats,
        "py_stats": py_stats,
        "p_diff": comparison.get("p_diff"),
        "effect_size_ratio": comparison.get("effect_size_ratio"),
        "df_diff": comparison.get("df_diff"),
        "conclusion_conflict": comparison.get("conclusion_conflict", False),
        "test_statistic_key": comparison.get("test_statistic_key"),
        "test_statistic_diff_rel": comparison.get("test_statistic_diff_rel"),
        "n_valid_spss": comparison.get("n_valid_spss"),
        "n_valid_py": comparison.get("n_valid_py"),
        "notes": " | ".join(notes_parts) if notes_parts else "",
        "duration_spss_s": spss_duration,
        "duration_py_s": py_duration,
        "_progress_spss": status_spss,
        "_progress_py": status_py,
    }


# ===================================================================
# Method trust computation
# ===================================================================


def _compute_method_trust(
    results: list[dict[str, Any]],
    alpha: float,
    trust_threshold: float,
) -> dict[str, Any]:
    """Compute per-method trust scores from comparison results.

    Only considers cases where both backends succeeded.
    """
    by_method: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"tested": 0, "conflicts": 0, "failed": 0, "total": 0}
    )

    for r in results:
        m = r["method"]
        by_method[m]["total"] += 1
        if not r["spss_success"] and not r["py_success"]:
            by_method[m]["failed"] += 1
        elif not r["spss_success"] or not r["py_success"]:
            by_method[m]["failed"] += 1
        else:
            by_method[m]["tested"] += 1
            if r.get("conclusion_conflict"):
                by_method[m]["conflicts"] += 1

    methods_out: dict[str, dict[str, Any]] = {}
    for method, stats in sorted(by_method.items()):
        tested = stats["tested"]
        conflicts = stats["conflicts"]
        rate = conflicts / tested if tested > 0 else 0.0
        trusted = (1.0 - rate) >= trust_threshold if tested > 0 else False
        methods_out[method] = {
            "trusted": trusted,
            "conflict_rate": round(rate, 4),
            "cases_tested": tested,
            "cases_failed": stats["failed"],
            "cases_total": stats["total"],
            "conflicts": conflicts,
        }

    return methods_out


# ===================================================================
# Console output
# ===================================================================


def _print_summary(
    results: list[dict[str, Any]],
    alpha: float,
    trust_threshold: float,
    method_trust: dict[str, Any],
) -> None:
    """Print a formatted summary table to stdout."""
    separator = "=" * 60
    print(f"\n{separator}")
    print("P5-3 Backend Comparison Results")
    print(separator)
    print(f"Alpha threshold: {alpha} | Trust threshold: {trust_threshold}")
    print()
    print(
        f"{'Method':<28} {'Cases':>5}  {'Failed':>6}  {'Conflicts':>9}  {'Rate':>6}  {'Trusted':>7}"
    )
    print(f"{'—' * 28}  {'—' * 5}  {'—' * 6}  {'—' * 9}  {'—' * 6}  {'—' * 7}")

    total_cases = 0
    total_failed = 0
    total_conflicts = 0
    total_tested = 0

    for method in sorted(method_trust):
        info = method_trust[method]
        total_cases += info["cases_total"]
        total_failed += info["cases_failed"]
        conflicts_for_method = info.get("conflicts", 0)
        total_conflicts += conflicts_for_method

        tested_both = sum(
            1 for r in results if r["method"] == method and r["spss_success"] and r["py_success"]
        )
        total_tested += tested_both

        rate_str = f"{info['conflict_rate']:.3f}" if info["cases_tested"] > 0 else "N/A"
        trusted_str = "YES" if info["trusted"] else ("NO" if info["cases_tested"] > 0 else "—")
        print(
            f"{method:<28} {info['cases_total']:>5}  {info['cases_failed']:>6}  "
            f"{conflicts_for_method:>9}  {rate_str:>6}  {trusted_str:>7}"
        )

    print(f"{'—' * 28}  {'—' * 5}  {'—' * 6}  {'—' * 9}  {'—' * 6}  {'—' * 7}")
    overall_rate = total_conflicts / total_tested if total_tested > 0 else 0.0
    print(
        f"{'TOTAL':<28} {total_cases:>5}  {total_failed:>6}  "
        f"{total_conflicts:>9}  {overall_rate:.3f}"
    )

    trusted = [m for m, i in method_trust.items() if i["trusted"]]
    not_trusted = [m for m, i in method_trust.items() if not i["trusted"] and i["cases_tested"] > 0]
    untestable = [m for m, i in method_trust.items() if i["cases_tested"] == 0]

    print()
    if trusted:
        print(f"Trusted methods ({len(trusted)}/{len(method_trust)}): {', '.join(trusted)}")
    if not_trusted:
        print(f"Not trusted ({len(not_trusted)}/{len(method_trust)}): {', '.join(not_trusted)}")
    if untestable:
        print(f"Untestable ({len(untestable)}): {', '.join(untestable)}")

    print()
    print("Outputs: p0_output/backend_comparison.json, p0_output/method_trust.json")
    print(separator)


# ===================================================================
# JSON output helpers
# ===================================================================


def _save_json(filepath: Path, data: dict[str, Any]) -> None:
    """Write a dict to a pretty-printed JSON file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
    print(f"  → {filepath}")


def _build_comparison_json(
    results: list[dict[str, Any]],
    alpha: float,
    spss_available: bool,
    python_available: bool,
) -> dict[str, Any]:
    """Build the ``backend_comparison.json`` payload."""
    spss_failed = sum(1 for r in results if not r["spss_success"])
    py_failed = sum(1 for r in results if not r["py_success"])
    both_ok = sum(1 for r in results if r["spss_success"] and r["py_success"])
    conflicts = sum(1 for r in results if r.get("conclusion_conflict"))

    clean_results: list[dict[str, Any]] = []
    for r in results:
        entry = {k: v for k, v in r.items() if not k.startswith("_")}
        clean_results.append(entry)

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "alpha_threshold": alpha,
            "total_cases": len(results),
            "spss_available": spss_available,
            "python_available": python_available,
            "spss_failed": spss_failed,
            "py_failed": py_failed,
            "both_succeeded": both_ok,
            "conclusion_conflicts": conflicts,
            "total_conflict_rate": round(conflicts / both_ok, 4) if both_ok > 0 else None,
        },
        "results": clean_results,
    }


def _build_trust_json(
    method_trust: dict[str, Any],
    alpha: float,
    trust_threshold: float,
) -> dict[str, Any]:
    """Build the ``method_trust.json`` payload."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "alpha_threshold": alpha,
        "trust_threshold": trust_threshold,
        "methods": method_trust,
    }


# ===================================================================
# Main
# ===================================================================


def main() -> None:
    parser = argparse.ArgumentParser(
        description="P5-3 Backend Comparison: SPSS vs Python (pingouin) validation",
    )
    parser.add_argument(
        "--methods",
        type=str,
        default=None,
        help="Comma-separated method filter (e.g., ttest,anova,correlation)",
    )
    parser.add_argument(
        "--ids",
        type=str,
        default=None,
        help="Comma-separated test case IDs (e.g., ttest_independent_001,anova_oneway_001)",
    )
    parser.add_argument(
        "--backend",
        type=str,
        choices=["spss", "python"],
        default=None,
        help="Run only one backend (default: both)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available test cases and exit",
    )
    parser.add_argument(
        "--alpha",
        type=float,
        default=0.05,
        help="Significance threshold for conclusion conflict detection (default: 0.05)",
    )
    parser.add_argument(
        "--trust-threshold",
        type=float,
        default=DEFAULT_TRUST_THRESHOLD,
        help=f"Minimum (1 - conflict_rate) to trust a method (default: {DEFAULT_TRUST_THRESHOLD})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Max seconds per test case (default: {DEFAULT_TIMEOUT})",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # --list mode
    # ------------------------------------------------------------------
    if args.list:
        methods_filter = args.methods.split(",") if args.methods else None
        ids_filter = args.ids.split(",") if args.ids else None
        cases = load_test_cases(YAML_PATH, methods_filter=methods_filter, ids_filter=ids_filter)
        print(f"Test cases ({len(cases)}):")
        print(f"{'ID':<35} {'Method':<25} {'Description'}")
        print(f"{'—' * 35} {'—' * 25} {'—' * 40}")
        for c in cases:
            print(f"{c['id']:<35} {c['method']:<25} {c.get('description', '')}")
        return

    # ------------------------------------------------------------------
    # Backend availability checks
    # ------------------------------------------------------------------
    spss_available = _check_spss_available() if args.backend != "python" else True
    python_available = True  # pingouin/pandas are always importable

    if not spss_available and args.backend is None:
        print(
            "[WARNING] SPSS not detected (stats.exe not found). Use --backend python to skip SPSS.",
            file=sys.stderr,
        )
        print("[WARNING] Will attempt SPSS execution anyway — expect failures.\n", file=sys.stderr)
    elif not spss_available and args.backend == "spss":
        print("[ERROR] SPSS not available but --backend spss was requested.", file=sys.stderr)
        print(f"  Checked: {__import__('snla.config').SPSS_EXECUTABLE}", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------------------
    # Load test cases
    # ------------------------------------------------------------------
    methods_filter = args.methods.split(",") if args.methods else None
    ids_filter = args.ids.split(",") if args.ids else None
    test_cases = load_test_cases(YAML_PATH, methods_filter=methods_filter, ids_filter=ids_filter)

    if not test_cases:
        print("[ERROR] No test cases matched the filter.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(test_cases)} test case(s).")
    print(f"Backend filter: {args.backend or 'both'}")
    print(f"Alpha threshold: {args.alpha}")
    print()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    adapter = None
    if args.backend != "python":
        try:
            adapter = _create_adapter(str(OUTPUT_DIR))
        except Exception as exc:
            if args.backend == "spss":
                print(f"[ERROR] Failed to create SPSS adapter: {exc}", file=sys.stderr)
                sys.exit(1)
            else:
                print(f"[WARNING] Failed to create SPSS adapter: {exc}", file=sys.stderr)
                print("[WARNING] Continuing with Python backend only.\n", file=sys.stderr)
                spss_available = False

    if args.backend == "python" or not spss_available:
        # Python-only: create adapter with no SPSS
        from snla.executor.adapter import BackendAdapter
        from snla.executor.python import PythonStatsExecutor

        adapter = BackendAdapter(spss_executor=None, python_executor=PythonStatsExecutor())

    # ------------------------------------------------------------------
    # Create temp dir for boundary data copies
    # ------------------------------------------------------------------
    tmpdir_obj = tempfile.mkdtemp(prefix="snla_compare_")
    try:
        # ------------------------------------------------------------------
        # Execute test cases
        # ------------------------------------------------------------------
        results: list[dict[str, Any]] = []
        total = len(test_cases)
        interrupted = False

        for idx, tc in enumerate(test_cases, 1):
            case_id = tc.get("id", "unknown")

            try:
                entry = _run_single_case(
                    adapter=adapter,
                    test_case=tc,
                    tmpdir=tmpdir_obj,
                    alpha=args.alpha,
                    backend_filter=args.backend,
                )
            except KeyboardInterrupt:
                interrupted = True
                print(f"\n[INTERRUPTED] Saving partial results ({idx - 1}/{total})...")
                break
            except Exception as exc:
                entry = {
                    "id": case_id,
                    "method": tc.get("method", ""),
                    "description": tc.get("description", ""),
                    "data_file": tc.get("data_file", ""),
                    "spss_success": False,
                    "py_success": False,
                    "spss_stats": None,
                    "py_stats": None,
                    "p_diff": None,
                    "effect_size_ratio": None,
                    "df_diff": None,
                    "conclusion_conflict": False,
                    "notes": f"Script error: {exc}",
                    "duration_spss_s": 0,
                    "duration_py_s": 0,
                    "_progress_spss": "FAIL",
                    "_progress_py": "FAIL",
                }

            results.append(entry)

            # Print progress
            spss = entry["_progress_spss"]
            py = entry["_progress_py"]
            d_spss = f"({entry['duration_spss_s']:.1f}s)" if entry["spss_success"] else ""
            d_py = f"({entry['duration_py_s']:.1f}s)" if entry["py_success"] else ""
            conflict_marker = " **CONFLICT**" if entry.get("conclusion_conflict") else ""
            print(
                f"[{idx}/{total}] {case_id}: SPSS {spss} {d_spss} | Python {py} {d_py}{conflict_marker}"
            )

        # ------------------------------------------------------------------
        # Compute method trust
        # ------------------------------------------------------------------
        method_trust = _compute_method_trust(results, args.alpha, args.trust_threshold)

        # ------------------------------------------------------------------
        # Build & save outputs
        # ------------------------------------------------------------------
        comparison_json = _build_comparison_json(
            results,
            args.alpha,
            spss_available,
            python_available,
        )
        trust_json = _build_trust_json(method_trust, args.alpha, args.trust_threshold)

        _save_json(OUTPUT_DIR / "backend_comparison.json", comparison_json)
        _save_json(OUTPUT_DIR / "method_trust.json", trust_json)

        # ------------------------------------------------------------------
        # Print summary
        # ------------------------------------------------------------------
        _print_summary(results, args.alpha, args.trust_threshold, method_trust)

        if interrupted:
            print("\n[WARNING] Execution was interrupted. Partial results saved.")
            sys.exit(2)

    finally:
        # Cleanup temp directory
        try:
            shutil.rmtree(tmpdir_obj, ignore_errors=True)
        except Exception:
            pass

        # Adapter cleanup
        if adapter is not None:
            try:
                adapter.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    main()
