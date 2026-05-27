"""Integration tests for SNLA — covering LLM prompt correctness, validator+prompt
interaction, sanitizer+session mapping, session state flow, template fallback,
and explainer constraint layer.

These tests require no real LLM, SPSS, or network access — all dependencies
are mocked or isolated via conftest fixtures.
"""

import pytest

# ===========================================================================
# Test 1: Intent prompt structure
# ===========================================================================


def test_intent_prompt_structure(sample_variables):
    """Verify build_intent_prompt returns correctly structured messages with
    system role definition, intent categories, user input, and few-shot examples."""
    from snla.llm.prompts.intent import build_intent_prompt

    user_input = "比较男女生在成绩上的差异"
    messages = build_intent_prompt(user_input, variables=sample_variables)

    assert isinstance(messages, list), "Expected a list of messages"
    assert len(messages) >= 2, "Expected at least 2 messages (system + user)"

    # ── First message: system role ──────────────────────────────────────
    assert messages[0]["role"] == "system", "First message should have role='system'"
    system_content = messages[0]["content"]

    # System prompt must define all four intent categories
    for intent in ("describe", "compare_groups", "relationship", "follow_up"):
        assert intent in system_content, f"System prompt should mention intent category '{intent}'"

    # ── Second message: user role ───────────────────────────────────────
    assert messages[1]["role"] == "user", "Second message should have role='user'"
    user_content = messages[1]["content"]

    assert user_input in user_content, "User message should contain the original input text"
    assert "示例" in user_content or "Examples" in user_content, (
        "User message should contain few-shot examples"
    )
    # Variable names from sample_variables should appear in dataset context
    assert "gender" in user_content, (
        "User message should contain variable 'gender' from sample_variables"
    )
    assert "score" in user_content, (
        "User message should contain variable 'score' from sample_variables"
    )


# ===========================================================================
# Test 2: Syntax prompt structure
# ===========================================================================


def test_syntax_prompt_structure(sample_variables):
    """Verify build_syntax_prompt returns SPSS-specific system instructions
    and a user message with dataset context and method details."""
    from snla.llm.prompts.syntax import build_syntax_prompt

    messages = build_syntax_prompt(
        method="independent_t_test",
        variables=sample_variables,
        dataset_summary={"row_count": 200, "variable_count": 4},
    )

    assert isinstance(messages, list), "Expected a list of messages"
    assert len(messages) == 2, "Expected exactly 2 messages (system + user)"

    # ── System prompt ───────────────────────────────────────────────────
    assert messages[0]["role"] == "system", "First message should have role='system'"
    sys_content = messages[0]["content"]
    assert "SPSS" in sys_content, "System prompt should mention SPSS expertise"
    assert "语法" in sys_content, "System prompt should reference 语法 (syntax) in Chinese"
    assert "JSON" in sys_content or "json" in sys_content.lower(), (
        "System prompt should reference JSON output format"
    )

    # ── User message ────────────────────────────────────────────────────
    assert messages[1]["role"] == "user", "Second message should have role='user'"
    user_content = messages[1]["content"]
    # Dataset context with variable names
    assert "gender" in user_content, "User message should contain variable 'gender'"
    assert "score" in user_content, "User message should contain variable 'score'"
    # Dataset summary
    assert "200" in user_content, "User message should contain dataset row count (200)"
    # Method identifier
    assert "independent_t_test" in user_content, "User message should mention the requested method"


# ===========================================================================
# Test 3: Method prompt structure
# ===========================================================================


def test_method_prompt_structure(sample_variables):
    """Verify build_method_prompt returns method-recommendation messages
    with statistical method catalog and output schema instructions."""
    from snla.llm.prompts.method import build_method_prompt

    messages = build_method_prompt(
        intent="compare_groups",
        variables=sample_variables,
    )

    assert isinstance(messages, list), "Expected a list of messages"
    assert len(messages) == 2, "Expected exactly 2 messages (system + user)"

    # ── System prompt ───────────────────────────────────────────────────
    assert messages[0]["role"] == "system", "First message should have role='system'"
    sys_content = messages[0]["content"]
    assert "统计方法" in sys_content or "statistical" in sys_content.lower(), (
        "System prompt should mention statistical methods catalog"
    )
    assert "recommended_method" in sys_content, (
        "System prompt should specify 'recommended_method' as output field"
    )

    # ── User message ────────────────────────────────────────────────────
    assert messages[1]["role"] == "user", "Second message should have role='user'"
    user_content = messages[1]["content"]
    # Variable context
    assert "gender" in user_content, "User message should contain variable 'gender'"
    # Intent classification
    assert "compare_groups" in user_content, "User message should mention the analysis intent"
    # Output format reference
    assert "recommended_method" in user_content, (
        "User message should reference the required output format"
    )


