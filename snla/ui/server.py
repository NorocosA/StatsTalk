"""
SNLA Flask API Server — PyWebView backend.

Exposes REST endpoints consumed by the WebView frontend.
Reuses all existing snla backend modules (config, llm, syntax, executor, parser, explainer).

Run standalone:  python snla/ui/server.py
Run via launcher: launcher.py spawns this in a thread.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from flask import Flask, request, jsonify, send_from_directory

from snla.config import DEBUG, LLM_MOCK
from snla.session import SessionState
from snla.data.reader import read_and_extract
from snla.data.sanitizer import filter_for_cloud

app = Flask(__name__, static_folder=None)

# In-memory session (one user, one session)
session = SessionState()
UI_DIR = Path(__file__).resolve().parent

# ── Concurrency & state guards ───────────────────────────────────────
_executing: bool = False                   # True while /api/analyze is running
_active_executor: "SPSSExecutor | None" = None  # for cancellation
_pending_greylist: dict | None = None      # {syntax, warnings, method, intent} awaiting confirm
_was_cancelled: bool = False               # True when user requested cancellation

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
    return jsonify({
        "ok": True,
        "has_data": session.dataset_meta is not None,
        "variable_count": len(session.variables),
        "executing": _executing,
    })

# ── File Upload ───────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    suffix = Path(f.filename).suffix.lower()
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

        return jsonify({
            "ok": True,
            "filename": f.filename,
            "variables": cloud_vars,
            "row_count": meta.get("row_count", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Cancel ────────────────────────────────────────────────────────────
@app.route("/api/cancel", methods=["POST"])
def cancel():
    """Cancel the currently running analysis.

    Sets the session cancellation token and terminates the active SPSS
    subprocess (if any).  Returns ``{ok: True}`` even if nothing was
    running — the frontend can safely call this at any time.
    """
    global _executing, _active_executor, _pending_greylist, _was_cancelled
    session.cancellation_token = True
    _was_cancelled = True
    if _active_executor is not None:
        try:
            _active_executor.terminate()
        except Exception:
            pass
    _executing = False
    _pending_greylist = None
    session.reset_cancellation()
    return jsonify({"ok": True})

# ── Analyze ───────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    global _executing, _active_executor, _pending_greylist, _was_cancelled

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
    _degradation: dict | None = None   # populated on template fallback

    try:
        # ── Phase 1: Analysis Planning (intent + method + vars, 1 LLM call) ──
        method, plan_explanation, gvar, tvar = _phase1_plan(user_input)

        # ── Execution: Syntax from template (deterministic, fast) ──
        syntax = _syntax_template(method, grouping_var=gvar, test_var=tvar)
        used_template = True  # Always template-based now (fast, reliable)

        # 4. Validate
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
            _executing = False
            _active_executor = None
            return jsonify({
                "error": "Syntax validation failed",
                "syntax": syntax,
                "validation_errors": validation["errors"],
            }), 422

        greylist_warnings = [w for w in validation.get("warnings", [])
                             if "greylist" in w.lower() or "confirm" in w.lower()]

        # ── Greylist gate: require explicit confirmation ───────────
        if greylist_warnings and not confirm_greylist:
            _pending_greylist = {
                "syntax": syntax,
                "warnings": greylist_warnings,
                "method": method,
                "user_input": user_input,
            }
            _executing = False
            _active_executor = None
            return jsonify({
                "ok": False,
                "requires_confirmation": True,
                "greylist_warnings": greylist_warnings,
                "message": (
                    "此操作将修改数据（如 COMPUTE / RECODE / SELECT IF），"
                    "需要在临时副本上执行。请确认是否继续。"
                ),
                "syntax": syntax,
            })

        # ── Build degradation info if template was used ────────────
        if used_template:
            _degradation = {
                "method": method,
                "note": (
                    "语法自动修正已用尽，已切换至标准模板语法，"
                    "可能无法完全匹配您的原始意图。"
                ),
            }

        # 5. Execute
        exec_result = _execute_syntax(syntax, executor,
                                      cancellation_token=session.cancellation_token)

        if _was_cancelled or session.cancellation_token:
            _executing = False
            _active_executor = None
            _was_cancelled = False
            session.reset_cancellation()
            return jsonify({"ok": False, "cancelled": True}), 200

        if not exec_result.get("success"):
            _executing = False
            _active_executor = None
            return jsonify({
                "error": exec_result.get("error", "SPSS execution failed"),
                "syntax": syntax,
                "degradation": _degradation,
            }), 500

        # 6. Parse
        parsed = _parse_output(exec_result, method)

        # ── Phase 2: Report Interpretation (LLM explains SPSS output) ──
        explanation = _phase2_explain(parsed, method, user_input)

        # Store in history
        session.history.append({
            "role": "user", "content": user_input,
        })
        session.history.append({
            "role": "assistant",
            "content": explanation,
            "method": method,
            "syntax": syntax,
            "result": parsed,
        })
        session.last_analysis = {
            "method": method,
            "syntax": syntax,
        }

        return jsonify({
            "ok": True,
            "method": method,
            "syntax": syntax,
            "plan_explanation": plan_explanation,
            "greylist_warnings": greylist_warnings,
            "result": parsed,
            "explanation": explanation,
            "degradation": _degradation,
            "last_analysis": session.last_analysis,
        })

    except Exception as e:
        traceback.print_exc()
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
    retrieved from ``_pending_greylist`` (set by `/api/analyze`).

    Execution happens on a **temporary copy** of the data file so the
    original is never modified.
    """
    global _executing, _active_executor, _pending_greylist, _was_cancelled

    if _executing:
        return jsonify({"error": "An analysis is already running"}), 409

    if not _pending_greylist:
        return jsonify({"error": "No pending greylist operation"}), 400

    _executing = True
    _was_cancelled = False
    session.reset_cancellation()
    executor = _make_executor()
    _active_executor = executor

    try:
        pg = _pending_greylist
        if not pg:  # Double-check — cancel() may have cleared it
            _executing = False
            _active_executor = None
            return jsonify({"error": "No pending greylist operation"}), 400
        syntax = pg["syntax"]
        method = pg["method"]
        user_input = pg["user_input"]

        # Execute on temp copy
        data_path = session.dataset_meta.get("file_path", "")
        if data_path:
            exec_result = executor.execute_on_temp_copy(
                syntax=syntax, data_path=data_path,
                cancellation_token=session.cancellation_token,
            )
        else:
            exec_result = _execute_syntax(syntax, executor,
                                          cancellation_token=session.cancellation_token)

        if _was_cancelled or session.cancellation_token:
            _executing = False
            _active_executor = None
            _pending_greylist = None
            _was_cancelled = False
            session.reset_cancellation()
            return jsonify({"ok": False, "cancelled": True}), 200

        if not exec_result.get("success"):
            return jsonify({
                "error": exec_result.get("error", "SPSS execution failed"),
                "syntax": syntax,
            }), 500

        parsed = _parse_output(exec_result, method)
        explanation = _phase2_explain(parsed, method, user_input)

        session.history.append({"role": "user", "content": user_input})
        session.history.append({
            "role": "assistant", "content": explanation,
            "method": method, "syntax": syntax, "result": parsed,
        })
        session.last_analysis = {"method": method, "syntax": syntax}

        return jsonify({
            "ok": True,
            "method": method,
            "syntax": syntax,
            "greylist_warnings": pg["warnings"],
            "result": parsed,
            "explanation": explanation,
            "temp_copy_note": (
                "此操作已在数据的临时副本上执行，您的原始数据文件未被修改。"
            ),
            "last_analysis": session.last_analysis,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    finally:
        _executing = False
        _active_executor = None
        _pending_greylist = None
        session.reset_cancellation()
@app.route("/api/variables")
def variables():
    cloud_vars = _cloud_vars() if session.variables else []
    return jsonify({
        "variables": cloud_vars,
        "row_count": session.dataset_meta.get("row_count", 0),
        "filename": session.dataset_meta.get("filename", "") if session.dataset_meta else "",
    })

# ── Settings ──────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    """GET: return current settings.  POST: update + persist to .env."""
    if request.method == "GET":
        import snla.config as cfg
        return jsonify({
            "LLM_ENDPOINT": cfg.LLM_ENDPOINT,
            "LLM_API_KEY": cfg.LLM_API_KEY,
            "LLM_MODEL": cfg.LLM_MODEL,
            "SPSS_PYTHON_PATH": cfg.SPSS_PYTHON_PATH,
        })

    # ── POST: update settings ─────────────────────────────
    data = request.get_json(force=True)
    import snla.config as cfg
    changed = []

    # ── Update in-memory config ────────────────────────────
    for key in ("LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL", "SPSS_PYTHON_PATH"):
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
        with open(env_path, "r", encoding="utf-8") as f:
            existing = f.read().splitlines()
    else:
        existing = []

    managed = {"SPSS_PYTHON_PATH", "LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL"}
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
            return jsonify({
                "error": "API 端点返回 403 禁止访问。该服务可能不支持列出模型，请手动输入模型名称。",
                "detail": err_body or None,
            }), 502
        if e.code == 404:
            return jsonify({
                "error": "该 API 端点不支持 /models 接口，请手动输入模型名称。",
            }), 502
        return jsonify({
            "error": f"获取模型列表失败 (HTTP {e.code})。请手动输入模型名称。",
            "detail": err_body or None,
        }), 502
    except Exception as e:
        return jsonify({
            "error": f"无法连接到 API 端点。请检查端点和网络。({e})",
        }), 502

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
    import glob as _glob

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
                            candidates.append({
                                "version": entry.name,
                                "path": os.path.abspath(py_path),
                            })
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
        from snla.explainer.export import export_word_report
        import io

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
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         as_attachment=True, download_name="snla_report.docx")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Pipeline helpers ──────────────────────────────────────────────────

def _cloud_vars() -> list[dict]:
    """Return cloud-safe variable metadata for LLM prompts."""
    return filter_for_cloud({"variables": session.variables}).get("variables", [])

# ── Phase 1: Analysis Planning ────────────────────────────────────────
def _phase1_plan(user_input: str) -> tuple[str, str, str | None, str | None]:
    """Determine statistical method, plan explanation, and variable mapping.

    Returns:
        (method, plan_explanation, grouping_variable, test_variable)
    """
    if LLM_MOCK or not _has_llm():
        method = _mock_intent(user_input)
        if method not in ("independent_t_test", "paired_t_test", "oneway_anova",
                          "simple_regression", "pearson_correlation",
                          "spearman_correlation", "chi_square", "crosstabs",
                          "frequencies", "descriptives", "mann_whitney_u",
                          "kruskal_wallis"):
            method = "descriptives"
        # MOCK: auto-detect variables (best-effort, no semantic matching)
        cat, num = _auto_detect_vars()
        return method, f"（MOCK 模式）{method}", cat, num

    from snla.llm.client import LLMClient
    cloud_vars = _cloud_vars()
    # Build variable catalog with both name and label for semantic matching
    var_lines = []
    for v in cloud_vars[:30]:
        lbl = v.get("label", "")
        vl = v.get("value_labels", {})
        vl_str = f" [{' '.join(f'{k}={v}' for k,v in list(vl.items())[:5])}]" if vl else ""
        var_lines.append(
            f"  - {v['name']} ({v.get('type','?')})"
            f"{': ' + lbl if lbl else ''}{vl_str}"
        )
    var_catalog = "\n".join(var_lines)

    ds = session.dataset_meta or {}
    row_count = ds.get("row_count", 0)

    prompt = [
        {"role": "system", "content": (
            "你是 SPSS 统计分析专家。根据用户的自然语言问题，选择最合适的"
            "统计方法，并确定对应的变量。\n\n"
            "可用方法: independent_t_test, paired_t_test, oneway_anova, "
            "mann_whitney_u, kruskal_wallis, pearson_correlation, "
            "spearman_correlation, simple_regression, chi_square, "
            "frequencies, descriptives\n\n"
            "规则:\n"
            "- 分组变量(grouping_variable): 必须是分类变量（有值标签的Numeric或String）\n"
            "- 检验变量(test_variable): 必须是连续变量（无值标签的Numeric）\n"
            "- 2组→t检验, 3组+→ANOVA\n"
            "- 非参数检验用于数据不满足正态假设\n"
            "- 仔细匹配变量名和标签的语义含义\n\n"
            "返回 JSON: {\"method\":\"...\", \"plan_explanation\":\"...\", "
            "\"grouping_variable\":\"变量名或null\", \"test_variable\":\"变量名或null\"}"
        )},
        {"role": "user", "content": (
            f"数据集: {row_count} 条记录, {len(cloud_vars)} 个变量\n"
            f"{var_catalog}\n\n"
            f"用户问题: {user_input}\n\n"
            f"请分析用户问题中的关键词，匹配到正确的变量，返回 JSON。"
        )},
    ]

    client = LLMClient()
    try:
        result = client.chat(prompt)
        parsed = json.loads(result.get("content", "{}"))
        method = parsed.get("method", "descriptives")
        plan = parsed.get("plan_explanation", "")
        gvar = parsed.get("grouping_variable")
        tvar = parsed.get("test_variable")
        valid = {"independent_t_test", "paired_t_test", "oneway_anova",
                 "mann_whitney_u", "kruskal_wallis", "pearson_correlation",
                 "spearman_correlation", "simple_regression", "chi_square",
                 "frequencies", "descriptives", "crosstabs"}
        if method not in valid:
            method = "descriptives"
        return method, plan, gvar, tvar
    except Exception:
        return "descriptives", "", None, None


def _auto_detect_vars() -> tuple[str | None, str | None]:
    """Auto-detect categorical and numeric variables from metadata.
    Returns (cat_var, num_var) — picks first categorical and first numeric."""
    cat_var = num_var = None
    for v in session.variables:
        if v.get("value_labels") and not cat_var:
            cat_var = v["name"]
        elif v.get("type") == "Numeric" and not num_var:
            num_var = v["name"]
        if cat_var and num_var:
            break
    return cat_var, num_var


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

def _syntax_template(method: str, grouping_var: str | None = None,
                     test_var: str | None = None) -> str:
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

    # Fall back to auto-detection if Phase 1 didn't provide
    if not cat_var or not num_var:
        for v in vars_list:
            if v.get("value_labels") and not cat_var:
                cat_var = v["name"]
            elif v.get("type") == "Numeric" and not num_var:
                num_var = v["name"]
            elif v.get("type") == "Numeric" and not num_var2 and v["name"] != num_var:
                num_var2 = v["name"]

    cat = cat_var or (vars_list[0]["name"] if vars_list else "group")
    num = num_var or (vars_list[1]["name"] if len(vars_list) > 1 else "score")

    def _corr_args():
        """Find two numeric variables for correlation."""
        nv = [v["name"] for v in vars_list if v.get("type") == "Numeric" and not v.get("value_labels")]
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
    template_method = {"crosstabs": "chi_square", "mann_whitney": "mann_whitney_u",
                       "kruskal_wallis": "kruskal_wallis"}.get(method, method)
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
    result = executor.run(syntax=syntax, data_path=data_path,
                          cancellation_token=cancellation_token)
    # Read LST file content from path (ExecutionResult has lst_path, not lst_text)
    lst_text = ""
    if result.lst_path and os.path.isfile(result.lst_path):
        try:
            with open(result.lst_path, "r", encoding="utf-8", errors="replace") as f:
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
        "independent_t_test": "T-TEST", "paired_t_test": "T-TEST",
        "oneway_anova": "ANOVA", "simple_regression": "REGRESSION",
        "pearson_correlation": "CORRELATIONS", "correlations": "CORRELATIONS",
        "spearman_correlation": "CORRELATIONS",
        "chi_square": "CROSSTABS", "crosstabs": "CROSSTABS",
        "frequencies": "FREQUENCIES", "descriptives": "DESCRIPTIVES",
        "mann_whitney_u": "T-TEST", "kruskal_wallis": "ANOVA",
    }
    analysis_type = method_to_analysis.get(method, "UNKNOWN")
    try:
        return parse(
            oms_xml_path=exec_result.get("xml_path"),
            lst_text=exec_result.get("lst_text"),
            analysis_type=analysis_type,
        )
    except (ValueError, RuntimeError, FileNotFoundError):
        from snla.parser.schema import AnalysisResult, TableResult
        return AnalysisResult(
            analysis_type=analysis_type or method or "UNKNOWN",
            tables=[],
            statistics={},
            notes=["SPSS 执行完成，但未能解析输出。请检查数据文件格式。"]
        )

