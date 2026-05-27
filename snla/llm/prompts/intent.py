"""
Intent recognition prompt builder for SNLA.

Constructs a complete LLM messages array for classifying user natural
language input into statistical analysis intent categories
(describe, compare_groups, relationship, visualize, follow_up, unknown).

This module is consumed by ``llm.client`` — it produces messages ready
for ``LLMClient.chat()``.
"""

from __future__ import annotations

from typing import Any

# ── Intent category definitions (injected into the system prompt) ──────
_INTENT_DEFINITIONS: dict[str, str] = {
    "describe": "描述统计（求均值、标准差、频数、百分比等）",
    "compare_groups": "组间比较（差异检验、t检验、方差分析等）",
    "relationship": "关系分析（相关分析、回归分析等）",
    "visualize": "图表需求（画图、箱线图、散点图等）",
    "follow_up": '追问/修改上一轮分析（"换成XX呢？""再看看YY"）',
    "unknown": "无法识别意图",
}

# Allowed values for the ``suggested_method`` field in the output JSON.
_VALID_METHODS: tuple[str, ...] = (
    "independent_t_test",
    "paired_t_test",
    "oneway_anova",
    "pearson_correlation",
    "spearman_correlation",
    "simple_regression",
    "multiple_regression",
    "chi_square",
    "frequencies",
    "descriptives",
    "boxplot",
    "scatter",
    "bar_chart",
    "histogram",
)

# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════


def _build_system_prompt() -> str:
    """Build the system message content.

    Returns a Chinese-language instruction that defines the assistant's
    role, enumerates the supported intent categories together with their
    descriptions, and specifies the required JSON output schema.
    """
    lines: list[str] = [
        "你是 SPSS 统计分析专家，擅长理解用户的自然语言分析需求并识别其统计意图。",
        "",
        "意图分类说明：",
    ]
    for key, desc in _INTENT_DEFINITIONS.items():
        lines.append(f"- {key}: {desc}")

    lines += [
        "",
        "输出格式要求：",
        "返回严格的 JSON 对象，包含以下字段：",
        "{",
        '  "intent": "intent_category",',
        '  "confidence": 0.0-1.0,',
        '  "rationale": "理由说明",',
        '  "modified_variable": null_or_string,',
        '  "suggested_method": "method_name_or_null"',
        "}",
        "",
        "各字段说明：",
        (
            "- intent: 上述意图分类之一（"
            "describe / compare_groups / relationship / "
            "visualize / follow_up / unknown）"
        ),
        "- confidence: 0-1 之间的浮点数，表示对该分类的置信度",
        "- rationale: 选择该意图的中文理由",
        (
            "- modified_variable: 仅当 intent 为 follow_up 时，"
            "填写被修改（替换）的变量名；其他情况为 null"
        ),
        "- suggested_method: 建议的统计方法，可选值包括：",
    ]
    lines.append(f"  {', '.join(_VALID_METHODS)}")
    lines.append("  未知或无法确定时为 null")
    lines += [
        "",
        ("请严格按照上述 JSON 格式输出，不要包含任何额外说明或 markdown 代码块标记。"),
    ]
    return "\n".join(lines)


