"""
Analysis pipeline functions for the SNLA server.

Extracted from server.py.  These functions orchestrate the core analysis
flow: syntax generation → validation → execution → parsing → explanation.

Shared mutable state (*session*, *_was_cancelled*) is accessed via lazy
import from ``snla.ui.server`` to avoid circular dependencies.
"""

from __future__ import annotations

import json
import logging
import os

from snla.config import LLM_MOCK, STATS_BACKEND
from snla.data.sanitizer import filter_for_cloud
from snla.orchestrator import GreylistPending, planner

from ._helpers import _can_full_interpret, _has_llm, _load_dataframe, _make_executor

logger = logging.getLogger(__name__)


# ── Phase 2: Report Interpretation ───────────────────────────────────────────


def _phase2_explain(parsed, method: str, user_input: str) -> str:
    """Explain SPSS results using LLM polish when available.

    The constraint layer (explainer/naturalize.py) always runs first
    to enforce statistical correctness.  If an LLM is available, the
    constrained output is polished for readability.
    """
    from snla.explainer.naturalize import explain

    use_llm = _has_llm() and not LLM_MOCK
    if use_llm:
        from snla.llm.client import LLMClient

        return explain(parsed, use_llm_polish=True, llm_client=LLMClient())
    return explain(parsed, use_llm_polish=False)


# ── Syntax template generation ──────────────────────────────────────────────


def _syntax_template(
    method: str, grouping_var: str | None = None, test_var: str | None = None
) -> str:
    import snla.ui.server as _server
    from snla.syntax.templates import get_syntax_by_method

    vars_list = _server.session.variables
    if not vars_list:
        return ""

    # Auto-detect only if not explicitly provided by Phase 1
    cat_var = num_var = num_var2 = None
    if grouping_var:
        cat_var = grouping_var
        # Verify it exists in dataset
        if not any(v["name"] == grouping_var for v in vars_list):
            cat_var = None
    if test_var:
        num_var = test_var
        if not any(v["name"] == test_var for v in vars_list):
            num_var = None

    # Skip metadata/ID variables in auto-detection
    _skip_vars = {"id", "ID", "Id", "customerid", "customer_id", "row", "ROW", "case", "CASE"}
    _ok_var = lambda v: v.get("type") == "Numeric" and v["name"] not in _skip_vars

    # Fall back to auto-detection if Phase 1 didn't provide
    if not cat_var or not num_var:
        for v in vars_list:
            if v.get("value_labels") and not cat_var:
                cat_var = v["name"]
            elif _ok_var(v) and not num_var:
                num_var = v["name"]
            elif _ok_var(v) and not num_var2 and v["name"] != num_var:
                num_var2 = v["name"]

    # Default fallback — skip id columns
    _def_num = next(
        (
            v["name"]
            for v in vars_list
            if v.get("type") == "Numeric" and v["name"] not in _skip_vars
        ),
        "score",
    )
    cat = cat_var or (vars_list[0]["name"] if vars_list else "group")
    num = num_var or _def_num

    def _corr_args():
        """Find two numeric variables for correlation (skip id columns)."""
        _skip = {"id", "ID", "Id"}
        nv = [
            v["name"]
            for v in vars_list
            if v.get("type") == "Numeric" and not v.get("value_labels") and v["name"] not in _skip
        ]
        if len(nv) >= 2:
            return {"var1": nv[0], "var2": nv[1]}
        return {"var1": num, "var2": num}

    args_map = {
        "independent_t_test": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "oneway_anova": {"group_var": cat, "test_var": num},
        "paired_t_test": _corr_args(),  # two paired numeric variables (before/after)
        "simple_regression": {"dep_var": num, "indep_var": num_var2 or num},
        "pearson_correlation": _corr_args(),
        "spearman_correlation": _corr_args(),
        "correlations": _corr_args(),
        "chi_square": {"row_var": cat, "col_var": num},
        "crosstabs": {"row_var": cat, "col_var": num},
        "frequencies": {"var": cat if cat else num},
        "descriptives": {"var": num},
        "mann_whitney_u": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "kruskal_wallis": {"group_var": cat, "test_var": num},
    }
    args = args_map.get(method, {"var": num})
    # Map method aliases to template keys
    template_method = {
        "crosstabs": "chi_square",
        "mann_whitney": "mann_whitney_u",
        "kruskal_wallis": "kruskal_wallis",
    }.get(method, method)
    return get_syntax_by_method(template_method, **args)


# ── Syntax execution ────────────────────────────────────────────────────────


def _execute_syntax(syntax: str, executor=None, cancellation_token: bool = False):
    if executor is None:
        executor = _make_executor()

    import snla.ui.server as _server

    # Find the data file path — use the last uploaded file
    data_path = _server.session.dataset_meta.get("file_path", "") if _server.session.dataset_meta else ""
    if not data_path and _server.session.dataset_meta:
        # Re-save from in-memory data if available
        pass
    result = executor.run(syntax=syntax, data_path=data_path, cancellation_token=cancellation_token)
    # Read LST file content from path (ExecutionResult has lst_path, not lst_text)
    lst_text = ""
    if result.lst_path and os.path.isfile(result.lst_path):
        try:
            with open(result.lst_path, encoding="utf-8", errors="replace") as f:
                lst_text = f.read()
        except OSError:
            pass
    return {
        "success": result.success,
        "exit_code": result.exit_code,
        "xml_path": result.xml_path,
        "lst_text": lst_text,
        "error": result.error_message,
    }


