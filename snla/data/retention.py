"""Ephemeral dataset workspaces and opt-in encrypted restore references."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from snla.secrets import (
    ProtectionProvider,
    SecretProtectionError,
    WindowsDPAPIProvider,
    application_data_directory,
)

RESTORE_FORMAT = "statstalk-dataset-restore"
RESTORE_VERSION = 1
SUPPORTED_DATASET_SUFFIXES = {".csv", ".sav", ".xlsx"}
RESTORE_DPAPI_ENTROPY = b"StatsTalk dataset restore reference v1"


class DatasetRetentionError(Exception):
    """A retention operation failed without exposing private metadata."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DatasetRetention:
    """Own the local lifecycle for dataset working files and restore metadata."""

    def __init__(
        self,
        *,
        reference_path: Path,
        workspace_root: Path,
        provider: ProtectionProvider,
        restore_enabled: Callable[[], bool],
        legacy_session_path: Path | None = None,
    ) -> None:
        self.reference_path = reference_path
        self.workspace_root = workspace_root
        self.provider = provider
        self.restore_enabled = restore_enabled
        self.legacy_session_path = legacy_session_path
        self._workspace: Path | None = None

    def remember(self, source_path: str | Path) -> dict[str, object]:
        """Remember an original dataset path only when restore is enabled."""

        if not self.restore_enabled():
            return {"state": "disabled", "available": False}
        source = self._validated_source(source_path)
        payload = json.dumps(
            {
                "format": RESTORE_FORMAT,
                "version": RESTORE_VERSION,
                "source_path": str(source),
                "filename": source.name,
                "data_format": source.suffix.lower().removeprefix("."),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        previous = self.reference_path.read_bytes() if self.reference_path.exists() else None
        try:
            ciphertext = self.provider.protect(payload)
            self._atomic_write(ciphertext)
        except Exception:
            raise DatasetRetentionError(
                "restore_reference_write_failed",
                "The encrypted dataset reference could not be saved.",
            ) from None
        try:
            persisted = self._read_reference()
            if persisted["source_path"] != str(source):
                raise ValueError("persisted source did not match")
        except Exception:
            if previous is None:
                self.reference_path.unlink(missing_ok=True)
            else:
                self._atomic_write(previous)
            raise DatasetRetentionError(
                "restore_reference_verification_failed",
                "The encrypted dataset reference could not be verified after saving.",
            ) from None
        return self.restore_status()

    def restore_status(self) -> dict[str, object]:
        """Return non-sensitive status for a remembered dataset reference."""

        if not self.restore_enabled():
            return {"state": "disabled", "available": False}
        if not self.reference_path.exists():
            return {"state": "empty", "available": False}
        try:
            reference = self._read_reference()
        except DatasetRetentionError as exc:
            return {
                "state": "unavailable",
                "available": False,
                "code": exc.code,
                "message": str(exc),
            }
        available = Path(reference["source_path"]).is_file()
        return {
            "state": "pending" if available else "missing",
            "available": available,
            "filename": reference["filename"],
            "format": reference["data_format"],
            **(
                {
                    "code": "restore_source_missing",
                    "message": "The original dataset is missing or inaccessible. Choose it again.",
                }
                if not available
                else {}
            ),
        }

    def restore(self, *, consent: bool) -> Path | None:
        """Resolve the remembered original only after explicit user consent."""

        if not consent:
            return None
        reference = self._read_reference()
        source = Path(reference["source_path"])
        try:
            validated = self._validated_source(source)
        except (FileNotFoundError, OSError, DatasetRetentionError):
            raise DatasetRetentionError(
                "restore_source_missing",
                "The original dataset is missing or inaccessible. Choose it again.",
            ) from None
        if validated.name != reference["filename"]:
            raise DatasetRetentionError(
                "restore_reference_corrupt",
                "The encrypted dataset reference is damaged or unsupported.",
            )
        return validated

    def forget(self) -> None:
        """Delete the encrypted restore reference without touching the source."""

        self.reference_path.unlink(missing_ok=True)

    def cleanup_startup(self) -> dict[str, int]:
        """Remove crash remnants before a new session workspace is created."""

        removed = 0
        if self.workspace_root.is_dir():
            for child in self.workspace_root.iterdir():
                self._remove_path(child)
                removed += 1
        if self.legacy_session_path is not None:
            with suppress(OSError):
                self.legacy_session_path.unlink(missing_ok=True)
        self._workspace = None
        return {"removed_workspaces": removed}

    def allocate_upload(self, filename: str) -> Path:
        """Allocate a private working-copy path for one browser upload."""

        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_DATASET_SUFFIXES:
            raise DatasetRetentionError(
                "working_file_invalid",
                "Choose a .sav, .csv, or .xlsx dataset.",
            )
        if self._workspace is None:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self._workspace = Path(
                tempfile.mkdtemp(prefix="session-", dir=self.workspace_root)
            ).resolve()
        return self._workspace / f"uploaded_{secrets.token_hex(4)}{suffix}"

    def cleanup_session(self) -> None:
        """Remove every working copy owned by the current process."""

        if self._workspace is not None:
            self._remove_path(self._workspace)
            self._workspace = None

    def inspect_local_data(self) -> dict[str, object]:
        """Summarize retained dataset artifacts without exposing their contents."""

        files = 0
        total_bytes = 0
        if self.workspace_root.is_dir():
            for path in self.workspace_root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    files += 1
                    total_bytes += path.stat().st_size
        return {
            "restore_reference": self._file_summary(self.reference_path),
            "working_copies": {"files": files, "bytes": total_bytes},
            "legacy_session": self._file_summary(self.legacy_session_path),
        }

    def clear_local_data(self) -> dict[str, bool]:
        """Clear restore metadata, working copies, and legacy session shadows."""

        self.cleanup_session()
        if self.workspace_root.is_dir():
            for child in self.workspace_root.iterdir():
                self._remove_path(child)
        self.reference_path.unlink(missing_ok=True)
        if self.legacy_session_path is not None:
            self.legacy_session_path.unlink(missing_ok=True)
        return {"ok": True}

    @staticmethod
    def _file_summary(path: Path | None) -> dict[str, object]:
        exists = path is not None and path.is_file()
        return {
            "exists": exists,
            "bytes": path.stat().st_size if exists and path is not None else 0,
        }

    @staticmethod
    def _remove_path(path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)

    @staticmethod
    def _validated_source(source_path: str | Path) -> Path:
        try:
            source = Path(source_path).expanduser().resolve(strict=True)
        except OSError:
            raise DatasetRetentionError(
                "restore_source_invalid",
                "Choose an existing .sav, .csv, or .xlsx dataset.",
            ) from None
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_DATASET_SUFFIXES:
            raise DatasetRetentionError(
                "restore_source_invalid",
                "Choose an existing .sav, .csv, or .xlsx dataset.",
            )
        return source

    def _read_reference(self) -> dict[str, str]:
        try:
            plaintext = self.provider.unprotect(self.reference_path.read_bytes())
        except Exception:
            raise DatasetRetentionError(
                "restore_reference_unavailable",
                "The encrypted dataset reference cannot be opened for this Windows account.",
            ) from None
        try:
            payload = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DatasetRetentionError(
                "restore_reference_corrupt",
                "The encrypted dataset reference is damaged or unsupported.",
            ) from None
        required = {"format", "version", "source_path", "filename", "data_format"}
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload.get("format") != RESTORE_FORMAT
            or payload.get("version") != RESTORE_VERSION
            or not all(isinstance(payload.get(key), str) for key in required - {"version"})
        ):
            raise DatasetRetentionError(
                "restore_reference_corrupt",
                "The encrypted dataset reference is damaged or unsupported.",
            )
        return payload

    def _atomic_write(self, contents: bytes) -> None:
        self.reference_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.reference_path.name}.",
            suffix=".tmp",
            dir=self.reference_path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(contents)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.reference_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)


