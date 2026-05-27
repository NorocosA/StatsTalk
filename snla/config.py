"""
SNLA 集中配置中心

所有配置从环境变量读取，提供合理默认值。
敏感信息（API Key）仅从 .env 文件加载，不硬编码。
"""

import os
from dotenv import load_dotenv

# 自动加载项目根目录下的 .env 文件
load_dotenv()


# ========== SPSS 配置 ==========
SPSS_EXECUTABLE = os.getenv(
    "SPSS_PATH",
    r"C:\Program Files\IBM\SPSS\Statistics\29\stats.exe"
)

# SPSS 自带的 Python 3 解释器（用于 spss.Submit() 语法执行）
# SPSS 26+: Python3/python.exe 在安装目录下
SPSS_PYTHON_PATH = os.getenv(
    "SPSS_PYTHON_PATH",
    r"C:\Program Files\IBM\SPSS\Statistics\26\Python3\python.exe"
)

# SPSS 语法执行模式: "python" (推荐, 通过 spss.Submit) | "batch" (stats.exe 批处理)
SPSS_EXEC_MODE = os.getenv("SPSS_EXEC_MODE", "python")

# ========== LLM 配置 ==========
LLM_ENDPOINT = os.getenv(
    "LLM_ENDPOINT",
    "https://opencode.ai/zen/go/v1/chat/completions"
)
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")

# ========== Token 控制 ==========
LLM_MAX_INPUT_TOKENS = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "2000"))
LLM_MAX_HISTORY_ROUNDS = int(os.getenv("LLM_MAX_HISTORY_ROUNDS", "3"))

# ========== 执行器配置 ==========
SPSS_EXECUTION_TIMEOUT = int(os.getenv("SPSS_EXECUTION_TIMEOUT", "120"))  # 秒
P0_OUTPUT_DIR = os.getenv("P0_OUTPUT_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "p0_output"))

# ========== 统计后端选择 ==========
STATS_BACKEND = os.getenv("STATS_BACKEND", "spss")  # "spss" | "python"

# ========== 调试与审计 ==========
LLM_CALL_LOG = os.getenv("LLM_CALL_LOG", "false").lower() == "true"
LLM_MOCK = os.getenv("LLM_MOCK", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# ========== P5-4: 方法信任白名单（嵌入式回退） ==========
# 当 p0_output/method_trust.json 不存在时使用（如全新安装、PyInstaller 打包后）
# 基于 2026-05-20 P5-3 验证结果：11/12 方法可信
_FALLBACK_TRUSTED_METHODS: set[str] = {
    "independent_t_test", "paired_t_test", "oneway_anova",
    "pearson_correlation", "spearman_correlation", "correlations",
    "chi_square", "crosstabs",
    "frequencies", "descriptives",
    "mann_whitney_u", "kruskal_wallis",
    # simple_regression 排除 — SPSS 解析器限制，无法测试
}


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
    if STATS_BACKEND == "spss":
        if not os.path.exists(SPSS_EXECUTABLE):
            warnings.append(f"SPSS 可执行文件不存在: {SPSS_EXECUTABLE}")
    # "python" 模式下不检查 SPSS 路径
    if not LLM_API_KEY and not LLM_MOCK:
        warnings.append("LLM_API_KEY 未配置且未启用 LLM_MOCK")
    return warnings