# ── Output parsing ──────────────────────────────────────────────────────────


def _parse_output(exec_result: dict, method: str):
    from snla.parser.output import parse

    # Map method name → parser analysis type
    method_to_analysis = {
        "independent_t_test": "T-TEST",
        "paired_t_test": "T-TEST",
        "oneway_anova": "ANOVA",
        "simple_regression": "REGRESSION",
        "pearson_correlation": "CORRELATIONS",
        "correlations": "CORRELATIONS",
        "spearman_correlation": "CORRELATIONS",
        "chi_square": "CROSSTABS",
        "crosstabs": "CROSSTABS",
        "frequencies": "FREQUENCIES",
        "descriptives": "DESCRIPTIVES",
        "mann_whitney_u": "T-TEST",
        "kruskal_wallis": "ANOVA",
    }
    analysis_type = method_to_analysis.get(method, "UNKNOWN")
    try:
        return parse(
            oms_xml_path=exec_result.get("xml_path"),
            lst_text=exec_result.get("lst_text"),
            analysis_type=analysis_type,
        )
    except (ValueError, RuntimeError, FileNotFoundError):
        from snla.parser.schema import AnalysisResult

        return AnalysisResult(
            analysis_type=analysis_type or method or "UNKNOWN",
            tables=[],
            statistics={},
            notes=["SPSS 执行完成，但未能解析输出。请检查数据文件格式。"],
        )


# ── LLM syntax fixing ───────────────────────────────────────────────────────


def _llm_fix_syntax(failed_syntax: str, error_text: str, method: str | None = None) -> str | None:
    if LLM_MOCK or not _has_llm():
        return None
    try:
        import snla.ui.server as _server
        from snla.llm.client import LLMClient

        cloud_vars = filter_for_cloud({"variables": _server.session.variables}).get("variables", [])
        var_list = "\n".join(f"  - {v['name']} ({v.get('type', '?')})" for v in cloud_vars[:30])

        # Try RAG context retrieval for official SPSS syntax reference
        rag_context = ""
        if method:
            try:
                from snla.rag.integration import get_syntax_context

                rag_context = get_syntax_context(method, n_chunks=2, max_chars=2000)
            except Exception:
                pass  # RAG is optional — silently skip on any error

        user_content = (
            f"以下 SPSS 语法执行失败。\n\n"
            f"可用变量:\n{var_list}\n\n"
            f"错误语法:\n{failed_syntax}\n\n"
            f"错误信息:\n{error_text}\n\n"
            f'请返回修正后的语法，JSON 格式: {{"syntax": "..."}}'
        )
        if rag_context:
            user_content = f"SPSS 官方语法参考:\n{rag_context}\n\n{user_content}"

        messages = [
            {"role": "system", "content": "你是 SPSS 语法专家。修正下面的语法错误。如有官方参考文档，严格按文档规范修正。"},
            {"role": "user", "content": user_content},
        ]
        client = LLMClient()
        result = client.chat(messages)
        try:
            parsed = json.loads(result.get("content", "{}"))
            return parsed.get("syntax")
        except (json.JSONDecodeError, AttributeError):
            return None
    except Exception:
        logger.exception("LLM fix syntax failed")
        return None


# ── Python backend fast path ─────────────────────────────────────────────────


