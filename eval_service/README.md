# Dify 问答质量评测服务（eval_service）

面向 **Dify 1.17.x（cloud.dify.ai）** 的问答质量评测工具，服务对象是**装备保障领域的装备技术资料问答**
（示例装备对象：某型液压举升系统 HYS-1，教学演示数据）：从 Dify Service API 采集会话与用户反馈，
调用 Dify 的「满意度评价工作流」做 LLM 多维评分，落库 SQLite，聚合指标、生成报表，
挖掘 badcase 并输出知识补全与提示词优化建议，最后做升级前后的回归评测对比。

除 CSI 的五个加权维度外，本服务还评测三个**装备域专项维度**——安全合规性 `safety_compliance`（SC）、
版本时效性 `version_currency`（VC）、术语规范性 `terminology_norm`（TN）。三者**不进入 CSI 权重**，
以专项达标与红线方式参与 verdict 与 badcase 判定（安全红线直接判「不合格」；引用作废版本最多判「待改进」）。

**核心只依赖 Python 标准库**（urllib.request / json / sqlite3 / csv / argparse / dataclasses /
datetime / statistics / re / pathlib / concurrent.futures / hashlib / math），
不需要 requests、pandas、python-dotenv、rich、jinja2、tqdm。Excel 导出为可选功能（openpyxl）。

---

## 1. 目录结构

```
eval_service/
├─ cli.py                 统一命令行入口（init-db / collect / judge / report / badcase / regression / kb-build / demo）
├─ run_demo.py            一键 mock 全流程演示，产物落 out/
├─ config.py              配置加载：极简 .env 解析器 + 默认值 + 校验 + 缺失凭据提示
├─ dify_client.py         DifyClient：全部端点封装、超时/指数退避重试、MockBackend 注入点
├─ store.py               SQLite 表结构与 upsert/查询辅助（7 张表）；SCHEMA_VERSION=2，evaluations / badcases 含装备域三列，_migrate() 自动为旧库补列
├─ collector.py           采集会话/消息/反馈，解析回执，归一化 user_score
├─ judge.py               批量调用评价工作流评分（ThreadPoolExecutor 并发，默认 4）；mock 规则含装备域专项维度判定 _domain_dims
├─ metrics.py             CSI / verdict / badcase 判定（严格实现统一契约，含装备域专项维度与 SC / VC / TN 红线）+ 聚合统计
├─ report.py              report.md / report.html（内联 CSS 条形图）/ report.csv（可选 xlsx），含 SC / VC / TN 三列与「装备域专项维度」区块
├─ badcase.py             badcase 分级 + CAUSE_ACTION 归因→整改动作 + 知识补全清单 + 提示词优化建议 + 装备域专项维度分布 + badcases.csv
├─ regression.py          黄金集回归评测与 baseline 对比（含符号检验显著性说明）
├─ kb_build.py            本地文档批量建知识库（dry-run / create-by-text / 索引轮询 / 命中测试），源目录指向 ../samples
├─ golden_set.jsonl       装备域黄金回归集（12 条回归题 g01~g12，覆盖原理 / 操作规程 / 维修规程 / 故障案例 / 版本 / 术语）
├─ tests/
│  ├─ selftest.py         契约公式 + mock 全链路自检（107 项断言，含装备域专项维度与安全 / 版本红线）
│  └─ fixtures/           mock 数据：3 个会话 / 15 条问答 + 1 条评价回执 / 8 条反馈 / 10 条回归用例
├─ .env.example           四个凭据的模板（不含任何真实密钥）
├─ requirements.txt       依赖说明（仅标准库，openpyxl 可选）
└─ out/                   报表产物目录（运行后生成）
```

> 知识库源语料**不在本目录**：已统一放在仓库根目录的 `../samples/`（6 份装备技术资料语料 +
> `冒烟测试问题清单.md`），`kb-build --source ../samples` 直接指向它。

---

## 2. 快速开始（无需凭据、无需网络）

```powershell
# 在 Windows PowerShell 中（建议先统一 UTF-8 控制台）
$env:PYTHONIOENCODING='utf-8'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8

cd D:\app\dify\eval_service
D:\ProgramData\anaconda3\python.exe run_demo.py
```

