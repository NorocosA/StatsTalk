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
from pathlib import Path

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, jsonify, request, send_from_directory

from snla.config import DEBUG, LLM_MOCK  # noqa: F401 — LLM_MOCK imported for test patching
from snla.data.persistence import load_session, save_session
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud
from snla.orchestrator import NoPendingError, planner
from snla.session import SessionState
from snla.trust import get_trusted_methods, trust_loaded_from
from snla.ui._helpers import (
    RATE_LIMIT_MAX_REQUESTS,
    RATE_LIMIT_WINDOW,
    _check_rate_limit,
    _make_executor,
    _spss_available,
)
from snla.ui._pipeline import (
    _execute_and_parse,
    _phase2_explain,
    _prepare_syntax,
    _run_python_backend,
)

logger = logging.getLogger(__name__)

# Ensure root logger has a basic config when running standalone
if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

app = Flask(__name__, static_folder=None)

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

# ── Concurrency & state guards ───────────────────────────────────────
_executing: bool = False  # True while /api/analyze is running
_active_executor: SPSSExecutor | None = None  # for cancellation
_was_cancelled: bool = False  # True when user requested cancellation

# ── Rate limiting ─────────────────────────────────────────────────────
_rate_limit_store: dict[str, list[float]] = {}

# ── CORS for local WebView ────────────────────────────────────────────
@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    return response


# ── Static frontend ───────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(str(UI_DIR), "index.html")


