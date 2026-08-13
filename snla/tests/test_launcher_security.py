"""Tests for secure loopback server startup."""

import os
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from snla.ui.security import LoopbackSecurity
from snla.ui.server import app


def test_launcher_version_probe_does_not_start_server(capsys, monkeypatch):
    import launcher

    def fail_if_called(*args, **kwargs):
        raise AssertionError("version probe must not initialize the server")

    monkeypatch.setattr(launcher, "prepare_loopback_server", fail_if_called)

    assert launcher.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "StatsTalk 0.9.0-beta"


def test_launch_uses_os_assigned_loopback_ports_and_fresh_secrets():
    from snla.ui.launch import prepare_loopback_server

    calls = []
    assigned_ports = iter((43125, 43126))

    class FakeServer:
        effective_host = "127.0.0.1"

        def __init__(self):
            self.effective_port = next(assigned_ports)

    def server_factory(application, **kwargs):
        calls.append((application, kwargs))
        return FakeServer()

    security = LoopbackSecurity()
    first_server, first = prepare_loopback_server(
        object(), security=security, server_factory=server_factory
    )
    second_server, second = prepare_loopback_server(
        object(), security=security, server_factory=server_factory
    )

    assert first_server.effective_port == 43125
    assert second_server.effective_port == 43126
    assert all(kwargs["host"] == "127.0.0.1" for _, kwargs in calls)
    assert all(kwargs["port"] == 0 for _, kwargs in calls)
    assert first.origin == "http://127.0.0.1:43125"
    assert second.origin == "http://127.0.0.1:43126"

    first_url = urlsplit(first.bootstrap_url)
    second_url = urlsplit(second.bootstrap_url)
    assert first_url.query == ""
    assert second_url.query == ""
    first_token = parse_qs(first_url.fragment)["bootstrap_token"][0]
    second_token = parse_qs(second_url.fragment)["bootstrap_token"][0]
    assert first_token != second_token


def test_frontend_removes_bootstrap_secret_and_keeps_session_token_per_tab():
    app.config["TESTING"] = True

    with app.test_client() as client:
        response = client.get("/")

    html = response.get_data(as_text=True)
    remove_index = html.index("history.replaceState")
    exchange_index = html.index('window.fetch("/api/bootstrap"')

    assert response.status_code == 200
    assert remove_index < exchange_index
    assert "window.location.hash.slice(1)" in html
    assert "window.location.search" not in html
    assert "history.replaceState(null, document.title, window.location.pathname);" in html
    assert "window.location.pathname + window.location.hash" not in html
    assert "sessionStorage.setItem" in html
    assert 'headers.set("Authorization", `Bearer ${sessionToken}`)' in html
    assert 'apiFetch("/api/upload"' in html


def test_desktop_exit_always_cleans_ephemeral_runtime_data(monkeypatch):
    import launcher

    events = []

    class FakeServer:
        effective_port = 43125

        def run(self):
            return None

        def close(self):
            events.append("server_closed")

    fake_window = SimpleNamespace()
    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: fake_window,
        start=lambda: events.append("webview_closed"),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(
        launcher,
        "prepare_loopback_server",
        lambda _app: (
            FakeServer(),
            SimpleNamespace(
                origin="http://127.0.0.1:43125",
                bootstrap_url="http://127.0.0.1:43125/#token",
            ),
        ),
    )
    monkeypatch.setattr(launcher, "_wait_for_port", lambda _port: True)
    monkeypatch.setattr(launcher, "cleanup_runtime_data", lambda: events.append("cleaned"))

    assert launcher.main([]) == 0
    assert events[-2:] == ["cleaned", "server_closed"]


def test_desktop_file_dialog_returns_only_an_explicit_user_selection():
    import launcher

    calls = []

    class FakeWindow:
        def create_file_dialog(self, **kwargs):
            calls.append(kwargs)
            return [r"C:\Research\scores.sav"]

    api = launcher.DesktopApi()
    api.attach_window(FakeWindow())

    assert api.choose_dataset() == r"C:\Research\scores.sav"
    assert calls == [
        {
            "allow_multiple": False,
            "file_types": ("StatsTalk datasets (*.sav;*.csv;*.xlsx)",),
        }
    ]


def test_portable_marker_routes_application_data_beside_executable(tmp_path, monkeypatch):
    import launcher
    from snla.secrets import application_data_directory

    executable = tmp_path / "StatsTalk.exe"
    executable.touch()
    (tmp_path / "portable.marker").touch()
    monkeypatch.delenv("STATSTALK_PORTABLE_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    try:
        launcher._configure_portable_data()
        assert application_data_directory() == (tmp_path / "Data").resolve()
    finally:
        os.environ.pop("STATSTALK_PORTABLE_DATA_DIR", None)


def test_webview_failure_uses_fresh_token_protected_browser_fallback(monkeypatch):
    import launcher

    opened = []
    events = []

    class FakeServer:
        effective_port = 43125

        def run(self):
            return None

        def close(self):
            events.append("closed")

    fake_webview = SimpleNamespace(
        create_window=lambda *args, **kwargs: SimpleNamespace(),
        start=lambda: (_ for _ in ()).throw(RuntimeError("WebView2 unavailable at C:\\Users\\x")),
    )
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    monkeypatch.setattr(
        launcher,
        "prepare_loopback_server",
        lambda _app: (
            FakeServer(),
            SimpleNamespace(
                origin="http://127.0.0.1:43125",
                bootstrap_url="http://127.0.0.1:43125/#bootstrap_token=old",
            ),
        ),
    )
    monkeypatch.setattr(launcher, "_wait_for_port", lambda _port: True)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(
        launcher.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(launcher, "cleanup_runtime_data", lambda: events.append("cleaned"))

    assert launcher.main([]) == 0
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:43125/#bootstrap_token=")
    assert "old" not in opened[0]
    assert events[-2:] == ["cleaned", "closed"]
