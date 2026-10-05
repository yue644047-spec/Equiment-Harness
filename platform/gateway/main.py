"""装备数据治理 · 统一网关（FastAPI）。

一个进程对外提供一套契约，对内组合两个内核（知识内核 = RAGFlow 源码，编排内核 = Dify graphon）。
与旧 BFF 的区别：旧 BFF 通过 HTTP 调现成的 RAGFlow/Dify 服务；本网关直接调用两个内核的源码。

运行：
  python platform/gateway/main.py            # 默认 127.0.0.1:8190
  uvicorn main:app --host 127.0.0.1 --port 8190   # 在 gateway 目录下
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine_client import ENV, build_workers, masked_config  # noqa: E402

app = FastAPI(title="装备数据治理 · 引擎网关", version="0.1.0")
WORKERS = build_workers()


class ChunkRequest(BaseModel):
    path: Optional[str] = None
    name: Optional[str] = None
    text: Optional[str] = None
    chunk_method: str = "naive"
    parser_config: Dict[str, Any] = Field(default_factory=dict)
    from_page: int = 0
    to_page: int = 100000000
    lang: str = "Chinese"
    max_chunks: int = 0          # >0 时只回前 N 片（用于预览）


class TokenizeRequest(BaseModel):
    text: str


class RunRequest(BaseModel):
    dsl: Any
    inputs: Dict[str, Any] = Field(default_factory=dict)
    credentials: Dict[str, Any] = Field(default_factory=dict)


class InspectRequest(BaseModel):
    dsl: Any


async def call_kernel(kernel: str, op: str, timeout: float = 900.0, **payload: Any) -> Dict[str, Any]:
    worker = WORKERS[kernel]
    try:
        response = await run_in_threadpool(lambda: worker.call(op, timeout=timeout, **payload))
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"{kernel} 内核调用失败：{exc}")
    if not response.get("ok"):
        raise HTTPException(status_code=400, detail={
            "kernel": kernel,
            "op": op,
            "error": response.get("error"),
            "trace": (response.get("trace") or "")[-800:],
        })
    return response.get("data") or {}


# ================================================================ 健康与配置


@app.get("/api/engine/health")
async def health() -> Dict[str, Any]:
    """两个内核的可用性 + 配置摘要。"""
    result: Dict[str, Any] = {"gateway": "ok", "kernels": {}}
    for name in ("knowledge", "orchestration"):
        worker = WORKERS[name]
        try:
            data = await call_kernel(name, "health", timeout=180.0)
            result["kernels"][name] = {"ok": True, "alive": worker.alive, "restarts": worker.restarts, "data": data}
        except HTTPException as exc:
            result["kernels"][name] = {"ok": False, "alive": worker.alive, "restarts": worker.restarts,
                                       "error": exc.detail, "stderr": worker.stderr_tail()}
    result["config"] = masked_config()
    return result


@app.get("/api/engine/config")
async def config() -> Dict[str, Any]:
    """统一配置（模型 url + api key 已打码）。"""
    return masked_config()


# ================================================================ 知识内核


@app.get("/api/engine/knowledge/supported")
async def knowledge_supported() -> Dict[str, Any]:
    """支持的解析方法（chunk_method）与扩展名映射。"""
    return await call_kernel("knowledge", "supported", timeout=120.0)


@app.post("/api/engine/knowledge/tokenize")
async def knowledge_tokenize(payload: TokenizeRequest) -> Dict[str, Any]:
    """用 RAGFlow 的分词器切词（检索侧同源，保证口径一致）。"""
    return await call_kernel("knowledge", "tokenize", timeout=120.0, text=payload.text)


@app.post("/api/engine/knowledge/chunk")
async def knowledge_chunk(payload: ChunkRequest) -> Dict[str, Any]:
    """解析 + 分块（直接调用 RAGFlow 源码的解析器）。"""
    if not payload.path and payload.text is None:
        raise HTTPException(status_code=400, detail="必须提供 path 或 text")
    data = await call_kernel(
        "knowledge", "chunk",
        path=payload.path, name=payload.name, text=payload.text,
        chunk_method=payload.chunk_method, parser_config=payload.parser_config,
        from_page=payload.from_page, to_page=payload.to_page, lang=payload.lang,
    )
    if payload.max_chunks and payload.max_chunks > 0:
        data = dict(data)
        data["chunks"] = (data.get("chunks") or [])[: payload.max_chunks]
        data["truncated"] = True
    return data


# ================================================================ 编排内核


@app.post("/api/engine/orchestration/inspect")
async def orchestration_inspect(payload: InspectRequest) -> Dict[str, Any]:
    """解析 DSL：可执行性、节点数、节点类型、插件依赖。"""
    return await call_kernel("orchestration", "inspect", timeout=180.0, dsl=payload.dsl)


@app.post("/api/engine/orchestration/run")
async def orchestration_run(payload: RunRequest) -> Dict[str, Any]:
    """运行 DSL 图（Dify 的 graphon 引擎），返回输出与事件回放。"""
    return await call_kernel(
        "orchestration", "run",
        dsl=payload.dsl, inputs=payload.inputs, credentials=payload.credentials,
    )


@app.on_event("shutdown")
async def shutdown() -> None:
    for worker in WORKERS.values():
        worker.stop()


def main() -> int:
    import uvicorn
    host = ENV.get("GATEWAY_HOST", "127.0.0.1")
    port = int(ENV.get("GATEWAY_PORT", "8190"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
