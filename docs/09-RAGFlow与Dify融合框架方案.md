# RAGFlow × Dify 融合框架方案

> 文档编号：EVAL-ARCH-009
> 项目名称：基于大模型的多模数据管理查询及迭代优化技术研究
> 编写时间：2026-09-23
> 密级：内部
> 适用范围：装备技术资料智能问答与评价闭环迭代系统（现基于 Dify 1.17.1 构建）
> 结论摘要：**融合的正确形态不是「并排部署两个平台」，而是把 RAGFlow 拆成「文档解析能力」与「chunk 级检索能力」两个可独立接入的能力面，用三个契约（文档 / 检索 / 引用）把接口固定下来，按 L0 / L1 / L2 三档渐进接入。**
> 在当前硬约束（ARM64 信创、禁容器、离线摆渡、全栈国产化）下，推荐 **L0（离线解析工厂）+ L1（检索契约层）**；
> L2（RAGFlow 全栈原生化）只在甲方明确要求且资源到位时启动。
> 治理视角的定位与度量框架见 `docs/10-以文档质量为中心的治理度量框架.md`：**本文件解决「仪器怎么接」，doc 10 解决「测什么、算谁的账」**。

---

## 1. 结论先行

| 问题 | 结论 |
| --- | --- |
| 两个平台叠加能带来什么 | RAGFlow 补的是**资料「读进来」的质量**（版式 / 表格 / 扫描件 / 图片 / chunk 级可追溯），Dify 补的是**「用起来」的链路**（编排、发布、会话、反馈、Workflow）。二者互补，不冲突 |
| 最大的坑是什么 | **重叠区**（知识库管理、向量检索、问答界面）两边都有一套。重叠区只能选一个主人，否则出现双源语料、双份答案、两套口径 |
| 融合的接口在哪 | 只有三条接缝：**解析接缝、检索接缝、入库接缝**。接缝用契约固定，接缝之外两边各自演进 |
| 推荐形态 | 六层架构：治理层（自有）→ 解析层（RAGFlow）→ 检索层（RAGFlow 或 Dify，经契约层隔离）→ 编排层（Dify）→ 评价层（自有 eval_service）→ 展示层（Dify WebApp / 自建） |
| 现在最该先做哪一步 | **L0：拿 1 份真实扫描件 / 复杂表格 PDF 做 30 分钟对照实验**（RAGFlow DeepDoc 解析 vs 现状解析），收益看得见再谈架构改动 |
| 评测闭环要不要改 | **不要改。** eval_service 与检索引擎解耦是这套框架最重要的性质：换引擎不改评价口径，黄金集 g01~g12 是唯一裁决者 |

---

## 2. 两个平台的能力边界：到底该结合什么

### 2.1 能力面归属表

| 能力面 | RAGFlow 的强项 | Dify 的强项 | 本项目归属 |
| --- | --- | --- | --- |
| 文档解析 | ★★★ DeepDoc：版式还原、表格结构化、扫描件 OCR、图片抽取、TOC 增强 | ★☆ 基础解析（含视觉模型兜底） | **RAGFlow**，产出物回归治理层 |
| 切片策略 | ★★★ 模板化 chunk、父子块（parent-child）、RAPTOR、按条款 / 表格切 | ★★ 通用分段 + 自定义分段 | **RAGFlow**（装备资料条款密集，切法决定成败） |
| 检索 | ★★★ 混合检索（向量 + 词项加权）、rerank、元数据过滤、知识图谱多跳 | ★★ 向量 + 全文 + rerank，元数据过滤能力弱 | **RAGFlow**，或经契约层后接 Dify |
| 可追溯性 | ★★★ chunk 级引用、页码坐标 positions、图片 image_id | ★★ 引用卡片（体验好，粒度到文档段） | 两者都要，靠 C3 引用契约统一 |
| 编排 | ★ 仅 Chat Assistant 的固定提示词配置 | ★★★ Chatflow / Workflow / 条件分支 / 变量 / 循环 / 代码节点 | **Dify** |
| 多模态入口 | ★★ 文档内图片理解 | ★★★ 用户上传图片 / 视频 / 文档 `sys.files` + vision 节点 | **Dify**（入口侧） |
| 会话与反馈 | ★ 有会话，无标准反馈 API | ★★★ 会话管理 + 消息反馈 API（👍/👎） | **Dify** |
| 发布与界面 | ★★ 自带 Web | ★★★ WebApp / 嵌入 / API | **Dify** |
| 元数据 / 版本治理 | ★★★ 文档级 meta_fields + 检索期 metadata_condition 硬过滤 | ★★ 文档元数据查询能力有限 | **RAGFlow**（本项目关键差异点） |

### 2.2 一句话分工

- **RAGFlow 管「资料怎么被读懂、怎么被准确捞出来」**；
- **Dify 管「用户怎么问、答案怎么给、链路怎么编排、反馈怎么收」**；
- **eval_service 管「答得好不好，谁说了算」**——它的口径不因引擎更替而改动。

### 2.3 收益取决于语料真实形态（决定要不要上 RAGFlow 的第一判据）

| 语料形态 | DeepDoc 解析带来的增益 | 结论 |
| --- | --- | --- |
| 结构良好的 Markdown / 原生电子文档 | 接近 0（现在 `samples/` 的 6 份资料就属于这一类） | 只上 L1 契约层，不必上 RAGFlow |
| 扫描件 PDF、影印规程、图纸说明 | 极高（OCR + 版式还原，现状几乎无法处理） | L0 优先，收益立竿见影 |
| 参数表密集、跨页大表 | 高（表格结构化，避免参数错行 / 串行） | L0 + L1 |
| 图文混排（结构图、故障照片） | 高（图片抽取 + 位置坐标可回链） | L0 + 多模态分工（§8） |

