"""
SPSS output parser — OMS XML (primary) and LST regex (fallback) parsing strategies.

Provides structured extraction of analysis results from either:
1. OMS XML files (structured, cross-version consistent)
2. Raw SPSS ``.lst`` text output (fallback, using regex + fixed-position extraction)

Supports both Chinese and English SPSS output.
"""

import logging
import os

from snla.parser._lst import (
    EXTRACTION_RULES,
    parse_raw_lst,
)
from snla.parser._oms import (
    HAS_LXML,
    TITLE_MAP_ZH_EN,
    _safe_float,
    parse_oms_xml,
)
from snla.parser.schema import AnalysisResult

logger = logging.getLogger(__name__)


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
            # Skip OMS XML if file is too small (e.g., ONEWAY produces 15-byte empty XML)
            if os.path.getsize(oms_xml_path) < 100:
                logger.warning(
                    "OMS XML too small (%d bytes), falling back to LST.",
                    os.path.getsize(oms_xml_path),
                )
            else:
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
        raise ValueError("All parsing strategies failed. See log for details.") from last_error

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
