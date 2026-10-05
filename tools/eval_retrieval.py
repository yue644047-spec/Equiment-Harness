# -*- coding: utf-8 -*-
"""检索效果评测（RAG 召回质量）：用带「必需事实」的金标集量化检索是否够用。

为什么用「必需事实命中」而不是纯相似度：
    检索的目标不是分数好看，而是**答案所需的事实能否被召回**。
    每条题目标注若干个必须出现在召回片段里的关键事实（数值/编号/代码），
    另可标注期望命中的资料编号——两者都能确定性判定，不需要人工打分。

指标：
    fact_hit     必需事实全部被召回的题目占比（最关键：召回是否足够作答）
    fact_partial 至少命中一半事实的题目占比
    doc_hit      期望资料编号出现在召回结果里的题目占比（有标注时）
    top1_doc     首条结果命中期望资料的题目占比
    recall@k     平均召回的片段数
    latency      平均检索耗时（秒）

用法：
    python tools/eval_retrieval.py                     # 用当前线上默认配置跑一遍
    python tools/eval_retrieval.py --label baseline     # 打标签，便于对比
    python tools/eval_retrieval.py --keyword --top-k 10 --vector-weight 0.2
    python tools/eval_retrieval.py --no-kg
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "eval_service" / "retrieval_gold.jsonl"
PORTAL = "http://127.0.0.1:8090"


def env_values() -> dict:
    values = {}
    for line in Path(r"E:\llm-stack\.env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def login(opener) -> None:
    body = json.dumps({"username": "admin", "password": "admin123"}).encode()
    req = urllib.request.Request(PORTAL + "/api/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    opener.open(req, timeout=60).read()


def retrieve(opener, dataset_id: str, question: str, args) -> tuple[list[dict], float]:
    payload = {
        "question": question,
        "dataset_ids": [dataset_id],
        "top_k": args.top_k,
        "similarity_threshold": args.threshold,
        "vector_weight": args.vector_weight,
    }
    # use_kg 三态：--no-kg 强制关、--kg 强制开、都不给则交给服务端策略（EXT_KB_USE_KG=auto）
    if args.no_kg:
        payload["use_kg"] = False
    elif args.kg:
        payload["use_kg"] = True
    if args.keyword:
        payload["keyword"] = True
    req = urllib.request.Request(PORTAL + "/api/kb/retrieval-test",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    try:
        data = json.loads(opener.open(req, timeout=600).read().decode())
    except urllib.error.HTTPError as exc:
        print("  [warn] 检索失败：", exc.code, exc.read().decode()[:160])
        return [], time.time() - started
    chunks = data.get("chunks") or data.get("data") or []
    return chunks, time.time() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="检索效果评测（必需事实命中率）")
    parser.add_argument("--gold", default=str(GOLD))
    parser.add_argument("--label", default="current")
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--threshold", type=float, default=0.2)
    parser.add_argument("--vector-weight", type=float, default=0.3)
    parser.add_argument("--keyword", action="store_true", help="开启 RAGFlow 关键词提取（query 增强）")
    parser.add_argument("--no-kg", action="store_true", help="强制关闭 GraphRAG 图谱检索")
    parser.add_argument("--kg", action="store_true", help="强制开启 GraphRAG（默认交给服务端 auto 策略）")
    parser.add_argument("--json-out", default=None, help="把明细写到该 JSON 文件")
    parser.add_argument("--dataset", default=None, help="数据集 ID（默认取 .env 的 RAGFLOW_DATASET_ID）")
    args = parser.parse_args()

    env = env_values()
    dataset_id = args.dataset or env.get("RAGFLOW_DATASET_ID", "")
    cases = [json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines() if line.strip()]

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login(opener)

    rows = []
    for case in cases:
        chunks, elapsed = retrieve(opener, dataset_id, case["question"], args)
        blob = "\n".join(str(c.get("content") or "") for c in chunks)
        cites = [str(c.get("citation") or c.get("document_keyword") or "") for c in chunks]
        facts = [f for f in case.get("required_facts") or []]
        hit = [f for f in facts if f in blob]
        docs = [d for d in case.get("expected_docs") or []]
        doc_hit = [d for d in docs if any(d in c for c in cites)]
        rows.append({
            "id": case["id"], "question": case["question"], "n_chunks": len(chunks),
            "facts": facts, "facts_hit": hit, "docs": docs, "docs_hit": doc_hit,
            "top1": cites[0] if cites else "", "elapsed": round(elapsed, 2),
            "fact_all": bool(facts) and len(hit) == len(facts),
            "fact_half": bool(facts) and len(hit) * 2 >= len(facts),
            "doc_all": (not docs) or len(doc_hit) == len(docs),
            "top1_doc": bool(docs) and bool(cites) and any(d in cites[0] for d in docs),
        })

    total = len(rows)
    fact_all = sum(1 for r in rows if r["fact_all"])
    fact_half = sum(1 for r in rows if r["fact_half"])
    doc_all = sum(1 for r in rows if r["doc_all"])
    top1_doc = sum(1 for r in rows if r["top1_doc"])
    latency = sum(r["elapsed"] for r in rows) / total if total else 0
    chunks_avg = sum(r["n_chunks"] for r in rows) / total if total else 0

    kg_label = "off" if args.no_kg else ("on" if args.kg else "auto")
    print(f"=== 检索评测 [{args.label}] top_k={args.top_k} thr={args.threshold} "
          f"vw={args.vector_weight} kg={kg_label} keyword={args.keyword} ===")
    for r in rows:
        mark = "OK  " if r["fact_all"] else ("部分" if r["facts_hit"] else "MISS")
        print(f"  [{mark}] {r['id']} {r['question'][:26]:<28} 事实 {len(r['facts_hit'])}/{len(r['facts'])}"
              f" 片段 {r['n_chunks']} 首条 {r['top1'][:22]}")
    print("-" * 78)
    print(f"必需事实全中    : {fact_all}/{total}  ({fact_all / total:.0%})")
    print(f"至少命中一半    : {fact_half}/{total}  ({fact_half / total:.0%})")
    print(f"期望资料全中    : {doc_all}/{total}  ({doc_all / total:.0%})")
    print(f"首条命中期望资料: {top1_doc}/{total}  ({top1_doc / total:.0%})")
    print(f"平均片段数      : {chunks_avg:.1f}   平均耗时: {latency:.2f}s")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("明细已写出：", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