> **行动要求（30 分钟可完成）**：取 1 份真实困难样本（例如仓库根目录的《某项目技术要求.pdf》这类交付物），
> 分别用现状解析链路与 RAGFlow 解析跑一遍，比对：条款编号是否保留、表格是否错行、数值是否串行、页眉页脚是否污染。
> **这份对照结果应当写入本章，作为 L0 是否立项的证据**。本方案后续档位设计不以「技术先进」为准，以这份对照为准。

---

## 3. 总体架构：六层 + 三条接缝 + 三个契约

```
┌─────────────────────────────────────────────────────────────────────┐
│ L6 展示与运营层      Dify WebApp / 自建前端（docs/04）                │
├─────────────────────────────────────────────────────────────────────┤
│ L5 评价闭环层        eval_service（六维 + CSI + SC/VC/TN + badcase）  │  ← 自有，唯一口径
│                      collector / judge / metrics / report / regression│
├─────────────────────────────────────────────────────────────────────┤
│ L4 编排与会话层      Dify Chatflow / Workflow（多模态入口、反馈采集） │  ← 取 Dify 之长
│                      ↑ 检索结果按 C2 契约注入（保留原生引用卡片）      │
├─────────────────────────── 接缝②检索 ───────────────────────────────┤
│ L3 检索层            kb_bridge（实现 Dify 外部知识库协议）            │  ← 契约隔离层
│                      后端可切：RAGFlow / Dify 内置 / 混合            │
├─────────────────────────── 接缝①解析 ───────────────────────────────┤
│ L2 解析与索引层      RAGFlow（DeepDoc 解析 + 混合检索 + 元数据过滤）  │  ← 取 RAGFlow 之长
├─────────────────────────── 接缝③入库 ───────────────────────────────┤
│ L1 语料治理层        check_corpus → 入库清单 manifest（8 字段文档头） │  ← 自有，唯一真源
├─────────────────────────────────────────────────────────────────────┤
│ L0 资料源            正式发布的技术资料（电子文档 / 扫描件 / 图纸）   │
└─────────────────────────────────────────────────────────────────────┘
```

> 图注：数字圈 ①②③ 即三条接缝。**框架的稳定性来自「接缝少、契约硬」**：只要有第四条接缝（例如让 Dify 直接读 RAGFlow 的数据库），框架就开始腐化。

### 3.1 分层职责与「取谁的优点」

| 层 | 主责 | 取谁的优点 | 本项目现状 | 需新增 |
| --- | --- | --- | --- | --- |
| L1 语料治理 | 编号 / 版本 / 状态 / 参数一致性 / 术语规范的准入校验 | 自有资产（288 条款、0 错误门禁） | ✅ 已有 `tools/check_corpus.py` | 增加「入库清单」产物 |
| L2 解析与索引 | 把资料变成结构化、可检索、可追溯的 chunk | RAGFlow DeepDoc + 混合检索 | ⚠️ 现状：Dify 内置解析 | 按档位接入 |
| L3 检索契约 | 统一检输入 / 输出与分数语义 | 自有（这是本方案新增的核心） | ❌ 无 | `kb_bridge` |
| L4 编排与会话 | 问法、路由、兜底、多模态入口、反馈 | Dify | ✅ 已有三个 DSL | 检索节点改为外部知识库 |
| L5 评价闭环 | 打分、归因、报表、回归 | 自有（六维 + 装备域专项维度） | ✅ 已有 | 报表增加 `engine` 维度 |
| L6 展示运营 | 界面、引用卡片、安全警示 | Dify WebApp | ⚠️ 见 docs/04 | 引用卡片字段扩展 |

### 3.2 单一真源原则（必须写进纪律）

| 对象 | 唯一真源 | 其他位置的角色 |
| --- | --- | --- |
| 资料内容 | 治理后的语料文件（`samples/` 同规范） | 索引只是投影，可随时重建 |
| 版本状态 | 《装备技术资料清单与版本台账》HYS-ZL-005 | 引擎里的 `status` 元数据是它的镜像，来自 manifest |
| 检索服务 | 同一时刻只允许一个引擎对外服务 | 另一引擎只能做影子对比，不得直接对用户 |
| 评价口径 | `eval_service` | 任何引擎的分数都必须归一化后进入，不得直接当满意度 |

> 反面案例（必须避免）：让 Dify 内置知识库与 RAGFlow 同时长期对外提供检索。结果必然是「同一个问题两个答案」，
> 而报表无法归因——这正是本项目 badcase 归因里「跨文档冲突」之外的**第五种系统性错误：双源冲突**。

---

## 4. 三个契约（框架的骨架）

「结合两个平台的优点」之所以常常失败，是因为接口靠人记、靠约定。本框架把接口降成三个可测试的契约。

### 4.1 C1 文档契约（Document Contract）

沿用本项目既有的 8 字段文档头，并把其中治理字段**物化**为入库清单 `manifest.jsonl`，一行一份资料：

```json
{
  "doc_no": "HYS-CZ-002",          // 资料编号（唯一）
  "version": "V4.0",               // 版本号
  "status": "现行有效",             // 现行有效 | 作废
  "title": "某型液压举升系统操作规程",
  "effective_date": "2025-06-01",
  "security_level": "内部",         // 密级
  "doc_type": "操作规程",           // 原理|操作规程|维修规程|故障案例|资料管理|术语
  "source_file": "samples/02-某型液压举升系统操作规程.md",
  "source_sha256": "...",           // 内容指纹：防止「索引与语料不一致」
  "chunk_method": "naive",          // RAGFlow 解析模板（按 doc_type 定策略）
  "parser_config": { "chunk_token_num": 512, "delimiter": "\n## " }
}
```

**契约要求**：manifest 是唯一允许写入引擎元数据的来源；`check_corpus.py` 通过后自动生成；
`source_sha256` 与引擎中该文档的指纹不一致时，入库流水线**拒绝**（对应「资料换版后三处同步」的既有纪律）。

### 4.2 C2 检索契约（Retrieval Contract）

任何引擎（RAGFlow / Dify 内置 / 未来其他）返回的 chunk，必须能被归一化成同一结构：

