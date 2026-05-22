"""Method trust whitelist loader for P5-4 no-SPSS mode.

Priority: JSON file (runtime) > embedded constant (fallback)

加载 P5-3 交叉验证结果，确定哪些统计方法在无 SPSS 的独立模式下
仍然可以信任其 Python (pingouin) 计算结果。
"""

import json
import os
from pathlib import Path

# --- 从 P5-3 输出加载或使用嵌入式回退 ---

_TRUST_JSON_PATH = Path(__file__).resolve().parent.parent / "p0_output" / "method_trust.json"
_TRUSTED_METHODS: set[str] = set()
_TRUST_LOADED_FROM = "embedded"


def _load_trust_json():
    """加载 method_trust.json，返回受信任的方法名集合。"""
    if _TRUST_JSON_PATH.exists():
        try:
            with open(_TRUST_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            trusted = set()
            for method, info in data.get("methods", {}).items():
                if info.get("trusted", False):
                    trusted.add(method)
            return trusted, "json"
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    # 回退到嵌入式常量
    from snla.config import _FALLBACK_TRUSTED_METHODS
    return _FALLBACK_TRUSTED_METHODS, "embedded"


_TRUSTED_METHODS, _TRUST_LOADED_FROM = _load_trust_json()

# Method aliases — normalise alternative names to canonical keys
# (must match METHOD_ALIASES in snla/executor/adapter.py)
_METHOD_ALIASES: dict[str, str] = {
    "crosstabs": "chi_square",
}


def is_method_trusted(method: str) -> bool:
    """检查某个方法在独立（无 SPSS）模式下是否可信。

    仅当 P5-3 交叉验证确认该方法与 SPSS 输出无冲突时返回 True。
    自动解析方法别名（如 crosstabs → chi_square）。
    """
    canonical = _METHOD_ALIASES.get(method, method)
    return canonical in _TRUSTED_METHODS


def get_trusted_methods() -> set[str]:
    """返回所有受信任方法的名称集合（副本）。"""
    return _TRUSTED_METHODS.copy()


def trust_loaded_from() -> str:
    """返回信任数据来源：'json' 或 'embedded'。"""
    return _TRUST_LOADED_FROM
