"""Current-user secret protection and API-key persistence."""

from __future__ import annotations

import ctypes
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

CRYPTPROTECT_UI_FORBIDDEN = 0x1
API_KEY_MARKER = "dpapi"


class SecretProtectionError(Exception):
    """A secret operation failed without exposing credential material."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SecretResolution:
    """Runtime key plus the non-sensitive state exposed to callers."""

    state: str
    api_key: str = field(default="", repr=False)
    cloud_available: bool = False
    action: str | None = None
    message: str = ""

    def public_status(self) -> dict[str, object]:
        return {
            "state": self.state,
            "configured": self.state == "configured",
            "cloud_available": self.cloud_available,
            "action": self.action,
            "message": self.message,
        }


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    contents: bytes = field(default=b"", repr=False)


class WindowsDPAPIProvider:
    """Protect bytes with Windows DPAPI scoped to the current user."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretProtectionError(
                "dpapi_unavailable",
                "Windows DPAPI is unavailable on this operating system.",
            )
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()

    def _configure_functions(self) -> None:
        blob_pointer = ctypes.POINTER(_DataBlob)
        self._crypt32.CryptProtectData.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            blob_pointer,
            ctypes.c_void_p,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    @staticmethod
    def _input_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
        buffer = ctypes.create_string_buffer(data or b"\0", max(1, len(data)))
        blob = _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    def protect(self, plaintext: bytes) -> bytes:
        input_blob, _buffer = self._input_blob(plaintext)
        output_blob = _DataBlob()
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "StatsTalk API key",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        return self._finish(succeeded, output_blob, "dpapi_encrypt_failed", "protect")

    def unprotect(self, ciphertext: bytes) -> bytes:
        input_blob, _buffer = self._input_blob(ciphertext)
        output_blob = _DataBlob()
        succeeded = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        return self._finish(succeeded, output_blob, "dpapi_decrypt_failed", "unprotect")

    def _finish(
        self,
        succeeded: int,
        output_blob: _DataBlob,
        code: str,
        operation: str,
    ) -> bytes:
        if not succeeded:
            windows_error = ctypes.get_last_error()
            raise SecretProtectionError(
                code,
                f"Windows DPAPI could not {operation} the API key (error {windows_error}).",
            )
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


class ProtectionProvider(Protocol):
    """Encrypt and decrypt bytes for the current operating-system user."""

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _UnavailableProtectionProvider:
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


def _atomic_write_bytes(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "wb") as temporary_file:
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


class SecretStore:
    """Persist API-key ciphertext separately from ordinary configuration."""

    def __init__(self, path: Path, provider: ProtectionProvider) -> None:
        self.path = path
        self.provider = provider

    def replace(self, api_key: str) -> None:
        plaintext = api_key.encode("utf-8")
        try:
            ciphertext = self.provider.protect(plaintext)
        except SecretProtectionError as exc:
            raise SecretProtectionError(
                exc.code,
                "The API key could not be encrypted for the current Windows user.",
            ) from None
        except Exception:
            raise SecretProtectionError(
                "secret_encrypt_failed",
                "The API key could not be encrypted for the current Windows user.",
            ) from None
        previous = self.path.read_bytes() if self.path.exists() else None
        self._write_ciphertext(ciphertext)
        try:
            persisted = self.path.read_bytes()
            if self.provider.unprotect(persisted) != plaintext:
                raise ValueError("decrypted value did not match")
        except Exception:
            if previous is None:
                self.path.unlink(missing_ok=True)
            else:
                self._write_ciphertext(previous)
            raise SecretProtectionError(
                "secret_verification_failed",
                "The encrypted API key could not be verified after it was saved.",
            ) from None

    def _write_ciphertext(self, ciphertext: bytes) -> None:
        _atomic_write_bytes(self.path, ciphertext)

    def read(self) -> str:
        plaintext = self.provider.unprotect(self.path.read_bytes())
        return plaintext.decode("utf-8")

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)


