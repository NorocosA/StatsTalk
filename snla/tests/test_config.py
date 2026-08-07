from __future__ import annotations

from pathlib import Path


def test_spss_availability_respects_backend_and_executable(monkeypatch):
    import snla.config as cfg

    monkeypatch.setattr(cfg, "STATS_BACKEND", "spss")
    monkeypatch.setattr(cfg.os.path, "exists", lambda path: path == cfg.SPSS_EXECUTABLE)

    assert cfg.check_spss_available() is True

    monkeypatch.setattr(cfg, "STATS_BACKEND", "python")
    assert cfg.check_spss_available() is False


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


def test_reload_config_skips_unknown_and_malformed_values():
    import snla.config as cfg

    config_path = Path(cfg.__file__).resolve()
    env_path = config_path.parent.parent / ".env"
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
