# 装备技术资料智能问答与评价闭环迭代系统（Dify 版）

针对装备系统技术日益复杂、资料繁多，原理及规程等分散于各类手册、技术文件中，用户学习和查询不便的现实问题，本课题在 **Dify** 上构建一套完整的「装备技术资料知识库问答 + 多维度满意度评价 + 语料标注 + 闭环迭代」体系：

- 用户的**满意度评分**与**改进建议**被自动采集；
- 每条回答由大模型评委按**六个维度**打分，输出**综合满意度指数 CSI**，并按**装备域专项维度**（安全合规性 SC、版本时效性 VC、术语规范性 TN）判定红线，给出**标注标签 / 错误归因**；
- 评价结果自动聚合成报表，挖出 **badcase**、**知识完备性缺口**、**作废版本残留**与**提示词优化建议**；
- 通过**黄金测试集回归**（12 条装备域题）验证每一轮升级，未达标不允许发布。

> 装备对象：某型液压举升系统（代号 HYS-1，示例数据，文中数值均为教学演示值）。
> 总体技术方案（课题背景、指标体系、装备域适配设计）见 [`docs/00-装备技术资料智能问答与评价系统技术方案.md`](docs/00-装备技术资料智能问答与评价系统技术方案.md)。

> ⚠️ **开工先读（防跑偏）**：[`AGENTS.md`](AGENTS.md)（给 AI 会话的硬规则）与
> [`docs/16-项目基线、口径与变更纪律.md`](docs/16-项目基线、口径与变更纪律.md)（口径唯一来源、冻结口径、系统地图、变更纪律、已知缺陷与绕行）。
> **改任何东西前后都先跑门禁**：`powershell -ExecutionPolicy Bypass -File tools\run_all_checks.ps1`（**7 个套件**必须全部 exit=0，含漂移检查 `tools\check_drift.py`）。

---

## 一、交付物一览

