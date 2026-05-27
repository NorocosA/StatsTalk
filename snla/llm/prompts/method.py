"""
Statistical method recommendation prompt builder for SNLA.

Constructs a complete LLM messages array for recommending the
appropriate statistical test/method based on user intent, available
variables, and conversation context.

This module is consumed by ``llm.client`` — it produces messages ready
for ``LLMClient.chat()``.
"""

from __future__ import annotations

from typing import Any

# ── Available methods catalog (injected into the system prompt) ────────
_AVAILABLE_METHODS: dict[str, str] = {
    "independent_t_test": "独立样本t检验（两组比较，连续因变量）",
    "paired_t_test": "配对样本t检验（同组前后测）",
    "oneway_anova": "单因素方差分析（三组及以上比较）",
    "mann_whitney_u": "Mann-Whitney U检验（非参数两组比较）",
    "kruskal_wallis": "Kruskal-Wallis检验（非参数多组比较）",
    "pearson_correlation": "Pearson相关（两连续变量）",
    "spearman_correlation": "Spearman秩相关（非参数相关）",
    "simple_regression": "简单线性回归",
    "multiple_regression": "多元线性回归",
    "chi_square": "卡方检验（两分类变量关联）",
    "frequencies": "频数分析",
    "descriptives": "描述统计",
}

# Variable type rules enforced during recommendation.
_VARIABLE_TYPE_RULES: str = (
    "- t检验/方差分析: 分组变量必须是分类变量，检验变量必须是连续变量\n"
    "- 相关/回归: 两个变量都必须是连续变量\n"
    "- 卡方: 两个变量都必须是分类变量\n"
    "- 分组数 = 2 → t检验，分组数 ≥ 3 → ANOVA"
)

# Valid assumption-check names.
_VALID_ASSUMPTIONS: tuple[str, ...] = (
    "normality",
    "homogeneity_of_variance",
    "linearity",
    "independence",
    "sample_size",
)

# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════


def _build_system_prompt() -> str:
    """Build the system message content.

    Defines the assistant's role as a Chinese-speaking SPSS statistical
    method expert, enumerates all available methods with descriptions,
    states the variable-type rules, and specifies the required JSON
    output schema.
    """
    lines: list[str] = [
        "你是 SPSS 统计方法专家。你的任务是根据用户的分析意图和可用变量，"
        "推荐最合适的统计检验方法。你必须考虑变量的类型（连续/分类）、"
        "分组数量、以及统计假设。",
        "",
        "可推荐的统计方法：",
    ]

    for method, desc in _AVAILABLE_METHODS.items():
        lines.append(f"- {method}: {desc}")

    lines += [
        "",
        "变量类型规则：",
        _VARIABLE_TYPE_RULES,
        "",
        "输出格式要求：",
        "返回严格的 JSON 对象，包含以下字段：",
        "{",
        '  "recommended_method": "方法名称",',
        '  "alternatives": ["备用方法1", "备用方法2"],',
        '  "assumptions_check": ["假设1", "假设2"],',
        '  "grouping_variable": "分组变量名或null",',
        '  "test_variable": "检验变量名或null",',
        '  "rationale": "推荐理由（中文）",',
        '  "confidence": 0.0-1.0',
        "}",
        "",
        "字段说明：",
        ("- recommended_method: 推荐的方法，可选值包括 " + ", ".join(_AVAILABLE_METHODS.keys())),
        ("- alternatives: 备选方法列表（可能为空数组），当首选方法假设不满足时可供选择的替代方法"),
        (
            "- assumptions_check: 需要检验的统计假设列表，"
            f"可选值包括 {', '.join(_VALID_ASSUMPTIONS)}"
        ),
        ("- grouping_variable: 分组变量名，不适用时（如相关分析）为 null"),
        ("- test_variable: 检验变量名，不适用时（如频数分析）为 null"),
        "- rationale: 选择该方法的详细中文理由",
        "- confidence: 0-1 置信度，反映推荐的确定程度",
        "",
        ("请严格按照上述 JSON 格式输出，不要包含任何额外说明或 markdown 代码块标记。"),
    ]

    return "\n".join(lines)


