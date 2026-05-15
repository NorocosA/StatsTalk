"""
SNLA Streamlit MVP Frontend

Single-page chat interface for natural language SPSS analysis.
Session state tracked via Streamlit's session_state.

Run: streamlit run snla/ui/streamlit_app.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import streamlit as st

from snla.config import DEBUG, LLM_MOCK, SPSS_EXECUTABLE
from snla.session import SessionState

# ── Session Initialization ──────────────────────────────────────────────


def init_session() -> None:
    """Initialize Streamlit session state on first run."""
    if "session" not in st.session_state:
        st.session_state.session = SessionState()
    if "messages" not in st.session_state:
        st.session_state.messages = [{
            "role": "assistant",
            "content": "👋 欢迎使用 SPSS 自然语言助手！"
                       "请先上传你的数据文件（.sav 或 .csv），"
                       "然后告诉我你想做什么分析。",
        }]
    if "stage" not in st.session_state:
        st.session_state.stage = "UPLOADING"

# ── Sidebar — File Upload & Variable Overview ──────────────────────────────


def render_sidebar() -> None:
    """Render sidebar with file uploader and variable overview."""
    with st.sidebar:
        st.header("📁 数据文件")
        uploaded_file = st.file_uploader(
            "上传 .sav 或 .csv 文件", type=["sav", "csv"],
            help="支持 SPSS (.sav) 和 CSV 文件", key="file_uploader",
        )
        if uploaded_file is not None and st.session_state.stage == "UPLOADING":
            handle_file_upload(uploaded_file)

        sess: SessionState = st.session_state.session
        if sess.has_data:
            st.divider()
            st.header("📋 变量概览")
            with st.expander(f"共 {len(sess.variables)} 个变量", expanded=False):
                for var in sess.variables:
                    col1, col2 = st.columns([2, 1])
                    with col1:
                        st.text(var["name"])
                    with col2:
                        st.caption(var.get("type", ""))
                    if var.get("label"):
                        st.caption(f"  {var['label']}")
                    if var.get("value_labels"):
                        labels_str = ", ".join(
                            f"{k}={v}" for k, v in var["value_labels"].items())
                        st.caption(f"  [{labels_str}]")


def handle_file_upload(uploaded_file: Any) -> None:
    """Process uploaded data file: save, read metadata, sanitize, update state."""
    try:
        suffix = ".sav" if uploaded_file.name.lower().endswith(".sav") else ".csv"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded_file.getbuffer())
            tmp_path: str = tmp.name

        from snla.data.reader import read_and_extract
        metadata = read_and_extract(tmp_path)
        sess: SessionState = st.session_state.session

        sess.dataset_meta = {
            "filename": uploaded_file.name,
            "format": "sav" if suffix == ".sav" else "csv",
            "row_count": metadata["row_count"],
            "column_count": metadata["column_count"],
            "file_path": tmp_path,
        }
        sess.variables = metadata["variables"]

        from snla.data.sanitizer import sanitize_variables
        sanitized_vars, sensitive_count = sanitize_variables(sess.variables)
        if sensitive_count > 0:
            sess.variables = sanitized_vars
            st.toast(f"⚠️ 检测到 {sensitive_count} 个变量含隐私信息，已自动脱敏", icon="⚠️")

        for v in sess.variables:
            if v.get("desensitized") and v.get("original_name"):
                sess.var_name_map[v["name"]] = v["original_name"]
                sess.reverse_var_name_map[v["original_name"]] = v["name"]

        st.session_state.stage = "READY"
        st.session_state.messages.append({
            "role": "assistant",
            "content": (
                f"✅ 已加载数据文件 **{uploaded_file.name}**\n\n"
                f"- 样本量：{metadata['row_count']}\n"
                f"- 变量数：{metadata['column_count']}\n\n"
                f"现在你可以输入分析需求了！例如：\n"
                f"- 计算各班级成绩的平均分\n"
                f"- 比较男女生在成绩上的差异\n"
                f"- 研究年龄和收入的关系"
            ),
        })
        st.rerun()
    except Exception as exc:
        st.error(f"❌ 文件读取失败: {exc}")
        if DEBUG:
            st.exception(exc)


# ── Chat Display & Input Area ────────────────────────────────────────────


def render_chat() -> None:
    """Render all chat messages from session state."""
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def render_input() -> None:
    """Render chat input and stop button based on current stage."""
    stage: str = st.session_state.stage
    sess: SessionState = st.session_state.session

    if stage == "UPLOADING":
        st.info("👆 请先在侧边栏上传数据文件")
        return

    col1, col2 = st.columns([5, 1])
    with col1:
        user_input = st.chat_input(
            "输入分析需求...", disabled=(stage in ("THINKING", "EXECUTING")))
    with col2:
        if stage in ("THINKING", "EXECUTING"):
            if st.button("⏹ 停止", type="secondary", use_container_width=True):
                sess.cancel()
                st.toast("正在停止当前操作...", icon="⏹")

    if user_input:
        handle_user_message(user_input)


def handle_user_message(user_input: str) -> None:
    """Process user's natural language analysis request."""
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.stage = "THINKING"
    with st.chat_message("user"):
        st.markdown(user_input)
    with st.chat_message("assistant"):
        process_analysis(user_input)


