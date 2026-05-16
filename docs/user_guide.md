# SPSS Natural Language Assistant — 用户指南

## 快速开始

### 1. 安装

```powershell
# 克隆项目
git clone <repo-url>
cd "SPSS Natural Language Assistant(SNLA)"

# 创建虚拟环境
python -m venv venv
venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入你的配置：

```ini
# SPSS 路径（必须）
SPSS_PATH=C:\Program Files\IBM\SPSS\Statistics\26\stats.exe
SPSS_PYTHON_PATH=C:\Program Files\IBM\SPSS\Statistics\26\Python3\python.exe

# LLM 配置（可选，不配则使用 MOCK 模式）
LLM_ENDPOINT=https://opencode.ai/zen/go/v1/chat/completions
LLM_API_KEY=your-api-key-here
LLM_MODEL=deepseek-v4-flash
```

### 3. 启动

```powershell
# 网页界面（推荐）
streamlit run snla/ui/streamlit_app.py

# 命令行 Demo
python scripts/e2e_demo.py --data-file data/fixtures/test_data.sav
```

---

## 使用说明

### 网页界面

1. **上传数据**：左侧点击"Browse files"，选择 `.sav` 或 `.csv` 文件
2. **输入问题**：在底部输入框用自然语言描述分析需求
3. **查看结果**：系统会自动推荐方法、生成语法、执行分析、返回白话解读

### 支持的分析类型

| 你的问题 | 自动执行 |
|----------|----------|
| "计算平均分和标准差" | 描述性统计 (DESCRIPTIVES) |
| "统计男女各多少人" | 频率分析 (FREQUENCIES) |
| "比较男女成绩差异" | 独立样本 t 检验 |
| "不同班级成绩有没有差异" | 单因素方差分析 (ANOVA) |
| "年龄和成绩有关系吗" | Pearson 相关分析 |
| "性别和专业选择有关系吗" | 卡方检验 (CROSSTABS) |
| "培训前后分数有变化吗" | 配对样本 t 检验 |
| "数据不服从正态分布" | Mann-Whitney U / Kruskal-Wallis |

### 追问功能

在一次分析完成后，可以直接追问：

```
👤 "比较男女成绩差异"
🤖 [t 检验结果...]

👤 "那换成班级呢？"
🤖 [自动切换为班级 × 成绩的 ANOVA]
```

### Word 报告导出

分析完成后点击 **📥 导出 Word 报告** 按钮，生成包含 APA 格式的完整报告。

---

## 数据格式要求

### .sav 文件（推荐）
IBM SPSS 原生格式，支持变量标签和值标签。

### .csv 文件
UTF-8 或 GBK 编码。第一行为列名，后续行为数据。

### 变量命名建议
- 使用有意义的中文或英文变量名（如 "gender", "score", "年龄"）
- 分类变量应设置值标签（如 gender: 1=男, 2=女）
- 避免使用姓名、手机号等隐私信息作为变量名

---

## 常见问题

**Q: 系统说"SPSS 不可用"？**
A: 检查 `.env` 中 `SPSS_PYTHON_PATH` 是否正确指向 SPSS 安装目录下的 `Python3/python.exe`。

**Q: 分析结果为空？**
A: 检查数据文件格式是否正确（.sav 或 .csv），变量名不能包含特殊字符。

**Q: 隐私安全吗？**
A: 系统只向云端 LLM 发送变量名、类型、标签——**绝不发送原始数据值**。敏感变量名（如"患者姓名"）会自动脱敏为 var_01、var_02。

---

## 测试

```powershell
# 运行全部测试
python -m pytest snla/tests/ -v

# 快速检查
python -m pytest snla/tests/ -q
```
