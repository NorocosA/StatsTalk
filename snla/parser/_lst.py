"""
LST text parser — fallback parsing strategy for SPSS output.

Parses raw SPSS ``.lst`` text output using regex-based extraction rules.
Supports T-TEST and ANOVA tables in both Chinese and English locales.
"""

import logging
import re
from typing import Any

from snla.parser._oms import _safe_float
from snla.parser.schema import AnalysisResult, TableResult

logger = logging.getLogger(__name__)

# ===================================================================
# LST extraction rules — master dictionary
# ===================================================================
# Each top-level key is an SPSS analysis type. The rules provide:
#   - title_pattern: regex to locate the table heading in raw text
#   - row_pattern:   regex to extract individual data rows
#   - columns:       ordered column names
#
# Currently T-TEST rules are fully implemented; other types are stubs
# for future extension.

EXTRACTION_RULES: dict[str, dict[str, Any]] = {
    "T-TEST": {
        "group_stats": {
            "title_pattern": r"(?i)(Group Statistics|组统计)",
            "columns": ["Group", "N", "Mean", "Std. Deviation", "Std. Error Mean"],
        },
        "independent_test": {
            "title_pattern": r"(?i)(Independent Samples Test|独立样本检验)",
            "equal_var_row": (
                r"(?i)"
                r"(Equal variances assumed|(?<!不)假设方差相等)"
                r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"
                r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"
                r"\s{2,}([\d.eE\-]+)"
                r"(?:\s{2,}([\d.,\-]+))?"
                r"(?:\s{2,}([\d.,\-]+))?"
            ),
            "unequal_var_row": (
                r"(?i)"
                r"(Equal variances not assumed|不假设方差相等)"
                r"\s{2,}([\d.,\-]+)"
                r"\s{2,}([\d.,\-]+)"
                r"\s{2,}([\d.eE\-]+)"
                r"(?:\s{2,}([\d.,\-]+))?"
                r"(?:\s{2,}([\d.,\-]+))?"
            ),
            "columns": [
                "",
                "F",
                "Sig.",
                "t",
                "df",
                "Sig. (2-tailed)",
                "Mean Difference",
                "Std. Error Difference",
            ],
        },
    },
    "ANOVA": {
        "title_pattern": r"(?i)(ANOVA|主体间效应检验)",
        "columns": ["Source", "Type III SS", "df", "Mean Square", "F", "Sig."],
    },
    "REGRESSION": {
        "title_pattern": r"(?i)(Coefficients|系数|Model Summary|模型摘要)",
        "columns": ["Model", "B", "Std. Error", "Beta", "t", "Sig."],
    },
    "FREQUENCIES": {
        "title_pattern": r"(?i)(Statistics|统计|Frequency|频率)",
        "columns": ["", "Frequency", "Percent", "Valid Percent", "Cumulative Percent"],
    },
    "CROSSTABS": {
        "title_pattern": r"(?i)(Crosstabulation|交叉表|Chi-Square Tests|卡方检验)",
        "columns": ["", "Value", "df", "Asymptotic Significance (2-sided)"],
    },
    "DESCRIPTIVES": {
        "title_pattern": r"(?i)(Descriptive Statistics|描述统计)",
        "columns": ["", "N", "Minimum", "Maximum", "Mean", "Std. Deviation"],
    },
}


def _extract_table_block(text: str, title_pattern: re.Pattern) -> str | None:
    """
    Locate a table title in LST text and extract the content block that
    follows it, delimited by the next table title (or end of string).

    Table titles are typically on their own line, followed by one or more
    data lines, then a blank line and the next title.

    Args:
        text: The full LST text content.
        title_pattern: A compiled regex matching the table title line.

    Returns:
        The raw text block belonging to the table, or ``None`` if the
        title was not found.
    """
    match = title_pattern.search(text)
    if not match:
        return None

    start = match.end()
    # Look ahead for the next table-title-like boundary:
    # blank line followed by a non-whitespace or CJK character.
    end_match = re.search(
        r"\n\s*\n(?=\S|[\u4e00-\u9fff\u3400-\u4dbf])",
        text[start:],
    )
    if end_match:
        end = start + end_match.start()
    else:
        end = len(text)

    block = text[start:end].strip()
    return block if block else None


