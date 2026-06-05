"""Chart generation for analysis results using matplotlib.

Generates base64-encoded PNG charts that can be embedded in HTML or
Word exports.  No new pip dependencies — matplotlib is a transitive
dependency of pingouin.

Usage
-----
>>> from snla.explainer.charts import generate_chart
>>> img_b64 = generate_chart(analysis_result, method="independent_t_test")
>>> # img_b64 is a base64 PNG string or None
"""

from __future__ import annotations

import base64
import io
from contextlib import suppress
from typing import Any

import matplotlib

matplotlib.use("Agg")  # non-interactive backend — must be before pyplot import
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------------------
# Chinese font configuration
# ---------------------------------------------------------------------------

def _setup_chinese_font() -> None:
    """Try to set a Chinese-friendly font; silently fall back to default."""
    candidates = ["SimHei", "Microsoft YaHei", "Microsoft JhengHei",
                  "PingFang SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei"]
    for name in candidates:
        if name in [f.name for f in matplotlib.font_manager.fontManager.ttflist]:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return
    # No Chinese font found — matplotlib will use default (may show boxes)
    plt.rcParams["axes.unicode_minus"] = False


_setup_chinese_font()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bar_chart(
    groups: list[str],
    means: list[float],
    errors: list[float] | None = None,
    title: str = "组间比较",
    ylabel: str = "均值",
) -> str:
    """Generate a grouped bar chart with optional error bars.

    Args:
        groups: Group labels (e.g. ["男性", "女性"]).
        means: Mean values for each group.
        errors: Error-bar magnitudes (e.g. std error).  Same length as means.
        title: Chart title.
        ylabel: Y-axis label.

    Returns:
        Base64-encoded PNG string.
    """
    if not groups or not means or len(groups) != len(means):
        return ""

    n = len(groups)
    x = np.arange(n)
    width = 0.5

    fig, ax = plt.subplots(figsize=(6, 4))

    if errors and len(errors) == n:
        ax.bar(x, means, width, yerr=errors, capsize=5,
               color="#4A90D9", edgecolor="white", linewidth=0.8)
    else:
        ax.bar(x, means, width, color="#4A90D9", edgecolor="white", linewidth=0.8)

    ax.set_xticks(x)
    ax.set_xticklabels(groups, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)

    # Add value labels on top of bars
    for i, (xi, mi) in enumerate(zip(x, means, strict=True)):
        ax.text(xi, mi + (errors[i] if errors and len(errors) == n else 0) + 0.02 * max(means),
                f"{mi:.1f}", ha="center", va="bottom", fontsize=10)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    return _fig_to_b64(fig)


def scatter_plot(
    x_data: list[float],
    y_data: list[float],
    title: str = "相关性分析",
    xlabel: str = "X",
    ylabel: str = "Y",
    show_line: bool = True,
) -> str:
    """Generate a scatter plot with optional regression line.

    Args:
        x_data: X-axis values.
        y_data: Y-axis values.
        title: Chart title.
        xlabel: X-axis label.
        ylabel: Y-axis label.
        show_line: Whether to overlay a linear regression line.

    Returns:
        Base64-encoded PNG string.
    """
    if not x_data or not y_data or len(x_data) != len(y_data):
        return ""
    if len(x_data) < 2:
        return ""

    x_arr = np.array(x_data, dtype=float)
    y_arr = np.array(y_data, dtype=float)

    # Remove NaN pairs
    mask = ~(np.isnan(x_arr) | np.isnan(y_arr))
    x_arr = x_arr[mask]
    y_arr = y_arr[mask]

    if len(x_arr) < 2:
        return ""

    fig, ax = plt.subplots(figsize=(6, 4))

    ax.scatter(x_arr, y_arr, alpha=0.6, color="#4A90D9", edgecolors="white", linewidth=0.5, s=40)

    if show_line and len(x_arr) >= 2:
        coeffs = np.polyfit(x_arr, y_arr, 1)
        poly_fn = np.poly1d(coeffs)
        x_line = np.linspace(x_arr.min(), x_arr.max(), 100)
        ax.plot(x_line, poly_fn(x_line), color="#E74C3C", linewidth=2, alpha=0.8,
                label=f"y = {coeffs[0]:.2f}x + {coeffs[1]:.2f}")
        ax.legend(fontsize=10)

    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    return _fig_to_b64(fig)


