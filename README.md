# StatsTalk

用说话的方式完成统计分析。支持 SPSS 和 Python 双引擎。

## 快速开始

```powershell
# 1. 安装
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（复制 .env.example 为 .env，填入 LLM Key，SPSS 可选）
copy .env.example .env

# 3. 启动
python launcher.py

# 无 SPSS 模式（纯 Python 后端）
# 在 .env 中设置 STATS_BACKEND=python 即可

# 命令行 Demo
python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav
```

## 功能

| 功能 | 说明 |
|------|------|
| 🗣 自然语言输入 | "比较男女成绩差异" → 自动执行 t 检验 |
| 🧠 LLM 智能规划 | DeepSeek V4 Flash 识别意图、推荐方法、匹配变量 |
| 🔀 双统计引擎 | SPSS (Python Submit / Batch) 或 Python (pingouin)，可自动检测、可配置切换 |
| 📋 模板语法生成 | 10+ 预置模板，零幻觉，100% 通过校验 |
| 📊 白话解读 | 统计约束层防过度推断，LLM 润色为社科本科生可懂语言 |
| 🔁 多轮对话 | "那换成班级差异呢？" 自动切换分析变量 |
| 🛑 取消中断 | 长任务可随时取消，自动清理进程和临时文件 |
| ⚠️ 灰名单确认 | COMPUTE/RECODE 等修改操作触发确认，在临时副本上执行 |
| 📥 Word 导出 | APA 格式报告一键下载 |
| 🔒 隐私保护 | 仅变量结构信息发云端 LLM，value_labels 自动剥离，原始数据永不过网 |
| 🛡 安全沙箱 | 黑名单拦截 SAVE/DELETE/HOST COMMAND 等危险操作，上传限制 500MB 白名单校验 |
| 🖥 桌面应用 | PyWebView 原生窗口（fallback 浏览器），`SNLA.exe` 单文件分发 |
| 🔧 设置持久化 | API 配置保存到本地 `.env`，下次启动自动加载 |
| 🌐 多渠道支持 | MCP Server（7 工具）+ OpenClaw Skill，支持 Claude Desktop 等客户端 |

## 支持的分析

独立样本 t · 单因素 ANOVA · 配对 t · Pearson 相关 · Spearman 相关 · 简单回归 · 卡方检验 · 交叉表 · 描述统计 · 频率分析 · Mann-Whitney U · Kruskal-Wallis

**双后端覆盖**: 11/12 方法经 SPSS-Python 交叉验证，`simple_regression` 在无 SPSS 时降级为原始输出。

## 测试

```powershell
python -m pytest snla/tests/ -v          # 63 单元/集成测试（不需要 SPSS/LLM）
python -m pytest snla/tests/ -v -m "not slow"  # CI-safe
python scripts/verify_combined.py        # 65 例真实 LLM 验证 (含 airline.sav)
python scripts/verify_50_cases.py --mock # 50 例 Mock 语法验证
```

## 项目结构

```
snla/
├── config.py          # 集中配置 (从 .env 读取)
├── session.py         # 多轮对话状态管理
├── trust.py           # 统计方法信任白名单 (JSON 运行时加载)
├── mcp_server.py      # MCP Server — FastMCP 7 工具，支持外部 AI 客户端
├── data/              # 数据读写 (.sav/.csv) + 隐私过滤
├── llm/               # LLM 客户端 (指数退避重试) + Prompt 模板
├── syntax/            # 语法模板 + 安全校验 (黑/灰名单)
├── executor/          # 双后端适配器 (SPSS/Python) + SPSS 进程管理
├── parser/            # OMS XML + LST 输出解析
├── explainer/         # 统计约束层 + 白话解读 + Word 导出
├── orchestrator/      # 分析规划器 + 灰名单状态机 (Flask & MCP 共享)
├── ui/                # Flask REST API + 前端页面
├── rag/               # RAG 知识库模块 (构建中)
└── tests/             # 63 单元/集成测试

scripts/               # E2E Demo / 50/65 例验证 / MCP 集成测试
docs/                  # 用户指南
data/fixtures/         # test_data.sav + airline.sav (25K 行)
.opencode/skills/snla/ # OpenClaw Skill 配置
launcher.py            # 桌面启动器
snla.spec              # PyInstaller 打包配置
```

## MCP 多渠道接入

```powershell
# 启动 MCP Server（stdio 传输）
python snla/mcp_server.py

# OpenClaw 配置
openclaw mcp set snla --command python --args "snla/mcp_server.py"
```

提供 7 个工具: `snla_status`, `snla_upload`, `snla_variables`, `snla_analyze`, `snla_confirm`, `snla_cancel`, `snla_export`

## 打包

```powershell
pyinstaller snla.spec --noconfirm
# 输出: dist/SNLA.exe (约 78 MB 单文件)
```

## 许可

内部项目
