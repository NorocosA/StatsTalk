from __future__ import annotations

from pathlib import Path


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
