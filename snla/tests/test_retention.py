"""Dataset retention and workspace lifecycle contracts."""

from __future__ import annotations

import asyncio
import sys

import pytest


class FakeProtectionProvider:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("invalid ciphertext")
        return ciphertext.removeprefix(b"protected:")[::-1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is Windows-only")
def test_real_dpapi_restore_reference_round_trips_without_plaintext(tmp_path):
    from snla.data.retention import RESTORE_DPAPI_ENTROPY, DatasetRetention
    from snla.secrets import WindowsDPAPIProvider

    source = tmp_path / "private_scores.csv"
    source.write_text("score\n80\n", encoding="utf-8")
    reference_path = tmp_path / "restore_reference.bin"
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=WindowsDPAPIProvider(
            description="StatsTalk dataset restore reference",
            entropy=RESTORE_DPAPI_ENTROPY,
        ),
        restore_enabled=lambda: True,
    )

    retention.remember(source)

    assert str(source).encode() not in reference_path.read_bytes()
    assert retention.restore(consent=True) == source.resolve()


def test_restore_disabled_writes_nothing(tmp_path):
    from snla.data.retention import DatasetRetention

    source = tmp_path / "scores.csv"
    source.write_text("score\n80\n", encoding="utf-8")
    reference_path = tmp_path / "app-data" / "restore_reference.bin"
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=FakeProtectionProvider(),
        restore_enabled=lambda: False,
    )

    status = retention.remember(source)

    assert status["state"] == "disabled"
    assert not reference_path.exists()


def test_enabled_restore_writes_only_encrypted_minimal_reference(tmp_path):
    from snla.data.retention import DatasetRetention

    source = tmp_path / "private_scores.csv"
    source.write_text("patient_id,score\nP001,80\n", encoding="utf-8")
    reference_path = tmp_path / "app-data" / "restore_reference.bin"
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=FakeProtectionProvider(),
        restore_enabled=lambda: True,
    )

    remembered = retention.remember(source)
    pending = retention.restore_status()

    ciphertext = reference_path.read_bytes()
    assert remembered == pending
    assert pending == {
        "state": "pending",
        "available": True,
        "filename": "private_scores.csv",
        "format": "csv",
    }
    assert str(source).encode() not in ciphertext
    assert b"patient_id" not in ciphertext
    assert b"P001" not in ciphertext


def test_failed_restore_reference_verification_rolls_back_previous_ciphertext(tmp_path):
    from snla.data.retention import DatasetRetention, DatasetRetentionError

    class ToggleProvider(FakeProtectionProvider):
        fail_unprotect = False

        def unprotect(self, ciphertext):
            if self.fail_unprotect:
                raise ValueError("verification failed")
            return super().unprotect(ciphertext)

    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    first.write_text("score\n1\n", encoding="utf-8")
    second.write_text("score\n2\n", encoding="utf-8")
    provider = ToggleProvider()
    reference_path = tmp_path / "restore_reference.bin"
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=provider,
        restore_enabled=lambda: True,
    )
    retention.remember(first)
    previous = reference_path.read_bytes()
    provider.fail_unprotect = True

    try:
        retention.remember(second)
    except DatasetRetentionError as exc:
        assert exc.code == "restore_reference_verification_failed"
    else:
        raise AssertionError("unverified restore reference must fail")

    assert reference_path.read_bytes() == previous


