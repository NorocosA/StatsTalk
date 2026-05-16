"""Pre-built SPSS Syntax Templates and Method Validation Rule Engine.

This module serves as the **Layer 3 fallback** in the SNLA error recovery chain:

    Layer 1: validator.py        — Syntax security sandbox (pre-check)
    Layer 2: LLM retry           — Ask the LLM to fix failed syntax
    Layer 3: Template fallback   — THIS MODULE (pre-built static templates)
    Layer 4: User manual edit    — Human intervention

It provides two major components:

1.  **Syntax Templates** (Part 1) — Six pre-built SPSS syntax template functions
    covering the most common analysis types. Each takes variable parameters and
    returns a complete, valid SPSS command string (with period terminator).
    A ``TEMPLATE_MAP`` dict and a ``get_syntax_by_method()`` convenience
    function make lookup and execution straightforward.

2.  **Method Validation Rule Engine** (Part 2) — ``validate_method()`` performs
    a rule-based double-check on LLM-recommended statistical methods by
    comparing their requirements against the actual variable metadata (type,
    value labels, etc.). It returns validation errors, warnings, and — where
    possible — a corrected method suggestion.

Usage:
    from snla.syntax.templates import get_syntax_by_method, validate_method

    # Generate syntax from template
    syntax = get_syntax_by_method("independent_t_test",
                                  group_var="gender",
                                  test_var="score",
                                  groups=(1, 2))

    # Validate a method recommendation
    result = validate_method(variables, "independent_t_test",
                             grouping_var="gender", test_var="score")
    if not result["valid"]:
        print(result["errors"])
        print("Suggested:", result["corrected_method"])
"""

from typing import Any, Callable, Dict, List, Optional

# ============================================================================
# PART 1 — Syntax Templates
# ============================================================================


def ttest_independent(group_var: str, test_var: str, groups: tuple) -> str:
    """Build an independent-samples *t*-test command.

    The ``T-TEST`` command compares means of *test_var* across two groups
    defined by *group_var*.

    Args:
        group_var: Name of the binary grouping variable.
        test_var:  Name of the continuous test variable.
        groups:    A 2-tuple ``(val1, val2)`` specifying the two group values.

    Returns:
        Complete SPSS syntax string, e.g.::

            T-TEST GROUPS=gender(1 2)
              /VARIABLES=score.

    Example:
        >>> ttest_independent("gender", "score", (1, 2))
        'T-TEST GROUPS=gender(1 2)\\n  /VARIABLES=score.'
    """
    return (
        f"T-TEST GROUPS={group_var}({groups[0]} {groups[1]})\n"
        f"  /VARIABLES={test_var}."
    )


def anova_oneway(group_var: str, test_var: str) -> str:
    """Build a one-way ANOVA command.

    The ``ONEWAY`` command performs a one-way analysis of variance. This
    template also requests descriptive statistics and a homogeneity-of-variance
    test.

    Args:
        group_var: Name of the factor / grouping variable.
        test_var:  Name of the continuous dependent variable.

    Returns:
        Complete SPSS syntax string, e.g.::

            ONEWAY score BY group
              /STATISTICS DESCRIPTIVES HOMOGENEITY.

    Note:
        SPSS ``ONEWAY`` uses the order ``dependent BY factor``, which is the
        **opposite** of the ``T-TEST`` convention.
    """
    return (
        f"ONEWAY {test_var} BY {group_var}\n"
        f"  /STATISTICS DESCRIPTIVES HOMOGENEITY."
    )


def regression_simple(dep_var: str, indep_var: str) -> str:
    """Build a simple linear regression command.

    The ``REGRESSION`` command estimates a linear model with one predictor.
    Statistics requested include unstandardised coefficients, model fit (R),
    and the ANOVA table.

    Args:
        dep_var:   Name of the dependent (outcome) variable.
        indep_var: Name of the independent (predictor) variable.

    Returns:
        Complete SPSS syntax string, e.g.::

            REGRESSION
              /DEPENDENT score
              /METHOD=ENTER age
              /STATISTICS COEFF R ANOVA.

    Example:
        >>> regression_simple("score", "age")
        'REGRESSION\\n  /DEPENDENT score\\n  /METHOD=ENTER age\\n  /STATISTICS COEFF R ANOVA.'
    """
    return (
        f"REGRESSION\n"
        f"  /DEPENDENT {dep_var}\n"
        f"  /METHOD=ENTER {indep_var}\n"
        f"  /STATISTICS COEFF R ANOVA."
    )


