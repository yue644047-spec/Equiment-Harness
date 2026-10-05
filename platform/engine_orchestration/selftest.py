r"""编排引擎内核自检：用 Dify 的工作流引擎 graphon 跑通一张最小 DSL 图。

用法：& $ORCHESTRATION_PYTHON platformengine_orchestrationselftest.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
PYTHON = Path(r"E:\llm-stack\src\dify\api\.venv\Scripts\python.exe")

# 最小工作流 DSL（Dify 图结构）：开始 → 代码节点 → 结束
DSL = {
    # graphon 需要顶层 kind 声明 DSL 类型："app" = Dify 应用导出格式（含 workflow.graph）
    "kind": "app",
    "app": {"mode": "workflow", "name": "装备数据治理 · 编排内核自检"},
    "workflow": {
        "environment_variables": [],
        "features": {},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "custom",
                    "data": {
                        "type": "start",
                        "title": "开始",
                        "variables": [
                            {"variable": "question", "label": "问题", "type": "text-input",
                             "required": True, "max_length": 512},
                        ],
                    },
                },
                {
                    "id": "tpl1",
                    "type": "custom",
                    "data": {
                        "type": "template-transform",
                        "title": "回答拼装",
                        "template": "收到问题：{{ question }}（字数 {{ question | length }}）",
                        "variables": [{"variable": "question", "value_selector": ["start", "question"]}],
                    },
                },
                {
                    "id": "end",
                    "type": "custom",
                    "data": {
                        "type": "end",
                        "title": "结束",
                        "outputs": [{"variable": "answer", "value_selector": ["tpl1", "output"]}],
                    },
                },
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "tpl1", "sourceHandle": "source", "targetHandle": "target"},
                {"id": "e2", "source": "tpl1", "target": "end", "sourceHandle": "source", "targetHandle": "target"},
            ],
            "viewport": {"x": 0, "y": 0, "zoom": 1},
        },
    },
}


def call(reqs: list[dict]) -> list[dict]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [str(PYTHON), str(HERE / "worker.py")],
        input="\n".join(json.dumps(r, ensure_ascii=False) for r in reqs),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, env=env,
    )
    if proc.returncode != 0:
        print("worker stderr:", (proc.stderr or "")[-1200:])
    return [json.loads(line) for line in (proc.stdout or "").splitlines() if line.strip()]


def main() -> int:
    responses = call([
        {"id": 1, "op": "health"},
        {"id": 2, "op": "inspect", "dsl": DSL},
        {"id": 3, "op": "run", "dsl": DSL, "inputs": {"question": "滤芯多久更换一次？"}},
    ])
    by_id = {r.get("id"): r for r in responses}
    ok = True

    health = by_id.get(1, {})
    if health.get("ok"):
        data = health["data"]
        print(f"[OK] 编排内核：{data['engine']} {data['engine_version']} | Python {data['python']}")
    else:
        print("[FAIL] health:", health.get("error"), (health.get("trace") or "")[-500:])
        ok = False

    plan = by_id.get(2, {})
    if plan.get("ok"):
        data = plan["data"]
        print(f"[OK] DSL 解析：{data['load_status']} 节点 {data['node_count']} 边 {data['edge_count']} 类型 {data['node_types']}")
    else:
        print("[FAIL] inspect:", plan.get("error"), (plan.get("trace") or "")[-600:])
        ok = False

    run = by_id.get(3, {})
    if run.get("ok"):
        data = run["data"]
        print(f"[OK] 图运行：status={data['status']} 事件 {data['event_count']} 条 {data['elapsed_ms']} ms")
        print("     输出：", json.dumps(data["outputs"], ensure_ascii=False))
        if data["status"] != "succeeded":
            print("     错误：", data["error"])
            for event in data["events"]:
                if event.get("error"):
                    print("     事件错误：", event["type"], str(event["error"])[:200])
            ok = False
    else:
        print("[FAIL] run:", run.get("error"))
        print((run.get("trace") or "")[-800:])
        ok = False

    print()
    print("编排内核自检：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
