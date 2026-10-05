"""检索契约层自测：协议、统一片段、引用契约、版本硬过滤。

无凭据、无网络也能跑：只依赖 samples/ 与 demo_corpus/ 两个本地目录。
    python kb_bridge/selftest.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "eval_service"))
sys.path.insert(0, str(ROOT / "kb_bridge"))

from chunk_index import ChunkIndex, load_ledger_current  # noqa: E402

CITATION_RE = re.compile(r"^HYS-[A-Z]{2}-\d{3} V\d+\.\d+ §[\d.]+$")

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """断言并累计结果。"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def build() -> ChunkIndex:
    """构建演示索引（samples + demo_corpus）。"""
    ledger = load_ledger_current(ROOT / "samples")
    index = ChunkIndex()
    for directory in (ROOT / "samples", ROOT / "demo_corpus"):
        index.chunks.extend(ChunkIndex.build(directory, ledger).chunks)
    index._build_stats()
    return index


def main() -> int:
    """执行自测。"""
    print("检索契约层自测")
    index = build()
    stats = index.stats()

    print("\n[1] 索引构建")
    check("条款片段已切分", stats["chunks"] >= 280, str(stats))
    check("资料份数正确", stats["documents"] == 6, str(stats))
    check("作废片段被识别（演示语料 2 条）", stats["obsolete_chunks"] == 2, str(stats))

    print("\n[2] 版本硬过滤（默认只召回现行有效）")
    res_clean = index.search("滤芯每 300 工作小时更换一次吗", top_k=10)
    check("默认检索不含作废片段", all(r.chunk.status == "现行有效" for r in res_clean),
          str([r.chunk.status for r in res_clean]))
    res_all = index.search("滤芯每 300 工作小时更换一次吗", top_k=10, include_obsolete=True)
    check("放开过滤后应出现作废片段", any(r.chunk.status != "现行有效" for r in res_all))
    check("作废片段在放开过滤时为 Top1（正是要防的失败模式）",
          bool(res_all) and res_all[0].chunk.status != "现行有效",
          res_all[0].chunk.citation if res_all else "无结果")
    check("过滤后 Top1 为现行版本", bool(res_clean) and res_clean[0].chunk.status == "现行有效")

    print("\n[3] 统一片段与引用契约")
    record = res_clean[0].chunk.to_unified(res_clean[0].score)
    check("record 含 content", bool(record.get("content")))
    check("score 在 0~1 之间", 0.0 <= record["score"] <= 1.0, str(record["score"]))
    check("title 为「资料编号 版本号」", re.match(r"^HYS-[A-Z]{2}-\d{3} V\d+\.\d+$", record["title"]) is not None,
          record["title"])
    check("引用契约格式正确", CITATION_RE.match(record["metadata"]["citation"]) is not None,
          record["metadata"]["citation"])
    check("metadata 为对象（Dify 要求不得为 null）", isinstance(record["metadata"], dict))
    check("metadata 含 chunk_id 与 status", bool(record["metadata"].get("chunk_id")) and bool(record["metadata"].get("status")))

    print("\n[4] 检索参数")
    top2 = index.search("滤芯更换周期", top_k=2)
    check("top_k 生效", len(top2) == 2, str(len(top2)))
    check("分数降序", all(top2[i].score >= top2[i + 1].score for i in range(len(top2) - 1)))
    thr = index.search("滤芯更换周期", top_k=10, score_threshold=0.30)
    check("score_threshold 生效", all(r.score >= 0.30 for r in thr), str([round(r.score, 2) for r in thr]))
    limited = index.search("滤芯更换周期", top_k=10, doc_nos=["HYS-CZ-002"])
    check("可按资料编号过滤", all(r.chunk.doc_no == "HYS-CZ-002" for r in limited))
    empty = index.search("", top_k=5)
    check("空查询返回空结果", empty == [])

    print(f"\n自测结束：通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