def _run_python_backend(plan_result, user_input: str) -> dict | None:
    """Try Python backend. Returns response dict on success, None to fall through to SPSS.

    On success (method trusted or untrusted), returns a dict suitable for :func:`jsonify`.
    On failure or when ``STATS_BACKEND != "python"``, returns None so the caller
    proceeds with the SPSS path.
    """
    if STATS_BACKEND != "python":
        return None

    try:
        df = _load_dataframe()
        if df is None:
            logger.warning("Python backend: failed to load dataframe")
            return None

        from snla.executor.python import PythonStatsExecutor

        py_exec = PythonStatsExecutor()
        method = plan_result.method
        plan_explanation = plan_result.plan_explanation
        gvar = plan_result.grouping_variable
        tvar = plan_result.test_variable

        result = py_exec.execute(
            method, df, grouping_var=gvar, test_var=tvar, dep_var=gvar, indep_var=tvar
        )

        import snla.ui.server as _server

        # P5-4: Strategy C — no-SPSS + untrusted method → raw numbers only
        if not _can_full_interpret(method):
            _server.session.history.append({"role": "user", "content": user_input})
            _server.session.history.append(
                {"role": "assistant", "content": None, "method": method, "result": result}
            )
            _server.session.last_analysis = {"method": method}
            return {
                "ok": True,
                "method": method,
                "backend": "python",
                "plan_explanation": plan_explanation,
                "result": {
                    "analysis_type": result.analysis_type,
                    "tables": [{"title": t.title, "rows": t.rows} for t in result.tables],
                    "statistics": result.statistics,
                    "n_valid": result.n_valid,
                    "parser_used": result.parser_used,
                },
                "explanation": None,
                "warning": (
                    f"Python 引擎下「{method}」方法的可靠性尚未经 SPSS 交叉验证。"
                    f"以下为原始统计数字，非统计专业人士请谨慎解读。"
                    f"建议安装 SPSS 以获得完整解读。"
                ),
                "limited_mode": True,
                "last_analysis": _server.session.last_analysis,
            }

        # Trusted method or SPSS available → full explanation
        explanation = _phase2_explain(result, method, user_input)
        _server.session.history.append({"role": "user", "content": user_input})
        _server.session.history.append(
            {"role": "assistant", "content": explanation, "method": method, "result": result}
        )
        _server.session.last_analysis = {"method": method}
        return {
            "ok": True,
            "method": method,
            "backend": "python",
            "plan_explanation": plan_explanation,
            "result": {
                "analysis_type": result.analysis_type,
                "tables": [{"title": t.title, "rows": t.rows} for t in result.tables],
                "statistics": result.statistics,
                "n_valid": result.n_valid,
                "parser_used": result.parser_used,
            },
            "explanation": explanation,
            "last_analysis": _server.session.last_analysis,
        }
    except Exception:
        logger.exception("Python backend failed, falling through to SPSS")
        return None


# ── Syntax generation + validation + greylist gating ────────────────────────


def _prepare_syntax(
    method: str,
    grouping_variable: str | None,
    test_variable: str | None,
    confirm_greylist: bool = False,
    user_input: str = "",
):
    """Generate, validate, and gate SPSS syntax.

    Returns a dict with the following possible keys:

    - ``{"syntax": str, "greylist_warnings": list, "used_template": bool}``
      when syntax is ready to execute.
    - ``{"error": str, "syntax": str, "validation_errors": list}``
      when validation fails after LLM fix + template fallback.
    - ``{"_greylist": True, "syntax": str, "greylist_warnings": list}``
      when the syntax requires user confirmation (already staged in
      ``planner``).  The caller should return the confirmation response
      immediately.

    The caller dispatches based on the dict key presence:

        prep = _prepare_syntax(...)
        if prep.get("error"):
            return jsonify(prep), 422
        if prep.get("_greylist"):
            return jsonify(confirmation_response(prep))
        syntax, warnings, used_template = prep["syntax"], prep["greylist_warnings"], prep["used_template"]
    """
    import snla.ui.server as _server

    syntax = _syntax_template(method, grouping_var=grouping_variable, test_var=test_variable)
    used_template = True  # Always template-based now (fast, reliable)

    from snla.syntax.validator import validate

    validation = validate(syntax, [v["name"] for v in _server.session.variables])
    if not validation["valid"]:
        # Try LLM fix once
        fixed = _llm_fix_syntax(syntax, "; ".join(validation["errors"]), method)
        if fixed:
            syntax = fixed
            validation = validate(syntax, [v["name"] for v in _server.session.variables])
        else:
            # Use pre-built template directly — preserve original variables
            syntax = _syntax_template(method, grouping_var=grouping_variable, test_var=test_variable)
            validation = validate(syntax, [v["name"] for v in _server.session.variables])
            used_template = True

    if not validation["valid"]:
        return {
            "error": "Syntax validation failed",
            "syntax": syntax,
            "validation_errors": validation["errors"],
        }

    greylist_warnings = [
        w
        for w in validation.get("warnings", [])
        if "greylist" in w.lower() or "confirm" in w.lower()
    ]

    # ── Greylist gate: require explicit confirmation ───────────────
    if greylist_warnings and not confirm_greylist:
        planner.stage_greylist(
            "default",
            GreylistPending(
                syntax=syntax,
                warnings=greylist_warnings,
                method=method,
                user_input=user_input,
            ),
        )
        return {
            "_greylist": True,
            "syntax": syntax,
            "greylist_warnings": greylist_warnings,
        }

    return {
        "syntax": syntax,
        "greylist_warnings": greylist_warnings,
        "used_template": used_template,
    }


# ── Execute + parse combined ────────────────────────────────────────────────


def _execute_and_parse(syntax: str, executor, method: str, exec_result: dict | None = None):
    """Execute SPSS syntax and parse the output.

    When *exec_result* is provided (e.g. from a temp-copy execution),
    execution is skipped and the pre-built result is parsed directly.

    Returns:
        ``(exec_result_dict, AnalysisResult)`` on success.
        ``None`` if execution was cancelled by the user
        (caller should return a ``{"cancelled": True}`` response).
    """
    import snla.ui.server as _server

    if exec_result is None:
        exec_result = _execute_syntax(
            syntax, executor, cancellation_token=_server.session.cancellation_token
        )

    if _server._was_cancelled or _server.session.cancellation_token:
        return None

    parsed = _parse_output(exec_result, method)
    return (exec_result, parsed)
