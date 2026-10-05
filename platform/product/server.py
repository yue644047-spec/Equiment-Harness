"""装备数据治理 · 产品服务（FastAPI）。

一个进程 = 一个完整产品：
  * 数据面：产品自持 SQLite（数据集/文档/切片/向量/图谱/标注/工作流/会话）
  * 知识内核：RAGFlow 源码（解析、分块、分词）
  * 编排内核：Dify 的 graphon 引擎（DSL 图执行）
  * 模型：OpenAI 兼容 url + api key（嵌入/对话），可在「模型提供商」页在线配置
  * 前端：直接托管门户构建产物（dist），一个地址打开整套系统

启动：python platform/serve.py   （默认 http://127.0.0.1:8095）
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import platform as _platform
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
if str(HOME) not in sys.path:
    sys.path.insert(0, str(HOME))

from .embedder import ROLES, ModelError, ModelGateway  # noqa: E402
from .graphdb import GraphDBError, Neo4jGraphDB  # noqa: E402
from .ingest import ingest_document, save_upload  # noqa: E402
from .kernels import ENV, health as kernel_health  # noqa: E402
from .kernels import restart as restart_kernel  # noqa: E402
from .kernels import stop as stop_kernels  # noqa: E402
from .kernels import warmup as warmup_kernels  # noqa: E402
from .kernels import workers_info as kernel_workers  # noqa: E402
from .orchestrator import (answer, ensure_default_workflow, inspect_workflow, parse_dsl,  # noqa: E402
                           run_workflow, skeleton_dsl)
from .scoring import build_gaps, coverage_miss, judge_objective, miss_answer  # noqa: E402
from .retriever import build_graph, kg_needed, retrieve  # noqa: E402
from .store import Store, new_id, now  # noqa: E402

DATA_DIR = Path(ENV.get("EQUIPMENT_DATA", HOME / "data"))
FILES_DIR = DATA_DIR / "files"
STORE = Store(DATA_DIR / "equipment.db")
GATEWAY = ModelGateway(ENV, STORE)
GRAPHDB = Neo4jGraphDB(ENV.get("NEO4J_URI", ""), ENV.get("NEO4J_USER", ""),
                      ENV.get("NEO4J_PASSWORD", ""), ENV.get("NEO4J_DATABASE", "neo4j"))
GRAPH_BACKEND = ENV.get("GRAPH_BACKEND", "sqlite")
#: 图谱启用策略：off/never 关、on/always 开、auto（默认）只对跨文档·多跳类问题启用（docs/16 D9·D15）
KG_MODE = ENV.get("KG_MODE", "auto")
#: 建图规模（可调）：原来写死 80/160 把图谱抽干成稀疏网，实测语料 2206 切片可抽 484 实体 / 508 边
GRAPH_MAX_NODES = int(ENV.get("GRAPH_MAX_NODES", "400"))
GRAPH_MAX_EDGES = int(ENV.get("GRAPH_MAX_EDGES", "800"))
GRAPH_EDGES_PER_NODE = int(ENV.get("GRAPH_EDGES_PER_NODE", "6"))
SERIALIZER = URLSafeTimedSerializer(ENV.get("PRODUCT_SECRET", "equipment-governance-secret"),
                                    salt="equipment-session")
SESSION_COOKIE = "equipment_session"
SESSION_MAX_AGE = 12 * 3600
STARTED_AT = time.time()

app = FastAPI(title="装备数据治理", version="1.0.0")
ensure_default_workflow(STORE)


def users() -> Dict[str, Dict[str, str]]:
    raw = ENV.get("PRODUCT_USERS", "admin:admin123:系统管理员:admin,auditor:audit123:质量审核员:auditor")
    out: Dict[str, Dict[str, str]] = {}
    for item in raw.split(","):
        parts = [part.strip() for part in item.split(":")]
        if len(parts) >= 2 and parts[0]:
            out[parts[0]] = {"password": parts[1], "display": parts[2] if len(parts) > 2 else parts[0],
                             "role": parts[3] if len(parts) > 3 else "user"}
    return out


def current_user(request: Request) -> Dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        data = SERIALIZER.loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=401, detail="会话已失效，请重新登录")
    return {"username": data["u"], "display": data.get("d", data["u"]), "role": data.get("r", "user")}


def current_admin(user: Dict[str, str] = Depends(current_user)) -> Dict[str, str]:
    if user.get("role") not in ("admin", "auditor"):
        raise HTTPException(status_code=403, detail="仅系统管理员或质量审核员可执行该操作")
    return user


def fail(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


# ================================================================ 鉴权

class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(payload: LoginPayload, response: Response) -> Dict[str, Any]:
    account = users().get(payload.username)
    if not account or account["password"] != payload.password:
        raise fail("用户名或密码错误", 401)
    token = SERIALIZER.dumps({"u": payload.username, "d": account["display"], "r": account["role"]})
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE)
    return {"username": payload.username, "display": account["display"], "role": account["role"]}


@app.post("/api/auth/logout")
async def logout(response: Response) -> Dict[str, str]:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def me(user: Dict[str, str] = Depends(current_user)) -> Dict[str, str]:
    return user


# ================================================================ 健康与设置

@app.get("/api/health")
async def health(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    services: List[Dict[str, Any]] = []

    def probe(name: str, fn) -> None:
        started = time.perf_counter()
        try:
            detail = fn()
            services.append({"name": name, "up": True, "status": str(detail)[:80], "error": "",
                             "latency_ms": int((time.perf_counter() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001
            services.append({"name": name, "up": False, "status": "不可用", "error": str(exc)[:200],
                             "latency_ms": int((time.perf_counter() - started) * 1000)})

    kernels = await run_in_threadpool(kernel_health)
    for name, info in kernels.items():
        services.append({"name": f"引擎内核 · {name}", "up": bool(info.get("ok")),
                         "status": "在线" if info.get("ok") else "不可用",
                         "error": (info.get("error") or "")[:200],
                         "url": (info.get("data") or {}).get("source", "")})

    probe("嵌入模型", lambda: f"{GATEWAY.resolve('embedding').get('name')} 就绪"
          if GATEWAY.resolve("embedding").get("base_url") else (_ for _ in ()).throw(RuntimeError("未配置")))
    probe("对话模型", lambda: f"{GATEWAY.resolve('chat').get('name')} 就绪"
          if GATEWAY.resolve("chat").get("base_url") else (_ for _ in ()).throw(RuntimeError("未配置")))
    probe("产品数据面", lambda: f"{len(STORE.list_datasets())} 个知识库 / {STORE.path.name}")
    if GATEWAY.rerank_configured():
        # 重排角色配了就必须真的活着：这里打一次真实的重排请求（两条短文本），
        # 服务没起 / 模型没加载都会如实显示为「不可用」，而不是只看配置有没有填。
        probe("重排模型", lambda: f"{GATEWAY.resolve('rerank').get('name')} 就绪"
              f"（{len(GATEWAY.rerank('连通性测试', ['甲', '乙']).get('results') or [])} 条打分）")

    return {"services": services, "missing_keys": [], "generated_at": now()}


@app.get("/api/settings")
async def settings(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    datasets = STORE.list_datasets()
    return {
        "portal": {
            "name": "装备数据治理",
            "version": app.version,
            "runtime": f"Python {sys.version.split()[0]} · {_platform.system()}",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT)),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "session_hours": SESSION_MAX_AGE // 3600,
            "config_source": str(HOME / "config" / "equipment.env"),
            "config_source_exists": (HOME / "config" / "equipment.env").exists(),
        },
        "viewer": {"username": user["username"], "display": user["display"], "role": user["role"],
                   "admin": user.get("role") in ("admin", "auditor")},
        "accounts": [{"username": name, "display": item["display"], "role": item["role"],
                      "current": name == user["username"]} for name, item in users().items()],
        "credentials": [{"key": role.upper(), "label": f"{role} 模型凭据",
                         "scope": config.get("provider", ""), "configured": bool(config.get("api_key")),
                         "source": "模型提供商", "fingerprint": (config.get("api_key") or "")[:6]}
                        for role, config in GATEWAY.public_config().items()],
        "upstreams": [{"name": "引擎内核 · 知识", "url": ENV.get("KNOWLEDGE_SOURCE", "")},
                      {"name": "引擎内核 · 编排", "url": "graphon（Dify 引擎）"}],
        "models": {
            "provider": GATEWAY.resolve("chat").get("provider", ""),
            "chat": GATEWAY.resolve("chat").get("name", ""),
            "chat_fast": GATEWAY.resolve("chat").get("name", ""),
            "embedding": GATEWAY.resolve("embedding").get("name", ""),
            "embedding_dim": int(GATEWAY.resolve("embedding").get("dim") or 0),
        },
        "policy": {"current_version_only": True, "retrieval_top_k": 10, "rating_scale": 5,
                   "receipt_channel": "满意度回执 /api/eval/receipt", "attribution_gate": "检索门控归因",
                   "defect_rules": "标注维度 D1~D8", "dqi_pass_line": 85, "csi_target": 4.3,
                   "closure_target": "缺陷整改闭环率 ≥ 90%"},
        "governance": {"eval_service_dir": str(DATA_DIR), "eval_db": str(STORE.path),
                       "eval_db_exists": STORE.path.exists()},
        "datasets": len(datasets),
        "generated_at": now(),
    }


# ================================================================ 知识库（门户资料页）

class DatasetPayload(BaseModel):
    name: str
    description: str = ""
    chunk_method: str = "naive"


@app.get("/api/kb/datasets")
async def kb_datasets(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"datasets": STORE.list_datasets()}


@app.post("/api/kb/datasets")
async def kb_create_dataset(payload: DatasetPayload,
                            user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    name = (payload.name or "").strip()
    if not name:
        raise fail("知识库名称不能为空")
    return {"dataset": STORE.create_dataset(name, payload.description, payload.chunk_method or "naive")}


@app.post("/api/kb/datasets/{dataset_id}/upload")
async def kb_upload(dataset_id: str, files: List[UploadFile] = File(...), parse: bool = True,
                    user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    dataset = STORE.get_dataset(dataset_id)
    if not dataset:
        raise fail("知识库不存在", 404)
    uploaded: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []
    for item in files:
        name = item.filename or "未命名文件"
        try:
            content = await item.read()
            stored = save_upload(FILES_DIR / dataset_id, name, content)
            document = STORE.add_document(dataset_id, name, str(stored), len(content))
            uploaded.append({"name": name, "id": document["id"], "size": len(content)})
        except Exception as exc:  # noqa: BLE001
            failed.append({"name": name, "error": str(exc)[:200]})
    parsed = False
    if parse and uploaded:
        for item in uploaded:
            await run_in_threadpool(ingest_document, STORE, GATEWAY, item["id"])
        parsed = True
    return {"uploaded": uploaded, "failed": failed, "parsed": parsed}


async def _graphdb_stats(dataset_id: str) -> Dict[str, Any]:
    """图库侧统计（不可达时返回 ok=False，不影响页面其余部分）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        return {"backend": "sqlite", "ok": True}
    try:
        stats = await run_in_threadpool(GRAPHDB.stats, dataset_id)
        return {"backend": "neo4j", "ok": True, **stats}
    except GraphDBError as exc:
        return {"backend": "neo4j", "ok": False, "error": str(exc)[:200]}