class _UnavailableRetentionProvider:
    def protect(self, plaintext: bytes) -> bytes:
        raise SecretProtectionError(
            "dpapi_unavailable",
            "Windows DPAPI is unavailable on this operating system.",
        )

    def unprotect(self, ciphertext: bytes) -> bytes:
        raise SecretProtectionError(
            "dpapi_unavailable",
            "Windows DPAPI is unavailable on this operating system.",
        )


def create_dataset_retention(
    *,
    provider: ProtectionProvider | None = None,
    application_directory: Path | None = None,
    legacy_session_path: Path | None = None,
) -> DatasetRetention:
    """Build the process-wide dataset retention service."""

    from snla import config
    from snla.data.persistence import DB_PATH

    app_directory = application_directory or application_data_directory()
    if provider is None:
        provider = (
            WindowsDPAPIProvider(
                description="StatsTalk dataset restore reference",
                entropy=RESTORE_DPAPI_ENTROPY,
            )
            if os.name == "nt"
            else _UnavailableRetentionProvider()
        )
    return DatasetRetention(
        reference_path=app_directory / "dataset_restore" / "restore_reference.bin",
        workspace_root=app_directory / "session_workspaces",
        provider=provider,
        restore_enabled=lambda: config.SESSION_RESTORE_ENABLED,
        legacy_session_path=legacy_session_path or DB_PATH,
    )


__all__ = [
    "DatasetRetention",
    "DatasetRetentionError",
    "create_dataset_retention",
]
