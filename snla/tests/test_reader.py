"""Tests for snla.data.reader — 8 test cases covering .sav reading, .csv reading,
metadata extraction, error handling, and the convenience read_and_extract function."""

import os
import tempfile

import pandas as pd
import pytest

from snla.data.reader import extract_metadata, read_and_extract, read_csv, read_sav

# =========================================================================
# Test 1: read_and_extract — .sav file
# =========================================================================


def test_read_and_extract_sav():
    """Verify read_and_extract works with the project's test_data.sav fixture."""
    test_file = os.path.join(os.path.dirname(__file__), "fixtures", "test_data.sav")
    if not os.path.exists(test_file):
        pytest.skip("test_data.sav fixture not found")

    result = read_and_extract(test_file)

    assert result["format"] == "sav", f"Expected format='sav', got {result['format']}"
    assert result["row_count"] > 0, "Expected non-zero row count"
    assert result["column_count"] > 0, "Expected non-zero column count"
    assert "variables" in result, "Expected 'variables' key in result"
    assert len(result["variables"]) == result["column_count"], (
        f"Expected {result['column_count']} variables, got {len(result['variables'])}"
    )

    # Verify variable structure
    for var in result["variables"]:
        assert "name" in var, f"Variable missing 'name': {var}"
        assert "type" in var, f"Variable missing 'type': {var}"
        assert "label" in var, f"Variable missing 'label': {var}"
        assert "value_labels" in var, f"Variable missing 'value_labels': {var}"
        assert var["type"] in ("Numeric", "String", "Date"), (
            f"Unexpected variable type '{var['type']}' for {var['name']}"
        )

    # Verify specific known variables from test_data.sav
    var_names = [v["name"] for v in result["variables"]]
    assert "gender" in var_names, f"Expected 'gender' in variables, got {var_names}"
    assert "score" in var_names, f"Expected 'score' in variables, got {var_names}"


# =========================================================================
# Test 2: read_and_extract — .csv file
# =========================================================================


def test_read_and_extract_csv():
    """Verify read_and_extract works with a simple CSV file."""
    csv_content = "name,age,score\nAlice,25,88.5\nBob,30,92.0\nCharlie,22,79.3\n"

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, encoding="utf-8")
    try:
        tmp.write(csv_content)
        tmp.close()

        result = read_and_extract(tmp.name)

        assert result["format"] == "csv", f"Expected format='csv', got {result['format']}"
        assert result["row_count"] == 3, f"Expected 3 rows, got {result['row_count']}"
        assert result["column_count"] == 3, f"Expected 3 columns, got {result['column_count']}"

        var_names = [v["name"] for v in result["variables"]]
        assert "name" in var_names
        assert "age" in var_names
        assert "score" in var_names

        # CSV files should have no value_labels
        for v in result["variables"]:
            assert v["value_labels"] is None, (
                f"CSV variables should have no value_labels, got {v['value_labels']} for {v['name']}"
            )

        # Type inference: age and score should be Numeric, name should be String
        age_var = next(v for v in result["variables"] if v["name"] == "age")
        assert age_var["type"] == "Numeric", f"age should be Numeric, got {age_var['type']}"

        name_var = next(v for v in result["variables"] if v["name"] == "name")
        assert name_var["type"] == "String", f"name should be String, got {name_var['type']}"

    finally:
        os.unlink(tmp.name)


# =========================================================================
# Test 3: read_sav — missing pyreadstat
# =========================================================================


def test_read_sav_missing_pyreadstat(monkeypatch):
    """Verify read_sav raises ImportError when pyreadstat is not installed."""
    import snla.data.reader as reader_mod

    monkeypatch.setattr(reader_mod, "pyreadstat", None)

    with pytest.raises(ImportError) as excinfo:
        read_sav("nonexistent.sav")
    assert "pyreadstat" in str(excinfo.value).lower(), (
        f"Expected ImportError mentioning pyreadstat, got: {excinfo.value}"
    )


# =========================================================================
# Test 4: read_sav — file not found
# =========================================================================


def test_read_sav_file_not_found():
    """Verify read_sav raises FileNotFoundError for non-existent file."""
    with pytest.raises(FileNotFoundError) as excinfo:
        read_sav("nonexistent_file_12345.sav")
    assert "nonexistent_file_12345.sav" in str(excinfo.value), (
        f"Expected error mentioning the file path, got: {excinfo.value}"
    )


# =========================================================================
# Test 5: read_csv — file not found
# =========================================================================


def test_read_csv_file_not_found():
    """Verify read_csv raises FileNotFoundError for non-existent file."""
    with pytest.raises(FileNotFoundError) as excinfo:
        read_csv("nonexistent_file_67890.csv")
    assert "nonexistent_file_67890.csv" in str(excinfo.value), (
        f"Expected error mentioning the file path, got: {excinfo.value}"
    )


# =========================================================================
# Test 6: read_csv — encoding fallback (gbk)
# =========================================================================


def test_read_csv_encoding_fallback(monkeypatch):
    """Verify read_csv tries gbk encoding when utf-8 fails."""
    # Create a file with bytes that are valid in gbk but invalid in utf-8
    # We simulate by patching pd.read_csv to fail on first call with UnicodeDecodeError
    import snla.data.reader as reader_mod

    fake_path = "fake_csv_for_encoding_test.csv"

    call_count = [0]

    def _mock_read_csv(filepath_or_buffer, encoding="utf-8", **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "fake error")
        # Second call (gbk) succeeds
        return pd.DataFrame({"col1": [1, 2, 3]})

    monkeypatch.setattr(reader_mod.pd, "read_csv", _mock_read_csv)
    monkeypatch.setattr(reader_mod.os.path, "exists", lambda p: True)

    df, meta = read_csv(fake_path)
    assert call_count[0] == 2, f"Expected 2 calls (utf-8 fail → gbk succeed), got {call_count[0]}"
    assert isinstance(df, pd.DataFrame)
    assert meta["format"] == "csv"


# =========================================================================
# Test 7: read_and_extract — unsupported format
# =========================================================================


def test_read_and_extract_unsupported_format():
    """Verify read_and_extract raises ValueError for unsupported file extension."""
    with pytest.raises(ValueError) as excinfo:
        read_and_extract("data.xlsx")
    assert "Unsupported" in str(excinfo.value) or "xlsx" in str(excinfo.value), (
        f"Expected ValueError about unsupported format, got: {excinfo.value}"
    )


# =========================================================================
# Test 8: extract_metadata — NaN label handling
# =========================================================================


def test_extract_metadata_nan_label():
    """Verify extract_metadata handles NaN labels (common in SPSS files)."""
    df = pd.DataFrame({"col1": [1, 2], "col2": ["a", "b"]})

    meta = {
        "filename": "test.sav",
        "format": "sav",
        "row_count": 2,
        "column_count": 2,
        "file_path": "/tmp/test.sav",
        "file_label": None,
        "_column_names": ["col1", "col2"],
        "_column_labels": [float("nan"), None],
        "_variable_value_labels": {},
    }

    result = extract_metadata(df, meta)

    # NaN and None labels should become empty strings
    for var in result["variables"]:
        assert isinstance(var["label"], str), (
            f"Label should be a string, got {type(var['label'])}: {var['label']}"
        )
        assert var["label"] == "", (
            f"NaN/None label should become empty string, got '{var['label']}'"
        )

    # Internal keys should be removed
    assert "_column_names" not in result
    assert "_column_labels" not in result
    assert "_variable_value_labels" not in result