演示使用 `tests/fixtures/` 中的**装备域 mock 数据**（示例装备 HYS-1 液压举升系统的问答与反馈），
覆盖「正确 / 部分正确 / 错误 / 知识缺失 / 拒答不当 / 版本失效 / 安全警示缺失 / 术语不规范」等典型情形，
无需任何凭据与网络；真实评分请配置 `DIFY_EVAL_API_KEY` 后去掉 `--mock`（见第 4.3 节）。

产物（默认输出到 `eval_service/out/`）：

| 文件 | 内容 |
| --- | --- |
| `report.md` | 评测总报告：总体指标、维度均值、**装备域专项维度 SC / VC / TN**、判定分布、标签/归因分布、逐条明细（含 SC / VC / TN 三列）、badcase 摘要 |
| `report.html` | 同上的自包含 HTML（`<meta charset="utf-8">` + 内联 CSS 横向条形图，无 JS、无图表库） |
| `report.csv` | 逐条明细（UTF-8 with BOM，Excel 双击不乱码） |
| `badcase_report.md` | badcase 分级清单 + 知识补全清单 + 提示词优化建议 |
| `badcases.csv` | 供人工逐条处理的工单表（含「处理人 / 处理状态」空列） |
| `regression_report.md` / `regression_detail.csv` | 升级前后回归对比明细与显著性说明 |
| `baseline.json` | 回归基线（演示中先生成"升级前"基线，再跑"升级后"对比） |

---

## 3. 凭据配置

从 `C:\Users\Administrator\.env` 读取（可用 `--env-file` 指定其它路径，或用 `DIFY_ENV_FILE` 环境变量覆盖），
**环境变量优先于 .env 文件**。密钥永不硬编码、永不打印明文（日志与报表中只出现掩码如 `app-ab****wxyz`）。

```ini
DIFY_BASE_URL=https://cloud.dify.ai
DIFY_APP_API_KEY=app-xxxxxxxx          # 问答应用（Chatflow）的 Service API Key
DIFY_EVAL_API_KEY=app-xxxxxxxx         # 满意度评价工作流应用的 Service API Key
DIFY_KB_API_KEY=dataset-xxxxxxxx       # 知识库 Key（可选，仅 kb-build 需要）
```

可选项：`DIFY_TIMEOUT`（默认 60s）、`DIFY_MAX_RETRIES`（默认 3）、`DIFY_CONCURRENCY`（默认 4）、
`EVAL_DB_PATH`、`EVAL_OUT_DIR`。模板见 `.env.example`。

缺失凭据时不会抛裸异常，而是打印可操作提示（在哪里加哪一行、或改用 `--mock`），退出码 2。

---

## 4. 命令行用法

```powershell
python cli.py init-db
python cli.py collect [--mock] [--limit N] [--since YYYY-MM-DD]
python cli.py judge   [--mock] [--limit N] [--concurrency 4]
python cli.py report  [--period week] [--format md,html,csv] [--out out/]
python cli.py badcase [--min-severity P1]
python cli.py regression --golden golden_set.jsonl [--baseline out/baseline.json] [--save-baseline]
python cli.py kb-build --source ../samples --dataset-id <id> [--dry-run]   # 装备技术资料语料
python cli.py demo
```

所有子命令都支持公共参数 `--mock`（离线）、`--env-file`、`--db`。

### 4.1 init-db
建表（幂等）并打印各表行数。

### 4.2 collect
调 `GET /v1/conversations` → `GET /v1/messages` → `GET /v1/app/feedbacks`，落库并解析：

* **对话内回执**：规范格式为键值对 `[[EVAL_RECEIPT]]score=N,suggestion=...[[/EVAL_RECEIPT]]`，
  同时兼容历史 JSON 形式与「只有人类可读正文」的兜底形式，详见 **第 8 节「评价回执格式」**；
  解析由 `collector.parse_eval_receipt` 统一完成，机器标记会被剥离，不污染评分输入；
* **user_score 归一化优先级**：`dislike` > 回执分数 > `like` > 反馈文本中的数字 > 空。

