r"""产品端到端自检：一个进程、一套数据面、两个源码内核，全链路真实跑一遍。

用法：& $PRODUCT_PYTHON platform\product\selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HERE))

from fastapi.testclient import TestClient  # noqa: E402

from product import server as product  # noqa: E402

REPO = Path(__file__).resolve().parents[2]  # D:\app\dify
# 样例文档：外部交付目录优先（开发机），缺失时回落到仓库内已入库的真实 docx；
# 两者都没有才跳过入库/问答链路（保持原有优雅降级）。
DOCX_CANDIDATES = [
    Path(r"D:\app\审价\1、385万（4号文）\交付成果\06_开发计划.docx"),
    *sorted(REPO.glob("platform/data/files/*/06_开发计划.docx")),
]
DOCX = next((path for path in DOCX_CANDIDATES if path.exists()), DOCX_CANDIDATES[0])
FAILS: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(("  [ok]   " if condition else "  [FAIL] ") + label + (" — " + str(detail) if detail else ""))
    if not condition:
        FAILS.append(label)


def ask(client, question: str):
    """跑一次问答链，返回 (答案, 引用, 评价)。"""
    answer, resources, evaluation = "", [], None
    with client.stream("POST", "/api/chat/stream", json={"query": question}) as response:
        event = "message"
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if event == "message":
                    answer += payload.get("answer") or ""
                elif event == "message_end":
                    metadata = payload.get("metadata") or {}
                    resources = metadata.get("retriever_resources") or []
                    evaluation = metadata.get("evaluation")
    return answer, resources, evaluation


def main() -> int:
    client = TestClient(product.app)
    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    check("登录", login.status_code == 200, login.text[:80])

    health = client.get("/api/health").json()
    kernels = {item["name"]: item for item in health["services"]}
    check("健康检查（内核 + 模型）", health["services"] and all(
        item["up"] for name, item in kernels.items() if name.startswith("引擎内核")),
        "；".join(f"{item['name']}={item['up']}" for item in health["services"][:5]))

    models = client.get("/api/models/providers").json()
    check("模型提供商目录", len(models["vendors"]) >= 10, f"{len(models['vendors'])} 个厂商 / 角色 " + ",".join(models["roles"]))
    test_emb = client.post("/api/models/test", json={"role": "embedding"}).json()
    check("嵌入模型连通", bool(test_emb.get("ok")), test_emb.get("detail", "")[:90])
    test_chat = client.post("/api/models/test", json={"role": "chat"}).json()
    check("对话模型连通", bool(test_chat.get("ok")), test_chat.get("detail", "")[:90])
    if product.GATEWAY.rerank_configured():
        test_rerank = client.post("/api/models/test", json={"role": "rerank"}).json()
        check("重排模型连通", bool(test_rerank.get("ok")), test_rerank.get("detail", "")[:90])
    else:
        print("  [skip] 重排角色未配置（MODEL_RERANK_* 为空）：检索走纯混合排序")

    dataset = client.post("/api/rf/datasets", json={"name": "产品自检库（可删）", "description": "端到端自检"}).json()["dataset"]
    dataset_id = dataset["id"]
    check("建知识库", bool(dataset_id), dataset_id)

    graph_status: dict = {}
    if DOCX.exists():
        with DOCX.open("rb") as handle:
            upload = client.post(f"/api/kb/datasets/{dataset_id}/upload?parse=true",
                                 files=[("files", (DOCX.name, handle.read(), "application/octet-stream"))])
        check("上传并解析", upload.status_code == 200 and upload.json()["uploaded"], upload.text[:120])
        document_id = upload.json()["uploaded"][0]["id"]
        deadline = time.time() + 900
        status = {}
        while time.time() < deadline:
            status = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]
            if status["run"] in ("DONE", "FAIL"):
                break
            time.sleep(3)
        check("解析完成并建立向量索引", status.get("run") == "DONE",
              f"run={status.get('run')} 切片={status.get('chunk_count')} 消息={str(status.get('progress_msg'))[:70]}")

        chunks = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks", params={"page_size": 5}).json()
        check("切片可查", len(chunks.get("chunks") or []) > 0, f"共 {chunks.get('total')} 片")

        retrieval = client.post("/api/rf/retrieval", json={"question": "本开发计划的文档编号是什么？",
                                                           "dataset_ids": [dataset_id], "page_size": 5}).json()
        check("检索命中", bool(retrieval.get("chunks")),
              f"命中 {retrieval.get('total')} 条 / {retrieval.get('elapsed')} ms / mode={retrieval.get('mode')} / "
              f"首条 {str((retrieval.get('chunks') or [{}])[0].get('content'))[:40]}")

        # 重排（docs/16 D53）：配了重排角色就必须真的接进检索，且能一键关掉做对照
        if product.GATEWAY.rerank_configured():
            reranked = client.post("/api/rf/retrieval", json={"question": "本开发计划的文档编号是什么？",
                                                              "dataset_ids": [dataset_id], "page_size": 5,
                                                              "use_rerank": True}).json()
            top = (reranked.get("chunks") or [{}])[0]
            check("重排接入检索（use_rerank=true）", "+rerank" in str(reranked.get("mode")),
                  f"mode={reranked.get('mode')} 重排 {reranked.get('reranked')} 条 / "
                  f"首条重排分 {top.get('rerank_score')}")
            plain = client.post("/api/rf/retrieval", json={"question": "本开发计划的文档编号是什么？",
                                                           "dataset_ids": [dataset_id], "page_size": 5,
                                                           "use_rerank": False}).json()
            check("重排可关（use_rerank=false 回纯混合）", "+rerank" not in str(plain.get("mode")),
                  f"mode={plain.get('mode')}")

        before_count = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]["chunk_count"]
        manual = client.post(f"/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks",
                             json={"content": "产品自检人工切片：代号 ZJ-PRODUCT-001。", "important_keywords": ["自检代号"]}).json()
        chunk_id = (manual.get("chunk") or {}).get("id")
        check("人工新增切片（即时向量化）", bool(chunk_id))
        after_add = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]["chunk_count"]
        check("新增切片后文档切片数 +1", after_add == before_count + 1, f"{before_count} -> {after_add}")
        if chunk_id:
            check("停用切片", client.put(f"/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}",
                                     json={"available": False}).status_code == 200)
            check("删除切片", client.request("DELETE", f"/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks",
                                        json={"chunk_ids": [chunk_id]}).status_code == 200)
            after_del = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]["chunk_count"]
            check("删除切片后文档切片数同步（回归：曾只删切片不更新 chunk_count）", after_del == before_count,
                  f"{before_count} -> {after_add} -> {after_del}")

        # 文档级 启用/禁用：禁用必须落库并让该文档退出检索（回归：曾因 update_document 忽略 enabled 而假成功）
        client.put(f"/api/rf/datasets/{dataset_id}/documents/{document_id}", json={"enabled": 0})
        disabled = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]
        check("禁用文档（状态落库）", disabled.get("status") == "0", f"status={disabled.get('status')}")
        blocked = client.post("/api/rf/retrieval", json={"question": "本开发计划的文档编号是什么？",
                                                        "dataset_ids": [dataset_id], "page_size": 5}).json()
        check("禁用文档退出检索",
              not any(item.get("document_id") == document_id for item in (blocked.get("chunks") or [])),
              f"命中 {len(blocked.get('chunks') or [])} 条")
        client.put(f"/api/rf/datasets/{dataset_id}/documents/{document_id}", json={"enabled": 1})
        enabled = client.get(f"/api/rf/datasets/{dataset_id}/documents/{document_id}").json()["document"]
        check("恢复启用文档", enabled.get("status") == "1", f"status={enabled.get('status')}")

        graph = client.post(f"/api/kb/datasets/{dataset_id}/graph/build").json()
        check("构建知识图谱", graph.get("entity_count", 0) > 0,
              f"实体 {graph.get('entity_count')} / 关系 {graph.get('relation_count')}")
        nodes = graph.get("nodes") or []
        check("图谱实体带类别", all(node.get("category") for node in nodes),
              json.dumps((graph.get("stats") or {}).get("categories") or {}, ensure_ascii=False))
        bad = [node["name"] for node in nodes if any(ch in node["name"] for ch in "_<>=(){};")]
        check("图谱已过滤代码标识", not bad, f"残留：{bad[:5]}" if bad else "无代码标识")
        check("图谱稀疏化（每点≤3 条边）", graph.get("relation_count", 0) <= len(nodes) * 3,
              f"{graph.get('relation_count')} 边 / {len(nodes)} 点")

        # 图数据库（Neo4j）：构图入库 + Cypher 查询
        graph_status = client.get("/api/graph/status").json()
        check("图数据库可达", graph_status.get("backend") == "neo4j" and graph_status.get("reachable"),
              f"{graph_status.get('version')} @ {graph_status.get('uri')}")
        db = (graph.get("graphdb") or {})
        check("构图写入图数据库", db.get("ok") and (db.get("nodes") or 0) > 0,
              f"neo4j {db.get('nodes')} 实体 / {db.get('relationships')} 关系")
        entities = client.get("/api/graph/entities", params={"dataset_id": dataset_id, "limit": 10}).json()
        check("图库实体检索（Cypher）", len(entities.get("entities") or []) > 0,
              "、".join(item["name"] for item in (entities.get("entities") or [])[:5]))
        if entities.get("entities"):
            top = entities["entities"][0]["name"]
            neighbours = client.get("/api/graph/neighbours",
                                    params={"dataset_id": dataset_id, "entity": top}).json()
            check("图库邻居查询（Cypher）", bool(neighbours.get("neighbours")),
                  f"{top} → " + "、".join(item["name"] for item in (neighbours.get("neighbours") or [])[:4]))

        # 问答链（检索 → 生成 → 引用）+ 三主观三客观
        answer, resources, evaluation = ask(client, "本开发计划的文档编号是什么？")
        check("问答链返回带引用答案", bool(answer.strip()) and bool(resources),
              f"答案 {len(answer)} 字 / 引用 {len(resources)} 条")
        objective = (evaluation or {}).get("objective") or {}
        check("命中：客观三项自动计算", all(objective.get(key) for key in ("fa", "kc", "cl")),
              json.dumps(objective, ensure_ascii=False)[:110])
        check("命中：no_hit=False", (evaluation or {}).get("no_hit") is False)

        # 未命中：主客观一致地"未检索"，并给出缺口（甲方 2026-09-27 要求）
        miss_answer_text, miss_resources, miss_eval = ask(client, "本计划规定的液压泵站溢流阀调定压力与蓄能器充氮压力分别是多少？")
        check("未命中：客观三项判不适用", bool(miss_eval) and miss_eval["no_hit"] is True
              and miss_eval["objective"]["fa"] is None,
              json.dumps((miss_eval or {}).get("objective"), ensure_ascii=False)[:110])
        check("未命中：回答明确说明未收录", ("未收录" in miss_answer_text) or ("未检索" in miss_answer_text),
              miss_answer_text[:50].replace(chr(10), " "))
        check("未命中：给出缺口要素与建议", "缺口要素" in miss_answer_text and "建议补充" in miss_answer_text)
        miss_gaps = (miss_eval or {}).get("gaps") or []
        check("未命中：回执带缺口清单", len(miss_gaps) > 0, "；".join(item["term"] for item in miss_gaps[:4]))

        # 主观三项 + 人工复核覆盖 + 汇总
        evaluations = client.get("/api/eval/evaluations").json()["evaluations"]
        if evaluations:
            target_id = evaluations[0]["message_id"]
            saved = client.post("/api/eval/receipt", json={"message_id": target_id, "conversation_id": "",
                                                           "satisfaction": 4, "usefulness": 5,
                                                           "experience": 3, "comment": "自检"}).json()
            check("主观三项落库", (saved.get("evaluation") or {}).get("subjective", {}).get("usefulness") == 5,
                  json.dumps((saved.get("evaluation") or {}).get("subjective"), ensure_ascii=False))
            overridden = client.put(f"/api/eval/dimensions/{target_id}",
                                    json={"fa": 5, "kc": 4, "cl": 4, "note": "自检人工复核"}).json()
            check("人工复核覆盖客观三项", overridden["objective"]["source"] == "human",
                  json.dumps(overridden["objective"], ensure_ascii=False)[:110])
        summary = client.get("/api/eval/summary").json()
        check("3+3 汇总（未命中单列）", summary["no_hit"] >= 1 and summary["objective"]["fa"] is not None,
              json.dumps(summary, ensure_ascii=False)[:130])
        gaps = client.get("/api/eval/gaps").json()["gaps"]
        check("覆盖缺口已登记", len(gaps) > 0, f"{len(gaps)} 项")
    else:
        print("  [skip] 样例 docx 不存在，跳过入库与问答")

    anno = client.post("/api/anno/tasks", json={"name": "产品自检标注（可删）", "dataset_id": dataset_id}).json()["task"]
    added = client.post(f"/api/anno/tasks/{anno['id']}/seed", json={"limit": 3}).json()
    if DOCX.exists():
        check("标注任务抽样", added.get("added", 0) > 0, f"导入 {added.get('added')} 条")
    else:
        print("  [skip] 样例 docx 不存在，跳过标注任务抽样（无源切片）")
    items = client.get(f"/api/anno/tasks/{anno['id']}/items", params={"status": "pending"}).json()["items"]
    if items:
        saved = client.put(f"/api/anno/items/{items[0]['id']}", json={
            "status": "done", "defect_dimensions": ["D1", "D6"], "severity": "P1",
            "completeness": "partial", "compliance": "doubtful", "comment": "自检：安全前置条件缺失"}).json()
        check("保存标注", saved.get("ok"), json.dumps(saved.get("labels"), ensure_ascii=False))
        stats = client.get(f"/api/anno/tasks/{anno['id']}/stats").json()
        check("标注统计", stats["defect_dimensions"].get("D1") == 1, json.dumps(stats["compliance"], ensure_ascii=False))
        exported = client.get(f"/api/anno/tasks/{anno['id']}/export").text.strip().splitlines()
        check("导出 JSONL", len(exported) == 1, f"{len(exported)} 行")
        defects = client.get("/api/gov/defects").json()["defects"]
        check("缺陷整改单联动（标注 + 覆盖缺口）",
              any(item["defect_id"] == items[0]["id"] for item in defects)
              and any(str(item["defect_label"]).startswith("覆盖缺口") for item in defects),
              f"{len(defects)} 条缺陷")

    # 缺陷整改：状态保存必须落库（回归：曾因 /api/gov/defects/{id}/status 只回显不落库而假成功）
    defects = client.get("/api/gov/defects").json()["defects"]
    if defects:
        target = defects[0]
        original = {"status": target["status"], "owner": target["owner"],
                    "due_date": target.get("due_date") or ""}
        client.post(f"/api/gov/defects/{target['defect_id']}/status",
                    json={"status": "整改中", "owner": "自检责任单位", "due_date": "2026-12-31"})
        again = {item["defect_id"]: item
                 for item in client.get("/api/gov/defects").json()["defects"]}
        row = again.get(target["defect_id"]) or {}
        check("缺陷状态落库",
              row.get("status") == "整改中" and row.get("owner") == "自检责任单位"
              and row.get("due_date") == "2026-12-31",
              f"status={row.get('status')} owner={row.get('owner')} due={row.get('due_date')}")
        client.post(f"/api/gov/defects/{target['defect_id']}/status", json=original)
        restored = {item["defect_id"]: item
                    for item in client.get("/api/gov/defects").json()["defects"]}
        check("缺陷状态可回滚", (restored.get(target["defect_id"]) or {}).get("status")
              == original["status"], f"status={(restored.get(target['defect_id']) or {}).get('status')}")

    apps = client.get("/api/orchestration/apps").json()["apps"]
    check("编排工作流列表", len(apps) >= 1, apps[0]["name"] if apps else "无")
    if apps:
        graph = client.get(f"/api/orchestration/apps/{apps[0]['id']}/graph").json()
        check("编排图可渲染", len(graph["nodes"]) >= 3, f"{len(graph['nodes'])} 节点 / {len(graph['edges'])} 边")
        run = client.post(f"/api/orchestration/apps/{apps[0]['id']}/run",
                          json={"inputs": {"question": "产品自检"}}).json()
        check("运行编排（Dify graphon 引擎）", run.get("status") == "succeeded",
              json.dumps(run.get("outputs"), ensure_ascii=False)[:100])

    # 诉求四：外部 Dify DSL（.yml）导入 —— 严格校验（解析 + graphon inspect 通过才落库）
    yaml_dsl = """kind: app
