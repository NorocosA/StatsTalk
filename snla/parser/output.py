"""
SPSS output parser — OMS XML (primary) and LST regex (fallback) parsing strategies.

Provides structured extraction of analysis results from either:
1. OMS XML files (structured, cross-version consistent)
2. Raw SPSS ``.lst`` text output (fallback, using regex + fixed-position extraction)

Supports both Chinese and English SPSS output.
"""

import logging
import os
import re
from typing import Any

from snla.parser.schema import AnalysisResult, TableResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional lxml import
# ---------------------------------------------------------------------------

try:
    from lxml import etree

    HAS_LXML = True
except ImportError:  # pragma: no cover
    etree = None  # type: ignore[assignment]
    HAS_LXML = False

# SPSS OMS XML namespace (IBM-specific)
OMS_NS = "http://www.ibm.com/software/analytics/spss/xml/oms"

# ---------------------------------------------------------------------------
# Bilingual table title mapping (Chinese → English)
# ---------------------------------------------------------------------------

TITLE_MAP_ZH_EN: dict[str, str] = {
    "组统计": "Group Statistics",
    "独立样本检验": "Independent Samples Test",
    "主体间效应检验": "Tests of Between-Subjects Effects",
    "系数": "Coefficients",
    "模型摘要": "Model Summary",
    "卡方检验": "Chi-Square Tests",
    "交叉表": "Crosstabulation",
    "统计": "Statistics",
    "描述统计": "Descriptive Statistics",
    "个案处理摘要": "Case Processing Summary",
    "频率": "Frequency",
}

# ---------------------------------------------------------------------------
# Numeric helper
# ---------------------------------------------------------------------------


