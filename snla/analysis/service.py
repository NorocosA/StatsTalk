"""One application service for every StatsTalk analysis entry point."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from snla.capabilities import can_fallback_to_python, get_capability
from snla.explainer.record import build_analysis_record
from snla.orchestrator import (
    GreylistPending,
    NoPendingError,
    Planner,
    PlanResult,
    planner_instance,
)
from snla.parser.schema import AnalysisResult, analysis_result_to_dict

from .applicability import (
    ApplicabilityDecision,
    ApplicabilityIssue,
    CorrectionChoice,
    evaluate_applicability,
    resolve_role_bindings,
)

_ANALYSIS_TYPES = {
    "independent_t_test": "T-TEST",
    "paired_t_test": "T-TEST",
    "oneway_anova": "ANOVA",
    "simple_regression": "REGRESSION",
    "pearson_correlation": "CORRELATIONS",
    "spearman_correlation": "CORRELATIONS",
    "chi_square": "CROSSTABS",
    "frequencies": "FREQUENCIES",
    "descriptives": "DESCRIPTIVES",
    "mann_whitney_u": "T-TEST",
    "kruskal_wallis": "ANOVA",
}


@dataclass(frozen=True)
class AnalysisRequest:
    """Protocol-independent input required to run one analysis."""

    session_id: str
    query: str
    variables: list[dict[str, Any]]
    dataset_meta: dict[str, Any]
    last_analysis: dict[str, Any] | None = None
    confirm_greylist: bool = False
    method: str | None = None
    grouping_variable: str | None = None
    test_variable: str | None = None
    alpha: float = 0.05
    selection_source: str = "planner"


@dataclass(frozen=True)
class AnalysisConfirmationRequest:
    """Protocol-independent input for a pending greylist confirmation."""

    session_id: str
    variables: list[dict[str, Any]]
    dataset_meta: dict[str, Any]
    decision: str = "accept"
    correction_id: str | None = None


@dataclass(frozen=True)
class AnalysisAudit:
    """Non-sensitive facts describing how an analysis was produced."""

    request_id: str
    started_at: str
    completed_at: str
    method: str | None
    preferred_backend: str
    effective_backend: str | None
    parser_used: str | None
    fallback_reason: dict[str, Any] | None = None


@dataclass(frozen=True)
class AnalysisError:
    """Stable, protocol-independent analysis error details."""

    category: str
    user_message: str
    code: str
    suggestion: str | None = None


@dataclass(frozen=True)
class AnalysisFailure:
    """Typed failure outcome with an adapter-friendly HTTP status hint."""

    error: AnalysisError
    audit: AnalysisAudit
    http_status: int = 500
    issues: tuple[ApplicabilityIssue, ...] = ()
    correction_choices: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "ok": False,
            "error": asdict(self.error),
            "preferred_backend": self.audit.preferred_backend,
            "effective_backend": self.audit.effective_backend,
            "fallback_reason": self.audit.fallback_reason,
            "audit": asdict(self.audit),
        }
        if self.issues:
            payload["issues"] = [asdict(issue) for issue in self.issues]
        if self.correction_choices:
            payload["correction_choices"] = list(self.correction_choices)
        return payload


@dataclass(frozen=True)
class AnalysisConfirmationRequired:
    """A validated mutating operation waiting for explicit confirmation."""

    syntax: str
    greylist_warnings: tuple[str, ...]
    audit: AnalysisAudit
    requires_confirmation: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "requires_confirmation": True,
            "greylist_warnings": list(self.greylist_warnings),
            "syntax": self.syntax,
            "syntax_preview": self.syntax[:200],
            "message": "此操作会修改数据，将仅在临时副本上执行。请确认是否继续。",
            "audit": asdict(self.audit),
        }


@dataclass(frozen=True)
class AnalysisCorrectionRequired:
    """An inapplicable plan waiting for an explicit method correction."""

    original_method: str
    issues: tuple[ApplicabilityIssue, ...]
    corrections: tuple[CorrectionChoice, ...]
    audit: AnalysisAudit
    requires_confirmation: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "requires_confirmation": True,
            "confirmation_type": "method_correction",
            "original_method": self.original_method,
            "issues": [asdict(issue) for issue in self.issues],
            "correction_options": [asdict(choice) for choice in self.corrections],
            "message": self.issues[0].message,
            "audit": asdict(self.audit),
        }


@dataclass(frozen=True)
class AnalysisCorrectionRejected:
    """A proposed method change explicitly rejected by the user."""

    original_method: str
    audit: AnalysisAudit

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "correction_rejected": True,
            "original_method": self.original_method,
            "message": "已取消方法修正，原分析计划未执行。",
            "audit": asdict(self.audit),
        }


@dataclass(frozen=True)
class AnalysisCancelled:
    """Typed outcome for an analysis stopped by its caller."""

    audit: AnalysisAudit

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "cancelled": True,
            "audit": asdict(self.audit),
        }


@dataclass(frozen=True)
class AnalysisSuccess:
    """Typed successful outcome returned by :class:`AnalysisService`."""

    user_query: str
    method: str
    backend: str
    plan_explanation: str
    result: AnalysisResult
    explanation: str | None
    audit: AnalysisAudit
    ai_polish: str | None = None
    syntax: str = ""
    greylist_warnings: tuple[str, ...] = ()
    limited_mode: bool = False
    warning: str | None = None
    degradation: dict[str, Any] | None = None
    temp_copy: bool = False
    parameters: dict[str, Any] | None = None
    selection_source: str = "planner"
    fallback_reason: dict[str, Any] | None = None
    backend_restored: bool = False
    variable_roles: dict[str, str] | None = None
    applicability_warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        result = analysis_result_to_dict(self.result)
        analysis_record = build_analysis_record(
            capability=self.method,
            variable_roles=self.variable_roles or {},
            parameters=self.parameters or {},
            result=self.result,
            conclusion=self.explanation,
            syntax=self.syntax,
            preferred_backend=self.audit.preferred_backend,
            effective_backend=self.backend,
            fallback_reason=self.fallback_reason,
            warning=self.warning,
            greylist_warnings=self.greylist_warnings,
            audit=self.audit,
            selection_source=self.selection_source,
            applicability_warnings=self.applicability_warnings,
        )
        payload: dict[str, Any] = {
            "ok": True,
            "method": self.method,
            "backend": self.backend,
            "preferred_backend": self.audit.preferred_backend,
            "effective_backend": self.audit.effective_backend,
            "syntax": self.syntax,
            "syntax_used": self.syntax,
            "plan_explanation": self.plan_explanation,
            "greylist_warnings": list(self.greylist_warnings),
            "result": result,
            "explanation": self.explanation,
            "ai_polish": self.ai_polish,
            "authoritative_explanation": "explanation",
            "markdown": _render_markdown(self.method, result, self.explanation),
            "limited_mode": self.limited_mode,
            "parameters": self.parameters or {},
            "selection_source": self.selection_source,
            "degradation": self.degradation,
            "fallback_reason": self.fallback_reason,
            "backend_restored": self.backend_restored,
            "last_analysis": {
                "method": self.method,
                **({"syntax": self.syntax} if self.syntax else {}),
            },
            "audit": asdict(self.audit),
            "summary": analysis_record["summary"],
            "advanced": analysis_record["advanced"],
            "analysis_record": analysis_record,
        }
        if self.warning:
            payload["warning"] = self.warning
        if self.temp_copy:
            payload["temp_copy_note"] = "此操作已在数据的临时副本上执行，原始数据文件未被修改。"
        return payload


AnalysisOutcome = (
    AnalysisSuccess
    | AnalysisFailure
    | AnalysisConfirmationRequired
    | AnalysisCorrectionRequired
    | AnalysisCorrectionRejected
    | AnalysisCancelled
)


@dataclass
class _ActiveAnalysis:
    cancelled: Event
    executor: Any = None


@dataclass(frozen=True)
class _PendingCorrection:
    request: AnalysisRequest
    plan: PlanResult
    decision: ApplicabilityDecision


class AnalysisService:
    """Own the complete analysis policy behind a small typed interface."""

    def __init__(
        self,
        *,
        backend: str | None = None,
        analysis_planner: Planner = planner_instance,
        spss_available: Callable[[], bool] | None = None,
    ):
        self._backend = backend
        self._planner = analysis_planner
        self._spss_available = spss_available or _configured_spss_available
        self._active: dict[str, _ActiveAnalysis] = {}
        self._pending_corrections: dict[str, _PendingCorrection] = {}
        self._fallback_notified_sessions: set[str] = set()
        self._fallback_sessions: set[str] = set()
        self._active_lock = Lock()

    def analyze(self, request: AnalysisRequest) -> AnalysisOutcome:
        active = _ActiveAnalysis(cancelled=Event())
        with self._active_lock:
            if self._active:
                return _failure(
                    request_id=uuid4().hex,
                    started_at=_now(),
                    preferred_backend=self._backend or _configured_backend(),
                    category="system",
                    user_message="当前已有分析正在执行。",
                    code="ENGINE_BUSY",
                    suggestion="请等待当前分析完成后重试。",
                    http_status=409,
                )
            self._pending_corrections.pop(request.session_id, None)
            self._active[request.session_id] = active
        try:
            return self._analyze(request, active)
        finally:
            with self._active_lock:
                self._active.pop(request.session_id, None)

    def _analyze(
        self,
        request: AnalysisRequest,
        active: _ActiveAnalysis,
        *,
        plan_override: PlanResult | None = None,
    ) -> AnalysisOutcome:
        started_at = _now()
        preferred_backend = self._backend or _configured_backend()
        request_id = uuid4().hex
        if not request.query.strip():
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="请输入分析问题。",
                code="EMPTY_QUERY",
                suggestion="例如：比较男女成绩差异，或对成绩做描述统计。",
                http_status=400,
            )
        if not 0 < request.alpha < 1:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="显著性水平必须大于 0 且小于 1。",
                code="INVALID_ALPHA",
                suggestion="常用值为 0.05 或 0.01。",
                http_status=400,
            )
        if not request.variables or not request.dataset_meta.get("file_path"):
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="请先上传数据文件。",
                code="NO_DATA",
                suggestion="上传 .sav 或 .csv 文件后重试。",
                http_status=400,
            )
        if plan_override is not None:
            plan = plan_override
        elif request.method:
            plan = PlanResult(
                method=request.method,
                plan_explanation=(
                    "本地建议，已由用户确认。"
                    if request.selection_source == "local_suggestion"
                    else "用户通过结构化控件选择。"
                ),
                grouping_variable=request.grouping_variable,
                test_variable=request.test_variable,
            )
        else:
            try:
                plan = self._planner.plan(
                    request.session_id,
                    request.query,
                    variables=request.variables,
                    dataset_meta=request.dataset_meta,
                    last_analysis=request.last_analysis,
                )
            except Exception:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    category="system",
                    user_message="无法确定分析方法。",
                    code="PLANNING_FAILED",
                    suggestion="请更具体地描述分析目标和变量。",
                )
        selection_source = request.selection_source
        if selection_source == "planner" and plan.plan_explanation.startswith("本地建议："):
            selection_source = "local_suggestion"
        capability = get_capability(plan.method)
        if capability is None:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="当前版本不支持所请求的统计方法。",
                code="METHOD_UNAVAILABLE",
                suggestion="请从状态页列出的分析方法中选择。",
                http_status=422,
                method=plan.method,
            )
        effective_backend = preferred_backend
        fallback_reason = None
        if preferred_backend == "spss" and not self._spss_available():
            if not can_fallback_to_python(capability.name):
                choices = (
                    "检查 SPSS 可执行文件路径后重试。",
                    "选择已通过 Python 验证的其他统计方法。",
                )
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    category="configuration",
                    user_message=("当前未检测到 SPSS，且该方法尚未通过 Python 回退验证。"),
                    code="SPSS_FALLBACK_UNAVAILABLE",
                    suggestion=choices[0],
                    http_status=422,
                    method=capability.name,
                    correction_choices=choices,
                    fallback_reason={
                        "code": "SPSS_EXECUTABLE_NOT_FOUND",
                        "method": capability.name,
                    },
                )
            effective_backend = "python"
            fallback_reason = {
                "code": "SPSS_EXECUTABLE_NOT_FOUND",
                "message": "未检测到 SPSS，已自动使用 Python 引擎完成本次分析。",
                "method": capability.name,
                "announce": request.session_id not in self._fallback_notified_sessions,
            }
        backend_capability = capability.backend(effective_backend)
        if backend_capability is None or not backend_capability.supported:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="所选统计后端不支持该方法。",
                code="METHOD_UNAVAILABLE",
                suggestion="请选择该方法支持的统计后端。",
                http_status=422,
                method=capability.name,
            )
        try:
            data = _load_dataframe(request.dataset_meta["file_path"])
        except Exception:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                method=capability.name,
                category="system",
                user_message="The dataset could not be loaded for method validation.",
                code="DATA_LOAD_FAILED",
                suggestion="Re-import the dataset and try again.",
            )
        bindings = resolve_role_bindings(
            capability.name,
            grouping_variable=plan.grouping_variable,
            test_variable=plan.test_variable,
        )
        applicability = evaluate_applicability(
            capability.name,
            request.variables,
            data,
            grouping_variable=plan.grouping_variable,
            test_variable=plan.test_variable,
        )
        if not applicability.valid and applicability.corrections:
            with self._active_lock:
                self._pending_corrections[request.session_id] = _PendingCorrection(
                    request=request,
                    plan=plan,
                    decision=applicability,
                )
            return AnalysisCorrectionRequired(
                original_method=capability.name,
                issues=applicability.issues,
                corrections=applicability.corrections,
                audit=AnalysisAudit(
                    request_id=request_id,
                    started_at=started_at,
                    completed_at=_now(),
                    method=capability.name,
                    preferred_backend=preferred_backend,
                    effective_backend=None,
                    parser_used=None,
                ),
            )
        if not applicability.valid:
            choices = (
                "Choose existing variables for the required roles and try again.",
                "Choose another method compatible with the available variables.",
            )
            return AnalysisFailure(
                error=AnalysisError(
                    category="user",
                    user_message=applicability.issues[0].message,
                    code="METHOD_INAPPLICABLE",
                    suggestion=choices[0],
                ),
                audit=AnalysisAudit(
                    request_id=request_id,
                    started_at=started_at,
                    completed_at=_now(),
                    method=capability.name,
                    preferred_backend=preferred_backend,
                    effective_backend=None,
                    parser_used=None,
                ),
                http_status=422,
                issues=applicability.issues,
                correction_choices=choices,
            )
        syntax = ""
        temp_copy = False
        if effective_backend == "python":
            try:
                from snla.executor.python import PythonStatsExecutor

                result = PythonStatsExecutor().execute(
                    plan.method,
                    data,
                    grouping_var=bindings.get("grouping_variable") or bindings.get("row_variable"),
                    test_var=bindings.get("test_variable")
                    or bindings.get("column_variable")
                    or bindings.get("variable"),
                    dep_var=bindings.get("dependent_variable"),
                    indep_var=bindings.get("independent_variable"),
                    var1=bindings.get("first_variable"),
                    var2=bindings.get("second_variable"),
                )
                if active.cancelled.is_set():
                    return _cancelled(
                        request_id,
                        started_at,
                        capability.name,
                        preferred_backend,
                        "python",
                        fallback_reason,
                    )
            except Exception:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="python",
                    method=capability.name,
                    category="system",
                    user_message="Python 分析执行失败。",
                    code="EXECUTION_FAILED",
                    suggestion="请检查数据文件和变量后重试。",
                    fallback_reason=fallback_reason,
                )
        elif effective_backend == "spss":
            try:
                syntax = _build_syntax(
                    capability.name,
                    bindings,
                )
            except (KeyError, TypeError, ValueError):
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="spss",
                    method=capability.name,
                    category="system",
                    user_message="无法生成 SPSS 分析语法。",
                    code="SYNTAX_GENERATION_FAILED",
                    suggestion="请确认分析变量仍存在于当前数据集中。",
                    http_status=422,
                )
            from snla.syntax.validator import validate

            validation = validate(syntax, [item["name"] for item in request.variables])
            if not validation["valid"]:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="spss",
                    method=capability.name,
                    category="system",
                    user_message="SPSS 语法验证失败。",
                    code="SYNTAX_INVALID",
                    suggestion="请确认分析变量仍存在于当前数据集中。",
                    http_status=422,
                )
            greylist_warnings = tuple(
                warning
                for warning in validation.get("warnings", [])
                if "greylist" in warning.lower() or "confirm" in warning.lower()
            )
            if greylist_warnings and not request.confirm_greylist:
                self._planner.stage_greylist(
                    request.session_id,
                    GreylistPending(
                        syntax=syntax,
                        warnings=list(greylist_warnings),
                        method=capability.name,
                        user_input=request.query,
                    ),
                )
                return AnalysisConfirmationRequired(
                    syntax=syntax,
                    greylist_warnings=greylist_warnings,
                    audit=AnalysisAudit(
                        request_id=request_id,
                        started_at=started_at,
                        completed_at=_now(),
                        method=capability.name,
                        preferred_backend=preferred_backend,
                        effective_backend="spss",
                        parser_used=None,
                    ),
                )
            from snla.executor.spss import SPSSExecutor

            executor = SPSSExecutor()
            active.executor = executor
            try:
                if greylist_warnings:
                    temp_copy = True
                    execution = executor.execute_on_temp_copy(
                        syntax=syntax,
                        data_path=request.dataset_meta["file_path"],
                        cancellation_token=False,
                    )
                else:
                    execution = executor.run(
                        syntax=syntax,
                        data_path=request.dataset_meta["file_path"],
                        cancellation_token=False,
                    )
            except Exception:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="spss",
                    method=capability.name,
                    category="system",
                    user_message="SPSS 分析执行失败。",
                    code="EXECUTION_FAILED",
                    suggestion="请检查 SPSS 配置和数据文件后重试。",
                )
            if active.cancelled.is_set():
                return _cancelled(
                    request_id, started_at, capability.name, preferred_backend, "spss"
                )
            if not _execution_value(execution, "success", False):
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="spss",
                    method=capability.name,
                    category="system",
                    user_message="SPSS 分析执行失败。",
                    code="EXECUTION_FAILED",
                    suggestion="请检查 SPSS 输出和数据文件后重试。",
                )
            try:
                result = _parse_execution(execution, capability.name)
            except (ValueError, RuntimeError, FileNotFoundError):
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend="spss",
                    method=capability.name,
                    category="system",
                    user_message="SPSS 已执行，但输出无法解析。",
                    code="PARSE_FAILED",
                    suggestion="请检查 SPSS OMS 输出配置后重试。",
                )
        else:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="configuration",
                user_message="统计后端配置无效。",
                code="BACKEND_INVALID",
                suggestion="请在设置中选择 Python 或 SPSS。",
                http_status=400,
            )

        limited_mode = effective_backend == "python" and not backend_capability.validated
        warning = None
        explanation = None
        ai_polish = None
        if limited_mode:
            warning = (
                f"Python 引擎下“{capability.name}”方法的可靠性尚未经 SPSS 交叉验证。"
                "以下为原始统计数字，请谨慎解读。"
            )
        else:
            try:
                explanation, ai_polish = _explain_result(result, request.alpha)
            except Exception:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend=effective_backend,
                    method=capability.name,
                    category="system",
                    user_message="统计结果已生成，但解释失败。",
                    code="EXPLANATION_FAILED",
                    suggestion="请重试，或直接查看统计结果表。",
                    fallback_reason=fallback_reason,
                )
        backend_restored = False
        with self._active_lock:
            if fallback_reason is not None:
                self._fallback_notified_sessions.add(request.session_id)
                self._fallback_sessions.add(request.session_id)
            elif preferred_backend == "spss" and request.session_id in self._fallback_sessions:
                self._fallback_sessions.remove(request.session_id)
                backend_restored = True
        return AnalysisSuccess(
            user_query=request.query,
            method=capability.name,
            backend=effective_backend,
            plan_explanation=plan.plan_explanation,
            result=result,
            explanation=explanation,
            ai_polish=ai_polish,
            syntax=syntax,
            limited_mode=limited_mode,
            warning=warning,
            temp_copy=temp_copy,
            parameters={"alpha": request.alpha},
            selection_source=selection_source,
            fallback_reason=fallback_reason,
            backend_restored=backend_restored,
            variable_roles=bindings,
            audit=AnalysisAudit(
                request_id=request_id,
                started_at=started_at,
                completed_at=_now(),
                method=capability.name,
                preferred_backend=preferred_backend,
                effective_backend=effective_backend,
                parser_used=result.parser_used,
                fallback_reason=fallback_reason,
            ),
        )

    def cancel(self, session_id: str) -> bool:
        """Cancel one active analysis and clear pending greylist state."""

        self._planner.cancel_pending(session_id)
        with self._active_lock:
            self._pending_corrections.pop(session_id, None)
            active = self._active.get(session_id)
            if active is None:
                return False
            active.cancelled.set()
            executor = active.executor
        if executor is not None:
            with suppress(Exception):
                executor.terminate()
        return True

    def is_active(self, session_id: str) -> bool:
        """Return whether the session currently owns an analysis slot."""

        with self._active_lock:
            return session_id in self._active

    def confirm(self, request: AnalysisConfirmationRequest) -> AnalysisOutcome:
        active = _ActiveAnalysis(cancelled=Event())
        with self._active_lock:
            if self._active:
                return _failure(
                    request_id=uuid4().hex,
                    started_at=_now(),
                    preferred_backend=self._backend or _configured_backend(),
                    category="system",
                    user_message="当前已有分析正在执行。",
                    code="ENGINE_BUSY",
                    suggestion="请等待当前分析完成后重试。",
                    http_status=409,
                )
            self._active[request.session_id] = active
        try:
            return self._confirm(request, active)
        finally:
            with self._active_lock:
                self._active.pop(request.session_id, None)

    def _confirm(
        self, request: AnalysisConfirmationRequest, active: _ActiveAnalysis
    ) -> AnalysisOutcome:
        started_at = _now()
        request_id = uuid4().hex
        preferred_backend = self._backend or _configured_backend()
        with self._active_lock:
            correction = self._pending_corrections.pop(request.session_id, None)
        if correction is not None:
            if request.decision == "reject":
                return AnalysisCorrectionRejected(
                    original_method=correction.plan.method,
                    audit=AnalysisAudit(
                        request_id=request_id,
                        started_at=started_at,
                        completed_at=_now(),
                        method=correction.plan.method,
                        preferred_backend=preferred_backend,
                        effective_backend=None,
                        parser_used=None,
                    ),
                )
            selected = next(
                (
                    choice
                    for choice in correction.decision.corrections
                    if choice.id == request.correction_id
                ),
                None,
            )
            if selected is None:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    method=correction.plan.method,
                    category="user",
                    user_message="请选择一个有效的分析方法修正方案。",
                    code="INVALID_CORRECTION",
                    suggestion="重新提交分析请求并选择列出的修正方案。",
                    http_status=400,
                )
            corrected_plan = replace(
                correction.plan,
                method=selected.method,
                plan_explanation=(
                    f"{correction.plan.plan_explanation} Confirmed correction: {selected.label}."
                ),
            )
            corrected_request = replace(
                correction.request,
                variables=request.variables,
                dataset_meta=request.dataset_meta,
            )
            return self._analyze(
                corrected_request,
                active,
                plan_override=corrected_plan,
            )
        try:
            pending = self._planner.pop_pending(request.session_id)
        except NoPendingError:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="没有待确认的操作。",
                code="NO_PENDING",
                suggestion="请先提交需要确认的分析请求。",
                http_status=400,
            )
        if preferred_backend == "spss" and not self._spss_available():
            choices = (
                "检查 SPSS 可执行文件路径后重新提交操作。",
                "取消本次数据修改操作。",
            )
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="configuration",
                user_message=("确认操作执行前 SPSS 已不可用，数据修改语法不能自动转换为 Python。"),
                code="SPSS_FALLBACK_UNAVAILABLE",
                suggestion=choices[0],
                http_status=422,
                method=pending.method,
                correction_choices=choices,
                fallback_reason={
                    "code": "SPSS_EXECUTABLE_NOT_FOUND",
                    "method": pending.method,
                },
            )
        data_path = request.dataset_meta.get("file_path", "")
        if not request.variables or not data_path:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                category="user",
                user_message="请先上传数据文件。",
                code="NO_DATA",
                suggestion="上传 .sav 文件后重新提交操作。",
                http_status=400,
                method=pending.method,
            )

        from snla.executor.spss import SPSSExecutor

        executor = SPSSExecutor()
        active.executor = executor
        try:
            execution = executor.execute_on_temp_copy(
                syntax=pending.syntax,
                data_path=data_path,
                cancellation_token=False,
            )
        except Exception:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                effective_backend="spss",
                method=pending.method,
                category="system",
                user_message="临时副本上的 SPSS 分析执行失败。",
                code="EXECUTION_FAILED",
                suggestion="请检查 SPSS 配置和数据文件后重试。",
            )
        if active.cancelled.is_set():
            return _cancelled(request_id, started_at, pending.method, preferred_backend, "spss")
        if not _execution_value(execution, "success", False):
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                effective_backend="spss",
                method=pending.method,
                category="system",
                user_message="临时副本上的 SPSS 分析执行失败。",
                code="EXECUTION_FAILED",
                suggestion="请检查 SPSS 输出后重试。",
            )

        try:
            result = _parse_execution(execution, pending.method)
        except (ValueError, RuntimeError, FileNotFoundError):
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                effective_backend="spss",
                method=pending.method,
                category="system",
                user_message="SPSS 已执行，但输出无法解析。",
                code="PARSE_FAILED",
                suggestion="请检查 SPSS OMS 输出配置后重试。",
            )
        try:
            explanation, ai_polish = _explain_result(result)
        except Exception:
            return _failure(
                request_id=request_id,
                started_at=started_at,
                preferred_backend=preferred_backend,
                effective_backend="spss",
                method=pending.method,
                category="system",
                user_message="统计结果已生成，但解释失败。",
                code="EXPLANATION_FAILED",
                suggestion="请重试，或直接查看统计结果表。",
            )
        return AnalysisSuccess(
            user_query=pending.user_input,
            method=pending.method,
            backend="spss",
            plan_explanation="",
            result=result,
            explanation=explanation,
            ai_polish=ai_polish,
            syntax=pending.syntax,
            greylist_warnings=tuple(pending.warnings),
            temp_copy=True,
            audit=AnalysisAudit(
                request_id=request_id,
                started_at=started_at,
                completed_at=_now(),
                method=pending.method,
                preferred_backend=preferred_backend,
                effective_backend="spss",
                parser_used=result.parser_used,
            ),
        )


def _configured_backend() -> str:
    from snla import config

    return config.STATS_BACKEND


def _configured_spss_available() -> bool:
    from snla import config

    return config.check_spss_available()


def _load_dataframe(file_path: str):
    path = Path(file_path)
    if path.suffix.lower() == ".sav":
        import pyreadstat

        data, _ = pyreadstat.read_sav(path)
        return data

    import pandas as pd

    return pd.read_csv(path)


def _build_syntax(
    method: str,
    bindings: dict[str, str | None],
) -> str:
    from snla.syntax.templates import get_syntax_by_method

    arguments = {
        "independent_t_test": {
            "group_var": bindings.get("grouping_variable"),
            "test_var": bindings.get("test_variable"),
            "groups": (1, 2),
        },
        "paired_t_test": {
            "var1": bindings.get("first_variable"),
            "var2": bindings.get("second_variable"),
        },
        "oneway_anova": {
            "group_var": bindings.get("grouping_variable"),
            "test_var": bindings.get("test_variable"),
        },
        "simple_regression": {
            "dep_var": bindings.get("dependent_variable"),
            "indep_var": bindings.get("independent_variable"),
        },
        "pearson_correlation": {
            "var1": bindings.get("first_variable"),
            "var2": bindings.get("second_variable"),
        },
        "spearman_correlation": {
            "var1": bindings.get("first_variable"),
            "var2": bindings.get("second_variable"),
        },
        "chi_square": {
            "row_var": bindings.get("row_variable"),
            "col_var": bindings.get("column_variable"),
        },
        "frequencies": {"var": bindings.get("variable")},
        "descriptives": {"var": bindings.get("variable")},
        "mann_whitney_u": {
            "group_var": bindings.get("grouping_variable"),
            "test_var": bindings.get("test_variable"),
            "groups": (1, 2),
        },
        "kruskal_wallis": {
            "group_var": bindings.get("grouping_variable"),
            "test_var": bindings.get("test_variable"),
        },
    }
    return get_syntax_by_method(method, **arguments[method])


def _execution_value(execution: Any, name: str, default: Any = None) -> Any:
    if isinstance(execution, dict):
        return execution.get(name, default)
    return getattr(execution, name, default)


def _read_listing(execution: Any) -> str:
    listing_path = _execution_value(execution, "lst_path")
    if listing_path and Path(listing_path).is_file():
        return Path(listing_path).read_text(encoding="utf-8", errors="replace")
    return _execution_value(execution, "lst_text", "") or _execution_value(execution, "stdout", "")


def _parse_execution(execution: Any, method: str) -> AnalysisResult:
    from snla.parser.output import parse

    return parse(
        oms_xml_path=_execution_value(execution, "xml_path"),
        lst_text=_read_listing(execution),
        analysis_type=_ANALYSIS_TYPES.get(method, "UNKNOWN"),
    )


def _explain_result(result: AnalysisResult, alpha: float = 0.05) -> tuple[str, str | None]:
    from snla import config
    from snla.explainer.naturalize import explain

    local_explanation = explain(result, use_llm_polish=False, alpha=alpha)
    use_llm = config.AI_POLISH_ENABLED and bool(config.LLM_API_KEY) and not config.LLM_MOCK
    if use_llm:
        from snla.llm.client import LLMClient

        polished = explain(result, use_llm_polish=True, llm_client=LLMClient(), alpha=alpha)
        return local_explanation, polished if polished != local_explanation else None
    return local_explanation, None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _failure(
    *,
    request_id: str,
    started_at: str,
    preferred_backend: str,
    category: str,
    user_message: str,
    code: str,
    suggestion: str | None = None,
    http_status: int = 500,
    method: str | None = None,
    effective_backend: str | None = None,
    correction_choices: tuple[str, ...] = (),
    fallback_reason: dict[str, Any] | None = None,
) -> AnalysisFailure:
    return AnalysisFailure(
        error=AnalysisError(
            category=category,
            user_message=user_message,
            code=code,
            suggestion=suggestion,
        ),
        audit=AnalysisAudit(
            request_id=request_id,
            started_at=started_at,
            completed_at=_now(),
            method=method,
            preferred_backend=preferred_backend,
            effective_backend=effective_backend,
            parser_used=None,
            fallback_reason=fallback_reason,
        ),
        http_status=http_status,
        correction_choices=correction_choices,
    )


def _cancelled(
    request_id: str,
    started_at: str,
    method: str | None,
    preferred_backend: str,
    effective_backend: str | None,
    fallback_reason: dict[str, Any] | None = None,
) -> AnalysisCancelled:
    return AnalysisCancelled(
        audit=AnalysisAudit(
            request_id=request_id,
            started_at=started_at,
            completed_at=_now(),
            method=method,
            preferred_backend=preferred_backend,
            effective_backend=effective_backend,
            parser_used=None,
            fallback_reason=fallback_reason,
        )
    )


def _render_markdown(method: str, result: dict[str, Any], explanation: str | None) -> str:
    parts = [f"## {method}\n"]
    for table in result.get("tables", []):
        parts.append(f"### {table.get('title', '')}")
        rows = table.get("rows", [])
        if rows:
            parts.append("| " + " | ".join(rows[0]) + " |")
            parts.append("|" + "|".join(["---"] * len(rows[0])) + "|")
            for row in rows:
                parts.append("| " + " | ".join(str(value) for value in row.values()) + " |")
        parts.append("")
    if explanation:
        parts.append(explanation)
    return "\n".join(parts)


analysis_service = AnalysisService()
