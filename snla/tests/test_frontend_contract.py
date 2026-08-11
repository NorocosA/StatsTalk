"""Browser-facing contracts for the embedded desktop UI."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def test_analyze_and_confirm_render_structured_error_user_message():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")
    helper = re.search(r"function errorMessage\(error\) \{.*?\n\}", html, re.DOTALL)

    assert helper is not None
    script = (
        f"{helper.group(0)}\n"
        f"process.stdout.write(errorMessage({json.dumps({'user_message': '请重新上传数据'})}));"
    )
    rendered = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    ).stdout

    assert rendered == "请重新上传数据"
    assert html.count("esc(errorMessage(data.error))") == 2
    assert "esc(data.error)" not in html


def test_method_correction_modal_requires_an_explicit_accept_or_reject():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert 'data.confirmation_type === "method_correction"' in html
    assert "data.correction_options" in html
    assert 'name = "method-correction"' in html
    assert 'decision: "accept"' in html
    assert 'decision: "reject"' in html
    assert "correction_id" in html


def test_local_analysis_controls_submit_method_roles_and_alpha():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    for control_id in (
        "method-select",
        "first-variable-select",
        "second-variable-select",
        "alpha-input",
        "local-suggest-btn",
    ):
        assert f'id="{control_id}"' in html
    assert 'apiFetch("/api/suggest"' in html
    assert "selection_source: selectionSource" in html
    assert "grouping_variable:" in html
    assert "test_variable:" in html


def test_spss_fallback_notice_preserves_preference_and_offers_explicit_switch():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert 'backendSelect.value = "python"' not in html
    assert "data.fallback_reason && data.fallback_reason.announce" in html
    assert "switchBackendToPython" in html
    assert 'JSON.stringify({STATS_BACKEND: "python"})' in html
    assert "data.backend_restored" in html
    assert html.count("showCorrectionChoices(data.correction_choices)") == 2


def test_results_keep_warnings_in_summary_and_evidence_in_advanced_section():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert "summary.warnings" in html
    assert 'class="result-warnings"' in html
    assert "renderAdvancedResults(data.advanced || {})" in html
    assert '<details class="advanced-results">' in html
    assert "advanced.tables" in html
    assert "advanced.syntax" in html


def test_frontend_offers_word_and_json_exports():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert 'id="export-btn"' in html
    assert 'id="export-json-btn"' in html
    assert 'apiFetch("/api/export?format=json")' in html


def test_mcp_is_explicit_opt_in_with_local_data_disclosure():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert 'id="s-mcp-enabled" type="checkbox"' in html
    assert 'id="mcp-disclosure"' in html
    for disclosure in ("明确选择的数据文件", "变量结构", "统计结果", "重启 MCP 服务"):
        assert disclosure in html
    assert 'MCP_ENABLED: document.getElementById("s-mcp-enabled").checked' in html
    assert "data.MCP_ENABLED === true" in html


def test_api_key_backup_ui_requires_password_and_never_handles_plaintext_key():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    for control_id in (
        "export-api-key-backup",
        "import-api-key-backup",
        "api-key-backup-file",
        "secret-backup-password",
        "secret-backup-confirmation",
    ):
        assert f'id="{control_id}"' in html
    assert 'type="password"' in html
    assert "备份密码无法找回" in html
    assert 'apiFetch("/api/api-key-backup/export"' in html
    assert 'apiFetch("/api/api-key-backup/import"' in html
    assert 'form.append("password", password)' in html
    backup_script = html[
        html.index("function openSecretBackupModal") : html.index("// ── Model Fetch")
    ]
    assert "LLM_API_KEY" not in backup_script


def test_ai_polish_is_explicit_opt_in_with_disclosure_and_separate_output():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    assert 'id="s-ai-polish" type="checkbox"' in html
    assert 'id="ai-polish-disclosure"' in html
    for disclosure in ("原始数据", "变量名", "标签", "文件路径"):
        assert disclosure in html
    assert 'AI_POLISH_ENABLED: document.getElementById("s-ai-polish").checked' in html
    assert "data.AI_POLISH_ENABLED === true" in html
    assert "event.target.checked && !confirm(" in html
    assert "data.ai_polish" in html
    assert "data.explanation" in html
    assert 'class="ai-polish-result"' in html


def test_dataset_restore_is_opt_in_and_requires_startup_confirmation():
    html = (Path(__file__).parents[1] / "ui" / "index.html").read_text(encoding="utf-8")

    for control_id in (
        "s-session-restore",
        "inspect-local-data",
        "clear-local-data",
        "choose-dataset-btn",
    ):
        assert f'id="{control_id}"' in html
    assert "SESSION_RESTORE_ENABLED" in html
    assert "window.pywebview.api.choose_dataset()" in html
    assert 'apiFetch("/api/open-local-dataset"' in html
    assert 'apiFetch("/api/restore-dataset"' in html
    assert 'apiFetch("/api/local-data"' in html
    assert 'method: "DELETE"' in html
    assert 'status.restore.state === "pending"' in html
    assert "confirm(" in html
