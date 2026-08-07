"""Provider-neutral transport policy for configurable LLM endpoints."""

from __future__ import annotations

import ssl
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlsplit

import requests

_HTTP_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


class EndpointPolicyError(ValueError):
    """An LLM endpoint violates the product transport policy."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransportDiagnostic:
    """A secret-safe transport failure suitable for logs and user errors."""

    code: str
    message: str
    retryable: bool


class LLMTransportError(RuntimeError):
    """A network exception converted to an actionable safe diagnostic."""

    def __init__(self, diagnostic: TransportDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


def require_secure_llm_endpoint(endpoint: str) -> str:
    """Return ``endpoint`` when its scheme and host satisfy transport policy."""

    parsed = urlsplit(endpoint)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise EndpointPolicyError(
            "unsupported_scheme",
            "LLM endpoints must use HTTPS, except for loopback models over HTTP.",
        )
    try:
        host = parsed.hostname
        _port = parsed.port
    except ValueError as exc:
        raise EndpointPolicyError(
            "invalid_endpoint",
            "The LLM endpoint must contain a valid host and port.",
        ) from exc
    if not host:
        raise EndpointPolicyError(
            "invalid_endpoint",
            "The LLM endpoint must contain a valid host and port.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise EndpointPolicyError(
            "embedded_credentials",
            "Credentials must be configured separately from the LLM endpoint URL.",
        )
    if scheme == "http" and host not in _HTTP_LOOPBACK_HOSTS:
        raise EndpointPolicyError(
            "public_http_forbidden",
            "Public LLM endpoints must use HTTPS; plain HTTP is limited to loopback models.",
        )
    return endpoint


def safe_endpoint_label(endpoint: str) -> str:
    """Return only the host and optional port, excluding credentials and paths."""

    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return "configured endpoint"
    if not host:
        return "configured endpoint"
    return f"{host}:{port}" if port is not None else host


def diagnose_transport_failure(endpoint: str, error: BaseException) -> TransportDiagnostic:
    """Convert requests or urllib failures into secret-safe actionable diagnostics."""

    label = safe_endpoint_label(endpoint)
    reason = getattr(error, "reason", None)
    if isinstance(error, requests.exceptions.SSLError) or isinstance(
        reason, ssl.SSLCertVerificationError
    ):
        return TransportDiagnostic(
            code="tls_verification_failed",
            message=(
                f"TLS certificate or hostname verification failed for {label}. "
                "Check the system clock and provider certificate; certificate checks "
                "cannot be disabled."
            ),
            retryable=False,
        )
    if isinstance(error, (requests.exceptions.Timeout, TimeoutError)):
        return TransportDiagnostic(
            code="connection_timeout",
            message=(
                f"Connection to LLM endpoint {label} timed out. "
                "Check the endpoint address, network, proxy, or local model status."
            ),
            retryable=True,
        )

    status_code = None
    if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
        status_code = error.response.status_code
    elif isinstance(error, urllib.error.HTTPError):
        status_code = error.code
    if status_code is not None:
        return TransportDiagnostic(
            code="http_error",
            message=f"LLM endpoint {label} returned HTTP {status_code}.",
            retryable=status_code >= 500,
        )

    if isinstance(error, (requests.exceptions.ConnectionError, urllib.error.URLError)):
        return TransportDiagnostic(
            code="connection_failed",
            message=(
                f"Could not connect to LLM endpoint {label}. "
                "Check the endpoint address, network, proxy, or local model status."
            ),
            retryable=True,
        )
    return TransportDiagnostic(
        code="transport_failed",
        message=f"The LLM request to {label} failed before a response was received.",
        retryable=False,
    )


__all__ = [
    "EndpointPolicyError",
    "LLMTransportError",
    "TransportDiagnostic",
    "diagnose_transport_failure",
    "require_secure_llm_endpoint",
    "safe_endpoint_label",
]