```
D:\app\dify\
├─ AGENTS.md                            ★ 给 AI 会话/Agent 的硬规则（开工先读）
├─ 启动演示系统.bat                      ★ 一键启动演示（检索契约层 + 治理看板，仅标准库）
├─ dsl\                                 ← 在 Dify 里导入的三个应用
│   ├─ 01-多模态智能问答-Chatflow.yml       智能问答助手（advanced-chat：多模态意图分流 + 知识检索 + 满意度采集回执）
│   ├─ 02-满意度评价-Workflow.yml         多维度评分与指标计算（workflow：LLM 评委 + SSI / OSI / CSI + SC / VC / TN 红线）
│   ├─ 03-多模态语料标注-Workflow.yml      语料 → 问答对抽取与自动质检标注（workflow：抽帧 QC + 去重 + 复核标记 + 作废值扫描）
│   └─ _v1-原始备份\                      初版 DSL 存档（禁止再被代码/文档引用）
├─ docs\
│   ├─ 00-装备技术资料智能问答与评价系统技术方案.md   ★ 总体技术方案（课题背景、指标体系、装备域适配设计）
│   ├─ 01-满意度评价指标体系.md            ★ 指标体系设计（评分锚点、装备域专项维度、标签体系、验收标准）
│   ├─ 02-系统架构与部署指南.md            ★ 从零部署：导入 DSL、配密钥、排错
│   ├─ 03-闭环迭代SOP.md                   ★ 采集→评价→归因→整改→回归 的标准作业程序
│   ├─ 04-Web界面方案对比与选型.md          ★ Web 界面三条路径对比、内网部署关键设计与推荐路线
│   ├─ 05~09（技术难点 / 多模数据技术方案 / 技术协议 / 国产化适配 / RAGFlow 融合框架）
│   ├─ 10-以文档质量为中心的治理度量框架.md   ★ 归因前置、记分卡 DQI、整改单、闭环率
│   ├─ 11-技术方案设计报告.md               ★ 验收基线（EVAL-DESIGN-011，F/T/P/S/Q 指标表）
│   ├─ 12-技术方案设计报告（基于RAG…）.md     另一客户口径（鲲鹏+昇腾/扫描件 OCR/知识图谱），勿混用
│   ├─ 13-Windows演示系统说明.md / 14-门户前端与系统设置说明.md / 15-RAGFlow与Dify链路自检与修复记录.md
│   └─ 16-项目基线、口径与变更纪律.md        ★ 防跑偏总纲（口径唯一来源 + 变更纪律 + 状态快照）
├─ eval_service\                         Python 评测服务（仅标准库，Windows 可直接跑）
│   ├─ cli.py                            统一命令行入口（demo / collect / judge / report / badcase / attribute / scorecard / dashboard / regression / kb-build）
│   ├─ collector.py                      从 Dify 拉取会话/消息/反馈，解析评价回执
│   ├─ judge.py                          调用评价工作流做多维评分（含 mock 模式）
│   ├─ metrics.py                        CSI、verdict、装备域专项维度、badcase 分级、聚合统计
│   ├─ badcase.py                        badcase 分级 + 归因→整改映射（CAUSE_ACTION）+ 知识补全清单
│   ├─ corpus.py                         语料索引：文档头 / 条款清单 / 台账现行版本（治理度量用）
│   ├─ attribution.py                    归因前置（R1~R9）：判定缺陷归资料还是系统
│   ├─ doc_score.py                      文档质量记分卡：DQI + Wilson 区间 + 缺陷整改单
│   ├─ dashboard.py                      ★ 文档质量治理看板（Windows 本地版，仅标准库）
│   ├─ asset_index.py                     ★ 资产契约与资产索引（图片/视频登记与元数据检索）
│   ├─ db_query.py                        ★ 数据库交互查询（台账/履历/检修记录，只读+白名单模板）
│   ├─ report.py                         Markdown / HTML / CSV 报表
│   ├─ regression.py                     黄金集回归评测与基线对比
│   ├─ kb_build.py                       知识库批量构建与标注入库
│   ├─ store.py                           SQLite 八张表与旧库迁移（SCHEMA_VERSION = 3）
│   ├─ config.py / dify_client.py        配置读取与 Dify API 客户端
│   ├─ run_demo.py / README.md           离线演示脚本与评测服务说明
│   ├─ golden_set.jsonl                  黄金集：12 条装备域回归题（g01~g12）
│   └─ tests\                            自测（selftest.py）与 fixtures\ mock 数据（无凭据也能跑通全流程）
├─ tools\
│   ├─ run_all_checks.ps1                ★ 一键自检门禁（7 个套件全绿才放行）
│   ├─ check_drift.py                    ★ 漂移检查（文档/代码/线上草稿一致性）
│   ├─ validate_dsl.py                   DSL 静态校验（结构/连线/变量/代码语法）
│   ├─ test_code_nodes.py                DSL 内评分逻辑的 50 项行为断言
│   ├─ test_contract.py                  ★ DSL ↔ Python 跨文件契约测试（72 项断言）
│   ├─ check_corpus.py                   ★ 语料与版本治理校验（编号/版本一致性、作废值隔离、参数一致性、术语规范）
│   ├─ make_demo_assets.py                ★ 生成演示资产（图片/视频）+ 缩略图 + 资产契约清单
│   ├─ make_demo_business_db.py           ★ 生成演示业务库（装备台账/履历/检修记录）
│   └─ check_env.py                       凭据与连通性自检、知识库 ID 查询、配置清单
├─ samples\                              6 份装备技术资料语料 + 冒烟测试问题清单
│   ├─ 01-某型液压举升系统工作原理.md       HYS-GY-001 V3.0（原理）
│   ├─ 02-某型液压举升系统操作规程.md       HYS-CZ-002 V4.0（操作规程）
│   ├─ 03-某型液压举升系统维修规程.md       HYS-WX-003 V2.0（维修规程）
│   ├─ 04-某型液压举升系统典型故障案例集.md  HYS-GZ-004 V1.0（故障案例）
│   ├─ 05-装备技术资料清单与版本台账.md      HYS-ZL-005 V1.0（资料管理）
│   ├─ 06-术语与缩略语对照表.md             HYS-TY-005 V2.0（术语）
│   └─ 冒烟测试问题清单.md                  上线前的冒烟问题清单
├─ kb_bridge\                            ★ 检索契约层（对应 11 号报告 3.5）
│   ├─ chunk_index.py                    条款级索引：切分 / TF-IDF 检索 / 版本硬过滤 / 引用契约
│   ├─ bridge_server.py                  POST /retrieval（Dify 外部知识库协议兼容）+ 演示接口
│   └─ selftest.py                       18 项自测（协议 / 统一片段 / 引用契约 / 版本过滤）
├─ assets\                               演示资产（images / videos / thumbs / assets.json）
├─ demo_corpus\                          演示用作废版本语料（验证版本硬过滤，不参与治理门禁）
├─ demo_data\business.db                 演示业务库（装备台账 / 履历 / 检修记录）
└─ _reference\                           Dify 官方 DSL/节点 schema 基线（改 DSL 时查证用）
```