def test_restore_requires_consent_and_reports_missing_or_corrupt_sources(tmp_path):
    from snla.data.retention import DatasetRetention, DatasetRetentionError

    source = tmp_path / "scores.sav"
    source.write_bytes(b"dataset")
    reference_path = tmp_path / "app-data" / "restore_reference.bin"
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=FakeProtectionProvider(),
        restore_enabled=lambda: True,
    )
    retention.remember(source)

    assert retention.restore(consent=False) is None
    assert retention.restore(consent=True) == source.resolve()

    source.unlink()
    status = retention.restore_status()
    assert status["state"] == "missing"
    assert status["available"] is False
    try:
        retention.restore(consent=True)
    except DatasetRetentionError as exc:
        assert exc.code == "restore_source_missing"
    else:
        raise AssertionError("missing source must not be restored")

    reference_path.write_bytes(b"corrupt")
    status = retention.restore_status()
    assert status["state"] == "unavailable"
    assert status["code"] == "restore_reference_unavailable"

    reference_path.write_bytes(FakeProtectionProvider().protect(b"{"))
    status = retention.restore_status()
    assert status["state"] == "unavailable"
    assert status["code"] == "restore_reference_corrupt"


def test_workspace_cleanup_handles_normal_exit_and_stale_crash_remnants(tmp_path):
    from snla.data.retention import DatasetRetention

    workspace_root = tmp_path / "workspaces"
    stale = workspace_root / "session-stale"
    stale.mkdir(parents=True)
    (stale / "uploaded_private.csv").write_text("secret", encoding="utf-8")
    legacy_database = tmp_path / "snla_session.db"
    legacy_database.write_bytes(b"legacy history and metadata")
    retention = DatasetRetention(
        reference_path=tmp_path / "app-data" / "restore_reference.bin",
        workspace_root=workspace_root,
        provider=FakeProtectionProvider(),
        restore_enabled=lambda: False,
        legacy_session_path=legacy_database,
    )

    cleanup = retention.cleanup_startup()

    assert cleanup["removed_workspaces"] == 1
    assert not stale.exists()
    assert not legacy_database.exists()

    working_file = retention.allocate_upload("student_data.csv")
    working_file.write_text("private working data", encoding="utf-8")
    assert working_file.is_file()

    retention.cleanup_session()

    assert not working_file.exists()
    assert list(workspace_root.iterdir()) == []


def test_users_can_inspect_and_clear_all_retained_dataset_artifacts(tmp_path):
    from snla.data.retention import DatasetRetention

    source = tmp_path / "scores.csv"
    source.write_text("score\n80\n", encoding="utf-8")
    reference_path = tmp_path / "app-data" / "restore_reference.bin"
    legacy_database = tmp_path / "snla_session.db"
    legacy_database.write_bytes(b"legacy")
    retention = DatasetRetention(
        reference_path=reference_path,
        workspace_root=tmp_path / "workspaces",
        provider=FakeProtectionProvider(),
        restore_enabled=lambda: True,
        legacy_session_path=legacy_database,
    )
    retention.remember(source)
    working_file = retention.allocate_upload("scores.csv")
    working_file.write_bytes(b"working-copy")

    report = retention.inspect_local_data()

    assert report["restore_reference"]["exists"] is True
    assert report["restore_reference"]["bytes"] == reference_path.stat().st_size
    assert report["working_copies"] == {"files": 1, "bytes": len(b"working-copy")}
    assert report["legacy_session"]["exists"] is True
    assert "contents" not in str(report)
    assert "source_path" not in str(report)

    cleared = retention.clear_local_data()

    assert cleared["ok"] is True
    assert not reference_path.exists()
    assert not working_file.exists()
    assert not legacy_database.exists()


def test_mcp_lifecycle_removes_fresh_crash_remnants_and_in_memory_history(tmp_path, monkeypatch):
    from snla import mcp_server

    upload_root = tmp_path / "mcp-workspaces"
    stale = upload_root / "recent-crash"
    stale.mkdir(parents=True)
    (stale / "private.csv").write_bytes(b"private")
    monkeypatch.setattr(mcp_server, "_upload_dir", upload_root)
    mcp_server._session_states["private-session"] = mcp_server.MCPState(
        last_query="private question"
    )

    async def exercise_lifespan():
        async with mcp_server.server_lifespan(None):
            assert not stale.exists()
            (upload_root / "active").mkdir()

    asyncio.run(exercise_lifespan())

    assert not upload_root.exists()
    assert mcp_server._session_states == {}
