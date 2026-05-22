"""
Backend Adapter — unified bridge for SPSS vs Python comparison (P5-3).

Provides ``BackendAdapter`` which wraps both ``SPSSExecutor`` and
``PythonStatsExecutor`` behind a single ``run_spss`` / ``run_python``
interface.  Both methods return identically-structured ``AnalysisResult``
objects, enabling direct apples-to-apples statistical comparison.

Usage::

    from snla.executor.spss import SPSSExecutor
    from snla.executor.python import PythonStatsExecutor
    from snla.executor.adapter import BackendAdapter

    adapter = BackendAdapter(spss_executor=spss, python_executor=py)
    result_spss  = adapter.run_spss("independent_t_test", "data.sav",
                                     group_var="gender", test_var="score",
                                     groups=(1, 2))
    result_py    = adapter.run_python("independent_t_test", "data.sav",
                                       group_var="gender", test_var="score",
                                       groups=(1, 2))
    comparison   = adapter.extract_comparable_stats(result_spss)
"""

from __future__ import annotations

import os
from typing import Any

import pandas as pd

from snla.parser.output import parse as _parse_output
from snla.parser.schema import AnalysisResult
from snla.syntax.templates import get_syntax_by_method

# ---------------------------------------------------------------------------
# Method name aliases — canonicalise between template keys and Python
# dispatch keys so that both "pearson_correlation" and "correlations"
# work identically in either backend.
# ---------------------------------------------------------------------------

METHOD_ALIASES: dict[str, str] = {
    # "user_facing_name" → "canonical_name"
    # (canonical keys must match TEMPLATE_MAP in snla/syntax/templates.py)
    "pearson_correlation": "correlations",
    "correlations": "correlations",
    "chi_square": "chi_square",
    "crosstabs": "chi_square",
}
"""Normalise alternate method names to a single canonical key.

The YAML test cases use the canonical keys from ``TEMPLATE_MAP``, but
some methods have legacy aliases (e.g. ``"pearson_correlation"`` is
also known as ``"correlations"``).  This map ensures both variants
resolve correctly regardless of backend.
"""

# ---------------------------------------------------------------------------
# Method → SPSS analysis type (for parser hint)
# ---------------------------------------------------------------------------

_ANALYSIS_TYPE_MAP: dict[str, str] = {
    "independent_t_test": "T-TEST",
    "paired_t_test": "T-TEST",
    "oneway_anova": "ANOVA",
    "simple_regression": "REGRESSION",
    "chi_square": "CROSSTABS",
    "crosstabs": "CROSSTABS",
    "correlations": "CORRELATIONS",
    "pearson_correlation": "CORRELATIONS",
    "spearman_correlation": "CORRELATIONS",
    "frequencies": "FREQUENCIES",
    "descriptives": "DESCRIPTIVES",
    "mann_whitney_u": "UNKNOWN",
    "kruskal_wallis": "UNKNOWN",
}

# ---------------------------------------------------------------------------
# Template kwarg → Python kwarg translation
# ---------------------------------------------------------------------------
# Templates use names like ``group_var``, ``row_var``, ``col_var``, ``var``.
# Python execute() expects ``grouping_var``, ``test_var``, etc.
# This map translates the template naming convention to Python naming.


def _translate_kwargs(method: str, **kwargs: Any) -> dict[str, Any]:
    """Translate template-style kwargs to Python-executor-style kwargs.

    The key difference:
    - Templates: ``group_var``, ``test_var``, ``row_var``, ``col_var``, ``var``
    - Python dispatch: ``grouping_var``, ``test_var``, ``var1``, ``var2``, etc.

    This function normalises so that both backends receive the same
    parameter names.
    """
    result: dict[str, Any] = {}

    # --- grouping variable ---
    for key in ("grouping_var", "group_var", "row_var"):
        if key in kwargs:
            result["grouping_var"] = kwargs[key]
            break

    # --- test variable ---
    for key in ("test_var", "col_var", "var", "dep_var"):
        if key in kwargs and key not in ("grouping_var",):
            result["test_var"] = kwargs[key]
            break

    # --- dependent / independent (regression) ---
    if "dep_var" in kwargs:
        result["dep_var"] = kwargs["dep_var"]
    if "indep_var" in kwargs:
        result["indep_var"] = kwargs["indep_var"]

    # --- paired variables ---
    if "var1" in kwargs:
        result["var1"] = kwargs["var1"]
    if "var2" in kwargs:
        result["var2"] = kwargs["var2"]

    # --- groups tuple ---
    if "groups" in kwargs:
        result["groups"] = kwargs["groups"]
    elif method in ("independent_t_test", "mann_whitney_u"):
        result["groups"] = (1, 2)

    return result


# ===================================================================
# BackendAdapter
# ===================================================================