| 字段 | 含义 | 来源（RAGFlow → 统一） | 用途 |
| --- | --- | --- | --- |
| `chunk_id` | chunk 唯一标识 | `chunks[].id` | 去重、引用锚点、badcase 定位 |
| `doc_no` | 资料编号 | 文档 meta_fields（经 `document_id` 映射） | 引用展示、VC 判定 |
| `version` | 版本号 | 文档 meta_fields | **版本红线**、VC 判定 |
| `status` | 现行有效 / 作废 | 文档 meta_fields | **检索期硬过滤** |
| `section_path` | 章节路径（如 `4.2 滤芯更换`） | chunk 内容首行 / 标题层级 | 引用可读性、人工复核 |
| `page` | 页码 | `positions` | 引用卡片、原文回溯 |
| `content` | 正文 | `content` | 送入 LLM 的唯一文本 |
| `score` | 归一化相似度 0~1 | `similarity`（已是 0~1 加权混合分） | 阈值、RQ 指标、排序 |
| `engine` | `ragflow` / `dify` | 注入 | 报表 A/B 维度 |
| `image_refs` | 关联图片 | `image_id` | 多模态回链（§8） |

**契约要求**：`content` 之外的一切都是元数据，禁止把元数据拼进正文（否则污染 LLM 上下文，也污染 CSI 打分）；
`score` 必须单调、0~1、可跨引擎比较（见 §6.3）。

### 4.3 C3 引用契约（Citation Contract）

回答中、界面上、评价里，同一条知识必须能被同一个字符串定位：

```
引用标识 = <资料编号> <版本号> §<章节> P<页码>
示例     = HYS-CZ-002 V4.0 §4.2 P12
判定正则 = ^(HYS-[A-Z]{2}-\d{3})\s+(V\d+\.\d+)\s+§([\d.]+)\s+P(\d+)$
```

三处落地：

| 位置 | 载体 | 现状 | 改造 |
| --- | --- | --- | --- |
| 回答正文 | Chatflow 提示词模板 | 已要求标注资料编号与版本 | 补齐章节与页码 |
| 界面卡片 | Dify 引用卡片（`records[].title` / `metadata`） | 仅文档名 | bridge 把 C3 标识拼进 `title`，`metadata` 带全字段 |
| 评价判定 | 评价工作流的 `version_currency` | 由 LLM 从正文里认 | 优先读结构化字段，LLM 只做兜底 |

---

## 5. 接缝①：解析（RAGFlow DeepDoc → 治理语料）

**形态**：离线流水线（L0）。RAGFlow 在此处是「解析工具」，不是「运行时组件」。

```
原始资料(PDF/扫描件)
   → [联网构建机] RAGFlow 解析（版式/表格/OCR/图片抽取）
   → 结构化 Markdown（表格转 Markdown 表、条款编号保留、图片另存 + 占位引用）
   → 人工抽检（条款数、数值、表头三类必查）
   → tools/check_corpus.py 门禁（0 错误 0 告警）
   → 进入 samples/ 规范语料库（唯一真源）
```

### 5.1 产物规范

| 项目 | 要求 |
| --- | --- |
| 文档头 | 8 字段齐全，与台账 HYS-ZL-005 一致（check_corpus 强校验） |
| 表格 | 保留为 Markdown 表；跨页表合并并标注「（跨页）」；表头不得丢失 |
| 数值 | 原样保留（含单位、公差、上下限），禁止四舍五入或单位换算 |
| 条款编号 | 原编号保留（如 `4.2.3`），不得重排 |
| 图片 | 另存为 `assets/<doc_no>/<序号>.png`，正文以 `![图N](assets/.../N.png)` 占位 |
| 页眉页脚 / 水印 | 剔除；页码信息转入 chunk 元数据而非正文 |
| 作废版本 | **解析阶段就隔离**，产物带 `status: 作废`，进入独立目录，不参与入库 |

### 5.2 验收判据

| # | 判据 | 方法 |
| --- | --- | --- |
| 1 | 条款不丢 | 解析后条款计数 ≥ 原件条款计数（抽样 1 份全套人工点数） |
| 2 | 表格不错行 | 抽检 10 张参数表，逐格比对原件 |
| 3 | 数值零篡改 | 关键字参数（滤芯周期、额定压力等）与台账一致 |
| 4 | 过门禁 | `python tools\check_corpus.py` 0 错误 0 告警 |

> **L0 不需要 RAGFlow 进内网生产环境**，只需要一台联网构建机。这是本档位风险最低的根本原因。

---

## 6. 接缝②：检索（`kb_bridge` 实现 Dify 外部知识库协议）

### 6.1 为什么选「外部知识库 API」这一条路

Dify 侧有三种接法，逐一比对（这是本方案最关键的选型）：

| 方案 | 接入方式 | 保留 Dify 原生引用卡片 | 链路确定性 | 离线/信创代价 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **A. 外部知识库 API** | 知识库 → 外部知识库 API → 注册 endpoint + key；Chatflow 的检索节点直接引用 | ✅ 保留 | ✅ 每次提问必检索，无额外 LLM 决策 | 只需部署一个 stdlib 小服务 | ★ **推荐** |
| B. MCP 工具调用 | 安装 MCP 客户端插件 → 连 RAGFlow MCP server | ❌ 工具结果不产生引用卡片 | ⚠️ 检索变成 Agent 的工具选择，可能不检索或重复检索 | 需离线打包 `.difypkg`，多一个插件依赖 | 备用（探索型问答可用） |
| C. HTTP Request 节点 | Chatflow 里直接调 RAGFlow `/api/v1/retrieval` | ❌ 丢失原生引用 UI，需自绘来源 | ✅ 确定 | 无额外服务 | 不推荐（体验倒退） |

