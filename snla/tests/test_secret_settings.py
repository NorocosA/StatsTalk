from __future__ import annotations

from unittest.mock import patch

import snla.config as cfg
from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretProtectionError, SecretStore
from snla.ui.security import loopback_security
from snla.ui.server import app


class FakeDPAPIProvider:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("cannot decrypt")
        return ciphertext.removeprefix(b"protected:")[::-1]


class AccountMismatchProvider(FakeDPAPIProvider):
    def unprotect(self, ciphertext: bytes) -> bytes:
        raise ValueError("account mismatch containing sk-do-not-expose")


class LeakyProtectProvider(FakeDPAPIProvider):
    def protect(self, plaintext: bytes) -> bytes:
        raise SecretProtectionError(
            "secret_encrypt_failed",
            f"failed while handling {plaintext.decode()}",
        )


def _authenticated_client():
    client = app.test_client()
    bootstrap_token = loopback_security.begin_launch("http://127.0.0.1:43125")
    bootstrap = client.post(
        "/api/bootstrap",
        json={"bootstrap_token": bootstrap_token},
    )
    client.environ_base["HTTP_AUTHORIZATION"] = f"Bearer {bootstrap.get_json()['session_token']}"
    return client


def test_settings_replacement_persists_dpapi_marker_without_returning_key(
    tmp_path,
    monkeypatch,
):
    api_key = "sk-settings-private"
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_MODEL=test-model\n", encoding="utf-8")
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    service = ApiKeyService(SecretStore(secret_path, FakeDPAPIProvider()), env_path)
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service, raising=False)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", service.resolve(""))
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    with _authenticated_client() as client, patch("snla.ui.server._save_env_file"):
        response = client.post("/api/settings", json={"LLM_API_KEY": api_key})

    assert response.status_code == 200
    assert api_key.encode() not in response.data
    assert api_key not in env_path.read_text(encoding="utf-8")
    assert f"LLM_API_KEY_STORAGE={API_KEY_MARKER}" in env_path.read_text(encoding="utf-8")
    assert api_key.encode() not in secret_path.read_bytes()
    assert api_key == cfg.LLM_API_KEY
    assert response.get_json()["api_key_status"]["state"] == "configured"


def test_settings_migrates_legacy_key_only_after_explicit_consent(tmp_path, monkeypatch):
    legacy_key = "sk-legacy-settings"
    env_path = tmp_path / ".env"
    env_path.write_text(f"LLM_API_KEY={legacy_key}\n", encoding="utf-8")
    service = ApiKeyService(
        SecretStore(tmp_path / "app-data" / "secure_key.bin", FakeDPAPIProvider()),
        env_path,
    )
    pending = service.resolve(legacy_key)
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", pending)
    monkeypatch.setattr(cfg, "_LEGACY_LLM_API_KEY", legacy_key)
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    with _authenticated_client() as client, patch("snla.ui.server._save_env_file"):
        before = client.get("/api/settings")
        response = client.post(
            "/api/settings",
            json={"MIGRATE_LLM_API_KEY": True},
        )

    assert before.get_json()["LLM_API_KEY_STATE"] == "migration_required"
    assert response.status_code == 200
    assert legacy_key.encode() not in response.data
    assert env_path.read_text(encoding="utf-8") == (f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n")
    assert response.get_json()["api_key_status"]["state"] == "configured"


def test_settings_deletes_key_marker_ciphertext_and_runtime_value(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    service = ApiKeyService(
        SecretStore(tmp_path / "app-data" / "secure_key.bin", FakeDPAPIProvider()),
        env_path,
    )
    configured = service.replace("sk-delete-settings")
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", configured)
    monkeypatch.setattr(cfg, "_LEGACY_LLM_API_KEY", "")
    monkeypatch.setattr(cfg, "LLM_API_KEY", configured.api_key)

    with _authenticated_client() as client, patch("snla.ui.server._save_env_file"):
        response = client.post(
            "/api/settings",
            json={"DELETE_LLM_API_KEY": True},
        )

    assert response.status_code == 200
    assert cfg.LLM_API_KEY == ""
    assert not service.store.path.exists()
    assert "LLM_API_KEY" not in env_path.read_text(encoding="utf-8")
    assert response.get_json()["api_key_status"]["state"] == "missing"


def test_settings_and_status_expose_only_reentry_state_after_dpapi_failure(
    tmp_path,
    monkeypatch,
):
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    secret_path.parent.mkdir()
    secret_path.write_bytes(b"encrypted-secret-material")
    service = ApiKeyService(
        SecretStore(secret_path, AccountMismatchProvider()),
        tmp_path / ".env",
    )
    failed = service.resolve(API_KEY_MARKER)
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", failed)
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    with _authenticated_client() as client:
        settings_response = client.get("/api/settings")
        status_response = client.get("/api/status")

    assert settings_response.get_json()["LLM_API_KEY_STATE"] == "reenter_required"
    assert settings_response.get_json()["LLM_CLOUD_AVAILABLE"] is False
    assert settings_response.get_json()["LLM_API_KEY_ACTION"] == "enter_api_key"
    assert status_response.get_json()["api_key_status"]["state"] == "reenter_required"
    combined = settings_response.data + status_response.data
    assert b"sk-do-not-expose" not in combined
    assert b"encrypted-secret-material" not in combined


def test_model_discovery_is_disabled_until_failed_dpapi_key_is_reentered(
    tmp_path,
    monkeypatch,
):
    secret_path = tmp_path / "app-data" / "secure_key.bin"
    secret_path.parent.mkdir()
    secret_path.write_bytes(b"unreadable-ciphertext")
    service = ApiKeyService(
        SecretStore(secret_path, AccountMismatchProvider()),
        tmp_path / ".env",
    )
    failed = service.resolve(API_KEY_MARKER)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", failed)
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    with _authenticated_client() as client, patch("urllib.request.build_opener") as opener:
        response = client.post(
            "/api/models",
            json={"endpoint": "https://api.example.com/v1/chat", "api_key": ""},
        )

    assert response.status_code == 409
    assert response.get_json()["code"] == "api_key_reentry_required"
    assert response.get_json()["api_key_status"]["action"] == "enter_api_key"
    opener.assert_not_called()


def test_provider_failure_never_exposes_api_key_in_response_or_logs(
    tmp_path,
    monkeypatch,
    caplog,
):
    api_key = "sk-provider-must-not-leak"
    service = ApiKeyService(
        SecretStore(tmp_path / "secure_key.bin", LeakyProtectProvider()),
        tmp_path / ".env",
    )
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", service.resolve(""))
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    with _authenticated_client() as client, patch("snla.ui.server._save_env_file"):
        response = client.post("/api/settings", json={"LLM_API_KEY": api_key})

    assert response.status_code == 500
    assert response.get_json()["code"] == "secret_encrypt_failed"
    assert api_key.encode() not in response.data
    assert api_key not in caplog.text
