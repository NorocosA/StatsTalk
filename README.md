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
streamlit run snla/ui/streamlit_app.py

# 或者命令行 Demo
python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav
```

## 功能

| 功能 | 说明 |
|------|------|
| 🗣 自然语言输入 | "比较男女成绩差异" → 自动执行 t 检验 |
| 🧠 LLM 智能推荐 | DeepSeek V4 Flash 自动识别意图、推荐方法、生成 SPSS 语法 |
| ⚙️ SPSS 自动执行 | Python Submit 模式，OMS XML 输出解析 |
| 📊 白话解读 | 统计结果 → 社科本科生可读懂的自然语言 |
| 🔁 多轮对话 | "那换成班级差异呢？" 自动切换分析变量 |
| 📥 Word 导出 | APA 格式报告一键下载 |
| 🔒 隐私保护 | 只发变量名/类型/标签给云端 LLM，原始数据不过网 |
| 🛡 安全沙箱 | 黑名单拦截危险操作（SAVE/DELETE/HOST COMMAND） |

## 支持的分析

独立样本 t 检验 · 单因素 ANOVA · 配对 t 检验 · Pearson/Spearman 相关 · 简单/多元回归 · 卡方检验 · 描述统计 · 频率分析 · Mann-Whitney U · Kruskal-Wallis

## 测试

```powershell
python -m pytest snla/tests/ -v    # 56 tests
```

## 项目结构

```
snla/
├── config.py          # 集中配置
├── session.py         # 多轮对话状态
├── data/              # 数据读写 + 隐私过滤
├── llm/               # LLM 客户端 + Prompt 模板
├── syntax/            # 语法校验 + 模板兜底
├── executor/          # SPSS 进程管理器
├── parser/            # OMS XML + LST 输出解析
├── explainer/         # 统计约束层 + 白话解读 + Word 导出
├── rag/               # RAG 知识库（可选）
├── ui/                # Streamlit 前端
└── tests/             # 56 单元/集成测试

scripts/               # E2E Demo / 批量验证 / 崩溃恢复
docs/                  # 用户指南
data/                  # 测试数据集（4/11 变量版本）
```

## 许可

内部项目