# ---------------------------------------------------------------------------
# T-TEST extractors
# ---------------------------------------------------------------------------


def _extract_ttest_group_stats(text: str) -> list[dict[str, Any]]:
    """
    Extract the *Group Statistics* (组统计) table from T-TEST LST output.

    Uses fixed-width column detection (split on 2+ spaces) which is the
    standard SPSS output format. Each data row must have an integer in
    the second position (N), which distinguishes data from header lines.

    Returns a list of rows, each with keys:
    ``Group``, ``N``, ``Mean``, ``Std. Deviation``, ``Std. Error Mean``.
    """
    title_pattern = re.compile(r"(?i)(Group Statistics|组统计)", re.UNICODE)

    block = _extract_table_block(text, title_pattern)
    if not block:
        return []

    rows: list[dict[str, Any]] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # SPSS uses fixed-width columns separated by 2+ spaces.
        # Split on that first.
        parts = re.split(r"\s{2,}", stripped)
        if len(parts) >= 4 and re.match(r"^\d+$", parts[1].strip()):
            rows.append(
                {
                    "Group": parts[0].strip(),
                    "N": parts[1].strip(),
                    "Mean": parts[2].strip(),
                    "Std. Deviation": parts[3].strip(),
                    "Std. Error Mean": parts[4].strip() if len(parts) > 4 else "",
                }
            )
        elif len(parts) >= 4:
            # Fallback: try splitting on single spaces (e.g. narrow columns)
            parts2 = stripped.split()
            if len(parts2) >= 4 and re.match(r"^\d+$", parts2[1]):
                rows.append(
                    {
                        "Group": parts2[0],
                        "N": parts2[1],
                        "Mean": parts2[2],
                        "Std. Deviation": parts2[3],
                        "Std. Error Mean": parts2[4] if len(parts2) > 4 else "",
                    }
                )

    return rows


def _extract_ttest_independent(text: str) -> list[dict[str, Any]]:
    """
    Extract the *Independent Samples Test* (独立样本检验) table from T-TEST LST output.

    Returns a list of rows with keys:
    ``F``, ``Sig.``, ``t``, ``df``, ``Sig. (2-tailed)``,
    ``Mean Difference``, ``Std. Error Difference``.

    Handles two variant row formats:
    - *Equal variances assumed* row (has Levene's F and Sig columns).
    - *Equal variances not assumed* row (only t-test columns).
    """
    title_pattern = re.compile(r"(?i)(Independent Samples Test|独立样本检验)", re.UNICODE)

    # Row for "Equal variances assumed" (includes Levene's columns).
    # NOTE: (?<!不) negative lookbehind prevents matching the substring
    # "假设方差相等" inside "不假设方差相等" (Chinese "not assumed").
    equal_var_pattern = re.compile(
        r"(?i)"
        r"(Equal variances assumed|(?<!不)假设方差相等)"
        r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"  # F, Sig. (Levene)
        r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"  # t, df
        r"\s{2,}([\d.eE\-]+)"  # Sig. (2-tailed)
        r"(?:\s{2,}([\d.,\-]+))?"  # Mean Difference (optional)
        r"(?:\s{2,}([\d.,\-]+))?",  # Std. Error Difference (optional)
        re.UNICODE,
    )

    # Row for "Equal variances not assumed" (no Levene's columns).
    # Chinese: 不假设方差相等
    unequal_var_pattern = re.compile(
        r"(?i)"
        r"(Equal variances not assumed|不假设方差相等)"
        r"\s{2,}([\d.,\-]+)"  # t
        r"\s{2,}([\d.,\-]+)"  # df
        r"\s{2,}([\d.eE\-]+)"  # Sig. (2-tailed)
        r"(?:\s{2,}([\d.,\-]+))?"  # Mean Difference (optional)
        r"(?:\s{2,}([\d.,\-]+))?",  # Std. Error Difference (optional)
        re.UNICODE,
    )

    block = _extract_table_block(text, title_pattern)
    if not block:
        return []

    rows: list[dict[str, Any]] = []

    # --- Equal variances assumed ---
    for m in equal_var_pattern.finditer(block):
        rows.append(
            {
                "": m.group(1).strip(),
                "F": m.group(2),
                "Sig.": m.group(3),
                "t": m.group(4),
                "df": m.group(5),
                "Sig. (2-tailed)": m.group(6),
                "Mean Difference": m.group(7) or "",
                "Std. Error Difference": m.group(8) or "",
            }
        )

    # --- Equal variances not assumed ---
    for m in unequal_var_pattern.finditer(block):
        rows.append(
            {
                "": m.group(1).strip(),
                "t": m.group(2),
                "df": m.group(3),
                "Sig. (2-tailed)": m.group(4),
                "Mean Difference": m.group(5) or "",
                "Std. Error Difference": m.group(6) or "",
            }
        )

    return rows