# ===========================================================================
# Test 4: Validator integration with LLM-generated syntax
# ===========================================================================


def test_validator_with_llm_generated_syntax():
    """Verify validate() correctly accepts well-formed LLM-generated SPSS
    syntax with valid variables and rejects syntax referencing unknown
    variables."""
    from snla.syntax.validator import validate

    # ── Valid syntax — all variables exist ──────────────────────────────
    valid_syntax = "T-TEST GROUPS=gender(1 2) /VARIABLES=score."
    result = validate(valid_syntax, var_list=["gender", "score"])

    assert result["valid"] is True, (
        f"Valid syntax with known variables should pass, got errors={result['errors']}"
    )
    assert len(result["errors"]) == 0, (
        f"Valid syntax should produce no errors, got {result['errors']}"
    )
    assert len(result["warnings"]) == 0, (
        f"Valid syntax should produce no warnings, got {result['warnings']}"
    )

    # ── Invalid syntax — non-existent variable ──────────────────────────
    invalid_syntax = "T-TEST GROUPS=gender(1 2) /VARIABLES=nonexistent."
    result2 = validate(invalid_syntax, var_list=["gender", "score"])

    assert result2["valid"] is False, "Syntax referencing non-existent variable should fail"
    assert any("nonexistent" in e for e in result2["errors"]), (
        f"Expected error mentioning 'nonexistent', got errors={result2['errors']}"
    )


# ===========================================================================
# Test 5: Sanitizer + session variable name mapping
# ===========================================================================


def test_sanitizer_session_mapping(sensitive_variables):
    """Verify sanitize_variables output integrates with SessionState:
    sensitive vars get var_NN names, original names are preserved, and
    the session's var_name_map supports bidirectional lookup and syntax
    restoration."""
    from snla.data.sanitizer import sanitize_variables
    from snla.session import SessionState

    # ── Sanitize the sensitive variables ────────────────────────────────
    sanitized, count = sanitize_variables(sensitive_variables)
    assert count >= 2, f"Expected at least 2 sensitive variables, got {count}"

    # ── Create session state and populate ───────────────────────────────
    session = SessionState()
    session.variables = sanitized

    # Simulate session flow: populate var_name_map + reverse map
    for var in sanitized:
        if var.get("desensitized") and var.get("original_name"):
            session.var_name_map[var["name"]] = var["original_name"]
            session.reverse_var_name_map[var["original_name"]] = var["name"]

    # ── Verify desensitized names present ───────────────────────────────
    var_names = [v["name"] for v in sanitized]
    assert "var_01" in var_names, (
        f"First sensitive variable should be renamed to var_01, got names={var_names}"
    )
    assert "var_02" in var_names, (
        f"Second sensitive variable should be renamed to var_02, got names={var_names}"
    )

    # ── Verify original names are preserved ─────────────────────────────
    desensitized_vars = [v for v in sanitized if v.get("desensitized")]
    for dv in desensitized_vars:
        assert "original_name" in dv, (
            f"Desensitized variable '{dv['name']}' must preserve original_name"
        )
        assert dv["original_name"] != dv["name"], (
            f"original_name must differ from desensitized name for '{dv['name']}'"
        )

    # ── Verify var_name_map completeness ────────────────────────────────
    assert len(session.var_name_map) == count, (
        f"var_name_map should have {count} entries (one per sensitive var), "
        f"got {len(session.var_name_map)}"
    )
    for cloud_name, original_name in session.var_name_map.items():
        assert cloud_name.startswith("var_"), (
            f"Cloud/variable key should start with 'var_', got '{cloud_name}'"
        )
        assert isinstance(original_name, str) and len(original_name) > 0

    # ── Verify reverse mapping consistency ──────────────────────────────
    assert session.reverse_var_name_map, "reverse_var_name_map should not be empty"
    for original, cloud in session.reverse_var_name_map.items():
        assert cloud.startswith("var_"), (
            f"Reverse map value should start with 'var_', got '{cloud}'"
        )
        assert session.var_name_map[cloud] == original, (
            f"Bidirectional mapping broken: "
            f"var_name_map['{cloud}']={session.var_name_map[cloud]}, "
            f"expected '{original}'"
        )

    # ── Verify map_to_local restores original names in syntax ───────────
    first_cloud = "var_01"
    second_cloud = "var_02"
    test_syntax = f"T-TEST GROUPS={first_cloud}(1 2) /VARIABLES={second_cloud}."
    restored = session.map_to_local(test_syntax)

    first_orig = session.var_name_map.get(first_cloud, "")
    second_orig = session.var_name_map.get(second_cloud, "")
    assert first_orig in restored, (
        f"map_to_local should restore '{first_orig}' in syntax, got: {restored}"
    )
    assert second_orig in restored, (
        f"map_to_local should restore '{second_orig}' in syntax, got: {restored}"
    )


