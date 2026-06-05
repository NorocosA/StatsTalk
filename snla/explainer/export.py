"""Word (docx) report export for SNLA analysis results.

Produces a formatted ``.docx`` file with the analysis description,
statistical method, key results, plain-language explanation, and an
optional APA-style summary paragraph.  Designed for social science
undergraduates writing theses or lab reports.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from snla.parser.schema import AnalysisResult


def export_to_docx(
    output_path: str,
    user_query: str,
    method: str,
    analysis_result: AnalysisResult,
    explanation: str,
    data_file: str = "",
    export_apa: bool = True,
) -> str:
    """Generate a Word report from an analysis result and save to *output_path*.

    Args:
        output_path: Destination ``.docx`` file path.
        user_query: The user's original natural-language question.
        method: The statistical method code (e.g. ``"independent_t_test"``).
        analysis_result: Parsed ``AnalysisResult`` from the parser layer.
        explanation: The plain-language explanation string.
        data_file: Name of the source data file (for metadata).
        export_apa: If ``True``, include an APA-style summary paragraph.

    Returns:
        Absolute path to the generated ``.docx`` file.
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = Document()

    # ── Styles ───────────────────────────────────────────────────────────
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    # ── Title ────────────────────────────────────────────────────────────
    title = doc.add_heading("SPSS 统计分析报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── Metadata block ───────────────────────────────────────────────────
    doc.add_paragraph(
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        style="List Bullet",
    )
    if data_file:
        doc.add_paragraph(f"数据文件：{os.path.basename(data_file)}", style="List Bullet")
    doc.add_paragraph("")

    # ── Section 1: Analysis Description ──────────────────────────────────
    doc.add_heading("1. 分析描述", level=1)
    doc.add_paragraph(f"用户提问：{user_query}")

    # ── Section 2: Statistical Method ────────────────────────────────────
    doc.add_heading("2. 统计方法", level=1)
    method_label = _method_label(method)
    doc.add_paragraph(f"推荐方法：{method_label}")
    if analysis_result.n_valid:
        doc.add_paragraph(f"有效样本量：N = {analysis_result.n_valid}")

    # ── Section 3: Key Results ───────────────────────────────────────────
    doc.add_heading("3. 关键统计量", level=1)
    stats = analysis_result.statistics
    if stats:
        table = doc.add_table(rows=1, cols=2, style="Table Grid")
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = "统计量"
        hdr_cells[1].text = "数值"
        # Bold header
        for cell in hdr_cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True

        # Add rows for each notable statistic
        _add_stat_row(table, "p 值", stats.get("p_value"), fmt=".4f")
        _add_stat_row(table, "t 值", stats.get("t_value"), fmt=".3f")
        _add_stat_row(table, "F 值", stats.get("f_value"), fmt=".3f")
        _add_stat_row(table, "χ² 值", stats.get("chi_square"), fmt=".3f")
        _add_stat_row(table, "自由度 (df)", stats.get("df"))
        _add_stat_row(table, "效应量 (Cohen's d)", stats.get("d_value"), fmt=".3f")
        _add_stat_row(table, "R²", stats.get("r_squared") or stats.get("r2"), fmt=".3f")
        _add_stat_row(table, "相关系数 (r)", stats.get("r"), fmt=".3f")
        _add_stat_row(table, "均值差", stats.get("mean_diff"), fmt=".2f")
        _add_stat_row(table, "样本量 (N)", stats.get("n_valid") or stats.get("n"))

        # Group means (T-TEST style)
        mean_a = stats.get("mean_a") or stats.get("mean_group1") or stats.get("mean_male")
        mean_b = stats.get("mean_b") or stats.get("mean_group2") or stats.get("mean_female")
        label_a = stats.get("label_a") or "组 A"
        label_b = stats.get("label_b") or "组 B"
        if mean_a is not None:
            _add_stat_row(table, f"均值 ({label_a})", mean_a, fmt=".2f")
        if mean_b is not None:
            _add_stat_row(table, f"均值 ({label_b})", mean_b, fmt=".2f")

        # Descriptive statistics
        _add_stat_row(table, "均值", stats.get("mean"), fmt=".2f")
        _add_stat_row(
            table, "标准差", stats.get("std_dev") or stats.get("std_deviation"), fmt=".2f"
        )
        _add_stat_row(table, "最小值", stats.get("minimum"), fmt=".2f")
        _add_stat_row(table, "最大值", stats.get("maximum"), fmt=".2f")

    # ── Section 4: Explanation ───────────────────────────────────────────
    doc.add_heading("4. 结果解读", level=1)
    doc.add_paragraph(explanation)

    # ── Section 5: APA Format (optional) ─────────────────────────────────
    if export_apa:
        doc.add_heading("5. APA 格式摘要", level=1)
        apa_text = _build_apa(method, method_label, stats)
        p = doc.add_paragraph(apa_text)
        p.runs[0].bold = True

    # ── Footer ───────────────────────────────────────────────────────────
    doc.add_paragraph("")
    doc.add_paragraph(
        "本报告由 StatsTalk 自动生成。",
    ).runs[0].italic = True

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    doc.save(output_path)
    return os.path.abspath(output_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add_stat_row(table, label: str, value: Any, fmt: str = "") -> None:
    """Add a row to the statistics table if *value* is not ``None``."""
    if value is None:
        return
    row = table.add_row()
    row.cells[0].text = label
    if isinstance(value, float) and fmt:
        try:
            row.cells[1].text = format(value, fmt)
        except (ValueError, TypeError):
            row.cells[1].text = str(value)
    else:
        row.cells[1].text = str(value)


def _method_label(method: str) -> str:
    """Convert a method code to its Chinese display label."""
    labels: dict[str, str] = {
        "independent_t_test": "独立样本 t 检验",
        "paired_t_test": "配对样本 t 检验",
        "oneway_anova": "单因素方差分析 (ANOVA)",
        "pearson_correlation": "Pearson 相关分析",
        "spearman_correlation": "Spearman 秩相关分析",
        "simple_regression": "简单线性回归",
        "multiple_regression": "多元线性回归",
        "chi_square": "卡方检验 (χ²)",
        "descriptives": "描述性统计",
        "frequencies": "频率分析",
        "mann_whitney_u": "Mann-Whitney U 检验",
        "kruskal_wallis": "Kruskal-Wallis 检验",
        "ancova": "协方差分析 (ANCOVA)",
        "manova": "多变量方差分析 (MANOVA)",
    }
    return labels.get(method, method.replace("_", " ").title())


def _build_apa(method: str, method_label: str, stats: dict[str, Any]) -> str:
    """Build a brief APA-style sentence summarising the result.

    Returns an empty string if insufficient statistics are available.
    """
    p = stats.get("p_value")

    # ── T-TEST style ──
    t = stats.get("t_value")
    df_t = stats.get("df")
    if t is not None and df_t is not None and p is not None:
        t_str = f"t({int(df_t)}) = {t:.2f}"
        p_str = _apa_p(p)
        mean_diff = stats.get("mean_diff")
        if mean_diff is not None:
            return f"采用{method_label}，结果{t_str}，{p_str}，均值差 = {mean_diff:.2f}。"
        return f"采用{method_label}，结果{t_str}，{p_str}。"

    # ── ANOVA style ──
    f_val = stats.get("f_value")
    if f_val is not None and p is not None:
        df_model = stats.get("df")
        df_str = f"({int(df_model)}, ...)" if df_model is not None else ""
        return f"采用{method_label}，结果F{df_str} = {f_val:.2f}，{_apa_p(p)}。"

    # ── Correlation style ──
    r = stats.get("r")
    n = stats.get("n_valid") or stats.get("n")
    if r is not None and p is not None:
        n_str = f", N = {int(n)}" if n is not None else ""
        return f"采用{method_label}，结果r = {r:.3f}{n_str}，{_apa_p(p)}。"

    # ── Chi-square style ──
    chi2 = stats.get("chi_square")
    if chi2 is not None and p is not None:
        df_chi = stats.get("df")
        df_str = f"({int(df_chi)}, N = ...)" if df_chi is not None else ""
        return f"采用{method_label}，结果χ²{df_str} = {chi2:.2f}，{_apa_p(p)}。"

    # ── Generic fallback ──
    if p is not None:
        return f"采用{method_label}，p = {p:.4f}。"

    return ""


def _apa_p(p_value: float) -> str:
    """Format a p-value in APA style (p < .05, p = .031, etc.)."""
    if p_value < 0.001:
        return "p < .001"
    return f"p = {p_value:.3f}"
