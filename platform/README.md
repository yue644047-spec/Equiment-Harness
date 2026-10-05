# 装备数据治理 · 完整产品

> 一个进程 = 一整套系统。启动即用，不依赖任何外部平台服务。
> 架构决策：docs/16 ADR-D23（源码级整合）、ADR-D24（完整产品交付）。

## 一、产品是什么

**装备数据治理** 是一套内网离线可运行的资料治理与智能问答平台，功能对标 RAGFlow 的
数据治理客户端 + Dify 的编排引擎 + 面向装备资料的质量治理与标注，并整合为一个产品：

| 能力 | 界面 |
| --- | --- |
| 数据治理台 | `#/data-governance` |
| 资料总览 | `#/knowledge` |
| 检索测试 | `#/retrieval` |
| 知识图谱 | `#/graph` |
| 智能问答 | `#/chat` |
| 智能编排 | `#/orchestration`、`#/studio` |
| 文档质量治理 | `#/scorecard`、`#/defects` |
| 数据标注 | `#/annotation` |
| 模型提供商 | `#/models` |
| 长期记忆 | `#/memory` |
| 进程资源管理器 | `#/processes` |
| 服务状态 / 系统设置 | `#/health`、`#/settings` |

## 二、怎么启动

```powershell
# 方式一：双击交付树根目录的 启动装备数据治理.bat
# 方式二：命令行
& "D:\llm-stack\src\portal\bff\.venv\Scripts\python.exe" D:\app\dify\platform\serve.py
```

```powershell
# 重排角色依赖本机重排服务（不启动则检索自动回落纯混合排序，门户不再显示「缺失项：RERANK」需它在线）
# 方式一：双击 启动重排服务.bat
# 方式二：& "E:\llm-stack\src\rerank-py\.venv\Scripts\python.exe" D:\app\dify\platform\rerank_server.py
```

打开 <http://127.0.0.1:8095> ，用 `admin / admin123` 登录（另有 `auditor / audit123`、`guest / guest123`）。

## 三、架构（源码级整合，非外部调用）

```
                     ┌──────────────────────────────┐
   浏览器 ──────────▶│ product/server.py (FastAPI)   │  前端静态托管 + 全部 API
                     │ 数据面 SQLite / 模型网关 / 检索 │
                     └───────┬──────────────┬────────┘
                             │ 子进程        │ 子进程
                 ┌───────────▼──────┐  ┌────▼─────────────────────┐
                 │ engine_knowledge │  │ engine_orchestration     │
                 │ ★ RAGFlow 源码    │  │ ★ Dify 引擎 graphon       │
                 │ rag.app / rag.nlp│  │ graphon.dsl.loads(dsl)   │
                 │ deepdoc 解析分块  │  │ → GraphEngine.run()      │
                 └──────────────────┘  └──────────────────────────┘
```

* **知识内核**：直接 import RAGFlow 源码的解析器（`rag.app.naive` 等 12 种）、分词器
  （`rag.nlp.rag_tokenizer`）与版面组件（`deepdoc`）——不启动 RAGFlow 服务、不连它的数据库。
* **编排内核**：直接驱动 Dify 1.17 自身依赖的工作流引擎 `graphon` 0.7.0，支持其全部内置节点
  （start / end / llm / knowledge-retrieval / if-else / code / template-transform / iteration …）。
* **产品自研**：数据面（SQLite + 向量）、混合检索、入库流水线、问答链、质量治理、标注、
  模型网关、显存编排与门户；两个内核只提供"引擎能力"。
* **模型**：一律 OpenAI 兼容 url + api key，配置一次，两个内核与问答链共用。
  三个角色——对话 / 嵌入 / **重排**；嵌入走本机 Ollama（`bge-m3`），重排走本机独立服务
  `rerank_server.py`（`bge-reranker-v2-m3`，OpenAI 兼容 `/v1/rerank`），二者都按
  「base_url + 模型名」接入，门户「模型接入配置」页可随时改（docs/16 D53）。

## 四、目录

| 路径 | 作用 |
| --- | --- |
| `serve.py` | 一键启动产品服务 |
| `rerank_server.py` | 本机重排服务（BGE Reranker v2 m3，OpenAI 兼容 `/v1/rerank`）；用独立 venv 跑，见 `启动重排服务.bat` |
| `config/equipment.env` | 统一配置：内核位置、模型 url/key、端口、账号、前端产物 |
| `product/store.py` | 数据面：数据集/文档/切片/向量/图谱/标注/工作流/会话/模型提供商 |
| `product/embedder.py` | 模型网关：嵌入、对话（含流式）、**重排**、连通性测试、嵌入缓存 |
| `product/ingest.py` | 入库流水线：解析 → 分词 → 嵌入 → 落库，带状态与进度 |
| `product/retriever.py` | 混合检索（向量 + 词项）+ 可选重排（放大召回→交叉编码器重排，服务不可用自动回落）+ 知识图谱构建 |
| `product/orchestrator.py` | 内置问答链 + 工作流执行（graphon） |
| `product/server.py` | 产品 HTTP 服务（门户全部接口） |
| `engine_knowledge/`、`engine_orchestration/` | 两个引擎内核（JSON-line 子进程协议） |
| `gateway/` | 内核调试网关（可选，产品不依赖） |
| `data/` | 产品数据（SQLite + 上传原件） |

## 五、自检（真实调用，不打桩）

```powershell
& "D:\llm-stack\src\portal\bff\.venv\Scripts\python.exe" platform\product\selftest.py
# 覆盖：登录/健康/模型连通/建库/上传解析/切片/检索/图谱/问答/标注/导出/缺陷联动/编排/显存/设置
```

实测基线（本机 16GB 显卡）：真实 docx 427KB → **176 切片 / 31.2s**（含向量化）；
检索 39ms 命中；问答链 81 字答案 + 6 条引用；编排 DSL 运行 succeeded。

## 六、与旧环境的关系

产品**不需要** RAGFlow 服务（9380）与 Dify 服务（5001），也不需要旧 BFF（8090）：
它们只作为"源码来源"和过渡对照。确认产品可用后可直接停掉这三个进程，
源码树留在原位置供内核 import。