def _llm_fix_syntax(failed_syntax: str, error_text: str) -> str | None:
    if LLM_MOCK or not _has_llm():
        return None
    try:
        from snla.llm.client import LLMClient
        cloud_vars = _cloud_vars()
        # Build a simple fix prompt inline (no dedicated function needed)
        var_list = "\n".join(
            f"  - {v['name']} ({v.get('type', '?')})"
            for v in cloud_vars[:30]
        )
        messages = [
            {"role": "system", "content": "你是 SPSS 语法专家。修正下面的语法错误。"},
            {"role": "user", "content": (
                f"以下 SPSS 语法执行失败。\n\n"
                f"可用变量:\n{var_list}\n\n"
                f"错误语法:\n{failed_syntax}\n\n"
                f"错误信息:\n{error_text}\n\n"
                f"请返回修正后的语法，JSON 格式: {{\"syntax\": \"...\"}}"
            )},
        ]
        client = LLMClient()
        result = client.chat(messages)
        try:
            parsed = json.loads(result.get("content", "{}"))
            return parsed.get("syntax")
        except (json.JSONDecodeError, AttributeError):
            return None
    except Exception:
        return None

def _has_llm() -> bool:
    from snla.config import LLM_API_KEY
    return bool(LLM_API_KEY)

# ── MOCK fallbacks ────────────────────────────────────────────────────

