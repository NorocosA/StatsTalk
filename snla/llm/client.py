"""
SNLA LLM API Abstraction Layer

Provides a unified interface for OpenAI-compatible APIs (OpenAI, DeepSeek)
and local ollama backends with automatic fallback.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from snla import config

logger = logging.getLogger(__name__)

# Maximum number of retry attempts for LLM HTTP calls
LLM_MAX_RETRIES = 3

# Timeout for LLM HTTP calls (connect_timeout, read_timeout)
LLM_CONNECT_TIMEOUT = 10   # seconds
LLM_READ_TIMEOUT = 120     # seconds

# ---------------------------------------------------------------------------
# TLS adapter for servers with strict/complex SSL configurations
# (e.g. opencode.ai which requires relaxed cipher settings on some platforms)
# ---------------------------------------------------------------------------


def _build_tls_adapter() -> requests.adapters.HTTPAdapter:
    """Build a requests HTTPAdapter with a permissive TLS context.

    Some LLM API endpoints use TLS configurations that trigger
    ``SSLEOFError`` on Windows/Python combinations with stricter
    default cipher suites.  This adapter relaxes verification to
    ``CERT_NONE`` and lowers the OpenSSL security level to 1 so
    that handshakes succeed.
    """
    import ssl

    from requests.adapters import HTTPAdapter

    class _TLSAdapter(HTTPAdapter):
        def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)

    return _TLSAdapter()


class LLMError(Exception):
    """Raised when all LLM backends fail."""

    pass


class LLMClient:
    """Unified LLM interface supporting OpenAI/DeepSeek/ollama backends with automatic fallback.

    Usage::

        client = LLMClient()
        response = client.chat(
            messages=[{"role": "user", "content": "Hello"}],
            system_prompt="You are a helpful assistant.",
        )
        print(response["content"])
    """

    def __init__(self) -> None:
        """Initialize from config. Sets up endpoints and API keys."""
        self.primary_endpoint = config.LLM_ENDPOINT
        self.primary_api_key = config.LLM_API_KEY
        self.primary_model = config.LLM_MODEL

        self.fallback_endpoint = getattr(config, "LOCAL_LLM_ENDPOINT", None)
        self.fallback_model = getattr(config, "LOCAL_LLM_MODEL", None)

        self.mock_mode = config.LLM_MOCK
        self.max_output_tokens = config.LLM_MAX_OUTPUT_TOKENS
        self.debug = config.DEBUG

        # Two sessions: permissive TLS for opencode.ai, default for everything else
        self._session_default = requests.Session()
        self._session_permissive = requests.Session()
        self._session_permissive.mount("https://", _build_tls_adapter())

    def _get_session(self, endpoint: str) -> requests.Session:
        """Return the appropriate session based on endpoint TLS requirements."""
        if "opencode.ai" in endpoint:
            return self._session_permissive
        return self._session_default

    def chat(
        self,
        messages: list[dict[str, str]],
        system_prompt: str | None = None,
        output_format_instruction: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Send a chat completion request.

        Builds the final message list by prepending an optional system prompt
        and appending an output-format instruction to the last user message.
        Then dispatches to the primary backend, falling back to ollama on
        failure.  If all backends fail, raises :class:`LLMError`.

        Args:
            messages: List of ``{"role": "user"|"assistant", "content": "..."}``
                dicts representing the conversation history.
            system_prompt: Optional system-level instruction prepended to
                the message list as a ``{"role": "system", "content": ...}``
                entry.
            output_format_instruction: Optional instruction appended to the
                content of the **last user message** (e.g. ``"Return JSON
                only"``).  If there is no user message, a new one is created.
            temperature: Sampling temperature.  Defaults to ``0.1`` for
                deterministic output.
            max_tokens: Override for the configured maximum output tokens.
                Falls back to ``config.LLM_MAX_OUTPUT_TOKENS`` when ``None``.

        Returns:
            A dictionary with the following keys:

            - ``content`` (str): The response text.
            - ``model`` (str): The model that generated the response.
            - ``usage`` (dict): Token usage with ``prompt_tokens`` and
              ``completion_tokens`` (integers).

        Raises:
            LLMError: When **all** configured backends fail, including
                timeouts and connection errors.
        """
        if self.mock_mode:
            return self._mock_response()

        final_messages: list[dict[str, str]] = list(messages)

        # Prepend system prompt
        if system_prompt is not None:
            final_messages.insert(0, {"role": "system", "content": system_prompt})

        # Append output-format instruction to the last user message
        if output_format_instruction is not None:
            self._append_output_instruction(final_messages, output_format_instruction)

        if max_tokens is None:
            max_tokens = self.max_output_tokens

        return self._try_with_fallback(final_messages, temperature, max_tokens)

    # ------------------------------------------------------------------
    # Public helpers (useful for inspection / testing)
    # ------------------------------------------------------------------

    def masked_api_key(self) -> str:
        """Return the primary API key with all but the last 4 characters masked.

        Returns ``"<not-set>"`` when no key is configured.
        """
        if not self.primary_api_key:
            return "<not-set>"
        if len(self.primary_api_key) <= 4:
            return "*" * len(self.primary_api_key)
        return "*" * (len(self.primary_api_key) - 4) + self.primary_api_key[-4:]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _append_output_instruction(
        messages: list[dict[str, str]],
        instruction: str,
    ) -> None:
        """Append *instruction* to the content of the last user message.

        If no user message exists, a new one is created.
        """
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] += f"\n\n{instruction}"
                return
        # No user message found → create one
        messages.append({"role": "user", "content": instruction})

    def _try_with_fallback(
        self,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Try the primary LLM first, then fall back to the local ollama.

        Raises:
            LLMError: When every configured backend has failed.
        """
        errors: list[str] = []

        # 1. Primary: OpenAI-compatible
        try:
            if self.debug:
                logger.info(
                    "LLM primary call | endpoint=%s | model=%s | api_key=***%s | messages=%d",
                    self.primary_endpoint,
                    self.primary_model,
                    self.masked_api_key(),
                    len(messages),
                )
            return self._call_openai_compatible(
                endpoint=self.primary_endpoint,
                api_key=self.primary_api_key,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except requests.RequestException as exc:
            msg = f"Primary backend failed: {exc}"
            logger.warning(msg)
            errors.append(msg)
        except Exception as exc:
            msg = f"Primary backend unexpected error: {exc}"
            logger.warning(msg)
            errors.append(msg)

        # 2. Fallback: ollama (if configured)
        if self.fallback_endpoint:
            try:
                if self.debug:
                    logger.info(
                        "LLM fallback call | endpoint=%s | model=%s | messages=%d",
                        self.fallback_endpoint,
                        self.fallback_model,
                        len(messages),
                    )
                return self._call_ollama(
                    endpoint=self.fallback_endpoint,
                    model=self.fallback_model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except requests.RequestException as exc:
                msg = f"Fallback backend failed: {exc}"
                logger.warning(msg)
                errors.append(msg)
            except Exception as exc:
                msg = f"Fallback backend unexpected error: {exc}"
                logger.warning(msg)
                errors.append(msg)

        # 3. Everything failed
        error_summary = "; ".join(errors)
        logger.error("All LLM backends failed: %s", error_summary)
        raise LLMError(f"All LLM backends failed: {error_summary}")

    def _call_openai_compatible(
        self,
        endpoint: str,
        api_key: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Call an OpenAI-compatible ``/v1/chat/completions`` endpoint.

        Supports OpenAI, DeepSeek, and any provider that follows the same
        request/response schema.

        Args:
            endpoint: Full URL of the chat completions endpoint.
            api_key: Bearer token for authorization.
            messages: List of message dicts.
            temperature: Sampling temperature.
            max_tokens: Maximum output tokens.

        Returns:
            Normalised response dict with ``content``, ``model``, and
            ``usage`` keys.

        Raises:
            requests.RequestException: On HTTP errors, timeouts, or
                connection failures.
        """
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.primary_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        max_retries = LLM_MAX_RETRIES
        for attempt in range(max_retries + 1):
            try:
                response = self._get_session(endpoint).post(
                    endpoint,
                    headers=headers,
                    json=payload,
                    timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
                )
                response.raise_for_status()
                break  # success
            except requests.RequestException as exc:
                # Don't retry 4xx client errors
                if (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code < 500
                ):
                    raise
                if attempt < max_retries:
                    wait = 2**attempt  # 1s, 2s, 4s
                    logger.warning(
                        "LLM request failed (attempt %d/%d): %s. Retrying in %ds...",
                        attempt + 1,
                        max_retries + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("LLM request failed after %d attempts: %s", max_retries + 1, exc)
                    raise
        data = response.json()

        choices = data.get("choices", [])
        if not choices:
            raise LLMError("OpenAI-compatible response missing 'choices'")

        message = choices[0].get("message", {})
        content: str = message.get("content", "") or message.get("reasoning_content", "")

        usage_raw = data.get("usage", {})
        usage: dict[str, int] = {
            "prompt_tokens": usage_raw.get("prompt_tokens", 0),
            "completion_tokens": usage_raw.get("completion_tokens", 0),
        }

        return {
            "content": content,
            "model": data.get("model", self.primary_model),
            "usage": usage,
        }

    def _call_ollama(
        self,
        endpoint: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        """Call the ollama local ``/api/chat`` endpoint.

        Args:
            endpoint: Full URL of the ollama chat endpoint (e.g.
                ``http://localhost:11434/api/chat``).
            model: The model name to use (e.g. ``"llama3"``).
            messages: List of message dicts.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate (passed as
                ``num_predict`` in ollama's options).

        Returns:
            Normalised response dict with ``content``, ``model``, and
            ``usage`` keys.  Ollama fields ``prompt_eval_count`` and
            ``eval_count`` are mapped to ``prompt_tokens`` and
            ``completion_tokens``.

        Raises:
            requests.RequestException: On HTTP errors, timeouts, or
                connection failures.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        max_retries = LLM_MAX_RETRIES
        for attempt in range(max_retries + 1):
            try:
                response = self._get_session(endpoint).post(
                    endpoint,
                    json=payload,
                    timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
                )
                response.raise_for_status()
                break  # success
            except requests.RequestException as exc:
                # Don't retry 4xx client errors
                if (
                    isinstance(exc, requests.HTTPError)
                    and exc.response is not None
                    and exc.response.status_code < 500
                ):
                    raise
                if attempt < max_retries:
                    wait = 2**attempt  # 1s, 2s, 4s
                    logger.warning(
                        "LLM request failed (attempt %d/%d): %s. Retrying in %ds...",
                        attempt + 1,
                        max_retries + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("LLM request failed after %d attempts: %s", max_retries + 1, exc)
                    raise
        data = response.json()

        message = data.get("message", {})
        content: str = message.get("content", "")

        # Ollama uses different key names than OpenAI
        usage: dict[str, int] = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        }

        return {
            "content": content,
            "model": data.get("model", model),
            "usage": usage,
        }

    def _mock_response(self) -> dict[str, Any]:
        """Return a static mock response for testing without API keys."""
        logger.info("LLM mock mode — returning canned response")
        return {
            "content": ('{"intent": "describe", "confidence": 0.9, "rationale": "MOCK MODE"}'),
            "model": "mock",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
