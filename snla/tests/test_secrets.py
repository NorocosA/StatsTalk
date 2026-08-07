from __future__ import annotations

import sys

import pytest


def _raise_os_error():
    raise OSError("simulated write failure")


class FakeDPAPIProvider:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix = b"protected:"
        if not ciphertext.startswith(prefix):
            raise ValueError("ciphertext cannot be decrypted")
        return ciphertext[len(prefix) :][::-1]


class WrongAccountDPAPIProvider(FakeDPAPIProvider):
    def unprotect(self, ciphertext: bytes) -> bytes:
        raise ValueError("wrong account saw sk-sensitive-material")


class FailingVerificationProvider(FakeDPAPIProvider):
    fail_verification = False

    def unprotect(self, ciphertext: bytes) -> bytes:
        if self.fail_verification:
            raise ValueError("verification failed")
        return super().unprotect(ciphertext)


def test_secret_store_persists_only_ciphertext_and_round_trips(tmp_path):
    from snla.secrets import SecretStore

    secret_path = tmp_path / "StatsTalk" / "secure_key.bin"
    store = SecretStore(secret_path, FakeDPAPIProvider())

    store.replace("sk-private-value")

    assert secret_path.read_bytes() == b"protected:eulav-etavirp-ks"
    assert b"sk-private-value" not in secret_path.read_bytes()
    assert store.read() == "sk-private-value"


def test_secret_resolution_repr_hides_api_key():
    from snla.secrets import SecretResolution

    secret = "sk-never-in-repr"
    resolution = SecretResolution(state="configured", api_key=secret, cloud_available=True)

    assert secret not in repr(resolution)
    assert "api_key=" not in repr(resolution)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is Windows-only")
def test_windows_dpapi_round_trips_for_the_current_user():
    from snla.secrets import WindowsDPAPIProvider

    provider = WindowsDPAPIProvider()
    plaintext = b"StatsTalk DPAPI test value"

    ciphertext = provider.protect(plaintext)

    assert ciphertext != plaintext
    assert provider.unprotect(ciphertext) == plaintext


def test_explicit_legacy_migration_replaces_plaintext_with_marker(tmp_path):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    legacy_key = "sk-legacy-private"
    env_path = tmp_path / ".env"
    env_path.write_text(
        f"LLM_ENDPOINT=https://example.test/v1\nLLM_API_KEY={legacy_key}\n",
        encoding="utf-8",
    )
    store = SecretStore(tmp_path / "app-data" / "secure_key.bin", FakeDPAPIProvider())
    service = ApiKeyService(store, env_path)

    pending = service.resolve(legacy_key)
    migrated = service.migrate_legacy(legacy_key, consent=True)

    assert pending.state == "migration_required"
    assert pending.api_key == ""
    assert pending.cloud_available is False
    assert migrated.state == "configured"
    assert migrated.api_key == legacy_key
    assert env_path.read_text(encoding="utf-8") == (
        f"LLM_ENDPOINT=https://example.test/v1\nLLM_API_KEY_STORAGE={API_KEY_MARKER}\n"
    )
    assert legacy_key not in env_path.read_text(encoding="utf-8")
    assert store.read() == legacy_key


def test_legacy_migration_without_consent_changes_nothing(tmp_path):
    from snla.secrets import ApiKeyService, SecretProtectionError, SecretStore

    legacy_key = "sk-awaiting-consent"
    env_path = tmp_path / ".env"
    original_config = f"LLM_API_KEY={legacy_key}\n"
    env_path.write_text(original_config, encoding="utf-8")
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    service = ApiKeyService(SecretStore(secret_path, FakeDPAPIProvider()), env_path)

    with pytest.raises(SecretProtectionError) as error:
        service.migrate_legacy(legacy_key, consent=False)

    assert error.value.code == "migration_consent_required"
    assert env_path.read_text(encoding="utf-8") == original_config
    assert not secret_path.exists()


