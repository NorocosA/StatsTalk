"""SQLite session persistence for SNLA.

Saves/loads :class:`snla.session.SessionState` fields to a local SQLite file
so desktop restarts don't lose data.  The in-memory session remains the
primary store — SQLite is a shadow copy written on every successful upload
or analysis.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Project-root-relative path so the DB survives working-dir changes.
DB_PATH = Path(os.path.dirname(__file__)).parent.parent / "snla_session.db"


# ── Serialisation helpers ───────────────────────────────────────────────


class _SessionEncoder(json.JSONEncoder):
    """Custom JSON encoder that handles SNLA-specific types."""

    def default(self, obj):
        # AnalysisResult / TableResult dataclasses
        if hasattr(obj, "analysis_type") and hasattr(obj, "tables"):
            from snla.parser.schema import analysis_result_to_dict

            return analysis_result_to_dict(obj)
        if hasattr(obj, "title") and hasattr(obj, "rows"):
            from dataclasses import asdict

            return asdict(obj)
        # datetime objects in history timestamps
        try:
            return obj.isoformat()
        except AttributeError:
            pass
        return super().default(obj)


def _serialise(value) -> str:
    """Convert any session value to a JSON string for SQLite storage."""
    return json.dumps(value, ensure_ascii=False, cls=_SessionEncoder)


def _deserialise(raw: str):
    """Reconstruct a Python value from a JSON string, restoring dataclasses."""
    data = json.loads(raw)
    # Walk into history entries and restore AnalysisResult objects
    if isinstance(data, list):
        return [_restore_analysis_in_dict(item) for item in data]
    if isinstance(data, dict):
        return _restore_analysis_in_dict(data)
    return data


def _restore_analysis_in_dict(item):
    """If *item* looks like a serialised AnalysisResult, reconstruct it."""
    if not isinstance(item, dict):
        return item
    # History assistant entries have a "result" key
    if "result" in item and isinstance(item["result"], dict):
        rd = item["result"]
        if "analysis_type" in rd and "tables" in rd:
            from snla.parser.schema import dict_to_analysis_result

            item = dict(item)
            item["result"] = dict_to_analysis_result(rd)
    return item


# ── Public API ──────────────────────────────────────────────────────────


def save_session(session_state, db_path: str | Path = DB_PATH):
    """Persist key :class:`SessionState` fields to SQLite.

    Called after every successful upload or analysis.
    Only saves fields that are meaningful across restarts:
    ``dataset_meta``, ``variables``, ``var_name_map``,
    ``reverse_var_name_map``, ``history``, ``last_analysis``,
    and ``current_stage``.

    Transient fields (``active_syntax``, ``active_process``,
    ``cancellation_token``, ``temp_files``, ``error_message``)
    are intentionally omitted.
    """
    db_path = str(db_path)
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS session (key TEXT PRIMARY KEY, value TEXT)"
        )

        _upsert(c, "dataset_meta", _serialise(session_state.dataset_meta or {}))
        _upsert(c, "variables", _serialise(session_state.variables or []))
        _upsert(c, "var_name_map", _serialise(session_state.var_name_map or {}))
        _upsert(
            c, "reverse_var_name_map",
            _serialise(session_state.reverse_var_name_map or {}),
        )
        _upsert(c, "history", _serialise(session_state.history or []))
        _upsert(
            c, "last_analysis",
            _serialise(session_state.last_analysis or {}),
        )
        _upsert(
            c, "current_stage",
            _serialise(session_state.current_stage or "UPLOADING"),
        )

        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to save session to %s", db_path)


def load_session(session_state, db_path: str | Path = DB_PATH) -> bool:
    """Restore persisted state into *session_state*.  Returns ``True`` if
    data was loaded, ``False`` if the DB doesn't exist or is empty.
    """
    db_path = str(db_path)
    if not os.path.exists(db_path):
        return False

    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS session (key TEXT PRIMARY KEY, value TEXT)"
        )
        rows = {
            row[0]: row[1]
            for row in c.execute("SELECT key, value FROM session").fetchall()
        }
        conn.close()
    except Exception:
        logger.exception("Failed to read session from %s", db_path)
        return False

    if not rows:
        return False

    try:
        session_state.dataset_meta = _deserialise(
            rows.get("dataset_meta", "{}")
        )
        session_state.variables = _deserialise(
            rows.get("variables", "[]")
        )
        session_state.var_name_map = _deserialise(
            rows.get("var_name_map", "{}")
        )
        session_state.reverse_var_name_map = _deserialise(
            rows.get("reverse_var_name_map", "{}")
        )
        session_state.history = _deserialise(
            rows.get("history", "[]")
        )
        session_state.last_analysis = _deserialise(
            rows.get("last_analysis", "null")
        )
        session_state.current_stage = _deserialise(
            rows.get("current_stage", '"READY"')
        )
        logger.info(
            "Restored session: %d variables, %d history entries, stage=%s",
            len(session_state.variables),
            len(session_state.history),
            session_state.current_stage,
        )
        return True
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.exception("Failed to deserialise session data")
        return False


def clear_session(db_path: str | Path = DB_PATH):
    """Remove the persisted session database file."""
    db_path = str(db_path)
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            logger.info("Cleared persisted session at %s", db_path)
        except OSError:
            logger.exception("Failed to clear session DB")


# ── Internal helpers ────────────────────────────────────────────────────


def _upsert(cursor, key: str, value: str):
    """INSERT OR REPLACE a key-value row."""
    cursor.execute(
        "INSERT OR REPLACE INTO session (key, value) VALUES (?, ?)",
        (key, value),
    )