---

## 二、指标体系速览

| 层级 | 指标 | 键名 | 取值 | 权重 |
| --- | --- | --- | --- | --- |
| 用户感知 | 用户满意度 | `user_score` | 1-5（👍=5 / 👎=1） | 0.30 |
| 回答质量 | 事实准确性 | `factual_accuracy` | 1-5 | 0.25 |
| 回答质量 | 知识完备性 | `knowledge_completeness` | 1-5 | 0.20 |
| 回答质量 | 相关性 | `relevance` | 1-5 | 0.15 |
| 回答质量 | 表达清晰度 | `clarity` | 1-5 | 0.10 |
| 检索 | 检索命中质量 | `retrieval_quality` | 0-5 | 参考 |
| 检索 | 知识覆盖缺口 | `knowledge_gap` | 布尔 | 参考 |

**装备域专项维度**（不进入 CSI 权重，以专项达标与红线方式参与判定）：

| 专项维度 | 键名 | 取值 | 红线 / 降级规则 |
| --- | --- | --- | --- |
| 安全合规性 | `safety_compliance` | 0-5 整数（0 = 本条不涉及安全内容） | ≤ 2 → verdict 直接判「不合格」，badcase 归 P0，**24 小时内修复** |
| 版本时效性 | `version_currency` | 0-5 整数（0 = 本条不涉及版本引用） | = 1（引用已作废版本数值且未提示）→ verdict 最多判「待改进」，badcase 归 P1，本周内处理 |
| 术语规范性 | `terminology_norm` | 1-5 整数（无 0） | ≤ 2 → 计入 badcase（P2），随双周批次统一术语 |

三个专项维度在 `evaluations` 与 `badcases` 表中各占一列（SCHEMA_VERSION = "2"，旧库由 `_migrate()` 用 ALTER TABLE 补列）。badcase 分级为 P0 / P1 / P2，错误归因共 14 项：无、知识库缺失、检索未命中、分块不当、提示词缺陷、模型能力不足、幻觉编造、知识过期、版本失效、问题歧义、型号适配错误、术语不统一、安全警示缺失、跨文档冲突。

**综合满意度指数**：

```
有用户评分时：CSI = 0.30×US + 0.25×FA + 0.20×KC + 0.15×RE + 0.10×CL
无用户评分时：CSI = (0.25×FA + 0.20×KC + 0.15×RE + 0.10×CL) / 0.70
红线：FA ≤ 2 或 safety_compliance ≤ 2 → 直接判「不合格」；用户点踩（dislike）且 CSI ≥ 3.5 → 降级为「待改进」；version_currency = 1 → 最多判「待改进」
```

| CSI | 判定 |
| --- | --- |
| ≥ 4.2 | 优秀 |
| 3.5 ~ 4.2 | 良好 |
| 2.8 ~ 3.5 | 待改进 |
| < 2.8 | 不合格 |

完整定义（含每一档的评分锚点、正反例、标注标签体系、验收阈值、统计方法）见 `docs/01-满意度评价指标体系.md`；装备域专项维度的判定锚点、badcase 分级与归因去向见 `docs/03-闭环迭代SOP.md`。

