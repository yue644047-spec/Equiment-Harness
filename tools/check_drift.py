# -*- coding: utf-8 -*-
r"""漂移检查：防止文档/代码/线上工作流之间出现"说一套做一套"。

检查三件事（离线即可跑，不需要凭据）：
  1. 文档中引用的 DSL 文件名必须真实存在（历史上出现过 README/测试引用旧文件名导致整条自检跑不起来）；
  2. 禁止再引用 dsl/_v1-原始备份/ 里的旧文件名（只能显式写全路径）；
  3. 代码里 load_dsl("xxx.yml") / 直接打开 dsl/xxx.yml 的文件名必须存在。

可选（--online）：比对线上 Dify 草稿与本地 DSL 的 code / prompt_template 是否一致，
用于确认"改了本地 DSL 但忘了发布"。需要 E:\llm-stack\setup\dify_bootstrap.py 可用。

用法：
    python tools/check_drift.py            # 离线检查
    python tools/check_drift.py --online   # 追加线上一致性检查（需要 Dify 控制台可用）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DSL_DIR = ROOT / "dsl"
BACKUP = "_v1-原始备份"

#: 形如 dsl/xxx.yml 或 dsl\xxx.yml 的引用
DSL_REF_RE = re.compile(r"dsl[\\/]([^\\/\s\`\"')]+\.ya?ml)")
#: 代码里 load_dsl("xxx.yml") / "dsl" / "xxx.yml"
LOAD_DSL_RE = re.compile(r"load_dsl\(\s*[\"']([^\"']+\.ya?ml)[\"']\s*\)")

failures: list[str] = []
checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        failures.append(name)


#: 文档里的通配符/占位写法（dsl/*.yml、xxx.yml）不算引用
PLACEHOLDER_RE = re.compile(r"[*?]|^x+\.ya?ml$", re.I)


def _is_placeholder(name: str) -> bool:
    return bool(PLACEHOLDER_RE.search(name))


def real_dsl_names() -> set[str]:
    return {p.name for p in DSL_DIR.glob("*.y*ml")}


def backup_dsl_names() -> set[str]:
    backup = DSL_DIR / BACKUP
    if not backup.exists():
        return set()
    return {p.name for p in backup.glob("*.y*ml")}


def doc_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "AGENTS.md"]
    files += sorted((ROOT / "docs").glob("*.md"))
    files += sorted((ROOT / "deploy").glob("*.md")) if (ROOT / "deploy").exists() else []
    return [f for f in files if f.exists()]


def code_files() -> list[Path]:
    files: list[Path] = []
    for sub in ("tools", "eval_service", "kb_bridge"):
        base = ROOT / sub
        if base.exists():
            files += [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]
    return files


def check_doc_refs() -> None:
    print("\n=== 1. 文档中的 DSL 引用 ===")
    real, backup = real_dsl_names(), backup_dsl_names()
    for path in doc_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in sorted(set(DSL_REF_RE.findall(text))):
            rel = path.relative_to(ROOT)
            if name in real or _is_placeholder(name):
                continue
            if name in backup:
                check(f"{rel} 不得引用旧版 DSL（{name} 已归档到 {BACKUP}/）", False,
                      "请改用当前文件名")
            else:
                check(f"{rel} 引用的 {name} 不存在", False, f"现有：{sorted(real)}")


def check_code_refs() -> None:
    print("\n=== 2. 代码中的 DSL 文件名 ===")
    real = real_dsl_names()
    for path in code_files():
        if path.name == Path(__file__).name:  # 跳过本文件里的示例字符串
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in sorted(set(LOAD_DSL_RE.findall(text))):
            if _is_placeholder(name):
                continue
            check(f"{path.relative_to(ROOT)} 引用的 {name} 存在", name in real,
                  f"现有：{sorted(real)}")


def check_dsl_set() -> None:
    print("\n=== 3. DSL 文件集合 ===")
    real = real_dsl_names()
    expected = {
        "01-多模态智能问答-Chatflow.yml",
        "02-满意度评价-Workflow.yml",
        "03-多模态语料标注-Workflow.yml",
    }
    check("当前 DSL 集合与基线一致", real == expected,
          f"实际={sorted(real)} 期望={sorted(expected)}")


def check_online() -> None:
    print("\n=== 4. 线上草稿 vs 本地 DSL（--online）===")
    sys.path.insert(0, r"E:\llm-stack\setup")
    try:
        import yaml  # noqa: PLC0415
        from dify_bootstrap import Console  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        check("线上检查依赖可用", False, f"{type(exc).__name__}: {exc}")
        return

    targets = [
        ("02-满意度评价-Workflow.yml", "满意度评价"),
        ("03-多模态语料标注-Workflow.yml", "语料标注"),
    ]
    console = Console()
    if not console.login():
        check("控制台登录", False, "登录失败，跳过线上一致性检查")
        return
    apps = console.get("/console/api/apps", params={"page": 1, "limit": 50}).json().get("data") or []
    for filename, keyword in targets:
        app = next((a for a in apps if keyword in str(a.get("name"))), None)
        if not app:
            check(f"{filename} 对应的线上应用存在", False, f"未找到含「{keyword}」的应用")
            continue
        local = yaml.safe_load((DSL_DIR / filename).read_text(encoding="utf-8"))
        local_nodes = {n["id"]: (n.get("data") or {})
                       for n in local["workflow"]["graph"]["nodes"]}
        draft = console.get(f"/console/api/apps/{app['id']}/workflows/draft").json()
        gaps = []
        for node in (draft.get("graph") or {}).get("nodes") or []:
            node_id = node.get("id")
            here, there = local_nodes.get(node_id), (node.get("data") or {})
            if not here:
                continue
            for field in ("code", "prompt_template", "variables", "outputs"):
                if field in here and here[field] != there.get(field):
                    gaps.append(f"{node_id}.{field}")
        check(f"{filename} 线上草稿与本地一致", not gaps, f"有差异：{gaps[:6]}")


def main() -> int:
    print("漂移检查（文档 / 代码 / 线上）")
    check_dsl_set()
    check_doc_refs()
    check_code_refs()
    if "--online" in sys.argv:
        check_online()
    print(f"\n漂移检查结束：{checks} 项，失败 {len(failures)} 项")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
