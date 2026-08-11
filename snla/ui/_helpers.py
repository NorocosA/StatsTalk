"""
General-purpose helper functions for the SNLA server.

Extracted from server.py to keep the route module focused on HTTP handling.
These helpers are stateless or access shared state via the server module
(imported lazily to avoid circular imports).
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ── Rate limit constants (used by _check_rate_limit + server route error msg) ──
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 10  # max requests per window


# ── SPSS availability & trust helpers ────────────────────────────────────────


def _spss_available() -> bool:
    """Check if SPSS is available on this machine."""
    from snla.config import check_spss_available

    return check_spss_available()


# ── Rate limit helper ────────────────────────────────────────────────────────


def _check_rate_limit(endpoint: str = "/api/analyze") -> bool:
    """Return True if rate limit exceeded.

    Accesses ``_rate_limit_store`` on the server module via lazy import to
    avoid circular dependencies at module load time.
    """
    import snla.ui.server as _server

    now = time.time()
    timestamps = _server._rate_limit_store.get(endpoint, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    _server._rate_limit_store[endpoint] = timestamps
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        return True
    timestamps.append(now)
    return False
