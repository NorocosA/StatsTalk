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

from snla.config import DEBUG, LLM_MOCK, STATS_BACKEND
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud
from snla.orchestrator import GreylistPending, NoPendingError, planner
from snla.session import SessionState
from snla.trust import get_trusted_methods, is_method_trusted, trust_loaded_from

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

# In-memory session (one user, one session)
session = SessionState()
UI_DIR = Path(__file__).resolve().parent

# ── Concurrency & state guards ───────────────────────────────────────
_executing: bool = False  # True while /api/analyze is running
_active_executor: SPSSExecutor | None = None  # for cancellation
_was_cancelled: bool = False  # True when user requested cancellation

# ── P5-4: SPSS availability & method trust helpers ──────────────────


def _spss_available() -> bool:
    """Check if SPSS is available on this machine."""
    from snla.config import check_spss_available

    return check_spss_available()


def _can_full_interpret(method: str) -> bool:
    """Can we produce a full plain-language explanation for this method?

    Returns True if:
    - SPSS is available (always trust SPSS output), OR
    - The method is in the trusted whitelist (no-SPSS mode)
    """
    if _spss_available():
        return True
    return is_method_trusted(method)


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

    data = request.get_json(force=True)
    user_input = data.get("text", "").strip()
    confirm_greylist = data.get("confirm_greylist", False)

    if not user_input:
        return jsonify({"error": "Empty input"}), 400

    if not session.variables:
        return jsonify({"error": "Please upload a data file first"}), 400

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


# ── Phase 2: Report Interpretation ─────────────────────────────────────
def _phase2_explain(parsed, method: str, user_input: str) -> str:
    """Explain SPSS results using LLM polish when available.

    The constraint layer (explainer/naturalize.py) always runs first
    to enforce statistical correctness.  If an LLM is available, the
    constrained output is polished for readability.
    """
    from snla.explainer.naturalize import explain

    use_llm = _has_llm() and not LLM_MOCK
    if use_llm:
        from snla.llm.client import LLMClient

        return explain(parsed, use_llm_polish=True, llm_client=LLMClient())
    return explain(parsed, use_llm_polish=False)


def _syntax_template(
    method: str, grouping_var: str | None = None, test_var: str | None = None
) -> str:
    from snla.syntax.templates import get_syntax_by_method

    vars_list = session.variables
    if not vars_list:
        return ""

    # Auto-detect only if not explicitly provided by Phase 1
    cat_var = num_var = num_var2 = None
    if grouping_var:
        cat_var = grouping_var
        # Verify it exists in dataset
        if not any(v["name"] == grouping_var for v in vars_list):
            cat_var = None
    if test_var:
        num_var = test_var
        if not any(v["name"] == test_var for v in vars_list):
            num_var = None

    # Skip metadata/ID variables in auto-detection
    _skip_vars = {"id", "ID", "Id", "customerid", "customer_id", "row", "ROW", "case", "CASE"}
    _ok_var = lambda v: v.get("type") == "Numeric" and v["name"] not in _skip_vars

    # Fall back to auto-detection if Phase 1 didn't provide
    if not cat_var or not num_var:
        for v in vars_list:
            if v.get("value_labels") and not cat_var:
                cat_var = v["name"]
            elif _ok_var(v) and not num_var:
                num_var = v["name"]
            elif _ok_var(v) and not num_var2 and v["name"] != num_var:
                num_var2 = v["name"]

    # Default fallback — skip id columns
    _def_num = next(
        (
            v["name"]
            for v in vars_list
            if v.get("type") == "Numeric" and v["name"] not in _skip_vars
        ),
        "score",
    )
    cat = cat_var or (vars_list[0]["name"] if vars_list else "group")
    num = num_var or _def_num

    def _corr_args():
        """Find two numeric variables for correlation (skip id columns)."""
        _skip = {"id", "ID", "Id"}
        nv = [
            v["name"]
            for v in vars_list
            if v.get("type") == "Numeric" and not v.get("value_labels") and v["name"] not in _skip
        ]
        if len(nv) >= 2:
            return {"var1": nv[0], "var2": nv[1]}
        return {"var1": num, "var2": num}

    args_map = {
        "independent_t_test": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "oneway_anova": {"group_var": cat, "test_var": num},
        "paired_t_test": _corr_args(),  # two paired numeric variables (before/after)
        "simple_regression": {"dep_var": num, "indep_var": num_var2 or num},
        "pearson_correlation": _corr_args(),
        "spearman_correlation": _corr_args(),
        "correlations": _corr_args(),
        "chi_square": {"row_var": cat, "col_var": num},
        "crosstabs": {"row_var": cat, "col_var": num},
        "frequencies": {"var": cat if cat else num},
        "descriptives": {"var": num},
        "mann_whitney_u": {"group_var": cat, "test_var": num, "groups": (1, 2)},
        "kruskal_wallis": {"group_var": cat, "test_var": num},
    }
    args = args_map.get(method, {"var": num})
    # Map method aliases to template keys
    template_method = {
        "crosstabs": "chi_square",
        "mann_whitney": "mann_whitney_u",
        "kruskal_wallis": "kruskal_wallis",
    }.get(method, method)
    return get_syntax_by_method(template_method, **args)


