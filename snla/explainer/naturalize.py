"""
Statistical result explainer with constraint layer (rules engine)
and optional LLM polish layer.

Architecture
============
Layer 1 — Constraint Layer (MANDATORY, runs first):
    :func:`apply_constraints` applies non-negotiable rule-based
    constraints from Plan.md §3.10.  This runs BEFORE any LLM
    interaction, ensuring the LLM can never override statistical
    conclusions.

Layer 2 — Template Explanation (default, no LLM):
    :func:`explain_template` produces a deterministic Chinese
    explanation using only formatting rules.  100 % reproducible.

Layer 3 — LLM Polish (optional, A/B controllable):
    :func:`build_polish_prompt` constructs a prompt that constrains
    the LLM to rephrase only the forced phrase for readability
    without modifying the statistical conclusion.

Unified entry point:
    :func:`explain` chains Layer 1 → (Layer 3 if configured) → Layer 2.
    Always falls back to Layer 2 on LLM errors.
"""

from __future__ import annotations

from typing import Any

from snla.llm.client import LLMClient
from snla.parser.schema import AnalysisResult

# ===========================================================================
# Public API
# ===========================================================================


def apply_constraints(analysis_result: AnalysisResult) -> dict[str, Any]:
    """Apply statistical constraint rules to parsed analysis results.

    THIS RUNS BEFORE ANY LLM POLISH. The rules are non-negotiable.
    LLM can only rephrase the *forced_phrase* but must NOT change its
    meaning.

    Args:
        analysis_result:
            Parsed :class:`~snla.parser.schema.AnalysisResult` from
            the parser layer.

    Returns:
        A dict with the following keys:

        * **significance** — ``"SIGNIFICANT"`` | ``"NOT_SIG"`` |
          ``"EDGE_SIGNIFICANT"`` | ``"UNKNOWN"``
        * **forced_phrase** — Mandatory conclusion text (str).
        * **effect_size_desc** — Effect-size description (str).
          May be the empty string when Cohen's *d* is unavailable.
        * **details** — Numerical values for LLM polish (dict).
        * **forbidden_phrases** — Phrases the LLM MUST NOT use
          (list[str]).
    """
    stats = analysis_result.statistics
    p_value = stats.get("p_value")
    d_value = stats.get("d_value")
    r_squared = stats.get("r_squared")
    analysis_type = analysis_result.analysis_type

    # 1. p-value significance (or descriptive fallback)
    if p_value is None and analysis_type in ("DESCRIPTIVES", "FREQUENCIES"):
        significance, forced_phrase = "DESCRIPTIVE", _build_descriptive_phrase(stats)
    else:
        significance, forced_phrase = _interpret_p_value(p_value)

    # 2. Effect size (Cohen's d)
    effect_size_desc = _interpret_effect_size(d_value)

    # 3. R² interpretation (append to forced_phrase)
    if r_squared is not None:
        r_sq = float(r_squared)
        if r_sq < 0.1:
            forced_phrase += "，模型解释力有限"
        else:
            forced_phrase += "，模型具有一定解释力"

    # 4. Forbidden phrases
    forbidden_phrases = _build_forbidden_phrases(significance, p_value)

    # 5. Details dict (normalised numerical values)
    details = _build_details(stats)

    return {
        "significance": significance,
        "forced_phrase": forced_phrase,
        "effect_size_desc": effect_size_desc,
        "details": details,
        "forbidden_phrases": forbidden_phrases,
    }


