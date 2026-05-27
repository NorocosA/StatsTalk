#!/usr/bin/env python
"""
SNLA End-to-End Demo — Minimal complete pipeline.

NL Input → Intent → Method → Syntax → Validate → SPSS Execute → Parse → Explain

Usage:
    python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav

    # Interactive mode:
    python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav --interactive

    # Single query:
    python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav --query "比较男女成绩差异"

Supports LLM_MOCK mode (no API key needed): uses keyword-based intent
classification and template syntax as fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows consoles (avoids GBK codec errors)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from snla.config import DEBUG, LLM_MOCK, SPSS_EXEC_MODE


def _safe_json_parse(text: str) -> dict:
    """Parse LLM output that may contain markdown fences or malformed JSON."""
    import json as _json

    text = text.strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Find the outermost JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return _json.loads(text[start : end + 1])
        except _json.JSONDecodeError:
            pass

    # Try to repair truncated JSON (LLM output cut off mid-response)
    if start >= 0 and (end < 0 or end <= start):
        fragment = text[start:]
        # Count open braces/brackets and close them
        open_braces = fragment.count("{") - fragment.count("}")
        open_brackets = fragment.count("[") - fragment.count("]")
        # Count if we're inside a string (odd number of unescaped quotes)
        in_string = fragment.count('"') % 2 == 1
        if in_string:
            fragment += '"'
        fragment += "}" * max(open_braces, 0)
        fragment += "]" * max(open_brackets, 0)
        try:
            return _json.loads(fragment)
        except _json.JSONDecodeError:
            pass

    raise ValueError(f"Failed to parse JSON from LLM output: {text[:200]}")


# ═══════════════════════════════════════════════════════════════════════════
# Step 1: Data Loading & Metadata Extraction
# ═══════════════════════════════════════════════════════════════════════════


def load_data(data_path: str) -> dict:
    """Load dataset and extract variable metadata."""
    from snla.data.reader import read_and_extract

    print(f"[1/8] 加载数据: {data_path}")
    t0 = time.perf_counter()
    metadata = read_and_extract(data_path)
    elapsed = time.perf_counter() - t0

    print(f"      样本量: {metadata['row_count']}, 变量数: {metadata['column_count']}")
    for v in metadata["variables"]:
        labels = v.get("value_labels") or {}
        label_str = (
            f" [{', '.join(f'{k}={v}' for k, v in list(labels.items())[:4])}]" if labels else ""
        )
        print(f"      • {v['name']:12s} {v['type']:8s} {v.get('label', '')}{label_str}")
    print(f"      ⏱ {elapsed:.2f}s\n")
    return metadata


# ═══════════════════════════════════════════════════════════════════════════
# Step 2: Privacy Sanitization
# ═══════════════════════════════════════════════════════════════════════════


def sanitize(metadata: dict) -> tuple[list[dict], dict[str, str]]:
    """Sanitize variable names for cloud LLM safety."""
    from snla.data.sanitizer import sanitize_variables

    print("[2/8] 隐私脱敏...")
    variables = metadata["variables"]
    sanitized, count = sanitize_variables(variables)

    name_map: dict[str, str] = {}
    if count > 0:
        print(f"      ⚠️ 检测到 {count} 个敏感变量，已自动脱敏")
        for v in sanitized:
            if v.get("desensitized"):
                name_map[v["name"]] = v.get("original_name", v["name"])
                print(f"      {v['original_name']} → {v['name']}")
    else:
        print(f"      ✅ 未检测到敏感变量")

    return sanitized, name_map


# ═══════════════════════════════════════════════════════════════════════════
# Step 3: Intent Recognition
# ═══════════════════════════════════════════════════════════════════════════


def recognize_intent(user_input: str, variables: list[dict]) -> dict:
    """Recognize user's analysis intent."""
    print(f'[3/8] 意图识别: "{user_input}"')

    if LLM_MOCK:
        result = _mock_intent(user_input)
    else:
        from snla.llm.client import LLMClient
        from snla.llm.prompts.intent import build_intent_prompt

        client = LLMClient()
        messages = build_intent_prompt(user_message=user_input, variables=variables)
        result = _safe_json_parse(client.chat(messages)["content"])

    intent = result.get("intent", "unknown")
    confidence = result.get("confidence", 0)
    print(f"      意图: {intent} (置信度: {confidence:.0%})")
    return result