def histogram(
    values: list[float],
    title: str = "分布",
    xlabel: str = "值",
    bins: int | None = None,
    show_mean: bool = True,
) -> str:
    """Generate a histogram with optional mean line.

    Args:
        values: Data values.
        title: Chart title.
        xlabel: X-axis label.
        bins: Number of bins (auto-calculated if None).
        show_mean: Whether to overlay a vertical mean line.

    Returns:
        Base64-encoded PNG string.
    """
    if not values:
        return ""

    arr = np.array(values, dtype=float)
    arr = arr[~np.isnan(arr)]

    if len(arr) < 2:
        return ""

    if bins is None:
        # Freedman-Diaconis rule
        iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
        if iqr == 0:
            bins = 10
        else:
            bin_width = 2 * iqr / (len(arr) ** (1 / 3))
            bins = max(3, int(np.ceil((arr.max() - arr.min()) / bin_width)))

    fig, ax = plt.subplots(figsize=(6, 4))

    ax.hist(arr, bins=bins, color="#4A90D9", edgecolor="white", linewidth=0.8, alpha=0.85)

    if show_mean:
        mean_val = float(np.mean(arr))
        ax.axvline(mean_val, color="#E74C3C", linewidth=2, linestyle="--",
                   label=f"均值 = {mean_val:.1f}")
        ax.legend(fontsize=10)

    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel("频数", fontsize=11)
    ax.set_title(title, fontsize=13, fontweight="bold", pad=12)

    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()

    return _fig_to_b64(fig)


def generate_chart(result: Any, method: str) -> str | None:
    """Route to the appropriate chart generator based on analysis method.

    Extracts data from an AnalysisResult and generates the most suitable
    chart for the given statistical method.

    Args:
        result: An AnalysisResult instance (from snla.parser.schema).
        method: Statistical method code (e.g. "independent_t_test").

    Returns:
        Base64-encoded PNG string, or None if no chart is applicable
        or data is insufficient.
    """
    from snla.parser.schema import AnalysisResult

    if not isinstance(result, AnalysisResult):
        return None

    analysis_type = result.analysis_type.upper()

    # ---- Bar chart: t-test, ANOVA, Mann-Whitney, Kruskal-Wallis ----------
    bar_methods = {
        "independent_t_test", "paired_t_test", "oneway_anova",
        "mann_whitney_u", "kruskal_wallis",
    }
    bar_types = {"T-TEST", "PAIRED T-TEST", "PAIRED_SAMPLES_TTEST",
                 "ONEWAY", "ANOVA", "UNIANOVA",
                 "MANN_WHITNEY", "KRUSKAL_WALLIS"}

    if method in bar_methods or analysis_type in bar_types:
        return _bar_from_result(result)

    # ---- Scatter plot: correlation, regression ---------------------------
    scatter_methods = {
        "pearson_correlation", "spearman_correlation", "simple_regression",
        "multiple_regression",
    }
    scatter_types = {"CORRELATION", "CORRELATIONS", "NONPAR CORR",
                     "REGRESSION", "LINEAR REGRESSION"}

    if method in scatter_methods or analysis_type in scatter_types:
        return _scatter_from_result(result)

    # ---- Histogram: descriptives, frequencies ----------------------------
    hist_methods = {"descriptives", "frequencies"}
    hist_types = {"DESCRIPTIVES", "FREQUENCIES"}

    if method in hist_methods or analysis_type in hist_types:
        return _hist_from_result(result)

    return None


# ---------------------------------------------------------------------------
# Internal: result-aware chart builders
# ---------------------------------------------------------------------------


