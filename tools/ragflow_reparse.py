# -*- coding: utf-8 -*-
"""按新的分段参数重建 RAGFlow 数据集索引（提升检索精度与可溯源性）。

为什么要重建：
    当前 chunk_token_num=512 会把相邻小节合并成一个片段。实测（HYS-WX-003 维修规程）：
    「§2 保养周期（含大表格）」与「§3 滤芯更换作业步骤（3.1.1~3.1.9）」被并进同一个 868 字片段，
    片段向量被表格与保养主题"平均化"，问题很难精确落到具体条款上。
    把 chunk_token_num 降到 256（保留 "\n## " 分隔符）后每个小节独立成片段，
    既提升精度，也让"引用到条款"具备可行的数据结构基础（对应 11 号报告 F4）。

用法：
    python tools/ragflow_reparse.py --dataset <dataset_id>                    # 只看现状
    python tools/ragflow_reparse.py --dataset <dataset_id> --chunk-token-num 256 --apply
    python tools/ragflow_reparse.py --dataset <dataset_id> --apply --poll-timeout 1800
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(r"E:\llm-stack\.env")
DEFAULT_BASE = "http://127.0.0.1:9380"


def load_env() -> dict:
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def api(base: str, key: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=300).read().decode())
    except urllib.error.HTTPError as exc:
        return {"code": exc.code, "message": exc.read().decode()[:300]}


def main() -> int:
    parser = argparse.ArgumentParser(description="重建 RAGFlow 数据集索引（分段参数调整）")
    parser.add_argument("--dataset", required=True, help="数据集 ID")
    parser.add_argument("--chunk-token-num", type=int, default=256, help="目标分段长度（默认 256）")
    parser.add_argument("--delimiter", default=None, help="分段分隔符（默认沿用现状，通常为 \\n## ）")
    parser.add_argument("--apply", action="store_true", help="真正写入配置并触发重新解析")
    parser.add_argument("--poll-timeout", type=int, default=1800, help="等待解析完成的最长秒数")
    parser.add_argument("--poll-interval", type=int, default=15, help="轮询间隔秒数")
    args = parser.parse_args()

    env = load_env()
    base = (env.get("RAGFLOW_BASE_URL") or DEFAULT_BASE).rstrip("/")
    key = env.get("RAGFLOW_API_KEY", "")
    if not key:
        print("[FATAL] 缺少 RAGFLOW_API_KEY")
        return 2

    listing = api(base, key, "GET", "/api/v1/datasets?page=1&page_size=100")
    target = next((d for d in (listing.get("data") or []) if d.get("id") == args.dataset), None)
    if not target:
        print("[FATAL] 未找到数据集", args.dataset)
        return 2
    current = target.get("parser_config") or {}
    print("数据集：%s" % target.get("name"))
    print("当前：chunk_token_num=%s delimiter=%r 文档=%s 片段=%s token=%s"
          % (current.get("chunk_token_num"), current.get("delimiter"),
             target.get("document_count"), target.get("chunk_count"), target.get("token_num")))

    new_config = {"chunk_token_num": args.chunk_token_num}
    if args.delimiter:
        new_config["delimiter"] = args.delimiter
    print("目标：chunk_token_num=%s%s" % (args.chunk_token_num,
                                       (" delimiter=%r" % args.delimiter) if args.delimiter else "（分隔符不变）"))
    if not args.apply:
        print("[dry-run] 未写入；加 --apply 生效（会触发全部文档重新解析，耗时数分钟）")
        return 0

    updated = api(base, key, "PUT", f"/api/v1/datasets/{args.dataset}",
                  {"parser_config": new_config})
    print("更新配置：", updated.get("code"), str(updated.get("message"))[:80])

    docs = api(base, key, "GET", f"/api/v1/datasets/{args.dataset}/documents?page=1&page_size=200")
    doc_ids = [d.get("id") for d in ((docs.get("data") or {}).get("docs") or [])]
    if not doc_ids:
        print("[FATAL] 数据集内没有文档")
        return 2
    print("触发重新解析：%d 个文档" % len(doc_ids))
    started = api(base, key, "POST", f"/api/v1/datasets/{args.dataset}/chunks",
                  {"document_ids": doc_ids})
    print("解析请求：", started.get("code"), str(started.get("message"))[:80])

    deadline = time.time() + args.poll_timeout
    while time.time() < deadline:
        time.sleep(args.poll_interval)
        docs = api(base, key, "GET", f"/api/v1/datasets/{args.dataset}/documents?page=1&page_size=200")
        items = (docs.get("data") or {}).get("docs") or []
        pending = [d for d in items if str(d.get("run")) not in ("DONE", "3")]
        chunks = sum(int(d.get("chunk_count") or 0) for d in items)
        print("  进度：完成 %d/%d，片段合计 %d" % (len(items) - len(pending), len(items), chunks))
        if not pending:
            result = api(base, key, "GET", f"/api/v1/datasets?page=1&page_size=100")
            now = next((d for d in (result.get("data") or []) if d.get("id") == args.dataset), {})
            print("解析完成：片段 %s（原 %s），token %s（原 %s）"
                  % (now.get("chunk_count"), target.get("chunk_count"),
                     now.get("token_num"), target.get("token_num")))
            print("下一步：python tools/eval_retrieval.py --label after-reparse 对比效果")
            return 0
    print("[WARN] 等待超时，请稍后用 GET /api/v1/datasets/{id}/documents 复查解析状态")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
