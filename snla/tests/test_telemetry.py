from __future__ import annotations

import json
import subprocess
import sys

from snla.telemetry import CrashReporter


class FakeClient:
    def __init__(self) -> None:
        self.closed_with = None

    def close(self, timeout=None) -> None:
        self.closed_with = timeout


class FakeSentry:
    def __init__(self) -> None:
        self.init_calls = []
        self.events = []
        self.client = FakeClient()

    def init(self, **kwargs) -> None:
        self.init_calls.append(kwargs)

    def capture_event(self, event) -> str:
        before_send = self.init_calls[-1]["before_send"]
        sanitized = before_send(event, {})
        if sanitized is not None:
            self.events.append(sanitized)
        return "event-id"

    def get_client(self):
        return self.client


def make_reporter(tmp_path, sdk_loader):
    return CrashReporter(
        storage_dir=tmp_path / "telemetry",
        sdk_loader=sdk_loader,
        app_version="0.9.0",
        windows_version=lambda: "11.0.26100",
        backend_type=lambda: "python",
    )


def test_sdk_is_not_loaded_and_install_id_is_not_created_before_consent(tmp_path):
    calls = []
    reporter = make_reporter(tmp_path, lambda: calls.append("loaded"))

    assert reporter.initialize(consented=False, dsn="https://public@sentry.invalid/1") is False

    assert calls == []
    assert reporter.status() == {
        "enabled": False,
        "initialized": False,
        "has_install_id": False,
        "has_preview": False,
    }
    assert not (tmp_path / "telemetry").exists()


def test_importing_desktop_launcher_does_not_import_sentry_sdk():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import launcher; print('sentry_sdk' in sys.modules)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "False"


def test_consent_initializes_hardened_sdk_and_only_then_creates_install_id(tmp_path):
    sdk = FakeSentry()
    reporter = make_reporter(tmp_path, lambda: sdk)

    assert reporter.initialize(consented=True, dsn="https://public@sentry.invalid/1") is True

    assert reporter.status()["has_install_id"] is True
    options = sdk.init_calls[0]
    assert options["send_default_pii"] is False
    assert options["auto_session_tracking"] is False
    assert options["default_integrations"] is False
    assert options["include_local_variables"] is False
    assert options["max_breadcrumbs"] == 0
    assert options["traces_sample_rate"] == 0.0
    assert options["profiles_sample_rate"] == 0.0
    assert callable(options["before_send"])


def test_capture_reconstructs_event_from_allowlist_without_exception_message(tmp_path):
    sdk = FakeSentry()
    reporter = make_reporter(tmp_path, lambda: sdk)
    reporter.initialize(consented=True, dsn="https://public@sentry.invalid/1")

    def analysis_step():
        secret_local = "student-name"
        raise ValueError(f"secret path C:/Users/student/{secret_local}.sav")

    try:
        analysis_step()
    except ValueError as exc:
        reporter.capture_exception(exc)

    preview = reporter.preview_latest()
    assert set(preview) == {
        "exception_type",
        "stacktrace",
        "app_version",
        "windows_version",
        "backend_type",
        "install_id",
    }
    assert preview["exception_type"] == "ValueError"
    assert preview["stacktrace"][-1]["function"] == "analysis_step"
    assert set(preview["stacktrace"][-1]) == {"function", "lineno"}
    serialized = json.dumps(preview)
    assert "student-name" not in serialized
    assert "Users" not in serialized
    assert ".sav" not in serialized

    sent = sdk.events[0]
    assert sent["exception"]["values"][0]["type"] == "ValueError"
    assert sent["exception"]["values"][0]["value"] == ""
    assert "filename" not in json.dumps(sent)
    assert "vars" not in json.dumps(sent)
    assert "request" not in sent
    assert "breadcrumbs" not in sent


def test_before_send_drops_unowned_sdk_events_and_ignores_hostile_payload(tmp_path):
    sdk = FakeSentry()
    reporter = make_reporter(tmp_path, lambda: sdk)
    reporter.initialize(consented=True, dsn="https://public@sentry.invalid/1")
    before_send = sdk.init_calls[0]["before_send"]

    assert before_send({"message": "private", "request": {"data": "raw"}}, {}) is None


def test_withdrawal_discards_queue_and_deletes_local_identifier_and_preview(tmp_path):
    sdk = FakeSentry()
    reporter = make_reporter(tmp_path, lambda: sdk)
    reporter.initialize(consented=True, dsn="https://public@sentry.invalid/1")
    reporter.capture_exception(RuntimeError("private"))

    reporter.withdraw()

    assert sdk.client.closed_with == 0
    assert reporter.status() == {
        "enabled": False,
        "initialized": False,
        "has_install_id": False,
        "has_preview": False,
    }
    assert list((tmp_path / "telemetry").glob("*")) == []


def test_preview_can_be_cleared_without_deleting_install_id(tmp_path):
    sdk = FakeSentry()
    reporter = make_reporter(tmp_path, lambda: sdk)
    reporter.initialize(consented=True, dsn="https://public@sentry.invalid/1")
    reporter.capture_exception(RuntimeError("private"))

    reporter.clear_queued_reports()

    assert reporter.preview_latest() is None
    assert reporter.status()["has_install_id"] is True
    assert sdk.client.closed_with == 0