def _bar_from_result(result: Any) -> str | None:
    """Build a bar chart from an AnalysisResult for group-comparison methods."""
    stats = result.statistics

    # Try to extract group means and labels
    mean_a = stats.get("mean_a") or stats.get("mean_group1") or stats.get("mean_1")
    mean_b = stats.get("mean_b") or stats.get("mean_group2") or stats.get("mean_2")
    label_a = stats.get("label_a") or stats.get("label_group1") or "组A"
    label_b = stats.get("label_b") or stats.get("label_group2") or "组B"

    if mean_a is None and mean_b is None:
        # Try generic mean (single group — not ideal for bar chart)
        mean_val = stats.get("mean")
        if mean_val is not None:
            return bar_chart(["总体"], [float(mean_val)], title="均值")
        return None

    means: list[float] = []
    groups: list[str] = []
    errors: list[float] = []

    if mean_a is not None:
        means.append(float(mean_a))
        groups.append(str(label_a))
        se_a = stats.get("std_error_a") or stats.get("std_error_group1")
        if se_a is not None:
            errors.append(float(se_a))

    if mean_b is not None:
        means.append(float(mean_b))
        groups.append(str(label_b))
        se_b = stats.get("std_error_b") or stats.get("std_error_group2")
        if se_b is not None:
            errors.append(float(se_b))

    # For ANOVA, try to extract multiple group means from tables
    if not means and result.tables:
        for table in result.tables:
            for row in table.rows:
                row_mean = row.get("Mean") or row.get("均值") or row.get("mean")
                row_label = row.get("Group") or row.get("组") or row.get("group")
                if row_mean is not None:
                    try:
                        means.append(float(row_mean))
                        groups.append(str(row_label) if row_label else f"组{len(means)}")
                    except (ValueError, TypeError):
                        pass

    if not means:
        return None

    title = _chart_title_for_result(result, "组间比较")
    ylabel = "均值"
    return bar_chart(groups, means, errors if errors else None, title=title, ylabel=ylabel)


def _scatter_from_result(result: Any) -> str | None:
    """Build a scatter plot from an AnalysisResult for correlation/regression."""
    stats = result.statistics

    # Check if raw x/y data is available in statistics
    x_data = stats.get("x_values") or stats.get("x_data")
    y_data = stats.get("y_values") or stats.get("y_data")

    if x_data and y_data:
        xlabel = stats.get("x_label", "X")
        ylabel = stats.get("y_label", "Y")
        title = _chart_title_for_result(result, "相关性分析")
        show_line = result.analysis_type.upper() in {"REGRESSION", "LINEAR REGRESSION"}
        return scatter_plot(
            list(x_data), list(y_data),
            title=title, xlabel=str(xlabel), ylabel=str(ylabel),
            show_line=show_line,
        )

    # Try to extract from tables (less common — raw data usually not in SPSS output)
    return None


def _hist_from_result(result: Any) -> str | None:
    """Build a histogram from an AnalysisResult for descriptives/frequencies."""
    stats = result.statistics

    values = stats.get("values") or stats.get("data_values")
    if values:
        var_name = stats.get("variable_name") or stats.get("var_name", "变量")
        title = _chart_title_for_result(result, f"{var_name} 分布")
        return histogram(list(values), title=title, xlabel=str(var_name))

    # Try to extract from tables
    if result.tables:
        for table in result.tables:
            for row in table.rows:
                val = row.get("Value") or row.get("值") or row.get("value")
                if val is not None:
                    try:
                        values = [float(val)]
                        # Collect more values from subsequent rows
                        for r in result.tables:
                            for rw in r.rows:
                                v = rw.get("Value") or rw.get("值")
                                if v is not None:
                                    with suppress(ValueError, TypeError):
                                        values.append(float(v))
                        if len(values) >= 2:
                            var_name = stats.get("variable_name", "变量")
                            title = _chart_title_for_result(result, f"{var_name} 分布")
                            return histogram(values, title=title, xlabel=str(var_name))
                    except (ValueError, TypeError):
                        pass

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _chart_title_for_result(result: Any, default: str) -> str:
    """Build a chart title from AnalysisResult metadata."""
    stats = result.statistics
    dep_var = stats.get("dependent_variable") or stats.get("dep_var")
    if dep_var:
        return f"{dep_var} — {default}"
    return default


def _fig_to_b64(fig: plt.Figure) -> str:
    """Convert a matplotlib Figure to a base64-encoded PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    buf.seek(0)
    img_b64 = base64.b64encode(buf.read()).decode("utf-8")
    plt.close(fig)
    return img_b64