def _mock_intent(user_input: str) -> dict:
    """Keyword-based intent classification (mock mode).

    Priority order (first match wins):
    1. crosstabs/chi-square — categorical association queries
    2. frequency/count — "多少人", "计数", etc.
    3. compare_groups — binary/multi-group comparison
    4. relationship — correlation/regression
    5. describe — descriptive statistics (fallback)
    """
    lower = user_input.lower()

    # ── 1. Crosstabs / Chi-square (before generic "关系" to avoid Pearson false-positive) ──
    crosstab_words = ("卡方", "交叉表", "列联表", "独立性检验")
    if any(w in lower for w in crosstab_words):
        return {
            "intent": "relationship",
            "confidence": 0.9,
            "rationale": "MOCK crosstab keyword",
            "modified_variable": None,
            "suggested_method": "chi_square",
        }

    # ── 2. Frequency / count queries (before compare, "男女" shouldn't trigger t-test for "多少人") ──
    freq_words = ("多少人", "几个人", "多少个", "计数", "人数", "频数", "个案数")
    if any(w in lower for w in freq_words):
        return {
            "intent": "describe",
            "confidence": 0.9,
            "rationale": "MOCK frequency keyword",
            "modified_variable": None,
            "suggested_method": "frequencies",
        }

    # ── 3. Group comparison (t-test / ANOVA) ──
    compare_words = (
        "比较",
        "差异",
        "差别",
        "显著",
        "差得",
        "compare",
        "diff",
        "男生",
        "女生",
        "男女",
        "不同",
        "区别",
    )
    if any(w in lower for w in compare_words):
        # Hint: "不同班级" / "各班级" / "几个班" → multi-group → ANOVA rather than t-test
        # Hint: "不同班级" / "各班级" / "几个班" → multi-group → ANOVA rather than t-test
        multi_group_hints = (
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
            "不同.*等级",
            "不同.*类型",
            "几种",
            "几类",
            "各类",
            "舱位",
            "不同舱",
            "几种舱",
        )
        method = (
            "oneway_anova" if any(w in lower for w in multi_group_hints) else "independent_t_test"
        )
        return {
            "intent": "compare_groups",
            "confidence": 0.9,
            "rationale": "MOCK keyword match",
            "modified_variable": None,
            "suggested_method": method,
        }

    # ── 4. Relationship (correlation / regression) ──
    relation_words = ("关系", "相关", "影响", "预测", "correlation", "regression")
    if any(w in lower for w in relation_words):
        return {
            "intent": "relationship",
            "confidence": 0.9,
            "rationale": "MOCK keyword match",
            "modified_variable": None,
            "suggested_method": "pearson_correlation",
        }

    # ── 5. Descriptive statistics (catch-all) ──
    describe_words = (
        "平均",
        "均值",
        "标准差",
        "描述",
        "统计",
        "频率",
        "分布",
        "mean",
        "describe",
        "frequen",
    )
    if any(w in lower for w in describe_words):
        return {
            "intent": "describe",
            "confidence": 0.9,
            "rationale": "MOCK keyword match",
            "modified_variable": None,
            "suggested_method": "descriptives",
        }
    return {
        "intent": "describe",
        "confidence": 0.5,
        "rationale": "MOCK fallback",
        "modified_variable": None,
        "suggested_method": "descriptives",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Step 4: Method Recommendation + Validation
# ═══════════════════════════════════════════════════════════════════════════


def recommend_method(
    intent_data: dict, variables: list[dict], user_input: str, row_count: int
) -> tuple[str, str | None, str | None]:
    """Recommend statistical method and validate with rule engine."""
    print("[4/8] 方法推荐...")

    intent = intent_data.get("intent", "unknown")

    if LLM_MOCK:
        # Auto-detect grouping and test variables from the dataset
        cat_var, num_var = _auto_detect_vars(variables, intent)
        suggested = intent_data.get("suggested_method")
        method = _mock_method(intent, cat_var, num_var, suggested)
    else:
        from snla.llm.client import LLMClient
        from snla.llm.prompts.method import build_method_prompt

        client = LLMClient()
        messages = build_method_prompt(
            intent=intent, variables=variables, conversation_context=user_input
        )
        try:
            result = _safe_json_parse(client.chat(messages)["content"])
            method = result.get("recommended_method", "descriptives")
            cat_var = result.get("grouping_variable")
            num_var = result.get("test_variable")
        except (ValueError, KeyError):
            # LLM returned unparseable JSON — fall back to intent hint
            suggested = intent_data.get("suggested_method")
            if suggested:
                method = suggested
                cat_var, num_var = _auto_detect_vars(variables, intent)
                print(f"      LLM 方法推荐失败，回退到建议方法: {method}")
            else:
                cat_var, num_var = _auto_detect_vars(variables, intent)
                method = _mock_method(intent, cat_var, num_var)
                print(f"      LLM 方法推荐失败，回退到 MOCK: {method}")

    # Rule-engine double-check
    from snla.syntax.templates import validate_method

    validation = validate_method(
        variables=variables,
        recommended_method=method,
        grouping_var=cat_var,
        test_var=num_var,
        row_count=row_count,
    )

    corrected = method
    if not validation.get("valid", True):
        for err in validation.get("errors", []):
            print(f"      ❌ {err}")
        corrected = validation.get("corrected_method") or method
        if corrected != method:
            print(f"      🔧 已自动更正为: {corrected}")
    else:
        print(f"      方法: {corrected}")

    for w in validation.get("warnings", []):
        print(f"      ⚠️ {w}")

    return corrected, cat_var, num_var


def _auto_detect_vars(variables: list[dict], intent: str) -> tuple[str | None, str | None]:
    """Auto-detect categorical and numeric variables from metadata."""
    cat_var = num_var = None
    cat_var2 = None
    for v in variables:
        if v.get("value_labels") and not cat_var:
            cat_var = v["name"]
        elif v.get("value_labels") and not cat_var2:
            cat_var2 = v["name"]
        elif v.get("type") == "Numeric" and not num_var:
            num_var = v["name"]
    # For correlation/regression, prefer two numeric variables
    if intent == "relationship" and num_var:
        num_var2 = None
        for v in variables:
            if v.get("type") == "Numeric" and v["name"] != num_var and not v.get("value_labels"):
                num_var2 = v["name"]
                break
    return cat_var, num_var


def _mock_method(
    intent: str, cat_var: str | None, num_var: str | None, suggested_method: str | None = None
) -> str:
    """Mock method recommendation based on intent. Respects suggested_method from intent."""
    if suggested_method:
        return suggested_method
    mapping = {
        "compare_groups": "independent_t_test",
        "relationship": "pearson_correlation",
        "describe": "descriptives",
        "visualize": "frequencies",
    }
    return mapping.get(intent, "descriptives")


# ═══════════════════════════════════════════════════════════════════════════
# Step 5: Syntax Generation + RAG Enhancement
# ═══════════════════════════════════════════════════════════════════════════


def generate_syntax(
    method: str, variables: list[dict], cat_var: str | None, num_var: str | None
) -> str:
    """Generate SPSS syntax, falling back to templates on LLM failure."""
    print("[5/8] 语法生成...")

    syntax = ""
    if not LLM_MOCK:
        try:
            from snla.llm.client import LLMClient
            from snla.llm.prompts.syntax import build_syntax_prompt

            client = LLMClient()
            dataset_summary = {"row_count": 30, "variable_count": len(variables)}
            messages = build_syntax_prompt(
                method=method, variables=variables, dataset_summary=dataset_summary
            )
            response = client.chat(messages)
            syntax = json.loads(response["content"]).get("syntax", "")
            if syntax:
                print(f"      LLM 生成: {syntax[:100]}...")
        except Exception as e:
            print(f"      LLM 失败 ({e}), 使用模板兜底")

    if not syntax:
        syntax = _syntax_from_template(method, variables, cat_var, num_var)
        print(f"      模板语法: {syntax[:100]}...")

    # RAG enhancement: show relevant documentation reference
    try:
        from snla.rag.integration import get_syntax_context

        ctx = get_syntax_context(method, n_chunks=1, max_chars=500)
        if ctx:
            # Extract just the command reference info
            lines = ctx.split("\n")[:3]
            ref = " ".join(l.strip() for l in lines if l.strip())[:120]
            print(f"      📚 RAG: {ref}...")
    except Exception:
        pass  # RAG is optional enhancement

    return syntax


def _syntax_from_template(
    method: str, variables: list[dict], cat_var: str | None, num_var: str | None
) -> str:
    """Generate syntax from pre-built templates."""
    from snla.syntax.templates import get_syntax_by_method

    cat = cat_var or (variables[0]["name"] if variables else "group")
    num = num_var or (variables[1]["name"] if len(variables) > 1 else "score")

    def _get_correlation_args(vars_list, cat, num):
        """Find two numeric variables for correlation."""
        num_vars = [
            v["name"] for v in vars_list if v.get("type") == "Numeric" and not v.get("value_labels")
        ]
        if len(num_vars) >= 2:
            return {"var1": num_vars[0], "var2": num_vars[1]}
        return {"var1": num, "var2": num}  # fallback

    args_map = {
        "independent_t_test": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "oneway_anova": {"group_var": cat, "test_var": num},
        "paired_t_test": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "simple_regression": {"dep_var": num, "indep_var": num},
        "pearson_correlation": _get_correlation_args(variables, cat, num),
        "correlations": _get_correlation_args(variables, cat, num),
        "chi_square": {"row_var": cat, "col_var": num},
        "frequencies": {"var": cat if cat else num},
        "descriptives": {"var": num},
    }
    args = args_map.get(method, {"var": num})
    return get_syntax_by_method(method, **args)


# ═══════════════════════════════════════════════════════════════════════════
# Step 6: Syntax Validation (Security Sandbox)
# ═══════════════════════════════════════════════════════════════════════════


def validate_syntax(syntax: str, var_list: list[str], name_map: dict[str, str]) -> dict:
    """Validate syntax against security sandbox."""
    print("[6/8] 安全校验...")

    from snla.syntax.validator import validate as basic_validate

    result = basic_validate(syntax, var_list)

    if result["valid"]:
        print("      ✅ 语法校验通过")
    else:
        for err in result.get("errors", []):
            print(f"      ❌ {err}")

    for w in result.get("warnings", []):
        print(f"      ⚠️ {w}")

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Step 7: SPSS Execution (Python Submit mode)
# ═══════════════════════════════════════════════════════════════════════════


def execute_spss(syntax: str, data_path: str, output_name: str = "e2e_analysis"):
    """Execute SPSS syntax and return the execution result."""
    print(f"[7/8] SPSS 执行 (模式: {SPSS_EXEC_MODE})...")

    from snla.executor.spss import SPSSExecutor

    executor = SPSSExecutor()
    result = executor.run(
        syntax=syntax,
        data_path=data_path,
        output_name=output_name,
    )

    if result.success:
        xml_size = os.path.getsize(result.xml_path) if result.xml_path else 0
        print(f"      ✅ 执行成功 ({result.duration_seconds:.1f}s, XML: {xml_size} bytes)")
    else:
        print(f"      ❌ 执行失败: {result.error_message}")

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Step 8: Parse & Explain
# ═══════════════════════════════════════════════════════════════════════════


def parse_and_explain(exec_result, method: str) -> dict:
    """Parse SPSS output and generate natural language explanation."""
    print("[8/8] 解析 & 白话解读...")

    # Parse OMS XML
    from snla.parser.output import parse as parse_output

    lst_text = ""
    if exec_result.lst_path and os.path.exists(exec_result.lst_path):
        with open(exec_result.lst_path, "r", encoding="utf-8", errors="replace") as f:
            lst_text = f.read()

    # Map method → analysis type for parser
    method_to_analysis = {
        "independent_t_test": "T-TEST",
        "paired_t_test": "T-TEST",
        "oneway_anova": "ANOVA",
        "simple_regression": "REGRESSION",
        "pearson_correlation": "CORRELATIONS",
        "correlations": "CORRELATIONS",
        "chi_square": "CROSSTABS",
        "frequencies": "FREQUENCIES",
        "descriptives": "DESCRIPTIVES",
    }
    analysis_type = method_to_analysis.get(method, "UNKNOWN")

    try:
        analysis_result = parse_output(
            oms_xml_path=exec_result.xml_path,
            lst_text=lst_text if lst_text else None,
            analysis_type=analysis_type,
        )
    except Exception as e:
        print(f"      ⚠️ 解析失败: {e}")
        return {"error": str(e)}

    print(f"      分析类型: {analysis_result.analysis_type}")
    print(f"      表格数:   {len(analysis_result.tables)}")
    for t in analysis_result.tables:
        print(f"        • {t.title} ({len(t.rows)} 行)")

    # Statistics summary
    stats = analysis_result.statistics
    if stats:
        print(f"      统计量:   {json.dumps(stats, ensure_ascii=False)}")

    # Generate explanation
    from snla.explainer.naturalize import explain as explain_result

    explanation = explain_result(analysis_result, use_llm_polish=False)
    print(f"\n{'─' * 60}")
    print(f"📊 白话解读:\n{explanation}")
    print(f"{'─' * 60}")

    return {
        "analysis_type": analysis_result.analysis_type,
        "tables": len(analysis_result.tables),
        "statistics": stats,
        "explanation": explanation,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════


def run_pipeline(data_path: str, query: str, output_dir: str = "./p0_output") -> dict:
    """Run the complete SNLA analysis pipeline."""
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"SNLA E2E Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Data: {data_path}")
    print(f"Query: {query}")
    print(f"{'=' * 60}\n")

    t_total = time.perf_counter()

    # Pipeline steps
    metadata = load_data(data_path)
    variables, name_map = sanitize(metadata)
    intent_data = recognize_intent(query, variables)
    method, cat_var, num_var = recommend_method(
        intent_data, variables, query, metadata["row_count"]
    )
    syntax = generate_syntax(method, variables, cat_var, num_var)
    var_names = [v["name"] for v in variables]
    val_result = validate_syntax(syntax, var_names, name_map)

    if not val_result["valid"]:
        print(f"\n❌ 语法校验未通过，无法继续执行。")
        return {"success": False, "validation_errors": val_result["errors"]}

    exec_result = execute_spss(syntax, data_path)
    if not exec_result.success:
        print(f"\n❌ SPSS 执行失败。")
        return {"success": False, "exec_error": exec_result.error_message}

    analysis = parse_and_explain(exec_result, method)

    total_time = time.perf_counter() - t_total
    print(f"\n⏱ 总耗时: {total_time:.1f}s")

    # Save report
    report = {
        "timestamp": datetime.now().isoformat(),
        "query": query,
        "data_file": data_path,
        "intent": intent_data.get("intent"),
        "method": method,
        "syntax": syntax,
        "validation": {"valid": val_result["valid"], "warnings": val_result.get("warnings", [])},
        "execution": {
            "success": exec_result.success,
            "duration_s": exec_result.duration_seconds,
            "xml_path": exec_result.xml_path,
        },
        "analysis": analysis,
        "total_time_s": total_time,
    }

    report_path = os.path.join(output_dir, "e2e_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n📄 报告已保存: {report_path}")

    return report


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="SNLA End-to-End Demo — NL → SPSS → Explanation")
    parser.add_argument("--data-file", required=True, help="Path to .sav or .csv data file")
    parser.add_argument("--query", default=None, help="Natural language analysis query")
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    parser.add_argument("--output-dir", default="./p0_output", help="Output directory")
    args = parser.parse_args()

    if not os.path.exists(args.data_file):
        print(f"❌ Data file not found: {args.data_file}")
        sys.exit(1)

    if args.interactive:
        run_interactive(args.data_file, args.output_dir)
    elif args.query:
        run_pipeline(args.data_file, args.query, args.output_dir)
    else:
        # Default: run 3 demo queries
        demo_queries = [
            "比较男女生在成绩上的差异",
            "计算各变量的描述统计",
            "研究年龄和成绩的关系",
        ]
        for q in demo_queries:
            print(f"\n{'#' * 60}")
            print(f"# Demo Query: {q}")
            print(f"{'#' * 60}")
            try:
                run_pipeline(args.data_file, q, args.output_dir)
            except Exception as e:
                print(f"\n❌ Pipeline failed: {e}")
                if DEBUG:
                    import traceback

                    traceback.print_exc()
                continue
            print("\n")


def run_interactive(data_path: str, output_dir: str):
    """Interactive REPL mode for the E2E pipeline."""
    print("\n🔹 SNLA Interactive Mode — type 'quit' to exit\n")
    print("Example queries:")
    print("  • 比较男女成绩差异")
    print("  • 计算描述统计")
    print("  • 年龄和成绩有关系吗\n")

    while True:
        try:
            query = input("📝 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Goodbye!")
            break

        if query.lower() in ("quit", "exit", "q"):
            print("👋 Goodbye!")
            break
        if not query:
            continue

        try:
            run_pipeline(data_path, query, output_dir)
        except Exception as e:
            print(f"\n❌ Error: {e}")
            if DEBUG:
                import traceback

                traceback.print_exc()


if __name__ == "__main__":
    main()