@app.get("/api/graph/status")
async def graph_status(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """图数据库状态（页面顶部展示）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        return {"backend": "sqlite", "reachable": True, "detail": "本地 SQLite 图（未启用 Neo4j）"}
    health = await run_in_threadpool(GRAPHDB.health)
    totals = {"nodes": 0, "relationships": 0}
    composition: Dict[str, Any] = {"by_label": [], "by_relationship_type": []}
    if health.get("reachable"):
        try:
            datasets = STORE.list_datasets()
            for dataset in datasets:
                stats = await run_in_threadpool(GRAPHDB.stats, dataset["id"])
                totals["nodes"] += int(stats.get("nodes") or 0)
                totals["relationships"] += int(stats.get("relationships") or 0)
            active = STORE.list_datasets()
            if active:
                composition = await run_in_threadpool(GRAPHDB.composition, active[0]["id"])
        except GraphDBError:
            pass
    return {"backend": "neo4j", **health, **totals, "composition": composition}


@app.get("/api/graph/model")
async def graph_model(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """图模型自述（Neo4j 原生）：标签 / 关系类型 / 约束 / 索引。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        return {"backend": "sqlite", "labels": [], "relationship_types": [],
                "constraints": [], "indexes": [], "detail": "未启用 Neo4j"}
    try:
        model = await run_in_threadpool(GRAPHDB.model)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", **model}


@app.get("/api/graph/versions")
async def graph_versions(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """资料—版本—状态：用关系类型（CURRENT_VERSION / OBSOLETE）一次查清版本陷阱。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        return {"backend": "sqlite", "rows": []}
    try:
        rows = await run_in_threadpool(GRAPHDB.version_board, dataset_id)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", "rows": rows}


@app.get("/api/graph/central")
async def graph_central(dataset_id: str, limit: int = 20,
                        user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """度中心性排名（Cypher 现场算，不依赖 GDS 插件）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        return {"backend": "sqlite", "entities": []}
    try:
        rows = await run_in_threadpool(GRAPHDB.central_entities, dataset_id, limit)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", "entities": rows}


@app.get("/api/graph/entities")
async def graph_entities(dataset_id: str, q: str = "", limit: int = 50,
                         user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """在图库里按名称检索实体（Cypher 查询，返回度数与权重）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        nodes = STORE.get_graph(dataset_id)["nodes"]
        return {"backend": "sqlite", "entities": [{"id": n["id"], "name": n["name"],
                                                   "category": n.get("category"),
                                                   "weight": n["weight"], "degree": None}
                                                  for n in nodes if not q or q in n["name"]][:limit]}
    try:
        rows = await run_in_threadpool(GRAPHDB.search_entities, dataset_id, q, limit)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", "entities": rows}


@app.get("/api/graph/neighbours")
async def graph_neighbours(dataset_id: str, entity: str, limit: int = 30,
                           user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """某实体的邻居（关系 + 共现强度）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        graph = STORE.get_graph(dataset_id)
        rows = [{"name": e["target"] if e["source"] == entity else e["source"],
                 "relation": e["relation"], "weight": e.get("weight")}
                for e in graph["edges"] if entity in (e["source"], e["target"])]
        return {"backend": "sqlite", "neighbours": rows[:limit]}
    try:
        rows = await run_in_threadpool(GRAPHDB.neighbours, dataset_id, entity, limit)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", "neighbours": rows}


@app.get("/api/graph/path")
async def graph_path(dataset_id: str, left: str, right: str,
                     user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """两个实体之间的最短路径（跨资料关联查询）。"""
    if GRAPH_BACKEND != "neo4j" or not GRAPHDB.enabled:
        raise fail("最短路径查询需要图数据库（当前为本地 SQLite 图）", 501)
    try:
        rows = await run_in_threadpool(GRAPHDB.path_between, dataset_id, left, right)
    except GraphDBError as exc:
        raise fail(str(exc), 502)
    return {"backend": "neo4j", "paths": rows}


@app.get("/api/kb/datasets/{dataset_id}/documents")
async def kb_documents(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"data": {"docs": [_portal_document(item) for item in STORE.list_documents(dataset_id)]}}


@app.get("/api/kb/datasets/{dataset_id}/documents/{document_id}/chunks")
async def kb_chunks(dataset_id: str, document_id: str,
                    user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    chunks = STORE.list_chunks(dataset_id=dataset_id, document_id=document_id, limit=500)
    return {"data": {"chunks": [{"content": item["content"], "clause": None,
                                 "status": "effective" if item["available"] else "disabled",
                                 "tokens": len((item.get("tokens") or "").split())} for item in chunks]}}


def _portal_document(item: Dict[str, Any]) -> Dict[str, Any]:
    meta = item.get("meta_fields") or {}
    return {"id": item["id"], "name": item["name"], "size": item["size"],
            "chunk_count": item["chunk_count"], "run": item["status"], "create_date": item["created_at"],
            "version": meta.get("version") or "", "doc_no": meta.get("doc_no") or ""}


class RetrievalPayload(BaseModel):
    question: str
    dataset_ids: List[str]
    top_k: int = 10
    similarity_threshold: float = 0.2
    vector_weight: float = 0.3
    include_obsolete: bool = False
    use_kg: Optional[bool] = None
    use_rerank: Optional[bool] = None
    keyword: bool = False


@app.post("/api/kb/retrieval-test")
async def kb_retrieval_test(payload: RetrievalPayload,
                            user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    try:
        use_kg = kg_needed(payload.question, KG_MODE) if payload.use_kg is None else bool(payload.use_kg)
        result = await run_in_threadpool(
            retrieve, STORE, GATEWAY, payload.question, payload.dataset_ids, payload.top_k,
            payload.similarity_threshold, payload.vector_weight, use_kg, None, payload.use_rerank)
    except (ModelError, ValueError) as exc:
        raise fail(str(exc))
    chunks = [{"content": item["content"], "score": item["similarity"],
               "rerank_score": item.get("rerank_score"),
               "citation": " ".join(part for part in [item.get("doc_no"), item.get("version")] if part)
                           or item["document_name"],
               "status": "effective", "doc_no": item.get("doc_no") or "",
               "version": item.get("version") or ""} for item in result["chunks"]]
    return {"total": result["total"], "chunks": chunks, "blocked": [], "blocked_total": 0,
            "elapsed_ms": result["elapsed_ms"], "mode": result["mode"],
            "reranked": result.get("reranked", 0)}


@app.get("/api/kb/datasets/{dataset_id}/graph")
async def kb_graph(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """图谱数据面：**Neo4j 在场就直接用 Cypher 读图**，读不到再回落到本地 SQLite 图。

    返回结构对前端保持不变（nodes/edges/stats），额外给出 data_source 与边的关系类型。
    """
    categories: Dict[str, int] = {}
    communities: Dict[str, int] = {}
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    data_source = "sqlite"

    if GRAPH_BACKEND == "neo4j" and GRAPHDB.enabled:
        try:
            payload = await run_in_threadpool(GRAPHDB.graph_payload, dataset_id)
            for item in payload.get("nodes") or []:
                category = item.get("category") or "术语"
                community = item.get("community") or ""
                categories[category] = categories.get(category, 0) + 1
                if community:
                    communities[community] = communities.get(community, 0) + 1
                nodes.append({"id": item["id"], "name": item["name"], "category": category,
                              "community": community, "value": item.get("value") or 0,
                              "degree": item.get("degree")})
            for item in payload.get("edges") or []:
                edges.append({"source": item["source"], "target": item["target"],
                              "relation": item.get("relation"), "type": item.get("type"),
                              "weight": item.get("weight")})
            if nodes:
                data_source = "neo4j"
        except GraphDBError:
            nodes, edges, categories, communities = [], [], {}, {}

    if data_source == "sqlite":
        graph = STORE.get_graph(dataset_id)
        for node in graph["nodes"]:
            category = node.get("category") or "术语"
            community = node.get("community") or ""
            categories[category] = categories.get(category, 0) + 1
            if community:
                communities[community] = communities.get(community, 0) + 1
            nodes.append({"id": node["id"], "name": node["name"], "category": category,
                          "community": community, "value": node["weight"]})
        edges = [{"source": edge["source"], "target": edge["target"], "relation": edge["relation"],
                  "weight": edge.get("weight")} for edge in graph["edges"]]

    database = await _graphdb_stats(dataset_id)
    return {"nodes": nodes, "edges": edges,
            "entity_count": len(nodes), "relation_count": len(edges),
            "stats": {"categories": categories, "communities": communities, "chunks": 0, "noise_filtered": 0},
            "graph_backend": GRAPH_BACKEND, "data_source": data_source,
            "graphdb": database,
            # 门户"最近构建时间"卡片：Neo4j 的 (:Dataset).updated_at；回落 SQLite 时没有时间戳。
            "built_at": (database or {}).get("built_at") or "",
            "status": "ready" if nodes else "empty"}


@app.post("/api/kb/datasets/{dataset_id}/graph/build")
async def kb_graph_build(dataset_id: str,
                         user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    backend = GRAPHDB if GRAPH_BACKEND == "neo4j" else None
    result = await run_in_threadpool(build_graph, STORE, dataset_id, GRAPH_MAX_NODES,
                                     GRAPH_MAX_EDGES, backend, GATEWAY, GRAPH_EDGES_PER_NODE)
    return {"nodes": [{"id": node["id"], "name": node["name"],
                       "category": node.get("category") or "术语",
                       "value": node.get("value") or node.get("weight") or 0,
                       "community": node.get("community") or "",
                       "aliases": node.get("aliases") or [],
                       "coverage": node.get("coverage"), "occurrences": node.get("occurrences")}
                      for node in result["nodes"]],
            "edges": result["edges"], "entity_count": result["entity_count"],
            "relation_count": result["relation_count"], "stats": result.get("stats") or {},
            "graphdb": result.get("graphdb"), "graph_backend": GRAPH_BACKEND,
            # 与 GET /graph 契约保持一致：门户"最近构建时间"卡片直接读顶层 built_at，
            # 缺了它会导致刚构建完仍显示"未构建"（实测 2026-09-28）。
            "built_at": (result.get("graphdb") or {}).get("built_at") or "",
            "status": "ready"}


# ================================================================ 智能问答

class ChatPayload(BaseModel):
    query: str
    conversation_id: str = ""
    inputs: Dict[str, Any] = Field(default_factory=dict)
    files: List[Any] = Field(default_factory=list)


@app.post("/api/chat/stream")
async def chat_stream(payload: ChatPayload, user: Dict[str, str] = Depends(current_user)):
    question = (payload.query or "").strip()
    if not question:
        raise fail("问题不能为空")
    dataset_rows = STORE.list_datasets()
    datasets = [item["id"] for item in dataset_rows]
    if not datasets:
        raise fail("还没有知识库：请先在「装备数据治理」里创建知识库并上传资料")
    # 长期记忆按「项目」生效：项目记忆只在同名知识库参与检索时注入（全局记忆始终生效）
    project_names = [str(item["name"]) for item in dataset_rows]

    async def generator():
        conversation_id = payload.conversation_id or new_id("conv")
        STORE.ensure_conversation(conversation_id, user["username"], question)
        STORE.add_message(conversation_id, "user", question)
        full = ""
        gaps: List[Dict[str, Any]] = []
        try:
            hits, result, stream = await run_in_threadpool(
                answer, STORE, GATEWAY, question, datasets, 10, 0.2, 0.3,
                kg_needed(question, KG_MODE), True,
                project_names=project_names)
        except (ModelError, ValueError) as exc:
            yield "event: error\ndata: " + json.dumps({"message": str(exc)}, ensure_ascii=False) + "\n\n"
            return

        # 未命中：不给"看起来正常"的答案，直接说明没检索到 + 告诉用户资料缺什么（ADR-D25）
        # 检索器总会返回 top_k 条，所以用「要素覆盖率」判定未命中，而不是只看 hits 是否为空
        coverage = await run_in_threadpool(coverage_miss, question, hits)
        if coverage["miss"]:
            gaps = await run_in_threadpool(build_gaps, STORE, datasets, question, hits, coverage["reason"])
            dataset_names = [item["name"] for item in STORE.list_datasets()]
            full = miss_answer(question, gaps, dataset_names)
            chunk = max(40, len(full) // 6)
            for start in range(0, len(full), chunk):
                yield "event: message\ndata: " + json.dumps(
                    {"answer": full[start:start + chunk]}, ensure_ascii=False) + "\n\n"
            message = STORE.add_message(conversation_id, "assistant", full, [])
            STORE.save_objective(message["id"], None, None, None, True,
                                 "本次未命中资料（" + coverage["reason"] + "），客观三项不适用（不参与均值）",
                                 "auto", gaps)
            for gap in gaps:
                STORE.add_gap(gap.get("dataset_id") or (datasets[0] if datasets else ""), question,
                              [gap["term"]], gap["missing"], gap["suggestion"], gap.get("dimension") or "D1",
                              message["id"])
            yield "event: message_end\ndata: " + json.dumps({
                "message_id": message["id"], "conversation_id": conversation_id,
                "metadata": {"retriever_resources": [],
                             "retrieval": {"mode": result.get("mode"), "elapsed_ms": result.get("elapsed_ms"),
                                           "coverage": coverage},
                             "evaluation": {"no_hit": True, "objective": {"fa": None, "kc": None, "cl": None,
                                            "source": "auto",
                                            "reason": "本次未检索到任何资料片段，客观三项不适用"},
                                            "subjective": {"satisfaction": None, "usefulness": None,
                                                           "experience": None},
                                            "gaps": gaps}},
            }, ensure_ascii=False) + "\n\n"
            return

        try:
            while True:
                delta = await run_in_threadpool(next, stream, None)
                if delta is None:
                    break
                full += delta
                yield "event: message\ndata: " + json.dumps({"answer": delta}, ensure_ascii=False) + "\n\n"
        except StopIteration:
            pass
        except ModelError as exc:
            yield "event: error\ndata: " + json.dumps({"message": str(exc)}, ensure_ascii=False) + "\n\n"
            return

        resources = [{"document_name": item["document_name"], "score": item["similarity"],
                      "content": item["content"][:1200], "document_id": item["document_id"],
                      "metadata": item.get("metadata") or {}} for item in hits[:6]]
        message = STORE.add_message(conversation_id, "assistant", full, resources)

        # 客观三项：系统自动算（人工可在门户复核覆盖）；KC 偏低时同时记入覆盖缺口
        objective = await run_in_threadpool(judge_objective, GATEWAY, question, full, hits)
        if objective.get("kc") is not None and int(objective["kc"]) <= 2:
            gaps = await run_in_threadpool(build_gaps, STORE, datasets, question, hits,
                                           "命中片段未覆盖问题要点（KC=" + str(objective["kc"]) + "）")
            for gap in gaps:
                STORE.add_gap(datasets[0] if datasets else "", question, [gap["term"]],
                              gap["missing"], gap["suggestion"], gap.get("dimension") or "D1", message["id"])
        STORE.save_objective(message["id"], objective.get("fa"), objective.get("kc"),
                             objective.get("cl"), bool(objective.get("no_hit")),
                             str(objective.get("reason") or ""), str(objective.get("source") or "auto"),
                             gaps)
        yield "event: message_end\ndata: " + json.dumps({
            "message_id": message["id"], "conversation_id": conversation_id,
            "metadata": {"retriever_resources": resources,
                         "retrieval": {"mode": result.get("mode"), "elapsed_ms": result.get("elapsed_ms")},
                         "evaluation": {"no_hit": bool(objective.get("no_hit")),
                                        "objective": {"fa": objective.get("fa"), "kc": objective.get("kc"),
                                                      "cl": objective.get("cl"),
                                                      "source": objective.get("source"),
                                                      "reason": objective.get("reason")},
                                        "subjective": {"satisfaction": None, "usefulness": None,
                                                       "experience": None},
                                        "gaps": gaps}},
        }, ensure_ascii=False) + "\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/conversations")
async def conversations(limit: int = 30, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"data": STORE.list_conversations(user["username"], limit)}


@app.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, limit: int = 50,
                                user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"data": STORE.list_messages(conversation_id, limit)}


class FeedbackPayload(BaseModel):
    rating: str
    content: str = ""


@app.post("/api/messages/{message_id}/feedback")
async def message_feedback(message_id: str, payload: FeedbackPayload,
                           user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    STORE.set_feedback(message_id, payload.rating)
    return {"ok": True}


class ReceiptPayload(BaseModel):
    """门户回执：主观三项（满意度 / 有用性 / 体验感），1–5；缺项留空不补默认值（docs/01）。"""

    satisfaction: Optional[int] = None
    usefulness: Optional[int] = None
    experience: Optional[int] = None
    comment: str = ""
    conversation_id: str = ""
    message_id: str = ""
    # 兼容旧版单分回执
    score: Optional[float] = None
    suggestion: str = ""


@app.post("/api/eval/receipt")
async def eval_receipt(payload: ReceiptPayload, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    satisfaction = payload.satisfaction if payload.satisfaction is not None else (
        int(payload.score) if payload.score is not None else None)
    comment = payload.comment or payload.suggestion or ""
    if payload.message_id:
        STORE.save_subjective(payload.message_id, satisfaction, payload.usefulness, payload.experience, comment)
    STORE.add_receipt(payload.conversation_id, payload.message_id, float(satisfaction or 0), comment)
    view = None
    if payload.message_id:
        record = STORE.get_evaluation(payload.message_id)
        view = _evaluation_view(record) if record else None
    return {"ok": True, "evaluation": view, "summary": STORE.evaluation_summary()}


class ObjectiveOverride(BaseModel):
    """人工复核覆盖自动客观三项（ADR-D25：自动先算、人工可覆盖）。"""

    fa: Optional[int] = None
    kc: Optional[int] = None
    cl: Optional[int] = None
    note: str = ""


@app.get("/api/eval/dimensions/{message_id}")
async def eval_dimensions(message_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """取一条回答的 3 主观 + 3 客观（含未命中状态、判据与覆盖缺口）。"""
    item = STORE.get_evaluation(message_id)
    if not item:
        raise fail("评价记录不存在", 404)
    return _evaluation_view(item)


@app.put("/api/eval/dimensions/{message_id}")
async def eval_override(message_id: str, payload: ObjectiveOverride,
                        user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    """人工复核覆盖客观三项，覆盖后 obj_source=human，原判据与复核人一并留痕。"""
    item = STORE.get_evaluation(message_id)
    if not item:
        raise fail("评价记录不存在", 404)
    STORE.override_objective(message_id, payload.fa, payload.kc, payload.cl, user["display"], payload.note)
    return _evaluation_view(STORE.get_evaluation(message_id) or {})


@app.get("/api/eval/summary")
async def eval_summary(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """3 主观 + 3 客观汇总；未命中单独计数，不混入均值。"""
    return STORE.evaluation_summary()


@app.get("/api/eval/evaluations")
async def eval_list(limit: int = 100, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"evaluations": [_evaluation_view(item) for item in STORE.list_evaluations(limit)]}


@app.get("/api/eval/gaps")
async def eval_gaps(status: str = "", user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """覆盖缺口清单（"这份资料缺了什么"）。"""
    return {"gaps": STORE.list_gaps(status)}


class GapStatusPayload(BaseModel):
    status: str
    owner: str = ""


@app.post("/api/eval/gaps/{gap_id}/status")
async def eval_gap_status(gap_id: str, payload: GapStatusPayload,
                          user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.update_gap(gap_id, payload.status, payload.owner)
    return {"ok": True}


def _evaluation_view(item: Dict[str, Any]) -> Dict[str, Any]:
    """统一的评价视图：主观三项 + 客观三项 + 未命中标记 + 缺口。"""
    return {
        "message_id": item.get("id"),
        "conversation_id": item.get("conversation_id"),
        "question": "",
        "answer": item.get("content"),
        "created_at": item.get("created_at"),
        "no_hit": bool(item.get("no_hit")),
        "subjective": {
            "satisfaction": item.get("us_satisfaction"),
            "usefulness": item.get("us_usefulness"),
            "experience": item.get("us_experience"),
        },
        "objective": {
            "fa": item.get("obj_fa"), "kc": item.get("obj_kc"), "cl": item.get("obj_cl"),
            "source": item.get("obj_source") or "",
            "reason": item.get("obj_reason") or "",
            "applicable": not bool(item.get("no_hit")),
        },
        "review": {"by": item.get("review_by") or "", "at": item.get("review_at") or "",
                   "note": item.get("review_note") or ""},
        "gaps": item.get("gaps") or [],
    }


# ================================================================ 数据治理台（/api/rf）

class DatasetPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    chunk_method: Optional[str] = None
    parser_config: Optional[Dict[str, Any]] = None
    embedding_model: Optional[str] = None


class IdsPayload(BaseModel):
    ids: List[str] = Field(default_factory=list)


class DocumentIds(BaseModel):
    document_ids: List[str] = Field(default_factory=list)


class DocumentPatch(BaseModel):
    name: Optional[str] = None
    meta_fields: Optional[Dict[str, Any]] = None
    enabled: Optional[int] = None
    chunk_method: Optional[str] = None


class ChunkCreate(BaseModel):
    content: str
    important_keywords: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)


class ChunkIds(BaseModel):
    chunk_ids: List[str] = Field(default_factory=list)


@app.get("/api/rf/system")
async def rf_system(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    started = time.perf_counter()
    datasets = STORE.list_datasets()
    return {"base": "产品内置知识引擎（RAGFlow 源码内核）", "reachable": True,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "dataset_count": len(datasets),
            "document_count": sum(item["document_count"] for item in datasets),
            "chunk_count": sum(item["chunk_count"] for item in datasets),
            "default_embedding_model": GATEWAY.resolve("embedding").get("name", ""),
            "datasets": [{"id": item["id"], "name": item["name"],
                          "document_count": item["document_count"], "chunk_count": item["chunk_count"],
                          "embedding_model": GATEWAY.resolve("embedding").get("name", ""),
                          "chunk_method": item["chunk_method"]} for item in datasets]}


@app.get("/api/rf/datasets")
async def rf_datasets(name: str = "", user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    items = STORE.list_datasets(name)
    for item in items:
        item["embedding_model"] = GATEWAY.resolve("embedding").get("name", "")
        item["create_date"] = item["created_at"]
        item["update_date"] = item["updated_at"]
    return {"datasets": items}


@app.post("/api/rf/datasets")
async def rf_dataset_create(payload: DatasetPayload,
                            user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    return await kb_create_dataset(payload, user)


@app.get("/api/rf/datasets/{dataset_id}")
async def rf_dataset_detail(dataset_id: str,
                            user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    item = STORE.get_dataset(dataset_id)
    if not item:
        raise fail("知识库不存在", 404)
    item["embedding_model"] = GATEWAY.resolve("embedding").get("name", "")
    item["create_date"] = item["created_at"]
    item["update_date"] = item["updated_at"]
    return {"dataset": item}


@app.put("/api/rf/datasets/{dataset_id}")
async def rf_dataset_update(dataset_id: str, payload: DatasetPatch,
                            user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not patch:
        raise fail("没有需要修改的字段")
    STORE.update_dataset(dataset_id, patch)
    return await rf_dataset_detail(dataset_id, user)


@app.delete("/api/rf/datasets")
async def rf_dataset_delete(payload: IdsPayload,
                            user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.ids:
        raise fail("请至少选择一个知识库")
    STORE.delete_datasets(payload.ids)
    if GRAPH_BACKEND == "neo4j" and GRAPHDB.enabled:
        for dataset_id in payload.ids:
            try:
                await run_in_threadpool(GRAPHDB.delete_dataset, dataset_id)
            except GraphDBError:
                pass
    return {"ok": True, "deleted": payload.ids}


@app.get("/api/rf/datasets/{dataset_id}/documents")
async def rf_documents(dataset_id: str, keywords: str = "", page: int = 1, page_size: int = 200,
                       user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    items = STORE.list_documents(dataset_id, keywords)
    docs = []
    for item in items:
        docs.append({"id": item["id"], "name": item["name"], "size": item["size"],
                     "chunk_count": item["chunk_count"], "run": item["status"],
                     "progress": item["progress"], "progress_msg": item["message"],
                     "status": "1" if item.get("enabled", 1) else "0", "chunk_method": item["chunk_method"],
                     "meta_fields": item["meta_fields"], "create_date": item["created_at"],
                     "update_date": item["updated_at"]})
    return {"docs": docs, "total": len(docs)}


@app.post("/api/rf/datasets/{dataset_id}/documents")
async def rf_document_upload(dataset_id: str, files: List[UploadFile] = File(...), parse: bool = False,
                             user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    result = await kb_upload(dataset_id, files, parse, user)
    docs = [{"id": item["id"], "name": item["name"]} for item in result["uploaded"]]
    return {"documents": docs, "parsed": result["parsed"], "failed": result["failed"]}


@app.get("/api/rf/datasets/{dataset_id}/documents/{document_id}")
async def rf_document_detail(dataset_id: str, document_id: str,
                             user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    item = STORE.get_document(document_id)
    if not item:
        raise fail("文档不存在", 404)
    return {"document": {"id": item["id"], "name": item["name"], "chunk_count": item["chunk_count"],
                         "run": item["status"], "progress": item["progress"],
                         "progress_msg": item["message"], "meta_fields": item["meta_fields"],
                         "size": item["size"], "status": "1" if item.get("enabled", 1) else "0"}}


@app.put("/api/rf/datasets/{dataset_id}/documents/{document_id}")
async def rf_document_update(dataset_id: str, document_id: str, payload: DocumentPatch,
                             user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    patch = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not patch:
        raise fail("没有需要修改的字段")
    STORE.update_document(document_id, patch)
    return {"ok": True}


@app.delete("/api/rf/datasets/{dataset_id}/documents")
async def rf_document_delete(dataset_id: str, payload: DocumentIds,
                             user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.document_ids:
        raise fail("请至少选择一份文档")
    STORE.delete_documents(payload.document_ids)
    return {"ok": True}


@app.post("/api/rf/datasets/{dataset_id}/parse")
async def rf_parse(dataset_id: str, payload: DocumentIds,
                   user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.document_ids:
        raise fail("请至少选择一份文档")
    results = []
    for document_id in payload.document_ids:
        result = await run_in_threadpool(ingest_document, STORE, GATEWAY, document_id)
        results.append(result)
    return {"ok": True, "results": results}


@app.delete("/api/rf/datasets/{dataset_id}/parse")
async def rf_parse_stop(dataset_id: str, payload: DocumentIds,
                        user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    for document_id in payload.document_ids:
        STORE.update_document(document_id, {"status": "CANCEL", "message": "用户取消"})
    return {"ok": True}


@app.get("/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks")
async def rf_chunks(dataset_id: str, document_id: str, keywords: str = "", page: int = 1,
                    page_size: int = 200, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    items = STORE.list_chunks(dataset_id=dataset_id, document_id=document_id,
                              keywords=keywords, limit=page_size, offset=(page - 1) * page_size)
    return {"chunks": [{"id": item["id"], "content": item["content"], "document_id": item["document_id"],
                        "important_keywords": item["important_keywords"], "questions": [],
                        "available": item["available"]} for item in items],
            "total": len(items)}


@app.post("/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks")
async def rf_chunk_add(dataset_id: str, document_id: str, payload: ChunkCreate,
                       user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.content.strip():
        raise fail("切片内容不能为空")
    try:
        vector = await run_in_threadpool(GATEWAY.embed_one, payload.content)
    except ModelError as exc:
        vector = []
        if not vector:
            raise fail(str(exc))
    from . import kernels as _kernels
    tokens = (await run_in_threadpool(_kernels.tokenize_batch, [payload.content]))[0]
    chunk = STORE.add_chunk(dataset_id, document_id, payload.content, vector,
                            payload.important_keywords, tokens)
    return {"chunk": {"id": chunk["id"], "content": chunk["content"]}}


@app.put("/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}")
async def rf_chunk_update(dataset_id: str, document_id: str, chunk_id: str, payload: Dict[str, Any],
                          user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    patch: Dict[str, Any] = {}
    if "content" in payload:
        patch["content"] = payload["content"]
        try:
            patch["embedding"] = await run_in_threadpool(GATEWAY.embed_one, payload["content"])
        except ModelError as exc:
            raise fail(str(exc))
    if "available" in payload:
        patch["available"] = bool(payload["available"])
    if "important_keywords" in payload:
        patch["important_keywords"] = payload["important_keywords"]
    if not patch:
        raise fail("没有需要修改的字段")
    STORE.update_chunk(chunk_id, patch)
    return {"ok": True}


@app.delete("/api/rf/datasets/{dataset_id}/documents/{document_id}/chunks")
async def rf_chunk_delete(dataset_id: str, document_id: str, payload: ChunkIds,
                          user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    ids = payload.chunk_ids or [item["id"] for item in
                               STORE.list_chunks(document_id=document_id, limit=10000)]
    STORE.delete_chunks(ids)
    return {"ok": True, "deleted": len(ids)}


class RfRetrieval(BaseModel):
    question: str
    dataset_ids: List[str]
    top_k: int = 10
    page_size: int = 10
    similarity_threshold: float = 0.2
    vector_similarity_weight: float = 0.3
    keyword: bool = False
    highlight: bool = False
    use_kg: bool = False
    use_rerank: Optional[bool] = None


@app.post("/api/rf/retrieval")
async def rf_retrieval(payload: RfRetrieval, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    if not payload.question.strip():
        raise fail("问题不能为空")
    if not payload.dataset_ids:
        raise fail("请至少选择一个知识库")
    try:
        result = await run_in_threadpool(
            retrieve, STORE, GATEWAY, payload.question, payload.dataset_ids,
            payload.page_size or payload.top_k, payload.similarity_threshold,
            payload.vector_similarity_weight, payload.use_kg, None, payload.use_rerank)
    except (ModelError, ValueError) as exc:
        raise fail(str(exc))
    return {"total": result["total"],
            "chunks": [{"id": item["id"], "content": item["content"],
                        "document_id": item["document_id"], "document_name": item["document_name"],
                        "dataset_id": item["dataset_id"], "similarity": item["similarity"],
                        "vector_similarity": item["vector_similarity"],
                        "term_similarity": item["term_similarity"],
                        "rerank_score": item.get("rerank_score"),
                        "highlight": _highlight(item["content"], payload.question) if payload.highlight else "",
                        "important_keywords": item["important_keywords"]} for item in result["chunks"]],
            "elapsed": result["elapsed_ms"], "mode": result.get("mode"),
            "reranked": result.get("reranked", 0)}


def _highlight(content: str, question: str, window: int = 60) -> str:
    import re
    for term in re.findall(r"[0-9A-Za-z_]+|[\u4e00-\u9fff]{2,}", question or ""):
        index = content.find(term)
        if index >= 0:
            start = max(0, index - window)
            snippet = content[start:index + len(term) + window]
            escaped = snippet.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return escaped.replace(term, "<em>" + term + "</em>")
    escaped = content[:200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped


async def _graph_scale(dataset_id: str) -> Dict[str, int]:
    """图谱规模（权威计数）：Neo4j 在场用图库的 count，否则用 SQLite 的 COUNT。

    页面上的"实体/关系"必须用它，而不是返回数组的长度——返回体里的边是带 LIMIT 的，
    拿长度当规模会少报（2026-09-28 实测：治理台显示"关系 128"，图谱页显示"关系 280"）。
    """
    if GRAPH_BACKEND == "neo4j" and GRAPHDB.enabled:
        try:
            stats = await run_in_threadpool(GRAPHDB.stats, dataset_id)
            return {"node_count": int(stats.get("nodes") or 0),
                    "edge_count": int(stats.get("relationships") or 0)}
        except GraphDBError:
            pass
    counts = await run_in_threadpool(STORE.graph_counts, dataset_id)
    return {"node_count": counts["nodes"], "edge_count": counts["edges"]}


@app.get("/api/rf/datasets/{dataset_id}/graph")
async def rf_graph(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    graph = STORE.get_graph(dataset_id)
    return {"graph": {"nodes": [{"id": node["id"], "name": node["name"],
                                 "category": node.get("category") or "术语",
                                 "pagerank": node["weight"]} for node in graph["nodes"]],
                      "edges": [{"source": edge["source"], "target": edge["target"],
                                 "weight": edge["weight"]} for edge in graph["edges"]]},
            "mind_map": {},
            **(await _graph_scale(dataset_id))}


@app.post("/api/rf/datasets/{dataset_id}/graph")
async def rf_graph_build(dataset_id: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    backend = GRAPHDB if GRAPH_BACKEND == "neo4j" else None
    result = await run_in_threadpool(build_graph, STORE, dataset_id, GRAPH_MAX_NODES,
                                     GRAPH_MAX_EDGES, backend, GATEWAY, GRAPH_EDGES_PER_NODE)
    return {"graphrag_task_id": new_id("kg"), **result}


@app.get("/api/rf/datasets/{dataset_id}/graph/trace")
async def rf_graph_trace(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    scale = await _graph_scale(dataset_id)
    return {"progress": 1.0 if scale["node_count"] else 0.0,
            "progress_msg": f"实体 {scale['node_count']} / 关系 {scale['edge_count']}"}


@app.delete("/api/rf/datasets/{dataset_id}/graph")
async def rf_graph_delete(dataset_id: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.replace_graph(dataset_id, [], [])
    if GRAPH_BACKEND == "neo4j" and GRAPHDB.enabled:
        try:
            await run_in_threadpool(GRAPHDB.delete_dataset, dataset_id)
        except GraphDBError:
            pass
    return {"ok": True}


#: 内核能力开关：声明"哪些能力本产品内核没有实现"。
#: 前端据此**把入口置灰并写明原因**，而不是点下去收一个 501 报错——
#: 接口本身保留（POST /raptor 仍在），只是不再让用户撞墙。
#: 判据见 docs/16 §7 D37（2026-09-28 甲方口径："RAPTOR 摘要树在本产品内核中未启用（保留接口，避免前端报错）"）。
RAPTOR_UNAVAILABLE = "RAPTOR 摘要树在本产品内核中未启用（保留接口，避免前端报错）"
CAPABILITIES: Dict[str, Dict[str, Any]] = {
    "raptor": {"enabled": False, "reason": RAPTOR_UNAVAILABLE},
}


@app.get("/api/rf/capabilities")
async def rf_capabilities(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """内核能力清单（当前只有 RAPTOR 一项需要前端置灰）。"""
    return {"capabilities": CAPABILITIES}


@app.post("/api/rf/datasets/{dataset_id}/raptor")
async def rf_raptor(dataset_id: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    raise fail(RAPTOR_UNAVAILABLE, 501)


@app.get("/api/rf/datasets/{dataset_id}/raptor/trace")
async def rf_raptor_trace(dataset_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {}


# ================================================================ 模型提供商

class ProviderPayload(BaseModel):
    provider: str
    name: str
    base_url: str
    api_key: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)
    provider_id: str = ""


class RoleBindingPayload(BaseModel):
    role: str
    provider_id: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


VENDORS = [
    {"provider": "ollama", "label": "Ollama（本地 / GPU）", "roles": ["chat", "embedding", "rerank"],
     "default_base_url": "http://127.0.0.1:11434/v1", "needs_key": False},
    {"provider": "deepseek", "label": "DeepSeek", "roles": ["chat"],
     "default_base_url": "https://api.deepseek.com/v1", "needs_key": True},
    {"provider": "openai", "label": "OpenAI", "roles": ["chat", "embedding"],
     "default_base_url": "https://api.openai.com/v1", "needs_key": True},
    {"provider": "azure_openai", "label": "Azure OpenAI", "roles": ["chat", "embedding"],
     "default_base_url": "", "needs_key": True},
    {"provider": "qwen", "label": "通义千问（DashScope 兼容模式）", "roles": ["chat", "embedding"],
     "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "needs_key": True},
    {"provider": "zhipu", "label": "智谱 AI", "roles": ["chat", "embedding"],
     "default_base_url": "https://open.bigmodel.cn/api/paas/v4", "needs_key": True},
    {"provider": "moonshot", "label": "月之暗面 Kimi", "roles": ["chat"],
     "default_base_url": "https://api.moonshot.cn/v1", "needs_key": True},
    {"provider": "baichuan", "label": "百川智能", "roles": ["chat"],
     "default_base_url": "https://api.baichuan-ai.com/v1", "needs_key": True},
    {"provider": "minimax", "label": "MiniMax", "roles": ["chat"],
     "default_base_url": "https://api.minimax.chat/v1", "needs_key": True},
    {"provider": "xinference", "label": "Xinference（自部署）", "roles": ["chat", "embedding", "rerank"],
     "default_base_url": "http://127.0.0.1:9997/v1", "needs_key": False},
    {"provider": "vllm", "label": "vLLM / LMDeploy（OpenAI 兼容）", "roles": ["chat", "embedding"],
     "default_base_url": "http://127.0.0.1:8000/v1", "needs_key": False},
    {"provider": "openai_compatible", "label": "OpenAI 兼容（自填 url）", "roles": ["chat", "embedding", "rerank"],
     "default_base_url": "", "needs_key": False},
]


def _mask_key(value: str) -> str:
    value = value or ""
    return (value[:4] + "****" + value[-4:]) if len(value) > 8 else ("****" if value else "")


@app.get("/api/models/providers")
async def models_providers(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    """模型厂商目录 + 接入配置（providers）+ 角色绑定 + 角色生效配置。"""
    return {"vendors": VENDORS, "roles": GATEWAY.public_config(),
            "bindings": STORE.list_role_bindings(),
            "providers": [{**item, "api_key": _mask_key(item.get("api_key") or "")}
                          for item in STORE.list_providers()],
            "local_models": await run_in_threadpool(_ollama_models)}


def _ollama_models() -> Dict[str, Any]:
    base = (ENV.get("OLLAMA_BASE") or "http://127.0.0.1:11434").rstrip("/")
    out: Dict[str, Any] = {"reachable": False, "installed": [], "loaded": []}
    try:
        with httpx.Client(timeout=10.0) as client:
            tags = client.get(base + "/api/tags").json()
            ps = client.get(base + "/api/ps").json()
        out["reachable"] = True
        out["installed"] = [{"name": item.get("name"), "size_mb": round(int(item.get("size") or 0) / 1048576)}
                            for item in (tags.get("models") or [])]
        out["loaded"] = [{"name": item.get("name"),
                          "size_vram_mb": round(int(item.get("size_vram") or 0) / 1048576)}
                         for item in (ps.get("models") or [])]
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


@app.post("/api/models/providers")
async def models_provider_save(payload: ProviderPayload,
                               user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.base_url.strip():
        raise fail("请填写模型服务的 base url（例如 http://127.0.0.1:11434/v1）")
    if not payload.name.strip():
        raise fail("请填写模型名称")
    return {"provider": STORE.upsert_provider(payload.provider.strip() or "openai_compatible",
                                              payload.name.strip(), payload.base_url.strip(),
                                              payload.api_key.strip(), payload.extra, payload.provider_id)}


@app.post("/api/models/roles")
async def models_role_bind(payload: RoleBindingPayload,
                           user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    """把模型角色绑定到某条接入配置（provider_id 为空表示回退配置文件默认值）。"""
    if payload.role not in ROLES:
        raise fail(f"未知模型角色：{payload.role}")
    if payload.provider_id and not STORE.get_provider(payload.provider_id):
        raise fail("请选择已保存的模型接入配置")
    return {"binding": STORE.set_role_binding(payload.role, payload.provider_id, payload.extra)}


@app.delete("/api/models/providers/{provider_id}")
async def models_provider_delete(provider_id: str,
                                 user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.delete_provider(provider_id)
    return {"ok": True}


class ModelTestPayload(BaseModel):
    role: str


@app.post("/api/models/test")
async def models_test(payload: ModelTestPayload, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return await run_in_threadpool(GATEWAY.test, payload.role)


# ================================================================ 长期记忆

MEMORY_SCOPES = [
    {"value": "global", "label": "全局记忆", "hint": "对所有问答生效"},
    {"value": "project", "label": "项目记忆", "hint": "仅当同名知识库参与检索时生效"},
]


class MemoryPayload(BaseModel):
    id: str = ""
    scope: str = "global"
    project: str = ""
    title: str = ""
    content: str = ""
    tags: str = ""
    enabled: bool = True


@app.get("/api/memory")
async def memory_list(scope: str = "", project: str = "", keyword: str = "",
                      user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    memories = STORE.list_memories(scope=scope, project=project, keyword=keyword)
    return {"scopes": MEMORY_SCOPES, "projects": [item["name"] for item in STORE.list_datasets()],
            "memories": memories,
            "stats": {"total": len(memories),
                      "global": len([item for item in memories if item["scope"] == "global"]),
                      "enabled": len([item for item in memories if item["enabled"]])}}


@app.post("/api/memory")
async def memory_save(payload: MemoryPayload, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    title = payload.title.strip()
    content = payload.content.strip()
    if not title:
        raise fail("请填写记忆标题")
    if not content:
        raise fail("请填写记忆内容")
    scope = payload.scope if payload.scope in ("global", "project") else "global"
    project = payload.project.strip() if scope == "project" else ""
    if scope == "project" and not project:
        raise fail("项目记忆需要选择所属知识库（项目）")
    existing = STORE.get_memory(payload.id) if payload.id else None
    return {"memory": STORE.upsert_memory(scope, title, content, project, payload.tags.strip(),
                                          (existing or {}).get("source") or "manual",
                                          payload.enabled, payload.id)}


@app.delete("/api/memory/{memory_id}")
async def memory_delete(memory_id: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.delete_memory(memory_id)
    return {"ok": True}


# ================================================================ 算力与显存

@app.get("/api/platform/vram")
async def platform_vram(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    gpu = await run_in_threadpool(_gpu_info)
    ollama = await run_in_threadpool(_ollama_models)
    plans = [
        {"key": "A", "name": "质量优先", "allocation": "对话 32B 级 INT4 + 嵌入 + 重排全部常驻",
         "peak_gb": [26, 29], "scene": "问答质量优先，并发中等", "cost": "上下文与并发预算较紧，首字延迟略高"},
        {"key": "B", "name": "余量优先（推荐起步）", "allocation": "对话 14B INT8 + 嵌入 + 重排全部常驻",
         "peak_gb": [23, 25], "scene": "并发与上下文优先", "cost": "对话能力较方案 A 低一档"},
    ]
    fits = [plan["key"] for plan in plans if (gpu.get("memory_total_gb") or 0) >= plan["peak_gb"][0]]
    if not gpu.get("available"):
        verdict = "未检测到 NVIDIA 显卡，按 CPU 推理运行（问答与嵌入仍可用，速度受限）"
    elif fits:
        verdict = "当前显卡 " + str(gpu["memory_total_gb"]) + " GB，满足方案 " + " / ".join(fits) + " 的显存要求"
    else:
        verdict = ("当前显卡 " + str(gpu.get("memory_total_gb")) + " GB，低于表 46 任一方案的峰值下限 23 GB："
                   "按 16GB 档位裁剪对话模型规模与上下文预算，或更换 32GB 加速卡")
    return {
        "gpu": gpu, "ollama": ollama,
        "budget_rows": [
            {"model": "对话与生成模型（14B 级）", "precision": "INT8", "weights": "约 15 GB",
             "runtime": "KV 缓存与激活 2~4 GB", "total": "约 17~19 GB"},
            {"model": "嵌入模型", "precision": "FP16", "weights": "约 2.3 GB", "runtime": "0.5 GB",
             "total": "约 2.8 GB"},
            {"model": "重排模型", "precision": "FP16", "weights": "约 2.3 GB", "runtime": "0.5 GB",
             "total": "约 2.8 GB"},
            {"model": "合计（14B 方案，全部常驻显卡）", "precision": "—", "weights": "—", "runtime": "—",
             "total": "约 23~25 GB，余量 7~9 GB"},
            {"model": "合计（32B 级 INT4 方案，全部常驻显卡）", "precision": "—", "weights": "—",
             "runtime": "—", "total": "约 26~29 GB，余量 3~6 GB"},
        ],
        "plans": plans, "fits": fits, "verdict": verdict,
        "source": "docs/11 §3.10 表 45 / 表 46（32GB 单卡显存预算与部署组合方案）",
        "role_plan": [
            {"role": "对话与生成", "plan_a": "32B 级 INT4 常驻（约 19~21 GB）",
             "plan_b": "14B 级 INT8 常驻（约 17~19 GB）",
             "current": GATEWAY.public_config()["chat"].get("name") or "未配置"},
            {"role": "嵌入", "plan_a": "FP16 常驻（约 2.8 GB）", "plan_b": "FP16 常驻（约 2.8 GB）",
             "current": GATEWAY.public_config()["embedding"].get("name") or "未配置"},
            {"role": "重排", "plan_a": "FP16 常驻（约 2.8 GB）", "plan_b": "FP16 常驻（约 2.8 GB）",
             "current": GATEWAY.public_config()["rerank"].get("name") or "未部署（首条命中率的最大杠杆）"},
        ],
        "effect_knobs": [
            {"name": "检索 top_k 6 → 10", "where": "产品检索默认值（可改）",
             "effect": "真实文档 10 题：全集命中 50%→60%、至少命中一条 90%→100%", "status": "已落地"},
            {"name": "重排（rerank）", "where": "模型提供商 · rerank 角色",
             "effect": "把「第一条就是对的」概率拉起来（当前集首条命中 0/10）", "status": "待部署"},
            {"name": "混合检索权重（向量 + 词项）", "where": "检索测试页 vector_weight",
             "effect": "条款编号、代号类问题提高词项权重更稳", "status": "已落地"},
            {"name": "嵌入批量与常驻", "where": "OLLAMA_KEEP_ALIVE / 批量 16",
             "effect": "嵌入吞吐提升约 40 倍，避免每轮重载模型", "status": "已落地"},
            {"name": "解析并行", "where": "知识内核并发（单进程串行，可多开）",
             "effect": "真实交付文档 11 份 / 2490 切片可在数分钟内建索引", "status": "已落地"},
        ],
    }


def _gpu_info() -> Dict[str, Any]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"available": False, "error": "未找到 nvidia-smi（按 CPU 推理运行）"}
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": str(exc)[:200]}
    if out.returncode != 0 or not (out.stdout or "").strip():
        return {"available": False, "error": (out.stderr or "nvidia-smi 无输出").strip()[:200]}
    parts = [part.strip() for part in out.stdout.strip().splitlines()[0].split(",")]
    total = int(float(parts[1]))
    used = int(float(parts[2]))
    return {"available": True, "name": parts[0], "memory_total_mb": total, "memory_used_mb": used,
            "memory_free_mb": int(float(parts[3])), "memory_total_gb": round(total / 1024, 1),
            "memory_used_gb": round(used / 1024, 1),
            "utilization": int(float(parts[4])) if parts[4] else None,
            "temperature_c": int(float(parts[5])) if len(parts) > 5 and parts[5] else None}


# ================================================================ 进程资源管理器

#: 产品相关的外部依赖服务（按监听端口探测 PID），仅列本产品用到的这些
PROCESS_SERVICES = [
    {"key": "neo4j", "name": "Neo4j 图数据库", "ports": [7485, 7695]},
    {"key": "ollama", "name": "Ollama 模型服务", "ports": [11434]},
    {"key": "rerank", "name": "本地重排服务（BGE Reranker）", "ports": [8196]},
    {"key": "sandbox", "name": "沙箱执行 shim", "ports": [8194]},
    {"key": "plugin-daemon", "name": "插件守护进程", "ports": [5002]},
    {"key": "dify-api", "name": "Dify API", "ports": [5001]},
]

#: CPU 采样的上一次快照（pid → (monotonic 时间, 累计 CPU 秒)），用于算增量
_CPU_SAMPLE: Dict[int, tuple] = {}


def _process_thresholds(cpu: Optional[float] = None, mem: Optional[float] = None) -> Dict[str, Any]:
    """进程告警阈值：请求参数优先，其次 equipment.env，最后内置默认值。"""
    cpu_value = float(ENV.get("EQUIPMENT_PROCESS_CPU_THRESHOLD", "80"))
    mem_value = float(ENV.get("EQUIPMENT_PROCESS_MEM_THRESHOLD_MB", "4096"))
    source = "env"
    if cpu is not None:
        cpu_value = float(cpu)
        source = "request"
    if mem is not None:
        mem_value = float(mem)
        source = "request"
    return {"cpu_percent": cpu_value, "mem_mb": mem_value, "source": source}


def _listening_pids_windows() -> Dict[int, int]:
    """端口 → 监听进程 PID（Windows，解析 netstat -ano）。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return {}
    mapping: Dict[int, int] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        try:
            port = int(parts[1].rsplit(":", 1)[1])
            pid = int(parts[4])
        except (ValueError, IndexError):
            continue
        mapping.setdefault(port, pid)
    return mapping


def _listening_pids_linux() -> Dict[int, int]:
    """端口 → 监听进程 PID（Linux，解析 /proc/net/tcp 与 /proc/*/fd）。"""
    inode_port: Dict[str, int] = {}
    for proto in ("tcp", "tcp6"):
        path = Path(f"/proc/net/{proto}")
        if not path.exists():
            continue
        for raw in path.read_text(errors="ignore").splitlines()[1:]:
            cols = raw.split()
            if len(cols) < 10 or cols[3] != "0A":  # 0A = LISTEN
                continue
            try:
                inode_port[cols[9]] = int(cols[1].rsplit(":", 1)[1], 16)
            except (ValueError, IndexError):
                continue
    mapping: Dict[int, int] = {}
    if not inode_port:
        return mapping
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            for fd in (entry / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:["):
                    port = inode_port.get(target[8:-1])
                    if port is not None:
                        mapping.setdefault(port, int(entry.name))
        except OSError:
            continue
    return mapping


def _listening_pids() -> Dict[int, int]:
    return _listening_pids_windows() if os.name == "nt" else _listening_pids_linux()


def _sample_windows(pids: List[int]) -> Dict[int, Dict[str, Any]]:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe or not pids:
        return {}
    ids = ",".join(str(p) for p in pids)
    script = (f"Get-Process -Id {ids} -ErrorAction SilentlyContinue | ForEach-Object "
              "{ \"{0}|{1}|{2}|{3}\" -f $_.Id,$_.ProcessName,$_.WorkingSet64,$_.CPU }")
    try:
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=25)
    except Exception:  # noqa: BLE001
        return {}
    result: Dict[int, Dict[str, Any]] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4 or not parts[0].strip().isdigit():
            continue
        try:
            pid = int(parts[0])
            mem_mb = round(int(parts[2]) / 1024 / 1024, 1) if parts[2].strip() else None
            cpu_seconds = float(parts[3]) if parts[3].strip() else None
        except ValueError:
            continue
        result[pid] = {"name": parts[1].strip(), "mem_mb": mem_mb, "cpu_seconds": cpu_seconds}
    return result


def _sample_linux(pids: List[int]) -> Dict[int, Dict[str, Any]]:
    try:
        clk = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk = 100
    result: Dict[int, Dict[str, Any]] = {}
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(errors="ignore")
            rp = stat.rindex(")")
            fields = stat[rp + 2:].split()
            cpu_seconds = (int(fields[11]) + int(fields[12])) / clk
        except (OSError, ValueError, IndexError):
            continue
        name, mem_mb = str(pid), None
        try:
            name = Path(f"/proc/{pid}/comm").read_text(errors="ignore").strip() or str(pid)
        except OSError:
            pass
        try:
            for raw in Path(f"/proc/{pid}/status").read_text(errors="ignore").splitlines():
                if raw.startswith("VmRSS:"):
                    mem_mb = round(int(raw.split()[1]) / 1024, 1)
                    break
        except OSError:
            pass
        result[pid] = {"name": name, "mem_mb": mem_mb, "cpu_seconds": cpu_seconds}
    return result


def _sample_processes(pids: List[int]) -> Dict[int, Dict[str, Any]]:
    """采样指定 PID 的内存与 CPU 占用；CPU% 由两次快照差值算出。"""
    unique = sorted({int(p) for p in pids if p})
    raw = _sample_windows(unique) if os.name == "nt" else _sample_linux(unique)
    now_ts = time.monotonic()
    result: Dict[int, Dict[str, Any]] = {}
    for pid, info in raw.items():
        cpu_seconds = info.get("cpu_seconds")
        cpu_percent = None
        prev = _CPU_SAMPLE.get(pid)
        if prev is not None and cpu_seconds is not None:
            elapsed = now_ts - prev[0]
            if elapsed > 0.05:
                cpu_percent = round(max(0.0, (cpu_seconds - prev[1]) / elapsed * 100), 1)
        if cpu_seconds is not None:
            _CPU_SAMPLE[pid] = (now_ts, cpu_seconds)
        result[pid] = {"name": info.get("name"), "mem_mb": info.get("mem_mb"), "cpu_percent": cpu_percent}
    if len(_CPU_SAMPLE) > 512:
        for stale in list(_CPU_SAMPLE.keys())[:-256]:
            _CPU_SAMPLE.pop(stale, None)
    return result


def _process_alert(item: Dict[str, Any], thresholds: Dict[str, Any]) -> Optional[str]:
    if item["status"] != "running":
        return "status" if item["kind"] == "kernel" else None
    cpu = item.get("cpu_percent")
    mem = item.get("mem_mb")
    if cpu is not None and cpu > thresholds["cpu_percent"]:
        return "cpu"
    if mem is not None and mem > thresholds["mem_mb"]:
        return "mem"
    return None


def _process_snapshot(cpu: Optional[float] = None, mem: Optional[float] = None) -> Dict[str, Any]:
    """采集产品相关进程快照：内核 worker + 产品服务自身 + 外部依赖服务。"""
    thresholds = _process_thresholds(cpu, mem)
    listening = _listening_pids()
    items: List[Dict[str, Any]] = []

    for info in kernel_workers():
        pid = info.get("pid")
        label = "知识内核" if info.get("name") == "knowledge" else "编排内核"
        items.append({
            "pid": pid, "name": label, "kind": "kernel", "kernel": info.get("name"),
            "status": "running" if info.get("alive") else "stopped", "restarts": info.get("restarts"),
            "ports": [], "killable": bool(pid),
        })

    self_pid = os.getpid()
    items.append({
        "pid": self_pid, "name": "产品服务", "kind": "product",
        "kernel": None, "status": "running", "restarts": 0, "ports": [8095],
        "killable": False,
    })

    for svc in PROCESS_SERVICES:
        pid = next((listening[p] for p in svc["ports"] if p in listening), None)
        items.append({
            "pid": pid, "name": svc["name"], "kind": "service", "kernel": None,
            "status": "running" if pid else "stopped", "restarts": None, "ports": list(svc["ports"]),
            "killable": bool(pid),
        })

    sample = _sample_processes([it["pid"] for it in items if it.get("pid")])
    for item in items:
        measured = sample.get(item.get("pid") or -1, {})
        item["cpu_percent"] = measured.get("cpu_percent")
        item["mem_mb"] = measured.get("mem_mb")
        item["alert"] = _process_alert(item, thresholds)

    alerts: List[Dict[str, Any]] = []
    for item in items:
        metric = item.get("alert")
        if not metric:
            continue
        if metric == "status":
            alerts.append({"pid": item["pid"], "name": item["name"], "metric": "status", "value": "stopped",
                           "threshold": "running", "level": "warn", "message": f"{item['name']} 未运行"})
            continue
        value = item["cpu_percent"] if metric == "cpu" else item["mem_mb"]
        limit = thresholds["cpu_percent"] if metric == "cpu" else thresholds["mem_mb"]
        unit = "%" if metric == "cpu" else " MB"
        note = f"（累计重启 {item['restarts']} 次，持续超限可能被内核自动重建）" if item["kind"] == "kernel" else ""
        alerts.append({
            "pid": item["pid"], "name": item["name"], "metric": metric, "value": value, "threshold": limit,
            "level": "crit" if value > limit * 1.5 else "warn",
            "message": f"{item['name']} {metric.upper()} 占用 {value}{unit}，超过阈值 {limit}{unit}{note}",
        })

    return {"processes": items, "thresholds": thresholds, "alerts": alerts, "count": len(items),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "host": _platform.node()}


@app.get("/api/platform/processes")
async def platform_processes(cpu: Optional[float] = None, mem: Optional[float] = None,
                             user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return await run_in_threadpool(_process_snapshot, cpu, mem)


@app.get("/api/platform/processes/export")
async def platform_processes_export(format: str = "csv", cpu: Optional[float] = None, mem: Optional[float] = None,
                                    user: Dict[str, str] = Depends(current_user)) -> PlainTextResponse:
    snapshot = await run_in_threadpool(_process_snapshot, cpu, mem)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if format.lower() == "json":
        body = json.dumps(snapshot, ensure_ascii=False, indent=2)
        media, ext = "application/json", "json"
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["pid", "name", "kind", "status", "cpu_percent", "mem_mb", "restarts", "ports"])
        for item in snapshot["processes"]:
            writer.writerow([item.get("pid"), item.get("name"), item.get("kind"),
                             item.get("status"), item.get("cpu_percent"), item.get("mem_mb"),
                             item.get("restarts"), "/".join(str(p) for p in item.get("ports") or [])])
        body = buf.getvalue()
        media, ext = "text/csv", "csv"
    return PlainTextResponse(body, media_type=media,
                             headers={"Content-Disposition": f'attachment; filename="processes-{stamp}.{ext}"'})


@app.post("/api/platform/kernels/{name}/restart")
async def platform_kernel_restart(name: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if name not in ("knowledge", "orchestration"):
        raise fail("未知内核：仅支持 knowledge / orchestration", 404)
    result = await run_in_threadpool(restart_kernel, name)
    return {"ok": True, **result}


@app.post("/api/platform/processes/{pid}/kill")
async def platform_process_kill(pid: int, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    snapshot = await run_in_threadpool(_process_snapshot, None, None)
    target = next((it for it in snapshot["processes"] if it.get("pid") == pid), None)
    if target is None:
        raise fail("该 PID 不在产品相关进程白名单内，拒绝结束", 403)
    if pid == os.getpid():
        raise fail("禁止结束产品服务自身进程", 400)
    if not target.get("killable"):
        raise fail(f"{target['name']} 不允许在此结束", 400)
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception as exc:  # noqa: BLE001
        raise fail(f"结束进程失败：{exc}", 500)
    return {"ok": True, "pid": pid, "name": target["name"]}


# ================================================================ 数据标注

ANNO_DIMENSIONS = [
    {"code": "D1", "name": "覆盖完备性", "hint": "该有的知识没有：问题在资料中无对应条款"},
    {"code": "D2", "name": "内容准确性", "hint": "写了的内容不对：与权威值不符"},
    {"code": "D3", "name": "一致性", "hint": "文档内或跨文档自相矛盾"},
    {"code": "D4", "name": "版本时效性", "hint": "非现行有效：作废版本仍被引用"},
    {"code": "D5", "name": "术语规范性", "hint": "术语不统一、缩略语未展开"},
    {"code": "D6", "name": "安全完备性", "hint": "安全警示缺失：操作步骤未带前置条件与禁止事项"},
    {"code": "D7", "name": "结构可检索性", "hint": "无标题层级 / 编号断裂 / 表格跨页错乱"},
    {"code": "D8", "name": "引用可追溯性", "hint": "命中片段无法定位到条款号与页码"},
]
ANNO_COMPLETENESS = [{"value": "complete", "label": "健全"}, {"value": "partial", "label": "部分缺失"},
                     {"value": "missing", "label": "缺失"}]
ANNO_COMPLIANCE = [{"value": "compliant", "label": "合规"}, {"value": "doubtful", "label": "存疑"},
                   {"value": "violation", "label": "不合规"}]


@app.get("/api/anno/schema")
async def anno_schema(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"defect_dimensions": ANNO_DIMENSIONS, "completeness": ANNO_COMPLETENESS,
            "compliance": ANNO_COMPLIANCE, "db": str(STORE.path)}


class AnnoTaskPayload(BaseModel):
    name: str
    dataset_id: str = ""
    note: str = ""


class AnnoSeedPayload(BaseModel):
    limit: int = 20
    dataset_id: str = ""


class AnnoItemPayload(BaseModel):
    status: str = "done"
    defect_dimensions: List[str] = Field(default_factory=list)
    severity: str = "none"
    completeness: str = ""
    compliance: str = ""
    comment: str = ""


@app.get("/api/anno/tasks")
async def anno_tasks(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"tasks": STORE.list_anno_tasks()}


@app.post("/api/anno/tasks")
async def anno_task_create(payload: AnnoTaskPayload,
                           user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    if not payload.name.strip():
        raise fail("任务名称不能为空")
    return {"task": STORE.create_anno_task(payload.name.strip(), payload.dataset_id, payload.note,
                                           user["username"])}


@app.delete("/api/anno/tasks/{task_id}")
async def anno_task_delete(task_id: str, user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.delete_anno_task(task_id)
    return {"ok": True}


@app.post("/api/anno/tasks/{task_id}/seed")
async def anno_task_seed(task_id: str, payload: AnnoSeedPayload,
                         user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    task = STORE.get_anno_task(task_id)
    if not task:
        raise fail("标注任务不存在", 404)
    dataset_id = payload.dataset_id or task["dataset_id"]
    if not dataset_id:
        raise fail("请先指定知识库")
    added = STORE.seed_anno_items(task_id, dataset_id, max(1, min(payload.limit, 500)))
    return {"added": added, **STORE.anno_progress(task_id)}


@app.get("/api/anno/tasks/{task_id}/items")
async def anno_items(task_id: str, status: str = "", limit: int = 50,
                     user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"items": STORE.list_anno_items(task_id, status, limit), **STORE.anno_progress(task_id)}


@app.put("/api/anno/items/{item_id}")
async def anno_item_save(item_id: str, payload: AnnoItemPayload,
                         user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    labels = {"defect_dimensions": payload.defect_dimensions, "severity": payload.severity,
              "completeness": payload.completeness, "compliance": payload.compliance}
    STORE.save_anno_item(item_id, payload.status or "done", labels, payload.comment, user["username"])
    return {"ok": True, "labels": labels}


@app.get("/api/anno/tasks/{task_id}/stats")
async def anno_stats(task_id: str, user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return STORE.anno_stats(task_id)


@app.get("/api/anno/tasks/{task_id}/export", response_class=PlainTextResponse)
async def anno_export(task_id: str, only_done: bool = True,
                      user: Dict[str, str] = Depends(current_user)) -> PlainTextResponse:
    lines = []
    for item in STORE.anno_export_rows(task_id, only_done):
        lines.append(json.dumps({"task_id": item["task_id"], "item_id": item["id"],
                                 "chunk_id": item["chunk_id"], "document_id": item["document_id"],
                                 "doc_name": item["doc_name"], "content": item["content"],
                                 "labels": item["labels"], "comment": item["comment"],
                                 "annotator": item["annotator"], "updated_at": item["updated_at"]},
                                ensure_ascii=False))
    return PlainTextResponse("\n".join(lines) + ("\n" if lines else ""), media_type="application/x-ndjson")


# ================================================================ 编排工作台

#: DSL 节点类型 → 运行所需凭据角色（产品没有 per-app 密钥，凭据统一来自「模型提供商」配置）
APP_CREDENTIAL_ROLES = {
    "llm": "chat",
    "agent": "chat",
    "question-classifier": "chat",
    "parameter-extractor": "chat",
    "knowledge-retrieval": "embedding",
}


def _app_node_types(dsl: Any) -> List[str]:
    graph = ((dsl.get("workflow") or {}).get("graph") or {}) if isinstance(dsl, dict) else {}
    return [str((node.get("data") or {}).get("type") or "") for node in (graph.get("nodes") or [])]


def _app_credentials(dsl: Any) -> bool:
    """应用所需凭据是否齐备（门户据此显示「密钥已配置 / 缺少密钥」）。

    只按节点实际用到的东西判定：模型节点看统一模型配置、代码节点看统一沙箱地址。
    示例链「开始 → 提示词组装 → 结束」不依赖任何凭据，因此判定为就绪。
    """
    node_types = set(_app_node_types(dsl))
    for node_type, role in APP_CREDENTIAL_ROLES.items():
        if node_type in node_types:
            try:
                if not GATEWAY.resolve(role).get("base_url"):
                    return False
            except ModelError:
                return False
    if "code" in node_types and not ENV.get("CODE_EXEC_ENDPOINT"):
        return False
    return True


@app.get("/api/engine/health")
async def engine_health(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"gateway": "ok", "kernels": await run_in_threadpool(kernel_health)}


@app.get("/api/orchestration/apps")
async def orchestration_apps(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    apps = []
    for workflow in STORE.list_workflows():
        info = inspect_workflow(workflow["dsl_parsed"])
        apps.append({"id": workflow["id"], "name": workflow["name"], "mode": workflow["mode"],
                     "status": info.get("load_status") or "unknown",
                     "description": workflow.get("note") or "",
                     "updated_at": workflow["updated_at"],
                     "has_api_key": _app_credentials(workflow["dsl_parsed"]),
                     "nodes": info.get("node_count"), "edges": info.get("edge_count")})
    return {"apps": apps}


@app.get("/api/orchestration/apps/{app_id}")
async def orchestration_app_detail(app_id: str,
                                   user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    workflow = STORE.get_workflow(app_id)
    if not workflow:
        raise fail("工作流不存在", 404)
    graph = _flow_graph(workflow["dsl_parsed"])
    return {"app": {"id": workflow["id"], "name": workflow["name"], "mode": workflow["mode"],
                    "status": "published", "description": workflow.get("note") or "",
                    "updated_at": workflow["updated_at"],
                    "has_api_key": _app_credentials(workflow["dsl_parsed"]),
                    "nodes": len(graph["nodes"]), "edges": len(graph["edges"])},
            "dsl": workflow["dsl_parsed"],
            **graph}


@app.get("/api/orchestration/apps/{app_id}/graph")
async def orchestration_app_graph(app_id: str,
                                  user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    workflow = STORE.get_workflow(app_id)
    if not workflow:
        raise fail("工作流不存在", 404)
    return _flow_graph(workflow["dsl_parsed"])


def _flow_graph(dsl: Dict[str, Any]) -> Dict[str, Any]:
    graph = ((dsl.get("workflow") or {}).get("graph") or {}) if isinstance(dsl, dict) else {}
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    nodes = []
    for index, node in enumerate(raw_nodes):
        data = node.get("data") or {}
        nodes.append({"id": node.get("id") or f"node{index}", "type": data.get("type") or "custom",
                      "category": data.get("type") or "",
                      "title": data.get("title") or data.get("type") or "",
                      "desc": (data.get("desc") or data.get("template") or "")[:120],
                      "x": float(((node.get("position") or {}).get("x")) or 120 + index * 220),
                      "y": float(((node.get("position") or {}).get("y")) or 120)})
    edges = [{"id": edge.get("id") or f"e{index}", "source": edge.get("source"),
              "target": edge.get("target"), "label": ""} for index, edge in enumerate(raw_edges)]
    start = next((node for node in raw_nodes if (node.get("data") or {}).get("type") == "start"), None)
    variables = []
    for item in ((start or {}).get("data") or {}).get("variables") or []:
        variables.append({"variable": item.get("variable"), "label": item.get("label") or item.get("variable"),
                          "type": item.get("type") or "text-input", "required": bool(item.get("required")),
                          "default": item.get("default")})
    return {"nodes": nodes, "edges": edges, "variables": variables}


@app.get("/api/orchestration/apps/{app_id}/runs")
async def orchestration_app_runs(app_id: str,
                                 user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    runs = []
    for item in STORE.list_runs(app_id):
        runs.append({"id": item["id"], "status": item["status"], "triggered_from": "portal",
                     "created_at": item["created_at"], "elapsed_time": item["elapsed_ms"] / 1000.0,
                     "total_tokens": 0, "error": item.get("error") or "",
                     "inputs": item.get("inputs") or {}, "outputs": item.get("outputs") or {},
                     "events": item.get("events") or []})
    return {"runs": runs}


class RunPayload(BaseModel):
    inputs: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/orchestration/apps/{app_id}/run")
async def orchestration_app_run(app_id: str, payload: RunPayload,
                                user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    workflow = STORE.get_workflow(app_id)
    if not workflow:
        raise fail("工作流不存在", 404)
    record = await run_in_threadpool(run_workflow, STORE, workflow, payload.inputs)
    return {"status": record["status"], "http_status": 200,
            "elapsed_time": record["elapsed_ms"] / 1000.0, "total_tokens": 0,
            "outputs": record["outputs"], "error": record.get("error") or "",
            "events": record.get("events") or []}


class WorkflowPayload(BaseModel):
    name: str
    mode: str = "workflow"
    dsl: Any = None
    note: str = ""
    workflow_id: str = ""


def _normalize_mode(mode: str) -> str:
    """graphon 只执行 workflow / advanced-chat；「对话流」统一归一为 advanced-chat。"""
    mode = (mode or "").strip() or "workflow"
    return "advanced-chat" if mode == "chat" else mode


@app.post("/api/orchestration/workflows")
async def orchestration_workflow_save(payload: WorkflowPayload,
                                      user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    name = payload.name or "未命名工作流"
    mode = _normalize_mode(payload.mode)
    dsl = payload.dsl if payload.dsl is not None else skeleton_dsl(mode, name)
    text = dsl if isinstance(dsl, str) else json.dumps(dsl, ensure_ascii=False)
    return {"workflow": STORE.save_workflow(name, text, mode=mode,
                                            note=payload.note, workflow_id=payload.workflow_id)}


class ImportPayload(BaseModel):
    name: str = ""
    note: str = ""
    content: str = ""
    mode: str = ""


@app.post("/api/orchestration/workflows/import")
async def orchestration_workflow_import(payload: ImportPayload,
                                        user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    """导入外部 Dify DSL（.yml/.yaml/.json）：解析 → 严格校验 → 通过才落库。

    严格版口径（甲方 2026-09-28 确认）：yaml 解析 + graphon inspect 必须可加载且含节点，
    否则直接报错、**不落库**（不在应用列表里留半成品）。YAML 解析由编排内核代劳（带 PyYAML）。
    """
    content = (payload.content or "").strip()
    if not content:
        raise fail("请提供 DSL 内容：上传 .yml/.yaml/.json 文件，或直接粘贴文本", 400)
    try:
        parsed = await run_in_threadpool(parse_dsl, content)
    except ValueError as exc:
        raise fail(f"DSL 解析失败，已拒收（未落库）：{exc}", 400)
    dsl_obj = parsed.get("dsl")
    if not isinstance(dsl_obj, dict):
        raise fail("DSL 顶层必须是映射对象（key: value），已拒收（未落库）", 400)
    kind = str(parsed.get("kind") or "")
    if kind != "app":
        raise fail(f"仅支持 Dify 应用 DSL（kind: app），收到 kind={kind or '空'}，已拒收（未落库）", 422)
    mode = _normalize_mode(payload.mode or str(parsed.get("mode") or ""))
    info = await run_in_threadpool(inspect_workflow, dsl_obj)
    if str(info.get("load_status") or "").strip().lower() != "loadable":
        raise fail(f"DSL 校验未通过，已拒收（未落库）：{info.get('reason') or '结构不可加载'}", 422)
    if int(info.get("node_count") or 0) <= 0:
        raise fail("DSL 校验未通过，已拒收（未落库）：图中没有可执行节点", 422)
    app = dsl_obj.get("app") or {}
    name = (payload.name or "").strip() or str(app.get("name") or "").strip() or "导入工作流"
    note = (payload.note or "").strip() or str(app.get("description") or "").strip()
    workflow = STORE.save_workflow(name, json.dumps(dsl_obj, ensure_ascii=False),
                                   mode=mode, note=note)
    return {"workflow": workflow,
            "inspect": {"load_status": info.get("load_status"), "kind": info.get("kind"),
                        "mode": mode, "node_count": info.get("node_count"),
                        "edge_count": info.get("edge_count")}}


@app.delete("/api/orchestration/workflows/{workflow_id}")
async def orchestration_workflow_delete(workflow_id: str,
                                        user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    STORE.delete_workflow(workflow_id)
    return {"ok": True}


# ================================================================ 质量治理

@app.get("/api/gov/summary")
async def gov_summary(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return _gov_summary()


@app.get("/api/gov/scorecard")
async def gov_scorecard(user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"rows": _gov_rows()}


@app.get("/api/gov/defects")
async def gov_defects(blame: str = "", status: str = "",
                      user: Dict[str, str] = Depends(current_user)) -> Dict[str, Any]:
    return {"defects": _gov_defects()}


class DefectStatusPayload(BaseModel):
    status: str
    owner: str = ""
    due_date: str = ""


@app.post("/api/gov/defects/{defect_id}/status")
async def gov_defect_status(defect_id: str, payload: DefectStatusPayload,
                            user: Dict[str, str] = Depends(current_admin)) -> Dict[str, Any]:
    status = payload.status or "待整改"
    STORE.upsert_defect_state(defect_id, status, payload.owner, payload.due_date)
    return {"defect_id": defect_id, "status": status, "owner": payload.owner,
            "due_date": payload.due_date}


def _gov_defects() -> List[Dict[str, Any]]:
    """缺陷整改单：来源 = 覆盖缺口（D1）+ 人工标注（严重度 P0/P1/P2）。

    整改处置（状态/责任单位/期限）以 defect_states 表为准，未登记时回落到来源默认值。
    """
    states = STORE.defect_states()
    # 覆盖缺口是按知识库登记的，没有具体文档：用知识库名代替原始 id 展示
    dataset_names = {item["id"]: item["name"] for item in STORE.list_datasets()}
    out: List[Dict[str, Any]] = []
    for gap in STORE.list_gaps():
        dataset_id = gap.get("dataset_id") or ""
        out.append({
            "defect_id": gap["id"],
            "severity": "P1",
            "doc_no": (dataset_names.get(dataset_id) or ("（知识库已删除）" if dataset_id else ""))[:40],
            "doc_version": "",
            "defect_label": "覆盖缺口：" + "、".join(gap.get("keywords") or [])[:60],
            "blame": "文档",
            "occurrences": 1,
            "question": gap.get("question") or "",
            "action": gap.get("suggestion") or "补充对应条款后重新入库",
            "status": gap.get("status") or "待整改",
            "owner": gap.get("owner") or "",
            "due_date": "",
            "task": "覆盖缺口（问答未命中自动登记）",
            "completeness": "missing",
            "compliance": "",
        })
    for task in STORE.list_anno_tasks():
        for item in STORE.list_anno_items(task["id"], status="done", limit=1000):
            labels = item["labels"] or {}
            severity = labels.get("severity") or "none"
            dims = labels.get("defect_dimensions") or []
            if severity == "none" and not dims:
                continue
            out.append({
                "defect_id": item["id"],
                "severity": severity if severity != "none" else "P2",
                "doc_no": (item.get("doc_name") or "")[:60],
                "doc_version": "",
                "defect_label": "、".join(dims) or "标注缺陷",
                "blame": "文档",
                "occurrences": 1,
                "question": "",
                "action": item.get("comment") or "按标注意见整改并复核",
                "status": "待整改",
                "owner": item.get("annotator") or "",
                "due_date": "",
                "task": task["name"],
                "completeness": labels.get("completeness") or "",
                "compliance": labels.get("compliance") or "",
            })
    for item in out:
        state = states.get(item["defect_id"])
        if state:
            item["status"] = state["status"]
            if state["owner"]:
                item["owner"] = state["owner"]
            item["due_date"] = state["due_date"]
    return out


def _gov_rows() -> List[Dict[str, Any]]:
    receipts = STORE.receipt_summary()
    defects = _gov_defects()
    rows: List[Dict[str, Any]] = []
    for dataset in STORE.list_datasets():
        docs = STORE.list_documents(dataset["id"])
        for document in docs:
            related = [item for item in defects if item["doc_no"] == document["name"][:60]]
            p0 = sum(1 for item in related if item["severity"] == "P0")
            p1 = sum(1 for item in related if item["severity"] == "P1")
            p2 = sum(1 for item in related if item["severity"] == "P2")
            probes = len(related)
            density = (probes / document["chunk_count"]) if document["chunk_count"] else None
            rows.append({
                "doc_no": (document["meta_fields"] or {}).get("doc_no") or document["name"][:40],
                "name": document["name"], "version": (document["meta_fields"] or {}).get("version") or "",
                "probes": probes,
                "csi_mean": receipts["mean"] if probes else None,
                "clause_total": document["chunk_count"], "p0": p0, "p1": p1, "p2": p2,
                "density": density,
                "dqi": (round(100 * (1 - density), 1) if density is not None else None),
                "ci95": None, "sample_ok": probes >= 8,
                "top_defect": related[0]["defect_label"] if related else "",
            })
    return rows


def _gov_summary() -> Dict[str, Any]:
    defects = _gov_defects()
    receipts = STORE.receipt_summary()
    return {
        "evaluations": receipts["count"],
        "csi_mean": receipts["mean"],
        "defects": {"doc": sum(1 for item in defects if item["blame"] == "文档"),
                    "corpus": 0,
                    "system": 0},
        "closure_rate": 0.0,
        "scorecard": _gov_rows(),
    }


# ================================================================ 静态前端

DIST = Path(ENV.get("PORTAL_DIST", r"D:\llm-stack\src\portal\web-modao\equipment-portal.vue\dist"))


class PortalFiles(StaticFiles):
    """门户静态资源：index.html 每次回源校验，带内容哈希的 assets 才可长缓存。

    不设缓存头时浏览器按启发式规则长期复用 index.html —— 前端已经重建、
    用户却一直看到旧包（2026-09-28 实际踩到：页面还停在旧版图例与旧提示文案）。
    """

    async def get_response(self, path: str, scope):  # type: ignore[override]
        response = await super().get_response(path, scope)
        # 用请求路径判断，而不是 StaticFiles 解析后的 path（"/" 会解析成 index.html，语义丢失）
        request_path = str(scope.get("path") or "").lower()
        tail = request_path.rsplit("/", 1)[-1]
        if tail == "" or tail.endswith(".html") or "." not in tail:
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        else:
            # dist 里的 js/css 都带内容哈希，可放心长期缓存
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if DIST.exists():
    app.mount("/", PortalFiles(directory=str(DIST), html=True), name="portal")


@app.on_event("startup")
async def _startup() -> None:
    # 两个内核按需常驻：启动时在后台线程预拉起，避免首次调用才出现进程，同时不阻塞 uvicorn 启动
    threading.Thread(target=warmup_kernels, daemon=True, name="kernel-warmup").start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    stop_kernels()