def _format_variables(variables: list[dict[str, Any]]) -> str:
    """Format the variable list into a human-readable catalog.

    Each variable line follows the pattern::

        - name (Type, label, 值标签: key=value ...)

    When ``value_labels`` are present, they are displayed as space-
    separated ``key=value`` pairs prefixed with ``值标签:``.  This is
    critical for distinguishing categorical Numeric variables (those
    with value labels) from continuous ones.
    """
    lines: list[str] = ["可用变量:"]
    for v in variables:
        name = v.get("name", "?")
        vtype = v.get("type", "?")
        label = v.get("label", "")
        value_labels = v.get("value_labels")

        parts: list[str] = [f"- {name} ({vtype}"]

        # Build descriptive suffix: label + value_labels
        suffix_parts: list[str] = []
        if label:
            suffix_parts.append(label)
        if value_labels:
            pairs = " ".join(f"{k}={v}" for k, v in sorted(value_labels.items()))
            suffix_parts.append(f"值标签: {pairs}")

        suffix = ", ".join(suffix_parts)
        if suffix:
            parts[-1] = f"{parts[-1]}, {suffix}"

        parts[-1] = f"{parts[-1]})"
        lines.append("".join(parts))

    return "\n".join(lines)


def _get_sample_size(variables: list[dict[str, Any]]) -> int | None:
    """Extract the sample size from the variable list if available.

    Checks the first entry for metadata keys ``N``, ``row_count``, or
    ``sample_size``.
    """
    if not variables:
        return None
    first = variables[0]
    for key in ("N", "row_count", "sample_size"):
        val = first.get(key)
        if val is not None and isinstance(val, (int, float)):
            return int(val)
    return None


def _build_dataset_context(variables: list[dict[str, Any]]) -> str:
    """Build the ``[数据集信息]`` section.

    Includes the formatted variable listing and, when available, the
    sample size.
    """
    lines: list[str] = ["[数据集信息]"]
    lines.append(_format_variables(variables))
    n = _get_sample_size(variables)
    if n is not None:
        lines.append(f"样本量: {n}")
    return "\n".join(lines)


def _build_few_shot_section() -> str:
    """Build the few-shot examples section (≥5 examples).

    Each example shows an intent, the available variables, a problem
    description, and the expected JSON output.  The examples help the
    LLM learn the expected reasoning pattern and output format.
    """
    examples: list[dict[str, str]] = [
        # ── 1: compare_groups → independent_t_test ─────────────────
        {
            "intent": "compare_groups",
            "variables": "gender (Numeric, 1=男 2=女), score (Numeric)",
            "question": "比较男女生成绩差异",
            "output": (
                '{"recommended_method": "independent_t_test", '
                '"alternatives": ["mann_whitney_u"], '
                '"assumptions_check": ["normality", "homogeneity_of_variance"], '
                '"grouping_variable": "gender", '
                '"test_variable": "score", '
                '"rationale": "两组独立样本比较连续变量差异", '
                '"confidence": 0.95}'
            ),
        },
        # ── 2: compare_groups (3+) → oneway_anova ─────────────────
        {
            "intent": "compare_groups",
            "variables": ("class (String, 班级A/班级B/班级C), score (Numeric)"),
            "question": "三个班级成绩差异",
            "output": (
                '{"recommended_method": "oneway_anova", '
                '"alternatives": ["kruskal_wallis"], '
                '"assumptions_check": ["normality", "homogeneity_of_variance"], '
                '"grouping_variable": "class", '
                '"test_variable": "score", '
                '"rationale": "三组独立样本比较连续变量差异", '
                '"confidence": 0.93}'
            ),
        },
        # ── 3: relationship → pearson_correlation ─────────────────
        {
            "intent": "relationship",
            "variables": "age (Numeric), income (Numeric)",
            "question": "年龄和收入的关系",
            "output": (
                '{"recommended_method": "pearson_correlation", '
                '"alternatives": ["spearman_correlation", "simple_regression"], '
                '"assumptions_check": ["linearity", "normality"], '
                '"grouping_variable": null, '
                '"test_variable": null, '
                '"rationale": "两个连续变量之间的关系分析", '
                '"confidence": 0.90}'
            ),
        },
        # ── 4: relationship (categorical) → chi_square ────────────
        {
            "intent": "relationship",
            "variables": ("gender (Numeric, 1=男 2=女), pass_exam (Numeric, 0=否 1=是)"),
            "question": "性别和通过率有关系吗",
            "output": (
                '{"recommended_method": "chi_square", '
                '"alternatives": [], '
                '"assumptions_check": ["sample_size"], '
                '"grouping_variable": "gender", '
                '"test_variable": "pass_exam", '
                '"rationale": "两个分类变量的关联性检验", '
                '"confidence": 0.91}'
            ),
        },
        # ── 5: describe → descriptives ────────────────────────────
        {
            "intent": "describe",
            "variables": "class (String), score (Numeric)",
            "question": "各班级成绩均值和标准差",
            "output": (
                '{"recommended_method": "descriptives", '
                '"alternatives": ["frequencies"], '
                '"assumptions_check": [], '
                '"grouping_variable": null, '
                '"test_variable": "score", '
                '"rationale": "描述统计量计算", '
                '"confidence": 0.95}'
            ),
        },
    ]

    lines: list[str] = [
        "以下是一些示例，请参考这些示例的推理方式和输出格式：",
        "",
    ]
    for i, ex in enumerate(examples, start=1):
        lines.append(f"示例 {i}:")
        lines.append(f"意图: {ex['intent']}")
        lines.append(f"变量: {ex['variables']}")
        lines.append(f"问题: {ex['question']}")
        lines.append(f"输出: {ex['output']}")
        lines.append("")

    return "\n".join(lines)