def _extract_anova_lst(text: str) -> list[dict[str, Any]]:
    """Extract the ANOVA summary table from ONEWAY / UNIANOVA LST output.

    The ANOVA table contains between-groups and within-groups rows with
    Sum of Squares, df, Mean Square, F, and Sig.  We extract the
    between-groups (or first non-error) row for the key statistics.

    Supports both English and Chinese (Simplified) SPSS output.
    """
    title_pattern = re.compile(r"(?i)(ANOVA|主体间效应检验)", re.UNICODE)
    block = _extract_table_block(text, title_pattern)
    if not block:
        return []

    rows: list[dict[str, Any]] = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # SPSS fixed-width columns separated by 2+ spaces
        parts = re.split(r"\s{2,}", stripped)
        # ANOVA rows have 6 columns: Source, SS, df, MS, F, Sig
        # A data row must have at least 5 numeric-looking tokens
        if len(parts) >= 4:
            numeric_count = sum(1 for p in parts[1:] if re.match(r"^[\d.,\-eE]+$", p.strip()))
            if numeric_count >= 3:
                rows.append(
                    {
                        "Source": parts[0].strip(),
                        "Sum of Squares": parts[1].strip() if len(parts) > 1 else "",
                        "df": parts[2].strip() if len(parts) > 2 else "",
                        "Mean Square": parts[3].strip() if len(parts) > 3 else "",
                        "F": parts[4].strip() if len(parts) > 4 else "",
                        "Sig.": parts[5].strip() if len(parts) > 5 else "",
                    }
                )

    return rows


# ---------------------------------------------------------------------------