def _mock_intent(user_input: str) -> str:
    """Keyword-based intent classification (no LLM required).

    Priority order (first match wins) — mirrors the LLM intent categories:
    describe, compare_groups, relationship, visualize, follow_up.
    Also detects sub-types via *suggested_method* for more accurate routing.
    """
    text = user_input.lower()

    # ── 0. Follow-up ──────────────────────────────────────
    follow_up_words = ("换成", "再看看", "那", "如果不是", "改成", "换一个",
                         "改为", "不是这个", "不对", "重新")
    if session.last_analysis and any(w in text for w in follow_up_words):
        return "follow_up"

    # ── 1. Visualize ──────────────────────────────────────
    if any(w in text for w in ("画", "图", "plot", "chart", "graph", "箱线", "直方", "散点",
                                "条形", "饼图", "可视化", "折线", "绘制", "作图")):
        return "visualize"

    # ── 2. Crosstabs / Chi-square (categorical × categorical) ─
    crosstab_words = ("卡方", "交叉表", "列联表", "独立性检验", "是否有关", "是否有关系",
                       "是否相关", "是否独立", "比例", "构成比", "百分比分布")
    if any(w in text for w in crosstab_words):
        return "crosstabs"

    # ── 3. Frequency / count ──────────────────────────────
    freq_words = ("多少人", "几个人", "多少个", "计数", "人数", "频数", "个案数",
                   "几个", "统计一下", "有多少", "占比", "分别有多少")
    if any(w in text for w in freq_words):
        return "frequencies"

    # ── 4. Paired comparison ──────────────────────────────
    paired_words = ("前后", "配对", "培训前", "培训后", "干预前", "干预后",
                     "治疗前", "治疗后", "之前之后", "before", "after",
                     "变化", "改变", "前后测", "有变化吗", "有提升吗", "有改善吗",
                     "第一次", "第二次", "自身对照", "成对")
    if any(w in text for w in paired_words):
        return "paired_t_test"

    # ── 5. Non-parametric — Mann-Whitney ──────────────────
    mw_words = ("非参数.*两组", "mann.*whitney", "曼惠特尼", "秩和.*两组",
                 "不服从正态.*比较", "非正态.*比较", "偏态.*比较",
                 "不符合正态.*差异", "方差不齐.*比较", "等级数据.*比较")
    if any(w in text for w in mw_words):
        return "mann_whitney_u"

    # ── 6. Non-parametric — Kruskal-Wallis ────────────────
    kw_words = ("非参数.*多组", "kruskal.*wallis", "克鲁斯卡尔", "秩和.*多组",
                 "不服从正态.*多组", "非正态.*多组",
                 "不符合正态.*不同", "偏态.*多组", "不满足.*anova", "不满足.*方差")
    if any(w in text for w in kw_words):
        return "kruskal_wallis"

    # ── 7. Group comparison (t-test / ANOVA) ──────────────
    compare_words = ("比较", "差异", "差别", "显著", "compare", "diff",
                      "男生", "女生", "男女", "不同", "区别", "之间",
                      "是否显著", "有无差异", "有没有差别", "有无差别",
                      "是不是不一样", "有没有不同", "哪个更高", "哪个更好",
                      "谁比谁", "t检验", "t测试", "实验组", "对照组",
                      "A组", "B组", "处理组", "两组")
    if any(w in text for w in compare_words):
        # Multi-group detection → ANOVA
        multi_hints = ("各", "不同班", "多个", "三种", "三级", "四组",
                        "几组", "各组", "几个班", "几个组", "不同组",
                        "年级", "班级", "专业", "部门", "地区",
                        "学历", "不同级别", "不同类型", "各类", "各种",
                        "方差分析", "ANOVA", "F检验", "多组比较")
        if any(w in text for w in multi_hints):
            return "oneway_anova"
        return "independent_t_test"

    # ── 8. Relationship (correlation / regression) ────────
    relation_words = ("关系", "相关", "影响", "因素", "预测", "correlation",
                       "regression", "自变量", "因变量", "能否预测",
                       "是否影响", "会不会影响", "决定因素", "解释",
                       "关联", "联系", "正相关", "负相关", "成正比",
                       "随着", "越来越")
    if any(w in text for w in relation_words):
        # Regression hints
        reg_hints = ("预测", "regression", "回归", "影响.*因素", "自变量",
                      "因变量", "能否预测", "解释.*变异", "决定因素",
                      "哪个影响大", "解释力", "R平方", "多元", "多个.*影响")
        if any(w in text for w in reg_hints):
            return "simple_regression"
        # Spearman hints
        spearman_hints = ("spearman", "斯皮尔曼", "等级相关", "秩相关",
                           "不服从正态.*相关", "非参数.*相关", "等级", "排名",
                           "次序", "Likert", "满意度.*级")
        if any(w in text for w in spearman_hints):
            return "spearman_correlation"
        return "pearson_correlation"

    # ── 9. Descriptive (catch-all) ────────────────────────
    describe_words = ("描述", "统计", "平均", "均值", "标准差", "中位数",
                       "describe", "mean", "frequenc", "分布", "概要",
                       "基本情况", "基本特征", "总体情况", "最大值", "最小值",
                       "缺失值", "极差", "汇总", "偏度", "峰度")
    if any(w in text for w in describe_words):
        return "descriptives"

    return "descriptives"  # safe default

def _mock_method(intent: str) -> str:
    """Map intent keyword to recommended statistical method.

    Handles both abstract intent categories (compare_groups, relationship)
    and specific method names returned by the enhanced MOCK classifier.
    """
    # If already a specific method name, use directly
    direct_methods = {
        "independent_t_test", "paired_t_test", "oneway_anova",
        "simple_regression", "chi_square", "frequencies", "descriptives",
        "correlations", "pearson_correlation", "spearman_correlation",
        "mann_whitney_u", "kruskal_wallis", "crosstabs",
    }
    if intent in direct_methods:
        return intent

    # Abstract intent → specific method
    method_map = {
        "compare_groups": "independent_t_test",
        "relationship": "pearson_correlation",
        "describe": "descriptives",
        "visualize": "frequencies",
        "follow_up": "independent_t_test",
    }
    return method_map.get(intent, "descriptives")

# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8501, debug=DEBUG)
