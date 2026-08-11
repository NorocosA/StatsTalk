"""Current-user secret protection and API-key persistence."""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

CRYPTPROTECT_UI_FORBIDDEN = 0x1
API_KEY_MARKER = "dpapi"
BACKUP_FORMAT = "statstalk-api-key-backup"
BACKUP_VERSION = 1
BACKUP_AAD = b"StatsTalk API key backup v1"
BACKUP_MAX_BYTES = 64 * 1024
BACKUP_MIN_PASSWORD_LENGTH = 12
BACKUP_MAX_PASSWORD_BYTES = 1024


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


class SecretBackupCodec:
    """Encode portable API-key backups with a password-derived AEAD key."""

    def __init__(self, *, n: int = 2**15, r: int = 8, p: int = 1) -> None:
        self.n = n
        self.r = r
        self.p = p

    def encrypt(self, plaintext: bytes, password: str) -> bytes:
        password_bytes = self._password_bytes(password, require_strength=True)
        salt = os.urandom(16)
        nonce = os.urandom(12)
        key = self._derive(password_bytes, salt)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, BACKUP_AAD)
        container = {
            "cipher": {
                "ciphertext": self._encode(ciphertext),
                "name": "aes-256-gcm",
                "nonce": self._encode(nonce),
            },
            "format": BACKUP_FORMAT,
            "kdf": {
                "n": self.n,
                "name": "scrypt",
                "p": self.p,
                "r": self.r,
                "salt": self._encode(salt),
            },
            "version": BACKUP_VERSION,
        }
        return json.dumps(container, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def decrypt(self, payload: bytes, password: str) -> bytes:
        if len(payload) > BACKUP_MAX_BYTES:
            raise SecretProtectionError(
                "backup_too_large",
                "The API-key backup file is larger than StatsTalk supports.",
            )
        password_bytes = self._password_bytes(password, require_strength=False)
        container = self._parse_container(payload)
        kdf = container["kdf"]
        cipher = container["cipher"]
        salt = self._decode(kdf["salt"], expected_length=16)
        nonce = self._decode(cipher["nonce"], expected_length=12)
        ciphertext = self._decode(cipher["ciphertext"], maximum_length=16 * 1024)
        try:
            key = self._derive(password_bytes, salt)
            return AESGCM(key).decrypt(nonce, ciphertext, BACKUP_AAD)
        except InvalidTag:
            raise SecretProtectionError(
                "backup_authentication_failed",
                "The backup password is incorrect or the backup file is damaged.",
            ) from None
        except SecretProtectionError:
            raise
        except Exception:
            raise SecretProtectionError(
                "backup_decryption_failed",
                "The API-key backup could not be decrypted.",
            ) from None

    def _parse_container(self, payload: bytes) -> dict[str, object]:
        try:
            container = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SecretProtectionError(
                "backup_invalid",
                "The selected file is not a valid StatsTalk API-key backup.",
            ) from None
        if not isinstance(container, dict):
            self._raise_invalid()
        if container.get("format") != BACKUP_FORMAT:
            self._raise_invalid()
        if container.get("version") != BACKUP_VERSION:
            raise SecretProtectionError(
                "backup_unsupported_version",
                "This API-key backup version is not supported by StatsTalk.",
            )
        kdf = container.get("kdf")
        cipher = container.get("cipher")
        if not isinstance(kdf, dict) or not isinstance(cipher, dict):
            self._raise_invalid()
        expected_kdf = {"name": "scrypt", "n": self.n, "r": self.r, "p": self.p}
        if any(kdf.get(key) != value for key, value in expected_kdf.items()):
            raise SecretProtectionError(
                "backup_unsupported_parameters",
                "This API-key backup uses unsupported security parameters.",
            )
        if cipher.get("name") != "aes-256-gcm":
            raise SecretProtectionError(
                "backup_unsupported_parameters",
                "This API-key backup uses unsupported security parameters.",
            )
        for mapping, fields in ((kdf, ("salt",)), (cipher, ("nonce", "ciphertext"))):
            if any(not isinstance(mapping.get(field), str) for field in fields):
                self._raise_invalid()
        return container

    @staticmethod
    def _password_bytes(password: str, *, require_strength: bool) -> bytes:
        if not isinstance(password, str) or not password:
            raise SecretProtectionError(
                "backup_password_required",
                "Enter the backup password.",
            )
        if require_strength and len(password) < BACKUP_MIN_PASSWORD_LENGTH:
            raise SecretProtectionError(
                "backup_password_weak",
                f"Use at least {BACKUP_MIN_PASSWORD_LENGTH} characters for the backup password.",
            )
        encoded = password.encode("utf-8")
        if len(encoded) > BACKUP_MAX_PASSWORD_BYTES:
            raise SecretProtectionError(
                "backup_password_too_long",
                "The backup password is longer than StatsTalk supports.",
            )
        return encoded

    def _derive(self, password: bytes, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=self.n, r=self.r, p=self.p).derive(password)

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.b64encode(value).decode("ascii")

    @classmethod
    def _decode(
        cls,
        value: str,
        *,
        expected_length: int | None = None,
        maximum_length: int | None = None,
    ) -> bytes:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            cls._raise_invalid()
        if expected_length is not None and len(decoded) != expected_length:
            cls._raise_invalid()
        if maximum_length is not None and len(decoded) > maximum_length:
            cls._raise_invalid()
        return decoded

    @staticmethod
    def _raise_invalid() -> None:
        raise SecretProtectionError(
            "backup_invalid",
            "The selected file is not a valid StatsTalk API-key backup.",
        )


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

    def __init__(
        self,
        store: SecretStore,
        env_path: Path,
        *,
        backup_codec: SecretBackupCodec | None = None,
    ) -> None:
        self.store = store
        self.env_path = env_path
        self.backup_codec = backup_codec or SecretBackupCodec()

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

    def export_backup(self, password: str, password_confirmation: str) -> bytes:
        if password != password_confirmation:
            raise SecretProtectionError(
                "backup_password_mismatch",
                "The backup password confirmation does not match.",
            )
        try:
            api_key = self.store.read()
        except Exception:
            raise SecretProtectionError(
                "backup_key_unavailable",
                "The current API key cannot be opened, so it cannot be backed up.",
            ) from None
        if not api_key:
            raise SecretProtectionError(
                "backup_key_unavailable",
                "There is no configured API key to back up.",
            )
        return self.backup_codec.encrypt(api_key.encode("utf-8"), password)

    def import_backup(self, payload: bytes, password: str) -> SecretResolution:
        plaintext = self.backup_codec.decrypt(payload, password)
        try:
            api_key = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretProtectionError(
                "backup_invalid_secret",
                "The API-key backup does not contain a valid key.",
            ) from None
        if not api_key:
            raise SecretProtectionError(
                "backup_invalid_secret",
                "The API-key backup does not contain a valid key.",
            )
        return self.replace(api_key)

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
    "BACKUP_MAX_BYTES",
    "BACKUP_MIN_PASSWORD_LENGTH",
    "ApiKeyService",
    "ProtectionProvider",
    "SecretResolution",
    "SecretBackupCodec",
    "SecretProtectionError",
    "SecretStore",
    "WindowsDPAPIProvider",
    "application_data_directory",
    "create_api_key_service",
]