class ApiKeyService:
    """Coordinate secure storage with the API-key marker in ``.env``."""

    def __init__(self, store: SecretStore, env_path: Path) -> None:
        self.store = store
        self.env_path = env_path

    def resolve(self, configured_value: str | None) -> SecretResolution:
        raw_value = (configured_value or "").strip()
        if not raw_value:
            return SecretResolution(
                state="missing",
                action="enter_api_key",
                message="Enter an API key to enable cloud features.",
            )
        if raw_value != API_KEY_MARKER:
            return SecretResolution(
                state="migration_required",
                action="confirm_migration",
                message="Confirm secure migration of the existing API key.",
            )
        try:
            api_key = self.store.read()
        except Exception:
            return SecretResolution(
                state="reenter_required",
                action="enter_api_key",
                message=(
                    "The encrypted API key cannot be opened for this Windows account. "
                    "Enter the API key again to restore cloud features."
                ),
            )
        if not api_key:
            return SecretResolution(
                state="reenter_required",
                action="enter_api_key",
                message=(
                    "The encrypted API key is empty or invalid. "
                    "Enter the API key again to restore cloud features."
                ),
            )
        return SecretResolution(
            state="configured",
            api_key=api_key,
            cloud_available=True,
            message="API key is protected for the current Windows user.",
        )

    def migrate_legacy(self, legacy_key: str, *, consent: bool) -> SecretResolution:
        if not consent:
            raise SecretProtectionError(
                "migration_consent_required",
                "Explicit consent is required before migrating the existing API key.",
            )
        return self._transaction(
            lambda: self._replace_and_mark(legacy_key),
        )

    def replace(self, api_key: str) -> SecretResolution:
        if not api_key:
            raise SecretProtectionError(
                "empty_api_key",
                "Enter a non-empty API key.",
            )
        return self._transaction(
            lambda: self._replace_and_mark(api_key),
        )

    def delete(self) -> SecretResolution:
        return self._transaction(self._delete)

    def _replace_and_mark(self, api_key: str) -> SecretResolution:
        self._persist_verified(api_key)
        self._persist_config_value(API_KEY_MARKER)
        return self._configured(api_key)

    def _delete(self) -> SecretResolution:
        self._persist_config_value("")
        try:
            self.store.delete()
        except OSError:
            raise SecretProtectionError(
                "secret_delete_failed",
                "The encrypted API key could not be deleted.",
            ) from None
        return SecretResolution(
            state="missing",
            action="enter_api_key",
            message="Enter an API key to enable cloud features.",
        )

    def _transaction(
        self,
        operation: Callable[[], SecretResolution],
    ) -> SecretResolution:
        secret_snapshot = self._snapshot_file(self.store.path)
        config_snapshot = self._snapshot_file(self.env_path)
        try:
            return operation()
        except SecretProtectionError:
            try:
                self._restore_file(self.store.path, secret_snapshot)
                self._restore_file(self.env_path, config_snapshot)
            except OSError:
                raise SecretProtectionError(
                    "secret_rollback_failed",
                    "API-key storage failed and the previous state could not be restored.",
                ) from None
            raise

    @staticmethod
    def _snapshot_file(path: Path) -> _FileSnapshot:
        try:
            return _FileSnapshot(existed=True, contents=path.read_bytes())
        except (FileNotFoundError, NotADirectoryError):
            return _FileSnapshot(existed=False)

    @staticmethod
    def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
        if snapshot.existed:
            _atomic_write_bytes(path, snapshot.contents)
        else:
            with suppress(FileNotFoundError, NotADirectoryError):
                path.unlink(missing_ok=True)

    def _persist_verified(self, api_key: str) -> None:
        try:
            self.store.replace(api_key)
        except SecretProtectionError:
            raise
        except OSError:
            raise SecretProtectionError(
                "secret_write_failed",
                "The encrypted API key could not be saved.",
            ) from None

    def _persist_config_value(self, value: str) -> None:
        try:
            self._write_config_value(value)
        except OSError:
            raise SecretProtectionError(
                "config_write_failed",
                "The API-key storage marker could not be saved to configuration.",
            ) from None

    @staticmethod
    def _configured(api_key: str) -> SecretResolution:
        return SecretResolution(
            state="configured",
            api_key=api_key,
            cloud_available=True,
            message="API key is protected for the current Windows user.",
        )

    def _write_config_value(self, value: str) -> None:
        self.env_path.parent.mkdir(parents=True, exist_ok=True)
        existing = (
            self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
        )
        output: list[str] = []
        marker_found = False
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key == "LLM_API_KEY":
                    continue
                if key == "LLM_API_KEY_STORAGE":
                    if not marker_found and value:
                        output.append(f"LLM_API_KEY_STORAGE={value}")
                    marker_found = True
                    continue
            output.append(line)
        if not marker_found and value:
            output.append(f"LLM_API_KEY_STORAGE={value}")

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.env_path.name}.",
            suffix=".tmp",
            dir=self.env_path.parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temporary_file:
                temporary_file.write("\n".join(output) + "\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self.env_path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)


def application_data_directory() -> Path:
    """Return the per-user StatsTalk application-data directory."""

    configured_root = os.getenv("APPDATA") or os.getenv("LOCALAPPDATA")
    if configured_root:
        return Path(configured_root) / "StatsTalk"
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "StatsTalk"
    return Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "StatsTalk"


def create_api_key_service(
    env_path: Path,
    *,
    provider: ProtectionProvider | None = None,
) -> ApiKeyService:
    """Build the default API-key service while allowing CI provider injection."""

    if provider is None:
        provider = WindowsDPAPIProvider() if os.name == "nt" else _UnavailableProtectionProvider()
    store = SecretStore(application_data_directory() / "secure_key.bin", provider)
    return ApiKeyService(store, env_path)


__all__ = [
    "API_KEY_MARKER",
    "ApiKeyService",
    "ProtectionProvider",
    "SecretResolution",
    "SecretProtectionError",
    "SecretStore",
    "WindowsDPAPIProvider",
    "application_data_directory",
    "create_api_key_service",
]
