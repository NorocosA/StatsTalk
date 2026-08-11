"""
SNLA Flask API Server — PyWebView backend.

Exposes REST endpoints consumed by the WebView frontend.
Reuses all existing snla backend modules (config, llm, syntax, executor, parser, explainer).

Run standalone:  python snla/ui/server.py
Run via launcher: launcher.py spawns this in a thread.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from io import BytesIO
from pathlib import Path

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, jsonify, request, send_file, send_from_directory

from snla.analysis import (
    AnalysisConfirmationRequest,
    AnalysisRequest,
    AnalysisSuccess,
    analysis_service,
)
from snla.capabilities import get_public_capabilities_payload
from snla.config import DEBUG, LLM_MOCK  # noqa: F401 — LLM_MOCK imported for test patching
from snla.data.persistence import load_session, save_session
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud
from snla.llm.transport import (
    EndpointPolicyError,
    NoRedirectHandler,
    diagnose_transport_failure,
    require_secure_llm_endpoint,
)
from snla.orchestrator import planner_instance
from snla.secrets import BACKUP_MAX_BYTES, SecretProtectionError
from snla.session import SessionState
from snla.trust import get_trusted_methods, trust_loaded_from
from snla.ui._helpers import (
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW,
    _check_rate_limit,
    _spss_available,
)
from snla.ui.security import BootstrapError, loopback_security

planner = planner_instance
logger = logging.getLogger(__name__)

# Ensure root logger has a basic config when running standalone
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

app = Flask(__name__, static_folder=None)


@app.before_request
def _require_api_authentication():
    """Reject unauthenticated control-plane requests by default."""

    if not request.path.startswith("/api/"):
        return None

    if not loopback_security.is_origin_allowed(request.headers.get("Origin")):
        return jsonify(
            {
                "error": "cross_origin_request",
                "reason": "origin_not_allowed",
            }
        ), 403

    if request.method == "OPTIONS":
        return "", 204

    if request.path == "/api/bootstrap":
        return None

    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        return jsonify(
            {
                "error": "authentication_required",
                "reason": "missing_token",
            }
        ), 401

    failure_reason = loopback_security.validate_session(authorization.removeprefix("Bearer "))
    if failure_reason is not None:
        return jsonify(
            {
                "error": "authentication_required",
                "reason": failure_reason,
            }
        ), 401

    return None


@app.after_request
def _add_exact_origin_cors(response):
    origin = request.headers.get("Origin")
    if origin is not None and loopback_security.is_origin_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers.add("Vary", "Origin")
    return response


# ── Upload limits ────────────────────────────────────────────────────
MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB
ALLOWED_EXTENSIONS = {".sav", ".csv"}
ALLOWED_MIME_TYPES = {"application/octet-stream", "text/csv"}
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE

# ── Query limits ──────────────────────────────────────────────────────
MAX_QUERY_LENGTH = 2000  # max characters per user query

# In-memory session (one user, one session)
session = SessionState()
_load_ok = load_session(session)
if _load_ok:
    logger.info("Restored previous session from SQLite")
UI_DIR = Path(__file__).resolve().parent

# ── Rate limiting ─────────────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = {}


# ── Static frontend ───────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(UI_DIR), "index.html")


@app.route("/api/bootstrap", methods=["POST"])
def bootstrap():
    """Exchange the one-time launch credential for a session token."""

    data = request.get_json(silent=True) or {}
    try:
        session_token = loopback_security.exchange_bootstrap(str(data.get("bootstrap_token", "")))
    except BootstrapError as exc:
        return jsonify({"error": "bootstrap_failed", "reason": exc.reason}), 401
    return jsonify({"ok": True, "session_token": session_token})


# ── Health ────────────────────────────────────────────────────────────
@app.route("/api/status")
def status():
    import snla.config as cfg

    return jsonify(
        {
            "ok": True,
            "has_data": session.dataset_meta is not None,
            "variable_count": len(session.variables),
            "executing": analysis_service.is_active("default"),
            "spss_available": _spss_available(),
            "current_backend": cfg.STATS_BACKEND,
            "api_key_status": cfg.api_key_public_status(),
            "capabilities": get_public_capabilities_payload(),
            "trusted_methods": list(get_trusted_methods()),
            "trust_source": trust_loaded_from(),
        }
    )


# ── File Upload ───────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "未选择文件"}), 400

    # Content-length check (redundant with Flask MAX_CONTENT_LENGTH but explicit)
    if f.content_length is not None and f.content_length > MAX_UPLOAD_SIZE:
        return jsonify({"error": "文件大小超过限制（最大500MB）"}), 413

    # Extension validation
    suffix = Path(f.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "不支持的文件类型，仅支持 .sav 和 .csv"}), 400

    # MIME validation (allow octet-stream since .sav has no standard MIME)
    mime = f.content_type or ""
    if mime not in ALLOWED_MIME_TYPES and mime != "":
        return jsonify({"error": "文件类型无效"}), 400

    # Save to persistent location so SPSS can access it later
    from snla.config import P0_OUTPUT_DIR

    os.makedirs(P0_OUTPUT_DIR, exist_ok=True)
    dest = os.path.join(P0_OUTPUT_DIR, f"uploaded_{os.urandom(4).hex()}{suffix}")
    f.save(dest)

    try:
        meta = read_and_extract(dest)
        meta["file_path"] = dest  # Store path for SPSS execution
        meta["filename"] = f.filename
        session.dataset_meta = meta
        session.variables = meta.get("variables", [])

        # Persist so a desktop restart doesn't lose the uploaded dataset
        save_session(session)

        # Sanitize for cloud safety
        cloud_safe = filter_for_cloud(meta)
        cloud_vars = cloud_safe.get("variables", session.variables)

        return jsonify(
            {
                "ok": True,
                "filename": f.filename,
                "variables": cloud_vars,
                "row_count": meta.get("row_count", 0),
            }
        )
    except Exception as e:
        logger.exception("Upload failed")
        return jsonify({"error": str(e)}), 500


# ── Cancel ────────────────────────────────────────────────────────────
@app.route("/api/cancel", methods=["POST"])
def cancel():
    """Cancel the currently running analysis.

    Sets the session cancellation token and terminates the active SPSS
    subprocess (if any).  Returns ``{ok: True}`` even if nothing was
    running — the frontend can safely call this at any time.
    """
    analysis_service.cancel("default")
    session.reset_cancellation()
    return jsonify({"ok": True})


# ── Local suggestion ─────────────────────────────────────────────────
@app.route("/api/suggest", methods=["POST"])
def suggest():
    """Return a deterministic, network-free method and variable suggestion."""

    data = request.get_json(silent=True) or {}
    user_input = data.get("text", "")
    if not isinstance(user_input, str):
        return jsonify({"error": "输入类型无效"}), 400
    if not session.variables or not session.dataset_meta:
        return jsonify({"error": "请先上传数据文件"}), 400

    from snla.orchestrator.planner import suggest_local

    plan = suggest_local(
        user_input.strip(),
        session.variables,
        last_analysis=session.last_analysis,
    )
    return jsonify(
        {
            "ok": True,
            "source": "local_suggestion",
            "method": plan.method,
            "grouping_variable": plan.grouping_variable,
            "test_variable": plan.test_variable,
            "label": "本地建议",
        }
    )


# ── Analyze ───────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    if _check_rate_limit():
        return (
            jsonify(
                {
                    "error": f"请求过于频繁，请等待后再试（每{RATE_LIMIT_WINDOW}秒最多{RATE_LIMIT_MAX_REQUESTS}次）"
                }
            ),
            429,
        )

    data = request.get_json(force=True)
    method = data.get("method")
    if method is not None and not isinstance(method, str):
        return jsonify({"error": "分析方法类型无效"}), 400
    raw_text = data.get("text", "")
    if not isinstance(raw_text, str):
        return jsonify({"error": "输入类型无效"}), 400
    user_input = raw_text.strip()
    if method and not user_input:
        user_input = f"使用结构化控件运行 {method}"
    confirm_greylist = data.get("confirm_greylist", False)

    if len(user_input) > MAX_QUERY_LENGTH:
        return jsonify({"error": f"输入文本过长（最大 {MAX_QUERY_LENGTH} 字符）"}), 400
    try:
        alpha = float(data.get("alpha", 0.05))
    except (TypeError, ValueError):
        return jsonify({"error": "显著性水平必须是数字"}), 400
    selection_source = str(data.get("selection_source", "planner"))
    if selection_source not in {"planner", "local_suggestion", "user_selection"}:
        return jsonify({"error": "分析选择来源无效"}), 400

    # ── Range expansion (Q1-Q10 → Q1, Q2, ..., Q10) ────────────────
    try:
        from snla.data.range_expander import expand_query

        var_names = [v.get("name", "") for v in session.variables if v.get("name")]
        expanded = expand_query(user_input, var_names)
        if expanded != user_input:
            logger.info("Range expanded: %s → %s", user_input, expanded)
            user_input = expanded
    except Exception:
        logger.warning("Range expansion failed, continuing with original input", exc_info=True)

    session.reset_cancellation()
    try:
        outcome = analysis_service.analyze(
            AnalysisRequest(
                session_id="default",
                query=user_input,
                variables=session.variables,
                dataset_meta=session.dataset_meta,
                last_analysis=session.last_analysis,
                confirm_greylist=confirm_greylist,
                method=method,
                grouping_variable=data.get("grouping_variable"),
                test_variable=data.get("test_variable"),
                alpha=alpha,
                selection_source=selection_source,
            )
        )
        payload = outcome.to_payload()
        if isinstance(outcome, AnalysisSuccess):
            session.history.append({"role": "user", "content": outcome.user_query})
            session.history.append(
                {
                    "role": "assistant",
                    "content": outcome.explanation,
                    "method": outcome.method,
                    "syntax": outcome.syntax,
                    "result": outcome.result,
                    "parameters": outcome.parameters or {},
                    "selection_source": outcome.selection_source,
                    "backend": outcome.backend,
                }
            )
            session.last_analysis = payload["last_analysis"]
            save_session(session)
        return jsonify(payload), getattr(outcome, "http_status", 200)
    except Exception:
        logger.exception("Analysis failed")
        return jsonify({"error": "Analysis failed"}), 500
    finally:
        session.reset_cancellation()


# ── Confirm Greylist ──────────────────────────────────────────────────
@app.route("/api/confirm", methods=["POST"])
def confirm_greylist():
    """Resolve a pending greylist operation or method correction.

    The frontend calls this after the user clicks "Yes, execute" on the
    greylist confirmation dialog.  The pending greylist details are
    retrieved from ``Planner`` (set by `/api/analyze`).

    Execution happens on a **temporary copy** of the data file so the
    original is never modified.
    """
    session.reset_cancellation()
    try:
        data = request.get_json(silent=True) or {}
        outcome = analysis_service.confirm(
            AnalysisConfirmationRequest(
                session_id="default",
                variables=session.variables,
                dataset_meta=session.dataset_meta,
                decision=str(data.get("decision", "accept")),
                correction_id=data.get("correction_id"),
            )
        )
        payload = outcome.to_payload()
        if isinstance(outcome, AnalysisSuccess):
            session.history.append({"role": "user", "content": outcome.user_query})
            session.history.append(
                {
                    "role": "assistant",
                    "content": outcome.explanation,
                    "method": outcome.method,
                    "syntax": outcome.syntax,
                    "result": outcome.result,
                    "parameters": outcome.parameters or {},
                    "selection_source": outcome.selection_source,
                    "backend": outcome.backend,
                }
            )
            session.last_analysis = payload["last_analysis"]
            save_session(session)
        return jsonify(payload), getattr(outcome, "http_status", 200)
    except Exception:
        logger.exception("Analysis confirmation failed")
        return jsonify({"error": "Analysis confirmation failed"}), 500
    finally:
        session.reset_cancellation()


@app.route("/api/variables")
def variables():
    cloud_vars = (
        filter_for_cloud({"variables": session.variables}).get("variables", [])
        if session.variables
        else []
    )
    return jsonify(
        {
            "variables": cloud_vars,
            "row_count": session.dataset_meta.get("row_count", 0),
            "filename": session.dataset_meta.get("filename", "") if session.dataset_meta else "",
        }
    )


# ── Demo Mode ─────────────────────────────────────────────────────────
@app.route("/api/load-demo", methods=["POST"])
def load_demo():
    """Load bundled sample data for demo mode (no file upload needed)."""
    demo_path = os.path.join(str(PROJECT_ROOT), "data", "fixtures", "test_data.sav")
    # In PyInstaller bundle, data is in sys._MEIPASS
    if not os.path.exists(demo_path):
        import sys

        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root:
            demo_path = os.path.join(bundle_root, "data", "fixtures", "test_data.sav")
    if not os.path.exists(demo_path):
        return jsonify({"error": "示例数据文件不存在"}), 404
    try:
        meta = read_and_extract(demo_path)
        meta["file_path"] = demo_path
        meta["filename"] = "test_data.sav (Demo)"
        session.dataset_meta = meta
        session.variables = meta.get("variables", [])
        cloud_vars = filter_for_cloud({"variables": session.variables}).get("variables", [])
        save_session(session)
        return jsonify(
            {
                "ok": True,
                "filename": "test_data.sav",
                "variables": cloud_vars,
                "row_count": meta.get("row_count", 0),
            }
        )
    except Exception as e:
        logger.exception("Demo load failed")
        return jsonify({"error": str(e)}), 500


# ── Startup Warnings ────────────────────────────────────────────────────
@app.route("/api/startup-warnings", methods=["GET"])
def startup_warnings():
    """Return config validation warnings for first-launch UI guidance."""
    from snla.config import STATS_BACKEND, validate

    raw = validate()
    guidance = []
    for w in raw:
        if "SPSS" in w and LLM_MOCK:
            guidance.append(
                {
                    "level": "info",
                    "message": "Demo 模式已启用，无需 SPSS 或 API Key。",
                    "action": None,
                }
            )
        elif "LLM_API_KEY" in w or "API key" in w or "API Key" in w:
            guidance.append(
                {
                    "level": "warning",
                    "message": w,
                    "action": "settings",
                }
            )
        elif "SPSS" in w:
            guidance.append(
                {
                    "level": "info",
                    "message": w + " 将自动使用 Python 后端。",
                    "action": None,
                }
            )
        else:
            guidance.append({"level": "warning", "message": w, "action": None})

    return jsonify(
        {
            "ok": True,
            "warnings": guidance,
            "llm_mock": LLM_MOCK,
            "spss_available": _spss_available(),
            "backend": STATS_BACKEND,
        }
    )


# ── Settings ──────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    """Read settings or update them without exposing API-key material."""
    if request.method == "GET":
        import snla.config as cfg
        from snla.explainer.naturalize import POLISH_AGGREGATE_FIELDS

        api_key_status = cfg.api_key_public_status()
        return jsonify(
            {
                "LLM_ENDPOINT": cfg.LLM_ENDPOINT,
                "LLM_API_KEY": "",
                "LLM_API_KEY_CONFIGURED": api_key_status["configured"],
                "LLM_API_KEY_STATE": api_key_status["state"],
                "LLM_CLOUD_AVAILABLE": api_key_status["cloud_available"],
                "LLM_API_KEY_ACTION": api_key_status["action"],
                "LLM_API_KEY_MESSAGE": api_key_status["message"],
                "LLM_MODEL": cfg.LLM_MODEL,
                "AI_POLISH_ENABLED": cfg.AI_POLISH_ENABLED,
                "AI_POLISH_FIELDS": list(POLISH_AGGREGATE_FIELDS),
                "SPSS_PATH": cfg.SPSS_EXECUTABLE,
                "SPSS_PYTHON_PATH": cfg.SPSS_PYTHON_PATH,
                "STATS_BACKEND": cfg.STATS_BACKEND,
            }
        )

    # ── POST: update settings ─────────────────────────────
    data = request.get_json(force=True)
    import snla.config as cfg

    endpoint = data.get("LLM_ENDPOINT")
    if endpoint:
        try:
            data["LLM_ENDPOINT"] = require_secure_llm_endpoint(str(endpoint).strip())
        except EndpointPolicyError as exc:
            return jsonify(
                {
                    "error": "invalid_llm_endpoint",
                    "code": exc.code,
                    "message": str(exc),
                }
            ), 400

    changed = []
    if data.get("DELETE_LLM_API_KEY") is True:
        try:
            cfg.delete_api_key()
        except (OSError, SecretProtectionError):
            return jsonify(
                {
                    "error": "api_key_delete_failed",
                    "code": "secret_delete_failed",
                    "message": "The encrypted API key could not be deleted.",
                    "api_key_status": cfg.api_key_public_status(),
                }
            ), 500
        changed.append("LLM_API_KEY")

    if data.get("MIGRATE_LLM_API_KEY") is True:
        try:
            cfg.migrate_legacy_api_key(consent=True)
        except SecretProtectionError as exc:
            return jsonify(
                {
                    "error": "api_key_migration_failed",
                    "code": exc.code,
                    "message": str(exc),
                    "api_key_status": cfg.api_key_public_status(),
                }
            ), 409
        changed.append("LLM_API_KEY")

    api_key = data.get("LLM_API_KEY")
    if api_key:
        try:
            cfg.replace_api_key(str(api_key))
        except SecretProtectionError as exc:
            return jsonify(
                {
                    "error": "api_key_storage_failed",
                    "code": exc.code,
                    "message": str(exc),
                    "api_key_status": cfg.api_key_public_status(),
                }
            ), 500
        changed.append("LLM_API_KEY")

    # ── Update in-memory config ────────────────────────────
    for key in ("LLM_ENDPOINT", "LLM_MODEL", "SPSS_PYTHON_PATH", "STATS_BACKEND"):
        if key in data and data[key]:
            setattr(cfg, key, data[key])
            changed.append(key)
    if data.get("SPSS_PATH"):
        cfg.SPSS_EXECUTABLE = str(data["SPSS_PATH"])
        changed.append("SPSS_PATH")
    if "AI_POLISH_ENABLED" in data:
        cfg.AI_POLISH_ENABLED = data["AI_POLISH_ENABLED"] is True
        changed.append("AI_POLISH_ENABLED")

    # ── Persist to local .env file (never uploaded) ────────
    if changed:
        _save_env_file()

    return jsonify(
        {
            "ok": True,
            "changed": changed,
            "api_key_status": cfg.api_key_public_status(),
        }
    )


def _backup_error_response(exc: SecretProtectionError):
    client_error_codes = {
        "backup_authentication_failed",
        "backup_invalid",
        "backup_invalid_secret",
        "backup_password_mismatch",
        "backup_password_required",
        "backup_password_too_long",
        "backup_password_weak",
        "backup_too_large",
        "backup_unsupported_parameters",
        "backup_unsupported_version",
    }
    status_code = 400 if exc.code in client_error_codes else 409
    return jsonify(
        {
            "error": "api_key_backup_failed",
            "code": exc.code,
            "message": str(exc),
        }
    ), status_code


@app.post("/api/api-key-backup/export")
def export_api_key_backup():
    """Download a portable password-protected backup of the current API key."""

    import snla.config as cfg

    data = request.get_json(silent=True) or {}
    try:
        payload = cfg.export_api_key_backup(
            str(data.get("password") or ""),
            str(data.get("password_confirmation") or ""),
        )
    except SecretProtectionError as exc:
        return _backup_error_response(exc)
    response = send_file(
        BytesIO(payload),
        mimetype="application/vnd.statstalk.key-backup+json",
        as_attachment=True,
        download_name="StatsTalk-api-key-backup.stkb",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/api/api-key-backup/import")
def import_api_key_backup():
    """Restore a portable backup into the current user's DPAPI store."""

    import snla.config as cfg

    backup_file = request.files.get("backup")
    if backup_file is None or not backup_file.filename:
        return jsonify(
            {
                "error": "api_key_backup_failed",
                "code": "backup_file_required",
                "message": "Choose a StatsTalk API-key backup file.",
            }
        ), 400
    payload = backup_file.stream.read(BACKUP_MAX_BYTES + 1)
    try:
        api_key_status = cfg.import_api_key_backup(
            payload,
            str(request.form.get("password") or ""),
        )
    except SecretProtectionError as exc:
        return _backup_error_response(exc)
    return jsonify({"ok": True, "api_key_status": api_key_status})