def _safe_float(val: Any) -> float | None:
    """
    Safely parse an SPSS numeric string to ``float``, handling locale variations.

    SPSS output may use comma as thousands separator (``1,234.56``) or as decimal
    separator (``1234,56``) depending on locale. This function normalises both.

    It also handles:
    - Leading/trailing whitespace
    - Non-breaking spaces (``\\xa0``)
    - Missing values (``"a"``, ``"—"``, empty strings) — returns ``None``
    - Scientific notation (``"1.23E-4"``)
    - Leading-dot notation (``".021"`` → ``0.021``)

    Args:
        val: The value to parse. Strings, ints, and floats are acceptable.

    Returns:
        The parsed float, or ``None`` if the value cannot be parsed.
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if not isinstance(val, str):
        try:
            val = str(val)
        except Exception:
            return None

    val = val.strip()
    if not val:
        return None

    # Remove non-breaking spaces and thin spaces
    val = val.replace("\xa0", "").replace("\u2009", "")

    # Replace typographic minus with ASCII hyphen
    val = val.replace("−", "-").replace("–", "-")

    # SPSS sometimes uses "a" or "." or "-" or "—" for missing values
    if val in (".", "-", "—", "–", "a", "A", "..."):
        return None

    # --- Locale-aware number parsing ---
    # Strategy: remove whitespace, then decide comma/period role
    # Special-case: if val starts with '.', prepend '0'
    if val.startswith("."):
        val = "0" + val

    comma_count = val.count(",")
    period_count = val.count(".")

    if comma_count == 0 and period_count == 0:
        # Plain integer — nothing to normalise
        pass
    elif comma_count == 1 and period_count == 0:
        # Only comma — assume European decimal separator
        val = val.replace(",", ".")
    elif comma_count > 0 and period_count == 0:
        # Multiple commas — likely thousands separators, remove them
        # (e.g., "1,234,567")
        val = val.replace(",", "")
    elif comma_count > 0 and period_count >= 1:
        # Both present — determine role by position.
        # If the last comma appears after the last period, comma is decimal
        # (European: 1.234,56); otherwise period is decimal (US/UK: 1,234.56).
        if val.rfind(",") > val.rfind("."):
            # European: 1.234,56 → remove period thousands, replace comma decimal
            val = val.replace(".", "")
            val = val.replace(",", ".")
        else:
            # US/UK: 1,234.56 → remove comma thousands
            val = val.replace(",", "")
    # else period only — already valid float format

    # Strip any remaining non-numeric characters (keep . - e E for float syntax)
    # But allow leading minus/plus
    val = re.sub(r"[^\d.eE\-+]", "", val, flags=re.UNICODE)

    if not val or val in ("-", ".", "+", "-.", "+."):
        return None

    # Handle "1." or ".5" edge cases
    if val.endswith("."):
        val = val[:-1]
    if not val:
        return None

    try:
        return float(val)
    except ValueError:
        return None


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


# ===================================================================
# OMS XML parser (primary strategy)
# ===================================================================


def _get_cell_coordinates(
    cell_elem: Any,
) -> list[tuple[str, str]]:
    """
    Walk up the XML tree from a ``<cell>`` element to collect all ancestor
    ``<category>`` labels together with their enclosing dimension axis name.

    Returns a list of ``(axis_name, category_label)`` tuples ordered from
    outermost (root-ward) to innermost (cell-ward).

    Args:
        cell_elem: An lxml ``Element`` representing ``<cell>``.

    Returns:
        Ordered list of (axis, label) coordinate pairs.
    """
    coords: list[tuple[str, str]] = []
    parent = cell_elem.getparent() if hasattr(cell_elem, "getparent") else None

    while parent is not None:
        # Handle namespaced tags: strip namespace prefix for comparison
        tag = parent.tag.split("}")[-1] if "}" in (parent.tag or "") else (parent.tag or "")
        if tag == "category":
            label = parent.get("text", "")
            dim = parent.getparent()
            if dim is not None:
                dim_tag = dim.tag.split("}")[-1] if "}" in (dim.tag or "") else (dim.tag or "")
                if dim_tag == "dimension":
                    axis = dim.get("axis", "")
                    if axis and label:
                        coords.insert(0, (axis, label))
        if tag == "pivotTable":
            break
        parent = parent.getparent() if hasattr(parent, "getparent") else None

    return coords


def _infer_table_title(pivot_elem: Any) -> str:
    """
    Fallback: infer a table title from the first row or column category label
    when the ``<pivotTable text="...">`` attribute is missing.

    Args:
        pivot_elem: An lxml ``Element`` representing ``<pivotTable>``.

    Returns:
        A best-guess title string, or empty string if nothing is found.
    """
    for axis_name in ("row", "column"):
        for dim in pivot_elem.findall("{*}dimension"):
            if dim.get("axis") == axis_name:
                first_cat = dim.find("{*}category")
                if first_cat is not None:
                    return first_cat.get("text", "")
    return ""


def _parse_pivot_table(pivot_elem: Any) -> TableResult:
    """
    Parse a single ``<pivotTable>`` XML element into a ``TableResult``.

    **Algorithm**

    1. Collect the set of dimension axes and their category labels.
    2. Enumerate every ``<cell>`` descendant and build its coordinate map by
       walking ancestor ``<category>`` elements.
    3. Group cells by their "row" axis coordinate to form table rows; use
       the remaining axes (column, statistics, layer) to name columns.
    4. Order rows to match the dimension's natural category ordering.

    Args:
        pivot_elem: An lxml ``Element`` (``<pivotTable>`` node).

    Returns:
        A ``TableResult`` with rows populated from the dimensional structure.
    """
    title = pivot_elem.get("text", "") or _infer_table_title(pivot_elem)

    # --- Step 1: Collect dimension → category-labels map ---
    # dims[axis_label] = [category_label, ...]
    dims: dict[str, list[str]] = {}
    for dim_elem in pivot_elem.findall("{*}dimension"):
        axis = dim_elem.get("axis")
        if not axis:
            continue
        seen: list[str] = []
        # Use iter() to also collect nested category labels
        for cat in dim_elem.iter("{*}category"):
            label = cat.get("text", "")
            if label and label not in seen:
                seen.append(label)
        dims[axis] = seen

    # --- Step 2: Collect all cells with their dimensional coordinates ---
    cell_coords: list[tuple[dict[str, str], str]] = []
    for cell in pivot_elem.iter("{*}cell"):
        text = cell.get("text")
        if text is None:
            continue
        coord: dict[str, str] = {}
        for axis, label in _get_cell_coordinates(cell):
            coord[axis] = label
        cell_coords.append((coord, text))

    # --- Step 3: Determine which axis to treat as "row" for grouping ---
    row_axis: str = ""
    for candidate in ("row", "column", "variable", "layer", "statistics"):
        if candidate in dims:
            row_axis = candidate
            break
    if not row_axis and dims:
        row_axis = next(iter(dims))

    column_axes: list[str] = [a for a in dims if a != row_axis]
    row_labels: list[str] = dims.get(row_axis, [])

    # --- Step 4: Group cells by their row coordinate ---
    rows_by_label: dict[str, dict[str, str]] = {}
    for coord, val_str in cell_coords:
        row_key = coord.get(row_axis, "")
        if not row_key:
            # Cells without a row-coordinate get placed under an empty key
            row_key = ""
        if row_key not in rows_by_label:
            rows_by_label[row_key] = {}

        # Build a column header from the remaining coordinates
        col_parts: list[str] = []
        for ca in column_axes:
            ca_val = coord.get(ca)
            if ca_val:
                col_parts.append(ca_val)

        if not col_parts:
            col_header = "Value"
        else:
            col_header = " | ".join(col_parts)

        # Prefer non-empty values; don't overwrite with empty
        stripped = val_str.strip()
        if stripped:
            rows_by_label[row_key][col_header] = stripped

    # --- Step 5: Build ordered rows ---
    rows: list[dict[str, Any]] = []
    for label in row_labels:
        row: dict[str, Any] = {"": label}
        row_data = rows_by_label.get(label, {})
        row.update(row_data)
        rows.append(row)

    # Include any cells that had no matching row label
    for extra_key in rows_by_label:
        if extra_key not in row_labels and extra_key != "":
            row = {"": extra_key}
            row.update(rows_by_label[extra_key])
            rows.append(row)

    return TableResult(title=title, rows=rows, source_format="oms_xml")


def _determine_analysis_type(text: str) -> str:
    """
    Infer the SPSS analysis type from a command or table title text.

    Args:
        text: The ``text`` attribute of a ``<command>`` or ``<pivotTable>``.

    Returns:
        One of ``"T-TEST"``, ``"ANOVA"``, ``"REGRESSION"``, ``"CROSSTABS"``,
        ``"FREQUENCIES"``, ``"DESCRIPTIVES"``, or ``"UNKNOWN"``.
    """
    lower = text.lower()
    if "t-test" in lower or "t test" in lower or "独立样本" in lower:
        return "T-TEST"
    if "anova" in lower or "unianova" in lower or "主体间" in lower or "univariate" in lower:
        return "ANOVA"
    if "regression" in lower or "回归" in lower:
        return "REGRESSION"
    if "correlation" in lower or "相关" in lower:
        return "CORRELATIONS"
    if "crosstab" in lower or "交叉" in lower:
        return "CROSSTABS"
    if "frequenc" in lower or "频率" in lower:
        return "FREQUENCIES"
    if "descriptive" in lower or "描述" in lower or "descriptives" in lower:
        return "DESCRIPTIVES"
    return "UNKNOWN"


def _extract_statistics(tables: list[TableResult]) -> dict[str, Any]:
    """
    Extract key statistics from parsed table rows.

    Scans all table rows for well-known column names and aggregates them into
    a flat dictionary with snake_case keys.

    Args:
        tables: List of already-parsed ``TableResult`` objects.

    Returns:
        Dict of key statistic names → values (as Python floats).
    """
    stats: dict[str, Any] = {}

    for table in tables:
        title_lower = table.title.lower()

        for row in table.rows:
            for key, val in row.items():
                numeric = _safe_float(val)
                if numeric is None:
                    continue

                key_lower = key.lower().strip()

                # --- p-value / significance ---
                if "sig" in key_lower:
                    # Prefer 2-tailed over 1-tailed
                    if "双尾" in key_lower or "2-tail" in key_lower or "two-tail" in key_lower:
                        stats["p_value"] = numeric
                    elif "1-tail" in key_lower or "one-tail" in key_lower or "单尾" in key_lower:
                        # Only set if no 2-tailed p-value yet
                        stats.setdefault("p_value", numeric)
                    else:
                        # Generic "Sig." column
                        stats.setdefault("p_value", numeric)
                    continue

                if "p" == key_lower and numeric <= 1.0:
                    stats.setdefault("p_value", numeric)
                    continue

                # --- t-value ---
                if key_lower == "t":
                    stats["t_value"] = numeric
                    continue

                # --- F-value ---
                if key_lower == "f":
                    stats["f_value"] = numeric
                    continue

                # --- df ---
                if key_lower in ("df",) or key_lower.startswith("df"):
                    stats.setdefault("df", numeric)
                    continue

                # --- mean ---
                if ("mean" in key_lower and "square" not in key_lower and "difference" not in key_lower
                        and "error" not in key_lower) or "均值" in key_lower:
                    stats.setdefault("mean", numeric)
                    continue

                # --- chi-square ---
                if "chi" in key_lower or "卡方" in key_lower:
                    stats["chi_square"] = numeric
                    continue

                # --- R / R-squared (regression) ---
                if key_lower == "r" and "square" not in key_lower:
                    stats.setdefault("r", numeric)
                    continue
                if "r square" in key_lower or "r-sq" in key_lower or "r方" in key_lower:
                    stats["r_squared"] = numeric
                    continue

                # --- Proportion / Percentage (frequencies) ---
                if "percent" in key_lower or "百分比" in key_lower:
                    stats.setdefault("percent", numeric)
                    continue

    return stats


# ===================================================================
# Dedicated OMS XML extractors — per analysis type
# ===================================================================
# These bypass the generic recursive parser and directly navigate the
# OMS XML tree using knowledge of each analysis type's dimension structure.
# This is the approach recommended in Plan.md §3.5.2.


def _find_cell_number(category_elem) -> float | None:
    """Extract the ``number`` attribute from a ``<cell>`` inside a ``<category>``."""
    cell = category_elem.find("{*}cell")
    if cell is not None:
        num_str = cell.get("number")
        if num_str is not None:
            try:
                return float(num_str)
            except (ValueError, TypeError):
                pass
    return None


def _extract_ttest_from_oms(xml_path: str) -> dict[str, float]:
    """Extract T-TEST statistics directly from OMS XML structure.

    Returns dict with keys: n_male, n_female, mean_male, mean_female,
    sd_male, sd_female, t_value, df, p_value, mean_diff, levene_f, levene_p.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        subtype = pivot.get("subType", "")

        # ---- Group Statistics ----
        if subtype == "Group Statistics":
            for cat in pivot.iter("{*}category"):
                if cat.get("variable") == "true":
                    # This is the dependent variable container
                    continue
                label = cat.get("label", "")
                if not label:
                    continue
                # Find child dimension with column axis → Statistics
                col_dim = cat.find("{*}dimension")
                if col_dim is None:
                    continue
                label_lower = label.lower()
                if "female" in label_lower or "女" in label:
                    prefix = "female"
                    stats["label_b"] = label  # Female = group B
                elif "male" in label_lower or "男" in label:
                    prefix = "male"
                    stats["label_a"] = label  # Male = group A
                else:
                    prefix = "group"
                for stat_cat in col_dim.iter("{*}category"):
                    stat_name = stat_cat.get("text", "")
                    val = _find_cell_number(stat_cat)
                    if val is None:
                        continue
                    if stat_name == "N":
                        stats[f"n_{prefix}"] = int(val)
                    elif stat_name == "Mean":
                        stats[f"mean_{prefix}"] = val
                    elif stat_name == "Std. Deviation":
                        stats[f"sd_{prefix}"] = val

        # ---- Independent Samples Test ----
        if subtype == "Independent Samples Test":
            for cat in pivot.iter("{*}category"):
                text = cat.get("text", "")
                if text == "Equal variances assumed":
                    col_dim = cat.find("{*}dimension")
                    if col_dim is None:
                        continue
                    for group in col_dim.iter("{*}group"):
                        for stat_cat in group.iter("{*}category"):
                            sname = stat_cat.get("text", "")
                            val = _find_cell_number(stat_cat)
                            if val is None:
                                continue
                            if sname == "t":
                                stats["t_value"] = val
                            elif sname == "df":
                                stats["df"] = int(val)
                            elif sname == "Sig. (2-tailed)":
                                stats["p_value"] = val
                            elif sname == "Sig.":
                                stats["levene_p"] = val
                            elif sname == "F":
                                stats["levene_f"] = val
                            elif sname == "Mean Difference":
                                stats["mean_diff"] = val
                    break  # Only use equal variances assumed row

    # Compute n_valid
    if "n_male" in stats and "n_female" in stats:
        stats["n_valid"] = int(stats["n_male"] + stats["n_female"])

    return stats