def _build_few_shot_section() -> str:
    """Build the few-shot examples section (≥10 examples).

    Each example shows a user utterance (with an optional previous-analysis
    context for *follow_up* scenarios) and the expected JSON output.
    """
    # Each entry: {"user": str, "output": str, "previous": dict | None}
    examples: list[dict[str, Any]] = [
        # ── 1: describe ─────────────────────────────────────────────
        {
            "user": "计算语文成绩的平均分和标准差",
            "output": (
                '{"intent": "describe", "confidence": 0.95, '
                '"rationale": "要求计算描述统计量", '
                '"modified_variable": null, '
                '"suggested_method": "descriptives"}'
            ),
        },
        # ── 2: compare_groups (two groups) ─────────────────────────
        {
            "user": "比较男女生在成绩上的差异",
            "output": (
                '{"intent": "compare_groups", "confidence": 0.92, '
                '"rationale": "比较两组在一个连续变量上的差异", '
                '"modified_variable": null, '
                '"suggested_method": "independent_t_test"}'
            ),
        },
        # ── 3: relationship (correlation) ──────────────────────────
        {
            "user": "收入和受教育年限之间有关系吗",
            "output": (
                '{"intent": "relationship", "confidence": 0.90, '
                '"rationale": "询问两个连续变量之间的关系", '
                '"modified_variable": null, '
                '"suggested_method": "pearson_correlation"}'
            ),
        },
        # ── 4: follow_up (with previous analysis context) ──────────
        {
            "previous": {
                "method": "Independent T-Test",
                "grouping_var": "gender",
                "test_var": "score",
            },
            "user": "那换成班级差异呢",
            "output": (
                '{"intent": "follow_up", "confidence": 0.88, '
                '"rationale": "用户要求将分组变量从gender替换为class", '
                '"modified_variable": "class", '
                '"suggested_method": "independent_t_test"}'
            ),
        },
        # ── 5: visualize ───────────────────────────────────────────
        {
            "user": "画一个成绩的直方图",
            "output": (
                '{"intent": "visualize", "confidence": 0.93, '
                '"rationale": "要求绘制图形", '
                '"modified_variable": null, '
                '"suggested_method": "histogram"}'
            ),
        },
        # ── 6: compare_groups (multi-group) ────────────────────────
        {
            "user": "三个班级的语文成绩有没有差异",
            "output": (
                '{"intent": "compare_groups", "confidence": 0.91, '
                '"rationale": "比较多组均值差异", '
                '"modified_variable": null, '
                '"suggested_method": "oneway_anova"}'
            ),
        },
        # ── 7: relationship (multiple regression) ──────────────────
        {
            "user": "研究年龄和受教育年限对收入的影响",
            "output": (
                '{"intent": "relationship", "confidence": 0.89, '
                '"rationale": "多个自变量对一个因变量的影响", '
                '"modified_variable": null, '
                '"suggested_method": "multiple_regression"}'
            ),
        },
        # ── 8: describe (frequencies) ──────────────────────────────
        {
            "user": "统计一下各班级的人数",
            "output": (
                '{"intent": "describe", "confidence": 0.94, '
                '"rationale": "计算分类变量的频数分布", '
                '"modified_variable": null, '
                '"suggested_method": "frequencies"}'
            ),
        },
        # ── 9: compare_groups (chi-square) ─────────────────────────
        {
            "user": "性别和是否通过考试之间有关系吗",
            "output": (
                '{"intent": "compare_groups", "confidence": 0.87, '
                '"rationale": "检验两个分类变量的关联性", '
                '"modified_variable": null, '
                '"suggested_method": "chi_square"}'
            ),
        },
        # ── 10: colloquial Chinese ──────────────────────────────────
        {
            "user": "帮我瞅瞅年纪大的是不是工资也高",
            "output": (
                '{"intent": "relationship", "confidence": 0.85, '
                '"rationale": "中文口语化表达，询问年龄与工资的关系", '
                '"modified_variable": null, '
                '"suggested_method": "pearson_correlation"}'
            ),
        },
    ]

    lines: list[str] = [
        "以下是一些示例对话，请参考这些示例的输出格式和推理方式：",
        "",
    ]
    for i, ex in enumerate(examples, start=1):
        lines.append(f"示例 {i}:")
        if "previous" in ex and ex["previous"] is not None:
            prev = ex["previous"]
            lines.append(
                f"上一轮分析: 方法: {prev['method']}, "
                f"分组变量: {prev['grouping_var']}, "
                f"检验变量: {prev['test_var']}"
            )
        lines.append(f"用户: {ex['user']}")
        lines.append(f"输出: {ex['output']}")
        lines.append("")

    return "\n".join(lines)


def _format_variables(variables: list[dict[str, Any]]) -> str:
    """Format the variable list into a human-readable catalog.

    Each variable line follows the pattern::

        - name (type, label)

    The optional ``value_labels`` field (if present in the dict) is
    appended to the label as ``key=value`` pairs.
    """
    lines: list[str] = ["可用变量:"]
    for v in variables:
        name = v.get("name", "?")
        vtype = v.get("type", "?")
        parts: list[str] = [name, f"({vtype}"]

        label = v.get("label", "")
        value_labels = v.get("value_labels")

        label_str = label
        if value_labels:
            pairs = " ".join(f"{k}={v}" for k, v in sorted(value_labels.items()))
            if label_str:
                label_str = f"{label_str}, {pairs}"
            else:
                label_str = pairs

        if label_str:
            # Append inside the parentheses after type
            parts[-1] = f"({vtype}, {label_str})"
        else:
            parts[-1] = f"({vtype})"

        lines.append(f"- {' '.join(parts)}")

    return "\n".join(lines)