# ── Analysis Pipeline ───────────────────────────────────────────────────


def process_analysis(user_input: str) -> None:
    """Run the full SNLA analysis pipeline: intent→method→syntax→execute→parse→explain."""
    sess: SessionState = st.session_state.session
    try:
        # Step 1: Intent recognition
        with st.spinner("🤔 正在理解你的分析需求..."):
            intent_data = _call_intent(user_input)
        intent = intent_data.get("intent", "unknown")
        if intent == "unknown":
            _fail("❌ 抱歉，我没能理解你的分析需求。请换一种方式描述试试？")
            return

        # Step 1b: Handle follow_up — reuse previous context, skip method recommendation
        if intent == "follow_up":
            if sess.last_analysis is None:
                _fail("抱歉，没有上一轮分析上下文...")
                return
            recommended_method = sess.last_analysis["method"]
            modified_variable = intent_data.get("modified_variable")
            grouping_var = sess.last_analysis.get("grouping_var")
            test_var = sess.last_analysis.get("test_var")
            if modified_variable:
                # Auto-detect: modified var with value_labels → grouping_var, else → test_var
                is_grouping = any(
                    v["name"] == modified_variable and v.get("value_labels")
                    for v in sess.variables
                )
                if is_grouping:
                    grouping_var = modified_variable
                else:
                    test_var = modified_variable
            if modified_variable:
                st.info(
                    f"🔁 基于上一轮分析上下文，将 **{modified_variable}** "
                    f"代入 **{_method_label(recommended_method)}**"
                )
            else:
                st.info(
                    f"🔁 基于上一轮分析上下文，继续使用 **{_method_label(recommended_method)}**"
                )
        else:
            # Step 2: Method recommendation
            with st.spinner("🔬 正在推荐统计方法..."):
                method_data = _call_method(intent, user_input)
            recommended_method = method_data.get("recommended_method", "descriptives")
            grouping_var = method_data.get("grouping_variable")
            test_var = method_data.get("test_variable")

        # Step 3: Validate method with rules engine
        from snla.syntax.templates import validate_method
        validation = validate_method(
            variables=sess.variables,
            recommended_method=recommended_method,
            grouping_var=grouping_var, test_var=test_var,
            row_count=sess.dataset_meta.get("row_count"),
        )
        if not validation.get("valid", True):
            st.warning(f"⚠️ 方法校验警告: {'; '.join(validation.get('errors', []))}")
            corrected = validation.get("corrected_method")
            if corrected:
                recommended_method = corrected
                st.info(f"已自动更正为: {corrected}")
        for w in validation.get("warnings", []):
            st.warning(w)

        # Step 4: Syntax generation
        with st.spinner("✍️ 正在生成 SPSS 语法..."):
            syntax = _call_syntax(recommended_method)

        # Step 5: Validate syntax (security sandbox)
        from snla.syntax.validator import validate as validate_syntax
        if sess.var_name_map:
            syntax = sess.map_to_local(syntax)
        val_result = validate_syntax(syntax, sess.get_variable_names())
        if not val_result["valid"]:
            _fail("❌ 语法校验失败:\n" + "\n".join(f"- {e}" for e in val_result["errors"]))
            return
        if val_result.get("warnings"):
            st.warning("⚠️ 以下操作需要确认:\n" + "\n".join(f"- {w}" for w in val_result["warnings"]))

        # Show syntax for user review
        with st.expander("📝 查看生成的 SPSS 语法", expanded=False):
            st.code(syntax, language="text")

        # Step 6: Execute in SPSS (with 4-layer error recovery)
        st.session_state.stage = "EXECUTING"
        max_retries = 2
        attempt = 0
        exec_result = None

        while attempt <= max_retries:
            with st.spinner(f"⚙️ 正在 SPSS 中执行分析... (第 {attempt + 1} 次)"
                            if attempt > 0 else "⚙️ 正在 SPSS 中执行分析..."):
                exec_result = _execute_syntax(syntax, bool(val_result.get("warnings")))
            if sess.cancellation_token:
                _fail("⏹ 操作已取消")
                sess.reset_cancellation()
                return
            if exec_result.success:
                break  # ── success, exit retry loop

            # ── Layer 2: LLM auto-fix ──
            if attempt < max_retries:
                error_text = exec_result.stderr[:800] if exec_result.stderr else f"退出码: {exec_result.exit_code}"
                st.warning(f"⚠️ SPSS 执行失败，正在请求 LLM 自动修正...")
                fixed_syntax = _llm_fix_syntax(syntax, error_text, sess)
                if fixed_syntax and fixed_syntax != syntax:
                    # Re-validate the fixed syntax
                    val_result = validate_syntax(fixed_syntax, sess.get_variable_names())
                    if val_result["valid"]:
                        syntax = fixed_syntax
                        with st.expander("📝 查看修正后的 SPSS 语法", expanded=False):
                            st.code(syntax, language="text")
                        attempt += 1
                        continue
                st.warning("⚠️ LLM 修正失败，尝试模板兜底...")
                break  # fall through to template

            attempt += 1

        # ── Layer 3: Template fallback ──
        if not exec_result or not exec_result.success:
            st.warning("⚠️ 自动修正已用尽，切换至标准模板语法...")
            template_syntax = _syntax_template_fallback(recommended_method)
            if template_syntax:
                val_result = validate_syntax(template_syntax, sess.get_variable_names())
                if val_result["valid"]:
                    syntax = template_syntax
                    with st.expander("📝 查看模板语法（可能无法完全匹配原始意图）", expanded=True):
                        st.caption("具体差异：已从 LLM 生成语法切换为标准模板")
                        st.code(syntax, language="text")
                    with st.spinner("⚙️ 正在执行模板语法..."):
                        exec_result = _execute_syntax(syntax, False)

        # ── Layer 4: User manual edit ──
        if not exec_result or not exec_result.success:
            error_detail = (exec_result.stderr[:500] if exec_result and exec_result.stderr
                            else f"退出码: {exec_result.exit_code if exec_result else 'N/A'}")
            st.error(f"❌ 所有自动修正均已用尽，SPSS 执行仍失败\n\n"
                     f"```\n{error_detail}\n```")
            with st.expander("📝 手动编辑语法后重试", expanded=True):
                edited = st.text_area("编辑 SPSS 语法", value=syntax, height=150, key="manual_edit")
                if st.button("🔄 重新执行", key="retry_manual"):
                    val_result = validate_syntax(edited, sess.get_variable_names())
                    if val_result["valid"]:
                        exec_result = _execute_syntax(edited, False)
                        if exec_result and exec_result.success:
                            st.success("✅ 手动修正后执行成功！")
                            syntax = edited
                        else:
                            st.error("❌ 手动修正后执行仍失败，请检查语法")
                    else:
                        st.error("❌ 语法校验失败:\n" + "\n".join(f"- {e}" for e in val_result["errors"]))
            if not (exec_result and exec_result.success):
                _fail("❌ 无法完成 SPSS 执行，请检查数据或语法后重试")
                return

        # Step 7: Parse output
        with st.spinner("📊 正在解析结果..."):
            analysis_result = _parse_output(exec_result, recommended_method)

        # Step 8: Explain results
        with st.spinner("💬 正在生成白话解读..."):
            from snla.explainer.naturalize import explain as explain_result
            explanation = explain_result(analysis_result, use_llm_polish=False)

        # Display results
        st.markdown("### 📈 分析结果")
        st.markdown(explanation)
        if analysis_result.statistics:
            with st.expander("📊 关键统计量", expanded=False):
                for key, value in analysis_result.statistics.items():
                    st.metric(label=key.replace("_", " ").title(), value=value)

        # Record in session
        sess.add_message("assistant", explanation)
        sess.set_last_analysis(
            method=recommended_method, grouping_var=grouping_var,
            test_var=test_var, analysis_type=analysis_result.analysis_type,
        )
        st.session_state.messages.append({"role": "assistant", "content": explanation})
        st.session_state.stage = "READY"
        sess.reset_cancellation()

    except json.JSONDecodeError as exc:
        _fail(f"❌ LLM 返回格式错误: {exc}")
    except Exception as exc:
        _fail(f"❌ 分析过程中出错: {exc}")
        if DEBUG:
            st.exception(exc)


