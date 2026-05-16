"""SNLA Standalone Launcher — double-click to run.

Starts the Streamlit server and opens the default browser.
No command-line required.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser


def find_streamlit() -> str:
    """Find the streamlit executable in the current environment."""
    # Check alongside this exe (PyInstaller bundle)
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__)
    candidates = [
        os.path.join(base, "streamlit.exe"),
        os.path.join(base, "Scripts", "streamlit.exe"),
        os.path.join(sys.prefix, "Scripts", "streamlit.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # Fallback to PATH
    return "streamlit"


def main():
    app_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "snla", "ui", "streamlit_app.py",
    )

    if not os.path.exists(app_path):
        print(f"ERROR: Streamlit app not found at {app_path}")
        print("Make sure this launcher is in the project root directory.")
        input("Press Enter to exit...")
        sys.exit(1)

    streamlit_exe = find_streamlit()

    print("=" * 50)
    print("  SPSS Natural Language Assistant")
    print("=" * 50)
    print()
    print(f"  Starting server...")
    print(f"  App: {app_path}")
    print()

    # Start streamlit in a subprocess
    proc = subprocess.Popen(
        [streamlit_exe, "run", app_path,
         "--server.headless", "true",
         "--browser.serverAddress", "localhost"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    # Wait for server to be ready, then open browser
    url = "http://localhost:8501"

    def _wait_and_open():
        for line in proc.stdout:
            if "You can now view" in line or "Network URL" in line:
                webbrowser.open(url)
                break

    threading.Thread(target=_wait_and_open, daemon=True).start()

    print(f"  Opening browser to {url} ...")
    print()
    print("  Press Ctrl+C to stop the server.")
    print("=" * 50)

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
