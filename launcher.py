"""StatsTalk Desktop Launcher — PyWebView Edition.

Starts an embedded Flask server, then opens a native desktop window
via the system WebView (Edge WebView2 on Windows). No browser needed.
"""

import socket
import sys
import threading
import time
import webbrowser

from snla.ui.launch import prepare_loopback_server
from snla.ui.server import app as flask_app
from snla.ui.server import cleanup_runtime_data
from snla.version import APP_VERSION


class DesktopApi:
    """Minimal trusted bridge for choosing an original dataset path."""

    def __init__(self) -> None:
        self._window = None

    def attach_window(self, window) -> None:
        self._window = window

    def choose_dataset(self) -> str | None:
        if self._window is None:
            return None
        selected = self._window.create_file_dialog(
            allow_multiple=False,
            file_types=("StatsTalk datasets (*.sav;*.csv;*.xlsx)",),
        )
        return selected[0] if selected else None


def _wait_for_port(port: int, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            time.sleep(0.3)
    return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print(f"StatsTalk {APP_VERSION}")
        return 0

    from snla import config
    from snla.telemetry import crash_reporter

    crash_reporter.initialize(
        consented=config.CRASH_REPORTING_ENABLED and config.CRASH_REPORTING_DECIDED,
        dsn=config.SENTRY_DSN,
    )
    crash_reporter.install_exception_hooks()

    waitress_server, launch = prepare_loopback_server(flask_app)
    port = int(waitress_server.effective_port)

    print("=" * 50)
    print("  StatsTalk")
    print("=" * 50)
    print("  Starting secure local server...")

    # Start Flask in a daemon thread with waitress (production WSGI)
    def _run_flask():
        waitress_server.run()

    server_thread = threading.Thread(target=_run_flask, daemon=True)
    server_thread.start()

    print("  Waiting for server...")
    if not _wait_for_port(port):
        waitress_server.close()
        print("  ERROR: Server failed to start within 30 seconds.")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"  Server ready at {launch.origin}")
    print("=" * 50)

    # ── PyWebView native window ──────────────────────────────────────
    try:
        import webview

        print("  Opening desktop window...")
        desktop_api = DesktopApi()
        window = webview.create_window(
            "StatsTalk",
            launch.bootstrap_url,
            js_api=desktop_api,
            width=1100,
            height=800,
            min_size=(800, 600),
            resizable=True,
            text_select=True,
        )
        desktop_api.attach_window(window)
        webview.start()
    except ImportError:
        print("  pywebview not installed. Opening in browser instead...")
        webbrowser.open(launch.bootstrap_url)
        print("  Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        cleanup_runtime_data()
        waitress_server.close()

    print("Goodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
