# 15 · RAGFlow × Dify 链路自检与修复记录

> 记录时间：2026-09-27 下午（Windows 源码部署环境）
> 相关文档：09（融合框架与契约）、10（治理度量框架）、11（技术方案设计报告）、13（演示系统）、14（门户前端与系统设置）
> 目的：把「编排链路能不能真跑通」这件事查清楚、修掉、留下证据与复现步骤。

## 1. 自检结论

| 环节 | 自检前 | 自检后 |
| --- | --- | --- |
| Dify 三个应用 API 可达 | ✅ | ✅ |
| 语料标注工作流 | ❌ 0 token 秒失败 | ✅ succeeded（1326 token） |
| 满意度评价工作流 | ❌ 0 token 秒失败 | ✅ succeeded（3880 token，CSI 4.67 / SSI 4.33 / OSI 5.0 / 优秀） |
| 问答应用检索 | ❌ 召回 0 条，答「知识库未收录」 | ✅ 召回 6 条 RAGFlow 条款，答案引用资料编号与版本号 |
| 知识图谱 | 任务运行中（60%） | 任务运行中，节点向量化进行中（见 §6） |

## 2. 发现并修复的四个缺陷

### 2.1 工作流引用了 chatflow 专属变量 `sys.query`

- 现象：`Variable #sys.query# not found`，1.5 s 失败、0 token。
- 原因：两个 workflow 的 LLM 节点误带 chatflow 的 `memory.query_prompt_template: '{{#sys.query#}}'`；workflow 作用域内没有 `sys.query`。
- 处置：DSL 源码改为 `query_prompt_template: ''`，并通过控制台 API 同步草稿、重新发布。
- 脚本：`E:\llm-stack\setup\dify_fix_workflow_memory.py`

### 2.2 LLM 采样参数越界 `top_p: 0.9`

- 现象：`Model Parameter top_p should be greater than or equal to 0.95`（DeepSeek 插件 deepseek-0.0.24 的取值约束）。
- 处置：`top_p` 改为 `1`（与其它 DSL 一致），同步并重新发布。
- 脚本：`E:\llm-stack\setup\dify_fix_workflow_params.py`

### 2.3 Code 节点缺少沙箱服务（Windows 无法运行官方 dify-sandbox）

- 现象：`Failed to execute code … check if the sandbox service is running (WinError 10061)`。
- 原因：Dify 的 Code 节点把脚本 POST 给独立沙箱服务（`CODE_EXECUTION_ENDPOINT`，本项目为 `http://127.0.0.1:8194`）；
  官方 `dify-sandbox` 为 Go 编写的 Linux 专用程序（seccomp / landlock、`//go:build linux`），Windows 源码环境无法编译运行。
- 处置：新增最小协议垫片 `E:\llm-stack\setup\sandbox_shim.py`（实现 `POST /v1/sandbox/run`，用本机 Python 子进程执行并回传 stdout）。
  自测：正常脚本返回 `<<RESULT>>…<<RESULT>>`；异常脚本返回 `code=-1` 与 traceback；错误密钥返回 401。
- ⚠️ **该垫片没有任何隔离能力**，仅供本机开发验证；信创目标环境（ARM64 Linux）必须使用官方 dify-sandbox。
- 启动：`python sandbox_shim.py --port 8194 --api-key dify-sandbox`

**垫片自身的两个缺陷（18:39 修复）**

1. `WinError 206 文件名或扩展名太长`：最初用 `python -I -c "<脚本>"` 执行，Windows 命令行上限约 32K，
   工作流的 Code 节点脚本（100+ 行 + 输入注入）会超限。改为把脚本写入临时目录、用文件路径执行（`python -I main.py`）。
2. 启动解释器失败时异常直接抛出，Dify 侧只能等到读超时。现在统一返回结构化错误 `{"code": -1, "message": ...}`。

自测（`selftest_sandbox_shim.py`）：小脚本 `code=0`、异常脚本 `code=-1` 且带堆栈、**29.9 KB 大脚本 `code=0`**、错误密钥 401。

### 2.4 问答链路没有接通真实语料

- 现象：`/v1/chat-messages` 返回 200 但 `retriever_resources: 0`，答案走「知识库未收录」兜底。
- 原因：chatflow 的知识检索节点仍指向 DSL 里的占位数据集 `00000000-0000-0000-0000-000000000000`，而 Dify 侧一个数据集都没有；
  RAGFlow 中的 7 份资料从未被 Dify 检索。