def test_migration_write_failure_preserves_legacy_plaintext(tmp_path):
    from snla.secrets import ApiKeyService, SecretProtectionError, SecretStore

    legacy_key = "sk-must-not-be-lost"
    env_path = tmp_path / ".env"
    original_config = f"LLM_API_KEY={legacy_key}\nLLM_MODEL=test-model\n"
    env_path.write_text(original_config, encoding="utf-8")
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocks mkdir", encoding="utf-8")
    store = SecretStore(blocked_parent / "secure_key.bin", FakeDPAPIProvider())
    service = ApiKeyService(store, env_path)

    with pytest.raises(SecretProtectionError) as error:
        service.migrate_legacy(legacy_key, consent=True)

    assert error.value.code == "secret_write_failed"
    assert env_path.read_text(encoding="utf-8") == original_config


def test_transaction_helpers_treat_invalid_parent_as_missing():
    from unittest.mock import Mock

    from snla.secrets import ApiKeyService

    invalid_path = Mock()
    invalid_path.read_bytes.side_effect = NotADirectoryError
    snapshot = ApiKeyService._snapshot_file(invalid_path)

    assert snapshot.existed is False

    invalid_path.unlink.side_effect = NotADirectoryError
    ApiKeyService._restore_file(invalid_path, snapshot)


def test_migration_config_write_failure_restores_previous_ciphertext(
    tmp_path,
    monkeypatch,
):
    from snla.secrets import ApiKeyService, SecretProtectionError, SecretStore

    legacy_key = "sk-legacy-transaction"
    env_path = tmp_path / ".env"
    original_config = f"LLM_API_KEY={legacy_key}\n"
    env_path.write_text(original_config, encoding="utf-8")
    store = SecretStore(tmp_path / "app-data" / "secure_key.bin", FakeDPAPIProvider())
    store.replace("sk-previous-orphan")
    previous_ciphertext = store.path.read_bytes()
    service = ApiKeyService(store, env_path)
    monkeypatch.setattr(service, "_write_config_value", lambda _value: _raise_os_error())

    with pytest.raises(SecretProtectionError) as error:
        service.migrate_legacy(legacy_key, consent=True)

    assert error.value.code == "config_write_failed"
    assert env_path.read_text(encoding="utf-8") == original_config
    assert store.path.read_bytes() == previous_ciphertext
    assert store.read() == "sk-previous-orphan"


