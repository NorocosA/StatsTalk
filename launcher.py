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

APP_VERSION = "0.9.0-beta"


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
        webview.create_window(
            "StatsTalk",
            launch.bootstrap_url,
            width=1100,
            height=800,
            min_size=(800, 600),
            resizable=True,
            text_select=True,
        )
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
        waitress_server.close()

    print("Goodbye.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
