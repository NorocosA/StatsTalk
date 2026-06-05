"""
OMS XML parser — primary parsing strategy for SPSS output.

Parses SPSS OMS XML files using lxml, with dedicated extractors for each
analysis type (T-TEST, ANOVA, REGRESSION, CORRELATIONS, CROSSTABS,
FREQUENCIES, DESCRIPTIVES).
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
    if (
        "anova" in lower
        or "unianova" in lower
        or "oneway" in lower
        or "主体间" in lower
        or "univariate" in lower
    ):
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

                if key_lower == "p" and numeric <= 1.0:
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
                if (
                    "mean" in key_lower
                    and "square" not in key_lower
                    and "difference" not in key_lower
                    and "error" not in key_lower
                ) or "均值" in key_lower:
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
    """Extract ANOVA (ONEWAY / UNIANOVA) statistics from OMS XML.

    Navigates the ANOVA pivot table to find the 'Between Groups' row
    and extract F, p-value, and df.

    Handles two subType values:
    - ``"ANOVA"`` — produced by the ONEWAY command
    - ``"Tests of Between-Subjects Effects"`` — produced by UNIANOVA/GLM

    Returns dict with keys: f_value, p_value, df.
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, float] = {}

    # Supported subType values for ANOVA tables
    anova_subtypes = {"ANOVA", "Tests of Between-Subjects Effects"}

    for pivot in root.iter("{*}pivotTable"):
        subtype = pivot.get("subType", "")
        if subtype not in anova_subtypes:
            continue

        # Walk all category elements to find "Between Groups" row
        for cat in pivot.iter("{*}category"):
            text = cat.get("text", "")

            # ONEWAY: "Source" dimension with "Between Groups" label
            # UNIANOVA: variable="true" categories
            if text == "Between Groups" or cat.get("variable") == "true":
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
                    elif sname == "df" and "df" not in stats:
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


def _extract_frequencies_from_oms(xml_path: str) -> dict[str, Any]:
    """Extract FREQUENCIES statistics from OMS XML.

    Handles both numeric (with value labels) and string variables.
    SPSS FREQUENCIES outputs a multi-dimensional pivot table where
    category labels and statistic names are interleaved in the row
    dimension, with separate column dimensions for each category.

    Returns dict with keys: n_valid, n_missing, categories (list of
    {label, frequency, percent, valid_percent, cumulative_percent}).
    """
    tree = etree.parse(xml_path)
    root = tree.getroot()
    stats: dict[str, Any] = {"n_valid": 0, "n_missing": 0, "categories": []}

    # Phase 1: Extract N from "Statistics" table
    for pivot in root.iter("{*}pivotTable"):
        if pivot.get("subType", "") != "Statistics":
            continue
        for dim in pivot.iter("{*}dimension"):
            if dim.get("axis") != "row":
                continue
            for cat in dim.iter("{*}category"):
                text = cat.get("text", "").strip()
                val = _find_cell_number(cat)
                if text == "Valid" and val is not None:
                    stats["n_valid"] = int(val)
                elif text == "Missing" and val is not None:
                    stats["n_missing"] = int(val)

    # Phase 2: Extract frequency data from "Frequencies" table
    for pivot in root.iter("{*}pivotTable"):
        if pivot.get("subType", "") != "Frequencies":
            continue

        # Collect row-dimension categories with their cells
        row_cats: list[dict] = []
        for dim in pivot.iter("{*}dimension"):
            if dim.get("axis") != "row":
                continue
            for cat in dim.iter("{*}category"):
                text = cat.get("text", "").strip()
                if not text:
                    continue
                cells = []
                for cell in cat.iter("{*}cell"):
                    ctext = cell.get("text", "")
                    cnum = cell.get("number")
                    cells.append(ctext if ctext else (cnum if cnum else ""))
                row_cats.append({"text": text, "cells": cells})

        # Identify category labels (have >1 cell)
        cat_labels = [rc for rc in row_cats if len(rc["cells"]) > 1]
        stat_labels = [rc for rc in row_cats if len(rc["cells"]) == 1]

        if not cat_labels:
            continue

        # Collect unique stat names in order
        stat_names = []
        seen = set()
        for sl in stat_labels:
            if sl["text"] not in seen:
                stat_names.append(sl["text"])
                seen.add(sl["text"])
            if len(stat_names) >= 4:
                break
        if not stat_names:
            stat_names = ["Frequency", "Percent", "Valid Percent", "Cumulative Percent"]

        num_stats = len(stat_names)
        idx = 0
        while idx < len(row_cats):
            rc = row_cats[idx]
            if len(rc["cells"]) > 1 and rc["text"] not in ("Total",):
                cat_entry: dict[str, Any] = {"label": rc["text"]}
                for si in range(num_stats):
                    if idx + 1 + si < len(row_cats):
                        src = row_cats[idx + 1 + si]
                        if src["cells"]:
                            val = _safe_float(src["cells"][0])
                            if val is not None and si < len(stat_names):
                                cat_entry[stat_names[si]] = val
                if "Frequency" in cat_entry:
                    stats["categories"].append(cat_entry)
                idx += 1 + num_stats
            elif rc["text"] == "Total":
                total_entry: dict[str, Any] = {"label": "Total"}
                for si in range(num_stats):
                    if idx + 1 + si < len(row_cats):
                        src = row_cats[idx + 1 + si]
                        if src["cells"]:
                            val = _safe_float(src["cells"][0])
                            if val is not None and si < len(stat_names):
                                total_entry[stat_names[si]] = val
                if "Frequency" in total_entry:
                    stats["total"] = total_entry
                idx += 1 + num_stats
            else:
                idx += 1

        break  # Only first Frequencies table

    return stats


# Map analysis types to their dedicated extractors
_DEDICATED_EXTRACTORS: dict[str, Any] = {
    "T-TEST": _extract_ttest_from_oms,
    "DESCRIPTIVES": _extract_descriptives_from_oms,
    "CORRELATIONS": _extract_correlations_from_oms,
    "REGRESSION": _extract_regression_from_oms,
    "FREQUENCIES": _extract_frequencies_from_oms,
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
            "lxml is required for OMS XML parsing. Install it with: pip install lxml"
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
        raise ValueError(f"No parseable pivot tables found in {xml_path}")

    # --- Extract key statistics (dedicated extractor preferred) ---
    extractor = _DEDICATED_EXTRACTORS.get(analysis_type)
    if extractor is not None:
        try:
            statistics = extractor(xml_path)
            # Fallback: if dedicated extractor produced empty stats, use generic
            if not statistics:
                logger.debug(
                    "Dedicated extractor for %s returned empty, using generic", analysis_type
                )
                statistics = _extract_statistics(tables)
        except Exception as exc:
            notes.append(f"Dedicated extractor for {analysis_type} failed: {exc}")
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
