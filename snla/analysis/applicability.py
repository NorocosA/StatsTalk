"""Statistical applicability rules evaluated before backend execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from snla.capabilities import canonicalize_method, get_capability


@dataclass(frozen=True)
class ApplicabilityIssue:
    """One plain-language reason an analysis must not execute."""

    code: str
    message: str


@dataclass(frozen=True)
class CorrectionChoice:
    """A method correction that can be applied only after confirmation."""

    id: str
    method: str
    label: str


@dataclass(frozen=True)
class ApplicabilityDecision:
    """Result of checking a planned method against the loaded dataset."""

    valid: bool
    issues: tuple[ApplicabilityIssue, ...] = ()
    corrections: tuple[CorrectionChoice, ...] = ()


def evaluate_applicability(
    method: str,
    variables: list[dict[str, Any]],
    data: Any,
    *,
    grouping_variable: str | None,
    test_variable: str | None,
) -> ApplicabilityDecision:
    """Evaluate method and variable-role compatibility using local metadata."""

    canonical_method = canonicalize_method(method)
    capability = get_capability(canonical_method)
    if capability is None:
        return ApplicabilityDecision(valid=True)

    by_name = {str(variable.get("name")): variable for variable in variables}
    bindings = resolve_role_bindings(
        canonical_method,
        grouping_variable=grouping_variable,
        test_variable=test_variable,
    )
    missing = [
        name or role
        for role, name in bindings.items()
        if not name or name not in by_name or name not in data.columns
    ]
    if missing:
        return ApplicabilityDecision(
            valid=False,
            issues=tuple(
                ApplicabilityIssue(
                    code="VARIABLE_NOT_FOUND",
                    message=f"The planned variable '{name}' is not available in this dataset.",
                )
                for name in missing
            ),
        )

    role_names = [name for name in bindings.values() if name]
    if len(role_names) > 1 and len(set(role_names)) != len(role_names):
        return ApplicabilityDecision(
            valid=False,
            issues=(
                ApplicabilityIssue(
                    code="VARIABLE_ROLES_NOT_DISTINCT",
                    message=f"{canonical_method} requires different variables for each role.",
                ),
            ),
        )

    grouping_name = bindings.get("grouping_variable")
    tested_name = bindings.get("test_variable")
    grouping = by_name.get(grouping_name or "")
    tested = by_name.get(tested_name or "")

    group_comparison_methods = {
        "independent_t_test",
        "oneway_anova",
        "mann_whitney_u",
        "kruskal_wallis",
    }
    if canonical_method in group_comparison_methods:
        if grouping is not None and not _is_categorical(grouping):
            corrections: tuple[CorrectionChoice, ...] = ()
            if tested is not None and _is_continuous(tested):
                corrections = (
                    CorrectionChoice(
                        id="use_pearson_correlation",
                        method="pearson_correlation",
                        label="Use Pearson correlation for two continuous variables",
                    ),
                )
            return ApplicabilityDecision(
                valid=False,
                issues=(
                    ApplicabilityIssue(
                        code="GROUPING_VARIABLE_NOT_CATEGORICAL",
                        message=(
                            f"{grouping_variable} is continuous and cannot define groups for "
                            f"{canonical_method}."
                        ),
                    ),
                ),
                corrections=corrections,
            )
        if tested is not None and not _is_continuous(tested):
            corrections: tuple[CorrectionChoice, ...] = ()
            if grouping is not None and _is_categorical(grouping) and _is_categorical(tested):
                corrections = (
                    CorrectionChoice(
                        id="use_chi_square",
                        method="chi_square",
                        label="Use a chi-square test for two categorical variables",
                    ),
                )
            return ApplicabilityDecision(
                valid=False,
                issues=(
                    ApplicabilityIssue(
                        code="TEST_VARIABLE_NOT_CONTINUOUS",
                        message=(
                            f"{test_variable} is categorical, but {canonical_method} "
                            "requires a continuous outcome."
                        ),
                    ),
                ),
                corrections=corrections,
            )

    expected_types = {
        "grouping_variable": "categorical",
        "row_variable": "categorical",
        "column_variable": "categorical",
        "test_variable": "continuous",
        "first_variable": "continuous",
        "second_variable": "continuous",
        "dependent_variable": "continuous",
        "independent_variable": "continuous",
    }
    if canonical_method == "descriptives":
        expected_types["variable"] = "continuous"
    for role, name in bindings.items():
        expected = expected_types.get(role)
        variable = by_name.get(name or "")
        if expected == "categorical" and variable is not None and not _is_categorical(variable):
            corrections: tuple[CorrectionChoice, ...] = ()
            other_variables = [by_name[item] for item in role_names if item != name]
            if other_variables and all(
                _is_continuous(item) for item in [variable, *other_variables]
            ):
                corrections = (
                    CorrectionChoice(
                        id="use_pearson_correlation",
                        method="pearson_correlation",
                        label="Use Pearson correlation for two continuous variables",
                    ),
                )
            return ApplicabilityDecision(
                valid=False,
                issues=(
                    ApplicabilityIssue(
                        code=f"{role.upper()}_NOT_CATEGORICAL",
                        message=f"{name} must be categorical for the {role.replace('_', ' ')} role.",
                    ),
                ),
                corrections=corrections,
            )
        if expected == "continuous" and variable is not None and not _is_continuous(variable):
            corrections = ()
            other_variables = [by_name[item] for item in role_names if item != name]
            if other_variables and all(
                _is_categorical(item) for item in [variable, *other_variables]
            ):
                corrections = (
                    CorrectionChoice(
                        id="use_chi_square",
                        method="chi_square",
                        label="Use a chi-square test for two categorical variables",
                    ),
                )
            return ApplicabilityDecision(
                valid=False,
                issues=(
                    ApplicabilityIssue(
                        code=f"{role.upper()}_NOT_CONTINUOUS",
                        message=f"{name} must be continuous for the {role.replace('_', ' ')} role.",
                    ),
                ),
                corrections=corrections,
            )

    if (
        capability.requirements.maximum_groups is not None
        and grouping_name
        and grouping_name in data.columns
    ):
        observed_groups = int(data[grouping_name].dropna().nunique())
        if observed_groups > capability.requirements.maximum_groups:
            corrections: tuple[CorrectionChoice, ...] = ()
            if canonical_method == "independent_t_test":
                corrections = (
                    CorrectionChoice(
                        id="use_oneway_anova",
                        method="oneway_anova",
                        label="Use one-way ANOVA for three or more groups",
                    ),
                )
            elif canonical_method == "mann_whitney_u":
                corrections = (
                    CorrectionChoice(
                        id="use_kruskal_wallis",
                        method="kruskal_wallis",
                        label="Use Kruskal-Wallis for three or more groups",
                    ),
                )
            return ApplicabilityDecision(
                valid=False,
                issues=(
                    ApplicabilityIssue(
                        code="GROUP_COUNT_TOO_HIGH",
                        message=(
                            f"{canonical_method} supports at most "
                            f"{capability.requirements.maximum_groups} groups, but "
                            f"{observed_groups} were found."
                        ),
                    ),
                ),
                corrections=corrections,
            )

    role_columns = [name for name in role_names if name in data.columns]
    complete = data[list(dict.fromkeys(role_columns))].dropna() if role_columns else data
    issues: list[ApplicabilityIssue] = []
    if len(complete) < capability.requirements.minimum_sample_size:
        issues.append(
            ApplicabilityIssue(
                code="INSUFFICIENT_COMPLETE_SAMPLE",
                message=(
                    f"{canonical_method} needs at least "
                    f"{capability.requirements.minimum_sample_size} complete cases; "
                    f"only {len(complete)} are available."
                ),
            )
        )

    minimum_groups = capability.requirements.minimum_groups
    for role in ("row_variable", "column_variable"):
        name = bindings.get(role)
        if minimum_groups is None or not name or name not in complete.columns:
            continue
        observed_levels = int(complete[name].nunique(dropna=True))
        if observed_levels < minimum_groups:
            issues.append(
                ApplicabilityIssue(
                    code="CATEGORY_COUNT_TOO_LOW",
                    message=(
                        f"{canonical_method} needs at least {minimum_groups} observed "
                        f"levels in {name}; only {observed_levels} were found."
                    ),
                )
            )

    if grouping_name and grouping_name in complete.columns:
        group_counts = complete[grouping_name].value_counts(dropna=True)
        observed_groups = len(group_counts)
        minimum_groups = capability.requirements.minimum_groups
        if minimum_groups is not None and observed_groups < minimum_groups:
            issues.append(
                ApplicabilityIssue(
                    code="GROUP_COUNT_TOO_LOW",
                    message=(
                        f"{canonical_method} needs at least {minimum_groups} groups; "
                        f"only {observed_groups} were found."
                    ),
                )
            )
        minimum_group_size = capability.requirements.minimum_group_size
        if (
            minimum_group_size is not None
            and not group_counts.empty
            and int(group_counts.min()) < minimum_group_size
        ):
            issues.append(
                ApplicabilityIssue(
                    code="GROUP_SAMPLE_TOO_SMALL",
                    message=(
                        f"Each group needs at least {minimum_group_size} complete cases; "
                        f"the smallest group has {int(group_counts.min())}."
                    ),
                )
            )

    if issues:
        corrections: tuple[CorrectionChoice, ...] = ()
        continuous = next(
            (by_name[name] for name in role_names if _is_continuous(by_name[name])),
            None,
        )
        if continuous is not None:
            corrections = (
                CorrectionChoice(
                    id="use_descriptives",
                    method="descriptives",
                    label="Summarize the available outcome with descriptive statistics",
                ),
            )
        return ApplicabilityDecision(
            valid=False,
            issues=tuple(issues),
            corrections=corrections,
        )

    return ApplicabilityDecision(valid=True)


def resolve_role_bindings(
    method: str,
    *,
    grouping_variable: str | None,
    test_variable: str | None,
) -> dict[str, str | None]:
    """Map the planner's two generic slots to registered method roles."""

    capability = get_capability(canonicalize_method(method))
    if capability is None:
        return {}
    roles = capability.requirements.variable_roles
    if len(roles) == 1:
        return {roles[0]: test_variable or grouping_variable}
    values = (grouping_variable, test_variable)
    return {
        role: values[index] if index < len(values) else None for index, role in enumerate(roles)
    }


def _is_categorical(variable: dict[str, Any]) -> bool:
    if variable.get("type") == "String":
        return True
    labels = variable.get("value_labels")
    return variable.get("type") == "Numeric" and isinstance(labels, dict) and bool(labels)


def _is_continuous(variable: dict[str, Any]) -> bool:
    if variable.get("type") != "Numeric":
        return False
    labels = variable.get("value_labels")
    return labels is None or labels == {}


__all__ = [
    "ApplicabilityDecision",
    "ApplicabilityIssue",
    "CorrectionChoice",
    "evaluate_applicability",
    "resolve_role_bindings",
]
