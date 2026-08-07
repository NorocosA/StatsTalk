"""Transport-security contracts for configurable LLM endpoints."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://api.example.com/v1/chat/completions",
        "http://localhost.example/v1/chat/completions",
        "http://127.0.0.1.example/v1/chat/completions",
    ),
)
def test_plain_http_is_rejected_for_every_public_host(endpoint):
    from snla.llm.transport import EndpointPolicyError, require_secure_llm_endpoint

    with pytest.raises(EndpointPolicyError) as caught:
        require_secure_llm_endpoint(endpoint)

    assert caught.value.code == "public_http_forbidden"


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://api.example.com/v1/chat/completions",
        "https://custom-provider.example/custom/chat",
        "http://localhost:11434/api/chat",
        "http://127.0.0.1:8080/v1/chat/completions",
    ),
)
def test_public_https_and_loopback_http_are_accepted(endpoint):
    from snla.llm.transport import require_secure_llm_endpoint

    assert require_secure_llm_endpoint(endpoint) == endpoint


@pytest.mark.parametrize("endpoint", ("ftp://api.example.com/model", "file:///tmp/model"))
def test_non_http_transport_schemes_are_rejected(endpoint):
    from snla.llm.transport import EndpointPolicyError, require_secure_llm_endpoint

    with pytest.raises(EndpointPolicyError) as caught:
        require_secure_llm_endpoint(endpoint)

    assert caught.value.code == "unsupported_scheme"


@pytest.mark.parametrize(
    "endpoint",
    ("https:///v1/chat/completions", "https://api.example.com:not-a-port/chat"),
)
def test_malformed_network_endpoints_are_rejected(endpoint):
    from snla.llm.transport import EndpointPolicyError, require_secure_llm_endpoint

    with pytest.raises(EndpointPolicyError) as caught:
        require_secure_llm_endpoint(endpoint)

    assert caught.value.code == "invalid_endpoint"


def test_endpoint_cannot_embed_credentials():
    from snla.llm.transport import EndpointPolicyError, require_secure_llm_endpoint

    with pytest.raises(EndpointPolicyError) as caught:
        require_secure_llm_endpoint("https://user:password@api.example.com/v1/chat")

    assert caught.value.code == "embedded_credentials"
    assert "password" not in str(caught.value)


def test_llm_client_rejects_public_http_before_network_access():
    from snla.llm.client import LLMClient
    from snla.llm.transport import EndpointPolicyError

    with (
        patch("snla.config.LLM_ENDPOINT", "http://api.example.com/v1/chat/completions"),
        patch("snla.config.LLM_API_KEY", "sk-secret"),
        patch("snla.config.LLM_MOCK", False),
        patch("requests.Session.post") as post,
    ):
        client = LLMClient()
        with pytest.raises(EndpointPolicyError) as caught:
            client.chat([{"role": "user", "content": "hello"}])

    assert caught.value.code == "public_http_forbidden"
    post.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    (
        "https://api.example.com/v1/chat/completions",
        "http://127.0.0.1:11434/v1/chat/completions",
    ),
)
def test_llm_client_keeps_certificate_verification_enabled(endpoint):
    from snla.llm.client import LLMClient

    response = MagicMock()
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "model": "test-model",
        "usage": {},
    }

    with (
        patch("snla.config.LLM_ENDPOINT", endpoint),
        patch("snla.config.LLM_API_KEY", "sk-secret"),
        patch("snla.config.LLM_MOCK", False),
        patch("requests.Session.post", return_value=response) as post,
    ):
        result = LLMClient().chat([{"role": "user", "content": "hello"}])

    assert result["content"] == "ok"
    assert post.call_args.kwargs["verify"] is True


def test_llm_client_rejects_redirects_without_following_them():
    import requests

    from snla.llm.client import LLMClient, LLMError

    response = requests.Response()
    response.status_code = 302
    response.headers["Location"] = "http://api.example.com/insecure"

    with (
        patch("snla.config.LLM_ENDPOINT", "https://api.example.com/v1/chat"),
        patch("snla.config.LLM_API_KEY", "sk-secret"),
        patch("snla.config.LLM_MOCK", False),
        patch("requests.Session.post", return_value=response) as post,
        pytest.raises(LLMError) as caught,
    ):
        LLMClient().chat([{"role": "user", "content": "hello"}])

    assert post.call_args.kwargs["allow_redirects"] is False
    assert "redirected" in str(caught.value)
    assert "final HTTPS URL" in str(caught.value)
    assert "http://api.example.com/insecure" not in str(caught.value)


def test_invalid_certificate_has_actionable_secret_safe_diagnostics(caplog):
    import requests

    from snla.llm.client import LLMClient, LLMError

    endpoint = "https://api.example.com/v1/chat?tenant=private"
    api_key = "sk-supersecret"
    certificate_error = requests.exceptions.SSLError(
        "certificate verify failed; leaked input sk-supersecret tenant=private"
    )

    with (
        patch("snla.config.LLM_ENDPOINT", endpoint),
        patch("snla.config.LLM_API_KEY", api_key),
        patch("snla.config.LLM_MOCK", False),
        patch("requests.Session.post", side_effect=certificate_error) as post,
        patch("snla.llm.client.time.sleep"),
        pytest.raises(LLMError) as caught,
    ):
        LLMClient().chat([{"role": "user", "content": "hello"}])

    message = str(caught.value)
    assert "TLS certificate or hostname verification failed for api.example.com" in message
    assert "system clock" in message
    assert api_key not in message
    assert "tenant=private" not in message
    assert api_key not in caplog.text
    assert "tenant=private" not in caplog.text
    assert post.call_count == 1


def test_connection_failure_retries_with_actionable_secret_safe_diagnostics(caplog):
    import requests

    from snla.llm.client import LLM_MAX_RETRIES, LLMClient, LLMError

    endpoint = "https://offline.example.com/v1/chat?workspace=private"
    api_key = "sk-another-secret"
    connection_error = requests.exceptions.ConnectionError(
        "connection refused sk-another-secret workspace=private"
    )

    with (
        patch("snla.config.LLM_ENDPOINT", endpoint),
        patch("snla.config.LLM_API_KEY", api_key),
        patch("snla.config.LLM_MOCK", False),
        patch("requests.Session.post", side_effect=connection_error) as post,
        patch("snla.llm.client.time.sleep"),
        pytest.raises(LLMError) as caught,
    ):
        LLMClient().chat([{"role": "user", "content": "hello"}])

    message = str(caught.value)
    assert "Could not connect to LLM endpoint offline.example.com" in message
    assert "network, proxy, or local model status" in message
    assert api_key not in message
    assert "workspace=private" not in message
    assert api_key not in caplog.text
    assert "workspace=private" not in caplog.text
    assert post.call_count == LLM_MAX_RETRIES + 1


def test_debug_logging_reports_only_api_key_presence(caplog):
    import logging
    from unittest.mock import Mock

    from snla.llm.client import LLMClient

    api_key = "sk-secret-TAIL"
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "model": "test-model",
        "usage": {},
    }
    caplog.set_level(logging.INFO, logger="snla.llm.client")

    with (
        patch("snla.config.LLM_ENDPOINT", "https://api.example.com/v1/chat"),
        patch("snla.config.LLM_API_KEY", api_key),
        patch("snla.config.LLM_MOCK", False),
        patch("snla.config.DEBUG", True),
        patch("requests.Session.post", return_value=response),
    ):
        LLMClient().chat([{"role": "user", "content": "hello"}])

    assert "api_key_configured=True" in caplog.text
    assert api_key not in caplog.text
    assert "TAIL" not in caplog.text