def explain_template(
    constraints: dict[str, Any],
    analysis_result: AnalysisResult,
) -> str:
    """Generate a deterministic natural-language explanation (no LLM).

    This is the reliable fallback when LLM polish is disabled or the
    LLM is unavailable.  The output is 100 % reproducible from the
    same inputs.

    Args:
        constraints:
            Output of :func:`apply_constraints`.
        analysis_result:
            The parsed analysis result providing table metadata and
            raw values not yet in the constraints dict.

    Returns:
        Chinese explanation string.
    """
    analysis_type = analysis_result.analysis_type
    stats = analysis_result.statistics
    significance = constraints["significance"]
    forced_phrase = constraints["forced_phrase"]
    effect_size_desc = constraints["effect_size_desc"]
    details = constraints["details"]

    type_label = _analysis_type_label(analysis_type)

    t_value = details.get("t_value")
    f_value = details.get("f_value")
    p_value = details.get("p_value")
    chi2_value = details.get("chi2_value")
    d_value = details.get("d_value")
    r_squared = details.get("r_squared")
    mean_a = details.get("mean_a")
    mean_b = details.get("mean_b")
    label_a = details.get("label_a", "组A")
    label_b = details.get("label_b", "组B")
    mean_diff = details.get("mean_diff")

    # ---- Build statistical-value inline string ----------------------------
    stat_items: list[str] = []

    if t_value is not None:
        stat_items.append(f"t={t_value:.3f}")
    if f_value is not None:
        stat_items.append(f"F={f_value:.3f}")
    if chi2_value is not None:
        stat_items.append(f"χ²={chi2_value:.3f}")
    if details.get("r") is not None and "r_squared" not in details:
        stat_items.append(f"r={details['r']:.3f}")

    # Append p-value with comparison operators — but *skip* for
    # EDGE_SIGNIFICANT because the forced_phrase already embeds the
    # p-value in its natural-language message.
    if p_value is not None and significance != "EDGE_SIGNIFICANT":
        if significance == "SIGNIFICANT":
            stat_items.append(f"p={p_value:.3f}<0.05")
        elif significance == "NOT_SIG":
            stat_items.append(f"p={p_value:.3f}>0.05")
        else:
            stat_items.append(f"p={p_value:.3f}")

    stat_str = f"（{'，'.join(stat_items)}）" if stat_items else ""

    # ---- Compose sentences ------------------------------------------------
    sentences: list[str] = []

    # Sentence 1:  According to {test}, {forced_phrase}
    if significance == "DESCRIPTIVE":
        main = f"根据{type_label}结果，{forced_phrase}。"
    else:
        main = f"根据{type_label}结果，{forced_phrase}{stat_str}。"
    sentences.append(main)

    # Sentence(s) for group means
    if mean_a is not None and mean_b is not None:
        mean_parts = [f"{label_a}均值为{mean_a:.1f}，{label_b}均值为{mean_b:.1f}"]
        if mean_diff is not None:
            mean_parts.append(f"均值差为{mean_diff:.1f}")
        sentences.append("，".join(mean_parts) + "。")

    # Effect-size description
    if effect_size_desc:
        if d_value is not None:
            sentences.append(f"{effect_size_desc}（Cohen's d={d_value}）。")
        else:
            sentences.append(f"{effect_size_desc}。")

    # R² mention (the qualitative assessment is already in forced_phrase,
    # here we add the actual value)
    if r_squared is not None:
        sentences.append(f"R²={r_squared}。")

    # Clarifying tail sentence for NOT_SIG when we have group means
    if significance == "NOT_SIG" and mean_a is not None and mean_b is not None:
        if mean_diff is not None:
            sentences.append(
                f"虽然{label_a}与{label_b}之间存在{abs(mean_diff)}分的差值，"
                "但该差异未达统计学显著水平。"
            )
        else:
            sentences.append("但该差异未达统计学显著水平。")

    return "".join(sentences)


def build_polish_prompt(
    constraints: dict[str, Any],
    analysis_result: AnalysisResult,
) -> list[dict[str, str]]:
    """Build an LLM prompt for polishing the constrained explanation.

    The LLM is **only** allowed to rephrase the *forced_phrase* for
    readability.  It MUST NOT change the statistical conclusion.

    Args:
        constraints:
            Output of :func:`apply_constraints`.
        analysis_result:
            The parsed analysis result.

    Returns:
        A message list suitable for passing to
        :meth:`LLMClient.chat() <snla.llm.client.LLMClient.chat>`.
    """
    details = constraints["details"]
    forced_phrase = constraints["forced_phrase"]
    forbidden = constraints["forbidden_phrases"]

    t_value = details.get("t_value")
    f_value = details.get("f_value")
    p_value = details.get("p_value")
    d_value = details.get("d_value")
    r_squared = details.get("r_squared")
    mean_a = details.get("mean_a")
    mean_b = details.get("mean_b")
    label_a = details.get("label_a", "组A")
    label_b = details.get("label_b", "组B")
    mean_diff = details.get("mean_diff")

    forbidden_str = "；".join(forbidden) if forbidden else "无"

    # ---- System prompt ----------------------------------------------------
    system_prompt = (
        "你是面向社科本科生的统计结果解说员。你的任务是用极通俗的口语复述统计结论，"
        "让没有统计背景的人也能听懂。你必须严格遵守给定的统计结论，不得修改其含义。"
    )

    # ---- Build STATISTICAL FACTS block ------------------------------------
    facts_lines = [
        "[STATISTICAL FACTS — 必须严格遵守以下事实，不得修改]",
        f"- 显著性结论: {forced_phrase}",
    ]

    num_parts: list[str] = []
    if t_value is not None:
        num_parts.append(f"t={t_value}")
    if f_value is not None:
        num_parts.append(f"F={f_value}")
    if p_value is not None:
        num_parts.append(f"p={p_value}")
    if mean_a is not None and mean_b is not None:
        num_parts.append(f"{label_a}={mean_a}")
        num_parts.append(f"{label_b}={mean_b}")
    if mean_diff is not None:
        num_parts.append(f"均值差={mean_diff}")
    if d_value is not None:
        num_parts.append(f"Cohen's d={d_value}")
    if r_squared is not None:
        num_parts.append(f"R²={r_squared}")

    facts_lines.append(f"- 具体数值: {', '.join(num_parts)}")
    facts_lines.append(f"- 不允许使用的措辞: {forbidden_str}")
    facts_lines.append("- 允许使用的措辞: 仅上述强制表述 + 数值的通俗化表达")
    facts_lines.append(
        "\n请用面向社科本科生的通俗语言复述以上结论，可添加数值解释，但统计结论必须与上述完全一致。"
    )

    user_content = "\n".join(facts_lines)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def explain(
    analysis_result: AnalysisResult,
    use_llm_polish: bool = False,
    llm_client: LLMClient | None = None,
) -> str:
    """Generate a natural-language explanation for analysis results.

    Always applies the constraint layer first.  Optionally passes the
    constrained output to an LLM for polishing.  Falls back to the
    deterministic template on LLM errors.

    Args:
        analysis_result:
            Parsed :class:`~snla.parser.schema.AnalysisResult`.
        use_llm_polish:
            If ``True``, attempt LLM polish.  Default ``False``.
        llm_client:
            :class:`~snla.llm.client.LLMClient` instance required when
            *use_llm_polish* is ``True``.

    Returns:
        Chinese explanation string.
    """
    # Step 1: Always apply constraints first (NON-NEGOTIABLE)
    constraints = apply_constraints(analysis_result)

    # Step 2: Optional LLM polish
    if use_llm_polish and llm_client is not None:
        try:
            messages = build_polish_prompt(constraints, analysis_result)
            response = llm_client.chat(messages)
            return response["content"]
        except Exception:
            pass  # Silently fall back to template on any LLM failure

    # Step 3: Template fallback (always works, no external dependencies)
    return explain_template(constraints, analysis_result)


