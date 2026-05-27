"""
SNLA MCP Server — statistical analysis via natural language over MCP protocol.

Exposes 7 tools for OpenClaw / Claude Desktop / any MCP client:

    snla_status     — server health, trusted methods, SPSS availability
    snla_upload     — upload .sav / .csv data file
    snla_variables  — list variable metadata
    snla_analyze    — plan + execute statistical analysis
    snla_confirm    — confirm a pending greylist operation
    snla_cancel     — cancel running analysis
    snla_export     — export last result as DOCX

Usage:
    python snla/mcp_server.py                  # stdio transport (Claude Desktop)
    python snla/mcp_server.py --transport sse  # SSE transport (OpenClaw)

Design decisions (from P6 grill, 2026-05-23):
    - Direct integration with orchestrator (not HTTP-wrapping Flask)
    - Session-scoped file storage in ``uploads/{session_id}/`` (30-min TTL)
    - Two-tool greylist flow (analyze returns requires_confirmation → confirm resumes)
    - Structured errors: {ok, error: {category, user_message, code, suggestion}}
    - Python backend fast path for trusted methods; ENGINE_BUSY for SPSS contention
    - simple_regression: hard-reject without SPSS with actionable alternatives
"""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

# ── Ensure project root on sys.path ───────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)  # config.py reads .env from CWD

# ── SNLA imports (after path setup) ────────────────────────────────────
from snla.config import STATS_BACKEND
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud
from snla.explainer.export import export_to_docx
from snla.explainer.naturalize import explain as naturalize_explain
from snla.orchestrator import GreylistPending, NoPendingError, PlanResult, planner
from snla.parser.output import parse as parse_output
from snla.syntax.templates import get_syntax_by_method
from snla.syntax.validator import validate as validate_syntax
from snla.trust import get_trusted_methods, is_method_trusted, trust_loaded_from

# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

SESSION_TTL = 30 * 60  # 30 minutes — uploads/{session_id}/ cleanup
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB max upload


# ═════════════════════════════════════════════════════════════════════════
# Per-session state (in-memory; replaces Flask's global SessionState)
# ═════════════════════════════════════════════════════════════════════════