def _extract_descriptives_from_oms(xml_path: str) -> dict[str, float]:
    """Extract DESCRIPTIVES statistics from OMS XML.

    Returns dict with keys: n, mean, std_dev, min, max for each variable.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        subtype = pivot.get("subType", "")
        if subtype != "Descriptive Statistics":
            continue

        for cat in pivot.iter("{*}category"):
            if cat.get("variable") != "true":
                continue
            var_name = cat.get("varName", "")
            col_dim = cat.find("{*}dimension")
            if col_dim is None:
                continue
            for stat_cat in col_dim.iter("{*}category"):
                sname = stat_cat.get("text", "")
                val = _find_cell_number(stat_cat)
                if val is None:
                    continue
                key = sname.lower().replace(" ", "_").replace(".", "")
                stats[key] = val
                if sname == "N":
                    stats["n_valid"] = int(val)
                elif sname == "Mean":
                    stats["mean"] = val
                elif sname == "Std. Deviation":
                    stats["std_dev"] = val

            # First variable only for summary stats
            break

    return stats


def _extract_correlations_from_oms(xml_path: str) -> dict[str, float]:
    """Extract CORRELATIONS statistics from OMS XML.

    The Correlations table has a complex nested structure:
    Variables → Statistics (Pearson/Sig/N) → Variables again.
    We scan all <cell> elements and look for the off-diagonal correlation value.

    Returns dict with keys: r, p_value, n_valid.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        if pivot.get("subType", "") != "Correlations":
            continue

        for cell in pivot.iter("{*}cell"):
            cell_text = cell.get("text", "")
            cell_num = cell.get("number")
            if cell_num is None:
                continue

            # Walk up to find what statistic this cell represents
            parent = cell.getparent()
            stat_name = ""
            while parent is not None:
                tag = parent.tag.split("}")[-1] if "}" in (parent.tag or "") else (parent.tag or "")
                if tag == "category":
                    txt = parent.get("text", "")
                    if txt in ("Pearson Correlation", "Sig. (2-tailed)", "N"):
                        stat_name = txt
                        break
                if tag == "pivotTable":
                    break
                parent = parent.getparent() if hasattr(parent, "getparent") else None

            try:
                val = float(cell_num)
            except (ValueError, TypeError):
                continue

            # Skip the diagonal (r=1.0 for self-correlation)
            if stat_name == "Pearson Correlation" and abs(val - 1.0) < 0.001:
                continue

            if stat_name == "Pearson Correlation":
                stats["r"] = val
            elif stat_name == "Sig. (2-tailed)":
                stats["p_value"] = val
            elif stat_name == "N":
                if "n_valid" not in stats:
                    stats["n_valid"] = int(val)

    return stats