---

## 三、数据流

```
学员 / 使用人员在 Dify 前端提问
      │
      ├─ 是资料问答 ──→ 装备技术资料知识库检索 → 有召回？ ──是──→ LLM 依据片段作答（数值原样引用，并标注资料编号与版本号）
      │                                          └──否──→ 兜底回答（明示资料未覆盖，不猜测）
      │
      └─ 是评价反馈 ──→ 参数提取器（抽 1-5 分 + 建议原文）──→ 评价回执
                                                              │
   用户在回答下方点 👍 / 👎 ─────────────────────────────────┤
                                                              ▼
                                            Dify 原生反馈 API + 会话消息
                                                              │
                        eval_service collect ─────────────────┘
                                    │
                                    ▼
                   eval_service judge ──→ 调用「满意度评价工作流」
                                    │         （六维打分 + CSI + 装备域专项维度 SC / VC / TN + 标签 + 归因）
                                    ▼
        版本校验（version_currency：是否引用现行有效版本，作废版本数值不得进入回答）
        安全红线（safety_compliance ≤ 2 → 直接判不合格，24 小时内修复）
                                    │
                                    ▼
                  SQLite 落库 → 指标聚合 → 报表 / badcase / 知识补全清单
                                    │
                                    ▼
      整改（补知识 / 调检索 / 改分块 / 改提示词 / 换模型 / 清作废版本 / 补安全条款）
                                    │
                                    ▼
              黄金集回归（g01~g12）→ 达标才允许发布 → 新数据回流
```

---

## 四、快速开始

### 第 1 步：在 Dify 里准备好知识库与模型

1. 工作区安装并授权 **DeepSeek** 插件（provider 标识 `langgenius/deepseek/deepseek`，默认用 `deepseek-flash`）。
2. 新建知识库，把 `samples/` 下的 **6 份装备技术资料**全部上传（HYS-GY-001 工作原理、HYS-CZ-002 操作规程、HYS-WX-003 维修规程、HYS-GZ-004 典型故障案例集、HYS-ZL-005 资料清单与版本台账、HYS-TY-005 术语与缩略语对照表），等待索引完成，记录知识库 ID。
3. **上传前务必剔除作废版本段落**：HYS-CZ-002 V3.0（滤芯周期旧值 300 工作小时）与 HYS-GY-001 V2.0（额定压力旧值 14 MPa）不得进入知识库，版本判定以 HYS-ZL-005 V1.0 台账为准。

### 第 2 步：导入三个 DSL

在 Dify 中：**工作室 → 创建应用 → 从 DSL 文件导入**，依次导入 `dsl/` 下的三个文件。

导入后打开 Chatflow，点开「知识检索」节点，把占位符 `00000000-0000-0000-0000-000000000000` 换成第 1 步的知识库，并将 **top_k 设为 6**、**建议开启 Rerank**（装备资料中参数与条款密集，重排可显著提升检索命中质量 RQ）。

> 校验 DSL 是否完好：`python tools/validate_dsl.py`（应输出 3 个 PASS、0 错误）

### 第 3 步：创建 API 密钥并写入 .env

为三个应用分别创建 API 密钥（形如 `app-xxx`），为知识库创建一个密钥（形如 `dataset-xxx`），然后**用隐藏回显方式**写入 `%USERPROFILE%\.env`：

```powershell
$k = Read-Host "Dify API Key" -AsSecureString
$p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($k)
$s = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p)
Add-Content -Path "$HOME\.env" -Value "DIFY_APP_API_KEY=$s"
```

需要四个变量：`DIFY_BASE_URL`、`DIFY_APP_API_KEY`、`DIFY_EVAL_API_KEY`、`DIFY_KB_API_KEY`。

> **切勿把密钥粘贴到聊天或提交到代码库。** 完整命令见 `docs/02-系统架构与部署指南.md`。

### 第 4 步：自检

```powershell
D:\ProgramData\anaconda3\python.exe tools\check_env.py check      # 校验凭据与连通性
D:\ProgramData\anaconda3\python.exe tools\check_env.py datasets   # 列出知识库 ID
```