**关键事实（已核对上游文档）**：Dify 会向 `{你注册的 endpoint}/retrieval` 发 POST，
请求体为 `knowledge_id` / `query` / `retrieval_setting{top_k, score_threshold}`，可选 `metadata_condition`；
期望响应 `{"records":[{"content","score","title","metadata"}]}`，`metadata` 必须是对象（`null` 会让 Dify 检索管线报错）。
**其中 `metadata_condition` 目前 Dify 没有界面可配，只能由程序传入** —— 这恰好决定了 §6.4 的设计：由 bridge 服务端强制过滤，比依赖界面配置更可靠。

### 6.2 协议映射全表

**Dify → bridge → RAGFlow（请求方向）**

| Dify 请求字段 | bridge 处理 | RAGFlow 请求字段 |
| --- | --- | --- |
| `query` | 原样透传（可做术语归一化，见下） | `question` |
| `knowledge_id` | 映射为知识库配置项（dataset_ids 等），**不暴露给调用方** | `dataset_ids`（可多条） |
| `retrieval_setting.top_k` | `page_size = top_k`；召回池另设（≥ 8×top_k） | `page_size`、`knn_top_k`、`rerank_candidates_count` |
| `retrieval_setting.score_threshold` | 0.0 视为「不限」，直接映射；否则作为下限 | `similarity_threshold` |
| `metadata_condition`（可选） | **与强制条件合并**（AND），调用方不能放宽版本约束 | `metadata_condition` |
| （bridge 注入） | 固定注入，不可被调用方覆盖 | `metadata_condition.conditions += {name:"status", comparison_operator:"is", value:"现行有效"}` |
| （bridge 配置） | 混合权重：装备资料以词项命中为主 | `vector_similarity_weight: 0.3` |
| （bridge 配置） | 重排模型（BGE-Reranker 本地部署） | `rerank_id` |
| （bridge 配置） | 关键词匹配开关 | `keyword: true` |

**RAGFlow → bridge → Dify（响应方向）**

| RAGFlow 响应字段 | bridge 处理 | Dify record 字段 |
| --- | --- | --- |
| `chunks[].content` | 原样 | `content` |
| `chunks[].similarity` | 归一化 + 阈值复核（§6.3） | `score` |
| `chunks[].document_id` | 查元数据缓存 → `doc_no` / `version` | `title` = `HYS-CZ-002 V4.0`（C3 标识） |
| `chunks[].positions` | 取首页页码 | `metadata.page` |
| `chunks[].id` | 原样 | `metadata.chunk_id` |
| `chunks[].image_id` | 原样 | `metadata.image_refs` |
| （bridge 注入） | 引擎与分数明细 | `metadata.engine` / `metadata.vector_similarity` / `metadata.term_similarity` |
| `doc_aggs[]` | 不单独返回，用于日志与去重统计 | — |

> 注意：RAGFlow 的检索响应本身**不带文档元数据**，只带 `document_id`。因此 bridge 必须维护
> **`document_id → {doc_no, version, status, page_offset}` 的元数据缓存**，在入库后刷新（也可按 `GET /api/v1/datasets/{id}/documents` 定时校准）。
> 这是本方案里唯一一处「必须自建的粘合逻辑」，也是最容易被低估的工作量。

### 6.3 分数归一化与阈值语义（不做这一步，RQ 指标会失真）

| 事实 | 处理 |
| --- | --- |
| RAGFlow `similarity` = 向量相似度 × `vector_similarity_weight` + 词项相似度 × (1 − 权重)，取值天然 0~1 | 直接作为 `score`，无需再缩放 |
| Dify 的 `score_threshold` 语义是「最低相似度（0~1）」，`0.0` 表示未启用 | `0.0` → 不设 `similarity_threshold`（或设为引擎默认），避免把全部结果过滤掉 |
| 两个引擎的分数分布不同（RAGFlow 混合分普遍偏高） | **跨引擎比较只看排序与命中率，不看绝对值**；报表里按引擎分别给分位数 |
| 检索命中质量 RQ 在 `metrics.py` 中已有定义 | 数据源从「Dify 检索节点分数」切到「bridge 返回的 `score`」；口径不变，来源可切换 |

### 6.4 版本硬过滤：把 VC 从「提示词约束」升级为「检索层约束」

这是本项目引入 RAGFlow 最硬的理由（直接支撑验收项 #10：作废版本数值不得进入回答，`version_currency = 1` 占比 ≤ 3%）：

| 层次 | 现状（纯 Dify） | 融合后 |
| --- | --- | --- |
| 语料层 | 靠人工剔除作废段落 | 治理层保证（不变） |
| 检索层 | 无法按元数据过滤，作废段落一旦入库就可能被召回 | **bridge 强制注入 `status = 现行有效`，作废 chunk 从物理上召不回** |
| 提示词层 | 靠提示词叮嘱「不得引用作废版本」 | 保留，作为第二道防线 |
| 评价层 | LLM 判定 `version_currency` | 保留；硬过滤后若仍出现 VC=1，说明**元数据缺失**，反向驱动治理补录 |

> 三层防御的价值：**提示词是概率，元数据过滤是确定性**。安全与版本这两条红线，本项目的态度应当是「能用确定性手段就不用概率手段」。

### 6.5 超时、缓存与降级

| 项 | 设计 |
| --- | --- |
| 超时预算 | bridge 对 RAGFlow 的调用 8s（可配）；Dify 侧整体容忍 10s 内 |
| 检索缓存 | 相同 `query` + 知识库 60s 内存缓存（高频重复提问是军内问答的常态） |
| 熔断 | 连续 N 次失败进入熔断，直接返回降级结果而不是拖慢全链路 |
| 降级一 | 返回空 `records` → Chatflow 走既有「兜底回答」分支（明示资料未覆盖，不猜测） |
| 降级二（双轨期） | bridge 内置 fallback 到 Dify 内置知识库检索，`metadata.engine = dify-fallback` 标记，报表可统计降级率 |
| 降级三 | 完全不可用时，Chatflow 条件分支直接给运维提示页 |