def test_corrupt_ciphertext_requires_reentry_without_plaintext_fallback(tmp_path, caplog):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    secret_path = tmp_path / "app-data" / "secure_key.bin"
    secret_path.parent.mkdir()
    secret_path.write_bytes(b"corrupt-ciphertext")
    env_path = tmp_path / ".env"
    env_path.write_text(f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n", encoding="utf-8")
    service = ApiKeyService(SecretStore(secret_path, FakeDPAPIProvider()), env_path)

    resolution = service.resolve(API_KEY_MARKER)

    assert resolution.state == "reenter_required"
    assert resolution.api_key == ""
    assert resolution.cloud_available is False
    assert resolution.action == "enter_api_key"
    assert "Enter the API key again" in resolution.message
    assert "corrupt-ciphertext" not in resolution.message
    assert "corrupt-ciphertext" not in caplog.text


def test_account_mismatch_disables_cloud_without_exposing_provider_error(tmp_path, caplog):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    secret_path = tmp_path / "app-data" / "secure_key.bin"
    secret_path.parent.mkdir()
    secret_path.write_bytes(b"ciphertext-from-another-account")
    service = ApiKeyService(
        SecretStore(secret_path, WrongAccountDPAPIProvider()),
        tmp_path / ".env",
    )

    resolution = service.resolve(API_KEY_MARKER)

    assert resolution.state == "reenter_required"
    assert resolution.api_key == ""
    assert resolution.cloud_available is False
    assert "sk-sensitive-material" not in resolution.message
    assert "sk-sensitive-material" not in caplog.text


def test_replacing_api_key_updates_ciphertext_and_keeps_only_marker(tmp_path):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    env_path = tmp_path / ".env"
    env_path.write_text(f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n", encoding="utf-8")
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    store = SecretStore(secret_path, FakeDPAPIProvider())
    store.replace("sk-old-value")
    service = ApiKeyService(store, env_path)

    resolution = service.replace("sk-new-value")

    assert resolution.state == "configured"
    assert resolution.api_key == "sk-new-value"
    assert store.read() == "sk-new-value"
    assert env_path.read_text(encoding="utf-8") == (f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n")
    assert b"sk-old-value" not in secret_path.read_bytes()
    assert b"sk-new-value" not in secret_path.read_bytes()


def test_replacement_config_write_failure_restores_old_key(tmp_path, monkeypatch):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretProtectionError, SecretStore

    env_path = tmp_path / ".env"
    original_config = f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n"
    env_path.write_text(original_config, encoding="utf-8")
    store = SecretStore(tmp_path / "app-data" / "secure_key.bin", FakeDPAPIProvider())
    store.replace("sk-original-key")
    previous_ciphertext = store.path.read_bytes()
    service = ApiKeyService(store, env_path)
    monkeypatch.setattr(service, "_write_config_value", lambda _value: _raise_os_error())

    with pytest.raises(SecretProtectionError) as error:
        service.replace("sk-new-key")

    assert error.value.code == "config_write_failed"
    assert env_path.read_text(encoding="utf-8") == original_config
    assert store.path.read_bytes() == previous_ciphertext
    assert store.read() == "sk-original-key"


def test_deleting_api_key_removes_marker_and_ciphertext(tmp_path):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    env_path = tmp_path / ".env"
    env_path.write_text(
        f"LLM_ENDPOINT=https://example.test/v1\nLLM_API_KEY_STORAGE={API_KEY_MARKER}\n",
        encoding="utf-8",
    )
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    store = SecretStore(secret_path, FakeDPAPIProvider())
    store.replace("sk-delete-me")
    service = ApiKeyService(store, env_path)

    resolution = service.delete()

    assert resolution.state == "missing"
    assert resolution.api_key == ""
    assert resolution.cloud_available is False
    assert not secret_path.exists()
    assert env_path.read_text(encoding="utf-8") == ("LLM_ENDPOINT=https://example.test/v1\n")


def test_delete_failure_restores_marker_and_ciphertext(tmp_path):
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretProtectionError, SecretStore

    class DeleteThenFailStore(SecretStore):
        def delete(self) -> None:
            super().delete()
            raise OSError("simulated delete failure")

    env_path = tmp_path / ".env"
    original_config = f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n"
    env_path.write_text(original_config, encoding="utf-8")
    store = DeleteThenFailStore(
        tmp_path / "app-data" / "secure_key.bin",
        FakeDPAPIProvider(),
    )
    store.replace("sk-restore-after-delete")
    previous_ciphertext = store.path.read_bytes()
    service = ApiKeyService(store, env_path)

    with pytest.raises(SecretProtectionError) as error:
        service.delete()

    assert error.value.code == "secret_delete_failed"
    assert env_path.read_text(encoding="utf-8") == original_config
    assert store.path.read_bytes() == previous_ciphertext
    assert store.read() == "sk-restore-after-delete"


def test_default_store_uses_statstalk_application_data_directory(tmp_path, monkeypatch):
    from snla.secrets import create_api_key_service

    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))

    service = create_api_key_service(
        tmp_path / ".env",
        provider=FakeDPAPIProvider(),
    )

    assert service.store.path == tmp_path / "roaming" / "StatsTalk" / "secure_key.bin"
    assert service.store.path.parent != service.env_path.parent


def test_failed_replacement_verification_restores_previous_ciphertext(tmp_path):
    from snla.secrets import ApiKeyService, SecretProtectionError, SecretStore

    provider = FailingVerificationProvider()
    store = SecretStore(tmp_path / "app-data" / "secure_key.bin", provider)
    service = ApiKeyService(store, tmp_path / ".env")
    service.replace("sk-original")
    provider.fail_verification = True

    with pytest.raises(SecretProtectionError) as error:
        service.replace("sk-invalid-replacement")

    provider.fail_verification = False
    assert error.value.code == "secret_verification_failed"
    assert store.read() == "sk-original"