def crosstabs(row_var: str, col_var: str) -> str:
    """Build a crosstabulation (contingency table) command.

    The ``CROSSTABS`` command produces a contingency table with the chi-squared
    test of independence and the phi coefficient.

    Args:
        row_var: Name of the row variable.
        col_var: Name of the column variable.

    Returns:
        Complete SPSS syntax string, e.g.::

            CROSSTABS
              /TABLES=gender BY education
              /STATISTICS=CHISQ PHI.

    Example:
        >>> crosstabs("gender", "education")
        'CROSSTABS\\n  /TABLES=gender BY education\\n  /STATISTICS=CHISQ PHI.'
    """
    return (
        f"CROSSTABS\n"
        f"  /TABLES={row_var} BY {col_var}\n"
        f"  /STATISTICS=CHISQ PHI."
    )


def frequencies(var: str) -> str:
    """Build a frequency table command with a bar chart.

    The ``FREQUENCIES`` command produces a frequency table and a bar chart for
    a single variable.

    Args:
        var: Name of the variable.

    Returns:
        Complete SPSS syntax string, e.g.::

            FREQUENCIES VARIABLES=gender
              /BARCHART
              /ORDER=ANALYSIS.

    Example:
        >>> frequencies("gender")
        'FREQUENCIES VARIABLES=gender\\n  /BARCHART\\n  /ORDER=ANALYSIS.'
    """
    return (
        f"FREQUENCIES VARIABLES={var}\n"
        f"  /BARCHART\n"
        f"  /ORDER=ANALYSIS."
    )


def descriptives(var: str) -> str:
    """Build a descriptive statistics command.

    The ``DESCRIPTIVES`` command produces summary statistics (mean, std dev,
    minimum, maximum) for a single variable.

    Args:
        var: Name of the variable.

    Returns:
        Complete SPSS syntax string, e.g.::

            DESCRIPTIVES VARIABLES=score
              /STATISTICS=MEAN STDDEV MIN MAX.

    Example:
        >>> descriptives("score")
        'DESCRIPTIVES VARIABLES=score\\n  /STATISTICS=MEAN STDDEV MIN MAX.'
    """
    return (
        f"DESCRIPTIVES VARIABLES={var}\n"
        f"  /STATISTICS=MEAN STDDEV MIN MAX."
    )


def correlations(var1: str, var2: str) -> str:
    """Build a Pearson correlation command.

    The ``CORRELATIONS`` command computes Pearson correlation coefficients
    between two variables.

    Args:
        var1: First variable name.
        var2: Second variable name.

    Returns:
        Complete SPSS syntax string, e.g.::

            CORRELATIONS
              /VARIABLES=score age
              /PRINT=TWOTAIL NOSIG
              /STATISTICS DESCRIPTIVES.

    Example:
        >>> correlations("score", "age")
        'CORRELATIONS\\n  /VARIABLES=score age\\n  /PRINT=TWOTAIL NOSIG\\n  /STATISTICS DESCRIPTIVES.'
    """
    return (
        f"CORRELATIONS\n"
        f"  /VARIABLES={var1} {var2}\n"
        f"  /PRINT=TWOTAIL NOSIG\n"
        f"  /STATISTICS DESCRIPTIVES."
    )


def mann_whitney(group_var: str, test_var: str, groups: tuple = (1, 2)) -> str:
    """Build a Mann-Whitney U test (non-parametric independent t-test).

    Uses ``NPAR TESTS /M-W`` for two independent samples.
    """
    return (
        f"NPAR TESTS\n"
        f"  /M-W= {test_var} BY {group_var}({groups[0]} {groups[1]})\n"
        f"  /STATISTICS DESCRIPTIVES."
    )