# ===========================================================================
# Internal helpers
# ===========================================================================


def _build_descriptive_phrase(stats: dict[str, Any]) -> str:
    """Build a forced phrase for descriptive statistics (no p-value)."""
    parts = []
    mean = stats.get("mean")
    std = stats.get("std_dev") or stats.get("std_deviation")
    n = stats.get("n_valid") or stats.get("n")
    min_v = stats.get("minimum")
    max_v = stats.get("maximum")

    if mean is not None:
        parts.append(f"均值为{mean:.1f}")
    if std is not None:
        parts.append(f"标准差为{std:.1f}")
    if min_v is not None and max_v is not None:
        parts.append(f"范围从{min_v:.1f}到{max_v:.1f}")
    if n is not None:
        parts.append(f"样本量为{int(n)}")

    if parts:
        return "，".join(parts)
    return "已完成描述性统计"


def _interpret_p_value(p_value: float | None) -> tuple[str, str]:
    """Return ``(significance_level, forced_phrase)`` based on *p_value*.

    Boundary rules (Plan.md §3.10):

    ============= ============== =============================================
    Condition     Significance   Forced phrase
    ============= ============== =============================================
    ``p ≤ 0.05``  SIGNIFICANT    存在统计学上的显著差异/关系
    ``p < 0.10``  EDGE_SIG      未达统计学显著水平，但接近边缘显著…
    otherwise     NOT_SIG        未发现统计学上的显著差异/关系
    ``None``      UNKNOWN        无法判断显著性（p值缺失）
    ============= ============== =============================================

    Boundary verification:

    * ``p = 0.050`` → ``"SIGNIFICANT"``, ``"存在统计学上的显著差异/关系"``
    * ``p = 0.051`` → ``"EDGE_SIGNIFICANT"``,
      ``"未达统计学显著水平，但接近边缘显著…"``
      (NOT ``"存在统计学上的显著差异/关系"``)
    * ``p = 0.100`` → ``"NOT_SIG"``,
      ``"未发现统计学上的显著差异/关系"``
    """
    if p_value is None:
        return ("UNKNOWN", "无法判断显著性（p值缺失）")
    if p_value <= 0.05:
        return ("SIGNIFICANT", "存在统计学上的显著差异/关系")
    if p_value < 0.10:
        return (
            "EDGE_SIGNIFICANT",
            f"未达统计学显著水平，但接近边缘显著（p={p_value:.3f}），建议增加样本量后再次检验",
        )
    return ("NOT_SIG", "未发现统计学上的显著差异/关系")