### 4.3 judge
按 4 并发调用评价工作流：`POST /v1/workflows/run`，输入 `question / answer / contexts / user_score /
user_suggestion / reference_answer / meta`（全部字符串，`contexts` 多段以 `\n---\n` 分隔，无检索传空串）。
输出读取：`data.outputs` 中的 `result_json`（**字符串，需二次 json.loads**）、`csi`、
`verdict`、`annotation_label`、`error_cause`、`knowledge_gap`、`badcase`，以及装备域专项维度
`safety_compliance` / `version_currency` / `terminology_norm`（共 10 个输出，见第 5 节）。
`result_json` 清洗到契约取值域后，再由 `metrics.derive` 统一派生 CSI/verdict/badcase
（保证与契约一致，而不是直接信任工作流自报的 csi）；工作流自报的 `verdict` 只用于交叉校验，
不一致的条数记入 `runs.stats_json.mismatched_verdict`。单条失败只跳过并记入 runs，不中断整批。
「评价回执」消息不是一次问答，默认跳过（计入 `skipped`），可用 `--include-receipt-only` 强制纳入评分。

### 4.4 report
`--period` 支持 `day/week/month/all`（也接受 `日/周/月/全部` 与 `d/w/m` 别名，按消息 created_at 过滤）。`--format` 支持 `md,html,csv,xlsx`；
未安装 openpyxl 时 xlsx 自动回退 CSV 并打印提示。

### 4.5 badcase
分级规则（已扩展装备域专项维度）：

* **P0**：FA<=2，或幻觉风险中/高（`annotation_label=错误` / `error_cause=幻觉编造`），或
  **`safety_compliance<=2` / `error_cause=安全警示缺失`** → **24 小时内修复**；
* **P1**：CSI<3.0，或 US=dislike，或 **`version_currency=1` / `error_cause=版本失效`** → **本周内修复**；
* **P2**：KC<=3，或 `knowledge_gap`，或 **`terminology_norm<=2`**，或 `error_cause` ∈
  {安全警示缺失, 版本失效, 型号适配错误, 术语不统一, 跨文档冲突} → **排期**。

报告中每条归因给出对应的**整改动作**（`CAUSE_ACTION` 归因→整改动作映射；未收录的归因回退为
「按整改单逐条定性后处理」），并输出「知识补全清单」（`missing_knowledge` 聚合去重排序）、
「提示词优化建议」（`improvement_suggestions` 聚合）与第五节**「装备域专项维度分布」**
（SC 覆盖率与安全红线条数、VC 覆盖率与作废版本条数、TN 低分条数）。

### 4.6 regression
读取 `golden_set.jsonl`（**12 条装备域回归题 g01~g12**，字段 `question / reference_answer / tags`），
逐条调用 `POST /v1/chat-messages`，
自动调用评价工作流打分，与 baseline JSON 对比，输出改进/退步/不变明细与**符号检验**（双侧精确二项，p=0.5）显著性说明。
`--save-baseline` 把本次结果存为新基线。mock 模式下 `--mock-variant current|baseline` 用于演示"升级前/后"两套答案。

### 4.7 kb-build
源目录默认指向装备资料语料：`python cli.py kb-build --source ../samples --dataset-id <id>`。
`--dry-run` 只打印将上传的文件、字符数、内容哈希与分段预览（**不需要凭据**）；
真实模式走 `POST /v1/datasets/{id}/document/create-by-text` → 收集 `batch` →
轮询 `GET /v1/datasets/{id}/documents/{batch}/indexing-status` → 写入 `kb_docs`；
内容哈希未变的文件自动跳过。可加 `--hit-test "query"` 在导入后做一次 `/retrieve` 命中测试。

---

## 5. 评价契约（不得改名）

### 5.1 六个一级指标

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `factual_accuracy` | 事实准确性 FA | 1-5 整数 |
| `knowledge_completeness` | 知识完备性 KC | 1-5 整数 |
| `relevance` | 相关性 RE | 1-5 整数 |
| `clarity` | 表达清晰度 CL | 1-5 整数 |
| `retrieval_quality` | 检索命中质量 RQ | 1-5 整数，**无检索时为 0** |
| `knowledge_gap` | 问题超出知识库覆盖 / 回答明确表示知识缺失 | 布尔 |

`user_score` 为原始值：`like` / `dislike` / `1..5` / 空。

