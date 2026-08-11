from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path


class Context:
    session_id = "mcp-opt-in"

    async def info(self, *args, **kwargs):
        return None

    async def report_progress(self, *args, **kwargs):
        return None


def test_mcp_is_disabled_by_default_in_distributed_configuration():
    root = Path(__file__).parents[2]
    config_source = (root / "snla" / "config.py").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")

    assert 'os.getenv("MCP_ENABLED", "false")' in config_source
    assert "MCP_ENABLED=false" in example


def test_direct_mcp_entrypoint_exits_cleanly_while_disabled():
    root = Path(__file__).parents[2]
    environment = os.environ.copy()
    environment["MCP_ENABLED"] = "false"

    completed = subprocess.run(
        [sys.executable, str(root / "snla" / "mcp_server.py")],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 2
    assert "MCP is disabled" in completed.stdout
    assert completed.stderr == ""


def test_disabled_mcp_blocks_every_data_or_analysis_tool_before_side_effects(monkeypatch):
    from snla import config, mcp_server

    monkeypatch.setattr(config, "MCP_ENABLED", False)
    mcp_server._session_states.clear()
    calls = [
        mcp_server.snla_upload(Context(), r"C:\private\missing.sav"),
        mcp_server.snla_select_worksheet(Context(), "Data"),
        mcp_server.snla_variables(Context()),
        mcp_server.snla_analyze(Context(), "describe score"),
        mcp_server.snla_confirm(Context()),
        mcp_server.snla_cancel(Context()),
        mcp_server.snla_export(Context()),
    ]

    results = [asyncio.run(call) for call in calls]

    assert all(result["ok"] is False for result in results)
    assert {result["error"]["code"] for result in results} == {"MCP_DISABLED"}
    assert mcp_server._session_states == {}


def test_status_reports_disabled_without_exposing_session_state(monkeypatch):
    from snla import config, mcp_server

    monkeypatch.setattr(config, "MCP_ENABLED", False)
    mcp_server._session_states.clear()
    mcp_server._session_states[Context.session_id] = mcp_server.MCPState(
        variables=[{"name": "private_name", "type": "String"}],
        dataset_meta={"filename": "private.sav"},
    )

    result = asyncio.run(mcp_server.snla_status(Context()))

    assert result["ok"] is True
    assert result["enabled"] is False
    assert result["experimental"] is True
    assert result["has_data"] is False
    assert result["variable_count"] == 0
    assert result["filename"] == ""


def test_enabled_mcp_runs_shared_upload_analysis_export_and_cancel(tmp_path, monkeypatch):
    from snla import config, mcp_server

    source = tmp_path / "scores.csv"
    source.write_text("score\n1\n2\n3\n", encoding="utf-8")
    monkeypatch.setattr(config, "MCP_ENABLED", True)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(mcp_server, "STATS_BACKEND", "python")
    monkeypatch.setattr(mcp_server, "_upload_dir", tmp_path / "mcp")
    mcp_server._session_states.clear()

    status = asyncio.run(mcp_server.snla_status(Context()))
    upload = asyncio.run(mcp_server.snla_upload(Context(), str(source)))
    variables = asyncio.run(mcp_server.snla_variables(Context()))
    analysis = asyncio.run(
        mcp_server.snla_analyze(
            Context(),
            "describe score",
            method="descriptives",
            test_variable="score",
        )
    )
    exported = asyncio.run(mcp_server.snla_export(Context(), format="json"))
    cancelled = asyncio.run(mcp_server.snla_cancel(Context()))

    assert status["enabled"] is True
    assert upload["ok"] is True
    assert variables["variables"][0]["name"] == "score"
    assert analysis["ok"] is True
    assert json.loads(base64.b64decode(exported["content_base64"]))["capability"] == (
        "descriptives"
    )
    assert cancelled["ok"] is True