app:
  mode: workflow
  name: 自检导入工作流（可删）
  description: 自检专用
workflow:
  graph:
    nodes:
      - id: start
        type: custom
        data:
          type: start
          title: 开始
          variables:
            - variable: question
              label: 问题
              type: text-input
              required: true
      - id: end
        type: custom
        data:
          type: end
          title: 结束
          outputs:
            - variable: answer
              value_selector: [start, question]
    edges:
      - id: e1
        source: start
        target: end
"""
    imp = client.post("/api/orchestration/workflows/import", json={"content": yaml_dsl})
    body = imp.json()
    imported_id = (body.get("workflow") or {}).get("id")
    check("导入外部 YAML 工作流（解析 + 校验）",
          imp.status_code == 200 and bool(imported_id)
          and str((body.get("inspect") or {}).get("load_status") or "").strip().lower() == "loadable"
          and (body.get("inspect") or {}).get("mode") == "workflow",
          json.dumps(body.get("inspect") or body.get("detail"), ensure_ascii=False)[:120])
    check("导入的工作流进入应用列表",
          any(item["id"] == imported_id for item in client.get("/api/orchestration/apps").json()["apps"]),
          str(imported_id))
    bad = client.post("/api/orchestration/workflows/import",
                      json={"content": "kind: app\napp:\n  mode: chat\n  name: 自检非法DSL（可删）\n"})
    check("非法 DSL 拒收且不落库",
          bad.status_code == 422
          and not [item for item in client.get("/api/orchestration/apps").json()["apps"]
                   if item["name"] == "自检非法DSL（可删）"],
          f"HTTP {bad.status_code}")
    if imported_id:
        client.delete(f"/api/orchestration/workflows/{imported_id}")
        check("清理导入的自检工作流",
              not [item for item in client.get("/api/orchestration/apps").json()["apps"]
                   if item["id"] == imported_id])

    vram = client.get("/api/platform/vram").json()
    check("模型与显存视图", "verdict" in vram and vram["plans"], vram["verdict"][:80])

    # 进程资源管理器（Trae 进程管理器同款：进程快照 + 阈值告警 + 重启内核 + 结束进程 + 导出）
    procs = client.get("/api/platform/processes").json()
    kinds = {item["kind"] for item in procs["processes"]}
    check("进程列表（内核 / 产品服务 / 依赖服务）",
          procs["count"] == len(procs["processes"]) and {"kernel", "product"} <= kinds,
          f"{procs['count']} 个进程，归属 {sorted(kinds)}")
    check("进程列表含产品服务自身 PID",
          any(item["pid"] == os.getpid() for item in procs["processes"]), f"自身 PID={os.getpid()}")
    check("阈值与告警结构",
          "cpu_percent" in procs["thresholds"] and isinstance(procs["alerts"], list),
          f"阈值 CPU={procs['thresholds']['cpu_percent']}% / 内存={procs['thresholds']['mem_mb']}MB"
          f" · 告警 {len(procs['alerts'])} 条 · 来源 {procs['thresholds']['source']}")
    csv_body = client.get("/api/platform/processes/export", params={"format": "csv"}).text.strip().splitlines()
    check("快照导出 CSV", len(csv_body) >= 2 and csv_body[0].startswith("pid,"), f"{len(csv_body)} 行（含表头）")
    json_export = client.get("/api/platform/processes/export", params={"format": "json"}).json()
    check("快照导出 JSON", json_export.get("count") == procs["count"], f"{json_export.get('count')} 个进程")

    kernel_name = "orchestration"
    before_pid = next((item["pid"] for item in procs["processes"] if item.get("kernel") == kernel_name), None)
    restarted = client.post(f"/api/platform/kernels/{kernel_name}/restart").json()
    check("一键重启内核（新旧 PID 回显）",
          restarted.get("ok") and restarted.get("pid") is not None and restarted.get("pid") != restarted.get("old_pid"),
          f"{kernel_name} PID {restarted.get('old_pid')} → {restarted.get('pid')}（累计 {restarted.get('restarts')} 次）")
    after = client.get("/api/platform/processes").json()
    check("重启后进程表 PID 已更新",
          any(item.get("kernel") == kernel_name and item["pid"] == restarted.get("pid") for item in after["processes"]),
          f"原 PID {before_pid} → 新 PID {restarted.get('pid')}")

    check("结束进程：白名单外 PID 拒绝", client.post("/api/platform/processes/999999/kill").status_code == 403)
    check("结束进程：禁止自身进程", client.post(f"/api/platform/processes/{os.getpid()}/kill").status_code == 400)

    mem = client.post("/api/memory", json={
        "scope": "global", "title": "自检记忆（可删）",
        "content": "自检：回答需带引用编号", "tags": "自检"}).json()["memory"]
    listed = client.get("/api/memory", params={"keyword": "自检记忆"}).json()
    check("长期记忆落库", any(item["id"] == mem["id"] for item in listed["memories"]),
          f"{listed['stats']['total']} 条命中 / 全局 {listed['stats']['global']}")
    client.delete(f"/api/memory/{mem['id']}")
    check("长期记忆删除", not [item for item in client.get("/api/memory", params={"keyword": "自检记忆"}).json()["memories"]
                          if item["id"] == mem["id"]])

    settings = client.get("/api/settings").json()
    check("系统设置页数据", settings["portal"]["name"] == "装备数据治理",
          f"数据集 {settings.get('datasets')} / 模型 {settings['models']['chat']}")

    # 清理
    client.delete(f"/api/anno/tasks/{anno['id']}")
    client.request("DELETE", "/api/rf/datasets", json={"ids": [dataset_id]})
    check("清理自检数据", not [item for item in client.get("/api/rf/datasets").json()["datasets"]
                            if item["id"] == dataset_id])
    if graph_status.get("backend") == "neo4j":
        left = client.get("/api/graph/entities", params={"dataset_id": dataset_id}).json()
        check("删库级联清理图库", len(left.get("entities") or []) == 0,
              f"残留 {len(left.get('entities') or [])} 个实体")

    print()
    print("产品自检：", "全部通过" if not FAILS else ("失败：" + "、".join(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