def _extract_regression_from_oms(xml_path: str) -> dict[str, float]:
    """Extract REGRESSION statistics from OMS XML.

    Returns dict with keys: r_squared, f_value, p_value, b, beta, n_valid.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        subtype = pivot.get("subType", "")

        if subtype == "Model Summary":
            for cat in pivot.iter("{*}category"):
                text = cat.get("text", "")
                val = _find_cell_number(cat)
                if val is None:
                    continue
                if "R Square" in text:
                    stats["r_squared"] = val

        if subtype == "ANOVA":
            for cat in pivot.iter("{*}category"):
                text = cat.get("text", "")
                if text == "Regression":
                    col_dim = cat.find("{*}dimension")
                    if col_dim is None:
                        continue
                    for stat_cat in col_dim.iter("{*}category"):
                        sname = stat_cat.get("text", "")
                        val = _find_cell_number(stat_cat)
                        if val is None:
                            continue
                        if sname == "F":
                            stats["f_value"] = val
                        elif sname == "Sig.":
                            stats["p_value"] = val
                    break

        if subtype == "Coefficients":
            for cat in pivot.iter("{*}category"):
                if cat.get("variable") != "true":
                    continue
                label = cat.get("label", "")
                if label == "(Constant)":
                    continue  # Skip intercept
                col_dim = cat.find("{*}dimension")
                if col_dim is None:
                    continue
                for stat_cat in col_dim.iter("{*}category"):
                    sname = stat_cat.get("text", "")
                    val = _find_cell_number(stat_cat)
                    if val is None:
                        continue
                    if sname == "B":
                        stats["b"] = val
                    elif sname == "Beta":
                        stats["beta"] = val
                    elif sname == "t":
                        stats["t_value"] = val

    return stats


def _extract_anova_from_oms(xml_path: str) -> dict[str, float]:
    """Extract ANOVA (UNIANOVA) statistics from OMS XML.

    Navigates the 'Tests of Between-Subjects Effects' pivot table to
    find factor variable rows and extract F, p-value, and df.

    Returns dict with keys: f_value, p_value, df.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        if pivot.get("subType", "") != "Tests of Between-Subjects Effects":
            continue

        for cat in pivot.iter("{*}category"):
            if cat.get("variable") != "true":
                continue

            col_dim = cat.find("{*}dimension")
            if col_dim is None:
                continue

            for stat_cat in col_dim.iter("{*}category"):
                sname = stat_cat.get("text", "")
                val = _find_cell_number(stat_cat)
                if val is None:
                    continue
                if sname == "F":
                    stats["f_value"] = val
                elif sname == "Sig.":
                    stats["p_value"] = val
                elif sname == "df":
                    stats["df"] = int(val)

    return stats


