"""One application service for every StatsTalk analysis entry point."""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from snla.capabilities import get_capability
from snla.orchestrator import GreylistPending, NoPendingError, Planner, planner
from snla.parser.schema import AnalysisResult, analysis_result_to_dict

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


@dataclass(frozen=True)
class AnalysisConfirmationRequest:
    """Protocol-independent input for a pending greylist confirmation."""

    session_id: str
    variables: list[dict[str, Any]]
    dataset_meta: dict[str, Any]


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": asdict(self.error),
            "audit": asdict(self.audit),
        }


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

    method: str
    backend: str
    plan_explanation: str
    result: AnalysisResult
    explanation: str | None
    audit: AnalysisAudit
    syntax: str = ""
    greylist_warnings: tuple[str, ...] = ()
    limited_mode: bool = False
    warning: str | None = None
    degradation: dict[str, Any] | None = None
    temp_copy: bool = False

    def to_payload(self) -> dict[str, Any]:
        result = analysis_result_to_dict(self.result)
        payload: dict[str, Any] = {
            "ok": True,
            "method": self.method,
            "backend": self.backend,
            "syntax": self.syntax,
            "syntax_used": self.syntax,
            "plan_explanation": self.plan_explanation,
            "greylist_warnings": list(self.greylist_warnings),
            "result": result,
            "explanation": self.explanation,
            "markdown": _render_markdown(self.method, result, self.explanation),
            "limited_mode": self.limited_mode,
            "degradation": self.degradation,
            "last_analysis": {
                "method": self.method,
                **({"syntax": self.syntax} if self.syntax else {}),
            },
            "audit": asdict(self.audit),
        }
        if self.warning:
            payload["warning"] = self.warning
        if self.temp_copy:
            payload["temp_copy_note"] = "此操作已在数据的临时副本上执行，原始数据文件未被修改。"
        return payload


AnalysisOutcome = (
    AnalysisSuccess | AnalysisFailure | AnalysisConfirmationRequired | AnalysisCancelled
)


@dataclass
class _ActiveAnalysis:
    cancelled: Event
    executor: Any = None


class AnalysisService:
    """Own the complete analysis policy behind a small typed interface."""

    def __init__(self, *, backend: str | None = None, analysis_planner: Planner = planner):
        self._backend = backend
        self._planner = analysis_planner
        self._active: dict[str, _ActiveAnalysis] = {}
        self._active_lock = Lock()

    def analyze(self, request: AnalysisRequest) -> AnalysisOutcome:
        active = _ActiveAnalysis(cancelled=Event())
        with self._active_lock:
            if request.session_id in self._active:
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
            return self._analyze(request, active)
        finally:
            with self._active_lock:
                self._active.pop(request.session_id, None)

    def _analyze(self, request: AnalysisRequest, active: _ActiveAnalysis) -> AnalysisOutcome:
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
        backend_capability = capability.backend(preferred_backend)
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
        syntax = ""
        temp_copy = False
        if preferred_backend == "python":
            try:
                data = _load_dataframe(request.dataset_meta["file_path"])
                from snla.executor.python import PythonStatsExecutor

                result = PythonStatsExecutor().execute(
                    plan.method,
                    data,
                    grouping_var=plan.grouping_variable,
                    test_var=plan.test_variable,
                    dep_var=plan.grouping_variable,
                    indep_var=plan.test_variable,
                )
                if active.cancelled.is_set():
                    return _cancelled(
                        request_id, started_at, capability.name, preferred_backend, "python"
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
                )
        elif preferred_backend == "spss":
            try:
                syntax = _build_syntax(
                    capability.name,
                    request.variables,
                    plan.grouping_variable,
                    plan.test_variable,
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

        limited_mode = preferred_backend == "python" and not backend_capability.validated
        warning = None
        explanation = None
        if limited_mode:
            warning = (
                f"Python 引擎下“{capability.name}”方法的可靠性尚未经 SPSS 交叉验证。"
                "以下为原始统计数字，请谨慎解读。"
            )
        else:
            try:
                explanation = _explain_result(result)
            except Exception:
                return _failure(
                    request_id=request_id,
                    started_at=started_at,
                    preferred_backend=preferred_backend,
                    effective_backend=preferred_backend,
                    method=capability.name,
                    category="system",
                    user_message="统计结果已生成，但解释失败。",
                    code="EXPLANATION_FAILED",
                    suggestion="请重试，或直接查看统计结果表。",
                )
        return AnalysisSuccess(
            method=capability.name,
            backend=preferred_backend,
            plan_explanation=plan.plan_explanation,
            result=result,
            explanation=explanation,
            syntax=syntax,
            limited_mode=limited_mode,
            warning=warning,
            temp_copy=temp_copy,
            audit=AnalysisAudit(
                request_id=request_id,
                started_at=started_at,
                completed_at=_now(),
                method=capability.name,
                preferred_backend=preferred_backend,
                effective_backend=preferred_backend,
                parser_used=result.parser_used,
            ),
        )

    def cancel(self, session_id: str) -> bool:
        """Cancel one active analysis and clear pending greylist state."""

        self._planner.cancel_pending(session_id)
        with self._active_lock:
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
            if request.session_id in self._active:
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
            explanation = _explain_result(result)
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
            method=pending.method,
            backend="spss",
            plan_explanation="",
            result=result,
            explanation=explanation,
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
    variables: list[dict[str, Any]],
    grouping_variable: str | None,
    test_variable: str | None,
) -> str:
    from snla.syntax.templates import get_syntax_by_method

    names = {item.get("name") for item in variables}
    grouping = grouping_variable if grouping_variable in names else None
    tested = test_variable if test_variable in names else None
    skip = {"id", "ID", "Id", "customerid", "customer_id", "row", "ROW", "case", "CASE"}
    numeric = [
        item["name"]
        for item in variables
        if item.get("type") == "Numeric"
        and not item.get("value_labels")
        and item.get("name") not in skip
    ]
    categorical = [item["name"] for item in variables if item.get("value_labels")]
    grouping = grouping or (categorical[0] if categorical else variables[0]["name"])
    tested = tested or (numeric[0] if numeric else variables[0]["name"])
    second = next((name for name in numeric if name != tested), tested)
    arguments = {
        "independent_t_test": {"group_var": grouping, "test_var": tested, "groups": (1, 2)},
        "paired_t_test": {"var1": tested, "var2": second},
        "oneway_anova": {"group_var": grouping, "test_var": tested},
        "simple_regression": {"dep_var": tested, "indep_var": second},
        "pearson_correlation": {"var1": tested, "var2": second},
        "spearman_correlation": {"var1": tested, "var2": second},
        "chi_square": {"row_var": grouping, "col_var": tested},
        "frequencies": {"var": grouping},
        "descriptives": {"var": tested},
        "mann_whitney_u": {"group_var": grouping, "test_var": tested, "groups": (1, 2)},
        "kruskal_wallis": {"group_var": grouping, "test_var": tested},
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


def _explain_result(result: AnalysisResult) -> str:
    from snla import config
    from snla.explainer.naturalize import explain

    use_llm = bool(config.LLM_API_KEY) and not config.LLM_MOCK
    if use_llm:
        from snla.llm.client import LLMClient

        return explain(result, use_llm_polish=True, llm_client=LLMClient())
    return explain(result, use_llm_polish=False)


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
        ),
        http_status=http_status,
    )


def _cancelled(
    request_id: str,
    started_at: str,
    method: str | None,
    preferred_backend: str,
    effective_backend: str | None,
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
