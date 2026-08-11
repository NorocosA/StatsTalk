"""Release qualification tests for all 11 public Python capabilities."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from snla.analysis.applicability import evaluate_applicability, resolve_role_bindings
from snla.capabilities import get_capability, get_public_capabilities
from snla.executor.python import PythonStatsExecutor


@pytest.fixture(scope="module")
def qualification_data() -> pd.DataFrame:
    x = np.arange(1.0, 13.0)
    return pd.DataFrame(
        {
            "binary_group": ["A"] * 6 + ["B"] * 6,
            "three_groups": ["A"] * 4 + ["B"] * 4 + ["C"] * 4,
            "score": [1, 2, 3, 4, 5, 6, 12, 13, 14, 15, 16, 17],
            "anova_score": [1, 2, 3, 4, 10, 11, 12, 13, 20, 21, 22, 23],
            "before": x,
            "after": x + np.tile([4.0, 5.0, 6.0], 4),
            "x": x,
            "y": 2.0 * x + np.tile([0.0, 0.2, -0.2], 4),
            "category": ["X"] * 5 + ["Y"] + ["X"] + ["Y"] * 5,
        }
    )


METHOD_CASES = {
    "descriptives": {"test_var": "score"},
    "frequencies": {"test_var": "category"},
    "independent_t_test": {"grouping_var": "binary_group", "test_var": "score"},
    "paired_t_test": {"var1": "before", "var2": "after"},
    "oneway_anova": {"grouping_var": "three_groups", "test_var": "anova_score"},
    "pearson_correlation": {"var1": "x", "var2": "y"},
    "spearman_correlation": {"var1": "x", "var2": "y"},
    "chi_square": {"grouping_var": "binary_group", "test_var": "category"},
    "mann_whitney_u": {"grouping_var": "binary_group", "test_var": "score"},
    "kruskal_wallis": {"grouping_var": "three_groups", "test_var": "anova_score"},
    "simple_regression": {"dep_var": "y", "indep_var": "x"},
}


def _reference(method: str, data: pd.DataFrame) -> tuple[str, float]:
    if method == "descriptives":
        return "mean", float(data["score"].mean())
    if method == "frequencies":
        return "n_valid", float(data["category"].notna().sum())
    if method == "independent_t_test":
        result = stats.ttest_ind(data.loc[:5, "score"], data.loc[6:, "score"], equal_var=True)
        return "t_value", float(result.statistic)
    if method == "paired_t_test":
        result = stats.ttest_rel(data["before"], data["after"])
        return "t_value", float(result.statistic)
    if method == "oneway_anova":
        groups = [group["anova_score"] for _, group in data.groupby("three_groups")]
        return "f_value", float(stats.f_oneway(*groups).statistic)
    if method == "pearson_correlation":
        return "r", float(stats.pearsonr(data["x"], data["y"]).statistic)
    if method == "spearman_correlation":
        return "r", float(stats.spearmanr(data["x"], data["y"]).statistic)
    if method == "chi_square":
        table = pd.crosstab(data["binary_group"], data["category"])
        return "chi_square", float(stats.chi2_contingency(table, correction=False).statistic)
    if method == "mann_whitney_u":
        result = stats.mannwhitneyu(
            data.loc[:5, "score"],
            data.loc[6:, "score"],
            method="asymptotic",
            use_continuity=False,
        )
        return "u", float(result.statistic)
    if method == "kruskal_wallis":
        groups = [group["anova_score"] for _, group in data.groupby("three_groups")]
        return "h", float(stats.kruskal(*groups).statistic)
    regression = stats.linregress(data["x"], data["y"])
    return "r_squared", float(regression.rvalue**2)


@pytest.mark.parametrize("method", METHOD_CASES)
def test_method_schema_and_expected_numeric_statistic(method, qualification_data):
    result = PythonStatsExecutor().execute(method, qualification_data, **METHOD_CASES[method])
    capability = get_capability(method)
    key, expected = _reference(method, qualification_data)

    assert result.parser_used == "python_pingouin"
    assert result.tables
    assert key in result.statistics
    assert result.statistics[key] == pytest.approx(expected, rel=1e-7, abs=1e-9)
    assert all(
        tolerance.metric in result.statistics or tolerance.metric in {"u_statistic", "h_statistic"}
        for tolerance in capability.comparison_tolerances
    )


@pytest.mark.parametrize(
    "method",
    [
        "independent_t_test",
        "paired_t_test",
        "oneway_anova",
        "pearson_correlation",
        "spearman_correlation",
        "chi_square",
        "mann_whitney_u",
        "kruskal_wallis",
        "simple_regression",
    ],
)
def test_inferential_decisions_are_significant_for_known_separated_data(
    method,
    qualification_data,
):
    result = PythonStatsExecutor().execute(method, qualification_data, **METHOD_CASES[method])

    assert result.statistics["p_value"] < 0.05


@pytest.mark.parametrize("method", METHOD_CASES)
def test_missing_values_are_handled_locally_for_every_method(method, qualification_data):
    data = qualification_data.copy()
    for column in data.columns:
        data.loc[data.index[0], column] = np.nan
    result = PythonStatsExecutor().execute(method, data, **METHOD_CASES[method])

    assert result.parser_used == "python_pingouin"
    assert result.n_valid <= len(data) - 1
    assert not any("Traceback" in note for note in result.notes)


@pytest.mark.parametrize("capability", get_public_capabilities(), ids=lambda item: item.name)
def test_invalid_variable_inputs_are_rejected_for_every_public_method(capability):
    data = pd.DataFrame({"available": [1, 2, 3, 4]})
    variables = [{"name": "available", "type": "Numeric"}]

    decision = evaluate_applicability(
        capability.name,
        variables,
        data,
        grouping_variable="missing_group",
        test_variable="missing_value",
    )

    assert decision.valid is False
    assert decision.issues
    assert all(issue.code == "VARIABLE_NOT_FOUND" for issue in decision.issues)


@pytest.mark.parametrize(
    "capability",
    [item for item in get_public_capabilities() if item.requirements.minimum_sample_size > 1],
    ids=lambda item: item.name,
)
def test_small_samples_are_rejected_before_backend_execution(capability):
    bindings = resolve_role_bindings(
        capability.name,
        grouping_variable="first",
        test_variable="second",
    )
    categorical_roles = {"grouping_variable", "row_variable", "column_variable"}
    variables = []
    values = {}
    for role, name in bindings.items():
        if name not in values:
            is_categorical = role in categorical_roles
            values[name] = ["A"] if is_categorical else [1.0]
            variables.append(
                {
                    "name": name,
                    "type": "String" if is_categorical else "Numeric",
                    "measurement_level": "nominal" if is_categorical else "scale",
                }
            )

    decision = evaluate_applicability(
        capability.name,
        variables,
        pd.DataFrame(values),
        grouping_variable="first",
        test_variable="second",
    )

    assert decision.valid is False
    assert {issue.code for issue in decision.issues} & {
        "INSUFFICIENT_COMPLETE_SAMPLE",
        "GROUP_COUNT_TOO_LOW",
        "CATEGORY_COUNT_TOO_LOW",
        "GROUP_SAMPLE_TOO_SMALL",
    }


def test_pairwise_missing_values_do_not_shift_paired_observations():
    data = pd.DataFrame({"before": [1.0, np.nan, 3.0, 4.0], "after": [2.0, 3.0, np.nan, 8.0]})

    result = PythonStatsExecutor().execute("paired_t_test", data, var1="before", var2="after")

    assert result.statistics["n_valid"] == 2


def test_frequency_missing_values_are_not_counted_as_valid_categories():
    data = pd.DataFrame({"category": ["A", "A", "B", np.nan]})

    result = PythonStatsExecutor().execute("frequencies", data, test_var="category")

    assert result.statistics == {"n_valid": 3, "n_missing": 1}
    assert sum(row["Frequency"] for row in result.tables[0].rows) == 3
