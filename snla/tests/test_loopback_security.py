"""Security contract tests for the local Flask control plane."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from snla.ui.security import BootstrapError, LoopbackSecurity, loopback_security
from snla.ui.server import app

TEST_ORIGIN = "http://127.0.0.1:43125"


def _start_test_launch() -> str:
    return loopback_security.begin_launch(TEST_ORIGIN)


def test_bootstrap_exchange_before_launch_is_rejected():
    security = LoopbackSecurity()

    with pytest.raises(BootstrapError) as caught:
        security.exchange_bootstrap("never-issued")

    assert caught.value.reason == "invalid_bootstrap_token"


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/settings"),
        ("POST", "/api/upload"),
        ("POST", "/api/analyze"),
        ("GET", "/api/export"),
        ("POST", "/api/models"),
        ("POST", "/api/mcp/control"),
    ),
)
def test_sensitive_route_rejects_a_missing_session_token(method, path):
    app.config["TESTING"] = True
    _start_test_launch()

    with app.test_client() as client:
        response = client.open(path, method=method)

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authentication_required",
        "reason": "missing_token",
    }
    assert response.headers.get("Access-Control-Allow-Origin") is None


def test_sensitive_route_rejects_an_invalid_session_token():
    app.config["TESTING"] = True
    _start_test_launch()

    with app.test_client() as client:
        response = client.get(
            "/api/settings",
            headers={"Authorization": "Bearer forged-token"},
        )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authentication_required",
        "reason": "invalid_token",
    }


def test_bootstrap_token_is_exchanged_for_a_working_session_token():
    app.config["TESTING"] = True
    bootstrap_token = _start_test_launch()

    with app.test_client() as client:
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        session_token = bootstrap.get_json()["session_token"]
        settings = client.get(
            "/api/settings",
            headers={"Authorization": f"Bearer {session_token}"},
        )

    assert bootstrap.status_code == 200
    assert settings.status_code == 200


def test_bootstrap_token_cannot_be_replayed():
    app.config["TESTING"] = True
    bootstrap_token = _start_test_launch()

    with app.test_client() as client:
        first = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        replay = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.get_json() == {
        "error": "bootstrap_failed",
        "reason": "replayed_bootstrap_token",
    }


def test_concurrent_bootstrap_exchange_issues_only_one_session_token():
    security = LoopbackSecurity()
    bootstrap_token = security.begin_launch(TEST_ORIGIN)

    def exchange():
        try:
            return "issued", security.exchange_bootstrap(bootstrap_token)
        except BootstrapError as exc:
            return "rejected", exc.reason

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _index: exchange(), range(4)))

    assert sum(status == "issued" for status, _ in outcomes) == 1
    assert [reason for status, reason in outcomes if status == "rejected"] == [
        "replayed_bootstrap_token",
        "replayed_bootstrap_token",
        "replayed_bootstrap_token",
    ]


def test_expired_session_token_is_rejected(monkeypatch):
    now = [100.0]
    security = LoopbackSecurity(session_ttl_seconds=5, clock=lambda: now[0])
    monkeypatch.setattr("snla.ui.server.loopback_security", security)
    bootstrap_token = security.begin_launch(TEST_ORIGIN)

    with app.test_client() as client:
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        session_token = bootstrap.get_json()["session_token"]
        now[0] += 6
        expired = client.get(
            "/api/settings",
            headers={"Authorization": f"Bearer {session_token}"},
        )

    assert expired.status_code == 401
    assert expired.get_json() == {
        "error": "authentication_required",
        "reason": "expired_token",
    }


def test_new_launch_invalidates_every_existing_session_token():
    app.config["TESTING"] = True
    first_bootstrap_token = _start_test_launch()

    with app.test_client() as client:
        first_bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": first_bootstrap_token},
        )
        old_session_token = first_bootstrap.get_json()["session_token"]
        _start_test_launch()
        response = client.get(
            "/api/settings",
            headers={"Authorization": f"Bearer {old_session_token}"},
        )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": "authentication_required",
        "reason": "invalid_token",
    }


def test_authenticated_cross_origin_request_is_rejected():
    app.config["TESTING"] = True
    bootstrap_token = _start_test_launch()

    with app.test_client() as client:
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        session_token = bootstrap.get_json()["session_token"]
        response = client.get(
            "/api/settings",
            headers={
                "Authorization": f"Bearer {session_token}",
                "Origin": "https://attacker.example",
            },
        )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": "cross_origin_request",
        "reason": "origin_not_allowed",
    }
    assert response.headers.get("Access-Control-Allow-Origin") is None


def test_active_origin_receives_only_an_exact_cors_allowance():
    app.config["TESTING"] = True
    bootstrap_token = _start_test_launch()

    with app.test_client() as client:
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
            headers={"Origin": TEST_ORIGIN},
        )
        session_token = bootstrap.get_json()["session_token"]
        response = client.get(
            "/api/settings",
            headers={
                "Authorization": f"Bearer {session_token}",
                "Origin": TEST_ORIGIN,
            },
        )

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == TEST_ORIGIN
    assert response.headers["Access-Control-Allow-Origin"] != "*"


def test_settings_never_return_the_complete_api_key():
    app.config["TESTING"] = True
    bootstrap_token = _start_test_launch()

    public_status = {
        "state": "configured",
        "configured": True,
        "cloud_available": True,
        "action": None,
        "message": "API key is protected for the current Windows user.",
    }
    with (
        app.test_client() as client,
        patch("snla.config.LLM_API_KEY", "sk-test-secret-value"),
        patch("snla.config.api_key_public_status", return_value=public_status),
    ):
        bootstrap = client.post(
            "/api/bootstrap",
            json={"bootstrap_token": bootstrap_token},
        )
        session_token = bootstrap.get_json()["session_token"]
        response = client.get(
            "/api/settings",
            headers={"Authorization": f"Bearer {session_token}"},
        )

    assert response.status_code == 200
    assert b"sk-test-secret-value" not in response.data
    assert response.get_json()["LLM_API_KEY"] == ""
    assert response.get_json()["LLM_API_KEY_CONFIGURED"] is True