def parse_raw_lst(lst_text: str, analysis_type: str) -> AnalysisResult:
    """
    Parse raw SPSS ``.lst`` text output using regex-based extraction rules.

    Currently supports full extraction for:
    - **T-TEST**: Group Statistics + Independent Samples Test (Chinese & English).

    Other analysis types return an empty result with a note indicating
    limited support.

    Args:
        lst_text: Raw text content of the SPSS output listing (``.lst``).
        analysis_type: One of ``"T-TEST"``, ``"ANOVA"``, ``"REGRESSION"``,
            ``"CROSSTABS"``, ``"FREQUENCIES"``, ``"DESCRIPTIVES"``.

    Returns:
        ``AnalysisResult`` populated from regex extraction.

    Raises:
        ValueError: If *analysis_type* is not recognised.
    """
    analysis_type = analysis_type.upper()
    valid_types = {
        "T-TEST",
        "ANOVA",
        "REGRESSION",
        "CROSSTABS",
        "FREQUENCIES",
        "DESCRIPTIVES",
    }
    if analysis_type not in valid_types:
        raise ValueError(
            f"Unknown analysis_type '{analysis_type}'. "
            f"Must be one of: {', '.join(sorted(valid_types))}"
        )

    tables: list[TableResult] = []
    statistics: dict[str, Any] = {}
    notes: list[str] = []

    if analysis_type == "T-TEST":
        # ---- Group Statistics ----
        try:
            gs_rows = _extract_ttest_group_stats(lst_text)
            if gs_rows:
                tables.append(
                    TableResult(
                        title="Group Statistics",
                        rows=gs_rows,
                        source_format="regex_lst",
                    )
                )
                # Extract sample sizes from Group Statistics
                for row in gs_rows:
                    n = _safe_float(row.get("N"))
                    if n is not None:
                        statistics["n_valid"] = int(n)
                    mean = _safe_float(row.get("Mean"))
                    if mean is not None:
                        # Store first mean; use group label for disambiguation
                        group_label = row.get("Group", "")
                        if "mean" not in statistics:
                            statistics["mean"] = mean
                            statistics["mean_group"] = group_label
                        else:
                            # Second group mean
                            statistics.setdefault("mean_group2", mean)
                            if statistics.get("mean_group") != group_label:
                                statistics["mean_group2"] = mean
        except Exception as exc:
            notes.append(f"Failed to parse Group Statistics: {exc}")
            logger.warning("Group Statistics parse failure: %s", exc)

        # ---- Independent Samples Test ----
        try:
            it_rows = _extract_ttest_independent(lst_text)
            if it_rows:
                tables.append(
                    TableResult(
                        title="Independent Samples Test",
                        rows=it_rows,
                        source_format="regex_lst",
                    )
                )
                # Extract key statistics from the equal-variance row (preferred)
                for row in it_rows:
                    row_label = row.get("", "")
                    if "assumed" in row_label.lower() or "假设方差相等" in row_label:
                        t_val = _safe_float(row.get("t"))
                        df_val = _safe_float(row.get("df"))
                        p_val = _safe_float(row.get("Sig. (2-tailed)"))
                        f_val = _safe_float(row.get("F"))
                        sig_val = _safe_float(row.get("Sig."))

                        if t_val is not None:
                            statistics["t_value"] = t_val
                        if df_val is not None:
                            statistics["df"] = df_val
                        if p_val is not None:
                            statistics["p_value"] = p_val
                        if f_val is not None:
                            statistics["f_value"] = f_val
                        if sig_val is not None:
                            statistics.setdefault("levene_sig", sig_val)
                        break  # Prefer equal-variance row
                else:
                    # Fallback: use whatever row has values
                    for row in it_rows:
                        t_val = _safe_float(row.get("t"))
                        df_val = _safe_float(row.get("df"))
                        p_val = _safe_float(row.get("Sig. (2-tailed)"))
                        if t_val is not None:
                            statistics["t_value"] = t_val
                        if df_val is not None:
                            statistics["df"] = df_val
                        if p_val is not None:
                            statistics["p_value"] = p_val
                        break
        except Exception as exc:
            notes.append(f"Failed to parse Independent Samples Test: {exc}")
            logger.warning("Independent Samples Test parse failure: %s", exc)

    else:
        # ── ANOVA: extract F, p, df from between-groups table ──
        if analysis_type == "ANOVA":
            try:
                anova_rows = _extract_anova_lst(lst_text)
                if anova_rows:
                    tables.append(
                        TableResult(
                            title="ANOVA",
                            rows=anova_rows,
                            source_format="regex_lst",
                        )
                    )
                    for row in anova_rows:
                        f_val = _safe_float(row.get("F"))
                        p_val = _safe_float(row.get("Sig."))
                        df_val = _safe_float(row.get("df"))
                        if f_val is not None:
                            statistics["f_value"] = f_val
                        if p_val is not None:
                            statistics["p_value"] = p_val
                        if df_val is not None:
                            statistics["df"] = int(df_val)
                        break  # Use first meaningful row (between-groups)
            except Exception as exc:
                notes.append(f"Failed to parse ANOVA LST: {exc}")
                logger.warning("ANOVA LST parse failure: %s", exc)

        if not tables:
            notes.append(
                f"LST parsing for {analysis_type} is not yet fully implemented. "
                "Only T-TEST and ANOVA tables are currently extracted via regex."
            )

    if not tables:
        raise ValueError(
            f"No parseable tables found for analysis type '{analysis_type}' "
            "in the provided LST text"
        )

    # If we didn't get n_valid from Group Statistics, try to infer it
    if "n_valid" not in statistics:
        total_n = 0
        for table in tables:
            for row in table.rows:
                n = _safe_float(row.get("N"))
                if n is not None:
                    total_n += int(n)
        if total_n > 0:
            statistics["n_valid"] = total_n

    return AnalysisResult(
        analysis_type=analysis_type,
        tables=tables,
        statistics=statistics,
        notes=notes,
        parser_used="regex_lst",
    )