def _make_executor():
    """Create a new SPSSExecutor instance (avoids repeated imports)."""
    from snla.executor.spss import SPSSExecutor

    return SPSSExecutor()


def _execute_syntax(syntax: str, executor=None, cancellation_token: bool = False):
    if executor is None:
        executor = _make_executor()
    # Find the data file path — use the last uploaded file
    data_path = session.dataset_meta.get("file_path", "") if session.dataset_meta else ""
    if not data_path and session.dataset_meta:
        # Re-save from in-memory data if available
        pass
    result = executor.run(syntax=syntax, data_path=data_path, cancellation_token=cancellation_token)
    # Read LST file content from path (ExecutionResult has lst_path, not lst_text)
    lst_text = ""
    if result.lst_path and os.path.isfile(result.lst_path):
        try:
            with open(result.lst_path, encoding="utf-8", errors="replace") as f:
                lst_text = f.read()
        except OSError:
            pass
    return {
        "success": result.success,
        "exit_code": result.exit_code,
        "xml_path": result.xml_path,
        "lst_text": lst_text,
        "error": result.error_message,
    }


def _parse_output(exec_result: dict, method: str):
    from snla.parser.output import parse

    # Map method name → parser analysis type
    method_to_analysis = {
        "independent_t_test": "T-TEST",
        "paired_t_test": "T-TEST",
        "oneway_anova": "ANOVA",
        "simple_regression": "REGRESSION",
        "pearson_correlation": "CORRELATIONS",
        "correlations": "CORRELATIONS",
        "spearman_correlation": "CORRELATIONS",
        "chi_square": "CROSSTABS",
        "crosstabs": "CROSSTABS",
        "frequencies": "FREQUENCIES",
        "descriptives": "DESCRIPTIVES",
        "mann_whitney_u": "T-TEST",
        "kruskal_wallis": "ANOVA",
    }
    analysis_type = method_to_analysis.get(method, "UNKNOWN")
    try:
        return parse(
            oms_xml_path=exec_result.get("xml_path"),
            lst_text=exec_result.get("lst_text"),
            analysis_type=analysis_type,
        )
    except (ValueError, RuntimeError, FileNotFoundError):
        from snla.parser.schema import AnalysisResult

        return AnalysisResult(
            analysis_type=analysis_type or method or "UNKNOWN",
            tables=[],
            statistics={},
            notes=["SPSS 执行完成，但未能解析输出。请检查数据文件格式。"],
        )


