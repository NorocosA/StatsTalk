"""Flask API endpoint tests for server.py.

Tests cover 20 scenarios across all major endpoints:
  - /api/status (2)
  - /api/upload (3)
  - /api/analyze (5)
  - /api/cancel (1)
  - /api/variables (2)
  - /api/settings (2)
  - /api/export (1)
  - /api/confirm (1)
  - Greylist flow (2)
  - /api/models (1)

Uses Flask test client with mocked dependencies (no real SPSS/LLM).
"""

from __future__ import annotations

import io
import json
import tempfile
from unittest.mock import patch

import pytest

from snla.ui.server import app, session, planner


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def client():
    """Flask test client configured for testing."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_global_state():
    """Reset all module-level state between tests.

    Clears:
      - Concurrency guards (_executing, _active_executor, _was_cancelled)
      - SessionState (variables, history, metadata)
      - Planner pending greylist
    """
    import snla.ui.server as srv

    srv._executing = False
    srv._active_executor = None
    srv._was_cancelled = False
    session.reset()
    planner._pending.clear()
    yield


@pytest.fixture(autouse=True)
def mock_llm():
    """Prevent any real LLM calls by patching LLM_MOCK in both modules.

    server.py imports LLM_MOCK as a module-level name (copy by value),
    and planner.py imports it from snla.config directly.  Both must
    be patched to prevent accidental LLM calls during tests.
    """
    with patch("snla.config.LLM_MOCK", True), patch(
        "snla.ui.server.LLM_MOCK", True
    ):
        yield


@pytest.fixture(autouse=True)
def mock_save_env():
    """Prevent _save_env_file from writing to the real .env file."""
    with patch("snla.ui.server._save_env_file"):
        yield


# ===========================================================================
# Helpers
# ===========================================================================


def _setup_session_with_data(sample_variables, dataset_meta=None):
    """Populate session with sample data for tests that need it.

    Args:
        sample_variables: List of variable dicts (from conftest fixture).
        dataset_meta: Optional dict overriding default metadata.
    """
    import snla.ui.server as srv

    srv.session.variables = list(sample_variables)
    if dataset_meta:
        srv.session.dataset_meta = dict(dataset_meta)
    else:
        srv.session.dataset_meta = {
            "row_count": 200,
            "filename": "test.sav",
            "file_path": "/mock/path/test.sav",
        }


# ===========================================================================
# /api/status
# ===========================================================================


class TestStatusEndpoint:
    """GET /api/status — health check + dataset info."""

    def test_status_empty(self, client):
        """No data loaded → ok=true, variable_count=0."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert data["variable_count"] == 0

    def test_status_with_data(self, client, sample_variables):
        """Variables loaded → has_data=true, variable_count>0."""
        _setup_session_with_data(sample_variables)
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["has_data"] is True
        assert data["variable_count"] == len(sample_variables)
        assert data["executing"] is False


# ===========================================================================
# /api/upload
# ===========================================================================


class TestUploadEndpoint:
    """POST /api/upload — file upload + metadata extraction."""

    def test_upload_no_file(self, client):
        """No file in request → 400."""
        resp = client.post("/api/upload", data={})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_upload_invalid_extension(self, client):
        """Unsupported extension (.txt) → 400 with Chinese error."""
        data = {"file": (io.BytesIO(b"hello"), "test.txt")}
        resp = client.post("/api/upload", data=data)
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert "error" in body
        assert "不支持" in body["error"]

    @patch("snla.ui.server.read_and_extract")
    def test_upload_valid_csv(self, mock_read, client, sample_variables):
        """Valid .csv upload → 200, ok=true, returns variables."""
        mock_read.return_value = {
            "filename": "test.csv",
            "format": "csv",
            "row_count": 200,
            "column_count": 4,
            "file_path": "/fake/path/test.csv",
            "variables": sample_variables,
        }
        data = {"file": (io.BytesIO(b"a,b,c\n1,2,3"), "test.csv", "text/csv")}
        resp = client.post("/api/upload", data=data)
        assert resp.status_code == 200
        result = json.loads(resp.data)
        assert result["ok"] is True
        assert result["row_count"] == 200
        assert len(result["variables"]) == len(sample_variables)


# ===========================================================================
# /api/analyze
# ===========================================================================