def _interpret_effect_size(d_value: float | None) -> str:
    """Return Chinese effect-size description from Cohen's *d*.

    Thresholds (conventional):

    ========== ===========================
    *d* range  Description
    ========== ===========================
    ``d<0.2``  但效应量较小 (negligible)
    ``0.2≤d<0.5``  效应量中等 (medium)
    ``0.5≤d<0.8``  效应量较大 (large)
    ``d≥0.8``  效应量很大 (very large)
    ========== ===========================

    Returns empty string when *d* is ``None``.
    """
    if d_value is None:
        return ""
    d = float(d_value)
    if d < 0.2:
        return "但效应量较小"
    if d < 0.5:
        return "效应量中等"
    if d < 0.8:
        return "效应量较大"
    return "效应量很大"


def _build_forbidden_phrases(
    significance: str,
    p_value: float | None,
) -> list[str]:
    """Build the list of phrases the LLM MUST NOT use.

    When *p > 0.05* (both NOT_SIG and EDGE_SIGNIFICANT) the following
    are forbidden because they imply a relationship the data cannot
    support:

    * ``"存在显著差异"`` / ``"存在显著相关"``
    * ``"具有统计学意义"``
    * ``"两者之间存在关系"``
    * ``"相关"`` / ``"有影响"`` / ``"有意义"``

    ``"接近显著"`` is additionally forbidden unless the result is
    genuinely EDGE_SIGNIFICANT.
    """
    forbidden: list[str] = []

    if significance in ("NOT_SIG", "EDGE_SIGNIFICANT"):
        forbidden.extend(
            [
                "存在显著差异",
                "存在显著相关",
                "具有统计学意义",
                "两者之间存在关系",
                "相关",
                "有影响",
                "有意义",
            ]
        )

    # "接近显著" —— only allowed when the result is truly borderline
    if significance != "EDGE_SIGNIFICANT":
        forbidden.append("接近显著")

    return forbidden


def _build_details(stats: dict[str, Any]) -> dict[str, Any]:
    """Extract numerical values from *stats* into a clean details dict.

    Normalises common key-name variants to a canonical set so that
    downstream code (template, LLM prompt) does not need to guess.
    """
    details: dict[str, Any] = {}

    # Direct one-to-one mappings (source_key → target_key)
    key_map: dict[str, str] = {
        "p_value": "p_value",
        "t_value": "t_value",
        "f_value": "f_value",
        "d_value": "d_value",
        "chi2_value": "chi2_value",
        "r_squared": "r_squared",
        "r2": "r_squared",
        "r": "r",  # CORRELATIONS dedicated extractor
        "n_valid": "n_valid",
        "n_missing": "n_missing",
        "mean_diff": "mean_diff",
    }
    for source_key, target_key in key_map.items():
        val = stats.get(source_key)
        if val is not None:
            details[target_key] = val

    # Mean values — try multiple possible key names to maximise
    # compatibility with different parsers.
    mean_a = (
        stats.get("mean_a")
        or stats.get("mean_group1")
        or stats.get("mean_1")
        or stats.get("mean_male")  # T-TEST dedicated extractor
    )
    mean_b = (
        stats.get("mean_b")
        or stats.get("mean_group2")
        or stats.get("mean_2")
        or stats.get("mean_female")  # T-TEST dedicated extractor
    )
    if mean_a is not None:
        details["mean_a"] = mean_a
    if mean_b is not None:
        details["mean_b"] = mean_b

    # Group labels
    label_a = stats.get("label_a") or stats.get("label_group1") or stats.get("group1_label")
    label_b = stats.get("label_b") or stats.get("label_group2") or stats.get("group2_label")
    if label_a is not None:
        details["label_a"] = label_a
    if label_b is not None:
        details["label_b"] = label_b

    return details


def _analysis_type_label(analysis_type: str) -> str:
    """Map SPSS analysis type codes to human-readable Chinese labels.

    Falls back to ``"{code}分析结果"`` for unknown codes.
    """
    mapping: dict[str, str] = {
        "T-TEST": "独立样本t检验",
        "PAIRED T-TEST": "配对样本t检验",
        "PAIRED_SAMPLES_TTEST": "配对样本t检验",
        "ONEWAY": "单因素方差分析",
        "ANOVA": "方差分析",
        "UNIANOVA": "多因素方差分析",
        "REGRESSION": "回归分析",
        "LINEAR REGRESSION": "线性回归分析",
        "FREQUENCIES": "频率分析",
        "DESCRIPTIVES": "描述性统计",
        "CROSSTABS": "交叉表分析",
        "CORRELATION": "相关分析",
        "CORRELATIONS": "相关分析",
        "NONPAR CORR": "非参数相关分析",
        "NPAR TESTS": "非参数检验",
        "MANOVA": "多变量方差分析",
        "GLM": "一般线性模型",
        "LOGISTIC REGRESSION": "逻辑回归分析",
        "FACTOR": "因子分析",
        "RELIABILITY": "信度分析",
        "CLUSTER": "聚类分析",
    }
    return mapping.get(analysis_type.upper(), f"{analysis_type}分析结果")
