from __future__ import annotations

import base64
import json

import pytest

from snla.secrets import (
    API_KEY_MARKER,
    ApiKeyService,
    SecretBackupCodec,
    SecretProtectionError,
    SecretStore,
)


class AccountProvider:
    def __init__(self, account: bytes) -> None:
        self.account = account
        self.fail_protection = False

    def protect(self, plaintext: bytes) -> bytes:
        if self.fail_protection:
            raise SecretProtectionError(
                "secret_encrypt_failed",
                f"provider rejected {plaintext.decode()}",
            )
        return b"dpapi:" + self.account + b":" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        prefix = b"dpapi:" + self.account + b":"
        if not ciphertext.startswith(prefix):
            raise ValueError("wrong account")
        return ciphertext.removeprefix(prefix)[::-1]


@pytest.fixture
def codec():
    return SecretBackupCodec(n=2**10)


def _service(tmp_path, name: str, provider: AccountProvider, codec: SecretBackupCodec):
    env_path = tmp_path / name / ".env"
    store = SecretStore(tmp_path / name / "secure_key.bin", provider)
    return ApiKeyService(store, env_path, backup_codec=codec)


def test_production_backup_codec_round_trips_with_default_security_parameters():
    codec = SecretBackupCodec()

    payload = codec.encrypt(b"sk-production-codec", "production-password")

    assert codec.decrypt(payload, "production-password") == b"sk-production-codec"


def test_backup_round_trips_across_dpapi_accounts_without_exporting_secret_material(
    tmp_path,
    codec,
):
    api_key = "sk-portable-private-value"
    password = "correct horse battery staple"
    source = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    source.replace(api_key)
    source_ciphertext = source.store.path.read_bytes()

    payload = source.export_backup(password, password)

    assert api_key.encode() not in payload
    assert source_ciphertext not in payload
    container = json.loads(payload)
    assert container["format"] == "statstalk-api-key-backup"
    assert container["version"] == 1
    assert container["kdf"]["name"] == "scrypt"
    assert container["cipher"]["name"] == "aes-256-gcm"

    destination = _service(tmp_path, "destination", AccountProvider(b"account-b"), codec)
    resolution = destination.import_backup(payload, password)

    assert resolution.state == "configured"
    assert destination.store.read() == api_key
    assert destination.store.path.read_bytes().startswith(b"dpapi:account-b:")
    assert destination.env_path.read_text(encoding="utf-8") == (
        f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n"
    )


@pytest.mark.parametrize(
    ("password", "confirmation", "expected_code"),
    [
        ("short", "short", "backup_password_weak"),
        ("long-enough-password", "different-password", "backup_password_mismatch"),
        ("", "", "backup_password_required"),
    ],
)
def test_export_enforces_password_policy_and_confirmation(
    tmp_path,
    codec,
    password,
    confirmation,
    expected_code,
):
    service = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    service.replace("sk-private")

    with pytest.raises(SecretProtectionError) as error:
        service.export_backup(password, confirmation)

    assert error.value.code == expected_code


def test_wrong_password_preserves_existing_key_and_config(tmp_path, codec, caplog):
    source = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    source.replace("sk-import-me")
    payload = source.export_backup("right-password", "right-password")

    destination = _service(tmp_path, "destination", AccountProvider(b"account-b"), codec)
    destination.replace("sk-keep-existing")
    previous_ciphertext = destination.store.path.read_bytes()
    previous_config = destination.env_path.read_bytes()

    with pytest.raises(SecretProtectionError) as error:
        destination.import_backup(payload, "wrong-password")

    assert error.value.code == "backup_authentication_failed"
    assert destination.store.path.read_bytes() == previous_ciphertext
    assert destination.env_path.read_bytes() == previous_config
    assert destination.store.read() == "sk-keep-existing"
    assert "right-password" not in caplog.text
    assert "wrong-password" not in caplog.text
    assert "sk-import-me" not in caplog.text


def test_corrupt_ciphertext_and_unsupported_version_are_explicit(tmp_path, codec):
    source = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    source.replace("sk-import-me")
    payload = source.export_backup("right-password", "right-password")

    corrupt = json.loads(payload)
    ciphertext = bytearray(base64.b64decode(corrupt["cipher"]["ciphertext"]))
    ciphertext[-1] ^= 1
    corrupt["cipher"]["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    with pytest.raises(SecretProtectionError) as corrupt_error:
        source.import_backup(json.dumps(corrupt).encode(), "right-password")
    assert corrupt_error.value.code == "backup_authentication_failed"

    unsupported = json.loads(payload)
    unsupported["version"] = 99
    with pytest.raises(SecretProtectionError) as version_error:
        source.import_backup(json.dumps(unsupported).encode(), "right-password")
    assert version_error.value.code == "backup_unsupported_version"


def test_import_rejects_untrusted_kdf_cost_before_deriving(tmp_path, codec, monkeypatch):
    source = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    source.replace("sk-import-me")
    container = json.loads(source.export_backup("right-password", "right-password"))
    container["kdf"]["n"] = 2**30
    derive_called = False

    def record_derive(*_args):
        nonlocal derive_called
        derive_called = True
        return b""

    monkeypatch.setattr(codec, "_derive", record_derive)
    with pytest.raises(SecretProtectionError) as error:
        source.import_backup(json.dumps(container).encode(), "right-password")

    assert error.value.code == "backup_unsupported_parameters"
    assert derive_called is False


def test_dpapi_reencryption_failure_rolls_back_without_leaking_secrets(
    tmp_path,
    codec,
    caplog,
):
    source = _service(tmp_path, "source", AccountProvider(b"account-a"), codec)
    source.replace("sk-import-me")
    payload = source.export_backup("right-password", "right-password")

    provider = AccountProvider(b"account-b")
    destination = _service(tmp_path, "destination", provider, codec)
    destination.replace("sk-keep-existing")
    previous_ciphertext = destination.store.path.read_bytes()
    previous_config = destination.env_path.read_bytes()
    provider.fail_protection = True

    with pytest.raises(SecretProtectionError) as error:
        destination.import_backup(payload, "right-password")

    provider.fail_protection = False
    assert error.value.code == "secret_encrypt_failed"
    assert destination.store.path.read_bytes() == previous_ciphertext
    assert destination.env_path.read_bytes() == previous_config
    assert destination.store.read() == "sk-keep-existing"
    assert "sk-import-me" not in str(error.value)
    assert "sk-import-me" not in caplog.text
    assert "right-password" not in caplog.text


def test_backup_rejects_oversized_and_non_json_payloads(tmp_path, codec):
    service = _service(tmp_path, "destination", AccountProvider(b"account-b"), codec)

    with pytest.raises(SecretProtectionError) as oversized:
        service.import_backup(b"x" * (64 * 1024 + 1), "right-password")
    with pytest.raises(SecretProtectionError) as invalid:
        service.import_backup(b"not-json", "right-password")

    assert oversized.value.code == "backup_too_large"
    assert invalid.value.code == "backup_invalid"
