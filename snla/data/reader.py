"""
SNLA Data Reader

Reads .sav (SPSS) and .csv files, extracting variable metadata
for the LLM pipeline. Raw data values are NEVER exposed to cloud APIs.
"""

import os

import pandas as pd

try:
    import pyreadstat
except ImportError:
    pyreadstat = None  # type: ignore[assignment]


def read_sav(file_path: str) -> tuple["pd.DataFrame", dict]:
    """
    Read an SPSS .sav file and extract metadata.

    Args:
        file_path: Path to .sav file

    Returns:
        (dataframe, metadata) tuple
        metadata: {
            "filename": str,
            "format": "sav",
            "row_count": int,
            "column_count": int,
            "file_path": str,
            "file_label": str | None,
            # Internal keys (consumed by extract_metadata):
            "_column_names": list[str],
            "_column_labels": list[str | None],
            "_variable_value_labels": dict,
        }

    Raises:
        FileNotFoundError: If file doesn't exist
        ImportError: If pyreadstat is not installed
        ValueError: If file is not a valid .sav file
    """
    if pyreadstat is None:
        raise ImportError("pyreadstat is not installed. Install it with: pip install pyreadstat")

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    df, meta = pyreadstat.read_sav(file_path)

    column_names = list(getattr(meta, "column_names", df.columns))
    column_labels = getattr(meta, "column_labels", None)
    value_labels = getattr(meta, "variable_value_labels", {})

    metadata = {
        "filename": os.path.basename(file_path),
        "format": "sav",
        "row_count": len(df),
        "column_count": len(df.columns),
        "file_path": os.path.abspath(file_path),
        "file_label": getattr(meta, "file_label", None),
        # Internal: passed through to extract_metadata
        "_column_names": column_names,
        "_column_labels": column_labels or [None] * len(column_names),
        "_variable_value_labels": value_labels,
    }

    return df, metadata


def read_csv(file_path: str, encoding: str = "utf-8") -> tuple["pd.DataFrame", dict]:
    """
    Read a CSV file and extract basic metadata.

    Args:
        file_path: Path to .csv file
        encoding: File encoding (default utf-8, try gbk for Chinese CSV files)

    Returns:
        (dataframe, metadata) tuple
        metadata: {
            "filename": str,
            "format": "csv",
            "row_count": int,
            "column_count": int,
            "file_path": str,
            "file_label": None,
        }

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If encoding fails after both utf-8 and gbk attempts
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    try:
        df = pd.read_csv(file_path, encoding=encoding)
    except UnicodeDecodeError:
        # Chinese CSV files often use GBK encoding
        try:
            df = pd.read_csv(file_path, encoding="gbk")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Failed to read CSV with utf-8 or gbk encoding: {file_path}") from exc

    metadata = {
        "filename": os.path.basename(file_path),
        "format": "csv",
        "row_count": len(df),
        "column_count": len(df.columns),
        "file_path": os.path.abspath(file_path),
        "file_label": None,
    }

    return df, metadata


def extract_metadata(df: "pd.DataFrame", meta: dict) -> dict:
    """
    Extract unified variable metadata from a dataframe and file metadata.

    Combines pandas dtypes with any pyreadstat variable metadata.
    The output is the canonical variable list format used throughout SNLA.

    Works for both .sav files (pyreadstat metadata available) and .csv files
    (metadata derived purely from pandas dtypes and column names).

    Args:
        df: Pandas DataFrame (from read_sav or read_csv)
        meta: File-level metadata dict (from read_sav or read_csv)

    Returns:
        Unified metadata dict:
        {
            "filename": str,
            "format": "sav" | "csv",
            "row_count": int,
            "column_count": int,
            "file_path": str,
            "file_label": str | None,
            "variables": [
                {
                    "name": str,
                    "type": "Numeric" | "String" | "Date",
                    "label": str,
                    "value_labels": dict | None,
                },
                ...
            ]
        }
    """
    variables = []

    # Pull pyreadstat data from internal keys if present (SAV files).
    # For CSV files these fall back to pandas-derived values.
    column_names = meta.get("_column_names", list(df.columns))
    column_labels = meta.get("_column_labels", [""] * len(df.columns))
    value_labels_dict = meta.get("_variable_value_labels", {})

    for i, col_name in enumerate(column_names):
        if col_name not in df.columns:
            continue

        # Determine type from pandas dtype
        dtype = df[col_name].dtype
        if pd.api.types.is_numeric_dtype(dtype):
            var_type = "Numeric"
        elif pd.api.types.is_datetime64_any_dtype(dtype):
            var_type = "Date"
        else:
            var_type = "String"

        # Get label (handle NaN, None, and non-string values gracefully)
        label = ""
        if i < len(column_labels):
            raw_label = column_labels[i]
            if isinstance(raw_label, str):
                label = raw_label
            else:
                try:
                    # NaN check (NaN != NaN)
                    if raw_label is not None and not (
                        isinstance(raw_label, float) and raw_label != raw_label
                    ):
                        label = str(raw_label)
                except Exception:
                    label = ""

        # Get value labels (if any) — ensure JSON-compatible keys
        value_labels = value_labels_dict.get(col_name)
        if value_labels is not None:
            value_labels = {str(k): v for k, v in value_labels.items()}

        variables.append(
            {
                "name": col_name,
                "type": var_type,
                "label": label,
                "value_labels": value_labels,
            }
        )

    result = dict(meta)
    # Drop internal keys before returning
    result.pop("_column_names", None)
    result.pop("_column_labels", None)
    result.pop("_variable_value_labels", None)
    result["variables"] = variables
    return result


def read_and_extract(file_path: str) -> dict:
    """
    Convenience function: read a file and extract metadata in one call.

    Auto-detects format by file extension (.sav or .csv).

    Args:
        file_path: Path to .sav or .csv file

    Returns:
        Unified metadata dict with variables list

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format is not supported
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".sav":
        df, file_meta = read_sav(file_path)
    elif ext == ".csv":
        df, file_meta = read_csv(file_path)
    else:
        raise ValueError(f"Unsupported file format: {ext}. Expected .sav or .csv")

    return extract_metadata(df, file_meta)
