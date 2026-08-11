"""Transparent SPSS fallback contracts."""

from __future__ import annotations

from snla.analysis import (
    AnalysisConfirmationRequest,
    AnalysisFailure,
    AnalysisRequest,
    AnalysisService,
    AnalysisSuccess,
)
from snla.orchestrator import GreylistPending, Planner
from snla.parser.schema import AnalysisResult


def _variables(*names: str) -> list[dict]:
    return [
        {"name": name, "type": "Numeric", "label": name, "value_labels": None} for name in names
    ]


def test_validated_method_falls_back_without_changing_preference_or_method(tmp_path, monkeypatch):
    from snla import config

    data_path = tmp_path / "scores.csv"
    data_path.write_text("score\n70\n80\n90\n", encoding="utf-8")
    monkeypatch.setattr(config, "STATS_BACKEND", "spss")
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda _self, method, *_args, **_kwargs: (
            calls.append(method)
            or AnalysisResult(
                analysis_type="DESCRIPTIVES",
                statistics={"mean": 80.0, "n_valid": 3},
                parser_used="python_pingouin",
            )
        ),
    )
    monkeypatch.setattr("snla.analysis.service._explain_result", lambda *_args: ("Done", None))
    service = AnalysisService(spss_available=lambda: False)
    request = AnalysisRequest(
        session_id="fallback-session",
        query="Describe score",
        variables=_variables("score"),
        dataset_meta={"file_path": str(data_path), "row_count": 3},
        method="descriptives",
        test_variable="score",
        selection_source="user_selection",
    )

    first = service.analyze(request)
    second = service.analyze(request)

    assert isinstance(first, AnalysisSuccess)
    assert first.method == "descriptives"
    assert first.backend == "python"
    assert first.audit.preferred_backend == "spss"
    assert first.audit.effective_backend == "python"
    assert first.fallback_reason == {
        "code": "SPSS_EXECUTABLE_NOT_FOUND",
        "message": "未检测到 SPSS，已自动使用 Python 引擎完成本次分析。",
        "method": "descriptives",
        "announce": True,
    }
    assert isinstance(second, AnalysisSuccess)
    assert second.fallback_reason["announce"] is False
    assert config.STATS_BACKEND == "spss"
    assert calls == ["descriptives", "descriptives"]


def test_unvalidated_python_method_stops_instead_of_falling_back(tmp_path, monkeypatch):
    data_path = tmp_path / "regression.csv"
    data_path.write_text("y,x\n1,1\n2,2\n3,3\n4,4\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr("snla.analysis.service.can_fallback_to_python", lambda _method: False)

    outcome = AnalysisService(backend="spss", spss_available=lambda: False).analyze(
        AnalysisRequest(
            session_id="unsupported-fallback",
            query="Regress y on x",
            variables=_variables("y", "x"),
            dataset_meta={"file_path": str(data_path), "row_count": 4},
            method="simple_regression",
            grouping_variable="y",
            test_variable="x",
            selection_source="user_selection",
        )
    )

    assert isinstance(outcome, AnalysisFailure)
    assert outcome.error.code == "SPSS_FALLBACK_UNAVAILABLE"
    assert outcome.audit.preferred_backend == "spss"
    assert outcome.audit.effective_backend is None
    assert outcome.to_payload()["preferred_backend"] == "spss"
    assert outcome.to_payload()["effective_backend"] is None
    assert outcome.to_payload()["fallback_reason"]["code"] == "SPSS_EXECUTABLE_NOT_FOUND"
    assert outcome.correction_choices == (
        "检查 SPSS 可执行文件路径后重试。",
        "选择已通过 Python 验证的其他统计方法。",
    )
    assert calls == []


def test_spss_availability_is_rechecked_for_each_analysis(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("score\n70\n80\n90\n", encoding="utf-8")
    available = {"value": False}
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *_args, **_kwargs: AnalysisResult(
            analysis_type="DESCRIPTIVES", parser_used="python_pingouin"
        ),
    )
    monkeypatch.setattr("snla.analysis.service._explain_result", lambda *_args: ("Done", None))
    service = AnalysisService(backend="spss", spss_available=lambda: available["value"])
    request = AnalysisRequest(
        session_id="restore-session",
        query="Describe score",
        variables=_variables("score"),
        dataset_meta={"file_path": str(data_path), "row_count": 3},
        method="descriptives",
        test_variable="score",
    )

    fallback = service.analyze(request)
    available["value"] = True

    class FakeSPSSExecutor:
        def run(self, **kwargs):
            return {"success": True, "xml_path": "result.xml"}

    monkeypatch.setattr("snla.executor.spss.SPSSExecutor", FakeSPSSExecutor)
    monkeypatch.setattr(
        "snla.analysis.service._parse_execution",
        lambda *_args: AnalysisResult(analysis_type="DESCRIPTIVES", parser_used="oms_xml"),
    )
    restored = service.analyze(request)

    assert isinstance(fallback, AnalysisSuccess)
    assert isinstance(restored, AnalysisSuccess)
    assert restored.backend == "spss"
    assert restored.backend_restored is True
    assert restored.audit.preferred_backend == "spss"
    assert restored.audit.effective_backend == "spss"


def test_pending_spss_mutation_stops_if_spss_disappears(tmp_path, monkeypatch):
    planner = Planner()
    planner.stage_greylist(
        "pending-fallback",
        GreylistPending(
            syntax="RECODE score (1=2).",
            warnings=["Greylist command requires confirmation: RECODE"],
            method="descriptives",
            user_input="Recode and describe score",
        ),
    )
    monkeypatch.setattr(
        "snla.executor.spss.SPSSExecutor",
        lambda: (_ for _ in ()).throw(AssertionError("SPSS must not start")),
    )

    outcome = AnalysisService(
        backend="spss", analysis_planner=planner, spss_available=lambda: False
    ).confirm(
        AnalysisConfirmationRequest(
            session_id="pending-fallback",
            variables=_variables("score"),
            dataset_meta={"file_path": str(tmp_path / "scores.sav")},
        )
    )

    assert isinstance(outcome, AnalysisFailure)
    assert outcome.error.code == "SPSS_FALLBACK_UNAVAILABLE"
    assert outcome.audit.effective_backend is None
