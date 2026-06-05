"""
General-purpose helper functions for the SNLA server.

Extracted from server.py to keep the route module focused on HTTP handling.
These helpers are stateless or access shared state via the server module
(imported lazily to avoid circular imports).
"""

from __future__ import annotations

import logging
import os
import time

from snla.config import LLM_MOCK

logger = logging.getLogger(__name__)

# ── Rate limit constants (used by _check_rate_limit + server route error msg) ──
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 10  # max requests per window


# ── SPSS availability & trust helpers ────────────────────────────────────────


def _spss_available() -> bool:
    """Check if SPSS is available on this machine."""
    from snla.config import check_spss_available

    return check_spss_available()


def _can_full_interpret(method: str) -> bool:
    """Can we produce a full plain-language explanation for this method?

    Returns True if:
    - SPSS is available (always trust SPSS output), OR
    - The method is in the trusted whitelist (no-SPSS mode)
    """
    if _spss_available():
        return True
    from snla.trust import is_method_trusted

    return is_method_trusted(method)


# ── Executor factory ─────────────────────────────────────────────────────────


def _make_executor():
    """Create a new SPSSExecutor instance (avoids repeated imports)."""
    from snla.executor.spss import SPSSExecutor

    return SPSSExecutor()


# ── LLM availability ─────────────────────────────────────────────────────────


def _has_llm() -> bool:
    from snla.config import LLM_API_KEY

    return bool(LLM_API_KEY)


# ── Data loading ─────────────────────────────────────────────────────────────


def _load_dataframe():
    """Load the uploaded dataset as a pandas DataFrame for Python backend."""
    import snla.ui.server as _server

    file_path = _server.session.dataset_meta.get("file_path", "")
    if not file_path or not os.path.isfile(file_path):
        return None
    suffix = os.path.splitext(file_path)[1].lower()
    try:
        if suffix in (".sav",):
            import pyreadstat

            df, _ = pyreadstat.read_sav(file_path)
            return df
        elif suffix == ".csv":
            import pandas as pd

            return pd.read_csv(file_path)
        else:
            import pandas as pd

            return pd.read_csv(file_path)
    except Exception:
        logger.exception("Failed to load dataframe")
        return None


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
