"""编排引擎内核（直接套用 Dify 的工作流引擎 graphon，不经过 Dify 服务）。

协议：stdin 一行一个 JSON 请求，stdout 一行一个 JSON 响应（JSON-lines）。
  {"id": 1, "op": "health"}
  {"id": 2, "op": "inspect", "dsl": "...DSL 文本或对象..."}
  {"id": 3, "op": "parse", "dsl": "...外部 YAML/JSON 文本..."}   → {mode, kind, dsl}
  {"id": 4, "op": "run", "dsl": "...", "inputs": {...}, "credentials": {"deepseek": {"api_key": "..."}}}

依赖：必须用 Dify 的解释器运行（ORCHESTRATION_PYTHON），因为 graphon 是 Dify 1.17 的工作流
引擎包（E:\llm-stack\src\dify\api\.venv\Lib\site-packages\graphon）。图执行、节点调度、
变量池、事件流全部由 graphon 负责，这里只做"装配与回放"。
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def _load_env() -> Dict[str, str]:
    here = Path(__file__).resolve().parents[1]
    env_file = Path(os.environ.get("EQUIPMENT_ENV", here / "config" / "equipment.env"))
    values: Dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    for key, value in os.environ.items():
        if key.startswith(("ORCHESTRATION_", "EQUIPMENT_", "MODEL_", "SLIM_")):
            values[key] = value
    return values


ENV = _load_env()
# slim 客户端在子进程里读 os.environ（SlimClientConfig.build_env 从 os.environ 复制），
# 因此把配置文件里的 SLIM_* 回灌到进程环境，否则 SLIM_BINARY_PATH 对子进程不可见。
for _key, _value in ENV.items():
    if _key.startswith("SLIM_") and _value:
        os.environ.setdefault(_key, _value)


SOURCE = Path(ENV.get("ORCHESTRATION_SOURCE", r"E:\llm-stack\src\dify\api"))
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))


def _dsl_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _credentials(req: Dict[str, Any]) -> Dict[str, Any]:
    """装配 graphon 需要的凭据结构（model_credentials / tool_credentials / code / slim）。

    graphon 的 DslCredentials 是强校验模型（extra=forbid），所以这里严格按它的字段拼：
      model_credentials: [{provider|plugin_unique_identifier, values:{api_key, base_url, mode}}]
      slim:              {mode, daemon_addr, daemon_key}      ← 模型插件执行通道（LLM/工具节点）
      code:              {execution_endpoint, execution_api_key}  ← 代码节点沙箱（Dify sandbox 协议）
    模型值来自统一配置 MODEL_*（url + api key），请求里传入的凭据优先。
    """
    supplied = req.get("credentials")
    if isinstance(supplied, dict) and "model_credentials" in supplied:
        return supplied

    models: List[Dict[str, Any]] = []
    chat_provider = ENV.get("MODEL_CHAT_PROVIDER", "")
    if chat_provider and (ENV.get("MODEL_CHAT_BASE_URL") or ENV.get("MODEL_CHAT_API_KEY")):
        models.append(_model_credential(chat_provider))
    emb_provider = ENV.get("MODEL_EMBEDDING_PROVIDER", "")
    if emb_provider and ENV.get("MODEL_EMBEDDING_BASE_URL"):
        models.append({
            "provider": emb_provider,
            "values": {
                "api_key": ENV.get("MODEL_EMBEDDING_API_KEY", ""),
                "base_url": ENV.get("MODEL_EMBEDDING_BASE_URL", ""),
            },
        })
    if isinstance(supplied, dict):
        for provider, values in supplied.items():
            if isinstance(values, dict):
                models.append({"provider": provider, "values": values})

    creds: Dict[str, Any] = {"model_credentials": models}
    slim_mode = ENV.get("SLIM_MODE", "").strip()
    if slim_mode:
        creds["slim"] = {
            "mode": slim_mode,
            "daemon_addr": ENV.get("SLIM_DAEMON_ADDR", ""),
            "daemon_key": ENV.get("SLIM_DAEMON_KEY", ""),
        }
    endpoint = ENV.get("CODE_EXEC_ENDPOINT", "")
    if endpoint:
        creds["code"] = {
            "execution_endpoint": endpoint,
            "execution_api_key": ENV.get("CODE_EXEC_API_KEY", ""),
        }
    return creds


def _model_credential(provider: str) -> Dict[str, Any]:
    """对话模型的凭据条目。

    DSL 里 LLM 节点的 provider 写作 ``langgenius/deepseek/deepseek``（Dify 全限定名），
    而统一配置里的 MODEL_CHAT_PROVIDER 只有短名 ``deepseek``；两者对不上会导致凭据匹配失败。
    配置了 MODEL_CHAT_PLUGIN_ID 时改用插件唯一标识匹配，短名/全限定名都能命中同一个插件。
    """
    values = {
        "api_key": ENV.get("MODEL_CHAT_API_KEY", ""),
        "base_url": ENV.get("MODEL_CHAT_BASE_URL", ""),
        "mode": ENV.get("MODEL_CHAT_MODE", "chat"),
    }
    plugin_id = ENV.get("MODEL_CHAT_PLUGIN_ID", "").strip()
    if plugin_id:
        return {"plugin_unique_identifier": plugin_id, "values": values}
    return {"provider": provider, "values": values}


# ---------------------------------------------------------------- 操作


def op_health(_req: Dict[str, Any]) -> Dict[str, Any]:
    import graphon
    from graphon.dsl import loads  # noqa: F401  证明引擎可用
    return {
        "ok": True,
        "engine": "graphon",
        "engine_version": getattr(graphon, "__version__", "unknown"),
        "package_path": str(Path(graphon.__file__).parent),
        "source": str(SOURCE),
        "python": sys.version.split()[0],
        "credentials": sorted(_credentials({})),
    }


def op_inspect(req: Dict[str, Any]) -> Dict[str, Any]:
    from graphon.dsl import inspect as dsl_inspect
    plan = dsl_inspect(_dsl_text(req.get("dsl")))
    document = plan.document
    graph = document.graph_config
    nodes = list((graph or {}).get("nodes") or []) if isinstance(graph, dict) else []
    edges = list((graph or {}).get("edges") or []) if isinstance(graph, dict) else []
    return {
        "load_status": str(plan.load_status),
        "kind": str(document.kind),
        "reason": plan.load_reason,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_types": sorted({str((n.get("data") or {}).get("type")) for n in nodes}),
        "dependencies": [str(d) for d in (plan.dependencies or [])],
    }


def op_parse(req: Dict[str, Any]) -> Dict[str, Any]:
    """把外部 DSL 文本（YAML 或 JSON）解析为结构化对象，并给出 Dify 应用模式。

    产品服务侧的解释器（门户 BFF venv）没有 PyYAML，而内核解释器（Dify api venv）有；
    因此外部 .yml 的解析在这里代劳，只回传 JSON 可序列化的结果，产品侧不引入 yaml 依赖。
    """
    import yaml
    text = _dsl_text(req.get("dsl"))
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"不是合法的 YAML/JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("DSL 内容必须是映射（顶层为 key: value）")
    app = payload.get("app")
    mode = str(app.get("mode") or "") if isinstance(app, dict) else ""
    kind = str(payload.get("kind") or "")
    return {"mode": mode, "kind": kind, "dsl": payload}


def _event_summary(event: Any) -> Dict[str, Any]:
    """把 graphon 事件压成可回放的摘要（保留类型与关键字段）。"""
    item: Dict[str, Any] = {"type": type(event).__name__}
    for field in ("node_id", "node_type", "title", "status", "error", "outputs", "inputs",
                  "workflow_id", "execution_id"):
        value = getattr(event, field, None)
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)):
            item[field] = value
        else:
            item[field] = str(value)[:400]
    return item


def _inject_dependencies(dsl_text: str) -> str:
    """运行前给 DSL 顶层补齐插件依赖（dependencies）。

    graphon 的导入器从 DSL 顶层 ``dependencies`` 解析插件标识（importer._extract_dependencies），
    为空时 LLM / 工具 / 问题分类器 / 参数提取器等节点会因找不到插件报 dependency.missing_plugin。
    存量 DSL（含骨架生成）大多写的是 ``dependencies: []``，这里按统一配置补上对话模型插件；
    仅当原值为空时注入，用户/DSL 里显式声明的依赖原样保留。
    """
    plugin_id = ENV.get("MODEL_CHAT_PLUGIN_ID", "").strip()
    if not plugin_id:
        return dsl_text
    try:
        payload = json.loads(dsl_text)
    except (TypeError, ValueError):
        return dsl_text
    if not isinstance(payload, dict) or payload.get("dependencies"):
        return dsl_text
    payload["dependencies"] = [{
        "type": "marketplace",
        "value": {"marketplace_plugin_unique_identifier": plugin_id},
        "current_identifier": plugin_id,
    }]
    return json.dumps(payload, ensure_ascii=False)


def op_run(req: Dict[str, Any]) -> Dict[str, Any]:
    from graphon.dsl import loads
    dsl_text = _inject_dependencies(_dsl_text(req.get("dsl")))
    inputs = dict(req.get("inputs") or {})
    started = time.perf_counter()
    engine = loads(dsl_text, credentials=_credentials(req), start_inputs=inputs)
    events: List[Dict[str, Any]] = []
    outputs: Dict[str, Any] = {}
    status = "unknown"
    error = ""
    for event in engine.run():
        name = type(event).__name__
        events.append(_event_summary(event))
        if name == "GraphRunSucceededEvent":
            status = "succeeded"
            raw = getattr(event, "outputs", None)
            if isinstance(raw, dict):
                outputs = raw
        elif name in ("GraphRunFailedEvent", "GraphRunAbortedEvent", "GraphRunPartialSucceededEvent"):
            status = name.replace("GraphRun", "").replace("Event", "").lower()
            error = str(getattr(event, "error", "") or "")[:500]
    return {
        "status": status,
        "outputs": outputs,
        "error": error,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "event_count": len(events),
        "events": events[-60:],
    }


OPS = {"health": op_health, "inspect": op_inspect, "parse": op_parse, "run": op_run}


def handle(req: Dict[str, Any]) -> Dict[str, Any]:
    op = str(req.get("op") or "")
    if op not in OPS:
        return {"ok": False, "error": f"未知操作：{op}", "ops": sorted(OPS)}
    try:
        return {"ok": True, "data": OPS[op](req)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc()[-1500:]}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": f"请求不是合法 JSON：{exc}"}, ensure_ascii=False), flush=True)
            continue
        resp = handle(req)
        resp["id"] = req.get("id")
        print(json.dumps(resp, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