def _llm_fix_syntax(failed_syntax: str, error_text: str) -> str | None:
    if LLM_MOCK or not _has_llm():
        return None
    try:
        from snla.llm.client import LLMClient

        cloud_vars = filter_for_cloud({"variables": session.variables}).get("variables", [])
        # Build a simple fix prompt inline (no dedicated function needed)
        var_list = "\n".join(f"  - {v['name']} ({v.get('type', '?')})" for v in cloud_vars[:30])
        messages = [
            {"role": "system", "content": "你是 SPSS 语法专家。修正下面的语法错误。"},
            {
                "role": "user",
                "content": (
                    f"以下 SPSS 语法执行失败。\n\n"
                    f"可用变量:\n{var_list}\n\n"
                    f"错误语法:\n{failed_syntax}\n\n"
                    f"错误信息:\n{error_text}\n\n"
                    f'请返回修正后的语法，JSON 格式: {{"syntax": "..."}}'
                ),
            },
        ]
        client = LLMClient()
        result = client.chat(messages)
        try:
            parsed = json.loads(result.get("content", "{}"))
            return parsed.get("syntax")
        except (json.JSONDecodeError, AttributeError):
            return None
    except Exception:
        logger.exception("LLM fix syntax failed")
        return None


def _has_llm() -> bool:
    from snla.config import LLM_API_KEY

    return bool(LLM_API_KEY)


def _load_dataframe():
    """Load the uploaded dataset as a pandas DataFrame for Python backend."""
    file_path = session.dataset_meta.get("file_path", "")
    if not file_path or not os.path.isfile(file_path):
        return None
    suffix = os.path.splitext(file_path)[1].lower()
    try:
        if suffix in (".sav",):
            import pyreadstat

            df, _ = pyreadstat.read_sav(file_path)
            return df
        elif suffix == ".csv":
            import pandas as pd

            return pd.read_csv(file_path)
        else:
            import pandas as pd

            return pd.read_csv(file_path)
    except Exception:
        logger.exception("Failed to load dataframe")
        return None


def _run_python_backend(plan_result, user_input: str) -> dict | None:
    """Try Python backend. Returns response dict on success, None to fall through to SPSS.

    On success (method trusted or untrusted), returns a dict suitable for :func:`jsonify`.
    On failure or when ``STATS_BACKEND != "python"``, returns None so the caller
    proceeds with the SPSS path.
    """
    if STATS_BACKEND != "python":
        return None

    try:
        df = _load_dataframe()
        if df is None:
            logger.warning("Python backend: failed to load dataframe")
            return None

        from snla.executor.python import PythonStatsExecutor

        py_exec = PythonStatsExecutor()
        method = plan_result.method
        plan_explanation = plan_result.plan_explanation
        gvar = plan_result.grouping_variable
        tvar = plan_result.test_variable

        result = py_exec.execute(
            method, df, grouping_var=gvar, test_var=tvar, dep_var=gvar, indep_var=tvar
        )

        # P5-4: Strategy C — no-SPSS + untrusted method → raw numbers only
        if not _can_full_interpret(method):
            session.history.append({"role": "user", "content": user_input})
            session.history.append(
                {"role": "assistant", "content": None, "method": method, "result": result}
            )
            session.last_analysis = {"method": method}
            return {
                "ok": True,
                "method": method,
                "backend": "python",
                "plan_explanation": plan_explanation,
                "result": {
                    "analysis_type": result.analysis_type,
                    "tables": [{"title": t.title, "rows": t.rows} for t in result.tables],
                    "statistics": result.statistics,
                    "n_valid": result.n_valid,
                    "parser_used": result.parser_used,
                },
                "explanation": None,
                "warning": (
                    f"Python 引擎下「{method}」方法的可靠性尚未经 SPSS 交叉验证。"
                    f"以下为原始统计数字，非统计专业人士请谨慎解读。"
                    f"建议安装 SPSS 以获得完整解读。"
                ),
                "limited_mode": True,
                "last_analysis": session.last_analysis,
            }

        # Trusted method or SPSS available → full explanation
        explanation = _phase2_explain(result, method, user_input)
        session.history.append({"role": "user", "content": user_input})
        session.history.append(
            {"role": "assistant", "content": explanation, "method": method, "result": result}
        )
        session.last_analysis = {"method": method}
        return {
            "ok": True,
            "method": method,
            "backend": "python",
            "plan_explanation": plan_explanation,
            "result": {
                "analysis_type": result.analysis_type,
                "tables": [{"title": t.title, "rows": t.rows} for t in result.tables],
                "statistics": result.statistics,
                "n_valid": result.n_valid,
                "parser_used": result.parser_used,
            },
            "explanation": explanation,
            "last_analysis": session.last_analysis,
        }
    except Exception:
        logger.exception("Python backend failed, falling through to SPSS")
        return None


