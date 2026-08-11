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
