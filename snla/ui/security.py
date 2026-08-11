"""Per-launch authentication state for the local control plane."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections.abc import Callable


class BootstrapError(ValueError):
    """A bootstrap token could not be exchanged."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LoopbackSecurity:
    """Keep short-lived control-plane credentials in process memory only."""

    def __init__(
        self,
        *,
        session_ttl_seconds: int = 12 * 60 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_ttl_seconds = session_ttl_seconds
        self._clock = clock
        self._bootstrap_digest: bytes | None = None
        self._bootstrap_consumed = False
        self._active_origin: str | None = None
        self._session_expiry_by_digest: dict[bytes, float] = {}
        self._lock = threading.RLock()

    def begin_launch(self, active_origin: str) -> str:
        """Reset all credentials and return a fresh one-time bootstrap token."""

        with self._lock:
            bootstrap_token = secrets.token_urlsafe(32)
            self._active_origin = active_origin
            self._bootstrap_digest = self._digest(bootstrap_token)
            self._bootstrap_consumed = False
            self._session_expiry_by_digest.clear()
            return bootstrap_token

    def exchange_bootstrap(self, token: str) -> str:
        """Consume the launch token and return an in-memory session credential."""

        with self._lock:
            digest = self._digest(token)
            if self._bootstrap_digest is None or not hmac.compare_digest(
                digest, self._bootstrap_digest
            ):
                raise BootstrapError("invalid_bootstrap_token")
            if self._bootstrap_consumed:
                raise BootstrapError("replayed_bootstrap_token")

            self._bootstrap_consumed = True
            session_token = secrets.token_urlsafe(32)
            self._session_expiry_by_digest[self._digest(session_token)] = (
                self._clock() + self._session_ttl_seconds
            )
            return session_token

    def is_origin_allowed(self, origin: str | None) -> bool:
        """Allow non-browser requests or the exact active local origin."""

        if origin is None:
            return True
        with self._lock:
            return self._active_origin is not None and hmac.compare_digest(
                origin, self._active_origin
            )

    def validate_session(self, token: str) -> str | None:
        """Return an authentication failure reason, or ``None`` when valid."""

        with self._lock:
            digest = self._digest(token)
            expires_at = self._session_expiry_by_digest.get(digest)
            if expires_at is None:
                return "invalid_token"
            if expires_at <= self._clock():
                self._session_expiry_by_digest.pop(digest, None)
                return "expired_token"
            return None

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()


loopback_security = LoopbackSecurity()


__all__ = ["BootstrapError", "LoopbackSecurity", "loopback_security"]
