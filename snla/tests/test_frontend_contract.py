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
