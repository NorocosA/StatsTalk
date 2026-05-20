# SPSS Natural Language Assistant (SNLA)

用说话的方式完成统计分析——让 SPSS 操作零门槛。

## 快速开始

```powershell
# 1. 安装
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置（复制 .env.example 为 .env，填入 SPSS 路径和 LLM Key）
copy .env.example .env

# 3. 启动
python launcher.py

# 或者命令行 Demo
python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav
```

## 功能

| 功能 | 说明 |
|------|------|
| 🗣 自然语言输入 | "比较男女成绩差异" → 自动执行 t 检验 |
| 🧠 LLM 智能规划 | DeepSeek V4 Flash 识别意图、推荐方法、匹配变量 |
| 📋 模板语法生成 | 10+ 预置 SPSS 模板，零幻觉，100% 通过校验 |
| ⚙️ SPSS 自动执行 | Python Submit 模式，OMS XML 输出解析 |
| 📊 白话解读 | 统计约束层防过度推断，LLM 润滑为社科本科生可懂语言 |
| 🔁 多轮对话 | "那换成班级差异呢？" 自动切换分析变量 |
| 🛑 取消中断 | 长任务可随时取消，自动清理 SPSS 进程和临时文件 |
| ⚠️ 灰名单确认 | COMPUTE/RECODE 等数据修改操作触发确认，在临时副本上执行 |
| 📥 Word 导出 | APA 格式报告一键下载 |
| 🔒 隐私保护 | 仅变量名/类型/标签发云端 LLM，原始数据永不过网 |
| 🛡 安全沙箱 | 黑名单拦截 SAVE/DELETE/HOST COMMAND 等危险操作 |
| 🖥 桌面应用 | PyWebView 原生窗口（fallback 浏览器），`SNLA.exe` 单文件分发 |
| 🔧 设置持久化 | API 配置保存到本地 `.env`，下次启动自动加载 |

## 支持的分析

独立样本 t · 单因素 ANOVA · 配对 t · Pearson 相关 · Spearman 相关 · 简单回归 · 卡方检验 · 交叉表 · 描述统计 · 频率分析 · Mann-Whitney U · Kruskal-Wallis

## 测试

```powershell
python -m pytest snla/tests/ -v          # 56 单元/集成测试
python scripts/verify_combined.py        # 65 例真实 LLM 验证 (含 airline.sav)
python scripts/verify_50_cases.py --mock # 50 例 Mock 语法验证
```

## 项目结构

```
snla/
├── config.py          # 集中配置 (从 .env 读取)
├── session.py         # 多轮对话状态管理
├── data/              # 数据读写 + 隐私过滤
├── llm/               # LLM 客户端 + Prompt 模板 (意图+方法)
├── syntax/            # 语法模板 + 安全校验 (黑/灰名单)
├── executor/          # SPSS 进程管理器 (Python/Batch 双模式)
├── parser/            # OMS XML + LST 输出解析
├── explainer/         # 统计约束层 + 白话解读 + Word 导出
├── ui/                # Flask API + 前端页面
└── tests/             # 56 单元/集成测试

scripts/               # E2E Demo / 50/65 例验证 / 崩溃恢复
docs/                  # 用户指南
data/fixtures/         # test_data.sav + airline.sav (25K 行)
launcher.py            # 桌面启动器
snla.spec              # PyInstaller 打包配置
```

## 打包

```powershell
pyinstaller snla.spec --noconfirm
# 输出: dist/SNLA.exe (约 78 MB 单文件)
```

## 许可

内部项目
