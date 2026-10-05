# AGENTS.md — 本项目对 AI 会话 / Agent 的硬性约束

> 本文件是**给 AI 看的入口**：任何会话（含新开会话）在动手前必须读完 `docs/16-项目基线、口径与变更纪律.md`。
> 项目根目录：`D:\app\dify`（交付树）；运行平台在 `D:\llm-stack` 与 `E:\llm-stack`（交付树 ≠ 运行树，别混）。

## 七条硬规则

1. **先读基线**：`docs/16` 是防跑偏总纲；口径唯一来源是 `docs/01`（指标体系）与 `docs/11`（设计报告 EVAL-DESIGN-011）。12 号报告是另一客户口径，不得混用。
2. **门禁前置**：任何改动前后都跑 `powershell -ExecutionPolicy Bypass -File tools\run_all_checks.ps1`，**7 个套件**必须全部 exit=0；发布工作流后再跑 `python tools\check_drift.py --online` 确认线上与本地一致。
3. **改 DSL 必同步**：改 `dsl/*.yml` 的 code / prompt / variables / outputs 后，必须同步评测服务侧契约（`judge.build_inputs`、`collector` 解析、`store` 落库列、自检用例），再执行
   `python E:\llm-stack\setup\dify_sync_dsl.py --apply` 重新发布，并用服务 API 跑通一次作为复验。
4. **禁止改测试凑绿**：测试红了先判断"实现漂移"还是"契约变更"，两种情况都要留证据与记录。
5. **口径变更要留痕**：改权重 / 阈值 / 维度 / 标签 / 契约字段，必须先改 `docs/01` 并在 `docs/16` §2、§7 登记决策，再改代码并重算历史。
6. **交付树整洁**：`D:\app\dify` 只留长期有用的文件；一次性排查脚本放 `%TEMP%` 并在结束前删除。
7. **不做无授权扩围**：不引入 OCR / 图像与视频理解 / 抽帧 / 语音转写，不容器化，除非甲方书面变更。

## 常用入口

| 目的 | 命令 / 位置 |
| --- | --- |
| 一键自检门禁（7 套件） | `tools\run_all_checks.ps1` |
| 漂移检查（文档/代码/线上） | `tools\check_drift.py`、`python tools\check_drift.py --online` |
| DSL 静态校验 / 代码节点测试 / 跨文件契约 | `tools\validate_dsl.py`、`tools\test_code_nodes.py`、`tools\test_contract.py` |
| 语料与版本治理门禁 | `tools\check_corpus.py` |
| 评测服务自检 | `eval_service\tests\selftest.py`（无凭据可跑，mock 模式） |
| 检索契约层自测 | `kb_bridge\selftest.py` |
| DSL → 线上同步（草稿 + 发布） | `E:\llm-stack\setup\dify_sync_dsl.py --apply` |
| 数据治理台（RAGFlow 全生命周期） | 门户 → http://127.0.0.1:8090/#/data-governance（开发态 5173） |
| 数据标注 / 长期记忆 | 门户 → `#/annotation`、`#/memory` |
| **完整产品（一键启动，ADR-D24）** | `启动装备数据治理.bat` 或 `python platform\serve.py` → http://127.0.0.1:8095 |
| 产品端到端自检 | `platform\product\selftest.py` |
| 图数据库（Neo4j 独立实例） | `D:\neo4j-equipment`（HTTP 7485 / Bolt 7695）；选型与麒麟适配见 `docs\17` |
| 集成产品内核层（ADR-D23） | `platform/engine_knowledge`（RAGFlow 源码）、`platform/engine_orchestration`（Dify graphon） |
| 内核自检三件套 | `platform\engine_knowledge\selftest.py`、`platform\engine_orchestration\selftest.py`、`platform\gateway\selftest.py` |
| 治理看板（Windows 本地） | `启动治理看板.bat` → http://127.0.0.1:8848/ |

## 已知平台缺陷与既定绕行

见 `docs/16` §5（SSE 只发 ping → BFF 阻塞回落；sandbox 间歇断开 → 不要随手重启 shim；思考段花括号 → 已修并有回归用例）。**不要重复踩，也不要在没登记的情况下改变绕行方式。**

## 本项目专属 Neo4j 实例（跟着项目启停）

本项目的数据图谱跑在**独立**实例上，不与其他项目共用。**要用图数据之前先把它拉起来。**

| 项 | 值 |
| --- | --- |
| 服务名 | `neo4j-equipment` |
| 入口 | Bolt `bolt://127.0.0.1:7695`、HTTP `http://127.0.0.1:7485`（Browser：http://127.0.0.1:7485/browser/） |
| home / data | `D:\neo4j-equipment` / `D:\neo4j-equipment\data` |
| 认证 | 需要 —— 账号见 `platform\config\equipment.env` 的 `NEO4J_USER` / `NEO4J_PASSWORD` |
| 启动类型 | Manual —— 开机不自启，跟着项目启停 |

```bat
D:\app\dify\neo4j-dify.bat status     rem 查状态（默认动作）
D:\app\dify\neo4j-dify.bat start      rem 幂等启动，已在跑就直接返回
D:\app\dify\neo4j-dify.bat stop       rem 用完省内存
D:\app\dify\neo4j-dify.bat console    rem 前台调试（Ctrl+C 结束）
```

**规则**

- 只操作本项目的实例（服务名 `neo4j-equipment`）；**不要** start/stop 其它项目的实例。
- `start` / `stop` 会等服务真正到达 RUNNING / STOPPED 并等 Bolt 端口开始监听才返回，可安全串联。
- 端口分配与全部实例总账：`D:\app\neo4j\notes\Neo4j工具-一项目一实例绑定表.md`（HTTP 段 7474–7485、Bolt 段 7687–7695，新项目往后顺延）。
- 需要走 `neo4j.bat` 命令行时，一律用 `D:\app\neo4j\neo4j-clean.bat` 包装：本机进程环境里有大小写重复的代理变量，会让 .NET 的进程启动直接报「已添加项。字典中的关键字」。
