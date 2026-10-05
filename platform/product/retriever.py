"""混合检索（产品自研）：向量余弦 + 词项重合，打分口径与 RAGFlow 的 vector_similarity_weight 一致。

  hybrid = vector_weight × 向量相似度 + (1 − vector_weight) × 词项相似度

向量来自产品库（嵌入模型由配置决定），词项来自 RAGFlow 分词器（知识内核算），
因此"检索口径"和"入库口径"同源。

**可选的重排阶段**（docs/16 D53）：混合检索负责"把对的切片捞进来"，重排负责"把对的排到第一"。
配置了重排角色（`MODEL_RERANK_*` 或门户绑定的 rerank 接入配置）且 `RERANK_MODE` 非 off 时，
先按 `top_k × RERANK_CANDIDATE_FACTOR` 放大召回（阈值放宽到 `min(threshold, 0.1)`），
交给交叉编码器逐对打分，再按重排分截取 top_k；重排服务不可用时**静默回落**混合排序，
绝不因为重排挂掉而让问答失败。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

import numpy as np

from . import kernels
from .embedder import ModelError, ModelGateway
from .graph import annotate_communities, build_graph_entities, merge_similar_entities
from .lexicon import parse_lexicon
from .store import Store

#: 图谱启用策略 = auto 时的触发条件（与 docs/16 D9·D15、旧 BFF `KG_TRIGGER_RE` 同口径）：
#: 只对"哪些 / 涉及 / 对比 / 汇总 / 清单"等跨文档·多跳类问题启用图谱。
KG_TRIGGER_RE = re.compile(r"哪些资料|哪几份|涉及|关联|跨文档|对比|区别|差异|汇总|清单|都有|一共有")


def kg_needed(question: str, mode: str = "auto") -> bool:
    """图谱启用策略：off/never 关、on/always 开，其余（默认 auto）按问题意图判定。"""
    mode = (mode or "auto").strip().lower()
    if mode in ("off", "false", "0", "never", "no"):
        return False
    if mode in ("on", "true", "1", "always", "yes"):
        return True
    return bool(KG_TRIGGER_RE.search(str(question or "")))


#: 重排角色的配置判据与 `/api/settings` 的凭据判据同一口径（url + 模型名都非空）。
RERANK_OFF_VALUES = ("off", "false", "0", "never", "no", "none")


def rerank_configured(gateway: ModelGateway) -> bool:
    """重排角色是否配置齐。"""
    config = gateway.resolve("rerank")
    return bool(config.get("base_url") and config.get("name"))


def rerank_enabled(gateway: ModelGateway, use_rerank: Optional[bool] = None) -> bool:
    """重排开关：显式参数优先；否则「配置了就用，`RERANK_MODE=off` 才关」（与 `KG_MODE` 同一风格）。"""
    if not rerank_configured(gateway):
        return False
    if use_rerank is not None:
        return bool(use_rerank)
    mode = (gateway.env.get("RERANK_MODE", "auto") or "auto").strip().lower()
    return mode not in RERANK_OFF_VALUES


def _apply_rerank(gateway: ModelGateway, question: str, rows: List[Dict[str, Any]],
                  hybrid: Any, order: Any, top_k: int, threshold: float):
    """放大召回 → 交叉编码器重排；返回 (新的切片顺序, {行号: 重排分})。"""
    factor = max(1, int(gateway.env.get("RERANK_CANDIDATE_FACTOR", "4") or 4))
    candidates = int(min(len(order), max(top_k * factor, top_k)))
    # 阈值放宽到 min(threshold, 0.1)：重排的意义就是从"混合分偏低但语义相关"的切片里挑出对的，
    # 沿用原阈值会把候选池卡死（与旧 BFF EXT_KB_RERANK 的放大口径一致）。
    floor = min(float(threshold), 0.1)
    picked = [int(index) for index in order[:candidates] if float(hybrid[int(index)]) >= floor]
    if len(picked) <= 1:
        return list(order), {}
    limit = max(200, int(gateway.env.get("RERANK_DOC_MAX_CHARS", "1200") or 1200))
    documents = [str(rows[index]["content"])[:limit] for index in picked]
    result = gateway.rerank(question, documents, top_n=len(picked))
    scores: Dict[int, float] = {}
    ranked: List[int] = []
    for item in result.get("results") or []:
        position = int(item.get("index") or 0)
        if not 0 <= position < len(picked):
            continue
        row_index = picked[position]
        if row_index in scores:
            continue
        ranked.append(row_index)
        scores[row_index] = float(item.get("relevance_score") or 0.0)
    if not ranked:
        return list(order), {}
    ranked_set = set(ranked)
    tail = [int(index) for index in order if int(index) not in ranked_set]
    return ranked + tail, scores


def retrieve(store: Store, gateway: ModelGateway, question: str, dataset_ids: List[str],
             top_k: int = 10, threshold: float = 0.2, vector_weight: float = 0.3,
             use_kg: bool = False, document_ids: Optional[List[str]] = None,
             use_rerank: Optional[bool] = None) -> Dict[str, Any]:
    """返回 {chunks: [...], total, elapsed_ms, mode}。"""
    started = time.perf_counter()
    question = (question or "").strip()
    if not question or not dataset_ids:
        return {"chunks": [], "total": 0, "elapsed_ms": 0, "mode": "empty"}

    query_vector = np.asarray(gateway.embed_one(question), dtype=np.float32)
    rows = store.vectors(dataset_ids)
    if document_ids:
        allowed = set(document_ids)
        rows = [row for row in rows if row["document_id"] in allowed]
    if not rows:
        return {"chunks": [], "total": 0, "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "mode": "no-data"}

    matrix = np.vstack([row["vector"] for row in rows])
    if matrix.shape[1] != query_vector.shape[0]:
        raise ValueError(f"向量维度不一致：库内 {matrix.shape[1]}，查询 {query_vector.shape[0]}（请重建索引）")
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query_vector) or 1.0)
    norms[norms == 0] = 1e-9
    vector_scores = (matrix @ query_vector) / norms
    vector_scores = np.clip(vector_scores, 0.0, 1.0)

    query_terms = _query_terms(question)
    term_scores = np.zeros(len(rows), dtype=np.float32)
    for index, row in enumerate(rows):
        tokens = set(_terms(row.get("tokens") or row.get("content") or ""))
        if not query_terms or not tokens:
            continue
        overlap = query_terms & tokens
        term_scores[index] = len(overlap) / (len(query_terms) ** 0.5 * len(tokens) ** 0.5) * 3.0
    term_scores = np.clip(term_scores, 0.0, 1.0)

    hybrid = vector_weight * vector_scores + (1.0 - vector_weight) * term_scores
    order = np.argsort(-hybrid)

    # 候选池：**全部切片**（不受 threshold 限制），供图谱按实体链接回捞；
    # 向量/词项命中仍按 threshold + top_k 截取，图谱命中不另造分数、沿用切片真实混合得分。
    scored: Dict[str, Dict[str, Any]] = {}
    for index in order:
        score = float(hybrid[index])
        row = rows[int(index)]
        scored[row["id"]] = {"row": row, "score": score,
                             "vector": float(vector_scores[index]),
                             "term": float(term_scores[index])}

    # ---------------------------------------------------------------- 重排（可选）
    rank_order: List[int] = [int(index) for index in order]
    rerank_scores: Dict[int, float] = {}
    rerank_note = ""
    if rerank_enabled(gateway, use_rerank):
        try:
            rank_order, rerank_scores = _apply_rerank(gateway, question, rows, hybrid, order,
                                                      top_k, threshold)
            rerank_note = "+rerank"
        except ModelError as exc:  # 重排挂了不能拖垮问答：回落混合排序并在 mode 里如实标注
            rerank_note = "+rerank-failed"
            print(f"[retrieve] 重排不可用，回落混合排序：{exc}", flush=True)

    hits: List[Dict[str, Any]] = []
    for index in rank_order:
        score = float(hybrid[index])
        row = rows[int(index)]
        # 参与过重排的候选已把阈值放宽到 min(threshold, 0.1)，此处不再用原阈值把它们挡回去；
        # 未参与重排的切片仍按原阈值过滤，口径与不开重排时逐字一致。
        if score < threshold and index not in rerank_scores:
            continue
        if len(hits) >= top_k:
            break
        payload = row.get("meta") or {}
        hit = {
            "id": row["id"],
            "content": row["content"],
            "document_id": row["document_id"],
            "document_name": row.get("doc_name") or "",
            "document_keyword": row.get("doc_name") or "",
            "dataset_id": row["dataset_id"],
            "similarity": round(score, 6),
            "vector_similarity": round(float(vector_scores[index]), 6),
            "term_similarity": round(float(term_scores[index]), 6),
            "important_keywords": row.get("keywords") or [],
            "metadata": payload,
            "doc_no": payload.get("doc_no") or "",
            "version": payload.get("version") or "",
        }
        if index in rerank_scores:
            hit["rerank_score"] = round(float(rerank_scores[index]), 6)
        hits.append(hit)

    # 图谱命中与向量/词项命中**合并**（按 id 去重、按真实得分排序后截断 top_k）。
    # 旧实现用「混合命中自己的 id 集合」过滤混合命中，导致图谱一旦有结果就把正常结果全部丢弃。
    kg_hits = _graph_hits(store, dataset_ids, question, scored) if use_kg else []
    if kg_hits:
        merged = {hit["id"]: hit for hit in hits}
        for hit in kg_hits:
            merged.setdefault(hit["id"], hit)
        if rerank_scores:
            # 重排生效时按**重排后的名次**合并（图谱回捞的切片也有真实名次，不会被打回原处）
            position = {rows[index]["id"]: rank for rank, index in enumerate(rank_order)}
            hits = sorted(merged.values(), key=lambda hit: position.get(hit["id"], 10 ** 6))[:top_k]
        else:
            hits = sorted(merged.values(), key=lambda hit: hit["similarity"], reverse=True)[:top_k]

    return {
        "chunks": hits,
        "total": len(hits),
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "mode": "hybrid" + ("+kg" if use_kg and kg_hits else "") + rerank_note,
        "weights": {"vector": vector_weight, "term": round(1 - vector_weight, 4)},
        "candidates": len(rows),
        "reranked": len(rerank_scores),
    }


def _query_terms(question: str) -> set:
    """查询侧词项：**直接用知识内核（RAGFlow）分词器**，与入库口径同源。

    旧实现把原始问句交给 `_terms()`，而 `_terms()` 的「已分词」判断恒为真
    （含空格即整段切分），导致「发射机冷却液液位低于 1/3 时能否短时开机运行？」
    这类含空格的中文问句被切成 3 个巨型词项、与库内任意切片零重合 →
    词项得分恒为 0，混合分只剩 0.3 的向量项，相关问句几乎必然低于阈值而召回为空。
    """
    try:
        tokenized = kernels.tokenize_batch([question])[0]
    except Exception:  # noqa: BLE001
        tokenized = ""
    terms = {word for word in (tokenized or "").split() if word}
    return terms or set(_terms(question))


def _terms(text: str) -> List[str]:
    """词项切分：优先用内核分词结果（已带空格），否则退化为字符二元组。"""
    if not text:
        return []
    if " " in text and not any("\u4e00" <= ch <= "\u9fff" and " " in text for ch in ""):
        words = [word for word in text.split() if word]
        if words and all(len(word) <= 24 for word in words[:50]):
            return words
    out: List[str] = []
    buffer = ""
    for char in text:
        if "\u4e00" <= char <= "\u9fff":
            buffer += char
        else:
            if buffer:
                out.extend(buffer[i:i + 2] for i in range(max(1, len(buffer) - 1)))
                buffer = ""
            if char.isalnum():
                out.append(char.lower())
    if buffer:
        out.extend(buffer[i:i + 2] for i in range(max(1, len(buffer) - 1)))
    return out


def _graph_hits(store: Store, dataset_ids: List[str], question: str,
                scored: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """图谱检索：实体名与问题做子串匹配 → 回捞该实体覆盖切片的真实混合得分 top-2。

    修正点：① 匹配方式由「字符二元组精确等于实体全名」改为「实体名是问题子串」
    （长度 ≥2，ASCII 忽略大小写）——原逻辑对中文几乎必然空转；② 相似度不再写死 0.99，
    直接沿用该切片在混合检索里的真实得分；③ 文档名 / 文号 / 版本从检索行补齐。
    """
    if not question or not scored:
        return []
    probe = question.lower()
    hits: List[Dict[str, Any]] = []
    seen: set = set()
    for dataset_id in dataset_ids:
        entries = [entry for entry in scored.values()
                   if entry["row"]["dataset_id"] == dataset_id]
        if not entries:
            continue
        graph = store.get_graph(dataset_id)
        for node in graph.get("nodes") or []:
            name = str(node.get("name") or "").strip()
            if len(name) < 2:
                continue
            needle = name.lower()
            if needle not in probe:
                continue
            matched = [entry for entry in entries
                       if needle in str(entry["row"].get("content") or "").lower()
                       or any(needle in str(keyword).lower()
                              for keyword in (entry["row"].get("keywords") or []))]
            matched.sort(key=lambda entry: entry["score"], reverse=True)
            for entry in matched[:2]:
                row = entry["row"]
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                payload = row.get("meta") or {}
                hits.append({
                    "id": row["id"],
                    "content": row["content"],
                    "document_id": row["document_id"],
                    "document_name": row.get("doc_name") or "",
                    "document_keyword": row.get("doc_name") or "",
                    "dataset_id": dataset_id,
                    "similarity": round(entry["score"], 6),
                    "vector_similarity": round(entry["vector"], 6),
                    "term_similarity": round(entry["term"], 6),
                    "important_keywords": row.get("keywords") or [],
                    "metadata": {"source": "graph", "entity": name},
                    "doc_no": payload.get("doc_no") or "",
                    "version": payload.get("version") or "",
                })
    return hits


def build_graph(store: Store, dataset_id: str, max_nodes: int = 400, max_edges: int = 800,
                graphdb: Any = None, gateway: Any = None, per_node: int = 6) -> Dict[str, Any]:
    """知识图谱构建流水线（**全程不用生成式大模型**）：

      1. 词典驱动：从语料自带的对照表/版本台账解析权威实体与关系（部件↔代号、缩略语含义、
         资料↔现行版本、已作废版本）；
      2. 规则抽词 + 切片共现：得到同文档 / 跨资料的关系；
      3. 嵌入归并：只用嵌入模型（bge-m3）算相似度，合并同义实体；
      4. 社区发现：纯图算法，给节点打社区号，供前端按社区着色/聚合；
      5. 同时写入图数据库（Neo4j）与本地库：本地保证离线可用，Neo4j 提供 Cypher 查询能力。
    """
    chunks = store.list_chunks(dataset_id=dataset_id, limit=5000)
    lexicon = parse_lexicon(chunks)
    result = build_graph_entities(chunks, top_n=max_nodes, max_edges=max_edges, lexicon=lexicon,
                                  per_node=per_node)
    if gateway is not None:
        merged_nodes, merged_edges, merge_stats = merge_similar_entities(
            result["nodes"], result["edges"], gateway, protected=set(lexicon.terms))
        result["nodes"], result["edges"] = merged_nodes, merged_edges
        result["stats"]["merge"] = merge_stats
        result["stats"]["node_count"] = len(merged_nodes)
        result["stats"]["edge_count"] = len(merged_edges)
    annotate_communities(result)
    nodes = [{"id": item["id"], "name": item["name"], "category": item.get("category") or "术语",
              "weight": float(item["value"]), "community": item.get("community") or ""}
             for item in result["nodes"]]
    store.replace_graph(dataset_id, nodes, result["edges"])
    payload: Dict[str, Any] = {"nodes": result["nodes"], "edges": result["edges"],
                               "stats": result["stats"], "entity_count": len(result["nodes"]),
                               "relation_count": len(result["edges"]), "graphdb": None}
    if graphdb is not None and getattr(graphdb, "enabled", False):
        dataset = store.get_dataset(dataset_id) or {}
        try:
            stats = graphdb.replace_dataset_graph(dataset_id, dataset.get("name") or dataset_id,
                                                  result["nodes"], result["edges"])
            payload["graphdb"] = {"backend": "neo4j", "ok": True, **stats}
        except Exception as exc:  # noqa: BLE001
            payload["graphdb"] = {"backend": "neo4j", "ok": False, "error": str(exc)[:300]}
    return payload
