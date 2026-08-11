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
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from snla.ui.security import loopback_security
from snla.ui.server import app, planner, session

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def client():
    """Flask test client configured for testing."""
    app.config["TESTING"] = True
    with app.test_client() as c:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = c.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        c.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {bootstrap.get_json()['session_token']}"
        yield c


@pytest.fixture(autouse=True)
def reset_global_state(tmp_path, monkeypatch):
    """Reset all module-level state between tests.

    Clears:
      - Concurrency guards (_executing, _active_executor, _was_cancelled)
      - SessionState (variables, history, metadata)
      - Planner pending greylist
    """
    import snla.ui.server as srv
    from snla import config
    from snla.data.retention import DatasetRetention

    class FakeProvider:
        def protect(self, plaintext):
            return b"protected:" + plaintext[::-1]

        def unprotect(self, ciphertext):
            return ciphertext.removeprefix(b"protected:")[::-1]

    monkeypatch.setattr(config, "SESSION_RESTORE_ENABLED", False)
    monkeypatch.setattr(
        srv,
        "dataset_retention",
        DatasetRetention(
            reference_path=tmp_path / "restore" / "restore_reference.bin",
            workspace_root=tmp_path / "workspaces",
            provider=FakeProvider(),
            restore_enabled=lambda: config.SESSION_RESTORE_ENABLED,
        ),
    )
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
    with patch("snla.config.LLM_MOCK", True), patch("snla.ui.server.LLM_MOCK", True):
        yield


@pytest.fixture(autouse=True)
def mock_save_env():
    """Prevent _save_env_file from writing to the real .env file."""
    with patch("snla.ui.server._save_env_file"):
        yield


