"""StatsTalk Desktop Launcher — PyWebView Edition.

Starts an embedded Flask server, then opens a native desktop window
via the system WebView (Edge WebView2 on Windows). No browser needed.
"""
import os
import sys
import threading
import time
import socket
import webbrowser

from snla.ui.server import app as flask_app


def _wait_for_port(port: int, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.3)
    return False


def main():
    port = 8501
    url = f"http://127.0.0.1:{port}"

    print("=" * 50)
    print("  StatsTalk")
    print("=" * 50)
    print(f"  Starting server on port {port}...")

    # Start Flask in a daemon thread
    def _run_flask():
        flask_app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    server_thread = threading.Thread(target=_run_flask, daemon=True)
    server_thread.start()

    print("  Waiting for server...")
    if not _wait_for_port(port):
        print("  ERROR: Server failed to start within 30 seconds.")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"  Server ready at {url}")
    print("=" * 50)

    # ── PyWebView native window ──────────────────────────────────────
    try:
        import webview
        print("  Opening desktop window...")
        webview.create_window(
            "StatsTalk",
            url,
            width=1100,
            height=800,
            min_size=(800, 600),
            resizable=True,
            text_select=True,
        )
        webview.start()
    except ImportError:
        print("  pywebview not installed. Opening in browser instead...")
        webbrowser.open(url)
        print("  Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print("Goodbye.")


if __name__ == "__main__":
    main()