def kruskal_wallis(group_var: str, test_var: str) -> str:
    """Build a Kruskal-Wallis test (non-parametric one-way ANOVA).

    Uses ``NPAR TESTS /K-W`` for k independent samples.
    """
    return (
        f"NPAR TESTS\n"
        f"  /K-W= {test_var} BY {group_var}(1 99)\n"
        f"  /STATISTICS DESCRIPTIVES."
    )


def spearman_correlation(var1: str, var2: str) -> str:
    """Build a Spearman rank correlation (non-parametric).

    Uses ``NONPAR CORR`` with SPEARMAN keyword.
    """
    return (
        f"NONPAR CORR\n"
        f"  /VARIABLES={var1} {var2}\n"
        f"  /PRINT=SPEARMAN TWOTAIL NOSIG."
    )


def paired_ttest(var1: str, var2: str) -> str:
    """Build a paired-samples t-test."""
    return (
        f"T-TEST PAIRS={var1} WITH {var2} (PAIRED)\n"
        f"  /CRITERIA=CI(0.95)\n"
        f"  /MISSING=ANALYSIS."
    )


# ---------------------------------------------------------------------------
# Template Registry
# ---------------------------------------------------------------------------

TEMPLATE_MAP: Dict[str, Callable[..., str]] = {
    "independent_t_test": ttest_independent,
    "paired_t_test": paired_ttest,
    "oneway_anova": anova_oneway,
    "simple_regression": regression_simple,
    "chi_square": crosstabs,
    "frequencies": frequencies,
    "descriptives": descriptives,
    "correlations": correlations,
    "pearson_correlation": correlations,
    "spearman_correlation": spearman_correlation,
    "mann_whitney_u": mann_whitney,
    "kruskal_wallis": kruskal_wallis,
    # Graph aliases — fall back to closest statistical equivalent
    "bar_chart": anova_oneway,
    "histogram": frequencies,
    "scatter": correlations,
    "boxplot": ttest_independent,
    "qq_plot": descriptives,
}
"""Map of statistical method keys to their corresponding template functions.

Each value is a callable that accepts keyword arguments specific to that
template and returns a complete SPSS syntax string.
"""


def get_syntax_by_method(method: str, **kwargs: Any) -> str:
    """Look up and execute the template for a given statistical method.

    This is a convenience wrapper around :data:`TEMPLATE_MAP` that performs
    the lookup and calls the template function in one step.

    Args:
        method: Statistical method key (e.g. ``"independent_t_test"``).
                Must be a key present in :data:`TEMPLATE_MAP`.
        **kwargs: Keyword arguments forwarded to the underlying template
                  function. The required arguments differ per method:

                  - ``independent_t_test``: ``group_var``, ``test_var``, ``groups``
                  - ``oneway_anova``: ``group_var``, ``test_var``
                  - ``simple_regression``: ``dep_var``, ``indep_var``
                  - ``chi_square``: ``row_var``, ``col_var``
                  - ``frequencies``: ``var``
                  - ``descriptives``: ``var``

    Returns:
        Complete SPSS syntax string produced by the template function.

    Raises:
        ValueError: If *method* is not found in :data:`TEMPLATE_MAP`.

    Example:
        >>> get_syntax_by_method("independent_t_test",
        ...                      group_var="gender",
        ...                      test_var="score",
        ...                      groups=(1, 2))
        'T-TEST GROUPS=gender(1 2)\\n  /VARIABLES=score.'

        >>> get_syntax_by_method("descriptives", var="score")
        'DESCRIPTIVES VARIABLES=score\\n  /STATISTICS=MEAN STDDEV MIN MAX.'
    """
    if method not in TEMPLATE_MAP:
        known = ", ".join(sorted(TEMPLATE_MAP))
        raise ValueError(
            f"Unknown method '{method}'. "
            f"Available methods: {known}"
        )
    return TEMPLATE_MAP[method](**kwargs)