@pytest.fixture(autouse=True)
def mock_spss_executor_factory():
    """Keep API tests independent from a locally installed SPSS runtime."""
    with patch("snla.executor.spss.SPSSExecutor", return_value=MagicMock()) as factory:
        yield factory


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
            "filename": "test_data.sav",
            "file_path": str(Path(__file__).parents[2] / "data" / "fixtures" / "test_data.sav"),
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

    def test_status_exposes_the_public_capability_contract(self, client):
        resp = client.get("/api/status")

        assert resp.status_code == 200
        capabilities = resp.get_json()["capabilities"]
        assert len(capabilities) == 11
        assert [item["name"] for item in capabilities].count("pearson_correlation") == 1

        pearson = next(item for item in capabilities if item["name"] == "pearson_correlation")
        assert pearson["aliases"] == ["correlations"]
        assert pearson["requirements"]["variable_roles"] == [
            "first_variable",
            "second_variable",
        ]

        regression = next(item for item in capabilities if item["name"] == "simple_regression")
        assert regression["backends"]["python"] == {
            "supported": True,
            "validated": True,
        }


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
        assert data["error"]["code"] == "NO_DATA"

    def test_analyze_empty_input(self, client):
        """Empty text → 400 'Empty input'."""
        resp = client.post("/api/analyze", json={"text": ""})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["error"]["code"] == "EMPTY_QUERY"

    def test_analyze_concurrent(self, client, sample_variables, monkeypatch):
        """A service-level busy result is mapped to HTTP 409."""
        from snla.analysis import AnalysisAudit, AnalysisError, AnalysisFailure
        from snla.ui.server import analysis_service

        _setup_session_with_data(sample_variables)
        monkeypatch.setattr(
            analysis_service,
            "analyze",
            lambda request: AnalysisFailure(
                error=AnalysisError("system", "当前已有分析正在执行。", "ENGINE_BUSY"),
                audit=AnalysisAudit("busy", "start", "end", None, "python", None, None),
                http_status=409,
            ),
        )

        resp = client.post("/api/analyze", json={"text": "比较差异"})
        assert resp.status_code == 409
        data = json.loads(resp.data)
        assert data["error"]["code"] == "ENGINE_BUSY"

    def test_analyze_success(self, client, sample_variables):
        """Happy path: plan → prepare syntax → execute → explain → 200."""
        _setup_session_with_data(sample_variables)

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
        assert data["explanation"]
        assert data["last_analysis"]["method"] == "independent_t_test"
        assert data["audit"]["effective_backend"] == "python"

    def test_analyze_plan_explanation(self, client, sample_variables):
        """Verify plan_explanation is returned in analyze response."""
        _setup_session_with_data(sample_variables)

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
        assert data["filename"] == "test_data.sav"
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
        for key in (
            "LLM_ENDPOINT",
            "LLM_MODEL",
            "AI_POLISH_ENABLED",
            "AI_POLISH_FIELDS",
            "SESSION_RESTORE_ENABLED",
            "MCP_ENABLED",
            "SPSS_PATH",
            "STATS_BACKEND",
        ):
            assert key in data

        assert isinstance(data["AI_POLISH_FIELDS"], list)
        assert "analysis_type" in data["AI_POLISH_FIELDS"]
        assert "raw_data" not in data["AI_POLISH_FIELDS"]

    def test_settings_can_explicitly_enable_and_disable_mcp(self, client, monkeypatch):
        import snla.config as cfg
        from snla.ui import server

        monkeypatch.setattr(cfg, "MCP_ENABLED", False)
        monkeypatch.setattr(server, "_save_env_file", lambda: None)

        enabled = client.post("/api/settings", json={"MCP_ENABLED": True})
        disabled = client.post("/api/settings", json={"MCP_ENABLED": False})

        assert enabled.status_code == 200
        assert "MCP_ENABLED" in enabled.get_json()["changed"]
        assert disabled.status_code == 200
        assert cfg.MCP_ENABLED is False

    def test_settings_can_explicitly_disable_ai_polish(self, client, monkeypatch):
        import snla.config as cfg
        from snla.ui import server

        monkeypatch.setattr(cfg, "AI_POLISH_ENABLED", True)
        monkeypatch.setattr(server, "_save_env_file", lambda: None)

        resp = client.post("/api/settings", json={"AI_POLISH_ENABLED": False})

        assert resp.status_code == 200
        assert cfg.AI_POLISH_ENABLED is False
        assert "AI_POLISH_ENABLED" in resp.get_json()["changed"]

    def test_disabling_session_restore_clears_the_encrypted_reference(self, client, monkeypatch):
        import snla.config as cfg
        from snla.ui import server

        calls = []
        monkeypatch.setattr(cfg, "SESSION_RESTORE_ENABLED", True)
        monkeypatch.setattr(server.dataset_retention, "forget", lambda: calls.append("forgotten"))
        monkeypatch.setattr(server, "_save_env_file", lambda: None)

        response = client.post("/api/settings", json={"SESSION_RESTORE_ENABLED": False})

        assert response.status_code == 200
        assert cfg.SESSION_RESTORE_ENABLED is False
        assert calls == ["forgotten"]

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

    def test_settings_updates_spss_executable_without_changing_backend(self, client):
        import snla.config as cfg

        original_path = cfg.SPSS_EXECUTABLE
        original_backend = cfg.STATS_BACKEND
        try:
            resp = client.post("/api/settings", json={"SPSS_PATH": r"C:\Stats\stats.com"})

            assert resp.status_code == 200
            assert cfg.SPSS_EXECUTABLE == r"C:\Stats\stats.com"
            assert original_backend == cfg.STATS_BACKEND
            assert "SPSS_PATH" in resp.get_json()["changed"]
        finally:
            cfg.SPSS_EXECUTABLE = original_path

    def test_settings_rejects_public_plain_http_endpoint(self, client):
        import snla.config as cfg

        original_endpoint = cfg.LLM_ENDPOINT
        resp = client.post(
            "/api/settings",
            json={"LLM_ENDPOINT": "http://api.example.com/v1/chat/completions"},
        )

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "public_http_forbidden"
        assert original_endpoint == cfg.LLM_ENDPOINT

    def test_settings_persists_the_validated_endpoint_value(self, client):
        import snla.config as cfg

        original_endpoint = cfg.LLM_ENDPOINT
        try:
            resp = client.post(
                "/api/settings",
                json={"LLM_ENDPOINT": "  http://127.0.0.1:11434/v1/chat  "},
            )

            assert resp.status_code == 200
            assert cfg.LLM_ENDPOINT == "http://127.0.0.1:11434/v1/chat"
        finally:
            cfg.LLM_ENDPOINT = original_endpoint


