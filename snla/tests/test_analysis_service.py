"""Contract tests for the protocol-neutral analysis service."""

from __future__ import annotations

import threading

from snla.analysis import (
    AnalysisCancelled,
    AnalysisConfirmationRequest,
    AnalysisConfirmationRequired,
    AnalysisFailure,
    AnalysisRequest,
    AnalysisService,
    AnalysisSuccess,
)
from snla.executor.spss import ExecutionResult
from snla.orchestrator import GreylistPending, PlanResult, planner
from snla.parser.schema import AnalysisResult


def test_python_analysis_returns_typed_success_with_audit_metadata(tmp_path, sample_variables):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("gender,score,class,age\n1,80,A,20\n2,90,B,21\n", encoding="utf-8")
    request = AnalysisRequest(
        session_id="contract",
        query="描述统计",
        variables=sample_variables,
        dataset_meta={"file_path": str(data_path), "row_count": 2},
    )

    outcome = AnalysisService(backend="python").analyze(request)

    assert isinstance(outcome, AnalysisSuccess)
    assert outcome.method == "descriptives"
    assert outcome.backend == "python"
    assert outcome.result.analysis_type == "DESCRIPTIVES"
    assert outcome.audit.method == "descriptives"
    assert outcome.audit.preferred_backend == "python"
    assert outcome.audit.effective_backend == "python"
    assert outcome.audit.parser_used == "python_pingouin"
    assert outcome.user_query == "描述统计"


def test_empty_query_returns_a_typed_structured_error(sample_variables):
    outcome = AnalysisService(backend="python").analyze(
        AnalysisRequest(
            session_id="contract",
            query="   ",
            variables=sample_variables,
            dataset_meta={"file_path": "unused.csv"},
        )
    )

    assert isinstance(outcome, AnalysisFailure)
    assert outcome.error.category == "user"
    assert outcome.error.code == "EMPTY_QUERY"
    assert outcome.error.suggestion
    assert outcome.http_status == 400
    assert outcome.audit.preferred_backend == "python"


def test_missing_dataset_returns_no_data_error():
    outcome = AnalysisService(backend="python").analyze(
        AnalysisRequest(
            session_id="contract",
            query="描述统计",
            variables=[],
            dataset_meta={},
        )
    )

    assert isinstance(outcome, AnalysisFailure)
    assert outcome.error.code == "NO_DATA"
    assert outcome.error.category == "user"
    assert outcome.http_status == 400


def test_spss_analysis_validates_executes_parses_and_audits(
    tmp_path, sample_variables, monkeypatch
):
    data_path = tmp_path / "scores.sav"
    data_path.write_bytes(b"test fixture placeholder")

    class FakeExecutor:
        def __init__(self):
            self.syntax = ""
            self.data_path = ""

        def run(self, syntax, data_path, cancellation_token=False):
            self.syntax = syntax
            self.data_path = data_path
            return ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                xml_path="result.xml",
                lst_path=None,
                success=True,
            )

    executor = FakeExecutor()
    parsed = AnalysisResult(
        analysis_type="DESCRIPTIVES",
        statistics={"mean": 85.0, "n_valid": 2},
        n_valid=2,
        parser_used="oms_xml",
    )
    monkeypatch.setattr("snla.executor.spss.SPSSExecutor", lambda: executor)
    monkeypatch.setattr("snla.parser.output.parse", lambda **kwargs: parsed)

    outcome = AnalysisService(backend="spss").analyze(
        AnalysisRequest(
            session_id="contract",
            query="描述统计",
            variables=sample_variables,
            dataset_meta={"file_path": str(data_path), "row_count": 2},
        )
    )

    assert isinstance(outcome, AnalysisSuccess)
    assert "DESCRIPTIVES VARIABLES=score" in executor.syntax
    assert executor.data_path == str(data_path)
    assert outcome.result is parsed
    assert outcome.backend == "spss"
    assert outcome.audit.effective_backend == "spss"
    assert outcome.audit.parser_used == "oms_xml"


def test_greylist_validation_returns_typed_confirmation_and_stages_context(
    tmp_path, sample_variables, monkeypatch
):
    data_path = tmp_path / "scores.sav"
    data_path.write_bytes(b"test fixture placeholder")
    planner.cancel_pending("greylist-contract")
    monkeypatch.setattr(
        "snla.analysis.service._build_syntax", lambda *args: "RECODE gender (1=0)(2=1)."
    )

    outcome = AnalysisService(backend="spss").analyze(
        AnalysisRequest(
            session_id="greylist-contract",
            query="描述统计",
            variables=sample_variables,
            dataset_meta={"file_path": str(data_path)},
        )
    )

    assert isinstance(outcome, AnalysisConfirmationRequired)
    assert outcome.requires_confirmation is True
    assert outcome.syntax == "RECODE gender (1=0)(2=1)."
    assert outcome.greylist_warnings
    assert planner.has_pending("greylist-contract")


