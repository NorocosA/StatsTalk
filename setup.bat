@echo off
chcp 65001 >nul
title StatsTalk 一键安装

echo.
echo  ╔══════════════════════════════════════╗
echo  ║     StatsTalk — 一键安装脚本        ║
echo  ╚══════════════════════════════════════╝
echo.

:: ── Step 1: Check Python ────────────────────────────────────────
echo [1/4] 检查 Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    echo         下载: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version
echo        ✓ Python 已就绪

:: ── Step 2: Create venv ─────────────────────────────────────────
echo.
echo [2/4] 创建虚拟环境...
if exist "venv\" (
    echo        venv 已存在，跳过创建
) else (
    python -m venv venv
    echo        ✓ venv 创建完成
)

:: ── Step 3: Install dependencies ─────────────────────────────────
echo.
echo [3/4] 安装依赖...
call venv\Scripts\activate
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [警告] 部分依赖安装失败，请手动检查
) else (
    echo        ✓ 依赖安装完成
)

:: ── Step 4: Configure .env ──────────────────────────────────────
echo.
echo [4/4] 配置环境...
if not exist ".env" (
    copy .env.example .env >nul
    echo        ✓ .env 已创建 (Demo 模式，无需 API Key)
    echo.
    echo   ┌─────────────────────────────────────────┐
    echo   │  当前为 Demo 模式 (LLM_MOCK=true)        │
    echo   │  无需 API Key 或 SPSS，可直接试用！      │
    echo   │                                         │
    echo   │  如需真实 LLM 分析：                      │
    echo   │  1. 编辑 .env 文件                        │
    echo   │  2. 填入 LLM_API_KEY                      │
    echo   │  3. 将 LLM_MOCK 改为 false                 │
    echo   └─────────────────────────────────────────┘
) else (
    echo        .env 已存在，跳过
)

:: ── Launch ──────────────────────────────────────────────────────
echo.
echo ═══════════════════════════════════════════
echo   安装完成！正在启动 StatsTalk...
echo ═══════════════════════════════════════════
echo.

start "" venv\Scripts\python.exe launcher.py

echo.
echo   浏览器访问: http://localhost:8501
echo   关闭此窗口不会影响 StatsTalk 运行
echo.
pause