def test_restore_candidate_requires_explicit_http_consent(client, tmp_path, monkeypatch):
    import snla.config as cfg
    from snla.data.retention import DatasetRetention
    from snla.ui import server

    class FakeProvider:
        def protect(self, plaintext):
            return b"protected:" + plaintext[::-1]

        def unprotect(self, ciphertext):
            return ciphertext.removeprefix(b"protected:")[::-1]

    source = tmp_path / "scores.csv"
    source.write_text("score\n70\n80\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "SESSION_RESTORE_ENABLED", True)
    retention = DatasetRetention(
        reference_path=tmp_path / "restore" / "restore_reference.bin",
        workspace_root=tmp_path / "workspaces",
        provider=FakeProvider(),
        restore_enabled=lambda: cfg.SESSION_RESTORE_ENABLED,
    )
    retention.remember(source)
    monkeypatch.setattr(server, "dataset_retention", retention)
    server.session.reset()

    status = client.get("/api/status").get_json()
    declined = client.post("/api/restore-dataset", json={"consent": False}).get_json()

    assert status["has_data"] is False
    assert status["restore"] == {
        "state": "pending",
        "available": True,
        "filename": "scores.csv",
        "format": "csv",
    }
    assert declined == {"ok": True, "restored": False}
    assert server.session.has_data is False

    restored = client.post("/api/restore-dataset", json={"consent": True}).get_json()

    assert restored["ok"] is True
    assert restored["restored"] is True
    assert restored["filename"] == "scores.csv"
    assert server.session.dataset_meta["file_path"] == str(source.resolve())


def test_browser_upload_is_ephemeral_and_normal_exit_clears_data_and_history(
    client, tmp_path, monkeypatch
):
    from snla.data.retention import DatasetRetention
    from snla.ui import server

    retention = DatasetRetention(
        reference_path=tmp_path / "restore" / "restore_reference.bin",
        workspace_root=tmp_path / "workspaces",
        provider=object(),
        restore_enabled=lambda: False,
    )
    monkeypatch.setattr(server, "dataset_retention", retention)
    server.session.reset()

    response = client.post(
        "/api/upload",
        data={"file": (io.BytesIO(b"score\n70\n80\n"), "scores.csv", "text/csv")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    working_file = Path(server.session.dataset_meta["file_path"])
    assert working_file.is_file()
    assert working_file.is_relative_to(retention.workspace_root)
    assert retention.restore_status()["state"] == "disabled"
    server.session.history.append({"role": "user", "content": "private question"})

    server.cleanup_runtime_data()

    assert not working_file.exists()
    assert server.session.history == []
    assert server.session.has_data is False


def test_native_local_open_can_opt_in_to_encrypted_restore(client, tmp_path, monkeypatch):
    import snla.config as cfg
    from snla.data.retention import DatasetRetention
    from snla.ui import server

    class FakeProvider:
        def protect(self, plaintext):
            return b"protected:" + plaintext[::-1]

        def unprotect(self, ciphertext):
            return ciphertext.removeprefix(b"protected:")[::-1]

    source = tmp_path / "private_scores.csv"
    source.write_text("score\n70\n80\n", encoding="utf-8")
    reference_path = tmp_path / "restore" / "restore_reference.bin"
    monkeypatch.setattr(cfg, "SESSION_RESTORE_ENABLED", True)
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=FakeProvider(),
        restore_enabled=lambda: cfg.SESSION_RESTORE_ENABLED,
    )
    monkeypatch.setattr(server, "dataset_retention", retention)
    server.session.reset()

    response = client.post("/api/open-local-dataset", json={"path": str(source)})

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert server.session.dataset_meta["file_path"] == str(source.resolve())
    assert reference_path.is_file()
    assert str(source).encode() not in reference_path.read_bytes()


def test_local_data_api_inspects_and_clears_dataset_artifacts(client, tmp_path, monkeypatch):
    from snla.data.retention import DatasetRetention
    from snla.ui import server

    retention = DatasetRetention(
        reference_path=tmp_path / "restore" / "restore_reference.bin",
        workspace_root=tmp_path / "workspaces",
        provider=object(),
        restore_enabled=lambda: False,
    )
    working_file = retention.allocate_upload("scores.csv")
    working_file.write_bytes(b"private")
    monkeypatch.setattr(server, "dataset_retention", retention)
    server.session.variables = [{"name": "score", "type": "Numeric"}]
    server.session.history = [{"role": "user", "content": "private question"}]

    report = client.get("/api/local-data").get_json()

    assert report["ok"] is True
    assert report["retained"]["working_copies"] == {"files": 1, "bytes": 7}
    assert report["current_session"] == {"has_data": True, "history_entries": 1}

    cleared = client.delete("/api/local-data")

    assert cleared.status_code == 200
    assert cleared.get_json() == {"ok": True}
    assert not working_file.exists()
    assert server.session.has_data is False
    assert server.session.history == []


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

    def test_export_json_returns_only_stored_allowlisted_record(self, client):
        record = {
            "schema_version": "1.0",
            "capability": "descriptives",
            "variables": [{"role": "variable", "name": "score"}],
            "parameters": {"alpha": 0.05},
            "versions": {"statstalk": "0.9.0-beta", "python": "3.12"},
            "warnings": [],
            "fallback": None,
            "summary": {"conclusion": "Mean is 3.0."},
            "advanced": {"tables": [], "syntax": "", "diagnostics": {}},
        }
        session.history = [
            {"role": "user", "content": "describe score"},
            {
                "role": "assistant",
                "content": "Mean is 3.0.",
                "analysis_record": record,
            },
        ]

        response = client.get("/api/export?format=json")

        assert response.status_code == 200
        assert response.mimetype == "application/json"
        assert response.get_json() == record
        assert "no-store" in response.headers["Cache-Control"]


def test_spss_detection_only_reports_stats_executables(tmp_path):
    from snla.ui.server import _find_spss_candidates

    version_dir = tmp_path / "Statistics" / "26"
    version_dir.mkdir(parents=True)
    executable = version_dir / "stats.com"
    executable.write_text("", encoding="ascii")
    python_dir = version_dir / "Python3"
    python_dir.mkdir()
    python_executable = python_dir / "python.exe"
    python_executable.write_text("", encoding="ascii")

    candidates = _find_spss_candidates([tmp_path / "Statistics"])

    assert candidates == [
        {
            "version": "26",
            "path": str(executable.resolve()),
            "python_path": str(python_executable.resolve()),
        }
    ]
    assert all("license" not in key.lower() for key in candidates[0])


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
        assert data["error"]["code"] == "NO_PENDING"


# ===========================================================================
# Greylist flow
# ===========================================================================


class TestGreylistFlow:
    """End-to-end greylist state machine: stage → requires_confirmation → confirm."""

    def test_analyze_greylist_triggered(self, client, sample_variables, monkeypatch):
        """Syntax with greylist warnings → requires_confirmation=true."""
        from snla.analysis import AnalysisAudit, AnalysisConfirmationRequired
        from snla.ui.server import analysis_service

        _setup_session_with_data(sample_variables)
        monkeypatch.setattr(
            analysis_service,
            "analyze",
            lambda request: AnalysisConfirmationRequired(
                syntax="COMPUTE newvar = score * 2.",
                greylist_warnings=("greylist: COMPUTE will modify data",),
                audit=AnalysisAudit(
                    "greylist", "start", "end", "descriptives", "spss", "spss", None
                ),
            ),
        )

        resp = client.post("/api/analyze", json={"text": "计算一个新变量"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data.get("requires_confirmation") is True
        assert "greylist_warnings" in data
        assert "syntax" in data

    def test_greylist_confirm_flow(self, client, sample_variables, monkeypatch):
        """Stage greylist → POST /api/confirm → execution succeeds."""
        from snla.analysis import AnalysisAudit, AnalysisSuccess
        from snla.parser.schema import AnalysisResult
        from snla.ui.server import analysis_service

        _setup_session_with_data(sample_variables)
        monkeypatch.setattr(
            analysis_service,
            "confirm",
            lambda request: AnalysisSuccess(
                user_query="计算新变量后做描述统计",
                method="descriptives",
                backend="spss",
                plan_explanation="",
                result=AnalysisResult(
                    analysis_type="DESCRIPTIVES",
                    statistics={"n_valid": 200},
                    parser_used="oms_xml",
                ),
                explanation="变量计算完成，描述统计结果如下",
                syntax="COMPUTE newvar = score * 2.",
                temp_copy=True,
                audit=AnalysisAudit(
                    "confirm", "start", "end", "descriptives", "spss", "spss", "oms_xml"
                ),
            ),
        )

        resp = client.post("/api/confirm")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "temp_copy_note" in data
        assert "result" in data
        assert "explanation" in data
        assert "last_analysis" in data
        assert session.history[-2]["content"] == "计算新变量后做描述统计"


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

    @patch("urllib.request.build_opener")
    def test_models_rejects_public_plain_http_before_network(self, build_opener, client):
        resp = client.post(
            "/api/models",
            json={
                "endpoint": "http://api.example.com/v1/chat/completions",
                "api_key": "sk-secret",
            },
        )

        assert resp.status_code == 400
        assert resp.get_json()["code"] == "public_http_forbidden"
        build_opener.assert_not_called()

    @patch("urllib.request.build_opener")
    def test_models_certificate_failure_returns_secret_safe_diagnostic(self, build_opener, client):
        import ssl
        import urllib.error

        build_opener.return_value.open.side_effect = urllib.error.URLError(
            ssl.SSLCertVerificationError("certificate failed sk-secret workspace=private")
        )
        resp = client.post(
            "/api/models",
            json={
                "endpoint": "https://api.example.com/v1/chat?workspace=private",
                "api_key": "sk-secret",
            },
        )

        assert resp.status_code == 502
        assert resp.get_json()["code"] == "tls_verification_failed"
        assert "system clock" in resp.get_json()["message"]
        assert b"sk-secret" not in resp.data
        assert b"workspace=private" not in resp.data

    @patch("urllib.request.build_opener")
    def test_models_public_https_uses_the_default_verified_ssl_context(self, build_opener, client):
        import ssl
        import urllib.request

        build_opener.return_value.open.return_value.read.return_value = (
            b'{"data": [{"id": "model-a"}]}'
        )
        resp = client.post(
            "/api/models",
            json={
                "endpoint": "https://api.example.com/v1/chat/completions",
                "api_key": "sk-secret",
            },
        )

        assert resp.status_code == 200
        assert resp.get_json()["models"] == ["model-a"]
        https_handler = next(
            handler
            for handler in build_opener.call_args.args
            if isinstance(handler, urllib.request.HTTPSHandler)
        )
        ssl_context = https_handler._context
        assert ssl_context.check_hostname is True
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED

    @patch("urllib.request.build_opener")
    def test_models_disable_automatic_redirects(self, build_opener, client):
        import urllib.request

        build_opener.return_value.open.return_value.read.return_value = (
            b'{"data": [{"id": "model-a"}]}'
        )

        resp = client.post(
            "/api/models",
            json={
                "endpoint": "https://api.example.com/v1/chat/completions",
                "api_key": "sk-secret",
            },
        )

        assert resp.status_code == 200
        redirect_handler = next(
            handler
            for handler in build_opener.call_args.args
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        )
        assert redirect_handler.redirect_request(None, None, 302, "", {}, "") is None


# ===========================================================================
# Edge cases / error handling
# ===========================================================================


class TestEdgeCases:
    """Miscellaneous edge cases and robustness checks."""

    def test_analyze_history_appended(self, client, sample_variables):
        """Successful analyze appends user + assistant messages to history."""
        _setup_session_with_data(sample_variables)

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

        with patch.object(
            planner,
            "plan",
            return_value=PlanResult(
                method="nonexistent_method",
                plan_explanation="Test unknown method",
                grouping_variable="gender",
                test_variable="score",
            ),
        ):
            resp = client.post("/api/analyze", json={"text": "测试未知方法"})
            assert resp.status_code == 422
            assert resp.get_json()["error"]["code"] == "METHOD_UNAVAILABLE"