class BackendAdapter:
    """Unified adapter that bridges SPSS and Python statistical backends.

    Both ``run_spss`` and ``run_python`` return ``AnalysisResult``
    objects with identical shape, enabling direct comparison for
    P5-3 validation.

    Parameters:
        spss_executor:
            An already-configured ``SPSSExecutor`` instance, or ``None``
            to construct one lazily from environment settings.
        python_executor:
            An already-configured ``PythonStatsExecutor`` instance, or
            ``None`` to construct one lazily.
        output_dir:
            Directory for SPSS output artifacts.  Only used if
            *spss_executor* is ``None``.
    """

    def __init__(
        self,
        spss_executor: Any = None,
        python_executor: Any = None,
        output_dir: str | None = None,
    ) -> None:
        self._spss = spss_executor
        self._python = python_executor
        self._output_dir = output_dir

    # -- Lazy executors ---------------------------------------------------

    @property
    def spss(self) -> Any:
        if self._spss is None:
            from snla.executor.spss import SPSSExecutor

            self._spss = SPSSExecutor(output_dir=self._output_dir)
        return self._spss

    @property
    def python(self) -> Any:
        if self._python is None:
            from snla.executor.python import PythonStatsExecutor

            self._python = PythonStatsExecutor()
        return self._python

    # -- Public API -------------------------------------------------------

    def run_spss(self, method: str, data_path: str, **kwargs: Any) -> AnalysisResult:
        """Execute an analysis via SPSS backend.

        Full pipeline:
        1. Resolve method → template function via ``get_syntax_by_method``
        2. Generate SPSS syntax from template + kwargs
        3. Execute via ``SPSSExecutor.run(syntax, data_path)``
        4. Parse OMS XML via ``snla.parser.output.parse``
        5. Return ``AnalysisResult``

        Args:
            method: Analysis method key (e.g. ``"independent_t_test"``).
            data_path: Path to the ``.sav`` file.
            **kwargs: Variable parameters matching the template signature
                (``group_var``, ``test_var``, ``groups``, ``var1``,
                ``var2``, ``dep_var``, ``indep_var``, ``row_var``,
                ``col_var``, ``var``, and optional ``params`` dict).

        Returns:
            ``AnalysisResult`` with ``parser_used`` set to one of
            ``"oms_xml"``, ``"regex_lst"``, or ``"adapter_error"``.
        """
        canonical = _canonical_method(method)

        # --- Handle ``params`` dict if provided ---
        extra_params: dict[str, Any] = {}
        if "params" in kwargs and isinstance(kwargs["params"], dict):
            extra_params = kwargs.pop("params")

        # --- 1. Build template kwargs & generate syntax ---
        template_kwargs = _build_template_kwargs(canonical, {**kwargs, **extra_params})
        try:
            syntax = get_syntax_by_method(canonical, **template_kwargs)
        except (ValueError, TypeError) as exc:
            return AnalysisResult(
                analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
                notes=[f"SPSS template failed for '{canonical}': {exc}"],
                parser_used="adapter_error",
            )

        # --- 2. Execute via SPSS ---
        try:
            exec_result = self.spss.run(syntax, data_path)
        except Exception as exc:
            return AnalysisResult(
                analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
                notes=[f"SPSS execution failed: {exc}"],
                parser_used="adapter_error",
            )

        # --- 3. Parse OMS XML output ---
        if exec_result.xml_path and os.path.isfile(exec_result.xml_path):
            try:
                result = _parse_output(
                    oms_xml_path=exec_result.xml_path,
                    analysis_type=_ANALYSIS_TYPE_MAP.get(canonical),
                )
                # If parser left n_valid at 0, try to infer from stats
                if result.n_valid == 0 and "n_valid" in result.statistics:
                    result.n_valid = int(result.statistics["n_valid"])
                return result
            except Exception as exc:
                return AnalysisResult(
                    analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
                    notes=[f"SPSS parsing failed: {exc}"],
                    statistics={"n_valid": 0},
                    raw_output_path=exec_result.xml_path,
                    parser_used="adapter_error",
                )

        # --- 4. No XML produced — try LST fallback ---
        lst_text = None
        if exec_result.lst_path and os.path.isfile(exec_result.lst_path):
            try:
                with open(exec_result.lst_path, "r", encoding="utf-8", errors="replace") as f:
                    lst_text = f.read()
            except OSError:
                pass

        if not lst_text and exec_result.stdout.strip():
            lst_text = exec_result.stdout

        if lst_text:
            analysis_type = _ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN")
            try:
                result = _parse_output(
                    lst_text=lst_text,
                    analysis_type=analysis_type,
                )
                return result
            except Exception as exc:
                return AnalysisResult(
                    analysis_type=analysis_type,
                    notes=[f"LST parsing failed: {exc}"],
                    parser_used="adapter_error",
                )

        # --- Complete failure ---
        return AnalysisResult(
            analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
            notes=[
                "SPSS produced no parseable output — "
                f"error: {exec_result.error_message or 'unknown'}",
            ],
            statistics={"n_valid": 0},
            parser_used="adapter_error",
        )

    def run_python(self, method: str, data_path: str, **kwargs: Any) -> AnalysisResult:
        """Execute an analysis via Python (pingouin) backend.

        Full pipeline:
        1. Load ``.sav`` / ``.csv`` data as ``pd.DataFrame``
        2. Translate kwargs to Python executor names
        3. Execute via ``PythonStatsExecutor.execute(method, data=df, ...)``
        4. Return ``AnalysisResult``

        Args:
            method: Analysis method key (e.g. ``"independent_t_test"``).
            data_path: Path to ``.sav`` (or ``.csv``) data file.
            **kwargs: Variable parameters (``group_var``, ``test_var``,
                ``groups``, ``var1``, ``var2``, ``dep_var``, ``indep_var``,
                ``row_var``, ``col_var``, ``var``, and optional ``params``
                dict).

        Returns:
            ``AnalysisResult`` with ``parser_used="python_pingouin"``.
        """
        canonical = _canonical_method(method)

        # --- Handle ``params`` dict ---
        extra_params: dict[str, Any] = {}
        if "params" in kwargs and isinstance(kwargs["params"], dict):
            extra_params = kwargs.pop("params")

        merged = {**kwargs, **extra_params}

        # --- 1. Load data ---
        try:
            df = _load_data(data_path)
        except Exception as exc:
            return AnalysisResult(
                analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
                notes=[f"Python backend: data load failed — {exc}"],
                parser_used="adapter_error",
            )

        # --- 2. Translate kwargs & execute ---
        python_kwargs = _translate_kwargs(canonical, **merged)

        try:
            result = self.python.execute(
                method=canonical,
                data=df,
                **python_kwargs,
            )
        except Exception as exc:
            return AnalysisResult(
                analysis_type=_ANALYSIS_TYPE_MAP.get(canonical, "UNKNOWN"),
                notes=[f"Python backend: execution failed — {exc}"],
                parser_used="adapter_error",
            )

        return result

    # -- Stats extraction -------------------------------------------------

    @staticmethod
    def extract_comparable_stats(result: AnalysisResult) -> dict[str, Any]:
        """Extract a flat dictionary of comparable statistics from an
        ``AnalysisResult``, normalised across backends.

        Returns a dict with common keys like ``p_value``, ``t_value``,
        ``f_value``, ``r``, ``chi_square``, ``df``, ``n_valid`` — and
        a backend-identifying ``backend`` key.

        Args:
            result: Either an SPSS-parsed or Python-pingouin result.

        Returns:
            Flat dict of comparable statistics.  Missing keys are omitted.
        """
        stats: dict[str, Any] = result.statistics.copy()
        comparable: dict[str, Any] = {}

        # --- Identify backend ---
        comparable["backend"] = result.parser_used

        # --- p-value (universal) ---
        for key in ("p_value", "p_val", "p"):
            if key in stats and stats[key] is not None:
                comparable["p_value"] = float(stats[key])
                break

        # --- t-value ---
        if "t_value" in stats or "t" in stats:
            comparable["t_value"] = float(stats.get("t_value", stats.get("t", 0)))

        # --- F-value ---
        if "f_value" in stats or "f" in stats or "F" in stats:
            comparable["f_value"] = float(stats.get("f_value", stats.get("f", stats.get("F", 0))))

        # --- Chi-square ---
        if "chi_square" in stats or "chi2" in stats:
            comparable["chi_square"] = float(stats.get("chi_square", stats.get("chi2", 0)))

        # --- correlation coefficient ---
        if "r" in stats:
            comparable["r"] = float(stats["r"])

        # --- R-squared ---
        if "r_squared" in stats:
            comparable["r_squared"] = float(stats["r_squared"])

        # --- degrees of freedom ---
        if "df" in stats or "dof" in stats:
            comparable["df"] = float(stats.get("df", stats.get("dof", 0)))

        # --- effect size ---
        for key in ("cohen_d", "eta_sq", "np2", "rbc", "effect_size"):
            if key in stats and stats[key] is not None:
                comparable["effect_size"] = float(stats[key])
                break

        # --- sample size ---
        if "n_valid" in stats and stats["n_valid"]:
            comparable["n_valid"] = int(stats["n_valid"])
        elif result.n_valid:
            comparable["n_valid"] = result.n_valid

        # --- U-statistic (Mann-Whitney) ---
        if "u" in stats or "U_val" in stats:
            comparable["u_statistic"] = float(stats.get("u", stats.get("U_val", 0)))

        # --- H-statistic (Kruskal-Wallis) ---
        if "h" in stats or "H" in stats:
            comparable["h_statistic"] = float(stats.get("h", stats.get("H", 0)))

        # --- mean ---
        if "mean" in stats:
            comparable["mean"] = float(stats["mean"])

        # --- std dev ---
        for key in ("std_dev", "stddev", "sd"):
            if key in stats and stats[key] is not None:
                comparable["std_dev"] = float(stats[key])
                break

        return comparable

    # -- Cleanup ---------------------------------------------------------

    def cleanup(self) -> None:
        """Clean up any SPSS executor temp files."""
        if self._spss is not None:
            try:
                self._spss.cleanup()
            except Exception:
                pass