# ── Health ────────────────────────────────────────────────────────────
@app.route("/api/status")
def status():
    import snla.config as cfg

    return jsonify(
        {
            "ok": True,
            "has_data": session.dataset_meta is not None,
            "variable_count": len(session.variables),
            "executing": _executing,
            "spss_available": _spss_available(),
            "current_backend": cfg.STATS_BACKEND,
            "trusted_methods": list(get_trusted_methods()),
            "trust_source": trust_loaded_from(),  # "json" or "embedded"
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
    global _executing, _active_executor, _was_cancelled
    session.cancellation_token = True
    _was_cancelled = True
    if _active_executor is not None:
        try:
            _active_executor.terminate()
        except Exception:
            logger.exception("Failed to terminate executor")
    _executing = False
    planner.cancel_pending("default")
    session.reset_cancellation()
    return jsonify({"ok": True})


# ── Analyze ───────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    global _executing, _active_executor, _was_cancelled

    if _executing:
        return jsonify({"error": "An analysis is already running"}), 409

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
    user_input = data.get("text", "").strip()
    confirm_greylist = data.get("confirm_greylist", False)

    if not isinstance(data.get("text"), str):
        return jsonify({"error": "输入类型无效"}), 400
    if len(user_input) > MAX_QUERY_LENGTH:
        return jsonify({"error": f"输入文本过长（最大 {MAX_QUERY_LENGTH} 字符）"}), 400

    if not user_input:
        return jsonify({"error": "Empty input"}), 400

    if not session.variables:
        return jsonify({"error": "Please upload a data file first"}), 400

    # ── Range expansion (Q1-Q10 → Q1, Q2, ..., Q10) ────────────────
    try:
        from snla.data.range_expander import expand_query

        var_names = [v["name"] for v in session.variables]
        expanded = expand_query(user_input, var_names)
        if expanded != user_input:
            logger.info("Range expanded: %s → %s", user_input, expanded)
            user_input = expanded
    except Exception:
        logger.warning("Range expansion failed, continuing with original input", exc_info=True)

    _executing = True
    _was_cancelled = False
    session.reset_cancellation()
    executor = _make_executor()
    _active_executor = executor
    _degradation: dict | None = None  # populated on template fallback

    try:
        # ── Phase 1: Analysis Planning (intent + method + vars, 1 LLM call) ──
        plan_result = planner.plan(
            "default",
            user_input,
            variables=session.variables,
            dataset_meta=session.dataset_meta,
            last_analysis=session.last_analysis,
        )
        method = plan_result.method
        plan_explanation = plan_result.plan_explanation
        gvar = plan_result.grouping_variable
        tvar = plan_result.test_variable

        # ── Python backend fast path ───────────────────────────────
        py_response = _run_python_backend(plan_result, user_input)
        if py_response is not None:
            save_session(session)
            return jsonify(py_response)

        # ── Syntax generation + validation + greylist gate ────────
        prep = _prepare_syntax(method, gvar, tvar, confirm_greylist, user_input)
        if prep.get("error"):
            return jsonify(
                {
                    "error": prep["error"],
                    "syntax": prep["syntax"],
                    "validation_errors": prep["validation_errors"],
                }
            ), 422
        if prep.get("_greylist"):
            return jsonify(
                {
                    "ok": False,
                    "requires_confirmation": True,
                    "greylist_warnings": prep["greylist_warnings"],
                    "message": (
                        "此操作将修改数据（如 COMPUTE / RECODE / SELECT IF），"
                        "需要在临时副本上执行。请确认是否继续。"
                    ),
                    "syntax": prep["syntax"],
                }
            )
        syntax = prep["syntax"]
        greylist_warnings = prep["greylist_warnings"]
        used_template = prep["used_template"]

        # ── Build degradation info if template was used ────────────
        if used_template:
            _degradation = {
                "method": method,
                "note": (
                    "语法自动修正已用尽，已切换至标准模板语法，可能无法完全匹配您的原始意图。"
                ),
            }

        # 5+6. Execute + cancel check + parse
        result = _execute_and_parse(syntax, executor, method)
        if result is None:
            _was_cancelled = False
            session.reset_cancellation()
            return jsonify({"ok": False, "cancelled": True}), 200
        exec_result, parsed = result

        if not exec_result.get("success"):
            return jsonify(
                {
                    "error": exec_result.get("error", "SPSS execution failed"),
                    "syntax": syntax,
                    "degradation": _degradation,
                }
            ), 500

        # ── Phase 2: Report Interpretation (LLM explains SPSS output) ──
        explanation = _phase2_explain(parsed, method, user_input)

        # Store in history
        session.history.append(
            {
                "role": "user",
                "content": user_input,
            }
        )
        session.history.append(
            {
                "role": "assistant",
                "content": explanation,
                "method": method,
                "syntax": syntax,
                "result": parsed,
            }
        )
        session.last_analysis = {
            "method": method,
            "syntax": syntax,
        }

        save_session(session)

        return jsonify(
            {
                "ok": True,
                "method": method,
                "syntax": syntax,
                "plan_explanation": plan_explanation,
                "greylist_warnings": greylist_warnings,
                "result": parsed,
                "explanation": explanation,
                "degradation": _degradation,
                "last_analysis": session.last_analysis,
            }
        )

    except Exception as e:
        logger.exception("Analysis failed")
        return jsonify({"error": str(e)}), 500
    finally:
        _executing = False
        _active_executor = None
        session.reset_cancellation()


# ── Confirm Greylist ──────────────────────────────────────────────────
@app.route("/api/confirm", methods=["POST"])
def confirm_greylist():
    """Execute a previously-pending greylist operation after user confirmation.

    The frontend calls this after the user clicks "Yes, execute" on the
    greylist confirmation dialog.  The pending greylist details are
    retrieved from ``Planner`` (set by `/api/analyze`).

    Execution happens on a **temporary copy** of the data file so the
    original is never modified.
    """
    global _executing, _active_executor, _was_cancelled

    if _executing:
        return jsonify({"error": "An analysis is already running"}), 409

    try:
        pg = planner.pop_pending("default")
    except NoPendingError:
        return jsonify({"error": "No pending greylist operation"}), 400

    _executing = True
    _was_cancelled = False
    session.reset_cancellation()
    executor = _make_executor()
    _active_executor = executor

    try:
        syntax = pg.syntax
        method = pg.method
        user_input = pg.user_input

        # Execute on temp copy (normalize ExecutionResult → dict for _execute_and_parse)
        data_path = session.dataset_meta.get("file_path", "")
        if data_path:
            raw = executor.execute_on_temp_copy(
                syntax=syntax,
                data_path=data_path,
                cancellation_token=session.cancellation_token,
            )
            exec_result_dict = {
                "success": raw.success,
                "exit_code": raw.exit_code,
                "xml_path": raw.xml_path,
                "lst_text": "",
                "error": raw.error_message or None,
            }
        else:
            exec_result_dict = None

        result = _execute_and_parse(syntax, executor, method, exec_result=exec_result_dict)
        if result is None:
            _was_cancelled = False
            return jsonify({"ok": False, "cancelled": True}), 200
        exec_result, parsed = result

        if not exec_result.get("success"):
            return jsonify(
                {
                    "error": exec_result.get("error", "SPSS execution failed"),
                    "syntax": syntax,
                }
            ), 500
        explanation = _phase2_explain(parsed, method, user_input)

        session.history.append({"role": "user", "content": user_input})
        session.history.append(
            {
                "role": "assistant",
                "content": explanation,
                "method": method,
                "syntax": syntax,
                "result": parsed,
            }
        )
        session.last_analysis = {"method": method, "syntax": syntax}

        save_session(session)

        return jsonify(
            {
                "ok": True,
                "method": method,
                "syntax": syntax,
                "greylist_warnings": pg.warnings,
                "result": parsed,
                "explanation": explanation,
                "temp_copy_note": ("此操作已在数据的临时副本上执行，您的原始数据文件未被修改。"),
                "last_analysis": session.last_analysis,
            }
        )

    except Exception as e:
        logger.exception("Greylist confirmation failed")
        return jsonify({"error": str(e)}), 500
    finally:
        _executing = False
        _active_executor = None
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
    from snla.config import LLM_MOCK, STATS_BACKEND, validate

    raw = validate()
    guidance = []
    for w in raw:
        if "SPSS" in w and LLM_MOCK:
            guidance.append({
                "level": "info",
                "message": "Demo 模式已启用，无需 SPSS 或 API Key。",
                "action": None,
            })
        elif "LLM_API_KEY" in w:
            guidance.append({
                "level": "warning",
                "message": w,
                "action": "settings",
            })
        elif "SPSS" in w:
            guidance.append({
                "level": "info",
                "message": w + " 将自动使用 Python 后端。",
                "action": None,
            })
        else:
            guidance.append({"level": "warning", "message": w, "action": None})

    return jsonify({
        "ok": True,
        "warnings": guidance,
        "llm_mock": LLM_MOCK,
        "spss_available": _spss_available(),
        "backend": STATS_BACKEND,
    })


# ── Settings ──────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    """GET: return current settings.  POST: update + persist to .env."""
    if request.method == "GET":
        import snla.config as cfg

        return jsonify(
            {
                "LLM_ENDPOINT": cfg.LLM_ENDPOINT,
                "LLM_API_KEY": cfg.LLM_API_KEY,
                "LLM_MODEL": cfg.LLM_MODEL,
                "SPSS_PYTHON_PATH": cfg.SPSS_PYTHON_PATH,
                "STATS_BACKEND": cfg.STATS_BACKEND,
            }
        )

    # ── POST: update settings ─────────────────────────────
    data = request.get_json(force=True)
    import snla.config as cfg

    changed = []

    # ── Update in-memory config ────────────────────────────
    for key in ("LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL", "SPSS_PYTHON_PATH", "STATS_BACKEND"):
        if key in data and data[key]:
            setattr(cfg, key, data[key])
            changed.append(key)

    # ── Persist to local .env file (never uploaded) ────────
    if changed:
        _save_env_file()

    return jsonify({"ok": True, "changed": changed})


def _save_env_file():
    """Write current config values back to .env file for persistence."""
    import snla.config as cfg

    env_path = os.path.join(str(PROJECT_ROOT), ".env")
    lines = []
    # Read existing .env, preserving comments and non-managed keys
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            existing = f.read().splitlines()
    else:
        existing = []

    managed = {"SPSS_PYTHON_PATH", "LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL", "STATS_BACKEND"}
    updated = set()
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            lines.append(line)
            continue
        if "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in managed:
                v = getattr(cfg, k, "")
                # Mask sensitive values
                lines.append(f"{k}={v}")
                updated.add(k)
            else:
                lines.append(line)
        else:
            lines.append(line)

    # Append any managed keys not found in existing file
    for k in managed - updated:
        v = getattr(cfg, k, "")
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
        return jsonify({"error": "API key is required"}), 400

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
        import urllib.request

        req = urllib.request.Request(models_url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Content-Type", "application/json")
        resp = urllib.request.urlopen(req, timeout=10)
        body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Read error body for diagnostics
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        # Friendly messages for common failures
        if e.code == 403:
            return jsonify(
                {
                    "error": "API 端点返回 403 禁止访问。该服务可能不支持列出模型，请手动输入模型名称。",
                    "detail": err_body or None,
                }
            ), 502
        if e.code == 404:
            return jsonify(
                {
                    "error": "该 API 端点不支持 /models 接口，请手动输入模型名称。",
                }
            ), 502
        return jsonify(
            {
                "error": f"获取模型列表失败 (HTTP {e.code})。请手动输入模型名称。",
                "detail": err_body or None,
            }
        ), 502
    except Exception as e:
        logger.exception("Failed to list models")
        return jsonify(
            {
                "error": f"无法连接到 API 端点。请检查端点和网络。({e})",
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
    """Auto-detect SPSS Python path by scanning common install locations."""

    candidates = []
    # Common SPSS install roots
    search_roots = [
        r"C:\Program Files\IBM\SPSS\Statistics",
        r"C:\Program Files (x86)\IBM\SPSS\Statistics",
    ]

    for root in search_roots:
        if os.path.isdir(root):
            try:
                for entry in os.scandir(root):
                    if entry.is_dir():
                        py_path = os.path.join(entry.path, "Python3", "python.exe")
                        if os.path.isfile(py_path):
                            candidates.append(
                                {
                                    "version": entry.name,
                                    "path": os.path.abspath(py_path),
                                }
                            )
            except PermissionError:
                pass

    candidates.sort(key=lambda c: c["version"], reverse=True)
    return jsonify({"ok": True, "candidates": candidates})


# ── Export ────────────────────────────────────────────────────────────
@app.route("/api/export")
def export():
    """Generate and download Word report."""
    if not session.history:
        return jsonify({"error": "No analysis to export"}), 400

    try:
        import io

        from snla.explainer.export import export_word_report

        last = next((m for m in reversed(session.history) if m["role"] == "assistant"), None)
        if not last:
            return jsonify({"error": "No analysis found"}), 400

        buf = io.BytesIO()
        export_word_report(
            buf,
            user_query=session.history[-2]["content"] if len(session.history) >= 2 else "",
            method=last.get("method", "unknown"),
            syntax=last.get("syntax", ""),
            explanation=last.get("content", ""),
            result=last.get("result"),
        )
        buf.seek(0)

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
    app.run(host="127.0.0.1", port=8501, debug=DEBUG)
