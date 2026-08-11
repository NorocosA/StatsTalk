"""Tests for snla.data.sanitizer — 4 test cases covering cloud filtering,
sensitive-variable scanning, and normal variable pass-through."""

from snla.data.sanitizer import (
    build_cloud_planning_context,
    filter_for_cloud,
    sanitize_variables,
)

# ── Cloud-safe field filtering ──────────────────────────────────────────────


def test_filter_cloud_safe():
    """Only CLOUD_SAFE_FIELDS keys survive; unsafe keys (raw_data, identifiers) are dropped."""
    metadata = {
        "variables": [
            {"name": "gender", "type": "Numeric", "label": "Gender"},
            {"name": "score", "type": "Numeric", "label": "Score"},
        ],
        "row_count": 200,
        "raw_data": [[1, 2], [3, 4]],
        "identifiers": ["id_001", "id_002"],
    }
    result = filter_for_cloud(metadata)

    assert len(result["variables"]) == 2
    assert "row_count" in result
    assert "raw_data" not in result, f"raw_data should be filtered out, got keys={list(result)}"
    assert "identifiers" not in result, (
        f"identifiers should be filtered out, got keys={list(result)}"
    )
    # Ensure unsafe keys are the only ones missing
    assert set(result.keys()) == {"variables", "row_count"}, (
        f"Unexpected result keys: {set(result.keys())}"
    )


def test_filter_cloud_derives_role_type_without_exposing_value_labels():
    metadata = {
        "variables": [
            {
                "name": "group",
                "type": "Numeric",
                "label": "Group",
                "value_labels": {1: "Control", 2: "Treatment"},
            },
            {
                "name": "score",
                "type": "Numeric",
                "label": "Score",
                "value_labels": None,
            },
        ]
    }

    variables = filter_for_cloud(metadata)["variables"]

    assert variables[0]["role_type"] == "categorical"
    assert variables[1]["role_type"] == "continuous"
    assert all("value_labels" not in variable for variable in variables)


# ── Sensitive variable desensitization ──────────────────────────────────────


def test_sanitize_sensitive_variable():
    """Chinese sensitive name (患者姓名) and English pattern (email) are renamed to var_NN."""
    variables = [
        {"name": "患者姓名", "type": "String", "label": "Patient Name"},
        {"name": "email_addr", "type": "String", "label": "Email Address"},
    ]
    output, count = sanitize_variables(variables)

    assert count == 2, f"Expected 2 sensitive variables, got {count}"
    # First sensitive → var_01
    assert output[0]["name"] == "var_01", f"Expected var_01, got {output[0]['name']}"
    assert output[0]["original_name"] == "患者姓名"
    assert output[0]["label"] == "Sensitive variable 01"
    assert output[0]["original_label"] == "Patient Name"
    assert output[0]["desensitized"] is True
    # Second sensitive → var_02
    assert output[1]["name"] == "var_02", f"Expected var_02, got {output[1]['name']}"
    assert output[1]["original_name"] == "email_addr"


def test_cloud_planning_context_replaces_names_labels_and_restores_response_references():
    context = build_cloud_planning_context(
        [
            {
                "name": "patient_id",
                "type": "String",
                "label": "Patient Identifier",
                "value_labels": {"P001": "Alice"},
                "raw_values": ["P001"],
            },
            {"name": "score", "type": "Numeric", "label": "Score"},
        ]
    )

    serialized = str(context.variables)
    assert "patient_id" not in serialized
    assert "Patient Identifier" not in serialized
    assert "P001" not in serialized
    assert "Alice" not in serialized
    assert context.variables[0]["name"] == "var_01"
    assert context.variables[0]["label"] == "Sensitive variable 01"
    assert context.sanitize_text("分析 patient_id 与 Patient Identifier") == (
        "分析 var_01 与 var_01"
    )
    assert context.restore_reference("var_01, score") == "patient_id, score"
    assert context.restore_text("选择 var_01") == "选择 patient_id"


def test_sanitize_no_sensitive():
    """Normal variable names (gender, score, age, class) pass through unchanged."""
    variables = [
        {"name": "gender", "type": "Numeric", "label": "Gender"},
        {"name": "score", "type": "Numeric", "label": "Test Score"},
        {"name": "age", "type": "Numeric", "label": "Age"},
        {"name": "class", "type": "String", "label": "Class"},
    ]
    output, count = sanitize_variables(variables)

    assert count == 0, f"Expected 0 sensitive variables, got {count}"
    assert output == variables, (
        f"Non-sensitive variables should be returned unchanged:\n"
        f"  expected={variables}\n"
        f"  got     ={output}"
    )


def test_sanitize_count():
    """Mix of 2 sensitive + 3 normal variables → count=2, normal vars unchanged."""
    variables = [
        {"name": "patient_id", "type": "String", "label": "Patient ID"},
        {"name": "score", "type": "Numeric", "label": "Score"},
        {"name": "姓名", "type": "String", "label": "Name"},
        {"name": "age", "type": "Numeric", "label": "Age"},
        {"name": "class", "type": "String", "label": "Class"},
    ]
    output, count = sanitize_variables(variables)

    assert count == 2, f"Expected 2 sensitive variables, got {count}"

    # Sensitive variables renamed sequentially
    assert output[0]["name"] == "var_01", (
        f"patient_id should become var_01, got {output[0]['name']}"
    )
    assert output[0]["desensitized"] is True
    assert output[0]["original_name"] == "patient_id"

    assert output[2]["name"] == "var_02", f"姓名 should become var_02, got {output[2]['name']}"
    assert output[2]["desensitized"] is True
    assert output[2]["original_name"] == "姓名"

    # Normal variables unchanged
    assert output[1] == variables[1], f"score should be unchanged: {output[1]}"
    assert output[3] == variables[3], f"age should be unchanged: {output[3]}"
    assert output[4] == variables[4], f"class should be unchanged: {output[4]}"
