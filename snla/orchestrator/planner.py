"""
Planner implementation — LLM intent recognition, method recommendation,
variable matching, and MOCK-mode fallback.

Extracted from server.py's internal helpers (_phase1_plan, _auto_detect_vars,
_cloud_vars, _mock_intent, _mock_method).  Parameterised to remove the direct
``session`` global dependency so both Flask and MCP servers can call these
functions with their own data.

Imports are local to avoid pulling in LLM / data modules at package init time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from snla.data.sanitizer import filter_for_cloud

from . import PlanResult

if TYPE_CHECKING:
    from . import Planner


# ═════════════════════════════════════════════════════════════════════════
# Public entry point
# ═════════════════════════════════════════════════════════════════════════


def _plan(
    planner: Planner,
    session_id: str,
    user_input: str,
    variables: list[dict],
    dataset_meta: dict | None = None,
    last_analysis: dict | None = None,
) -> PlanResult:
    """Determine statistical method, plan explanation, and variable mapping.

    Returns a PlanResult.  The ``session_id`` parameter is accepted but
    currently unused by the planning logic (it is consumed by the greylist
    state machine on the Planner itself).
    """
    from snla.config import LLM_MOCK

    if LLM_MOCK or not _has_llm():
        method = _mock_intent(user_input, last_analysis)
        valid = {
            "independent_t_test",
            "paired_t_test",
            "oneway_anova",
            "simple_regression",
            "pearson_correlation",
            "spearman_correlation",
            "chi_square",
            "crosstabs",
            "frequencies",
            "descriptives",
            "mann_whitney_u",
            "kruskal_wallis",
        }
        if method not in valid:
            method = "descriptives"
        cat, num = _auto_detect_vars(variables)
        return PlanResult(
            method=method,
            plan_explanation=f"（MOCK 模式）{method}",
            grouping_variable=cat,
            test_variable=num,
        )

    # ── Real LLM path ─────────────────────────────────────────────────
    from snla.llm.client import LLMClient

    cloud_vars = _cloud_vars(variables)
    ds = dataset_meta or {}
    row_count = ds.get("row_count", 0)

    # Build variable catalog for semantic matching
    var_lines = []
    for v in cloud_vars[:30]:
        lbl = v.get("label", "")
        vl = v.get("value_labels", {})
        vl_str = ""
        if vl:
            items = list(vl.items())[:5]
            vl_str = " [" + " ".join(f"{k}={v}" for k, v in items) + "]"
        var_lines.append(
            f"  - {v['name']} ({v.get('type', '?')}){': ' + lbl if lbl else ''}{vl_str}"
        )
    var_catalog = "\n".join(var_lines)

    prompt = [
        {
            "role": "system",
            "content": (
                "你是 SPSS 统计分析专家。根据用户的自然语言问题，选择最合适的"
                "统计方法，并确定对应的变量。\n\n"
                "可用方法: independent_t_test, paired_t_test, oneway_anova, "
                "mann_whitney_u, kruskal_wallis, pearson_correlation, "
                "spearman_correlation, simple_regression, chi_square, "
                "frequencies, descriptives\n\n"
                "规则:\n"
                "- 分组变量(grouping_variable): 必须是分类变量（有值标签的Numeric或String）\n"
                "- 检验变量(test_variable): 必须是连续变量（无值标签的Numeric）\n"
                "- 2组→t检验, 3组+→ANOVA\n"
                "- 非参数检验用于数据不满足正态假设\n"
                "- 仔细匹配变量名和标签的语义含义\n\n"
                '返回 JSON: {"method":"...", "plan_explanation":"...", '
                '"grouping_variable":"变量名或null", "test_variable":"变量名或null"}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"数据集: {row_count} 条记录, {len(cloud_vars)} 个变量\n"
                f"{var_catalog}\n\n"
                f"用户问题: {user_input}\n\n"
                f"请分析用户问题中的关键词，匹配到正确的变量，返回 JSON。"
            ),
        },
    ]

    client = LLMClient()
    try:
        result = client.chat(prompt)
        parsed = json.loads(result.get("content", "{}"))
        method = parsed.get("method", "descriptives")
        plan = parsed.get("plan_explanation", "")
        gvar = parsed.get("grouping_variable")
        tvar = parsed.get("test_variable")
        valid_methods = {
            "independent_t_test",
            "paired_t_test",
            "oneway_anova",
            "mann_whitney_u",
            "kruskal_wallis",
            "pearson_correlation",
            "spearman_correlation",
            "simple_regression",
            "chi_square",
            "frequencies",
            "descriptives",
            "crosstabs",
        }
        if method not in valid_methods:
            method = "descriptives"
        return PlanResult(
            method=method,
            plan_explanation=plan,
            grouping_variable=gvar,
            test_variable=tvar,
        )
    except Exception:
        return PlanResult(
            method="descriptives",
            plan_explanation="",
            grouping_variable=None,
            test_variable=None,
        )


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════


def _cloud_vars(variables: list[dict]) -> list[dict]:
    """Return cloud-safe variable metadata (strips raw values)."""
    return filter_for_cloud({"variables": variables}).get("variables", [])


def _auto_detect_vars(variables: list[dict]) -> tuple[str | None, str | None]:
    """Auto-detect categorical and numeric variables from metadata.

    Returns (cat_var, num_var) — picks first categorical and first numeric.
    """
    cat_var = num_var = None
    for v in variables:
        if v.get("value_labels") and not cat_var:
            cat_var = v["name"]
        elif v.get("type") == "Numeric" and not num_var:
            num_var = v["name"]
        if cat_var and num_var:
            break
    return cat_var, num_var


def _has_llm() -> bool:
    """Return True if an LLM API key is configured."""
    from snla.config import LLM_API_KEY

    return bool(LLM_API_KEY)


def _mock_intent(
    user_input: str,
    last_analysis: dict | None = None,
) -> str:
    """Keyword-based intent classification (no LLM required).

    Priority order (first match wins) — mirrors the LLM intent categories:
    describe, compare_groups, relationship, visualize, follow_up.
    """
    text = user_input.lower()

    # ── 0. Follow-up ──────────────────────────────────────────────
    follow_up_words = (
        "换成",
        "再看看",
        "那",
        "如果不是",
        "改成",
        "换一个",
        "改为",
        "不是这个",
        "不对",
        "重新",
    )
    if last_analysis and any(w in text for w in follow_up_words):
        return "follow_up"

    # ── 1. Visualize ──────────────────────────────────────────────
    if any(
        w in text
        for w in (
            "画",
            "图",
            "plot",
            "chart",
            "graph",
            "箱线",
            "直方",
            "散点",
            "条形",
            "饼图",
            "可视化",
            "折线",
            "绘制",
            "作图",
        )
    ):
        return "visualize"

    # ── 2. Crosstabs / Chi-square ─────────────────────────────────
    if any(
        w in text
        for w in (
            "卡方",
            "交叉表",
            "列联表",
            "独立性检验",
            "是否有关",
            "是否有关系",
            "是否相关",
            "是否独立",
            "比例",
            "构成比",
            "百分比分布",
        )
    ):
        return "crosstabs"

    # ── 3. Frequency / count ──────────────────────────────────────
    if any(
        w in text
        for w in (
            "多少人",
            "几个人",
            "多少个",
            "计数",
            "人数",
            "频数",
            "个案数",
            "几个",
            "统计一下",
            "有多少",
            "占比",
            "分别有多少",
        )
    ):
        return "frequencies"

    # ── 4. Paired comparison ──────────────────────────────────────
    if any(
        w in text
        for w in (
            "前后",
            "配对",
            "培训前",
            "培训后",
            "干预前",
            "干预后",
            "治疗前",
            "治疗后",
            "之前之后",
            "before",
            "after",
            "变化",
            "改变",
            "前后测",
            "有变化吗",
            "有提升吗",
            "有改善吗",
            "第一次",
            "第二次",
            "自身对照",
            "成对",
        )
    ):
        return "paired_t_test"

    # ── 5. Non-parametric — Mann-Whitney ──────────────────────────
    if any(
        w in text
        for w in (
            "非参数.*两组",
            "mann.*whitney",
            "曼惠特尼",
            "秩和.*两组",
            "不服从正态.*比较",
            "非正态.*比较",
            "偏态.*比较",
            "不符合正态.*差异",
            "方差不齐.*比较",
            "等级数据.*比较",
        )
    ):
        return "mann_whitney_u"

    # ── 6. Non-parametric — Kruskal-Wallis ────────────────────────
    if any(
        w in text
        for w in (
            "非参数.*多组",
            "kruskal.*wallis",
            "克鲁斯卡尔",
            "秩和.*多组",
            "不服从正态.*多组",
            "非正态.*多组",
            "不符合正态.*不同",
            "偏态.*多组",
            "不满足.*anova",
            "不满足.*方差",
        )
    ):
        return "kruskal_wallis"

    # ── 7. Group comparison (t-test / ANOVA) ──────────────────────
    if any(
        w in text
        for w in (
            "比较",
            "差异",
            "差别",
            "显著",
            "compare",
            "diff",
            "男生",
            "女生",
            "男女",
            "不同",
            "区别",
            "之间",
            "是否显著",
            "有无差异",
            "有没有差别",
            "有无差别",
            "是不是不一样",
            "有没有不同",
            "哪个更高",
            "哪个更好",
            "谁比谁",
            "t检验",
            "t测试",
            "实验组",
            "对照组",
            "A组",
            "B组",
            "处理组",
            "两组",
        )
    ):
        multi_hints = (
            "各",
            "不同班",
            "多个",
            "三种",
            "三级",
            "四组",
            "几组",
            "各组",
            "几个班",
            "几个组",
            "不同组",
            "年级",
            "班级",
            "专业",
            "部门",
            "地区",
            "学历",
            "不同级别",
            "不同类型",
            "各类",
            "各种",
            "方差分析",
            "ANOVA",
            "F检验",
            "多组比较",
        )
        if any(w in text for w in multi_hints):
            return "oneway_anova"
        return "independent_t_test"

    # ── 8. Relationship (correlation / regression) ────────────────
    if any(
        w in text
        for w in (
            "关系",
            "相关",
            "影响",
            "因素",
            "预测",
            "correlation",
            "regression",
            "自变量",
            "因变量",
            "能否预测",
            "是否影响",
            "会不会影响",
            "决定因素",
            "解释",
            "关联",
            "联系",
            "正相关",
            "负相关",
            "成正比",
            "随着",
            "越来越",
        )
    ):
        reg_hints = (
            "预测",
            "regression",
            "回归",
            "影响.*因素",
            "自变量",
            "因变量",
            "能否预测",
            "解释.*变异",
            "决定因素",
            "哪个影响大",
            "解释力",
            "R平方",
            "多元",
            "多个.*影响",
        )
        if any(w in text for w in reg_hints):
            return "simple_regression"
        spearman_hints = (
            "spearman",
            "斯皮尔曼",
            "等级相关",
            "秩相关",
            "不服从正态.*相关",
            "非参数.*相关",
            "等级",
            "排名",
            "次序",
            "Likert",
            "满意度.*级",
        )
        if any(w in text for w in spearman_hints):
            return "spearman_correlation"
        return "pearson_correlation"

    # ── 9. Descriptive (catch-all) ────────────────────────────────
    if any(
        w in text
        for w in (
            "描述",
            "统计",
            "平均",
            "均值",
            "标准差",
            "中位数",
            "describe",
            "mean",
            "frequenc",
            "分布",
            "概要",
            "基本情况",
            "基本特征",
            "总体情况",
            "最大值",
            "最小值",
            "缺失值",
            "极差",
            "汇总",
            "偏度",
            "峰度",
        )
    ):
        return "descriptives"

    return "descriptives"  # safe default


def _mock_method(intent: str) -> str:
    """Map intent keyword to recommended statistical method.

    Handles both abstract intent categories (compare_groups, relationship)
    and specific method names returned by the enhanced MOCK classifier.
    """
    direct_methods = {
        "independent_t_test",
        "paired_t_test",
        "oneway_anova",
        "simple_regression",
        "chi_square",
        "frequencies",
        "descriptives",
        "correlations",
        "pearson_correlation",
        "spearman_correlation",
        "mann_whitney_u",
        "kruskal_wallis",
        "crosstabs",
    }
    if intent in direct_methods:
        return intent

    method_map = {
        "compare_groups": "independent_t_test",
        "relationship": "pearson_correlation",
        "describe": "descriptives",
        "visualize": "frequencies",
        "follow_up": "independent_t_test",
    }
    return method_map.get(intent, "descriptives")
