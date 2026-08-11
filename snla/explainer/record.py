"""Privacy-safe, reproducible analysis record shared by UI and exports."""

from __future__ import annotations

import math
import platform
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from snla.parser.schema import AnalysisResult
from snla.version import APP_VERSION

_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "password",
    "secret",
    "source_path",
    "file_path",
    "filepath",
    "raw_data",
    "raw_output",
    "crash",
)


def build_analysis_record(
    *,
    capability: str,
    variable_roles: dict[str, str],
    parameters: dict[str, Any],
    result: AnalysisResult,
    conclusion: str | None,
    syntax: str,
    preferred_backend: str,
    effective_backend: str,
    fallback_reason: dict[str, Any] | None,
    warning: str | None,
    greylist_warnings: tuple[str, ...],
    audit: Any,
    selection_source: str,
    applicability_warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a strict allowlist record without dataset or credential state."""

    fallback = _safe_fallback(fallback_reason)
    warnings = _collect_warnings(
        warning,
        applicability_warnings,
        greylist_warnings,
        result,
        fallback,
    )
    tables = _safe_export_value([asdict(table) for table in result.tables])
    statistics = _safe_export_value(result.statistics)
    audit_values = asdict(audit) if not isinstance(audit, dict) else dict(audit)
    diagnostics = {
        "analysis_type": result.analysis_type,
        "parser_used": result.parser_used,
        "n_valid": result.n_valid,
        "n_missing": result.n_missing,
        "selection_source": selection_source,
        "request_id": audit_values.get("request_id"),
        "started_at": audit_values.get("started_at"),
        "completed_at": audit_values.get("completed_at"),
    }
    backend = {
        "preferred": preferred_backend,
        "effective": effective_backend,
        "fallback": fallback,
    }
    return {
        "schema_version": "1.0",
        "analysis_id": audit_values.get("request_id"),
        "completed_at": audit_values.get("completed_at"),
        "capability": capability,
        "variables": [
            {"role": role, "name": name}
            for role, name in variable_roles.items()
            if isinstance(role, str) and isinstance(name, str) and name
        ],
        "parameters": _safe_export_value(parameters),
        "versions": _runtime_versions(effective_backend),
        "warnings": warnings,
        "fallback": fallback,
        "summary": {
            "conclusion": conclusion or "",
            "key_statistics": statistics,
            "warnings": warnings,
            "applicability_warnings": list(applicability_warnings),
            "fallback": {"used": fallback is not None, "reason": fallback},
        },
        "advanced": {
            "tables": tables,
            "syntax": syntax,
            "backend": backend,
            "diagnostics": diagnostics,
        },
    }


def _collect_warnings(
    warning: str | None,
    applicability_warnings: tuple[str, ...],
    greylist_warnings: tuple[str, ...],
    result: AnalysisResult,
    fallback: dict[str, Any] | None,
) -> list[str]:
    candidates: list[Any] = [warning, *applicability_warnings, *greylist_warnings]
    candidates.extend(result.notes)
    for table in result.tables:
        candidates.extend(table.notes)
    if fallback:
        candidates.append(fallback.get("message"))
    warnings: list[str] = []
    for item in candidates:
        if isinstance(item, str) and item.strip() and item.strip() not in warnings:
            warnings.append(item.strip())
    return warnings


def _safe_fallback(reason: dict[str, Any] | None) -> dict[str, Any] | None:
    if not reason:
        return None
    allowed = {"code", "message", "method"}
    return {
        key: value
        for key, value in reason.items()
        if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
    }


def _safe_export_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_export_value(item)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_export_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _runtime_versions(effective_backend: str) -> dict[str, Any]:
    packages = {}
    for package in ("pandas", "scipy", "pingouin", "pyreadstat"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            continue
    return {
        "statstalk": APP_VERSION,
        "python": platform.python_version(),
        "effective_backend": effective_backend,
        "packages": packages,
    }


__all__ = ["build_analysis_record"]