# ── Pipeline Helpers ────────────────────────────────────────────────────


def _get_cloud_vars() -> list[dict[str, Any]]:
    """Return cloud-safe variable list for LLM prompts.

    Desensitized variable names are mapped to their cloud-safe versions
    (var_01, var_02, etc.) and only safe fields (name, type, label,
    value_labels) are included in each variable dict sent to the LLM.
    """
    from snla.data.sanitizer import CLOUD_SAFE_FIELDS
    sess: SessionState = st.session_state.session
    # Use map_to_cloud to get desensitized names
    cloud_vars = sess.map_to_cloud(sess.variables) if sess.var_name_map else sess.variables
    # Strip each variable dict to only cloud-safe fields
    safe_vars = []
    for v in cloud_vars:
        safe_vars.append({
            k: v[k] for k in ("name", "type", "label", "value_labels")
            if k in v
        })
    return safe_vars


def _call_intent(user_input: str) -> dict[str, Any]:
    """Run intent recognition (or mock)."""
    if LLM_MOCK:
        return _mock_intent(user_input)
    from snla.llm.client import LLMClient
    from snla.llm.prompts.intent import build_intent_prompt
    sess: SessionState = st.session_state.session
    client = LLMClient()
    messages = build_intent_prompt(
        user_message=user_input, variables=_get_cloud_vars(),
        last_analysis=sess.last_analysis,
    )
    return json.loads(client.chat(messages)["content"])


