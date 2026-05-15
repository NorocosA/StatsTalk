"""Explainer integration tests — A/B testing template mode vs LLM polish mode.

Verifies the constraint layer (Plan.md §3.10) is correctly applied in both
template and LLM-polished explanations.  The golden rule: LLM polish MUST
NOT change the statistical conclusion.
"""

import pytest

from snla.explainer.naturalize import (
    apply_constraints,
    build_polish_prompt,
    explain,
    explain_template,
)


# =========================================================================
# Test 1: Template explanation — SIGNIFICANT result (p=0.021)
# =========================================================================


def test_template_significant_result(analysis_result_ttest):
    """Template explanation for p=0.021 must include '存在显著差异'."""
    constraints = apply_constraints(analysis_result_ttest)
    result = explain_template(constraints, analysis_result_ttest)

    assert "存在统计学上的显著差异" in result, (
        f"Significant result must state '存在显著差异', got: {result[:100]}"
    )
    assert "t=2.340" in result or "t=2.34" in result, (
        f"Should include t-value, got: {result[:100]}"
    )
    assert "p=0.021" in result, "Should include p-value"

    # Must NOT contain non-significant language
    assert "未发现" not in result, (
        f"Significant result must not say '未发现', got: {result[:100]}"
    )


# =========================================================================
# Test 2: Template explanation — NOT_SIG (p=0.051 → EDGE_SIGNIFICANT)
# =========================================================================


def test_template_not_significant_result(analysis_result_not_sig):
    """Template explanation for p=0.051 must include '未达统计学显著水平'."""
    constraints = apply_constraints(analysis_result_not_sig)
    result = explain_template(constraints, analysis_result_not_sig)

    assert "未达统计学显著水平" in result, (
        f"Non-sig result must say '未达显著水平', got: {result[:100]}"
    )
    assert "边缘显著" in result, (
        f"p=0.051 should be edge significant, got: {result[:100]}"
    )
    assert "建议增加样本量" in result, (
        f"Edge significant should suggest larger sample, got: {result[:100]}"
    )

    # CRITICAL: Must NOT contain significance-implying language
    for forbidden in ("存在显著差异", "存在显著相关", "相关", "有意义"):
        assert forbidden not in result, (
            f"Forbidden phrase '{forbidden}' found in non-significant explanation"
        )


# =========================================================================
# Test 3: Template explanation — EDGE_SIGNIFICANT (p=0.09)
# =========================================================================


def test_template_edge_significant_result(analysis_result_edge_sig):
    """Template explanation for p=0.09 must include '边缘显著' and sample size advice."""
    constraints = apply_constraints(analysis_result_edge_sig)
    result = explain_template(constraints, analysis_result_edge_sig)

    assert "边缘显著" in result, (
        f"Edge significant must mention '边缘显著', got: {result[:100]}"
    )
    assert "建议增加样本量" in result, (
        f"Must suggest increasing sample size, got: {result[:100]}"
    )
    assert "p=0.090" in result, "Should include p-value"

    # Must NOT claim significance
    assert "存在统计学上的显著差异" not in result, (
        f"Edge significant must not claim definite significance"
    )


# =========================================================================
# Test 4: build_polish_prompt — structure and constraint injection
# =========================================================================


def test_build_polish_prompt_structure(analysis_result_ttest):
    """Verify polish prompt includes system role, forced_phrase, and forbidden list."""
    constraints = apply_constraints(analysis_result_ttest)
    messages = build_polish_prompt(constraints, analysis_result_ttest)

    assert len(messages) == 2, (
        f"Expected 2 messages (system + user), got {len(messages)}"
    )
    assert messages[0]["role"] == "system"
    assert "社科本科生" in messages[0]["content"], (
        "System prompt should target social science undergraduates"
    )
    assert messages[1]["role"] == "user"
    assert "STATISTICAL FACTS" in messages[1]["content"], (
        "User message must contain STATISTICAL FACTS block"
    )
    assert constraints["forced_phrase"] in messages[1]["content"], (
        "User message must inject the forced_phrase"
    )


def test_build_polish_prompt_forbids_phrases(analysis_result_not_sig):
    """For non-significant results, the polish prompt must forbid misleading phrases."""
    constraints = apply_constraints(analysis_result_not_sig)
    messages = build_polish_prompt(constraints, analysis_result_not_sig)

    user_content = messages[1]["content"]

    # forbidden_phrases from constraints should appear in the prompt
    for phrase in constraints["forbidden_phrases"]:
        assert phrase in user_content, (
            f"Forbidden phrase '{phrase}' must be listed in polish prompt"
        )

    # The prompt must explicitly say "不允许使用的措辞"
    assert "不允许使用的措辞" in user_content, (
        "Polish prompt must explicitly list forbidden phrases"
    )


# =========================================================================
# Test 5: explain() — template fallback on LLM failure
# =========================================================================


def test_explain_falls_back_to_template(analysis_result_ttest):
    """When use_llm_polish=True but no llm_client, should return template output."""
    result = explain(analysis_result_ttest, use_llm_polish=True, llm_client=None)
    assert "存在统计学上的显著差异" in result, (
        f"Fallback to template should work, got: {result[:100]}"
    )


# =========================================================================
# Test 6: Real LLM polish — constraint integrity (requires LLM)
# =========================================================================


@pytest.mark.slow
def test_llm_polish_preserves_constraint(analysis_result_not_sig):
    """Real LLM polish: even with rewording, p=0.051 must NOT claim significance.

    This is the critical A/B test: template mode is 100% controlled, LLM
    polish adds readability.  If the LLM ever changes the statistical
    conclusion, the polish layer must be disabled.
    """
    from snla.llm.client import LLMClient
    from snla.config import LLM_MOCK

    if LLM_MOCK:
        pytest.skip("LLM_MOCK=true — real LLM polish test skipped")

    try:
        client = LLMClient()
    except Exception:
        pytest.skip("LLM client unavailable")

    result = explain(analysis_result_not_sig, use_llm_polish=True, llm_client=client)

    # CRITICAL: The output must NOT claim statistical significance
    for forbidden in ("存在显著差异", "存在显著相关", "具有统计学意义"):
        assert forbidden not in result, (
            f"LLM polish VIOLATED constraint: '{forbidden}' found in output:\n{result[:300]}"
        )

    # The output should mention the p-value
    assert "0.051" in result or "0.05" in result, (
        f"LLM-polished output should mention the p-value, got: {result[:200]}"
    )

    # Should convey non-significance or marginality (fuzzy check)
    # The LLM can reword freely — as long as it doesn't claim significance
    non_sig_words = ("没达到", "未达到", "未达", "不显著", "没有达到", "没达标",
                     "没有显著", "没发现显著", "边缘")
    has_non_sig_hint = any(w in result for w in non_sig_words)
    if not has_non_sig_hint:
        # Not a hard failure, but worth noting
        pytest.fail(
            f"LLM-polished output may not clearly convey non-significance. "
            f"Expected one of {non_sig_words} in: {result[:300]}"
        )

    # Must include the p-value
    assert "0.051" in result, (
        f"LLM-polished output should include p-value 0.051, got: {result[:200]}"
    )