def test_confirm_executes_pending_greylist_on_a_temporary_copy(
    tmp_path, sample_variables, monkeypatch
):
    data_path = tmp_path / "scores.sav"
    data_path.write_bytes(b"test fixture placeholder")
    planner.stage_greylist(
        "confirm-contract",
        GreylistPending(
            syntax="RECODE gender (1=0)(2=1).",
            warnings=["Greylist command requires confirmation: RECODE"],
            method="descriptives",
            user_input="重新编码后描述统计",
        ),
    )

    class FakeExecutor:
        def __init__(self):
            self.used_temp_copy = False

        def execute_on_temp_copy(self, syntax, data_path, cancellation_token=False):
            self.used_temp_copy = True
            return ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                xml_path="result.xml",
                lst_path=None,
                success=True,
            )

    executor = FakeExecutor()
    parsed = AnalysisResult(analysis_type="DESCRIPTIVES", parser_used="oms_xml")
    monkeypatch.setattr("snla.executor.spss.SPSSExecutor", lambda: executor)
    monkeypatch.setattr("snla.parser.output.parse", lambda **kwargs: parsed)

    outcome = AnalysisService(backend="spss").confirm(
        AnalysisConfirmationRequest(
            session_id="confirm-contract",
            variables=sample_variables,
            dataset_meta={"file_path": str(data_path)},
        )
    )

    assert isinstance(outcome, AnalysisSuccess)
    assert executor.used_temp_copy is True
    assert outcome.temp_copy is True
    assert outcome.syntax == "RECODE gender (1=0)(2=1)."
    assert outcome.user_query == "重新编码后描述统计"
    assert not planner.has_pending("confirm-contract")


def test_cancel_terminates_the_active_executor_and_returns_typed_cancelled(
    tmp_path, sample_variables, monkeypatch
):
    data_path = tmp_path / "scores.sav"
    data_path.write_bytes(b"test fixture placeholder")

    class BlockingExecutor:
        def __init__(self):
            self.started = threading.Event()
            self.terminated = threading.Event()

        def run(self, syntax, data_path, cancellation_token=False):
            self.started.set()
            assert self.terminated.wait(timeout=5)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="",
                xml_path=None,
                lst_path=None,
                success=False,
                error_message="terminated",
            )

        def terminate(self):
            self.terminated.set()

    executor = BlockingExecutor()
    monkeypatch.setattr("snla.executor.spss.SPSSExecutor", lambda: executor)
    service = AnalysisService(backend="spss")
    captured = []

    thread = threading.Thread(
        target=lambda: captured.append(
            service.analyze(
                AnalysisRequest(
                    session_id="cancel-contract",
                    query="描述统计",
                    variables=sample_variables,
                    dataset_meta={"file_path": str(data_path)},
                )
            )
        )
    )
    thread.start()
    assert executor.started.wait(timeout=5)

    assert service.cancel("cancel-contract") is True
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert executor.terminated.is_set()
    assert isinstance(captured[0], AnalysisCancelled)
    assert captured[0].to_payload()["cancelled"] is True


def test_different_sessions_share_one_global_execution_gate(
    tmp_path, sample_variables, monkeypatch
):
    data_path = tmp_path / "scores.sav"
    data_path.write_bytes(b"test fixture placeholder")

    class BlockingExecutor:
        def __init__(self):
            self.started = threading.Event()
            self.terminated = threading.Event()

        def run(self, syntax, data_path, cancellation_token=False):
            self.started.set()
            assert self.terminated.wait(timeout=5)
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr="",
                xml_path=None,
                lst_path=None,
                success=False,
                error_message="terminated",
            )

        def terminate(self):
            self.terminated.set()

    class FailingExecutor:
        def run(self, syntax, data_path, cancellation_token=False):
            return ExecutionResult(
                exit_code=1,
                stdout="",
                stderr="second executor should not run",
                xml_path=None,
                lst_path=None,
                success=False,
                error_message="second executor should not run",
            )

    first_executor = BlockingExecutor()
    executors = iter((first_executor, FailingExecutor()))
    monkeypatch.setattr("snla.executor.spss.SPSSExecutor", lambda: next(executors))
    service = AnalysisService(backend="spss")
    monkeypatch.setattr(
        service._planner,
        "plan",
        lambda *args, **kwargs: PlanResult(
            method="descriptives",
            plan_explanation="test plan",
            grouping_variable=None,
            test_variable="score",
        ),
    )
    captured = []

    first_thread = threading.Thread(
        target=lambda: captured.append(
            service.analyze(
                AnalysisRequest(
                    session_id="session-a",
                    query="描述统计",
                    variables=sample_variables,
                    dataset_meta={"file_path": str(data_path)},
                )
            )
        )
    )
    first_thread.start()
    assert first_executor.started.wait(timeout=5)

    try:
        second = service.analyze(
            AnalysisRequest(
                session_id="session-b",
                query="描述统计",
                variables=sample_variables,
                dataset_meta={"file_path": str(data_path)},
            )
        )
    finally:
        service.cancel("session-a")
        first_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert isinstance(second, AnalysisFailure)
    assert second.error.code == "ENGINE_BUSY"
    assert second.http_status == 409
