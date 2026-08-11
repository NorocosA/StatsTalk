"""Production contracts for method applicability decisions."""

from __future__ import annotations

from snla.analysis import (
    AnalysisConfirmationRequest,
    AnalysisCorrectionRejected,
    AnalysisCorrectionRequired,
    AnalysisFailure,
    AnalysisRequest,
    AnalysisService,
    AnalysisSuccess,
)
from snla.analysis.applicability import evaluate_applicability
from snla.orchestrator import NoPendingError, PlanResult
from snla.parser.schema import AnalysisResult


class FixedPlanner:
    def __init__(self, plan: PlanResult) -> None:
        self.plan_result = plan

    def plan(self, *args, **kwargs) -> PlanResult:
        return self.plan_result

    def cancel_pending(self, session_id: str) -> None:
        return None

    def pop_pending(self, session_id: str):
        raise NoPendingError(session_id)


def _variables(*items):
    return [
        {
            "name": name,
            "type": variable_type,
            "value_labels": value_labels,
            "label": name,
        }
        for name, variable_type, value_labels in items
    ]


def test_continuous_grouping_stops_before_execution_and_offers_correlation(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("age,score\n20,70\n21,75\n22,80\n23,85\n", encoding="utf-8")
    variables = _variables(
        ("age", "Numeric", None),
        ("score", "Numeric", None),
    )
    planner = FixedPlanner(
        PlanResult(
            method="independent_t_test",
            plan_explanation="Compare two groups",
            grouping_variable="age",
            test_variable="score",
        )
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = AnalysisService(backend="python", analysis_planner=planner).analyze(
        AnalysisRequest(
            session_id="type-mismatch",
            query="Compare score by age group",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 4},
        )
    )

    assert isinstance(outcome, AnalysisCorrectionRequired)
    assert outcome.original_method == "independent_t_test"
    assert outcome.issues[0].code == "GROUPING_VARIABLE_NOT_CATEGORICAL"
    assert [choice.method for choice in outcome.corrections] == ["pearson_correlation"]
    assert calls == []


def test_three_observed_groups_stop_t_test_and_offer_anova(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text(
        "group,score\n1,70\n1,72\n2,80\n2,82\n3,90\n3,92\n",
        encoding="utf-8",
    )
    variables = _variables(
        ("group", "Numeric", {1: "A", 2: "B", 3: "C"}),
        ("score", "Numeric", None),
    )
    planner = FixedPlanner(
        PlanResult(
            method="independent_t_test",
            plan_explanation="Compare groups",
            grouping_variable="group",
            test_variable="score",
        )
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = AnalysisService(backend="python", analysis_planner=planner).analyze(
        AnalysisRequest(
            session_id="group-count",
            query="Compare the three groups",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 6},
        )
    )

    assert isinstance(outcome, AnalysisCorrectionRequired)
    assert outcome.issues[0].code == "GROUP_COUNT_TOO_HIGH"
    assert [choice.method for choice in outcome.corrections] == ["oneway_anova"]
    assert calls == []


def test_insufficient_complete_samples_stop_execution_and_offer_descriptives(tmp_path, monkeypatch):
    data_path = tmp_path / "small.csv"
    data_path.write_text("group,score\n1,70\n1,72\n2,80\n", encoding="utf-8")
    variables = _variables(
        ("group", "Numeric", {1: "A", 2: "B"}),
        ("score", "Numeric", None),
    )
    planner = FixedPlanner(
        PlanResult(
            method="independent_t_test",
            plan_explanation="Compare groups",
            grouping_variable="group",
            test_variable="score",
        )
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = AnalysisService(backend="python", analysis_planner=planner).analyze(
        AnalysisRequest(
            session_id="small-sample",
            query="Compare the groups",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 3},
        )
    )

    assert isinstance(outcome, AnalysisCorrectionRequired)
    assert {issue.code for issue in outcome.issues} == {
        "INSUFFICIENT_COMPLETE_SAMPLE",
        "GROUP_SAMPLE_TOO_SMALL",
    }
    assert [choice.method for choice in outcome.corrections] == ["descriptives"]
    assert calls == []


def test_missing_planned_variable_returns_actionable_failure_before_execution(
    tmp_path, monkeypatch
):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("group,score\n1,70\n1,72\n2,80\n2,82\n", encoding="utf-8")
    variables = _variables(
        ("group", "Numeric", {1: "A", 2: "B"}),
        ("score", "Numeric", None),
    )
    planner = FixedPlanner(
        PlanResult(
            method="independent_t_test",
            plan_explanation="Compare groups",
            grouping_variable="missing_group",
            test_variable="score",
        )
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = AnalysisService(backend="python", analysis_planner=planner).analyze(
        AnalysisRequest(
            session_id="missing-variable",
            query="Compare the groups",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 4},
        )
    )

    assert isinstance(outcome, AnalysisFailure)
    assert outcome.error.code == "METHOD_INAPPLICABLE"
    assert outcome.issues[0].code == "VARIABLE_NOT_FOUND"
    assert len(outcome.correction_choices) == 2
    assert calls == []


def test_categorical_outcome_stops_t_test_and_offers_chi_square(tmp_path, monkeypatch):
    data_path = tmp_path / "categories.csv"
    data_path.write_text("group,outcome\n1,A\n1,B\n2,A\n2,B\n", encoding="utf-8")
    variables = _variables(
        ("group", "Numeric", {1: "Control", 2: "Treatment"}),
        ("outcome", "String", None),
    )
    planner = FixedPlanner(
        PlanResult(
            method="independent_t_test",
            plan_explanation="Compare outcomes",
            grouping_variable="group",
            test_variable="outcome",
        )
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    outcome = AnalysisService(backend="python", analysis_planner=planner).analyze(
        AnalysisRequest(
            session_id="categorical-outcome",
            query="Compare categorical outcomes",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 4},
        )
    )

    assert isinstance(outcome, AnalysisCorrectionRequired)
    assert outcome.issues[0].code == "TEST_VARIABLE_NOT_CONTINUOUS"
    assert [choice.method for choice in outcome.corrections] == ["chi_square"]
    assert calls == []


def test_confirmed_method_correction_revalidates_and_then_executes(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text(
        "group,score\n1,70\n1,72\n2,80\n2,82\n3,90\n3,92\n",
        encoding="utf-8",
    )
    variables = _variables(
        ("group", "Numeric", {1: "A", 2: "B", 3: "C"}),
        ("score", "Numeric", None),
    )
    original_plan = PlanResult(
        method="independent_t_test",
        plan_explanation="Compare groups",
        grouping_variable="group",
        test_variable="score",
    )
    service = AnalysisService(backend="python", analysis_planner=FixedPlanner(original_plan))
    executed_methods = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda _self, method, *_args, **_kwargs: (
            executed_methods.append(method)
            or AnalysisResult(analysis_type="ANOVA", parser_used="python_pingouin")
        ),
    )
    monkeypatch.setattr("snla.analysis.service._explain_result", lambda *_args: ("Complete", None))
    request = AnalysisRequest(
        session_id="confirm-correction",
        query="Compare the three groups",
        variables=variables,
        dataset_meta={"file_path": str(data_path), "row_count": 6},
    )

    pending = service.analyze(request)
    assert isinstance(pending, AnalysisCorrectionRequired)
    assert executed_methods == []

    confirmed = service.confirm(
        AnalysisConfirmationRequest(
            session_id=request.session_id,
            variables=variables,
            dataset_meta=request.dataset_meta,
            decision="accept",
            correction_id="use_oneway_anova",
        )
    )

    assert isinstance(confirmed, AnalysisSuccess)
    assert confirmed.method == "oneway_anova"
    assert confirmed.audit.method == "oneway_anova"
    assert executed_methods == ["oneway_anova"]
    assert original_plan.method == "independent_t_test"


def test_rejected_method_correction_is_cleared_without_execution(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text(
        "group,score\n1,70\n1,72\n2,80\n2,82\n3,90\n3,92\n",
        encoding="utf-8",
    )
    variables = _variables(
        ("group", "Numeric", {1: "A", 2: "B", 3: "C"}),
        ("score", "Numeric", None),
    )
    service = AnalysisService(
        backend="python",
        analysis_planner=FixedPlanner(
            PlanResult(
                method="independent_t_test",
                plan_explanation="Compare groups",
                grouping_variable="group",
                test_variable="score",
            )
        ),
    )
    calls = []
    monkeypatch.setattr(
        "snla.executor.python.PythonStatsExecutor.execute",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    request = AnalysisRequest(
        session_id="reject-correction",
        query="Compare the three groups",
        variables=variables,
        dataset_meta={"file_path": str(data_path), "row_count": 6},
    )

    assert isinstance(service.analyze(request), AnalysisCorrectionRequired)
    rejected = service.confirm(
        AnalysisConfirmationRequest(
            session_id=request.session_id,
            variables=variables,
            dataset_meta=request.dataset_meta,
            decision="reject",
        )
    )
    replay = service.confirm(
        AnalysisConfirmationRequest(
            session_id=request.session_id,
            variables=variables,
            dataset_meta=request.dataset_meta,
            decision="accept",
            correction_id="use_oneway_anova",
        )
    )

    assert isinstance(rejected, AnalysisCorrectionRejected)
    assert rejected.original_method == "independent_t_test"
    assert isinstance(replay, AnalysisFailure)
    assert replay.error.code == "NO_PENDING"
    assert calls == []


def test_non_group_methods_validate_registered_variable_roles():
    import pandas as pd

    variables = _variables(
        ("before", "Numeric", None),
        ("after", "Numeric", None),
        ("category_a", "String", None),
        ("category_b", "Numeric", {1: "Yes", 2: "No"}),
    )
    data = pd.DataFrame(
        {
            "before": [1, 2, 3, 4],
            "after": [2, 3, 4, 5],
            "category_a": ["A", "A", "B", "B"],
            "category_b": [1, 2, 1, 2],
        }
    )

    for method in (
        "paired_t_test",
        "pearson_correlation",
        "spearman_correlation",
        "simple_regression",
    ):
        decision = evaluate_applicability(
            method,
            variables,
            data,
            grouping_variable="before",
            test_variable="after",
        )
        assert decision.valid is True

    chi_square = evaluate_applicability(
        "chi_square",
        variables,
        data,
        grouping_variable="category_a",
        test_variable="category_b",
    )
    invalid_correlation = evaluate_applicability(
        "pearson_correlation",
        variables,
        data,
        grouping_variable="category_a",
        test_variable="after",
    )
    invalid_descriptives = evaluate_applicability(
        "descriptives",
        variables,
        data,
        grouping_variable=None,
        test_variable="category_a",
    )

    assert chi_square.valid is True
    assert invalid_correlation.valid is False
    assert invalid_correlation.issues[0].code == "FIRST_VARIABLE_NOT_CONTINUOUS"
    assert invalid_descriptives.valid is False
    assert invalid_descriptives.issues[0].code == "VARIABLE_NOT_CONTINUOUS"


def test_two_role_method_rejects_using_the_same_variable_twice():
    import pandas as pd

    variables = _variables(("score", "Numeric", None))
    data = pd.DataFrame({"score": [1, 2, 3, 4]})

    decision = evaluate_applicability(
        "paired_t_test",
        variables,
        data,
        grouping_variable="score",
        test_variable="score",
    )

    assert decision.valid is False
    assert decision.issues[0].code == "VARIABLE_ROLES_NOT_DISTINCT"


def test_python_executor_receives_the_same_roles_that_were_validated(tmp_path, monkeypatch):
    data_path = tmp_path / "paired.csv"
    data_path.write_text(
        "before,after\n1,2\n2,3\n3,4\n4,5\n",
        encoding="utf-8",
    )
    variables = _variables(
        ("before", "Numeric", None),
        ("after", "Numeric", None),
    )
    service = AnalysisService(
        backend="python",
        analysis_planner=FixedPlanner(
            PlanResult(
                method="paired_t_test",
                plan_explanation="Compare paired scores",
                grouping_variable="before",
                test_variable="after",
            )
        ),
    )
    captured = {}

    def execute(_self, method, _data, **kwargs):
        captured.update(method=method, **kwargs)
        return AnalysisResult(analysis_type="T-TEST", parser_used="python_pingouin")

    monkeypatch.setattr("snla.executor.python.PythonStatsExecutor.execute", execute)
    monkeypatch.setattr("snla.analysis.service._explain_result", lambda *_args: ("Complete", None))

    outcome = service.analyze(
        AnalysisRequest(
            session_id="paired-bindings",
            query="Compare before and after",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 4},
        )
    )

    assert isinstance(outcome, AnalysisSuccess)
    assert captured["method"] == "paired_t_test"
    assert captured["var1"] == "before"
    assert captured["var2"] == "after"


def test_spss_syntax_validation_is_not_reached_when_method_is_inapplicable(tmp_path, monkeypatch):
    data_path = tmp_path / "scores.csv"
    data_path.write_text("age,score\n20,70\n21,75\n22,80\n23,85\n", encoding="utf-8")
    variables = _variables(("age", "Numeric", None), ("score", "Numeric", None))
    service = AnalysisService(
        backend="spss",
        analysis_planner=FixedPlanner(
            PlanResult(
                method="independent_t_test",
                plan_explanation="Compare groups",
                grouping_variable="age",
                test_variable="score",
            )
        ),
    )
    syntax_calls = []
    monkeypatch.setattr(
        "snla.analysis.service._build_syntax",
        lambda *args, **kwargs: syntax_calls.append((args, kwargs)),
    )

    outcome = service.analyze(
        AnalysisRequest(
            session_id="validation-order",
            query="Compare score by age",
            variables=variables,
            dataset_meta={"file_path": str(data_path), "row_count": 4},
        )
    )

    assert isinstance(outcome, AnalysisCorrectionRequired)
    assert syntax_calls == []


def test_chi_square_requires_two_observed_levels_in_each_role():
    import pandas as pd

    variables = _variables(
        ("row", "String", None),
        ("column", "Numeric", {1: "Yes", 2: "No"}),
    )
    data = pd.DataFrame({"row": ["A"] * 4, "column": [1, 2, 1, 2]})

    decision = evaluate_applicability(
        "chi_square",
        variables,
        data,
        grouping_variable="row",
        test_variable="column",
    )

    assert decision.valid is False
    assert decision.issues[0].code == "CATEGORY_COUNT_TOO_LOW"
