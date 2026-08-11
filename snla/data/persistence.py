"""Legacy session-shadow compatibility helpers.

StatsTalk no longer persists dataset metadata, variables, analysis history, or
working-file paths. The legacy database path remains here only so startup can
remove artifacts created by older releases.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().parents[2] / "snla_session.db"


def save_session(session_state, db_path: str | Path = DB_PATH) -> None:
    """Deprecated no-op retained for third-party import compatibility."""


def load_session(session_state, db_path: str | Path = DB_PATH) -> bool:
    """Never restore legacy session shadows into a new process."""

    return False


def clear_session(db_path: str | Path = DB_PATH) -> None:
    """Remove a legacy session database if it exists."""

    try:
        Path(db_path).unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to clear legacy session database")
