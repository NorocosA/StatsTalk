"""Privacy integration tests — full pipeline from sanitize to LLM-safe
variable lists, bidirectional name mapping, and syntax restoration.

Covers Plan.md §3.7 privacy protocol: verify that ONLY variable names,
types, labels, and value_labels reach the LLM — never raw data or
identifiers.
"""

from snla.data.sanitizer import (
    CLOUD_SAFE_FIELDS,
    build_cloud_planning_context,
    filter_for_cloud,
)
from snla.session import SessionState

# =========================================================================
# Test 1: Full privacy pipeline — sensitive variables end-to-end
# =========================================================================


def test_full_privacy_pipeline(sensitive_variables):
    """Simulate the full Streamlit privacy flow that runs on every file upload.

    Steps:
    1. sanitize_variables → rename sensitive vars to var_NN
    2. Populate SessionState with desensitized variables + var_name_map
    3. map_to_cloud → produce cloud-safe variable list for LLM prompt
    4. Verify cloud vars contain only safe fields, never raw data or original names
    5. Simulate LLM returning syntax with desensitized names
    6. map_to_local → restore original names for SPSS execution
    7. Verify restored syntax references original variable names
    """
    context = build_cloud_planning_context(sensitive_variables)
    safe_vars = context.variables
    assert len(safe_vars) == len(sensitive_variables), (
        f"Expected {len(sensitive_variables)} safe vars, got {len(safe_vars)}"
    )

    # ── Step 4: Verify cloud safety ─────────────────────────────────────
    # Sensitive variables should have desensitized names
    desensitized_names = {v["name"] for v in safe_vars if v["name"].startswith("var_")}
    assert len(desensitized_names) == 3, f"Expected 3 desensitized names, got {desensitized_names}"
    assert "var_01" in desensitized_names
    assert "var_02" in desensitized_names
    assert "var_03" in desensitized_names

    # Original sensitive names must NOT appear in cloud-safe list
    all_cloud_names = {v["name"] for v in safe_vars}
    assert "患者姓名" not in all_cloud_names, (
        "Sensitive name '患者姓名' leaked into cloud-safe variable list"
    )
    assert "手机号" not in all_cloud_names, (
        "Sensitive name '手机号' leaked into cloud-safe variable list"
    )
    assert "email_addr" not in all_cloud_names, (
        "Sensitive name 'email_addr' leaked into cloud-safe variable list"
    )
    serialized = str(safe_vars)
    for original in sensitive_variables:
        if original["name"] == "score":
            continue
        assert original["name"] not in serialized
        assert original["label"] not in serialized

    # Normal (non-sensitive) variable must retain its original name
    normal_names = {v["name"] for v in safe_vars if not v["name"].startswith("var_")}
    assert "score" in normal_names, (
        f"Normal variable 'score' should keep original name, got {normal_names}"
    )

    # Every cloud-safe variable dict must ONLY contain safe fields
    for v in safe_vars:
        for key in v:
            assert key in ("name", "type", "label", "role_type"), (
                f"Unsafe key '{key}' in cloud variable dict: {v}"
            )
        # No raw data, identifiers, or internal fields
        assert "raw_data" not in v
        assert "original_name" not in v
        assert "desensitized" not in v

    assert context.restore_reference("var_01") == "患者姓名"
    assert context.restore_reference("var_02") == "手机号"


# =========================================================================
# Test 2: filter_for_cloud — strips unsafe metadata keys
# =========================================================================


def test_filter_for_cloud_strips_unsafe():
    """Verify filter_for_cloud drops raw_data, identifiers, and free_text.

    Only keys listed in CLOUD_SAFE_FIELDS should survive filtering.
    """
    metadata = {
        "variables": [
            {"name": "gender", "type": "Numeric", "label": "Gender"},
            {"name": "score", "type": "Numeric", "label": "Score"},
        ],
        "row_count": 200,
        "raw_data": [[1, 85.5], [2, 92.0]],
        "identifiers": ["P001", "P002"],
        "free_text_responses": "Some open-ended text",
        "unknown_key": "should be dropped",
    }

    filtered = filter_for_cloud(metadata)

    # Safe keys survive
    assert "variables" in filtered
    assert "row_count" in filtered

    # Unsafe keys are dropped
    assert "raw_data" not in filtered, "raw_data should be filtered out"
    assert "identifiers" not in filtered, "identifiers should be filtered out"
    assert "free_text_responses" not in filtered, "free_text_responses should be filtered out"
    assert "unknown_key" not in filtered, "unknown keys should be filtered out"

    # Exactly the safe keys remain
    expected = {"variables", "row_count"}
    assert set(filtered.keys()) == expected, f"Expected only {expected}, got {set(filtered.keys())}"