- 处置：按 docs/09 的分工（RAGFlow 负责解析与条款级检索，Dify 负责编排），用 **Dify 外部知识库协议**把两者接起来：
  1. 门户 BFF 新增 `POST /api/ext-kb/retrieval`（Dify External Knowledge API 契约），内部桥接 RAGFlow `/api/v1/retrieval`，
     返回 `records[{content, score, title, metadata}]`，metadata 带 `doc_no / version / status / clause_no / citation`，
     并沿用**现行版本硬过滤**（`status` 含「作废」的片段不返回）。鉴权用 `PORTAL_EXT_KB_KEY`（Bearer）。
  2. Dify 控制台注册外部知识库 API（`POST /console/api/datasets/external-knowledge-api`）与外部数据集
     （`POST /console/api/datasets/external`，`external_knowledge_id` = RAGFlow 数据集 ID，
     `external_retrieval_model` = `{top_k: 6, score_threshold: 0.2, score_threshold_enabled: true}`）。
  3. 改写 chatflow 知识检索节点：`dataset_ids = [外部数据集 UUID]`，并写入
     `dataset_configs.external_knowledge_info` 与 `external_retrieval_model`，重新发布。
- 脚本：`E:\llm-stack\setup\dify_ext_kb_setup.py`（默认只查看，`--apply` 才落库）
- 新增环境变量：`PORTAL_EXT_KB_KEY`（门户侧校验）、`RAGFLOW_DATASET_ID`（默认知识库）

## 3. 验证证据

工作流（服务 API 直跑）：

```
语料标注：HTTP 200 / succeeded / 7.2 s / 1694 token（Code 节点经垫片执行）
满意度评价：HTTP 200 / succeeded / 8.3 s / 2463 token
            outputs: csi=4.25 ssi=4.33 osi=4.36 verdict=优秀 annotation_label=正确 knowledge_gap=True

注意：满意度评价应用的三个主观指标（user_satisfaction / user_usefulness / user_experience）
在并行会话中被改成 text-input，调用方需按字符串传值（`"4"` 而不是 `4`），否则返回
`400 invalid_param … must be a string`。
```

问答（`POST /v1/chat-messages`，blocking）：

```
Q：滤芯更换周期是多少工作小时？
召回 6 条（HYS-ZL-005 V1.0、HYS-WX-003 V2.0 …）
答：滤芯更换周期为每 500 工作小时或 12 个月，以先到为准 …
    并主动说明 HYS-CZ-002 V3.0 的 300 工作小时已作废、不得继续使用

Q：溢流阀 HYD-RV-04 开启压力标准值是多少？
答：17.5 MPa（二级保养时检测；调整时系统压力不得超过 18 MPa）
```

外部知识库接口自测：正确密钥返回 6 条记录、错误密钥 401、空查询返回空集（Dify 注册时用空查询探测）。

## 4. 推理前缀处理

推理型模型会把思维链写在答案开头（`<think>…</think>`）。门户在流式解析阶段丢弃这段前缀
（`web-modao/equipment-portal.vue/src/api/index.ts` 的 `filterReasoning`），界面只显示正式回答；
Dify 侧存储的原始答案不受影响，便于后续评估。若希望像 Dify 云端那样折叠展示"思考过程"，后续可把该前缀改为
单独的 SSE 字段与折叠面板。

## 5. 复现步骤（新环境）

```powershell
# 1) 沙箱垫片（Code 节点）
cd E:\llm-stack\setup
python sandbox_shim.py --port 8194 --api-key dify-sandbox

# 2) 门户 BFF（含外部知识库桥接）
cd D:\llm-stack\src\portal\bff
.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8090

# 3) 注册外部知识库并把 chatflow 节点接过去（幂等）
cd E:\llm-stack\setup
python dify_ext_kb_setup.py --apply
```

## 6. 遗留事项

1. **GraphRAG 图谱任务**仍在运行（进度 60%，17:08 时正在为 202 个节点做向量化，使用 Ollama bge-m3）；
   完成后 `GET /api/v1/datasets/{id}/knowledge_graph` 才会返回节点与边，门户「知识图谱」页面也才有真实数据。
2. **Dify 官方控制台（:3000）**仍未启动（pnpm catalog 问题），编排可视化依赖门户自研工作台。
3. **沙箱垫片**仅为 Windows 开发环境权宜；信创环境请改用官方 dify-sandbox（ARM64 构建）。
4. 语料标注工作流虽 succeeded，但本次样例抽取条数为 0（`qa_count=0`），需要用更长的正文再验证抽取质量。
