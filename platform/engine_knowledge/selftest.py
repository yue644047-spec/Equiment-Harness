r"""知识引擎内核自检：用 RAGFlow 源码真实解析一份交付文档并输出切片。

用法：& $env:KNOWLEDGE_PYTHON platform\engine_knowledge\selftest.py [可选文件路径]
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
PYTHON = Path(r"E:\llm-stack\src\ragflow-py\.venv\Scripts\python.exe")

REPO = HERE.parents[1]  # D:\app\dify
CANDIDATES = [
    Path(r"D:\app\审价\1、385万（4号文）\交付成果\06_开发计划.docx"),
    Path(r"D:\app\审价\1、385万（4号文）\交付成果\07_用户手册.docx"),
    *sorted(REPO.glob("platform/data/files/*/06_开发计划.docx")),
]


def call(reqs: list[dict]) -> list[dict]:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [str(PYTHON), str(HERE / "worker.py")],
        input="\n".join(json.dumps(r, ensure_ascii=False) for r in reqs),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, env=env,
    )
    if proc.returncode != 0:
        print("worker stderr:", (proc.stderr or "")[-1500:])
    out = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else next((p for p in CANDIDATES if p.exists()), None)
    if target is None:
        print("[SKIP] 找不到样例交付文档，请传入路径")
        return 2
    print("样例文件：", target, f"({target.stat().st_size // 1024} KB)")
    responses = call([
        {"id": 1, "op": "health"},
        {"id": 2, "op": "supported"},
        {"id": 3, "op": "chunk", "path": str(target), "chunk_method": "naive",
         "parser_config": {"chunk_token_num": 256, "delimiter": "\n!?;。；！？"}, "to_page": 3},
    ])
    by_id = {r.get("id"): r for r in responses}
    health = by_id.get(1, {})
    chunk = by_id.get(3, {})

    ok = True
    if not health.get("ok"):
        print("[FAIL] health:", health.get("error"))
        ok = False
    else:
        print("[OK] 内核源码：", health["data"]["source"], "| Python", health["data"]["python"])
    if not chunk.get("ok"):
        print("[FAIL] chunk:", chunk.get("error"))
        print((chunk.get("trace") or "")[-800:])
        ok = False
    else:
        data = chunk["data"]
        print(f"[OK] 解析分块：{data['count']} 片 / {data['total_chars']} 字 / {data['elapsed_ms']} ms",
              f"| 解析器 {data['parser_module']}")
        for item in data["chunks"][:3]:
            preview = item["content"][:90].replace("\n", " ")
            print(f"     #{item['index'] + 1} {item['chars']}字 表={item['has_table']} :: {preview}")
    print()
    print("知识内核自检：", "通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