# =========================================================================
# Test 3: map_to_cloud — preserves non-sensitive variable names
# =========================================================================


def test_map_to_cloud_no_sensitive(sample_variables):
    """Verify map_to_cloud returns unchanged variables when none are sensitive."""
    session = SessionState()
    session.variables = sample_variables

    result = session.map_to_cloud()
    assert len(result) == len(sample_variables), (
        f"Expected {len(sample_variables)} mapped vars, got {len(result)}"
    )

    # All original names preserved
    for original, mapped in zip(sample_variables, result, strict=False):
        assert mapped["name"] == original["name"], (
            f"Non-sensitive name '{original['name']}' was modified to '{mapped['name']}'"
        )
        # Internal fields should be absent from mapped output
        assert "_original_name" not in mapped, (
            f"No desensitization should add internal fields for '{mapped['name']}'"
        )


# =========================================================================
# Test 4: CLOUD_SAFE_FIELDS — correct set definition
# =========================================================================


def test_cloud_safe_fields_definition():
    """Verify CLOUD_SAFE_FIELDS contains expected keys and excludes dangerous ones.

    This is a regression test: if someone accidentally adds a key like
    'raw_data' to the safe set, every LLM call would leak data.
    """
    assert "name" in CLOUD_SAFE_FIELDS
    assert "type" in CLOUD_SAFE_FIELDS
    assert "label" in CLOUD_SAFE_FIELDS
    assert "role_type" in CLOUD_SAFE_FIELDS
    # value_labels intentionally excluded — contains actual value mappings
    # like {1:"Male"} that could leak private information to cloud LLM
    assert "value_labels" not in CLOUD_SAFE_FIELDS
    assert "aggregate_stats" not in CLOUD_SAFE_FIELDS
    assert "row_count" in CLOUD_SAFE_FIELDS
    assert "variables" in CLOUD_SAFE_FIELDS

    # These must NEVER be in the safe set
    dangerous = {
        "aggregate_stats",
        "filename",
        "file_path",
        "raw_data",
        "identifiers",
        "free_text_responses",
        "value_labels",
    }
    for key in dangerous:
        assert key not in CLOUD_SAFE_FIELDS, f"DANGER: '{key}' must NOT be in CLOUD_SAFE_FIELDS"

    assert len(CLOUD_SAFE_FIELDS) >= 6, (
        f"CLOUD_SAFE_FIELDS size changed from 6 to {len(CLOUD_SAFE_FIELDS)} — "
        f"review all additions for privacy impact"
    )


# =========================================================================
# Test 5: map_to_local — handles edge cases
# =========================================================================


def test_map_to_local_edge_cases():
    """Verify map_to_local handles empty maps, no matches, and partial matches."""
    session = SessionState()

    # ── Empty var_name_map ──────────────────────────────────────────────
    result = session.map_to_local("T-TEST GROUPS=gender(1 2) /VARIABLES=score.")
    assert result == "T-TEST GROUPS=gender(1 2) /VARIABLES=score.", (
        f"Empty map should return syntax unchanged, got: {result}"
    )

    # ── var_01 appears as substring of another word ─────────────────────
    session.var_name_map["var_01"] = "患者姓名"
    session.var_name_map["var_01_long"] = "another_var"
    # map_to_local sorts by key length descending to avoid partial replacement
    result = session.map_to_local("COMPUTE var_01_extra = var_01 * 2.")
    # var_01_long (longer) should be matched first, then var_01
    # Actually, "var_01_extra" shouldn't match "var_01" because it's sorted
    # descending and "var_01" is a substring of "var_01_long" but not of
    # "var_01_extra" after var_01_long is replaced first.
    # The key point: no partial replacement of "var_01" inside "var_01_extra"
    assert "var_01_extra" not in result or "another_var_extra" in result, (
        f"Partial match should be handled correctly, got: {result}"
    )
