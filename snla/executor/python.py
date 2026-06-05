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

    Covers 15 analysis types using ``pingouin`` for inferential statistics
    and ``pandas`` for descriptive summaries.  Every method returns a
    fully-populated ``AnalysisResult``.

    **Available methods**: independent_t_test, paired_t_test, oneway_anova,
    simple_regression, multiple_regression, logistic_regression,
    pearson_correlation, spearman_correlation, correlations, chi_square,
    crosstabs, frequencies, descriptives, mann_whitney_u, kruskal_wallis,
    wilcoxon.

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
            # ── P5/P6 additions ─────────────────────────────────────
            "wilcoxon": self._wilcoxon,
            "multiple_regression": self._multiple_regression,
            "logistic_regression": self._logistic_regression,
        }
        handler = dispatch.get(method)
        if handler is None:
            return AnalysisResult(
                analysis_type=method,
                notes=[f"Python backend: method '{method}' not yet implemented"],
                parser_used="python_pingouin",
            )
        return handler(
            data=data,
            grouping_var=grouping_var,
            test_var=test_var,
            dep_var=dep_var,
            indep_var=indep_var,
            var1=var1,
            var2=var2,
            groups=groups,
            **kwargs,
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

        if len(g1) < 2 or len(g2) < 2:
            return AnalysisResult(
                analysis_type="T-TEST",
                notes=[
                    f"Python backend: insufficient data for t-test "
                    f"(group sizes: {len(g1)}, {len(g2)}). Need ≥2 per group."
                ],
                parser_used="python_pingouin",
            )

        res = pg.ttest(g1, g2, correction="auto")
        t_val = float(res["T"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        dof = float(res["dof"].iloc[0])
        cd = float(res.get("cohen_d", pd.Series([0])).iloc[0])
        ci = res.get("CI95%", pd.Series([[]])).iloc[0]

        return AnalysisResult(
            analysis_type="T-TEST",
            tables=[
                TableResult(
                    title="Group Statistics",
                    rows=[
                        {
                            "group": str(groups[0]),
                            "N": len(g1),
                            "Mean": round(g1.mean(), 4),
                            "StdDev": round(g1.std(ddof=1), 4),
                        },
                        {
                            "group": str(groups[1]) if len(groups) > 1 else "—",
                            "N": len(g2),
                            "Mean": round(g2.mean(), 4) if len(g2) else 0,
                            "StdDev": round(g2.std(ddof=1), 4) if len(g2) else 0,
                        },
                    ],
                    source_format="python_pingouin",
                ),
                TableResult(
                    title="Independent Samples Test",
                    rows=[
                        {
                            "t": round(t_val, 4),
                            "df": int(dof),
                            "p_value": round(p_val, 4),
                            "Cohen_d": round(cd, 4),
                            "CI95": f"[{ci[0]:.4f}, {ci[1]:.4f}]"
                            if isinstance(ci, (list, tuple)) and len(ci) == 2
                            else str(ci),
                        }
                    ],
                    source_format="python_pingouin",
                ),
            ],
            statistics={
                "t_value": t_val,
                "p_value": p_val,
                "df": int(dof),
                "cohen_d": cd,
                "n_valid": len(g1) + len(g2),
            },
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
        if v1 == v2:
            return AnalysisResult(
                analysis_type="T-TEST",
                notes=[
                    f"Python backend: paired t-test requires two different variables (got '{v1}' twice)"
                ],
                parser_used="python_pingouin",
            )
        d1 = data[v1].dropna()
        d2 = data[v2].dropna()
        # Align lengths
        n = min(len(d1), len(d2))
        d1, d2 = d1.iloc[:n], d2.iloc[:n]

        res = pg.ttest(d1, d2, paired=True)
        t_val = float(res["T"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        dof = float(res["dof"].iloc[0])
        cd = float(res.get("cohen_d", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="T-TEST",
            tables=[
                TableResult(
                    title="Paired Samples Statistics",
                    rows=[
                        {
                            "variable": v1,
                            "N": n,
                            "Mean": round(d1.mean(), 4),
                            "StdDev": round(d1.std(ddof=1), 4),
                        },
                        {
                            "variable": v2,
                            "N": n,
                            "Mean": round(d2.mean(), 4),
                            "StdDev": round(d2.std(ddof=1), 4),
                        },
                    ],
                    source_format="python_pingouin",
                ),
                TableResult(
                    title="Paired Samples Test",
                    rows=[
                        {
                            "t": round(t_val, 4),
                            "df": int(dof),
                            "p_value": round(p_val, 4),
                            "Cohen_d": round(cd, 4),
                        },
                    ],
                    source_format="python_pingouin",
                ),
            ],
            statistics={
                "t_value": t_val,
                "p_value": p_val,
                "df": int(dof),
                "cohen_d": cd,
                "n_valid": n,
            },
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

        if len(clean[gv].unique()) < 2:
            return AnalysisResult(analysis_type="ONEWAY ANOVA", statistics={}, n_valid=len(clean),
                                  notes=["分组变量只有一个水平，无法进行 ANOVA 分析。"])

        res = pg.anova(data=clean, dv=tv, between=gv, detailed=True)
        f_val = float(res["F"].iloc[0])
        p_val = float(res["p_unc"].iloc[0])
        np2 = float(res.get("np2", pd.Series([0])).iloc[0])

        # Group descriptives
        desc = clean.groupby(gv)[tv].agg(["count", "mean", "std"]).reset_index()
        desc_rows = []
        for _, row in desc.iterrows():
            desc_rows.append(
                {
                    "group": str(row[gv]),
                    "N": int(row["count"]),
                    "Mean": round(row["mean"], 4),
                    "StdDev": round(row["std"], 4),
                }
            )

        return AnalysisResult(
            analysis_type="ANOVA",
            tables=[
                TableResult(title="Descriptives", rows=desc_rows, source_format="python_pingouin"),
                TableResult(
                    title="ANOVA",
                    rows=[
                        {"F": round(f_val, 4), "p_value": round(p_val, 4), "eta_sq": round(np2, 4)},
                    ],
                    source_format="python_pingouin",
                ),
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
        if dep == ind:
            return AnalysisResult(
                analysis_type="REGRESSION",
                notes=[
                    f"Python backend: dependent and independent variable "
                    f"are the same ('{dep}') — need different variables"
                ],
                parser_used="python_pingouin",
            )
        clean = data[[dep, ind]].dropna()

        res = pg.linear_regression(clean[ind], clean[dep])
        r2 = float(res["r2"].iloc[0])
        adj_r2 = float(res["adj_r2"].iloc[0])
        coef_rows = []
        for _, row in res.iterrows():
            coef_rows.append(
                {
                    "Predictor": row.get("names", "—"),
                    "B": round(float(row["coef"]), 4),
                    "SE": round(float(row["se"]), 4),
                    "t": round(float(row["T"]), 4),
                    "p": round(float(row["pval"]), 4),
                }
            )

        return AnalysisResult(
            analysis_type="REGRESSION",
            tables=[
                TableResult(
                    title="Model Summary",
                    rows=[
                        {
                            "R_squared": round(r2, 4),
                            "Adj_R_squared": round(adj_r2, 4),
                            "N": len(clean),
                        },
                    ],
                    source_format="python_pingouin",
                ),
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
        self,
        data: pd.DataFrame | None = None,
        var1: str | None = None,
        var2: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        return self._correlation(data, method="pearson", var1=var1, var2=var2)  # type: ignore[arg-type]

    def _correlation_spearman(
        self,
        data: pd.DataFrame | None = None,
        var1: str | None = None,
        var2: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        return self._correlation(data, method="spearman", var1=var1, var2=var2)  # type: ignore[arg-type]

    def _correlation(
        self,
        data: pd.DataFrame | None = None,
        method: str = "pearson",
        var1: str | None = None,
        var2: str | None = None,
    ) -> AnalysisResult:
        if data is None:
            return AnalysisResult(
                analysis_type="CORRELATIONS",
                notes=["Python backend: no data provided"],
                parser_used="python_pingouin",
            )
        import pingouin as pg

        # Use specified vars when provided and valid, else pick two numeric cols
        if var1 and var2 and var1 in data.columns and var2 in data.columns and var1 != var2:
            v1, v2 = var1, var2
        else:
            nums = [
                c
                for c in data.columns
                if pd.api.types.is_numeric_dtype(data[c]) and c.lower() not in ("id",)
            ]
            if len(nums) < 2:
                return AnalysisResult(
                    analysis_type="CORRELATIONS",
                    notes=["Python backend: need ≥2 numeric columns for correlation"],
                    parser_used="python_pingouin",
                )
            v1 = nums[0]
            v2 = nums[1]
        clean = data[[v1, v2]].dropna()

        res = pg.corr(clean[v1], clean[v2], method=method)
        r_val = float(res["r"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        n_val = int(res["n"].iloc[0])

        return AnalysisResult(
            analysis_type="CORRELATIONS",
            tables=[
                TableResult(
                    title="Correlations",
                    rows=[
                        {
                            "variables": f"{v1} × {v2}",
                            "r": round(r_val, 4),
                            "p_value": round(p_val, 4),
                            "N": n_val,
                            "method": method,
                        },
                    ],
                    source_format="python_pingouin",
                ),
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
        dof = int(pearson["dof"].iloc[0])

        ctab_rows = []
        for idx, row in ctab.iterrows():
            r = {"": str(idx)}
            r.update({str(k): int(v) for k, v in row.items()})
            ctab_rows.append(r)

        result = AnalysisResult(
            analysis_type="CROSSTABS",
            tables=[
                TableResult(
                    title="Crosstabulation", rows=ctab_rows, source_format="python_pingouin"
                ),
                TableResult(
                    title="Chi-Square Tests",
                    rows=[
                        {"chi_square": round(chi2, 4), "df": dof, "p_value": round(p_val, 4)},
                    ],
                    source_format="python_pingouin",
                ),
            ],
            statistics={"chi_square": chi2, "p_value": p_val, "df": dof, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )
        if "expected" in dir() and hasattr(expected, 'values'):
            min_expected = min(expected.values().flat) if hasattr(expected, 'values') else None
            if min_expected is not None and min_expected < 5:
                result.notes.append(f"最小期望频数为 {min_expected:.1f}（<5），卡方检验结果可能不可靠。")
        return result

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
            rows.append(
                {
                    "value": str(r["value"]),
                    "Frequency": int(r["Frequency"]),
                    "Percent": round(float(r["Percent"]), 2),
                    "Valid_Percent": round(float(r["Valid_Percent"]), 2),
                    "Cumulative_Percent": round(float(r["Cumulative_Percent"]), 2),
                }
            )

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
        rows = [
            {
                "variable": tv,
                "N": len(col),
                "Mean": round(float(col.mean()), 4),
                "StdDev": round(float(col.std(ddof=1)), 4),
                "Min": round(float(col.min()), 4),
                "Max": round(float(col.max()), 4),
                "Median": round(float(col.median()), 4),
                "Skewness": round(float(col.skew()), 4),
                "Kurtosis": round(float(col.kurtosis()), 4),
            }
        ]

        nan_stats = [k for k, v in {"mean": rows[0]["Mean"], "std_dev": rows[0]["StdDev"],
                                      "minimum": rows[0]["Min"], "maximum": rows[0]["Max"]}.items()
                     if isinstance(v, float) and (v != v)]
        result = AnalysisResult(
            analysis_type="DESCRIPTIVES",
            tables=[
                TableResult(
                    title="Descriptive Statistics", rows=rows, source_format="python_pingouin"
                )
            ],
            statistics={
                "n": len(col),
                "n_valid": len(col),
                "mean": rows[0]["Mean"],
                "std_dev": rows[0]["StdDev"],
                "minimum": rows[0]["Min"],
                "maximum": rows[0]["Max"],
            },
            n_valid=len(col),
            n_missing=int(len(data) - len(col)),
            parser_used="python_pingouin",
        )
        if nan_stats:
            result.notes.append(f"以下统计量为 NaN（常数列或无变异）：{', '.join(nan_stats)}")
        return result

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

        if len(g1) < 2 or len(g2) < 2:
            return AnalysisResult(
                analysis_type="MANN_WHITNEY",
                notes=[f"Python backend: insufficient data (group sizes: {len(g1)}, {len(g2)})"],
                parser_used="python_pingouin",
            )

        res = pg.mwu(g1, g2)
        u_val = float(res["U_val"].iloc[0])
        p_val = float(res["p_val"].iloc[0])
        rbc = float(res.get("RBC", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="MANN_WHITNEY",
            tables=[
                TableResult(
                    title="Mann-Whitney U Test",
                    rows=[
                        {
                            "U": round(u_val, 4),
                            "p_value": round(p_val, 4),
                            "RBC": round(rbc, 4),
                            "N": len(g1) + len(g2),
                        },
                    ],
                    source_format="python_pingouin",
                ),
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

        if len(clean[gv].unique()) < 2:
            return AnalysisResult(analysis_type="KRUSKAL_WALLIS", statistics={}, n_valid=len(clean),
                                  notes=["分组变量只有一个水平，无法进行 Kruskal-Wallis 检验。"])

        res = pg.kruskal(data=clean, dv=tv, between=gv)
        h_val = float(res["H"].iloc[0])
        p_val = float(res["p_unc"].iloc[0])
        eta_sq = float(res.get("np2", pd.Series([0])).iloc[0])

        return AnalysisResult(
            analysis_type="KRUSKAL_WALLIS",
            tables=[
                TableResult(
                    title="Kruskal-Wallis Test",
                    rows=[
                        {
                            "H": round(h_val, 4),
                            "p_value": round(p_val, 4),
                            "eta_sq": round(eta_sq, 4),
                            "N": len(clean),
                        },
                    ],
                    source_format="python_pingouin",
                ),
            ],
            statistics={"h": h_val, "p_value": p_val, "eta_sq": eta_sq, "n_valid": len(clean)},
            n_valid=len(clean),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Wilcoxon Signed-Rank (non-parametric paired test)
    # ==================================================================

    def _wilcoxon(
        self,
        data: pd.DataFrame,
        test_var: str | None = None,
        grouping_var: str | None = None,
        **__: Any,
    ) -> AnalysisResult:
        """Wilcoxon signed-rank test — a non-parametric alternative to the
        paired t-test.  Compares two related samples (two columns or two
        groups of a grouping variable).

        Returns W-statistic and p-value via ``pingouin.wilcoxon``.
        """
        import pingouin as pg

        # For paired test, need two columns or two groups
        cols = list(data.select_dtypes(include="number").columns)

        if len(cols) < 2:
            return AnalysisResult(
                analysis_type="WILCOXON",
                notes=["Python backend: Wilcoxon test requires ≥2 numeric columns"],
                parser_used="python_pingouin",
            )

        # Grab the first two numeric columns
        x = data[cols[0]].dropna()
        y = data[cols[1]].dropna()
        # Align lengths
        n = min(len(x), len(y))
        x, y = x.iloc[:n], y.iloc[:n]

        if n < 2:
            return AnalysisResult(
                analysis_type="WILCOXON",
                notes=["Python backend: insufficient data for Wilcoxon test"],
                parser_used="python_pingouin",
            )

        res = pg.wilcoxon(x, y)
        w_val = float(res["W-val"].iloc[0])
        p_val = float(res["p-val"].iloc[0])

        return AnalysisResult(
            analysis_type="WILCOXON",
            tables=[
                TableResult(
                    title="Wilcoxon Signed-Rank Test",
                    rows=[
                        {
                            "W": round(w_val, 4),
                            "p_value": round(p_val, 4),
                            "N": n,
                            "variables": f"{cols[0]} × {cols[1]}",
                        },
                    ],
                    source_format="python_pingouin",
                ),
            ],
            statistics={"w_value": w_val, "p_value": p_val, "n_valid": n},
            n_valid=n,
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Multiple Regression (linear, multiple predictors)
    # ==================================================================

    def _multiple_regression(
        self,
        data: pd.DataFrame,
        dep_var: str | None = None,
        **kwargs: Any,
    ) -> AnalysisResult:
        """Multiple linear regression via ``pingouin.linear_regression``.

        Accepts a list of predictor columns via the ``covariates`` kwarg.
        Falls back to all numeric columns (minus dep_var) when no
        covariates are specified.
        """
        import pingouin as pg

        dep = dep_var or data.columns[0]
        predictors: list[str] = kwargs.get("covariates") or [
            c
            for c in data.select_dtypes(include="number").columns
            if c != dep
        ]

        if not predictors:
            return AnalysisResult(
                analysis_type="REGRESSION",
                notes=["Python backend: no predictors available for multiple regression"],
                parser_used="python_pingouin",
            )

        all_vars = [dep] + predictors
        clean = data[all_vars].dropna()

        if len(clean) < len(predictors) + 2:
            return AnalysisResult(
                analysis_type="REGRESSION",
                notes=[
                    f"Python backend: insufficient data ({len(clean)} rows, "
                    f"{len(predictors)} predictors) for multiple regression"
                ],
                parser_used="python_pingouin",
            )

        res = pg.linear_regression(clean[predictors], clean[dep])
        r2 = float(res["r2"].iloc[0])
        adj_r2 = float(res["adj_r2"].iloc[0])

        coef_rows = []
        for _, row in res.iterrows():
            coef_rows.append(
                {
                    "Predictor": row.get("names", "—"),
                    "B": round(float(row["coef"]), 4),
                    "SE": round(float(row["se"]), 4),
                    "t": round(float(row["T"]), 4),
                    "p": round(float(row["pval"]), 4),
                }
            )

        return AnalysisResult(
            analysis_type="REGRESSION",
            tables=[
                TableResult(
                    title="Model Summary",
                    rows=[
                        {
                            "R_squared": round(r2, 4),
                            "Adj_R_squared": round(adj_r2, 4),
                            "N": len(clean),
                            "predictors": len(predictors),
                        },
                    ],
                    source_format="python_pingouin",
                ),
                TableResult(
                    title="Coefficients",
                    rows=coef_rows,
                    source_format="python_pingouin",
                ),
            ],
            statistics={
                "r_squared": r2,
                "adj_r_squared": adj_r2,
                "n_valid": len(clean),
                "n_predictors": len(predictors),
            },
            n_valid=len(clean),
            parser_used="python_pingouin",
        )

    # ==================================================================
    # Logistic Regression (placeholder)
    # ==================================================================

    def _logistic_regression(
        self,
        data: pd.DataFrame,
        dep_var: str | None = None,
        **kwargs: Any,
    ) -> AnalysisResult:
        """Logistic regression — placeholder.

        pingouin does not provide logistic regression; statsmodels is
        required.  Returns a polite "not yet supported" result that will
        trigger the SPSS fallback when available.
        """
        dep = dep_var or (data.columns[0] if len(data.columns) > 0 else "?")
        predictors = kwargs.get("covariates", [])
        if not predictors:
            predictors_str = "no predictors specified"
        else:
            predictors_str = ", ".join(str(p) for p in predictors)

        return AnalysisResult(
            analysis_type="REGRESSION",
            notes=[
                f"Python backend: logistic regression 尚未实现（需要 statsmodels 库）。"
                f"  Dependent: {dep}.  Predictors: {predictors_str}.",
                "This method will fall through to the SPSS backend when available.",
            ],
            statistics={},
            parser_used="python_pingouin",
        )
