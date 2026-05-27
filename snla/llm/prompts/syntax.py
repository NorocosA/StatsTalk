"""
SNLA SPSS Syntax Generation Prompt Builder

Builds the message array for the syntax-generation LLM call.
Transforms a statistical method recommendation + variable context
into a structured prompt with few-shot examples for the LLM client.

Typical usage::

    from snla.llm.prompts.syntax import build_syntax_prompt

    messages = build_syntax_prompt(
        method="independent_t_test",
        variables=[{"name": "gender", "type": "Numeric", "label": "性别",
                     "value_labels": {1: "男", 2: "女"}},
                   {"name": "score", "type": "Numeric", "label": "考试成绩"}],
        dataset_summary={"row_count": 200, "variable_count": 4},
        output_language="zh",
    )
    response = client.chat(messages=messages)
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_variable_entry(var: dict[str, Any]) -> str:
    """Format a single variable dict into a human-readable prompt line.

    Output examples::

        gender (Numeric, 1=男 2=女)
        score (Numeric, 考试成绩)
        class (String)

    Args:
        var: Variable dictionary with at least ``name`` and ``type`` keys.
            May contain ``label`` (str) and ``value_labels`` (dict).

    Returns:
        A single formatted line (without trailing newline).
    """
    name = var.get("name", "?")
    var_type = var.get("type", "?")

    details: list[str] = []

    label = var.get("label", "")
    if label and isinstance(label, str):
        stripped = label.strip()
        if stripped:
            details.append(stripped)

    value_labels = var.get("value_labels")
    if value_labels and isinstance(value_labels, dict) and value_labels:
        parts = (f"{k}={v}" for k, v in value_labels.items())
        details.append(" ".join(parts))

    if details:
        return f"- {name} ({var_type}, {', '.join(details)})"
    return f"- {name} ({var_type})"


def _format_variables(variables: list[dict[str, Any]]) -> str:
    """Format the full variable list for prompt injection.

    Args:
        variables: List of variable dicts.

    Returns:
        Newline-separated variable entries, one per line.
    """
    return "\n".join(_format_variable_entry(var) for var in variables)


def _build_few_shot_examples() -> str:
    """Return 7 few-shot examples covering common SPSS analysis types.

    Each example includes method name, natural-language question,
    variable list, and the expected JSON output.  All SPSS syntax
    in the examples has been verified against IBM SPSS Statistics
    Command Syntax Reference.
    """
    # Each example is a dict for clarity; the method assembles them
    # into the final prompt block.
    examples: list[dict[str, str]] = [
        {
            "method": "independent_t_test",
            "question": "比较男女生在成绩上的差异",
            "variables": "gender (Numeric, 1=男 2=女), score (Numeric)",
            "output": (
                '{"syntax": "T-TEST GROUPS=gender(1 2)\\n'
                '  /VARIABLES=score.", '
                '"required_variables": ["gender", "score"], '
                '"notes": "独立样本t检验"}'
            ),
        },
        {
            "method": "oneway_anova",
            "question": "三个班级的语文成绩差异",
            "variables": "class (String, 班级名), chinese_score (Numeric)",
            "output": (
                '{"syntax": "ONEWAY chinese_score BY class\\n'
                "  /STATISTICS DESCRIPTIVES HOMOGENEITY\\n"
                '  /POSTHOC=LSD ALPHA(0.05).", '
                '"required_variables": ["class", "chinese_score"], '
                '"notes": "单因素方差分析，含方差齐性检验和LSD事后比较"}'
            ),
        },
        {
            "method": "simple_regression",
            "question": "年龄对收入的影响",
            "variables": "age (Numeric), income (Numeric)",
            "output": (
                '{"syntax": "REGRESSION\\n'
                "  /DEPENDENT income\\n"
                "  /METHOD=ENTER age\\n"
                '  /STATISTICS COEFF R ANOVA.", '
                '"required_variables": ["age", "income"], '
                '"notes": "简单线性回归，年龄预测收入"}'
            ),
        },
        {
            "method": "chi_square",
            "question": "性别与是否通过考试的关系",
            "variables": ("gender (Numeric, 1=男 2=女), pass_exam (Numeric, 0=未通过 1=通过)"),
            "output": (
                '{"syntax": "CROSSTABS\\n'
                "  /TABLES=gender BY pass_exam\\n"
                '  /STATISTICS=CHISQ PHI.", '
                '"required_variables": ["gender", "pass_exam"], '
                '"notes": "卡方检验，含Phi系数"}'
            ),
        },
        {
            "method": "frequencies",
            "question": "统计各班级人数",
            "variables": "class (String)",
            "output": (
                '{"syntax": "FREQUENCIES VARIABLES=class\\n'
                "  /BARCHART\\n"
                '  /ORDER=ANALYSIS.", '
                '"required_variables": ["class"], '
                '"notes": "频数分析，含条形图"}'
            ),
        },
        {
            "method": "descriptives",
            "question": "计算成绩的均值和标准差",
            "variables": "score (Numeric)",
            "output": (
                '{"syntax": "DESCRIPTIVES VARIABLES=score\\n'
                '  /STATISTICS=MEAN STDDEV MIN MAX.", '
                '"required_variables": ["score"], '
                '"notes": "描述统计"}'
            ),
        },
        {
            "method": "pearson_correlation",
            "question": "年龄和收入的关系",
            "variables": "age (Numeric), income (Numeric)",
            "output": (
                '{"syntax": "CORRELATIONS\\n'
                "  /VARIABLES=age income\\n"
                '  /STATISTICS DESCRIPTIVES.", '
                '"required_variables": ["age", "income"], '
                '"notes": "Pearson相关分析"}'
            ),
        },
    ]

    blocks: list[str] = []
    for ex in examples:
        block = (
            f"---\n"
            f"方法: {ex['method']}\n"
            f"问题: {ex['question']}\n"
            f"变量: {ex['variables']}\n"
            f"输出: {ex['output']}"
        )
        blocks.append(block)

    return "\n\n".join(blocks)


def _build_system_prompt(output_language: str) -> str:
    """Build the system-level instruction prompt.

    Args:
        output_language: ``"zh"`` for Chinese, ``"en"`` for English notes.

    Returns:
        System prompt string.
    """
    lang_hint = "中文" if output_language == "zh" else "English"

    output_format = (
        "{"
        + '"syntax": "<SPSS语法命令>", '
        + '"required_variables": ["<变量名1>", "<变量名2>"], '
        + '"notes": "<解释说明>"'
        + "}"
    )

    parts: list[str] = [
        "你是 IBM SPSS Statistics 语法专家，精通 SPSS Syntax。",
        "你的任务是根据统计方法和变量信息，生成精确的 SPSS 语法命令。",
        "",
        "【关键规则】",
        "1. 仅生成单一分析语句块，不添加无关命令",
        "2. 变量名必须来自提供的变量清单，不得编造",
        "3. 禁止生成 SAVE, DELETE, ERASE, DATASET CLOSE, NEW FILE, "
        "BEGIN PROGRAM, HOST COMMAND 等危险命令",
        "4. 语法注释使用 `*` 开头（如 `* 这是注释.`）",
        "5. 每个命令以句点 `.` 结束",
        "6. 返回 ONLY 一个 JSON 对象，不要包含额外文本或代码块标记",
        "",
        "【输出格式】",
        output_format,
        "",
        f"【语言要求】notes 字段请使用{lang_hint}生成。",
    ]

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_syntax_prompt(
    method: str,
    variables: list[dict[str, Any]],
    dataset_summary: dict[str, Any],
    output_language: str = "zh",
) -> list[dict[str, str]]:
    """Build the messages array for SPSS syntax generation LLM call.

    The returned message list is ready to be passed to
    ``LLMClient.chat(messages=...)``.

    Args:
        method: Recommended statistical method identifier, e.g.
            ``"independent_t_test"``, ``"oneway_anova"``,
            ``"pearson_correlation"``.
        variables: List of variable dicts. Each dict **must** contain
            ``"name"`` (str) and ``"type"`` (str, e.g. ``"Numeric"`` /
            ``"String"``).  Optionally may contain ``"label"`` (str) and
            ``"value_labels"`` (dict mapping value → label).
        dataset_summary: Dict with at least ``"row_count"`` (int) and
            optionally ``"variable_count"`` (int).
        output_language: ``"zh"`` (default) for Chinese notes in the
            output JSON, ``"en"`` for English.

    Returns:
        A two-element message list::

            [
                {"role": "system", "content": "<instruction prompt>"},
                {"role": "user", "content": "<examples + dataset + task>"},
            ]

    Raises:
        ValueError: If ``method`` is empty or ``variables`` is empty.

    Example::

        >>> prompt = build_syntax_prompt(
        ...     method="independent_t_test",
        ...     variables=[{"name": "gender", "type": "Numeric",
        ...                  "label": "性别",
        ...                  "value_labels": {1: "男", 2: "女"}},
        ...                {"name": "score", "type": "Numeric",
        ...                  "label": "考试成绩"}],
        ...     dataset_summary={"row_count": 200},
        ... )
        >>> len(prompt)
        2
        >>> prompt[0]["role"]
        'system'
        >>> prompt[1]["role"]
        'user'
    """
    # --- Input validation ---
    if not method:
        raise ValueError("`method` must be a non-empty string")
    if not variables:
        raise ValueError("`variables` must be a non-empty list")

    # --- Build system prompt ---
    system_prompt = _build_system_prompt(output_language)

    # --- Build user content ---
    row_count = dataset_summary.get("row_count", "?")
    formatted_vars = _format_variables(variables)

    user_content = (
        "[参考示例]\n"
        f"{_build_few_shot_examples()}\n\n"
        "[数据集信息]\n"
        f"{formatted_vars}\n"
        f"样本量: {row_count}\n\n"
        "[当前任务]\n"
        f"方法: {method}\n"
        "请根据以上统计方法和变量信息，生成 SPSS 语法，并以 JSON 格式返回。"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