def _extract_crosstabs_from_oms(xml_path: str) -> dict[str, float]:
    """Extract CROSSTABS statistics from OMS XML.

    Navigates the 'Chi-Square Tests' pivot table to find the
    'Pearson Chi-Square' row and extract Value, df, and p-value.

    Returns dict with keys: chi_square, p_value, df.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    for pivot in root.iter("{*}pivotTable"):
        if pivot.get("subType", "") != "Chi-Square Tests":
            continue

        for cat in pivot.iter("{*}category"):
            if cat.get("text", "") != "Pearson Chi-Square":
                continue

            col_dim = cat.find("{*}dimension")
            if col_dim is None:
                continue

            for stat_cat in col_dim.iter("{*}category"):
                sname = stat_cat.get("text", "")
                val = _find_cell_number(stat_cat)
                if val is None:
                    continue
                if sname == "Value":
                    stats["chi_square"] = val
                elif sname == "df":
                    stats["df"] = int(val)
                elif sname == "Asymptotic Significance (2-sided)":
                    stats["p_value"] = val

    return stats


# Map analysis types to their dedicated extractors
_DEDICATED_EXTRACTORS: dict[str, Any] = {
    "T-TEST": _extract_ttest_from_oms,
    "DESCRIPTIVES": _extract_descriptives_from_oms,
    "CORRELATIONS": _extract_correlations_from_oms,
    "REGRESSION": _extract_regression_from_oms,
    "FREQUENCIES": None,  # Uses generic parser
    "CROSSTABS": _extract_crosstabs_from_oms,
    "ANOVA": _extract_anova_from_oms,
}


# ---------------------------------------------------------------------------


def parse_oms_xml(xml_path: str) -> AnalysisResult:
    """
    Parse an SPSS OMS XML output file into a structured ``AnalysisResult``.

    Uses ``lxml`` to parse the XML, traverses ``<command>`` and ``<pivotTable>``
    elements, and extracts dimensional coordinates to build table rows.

    **Key characteristics**

    - Recursively walks all ``<dimension>`` elements to build an index tree.
    - Extracts statistic values from axis-annotated categories.
    - Handles multi-layer table headers (e.g., multi-factor ANOVA).
    - Collects key statistics (p_value, t_value, f_value, df, chi_square, …)
      into a flat dictionary for convenient access.

    Args:
        xml_path: Path to the OMS XML output file (``.xml``).

    Returns:
        ``AnalysisResult`` populated from the XML structure.

    Raises:
        FileNotFoundError: If *xml_path* does not exist.
        RuntimeError: If ``lxml`` is not installed.
        ValueError: If the XML is malformed or contains no parseable tables.
    """
    if not HAS_LXML:
        raise RuntimeError(
            "lxml is required for OMS XML parsing. "
            "Install it with: pip install lxml"
        )
    if not os.path.exists(xml_path):
        raise FileNotFoundError(f"OMS XML file not found: {xml_path}")

    try:
        tree = etree.parse(xml_path)
    except Exception as exc:
        raise ValueError(f"Failed to parse XML file {xml_path}: {exc}") from exc

    root = tree.getroot()
    if root is None:
        raise ValueError(f"Empty XML document: {xml_path}")

    # --- Determine analysis type ---
    analysis_type = "UNKNOWN"
    for cmd_elem in root.iter("{*}command"):
        cmd_text = cmd_elem.get("text", "")
        inferred = _determine_analysis_type(cmd_text)
        if inferred != "UNKNOWN":
            analysis_type = inferred
            break

    # --- Parse every pivot table ---
    tables: list[TableResult] = []
    notes: list[str] = []

    for pivot in root.iter("{*}pivotTable"):
        try:
            table = _parse_pivot_table(pivot)
            tables.append(table)
        except Exception as exc:
            msg = f"Failed to parse a pivot table ({exc})"
            notes.append(msg)
            logger.warning(msg)

    if not tables:
        raise ValueError(
            f"No parseable pivot tables found in {xml_path}"
        )

    # --- Extract key statistics (dedicated extractor preferred) ---
    extractor = _DEDICATED_EXTRACTORS.get(analysis_type)
    if extractor is not None:
        try:
            statistics = extractor(xml_path)
        except Exception as exc:
            notes.append(
                f"Dedicated extractor for {analysis_type} failed: {exc}"
            )
            statistics = _extract_statistics(tables)
    else:
        statistics = _extract_statistics(tables)

    return AnalysisResult(
        analysis_type=analysis_type,
        tables=tables,
        statistics=statistics,
        n_valid=0,
        n_missing=0,
        notes=notes,
        raw_output_path=xml_path,
        parser_used="oms_xml",
    )


# ===================================================================
# LST text parser — fallback strategy
# ===================================================================


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
    title_pattern = re.compile(
        r"(?i)(Group Statistics|组统计)", re.UNICODE
    )

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
    title_pattern = re.compile(
        r"(?i)(Independent Samples Test|独立样本检验)", re.UNICODE
    )

    # Row for "Equal variances assumed" (includes Levene's columns).
    # NOTE: (?<!不) negative lookbehind prevents matching the substring
    # "假设方差相等" inside "不假设方差相等" (Chinese "not assumed").
    equal_var_pattern = re.compile(
        r"(?i)"
        r"(Equal variances assumed|(?<!不)假设方差相等)"
        r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"  # F, Sig. (Levene)
        r"\s{2,}([\d.,\-]+)\s{2,}([\d.,\-]+)"  # t, df
        r"\s{2,}([\d.eE\-]+)"                    # Sig. (2-tailed)
        r"(?:\s{2,}([\d.,\-]+))?"                 # Mean Difference (optional)
        r"(?:\s{2,}([\d.,\-]+))?",                # Std. Error Difference (optional)
        re.UNICODE,
    )

    # Row for "Equal variances not assumed" (no Levene's columns).
    # Chinese: 不假设方差相等
    unequal_var_pattern = re.compile(
        r"(?i)"
        r"(Equal variances not assumed|不假设方差相等)"
        r"\s{2,}([\d.,\-]+)"                     # t
        r"\s{2,}([\d.,\-]+)"                     # df
        r"\s{2,}([\d.eE\-]+)"                    # Sig. (2-tailed)
        r"(?:\s{2,}([\d.,\-]+))?"                # Mean Difference (optional)
        r"(?:\s{2,}([\d.,\-]+))?",               # Std. Error Difference (optional)
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
        notes.append(
            f"LST parsing for {analysis_type} is not yet fully implemented. "
            "Only T-TEST tables are currently extracted via regex."
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


# ===================================================================
# Unified entry point
# ===================================================================


def parse(
    oms_xml_path: str | None = None,
    lst_text: str | None = None,
    analysis_type: str | None = None,
) -> AnalysisResult:
    """
    Unified parser entry point — tries **OMS XML** first, falls back to **LST regex**.

    **Priority**

    1. **OMS XML** (``.xml``) — structured, cross-version consistent, preferred.
    2. **LST regex** (``.lst`` text) — fallback for simple or unstructured output.

    If ``lxml`` is not installed, OMS XML parsing is silently skipped and the
    function falls through to LST parsing.

    Args:
        oms_xml_path: Path to an OMS XML output file (``.xml``).
        lst_text: Raw SPSS listing text (``.lst`` file content).
        analysis_type: Required for LST parsing; one of ``"T-TEST"``,
            ``"ANOVA"``, ``"REGRESSION"``, ``"CROSSTABS"``,
            ``"FREQUENCIES"``, ``"DESCRIPTIVES"``.

    Returns:
        ``AnalysisResult`` parsed from the best available source.

    Raises:
        ValueError: If no parsable source is available, or parsing fails
            on all available sources.
    """
    last_error: Exception | None = None

    # --- 1. Try OMS XML (primary strategy) ---
    if oms_xml_path and os.path.exists(oms_xml_path):
        if HAS_LXML:
            try:
                result = parse_oms_xml(oms_xml_path)
                logger.info("Successfully parsed OMS XML: %s", oms_xml_path)
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "OMS XML parsing failed for %s: %s. Falling back to LST.",
                    oms_xml_path,
                    exc,
                )
        else:
            logger.info(
                "lxml not available; skipping OMS XML parsing for %s.",
                oms_xml_path,
            )

    # --- 2. Fall back to LST regex ---
    if lst_text and analysis_type:
        try:
            result = parse_raw_lst(lst_text, analysis_type)
            logger.info(
                "Successfully parsed LST text (analysis_type=%s).",
                analysis_type,
            )
            return result
        except Exception as exc:
            last_error = exc
            logger.warning(
                "LST parsing failed for analysis_type=%s: %s",
                analysis_type,
                exc,
            )

    if last_error is not None:
        raise ValueError(
            "All parsing strategies failed. See log for details."
        ) from last_error

    raise ValueError(
        "No parsable SPSS output available. Provide either "
        "oms_xml_path (to an existing file) or lst_text + analysis_type."
    )


__all__ = [
    "parse",
    "parse_oms_xml",
    "parse_raw_lst",
    "_safe_float",
    "TITLE_MAP_ZH_EN",
    "EXTRACTION_RULES",
    "HAS_LXML",
]