def _call_method(intent: str, user_input: str) -> dict[str, Any]:
    """Run method recommendation (or mock)."""
    sess: SessionState = st.session_state.session
    # Read suggested_method hint set by _mock_intent
    hinted = getattr(sess, "_intent_suggested_method", None)
    if hinted is not None:
        delattr(sess, "_intent_suggested_method")  # consume once
    if LLM_MOCK:
        return _mock_method(intent, hinted)
    from snla.llm.client import LLMClient
    from snla.llm.prompts.method import build_method_prompt
    client = LLMClient()
    messages = build_method_prompt(
        intent=intent, variables=_get_cloud_vars(),
        conversation_context=user_input,
    )
    return json.loads(client.chat(messages)["content"])


def _call_syntax(method: str) -> str:
    """Generate SPSS syntax via LLM, falling back to templates on failure."""
    if LLM_MOCK:
        return _syntax_template_fallback(method)
    from snla.llm.client import LLMClient
    from snla.llm.prompts.syntax import build_syntax_prompt
    sess: SessionState = st.session_state.session
    client = LLMClient()
    dataset_summary = {
        "row_count": sess.dataset_meta.get("row_count", 0),
        "variable_count": sess.dataset_meta.get("column_count", 0),
    }
    try:
        messages = build_syntax_prompt(
            method=method, variables=_get_cloud_vars(),
            dataset_summary=dataset_summary,
        )
        return json.loads(client.chat(messages)["content"]).get("syntax", "")
    except Exception:
        return _syntax_template_fallback(method)


