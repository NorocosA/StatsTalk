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
    })

# ── File Upload ───────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400

    suffix = Path(f.filename).suffix.lower()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.save(tmp.name)
    tmp.close()

    try:
        df, meta = read_and_extract(tmp.name)
        session.dataset_meta = meta
        session.variables = meta.get("variables", [])
        session.row_count = meta.get("row_count", len(df))

        # Sanitize for cloud safety
        cloud_vars = filter_for_cloud(session.variables)

        return jsonify({
            "ok": True,
            "filename": f.filename,
            "variables": cloud_vars,
            "row_count": session.row_count,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

# ── Analyze ───────────────────────────────────────────────────────────
@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True)
    user_input = data.get("text", "").strip()
    if not user_input:
        return jsonify({"error": "Empty input"}), 400

    if not session.variables:
        return jsonify({"error": "Please upload a data file first"}), 400

    try:
        # 1. Intent
        intent = _call_intent(user_input)

        # 2. Method
        method = _call_method(intent, user_input)

        # 3. Syntax
        syntax, notes = _call_syntax(method)

        # 4. Validate
        from snla.syntax.validator import validate
        validation = validate(syntax, [v["name"] for v in session.variables])
        if not validation["valid"]:
            # Try LLM fix once
            fixed = _llm_fix_syntax(syntax, "; ".join(validation["errors"]))
            if fixed:
                syntax = fixed
                validation = validate(syntax, [v["name"] for v in session.variables])

        if not validation["valid"]:
            return jsonify({
                "error": "Syntax validation failed",
                "syntax": syntax,
                "validation_errors": validation["errors"],
            }), 422

        greylist_warnings = [w for w in validation.get("warnings", []) if "greylist" in w.lower() or "confirm" in w.lower()]

        # 5. Execute
        exec_result = _execute_syntax(syntax)

        if not exec_result.get("success"):
            return jsonify({
                "error": exec_result.get("error", "SPSS execution failed"),
                "syntax": syntax,
            }), 500

        # 6. Parse
        parsed = _parse_output(exec_result, method)

        # 7. Explain
        explanation = _explain(parsed, method)

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
            "intent": intent,
            "syntax": syntax,
        }

        return jsonify({
            "ok": True,
            "method": method,
            "syntax": syntax,
            "greylist_warnings": greylist_warnings,
            "result": parsed,
            "explanation": explanation,
            "intent": intent,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Variables ─────────────────────────────────────────────────────────
@app.route("/api/variables")
def variables():
    cloud_vars = filter_for_cloud(session.variables) if session.variables else []
    return jsonify({
        "variables": cloud_vars,
        "row_count": session.row_count,
        "filename": session.dataset_meta.get("filename", "") if session.dataset_meta else "",
    })

# ── Settings ──────────────────────────────────────────────────────────
@app.route("/api/settings", methods=["POST"])
def settings():
    data = request.get_json(force=True)
    import snla.config as cfg
    changed = []
    for key in ("LLM_ENDPOINT", "LLM_API_KEY", "LLM_MODEL", "SPSS_PYTHON_PATH"):
        if key in data and data[key]:
            setattr(cfg, key, data[key])
            changed.append(key)
    return jsonify({"ok": True, "changed": changed})

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

def _call_intent(user_input: str) -> str:
    """Recognize user intent. Falls back to MOCK if no LLM API key."""
    if LLM_MOCK or not _has_llm():
        return _mock_intent(user_input)

    from snla.llm.client import LLMClient
    from snla.llm.prompts.intent import build_intent_prompt
    cloud_vars = filter_for_cloud(session.variables)
    prompt = build_intent_prompt(user_input, last_analysis=session.last_analysis,
                                 variables=cloud_vars)
    client = LLMClient()
    result = client.chat(prompt)
    return result.get("intent", "unknown")

def _call_method(intent: str, user_input: str) -> str:
    if LLM_MOCK or not _has_llm():
        return _mock_method(intent)

    from snla.llm.client import LLMClient
    from snla.llm.prompts.method import build_method_prompt
    cloud_vars = filter_for_cloud(session.variables)
    prompt = build_method_prompt(intent, cloud_vars, user_input)
    client = LLMClient()
    result = client.chat(prompt)
    method = result.get("recommended_method", "frequencies")
    # Rule-engine validation
    from snla.syntax.templates import validate_method as _validate
    if not _validate(session.variables, method,
                     result.get("grouping_variable"), result.get("test_variable")):
        # Fall back to a safe default
        return "frequencies"
    return method

def _call_syntax(method: str) -> tuple[str, str]:
    if LLM_MOCK or not _has_llm():
        return _syntax_template(method), ""

    from snla.llm.client import LLMClient
    from snla.llm.prompts.syntax import build_syntax_prompt
    cloud_vars = filter_for_cloud(session.variables)
    prompt = build_syntax_prompt(method, cloud_vars)
    client = LLMClient()
    result = client.chat(prompt)
    return result.get("syntax", ""), result.get("notes", "")

def _syntax_template(method: str) -> str:
    from snla.syntax.templates import get_template
    return get_template(method, session.variables)

def _execute_syntax(syntax: str):
    from snla.executor.spss import SPSSExecutor
    executor = SPSSExecutor()
    # Find the data file path — use the last uploaded file
    data_path = session.dataset_meta.get("file_path", "") if session.dataset_meta else ""
    if not data_path and session.dataset_meta:
        # Re-save from in-memory data if available
        pass
    result = executor.run(syntax=syntax, data_path=data_path)
    return {
        "success": result.success,
        "exit_code": result.exit_code,
        "xml_path": result.xml_path,
        "lst_text": result.lst_text,
        "error": result.error_message,
    }

def _parse_output(exec_result: dict, method: str):
    from snla.parser.output import parse
    return parse(
        oms_xml_path=exec_result.get("xml_path"),
        lst_text=exec_result.get("lst_text"),
        analysis_type=method,
    )

def _explain(parsed, method: str) -> str:
    from snla.explainer.naturalize import explain
    return explain(parsed, method)

def _llm_fix_syntax(failed_syntax: str, error_text: str) -> str | None:
    if LLM_MOCK or not _has_llm():
        return None
    try:
        from snla.llm.client import LLMClient
        from snla.llm.prompts.syntax import build_fix_prompt
        cloud_vars = filter_for_cloud(session.variables)
        prompt = build_fix_prompt(failed_syntax, error_text, cloud_vars)
        client = LLMClient()
        result = client.chat(prompt)
        return result.get("syntax")
    except Exception:
        return None

def _has_llm() -> bool:
    from snla.config import LLM_API_KEY
    return bool(LLM_API_KEY)

# ── MOCK fallbacks ────────────────────────────────────────────────────

def _mock_intent(user_input: str) -> str:
    """Keyword-based intent classification when no LLM available."""
    text = user_input.lower()
    if any(w in text for w in ("比较", "差异", "区别", "compare", "vs", "versus", "男女", "t test", "t-test")):
        return "compare_groups"
    if any(w in text for w in ("关系", "相关", "correlation", "回归", "regression", "预测")):
        return "relationship"
    if any(w in text for w in ("描述", "统计", "平均", "describe", "mean", "frequency", "频率", "频数")):
        return "describe"
    if any(w in text for w in ("画", "图", "plot", "chart", "graph", "箱线", "直方")):
        return "visualize"
    return "unknown"

def _mock_method(intent: str) -> str:
    method_map = {
        "compare_groups": "independent_t_test",
        "relationship": "regression",
        "describe": "frequencies",
        "visualize": "frequencies",
    }
    return method_map.get(intent, "frequencies")

# ── Main ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8501, debug=DEBUG)
