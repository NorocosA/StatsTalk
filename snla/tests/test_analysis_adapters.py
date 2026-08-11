"""End-to-end contracts shared by the HTTP and MCP adapters."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from snla.analysis import AnalysisAudit, AnalysisCorrectionRejected, AnalysisSuccess
from snla.orchestrator import planner
from snla.parser.schema import AnalysisResult
from snla.ui.security import loopback_security


class _MCPContext:
    session_id = "mcp-contract"

    async def report_progress(self, *args, **kwargs):
        return None

    async def info(self, *args, **kwargs):
        return None


def test_http_and_mcp_planning_payloads_enforce_cloud_privacy(tmp_path, monkeypatch):
    from snla import config, mcp_server
    from snla.llm.client import LLMClient
    from snla.ui import server

    data_path = tmp_path / "private_scores.csv"
    data_path.write_text("patient_id,score\nP001,80\nP002,90\n", encoding="utf-8")
    variables = [
        {
            "name": "patient_id",
            "type": "String",
            "label": "Patient Identifier",
            "value_labels": {"P001": "Alice"},
            "raw_values": ["P001", "P002"],
        },
        {"name": "score", "type": "Numeric", "label": "Score"},
    ]
    dataset_meta = {
        "file_path": str(data_path),
        "filename": data_path.name,
        "row_count": 2,
        "column_count": 2,
        "raw_data": [["P001", 80]],
        "aggregate_stats": {"private_metric": 12345},
    }
    captured_messages = []

    def capture_chat(self, messages, **kwargs):
        captured_messages.append(messages)
        return {
            "content": json.dumps(
                {
                    "method": "descriptives",
                    "plan_explanation": "Use score while protecting var_01",
                    "grouping_variable": None,
                    "test_variable": "score",
                }
            )
        }

    monkeypatch.setattr(config, "LLM_MOCK", False)
    monkeypatch.setattr(config, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "AI_POLISH_ENABLED", False)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(mcp_server, "STATS_BACKEND", "python")
    monkeypatch.setattr(LLMClient, "chat", capture_chat)
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)

    server.session.reset()
    server.session.variables = list(variables)
    server.session.dataset_meta = dict(dataset_meta)
    server._executing = False
    mcp_server._session_states.clear()
    mcp_server._session_states[_MCPContext.session_id] = mcp_server.MCPState(
        variables=list(variables),
        dataset_meta=dict(dataset_meta),
        file_path=str(data_path),
    )
    planner._pending.clear()

    query = "Describe score for patient_id Patient Identifier"
    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        http_payload = client.post("/api/analyze", json={"text": query}).get_json()

    mcp_payload = asyncio.run(mcp_server.snla_analyze(_MCPContext(), query))

    assert http_payload["ok"] is True
    assert mcp_payload["ok"] is True
    assert len(captured_messages) == 2
    for messages in captured_messages:
        payload = json.dumps(messages, ensure_ascii=False)
        for forbidden in (
            "patient_id",
            "Patient Identifier",
            "P001",
            "P002",
            "Alice",
            data_path.name,
            str(data_path),
            "private_metric",
            "12345",
        ):
            assert forbidden not in payload
        assert "var_01" in payload
        assert "Sensitive variable 01" in payload
        assert "2" in payload
    assert http_payload["plan_explanation"] == "Use score while protecting patient_id"
    assert mcp_payload["plan_explanation"] == "Use score while protecting patient_id"


def test_mcp_restores_sensitive_aliases_for_explicit_method_inputs(monkeypatch):
    from snla import mcp_server

    captured = []

    class FakeOutcome:
        def to_payload(self):
            return {"ok": False, "error": {"code": "CAPTURED"}}

    def capture_request(request):
        captured.append(request)
        return FakeOutcome()

    monkeypatch.setattr(mcp_server.analysis_service, "analyze", capture_request)
    mcp_server._session_states.clear()
    mcp_server._session_states[_MCPContext.session_id] = mcp_server.MCPState(
        variables=[
            {"name": "patient_id", "type": "String", "label": "Patient Identifier"},
            {"name": "score", "type": "Numeric", "label": "Score"},
        ],
        dataset_meta={"file_path": "scores.csv"},
        file_path="scores.csv",
    )

    asyncio.run(
        mcp_server.snla_analyze(
            _MCPContext(),
            "compare groups",
            method="independent_t_test",
            grouping_variable="var_01",
            test_variable="score",
        )
    )

    assert len(captured) == 1
    assert captured[0].grouping_variable == "patient_id"
    assert captured[0].test_variable == "score"


def _policy_view(payload):
    return {
        "method": payload["method"],
        "backend": payload["backend"],
        "statistics": payload["result"]["statistics"],
        "explanation": payload["explanation"],
        "limited_mode": payload.get("limited_mode", False),
        "audit": {
            "method": payload["audit"]["method"],
            "preferred_backend": payload["audit"]["preferred_backend"],
            "effective_backend": payload["audit"]["effective_backend"],
            "parser_used": payload["audit"]["parser_used"],
        },
    }


def test_equivalent_http_and_mcp_requests_are_policy_equivalent(
    tmp_path, sample_variables, monkeypatch
):
    from snla import config, mcp_server
    from snla.ui import server

    data_path = tmp_path / "scores.csv"
    data_path.write_text("gender,score,class,age\n1,80,A,20\n2,90,B,21\n", encoding="utf-8")
    dataset_meta = {"file_path": str(data_path), "filename": data_path.name, "row_count": 2}

    monkeypatch.setattr(config, "LLM_MOCK", True)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(mcp_server, "STATS_BACKEND", "python")
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)

    server.session.reset()
    server.session.variables = list(sample_variables)
    server.session.dataset_meta = dict(dataset_meta)
    server._executing = False

    mcp_server._session_states.clear()
    mcp_server._session_states[_MCPContext.session_id] = mcp_server.MCPState(
        variables=list(sample_variables),
        dataset_meta=dict(dataset_meta),
        file_path=str(data_path),
    )
    planner._pending.clear()

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        http_payload = client.post("/api/analyze", json={"text": "描述统计"}).get_json()

    mcp_payload = asyncio.run(mcp_server.snla_analyze(_MCPContext(), "描述统计"))

    assert _policy_view(http_payload) == _policy_view(mcp_payload)


def test_http_and_mcp_share_the_same_structured_no_data_error(monkeypatch):
    from snla import config, mcp_server
    from snla.ui import server

    monkeypatch.setattr(config, "LLM_MOCK", True)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)
    server.session.reset()
    server._executing = False
    mcp_server._session_states.clear()

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        http_response = client.post("/api/analyze", json={"text": "描述统计"})

    mcp_payload = asyncio.run(mcp_server.snla_analyze(_MCPContext(), "描述统计"))

    assert http_response.status_code == 400
    assert http_response.get_json()["error"] == mcp_payload["error"]
    assert mcp_payload["error"]["code"] == "NO_DATA"


def test_http_confirm_preserves_original_query_for_word_export(monkeypatch):
    from snla.ui import server

    query = "重新编码后描述统计"
    result = AnalysisResult(analysis_type="DESCRIPTIVES", parser_used="oms_xml")
    outcome = AnalysisSuccess(
        user_query=query,
        method="descriptives",
        backend="spss",
        plan_explanation="",
        result=result,
        explanation="分析完成",
        syntax="RECODE gender (1=0)(2=1).",
        temp_copy=True,
        audit=AnalysisAudit(
            request_id="confirm-http",
            started_at="2026-08-07T00:00:00+00:00",
            completed_at="2026-08-07T00:00:01+00:00",
            method="descriptives",
            preferred_backend="spss",
            effective_backend="spss",
            parser_used="oms_xml",
        ),
    )
    exported = {}

    def fake_export(*, output_path, **kwargs):
        exported.update(kwargs)
        Path(output_path).write_bytes(b"docx")

    monkeypatch.setattr(server.analysis_service, "confirm", lambda request: outcome)
    monkeypatch.setattr(server, "save_session", lambda session: None)
    monkeypatch.setattr("snla.explainer.export.export_to_docx", fake_export)
    server.session.reset()
    server.session.variables = [{"name": "gender", "type": "Numeric"}]
    server.session.dataset_meta = {"file_path": "scores.sav", "filename": "scores.sav"}

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        confirm_response = client.post("/api/confirm")
        export_response = client.get("/api/export")

    assert confirm_response.status_code == 200
    assert export_response.status_code == 200
    assert exported["user_query"] == query
    assert [item["role"] for item in server.session.history] == ["user", "assistant"]


def test_mcp_confirm_preserves_original_query_for_word_export(tmp_path, monkeypatch):
    from snla import mcp_server

    query = "重新编码后描述统计"
    result = AnalysisResult(analysis_type="DESCRIPTIVES", parser_used="oms_xml")
    outcome = AnalysisSuccess(
        user_query=query,
        method="descriptives",
        backend="spss",
        plan_explanation="",
        result=result,
        explanation="分析完成",
        syntax="RECODE gender (1=0)(2=1).",
        temp_copy=True,
        audit=AnalysisAudit(
            request_id="confirm-mcp",
            started_at="2026-08-07T00:00:00+00:00",
            completed_at="2026-08-07T00:00:01+00:00",
            method="descriptives",
            preferred_backend="spss",
            effective_backend="spss",
            parser_used="oms_xml",
        ),
    )
    exported = {}

    def fake_export(*, output_path, **kwargs):
        exported.update(kwargs)
        Path(output_path).write_bytes(b"docx")

    monkeypatch.setattr(mcp_server.analysis_service, "confirm", lambda request: outcome)
    monkeypatch.setattr(mcp_server, "export_to_docx", fake_export)
    monkeypatch.setattr(mcp_server, "_upload_dir", tmp_path)
    mcp_server._session_states.clear()
    state = mcp_server.MCPState(
        variables=[{"name": "gender", "type": "Numeric"}],
        dataset_meta={"file_path": "scores.sav", "filename": "scores.sav"},
        file_path="scores.sav",
    )
    mcp_server._session_states[_MCPContext.session_id] = state

    confirm_payload = asyncio.run(mcp_server.snla_confirm(_MCPContext()))
    export_payload = asyncio.run(mcp_server.snla_export(_MCPContext()))

    assert confirm_payload["ok"] is True
    assert export_payload["ok"] is True
    assert exported["user_query"] == query
    assert state.last_query == query


def test_http_and_mcp_forward_method_correction_decisions(monkeypatch):
    from snla import mcp_server
    from snla.ui import server

    captured = []

    def fake_confirm(request):
        captured.append(request)
        return AnalysisCorrectionRejected(
            original_method="independent_t_test",
            audit=AnalysisAudit(
                request_id="correction-adapter",
                started_at="2026-08-07T00:00:00+00:00",
                completed_at="2026-08-07T00:00:00+00:00",
                method="independent_t_test",
                preferred_backend="python",
                effective_backend=None,
                parser_used=None,
            ),
        )

    monkeypatch.setattr(server.analysis_service, "confirm", fake_confirm)
    monkeypatch.setattr(mcp_server.analysis_service, "confirm", fake_confirm)
    server.session.reset()
    mcp_server._session_states.clear()

    server.app.config["TESTING"] = True
    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        response = client.post(
            "/api/confirm",
            json={"decision": "reject", "correction_id": "use_oneway_anova"},
        )

    mcp_payload = asyncio.run(
        mcp_server.snla_confirm(
            _MCPContext(),
            decision="reject",
            correction_id="use_oneway_anova",
        )
    )

    assert response.status_code == 200
    assert response.get_json()["correction_rejected"] is True
    assert mcp_payload["correction_rejected"] is True
    assert [(item.decision, item.correction_id) for item in captured] == [
        ("reject", "use_oneway_anova"),
        ("reject", "use_oneway_anova"),
    ]