def _syntax_template_fallback(method: str) -> str:
    """Fall back to pre-built syntax templates when LLM fails."""
    from snla.syntax.templates import get_syntax_by_method
    sess: SessionState = st.session_state.session
    variables = sess.variables

    cat_var = num_var = None
    for v in variables:
        if v.get("value_labels"):
            cat_var = v["name"]
        elif v.get("type") == "Numeric" and num_var is None:
            num_var = v["name"]
    cat_var = cat_var or (variables[0]["name"] if variables else "group")
    num_var = num_var or (variables[1]["name"] if len(variables) > 1 else "score")

    # Find two numeric vars for correlation
    num_vars = [v["name"] for v in variables
                if v.get("type") == "Numeric" and not v.get("value_labels")]
    num_var2 = num_vars[1] if len(num_vars) > 1 else num_var

    fallback_args: dict[str, Any] = {
        "independent_t_test": {"group_var": cat_var, "test_var": num_var, "groups": (1, 2)},
        "oneway_anova": {"group_var": cat_var, "test_var": num_var},
        "simple_regression": {"dep_var": num_var, "indep_var": num_var},
        "pearson_correlation": {"var1": num_var, "var2": num_var2},
        "correlations": {"var1": num_var, "var2": num_var2},
        "chi_square": {"row_var": cat_var, "col_var": num_var},
        "frequencies": {"var": cat_var or num_var},
        "descriptives": {"var": num_var},
    }
    return get_syntax_by_method(method, **fallback_args.get(method, {"var": num_var}))


def _execute_syntax(syntax: str, has_greylist: bool) -> Any:
    """Execute SPSS syntax via Python Submit mode (or mock fallback)."""
    try:
        from snla.executor.spss import SPSSExecutor
    except Exception:
        return _MockExecResult()

    sess: SessionState = st.session_state.session
    try:
        executor = SPSSExecutor()
        if has_greylist:
            return executor.execute_on_temp_copy(
                syntax=syntax, data_path=sess.dataset_meta["file_path"],
                cancellation_token=sess.cancellation_token,
            )
        return executor.run(
            syntax=syntax, data_path=sess.dataset_meta["file_path"],
            cancellation_token=sess.cancellation_token,
        )
    except FileNotFoundError:
        st.warning("⚠️ SPSS 不可用（Python 解释器未找到），使用模拟结果")
        return _MockExecResult()
    except Exception as exc:
        st.error(f"SPSS 执行异常: {exc}")
        return _MockExecResult()


def _parse_output(exec_result: Any, analysis_type: str) -> Any:
    """Parse SPSS output (OMS XML preferred, LST fallback)."""
    # Map SNLA method names → SPSS analysis types
    METHOD_TO_ANALYSIS: dict[str, str] = {
        "independent_t_test": "T-TEST",
        "paired_t_test": "T-TEST",
        "oneway_anova": "ANOVA",
        "simple_regression": "REGRESSION",
        "pearson_correlation": "CORRELATIONS",
        "chi_square": "CROSSTABS",
        "frequencies": "FREQUENCIES",
        "descriptives": "DESCRIPTIVES",
    }
    spss_type = METHOD_TO_ANALYSIS.get(analysis_type, analysis_type.upper())

    # If mock exec result (xml_path is None), return mock analysis
    if getattr(exec_result, "xml_path", None) is None:
        return _mock_analysis_result(spss_type)

    from snla.parser.output import parse as parse_output
    lst_text = ""
    if exec_result.lst_path and os.path.exists(exec_result.lst_path):
        with open(exec_result.lst_path, "r", encoding="utf-8", errors="replace") as f:
            lst_text = f.read()
    try:
        return parse_output(
            oms_xml_path=exec_result.xml_path, lst_text=lst_text,
            analysis_type=spss_type,
        )
    except Exception:
        return _mock_analysis_result(spss_type)


# ── Mock Helpers (LLM_MOCK=True) ────────────────────────────────────────


class _MockExecResult:
    exit_code = 0
    stdout = "MOCK SPSS output"
    stderr = ""
    xml_path = None
    lst_path = None
    success = True
    error_message = None


