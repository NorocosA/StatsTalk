"""Release capability registry for StatsTalk statistical methods."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCapability:
    """Support and release-validation state for one statistical backend."""

    supported: bool
    validated: bool


@dataclass(frozen=True)
class MethodRequirements:
    """Dataset roles and minimum conditions required by a method."""

    variable_roles: tuple[str, ...]
    minimum_sample_size: int
    minimum_groups: int | None = None
    maximum_groups: int | None = None
    minimum_group_size: int | None = None


@dataclass(frozen=True)
class MetricTolerance:
    """Initial cross-backend comparison tolerance for one normalized metric."""

    metric: str
    absolute: float
    relative: float
    rationale: str


@dataclass(frozen=True)
class MethodCapability:
    """A statistical capability exposed by the public product surface."""

    name: str
    display_name_zh: str
    display_name_en: str
    aliases: tuple[str, ...]
    requirements: MethodRequirements
    comparison_tolerances: tuple[MetricTolerance, ...]
    python: BackendCapability
    spss: BackendCapability

    def backend(self, name: str) -> BackendCapability | None:
        """Return backend metadata for ``python`` or ``spss``."""

        if name == "python":
            return self.python
        if name == "spss":
            return self.spss
        return None


def _tol(metric: str, absolute: float = 1e-4, relative: float = 1e-3) -> MetricTolerance:
    if absolute == 0 and relative == 0:
        rationale = "Discrete counts and degrees of freedom must match exactly."
    elif metric == "p_value":
        rationale = "Allows SPSS display rounding and small tail-probability algorithm differences."
    elif metric == "effect_size":
        rationale = "Allows documented backend rounding in derived effect-size calculations."
    else:
        rationale = (
            "Allows floating-point and displayed-value rounding without changing interpretation."
        )
    return MetricTolerance(
        metric=metric,
        absolute=absolute,
        relative=relative,
        rationale=rationale,
    )


def _capability(
    name: str,
    display_name_zh: str,
    display_name_en: str,
    roles: tuple[str, ...],
    metrics: tuple[MetricTolerance, ...],
    *,
    aliases: tuple[str, ...] = (),
    minimum_sample_size: int = 2,
    minimum_groups: int | None = None,
    maximum_groups: int | None = None,
    minimum_group_size: int | None = None,
    python_validated: bool = True,
) -> MethodCapability:
    return MethodCapability(
        name=name,
        display_name_zh=display_name_zh,
        display_name_en=display_name_en,
        aliases=aliases,
        requirements=MethodRequirements(
            variable_roles=roles,
            minimum_sample_size=minimum_sample_size,
            minimum_groups=minimum_groups,
            maximum_groups=maximum_groups,
            minimum_group_size=minimum_group_size,
        ),
        comparison_tolerances=metrics,
        python=BackendCapability(supported=True, validated=python_validated),
        spss=BackendCapability(supported=True, validated=True),
    )


_PUBLIC_CAPABILITIES: tuple[MethodCapability, ...] = (
    _capability(
        "descriptives",
        "描述统计",
        "Descriptive statistics",
        ("variable",),
        (
            _tol("n_valid", 0, 0),
            _tol("mean"),
            _tol("std_dev"),
        ),
        minimum_sample_size=1,
    ),
    _capability(
        "frequencies",
        "频数分析",
        "Frequencies",
        ("variable",),
        (_tol("n_valid", 0, 0),),
        minimum_sample_size=1,
    ),
    _capability(
        "independent_t_test",
        "独立样本 t 检验",
        "Independent-samples t-test",
        ("grouping_variable", "test_variable"),
        (
            _tol("t_value"),
            _tol("df", 0, 0),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=4,
        minimum_groups=2,
        maximum_groups=2,
        minimum_group_size=2,
    ),
    _capability(
        "paired_t_test",
        "配对样本 t 检验",
        "Paired-samples t-test",
        ("first_variable", "second_variable"),
        (
            _tol("t_value"),
            _tol("df", 0, 0),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=2,
    ),
    _capability(
        "oneway_anova",
        "单因素方差分析",
        "One-way ANOVA",
        ("grouping_variable", "test_variable"),
        (
            _tol("f_value"),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=4,
        minimum_groups=2,
        minimum_group_size=2,
    ),
    _capability(
        "pearson_correlation",
        "皮尔逊相关",
        "Pearson correlation",
        ("first_variable", "second_variable"),
        (_tol("r"), _tol("p_value", 1e-4, 1e-2), _tol("n_valid", 0, 0)),
        aliases=("correlations",),
        minimum_sample_size=3,
    ),
    _capability(
        "spearman_correlation",
        "斯皮尔曼相关",
        "Spearman correlation",
        ("first_variable", "second_variable"),
        (_tol("r"), _tol("p_value", 1e-4, 1e-2), _tol("n_valid", 0, 0)),
        minimum_sample_size=3,
    ),
    _capability(
        "chi_square",
        "卡方检验",
        "Chi-square test",
        ("row_variable", "column_variable"),
        (
            _tol("chi_square"),
            _tol("df", 0, 0),
            _tol("p_value", 1e-4, 1e-2),
            _tol("n_valid", 0, 0),
        ),
        aliases=("crosstabs",),
        minimum_sample_size=4,
        minimum_groups=2,
    ),
    _capability(
        "mann_whitney_u",
        "Mann-Whitney U 检验",
        "Mann-Whitney U test",
        ("grouping_variable", "test_variable"),
        (
            _tol("u_statistic"),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=4,
        minimum_groups=2,
        maximum_groups=2,
        minimum_group_size=2,
    ),
    _capability(
        "kruskal_wallis",
        "Kruskal-Wallis 检验",
        "Kruskal-Wallis test",
        ("grouping_variable", "test_variable"),
        (
            _tol("h_statistic"),
            _tol("df", 0, 0),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=3,
        minimum_groups=2,
    ),
    _capability(
        "simple_regression",
        "简单线性回归",
        "Simple linear regression",
        ("dependent_variable", "independent_variable"),
        (
            _tol("r_squared"),
            _tol("t_value"),
            _tol("p_value", 1e-4, 1e-2),
        ),
        minimum_sample_size=3,
    ),
)
_CAPABILITIES_BY_NAME = {capability.name: capability for capability in _PUBLIC_CAPABILITIES}
_ALIASES = {
    alias: capability.name for capability in _PUBLIC_CAPABILITIES for alias in capability.aliases
}
_NORMALIZED_ALIASES = {
    **_ALIASES,
    "independent_samples_t_test": "independent_t_test",
    "independentsamplesttest": "independent_t_test",
    "t_test": "independent_t_test",
    "ttest": "independent_t_test",
    "independent": "independent_t_test",
    "pairedsamplesttest": "paired_t_test",
    "paired": "paired_t_test",
    "one_way_anova": "oneway_anova",
    "anova": "oneway_anova",
    "mannwhitney": "mann_whitney_u",
    "mann_whitney": "mann_whitney_u",
    "kruskalwallis": "kruskal_wallis",
    "correlation": "pearson_correlation",
    "corr": "pearson_correlation",
    "regression": "simple_regression",
    "regress": "simple_regression",
    "chi": "chi_square",
    "crosstab": "chi_square",
    "cross_tab": "chi_square",
    "freq": "frequencies",
    "describe": "descriptives",
}


def get_public_capabilities() -> tuple[MethodCapability, ...]:
    """Return the immutable 0.9 public capability list."""

    return _PUBLIC_CAPABILITIES


def get_public_capabilities_payload() -> list[dict[str, object]]:
    """Return the JSON-safe capability contract shared by all entry points."""

    payload: list[dict[str, object]] = []
    for capability in _PUBLIC_CAPABILITIES:
        payload.append(
            {
                "name": capability.name,
                "display_name": {
                    "zh": capability.display_name_zh,
                    "en": capability.display_name_en,
                },
                "aliases": list(capability.aliases),
                "requirements": {
                    "variable_roles": list(capability.requirements.variable_roles),
                    "minimum_sample_size": capability.requirements.minimum_sample_size,
                    "minimum_groups": capability.requirements.minimum_groups,
                    "maximum_groups": capability.requirements.maximum_groups,
                    "minimum_group_size": capability.requirements.minimum_group_size,
                },
                "backends": {
                    "python": {
                        "supported": capability.python.supported,
                        "validated": capability.python.validated,
                    },
                    "spss": {
                        "supported": capability.spss.supported,
                        "validated": capability.spss.validated,
                    },
                },
                "fallback_to_python": can_fallback_to_python(capability.name),
                "comparison_tolerances": [
                    {
                        "metric": tolerance.metric,
                        "absolute": tolerance.absolute,
                        "relative": tolerance.relative,
                        "rationale": tolerance.rationale,
                    }
                    for tolerance in capability.comparison_tolerances
                ],
            }
        )
    return payload


def canonicalize_method(method: str) -> str:
    """Return the canonical public name for a method or legacy alias."""

    normalized = str(method).strip().lower().replace(" ", "").replace("-", "_")
    return _NORMALIZED_ALIASES.get(normalized, normalized)


def get_capability(method: str) -> MethodCapability | None:
    """Look up a public capability by canonical name or legacy alias."""

    return _CAPABILITIES_BY_NAME.get(canonicalize_method(method))


def is_backend_supported(method: str, backend: str) -> bool:
    """Return whether a public method has an implementation for ``backend``."""

    capability = get_capability(method)
    status = capability.backend(backend) if capability else None
    return bool(status and status.supported)


def is_backend_validated(method: str, backend: str) -> bool:
    """Return whether a public method is release-validated for ``backend``."""

    capability = get_capability(method)
    status = capability.backend(backend) if capability else None
    return bool(status and status.validated)


def can_fallback_to_python(method: str) -> bool:
    """Return whether transparent SPSS-to-Python fallback is permitted."""

    return is_backend_supported(method, "python") and is_backend_validated(method, "python")


__all__ = [
    "BackendCapability",
    "MethodRequirements",
    "MethodCapability",
    "MetricTolerance",
    "can_fallback_to_python",
    "canonicalize_method",
    "get_capability",
    "get_public_capabilities",
    "get_public_capabilities_payload",
    "is_backend_supported",
    "is_backend_validated",
]