### 6.6 `kb_bridge` 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/retrieval` | **Dify 外部知识库协议入口**（Bearer 校验 → 参数合并 → 强制元数据条件 → 调引擎 → 归一化 → 返回 records） |
| GET | `/healthz` | 存活 + 后端连通性（给 systemd / 监控用） |
| GET | `/admin/mapping` | 查看 `document_id → doc_no/version` 映射（排障用） |
| POST | `/admin/reload` | 重新加载 manifest 与元数据缓存（入库后触发） |
| GET | `/admin/stats` | 命中率、降级率、平均延迟、分数分布（喂给报表做引擎维度） |

**技术要求**（与 `eval_service` 保持一致）

- 仅 Python 标准库（`http.server` + `urllib`），不引入框架依赖 → 便于离线摆渡与信创环境部署；
- 配置复用 `.env` 解析逻辑（增加 `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY` / `RAGFLOW_DATASET_IDS`）；
- 密钥只输出掩码，不落日志；
- 自带 `selftest.py`：mock 后端跑通「请求 → 归一化 → 响应」全流程，无凭据也能测。

---

## 7. 接缝③：入库（语料治理 → 索引）

```
check_corpus.py 通过
   → 生成 manifest.jsonl（C1 契约）
   → 建 / 找 dataset（POST /api/v1/datasets，指定 embedding 模型与 chunk_method）
   → 上传文档（POST /api/v1/datasets/{id}/documents，multipart）
   → 写入元数据（PUT /api/v1/datasets/{id}/documents/{doc_id}，meta_fields = C1 字段）
   → 触发解析（Parse documents）
   → 轮询解析状态直到完成（失败即中止，不发版）
   → 冒烟检索 6 问（samples/冒烟测试问题清单.md）
   → 刷新 bridge 元数据缓存（POST /admin/reload）
```

| 环节 | 硬规则 |
| --- | --- |
| 元数据先行 | **先写 `meta_fields` 再触发解析**，避免解析期被检索到无元数据的 chunk |
| 幂等 | 以 `doc_no + version + source_sha256` 为幂等键：同键重复上传 → 更新而非新增；换版 → 旧版标记作废并移出检索范围 |
| 作废隔离 | 作废版本可保留在库（供追溯），但 `status=作废`，被 §6.4 的强制条件挡在检索之外 |
| 解析门禁 | 解析失败 / chunk 数为 0 / 冒烟未过 → **阻断本次发版**（与既有「未达标不允许发布」一致） |
| 可回滚 | 入库前后各留一次 dataset 快照（文档 ID 清单），异常时按清单回滚 |

---

## 8. 多模态分工

两类「多模态」完全不同，必须分开归属，否则会做出一个四不像：

| 模态场景 | 例子 | 归属 | 接口 / 节点 |
| --- | --- | --- | --- |
| **入口多模态**：用户现场拍的照片 / 短视频 | 操作手拍下故障部位照片问「这是什么故障」 | **Dify** | 已有：`sys.files` + vision 节点 + 「解析服务不可用时由视觉模型直接读图」的兜底分支 |
| **语料内多模态**：资料里的结构图、曲线、零件图 | 原理图里的液压回路、维修规程里的分解图 | **RAGFlow（解析期）** | 解析期抽图 → `assets/`；检索期 `image_id` → bridge 透传 → 界面回链 |
| 视频类资料 | 培训视频、操作录像 | 转写后按文本治理（ASR：Paraformer / SenseVoice，见 docs/08） | 转写产物进入 L1 治理层，与文字资料同等待遇 |

**设计要点**：语料内图片必须**回链到资料编号 + 页码**，否则界面里出现一张无出处的图，在装备场景是不可接受的；
而入口图片不进入知识库（既不训练也不入库），只作为本次提问的上下文——避免用户随手拍的图污染知识资产。

---

## 9. 评测闭环不变式（融合不破坏评价口径）

### 9.1 三个不变式

| 不变式 | 内容 | 违反后果 |
| --- | --- | --- |
| I1 引擎无关 | `collector.py` / `judge.py` / `metrics.py` / `store.py` 不感知检索引擎 | 换引擎要改评测代码 → 历史数据不可比 |
| I2 口径唯一 | CSI、SC、VC、TN 的定义只在 `docs/01` 定义 | 多方口径 → 报表互不认账 |
| I3 回归裁决 | 黄金集 g01~g12 通过是发版前提 | 引擎升级靠感觉 → 退步不可发现 |

### 9.2 引擎维度只增加「数据列」，不增加「新指标」

| 报表新增 | 用途 |
| --- | --- |
| `messages.engine` / `evaluations.engine` | 按引擎聚合 CSI / RQ / VC / 引用正确率 |
| 降级率（`engine=dify-fallback` 占比） | 运维健康度 |
| 引用正确率（C3 标识可解析且与命中 chunk 一致的比例） | 检索质量与提示词纪律的共同体现 |

### 9.3 切换前的影子对比（灰度的核心动作）

```
同一批黄金集 g01~g12（外加从真实 badcase 里挑 20 条）
   ├─ 引擎 A：现状（Dify 内置检索）
   └─ 引擎 B：RAGFlow 经 bridge
比较维度：RQ（检索命中质量）/ VC（版本时效性）/ 引用正确率 / CSI 均值 / P0 数 / 平均延迟
判定：B 在 CSI 均值不劣化、P0 不增加、且 VC 与引用正确率显著改善 → 允许切换；否则不切
```

> 这套对比之所以可行，正是因为 C2 契约让两个引擎的输出可比较。没有契约，A/B 只是两台机器的各说各话。

---

## 10. 信创与离线部署约束（本方案最硬的一段）

### 10.1 RAGFlow 的依赖 vs 本项目约束

RAGFlow 官方以容器编排为唯一受支持路径，其内置依赖栈如下（已核对上游 `docker-compose-base.yml`）：

