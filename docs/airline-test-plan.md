# airline.sav Web 界面手动测试方案

> 数据: 25,976 行 × 24 列 | SPSS 模式 | LLM 真实模式

## 启动

```powershell
cd D:\Projects\StatsTalk
venv\Scripts\activate
python launcher.py
# 或 API only: python snla/ui/server.py
# 浏览器: http://localhost:8501
```

## 数据集概览

| 类别 | 变量名 | 中文含义 |
|------|--------|----------|
| 人口 | Gender, Age | 性别, 年龄 |
| 行程 | FlightDistance, TypeofTravel, Class, CustomerType | 飞行距离, 出行类型, 舱位, 客户类型 |
| 服务评价 | Inflightwifiservice, Foodanddrink, Seatcomfort, Onlineboarding, Baggagehandling, Checkinservice, Inflightservice, Cleanliness, Legroomservice, Inflightentertainment, Onboardservice | WiFi, 餐饮, 座椅, 值机, 行李, 登机, 服务, 清洁, 腿部空间, 娱乐, 机上服务 |
| 便利性 | DepartureANDArrivaltimeconvenient, EaseofOnlinebooking, Gatelocation | 时刻便利, 在线预订, 登机口 |
| 延迟 | DepartureDelayinMinutes, ArrivalDelayinMinutes | 出发/到达延迟(分钟) |
| 结果 | satisfaction | 满意度 |

---

## 测试场景

### 场景 1: 基本分析（验证核心管线）

| # | 输入 | 预期方法 | 验证点 |
|---|------|----------|--------|
| 1.1 | `飞行距离的平均值和中位数是多少` | descriptives | 返回均值、标准差，白话解读 |
| 1.2 | `显示满意度的频率分布` | frequencies | 频数表、百分比 |
| 1.3 | `统计各舱位等级的人数` | crosstabs/frequencies | 频数分布 |

### 场景 2: 假设检验

| # | 输入 | 预期方法 | 验证点 |
|---|------|----------|--------|
| 2.1 | `比较男性和女性的满意度是否有差异` | independent_t_test | t值、p值、白话结论 |
| 2.2 | `不同出行类型的飞行距离是否有显著差异` | oneway_anova | F值、p值、事后比较 |
| 2.3 | `舱位等级和满意度之间是否有关联` | chi_square/crosstabs | 卡方值、p值 |

### 场景 3: 相关与回归

| # | 输入 | 预期方法 | 验证点 |
|---|------|----------|--------|
| 3.1 | `告诉我飞行距离和什么因素有关` | pearson_correlation | 相关系数矩阵 |
| 3.2 | `研究在线值机便利性和满意度的关系` | pearson_correlation | r值、p值 |
| 3.3 | `飞行距离能预测满意度吗` | simple_regression | R²、系数、白话 |

### 场景 4: 非参数检验（验证 Phase B 修复）

| # | 输入 | 预期方法 | 验证点 |
|---|------|----------|--------|
| 4.1 | `比较不同舱位等级的满意度（用非参数检验）` | kruskal_wallis | H值、白话解读 |
| 4.2 | `男性和女性的行李服务评价是否有差异（非参数）` | mann_whitney_u | U值、效应量 |

### 场景 5: 边界条件

| # | 操作 | 预期 |
|---|------|------|
| 5.1 | 不输入文字，点发送 | 400 "Empty input" |
| 5.2 | 输入 2500+ 字的长文本 | 400 "输入文本过长" |
| 5.3 | 分析执行中点击取消 | SPSS 进程终止，状态恢复 |
| 5.4 | 连续快速发送 11 次分析 | 第 11 次返回 429 |

### 场景 6: 多轮对话

| # | 操作 | 预期 |
|---|------|------|
| 6.1 | 先问 `比较男女满意度` → 再问 `那不同舱位等级呢？` | 自动切换分组变量 |
| 6.2 | 先问 `显示满意度的描述统计` → 再问 `看看飞行距离的` | 自动切换分析变量 |

### 场景 7: 导出与设置

| # | 操作 | 预期 |
|---|------|------|
| 7.1 | 分析完成后点击导出 Word | 下载 .docx，内容含统计表格+白话 |
| 7.2 | 修改设置中 LLM Model，保存 | 配置持久化到 .env |
| 7.3 | 关闭重开 → 上传 airline.sav → 数据恢复 | 会话持久化生效 |

### 场景 8: Demo 按钮验证

| # | 操作 | 预期 |
|---|------|------|
| 8.1 | 清空数据 → 点击 Demo 按钮 | 自动加载 test_data.sav，显示变量 |
| 8.2 | Demo 加载后输入 `比较男女成绩` | 正常分析 |

---

## 每次测试记录

| 场景 | 输入 | 是否成功 | 异常信息 | 耗时 |
|------|------|----------|----------|------|
| 1.1 | 飞行距离的平均值和中位数 | | | |
| 2.1 | 比较男性和女性满意度 | | | |
| 3.1 | 飞行距离和什么因素有关 | | | |
| ... | ... | | | |

---

## 预期常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| 红色错误 `'name'` | 已修复 (b7b271d) | 拉取最新代码 |
| 分析超时 | 25K 行数据 SPSS 执行慢 | 等待 ~30s |
| LLM 返回空 | LLM_MOCK 未关闭 | `.env` 中 LLM_MOCK=false |
| 端口占用 | 上次未正常退出 | 任务管理器杀 python 进程 |
