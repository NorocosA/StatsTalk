"""Consent-gated, allowlist-only crash reporting."""

from __future__ import annotations

import importlib
import json
import os
import platform
import re
import threading
import traceback
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import ModuleType

from snla.secrets import application_data_directory
from snla.version import APP_VERSION

_SAFE_FUNCTION = re.compile(r"[^A-Za-z0-9_.<>]")
_MAX_FRAMES = 40


def _load_sentry_sdk() -> ModuleType:
    return importlib.import_module("sentry_sdk")


def _windows_version() -> str:
    return platform.version()


def _backend_type() -> str:
    from snla import config

    if config.STATS_BACKEND == "spss" and not config.check_spss_available():
        return "python"
    return config.STATS_BACKEND


class CrashReporter:
    """Own Sentry lifecycle and ensure private data never reaches its SDK."""

    def __init__(
        self,
        *,
        storage_dir: Path | None = None,
        sdk_loader: Callable[[], object] = _load_sentry_sdk,
        app_version: str = APP_VERSION,
        windows_version: Callable[[], str] = _windows_version,
        backend_type: Callable[[], str] = _backend_type,
    ) -> None:
        self.storage_dir = storage_dir or application_data_directory() / "telemetry"
        self._install_id_path = self.storage_dir / "install_id"
        self._preview_path = self.storage_dir / "latest_event.json"
        self._sdk_loader = sdk_loader
        self._app_version = app_version
        self._windows_version = windows_version
        self._backend_type = backend_type
        self._sdk = None
        self._dsn = ""
        self._enabled = False
        self._initialized = False
        self._pending_event: dict[str, object] | None = None
        self._lock = threading.RLock()
        self._original_sys_hook = None
        self._original_thread_hook = None

    def initialize(self, *, consented: bool, dsn: str) -> bool:
        """Initialize Sentry only after explicit consent."""

        with self._lock:
            if not consented:
                self._enabled = False
                return False
            self._enabled = True
            self._dsn = str(dsn or "").strip()
            self._get_or_create_install_id()
            if not self._dsn:
                return False
            self._sdk = self._sdk_loader()
            self._sdk.init(
                dsn=self._dsn,
                before_send=self._before_send,
                send_default_pii=False,
                auto_session_tracking=False,
                default_integrations=False,
                include_local_variables=False,
                max_breadcrumbs=0,
                traces_sample_rate=0.0,
                profiles_sample_rate=0.0,
            )
            self._initialized = True
            return True

    def capture_exception(self, error: BaseException) -> str | None:
        """Capture only a reconstructed allowlist record, never the exception object."""

        with self._lock:
            if not self._enabled or not self._initialized or self._sdk is None:
                return None
            record = self._build_record(error)
            self._write_preview(record)
            self._pending_event = record
            try:
                return self._sdk.capture_event({"level": "error"})
            finally:
                self._pending_event = None

    def preview_latest(self) -> dict[str, object] | None:
        """Return the exact allowlisted record most recently submitted."""

        try:
            value = json.loads(self._preview_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def clear_queued_reports(self) -> None:
        """Discard the SDK's in-memory queue and local event preview."""

        with self._lock:
            self._close_sdk()
            self._preview_path.unlink(missing_ok=True)
            if self._enabled and self._dsn:
                self.initialize(consented=True, dsn=self._dsn)

    def withdraw(self) -> None:
        """Stop reporting immediately and delete all telemetry identifiers."""

        with self._lock:
            self._enabled = False
            self._close_sdk()
            self._pending_event = None
            self._preview_path.unlink(missing_ok=True)
            self._install_id_path.unlink(missing_ok=True)
            with suppress(OSError):
                self.storage_dir.rmdir()

    def uninstall_cleanup(self) -> None:
        self.withdraw()

    def install_exception_hooks(self) -> None:
        """Capture uncaught exceptions without enabling Sentry's broad integrations."""

        import sys

        if self._original_sys_hook is not None:
            return
        self._original_sys_hook = sys.excepthook

        def sys_hook(error_type, error, error_traceback):
            if error.__traceback__ is None:
                error = error.with_traceback(error_traceback)
            self.capture_exception(error)
            self._original_sys_hook(error_type, error, error_traceback)

        sys.excepthook = sys_hook
        if hasattr(threading, "excepthook"):
            self._original_thread_hook = threading.excepthook

            def thread_hook(args):
                error = args.exc_value
                if error.__traceback__ is None:
                    error = error.with_traceback(args.exc_traceback)
                self.capture_exception(error)
                self._original_thread_hook(args)

            threading.excepthook = thread_hook

    def status(self) -> dict[str, bool]:
        return {
            "enabled": self._enabled,
            "initialized": self._initialized,
            "has_install_id": self._install_id_path.is_file(),
            "has_preview": self._preview_path.is_file(),
        }

    def _before_send(self, _event, _hint):
        with self._lock:
            if not self._enabled or self._pending_event is None:
                return None
            return self._to_sentry_event(self._pending_event)

    def _build_record(self, error: BaseException) -> dict[str, object]:
        frames = traceback.extract_tb(error.__traceback__)[-_MAX_FRAMES:]
        stacktrace = [
            {
                "function": self._sanitize_function(frame.name),
                "lineno": max(0, int(frame.lineno)),
            }
            for frame in frames
        ]
        return {
            "exception_type": self._sanitize_function(type(error).__name__),
            "stacktrace": stacktrace,
            "app_version": self._app_version,
            "windows_version": str(self._windows_version()),
            "backend_type": str(self._backend_type()),
            "install_id": self._get_or_create_install_id(),
        }

    @staticmethod
    def _sanitize_function(value: str) -> str:
        cleaned = _SAFE_FUNCTION.sub("_", str(value))[:120]
        return cleaned or "<unknown>"

    @staticmethod
    def _to_sentry_event(record: dict[str, object]) -> dict[str, object]:
        frames = [
            {"function": frame["function"], "lineno": frame["lineno"]}
            for frame in record["stacktrace"]
        ]
        return {
            "level": "error",
            "exception": {
                "values": [
                    {
                        "type": record["exception_type"],
                        "value": "",
                        "stacktrace": {"frames": frames},
                    }
                ]
            },
            "release": record["app_version"],
            "contexts": {"os": {"name": "Windows", "version": record["windows_version"]}},
            "tags": {
                "backend_type": record["backend_type"],
                "install_id": record["install_id"],
            },
        }

    def _get_or_create_install_id(self) -> str:
        try:
            current = self._install_id_path.read_text(encoding="ascii").strip()
            return str(uuid.UUID(current))
        except (FileNotFoundError, OSError, ValueError):
            install_id = str(uuid.uuid4())
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            self._atomic_write(self._install_id_path, install_id)
            return install_id

    def _write_preview(self, record: dict[str, object]) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(
            self._preview_path,
            json.dumps(record, ensure_ascii=True, separators=(",", ":")),
        )

    @staticmethod
    def _atomic_write(path: Path, value: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)

    def _close_sdk(self) -> None:
        if self._sdk is not None:
            try:
                client = self._sdk.get_client()
                client.close(timeout=0)
            except (AttributeError, RuntimeError):
                pass
        self._sdk = None
        self._initialized = False


crash_reporter = CrashReporter()