### 第 5 步：先跑离线演示，再接真实数据

```powershell
# 无凭据也能跑通的全流程演示（mock 数据）
D:\ProgramData\anaconda3\python.exe eval_service\cli.py demo

# 真实数据：采集 → 评分 → 报表 → badcase
D:\ProgramData\anaconda3\python.exe eval_service\cli.py collect --limit 200
D:\ProgramData\anaconda3\python.exe eval_service\cli.py judge --limit 200 --concurrency 4
D:\ProgramData\anaconda3\python.exe eval_service\cli.py report --period week --format md,html,csv
D:\ProgramData\anaconda3\python.exe eval_service\cli.py badcase --min-severity P1
```

---

## 五、常用命令

| 目的 | 命令 |
| --- | --- |
| 一键演示（离线） | `python eval_service\cli.py demo` |
| 拉取会话与反馈 | `python eval_service\cli.py collect --since 2026-09-01` |
| 批量多维评分 | `python eval_service\cli.py judge --limit 500 --concurrency 4` |
| 生成周报/月报 | `python eval_service\cli.py report --period week --format md,html,csv` |
| 挖掘 badcase | `python eval_service\cli.py badcase --min-severity P1` |
| 归因前置（资料 vs 系统） | `python eval_service\cli.py attribute --only-badcase` |
| 文档质量记分卡 | `python eval_service\cli.py scorecard --format md,csv` |
| 治理看板（Windows 本地） | 双击 `启动治理看板.bat`，或 `python eval_service\cli.py dashboard --open` |
| 演示系统（检索契约层 + 看板） | 双击 `启动演示系统.bat`，或 `python kb_bridge\bridge_server.py --port 9380` + 看板 |
| 检索契约层自测 | `python kb_bridge\selftest.py` |
| 生成演示资产 / 业务库 | `python tools\make_demo_assets.py --force`、`python tools\make_demo_business_db.py --force` |
| 建立回归基线 | `python eval_service\cli.py regression --golden eval_service\golden_set.jsonl --save-baseline` |
| 发布前回归 | `python eval_service\cli.py regression --golden eval_service\golden_set.jsonl --baseline out\baseline.json` |
| 资料入库前治理校验 | `python tools\check_corpus.py` |
| 批量补充知识库 | `python eval_service\cli.py kb-build --source ..\samples --dataset-id <id>` |
| DSL 静态校验 | `python tools\validate_dsl.py` |
| 评分逻辑自测 | `python tools\test_code_nodes.py` |
| 跨文件契约测试 | `python tools\test_contract.py` |
| 评测服务自检 | `python eval_service\tests\selftest.py` |
| 凭据自检 | `python tools\check_env.py check` |

四套测试 + 一道资料治理门禁全部通过时，说明 DSL、评测服务与知识语料三者都没有被改坏：

```
tools\check_corpus.py            6 份资料，288 条款，0 错误，0 告警
tools\validate_dsl.py            3 个 DSL 全 PASS，0 错误
tools\test_code_nodes.py         50 项断言，0 失败
tools\test_contract.py           72 项断言，0 失败
eval_service\tests\selftest.py  107 项断言，0 失败
```

> `check_corpus.py` 是**资料入库前的门禁**：校验 8 字段文档头、文档头与《装备技术资料清单与版本台账》
> 的编号/版本/状态一致性、同一编号只有一个现行有效版本、作废版本数值不得泄漏到台账之外、
> 关键技术参数在各资料间取值一致、术语必须规范。
> 资料入库的顺序是 **check_corpus → kb-build → 索引 → 冒烟验证**。

> Windows 控制台中文乱码时，先执行：`[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $env:PYTHONIOENCODING="utf-8"`

---

## 六、验收清单

