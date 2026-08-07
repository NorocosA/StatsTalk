"""Complete branch contracts for method selection and SPSS template dispatch."""

import pytest

from snla.syntax.templates import (
    _count_groups,
    _find_var,
    _is_categorical,
    _is_continuous,
    get_syntax_by_method,
    validate_method,
)


@pytest.mark.parametrize(
    ("method", "kwargs", "fragment"),
    (
        (
            "independent_t_test",
            {"group_var": "g", "test_var": "x", "groups": (1, 2)},
            "T-TEST GROUPS=g",
        ),
        ("paired_t_test", {"var1": "before", "var2": "after"}, "T-TEST PAIRS=before"),
        ("oneway_anova", {"group_var": "g", "test_var": "x"}, "ONEWAY x BY g"),
        ("simple_regression", {"dep_var": "y", "indep_var": "x"}, "/DEPENDENT y"),
        ("chi_square", {"row_var": "a", "col_var": "b"}, "/TABLES=a BY b"),
        ("frequencies", {"var": "x"}, "FREQUENCIES VARIABLES=x"),
        ("descriptives", {"var": "x"}, "DESCRIPTIVES VARIABLES=x"),
        ("correlations", {"var1": "x", "var2": "y"}, "/VARIABLES=x y"),
        ("spearman_correlation", {"var1": "x", "var2": "y"}, "SPEARMAN"),
        ("mann_whitney_u", {"group_var": "g", "test_var": "x"}, "/M-W= x BY g(1 2)"),
        ("kruskal_wallis", {"group_var": "g", "test_var": "x"}, "/K-W= x BY g"),
    ),
)
def test_each_public_template_dispatches(method, kwargs, fragment):
    syntax = get_syntax_by_method(method, **kwargs)

    assert fragment in syntax
    assert syntax.endswith(".")


def test_unknown_template_lists_available_methods():
    with pytest.raises(ValueError) as caught:
        get_syntax_by_method("not-a-method")

    assert "Unknown method 'not-a-method'" in str(caught.value)
    assert "descriptives" in str(caught.value)


def test_method_helpers_classify_metadata_boundaries():
    numeric = {"name": "score", "type": "Numeric", "value_labels": None}
    empty_labels = {"name": "score2", "type": "Numeric", "value_labels": {}}
    grouped = {"name": "group", "type": "Numeric", "value_labels": {1: "A", 2: "B"}}
    text = {"name": "label", "type": "String", "value_labels": None}

    assert _find_var([numeric], "score") is numeric
    assert _find_var([numeric], "missing") is None
    assert _is_categorical(grouped) is True
    assert _is_categorical(text) is True
    assert _is_categorical(numeric) is False
    assert _is_continuous(numeric) is True
    assert _is_continuous(empty_labels) is True
    assert _is_continuous(grouped) is False
    assert _is_continuous(text) is False
    assert _count_groups(grouped) == 2
    assert _count_groups(numeric) is None


def test_continuous_grouping_variable_is_rejected_and_corrected_to_correlation():
    variables = [
        {"name": "group", "type": "Numeric", "value_labels": None},
        {"name": "score", "type": "Numeric", "value_labels": None},
    ]

    result = validate_method(
        variables, "independent_t_test", grouping_var="group", test_var="score"
    )

    assert result["valid"] is False
    assert result["corrected_method"] == "pearson_correlation"


def test_string_outcome_is_rejected_without_an_unsafe_correction():
    variables = [
        {"name": "group", "type": "Numeric", "value_labels": None},
        {"name": "label", "type": "String", "value_labels": None},
    ]

    result = validate_method(variables, "oneway_anova", grouping_var="group", test_var="label")

    assert result["valid"] is False
    assert len(result["errors"]) == 2
    assert result["corrected_method"] is None


def test_three_group_t_test_is_corrected_to_anova_and_warns_for_small_samples():
    variables = [
        {
            "name": "group",
            "type": "Numeric",
            "value_labels": {1: "A", 2: "B", 3: "C"},
        },
        {"name": "score", "type": "Numeric", "value_labels": None},
    ]

    result = validate_method(
        variables,
        "independent_t_test",
        grouping_var="group",
        test_var="score",
        row_count=2,
    )

    assert result["valid"] is True
    assert result["corrected_method"] == "oneway_anova"
    assert len(result["warnings"]) == 2


def test_existing_correlation_correction_is_not_overwritten_by_group_count():
    variables = [
        {
            "name": "group",
            "type": "Unsupported",
            "value_labels": {1: "A", 2: "B", 3: "C"},
        },
        {"name": "score", "type": "Numeric", "value_labels": None},
    ]

    result = validate_method(
        variables, "independent_t_test", grouping_var="group", test_var="score"
    )

    assert result["valid"] is False
    assert result["corrected_method"] == "pearson_correlation"
    assert len(result["warnings"]) == 1


def test_method_without_roles_or_row_count_has_no_decision_side_effects():
    result = validate_method([], "descriptives")

    assert result == {
        "valid": True,
        "errors": [],
        "warnings": [],
        "corrected_method": None,
    }