# ===========================================================================
# Test 6: Session state flow
# ===========================================================================


def test_session_state_flow(sample_variables):
    """Verify SessionState methods for message history, analysis tracking,
    variable access, and cancellation lifecycle."""
    from snla.session import SessionState

    session = SessionState()
    session.variables = sample_variables

    # ── Add conversation messages ───────────────────────────────────────
    session.add_message("user", "比较男女成绩差异")
    session.add_message("assistant", "推荐使用独立样本t检验")

    # ── Record last analysis context ────────────────────────────────────
    session.set_last_analysis(
        method="independent_t_test",
        grouping_var="gender",
        test_var="score",
        analysis_type="T-TEST",
    )

    # ── Verify history ──────────────────────────────────────────────────
    assert len(session.history) == 2, f"Expected 2 history entries, got {len(session.history)}"
    assert session.history[0]["role"] == "user", "First history entry should have role='user'"
    assert "比较" in session.history[0]["content"], (
        "First history entry should contain user request"
    )
    assert session.history[1]["role"] == "assistant", (
        "Second history entry should have role='assistant'"
    )
    assert "独立" in session.history[1]["content"], (
        "Second history entry should contain assistant response"
    )

    # ── Verify last_analysis ────────────────────────────────────────────
    assert session.last_analysis is not None, (
        "last_analysis should not be None after set_last_analysis()"
    )
    assert session.last_analysis["method"] == "independent_t_test", (
        f"Expected method='independent_t_test', got {session.last_analysis['method']}"
    )
    assert session.last_analysis["grouping_var"] == "gender"
    assert session.last_analysis["test_var"] == "score"

    # ── Verify variable access ──────────────────────────────────────────
    var_names = session.get_variable_names()
    assert len(var_names) == 4, f"Expected 4 variable names, got {len(var_names)}: {var_names}"
    assert "gender" in var_names
    assert "score" in var_names
    assert "class" in var_names
    assert "age" in var_names

    gender_var = session.get_variable("gender")
    assert gender_var is not None, "get_variable('gender') should return a dict"
    assert gender_var["type"] == "Numeric"

    nonexistent = session.get_variable("nonexistent")
    assert nonexistent is None, "get_variable('nonexistent') should return None"

    # ── Verify cancellation lifecycle ───────────────────────────────────
    assert session.cancellation_token is False, "cancellation_token should initialise as False"

    session.cancel()
    assert session.cancellation_token is True, "cancel() should set cancellation_token to True"

    session.reset_cancellation()
    assert session.cancellation_token is False, (
        "reset_cancellation() should clear cancellation_token back to False"
    )


# ===========================================================================
# Test 7: Template fallback behavior
# ===========================================================================


