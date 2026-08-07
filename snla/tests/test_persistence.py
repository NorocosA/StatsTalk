"""Focused persistence lifecycle tests."""

from snla.data.persistence import clear_session


def test_clear_session_removes_existing_database(tmp_path):
    database = tmp_path / "session.db"
    database.write_bytes(b"temporary session")

    clear_session(database)

    assert not database.exists()
