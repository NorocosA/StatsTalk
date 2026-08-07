"""
SNLA 集中配置中心

所有配置从环境变量读取，提供合理默认值。
敏感信息（API Key）仅从 .env 文件加载，不硬编码。
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from snla.llm.transport import EndpointPolicyError, require_secure_llm_endpoint
from snla.secrets import (
    API_KEY_MARKER,
    ApiKeyService,
    SecretProtectionError,
    SecretResolution,
    application_data_directory,
    create_api_key_service,
)


def configuration_file_path(*, packaged: bool | None = None) -> Path:
    """Return a persistent config path for development or packaged execution."""

    if packaged is None:
        packaged = bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"))
    if packaged:
        return application_data_directory() / "config.env"
    return Path(__file__).resolve().parent.parent / ".env"


CONFIG_PATH = configuration_file_path()
_ENV_PATH = str(CONFIG_PATH)

# 自动加载项目根目录下的 .env 文件
if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    # First run from packaged exe — auto-create .env for Demo mode
    _FIRST_RUN = True
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.write("# StatsTalk — auto-generated for Demo mode\n")
        f.write("LLM_MOCK=true\n")
        f.write("STATS_BACKEND=python\n")
        f.write("LLM_MAX_OUTPUT_TOKENS=10000\n")
    load_dotenv(_ENV_PATH)


# ========== SPSS 配置 ==========
SPSS_EXECUTABLE = os.getenv("SPSS_PATH", r"C:\Program Files\IBM\SPSS\Statistics\29\stats.exe")

# SPSS 自带的 Python 3 解释器（用于 spss.Submit() 语法执行）
# SPSS 26+: Python3/python.exe 在安装目录下
SPSS_PYTHON_PATH = os.getenv(
    "SPSS_PYTHON_PATH", r"C:\Program Files\IBM\SPSS\Statistics\26\Python3\python.exe"
)

# SPSS 语法执行模式: "python" (推荐, 通过 spss.Submit) | "batch" (stats.exe 批处理)
SPSS_EXEC_MODE = os.getenv("SPSS_EXEC_MODE", "python")

# ========== LLM 配置 ==========
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "https://opencode.ai/zen/go/v1/chat/completions")
_CONFIGURED_API_KEY_STORAGE = os.getenv("LLM_API_KEY_STORAGE", "")
_LEGACY_CONFIGURED_API_KEY = os.getenv("LLM_API_KEY", "")
_CONFIGURED_API_KEY = (
    API_KEY_MARKER if _CONFIGURED_API_KEY_STORAGE == API_KEY_MARKER else _LEGACY_CONFIGURED_API_KEY
)
_API_KEY_SERVICE: ApiKeyService = create_api_key_service(Path(_ENV_PATH))
_API_KEY_RESOLUTION = _API_KEY_SERVICE.resolve(_CONFIGURED_API_KEY)
_LEGACY_LLM_API_KEY = (
    _LEGACY_CONFIGURED_API_KEY if _API_KEY_RESOLUTION.state == "migration_required" else ""
)
LLM_API_KEY = _API_KEY_RESOLUTION.api_key
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# ========== Token 控制 ==========
LLM_MAX_INPUT_TOKENS = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "10000"))
LLM_MAX_HISTORY_ROUNDS = int(os.getenv("LLM_MAX_HISTORY_ROUNDS", "3"))

# ========== 执行器配置 ==========
SPSS_EXECUTION_TIMEOUT = int(os.getenv("SPSS_EXECUTION_TIMEOUT", "120"))  # 秒
P0_OUTPUT_DIR = os.getenv(
    "P0_OUTPUT_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "p0_output"),
)

# ========== 统计后端选择 ==========
STATS_BACKEND = os.getenv("STATS_BACKEND", "spss")  # "spss" | "python"

# ========== 调试与审计 ==========
LLM_CALL_LOG = os.getenv("LLM_CALL_LOG", "false").lower() == "true"
LLM_MOCK = os.getenv("LLM_MOCK", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"


def api_key_public_status() -> dict[str, object]:
    """Return API-key state without key or ciphertext material."""

    return _API_KEY_RESOLUTION.public_status()


def _apply_api_key_resolution(resolution: SecretResolution) -> None:
    global LLM_API_KEY, _API_KEY_RESOLUTION

    _API_KEY_RESOLUTION = resolution
    LLM_API_KEY = resolution.api_key


def replace_api_key(api_key: str) -> dict[str, object]:
    """Encrypt and persist a new API key for the current Windows user."""

    global _LEGACY_LLM_API_KEY

    resolution = _API_KEY_SERVICE.replace(api_key)
    _LEGACY_LLM_API_KEY = ""
    _apply_api_key_resolution(resolution)
    return resolution.public_status()


def migrate_legacy_api_key(*, consent: bool) -> dict[str, object]:
    """Migrate the pending plaintext key only after explicit consent."""

    global _LEGACY_LLM_API_KEY

    if not _LEGACY_LLM_API_KEY:
        raise SecretProtectionError(
            "no_legacy_api_key",
            "There is no legacy API key waiting for migration.",
        )
    resolution = _API_KEY_SERVICE.migrate_legacy(
        _LEGACY_LLM_API_KEY,
        consent=consent,
    )
    _LEGACY_LLM_API_KEY = ""
    _apply_api_key_resolution(resolution)
    return resolution.public_status()


def delete_api_key() -> dict[str, object]:
    """Remove the API-key marker, ciphertext, and runtime value."""

    global _LEGACY_LLM_API_KEY

    resolution = _API_KEY_SERVICE.delete()
    _LEGACY_LLM_API_KEY = ""
    _apply_api_key_resolution(resolution)
    return resolution.public_status()


def check_spss_available() -> bool:
    """检查本机是否实际可用 SPSS 可执行文件。

    当 STATS_BACKEND == "python" 时返回 False（用户已选择 Python 后端）。
    否则检查 SPSS_EXECUTABLE 是否存在。
    """
    if STATS_BACKEND == "python":
        return False  # 用户主动选择 Python，不检查 SPSS
    return os.path.exists(SPSS_EXECUTABLE)


def validate():
    """启动时校验关键配置，缺失项打印警告。

    根据 STATS_BACKEND 值决定是否检查 SPSS 路径：
      - "python"：完全跳过 SPSS 检查
      - "spss"：检查 SPSS 路径，但仅 WARN（不阻止启动）
    """
    warnings = []
    if STATS_BACKEND == "spss" and not os.path.exists(SPSS_EXECUTABLE):
        warnings.append(f"SPSS 可执行文件不存在: {SPSS_EXECUTABLE}")
    # "python" 模式下不检查 SPSS 路径
    if not LLM_API_KEY and not LLM_MOCK and _API_KEY_RESOLUTION.state != "missing":
        warnings.append(_API_KEY_RESOLUTION.message)
    if not LLM_API_KEY and not LLM_MOCK and _API_KEY_RESOLUTION.state == "missing":
        warnings.append("LLM_API_KEY 未配置且未启用 LLM_MOCK")
    try:
        require_secure_llm_endpoint(LLM_ENDPOINT)
    except EndpointPolicyError as exc:
        warnings.append(f"LLM_ENDPOINT 配置不安全: {exc}")
    return warnings


def reload_config() -> list[str]:
    """Reload .env and update module-level config variables. Returns list of changed keys.

    Only updates string-based config values (LLM_ENDPOINT, LLM_MODEL, SPSS_PYTHON_PATH, etc.).
    Computed values (int/bool conversions like LLM_MAX_INPUT_TOKENS) are skipped — restart required.
    """
    global _LEGACY_LLM_API_KEY

    from dotenv import dotenv_values

    env_path = CONFIG_PATH
    if not os.path.exists(env_path):
        return []

    new_values = dotenv_values(env_path)
    changed = []
    configured_storage = str(new_values.get("LLM_API_KEY_STORAGE") or "")
    legacy_key = str(new_values.get("LLM_API_KEY") or "")
    configured_key = API_KEY_MARKER if configured_storage == API_KEY_MARKER else legacy_key
    resolution = _API_KEY_SERVICE.resolve(configured_key)
    _LEGACY_LLM_API_KEY = legacy_key if resolution.state == "migration_required" else ""
    if resolution != _API_KEY_RESOLUTION:
        _apply_api_key_resolution(resolution)
        changed.append("LLM_API_KEY_STORAGE" if configured_storage else "LLM_API_KEY")

    aliases = {"SPSS_PATH": "SPSS_EXECUTABLE"}
    parsers = {
        "LLM_MAX_INPUT_TOKENS": int,
        "LLM_MAX_OUTPUT_TOKENS": int,
        "LLM_MAX_HISTORY_ROUNDS": int,
        "SPSS_EXECUTION_TIMEOUT": int,
        "LLM_CALL_LOG": _parse_bool,
        "LLM_MOCK": _parse_bool,
        "DEBUG": _parse_bool,
    }
    reloadable_keys = {
        "SPSS_PATH",
        "SPSS_EXECUTABLE",
        "SPSS_PYTHON_PATH",
        "SPSS_EXEC_MODE",
        "LLM_ENDPOINT",
        "LLM_API_KEY",
        "LLM_API_KEY_STORAGE",
        "LLM_MODEL",
        "LLM_MAX_INPUT_TOKENS",
        "LLM_MAX_OUTPUT_TOKENS",
        "LLM_MAX_HISTORY_ROUNDS",
        "SPSS_EXECUTION_TIMEOUT",
        "P0_OUTPUT_DIR",
        "STATS_BACKEND",
        "LLM_CALL_LOG",
        "LLM_MOCK",
        "DEBUG",
    }

    for env_key, raw_value in new_values.items():
        if env_key not in reloadable_keys:
            continue
        if env_key in {"LLM_API_KEY", "LLM_API_KEY_STORAGE"}:
            continue
        key = aliases.get(env_key, env_key)
        parser = parsers.get(key, str)
        try:
            value = parser(raw_value)
        except (TypeError, ValueError):
            continue
        if key == "LLM_ENDPOINT":
            try:
                value = require_secure_llm_endpoint(str(value).strip())
            except EndpointPolicyError as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "Ignored insecure LLM endpoint during reload: %s", exc.code
                )
                continue
        current = globals().get(key)
        if current != value:
            globals()[key] = value
            changed.append(key)

    if changed:
        import logging

        logging.getLogger(__name__).info("Config reloaded, changed: %s", changed)

    return changed


def _parse_bool(value: object) -> bool:
    return str(value).lower() == "true"