def test_template_fallback():
    """Verify syntax template functions produce valid SPSS commands and
    get_syntax_by_method correctly dispatches to templates, with a
    ValueError for unknown methods."""
    from snla.syntax.templates import (
        TEMPLATE_MAP,
        get_syntax_by_method,
        ttest_independent,
    )

    # ── Direct template call: ttest_independent ─────────────────────────
    syntax = ttest_independent("gender", "score", (1, 2))
    assert "T-TEST" in syntax, f"ttest_independent output should contain 'T-TEST', got: {syntax}"
    assert "GROUPS=gender(1 2)" in syntax, f"Output should contain GROUPS clause, got: {syntax}"
    assert "/VARIABLES=score" in syntax, f"Output should contain VARIABLES clause, got: {syntax}"
    assert syntax.rstrip().endswith("."), f"SPSS syntax must end with a period, got: {syntax}"

    # ── get_syntax_by_method dispatch ───────────────────────────────────
    dispatched = get_syntax_by_method(
        "independent_t_test",
        group_var="gender",
        test_var="score",
        groups=(1, 2),
    )
    assert "T-TEST" in dispatched, f"Dispatched syntax should contain T-TEST, got: {dispatched}"
    assert dispatched == syntax, (
        "get_syntax_by_method should produce identical output to ttest_independent direct call"
    )

    # ── Unknown method raises ValueError ────────────────────────────────
    with pytest.raises(ValueError) as excinfo:
        get_syntax_by_method("nonexistent_method")
    assert "Unknown method" in str(excinfo.value), (
        f"Expected ValueError with 'Unknown method', got: {excinfo.value}"
    )

    # ── All TEMPLATE_MAP entries are callable ───────────────────────────
    for key, func in TEMPLATE_MAP.items():
        assert callable(func), f"TEMPLATE_MAP entry '{key}' should be a callable function"


# ===========================================================================
# Test 8: Explainer constraint layer
# ===========================================================================


def test_explainer_constraints(
    analysis_result_ttest,
    analysis_result_not_sig,
    analysis_result_edge_sig,
):
    """Verify apply_constraints correctly classifies significance and
    produces appropriate forced_phrase for each p-value regime.

    Boundary rules from snla/explainer/naturalize.py _interpret_p_value:
        p ≤ 0.05   → SIGNIFICANT
        0.05 < p < 0.10 → EDGE_SIGNIFICANT
        p ≥ 0.10   → NOT_SIG
    """
    from snla.explainer.naturalize import apply_constraints

    # ── SIGNIFICANT (p=0.021 ≤ 0.05) ────────────────────────────────────
    constraints_sig = apply_constraints(analysis_result_ttest)
    assert constraints_sig["significance"] == "SIGNIFICANT", (
        f"p=0.021 should be SIGNIFICANT, got {constraints_sig['significance']}"
    )
    assert "存在统计学上的显著差异" in constraints_sig["forced_phrase"], (
        f"Significant forced_phrase should mention significant difference, "
        f"got: {constraints_sig['forced_phrase']}"
    )

    # ── analysis_result_not_sig has p=0.051 → EDGE_SIGNIFICANT ─────────
    # (0.051 < 0.10 triggers the EDGE_SIGNIFICANT boundary per code rules)
    constraints_ns = apply_constraints(analysis_result_not_sig)
    assert constraints_ns["significance"] == "EDGE_SIGNIFICANT", (
        f"p=0.051 is < 0.10 → expected EDGE_SIGNIFICANT, got {constraints_ns['significance']}"
    )
    assert "未达统计学显著水平" in constraints_ns["forced_phrase"], (
        f"Non-sig forced_phrase should mention '未达统计学显著水平', "
        f"got: {constraints_ns['forced_phrase']}"
    )
    # CRITICAL: forced_phrase must not contain phrases implying significance
    for forbidden_phrase in ("存在显著", "相关"):
        assert forbidden_phrase not in constraints_ns["forced_phrase"], (
            f"Non-significant forced_phrase must not contain "
            f"'{forbidden_phrase}', got: {constraints_ns['forced_phrase']}"
        )

    # ── EDGE_SIGNIFICANT (p=0.09, 0.05 < 0.09 < 0.10) ──────────────────
    constraints_edge = apply_constraints(analysis_result_edge_sig)
    assert constraints_edge["significance"] == "EDGE_SIGNIFICANT", (
        f"p=0.09 should be EDGE_SIGNIFICANT, got {constraints_edge['significance']}"
    )
    assert "边缘显著" in constraints_edge["forced_phrase"], (
        f"Edge forced_phrase should mention 边缘显著 (borderline significant), "
        f"got: {constraints_edge['forced_phrase']}"
    )
    assert "建议增加样本量" in constraints_edge["forced_phrase"], (
        f"Edge forced_phrase should suggest increasing sample size, "
        f"got: {constraints_edge['forced_phrase']}"
    )
