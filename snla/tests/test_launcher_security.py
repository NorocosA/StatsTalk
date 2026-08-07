"""Tests for secure loopback server startup."""

from urllib.parse import parse_qs, urlsplit

from snla.ui.security import LoopbackSecurity
from snla.ui.server import app


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