# ===================================================================
# Internal helpers
# ===================================================================


def _canonical_method(method: str) -> str:
    """Resolve a method name to its canonical form via ``METHOD_ALIASES``."""
    return METHOD_ALIASES.get(method, method)


def _build_template_kwargs(method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract and rename kwargs into the names expected by template functions.

    Different templates expect different parameter names:
        - ``independent_t_test`` → group_var, test_var, groups
        - ``oneway_anova`` → group_var, test_var
        - ``paired_t_test`` → var1, var2
        - ``simple_regression`` → dep_var, indep_var
        - ``chi_square`` / ``crosstabs`` → row_var, col_var
        - ``correlations`` / ``pearson_correlation`` → var1, var2
        - ``spearman_correlation`` → var1, var2
        - ``frequencies`` → var
        - ``descriptives`` → var
        - ``mann_whitney_u`` → group_var, test_var, groups
        - ``kruskal_wallis`` → group_var, test_var
    """
    out: dict[str, Any] = {}

    # -- Grouping variable --
    group_var = kwargs.get("group_var") or kwargs.get("row_var")
    if not group_var:
        group_var = kwargs.get("grouping_var")

    # -- Test variable --
    test_var = kwargs.get("test_var") or kwargs.get("col_var") or kwargs.get("var")
    if not test_var:
        test_var = kwargs.get("dep_var")

    # -- Groups --
    groups = kwargs.get("groups", (1, 2))
    if isinstance(groups, list):
        groups = tuple(groups)

    # -- Dispatch per method --
    if method == "independent_t_test":
        out["group_var"] = group_var or ""
        out["test_var"] = test_var or ""
        out["groups"] = groups
    elif method == "oneway_anova":
        out["group_var"] = group_var or ""
        out["test_var"] = test_var or ""
    elif method == "paired_t_test":
        out["var1"] = kwargs.get("var1", group_var or "")
        out["var2"] = kwargs.get("var2", test_var or "")
    elif method in ("simple_regression",):
        out["dep_var"] = kwargs.get("dep_var", test_var or "")
        out["indep_var"] = kwargs.get("indep_var", group_var or "")
    elif method in ("crosstabs", "chi_square"):
        out["row_var"] = kwargs.get("row_var", group_var or "")
        out["col_var"] = kwargs.get("col_var", test_var or "")
    elif method in ("correlations", "pearson_correlation"):
        out["var1"] = kwargs.get("var1", group_var or "")
        out["var2"] = kwargs.get("var2", test_var or "")
    elif method == "spearman_correlation":
        out["var1"] = kwargs.get("var1", group_var or "")
        out["var2"] = kwargs.get("var2", test_var or "")
    elif method == "frequencies":
        out["var"] = kwargs.get("var", test_var or group_var or "")
    elif method == "descriptives":
        out["var"] = kwargs.get("var", test_var or group_var or "")
    elif method == "mann_whitney_u":
        out["group_var"] = group_var or ""
        out["test_var"] = test_var or ""
        out["groups"] = groups
    elif method == "kruskal_wallis":
        out["group_var"] = group_var or ""
        out["test_var"] = test_var or ""

    return out


def _load_data(data_path: str) -> pd.DataFrame:
    """Load a ``.sav`` or ``.csv`` file into a ``pd.DataFrame``.

    Args:
        data_path: Path to the data file.

    Returns:
        Loaded DataFrame.

    Raises:
        FileNotFoundError: If *data_path* does not exist.
        ValueError: If the file format is not supported.
    """
    if not os.path.isfile(data_path):
        raise FileNotFoundError(f"Data file not found: {data_path}")

    ext = os.path.splitext(data_path)[1].lower()

    if ext == ".sav":
        import pyreadstat

        df, _meta = pyreadstat.read_sav(data_path)
        return df

    if ext == ".csv":
        return pd.read_csv(data_path)

    raise ValueError(
        f"Unsupported data format: '{ext}'. "
        f"Expected .sav or .csv."
    )


__all__ = [
    "BackendAdapter",
    "METHOD_ALIASES",
]