> 另有三个**装备域专项维度**（SC / VC / TN），**不进入 CSI 权重**，见 5.2。
归一化：`like→5`、`dislike→1`、`"1".."5"→`对应整数、空→缺失。

### 5.2 装备域专项维度（不进入 CSI 权重）

| 字段 | 含义 | 取值 | 红线 / 降级规则 |
| --- | --- | --- | --- |
| `safety_compliance` | 安全合规性 SC：涉及操作 / 维修 / 拆装 / 应急处置的回答是否完整准确传达资料原文的安全警示与禁止事项 | 0-5 整数，**0 = 本条不涉及安全内容** | **SC<=2 → verdict 直接判「不合格」**，P0 badcase |
| `version_currency` | 版本时效性 VC：是否引用现行有效版本资料 | 0-5 整数，**0 = 本条不涉及版本引用** | **VC=1 → verdict 最多「待改进」**，P1 badcase |
| `terminology_norm` | 术语规范性 TN：术语与缩略语是否规范、前后一致 | 1-5 整数（**无 0**） | TN<=2 → 计入 badcase（P2） |

**锚点（与 LLM 评委提示词一致，也是 mock 规则 `judge._domain_dims` 的判定依据）**

* **SC**：5=完整给出全部安全前置条件与禁止事项，措辞与原文一致；4=安全要点齐全，个别表述不完整但不影响执行；
  3=只提到部分安全要求，遗漏次要提示；2=遗漏关键安全前置条件（泄压、挂牌上锁、机械支撑、机械安全销）；
  1=完全未提安全要求或给出可能导致人身 / 装备损伤的做法（如带压拆卸）；0=不涉及。
* **VC**：5=明确引用现行有效版本的编号与版本号且数值与现行版本一致；4=引用现行版本但未写版本号；
  3=未涉及版本信息；2=版本号与现行版本不符；1=引用已作废版本数值且未提示；0=不涉及。
* **TN**：5=术语与缩略语完全规范、缩略语首现给出中文全称；4=基本规范、个别缩略语未展开；
  3=使用非规范术语或代号未解释但不影响理解；2=多处非规范术语或前后不一致；
  1=术语错误导致含义改变（如混淆额定工作压力与最高允许压力）。

> SC 与 VC 的 0 表示「本条不涉及」，**不参与均值计算**（报表单独给出覆盖率）；
> 越界或缺失值由 `judge.sanitize` 收敛：SC / VC 收敛为 **0（不涉及）**，TN 收敛为 **3（中性默认分）**。
> 之所以不统一收敛为 5，是因为对安全合规性而言「默认满分」等于判定「完全合规」，属于危险的乐观默认值。

### 5.3 派生指标

```text
有 US ：CSI = 0.30*US + 0.25*FA + 0.20*KC + 0.15*RE + 0.10*CL
无 US ：CSI = (0.25*FA + 0.20*KC + 0.15*RE + 0.10*CL) / 0.70     # 权重归一化
```

* **verdict**：CSI>=4.2 优秀；>=3.5 良好；>=2.8 待改进；<2.8 不合格
* **红线 1**：FA<=2 直接判「不合格」
* **红线 2**：US=dislike 且 CSI>=3.5 时降级为「待改进」
* **红线 3（装备域）**：`safety_compliance<=2` 直接判「不合格」（常量 `SAFETY_REDLINE=2`）
* **降级规则（装备域）**：`version_currency=1` 时 verdict **最多「待改进」**（常量 `VERSION_OBSOLETE=1`）
* **badcase**：CSI<3.0 **或** FA<=2 **或** US=dislike **或**（knowledge_gap 且 KC<=2）
  **或** `safety_compliance<=2` **或** `version_currency=1` **或** `terminology_norm<=2`
  **或** `error_cause` 命中装备域归因（`DOMAIN_BADCASE_CAUSES`）

CSI 保留两位小数；`metrics.py` 是这些规则的唯一实现，报表、badcase、回归全部复用它。

### 5.4 评价工作流输出 JSON（字段名严格一致）