METHOD_TO_ANALYSIS = {
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


@dataclass
class MCPState:
    """Per-session analysis state."""

    variables: list[dict] = None
    dataset_meta: dict | None = None
    file_path: str | None = None
    last_analysis: dict | None = None  # metadata for follow-up
    last_result: Any = None  # AnalysisResult from parser
    last_explanation: str = ""  # natural-language explanation
    last_method: str = ""
    last_query: str = ""
    _executing: bool = False
    _cancelled: bool = False

    def __post_init__(self):
        if self.variables is None:
            self.variables = []


_session_states: dict[str, MCPState] = {}
_upload_dir = Path("uploads")


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════


def _session_state(ctx: Context) -> MCPState:
    """Get or create per-session state."""
    sid = ctx.session_id
    if sid not in _session_states:
        _session_states[sid] = MCPState()
    return _session_states[sid]


def _engine_busy() -> dict:
    return {
        "ok": False,
        "error": {
            "category": "system",
            "user_message": (
                "当前正有另一个分析在执行中（SPSS 引擎为独占资源），预计 15 秒后可用，请稍后重试。"
            ),
            "code": "ENGINE_BUSY",
            "suggestion": None,
        },
    }


def _mk_error(category: str, user_message: str, code: str, suggestion: str | None = None) -> dict:
    """Factory for structured error responses (grill Q6)."""
    return {
        "ok": False,
        "error": {
            "category": category,
            "user_message": user_message,
            "code": code,
            "suggestion": suggestion,
        },
    }


def _cleanup_stale_uploads():
    """Remove upload directories older than SESSION_TTL."""
    if not _upload_dir.exists():
        return
    now = time.time()
    for child in _upload_dir.iterdir():
        if child.is_dir():
            try:
                if now - child.stat().st_mtime > SESSION_TTL:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                pass


# ═════════════════════════════════════════════════════════════════════════
# Lifespan
# ═════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Startup: create upload directory. Shutdown: cleanup."""
    _upload_dir.mkdir(exist_ok=True)
    _cleanup_stale_uploads()
    try:
        yield {}
    finally:
        shutil.rmtree(_upload_dir, ignore_errors=True)


mcp = FastMCP("SNLA", lifespan=server_lifespan)


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_status
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_status(ctx: Context) -> dict:
    """Get server health, trusted methods, and SPSS availability.

    Returns the list of analysis methods currently trusted for Python-only
    execution and whether SPSS is available on this machine.  LLM should
    consult this before routing user requests to specific methods.
    """
    from snla.config import check_spss_available

    state = _session_state(ctx)
    return {
        "ok": True,
        "backend": STATS_BACKEND,
        "spss_available": check_spss_available(),
        "trusted_methods": get_trusted_methods(),
        "trust_source": trust_loaded_from(),
        "has_data": bool(state.variables),
        "variable_count": len(state.variables),
        "filename": state.dataset_meta.get("filename", "") if state.dataset_meta else "",
        "executing": state._executing,
    }


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_upload
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_upload(
    ctx: Context,
    file_path: str,
) -> dict:
    """Upload a data file (.sav or .csv) for analysis.

    Args:
        file_path: Absolute path to the local data file on the server.

    Returns variable metadata (name, type, label) for LLM consumption.
    """
    state = _session_state(ctx)
    fp = Path(file_path)
    if not fp.exists():
        return _mk_error(
            "user", f"文件不存在: {file_path}", "FILE_NOT_FOUND", "请检查文件路径后重试。"
        )

    size = fp.stat().st_size
    if size > MAX_FILE_SIZE:
        return _mk_error(
            "user",
            f"文件过大 ({size / 1024 / 1024:.1f} MB)，"
            f"最大支持 {MAX_FILE_SIZE / 1024 / 1024:.0f} MB",
            "FILE_TOO_LARGE",
            "请使用更小的数据集。",
        )

    # Copy to session-scoped upload directory
    session_dir = _upload_dir / ctx.session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    dest = session_dir / fp.name
    shutil.copy2(fp, dest)
    state.file_path = str(dest)

    # Read metadata
    try:
        meta = read_and_extract(str(dest))
        state.variables = meta.get("variables", [])
        state.dataset_meta = meta
    except Exception as e:
        return _mk_error(
            "system", f"文件解析失败: {e}", "PARSE_ERROR", "请确认文件格式正确（.sav 或 .csv）。"
        )

    cloud_vars = filter_for_cloud({"variables": state.variables}).get("variables", [])
    await ctx.info(
        f"已上传 {fp.name}（{len(state.variables)} 个变量，{meta.get('row_count', 0)} 条记录）"
    )

    return {
        "ok": True,
        "filename": fp.name,
        "row_count": meta.get("row_count", 0),
        "variable_count": len(state.variables),
        "variables": cloud_vars,
    }


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_variables
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_variables(ctx: Context) -> dict:
    """List variables in the currently uploaded data file.

    Returns cloud-safe metadata (name, type, label, value_labels) that the
    LLM can use to match user intent to actual variable names.
    """
    state = _session_state(ctx)
    if not state.variables:
        return _mk_error(
            "user", "请先上传数据文件", "NO_DATA", "使用 snla_upload 上传 .sav 或 .csv 文件后重试。"
        )
    cloud_vars = filter_for_cloud({"variables": state.variables}).get("variables", [])
    return {
        "ok": True,
        "filename": state.dataset_meta.get("filename", "") if state.dataset_meta else "",
        "row_count": state.dataset_meta.get("row_count", 0) if state.dataset_meta else 0,
        "variables": cloud_vars,
    }


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_analyze
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_analyze(
    ctx: Context,
    query: str,
    confirm_greylist: bool = False,
) -> dict:
    """Execute statistical analysis from a natural-language query.

    Orchestrates the full pipeline: intent recognition → method selection →
    syntax generation → validation → execution → parsing → explanation.

    Args:
        query: Natural-language description (e.g. "比较男女成绩差异")
        confirm_greylist: Set to true to confirm a pending greylist operation
            (COMPUTE/RECODE/SELECT IF).  Only valid when the previous
            snla_analyze call returned requires_confirmation=true.

    Returns:
        On success: {ok, method, result: {tables, statistics}, explanation,
                     markdown_table}
        On greylist: {ok: false, requires_confirmation: true, greylist_warnings,
                      syntax_preview, message}
        On busy: structured error with ENGINE_BUSY code
    """
    state = _session_state(ctx)
    sid = ctx.session_id

    # ── Guard: engine busy ──────────────────────────────────────────
    if state._executing:
        return _engine_busy()

    # ── Guard: data required ────────────────────────────────────────
    if not state.variables:
        return _mk_error(
            "user", "请先上传数据文件", "NO_DATA", "使用 snla_upload 上传 .sav 或 .csv 文件后重试。"
        )

    if not query.strip():
        return _mk_error(
            "user", "请输入分析问题", "EMPTY_QUERY", "例如: '比较男女成绩差异' 或 '描述统计'。"
        )

    state._executing = True
    state._cancelled = False

    try:
        # ── Phase 1: Planning (via orchestrator) ──────────────────
        await ctx.report_progress(0, 5, "正在识别分析意图…")
        plan: PlanResult = planner.plan(
            sid,
            query,
            variables=state.variables,
            dataset_meta=state.dataset_meta,
            last_analysis=state.last_analysis,
        )
        method = plan.method
        gvar = plan.grouping_variable
        tvar = plan.test_variable

        # ── P5-4: simple_regression gate ──────────────────────────
        if method == "simple_regression":
            from snla.config import check_spss_available

            if not check_spss_available():
                state._executing = False
                return _mk_error(
                    "user",
                    "回归分析当前仅在连接 SPSS 时可用。"
                    "你可以：(1) 在本地安装 SNLA 桌面版连接 SPSS，"
                    "(2) 使用相关分析（Pearson/Spearman）作为替代。",
                    "METHOD_UNAVAILABLE",
                    "建议改用 pearson_correlation 或 spearman_correlation。",
                )

        # ── Python backend fast path ──────────────────────────────
        if STATS_BACKEND == "python":
            state._executing = False  # Python is non-blocking
            return await _execute_python_backend(
                ctx, state, method, plan.plan_explanation, gvar, tvar, query
            )

        # ── Syntax generation (template-based) ────────────────────
        await ctx.report_progress(1, 5, "正在生成分析语法…")
        syntax = get_syntax_by_method(method, grouping_var=gvar, test_var=tvar)

        # ── Validation ────────────────────────────────────────────
        await ctx.report_progress(2, 5, "正在验证语法…")
        var_names = [v["name"] for v in state.variables]
        validation = validate_syntax(syntax, var_names)

        if not validation["valid"]:
            state._executing = False
            return _mk_error(
                "system",
                f"语法验证失败: {'; '.join(validation['errors'])}",
                "SYNTAX_INVALID",
                "请尝试更具体地描述分析需求。",
            )

        greylist_warnings = [
            w
            for w in validation.get("warnings", [])
            if "greylist" in w.lower() or "confirm" in w.lower()
        ]

        # ── Greylist gate ─────────────────────────────────────────
        if greylist_warnings and not confirm_greylist:
            planner.stage_greylist(
                sid,
                GreylistPending(
                    syntax=syntax,
                    warnings=greylist_warnings,
                    method=method,
                    user_input=query,
                ),
            )
            state._executing = False
            return {
                "ok": False,
                "requires_confirmation": True,
                "greylist_warnings": greylist_warnings,
                "syntax_preview": syntax[:200],
                "message": (
                    "此操作将修改数据（如 COMPUTE / RECODE / SELECT IF），"
                    "需要在临时副本上执行。请回复「确认」以继续。"
                ),
            }

        # ── Execute SPSS ──────────────────────────────────────────
        await ctx.report_progress(3, 5, "正在执行 SPSS 分析…")
        from snla.executor.spss import SPSSExecutor

        executor = SPSSExecutor()
        exec_result = executor.run(syntax)

        if state._cancelled:
            state._executing = False
            state._cancelled = False
            return {"ok": False, "cancelled": True}

        if not exec_result.get("success"):
            state._executing = False
            return _mk_error(
                "system",
                f"SPSS 执行失败: {exec_result.get('error', '未知错误')}",
                "EXECUTION_FAILED",
                "请检查数据文件是否正确，或尝试更简单的分析。",
            )

        # ── Parse + Explain ───────────────────────────────────────
        await ctx.report_progress(4, 5, "正在解读结果…")
        analysis_type = METHOD_TO_ANALYSIS.get(method, "UNKNOWN")
        parsed = parse_output(
            oms_xml_path=exec_result.get("xml_path"),
            lst_text=exec_result.get("lst_text"),
            analysis_type=analysis_type,
        )
        explanation = naturalize_explain(parsed)

        # ── Build response ────────────────────────────────────────
        state.last_analysis = {"method": method, "syntax": syntax}
        state.last_result = parsed
        state.last_explanation = explanation
        state.last_method = method
        state.last_query = query
        state._executing = False

        await ctx.report_progress(5, 5, "分析完成")
        return _format_response(
            method, plan.plan_explanation, syntax, parsed, explanation, greylist_warnings
        )

    except asyncio.CancelledError:
        state._executing = False
        state._cancelled = False
        return {"ok": False, "cancelled": True}
    except Exception as e:
        state._executing = False
        traceback.print_exc()
        return _mk_error("system", str(e), "INTERNAL_ERROR", "服务内部错误，请稍后重试。")
    finally:
        state._executing = False


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_confirm
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_confirm(ctx: Context) -> dict:
    """Confirm and execute a pending greylist operation.

    Call after snla_analyze returns requires_confirmation=true.
    Execution happens on a TEMPORARY COPY of the data — the original
    file is never modified.
    """
    state = _session_state(ctx)
    sid = ctx.session_id

    if state._executing:
        return _engine_busy()

    try:
        pg: GreylistPending = planner.pop_pending(sid)
    except NoPendingError:
        return _mk_error("user", "没有待确认的操作", "NO_PENDING", "当前没有等待确认的灰名单操作。")

    state._executing = True
    state._cancelled = False

    try:
        await ctx.report_progress(0, 4, "正在临时副本上执行…")

        syntax = pg.syntax
        method = pg.method

        # Execute on temp copy
        if state.file_path:
            from snla.executor.spss import SPSSExecutor

            executor = SPSSExecutor()
            exec_result = executor.execute_on_temp_copy(
                syntax=syntax,
                data_path=state.file_path,
            )
        else:
            from snla.executor.spss import SPSSExecutor

            executor = SPSSExecutor()
            exec_result = executor.run(syntax)

        if state._cancelled:
            state._executing = False
            state._cancelled = False
            return {"ok": False, "cancelled": True}

        if not exec_result.get("success"):
            state._executing = False
            return _mk_error(
                "system",
                f"执行失败: {exec_result.get('error', '未知错误')}",
                "EXECUTION_FAILED",
                None,
            )

        await ctx.report_progress(2, 4, "正在解读结果…")
        analysis_type = METHOD_TO_ANALYSIS.get(method, "UNKNOWN")
        parsed = parse_output(
            oms_xml_path=exec_result.get("xml_path"),
            lst_text=exec_result.get("lst_text"),
            analysis_type=analysis_type,
        )
        explanation = naturalize_explain(parsed)

        state.last_analysis = {"method": method, "syntax": syntax}
        state.last_result = parsed
        state.last_explanation = explanation
        state.last_method = method
        state.last_query = pg.user_input
        state._executing = False

        await ctx.report_progress(4, 4, "完成")

        result = _format_response(method, "", syntax, parsed, explanation, pg.warnings)
        result["temp_copy_note"] = "此操作已在数据的临时副本上执行，您的原始数据文件未被修改。"
        return result

    except asyncio.CancelledError:
        state._executing = False
        state._cancelled = False
        return {"ok": False, "cancelled": True}
    except Exception as e:
        state._executing = False
        traceback.print_exc()
        return _mk_error("system", str(e), "INTERNAL_ERROR", "服务内部错误，请稍后重试。")
    finally:
        state._executing = False


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_cancel
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_cancel(ctx: Context) -> dict:
    """Cancel the currently running analysis.

    Safe to call at any time — returns success even if nothing was running.
    Also clears any pending greylist operation.
    """
    state = _session_state(ctx)
    sid = ctx.session_id
    state._cancelled = True
    state._executing = False
    planner.cancel_pending(sid)
    return {"ok": True, "message": "已取消"}


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_export
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_export(ctx: Context) -> dict:
    """Export the last analysis result as a Word (.docx) report.

    Returns base64-encoded .docx file content.
    """
    state = _session_state(ctx)
    sid = ctx.session_id

    if not state.last_result:
        return _mk_error(
            "user", "没有可导出的分析结果", "NO_RESULT", "请先使用 snla_analyze 执行分析。"
        )

    try:
        output_path = _upload_dir / sid / f"snla_report_{sid[:8]}.docx"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        export_to_docx(
            output_path=str(output_path),
            user_query=state.last_query,
            method=state.last_method,
            analysis_result=state.last_result,
            explanation=state.last_explanation,
            data_file=state.file_path or "",
        )

        content = output_path.read_bytes()
        return {
            "ok": True,
            "filename": output_path.name,
            "size": len(content),
            "content_base64": base64.b64encode(content).decode(),
        }
    except Exception as e:
        return _mk_error("system", f"导出失败: {e}", "EXPORT_FAILED", "请稍后重试。")


# ═════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═════════════════════════════════════════════════════════════════════════


def _format_response(
    method: str,
    plan_explanation: str,
    syntax: str,
    parsed: Any,
    explanation: str,
    greylist_warnings: list[str],
) -> dict:
    """Build the unified response format (grill Q5)."""
    # Extract tables from parsed result
    tables = []
    if hasattr(parsed, "tables"):
        tables = [{"title": t.title, "rows": t.rows} for t in parsed.tables]
    elif isinstance(parsed, dict):
        tables = parsed.get("tables", [])

    # Build Markdown table representation
    md_parts = [f"## {method}\n"]
    for t in tables:
        md_parts.append(f"### {t.get('title', '')}")
        rows = t.get("rows", [])
        if rows:
            md_parts.append("| " + " | ".join(rows[0].keys()) + " |")
            md_parts.append("|" + "|".join(["---"] * len(rows[0])) + "|")
            for row in rows:
                md_parts.append("| " + " | ".join(str(v) for v in row.values()) + " |")
        md_parts.append("")
    if explanation:
        md_parts.append(f"\n{explanation}")

    statistics = {}
    if hasattr(parsed, "statistics"):
        statistics = parsed.statistics
    elif isinstance(parsed, dict):
        statistics = parsed.get("statistics", {})

    return {
        "ok": True,
        "method": method,
        "plan_explanation": plan_explanation,
        "syntax_used": syntax,
        "result": {
            "tables": tables,
            "statistics": statistics,
        },
        "explanation": explanation,
        "markdown": "\n".join(md_parts),
        "greylist_warnings": greylist_warnings,
    }


async def _execute_python_backend(
    ctx: Context,
    state: MCPState,
    method: str,
    plan_explanation: str,
    gvar: str | None,
    tvar: str | None,
    query: str,
) -> dict:
    """Execute analysis via Python/pingouin backend (fast path)."""
    await ctx.report_progress(2, 4, "正在通过 Python 引擎执行分析…")

    import pandas as pd

    from snla.executor.python import PythonStatsExecutor

    # Load dataframe
    file_path = state.file_path
    if not file_path or not os.path.isfile(file_path):
        return _mk_error("system", "数据文件丢失", "FILE_GONE")

    suffix = os.path.splitext(file_path)[1].lower()
    try:
        if suffix == ".sav":
            import pyreadstat

            df, _ = pyreadstat.read_sav(file_path)
        else:
            df = pd.read_csv(file_path)
    except Exception as e:
        return _mk_error("system", f"数据加载失败: {e}", "LOAD_ERROR")

    # Execute
    py_exec = PythonStatsExecutor()
    result = py_exec.execute(
        method, df, grouping_var=gvar, test_var=tvar, dep_var=gvar, indep_var=tvar
    )

    # P5-4: check trust
    if not is_method_trusted(method):
        response = _format_response(method, plan_explanation, "", result, "", [])
        response["warning"] = (
            f"Python 引擎下「{method}」方法的可靠性尚未经 SPSS 交叉验证。"
            f"以下为原始统计数字，非统计专业人士请谨慎解读。"
            f"建议安装 SPSS 以获得完整解读。"
        )
        response["limited_mode"] = True
        response["explanation"] = None
        state.last_analysis = {"method": method}
        return response

    # Trusted → full explanation
    explanation = naturalize_explain(result)
    state.last_analysis = {"method": method}
    state.last_result = result
    state.last_explanation = explanation
    state.last_method = method
    state.last_query = query

    await ctx.report_progress(4, 4, "分析完成")
    return _format_response(method, plan_explanation, "", result, explanation, [])


# ═════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        transport = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "stdio"
    else:
        transport = "stdio"
    mcp.run(transport=transport)