def _save_env_file():
    """Write current config values back to .env file for persistence."""
    import snla.config as cfg

    env_path = cfg.CONFIG_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    # Read existing .env, preserving comments and non-managed keys
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            existing = f.read().splitlines()
    else:
        existing = []

    managed = {
        "SPSS_PATH": "SPSS_EXECUTABLE",
        "SPSS_PYTHON_PATH": "SPSS_PYTHON_PATH",
        "LLM_ENDPOINT": "LLM_ENDPOINT",
        "LLM_MODEL": "LLM_MODEL",
        "AI_POLISH_ENABLED": "AI_POLISH_ENABLED",
        "STATS_BACKEND": "STATS_BACKEND",
        "LLM_MOCK": "LLM_MOCK",
    }
    updated = set()
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in managed:
                v = getattr(cfg, managed[k], "")
                # Mask sensitive values
                lines.append(f"{k}={v}")
                updated.add(k)
            else:
                lines.append(line)
        else:
            lines.append(line)

    # Append any managed keys not found in existing file
    for k in set(managed) - updated:
        v = getattr(cfg, managed[k], "")
        if v:
            lines.append(f"{k}={v}")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ── Config Hot-Reload ─────────────────────────────────────────────────
@app.route("/api/reload-config", methods=["POST"])
def reload_config():
    """Reload configuration from .env file without restarting."""
    try:
        from snla.config import reload_config as _reload

        changed = _reload()
        return jsonify(
            {
                "ok": True,
                "reloaded": True,
                "changed": changed,
            }
        )
    except Exception:
        logger.exception("Config reload failed")
        return jsonify({"ok": False, "error": "Config reload failed"}), 500