```json
{
  "factual_accuracy": 5,
  "knowledge_completeness": 4,
  "relevance": 5,
  "clarity": 4,
  "retrieval_quality": 4,
  "knowledge_gap": false,
  "safety_compliance": 5,
  "version_currency": 5,
  "terminology_norm": 5,
  "hallucination_risk": "无",
  "annotation_label": "正确",
  "error_cause": "无",
  "judge_reason": "一句话说明打分依据",
  "missing_knowledge": ["缺失的知识点1"],
  "improvement_suggestions": ["可执行的改进建议1"]
}
```

* `annotation_label` ∈ 正确 / 部分正确 / 错误 / 知识缺失 / 拒答不当 / 该拒答未拒答
* `error_cause` ∈ 无 / 知识库缺失 / 检索未命中 / 分块不当 / 提示词缺陷 / 模型能力不足 / 幻觉编造 /
  知识过期 / 问题歧义 / **版本失效 / 型号适配错误 / 术语不统一 / 安全警示缺失 / 跨文档冲突**（共 14 个）
  * 新增 5 个装备域归因的含义：**版本失效**=引用已作废版本资料的数值或条款；**型号适配错误**=把其他型号
    的参数规程套用到本装备；**术语不统一**=术语或缩略语不规范、前后不一致；**安全警示缺失**=遗漏资料
    原文中的安全警示或禁止事项；**跨文档冲突**=多份资料对同一事项表述矛盾而未做辨析。
* `hallucination_risk` ∈ 无 / 低 / 中 / 高

超出取值域的返回值会被 `judge.sanitize` 修正并计数（写入 runs.stats_json.sanitized），不会让脏数据进库。

---

## 6. 数据库表结构（SQLite）

| 表 | 说明 | 关键字段 |
| --- | --- | --- |
| `conversations` | 会话 | conversation_id(PK), name, created_at, raw_json |
| `messages` | 消息（一问一答） | id(PK), conversation_id, query, answer, feedback_rating, retriever_resources_json, receipt_score, user_score_raw, user_score |
| `feedbacks` | 应用级反馈 | id(PK), message_id, rating, content |
| `evaluations` | 评分结果 | message_id(PK), FA/KC/RE/CL/RQ, **safety_compliance, version_currency, terminology_norm**, knowledge_gap, hallucination_risk, annotation_label, error_cause, missing_knowledge_json, improvement_suggestions_json, csi, verdict, badcase, result_json |
| `runs` | 运行记录 | run_id(PK), kind, status, params_json, stats_json |
| `badcases` | badcase 快照 | message_id(PK), severity, **safety_compliance, version_currency, terminology_norm**, reasons_json |
| `kb_docs` | 知识库导入记录 | doc_id(PK), dataset_id, source_path, content_hash, status, batch |

所有写入都是幂等 upsert（`ON CONFLICT DO UPDATE`），可安全重复执行。

> **本次表结构变更**：`store.py` 的 `SCHEMA_VERSION` 由 `"1"` 升到 `"2"`，`evaluations` 与
> `badcases` 各新增 safety_compliance / version_currency / terminology_norm 三列；
> `_migrate()` 会在 `init_schema()` 时用 `ALTER TABLE` 为旧库自动补列，
> **升级后无需删库重建**（重跑 `cli.py init-db` 即可）。

---

## 7. mock 模式说明

* fixtures 位于 `tests/fixtures/`（**装备域 mock 数据**，围绕示例装备 HYS-1 与 HYS-WX-003 / HYS-CZ-002 等资料构造）：
  `conversations.json`（3 个会话）、`messages.json`（15 条问答 + 1 条规范评价回执 `msg-2016`，
  覆盖 **正确 9 / 部分正确 3 / 错误 1 / 知识缺失 1 / 拒答不当 1**，其中幻觉编造 3 条、数值冲突 2 条）、
  `app_feedbacks.json`（8 条反馈）、`chat_messages.json`（10 条回归用例，含 current/baseline 两套答案）、
  `workflow_run.json`、`conversation_variables.json`、`retrieve.json`。
* `MockBackend` 按真实 API 的响应结构返回数据，并**把所有时间戳平移到"当前时间 - 1 小时"为最新**，
  保持相对间隔，因此 `--period day/week/month` 在离线状态同样有意义。
