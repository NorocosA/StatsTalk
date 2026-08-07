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

import base64
import os
import shutil
import time
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
from snla.analysis import (
    AnalysisConfirmationRequest,
    AnalysisRequest,
    AnalysisSuccess,
    analysis_service,
)
from snla.capabilities import get_public_capabilities_payload
from snla.config import STATS_BACKEND
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud
from snla.explainer.export import export_to_docx
from snla.trust import get_trusted_methods, trust_loaded_from

# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

SESSION_TTL = 30 * 60  # 30 minutes — uploads/{session_id}/ cleanup
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB max upload


# ═════════════════════════════════════════════════════════════════════════
# Per-session state (in-memory; replaces Flask's global SessionState)
# ═════════════════════════════════════════════════════════════════════════


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
    last_backend: str = ""

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


mcp = FastMCP("StatsTalk", lifespan=server_lifespan)


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
        "capabilities": get_public_capabilities_payload(),
        "trusted_methods": get_trusted_methods(),
        "trust_source": trust_loaded_from(),
        "has_data": bool(state.variables),
        "variable_count": len(state.variables),
        "filename": state.dataset_meta.get("filename", "") if state.dataset_meta else "",
        "executing": analysis_service.is_active(ctx.session_id),
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
    method: str | None = None,
    grouping_variable: str | None = None,
    test_variable: str | None = None,
    alpha: float = 0.05,
) -> dict:
    """Execute statistical analysis from a natural-language query.

    Orchestrates the full pipeline: intent recognition → method selection →
    syntax generation → validation → execution → parsing → explanation.

    Args:
        query: Natural-language description (e.g. "比较男女成绩差异")
        confirm_greylist: Set to true to confirm a pending greylist operation
            (COMPUTE/RECODE/SELECT IF).  Only valid when the previous
            snla_analyze call returned requires_confirmation=true.
        method: Optional explicit method selected by the user.
        grouping_variable: First method-role variable when ``method`` is explicit.
        test_variable: Second method-role variable, or the single analysis variable.
        alpha: Significance level used by the deterministic explainer.

    Returns:
        On success: {ok, method, result: {tables, statistics}, explanation,
                     markdown_table}
        On greylist: {ok: false, requires_confirmation: true, greylist_warnings,
                      syntax_preview, message}
        On busy: structured error with ENGINE_BUSY code
    """
    state = _session_state(ctx)
    sid = ctx.session_id

    await ctx.report_progress(0, 2, "正在执行分析…")
    dataset_meta = dict(state.dataset_meta or {})
    if state.file_path:
        dataset_meta["file_path"] = state.file_path
    outcome = analysis_service.analyze(
        AnalysisRequest(
            session_id=sid,
            query=query,
            variables=state.variables,
            dataset_meta=dataset_meta,
            last_analysis=state.last_analysis,
            confirm_greylist=confirm_greylist,
            method=method,
            grouping_variable=grouping_variable,
            test_variable=test_variable,
            alpha=alpha,
            selection_source="user_selection" if method else "planner",
        )
    )
    payload = outcome.to_payload()
    if isinstance(outcome, AnalysisSuccess):
        state.last_analysis = payload["last_analysis"]
        state.last_result = outcome.result
        state.last_explanation = outcome.explanation or ""
        state.last_method = outcome.method
        state.last_query = outcome.user_query
        state.last_backend = outcome.backend
    await ctx.report_progress(2, 2, "分析完成")
    return payload


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_confirm
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_confirm(
    ctx: Context,
    decision: str = "accept",
    correction_id: str | None = None,
) -> dict:
    """Resolve a pending greylist operation or method correction.

    Call after snla_analyze returns requires_confirmation=true.
    Execution happens on a TEMPORARY COPY of the data — the original
    file is never modified.
    """
    state = _session_state(ctx)
    sid = ctx.session_id
    await ctx.report_progress(0, 2, "正在临时副本上执行…")
    dataset_meta = dict(state.dataset_meta or {})
    if state.file_path:
        dataset_meta["file_path"] = state.file_path
    outcome = analysis_service.confirm(
        AnalysisConfirmationRequest(
            session_id=sid,
            variables=state.variables,
            dataset_meta=dataset_meta,
            decision=decision,
            correction_id=correction_id,
        )
    )
    payload = outcome.to_payload()
    if isinstance(outcome, AnalysisSuccess):
        state.last_analysis = payload["last_analysis"]
        state.last_result = outcome.result
        state.last_explanation = outcome.explanation or ""
        state.last_method = outcome.method
        state.last_query = outcome.user_query
        state.last_backend = outcome.backend
    await ctx.report_progress(2, 2, "完成")
    return payload


# ═════════════════════════════════════════════════════════════════════════
# Tool: snla_cancel
# ═════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def snla_cancel(ctx: Context) -> dict:
    """Cancel the currently running analysis.

    Safe to call at any time — returns success even if nothing was running.
    Also clears any pending greylist operation.
    """
    sid = ctx.session_id
    analysis_service.cancel(sid)
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
            backend=state.last_backend,
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