class TestAnalyzeEndpoint:
    """POST /api/analyze — main analysis pipeline."""

    def test_analyze_no_data(self, client):
        """No session variables → 400 'upload first'."""
        resp = client.post("/api/analyze", json={"text": "test"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "upload" in data["error"].lower() or "data" in data["error"].lower()

    def test_analyze_empty_input(self, client):
        """Empty text → 400 'Empty input'."""
        resp = client.post("/api/analyze", json={"text": ""})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "empty" in data["error"].lower()

    def test_analyze_concurrent(self, client, sample_variables):
        """_executing=True → 409 'already running'."""
        import snla.ui.server as srv

        srv._executing = True
        _setup_session_with_data(sample_variables)

        resp = client.post("/api/analyze", json={"text": "比较差异"})
        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert "already running" in data["error"]

    @patch("snla.ui.server._run_python_backend", return_value=None)
    @patch("snla.ui.server._execute_and_parse")
    @patch("snla.ui.server._phase2_explain")
    def test_analyze_success(
        self, mock_explain, mock_exec_parse, mock_py, client, sample_variables
    ):
        """Happy path: plan → prepare syntax → execute → explain → 200."""
        _setup_session_with_data(sample_variables)
        mock_exec_parse.return_value = (
            {"success": True, "xml_path": None, "lst_text": "", "error": None},
            {
                "analysis_type": "T-TEST",
                "tables": [],
                "statistics": {"t_value": 2.34, "p_value": 0.021, "n_valid": 20},
            },
        )
        mock_explain.return_value = "这是一个显著的差异（t=2.34, p=0.021）"

        resp = client.post("/api/analyze", json={"text": "比较男女成绩差异"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "method" in data
        assert "syntax" in data
        assert "explanation" in data
        assert "result" in data
        assert "last_analysis" in data
        # Planner in MOCK mode detects "比较" → independent_t_test
        assert data["method"] == "independent_t_test"
        assert data["explanation"] == mock_explain.return_value
        assert data["last_analysis"]["method"] == "independent_t_test"

    @patch("snla.ui.server._run_python_backend", return_value=None)
    @patch("snla.ui.server._execute_and_parse")
    @patch("snla.ui.server._phase2_explain")
    def test_analyze_plan_explanation(
        self, mock_explain, mock_exec_parse, mock_py, client, sample_variables
    ):
        """Verify plan_explanation is returned in analyze response."""
        _setup_session_with_data(sample_variables)
        mock_exec_parse.return_value = (
            {"success": True, "xml_path": None, "lst_text": "", "error": None},
            {
                "analysis_type": "DESCRIPTIVES",
                "tables": [],
                "statistics": {"n_valid": 100},
            },
        )
        mock_explain.return_value = "描述统计分析结果"

        resp = client.post("/api/analyze", json={"text": "描述统计"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        # plan_explanation should be present (from mocked planner)
        assert "plan_explanation" in data
        assert data["plan_explanation"] != ""


# ===========================================================================
# /api/cancel
# ===========================================================================


class TestCancelEndpoint:
    """POST /api/cancel — cancel running analysis."""

    def test_cancel_idle(self, client):
        """Cancel when nothing is running → 200 ok=True (safe to call)."""
        resp = client.post("/api/cancel")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True


# ===========================================================================
# /api/variables
# ===========================================================================


class TestVariablesEndpoint:
    """GET /api/variables — return cloud-safe variable list."""

    def test_variables_empty(self, client):
        """No data loaded → empty variables list."""
        resp = client.get("/api/variables")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["variables"] == []
        assert data["row_count"] == 0

    def test_variables_with_data(self, client, sample_variables):
        """Variables loaded → returns filtered variable list with metadata."""
        _setup_session_with_data(sample_variables)
        resp = client.get("/api/variables")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert len(data["variables"]) == len(sample_variables)
        assert data["row_count"] == 200
        assert data["filename"] == "test.sav"
        # value_labels should be stripped by filter_for_cloud
        for var in data["variables"]:
            assert "value_labels" not in var


# ===========================================================================
# /api/settings
# ===========================================================================


class TestSettingsEndpoint:
    """GET/POST /api/settings — configuration management."""

    def test_settings_get(self, client):
        """GET returns current settings dict with expected keys."""
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        for key in ("LLM_ENDPOINT", "LLM_MODEL", "STATS_BACKEND"):
            assert key in data

    def test_settings_post(self, client):
        """POST updates settings and returns changed keys."""
        resp = client.post(
            "/api/settings", json={"LLM_MODEL": "test-model", "STATS_BACKEND": "python"}
        )
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "LLM_MODEL" in data["changed"]
        assert "STATS_BACKEND" in data["changed"]


# ===========================================================================
# /api/export
# ===========================================================================


class TestExportEndpoint:
    """GET /api/export — download Word report."""

    def test_export_no_history(self, client):
        """No analysis history → 400."""
        resp = client.get("/api/export")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data
        assert "analysis" in data["error"].lower() or "export" in data["error"].lower()


# ===========================================================================
# /api/confirm
# ===========================================================================


class TestConfirmEndpoint:
    """POST /api/confirm — confirm and execute greylist operation."""

    def test_confirm_no_pending(self, client):
        """No pending greylist → 400."""
        resp = client.post("/api/confirm")
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data
        assert "pending" in data["error"].lower() or "待确认" in data["error"]


# ===========================================================================
# Greylist flow
# ===========================================================================


class TestGreylistFlow:
    """End-to-end greylist state machine: stage → requires_confirmation → confirm."""

    @patch("snla.ui.server._prepare_syntax")
    @patch("snla.ui.server._run_python_backend", return_value=None)
    def test_analyze_greylist_triggered(
        self, mock_py, mock_prep, client, sample_variables
    ):
        """Syntax with greylist warnings → requires_confirmation=true."""
        _setup_session_with_data(sample_variables)
        mock_prep.return_value = {
            "_greylist": True,
            "syntax": "COMPUTE newvar = score * 2.",
            "greylist_warnings": ["greylist: COMPUTE will modify data"],
        }

        resp = client.post("/api/analyze", json={"text": "计算一个新变量"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get("requires_confirmation") is True
        assert "greylist_warnings" in data
        assert "syntax" in data

    @patch("snla.ui.server._make_executor")
    @patch("snla.ui.server._execute_and_parse")
    @patch("snla.ui.server._phase2_explain")
    def test_greylist_confirm_flow(
        self, mock_explain, mock_exec_parse, mock_make_exec, client, sample_variables
    ):
        """Stage greylist → POST /api/confirm → execution succeeds."""
        from unittest.mock import MagicMock
        from snla.orchestrator import GreylistPending

        # Mock executor so execute_on_temp_copy doesn't touch filesystem
        mock_exec = MagicMock()
        mock_exec.execute_on_temp_copy.return_value = MagicMock(
            success=True, exit_code=0, xml_path=None, error_message=None
        )
        mock_make_exec.return_value = mock_exec

        # Stage a pending greylist directly on the planner singleton
        planner.stage_greylist(
            "default",
            GreylistPending(
                syntax="COMPUTE newvar = score * 2.",
                warnings=["greylist: COMPUTE will modify data"],
                method="descriptives",
                user_input="计算一个新变量",
            ),
        )
        _setup_session_with_data(sample_variables)
        # Provide file_path so confirm endpoint attempts temp-copy execution
        import snla.ui.server as srv

        srv.session.dataset_meta["file_path"] = "/mock/path/test.sav"

        mock_exec_parse.return_value = (
            {
                "success": True,
                "xml_path": None,
                "lst_text": "",
                "error": None,
            },
            {
                "analysis_type": "DESCRIPTIVES",
                "tables": [{"title": "Descriptive Statistics", "rows": []}],
                "statistics": {"n_valid": 200},
            },
        )
        mock_explain.return_value = "变量计算完成，描述统计结果如下"

        resp = client.post("/api/confirm")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "temp_copy_note" in data
        assert "result" in data
        assert "explanation" in data
        assert "last_analysis" in data


# ===========================================================================
# /api/models
# ===========================================================================


class TestModelsEndpoint:
    """POST /api/models — fetch model list from LLM endpoint."""

    def test_models_missing_params(self, client):
        """Missing endpoint → 400."""
        resp = client.post("/api/models", json={"api_key": "test"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data

    def test_models_missing_api_key(self, client):
        """Missing api_key → 400."""
        resp = client.post("/api/models", json={"endpoint": "https://example.com"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert "error" in data


# ===========================================================================
# Edge cases / error handling
# ===========================================================================


class TestEdgeCases:
    """Miscellaneous edge cases and robustness checks."""

    @patch("snla.ui.server._run_python_backend", return_value=None)
    @patch("snla.ui.server._execute_and_parse")
    @patch("snla.ui.server._phase2_explain")
    def test_analyze_history_appended(
        self, mock_explain, mock_exec_parse, mock_py, client, sample_variables
    ):
        """Successful analyze appends user + assistant messages to history."""
        _setup_session_with_data(sample_variables)
        mock_exec_parse.return_value = (
            {"success": True, "xml_path": None, "lst_text": "", "error": None},
            {
                "analysis_type": "T-TEST",
                "tables": [],
                "statistics": {"t_value": 1.5, "p_value": 0.15},
            },
        )
        mock_explain.return_value = "无显著差异"

        assert len(session.history) == 0
        resp = client.post("/api/analyze", json={"text": "比较两组差异"})
        assert resp.status_code == 200
        # History should now have user + assistant entries
        assert len(session.history) == 2
        assert session.history[0]["role"] == "user"
        assert session.history[1]["role"] == "assistant"

    def test_analyze_non_method(self, client, sample_variables):
        """Planner returns a method that has no template → graceful 500 from _syntax_template."""
        _setup_session_with_data(sample_variables)
        # Patch planner.plan to return an unknown method
        from snla.orchestrator import PlanResult

        with patch.object(planner, "plan", return_value=PlanResult(
            method="nonexistent_method",
            plan_explanation="Test unknown method",
            grouping_variable="gender",
            test_variable="score",
        )), \
             patch("snla.ui.server._run_python_backend", return_value=None):
            resp = client.post("/api/analyze", json={"text": "测试未知方法"})
            # Should fail with 500 since _syntax_template calls get_syntax_by_method
            # which raises ValueError for unknown methods
            assert resp.status_code == 500
