"""No-key local journey contracts from upload through Word export."""

from __future__ import annotations

import io
import socket

from snla.analysis import AnalysisRequest, AnalysisService, AnalysisSuccess
from snla.explainer.naturalize import explain
from snla.parser.schema import AnalysisResult
from snla.ui.security import loopback_security


class NetworkForbidden(RuntimeError):
    pass


class PlannerMustNotRun:
    def plan(self, *args, **kwargs):
        raise AssertionError("explicit local controls must bypass LLM planning")

    def cancel_pending(self, session_id: str) -> None:
        return None


def test_explicit_local_selection_bypasses_planner_but_uses_analysis_service(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("score\n70\n80\n90\n", encoding="utf-8")
    monkeypatch.setattr(
        "snla.analysis.service._explain_result", lambda *args, **kwargs: ("Done", None)
    )

    outcome = AnalysisService(backend="python", analysis_planner=PlannerMustNotRun()).analyze(
        AnalysisRequest(
            session_id="explicit-local",
            query="Describe score",
            variables=[{"name": "score", "type": "Numeric", "value_labels": None}],
            dataset_meta={"file_path": str(data_path), "row_count": 3},
            method="descriptives",
            test_variable="score",
            alpha=0.01,
            selection_source="user_selection",
        )
    )

    assert isinstance(outcome, AnalysisSuccess)
    assert outcome.method == "descriptives"
    assert outcome.parameters == {"alpha": 0.01}
    assert outcome.selection_source == "user_selection"


def test_deterministic_explainer_respects_alpha_and_includes_interpretation_warning():
    result = AnalysisResult(
        analysis_type="CORRELATIONS",
        statistics={"r": 0.42, "p_value": 0.02, "n_valid": 40},
        parser_used="python_pingouin",
    )

    explanation = explain(result, use_llm_polish=False, alpha=0.01)

    assert "r=0.420" in explanation
    assert "p=0.020>0.01" in explanation
    assert "未发现统计学上的显著差异/关系" in explanation
    assert "不等于因果关系" in explanation


def test_no_key_text_request_is_labeled_local_and_never_enters_rag(tmp_path, monkeypatch):
    from snla import config

    data_path = tmp_path / "scores.csv"
    data_path.write_text("score\n70\n80\n90\n", encoding="utf-8")
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MOCK", False)
    monkeypatch.setattr(
        "snla.rag.integration.get_syntax_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("no-key planning must not enter RAG")
        ),
    )
    monkeypatch.setattr("snla.analysis.service._explain_result", lambda *_args: ("Done", None))

    outcome = AnalysisService(backend="python").analyze(
        AnalysisRequest(
            session_id="local-label",
            query="Describe score",
            variables=[{"name": "score", "type": "Numeric", "value_labels": None}],
            dataset_meta={"file_path": str(data_path), "row_count": 3},
        )
    )

    assert isinstance(outcome, AnalysisSuccess)
    assert outcome.selection_source == "local_suggestion"
    assert outcome.plan_explanation.startswith("本地建议：")


def test_no_key_journey_uses_no_network_and_exports_word_report(tmp_path, monkeypatch):
    from snla import config
    from snla.data.retention import DatasetRetention
    from snla.ui import server

    def forbid_network(*args, **kwargs):
        raise NetworkForbidden("network access is forbidden in the no-key journey")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    monkeypatch.setattr("snla.llm.client.LLMClient.chat", forbid_network)
    monkeypatch.setattr("snla.rag.integration.get_syntax_context", forbid_network)
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_MOCK", False)
    monkeypatch.setattr(config, "STATS_BACKEND", "python")
    monkeypatch.setattr(server, "_check_rate_limit", lambda: False)
    monkeypatch.setattr(
        server,
        "dataset_retention",
        DatasetRetention(
            reference_path=tmp_path / "restore.bin",
            workspace_root=tmp_path / "workspaces",
            provider=object(),
            restore_enabled=lambda: False,
        ),
    )
    server.session.reset()
    server.app.config["TESTING"] = True

    with server.app.test_client() as client:
        bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
        bootstrap = client.post("/api/bootstrap", json={"bootstrap_token": bootstrap_token})
        client.environ_base["HTTP_AUTHORIZATION"] = (
            f"Bearer {bootstrap.get_json()['session_token']}"
        )
        upload = client.post(
            "/api/upload",
            data={
                "file": (
                    io.BytesIO(b"score\n70\n80\n90\n"),
                    "scores.csv",
                    "text/csv",
                )
            },
            content_type="multipart/form-data",
        )
        suggestion = client.post(
            "/api/suggest",
            json={"text": "Describe score"},
        )
        analysis = client.post(
            "/api/analyze",
            json={
                "text": "Describe score",
                "method": "descriptives",
                "test_variable": "score",
                "alpha": 0.05,
                "selection_source": "local_suggestion",
            },
        )
        export = client.get("/api/export")

    assert upload.status_code == 200
    assert suggestion.status_code == 200
    assert suggestion.get_json() == {
        "ok": True,
        "source": "local_suggestion",
        "method": "descriptives",
        "grouping_variable": None,
        "test_variable": "score",
        "label": "本地建议",
    }
    assert analysis.status_code == 200
    assert analysis.get_json()["ok"] is True
    assert analysis.get_json()["selection_source"] == "local_suggestion"
    assert "均值为80.0" in analysis.get_json()["explanation"]
    assert "描述统计只概括当前样本" in analysis.get_json()["explanation"]
    assert export.status_code == 200
    assert export.mimetype == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert export.data.startswith(b"PK")
    from docx import Document

    report_text = "\n".join(
        paragraph.text for paragraph in Document(io.BytesIO(export.data)).paragraphs
    )
    assert "显著性水平: α = 0.05" in report_text
    assert "描述统计只概括当前样本" in report_text
