"""Focused persistence lifecycle tests."""

import sqlite3
from types import SimpleNamespace

from snla.data.persistence import clear_session, load_session


def test_clear_session_removes_existing_database(tmp_path):
    database = tmp_path / "session.db"
    database.write_bytes(b"temporary session")

    clear_session(database)

    assert not database.exists()


def test_load_session_returns_false_for_missing_database(tmp_path):
    assert load_session(SimpleNamespace(), tmp_path / "missing.db") is False


def test_load_session_returns_false_for_empty_database(tmp_path):
    database = tmp_path / "empty.db"
    sqlite3.connect(database).close()

    assert load_session(SimpleNamespace(), database) is False


def test_load_session_returns_false_for_corrupt_json(tmp_path):
    database = tmp_path / "corrupt.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE session (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO session VALUES (?, ?)", ("dataset_meta", "{"))

    assert load_session(SimpleNamespace(), database) is False