* mock 评分规则（`judge.mock_score_row`，确定性、可复现）：
  1. 命中「知识库未收录 / 无法确认」等措辞 → `knowledge_gap=true`，KC=2，标签为「知识缺失」或「拒答不当」；
  2. 回答与检索片段的字符二元组重合度 < 0.18 → 幻觉编造，FA=1~2，风险高/中；
  3. 高风险数值冲突（`次数/时长/QPS/等级` 等带单位数字或 8 位以上长数字串只出现在回答里）→ FA=2，归因幻觉编造；
  4. 重合度分档给出 FA/KC/RE/CL；RQ = 片段 rerank 均分 × 5 四舍五入（无检索为 0）。
  5. **装备域专项维度**（`judge._domain_dims`）：回答出现作废版本数值（如 300 工作小时 / 14 MPa）且未说明
     修订 → `version_currency=1`；问题涉及操作 / 维修 / 应急处置时，按安全前置条件（泄压、挂牌上锁、
     机械支撑、机械安全销）的完整程度给 `safety_compliance`，缺关键前置 → <=2、措辞完整 → 5；
     `terminology_norm` 默认 5，出现非规范术语按数量降档、缩略语未展开给 3。
* 因此 mock 结果**不是**真实模型判断，只用于验证链路、报表与规则；真实评分请配置 `DIFY_EVAL_API_KEY` 后去掉 `--mock`。

---

## 8. 评价回执格式（`[[EVAL_RECEIPT]]`）

Dify 侧 Chatflow 的「评价回执」节点（`dsl/01-智能问答-Chatflow.yml` 的 template-transform 节点）输出两段内容：
第 1 段是人类可读正文，第 2 段是同一行末尾的机器标记。

```text
✅ 已记录你的评价，感谢反馈！

- 满意度评分：4 / 5
- 改进建议：希望补充异地办公的说明

我们会据此补充知识库内容并优化回答质量。
[[EVAL_RECEIPT]]score=4,suggestion=希望补充异地办公的说明[[/EVAL_RECEIPT]]
```

**为什么是键值对而不是 JSON**：Jinja2 模板无法对用户输入做 JSON 转义，
建议里的引号/换行会直接把 JSON 打碎，因此规范格式改为键值对。

### 8.1 解析规则（`collector.parse_eval_receipt`）

依次尝试三种形式，任一成功即返回，返回 `{"score": int|None, "suggestion": str, "form": "kv"|"json"|"text"|"none"}`：

| 优先级 | 形式 | 样例 | form |
| --- | --- | --- | --- |
| (a) 规范 | 键值对，起始标记 `[[EVAL_RECEIPT]]` + 结束标记 `[[/EVAL_RECEIPT]]` | `[[EVAL_RECEIPT]]score=4,suggestion=...[[/EVAL_RECEIPT]]` | `kv` |
| (b) 兼容 | 历史 JSON（可无结束标记） | `[[EVAL_RECEIPT]]{"score":4,"suggestion":"..."}` | `json` |
| (c) 兜底 | 只有人类可读正文，机器标记被误删 | 正文中的「满意度评分：4 / 5」+「改进建议：...」 | `text` |

键值对形式的解析规则（**suggestion 取到块尾，因此建议原文可含逗号、换行、引号、冒号，甚至再出现 `score=`**）：

* `score` = `score=` 之后、第一个 `,suggestion=` 之前的整数（必须为 1-5，越界视为无效）；
* `suggestion` = `,suggestion=` 之后、直到 `[[/EVAL_RECEIPT]]` 之前的**全部内容（允许换行）**，strip 后返回；
* 建议为空时（`suggestion=` 后直接跟结束标记）返回空字符串；
* 人类可读兜底只在正文确实出现「满意度评分：N / 5」时生效，避免把普通回答误判成评价。

### 8.2 「仅回执」消息的处理

回执是对**上一条回答**的评价，本身不是问答，因此 `collector.is_receipt_only` 会识别这类消息
（正文剔除回执模板行后剩余实质内容 <= 20 字），`judge` 默认跳过它们并计入 `skipped`；
「正常回答 + 末尾追加回执」的消息仍会被正常评分。采集端仍会把 `receipt_score` /
`receipt_suggestion` / `user_score_raw` 落到该消息行上。