def _build_user_content(
    intent: str,
    variables: list[dict[str, Any]],
    conversation_context: str | None,
) -> str:
    """Assemble the complete user message content.

    Order of sections:
    1. Few-shot examples (always included)
    2. Dataset context (always included — variables is required)
    3. Conversation context (only when *conversation_context* is provided)
    4. Current analysis request with intent
    """
    parts: list[str] = [_build_few_shot_section()]

    # Dataset context with variable catalog
    parts.append(_build_dataset_context(variables))
    parts.append("")

    # Optional conversation context (user's original question / additional info)
    if conversation_context:
        parts.append("[会话上下文]")
        parts.append(conversation_context)
        parts.append("")

    # Current analysis request
    parts.append("[当前分析请求]")
    parts.append(f"意图: {intent}")

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════


def build_method_prompt(
    intent: str,
    variables: list[dict],
    conversation_context: str | None = None,
) -> list[dict[str, str]]:
    """Build the messages array for statistical method recommendation LLM call.

    Constructs a complete conversation including system instructions,
    few-shot examples, dataset context with variable catalog, optional
    conversation history context, and the current analysis intent.

    The returned messages list is ready to pass to ``LLMClient.chat()``::

        messages = build_method_prompt(
            intent="compare_groups",
            variables=[{"name": "gender", "type": "Numeric", ...}],
            conversation_context="User wants to compare male and female scores",
        )
        response = client.chat(messages=messages)

    Args:
        intent:
            The analysis intent from intent recognition.  Expected values
            include ``"compare_groups"``, ``"relationship"``,
            ``"describe"``, ``"visualize"``, etc.
        variables:
            List of variable metadata dicts.  Each dict should contain
            at least ``"name"``, ``"type"``, and optionally ``"label"``
            and ``"value_labels"``.  The ``value_labels`` field is
            critical for distinguishing categorical Numeric variables
            from continuous ones.

            An optional ``"row_count"``, ``"N"``, or ``"sample_size"``
            key in the first dict is treated as the dataset sample size.
        conversation_context:
            Optional additional context from conversation history, such
            as the user's original natural-language question or relevant
            background.  When provided, it is injected as a
            ``[会话上下文]`` block in the user message.

    Returns:
        A list of message dicts ready to pass to ``LLMClient.chat()``::

            [
                {"role": "system", "content": "<system instruction>"},
                {"role": "user", "content": "<assembled user content>"},
            ]

    Example::

        >>> messages = build_method_prompt(
        ...     intent="compare_groups",
        ...     variables=[
        ...         {"name": "gender", "type": "Numeric", "label": "性别",
        ...          "value_labels": {1: "男", 2: "女"}},
        ...         {"name": "score", "type": "Numeric", "label": "考试成绩"},
        ...     ],
        ...     conversation_context="用户想比较男女生的成绩差异",
        ... )
        >>> len(messages)
        2
        >>> messages[0]["role"]
        'system'
        >>> messages[1]["role"]
        'user'
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _build_system_prompt()},
        {
            "role": "user",
            "content": _build_user_content(
                intent=intent,
                variables=variables,  # type: ignore[arg-type]
                conversation_context=conversation_context,
            ),
        },
    ]

    return messages
