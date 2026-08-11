"""Public capability contract tests."""

import asyncio
import json
from unittest.mock import patch


def test_public_registry_exposes_the_11_canonical_beta_capabilities():
    from snla.capabilities import get_public_capabilities

    capabilities = get_public_capabilities()

    assert tuple(capability.name for capability in capabilities) == (
        "descriptives",
        "frequencies",
        "independent_t_test",
        "paired_t_test",
        "oneway_anova",
        "pearson_correlation",
        "spearman_correlation",
        "chi_square",
        "mann_whitney_u",
        "kruskal_wallis",
        "simple_regression",
    )


def test_legacy_aliases_resolve_to_their_canonical_capability():
    from snla.capabilities import canonicalize_method, get_capability

    assert canonicalize_method("correlations") == "pearson_correlation"
    assert canonicalize_method("crosstabs") == "chi_square"
    assert get_capability("correlations") is get_capability("pearson_correlation")
    assert get_capability("crosstabs") is get_capability("chi_square")


def test_backend_support_is_distinct_from_validation_and_fallback_eligibility():
    from snla.capabilities import (
        can_fallback_to_python,
        is_backend_supported,
        is_backend_validated,
    )

    assert is_backend_supported("simple_regression", "python") is True
    assert is_backend_validated("simple_regression", "python") is True
    assert can_fallback_to_python("simple_regression") is True

    assert is_backend_supported("descriptives", "python") is True
    assert is_backend_validated("descriptives", "python") is True
    assert can_fallback_to_python("descriptives") is True


def test_capability_backend_lookup_covers_both_engines_and_unknown_names():
    from snla.capabilities import get_capability

    capability = get_capability("descriptives")

    assert capability is not None
    assert capability.backend("python") is capability.python
    assert capability.backend("spss") is capability.spss
    assert capability.backend("unknown") is None


def test_each_public_capability_carries_the_release_decision_contract():
    from snla.capabilities import get_capability, get_public_capabilities

    for capability in get_public_capabilities():
        assert capability.display_name_zh
        assert capability.display_name_en
        assert capability.requirements.variable_roles
        assert capability.requirements.minimum_sample_size > 0
        assert capability.comparison_tolerances

    independent_t = get_capability("independent_t_test")
    assert independent_t is not None
    assert independent_t.requirements.variable_roles == (
        "grouping_variable",
        "test_variable",
    )
    assert independent_t.requirements.minimum_groups == 2
    assert independent_t.requirements.maximum_groups == 2
    assert {item.metric for item in independent_t.comparison_tolerances} >= {
        "t_value",
        "p_value",
    }

    pearson = get_capability("pearson_correlation")
    chi_square = get_capability("chi_square")
    assert pearson is not None and pearson.aliases == ("correlations",)
    assert chi_square is not None and chi_square.aliases == ("crosstabs",)

    for hidden_method in ("wilcoxon", "multiple_regression", "logistic_regression"):
        assert get_capability(hidden_method) is None


def test_mcp_status_exposes_the_same_public_capability_payload():
    from snla.capabilities import get_public_capabilities_payload
    from snla.mcp_server import _session_states, snla_status

    class Context:
        session_id = "capability-contract"

    _session_states.clear()
    try:
        status = asyncio.run(snla_status(Context()))
    finally:
        _session_states.clear()

    assert status["capabilities"] == get_public_capabilities_payload()


def test_trusted_methods_are_derived_from_reviewed_registry_state():
    from snla.capabilities import get_public_capabilities
    from snla.trust import get_trusted_methods, is_method_trusted, trust_loaded_from

    expected = {
        capability.name
        for capability in get_public_capabilities()
        if capability.python.supported and capability.python.validated
    }

    assert get_trusted_methods() == expected
    assert is_method_trusted("correlations") is True
    assert is_method_trusted("crosstabs") is True
    assert trust_loaded_from() == "capability_registry"


def test_planner_normalizes_a_supported_alias_through_the_registry():
    from snla.orchestrator import Planner

    llm_result = {
        "content": json.dumps(
            {
                "method": "correlations",
                "plan_explanation": "Use a correlation analysis.",
                "grouping_variable": "age",
                "test_variable": "score",
            }
        )
    }
    variables = [
        {"name": "age", "type": "Numeric", "label": "Age"},
        {"name": "score", "type": "Numeric", "label": "Score"},
    ]

    with (
        patch("snla.config.LLM_MOCK", False),
        patch("snla.config.LLM_API_KEY", "test-key"),
        patch("snla.llm.client.LLMClient.chat", return_value=llm_result),
    ):
        result = Planner().plan("test", "分析年龄和成绩的相关性", variables)

    assert result.method == "pearson_correlation"