| RAGFlow 组件 | 官方形态 | 本项目约束（docs/08） | 可行替代 | 风险 |
| --- | --- | --- | --- | --- |
| 应用本体 + 任务执行器 | `infiniflow/ragflow` 镜像（AMD64/ARM64 需自行核实） | 禁容器、ARM64、离线 | 原生进程 + systemd + 离线依赖树（同 Dify 现有打法） | **高**：Python 依赖树体量约 Dify 量级，且含大量解析模型 |
| 文档引擎 | Elasticsearch（默认）/ OpenSearch / Infinity / SerenaDB / OceanBase / seekdb | 许可审查 + 国产化 | **Infinity**（单二进制，C++）、**OceanBase / seekdb**（国产，官方已支持为文档引擎） | 中：Infinity 的 ARM64 构建需核实 |
| 元数据库 | MySQL 8.0.40 | 国产化 | **OceanBase / seekdb**（MySQL 协议兼容，RAGFlow 已有对应编排项） | 中：需实测兼容性 |
| 对象存储 | MinIO（现上游已换为 `pgsty/silo`） | 许可 + 离线 | 兼容 S3 协议的国产存储 / silo | 中 |
| 缓存 | Valkey / KVrocks | 可用（BSD / Apache） | 直接采用（与 docs/08 的 Valkey 结论一致） | 低 |
| 模型服务 | TEI（嵌入 / 重排） | 国产模型 | BGE + BGE-Reranker 本地部署（docs/08 已选型） | 低 |
| 沙箱执行器 | sandbox-executor-manager（需容器） | **禁容器 → 直接不可用** | 关闭 code-executor / agent 功能 | 低（本项目用不到） |

> **重要提醒（许可）**：Elasticsearch 与 OpenSearch 均非 OSI 认可的开源许可（SSPL / Elastic License 2.0），
> 原 MinIO 为 AGPLv3。若项目有信创合规审查，**这几项是真实的合规风险点**——改用 Infinity / OceanBase 既是国产化选择，也是合规避险。

### 10.2 三种部署档位

| 档位 | 前提 | RAGFlow 的角色 | 内网新增常驻组件 | 推荐度 |
| --- | --- | --- | --- | --- |
| **L0 离线解析工厂** | 有联网构建机 | 解析工具（不联网运行时） | **0 个** | ★★★ 立即做 |
| **L1 检索契约层** | 能在内网再加一台 x86/ARM VM | 检索后端（可与 Dify 同机不同进程） | RAGFlow 全栈（或经 L1 契约层先接 Dify 内置） | ★★★ 目标形态 |
| **L2 全栈原生化** | 禁容器 + ARM64 + 离线摆渡 | 与 Dify 同等待遇的原生进程组 | RAGFlow + 其依赖全部原生化 | ★☆☆ 高风险，需专项评估 |

**关于 L1 的一个重要取舍**：`kb_bridge` 的价值不依赖 RAGFlow。可以先落地 bridge（后端接 Dify 内置检索），
立刻获得：统一的 chunk schema、版本元数据契约、引擎可切换能力、报表的引擎维度。
**等 RAGFlow 就绪时只换后端，不动 Chatflow、不动 DSL、不动评测口径。** 这是本方案给出的最短风险路径。

### 10.3 ARM64 / 国产 OS 验证清单（不要靠听说，逐条实测）

| # | 验证项 | 命令 / 方法 | 通过标准 |
| --- | --- | --- | --- |
| 1 | RAGFlow 是否有 ARM64 镜像 | `docker manifest inspect <RAGFLOW_IMAGE>`（联网机） | 列出 arm64 条目 |
| 2 | 文档引擎 ARM64 | Infinity / OceanBase 的 arm64 发布物 | 能在鲲鹏 / 飞腾上启动 |
| 3 | 解析依赖 | 用离线依赖工具按 `aarch64-manylinux_2_28` 拉取 | 无缺失 wheel / 无必须编译的重包 |
| 4 | ONNX 解析模型 | DeepDoc 的模型文件在 ARM64 上加载 | 解析一张扫描页成功 |
| 5 | glibc | 目标机 `ldd --version` | ≥ 2.28（与 Dify 同门槛） |

> 上游仓库中存在名为 `Dockerfile.scratch.oc9` 的构建文件（OC9 = OpenCloudOS 9，国产 OS 系），
> 说明上游对非 Debian 基础镜像方向有投入，但**这不能作为可行性证据**，仍需按上表实测。

### 10.4 工作量估算（相对现有 96 + 10~16 人·天）

| 档位 | 工作内容 | 估算 |
| --- | --- | --- |
| L0 | 解析流水线脚本 + 抽检规范 + 与 check_corpus 衔接 + 2 份真实资料的解析验证 | 3 ~ 5 人·天 |
| L1 | `kb_bridge`（协议 + 映射 + 元数据缓存 + 降级 + 自测）+ 入库流水线 + DSL 检索节点替换 + 影子对比与报告 | 5 ~ 8 人·天 |
| L2 | RAGFlow 原生化离线包（aarch64）+ 依赖替换（Infinity/OceanBase/Silo）+ 系统集成 + 回归 | 20 ~ 35 人·天（**含不确定性**） |
| 合计（L0+L1） | 推荐范围 | **8 ~ 13 人·天** |

---

## 11. 分档落地路线

| 阶段 | 目标 | 关键动作 | 退出条件（可验收） |
| --- | --- | --- | --- |
| **P0（L0）** | 证明解析收益 | 取 2 份真实困难资料（扫描件 + 参数表 PDF）过 RAGFlow 解析；与现状对照 | 对照报告 + `check_corpus` 0 错误；甲方认可收益 |
| **P1（L1 骨架）** | 建立契约层 | 落地 `kb_bridge`（后端先接 Dify 内置检索）+ manifest + 报表加 `engine` 维度 | bridge 自测通过；Chatflow 经 bridge 检索；黄金集回归不劣化 |
| **P2（L1 后端切换）** | 接 RAGFlow | 入库流水线 → RAGFlow；影子对比；灰度分流（按用户组） | 影子对比达标（§9.3）；VC 与引用正确率改善；可一键回退 |
| **P3（增强）** | 吃满 RAGFlow 能力 | rerank 模型、父子块 / 模板切分、TOC 增强、图片回链、知识图谱多跳（对应 badcase 归因「跨文档冲突」） | 各类 badcase 归因占比下降；CSI 提升并稳定 |
| **P4（可选）** | L2 全栈原生化 | 仅当甲方明确要求 RAGFlow 独立交付 | 信创合规通过 + 性能达标 |

