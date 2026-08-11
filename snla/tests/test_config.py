from __future__ import annotations

import sys
from pathlib import Path


class FakeDPAPIProvider:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("cannot decrypt")
        return ciphertext.removeprefix(b"protected:")[::-1]


def test_packaged_config_path_is_persistent_across_meipass_directories(
    tmp_path,
    monkeypatch,
):
    import snla.config as cfg

    app_data = tmp_path / "roaming"
    monkeypatch.setenv("APPDATA", str(app_data))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle-one"), raising=False)
    first = cfg.configuration_file_path(packaged=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle-two"))
    second = cfg.configuration_file_path(packaged=True)

    expected = app_data / "StatsTalk" / "config.env"
    assert first == expected
    assert second == expected
    assert Path(sys._MEIPASS) not in first.parents


def test_server_save_and_reload_use_the_shared_config_path(tmp_path, monkeypatch):
    import snla.config as cfg
    import snla.ui.server as server

    config_path = tmp_path / "app-data" / "config.env"
    legacy_project_root = tmp_path / "legacy-project-root"
    legacy_project_root.mkdir()
    monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
    monkeypatch.setattr(server, "PROJECT_ROOT", legacy_project_root)
    monkeypatch.setattr(cfg, "LLM_MODEL", "saved-model")

    server._save_env_file()

    assert config_path.exists()
    assert not (legacy_project_root / ".env").exists()
    assert "LLM_MODEL=saved-model" in config_path.read_text(encoding="utf-8")

    config_path.write_text("LLM_MODEL=reloaded-model\n", encoding="utf-8")
    changed = cfg.reload_config()

    assert "LLM_MODEL" in changed
    assert cfg.LLM_MODEL == "reloaded-model"


def test_spss_availability_respects_backend_and_executable(monkeypatch):
    import snla.config as cfg

    monkeypatch.setattr(cfg, "STATS_BACKEND", "spss")
    monkeypatch.setattr(cfg.os.path, "isfile", lambda path: path == cfg.SPSS_EXECUTABLE)

    assert cfg.check_spss_available() is True

    monkeypatch.setattr(cfg, "STATS_BACKEND", "python")
    assert cfg.check_spss_available() is True


def test_validate_reports_missing_spss_key_and_insecure_endpoint(monkeypatch):
    import snla.config as cfg

    monkeypatch.setattr(cfg, "STATS_BACKEND", "spss")
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")
    monkeypatch.setattr(cfg, "LLM_MOCK", False)
    monkeypatch.setattr(cfg, "LLM_ENDPOINT", "http://api.example.com/v1")
    monkeypatch.setattr(cfg.os.path, "exists", lambda path: False)

    warnings = cfg.validate()

    assert any("SPSS" in warning for warning in warnings)
    assert any("LLM_API_KEY" in warning for warning in warnings)
    assert any("LLM_ENDPOINT" in warning for warning in warnings)


def test_reload_config_returns_empty_when_env_file_is_missing(monkeypatch):
    import snla.config as cfg

    monkeypatch.setattr(cfg.os.path, "exists", lambda path: False)

    assert cfg.reload_config() == []


def test_reload_config_applies_mcp_opt_in_state(tmp_path, monkeypatch):
    import snla.config as cfg

    config_path = tmp_path / "config.env"
    config_path.write_text("MCP_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "CONFIG_PATH", config_path)
    monkeypatch.setattr(cfg, "MCP_ENABLED", True)

    changed = cfg.reload_config()

    assert "MCP_ENABLED" in changed
    assert cfg.MCP_ENABLED is False


def test_reload_config_maps_spss_path_and_preserves_types():
    import snla.config as cfg

    env_path = cfg.CONFIG_PATH
    original_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None

    try:
        env_path.write_text(
            "\n".join(
                [
                    r"SPSS_PATH=C:\Stats\stats.exe",
                    "STATS_BACKEND=python",
                    "LLM_MOCK=true",
                    "LLM_MAX_OUTPUT_TOKENS=1234",
                    "SPSS_EXECUTION_TIMEOUT=77",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        changed = cfg.reload_config()

        assert "SPSS_EXECUTABLE" in changed
        assert cfg.SPSS_EXECUTABLE == r"C:\Stats\stats.exe"
        assert cfg.LLM_MOCK is True
        assert cfg.LLM_MAX_OUTPUT_TOKENS == 1234
        assert cfg.SPSS_EXECUTION_TIMEOUT == 77
    finally:
        if original_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_text, encoding="utf-8")
        cfg.reload_config()


def test_reload_config_ignores_an_insecure_public_llm_endpoint():
    import snla.config as cfg

    env_path = cfg.CONFIG_PATH
    original_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    original_endpoint = cfg.LLM_ENDPOINT

    try:
        env_path.write_text(
            "LLM_ENDPOINT=http://api.example.com/v1/chat/completions\n",
            encoding="utf-8",
        )

        changed = cfg.reload_config()

        assert "LLM_ENDPOINT" not in changed
        assert original_endpoint == cfg.LLM_ENDPOINT
    finally:
        if original_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_text, encoding="utf-8")
        cfg.LLM_ENDPOINT = original_endpoint


def test_reload_config_skips_unknown_and_malformed_values():
    import snla.config as cfg

    env_path = cfg.CONFIG_PATH
    original_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    original_timeout = cfg.SPSS_EXECUTION_TIMEOUT

    try:
        env_path.write_text(
            "UNKNOWN_SETTING=ignored\nSPSS_EXECUTION_TIMEOUT=not-an-integer\n",
            encoding="utf-8",
        )

        changed = cfg.reload_config()

        assert "UNKNOWN_SETTING" not in changed
        assert "SPSS_EXECUTION_TIMEOUT" not in changed
        assert original_timeout == cfg.SPSS_EXECUTION_TIMEOUT
    finally:
        if original_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_text, encoding="utf-8")


def test_reload_config_disables_legacy_plaintext_until_migration(tmp_path, monkeypatch):
    import snla.config as cfg
    from snla.secrets import ApiKeyService, SecretStore

    env_path = cfg.CONFIG_PATH
    original_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    service = ApiKeyService(
        SecretStore(tmp_path / "secure_key.bin", FakeDPAPIProvider()),
        env_path,
    )
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", service.resolve(""))
    monkeypatch.setattr(cfg, "_LEGACY_LLM_API_KEY", "")
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    try:
        env_path.write_text("LLM_API_KEY=sk-legacy-reload\n", encoding="utf-8")

        changed = cfg.reload_config()

        assert "LLM_API_KEY" in changed
        assert cfg.LLM_API_KEY == ""
        assert cfg.api_key_public_status()["state"] == "migration_required"
        assert cfg.api_key_public_status()["cloud_available"] is False
    finally:
        if original_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_text, encoding="utf-8")


def test_reload_config_resolves_dpapi_storage_marker(tmp_path, monkeypatch):
    import snla.config as cfg
    from snla.secrets import API_KEY_MARKER, ApiKeyService, SecretStore

    env_path = cfg.CONFIG_PATH
    original_text = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    store = SecretStore(tmp_path / "secure_key.bin", FakeDPAPIProvider())
    store.replace("sk-secure-reload")
    service = ApiKeyService(store, env_path)
    monkeypatch.setattr(cfg, "_API_KEY_SERVICE", service)
    monkeypatch.setattr(cfg, "_API_KEY_RESOLUTION", service.resolve(""))
    monkeypatch.setattr(cfg, "_LEGACY_LLM_API_KEY", "")
    monkeypatch.setattr(cfg, "LLM_API_KEY", "")

    try:
        env_path.write_text(
            f"LLM_API_KEY_STORAGE={API_KEY_MARKER}\n",
            encoding="utf-8",
        )

        changed = cfg.reload_config()

        assert "LLM_API_KEY_STORAGE" in changed
        assert cfg.LLM_API_KEY == "sk-secure-reload"
        assert cfg.api_key_public_status()["state"] == "configured"
    finally:
        if original_text is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_text, encoding="utf-8")