def _llm_fix_syntax(failed_syntax: str, error_text: str, sess: SessionState) -> str | None:
    """Ask LLM to fix SPSS syntax based on the execution error.

    Layer 2 of the error recovery chain: sends the failed syntax and the
    SPSS error message to the LLM, requesting a corrected version.  Falls
    back to ``None`` on any failure so the caller proceeds to Layer 3.

    Args:
        failed_syntax: The SPSS syntax that failed execution.
        error_text: The stderr / error message from SPSS.
        sess: Current ``SessionState`` with variable metadata.

    Returns:
        Corrected syntax string, or ``None`` if the fix attempt failed.
    """
    if LLM_MOCK:
        return None  # MOCK mode can't fix syntax

    from snla.llm.client import LLMClient
    from snla.data.sanitizer import filter_for_cloud

    try:
        client = LLMClient()
        vars_filtered = filter_for_cloud({"variables": sess.variables})
        var_list = vars_filtered.get("variables", sess.variables)

        var_desc = "\n".join(
            f"- {v['name']} ({v.get('type', '?')})"
            + (f" [{', '.join(f'{k}={val}' for k, val in v.get('value_labels', {}).items())}]"
               if v.get('value_labels') else "")
            for v in var_list[:20]
        )

        system = (
            "你是 SPSS 语法专家。用户提供的语法执行失败，请根据错误信息修正语法。"
            "仅返回修正后的完整 SPSS 语法字符串（以句点结尾），不要返回任何解释或 JSON。"
        )
        user = (
            "[DATASET VARIABLES]\n"
            f"{var_desc}\n\n"
            "[FAILED SYNTAX]\n"
            f"{failed_syntax}\n\n"
            "[SPSS ERROR]\n"
            f"{error_text}\n\n"
            "请修正以上语法，仅返回修正后的语法字符串："
        )

        response = client.chat(
            messages=[{"role": "user", "content": user}],
            system_prompt=system,
            temperature=0.1,
            max_tokens=500,
        )
        fixed = response.get("content", "").strip()
        if fixed and "." in fixed:
            # Extract just the SPSS command (strip any extra text)
            # Take everything up to the last period + period
            last_dot = fixed.rfind(".")
            if last_dot > 0:
                fixed = fixed[:last_dot + 1]
            return fixed
        return None
    except Exception:
        return None


def _method_label(method: str) -> str:
    """Convert method code to Chinese label."""
    labels = {
        "independent_t_test": "独立样本t检验",
        "paired_t_test": "配对样本t检验",
        "oneway_anova": "单因素方差分析",
        "pearson_correlation": "Pearson相关分析",
        "spearman_correlation": "Spearman相关分析",
        "simple_regression": "简单线性回归",
        "chi_square": "卡方检验",
        "descriptives": "描述统计",
        "frequencies": "频率分析",
    }
    return labels.get(method, method)


def _mock_intent(user_input: str) -> dict[str, Any]:
    """Keyword-based intent classification for mock mode."""
    lower = user_input.lower()
    # Follow-up detection — must come before other keyword checks
    sess = st.session_state.session
    if sess.last_analysis is not None:
        follow_markers = ("换成", "再看看", "改为", "改成", "换一下", "那...呢", "呢")
        has_follow = any(w in lower for w in follow_markers)
        if has_follow:
            modified_var = None
            for v in sess.variables:
                if v["name"].lower() in lower or ((v.get("label") or "").lower() in lower if v.get("label") else False):
                    modified_var = v["name"]
                    break
            if modified_var:
                return {"intent": "follow_up", "confidence": 0.85, "rationale": "MOCK follow_up",
                        "modified_variable": modified_var, "suggested_method": None}
    # ── Frequency / count queries (before compare, "男女" shouldn't trigger t-test for "多少人") ──
    freq_words = ("多少人", "几个人", "多少个", "计数", "人数", "频数", "个案数")
    if any(w in lower for w in freq_words):
        sess._intent_suggested_method = "frequencies"
        return {"intent": "describe", "confidence": 0.9, "rationale": "MOCK frequency keyword",
                "modified_variable": None, "suggested_method": "frequencies"}

    # ── Crosstabs / chi-square (before generic "关系" to avoid Pearson false-positive) ──
    crosstab_words = ("卡方", "交叉表", "列联表", "独立性检验")
    if any(w in lower for w in crosstab_words):
        sess._intent_suggested_method = "chi_square"
        return {"intent": "relationship", "confidence": 0.9, "rationale": "MOCK crosstab keyword",
                "modified_variable": None, "suggested_method": "chi_square"}

    if any(w in lower for w in ("比较", "差异", "差别", "显著", "差得", "compare", "diff", "男生", "女生", "男女")):
        # Multi-group hints → ANOVA, else t-test
        multi_group = any(w in lower for w in ("各", "不同班", "多个", "三种", "三级", "四组", "几组", "各组", "几个班"))
        method = "oneway_anova" if multi_group else "independent_t_test"
        if multi_group:
            sess._intent_suggested_method = method
        return {"intent": "compare_groups", "confidence": 0.9, "rationale": "MOCK",
                "modified_variable": None, "suggested_method": method}
    if any(w in lower for w in ("关系", "相关", "影响", "预测", "correlation", "regression")):
        return {"intent": "relationship", "confidence": 0.9, "rationale": "MOCK",
                "modified_variable": None, "suggested_method": "pearson_correlation"}
    if any(w in lower for w in ("平均", "均值", "标准差", "描述", "统计", "mean", "describe")):
        return {"intent": "describe", "confidence": 0.9, "rationale": "MOCK",
                "modified_variable": None, "suggested_method": "descriptives"}
    return {"intent": "describe", "confidence": 0.5, "rationale": "MOCK fallback",
            "modified_variable": None, "suggested_method": "descriptives"}