**回退开关（必须预留）**：DSL 中保留两条检索路径（外部知识库 / 内置知识库），用一个会话变量或环境开关切换；
回退操作应当是「改一个配置 + 重载」，而不是「改画布 + 重新发布」。

---

## 12. 降级与容错设计

| 故障 | 现象 | 处理 |
| --- | --- | --- |
| bridge 进程挂掉 | Dify 检索报错 | Chatflow 条件分支捕获 → 走兜底回答；systemd 自动重启 + `/healthz` 告警 |
| RAGFlow 不可达 | bridge 超时 | 熔断 → 降级到 Dify 内置检索（双轨期）或返回空 → 兜底回答 |
| RAGFlow 解析未完成 | 命中数为 0 | 入库流水线已阻断发版，不应在生产出现；监控 chunk 数突降告警 |
| 元数据缺失 | 引用卡片只有文件名、VC 判定失真 | bridge 对缺 `doc_no/version` 的 chunk **打标并降权**（不静默通过）；报表统计缺失率驱动治理补录 |
| 分数异常（全为 0 或全为 1） | RQ 失真 | bridge 自检阈值告警；报表按引擎分位数对照 |
| 双源不一致 | 同一问题答案飘移 | 单一真源原则：双轨期只允许一个引擎对用户，另一个仅影子 |
| 版本更新未同步 | 用户引用旧值 | manifest `source_sha256` 不一致即拒绝入库；黄金集含版本类题目（g 系列）守门 |

---

## 13. 风险登记表

| # | 风险 | 等级 | 触发信号 | 缓解 |
| --- | --- | --- | --- | --- |
| R1 | RAGFlow 无法在禁容器 / ARM64 环境落地 | 高 | §10.3 第 1、3 项不过 | 退守 L0 + L1（契约层接 Dify 内置）；把 L2 从交付项移出 |
| R2 | 双源语料不一致 | 高 | 同问不同答、报表不可归因 | 单一真源原则；双轨期影子化；入库幂等键 |
| R3 | 元数据缺失导致版本红线失效 | 中高 | 引用卡片缺版本；VC 缺失率高 | 入库时元数据先行 + 强制校验；缺元数据 chunk 降权并告警 |
| R4 | 许可 / 信创合规（ES / OpenSearch / 存储） | 中高 | 合规审查提问 | 文档引擎选 Infinity / OceanBase；缓存 Valkey / KVrocks；事前与甲方确认 |
| R5 | 引入第二套系统的运维负担 | 中 | 故障定位时间变长 | 契约层收敛（只有 3 个接缝）；`/healthz` + `/admin/stats`；运维手册增补 |
| R6 | 分数语义错配导致 RQ 失真 | 中 | RQ 突变、阈值过滤掉全部结果 | §6.3 归一化规则 + `0.0 视为不限` + 影子对比验证 |
| R7 | 范围蔓延（把 RAGFlow 的 Agent / 知识图谱全都上） | 中 | 工期被拖 | 档位制推进，每档有退出条件；P3 之后才谈增强能力 |
| R8 | 团队对两套平台都半懂 | 中 | 排障靠猜 | 契约文档化 + bridge 自测 + 影子对比报告作为交接材料 |

---

## 14. 需确认事项

| # | 事项 | 影响 | 优先级 |
| --- | --- | --- | --- |
| 1 | 真实语料形态：扫描件 / 参数表 PDF 占比多少 | 决定 L0 是否立项（§2.3） | ★★★ |
| 2 | 内网是否允许新增一台 VM（x86_64 或 ARM64）跑 RAGFlow | 决定 L1 后端能否上 RAGFlow | ★★★ |
| 3 | 是否允许容器（哪怕只在边缘节点） | 决定 L2 的形态与工期 | ★★★ |
| 4 | 是否接受 Infinity / OceanBase 作 RAGFlow 文档引擎与元数据库 | 决定合规与可行性 | ★★★ |
| 5 | 是否已有本地嵌入 / 重排模型服务（BGE 系列） | 决定质量上限与工期 | ★★☆ |
| 6 | 交付范围是否强制要求「RAGFlow 独立可交付」 | 直接决定是否启动 L2 | ★★☆ |
| 7 | 灰度分流口径（按用户组 / 按问题类型） | 决定切换风险 | ★★☆ |

---

## 附录 A　字段映射全表（Dify ↔ 统一 chunk ↔ RAGFlow）

| 统一契约字段 | Dify 外部知识库 record | RAGFlow retrieval chunk | 备注 |
| --- | --- | --- | --- |
| `content` | `content` | `content` | 唯一进入 LLM 上下文的内容 |
| `score` | `score` | `similarity` | 0~1；跨引擎只比排序 |
| `doc_no` + `version` | `title`（拼为 `HYS-CZ-002 V4.0`） | `document_id` → 元数据缓存 | C3 引用标识 |
| `section_path` | `metadata.section` | 正文首行 / 标题推断 | 也可由解析期写入 meta_fields |
| `page` | `metadata.page` | `positions[0].page_no`（结构以实测为准） | 页码坐标系需实测校准 |
| `chunk_id` | `metadata.chunk_id` | `id` | 去重与 badcase 定位 |
| `status` | `metadata.status` | 文档 meta_fields（用于过滤，不返回） | 由 bridge 注入过滤条件 |
| `image_refs` | `metadata.image_refs` | `image_id` | 多模态回链 |
| `engine` | `metadata.engine` | bridge 注入 | 报表 A/B 维度 |

## 附录 B　`kb_bridge` 请求处理伪代码