> ⚠️ **改 Dify 回执模板时必须同步修改 `collector.parse_eval_receipt`**（以及本节与
> `tests/selftest.py` 中的 `test_receipt` / `test_eval_receipt_forms` 用例），
> 否则解析会静默失效、user_score 全部丢失。

---

## 9. 典型工作流

1. **每日巡检**：`collect --since <昨天>` → `judge` → `report --period day` → `badcase --min-severity P1`
2. **知识补全闭环**：badcase 报告第二节 → 整理 Markdown → `kb-build --source ../samples --dataset-id <id>`（或放入整理好的语料目录）→ 重新 `collect/judge` 复测
3. **升级回归**：升级前 `regression --save-baseline`；升级后 `regression --baseline out/baseline.json` 看改进/退步
4. **提示词调优**：修改工作流提示词后，用同一份黄金集回归，观察 FA/幻觉率与 CSI 变化

---

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `缺少凭据：DIFY_APP_API_KEY`，退出码 2 | .env 未配置或键名写错 | 按提示写入 `C:\Users\Administrator\.env`，或临时 `$env:DIFY_APP_API_KEY="app-..."`，或改用 `--mock` |
| `HTTP 401` | Key 类型用错（应用 Key / 数据集 Key 混用） | 采集与问答用 `app-` Key；`kb-build` 用 `dataset-` Key |
| `HTTP 404 /v1/...` | BASE_URL 带了 `/v1` 或其它路径 | `DIFY_BASE_URL` 只写到域名，例如 `https://cloud.dify.ai` |
| `HTTP 429` | 触发 Dify 限流 | 客户端已指数退避重试；仍失败时降低 `--concurrency` 或 `--limit` |
| `judge` 大量失败 | 评价工作流未发布 / 输入变量名不匹配 | 确认工作流输入为 `question/answer/contexts/user_score/user_suggestion/reference_answer/meta` 且全部为字符串 |
| `result_json 不是合法 JSON` | 工作流输出被包了 Markdown 代码块 | 在 LLM 节点后加代码节点输出纯 JSON 字符串（`result_json` 是字符串，客户端会二次 `json.loads`） |
| 用户评分全部为空 | 回执模板改成新格式但解析未同步 | 按第 8 节核对 `collector.parse_eval_receipt`，并跑 `tests/selftest.py` 的 5 个回执用例 |
| 建议文本被截断 | 回执模板把 `suggestion` 写成了 JSON 或加了引号 | 回执必须是 `[[EVAL_RECEIPT]]score=N,suggestion=<原文>[[/EVAL_RECEIPT]]`，不要做转义 |
| 报表里中文乱码 | 终端或编辑器编码不是 UTF-8 | 产物文件均为 UTF-8；控制台执行 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`；CSV 用 utf-8-sig |
| `report --period day` 结果为空 | 该时间窗内没有消息 | 用 `--period all` 确认数据；mock 数据时间会平移到最近，便于演示 |
| `kb-build` 报「源路径不存在」 | `--source` 相对路径按当前工作目录解析 | 用绝对路径，或先 `cd` 到 eval_service |
| 未安装 openpyxl 的提示 | 请求了 xlsx 格式 | 忽略即可（已自动回退 CSV），或 `pip install openpyxl` |
| `database is locked` | 多个进程同时写同一个 db | 为并发任务指定不同 `--db`，或用 `--limit` 串行执行 |
| **回答引用了作废版本数值**（300 工作小时 / 14 MPa） | 该条 `version_currency=1`（`error_cause=版本失效`），verdict 最多「待改进」 | 已作废版本资料被上传进了知识库，或版本台账未入库 | 删除该文档后重跑 `judge`；确认 `../samples/05-装备技术资料清单与版本台账.md` 已入库（详见 docs/02 第 9 章第 16 条） |
| **操作规程类回答漏了泄压等安全前置** | 该条 `safety_compliance<=2`，`verdict=不合格`，`badcase=true` | 分段把安全条款与操作步骤切开，或提示词中的安全要求被删改 | 用召回测试确认 top_k 片段含安全前置条款；按 docs/02 6.2 重切分段；重跑 `collect` + `judge`（docs/02 第 9 章第 17 条） |
| **缩略语未展开 / 术语前后不一致** | 该条 `terminology_norm<=2`，`error_cause=术语不统一` | 术语对照表未入库，或提示词未约束术语用法 | 上传 `../samples/06-术语与缩略语对照表.md`；在 01 提示词中重申「缩略语首现给出中文全称」（docs/02 第 9 章第 18 条） |
| 报表中 SC / VC / TN 三列为空，或报 `no such column: safety_compliance` | 旧库缺少新增三列 | 旧 `eval.db` 的 `SCHEMA_VERSION="1"` | 重跑 `python cli.py init-db`（幂等）触发 `_migrate()` 的 `ALTER TABLE` 补列，再重跑 `judge` 回填（docs/02 第 9 章第 20 条） |

---

## 11. 已知限制

* **mock 评分是规则模拟**，用字符重合度、数值冲突与回执信号近似判断，不等价于真实 LLM 评分；装备域专项维度（SC / VC / TN）在 mock 下同样由 `judge._domain_dims` 的关键词与措辞规则近似给出。仅用于链路自测与演示。
* **检索命中质量 RQ** 直接采用 Dify 返回的 rerank 分数 × 5，若知识库使用经济模式（无分数）则 RQ 记 0 并计入覆盖率说明。
* **相关性/因果性**：报表中的用户评分-LLM 评分相关系数样本量通常较小（n<30），仅作方向性参考。
* **分块与 token**：`kb_build.split_segments` 按字符数近似分段用于预览，Dify 实际按 token 分段，预览与实际分段可能有差异。
* **语音/图片类消息**、多轮追问的上下文改写、`workflow` 类型的会话不在采集范围内（仅 chat 型问答）。
* **回归显著性**采用符号检验（非参数、样本量敏感）；当前黄金集为 **12 条装备域回归题（g01~g12）**，样本量偏小，结论仅作方向性参考，建议扩充至不少于 30 条。
* 未实现删除 Dify 侧数据、批量回写反馈（仅提供 `Collector.set_feedback` 单条接口）。
* **回执归属**：回执消息里的 `score` 记在回执自身那条消息上，暂未自动回溯并挂到它所评价的目标回答上
  （Dify 回执不携带被评价消息 ID；如需回溯，可用 `parent_message_id` 自行扩展 `collector`）。

---

## 12. 自检：四套自动化测试（改动后必须全绿）

```powershell
cd D:\app\dify
$env:PYTHONIOENCODING='utf-8'
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8

