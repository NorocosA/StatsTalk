"""
Schema definitions for parsed SPSS output.

This module defines the dataclass hierarchy used to represent
parsed SPSS analysis results throughout the SNLA system.

Consumed by:
    - parser/output.py (the actual parser)
    - explainer/naturalize.py (the result explainer)
"""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TableResult:
    """Represents a single output table from SPSS.

    Examples: "Group Statistics", "Independent Samples Test",
    "ANOVA", "Coefficients".

    Attributes:
        title: Table title as it appears in SPSS output.
        rows: Parsed rows, each as a dict of column_name → value.
        notes: Footnotes or annotations attached to the table.
        source_format: How this table was parsed.
    """

    title: str
    """Table title (e.g., "Group Statistics", "Independent Samples Test")."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    """Each row as a dict of column_name → value."""

    notes: list[str] = field(default_factory=list)
    """Footnotes or annotations from SPSS output."""

    source_format: str = "oms_xml"
    """How this table was parsed: "oms_xml" | "regex_lst" | "llm_fallback"."""


@dataclass
class AnalysisResult:
    """Represents the complete parsed output of one SPSS analysis.

    Aggregates all tables, key statistics, and metadata produced
    by a single SPSS procedure invocation.

    Attributes:
        analysis_type: Type of analysis performed.
        tables: All tables produced by this analysis.
        statistics: Flattened key stats for easy access (snake_case keys).
        n_valid: Number of valid (non-missing) cases.
        n_missing: Number of missing cases.
        notes: Global notes or warnings from the analysis.
        raw_output_path: Path to the original SPSS output file, if available.
        parser_used: Which parser succeeded.
    """

    analysis_type: str = "UNKNOWN"
    """Type of analysis performed (e.g., "T-TEST", "FREQUENCIES")."""

    tables: list[TableResult] = field(default_factory=list)
    """All tables produced by this analysis."""

    statistics: dict[str, Any] = field(default_factory=dict)
    """Flattened key statistics for easy access (snake_case keys).

    Example: {"p_value": 0.021, "t_value": 2.34,
              "mean_group1": 79.5, "mean_group2": 84.2}
    """

    n_valid: int = 0
    """Number of valid (non-missing) cases."""

    n_missing: int = 0
    """Number of missing cases."""

    notes: list[str] = field(default_factory=list)
    """Global notes or warnings from the analysis."""

    raw_output_path: str | None = None
    """Path to the original SPSS output file, if available."""

    parser_used: str = "oms_xml"
    """Which parser succeeded: "oms_xml" | "regex_lst" | "llm_fallback"."""


def analysis_result_to_dict(result: AnalysisResult) -> dict:
    """Convert an AnalysisResult to a plain dict for JSON serialization.

    Uses dataclasses.asdict() for deep conversion of nested dataclasses.

    Args:
        result: The AnalysisResult instance to convert.

    Returns:
        A plain Python dict suitable for json.dumps().
    """
    return asdict(result)


def dict_to_analysis_result(data: dict) -> AnalysisResult:
    """Reconstruct an AnalysisResult from a plain dict.

    Inverse of analysis_result_to_dict. Handles nested TableResult
    reconstruction automatically.

    Args:
        data: A dict previously produced by analysis_result_to_dict(),
              or conforming to the same shape.

    Returns:
        A fully populated AnalysisResult instance.
    """
    tables = [TableResult(**tbl) for tbl in data.get("tables", [])]
    return AnalysisResult(
        analysis_type=data.get("analysis_type", "UNKNOWN"),
        tables=tables,
        statistics=data.get("statistics", {}),
        n_valid=data.get("n_valid", 0),
        n_missing=data.get("n_missing", 0),
        notes=data.get("notes", []),
        raw_output_path=data.get("raw_output_path"),
        parser_used=data.get("parser_used", "oms_xml"),
    )
