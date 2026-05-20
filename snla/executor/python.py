"""Python Statistical Backend — pingouin-powered executor.

Provides ``PythonStatsExecutor`` as a drop-in alternative to the SPSS
backend.  All analyses produce ``AnalysisResult`` objects identical in
structure to those from ``SPSSExecutor``, enabling seamless switching
between backends via the router.

Dependencies: pandas, pingouin (already in requirements via numpy/scipy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from snla.parser.schema import AnalysisResult, TableResult


@dataclass
class PythonStatsExecutor:
    """Execute statistical analyses using Python libraries.

    Covers all 12 analysis types in ``snla/syntax/templates.py`` using
    ``pingouin`` for inferential statistics and ``pandas`` for descriptive
    summaries.  Every method returns a fully-populated ``AnalysisResult``.

    Usage::

        executor = PythonStatsExecutor()
        result = executor.execute("independent_t_test", df,
                                   grouping_var="gender",
                                   test_var="score", groups=(1, 2))
    """

    # -- Public API -----------------------------------------------------

    def execute(
        self,
        method: str,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        dep_var: str | None = None,
        indep_var: str | None = None,
        var1: str | None = None,
        var2: str | None = None,
        groups: tuple = (1, 2),
        **kwargs: Any,
    ) -> AnalysisResult:
        """Route *method* to the appropriate private handler.

        Args:
            method: One of the keys in ``TEMPLATE_MAP``.
            data: DataFrame with the loaded dataset.
            All other keyword args are forwarded to the handler.

        Returns:
            ``AnalysisResult`` with ``parser_used="python_pingouin"``.
        """
        dispatch: dict[str, Any] = {
            "independent_t_test": self._ttest_independent,
            "paired_t_test": self._paired_ttest,
            "oneway_anova": self._anova_oneway,
            "simple_regression": self._regression_simple,
            "pearson_correlation": self._correlation_pearson,
            "spearman_correlation": self._correlation_spearman,
            "correlations": self._correlation_pearson,
            "chi_square": self._chi_square,
            "crosstabs": self._chi_square,
            "frequencies": self._frequencies,
            "descriptives": self._descriptives,
            "mann_whitney_u": self._mann_whitney,
            "kruskal_wallis": self._kruskal_wallis,
        }
        handler = dispatch.get(method)
        if handler is None:
            return AnalysisResult(
                analysis_type=method,
                notes=[f"Python backend: method '{method}' not yet implemented"],
                parser_used="python_pingouin",
            )
        return handler(
            data=data, grouping_var=grouping_var, test_var=test_var,
            dep_var=dep_var, indep_var=indep_var,
            var1=var1, var2=var2, groups=groups,
        )

    # ==================================================================
    # T-Test (Independent Samples)
    # ==================================================================

    def _ttest_independent(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        gv = grouping_var or data.columns[0]
        tv = test_var or data.columns[1]
        groups = sorted(data[gv].dropna().unique())

        g1 = data[data[gv] == groups[0]][tv].dropna()
        g2 = data[data[gv] == groups[1]][tv].dropna() if len(groups) > 1 else pd.Series(dtype=float)

        res = pg.ttest(g1, g2, correction="auto")
        t_val = float(res["T"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        dof   = float(res["dof"].iloc[0])
        cd    = float(res.get("cohen_d", pd.Series([0])).iloc[0])
        ci    = res.get("CI95%", pd.Series([[]])).iloc[0]

        return AnalysisResult(
            analysis_type="T-TEST",
            tables=[
                TableResult(title="Group Statistics", rows=[
                    {"group": str(groups[0]), "N": len(g1), "Mean": round(g1.mean(), 4),
                     "StdDev": round(g1.std(ddof=1), 4)},
                    {"group": str(groups[1]) if len(groups) > 1 else "—",
                     "N": len(g2), "Mean": round(g2.mean(), 4) if len(g2) else 0,
                     "StdDev": round(g2.std(ddof=1), 4) if len(g2) else 0},
                ], source_format="python_pingouin"),
                TableResult(title="Independent Samples Test", rows=[
                    {"t": round(t_val, 4), "df": int(dof), "p_value": round(p_val, 4),
                     "Cohen_d": round(cd, 4),
                     "CI95": f"[{ci[0]:.4f}, {ci[1]:.4f}]" if isinstance(ci, (list, tuple)) and len(ci) == 2 else str(ci)}
                ], source_format="python_pingouin"),
            ],
            statistics={"t_value": t_val, "p_value": p_val, "df": int(dof),
                        "cohen_d": cd, "n_valid": len(g1) + len(g2)},
            n_valid=len(g1) + len(g2),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Paired T-Test
    # ==================================================================

    def _paired_ttest(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        var1: str | None = None,
        var2: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        v1 = var1 or grouping_var or data.columns[0]
        v2 = var2 or test_var or data.columns[1]
        d1 = data[v1].dropna()
        d2 = data[v2].dropna()
        # Align lengths
        n = min(len(d1), len(d2))
        d1, d2 = d1.iloc[:n], d2.iloc[:n]

        res = pg.ttest(d1, d2, paired=True)
        t_val = float(res["T"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        dof   = float(res["dof"].iloc[0])
        cd    = float(res.get("cohen_d", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="T-TEST",
            tables=[
                TableResult(title="Paired Samples Statistics", rows=[
                    {"variable": v1, "N": n, "Mean": round(d1.mean(), 4), "StdDev": round(d1.std(ddof=1), 4)},
                    {"variable": v2, "N": n, "Mean": round(d2.mean(), 4), "StdDev": round(d2.std(ddof=1), 4)},
                ], source_format="python_pingouin"),
                TableResult(title="Paired Samples Test", rows=[
                    {"t": round(t_val, 4), "df": int(dof), "p_value": round(p_val, 4),
                     "Cohen_d": round(cd, 4)},
                ], source_format="python_pingouin"),
            ],
            statistics={"t_value": t_val, "p_value": p_val, "df": int(dof), "cohen_d": cd, "n_valid": n},
            n_valid=n,
            parser_used="python_pingouin",
        )

    # ==================================================================
    # One-Way ANOVA
    # ==================================================================

    def _anova_oneway(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        gv = grouping_var or data.columns[0]
        tv = test_var or data.columns[1]
        clean = data[[gv, tv]].dropna()

        res = pg.anova(data=clean, dv=tv, between=gv, detailed=True)
        f_val = float(res["F"].iloc[0])
        p_val = float(res["p_unc"].iloc[0])
        np2   = float(res.get("np2", pd.Series([0])).iloc[0])

        # Group descriptives
        desc = clean.groupby(gv)[tv].agg(["count", "mean", "std"]).reset_index()
        desc_rows = []
        for _, row in desc.iterrows():
            desc_rows.append({
                "group": str(row[gv]), "N": int(row["count"]),
                "Mean": round(row["mean"], 4), "StdDev": round(row["std"], 4),
            })

        return AnalysisResult(
            analysis_type="ANOVA",
            tables=[
                TableResult(title="Descriptives", rows=desc_rows, source_format="python_pingouin"),
                TableResult(title="ANOVA", rows=[
                    {"F": round(f_val, 4), "p_value": round(p_val, 4), "eta_sq": round(np2, 4)},
                ], source_format="python_pingouin"),
            ],
            statistics={"f_value": f_val, "p_value": p_val, "eta_sq": np2, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Simple Linear Regression
    # ==================================================================

    def _regression_simple(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        dep_var: str | None = None,
        indep_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        dep = dep_var or test_var or data.columns[0]
        ind = indep_var or grouping_var or data.columns[1]
        clean = data[[dep, ind]].dropna()

        res = pg.linear_regression(clean[ind], clean[dep])
        r2    = float(res["r2"].iloc[0])
        adj_r2 = float(res["adj_r2"].iloc[0])
        coef_rows = []
        for _, row in res.iterrows():
            coef_rows.append({
                "Predictor": row.get("names", "—"),
                "B": round(float(row["coef"]), 4),
                "SE": round(float(row["se"]), 4),
                "t": round(float(row["T"]), 4),
                "p": round(float(row["pval"]), 4),
            })

        return AnalysisResult(
            analysis_type="REGRESSION",
            tables=[
                TableResult(title="Model Summary", rows=[
                    {"R_squared": round(r2, 4), "Adj_R_squared": round(adj_r2, 4), "N": len(clean)},
                ], source_format="python_pingouin"),
                TableResult(title="Coefficients", rows=coef_rows, source_format="python_pingouin"),
            ],
            statistics={"r_squared": r2, "adj_r_squared": adj_r2, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Pearson / Spearman Correlation
    # ==================================================================

    def _correlation_pearson(
        self, data: pd.DataFrame = None, **__: Any,
    ) -> AnalysisResult:
        return self._correlation(data, method="pearson")

    def _correlation_spearman(
        self, data: pd.DataFrame, **__: Any,
    ) -> AnalysisResult:
        return self._correlation(data, method="spearman")

    def _correlation(
        self, data: pd.DataFrame, method: str = "pearson",
    ) -> AnalysisResult:
        import pingouin as pg

        # Pick two numeric columns
        nums = [c for c in data.columns if pd.api.types.is_numeric_dtype(data[c])
                and c.lower() not in ("id",)]
        v1 = nums[0] if nums else data.columns[0]
        v2 = nums[1] if len(nums) > 1 else data.columns[0]
        clean = data[[v1, v2]].dropna()

        res = pg.corr(clean[v1], clean[v2], method=method)
        r_val = float(res["r"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        n_val = int(res["n"].iloc[0])

        return AnalysisResult(
            analysis_type="CORRELATIONS",
            tables=[
                TableResult(title="Correlations", rows=[
                    {"variables": f"{v1} × {v2}", "r": round(r_val, 4),
                     "p_value": round(p_val, 4), "N": n_val, "method": method},
                ], source_format="python_pingouin"),
            ],
            statistics={"r": r_val, "p_value": p_val, "n_valid": n_val, "method": method},
            n_valid=n_val,
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Chi-Square / Crosstabs
    # ==================================================================

    def _chi_square(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        rv = grouping_var or data.columns[0]
        cv = test_var or data.columns[1]
        clean = data[[rv, cv]].dropna()

        ctab = pd.crosstab(clean[rv], clean[cv])
        expected, observed, res = pg.chi2_independence(clean, rv, cv)
        # Pick Pearson row
        pearson = res[res["test"] == "pearson"]
        if pearson.empty:
            pearson = res.iloc[[0]]
        chi2 = float(pearson["chi2"].iloc[0])
        p_val = float(pearson["pval"].iloc[0])
        dof  = int(pearson["dof"].iloc[0])

        ctab_rows = []
        for idx, row in ctab.iterrows():
            r = {"": str(idx)}
            r.update({str(k): int(v) for k, v in row.items()})
            ctab_rows.append(r)

        return AnalysisResult(
            analysis_type="CROSSTABS",
            tables=[
                TableResult(title="Crosstabulation", rows=ctab_rows, source_format="python_pingouin"),
                TableResult(title="Chi-Square Tests", rows=[
                    {"chi_square": round(chi2, 4), "df": dof, "p_value": round(p_val, 4)},
                ], source_format="python_pingouin"),
            ],
            statistics={"chi_square": chi2, "p_value": p_val, "df": dof, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Frequencies (counts & percentages)
    # ==================================================================

    def _frequencies(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        tv = test_var or grouping_var or data.columns[0]
        counts = data[tv].value_counts(dropna=False).reset_index()
        counts.columns = ["value", "Frequency"]
        total = int(counts["Frequency"].sum())
        counts["Percent"] = (counts["Frequency"] / total * 100).round(2)
        counts["Valid_Percent"] = (counts["Frequency"] / counts["Frequency"].sum() * 100).round(2)
        counts["Cumulative_Percent"] = counts["Percent"].cumsum().round(2)

        rows = []
        for _, r in counts.iterrows():
            rows.append({
                "value": str(r["value"]),
                "Frequency": int(r["Frequency"]),
                "Percent": round(float(r["Percent"]), 2),
                "Valid_Percent": round(float(r["Valid_Percent"]), 2),
                "Cumulative_Percent": round(float(r["Cumulative_Percent"]), 2),
            })

        return AnalysisResult(
            analysis_type="FREQUENCIES",
            tables=[TableResult(title="Frequencies", rows=rows, source_format="python_pingouin")],
            statistics={"n_valid": total, "n_missing": int(len(data) - total)},
            n_valid=total,
            n_missing=int(len(data) - total),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Descriptives (mean, std, min, max)
    # ==================================================================

    def _descriptives(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        tv = test_var or grouping_var or data.columns[0]
        col = data[tv].dropna()
        rows = [{
            "variable": tv,
            "N": len(col),
            "Mean": round(float(col.mean()), 4),
            "StdDev": round(float(col.std(ddof=1)), 4),
            "Min": round(float(col.min()), 4),
            "Max": round(float(col.max()), 4),
            "Median": round(float(col.median()), 4),
            "Skewness": round(float(col.skew()), 4),
            "Kurtosis": round(float(col.kurtosis()), 4),
        }]

        return AnalysisResult(
            analysis_type="DESCRIPTIVES",
            tables=[TableResult(title="Descriptive Statistics", rows=rows, source_format="python_pingouin")],
            statistics={
                "n": len(col), "n_valid": len(col),
                "mean": rows[0]["Mean"], "std_dev": rows[0]["StdDev"],
                "minimum": rows[0]["Min"], "maximum": rows[0]["Max"],
            },
            n_valid=len(col),
            n_missing=int(len(data) - len(col)),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Mann-Whitney U (non-parametric)
    # ==================================================================

    def _mann_whitney(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        gv = grouping_var or data.columns[0]
        tv = test_var or data.columns[1]
        groups = sorted(data[gv].dropna().unique())

        g1 = data[data[gv] == groups[0]][tv].dropna()
        g2 = data[data[gv] == groups[1]][tv].dropna() if len(groups) > 1 else pd.Series(dtype=float)

        res = pg.mwu(g1, g2)
        u_val = float(res["U_val"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        rbc   = float(res.get("RBC", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="MANN_WHITNEY",
            tables=[
                TableResult(title="Mann-Whitney U Test", rows=[
                    {"U": round(u_val, 4), "p_value": round(p_val, 4),
                     "RBC": round(rbc, 4), "N": len(g1) + len(g2)},
                ], source_format="python_pingouin"),
            ],
            statistics={"u": u_val, "p_value": p_val, "rbc": rbc, "n_valid": len(g1) + len(g2)},
            n_valid=len(g1) + len(g2),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Kruskal-Wallis (non-parametric)
    # ==================================================================

    def _kruskal_wallis(
        self,
        data: pd.DataFrame,
        grouping_var: str | None = None,
        test_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        import pingouin as pg

        gv = grouping_var or data.columns[0]
        tv = test_var or data.columns[1]
        clean = data[[gv, tv]].dropna()

        res = pg.kruskal(data=clean, dv=tv, between=gv)
        h_val   = float(res["H"].iloc[0])
        p_val   = float(res["p_unc"].iloc[0])
        eta_sq  = float(res.get("np2", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="KRUSKAL_WALLIS",
            tables=[
                TableResult(title="Kruskal-Wallis Test", rows=[
                    {"H": round(h_val, 4), "p_value": round(p_val, 4),
                     "eta_sq": round(eta_sq, 4), "N": len(clean)},
                ], source_format="python_pingouin"),
            ],
            statistics={"h": h_val, "p_value": p_val, "eta_sq": eta_sq, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )
