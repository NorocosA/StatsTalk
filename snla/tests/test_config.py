from __future__ import annotations

from pathlib import Path


class FakeDPAPIProvider:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"protected:"):
            raise ValueError("cannot decrypt")
        return ciphertext.removeprefix(b"protected:")[::-1]


def test_reload_config_maps_spss_path_and_preserves_types():
    import snla.config as cfg

    config_path = Path(cfg.__file__).resolve()
    env_path = config_path.parent.parent / ".env"
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

    config_path = Path(cfg.__file__).resolve()
    env_path = config_path.parent.parent / ".env"
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


def test_reload_config_disables_legacy_plaintext_until_migration(tmp_path, monkeypatch):
    import snla.config as cfg
    from snla.secrets import ApiKeyService, SecretStore

    env_path = Path(cfg.__file__).resolve().parent.parent / ".env"
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

    env_path = Path(cfg.__file__).resolve().parent.parent / ".env"
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