```python
def handle_retrieval(req):
    auth(req)                                   # Bearer 校验（密钥掩码日志）
    kb = lookup_kb(req.knowledge_id)            # 知识库配置：dataset_ids / 权重 / rerank
    top_k = req.retrieval_setting.top_k
    thr = req.retrieval_setting.score_threshold # 0.0 = 不限

    cond = merge_conditions(
        req.metadata_condition,                  # 调用方（可选）
        FORCED_CONDITIONS,                       # 服务端强制：status = 现行有效
        logic="and",                            # 调用方不能放宽
    )

    raw = ragflow.retrieve(
        question=req.query,
        dataset_ids=kb.dataset_ids,
        page_size=top_k,                         # Dify 的 top_k = 返回条数
        knn_top_k=max(64, top_k * 8),            # 召回池
        rerank_candidates_count=max(64, top_k * 8),
        similarity_threshold=thr or None,
        vector_similarity_weight=kb.vector_weight,
        rerank_id=kb.rerank_id,
        keyword=True,
        metadata_condition=cond,
    )

    records = []; seen = set()
    for ch in raw["data"]["chunks"]:
        if ch["id"] in seen: continue
        seen.add(ch["id"])
        meta = meta_cache.get(ch["document_id"])       # C1 契约字段
        records.append({
            "content": ch["content"],
            "score": normalize(ch["similarity"]),      # 已是 0~1，保持单调
            "title": f"{meta.doc_no} {meta.version}",   # C3 引用标识
            "metadata": {
                "page": first_page(ch.get("positions")),
                "chunk_id": ch["id"],
                "status": meta.status,
                "engine": "ragflow",
                "image_refs": ch.get("image_id") or "",
            },
        })
    return {"records": records}                     # metadata 必须为对象，不能为 null
```

## 附录 C　对现有交付物的变更清单

| 变更 | 对象 | 说明 |
| --- | --- | --- |
| 新增 | `kb_bridge/`（server / ragflow_client / mapping / meta_cache / selftest） | 接缝②，仅标准库 |
| 新增 | `ingest/`（manifest 生成 + ragflow_ingest + 冒烟） | 接缝③ |
| 新增 | `docs/09-RAGFlow与Dify融合框架方案.md`（本文档） | 框架说明与决策依据 |
| 改动 | `dsl/01-多模态智能问答-Chatflow.yml` | 「知识检索」节点由内置知识库改为**外部知识库**；提示词补齐 C3 引用标识；保留兜底分支 |
| 改动 | `tools/check_env.py` | 增加 `RAGFLOW_BASE_URL` / `RAGFLOW_API_KEY` / `RAGFLOW_DATASET_IDS` 自检与连通性探测 |
| 改动 | `eval_service/config.py` | 增加 RAGFlow 配置项（掩码输出保持一致） |
| 改动 | `eval_service/store.py` | `messages` / `evaluations` 增 `engine` 列（`SCHEMA_VERSION = 3`，沿用现有 `_migrate()` 机制） |
| 改动 | `eval_service/report.py` | 报表增加引擎维度与降级率 |
| 不动 | `collector.py` / `judge.py` / `metrics.py` / `golden_set.jsonl` | 保证 I1、I2、I3 三个不变式 |
| 不动 | `samples/` 六份语料、`tools/check_corpus.py` 校验规则 | 治理层是唯一真源，不因引擎变化而变 |

## 附录 D　参考依据（本方案已核对的上游事实）

| 事实 | 来源 |
| --- | --- |
| Dify 外部知识库协议：POST `{endpoint}/retrieval`；请求 `knowledge_id` / `query` / `retrieval_setting{top_k, score_threshold}` / 可选 `metadata_condition`；响应 `records[{content, score, title, metadata}]`；`metadata` 不得为 `null`；`score_threshold` 未启用时为 `0.0` | Dify 官方文档（自托管版《External Knowledge API》《Connect to External Knowledge Base》） |
| Dify 注册方式：知识库 → 右上角 **External Knowledge API** → 填 Endpoint 与 API Key；再创建外部知识库并填 **External Knowledge ID**；`metadata_condition` 目前无界面，仅程序可传 | 同上 |
| RAGFlow 检索接口 `POST /api/v1/retrieval`：`question` / `dataset_ids` / `page_size` / `similarity_threshold` / `vector_similarity_weight` / `knn_top_k`（`top_k` 已废弃）/ `rerank_candidates_count` / `rerank_id` / `keyword` / `metadata_condition`；响应 `chunks[{content, id, document_id, document_keyword, similarity, vector_similarity, term_similarity, positions, image_id}]` | RAGFlow 官方 HTTP API 参考（main 分支） |
| RAGFlow 元数据：文档级 `meta_fields`（写入于 Update document），检索期 `metadata_condition` 支持 `contains / not contains / start with / empty / not empty / = / ≠ / > / < / ≥ / ≤` 等算子 | 同上 |
| RAGFlow 入库接口：`POST /api/v1/datasets`、`POST /api/v1/datasets/{id}/documents`、`PUT .../documents/{id}`、**Parse documents**、`GET .../documents`；`chunk_method` + `parser_config` 决定切片 | 同上 |
| RAGFlow 依赖栈：文档引擎可选 Elasticsearch / OpenSearch / Infinity / SerenaDB / **OceanBase** / **seekdb**；元数据库 MySQL 8.0.40；缓存 Valkey / KVrocks；对象存储 `pgsty/silo`（原 MinIO）；沙箱执行器需容器 | RAGFlow `docker/docker-compose-base.yml`（main 分支） |

---

## 15. 一页话总结

> **不要问「RAGFlow 和 Dify 怎么并存」，要问「资料从进入系统到变成答案，中间哪几步该由谁负责」。**
> 解析和检索交给 RAGFlow（它读得懂、捞得准），编排和会话交给 Dify（它能编排、能收反馈），
> 治理和评价留在自己手里（这是项目的核心资产，也是唯一口径）。
> 三条接缝、三个契约、三个档位——先做 L0 验证收益，再做 L1 建立契约，L2 留给资源与时间都允许的时候。