def _format_last_analysis(last: dict[str, Any]) -> str:
    """Format the previous analysis context block.

    Produces::

        方法: Independent T-Test
        分组变量: gender
        检验变量: score
    """
    method = last.get("method", "?")
    grouping_var = last.get("grouping_var", "?")
    test_var = last.get("test_var", "?")
    return f"方法: {method}\n分组变量: {grouping_var}\n检验变量: {test_var}"


def _get_sample_size(variables: list[dict[str, Any]]) -> int | None:
    """Extract the sample size from the variable list if available.

    The sample size can be stored as a top-level key ``row_count`` in
    a wrapped dict.  Since the function receives a flat list, we check
    the first entry (if it acts as a metadata carrier) for an ``N`` or
    ``row_count`` key.
    """
    if not variables:
        return None
    first = variables[0]
    for key in ("N", "row_count", "sample_size"):
        val = first.get(key)
        if val is not None and isinstance(val, (int, float)):
            return int(val)
    return None


def _build_user_content(
    user_message: str,
    variables: list[dict[str, Any]] | None,
    last_analysis: dict[str, Any] | None,
) -> str:
    """Assemble the complete user message content.

    Order of sections:
    1. Few-shot examples (always included)
    2. Dataset context (only when *variables* is provided)
    3. Previous analysis context (only when *last_analysis* is provided)
    4. Current user request wrapped in ``[USER REQUEST]`` tags
    """
    parts: list[str] = [_build_few_shot_section()]

    if variables:
        parts.append("[DATASET CONTEXT]")
        parts.append(_format_variables(variables))
        n = _get_sample_size(variables)
        if n is not None:
            parts.append(f"样本量: {n}")
        parts.append("")

    if last_analysis:
        parts.append("[上一次分析]")
        parts.append(_format_last_analysis(last_analysis))
        parts.append("")

    parts.append("[USER REQUEST]")
    parts.append(user_message)

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════


def build_intent_prompt(
    user_message: str,
    variables: list[dict] | None = None,
    last_analysis: dict | None = None,
    history: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Build the messages array for an intent-recognition LLM call.

    Constructs a complete conversation including system instructions,
    optional conversation history, few-shot examples, dataset context,
    previous-analysis context, and the user's current request.

    Args:
        user_message:
            The user's natural language input (Chinese).  This is the
            utterance to classify into an intent category.
        variables:
            Optional list of variable metadata dicts.  Each dict should
            contain at least ``"name"``, ``"type"``, and optionally
            ``"label"`` and ``"value_labels"``.  When provided, a
            ``[DATASET CONTEXT]`` block is injected into the prompt.
        last_analysis:
            Optional dict describing the previous analysis round.
            Expected keys: ``"method"``, ``"grouping_var"``,
            ``"test_var"``.  When provided, a ``[上一次分析]`` block
            is injected so the LLM can resolve *follow_up* intents.
        history:
            Optional list of previous conversation turns.  Each entry
            must be a dict with ``"role"`` (``"user"`` or ``"assistant"``)
            and ``"content"`` keys.  These are inserted between the
            system message and the current user message.

    Returns:
        A list of message dicts ready to pass to
        ``LLMClient.chat()``::

            [
                {"role": "system", "content": "<system instruction>"},
                *history,  # if provided
                {"role": "user", "content": "<assembled user content>"},
            ]

    Example::

        >>> messages = build_intent_prompt(
        ...     "比较男女生成绩差异",
        ...     variables=[{"name": "gender", "type": "Numeric",
        ...                 "label": "性别"}],
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
    ]

    # Inject conversation history (if any) between system and current user msg
    if history:
        for h in history:
            role = h.get("role", "user")
            content = h.get("content", "")
            messages.append({"role": role, "content": content})

    # Current user request with all context
    messages.append(
        {
            "role": "user",
            "content": _build_user_content(
                user_message=user_message,
                variables=variables,
                last_analysis=last_analysis,
            ),
        }
    )

    return messages
