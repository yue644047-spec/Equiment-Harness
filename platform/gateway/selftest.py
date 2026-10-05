r"""网关自检：两个内核通过统一 HTTP 契约可用（真实调用，不打桩）。

用法：& $BFF_PYTHON platformgatewayselftest.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fastapi.testclient import TestClient  # noqa: E402

import main as gateway  # noqa: E402

REPO = HERE.parents[1]  # D:\app\dify
DOCX_CANDIDATES = [
    Path(r"D:\app\审价\1、385万（4号文）\交付成果\06_开发计划.docx"),
    *sorted(REPO.glob("platform/data/files/*/06_开发计划.docx")),
]
DOCX = next((path for path in DOCX_CANDIDATES if path.exists()), DOCX_CANDIDATES[0])
DSL = {
    "kind": "app",
    "app": {"mode": "workflow", "name": "网关自检链"},
    "workflow": {
        "graph": {
            "nodes": [
                {"id": "start", "type": "custom", "data": {"type": "start", "title": "开始",
                 "variables": [{"variable": "question", "label": "问题", "type": "text-input", "required": True}]}},
                {"id": "tpl", "type": "custom", "data": {"type": "template-transform", "title": "拼装",
                 "template": "网关链路：{{ question }}",
                 "variables": [{"variable": "question", "value_selector": ["start", "question"]}]}},
                {"id": "end", "type": "custom", "data": {"type": "end", "title": "结束",
                 "outputs": [{"variable": "answer", "value_selector": ["tpl", "output"]}]}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "tpl"},
                {"id": "e2", "source": "tpl", "target": "end"},
            ],
        },
    },
}


def main() -> int:
    ok = True
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  [ok]   " if condition else "  [FAIL] ") + label + (" — " + detail if detail else ""))
        if not condition:
            ok = False
            failures.append(label)

    with TestClient(gateway.app) as client:
        data = client.get("/api/engine/health").json()
        print("内核健康：")
        for name, info in data["kernels"].items():
            check(f"{name} 内核可用", bool(info.get("ok")),
                  json.dumps(info.get("data") or info.get("error"), ensure_ascii=False)[:150])

        cfg = client.get("/api/engine/config").json()
        check("统一模型配置（key 打码）", bool(cfg.get("models")),
              json.dumps({k: v for k, v in list(cfg["models"].items())[:4]}, ensure_ascii=False))

        supported = client.get("/api/engine/knowledge/supported").json()
        check("知识内核解析方法表", len(supported.get("chunk_methods") or []) >= 10,
              ",".join(supported.get("chunk_methods") or [])[:120])

        tok = client.post("/api/engine/knowledge/tokenize", json={"text": "装备技术资料智能问答系统"}).json()
        check("知识内核分词", int(tok.get("count") or 0) > 0, f"{tok.get('count')} 词")

        if DOCX.exists():
            chunk = client.post("/api/engine/knowledge/chunk", json={
                "path": str(DOCX), "chunk_method": "naive",
                "parser_config": {"chunk_token_num": 256}, "to_page": 2, "max_chunks": 3,
            }).json()
            check("知识内核解析真实 docx", int(chunk.get("count") or 0) > 0,
                  f"{chunk.get('count')} 片 / {chunk.get('total_chars')} 字 / {chunk.get('elapsed_ms')} ms")
        else:
            print("  [skip] 样例 docx 不在，跳过真实解析")

        run = client.post("/api/engine/orchestration/run", json={
            "dsl": DSL, "inputs": {"question": "网关自检"},
        }).json()
        check("编排内核运行 DSL", run.get("status") == "succeeded",
              f"status={run.get('status')} outputs={json.dumps(run.get('outputs') or {}, ensure_ascii=False)[:120]}")

    print()
    print("网关自检：", "通过" if ok else ("失败：" + "、".join(failures)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