| # | 验收项 | 判定方法 |
| --- | --- | --- |
| 1 | 三个 DSL 可无损导入 | Dify 导入无报错；`tools/validate_dsl.py` 全 PASS |
| 2 | 问答链路可用 | 知识库内问题能正确作答并带引用；知识库外问题走兜底并明示未覆盖 |
| 3 | 满意度采集可用 | 点 👍/👎 有记录；发送 `#评价 4 建议` 返回评价回执 |
| 4 | 评价模型可用 | 工作流单跑输出 `csi` 与 `verdict`，并输出 `safety_compliance` / `version_currency` / `terminology_norm`；`tools/test_code_nodes.py` 50 项断言全通过 |
| 5 | 标注链路可用 | 语料标注工作流输出 `jsonl` 与人工复核表 |
| 6 | 指标可统计 | 报表含 CSI 趋势、六维分布、verdict 分布、badcase 归因占比 |
| 7 | 闭环可运转 | badcase 能生成知识补全清单；回归命令能输出改进/退步明细 |
| 8 | 自动评分可信 | 人工抽检 30 条，完全一致率 ≥ 70%、相邻一致率 ≥ 95% |
| 9 | 安全类回答零遗漏 | 抽查操作 / 维修 / 应急处置类问题：回答完整传达资料原文的安全警示与禁止事项，**safety_compliance ≤ 2 的条数为 0** |
| 10 | 无作废版本数值进入回答 | 针对滤芯周期、额定压力等易错点提问，回答引用现行版本数值（HYS-CZ-002 V4.0 滤芯 500 工作小时 / 12 个月、HYS-GY-001 V3.0），**version_currency = 1 的条数占比 ≤ 3%** |
| 11 | 术语规范 | 术语与缩略语符合 HYS-TY-005 V2.0，缩略语首次出现给出中文全称，**terminology_norm 均值 ≥ 4.0** |
| 12 | 黄金集回归通过 | `python eval_service\cli.py regression --golden eval_service\golden_set.jsonl --baseline out\baseline.json` 跑通 g01~g12 共 12 条装备域题，无新增 P0，CSI 均值不低于基线 |
| 13 | 版本台账与冒烟清单 | 知识库中无作废版本段落；`samples/冒烟测试问题清单.md` 逐条通过 |
| 14 | 资料入库门禁 | `python tools\check_corpus.py` 输出 0 错误 0 告警：文档头 8 字段齐全、编号版本与台账一致、唯一现行版本、作废值未泄漏、参数一致、术语规范 |

---

## 七、注意事项

1. **DSL 版本**：三个文件声明 `version: '0.7.0'`，对应 Dify 1.16+。若你的实例低于 1.16，把三个文件里的版本号改成 `'0.6.0'`（Dify 1.15.x）或 `'0.1.5'`（更老版本）后再导入。
2. **模型可替换**：所有 LLM 节点默认 `langgenius/deepseek/deepseek` + `deepseek-flash`。换模型只需在画布上点开节点重新选择，评分节点建议用更强的模型（如 `deepseek-v4-pro`）。
3. **回执标记不可随意删改**：回答里的 `[[EVAL_RECEIPT]]score=N,suggestion=...[[/EVAL_RECEIPT]]` 是自动解析的锚点。之所以用键值对而不是 JSON，是因为 Jinja2 模板无法对用户建议做 JSON 转义（建议里的引号/换行会把 JSON 打碎）。改模板必须同步改 `eval_service/collector.py`，并跑 `python tools\test_contract.py`——该测试会把模板渲染后喂给解析器做往返校验，格式一旦对不上立即失败。
4. **评价数据脱敏**：进入报表前应剔除或哈希用户标识。
5. **成本控制**：全量自动评分会产生模型调用费用，建议对高频问题全量、长尾问题抽样。
6. **作废版本治理（装备域必做）**：知识库中必须清除 HYS-CZ-002 V3.0（滤芯周期旧值 300 工作小时，2021-05-10）与 HYS-GY-001 V2.0（额定压力旧值 14 MPa，2019-07-22）等已作废版本段落；现行版本判定以《装备技术资料清单与版本台账》（HYS-ZL-005 V1.0，2025-06-01）为准。资料换版后要在知识库、提示词与回归黄金集三处同步更新，否则会出现「版本失效」类 badcase（P1，本周内处理）。