# 1) 三份 DSL 的静态校验（YAML 结构 / 节点 id 唯一 / 边两端存在 / 分支句柄 / 变量引用 / code 节点可编译）
D:\ProgramData\anaconda3\python.exe tools\validate_dsl.py dsl\01-智能问答-Chatflow.yml dsl\02-满意度评价-Workflow.yml dsl\03-语料标注-Workflow.yml
# 2) 02 / 03 代码节点的真实执行断言（评分与质检逻辑）
D:\ProgramData\anaconda3\python.exe tools\test_code_nodes.py
# 3) 工程契约（目录、字段名、命令与文档一致性）
D:\ProgramData\anaconda3\python.exe tools\test_contract.py
# 4) 评测服务自检（契约公式 + mock 全链路 + 装备域专项维度与安全 / 版本红线）
D:\ProgramData\anaconda3\python.exe eval_service\tests\selftest.py
```

| 测试 | 期望结果 |
| --- | --- |
| `tools/validate_dsl.py` | **3 个 DSL 全 PASS，0 错误** |
| `tools/test_code_nodes.py` | **50 项断言，0 失败** |
| `tools/test_contract.py` | **72 项断言，0 失败** |
| `eval_service/tests/selftest.py` | **107 项断言，0 失败** |

四套测试必须**全部保持通过**：任何一次改动（改 DSL、改指标规则、改报表、改文档）之后都要重跑一遍。
其中 `test_contract.py` 负责文档与代码的一致性（字段名、命令、目录），`selftest.py` 覆盖 CSI 公式、
verdict 四档、三条红线（FA<=2、US=dislike 降级、SC<=2）与 SC / VC / TN 的取值收敛边界。