def _prepare_syntax(
    method: str,
    grouping_variable: str | None,
    test_variable: str | None,
    confirm_greylist: bool = False,
    user_input: str = "",
):
    """Generate, validate, and gate SPSS syntax.

    Returns a dict with the following possible keys:

    - ``{"syntax": str, "greylist_warnings": list, "used_template": bool}``
      when syntax is ready to execute.
    - ``{"error": str, "syntax": str, "validation_errors": list}``
      when validation fails after LLM fix + template fallback.
    - ``{"_greylist": True, "syntax": str, "greylist_warnings": list}``
      when the syntax requires user confirmation (already staged in
      ``planner``).  The caller should return the confirmation response
      immediately.

    The caller dispatches based on the dict key presence:

        prep = _prepare_syntax(...)
        if prep.get("error"):
            return jsonify(prep), 422
        if prep.get("_greylist"):
            return jsonify(confirmation_response(prep))
        syntax, warnings, used_template = prep["syntax"], prep["greylist_warnings"], prep["used_template"]
    """
    syntax = _syntax_template(method, grouping_var=grouping_variable, test_var=test_variable)
    used_template = True  # Always template-based now (fast, reliable)

    from snla.syntax.validator import validate

    validation = validate(syntax, [v["name"] for v in session.variables])
    if not validation["valid"]:
        # Try LLM fix once
        fixed = _llm_fix_syntax(syntax, "; ".join(validation["errors"]))
        if fixed:
            syntax = fixed
            validation = validate(syntax, [v["name"] for v in session.variables])
        else:
            # Use pre-built template directly
            syntax = _syntax_template(method)
            validation = validate(syntax, [v["name"] for v in session.variables])
            used_template = True

    if not validation["valid"]:
        return {
            "error": "Syntax validation failed",
            "syntax": syntax,
            "validation_errors": validation["errors"],
        }

    greylist_warnings = [
        w
        for w in validation.get("warnings", [])
        if "greylist" in w.lower() or "confirm" in w.lower()
    ]

    # ── Greylist gate: require explicit confirmation ───────────────
    if greylist_warnings and not confirm_greylist:
        planner.stage_greylist(
            "default",
            GreylistPending(
                syntax=syntax,
                warnings=greylist_warnings,
                method=method,
                user_input=user_input,
            ),
        )
        return {
            "_greylist": True,
            "syntax": syntax,
            "greylist_warnings": greylist_warnings,
        }

    return {
        "syntax": syntax,
        "greylist_warnings": greylist_warnings,
        "used_template": used_template,
    }


def _execute_and_parse(syntax: str, executor, method: str, exec_result: dict | None = None):
    """Execute SPSS syntax and parse the output.

    When *exec_result* is provided (e.g. from a temp-copy execution),
    execution is skipped and the pre-built result is parsed directly.

    Returns:
        ``(exec_result_dict, AnalysisResult)`` on success.
        ``None`` if execution was cancelled by the user
        (caller should return a ``{"cancelled": True}`` response).
    """
    if exec_result is None:
        exec_result = _execute_syntax(
            syntax, executor, cancellation_token=session.cancellation_token
        )

    if _was_cancelled or session.cancellation_token:
        return None

    parsed = _parse_output(exec_result, method)
    return (exec_result, parsed)


# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8501, debug=DEBUG)