# ── Model List ────────────────────────────────────────────────────────
@app.route("/api/models", methods=["POST"])
def list_models():
    """Fetch available model list from the LLM API endpoint.

    Accepts {endpoint, api_key} in the request body so the frontend can
    query models without saving settings first.
    """
    data = request.get_json(force=True)
    endpoint = (data.get("endpoint") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    if not endpoint:
        return jsonify({"error": "LLM endpoint is required"}), 400
    if not api_key:
        import snla.config as cfg

        api_key_status = cfg.api_key_public_status()
        if api_key_status["state"] == "reenter_required":
            return jsonify(
                {
                    "error": "cloud_features_disabled",
                    "code": "api_key_reentry_required",
                    "message": api_key_status["message"],
                    "api_key_status": api_key_status,
                }
            ), 409
        api_key = cfg.LLM_API_KEY
        if not api_key:
            return jsonify({"error": "API key is required"}), 400
    try:
        endpoint = require_secure_llm_endpoint(endpoint)
    except EndpointPolicyError as exc:
        return jsonify(
            {
                "error": "invalid_llm_endpoint",
                "code": exc.code,
                "message": str(exc),
            }
        ), 400

    # Normalise endpoint to /models path
    base = endpoint.rstrip("/")
    if "/chat/completions" in base:
        base = base.rsplit("/chat/completions", 1)[0]
    if base.endswith("/v1"):
        models_url = base + "/models"
    elif base.endswith("/go/v1"):
        # OpenCode Go convention
        models_url = base + "/models"
    else:
        models_url = base.rstrip("/") + "/v1/models"

    try:
        import ssl
        import urllib.request

        req = urllib.request.Request(models_url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(
            NoRedirectHandler(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        resp = opener.open(req, timeout=10)
        body = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        diagnostic = diagnose_transport_failure(models_url, exc)
        logger.warning("Failed to list models: %s", diagnostic.message)
        return jsonify(
            {
                "error": "model_endpoint_failed",
                "code": diagnostic.code,
                "message": diagnostic.message,
            }
        ), 502
    except Exception:
        logger.exception("Failed to list models")
        return jsonify(
            {
                "error": "model_endpoint_failed",
                "code": "transport_failed",
                "message": "The model list request failed before a valid response was received.",
            }
        ), 502

    # Extract model IDs (OpenAI-compatible format: {"data": [{"id": "..."}, ...]})
    models = []
    for item in body.get("data", []):
        model_id = item.get("id", "")
        if model_id:
            models.append(model_id)
    models.sort()
    return jsonify({"ok": True, "models": models})


# ── SPSS Auto-detect ───────────────────────────────────────────────────
@app.route("/api/detect-spss")
def detect_spss():
    """Detect SPSS executables without inspecting license state."""

    return jsonify({"ok": True, "candidates": _find_spss_candidates()})


def _find_spss_candidates(search_roots=None):
    """Return installed ``stats.com`` commands and optional Python runtimes."""

    roots = search_roots or [
        r"C:\Program Files\IBM\SPSS\Statistics",
        r"C:\Program Files (x86)\IBM\SPSS\Statistics",
    ]
    candidates = []
    seen = set()
    for root in roots:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        try:
            version_dirs = [entry for entry in root_path.iterdir() if entry.is_dir()]
        except PermissionError:
            continue
        for version_dir in version_dirs:
            executable = version_dir / "stats.com"
            if not executable.is_file():
                continue
            normalized = str(executable.resolve()).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            candidate = {
                "version": version_dir.name,
                "path": str(executable.resolve()),
            }
            python_path = version_dir / "Python3" / "python.exe"
            if python_path.is_file():
                candidate["python_path"] = str(python_path.resolve())
            candidates.append(candidate)
    candidates.sort(key=lambda c: c["version"], reverse=True)
    return candidates


# ── Export ────────────────────────────────────────────────────────────
@app.route("/api/export")
def export():
    """Generate and download Word report."""
    if not session.history:
        return jsonify({"error": "No analysis to export"}), 400

    try:
        import io
        import os
        import tempfile

        from snla.explainer.export import export_to_docx as export_word_report

        last = next((m for m in reversed(session.history) if m["role"] == "assistant"), None)
        if not last:
            return jsonify({"error": "No analysis found"}), 400
        last_user = next((m for m in reversed(session.history) if m["role"] == "user"), None)

        # export_to_docx writes to a file path — use a temp file
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = tmp.name

        export_word_report(
            output_path=tmp_path,
            user_query=last_user["content"] if last_user else "",
            method=last.get("method", "unknown"),
            analysis_result=last.get("result"),
            explanation=last.get("content", ""),
            data_file=session.dataset_meta.get("filename", ""),
            parameters=last.get("parameters", {}),
            backend=last.get("backend", ""),
        )

        with open(tmp_path, "rb") as f:
            buf = io.BytesIO(f.read())
        os.unlink(tmp_path)

        from flask import send_file

        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name="snla_report.docx",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from snla.ui.launch import prepare_loopback_server

    waitress_server, launch = prepare_loopback_server(app)
    print(f"StatsTalk API bootstrap URL (one use only): {launch.bootstrap_url}")
    waitress_server.run()