# ============================================================================
# PART 2 — Method Validation Rule Engine
# ============================================================================


def _find_var(variables: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """Find a variable dictionary by name in the variable metadata list.

    Args:
        variables: List of variable dicts, each containing at least a
            ``"name"`` key.
        name: The variable name to search for (case-sensitive).

    Returns:
        The matching variable dict, or ``None`` if no variable with that name
        exists in the list.
    """
    for var in variables:
        if var.get("name") == name:
            return var
    return None


def _is_categorical(var: Dict[str, Any]) -> bool:
    """Determine whether a variable is categorical (nominal/ordinal).

    A variable is considered categorical if:

    - Its type is ``"Numeric"`` **and** it has non-empty ``value_labels``, or
    - Its type is ``"String"`` (text variables are inherently categorical).

    Args:
        var: A variable metadata dictionary with ``"type"`` and
            ``"value_labels"`` keys.

    Returns:
        ``True`` if the variable is categorical, ``False`` otherwise.
    """
    var_type = var.get("type", "")
    labels = var.get("value_labels")
    has_labels = labels is not None and isinstance(labels, dict) and len(labels) > 0
    return (var_type == "Numeric" and has_labels) or var_type == "String"


def _is_continuous(var: Dict[str, Any]) -> bool:
    """Determine whether a variable is continuous (scale).

    A variable is considered continuous if its type is ``"Numeric"`` **and**
    it has no value labels assigned.

    Args:
        var: A variable metadata dictionary with ``"type"`` and
            ``"value_labels"`` keys.

    Returns:
        ``True`` if the variable is continuous, ``False`` otherwise.
    """
    if var.get("type") != "Numeric":
        return False
    labels = var.get("value_labels")
    return labels is None or (isinstance(labels, dict) and len(labels) == 0)


def _count_groups(var: Dict[str, Any]) -> Optional[int]:
    """Count the number of groups defined in a variable's value labels.

    Args:
        var: A variable metadata dictionary with a ``"value_labels"`` key.

    Returns:
        The number of unique value labels, or ``None`` if the variable has no
        value labels.
    """
    labels = var.get("value_labels")
    if labels is not None and isinstance(labels, dict) and len(labels) > 0:
        return len(labels)
    return None


def validate_method(
    variables: List[Dict[str, Any]],
    recommended_method: str,
    grouping_var: Optional[str] = None,
    test_var: Optional[str] = None,
    row_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Validate an LLM-recommended statistical method against variable types.

    This function acts as a **rule engine** that double-checks whether a
    statistical method is appropriate for the given variable metadata. It
    checks data type compatibility, number of groups, and sample size.

    **Validation Rules**

    1. **Grouping variable must be categorical** (for *t*-test / ANOVA).
       If *grouping_var* is continuous (Numeric with no value labels), an
       error is raised.

    2. **Test variable must be continuous** (for *t*-test / ANOVA / regression).
       If *test_var* is a String variable, an error is raised.

    3. **Number of groups vs. method** — When the grouping variable has 3 or
       more value labels but the recommended method is ``independent_t_test``,
       a warning is emitted suggesting ``oneway_anova`` instead.

    4. **Sample size** — If *row_count* is provided and fewer than 3, a
       warning about unreliable results is returned.

    5. **Automatic method correction** — Where possible, ``corrected_method``
       is set to a more appropriate alternative:
       - ``independent_t_test`` / ``oneway_anova`` with a continuous grouping
         variable → ``"pearson_correlation"`` (if test variable is continuous).
       - ``independent_t_test`` with 3+ groups → ``"oneway_anova"``.

    Args:
        variables: List of variable metadata dictionaries. Each dict must
            contain at least:
            ``{"name": str, "type": str, "value_labels": dict | None}``.
            The ``"type"`` should be ``"Numeric"`` or ``"String"``.
            ``"value_labels"`` is ``None`` or a dict mapping numeric codes
            to label strings.
        recommended_method: The statistical method recommended by the LLM.
            Expected values match the keys in :data:`TEMPLATE_MAP`:
            ``"independent_t_test"``, ``"oneway_anova"``,
            ``"simple_regression"``, ``"chi_square"``, ``"frequencies"``,
            ``"descriptives"``.
        grouping_var: The name of the variable designated as the grouping /
            factor variable. Required for ``independent_t_test``,
            ``oneway_anova``, and ``chi_square`` validation. May be ``None``
            for methods that do not use a grouping variable.
        test_var: The name of the variable designated as the test / dependent
            variable. Required for ``independent_t_test``, ``oneway_anova``,
            and ``simple_regression`` validation. May be ``None`` for
            descriptive methods.
        row_count: Optional total number of data rows (cases). When provided,
            enables the sample-size heuristic check (Rule 4).

    Returns:
        A dictionary with the following keys:

        - **valid** (``bool``) — ``True`` when there are zero errors.
        - **errors** (``list[str]``) — Blocking errors that prevent the
          method from being used.
        - **warnings** (``list[str]``) — Non-blocking advisory messages.
        - **corrected_method** (``str | None``) — A suggested alternative
          method when the recommended one is inappropriate, or ``None`` if
          no correction is available.

    Example:
        >>> variables = [
        ...     {"name": "gender", "type": "Numeric",
        ...      "label": "Gender", "value_labels": {1: "Male", 2: "Female"}},
        ...     {"name": "score", "type": "Numeric",
        ...      "label": "Test Score", "value_labels": None},
        ... ]
        >>> result = validate_method(variables, "independent_t_test",
        ...                          grouping_var="gender", test_var="score")
        >>> result["valid"]
        True

        >>> # String test variable → error
        >>> variables[1]["type"] = "String"
        >>> result = validate_method(variables, "independent_t_test",
        ...                          grouping_var="gender", test_var="score")
        >>> result["valid"]
        False
        >>> result["errors"]
        ['检验变量 score 为字符串类型，无法进行数值分析']
    """
    errors: List[str] = []
    warnings: List[str] = []
    corrected_method: Optional[str] = None

    # Resolve variable metadata (may be None if variable is not in the list).
    group_info = _find_var(variables, grouping_var) if grouping_var else None
    test_info = _find_var(variables, test_var) if test_var else None

    # ------------------------------------------------------------------
    # Rule 1 — Grouping variable must be categorical
    # Applies to: independent_t_test, oneway_anova
    # ------------------------------------------------------------------
    if recommended_method in ("independent_t_test", "oneway_anova") and group_info:
        if not _is_categorical(group_info):
            errors.append(
                f"分组变量 {grouping_var} 为连续变量，不适合做分组"
            )
            # Rule 5 — Collateral correction suggestion
            if test_info and _is_continuous(test_info):
                corrected_method = "pearson_correlation"

    # ------------------------------------------------------------------
    # Rule 2 — Test / dependent variable must be continuous
    # Applies to: independent_t_test, oneway_anova, simple_regression
    # ------------------------------------------------------------------
    if recommended_method in (
        "independent_t_test",
        "oneway_anova",
        "simple_regression",
    ) and test_info:
        if test_info.get("type") == "String":
            errors.append(
                f"检验变量 {test_var} 为字符串类型，无法进行数值分析"
            )

    # ------------------------------------------------------------------
    # Rule 3 — Number of groups vs. method
    # Applies to: independent_t_test (suggest ANOVA for 3+ groups)
    # ------------------------------------------------------------------
    if recommended_method == "independent_t_test" and group_info:
        n_groups = _count_groups(group_info)
        if n_groups is not None and n_groups >= 3:
            warnings.append(
                f"分组变量 {grouping_var} 有 {n_groups} 个类别，"
                f"建议使用单因素方差分析(ANOVA)"
            )
            if corrected_method is None:
                corrected_method = "oneway_anova"

    # ------------------------------------------------------------------
    # Rule 4 — Sample size check
    # ------------------------------------------------------------------
    if row_count is not None and row_count < 3:
        warnings.append(
            "样本量较小（n<3），统计检验结果可能不可靠"
        )

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "corrected_method": corrected_method,
    }
