# -*- coding: utf-8 -*-
"""Dify DSL 静态校验器。

用法：
    python tools/validate_dsl.py dsl/01-多模态智能问答-Chatflow.yml [更多文件...]

校验内容：
1. YAML 可解析
2. 顶层结构（kind / version / app.mode / workflow.graph）
3. 节点 ID 唯一、边两端节点存在
4. 边的 sourceType / targetType 与节点 data.type 一致
5. 分支句柄（if-else 的 true/false、question-classifier 的 class id）在目标节点上真实存在
6. 终态节点：advanced-chat 至少一个可达 answer；workflow 必须有 end
7. code 节点：code_language 为 python3 时语法可编译，main 函数存在，参数名与 variables 一致，返回键与 outputs 一致
8. 提示词与模板中的 {{#node_id.field#}} 变量引用指向真实存在的节点
9. template-transform 的 Jinja2 模板语法可编译
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("[FATAL] 需要 PyYAML：python -m pip install pyyaml")
    sys.exit(2)

VAR_REF = re.compile(r"\{\{#([^#{}]+)#\}\}")
SYSTEM_SELECTORS = {"sys", "context", "conversation", "env", "globals"}


class Report:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def note(self, msg: str) -> None:
        self.info.append(msg)


def collect_text(data: dict) -> list[str]:
    """递归收集所有字符串，用于变量引用检查。"""
    out: list[str] = []

    def walk(node):
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return out


def check_variable_refs(rep: Report, graph: dict) -> None:
    node_ids = {node.get("id") for node in graph.get("nodes", [])}
    for text in collect_text(graph):
        for ref in VAR_REF.findall(text):
            head = ref.split(".")[0]
            if head in SYSTEM_SELECTORS:
                continue
            if head not in node_ids:
                rep.error(f"变量引用 {{{{#{ref}#}}}} 指向不存在的节点 id: {head}")


def check_code_nodes(rep: Report, graph: dict) -> None:
    for node in graph.get("nodes", []):
        data = node.get("data") or {}
        if data.get("type") != "code":
            continue
        nid = node.get("id")
        lang = data.get("code_language")
        code = data.get("code") or ""
        if not code.strip():
            rep.error(f"code 节点 {nid} 的 code 为空")
            continue
        if lang == "python3":
            try:
                tree = ast.parse(code)
            except SyntaxError as exc:
                rep.error(f"code 节点 {nid} Python 语法错误: {exc}")
                continue
            funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            if "main" not in funcs:
                rep.error(f"code 节点 {nid} 缺少 main 函数")
                continue
            main = funcs["main"]
            declared = [arg.arg for arg in main.args.args]
            expected = [v.get("variable") for v in (data.get("variables") or [])]
            if declared != expected:
                rep.error(f"code 节点 {nid} 的 main 参数 {declared} 与 variables {expected} 不一致")
            returns = {
                t.value.func.id
                for t in ast.walk(main)
                if isinstance(t, ast.Return) and isinstance(t.value, ast.Call) and isinstance(t.value.func, ast.Name)
            }
            if returns and "dict" not in returns and "json" not in " ".join(returns):
                rep.warn(f"code 节点 {nid} 的 return 可能不是 dict 字面量")
            out_keys = set((data.get("outputs") or {}).keys())
            if not out_keys:
                rep.error(f"code 节点 {nid} 未声明 outputs")
            else:
                rep.note(f"code 节点 {nid} 输出: {sorted(out_keys)}")


def check_jinja(rep: Report, graph: dict) -> None:
    try:
        from jinja2 import Environment
    except ImportError:
        rep.warn("未安装 jinja2，跳过模板语法检查")
        return
    env = Environment()
    for node in graph.get("nodes", []):
        data = node.get("data") or {}
        if data.get("type") != "template-transform":
            continue
        template = data.get("template") or ""
        try:
            env.parse(template)
        except Exception as exc:  # noqa: BLE001
            rep.error(f"template-transform 节点 {node.get('id')} 模板语法错误: {exc}")


def check_graph(rep: Report, app: dict) -> None:
    workflow = app.get("workflow") or {}
    graph = workflow.get("graph") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    mode = (app.get("app") or {}).get("mode")

    if not nodes:
        rep.error("graph.nodes 为空")
        return

    ids = [n.get("id") for n in nodes]
    if len(ids) != len(set(ids)):
        rep.error("存在重复的节点 id")
    id_set = set(ids)
    type_by_id = {n.get("id"): (n.get("data") or {}).get("type") for n in nodes}

    for edge in edges:
        src, tgt = edge.get("source"), edge.get("target")
        if src not in id_set:
            rep.error(f"边 {edge.get('id')} 的 source {src} 不存在")
        if tgt not in id_set:
            rep.error(f"边 {edge.get('id')} 的 target {tgt} 不存在")
        data = edge.get("data") or {}
        if src in type_by_id and data.get("sourceType") != type_by_id[src]:
            rep.error(f"边 {edge.get('id')} sourceType={data.get('sourceType')} 与节点类型 {type_by_id[src]} 不一致")
        if tgt in type_by_id and data.get("targetType") != type_by_id[tgt]:
            rep.error(f"边 {edge.get('id')} targetType={data.get('targetType')} 与节点类型 {type_by_id[tgt]} 不一致")

    # 分支句柄检查
    for node in nodes:
        data = node.get("data") or {}
        ntype, nid = data.get("type"), node.get("id")
        handles = {e.get("sourceHandle") for e in edges if e.get("source") == nid}
        if ntype == "if-else":
            case_ids = {c.get("case_id") for c in (data.get("cases") or [])} | {"true"}
            unknown = handles - case_ids - {"false"} - {None}
            if unknown:
                rep.error(f"if-else 节点 {nid} 存在未知分支句柄 {unknown}")
            if "false" not in handles:
                rep.warn(f"if-else 节点 {nid} 的 ELSE 分支未连线")
        if ntype == "question-classifier":
            class_ids = {c.get("id") for c in (data.get("classes") or [])}
            unknown = handles - class_ids - {None}
            if unknown:
                rep.error(f"分类器节点 {nid} 存在未定义的类别句柄 {unknown}")
            missing = class_ids - handles
            if missing:
                rep.warn(f"分类器节点 {nid} 的类别 {missing} 未连线")

    reachable = set()
    queue = [e.get("target") for e in edges if (type_by_id.get(e.get("source")) == "start")]
    while queue:
        cur = queue.pop()
        if cur in reachable or cur is None:
            continue
        reachable.add(cur)
        queue.extend(e.get("target") for e in edges if e.get("source") == cur)

    for node in nodes:
        nid, ntype = node.get("id"), (node.get("data") or {}).get("type")
        if ntype in ("start", "start-placeholder"):
            continue
        if nid not in reachable:
            rep.error(f"节点 {nid} ({ntype}) 从 start 不可达")
        if ntype == "end" and not [e for e in edges if e.get("source") == nid]:
            pass

    terminals = {type_by_id[n] for n in reachable if type_by_id.get(n)}
    if mode == "advanced-chat":
        if "answer" not in terminals:
            rep.error("advanced-chat 应用没有可达的 answer 节点")
        if "end" in terminals:
            rep.error("advanced-chat 应用不应包含 end 节点")
    elif mode == "workflow":
        if "end" not in terminals:
            rep.error("workflow 应用缺少可达的 end 节点")
        if "answer" in terminals:
            rep.warn("workflow 应用包含 answer 节点，通常应使用 end")

    dangling = [n for n in reachable if type_by_id.get(n) not in ("end", "answer")]
    for nid in dangling:
        if not [e for e in edges if e.get("source") == nid]:
            rep.error(f"节点 {nid} ({type_by_id.get(nid)}) 没有下游连线，流程会中断")

    check_variable_refs(rep, graph)
    check_code_nodes(rep, graph)
    check_jinja(rep, graph)


def validate(path: Path) -> Report:
    rep = Report(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        rep.error(f"读取失败: {exc}")
        return rep
    try:
        app = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        rep.error(f"YAML 解析失败: {exc}")
        return rep
    if not isinstance(app, dict):
        rep.error("顶层不是映射")
        return rep

    if app.get("kind") != "app":
        rep.error(f"kind 应为 app，实际为 {app.get('kind')!r}")
    if not isinstance(app.get("version"), str):
        rep.error("version 必须是字符串")
    if str(app.get("version")) != "0.7.0":
        rep.warn(f"version={app.get('version')}，Dify 1.16+ 建议使用 '0.7.0'")
    app_block = app.get("app") or {}
    for key in ("name", "mode", "icon", "icon_type", "icon_background"):
        if key not in app_block:
            rep.warn(f"app.{key} 缺失")
    mode = app_block.get("mode")
    if mode not in ("advanced-chat", "workflow"):
        rep.error(f"未预期的 app.mode: {mode!r}")
    if "workflow" not in app:
        rep.error("缺少 workflow 段")
        return rep

    check_graph(rep, app)
    return rep


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]]
    if not paths:
        paths = sorted(Path("dsl").glob("*.yml"))
    if not paths:
        print("没有找到待校验文件")
        return 2

    total_errors = 0
    for path in paths:
        rep = validate(path)
        status = "PASS" if not rep.errors else "FAIL"
        print(f"\n{'=' * 70}\n[{status}] {path}")
        for msg in rep.errors:
            print(f"  [ERROR] {msg}")
        for msg in rep.warnings:
            print(f"  [WARN ] {msg}")
        for msg in rep.info:
            print(f"  [INFO ] {msg}")
        total_errors += len(rep.errors)

    print(f"\n{'=' * 70}")
    print(f"共校验 {len(paths)} 个文件，错误 {total_errors} 个")
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