def _mock_method(intent: str, hinted_method: str | None = None) -> dict[str, Any]:
    """Return a reasonable mock method recommendation."""
    if intent == "follow_up":
        sess = st.session_state.session
        if sess.last_analysis:
            return {
                "recommended_method": sess.last_analysis["method"],
                "grouping_variable": sess.last_analysis.get("grouping_var"),
                "test_variable": sess.last_analysis.get("test_var"),
                "alternatives": [], "assumptions_check": [],
                "rationale": "MOCK follow_up", "confidence": 0.85,
            }
        return {"recommended_method": "descriptives", "grouping_variable": None, "test_variable": None,
                "alternatives": [], "assumptions_check": [], "rationale": "MOCK fallback", "confidence": 0.5}
    sess: SessionState = st.session_state.session
    cat_var = num_var = None
    for v in sess.variables:
        if v.get("value_labels") and cat_var is None:
            cat_var = v["name"]
        elif v.get("type") == "Numeric" and num_var is None:
            num_var = v["name"]

    methods = {
        "compare_groups": (hinted_method or "independent_t_test", cat_var, num_var),
        "relationship": (hinted_method or "pearson_correlation", None, None),
        "describe": (hinted_method or "descriptives", None, num_var),
        "visualize": ("histogram", None, num_var),
    }
    method_name, gv, tv = methods.get(intent, methods["describe"])
    return {"recommended_method": method_name, "grouping_variable": gv,
            "test_variable": tv, "alternatives": [],
            "assumptions_check": ["normality"], "rationale": "MOCK", "confidence": 0.9}


def _mock_analysis_result(analysis_type: str) -> Any:
    """Return a mock AnalysisResult for UI demonstration."""
    from snla.parser.schema import AnalysisResult, TableResult
    at = analysis_type.upper() if analysis_type else "UNKNOWN"
    stats: dict[str, Any] = {"p_value": 0.021, "t_value": 2.34, "df": 98}
    if "T-TEST" in at:
        stats.update({"mean_group1": 79.5, "mean_group2": 84.2, "mean_diff": 4.7, "n_valid": 100})
    elif "REGRESSION" in at:
        stats.update({"r_squared": 0.35, "f_value": 52.3})
    elif "ANOVA" in at:
        stats.update({"f_value": 8.42, "p_value": 0.001})
    return AnalysisResult(
        analysis_type=at,
        tables=[TableResult(title="Mock Output", rows=[], source_format="mock")],
        statistics=stats, notes=["MOCK MODE — 非真实 SPSS 输出"], parser_used="mock",
    )


# ── Helpers ─────────────────────────────────────────────────────────────


def _fail(message: str) -> None:
    """Display an error and reset stage to READY."""
    st.markdown(message)
    st.session_state.messages.append({"role": "assistant", "content": message})
    st.session_state.stage = "READY"
    st.session_state.session.reset_cancellation()


# ── Main Entry Point ────────────────────────────────────────────────────


def main() -> None:
    """Main Streamlit app entry point."""
    st.set_page_config(
        page_title="SPSS Natural Language Assistant", page_icon="📊",
        layout="wide", initial_sidebar_state="expanded",
    )
    st.title("📊 SPSS Natural Language Assistant")
    st.caption("用说话的方式完成统计分析")
    init_session()
    render_sidebar()
    render_chat()
    render_input()


if __name__ == "__main__":
    main()
